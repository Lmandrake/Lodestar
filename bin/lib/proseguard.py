"""Board prose never reaches the public remote (ruling 5, 2026-10-13: "Plug the last leaks").

Lodestar's remote is public-hosted, and the board is written by sessions that read work mail. What
they write as PROSE cannot be classified reliably, so the rule is structural and checkable:

* **A loop file carries only its frontmatter and a fixed pointer line.** The body — the prose
  sessions write — lives at `<private store>/projects/Lodestar/loops/<id>.md` (`lib.boundary`).
  `write_body` / `read_body` are its door.
* **A journal event carries no `note`.** The note goes to
  `.../projects/Lodestar/journal-notes/<date>/<machine>.jsonl` and the event keeps
  `note_ref: jnote_<12 hex>`; `note_text` reads it back where the store is reachable.
* **Some paths are never committed here at all** (`FORBIDDEN`): conversation records, handoffs,
  consult transcripts, private airlock material, per-machine memory snapshots of work machines.
* **No added line of board state may carry a private marker** (`MARKERS`): an address in one of
  the configured private domains other than the owner's own, an Outlook item id, a Teams thread id. A backstop, not the rule: it cannot
  see paraphrase, which is why the first three rules are structural.

`fix()` repairs the first two in the working tree (moving prose to the private store, or — on a
machine without one — to a machine-local stash under `~/dev/.lodestar/withheld/`, so
nothing is dropped and the tree is left clean). `violations()` reports what is still unsafe, and
the sweep and the pre-commit hook both refuse those paths. Neither ever deletes data.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from lib import boundary, clock, frontmatter, privatestore, machine, paths, prosehome, reckoning
except ImportError:  # pragma: no cover
    import boundary  # type: ignore
    import clock  # type: ignore
    import frontmatter  # type: ignore
    import privatestore  # type: ignore
    import machine  # type: ignore
    import paths  # type: ignore
    import prosehome  # type: ignore
    import reckoning  # type: ignore

POINTER_PREFIX = boundary.pointer_prefix()
POINTER_RE = re.compile(r"\A\s*" + re.escape(POINTER_PREFIX) + r"\S+\s*\Z")
NOTE_REF_RE = re.compile(r"\Ajnote_[0-9a-f]{12}\Z")

#: Never committed to Lodestar, whatever their content. Only additions and modifications are
#: refused; deleting one of these is the cleanup and always allowed.
FORBIDDEN = (
    "conversations/",
    "handoffs/",
    ".consult/",
    "airlock/captures/", "airlock/offers/", "airlock/predrafts/", "airlock/done/",
    "airlock/executed/", "airlock/shadow/", "airlock/requests/",
    "airlock/DIGEST.md", "airlock/kairos-queue.jsonl", "airlock/kairos-receipts.json",
    "priority/hold-queue.md",
)

#: Work machines' auto-memory snapshots carry work project memory; a home machine's does not.
WORK_MEMORY_RE = re.compile(r"\Aclaude/snapshot/([^/]+)/memory/")

#: airlock/review/ holds code (kept) and derived JSON/HTML built from mail (refused).
REVIEW_PREFIX = "airlock/review/"
REVIEW_CODE = (".py", ".js", ".mjs")

OWN_ADDRESSES = tuple(boundary.owner_addresses())
ADDRESS_LABEL = "private-domain address"


def _address_re() -> "re.Pattern":
    domains = boundary.address_domains()
    if not domains:
        return re.compile(r"(?!x)x")    # no private domain declared: matches nothing
    alt = "|".join(re.escape(d) for d in sorted(domains, key=len, reverse=True))
    return re.compile(r"[A-Za-z0-9._%%+-]+@(?:[A-Za-z0-9-]+\.)*(?:%s)\b" % alt, re.I)


MARKERS = (
    (ADDRESS_LABEL, _address_re()),
    ("Outlook item id", re.compile(r"\bAA[MQ]k[A-Za-z0-9+/_=-]{24,}")),
    ("Teams thread id", re.compile(r"\b19:[A-Za-z0-9_-]{8,}@thread\.[a-z0-9]+")),
)

STASH_DIR = "withheld"


# --- where prose goes -------------------------------------------------------------------------

def lodestar_root() -> str:
    return os.path.realpath(paths.root())


def store_dir(sub: str, store: Optional[str] = None) -> str:  # noqa: D401
    """`<private store>/projects/Lodestar/<sub>`. Raises `privatestore.Unavailable`.

    Deliberately `privatestore.root` rather than `prosehome.place`: board prose is store-only
    on EVERY machine, tower included, so that the shape rule stays one rule.

    The name is the constant `Lodestar`, never the checkout's directory name (a clone at
    `~/dev/.lodestar/x` is still the board). A sandboxed board — `LODESTAR_ROOT` set, as tests
    and check.py's snapshot do — gets an overlay named after its own root instead, so it can
    never read or write the real board's prose."""
    base = privatestore.root(store)
    name = (os.path.basename(lodestar_root().rstrip(os.sep))
            if os.environ.get("LODESTAR_ROOT") else "Lodestar")
    return os.path.join(base, prosehome.OVERLAY_DIR, name, sub)


