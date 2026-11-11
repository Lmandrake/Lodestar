"""Work Lodestar deliberately does not track. Two sources, one answer.

1. **Board scopes** (`untracked-scope.json` at the board root). Lukas, 2026-10-20: RimFlow work
   "belong[s] to RimFlow only. Remove them from Lodestar tracking entirely". Loop-id globs and
   repo substrings; every loop/journal enumerator filters through here and `touch.py` refuses.

2. **Project markers** (owner, 2026-11-06). A project that must leave NO record in Lodestar
   carries a `.lodestar-untracked` file at its own root. The marker's existence IS the mark:
   nothing in Lodestar names the project, so the exclusion spec (paths, names, ticket codes,
   senders, keywords) lives with the project, not here. Markers are discovered under the dev
   root (`~/dev/*/.lodestar-untracked`; `LODESTAR_UNTRACKED_ROOT` overrides) plus any extra roots in a
   machine-local cache (`~/dev/.lodestar/untracked.json`, never synced, written by `refresh`).

Every writer and reader that could record the project asks one of:

* `is_untracked_path(path)`  a cwd, repo, transcript, file or project dir;
* `is_untracked_name(name)`  a project/loop/registry name, an abbrev or a ticket id;
* `is_untracked_text(text)`  free text: a subject, a body, a title (keywords + senders);
* `is_untracked_sender(addr)`;
* `item_untracked(dict)`     any record: every string value in it is checked.

Contracts: a missing or unreadable marker means nothing is excluded by it (fail open on the
CONFIG, so ordinary projects are never hidden by a broken file) but the scan never raises.
Nothing here logs or returns matched text: callers count `untracked_skipped`, never quote.
"""

import fnmatch
import glob
import json
import os
import re
from typing import Any, Dict, Iterable, List, Optional

from . import paths

FILENAME = "untracked-scope.json"
MARKER = ".lodestar-untracked"
CACHE_NAME = "untracked.json"

_CACHE: Dict[str, Any] = {}


# --------------------------------------------------------------------------- board scopes

def _scopes(root: Optional[str] = None) -> List[Dict[str, Any]]:
    path = os.path.join(root or paths.root(), FILENAME)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, ValueError):
        return []
    scopes = doc.get("scopes") if isinstance(doc, dict) else None
    return [s for s in scopes or [] if isinstance(s, dict)]


