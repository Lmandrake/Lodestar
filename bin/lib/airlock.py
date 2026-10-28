"""Validate what Cowork deposits in the airlock.

Two schemas, two shapes, for one reason: captures are records a human may skim and Kairos
may read, so they are markdown with strict frontmatter and a loose body; requests are
imperative batches executed unattended, so they are line-oriented with per-line failure
isolation (the grammar of a fetch-request tool that has run thousands of directives).

Nothing here mutates or moves a file. A bad capture stays where Cowork put it and is
reported, so there is never anything to roll back.

Two different questions live here, and conflating them is a mistake this module has
already made once. `validate_capture` / `validate_request` ask "is this FILE well formed?"
`gate_groups` asks "was every window attempted on this DATE eventually sensed?" — a day
whose 06:00 run found a dead connector and whose 09:38 retry succeeded is a good day, even
though one of its files honestly reports `unreachable`.

See docs/superpowers/specs/2026-08-31-lodestar-phase-2-airlock-design.md.
"""

import os
import re
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Tuple

from . import clock, frontmatter, mdtable

ERROR = "error"
WARNING = "warning"
# A third level, deliberately not an error. A capture that honestly reports it could not
# reach M365 is CORRECT output — Cowork did the right thing — so failing it would make the
# validator red on correct input, which is how a validator teaches you to ignore it. What
# `unreachable` must be is loud and counted, because a disengaged connector and a quiet day
# otherwise produce byte-identical captures.
#
# This level is a per-FILE observation and nothing more. Whether a DAY is acceptable is a
# different question with a different answer — see `gate_groups` — because a window sensed
# on the second attempt has an `unreachable` capture in it and is still a good day.
UNREACHABLE = "unreachable"

CAPTURE_KINDS = ("calendar", "mail-triage", "wam-input", "teams-chat", "slack")
# Still 1, deliberately. D17 adds an OPTIONAL `bodies` key and an optional region below the
# records table; every pre-D17 capture is still valid and every body-bearing capture is
# readable by anything that already reads schema 1 (the bodies sit below the table, so
# mdtable stops before them). Bumping would fork the doc and make Cowork's next mistake
# "wrong schema number" for no gain — a version number earns its keep when it changes how
# an EXISTING field must be read, and nothing here does that.
SCHEMA_VERSIONS = (1,)
STATUSES = ("sensed", "unreachable")
# `bodies` is deliberately NOT here. Absent means "no bodies", which is what every capture
# written before D17 means, and what a triage with nothing worth quoting still means.
REQUIRED_KEYS = ("kind", "schema", "captured", "machine", "runtime", "status", "records")

# <kind>--<role>--<HHMMSS>.md. Seconds, not minutes: a retried twice-daily sense inside the
# same minute would otherwise silently overwrite its earlier attempt (decision D5). The role
# segment is decision D-I — Phase 1 bug 7 was two machines sharing one path.
CAPTURE_NAME = re.compile(
    r"^(?P<kind>[a-z][a-z-]*)--(?P<role>[a-z0-9-]+)--(?P<time>\d{6})\.md$")
DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WINDOW = re.compile(r"^\d{4}-\d{2}-\d{2}\.\.\d{4}-\d{2}-\d{2}$")
ISO_WEEK = re.compile(r"^\d{4}-W\d{2}$")
# clock.parse deliberately tolerates a naive timestamp (it assumes local) because journal
# replay must never crash on one bad event. Captures cross machines, so here an absent
# offset is genuinely ambiguous and must fail — hence a separate check on the raw text.
OFFSET = re.compile(r"(Z|[+-]\d{2}:?\d{2})$")

