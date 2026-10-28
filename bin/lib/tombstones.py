"""Append-only record of retired repo ids — so retirement is terminal.

Deliberately a SECOND store beside the registry's `State` column (see
`paths.tombstones_file`'s docstring for the full reasoning): the two need opposite
properties. A lifecycle disagreement between two peers must become a git merge
CONFLICT, which it does when the state lives on a row that both sides edited — but
split across two append-only files it merges *cleanly* into a desired state that says
one id is both active and retired, which is exactly the failure this module exists to
prevent. And a tombstone must survive the registry ROW BEING DELETED entirely, which
is precisely when a `State` column cannot help: nothing is left to read.

The threat this closes: a retired repo resurrected because someone pasted its old
active row back into `sync/registry.md`. That commit is a perfectly ordinary
descendant of history and would pass every freshness check the registry has. A
tombstone is a fact that outlives the row, so a resurrection attempt can be checked
against it independently of whatever the registry currently says.

Appends never contradict each other, which is why this file is safe to merge by hand
and safe for two machines to write concurrently: if both retire the same id before
either sees the other's commit, the merged file simply holds the row twice, and
`load` resolves that by taking the earliest — the first retirement is the one that
actually took effect; the second was already retiring a repo that was already gone.
"""

import os
from typing import Dict, List, NamedTuple, Optional

from lib import clock, mdtable, paths

HEADER = """# Tombstones — retired repo ids, and why that is final

<!-- HAND-EDITABLE, and append-only. Add a row for a repo you are retiring; never
     delete or reorder an existing row, and never rewrite this file wholesale.

     This exists so a retired repo cannot be resurrected by someone pasting its old
     active row back into sync/registry.md -- that commit would be a perfectly
     ordinary descendant of history and would pass every freshness check. Deleting a
     row here silently RE-PERMITS cloning a repo that was deliberately retired,
     because this file is the only thing that survives the registry row itself being
     deleted. If two peers retire the same id independently before merging, leave
     BOTH rows: appends cannot contradict each other, and the earliest one recorded
     is the one that actually took effect.
-->

| Id | At | Reason | Superseded by |
|----|----|--------|----------------|
"""


class Tombstone(NamedTuple):
    repo_id: str
    at: str             # ISO-8601 local timestamp, as recorded
    reason: str
    superseded_by: str  # a repo_id, or "" when merged into nothing in particular


def load(path: Optional[str] = None) -> Dict[str, Tombstone]:
    """repo_id -> Tombstone. A missing file returns {}, never an error.

    The same id can legitimately appear twice — two peers retired it independently,
    then their commits merged — and that is not a contradiction to raise on: appends
    cannot contradict. Keep the EARLIEST row by `at`, because the first retirement is
    the one that actually took effect; a later duplicate is just an echo of a repo
    that was already gone by the time it was written.
    """
    target = path or paths.tombstones_file()
    if not os.path.exists(target):
        return {}
    with open(target, "r", encoding="utf-8") as handle:
        text = handle.read()

    result = {}  # type: Dict[str, Tombstone]
    for row in mdtable.parse(text):
        repo_id = row.get("id", "").strip()
        if not repo_id:
            continue
        stone = Tombstone(
            repo_id=repo_id,
            at=row.get("at", "").strip(),
            reason=_unescape(row.get("reason", "")),
            superseded_by=row.get("superseded by", "").strip(),
        )
        earlier = result.get(repo_id)
        if earlier is None or clock.sort_key(stone.at) < clock.sort_key(earlier.at):
            result[repo_id] = stone
    return result


def is_retired(repo_id: str, path: Optional[str] = None) -> bool:
    return repo_id in load(path)


def append(repo_id: str, reason: str = "", superseded_by: str = "",
           at: Optional[str] = None, path: Optional[str] = None) -> bool:
    """Append a retirement row. Returns True if written, False if already tombstoned.

    Re-retiring an already-retired id is a no-op, not a duplicate row and not an
    error — the fact "this id is retired" does not change by being asserted twice.
    Creates the file with its header when it does not exist yet, and always writes
    via tmp + os.replace so a crash mid-write cannot leave a truncated file behind.
    """
    if not repo_id:
        raise ValueError("repo_id must not be empty")
    if superseded_by and superseded_by == repo_id:
        raise ValueError("a repo cannot supersede itself: %r" % (repo_id,))

    target = path or paths.tombstones_file()
    if is_retired(repo_id, target):
        return False

    stamp = at or clock.now()
    row = "| %s | %s | %s | %s |" % (
        repo_id, stamp, _escape(reason), superseded_by)

    if os.path.exists(target):
        with open(target, "r", encoding="utf-8") as handle:
            text = handle.read()
    else:
        text = HEADER

    lines = text.rstrip("\n").split("\n")
    lines.append(row)
    lines.append("")

    dirpath = os.path.dirname(target)
    if dirpath and not os.path.isdir(dirpath):
        os.makedirs(dirpath, exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    os.replace(tmp, target)
    return True


def supersede_cycles(store: Dict[str, Tombstone]) -> List[List[str]]:
    """Cycles in the `superseded_by` graph, each reported as the ids that form it.

    Two repos each claiming to be superseded by the other is a contradiction to
    surface, never to follow — following it would loop forever, and picking one side
    arbitrarily would hide the disagreement instead of reporting it. Each cycle is
    reported once regardless of which member's chain reaches it first, and any
    non-cyclic chain leading INTO a cycle is excluded from the reported cycle.
    """
    graph = {repo_id: stone.superseded_by
             for repo_id, stone in store.items() if stone.superseded_by}

    cycles = []  # type: List[List[str]]
    already_reported = set()  # type: set
    for start in graph:
        if start in already_reported:
            continue
        path = []  # type: List[str]
        visited = {}  # type: Dict[str, int]
        node = start
        while True:
            if node not in graph:
                break  # this chain runs off the graph -- not a cycle
            if node in visited:
                cycle = path[visited[node]:]
                cycles.append(cycle)
                already_reported.update(cycle)
                break
            visited[node] = len(path)
            path.append(node)
            node = graph[node]
    return cycles


def _escape(text: str) -> str:
    """Same convention as render.py/brief.py/maildump.py: a table cell is one line."""
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _unescape(text: str) -> str:
    return text.replace("\\|", "|").strip()
