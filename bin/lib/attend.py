"""Is a human actually here — and is anyone else already holding the door?

`bin/execute.py` carries the comment "INTERACTIVE ONLY — never run from launchd", and a comment
cannot enforce anything. A sidecar left open overnight is a running process with a live path to
Graph mutations and nobody at the screen; every check that comment can make passes. So this
module turns *interactive* into a mechanism, by deriving it from a recent human click rather than
from the fact that a process exists.

**Attendance is a recent click, not a running process.** `attending()` refuses when no click has
ever landed on this lease, and refuses again once the last one has aged past
`CLICK_WINDOW_SECONDS`. The two refusals read differently on purpose: a lease that has never been
clicked is a sidecar nobody is using, and one whose click aged out is a session he walked away
from. Conflating them hides which of the two happened.

**One interactive session at a time, and the loser still serves.** Two sidecars consuming the
same accepted bubbles is a double-send — the worst failure available in this design, because
`mail.reply` and `calendar.accept` have no inverse once they reach a person. So the lease is
created with `O_EXCL` and a second caller is granted READONLY: it serves the page with every
bubble disabled and the holding pid named. Refusing to start at all would push him to kill the
first process blind, which is how someone loses the session they were using.

**A live holder is honoured; a dead one is reclaimed.** That asymmetry is the whole content of
the exclusion. Nothing else distinguishes "someone is working" from "a process died holding the
door shut", and without the reclaim a crash would wedge the surface until a file was deleted by
hand.

**An unreadable lease is the one case that refuses on no evidence.** If the file exists but will
not parse, the holder cannot be identified and therefore cannot be proved gone — so this serves
read-only and names the path to delete. Writes here are atomic (`os.replace`), so a truncated
lease should be impossible; the branch exists because the alternative is guessing that nobody is
there, and that guess sends mail.

The lease lives at `~/dev/.lodestar/command.lease`, **outside the repo**. `gitutil.SOURCE_DIRS`
is `("docs",)`, so a lock-shaped file under `airlock/` would be committed and pushed within
~127s and propagate this machine's pid to the other one — the same shape as the `.git/index.lock`
wedge of 2026-08-31.
"""

import errno
import json
import os
from datetime import datetime, timezone
from typing import Callable, Dict, NamedTuple, Optional, Tuple

from . import paths

#: How recently a human must have clicked for execution to be allowed. Fifteen minutes is long
#: enough to read a bubble's basis and short enough that a session he walked away from stops
#: acting before the meeting he walked into has ended.
CLICK_WINDOW_SECONDS = 900

#: This process may execute (subject to attendance).
INTERACTIVE = "interactive"
#: Somebody else holds the lease, or it cannot be read. Serve the page, disable every bubble.
READONLY = "readonly"


class NotHolding(Exception):
    """Raised when a process that does not hold the lease tries to write to it."""


class Lease(NamedTuple):
    pid: int
    machine: str
    started: str
    last_click: Optional[str] = None

    def as_dict(self) -> Dict:
        return {"pid": self.pid, "machine": self.machine, "started": self.started,
                "last_click": self.last_click}


def lease_path() -> str:
    return paths.command_lease_file()


