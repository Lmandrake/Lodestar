"""The gate that refuses to act on stale intent.

Machines in this fleet are intermittently online and there is no coordinator. A machine that
has been offline can wake holding YESTERDAY's desired state -- an old remote URL, an old
machine scope. If it acts before learning today's state, it does not merely flap: it can push
material to a host that today's desired state has moved AWAY from, violating a confidentiality
scope (see the `sync/registry.md` host-move discipline this repo already carries for
the mail archive). The cost of acting on stale intent is not "redo the sync" -- it is a mailbox or a
repo landing somewhere it should not have.

**What this gate is NOT, stated so nobody over-claims it.** This is not a distributed lock.
Nothing here coordinates with any other machine, and instantaneous fleet-wide revocation is
NOT achievable over periodically-fetched git state -- a successful `git fetch` establishes what
*this* machine has learned, never that no peer published a new decision one second later. All
this gate can do is refuse to act on intent this machine already knows is old, or knows it is
behind on. It cannot make a machine current; it can only stop one that knows it is not.

The decision is a pure function (`gate`) so it is honestly testable without a clock, a
network, or a repo -- feed it numbers, read the verdict. `observed` is the thin, separately
testable adapter that turns a real checkout into those numbers.
"""

import subprocess
from typing import NamedTuple, Optional, Tuple

#: 1.5 sweep intervals at the hourly cadence (`StartInterval 3607` / `lodestar-sync.timer`).
#: A machine that has not fetched in longer than this has been offline through at least one
#: whole sweep and is exactly the case this gate exists for.
DEFAULT_MAX_AGE_SECONDS = 5400


class Verdict(NamedTuple):
    """The gate's answer. `reason` is always populated, even when `ok` is True, so a caller
    can log or report the verdict verbatim without a branch of its own."""
    ok: bool
    reason: str


def gate(now: float, last_fetch: Optional[float], behind: int,
         diverged: bool = False,
         max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS) -> Verdict:
    """Refuse to act unless this machine's view of intent is both present and current.

    Checked in this order, each a distinct way "I might be acting on stale intent":

    1. `last_fetch is None` -- never having fetched is not "fresh", it is unknown.
    2. `now - last_fetch` outside `[0, max_age_seconds]` -- either genuinely stale, or the
       clock is skewed and `last_fetch` reads as being in the future. A negative age is
       refused rather than silently treated as fresh: a skewed clock is exactly the moment
       you least want to trust "recently checked".
    3. `behind > 0` -- newer intent exists upstream that this machine has not applied yet.
    4. `diverged` -- both sides moved since the common ancestor, so which intent is current
       is ambiguous and must not be resolved by just acting.
    """
    if last_fetch is None:
        return Verdict(False, "never fetched -- no basis for believing intent is current")

    age = now - last_fetch
    if age < 0:
        return Verdict(False,
            "last_fetch is %.0fs in the future -- clock skew, refusing rather than "
            "trusting a fetch time that cannot be real" % (-age))
    if age > max_age_seconds:
        return Verdict(False,
            "last fetch was %.0fs ago, older than the %.0fs freshness window -- "
            "this machine may be holding yesterday's intent" % (age, max_age_seconds))

    if diverged:
        return Verdict(False,
            "local and upstream have diverged -- both sides moved since the common "
            "ancestor, so which intent is current is ambiguous")

    if behind > 0:
        return Verdict(False,
            "%d commit(s) behind upstream -- newer intent exists that has not been "
            "applied yet" % behind)

    return Verdict(True, "fetched %.0fs ago, up to date with upstream -- safe to act" % age)


def observed(repo_path: str) -> Tuple[int, bool]:
    """(commits behind upstream, diverged) for the checkout at `repo_path`.

    Runs `git rev-list --left-right --count HEAD...@{upstream}`, which prints two integers
    `ahead\\tbehind` measured from the merge base. `diverged` is `ahead > 0 and behind > 0`.

    Returns `(0, False)` when there is no upstream, git is missing, or the command otherwise
    fails. A repo with no upstream is not STALE -- it is out of scope for this gate -- and
    raising here would take down the whole sweep over one unconfigured repo. Error handling
    mirrors `observe_remote` in `lib/skillclone.py`: catch, and return the "nothing to report"
    value rather than propagate.
    """
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "rev-list", "--left-right", "--count",
             "HEAD...@{upstream}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return (0, False)
    if result.returncode != 0:
        return (0, False)
    text = result.stdout.decode("utf-8", "replace").strip()
    parts = text.split()
    if len(parts) != 2:
        return (0, False)
    try:
        ahead, behind = int(parts[0]), int(parts[1])
    except ValueError:
        return (0, False)
    return (behind, ahead > 0 and behind > 0)
