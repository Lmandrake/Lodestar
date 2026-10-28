"""`lifecycle._do_purge` driven against REAL throwaway repos, plus the whole CLI path.

Run from the repo root:

    PYTHONPATH=bin python3 -m unittest tests.test_lifecycle_purge

**Nothing here can reach a git host or touch anything outside its own temp directory.**
Every store is redirected (`paths.tombstones_file`,
`paths.lifecycle_transitions_file`, `paths.lifecycle_lock_file`,
`paths.lifecycle_journal_state_file`, `paths.plans_file`, `paths.approvals_file`), the
registry lookup and `machine.key`/`role`/`hosts_table` are patched, and the hosted-repository
deletion goes through an injected recorder that records argv and starts no process. The
`ExecutorTestCase` in `tests/test_lifecycle_executors.py` already established why that
redirection is load-bearing rather than tidy: the executors call the store writers with no
`path` argument, so a test that let them resolve normally would plant a tombstone for a fake
repo id in live state.

🔴 **`TestEndToEndCLI` is the test that matters most.** CLAUDE.md §7 records why: the `tilde()`
-applied-to-a-URL bug *could not* be caught by a fixture, because a fixture hands the value
straight to the executor and never goes through `propose`. A purge's `new` cell is a hosted
repository coordinate, so it is in exactly that family, and the only honest check is to drive
`propose -> approve -> converge --dry-run` against a real `git init` repo.
"""

import inspect
import json
import os
import shutil
import subprocess
import tempfile
import unittest

import lifecycle
import sync
from lib import (machine, paths, plans, purge, registry, satellites,  # noqa: E402
                 tombstones, transitions)

COORD = "github.com/Lmandrake/Throwaway"
DECLARED = "%s@github.com:Lmandrake/Throwaway.%s" % ("git", "git")
OTHER_COORD = "git.private.example/mandrake/SomethingElse"

AUTH_OK = "\n".join([
    "github.com",
    "  ✓ Logged in to github.com account example-user (keyring)",
    "  - Token scopes: 'delete_repo', 'repo'",
    "",
])
AUTH_NO_DELETE = "\n".join([
    "github.com",
    "  ✓ Logged in to github.com account example-user (keyring)",
    "  - Token scopes: 'gist', 'repo'",
    "",
])

REG_HEADER = ("| Name | Path | Branch | Send | Receive | Machines | Id | Former | Remote "
              "| State | Notes |\n"
              "|------|------|--------|------|---------|----------|----|--------|--------"
              "|-------|-------|\n")


class Runner(object):
    """Stands in for every subprocess the purge path would start. Starts none.

    One recorder serves both calls the executor can make — `gh auth status` (read-only) and
    `gh repo delete` (the irreversible one) — because routing them through one seam is what
    makes "no test can delete anything" checkable rather than asserted: `deletes` is empty in
    every test that is not specifically about an armed deletion.
    """

    def __init__(self, auth=AUTH_OK, delete=(0, "", "")):
        self.calls = []
        self.auth = auth
        self.delete = delete

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[:3] == ["gh", "auth", "status"]:
            return 0, self.auth, ""
        if argv[:3] == ["gh", "repo", "delete"]:
            return self.delete
        raise AssertionError("unexpected command in a test: %r" % (argv,))

    @property
    def deletes(self):
        return [argv for argv in self.calls if argv[:3] == ["gh", "repo", "delete"]]


def plan(**over):
    """A purge Plan with every field populated, so a field added later fails loudly."""
    fields = dict(
        plan_id="purge-throwaway-20261001",
        kind="purge",
        repo_id="throwaway",
        machines=("all",),
        old="",
        new=COORD,
        disposition="purge",
        created_at="2026-10-01T09:00:00.000000-07:00",
        created_by="test",
        note="exercising the purge executor",
    )
    fields.update(over)
    return plans.Plan(**fields)


class PurgeTestCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-purge-exec-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dev = os.path.join(self.tmp, "dev")
        self.lodestar = os.path.join(self.dev, "Lodestar")
        os.makedirs(self.lodestar)

        self.tombstones_file = os.path.join(self.tmp, "tombstones.md")
        self.transitions_file = os.path.join(self.tmp, "transitions.json")
        self.registry_file = os.path.join(self.tmp, "registry.md")

        self._patch(paths, "tombstones_file", lambda: self.tombstones_file)
        self._patch(paths, "lifecycle_transitions_file", lambda: self.transitions_file)
        # 🔴 The purge WRITES to `sync/registry.md` now, so an unredirected run would set
        # `State: retired` on a row of the operator's live desired state. Same argument the
        # tombstone and transition redirects above already make, with a larger blast radius.
        self._patch(paths, "registry_file", lambda: self.registry_file)
        self._patch(lifecycle, "declared_remote", lambda repo_id: DECLARED)

        # 🔴 Satellites are followed now, and their roots default to the REAL `~`,
        # `~/dev/Waypoint` and this checkout. Forced onto the fixture for every test in this
        # file — not per test — because a test that merely forgot to pass roots would remove a
        # real skill symlink or a real auto-memory directory off this machine.
        self.home = os.path.join(self.tmp, "home")
        self.waypoint = os.path.join(self.home, "dev", "Waypoint")
        self.board = os.path.join(self.tmp, "board")
        for made in (os.path.join(self.home, ".claude", "skills"),
                     os.path.join(self.home, ".claude", "projects"),
                     os.path.join(self.home, "dev", ".lodestar"),
                     self.waypoint,
                     os.path.join(self.board, "claude", "snapshot")):
            os.makedirs(made)
        real_removals = satellites.removals
        self._patch(satellites, "removals",
                    lambda repo_path, **kw: real_removals(
                        repo_path, home=self.home, repo_root=self.board,
                        waypoint_root=self.waypoint))
        # `purge.protected_paths` resolves the REAL dev root and Lodestar checkout by default.
        # Pointing it at the fixture keeps the protected-path refusal testable without
        # needing a path under the operator's actual home.
        real_local = purge.local_refusals
        self._patch(purge, "local_refusals",
                    lambda repo_id, path, declared, **kw: real_local(
                        repo_id, path, declared,
                        dev_root=self.dev, lodestar_root=self.lodestar))
        self.runner = Runner()

    def _env(self, name, value):
        """Set an env var for the duration of the test. Restores absence, not "".

        Popping vs restoring matters: leaving `LODESTAR_STATE_DIR=""` behind would make
        `paths.state_dir()` fall back correctly but would still be a leaked variable another
        test could read.
        """
        original = os.environ.get(name)
        os.environ[name] = value
        self.addCleanup(lambda: os.environ.__setitem__(name, original)
                        if original is not None else os.environ.pop(name, None))

    def _patch(self, module, name, value):
        original = getattr(module, name)
        setattr(module, name, value)
        self.addCleanup(setattr, module, name, original)

    def repo(self, name="Throwaway"):
        path = os.path.join(self.dev, name)
        os.makedirs(path)
        subprocess.run(["git", "init", "--quiet", "-b", "main", path], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "-C", path, "remote", "add", "origin", DECLARED], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return path

    def row(self, path, state="active"):
        """Write a REAL one-row registry declaring `path` for `throwaway`, return its Entry.

        🔴 A hand-built `registry.Entry` is no longer enough. `_purge_declaration_step` writes
        `State: retired` into the row, and `registry.set_state` refuses an id no row carries —
        correctly, since inventing a row is exactly what it must not do. So a synthetic Entry
        made every armed purge refuse with "no registry row has Id 'throwaway'", which reads
        like a code failure and is really a fixture that never existed on disk.
        """
        with open(self.registry_file, "w", encoding="utf-8") as handle:
            handle.write("# Registry\n\nprose above the table\n\n" + REG_HEADER
                         + "| Throwaway | %s | main | on | on |  | throwaway |  | %s | %s | a "
                           "note |\n" % (path, DECLARED, state))
        for entry in registry.entries(self.registry_file):
            if entry.repo_id == "throwaway":
                return entry
        raise AssertionError("the fixture registry did not parse back a throwaway row")

    def registry_state(self, repo_id="throwaway"):
        """What the registry file on disk now declares, read back through the real parser."""
        for entry in registry.entries(self.registry_file):
            if entry.repo_id == repo_id:
                return entry.state
        return None

    def run_purge(self, repo_path, *, row=None, dry_run=False, armed=False, the_plan=None):
        return lifecycle._do_purge(
            the_plan or plan(old=repo_path),
            row if row is not None else self.row(repo_path),
            repo_path, dry_run, armed, [], runner=self.runner)

    def blob(self, lines):
        return "\n".join(lines)


