"""Diff, release, the pre-push hook check, and verify.

Release order (any failure stops it; nothing is pushed):
  1. the built tree's hash equals build.json's (the tree came from `build`, unedited),
  2. the latest scan report passed on that identical tree hash and manifest,
  3. `diff` was shown for that identical tree hash (the human gate),
  3b. a cold second-model read (`review`) answered CLEAN on that identical tree and manifest,
      or the owner ruled over its FINDINGS receipt (`rule`); the record says which,
  4. one commit in the machine-local staging clone, author and committer the noreply identity,
     a generated message naming no private sha, git's own config and hooks neutralised,
  5. the staging clone's pre-push hook re-scans the commit being pushed; without a
     `public_remote` the same check runs directly and nothing is pushed,
  6. `git push` without force; a record appended to `<publish>/releases.jsonl`.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from typing import Dict, List, Optional

try:
    from lib.publish import manifest as manifest_mod
    from lib.publish import review as review_mod
    from lib.publish import scan as scan_mod
    from lib.publish.common import (NOREPLY, PUBLIC_NAME, PublishError, append_jsonl,
                                    check_repo_name, git, git_env, materialize, private_dir, read_json,
                                    staging_dir, tree_dir, tree_hash, walk_tree, work_dir,
                                    write_json)
except ImportError:  # pragma: no cover
    from publish import manifest as manifest_mod  # type: ignore
    from publish import review as review_mod  # type: ignore
    from publish import scan as scan_mod  # type: ignore
    from publish.common import (NOREPLY, PUBLIC_NAME, PublishError,  # type: ignore
                                append_jsonl, check_repo_name, git, git_env, materialize, private_dir,
                                read_json, staging_dir, tree_dir, tree_hash, walk_tree,
                                work_dir, write_json)

ZERO = "0" * 40
PUBLISH_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "publish.py")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_env() -> Dict[str, str]:
    """Git sees no global/system config (no includeIf identity, no hooksPath, no signing)."""
    env = git_env()
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_ATTR_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_AUTHOR_NAME": PUBLIC_NAME, "GIT_AUTHOR_EMAIL": NOREPLY,
                "GIT_COMMITTER_NAME": PUBLIC_NAME, "GIT_COMMITTER_EMAIL": NOREPLY,
                "GIT_TERMINAL_PROMPT": "0"})
    return env


def _sgit(repo: str, *args: str, check: bool = True, input_bytes: Optional[bytes] = None) -> str:
    return git(repo, *args, env=_clean_env(), check=check,
               input_bytes=input_bytes).decode("utf-8", "replace")


def _has_head(repo: str) -> bool:
    return bool(_sgit(repo, "rev-parse", "--verify", "-q", "HEAD^{commit}", check=False).strip())


# ---- diff: the human gate ---------------------------------------------------------------------

def _released_tree(repo: str, dest: str) -> Optional[str]:
    """Materialise the last released tree (the staging clone's HEAD) into `dest`."""
    stage = staging_dir(repo)
    if os.path.isdir(os.path.join(stage, ".git")) and _has_head(stage):
        materialize(stage, "HEAD", dest)
        return _sgit(stage, "rev-parse", "HEAD").strip()
    return None


def diff(repo: str, store: Optional[str] = None) -> Dict:
    private_dir(store)  # fail closed before anything else
    tree = tree_dir(repo)
    if not os.path.isdir(tree):
        raise PublishError("no built tree for %s; run build first" % repo)
    th = tree_hash(tree)
    with tempfile.TemporaryDirectory(prefix="publish-diff-") as tmp:
        old = os.path.join(tmp, "released")
        os.makedirs(old)
        base = _released_tree(repo, old)
        args = ["diff", "--no-index", "--no-color", "--no-ext-diff", "--no-textconv", "--binary"]
        stat_out = subprocess.run(["git"] + args[:-1] + ["--shortstat", old, tree],
                                  capture_output=True, env=_clean_env(), cwd=tmp)
        patch = subprocess.run(["git"] + args + [old, tree], capture_output=True,
                               env=_clean_env(), cwd=tmp)
        if stat_out.returncode not in (0, 1) or patch.returncode not in (0, 1):
            raise PublishError("git diff failed: %s" % patch.stderr.decode()[:300])
        text = patch.stdout.decode("utf-8", "replace").replace(old, "a").replace(tree, "b")
        summary = stat_out.stdout.decode("utf-8", "replace").replace(old, "a").replace(tree, "b")
    out = os.path.join(work_dir(repo), "diff.patch")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text)
    record = {"repo": repo, "tree_hash": th, "base": base, "patch": out, "shown_at": _now(),
              "changed": bool(text.strip()), "stat_tail": summary.strip().splitlines()[-1:]}
    write_json(os.path.join(work_dir(repo), "diff.json"), record)
    return record


