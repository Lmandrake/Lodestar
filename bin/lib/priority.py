"""Beacons and sightings — the single high-priority items registry.

See `docs/superpowers/specs/2026-10-06-high-priority-beacons-design.md`.

A **beacon** (`sticky=True`) is a standing, named risk (a vendor dispute holding up a project) that stays open and
keeps resurfacing until explicitly closed, regardless of whether new evidence has arrived
recently. A **sighting** (`sticky=False`) is a one-time flagged item, reviewed once and
closed. Both are rows in one markdown table, `priority/items.md`.

Who may close one (owner delegation, 2026-10-22): Lukas, by his word; or any agent or the
resolver (`bin/reconcile.py`), as DONE, when the row's attached end-condition check (`Check`
column, `lib/outcome.py`) is SATISFIED. Nothing may close a beacon by inference: a quiet
thread, a related item reaching a terminal state, or "probably over" is not a close; ending a
standing watch for any reason other than a satisfied check is his call.

Reading goes through `mdtable.parse`, same as every other registry in this repo. Writing has
two shapes:

- **A new row is a plain append.** Nothing else can be reading a row that does not exist yet,
  so there is no concurrent-edit hazard to guard against the way there is for an existing row.
- **Closing a row or updating a beacon's evidence are surgical, multi-cell edits.** They reuse
  the parse-before/after verification `registry.py::_set_cell` established for exactly this
  file format's signature failure mode (a pipe-escape bug silently shifting every later cell) —
  generalized here to more than one cell in a row at once, since closing touches three columns
  together and must never partially apply.
"""

import contextlib
import fcntl
import os
import re
import secrets
import sys
import tempfile
from datetime import datetime, timezone
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

from . import flockwait, mdtable, paths

#: The official language, Lukas's ruling 2026-10-06: a beacon is "lit", never "created" or
#: "added" — "Light a beacon" / "Beacon lit for <title>" is the phrase every surface uses.
#: Yellow is the house color for anything beacon-related; `highlight()` is the one place that
#: decides how, so a future color change (or a surface that needs plain text) touches one spot.
BEACON_YELLOW = "\033[33m"
_RESET = "\033[0m"


def highlight(text: str, enabled: Optional[bool] = None) -> str:
    """Wrap `text` in the house yellow for beacon-related output.

    `enabled` defaults to whether stdout is a real terminal, so output piped to a file, a log,
    or another program never carries stray escape codes — the same guard a well-behaved CLI
    uses for any ANSI color, and nothing here has ever needed one before.
    """
    if enabled is None:
        enabled = sys.stdout.isatty()
    return "%s%s%s" % (BEACON_YELLOW, text, _RESET) if enabled else text

#: Header cells as written to the file. Deliberately one word each (no spaces) so the
#: lowercased header `mdtable.parse` returns is usable directly as a dict/attribute key with
#: no name-mapping layer — "Last Matched" would lowercase to "last matched", a key with a
#: space in it, which is legal but awkward everywhere it is referenced.
#:
#: `Scope` (a `priority/routing.md` project name; empty = global) and `EventKey`
#: (`<source>:<event id>:<beacon id>`, sightings only) were added 2026-10-09 for project-scoped
#: beacons — see docs/bearing/SKILL.md. They are LAST so an older eleven-column file migrates by
#: appending cells (`migrate`), never by shifting any existing one.
#:
#: `Senders` (2026-10-12, the quiet cycle): exact-match sender addresses a beacon watches for,
#: comma-separated, the same equality rule as `priority/routing.md`'s senders. A person's NAME
#: as a body keyword matched quoted headers and cc lines (14 historical candidates, all
#: no-match); what the beacon wanted was mail FROM them. Last, for the same migration reason.
#:
#: `WaitingSince`/`WaitDays` and `AnsweredThrough`/`AnsweredNote` (2026-10-21 beacon-audit
#: rulings) shape a beacon's gem, never its matching. WAITING: the gem turns amber once
#: WaitDays (default 3) pass with no news after max(WaitingSince, last evidence); WaitingSince is
#: a date or `heartbeat:<machine key>` (the clock starts at that machine's latest journal
#: heartbeat). ANSWERED: evidence dated at or before AnsweredThrough no longer lights the gem.
#:
#: `Check` (2026-10-22, the outcome registry `lib/outcome.py`): the beacon's END CONDITION as
#: a check spec in compact JSON, or an explicit coverage gap. A beacon is a standing watch: the
#: resolver ends it only when an attached end-condition check is SATISFIED, never by inference.
HEADERS = ("Id", "Title", "Source", "Sticky", "Keywords", "Note", "Opened",
           "LastMatched", "Status", "ClosedAt", "ClosedReason", "Scope", "EventKey", "Senders",
           "WaitingSince", "WaitDays", "AnsweredThrough", "AnsweredNote", "Check")