class DisarmedByDefaultTest(PurgeTestCase):
    """🔴 The live destructive path ships DISARMED. This is the whole safety requirement.

    Lukas will arm it himself after review, so a `converge` that happened to reach a purge
    plan — including every hourly unattended sweep on three machines — must change nothing.
    """

    def test_a_plain_converge_removes_nothing_deletes_nothing_and_tombstones_nothing(self):
        repo = self.repo()

        status, lines = self.run_purge(repo)

        self.assertEqual("disarmed", status, self.blob(lines))
        self.assertTrue(os.path.isdir(os.path.join(repo, ".git")),
                        "the checkout must still be there")
        self.assertEqual([], self.runner.deletes, "no deletion may be sent to any host")
        self.assertFalse(os.path.exists(self.tombstones_file),
                         "a disarmed run must not record a retirement either")
        self.assertFalse(os.path.exists(self.transitions_file))

    def test_the_disarmed_report_names_the_flag_that_arms_it(self):
        """A refusal that does not say how to proceed is a dead end, and the operator would
        then go looking for a bug instead of a flag."""
        status, lines = self.run_purge(self.repo())

        self.assertIn("--arm-purge", self.blob(lines))

    def test_disarmed_exits_NON_ZERO_so_the_sweep_does_not_report_a_clean_run(self):
        """An approved destructive plan sitting unexecuted is an open obligation.

        `partial` is in this set for the measured reason in LESSONS.md — the sweep reported
        SYNCED while a capability was silently missing. `disarmed` is here for the sibling
        reason: a purge that is approved and has not happened is a thing a human must still
        decide about, and a zero exit would make it vanish from `SYNC.md`.
        """
        self.assertIn("disarmed", lifecycle.NONZERO_STATUSES)

    def test_a_PREVIEW_is_not_in_the_nonzero_set(self):
        """A dry run asked nothing of the world, so it must not flag the sweep."""
        for status in ("would-converge", "would-be-disarmed", "would-be-partial"):
            self.assertNotIn(status, lifecycle.NONZERO_STATUSES)

    def test_the_unattended_sweep_arms_ONLY_UNDER_THE_DECLARATION(self):
        """🔴 `sync.lifecycle_step` is the caller that runs with nobody present.

        **This test replaces one that asserted the flag was ABSENT here, and the replacement is
        deliberate rather than a regression.** That test was right for the design it guarded;
        Lukas reversed the premise on 2026-10-01 — *"I don't like this. I want it to work. Make
        it work."* — because interactive-only arming meant the home WSL box could never finish a
        fleet-wide purge, leaving exactly the permanently-half-applied state this whole loop
        exists to end.

        What is asserted now is STRICTLY STRONGER than "never passes it": the flag must be
        passed only inside a `purge.sweep_may_arm()` test. An unconditional append would satisfy
        the old test's opposite and is caught here.

        Comments and docstrings are stripped first, because a test can pass on a comment — that
        exact defect is recorded in CLAUDE.md §7, and the comment in this very function names
        `--arm-purge` several times while explaining it.
        """
        source = inspect.getsource(sync.lifecycle_step)
        code = "\n".join(line.split("#", 1)[0] for line in source.split("\n"))
        code = code.replace('"""', "\x00")
        body = code.split("\x00")[-1] if "\x00" in code else code

        self.assertIn("--arm-purge", body, "the sweep can no longer arm at all")
        self.assertIn("purge.sweep_may_arm()", body,
                      "the arming flag is not gated by the declaration")
        # The gate must come BEFORE the append, or it gates nothing.
        self.assertLess(body.index("purge.sweep_may_arm()"), body.index('"--arm-purge"'),
                        "the flag is appended before the declaration is consulted")
        # And it must never reach the argv unconditionally.
        for line in body.split("\n"):
            if '"--arm-purge"' in line:
                self.assertIn("append", line,
                              "--arm-purge reaches argv other than by a guarded append: %r"
                              % line)


class PreviewHonestyTest(PurgeTestCase):
    """🔴 A dry run must never be more optimistic than the run it previews.

    LESSONS.md records the measured case: `--dry-run` reported `would-converge` where the real
    run would report `partial`, because an unfollowable satellite was already known. The same
    trap has a new shape here — the arming flag means there are two different "real runs", and
    a preview must name the one you will actually get.
    """

    def test_a_dry_run_WITHOUT_arming_previews_the_DISARMED_run(self):
        status, lines = self.run_purge(self.repo(), dry_run=True, armed=False)

        self.assertEqual("would-be-disarmed", status, self.blob(lines))
        self.assertIn("--arm-purge", self.blob(lines))

    def test_a_dry_run_WITH_arming_previews_the_armed_run_and_still_changes_nothing(self):
        repo = self.repo()

        status, lines = self.run_purge(repo, dry_run=True, armed=True)

        self.assertEqual("would-converge", status, self.blob(lines))
        self.assertTrue(os.path.isdir(repo), "a dry run must not remove anything")
        self.assertEqual([], self.runner.deletes, "a dry run must send no deletion")
        self.assertFalse(os.path.exists(self.tombstones_file),
                         "a dry run must write NO record — not even the tombstone")

    def test_the_preview_names_the_exact_command_it_would_run(self):
        """"Prepared and reported, not invoked": the operator reads the argv before arming."""
        _, lines = self.run_purge(self.repo(), dry_run=True, armed=True)

        self.assertIn(" ".join(purge.delete_argv(purge.parse_coordinate(COORD))),
                      self.blob(lines))

    def test_the_preview_reports_the_local_material_that_exists_nowhere_else(self):
        """The number a human needs in front of them before arming an act with no undo."""
        repo = self.repo()
        with open(os.path.join(repo, "draft.txt"), "w", encoding="utf-8") as fh:
            fh.write("never committed")

        _, lines = self.run_purge(repo, dry_run=True, armed=True)

        self.assertIn("1 uncommitted path(s)", self.blob(lines))


