"""Find and follow the satellites of a repo rename — the capabilities a moved checkout
does NOT carry with it just because `git mv` (or `os.rename`) succeeded.

Implements guard 7 of `docs/superpowers/specs/2026-09-30-repo-lifecycle-design.md` §3D:
*"A moved directory does not mean a completed rename. Each satellite is checked
independently: the skill symlinks, the per-project memory directories, the in-repo memory
snapshot directories, and the shortcut rows. Declaring the row converged because the
checkout is in the right place is how a multi-step operation terminates early and leaves
a capability silently missing."*

Four independent kinds, each checked on its own — one kind finding nothing, erroring, or
being unreadable must never suppress another. `find()` is therefore a census, never a
short-circuit: it always visits all four finders and concatenates whatever each returns.

- `skill-symlink` — `~/.claude/skills/*` entries whose resolved target is the renamed
  directory or something inside it (`~/.claude/skills.md` in this repo is the ONLY
  cross-machine record of *which* links should exist; this module only follows links that
  are already live on disk).
- `project-memory` — the one auto-memory directory Claude Code keeps per project, named
  by mangling the project's absolute path (see `project_memory_name`).
- `snapshot-memory` — this repo's own `claude/snapshot/<machine>/memory/<mangled-path>`
  mirror of the same thing, one subtree per machine.
- `shortcut` — rows in Waypoint's `waypoints.md` / `launches.md` whose recorded `Path`
  is the old directory.

🔴 Two traps from `CLAUDE.md` this module exists to respect, not rediscover:

- **Provenance gates repair.** `lib/claudeskills.py`'s `~/dev/.lodestar/skill-links.json`
  records which links THIS machine created. A live link pointing at the renamed repo that
  is not in that set was made by a human (or another tool) and must be reported and left
  alone — never repaired or removed. `find()` marks such a symlink `followable=False`.
- **APFS is case-insensitive.** Two paths differing only by case are the SAME file, never
  two different ones. Every path comparison here folds case before comparing, but never
  folds case when constructing a path to write.

`followable=False` is not a failure mode — it is the correct answer for a snapshot directory
owned by a machine other than this one, a symlink this machine did not create, a shortcut row
scoped to another machine, or a destination that is already occupied. `follow()` REFUSES a
`followable=False` satellite outright rather than attempting it; the only path that mutates
anything is one where `find()` already judged it safe.

🔴 **A SHORTCUT ROW IS FOLLOWABLE NOW, AND THIS DOCSTRING USED TO SAY THE OPPOSITE AS FACT.**
It claimed *"there is no mechanical edit that is safe without a human deciding which row(s) to
touch"*, on the grounds that Waypoint's tables are hand-edited and per-row machine-scoped. The
second half is true and is honoured below; the first half was false and was never measured.
`~/dev/Waypoint/waypoint` is a stdlib-only Python CLI whose subcommands are non-interactive and
each `sync` afterwards, so the mechanical edit exists and is the tool's own supported route:

- a **removal** follows a row by deleting it — `remove <Name>` / `launch remove <Name>`;
- a **rename** follows it by upserting the path — `add <Name> <new-path>` (measured: `cmd_add`
  updates the existing `(NAME, machine)` row in place rather than appending a second one) and
  `launch add <Name> --path <new-path>`.

Lukas ruled on 2026-10-01 that a purge and a rename must follow these rows. Four things that
ruling does **not** license, each enforced below because getting one wrong edits a shortcut
this machine has no standing to touch:

- **`--machine` is passed on EVERY invocation, always, including the universal row.** Measured
  in `cmd_remove`: with no `--machine` the flag defaults to `None` and the row is dropped **for
  every machine**, not for this one. A row scoped to another machine would go with it.
- **A row scoped to another machine is NOT followable.** It names a path that does not and
  should not exist here; that machine removes or repoints it on its own converge, exactly as it
  does its own `snapshot-memory` subtree. A universal (blank) row and a row scoped to this
  machine are both actionable here.
- **A `blocked` row is NOT followable.** Its whole purpose is to record a standing human
  exclusion so that neither a person nor a later `add` re-adds the name; removing the row
  deletes that decision, and `cmd_add` refuses a blocked name outright without `--force`,
  which is a human's override and never ours.
- **A `launches.md` row with a non-blank `Waypoint` cell is NOT followable.** That reference
  takes precedence over the `Path` cell, so the path we matched is inert — and
  `launch add --path` would *clear* the reference (`l.waypoint, l.path = wp_ref, path_val`),
  breaking a working alias to tidy a dead cell.

Two measured facts about running it. The CLI is **not on `PATH`** on this machine, so the argv
names the executable inside `waypoint_root` and a satellite whose executable is absent or
non-executable is reported unfollowed rather than assumed fine — which is also why no test can
reach the real tables: a fixture's `waypoint_root` is a tempdir with no `waypoint` in it, so the
refusal fires before any process starts. And **`waypoint`'s own `expand()` stores
`Path.resolve()`'d paths**, so the row will not necessarily read back as the string we passed —
`/tmp` is `/private/tmp` on macOS and tower's `~/dev` is a symlink. Success is therefore the
command's exit status, never a string comparison against `wants`.

🔴 Following a shortcut row **regenerates the managed block in the shell login file**, because
`add`/`remove`/`launch add`/`launch remove` all end in `cmd_sync`. That is the tool's documented
behaviour and the reason `purge.SCOPE` names it; it is not a side effect to hide.
"""