COLUMNS = tuple(h.lower() for h in HEADERS)  # ("id", "title", ..., "closedreason")

OPEN = "open"
CLOSED = "closed"


class Item(NamedTuple):
    id: str
    title: str
    source: str
    sticky: bool
    keywords: Tuple[str, ...]
    note: str
    opened: str
    last_matched: str
    status: str
    closed_at: str
    closed_reason: str
    scope: str = ""
    event_key: str = ""
    senders: Tuple[str, ...] = ()
    waiting_since: str = ""
    wait_days: str = ""
    answered_through: str = ""
    answered_note: str = ""
    check: str = ""


#: WaitDays when a beacon is set waiting without one (owner ruling 2026-10-21).
DEFAULT_WAIT_DAYS = 3


def opaque_event_id(event_id: str) -> str:
    """An event id safe to store in this github.com-hosted table: a raw Outlook item id
    (`AAMk...`, the prose guard refuses it) becomes `<prefix>:h<sha256[:16]>`; anything else
    passes through. Deterministic, so the same mail dedupes to the same sighting."""
    import hashlib
    if not event_id or "AA" not in event_id:
        return event_id
    prefix, sep, rest = event_id.rpartition(":")
    if not re.match(r"AA[MQ]k", rest):
        return event_id
    return "%s%sh%s" % (prefix, sep, hashlib.sha256(rest.encode()).hexdigest()[:16])


def _path(path: Optional[str]) -> str:
    return path or os.path.join(paths.root(), "priority", "items.md")


def _truthy(value: str) -> bool:
    return value.strip().strip("`").lower() in ("true", "yes", "on", "1")


def _split_keywords(value: str) -> Tuple[str, ...]:
    """Comma-separated, never pipe-separated — a `|` is this file format's column break."""
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _row_to_item(row: Dict[str, str]) -> Item:
    return Item(
        id=row.get("id", "").strip().strip("`"),
        title=row.get("title", "").strip(),
        source=row.get("source", "").strip().strip("`"),
        sticky=_truthy(row.get("sticky", "")),
        keywords=_split_keywords(row.get("keywords", "")),
        note=row.get("note", "").strip(),
        opened=row.get("opened", "").strip(),
        last_matched=row.get("lastmatched", "").strip(),
        status=row.get("status", "").strip().strip("`") or OPEN,
        closed_at=row.get("closedat", "").strip(),
        closed_reason=row.get("closedreason", "").strip(),
        scope=row.get("scope", "").strip().strip("`"),
        event_key=row.get("eventkey", "").strip().strip("`"),
        senders=tuple(s.lower() for s in _split_keywords(row.get("senders", ""))),
        waiting_since=row.get("waitingsince", "").strip().strip("`"),
        wait_days=row.get("waitdays", "").strip(),
        answered_through=row.get("answeredthrough", "").strip(),
        answered_note=row.get("answerednote", "").strip(),
        check=row.get("check", "").strip(),
    )


def load(path: Optional[str] = None) -> List[Item]:
    """Every row, in file order. `[]` when the file does not exist yet — not an error."""
    target = _path(path)
    if not os.path.exists(target):
        return []
    with open(target, "r", encoding="utf-8") as handle:
        rows = mdtable.parse(handle.read())
    return [_row_to_item(row) for row in rows]


def _in_scope(item: Item, scope: Optional[object]) -> bool:
    """`scope=None` means every row; a string means exactly that scope; a list/tuple/set means
    any of those scopes (a parent destination showing its sub-destinations' rows)."""
    if scope is None:
        return True
    if isinstance(scope, str):
        return item.scope == scope
    return item.scope in scope


def open_beacons(path: Optional[str] = None, scope: Optional[object] = None) -> List[Item]:
    return [item for item in load(path)
            if item.sticky and item.status == OPEN and _in_scope(item, scope)]


def open_sightings(path: Optional[str] = None, scope: Optional[object] = None) -> List[Item]:
    return [item for item in load(path)
            if not item.sticky and item.status == OPEN and _in_scope(item, scope)]


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now(timezone.utc).astimezone().date().isoformat()


