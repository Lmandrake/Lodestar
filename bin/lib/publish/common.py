"""Locations and shared helpers for the exporter. Every location fails closed by name."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from lib import privatestore, paths
except ImportError:  # pragma: no cover
    import privatestore  # type: ignore
    import paths  # type: ignore

SOURCES_ENV = "PUBLISH_SOURCE_ROOT"
DEFAULT_SOURCES = "~/dev"
NOREPLY = "313496996+Lmandrake@users.noreply.github.com"
PUBLIC_NAME = "Lukas Mandrake"
_REPO_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")


class PublishError(Exception):
    """Any refusal. The pipeline stops and nothing is pushed."""


def check_repo_name(repo: str) -> str:
    if not _REPO_NAME.match(repo or "") or ".." in repo:
        raise PublishError("bad repo name %r" % repo)
    return repo


def private_dir(store: Optional[str] = None) -> str:
    """`<private store>/publish`, verified by privatestore.root(): no checkout, no export."""
    try:
        base = privatestore.root(store)
    except privatestore.Unavailable as error:
        raise PublishError("private store unavailable, refusing to publish: %s" % error)
    out = os.path.join(base, "publish")
    if not os.path.isdir(out):
        raise PublishError("no publish directory at %s" % out)
    if os.path.islink(out):
        raise PublishError("%s is a symlink" % out)
    return out


def source_dir(repo: str) -> str:
    base = os.path.expanduser(os.environ.get(SOURCES_ENV) or DEFAULT_SOURCES)
    return os.path.join(base, check_repo_name(repo))


def work_dir(repo: str) -> str:
    """Machine-local: `~/dev/.lodestar/publish/<repo>` (never synced, never committed)."""
    return paths.state_file("publish", check_repo_name(repo))


def tree_dir(repo: str) -> str:
    return os.path.join(work_dir(repo), "tree")


def staging_dir(repo: str) -> str:
    return os.path.join(work_dir(repo), "staging")


def git(repo: str, *args: str, input_bytes: Optional[bytes] = None,
        env: Optional[Dict[str, str]] = None, check: bool = True) -> bytes:
    full_env = git_env(env)
    out = subprocess.run(["git", "-C", repo] + list(args), input=input_bytes,
                         capture_output=True, env=full_env, timeout=300)
    if check and out.returncode != 0:
        raise PublishError("git %s failed in %s: %s" % (
            " ".join(args[:3]), repo, out.stderr.decode("utf-8", "replace").strip()[:400]))
    return out.stdout


def git_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """No inherited GIT_* (GIT_DIR, GIT_INDEX_FILE, GIT_CONFIG_COUNT...), and replacement refs
    off: `git replace` changes what reads see but not what a push transfers."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(extra or {})
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    return env


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def walk_tree(root: str) -> List[Tuple[str, str]]:
    """(relative posix path, absolute path) of every file under `root`, sorted. A symlink or
    anything that is not a regular file or directory is refused."""
    out: List[Tuple[str, str]] = []
    for dirpath, dirs, names in os.walk(root):
        dirs.sort()
        for name in list(dirs) + names:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                raise PublishError("symlink in tree: %s" % os.path.relpath(full, root))
        for name in sorted(names):
            full = os.path.join(dirpath, name)
            if not os.path.isfile(full):
                raise PublishError("not a regular file: %s" % os.path.relpath(full, root))
            out.append((os.path.relpath(full, root).replace(os.sep, "/"), full))
    out.sort()
    return out


def tree_hash(root: str) -> str:
    """sha256 over NUL-separated `mode, path, sha256(content)` records (a path cannot hold NUL,
    so no two trees serialise alike): what the files ARE, read from disk."""
    h = hashlib.sha256()
    for rel, full in walk_tree(root):
        mode = "100755" if os.access(full, os.X_OK) else "100644"
        h.update(b"%s\0%s\0%s\0" % (mode.encode(), rel.encode("utf-8", "surrogateescape"),
                                     sha256_file(full).encode()))
    return h.hexdigest()


def read_list(path: str, what: str) -> List[str]:
    """A private list (names/terms/hosts): one entry per line, `#` comments. Missing or empty
    is a refusal: an empty list would make its scanner silently pass."""
    if not os.path.isfile(path):
        raise PublishError("%s missing at %s" % (what, path))
    with open(path, encoding="utf-8") as fh:
        items = [l.strip() for l in fh if l.strip() and not l.strip().startswith("#")]
    if not items:
        raise PublishError("%s at %s is empty" % (what, path))
    return items


def append_jsonl(path: str, record: Dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def write_json(path: str, data: Dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def read_json(path: str) -> Optional[Dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def materialize(gitdir_repo: str, rev: str, dest: str) -> None:
    """Write commit `rev`'s files into empty `dest` from blobs (ls-tree + cat-file), NOT git
    archive: archive honours in-tree .gitattributes, so `export-subst` would write the private
    sha into the output and `export-ignore` would silently drop files."""
    entries = list_tree(gitdir_repo, rev)
    for mode, kind, sha, rel in entries:
        if kind != "blob" or mode not in ("100644", "100755"):
            raise PublishError("%s is not a plain file (mode %s)" % (rel, mode))
    blobs = cat_blobs(gitdir_repo, [e[2] for e in entries])
    for mode, _kind, sha, rel in entries:
        full = os.path.join(dest, *rel.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(blobs[sha])
        os.chmod(full, 0o755 if mode == "100755" else 0o644)


def list_tree(repo: str, rev: str) -> List[Tuple[str, str, str, str]]:
    out = git(repo, "ls-tree", "-r", "-z", "--full-tree", rev)
    entries = []
    for rec in out.split(b"\0"):
        if not rec:
            continue
        meta, _, path = rec.partition(b"\t")
        mode, kind, sha = meta.decode().split()
        entries.append((mode, kind, sha, path.decode("utf-8", "surrogateescape")))
    return entries


def cat_blobs(repo: str, shas: Sequence[str]) -> Dict[str, bytes]:
    unique = sorted(set(shas))
    if not unique:
        return {}
    raw = git(repo, "cat-file", "--batch", input_bytes=("\n".join(unique) + "\n").encode())
    out: Dict[str, bytes] = {}
    pos = 0
    for sha in unique:
        nl = raw.index(b"\n", pos)
        header = raw[pos:nl].decode().split()
        if len(header) != 3 or header[0] != sha:
            raise PublishError("cat-file answered %r for %s" % (header, sha))
        if header[1] != "blob":
            raise PublishError("%s is a %s, not a blob" % (sha, header[1]))
        size = int(header[2])
        end = nl + 1 + size
        if len(raw) < end + 1 or raw[end:end + 1] != b"\n":
            raise PublishError("cat-file output truncated at %s" % sha)
        out[sha] = raw[nl + 1:end]
        pos = end + 1
    if pos != len(raw):
        raise PublishError("cat-file returned unexpected trailing output")
    return out


def is_binary(data: bytes) -> bool:
    if b"\0" in data[:8192]:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def iter_lines(items: Iterable[str]) -> Iterable[str]:  # pragma: no cover - convenience
    for item in items:
        yield item
