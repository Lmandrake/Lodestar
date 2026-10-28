"""Materialize ~/.claude/skills from a declarative table.

`~/.claude/skills/` is a set of symlinks into ~/dev repos. A dangling symlink does not
error -- Claude Code simply does not see the skill -- so a capability can vanish on one
machine with no signal anywhere. This module makes the intended set explicit in
claude/skills.md and reconciles reality against it.

**Reporting is not linking.** ~/dev holds more SKILL.md files than are ever globally
linked: some are scoped to their own project (game-modding skills, say), some are drafts
never authored into a real skill at all -- and a draft can carry complete, valid
frontmatter and be otherwise indistinguishable from a deployable one by inspection. So
which gap is deliberate curation and which is a draft is not something layout can answer;
`claude/skills.md`'s `## Not global` section is the one place that answer is written down,
with a mandatory reason. Anything else found outside the table is reported as unlisted and
never linked -- auto-discovery would inject project-scoped skills into every session.
"""

import fnmatch
import json
import os
import re
import time
from typing import Callable, Dict, Iterable, List, NamedTuple, Optional, Set, Tuple

from . import mdtable, paths

# Pruning deletes. Confine it to trees this system is responsible for, so a link the
# user made to somewhere else is reported rather than removed.
PRUNE_ROOTS = (os.path.expanduser("~/dev"), os.path.expanduser("~/checkout"))

ALL = "all"


#: Directories Claude Code itself keeps under ~/.claude/skills. Not skills, not Lodestar's, and
#: never reported or validated as either. `synced` holds account-synced skill buckets
#: (`<uuid>_<uuid>/` plus `.bucket-*` markers); first seen on a WSL machine.
HARNESS_DIRS = frozenset(["synced"])


class Row(NamedTuple):
    skill: str
    target: str      # absolute, ~-expanded
    repo: str        # registry name, or "" when not repo-backed
    machines: str    # "all", or comma-separated machine keys and/or labels


class Action(NamedTuple):
    kind: str        # ok create repoint prune undeclared dangling broken unmanaged clone
    skill: str
    detail: str


def rows(path: Optional[str] = None) -> List[Row]:
    table_path = path or paths.claude_skills_file()
    if not os.path.exists(table_path):
        return []
    with open(table_path, encoding="utf-8") as handle:
        parsed = mdtable.parse(handle.read())
    result = []
    for row in parsed:
        skill = row.get("skill", "").strip()
        target = row.get("target", "").strip()
        if not skill or not target:
            continue
        result.append(Row(
            skill=skill,
            target=os.path.abspath(os.path.expanduser(target)),
            repo=row.get("repo", "").strip(),
            machines=(row.get("machines", "").strip() or ALL),
        ))
    return result


def relocate_targets(table_rows: List[Row], declared: Dict[str, str],
                     here: Dict[str, str]) -> List[Row]:
    """Rewrite each repo-backed target onto this machine's checkout of that repo.

    `skills.md` targets are written against the registry's declared Path, but a `Machine paths`
    override moves the checkout on one machine (e.g. a skill repo checked out under a different directory name on one machine). Without this, that machine reports the skill as absent and asks to clone a repo
    it already has. `declared` is `registry.declared_paths()`, `here` is {name: entry.path}.
    """
    result = []
    for row in table_rows:
        old, new = declared.get(row.repo), here.get(row.repo)
        if old and new:
            old, new = os.path.abspath(old), os.path.abspath(new)
            if old != new and (row.target == old or row.target.startswith(old + os.sep)):
                row = row._replace(target=new + row.target[len(old):])
        result.append(row)
    return result