# ---- the check the pre-push hook runs ---------------------------------------------------------

def check_commits(repo: str, gitdir: str, local: str, remote_sha: str, man, lists,
                  forbidden_shas: List[str]) -> Dict:
    """Every commit being pushed: noreply author+committer, message free of hits and private
    shas; the tip's tree scanned in full. Returns the scan report of the tip."""
    rng = local if remote_sha in ("", ZERO) else "%s..%s" % (remote_sha, local)
    commits = _sgit(gitdir, "rev-list", rng).split()
    if not commits:
        raise PublishError("nothing to push")
    for sha in commits:
        meta = _sgit(gitdir, "log", "-1", "--format=%ae%x00%ce%x00%an%x00%cn", sha).strip()
        ae, ce, an, cn = meta.split("\0")
        if ae != NOREPLY or ce != NOREPLY or an != PUBLIC_NAME or cn != PUBLIC_NAME:
            raise PublishError("commit %s identity is %s/%s, not the noreply" % (sha[:12], ae, ce))
        body = _sgit(gitdir, "log", "-1", "--format=%B", sha)
        parents = _sgit(gitdir, "rev-list", "--parents", "-n", "1", sha).split()[1:]
        if len(parents) > 1:
            raise PublishError("merge commit %s in a public push" % sha[:12])
        if any(full[:7].lower() in body.lower() for full in forbidden_shas if full):
            raise PublishError("commit %s message names a private sha" % sha[:12])
        msg_hits = scan_mod.text_hits(scan_mod.normalise(body), lists)
        if msg_hits:
            raise PublishError("commit %s message hits %s" % (sha[:12], sorted({h[0] for h in msg_hits})))
    # Every pushed commit's tree, not just the tip: a leak added then deleted is still public.
    # Older trees fail on hits only (the manifest's allowances describe the tip, so they may
    # lapse there legitimately); the tip's report is returned whole.
    by_tree: Dict[str, Dict] = {}
    for sha in reversed(commits):            # oldest first
        tree = _sgit(gitdir, "rev-parse", "%s^{tree}" % sha).strip()
        if tree not in by_tree:
            with tempfile.TemporaryDirectory(prefix="publish-hook-") as tmp:
                dest = os.path.join(tmp, "tree")
                os.makedirs(dest)
                materialize(gitdir, sha, dest)
                by_tree[tree] = scan_mod.scan_tree(dest, man, lists)
        rep = dict(by_tree[tree], commit=sha)
        if sha != commits[0] and rep["hits"]:
            rep["passed"] = False
            return rep
    return rep


def hook(repo: str, gitdir: str, stdin_text: str, store: Optional[str] = None) -> int:
    """The staging clone's pre-push hook: refuse unless every pushed ref passes."""
    publish = private_dir(store)
    man = manifest_mod.load(repo, publish)
    lists = scan_mod.load_lists(publish)
    forbidden = _private_shas(repo)
    lines = [l.split() for l in stdin_text.splitlines() if l.strip()]
    if not lines:
        raise PublishError("pre-push hook got no refs")
    for parts in lines:
        if len(parts) != 4:
            raise PublishError("unexpected pre-push line %r" % parts)
        local_ref, local, remote_ref, remote_sha = parts
        if local == ZERO:
            raise PublishError("refusing to delete %s on the public remote" % remote_ref)
        if remote_ref != "refs/heads/main":
            raise PublishError("public pushes go to refs/heads/main only, not %s" % remote_ref)
        report = check_commits(repo, gitdir, local, remote_sha, man, lists, forbidden)
        path = scan_mod.write_report(publish, report, prefix="prepush-")
        if not report["passed"]:
            raise PublishError("pre-push scan found %d hit(s): %s" % (len(report["hits"]), path))
    return 0


HOOK = """#!/bin/sh
# Installed by Lodestar publish.py: re-scan everything pushed to the public mirror.
exec "{python}" "{publish}" hook "{repo}" --git-dir "$(git rev-parse --absolute-git-dir)"
"""


def _install_hook(stage: str, repo: str) -> str:
    hooks = os.path.join(stage, ".git", "hooks")
    os.makedirs(hooks, exist_ok=True)
    path = os.path.join(hooks, "pre-push")
    with open(path, "w") as fh:
        fh.write(HOOK.format(python=sys.executable, publish=PUBLISH_PY, repo=repo))
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return hooks


def _private_shas(repo: str) -> List[str]:
    out = []
    build = read_json(os.path.join(work_dir(repo), "build.json")) or {}
    if build.get("source_head"):
        out.append(build["source_head"])
    return out


# ---- release ----------------------------------------------------------------------------------

