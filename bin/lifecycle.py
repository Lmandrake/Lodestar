#!/usr/bin/env python3
"""Repo lifecycle: propose a rename/rehome/retirement, approve it once, converge everywhere.

This is the production caller for `lib/plans.py`, `lib/lifelock.py`, `lib/transitions.py`,
`lib/relocate.py` and `lib/satellites.py`. That matters more than it sounds: this repo's
signature defect is a complete, well-tested subsystem wired to nothing — `lib/enroll.py`
shipped with 13 passing tests, zero callers and not one stamped checkout, and SWEEP's group
engine shipped with ~40 passing tests and a production path that never dispatched through it.
A lifecycle converger with no caller would be the third. `bin/sync.py`'s Lifecycle section is
the unattended caller; this file is the operator's.

    lifecycle.py propose --kind rename --id opencodecli \
        --old ~/dev/OpenCodeCLI --new ~/dev/DispatchOpenCode --machines all
    lifecycle.py approve rename-opencodecli-20260930      # the origin gate — asks once
    lifecycle.py status
    lifecycle.py converge --dry-run                       # then without it

**Authorization happens once, on the originating machine; every other machine obeys.**
(Lukas, 2026-09-30: *"It makes the costs clear on the originating machine. All other machines
simply obey and do not request permission at all."*) That is what makes tower evictable
with nobody sitting at tower. So `approve` prints the **fleet-wide** consequence, including
what machines that are currently offline will do when they wake, and `converge` never prompts.

**The origin prompt is therefore the whole safety argument for destructive work**, which is
why it enumerates per machine rather than printing a single summary line.

Order of the guards is load-bearing and follows §3D:

1. **Freshness before any repo action.** A machine offline through a decision wakes holding
   yesterday's scope and yesterday's host — and *pushes*. That is a scope violation, not a
   flap. This gate cannot make a machine current; it only stops one that knows it is not.
2. **Semantic validation before action.** A merged commit is a transport result, not an
   authorization result.
3. **The machine-local lock**, keyed on id, held outside the movable directory.
4. **Transition record written before mutating**, so a crash is recoverable by inspection.
5. **Relocation preconditions**, all of them, reported together.
6. **Satellites checked independently** — a moved directory is not a completed rename.

🔴 **Placement and eviction are evaluated BEFORE the "this machine does not participate, skip"
branch.** Otherwise the one machine that needs to evict is the one machine that never looks —
which is exactly how work material could reach a home machine and stay there.

🔴 **Purge SHIPS DISARMED, and the arming flag is the rest of its safety argument.** §4 says
a purge needs *"an explicitly authorised destructive procedure with a defined scope and
verification"*, because both other patterns retain material on purpose. So `purge` is a kind
now (Lukas, 2026-10-01: remove the checkout on every machine, tombstone the id, and delete
the hosted repository), and:

- **`converge` alone never removes anything for a purge plan.** It reports `disarmed`,
  prints the whole act, and exits non-zero. `converge --arm-purge` is the only thing that
  executes it, and `sync.lifecycle_step` does not pass it — so the unattended hourly sweep
  cannot purge, by construction.
- **`converge --dry-run` previews the run it would actually be**: `would-be-disarmed` without
  `--arm-purge`, `would-converge` / `would-be-partial` with it. A preview that was more
  optimistic than the next real run would be worse than no preview.
- **The scope is narrower than the word.** `lib/purge.py` states it on every report: this
  machine's working area — the checkout plus the satellites of it this machine created or
  owns, which since 2026-10-01 includes the Waypoint shortcut rows this machine owns — and
  the hosted repository. Backups, snapshots, search indexes, other clones, and shortcut rows
  belonging to another machine or marked `blocked` are outside it and are not touched.
  Nothing here claims erasure.
- **A purge writes its own desired state and follows its own satellites** (Lukas,
  2026-10-01). The registry row is set to `State: retired` FIRST, before the tombstone and
  before any removal, and is never deleted; the skill symlinks, auto-memory directory and
  this machine's snapshot-memory copy are removed after the checkout is gone, each named in
  the report. `_purge_declaration_step` and `_purge_satellite_step` carry the arguments.
- A `purge` **disposition** on any other kind is still refused, and still says why:
  *"erasure is not obtainable by renaming a rescue directory."*
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib import (clock, completions, freshness, journal, lifelock, machine,  # noqa: E402
                 paths, plans, purge, registry, relocate, satellites, tombstones,
                 transitions, validate)

#: The loop every lifecycle event is filed under, so the board shows this work.
LOOP = "repo-lifecycle"

#: `_converge_one`'s answer when a completion record retires the plan on this machine. A
#: SUCCESS status, so it never contributes to the non-zero exit — the whole point is that a
#: finished plan stops flagging. Kept distinct from `converged` so the report can say which
#: run did the work, and so nothing re-records a completion that is already on disk.
COMPLETE = "complete"

#: Kinds whose plan `new` cell is NOT a filesystem path, so `tilde()` must never touch it.
#: `rehome` carries a git URL and `purge` carries a hosted-repository coordinate. CLAUDE.md
#: §7 records what `tilde()` did to the first of those: `os.path.abspath` resolved the URL
#: against the current working directory, and an `https://` target collapsed to `https:/` —
#: unrecoverable even by stripping the prefix. A list, rather than a second `== "rehome"`
#: test, so the third kind that needs it cannot be added without seeing this.
NON_PATH_NEW_KINDS = ("rehome", "purge")

#: Kinds evaluated BEFORE the "this machine does not participate, skip the row" branch —
#: §3E. Otherwise the one machine that needs to remove material is the one machine that never
#: looks, which is exactly how work material could reach a home machine and stay there.
EVICTING_KINDS = ("retire", "quarantine", "purge")

#: Statuses that make `converge` exit non-zero, so `sync.lifecycle_step` reports FLAGGED
#: rather than SYNCED. `partial` is here because a capability may be silently missing
#: (LESSONS.md has the measured case); `disarmed` is here because an approved purge that has
#: not executed is an open obligation. A preview (`would-*`) is NOT here — a dry run reporting
#: a fault would make the sweep flag on a run that asked nothing of the world.
NONZERO_STATUSES = ("stop", "refused", "partial", "disarmed")

#: Where a quarantined checkout is held. Outside the repo and outside the managed working
#: area, so the sweep never sees it and it cannot be committed anywhere.
HOLDING_ROOT = os.path.expanduser("~/dev/.lodestar/quarantine")


# --- observations -----------------------------------------------------------------------

def tilde(path):
    """`/home/user/dev/X` -> `~/dev/X`. How a path must be STORED in a shared plan.

    🔴 A plan travels to machines with different home directories, so an expanded absolute
    path is a Desktop-specific fact masquerading as fleet-wide intent. tower is a WSL2 box
    whose checkouts live under `/mnt/...`, an entirely disjoint namespace from the Macs'
    `/Users/<name>/...` — so a plan carrying `/Users/<name>/dev/OpenCodeCLI` observes as
    `NEITHER` there, returns `consult-holding`, and refuses for ever. Safe, but permanently
    half-applied, which is the exact condition this whole loop exists to end.

    `sync/registry.md`'s own **Machine paths** table already settled this: its `home` row reads
    `~/dev/AlphaGame`, not an expanded path. Same convention here, and `registry._tilde` is the
    same helper one layer down.
    """
    if not path:
        return path
    home = os.path.expanduser("~")
    absolute = os.path.abspath(os.path.expanduser(path))
    if absolute == home:
        return "~"
    if absolute.startswith(home + os.sep):
        return "~" + absolute[len(home):]
    return absolute


def local(path, dev_root=None):
    """The stored (`~`-relative) form resolved for THIS machine. Expand at use, never at store.

    The dev root ITSELF is resolved through a symlink, and nothing below it is. On tower
    `~/dev` is a declared symlink to `/mnt/d/<user>/dev`, so every expanded path passed through
    it and relocate's `symlinked-destination` guard refused every rename there for ever
    (RimMandrake, 2026-09-30). That guard exists for a link *inside* the tree that would
    redirect a move; the machine's own root is not that, so it is canonicalised here and the
    guard keeps its full force below it.
    """
    if not path:
        return path
    expanded = os.path.expanduser(path)
    root = dev_root or os.path.expanduser("~/dev")
    if os.path.islink(root) and (expanded == root or expanded.startswith(root + os.sep)):
        return os.path.realpath(root) + expanded[len(root):]
    return expanded


def board_freshness(now=None):
    """Is this machine's view of DESIRED STATE current? Gate on the board, not the target.

    Intent lives in this repo, so freshness is a property of this checkout — a repo being
    moved may have no upstream at all, and gating on it would ask the wrong question.

    `last_fetch` is the mtime of `.git/FETCH_HEAD`, which is a real record of when the remote
    was last contacted. A missing FETCH_HEAD reads as `None`, which `freshness.gate` refuses
    as *unknown* rather than treating as fresh.

    🔴 `now` must be a FLOAT, and using `clock.epoch()` here was a catch-22. `epoch()` returns
    `int(...)` — truncated — while `os.path.getmtime` keeps its fraction, so a FETCH_HEAD
    written during the current second reads as up to 1s IN THE FUTURE and `gate` refuses it as
    clock skew. That fires precisely when the operator does the safest possible thing: the
    refusal message says "Run `git pull --rebase` here first", and doing so immediately before
    re-running `converge` is what trips it. Measured 2026-09-30 — a pull, then a converge,
    refused with "last_fetch is 0s in the future".

    `sync.py`'s call already passes `time.time()`; matching it removes the manufactured future
    without weakening the skew check, which still refuses any genuinely negative age.
    """
    root = paths.root()
    fetch_head = os.path.join(root, ".git", "FETCH_HEAD")
    last_fetch = None
    try:
        last_fetch = os.path.getmtime(fetch_head)
    except OSError:
        last_fetch = None
    behind, diverged = freshness.observed(root)
    return freshness.gate(now if now is not None else time.time(),
                          last_fetch, behind, diverged)


def fleet():
    """(key, role) for every machine the board knows about, sorted."""
    return sorted(machine.roles().items())


def managed_paths_here():
    """Every managed checkout path on this machine, for the nesting precondition."""
    found = []
    for entry in registry.entries():
        if entry.path:
            found.append(os.path.expanduser(entry.path))
    return found


def _row_for(repo_id):
    for entry in registry.entries():
        if entry.repo_id == repo_id:
            return entry
    return None


# --- the fleet-wide consequence, printed at the origin ----------------------------------

def consequence(plan):
    """What this plan will do on EVERY machine, including ones currently offline.

    Returned as a list of (machine_key, role, sentence). The point of naming offline machines
    explicitly is that they are the ones a human cannot check for themselves, and §3C makes
    them act without ever asking.
    """
    lines = []
    row = _row_for(plan.repo_id)
    for key, role in fleet():
        scoped = row is not None and registry.on_machine(row, role, key)
        if plan.machines and plan.machines != ("all",) and key not in plan.machines:
            lines.append((key, role, "not named in this plan — will do nothing"))
            continue
        if row is None:
            lines.append((key, role,
                          "no registry row for this id — will report `unmanaged / omitted` "
                          "and change NOTHING on disk. Absence of a row never authorises "
                          "removal."))
            continue
        if plan.kind == "purge":
            # Also before the participate check: a purge removes from EVERY machine, so a
            # row scoped away from a machine that nonetheless holds the directory is the one
            # case that must not be skipped.
            lines.append((key, role, _purge_sentence(plan, row, key)))
            continue
        if plan.kind in ("retire", "quarantine"):
            # Eviction is evaluated before the participate check, on purpose.
            lines.append((key, role,
                          "will remove the active checkout at %s and hold the material at "
                          "%s. The material REMAINS on that machine — this is a quarantine, "
                          "not an eviction." % (plan.old or row.path, HOLDING_ROOT)))
        elif not scoped:
            lines.append((key, role, "out of scope for this repo — will do nothing"))
        elif plan.kind == "rename":
            # For THIS machine we can observe reality, so we do. Printing "will move X to Y"
            # when X is already gone and Y is already there is a false statement in the one
            # place it is avoidable — and this text is the whole basis on which a human
            # authorises the plan, so an inaccuracy here is expensive.
            if key == machine.key():
                observed = transitions.observe(local(plan.old), local(plan.new))
                if observed == transitions.MOVED:
                    lines.append((key, role,
                                  "already moved — will re-check its skill links, memory "
                                  "directories and shortcut rows, and report any that still "
                                  "point at the old location"))
                    continue
                if observed == transitions.BOTH:
                    lines.append((key, role,
                                  "🔴 present at BOTH paths — will REFUSE and change nothing. "
                                  "Two directories claim this id and that needs a person."))
                    continue
                if observed == transitions.NEITHER:
                    lines.append((key, role,
                                  "present at neither path — nothing to move here"))
                    continue
            lines.append((key, role,
                          "will move %s to %s, then follow its skill links, memory "
                          "directories and shortcut rows, each checked separately"
                          % (plan.old, plan.new)))
        elif plan.kind == "rehome":
            # 🔴 `plan.old` is the PATH field, not the old URL, so using it here printed
            # "repoint origin from  to <url>" — nothing where the endpoint being abandoned
            # should be. This text is the entire basis on which a human authorises an act that
            # can send material to a different host, so a blank "from" is approving blind.
            # Same discipline as the rename branch above: for THIS machine, observe reality.
            current = ""
            if key == machine.key():
                from lib import gitutil
                fetch_url, push_urls = gitutil.remote_endpoints(
                    local(plan.old) or os.path.expanduser(row.path))
                extra = [u for u in push_urls if u and u != fetch_url]
                current = fetch_url or ""
                if extra:
                    current += " (push also: %s)" % ", ".join(extra)
            lines.append((key, role,
                          "will repoint origin from %s to %s and verify EVERY effective "
                          "fetch and push URL afterwards"
                          % (current or "an endpoint not readable from this machine", plan.new)))
    return lines


def _purge_sentence(plan, row, key):
    """What a purge will do on machine `key`, for the origin gate to print.

    For THIS machine reality is observable, so it is observed — the rename branch already
    makes that argument and it matters more here: this text is the whole basis on which a
    human authorises an act with no undo, and "will remove X" when X is already gone is a
    false statement in the one place it is avoidable.
    """
    try:
        coord = purge.parse_coordinate(plan.new)
    except ValueError as exc:
        return "🔴 the plan's hosted-repository target will not parse (%s) — nothing can act " \
               "on this plan" % exc

    declared = os.path.expanduser(row.path) if (row is not None and row.path) else ""
    local = "will remove the managed checkout at %s" % (plan.old or row.path)
    if key == machine.key() and purge.already_done(local_path_of(plan, row), declared):
        local = "already absent at the path the registry declares — nothing to remove here"

    if purge.host_reachable(coord, key):
        remote = "and run `%s`" % " ".join(purge.delete_argv(coord))
    else:
        remote = ("but declares NO route to %s, so the hosted repository is owed to a "
                  "machine that does" % coord.host)
    return "%s and its satellites, declare %s retired in the registry, tombstone it, %s. " \
           "Only with `--arm-purge`; a plain converge reports and changes nothing." \
           % (local, plan.repo_id, remote)


def local_path_of(plan, row):
    """The checkout path a plan acts on, resolved for this machine. Plan first, row second.

    One place for this, because `_converge_one` and the consequence printer must agree: a
    disagreement between what the origin promised and what the executor does is the whole
    class of bug the fleet-wide consequence print exists to prevent.
    """
    return local(plan.old) or (os.path.expanduser(row.path) if (row and row.path) else "")


def purge_fleet_warnings(plan):
    """Fleet-level facts about a purge that no single machine's sentence can carry.

    The one that matters: **if no named machine declares a route to the host, the hosted
    repository will not be deleted by anything.** Every per-machine line would still read
    correctly — each one truthfully says the remote half is owed elsewhere — and the plan
    would converge everywhere while the repo stayed up. §3C makes the origin print the whole
    fleet-wide consequence precisely so a gap that is invisible per machine is visible here.
    """
    warnings = []
    try:
        coord = purge.parse_coordinate(plan.new)
    except ValueError:
        return warnings

    named = [(key, role) for key, role in fleet()
             if not plan.machines or plan.machines == ("all",) or key in plan.machines]
    routed = [key for key, _ in named if purge.host_reachable(coord, key)]
    if not routed:
        warnings.append(
            "🔴 NO machine named in this plan declares a route to %s, so the hosted "
            "repository %s will NOT be deleted by any of them. Every per-machine line above "
            "is still true — each one says the remote half is owed elsewhere — and there is "
            "no elsewhere. Add a machine that reaches that host, or delete the repository by "
            "hand and say so in the plan's note." % (coord.host, coord.slug))
    return warnings


def print_consequence(plan, out=None):
    """Print the fleet-wide consequence. `out=None` resolves `sys.stdout` AT CALL TIME.

    🔴 This was `out=sys.stdout`, which binds the default at FUNCTION DEFINITION time — so the
    stream was captured at import and every later replacement of `sys.stdout` was ignored.
    Harmless in production only by luck: `sync.lifecycle_step` sets `sys.stdout = captured`
    around the converge, and converge happens not to print a consequence. It was not harmless
    for the tests, where 11 consequence blocks escaped a `contextlib.redirect_stdout` and
    spilled onto the runner's screen — which is how it was found.
    """
    out = out if out is not None else sys.stdout
    out.write("\nPlan %s — %s %s\n" % (plan.plan_id, plan.kind, plan.repo_id))
    out.write("  fingerprint: %s\n" % plans.fingerprint(plan))
    if plan.old or plan.new:
        out.write("  %s  ->  %s\n" % (plan.old or "(none)", plan.new or "(none)"))
    if plan.disposition:
        out.write("  disposition: %s\n" % plan.disposition)
    out.write("\nFleet-wide consequence — machines that are offline now will do this when "
              "they wake, WITHOUT asking:\n")
    for key, role, sentence in consequence(plan):
        out.write("  %-12s %-8s %s\n" % (key, role, sentence))
    if plan.kind == "rehome":
        # Approving a rehome now also authorises an edit to SHARED desired state, and the
        # consequence text is the whole basis on which that is authorised. A fleet-wide
        # registry rewrite that the prompt never mentioned would be approving blind.
        row = _row_for(plan.repo_id)
        out.write("\n  Shared desired state: `sync/registry.md`'s declared Remote for %s will "
                  "be set to %s (currently %s). One cell of one row; every other cell is left "
                  "byte-for-byte. Without this, a later rename would rewrite origin back to "
                  "the old endpoint.\n"
                  % (plan.repo_id, plan.new or "(none)",
                     (row.remote if row else "") or "(none)"))
    if plan.disposition == "purge" and plan.kind != "purge":
        out.write("\n  🔴 disposition `purge` on a %s is REFUSED at execution. Erasure is not "
                  "obtainable by renaming a rescue directory — if removal is what you want, "
                  "propose `--kind purge`, which has its own arming flag.\n" % plan.kind)
    if plan.kind == "purge":
        for warning in purge_fleet_warnings(plan):
            out.write("\n  %s\n" % warning)
        # Approving a purge also authorises an edit to SHARED desired state, for the same
        # reason the rehome block above says so: §3C makes every other machine act without
        # ever asking, so a fleet-wide registry write the prompt never mentioned would be
        # approving blind.
        purge_row = _row_for(plan.repo_id)
        out.write("\n  Shared desired state: `sync/registry.md`'s State for %s will be set to "
                  "`%s` (currently %s). One cell of one row; the row is NOT deleted, because "
                  "absence of a row never authorises removal and a deleted row reads "
                  "identically to a repo nobody registered.\n"
                  % (plan.repo_id, registry.RETIRED,
                     (purge_row.state if purge_row else "") or "(no row)"))
        out.write("  Satellites: skill symlinks Lodestar created into the checkout, its "
                  "`~/.claude/projects/` auto-memory directory, this machine's in-repo "
                  "snapshot-memory copy and the Waypoint shortcut rows this machine owns are "
                  "REMOVED with it, each named in the report. The auto-memory directory can "
                  "hold notes that outlive the repository. Removing a shortcut row runs "
                  "`waypoint remove`, which regenerates the managed block in the shell login "
                  "file. A row scoped to another machine, or marked `blocked`, is reported and "
                  "left for a human, which makes the run `partial`.\n")
        out.write("\n  %s\n" % purge.SCOPE)
        out.write("  🔴 THERE IS NO UNDO. A purge removes the checkout and deletes the "
                  "hosted repository, so re-cloning is not available afterwards. It executes "
                  "only under `converge --arm-purge`; the unattended sweep never passes that "
                  "flag, so arming is a deliberate act on each machine.\n")
    out.write("\n")


# --- commands ---------------------------------------------------------------------------

def cmd_propose(args):
    at = clock.now()
    plan = plans.Plan(
        plan_id=args.plan_id or plans.make_plan_id(args.kind, args.repo_id, at),
        kind=args.kind,
        repo_id=args.repo_id,
        machines=tuple(m.strip() for m in (args.machines or "all").split(",") if m.strip()),
        old=tilde(args.old or ""),
        # 🔴 `new` IS NOT ALWAYS A PATH. For a `rehome` it is a git URL, and `tilde()` calls
        # `os.path.abspath`, which resolves a non-absolute string against the CURRENT WORKING
        # DIRECTORY — so proposing a rehome from inside this repo stored
        # `~/dev/Lodestar/git@github…:mandrake/Thing.git`, and `_do_rehome` would have run
        # `git remote set-url origin <that>`. An `https://` target fares worse still: abspath
        # collapses the double slash to `https:/`, so the URL cannot even be recovered by
        # stripping a prefix. Found 2026-10-01 by exercising the executor against a real
        # registered repo; the plans store is append-only and earliest-row-wins, so a corrupted
        # plan id has to be corrected in place rather than superseded.
        new=(args.new or "") if args.kind in NON_PATH_NEW_KINDS else tilde(args.new or ""),
        # A purge's disposition is `purge` and nothing else. Defaulting it keeps the
        # fingerprint stable for the obvious invocation; refusing a contradictory one (below)
        # keeps `--disposition quarantine --kind purge` from reading as either.
        disposition=(args.disposition or ("purge" if args.kind == "purge" else "")),
        created_at=at,
        created_by=machine.key(),
        note=args.note or "",
    )

    if plan.kind == "purge":
        # 🔴 Validate the target AT AUTHORING TIME, and refuse to record a plan that cannot
        # execute. The plans store is append-only and earliest-row-wins, so a bad plan has to
        # be corrected in place by hand rather than superseded (CLAUDE.md §7) — and the origin
        # gate must never show a human a consequence that no machine can carry out. The
        # registry's declared `Remote` is in the repo, so this check is machine-independent
        # and answers the same here as at every converge.
        if plan.disposition != "purge":
            print("REFUSED: --kind purge takes disposition `purge`; %r says something else is "
                  "wanted and this will not guess which." % plan.disposition)
            return 1
        refusals = purge.remote_refusals(plan.repo_id, plan.new,
                                         declared_remote(plan.repo_id))
        if plan.old:
            row = _row_for(plan.repo_id)
            declared = os.path.expanduser(row.path) if (row and row.path) else ""
            refusals += [r for r in purge.local_refusals(
                plan.repo_id, local(plan.old), declared)
                if r.kind in (purge.PROTECTED, purge.UNDECLARED_PATH, purge.NO_ROW)]
        if refusals:
            print("REFUSED: this purge plan was not recorded.")
            for refusal in refusals:
                print("  %s: %s" % (refusal.kind, refusal.detail))
            return 1

    # `purge` joins `retire` here: a tombstone is a reason to refuse a plan that would bring
    # an id BACK, and a purge goes further in the same direction rather than against it. An
    # already-retired repo whose hosted copy is still up is precisely the follow-up a purge
    # exists for, and refusing it would leave the only remaining route back open for ever.
    if tombstones.is_retired(plan.repo_id) and plan.kind not in ("retire", "purge"):
        print("REFUSED: %s carries a tombstone, so it is retired and retirement is terminal. "
              "A plan that would bring it back is a separate explicit decision, not a "
              "lifecycle transition." % plan.repo_id)
        return 1

    if _row_for(plan.repo_id) is None:
        print("WARNING: no registry row has Id %r. The plan is recorded, but every machine "
              "will report `unmanaged / omitted` and change nothing — absence of a row never "
              "authorises removal." % plan.repo_id)

    if not plans.append_plan(plan):
        print("A plan with id %s already exists; nothing appended." % plan.plan_id)
        return 1

    print_consequence(plan)
    print("Recorded. It is NOT approved yet — nothing will act on it until:\n"
          "    %s approve %s" % (os.path.basename(sys.argv[0]), plan.plan_id))
    return 0


def cmd_approve(args):
    """The one gate. Prints the full fleet-wide consequence and asks once."""
    store = plans.load_plans()
    plan = store.get(args.plan_id)
    if plan is None:
        print("No plan %r. `status` lists what exists." % args.plan_id)
        return 1

    existing = plans.load_approvals().get(plan.plan_id)
    if existing is not None:
        ok, why = plans.approved(plan, {plan.plan_id: existing})
        if ok:
            print("Plan %s is already approved (%s by %s). Nothing to do."
                  % (plan.plan_id, existing.at, existing.machine))
            return 0
        print("An approval exists but has LAPSED: %s\nRe-approving against the current "
              "fingerprint." % why)

    print_consequence(plan)

    if not args.yes:
        if not sys.stdin.isatty():
            print("REFUSED: approval needs a human. Re-run with --yes only if you are that "
                  "human and have read the consequence above.")
            return 1
        answer = input("Approve this plan? Every machine above will act WITHOUT asking "
                       "again. Type the plan id to confirm: ").strip()
        if answer != plan.plan_id:
            print("Not approved (you typed %r)." % answer)
            return 1

    approval = plans.Approval(plan_id=plan.plan_id, fingerprint=plans.fingerprint(plan),
                              machine=machine.key(), at=clock.now(),
                              by=args.by or os.environ.get("USER", "unknown"))
    plans.append_approval(approval)
    journal.append(LOOP, "lifecycle",
                   note="approved plan %s (%s %s)" % (plan.plan_id, plan.kind, plan.repo_id),
                   extra={"plan": plan.plan_id, "fingerprint": approval.fingerprint,
                          "kind": plan.kind, "repo_id": plan.repo_id, "phase": "approved"})
    print("Approved %s, fingerprint %s. Every machine will now converge on its next run."
          % (plan.plan_id, approval.fingerprint))
    return 0


def cmd_status(args):
    verdict = board_freshness()
    print("board freshness: %s — %s" % ("OK" if verdict.ok else "STALE", verdict.reason))

    found = validate.faults()
    blocking = validate.blocking(found)
    print("registry validation: %d fault(s), %d blocking" % (len(found), len(blocking)))
    for fault in blocking:
        print("  BLOCKING %s: %s" % (fault.kind, fault.detail))

    store = plans.load_plans()
    approvals = plans.load_approvals()
    if not store:
        print("\nno plans recorded")
    try:
        done = completions.load()
    except completions.Unreadable as exc:
        print("\n🔴 completion store UNREADABLE: %s" % exc)
        return 1

    for plan_id, plan in sorted(store.items()):
        ok, why = plans.approved(plan, approvals)
        print("\n%s  %s %s" % (plan_id, plan.kind, plan.repo_id))
        print("   approved: %s — %s" % ("yes" if ok else "NO", why))
        retired, reason = completions.retires(
            done.get(plan_id), plans.fingerprint(plan), _row_for(plan.repo_id) is not None)
        print("   complete here: %s — %s" % ("yes" if retired else "no", reason))

    # The fleet view, derived from the committed per-machine receipts and from nothing else.
    fleet_lines = completions.section_lines()
    if fleet_lines:
        print("\nacross the fleet (from %s):" % paths.lifecycle_receipts_dir())
        for line in fleet_lines:
            print("  %s" % line)

    try:
        flight = transitions.in_flight(transitions.load())
    except transitions.Unreadable as exc:
        print("\n🔴 transition store UNREADABLE: %s" % exc)
        return 1
    if flight:
        print("\nin flight on this machine:")
        for plan_id, t in sorted(flight.items()):
            observed = transitions.observe(t.old_path, t.new_path)
            print("   %s  %s %s  observed=%s" % (plan_id, t.kind, t.repo_id, observed))
    return 0


def _converge_one(plan, dry_run, armed=False, runner=None):
    """Converge ONE plan on this machine. Returns (status, list of report lines).

    `armed` applies to `purge` and nothing else: every other kind is gated by the approval
    alone, which is §3C's whole design. A purge removes rather than relocates, so it carries
    a second gate that defaults off — see the module docstring. `runner` is the injection seam
    for the hosted-repository deletion, so a test can drive the executor without a process
    ever starting.
    """
    lines = []
    row = _row_for(plan.repo_id)
    role, key = machine.role(), machine.key()
    fingerprint = plans.fingerprint(plan)

    # 🔴 RETIREMENT, and its placement is deliberate. The row lookup above is a parse of a file
    # already in memory, so it is cheap and `retires` needs it — an `omitted` completion is
    # withheld once a registry row appears, because a plan that has become actionable must not
    # stay skipped. Everything BELOW this point is the expensive half: the machine-local lock,
    # git remote reads and a satellite scan, re-paid on every hourly sweep by every plan that
    # finished weeks ago. That is what this short-circuits.
    #
    # It sits above the `omitted` / `not-named` / `out-of-scope` branches rather than below
    # them because those are the statuses a completion can hold, so checking after them would
    # make the record unreachable for exactly the plans it was built to retire.
    try:
        recorded = completions.load().get(plan.plan_id)
    except completions.Unreadable as exc:
        return "stop", ["REFUSED: %s" % exc]
    done, why = completions.retires(recorded, fingerprint, row is not None)
    if done:
        return COMPLETE, ["%s — nothing re-evaluated. `lifecycle.py reopen %s` un-retires it."
                          % (why, plan.plan_id)]

    # Placement and eviction BEFORE the participate check — §3E. The machine that needs to
    # evict must not be the machine that skips the row.
    evicting = plan.kind in EVICTING_KINDS

    if row is None:
        return "omitted", ["unmanaged / omitted: no registry row has Id %r, so nothing on "
                           "disk was touched. Absence of a row never authorises removal."
                           % plan.repo_id]

    if plan.machines and plan.machines != ("all",) and key not in plan.machines:
        return "not-named", ["this machine is not named in the plan"]

    if not evicting and not registry.on_machine(row, role, key):
        return "out-of-scope", ["out of scope for this machine"]

    # A `purge` DISPOSITION on any other kind stays refused, and the reason is unchanged:
    # renaming a rescue directory does not remove anything, so a rename or quarantine that
    # claims to is claiming a property it cannot deliver. `--kind purge` is the route, and it
    # has its own arming flag rather than inheriting a relocation's.
    if plan.disposition == "purge" and plan.kind != "purge":
        return "refused", [
            "REFUSED: disposition `purge` on a %s is not a thing this can do. Erasure is not "
            "obtainable by renaming a rescue directory. Propose `--kind purge` if removal is "
            "what you want, or quarantine and be honest that the material stays on the "
            "machine." % plan.kind]

    old_path = local(plan.old) or os.path.expanduser(row.path)
    new_path = local(plan.new)

    try:
        with lifelock.held(plan.repo_id) as holder:
            lines.append("lock held by pid %d" % holder.pid)

            try:
                store = transitions.load()
            except transitions.Unreadable as exc:
                return "stop", ["REFUSED: %s" % exc]

            current = fingerprint
            # 🔴 Keyed by PLAN id now, not repo id. The old key gave a repo one slot, so a
            # FINISHED record for one plan was the record another plan found — and a
            # fingerprint mismatch is inevitable between two different plans, so every later
            # plan for that repo reported `stop` for ever (CLAUDE.md §7, measured 2026-10-01).
            existing = store.get(plan.plan_id)
            if existing is not None and not existing.finished_at:
                observed = transitions.observe(existing.old_path, existing.new_path)
                verdict = transitions.recovery_action(existing, observed, current)
                lines.append("recovery: %s — %s" % (verdict["action"], verdict["reason"]))
                if verdict["action"] == transitions.STOP:
                    return "stop", lines
                if verdict["action"] == transitions.CONSULT:
                    return "stop", lines
                if verdict["action"] == transitions.VERIFY:
                    old_path, new_path = existing.old_path, existing.new_path

            # 🔴 The EXPLICIT replacement for the mutual exclusion the single repo-keyed slot
            # used to provide by accident. `transitions.record` refuses this too, which is the
            # enforcing half — but a refusal that only fires at write time is invisible to a
            # `--dry-run`, and a preview that hides a stop is the wrong way round.
            clash = transitions.conflicting(store, plan.plan_id, plan.repo_id)
            if clash is not None:
                return "stop", lines + [
                    "REFUSED: plan %s already has an unfinished %s in flight for %s (old=%s "
                    "new=%s holding=%s). Two transitions for one repo can each be the other's "
                    "source or destination, so this is reported rather than resolved. Finish "
                    "or clear that one first."
                    % (clash.plan_id, clash.kind, clash.repo_id, clash.old_path or "(none)",
                       clash.new_path or "(none)", clash.holding_path or "(none)")]

            try:
                # Purge FIRST, and the order is load-bearing: `purge` is itself in
                # EVICTING_KINDS, so falling through to `_do_quarantine` would hold material
                # the plan says to remove -- quarantine deliberately retains, purge does not.
                if plan.kind == "purge":
                    return _do_purge(plan, row, old_path, dry_run, armed, lines, runner=runner)
                if evicting:
                    return _do_quarantine(plan, row, old_path, current, dry_run, lines)
                if plan.kind == "rename":
                    return _do_rename(plan, old_path, new_path, current, dry_run, lines)
                if plan.kind == "rehome":
                    return _do_rehome(plan, old_path, current, dry_run, lines)
            except transitions.Conflict as exc:
                # Reachable only if the store changed between the check above and the write —
                # another process on this machine holding a different plan's lock. Caught so a
                # race reports a stop rather than a traceback out of the sweep.
                return "stop", lines + ["REFUSED: %s" % exc]
            return "unsupported", lines + ["kind %r has no executor" % plan.kind]
    except lifelock.Busy as exc:
        return "busy", ["deferred: %s" % exc]


def declared_remote(repo_id: str) -> str:
    """The `Remote` the registry declares for this id, or "" when unknown or undeclared.

    Separated from `_do_rename` so a test can inject one without standing up a registry
    fixture, and — more importantly — so there is exactly one place that answers this. The
    empty return means *"say so and change nothing"*, never *"compute a URL from the new
    directory name"*: a guessed remote is how a repo gets repointed at a host nobody chose,
    which is the confidentiality failure this whole loop exists to prevent. Three live rows
    declare no remote and must stay untouched.
    """
    try:
        row = registry.by_id(registry.entries()).get(repo_id)
    except Exception:
        return ""
    return (row.remote or "") if row else ""


def _rename_remote_step(plan, repo_path, dry_run, lines):
    """Put `origin` on the registry's declared `Remote`. Returns (ok, lines).

    Lukas's ruling, 2026-09-30: *"Anything lodestar managed should have a matching dir and
    repo name, period. Keep them in sync. So rename MUST rename both."*

    The gap was measured, not reasoned about. The converger renamed a checkout
    `~/dev/OldName` to `~/dev/NewName` and left `origin` on
    `…/OldName.git`, while another machine — renamed by a human following a long checklist
    that happened to include `git remote set-url` — was correct. So **the automation built to
    replace those steps silently dropped one of them**, and GitHub's redirect of a renamed
    repository is what kept it invisible: both URLs `ls-remote` to the same HEAD, and
    `SYNC.md`'s endpoint section compares push against *fetch*, never against the registry.

    🔴 **The caller runs this on an ALREADY-MOVED checkout too, and that placement is the
    point.** Two machines had already converged when the gap was found, so a repair confined
    to the move branch would never reach them — it would need a hand-run `set-url` per machine,
    which is precisely how the old half-applied states became permanent. Sitting outside that
    branch means their next ordinary sweep fixes them with nobody present.

    Comparison is by IDENTITY (`skillclone.same_remote`), never string equality. This machine
    reaches GitHub over ssh and the WSL box over https, so a strict `!=` would both rewrite a
    correct remote on every sweep and force one machine onto the other's access method — on
    tower that means replacing the only scheme it can actually use.

    `ok` False means the caller must not report `converged`.
    """
    from lib import gitutil, skillclone

    declared = declared_remote(plan.repo_id)
    if not declared:
        lines.append("remote: the registry declares none for %s — origin left exactly as "
                     "found (a URL is never guessed from a directory name)" % plan.repo_id)
        return True, lines

    fetch_url, push_urls = gitutil.remote_endpoints(repo_path)
    if not fetch_url:
        lines.append("remote: no origin configured at %s — nothing to keep in sync"
                     % repo_path)
        return True, lines

    def matches(url):
        return skillclone.same_remote(url, declared)

    if matches(fetch_url) and all(matches(url) for url in push_urls):
        lines.append("remote: origin already matches the declared %s" % declared)
        return True, lines

    if dry_run:
        lines.append("would set origin to %s (currently fetch=%s push=%s)"
                     % (declared, fetch_url, ", ".join(push_urls) or "(none)"))
        return True, lines

    result = gitutil.run(repo_path, "remote", "set-url", "origin", declared)
    # `.code`, not `.returncode` — GitResult is a tuple subclass whose attribute is a property
    # over self[0]. A `getattr(result, "returncode", 1)` here is what made every rehome refuse
    # silently for this system's whole life; see CLAUDE.md §7.
    if result.code != 0:
        return False, lines + [
            "remote: git remote set-url failed (exit %d): %s"
            % (result.code, result.err.strip() or "no stderr")]

    fetch_url, push_urls = gitutil.remote_endpoints(repo_path)
    stale = [url for url in ([fetch_url] if fetch_url else []) + list(push_urls)
             if url and not matches(url)]
    lines.append("remote: set origin to %s (fetch=%s push=%s)"
                 % (declared, fetch_url, ", ".join(push_urls)))
    if stale:
        return False, lines + [
            "🔴 remote: %d endpoint(s) still point somewhere other than %s: %s. `set-url` "
            "does not clear a separately-set pushurl, so this repo can still push to the old "
            "name. Clear it with `git remote set-url --push --delete origin <url>`."
            % (len(stale), declared, ", ".join(stale))]
    return True, lines


def _do_rename(plan, old_path, new_path, fingerprint, dry_run, lines):
    observed = transitions.observe(old_path, new_path)

    if observed == transitions.BOTH:
        return "stop", lines + [
            "REFUSED: present at BOTH %s and %s. Two directories claim one id; both are "
            "preserved and this needs a person." % (old_path, new_path)]

    if observed == transitions.NOT_STARTED:
        refusals = relocate.preconditions(old_path, new_path,
                                          managed_paths=managed_paths_here())
        for refusal in refusals:
            lines.append("refusal %s: %s" % (refusal.kind, refusal.detail))
        if refusals:
            return "refused", lines

        if not dry_run:
            # 🔴 The record goes down BEFORE the mutation. That ordering IS the recovery.
            transitions.record(transitions.Transition(
                plan_id=plan.plan_id, repo_id=plan.repo_id, kind=plan.kind,
                old_path=old_path, new_path=new_path, holding_path="",
                fingerprint=fingerprint, started_at=clock.now()))
            result = relocate.move(old_path, new_path,
                                   managed_paths=managed_paths_here())
            lines.append(result["reason"])
            if not result["ok"]:
                return "refused", lines
        else:
            lines.append("would move %s to %s" % (old_path, new_path))

    # 🔴 OUTSIDE the move branch deliberately — see `_rename_remote_step`. A checkout already
    # at the destination (the Laptop and tower, which converged before this step existed)
    # must still have its origin repaired, or the repair needs a person at every machine.
    # Read the endpoints from wherever the repo actually is: after a real move that is
    # `new_path`, but a dry run on an unstarted rename has not moved anything yet.
    here = new_path if os.path.isdir(os.path.join(new_path, ".git")) else old_path
    remote_ok, lines = _rename_remote_step(plan, here, dry_run, lines)

    # A moved directory is NOT a completed rename. Every satellite is checked separately.
    # 🔴 BOTH roots are passed, and that is the whole of Lukas's second ruling of 2026-10-01.
    # They have no defaults, so until this line carried them `snapshot-memory` and `shortcut`
    # were found by nothing here — two of the four kinds inert while the report said "4 kinds
    # checked independently". He ruled the order too: shortcut rows had to become followable
    # FIRST, because scanning for rows that were unfollowable by design would have turned
    # renames that converge cleanly into permanently `partial` ones on every machine.
    found = satellites.find(old_path, new_path, repo_root=paths.root(),
                            waypoint_root=satellites.default_waypoint_root())
    followed, left = 0, []
    for sat in found:
        if not sat.followable:
            left.append("%s %s — %s" % (sat.kind, sat.where, sat.detail))
            continue
        outcome = satellites.follow(sat, dry_run=dry_run)
        if outcome["ok"] and outcome["changed"]:
            followed += 1
        elif not outcome["ok"]:
            left.append("%s %s — %s" % (sat.kind, sat.where, outcome["reason"]))
    lines.append("satellites: %d found, %d followed, %d need a person"
                 % (len(found), followed, len(left)))
    lines.extend("  needs a person: %s" % item for item in left)

    if dry_run:
        # Say which outcome this WOULD reach. `left` is already known here — unfollowable
        # satellites never needed a write to identify — so reporting "would-converge" when the
        # real run will report "partial" would make the dry run the more optimistic of the two,
        # which is backwards for a preview whose job is to warn.
        return ("would-be-partial" if (left or not remote_ok) else "would-converge"), lines

    if left or not remote_ok:
        unresolved = []
        if left:
            unresolved.append("%d satellite(s) still point at the old location" % len(left))
        if not remote_ok:
            unresolved.append("origin does not match the registry's declared remote")
        return "partial", lines + [
            "PARTIAL: the directory is in place but %s. This is deliberately not reported as "
            "converged." % " and ".join(unresolved)]

    transitions.finish(plan.plan_id)
    return "converged", lines


def _rehome_declaration_step(plan, dry_run, lines):
    """Put the registry's declared `Remote` on the endpoint this rehome moves to. (ok, lines).

    Lukas's ruling, 2026-10-01: **a rehome must update the declared `Remote` as part of doing
    it**, chosen over the alternative of making the registry authoritative and refusing a
    rehome that disagrees with it.

    The hazard was measured end-to-end, not reasoned about. A rehome repointed `origin` at the
    private host and left the row declaring github.com; `_rename_remote_step` then rewrites origin
    **to whatever the registry declares**, so a later rename of the same repo reported
    `converged` while silently putting origin back on the endpoint the repo had been moved off.
    Two correct-looking steps composing into a rollback of a confidentiality decision.

    🔴 **The declaration is written BEFORE the `set-url`, and the order is the safety
    argument.** Written afterwards, a failed registry write leaves origin on the new host with
    desired state naming the old one — the exact hazard, re-created at the one moment nobody is
    watching. Written first, a failed `set-url` leaves desired state naming the approved
    endpoint and origin behind it, which the declared-vs-actual audit reports and which the
    rename step then repairs *toward* the approval. Both orders have a failure; only one of them
    fails in the direction the human already authorised.

    It is also why this runs on a machine that does **not** hold the checkout. The declaration
    is fleet-wide desired state, not a per-machine fact, so making it depend on which machine
    happened to have the directory is how a half-applied state becomes permanent.

    Three things this deliberately does NOT treat as failure:

    - **No registry row.** Then nothing declares a remote for this id, `_rename_remote_step`
      finds none and leaves origin alone, so the collision cannot occur — and `_converge_one`
      has already reported `omitted` before any executor runs.
    - **An already-matching declaration**, compared by `skillclone.same_remote` identity rather
      than string equality, for the same reason `_rename_remote_step` does: this machine reaches
      GitHub over ssh and tower over https, and rewriting the shared declaration to one
      machine's scheme would force the other onto an access method it cannot use.
    - **A blank declaration.** A few live rows may declare no remote, to be measured
      against `sync/registry.md` and not inferred; any count recorded elsewhere in this file
      can go stale. Filling one in from a plan is
      not the guessed-from-a-directory-name URL the `_rename_remote_step` docstring forbids — it
      is an explicitly approved endpoint, and recording it is what lets the audit check anything
      at all afterwards.
    """
    from lib import skillclone

    if not plan.new:
        return False, lines + [
            "REFUSED: this rehome plan names no new endpoint, so there is nothing to declare "
            "and nothing to repoint to."]

    row = _row_for(plan.repo_id)
    if row is None:
        lines.append("registry: no row has Id %s, so there is no declaration to keep in step "
                     "(and nothing will later rewrite origin from one)" % plan.repo_id)
        return True, lines

    if skillclone.same_remote(row.remote, plan.new):
        lines.append("registry: already declares %s for %s" % (row.remote, plan.repo_id))
        return True, lines

    if dry_run:
        lines.append("would set the registry's declared Remote for %s to %s (currently %s)"
                     % (plan.repo_id, plan.new, row.remote or "(none)"))
        return True, lines

    result = registry.set_declared_remote(plan.repo_id, plan.new)
    lines.append("registry: %s" % result["reason"])
    if not result["ok"]:
        # The underlying reason travels, never a fixed sentence: a refusal whose text cannot
        # name its own cause is the only diagnosis a human ever gets.
        return False, lines + [
            "🔴 desired state was NOT updated, so the registry still declares %s while this "
            "rehome moves origin to %s. `_rename_remote_step` rewrites origin to whatever the "
            "registry declares, so a later rename would put this repo back on the old endpoint."
            % (row.remote or "(none)", plan.new)]
    return True, lines


def _do_rehome(plan, repo_path, fingerprint, dry_run, lines):
    """Repoint origin, then verify EVERY effective fetch and push URL (§3D guard 8).

    The push URL is the whole reason this is a separate verification rather than a write and
    a shrug: `git remote set-url origin <new>` leaves a separately-set *push* URL pointing at
    the OLD host, so a repo believed moved to the private host can still push to github.com.
    Reproduced in a fixture, 2026-09-30.

    The declared `Remote` is brought along first — see `_rehome_declaration_step` for why that
    ordering, and why it happens even on a machine holding no checkout.
    """
    from lib import gitutil

    declared_ok, lines = _rehome_declaration_step(plan, dry_run, lines)
    if not declared_ok:
        return "refused", lines

    # 🔴 NOTHING IS RECORDED UNTIL WE KNOW WE CAN ACT, and the ordering is the whole fix.
    # Measured 2026-10-01 while exercising both executors against one real repo: a quarantine
    # converged and finished its record, then the still-approved rehome ran in the SAME pass
    # against the now-missing directory, wrote its transition first, and only then failed at
    # `set-url`. Because `transitions` is keyed by **repo_id**, that unfinished record clobbered
    # the quarantine's finished one — and every later plan for the repo then reported `stop`
    # ("an old record is evidence that something was started, not permission to finish"). One
    # ordinary failure wedged the repo and needed a person.
    #
    # There is nothing to repoint on a machine that does not hold the checkout, so this is a
    # no-action converge rather than a refusal — the same answer the rename branch already gives
    # for `NEITHER`. The record-before-mutate discipline is untouched: it still happens before
    # the only step that changes anything.
    if not os.path.isdir(repo_path):
        lines.append("nothing at %s — this machine does not hold the checkout, so there is no "
                     "origin here to repoint" % repo_path)
        return ("would-converge" if dry_run else "converged"), lines

    if dry_run:
        fetch_url, push_urls = gitutil.remote_endpoints(repo_path)
        lines.append("would set origin to %s (currently fetch=%s push=%s)"
                     % (plan.new, fetch_url, ", ".join(push_urls) or "(none)"))
        return "would-converge", lines

    transitions.record(transitions.Transition(
        plan_id=plan.plan_id, repo_id=plan.repo_id, kind=plan.kind,
        old_path=repo_path, new_path=repo_path, holding_path="",
        fingerprint=fingerprint, started_at=clock.now()))

    result = gitutil.run(repo_path, "remote", "set-url", "origin", plan.new)
    # 🔴 `GitResult` exposes `.code`, NOT `.returncode` — it is a tuple subclass whose
    # attribute is a property over `self[0]`. This was `getattr(result, "returncode", 1)`,
    # so the missing attribute fell through to the default 1 and EVERY rehome refused with
    # "git remote set-url failed" while the set-url had actually succeeded. The defensive
    # default is what hid it: without it the mismatch would have raised AttributeError on
    # the first run instead of becoming a permanent silent refusal.
    if result.code != 0:
        return "refused", lines + [
            "git remote set-url failed (exit %d): %s"
            % (result.code, result.err.strip() or "no stderr")]

    fetch_url, push_urls = gitutil.remote_endpoints(repo_path)
    stale = [url for url in ([fetch_url] if fetch_url else []) + list(push_urls)
             if url and url != plan.new]
    lines.append("endpoints after: fetch=%s push=%s" % (fetch_url, ", ".join(push_urls)))
    if stale:
        return "partial", lines + [
            "🔴 PARTIAL: %d endpoint(s) still point somewhere other than %s: %s. "
            "`set-url` does not clear a separately-set pushurl, so this repo can still push "
            "to the old host. Clear it with `git remote set-url --push --delete`."
            % (len(stale), plan.new, ", ".join(stale))]

    transitions.finish(plan.plan_id)
    return "converged", lines


def _purge_declaration_step(plan, row, dry_run, lines):
    """Set the registry row's `State` to `retired` as part of the purge. (ok, lines).

    Lukas's ruling, 2026-10-01, in the same breath as the rehome one: **a purge writes that
    desired state itself** rather than leaving a row reading `active` for a repository that no
    longer exists. Two reasons he gave, and they are different reasons:

    - It follows from the rehome decision made the same day — an executor writes the
      declaration rather than leaving it stale for a human to chase.
    - The spec's own `Retired` postcondition, *"Lodestar no longer syncs or clones this id, a
      permanent tombstone is retained"*, is **part of what a purge means**. Leaving the row
      active left the act half-done by its own definition, and `validate` said so on every
      sweep: `active-but-retired`, a contradiction nobody could resolve without a hand edit.

    🔴 **THE ORDER IS THE SAFETY ARGUMENT, AND IT RUNS FIRST — BEFORE THE TOMBSTONE AND BEFORE
    ANY REMOVAL.** `_rehome_declaration_step` settles the form of the argument: both orders can
    fail, so the question is only ever *which direction a failure leaves us in*.

    - **Declaration first.** A failed write refuses with nothing removed, nothing tombstoned
      and desired state exactly as found — the whole plan is retryable and no terminal record
      exists. A write that succeeds and a removal that then fails leaves a row declaring
      `retired` with material still present, which `validate` reports and which a retry
      finishes *toward* the approval.
    - **Declaration last.** The removal succeeds, the write fails, and the contradiction this
      change exists to delete is re-created unattended — row `active`, repository gone, nobody
      watching. That is strictly the worse direction and it is the state we were asked to end.

    It also goes before the **tombstone** specifically, not merely before the removal. A
    tombstone is terminal and append-only; a registry cell is editable. Doing the editable
    write first means a failure at this step has left nothing irreversible behind, while
    tombstoning first and failing here would produce `active-but-retired` — today's complaint,
    reached by a new route.

    🔴 **The row is NEVER deleted.** §3E is explicit that absence of a row never authorises
    removal, and a deleted row reads identically to a repo nobody ever registered — so deleting
    it would destroy the one piece of evidence that this id was managed and then retired.

    **Two machines purging one id will both write this cell, and that is sufficient.**
    `registry.set_state` treats already-`retired` as a no-op *success*, so whichever machine
    converges second adds nothing and reports converged rather than `partial`. The idempotence
    is not a convenience: a plan named `all` runs on every machine, and a second writer reading
    as failure would leave the plan permanently unfinished everywhere except wherever it
    happened to run first. Two peers writing the identical value into one line also cannot
    produce a git conflict, which is why no lock is needed beyond the one `_converge_one`
    already holds.

    `row` is the one `_converge_one` already resolved, rather than a second `_row_for` read:
    the executor and the declaration must agree about which row they are talking about, and two
    independent reads of a file a peer may be editing is how they would come to disagree.
    """
    if row is None:
        # Unreachable through `_do_purge` — `purge.local_refusals` already refuses with NO_ROW
        # when nothing declares a path, and `_converge_one` reports `omitted` before any
        # executor runs. Kept as an explicit, non-blocking report rather than an assertion: if
        # a future caller does reach it, "no row to retire" is a true statement and not a
        # reason to abandon a removal.
        lines.append("registry: no row has Id %s, so there is no State cell to retire"
                     % plan.repo_id)
        return True, lines

    if (row.state or "").strip().lower() == registry.RETIRED:
        lines.append("registry: the row for %s already reads State: %s"
                     % (plan.repo_id, registry.RETIRED))
        return True, lines

    if dry_run:
        lines.append("would set the registry's State for %s to %s (currently %s), BEFORE the "
                     "tombstone and before any removal"
                     % (plan.repo_id, registry.RETIRED, row.state or "(blank)"))
        return True, lines

    result = registry.set_state(plan.repo_id, registry.RETIRED)
    lines.append("registry: %s" % result["reason"])
    if not result["ok"]:
        # The underlying reason travels, never a fixed sentence — CLAUDE.md §7 records a whole
        # system's worth of wrong diagnosis caused by a refusal that named the wrong cause.
        return False, lines + [
            "🔴 desired state was NOT updated, so nothing was removed and no tombstone was "
            "written. The registry still declares %s active for %s, and refusing here keeps "
            "the whole purge retryable rather than leaving a removed repository behind a row "
            "that still claims it is live." % (row.name, plan.repo_id)]
    return True, lines


def _purge_satellite_step(repo_path, dry_run, lines):
    """Remove the satellites of a removed checkout. (removed, left, lines).

    Lukas's ruling, 2026-10-01: **follow and remove them, as a rename already does.** Before
    it, `_PURGE_OUT_OF_SCOPE` named the four kinds in prose and checked none of them, so a
    skill symlink into the removed checkout was left dangling and an auto-memory directory was
    orphaned with nothing saying which ones existed.

    The traversal is `satellites.removals`, which is `find`'s census with no destination — not
    a second walk of the same four kinds. Followability for a removal is per-kind and argued
    there; here it reduces to: a satellite this machine created or owns is removed, and
    anything belonging to a human or another machine is **named** and counted as needing a
    person.

    🔴 **Every removal is named individually, and that is the point rather than verbosity.**
    He was shown and accepted that this removes an auto-memory directory which can hold notes
    outliving the repository. An accepted loss and a silent one differ only in whether the
    report says which files went, so the per-satellite line is what makes the acceptance mean
    anything afterwards.

    `dry_run` reports what each removal would do and mutates nothing, so the preview knows
    every unfollowable satellite the real run will find — which is what lets `_do_purge` answer
    `would-be-partial` instead of the more optimistic `would-converge`.
    """
    found = satellites.removals(repo_path)
    removed, left = [], []
    for sat in found:
        if not sat.followable:
            left.append("%s %s — %s" % (sat.kind, sat.where, sat.detail))
            continue
        outcome = satellites.remove(sat, dry_run=dry_run)
        if outcome["ok"]:
            removed.append("%s %s" % (sat.kind, sat.where))
            lines.append("satellite: %s" % outcome["reason"])
        else:
            left.append("%s %s — %s" % (sat.kind, sat.where, outcome["reason"]))
    lines.append("satellites: %d found, %d %s, %d need a person"
                 % (len(found), len(removed),
                    "would be removed" if dry_run else "removed", len(left)))
    lines.extend("  needs a person: %s" % item for item in left)
    return removed, left, lines


def _do_purge(plan, row, repo_path, dry_run, armed, lines, runner=None):
    """Remove the checkout here, tombstone the id, delete the hosted repository.

    Lukas's scoping, 2026-10-01, and the scope rides on every line of output: *"Remove the
    managed checkout on every machine, retire the id with a permanent tombstone so nothing
    re-clones it, and delete the hosted repository ... so there is no route back."* Backups
    are explicitly out of his jurisdiction and are not touched, so this says **removal**, with
    the storage scope named, and never erasure.

    **Four properties, each of which was a bug somewhere else in this file first:**

    1. 🔴 **Nothing mutates unless `armed and not dry_run`.** One branch, so there is no second
       path to audit. Without arming the status is `disarmed` and the exit code is non-zero —
       an approved destructive plan sitting unexecuted is an open obligation, not a clean run.
    2. 🔴 **The TOMBSTONE GOES DOWN FIRST**, before the removal and before the remote delete.
       A tombstone appended afterwards is lost precisely when it is most needed: a removal that
       half-fails would leave an id with no retirement recorded, and the next sweep would
       re-clone the repo it was told to remove. Every refusal is evaluated before this, so a
       tombstone is never written for a purge that was going to refuse anyway.
    3. 🔴 **No transition record is written, and that is a decision rather than an omission.**
       `transitions` answers "where did the material go?", and for a purge the answer is
       "nowhere, it was removed" — which the tombstone already states durably and
       cross-machine. Writing one would be actively harmful twice over: the store is keyed by
       **repo_id**, so a purge record would clobber another plan's (measured 2026-10-01, one
       ordinary failure wedged a repo and needed a person); and `observe(path, "")` on a
       completed purge reports `NEITHER`, which `recovery_action` correctly turns into
       `consult-holding` — a `stop` that would wedge every retry of the remote half. Recovery
       here is by inspection and needs no third store: tombstone present plus the declared path
       absent IS "this machine's half is done".
    4. 🔴 **A dry run previews the run it would actually be**, so `--dry-run` without
       `--arm-purge` reports `would-be-disarmed`, and an armed preview that already knows a
       satellite cannot be followed reports `would-be-partial`. A preview more optimistic than
       the next real run is the failure mode LESSONS.md records for
       `would-converge`-instead-of-`partial`.
    5. 🔴 **The registry row is set to `State: retired` FIRST**, before the tombstone and before
       any removal — `_purge_declaration_step` holds the whole which-direction-does-a-failure-
       leave-us-in argument, and the row is never deleted.
    6. 🔴 **Satellites are followed and removed AFTER the checkout is gone**, never before:
       `_purge_satellite_step` names every one it removes and counts the rest as needing a
       person, which makes the run `partial`. A failed local removal leaves them alone, because
       a symlink into a checkout that still exists is a working capability.

    An unreachable host is **deliberate non-participation, not a fault** — the meaning
    `UNREACHABLE` already carries in the sweep. a home machine may have no route to the private host at all,
    so its remote half is owed to a machine that does, and reporting `partial` there would flag
    for ever with no resolution available on that machine. The gap that *would* be invisible —
    no machine having a route — is caught at the origin by `purge_fleet_warnings`, which is
    what §3C's fleet-wide print is for.
    """
    lines.append(purge.SCOPE)

    declared_path = os.path.expanduser(row.path) if (row is not None and row.path) else ""
    done_here = purge.already_done(repo_path, declared_path)

    refusals = [r for r in purge.local_refusals(plan.repo_id, repo_path, declared_path)
                if not (done_here and r.kind == purge.MISSING_SOURCE)]
    refusals += purge.remote_refusals(plan.repo_id, plan.new, declared_remote(plan.repo_id))
    if refusals:
        for refusal in refusals:
            lines.append("refusal %s: %s" % (refusal.kind, refusal.detail))
        return "refused", lines + ["REFUSED: nothing was removed and no tombstone was written."]

    coord = purge.parse_coordinate(plan.new)
    reachable = purge.host_reachable(coord)
    argv = purge.delete_argv(coord)

    # 🔴 Check the token scope BEFORE the local removal, and REFUSE on a positive absence.
    # This machine has a route to the host, so the remote half is its job — and if it provably
    # cannot do it, removing the local copy first would destroy the checkout and leave the one
    # irreversible step undone, which is `partial` reported after the loss rather than before
    # it. The remedy is `gh auth refresh`, an interactive login that belongs to the account
    # holder, so refusing hands it back to the one person who can act. Observed in practice:
    # a private-host token can lack `delete_repo` while the github.com
    # one has it, so this is a live condition and not a hypothetical.
    #
    # UNMEASURED is not absent. `can_delete_on` returns three answers for exactly this reason;
    # manufacturing a refusal out of a failed `gh auth status` would block a working path.
    if reachable:
        scope = purge.can_delete_on(coord.host, runner)
        lines.append("gh authorization: %s" % scope["detail"])
        if scope["known"] and not scope["ok"]:
            return "refused", lines + [
                "REFUSED before touching anything: %s. %s Nothing was removed and no "
                "tombstone was written — the local checkout is still the only copy you can "
                "lose, so it is not spent on a remote deletion that cannot happen."
                % (scope["detail"], purge.scope_hint(coord))]

    if done_here:
        lines.append("local: nothing at %s — already absent at the path the registry "
                     "declares, so this machine's checkout is already removed" % repo_path)
    else:
        material = purge.local_material(repo_path)
        lines.append("local: the managed checkout at %s" % repo_path)
        lines.append("local material that exists nowhere else: %s%s"
                     % (material["detail"],
                        " (UNKNOWN — git could not answer, so treat this as 'unmeasured', "
                        "never as 'none')" if material["unknown"] else ""))

    lines.append("hosted repository: %s on %s" % (coord.slug, coord.host))
    if reachable:
        lines.append("remote command: %s" % " ".join(argv))
    else:
        lines.append("remote: this machine declares no route to %s, so the hosted repository "
                     "is owed to a machine that does — deliberate non-participation, not a "
                     "fault" % coord.host)

    if dry_run or not armed:
        # The declaration is previewed in the position it will run in — first — so the preview
        # shows the ordering and not just the acts.
        _, lines = _purge_declaration_step(plan, row, True, lines)
        lines.append("tombstone: would record %s as retired, BEFORE any removal"
                     % plan.repo_id)
        _, left, lines = _purge_satellite_step(repo_path, True, lines)
        lines.append(_PURGE_OUT_OF_SCOPE)
        if dry_run:
            if armed:
                # Reachability does NOT make this `would-be-partial`: an unreachable host is
                # this machine fully discharging its share — see the docstring — so flagging it
                # would invent a fault the real run will not report, which is the same
                # dishonesty as the optimistic direction. The genuinely-cannot-complete case
                # (the token lacks `delete_repo`) already returned `refused` above.
                #
                # An unfollowable SATELLITE is the opposite case and does make this
                # `would-be-partial`: it is already known here, needing no write to identify,
                # so reporting `would-converge` would make the preview the more optimistic of
                # the two runs — exactly the failure LESSONS.md records for the rename path.
                if left:
                    return "would-be-partial", lines + [
                        "PREVIEW of the ARMED run, which will report PARTIAL: %d satellite(s) "
                        "cannot be followed by this machine. Nothing was changed."
                        % len(left)]
                return "would-converge", lines + [
                    "PREVIEW of the ARMED run. Nothing was changed."]
            return "would-be-disarmed", lines + [
                "PREVIEW of the run you will actually get: without `--arm-purge` a converge "
                "reports `disarmed` and changes nothing. Preview the armed run with "
                "`converge --dry-run --arm-purge`."]
        return "disarmed", lines + [
            "DISARMED: this plan is approved but purge does not execute without "
            "`--arm-purge`. Nothing was removed. This exits non-zero on purpose — an "
            "approved destructive plan sitting unexecuted is an open obligation, and the "
            "unattended sweep never passes that flag."]

    # --- the only branch that changes anything ------------------------------------------
    # 🔴 DESIRED STATE FIRST, then the tombstone, then the removals. See
    # `_purge_declaration_step` for why a failure here must leave nothing irreversible behind.
    declared_ok, lines = _purge_declaration_step(plan, row, False, lines)
    if not declared_ok:
        return "refused", lines

    # 🔴 TOMBSTONE BEFORE ANY REMOVAL. See property 2 in the docstring.
    if tombstones.append(plan.repo_id,
                         reason=plan.note or "purged by plan %s" % plan.plan_id):
        lines.append("tombstoned %s BEFORE removing anything — retirement is terminal and "
                     "nothing will re-clone this id" % plan.repo_id)
    else:
        lines.append("%s was already tombstoned; retirement is recorded" % plan.repo_id)

    unresolved = []
    gone = done_here
    if not done_here:
        removal = purge.remove_tree(repo_path, armed=True)
        lines.append(removal["reason"])
        gone = bool(removal["ok"])
        if not removal["ok"]:
            unresolved.append("the checkout at %s is still there" % repo_path)

    # 🔴 SATELLITES AFTER THE CHECKOUT, and only once it is actually gone. A skill symlink into
    # a checkout that still exists is a WORKING capability and an auto-memory directory beside
    # a live repo is live notes — removing either while the removal has failed would destroy
    # something useful for a repository that is still there, which is a worse outcome than the
    # failure itself. So a failed removal leaves the satellites alone and says so; the retry
    # that finally removes the checkout is what removes them.
    if gone:
        _, left, lines = _purge_satellite_step(repo_path, False, lines)
        if left:
            unresolved.append("%d satellite(s) need a person" % len(left))
    else:
        lines.append("satellites: NOT touched — the checkout is still there, so its symlinks "
                     "and memory directories still refer to something that exists")

    if reachable:
        outcome = purge.delete_remote(coord, armed=True, runner=runner)
        lines.append(outcome["reason"])
        if not outcome["ok"]:
            unresolved.append("the hosted repository %s is still there" % coord.slug)

    lines.append(_PURGE_OUT_OF_SCOPE)

    if unresolved:
        return "partial", lines + [
            "PARTIAL: %s. The id is tombstoned and the registry row declares it retired, so "
            "nothing will re-clone it, but this is deliberately not reported as converged and "
            "keeps flagging until a person resolves it." % " and ".join(unresolved)]
    return "converged", lines


#: Named in every purge report, armed or not. These are real leftovers and saying nothing
#: about them would be the overclaim this whole module is written against — but they are
#: outside the scope Lukas set, so they are reported and not touched.
#:
#: 🔴 It used to name skill symlinks and memory directories here, because a purge reported
#: them and left them dangling. His 2026-10-01 ruling moved those INSIDE the scope, so they
#: are now followed and each one is named individually in the satellites lines above. What
#: remains is what genuinely belongs to someone else.
_PURGE_OUT_OF_SCOPE = (
    "NOT touched, and outside the scope above: any skill symlink this machine did not "
    "create, another machine's snapshot-memory subtree, a Waypoint shortcut row scoped to "
    "another machine or marked `blocked`, and any quarantine copy under "
    "`~/dev/.lodestar/quarantine`. Each one that exists is named individually above; check "
    "those by hand.")


def _absent_as_evicted(plan, row, repo_path, dry_run, lines):
    """(status, lines) when there is nothing at `repo_path`, else None. Lukas, 2026-10-01.

    **"Nothing here to evict" is the desired end state of an eviction, not a failure.**
    `relocate.quarantine` on a missing path returns `ok: False`, and mapping that to `refused`
    made a retirement aimed at `all` machines **permanently refused on every machine that already
    lacked the directory** — flagging on every sweep for ever. Measured on a machine where a
    retired checkout had never been present.

    Rename never had this problem because it asks `transitions.observe`, so an already-moved
    checkout reports `DESTINATION` and converges. Eviction had no equivalent observation, so
    absence and failure were the same answer.

    🔴 **The guard is the entire design, because the naive fix is worse than the bug.** Treating
    *any* missing path as converged would make a mistyped `--old` report success on every machine
    while the real directory sat untouched — a green light on an act that never happened, in the
    one place it is most expensive to be wrong. So absence counts as done **only at the path the
    registry row itself declares.** Anywhere else it stays a refusal, because an absent
    hand-typed path is a typo until proven otherwise; and with no row at all there is nothing to
    check the absence against, so that refuses too.

    Nothing is recorded in `transitions` — there is no move to recover from — but a `retire`
    still tombstones, because a retirement every machine had already completed by hand would
    otherwise never be recorded anywhere. Duplicate rows are safe (`tombstones.load` is
    earliest-wins) and a retirement with no tombstone is the worse failure.
    """
    if os.path.exists(repo_path):
        return None

    declared = os.path.expanduser(row.path) if (row is not None and row.path) else ""
    if not declared or os.path.realpath(declared) != os.path.realpath(repo_path):
        return "refused", lines + [
            "REFUSED: nothing at %s, and the registry does not declare that path for %s "
            "(declared: %s). An absent path that is not the declared one is a typo until proven "
            "otherwise — reporting it as evicted would claim an act that never happened."
            % (repo_path, plan.repo_id, declared or "(no row)")]

    lines.append("nothing at %s — already absent at the path the registry declares, so there is "
                 "nothing on this machine to evict" % repo_path)
    if dry_run:
        return "would-converge", lines
    if plan.kind == "retire":
        tombstones.append(plan.repo_id, reason=plan.note or "retired by plan %s" % plan.plan_id)
        lines.append("tombstoned %s — retirement is terminal" % plan.repo_id)
    return "converged", lines


def _do_quarantine(plan, row, repo_path, fingerprint, dry_run, lines):
    already = _absent_as_evicted(plan, row, repo_path, dry_run, lines)
    if already is not None:
        return already

    holding = relocate.quarantine(repo_path, HOLDING_ROOT, dry_run=True)
    if not holding["ok"]:
        return "refused", lines + [holding["reason"]]

    if dry_run:
        return "would-converge", lines + [holding["reason"]]

    # The holding location is recorded BEFORE the move, because `transitions` has no way to
    # find material whose destination was never written down.
    transitions.record(transitions.Transition(
        plan_id=plan.plan_id, repo_id=plan.repo_id, kind=plan.kind,
        old_path=repo_path, new_path="", holding_path=holding["holding_path"],
        fingerprint=fingerprint, started_at=clock.now()))

    result = relocate.quarantine(repo_path, HOLDING_ROOT)
    lines.append(result["reason"])
    if not result["ok"]:
        return "refused", lines

    if plan.kind == "retire":
        tombstones.append(plan.repo_id, reason=plan.note or "retired by plan %s"
                          % plan.plan_id)
        lines.append("tombstoned %s — retirement is terminal" % plan.repo_id)

    transitions.finish(plan.plan_id)
    return "converged", lines


def status_changed(plan_id, status, state_path=None):
    """True, and remembered, when `status` differs from the last one journaled for `plan_id`.

    The journal records transitions, not heartbeats. Journaling every run kept the board from
    ever passing the sweep's quiescence gate — see `paths.lifecycle_journal_state_file`. The
    exit code still flags a stuck plan every sweep; only the repeated journal event is dropped.
    An unreadable state file reads as empty, so the failure mode is one duplicate event.
    """
    state_path = state_path or paths.lifecycle_journal_state_file()
    try:
        with open(state_path, encoding="utf-8") as handle:
            seen = json.load(handle)
        if not isinstance(seen, dict):
            seen = {}
    except (OSError, ValueError):
        seen = {}
    if seen.get(plan_id) == status:
        return False
    seen[plan_id] = status
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as handle:
        json.dump(seen, handle, indent=2, sort_keys=True)
    return True


def cmd_converge(args):
    """Execute every approved plan that applies here. NEVER prompts — §3C."""
    verdict = board_freshness()
    if not verdict.ok and not args.ignore_freshness:
        print("REFUSED before touching any repo: %s\n\nThis machine may be holding "
              "yesterday's scope and yesterday's host, and acting on that can push material "
              "to a host the current desired state has moved away from. Run `git pull "
              "--rebase` here first." % verdict.reason)
        return 1
    print("board freshness: %s" % verdict.reason)

    blocking = validate.blocking(validate.faults())
    if blocking:
        print("REFUSED: %d blocking validation fault(s). A merged commit is a transport "
              "result, not an authorization result." % len(blocking))
        for fault in blocking:
            print("  %s: %s" % (fault.kind, fault.detail))
        return 1

    store = plans.load_plans()
    approvals = plans.load_approvals()
    actionable = []
    for plan_id, plan in sorted(store.items()):
        ok, why = plans.approved(plan, approvals)
        if not ok:
            print("skip %s — %s" % (plan_id, why))
            continue
        actionable.append(plan)

    if not actionable:
        print("nothing approved and pending")
        return 0

    if args.arm_purge:
        print("🔴 PURGE IS ARMED for this run. A purge plan will remove its checkout and "
              "delete its hosted repository. There is no undo.")

    worst = 0
    for plan in actionable:
        status, lines = _converge_one(plan, args.dry_run, armed=args.arm_purge)
        print("\n%s [%s] %s" % (plan.plan_id, status, plan.repo_id))
        for line in lines:
            print("   %s" % line)
        # 🔴 A DRY RUN RECORDS NOTHING. This repo already settled that for mail receipts —
        # "there is deliberately no outcome value meaning pretended" — and it matters more
        # here, because a completion written from a preview would retire a plan that has never
        # run and the next real sweep would skip it in silence. `COMPLETE` is also not
        # re-recorded: the record it was read from is already on disk and rewriting it would
        # move `at` on every sweep for no new fact.
        if not args.dry_run:
            if status != COMPLETE:
                completions.record(plan.plan_id, plan.repo_id, plans.fingerprint(plan), status)
            if status_changed(plan.plan_id, status):
                journal.append(LOOP, "lifecycle",
                               note="%s %s on %s: %s" % (plan.kind, plan.repo_id,
                                                         machine.key(), status),
                               extra={"plan": plan.plan_id, "repo_id": plan.repo_id,
                                      "kind": plan.kind, "phase": "converge",
                                      "status": status})
        # `partial` counts as non-zero deliberately. It means the directory landed but a
        # satellite still points at the old location, so a capability may be silently missing
        # — exactly what §3D guard 7 exists to surface. Exiting 0 would make
        # `sync.lifecycle_step` report SYNCED and the unresolved satellite would vanish from
        # the report, which is the failure mode rather than a tidy outcome. It will keep
        # flagging every sweep until a human resolves it, and that is the intent.
        # `disarmed` joins the non-zero set for the same reason `partial` is in it: the
        # report must not read as a clean run while an approved destructive plan sits
        # unexecuted. It will flag on every sweep until a human either arms it here or
        # supersedes the plan, and that persistence is the intent.
        if status in NONZERO_STATUSES:
            worst = 1

    # The completion store is machine-local and therefore unreadable by a peer, so the
    # cross-machine view needs a committed artifact. One file per machine key, no clock read in
    # it, rewritten only when the bytes change — see `lib/completions.receipt_payload`.
    if not args.dry_run:
        try:
            if completions.write_receipt(machine.key(), completions.load()):
                print("\nreceipt: updated %s"
                      % paths.lifecycle_receipt_file(machine.key()))
        except (OSError, completions.Unreadable) as error:
            # A receipt is a read channel for other machines, never a gate on this one's work.
            # The acts above already happened; failing the run now would misreport them.
            print("\nreceipt: could NOT be written (%s) — this machine's completions are not "
                  "readable from any other machine until it is." % error)
    return worst


def cmd_reopen(args):
    """Drop a plan's completion record here, so the next converge re-evaluates it.

    The deliberate un-retirement, and the reason `completions.forget` is not an inert feature.
    It is machine-local on purpose: a completion is a per-machine fact, so reopening one must
    not reach across the fleet and silently re-run an act on a machine nobody asked about.
    """
    store = plans.load_plans()
    if args.plan_id not in store:
        print("WARNING: no plan %r in %s. Clearing the record anyway — a plan id may be "
              "ahead of this machine's checkout." % (args.plan_id, paths.plans_file()))
    if completions.forget(args.plan_id):
        print("Reopened %s on %s. It will be re-evaluated on the next converge; nothing on "
              "disk was changed." % (args.plan_id, machine.key()))
        return 0
    print("No completion record for %s on %s — nothing to reopen."
          % (args.plan_id, machine.key()))
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command")

    propose = sub.add_parser("propose", help="author a lifecycle plan (approves nothing)")
    propose.add_argument("--kind", required=True, choices=plans.KINDS)
    propose.add_argument("--id", dest="repo_id", required=True)
    propose.add_argument("--old", default="")
    propose.add_argument("--new", default="")
    propose.add_argument("--machines", default="all")
    propose.add_argument("--disposition", default="",
                         choices=["", "quarantine", "purge"])
    propose.add_argument("--note", default="")
    propose.add_argument("--plan-id", dest="plan_id", default="")
    propose.set_defaults(func=cmd_propose)

    approve = sub.add_parser("approve", help="the origin gate — asks once, then every "
                                             "machine obeys without prompting")
    approve.add_argument("plan_id")
    approve.add_argument("--yes", action="store_true",
                         help="skip the interactive confirmation; you have read the "
                              "consequence and are authorising it")
    approve.add_argument("--by", default="")
    approve.set_defaults(func=cmd_approve)

    status = sub.add_parser("status", help="freshness, validation, plans, in-flight work")
    status.set_defaults(func=cmd_status)

    converge = sub.add_parser("converge", help="execute approved plans here; never prompts")
    converge.add_argument("--dry-run", action="store_true")
    converge.add_argument("--ignore-freshness", action="store_true",
                          help="act despite a stale board. This is a scope-violation risk, "
                               "not a convenience — see the module docstring.")
    converge.add_argument("--arm-purge", dest="arm_purge", action="store_true",
                          help="EXECUTE approved `purge` plans. Without this a purge reports "
                               "what it would do and changes nothing. A purge removes the "
                               "checkout and deletes the hosted repository, and there is no "
                               "undo. `sync.py`'s unattended step never passes this.")
    converge.set_defaults(func=cmd_converge)

    reopen = sub.add_parser("reopen", help="clear a plan's completion record on THIS machine "
                                           "so the next converge re-evaluates it")
    reopen.add_argument("plan_id")
    reopen.set_defaults(func=cmd_reopen)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