def applies(row: Row, key: str, labels: Optional[Iterable[str]] = None) -> bool:
    """Does this row's Machines selector pick machine `key`?

    The column accepts machine keys AND machines.md labels (`mac`, `work`, `mac+work`) —
    see `machine.selector_matches`. Every pre-label row (`all`, `mt-001, mt-002`) keeps
    its exact meaning, because a key is still a token that matches itself. `labels` defaults
    to what machines.md declares for `key`; an unregistered key has none, so it gets only
    `all` rows and rows naming it directly — the safe direction.
    """
    from . import machine
    if labels is None:
        labels = machine.labels(key)
    return machine.selector_matches(row.machines, key, labels)


def unknown_selectors(table_rows: List[Row],
                      machines_path: Optional[str] = None) -> List[str]:
    """Faults for Machines cells naming no machine key and no label.

    A typo (`mca`) would otherwise select nothing, quietly, forever — the skill would simply
    never link anywhere and nothing would say why.
    """
    from . import machine
    faults = []
    for row in table_rows:
        unknown = machine.unknown_selector_tokens(row.machines, machines_path)
        if unknown:
            faults.append("%s: Machines names %s — no such machine key or label in "
                          "machines.md" % (row.skill, ", ".join("`%s`" % t for t in unknown)))
    return faults


# Top-level key only: no leading whitespace, so an indented continuation line inside a
# folded scalar is not mistaken for a key.
_TOP_LEVEL_KEY = re.compile(r"^([A-Za-z0-9_-]+)\s*:")

REQUIRED_FIELDS = ("name", "description")


def validate(target: str) -> Optional[str]:
    """Return a fault string if the skill would not load, else None.

    Deliberately does NOT use lib/frontmatter: that is Lodestar's minimal `key: value`
    parser for loop files, and SKILL.md carries real YAML. Several working skills use a
    folded scalar —

        description: >
          first line
          continuation

    — which frontmatter.parse rejects as "malformed line (no ':')". Validating with it
    reported two valid skills as broken while both were loaded and
    working, and a validator that flags healthy skills is worse than none: it builds exactly
    the wall of unread flags that D13's corollary warns about.

    So this checks only what actually determines loadability at this level: a closed
    frontmatter block containing top-level `name` and `description` keys. Whether the YAML
    *value* is well-formed is Claude Code's business, not the sweep's.
    """
    skill_md = os.path.join(target, "SKILL.md")
    if not os.path.exists(skill_md):
        return "no SKILL.md at %s" % target
    with open(skill_md, encoding="utf-8") as handle:
        lines = handle.read().split("\n")

    start = None
    for index, line in enumerate(lines):
        if line.strip() == "---":
            start = index
            break
        if line.strip():
            return "SKILL.md does not open with a --- frontmatter block"
    if start is None:
        return "SKILL.md has no frontmatter block"

    keys, closed = set(), False
    for line in lines[start + 1:]:
        if line.strip() == "---":
            closed = True
            break
        match = _TOP_LEVEL_KEY.match(line)
        if match:
            keys.add(match.group(1).lower())
    if not closed:
        return "SKILL.md frontmatter block is never closed"

    missing = [field for field in REQUIRED_FIELDS if field not in keys]
    if missing:
        return "SKILL.md frontmatter has no %s" % ", ".join(missing)
    return None


def _prunable(link_target: str, prune_roots: Iterable[str]) -> bool:
    # Both sides resolved: the caller passes a realpath'd target, so an unresolved root that is
    # itself a symlink (tower's ~/dev -> /mnt/d/<user>/dev) matched nothing, and every link on
    # that machine read as "outside ~/dev" -- unprunable and misreported as unmanaged.
    resolved = os.path.realpath(link_target)
    return any(resolved.startswith(real + os.sep) or resolved == real
               for real in (os.path.realpath(root) for root in prune_roots))


