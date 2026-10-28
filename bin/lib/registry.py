"""The sync participation registry.

`MULTI_MACHINE_SYNC.md` proposes seeding this from Waypoint's active roots. It cannot be
Waypoint directly: several of Waypoint's rows point at Google Drive project
folders, which are Google-Drive-replicated and must never hold a `.git` directory (that is
Option C, the anti-pattern that document bans), and one points at a third party's repo.

So Waypoint is the *candidate* source and this table is the registry — which also satisfies
that document's safety principle #4, opt-in rather than opt-out.

**Two switches per repo, not one** (owner, 2026-09-25):

- `Send` — "I'm OK with my work being committed and pushed out." Governs the auto-commit and
  the push.
- `Receive` — "I'm OK receiving updates from elsewhere." Governs the fast-forward (and, only
  with Send on too, the rebase of local commits over a diverged remote).

A repo participates when either is on. The old single `Sync` column is still read when a
row has neither new column — `on` means both, `off` means neither — because each Mac
self-updates mid-sweep and must be able to read either shape during the transition.

`Machines` (optional) names the machine roles that carry the repo; blank means all. A second
table, **Machine paths**, overrides one repo's path on one machine (e.g. a repo living at
`~/dev/other-name` on `home`). Old readers see only the first table, so appending it is safe.
"""

import os
import re
from typing import Dict, List, NamedTuple, Optional, Tuple

from . import mdtable, paths

TRUTHY = ("on", "yes", "true", "1")

MACHINE_PATHS_HEADING = "## Machine paths"


class Entry(NamedTuple):
    name: str
    path: str
    branch: str  # empty means "whatever branch it is currently on"
    sync: bool   # participates at all: send or receive. Kept for every older caller.
    notes: str
    send: bool = True
    receive: bool = True
    machines: Tuple[str, ...] = ()
    repo_id: str = ""              # from column "Id" -- stable, slug-shaped, never guessed
    former: Tuple[str, ...] = ()   # from column "Former" -- prior names AND prior remote URLs
    remote: str = ""               # from column "Remote"
    state: str = "active"          # from column "State" -- missing/blank must read as active
    mirror: str = ""               # from column "Mirror" -- https URL of the second home, or ""


def _truthy(value: str) -> bool:
    return value.strip().strip("`").lower() in TRUTHY


def _split_machines(value: str) -> Tuple[str, ...]:
    cleaned = value.strip().strip("`").lower()
    if not cleaned or cleaned in ("all", "*", "-", "—"):
        return ()
    return tuple(part for part in re.split(r"[,\s]+", cleaned) if part)


def _split_former(value: str) -> Tuple[str, ...]:
    """Like `_split_machines`, but case-preserving.

    `Former` holds prior repo names AND prior remote URLs mixed together, and a URL path
    is case-sensitive even though its host is not -- lowercasing here would silently break
    a lookup against a real remote.
    """
    cleaned = value.strip().strip("`")
    if not cleaned or cleaned.lower() in ("all", "*", "-", "—"):
        return ()
    return tuple(part for part in re.split(r"[,\s]+", cleaned) if part)


def _this_machine() -> Tuple[Optional[str], Optional[str]]:
    """(role, key) for this machine, or (None, None) when it cannot be resolved.

    Imported lazily and never allowed to raise: a registry read must not fail because the
    machine is unregistered — `sync.py` reports that itself, loudly, with exit 2.
    """
    try:
        from . import machine
        key = machine.key()
        try:
            return machine.role(), key
        except machine.UnknownMachine:
            return None, key
    except Exception:
        return None, None


def machine_paths(text: str) -> Dict[Tuple[str, str], str]:
    """{(repo name, machine): path} from the Machine paths table, if the document has one."""
    found = {}  # type: Dict[Tuple[str, str], str]
    for headers, rows in mdtable.tables(text)[1:]:
        if "machine" not in headers or "path" not in headers:
            continue
        for row in rows:
            name = (row.get("repo") or row.get("name") or "").strip().strip("`")
            where = row.get("machine", "").strip().strip("`").lower()
            target = row.get("path", "").strip().strip("`")
            if name and where and target:
                found[(name, where)] = target
    return found


