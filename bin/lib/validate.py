"""Semantic validator over the merged desired state -- run before anything acts.

A clean git merge can produce a CONTRADICTORY desired state. Verified in a fixture
2026-09-30: two peers -- one retiring a repo, one renaming the still-active row --
merged with exit 0 and no conflict markers, leaving one id both active-and-renamed
and retired-and-superseded. Which behaviour a reader then gets depends on parser
order, and a merge's exit code cannot see that at all. So a merged commit is a
TRANSPORT result, never an AUTHORIZATION result: this module is the gate between the
two, run after merge and before anything treats the registry as intent.

Every check here answers one question -- "is the merged state internally
consistent?" -- never "is this state good", which is a human's call recorded
elsewhere (decisions.json and friends). `faults()` never raises on an ordinary bad
row; it collects and reports, because refusing to even describe a contradiction is
worse than describing it.
"""

import os
from typing import Dict, List, NamedTuple, Optional, Sequence, Set

from . import machine, mirrors, registry, skillclone, tombstones


class Fault(NamedTuple):
    kind: str      # stable machine-readable slug, e.g. "duplicate-id"
    subject: str   # the id, path or token at fault
    detail: str    # one human sentence, usable verbatim in a report line


# "missing-id" is a schema-migration fact, not a contradiction in the merged state --
# see `blocking()`'s docstring for why that distinction is the whole point of this set.
#
# The two name-matching kinds are non-blocking for a different reason, recorded here because
# the temptation to promote them will recur: they describe a NAMING CONVENTION the owner ruled
# on 2026-09-30 ("anything lodestar managed should have a matching dir and repo name, period"),
# not an ambiguity that makes the merged state unreadable. A mismatch leaves no reader guessing
# which row owns an id or a path -- it just means a rename is half-applied. There was a live
# instance on the fleet (a renamed repo whose declared remote still named the old name), so making either kind
# blocking would refuse every sweep on every machine over a condition that has been tolerated
# for months. That is the exact shape of the false positive LESSONS.md records from the first
# real run of `_unknown_machine`.
_NON_BLOCKING = frozenset({"missing-id", "name-remote-mismatch", "path-remote-mismatch",
                           "dead-scope", "mirror-name-mismatch"})
_KNOWN_STATES = frozenset({"active", "retired"})


def faults(registry_path: Optional[str] = None,
           machines_path: Optional[str] = None,
           tombstones_path: Optional[str] = None) -> List[Fault]:
    """Every internal contradiction in the merged registry + tombstones + machines state.

    Sorted by (kind, subject) so the report is byte-identical across machines that
    merged the same commit -- this repo's board renderer already requires that
    property of everything it prints, for the same reason: never sort by anything
    time-dependent, because a differently-ordered report on each machine is its own
    small instance of the "which parser order wins" problem this module exists to end.
    """
    rows = registry.entries(registry_path)
    store = tombstones.load(tombstones_path)

    # The DECLARED rows, with no Machine-paths override applied -- `role=""`/`key=""` makes
    # `registry.entries` skip the override lookup entirely (`here` comes out empty) instead of
    # resolving this machine. The name-matching check below reads a Path basename, and an
    # override replaces that basename on exactly one machine: one repo is `~/dev/Name`
    # here and `~/dev/other-name` on `home`. Using
    # the resolved rows would make this module's fault list differ per machine for those two
    # rows, which its own docstring forbids -- and would report a deliberate, declared override
    # as drift. A deliberately overridden path is a human's recorded choice, not a contradiction.
    declared = registry.entries(registry_path, role="", key="")

    found = []  # type: List[Fault]
    found.extend(_duplicate_ids(rows))
    found.extend(_colliding_paths(rows))
    found.extend(_active_but_retired(rows, store))
    found.extend(_unknown_state(rows))
    found.extend(_supersede_cycles(store))
    found.extend(_supersede_unknown(rows, store))
    found.extend(_unknown_machine(rows, machines_path))
    found.extend(_missing_id(rows))
    found.extend(_name_remote_mismatch(declared))
    found.extend(_mirror_faults(declared, machines_path))
    found.extend(dead_scopes())

    return sorted(found, key=lambda fault: (fault.kind, fault.subject))


