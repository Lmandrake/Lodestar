#!/usr/bin/env python3
"""The one machine-wide lock between the sync sweep and Tender (2026-10-26).

    python3 bin/lib/sweeplock.py status
    python3 bin/lib/sweeplock.py run --as tender --wait 120 --detail tdr-... -- CMD ...

Design: docs/design/2026-10-25-remote-execution.md §4 ("Lock against the sweep"). Before this,
nothing a second process could take existed: `sync.py` had a progress file, a watchdog and
per-repo pauses, but a Tender recipe (rewrite recovery, a git-state repair) could run while the
sweep was fast-forwarding, committing or pushing the very checkout it was repairing.

**Mechanism: `flock` on `<state-dir>/sweep.lock`, through `flockwait`'s bounded wait.** One
mechanism, not two: the kernel releases a `flock` when its holder dies, so a crashed sweep, a
watchdog `os._exit`, a SIGKILLed Tender or a systemd/launchd timeout can never leave a stale lock
behind; there is nothing to reclaim and no pid to trust. Measured on a WSL machine: `flock`
on `~/dev/.lodestar` (drvfs, 9p) excludes a second WSL process and is released when the holder is
killed. Python opens files non-inheritable (PEP 446), so a git child cannot keep the lock alive.

**The holder record is evidence, never authority.** After taking the lock the holder writes
`{"role", "pid", "detail", "since", "host"}` into the file so a waiter can say *who* it waited for.
It is read only while the lock is really held; a record left by a dead holder in a free file is
reported as "free (last holder ... exited)", never as held.

**Both directions, deliberately asymmetric when the lock itself is broken:**

- the sweep waits `SWEEP_WAIT_S`, then skips this run and says so (`sync.py`); if `flock` itself
  errors (not contention: the filesystem refused), the sweep proceeds unlocked with a loud note,
  because the hourly sweep must never be silenced by its own guard;
- Tender waits `TENDER_WAIT_S`, then refuses (the directive stays pending for its next tick); on a
  lock *error* it refuses too. Fail-open for one side and fail-closed for the other is still safe
  as a pair: when the lock cannot be taken, Tender does nothing.

**Inheritance, for Tender's `restart-sweep` recipe.** A holder may run a child that itself takes
the lock (Tender running `bin/sync.py` directly on WSL). The holder passes `child_env()`; the child
sees `LODESTAR_SWEEPLOCK_HELD=<pid>`, confirms the lock is held and the record names that pid, and
runs under its parent's lock instead of deadlocking against it for five minutes.

Stdlib only and 3.9-compatible, and imports nothing from Lodestar but `flockwait` (with a plain
import fallback), so Tender's bootloader can vendor this file and `flockwait.py` beside it and
never depend on the checkout. The path rule mirrors `paths.state_dir()` (tested to agree).
"""

import contextlib
import errno
import fcntl
import json
import os
import socket
import sys
import time
from typing import Callable, Dict, Iterator, List, Optional

try:
    from . import flockwait
except ImportError:  # vendored beside flockwait.py, or run as a script
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import flockwait  # type: ignore

SWEEP = "sweep"
TENDER = "tender"
ROLES = (SWEEP, TENDER)

#: The sweep waits this long for a Tender run, then skips. Bounded well inside systemd's
#: TimeoutStartSec=62min (HARD_DEADLINE_S 55 min + this wait + slack) and the sweep's own 40-minute soft deadline.
SWEEP_WAIT_S = 5 * 60
#: Tender waits this long for a sweep, then refuses until its next 15-minute tick.
TENDER_WAIT_S = 2 * 60

PATH_ENV = "LODESTAR_SWEEP_LOCK"
HELD_ENV = "LODESTAR_SWEEPLOCK_HELD"
_DEFAULT_STATE = "~/dev/.lodestar"