def plan(table_rows: List[Row], key: str, live_dir: str,
         registered: Set[str], prune_roots: Iterable[str] = PRUNE_ROOTS,
         created: Optional[Set[str]] = None) -> List[Action]:
    """Compute what to do. Pure: reads the filesystem, changes nothing.

    `created` is the set of link names this machine recorded creating (`created_links()`).
    It splits what used to be one case in two, and that split is the whole point:

    * a link Lodestar created whose row is gone -> `prune`, propagating a real removal
    * a link Lodestar never created            -> `undeclared`, reported and never touched

    The old code could not tell these apart, because it used the link's *target location* as
    a proxy for "did we make this". The 2026-09-01 design stated the correct principle -- "a
    link ... not made by this system is not its to remove" -- but a hand-made link into ~/dev
    satisfies the location test too, so seven hand-wired skills were on a deletion path.

    `None` means the empty set, not "unknown, allow anything". Absent provenance forbids
    deletion.
    """
    actions = []  # type: List[Action]
    wanted = {}   # type: Dict[str, Row]
    owned = created if created is not None else set()

    for row in table_rows:
        if not applies(row, key):
            continue
        wanted[row.skill] = row
        link = os.path.join(live_dir, row.skill)

        if not os.path.isdir(row.target):
            if row.repo and row.repo in registered:
                actions.append(Action("clone", row.skill,
                                      "%s not present; clone %s" % (row.target, row.repo)))
            else:
                actions.append(Action("dangling", row.skill,
                                      "target missing and no registered repo: %s"
                                      % row.target))
            continue

        fault = validate(row.target)
        if fault:
            actions.append(Action("broken", row.skill, fault))
            continue

        if os.path.islink(link):
            if os.path.abspath(os.path.realpath(link)) == os.path.realpath(row.target):
                actions.append(Action("ok", row.skill, "linked"))
            else:
                actions.append(Action("repoint", row.skill,
                                      "points at %s" % os.readlink(link)))
        elif os.path.exists(link):
            actions.append(Action("unmanaged", row.skill,
                                  "a real directory occupies the link name"))
        else:
            actions.append(Action("create", row.skill, "link -> %s" % row.target))

    for name in sorted(os.listdir(live_dir) if os.path.isdir(live_dir) else []):
        if name in wanted or name.startswith(".") or name in HARNESS_DIRS:
            continue
        path = os.path.join(live_dir, name)
        if os.path.islink(path):
            if not _prunable(os.path.realpath(path), prune_roots):
                actions.append(Action("unmanaged", name,
                                      "symlink outside %s" % ", ".join(prune_roots)))
            elif name in owned:
                actions.append(Action("prune", name,
                                      "not in the table -> %s" % os.readlink(path)))
            else:
                actions.append(Action("undeclared", name,
                                      "not in the table and not created by Lodestar -> %s; "
                                      "declare it in claude/skills.md or remove it by hand"
                                      % os.readlink(path)))
        else:
            actions.append(Action("unmanaged", name, "real directory, not in the table"))
    return actions


# The most links that may be removed per rolling window. A flat cap, NOT a fraction of the
# table, and that distinction is the whole guard -- see prunable().
PRUNE_FLOOR = 3

#: The window the cap applies over. One hour, chosen against the sweep's own period: at ~127 s
#: it runs ~28 times an hour, so a per-call cap of 3 was really a licence to remove 84 links an
#: hour -- four times the whole global set. An hour is long enough that the cap describes
#: maintenance (dropping a skill removes one link, occasionally two) rather than a rate, and
#: short enough that a machine which legitimately needs a fourth removal heals by itself
#: within one hour instead of needing a human. Shorter windows re-open the drain by allowing
#: several batches per hour; a permanent counter would need clearing by hand, which turns a
#: safety margin into an errand.
PRUNE_WINDOW_SECONDS = 3600


def prune_state_file() -> str:
    """How many links were pruned here, and when. Machine-local, outside the repo.

    Deliberately *not* in `lib/paths.py` yet -- see the report on this change; move it there
    if we want it centralised. The location follows `paths.clone_attempts_file`: how many
    links a machine has removed is per-machine, so committing it would make two machines
    overwrite each other (decision D-D), and anything lock-shaped inside the synced board
    propagates and wedges the other machine.

    Pure cache, and must stay safe to delete: missing or unreadable means "no prunes
    recorded", which permits a batch rather than crashing a sweep.
    """
    return os.path.expanduser("~/dev/.lodestar/skill-prunes.json")


