"""Where a repo's session prose may be written (ruling 5, 2026-10-13: "Plug the last leaks").

Lodestar's tooling writes prose into every repo it serves: conversation records, handoffs,
consult transcripts, Bearing's filed mail. On a work Mac any of it can carry private text (mail
subjects, colleagues' words, internal project detail), and a repo whose push URL is public
would publish it. Classifying the prose is not possible, so the rule is about the REPO and the
MACHINE, and it is deterministic:

* a repo that can push only to the private host (`lib.boundary`) keeps its prose in the repo;
* any other repo's prose goes to the overlay `<private store>/projects/<RepoName>/<sub>`,
  when this machine holds a private-store checkout that itself pushes only to the private host;
* a machine with no private-store checkout that is not a `work` machine (a home machine with no
  route to any private source) keeps writing in the repo — it has no private text to leak;
* a `work` machine with no usable checkout, or a checkout that could push elsewhere, gets
  `WITHHELD`: the caller writes nothing to the repo and says so. Failing toward the repo is the
  leak this module exists to stop, so it never happens silently.

`place()` is the one decision. Callers never re-derive it.
"""

from __future__ import annotations

import os
from typing import Dict, NamedTuple, Optional

try:
    from lib import privatestore, machine, reckoning
except ImportError:  # pragma: no cover
    import privatestore  # type: ignore
    import machine  # type: ignore
    import reckoning  # type: ignore

REPO = "repo"
STORE = "store"
WITHHELD = "withheld"

WORK_LABEL = "work"
OVERLAY_DIR = "projects"


class Placement(NamedTuple):
    where: str            # REPO, STORE or WITHHELD
    path: Optional[str]   # the directory to write into; None when WITHHELD
    reason: str


_PRIVATE_CACHE: Dict[str, bool] = {}


def is_work_machine(machine_key: Optional[str] = None) -> bool:
    """True on a machine labelled `work` — and on one whose labels cannot be read.

    Unknown fails toward `work`, because `work` is the cautious answer here: it withholds
    instead of writing to a github.com repo."""
    try:
        found = machine.labels(machine_key or machine.key())
    except Exception:
        return True
    if not found:
        return True
    return WORK_LABEL in found


def repo_is_private(repo: str) -> bool:
    """Every push URL of `repo`'s checkout is on the private host. A cached `True` must not
    outlive a remote being repointed at a public host (GPT review, 2026-10-13), so the only cache
    used is reckoning's fingerprinted PASS cache, invalidated by any change to the repo's
    .git/config, ~/.gitconfig, ssh config or GIT_* env (and a 600 s TTL). A live pass is
    remembered; a refusal never is."""
    real = os.path.realpath(os.path.expanduser(repo))
    if not os.path.isdir(real):
        return False
    if reckoning._cached_top(real) is not None:  # inside a verified checkout, nothing nested
        return True
    top = reckoning.repo_top(real)
    ok = bool(top) and reckoning.private_only(top)
    if ok:
        reckoning._remember(top)
    return ok


def overlay(repo: str, store: Optional[str] = None) -> str:
    """`<private store>/projects/<RepoName>`. Raises `privatestore.Unavailable`."""
    base = privatestore.root(store)
    name = os.path.basename(os.path.realpath(os.path.expanduser(repo)).rstrip(os.sep))
    if not name or name in (".", ".."):
        raise privatestore.Rejected("cannot name an overlay for %r" % repo)
    return os.path.join(base, OVERLAY_DIR, name)


def place(repo: str, sub: str, store: Optional[str] = None,
          machine_key: Optional[str] = None) -> Placement:
    """Where `<repo>/<sub>` prose belongs on this machine."""
    repo = os.path.realpath(os.path.expanduser(repo))
    if repo_is_private(repo):
        return Placement(REPO, os.path.join(repo, sub), "repo pushes only to the private host")
    try:
        base = overlay(repo, store)
    except privatestore.NoCheckout as error:
        if is_work_machine(machine_key):
            return Placement(WITHHELD, None,
                             "work machine with no private store checkout (%s)" % error)
        return Placement(REPO, os.path.join(repo, sub),
                         "no private access on this machine, so nothing private to leak")
    except privatestore.Unavailable as error:
        return Placement(WITHHELD, None, str(error))
    return Placement(STORE, os.path.join(base, sub),
                     "public-hosted repo: prose lives in the private store")


def ensure_dir(placement: Placement, store: Optional[str] = None) -> str:
    """Create a placement's directory; for STORE, refuse one that escapes the checkout."""
    if placement.where == WITHHELD or not placement.path:
        raise privatestore.Rejected(placement.reason)
    if placement.where == STORE:
        privatestore._safe_dir(privatestore.root(store), placement.path)
    else:
        os.makedirs(placement.path, exist_ok=True)
    return placement.path


def reset_cache() -> None:
    _PRIVATE_CACHE.clear()