def stash_dir(sub: str) -> str:
    return os.path.join(paths.state_dir(), STASH_DIR, sub)


def pointer(loop_id: str) -> str:
    return "%s%s/projects/Lodestar/loops/%s.md\n" % (POINTER_PREFIX, boundary.root_display(),
                                                     loop_id)


def is_clean_body(body: Optional[str], loop_id: Optional[str] = None) -> bool:
    """Empty, or THE pointer. With `loop_id`, only that loop's exact pointer counts: a
    "pointer" whose path token is itself prose would otherwise pass (GPT review 2026-10-13)."""
    if not (body or "").strip():
        return True
    if loop_id is not None:
        return (body or "").strip() == pointer(loop_id).strip()
    return bool(POINTER_RE.match(body or ""))


def _body_path(loop_id: str, store: Optional[str] = None) -> str:
    return os.path.join(store_dir("loops", store), "%s.md" % loop_id)


def read_body(loop_id: str, store: Optional[str] = None) -> Optional[str]:
    """The loop's prose, or None where the private store is not reachable or holds none."""
    try:
        path = _body_path(loop_id, store)
    except privatestore.Unavailable:
        return None
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def write_body(loop_id: str, text: str, store: Optional[str] = None) -> str:
    """Replace the loop's prose in the private store. Raises `privatestore.Unavailable`."""
    base = privatestore.root(store)
    path = _body_path(loop_id, store)
    privatestore._safe_dir(base, os.path.dirname(path))
    _atomic_write(path, text)
    return path


def _atomic_write(path: str, text: str) -> None:
    import uuid
    tmp = "%s.tmp.%d.%s" % (path, os.getpid(), uuid.uuid4().hex[:8])
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def merge_body(loop_id: str, text: str, store: Optional[str] = None) -> str:
    """Fold prose found in a Lodestar loop file into the store copy without losing either.

    Returns `moved` (the store copy was empty or a prefix of `text`: `text` replaces it), `same`
    (`text` is a prefix of the store copy: a stale whole-body rewrite), or `merged` (they
    diverged: `text` is appended under a marker for a human to reconcile)."""
    current = read_body(loop_id, store)
    if current is None:
        current = ""
    a, b = current.strip(), text.strip()
    if not a or b.startswith(a):
        write_body(loop_id, text, store)
        return "moved"
    if a.startswith(b):
        return "same"  # an older version of the stored prose: a stale whole-body rewrite
    marker = ("\n\n<!-- moved from Lodestar loops/%s.md by %s at %s; it did not match this "
              "copy, so both are kept — reconcile by hand -->\n\n"
              % (loop_id, _machine_key(), clock.now(timespec="seconds")))
    write_body(loop_id, current.rstrip("\n") + marker + text, store)
    return "merged"


def note_id(loop: str, ts: str, note: str) -> str:
    return "jnote_" + hashlib.sha256(("%s\x1f%s\x1f%s" % (loop, ts, note))
                                     .encode("utf-8")).hexdigest()[:12]