import os
import re
import shutil
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from lib import claudeskills, machine, mdtable, paths

#: The Waypoint CLI, relative to `waypoint_root`. Named rather than looked up on `PATH`:
#: measured 2026-10-01, `which waypoint` finds nothing on this machine, so a bare "waypoint"
#: argv would fail with a diagnosis that pointed at the wrong thing.
WAYPOINT_EXE = "waypoint"

SKILL_SYMLINK = "skill-symlink"
PROJECT_MEMORY = "project-memory"
SNAPSHOT_MEMORY = "snapshot-memory"
SHORTCUT = "shortcut"
KINDS = (SKILL_SYMLINK, PROJECT_MEMORY, SNAPSHOT_MEMORY, SHORTCUT)

#: Every non-alphanumeric character becomes "-". Measured empirically 2026-09-30 against
#: real ~/.claude/projects/ directory names: "/home/user/dev/Alpha_Project" (an
#: underscore in the dirname) mangles to "-home-user-dev-Alpha-Project" — so both
#: "/" AND "_" fold to "-", and case is preserved. A regex over "not alphanumeric" is the only
#: rule that reproduces this; guessing "just replace slashes" or "slashes and dots" both fail
#: on the underscore case.
_MANGLE_RE = re.compile(r"[^A-Za-z0-9]")


class Satellite(NamedTuple):
    kind: str        # one of KINDS
    where: str        # the satellite's own location/identifier (a path, or "file:Name#row")
    detail: str       # human-readable explanation, including why followable is what it is
    target: str       # what it currently points at / currently is
    wants: str        # what it should point at / become; "" means NOTHING -- see `is_removal`
    followable: bool  # can `follow()`/`remove()` act on this without a human deciding first?
    #: The exact command that follows/removes this satellite, for the kinds repaired by driving
    #: a tool rather than by a filesystem call -- today only `shortcut`. Empty for every other
    #: kind, and empty whenever `followable` is False, so nothing can be executed for a
    #: satellite the census already judged someone else's. The census builds it because the
    #: census is the only thing that knows `waypoint_root`, the row's Name and its machine
    #: scope; `follow`/`remove` then only run it. A test can assert the argv without a process
    #: ever starting, which is the same reason `purge.delete_remote` takes a runner.
    argv: Tuple[str, ...] = ()


def is_removal(sat: Satellite) -> bool:
    """Was this satellite found for a REMOVAL rather than a rename?

    🔴 **The two censuses produce the same type and opposite instructions**, so the marker is
    structural rather than a convention a caller has to remember: `removals()` leaves `wants`
    empty, because there is nowhere for the satellite to go. `find()` always fills it in, since
    a rename names its destination.

    `remove()` refuses anything this returns False for, and `follow()` would have nothing to
    aim at. Without the marker, handing a rename satellite to `remove()` would delete the
    capability that was supposed to be *relinked* — and nothing about the call would look
    wrong.
    """
    return sat.wants == ""


def default_waypoint_root(home: Optional[str] = None) -> str:
    """`~/dev/Waypoint`, in ONE place.

    Two production callers need it — `removals()` resolves it for itself and
    `lifecycle._do_rename` passes it to `find()` — and this repo has already paid for the
    alternative: five private `_escape` copies are why `mdtable`'s reader drifted from its
    writers for months. A second literal here would be the same shape.
    """
    return os.path.join(home or os.path.expanduser("~"), "dev", "Waypoint")


