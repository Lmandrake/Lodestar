"""A bounded exclusive `flock` (2026-10-15).

Every read-modify-write lock that Bearing's cycle takes (`bearingpending`, `bearingextract`'s
queue, `priority`'s items file, `reckoning`'s outbox) used a bare blocking
`fcntl.flock(LOCK_EX)`: if any other process held one of those files and stalled, the waiter
slept for ever with no CPU, no child and no socket, which is exactly the signature of the
2026-10-15 08:25 Bearing hang. Waiting is still right (the holders are short); waiting for ever
is not. `exclusive` polls a non-blocking lock and raises `LockWaitTimeout` after `timeout`
seconds, so a stuck peer becomes a logged error in the caller instead of a silent wedge.
"""

import fcntl
import time
from typing import Callable

#: Long enough for any honest holder (they do a JSON read-modify-write), far below Bearing's
#: 15-minute stall watchdog, so a stuck peer surfaces as this error and not as a kill.
WAIT_S = 300.0
POLL_S = 0.1


class LockWaitTimeout(TimeoutError):
    """Another process held the lock longer than `timeout`."""


def exclusive(handle, label: str = "", timeout: float = WAIT_S, poll: float = POLL_S,
              clock: Callable[[], float] = time.monotonic,
              sleep: Callable[[float], None] = time.sleep) -> None:
    """Take LOCK_EX on `handle`, waiting at most `timeout` seconds."""
    deadline = clock() + timeout
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            pass
        except OSError as exc:  # EWOULDBLOCK arrives as a plain OSError on some builds
            if exc.errno not in (11, 35):
                raise
        if clock() >= deadline:
            raise LockWaitTimeout("lock %s held by another process for over %ds"
                                  % (label or getattr(handle, "name", "?"), int(timeout)))
        sleep(poll)