# ------------------------------------------------------------------------------------------
# The bodies region (decision D17), and why it is shaped like this
# ------------------------------------------------------------------------------------------
# D17 lets `mail-triage` carry the FULL text of any message it flags as needing Lukas, because
# every deadline judgement was previously guessed from a subject line. That text is written by
# outsiders, and D16 separately allows an LLM into the unattended path, so DECISIONS.md's
# composition warning applies to exactly this data. The containment therefore has to hold
# against a message body that is actively trying to look like the capture.
#
# So bodies are NOT fenced. A fence has a closing delimiter, a closing delimiter is a string,
# and any string can appear inside an email — `---`, ```` ``` ````, `-->`, `</body>`. Every
# fence is forgeable by its own contents. Instead the region is **quoted line by line**:
#
#   ## BODIES                      <- opens the region, once, at column 0
#   ### BODY 1 - who, "subject"    <- one block per flagged row, numbered 1..records
#   > every line of the message    <- untrusted text, and it may never leave column 1
#
# Two consequences, and they are the whole design:
#
# 1. **No line of a message body can appear at column 0**, so no line of a message body can
#    be read as frontmatter (`---`), as a table row (`| … |`), as a heading, or as a block
#    delimiter. Containment is per-line, so there is nothing to break out OF.
# 2. **The region has no closing delimiter and runs to end of file.** That is not laziness:
#    an "end of bodies" marker would be the one forgeable string in the scheme. The cost is
#    that nothing may follow the region, which the check below enforces.
#
# The `records` cross-check then runs on the text ABOVE the marker only, so a table a sponsor
# forwarded cannot change the number the capture's own claim is compared against.
BODIES_MARKER = re.compile(r"^##\s+BODIES\b")
BODY_OPENER = re.compile(r"^###\s+BODY\s+(?P<index>\d+)\b")
BODY_QUOTE = ">"
# mail-triage, teams-chat and slack all carry message TEXT, so all three have a reason to
# quote full bodies the same way (decision D17's reasoning: a deadline judgement guessed from
# a subject line/preview is a guess). calendar and wam-input carry structured rows, not prose
# worth quoting in full, so they stay off this list — an allowlist grown ahead of a consumer
# is not a safety feature, the same argument as VERBS below.
BODIES_KINDS = ("mail-triage", "teams-chat", "slack")


class Problem(NamedTuple):
    path: str
    line: Optional[int]
    level: str
    message: str


def validate_capture(relpath: str, text: str, roles: Iterable[str]) -> List[Problem]:
    """Check one capture. `relpath` is repo-relative; `roles` are the known machine roles."""
    problems = []  # type: List[Problem]

    def fail(message: str) -> None:
        problems.append(Problem(relpath, None, ERROR, message))

    def warn(message: str) -> None:
        problems.append(Problem(relpath, None, WARNING, message))

    directory, name = os.path.split(relpath)
    day = os.path.basename(directory)

    name_match = CAPTURE_NAME.match(name)
    if not name_match:
        fail("filename must be <kind>--<role>--<HHMMSS>.md, got %r" % name)
    if not DATE_DIR.match(day):
        fail("captures must live in captures/<YYYY-MM-DD>/, got %r" % day)

    try:
        fields, body = frontmatter.parse(text)
    except ValueError as exc:
        fail("frontmatter: %s" % exc)
        return problems
    if not fields:
        fail("no frontmatter block — a capture must declare its schema")
        return problems

    for key in REQUIRED_KEYS:
        if key not in fields:
            fail("missing required key %r" % key)

    kind = fields.get("kind")
    if kind is not None and kind not in CAPTURE_KINDS:
        fail("unknown kind %r — allowed: %s" % (kind, ", ".join(CAPTURE_KINDS)))
    if name_match and kind is not None and name_match.group("kind") != kind:
        fail("filename says kind %r but frontmatter says %r"
             % (name_match.group("kind"), kind))

    raw_schema = fields.get("schema")
    if raw_schema is not None:
        try:
            if int(raw_schema) not in SCHEMA_VERSIONS:
                raise ValueError
        except ValueError:
            fail("unknown schema version %r — this validator understands: %s"
                 % (raw_schema, ", ".join(str(v) for v in SCHEMA_VERSIONS)))

    status = fields.get("status")
    if status is not None and status not in STATUSES:
        fail("unknown status %r — allowed: %s" % (status, ", ".join(STATUSES)))

    known = list(roles)
    role = fields.get("machine")
    if role is not None and role not in known:
        fail("machine %r is not a role in machines.md — known: %s"
             % (role, ", ".join(sorted(known))))

    records = None  # type: Optional[int]
    raw_records = fields.get("records")
    if raw_records is not None:
        try:
            records = int(raw_records)
            if records < 0:
                raise ValueError
        except ValueError:
            fail("records must be a non-negative integer, got %r" % raw_records)
            records = None

    captured = fields.get("captured")
    if captured is not None:
        try:
            stamp = clock.parse(captured)
        except ValueError:
            fail("captured is not an ISO 8601 timestamp: %r" % captured)
        else:
            if not OFFSET.search(captured.strip()):
                fail("captured must carry a UTC offset (got %r) — captures cross machines, "
                     "so a bare local time is ambiguous" % captured)
            if DATE_DIR.match(day) and stamp.strftime("%Y-%m-%d") != day:
                fail("folder says %s but captured is %s"
                     % (day, stamp.strftime("%Y-%m-%d")))

    if kind in ("calendar", "mail-triage", "teams-chat", "slack"):
        window = fields.get("window")
        if window is None:
            fail("%s captures require a 'window' key" % kind)
        elif not WINDOW.match(window):
            fail("window must be YYYY-MM-DD..YYYY-MM-DD (inclusive), got %r" % window)
    elif kind == "wam-input":
        week = fields.get("week")
        if week is None:
            fail("wam-input captures require a 'week' key")
        elif not ISO_WEEK.match(week):
            fail("week must be an ISO week like 2026-W36, got %r" % week)

    # Split before the records cross-check, not after: everything below the `## BODIES`
    # marker is outsider-authored, so it must not be able to reach the count.
    trusted, marker_index, region = split_bodies(body)
    problems.extend(_check_bodies(relpath, fields, kind, status, records,
                                  marker_index, region, _body_line(text)))

    if status == "unreachable":
        # Valid, and deliberately NOT an error — an honest "I could not look" is correct
        # output. But it must never be mistakable for a quiet day, so it gets its own level
        # and its own count. records is meaningless here, so the cross-check is skipped.
        problems.append(Problem(
            relpath, None, UNREACHABLE,
            "reported the source unreachable — this window was NOT sensed, and 'records' "
            "does not mean zero results"))
    elif records is not None:
        rows = mdtable.parse(trusted)
        if rows:
            if len(rows) != records:
                fail("records: %d but the body table has %d rows" % (records, len(rows)))
        elif records > 0 and not body.strip():
            warn("records: %d but the body is empty" % records)
        # `records: 0` with a prose body is CORRECT and must not warn. The Task 1 smoke
        # test settled this: the right shape for a zero capture is `records: 0` plus a
        # sentence saying nothing was sensed. Warning there would fire on the expected
        # case and teach the reader to ignore warnings.

    return problems