def recent_prunes(now: Optional[float] = None, window: int = PRUNE_WINDOW_SECONDS,
                  path: Optional[str] = None) -> int:
    """Links pruned on this machine inside the window. Unreadable state counts as none.

    Erring toward zero is the safe direction here only because pruning removes symlinks and
    `plan` re-creates them from the table: the cost of under-counting is a window of
    missing capability, while over-counting would block a legitimate removal forever.
    """
    target = path or prune_state_file()
    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    stamps = data.get("pruned_at")
    if not isinstance(stamps, list):
        return 0
    cutoff = (now if now is not None else time.time()) - window
    return len([stamp for stamp in stamps
                if isinstance(stamp, (int, float)) and stamp >= cutoff])


def record_prunes(count: int, now: Optional[float] = None,
                  window: int = PRUNE_WINDOW_SECONDS, path: Optional[str] = None) -> None:
    """Record `count` prunes as having happened now. One timestamp per removed link.

    Never raises: this is bookkeeping for a guard, and a sweep that dies writing its own
    cache is worse than a sweep whose cap forgets an hour. Callers must skip this on a dry
    run -- recording a prune that did not happen spends the budget on nothing.
    """
    if count <= 0:
        return
    moment = now if now is not None else time.time()
    stamps = [stamp for stamp in _load_stamps(path)
              if stamp >= moment - window] + [moment] * count
    target = path or prune_state_file()
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"pruned_at": stamps}, handle, indent=2)
        os.replace(tmp, target)
    except OSError:
        return


def _load_stamps(path: Optional[str]) -> List[float]:
    target = path or prune_state_file()
    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    stamps = data.get("pruned_at") if isinstance(data, dict) else None
    if not isinstance(stamps, list):
        return []
    return [float(stamp) for stamp in stamps if isinstance(stamp, (int, float))]


def links_state_file() -> str:
    """Which links Lodestar itself created here. Machine-local, outside the repo.

    Placement follows `prune_state_file()`: per-machine, so committing it would make two
    machines overwrite each other (D-D), and anything lock-shaped inside the synced board
    propagates and wedges the other machine.

    This is the thing that lets `plan` tell "a declared row was removed" (propagate the
    removal) from "a human's intent was never captured" (destroying it). Safe to delete: a
    missing file means "owns nothing", which prunes nothing.
    """
    return os.path.expanduser("~/dev/.lodestar/skill-links.json")


def created_links(path: Optional[str] = None) -> Set[str]:
    """Link names this machine recorded creating. Unreadable state owns nothing.

    Erring toward the empty set is the safe direction and the opposite of `recent_prunes()`'s:
    there, under-counting permits a removal; here, under-counting forbids one. Both err toward
    not deleting.
    """
    target = path or links_state_file()
    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()
    names = data.get("created")
    if not isinstance(names, list):
        return set()
    return set(name for name in names if isinstance(name, str))


def _write_created(names: Iterable[str], path: Optional[str] = None) -> None:
    """Write the owned set, atomically. Never raises. The only writer of this file.

    Write-temp-then-replace for the same reason `record_prunes` does it: a sweep killed
    mid-write must not leave a truncated file, because a truncated file reads as "owns
    nothing" and silently makes every link unprunable.

    Accepts an empty set on purpose -- `seed_created` needs to create the file even when it
    claims nothing, or every later sweep re-runs the migration.
    """
    target = path or links_state_file()
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"created": sorted(set(str(n) for n in names))}, handle, indent=2)
        os.replace(tmp, target)
    except OSError:
        return


def record_created(names: Iterable[str], path: Optional[str] = None) -> None:
    """Add `names` to the owned set. Merges, never replaces."""
    incoming = set(str(name) for name in names)
    if not incoming:
        return
    _write_created(created_links(path) | incoming, path)