def project_memory_name(repo_path: str) -> str:
    """Claude Code's mangling of an absolute project path into a `~/.claude/projects/` dirname."""
    return _MANGLE_RE.sub("-", os.path.abspath(repo_path))


def find(old_path: str, new_path: str, *, home: Optional[str] = None,
         repo_root: Optional[str] = None, waypoint_root: Optional[str] = None) -> List[Satellite]:
    """Every satellite that still refers to `old_path`, across all four independent kinds.

    `home` defaults to the real `~`. 🔴 **`repo_root` and `waypoint_root` still do NOT default,
    and the production caller passes them now** — `lifecycle._do_rename` supplies
    `paths.root()` and `default_waypoint_root()`. Until 2026-10-01 it passed neither, so
    `snapshot-memory` and `shortcut` were found by **nothing** in the rename path: two of four
    kinds inert, the never-run-subsystem shape CLAUDE.md §7 keeps a tally of.

    🔴 **The ORDER in which that was fixed is load-bearing, and a future session must not undo
    it by "simplifying" either half.** Lukas's ruling was explicitly *wire it up once shortcuts
    no longer block* — because while a shortcut row was `followable=False` by design, finding
    one would have turned renames that converge cleanly today into permanently `partial` ones
    on every machine. Breaking something that works in order to see more is the opposite
    of the point. So shortcut rows became followable first (see the module docstring), and the
    scan was wired only after.

    The parameters are left explicit rather than defaulted: `None` for `waypoint_root` means
    *do not look*, which is what every fixture relies on to stay clear of his live shortcut
    tables, and a default would silently re-point those at the live shortcut tables.

    Each kind is found by its own function and wrapped so an `OSError` in one (a missing or
    unreadable directory) cannot suppress the others; a kind finding nothing legitimately
    contributes nothing, which is not the same as a kind failing to run.
    """
    return _census(old_path, new_path, home=home, repo_root=repo_root,
                   waypoint_root=waypoint_root)


def removals(repo_path: str, *, home: Optional[str] = None,
             repo_root: Optional[str] = None,
             waypoint_root: Optional[str] = None) -> List[Satellite]:
    """Every satellite of a checkout that is being REMOVED. Same traversal, no destination.

    Lukas's ruling, 2026-10-01: a purge **follows and removes** its satellites rather than
    reporting them and leaving them dangling, *"as a rename already does"*. He was shown and
    accepted that this removes an auto-memory directory which can hold notes outliving the
    repository, so nothing here quietly spares it — but every removal is named in the report,
    so the loss is visible rather than silent.

    🔴 **This is the same census, not a second traversal.** `find()` and this both go through
    `_census`, and the four finders decide followability from the same facts; the only
    difference is that a removal has no destination, so `new_abs` is None. That matters because
    followability means something slightly different in each direction and the finders already
    hold the facts that answer both:

    - `skill-symlink` — followable only when **Lodestar created the link**. Identical in both
      directions: provenance gates repair, and removing a link a human made by hand is exactly
      what the provenance set exists to prevent.
    - `project-memory` — a rename can be blocked by an occupied destination; a removal has no
      destination, so it is always followable.
    - `snapshot-memory` — followable only in **this machine's own subtree**, because another
      machine's snapshot directory is that machine's to remove. Occupancy is irrelevant here.
    - `shortcut` — followable when the row is this machine's to edit, in both directions: the
      universal row or a row scoped to this machine, not `blocked`, and (in `launches.md`) not
      resolving through a waypoint reference. A removal deletes the row, a rename upserts its
      path, and both go through the Waypoint CLI. The four refusals are argued in the module
      docstring; each is about standing, not capability.

    Unlike `find()`, this DOES resolve `repo_root` and `waypoint_root`. A purge is the one
    operation with no undo, so a satellite it cannot see is a leftover nobody will ever be told
    about; and an unfollowable shortcut row makes a purge `partial`, which is the honest answer
    — the row really is dangling and really does need a person. Pass them explicitly in tests.
    """
    home = home or os.path.expanduser("~")
    if repo_root is None:
        repo_root = paths.root()
    if waypoint_root is None:
        waypoint_root = default_waypoint_root(home)
    return _census(repo_path, None, home=home, repo_root=repo_root,
                   waypoint_root=waypoint_root)


