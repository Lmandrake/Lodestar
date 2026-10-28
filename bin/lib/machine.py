"""Who and what is writing to the board.

Every journal event and every `last_touch` is stamped `<role>/<runtime>` — `desktop/cc`,
`laptop/cowork`. That stamp drives the advisory soft-lock, so a wrong answer here is worse
than no answer: it would let two machines auto-commit the same repo believing each was the
only one working it.

Hence two rules:

- **The machine key comes from Waypoint** (`waypoint machine`), not from a second
  hostname-parsing scheme of our own that could drift from it (decision D-B).
- **An unregistered machine raises.** `machines.md` must gain a row; we never guess a role.

`LODESTAR_MACHINE` and `LODESTAR_RUNTIME` override the detected values — that is how the
test suite exercises this without a second computer, and how Cowork can declare itself
explicitly if its sandbox ever looks like Claude Code.
"""

import os
import socket
import subprocess
from typing import Dict, Iterable, List, Optional, Set

from . import mdtable, paths

WAYPOINT = os.path.expanduser("~/dev/Waypoint/waypoint")

_key_cache = None  # type: Optional[str]


class UnknownMachine(Exception):
    """This machine's key has no row in machines.md."""


def key() -> str:
    """This machine's identity key, e.g. 'mt-001'.

    Asks Waypoint first. Falls back to the lowercased hostname if Waypoint is missing or
    fails — the same source Waypoint itself uses, so the fallback agrees with it rather
    than inventing a different answer.
    """
    global _key_cache
    override = os.environ.get("LODESTAR_MACHINE")
    if override:
        return override.strip().lower()
    if _key_cache is not None:
        return _key_cache
    _key_cache = _ask_waypoint() or socket.gethostname().split(".")[0].lower()
    return _key_cache


