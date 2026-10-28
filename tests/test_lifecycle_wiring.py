"""Tests for the WIRING — the production callers, not the libraries they call.

This file exists because of a specific, repeated defect in this repo: a complete and
well-tested module with no production caller looks exactly like a working feature.
`lib/enroll.py` shipped with 13 passing tests, zero callers and not one stamped checkout.
SWEEP's group engine shipped with ~40 passing tests and a dispatch path that never reached it.
And `Former` was parsed into `registry.Entry.former` by Phase 1 and read by **nothing** until
2026-09-30 — which is the bug these tests are mostly about.

So what is asserted here is *reachability and refusal*, not library behaviour:

1. `skillclone.former_instance` recognises a previous identity still on disk.
2. `sync.clone_step` REFUSES to clone such a row, instead of making a second checkout.
3. `sync.lifecycle_step` exists, is called, and does not prompt.
4. The Lifecycle step runs BEFORE the clone step in `sync.main`.
"""

import inspect
import os
import re
import shutil
import tempfile
import unittest

import lifecycle
import sync
from lib import plans, registry, satellites, skillclone


def source_without_comments(obj):
    """A function's source with `#` comments stripped.

    CLAUDE.md §7: a test can pass on a comment. A presence check that reads the sentence
    EXPLAINING why something is wired stays green when the wiring beneath it is deleted.
    """
    text = inspect.getsource(obj)
    return "\n".join(re.sub(r"#.*$", "", line) for line in text.split("\n"))


def an_entry(**over):
    base = dict(name="DispatchOpenCode", path="~/dev/DispatchOpenCode", branch="main",
                sync=True, notes="", send=True, receive=True, machines=(),
                repo_id="opencodecli",
                former=("OpenCodeCLI", "git@github.com:Lmandrake/OpenCodeCLI.git"),
                remote="git@github.com:Lmandrake/DispatchOpenCode.git", state="active")
    base.update(over)
    return registry.Entry(**base)


class TestRenameScansAllFourSatelliteKinds(unittest.TestCase):
    """🔴 `satellites.find`'s roots have no defaults, so an omitted argument is an inert kind.

    Measured 2026-10-01 before the fix: `_do_rename` called `find(old_path, new_path)` and
    nothing else, so `snapshot-memory` and `shortcut` were found by **nothing** while the
    module docstring described four kinds checked independently. This is the wiring, which is
    the only thing that can go missing without a single library test noticing.
    """

    def test_do_rename_passes_BOTH_roots_to_the_census(self):
        source = source_without_comments(lifecycle._do_rename)

        self.assertIn("satellites.find(", source)
        self.assertIn("repo_root=", source)
        self.assertIn("waypoint_root=", source)

    def test_the_waypoint_root_comes_from_the_ONE_shared_resolver(self):
        """A second `os.path.join(home, "dev", "Waypoint")` literal here would be the
        five-copies-of-`_escape` shape: two resolvers free to drift apart."""
        source = source_without_comments(lifecycle._do_rename)

        self.assertIn("satellites.default_waypoint_root()", source)
        self.assertNotIn('"Waypoint"', source)
        self.assertTrue(satellites.default_waypoint_root().endswith(os.path.join("dev", "Waypoint")))

    def test_find_still_has_NO_defaults_so_a_fixture_can_opt_out(self):
        """The reason the roots are passed explicitly instead of defaulted: `None` must keep
        meaning *do not look*, or every fixture that passes it would quietly start reading his
        live shortcut tables."""
        signature = inspect.signature(satellites.find)
        for name in ("repo_root", "waypoint_root"):
            self.assertIsNone(signature.parameters[name].default)
        self.assertEqual([], satellites.find("/nonexistent/Old", "/nonexistent/New",
                                             repo_root=None, waypoint_root=None))


