"""Shadow mode: the consumption loop's logic, as pure functions.

Phase 2 shipped a validator that reads and reports. This is the half that *acts* on what it
validated — with the tip deliberately blunted. In shadow mode the live behaviour is
bookkeeping: claim requests at-most-once, write proposals, log, journal, render a digest, and
report the sensing verdict. No domain action executes at all.

See docs/superpowers/specs/2026-09-01-lodestar-phase-3-shadow-design.md. Four of its
decisions are load-bearing here and are cheap to break by accident:

**D-L · deterministic and LLM-free.** Nothing in this module or its CLI calls a model.
Capture bodies contain subject lines and sender names composed by people outside work; feeding
those to an unattended agent with file-write authority is a prompt-injection surface with no
mitigation at this scale. A deterministic runner cannot be talked into anything. The cost is
that the token counter reads 0 all week — so it exists anyway, with a cap, a breach path and a
test, and Phase 4 changes a constant rather than a design.

**D-N · claim by rename BEFORE acting.** At-most-once, not at-least-once. A crash mid-batch
loses an action and leaves a partial log; the alternative duplicates an irreversible one.

**D-P · settledness is a git question, not an mtime question.** The sync agent only
auto-commits a tree quiescent for 10 minutes, so a *committed* request file is definitively
finished. Reuse that guarantee instead of adding a second heuristic that can disagree with it.

**D13 · halt is not block.** A cap breach stops the run and exits non-zero. Stopping is the
opposite of waiting: the process exits and the machine is free. Every "continue" leaves a
durable artifact — a log line, a journal event, a status field, a digest 🚩.

`bin/shadow.py` holds the I/O and the argument parsing; everything here is a function over
its inputs so the whole thing is testable under `unittest` with no network and no clock.

Note the module-name shadowing, same as `lib/airlock.py` against `airlock/`: there is an
`airlock/shadow/` directory in the repo, but it is not importable as `shadow` — only the repo
root is ever on sys.path, and `bin/` leads it.
"""

import datetime
import hashlib
import os
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

from . import airlock, clock, frontmatter

# --- who, where, how much ---------------------------------------------------------------

# D-M: exactly one machine consumes. The claim protocol makes a race safe; this keeps it from
# happening, and means a stray install on the Laptop is inert rather than harmful.
CONSUMER_ROLE = "desktop"

LOOP_ID = "airlock-shadow"
MODE = "shadow"

# Caps. Constants and CLI flags, deliberately NOT a config file — a config file would be a
# fifth place for the caps to be wrong.
MAX_ACTIONS_PER_RUN = 20      # ~4x the largest plausible batch; a real queue has been a handful
MAX_ACTIONS_PER_DAY = 60      # three full runs' worth
MAX_BYTES_PER_RUN = 2 * 1024 * 1024   # a normal run writes single-digit KiB
MAX_BYTES_PER_DAY = 8 * 1024 * 1024
MAX_SECONDS_PER_RUN = 120     # slower than this is a bug, and must not still be running at the
                              # next slot
MAX_TOKENS_PER_RUN = 0        # zero in shadow mode (D-L)
MAX_TOKENS_PER_DAY = 0

# `--override-caps` raises every ceiling for one interactive invocation. Interactive-only, per
# D13's rule of thumb: the override exists precisely because a human is present. An overridden
# run is recorded `override: true` so the go-live gate can refuse to count it.
OVERRIDE_MULTIPLIER = 10

# Fallback settledness only (see `settled`).
SETTLE_SECONDS = 120

# A request nobody could consume for this long stops being latency and becomes a stuck queue.
STUCK_SECONDS = 24 * 3600

LOCK_STALE_SECONDS = 15 * 60

DIGEST_DAYS = 14

# Outcomes, recorded in the journal event and the status JSON. `idle` and `ran` are the two
# healthy ones; every other value is a thing to look at.
RAN = "ran"
IDLE = "idle"
HALTED = "halted"
PAUSED = "paused"
DEFERRED = "deferred"
ROLE_SKIP = "role-skip"
CRASHED = "crashed"

# Per-directive verdicts. `executed` exists and is never produced in shadow mode; Phase 4
# fills it in.
PROPOSED = "proposed"
REFUSED = "refused"
EXECUTED = "executed"

# Every repo-relative path a shadow run may write. §9 of the spec makes this a test rather
# than a promise: everything here is inside one git repo, so the entire effect of a shadow
# week is `git revert`-able by construction.
#
# `airlock/requests/` is on the list even though the spec's write-set table omits it, because
# the claim IS a write there — `os.rename` removes the file from that directory. Naming it is
# more honest than a list that quietly does not match the code.
WRITE_ALLOWLIST = (
    "airlock/shadow/",        # proposals
    "airlock/executed/",      # execution receipts (lib/executing) — INSIDE the repo on
                              # purpose, so an execution's whole effect stays revertable by
                              # construction, and so a receipt cannot be written somewhere the
                              # allowlist does not cover. Reached only from the interactive
                              # bin/execute.py; the unattended runner never writes here.
    "airlock/done/",          # claimed requests + their logs
    "airlock/requests/",      # removals only: the claim renames out of here
    "airlock/DIGEST.md",      # the digest
    "BOARD.md",               # one summary line
    "journal/",               # run events
    "loops/%s.md" % LOOP_ID,  # last_touch only
)