def pid_alive(pid) -> bool:
    """Is that process running? False for nonsense rather than raising, because the caller's
    next move on an unreadable pid is the same as on a dead one."""
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return False
    if number <= 0:
        return False
    try:
        os.kill(number, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _parse(stamp: Optional[str]) -> Optional[datetime]:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def judge_attendance(last_click: Optional[str], *,
                     now: Optional[datetime] = None) -> Dict:
    """Does this click count as a human being here? Pure — no file, no process table.

    Returns `ok`, `seconds` (age of the click, None when there wasn't one) and a `reason` that
    names the age, because a refusal with no number cannot be told apart from a bug.
    """
    clock = now or datetime.now(timezone.utc)

    if last_click is None:
        return {"ok": False, "seconds": None,
                "reason": "no click has ever landed on this lease, so nothing here is "
                          "interactive — a sidecar started and left alone must not act. Click "
                          "a bubble on the board and it becomes attended"}

    moment = _parse(last_click)
    if moment is None:
        return {"ok": False, "seconds": None,
                "reason": "the last click stamp %r is unreadable, so attendance cannot be "
                          "established. Click anything on the board to write a fresh one"
                          % (last_click,)}

    age = (clock - moment).total_seconds()
    if age < 0:
        return {"ok": False, "seconds": age,
                "reason": "the last click is stamped %.0fs in the future, so the clocks "
                          "disagree and the age cannot be trusted. Refusing rather than "
                          "treating skew as attendance" % (-age,)}
    if age > CLICK_WINDOW_SECONDS:
        return {"ok": False, "seconds": age,
                "reason": "the last click was %.0fs ago, past the %ds attendance window — this "
                          "session was left open rather than being used. Click anything on the "
                          "board and it is attended again" % (age, CLICK_WINDOW_SECONDS)}
    return {"ok": True, "seconds": age,
            "reason": "clicked %.0fs ago, inside the %ds window" % (age, CLICK_WINDOW_SECONDS)}


def judge_takeover(existing: Optional[Lease], *, our_pid: int, alive: bool,
                   readable: bool, path: str = "") -> Dict:
    """May this process hold the lease? Pure — `alive` and `readable` are facts, not lookups.

    Returns `mode`, `reclaim` (the existing lease must be overwritten), `holder_pid` and a
    `reason` that names who is holding it, so a READONLY page never asks him to kill something
    unnamed.
    """
    if not readable:
        return {"mode": READONLY, "reclaim": False, "holder_pid": None,
                "reason": "the lease at %s exists but could not be read, so it cannot be shown "
                          "that no other session is holding it. Serving read-only. Delete that "
                          "file if no other board window is open" % (path or lease_path())}

    if existing is None:
        return {"mode": INTERACTIVE, "reclaim": False, "holder_pid": None,
                "reason": "no session was holding the lease"}

    if existing.pid == our_pid:
        return {"mode": INTERACTIVE, "reclaim": False, "holder_pid": our_pid,
                "reason": "this process already holds the lease"}

    if alive:
        return {"mode": READONLY, "reclaim": False, "holder_pid": existing.pid,
                "reason": "pid %s on %s is holding the command lease and is still running, so "
                          "this window serves read-only with every bubble disabled. Two sessions "
                          "acting on the same accepted bubbles would send twice"
                          % (existing.pid, existing.machine or "an unnamed machine")}

    return {"mode": INTERACTIVE, "reclaim": True, "holder_pid": existing.pid,
            "reason": "reclaimed the lease from pid %s, which is no longer running"
                      % (existing.pid,)}


def _load(path: str) -> Tuple[Optional[Lease], bool]:
    """(lease, readable). A missing file is readable-and-empty; a corrupt one is not readable."""
    try:
        with open(path) as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None, True
    except (ValueError, OSError):
        return None, False
    if not isinstance(data, dict) or "pid" not in data:
        return None, False
    try:
        pid = int(data["pid"])
    except (TypeError, ValueError):
        return None, False
    return Lease(pid=pid, machine=data.get("machine") or "",
                 started=data.get("started") or "",
                 last_click=data.get("last_click")), True


def read(path: Optional[str] = None) -> Optional[Lease]:
    """The lease, or None when there is none or it cannot be parsed."""
    return _load(path or lease_path())[0]


def _serialise(lease: Lease) -> str:
    return json.dumps(lease.as_dict(), indent=2, sort_keys=True) + "\n"


def _write_exclusive(path: str, lease: Lease) -> bool:
    """Create the lease or lose the race. `O_EXCL` is the mutual exclusion itself."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            return False
        raise
    with os.fdopen(handle, "w") as out:
        out.write(_serialise(lease))
    return True


def _replace(path: str, lease: Lease) -> None:
    """Atomic overwrite, so a reader never sees a half-written lease."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w") as out:
        out.write(_serialise(lease))
    os.replace(tmp, path)


