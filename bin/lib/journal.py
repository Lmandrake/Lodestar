"""The append-only event journal.

Every change to the board is also written here as its own uniquely-named file
(`journal/YYYY-MM-DD/<epoch>-<uuid>.json`). That is what makes two machines safe on one
repo: concurrent writes create different files, so git merges them without a conflict
instead of fighting over one log (decision D5 in docs/DECISIONS.md).

Loop *state* is re-derivable from this journal. Loop *prose* is not — the freeform body of
a loop file never enters the journal, so `board.py --rebuild` restores frontmatter and
preserves bodies rather than claiming to reconstruct them (decision D-F).

Reads are deliberately forgiving: one corrupt event file must never stop a rebuild, so
`load` collects problems and carries on.
"""

import json
import os
import uuid
from typing import Any, Dict, List, Optional, Tuple

from . import clock, machine, paths, untracked

# BUILD_SPEC.md section 3, plus "run".
#
# "run" is the unattended shadow runner's event (Phase 3, §6). It is here rather than in a
# second log because the caps and the go-live gate need an event-sourced, synced, durable
# count of what ran — which is exactly what this directory already is. Verified safe against
# `loops.derive`: it switches on the event types it knows and an unrecognized type falls
# through to the `last_touch` update only, so a run event animates the loop without
# reopening, closing, or reclassifying it.
#
# "handoff" is the reboot ritual's event. `claude/global/55-handoff-on-reboot.md` instructs every
# session to "append a `handoff` event to journal/<date>/" before a restart, and until 2026-09-15
# `append` raised UnknownEvent for exactly that word — the instruction that governs every session
# named an event the journal refused to write. It is safe for the same reason "run" is: `derive`
# switches on the types it knows, so a handoff moves `last_touch` and cannot reopen a closed loop.
#
# "lifecycle" is the repo-lifecycle converger's event (Phase 3): a rename, rehome, retirement or
# quarantine reaching a recognised state on one machine. It is here for the reason "run" is —
# the record must be event-sourced, synced and durable, and this directory already is all three —
# and it is safe for the same reason: `loops.derive` switches on the types it knows and an
# unrecognized type falls through to the `last_touch` update only, so a lifecycle event animates
# the loop without reopening, closing or reclassifying it.
#
# 🔴 ONLY METADATA. §3E forbids rescue payloads entering the board: this directory is the channel
# the home machine reads, so journalling the CONTENT of something being evicted for confidentiality would
# republish exactly what is being removed. Ids, paths, counts and verdicts — never file contents.
#
# "backfill" and "retire" (2026-10-25, the 39-file round-trip drift): `backfill` records loop
# frontmatter fields that no earlier event carried (title, class, repo, opened ...) so a rebuild
# reproduces the file; it changes no state and no last_touch. `retire` marks a loop whose file was
# deliberately deleted (a rename, a merge), so a rebuild does not resurrect it.
#
# "tender" (2026-10-26) is the Tender harvest's event (lib/tenderharvest.py): one per harvested
# receipt, ids and verdict tokens only. Safe for the reason "run" is: `derive` ignores the type.
EVENT_TYPES = ("open", "touch", "progress", "park", "resume", "close", "run", "skill-prune",
               "handoff", "lifecycle", "backfill", "retire", "tender")


class UnknownEvent(Exception):
    """An event type outside the documented set."""


def append(
    loop: str,
    event: str,
    note: Optional[str] = None,
    tokens: Optional[int] = None,
    extra: Optional[Dict[str, Any]] = None,
    machines_path: Optional[str] = None,
) -> str:
    """Write one event file and return its path."""
    if event not in EVENT_TYPES:
        raise UnknownEvent(
            "unknown event type %r; expected one of %s" % (event, ", ".join(EVENT_TYPES))
        )

    record = {
        # Microsecond precision is a correctness requirement, not decoration. With
        # second precision, two events written in the same second tie, and the only
        # tiebreak is the random uuid in the filename -- so a touch and a close issued
        # back to back could replay in either order and `derive` would produce the wrong
        # final state. Still valid ISO 8601.
        "ts": clock.now(timespec="microseconds"),
        "loop": loop,
        "event": event,
        "machine": machine.role(machines_path),
        "runtime": machine.runtime(),
    }  # type: Dict[str, Any]
    if note:
        record["note"] = note
    if tokens is not None:
        record["tokens"] = int(tokens)
    session = machine.session_id()
    if session:
        record["session"] = session
    if extra:
        record.update(extra)
    # 🔴 The note is prose and Lodestar's remote is github.com (ruling 5, 2026-10-13): it goes
    # to the private store and the event keeps `note_ref`, or — on a machine without the store —
    # to a local stash with `note_withheld`. See lib/proseguard.py.
    from . import proseguard
    record = proseguard.record_note(record)

    directory = paths.ensure_dir(os.path.join(paths.journal_dir(), clock.today()))
    name = "%d-%s.json" % (clock.epoch(), uuid.uuid4().hex[:6])
    path = os.path.join(directory, name)

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)
    return path


def load() -> Tuple[List[Dict[str, Any]], List[Tuple[str, str]]]:
    """Return (events, problems), events ordered oldest first.

    `problems` is a list of (path, reason) for files that could not be read. Callers
    surface them; nothing here raises, because a single bad file must not block a rebuild
    of the whole board.
    """
    events = []  # type: List[Dict[str, Any]]
    problems = []  # type: List[Tuple[str, str]]
    root = paths.journal_dir()
    if not os.path.isdir(root):
        return events, problems

    for day in sorted(os.listdir(root)):
        day_dir = os.path.join(root, day)
        if not os.path.isdir(day_dir):
            continue
        for name in sorted(os.listdir(day_dir)):
            if not name.endswith(".json"):
                continue  # .tmp files mid-write, .gitkeep, editor droppings
            path = os.path.join(day_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    record = json.load(handle)
            except (OSError, ValueError) as error:
                problems.append((path, str(error)))
                continue
            if not isinstance(record, dict) or "loop" not in record:
                problems.append((path, "not an event object"))
                continue
            if untracked.event_untracked(record):
                continue  # out of Lodestar's scope (lib/untracked.py); history stays on disk
            record["_path"] = path
            events.append(record)

    # Sort by timestamp, then filename, so events written in the same second by the same
    # machine still replay in a stable, reproducible order.
    events.sort(key=lambda r: (clock.sort_key(r.get("ts")), r.get("_path", "")))
    return events, problems


def for_loop(loop: str) -> List[Dict[str, Any]]:
    events, _ = load()
    return [event for event in events if event.get("loop") == loop]