def declared_paths(registry_path: Optional[str] = None) -> Dict[str, str]:
    """{name: ~-expanded Path} exactly as each row declares it, with NO machine override.

    The un-overridden half of `entries()`. Callers holding a path written against the declared
    location (a skill target in `claude/skills.md`) need it to translate onto this machine's.
    """
    path = registry_path or paths.registry_file()
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        rows = mdtable.parse(handle.read())
    found = {}  # type: Dict[str, str]
    for row in rows:
        name = row.get("name", "").strip().strip("`")
        raw_path = row.get("path", "").strip().strip("`")
        if name and raw_path:
            found[name] = os.path.expanduser(raw_path)
    return found


def entries(registry_path: Optional[str] = None, role: Optional[str] = None,
            key: Optional[str] = None) -> List[Entry]:
    """Every row, with this machine's path override applied.

    `role`/`key` default to this machine's. Pass them explicitly in tests, or to ask what
    another machine would see.
    """
    path = registry_path or paths.registry_file()
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    rows = mdtable.parse(text)
    if role is None and key is None:
        role, key = _this_machine()
    overrides = machine_paths(text)
    here = [value for value in (role, key) if value]

    result = []
    for row in rows:
        name = row.get("name", "").strip().strip("`")
        raw_path = row.get("path", "").strip().strip("`")
        if not name or not raw_path:
            continue
        for where in here:
            if (name, where.lower()) in overrides:
                raw_path = overrides[(name, where.lower())]
                break

        # A missing switch falls back to the legacy `Sync` column, and to off without one.
        legacy = _truthy(row.get("sync", ""))
        send = _truthy(row["send"]) if "send" in row else legacy
        receive = _truthy(row["receive"]) if "receive" in row else legacy

        # A missing State column must never mean retired -- it means the schema predates
        # the column, which is not a lifecycle decision.
        state = row.get("state", "").strip().strip("`").lower() or "active"

        result.append(Entry(
            name=name,
            path=os.path.expanduser(raw_path),
            branch=row.get("branch", "").strip().strip("`"),
            sync=send or receive,
            notes=row.get("notes", "").strip(),
            send=send,
            receive=receive,
            machines=_split_machines(row.get("machines", "")),
            repo_id=row.get("id", "").strip().strip("`"),
            former=_split_former(row.get("former", "")),
            remote=row.get("remote", "").strip().strip("`"),
            state=state,
            mirror=row.get("mirror", "").strip().strip("`"),
        ))
    return result


def on_machine(entry: Entry, role: Optional[str], key: Optional[str] = None) -> bool:
    """True when `entry` is carried by this machine. Blank `Machines` means everywhere.

    An unresolvable machine (role and key both None) reads as "carried": the caller that
    needs a role fails loudly on its own, and silently dropping rows would hide that.
    """
    if not entry.machines:
        return True
    if role is None and key is None:
        return True
    return any(value and value.lower() in entry.machines for value in (role, key))


def _here(registry_path: Optional[str], role: Optional[str],
          key: Optional[str]) -> Tuple[List[Entry], Optional[str], Optional[str]]:
    if role is None and key is None:
        role, key = _this_machine()
    return entries(registry_path, role, key), role, key


def participating(registry_path: Optional[str] = None, role: Optional[str] = None,
                  key: Optional[str] = None) -> List[Entry]:
    rows, role, key = _here(registry_path, role, key)
    return [entry for entry in rows if entry.sync and on_machine(entry, role, key)]


def excluded(registry_path: Optional[str] = None, role: Optional[str] = None,
             key: Optional[str] = None) -> List[Entry]:
    """Rows switched off entirely. Rows merely carried elsewhere are `elsewhere()`."""
    rows, role, key = _here(registry_path, role, key)
    return [entry for entry in rows if not entry.sync and on_machine(entry, role, key)]