def split_bodies(body: str) -> Tuple[str, Optional[int], List[str]]:
    """Split a capture body at the `## BODIES` marker.

    Returns (trusted text above the marker, 0-based line index of the marker or None, the
    region's lines after it). With no marker the whole body is trusted and the region is
    empty, which is exactly the pre-D17 behaviour — that is what keeps a body-less capture
    validating unchanged.

    Only the FIRST marker counts. A second one further down is not a second region: it is an
    unquoted line inside the first, which `_check_bodies` reports. Untrusted text does not get
    to reopen the boundary that contains it.
    """
    lines = body.split("\n")
    for index, line in enumerate(lines):
        if BODIES_MARKER.match(line):
            return "\n".join(lines[:index]), index, lines[index + 1:]
    return body, None, []


def _body_line(text: str) -> Optional[int]:
    """The file line number (1-based) of the first line below the frontmatter.

    Errors inside a bodies region name a line, because a capture with bodies is long enough
    that "somewhere in this file" is not a usable report.
    """
    if not text.startswith(frontmatter.DELIMITER + "\n"):
        return None
    lines = text.split("\n")
    for index in range(1, len(lines)):
        if lines[index].strip() == frontmatter.DELIMITER:
            return index + 2
    return None


def _clip(line: str, limit: int = 60) -> str:
    line = line.strip()
    return line if len(line) <= limit else line[:limit] + "…"