def blocking(found: Sequence[Fault]) -> List[Fault]:
    """The subset that must STOP action.

    `missing-id` is excluded on purpose: the Id column is mid-migration and every row
    is blank until the migration runs, so treating a blank Id as blocking would refuse
    the entire fleet on day one -- the difference between a check and an outage. Every
    other kind here is a genuine contradiction inside the merged state itself (two
    rows disagreeing about one id or path, a row resurrecting a retired repo, a cycle
    or dangling reference in the tombstones graph, a selector naming nothing) and none
    of those get safer by waiting.
    """
    return [fault for fault in found if fault.kind not in _NON_BLOCKING]


def _duplicate_ids(rows: List[registry.Entry]) -> List[Fault]:
    result = []
    for repo_id in registry.duplicate_ids(rows):
        names = sorted(entry.name for entry in rows if entry.repo_id == repo_id)
        result.append(Fault(
            kind="duplicate-id", subject=repo_id,
            detail="id '%s' is claimed by more than one row: %s"
                   % (repo_id, ", ".join(names)),
        ))
    return result


def _colliding_paths(rows: List[registry.Entry]) -> List[Fault]:
    result = []
    for real, names in registry.colliding_paths(rows):
        result.append(Fault(
            kind="colliding-path", subject=real,
            detail="path '%s' is claimed by more than one row: %s"
                   % (real, ", ".join(sorted(names))),
        ))
    return result


def _active_but_retired(rows: List[registry.Entry],
                         store: Dict[str, tombstones.Tombstone]) -> List[Fault]:
    # Compare on Id, never on Name: a repo can be renamed on the same commit that
    # retires it, and the renamed row is the exact resurrection this check exists to
    # catch. Comparing on Name would miss it -- verified 2026-09-30 by mutating this
    # to `entry.name in store` and watching the fixture's test go red.
    result = []
    for entry in rows:
        if entry.state == "active" and entry.repo_id and entry.repo_id in store:
            stone = store[entry.repo_id]
            result.append(Fault(
                kind="active-but-retired", subject=entry.repo_id,
                detail="id '%s' (row '%s') is marked active but was retired on %s: %s"
                       % (entry.repo_id, entry.name, stone.at,
                          stone.reason or "no reason given"),
            ))
    return result


def _unknown_state(rows: List[registry.Entry]) -> List[Fault]:
    result = []
    for entry in rows:
        if entry.state not in _KNOWN_STATES:
            subject = entry.repo_id or entry.name
            result.append(Fault(
                kind="unknown-state", subject=subject,
                detail="row '%s' has unknown state '%s'" % (entry.name, entry.state),
            ))
    return result


def _supersede_cycles(store: Dict[str, tombstones.Tombstone]) -> List[Fault]:
    result = []
    for cycle in tombstones.supersede_cycles(store):
        # Subject is sorted independently of the graph's own traversal order, so the
        # same cycle reports under the same subject regardless of which member's
        # tombstone row was written first.
        subject = ",".join(sorted(cycle))
        result.append(Fault(
            kind="supersede-cycle", subject=subject,
            detail="supersede cycle: %s" % " -> ".join(list(cycle) + [cycle[0]]),
        ))
    return result


def _supersede_unknown(rows: List[registry.Entry],
                        store: Dict[str, tombstones.Tombstone]) -> List[Fault]:
    known_ids = set(store.keys())
    known_ids.update(entry.repo_id for entry in rows if entry.repo_id)

    result = []
    for repo_id, stone in store.items():
        if stone.superseded_by and stone.superseded_by not in known_ids:
            result.append(Fault(
                kind="supersede-unknown", subject=stone.superseded_by,
                detail="tombstone '%s' is superseded by unknown id '%s'"
                       % (repo_id, stone.superseded_by),
            ))
    return result