class TestFormerInstanceHasAConsumer(unittest.TestCase):
    """The `Former` column's whole job. Parsed since Phase 1, consumed since 2026-09-30."""

    def test_a_former_name_still_on_disk_is_recognised(self):
        entry = an_entry()
        inventory = [("/home/user/dev/OpenCodeCLI",
                      "git@github.com:Lmandrake/OpenCodeCLI.git")]
        hit = skillclone.former_instance(entry, inventory=inventory)
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], "/home/user/dev/OpenCodeCLI")

    def test_a_former_remote_under_a_different_directory_name_is_recognised(self):
        """Matching only on the name would miss a checkout someone had already moved."""
        entry = an_entry()
        inventory = [("/home/user/dev/SomethingElse",
                      "git@github.com:Lmandrake/OpenCodeCLI.git")]
        hit = skillclone.former_instance(entry, inventory=inventory)
        self.assertIsNotNone(hit)
        self.assertIn("former remote", hit[1])

    def test_the_https_form_of_a_former_remote_still_matches(self):
        entry = an_entry()
        inventory = [("/home/user/dev/SomethingElse",
                      "https://github.com/Lmandrake/OpenCodeCLI.git")]
        self.assertIsNotNone(skillclone.former_instance(entry, inventory=inventory))

    def test_a_case_variant_of_a_former_name_matches_because_apfs_is_case_insensitive(self):
        entry = an_entry()
        inventory = [("/home/user/dev/opencodecli", "whatever")]
        self.assertIsNotNone(skillclone.former_instance(entry, inventory=inventory))

    def test_a_row_with_no_former_never_matches(self):
        """No false positives: the overwhelming majority of rows have an empty Former."""
        entry = an_entry(former=())
        inventory = [("/home/user/dev/Anything", "git@github.com:x/y.git")]
        self.assertIsNone(skillclone.former_instance(entry, inventory=inventory))

    def test_the_rows_own_current_path_is_not_treated_as_a_former_identity(self):
        """Otherwise a correctly-renamed repo would report itself as mid-rename for ever."""
        entry = an_entry()
        # expanduser, not a literal /Users path: the row's `~/dev/...` resolves to the running
        # machine's home, so a hardcoded macOS path only ever matched on a Mac.
        inventory = [(os.path.expanduser("~/dev/DispatchOpenCode"),
                      "git@github.com:Lmandrake/OpenCodeCLI.git")]
        self.assertIsNone(skillclone.former_instance(entry, inventory=inventory))

    def test_an_unrelated_checkout_never_matches(self):
        entry = an_entry()
        inventory = [("/home/user/dev/Totally", "git@github.com:Lmandrake/Unrelated.git")]
        self.assertIsNone(skillclone.former_instance(entry, inventory=inventory))