def put_note(loop: str, ts: str, note: str, store: Optional[str] = None,
             machine_key: Optional[str] = None) -> str:
    """Append a journal note to the private store; returns its id. Raises `privatestore.Unavailable`."""
    base = privatestore.root(store)
    nid = note_id(loop, ts, note)
    day = (ts or "")[:10] or clock.today()
    path = os.path.join(store_dir("journal-notes", store), day,
                        "%s.jsonl" % (machine_key or _machine_key()))
    privatestore._safe_dir(base, os.path.dirname(path))
    known = _KNOWN_NOTES.get(base)
    if known is None:
        known = _KNOWN_NOTES[base] = set(load_notes(store))
    if nid in known:
        return nid
    record = {"id": nid, "loop": loop, "ts": ts, "note": note}
    with reckoning._locked(path + ".lock"):
        reckoning._repair_torn(path)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    known.add(nid)
    return nid


#: Note ids already in each store, so a bulk migration is not quadratic.
_KNOWN_NOTES: Dict[str, set] = {}


def stash(kind: str, name: str, text: str) -> str:
    """Keep prose this machine may not commit, locally, and say where."""
    directory = stash_dir(kind)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(text if text.endswith("\n") else text + "\n")
    return path


def record_note(record: Dict, store: Optional[str] = None) -> Dict:
    """Move `record["note"]` out of a journal event. Returns the record to write."""
    note = record.pop("note", None)
    if not note:
        return record
    try:
        record["note_ref"] = put_note(record.get("loop", ""), record.get("ts", ""), note, store)
    except privatestore.Unavailable:
        nid = note_id(record.get("loop", ""), record.get("ts", ""), note)
        stash("journal-notes", "notes.jsonl",
              json.dumps({"id": nid, "loop": record.get("loop"), "ts": record.get("ts"),
                          "note": note}, ensure_ascii=False, sort_keys=True))
        record["note_withheld"] = nid
    return record


_NOTES_CACHE: Dict[str, Tuple[float, Dict[str, Dict]]] = {}


def load_notes(store: Optional[str] = None) -> Dict[str, Dict]:
    try:
        folder = store_dir("journal-notes", store)
    except privatestore.Unavailable:
        return {}
    out: Dict[str, Dict] = {}
    if not os.path.isdir(folder):
        return out
    for dirpath, dirs, names in sorted(os.walk(folder)):
        dirs.sort()
        for name in sorted(names):
            if not name.endswith(".jsonl"):
                continue
            with open(os.path.join(dirpath, name), encoding="utf-8") as fh:
                for line in fh.read().split("\n")[:-1]:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    out.setdefault(rec.get("id", ""), rec)
    return out


def note_text(event: Dict, notes: Optional[Dict[str, Dict]] = None) -> Optional[str]:
    """An event's note: inline (old events), from the private store, or None."""
    if event.get("note"):
        return event["note"]
    ref = event.get("note_ref")
    if not ref:
        return None
    rec = (notes if notes is not None else load_notes()).get(ref)
    return rec.get("note") if rec else None


def _machine_key() -> str:
    try:
        return machine.key()
    except Exception:
        return "unknown"


# --- repair the working tree ------------------------------------------------------------------

def _is_loop(path: str) -> bool:
    """Any markdown under loops/, at any depth: a rename into a subdirectory must not escape
    the shape rule (GPT review 2026-10-13)."""
    return path.startswith("loops/") and path.endswith(".md")


def _is_event(path: str) -> bool:
    return path.startswith("journal/") and path.endswith(".json")


def _loop_id(rel: str) -> str:
    return os.path.basename(rel)[:-3]


#: Tracked files whose writers now write to the private store instead. A leftover working-tree
#: change to one (written by older code, mid-upgrade) is preserved in the store and the file is
#: restored to HEAD, so the tree stays clean and the sweep keeps pulling. Everything else that
#: is forbidden is only withheld and flagged: its writer may still depend on the local copy.
RESTORABLE = (("conversations/", "conversations/"), ("handoffs/", "handoffs/"),
              (".consult/", "consult/"))