def _registry_machine_tokens(machines_path: Optional[str]) -> Set[str]:
    """Every token the REGISTRY's `Machines` column may legitimately use: roles and keys.

    🔴 NOT `machine.known_selector_tokens`, which is keys + LABELS. The two Machines columns
    in this project look identical and are not interchangeable — a trap already recorded in
    LESSONS.md. `sync/registry.md` takes machine ROLES (`desktop`, `laptop`);
    `claude/skills.md` takes keys or labels (`mac`, `home-box`) and rejects role names.

    The authority for what belongs here is `registry.on_machine`, which matches a row against
    `(role, key)` and never against a label. Validating this column with the label vocabulary
    reported every private-host row as a fault on the first real run — a blocking false positive on
    every sweep, caused by reusing the wrong one of two same-named things.
    """
    table = machine.roles(machines_path)
    tokens = {machine.ALL}
    for machine_key, machine_role in table.items():
        tokens.add(machine_key.strip().lower())
        if machine_role:
            tokens.add(machine_role.strip().lower())
    return tokens


def _unknown_machine(rows: List[registry.Entry],
                      machines_path: Optional[str]) -> List[Fault]:
    known = _registry_machine_tokens(machines_path)
    result = []
    seen = set()
    for entry in rows:
        if not entry.machines:
            continue
        for token in entry.machines:
            # `registry._split_machines` lowercases and splits on comma/whitespace, so each
            # token is already normalised. A "+"-joined alternative is not meaningful in this
            # column — on_machine does a plain membership test — so a token containing "+"
            # cannot match anything and is reported as unknown rather than split.
            if token in known or (entry.name, token) in seen:
                continue
            seen.add((entry.name, token))
            result.append(Fault(
                kind="unknown-machine", subject=token,
                detail="row '%s' names unknown machine token '%s'; the registry's Machines "
                       "column takes machine roles or keys, never labels" % (entry.name, token),
            ))
    return result


def dead_scopes(items_path: Optional[str] = None,
                routing_path: Optional[str] = None) -> List[Fault]:
    """Open beacons/sightings whose `Scope` names no `priority/routing.md` row. Such a beacon
    is never a Bearing candidate, and nothing else would say so. Non-blocking: it is a dead
    alert, not a contradiction in the fleet's merged state."""
    from . import paths, priority, routing
    root = paths.root()
    items_path = items_path or os.path.join(root, "priority", "items.md")
    routing_path = routing_path or os.path.join(root, "priority", "routing.md")
    if not os.path.exists(items_path):
        return []
    known = ({p.name for p in routing.load_projects(routing_path)}
             if os.path.exists(routing_path) else set())
    result = []
    for item in priority.load(items_path):
        if item.scope and item.status == priority.OPEN and item.scope not in known:
            result.append(Fault(
                kind="dead-scope", subject=item.scope,
                detail="%s '%s' is scoped to '%s', which no priority/routing.md row names "
                       "-- it can never match" % (item.id, item.title, item.scope)))
    return result


def _missing_id(rows: List[registry.Entry]) -> List[Fault]:
    result = []
    for entry in rows:
        if not entry.repo_id:
            result.append(Fault(
                kind="missing-id", subject=entry.name,
                detail="row '%s' has no Id -- schema mid-migration" % entry.name,
            ))
    return result


def remote_repo_name(url: str) -> str:
    """The repository name at the end of a remote URL, lowercased. "" when unparseable.

    Built on `skillclone.normalize_remote` rather than a fresh parse, so it inherits that
    function's scp-like / ssh:// / https:// / trailing-.git handling and cannot drift from the
    normaliser the rest of the fleet compares remotes with. The consequence worth stating out
    loud: `normalize_remote` LOWERCASES, so every comparison built on this is necessarily
    case-insensitive. That is the right direction here -- GitHub treats `DispatchOpenCode` and
    `dispatchopencode` as one repo, so reporting a case difference as a mismatch would be a
    finding nobody could act on -- but it does mean this check cannot see a case-only drift.

    `normalize_remote("")` reduces to `"/"`, whose last path component is empty, so a blank or
    unparseable URL degrades to "" here rather than raising or inventing a name.
    """
    return skillclone.normalize_remote(url).rsplit("/", 1)[-1]


