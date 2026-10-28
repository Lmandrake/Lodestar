"""Loop state files and state derivation from the journal.

One file per loop (`loops/<id>.md`) is half of the conflict-free model: two sessions
editing *different* loops never collide (decision D5).

One behavior here is load-bearing rather than incidental:

**A new loop defaults to `background`, not `active`.** 🔴 active means dominant focus, so
defaulting to it would mean every casual touch silently claimed that status. Entering the
active class is a deliberate act, independent of any numeric limit on how many may be open.

There used to be a second load-bearing behavior here: a WIP guard capping 🔴 active+open
loops at three, evaluated against the prospective state so a re-touch of an already-open
active loop never tripped it. Lukas ruled 2026-09-03 that the cap "is just not practicable"
(docs/DECISIONS.md D24, superseding D3), so `check_wip`/`WipExceeded` are gone and `upsert`
no longer refuses a fourth active loop. `is_active_open`/`active_open_ids` remain — other
code (board rendering, `close.py`) still wants the open-🔴 count for *display*, not gating.
"""

import os
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import clock, duration, frontmatter, machine, paths, untracked

# BUILD_SPEC.md section 2, in the order it documents.
FIELD_ORDER = (
    "id",
    "title",
    "class",
    "state",
    "account",
    "repo",
    "est_close",
    "actual_spent",
    "opened",
    "last_touch",
    "deadline",
    # Typed value plus the note beside it. The prose these hold used to sit *inside* the field
    # above — "2026-09-16 (the session runs in the afternoon)" — which left the date unparseable
    # and so made every time-relative rendering impossible. See lib/loopfields.py.
    "deadline_note",
    "est_note",
    "spent_note",
)

CLASSES = ("active", "background")
STATES = ("open", "parked", "closed")

# Fields a journal event may carry so that state is faithfully re-derivable.
TRACKED_FIELDS = ("title", "class", "account", "repo", "est_close", "deadline")
#: Extra fields only a `backfill` event may carry (the file held them, no earlier event did).
BACKFILL_FIELDS = TRACKED_FIELDS + ("opened", "actual_spent", "last_touch", "deadline_note", "est_note",
                                    "spent_note")
#: Events that never write the loop file. A rebuild that let them create a loop or move
#: `last_touch` would disagree with the real file (measured 2026-10-25: 8 loops, via handoffs).
FILE_SILENT_EVENTS = ("handoff",)


class InvalidLoop(Exception):
    """A loop id, class, or state that cannot be stored."""


def valid_id(loop_id: str) -> bool:
    """Loop ids become filenames, so keep them boring."""
    if not loop_id or loop_id in (".", ".."):
        return False
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-_.")
    return set(loop_id) <= allowed


def _require_id(loop_id: str) -> str:
    if not valid_id(loop_id):
        raise InvalidLoop(
            "invalid loop id %r: use lowercase letters, digits, dash, underscore, dot"
            % (loop_id,)
        )
    return loop_id


def load(loop_id: str) -> Optional[Tuple[Dict[str, str], str]]:
    """Return (fields, body) for a loop, or None if it has no file yet."""
    path = paths.loop_file(_require_id(loop_id))
    if not os.path.exists(path) or untracked.is_untracked(loop_id):
        return None
    fields, body = frontmatter.read_file(path)
    if untracked.is_untracked(repo=fields.get("repo", "")):
        return None
    return fields, body


def all_loops() -> Dict[str, Tuple[Dict[str, str], str]]:
    """Every loop on the board, keyed by id. Unreadable files are skipped."""
    directory = paths.loops_dir()
    result = {}  # type: Dict[str, Tuple[Dict[str, str], str]]
    if not os.path.isdir(directory):
        return result
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".md"):
            continue
        loop_id = name[:-3]
        if untracked.is_untracked(loop_id):
            continue  # outside Lodestar's scope (lib/untracked.py)
        try:
            entry = frontmatter.read_file(os.path.join(directory, name))
        except (OSError, ValueError):
            continue  # a torn or hand-mangled file must not hide the rest of the board
        if untracked.is_untracked(repo=entry[0].get("repo", "")):
            continue
        result[loop_id] = entry
    return result


def is_active_open(fields: Dict[str, str]) -> bool:
    return fields.get("class") == "active" and fields.get("state") == "open"


def active_open_ids(loops: Optional[Dict[str, Tuple[Dict[str, str], str]]] = None) -> List[str]:
    loops = all_loops() if loops is None else loops
    return sorted(i for i, (fields, _) in loops.items() if is_active_open(fields))


def order_fields(fields: Dict[str, str]) -> Dict[str, str]:
    """Documented fields first in spec order, then anything else as-found.

    Unknown keys are kept because Cowork may add fields this code has never heard of.
    """
    ordered = {}  # type: Dict[str, str]
    for key in FIELD_ORDER:
        if key in fields:
            ordered[key] = fields[key]
    for key, value in fields.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


