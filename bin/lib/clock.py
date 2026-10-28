"""Timestamps, in one place.

Everything Lodestar writes is timestamped, and the journal is *ordered* by those
timestamps, so a single formatting/parsing pair matters more than it looks.

Local time with an explicit UTC offset is deliberate: the board is read by a human
who thinks in local time, and the offset keeps ordering correct across DST and across
machines in different zones.
"""

import datetime
from typing import Optional


def now(timespec: str = "seconds") -> str:
    """Current local time as ISO 8601 with offset, e.g. 2026-08-31T11:45:02-07:00."""
    return datetime.datetime.now().astimezone().isoformat(timespec=timespec)


def parse(text: str) -> datetime.datetime:
    """Parse an ISO 8601 timestamp written by `now`.

    Naive timestamps are assumed to be local, so a hand-edited file missing its
    offset still sorts sensibly instead of raising.
    """
    parsed = datetime.datetime.fromisoformat(text.strip())
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed


def sort_key(text: Optional[str]) -> datetime.datetime:
    """Ordering key that tolerates a missing or unparseable timestamp.

    Journal replay must never crash on one malformed event; such events sort to the
    beginning, where they are harmless, rather than aborting the rebuild.
    """
    if not text:
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    try:
        return parse(text)
    except ValueError:
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


def today() -> str:
    """Local date as YYYY-MM-DD, used for the journal's daily directory."""
    return datetime.date.today().isoformat()


def epoch() -> int:
    """Whole seconds since the epoch, used to prefix journal filenames."""
    return int(datetime.datetime.now().timestamp())