class TestCloneStepRefusesAMidRenameRow(unittest.TestCase):
    """🔴 The actual failure being prevented: a second checkout of one repo.

    `clone_step` computes absence from the row's `Path`, and `adopt` matches an existing
    checkout on its origin URL. A renamed row shares neither with the old checkout — the old
    origin normalises to a *different* identity — so without this guard the machine clones a
    fresh copy and leaves the stale one pointing at the old remote. That is the
    `RimMaster`/`RimMandrake` failure in `docs/SYNC_AGENT.md`.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-wiring-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.old_checkouts = skillclone.checkouts
        self.addCleanup(self._restore)

    def _restore(self):
        skillclone.checkouts = self.old_checkouts

    def test_a_row_whose_former_identity_is_present_is_not_cloned(self):
        entry = an_entry(path=os.path.join(self.tmp, "DispatchOpenCode"))
        skillclone.checkouts = lambda root=None: [
            (os.path.join(self.tmp, "OpenCodeCLI"),
             "git@github.com:Lmandrake/OpenCodeCLI.git")]

        result = sync.clone_step([entry], dry_run=True, where="desktop")

        self.assertIsNotNone(result, "the row must be REPORTED, not silently skipped")
        blob = " ".join(result.actions)
        self.assertIn("NOT cloned", blob)
        self.assertIn("mid-rename", blob)
        self.assertIn("lifecycle", blob,
                      "the report must name the tool that completes the rename")

    def test_the_mid_rename_row_is_flagged_rather_than_reported_as_clean(self):
        entry = an_entry(path=os.path.join(self.tmp, "DispatchOpenCode"))
        skillclone.checkouts = lambda root=None: [
            (os.path.join(self.tmp, "OpenCodeCLI"), "git@github.com:Lmandrake/OpenCodeCLI.git")]
        result = sync.clone_step([entry], dry_run=True, where="desktop")
        self.assertEqual(result.outcome, sync.FLAGGED)

    def test_a_genuinely_absent_row_with_no_former_still_reaches_the_clone_path(self):
        """The guard must not swallow ordinary cloning — that would stall a fresh machine."""
        entry = an_entry(path=os.path.join(self.tmp, "Brand New"), former=())
        skillclone.checkouts = lambda root=None: []
        result = sync.clone_step([entry], dry_run=True, where="desktop")
        # Either an attempt was made or it was refused for a reason, but it must NOT be
        # reported as mid-rename.
        if result is not None:
            self.assertNotIn("mid-rename", " ".join(result.actions))

    def test_a_row_already_present_on_disk_is_not_considered_at_all(self):
        present = os.path.join(self.tmp, "DispatchOpenCode")
        os.makedirs(present)
        entry = an_entry(path=present)
        skillclone.checkouts = lambda root=None: []
        self.assertIsNone(sync.clone_step([entry], dry_run=True, where="desktop"))


class TestLifecycleStepIsWired(unittest.TestCase):
    def test_sync_has_a_lifecycle_step(self):
        self.assertTrue(callable(getattr(sync, "lifecycle_step", None)),
                        "the converger needs a caller in the sweep or it is inert")

    def test_it_returns_none_when_no_plan_is_approved(self):
        """A sweep on a fleet with no pending lifecycle work must report nothing at all."""
        real = plans.load_plans
        plans.load_plans = lambda path=None: {}
        try:
            self.assertIsNone(sync.lifecycle_step(dry_run=True))
        finally:
            plans.load_plans = real

    def test_it_returns_none_when_plans_exist_but_none_are_approved(self):
        real_plans, real_approvals = plans.load_plans, plans.load_approvals
        plan = plans.Plan(plan_id="p1", kind="rename", repo_id="x", machines=("all",),
                          old="/a", new="/b", disposition="", created_at="2026-09-30T00:00-07:00",
                          created_by="mt-001", note="")
        plans.load_plans = lambda path=None: {"p1": plan}
        plans.load_approvals = lambda path=None: {}
        try:
            self.assertIsNone(sync.lifecycle_step(dry_run=True))
        finally:
            plans.load_plans, plans.load_approvals = real_plans, real_approvals


class TestLifecycleRunsBeforeCloning(unittest.TestCase):
    """🔴 Order is load-bearing, not cosmetic.

    A row whose rename has not been applied here reads as absent to `clone_step`. Converging
    afterwards would race a second checkout that the converger then has to refuse as
    `both-present` — turning a completable operation into a conflict needing a human. The
    `former_instance` guard is the belt; this ordering is the braces.
    """

    def test_lifecycle_step_is_called_before_clone_step_in_main(self):
        import inspect
        # The sweep body moved into `_sweep` (2026-10-26) so `_main` can hold the sweep lock.
        source = inspect.getsource(sync._sweep)
        # Strip comments, so a mention inside an explanatory comment cannot satisfy this —
        # this repo has a recorded case of a test passing on a comment alone.
        code = "\n".join(line.split("#", 1)[0] for line in source.split("\n"))
        life = code.find("lifecycle_step(")
        clone = code.find("clone_step(")
        self.assertGreater(life, -1, "lifecycle_step is never called from main")
        self.assertGreater(clone, -1, "clone_step is never called from main")
        self.assertLess(life, clone,
                        "lifecycle_step must be invoked BEFORE clone_step in sync.main")

    def test_the_wrapper_never_passes_ignore_freshness(self):
        """The unattended caller must not be able to act on a stale board.

        `cmd_converge` enforces freshness internally; the escape hatch exists for a human at a
        terminal. An unattended sweep reaching past the gate would be a scope violation, not a
        convenience — so this asserts the flag is absent from the wrapper's source.
        """
        import inspect
        source = inspect.getsource(sync.lifecycle_step)
        code = "\n".join(line.split("#", 1)[0] for line in source.split("\n"))
        code = code.replace('"""', "\x00")
        body = code.split("\x00")[-1] if "\x00" in code else code
        self.assertNotIn("--ignore-freshness", body)


if __name__ == "__main__":
    unittest.main()


