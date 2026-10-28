"""A repo's export manifest: `<private store>/publish/<repo>.toml`. Default-deny.

    repo = "Dispatch"                 # must equal the file's stem
    public_remote = ""                # empty: release pushes only to a machine-local dry remote
    require_gitleaks = true           # default true: no gitleaks, no scan
    max_file_bytes = 1048576          # default 1 MB; larger files are refused, never truncated
    include = ["README.md", "dispatchlib/**"]
    exclude = ["routing/dispositions.jsonl"]   # exclude beats include, always
    binaries = ["docs/img/*.png"]     # a binary file outside these globs is refused
    smoke = ["python3 -m pytest -q"]  # recorded for the fresh-clone smoke test

    [[replace]]                       # literal text replacement in included text files
    paths = "dispatchlib/*.py"
    find = "old"
    with = "new"                      # a rule that changes nothing is an error (stale manifest)

    [[generate]]                      # a file the export writes (README, demo data)
    path = "README.md"
    from = "Dispatch/README.md"       # relative to the publish dir; or text = "..."

    [[allow]]                         # one accepted scanner hit, bound to the file's content
    path = "tests/test_x.py"
    sha256 = "<sha256 of the exported file>"
    class = "email"
    match = "someone@example.org"     # optional: only this exact match
    reason = "why this is not a leak"

Globs match the whole repo-relative path: `*` and `?` stay inside one path segment, `**` crosses
segments, and a trailing `/` means everything below.
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Optional

try:
    from lib.publish.common import PublishError, check_repo_name, sha256_file
except ImportError:  # pragma: no cover
    from publish.common import PublishError, check_repo_name, sha256_file  # type: ignore

DEFAULT_MAX_BYTES = 1 << 20
_KNOWN = {"repo", "public_remote", "require_gitleaks", "max_file_bytes", "include", "exclude",
          "binaries", "smoke", "replace", "generate", "allow"}
_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")


def glob_regex(pattern: str) -> "re.Pattern":
    if not isinstance(pattern, str) or not pattern.strip():
        raise PublishError("empty glob")
    pat = pattern.strip()
    if pat.startswith("/") or "\\" in pat or any(p == ".." for p in pat.split("/")):
        raise PublishError("glob %r must be repo-relative, forward slashes, no '..'" % pattern)
    if pat.endswith("/"):
        pat += "**"
    out, i = [], 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    return re.compile(r"\A" + "".join(out) + r"\Z", re.S)


class Globs:
    def __init__(self, patterns: List[str], field: str):
        if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
            raise PublishError("%s must be a list of strings" % field)
        self.patterns = list(patterns)
        self.regexes = [glob_regex(p) for p in patterns]

    def match(self, rel: str) -> bool:
        return any(r.match(rel) for r in self.regexes)

    def unused(self, rels: List[str]) -> List[str]:
        return [p for p, r in zip(self.patterns, self.regexes) if not any(r.match(x) for x in rels)]


def _safe_rel(rel: str, field: str) -> str:
    if not isinstance(rel, str) or not rel or rel.startswith("/") or "\\" in rel:
        raise PublishError("%s: bad path %r" % (field, rel))
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts) or parts[0] == ".git" or ".git" in parts:
        raise PublishError("%s: bad path %r" % (field, rel))
    return rel


class Manifest:
    def __init__(self, data: Dict, path: str, publish_dir: str):
        unknown = set(data) - _KNOWN
        if unknown:
            raise PublishError("%s: unknown field(s) %s" % (path, ", ".join(sorted(unknown))))
        self.path = path
        self.sha256 = sha256_file(path)
        self.publish_dir = publish_dir
        self.repo = check_repo_name(str(data.get("repo", "")))
        stem = os.path.splitext(os.path.basename(path))[0]
        if self.repo != stem:
            raise PublishError("%s names repo %r; the file must be <repo>.toml" % (path, self.repo))
        self.public_remote = str(data.get("public_remote", "") or "").strip()
        self.require_gitleaks = data.get("require_gitleaks", True)
        if not isinstance(self.require_gitleaks, bool):
            raise PublishError("require_gitleaks must be true or false")
        self.max_file_bytes = data.get("max_file_bytes", DEFAULT_MAX_BYTES)
        if not isinstance(self.max_file_bytes, int) or not 0 < self.max_file_bytes <= DEFAULT_MAX_BYTES * 50:
            raise PublishError("max_file_bytes must be a positive int (<= 50 MB)")
        if not data.get("include"):
            raise PublishError("%s: include is empty; default-deny would export nothing" % path)
        self.include = Globs(data.get("include"), "include")
        self.exclude = Globs(data.get("exclude", []), "exclude")
        self.binaries = Globs(data.get("binaries", []), "binaries")
        self.smoke = data.get("smoke", [])
        if not isinstance(self.smoke, list) or not all(isinstance(s, str) for s in self.smoke):
            raise PublishError("smoke must be a list of strings")
        self.replace = [self._replace(r) for r in self._tables(data, "replace")]
        self.generate = [self._generate(g) for g in self._tables(data, "generate")]
        self.allow = [self._allow(a) for a in self._tables(data, "allow")]

    @staticmethod
    def _tables(data: Dict, key: str) -> List[Dict]:
        items = data.get(key, [])
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            raise PublishError("%s must be an array of tables ([[%s]])" % (key, key))
        return items

    @staticmethod
    def _replace(rule: Dict) -> Dict:
        if set(rule) - {"paths", "find", "with"} or not rule.get("find") \
                or not isinstance(rule.get("with", None), str):
            raise PublishError("[[replace]] needs exactly paths, find, with: %r" % rule)
        return {"paths": glob_regex(str(rule.get("paths", "**"))),
                "paths_text": str(rule.get("paths", "**")),
                "find": str(rule["find"]), "with": rule["with"]}

    def _generate(self, rule: Dict) -> Dict:
        if set(rule) - {"path", "from", "text"} or (("from" in rule) == ("text" in rule)):
            raise PublishError("[[generate]] needs path and exactly one of from/text: %r" % rule)
        rel = _safe_rel(rule.get("path"), "[[generate]]")
        if "from" in rule:
            src = os.path.realpath(os.path.join(self.publish_dir, _safe_rel(rule["from"], "from")))
            if not src.startswith(os.path.realpath(self.publish_dir) + os.sep):
                raise PublishError("[[generate]] from %r leaves the publish dir" % rule["from"])
            if os.path.islink(os.path.join(self.publish_dir, rule["from"])) or not os.path.isfile(src):
                raise PublishError("[[generate]] source missing or a symlink: %s" % src)
            with open(src, "rb") as fh:
                content = fh.read()
        else:
            content = str(rule["text"]).encode("utf-8")
        return {"path": rel, "content": content}

    @staticmethod
    def _allow(rule: Dict) -> Dict:
        if set(rule) - {"path", "sha256", "class", "match", "reason"}:
            raise PublishError("[[allow]] unknown field: %r" % rule)
        for field in ("path", "sha256", "class", "reason"):
            if not isinstance(rule.get(field), str) or not rule[field].strip():
                raise PublishError("[[allow]] needs %s: %r" % (field, rule))
        if not _HEX64.match(rule["sha256"]):
            raise PublishError("[[allow]] sha256 must be 64 lowercase hex: %r" % rule)
        return {"path": rule["path"], "sha256": rule["sha256"], "class": rule["class"],
                "match": rule.get("match"), "reason": rule["reason"]}

    def selected(self, rel: str) -> bool:
        """Default-deny: included AND not excluded. Exclude always wins."""
        return self.include.match(rel) and not self.exclude.match(rel)


def load(repo: str, publish_dir: str) -> Manifest:
    try:
        import tomllib  # Python 3.11+
    except ImportError:  # pragma: no cover - /usr/bin/python3 is 3.9
        raise PublishError("publish needs Python 3.11+ (tomllib); run it with python3.12")
    path = os.path.join(publish_dir, "%s.toml" % check_repo_name(repo))
    if not os.path.isfile(path) or os.path.islink(path):
        raise PublishError("no manifest at %s" % path)
    with open(path, "rb") as fh:
        try:
            data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as error:
            raise PublishError("%s: %s" % (path, error))
    return Manifest(data, path, publish_dir)


def allowance_for(manifest: Manifest, hit: Dict) -> Optional[Dict]:
    for rule in manifest.allow:
        if (rule["path"] == hit["path"] and rule["sha256"] == hit["sha256"]
                and rule["class"] == hit["class"]
                and (rule["match"] is None or rule["match"] == hit["match"])):
            return rule
    return None