def _check_bodies(relpath: str, fields: Dict[str, str], kind: Optional[str],
                  status: Optional[str], records: Optional[int],
                  marker_index: Optional[int], region: List[str],
                  body_line: Optional[int]) -> List[Problem]:
    """Check the D17 bodies region: its declared count, its blocks, and its quoting.

    The quoting check is the one that matters. Everything else here is bookkeeping; that one
    is the boundary between text Lodestar wrote and text a stranger wrote.
    """
    problems = []  # type: List[Problem]

    def at(offset: Optional[int]) -> Optional[int]:
        if body_line is None or offset is None:
            return None
        return body_line + offset

    def fail(message: str, offset: Optional[int] = None) -> None:
        problems.append(Problem(relpath, at(offset), ERROR, message))

    raw = fields.get("bodies")
    declared = None  # type: Optional[int]
    if raw is not None:
        try:
            declared = int(raw)
            if declared < 0:
                raise ValueError
        except ValueError:
            fail("bodies must be a non-negative integer, got %r" % raw)
            declared = None

    if marker_index is None:
        if declared:
            fail("bodies: %d but the body has no '## BODIES' region. Either write the "
                 "region or drop the key — a count with nothing behind it is the shape a "
                 "truncated capture takes." % declared)
        return problems

    if kind is not None and kind not in BODIES_KINDS:
        fail("only %s captures may carry a '## BODIES' region, and this one is %r"
             % (", ".join(BODIES_KINDS), kind), marker_index)
    if status == "unreachable":
        fail("an unreachable capture cannot carry message bodies — you did not read "
             "anything. Drop the region and say what went wrong in prose.", marker_index)
    if raw is None:
        fail("a '## BODIES' region requires a 'bodies' key saying how many blocks it holds, "
             "for the same reason 'records' exists: a count you claimed can be checked, a "
             "count a reader infers cannot.", marker_index)

    blocks = []  # type: List[Tuple[int, int]]
    for number, line in enumerate(region):
        offset = marker_index + 1 + number
        opener = BODY_OPENER.match(line)
        if opener:
            blocks.append((int(opener.group("index")), offset))
        elif line.strip() and not line.startswith(BODY_QUOTE):
            fail("every line below '## BODIES' must be a '### BODY <n>' header or a line of "
                 "message text quoted with '>' — this one is neither: %r. Unquoted, a body "
                 "line sits in the capture's own structure, where it can pass for "
                 "frontmatter, a table row, or a delimiter." % _clip(line), offset)

    if not blocks:
        fail("a '## BODIES' region with no '### BODY <n>' block in it", marker_index)
    elif declared is not None and len(blocks) != declared:
        fail("bodies: %d but the region holds %d '### BODY <n>' block(s)"
             % (declared, len(blocks)), marker_index)

    expected = 1
    for index, offset in blocks:
        if index != expected:
            fail("body blocks are numbered 1, 2, 3 … in table order — expected "
                 "'### BODY %d', got '### BODY %d'" % (expected, index), offset)
            expected = index + 1
            continue
        expected += 1
        # This is how a body stays tied to its row without living inside the table: block N
        # IS row N. So the table remains the countable thing and `records` keeps its meaning.
        if records is not None and status != "unreachable" and index > records:
            fail("'### BODY %d' names row %d but the table has only %d row(s)"
                 % (index, index, records), offset)
    return problems


def window_covers(have: str, want: str) -> bool:
    """Does window `have` contain all of window `want`? CONTAINMENT, not equality — and the
    difference is a real day's behaviour either way:

      * Too strict (equality) re-collects work already done. Measured 2026-09-02: the day's
        successful mail capture covered `2026-08-26..2026-09-02` while the daily default asked
        for `2026-08-30..2026-09-02`. Every span the default wanted was already in hand, and an
        equality test would have seized the screen to fetch it again.
      * Too loose (any sensed capture counts, kind-and-date alone) declares the day done while
        blind — the failure this project guards hardest against. Measured 2026-09-02 the other
        direction: that day's captures directory held three `unreachable` captures for the
        forward window the schedule exists to collect, alongside three `sensed` hand-run August
        backfill captures. A kind-and-date test answered "calendar is done today" while the
        forward window had never once been collected.

    Containment is the test that gets both right: a wider capture satisfies a narrower ask, and
    a disjoint or narrower one does not.

    This is the ONE rule shared by two callers that must never disagree: `sense.sensed_today`
    (a single capture's window against the one the live scheduler wants, right now) and
    `gate_groups` below (a whole date's captures, letting a wider one cover a narrower one's
    own gate group). They used to be two separate implementations, and the gap between them was
    measured directly on 2026-09-03: five `unreachable` stand-ins for the scheduled calendar
    window `2026-09-03..2026-09-17` sat in their own exact-match group and failed the gate,
    while a same-day, same-kind `sensed` capture for `2026-09-03..2026-10-02` — a wider mail-dump
    backfill — already covered every day of it. `sensed_today`, which the live scheduler
    actually consults before deciding whether to re-attempt, answered "sensed" for that exact
    narrow window; the gate answered "never sensed" for the identical question. Two readers of
    the same captures must not disagree, which is why both now call this one function instead of
    keeping their own copy of the rule.

    ISO dates compare correctly as strings, which is why they are stored this way. A window
    that fails `WINDOW`'s START..END shape (an ISO week like wam-input's, or a malformed or
    empty string) answers False on either side — a span that cannot be read as a range
    cannot be claimed to cover, or be covered by, anything.
    """
    if not have or not want or not WINDOW.match(have) or not WINDOW.match(want):
        return False
    have_start, have_end = have.split("..")
    want_start, want_end = want.split("..")
    return have_start <= want_start and have_end >= want_end


