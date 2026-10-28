"""Pull and push the board repo itself.

Ritual section 1: pull before, push after, never hold the lock. Every board-mutating
command routes through here so that discipline is in one place instead of duplicated
across touch/close/board.

Failures are reported, never fatal. A board edit that succeeded locally but could not push
(offline, or the remote moved) must still leave the local change in place — losing the edit
would be strictly worse than being temporarily out of sync.
"""

from typing import List, Optional, Tuple

from . import gitutil, paths, proseguard, pushguard


def available(root: Optional[str] = None) -> bool:
    return gitutil.is_repo(root or paths.root())


def pull(root: Optional[str] = None) -> Tuple[bool, str]:
    """Pull with rebase. Returns (ok, note)."""
    repo = root or paths.root()
    if not available(repo):
        return True, "not a git repo — skipping pull"
    if not gitutil.has_upstream(repo):
        return True, "no upstream — skipping pull"

    state = gitutil.state(repo)
    if state in (gitutil.MERGING, gitutil.REBASING, gitutil.CONFLICTED):
        return False, "board is %s — resolve it before syncing" % state

    # Generated snapshot files the sweep wrote would make the autostash conflict; commit them.
    gitutil.commit_generated_snapshots(repo)

    gitutil.fetch(repo)
    rewritten = gitutil.upstream_rewrite(repo)
    if rewritten:
        return False, rewritten + " — not pulled"

    code, _, err = gitutil.pull_rebase(repo)
    if code != 0:
        gitutil.abort(repo)
        return False, "pull failed and was aborted: %s" % err.strip().split("\n")[-1]
    return True, "pulled"


def receive(root: Optional[str] = None) -> Tuple[bool, str]:
    """The sweep's board pull: fetch, then fast-forward only. No rebase, no autostash.

    `pull` above rebases with autostash, which is fine for a board command acting on its own
    files but is exactly the shape the sweep has given up. So the sweep's pre-pull only moves
    HEAD when that is a pure fast-forward over a tracked-clean tree; anything else is left for
    Lodestar's own row later in the same sweep, which flags it if it needs a human.
    """
    repo = root or paths.root()
    if not available(repo):
        return True, "not a git repo — skipping pull"
    if not gitutil.has_upstream(repo):
        return True, "no upstream — skipping pull"
    if gitutil.state(repo) != gitutil.OK:
        return True, "board not in a clean state — left for its own row"
    code, _, err = gitutil.fetch(repo)
    if code != 0:
        return False, "fetch failed: %s" % err.strip().split("\n")[-1]
    counts = gitutil.ahead_behind(repo)
    if not counts or not counts[1]:
        return True, "up to date"
    if counts[0]:
        return True, "diverged — left for its own row"
    if gitutil.tracked_dirty(repo):
        return True, "behind with local edits — left for its own row"
    step = gitutil.fast_forward(repo)
    if step.code != 0:
        return False, "fast-forward refused: %s" % (step.out + step.err).strip().split("\n")[-1]
    return True, "fast-forwarded %d" % counts[1]


# The board's own artifacts. touch/close/board stage only these, because this repo is
# simultaneously the board *and* the source of the tooling that maintains it -- an
# unscoped `add -A` swept in-flight source edits into commits titled "touch <loop>".
BOARD_PATHS = ("loops", "journal", "BOARD.md")


def commit_and_push(
    message: str,
    root: Optional[str] = None,
    paths_to_add: Optional[List[str]] = None,
) -> Tuple[bool, str]:
    """Stage `paths_to_add` (default: the board artifacts), commit, then push.

    Pass `paths_to_add=["-A"]`-style breadth only when you genuinely mean everything;
    board commands should stay scoped so a developer's half-finished edit to bin/ is never
    committed as a side effect of touching a loop.
    """
    repo = root or paths.root()
    if not available(repo):
        return True, "not a git repo — skipping commit"

    targets = list(BOARD_PATHS) if paths_to_add is None else list(paths_to_add)
    # Board prose never reaches github.com (ruling 5): a hand-edited loop body or a journal
    # note is moved to the private store before staging, so the pre-commit guard then passes.
    if proseguard.applies(repo):
        proseguard.fix(repo, [p for p in gitutil.changed_paths(repo)
                              if p.startswith(("loops/", "journal/"))])
    code, _, err = gitutil.add_paths(repo, targets)
    if code != 0:
        return False, "git add failed: %s" % err.strip()

    if not gitutil.has_staged_changes(repo):
        return True, "nothing to commit"

    code, out, err = gitutil.commit(repo, message)
    if code != 0:
        combined = (out + err).strip()
        if "nothing to commit" in combined:
            return True, "nothing to commit"
        return False, "commit failed: %s" % combined.split("\n")[-1]

    if not gitutil.has_upstream(repo):
        return True, "committed (no upstream to push to)"

    if proseguard.applies(repo):
        try:
            found = proseguard.outgoing_violations(repo)
        except proseguard.GuardError as error:
            return False, "committed locally, NOT pushed: private-text guard failed (%s)" % error
        if found:
            return False, ("committed locally, NOT pushed: the private-text guard refused %s"
                           % "; ".join("%s (%s)" % f for f in found[:3]))

    refused = pushguard.refusal_for(repo)
    if refused:
        return False, "committed locally, " + refused
    code, _, err = gitutil.push(repo)
    if code != 0:
        return False, "committed locally but push failed: %s" % err.strip().split("\n")[-1]
    return True, "committed and pushed"
