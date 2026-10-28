"""Parse and format the short durations loops carry (`90m`, `2h`, `1h30m`).

Kept separate because three things need it: the board's "just do it now" flag, the
actual_spent rollup, and eventually a timesheet helper's arithmetic.

Unparseable text returns None rather than raising — `est_close` is hand-typed, and a typo
should show up as "unset" on the board rather than crashing a render.
"""

import re
from typing import Optional

_PATTERN = re.compile(
    r"^\s*(?:(?P<h>\d+(?:\.\d+)?)\s*h)?\s*(?:(?P<m>\d+(?:\.\d+)?)\s*m)?\s*$",
    re.IGNORECASE,
)

# Ritual section 3: anything at or under this gets flagged "just do it now", to defeat
# the tiny-task-avoidance trap.
JUST_DO_IT_MINUTES = 2

#: Sentinel for "deliberately never recorded" (T4 ruling, 2026-10-14, question card "Yes, mark
#: them 'unknown'" — docs/superpowers/specs/2026-10-14-project-tracking-system-design.md).
#: Replaces the old `0m` placeholder `loops.upsert`/`loops.derive` used to seed every new loop,
#: which conflated "nobody logged this" with "this took no time at all" — 48 of 71 loop files
#: carried it as of the measurement that prompted the ruling. `minutes()` returns None for this
#: value, same as for any other unparseable text, so it is never summed, averaged, or divided
#: into a ratio as if it were zero.
UNKNOWN = "unknown"


def is_unknown(text: Optional[str]) -> bool:
    """True for the explicit not-recorded sentinel (case/whitespace insensitive)."""
    return bool(text) and text.strip().lower() == UNKNOWN


def minutes(text: Optional[str]) -> Optional[int]:
    """Return whole minutes, or None if `text` is empty, the "unknown" sentinel, or unparseable."""
    if not text:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    if is_unknown(stripped):
        return None

    # A bare number is read as minutes, which is how people actually type it.
    if re.match(r"^\d+$", stripped):
        return int(stripped)

    match = _PATTERN.match(stripped)
    if not match or not (match.group("h") or match.group("m")):
        return None
    total = 0.0
    if match.group("h"):
        total += float(match.group("h")) * 60
    if match.group("m"):
        total += float(match.group("m"))
    return int(round(total))


def format_minutes(total: Optional[int]) -> str:
    """Render minutes back to compact form: 90 -> 1h30m, 45 -> 45m, 120 -> 2h."""
    if total is None:
        return ""
    if total < 60:
        return "%dm" % total
    hours, remainder = divmod(total, 60)
    if remainder == 0:
        return "%dh" % hours
    return "%dh%dm" % (hours, remainder)


def is_just_do_it(text: Optional[str]) -> bool:
    value = minutes(text)
    return value is not None and value <= JUST_DO_IT_MINUTES
