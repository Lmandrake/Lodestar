"""Build: the source repo's pushed, clean HEAD -> `~/dev/.lodestar/publish/<repo>/tree`.

Refuses: a dirty or unpushed source, symlinks and submodules anywhere in the commit, a selected
binary outside the manifest's `binaries`, any selected file over `max_file_bytes`, a selected
`.gitattributes`/`.gitmodules`, a replacement or allowance that no longer matches anything, and
an include glob that selects nothing (a stale manifest is an error, not a quieter export).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Dict, List

try:
    from lib.publish import manifest as manifest_mod
    from lib.publish.common import (PublishError, cat_blobs, git, git_env, is_binary, list_tree,
                                    private_dir, sha256_bytes, source_dir, tree_dir, tree_hash,
                                    work_dir, write_json)
except ImportError:  # pragma: no cover
    from publish import manifest as manifest_mod  # type: ignore
    from publish.common import (PublishError, cat_blobs, git, git_env, is_binary,  # type: ignore
                                list_tree, private_dir, sha256_bytes, source_dir, tree_dir,
                                tree_hash, work_dir, write_json)

NEVER = (".gitattributes", ".gitmodules")


def source_state(src: str) -> Dict[str, str]:
    """HEAD sha, refusing anything but a clean checkout whose HEAD is on its upstream."""
    if not os.path.isdir(os.path.join(src, ".git")):
        raise PublishError("no git checkout at %s" % src)
    status = git(src, "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none")
    if status.strip():
        raise PublishError("source %s is dirty; commit and push first" % src)
    head = git(src, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
    upstream = git(src, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}",
                   check=False).decode().strip()
    if not upstream or upstream == "@{upstream}":
        raise PublishError("source %s has no upstream; HEAD is not pushed" % src)
    # Ask the remote itself, not the local tracking ref (which can be stale or hand-advanced).
    branch = git(src, "rev-parse", "--abbrev-ref", "HEAD").decode().strip()
    remote = git(src, "config", "--get", "branch.%s.remote" % branch, check=False).decode().strip()
    merge = git(src, "config", "--get", "branch.%s.merge" % branch, check=False).decode().strip()
    if not remote or not merge or remote == ".":
        raise PublishError("source %s branch %s has no remote upstream" % (src, branch))
    live = git(src, "ls-remote", "--exit-code", remote, merge, check=False).decode().split()
    if not live:
        raise PublishError("could not read %s %s from the remote" % (remote, merge))
    pushed = subprocess.run(["git", "-C", src, "merge-base", "--is-ancestor", head, live[0]],
                            capture_output=True, env=git_env()).returncode == 0
    if not pushed:
        raise PublishError("source HEAD %s is not on %s (unpushed)" % (head[:12], upstream))
    return {"head": head, "upstream": upstream}


def build(repo: str, store: str = None) -> Dict:
    publish = private_dir(store)
    man = manifest_mod.load(repo, publish)
    src = source_dir(repo)
    if not os.path.isdir(os.path.join(src, ".git")):
        raise PublishError("no git checkout at %s" % src)
    # The commit's own shape first: a submodule also makes `status` report the tree dirty.
    entries = list_tree(src, "HEAD")
    for mode, kind, _sha, rel in entries:
        if mode == "120000":
            raise PublishError("symlink in source commit: %s" % rel)
        if mode == "160000" or kind == "commit":
            raise PublishError("submodule in source commit: %s" % rel)
        if kind != "blob" or mode not in ("100644", "100755"):
            raise PublishError("unsupported entry %s (mode %s)" % (rel, mode))
    state = source_state(src)
    if list_tree(src, state["head"]) != entries:
        raise PublishError("source HEAD moved during build; retry")
    all_rels = [e[3] for e in entries]
    unused = man.include.unused(all_rels)
    if unused:
        raise PublishError("include glob(s) select nothing (stale manifest): %s" % ", ".join(unused))
    chosen = [e for e in entries if man.selected(e[3])]
    generated = {g["path"] for g in man.generate}
    for _m, _k, _s, rel in chosen:
        if rel.split("/")[-1] in NEVER:
            raise PublishError("%s may not be exported (it changes how git treats files)" % rel)
    blobs = cat_blobs(src, [e[2] for e in chosen if e[3] not in generated])
    files: Dict[str, Dict] = {}
    for mode, _k, sha, rel in chosen:
        if rel in generated:
            continue
        files[rel] = {"data": blobs[sha], "mode": mode}
    for gen in man.generate:
        if gen["path"].split("/")[-1] in NEVER:
            raise PublishError("%s may not be generated" % gen["path"])
        files[gen["path"]] = {"data": gen["content"], "mode": "100644"}
    replaced = {id(r): 0 for r in man.replace}
    for rel, info in files.items():
        data = info["data"]
        binary = is_binary(data)
        if binary and not man.binaries.match(rel):
            raise PublishError("unlisted binary %s (add it to binaries or exclude it)" % rel)
        if len(data) > man.max_file_bytes:
            raise PublishError("%s is %d bytes, over max_file_bytes %d" % (rel, len(data),
                                                                          man.max_file_bytes))
        if binary:
            continue
        text = data.decode("utf-8")
        for rule in man.replace:
            if rule["paths"].match(rel) and rule["find"] in text:
                replaced[id(rule)] += text.count(rule["find"])
                text = text.replace(rule["find"], rule["with"])
        info["data"] = text.encode("utf-8")
        if len(info["data"]) > man.max_file_bytes:
            raise PublishError("%s is %d bytes after replacements, over max_file_bytes" % (
                rel, len(info["data"])))
    stale = [r["paths_text"] + ": " + r["find"][:40] for r in man.replace if not replaced[id(r)]]
    if stale:
        raise PublishError("replacement(s) changed nothing (stale manifest): %s" % "; ".join(stale))
    if not files:
        raise PublishError("manifest selects no files")
    out = tree_dir(repo)
    parent = work_dir(repo)
    os.makedirs(parent, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="tree-", dir=parent)
    try:
        for rel, info in sorted(files.items()):
            full = os.path.join(tmp, *rel.split("/"))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as fh:
                fh.write(info["data"])
            os.chmod(full, 0o755 if info["mode"] == "100755" else 0o644)
        if os.path.exists(out):
            shutil.rmtree(out)
        os.replace(tmp, out)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    record = {"repo": repo, "source_head": state["head"], "upstream": state["upstream"],
              "manifest_sha256": man.sha256, "tree_hash": tree_hash(out), "files": len(files),
              "selected": sorted(files), "excluded_count": len(entries) - len(chosen)}
    write_json(os.path.join(parent, "build.json"), record)
    return record