class TombstoneOrderingTest(PurgeTestCase):
    """🔴 The tombstone goes down BEFORE anything is removed, and this proves the order.

    A tombstone appended afterwards is lost precisely when it matters most: a removal that
    half-fails would leave an id with no retirement recorded, and the next sweep's
    `clone_step` would re-clone the repository it was told to remove. Asserting both exist at
    the end cannot tell the two orders apart, so the removal itself reports what it saw.
    """

    def test_the_tombstone_already_EXISTS_when_the_removal_is_called(self):
        repo = self.repo()
        seen = {}
        real_remove = purge.remove_tree

        def watching(path, **kw):
            seen["tombstone_present"] = os.path.exists(self.tombstones_file)
            seen["id_retired"] = tombstones.is_retired("throwaway", self.tombstones_file)
            return real_remove(path, **kw)

        self._patch(purge, "remove_tree", watching)

        status, lines = self.run_purge(repo, armed=True)

        self.assertEqual("converged", status, self.blob(lines))
        self.assertTrue(seen.get("tombstone_present"),
                        "the tombstone file must exist before the removal runs")
        self.assertTrue(seen.get("id_retired"),
                        "and it must already name this id, not merely exist")

    def test_a_FAILED_removal_still_leaves_the_retirement_recorded(self):
        """The half-failure the ordering exists for. Nothing may re-clone this id afterwards."""
        repo = self.repo()
        self._patch(purge, "remove_tree",
                    lambda path, **kw: {"ok": False, "removed": False, "armed": True,
                                        "reason": "removing %s failed: pretend EACCES" % path})

        status, lines = self.run_purge(repo, armed=True)

        self.assertEqual("partial", status, self.blob(lines))
        self.assertTrue(tombstones.is_retired("throwaway", self.tombstones_file))
        self.assertIn("still there", self.blob(lines))

    def test_a_refused_purge_writes_NO_tombstone(self):
        """Every refusal is evaluated before the tombstone, so a purge that was never going
        to act does not retire an id as a side effect."""
        repo = self.repo()

        status, lines = self.run_purge(
            repo, the_plan=plan(old=repo, new=OTHER_COORD))

        self.assertEqual("refused", status, self.blob(lines))
        self.assertFalse(os.path.exists(self.tombstones_file))


class ArmedExecutionTest(PurgeTestCase):

    def test_an_armed_purge_removes_the_checkout_and_sends_exactly_one_deletion(self):
        repo = self.repo()

        status, lines = self.run_purge(repo, armed=True)

        self.assertEqual("converged", status, self.blob(lines))
        self.assertFalse(os.path.exists(repo))
        self.assertEqual([purge.delete_argv(purge.parse_coordinate(COORD))],
                         self.runner.deletes)
        self.assertTrue(tombstones.is_retired("throwaway", self.tombstones_file))

    def test_NO_transition_record_is_written_and_that_is_deliberate(self):
        """🔴 Two measured reasons, both of which would wedge the repo.

        `transitions` is keyed by **repo_id**, so a purge record would clobber another plan's
        — measured 2026-10-01, where one ordinary failure left an unfinished record and every
        later plan reported `stop` until a person cleared it. And `observe(path, "")` after a
        completed purge reports `NEITHER`, which `recovery_action` correctly turns into
        `consult-holding`: a `stop` that would block every retry of the remote half. The
        tombstone is the durable record, it is cross-machine, and it goes down first.
        """
        repo = self.repo()

        self.run_purge(repo, armed=True)

        self.assertFalse(os.path.exists(self.transitions_file))
        self.assertEqual({}, transitions.load(self.transitions_file))

    def test_a_failed_remote_deletion_is_PARTIAL_and_carries_gh_s_own_error(self):
        """Local gone, hosted repo still up. Reported as partial so it flags every sweep."""
        self.runner.delete = (1, "", "HTTP 403: Must have admin rights to Repository.")
        repo = self.repo()

        status, lines = self.run_purge(repo, armed=True)

        self.assertEqual("partial", status, self.blob(lines))
        self.assertFalse(os.path.exists(repo), "the local half still happened")
        self.assertIn("admin rights", self.blob(lines))
        self.assertIn("partial", lifecycle.NONZERO_STATUSES)

    def test_an_already_absent_hosted_repo_converges_because_a_fleet_purge_runs_twice(self):
        """The second machine finds the repository already gone. That is the end state."""
        self.runner.delete = (1, "", "HTTP 404: Not Found")
        repo = self.repo()

        status, lines = self.run_purge(repo, armed=True)

        self.assertEqual("converged", status, self.blob(lines))
        self.assertIn("already absent", self.blob(lines))

    def test_every_report_carries_the_scope_and_what_was_NOT_touched(self):
        """🔴 The claim must be the same size as the act, armed or not.

        Satellites are real leftovers and outside the scope Lukas set, so saying nothing about
        them would be the overclaim this whole module is written against.
        """
        for armed in (False, True):
            runner = Runner()
            self.runner = runner
            repo = self.repo("Throwaway-%s" % armed)
            _, lines = lifecycle._do_purge(
                plan(old=repo), self.row(repo), repo, False, armed, [], runner=runner)
            blob = self.blob(lines)
            self.assertIn(purge.SCOPE, blob)
            self.assertIn("skill symlinks", blob)
            self.assertIn("outside the scope", blob)

    def test_the_purge_SETS_THE_REGISTRY_ROW_TO_RETIRED_itself(self):
        """🔴 Lukas, 2026-10-01. This test used to assert the OPPOSITE and is the replacement.

        Until today a purge left the row reading `active` and printed a sentence telling a
        human to go and edit it, so `validate` reported `active-but-retired` — a contradiction
        in shared desired state, on every sweep, for a repository that no longer existed. His
        ruling: the executor writes the declaration, as a rehome now does, because the spec's
        own `Retired` postcondition is part of what a purge *means* rather than a follow-up
        chore.
        """
        repo = self.repo()

        status, lines = self.run_purge(repo, armed=True)

        self.assertEqual("converged", status, self.blob(lines))
        self.assertEqual("retired", self.registry_state())
        self.assertNotIn("still reads active", self.blob(lines),
                         "the sentence telling a human to go and edit the row is gone")

    def test_the_row_is_RETIRED_and_NOT_DELETED(self):
        """§3E: absence of a row never authorises removal, so the row must survive.

        A deleted row reads identically to a repository nobody ever registered — it would
        destroy the only evidence that this id was managed and then deliberately retired, and
        `validate`'s resurrection check would have nothing to compare a future row against.
        """
        repo = self.repo()

        self.run_purge(repo, armed=True)

        rows = registry.entries(self.registry_file)
        self.assertEqual(1, len(rows), "the row must still be there")
        self.assertEqual("throwaway", rows[0].repo_id)
        self.assertEqual(repo, rows[0].path, "only the State cell may have moved")
        self.assertEqual("retired", rows[0].state)

    def test_a_SECOND_machine_purging_the_same_id_is_a_no_op_SUCCESS(self):
        """🔴 Idempotence is what makes a fleet-wide purge finishable at all.

        A purge plan named `all` executes on every machine. If the second machine to converge
        read an already-`retired` cell as a failed write, the plan would report `partial` for
        ever on every machine except whichever happened to run first — permanently
        half-applied, which is the exact condition this whole loop exists to end. Two peers
        writing the identical value into one cell are making the same statement, so the second
        has nothing to add.
        """
        repo = self.repo()
        row = self.row(repo, state="retired")
        before = open(self.registry_file, encoding="utf-8").read()

        status, lines = self.run_purge(repo, row=row, armed=True)

        self.assertEqual("converged", status, self.blob(lines))
        self.assertIn("already reads State: retired", self.blob(lines))
        self.assertEqual(before, open(self.registry_file, encoding="utf-8").read(),
                         "an idempotent write must not churn a file three machines merge")

    def test_a_row_read_BEFORE_a_peers_write_still_converges(self):
        """🔴 The idempotence that actually carries the fleet, and the first version of this
        test did NOT reach it.

        `_purge_declaration_step` has its own cheap pre-check on the row object
        `_converge_one` already resolved, so a test whose row says `retired` never calls
        `registry.set_state` at all — mutating `set_state`'s no-op branch to a refusal left
        that test green, which is how the gap was found rather than reasoned about.

        The case that matters is the one the pre-check cannot see: this machine read the row,
        a peer's commit landed, and the cell on disk now says `retired` while the Entry in
        memory still says `active`. That is not a race to prevent — it is the normal shape of
        a plan named `all` converging on several machines — so the write itself has to treat
        an already-correct cell as a no-op success, or the second machine reports `partial`
        for ever.
        """
        repo = self.repo()
        stale = self.row(repo, state="active")
        self.row(repo, state="retired")  # a peer's write lands on disk underneath us

        status, lines = self.run_purge(repo, row=stale, armed=True)

        self.assertEqual("converged", status, self.blob(lines))
        self.assertEqual("retired", self.registry_state())

    def test_a_FAILED_registry_write_REFUSES_before_anything_is_destroyed(self):
        """🔴 The ordering argument, exercised: the declaration runs FIRST and failing it
        leaves NOTHING irreversible behind.

        Written last instead, a successful removal followed by a failed write re-creates the
        contradiction this change deletes — row `active`, repository gone, unattended. Written
        first, the failure costs a retry and nothing else: no tombstone (terminal,
        append-only), no removal, no deletion sent to the host.
        """
        repo = self.repo()
        self._patch(registry, "set_state",
                    lambda repo_id, state, path=None: {
                        "ok": False, "changed": False, "reason": "simulated write failure"})

        status, lines = self.run_purge(repo, armed=True)

        self.assertEqual("refused", status, self.blob(lines))
        self.assertTrue(os.path.isdir(repo), "the checkout must be untouched")
        self.assertEqual({}, tombstones.load(self.tombstones_file),
                         "no terminal record may exist after a refusal at step one")
        self.assertEqual([], self.runner.deletes)
        self.assertIn("simulated write failure", self.blob(lines),
                      "the refusal must carry the underlying reason, not a fixed sentence")