def elsewhere(registry_path: Optional[str] = None, role: Optional[str] = None,
              key: Optional[str] = None) -> List[Entry]:
    """Participating rows whose `Machines` column leaves this machine out."""
    rows, role, key = _here(registry_path, role, key)
    return [entry for entry in rows if entry.sync and not on_machine(entry, role, key)]


def by_path(registry_path: Optional[str] = None) -> Dict[str, Entry]:
    return {os.path.realpath(entry.path): entry for entry in entries(registry_path)}


def _tilde(path: str) -> str:
    home = os.path.expanduser("~")
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def record_machine_path(name: str, where: str, target: str,
                        registry_path: Optional[str] = None) -> bool:
    """Record that repo `name` lives at `target` on machine `where`. Returns True if written.

    Appends a row to the Machine paths table, creating the table at the end of the file if it
    is not there yet. An existing row for the same (repo, machine) is left alone and reported
    as not written: changing where a repo lives is a human edit, never an inference.
    """
    path = registry_path or paths.registry_file()
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    if (name, where.lower()) in machine_paths(text):
        return False

    row = "| %s | %s | %s |" % (name, where.lower(), _tilde(target))
    lines = text.split("\n")
    start = None
    for index, line in enumerate(lines):
        if line.strip().lower() == MACHINE_PATHS_HEADING.lower():
            start = index
            break

    if start is None:
        block = ["", MACHINE_PATHS_HEADING, "",
                 "Where a repo lives on one machine when it is not the Path above. Written by "
                 "the sweep when it adopts an existing checkout instead of cloning a duplicate.",
                 "", "| Repo | Machine | Path |", "|------|---------|------|", row, ""]
        while lines and lines[-1] == "":
            lines.pop()
        lines.extend(block)
    else:
        # Insert after the last row of the first table below the heading.
        index = start + 1
        while index < len(lines) and not lines[index].strip().startswith("|"):
            index += 1
        while index < len(lines) and lines[index].strip().startswith("|"):
            index += 1
        lines.insert(index, row)

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    os.replace(tmp, path)
    return True


#: The columns the surgical writer may aim at: one cell of one row, and never another.
REMOTE_COLUMN = "remote"
STATE_COLUMN = "state"
ID_COLUMN = "id"
MIRROR_COLUMN = "mirror"

#: The `State` value a retirement declares. `validate._KNOWN_STATES` is {active, retired}, so
#: this is the only non-default value the schema admits — spelled once, here, rather than as a
#: literal at each writer, because a typo'd state reads as an unknown-state fault rather than
#: as a failed write.
RETIRED = "retired"


def set_declared_remote(repo_id: str, remote: str,
                        registry_path: Optional[str] = None) -> Dict[str, object]:
    r"""Put `remote` in the `Remote` cell of the row whose `Id` is `repo_id`.

    `{"ok": bool, "changed": bool, "reason": str}` — `ok` False means desired state was NOT
    written and the caller must not report success.

    🔴 **This writes to shared, hand-edited desired state, which is a bigger blast radius than
    anything else the lifecycle executors touch.** Three machines and a human all edit
    `sync/registry.md`, and one row carries a thousand-word `Notes` cell. So the contract is
    narrow on purpose:

    - **One cell of one row.** The file is never re-rendered, never re-aligned and never
      reordered. `mdtable.raw_cells` keeps every other segment byte-for-byte (its join is the
      identity), so a cell this function did not aim at cannot move.
    - **A missing `Remote` column is a refusal, not a column to add.** Adding one would rewrite
      the header, the separator and all 53 rows — the opposite of surgical.
    - **A short row is a refusal.** Padding it to reach the column would shift every cell after
      the gap, which is exactly the silent column shift `mdtable`'s pipe-escape bug caused.
    - **The result is parsed back before the write lands.** The new text is parsed in memory and
      compared row-for-row against the old; unless the ONLY difference in the whole table is the
      one intended cell, nothing is replaced and this refuses. So a file that would have been
      corrupted is left untouched instead, rather than needing a human to notice afterwards.

    Only the first contiguous table is in scope (`mdtable.first_table`), which keeps the
    **Machine paths** table below it structurally unreachable from here.

    The mechanism is `_set_cell`, shared with `set_state` — one writing convention for this
    file and not two. The five private `_escape` copies CLAUDE.md §7 records are what a second
    convention turns into.
    """
    return _set_cell(repo_id, REMOTE_COLUMN, remote, registry_path)