def seed_created(table_rows: List[Row], key: str, live_dir: str,
                 path: Optional[str] = None) -> int:
    """One-time migration: claim `declared rows ∩ live links`. Returns how many were claimed.

    The live links predate this file. A link whose name matches a row declared for this
    machine can only have come from this system, so claiming it is safe; anything else starts
    un-owned and is therefore never pruned.

    A no-op once the file exists. Re-seeding would re-claim links a human had deliberately
    removed from the set, which would quietly restore the destructive behaviour this replaces.
    """
    target = path or links_state_file()
    if os.path.exists(target):
        return 0
    declared = set(row.skill for row in table_rows if applies(row, key))
    live = set(name for name in (os.listdir(live_dir) if os.path.isdir(live_dir) else [])
               if os.path.islink(os.path.join(live_dir, name)))
    claimed = declared & live
    # Written even when `claimed` is empty: the file's existence is what marks the migration
    # done. Without it every later sweep re-seeds, and a link created in the meantime gets
    # retroactively claimed by a migration rather than by its own create.
    _write_created(claimed, target)
    return len(claimed)


def remove_created(names: Iterable[str], path: Optional[str] = None) -> None:
    """Drop `names` from the owned set -- called after a successful prune."""
    going = set(str(name) for name in names)
    if not going:
        return
    _write_created(created_links(path) - going, path)


def journal_prune(skill: str, target: str) -> None:
    """Append a durable, committed record that a skill link was removed. Never raises.

    SYNC.md cannot serve this purpose: it is gitignored and fully regenerated every sweep, so
    the name of a removed skill survived about 127 seconds and never reached the other
    machine. `record_prunes()` keeps only timestamps, so the durable record was an anonymous
    integer. The 2026-09-01 design requires "report every removal"; this is what actually
    satisfies it.

    `loop="lodestar"` because `loops.derive()` does `setdefault(loop_id, ...)` -- an event
    under an unknown loop id would fabricate a board row on `board.py --rebuild`.
    `loops/lodestar.md` exists. Do not invent an id here.
    """
    try:
        from . import journal
        journal.append(loop="lodestar", event="skill-prune",
                       note="link removed: not in claude/skills.md and Lodestar-created",
                       extra={"skill": skill, "target": target})
    except Exception:      # noqa: BLE001 -- bookkeeping must not break the sweep
        return