def _census(old_path: str, new_path: Optional[str], *, home: Optional[str],
            repo_root: Optional[str], waypoint_root: Optional[str]) -> List[Satellite]:
    """The one traversal. `new_path is None` means the checkout is going away, not moving."""
    home = home or os.path.expanduser("~")
    old_abs = os.path.abspath(os.path.expanduser(old_path))
    new_abs = (None if new_path is None
               else os.path.abspath(os.path.expanduser(new_path)))

    finders = (
        lambda: _find_skill_symlinks(old_abs, new_abs, home),
        lambda: _find_project_memory(old_abs, new_abs, home),
        lambda: _find_snapshot_memory(old_abs, new_abs, repo_root),
        lambda: _find_shortcuts(old_abs, new_abs, waypoint_root),
    )
    result = []  # type: List[Satellite]
    for finder in finders:
        try:
            result.extend(finder())
        except OSError:
            continue
    return result


def follow(sat: Satellite, *, dry_run: bool = False,
           runner: Optional[Callable[[Sequence[str]], Tuple[int, str, str]]] = None) -> Dict:
    """Repair one satellite, or refuse. Never attempts a `followable=False` satellite.

    Returns `{"ok", "changed", "reason"}`. `dry_run` evaluates the same preconditions
    `follow` would act on and reports what would happen without mutating anything.

    `runner` takes the argv and returns `(code, out, err)`; it exists so a `shortcut` can be
    driven without a process ever starting, the same reason `purge.delete_remote` takes one.
    It defaults to the production runner, and a satellite built from a fixture's tempdir
    `waypoint_root` is refused before any process starts because no `waypoint` executable is
    there — so forgetting the runner cannot reach the real shortcut tables.
    """
    if not sat.followable:
        return {"ok": False, "changed": False,
                "reason": "refusing: %s at %s is not followable — %s"
                          % (sat.kind, sat.where, sat.detail)}
    if is_removal(sat):
        # The marker cuts both ways: a removal satellite names no destination, so there is
        # nothing to relink it to and "follow" would be a silent no-op reported as success.
        return {"ok": False, "changed": False,
                "reason": "refusing: %s at %s was found for a REMOVAL and names no "
                          "destination; use remove()" % (sat.kind, sat.where)}

    if sat.kind == SKILL_SYMLINK:
        return _follow_symlink(sat, dry_run)
    if sat.kind in (PROJECT_MEMORY, SNAPSHOT_MEMORY):
        return _follow_rename(sat, dry_run)
    if sat.kind == SHORTCUT:
        return _run_shortcut(sat, dry_run, runner)
    # Anything a future kind adds without coming through here. Kept as an explicit refusal
    # rather than an assertion: a kind this function does not understand must not be silently
    # reported as repaired.
    return {"ok": False, "changed": False,
            "reason": "%s satellites have no follow step — %s needs a person"
                      % (sat.kind, sat.where)}