# --------------------------------------------------------------------------------------------
# Source freshness: distinct from "sensed at all"
# --------------------------------------------------------------------------------------------
# `source_as_of` (bin/lib/maildump.py) is the newest mtime among the files a capture actually
# read. `captured` is when the capture was WRITTEN. A frozen file dump (a dead M365 token, a
# stopped connector) keeps producing a fresh `captured` every day while `source_as_of` never
# moves — that is the exact incident this closes: five days of `status: sensed` calendar
# captures, gate PASSED every one of them, built from the same Sep-3 files. The gap between
# the two fields IS the staleness; this is where it gets measured.
#
# Per-kind, because the two kinds that carry this field tolerate old inputs very differently:
#   calendar     — a forward-looking WINDOW. A day-old snapshot has already missed same-day
#                  reschedules and new invites, so the grace is tight: one day.
#   mail-triage  — a transactional log of what already arrived. A dump that ran a day late is
#                  still an honest, complete list of yesterday's mail, so the grace is wider.
# `wam-input` carries no `source_as_of` at all (Cowork writes it directly, never through the
# file dump) and has no entry here on purpose — see `source_freshness`.
#   teams-chat   — same shape as calendar's forward risk: a stale snapshot has already missed
#                  same-day messages, so the grace matches calendar's, not mail-triage's wider
#                  one — chat is scheduled at the same cadence as calendar, not continuous.
#   slack        — same reasoning and the same number as teams-chat.
SOURCE_STALE_AFTER = {
    "calendar": 24 * 3600,
    "mail-triage": 48 * 3600,
    "teams-chat": 24 * 3600,
    "slack": 24 * 3600,
}

FRESH = "fresh"
STALE_SOURCE = "stale"
# Absence must never read as fresh — this project's single most repeated bug (a positive
# signal's absence mistaken for a negative finding). Every capture written before this field
# existed, and every capture Cowork's own GUI-driven sensing writes (it reads M365 live and
# has no file-mtime to report), lacks `source_as_of` and always will. Both cases mean "cannot
# judge", which is its own state — never silently folded into FRESH (that would resurrect
# exactly the incident this closes) and never folded into STALE_SOURCE either (that would fail
# the gate on every ordinary Cowork-sensed capture that ever existed).
UNKNOWN_SOURCE = "unknown"


def source_freshness(fields: Dict[str, str]) -> Tuple[str, Optional[float]]:
    """How current were the files THIS capture was built from, at the moment it was written?

    Returns (state, lag_seconds):

      * `UNKNOWN_SOURCE`, `None` — no `source_as_of` (or it is empty/unparseable), or `kind`
        has no registered threshold. Nothing here entitles a claim of "fresh" or "stale".
      * `STALE_SOURCE`, lag — both timestamps parse and `captured - source_as_of` exceeds this
        kind's `SOURCE_STALE_AFTER` threshold.
      * `FRESH`, lag — both parse and the gap is within threshold.

    `lag_seconds` is floored at 0: a dump file with an mtime slightly ahead of `captured`
    (clock skew between the machine that ran the dump and the one that wrote the capture) is
    not evidence of NEGATIVE staleness, just noise — same reasoning as `format_age`'s floor.
    """
    threshold = SOURCE_STALE_AFTER.get(fields.get("kind"))
    raw_source = fields.get("source_as_of")
    raw_captured = fields.get("captured")
    if threshold is None or not raw_source or not raw_captured:
        return UNKNOWN_SOURCE, None
    try:
        source_ts = clock.parse(raw_source)
        captured_ts = clock.parse(raw_captured)
    except (ValueError, TypeError):
        return UNKNOWN_SOURCE, None
    lag = (captured_ts - source_ts).total_seconds()
    if lag < 0:
        lag = 0.0
    return (STALE_SOURCE if lag > threshold else FRESH), lag