def prunable(actions: List[Action], table_rows: List[Row], key: str,
             limit: Optional[int] = None,
             recent_prunes: int = 0) -> Tuple[List[Action], Optional[str]]:
    """Return (prune actions that may proceed, refusal reason or None).

    The bulk-delete guard. `plan` derives staleness solely from the table, so if the table is
    empty, truncated, corrupted, or read mid-write, every live link is *legitimately* stale
    and pruning would take out the whole global skill set -- unattended, with no prompt
    possible, and with the table itself as the thing that just failed.

    Two refusals:

    1. No rows applying to THIS machine -> refuse. "The table says nothing" means
       seed-not-yet-run or read failure. It never means "delete everything".
    2. This batch plus `recent_prunes` over `limit` (default PRUNE_FLOOR) -> refuse.

    **Why the cap counts history.** It used to apply per call, and the only caller is a sweep
    running every ~127 s with no memory of earlier passes -- so the cap bounded the *rate*,
    not the total, and never refused anything a table shrinking a few rows at a sweep asked
    for. 21 links went to 3 in six sweeps (~13 minutes) with no refusal firing; only rule 1
    saved the last three. `recent_prunes` is the count the caller reads back from
    machine-local state (`recent_prunes()`), so the cap now means "3 links an hour on this
    machine" -- which is what "a flat cap does not move" was always supposed to buy. It stays
    a parameter rather than a read so this function keeps touching no filesystem: it must be
    trustworthy about state when state is the thing that just failed.

    **Why a flat cap rather than a fraction of the table.** A fraction is partially
    self-authorizing, because the denominator is the surviving rows. Scaled at
    `max(3, applicable // 2)`, a table truncated from 21 rows to 15 permits 7 removals
    against the 6 links that just became "stale" -- so the six vanish silently. That is the
    *likelier* corruption (a mid-write read beats a zeroed file), and it is exactly the case
    the fraction was weakest against. A flat cap does not move when the table shrinks.

    Three is also closer to what maintenance actually looks like: dropping a skill removes one
    link, occasionally two. A batch of ten was never routine, it was just under half of
    twenty-one. Anything larger is still *reported*, and a human can authorize it by raising
    `limit` from the interactive command -- which is the right division of trust, since the
    sweep cannot ask and a person can look.

    Refusal is all-or-nothing: a half-executed bulk delete leaves a state nobody can reason
    about afterwards, which is worse than not acting. Pure -- touches no filesystem, so it can
    be trusted not to re-read the thing it is guarding against.
    """
    prunes = [action for action in actions if action.kind == "prune"]
    if not prunes:
        return ([], None)

    applicable = [row for row in table_rows if applies(row, key)]
    if not applicable:
        return ([], "refusing to prune %d link(s): the table declares 0 rows for machine "
                    "'%s' (of %d total), so an empty or unreadable table cannot be "
                    "distinguished from an intent to remove everything"
                    % (len(prunes), key, len(table_rows)))

    ceiling = PRUNE_FLOOR if limit is None else limit
    if len(prunes) + max(0, recent_prunes) > ceiling:
        if recent_prunes > 0:
            return ([], "refusing to prune %d link(s): %d already pruned on this machine in "
                        "the last %d min, which puts the total over the cap of %d per %d min. "
                        "Links drained a few at a time are the same corrupted-table symptom "
                        "as one large batch. They stay listed and the cap clears on its own; "
                        "to act now, authorise explicitly with: claudeconf.py skills --apply "
                        "--prune" % (len(prunes), recent_prunes, PRUNE_WINDOW_SECONDS // 60,
                                     ceiling, PRUNE_WINDOW_SECONDS // 60))
        return ([], "refusing to prune %d link(s) at once: over the cap of %d. A batch this "
                    "size is a symptom of a truncated or corrupted table, not maintenance. "
                    "Review them, then authorise explicitly with: claudeconf.py skills "
                    "--apply --prune" % (len(prunes), ceiling))
    return (prunes, None)


class ApplyResult(list):
    """`apply`'s lines, with per-action failures hung off the side as `.faults`.

    A `list` subclass on purpose. `claudeconf.skills_state` does `lines.extend(apply(...))`
    and tests compare the return to `[]`, so behaving in every respect like the plain list
    this used to be keeps both callers source-compatible while `.faults` carries what used to
    be an uncaught exception. A tuple return would have been cleaner and would have broken
    them both silently at the `extend`.
    """

    def __init__(self, lines: Optional[Iterable[str]] = None,
                 faults: Optional[Iterable[str]] = None) -> None:
        list.__init__(self, lines or [])
        self.faults = list(faults or [])


def apply(actions: List[Action], live_dir: str, rows_by_skill: Dict[str, Row],
          allow_prune: bool = False, dry_run: bool = False,
          on_prune: Optional[Callable[[str, str], None]] = None,
          on_create: Optional[Callable[[str], None]] = None) -> ApplyResult:
    """Perform the safe actions. Returns lines describing what happened, plus `.faults`.

    Cloning is NOT done here -- it needs the network, backoff, and concurrency control,
    so sync.py owns it. This function only creates, repoints, and (opt-in) prunes.

    Two failure modes are handled here rather than raised, because the caller of record is an
    unattended sweep whose `except Exception` turns any raise into a permanent flag that heals
    nothing:

    * **`live_dir` absent.** Nothing else in bin/ creates `~/.claude/skills`, so on a freshly
      cloned machine -- D18's whole scenario -- the first `create` raised FileNotFoundError,
      sync.py caught it, and the sweep reported the same error every ~127 s while linking
      nothing. Worse, it was invisible: `check.py` calls `skills_state(dry_run=True)`, which
      never reaches `os.symlink`, so check reported clean while the sweep failed. Created here
      only when a create/repoint is actually pending, and never under `dry_run`.
    * **One action failing.** The loop had no per-action guard, so a single collision or
      permission error aborted every remaining create: one bad row cost the whole skill set.
      Each action is now isolated and its failure becomes a fault, which is the same
      report-and-continue shape `plan` already uses for `broken` and `dangling`.

    `on_create`, called after a successful, non-dry-run `create` or `repoint`, is what feeds
    provenance (`record_created`). Without it nothing writes the owned set on the path that
    actually creates links -- only `seed_created`'s one-time migration ever did, so on a
    machine where `~/.claude/skills` did not exist yet every link `apply` went on to create
    was created unrecorded, and `plan`'s prune split (see its docstring) had nothing to work
    with forever. Guarded exactly like `on_prune`: only when set and not `dry_run`, and any
    failure is swallowed -- the link already exists by then, so a bookkeeping error here must
    not read back as a failed create.
    """
    done, faults = [], []  # type: (List[str], List[str])

    pending = [a for a in actions if a.kind in ("create", "repoint")]
    if pending and not dry_run and not os.path.isdir(live_dir):
        try:
            os.makedirs(live_dir, exist_ok=True)
        except OSError as error:
            # Nothing below can succeed without it, and every action would report the same
            # cause; say it once.
            return ApplyResult([], ["cannot create %s: %s" % (live_dir, error)])

    for action in actions:
        link = os.path.join(live_dir, action.skill)
        try:
            if action.kind == "create":
                if not dry_run:
                    os.symlink(rows_by_skill[action.skill].target, link)
                done.append("linked %s" % action.skill)
                if on_create is not None and not dry_run:
                    try:
                        on_create(action.skill)
                    except Exception:      # noqa: BLE001
                        # The link already exists; a failed record must not read back as a
                        # failed create.
                        pass
            elif action.kind == "repoint":
                if not dry_run:
                    os.unlink(link)
                    os.symlink(rows_by_skill[action.skill].target, link)
                done.append("repointed %s" % action.skill)
                if on_create is not None and not dry_run:
                    try:
                        on_create(action.skill)
                    except Exception:      # noqa: BLE001
                        pass
            elif action.kind == "prune" and allow_prune:
                # Read the target BEFORE unlinking: the action carries a name, and once the
                # symlink is gone there is nothing left to resolve.
                removed_target = ""
                try:
                    removed_target = os.path.realpath(os.readlink(link))
                except OSError:
                    pass
                if not dry_run:
                    os.unlink(link)
                done.append("pruned %s" % action.skill)
                if on_prune is not None and not dry_run:
                    try:
                        on_prune(action.skill, removed_target)
                    except Exception:      # noqa: BLE001
                        # The link is already gone; a failed record must not become a fault
                        # that makes the caller think the removal failed.
                        pass
        except Exception as error:      # noqa: BLE001 -- see below
            # Deliberately broad. The point is that no single row can abort the others, and
            # the failures are not enumerable in advance: OSError for a collision or a
            # permission denial, KeyError when plan and apply are a re-plan apart. A narrow
            # catch here reintroduces exactly the abort this guard exists to prevent.
            faults.append("%s %s failed: %s" % (action.kind, action.skill, error))
    return ApplyResult(done, faults)


_NOT_GLOBAL_HEADING = re.compile(r"^##\s+Not global\s*$", re.IGNORECASE)
_NOT_GLOBAL_ENTRY = re.compile(r"^-\s+`(?P<pattern>[^`]+)`\s*(?:—|--|-)?\s*(?P<reason>.*)$")


def not_global(path: Optional[str] = None) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Parse the `## Not global` section. Returns (entries, faults).

    Each entry is (glob pattern relative to ~/dev, prose reason). **A reason is mandatory**,
    and an entry without one is a fault rather than a silent suppression -- that is the only
    thing stopping this list becoming a dumping ground for skills nobody decided about. It
    mirrors registry.md's "Not participating" rows, which all carry one.

    Patterns are fnmatch globs, not regexes: a mis-anchored regex silently suppressing a real
    skill is exactly the failure this section exists to prevent.
    """
    table_path = path or paths.claude_skills_file()
    if not os.path.exists(table_path):
        return ([], [])
    with open(table_path, encoding="utf-8") as handle:
        lines = handle.read().split("\n")

    inside = False
    entries, faults = [], []      # type: (List[Tuple[str, str]], List[str])
    pattern, reason_parts = None, []      # type: (Optional[str], List[str])

    def flush():
        if pattern is None:
            return
        reason = " ".join(part.strip() for part in reason_parts if part.strip()).strip()
        if reason:
            entries.append((pattern, reason))
        else:
            faults.append("Not global entry `%s` has no reason. Every suppression must say "
                          "why, or the list becomes a place skills go to be forgotten."
                          % pattern)

    for line in lines:
        if _NOT_GLOBAL_HEADING.match(line):
            inside = True
            continue
        if not inside:
            continue
        if line.startswith("## "):        # next section ends ours
            break
        match = _NOT_GLOBAL_ENTRY.match(line)
        if match:
            flush()
            pattern = match.group("pattern").strip()
            reason_parts = [match.group("reason")]
        elif pattern is not None and line.strip() and line.startswith((" ", "\t")):
            reason_parts.append(line)     # indented continuation of the current reason
        elif line.lstrip().startswith("-"):
            # A bullet that failed to parse -- most commonly a pattern written without the
            # required backticks, e.g. `- RimMaster/skills/* — reason`. Silently skipping it
            # suppresses nothing while sitting in the file reading as protection it does not
            # provide, which is exactly the failure this section exists to prevent. So it is
            # a fault naming the line, not a line the loop quietly drops.
            flush()
            pattern = None
            faults.append(
                "Not global entry could not be parsed -- the pattern must be wrapped in "
                "backticks (e.g. `RimMaster/skills/*`): %r" % line.strip()
            )
    flush()
    return (entries, faults)


def scan_unlisted(dev_root: str, table_rows: List[Row],
                  ignored: Iterable[str] = ()) -> Tuple[List[str], List[str], int]:
    """One walk. Returns (unlisted, patterns that matched nothing, suppressed count).

    Reporting the unused patterns matters: a typo'd pattern suppresses nothing and is
    otherwise indistinguishable from a working one, so it would sit in the file reading as
    protection it does not provide -- which is how VizSkillLab's .gitignore hardening
    achieved nothing. Reported as a line, not a fault: a repo can be legitimately absent
    from a machine.
    """
    listed = set(os.path.realpath(row.target) for row in table_rows)
    patterns = list(ignored)
    used = set()      # type: Set[str]
    found, suppressed = [], 0

    for base, dirs, files in os.walk(dev_root):
        dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", "plugins")]
        if "SKILL.md" not in files or os.path.realpath(base) in listed:
            continue
        relative = os.path.relpath(base, dev_root)
        hit = None
        for pattern in patterns:
            if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(relative + "/", pattern):
                hit = pattern
                break
        if hit:
            used.add(hit)
            suppressed += 1
        else:
            found.append(base)
    return (sorted(found), [p for p in patterns if p not in used], suppressed)


def unlinked(dev_root: str, table_rows: List[Row]) -> List[str]:
    """SKILL.md dirs under dev_root that no table row points at. Reported, never linked.

    Kept at its original signature so existing callers and tests are unaffected; new work
    should call `scan_unlisted`, which also reports suppression.
    """
    return scan_unlisted(dev_root, table_rows)[0]