def remove(sat: Satellite, *, dry_run: bool = False,
           runner: Optional[Callable[[Sequence[str]], Tuple[int, str, str]]] = None) -> Dict:
    """Remove one satellite of a purged checkout, or refuse. Returns `{ok, changed, reason}`.

    🔴 **Three refusals stand in front of the only destructive call**, each closing a way this
    could delete something it was never pointed at:

    1. **`followable=False` is refused outright**, never attempted — the same contract
       `follow()` has. A hand-made symlink, another machine's snapshot subtree and a shortcut
       row belonging to another machine (or recording a human's standing exclusion) all land
       here, and all of them belong to someone else.
    2. **A rename satellite is refused**, because `find()` fills in `wants` and `removals()`
       does not. Without this, passing the wrong census's output would delete a capability
       whose whole instruction was to be relinked.
    3. **The thing at `target` must still be what the census said it was.** A `skill-symlink`
       that is no longer a symlink is reported and left: `os.remove` on a real file there would
       be removing something nobody looked at. A memory directory that is itself a symlink is
       refused rather than `rmtree`-d, because `rmtree` through a symlink is how a removal
       escapes the tree it was scoped to.

    `dry_run` evaluates every one of those preconditions and reports what would happen,
    mutating nothing — so a preview can be as pessimistic as the real run, which is the
    property LESSONS.md records a dry run losing when it answers from fewer facts.
    """
    if not sat.followable:
        return {"ok": False, "changed": False,
                "reason": "refusing: %s at %s is not this machine's to remove — %s"
                          % (sat.kind, sat.where, sat.detail)}
    if not is_removal(sat):
        return {"ok": False, "changed": False,
                "reason": "refusing: %s at %s was found for a RENAME (it wants %s), so "
                          "removing it would destroy a capability that should be relinked"
                          % (sat.kind, sat.where, sat.wants)}

    if sat.kind == SKILL_SYMLINK:
        # `where` is the LINK and `target` is what it points at — the link is what goes, and
        # reaching for `target` here would remove a directory inside the purged repo (or, once
        # the checkout is gone, nothing at all while reporting success).
        if not os.path.islink(sat.where):
            return {"ok": False, "changed": False,
                    "reason": "%s is no longer a symlink; refusing to remove whatever is there "
                              "now" % sat.where}
        if dry_run:
            return {"ok": True, "changed": False,
                    "reason": "would remove the skill symlink %s" % sat.where}
        try:
            os.remove(sat.where)
        except OSError as exc:
            return {"ok": False, "changed": False,
                    "reason": "removing the skill symlink %s failed: %s" % (sat.where, exc)}
        return {"ok": True, "changed": True,
                "reason": "removed the skill symlink %s (it pointed into the removed checkout)"
                          % sat.where}

    if sat.kind in (PROJECT_MEMORY, SNAPSHOT_MEMORY):
        if not os.path.lexists(sat.target):
            return {"ok": True, "changed": False,
                    "reason": "%s is already gone" % sat.target}
        if os.path.islink(sat.target) or not os.path.isdir(sat.target):
            return {"ok": False, "changed": False,
                    "reason": "%s is not a plain directory (a symlink or a file), so it is not "
                              "the memory directory the census found; refusing to remove it"
                              % sat.target}
        if dry_run:
            return {"ok": True, "changed": False,
                    "reason": "would remove the %s directory %s" % (sat.kind, sat.target)}
        try:
            shutil.rmtree(sat.target)
        except (OSError, shutil.Error) as exc:
            return {"ok": False, "changed": False,
                    "reason": "removing %s failed: %s" % (sat.target, exc)}
        return {"ok": True, "changed": True,
                "reason": "removed the %s directory %s" % (sat.kind, sat.target)}

    if sat.kind == SHORTCUT:
        return _run_shortcut(sat, dry_run, runner)

    # Anything a future kind adds without coming through here. Kept as an explicit refusal
    # rather than an assertion: a kind this function does not understand must not be reported
    # as removed when nothing happened.
    return {"ok": False, "changed": False,
            "reason": "%s satellites have no removal step — %s needs a person"
                      % (sat.kind, sat.where)}


# --------------------------------------------------------------------------------------
# skill-symlink
# --------------------------------------------------------------------------------------

def _find_skill_symlinks(old_abs: str, new_abs: Optional[str], home: str) -> List[Satellite]:
    skills_dir = os.path.join(home, ".claude", "skills")
    names = sorted(os.listdir(skills_dir))  # raises OSError if missing/unreadable -- caught by find()

    links_path = os.path.join(home, "dev", ".lodestar", "skill-links.json")
    created = claudeskills.created_links(path=links_path)

    result = []
    for name in names:
        link_path = os.path.join(skills_dir, name)
        if not os.path.islink(link_path):
            continue
        try:
            raw_target = os.readlink(link_path)
        except OSError:
            continue
        target_abs = raw_target if os.path.isabs(raw_target) else os.path.join(skills_dir, raw_target)
        target_abs = os.path.abspath(target_abs)

        rel = _relative_if_under(target_abs, old_abs)
        if rel is None:
            continue
        if new_abs is None:
            wants = ""
        else:
            wants = new_abs if rel == "" else os.path.join(new_abs, rel)

        followable = name in created
        subject = "repo being removed" if new_abs is None else "renamed repo"
        if followable:
            detail = ("skill %r is symlinked into the %s and was created by Lodestar"
                      % (name, subject))
        else:
            detail = ("skill %r's link target is inside the %s, but it is not in "
                       "%r — not created by Lodestar, so it is reported and left for a human"
                       % (name, subject, links_path))
        result.append(Satellite(kind=SKILL_SYMLINK, where=link_path, detail=detail,
                                 target=target_abs, wants=wants, followable=followable))
    return result