def _name_remote_mismatch(rows: List[registry.Entry]) -> List[Fault]:
    """The owner's 2026-09-30 invariant: a managed repo's directory name and its remote's
    repository name must be the same string.

    Verbatim: "Anything lodestar managed should have a matching dir and repo name, period.
    Keep them in sync. So rename MUST rename both."

    Two separate comparisons, reported as two kinds, because they fail for different reasons and
    a reader needs to know which:

    - `name-remote-mismatch` -- the row's `Name` (its id-adjacent human handle, and what every
      report line prints) against the repository name in its declared `Remote`.
    - `path-remote-mismatch` -- the basename of the row's declared `Path`, which is the
      *directory* half of the invariant and the half the ruling is actually about.

    Subject is the `Id` where there is one, never the Name: §7's rule that a repo's identity is
    its Id is exactly what a half-applied rename puts under strain, and a fault keyed on the
    Name would move the moment someone fixes the thing being reported.

    A row with no declared `Remote` is skipped rather than reported: there is nothing to compare
    against, and "this row declares no remote" is a different fact belonging to a different
    check. Rows are NOT filtered by `State` -- a retired row with a mismatched remote still
    costs one non-blocking line, and a state filter here would be a second place for a
    lifecycle rule to drift out of step with `_active_but_retired`.
    """
    result = []
    for entry in rows:
        if not entry.remote:
            continue
        repo = remote_repo_name(entry.remote)
        if not repo:
            continue
        subject = entry.repo_id or entry.name
        if entry.name.lower() != repo:
            result.append(Fault(
                kind="name-remote-mismatch", subject=subject,
                detail="row '%s' names a remote whose repository is '%s' -- a managed repo's "
                       "row name and remote repository name must match (owner, 2026-09-30); "
                       "declared remote is %s"
                       % (entry.name, repo, entry.remote),
            ))
        base = os.path.basename(entry.path.rstrip("/"))
        if base.lower() != repo:
            result.append(Fault(
                kind="path-remote-mismatch", subject=subject,
                detail="row '%s' checks out as directory '%s' but its remote repository is "
                       "'%s' -- a managed repo's directory name and remote repository name "
                       "must match (owner, 2026-09-30); declared remote is %s"
                       % (entry.name, base, repo, entry.remote),
            ))
    return result


def _mirror_faults(rows: List[registry.Entry],
                   machines_path: Optional[str]) -> List[Fault]:
    """Design D7 (repo mirrors, 2026-10-19). Blocking: https only, differs from Remote, exactly
    the github.com + the private host pair, someone able to replicate it, no shared Mirror,
    never on a Mail-class row. Non-blocking: the mirror's repository name differs from Name.

    The row-local rules live in `mirrors.row_faults`, which the replicator also runs before it
    touches the network, so the validator and the runtime cannot disagree about a row.
    """
    result = []  # type: List[Fault]
    owners = {}  # type: Dict[str, List[str]]
    for entry in rows:
        if not entry.mirror:
            continue
        subject = entry.repo_id or entry.name
        local = mirrors.row_faults(entry)
        for kind, sentence in local:
            result.append(Fault(kind=kind, subject=subject, detail=sentence))
        owners.setdefault(skillclone.normalize_remote(entry.mirror), []).append(subject)
        if any(kind == "mirror-on-mail-class" for kind, _ in local):
            continue
        if not mirrors.replicators(entry, machines_path):
            result.append(Fault(
                kind="mirror-unreplicable", subject=subject,
                detail="row '%s' declares a Mirror but no machine in its Machines has Send on "
                       "and declares both github.com and the private host in machines.md, "
                       "so nothing can ever replicate it" % subject))
        repo = remote_repo_name(entry.mirror)
        if repo and repo != entry.name.lower():
            result.append(Fault(
                kind="mirror-name-mismatch", subject=subject,
                detail="row '%s' mirrors to a repository named '%s'" % (entry.name, repo)))
    for url, subjects in sorted(owners.items()):
        if len(subjects) > 1:
            result.append(Fault(
                kind="mirror-duplicate", subject=url,
                detail="rows %s all declare Mirror %s; one mirror per repo"
                       % (", ".join(sorted(subjects)), url)))
    return result