def scope_for(loop_id: str = "", repo: str = "", root: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The scope that excludes this loop id or repo, or None if Lodestar tracks it."""
    lid = (loop_id or "").lower()
    rep = (repo or "").lower()
    for scope in _scopes(root):
        if lid and any(fnmatch.fnmatchcase(lid, g.lower()) for g in scope.get("loop_id_globs") or []):
            return scope
        if rep and any(s.lower() in rep for s in scope.get("repo_substrings") or [] if s):
            return scope
    if (lid and is_untracked_name(lid)) or (repo and (is_untracked_path(repo) or is_untracked_name(repo)
                                                    or is_untracked_text(repo))):
        return {"name": "untracked project", "ruled": "marker"}
    return None


def is_untracked(loop_id: str = "", repo: str = "", root: Optional[str] = None) -> bool:
    return scope_for(loop_id, repo, root) is not None


def event_untracked(event: Dict[str, Any], root: Optional[str] = None) -> bool:
    return is_untracked(str(event.get("loop") or ""), str(event.get("repo") or ""), root)


# --------------------------------------------------------------------------- project markers

def dev_root() -> str:
    return os.path.expanduser(os.environ.get("LODESTAR_UNTRACKED_ROOT")
                              or os.environ.get("LODESTAR_DEV_ROOT") or "~/dev")


def cache_path() -> str:
    return os.environ.get("LODESTAR_UNTRACKED_CACHE") or paths.state_file(CACHE_NAME)


def _marker_files() -> List[str]:
    found = set(glob.glob(os.path.join(dev_root(), "*", MARKER)))
    try:
        with open(cache_path(), encoding="utf-8") as fh:
            extra = json.load(fh).get("markers") or []
    except (OSError, ValueError, AttributeError):
        extra = []
    for p in extra:
        p = os.path.expanduser(str(p))
        if os.path.isfile(p):
            found.add(p)
    return sorted(found)


def _norm(path: str) -> str:
    p = os.path.expanduser(str(path or "")).rstrip("/")
    return os.path.realpath(p) if p.startswith("/") else p


def _load_marker(path: str) -> Dict[str, Any]:
    root = os.path.dirname(path)
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict):
            doc = {}
    except (OSError, ValueError):
        doc = {}  # unreadable spec: the project's own directory is still excluded
    def strs(key):
        return [str(x) for x in doc.get(key) or [] if str(x).strip()]
    prefixes = {_norm(root), root.rstrip("/")}
    prefixes.update(_norm(p) for p in strs("paths"))
    names = {n.lower() for n in strs("names")} | {os.path.basename(root).lower()}
    return {"prefixes": sorted(p for p in prefixes if p),
            "names": names,
            "codes": {c.upper() for c in strs("codes")},
            "senders": [s.lower() for s in strs("senders")],
            "keywords": [k.lower() for k in strs("keywords")],
            # distinctive stems matched anywhere, even inside a longer token (`stem_x`, `stemdev`)
            "stems": [k.lower() for k in strs("stems") if len(k) >= 5]}


def specs() -> List[Dict[str, Any]]:
    """Every marker's parsed spec. Cached per process by the marker files' (path, mtime)."""
    files = _marker_files()
    sig = []
    for f in files:
        try:
            sig.append((f, os.path.getmtime(f)))
        except OSError:
            pass
    key = repr(sig)
    if _CACHE.get("sig") != key:
        loaded = [_load_marker(f) for f, _ in sig]
        pats = []
        for s in loaded:
            words = s["keywords"] + sorted(s["names"])
            alts = "|".join(re.escape(w) for w in sorted(set(words), key=len, reverse=True) if w)
            pats.append(re.compile(r"(?<![a-z0-9])(" + alts + r")(?![a-z0-9])", re.I) if alts else None)
            codes = "|".join(re.escape(c) for c in sorted(s["codes"]))
            s["code_re"] = re.compile(r"(?<![A-Za-z0-9])(" + codes + r")-\d+\b") if codes else None
        for s, p in zip(loaded, pats):
            s["word_re"] = p
        alts = []
        for s in loaded:
            alts += [r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(w)
                     for w in s["keywords"] + sorted(s["names"]) if w]
            alts += [r"(?-i:(?<![A-Za-z0-9])%s-\d+\b)" % re.escape(c) for c in sorted(s["codes"])]
            alts += [re.escape(x) for x in s["senders"] if x]
            alts += [re.escape(x) for x in s.get("stems") or [] if x]
            alts += [re.escape(x) for x in s["prefixes"] if x.startswith("/")]
        alts = sorted(set(alts), key=len, reverse=True)
        _CACHE.update(sig=key, specs=loaded,
                      any_re=re.compile("|".join(alts), re.I) if alts else None,
                      codes={c for s in loaded for c in s["codes"]},
                      names={n for s in loaded for n in s["names"]})
    return _CACHE["specs"]


def reset() -> None:
    _CACHE.clear()


def active() -> bool:
    return bool(specs())


def is_untracked_path(path: str) -> bool:
    if not path:
        return False
    raw = os.path.expanduser(str(path))
    cands = {raw.rstrip("/"), _norm(raw)}
    for s in specs():
        for pre in s["prefixes"]:
            for c in cands:
                if c == pre or c.startswith(pre + "/") or c.startswith(pre + " "):
                    return True
        # Claude Code's mangled project dir (-Users-x-dev-Name) and Waypoint-style mentions
        base = os.path.basename(raw.rstrip("/")).lower()
        for pre in s["prefixes"]:
            mangled = re.sub(r"[^A-Za-z0-9]", "-", pre).lower()
            if base == mangled or base.startswith(mangled + "-"):
                return True
    return False


def is_untracked_name(name: str) -> bool:
    n = str(name or "").strip().lower()
    if not n:
        return False
    for s in specs():
        if n in s["names"] or str(name).strip() in s["codes"]:  # codes: exact case only
            return True
        if s.get("code_re") and s["code_re"].fullmatch(str(name).strip()):
            return True
    return False


def is_untracked_sender(addr: str) -> bool:
    a = str(addr or "").lower()
    if not a:
        return False
    return any(snd and snd in a for s in specs() for snd in s["senders"])


def is_untracked_text(text: str) -> bool:
    t = str(text or "")
    if not t or not specs():
        return False
    rx = _CACHE.get("any_re")
    return bool(rx is not None and rx.search(t))


def _strings(obj: Any, depth: int = 0) -> Iterable[str]:
    if depth > 8:
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _strings(v, depth + 1)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            yield from _strings(v, depth + 1)


def item_untracked(item: Any) -> bool:
    """True when any string anywhere in a record names an untracked project."""
    if not specs():
        return False
    if isinstance(item, str):
        return is_untracked_text(item) or is_untracked_name(item)
    try:
        blob = json.dumps(item, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return any(is_untracked_text(s) or is_untracked_name(s) for s in _strings(item))
    if is_untracked_text(blob):
        return True
    low = blob.lower()
    if any('"%s"' % c in blob for c in _CACHE.get("codes") or ()):  # exact case only
        return True
    return any('"%s"' % n in low for n in _CACHE.get("names") or ())


def drop(items: Iterable[Any]) -> List[Any]:
    """`items` without the untracked ones (order kept)."""
    return [i for i in items if not item_untracked(i)]


def refresh(extra_markers: Iterable[str] = ()) -> int:
    """Write the machine-local cache of marker paths (for markers outside the dev root).
    Returns how many markers are known. Never synced: it lives in the state dir."""
    files = set(glob.glob(os.path.join(dev_root(), "*", MARKER)))
    files.update(os.path.expanduser(p) for p in extra_markers if os.path.isfile(os.path.expanduser(p)))
    try:
        os.makedirs(os.path.dirname(cache_path()), exist_ok=True)
        tmp = cache_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"markers": sorted(files)}, fh, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, cache_path())
    except OSError:
        pass
    reset()
    return len(files)


REDACTED = "(untracked)"
_RECORD_MAX = 20000  # a value this small that names the project is ONE record about it


def _is_record(v: Any) -> bool:
    if not isinstance(v, (dict, list)):
        return False
    try:
        return len(json.dumps(v, default=str)) < _RECORD_MAX
    except (TypeError, ValueError):
        return False


def _is_map(d: Dict) -> bool:
    """A dict keyed by ids (a ledger, a cache): many entries, every value a dict."""
    return len(d) >= 3 and all(isinstance(v, dict) for v in d.values())


def scrub(obj: Any, depth: int = 0) -> Any:
    """A copy of a JSON-shaped value with nothing left that names an untracked project:

    * a list element that is a record (small dict/list) naming it, or a string naming it: dropped;
    * a map entry (a dict of dicts, e.g. a ledger) whose key or record names it: dropped;
    * any other dict key naming it: dropped; any other string value naming it: REDACTED.

    The root is never dropped, so a derived view keeps its shape. A no-op with no marker."""
    if depth == 0 and not specs():
        return obj
    if depth > 16:
        return obj
    if isinstance(obj, (list, dict)) and not item_untracked(obj):
        return obj  # a clean subtree: nothing below it to drop
    if isinstance(obj, list):
        out = []
        for v in obj:
            if isinstance(v, str) and (is_untracked_text(v) or is_untracked_name(v)):
                continue
            if _is_record(v) and item_untracked(v):
                continue
            out.append(scrub(v, depth + 1))
        return out
    if isinstance(obj, dict):
        is_map = _is_map(obj)
        out = {}
        for k, v in obj.items():
            if is_untracked_name(str(k)) or is_untracked_text(str(k)):
                continue
            if is_map and _is_record(v) and item_untracked(v):
                continue
            if isinstance(v, str) and (is_untracked_text(v) or is_untracked_name(v)):
                out[k] = REDACTED
                continue
            out[k] = scrub(v, depth + 1)
        return out
    return obj


def item_untracked_json_text(text: str) -> bool:
    """Cheap pre-check for JSON text: a quoted bare project code (`"p": "CODE"`) that plain-text
    matching skips (a bare code is too short to be a safe word in prose)."""
    for s in specs():
        for c in s["codes"]:
            if '"%s"' % c in text:
                return True
    return False