class SatelliteRemovalTest(PurgeTestCase):
    """🔴 A purge FOLLOWS and REMOVES its satellites now — Lukas, 2026-10-01.

    Before today `_PURGE_OUT_OF_SCOPE` named the four kinds in prose and checked none of them,
    so a skill symlink into the removed checkout was left dangling and an auto-memory directory
    was orphaned with nothing saying which ones existed. His ruling: follow and remove them, as
    a rename already does.

    Every root here is the fixture's — `PurgeTestCase.setUp` forces it for the whole file, so
    no test in this class can reach the real `~/.claude/` even by forgetting to.
    """

    def make_skill_symlink(self, name, target, created=True):
        link = os.path.join(self.home, ".claude", "skills", name)
        os.symlink(target, link)
        state = os.path.join(self.home, "dev", ".lodestar", "skill-links.json")
        with open(state, "w", encoding="utf-8") as handle:
            json.dump({"created": [name] if created else []}, handle)
        return link

    def make_project_memory(self, repo_path):
        path = os.path.join(self.home, ".claude", "projects",
                            satellites.project_memory_name(repo_path))
        os.makedirs(path)
        with open(os.path.join(path, "MEMORY.md"), "w", encoding="utf-8") as handle:
            handle.write("notes that outlive the repo\n")
        return path

    def make_waypoint_row(self, repo_path):
        path = os.path.join(self.waypoint, "waypoints.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("| Name | Path | Added | Status | Notes | Machine |\n"
                         "|------|------|-------|--------|-------|---------|\n"
                         "| THROWAWAY | %s | 2026-09-30 | active |  |  |\n" % repo_path)
        return path

    def test_an_armed_purge_removes_the_symlink_and_the_auto_memory_directory(self):
        repo = self.repo()
        link = self.make_skill_symlink("throwaway", repo)
        memory = self.make_project_memory(repo)

        status, lines = self.run_purge(repo, armed=True)

        self.assertEqual("converged", status, self.blob(lines))
        self.assertFalse(os.path.lexists(link), "a dangling symlink is the thing this fixes")
        self.assertFalse(os.path.exists(memory))

    def test_the_report_NAMES_every_satellite_it_removed(self):
        """🔴 He accepted that this removes an auto-memory directory which can hold notes
        outliving the repository.

        An accepted loss and a silent one differ in exactly one thing: whether the report says
        which files went. So the per-satellite line is not verbosity, it is what makes the
        acceptance mean anything after the fact.
        """
        repo = self.repo()
        link = self.make_skill_symlink("throwaway", repo)
        memory = self.make_project_memory(repo)

        _, lines = self.run_purge(repo, armed=True)
        blob = self.blob(lines)

        self.assertIn(link, blob)
        self.assertIn(memory, blob)
        self.assertIn("satellites: 2 found, 2 removed, 0 need a person", blob)

    def test_a_WAYPOINT_row_makes_the_purge_PARTIAL_and_is_named_not_edited(self):
        """Waypoint's tables are hand-edited and per-row machine-scoped, so there is no safe
        mechanical edit — and a dangling shortcut is a real leftover. `partial` keeps flagging
        until a person removes the row, which is the intent rather than noise."""
        repo = self.repo()
        waypoints = self.make_waypoint_row(repo)
        before = open(waypoints, encoding="utf-8").read()

        status, lines = self.run_purge(repo, armed=True)

        self.assertEqual("partial", status, self.blob(lines))
        self.assertIn("need a person", self.blob(lines))
        self.assertIn("THROWAWAY", self.blob(lines))
        self.assertEqual(before, open(waypoints, encoding="utf-8").read(),
                         "the row must be reported, never rewritten")

    def test_a_HAND_MADE_skill_symlink_SURVIVES_and_makes_the_purge_PARTIAL(self):
        """Provenance gates removal. A link a human wired up by hand is theirs, and the purge
        reports it rather than deciding on their behalf."""
        repo = self.repo()
        link = self.make_skill_symlink("theirs", repo, created=False)

        status, lines = self.run_purge(repo, armed=True)

        self.assertEqual("partial", status, self.blob(lines))
        self.assertTrue(os.path.lexists(link), "someone else's link must survive a purge")

    def test_a_DISARMED_run_removes_no_satellite_at_all(self):
        repo = self.repo()
        link = self.make_skill_symlink("throwaway", repo)
        memory = self.make_project_memory(repo)

        status, lines = self.run_purge(repo, armed=False)

        self.assertEqual("disarmed", status, self.blob(lines))
        self.assertTrue(os.path.lexists(link))
        self.assertTrue(os.path.isdir(memory))
        self.assertIn("would be removed", self.blob(lines))

    def test_an_ARMED_DRY_RUN_reports_would_be_partial_when_one_cannot_be_FOLLOWED(self):
        """🔴 A preview must never be the more optimistic of the two runs.

        LESSONS.md records the measured case: mapping an unresolved satellite to a clean status
        made it vanish from the report entirely. The unfollowable satellite is already known
        here — identifying it needed no write — so answering `would-converge` would promise an
        outcome the next real run will not deliver.
        """
        repo = self.repo()
        self.make_waypoint_row(repo)

        status, lines = self.run_purge(repo, dry_run=True, armed=True)

        self.assertEqual("would-be-partial", status, self.blob(lines))
        self.assertIn("will report PARTIAL", self.blob(lines))
        self.assertTrue(os.path.isdir(repo), "a preview mutates nothing")

    def test_an_ARMED_DRY_RUN_with_nothing_unfollowable_still_says_would_converge(self):
        """The other direction matters too: inventing a fault a real run will not report is the
        same dishonesty pointing the other way."""
        repo = self.repo()
        self.make_skill_symlink("throwaway", repo)
        self.make_project_memory(repo)

        status, lines = self.run_purge(repo, dry_run=True, armed=True)

        self.assertEqual("would-converge", status, self.blob(lines))

    def test_satellites_are_removed_AFTER_the_checkout_and_only_once_it_is_GONE(self):
        """🔴 The ordering, proved by having the remover report what it saw.

        A skill symlink into a checkout that still exists is a WORKING capability and an
        auto-memory directory beside a live repo is live notes. Removing either first and then
        failing the checkout removal would destroy something useful for a repository that is
        still there — a worse outcome than the failure it was trying to complete.
        """
        repo = self.repo()
        self.make_skill_symlink("throwaway", repo)
        seen = []
        real_remove = satellites.remove
        self._patch(satellites, "remove",
                    lambda sat, **kw: (seen.append(os.path.exists(repo))
                                       or real_remove(sat, **kw)))

        status, lines = self.run_purge(repo, armed=True)

        self.assertEqual("converged", status, self.blob(lines))
        self.assertEqual([False], seen,
                         "the checkout must already be gone when a satellite is removed")

    def test_a_FAILED_checkout_removal_leaves_the_satellites_ALONE(self):
        repo = self.repo()
        link = self.make_skill_symlink("throwaway", repo)
        memory = self.make_project_memory(repo)
        self._patch(purge, "remove_tree",
                    lambda path, **kw: {"ok": False, "removed": False, "armed": True,
                                        "reason": "simulated removal failure"})

        status, lines = self.run_purge(repo, armed=True)

        self.assertEqual("partial", status, self.blob(lines))
        self.assertTrue(os.path.lexists(link))
        self.assertTrue(os.path.isdir(memory))
        self.assertIn("satellites: NOT touched", self.blob(lines))

    def test_a_machine_that_never_held_the_checkout_still_removes_its_satellites(self):
        """Absence at the declared path is this machine's share of the removal already done —
        but an auto-memory directory from before can still be sitting there, and nothing else
        will ever come back for it."""
        gone = os.path.join(self.dev, "GoneAlready")
        memory = self.make_project_memory(gone)

        status, lines = self.run_purge(gone, armed=True)

        self.assertEqual("converged", status, self.blob(lines))
        self.assertFalse(os.path.exists(memory))

    def test_the_scope_sentence_GREW_with_the_act(self):
        """🔴 Understating an act is the same defect as overclaiming, pointing the other way.

        `purge.SCOPE` rides on every report and now has to name the satellites as in scope,
        while keeping Waypoint rows, backups and snapshots out — and `test_purge`'s
        `HonestScopeTest` independently forbids any affirmative claim of unrecoverability in
        the same sentence.
        """
        lowered = purge.SCOPE.lower()

        for inside in ("skill symlinks", "auto-memory", "snapshot-memory"):
            self.assertIn(inside, lowered)
        for outside in ("backups", "waypoint shortcut rows", "quarantine"):
            self.assertIn(outside, lowered)
        self.assertIn("not erasure", lowered)


