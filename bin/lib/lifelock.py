"""One machine-local lock per repo id — the mutual exclusion §3D.3 of the repo-lifecycle
design calls for: "One machine-local lock, held outside the movable directory... Keyed on
**id**, never on the old path — a path-keyed lock fails across the rename it is guarding."

The hazard this closes is an hourly sweep overlapping a manual converger run on THIS
machine for THIS repo. It is not a distributed lock, and two machines each moving their own
checkout is correct and needs no coordination between them (same framing as
`paths.lifecycle_lock_file`'s own docstring, which this module's default `path` resolves
through — read it there rather than restated here).

**Why this reuses `attend.py`'s shape instead of `import attend`.** `attend` answers a
different question — is a *human* here right now — and its lease carries a `last_click`
that means nothing for a converger. But the mechanism underneath is identical and already
proven: a live holder is honoured, a dead one is reclaimed, `O_EXCL` IS the exclusion (not a
check-then-write racing itself), writes are atomic (`os.replace`), and an unreadable lock
file refuses on no evidence rather than guessing the holder is gone. Re-deriving that from
scratch would just re-introduce whatever `attend` already debugged. `attend.pid_alive` is
imported directly because liveness-of-a-pid is not a lease concept, it is an OS fact.

**The lock is keyed on `repo_id`, and nothing here accepts a repo directory as an input to
identity.** The only path this module ever sees is where the *lock file itself* lives — by
default `paths.lifecycle_lock_file(repo_id)`, a pure function of the id, and in tests an
explicit override so a test run never touches `~/dev/.lodestar/locks/`. Neither is the
checkout's own path. A rename moves the checkout; it must not move, release, or orphan the
lock, because the rename is exactly the operation the lock exists to guard while it is
in flight. `tests/test_lifelock.py` proves this by renaming a directory that is never
passed to any function here and showing the same lock is still held across it.

**`held()` releases in a `finally`, not after a clean return.** A converger that raises
mid-move (a failed `os.rename`, a bad precondition caught late) must not leave the lock
held — that wedges every later sweep for that repo until a human deletes the file by hand,
which is the exact failure `attend.py` calls out for its own lease. Losing the lock on an
exception is therefore not a nicety; it is the reason this exists as a context manager
rather than a bare `acquire`/`release` pair callers might forget to pair up on the
error path.
"""

import contextlib
import errno
import json
import os
from datetime import datetime, timezone
from typing import Callable, Dict, Iterator, NamedTuple, Optional, Tuple

from . import paths
from .attend import pid_alive


class Busy(Exception):
    """Raised by `held()` when the lock could not be acquired. Its message is `acquire()`'s
    own `reason`, so it always names the holding pid rather than refusing anonymously."""


class Held(NamedTuple):
    repo_id: str
    pid: int
    machine: str
    at: str

    def as_dict(self) -> Dict:
        return {"repo_id": self.repo_id, "pid": self.pid, "machine": self.machine,
                "at": self.at}


def _lock_path(repo_id: str, path: Optional[str]) -> str:
    return path or paths.lifecycle_lock_file(repo_id)


def _this_machine() -> str:
    try:
        from . import machine
        return machine.key()
    except Exception:
        return ""


def _serialise(entry: Held) -> str:
    return json.dumps(entry.as_dict(), indent=2, sort_keys=True) + "\n"


def _load(path: str) -> Tuple[Optional[Held], bool]:
    """(holder, readable). A missing file is readable-and-empty; a corrupt one is not
    readable — the same two-valued shape as `attend._load`, for the same reason: "no lock"
    and "cannot tell" must never collapse into one branch."""
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
    return Held(repo_id=data.get("repo_id") or "", pid=pid, machine=data.get("machine") or "",
                at=data.get("at") or ""), True


def _write_exclusive(path: str, entry: Held) -> bool:
    """Create the lock or lose the race. `O_EXCL` is the mutual exclusion itself, not a
    convenience — a check-then-create pair would have the exact race it exists to close."""
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
        out.write(_serialise(entry))
    return True