def _ask_waypoint() -> Optional[str]:
    if not os.path.exists(WAYPOINT):
        return None
    try:
        result = subprocess.run(
            [WAYPOINT, "machine"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    # `waypoint machine` prints the key, then a provenance line beginning with a bullet.
    first = result.stdout.decode("utf-8", "replace").strip().split("\n")[0]
    return first.strip().lower() or None


def roles(machines_path: Optional[str] = None) -> Dict[str, str]:
    """Key -> role, read from machines.md."""
    path = machines_path or paths.machines_file()
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        rows = mdtable.parse(handle.read())
    return {
        row["key"].lower(): row["role"]
        for row in rows
        if row.get("key") and row.get("role")
    }


def role(machines_path: Optional[str] = None) -> str:
    """This machine's role name, e.g. 'desktop'. Raises if unregistered."""
    this = key()
    table = roles(machines_path)
    if this not in table:
        raise UnknownMachine(
            "machine key %r is not listed in %s. Run `%s machine` here and add a row; "
            "Lodestar will not guess a role, because a wrong one corrupts the sync "
            "soft-lock and mislabels history."
            % (this, machines_path or paths.machines_file(), WAYPOINT)
        )
    return table[this]


def row(machines_path: Optional[str] = None) -> Dict[str, str]:
    """This machine's whole machines.md row (lowercased headers), or {} if unregistered."""
    path = machines_path or paths.machines_file()
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        rows = mdtable.parse(handle.read())
    this = key()
    for candidate in rows:
        if candidate.get("key", "").strip().lower() == this:
            return candidate
    return {}


LOCKS_REMOVE = "remove"
LOCKS_FLAG = "flag"


def lock_policy(machines_path: Optional[str] = None) -> str:
    """What the sweep may do with a stale-looking `.git/*.lock` on this machine.

    `remove` (the default, and the Macs' behaviour) lets `gitutil.lock_verdict`'s four-condition
    test authorise a delete. `flag` never deletes, only reports — for a machine where the
    holder check is blind. On WSL the repos live on the Windows drive, and `lsof` cannot see a
    Windows process holding a file there, so "nobody has it open" is not evidence. Declared per
    machine in the `Locks` column of machines.md, not inferred from a hostname.
    """
    value = row(machines_path).get("locks", "").strip().strip("`").lower()
    return LOCKS_FLAG if value == LOCKS_FLAG else LOCKS_REMOVE


def runtime() -> str:
    """'cc' for Claude Code, 'cowork' when declared, else 'shell'.

    This used to return 'cowork' for anything that was not Claude Code, which was wrong in
    the one direction that matters. A plain terminal, a cron entry, and the launchd sweep are
    none of them Cowork, and 'cowork' is the loaded value: it is the runtime forbidden from
    mutating Dialexis, so a false 'cowork' stamp misreads the two-runtime split precisely
    where the split carries a rule.

    There is no known positive signal for Cowork, so it is not inferred. Cowork declares
    itself with LODESTAR_RUNTIME=cowork; absence of evidence reads as 'shell'.
    """
    override = os.environ.get("LODESTAR_RUNTIME")
    if override:
        return override.strip().lower()
    if os.environ.get("CLAUDECODE"):
        return "cc"
    return "shell"


def stamp(machines_path: Optional[str] = None) -> str:
    """The `<role>/<runtime>` string written into last_touch and journal events."""
    return "%s/%s" % (role(machines_path), runtime())


def session_id() -> Optional[str]:
    """The Claude Code session this event came from, when available.

    Not required by BUILD_SPEC.md's event schema; recorded because it is the only way to
    trace a board change back to the conversation that made it.
    """
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or None


# ---------------------------------------------------------------------------
# Labels — the machine KIND, as opposed to the machine's identity.
#
# A key names one computer; a label names a class of them (`mac`, `work`, `laptop`,
# `windows-wsl`, `home`). Claude-config sections and skill rows select by label so that a
# rule true of "every Mac" is written once instead of listing every Mac's key — and so a
# third Mac inherits it without anyone editing the rule. Declared per machine in the
# `Labels` column of machines.md, never inferred from a hostname or `sys.platform`: the WSL
# box reports `linux`, which is not the useful answer.
# ---------------------------------------------------------------------------

ALL = "all"


def _split_tokens(text: str) -> List[str]:
    return [part.strip().strip("`").lower() for part in (text or "").split(",")
            if part.strip().strip("`")]


def labels_table(machines_path: Optional[str] = None) -> Dict[str, List[str]]:
    """Key -> declared labels (lowercased, in declared order). A missing column reads as []."""
    path = machines_path or paths.machines_file()
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        rows = mdtable.parse(handle.read())
    return {
        candidate["key"].strip().lower(): _split_tokens(candidate.get("labels", ""))
        for candidate in rows
        if candidate.get("key", "").strip()
    }


def labels(machine_key: Optional[str] = None,
           machines_path: Optional[str] = None) -> List[str]:
    """The labels of `machine_key` (default: this machine). [] when unregistered or unlabelled.

    Deliberately does not raise for an unregistered key, unlike `role()`: an unknown machine
    selecting *fewer* sections and skills is the safe direction, and the renderer refuses a
    machine with no machine section on its own.
    """
    this = (machine_key or key()).strip().lower()
    return list(labels_table(machines_path).get(this, []))


def selector_matches(spec: str, machine_key: str, machine_labels: Iterable[str]) -> bool:
    """Does a selector like `all`, `mac`, `mt-001, mt-002` or `mac+work` pick this machine?

    - empty or `all` matches every machine;
    - commas separate ALTERNATIVES (any one may match) — which is exactly what the old
      skills.md Machines column meant by `mt-001, mt-002`, so every existing row keeps
      its meaning;
    - `+` joins labels that must ALL hold (`mac+work`: a work Mac, not the home one).

    A token matches the machine's own key or any of its labels, so keys and labels mix freely.
    """
    alternatives = _split_tokens(spec)
    if not alternatives or ALL in alternatives:
        return True
    have = set(label.lower() for label in machine_labels)
    have.add(machine_key.strip().lower())
    for alternative in alternatives:
        needed = [part.strip() for part in alternative.split("+") if part.strip()]
        if needed and all(part in have for part in needed):
            return True
    return False


def known_selector_tokens(machines_path: Optional[str] = None) -> Set[str]:
    """Every token a selector may legitimately use: `all`, each key, each declared label."""
    table = labels_table(machines_path)
    tokens = {ALL}
    for machine_key, machine_labels in table.items():
        tokens.add(machine_key)
        tokens.update(machine_labels)
    return tokens


def unknown_selector_tokens(spec: str, machines_path: Optional[str] = None) -> List[str]:
    """Tokens in `spec` that name no machine and no label. A typo'd label matches nothing,
    silently, forever — so callers report these rather than let a rule quietly vanish."""
    known = known_selector_tokens(machines_path)
    unknown = []
    for alternative in _split_tokens(spec):
        for part in alternative.split("+"):
            part = part.strip()
            if part and part not in known and part not in unknown:
                unknown.append(part)
    return unknown


# ---------------------------------------------------------------------------
# Hosts — which git remote hosts a machine can actually reach.
#
# `tower` has no network path to the private host: not a permissions problem, no
# connectivity at all. Every repo on that host used to flag every sweep forever, with no
# backoff and no give-up, because unreachability was never a fact the sweep could look up —
# only an error it kept re-discovering. Declared per machine in the `Hosts` column of
# machines.md, never inferred from a ping or a git-fetch probe: a probe costs a network round
# trip on every sweep for a fact that changes maybe once a year, and a declared fact is what
# lets the sweep treat the repo as known-quiet instead of retrying into an error log.
# ---------------------------------------------------------------------------


def hosts_table(machines_path: Optional[str] = None) -> Dict[str, List[str]]:
    """Key -> declared hosts (lowercased, in declared order). A missing column reads as []."""
    path = machines_path or paths.machines_file()
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        rows = mdtable.parse(handle.read())
    return {
        candidate["key"].strip().lower(): _split_tokens(candidate.get("hosts", ""))
        for candidate in rows
        if candidate.get("key", "").strip()
    }


def hosts(machine_key: Optional[str] = None,
          machines_path: Optional[str] = None) -> List[str]:
    """The declared hosts of `machine_key` (default: this machine). [] when unregistered or
    undeclared — see `host_reachable`, which is where that empty list becomes "assume yes"."""
    this = (machine_key or key()).strip().lower()
    return list(hosts_table(machines_path).get(this, []))


def host_reachable(host: str, machine_key: Optional[str] = None,
                    machines_path: Optional[str] = None) -> bool:
    """Does this machine declare it can reach `host` (a bare host string, e.g.
    the private host — never a URL; URL parsing lives elsewhere on purpose, so this
    module stays free of it and there is no import cycle)?

    Returns True when the machine declares NO hosts at all — an unregistered machine, or a
    blank `Hosts` cell — because that is the safe direction, exactly as `labels()` already
    argues for an unregistered machine losing config sections rather than gaining wrong ones.
    Here the wrong direction is worse: treating silence as "reaches nothing" would suppress
    every repo on every remote for any machine nobody has gotten around to declaring, which is
    the whole fleet failing quietly instead of the one repo this column exists to quiet.
    """
    declared = hosts(machine_key, machines_path)
    if not declared:
        return True
    return host.strip().lower() in declared