class RefusalTest(PurgeTestCase):

    def test_a_target_that_is_not_the_registrys_declared_remote_is_REFUSED(self):
        """🔴 The guard against a typo deleting a repository nobody meant to touch.

        `gh repo delete` does not care whether the repository is yours by intent, and a plan is
        hand-edited markdown. Refusing rather than preferring one side also handles the live
        collision CLAUDE.md §7 records: a rehome leaves the declared `Remote` stale, so the
        two can legitimately disagree and a human must say which is right.
        """
        repo = self.repo()

        status, lines = self.run_purge(repo, the_plan=plan(old=repo, new=OTHER_COORD))

        self.assertEqual("refused", status)
        self.assertTrue(os.path.isdir(repo))
        self.assertEqual([], self.runner.deletes)
        self.assertIn("different repositories", self.blob(lines))

    def test_a_path_the_registry_does_NOT_declare_is_REFUSED(self):
        repo = self.repo()
        elsewhere = self.repo("Elsewhere")

        status, lines = self.run_purge(repo, row=self.row(elsewhere))

        self.assertEqual("refused", status)
        self.assertTrue(os.path.isdir(repo))
        self.assertIn("registry declares", self.blob(lines))

    def test_a_PROTECTED_id_is_REFUSED_even_with_a_perfectly_formed_plan(self):
        """§3G. Removing the control channel prevents every later repair."""
        repo = self.repo()

        status, lines = self.run_purge(
            repo, row=registry.Entry(name="Lodestar", path=repo, branch="main", sync=True,
                                     notes="", repo_id="lodestar", remote=DECLARED),
            the_plan=plan(repo_id="lodestar", old=repo))

        self.assertEqual("refused", status)
        self.assertTrue(os.path.isdir(repo))
        self.assertIn("control channel", self.blob(lines))

    def test_a_missing_delete_repo_SCOPE_refuses_BEFORE_the_local_copy_is_spent(self):
        """🔴 Measured condition, not a hypothetical.

        `gh auth status` on this Desktop, 2026-10-01: the github.com token carries
        `delete_repo`, the git.private.example one does not. This machine has a route to the
        host, so the remote half is its job — and if it provably cannot do it, removing the
        local checkout first would destroy the only copy you can lose and leave the one
        irreversible step undone. Refusing hands the remedy back to the account holder, whose
        interactive `gh auth refresh` it is.
        """
        self.runner.auth = AUTH_NO_DELETE
        repo = self.repo()

        status, lines = self.run_purge(repo, armed=True)

        self.assertEqual("refused", status, self.blob(lines))
        self.assertTrue(os.path.isdir(repo), "nothing may be spent on a deletion that fails")
        self.assertFalse(os.path.exists(self.tombstones_file))
        self.assertEqual([], self.runner.deletes)
        self.assertIn("gh auth refresh", self.blob(lines))

    def test_an_UNREADABLE_auth_status_does_NOT_manufacture_a_refusal(self):
        """Unmeasured is not absent. A refusal built out of ignorance blocks a working path.

        `can_delete_on` returns three answers for exactly this reason, and the executor acts
        only on the positive absence.
        """
        self.runner.auth = "gh: could not read keyring"
        repo = self.repo()

        status, lines = self.run_purge(repo, armed=True)

        self.assertEqual("converged", status, self.blob(lines))
        self.assertIn("UNMEASURED", self.blob(lines))

    def test_a_purge_DISPOSITION_on_another_kind_is_still_refused(self):
        """Unchanged from before purge existed, and the reason is unchanged: renaming a rescue
        directory removes nothing, so a rename claiming to is claiming a property it cannot
        deliver."""
        repo = self.repo()
        self._patch(lifecycle, "_row_for", lambda repo_id: self.row(repo))
        self._patch(machine, "key", lambda: "mt-001")
        self._patch(machine, "role", lambda: "desktop")

        status, lines = lifecycle._converge_one(
            plan(kind="rename", old=repo, new=os.path.join(self.dev, "New"),
                 disposition="purge"), False)

        self.assertEqual("refused", status)
        self.assertIn("renaming a rescue directory", self.blob(lines))