def _prepare_staging(repo: str, remote: str) -> str:
    stage = staging_dir(repo)
    if not os.path.isdir(os.path.join(stage, ".git")):
        if os.path.exists(stage):
            shutil.rmtree(stage)
        os.makedirs(stage)
        _sgit(stage, "init", "-q", "-b", "main")
    if remote:
        urls = _sgit(stage, "remote", "get-url", "--all", "origin", check=False).split()
        if not urls:
            _sgit(stage, "remote", "add", "origin", remote)
        _check_destination(stage, remote)
        _sgit(stage, "fetch", "-q", "origin")
        theirs = _sgit(stage, "rev-parse", "--verify", "-q", "refs/remotes/origin/main",
                       check=False).strip()
        ours = _sgit(stage, "rev-parse", "--verify", "-q", "HEAD", check=False).strip()
        if theirs and theirs != ours:
            if ours:
                raise PublishError("public main %s differs from the staging clone's %s; "
                                   "resolve by hand (never force)" % (theirs[:12], ours[:12]))
            _sgit(stage, "reset", "-q", "--hard", theirs)
    elif _sgit(stage, "remote", check=False).strip():
        raise PublishError("staging clone has a remote but the manifest has no public_remote")
    _sgit(stage, "config", "core.hooksPath", os.path.join(stage, ".git", "hooks"))
    _sgit(stage, "config", "commit.gpgsign", "false")
    _sgit(stage, "config", "core.autocrlf", "false")
    _sgit(stage, "config", "core.attributesFile", os.devnull)
    attrs = os.path.join(stage, ".git", "info", "attributes")
    if os.path.exists(attrs):
        os.remove(attrs)        # no attribute may transform what is staged
    for key in ("filter", "diff", "merge"):
        for line in _sgit(stage, "config", "--get-regexp", r"^%s\." % key, check=False).splitlines():
            _sgit(stage, "config", "--unset-all", line.split()[0], check=False)
    _install_hook(stage, repo)
    return stage


def _check_destination(stage: str, remote: str) -> None:
    """Fetch AND push URLs of every remote are exactly the manifest's, and nothing rewrites
    URLs (`url.<x>.insteadOf/pushInsteadOf`) or adds another destination."""
    remotes = _sgit(stage, "remote", check=False).split()
    if remotes != ["origin"]:
        raise PublishError("staging clone has remotes %s; only origin is allowed" % remotes)
    fetch = _sgit(stage, "remote", "get-url", "--all", "origin", check=False).split()
    push = _sgit(stage, "remote", "get-url", "--push", "--all", "origin", check=False).split()
    if fetch != [remote] or push != [remote]:
        raise PublishError("staging origin fetch %s / push %s, manifest says %s" % (fetch, push,
                                                                                 remote))
    rewrites = _sgit(stage, "config", "--get-regexp", r"^url\.|^remote\.pushdefault$",
                     check=False).strip()
    if rewrites:
        raise PublishError("staging clone rewrites URLs: %s" % rewrites.splitlines()[0])


def _committed_hash(stage: str, rev: str) -> str:
    with tempfile.TemporaryDirectory(prefix="publish-commit-") as tmp:
        dest = os.path.join(tmp, "tree")
        os.makedirs(dest)
        materialize(stage, rev, dest)
        return tree_hash(dest)


