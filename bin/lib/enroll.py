"""Stamp a repo identity into its own checkout, and record which checkout a machine manages.

The stamp asserts ENROLLMENT. It is not proof of ownership, and it is not immutable — it
lives in `.git/config`, a plain file, so anything that can copy a directory can copy the
claim along with it. Nobody downstream should ever treat a stamp as authority; that is
exactly why `conflicts()` exists, to say "two checkouts both claim this" rather than picking
a winner.

Three facts this module is built on, each verified on this machine 2026-09-30 and each
counter-intuitive enough to state rather than assume:

  (a) `git clone` does NOT copy a custom `git config --local` value. A clone-of-a-clone
      therefore arrives UNSTAMPED — which is correct. A clone is a new, unenrolled checkout,
      not a duplicate of the enrolled one.
  (b) `cp -R` of a directory DOES copy `.git/config`, stamp included. Two independent
      directories can then claim the same id, each with its own local, divergent work. That
      is a CONFLICT to surface, never something to resolve by "first one found wins" —
      picking a winner would silently discard whichever checkout lost the coin flip.
  (c) A LINKED WORKTREE (`git worktree add`) exposes the SAME stamp as its main checkout,
      because git shares local config across all worktrees of one repository. A naive
      "one id, several paths" check would report a conflict on a perfectly healthy repo the
      instant someone adds a worktree. Worktrees must be resolved FIRST, by comparing each
      path's `git rev-parse --git-common-dir` — two worktrees of the same repo share one
      common dir, and that is what makes them recognisably ONE instance rather than two.
"""

import json
import os
import subprocess
from typing import Dict, List, Optional, Sequence

from . import paths

CONFIG_KEY = "lodestar.id"


def read_stamp(repo_path: str) -> str:
    """The `lodestar.id` this checkout carries, or "" when absent or not a git repo."""
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "config", "--local", "--get", CONFIG_KEY],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8", "replace").strip()


def stamp(repo_path: str, repo_id: str) -> bool:
    """Write `repo_id` as this checkout's stamp. True if the stamp now reads `repo_id`.

    Refuses — returns False, writes nothing — when a DIFFERENT non-empty id is already
    stamped. Re-stamping an established checkout is a conflict to report, not a repair to
    perform silently. Writing the same id again is a no-op that still returns True.
    """
    if not repo_id or not repo_id.strip():
        raise ValueError("repo_id must not be empty")
    existing = read_stamp(repo_path)
    if existing and existing != repo_id:
        return False
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "config", "--local", CONFIG_KEY, repo_id],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def common_dir(repo_path: str) -> Optional[str]:
    """Realpath of `git rev-parse --git-common-dir`, or None. Shared across one repo's worktrees.

    This is the fact that makes several worktrees of one checkout recognisable as ONE
    instance rather than several: `--git-common-dir` names the main checkout's `.git`
    directory (or the `.git` file's target) from any linked worktree, while `--git-dir`
    would return each worktree's own private administrative directory instead.
    """
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "rev-parse", "--git-common-dir"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.decode("utf-8", "replace").strip()
    if not raw:
        return None
    if not os.path.isabs(raw):
        raw = os.path.join(repo_path, raw)
    return os.path.realpath(raw)


def instances(paths_in: Sequence[str]) -> Dict[str, List[str]]:
    """repo id -> the DISTINCT git common dirs claiming it, as sorted paths.

    Paths sharing a common dir (worktrees of one checkout) collapse to one entry — see fact
    (c). Unstamped paths are omitted entirely, never reported as claiming "".
    """
    by_id = {}  # type: Dict[str, Dict[str, str]]  # repo_id -> {common_dir: representative path}
    for path in paths_in:
        repo_id = read_stamp(path)
        if not repo_id:
            continue
        shared = common_dir(path)
        key = shared if shared is not None else os.path.realpath(path)
        by_id.setdefault(repo_id, {})
        by_id[repo_id].setdefault(key, os.path.realpath(path))
    return dict((repo_id, sorted(dirs.values())) for repo_id, dirs in by_id.items())


def conflicts(paths_in: Sequence[str]) -> Dict[str, List[str]]:
    """Only the ids claimed by MORE THAN ONE distinct common dir — see fact (b)."""
    return dict((repo_id, dirs) for repo_id, dirs in instances(paths_in).items()
                if len(dirs) > 1)


def load_managed(path: Optional[str] = None) -> Dict[str, str]:
    """repo id -> the checkout path this machine manages for it.

    This is NOT a cache. A missing file means "nothing enrolled here", never "re-derive it
    by guessing" — it is the only record able to tell a half-applied move (enrolled at the
    old path, not yet re-pointed to the new one) from a finished one. Callers must not paper
    over a missing entry by falling back to a scan.
    """
    target = path or paths.managed_instances_file()
    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return dict((key, value) for key, value in data.items()
                if isinstance(key, str) and isinstance(value, str))


def save_managed(mapping: Dict[str, str], path: Optional[str] = None) -> None:
    target = path or paths.managed_instances_file()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(mapping, handle, indent=2, sort_keys=True)
    os.replace(tmp, target)