class AlreadyRemovedTest(PurgeTestCase):
    """A purge aimed at every machine must converge where the directory was never present."""

    def test_absence_at_the_declared_path_converges_and_still_deletes_the_hosted_repo(self):
        gone = os.path.join(self.dev, "GoneAlready")

        status, lines = self.run_purge(gone, armed=True)

        self.assertEqual("converged", status, self.blob(lines))
        self.assertIn("already absent", self.blob(lines))
        self.assertEqual([purge.delete_argv(purge.parse_coordinate(COORD))],
                         self.runner.deletes,
                         "the remote half is owed even where the checkout was never here")

    def test_absence_at_a_path_the_registry_does_not_declare_is_still_refused(self):
        typo = os.path.join(self.dev, "Gnoe")
        declared = os.path.join(self.dev, "Gone")

        status, lines = self.run_purge(typo, row=self.row(declared), armed=True)

        self.assertEqual("refused", status, self.blob(lines))
        self.assertEqual([], self.runner.deletes)

    def test_no_registry_row_is_OMITTED_rather_than_removed(self):
        """§3E, verbatim: absence of a row never authorises removal. Checked through
        `_converge_one`, because that is where the branch lives."""
        self._patch(lifecycle, "_row_for", lambda repo_id: None)
        self._patch(machine, "key", lambda: "mt-001")
        self._patch(machine, "role", lambda: "desktop")

        status, lines = lifecycle._converge_one(plan(old=self.repo()), False, armed=True)

        self.assertEqual("omitted", status)
        self.assertIn("never authorises removal", self.blob(lines))


class UnreachableHostTest(PurgeTestCase):
    """An unreachable host is deliberate non-participation, never a fault.

    tower declares `github.com` only and has no connectivity to the private host at all
    (design §3B). Reporting `partial` there would flag on every sweep with no resolution
    available on that machine — the eternal-flag failure mode, arriving by a new route. The
    gap that WOULD be invisible per machine is caught at the origin instead, by
    `purge_fleet_warnings`.
    """

    def test_a_machine_with_no_route_converges_locally_and_sends_nothing(self):
        self._patch(purge, "host_reachable", lambda coord, *a, **kw: False)
        repo = self.repo()

        status, lines = self.run_purge(repo, armed=True)

        self.assertEqual("converged", status, self.blob(lines))
        self.assertFalse(os.path.exists(repo), "the local half is still this machine's job")
        self.assertEqual([], self.runner.calls,
                         "no route means not even the read-only auth probe is run")
        self.assertIn("owed to a machine", self.blob(lines))

    def test_the_ORIGIN_warns_when_NO_named_machine_can_reach_the_host(self):
        """🔴 The failure every per-machine line would hide.

        Each machine would truthfully say the remote half is owed elsewhere, the plan would
        converge everywhere, and the repository would stay up. §3C makes the origin print the
        fleet-wide consequence precisely so a gap that is invisible per machine is visible
        once.
        """
        self._patch(lifecycle, "fleet", lambda: [("tower", "home")])
        self._patch(machine, "hosts_table", lambda path=None: {"tower": ["github.com"]})

        warnings = lifecycle.purge_fleet_warnings(plan(new=OTHER_COORD))

        self.assertEqual(1, len(warnings), warnings)
        self.assertIn("NOT be deleted by any of them", warnings[0])

    def test_no_warning_when_at_least_one_named_machine_has_a_route(self):
        self._patch(lifecycle, "fleet", lambda: [("tower", "home"), ("mt-001", "desktop")])
        self._patch(machine, "hosts_table",
                    lambda path=None: {"tower": ["github.com"],
                                       "mt-001": ["github.com", "git.private.example"]})

        self.assertEqual([], lifecycle.purge_fleet_warnings(plan(new=OTHER_COORD)))