def _follow_symlink(sat: Satellite, dry_run: bool) -> Dict:
    if not os.path.islink(sat.where):
        return {"ok": False, "changed": False,
                "reason": "%s is no longer a symlink; nothing to relink" % sat.where}
    if dry_run:
        return {"ok": True, "changed": False,
                "reason": "would relink %s -> %s" % (sat.where, sat.wants)}
    try:
        os.remove(sat.where)
        os.symlink(sat.wants, sat.where)
    except OSError as exc:
        return {"ok": False, "changed": False,
                "reason": "relinking %s -> %s failed: %s" % (sat.where, sat.wants, exc)}
    return {"ok": True, "changed": True, "reason": "relinked %s -> %s" % (sat.where, sat.wants)}


# --------------------------------------------------------------------------------------
# project-memory
# --------------------------------------------------------------------------------------

def _find_project_memory(old_abs: str, new_abs: Optional[str], home: str) -> List[Satellite]:
    projects_dir = os.path.join(home, ".claude", "projects")
    dir_path = os.path.join(projects_dir, project_memory_name(old_abs))
    if not os.path.isdir(dir_path):
        return []

    if new_abs is None:
        # A removal has no destination, so the occupancy question does not arise and this is
        # always followable. Lukas was shown that this directory can hold notes outliving the
        # repository and accepted the loss; the detail says so, because the report naming it is
        # the whole difference between an accepted loss and a silent one.
        return [Satellite(kind=PROJECT_MEMORY, where=dir_path,
                          detail="auto-memory directory for the removed checkout; it can hold "
                                 "notes that outlive the repository and they go with it",
                          target=dir_path, wants="", followable=True)]

    wants_path = os.path.join(projects_dir, project_memory_name(new_abs))
    occupied = os.path.lexists(wants_path)
    followable = not occupied
    if followable:
        detail = "auto-memory directory named for the old path; rename it to match the new one"
    else:
        detail = ("destination auto-memory directory %r already exists — needs a human to "
                   "reconcile the two" % wants_path)
    return [Satellite(kind=PROJECT_MEMORY, where=dir_path, detail=detail,
                       target=dir_path, wants=wants_path, followable=followable)]


# --------------------------------------------------------------------------------------
# snapshot-memory
# --------------------------------------------------------------------------------------

def _find_snapshot_memory(old_abs: str, new_abs: Optional[str],
                          repo_root: Optional[str]) -> List[Satellite]:
    if not repo_root:
        return []
    snapshot_root = os.path.join(repo_root, "claude", "snapshot")
    machine_names = sorted(os.listdir(snapshot_root))  # OSError caught by find()

    old_name = project_memory_name(old_abs)
    new_name = None if new_abs is None else project_memory_name(new_abs)
    try:
        this_machine = machine.key()
    except Exception:
        this_machine = None

    result = []
    for machine_name in machine_names:
        memory_dir = os.path.join(snapshot_root, machine_name, "memory")
        dir_path = os.path.join(memory_dir, old_name)
        if not os.path.isdir(dir_path):
            continue

        owned = this_machine is not None and machine_name == this_machine
        if new_abs is None:
            # Ownership is the ONLY question for a removal: another machine's snapshot subtree
            # is that machine's to remove when its own converge runs, exactly as for a rename.
            result.append(Satellite(
                kind=SNAPSHOT_MEMORY, where=dir_path,
                detail=("this machine's own in-repo snapshot copy of the removed checkout's "
                        "memory" if owned else
                        "in-repo snapshot memory lives under machine %r's own subtree — not "
                        "this machine's to remove; %r removes it on its own converge"
                        % (machine_name, machine_name)),
                target=dir_path, wants="", followable=owned))
            continue

        wants_path = os.path.join(memory_dir, new_name)
        occupied = os.path.lexists(wants_path)
        followable = owned and not occupied

        if not owned:
            detail = ("in-repo snapshot memory for the old path lives under machine %r's own "
                       "subtree — not this machine's to rename; %r must run its own rename"
                       % (machine_name, machine_name))
        elif occupied:
            detail = ("destination snapshot memory %r already exists — needs a human to "
                       "reconcile the two" % wants_path)
        else:
            detail = "this machine's own in-repo snapshot copy of the old path's memory"
        result.append(Satellite(kind=SNAPSHOT_MEMORY, where=dir_path, detail=detail,
                                 target=dir_path, wants=wants_path, followable=followable))
    return result