def _replace(path: str, entry: Held) -> None:
    """Atomic overwrite, so a reader never observes a half-written lock file."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w") as out:
        out.write(_serialise(entry))
    os.replace(tmp, path)


def read(repo_id: str, *, path: Optional[str] = None) -> Optional[Held]:
    """The current holder for `repo_id`, or None when there is none or it cannot be parsed."""
    return _load(_lock_path(repo_id, path))[0]


def acquire(repo_id: str, *, path: Optional[str] = None, pid: Optional[int] = None,
            machine: Optional[str] = None,
            now: Optional[datetime] = None,
            alive: Optional[Callable[[int], bool]] = None) -> Dict:
    """Take the lock for `repo_id` if it is free or its holder is no longer running.

    Returns a dict with:
      - `ok`: whether this process now holds it.
      - `holder`: the `Held` now in force — ours on success, the live blocker's on refusal,
        `None` only when the lock file could not even be read.
      - `reclaimed`: a dead holder's lock was overwritten to grant this one.
      - `reason`: names the pid and machine on every refusal — never an anonymous "busy",
        because a refusal that does not name the holder pushes whoever reads it to kill a
        process blind (`attend.judge_takeover`'s reasoning, reused here verbatim).
    """
    target = _lock_path(repo_id, path)
    our_pid = int(pid if pid is not None else os.getpid())
    clock = now or datetime.now(timezone.utc)
    liveness = alive or pid_alive

    mine = Held(repo_id=repo_id, pid=our_pid, machine=machine or _this_machine(),
                at=clock.isoformat())

    existing, readable = _load(target)
    if existing is None and readable and _write_exclusive(target, mine):
        return {"ok": True, "holder": mine, "reclaimed": False, "reason": "took the lock"}

    # Either something is there, or a racing acquire created one between the load and the
    # write above. Reload and judge against current reality rather than the stale read.
    existing, readable = _load(target)

    if not readable:
        return {"ok": False, "holder": None, "reclaimed": False,
                "reason": "the lifecycle lock at %s exists but could not be read, so it "
                          "cannot be shown that no other process holds it for repo %r. "
                          "Refusing on no evidence — delete that file by hand if nothing is "
                          "actually running against this repo" % (target, repo_id)}

    if existing is not None and existing.pid == our_pid:
        return {"ok": True, "holder": existing, "reclaimed": False,
                "reason": "this process already holds the lock"}

    if existing is not None and liveness(existing.pid):
        return {"ok": False, "holder": existing, "reclaimed": False,
                "reason": "pid %s on %s holds the lifecycle lock for %r and is still "
                          "running — refusing rather than acting on the same repo id twice "
                          "at once" % (existing.pid, existing.machine or "an unnamed machine",
                                       repo_id)}

    reclaimed = existing is not None
    reason = ("reclaimed the lock from pid %s, which is no longer running" % existing.pid
              if reclaimed else "took the lock")
    _replace(target, mine)
    return {"ok": True, "holder": mine, "reclaimed": reclaimed, "reason": reason}


def release(repo_id: str, *, path: Optional[str] = None, pid: Optional[int] = None) -> bool:
    """Give up the lock. True when this process held it and it is now gone."""
    target = _lock_path(repo_id, path)
    our_pid = int(pid if pid is not None else os.getpid())
    existing = read(repo_id, path=target)
    if existing is None or existing.pid != our_pid:
        return False
    try:
        os.remove(target)
    except FileNotFoundError:
        return False
    return True


@contextlib.contextmanager
def held(repo_id: str, *, path: Optional[str] = None, pid: Optional[int] = None,
         machine: Optional[str] = None, now: Optional[datetime] = None,
         alive: Optional[Callable[[int], bool]] = None) -> Iterator[Held]:
    """Acquire for `repo_id`, yield the `Held`, and release even when the body raises.

    Raises `Busy` (message = `acquire()`'s own `reason`) when the lock is not obtained —
    including the unreadable-lock case, which is a refusal on no evidence, not a "someone
    else has it" busy in the ordinary sense, but is surfaced the same way because the
    caller's correct move is identical: do not proceed.
    """
    our_pid = int(pid if pid is not None else os.getpid())
    got = acquire(repo_id, path=path, pid=our_pid, machine=machine, now=now, alive=alive)
    if not got["ok"]:
        raise Busy(got["reason"])
    try:
        yield got["holder"]
    finally:
        release(repo_id, path=path, pid=our_pid)