def set_state(repo_id: str, state: str,
              registry_path: Optional[str] = None) -> Dict[str, object]:
    """Put `state` in the `State` cell of the row whose `Id` is `repo_id`.

    Same contract, same guards and the same code as `set_declared_remote` — one cell of one
    row, a missing column or a short row is a refusal rather than a table to re-render, and the
    candidate text is parsed back and compared row-for-row before `os.replace`. Read that
    docstring; nothing here weakens any of it.

    🔴 **Already-`state` is a no-op SUCCESS, never a refusal, and that is what makes a
    fleet-wide retirement safe.** A purge plan is approved once and executed on every machine
    named by it, so the second machine to converge finds the cell already written by the first
    — and if that read as failure the plan would report `partial` for ever on every machine
    except whichever one happened to go first. The write is idempotent because it is a
    *declaration of desired state*, not a record of an act: two machines writing `retired` into
    one cell are making the same statement, so whichever lands second has nothing to add.
    Concurrency beyond that is git's: the cell is one line in a file peers merge, and two peers
    writing the identical value produce no conflict at all.

    `state` is not validated against a vocabulary here on purpose — `validate` owns which
    states are known and reports an unknown one as a fault, which is a better failure than a
    writer that silently refuses a value a future schema adds.
    """
    return _set_cell(repo_id, STATE_COLUMN, state, registry_path)


def set_mirror(repo_id: str, mirror: str,
               registry_path: Optional[str] = None) -> Dict[str, object]:
    """Put `mirror` in the `Mirror` cell of the row whose `Id` is `repo_id`. Same contract and
    the same code as `set_declared_remote`; `validate` owns whether the value is acceptable."""
    return _set_cell(repo_id, MIRROR_COLUMN, mirror, registry_path)


def _set_cell(repo_id: str, column: str, value: str,
              registry_path: Optional[str] = None) -> Dict[str, object]:
    """The surgical single-cell writer. See `set_declared_remote` for the whole argument."""
    path = registry_path or paths.registry_file()
    if not repo_id:
        return {"ok": False, "changed": False,
                "reason": "no repo id given, so no row can be identified"}
    if "\n" in value or "\r" in value:
        return {"ok": False, "changed": False,
                "reason": "a %s containing a newline is not a URL and a table cell is one "
                          "line; refusing rather than silently flattening it" % column}
    if not os.path.exists(path):
        return {"ok": False, "changed": False, "reason": "no registry at %s" % path}

    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()

    located = mdtable.first_table(text)
    if located is None:
        return {"ok": False, "changed": False,
                "reason": "%s holds no markdown table" % path}
    _, headers, data_lines = located
    if column not in headers or ID_COLUMN not in headers:
        return {"ok": False, "changed": False,
                "reason": "the registry table has no %s column, and adding one would rewrite "
                          "every row rather than one cell"
                          % (column if column not in headers else ID_COLUMN)}
    id_at, remote_at = headers.index(ID_COLUMN), headers.index(column)

    lines = text.split("\n")
    before = mdtable.parse(text)
    targets = []
    for position in data_lines:
        cells = mdtable.raw_cells(lines[position])
        if id_at + 1 >= len(cells):
            continue
        if cells[id_at + 1].strip().strip("`") == repo_id:
            targets.append(position)
    if not targets:
        return {"ok": False, "changed": False,
                "reason": "no registry row has Id %r" % repo_id}
    if len(targets) > 1:
        return {"ok": False, "changed": False,
                "reason": "%d rows claim Id %r; two rows disagreeing about one id is a fault to "
                          "report, never to resolve by picking one" % (len(targets), repo_id)}

    position = targets[0]
    cells = mdtable.raw_cells(lines[position])
    if remote_at + 1 >= len(cells):
        return {"ok": False, "changed": False,
                "reason": "the row for %r has only %d cells, so it has no %s cell to write; "
                          "padding it would shift every later cell"
                          % (repo_id, len(cells) - 2, column)}
    if cells[remote_at + 1].strip().strip("`") == value:
        return {"ok": True, "changed": False,
                "reason": "the registry already declares %s %s for %s"
                          % (column, value or "(none)", repo_id)}

    was = cells[remote_at + 1].strip().strip("`")
    cells[remote_at + 1] = (" %s " % mdtable.escape(value)) if value else " "
    candidate = list(lines)
    candidate[position] = "|".join(cells)
    new_text = "\n".join(candidate)

    # 🔴 Parse the result BEFORE it lands. A column shift is this file format's signature
    # silent failure, and the only honest proof that one cell moved is that no other did.
    after = mdtable.parse(new_text)
    drift = _only_cell_changed(before, after, repo_id, id_at, headers, column)
    if drift is not None:
        return {"ok": False, "changed": False,
                "reason": "refusing to write: the edit would have changed more than the %s "
                          "cell (%s). %s is unmodified." % (column, drift, path)}

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(new_text)
    os.replace(tmp, path)
    return {"ok": True, "changed": True,
            "reason": "registry now declares %s %s for %s (was %s)"
                      % (column, value or "(none)", repo_id, was or "(none)")}