def consumer_role() -> str:
    return (os.environ.get("LODESTAR_SHADOW_CONSUMER") or CONSUMER_ROLE).strip().lower()


def is_consumer(role: str) -> bool:
    return role.strip().lower() == consumer_role()


def allowed_write(relpath: str) -> bool:
    """True when `relpath` (repo-relative) is inside the shadow runner's blast radius.

    **Normalise before matching, or the allowlist does not contain anything.** This used
    to `lstrip("./")` and prefix-match, which had two holes found on 2026-09-01:

    - `lstrip` strips *characters*, not a prefix, so `airlock/shadow/../../../../tmp/pwn.md`
      opened with an allowed directory, matched, and then climbed out of the repo. It
      returned `True`.
    - An absolute path was never rejected, so `/airlock/shadow/x.md` could match too.

    The old negative case `../outside.txt` was refused, but incidentally — stripping turned
    it into `outside.txt`, which simply failed to match. The test passed while the property
    was false, which is the worst combination available.

    The property being enforced is **containment**, not the absence of a `..` character:
    `airlock/shadow/2026-09-01/../x.md` resolves back inside and is still allowed. Refusing
    every `..` would be the lazy fix and would reject paths the runner legitimately builds.

    Note the limit of what this can promise. It is a *string* predicate and cannot see the
    filesystem, so it does not resolve symlinks — a link inside an allowed directory
    pointing out of the repo would pass here. Symlink containment is `realpath`'s job at
    write time; this function is the cheap check that runs first.
    """
    if not relpath or not relpath.strip():
        return False
    candidate = relpath.replace(os.sep, "/")
    if candidate.startswith("/") or os.path.isabs(relpath):
        return False

    # Collapse `..` BEFORE any prefix comparison. Doing it after is the bug above.
    normalised = os.path.normpath(candidate).replace(os.sep, "/")
    if normalised == ".." or normalised.startswith("../") or normalised.startswith("/"):
        return False

    for allowed in WRITE_ALLOWLIST:
        if allowed.endswith("/"):
            if normalised.startswith(allowed):
                return True
        elif normalised == allowed:
            return True
    return False


# --- the ledger -------------------------------------------------------------------------


class Caps(NamedTuple):
    """Every ceiling in one place, so a breach cannot be checked in one spot and forgotten
    in another."""
    actions_run: int = MAX_ACTIONS_PER_RUN
    actions_day: int = MAX_ACTIONS_PER_DAY
    bytes_run: int = MAX_BYTES_PER_RUN
    bytes_day: int = MAX_BYTES_PER_DAY
    seconds_run: int = MAX_SECONDS_PER_RUN
    tokens_run: int = MAX_TOKENS_PER_RUN
    tokens_day: int = MAX_TOKENS_PER_DAY

    def raised(self, factor: int = OVERRIDE_MULTIPLIER) -> "Caps":
        return Caps(*[value * factor for value in self])


class DayTotals(NamedTuple):
    """What today's earlier runs already spent, summed from the journal.

    Derived from journal `run` events rather than the status JSON on purpose: the journal is
    durable, synced and event-sourced (D5), so a day cap stays correct even if the
    machine-local status file is lost.
    """
    actions: int = 0
    bytes: int = 0
    tokens: int = 0
    runs: int = 0


class Ledger:
    """Counters plus caps. `breach()` is the only place a cap is compared to a count."""

    def __init__(self, caps: Optional[Caps] = None, prior: Optional[DayTotals] = None,
                 override: bool = False):
        base = caps or Caps()
        self.caps = base.raised() if override else base
        self.override = override
        self.prior = prior or DayTotals()
        self.actions = 0
        self.bytes = 0
        self.tokens = 0
        self.seconds = 0.0
        self.claimed = 0
        self.proposed = 0
        self.refused = 0
        self.executed = 0

    def add_action(self, count: int = 1) -> None:
        self.actions += count

    def add_bytes(self, count: int) -> None:
        self.bytes += count

    def add_tokens(self, count: int) -> None:
        self.tokens += count

    def record(self, outcome: str) -> None:
        """Count one directive's verdict. Actions = executed + proposed + refused."""
        self.add_action()
        if outcome == PROPOSED:
            self.proposed += 1
        elif outcome == REFUSED:
            self.refused += 1
        elif outcome == EXECUTED:
            self.executed += 1

    def breach(self, actions: int = 0, extra_bytes: int = 0) -> Optional[str]:
        """The first cap that `actions` more directives / `extra_bytes` more bytes would
        cross, as the note the journal, status JSON and digest all carry. None means room.

        A cap is breached when the projected count is strictly GREATER than the limit. That
        uniform rule is what lets the token cap be 0 while the runner spends 0: `0 > 0` is
        false, so a zero-spend run never breaches, and the first token ever spent does.
        """
        checks = (
            ("actions", self.actions + actions, self.caps.actions_run),
            ("actions/day", self.prior.actions + self.actions + actions,
             self.caps.actions_day),
            ("bytes", self.bytes + extra_bytes, self.caps.bytes_run),
            ("bytes/day", self.prior.bytes + self.bytes + extra_bytes, self.caps.bytes_day),
            ("seconds", int(self.seconds), self.caps.seconds_run),
            ("tokens", self.tokens, self.caps.tokens_run),
            ("tokens/day", self.prior.tokens + self.tokens, self.caps.tokens_day),
        )
        for name, projected, limit in checks:
            if projected > limit:
                return "%s cap: %d/%d" % (name, projected, limit)
        return None


