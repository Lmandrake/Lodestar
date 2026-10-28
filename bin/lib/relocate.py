"""Move a managed checkout — and refuse, loudly and completely, when it is not safe to.

The mutation itself is one `os.rename`. Everything real in this module is the refusals, and
the design rule behind all of them is: **when the destination is not obviously free and the
source is not obviously a plain checkout, stop and report.** A relocation that half-works is
worse than one that never started, because the recovery path then has to guess.

**Every refusal is collected, never just the first.** A caller that shows a human one problem
at a time turns one decision into four round trips. `preconditions` runs all of its checks and
returns all of their verdicts.

**🔴 Contents are NEVER merged as a fallback.** If `os.rename` cannot do it, this refuses. The
spec says so explicitly, and the reason is specific to this repo: the sweep auto-commits
whatever it finds under a managed path, so merging one repo's files into another's directory
puts them inside the other's commit scope and pushes them under its name. A failed move is
recoverable; a silent merge is discovered weeks later.

The refusals, and why each exists:

- **`not-standalone`** — the one that would otherwise cost real time. A filesystem rename of a
  linked worktree breaks it with an unusable error (exit 128, needing `git worktree repair`),
  and a submodule's gitdir is recorded by path. `is_standalone` asks whether
  `git rev-parse --git-common-dir` resolves to *this* checkout's own `.git`; for a linked
  worktree it points back at the main checkout instead. No linked worktrees existed when this was written, which is exactly what makes this guard free to add now and expensive to
  discover missing later.
- **`case-only`** — APFS is case-insensitive, so `Alpha` → `alpha` is not a move at all. This
  repo has been bitten by case-insensitivity twice already (mixed-case ids silently
  overwriting files, and a test fixture whose hardcoded machine name *was*
  this machine). Renaming in place on this filesystem either no-ops or destroys the source
  depending on the libc call, so it is refused rather than attempted.
- **`destination-occupied`** / **`symlinked-destination`** — checked with `lexists`, so a
  dangling symlink counts as in the way. `exists` follows the link, reports the destination
  free, and the move lands on top of something.
- **`nested-managed-root`** — one managed checkout inside another puts the inner repo's files
  in the outer repo's sweep scope. Refused in both directions.
- **`path-swap`** — source and destination resolving to the same place. A no-op at best.

What this module deliberately does NOT do: decide whether the move is *wanted*. That is
`lib/plans.py` and the approval. This only decides whether it is *performable*.
"""

import os
import shutil
from typing import Dict, List, NamedTuple, Optional, Sequence

from . import enroll, gitutil

#: The source is not a git repository.
NOT_A_REPO = "not-a-repo"
#: The source path does not exist at all.
MISSING_SOURCE = "missing-source"
#: A linked worktree or a submodule — a filesystem rename breaks it.
NOT_STANDALONE = "not-standalone"
#: Something is already at the destination.
DESTINATION_OCCUPIED = "destination-occupied"
#: The destination path, or a parent of it, is a symlink.
SYMLINKED_DESTINATION = "symlinked-destination"
#: The destination's parent directory does not exist.
DESTINATION_PARENT_MISSING = "destination-parent-missing"
#: Source and destination are the same place.
PATH_SWAP = "path-swap"
#: Source and destination differ only by case — not a move on a case-insensitive filesystem.
CASE_ONLY = "case-only"
#: The destination nests inside, or contains, another managed checkout.
NESTED_MANAGED_ROOT = "nested-managed-root"

KINDS = (NOT_A_REPO, MISSING_SOURCE, NOT_STANDALONE, DESTINATION_OCCUPIED,
         SYMLINKED_DESTINATION, DESTINATION_PARENT_MISSING, PATH_SWAP, CASE_ONLY,
         NESTED_MANAGED_ROOT)


class Refusal(NamedTuple):
    kind: str
    detail: str


def is_standalone(repo_path: str) -> bool:
    """Is this a plain checkout owning its own `.git`, rather than a linked worktree?

    True only when `git rev-parse --git-common-dir` resolves to this directory's own `.git`.
    A linked worktree's common dir points back at the main checkout, and its `.git` is a FILE
    holding a `gitdir:` pointer rather than a directory — both of which a filesystem rename
    invalidates.
    """
    if not gitutil.is_repo(repo_path):
        return False
    shared = enroll.common_dir(repo_path)
    if shared is None:
        return False
    own = os.path.realpath(os.path.join(repo_path, ".git"))
    if shared != own:
        return False
    # A submodule has a `.git` FILE pointing into the superproject's modules directory. The
    # common-dir test above already catches the usual case; this catches a `.git` file whose
    # target happens to realpath back to itself.
    return os.path.isdir(own)