def _only_cell_changed(before: List[Dict[str, str]], after: List[Dict[str, str]],
                       repo_id: str, id_at: int,
                       headers: List[str], column: str) -> Optional[str]:
    """None when the two parses differ in nothing but `repo_id`'s `column`; else what moved.

    Compares the PARSED tables rather than the text, because a column shift reads as a
    perfectly valid file and only shows up once the cells are addressed by their headers.
    """
    id_key = headers[id_at]
    if len(before) != len(after):
        return "the row count went from %d to %d" % (len(before), len(after))
    for old_row, new_row in zip(before, after):
        if set(old_row) != set(new_row):
            return "the columns of a row changed"
        for seen in old_row:
            if old_row[seen] == new_row[seen]:
                continue
            if old_row.get(id_key, "").strip().strip("`") == repo_id and seen == column:
                continue
            return "row %r column %r changed from %r to %r" % (
                old_row.get(id_key, "?"), seen, old_row[seen], new_row[seen])
    return None


def duplicate_ids(rows: List[Entry]) -> List[str]:
    """Ids claimed by more than one row, sorted. A blank id is mid-migration, not a collision."""
    counts = {}  # type: Dict[str, int]
    for entry in rows:
        if entry.repo_id:
            counts[entry.repo_id] = counts.get(entry.repo_id, 0) + 1
    return sorted(repo_id for repo_id, count in counts.items() if count > 1)


def colliding_paths(rows: List[Entry]) -> List[Tuple[str, List[str]]]:
    """[(realpath, [row names])] for any path claimed by more than one row, sorted by path.

    Two rows sharing a path is never valid -- it is what makes one repo's files land in
    another's commit scope.
    """
    by_realpath = {}  # type: Dict[str, List[str]]
    for entry in rows:
        real = os.path.realpath(os.path.expanduser(entry.path))
        by_realpath.setdefault(real, []).append(entry.name)
    return sorted((real, names) for real, names in by_realpath.items() if len(names) > 1)


def by_id(rows: List[Entry]) -> Dict[str, Entry]:
    """repo_id -> Entry, skipping blank ids.

    Raises ValueError naming the id if it is duplicated -- silently picking one is the
    "last row wins" failure this schema forbids.
    """
    found = {}  # type: Dict[str, Entry]
    for entry in rows:
        if not entry.repo_id:
            continue
        if entry.repo_id in found:
            raise ValueError("duplicate registry id: %r" % entry.repo_id)
        found[entry.repo_id] = entry
    return found