def _restorable(rel: str) -> Optional[str]:
    for prefix, sub in RESTORABLE:
        if rel.startswith(prefix):
            return sub + rel[len(prefix):]
    m = WORK_MEMORY_RE.match(rel)
    if m and m.group(1) in _work_keys():
        return "claude-snapshot/%s/memory/%s" % (m.group(1), rel[m.end():])
    return None


def _tracked(repo: str, rel: str) -> bool:
    return _git(repo, "ls-files", "--error-unmatch", "--", rel)[0] == 0


def preserve(repo: str, rel: str, dest_rel: str, store: Optional[str] = None,
             text: Optional[str] = None) -> str:
    """Copy a working-tree file (or `text`) into the store, never overwriting a different
    copy there."""
    if text is None:
        with open(os.path.join(repo, rel), encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    try:
        base = privatestore.root(store)
        dest = os.path.join(store_dir("", store), dest_rel)
        privatestore._safe_dir(base, os.path.dirname(dest))
        where = "private store"
    except privatestore.Unavailable:
        dest = os.path.join(stash_dir("files"), dest_rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        where = "local stash"
    if os.path.exists(dest):
        with open(dest, encoding="utf-8", errors="replace") as handle:
            if handle.read() == text:
                return where
        # Never overwrite a different copy: a content hash makes the name unique per version.
        dest = "%s.%s" % (dest, hashlib.sha256(text.encode("utf-8")).hexdigest()[:12])
        if os.path.exists(dest):
            return where
    _atomic_write(dest, text)
    return where


def fix(repo: str, candidates: Sequence[str], store: Optional[str] = None) -> List[str]:
    """Move loop prose and journal notes out of the given working-tree paths, and preserve and
    restore leftover changes to `RESTORABLE` tracked files. Returns one line per path changed.
    Leaves each file in the safe shape whatever happens to the prose."""
    report = []
    for rel in candidates:
        full = os.path.join(repo, rel)
        if not os.path.isfile(full):
            continue
        try:
            dest = _restorable(rel)
            if dest is not None:
                if not _tracked(repo, rel):
                    continue  # untracked: ignored here, and withheld if not
                with open(full, encoding="utf-8", errors="replace") as handle:
                    seen = handle.read()
                where = preserve(repo, rel, dest, store, text=seen)
                code, staged = _git(repo, "show", ":%s" % rel)
                head = _git(repo, "show", "HEAD:%s" % rel)[1]
                if code == 0 and staged not in (seen, head):
                    preserve(repo, rel, dest, store, text=staged)
                with open(full, encoding="utf-8", errors="replace") as handle:
                    if handle.read() != seen:
                        report.append("%s: changed while being preserved; left for the next "
                                      "pass" % rel)
                        continue  # a writer is active: never destroy what was not saved
                if _git(repo, "checkout", "HEAD", "--", rel)[0] == 0:
                    report.append("%s: change preserved in the %s, file restored to HEAD"
                                  % (rel, where))
                continue
            if _is_loop(rel):
                line = _fix_loop(full, rel, store)
            elif _is_event(rel):
                line = _fix_event(full, rel, store)
            else:
                continue
        except (OSError, ValueError) as error:
            report.append("%s: could not repair (%s)" % (rel, error))
            continue
        if line:
            report.append(line)
    return report


def _fix_loop(full: str, rel: str, store: Optional[str]) -> Optional[str]:
    with open(full, encoding="utf-8") as handle:
        seen = handle.read()
    fields, body = frontmatter.parse(seen)
    loop_id = _loop_id(rel)
    if is_clean_body(body, loop_id):
        return None
    if POINTER_RE.match(body):  # some other loop's pointer (a renamed file): no prose to keep
        frontmatter.write_file(full, fields, pointer(loop_id))
        return "%s: pointer corrected" % rel
    try:
        outcome = merge_body(loop_id, body, store)
        where = "private store (%s)" % outcome
    except privatestore.Unavailable:
        where = "local stash %s" % stash(
            "loops", "%s.%s.md" % (loop_id, clock.now(timespec="seconds").replace(":", "")), body)
    with open(full, encoding="utf-8") as handle:
        if handle.read() != seen:
            return "%s: changed while being moved; left for the next pass" % rel
    frontmatter.write_file(full, fields, pointer(loop_id))
    return "%s: prose moved to the %s" % (rel, where)


def _fix_event(full: str, rel: str, store: Optional[str]) -> Optional[str]:
    with open(full, encoding="utf-8") as handle:
        seen = handle.read()
    record = json.loads(seen)
    if not isinstance(record, dict) or not record.get("note"):
        return None
    record = record_note(record, store)
    with open(full, encoding="utf-8") as handle:
        if handle.read() != seen:
            return "%s: changed while being moved; left for the next pass" % rel
    _atomic_write(full, json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return "%s: note moved to the %s" % (
        rel, "local stash" if record.get("note_withheld") else "private store")


# --- what may not be committed ----------------------------------------------------------------

def _work_keys() -> Tuple[str, ...]:
    try:
        table = machine.labels_table()
    except Exception:
        return ()
    return tuple(k for k, labels in table.items() if prosehome.WORK_LABEL in labels)


def forbidden(rel: str, work_keys: Optional[Sequence[str]] = None) -> Optional[str]:
    """Why `rel` may never be committed to Lodestar, or None."""
    for prefix in FORBIDDEN:
        if rel == prefix or (prefix.endswith("/") and rel.startswith(prefix)):
            return "private-class path (%s)" % prefix
    if rel.startswith(REVIEW_PREFIX) and not rel.endswith(REVIEW_CODE):
        return "private-derived review data (%s)" % REVIEW_PREFIX
    m = WORK_MEMORY_RE.match(rel)
    if m:
        keys = _work_keys() if work_keys is None else work_keys
        if m.group(1) in keys or not keys:
            return "a work Mac's memory snapshot (lives in the private store)"
    return None


def shape(rel: str, text: str) -> Optional[str]:
    """Why a loop or journal file's content is not in the safe shape, or None."""
    if _is_loop(rel):
        try:
            _, body = frontmatter.parse(text)
        except ValueError:
            return "unparseable loop file"
        if not is_clean_body(body, _loop_id(rel)):
            return "loop prose in Lodestar (it belongs in the private store's projects/Lodestar/loops/)"
    elif _is_event(rel):
        try:
            record = json.loads(text)
        except ValueError:
            return "unparseable journal event (refused rather than guessed safe)"
        if not isinstance(record, dict):
            return "journal event is not one JSON object (refused rather than guessed safe)"
        if record.get("note"):
            return "journal note in Lodestar (it belongs in the private store as note_ref)"
    return None


def markers(lines: Iterable[str]) -> List[str]:
    found = []
    for line in lines:
        for label, pattern in MARKERS:
            for hit in pattern.findall(line):
                if label == ADDRESS_LABEL and hit.lower() in OWN_ADDRESSES:
                    continue
                if label not in found:
                    found.append(label)
    return found


def _scanned(rel: str) -> bool:
    """Marker scanning covers board state. Source and docs are authored commits; tests and
    code carry the marker patterns themselves."""
    try:
        from lib import gitutil
    except ImportError:  # pragma: no cover
        import gitutil  # type: ignore
    return not gitutil.is_source(rel)


def _git(repo: str, *args: str) -> Tuple[int, str]:
    proc = subprocess.run(("git",) + args, cwd=repo, capture_output=True, text=True,
                          errors="replace")
    return proc.returncode, proc.stdout


class GuardError(Exception):
    """git could not answer: the guard refuses rather than passing what it could not read."""


def _added_from_diff(out: str) -> Tuple[List[str], bool]:
    """(added lines, binary?) from `git diff -U0` output. Only the file headers before the
    first hunk are skipped, so an added line that itself starts with `++` is kept."""
    lines, in_hunk, binary = [], False, False
    for raw in out.splitlines():
        if raw.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk:
            if raw.startswith("Binary files ") or raw.startswith("GIT binary patch"):
                binary = True
            continue
        if raw.startswith("+"):
            lines.append(raw[1:])
        elif raw.startswith("diff --git "):
            in_hunk = False
    return lines, binary


def _added_lines(repo: str, rel: str, base: Optional[str], staged: bool,
                 text: Optional[str] = None) -> List[str]:
    has_head = _git(repo, "rev-parse", "--verify", "-q", "HEAD")[0] == 0
    if not has_head:  # an unborn branch: everything is new
        content = text if text is not None else _content(repo, rel, staged)
        return (content or "").splitlines()
    if staged:
        args = ("diff", "--cached", "-U0", "--no-color", "--no-ext-diff", "--text", "--", rel)
    else:
        args = ("diff", base or "HEAD", "-U0", "--no-color", "--no-ext-diff", "--text",
                "--", rel)
    code, out = _git(repo, *args)
    if code != 0:
        raise GuardError("git diff failed for %s" % rel)
    lines, binary = _added_from_diff(out)
    tracked = _git(repo, "cat-file", "-e", "HEAD:%s" % rel)[0] == 0
    if binary or not tracked:
        # New to HEAD, or not representable as a diff: every line is new.
        content = text if text is not None else _content(repo, rel, staged)
        return (content or "").splitlines()
    return lines


def _content(repo: str, rel: str, staged: bool) -> Optional[str]:
    """The text that would be committed, or None when the path is gone (a deletion)."""
    if staged:
        code, out = _git(repo, "show", ":%s" % rel)
        if code != 0:
            raise GuardError("cannot read the staged copy of %s" % rel)
        return out
    full = os.path.join(repo, rel)
    if not os.path.lexists(full):
        return None
    with open(full, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def violations(repo: str, candidates: Sequence[str], staged: bool = False,
               work_keys: Optional[Sequence[str]] = None) -> List[Tuple[str, str]]:
    """(path, why) for every candidate that must not be committed to Lodestar.

    `candidates` are repo-relative paths that are changing; a path that no longer exists (a
    deletion) is never a violation."""
    out = []
    keys = _work_keys() if work_keys is None else work_keys
    for rel in candidates:
        rel = rel.replace(os.sep, "/")
        try:
            text = _content(repo, rel, staged)
            if text is None:
                continue  # deleted: removing private text is always allowed
            why = forbidden(rel, keys) or shape(rel, text)
            if not why and markers([rel]):
                why = "private marker in the path itself"
            if not why and _scanned(rel):
                hits = markers(_added_lines(repo, rel, None, staged, text=text))
                if hits:
                    why = "private marker on an added line: %s" % ", ".join(hits)
        except (GuardError, OSError) as error:
            why = "could not be checked (%s) — refused" % error
        if why:
            out.append((rel, why))
    return out


def staged_paths(repo: str) -> List[str]:
    code, out = _git(repo, "diff", "--cached", "--name-only", "--diff-filter=ACMRT",
                     "--no-renames", "-z")
    if code != 0:
        raise GuardError("cannot list the staged paths")
    return [p for p in out.split("\0") if p]


def outgoing_violations(repo: str, tip: str = "HEAD") -> List[Tuple[str, str]]:
    """(commit:path, why) for every commit about to be pushed: its message and every path it
    adds or changes, judged on that commit's own blobs. This is what a `--no-verify` commit, a
    hook-less clone or a third tool's commit cannot get past — the check is at the push, not
    at the commit (GPT review 2026-10-13). Merge commits are judged by message only; their
    content is their parents'. Raises GuardError when git cannot answer."""
    # Everything reachable from the tip that no remote-tracking ref has yet: correct with or
    # without an upstream, and for any branch.
    code, out = _git(repo, "rev-list", "--reverse", tip, "--not", "--remotes")
    if code != 0:
        raise GuardError("cannot list outgoing commits")
    found = []
    keys = _work_keys()
    for sha in out.split():
        code, msg = _git(repo, "log", "-1", "--format=%B", sha)
        if markers(msg.splitlines()):
            found.append(("%s:<message>" % sha[:10], "private marker in the commit message"))
        code, parents = _git(repo, "rev-list", "--parents", "-n", "1", sha)
        if len(parents.split()) != 2:
            continue  # a merge (or root): its content is reviewed through its parents
        code, names = _git(repo, "diff-tree", "-r", "--no-commit-id", "--name-only",
                           "--no-renames", "--diff-filter=ACMRT", "-z", sha)
        if code != 0:
            raise GuardError("cannot read commit %s" % sha[:10])
        for rel in [n for n in names.split("\0") if n]:
            code, text = _git(repo, "show", "%s:%s" % (sha, rel))
            if code != 0:
                raise GuardError("cannot read %s:%s" % (sha[:10], rel))
            why = forbidden(rel, keys) or shape(rel, text)
            if not why and markers([rel]):
                why = "private marker in the path itself"
            if not why and _scanned(rel):
                code, diff = _git(repo, "diff", "-U0", "--no-color", "--no-ext-diff", "--text",
                                  "%s^" % sha, sha, "--", rel)
                added, binary = _added_from_diff(diff)
                hits = markers(text.splitlines() if binary or not diff.strip() else added)
                if hits:
                    why = "private marker on an added line: %s" % ", ".join(hits)
            if why:
                found.append(("%s:%s" % (sha[:10], rel), why))
    return found


def applies(repo: str) -> bool:
    """The guard is Lodestar's: other repos' shapes are their own."""
    return os.path.realpath(repo) == lodestar_root()


HOOKS_DIR = ".githooks"


def install_hook(repo: str) -> Optional[str]:
    """Point Lodestar's `core.hooksPath` at the tracked `.githooks/`, once. Returns a line to
    report when it changed something. Never overrides a hooks path someone else set."""
    if not os.path.isfile(os.path.join(repo, HOOKS_DIR, "pre-commit")):
        return None
    code, current = _git(repo, "config", "--local", "--get", "core.hooksPath")
    current = current.strip()
    if current == HOOKS_DIR:
        return None
    if current and os.path.isabs(current) and (
            os.path.realpath(current) == os.path.realpath(os.path.join(repo, HOOKS_DIR))):
        # Some tool wrote the absolute spelling of our own hooks dir; the check wants the
        # relative form (it survives a moved checkout and linked worktrees).
        _git(repo, "config", "--local", "core.hooksPath", HOOKS_DIR)
        return "core.hooksPath rewritten from absolute %r to %r" % (current, HOOKS_DIR)
    if current:
        return "core.hooksPath is %r, not %r — the private-text guard is NOT installed" % (
            current, HOOKS_DIR)
    _git(repo, "config", "--local", "core.hooksPath", HOOKS_DIR)
    return "installed the private-text pre-commit guard (core.hooksPath=%s)" % HOOKS_DIR


def migrate(repo: str, store: Optional[str] = None) -> Dict[str, int]:
    """One-time: put EVERY loop body and journal note at HEAD into the safe shape, and copy the
    tracked RESTORABLE files into the store (they stay tracked until the history rewrite
    deletes them, so no checkout loses a file mid-upgrade). Requires the private store."""
    privatestore.root(store)
    counts = {"loops": 0, "notes": 0, "copied": 0, "unreadable": 0}
    code, out = _git(repo, "ls-files", "-z")
    for rel in [p for p in out.split("\0") if p]:
        full = os.path.join(repo, rel)
        if not os.path.isfile(full):
            continue
        try:
            dest = _restorable(rel)
            if dest is not None:
                preserve(repo, rel, dest, store)
                counts["copied"] += 1
            elif _is_loop(rel) and _fix_loop(full, rel, store):
                counts["loops"] += 1
            elif _is_event(rel) and _fix_event(full, rel, store):
                counts["notes"] += 1
        except ValueError:
            counts["unreadable"] += 1  # left as is; the guard refuses it if it ever changes
    return counts
