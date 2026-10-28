"""Git operations and repo-state classification for the sync sweep.

This module's job is to be paranoid. MULTI_MACHINE_SYNC.md's non-negotiables are encoded
here rather than left to the caller's discipline:

- **Never force.** There is no code path that passes --force to push.
- **Never auto-resolve.** A failed rebase is aborted and the tree restored, then flagged.
- **Never autostash, never merge.** The sweep receives by fast-forward only, and rebases
  local commits only over a clean tree. A refused merge or an autostash hard-resets the tree
  internally, which is a known file-loss path that the repo hooks ban.
- **Skip unsafe states.** Detached HEAD, mid-merge, mid-rebase, conflicts — and, per
  decision D-E, a *populated index*, which is the strongest available signal that a human
  is mid-thought. A sibling repo was in exactly that state when this was written.
- **Commit only quiescent trees**, so a half-saved file never lands in a sync commit.
- **Guard secrets and large files** as a backstop to .gitignore.
- **Survive `.git/index.lock` contention, and never do so silently** (see the lock section).

Nothing here knows about loops or the board; `sync.py` composes the policy.
"""

import fnmatch
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Callable, List, Optional, Sequence, Tuple

# Backstop only — .gitignore is the first line of defense.
SECRET_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.keystore",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "credentials",
    "credentials.*",
    ".netrc",
)

MAX_BYTES = 25 * 1024 * 1024

# Repo states. Only OK is safe to auto-commit.
OK = "ok"
NOT_A_REPO = "not-a-repo"
MISSING = "missing"
DETACHED = "detached-head"
MERGING = "mid-merge"
REBASING = "mid-rebase"
CONFLICTED = "conflicted"
DIRTY_INDEX = "dirty-index"

# A git command timed out while classifying: the machine was too slow to answer, which says
# nothing about the repo. Unsafe (nothing is touched) but not a "needs you": sync.py reports it
# as BUSY (amber). Until 2026-10-22 a timeout here read as NOT_A_REPO, flagging healthy repos.
TIMED_OUT = "git-timeout"

UNSAFE_STATES = (NOT_A_REPO, TIMED_OUT, MISSING, DETACHED, MERGING, REBASING, CONFLICTED, DIRTY_INDEX)


# --- .git/index.lock contention ---------------------------------------------------------
#
# The failure this section exists for is real and dated. On 2026-09-01 a zero-byte
# `.git/index.lock` was left behind in Lodestar and wedged every commit for several minutes:
# five consecutive `git commit` runs failed with "Another git process seems to be running in
# this repository", no git process was actually running, and the launchd sweep was idle. It
# was noticed only because a human happened to look — the failures were otherwise silent.
#
# Contention here is structural, not exceptional: the sync sweep commits hourly, Claude Code
# commits interactively, and Cowork reads and writes the same working tree. So
# git's index lock is something to *ride out and report*, never to crash on and never to
# swallow.
#
# Two mechanisms, in deliberate order of aggression:
#
#   1. Retry with a short bounded backoff. Nearly all contention is a real git process that
#      finishes in well under a second, and waiting is free of risk.
#   2. Only if that fails, consider the lock stale — and be very hard to convince. Deleting
#      a lock another process is actively holding can corrupt the index, which is far worse
#      than a sweep that skips a cycle. All four conditions in `lock_verdict` must hold.

LOCK_ATTEMPTS = 4
LOCK_BACKOFF = (0.5, 1.0, 2.0)  # seconds; ~3.5s worst case

# Why 120s: chosen when the sweep fired every 127 seconds (it is hourly since 2026-09-17), so
# a lock younger than that could plausibly belong to a git process that is simply slow — a big `add` in a
# large repo, a pre-commit hook, an index refresh on a cold cache. A lock that has survived
# a whole sweep interval without changing is a different animal. Note the threshold alone
# proves nothing: git does not touch the lock's mtime while it works, so a long-running git
# process leaves an *old* lock too. That is exactly why the holder check below exists and
# why age is only one of four conditions.
STALE_LOCK_SECONDS = 120

# Substrings of git's own contention messages. Matched case-insensitively.
LOCK_MARKERS = (
    "another git process seems to be running",
    "index.lock",
    "cannot lock ref",
    "unable to create",
)

LOCK_ABSENT = "absent"   # no lock file at all
LOCK_FRESH = "fresh"     # young enough that a live writer is the likely explanation
LOCK_HELD = "held"       # old, but something says it is in use — leave it alone
LOCK_STALE = "stale"     # all four conditions hold; safe to remove

# Every `.git/*.lock` the sweep guards, in the order it checks them.
#
# `HEAD.lock` was added 2026-09-15 after a real incident: a zero-byte HEAD.lock sat in
# an archive checkout for ten days, and because only index.lock was ever checked, the sweep
# staged files happily and then failed at the commit — `git add` needs no ref lock, `git
# commit` does. So the tree kept accumulating staged work and nothing ever said why. That is
# the same silent-wedge failure the index.lock guard was written for, one file over.
#
# The zero-byte test transfers: git writes the new ref value *into* HEAD.lock, so any content
# means a real writer got somewhere, and abandoned debris is empty.
GUARDED_LOCKS = ("index.lock", "HEAD.lock")

# Whether this process may delete a stale lock at all. `sync.py` sets it from this machine's
# `Locks` column in machines.md (machine.lock_policy). False on a machine where the holder
# check is blind — WSL, whose repos live on the Windows drive where `lsof` cannot see a
# Windows process holding the file. There, a lock is only ever reported.
ALLOW_LOCK_REMOVAL = True