# project-memory and snapshot-memory are both "rename this directory to that path" —
# followed identically once `find()` has already decided it is safe.
def _follow_rename(sat: Satellite, dry_run: bool) -> Dict:
    if not os.path.lexists(sat.target):
        return {"ok": False, "changed": False,
                "reason": "%s no longer exists; nothing to move" % sat.target}
    if os.path.lexists(sat.wants):
        return {"ok": False, "changed": False,
                "reason": "destination %s now exists — refusing to overwrite or merge"
                          % sat.wants}
    if dry_run:
        return {"ok": True, "changed": False,
                "reason": "would move %s -> %s" % (sat.target, sat.wants)}
    try:
        parent = os.path.dirname(sat.wants)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        os.rename(sat.target, sat.wants)
    except OSError as exc:
        return {"ok": False, "changed": False,
                "reason": "moving %s -> %s failed: %s" % (sat.target, sat.wants, exc)}
    return {"ok": True, "changed": True, "reason": "moved %s -> %s" % (sat.target, sat.wants)}


# --------------------------------------------------------------------------------------
# shortcut
# --------------------------------------------------------------------------------------

def _find_shortcuts(old_abs: str, new_abs: Optional[str],
                    waypoint_root: Optional[str]) -> List[Satellite]:
    if not waypoint_root:
        return []
    try:
        this_machine = machine.key()  # type: Optional[str]
    except Exception:
        this_machine = None
    if this_machine:
        this_machine = this_machine.strip().lower()

    result = []
    for filename in ("waypoints.md", "launches.md"):
        file_path = os.path.join(waypoint_root, filename)
        try:
            with open(file_path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            # waypoints.md and launches.md are independent files -- one missing or
            # unreadable must not cost the other its rows, for the same reason one
            # satellite KIND must not suppress another.
            continue
        rows = mdtable.parse(text)
        for index, row in enumerate(rows):
            raw_path = row.get("path", "").strip()
            if not raw_path:
                continue
            row_abs = os.path.abspath(os.path.expanduser(raw_path))
            if row_abs.lower() != old_abs.lower():
                continue

            row_name = row.get("name", "").strip() or "(unnamed)"
            scope = row.get("machine", "").strip().lower()
            where = "%s:%s#%d" % (filename, row_name, index)
            refusal = _shortcut_refusal(filename, row, row_name, scope, this_machine)
            if refusal:
                result.append(Satellite(kind=SHORTCUT, where=where, detail=refusal,
                                        target=row_abs, wants=new_abs or "",
                                        followable=False, argv=()))
                continue

            argv = _shortcut_argv(waypoint_root, filename, row_name, scope, new_abs)
            whose = "the universal row" if not scope else "machine %r's own row" % scope
            if new_abs is None:
                detail = ("%s row %r (%s) points at a checkout being REMOVED, so the shortcut "
                          "would dangle; removing the row with `%s`"
                          % (filename, row_name, whose, " ".join(argv)))
            else:
                detail = ("%s row %r (%s) still points at the old path; repointing it with `%s`"
                          % (filename, row_name, whose, " ".join(argv)))
            result.append(Satellite(kind=SHORTCUT, where=where, detail=detail,
                                    target=row_abs, wants=new_abs or "",
                                    followable=True, argv=argv))
    return result


def _shortcut_refusal(filename: str, row: Dict[str, str], row_name: str, scope: str,
                      this_machine: Optional[str]) -> str:
    """Why this row is not ours to edit, or "" when it is. Each branch is argued in the
    module docstring; all four are about standing, never about capability."""
    if scope and this_machine is None:
        return ("%s row %r is scoped to machine %r and this machine's own identity could not "
                "be read, so there is no way to tell whether the row is ours — reported and "
                "left alone" % (filename, row_name, scope))
    if scope and scope != this_machine:
        return ("%s row %r is scoped to machine %r, not this machine (%r) — that machine edits "
                "it on its own converge, and `--machine %s` from here would rewrite a row for a "
                "path that does not and should not exist on this disk"
                % (filename, row_name, scope, this_machine, scope))
    if row.get("status", "").strip().lower() == "blocked":
        return ("%s row %r is `blocked` — a standing human exclusion recorded so that neither a "
                "person nor a later `add` re-adds the name. Removing the row would delete that "
                "decision and re-adding it needs `--force`, which is a human's override"
                % (filename, row_name))
    if filename == "launches.md" and row.get("waypoint", "").strip():
        return ("launches.md row %r resolves through waypoint %r, which takes precedence over "
                "its Path cell — so the path matched here is inert, and `launch add --path` "
                "would clear the reference and break a working alias"
                % (row_name, row.get("waypoint", "").strip()))
    return ""


def _shortcut_argv(waypoint_root: str, filename: str, row_name: str, scope: str,
                   new_abs: Optional[str]) -> Tuple[str, ...]:
    """The exact Waypoint invocation that follows this row.

    🔴 `--machine` is ALWAYS present, including for the universal row, where it is passed
    empty. Measured in `cmd_remove`: the flag defaults to `None` and a `None` means *all
    machines*, so omitting it on a universal row would also delete every machine-scoped row
    that happens to share the Name. `--machine ""` selects the blank-scoped row and nothing
    else, and `--machine` must precede the subcommand.
    """
    exe = os.path.join(waypoint_root, WAYPOINT_EXE)
    head = (exe, "--machine", scope)
    if filename == "launches.md":
        if new_abs is None:
            return head + ("launch", "remove", row_name)
        return head + ("launch", "add", row_name, "--path", new_abs)
    if new_abs is None:
        return head + ("remove", row_name)
    return head + ("add", row_name, new_abs)


def _run_shortcut(sat: Satellite, dry_run: bool, runner) -> Dict:
    """Drive the Waypoint CLI for one row, or report why it was not driven.

    A refusal always carries the tool's own stderr rather than a fixed sentence — CLAUDE.md §7
    records a whole system's worth of wrong diagnosis caused by a message that named the wrong
    cause. The executable is checked BEFORE the `dry_run` return deliberately: a preview that
    said "would run" while the real run will refuse would be the more optimistic of the two.
    """
    if not sat.argv:
        return {"ok": False, "changed": False,
                "reason": "%s at %s carries no command to run; the row needs a person"
                          % (sat.kind, sat.where)}
    exe = sat.argv[0]
    if not os.path.exists(exe):
        return {"ok": False, "changed": False,
                "reason": "the Waypoint CLI is not present at %s, so %s cannot be followed "
                          "mechanically and the row needs a person" % (exe, sat.where)}
    if not os.access(exe, os.X_OK):
        return {"ok": False, "changed": False,
                "reason": "the Waypoint CLI at %s is not executable (`chmod +x` it), so %s "
                          "cannot be followed mechanically" % (exe, sat.where)}
    if dry_run:
        return {"ok": True, "changed": False,
                "reason": "would run `%s` for %s" % (" ".join(sat.argv), sat.where)}

    call = runner if runner is not None else _run_waypoint
    code, out, err = call(sat.argv)
    if code != 0:
        return {"ok": False, "changed": False,
                "reason": "`%s` failed (exit %d): %s"
                          % (" ".join(sat.argv), code,
                             ((err or "") + (out or "")).strip() or "no output")}
    # Success is the exit status and nothing else. `waypoint`'s own `expand()` stores a
    # `Path.resolve()`'d path, so comparing the row back against `wants` would report failure
    # on every machine whose checkout root is reached through a symlink.
    return {"ok": True, "changed": True,
            "reason": "ran `%s` for %s; `waypoint sync` regenerated the managed shell block"
                      % (" ".join(sat.argv), sat.where)}


def _run_waypoint(argv: Sequence[str]) -> Tuple[int, str, str]:
    """The production runner. Reached only from a real `follow`/`remove` of a shortcut row."""
    import subprocess
    try:
        done = subprocess.run(list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=120)
    except OSError as exc:
        return 127, "", str(exc)
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after 120s: %s" % " ".join(argv)
    return (done.returncode,
            done.stdout.decode("utf-8", "replace"),
            done.stderr.decode("utf-8", "replace"))


# --------------------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------------------

def _relative_if_under(path: str, root: str) -> Optional[str]:
    """"" if `path` == `root`, the relative remainder if `path` is inside `root`, else None.

    Case-folded on both sides -- APFS is case-insensitive, so a symlink target differing from
    `root` only by case is still the SAME directory, not a miss.
    """
    norm_path = os.path.normpath(path)
    norm_root = os.path.normpath(root)
    if norm_path.lower() == norm_root.lower():
        return ""
    prefix = norm_root.rstrip(os.sep) + os.sep
    if norm_path.lower().startswith(prefix.lower()):
        return norm_path[len(prefix):]
    return None