def _same_file(left: str, right: str) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _case_only_difference(old_path: str, new_path: str) -> bool:
    """Do these differ ONLY by case? Compared on the normalised absolute paths."""
    left = os.path.abspath(old_path).rstrip(os.sep)
    right = os.path.abspath(new_path).rstrip(os.sep)
    return left != right and left.lower() == right.lower()


def _symlinked_destination(path: str) -> Optional[str]:
    """The destination leaf or its immediate parent, if either is a symlink. Else None.

    🔴 **Deliberately NOT a walk to the filesystem root, and that is a measured decision.**
    The first version of this walked every ancestor, which refuses essentially every move on
    macOS: `/var` is a symlink to `/private/var` and `/tmp` to `/private/tmp`, so any path
    under either — including every temp directory — reported a symlinked destination and the
    whole module refused to move anything. Ten tests failed on one cause.

    An OS-level symlink high above the destination is not the hazard. The hazard is a symlink
    **at or immediately above** the destination, where `os.rename` would either fail
    (`NotADirectoryError`, renaming a directory onto a symlink) or land somewhere other than
    where the path reads. Stopping at the immediate parent catches that and tolerates a
    perfectly ordinary `~/dev` — which a user may legitimately have symlinked to another
    volume, and which must not make the whole fleet unmovable.
    """
    leaf = os.path.abspath(path).rstrip(os.sep)
    if os.path.islink(leaf):
        return leaf
    parent = os.path.dirname(leaf)
    if parent and parent != leaf and os.path.islink(parent):
        return parent
    return None


def _nests(inner: str, outer: str) -> bool:
    """Is `inner` strictly inside `outer`?"""
    inner_abs = os.path.abspath(inner).rstrip(os.sep)
    outer_abs = os.path.abspath(outer).rstrip(os.sep)
    if inner_abs == outer_abs:
        return False
    return inner_abs.startswith(outer_abs + os.sep)


def preconditions(old_path: str, new_path: str, *,
                  managed_paths: Sequence[str] = ()) -> List[Refusal]:
    """EVERY reason this move must not happen. An empty list means performable.

    All checks run: showing a human one refusal at a time turns one decision into several
    round trips, and the second refusal is often the one that changes what they do.
    """
    found = []  # type: List[Refusal]

    if not os.path.lexists(old_path):
        found.append(Refusal(MISSING_SOURCE,
                             "there is nothing at the source path %s to move" % old_path))
    elif not gitutil.is_repo(old_path):
        found.append(Refusal(NOT_A_REPO,
                             "%s exists but is not a git repository, so it is not a managed "
                             "checkout this may relocate" % old_path))
    elif not is_standalone(old_path):
        found.append(Refusal(NOT_STANDALONE,
                             "%s is not a plain standalone checkout — it is a linked worktree "
                             "or a submodule, whose git directory is recorded by path. A "
                             "filesystem rename breaks it with exit 128 and it then needs "
                             "`git worktree repair`. This one needs a person." % old_path))

    if _case_only_difference(old_path, new_path):
        found.append(Refusal(CASE_ONLY,
                             "%s and %s differ only by case, and this filesystem (APFS) is "
                             "case-insensitive — so this is not a move. Renaming in place here "
                             "either does nothing or destroys the source depending on the "
                             "call, so it is refused rather than attempted."
                             % (old_path, new_path)))
    elif os.path.abspath(old_path).rstrip(os.sep) == os.path.abspath(new_path).rstrip(os.sep) \
            or (os.path.lexists(old_path) and os.path.lexists(new_path)
                and _same_file(old_path, new_path)):
        found.append(Refusal(PATH_SWAP,
                             "%s and %s are the same place, so there is no move to make"
                             % (old_path, new_path)))
    elif os.path.lexists(new_path):
        found.append(Refusal(DESTINATION_OCCUPIED,
                             "something is already at the destination %s. Refusing rather than "
                             "merging into it: the sweep auto-commits whatever it finds under a "
                             "managed path, so merged contents would be pushed under the other "
                             "repo's name." % new_path))

    link = _symlinked_destination(new_path)
    if link is not None:
        found.append(Refusal(SYMLINKED_DESTINATION,
                             "the destination path %s passes through the symlink %s, so the "
                             "move would land somewhere other than where the path reads"
                             % (new_path, link)))

    parent = os.path.dirname(os.path.abspath(new_path))
    if parent and not os.path.isdir(parent):
        found.append(Refusal(DESTINATION_PARENT_MISSING,
                             "the destination's parent directory %s does not exist" % parent))

    for managed in managed_paths:
        if _same_file(managed, old_path) or \
                os.path.abspath(managed).rstrip(os.sep) == \
                os.path.abspath(old_path).rstrip(os.sep):
            continue  # this repo's own row
        if _nests(new_path, managed):
            found.append(Refusal(NESTED_MANAGED_ROOT,
                                 "the destination %s is inside the managed checkout %s, which "
                                 "would put this repo's files in that repo's auto-commit scope"
                                 % (new_path, managed)))
        elif _nests(managed, new_path):
            found.append(Refusal(NESTED_MANAGED_ROOT,
                                 "the managed checkout %s is inside the destination %s, which "
                                 "would put that repo's files in this repo's auto-commit scope"
                                 % (managed, new_path)))

    return found