class TestEndToEndCLI(unittest.TestCase):
    """🔴 propose -> approve -> converge --dry-run, through `main`, on a real `git init` repo.

    CLAUDE.md §7 is explicit that a fixture cannot reach the bugs in this path: the
    `tilde()`-applied-to-a-URL defect was invisible to every unit test because a fixture hands
    the value straight to the executor and never goes through `propose`. A purge's `new` cell
    is a hosted-repository coordinate, so it is the same shape of field, and only the CLI path
    can prove it survives authoring intact.

    `--dry-run` without `--arm-purge`, so this test is incapable of removing or deleting
    anything even if every other guard were wrong.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-purge-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dev = os.path.join(self.tmp, "dev")
        os.makedirs(self.dev)
        self.repo = os.path.join(self.dev, "Throwaway")
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "--quiet", "-b", "main", self.repo], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        self.plans_file = os.path.join(self.tmp, "plans.md")
        self.approvals_file = os.path.join(self.tmp, "approvals.md")
        self.tombstones_file = os.path.join(self.tmp, "tombstones.md")
        self.transitions_file = os.path.join(self.tmp, "transitions.json")

        # 🔴 One knob rather than a list. These patches name the machine-local stores one at a
        # time, and on 2026-10-01 the set was missing `lifecycle_completions_file` -- which did
        # not exist when they were written -- so a real `converge` here wrote the fixture plan
        # `purge-throwaway-20261001` into the LIVE store on the Desktop, and the committed fleet
        # receipt then claimed this machine had converged a purge that never happened.
        # Redirecting the whole state directory covers every store added later, including ones
        # nobody writing this test can know about. The explicit patches stay: they are narrower,
        # they document what this test actually touches, and belt-and-braces is cheap here.
        self._env("LODESTAR_STATE_DIR", os.path.join(self.tmp, "state"))
        # 🔴 And the BOARD, separately -- there are two roots and this test needed both. The
        # completion RECEIPT is root-relative (`sync/lifecycle-receipts/<key>.json`), not
        # machine-local, so redirecting only the state directory still let a real `converge`
        # here rewrite the repo's COMMITTED receipt, leaving it holding one fabricated plan.
        # Measured 2026-10-01, after the state-dir fix had already landed -- fixing one root
        # made the other root's hole the whole of the remaining bug.
        self._env("LODESTAR_ROOT", os.path.join(self.tmp, "board"))
        os.makedirs(os.path.join(self.tmp, "board", "sync"), exist_ok=True)

        self._patch(paths, "plans_file", lambda: self.plans_file)
        self._patch(paths, "approvals_file", lambda: self.approvals_file)
        self._patch(paths, "tombstones_file", lambda: self.tombstones_file)
        self._patch(paths, "lifecycle_transitions_file", lambda: self.transitions_file)
        self._patch(paths, "lifecycle_journal_state_file",
                    lambda: os.path.join(self.tmp, "journal-state.json"))
        self._patch(paths, "lifecycle_lock_file",
                    lambda repo_id: os.path.join(self.tmp, "lock-%s.json" % repo_id))

        # 🔴 A REAL registry file, redirected. `_purge_declaration_step` writes `State:
        # retired` through `registry.set_state`, which resolves `paths.registry_file()` — so
        # with only a synthetic Entry and `_row_for` patched, an armed run here would have
        # aimed a write at the operator's live `sync/registry.md`. It refused (no live row
        # carries this fake id), which is the guard working, but a near miss is not a design.
        self.registry_file = os.path.join(self.tmp, "registry.md")
        with open(self.registry_file, "w", encoding="utf-8") as handle:
            handle.write("# Registry\n\nprose\n\n" + REG_HEADER
                         + "| Throwaway | %s | main | on | on |  | throwaway |  | %s | active "
                           "| a note |\n" % (self.repo, DECLARED))
        self._patch(paths, "registry_file", lambda: self.registry_file)
        self.row = [e for e in registry.entries(self.registry_file)
                    if e.repo_id == "throwaway"][0]
        self._patch(lifecycle, "_row_for", lambda repo_id: self.row)

        # Satellite roots forced onto the fixture, for the same reason PurgeTestCase does it:
        # `satellites.removals` defaults to the real `~` and `~/dev/Waypoint`, and the CLI
        # gives a test no seam to pass roots through.
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, ".claude", "skills"))
        os.makedirs(os.path.join(self.home, ".claude", "projects"))
        os.makedirs(os.path.join(self.home, "dev", ".lodestar"))
        real_removals = satellites.removals
        self._patch(satellites, "removals",
                    lambda repo_path, **kw: real_removals(
                        repo_path, home=self.home,
                        repo_root=os.path.join(self.tmp, "no-board"),
                        waypoint_root=os.path.join(self.tmp, "no-waypoint")))
        self._patch(lifecycle, "declared_remote", lambda repo_id: DECLARED)
        self._patch(lifecycle, "managed_paths_here", lambda: [self.repo])
        self._patch(lifecycle, "fleet", lambda: [("mt-001", "desktop")])
        self._patch(lifecycle, "board_freshness",
                    lambda now=None: type("V", (), {"ok": True, "reason": "fixture"})())
        self._patch(lifecycle.validate, "faults", lambda *a, **kw: [])
        self._patch(lifecycle.journal, "append", lambda *a, **kw: None)
        self._patch(machine, "key", lambda: "mt-001")
        self._patch(machine, "role", lambda: "desktop")
        real_local = purge.local_refusals
        self._patch(purge, "local_refusals",
                    lambda repo_id, path, declared, **kw: real_local(
                        repo_id, path, declared, dev_root=self.dev,
                        lodestar_root=os.path.join(self.dev, "Lodestar")))
        # `cmd_converge` takes no `runner`, so without this the scope probe would shell out
        # to the real `gh auth status` and the test would answer differently on a machine
        # that is not logged in. Read-only either way, but a test whose verdict depends on
        # the operator's keyring is not a test.
        self._patch(purge, "can_delete_on",
                    lambda host, runner=None: {"known": True, "ok": True, "scopes": [],
                                               "detail": "fixture: scope present"})
        # 🔴 The two irreversible calls, replaced for the whole class. Even if every guard
        # above were wrong, nothing in this file can remove a directory or reach a host.
        self.removed = []
        self.deleted = []
        self._patch(purge, "remove_tree", self._record_remove)
        self._patch(purge, "delete_remote", self._record_delete)

    def _record_remove(self, path, **kw):
        self.removed.append((path, kw.get("armed", False)))
        return {"ok": True, "removed": True, "armed": kw.get("armed", False),
                "reason": "fixture: removed %s" % path}

    def _record_delete(self, coord, **kw):
        self.deleted.append((coord.slug, kw.get("armed", False)))
        return {"ok": True, "deleted": True, "armed": kw.get("armed", False),
                "argv": purge.delete_argv(coord),
                "reason": "fixture: deleted %s" % coord.slug}

    def _env(self, name, value):
        """Set an env var for the duration of the test. Restores absence, not "".

        Popping vs restoring matters: leaving `LODESTAR_STATE_DIR=""` behind would make
        `paths.state_dir()` fall back correctly but would still be a leaked variable another
        test could read.
        """
        original = os.environ.get(name)
        os.environ[name] = value
        self.addCleanup(lambda: os.environ.__setitem__(name, original)
                        if original is not None else os.environ.pop(name, None))

    def _patch(self, module, name, value):
        original = getattr(module, name)
        setattr(module, name, value)
        self.addCleanup(setattr, module, name, original)

    def cli(self, *argv):
        """Run `lifecycle.main`, keeping its output out of the test runner's screen."""
        import contextlib
        import io
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = lifecycle.main(list(argv))
        self.printed = buffer.getvalue()
        return code

    def propose(self):
        return self.cli("propose", "--kind", "purge", "--id", "throwaway",
                        "--old", self.repo, "--new", COORD,
                        "--machines", "all", "--note", "end-to-end test",
                        # Pinned: the default id embeds today's date, so every assertion below
                        # on "purge-throwaway-20261001" held on that one day only.
                        "--plan-id", "purge-throwaway-20261001")

    def test_the_whole_path_runs_and_the_coordinate_survives_authoring_intact(self):
        """🔴 The property a fixture cannot test: `new` must NOT be path-treated.

        `tilde()` calls `os.path.abspath`, which resolves a non-absolute string against the
        current working directory. Applied to a coordinate it would store
        `<cwd>/github.com/Lmandrake/Throwaway`, which parses as four segments and refuses for
        ever — safe, but permanently unexecutable, which is the condition this loop exists to
        end. `old`, by contrast, MUST be stored `~`-relative or tower's disjoint `/mnt/...`
        namespace observes nothing.
        """
        self.assertEqual(0, self.propose())

        stored = plans.load_plans(self.plans_file)["purge-throwaway-20261001"]
        self.assertEqual(COORD, stored.new,
                         "the coordinate must be stored exactly as authored")
        self.assertEqual(purge.parse_coordinate(COORD), purge.parse_coordinate(stored.new))
        self.assertEqual("purge", stored.disposition)
        self.assertTrue(stored.old.startswith("/") or stored.old.startswith("~"),
                        "the checkout path is still a path: %r" % stored.old)

    def test_an_unapproved_purge_plan_does_nothing_at_all(self):
        self.assertEqual(0, self.propose())

        code = self.cli("converge")

        self.assertEqual(0, code, "an unapproved plan is skipped, not flagged")
        self.assertTrue(os.path.isdir(self.repo))
        self.assertFalse(os.path.exists(self.tombstones_file))

    def test_approve_then_converge_dry_run_previews_the_disarmed_run_and_exits_zero(self):
        self.assertEqual(0, self.propose())
        self.assertEqual(0, self.cli("approve", "purge-throwaway-20261001", "--yes",
                                     "--by", "test"))

        code = self.cli("converge", "--dry-run")

        self.assertEqual(0, code)
        self.assertTrue(os.path.isdir(self.repo), "a dry run removes nothing")
        self.assertFalse(os.path.exists(self.tombstones_file))
        self.assertFalse(os.path.exists(self.transitions_file))

    def test_approve_then_a_plain_converge_is_DISARMED_and_exits_NON_ZERO(self):
        """The default live run. Nothing happens, and the sweep flags it."""
        self.assertEqual(0, self.propose())
        self.cli("approve", "purge-throwaway-20261001", "--yes", "--by", "test")

        code = self.cli("converge")

        self.assertEqual(1, code, "an approved-but-disarmed purge is an open obligation")
        self.assertTrue(os.path.isdir(os.path.join(self.repo, ".git")))
        self.assertFalse(os.path.exists(self.tombstones_file))

    def test_a_plan_whose_target_is_not_the_declared_remote_is_NEVER_RECORDED(self):
        """Refusing at authoring time means the origin gate never shows a human a consequence
        no machine can carry out — and the plans store is append-only, so a bad row has to be
        corrected by hand rather than superseded."""
        code = self.cli("propose", "--kind", "purge", "--id", "throwaway",
                        "--old", self.repo, "--new", OTHER_COORD)

        self.assertEqual(1, code)
        self.assertEqual({}, plans.load_plans(self.plans_file),
                         "a plan that cannot execute must not reach the store")

    def test_a_plan_whose_target_will_not_parse_is_never_recorded(self):
        code = self.cli("propose", "--kind", "purge", "--id", "throwaway",
                        "--old", self.repo, "--new", "Lmandrake/Throwaway")

        self.assertEqual(1, code)
        self.assertEqual({}, plans.load_plans(self.plans_file))

    def test_the_arming_flag_is_actually_PLUMBED_from_the_CLI_to_the_executor(self):
        """🔴 The flag has to reach the one branch that mutates, or it is decoration.

        This repo's signature defect is a complete subsystem wired to nothing — `lib/enroll.py`
        with 13 green tests and no caller, SWEEP's group engine with ~40. An arming flag that
        parsed and never arrived would be the same defect with the polarity reversed: the
        operator would arm it, read `disarmed`, and go hunting a bug. `remove_tree` and
        `delete_remote` are recorders for this whole class, so this proves the wiring without
        anything being removed or any host being reached.
        """
        self.propose()
        self.cli("approve", "purge-throwaway-20261001", "--yes", "--by", "test")

        code = self.cli("converge", "--arm-purge")

        self.assertEqual(0, code, self.printed)
        self.assertEqual([(self.repo, True)], self.removed,
                         "the executor must have been asked to remove, ARMED")
        self.assertEqual([("github.com/Lmandrake/Throwaway", True)], self.deleted)
        self.assertIn("PURGE IS ARMED", self.printed,
                      "an armed run must say so before it acts")
        self.assertTrue(tombstones.is_retired("throwaway", self.tombstones_file))

    def test_an_ARMED_CLI_converge_writes_State_retired_into_the_registry(self):
        """🔴 The whole path, through `main`, ending in a real single-cell write on disk.

        CLAUDE.md §7 records why a fixture cannot stand in for this: the rehome's
        corrupted-URL defect lived in `propose`, so every executor-level test was green while
        the authored value was already wrong. A purge's registry write is the same shape of
        claim — "desired state ends up saying retired" — and only the CLI can prove the id the
        write is aimed at survives authoring, approval and the converge lock intact.
        """
        self.propose()
        self.cli("approve", "purge-throwaway-20261001", "--yes", "--by", "test")

        code = self.cli("converge", "--arm-purge")

        self.assertEqual(0, code, self.printed)
        states = {e.repo_id: e.state for e in registry.entries(self.registry_file)}
        self.assertEqual({"throwaway": "retired"}, states,
                         "the row must survive and declare retired")
        self.assertIn("registry now declares state retired", self.printed)

    def test_a_dry_run_NAMES_the_registry_edit_and_writes_NOTHING(self):
        """A preview of a write to shared desired state has to show the write."""
        self.propose()
        self.cli("approve", "purge-throwaway-20261001", "--yes", "--by", "test")
        before = open(self.registry_file, encoding="utf-8").read()

        self.cli("converge", "--dry-run", "--arm-purge")

        self.assertIn("would set the registry's State for throwaway to retired", self.printed)
        self.assertEqual(before, open(self.registry_file, encoding="utf-8").read())

    def test_the_consequence_PRINT_names_the_shared_registry_edit_and_the_satellites(self):
        """🔴 Approving a purge authorises a fleet-wide write and the loss of memory notes.

        §3C makes every other machine act on an approved plan without ever asking, so anything
        the consequence text does not mention is being approved blind — the argument the rehome
        block already makes about the same file, plus the auto-memory directory he was shown and
        accepted.
        """
        self.propose()

        self.assertIn("Shared desired state", self.printed)
        self.assertIn("State for throwaway will be set to `retired`", self.printed)
        self.assertIn("NOT deleted", self.printed)
        self.assertIn("Satellites", self.printed)
        self.assertIn("outlive the repository", self.printed)

    def test_dry_run_PLUS_arm_purge_previews_the_armed_run_and_still_mutates_nothing(self):
        """`--dry-run` dominates `--arm-purge`. The review command is the two together."""
        self.propose()
        self.cli("approve", "purge-throwaway-20261001", "--yes", "--by", "test")

        code = self.cli("converge", "--dry-run", "--arm-purge")

        self.assertEqual(0, code, self.printed)
        self.assertEqual([], self.removed, "a dry run must ask for no removal at all")
        self.assertEqual([], self.deleted)
        self.assertFalse(os.path.exists(self.tombstones_file))
        self.assertIn("would-converge", self.printed)

    def test_the_consequence_for_an_UNREGISTERED_id_says_omitted_and_does_not_crash(self):
        """Reachable in real life, and the only route in.

        `cmd_propose` refuses an unregistered id earlier — there is no declared `Remote` to
        check the coordinate against — so this path is reached by a hand-written plan row and
        then `approve`. The purge branch dereferences the row for its "already absent"
        observation, so a missing row must be handled before it, not inside it.
        """
        import io
        self._patch(lifecycle, "_row_for", lambda repo_id: None)
        out = io.StringIO()

        lifecycle.print_consequence(plan(old="~/dev/Ghost"), out=out)

        self.assertIn("never authorises removal", out.getvalue())

    def test_the_origin_consequence_spells_out_that_there_is_no_undo(self):
        """§3C: the origin prompt is the whole safety argument, so it must state consequences
        rather than printing a summary line."""
        import io
        self.propose()
        out = io.StringIO()

        lifecycle.print_consequence(plans.load_plans(self.plans_file)
                                    ["purge-throwaway-20261001"], out=out)
        text = out.getvalue()

        self.assertIn("THERE IS NO UNDO", text)
        self.assertIn("--arm-purge", text)
        self.assertIn(purge.SCOPE, text)
        self.assertIn("gh repo delete", text)


if __name__ == "__main__":
    unittest.main()