class GitError(Exception):
    pass


class GitResult(tuple):
    """`(code, out, err)` plus what happened around the index lock.

    A `tuple` subclass rather than a wider NamedTuple because every existing caller and test
    unpacks exactly three values (`code, out, err = commit(...)`). Widening the tuple would
    break all of them; the lock facts ride along as attributes instead, so a caller that
    cares can tell "succeeded after retries" from "gave up" and a caller that does not is
    unaffected.
    """

    def __new__(cls, code: int, out: str, err: str, attempts: int = 1,
                lock_note: str = "", lock_blocked: bool = False):
        result = super(GitResult, cls).__new__(cls, (code, out, err))
        result.attempts = attempts
        result.lock_note = lock_note
        result.lock_blocked = lock_blocked
        return result

    @property
    def code(self) -> int:
        return self[0]

    @property
    def out(self) -> str:
        return self[1]

    @property
    def err(self) -> str:
        return self[2]

    @property
    def retried(self) -> bool:
        return self.attempts > 1


# --- launch latency and stopping a slow git ----------------------------------------------
#
# 2026-10-22: background security scanning made EVERY process launch on one laptop cost ~256 ms,
# and the sweep, at launchd ProcessType=Background, barely ran (one sweep took hours).
# Measured that day: a git command SIGKILLed while it refreshes the index leaves a 0-byte
# `.git/index.lock` behind; SIGTERM does not (git's handler removes it), and with
# GIT_OPTIONAL_LOCKS=0 a read-only `status` never takes the lock at all. So, for every caller:
#   1. GIT_OPTIONAL_LOCKS=0 by default (read-only commands never write index.lock);
#   2. on timeout, SIGTERM first and SIGKILL only if git ignores it, announced on stderr;
#   3. timeouts scale with the measured launch latency (`calibrate`, run at sweep start).
TIMEOUT_CODE = 124
LAUNCH_LATENCY_S = None  # type: Optional[float]
TIMEOUT_SCALE = 1.0
MAX_TIMEOUT_SCALE = 4.0
TERM_GRACE_S = 5.0
CURRENT_CHILD = None  # the git process in flight, so the sweep's watchdog can SIGTERM it


def scale_for(latency_s: Optional[float]) -> float:
    """1x at a normal launch (<= 20 ms), rising to MAX_TIMEOUT_SCALE by ~320 ms."""
    if latency_s is None:
        return 1.0
    return max(1.0, min(MAX_TIMEOUT_SCALE, 1.0 + max(0.0, latency_s - 0.02) * 10.0))