class GateGroup(NamedTuple):
    """One window that sensing attempted on a given date, and how the attempts went."""
    kind: str
    window: str
    total: int
    sensed: int
    unreachable: int
    # True when some OTHER sensed capture of the same kind, on the same date, is a wider
    # window that contains this one — see `window_covers`. Deliberately excludes this
    # group's own `sensed` count, which `passed` already tests directly; this field exists
    # so `passed` can also say yes to a window nobody attempted under its own exact name.
    covered_by_wider: bool = False
    # How many SENSED captures in this group have `source_freshness(...) == STALE_SOURCE`.
    # Never counts UNKNOWN_SOURCE ones — an ordinary Cowork-sensed capture, which never
    # carries `source_as_of`, must not contribute here (see `source_freshness`'s docstring).
    stale_sources: int = 0
    # The largest lag among this group's stale captures, for a human-readable detail message.
    # None whenever stale_sources is 0.
    max_lag_seconds: Optional[float] = None

    @property
    def passed(self) -> bool:
        """Sensed at least once, or contained by a wider same-kind capture that was.

        WHICH capture supplied the coverage does not matter, exactly as WHICH attempt
        succeeded does not matter for the exact-match case below. Equality here would
        make this disagree with `sense_lib.sensed_today` on the same captures — see
        `window_covers`.
        """
        return self.sensed > 0 or self.covered_by_wider

    @property
    def recovered(self) -> bool:
        """Sensed, but not on the first try — worth saying out loud rather than hiding."""
        return self.sensed > 0 and self.unreachable > 0

    @property
    def source_stale(self) -> bool:
        """Sensed, but at least one sensed capture read files that were already old.

        Deliberately independent of `passed`: a group can pass (something was sensed) and
        still be describing stale reality. This is what keeps a frozen file dump from
        reading as a clean day just because `captured` refreshed on schedule.
        """
        return self.stale_sources > 0


def gate_groups(captures: Iterable[Tuple[str, str]]) -> List[GateGroup]:
    """Group one date's captures by (kind, window-or-week) for the gate check.

    `captures` is (relpath, text) pairs. The grouping exists because sensing is a
    two-stage affair in practice: a 06:00 unattended run, then a retry once the M365
    connector has been brought back. That day holds an `unreachable` capture AND a
    `sensed` capture for the same kind and window, and the honest verdict is that the
    window was sensed. A group therefore passes on ANY sensed capture, and the failed
    first attempt is reported as recovery rather than as failure.

    A group ALSO passes when it never has a sensed capture of its own but a wider,
    same-kind capture elsewhere that date does cover it (`window_covers`) — the exact
    (kind, window) bucketing below still separates them for reporting (so the wider
    capture's own group is counted and printed on its own line), but `GateGroup.passed`
    reads across buckets of the same kind to answer the coverage question honestly.

    Captures whose frontmatter will not parse form no group: `validate_capture` already
    errors on them, and the gate fails on any error, so inventing a phantom group would
    double-report and would key it on a made-up kind.

    Each group also carries `stale_sources`/`max_lag_seconds` (see `source_freshness`): a
    group can pass (something was sensed) while every sensed capture in it read files that
    were already old. `GateGroup.source_stale` is the reader's hook for that — passed and
    stale are orthogonal, on purpose, so a caller can tell "never sensed" apart from "sensed,
    but from days-old inputs" instead of collapsing both into one failure.
    """
    buckets = {}  # type: Dict[Tuple[str, str], List[Any]]
    sensed_windows = {}  # type: Dict[str, set]
    for _, text in captures:
        try:
            fields, _body = frontmatter.parse(text)
        except ValueError:
            continue
        if not fields:
            continue
        kind = fields.get("kind") or "?"
        # The per-kind key IS the window identity: `window` for calendar/mail-triage,
        # `week` for wam-input. Nothing else distinguishes two attempts at the same scope.
        window = fields.get("window") or fields.get("week") or "?"
        counts = buckets.setdefault((kind, window), [0, 0, 0, 0, None])
        counts[0] += 1
        if fields.get("status") == "sensed":
            counts[1] += 1
            sensed_windows.setdefault(kind, set()).add(window)
            # Only a SENSED capture's own inputs can be judged stale — an unreachable one
            # read nothing, so it has no `source_as_of` claim worth grading.
            state, lag = source_freshness(fields)
            if state == STALE_SOURCE:
                counts[3] += 1
                if counts[4] is None or lag > counts[4]:
                    counts[4] = lag
        elif fields.get("status") == "unreachable":
            counts[2] += 1
    groups = []
    for (kind, window), c in sorted(buckets.items()):
        covered = any(window_covers(other, window)
                      for other in sensed_windows.get(kind, ()) if other != window)
        groups.append(GateGroup(kind, window, c[0], c[1], c[2], covered, c[3], c[4]))
    return groups