def _new_id(source: str) -> str:
    """32 random bits per source per day. Was 16 (`token_hex(2)`): the birthday bound at 100
    rows is 1 - exp(-100*99 / (2 * 2**16)) = 7.3%, re-derived 2026-10-09 after a GPT review
    flagged it; at 32 bits it is ~1e-6. `append` also refuses to reuse an existing id."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return "%s-%s-%s" % (source or "item", stamp, secrets.token_hex(4))


@contextlib.contextmanager
def _locked(target: str):
    """Serialize every writer of one items file. `_set_cells` verifies the table's SHAPE
    against its own read, not that nobody else wrote in between; without this a concurrent
    append or close could be silently overwritten by a stale read (GPT review 2026-10-09)."""
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    with open(target + ".lock", "w") as handle:
        flockwait.exclusive(handle, "priority-items")  # bounded (2026-10-15 Bearing hang)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _atomic_write(target: str, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".items-", suffix=".tmp",
                               dir=os.path.dirname(target) or ".")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, target)


def _migrate_text(text: str) -> Tuple[str, bool]:
    located = mdtable.first_table(text)
    if located is None:
        return text, False
    header_at, headers, data_lines = located
    if tuple(headers) == COLUMNS:
        return text, False
    if tuple(headers) != COLUMNS[:len(headers)]:
        raise ValueError("items table columns %r are not a prefix of %r; refusing to guess"
                         % (headers, COLUMNS))
    missing = HEADERS[len(headers):]
    lines = text.split("\n")
    lines[header_at] = lines[header_at].rstrip() + "".join(" %s |" % h for h in missing)
    lines[header_at + 1] = lines[header_at + 1].rstrip() + "---|" * len(missing)
    for position in data_lines:
        lines[position] = lines[position].rstrip() + " |" * len(missing)
    return "\n".join(lines), True


def migrate(path: Optional[str] = None) -> Dict[str, object]:
    """Add any trailing columns an older file lacks, leaving every existing cell and all prose
    untouched; verified by parsing before and after. Idempotent."""
    target = _path(path)
    if not os.path.exists(target):
        return {"ok": True, "changed": False, "reason": "no file"}
    with _locked(target):
        return _migrate_locked(target)


def _migrate_locked(target: str) -> Dict[str, object]:
    with open(target, "r", encoding="utf-8") as handle:
        text = handle.read()
    new_text, changed = _migrate_text(text)
    if not changed:
        return {"ok": True, "changed": False, "reason": "already current"}
    before = [_row_to_item(r) for r in mdtable.parse(text)]
    after = [_row_to_item(r) for r in mdtable.parse(new_text)]
    if before != after:
        return {"ok": False, "changed": False, "reason": "migration would change a row"}
    _atomic_write(target, new_text)
    return {"ok": True, "changed": True, "reason": "added %d column(s)" % (
        len(mdtable.first_table(new_text)[1]) - len(mdtable.first_table(text)[1]))}


def _bootstrap(target: str) -> None:
    os.makedirs(os.path.dirname(target), exist_ok=True)
    header = "| " + " | ".join(HEADERS) + " |\n"
    sep = "|" + "|".join("---" for _ in HEADERS) + "|\n"
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("# Priority items\n\n")
        handle.write("One row per high-priority item: a sticky **beacon** (repeat until "
                     "closed) or a one-time **sighting**. See "
                     "docs/superpowers/specs/2026-10-06-high-priority-beacons-design.md.\n\n")
        handle.write(header)
        handle.write(sep)


def append(title: str, source: str, sticky: bool, keywords: Sequence[str] = (),
          note: str = "", path: Optional[str] = None, scope: str = "",
          event_key: str = "", senders: Sequence[str] = (), check=None) -> Item:
    """Add a new row, one cell per `HEADERS` entry (it used to hardcode eleven cells, which
    would have mis-shaped the table the moment a column was added). An older file is migrated
    first. Serialized with every other writer by `_locked`.

    `check` (creator boundary, 2026-10-22): an end-condition check spec (dict or JSON); none
    given records an explicit coverage gap, so no new row is silently unchecked."""
    from . import outcome
    check_cell = outcome.encode(outcome.creator_check(
        check, "standing watch: ends on his word or an attached end-condition check"
        if sticky else "sighting: reviewed once; no mechanical check given"))
    target = _path(path)
    with _locked(target):
        if not os.path.exists(target):
            _bootstrap(target)
        else:
            result = _migrate_locked(target)
            if not result["ok"]:
                raise ValueError("cannot append to %s: %s" % (target, result["reason"]))
        return _append_locked(target, title, source, sticky, keywords, note, scope, event_key,
                              senders, check_cell)


def _append_locked(target, title, source, sticky, keywords, note, scope, event_key,
                   senders=(), check_cell: str = "") -> Item:
    existing_ids = {i.id for i in load(target)}
    item_id = _new_id(source)
    while item_id in existing_ids:
        item_id = _new_id(source)
    opened = _today()
    item = Item(id=item_id, title=title, source=source, sticky=sticky,
                keywords=tuple(keywords), note=note, opened=opened, last_matched="",
                status=OPEN, closed_at="", closed_reason="", scope=scope, event_key=event_key,
                senders=_norm_senders(senders), check=check_cell)
    values = {
        "id": item_id, "title": title, "source": source,
        "sticky": "true" if sticky else "false", "keywords": ", ".join(keywords),
        "note": note, "opened": opened, "lastmatched": "", "status": OPEN,
        "closedat": "", "closedreason": "", "scope": scope, "eventkey": event_key,
        "senders": ", ".join(_norm_senders(senders)),
        "waitingsince": "", "waitdays": "", "answeredthrough": "", "answerednote": "",
        "check": check_cell,
    }
    cells = [values[column] for column in COLUMNS]
    row = "| " + " | ".join(mdtable.escape(c) for c in cells) + " |\n"
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(row)
    return item


def append_sighting_once(title: str, source: str, scope: str, event_key: str,
                         note: str = "", path: Optional[str] = None) -> Tuple[Item, bool]:
    """Append a scoped sighting unless a row with this `event_key` already exists — OPEN OR
    CLOSED. Bearing's 7-day rescans see the same message again and again; without this a
    sighting a project already closed would be re-created, and the one event would be both
    acknowledged and outstanding. Returns `(item, created)`."""
    if not event_key:
        raise ValueError("append_sighting_once needs an event_key")
    target = _path(path)
    with _locked(target):
        if not os.path.exists(target):
            _bootstrap(target)
        else:
            result = _migrate_locked(target)
            if not result["ok"]:
                raise ValueError("cannot append to %s: %s" % (target, result["reason"]))
        for item in load(target):
            if item.event_key == event_key:
                return item, False
        from . import outcome
        cell = outcome.encode(outcome.gap("Bearing sighting: reviewed once; no mechanical check"))
        return _append_locked(target, title, source, False, (), note, scope, event_key,
                              check_cell=cell), True


def close(item_id: str, reason: str, path: Optional[str] = None) -> Dict[str, object]:
    """Set status=closed, closed_at=now, closed_reason=reason on the row whose Id matches.

    Already-closed is a no-op SUCCESS (`changed: False`), same convention as
    `registry.set_state` — closing is a declaration of desired state, and a second close saying
    the same thing again is not a conflict.
    """
    existing = {i.id: i for i in load(path)}
    if item_id in existing and existing[item_id].status == CLOSED:
        return {"ok": True, "changed": False,
               "reason": "%s is already closed" % item_id}
    return _set_cells(path, item_id, {
        "status": CLOSED,
        "closedat": _now_iso(),
        "closedreason": reason,
    })


def version(item: Item) -> str:
    """A token that changes whenever any cell of the row changes (the resolver's guard)."""
    import hashlib
    return hashlib.sha256(repr(tuple(item)).encode("utf-8")).hexdigest()[:16]


def close_if(item_id: str, reason: str, expect_version: str,
             path: Optional[str] = None) -> Dict[str, object]:
    """`close`, but only if the row is still open and unchanged since `expect_version` was
    read (checked under the writers' lock). `{"ok": False, "stale": True}` otherwise."""
    target = _path(path)
    if not os.path.exists(target):
        return {"ok": False, "changed": False, "reason": "no registry at %s" % target}
    with _locked(target):
        item = {i.id: i for i in load(target)}.get(item_id)
        if item is None:
            return {"ok": False, "changed": False, "reason": "no row has Id %r" % item_id}
        if item.status == CLOSED or version(item) != expect_version:
            return {"ok": False, "changed": False, "stale": True,
                    "reason": "%s changed since its check ran" % item_id}
        return _set_cells_locked(target, item_id, {
            "status": CLOSED, "closedat": _now_iso(), "closedreason": reason})


def set_check(item_id: str, check, path: Optional[str] = None,
              trusted: bool = False) -> Dict[str, object]:
    """Attach (or replace) one row's check: a validated spec, or a gap dict. Never a cmd: this
    file syncs to every machine, and a synced spec must not run commands."""
    from . import outcome
    try:
        spec = outcome.validate_spec(check, trusted=False)
    except ValueError as e:
        return {"ok": False, "changed": False, "reason": "bad check: %s" % e}
    target = _path(path)
    if not os.path.exists(target):
        return {"ok": False, "changed": False, "reason": "no registry at %s" % target}
    with _locked(target):
        migrated = _migrate_locked(target)
        if not migrated["ok"]:
            return {"ok": False, "changed": False, "reason": migrated["reason"]}
        return _set_cells_locked(target, item_id, {"check": outcome.encode(spec)})


def sender_token(sender: str) -> str:
    """How a sender address is stored: `h:` + 16 hex of sha256(lowercased address).

    Since the 2026-10-13 history scrub the Senders cell never holds a colleague's address on
    github.com; matching hashes the incoming sender the same way. Idempotent on a token."""
    norm = (sender or "").strip().lower()
    if not norm or norm.startswith("h:") or "@" not in norm:
        return norm
    import hashlib
    return "h:" + hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _norm_senders(senders: Sequence[str]) -> Tuple[str, ...]:
    out = []
    for sender in senders or ():
        norm = sender_token(sender)
        if norm and norm not in out:
            out.append(norm)
    return tuple(out)


def set_terms(item_id: str, keywords: Optional[Sequence[str]] = None,
              senders: Optional[Sequence[str]] = None,
              path: Optional[str] = None) -> Dict[str, object]:
    """Rewrite a beacon's Keywords and/or Senders cells, and nothing else — through the same
    locked, parse-verified `_set_cells` every other row edit uses. An older file is migrated
    first so the Senders cell exists. Retuning a beacon is Lukas's call (SKILL.md); this is the
    writer that applies it, never a hand edit of a row other writers own."""
    target = _path(path)
    if not os.path.exists(target):
        return {"ok": False, "changed": False, "reason": "no registry at %s" % target}
    updates = {}
    if keywords is not None:
        for term in keywords:
            if "|" in term or "," in term:
                return {"ok": False, "changed": False,
                        "reason": "a term may not contain ',' or '|': %r" % term}
        updates["keywords"] = ", ".join(k.strip() for k in keywords if k.strip())
    if senders is not None:
        updates["senders"] = ", ".join(_norm_senders(senders))
    if not updates:
        return {"ok": True, "changed": False, "reason": "nothing to change"}
    with _locked(target):
        migrated = _migrate_locked(target)
        if not migrated["ok"]:
            return {"ok": False, "changed": False, "reason": migrated["reason"]}
        return _set_cells_locked(target, item_id, updates)


def set_title(item_id: str, title: str, path: Optional[str] = None) -> Dict[str, object]:
    """Rewrite a beacon's Title cell, and nothing else, through the same locked,
    parse-verified `_set_cells` every other row edit uses. Retitling is Lukas's call."""
    target = _path(path)
    title = " ".join((title or "").split())
    if not title:
        return {"ok": False, "changed": False, "reason": "a title may not be empty"}
    if "|" in title:
        return {"ok": False, "changed": False, "reason": "a title may not contain '|'"}
    if not os.path.exists(target):
        return {"ok": False, "changed": False, "reason": "no registry at %s" % target}
    with _locked(target):
        migrated = _migrate_locked(target)
        if not migrated["ok"]:
            return {"ok": False, "changed": False, "reason": migrated["reason"]}
        return _set_cells_locked(target, item_id, {"title": title})


_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HEARTBEAT = re.compile(r"^heartbeat:[a-z0-9-]+$")


def _beacon_cells(item_id: str, path: Optional[str], updates: Dict[str, str]) -> Dict[str, object]:
    target = _path(path)
    if not os.path.exists(target):
        return {"ok": False, "changed": False, "reason": "no registry at %s" % target}
    with _locked(target):
        migrated = _migrate_locked(target)
        if not migrated["ok"]:
            return {"ok": False, "changed": False, "reason": migrated["reason"]}
        item = {i.id: i for i in load(target)}.get(item_id)
        if item is None or not item.sticky:
            return {"ok": False, "changed": False, "reason": "no beacon has Id %r" % item_id}
        return _set_cells_locked(target, item_id, updates)


def set_waiting(item_id: str, since: str, days: Optional[int] = None,
                path: Optional[str] = None) -> Dict[str, object]:
    """Mark a beacon WAITING: its gem turns amber once `days` (default DEFAULT_WAIT_DAYS) pass
    with no news after max(since, last evidence). `since` is YYYY-MM-DD or
    `heartbeat:<machine key>`; an empty `since` clears the wait."""
    since = (since or "").strip()
    if not since:
        return _beacon_cells(item_id, path, {"waitingsince": "", "waitdays": ""})
    if not (_DAY.match(since) or _HEARTBEAT.match(since)):
        return {"ok": False, "changed": False,
                "reason": "--since must be YYYY-MM-DD or heartbeat:<machine key>: %r" % since}
    days = DEFAULT_WAIT_DAYS if days is None else int(days)
    if days < 1:
        return {"ok": False, "changed": False, "reason": "--days must be at least 1"}
    return _beacon_cells(item_id, path, {"waitingsince": since, "waitdays": str(days)})


def set_answered(item_id: str, through: str, note: str = "",
                 path: Optional[str] = None) -> Dict[str, object]:
    """Mark a beacon ANSWERED through a date: evidence dated at or before it stops lighting
    the gem (it reads green with `note` as its why); later evidence lights it again. The
    beacon stays open. An empty `through` clears the marker."""
    through, note = (through or "").strip(), " ".join((note or "").split())
    if through and not _DAY.match(through):
        return {"ok": False, "changed": False, "reason": "--through must be YYYY-MM-DD"}
    if "|" in note:
        return {"ok": False, "changed": False, "reason": "a note may not contain '|'"}
    return _beacon_cells(item_id, path, {"answeredthrough": through,
                                         "answerednote": note if through else ""})


_SEGMENT = re.compile(r"; (?=\d{4}-\d{2}-\d{2}: )")


def evidence(note: str) -> List[str]:
    """A note's segments: the opening text, then one `<date>: <line>` per match. Split only
    before a dated segment, because a line may itself contain "; "."""
    return [seg for seg in _SEGMENT.split(note) if seg] if note else []


def _evidence_key(segment: str) -> str:
    # `<date>: "<quote>" — <why>`: one quote seen twice in a day is one piece of evidence,
    # however differently the matcher explained it the second time.
    return segment.split(" — ", 1)[0].strip()


def migrate_evidence(path: Optional[str] = None, store: Optional[str] = None) -> Dict[str, int]:
    """Ruling 5 (2026-10-13), stop private text in github.com: move every quoted evidence segment
    of every row, and each Bearing sighting's text title, to the private store
    (`lib.privatestore`), leaving `[<tag>:jev_…]` ids. The opening (hand-written) note stays.
    Holds the items lock across the whole read-modify-write. Idempotent."""
    from lib import privatestore
    target = _path(path)
    privatestore.root(store)  # fail before touching anything if there is no private store here
    moved = {"rows": 0, "segments": 0, "titles": 0}
    with _locked(target):
        for item in load(target):
            updates = {}
            bearing_row = not item.sticky and bool(item.event_key)  # Bearing wrote all of it
            new_note = privatestore.migrate_note(item.id, item.note, store=store,
                                               keep_opening=not bearing_row)
            if new_note != item.note:
                updates["note"] = new_note
                tag = privatestore.ref_open()
                moved["segments"] += new_note.count(tag) - item.note.count(tag)
            if (not item.sticky and item.event_key and item.scope
                    and privatestore.ref_open() not in item.title and item.title.startswith(item.scope + ":")):
                text = item.title[len(item.scope) + 1:].strip()
                evid = privatestore.put_evidence(item.id, text, "sighting title", store=store,
                                               day=item.opened or None)
                updates["title"] = "%s: sighting %s" % (item.scope, privatestore.ref(evid))
                moved["titles"] += 1
            if updates:
                result = _set_cells_locked(target, item.id, updates)
                if not result.get("ok"):
                    raise RuntimeError(result.get("reason"))
                moved["rows"] += 1
    return moved


def update_evidence(item_id: str, matched_on: str, note_line: str,
                    path: Optional[str] = None) -> Dict[str, object]:
    """Update last_matched and APPEND one line to note, never overwriting what was there.
    A repeat of a quote already recorded for the same day is a no-op success."""
    existing = {i.id: i for i in load(path)}
    if item_id not in existing:
        return {"ok": False, "changed": False, "reason": "no row has Id %r" % item_id}
    prior_note = existing[item_id].note
    segment_key = _evidence_key("%s: %s" % (matched_on, note_line))
    if any(_evidence_key(seg) == segment_key for seg in evidence(prior_note)):
        return {"ok": True, "changed": False, "reason": "already recorded"}
    new_note = ("%s; %s: %s" % (prior_note, matched_on, note_line)) if prior_note \
        else ("%s: %s" % (matched_on, note_line))
    return _set_cells(path, item_id, {
        "lastmatched": matched_on,
        "note": new_note,
    })


def _set_cells(path: Optional[str], item_id: str,
              updates: Dict[str, str]) -> Dict[str, object]:
    """Surgical multi-cell writer for one row. Generalizes `registry.py::_set_cell`.

    Writes every column in `updates` on the single row whose `Id` cell equals `item_id`, then
    parses the candidate text back and refuses unless the ONLY cells that differ from the
    current file are exactly those columns on exactly that row — the same verification
    `registry.set_declared_remote` uses, because a column shift from this file format's pipe-
    escape bug reads as a perfectly valid file and only shows up once cells are addressed by
    header.
    """
    target = _path(path)
    if not os.path.exists(target):
        return {"ok": False, "changed": False, "reason": "no registry at %s" % target}
    with _locked(target):
        return _set_cells_locked(target, item_id, updates)


def _set_cells_locked(target: str, item_id: str,
                      updates: Dict[str, str]) -> Dict[str, object]:
    with open(target, "r", encoding="utf-8") as handle:
        text = handle.read()

    located = mdtable.first_table(text)
    if located is None:
        return {"ok": False, "changed": False, "reason": "%s holds no table" % target}
    _, headers, data_lines = located
    for column in list(updates) + ["id"]:
        if column not in headers:
            return {"ok": False, "changed": False,
                    "reason": "no %r column in %s" % (column, target)}
    id_at = headers.index("id")

    lines = text.split("\n")
    before = mdtable.parse(text)
    targets = []
    for position in data_lines:
        cells = mdtable.raw_cells(lines[position])
        if id_at + 1 < len(cells) and cells[id_at + 1].strip().strip("`") == item_id:
            targets.append(position)
    if not targets:
        return {"ok": False, "changed": False, "reason": "no row has Id %r" % item_id}
    if len(targets) > 1:
        return {"ok": False, "changed": False,
               "reason": "%d rows claim Id %r" % (len(targets), item_id)}

    position = targets[0]
    cells = mdtable.raw_cells(lines[position])
    for column, value in updates.items():
        at = headers.index(column)
        if at + 1 >= len(cells):
            return {"ok": False, "changed": False,
                   "reason": "row %r has no %r cell" % (item_id, column)}
        cells[at + 1] = " %s " % mdtable.escape(value) if value else " "

    candidate = list(lines)
    candidate[position] = "|".join(cells)
    new_text = "\n".join(candidate)

    after = mdtable.parse(new_text)
    drift = _only_row_changed(before, after, item_id, headers.index("id"), set(updates))
    if drift is not None:
        return {"ok": False, "changed": False,
               "reason": "refusing to write: %s" % drift}

    _atomic_write(target, new_text)
    return {"ok": True, "changed": True, "reason": "updated %s on %s" % (sorted(updates), item_id)}


def _only_row_changed(before: List[Dict[str, str]], after: List[Dict[str, str]],
                      item_id: str, id_at: int,
                      allowed_columns: set) -> Optional[str]:
    if len(before) != len(after):
        return "row count changed from %d to %d" % (len(before), len(after))
    id_key = HEADERS[id_at].lower()
    for old_row, new_row in zip(before, after):
        if set(old_row) != set(new_row):
            return "a row's columns changed shape"
        for key in old_row:
            if old_row[key] == new_row[key]:
                continue
            is_target_row = old_row.get(id_key, "").strip().strip("`") == item_id
            if is_target_row and key in allowed_columns:
                continue
            return "row %r column %r changed from %r to %r unexpectedly" % (
                old_row.get(id_key, "?"), key, old_row[key], new_row[key])
    return None