def day_totals(events: Iterable[Dict[str, Any]], date: str) -> DayTotals:
    """Sum today's `run` events into the day counters the caps are checked against."""
    actions = written = tokens = runs = 0
    for event in run_events(events):
        if (event.get("ts") or "")[:10] != date:
            continue
        runs += 1
        if event.get("actions") is not None:
            actions += _int(event.get("actions"))
        else:
            actions += (_int(event.get("proposed")) + _int(event.get("refused"))
                        + _int(event.get("executed")))
        written += _int(event.get("bytes"))
        tokens += _int(event.get("tokens"))
    return DayTotals(actions=actions, bytes=written, tokens=tokens, runs=runs)


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def run_events(events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [e for e in events if e.get("event") == "run"]


# --- eligibility ------------------------------------------------------------------------


class RequestFile(NamedTuple):
    name: str            # basename, e.g. 2026-09-01_morning_followups.txt
    relpath: str         # repo-relative, e.g. airlock/requests/2026-09-01_...txt
    age_seconds: float   # now - mtime


class Candidate(NamedTuple):
    request: RequestFile
    eligible: bool
    reason: str          # "" when eligible
    stuck: bool          # ineligible for longer than STUCK_SECONDS

    @property
    def request_id(self) -> str:
        return request_id(self.request.name)


def settled(relpath: str, dirty_paths: Sequence[str], age_seconds: float,
            git_available: bool) -> bool:
    """Is this request file finished being written?

    **Primary rule (D-P): a request is settled iff its repo-relative path does not appear in
    `gitutil.status_entries()`** — i.e. it is tracked in HEAD and unmodified. The sync agent
    only auto-commits a tree that has been quiescent for 10 minutes, so a committed request
    file is definitively complete. That reuses a guarantee which already exists and already
    has tests, instead of adding a second heuristic that can disagree with it.

    The mtime rule below is a **fallback**, not a peer: it applies only when the root is not
    a git repo (a fresh clone, the test suite), so there is exactly one answer in production.
    """
    if git_available:
        normalised = relpath.replace(os.sep, "/")
        return normalised not in set(dirty_paths)
    return age_seconds >= SETTLE_SECONDS


def request_id(name: str) -> str:
    return name[:-4] if name.endswith(".txt") else name


def survey(files: Iterable[RequestFile], dirty_paths: Sequence[str],
           git_available: bool) -> List[Candidate]:
    """Classify the queue. Sorted by filename, so dates lead and the oldest goes first.

    Eligibility is per FILE; execution is per LINE. A malformed line costs one directive, not
    a file stuck in the queue forever — so the only unclaimable case here is an invalid
    filename, where there is no request id or date to derive. That file stays in `requests/`,
    is reported every run, and escalates at 24 h. Rare and loud, which is the right shape for
    the one stuck case.
    """
    out = []  # type: List[Candidate]
    for request in sorted(files, key=lambda f: f.name):
        stuck = request.age_seconds >= STUCK_SECONDS
        if not airlock.REQUEST_NAME.match(request.name):
            out.append(Candidate(
                request, False,
                "filename must be <YYYY-MM-DD>_<topic>.txt", stuck))
            continue
        if not settled(request.relpath, dirty_paths, request.age_seconds, git_available):
            if git_available:
                reason = "not yet committed"
            else:
                reason = "changed %s ago — settling for %ds" % (
                    human_age(request.age_seconds), SETTLE_SECONDS)
            out.append(Candidate(request, False, reason, stuck))
            continue
        out.append(Candidate(request, True, "", False))
    return out


def claim_name(name: str, taken: Sequence[str]) -> str:
    """The filename to claim into `done/<date>/`.

    `os.rename` within one filesystem is atomic, so the claim itself cannot half-happen. The
    target should never already exist — the source was unique in `requests/` — but a
    collision must not silently overwrite an earlier day's audit trail, so it suffixes.
    """
    if name not in taken:
        return name
    stem, ext = os.path.splitext(name)
    index = 2
    while "%s-%d%s" % (stem, index, ext) in taken:
        index += 1
    return "%s-%d%s" % (stem, index, ext)


# --- directives -------------------------------------------------------------------------


class Directive(NamedTuple):
    line: int
    raw: str
    verb: str
    args: List[str]


class Verdict(NamedTuple):
    directive: Directive
    outcome: str    # PROPOSED | REFUSED | EXECUTED
    detail: str     # the proposal's relpath, or the refusal reason
    target: str     # repo-relative target, "" when it could not be determined


def parse_directives(text: str) -> List[Directive]:
    """Split a request into directives, keeping the source line number.

    Same tokenisation as `airlock.validate_request` — verb case-insensitive, a trailing colon
    tolerated, `#` comments and blank lines skipped — because a request that validated clean
    must parse identically here or the validator is not checking what runs.
    """
    out = []  # type: List[Directive]
    for number, raw in enumerate(text.split("\n"), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        out.append(Directive(number, line, parts[0].rstrip(":").upper(), parts[1:]))
    return out


def refusal(directive: Directive, root: str) -> Optional[str]:
    """Why this directive must not be acted on, or None.

    Order matters: grammar, then containment, then semantics. Containment is re-checked here
    rather than trusting `bin/airlock.py`'s earlier validation pass, because the payload could
    have been replaced between the two — rail 5's bounded blast radius is cheap to re-assert
    and expensive to assume.
    """
    if directive.verb not in airlock.VERBS:
        return "unknown verb %r — allowed: %s" % (
            directive.raw.split()[0], ", ".join(sorted(airlock.VERBS)))

    expected = airlock.VERBS[directive.verb]
    if len(directive.args) != expected:
        return "%s takes %d argument(s), got %d" % (
            directive.verb, expected, len(directive.args))

    # Deliberately the private helper from lib/airlock rather than a copy. A containment
    # check is a security check, and two copies of one can disagree — the copy being the one
    # that rots. Nothing is mutated there; this is a read of an existing guarantee.
    problems = airlock._check_contained(  # noqa: SLF001
        "", directive.line, directive.args[0], root)
    if problems:
        return problems[0].message

    return _semantic_refusal(directive, root)


def _semantic_refusal(directive: Directive, root: str) -> Optional[str]:
    """The one semantic check shadow mode makes, and it is a string comparison.

    Ingesting an "I could not look" note into Dialexis is never the intent. This matters
    because connector death is the *normal* state at 06:00, so unreachable captures will be
    common — and a proposal queue padded with obviously-wrong entries makes the review the
    🔒 gate depends on worthless.
    """
    target = directive.args[0]
    resolved = target if os.path.isabs(target) else os.path.join(root, target)
    if not resolved.endswith(".md"):
        # A .vtt recording or a spreadsheet export has no frontmatter to read, and that is
        # not a problem — it is payload a request may legitimately reference.
        return None
    try:
        with open(resolved, "r", encoding="utf-8") as handle:
            fields, _body = frontmatter.parse(handle.read())
    except OSError as error:
        return "could not read the target: %s" % error
    except ValueError as error:
        return "capture frontmatter is unparseable: %s" % error

    if fields.get("status") == "unreachable":
        return "capture reports status: unreachable — nothing was sensed"
    return None


def target_facts(directive: Directive, root: str) -> Tuple[str, int, str]:
    """(sha256, bytes, frontmatter text) for a directive's target.

    The sha and the byte count are what let a reviewer answer "should this file have been
    ingested at all?" months later, even if the file has since changed.
    """
    target = directive.args[0]
    resolved = target if os.path.isabs(target) else os.path.join(root, target)
    try:
        with open(resolved, "rb") as handle:
            payload = handle.read()
    except OSError:
        return "", 0, ""
    digest = hashlib.sha256(payload).hexdigest()

    context = ""
    if resolved.endswith(".md"):
        text = payload.decode("utf-8", "replace")
        if text.startswith("---\n"):
            end = text.find("\n---", 4)
            if end != -1:
                context = text[:end + 4]
    return digest, len(payload), context


# --- propose-and-log --------------------------------------------------------------------


def proposal_relpath(date: str, request: str, line: int, verb: str) -> str:
    """One file per DIRECTIVE, not per request, because a reviewer's verdict is per action.

    `NN` is the zero-padded source line number, unique within a request id that was itself
    unique in the queue.
    """
    return "airlock/shadow/%s/%s--%02d--%s.md" % (date, request, line, verb)


# What Phase 4 would actually do, and how much of it comes back. Keyed by verb so adding one
# is a table entry with a reversal story attached — and the story is the forcing function:
# the Drive-staging problem below was found by trying to write this section, not by reasoning
# about it in the abstract. Phase 4 inherits every un-closed reversal note as work.
_PLAN = {
    "INGEST": (
        "Stage `%s` into the Dialexis drop zone resolved by `paths.resolve_inbox()`\n"
        "(a synced-drive inbox folder), then `run.py stage_inbox` → `validate` →\n"
        "`commit`, validate gate ON.",
        "`git -C <knowledge-graph repo> revert <commit>` reverts the commit `commit.py` would\n"
        "create; the archived raw copy is immutable by design and would remain. The\n"
        "Drive-side staged copy is not git-tracked and would need deleting by hand — **this\n"
        "is the part of the plan that is not cleanly reversible, and Phase 4 must close it\n"
        "before enabling INGEST.**",
    ),
    "TRANSCRIPT": (
        "File `%s` into the transcript archive repo: the raw source, and a written\n"
        "summary beside it — the summary being the durable artifact there.",
        "`git -C <transcript archive> revert <commit>` reverts the filing, so the\n"
        "mechanics are revertable. **The durable artifact is a written summary, which needs\n"
        "an LLM — so Phase 4's TRANSCRIPT is not the same shape as Phase 4's INGEST, and\n"
        "this reversal story is incomplete.** Either Phase 4 files only the raw source and\n"
        "leaves the summary to an interactive session, or the unattended path gains an LLM\n"
        "and with it the injection surface D-L exists to avoid. Open question 1 of the\n"
        "Phase 3 design; needs Lukas.",
    ),
}

_WHY_NOT = (
    "Shadow mode (Phase 3). No mutation outside the Lodestar repo, and no LLM anywhere in\n"
    "the unattended path (decisions D-L and D-Q)."
)


def render_proposal(request: str, directive: Directive, target_rel: str,
                    target_sha256: str, target_bytes: int, source_context: str,
                    proposed: str, role: str, runtime: str, run: str,
                    target_abs: Optional[str] = None) -> str:
    """The propose-and-log record, as markdown with flat-scalar frontmatter.

    Deterministic: every input is an argument, so rendering twice produces byte-identical
    output. That is not tidiness — the file is committed, so churn would make two machines
    fight over it, the same contract `render.py` documents.

    **It records the intended action, not a model's output.** With an LLM-free runner there is
    no rendered extraction to include, and that is the right trade: the question shadow mode
    asks a reviewer is "should this file have been ingested at all?", which target + sha256 +
    source frontmatter answers completely. Whether the extraction would have been *good* is a
    Dialexis question with its own validate gate, and conflating the two is how a gate stops
    meaning anything.
    """
    fields = {
        "schema": "1",
        "kind": "proposal",
        "proposed": proposed,
        "machine": role,
        "runtime": runtime,
        "run": run,
        "request": request,
        "line": str(directive.line),
        "verb": directive.verb,
        "target": target_rel,
        "target_sha256": target_sha256,
        "target_bytes": str(target_bytes),
        "status": PROPOSED,
    }
    plan, reversal = _PLAN.get(
        directive.verb, ("Execute `%s`.", "Unknown verb: no reversal story exists."))
    body_lines = [
        "## What would have happened",
        "",
        plan % (target_abs or target_rel) if "%s" in plan else plan,
        "",
        "## Why it was not executed",
        "",
        _WHY_NOT,
        "",
        "## Reversal",
        "",
        reversal,
        "",
        "## Source context",
        "",
    ]
    if source_context:
        body_lines.append("```")
        body_lines.append(source_context.rstrip("\n"))
        body_lines.append("```")
    else:
        body_lines.append("_The target carries no frontmatter (payload, not a capture)._")
    body_lines.append("")
    return frontmatter.dump(fields, "\n".join(body_lines))


STATUS_PROPOSED = PROPOSED
STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"
STATUS_SUPERSEDED = "superseded"
REVIEW_STATUSES = (STATUS_ACCEPTED, STATUS_REJECTED)


def apply_review(text: str, status: str, reviewed: str,
                 note: Optional[str] = None) -> str:
    """Move a proposal's `status:` and stamp `reviewed:`, appending an optional verdict note.

    The file stays where it is — moving it would break the path recorded in the request's
    `.log`. This exists because the 🔒 gate requires reviewed proposals, and hand-editing
    YAML five times is exactly the friction that makes a gate quietly not get met.
    """
    fields, body = frontmatter.parse(text)
    if not fields:
        raise ValueError("not a proposal: no frontmatter block")
    if fields.get("kind") != "proposal":
        raise ValueError("not a proposal: kind is %r" % fields.get("kind"))
    fields = dict(fields)
    fields["status"] = status
    fields["reviewed"] = reviewed
    if note:
        body = body.rstrip("\n") + "\n\n## Verdict\n\n%s\n" % note
    return frontmatter.dump(fields, body)


# --- the request log --------------------------------------------------------------------
#
# Fetcher-shaped and append-only within a run. The invariant a crash test leans on: a `done`
# footer line is present if and only if the request was processed to the end. A log that stops
# after a directive line is the partial state D-N promises — claimed, one action in flight,
# and never a double action.


def log_header(run: str, claimed_at: str, directives: int, mode: str = MODE) -> str:
    return ("run      %s   %s\n"
            "claimed  %s\n"
            "pending  %d directive(s)\n" % (run, mode, claimed_at, directives))


def log_directive(directive: Directive, target: str) -> str:
    return "  %d  %-11s %s\n" % (directive.line, directive.verb, target or "(no target)")


def log_verdict(verdict: Verdict) -> str:
    return "     %-11s %s\n" % (verdict.outcome, verdict.detail)


def log_footer(finished_at: str, ledger_proposed: int, ledger_refused: int,
               executed: int = 0) -> str:
    parts = ["%d proposed" % ledger_proposed, "%d refused" % ledger_refused]
    if executed:
        parts.insert(0, "%d executed" % executed)
    return "done     %s   %s\n" % (finished_at, ", ".join(parts))


def log_is_complete(text: str) -> bool:
    """True when the log carries its `done` footer — i.e. the request finished."""
    return any(line.startswith("done     ") for line in text.split("\n"))


# --- sensing ----------------------------------------------------------------------------


def sensing_summary(groups: Sequence[airlock.GateGroup]) -> Tuple[str, str]:
    """(`"2/3"`, human detail) for the day's sensing verdict.

    The grouping is NOT reimplemented here (D-S). `airlock.gate_groups` owns it, another
    agent owns `bin/airlock.py --gate`, and this consumes the library function and reports
    the verdict. A day whose every window was eventually sensed reads as sensed even though
    one capture honestly says `unreachable` — that is the whole reason the grouping exists.
    """
    if not groups:
        return "0/0", "no captures at all — nothing was even attempted"
    passed = sum(1 for g in groups if g.passed)
    details = []
    for group in groups:
        if group.recovered:
            details.append("%s %s recovered after %d unreachable attempt(s)"
                           % (group.kind, group.window, group.unreachable))
        elif group.passed:
            details.append("%s %s sensed" % (group.kind, group.window))
        else:
            details.append("%s %s NEVER SENSED" % (group.kind, group.window))
    return "%d/%d" % (passed, len(groups)), "; ".join(details)


# --- the digest -------------------------------------------------------------------------


def render_digest(events: Iterable[Dict[str, Any]],
                  proposals: Sequence[Tuple[str, Dict[str, str]]],
                  waiting: Sequence[Candidate] = (),
                  days: int = DIGEST_DAYS) -> str:
    """The rolling digest, re-rendered in full from committed sources only.

    Idempotent, so there is no special "end of day" run and no partial-day state to reason
    about. Determinism matters for the same reason it does in `render.py`: two machines
    rendering from the same journal must produce byte-identical output or the file churns and
    the machines fight over it. So no `now()`, no unsorted iteration, and the window is
    anchored on the **newest journal event**, not on the current time.
    """
    runs = run_events(events)
    dated = {}  # type: Dict[str, List[Dict[str, Any]]]
    for event in runs:
        dated.setdefault((event.get("ts") or "")[:10], []).append(event)
    for day in dated:
        dated[day].sort(key=lambda e: (clock.sort_key(e.get("ts")), e.get("_path", "")))

    by_day_proposals = {}  # type: Dict[str, List[Tuple[str, Dict[str, str]]]]
    for relpath, fields in proposals:
        by_day_proposals.setdefault((fields.get("proposed") or "")[:10], []).append(
            (relpath, fields))

    known = sorted((d for d in set(dated) | set(by_day_proposals) if d), reverse=True)
    # A rolling `days` CALENDAR days ending at the newest event — not the newest `days` days
    # that happen to have data. The distinction is the whole point of bounding it: a job that
    # sat idle for a month must age out of the file, or the digest grows without bound exactly
    # as the plist's own comments warn an unrotated log does.
    window = [d for d in known if d in set(window_dates(known[0], days))] if known else []

    lines = ["# Shadow-run digest", "",
             "<!-- Generated by bin/shadow.py. Re-rendered in full every run from journal "
             "`run` events and proposal frontmatter; never hand-edit. -->", ""]
    if not window:
        lines.append("_No shadow runs recorded yet._")
        return "\n".join(lines) + "\n"

    lines.append("Rolling %d day(s), newest first. Anchored on the newest journal event, not "
                 "on the clock, so this file is byte-stable." % days)
    lines.append("")
    # The render anchor stands in for "last rendered at", which a byte-deterministic file may
    # not carry (a real `now()` would churn it every run). It does the same operational job:
    # a committed digest whose anchor lags the newest journal event means the runner rendered
    # and the SYNC AGENT is wedged — nothing is being committed.
    anchor = max((e.get("ts") or "") for e in runs) if runs else ""
    if anchor:
        lines.append("Newest journal event: **%s**" % anchor)
        lines.append("")
    paused_since = _paused_since(runs)
    if paused_since:
        since, span = paused_since
        lines.append("⏸ paused since %s (%dd) — the PAUSE sentinel was present on every run "
                     "since. Remove it (or revert the commit that added it) to resume."
                     % (since, span))
        lines.append("")

    newest_day = window[0]
    for day in window:
        lines.extend(_digest_day(day, dated.get(day, []),
                                 by_day_proposals.get(day, []),
                                 waiting if day == newest_day else ()))
    return "\n".join(lines).rstrip("\n") + "\n"


def _digest_day(day: str, runs: List[Dict[str, Any]],
                proposals: List[Tuple[str, Dict[str, str]]],
                waiting: Sequence[Candidate]) -> List[str]:
    lines = ["## %s" % day, ""]
    from . import proseguard  # notes now live in the private store (ruling 5)
    notes = proseguard.load_notes() if any(e.get("note_ref") for e in runs) else {}

    def _note(event):
        return proseguard.note_text(event, notes)

    flags = []
    for event in runs:
        if event.get("outcome") == HALTED:
            flags.append("🚩 halted %s — %s" % (_hhmm(event.get("ts")),
                                                _note(event) or "cap breach"))
        elif event.get("outcome") == DEFERRED:
            flags.append("🚩 deferred %s — %s" % (_hhmm(event.get("ts")),
                                                  _note(event) or "unsafe state"))
        elif event.get("outcome") == CRASHED:
            flags.append("🚩 crashed %s — %s" % (_hhmm(event.get("ts")),
                                                 _note(event) or "see the status JSON"))
        if event.get("override"):
            flags.append("🚩 %s ran with --override-caps — this day does not count as "
                         "breach-free for the go-live gate" % _hhmm(event.get("ts")))
    paused = [e for e in runs if e.get("outcome") == PAUSED]
    if paused:
        flags.append("⏸ PAUSE sentinel present — %d run(s) halted before pulling anything"
                     % len(paused))
    for flag in flags:
        lines.append(flag)
    if flags:
        lines.append("")

    if runs:
        summary = " · ".join("%s %s" % (_hhmm(e.get("ts")), e.get("outcome") or "?")
                             for e in runs)
        lines.append("Runs: %d (%s)" % (len(runs), summary))
        # The newest run that actually LOOKED, not simply the newest run. A halted or deferred
        # run never reaches the sensing step, so taking runs[-1] unconditionally would erase
        # the day's sensing verdict — and the sensing line is the one genuinely useful live
        # product of the shadow window.
        sensing_runs = [e for e in runs if e.get("windows_sensed")]
        if sensing_runs:
            newest = sensing_runs[-1]
            lines.append("Sensing: %s windows sensed%s"
                         % (newest.get("windows_sensed"),
                            " (%s)" % newest["sensing"] if newest.get("sensing") else ""))
        tokens = sum(_int(e.get("tokens")) for e in runs)
        lines.append("Tokens: %d%s" % (tokens,
                                       " (shadow mode spends none)" if tokens == 0 else ""))
        written = sum(_int(e.get("bytes")) for e in runs)
        lines.append("Bytes written: %d" % written)
        lines.append("")

        did = [e.get("did") for e in runs if e.get("did")]
        lines.append("### What I did")
        lines.append("")
        if did:
            for entry in did:
                for item in str(entry).split(" ;; "):
                    lines.append("- %s" % item)
        else:
            lines.append("- nothing to consume")
        lines.append("")

    if proposals:
        lines.append("### What I would have done")
        lines.append("")
        lines.append("| Proposal | Verb | Target | Status |")
        lines.append("|----------|------|--------|--------|")
        for relpath, fields in sorted(proposals):
            status = fields.get("status") or "?"
            shown = ("**%s — awaiting review**" % status
                     if status == STATUS_PROPOSED else status)
            lines.append("| `%s` | %s | `%s` | %s |"
                         % (os.path.basename(relpath), fields.get("verb") or "?",
                            os.path.basename(fields.get("target") or "?"), shown))
        lines.append("")

    if waiting:
        lines.append("### Waiting")
        lines.append("")
        for candidate in waiting:
            age = human_age(candidate.request.age_seconds)
            if candidate.stuck:
                # 🚩 at 24 h, because latency stops being latency and becomes a stuck queue.
                lines.append("- 🚩 `%s` — ineligible for %s: %s"
                             % (candidate.request.name, age, candidate.reason))
            else:
                lines.append("- `%s` — %s (%s)"
                             % (candidate.request.name, candidate.reason, age))
        lines.append("")
    return lines


def _paused_since(runs: Sequence[Dict[str, Any]]) -> Optional[Tuple[str, int]]:
    """(first date of the current pause streak, inclusive day span), or None.

    Derived from the journal rather than from the sentinel's mtime, so it stays deterministic
    and so the digest says the same thing on either machine. The streak ends at the newest
    run: a later `ran` means the pause is over and there is nothing to announce.
    """
    ordered = sorted(runs, key=lambda e: (clock.sort_key(e.get("ts")), e.get("_path", "")))
    if not ordered or ordered[-1].get("outcome") != PAUSED:
        return None
    streak = []
    for event in reversed(ordered):
        if event.get("outcome") != PAUSED:
            break
        streak.append((event.get("ts") or "")[:10])
    since, newest = min(streak), max(streak)
    span = (clock.parse(newest + "T00:00:00") - clock.parse(since + "T00:00:00")).days + 1
    return since, span


def _hhmm(timestamp: Optional[str]) -> str:
    return (timestamp or "")[11:16] or "??:??"


def human_age(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    if seconds < 48 * 3600:
        return "%dh" % (seconds // 3600)
    return "%dd" % (seconds // 86400)


def board_line(events: Iterable[Dict[str, Any]]) -> Optional[str]:
    """One line for BOARD.md, anchored on the newest run event's date.

    Anchored rather than clock-based so BOARD.md stays byte-stable when regenerated on
    another machine (decision D-D). `awaiting` rides along on the run event because BOARD.md
    is rendered from `loops/` + `journal/` and must not need to walk `airlock/shadow/`.
    """
    runs = run_events(events)
    if not runs:
        return None
    newest = max(runs, key=lambda e: (clock.sort_key(e.get("ts")), e.get("_path", "")))
    day = (newest.get("ts") or "")[:10]
    today = [e for e in runs if (e.get("ts") or "")[:10] == day]
    parts = ["%d shadow run(s)" % len(today)]
    awaiting = _int(newest.get("awaiting"))
    if awaiting:
        parts.append("%d proposal(s) awaiting review" % awaiting)
    halted = sum(1 for e in today if e.get("outcome") == HALTED)
    if halted:
        parts.append("🚩 %d halted" % halted)
    paused = sum(1 for e in today if e.get("outcome") == PAUSED)
    if paused:
        parts.append("⏸ paused")
    return "Unattended: %s — see `airlock/DIGEST.md`" % " · ".join(parts)


# --- the go-live gate -------------------------------------------------------------------


class Criterion(NamedTuple):
    name: str
    passed: bool
    detail: str


def window_dates(as_of: str, days: int) -> List[str]:
    """The `days` calendar dates ending at `as_of`, oldest first."""
    end = clock.parse(as_of + "T00:00:00")
    return [(end - datetime.timedelta(days=offset)).strftime("%Y-%m-%d")
            for offset in range(days - 1, -1, -1)]


def evaluate_gate(events: Iterable[Dict[str, Any]],
                  proposals: Sequence[Tuple[str, Dict[str, str]]],
                  queued_requests: Sequence[str],
                  days: int = 7,
                  as_of: Optional[str] = None) -> List[Criterion]:
    """The 🔒 gate that authorises Phase 4. Mechanical, following this repo's taste.

    `bin/airlock.py --gate` exists because a judgement-call gate got recorded as passed twice
    on the strength of half the work being done. This one is the same shape: every criterion
    is a count over committed artifacts.

    What it deliberately does NOT require: **successful M365 sensing** (D-S — the connector is
    `cowork-sensing`'s open problem and `bin/airlock.py --gate`'s question; making Phase 4
    hostage to it would mean waiting on a problem it does not depend on), and **a human's
    judgement of the proposals** (criterion 2 checks a verdict exists; rejected proposals do
    not fail the gate, because a week that caught and rejected two bad proposals is a better
    week than one with nothing to reject, and a gate that punished rejection would create
    pressure to accept).

    `days` sets both the window length and the number of distinct days that must carry a run,
    so `--days 1` asks the honest question of a one-day shadow window.
    """
    runs = run_events(events)
    if as_of is None:
        dates = sorted(((e.get("ts") or "")[:10] for e in runs), reverse=True)
        as_of = dates[0] if dates and dates[0] else clock.today()
    window = set(window_dates(as_of, days))

    in_window = [e for e in runs if (e.get("ts") or "")[:10] in window]
    ordered = sorted(in_window,
                     key=lambda e: (clock.sort_key(e.get("ts")), e.get("_path", "")))
    healthy = (RAN, IDLE)

    criteria = []  # type: List[Criterion]

    # 1 · launchd actually fires unattended, which nothing has yet proven for a
    #     StartCalendarInterval job on this machine.
    ran_days = sorted({(e.get("ts") or "")[:10] for e in in_window
                       if e.get("outcome") != CRASHED})
    criteria.append(Criterion(
        "%d distinct day(s) with a non-crashed run" % days,
        len(ran_days) >= days,
        "%d of %d: %s" % (len(ran_days), days, ", ".join(ran_days) or "none")))

    # 2 · Presence is not review, and both verbs, because a week that only ever exercised
    #     TRANSCRIPT says nothing about INGEST.
    in_window_proposals = [(p, f) for p, f in proposals
                           if (f.get("proposed") or "")[:10] in window]
    verbs = {f.get("verb") for _, f in in_window_proposals}
    unreviewed = [p for p, f in in_window_proposals
                  if f.get("status") not in REVIEW_STATUSES]
    missing_verbs = sorted(set(airlock.VERBS) - verbs)
    enough = len(in_window_proposals) >= 5
    criteria.append(Criterion(
        "≥5 proposals, both verbs, every one reviewed",
        enough and not missing_verbs and not unreviewed,
        "%d proposal(s); verbs %s%s%s" % (
            len(in_window_proposals),
            ", ".join(sorted(v for v in verbs if v)) or "none",
            "; MISSING %s" % ", ".join(missing_verbs) if missing_verbs else "",
            "; %d UNREVIEWED" % len(unreviewed) if unreviewed else "")))

    # 3 · D-R, and the criterion that matters most. A valid request is indistinguishable from
    #     an intended one: the instant Phase 4 opens, every request still sitting in the queue
    #     becomes a live irreversible instruction and nobody can tell which were tests.
    criteria.append(Criterion(
        "airlock/requests/ is empty",
        not queued_requests,
        "empty" if not queued_requests
        else "%d request(s) STILL QUEUED: %s — each becomes a live irreversible instruction "
             "the instant Phase 4 opens" % (len(queued_requests),
                                            ", ".join(sorted(queued_requests)))))

    # 4 · Zero cap breaches, and an overridden run does not count as a breach-free day.
    halted = [e for e in in_window if e.get("outcome") == HALTED]
    overridden = [e for e in in_window if e.get("override")]
    criteria.append(Criterion(
        "zero cap breaches and zero --override-caps runs",
        not halted and not overridden,
        "%d halted, %d overridden" % (len(halted), len(overridden))))

    # 5 · A deferral is only acceptable if something later succeeded.
    crashed = [e for e in in_window if e.get("outcome") == CRASHED]
    dangling = []
    for index, event in enumerate(ordered):
        if event.get("outcome") != DEFERRED:
            continue
        if not any(later.get("outcome") in healthy for later in ordered[index + 1:]):
            dangling.append(_hhmm(event.get("ts")))
    criteria.append(Criterion(
        "zero crashed runs, no dangling deferral",
        not crashed and not dangling,
        "%d crashed; %s" % (len(crashed),
                            "deferred with no later success at %s" % ", ".join(dangling)
                            if dangling else "no dangling deferral")))

    # 6 · Rail 4 tested rather than assumed. An untested kill switch is decoration.
    paused_at = [index for index, e in enumerate(ordered) if e.get("outcome") == PAUSED]
    resumed = any(any(later.get("outcome") in healthy for later in ordered[index + 1:])
                  for index in paused_at)
    criteria.append(Criterion(
        "PAUSE was exercised and a later run resumed",
        bool(paused_at) and resumed,
        "%d paused run(s), %s" % (len(paused_at),
                                  "resumed" if resumed else "NEVER resumed in the window")))

    return criteria