# Deliberately two verbs. Unattended CC acts only on what is in this allowlist
# (RUNTIME_BRIDGE.md rail 5), so an allowlist grown ahead of an actual consumer is not a
# safety feature. Phase 3 adds entries one at a time, each justified by a consumer.
# There is no loop verb on purpose: Cowork may write loops/ directly, and a second path to
# the same place is a second thing to keep safe. (Decision D-J.)
VERBS = {"INGEST": 1, "TRANSCRIPT": 1}

REQUEST_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}_[A-Za-z0-9._-]+\.txt$")


def validate_request(relpath: str, text: str, repo_root: str) -> List[Problem]:
    """Check one request file, line by line.

    A malformed line fails alone and the rest of the file proceeds. All-or-nothing sounds
    safer but is not: one typo would block a whole batch, and the pressure that creates is
    to loosen the parser — which costs the allowlist.
    """
    problems = []  # type: List[Problem]

    name = os.path.basename(relpath)
    if not REQUEST_NAME.match(name):
        problems.append(Problem(relpath, None, ERROR,
                                "filename must be <YYYY-MM-DD>_<topic>.txt, got %r" % name))

    for number, raw in enumerate(text.split("\n"), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        verb = parts[0].rstrip(":").upper()
        args = parts[1:]

        if verb not in VERBS:
            problems.append(Problem(
                relpath, number, ERROR,
                "unknown verb %r — allowed: %s" % (parts[0], ", ".join(sorted(VERBS)))))
            continue

        expected = VERBS[verb]
        if len(args) != expected:
            problems.append(Problem(
                relpath, number, ERROR,
                "%s takes %d argument(s), got %d" % (verb, expected, len(args))))
            continue

        problems.extend(_check_contained(relpath, number, args[0], repo_root))

    return problems


def _check_contained(relpath: str, line: int, arg: str, repo_root: str) -> List[Problem]:
    """A path argument must resolve inside the repo and must exist.

    realpath, not a prefix test: a symlink inside the repo pointing at /etc would pass a
    naive string comparison. This is rail 5's bounded blast radius, enforced at the cheapest
    possible point — validation, not execution.
    """
    candidate = arg if os.path.isabs(arg) else os.path.join(repo_root, arg)
    resolved = os.path.realpath(candidate)
    anchor = os.path.realpath(repo_root)
    if resolved != anchor and not resolved.startswith(anchor + os.sep):
        return [Problem(relpath, line, ERROR, "path escapes the repo: %s" % arg)]
    if not os.path.exists(resolved):
        return [Problem(relpath, line, ERROR, "path does not exist: %s" % arg)]
    return []


def validate_machines(text: str) -> List[Problem]:
    """Roles must be unique: capture filenames and `machine` validation both rely on it."""
    problems = []  # type: List[Problem]
    seen = {}  # type: Dict[str, str]
    for row in mdtable.parse(text):
        role = (row.get("role") or "").strip()
        key = (row.get("key") or "?").strip()
        if not role:
            continue
        if role in seen:
            problems.append(Problem(
                "machines.md", None, ERROR,
                "role %r is used by both %r and %r; capture filenames require unique roles"
                % (role, seen[role], key)))
        else:
            seen[role] = key
    return problems
