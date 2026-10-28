#!/usr/bin/env python3
"""Move a checkout of Lodestar that predates the 2026-10-13 history rewrite onto the new history.

Run it on any machine whose sweep flags "upstream history was rewritten". From the OLD
checkout, which cannot pull, run the copy the fetch already brought down:

    git -C ~/dev/Lodestar fetch -q origin && git -C ~/dev/Lodestar show origin/main:bin/rewrite_recover.py | python3 -

Stdlib only and self-contained, because it runs against an old checkout whose `bin/lib` is not
the new one. In place, so a session whose cwd is the checkout keeps a valid cwd. Loses nothing:

1. pauses the sweep for this repo (`<git-dir>/lodestar-pause`, which survives the reset);
2. saves to `~/dev/.lodestar/rewrite-carryover/<stamp>/`: a bundle of EVERY local ref (all
   branches, stash, notes: the old history, which must never be pushed again, so it stays on
   this machine), a patch of uncommitted tracked changes, and a copy of every file the reset
   would delete from the working tree;
3. `reset --hard` main onto origin/main, deletes the other local branches, the stash and local
   notes, expires the reflog and prunes, so no old object is left to push;
4. puts back the saved files that the new history ignores (machine-local state), re-applies the
   uncommitted patch where it applies cleanly, and leaves the rest in the carryover folder;
5. keeps `lodestar.id` (git config is untouched) and lifts the pause.

Refuses, changing nothing, if the checkout is mid-rebase/merge or its upstream was not rewritten.

Safety (Tender GPT review, 2026-10-27): every git call runs with hooks and fsmonitor disabled
(`core.hooksPath=/dev/null`, no system config), so the unsigned tree it checks out can never
execute its own hooks. It acts on ONE explicit commit, resolved after the fetch (pass the
expected sha as the second argument and it refuses if origin/main is anything else). Untracked
or ignored files the checkout would overwrite are saved to `<carryover>/collisions/` first.
Each phase is persisted to `<carryover>/state.json`. Exit codes: 0 complete; 3 INCOMPLETE (the
checkout is on the new history but something was not restored; nothing is lost, see
state.json); anything else, failed. The pause is lifted only if this run created it and the
checkout is not half-moved.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time

MARKER = "sync/history-rewrite.md"
EXIT_INCOMPLETE = 3
#: Hooks, fsmonitor and external diff are repository-controlled executables: never run them.
SAFE = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
        "-c", "diff.external=", "-c", "core.pager=cat"]


def git(repo, *args, **kw):
    check = kw.pop("check", True)
    env = dict(os.environ)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    for var in ("GIT_EXTERNAL_DIFF", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(var, None)
    res = subprocess.run(["git"] + SAFE + ["-C", repo] + list(args), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, env=env,
                         universal_newlines=True)
    if check and res.returncode != 0:
        sys.exit("git %s failed: %s" % (" ".join(args), res.stderr.strip()))
    return res


def rewritten(repo, upstream):
    if git(repo, "merge-base", "HEAD", upstream, check=False).returncode != 0:
        return True
    marker = git(repo, "show", upstream + ":" + MARKER, check=False).stdout
    return any(git(repo, "merge-base", "--is-ancestor", sha, "HEAD", check=False).returncode == 0
               for sha in re.findall(r"\b[0-9a-f]{40}\b", marker))


def save_state(carry, state):
    tmp = os.path.join(carry, "state.json.tmp")
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, os.path.join(carry, "state.json"))


def copy_any(src, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.islink(src):
        if os.path.lexists(dest):
            os.remove(dest)
        os.symlink(os.readlink(src), dest)
    else:
        shutil.copy2(src, dest)


def collisions(repo, new):
    """Untracked and ignored paths the checkout of `new` (a set of paths) could overwrite or
    bury: the same path, a new file where a local directory is, or a new directory where a
    local file is."""
    out = git(repo, "ls-files", "-z", "--others", "--exclude-standard").stdout.split("\0")
    out += git(repo, "ls-files", "-z", "--others", "--ignored", "--exclude-standard").stdout.split("\0")
    dirs = set()
    for n in new:
        parts = n.split("/")
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]))
    found = []
    for u in sorted(set(out) - {""}):
        parts = u.split("/")
        if u in new or u in dirs or any("/".join(parts[:i]) in new for i in range(1, len(parts))):
            found.append(u)
    return found


def main(argv):
    repo = os.path.expanduser(argv[0] if argv else "~/dev/Lodestar")
    expected = argv[1] if len(argv) > 1 else None
    gitdir = git(repo, "rev-parse", "--absolute-git-dir").stdout.strip()
    for busy in ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD"):
        if os.path.exists(os.path.join(gitdir, busy)):
            sys.exit("refused: %s is mid-%s; finish or abort it first" % (repo, busy))
    git(repo, "fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main")
    target = git(repo, "rev-parse", "--verify", "-q", "refs/remotes/origin/main^{commit}"
                 ).stdout.strip()
    if not target:
        sys.exit("refused: origin has no main")
    if expected and expected != target:
        sys.exit("refused: origin/main is %s, not the authorized %s" % (target[:12], expected[:12]))
    if not rewritten(repo, target):
        print("nothing to do: %s already shares origin/main's history" % repo)
        return 0

    stamp = time.strftime("%Y%m%dT%H%M%S")
    carry = os.path.join(os.path.expanduser("~/dev/.lodestar"), "rewrite-carryover", stamp)
    os.makedirs(os.path.join(carry, "files"))
    state = {"stamp": stamp, "target": target, "phase": "start", "status": "running",
             "problems": []}
    save_state(carry, state)
    with open(os.path.join(carry, "RESTORE.txt"), "w") as fh:
        fh.write(
            "Restore the OLD state (tested: tests/test_history_rewrite.py::RestoreTest runs every\n"
            "line below that starts with '$ '). Never push the result anywhere.\n"
            "\n"
            "$ git -C %(repo)s fetch %(carry)s/old-history.bundle 'refs/*:refs/old/*'\n"
            "$ git -C %(repo)s worktree add -b old-main %(carry)s/restored refs/old/heads/main\n"
            "$ git -C %(carry)s/restored apply %(carry)s/uncommitted.patch\n"
            "\n"
            "refs/old/heads/<name> are the other old branches, refs/old/stash the stash.\n"
            "files/ and collisions/ hold saved copies of files the reset removed or overwrote.\n"
            "Delete it afterwards: git -C %(repo)s worktree remove --force %(carry)s/restored\n"
            % {"repo": repo, "carry": carry})
    pause = os.path.join(gitdir, "lodestar-pause")
    made_pause = False
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t))
    try:
        fd = os.open(pause, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(fd, "w") as fh:
            json.dump({"until": iso(time.time() + 3600), "reason": "history rewrite recovery",
                       "set_by": "rewrite_recover", "set_at": iso(time.time()),
                       "run": stamp, "directions": ["send", "receive"]}, fh)
        made_pause = True
    except FileExistsError:
        pass
    try:
        return run(repo, gitdir, target, carry, state)
    except BaseException:
        state["status"] = "failed"
        save_state(carry, state)
        raise
    finally:
        # half-moved checkout: lifting the pause would let the sweep see it, so keep it
        half_moved = state["phase"] in ("moving", "pruning")
        if made_pause and not half_moved and os.path.exists(pause):
            try:
                with open(pause) as fh:
                    ours = json.load(fh).get("run") == stamp
            except (OSError, ValueError):
                ours = False
            if ours:
                os.remove(pause)


def run(repo, gitdir, target, carry, state):
    def phase(name):
        state["phase"] = name
        save_state(carry, state)

    # 2. save everything first
    phase("saving")
    bundle = os.path.join(carry, "old-history.bundle")
    git(repo, "bundle", "create", bundle, "--all")
    git(repo, "bundle", "verify", bundle)
    with open(os.path.join(carry, "uncommitted.patch"), "w") as fh:
        fh.write(git(repo, "diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD").stdout)
    old = set(git(repo, "ls-files", "-z").stdout.split("\0")) - {""}
    new = set(git(repo, "ls-tree", "-r", "-z", "--name-only", target).stdout.split("\0")) - {""}
    doomed = sorted(p for p in old - new if os.path.lexists(os.path.join(repo, p))
                    and not os.path.isdir(os.path.join(repo, p)))
    for rel in doomed:
        copy_any(os.path.join(repo, rel), os.path.join(carry, "files", rel))
    clash = collisions(repo, new)
    for rel in clash:
        copy_any(os.path.join(repo, rel), os.path.join(carry, "collisions", rel))
    state["collisions"] = len(clash)
    ident = git(repo, "config", "--local", "lodestar.id", check=False).stdout.strip()
    phase("saved")

    # 3. move onto the new history and drop every old ref
    phase("moving")
    git(repo, "checkout", "-q", "-f", "-B", "main", target)
    git(repo, "reset", "-q", "--hard", target)
    git(repo, "branch", "--set-upstream-to=origin/main", "main")
    phase("pruning")
    for ref in git(repo, "for-each-ref", "--format=%(refname)", "refs/heads", "refs/notes",
                   "refs/original", "refs/stash").stdout.split():
        if ref != "refs/heads/main":
            git(repo, "update-ref", "-d", ref)
    git(repo, "remote", "prune", "origin", check=False)
    git(repo, "reflog", "expire", "--expire=now", "--all")
    git(repo, "gc", "-q", "--prune=now", check=False)

    # 4. put back machine-local state the new history ignores; keep the rest aside
    phase("restoring")
    restored, aside = 0, []
    for rel in doomed:
        if git(repo, "check-ignore", "-q", "--", rel, check=False).returncode == 0:
            copy_any(os.path.join(carry, "files", rel), os.path.join(repo, rel))
            restored += 1
        else:
            aside.append(rel)
    patch = os.path.join(carry, "uncommitted.patch")
    applied = "no uncommitted changes"
    if os.path.getsize(patch):
        clean = git(repo, "apply", "--check", patch, check=False).returncode == 0
        if clean:
            clean = git(repo, "apply", patch, check=False).returncode == 0
        applied = ("uncommitted changes re-applied" if clean else
                   "uncommitted changes NOT re-applied (left in %s)" % patch)
        if not clean:
            state["problems"].append("patch-not-reapplied")
    if ident and git(repo, "config", "--local", "lodestar.id", check=False).stdout.strip() != ident:
        git(repo, "config", "--local", "lodestar.id", ident)
    state["status"] = "incomplete" if state["problems"] else "complete"
    phase("done")

    print("Lodestar moved onto the rewritten history: main = %s"
          % git(repo, "rev-parse", "--short", "HEAD").stdout.strip())
    print("  old history (all refs) bundled, machine-local only: %s" % bundle)
    print("  %d ignored machine-local file(s) restored; %s" % (restored, applied))
    if state["collisions"]:
        print("  %d untracked/ignored file(s) the checkout overwrote saved in %s"
              % (state["collisions"], os.path.join(carry, "collisions")))
    if aside:
        print("  %d file(s) no longer in the repo kept aside in %s"
              % (len(aside), os.path.join(carry, "files")))
    print("  NEVER push from any other old clone of Lodestar; delete such clones.")
    if state["problems"]:
        print("INCOMPLETE: %s" % ", ".join(state["problems"]))
        return EXIT_INCOMPLETE
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
