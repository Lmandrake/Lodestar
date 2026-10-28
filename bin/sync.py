#!/usr/bin/env python3
"""Sweep the registered repos: receive (fast-forward), auto-commit quiescent work, push.

    sync.py --dry-run     # report what would happen, change nothing
    sync.py               # do it
    sync.py --quiescence 10
    sync.py --discover    # list repos under ~/dev that the registry does not know
    sync.py --pause [REPO] 8h --reason "big refactor" [--send-only]
    sync.py --resume [REPO]      # REPO is a registry name or a path; default: cwd
    sync.py --paused

Implements the sweep of docs/MULTI_MACHINE_SYNC.md. Its non-negotiables are enforced in
lib/gitutil.py rather than here, so no policy in this file can reach around them: there is
no force option anywhere in the module, and a failed rebase is aborted with the tree
restored rather than resolved.

Order matters. Each guard is a reason to *stop* touching a repo, checked before the
destructive step it protects:

    PAUSE sentinel -> unsafe state -> index.lock -> wrong branch -> repo pause
    -> local status -> unreachable host (deliberate non-participation, never flagged)
    -> fetch (throttled) -> fast-forward -> .lodestarignore -> soft-lock
    -> quiescence -> secret/size guard -> commit (explicit pathspec) -> rebase-if-diverged
    -> push

Each repo has two switches in sync/registry.md — `Send` and `Receive` — and the sweep never
stashes, never merges, and never rebases over uncommitted tracked work.

Conflicts and unsafe states are flagged, never resolved. Silent failure is the enemy, so
everything lands in SYNC.md and ~/dev/.lodestar/sync-status.json.

The fetch is the only step this file rate-limits, and the split is the whole design (see the
tier section below): the LOCAL half runs on every repo on every sweep because `git status` is
free, so nothing slows down how fast finished work is saved and pushed. Only a repo with
*nothing to push* can have its fetch deferred, because then a fetch could discover nothing
but inbound work.
"""

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib import (claudesettings, claudestatusline, clock, completions,  # noqa: E402
                 conversations, duration, enroll, freshness, gitutil, heartbeat, loops,
                 machine, mailreconcile, mirrors, paths, plans, proseguard, purge, pushguard, registry,
                 repopause, selfsync, skillclone, sweeplock, validate)
import claudeconf  # noqa: E402

DEFAULT_QUIESCENCE_MINUTES = 10
SOFT_LOCK_MINUTES = 30


def board_note(counts: Optional[Tuple[int, int]]) -> str:
    """The board's own push health, for the one-line `--quiet` sweep record.

    Only ahead-AND-behind is a flag. Being merely *ahead* is the normal transient state between
    this sweep's commit and its push, and flagging it would train the reader to ignore the line;
    being merely *behind* fast-forwards on its own. Ahead **and** behind is the pathological case:
    it can neither fast-forward nor push, and on 2026-09-30 it persisted for ~15 hours while
    `SYNC.md` showed the board as `skipped` — the quiescence gate returns SKIPPED before divergence
    is ever evaluated, and the board is almost never quiescent because it writes heartbeats.
    """
    if counts is None:
        return "  board: no upstream"
    ahead, behind = counts
    if ahead and behind:
        return ("  🚩 BOARD DIVERGED %d ahead / %d behind — it can neither fast-forward nor push;"
                " the auto-rebase could not resolve it, so a human must" % (ahead, behind))
    if ahead:
        return "  board: %d ahead, pushable" % ahead
    if behind:
        return "  board: %d behind" % behind
    return ""

# --- Fetch tiers ------------------------------------------------------------------------
#
# Why this exists, measured 2026-09-01: the LaunchAgent then fired every 127 seconds and the
# registry had grown to 26 repos — ~680 fetches an hour, ~16,000 a day, all over SSH to one
# host. GitHub throttles that, and its refusal looks exactly like lost auth ("Please make
# sure you have the correct access rights / and the repository exists"). The tell that it was
# throttling rather than auth was that the *set* of failing repos changed run to run:
# `--dry-run` reported 14 flagged, then 3 with a different membership, then 0.
#
# The 127s cadence was chosen at ~16 repos to satisfy "commit and push as you go", so work is
# never lost. But that promise needs *push*, not *fetch*. Fetching only matters in order to
# RECEIVE the other machine's work, and roughly half the registry is `clean · up to date` at
# any moment — a fetch there can only ever discover inbound work. So:
#
#   - Every sweep, every repo, does the local half: `git status` and `ahead/behind` against
#     the already-known remote ref are local and free. Anything dirty or ahead is fetched
#     immediately whatever its tier, because sync.py reconciles before it pushes and skipping the
#     fetch there would either skip the push or push blind.
#   - A clean, non-diverged repo has its fetch deferred to its tier's interval.
#
# Tiers are DERIVED from each repo's own newest commit rather than hand-maintained, so a repo
# promotes itself the moment work starts in it and no registry edit is ever needed.
HOT = "hot"
WARM = "warm"
COLD = "cold"

# Thresholds on the age of the repo's newest commit. 24h means "touched today or last night",
# which is the horizon over which one typically moves between machines; 7d catches a project
# still in flight across a week of interruptions. Anything older is genuinely quiescent, and
# the cost of hearing about its inbound work a few hours late is nil.
HOT_MAX_COMMIT_AGE = 24 * 3600
WARM_MAX_COMMIT_AGE = 7 * 24 * 3600

# Minimum seconds between fetches of a clean, non-diverged repo, by tier.
#
# 15 minutes for hot is the number that matters, and it is set by the cross-machine
# hand-off: it bounds how long after the Laptop pushes that the Desktop notices. Fifteen
# minutes of latency on inbound work you are not currently waiting on is unremarkable, and
# it cuts a hot repo from ~28 fetches an hour to 4. Warm at an hour and cold at six hours
# are the same argument with the stakes falling off.
#
# Written for a 127 s sweep. The sweep is hourly since 2026-09-17, so today hot and warm are
# effectively "every sweep" and only cold still defers; the tiers stay so that a faster
# cadence, if ever reinstated, is throttled from the first run.
FETCH_INTERVAL = {
    HOT: 15 * 60,
    WARM: 60 * 60,
    COLD: 6 * 3600,
}

# Lodestar is the board *and* the airlock: inbound captures and requests land there and are
# read by the other runtime, so its freshness is load-bearing in a way no other repo's is.
# It stays hot through any amount of quiet.
ALWAYS_HOT = frozenset(["Lodestar"])

# Outcomes, most-attention-needed first.
FLAGGED = "flagged"
CONTENDED = "lock-contention"
SKIPPED = "skipped"
PAUSED = "paused"
UNREACHABLE = "unreachable-host"
ABSENT = "absent"
SYNCED = "synced"
CLEAN = "clean"
# 2026-10-22: two TRANSIENT conditions that used to read as 🚩 "needs you" and painted the
# Companion SYNC tile red with healthy repos (9 then 43 of 55 flagged, all clean and level):
# BUSY  = git did not answer in time (a slow machine, ~256 ms per process launch), and
# OFFLINE = the remote host could not be reached (no DNS / VPN flap). Neither needs a human,
# the next sweep re-measures them, and the tile shows them amber.
BUSY = "busy"
OFFLINE = "offline"

# CONTENDED sits second because a repo that met a git lock and recovered is *not* clean, and
# hiding it among the routine rows is how the 2026-09-01 wedge went unnoticed for minutes:
# five commits failed on a stale .git/index.lock and nothing said so. The work succeeded, so
# it is not FLAGGED — but it is a landmine for the next writer and has to be readable at a
# glance. A lock that could not be cleared is FLAGGED like any other "needs you".
# UNREACHABLE sits right after PAUSED and, like it, is deliberate non-participation rather than
# a fault: this machine has DECLARED (in machines.md's Hosts column) that it has no route to
# the repo's remote at all, so never reaching it is expected, not broken. It is grouped with
# ABSENT and PAUSED rather than with FLAGGED for exactly the reason the next paragraph gives
# for ABSENT — a permanent, un-actionable 🚩 trains the reader to stop reading flags.
# ABSENT is deliberately NOT flagged. The registry is one shared file for the whole fleet, so
# a machine holding a subset of the repos would otherwise report a permanent 🚩 per missing
# repo on every sweep, forever — and a wall of unread flags is exactly the failure D13's
# corollary names: "non-blocking is only acceptable if the report is *seen*." One genuine
# conflict would sit in that wall undetected. This is LAPTOP_PARITY §6 defect 1.
ORDER = {FLAGGED: 0, CONTENDED: 1, BUSY: 1, OFFLINE: 1, SKIPPED: 2, PAUSED: 3, UNREACHABLE: 4,
         ABSENT: 5, SYNCED: 6, CLEAN: 7}
ICON = {FLAGGED: "🚩", CONTENDED: "🔓", BUSY: "⏱", OFFLINE: "📡", SKIPPED: "⏭", PAUSED: "⏸",
        UNREACHABLE: "⊘", ABSENT: "○", SYNCED: "✅", CLEAN: "·"}

# Network-class fetch failures. git's LAST stderr line for every ssh failure is the useless
# "and the repository exists." (what the 2026-10-22 15:25 sweep printed for 42 repos), so the
# cause is looked for in the whole stderr. A missing repo or refused key is NOT offline.
OFFLINE_MARKERS = ("could not resolve host", "could not resolve hostname",
                   "network is unreachable", "no route to host", "operation timed out",
                   "connection timed out", "connection refused", "ssh: connect to host",
                   "kex_exchange_identification", "connection reset by peer",
                   "failed to connect to", "temporary failure in name resolution")
NOT_OFFLINE_MARKERS = ("repository not found", "permission denied", "does not appear to be",
                       "authentication failed", "access denied")


def git_failure_cause(err: str) -> str:
    """The informative stderr line of a failed git network command, not git's boilerplate."""
    lines = [l.strip() for l in (err or "").splitlines() if l.strip()]
    boiler = ("please make sure you have the correct access rights", "and the repository exists.",
              "fatal: could not read from remote repository.")
    useful = [l for l in lines if l.lower() not in boiler]
    return (useful or lines or ["no stderr"])[0]


def is_offline_failure(err: str) -> bool:
    low = (err or "").lower()
    return (any(m in low for m in OFFLINE_MARKERS)
            and not any(m in low for m in NOT_OFFLINE_MARKERS))


# --- liveness while a sweep runs, and the watchdog ------------------------------------------
#
# 2026-10-22: one sweep ran 09:48 to 13:39 (launchd ProcessType=Background on a Mac whose E
# cores are saturated: the job got ~no CPU) and published NOTHING for five hours, so the tile
# could only say "silent". Now the sweep writes `sync-progress.json` before each repo, and a
# watchdog bounds the run: after SOFT_DEADLINE_S it stops starting new repos (each unvisited
# repo is reported BUSY "not reached"), and after HARD_DEADLINE_S it records where it was stuck,
# SIGTERMs the git in flight and exits, so launchd's next interval starts a fresh sweep.
SOFT_DEADLINE_S = 40 * 60
HARD_DEADLINE_S = 55 * 60
WATCHDOG_EXIT = 75


def progress_file() -> str:
    return paths.state_file("sync-progress.json")


def write_progress(**fields) -> None:
    """Merge `fields` into the progress record. Never raises: bookkeeping must not kill a sweep."""
    try:
        target = progress_file()
        try:
            with open(target, encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError):
            record = {}
        record.update(fields)
        record["updated_at"] = time.time()
        paths.ensure_dir(os.path.dirname(target))
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, sort_keys=True)
        os.replace(tmp, target)
    except Exception:
        pass