class TestConvergeJournalsTransitionsNotRuns(unittest.TestCase):
    """A converge that journals every run keeps the board from ever passing quiescence."""

    def setUp(self):
        import lifecycle
        self.lifecycle = lifecycle
        self.tmp = tempfile.mkdtemp(prefix="lodestar-journaled-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = os.path.join(self.tmp, "sub", "journaled.json")

    def test_the_first_status_is_journaled(self):
        self.assertTrue(self.lifecycle.status_changed("p1", "stop", self.state))

    def test_a_repeated_status_is_not_journaled_again(self):
        self.lifecycle.status_changed("p1", "converged", self.state)
        self.assertFalse(self.lifecycle.status_changed("p1", "converged", self.state))

    def test_a_changed_status_is_journaled(self):
        self.lifecycle.status_changed("p1", "stop", self.state)
        self.assertTrue(self.lifecycle.status_changed("p1", "converged", self.state))

    def test_plans_are_tracked_independently(self):
        self.lifecycle.status_changed("p1", "converged", self.state)
        self.assertTrue(self.lifecycle.status_changed("p2", "converged", self.state))

    def test_a_corrupt_state_file_costs_one_duplicate_not_a_crash(self):
        os.makedirs(os.path.dirname(self.state))
        with open(self.state, "w") as handle:
            handle.write("{not json")
        self.assertTrue(self.lifecycle.status_changed("p1", "converged", self.state))
        self.assertFalse(self.lifecycle.status_changed("p1", "converged", self.state))

    def test_converge_routes_its_journal_write_through_the_gate(self):
        """Reachability: the helper is worthless if cmd_converge journals around it."""
        import inspect
        source = inspect.getsource(self.lifecycle.cmd_converge)
        self.assertIn("status_changed(plan.plan_id, status)", source)


class TestLocalResolvesASymlinkedDevRoot(unittest.TestCase):
    """tower's ~/dev is a symlink; every rename there was refused as symlinked-destination."""

    def setUp(self):
        import lifecycle
        self.local = lifecycle.local
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="lodestar-local-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.real = os.path.join(self.tmp, "real")
        os.makedirs(os.path.join(self.real, "inner"))
        self.root = os.path.join(self.tmp, "dev")
        os.symlink(self.real, self.root)

    def test_a_path_under_a_symlinked_root_resolves_to_the_real_root(self):
        self.assertEqual(self.local(os.path.join(self.root, "Repo"), dev_root=self.root),
                         os.path.join(self.real, "Repo"))

    def test_a_symlink_below_the_root_is_left_for_the_guard_to_refuse(self):
        os.symlink(os.path.join(self.real, "inner"), os.path.join(self.real, "linked"))
        out = self.local(os.path.join(self.root, "linked", "Repo"), dev_root=self.root)
        self.assertEqual(out, os.path.join(self.real, "linked", "Repo"))

    def test_a_plain_root_and_an_outside_path_are_untouched(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        self.assertEqual(self.local(os.path.join(plain, "R"), dev_root=plain),
                         os.path.join(plain, "R"))
        self.assertEqual(self.local("/elsewhere/R", dev_root=self.root), "/elsewhere/R")
        self.assertEqual(self.local("", dev_root=self.root), "")


class TestObserveTreatsACompatibilityLinkAsMoved(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="lodestar-observe-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.new = os.path.join(self.tmp, "New")
        os.makedirs(self.new)
        self.old = os.path.join(self.tmp, "Old")

    def test_a_link_from_old_to_new_is_moved(self):
        from lib import transitions
        os.symlink(self.new, self.old)
        self.assertEqual(transitions.observe(self.old, self.new), transitions.MOVED)

    def test_two_real_directories_are_still_both(self):
        from lib import transitions
        os.makedirs(self.old)
        self.assertEqual(transitions.observe(self.old, self.new), transitions.BOTH)

    def test_a_link_pointing_elsewhere_is_still_both(self):
        from lib import transitions
        elsewhere = os.path.join(self.tmp, "Elsewhere")
        os.makedirs(elsewhere)
        os.symlink(elsewhere, self.old)
        self.assertEqual(transitions.observe(self.old, self.new), transitions.BOTH)