def upsert(
    loop_id: str,
    updates: Optional[Dict[str, str]] = None,
    body: Optional[str] = None,
    machines_path: Optional[str] = None,
) -> Tuple[Dict[str, str], str]:
    """Create or refresh a loop file, stamping last_touch. Returns (fields, body).

    `est_close` is left empty rather than invented when not supplied — ritual §3 wants it
    surfaced, and a fabricated estimate is worse than a visibly missing one.

    🔴 **The loop file holds only frontmatter and a pointer line** (ruling 5, 2026-10-13):
    `body`, the prose, is written to the private store by `proseguard.write_body`, and the returned
    body is that prose (None-preserving: the stored prose when `body` is None). Prose already
    sitting in the file — a hand edit — is moved there first. Raises
    `privatestore.Unavailable` BEFORE writing anything when `body` is given and this machine has
    no private store: prose is never written to Lodestar instead.
    """
    from . import privatestore, proseguard

    _require_id(loop_id)
    updates = dict(updates or {})
    if body is not None:
        privatestore.root()  # fail before touching anything

    existing = load(loop_id)
    if existing is None:
        fields = {
            "id": loop_id,
            "title": loop_id,
            "class": "background",
            "state": "open",
            "account": "",
            "repo": "",
            "est_close": "",
            "actual_spent": duration.UNKNOWN,
            "opened": clock.today(),
            "last_touch": "",
            "deadline": "",
        }
        current_body = ""
    else:
        fields, current_body = existing
        fields = dict(fields)

    for key, value in updates.items():
        if value is not None:
            fields[key] = value

    if fields.get("class") not in CLASSES:
        raise InvalidLoop(
            "class must be one of %s, got %r" % (", ".join(CLASSES), fields.get("class"))
        )
    if fields.get("state") not in STATES:
        raise InvalidLoop(
            "state must be one of %s, got %r" % (", ".join(STATES), fields.get("state"))
        )

    fields["last_touch"] = "%s %s" % (clock.now(timespec="minutes"),
                                      machine.stamp(machines_path))
    fields = order_fields(fields)

    paths.ensure_dir(paths.loops_dir())
    path = paths.loop_file(loop_id)
    if not proseguard.is_clean_body(current_body):
        proseguard.fix(paths.root(), [os.path.relpath(path, paths.root())])
    if body is not None:
        proseguard.write_body(loop_id, body)
        current_body = body
    else:
        current_body = proseguard.read_body(loop_id) or ""
    frontmatter.write_file(path, fields, proseguard.pointer(loop_id))
    return fields, current_body


def derive(events: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    """Replay journal events into per-loop frontmatter state.

    This is the event-sourcing half of the model and what `board.py --rebuild` uses. It
    reconstructs *state* only; freeform prose never enters the journal, so bodies are the
    caller's to preserve (decision D-F).

    **Precedence for `state`:** an event that carries a `state` field wins outright; an
    event that does not is read from its type (`park` → parked, `close` → closed, and
    activity after a close reopens). The type-only reading is a fallback for events written
    before writers journaled state — it is an inference, and it is the reason a normal
    `touch` of a closed loop used to make a rebuild silently disagree with the loop file.
    """
    derived = {}  # type: Dict[str, Dict[str, str]]
    retired = set()  # type: set
    backfills = {}  # type: Dict[str, List[Dict[str, Any]]]

    for event in events:
        loop_id = event.get("loop")
        if not loop_id:
            continue
        if untracked.event_untracked(event):
            continue  # out of scope; a rebuild must not resurrect it (lib/untracked.py)
        kind = event.get("event")
        if kind == "retire":
            derived.pop(loop_id, None)
            retired.add(loop_id)
            continue
        if kind == "backfill":
            if loop_id in derived:
                for key in BACKFILL_FIELDS:
                    if event.get(key):
                        derived[loop_id][key] = str(event[key])
            else:
                backfills.setdefault(loop_id, []).append(event)
            continue
        if kind in FILE_SILENT_EVENTS:
            continue
        retired.discard(loop_id)
        fields = derived.setdefault(
            loop_id,
            {
                "id": loop_id,
                "title": loop_id,
                "class": "background",
                "state": "open",
                "account": "",
                "repo": "",
                "est_close": "",
                "actual_spent": duration.UNKNOWN,
                "opened": "",
                "last_touch": "",
                "deadline": "",
            },
        )

        for early in backfills.pop(loop_id, ()):   # a backfill that arrived before the loop did
            for key in BACKFILL_FIELDS:
                if early.get(key):
                    fields[key] = str(early[key])
        timestamp = event.get("ts") or ""
        if kind == "open" and not fields["opened"]:
            fields["opened"] = timestamp[:10]
        if not fields["opened"]:
            fields["opened"] = timestamp[:10]

        for key in TRACKED_FIELDS:
            if event.get(key):
                fields[key] = str(event[key])

        if kind == "park":
            fields["state"] = "parked"
        elif kind == "resume":
            fields["state"] = "open"
        elif kind == "close":
            fields["state"] = "closed"
            if event.get("spent"):
                fields["actual_spent"] = str(event["spent"])
        elif kind in ("open", "touch", "progress"):
            if fields["state"] == "closed":
                # Legacy fallback for events written before state was journaled. It is a
                # *guess*, and guessing is what broke the Phase 0 gate: a plain touch of a
                # closed loop left the file `closed` and reconstructed it `open`. Events
                # that declare their state (just below) never reach this line.
                fields["state"] = "open"

        # An event's own `state` is authoritative, whatever its type says. That value is
        # what the writer put in `loops/<id>.md`, so honouring it is precisely what keeps
        # `board.py --rebuild` byte-identical — including for a touch that annotates an
        # already-closed loop. A junk value is ignored rather than fatal, matching the
        # forgiving reads in lib/journal.py.
        if event.get("state") in STATES:
            fields["state"] = str(event["state"])

        if timestamp:
            stamp = "%s/%s" % (event.get("machine", "?"), event.get("runtime", "?"))
            # Must match the format `upsert` writes, offset included, or a --rebuild
            # silently reformats every loop file it touches and the board churns.
            fields["last_touch"] = "%s %s" % (_minute_stamp(timestamp), stamp)

    return {loop_id: order_fields(fields) for loop_id, fields in derived.items()
            if not untracked.is_untracked(repo=fields.get("repo", ""))}


def _minute_stamp(timestamp: str) -> str:
    """Reduce an event timestamp to the minute precision loop files use, keeping the offset."""
    try:
        return clock.parse(timestamp).isoformat(timespec="minutes")
    except ValueError:
        return timestamp[:16]