def lock_path() -> str:
    """`$LODESTAR_SWEEP_LOCK`, else `sweep.lock` in the same state dir `paths.state_dir()` uses."""
    explicit = os.environ.get(PATH_ENV)
    if explicit:
        return os.path.expanduser(explicit)
    state = os.environ.get("LODESTAR_STATE_DIR") or _DEFAULT_STATE
    return os.path.join(os.path.expanduser(state), "sweep.lock")


def pid_alive(pid) -> bool:
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


def read_record(path: Optional[str] = None) -> Optional[Dict]:
    """The last holder record in the file, or None. Evidence only: see the module docstring."""
    try:
        with open(path or lock_path(), encoding="utf-8") as handle:
            text = handle.read().strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        record = json.loads(text)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def describe(record: Optional[Dict], now: Optional[float] = None) -> str:
    """'tender (tdr-x) pid 123 for 4m' — the words a waiter prints about a holder."""
    if not record:
        return "an unidentified process (no holder record)"
    now = time.time() if now is None else now
    since = record.get("since")
    age = ""
    if isinstance(since, (int, float)):
        minutes = max(0, int((now - since) // 60))
        age = " for %dm" % minutes
    detail = (" (%s)" % record["detail"]) if record.get("detail") else ""
    return "%s%s pid %s%s" % (record.get("role", "?"), detail, record.get("pid", "?"), age)


class Grant(object):
    """What `hold()` yields. Truthy only when this process may proceed under the lock."""

    def __init__(self, role: str, path: str, acquired: bool = False, inherited: bool = False,
                 holder: Optional[Dict] = None, waited_s: float = 0.0, error: str = ""):
        self.role, self.path = role, path
        self.acquired, self.inherited = acquired, inherited
        self.holder, self.waited_s, self.error = holder, waited_s, error

    def __bool__(self) -> bool:
        return self.acquired

    @property
    def contended(self) -> bool:
        """Another holder kept it past our wait (as opposed to the lock mechanism failing)."""
        return not self.acquired and not self.error

    def reason(self) -> str:
        if self.acquired:
            return "held by %s%s" % (self.role, " (inherited from parent)" if self.inherited else "")
        if self.error:
            return "sweep lock unusable at %s: %s" % (self.path, self.error)
        return "sweep lock held by %s; waited %ds" % (describe(self.holder), int(self.waited_s))


def _write_record(handle, record: Dict) -> None:
    handle.seek(0)
    handle.truncate(0)
    handle.write(json.dumps(record, sort_keys=True) + "\n")
    handle.flush()


def _inherited(role: str, path: str) -> Optional[Grant]:
    """A parent that holds the lock passed it down via `child_env()`: honour it, carefully.

    Honoured only when the lock is genuinely held right now AND the record names exactly the pid
    the environment names. A leftover variable in a shell, or a parent that has since exited,
    therefore falls through to an ordinary acquire instead of running unlocked.
    """
    claimed = os.environ.get(HELD_ENV, "").strip()
    if not claimed:
        return None
    record = read_record(path)
    if not record or str(record.get("pid")) != claimed or not pid_alive(claimed):
        return None
    try:
        with open(path, "a+", encoding="utf-8") as probe:
            try:
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return Grant(role, path, acquired=True, inherited=True, holder=record)
            fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
    except OSError:
        return None
    return None  # free: the claimed parent no longer holds it


def child_env(base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Environment for a child that must run under THIS process's lock (e.g. Tender -> sync.py)."""
    env = dict(os.environ if base is None else base)
    env[HELD_ENV] = str(os.getpid())
    return env


@contextlib.contextmanager
def hold(role: str, wait: float, detail: str = "", path: Optional[str] = None,
         poll: float = 0.5, clock: Callable[[], float] = time.monotonic,
         sleep: Callable[[float], None] = time.sleep) -> Iterator[Grant]:
    """Take the machine-wide sweep lock as `role`, waiting at most `wait` seconds.

    Always yields a `Grant`; never raises for contention or a broken filesystem, so each caller
    states its own policy (`sync.py` skips or proceeds, Tender refuses). Released in a `finally`,
    and by the kernel if the process dies first.
    """
    if role not in ROLES:
        raise ValueError("sweep lock role must be one of %s, not %r" % (ROLES, role))
    path = path or lock_path()
    inherited = _inherited(role, path)
    if inherited is not None:
        yield inherited
        return
    started = clock()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        handle = open(path, "a+", encoding="utf-8")
    except OSError as exc:
        yield Grant(role, path, error="cannot open: %s" % exc)
        return
    try:
        try:
            flockwait.exclusive(handle, label=path, timeout=wait, poll=poll,
                                clock=clock, sleep=sleep)
        except flockwait.LockWaitTimeout:
            yield Grant(role, path, holder=read_record(path), waited_s=clock() - started)
            return
        except OSError as exc:  # not contention (flockwait polls that): the filesystem refused
            yield Grant(role, path, error="flock failed: %s" % exc)
            return
        record = {"role": role, "pid": os.getpid(), "detail": detail, "since": time.time(),
                  "host": socket.gethostname()}
        try:
            _write_record(handle, record)
        except OSError:
            pass  # the record is evidence; the flock is the lock
        try:
            yield Grant(role, path, acquired=True, holder=record, waited_s=clock() - started)
        finally:
            try:
                handle.seek(0)
                handle.truncate(0)
            except OSError:
                pass
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        handle.close()


def status(path: Optional[str] = None) -> Dict:
    """{'state': 'free'|'held'|'error', 'holder': record|None, 'note': str}. Read-only in effect:
    it probes with a non-blocking flock and lets go at once."""
    path = path or lock_path()
    record = read_record(path)
    if not os.path.exists(path):
        return {"state": "free", "holder": None, "path": path, "note": "never taken here"}
    try:
        with open(path, "a+", encoding="utf-8") as probe:
            try:
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held = True
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                    held = True
                else:
                    return {"state": "error", "holder": record, "path": path,
                            "note": "flock failed: %s" % exc}
            else:
                held = False
                fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        return {"state": "error", "holder": record, "path": path, "note": "cannot open: %s" % exc}
    if held:
        note = "held by %s" % describe(record)
        if record and record.get("pid") and not pid_alive(record["pid"]):
            note += ("; that pid is not alive in this namespace, so an inherited descriptor or "
                     "another PID namespace holds it (lsof %s)" % path)
        return {"state": "held", "holder": record, "path": path, "note": note}
    note = "free"
    if record:  # a holder that died never ran its `finally`; the kernel released the flock
        note = "free (last holder %s exited without clearing its record)" % describe(record)
    return {"state": "free", "holder": record, "path": path, "note": note}


def _cli(argv: Optional[List[str]] = None) -> int:
    import argparse
    import subprocess
    parser = argparse.ArgumentParser(description="The machine-wide sweep/Tender lock.")
    sub = parser.add_subparsers(dest="cmd")
    s = sub.add_parser("status", help="say whether the lock is held, and by whom")
    s.add_argument("--json", action="store_true")
    r = sub.add_parser("run", help="run a command while holding the lock")
    r.add_argument("--as", dest="role", choices=ROLES, required=True)
    r.add_argument("--wait", type=float, default=None,
                   help="seconds to wait (default: the role's own: sweep %d, tender %d)"
                        % (SWEEP_WAIT_S, TENDER_WAIT_S))
    r.add_argument("--detail", default="")
    r.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.cmd == "status":
        info = status()
        print(json.dumps(info, sort_keys=True) if args.json else "%s: %s" % (info["path"],
                                                                            info["note"]))
        return 0 if info["state"] != "error" else 2
    if args.cmd == "run":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command:
            parser.error("run needs a command after --")
        wait = args.wait if args.wait is not None else (
            SWEEP_WAIT_S if args.role == SWEEP else TENDER_WAIT_S)
        with hold(args.role, wait, detail=args.detail) as grant:
            if not grant:
                print("refused: %s" % grant.reason(), file=sys.stderr)
                return 75  # EX_TEMPFAIL: try again next tick
            return subprocess.call(command, env=child_env())
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(_cli())