def calibrate(samples: int = 3, probe: str = "/usr/bin/true") -> Optional[float]:
    """Median wall time to launch `probe`; sets LAUNCH_LATENCY_S and TIMEOUT_SCALE."""
    global LAUNCH_LATENCY_S, TIMEOUT_SCALE
    times = []
    for _ in range(samples):
        start = time.monotonic()
        try:
            subprocess.run([probe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        times.append(time.monotonic() - start)
    times.sort()
    LAUNCH_LATENCY_S = times[len(times) // 2]
    TIMEOUT_SCALE = scale_for(LAUNCH_LATENCY_S)
    return LAUNCH_LATENCY_S


def _git_env(env):
    merged = dict(os.environ if env is None else env)
    merged.setdefault("GIT_OPTIONAL_LOCKS", "0")
    return merged


def stop_child(child) -> str:
    """SIGTERM, then SIGKILL after TERM_GRACE_S. Returns how it ended, for the log."""
    try:
        child.terminate()
        child.communicate(timeout=TERM_GRACE_S)
        return "SIGTERM"
    except subprocess.TimeoutExpired:
        child.kill()
        child.communicate()
        return "SIGKILL after ignoring SIGTERM for %ds" % TERM_GRACE_S
    except (OSError, ValueError):
        return "already gone"


def run(repo: str, *args: str, **kwargs) -> GitResult:
    """Run a git command in `repo`. Returns (returncode, stdout, stderr).

    Never raises on a non-zero exit — callers decide what a failure means, since for the
    sweep most failures are "flag it and move on" rather than "crash". A timeout returns
    TIMEOUT_CODE with a stderr that names it (see `timed_out`).
    """
    global CURRENT_CHILD
    timeout = kwargs.pop("timeout", 120) * TIMEOUT_SCALE
    env = _git_env(kwargs.pop("env", None))
    command = ["git", "-C", repo] + list(args)
    try:
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 env=env)
    except OSError as error:
        return GitResult(127, "", str(error))
    CURRENT_CHILD = child
    try:
        out, err = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        how = stop_child(child)
        note = ("timed out after %ds (launch latency %s, timeouts x%.1f), stopped with %s: %s"
                % (timeout, "unmeasured" if LAUNCH_LATENCY_S is None
                   else "%d ms" % (LAUNCH_LATENCY_S * 1000), TIMEOUT_SCALE, how,
                   " ".join(command)))
        print("⏱ %s: git %s" % (repo, note), file=sys.stderr)
        return GitResult(TIMEOUT_CODE, "", note)
    finally:
        CURRENT_CHILD = None
    return GitResult(
        child.returncode,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


def timed_out(result) -> bool:
    return result[0] == TIMEOUT_CODE and "timed out after" in (result[2] or "")


def looks_like_lock_contention(text: str) -> bool:
    """True when git's output is about a lock file rather than a real problem.

    Used to decide what is worth retrying. A wrong `False` costs a skipped sweep cycle; a
    wrong `True` costs a few wasted seconds. Neither loses work, so the matching can stay
    simple.
    """
    lowered = (text or "").lower()
    return any(marker in lowered for marker in LOCK_MARKERS)


def lock_path(repo: str, name: str = "index.lock") -> Optional[str]:
    """Path to one of this repo's `.git/*.lock` files, whether or not it exists."""
    directory = git_dir(repo)
    if not directory:
        return None
    return os.path.join(directory, name)


def index_lock(repo: str) -> Optional[str]:
    """Path to this repo's `.git/index.lock`, whether or not it exists."""
    return lock_path(repo, "index.lock")


def lock_holders(path: str) -> Optional[List[str]]:
    """Processes with `path` open, as `lsof` lines. `None` means the check could not run.

    `None` is not "nobody" and callers must not treat it as such: an unverifiable answer has
    to be read as "assume it is live". Asking whether anyone holds the *lock file itself* is
    a far sharper question than scanning for git processes — git's lockfile API keeps the
    descriptor open for the whole operation, so a live writer shows up here and abandoned
    debris does not.
    """
    tool = shutil.which("lsof") or "/usr/sbin/lsof"
    try:
        result = subprocess.run([tool, "--", path], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = [line for line in result.stdout.decode("utf-8", "replace").splitlines()
             if line.strip() and not line.startswith("COMMAND")]
    if not lines and result.returncode not in (0, 1):
        # lsof exits 1 with no output when simply nobody has the file open — that is the
        # normal answer. Any other non-zero exit means the question went unanswered.
        return None
    return lines


def lock_verdict(repo: str, max_age: int = STALE_LOCK_SECONDS,
                 name: str = "index.lock") -> Tuple[str, str]:
    """Classify one `.git/*.lock` as absent / fresh / held / stale, with a reason.

    `name` is any lock in GUARDED_LOCKS. It defaults to `index.lock` so existing callers and
    tests are unaffected.

    Only LOCK_STALE authorises removal, and it requires all four of: the file exists, it is
    zero bytes, it is older than `max_age`, and nothing has it open. Every other path — a
    stat failure, a byte of content, a young mtime, an open descriptor, an lsof that would
    not run — returns something else. The bias is deliberate and heavy: leaving a stale lock
    costs a skipped sweep, removing a live one can corrupt the repo.
    """
    path = lock_path(repo, name)
    if not path:
        return LOCK_HELD, "could not locate the git dir, so the lock cannot be judged"
    if not os.path.exists(path):
        return LOCK_ABSENT, "no %s" % name

    try:
        stat = os.stat(path)
    except OSError as error:
        return LOCK_HELD, "could not stat %s: %s" % (path, error)

    # git writes the replacement index — or, for HEAD.lock, the new ref value — *into* the
    # lock file, so any content at all means a real writer got as far as writing. Debris from
    # a killed process is zero bytes.
    if stat.st_size != 0:
        return LOCK_HELD, ("%s is %d bytes, not empty — a git process wrote to it; "
                           "leaving it alone" % (name, stat.st_size))

    age = int(time.time() - stat.st_mtime)
    if age < max_age:
        return LOCK_FRESH, ("%s is only %ds old (threshold %ds) — assuming a live "
                            "git process owns it" % (name, age, max_age))

    holders = lock_holders(path)
    if holders is None:
        return LOCK_HELD, ("could not check whether anything holds %s — assuming "
                           "it is live" % name)
    if holders:
        return LOCK_HELD, ("%s is still open by %d process(es) — leaving it alone"
                           % (name, len(holders)))

    # The cause is stated, not left to be guessed: a 0-byte lock with no holder is what a git
    # process leaves when it is SIGKILLed mid-write (a timeout's kill, an EDR kill, a killed
    # agent shell). The mtime is when that holder died; correlate it with ⏱ lines in the log.
    return LOCK_STALE, ("%s is 0 bytes, %ds old (holder died %s), and no process has it "
                        "open; a 0-byte orphan means its git was killed (SIGKILL) mid-write"
                        % (name, age, time.strftime("%Y-%m-%dT%H:%M:%S%z",
                                                    time.localtime(stat.st_mtime))))


def remove_stale_lock(repo: str, max_age: int = STALE_LOCK_SECONDS,
                      name: str = "index.lock") -> Tuple[bool, str]:
    """Remove one `.git/*.lock` if and only if `lock_verdict` says LOCK_STALE.

    Returns (removed, note). Deleting a lock is the one destructive act in this module that
    is not a git command, so it is announced on stderr as well as returned: under launchd,
    stderr is `~/Library/Logs/lodestar-sync.err`, the file SYNC_AGENT.md calls the failure
    signal. An unannounced deletion would be exactly the silence this whole section exists
    to remove.
    """
    verdict, why = lock_verdict(repo, max_age, name)
    if verdict != LOCK_STALE:
        return False, why
    if not ALLOW_LOCK_REMOVAL:
        return False, ("%s looks stale (%s), but this machine never deletes locks "
                       "(machines.md Locks: flag) — check no Windows process holds it, then "
                       "remove it by hand" % (name, why))

    path = lock_path(repo, name)
    if not path:
        return False, "could not locate the git dir, so %s cannot be removed" % name
    try:
        os.unlink(path)
    except OSError as error:
        return False, "could not remove %s: %s" % (path, error)

    note = "removed a stale .git/%s (%s)" % (name, why)
    print("🔓 %s: %s" % (repo, note), file=sys.stderr)
    return True, note


def _sleep(seconds: float) -> None:
    """Indirection so tests can drive the retry loop without waiting on a real clock."""
    time.sleep(seconds)


def _retry_on_lock(repo: str, label: str,
                   operation: Callable[[], GitResult],
                   attempts: int = LOCK_ATTEMPTS) -> GitResult:
    """Run `operation`, retrying only while git is complaining about a lock.

    Bounded on purpose: `attempts` tries with `LOCK_BACKOFF` between them, then give up and
    say so. A sweep that skips one cycle is harmless; a sweep that blocks forever holding a
    lock of its own is not.
    """
    notes = []  # type: List[str]
    tried_removal = False
    left_alone = "lock still there"
    result = GitResult(1, "", "")

    for attempt in range(1, attempts + 1):
        result = operation()

        if result.code == 0:
            if attempt > 1:
                notes.append("%s hit .git/index.lock and was retried %d time(s) before it "
                             "succeeded" % (label, attempt - 1))
            return GitResult(result[0], result[1], result[2], attempts=attempt,
                             lock_note="; ".join(notes))

        if not looks_like_lock_contention(result.out + result.err):
            return GitResult(result[0], result[1], result[2], attempts=attempt,
                             lock_note="; ".join(notes))

        if attempt == attempts:
            break

        # Try the conservative removal once, on the first contention rather than the last,
        # so the remaining attempts can actually benefit from it.
        if not tried_removal:
            tried_removal = True
            removed, note = remove_stale_lock(repo)
            if removed:
                notes.append(note)
            else:
                left_alone = note

        _sleep(LOCK_BACKOFF[min(attempt - 1, len(LOCK_BACKOFF) - 1)])

    notes.append("%s still blocked by .git/index.lock after %d attempt(s) — held by another "
                 "process, and left alone: %s; needs you" % (label, attempts, left_alone))
    return GitResult(result[0], result[1], result[2], attempts=attempts,
                     lock_note="; ".join(notes), lock_blocked=True)


def is_repo(path: str) -> bool:
    if not os.path.isdir(path):
        return False
    code, out, _ = run(path, "rev-parse", "--is-inside-work-tree")
    return code == 0 and out.strip() == "true"


class GitTimedOut(GitError):
    """A classifying git command did not answer in time (a slow machine, not a bad repo)."""


def _probe_repo(path: str) -> bool:
    result = run(path, "rev-parse", "--is-inside-work-tree")
    if timed_out(result):
        raise GitTimedOut(result[2])
    return result[0] == 0 and result[1].strip() == "true"


def git_dir(repo: str) -> Optional[str]:
    code, out, _ = run(repo, "rev-parse", "--git-dir")
    if code != 0:
        return None
    candidate = out.strip()
    if not os.path.isabs(candidate):
        candidate = os.path.join(repo, candidate)
    return candidate


def current_branch(repo: str) -> Optional[str]:
    """Branch name, or None when HEAD is detached."""
    code, out, _ = run(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    if code != 0:
        return None
    return out.strip() or None


def status_entries(repo: str) -> List[Tuple[str, str]]:
    """Parse `git status --porcelain -z -uall` into [(xy, path)].

    NUL-delimited so paths containing spaces or newlines are handled; rename and copy
    entries carry a second path which is consumed and discarded.

    `-uall` matters for safety, not tidiness: without it git collapses a new untracked
    directory to `config/`, whose basename is empty, so the secret and size guards below
    would inspect nothing and a directory containing a `.env` or a 500 MB blob would pass
    straight into `add -A`.
    """
    result = run(repo, "status", "--porcelain", "-z", "-uall")
    code, out, err = result
    if timed_out(result):
        raise GitTimedOut(err)
    if code != 0:
        raise GitError("git status failed in %s: %s" % (repo, err.strip()))

    entries = []
    fields = out.split("\0")
    index = 0
    while index < len(fields):
        field = fields[index]
        if not field:
            index += 1
            continue
        xy, path = field[:2], field[3:]
        entries.append((xy, path))
        if xy and xy[0] in ("R", "C"):
            index += 1  # skip the rename/copy source path
        index += 1
    return entries


def state(repo: str) -> str:
    """Classify the repo. Anything but OK must not be auto-committed."""
    if not os.path.isdir(repo):
        return MISSING
    try:
        if not _probe_repo(repo):
            return NOT_A_REPO
    except GitTimedOut:
        return TIMED_OUT

    directory = git_dir(repo)
    if directory:
        if os.path.exists(os.path.join(directory, "MERGE_HEAD")):
            return MERGING
        for marker in ("rebase-merge", "rebase-apply"):
            if os.path.exists(os.path.join(directory, marker)):
                return REBASING

    if current_branch(repo) is None:
        return DETACHED

    try:
        entries = status_entries(repo)
    except GitTimedOut:
        return TIMED_OUT
    except GitError:
        return NOT_A_REPO

    for xy, _ in entries:
        if "U" in xy or xy in ("AA", "DD"):
            return CONFLICTED

    # Decision D-E: a populated index means a human staged something deliberately.
    for xy, _ in entries:
        if xy and xy[0] not in (" ", "?", "!"):
            return DIRTY_INDEX

    return OK


def is_dirty(repo: str) -> bool:
    return bool(status_entries(repo))


def changed_paths(repo: str) -> List[str]:
    """Paths a sweep would add: modified tracked files plus untracked ones."""
    return [path for xy, path in status_entries(repo) if xy != "!!"]


def tracked_dirty(repo: str) -> bool:
    """True when a TRACKED file differs from HEAD (untracked files do not count).

    This is the receive guard. A fast-forward with only untracked files around is safe — git
    refuses by itself if one would be overwritten — but any tracked modification means the
    only ways to move HEAD are autostash or a merge that resets the tree, and the sweep does
    neither.
    """
    return bool(tracked_dirty_paths(repo))


def tracked_dirty_paths(repo: str) -> List[str]:
    """The tracked paths that differ from HEAD (see `tracked_dirty`)."""
    return [path for xy, path in status_entries(repo) if xy not in ("??", "!!")]


def newest_mtime(repo: str, candidates: Optional[Sequence[str]] = None) -> Optional[float]:
    """Most recent mtime among the paths a sweep would commit.

    Only candidate paths are stat'ed rather than walking the tree, so an ignored build
    directory churning in the background cannot keep a repo permanently non-quiescent.
    `candidates` narrows that further (the sweep passes the paths left after
    `.lodestarignore`, so a health file rewritten every minute cannot hold a repo open).
    """
    newest = None
    for path in (changed_paths(repo) if candidates is None else candidates):
        full = os.path.join(repo, path)
        try:
            stamp = os.stat(full).st_mtime
        except OSError:
            continue  # deleted, or a directory entry that vanished
        if newest is None or stamp > newest:
            newest = stamp
    return newest


def quiescent(repo: str, seconds: int, candidates: Optional[Sequence[str]] = None) -> bool:
    """True when nothing has changed for `seconds`. A clean tree is trivially quiescent."""
    newest = newest_mtime(repo, candidates)
    if newest is None:
        return True
    return (time.time() - newest) >= seconds


def offending_paths(
    repo: str,
    patterns: Sequence[str] = SECRET_PATTERNS,
    max_bytes: int = MAX_BYTES,
    candidates: Optional[Sequence[str]] = None,
) -> List[Tuple[str, str]]:
    """Return [(path, reason)] for anything that must not be auto-committed."""
    problems = []
    for path in (changed_paths(repo) if candidates is None else candidates):
        if not os.path.lexists(os.path.join(repo, path)):
            continue  # a deletion carries no bytes, secret or large
        base = os.path.basename(path)
        for pattern in patterns:
            if fnmatch.fnmatch(base, pattern):
                problems.append((path, "matches secret pattern %s" % pattern))
                break
        else:
            full = os.path.join(repo, path)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            if size > max_bytes and not _is_lfs_tracked(repo, path):
                problems.append(
                    (path, "%.1f MB exceeds the %.0f MB limit and is not LFS-tracked"
                     % (size / 1048576.0, max_bytes / 1048576.0))
                )
    return problems


def _is_lfs_tracked(repo: str, path: str) -> bool:
    code, out, _ = run(repo, "check-attr", "filter", "--", path)
    return code == 0 and "filter: lfs" in out


# --- What the sweep may auto-commit -----------------------------------------------------
#
# Measured 2026-09-15: of 304 `chore(sync)` commits, 8 had swallowed source changes. The harm
# is not untidy history. `lib/reversal.py` records the real cost — the sweep commits "by a
# different actor, after the receipt was written, under a sha the receipt never saw, possibly
# bundled with unrelated drift", and reverting such a sha reverts someone else's work. A
# bundled auto-commit therefore breaks the Phase 4 reversal protocol.
#
# So the split is narrow. The other 296 are board state — journal events, heartbeats, loop
# files, generated artifacts — and they must keep committing on every sweep, because that is
# the entire promise of the sweep. Only source is withheld, because only source needs an authored
# message and a scoped sha.
#
# Two classifications are load-bearing and easy to get wrong: `loops/<id>.md` is board state
# despite being Markdown, and `BOARD.md` is generated. Calling either one source would stop
# the board syncing between machines, which is the sweep's reason to exist.
#
# Extension-based rather than path-based, so it generalises across every swept repo instead of
# encoding Lodestar's own layout. `docs/` is included because authored prose deserves an
# authored message; the plans file in d79cb82 was swallowed exactly that way.

SOURCE_SUFFIXES = (
    ".py", ".sh", ".bash", ".zsh", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".rb",
    ".c", ".h", ".cpp", ".swift", ".sql", ".pl", ".lua",
)

#: Authored prose at the repo root. Deliberately a closed list: `BOARD.md` and `machines.md`
#: are generated or data, and `loops/*.md` must never match.
SOURCE_ROOT_DOCS = ("README.md", "USING.md", "CLAUDE.md", "AGENTS.md")

#: Any path whose first segment is one of these is authored prose.
SOURCE_DIRS = ("docs",)


def is_source(path: str) -> bool:
    """True when a path needs an authored commit rather than an automatic one."""
    normalised = path.replace(os.sep, "/")
    if normalised.startswith("./"):
        normalised = normalised[2:]
    if normalised in SOURCE_ROOT_DOCS:
        return True
    if normalised.split("/")[0] in SOURCE_DIRS:
        return True
    return normalised.endswith(SOURCE_SUFFIXES)


def partition_paths(paths: Sequence[str]) -> Tuple[List[str], List[str]]:
    """Split into (source, sweepable), preserving the given order in both."""
    source, sweepable = [], []
    for path in paths:
        (source if is_source(path) else sweepable).append(path)
    return source, sweepable


# --- What the sweep must never auto-commit: .lodestarignore --------------------------------
#
# Measured 2026-09-25 in one busy repo: most `chore(sync)` commits were generated health-file
# churn, and one committed an Office `~$…pptx` lock file. `.gitignore` is the
# wrong tool for either: the health files are tracked on purpose, and a project may want a
# file versioned by its own authored commits while still refusing it to the sweep.
#
# So a repo may carry `.lodestarignore` at its root, in gitignore syntax, owned and edited by
# the project itself. Matching is done here in Python rather than by `git check-ignore`,
# because `check-ignore --no-index` also applies the repo's `.gitignore` and would silently
# withhold every tracked-but-ignored file from the sweep — a behaviour change nobody asked for.

LODESTARIGNORE = ".lodestarignore"

#: Always withheld, in every repo, whatever its .lodestarignore says.
BUILTIN_IGNORES = (
    "~$*",               # Microsoft Office owner/lock files
    ".~lock*",           # LibreOffice lock files
    "*.swp", "*.swo",    # vim swap
    ".#*",               # emacs lock links
    "codebase_health*",  # generated health reports
)


def _gitignore_regex(pattern: str) -> Optional[Tuple["re.Pattern", bool]]:
    """Compile one gitignore line to (regex, negated), or None for a blank/comment line.

    Supports what projects actually write: `#` comments, `!` negation, a trailing `/` for
    directories only, a leading or inner `/` anchoring to the repo root, `*`, `?`, `[...]` and
    `**`. A matched directory withholds everything beneath it.
    """
    import re
    line = pattern.rstrip("\n").rstrip()
    if not line or line.startswith("#"):
        return None
    negated = line.startswith("!")
    if negated:
        line = line[1:]
    if line.startswith("\\#") or line.startswith("\\!"):
        line = line[1:]
    directory_only = line.endswith("/")
    line = line.rstrip("/")
    if not line:
        return None
    anchored = "/" in line
    line = line.lstrip("/")

    out, index = [], 0
    while index < len(line):
        char = line[index]
        if line.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
        elif line.startswith("/**", index) and index + 3 == len(line):
            out.append("/.*")
            index += 3
        elif line.startswith("**", index):
            out.append(".*")
            index += 2
        elif char == "*":
            out.append("[^/]*")
            index += 1
        elif char == "?":
            out.append("[^/]")
            index += 1
        elif char == "[":
            end = line.find("]", index + 1)
            if end == -1:
                out.append(re.escape(char))
                index += 1
            else:
                body = line[index + 1:end]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[%s]" % body.replace("\\", "\\\\"))
                index = end + 1
        else:
            out.append(re.escape(char))
            index += 1

    prefix = "^" if anchored else "^(?:.*/)?"
    suffix = "/.*$" if directory_only else "(?:/.*)?$"
    return re.compile(prefix + "".join(out) + suffix), negated


def ignore_rules(repo: str) -> List[Tuple["re.Pattern", bool]]:
    """The built-ins, then the repo's own `.lodestarignore` (so the repo can `!` one back)."""
    lines = list(BUILTIN_IGNORES)
    try:
        with open(os.path.join(repo, LODESTARIGNORE), encoding="utf-8") as handle:
            lines.extend(handle.read().splitlines())
    except (OSError, UnicodeDecodeError):
        pass
    rules = []
    for line in lines:
        compiled = _gitignore_regex(line)
        if compiled:
            rules.append(compiled)
    return rules


def is_sweep_ignored(path: str, rules: Sequence[Tuple["re.Pattern", bool]]) -> bool:
    """Last matching rule wins, exactly as in gitignore."""
    normalised = path.replace(os.sep, "/")
    if normalised.startswith("./"):
        normalised = normalised[2:]
    ignored = False
    for regex, negated in rules:
        if regex.match(normalised):
            ignored = not negated
    return ignored


def split_ignored(repo: str, paths: Sequence[str]) -> Tuple[List[str], List[str]]:
    """(candidates, ignored): `paths` with the sweep's ignore rules applied."""
    rules = ignore_rules(repo)
    keep, drop = [], []
    for path in paths:
        (drop if is_sweep_ignored(path, rules) else keep).append(path)
    return keep, drop


def ahead_behind(repo: str) -> Optional[Tuple[int, int]]:
    """(ahead, behind) relative to the upstream, or None if there is no upstream."""
    code, out, _ = run(repo, "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
    if code != 0:
        return None
    parts = out.split()
    if len(parts) != 2:
        return None
    behind, ahead = int(parts[0]), int(parts[1])
    return ahead, behind


def last_commit_time(repo: str) -> Optional[float]:
    """Committer timestamp of HEAD as epoch seconds, or None if it cannot be read.

    Entirely local — no network, no remote-tracking ref — which is what makes it usable as
    the input to the sweep's fetch tiering. `None` covers a missing directory, a non-repo,
    and a freshly `init`ed repo with no commits; the caller decides what an unknown age
    means, and `sync.py` deliberately reads it as "fetch anyway".
    """
    code, out, _ = run(repo, "log", "-1", "--format=%ct")
    if code != 0:
        return None
    try:
        return float(out.strip())
    except ValueError:
        return None


# A rewritten upstream (2026-10-13 scrub of Lodestar's github.com history). A checkout still
# holding the old history must STOP: a rebase would replay the scrubbed text onto the new
# history and the next push would publish it again. The upstream tip may carry this file,
# listing 40-hex shas of commits the rewrite replaced, and one `command:` line to run.
REWRITE_MARKER = "sync/history-rewrite.md"


def upstream_rewrite(repo: str) -> Optional[str]:
    """Why this checkout's history predates a rewrite of its upstream, or None.

    Reads only already-fetched refs. Two signals: no common ancestor with the upstream at
    all, or local history containing a commit the upstream's REWRITE_MARKER retires.
    """
    import re
    code, _, _ = run(repo, "rev-parse", "--verify", "-q", "@{upstream}")
    if code != 0:
        return None
    code, text, _ = run(repo, "show", "@{upstream}:" + REWRITE_MARKER)
    marker = text if code == 0 else ""
    command = ""
    for line in marker.splitlines():
        if line.strip().lower().startswith("command:"):
            command = line.split(":", 1)[1].strip()
            break
    tail = ("; run: %s" % command) if command else "; re-clone it, do not rebase"
    code, base, _ = run(repo, "merge-base", "HEAD", "@{upstream}")
    if code != 0 or not base.strip():
        return "upstream history was rewritten (no common ancestor)" + tail
    for sha in re.findall(r"\b[0-9a-f]{40}\b", marker):
        if run(repo, "merge-base", "--is-ancestor", sha, "HEAD")[0] == 0:
            return ("upstream history was rewritten (this checkout still holds retired "
                    "commit %s)" % sha[:10]) + tail
    return None


def has_upstream(repo: str) -> bool:
    code, _, _ = run(repo, "rev-parse", "--abbrev-ref", "@{upstream}")
    return code == 0


def fetch(repo: str) -> Tuple[int, str, str]:
    return run(repo, "fetch", "--quiet", timeout=180)


def pull_rebase(repo: str) -> GitResult:
    """pull --rebase --autostash. On failure the caller must call `abort`.

    **Not used by the sweep any more** (2026-09-25): an autostash is a known file-loss path.
    Kept only for the interactive board commands (`selfsync.pull`), which touch Lodestar alone.
    The sweep uses `fast_forward` and `rebase_onto_upstream`, neither of which stashes.

    Retried on lock contention: the autostash and the checkout both take the index lock, and
    a collision there is indistinguishable from a conflict in the exit code alone.
    """
    return _retry_on_lock(
        repo, "git pull --rebase",
        lambda: run(repo, "pull", "--rebase", "--autostash", timeout=300))


def fast_forward(repo: str) -> GitResult:
    """Move HEAD to the upstream if and only if that is a fast-forward. No stash, no merge.

    The caller has already checked that no tracked file is dirty. Git refuses by itself if an
    untracked file would be overwritten, so a refusal here is always "nothing happened".
    `merge.autoStash=false` pins that down against a user config that turns it on.
    """
    return _retry_on_lock(
        repo, "git merge --ff-only",
        lambda: run(repo, "-c", "merge.autoStash=false", "merge", "--ff-only", "--quiet",
                    "@{upstream}", timeout=300))


def rebase_onto_upstream(repo: str) -> GitResult:
    """Replay local commits over the upstream. Caller guarantees a clean tracked tree.

    `--no-autostash` plus `rebase.autoStash=false` so a user config cannot reintroduce the
    stash. On failure the caller must call `rebase_abort`.
    """
    return _retry_on_lock(
        repo, "git rebase",
        lambda: run(repo, "-c", "rebase.autoStash=false", "rebase", "--no-autostash",
                    "--quiet", "@{upstream}", timeout=300))


def rebase_abort(repo: str) -> None:
    """Abort a failed rebase. Touches no stash — the sweep never made one."""
    directory = git_dir(repo)
    if directory and any(os.path.exists(os.path.join(directory, marker))
                         for marker in ("rebase-merge", "rebase-apply")):
        run(repo, "rebase", "--abort")


def rebase_in_progress(repo: str) -> bool:
    directory = git_dir(repo)
    return bool(directory) and any(os.path.exists(os.path.join(directory, marker))
                                   for marker in ("rebase-merge", "rebase-apply"))


def conflicted_paths(repo: str) -> List[str]:
    """Paths left unmerged by a stopped rebase/merge, repo-relative."""
    code, out, _ = run(repo, "diff", "--name-only", "--diff-filter=U")
    if code != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def rebase_continue(repo: str) -> GitResult:
    """Continue a rebase after the caller staged its resolution; never opens an editor.

    A resolution identical to HEAD leaves the replayed commit empty, which `--continue`
    refuses, so that case is `--skip` — git drops only the now-empty commit.
    """
    empty = run(repo, "diff", "--cached", "--quiet").code == 0
    verb = "--skip" if empty else "--continue"
    return _retry_on_lock(
        repo, "git rebase " + verb,
        lambda: run(repo, "-c", "rebase.autoStash=false", "rebase", verb, timeout=300,
                    env=dict(os.environ, GIT_EDITOR="true")))


def abort(repo: str) -> None:
    """Undo a failed rebase and restore any autostash, leaving the tree as it was.

    Best-effort by design: this runs when something has already gone wrong, and each step
    is independently safe to attempt.
    """
    run(repo, "rebase", "--abort")
    code, out, _ = run(repo, "stash", "list")
    if code == 0 and "autostash" in out:
        run(repo, "stash", "pop")


def add_all(repo: str) -> GitResult:
    return _retry_on_lock(repo, "git add -A", lambda: run(repo, "add", "-A"))


def add_paths(repo: str, targets: Sequence[str]) -> GitResult:
    """Stage only the given paths, deletions included, leaving unrelated edits unstaged.

    A path that no longer exists is kept when git tracks it — that is a deletion, and `git
    add -- <path>` stages it. It used to be dropped by an `os.path.exists` filter, so a file
    deleted on one machine silently never left the other. A path that is neither on disk nor
    tracked (it vanished between `status` and here) is dropped, since naming it would fail
    the whole `add`.
    """
    existing = [t for t in targets if os.path.lexists(os.path.join(repo, t))]
    missing = [t for t in targets if not os.path.lexists(os.path.join(repo, t))]
    if missing:
        code, out, _ = run(repo, "ls-files", "-z", "--", *missing)
        tracked = set(out.split("\0")) if code == 0 else set()
        existing.extend(t for t in missing if t in tracked)
    if not existing:
        return GitResult(0, "", "")
    return _retry_on_lock(repo, "git add", lambda: run(repo, "add", "--", *existing))


def has_staged_changes(repo: str) -> bool:
    """True when the index holds something to commit.

    Distinct from `is_dirty`: after a scoped `add`, the tree may still be dirty from
    unrelated edits that must NOT be swept into this commit.
    """
    code, _, _ = run(repo, "diff", "--cached", "--quiet")
    return code == 1


#: The sweep's own author, so its commits can be told from a human's at a glance and filtered
#: with `git log --author="Lodestar sweep"`. The committer stays the machine's own identity.
SWEEP_AUTHOR = "Lodestar sweep <lodestar@localhost>"

#: The generated snapshot files the sweep itself writes into claude/snapshot/<key>/ (live -> repo
#: copies and provenance). `is_source` withholds the `.sh` ones, so nothing ever committed them
#: and they sat modified for ever, making the pull refuse (2026-10-19). Memory files are
#: deliberately NOT here: they are sweepable already and carry the work-machine withholding.
GENERATED_SNAPSHOT_RE = re.compile(
    r"\Aclaude/snapshot/[^/]+/(?:CLAUDE\.md|statusline\.sh|statusline\.rendered\.sh|"
    r"settings\.json|settings\.rendered\.json)\Z")


def generated_snapshot_dirt(repo: str) -> List[str]:
    """Modified/untracked generated snapshot files (see GENERATED_SNAPSHOT_RE), repo-relative."""
    return [p for p in changed_paths(repo) if GENERATED_SNAPSHOT_RE.match(p)]


def commit_generated_snapshots(repo: str) -> List[str]:
    """Commit, by NAMED path, whatever generated snapshot files are dirty. Returns the paths.

    Never stashes, never touches any other path, and does nothing during a merge/rebase. It
    commits rather than discards because CLAUDE.md and *.rendered.* are provenance: restoring
    them would make the next sweep read the live file as an external edit.
    """
    if state(repo) != OK:
        return []
    targets = generated_snapshot_dirt(repo)
    if not targets:
        return []
    if add_paths(repo, targets).code != 0:
        return []
    done = commit(repo, "chore(snapshot): generated claude/snapshot files (%d)" % len(targets),
                  paths=targets, author=SWEEP_AUTHOR)
    return targets if done.code == 0 else []


def commit(repo: str, message: str, paths: Optional[Sequence[str]] = None,
           author: Optional[str] = None) -> GitResult:
    """Commit. With `paths`, commit ONLY those paths (`git commit -- <paths>`).

    The pathspec is the fix for a real race: a bare `git commit` takes whatever is in the
    index, so a file a peer session staged between the sweep's state check and its commit
    rode along under the sweep's message. With a pathspec git commits exactly the named
    paths and leaves anything else staged, untouched, for its owner.
    """
    args = ["commit", "-m", message]
    if author:
        args += ["--author", author]
    if paths:
        args += ["--"] + list(paths)
    return _retry_on_lock(repo, "git commit", lambda: run(repo, *args))


def push(repo: str) -> GitResult:
    """Plain push. There is deliberately no force option anywhere in this module.

    Wrapped too: a push takes ref locks, and `cannot lock ref` is the same class of
    transient contention as the index lock.
    """
    return _retry_on_lock(repo, "git push", lambda: run(repo, "push", timeout=300))


def remote_endpoints(repo: str, remote: str = "origin") -> Tuple[Optional[str], List[str]]:
    """(fetch_url, push_urls) actually configured for `remote` — observation only, no verdict.

    Exists because `git remote set-url origin <new>` does NOT touch a separately-configured
    push URL. Verified in a fixture on this machine 2026-09-30: fetch moved to an internal
    host while push stayed on the public one, using exactly the command the documented rehome
    procedure calls for. A repo believed to have moved to a private host can still be pushing
    to a public one, and nothing about the fetch URL says so. A fleet scan found zero live
    mismatches, which is the reason this needs to become a standing check rather than a
    memory — the hazard is latent, not incident.

    `fetch_url` is `git remote get-url <remote>`. `push_urls` is `git remote get-url --push
    --all <remote>`, which — per git's own documented behaviour — returns the FETCH url when
    no explicit pushurl is configured: an unconfigured repo means "push goes where fetch
    goes", not "misconfigured". This function does NOT special-case that away; it returns
    exactly what git reports on each command and leaves any comparison to the caller. Treat a
    single-entry push_urls equal to fetch_url as "unconfigured push, tracking fetch", and a
    push_urls that differs from fetch_url as the mismatch this function exists to surface.

    Never raises. Returns `(None, [])` when `repo` is not a git repository or `remote` does
    not exist there, matching this module's discipline of checking returncodes rather than
    catching exceptions from git itself.
    """
    fetch_code, fetch_out, _ = run(repo, "remote", "get-url", remote, timeout=30)
    if fetch_code != 0:
        return None, []
    fetch_url = fetch_out.strip()

    push_code, push_out, _ = run(repo, "remote", "get-url", "--push", "--all", remote,
                                  timeout=30)
    if push_code != 0:
        return fetch_url, []
    push_urls = [line for line in push_out.splitlines() if line.strip()]
    return fetch_url, push_urls