def _sync_worktree(stage: str, tree: str) -> None:
    for name in os.listdir(stage):
        if name == ".git":
            continue
        full = os.path.join(stage, name)
        if os.path.isdir(full) and not os.path.islink(full):
            shutil.rmtree(full)
        else:
            os.remove(full)
    for rel, full in walk_tree(tree):
        dest = os.path.join(stage, *rel.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(full, dest)
    _sgit(stage, "add", "-A", "--force", ".")


def release(repo: str, approved_by: str, store: Optional[str] = None) -> Dict:
    check_repo_name(repo)
    if not (approved_by or "").strip():
        raise PublishError("--approved-by needs the owner's own words")
    publish = private_dir(store)
    man = manifest_mod.load(repo, publish)
    tree = tree_dir(repo)
    if not os.path.isdir(tree):
        raise PublishError("no built tree; run build")
    th = tree_hash(tree)
    build = read_json(os.path.join(work_dir(repo), "build.json")) or {}
    if build.get("tree_hash") != th:
        raise PublishError("tree changed since build (or no build record); rebuild")
    if build.get("manifest_sha256") != man.sha256:
        raise PublishError("manifest changed since build; rebuild and rescan")
    rpath, report = scan_mod.latest_report(publish, repo)
    if not report or report.get("tree_hash") != th or report.get("manifest_sha256") != man.sha256:
        raise PublishError("no scan of this exact tree and manifest; run scan")
    if not report.get("passed"):
        raise PublishError("latest scan has %d blocking hit(s): %s" % (len(report.get("hits", [])), rpath))
    lists = scan_mod.load_lists(publish)
    if report.get("lists", {}).get("sha256") != lists.digests:
        raise PublishError("names/terms/hosts changed since the scan; rescan")
    shown = read_json(os.path.join(work_dir(repo), "diff.json")) or {}
    if shown.get("tree_hash") != th:
        raise PublishError("diff not shown for this tree; run diff for the human gate")
    receipt = review_mod.require(repo, th, man.sha256, publish)   # the cold second-model read
    ruling = receipt.get("ruling")
    stage = _prepare_staging(repo, man.public_remote)
    before = _sgit(stage, "rev-parse", "--verify", "-q", "HEAD", check=False).strip()
    _sync_worktree(stage, tree)
    if before and not _sgit(stage, "status", "--porcelain").strip():
        raise PublishError("nothing changed since the last release")
    n = len(_releases(publish, repo))
    message = "%s release %d\n\n%d files. Exported from a private working repo.\n" % (
        repo, n + 1, len(walk_tree(tree)))
    try:
        _sgit(stage, "commit", "-q", "-F", "-", input_bytes=message.encode())
        commit = _sgit(stage, "rev-parse", "HEAD").strip()
        if _committed_hash(stage, commit) != th:
            raise PublishError("the committed tree differs from the scanned tree (attributes or "
                               "filters in the staging clone?)")
        if man.public_remote:
            _check_destination(stage, man.public_remote)
        if man.public_remote:
            out = subprocess.run(["git", "-C", stage, "push", "origin", "HEAD:refs/heads/main"],
                                 capture_output=True, text=True, env=_clean_env(), timeout=600)
            if out.returncode != 0:
                raise PublishError("push refused: %s" % out.stderr.strip()[-600:])
            pushed = True
        else:
            hook(repo, os.path.join(stage, ".git"), "HEAD %s refs/heads/main %s\n" % (
                commit, before or ZERO), store)
            pushed = False
    except BaseException:
        if before:
            _sgit(stage, "reset", "-q", "--hard", before, check=False)
        else:
            shutil.rmtree(stage, ignore_errors=True)
        raise
    record = {"repo": repo, "at": _now(), "approved_by": approved_by.strip(),
              "source_head": build.get("source_head"), "tree_hash": th, "public_commit": commit,
              "remote": man.public_remote or None, "pushed": pushed, "scan_report": rpath,
              "manifest_sha256": man.sha256, "cold_read": receipt.get("verdict"),
              "cold_read_findings": receipt.get("findings"),
              "cold_read_answer_sha256": receipt.get("answer_sha256")}
    if ruling:
        record["owner_ruling"] = {"owner_said": ruling["owner_said"], "machine": ruling["machine"],
                                  "ruled_at": ruling["ruled_at"], "findings": ruling["findings"]}
        record["gate"] = ("released on owner ruling over %d findings: \"%s\""
                          % (ruling["findings"], ruling["owner_said"]))
    else:
        record["gate"] = "cold read CLEAN"
    append_jsonl(os.path.join(publish, "releases.jsonl"), record)
    return record


def _releases(publish: str, repo: str) -> List[Dict]:
    path = os.path.join(publish, "releases.jsonl")
    out = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("repo") == repo:
                    out.append(rec)
    return out


# ---- verify -----------------------------------------------------------------------------------

def verify(repo: str, store: Optional[str] = None) -> Dict:
    """Clone the public remote fresh, check every commit's identity, scan HEAD's tree."""
    publish = private_dir(store)
    man = manifest_mod.load(repo, publish)
    if not man.public_remote:
        raise PublishError("%s has no public_remote; nothing published to verify" % repo)
    lists = scan_mod.load_lists(publish)
    with tempfile.TemporaryDirectory(prefix="publish-verify-") as tmp:
        clone = os.path.join(tmp, "clone")
        out = subprocess.run(["git", "clone", "-q", "--no-checkout", man.public_remote, clone],
                             capture_output=True, text=True, env=_clean_env(), timeout=600)
        if out.returncode != 0:
            raise PublishError("clone of %s failed: %s" % (man.public_remote, out.stderr[-300:]))
        head = _sgit(clone, "rev-parse", "HEAD").strip()
        report = check_commits(repo, os.path.join(clone, ".git"), head, ZERO, man, lists,
                               _private_shas(repo))
    report["public_head"] = head
    report["path"] = scan_mod.write_report(publish, report, prefix="verify-")
    return report


def manifests(store: Optional[str] = None) -> List[str]:
    publish = private_dir(store)
    return sorted(n[:-5] for n in os.listdir(publish) if n.endswith(".toml"))