def acquire(path: Optional[str] = None, *, pid: Optional[int] = None,
            machine: Optional[str] = None, now: Optional[datetime] = None,
            alive: Optional[Callable[[int], bool]] = None) -> Dict:
    """Take the lease if it is free or stale. Returns `mode`, `lease`, `holder_pid`, `reason`.

    A READONLY result is a working page, not a failure: every bubble is disabled and the bar
    names the holder. The caller must not treat it as a reason to exit.
    """
    target = path or lease_path()
    our_pid = int(pid if pid is not None else os.getpid())
    clock = now or datetime.now(timezone.utc)
    liveness = alive or pid_alive

    mine = Lease(pid=our_pid, machine=machine or _this_machine(),
                 started=clock.isoformat(), last_click=None)

    existing, readable = _load(target)
    if existing is None and readable and _write_exclusive(target, mine):
        return {"mode": INTERACTIVE, "lease": mine, "holder_pid": our_pid,
                "reason": "took the lease"}

    # Either something is there, or a racing process created one between the load and the write.
    existing, readable = _load(target)
    verdict = judge_takeover(existing, our_pid=our_pid,
                             alive=bool(existing) and liveness(existing.pid),
                             readable=readable, path=target)

    if verdict["mode"] != INTERACTIVE:
        return {"mode": READONLY, "lease": existing, "holder_pid": verdict["holder_pid"],
                "reason": verdict["reason"]}

    if existing is not None and existing.pid == our_pid:
        return {"mode": INTERACTIVE, "lease": existing, "holder_pid": our_pid,
                "reason": verdict["reason"]}

    # Reclaiming a dead holder's lease deliberately drops its `last_click`: inheriting it would
    # grant this process attendance that no human gave it.
    _replace(target, mine)
    return {"mode": INTERACTIVE, "lease": mine, "holder_pid": our_pid,
            "reason": verdict["reason"]}


def click(path: Optional[str] = None, *, pid: Optional[int] = None,
          now: Optional[datetime] = None) -> Lease:
    """Record that a human just clicked. Only the holder may — otherwise a second sidecar could
    keep the first one's worker attended by proxy."""
    target = path or lease_path()
    our_pid = int(pid if pid is not None else os.getpid())
    existing = read(target)
    if existing is None:
        raise NotHolding("no lease at %s — acquire it before recording a click" % target)
    if existing.pid != our_pid:
        raise NotHolding("pid %s holds the lease, not %s, so this click is not yours to record"
                         % (existing.pid, our_pid))
    updated = existing._replace(last_click=(now or datetime.now(timezone.utc)).isoformat())
    _replace(target, updated)
    return updated


def attending(path: Optional[str] = None, *, pid: Optional[int] = None,
              now: Optional[datetime] = None) -> Dict:
    """May this process execute right now? Holding the lease AND a recent click."""
    target = path or lease_path()
    our_pid = int(pid if pid is not None else os.getpid())
    existing = read(target)
    if existing is None:
        return {"ok": False, "seconds": None,
                "reason": "no lease is held at %s, so this process cannot show it is the "
                          "interactive session" % target}
    if existing.pid != our_pid:
        return {"ok": False, "seconds": None,
                "reason": "pid %s holds the lease, not %s — only the holder may execute"
                          % (existing.pid, our_pid)}
    return judge_attendance(existing.last_click, now=now)


def release(path: Optional[str] = None, *, pid: Optional[int] = None) -> bool:
    """Give up the lease. True when this process held it and it is now gone."""
    target = path or lease_path()
    our_pid = int(pid if pid is not None else os.getpid())
    existing = read(target)
    if existing is None or existing.pid != our_pid:
        return False
    try:
        os.remove(target)
    except FileNotFoundError:
        return False
    return True


def _this_machine() -> str:
    try:
        from . import machine
        return machine.key()
    except Exception:
        return ""
