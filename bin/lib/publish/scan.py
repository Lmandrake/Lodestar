"""Every scanner class over an exported tree. A hit blocks unless a manifest allowance names its
path, the file's exact content hash and its class (and, optionally, the exact match).

Classes:
    marker          proseguard.MARKERS (private-domain address, Outlook id, Teams id), with NO
                    exemption for Lukas's own address: a public tree carries none at all
    nasa_host       any *.nasa.gov host name
    host            an entry of the private hosts.txt (gateway hosts, account names), any case
    name            a full name from the private names.txt, any case, across one line break
    name_form       first/last name, nickname, first+initial, `Last, First`, handle derived from
                    names.txt (nameforms.py); ambiguous words only in a work context
    staffing        FTE / work-year counts
    term            a program/org code from the private terms.txt, exact case, whole words
    home_path       /Users/<name> or <drive>:\\Users\\<name>
    machine_id      a work-Mac key, mt-NNNN
    email           any address but the noreply identity and the RFC 2606/6761 reserved domains
    export_control  ITAR, CUI and kin
    secret          gitleaks (`gitleaks dir`), its default rules; in-tree ignore files ignored

Text is NFKC-normalised and stripped of zero-width characters before matching, binaries are
scanned as decoded bytes too, and every file PATH is scanned as if it were a line. A regex
cannot see paraphrase: default-deny and the human diff are the real control.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

try:
    from lib import proseguard
    from lib.publish import manifest as manifest_mod
    from lib.publish import nameforms
    from lib.publish.common import (NOREPLY, PublishError, read_list, sha256_bytes, sha256_file,
                                    tree_hash, walk_tree, write_json)
except ImportError:  # pragma: no cover
    import proseguard  # type: ignore
    from publish import manifest as manifest_mod  # type: ignore
    from publish import nameforms  # type: ignore
    from publish.common import (NOREPLY, PublishError, read_list, sha256_bytes,  # type: ignore
                                sha256_file, tree_hash, walk_tree, write_json)

CLASSES = ("marker", "nasa_host", "host", "name", "name_form", "staffing", "term", "home_path", "machine_id", "email",
           "export_control", "secret")

_ZERO_WIDTH = re.compile("[­᠎​-‏‪-‮⁠-⁤﻿]")
NASA_HOST = re.compile(r"\b(?:[A-Za-z0-9-]+\.)+nasa\.gov\b", re.I)
HOME_PATH = re.compile(r"/Users/(?!Shared\b)[A-Za-z0-9._-]+|\b[A-Za-z]:\\+Users\\+[A-Za-z0-9._ -]+")
MACHINE_ID = re.compile(r"(?<![A-Za-z0-9])mt-\d{4,}(?!\d)", re.I)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})\b")
_RESERVED = re.compile(r"(?:\A|\.)(?:example\.(?:com|net|org)|example|invalid|test|localhost)\Z",
                       re.I)
EXPORT_CONTROL = (
    re.compile(r"(?<![A-Za-z0-9])(?:ITAR|CUI|EAR99|FOUO|NOFORN|SBU)(?![A-Za-z0-9])"),
    re.compile(r"\bexport[- ]controll?ed\b|\bcontrolled unclassified\b"
               r"|\bsensitive but unclassified\b|\bfor official use only\b"
               r"|\binternational traffic in arms\b", re.I),
)
STAFFING = re.compile(r"\b\d+(?:\.\d+)?\s*(?:FTEs?|WYEs?|work[- ]?years?|full[- ]time[- ]equivalents?)\b", re.I)
_TOKEN = re.compile(r"[^\W_]+")
# Between two tokens of a name: any punctuation/markup/whitespace run (`**Pat** Example`,
# `pat_example`, `Pat\n\nExample`), bounded so unrelated words far apart do not join.
_NAME_GAP = re.compile(r"\A(?=[\W_]{1,12}\Z)(?!(?:[^\n]*\n){3})")
_TERM_GAP = re.compile(r"\A[^\w\n]{1,3}\Z|\A_{1,2}\Z")
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")


def normalise(text: str) -> str:
    """NFKC, then drop every format character (Cf: zero-width, bidi, soft hyphen) and the
    combining grapheme joiner, which render as nothing but split a token."""
    text = _ZERO_WIDTH.sub("", unicodedata.normalize("NFKC", text))
    if text.isascii():
        return text
    return "".join(c for c in text if c != "\u034f" and unicodedata.category(c) != "Cf")


class Phrases:
    """Multi-token phrase matcher, linear in the text: index phrases by first token, then
    compare the following tokens and the gaps between them."""

    def __init__(self, phrases: Sequence[str], fold: bool, gap: "re.Pattern"):
        self.fold, self.gap = fold, gap
        self.index: Dict[str, List[Tuple[str, ...]]] = {}
        for phrase in phrases:
            toks = tuple(self._key(t) for t in _TOKEN.findall(normalise(phrase)))
            if toks:
                self.index.setdefault(toks[0], []).append(toks)
        for seqs in self.index.values():
            seqs.sort(key=len, reverse=True)

    def _key(self, tok: str) -> str:
        return tok.casefold() if self.fold else tok

    def find(self, text: str) -> List[Tuple[int, str]]:
        toks = [(m.start(), m.end(), self._key(m.group(0))) for m in _TOKEN.finditer(text)]
        out: List[Tuple[int, str]] = []
        for i, (start, _end, key) in enumerate(toks):
            for seq in self.index.get(key, ()):
                n = len(seq)
                if i + n > len(toks):
                    continue
                ok = all(toks[i + k][2] == seq[k] and
                         self.gap.match(text[toks[i + k - 1][1]:toks[i + k][0]])
                         for k in range(1, n))
                if ok:
                    out.append((start, text[start:toks[i + n - 1][1]]))
                    break
        return out


class Lists:
    def __init__(self, names: Sequence[str], terms: Sequence[str], hosts: Sequence[str],
                 digests: Dict[str, str]):
        self.forms = nameforms.NameForms(names)
        names = [nameforms.parse_line(n)[0] for n in names]
        self.names = Phrases(names, fold=True, gap=_NAME_GAP)
        self.terms = Phrases(terms, fold=False, gap=_TERM_GAP)
        self.hosts = [h.casefold() for h in (normalise(x).strip() for x in hosts) if h]
        if not self.names.index or not self.terms.index or not self.hosts:
            raise PublishError("a names/terms/hosts list has no usable entry")
        self.counts = {"names": len(names), "terms": len(terms), "hosts": len(hosts)}
        self.digests = digests


def load_lists(publish_dir: str) -> Lists:
    lists, digests = {}, {}
    for key in ("names", "terms", "hosts"):
        path = os.path.join(publish_dir, "%s.txt" % key)
        lists[key] = read_list(path, "%s.txt" % key)
        digests[key] = sha256_file(path)
    return Lists(lists["names"], lists["terms"], lists["hosts"], digests)


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _offset_for_line(text: str, split: str, pos: int) -> int:
    """An offset in `text` on the same line as offset `pos` of `split` (only spaces were
    inserted, so the line number is preserved)."""
    line = split.count("\n", 0, pos)
    at = 0
    for _ in range(line):
        at = text.index("\n", at) + 1
    return at


def text_hits(text: str, lists: Lists) -> List[Tuple[str, int, str]]:
    """(class, offset, match) for every text scanner over already-normalised `text`."""
    out: List[Tuple[str, int, str]] = []
    for _label, pattern in proseguard.MARKERS:     # no own-address exemption here
        out.extend(("marker", m.start(), m.group(0)) for m in pattern.finditer(text))
    out.extend(("nasa_host", m.start(), m.group(0)) for m in NASA_HOST.finditer(text))
    low = text.casefold()
    for host in lists.hosts:
        pos = low.find(host)
        while pos >= 0:
            out.append(("host", pos, text[pos:pos + len(host)]))
            pos = low.find(host, pos + 1)
    out.extend(("name", pos, m) for pos, m in lists.names.find(text))
    split = _CAMEL.sub(" ", text)          # QuentinArbuckle; offsets shift, lines do not
    if split != text:
        found = {m for _p, m in lists.names.find(text)}
        for pos, m in lists.names.find(split):
            if m not in found:
                out.append(("name", _offset_for_line(text, split, pos), m))
    found_forms = {m for _p, m in lists.forms.find(text)}
    out.extend(("name_form", pos, m) for pos, m in lists.forms.find(text))
    if split != text:
        for pos, m in lists.forms.find(split):
            if m not in found_forms:
                out.append(("name_form", _offset_for_line(text, split, pos), m))
    out.extend(("staffing", m.start(), m.group(0)) for m in STAFFING.finditer(text))
    out.extend(("term", pos, m) for pos, m in lists.terms.find(text))
    out.extend(("home_path", m.start(), m.group(0)) for m in HOME_PATH.finditer(text))
    out.extend(("machine_id", m.start(), m.group(0)) for m in MACHINE_ID.finditer(text))
    for m in EMAIL.finditer(text):
        if m.group(0).lower() == NOREPLY.lower() or _RESERVED.search(m.group(1)):
            continue
        out.append(("email", m.start(), m.group(0)))
    for pattern in EXPORT_CONTROL:
        out.extend(("export_control", m.start(), m.group(0)) for m in pattern.finditer(text))
    return out


def decodings(data: bytes) -> List[str]:
    """Every reading of `data` a viewer might show: UTF-8, UTF-16 (BOM or many NULs), and the
    printable-ASCII runs of a binary (PNG tEXt, PDF metadata, zip names), each normalised."""
    out = [data.decode("utf-8", "replace")]
    nuls = data.count(b"\0")
    if data[:2] in (b"\xff\xfe", b"\xfe\xff") or (data and nuls * 4 >= len(data)):
        for codec in ("utf-16-le", "utf-16-be"):
            out.append(data.decode(codec, "replace"))
    if nuls or b"\xef\xbf\xbd" in out[0].encode("utf-8"):
        out.append("\n".join(m.group(0).decode("ascii")
                             for m in re.finditer(rb"[\x20-\x7e\t]{4,}", data)))
    out.extend(unescapings(out[0]))
    return [normalise(t) for t in out]


_B64 = re.compile(r"[A-Za-z0-9+/_-]{16,}={0,2}")
_AT = re.compile(r"\s*[\[({<]\s*at\s*[\])}>]\s*|\s+at\s+(?=[A-Za-z0-9-]+\s*(?:\.|[\[(]\s*dot))", re.I)
_DOT = re.compile(r"\s*[\[({<]\s*dot\s*[\])}>]\s*", re.I)


def unescapings(text: str) -> List[str]:
    """Readings an encoder would hide: JSON/Python \\uXXXX escapes, %-encoding, HTML entities,
    `[at]`/`[dot]` addresses, and base64 runs that decode to UTF-8 text."""
    import html
    import urllib.parse
    out = []
    if "\\u" in text or "\\x" in text:
        out.append(re.sub(r"\\u([0-9a-fA-F]{4})|\\x([0-9a-fA-F]{2})",
                          lambda m: chr(int(m.group(1) or m.group(2), 16)), text))
    if "%" in text:
        out.append(urllib.parse.unquote(text))
    if "&" in text:
        out.append(html.unescape(text))
    if _AT.search(text):
        out.append(_DOT.sub(".", _AT.sub("@", text)))
    decoded = []
    for m in _B64.finditer(text):
        tok = m.group(0)
        try:
            raw = base64.b64decode(tok + "=" * (-len(tok) % 4),
                                   altchars=b"-_" if ("-" in tok or "_" in tok) else None)
            piece = raw.decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            continue
        if piece and sum(c.isprintable() or c.isspace() for c in piece) >= 0.9 * len(piece):
            decoded.append(piece)
    if decoded:
        out.append("\n".join(decoded))
    return out


def gitleaks_version() -> Optional[str]:
    exe = shutil.which("gitleaks")
    if not exe:
        return None
    out = subprocess.run([exe, "version"], capture_output=True, text=True, timeout=60)
    return out.stdout.strip() or "unknown"


def run_gitleaks(root: str) -> List[Dict]:
    """Two passes, unioned: the tree as is (rules keyed on a file name still fire), and a copy
    with every file renamed `f<N>.txt`, because the default config's global allowlist skips by
    PATH (gitleaks.toml, package-lock.json, *.svg, *.png, ...) and `[extend] useDefault` inherits
    it; a neutral name gives every file the content rules."""
    hits = _gitleaks_once(root, {})
    with tempfile.TemporaryDirectory(prefix="publish-neutral-") as tmp:
        names = {}
        for i, (rel, full) in enumerate(walk_tree(root)):
            alias = "f%06d.txt" % i
            names[alias] = rel
            shutil.copyfile(full, os.path.join(tmp, alias))
        hits.extend(_gitleaks_once(tmp, names))
    seen, out = set(), []
    for hit in hits:
        key = (hit["rel"], hit["line"], hit["match"])
        if key not in seen:
            seen.add(key)
            out.append(hit)
    return out


def _gitleaks_once(root: str, aliases: Dict[str, str]) -> List[Dict]:
    """`gitleaks dir` with its default rules. In-tree `.gitleaks.toml`, `.gitleaksignore` and
    `gitleaks:allow` comments are all ignored: the tree being scanned may not exempt itself."""
    exe = shutil.which("gitleaks")
    if not exe:
        raise PublishError("gitleaks missing: the manifest requires it (brew install gitleaks)")
    with tempfile.TemporaryDirectory(prefix="publish-gitleaks-") as tmp:
        config = os.path.join(tmp, "config.toml")
        ignore = os.path.join(tmp, "ignore")
        os.makedirs(ignore)
        with open(config, "w") as fh:
            fh.write("[extend]\nuseDefault = true\n")
        report = os.path.join(tmp, "report.json")
        out = subprocess.run(
            [exe, "dir", "--no-banner", "--redact", "--exit-code", "99", "--log-level", "error",
             "--config", config, "--gitleaks-ignore-path", ignore, "--ignore-gitleaks-allow",
             "--report-format", "json",
             "--report-path", report, root],
            capture_output=True, text=True, timeout=1800, cwd=tmp,
            env={k: v for k, v in os.environ.items() if not k.startswith("GITLEAKS_")})
        if out.returncode not in (0, 99):
            raise PublishError("gitleaks failed (exit %d): %s" % (out.returncode,
                                                                  out.stderr.strip()[:400]))
        try:
            with open(report, encoding="utf-8") as fh:
                found = json.load(fh)
        except (OSError, ValueError) as error:
            raise PublishError("gitleaks wrote no readable report: %s" % error)
        if not isinstance(found, list) or not all(isinstance(f, dict) and f.get("File")
                                                   for f in found):
            raise PublishError("gitleaks report is not a list of findings")
        if out.returncode == 0 and found:
            raise PublishError("gitleaks exit 0 with findings in its report")
        if out.returncode == 99 and not found:
            raise PublishError("gitleaks reported leaks but its report is empty")
        real_root = os.path.realpath(root)
        hits = []
        for item in found:
            path = os.path.realpath(item.get("File", ""))
            rel = os.path.relpath(path, real_root).replace(os.sep, "/")
            rel = aliases.get(rel, rel) if aliases else rel
            hits.append({"rel": rel, "line": int(item.get("StartLine") or 0),
                         "match": "%s (redacted)" % item.get("RuleID", "?")})
        return hits


def _lowercase_words(files, max_bytes: int, wanted) -> Dict[str, int]:
    """How often each name word appears in all-lower-case across the tree (ordinary-word test)."""
    counts: Dict[str, int] = {}
    for _rel, full in files:
        if os.path.getsize(full) > max_bytes:
            continue
        with open(full, "rb") as fh:
            text = fh.read().decode("utf-8", "ignore")
        for w in re.findall(r"(?<![A-Za-z0-9@._-])[a-z]{3,}(?![A-Za-z0-9@._-])", text):
            if w in wanted:
                counts[w] = counts.get(w, 0) + 1
    return counts


def scan_tree(root: str, man, lists: Lists) -> Dict:
    """Scan every file under `root`. Returns the report (not yet written)."""
    files = walk_tree(root)
    hits: List[Dict] = []
    digests: Dict[str, str] = {}
    lists.forms.learn(_lowercase_words(files, man.max_file_bytes, lists.forms.bare))
    for rel, full in files:
        if os.path.getsize(full) > man.max_file_bytes:
            raise PublishError("%s exceeds max_file_bytes; refusing to scan (or publish) it" % rel)
        with open(full, "rb") as fh:
            data = fh.read()
        digest = sha256_bytes(data)
        digests[rel] = digest
        seen = set()
        for cls, offset, match in text_hits(normalise(rel), lists):
            seen.add((cls, 0, match))
        for text in decodings(data):
            for cls, offset, match in text_hits(text, lists):
                seen.add((cls, _line_of(text, offset), match))
        for cls, line, match in sorted(seen):
            hits.append({"class": cls, "path": rel, "line": line, "match": match,
                         "sha256": digest})
    version = gitleaks_version()
    if version is None and man.require_gitleaks:
        raise PublishError("gitleaks missing: manifest %s requires it" % man.path)
    if version is not None:
        for item in run_gitleaks(root):
            rel = item["rel"]
            if rel not in digests:
                raise PublishError("gitleaks named a file outside the tree: %s" % rel)
            hits.append({"class": "secret", "path": rel, "line": item["line"],
                         "match": item["match"], "sha256": digests[rel]})
    blocking, allowed, used = [], [], set()
    for hit in hits:
        rule = manifest_mod.allowance_for(man, hit)
        if rule:
            used.add(id(rule))
            allowed.append(dict(hit, reason=rule["reason"]))
        else:
            blocking.append(hit)
    lapsed = [a for a in man.allow if id(a) not in used]
    counts = {c: 0 for c in CLASSES}
    for hit in blocking:
        counts[hit["class"]] += 1
    return {
        "repo": man.repo,
        "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tree_hash": tree_hash(root),
        "files": len(files),
        "manifest_sha256": man.sha256,
        "lists": dict(lists.counts, sha256=lists.digests),
        "gitleaks": version or "absent (not required by manifest)",
        "counts": counts,
        "hits": blocking,
        "allowed": allowed,
        "lapsed_allowances": [{"path": a["path"], "class": a["class"]} for a in lapsed],
        "passed": not blocking and not lapsed,     # a stale allowance is a stale manifest
    }


def report_dir(publish_dir: str, repo: str) -> str:
    return os.path.join(publish_dir, "reports", repo)


def write_report(publish_dir: str, report: Dict, prefix: str = "") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = os.path.join(report_dir(publish_dir, report["repo"]), "%s%s.json" % (prefix, stamp))
    write_json(path, report)
    return path


def latest_report(publish_dir: str, repo: str) -> Tuple[Optional[str], Optional[Dict]]:
    folder = report_dir(publish_dir, repo)
    if not os.path.isdir(folder):
        return None, None
    names = sorted(n for n in os.listdir(folder) if n.endswith(".json") and n[:1].isdigit())
    if not names:
        return None, None
    path = os.path.join(folder, names[-1])
    with open(path, encoding="utf-8") as fh:
        return path, json.load(fh)