class Watchdog(object):
    """Soft deadline as a flag the sweep loop reads; hard deadline as a recorded exit."""

    def __init__(self, soft_s: float, hard_s: float, exit_fn=None):
        import threading
        self.started = time.time()
        self.soft_s, self.hard_s = soft_s, hard_s
        self._exit = exit_fn or os._exit
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._watch, name="sync-watchdog", daemon=True)

    def start(self) -> "Watchdog":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._done.set()

    def expired(self) -> bool:
        return time.time() - self.started >= self.soft_s

    def _watch(self) -> None:
        if self._done.wait(max(0.0, self.hard_s)):
            return
        where = "?"
        try:
            with open(progress_file(), encoding="utf-8") as handle:
                where = json.load(handle).get("repo") or "?"
        except (OSError, ValueError):
            pass
        how = "no git in flight"
        child = gitutil.CURRENT_CHILD
        if child is not None:
            how = "git in flight stopped with " + gitutil.stop_child(child)
        cause = ("watchdog: sweep still running after %d min, stuck at %s; %s; exiting so "
                 "launchd starts a fresh sweep" % (self.hard_s // 60, where, how))
        write_progress(finished=False, aborted=cause, aborted_at=time.time())
        print("🐕 %s" % cause, file=sys.stderr, flush=True)
        self._exit(WATCHDOG_EXIT)



class Result(NamedTuple):
    name: str
    path: str
    outcome: str
    detail: str
    actions: List[str]
    # Fetch-freshness facts, carried so the report can say them out loud. `remote_age` is
    # seconds since this repo's remote was last contacted — 0.0 when this sweep fetched it,
    # None when that is simply unknown (a repo skipped before the fetch step, or ~/.claude).
    tier: str = ""
    fetched: bool = False
    remote_age: Optional[float] = None


def tier_for(entry, now: Optional[float] = None) -> str:
    """Which fetch tier this repo is in, derived from its own newest commit.

    Derived, never declared: `git log -1 --format=%ct` is local and free, and it means a repo
    promotes itself to hot the moment someone commits in it — no registry column to keep
    current, and no way for the tiering to be quietly wrong because nobody edited a table.

    An unreadable age (missing directory, no commits, not a repo) reads as HOT. Unknown has
    to fail towards fetching: skipping a fetch on a guess is how inbound work goes unnoticed.
    """
    if entry.name in ALWAYS_HOT:
        return HOT
    stamp = gitutil.last_commit_time(entry.path)
    if stamp is None:
        return HOT
    age = (time.time() if now is None else now) - stamp
    if age <= HOT_MAX_COMMIT_AGE:
        return HOT
    if age <= WARM_MAX_COMMIT_AGE:
        return WARM
    return COLD


def fetch_due(tier: str, last_fetch: Optional[float], now: Optional[float] = None) -> bool:
    """True when a clean, non-diverged repo in `tier` is owed a fetch.

    `last_fetch is None` — never fetched, or the state file was deleted — is always due, so
    a missing cache degrades to the old behaviour rather than to silence.
    """
    if last_fetch is None:
        return True
    interval = FETCH_INTERVAL.get(tier, FETCH_INTERVAL[HOT])
    return ((time.time() if now is None else now) - last_fetch) >= interval


def load_fetch_state(path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Read `fetch-state.json`. Anything missing or malformed reads as empty.

    Pure cache, so every failure mode has the same safe answer: an empty mapping, which makes
    every repo due and the sweep behaves exactly as it did before this file existed.
    """
    target = path or paths.fetch_state_file()
    try:
        with open(target, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    repos = payload.get("repos")
    if not isinstance(repos, dict):
        return {}
    return {name: row for name, row in repos.items() if isinstance(row, dict)}


def save_fetch_state(state: Dict[str, Dict[str, Any]], path: Optional[str] = None) -> str:
    """Write `fetch-state.json` atomically. Never called on a dry run."""
    target = path or paths.fetch_state_file()
    paths.ensure_dir(os.path.dirname(target))
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"repos": state}, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, target)
    return target


def last_fetch_of(state: Dict[str, Dict[str, Any]], name: str) -> Optional[float]:
    row = state.get(name) or {}
    stamp = row.get("last_fetch")
    if isinstance(stamp, (int, float)):
        return float(stamp)
    return None


def _span(seconds: Optional[float]) -> str:
    """Compact human duration. Sub-minute says so rather than rendering a misleading "0m"."""
    if seconds is None:
        return "an unknown time"
    if seconds < 60:
        return "under a minute"
    return duration.format_minutes(int(seconds // 60))


def remote_note(tier: str, fetched: bool, remote_age: Optional[float]) -> str:
    """The freshness half of a repo's row, kept separate from what the sweep *did*.

    This is the wording that decision D13 forces. `clean` used to render as `up to date`,
    which invited the reading "checked just now" — and once fetches are throttled that would
    be a quiet lie for up to six hours. So freshness is always stated in its own words:
    *verified* means this sweep actually reached the remote; *not checked for N* means it
    did not, and says how stale the answer is.
    """
    if not tier:
        return "—"
    if fetched:
        return "%s · verified just now" % tier
    if remote_age is None:
        return "%s · not checked" % tier
    return "%s · not checked for %s" % (tier, _span(remote_age))


def soft_lock_owner(repo_path: str, our_role: str,
                    window_minutes: int = SOFT_LOCK_MINUTES) -> Optional[str]:
    """Return the other machine's role if it recently claimed a loop bound to this repo.

    The cheapest conflict is the one that never happens. Lodestar already records which
    machine has a loop attached, so the sweep treats that as an advisory lock: pull, but do
    not auto-commit work another machine is actively holding. Advisory, not enforced — it
    encodes "work a repo on one machine at a time", which is how a single owner typically works.
    """
    target = os.path.realpath(repo_path)
    for _, (fields, _) in sorted(loops.all_loops().items()):
        bound = fields.get("repo", "").strip()
        if not bound:
            continue
        if os.path.realpath(os.path.expanduser(bound)) != target:
            continue
        if fields.get("state") != "open":
            continue

        touch = fields.get("last_touch", "").strip()
        if not touch:
            continue
        parts = touch.split()
        if len(parts) < 2:
            continue
        stamp, owner = parts[0], parts[1]
        role = owner.split("/")[0]
        if role == our_role:
            continue
        try:
            age = clock.parse(clock.now()) - clock.parse(stamp)
        except ValueError:
            continue
        if age.total_seconds() <= window_minutes * 60:
            return owner
    return None


def _name_some(paths: List[str], limit: int = 3) -> str:
    """Name a few paths and count the rest. Used in commit messages and in the report."""
    shown = ", ".join(paths[:limit])
    if len(paths) > limit:
        shown += " and %d more" % (len(paths) - limit)
    return shown


def _note_lock(result, notes: List[str]) -> None:
    """Carry a git operation's lock story into the repo's report line.

    Set whether the operation succeeded or not, so "it worked, on the third try" is as
    visible as "it never worked". The first is the case that used to vanish.
    """
    if getattr(result, "lock_note", ""):
        notes.append(result.lock_note)


def _failure(label: str, result) -> str:
    """One-line detail for a failed git step, naming a lock explicitly when that was it."""
    if getattr(result, "lock_blocked", False):
        return "%s blocked: %s" % (label, result.lock_note)
    combined = (result.out + result.err).strip()
    return "%s failed: %s" % (label, combined.split("\n")[-1] if combined else "no output")


def lock_step(repo: str, dry_run: bool) -> Tuple[Optional[str], List[str]]:
    """Look at every lock in `gitutil.GUARDED_LOCKS` before touching anything.

    Returns (fatal, notes). Checked up front, and *whether or not there is anything to
    commit*, because the wedge this guards against is silent by nature: a stale lock in an
    otherwise clean repo reports `clean` while quietly blocking the next human commit too. A
    `fatal` string means do not touch this repo at all.

    Both locks are checked because they wedge different steps. `git add` needs no ref lock, so
    a stale `HEAD.lock` lets staging succeed and then fails the commit — which is how one repo
    accumulated staged work for ten days with nothing reporting a problem. Checking only
    `index.lock` cannot see that.

    A lock younger than the staleness threshold is ignored entirely — git holding its own
    lock for a moment is normal, and flagging that would make the report noise. Those are
    handled by the retry-with-backoff inside gitutil instead.
    """
    notes = []  # type: List[str]

    for name in gitutil.GUARDED_LOCKS:
        verdict, why = gitutil.lock_verdict(repo, name=name)

        if verdict in (gitutil.LOCK_ABSENT, gitutil.LOCK_FRESH):
            continue

        if verdict == gitutil.LOCK_STALE:
            if dry_run:
                notes.append("would remove a stale .git/%s (%s)" % (name, why))
                # Left in place, so every mutating step below would fail on it. Reporting is
                # the whole job of a dry run; acting is not.
                continue
            removed, note = gitutil.remove_stale_lock(repo, name=name)
            if removed:
                notes.append(note)
                continue
            return ("stale .git/%s could not be removed: %s; needs you" % (name, note)), notes

        # LOCK_HELD: old enough to be suspicious, but something says it is in use. Removing it
        # could corrupt the repo, so the sweep keeps its hands off and asks for a human.
        return ("%s — not touched; needs you" % why), notes

    return None, notes


def _commit_message(our_role: str, sweepable: List[str], limit: int = 40) -> str:
    """Subject names a few paths; the body lists them all (up to `limit`).

    A bare timestamp leaves a reverter nothing to judge, which is exactly the complaint in
    lib/reversal.py; the full list is what lets a sweep commit be audited without a diff.
    """
    subject = "chore(sync): %s %s — %s" % (our_role, clock.now(), _name_some(sweepable))
    body = ["Auto-committed by the Lodestar sweep (bin/sync.py), not by a person.", ""]
    body.extend("- %s" % path for path in sweepable[:limit])
    if len(sweepable) > limit:
        body.append("- … and %d more" % (len(sweepable) - limit))
    return subject + "\n\n" + "\n".join(body)


def sweep_one(entry, our_role: str, quiescence_minutes: int, dry_run: bool,
              fetch_state: Optional[Dict[str, Dict[str, Any]]] = None,
              now: Optional[float] = None) -> Result:
    """Sweep one repo. `fetch_state` is read for throttling and updated in place.

    Passing no `fetch_state` means "no cache", which makes every repo due — the pre-throttle
    behaviour, and the safe direction for any caller that does not care.

    Two switches decide what may happen (registry `Send` / `Receive`, narrowed further by a
    per-repo pause):

    - **receive** moves HEAD forward only by `merge --ff-only`, and only with no tracked file
      dirty. Behind with tracked edits is flagged, never stashed.
    - **send** commits quiescent, non-source, non-ignored paths with an explicit pathspec
      under the sweep's own author, and pushes.
    - **diverged** is rebased (no autostash) only with send on and a clean tracked tree —
      rebasing rewrites local commits, which send=off forbids. Otherwise flagged.
    """
    actions = []  # type: List[str]
    lock_notes = []  # type: List[str]
    notes = []  # type: List[str]    # deliberate non-actions, said out loud
    loud = []  # type: List[str]     # things that turn an otherwise-quiet row into a flag
    repo = entry.path
    if fetch_state is None:
        fetch_state = {}
    if now is None:
        now = time.time()

    tier = tier_for(entry, now)
    fetched = False
    remote_age = None  # type: Optional[float]

    def finish(outcome: str, detail: str, extra: Optional[List[str]] = None) -> Result:
        """Every exit carries the freshness facts as they stand at that point."""
        if loud and outcome != FLAGGED:
            outcome = FLAGGED
            detail = "; ".join(loud + ([detail] if detail else []))
        return Result(entry.name, repo, outcome, detail, actions + notes + (extra or []),
                      tier, fetched, remote_age)

    state = gitutil.state(repo)
    if state == gitutil.MISSING:
        # Absent by machine, not broken. `state()` returns MISSING for a path that is not a
        # directory and MISSING is in UNSAFE_STATES, so this used to flag — see ABSENT above.
        return finish(ABSENT, "not cloned on this machine; not touched")
    if state == gitutil.TIMED_OUT:
        return finish(BUSY, "git did not answer in time (machine busy); not touched, "
                            "re-checked next sweep")
    if state in gitutil.UNSAFE_STATES:
        return finish(FLAGGED, "%s — needs you; not touched" % state)

    fatal, lock_notes = lock_step(repo, dry_run)
    if fatal:
        return finish(FLAGGED, fatal)

    if proseguard.applies(repo) and not dry_run:
        hooked = proseguard.install_hook(repo)
        if hooked:
            (loud if "NOT installed" in hooked else notes).append(hooked)

    branch = gitutil.current_branch(repo)
    if entry.branch and branch != entry.branch:
        return finish(SKIPPED, "on %r, registered for %r" % (branch, entry.branch))

    if not gitutil.has_upstream(repo):
        return finish(SKIPPED, "no upstream branch")

    pause = repopause.evaluate(repo, now=now, dry_run=dry_run)
    if pause.loud:
        loud.append(pause.detail)
    elif pause.detail and not pause.directions:
        notes.append(pause.detail)
    send_on = bool(getattr(entry, "send", True)) and "send" not in pause.directions
    receive_on = bool(getattr(entry, "receive", True)) and "receive" not in pause.directions
    why_no_send = ("paused" if "send" in pause.directions else "send off")
    why_no_receive = ("paused" if "receive" in pause.directions else "receive off")

    # --- the local half: free, and therefore unconditional --------------------------------
    #
    # `git status` and a rev-list against the already-fetched remote ref touch no network at
    # all. Doing them BEFORE the fetch is what lets the fetch be throttled without ever
    # delaying a push: if there is anything to save or send, the fetch happens regardless of
    # tier, because a push has to know what it is pushing onto.
    dirty = gitutil.is_dirty(repo)
    counts = gitutil.ahead_behind(repo)
    ahead, behind = counts if counts else (0, 0)

    # --- unreachable host: deliberate non-participation, checked before any network reach ---
    #
    # Declared per machine in machines.md's Hosts column (lib/machine.py), never probed: a
    # probe costs a network round trip on every sweep for a fact that changes maybe once a
    # year, and a machine may have no network path to the private host at all — not a
    # permissions problem, no connectivity whatsoever. Every repo on that host used to flag
    # every sweep forever, with no backoff and no give-up, because unreachability was never a
    # fact the sweep could look up. Checked here so such a repo is never fetched or pushed,
    # and reports as neither clean nor flagged.
    declared_remote = entry.remote or skillclone.observe_remote(repo) or ""
    # The DECLARED remote wins over the checkout's observed origin: the declaration is intent
    # (what this repo is supposed to point at), while the observation is only evidence of
    # what a checkout happens to have configured right now, which can lag a rehome.
    host = skillclone.remote_host(declared_remote)
    if host and not machine.host_reachable(host, machine.key()):
        return finish(UNREACHABLE,
                      "this machine declares no route to %s; not fetched, not pushed" % host)

    last_fetch = last_fetch_of(fetch_state, entry.name)
    must_fetch = bool(dirty or ahead or behind)
    if must_fetch or fetch_due(tier, last_fetch, now):
        fetch_result = gitutil.fetch(repo)
        code, _, err = fetch_result
        if code != 0:
            remote_age = None if last_fetch is None else max(0.0, now - last_fetch)
            if gitutil.timed_out(fetch_result):
                return finish(BUSY, "fetch timed out (machine or network slow); not touched, "
                                    "re-checked next sweep")
            if is_offline_failure(err):
                return finish(OFFLINE, "remote unreachable (%s); not touched, re-checked next "
                                       "sweep" % git_failure_cause(err))
            return finish(FLAGGED, "fetch failed: %s" % git_failure_cause(err))
        fetched = True
        remote_age = 0.0
        if not dry_run:
            fetch_state[entry.name] = {"last_fetch": now, "tier": tier}
        actions.append("fetched")
        counts = gitutil.ahead_behind(repo)
        ahead, behind = counts if counts else (ahead, behind)
    else:
        remote_age = None if last_fetch is None else max(0.0, now - last_fetch)
        actions.append("fetch skipped (%s tier, remote not checked for %s)"
                       % (tier, _span(remote_age)))

    # 🔴 Push guard (owner card 2026-10-28 08:25): origin must be the repo the registry DECLARES,
    # by identity (`skillclone.same_remote`). A renamed repo's old name can now be a different,
    # PUBLIC repository, so a machine that has not converged the rehome must not push there.
    # Loud whether or not there is anything to push, so the drift is seen before it matters.
    push_refusal = pushguard.refusal(repo, entry.remote)
    if push_refusal:
        loud.append(push_refusal)

    if ahead and behind:
        # Before ANY reconcile, and even when paused: a checkout of history the upstream has
        # since rewritten must never be rebased onto it (that re-publishes the scrubbed text).
        rewritten = gitutil.upstream_rewrite(repo)
        if rewritten:
            return finish(FLAGGED, rewritten + " — nothing was changed; needs you")

    if pause.directions and not send_on and not receive_on:
        # Fully paused: fetched (harmless), and nothing else. Reported, never silent.
        state_bits = []
        if ahead:
            state_bits.append("%d ahead" % ahead)
        if behind:
            state_bits.append("%d behind" % behind)
        if dirty:
            state_bits.append("local changes")
        return finish(PAUSED, "%s%s" % (pause.detail,
                                        " (%s)" % ", ".join(state_bits) if state_bits else ""))
    if pause.directions:
        notes.append(pause.detail)

    def receive_fast_forward(count: int) -> Optional[Result]:
        """ff-only; returns a Result only when it had to stop."""
        if dry_run:
            actions.append("would fast-forward %d" % count)
            return None
        step = gitutil.fast_forward(repo)
        _note_lock(step, lock_notes)
        if step.code != 0:
            if step.lock_blocked:
                return finish(FLAGGED, "fast-forward blocked: %s" % step.lock_note)
            return finish(FLAGGED, _failure("fast-forward", step) + " — nothing was changed; "
                                                                   "needs you")
        actions.append("pulled %d" % count)
        return None

    # --- receive, the easy case: behind only, nothing tracked in the way -------------------
    if behind and not ahead and receive_on and not gitutil.tracked_dirty(repo):
        stop = receive_fast_forward(behind)
        if stop:
            return stop
        behind = 0  # a dry run pretends it happened, so it is not reported twice

    # --- send: commit quiescent, sweepable work ---------------------------------------------
    if proseguard.applies(repo) and not dry_run:
        # Leftover changes to tracked records whose writers now use the private store (older code
        # mid-upgrade, a consult transcript): preserved there, file restored to HEAD, so a
        # dirty tracked file can never freeze this checkout's pulls.
        actions.extend(proseguard.fix(repo, [p for p in gitutil.changed_paths(repo)
                                             if proseguard._restorable(p)]))
    candidates, ignored = gitutil.split_ignored(repo, gitutil.changed_paths(repo))
    withheld = ("%d path(s) withheld by .lodestarignore/built-ins" % len(ignored)
                if ignored else "")
    committed = []  # type: List[str]
    if candidates and not send_on:
        notes.append("%d local change(s) left uncommitted (%s)" % (len(candidates),
                                                                    why_no_send))
    elif candidates:
        # Why the send phase might stand down this pass. Both reasons used to
        # `return finish(SKIPPED, ...)` right here, which returned BEFORE the reconcile block
        # below — so a repo that is never quiescent never had its divergence evaluated at all.
        # The board is exactly that repo: it writes a heartbeat every sweep, so it is almost
        # never quiet. Measured 2026-09-30, it sat ~15 hours diverged while `SYNC.md` reported
        # it as `skipped`. Standing down from COMMITTING is not a reason to stand down from
        # RECONCILING, and the rebase below has its own independent guard against an unclean
        # tracked tree, so reaching it with uncommitted work is already handled.
        defer_send = None
        owner = soft_lock_owner(repo, our_role)
        if owner:
            defer_send = ("soft-lock: %s holds an open loop on this repo; not committed"
                          % owner)
        elif not gitutil.quiescent(repo, quiescence_minutes * 60, candidates):
            defer_send = ("changed within the last %dm — waiting for it to settle"
                          % quiescence_minutes)

        if defer_send and not (ahead and behind):
            return finish(SKIPPED, defer_send)
        if defer_send:
            notes.append(defer_send + ", but %d ahead / %d behind is reconciled below"
                         % (ahead, behind))

        if not defer_send:
            problems = gitutil.offending_paths(repo, candidates=candidates)
            if problems:
                detail = "; ".join("%s (%s)" % (path, why) for path, why in problems[:3])
                if len(problems) > 3:
                    detail += " and %d more" % (len(problems) - 3)
                return finish(FLAGGED, "refused to auto-add: %s" % detail)

            # The sweep owns board state; source needs an authored commit. Measured 2026-09-15:
            # 8 of 304 chore(sync) commits had swallowed source, and per lib/reversal.py reverting
            # such a bundled sha reverts someone else's work — so this is a correctness guard, not
            # tidiness. The other 296 were board state and are unaffected.
            source, sweepable = gitutil.partition_paths(candidates)
            # 🔴 Board prose never reaches github.com (ruling 5, 2026-10-13). Loop prose and
            # journal notes are moved to the private store (or a local stash where there is none),
            # and whatever is still unsafe is WITHHELD and flagged — never committed, never
            # deleted. See lib/proseguard.py.
            if proseguard.applies(repo) and sweepable:
                if not dry_run:
                    actions.extend(proseguard.fix(repo, sweepable))
                blocked = proseguard.violations(repo, sweepable)
                if blocked:
                    held = set(path for path, _ in blocked)
                    sweepable = [path for path in sweepable if path not in held]
                    loud.append("%d path(s) WITHHELD by the private-text guard: %s"
                                % (len(blocked), "; ".join("%s (%s)" % b for b in blocked[:3])
                                   + (" and %d more" % (len(blocked) - 3)
                                      if len(blocked) > 3 else "")))
            if source:
                # Reported, never silent: withholding a file without saying so would trade a
                # swallowed commit for a lost one.
                actions.append("%d source file(s) uncommitted — NOT auto-committed: %s"
                               % (len(source), _name_some(source)))

            if sweepable:
                count = len(sweepable)
                if dry_run:
                    actions.append("would commit %d file(s): %s" % (count, _name_some(sweepable)))
                    ahead += 1
                    committed = list(sweepable)
                else:
                    step = gitutil.add_paths(repo, sweepable)
                    _note_lock(step, lock_notes)
                    if step.code != 0:
                        return finish(FLAGGED, _failure("git add", step))
                    # Explicit pathspec: only these paths, whatever else a peer has staged since
                    # the state check. The pathspec is the guarantee; the DIRTY_INDEX skip above
                    # only narrows the window.
                    step = gitutil.commit(repo, _commit_message(our_role, sweepable),
                                          paths=sweepable, author=gitutil.SWEEP_AUTHOR)
                    _note_lock(step, lock_notes)
                    if step.code != 0:
                        combined = (step.out + step.err)
                        if "nothing to commit" in combined or "no changes added" in combined:
                            pass
                        else:
                            return finish(FLAGGED, _failure("commit", step))
                    else:
                        actions.append("committed %d file(s)" % count)
                        committed = list(sweepable)
                        counts = gitutil.ahead_behind(repo)
                        ahead = counts[0] if counts else ahead + 1

        # --- reconcile with the remote -----------------------------------------------------------
    if behind:
        # On a dry run the commit is imaginary, so the tree is judged by what it would leave.
        pending = set(committed) if dry_run else set()
        tracked = any(path not in pending for path in gitutil.tracked_dirty_paths(repo))
        if not receive_on:
            if ahead:
                return finish(FLAGGED, "diverged: %d ahead, %d behind, and %s — not rebased; "
                                       "needs you" % (ahead, behind, why_no_receive))
            notes.append("%d behind — not pulled (%s)" % (behind, why_no_receive))
        elif ahead:
            if not send_on:
                return finish(FLAGGED, "diverged: %d ahead, %d behind — %s, so local commits "
                                       "are not rebased; needs you"
                              % (ahead, behind, why_no_send))
            if tracked:
                return finish(FLAGGED, "diverged: %d ahead, %d behind, with uncommitted "
                                       "tracked changes — not rebased; needs you"
                              % (ahead, behind))
            if dry_run:
                actions.append("would rebase %d local commit(s) onto %d incoming"
                               % (ahead, behind))
            else:
                step = gitutil.rebase_onto_upstream(repo)
                _note_lock(step, lock_notes)
                resolved = 0
                if step.code != 0 and not step.lock_blocked:
                    step, resolved, why = resolve_remotes_rebase(repo, step)
                if step.code != 0:
                    gitutil.rebase_abort(repo)
                    if step.lock_blocked:
                        return finish(FLAGGED, "rebase blocked: %s" % step.lock_note)
                    return finish(FLAGGED, "conflict on rebase (%s) — aborted and left as it "
                                           "was; a human must resolve it — needs you" % why)
                actions.append("rebased %d onto %d incoming" % (ahead, behind))
                if resolved:
                    actions.append("auto-resolved %d %s conflict(s), preferring SSH"
                                   % (resolved, skillclone.REMOTES_PATH))
                behind = 0
                counts = gitutil.ahead_behind(repo)
                ahead = counts[0] if counts else ahead
        else:
            if tracked:
                return finish(FLAGGED, "%d behind with uncommitted tracked changes — not "
                                       "pulled (no autostash); needs you" % behind)
            stop = receive_fast_forward(behind)
            if stop:
                return stop
            behind = 0

    if ahead:
        if not send_on:
            notes.append("%d local commit(s) not pushed (%s)" % (ahead, why_no_send))
        elif behind and dry_run:
            pass  # reported above
        elif push_refusal:
            return finish(FLAGGED, "%s (%d local commit(s) held)" % (push_refusal, ahead))
        elif dry_run:
            actions.append("would push %d commit(s)" % ahead)
        elif proseguard.applies(repo) and _outgoing_refusal(repo):
            return finish(FLAGGED, _outgoing_refusal(repo))
        else:
            step = gitutil.push(repo)
            _note_lock(step, lock_notes)
            if step.code != 0:
                return finish(FLAGGED, _failure("push", step))
            actions.append("pushed %d" % ahead)

    if lock_notes:
        # Recovered, but never reported as clean or as a routine sync: the lock is the story.
        done = ", ".join(actions[1:])
        detail = "; ".join(lock_notes)
        return finish(CONTENDED, "%s%s" % (detail, " (%s)" % done if done else ""),
                      lock_notes)

    # actions[0] is always the fetch line — "fetched" or "fetch skipped (…)" — so a repo that
    # did nothing else has exactly one action.
    did = actions[1:]
    if withheld:
        notes.append(withheld)
    if not did and notes and notes != [withheld]:
        # Deliberately left alone (send/receive off, or paused one way): a non-action worth
        # reading, so never dressed up as clean or synced.
        return finish(SKIPPED, "; ".join(notes))
    if not did and not candidates:
        detail = clean_detail(fetched, tier, remote_age)
        return finish(CLEAN, detail + ("; " + withheld if withheld else ""))
    return finish(SYNCED, ", ".join(did + notes) or "nothing to do")


def _outgoing_refusal(repo: str) -> Optional[str]:
    """Why Lodestar's outgoing commits may not be pushed, or None. The last gate before
    github.com: it catches a commit made with --no-verify, in a hook-less clone, or by any
    tool, and judges commit messages too (ruling 5, 2026-10-13)."""
    try:
        found = proseguard.outgoing_violations(repo)
    except proseguard.GuardError as error:
        return "NOT pushed: the private-text guard could not check the outgoing commits (%s)" % error
    if not found:
        return None
    return ("NOT pushed: %d outgoing item(s) fail the private-text guard — %s; rewrite those local "
            "commits (move the text to the private store) — needs you"
            % (len(found), "; ".join("%s (%s)" % f for f in found[:3])))


REMOTES_REBASE_MAX_STEPS = 50


def resolve_remotes_rebase(repo: str, step) -> Tuple[Any, int, str]:
    """Carry a stopped rebase through when its ONLY conflicts are in `sync/remotes.md`.

    Owner ruling 2026-10-09 by question card: "Auto-rebase, prefer SSH". Two machines holding
    one repo as `git@…` and `https://…` write different rows (see CLAUDE.md), and that one
    file used to stop the board pushing for hours. Each conflicting row resolves to the SSH
    form. Any other conflicted path, or a row this rule cannot decide, returns the failing
    step with a reason; the caller then aborts — never a reset, stash or whole-tree checkout.

    Returns (last step, conflicts resolved, reason when it gave up).
    """
    resolved = 0
    for _ in range(REMOTES_REBASE_MAX_STEPS):
        if step.code == 0:
            return step, resolved, ""
        if not gitutil.rebase_in_progress(repo):
            return step, resolved, "rebase failed: %s" % (step.err or step.out).strip()[-200:]
        conflicts = gitutil.conflicted_paths(repo)
        if conflicts != [skillclone.REMOTES_PATH]:
            return step, resolved, ("conflicts in %s" % _name_some(conflicts)
                                    if conflicts else "rebase stopped without a conflict")
        target = os.path.join(repo, skillclone.REMOTES_PATH)
        with open(target, encoding="utf-8") as handle:
            text, why = skillclone.resolve_remotes_conflict(handle.read())
        if text is None:
            return step, resolved, "%s: %s" % (skillclone.REMOTES_PATH, why)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(text)
        added = gitutil.run(repo, "add", "--", skillclone.REMOTES_PATH)
        if added.code != 0:
            return added, resolved, "git add failed: %s" % added.err.strip()
        resolved += 1
        step = gitutil.rebase_continue(repo)
    return step, resolved, "gave up after %d rebase steps" % REMOTES_REBASE_MAX_STEPS


def clean_detail(fetched: bool, tier: str, remote_age: Optional[float]) -> str:
    """What `clean` says now — and what it deliberately stops implying.

    It used to read `up to date`, which conflated two different facts: "this tree has no
    local changes" (always freshly measured, because `git status` is free) and "the remote
    holds nothing new" (only true if this sweep actually fetched). Once fetches are throttled
    the second can be six hours old, so `up to date` would be exactly the quiet lie D13
    forbids. The two halves are now stated separately, and the second names its own age.
    """
    if fetched:
        return "no local changes; verified against the remote just now"
    return ("no local changes; remote not checked for %s (%s tier, next check due in %s)"
            % (_span(remote_age), tier,
               _span(max(0.0, FETCH_INTERVAL.get(tier, FETCH_INTERVAL[HOT])
                         - (remote_age or 0.0)))))


def sweep(entries, our_role: str, quiescence_minutes: int, dry_run: bool,
          fetch_state: Optional[Dict[str, Dict[str, Any]]] = None,
          now: Optional[float] = None, watchdog: Optional[Watchdog] = None) -> List[Result]:
    results = []
    total = len(entries)
    for index, entry in enumerate(entries):
        if watchdog is not None and watchdog.expired():
            # Past the soft deadline: say which repos were not reached rather than run on
            # into the next interval. BUSY, not FLAGGED: nothing is wrong with them.
            results.append(Result(entry.name, entry.path, BUSY,
                                  "not reached: sweep hit its %d-min soft deadline"
                                  % (watchdog.soft_s // 60), []))
            continue
        write_progress(repo=entry.name, index=index + 1, total=total, repo_started_at=time.time())
        try:
            results.append(sweep_one(entry, our_role, quiescence_minutes, dry_run,
                                     fetch_state, now))
        except Exception as error:  # a broken repo must not stop the sweep
            results.append(Result(entry.name, entry.path, FLAGGED,
                                  "unexpected error: %s" % error, []))

    # Re-measure transient rows once, at the end, while time allows: a repo that was BUSY or
    # OFFLINE a few minutes ago and is clean and level now is reported as what it is now. This
    # is the auto-clear for stale "needs you": the sweep re-checks, nobody edits state by hand.
    by_name = {entry.name: entry for entry in entries}
    for position, result in enumerate(results):
        if result.outcome not in (BUSY, OFFLINE) or result.name not in by_name:
            continue
        if watchdog is not None and watchdog.expired():
            break
        write_progress(repo=result.name, retry=True, repo_started_at=time.time())
        try:
            again = sweep_one(by_name[result.name], our_role, quiescence_minutes, dry_run,
                              fetch_state, time.time())
        except Exception:
            continue
        results[position] = again._replace(
            actions=list(again.actions) + ["re-checked after a %s first pass" % result.outcome])

    # Record each repo's real origin so a machine missing one can clone it from truth rather
    # than a guessed URL pattern. Written by whichever machine HAS the repo; a machine that
    # lacks it leaves the recorded value alone. Shared and fleet-wide, because a remote is the
    # same everywhere — it changes only when a remote does, so it does not churn.
    if not dry_run:
        try:
            mapping, changed = skillclone.update_remotes(entries)
            if changed:
                with open(skillclone.remotes_file(), "w", encoding="utf-8") as handle:
                    handle.write(skillclone.render_remotes(mapping))
        except Exception:
            pass  # a cosmetic map must never cost a sweep
    return results


def lifecycle_step(dry_run: bool) -> Optional[Result]:
    """Converge approved lifecycle plans on this machine. None when there are none.

    This is the unattended half of `bin/lifecycle.py`, and it exists so the converger has a
    caller that runs without anyone present — which is the entire reason §3C puts the
    authorization on the originating machine. Lukas's ruling: *"All other machines simply obey
    and do not request permission at all."* tower is evictable precisely because this runs
    here with nobody at tower.

    **It never prompts and it never asks.** A plan that is not approved is skipped with a
    reason; a plan that is approved is executed.

    🔴 **The freshness gate is ENFORCING here, unlike the sweep's own.** The gate computed
    later in `main` is deliberately report-only (see `report()`), because a stale board should
    not stop ordinary syncing. This one mutates the filesystem and can repoint a remote, so
    acting on yesterday's scope is a confidentiality violation rather than a flap — the same
    asymmetry `freshness.py`'s docstring argues for. `lifecycle.cmd_converge` enforces it
    internally; this wrapper does not pass `--ignore-freshness` and must never grow the
    option.
    """
    import io
    import lifecycle as lifecycle_cli  # bin/ is already on sys.path

    try:
        plan_store = plans.load_plans()
    except Exception as error:
        return Result("lifecycle", paths.root(), FLAGGED,
                      "could not read the plan store: %s" % error, [])
    if not plan_store:
        return None

    approvals = plans.load_approvals()
    pending = [plan for plan in plan_store.values() if plans.approved(plan, approvals)[0]]
    if not pending:
        return None

    captured = io.StringIO()
    stdout = sys.stdout
    try:
        sys.stdout = captured
        argv = ["converge"] + (["--dry-run"] if dry_run else [])
        # 🔴 The sweep may arm a purge, but ONLY for a machine named in `sync/PURGE_ARMED`.
        # Lukas's ruling 2026-10-01, on being told that interactive-only arming left one
        # machine unable ever to finish a fleet-wide purge: *"I don't like this. I want it to
        # work. Make it work."*
        #
        # This file used to be guarded by a test asserting the flag was ABSENT from this
        # function. That test was correct for the design it guarded and its premise has been
        # deliberately reversed; it is replaced by one asserting the flag is passed ONLY under
        # the declaration, which is a strictly stronger property than never passing it.
        #
        # What this does NOT loosen: a purge still needs a proposed plan and an explicit
        # approval carrying the printed fleet-wide consequence. The declaration removes the
        # third gate only, and only where a human wrote the machine's name down and committed
        # it. `purge.sweep_may_arm` refuses an empty selector before consulting the selector
        # grammar, because an empty spec matches EVERY machine under that grammar.
        if purge.sweep_may_arm():
            argv.append("--arm-purge")
        code = lifecycle_cli.main(argv)
    except Exception as error:
        sys.stdout = stdout
        return Result("lifecycle", paths.root(), FLAGGED,
                      "converge raised: %s" % error, [])
    finally:
        sys.stdout = stdout

    actions = [line for line in captured.getvalue().split("\n") if line.strip()]
    state = SYNCED if code == 0 else FLAGGED
    return Result("lifecycle", paths.root(), state,
                  "%d approved plan(s); converge exited %d" % (len(pending), code),
                  actions)


def clone_step(entries, dry_run: bool, where: Optional[str] = None) -> Optional[Result]:
    """Clone participating repos this machine does not hold. Returns None when none are.

    Scope is every participating registry row, not only the repos a skill needs. Lukas chose
    full parity: a machine ends up holding all 25, so nothing has to be remembered and no
    capability is quietly missing. The cost is that the first sweeps on a fresh machine pull
    real data, which is why `skillclone` takes one repo per sweep — the fleet converges over a
    few dozen sweeps instead of stalling one sweep for an hour.

    Runs before the repo sweep so a repo cloned now is swept in this same pass rather than
    waiting for the next one. Absent repos still report `ABSENT` in the same run: the clone is
    attempted one-per-sweep, so most of them genuinely are still absent when the sweep reads
    them, and claiming otherwise would be a lie in the report.
    """
    absent = [entry for entry in entries
              if not os.path.isdir(os.path.expanduser(entry.path))]
    if not absent:
        return None

    # 🔴 A row whose PREVIOUS identity is still checked out here is mid-rename, not absent.
    # Cloning it would produce a second checkout of one repo — the RimMaster/RimMandrake
    # failure in docs/SYNC_AGENT.md — because absence is computed from the row's Path while
    # `adopt` matches on origin URL, and a renamed row has neither in common with the old
    # checkout. Stand aside and let the lifecycle converger do the rename: adopting in place
    # would record the old path and make the mismatch permanent, which Lukas ruled against.
    inventory = skillclone.checkouts()
    mid_rename = []
    missing = []
    for entry in absent:
        hit = skillclone.former_instance(entry, inventory=inventory)
        if hit is None:
            missing.append(entry.name)
            continue
        path, matched = hit
        mid_rename.append(
            "%s: NOT cloned — %s is still here and matches its %s, so this row is "
            "mid-rename. `bin/lifecycle.py converge` completes it; cloning now would make a "
            "second checkout." % (entry.name, path, matched))

    if not missing:
        return Result("clones", os.path.expanduser("~/dev"), FLAGGED,
                      "%d row(s) mid-rename, nothing cloned" % len(mid_rename), mid_rename)

    name = "clones"
    try:
        mapping = skillclone.read_remotes()
        attempts = skillclone.clone_missing(missing, entries, mapping, dry_run=dry_run,
                                            where=where)
    except Exception as error:  # cloning must never cost the sweep
        return Result(name, os.path.expanduser("~/dev"), FLAGGED,
                      "clone step failed: %s" % error, [])

    actions = mid_rename + ["%s: %s" % (attempt.name, attempt.detail) for attempt in attempts]
    cloned = [attempt for attempt in attempts if attempt.ok]
    adopted = [attempt for attempt in cloned if attempt.detail.startswith("adopted")]
    detail = "%d of %d absent repo(s) cloned this sweep" % (len(cloned) - len(adopted),
                                                            len(missing))
    if adopted:
        detail += ", %d adopted from an existing checkout" % len(adopted)
    if mid_rename:
        detail += ", %d held back mid-rename" % len(mid_rename)
    if not attempts and not mid_rename:
        return None
    return Result(name, os.path.expanduser("~/dev"),
                  SYNCED if cloned else ABSENT, detail, actions)


def conversations_step(entries, dry_run: bool) -> Optional[Result]:
    """Re-derive each participating repo's conversation records. None when nothing changed.

    This is the unattended backstop for `bin/hooks/conversation_record.py`, and it exists
    because that hook cannot cover its own failure modes: `SessionEnd` does not fire on
    SIGKILL or power loss, it carries no guarantee that `transcript_path` has been flushed,
    and its output reaches only the debug log — so a hook that silently stopped working
    would look exactly like a quiet week. Re-deriving here is cheap (a 2 MB transcript
    renders in ~15 ms) and the writer compares before writing, so a repo whose records are
    already current costs nothing and dirties nothing.

    🔴 **It must run BEFORE `sweep()`.** The records are classified sweepable, so a record
    written during this pass is committed and pushed by the same pass; written afterwards it
    would sit uncommitted for an hour, which is the gap this step is here to close.

    Only `kind == "repo"` is written. Drive projects are deliberately excluded: they have no
    registry row by design, so the sweep never visits them, and a Drive folder's record is
    written by the hook or by hand instead.
    """
    written: List[str] = []
    problems: List[str] = []
    for entry in entries:
        root = os.path.expanduser(entry.path)
        if not os.path.isdir(root):
            continue
        try:
            for transcript in conversations.sessions_for(root):
                target, changed = conversations.write_record(
                    root, transcript, dry_run=dry_run)
                if changed:
                    # A github.com repo's records live in the private overlay, not under `root`.
                    written.append(os.path.join(entry.name, conversations.RECORD_DIR,
                                                os.path.basename(target)))
        except Exception as error:
            problems.append("%s: %s" % (entry.name, error))

    if not written and not problems:
        return None
    detail_parts = []
    if written:
        detail_parts.append("%d record(s) %s"
                            % (len(written), "would be written" if dry_run else "written"))
    if problems:
        detail_parts.append("%d repo(s) failed" % len(problems))
    actions = written[:20]
    if len(written) > 20:
        actions.append("...and %d more" % (len(written) - 20))
    actions.extend(problems)
    return Result("conversations", paths.root(),
                  FLAGGED if problems else SYNCED,
                  "; ".join(detail_parts), actions)


def _mail_dir() -> str:
    """The mail archive the census checks; named by the (private) mailreconcile adapter."""
    return str(getattr(mailreconcile, "MAIL_DIR", "") or "mail archive")


def mail_reconcile_step(dry_run: bool) -> Optional[Result]:
    """At most daily, on `mailreconcile.REPAIR_MACHINE` only: census the mail archive against
    Graph and collect any gap. None when it is not this machine's turn.

    🔴 **BEFORE `sweep()`**, for `conversations_step`'s reason: repaired mail files are swept by
    this same pass. The census exists because a daily collector can lose days in silence —
    sentinels are run signals, never a census. It runs under the adapter's own interpreter
    (`mailreconcile.GRAPH_PYTHON`); this process stays on plain `python3`.
    """
    if dry_run or machine.key() != mailreconcile.REPAIR_MACHINE:
        return None
    # Only from the real checkout: a sweep run against a fixture root (every sync test) must
    # never reach a real mailbox or write into the archive.
    if os.path.realpath(paths.root()) != os.path.realpath(os.path.expanduser("~/dev/Lodestar")):
        return None
    graph_python = str(getattr(mailreconcile, "GRAPH_PYTHON", "") or "")
    if not graph_python or not os.path.exists(graph_python):
        return Result("mail-reconcile", _mail_dir(), FLAGGED,
                      "no graphaccess venv at %s" % (graph_python or "(none configured)"), [])
    report = paths.state_file(mailreconcile.REPORT_NAME)
    if (os.path.exists(report) and time.time() - os.path.getmtime(report)
            < mailreconcile.MIN_INTERVAL_HOURS * 3600):
        return None
    import subprocess
    try:
        done = subprocess.run([graph_python, os.path.join(paths.root(), "bin", "mail_reconcile.py"),
                               "--repair"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=1800)
    except subprocess.TimeoutExpired:
        return Result("mail-reconcile", _mail_dir(), FLAGGED,
                      "timed out after 30 min", [])
    lines = (done.stdout or b"").decode("utf-8", "replace").strip().splitlines()
    last = lines[-1] if lines else "no output (exit %d)" % done.returncode
    return Result("mail-reconcile", _mail_dir(),
                  SYNCED if done.returncode == 0 else FLAGGED, last,
                  [l for l in lines if l.startswith(("GAP", "repair"))][:20])


def claude_config_step(dry_run: bool) -> Result:
    """Back up ~/.claude into the repo, and converge CLAUDE.md where that is provably safe.

    The asymmetry is deliberate and is the whole safety argument for automating this:

    - **Snapshotting in is lossless** — it only ever copies live files into the repo, so
      running it unattended on every sweep can lose nothing. That is what makes
      settings.json and the auto-memory continuously backed up rather than backed up
      whenever someone remembers.
    - **Rendering out is lossy in general** — it overwrites ~/.claude/CLAUDE.md, and `#`
      memory writes land there mid-session. But it is *not* lossy when the live file still
      matches the provenance copy in claude/snapshot/<key>/CLAUDE.md, because then nothing
      has been written to it since we last wrote it. That case renders automatically, which
      is how a shared-module edit made on one machine reaches the other unattended.
      Every other case captures the live file verbatim and flags for `claudeconf.py adopt`.

    Rendering is still never *unconditional*: see claudeconf.converge_claudemd, whose
    invariant I1 is what keeps a captured edit from being overwritten on the next sweep.
    """
    actions = []  # type: List[str]
    name = "~/.claude"
    path = claudeconf.CLAUDE_HOME
    withheld_memory = False

    try:
        if dry_run:
            actions.append("would snapshot ~/.claude")
        else:
            copied, files, projects = claudeconf.snapshot_all()
            if files < 0:
                withheld_memory = True
                actions.append("snapshotted %d file(s); auto-memory WITHHELD (work machine, "
                               "no usable private store)" % len(copied))
            elif copied or files:
                actions.append("snapshotted %d file(s) + %d memory file(s)"
                               % (len(copied), files))
    except OSError as error:
        return Result(name, path, FLAGGED, "snapshot failed: %s" % error, actions)

    outcome, detail = claudeconf.converge_claudemd(dry_run)
    if outcome == claudeconf.FAST_FORWARD:
        actions.append(detail)
    problems = [detail] if outcome in (claudeconf.DRIFTED, claudeconf.BLOCKED) else []
    if withheld_memory:
        problems.append("auto-memory snapshot withheld: no usable private store")

    # Skills and settings must not be skipped just because CLAUDE.md drifted: they are
    # independent surfaces, and a dangling skill is invisible until something reports it.
    try:
        skill_lines, skill_faults = claudeconf.skills_state(dry_run=dry_run)
        actions.extend("skills: %s" % line for line in skill_lines)
        problems.extend("skills: %s" % fault for fault in skill_faults)
    except Exception as error:  # a broken sub-step must never stop the sweep
        problems.append("skills step failed: %s" % error)

    try:
        # Converge settings.json the same way CLAUDE.md converges: render from the committed
        # base plus this machine's overlay, but only when the live file still matches what we
        # last wrote. Otherwise capture and flag — a permission the user granted at a prompt
        # lands in this file, and must never be silently discarded.
        #
        # Provenance is settings.rendered.json, NOT the settings.json under the same snapshot
        # directory, and that distinction is load-bearing: settings.json IS in
        # claudeconf.SNAPSHOT_FILES and is copied live -> repo unconditionally every sweep. Had
        # provenance been that file, a granted permission would be laundered into provenance by
        # the next snapshot, and the sweep after that would read live == previous, fast-forward,
        # and overwrite the grant. That is the I1 violation D18 names as the reason CLAUDE.md
        # must never join SNAPSHOT_FILES.
        settings_outcome, settings_detail = claudesettings.converge_settings(dry_run=dry_run)
        if settings_outcome in (claudesettings.FAST_FORWARD, claudesettings.UNCONFIGURED):
            actions.append("settings: %s" % settings_detail)
        elif settings_outcome in (claudesettings.DRIFTED, claudesettings.BLOCKED):
            problems.append("settings: %s" % settings_detail)

        settings_lines, settings_faults = claudesettings.report(
            os.path.join(claudeconf.CLAUDE_HOME, "settings.json"),
            os.path.join(paths.claude_dir(), "snapshot"),
            machine.key())
        actions.extend("settings: %s" % line for line in settings_lines)
        problems.extend("settings: %s" % fault for fault in settings_faults)
    except Exception as error:
        problems.append("settings step failed: %s" % error)

    try:
        # The status line, converged the same way CLAUDE.md is: render from the committed sources
        # for THIS machine, write only while the live file still matches what we last wrote, and
        # otherwise capture and refuse. Three machines had drifted into three unrelated scripts
        # because claudeconf.SNAPSHOT_FILES copies statusline.sh live -> repo and nothing ever
        # wrote it back.
        #
        # Provenance is statusline.rendered.sh, NOT the statusline.sh under the same snapshot
        # directory — the identical trap documented for settings above, and worse here, because
        # the thing it would silently overwrite is a hand-written script rather than one granted
        # permission. claudestatusline pins that structurally.
        #
        # GATED: converges only the machines named in claude/statusline/ENABLED, which is empty
        # until a flip is committed. So this step is inert on arrival and is enabled one box at a
        # time — and a machine left out is silent here rather than reported, because not being
        # enrolled yet is not a fault.
        if claudestatusline.is_enabled():
            sl_outcome, sl_detail = claudestatusline.converge(dry_run=dry_run)
            if sl_outcome == claudestatusline.FAST_FORWARD:
                actions.append("statusline: %s" % sl_detail)
            elif sl_outcome in (claudestatusline.DRIFTED, claudestatusline.BLOCKED):
                problems.append("statusline: %s" % sl_detail)
    except Exception as error:
        # Its own boundary even though converge() already swallows everything: the gate lookup
        # reads machines.md and a file, either of which can fail, and this step must never be the
        # reason the settings and skills results above are discarded.
        problems.append("statusline step failed: %s" % error)

    if not dry_run:
        # Root fix for the perpetually-dirty snapshot (2026-10-19): `is_source` withholds the
        # `.sh` snapshots from the generic commit, so commit what this step wrote, by name.
        try:
            done = gitutil.commit_generated_snapshots(paths.root())
            if done:
                actions.append("committed %d generated snapshot file(s)" % len(done))
        except Exception as error:
            problems.append("snapshot commit failed: %s" % error)

    if problems:
        return Result(name, path, FLAGGED, "; ".join(problems[:3]), actions)
    if not actions:
        return Result(name, path, CLEAN, "up to date", actions)
    return Result(name, path, SYNCED, ", ".join(actions), actions)


def push_url_mismatches(entries) -> List[Tuple[str, str, List[str]]]:
    """[(name, fetch_url, [differing push url(s)])] for repos present here whose push
    endpoint's IDENTITY differs from its fetch endpoint's. Report-only; fixes nothing.

    `git remote set-url origin <new>` does not touch a separately-configured push URL, so a
    repo believed rehomed on the strength of its fetch URL can still be pushing to the old
    host — verified in a fixture on 2026-09-30 (see gitutil.remote_endpoints). Compared with
    `skillclone.same_remote`, never string equality: ssh vs. https for the same repo is not a
    mismatch, and treating it as one would flag every repo on a machine that reaches
    GitHub over https while the others use ssh — that tolerance is deliberate and load-bearing.
    """
    mismatches = []  # type: List[Tuple[str, str, List[str]]]
    for entry in entries:
        repo = os.path.expanduser(entry.path)
        if not os.path.isdir(repo):
            continue  # not present on this machine; nothing to check
        fetch_url, push_urls = gitutil.remote_endpoints(repo)
        if not fetch_url:
            continue  # no origin configured here; out of scope for this check
        differing = [url for url in push_urls if not skillclone.same_remote(url, fetch_url)]
        if differing:
            mismatches.append((entry.name, fetch_url, differing))
    return mismatches


class RemoteAudit(NamedTuple):
    """What this machine's checkouts actually point at, against what the registry DECLARES.

    Four disjoint buckets over the rows examined, so a count can be read off directly rather
    than inferred from a difference. `examined` is the number of rows that reached a verdict at
    all (`agreeing + diverging + undeclared + no_origin`); rows whose checkout is absent here
    never enter it.
    """
    agreeing: List[str]
    # (name, declared remote, observed fetch url, [observed urls that differ from declared])
    diverging: List[Tuple[str, str, str, List[str]]]
    # (name, observed origin or "" when there is none) -- the registry has no Remote to compare
    undeclared: List[Tuple[str, str]]
    # (name, declared remote) -- a checkout is here but configures no `origin` at all
    no_origin: List[Tuple[str, str]]
    skipped: List[str]  # checkout not present on this machine; nothing observable
    error: str = ""     # set when the audit itself raised, so it reports on its own failure

    @property
    def examined(self) -> int:
        return (len(self.agreeing) + len(self.diverging)
                + len(self.undeclared) + len(self.no_origin))


def declared_remote_audit(entries) -> RemoteAudit:
    """Compare each present checkout's ACTUAL origin endpoints against the registry's `Remote`.

    This is a different question from `push_url_mismatches`, and the gap between them is why it
    exists. That check compares a checkout's push URL against *its own* fetch URL, so a checkout
    that is internally consistent but points somewhere the registry never declared passes it
    cleanly — and it truthfully reported "None found" on two machines while one machine's
    renamed checkout had `origin` = `…/OldName.git` against a registry declaring
    `…/NewName.git`. Nothing in the fleet could see that class of drift: the registry's
    declaration was never one of the two things being compared.

    Compared with `skillclone.same_remote`, never string equality, for the same load-bearing
    reason the push check gives: one machine may reach GitHub over ssh while another uses https,
    and rows declaring an `https://` Remote may legitimately be held as `git@`.
    String equality would report every one of those as drift and bury the real finding.

    Both the fetch URL and every push URL are compared against the declaration, so a repo whose
    fetch was rehomed and whose push was left behind reports here as well as in the push check.

    REPORT-ONLY. It repairs nothing and no caller may read a finding here as permission to act.
    Phase 3 enforcement was Lukas's separate decision; his card of 2026-10-28 08:25 made it for
    PUSH only, and `lib/pushguard.py` (called from `sweep_one`) is that gate. Nothing else is.
    """
    agreeing = []  # type: List[str]
    diverging = []  # type: List[Tuple[str, str, str, List[str]]]
    undeclared = []  # type: List[Tuple[str, str]]
    no_origin = []  # type: List[Tuple[str, str]]
    skipped = []  # type: List[str]

    for entry in entries:
        repo = os.path.expanduser(entry.path)
        if not os.path.isdir(repo):
            skipped.append(entry.name)
            continue
        fetch_url, push_urls = gitutil.remote_endpoints(repo)
        if not entry.remote:
            # Reported with whatever origin it does have: "the registry is behind this checkout"
            # is a more useful sentence than "no declaration", and the URL is the candidate.
            undeclared.append((entry.name, fetch_url or ""))
            continue
        if not fetch_url:
            no_origin.append((entry.name, entry.remote))
            continue
        observed = [fetch_url] + [url for url in push_urls if url != fetch_url]
        differing = [url for url in observed
                     if not skillclone.same_remote(url, entry.remote)]
        if differing:
            diverging.append((entry.name, entry.remote, fetch_url, differing))
        else:
            agreeing.append(entry.name)

    return RemoteAudit(sorted(agreeing), sorted(diverging), sorted(undeclared),
                       sorted(no_origin), sorted(skipped))


def enrollment_state(entries) -> Tuple[List[str], List[str], Dict[str, List[str]]]:
    """(enrolled, unenrolled, conflicts) for the checkouts present on this machine.

    Report-only, and it exists chiefly so `lib/enroll.py` has a production caller at all. That
    module shipped with thirteen passing tests and nothing calling it, and not one checkout was
    stamped — the failure mode CLAUDE.md names in capitals, because green tests plus no caller
    is indistinguishable from a working feature.

    Conflicts are scanned across EVERY checkout under the dev root rather than only registered
    ones: a `cp -R` duplicate normally sits beside the real checkout under a name the registry
    has never heard of, and that is precisely the case worth surfacing.
    """
    enrolled = []  # type: List[str]
    unenrolled = []  # type: List[str]
    for entry in entries:
        path = os.path.expanduser(entry.path)
        if not os.path.exists(os.path.join(path, ".git")):
            continue
        if not entry.repo_id:
            continue
        if enroll.read_stamp(path) == entry.repo_id:
            enrolled.append(entry.name)
        else:
            unenrolled.append(entry.name)
    try:
        found = enroll.conflicts([path for path, _ in skillclone.checkouts()])
    except Exception:
        found = {}
    return sorted(enrolled), sorted(unenrolled), found


#: Fault kinds withheld from the advisory list for VOLUME, never for importance.
#:
#: `missing-id` fired on EVERY row before the registry schema migration, and
#: `tests/test_lifecycle_sweep.py` records why that mattered: burying the real faults under
#: ~50 rows of noise "is the difference between a check and an outage". That test pins the
#: behaviour, so it is honoured rather than edited — but its premise has since expired, because
#: the migration has run and the live count is 0. This is therefore a standing guard against the
#: condition returning, not an active filter. Anything NOT listed here is rendered: non-blocking
#: means "does not gate", never "is not reported".
ADVISORY_VOLUME_SUPPRESSED = ("missing-id",)


def report(results: List[Result], our_role: str, dry_run: bool,
           excluded_entries, elsewhere_entries=(),
           freshness_verdict: Optional[freshness.Verdict] = None,
           push_mismatches: Optional[List[Tuple[str, str, List[str]]]] = None,
           desired_state_faults: Optional[List[validate.Fault]] = None,
           enrollment: Optional[Tuple[List[str], List[str], Dict[str, List[str]]]] = None,
           remote_audit: Optional[RemoteAudit] = None,
           mirror_outcomes: Optional[List[mirrors.Outcome]] = None) -> str:
    ranked = sorted(results, key=lambda r: (ORDER.get(r.outcome, 9), r.name))
    icon = ICON

    lines = ["# Sync health — %s" % our_role, ""]
    lines.append("<!-- Generated by bin/sync.py. Machine-local and gitignored: this file "
                 "differs per machine, so committing it would make two machines overwrite "
                 "each other (decision D-D). -->")
    lines.append("")
    lines.append("Last sweep: **%s**%s" % (clock.now(), "  · DRY RUN" if dry_run else ""))
    lines.append("")

    if freshness_verdict is not None:
        # REPORT-ONLY in this phase: nothing above is skipped on the strength of this line.
        # A later phase enforces it (refuses to act when this machine's own view of intent is
        # stale — see lib/freshness.py's module docstring for exactly what that does and does
        # not buy). Recording that out loud here so a future reader cannot mistake a report
        # for an enforcement that has not been wired in yet.
        lines.append("Freshness gate on this checkout (report-only; does not yet skip "
                     "anything — see lib/freshness.py): **%s** — %s"
                     % ("OK" if freshness_verdict.ok else "STALE", freshness_verdict.reason))
        lines.append("")

    flagged = [r for r in ranked if r.outcome == FLAGGED]
    if flagged:
        lines.append("## 🚩 Needs you — %d" % len(flagged))
        lines.append("")
        for result in flagged:
            lines.append("- **%s** — %s" % (result.name, result.detail))
        lines.append("")

    # Mirror states are listed here when they need him, but never as repo rows: a mirror
    # never changes a repo's own outcome or the flagged count (design D5).
    loud_mirrors = mirrors.flagged(mirror_outcomes or [])
    if loud_mirrors:
        lines.append("## 🚩 Mirrors need you — %d" % len(loud_mirrors))
        lines.append("")
        for outcome in loud_mirrors:
            lines.append("- **%s** — %s: %s" % (outcome.repo_id or outcome.name,
                                                 outcome.label, outcome.detail))
        lines.append("")

    contended = [r for r in ranked if r.outcome == CONTENDED]
    if contended:
        lines.append("## 🔓 Git lock contention — %d" % len(contended))
        lines.append("")
        lines.append("These recovered, but a `.git/index.lock` got in the way. Worth a look "
                     "if it repeats: it means two writers are colliding, or something left "
                     "debris behind.")
        lines.append("")
        for result in contended:
            lines.append("- **%s** — %s" % (result.name, result.detail))
        lines.append("")

    lines.append("## All repos")
    lines.append("")
    lines.append("`Remote` is the honest half of every row: **verified** means this sweep "
                 "reached the remote, and **not checked for N** means it did not. A repo's "
                 "tier comes from its own newest commit — committed within %dh is `hot`, "
                 "within %dd is `warm`, older is `cold` — so a repo promotes itself the "
                 "moment work starts in it, with no registry edit. A clean, non-diverged "
                 "repo is only fetched every %s (hot) / %s (warm) / %s (cold); anything "
                 "dirty or ahead is fetched every sweep whatever its tier, because a push "
                 "has to know what it lands on. `clean` therefore means *no local changes* and never "
                 "implies the remote was just checked."
                 % (HOT_MAX_COMMIT_AGE // 3600, WARM_MAX_COMMIT_AGE // 86400,
                    _span(FETCH_INTERVAL[HOT]), _span(FETCH_INTERVAL[WARM]),
                    _span(FETCH_INTERVAL[COLD])))
    lines.append("")
    lines.append("| | Repo | Outcome | Remote | Detail |")
    lines.append("|-|------|---------|--------|--------|")
    for result in ranked:
        lines.append("| %s | `%s` | %s | %s | %s |" % (
            icon.get(result.outcome, "?"), result.name, result.outcome,
            remote_note(result.tier, result.fetched, result.remote_age),
            result.detail.replace("|", "\\|")))
    lines.append("")

    tiered = [r for r in ranked if r.tier]
    deferred = [r for r in tiered if not r.fetched]
    if deferred:
        lines.append("**%d of %d repo(s) had their fetch deferred this sweep** — %d fetch(es) "
                     "not made. Nothing *local* was deferred: `git status` ran on all %d, so "
                     "anything with work to push was fetched and pushed on the spot."
                     % (len(deferred), len(tiered), len(deferred), len(tiered)))
        lines.append("")

    if excluded_entries:
        lines.append("## Not participating")
        lines.append("")
        for entry in excluded_entries:
            lines.append("- `%s` — %s" % (entry.name, entry.notes or "sync: off"))
        lines.append("")

    if elsewhere_entries:
        lines.append("## Not carried on this machine")
        lines.append("")
        lines.append("Registry rows whose `Machines` column leaves `%s` out: never cloned or "
                     "swept here. %s" % (our_role, ", ".join("`%s`" % entry.name
                                                           for entry in elsewhere_entries)))
        lines.append("")

    if mirror_outcomes:
        lines.append("## Mirrors")
        lines.append("")
        lines.append(mirrors.summary_line(mirror_outcomes))
        lines.append("")
        for outcome in mirror_outcomes:
            lines.append("- `%s` → %s — %s: %s" % (outcome.repo_id or outcome.name,
                                                   outcome.mirror, outcome.label,
                                                   outcome.detail))
        lines.append("")

    lines.append("## Push/fetch endpoint mismatches")
    lines.append("")
    lines.append("Reported only — nothing here is changed automatically. A push URL is "
                 "compared against the fetch URL by identity (`skillclone.same_remote`), not "
                 "string equality, so ssh vs. https for the same repo is never one of these.")
    lines.append("")
    if push_mismatches:
        for name, fetch_url, differing in push_mismatches:
            lines.append("- **%s** — fetch `%s`; push %s"
                         % (name, fetch_url, ", ".join("`%s`" % url for url in differing)))
    else:
        lines.append("None found.")
    lines.append("")

    # Always rendered, even when clean — same argument as the section below it: an absent
    # section reads as "not checked", and this one's whole reason for existing is that the
    # section above it reported "None found" on two machines while a real divergence sat on one of
    # them. A clean result here has to be visibly a result.
    if remote_audit is not None:
        lines.append("## Declared vs. actual remotes")
        lines.append("")
        lines.append("**This section gates nothing** and repairs nothing. Phase 3 enforcement "
                     "of the same comparison is the separate push guard (`lib/pushguard.py`, "
                     "owner card 2026-10-28): a repo listed as diverging below was NOT pushed "
                     "this sweep and its own row is flagged. Fetches and pulls are not gated.")
        lines.append("")
        lines.append("Each participating row whose checkout exists on this machine, with its "
                     "`origin` fetch and push URLs compared against the `Remote` the registry "
                     "**declares** — which is the comparison the section above does *not* make. "
                     "That one compares a checkout's push URL against its own fetch URL, so a "
                     "checkout pointing somewhere the registry never declared passes it "
                     "cleanly; it read `None found` on two machines while a third's "
                     "renamed checkout origin was still `…/OldName.git`. Compared by "
                     "identity (`skillclone.same_remote`), so ssh vs. https for the same repo "
                     "is never a finding — rows may declare `https://` and be held "
                     "as `git@`.")
        lines.append("")
        if remote_audit.error:
            lines.append("🚩 **%s** — read nothing below as a clean result."
                         % remote_audit.error)
            lines.append("")
        lines.append("- examined: **%d** of %d row(s) — %d agree with the registry"
                     % (remote_audit.examined,
                        remote_audit.examined + len(remote_audit.skipped),
                        len(remote_audit.agreeing)))
        if remote_audit.skipped:
            lines.append("- not examined: **%d** — no checkout here: %s"
                         % (len(remote_audit.skipped),
                            ", ".join("`%s`" % name for name in remote_audit.skipped)))
        lines.append("")
        if remote_audit.diverging:
            lines.append("🚩 **%d checkout(s) point somewhere the registry does not declare:**"
                         % len(remote_audit.diverging))
            for name, declared, fetch_url, differing in remote_audit.diverging:
                lines.append("- **%s** — declared `%s`; origin fetch `%s`; differs: %s"
                             % (name, declared, fetch_url,
                                ", ".join("`%s`" % url for url in differing)))
            lines.append("")
        else:
            lines.append("No divergence from the declared `Remote` found.")
            lines.append("")
        if remote_audit.undeclared:
            lines.append("**%d row(s) declare no `Remote`**, so nothing could be compared. The "
                         "URL shown is what the checkout here actually uses, i.e. the candidate "
                         "the registry is missing — recorded, never written:"
                         % len(remote_audit.undeclared))
            for name, observed in remote_audit.undeclared:
                lines.append("- `%s` — origin %s"
                             % (name, "`%s`" % observed if observed else "(none either)"))
            lines.append("")
        if remote_audit.no_origin:
            lines.append("**%d checkout(s) configure no `origin` at all**, so the declared "
                         "`Remote` has nothing to compare against:" % len(remote_audit.no_origin))
            for name, declared in remote_audit.no_origin:
                lines.append("- `%s` — declared `%s`" % (name, declared))
            lines.append("")

    # Always rendered, even when clean. An absent section reads as "not checked", and the
    # whole point of this one is that a CLEAN git merge can leave the desired state
    # self-contradictory — measured 2026-09-30: one peer retiring a repo and another renaming
    # the still-active row merged with exit 0 and no conflict markers. A merged commit is a
    # transport result, not an authorization result.
    lines.append("## Desired-state faults")
    lines.append("")
    lines.append("Reported only in this phase. These are contradictions in the merged "
                 "registry, `machines.md` and tombstones — the checks that will gate "
                 "rename/rehome/retire once those act (see "
                 "`docs/superpowers/specs/2026-09-30-repo-lifecycle-design.md` §3D).")
    lines.append("")
    all_faults = desired_state_faults or []
    blocking_faults = validate.blocking(all_faults)
    advisory = [fault for fault in all_faults if fault not in blocking_faults]
    if blocking_faults:
        for fault in blocking_faults:
            lines.append("- **%s** `%s` — %s" % (fault.kind, fault.subject, fault.detail))
    else:
        lines.append("None found.")
    lines.append("")
    # 🔴 Non-blocking faults are rendered too, and leaving them out was a real defect.
    # This section used to print only `validate.blocking(...)`, so a check added as
    # non-blocking fired in `validate.faults()` and reached NOBODY — SYNC.md is the only
    # surface anyone reads. Measured 2026-09-30: the two name-vs-remote checks returned 2
    # faults live while this section said "None found", which is this repo's signature defect
    # (a complete, tested subsystem whose production surface never shows it). Non-blocking
    # means "does not gate", never "is not reported".
    lines.append("Advisory — these do **not** gate anything and nothing is skipped on their "
                 "account; they are reported because a check nobody can see is the same as no "
                 "check at all.")
    lines.append("")
    advisory = [f for f in advisory if f.kind not in ADVISORY_VOLUME_SUPPRESSED]
    if advisory:
        for fault in advisory:
            lines.append("- %s `%s` — %s" % (fault.kind, fault.subject, fault.detail))
    else:
        lines.append("None advisory.")
    lines.append("")

    # Always rendered once any plan or receipt exists, even when every plan is complete
    # everywhere: "complete on all three" is the answer this section is most often asked for,
    # and an absent section reads as "not checked". Derived from the committed per-machine
    # receipts — never from this machine's own completion store, which no peer can see, and
    # never from inference about a machine that has filed nothing.
    try:
        lifecycle_lines = completions.section_lines()
    except Exception as error:  # a derived report must never cost a sweep
        lifecycle_lines = ["- could not read the lifecycle receipts: %s" % error]
    if lifecycle_lines:
        lines.append("## Lifecycle plan completion")
        lines.append("")
        lines.append("Who has finished which plan. Each machine writes only its own "
                     "`sync/lifecycle-receipts/<key>.json`, so no machine can assert another's "
                     "state; `pending (no receipt)` means that machine has filed nothing, not "
                     "that it failed. A receipt recorded against a superseded fingerprint "
                     "reads as pending, because the act it describes has changed.")
        lines.append("")
        lines.extend(lifecycle_lines)
        lines.append("")

    if enrollment is not None:
        enrolled, unenrolled, conflicting = enrollment
        lines.append("## Enrollment")
        lines.append("")
        lines.append("Which checkouts carry their registry `Id` as a `lodestar.id` stamp. A "
                     "stamp asserts enrollment, never ownership — a directory copy copies it, "
                     "which is why a duplicate claim is reported rather than resolved. Run "
                     "`bin/enroll_checkouts.py` to stamp the unenrolled.")
        lines.append("")
        lines.append("- enrolled: **%d**" % len(enrolled))
        if unenrolled:
            lines.append("- unenrolled: **%d** — %s"
                         % (len(unenrolled), ", ".join("`%s`" % name for name in unenrolled)))
        else:
            lines.append("- unenrolled: **0**")
        if conflicting:
            lines.append("")
            lines.append("🚩 **%d id(s) claimed by more than one checkout.** Both copies may hold "
                         "real divergent work, so nothing chooses between them:"
                         % len(conflicting))
            for repo_id, dirs in sorted(conflicting.items()):
                lines.append("- `%s`: %s" % (repo_id, ", ".join("`%s`" % p for p in dirs)))
        lines.append("")
    return "\n".join(lines) + "\n"


def write_status(results: List[Result], our_role: str, dry_run: bool,
                 freshness_verdict: Optional[freshness.Verdict] = None,
                 push_mismatches: Optional[List[Tuple[str, str, List[str]]]] = None,
                 desired_state_faults: Optional[List[validate.Fault]] = None,
                 enrollment: Optional[Tuple[List[str], List[str],
                                            Dict[str, List[str]]]] = None,
                 mirror_outcomes: Optional[List[mirrors.Outcome]] = None) -> str:
    payload = {
        "machine": our_role,
        "swept_at": clock.now(),
        "dry_run": dry_run,
        "freshness": (None if freshness_verdict is None
                     else {"ok": freshness_verdict.ok, "reason": freshness_verdict.reason}),
        # Only the blocking subset: `missing-id` fires on every row until the schema migration
        # runs, and carrying ~50 of those in machine-local status would bury the real faults.
        "desired_state_faults": [
            {"kind": fault.kind, "subject": fault.subject, "detail": fault.detail}
            for fault in validate.blocking(desired_state_faults or [])
        ],
        "enrollment": (None if enrollment is None else {
            "enrolled": enrollment[0],
            "unenrolled": enrollment[1],
            "conflicts": enrollment[2],
        }),
        # Design D5: mirror states live here, apart from `repos`, so they cannot alter any
        # repo's outcome.
        "mirrors": mirrors.status_payload(mirror_outcomes or []),
        "push_mismatches": [
            {"name": name, "fetch_url": fetch_url, "push_urls": differing}
            for name, fetch_url, differing in (push_mismatches or [])
        ],
        "repos": {
            result.name: {
                "path": result.path,
                "outcome": result.outcome,
                "detail": result.detail,
                "actions": result.actions,
                # Freshness, structured: `outcome` alone cannot distinguish "clean and just
                # verified" from "clean and not checked for six hours".
                "tier": result.tier,
                "fetched": result.fetched,
                "remote_age_seconds": (None if result.remote_age is None
                                       else int(result.remote_age)),
                "remote": remote_note(result.tier, result.fetched, result.remote_age),
            }
            for result in results
        },
    }  # type: Dict[str, object]
    target = paths.sync_status_file()
    paths.ensure_dir(os.path.dirname(target))
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, target)
    return target


def _resolve_repo(target: Optional[str]) -> Optional[Tuple[str, str]]:
    """(name, toplevel path) for a registry name or any path inside a repo (default: cwd)."""
    if target:
        for entry in registry.entries():
            if entry.name == target:
                return entry.name, entry.path
    where = os.path.abspath(os.path.expanduser(target or os.getcwd()))
    if not os.path.isdir(where):
        return None
    code, out, _ = gitutil.run(where, "rev-parse", "--show-toplevel")
    if code != 0 or not out.strip():
        return None
    top = out.strip()
    known = registry.by_path()
    entry = known.get(os.path.realpath(top))
    return (entry.name if entry else os.path.basename(top)), top


def _who() -> str:
    try:
        who = machine.stamp()
    except Exception:
        who = "%s/%s" % (machine.key(), machine.runtime())
    session = machine.session_id()
    return "%s %s" % (who, session) if session else who


def pause_command(args) -> int:
    """--pause / --resume / --paused. Machine-local; touches only <git-dir>/lodestar-pause."""
    if args.paused:
        found = 0
        for entry in registry.entries():
            if not os.path.isdir(entry.path):
                continue
            payload = repopause.read(entry.path)
            if payload is None and not os.path.exists(repopause.pause_path(entry.path) or ""):
                continue
            found += 1
            if payload is None:
                print("  ⏸ %-24s unreadable pause file" % entry.name)
                continue
            print("  ⏸ %-24s %s until %s — %s [%s]" % (
                entry.name, "+".join(payload.get("directions") or repopause.DIRECTIONS),
                payload.get("until", "?"), payload.get("reason") or "no reason given",
                payload.get("set_by") or "?"))
        if not found:
            print("no repo is paused on this machine")
        return 0

    if args.resume is not None:
        resolved = _resolve_repo(args.resume or None)
        if not resolved:
            print("🛑 %s is not a registry name or a path inside a git repo"
                  % (args.resume or os.getcwd()), file=sys.stderr)
            return 1
        name, top = resolved
        print("%s: %s" % (name, "resumed" if repopause.clear(top) else "was not paused"))
        return 0

    values = list(args.pause)
    if len(values) == 1:
        target, span = None, values[0]
    elif len(values) == 2:
        target, span = values
    else:
        print("🛑 --pause takes [REPO] DURATION, e.g. --pause 8h or --pause Hestia 30m",
              file=sys.stderr)
        return 1
    minutes = duration.minutes(span)
    if not minutes:
        print("🛑 cannot read %r as a duration (try 30m, 8h, 1h30m)" % span, file=sys.stderr)
        return 1
    resolved = _resolve_repo(target)
    if not resolved:
        print("🛑 %s is not a registry name or a path inside a git repo"
              % (target or os.getcwd()), file=sys.stderr)
        return 1
    name, top = resolved
    directions = ("send",) if args.send_only else repopause.DIRECTIONS
    payload = repopause.set_pause(top, minutes * 60, reason=args.reason or "",
                                  set_by=_who(), directions=directions)
    print("%s: paused (%s) until %s. Auto-resumes after that once the repo has been quiet "
          "for %dm; never past %dh from now. Undo: sync.py --resume %s"
          % (name, "+".join(payload["directions"]), payload["until"],
             repopause.QUIET_WINDOW // 60, repopause.HARD_CAP // 3600, name))
    return 0


def discover(root: Optional[str] = None) -> List[Tuple[str, Optional[str], str]]:
    """[(path, origin, note)] for checkouts under the dev root that no registry row names.

    Report-only by contract: adding a row is the owner's decision (a repo can be private,
    someone else's, or a stale duplicate), so nothing here writes anything.
    """
    rows = registry.entries()
    by_real = set(os.path.realpath(entry.path) for entry in rows)
    remotes = skillclone.read_remotes()
    found = []
    for path, origin in skillclone.checkouts(root):
        if os.path.realpath(path) in by_real:
            continue
        twins = [name for name, url in sorted(remotes.items())
                 if origin and skillclone.same_remote(url, origin)]
        if twins:
            note = ("same origin as registered %s — a duplicate checkout, or that row's path "
                    "here (add a Machine paths row)" % ", ".join(twins))
        else:
            note = "not in the registry"
        found.append((path, origin, note))
    return found


ACTIVE_WATCHDOG = None  # type: Optional[Watchdog]


def main(argv=None):
    """Every return path stops the watchdog, so an early exit never leaves it armed."""
    try:
        return _main(argv)
    finally:
        if ACTIVE_WATCHDOG is not None:
            ACTIVE_WATCHDOG.stop()


def _main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                       help="report what would happen; fetch only, never commit or push, "
                            "and never record a fetch in ~/dev/.lodestar/fetch-state.json")
    parser.add_argument("--quiescence", type=int, default=DEFAULT_QUIESCENCE_MINUTES,
                       help="minutes a tree must be unchanged before auto-commit "
                            "(default: %d)" % DEFAULT_QUIESCENCE_MINUTES)
    parser.add_argument("--only", action="append",
                       help="limit the sweep to this repo name; repeatable")
    parser.add_argument("--quiet", action="store_true", help="write files, print nothing")
    parser.add_argument("--discover", action="store_true",
                       help="report checkouts under ~/dev that are not in the registry; "
                            "changes nothing")
    parser.add_argument("--pause", nargs="+", metavar="[REPO] DURATION",
                       help="pause one repo (registry name or path; default cwd) for a "
                            "duration like 30m or 8h")
    parser.add_argument("--reason", help="why, for --pause (shown in SYNC.md)")
    parser.add_argument("--send-only", action="store_true",
                       help="with --pause: stop committing/pushing but keep receiving")
    parser.add_argument("--resume", nargs="?", const="", metavar="REPO",
                       help="lift a repo's pause (default: cwd)")
    parser.add_argument("--paused", action="store_true", help="list paused repos")
    args = parser.parse_args(argv)

    if args.pause or args.resume is not None or args.paused:
        return pause_command(args)

    if args.discover:
        found = discover()
        if not found:
            print("every checkout under %s is in the registry" % skillclone.dev_root())
        for path, origin, note in found:
            print("  %-40s %-55s %s" % (path, origin or "(no origin)", note))
        return 0

    # The machine-wide lock against Tender (lib/sweeplock.py; design 2026-10-25-remote-execution
    # §4). Taken BEFORE the board pull, so a Tender recipe repairing a checkout never has the
    # sweep fast-forwarding, committing or pushing under it. A dry run takes no lock: it only
    # reports, and must not queue behind a repair to do so.
    if args.dry_run:
        held = sweeplock.status()
        if held["state"] == "held":
            print("note: sweep lock %s; this dry run reports anyway" % held["note"])
        return _sweep(args)
    with sweeplock.hold(sweeplock.SWEEP, sweeplock.SWEEP_WAIT_S,
                        detail="sync.py%s" % (" --only " + ",".join(args.only)
                                              if args.only else "")) as grant:
        if grant.contended:
            # Skipped, never silent: one line even under --quiet (that line IS the liveness
            # signal in the journal), and the progress record says why this hour did nothing.
            line = "%s ⏭ sweep skipped: %s" % (time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                               grant.reason())
            print(line, flush=True)
            write_progress(finished=True, finished_at=time.time(), skipped=grant.reason(),
                           pid=os.getpid())
            return 0
        if grant.error:
            # The guard itself is broken (the filesystem refused flock). The hourly sweep must
            # not be silenced by its own lock; Tender refuses in this state, so proceeding is safe.
            print("⚠ %s — sweeping unlocked; Tender refuses until this is fixed"
                  % grant.reason(), file=sys.stderr, flush=True)
        return _sweep(args)


def _sweep(args):
    # Liveness first: a sweep that later stalls must still have said it started, with the
    # launch latency it measured, so the tile can say "stalled at X" instead of nothing.
    latency = gitutil.calibrate()
    started_at = time.time()
    if not args.dry_run:
        write_progress(started_at=started_at, finished=False, aborted=None, repo=None,
                       pid=os.getpid(), launch_latency_ms=(None if latency is None
                                                           else int(latency * 1000)),
                       timeout_scale=gitutil.TIMEOUT_SCALE)
    global ACTIVE_WATCHDOG
    watchdog = Watchdog(SOFT_DEADLINE_S, HARD_DEADLINE_S).start() if not args.dry_run else None
    ACTIVE_WATCHDOG = watchdog

    # Pull the board BEFORE checking PAUSE. PAUSE is not gitignored, so committing it is the
    # documented way to halt every machine — but if the check came first, a paused remote
    # machine would never pull again and could never receive the commit that un-pauses it.
    # A one-way kill switch is a trap, not a safety rail. The pull is cheap and read-only.
    #
    # Fast-forward only (selfsync.receive): the sweep never stashes, in Lodestar or anywhere.
    if not args.dry_run:
        ok, note = selfsync.receive()
        if not ok:
            print("board pull: %s" % note)

    if os.path.exists(paths.pause_sentinel()):
        print("⏸ PAUSE sentinel present at %s — sweep halted. "
              "Remove it (or revert the commit that added it) to resume."
              % paths.pause_sentinel())
        if not args.dry_run:
            write_progress(finished=True, finished_at=time.time(), paused=True)
        return 0

    try:
        our_role = machine.role()
    except machine.UnknownMachine as error:
        print("🛑 %s" % error, file=sys.stderr)
        return 2

    # On a machine where `lsof` cannot see every process that might hold a lock (WSL, whose
    # repos sit on the Windows drive), a stale-looking lock is flagged, never deleted.
    gitutil.ALLOW_LOCK_REMOVAL = machine.lock_policy() != machine.LOCKS_FLAG

    def load_entries():
        rows = registry.participating(role=our_role, key=machine.key())
        if args.only:
            wanted = set(args.only)
            rows = [entry for entry in rows if entry.name in wanted]
        return rows

    entries = load_entries()
    if not entries:
        print("no participating repos in %s" % paths.registry_file(), file=sys.stderr)
        return 1

    # ~/.claude first, so the snapshot it writes is on disk before Lodestar itself is swept
    # and can be committed by this same run rather than waiting for the next one.
    #
    # This does not trap Lodestar in a permanent non-quiescent state, which was the obvious
    # hazard: shutil.copy2 preserves the source mtime, so re-copying an unchanged file
    # produces no git diff and no fresh timestamp. Only a genuinely changed file resets the
    # settle clock, which is exactly what should happen.
    results = []  # type: List[Result]
    if not args.only:
        # Guarded the way sweep() guards each repo, and for a demonstrated reason: an
        # unexpected exception in this step used to kill sync.main outright — nothing swept, no
        # SYNC.md, no heartbeat — and because SYNC.md's timestamp is the documented liveness
        # signal, it froze at its old value instead of reporting. A ~/.claude problem must cost
        # one flagged row, never the whole fleet's sync.
        try:
            results.append(claude_config_step(args.dry_run))
        except Exception as error:
            results.append(Result("~/.claude", claudeconf.CLAUDE_HOME, FLAGGED,
                                  "unexpected error: %s" % error, []))
        # 🔴 BEFORE cloning, deliberately, and the order is the whole point. A row whose
        # rename has not been applied here reads as absent to `clone_step`, so converging
        # afterwards would race a second checkout that the converger then refuses as
        # `both-present`. Completing the rename first is what makes the operation safe; the
        # `former_instance` guard inside `clone_step` is the belt to this braces.
        try:
            lifecycle_result = lifecycle_step(args.dry_run)
            if lifecycle_result is not None:
                results.append(lifecycle_result)
                # A converged rename changed a path, and since 2026-10-01 a converged REHOME
                # changed the row's declared `Remote` — so this reload is what keeps
                # `clone_step` from acting on the registry as it was before the converge.
                entries = load_entries()
        except Exception as error:
            results.append(Result("lifecycle", paths.root(), FLAGGED,
                                  "unexpected error: %s" % error, []))
        try:
            clone_result = clone_step(entries, args.dry_run, where=our_role)
            if clone_result is not None:
                results.append(clone_result)
                # An adoption rewrote a path in the registry; sweep the adopted checkout now.
                entries = load_entries()
        except Exception as error:
            results.append(Result("clones", os.path.expanduser("~/dev"), FLAGGED,
                                  "unexpected error: %s" % error, []))

    # 🔴 BEFORE sweep(), deliberately: a conversation record is classified sweepable, so one
    # written now is committed and pushed by this same pass. Written after the sweep it would
    # sit uncommitted for an hour — the exact gap this backstop exists to close. It runs even
    # under --only, unlike the three steps above, because `--only` narrows which repos are
    # visited and a record for a named repo is precisely what the operator asked for.
    if not args.only:
        try:
            mail_result = mail_reconcile_step(args.dry_run)
            if mail_result is not None:
                results.append(mail_result)
        except Exception as error:
            results.append(Result("mail-reconcile", _mail_dir(), FLAGGED,
                                  "unexpected error: %s" % error, []))
    try:
        conversations_result = conversations_step(entries, args.dry_run)
        if conversations_result is not None:
            results.append(conversations_result)
    except Exception as error:
        results.append(Result("conversations", paths.root(), FLAGGED,
                              "unexpected error: %s" % error, []))

    # One `now` for the whole sweep, so tiering and fetch ages are judged against a single
    # instant rather than drifting across a 26-repo pass.
    now = time.time()
    fetch_state = load_fetch_state()
    results.extend(sweep(entries, our_role, args.quiescence, args.dry_run,
                         fetch_state, now, watchdog))

    # GitHub Issues STATE refresh (owner, 2026-10-23): one pilot project per run, throttled,
    # pause-aware, AFTER the sweep so a clean tree lets the helper commit its export. Never
    # fatal: lib/issues_refresh.py swallows its own failures and this guard covers the rest.
    try:
        from lib import issues_refresh
        issues_line = issues_refresh.sweep_step(entries, args.dry_run, now)
        if issues_line:
            print(issues_line)
    except Exception as error:  # noqa: BLE001
        print("issues STATE: step failed, ignored: %s" % error, file=sys.stderr)

    # Tender receipts (design 2026-10-25 §5): journal, repair tickets, Companion summary, and
    # close tender-checked tickets. One ls-remote when nothing moved. Never fatal.
    try:
        from lib import tenderharvest
        tender_line = tenderharvest.sweep_step(args.dry_run)
        if tender_line:
            print(tender_line)
    except Exception as error:  # noqa: BLE001
        print("tender harvest: step failed, ignored: %s" % error, file=sys.stderr)

    # Repo mirrors (design 2026-10-19), AFTER the per-repo loop so they never block an origin
    # fetch/rebase/push and never alter a repo's outcome. Guarded like every other step.
    try:
        mirror_outcomes = mirrors.run(entries, our_role, machine.key(), dry_run=args.dry_run)
    except Exception as error:
        mirror_outcomes = [mirrors.Outcome("mirrors", "mirrors", "", mirrors.CONFIG_ERROR,
                                           "mirror step raised: %s" % error,
                                           surface=mirrors.FLAGGED)]

    # Freshness gate on the control repo (Lodestar itself), computed ONCE per sweep rather
    # than per repo: it is a property of this machine's own view of intent, not of any one
    # checkout. Read AFTER the sweep so a fetch this same sweep just performed on Lodestar is
    # reflected here rather than judged against a cache one sweep stale. REPORT-ONLY: see the
    # comment in `report()` for the phase that will make this refuse anything.
    try:
        behind, diverged = freshness.observed(paths.root())
        freshness_verdict = freshness.gate(now, last_fetch_of(fetch_state, "Lodestar"),
                                          behind, diverged)
    except Exception as error:  # a broken check must report, never crash the sweep
        freshness_verdict = freshness.Verdict(False, "freshness check failed: %s" % error)

    push_mismatches = push_url_mismatches(entries)

    # The registry's declared `Remote` against what each checkout here really points at. Same
    # discipline as every other reporter in this sweep: it must never take the sweep down, and
    # it changes nothing — see `declared_remote_audit`'s docstring for why it is a separate
    # question from `push_url_mismatches` and why it is report-only.
    # The error is carried INSIDE the audit rather than appended to `results`: a synthetic row
    # there would enter the All-repos table, the repo count and the flagged count, so a broken
    # reporter would change what the sweep says about the fleet. A reporter reports on itself.
    try:
        remote_audit = declared_remote_audit(entries)
    except Exception as error:
        remote_audit = RemoteAudit([], [], [], [], [], "audit failed, checked nothing: %s" % error)

    # Validated once per sweep, against the state as it stands AFTER any merge this sweep did.
    # Report-only in this phase; §3D of the design doc names it as the gate that must pass
    # before any transition executes. A broken validator must report, never crash the sweep —
    # the same discipline as the freshness check above.
    # Same discipline as the two checks above: a broken reporter must never take the sweep
    # down, and `enroll.read_stamp` is a git call per repo on a possibly slow drive.
    try:
        enrollment = enrollment_state(entries)
    except Exception:
        enrollment = ([], [], {})

    try:
        desired_state_faults = validate.faults()
    except Exception as error:
        desired_state_faults = [validate.Fault(
            "validator-failed", "validate.faults",
            "the desired-state validator raised and checked nothing: %s" % error)]

    # Written after the sweep and never on a dry run — a dry run reports, it never writes, and
    # recording a fetch it only *considered* would make the next real sweep skip a repo it had
    # never actually contacted. The whole dict is rewritten, so `--only` cannot forget the
    # timestamps of the repos it did not visit.
    if not args.dry_run:
        save_fetch_state(fetch_state)

    # One heartbeat per machine per HOUR, so the OTHER machine can tell "silent" from
    # "sweeping fine and finding nothing dirty" — a clean sweep commits nothing and SYNC.md is
    # machine-local by D-D (lib/heartbeat.py). Never on a dry run: a dry run reports, it never
    # writes. After the sweep, so the fresh file does not reset Lodestar's own quiescence
    # clock; the next-but-one sweep commits and pushes it like any other work.
    if not args.dry_run:
        heartbeat.emit(repos=len(results),
                       flagged=sum(1 for r in results if r.outcome == FLAGGED))
        # Unconditionally, NOT only when `emit` wrote — `emit` is at-most-once per hour while
        # this line's freshness is what tells a statusline the sweep is still running. Keyed
        # to this sweep completing, so a re-stamp every sweep is the point, not redundancy.
        heartbeat.write_line()

    text = report(results, our_role, args.dry_run,
                  registry.excluded(role=our_role, key=machine.key()),
                  registry.elsewhere(role=our_role, key=machine.key()),
                  freshness_verdict, push_mismatches, desired_state_faults,
                  enrollment, remote_audit, mirror_outcomes)
    with open(paths.sync_report_file(), "w", encoding="utf-8") as handle:
        handle.write(text)
    status = write_status(results, our_role, args.dry_run, freshness_verdict, push_mismatches,
                         desired_state_faults, enrollment, mirror_outcomes)

    # Counted BEFORE the branch, because both branches need them. They used to be computed
    # inside the `not --quiet` arm only, while the `--quiet` arm's last line reads `contended`
    # — so every quiet sweep died with UnboundLocalError *after* doing all of its real work,
    # and launchd runs this job with `--quiet`. The work landed and the exit code said failure,
    # which is the worst possible combination: nothing to notice in the board, a red exit in a
    # log nobody reads. Found 2026-09-30 by running a dry sweep by hand; 3661 passing tests
    # never touched it, because no test exercised the `--quiet` print path.
    flagged = sum(1 for r in results if r.outcome == FLAGGED)
    contended = sum(1 for r in results if r.outcome == CONTENDED)
    transient = sum(1 for r in results if r.outcome in (BUSY, OFFLINE))
    if watchdog is not None:
        watchdog.stop()
    if not args.dry_run:
        write_progress(finished=True, finished_at=time.time(), repo=None,
                       duration_s=int(time.time() - started_at))

    if not args.quiet:
        print("%s sweep as %s — %d repo(s)"
              % ("DRY RUN:" if args.dry_run else "Sync:", our_role, len(results)))
        for result in sorted(results, key=lambda r: (ORDER.get(r.outcome, 9), r.name)):
            print("  %s %-24s %s" % (ICON.get(result.outcome, "?"),
                                     result.name, result.detail))
        tiered = [r for r in results if r.tier]
        deferred = [r for r in tiered if not r.fetched]
        if tiered:
            print("\n  fetched %d of %d, deferred %d by tier "
                  "(hot %s / warm %s / cold %s) — local status ran on all %d"
                  % (len(tiered) - len(deferred), len(tiered), len(deferred),
                     _span(FETCH_INTERVAL[HOT]), _span(FETCH_INTERVAL[WARM]),
                     _span(FETCH_INTERVAL[COLD]), len(tiered)))
        if mirror_outcomes:
            print("\n  %s" % mirrors.summary_line(mirror_outcomes))
        print("\n  %s   ·   status: %s" % (paths.sync_report_file(), status))
        if flagged:
            print("  🚩 %d repo(s) need you" % flagged)
    else:
        # `--quiet` printed NOTHING, which made an empty log indistinguishable from "the sweep is
        # not running" — and on 2026-09-30 a board that had been unable to push for ~15 hours left
        # no trace whatsoever. The log it *did* carry was three weeks stale and untimestamped, and
        # was read as current. So: exactly one timestamped line per sweep, always. Silence now
        # means the sweep did not run.
        #
        # The board's own ahead/behind is on this line deliberately. `SYNC.md` is gitignored (so it
        # is invisible from any other machine and keeps no history), and the quiescence gate can
        # return SKIPPED for Lodestar before divergence is ever evaluated — the board writes
        # heartbeats continuously, so it is almost never quiescent. That combination is why a
        # diverged board reported nothing for fifteen hours. This does not change the gate; it just
        # makes the condition impossible to miss.
        print("%s sweep as %s — %d repo(s), %d flagged%s"
              % (time.strftime("%Y-%m-%dT%H:%M:%S%z"), our_role, len(results), flagged,
                 board_note(gitutil.ahead_behind(paths.root()))),
              flush=True)
        if contended:
            print("  🔓 %d repo(s) hit git lock contention" % contended)
        if transient:
            print("  ⏱ %d repo(s) busy/offline (transient, re-checked next sweep); took %ds, "
                  "launch latency %s" % (transient, int(time.time() - started_at),
                                         "?" if latency is None else "%d ms" % (latency * 1000)))
        if mirrors.flagged(mirror_outcomes):
            print("  🚩 %s" % mirrors.summary_line(mirror_outcomes))

    return 0


if __name__ == "__main__":
    sys.exit(main())