def move(old_path: str, new_path: str, *, dry_run: bool = False,
         managed_paths: Sequence[str] = ()) -> Dict:
    """Relocate the checkout, or refuse. Never merges, never falls back to a copy.

    Returns `ok`, `refusals`, `moved` and a `reason`. `dry_run` evaluates every precondition
    and mutates nothing, so a caller can show the whole verdict before asking.
    """
    refusals = preconditions(old_path, new_path, managed_paths=managed_paths)
    if refusals:
        return {"ok": False, "refusals": refusals, "moved": False,
                "reason": "refusing to move %s to %s: %s"
                          % (old_path, new_path,
                             "; ".join("%s (%s)" % (r.kind, r.detail) for r in refusals))}

    if dry_run:
        return {"ok": True, "refusals": [], "moved": False,
                "reason": "would move %s to %s — every precondition passes and nothing was "
                          "changed" % (old_path, new_path)}

    try:
        os.rename(old_path, new_path)
    except OSError as exc:
        # Deliberately NOT falling back to copytree+rmtree. A cross-device move is a real
        # situation this cannot safely paper over: a partial copy leaves the identity claimed
        # at two paths, which `transitions.observe` reports as BOTH and refuses to resolve.
        return {"ok": False, "refusals": [], "moved": False,
                "reason": "os.rename(%s, %s) failed: %s. NOT falling back to a copy-and-merge "
                          "— a partial copy would leave the id claimed at two paths. If this is "
                          "a cross-device move it needs a person."
                          % (old_path, new_path, exc)}

    return {"ok": True, "refusals": [], "moved": True,
            "reason": "moved %s to %s" % (old_path, new_path)}


def quarantine(repo_path: str, holding_root: str, *, dry_run: bool = False) -> Dict:
    """Move a checkout out of the working area into a recorded holding location.

    Returns `ok`, `holding_path`, `moved`, `reason`. The holding path is returned so the
    caller can record it BEFORE it is needed — `transitions` has no way to find material whose
    location was never written down.

    🔴 The honest word for the result is **quarantined**, not evicted: the material is still on
    this machine. §3E requires a report to say so, because "evicted" claims a confidentiality
    property this does not deliver.
    """
    if not os.path.lexists(repo_path):
        return {"ok": False, "holding_path": "", "moved": False,
                "reason": "there is nothing at %s to quarantine" % repo_path}

    name = os.path.basename(os.path.abspath(repo_path).rstrip(os.sep))
    holding_path = os.path.join(holding_root, name)

    if os.path.lexists(holding_path):
        return {"ok": False, "holding_path": holding_path, "moved": False,
                "reason": "the holding location %s is already occupied. Refusing rather than "
                          "merging or overwriting — whatever is there was quarantined earlier "
                          "and has not been dealt with." % holding_path}

    if dry_run:
        return {"ok": True, "holding_path": holding_path, "moved": False,
                "reason": "would quarantine %s to %s — nothing was changed"
                          % (repo_path, holding_path)}

    try:
        os.makedirs(holding_root, exist_ok=True)
        shutil.move(repo_path, holding_path)
    except (OSError, shutil.Error) as exc:
        return {"ok": False, "holding_path": holding_path, "moved": False,
                "reason": "quarantining %s to %s failed: %s" % (repo_path, holding_path, exc)}

    return {"ok": True, "holding_path": holding_path, "moved": True,
            "reason": "Active checkout removed. Local quarantine retained at %s. "
                      "Confidentiality eviction incomplete." % holding_path}
