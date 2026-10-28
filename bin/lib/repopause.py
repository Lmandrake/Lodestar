"""Per-repo pause, with an expiry and a quiet-gated auto-resume.

`PAUSE` at the Lodestar root halts every sweep on a machine. This is the narrow version: one
repo, for a while, so a project agent doing heavy work can say "leave me alone for eight
hours" without anyone editing the shared registry.

**Where it lives.** `<git-dir>/lodestar-pause`, a small JSON file inside the repo's own git
directory. So it is machine-local by construction, is never committed, and never dirties the
tree — a pause cannot itself become sweep traffic.

    {"until": ISO, "reason": str, "set_by": str, "set_at": ISO,
     "directions": ["send", "receive"]}

**Expiry is gated on quiet.** When `until` passes, the pause lifts only if the repo has been
quiet for `QUIET_WINDOW` — no dirty path modified, and no new commit on HEAD, in that time.
If it is still active the pause extends itself by one window and says so. That is the point:
an agent that asked for 8h and is still mid-edit at hour 8 should not be swept mid-thought.

**It cannot pause forever.** However many extensions, a pause never outlives
`set_at + HARD_CAP`. At the cap it lifts regardless and the sweep reports it loudly, so a
forgotten pause surfaces within a day instead of silently turning a repo off.
"""

import datetime
import json
import os
import time
from typing import Any, Dict, List, NamedTuple, Optional, Sequence

from . import gitutil

PAUSE_FILE = "lodestar-pause"
QUIET_WINDOW = 60 * 60
HARD_CAP = 24 * 60 * 60
DIRECTIONS = ("send", "receive")


class PauseState(NamedTuple):
    directions: Sequence[str]   # empty when not paused
    detail: str                 # what to say in the report; "" when nothing to say
    loud: bool = False          # the hard cap lifted it: report as needs-you


NOT_PAUSED = PauseState((), "")


def _iso(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def _epoch(text: str) -> Optional[float]:
    try:
        return datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def pause_path(repo: str) -> Optional[str]:
    directory = gitutil.git_dir(repo)
    if not directory:
        return None
    return os.path.join(directory, PAUSE_FILE)


def read(repo: str) -> Optional[Dict[str, Any]]:
    """The pause record, or None. An unreadable file reads as None — see `evaluate`."""
    path = pause_path(repo)
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _write(repo: str, payload: Dict[str, Any]) -> str:
    path = pause_path(repo)
    if not path:
        raise ValueError("%s is not a git repo" % repo)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)
    return path


def set_pause(repo: str, seconds: int, reason: str = "", set_by: str = "",
              directions: Sequence[str] = DIRECTIONS,
              now: Optional[float] = None) -> Dict[str, Any]:
    """Pause `repo` for `seconds` (capped at HARD_CAP). Replaces any existing pause."""
    stamp = time.time() if now is None else now
    seconds = max(60, min(int(seconds), HARD_CAP))
    wanted = [d for d in DIRECTIONS if d in directions] or list(DIRECTIONS)
    payload = {
        "until": _iso(stamp + seconds),
        "reason": reason,
        "set_by": set_by,
        "set_at": _iso(stamp),
        "directions": wanted,
    }
    _write(repo, payload)
    return payload


def clear(repo: str) -> bool:
    path = pause_path(repo)
    if not path or not os.path.exists(path):
        return False
    os.unlink(path)
    return True


def last_activity(repo: str) -> Optional[float]:
    """Newest of: any dirty path's mtime, and HEAD's commit time."""
    stamps = [value for value in (gitutil.newest_mtime(repo), gitutil.last_commit_time(repo))
              if value is not None]
    return max(stamps) if stamps else None


def _describe(payload: Dict[str, Any], directions: List[str]) -> str:
    reason = payload.get("reason") or ""
    who = payload.get("set_by") or ""
    return "paused (%s) until %s%s%s" % (
        ", ".join(directions), payload.get("until", "?"),
        " — %s" % reason if reason else "",
        " [%s]" % who if who else "")


def evaluate(repo: str, now: Optional[float] = None, dry_run: bool = False,
             quiet_window: int = QUIET_WINDOW, hard_cap: int = HARD_CAP) -> PauseState:
    """Is `repo` paused right now? Applies expiry, extension and the hard cap.

    Writes (extends or clears the file) unless `dry_run`. A malformed file is reported and
    treated as paused — the author meant *something*, and guessing "not paused" would sweep
    a repo an agent asked to be left alone.
    """
    stamp = time.time() if now is None else now
    path = pause_path(repo)
    if not path or not os.path.exists(path):
        return NOT_PAUSED
    payload = read(repo)
    if payload is None:
        return PauseState(DIRECTIONS, "pause file %s is unreadable — treated as paused; "
                                      "fix or remove it (sync.py --resume)" % path)

    directions = [d for d in DIRECTIONS if d in (payload.get("directions") or DIRECTIONS)]
    until = _epoch(payload.get("until", ""))
    set_at = _epoch(payload.get("set_at", "")) or stamp
    if until is None:
        return PauseState(directions or DIRECTIONS,
                          "pause file has no readable `until` — treated as paused")

    if stamp < until:
        return PauseState(directions, _describe(payload, directions))

    cap = set_at + hard_cap
    if stamp >= cap:
        if not dry_run:
            clear(repo)
        return PauseState((), "pause force-lifted: it reached the %dh cap (set %s by %s, "
                              "reason %r) and the repo is being swept again"
                          % (hard_cap // 3600, payload.get("set_at", "?"),
                             payload.get("set_by") or "unknown",
                             payload.get("reason") or ""), loud=True)

    active = last_activity(repo)
    if active is not None and stamp - active < quiet_window:
        new_until = min(stamp + quiet_window, cap)
        payload["until"] = _iso(new_until)
        if not dry_run:
            _write(repo, payload)
        return PauseState(directions, "pause extended (active) — last activity %dm ago; "
                                      "%s" % (int((stamp - active) // 60),
                                              _describe(payload, directions)))

    if not dry_run:
        clear(repo)
    return PauseState((), "pause expired and the repo is quiet — resumed")
