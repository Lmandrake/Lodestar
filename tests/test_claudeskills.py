"""Tests for lib/claudeskills.py -- materializing ~/.claude/skills from a table."""

import json
import os
import shutil
import tempfile
import unittest

from lib import claudeskills

TABLE = """# Skills

| Skill | Target | Repo | Machines |
|-------|--------|------|----------|
| alpha | ~/dev/Alpha/.claude/skills/alpha | Alpha | all |
| beta | ~/dev/Beta | Beta | box-a |
"""


class SkillsTest(unittest.TestCase):
    def setUp(self):
        # realpath: on macOS a tempdir lives under /var, which is a symlink to
        # /private/var. _prunable resolves link targets before comparing them to the
        # prune roots, so an unresolved fixture root would never match its own symlinks.
        # The production roots (~/dev, ~/checkout) are already canonical.
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="lodestar-skills-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.live = os.path.join(self.tmp, "skills")
        os.makedirs(self.live)
        self.table_path = os.path.join(self.tmp, "skills.md")
        with open(self.table_path, "w", encoding="utf-8") as handle:
            handle.write(TABLE)

    def make_skill(self, relative, name="alpha", description="does alpha"):
        target = os.path.join(self.tmp, relative)
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, "SKILL.md"), "w", encoding="utf-8") as handle:
            handle.write("---\nname: %s\ndescription: %s\n---\n\nBody.\n"
                         % (name, description))
        return target

    def kinds(self, actions):
        return sorted((a.kind, a.skill) for a in actions)


class TestTable(SkillsTest):
    def test_rows_parse_and_expand_the_target(self):
        rows = claudeskills.rows(self.table_path)
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0].target.startswith(os.path.expanduser("~")))

    def test_machines_all_applies_everywhere(self):
        rows = claudeskills.rows(self.table_path)
        self.assertTrue(claudeskills.applies(rows[0], "box-a"))
        self.assertTrue(claudeskills.applies(rows[0], "box-b"))

    def test_machines_list_restricts(self):
        rows = claudeskills.rows(self.table_path)
        self.assertTrue(claudeskills.applies(rows[1], "box-a"))
        self.assertFalse(claudeskills.applies(rows[1], "box-b"))


class TestPlan(SkillsTest):
    def test_missing_link_is_created(self):
        target = self.make_skill("alpha-src")
        rows = [claudeskills.Row("alpha", target, "Alpha", "all")]
        actions = claudeskills.plan(rows, "box-a", self.live, {"Alpha"})
        self.assertIn(("create", "alpha"), self.kinds(actions))

    def test_correct_link_is_ok(self):
        target = self.make_skill("alpha-src")
        os.symlink(target, os.path.join(self.live, "alpha"))
        rows = [claudeskills.Row("alpha", target, "Alpha", "all")]
        actions = claudeskills.plan(rows, "box-a", self.live, {"Alpha"})
        self.assertIn(("ok", "alpha"), self.kinds(actions))

    def test_wrong_target_is_repointed(self):
        target = self.make_skill("alpha-src")
        other = self.make_skill("other-src", name="other")
        os.symlink(other, os.path.join(self.live, "alpha"))
        rows = [claudeskills.Row("alpha", target, "Alpha", "all")]
        actions = claudeskills.plan(rows, "box-a", self.live, {"Alpha"})
        self.assertIn(("repoint", "alpha"), self.kinds(actions))

    def test_absent_target_with_registered_repo_is_a_clone(self):
        rows = [claudeskills.Row("alpha", os.path.join(self.tmp, "nope"), "Alpha", "all")]
        actions = claudeskills.plan(rows, "box-a", self.live, {"Alpha"})
        self.assertIn(("clone", "alpha"), self.kinds(actions))

    def test_absent_target_without_registered_repo_is_dangling(self):
        rows = [claudeskills.Row("alpha", os.path.join(self.tmp, "nope"), "", "all")]
        actions = claudeskills.plan(rows, "box-a", self.live, set())
        self.assertIn(("dangling", "alpha"), self.kinds(actions))

    def test_row_for_another_machine_is_skipped_entirely(self):
        rows = [claudeskills.Row("beta", os.path.join(self.tmp, "nope"), "Beta", "box-a")]
        actions = claudeskills.plan(rows, "box-b", self.live, {"Beta"})
        self.assertEqual(actions, [])

    def test_unlisted_symlink_lodestar_did_not_create_is_undeclared_not_pruned(self):
        """The 2026-09-02 regression: a hand-made link is intent, not garbage."""
        target = self.make_skill("dev/Stray")
        os.symlink(target, os.path.join(self.live, "stray"))
        actions = claudeskills.plan(claudeskills.rows(self.table_path), "box-a", self.live,
                                    set(), prune_roots=(self.tmp,), created=set())
        self.assertIn(("undeclared", "stray"), self.kinds(actions))
        self.assertNotIn(("prune", "stray"), self.kinds(actions))

    def test_real_directory_is_never_pruned(self):
        os.makedirs(os.path.join(self.live, "cowork-bridge"))
        actions = claudeskills.plan([], "box-a", self.live, set(),
                                    prune_roots=(self.tmp,))
        self.assertIn(("unmanaged", "cowork-bridge"), self.kinds(actions))
        self.assertNotIn(("prune", "cowork-bridge"), self.kinds(actions))

    def test_symlink_outside_the_allowed_roots_is_not_pruned(self):
        outside = self.make_skill("outside-src")
        os.symlink(outside, os.path.join(self.live, "outside"))
        actions = claudeskills.plan([], "box-a", self.live, set(),
                                    prune_roots=(os.path.expanduser("~/dev"),))
        self.assertIn(("unmanaged", "outside"), self.kinds(actions))
        self.assertNotIn(("prune", "outside"), self.kinds(actions))


class TestUndeclared(SkillsTest):
    def plan_with(self, created, prune_roots=None):
        target = self.make_skill("dev/Stray")
        link = os.path.join(self.live, "stray")
        if not os.path.exists(link):
            os.symlink(target, link)
        return claudeskills.plan(claudeskills.rows(self.table_path), "box-a", self.live,
                                 set(), prune_roots=prune_roots or (self.tmp,),
                                 created=created)

    def test_lodestar_created_link_with_no_row_is_pruned(self):
        """A row was removed from the table: propagating that removal is prune's real job."""
        self.assertIn(("prune", "stray"), self.kinds(self.plan_with({"stray"})))

    def test_link_lodestar_did_not_create_is_undeclared(self):
        self.assertIn(("undeclared", "stray"), self.kinds(self.plan_with(set())))

    def test_created_none_prunes_nothing(self):
        """Fail-safe: absent provenance must forbid deletion, not permit it."""
        kinds = self.kinds(self.plan_with(None))
        self.assertIn(("undeclared", "stray"), kinds)
        self.assertNotIn(("prune", "stray"), kinds)

    def test_location_guard_still_wins_over_provenance(self):
        """Both conditions must hold. Provenance does not license deleting outside the roots."""
        kinds = self.kinds(self.plan_with({"stray"},
                                          prune_roots=(os.path.expanduser("~/dev"),)))
        self.assertNotIn(("prune", "stray"), kinds)
        self.assertIn(("unmanaged", "stray"), kinds)

    def test_real_directory_is_still_never_pruned_even_if_claimed(self):
        os.makedirs(os.path.join(self.live, "cowork-bridge"))
        actions = claudeskills.plan(claudeskills.rows(self.table_path), "box-a", self.live,
                                    set(), prune_roots=(self.tmp,),
                                    created={"cowork-bridge"})
        self.assertIn(("unmanaged", "cowork-bridge"), self.kinds(actions))
        self.assertNotIn(("prune", "cowork-bridge"), self.kinds(actions))


class TestPrunable(SkillsTest):
    """The bulk-delete guard. Pure logic: no fixture on disk is needed or touched."""

    def prunes(self, count, prefix="stray"):
        return [claudeskills.Action("prune", "%s%d" % (prefix, i), "not in the table")
                for i in range(count)]

    def table(self, count, machines="all"):
        # Targets deliberately point at nothing: prunable() must not stat them.
        return [claudeskills.Row("row%d" % i, "/nonexistent/row%d" % i, "", machines)
                for i in range(count)]

    def test_an_empty_table_refuses_every_prune(self):
        """Rule 1: empty means seed-or-read-failure, never 'delete everything'."""
        allowed, reason = claudeskills.prunable(self.prunes(21), [], "box-a")
        self.assertEqual(allowed, [])
        self.assertIsNotNone(reason)
        self.assertIn("21", reason)

    def test_a_table_with_no_rows_for_this_machine_refuses(self):
        """Same hazard as an empty table: nothing declared here, so everything looks stale."""
        allowed, reason = claudeskills.prunable(self.prunes(5),
                                                self.table(21, machines="box-a"), "box-b")
        self.assertEqual(allowed, [])
        self.assertIn("box-b", reason)

    def test_nothing_to_prune_is_not_a_refusal(self):
        """An empty table with no prunes must not emit a scary reason into the report."""
        self.assertEqual(claudeskills.prunable([], [], "box-a"), ([], None))

    def test_exactly_the_floor_is_allowed_against_a_tiny_table(self):
        """3 prunes / 2 rows: limit is max(3, 1) = 3, and 3 > 3 is false."""
        allowed, reason = claudeskills.prunable(self.prunes(3), self.table(2), "box-a")
        self.assertIsNone(reason)
        self.assertEqual(len(allowed), 3)

    def test_one_over_the_floor_is_refused_against_a_tiny_table(self):
        allowed, reason = claudeskills.prunable(self.prunes(4), self.table(2), "box-a")
        self.assertEqual(allowed, [])
        self.assertIn("4", reason)
        self.assertIn("3", reason)

    def test_the_cap_does_not_grow_with_the_table(self):
        """A big table buys no extra deletions: the cap is flat, not a fraction.

        Ten removals was never routine — it was just under half of twenty-one.
        """
        allowed, reason = claudeskills.prunable(self.prunes(10), self.table(21), "box-a")
        self.assertEqual(allowed, [])
        self.assertIsNotNone(reason)

    def test_a_partially_truncated_table_is_refused(self):
        """The hole a fractional cap left open, and the likelier corruption of the two.

        A 21-row table read mid-write as 15 rows makes 6 links look stale. Scaled at
        max(3, 15 // 2) the limit was 7, so all six were deleted silently — the denominator
        shrank along with the table, which made the guard partly self-authorizing.
        """
        allowed, reason = claudeskills.prunable(self.prunes(6), self.table(15), "box-a")
        self.assertEqual(allowed, [])
        self.assertIn("6", reason)

    def test_a_human_can_authorise_a_larger_batch_explicitly(self):
        """The sweep cannot ask; a person can look. That is where the trust line sits."""
        allowed, reason = claudeskills.prunable(self.prunes(10), self.table(21), "box-a",
                                                limit=10)
        self.assertIsNone(reason)
        self.assertEqual(len(allowed), 10)

    def test_a_truncated_table_refuses_the_whole_live_set(self):
        """The failure this guard exists for: table lands with one row, 20 links look stale."""
        allowed, reason = claudeskills.prunable(self.prunes(20), self.table(1), "box-a")
        self.assertEqual(allowed, [])
        self.assertIn("20", reason)

    def test_only_applicable_rows_count_toward_the_limit(self):
        """21 rows but 4 for this machine -> limit 3, not 10."""
        rows = self.table(4) + [claudeskills.Row("other%d" % i, "/nonexistent", "", "box-b")
                                for i in range(17)]
        self.assertIsNone(claudeskills.prunable(self.prunes(3), rows, "box-a")[1])
        self.assertIsNotNone(claudeskills.prunable(self.prunes(4), rows, "box-a")[1])

    def test_a_refusal_is_all_or_nothing(self):
        """Rule 3: no partial batch. A half-done bulk delete is harder to reason about."""
        allowed, reason = claudeskills.prunable(self.prunes(11), self.table(21), "box-a")
        self.assertEqual(allowed, [])
        self.assertNotEqual(len(allowed), 10)
        self.assertIsNotNone(reason)

    def test_non_prune_actions_are_never_returned(self):
        actions = (self.prunes(2)
                   + [claudeskills.Action("create", "alpha", "link"),
                      claudeskills.Action("unmanaged", "cowork-bridge", "real directory"),
                      claudeskills.Action("ok", "beta", "linked")])
        allowed, reason = claudeskills.prunable(actions, self.table(10), "box-a")
        self.assertIsNone(reason)
        self.assertEqual(sorted(a.kind for a in allowed), ["prune", "prune"])

    def test_the_reason_carries_the_numbers_and_the_way_out(self):
        """It lands in a report a human reads, so it must say what and how much — and,
        because a refusal a human cannot act on is just noise, how to authorise it."""
        _, reason = claudeskills.prunable(self.prunes(9), self.table(6), "box-a")
        self.assertIn("9", reason)                       # what it wanted to remove
        self.assertIn(str(claudeskills.PRUNE_FLOOR), reason)   # the cap it exceeded
        self.assertIn("refusing", reason.lower())
        self.assertIn("--prune", reason)                 # the way out


class TestApplyPruneSafety(SkillsTest):
    """apply(allow_prune=True) must delete only what plan() marked prunable."""

    def test_a_listed_prune_is_actually_removed(self):
        """Positive control: without this the negative tests could pass for free."""
        target = self.make_skill("stray-src")
        link = os.path.join(self.live, "stray")
        os.symlink(target, link)
        # created={"stray"}: this test is exercising apply()'s prune step given a `prune`
        # action, not plan()'s provenance guard (that is TestUndeclared's job) -- so the
        # fixture must claim the link the same way a real create would have.
        actions = claudeskills.plan([], "box-a", self.live, set(),
                                    prune_roots=(self.tmp,), created={"stray"})
        claudeskills.apply(actions, self.live, {}, allow_prune=True)
        self.assertFalse(os.path.lexists(link))
        self.assertTrue(os.path.isdir(target))      # only the link goes, never the target

    def test_prune_is_still_opt_in(self):
        target = self.make_skill("stray-src")
        link = os.path.join(self.live, "stray")
        os.symlink(target, link)
        actions = claudeskills.plan([], "box-a", self.live, set(),
                                    prune_roots=(self.tmp,))
        self.assertEqual(claudeskills.apply(actions, self.live, {}), [])
        self.assertTrue(os.path.lexists(link))

    def test_a_real_directory_survives_prune_enabled_apply(self):
        """What protects cowork-bridge, asserted at the apply layer, not just the plan."""
        real = os.path.join(self.live, "cowork-bridge")
        os.makedirs(real)
        actions = claudeskills.plan([], "box-a", self.live, set(),
                                    prune_roots=(self.tmp,))
        done = claudeskills.apply(actions, self.live, {}, allow_prune=True)
        self.assertTrue(os.path.isdir(real))
        self.assertEqual([line for line in done if "cowork-bridge" in line], [])

    def test_a_symlink_outside_the_roots_survives_prune_enabled_apply(self):
        outside = self.make_skill("outside-src")
        link = os.path.join(self.live, "outside")
        os.symlink(outside, link)
        # ~/dev is the real root and the fixture tree is under neither root, so this link
        # is exactly the "made by a human, not by this system" case.
        actions = claudeskills.plan([], "box-a", self.live, set(),
                                    prune_roots=(os.path.expanduser("~/dev"),))
        claudeskills.apply(actions, self.live, {}, allow_prune=True)
        self.assertTrue(os.path.lexists(link))

    def test_a_refused_batch_deletes_nothing(self):
        """plan -> prunable -> apply: the guard's refusal must survive into apply()."""
        links = []
        names = set()
        for index in range(6):
            target = self.make_skill("stray-src-%d" % index, name="stray%d" % index)
            link = os.path.join(self.live, "stray%d" % index)
            os.symlink(target, link)
            links.append(link)
            names.add("stray%d" % index)
        # created=names: this test exercises the bulk-delete guard given a batch of `prune`
        # actions, not plan()'s provenance split, so the fixture must claim them as created.
        actions = claudeskills.plan([], "box-a", self.live, set(),
                                    prune_roots=(self.tmp,), created=names)
        allowed, reason = claudeskills.prunable(actions, [], "box-a")
        self.assertIsNotNone(reason)
        claudeskills.apply(allowed, self.live, {}, allow_prune=True)
        for link in links:
            self.assertTrue(os.path.lexists(link))


class TestApplyOnAFreshMachine(SkillsTest):
    """`~/.claude/skills` absent — a freshly cloned machine, the case D18 was written for.

    Nothing in bin/ creates that directory, so the first `create` used to raise
    FileNotFoundError. sync.py catches it, which makes the failure permanent rather than
    loud: the sweep reports `skills step failed` every ~127 s and links nothing, while
    check.py (dry_run=True, never reaches os.symlink) reports clean.
    """

    def setUp(self):
        super(TestApplyOnAFreshMachine, self).setUp()
        self.absent = os.path.join(self.tmp, "no-skills-dir-here")
        self.target = self.make_skill("alpha-src")
        self.row = claudeskills.Row("alpha", self.target, "Alpha", "all")

    def test_a_missing_live_dir_is_created_rather_than_raising(self):
        actions = claudeskills.plan([self.row], "box-a", self.absent, {"Alpha"})
        self.assertIn(("create", "alpha"), self.kinds(actions))
        done = claudeskills.apply(actions, self.absent, {"alpha": self.row})
        self.assertTrue(
            os.path.islink(os.path.join(self.absent, "alpha")),
            "a machine whose ~/.claude/skills does not exist yet must get one; otherwise the "
            "first create raises and the sweep links nothing, forever. Lines were: %r"
            % (list(done),))
        self.assertEqual(list(done), ["linked alpha"])

    def test_a_dry_run_does_not_create_the_live_dir(self):
        """Shadow mode reports on runs that did not happen and must not make them happen."""
        actions = claudeskills.plan([self.row], "box-a", self.absent, {"Alpha"})
        claudeskills.apply(actions, self.absent, {"alpha": self.row}, dry_run=True)
        self.assertFalse(
            os.path.exists(self.absent),
            "a dry run created %s; writing nothing includes creating no directories"
            % self.absent)

    def test_nothing_pending_does_not_create_the_live_dir(self):
        """A plan with no create/repoint must not conjure a directory as a side effect."""
        claudeskills.apply([claudeskills.Action("ok", "alpha", "linked")], self.absent, {})
        self.assertFalse(os.path.exists(self.absent))


class TestApplyPerActionFailure(SkillsTest):
    """One bad row must cost one row, not the whole skill set."""

    def _colliding_actions(self):
        # A plain file already occupying the link name makes os.symlink raise
        # FileExistsError -- the shape a name collision or a half-finished write takes.
        with open(os.path.join(self.live, "beta"), "w", encoding="utf-8") as handle:
            handle.write("not a symlink\n")
        alpha = self.make_skill("alpha-src")
        beta = self.make_skill("beta-src", name="beta")
        rows = {"alpha": claudeskills.Row("alpha", alpha, "", "all"),
                "beta": claudeskills.Row("beta", beta, "", "all")}
        # beta first: without a per-action guard its failure aborts alpha as well.
        actions = [claudeskills.Action("create", "beta", "link -> %s" % beta),
                   claudeskills.Action("create", "alpha", "link -> %s" % alpha)]
        return actions, rows

    def test_one_failing_action_does_not_abort_the_rest(self):
        actions, rows = self._colliding_actions()
        done = claudeskills.apply(actions, self.live, rows)
        self.assertTrue(
            os.path.islink(os.path.join(self.live, "alpha")),
            "a failure on one action stopped every later one; a single collision must not "
            "disable the whole skill set. Lines were: %r" % (list(done),))
        self.assertEqual(list(done), ["linked alpha"])

    def test_a_failed_action_is_reported_as_a_fault(self):
        actions, rows = self._colliding_actions()
        done = claudeskills.apply(actions, self.live, rows)
        self.assertTrue(
            any("beta" in fault for fault in done.faults),
            "a swallowed failure is the silent capability loss this module exists to "
            "prevent; it must surface as a fault naming the skill. Faults were: %r"
            % (done.faults,))

    def test_the_return_value_stays_a_list_of_lines(self):
        """`claudeconf.skills_state` does `lines.extend(apply(...))` and tests compare it to
        `[]`. The fault channel may not change either."""
        actions, rows = self._colliding_actions()
        done = claudeskills.apply(actions, self.live, rows)
        self.assertIsInstance(done, list)
        lines = ["earlier"]
        lines.extend(done)
        self.assertEqual(lines, ["earlier", "linked alpha"])
        self.assertEqual(claudeskills.apply([], self.live, {}), [])
        self.assertEqual(claudeskills.apply([], self.live, {}).faults, [])

    def test_a_row_missing_from_the_table_is_a_fault_not_a_crash(self):
        """plan() and apply() can be one re-plan apart; a vanished row must not kill the pass."""
        actions = [claudeskills.Action("create", "ghost", "link -> nowhere")]
        done = claudeskills.apply(actions, self.live, {})
        self.assertEqual(list(done), [])
        self.assertTrue(any("ghost" in fault for fault in done.faults))


class TestPruneRateWindow(SkillsTest):
    """The cap must bound the TOTAL, not the rate.

    PRUNE_FLOOR applied per call, and the caller is a sweep running ~28x/hour with no memory
    of earlier prunes: 21 links drained to 3 in six sweeps (~13 minutes) with no refusal ever
    firing. Only the zero-rows refusal saved the last three.
    """

    def prunes(self, count, prefix="stray"):
        return [claudeskills.Action("prune", "%s%d" % (prefix, i), "not in the table")
                for i in range(count)]

    def table(self, count, machines="all"):
        return [claudeskills.Row("row%d" % i, "/nonexistent/row%d" % i, "", machines)
                for i in range(count)]

    def state_path(self):
        return os.path.join(self.tmp, "state", "skill-prunes.json")

    def test_prunes_recorded_this_hour_count_against_the_cap(self):
        allowed, reason = claudeskills.prunable(self.prunes(2), self.table(10), "box-a",
                                                recent_prunes=2)
        self.assertEqual(allowed, [])
        self.assertIsNotNone(reason)
        self.assertIn("refusing", reason.lower())
        self.assertIn("--prune", reason)

    def test_the_cap_is_the_sum_and_the_floor_is_still_reachable(self):
        allowed, reason = claudeskills.prunable(self.prunes(2), self.table(10), "box-a",
                                                recent_prunes=1)
        self.assertIsNone(reason)
        self.assertEqual(len(allowed), 2)

    def test_no_prunes_recorded_behaves_exactly_as_before(self):
        self.assertIsNone(claudeskills.prunable(self.prunes(3), self.table(10), "box-a",
                                                recent_prunes=0)[1])
        self.assertIsNotNone(claudeskills.prunable(self.prunes(4), self.table(10), "box-a",
                                                  recent_prunes=0)[1])

    def test_history_never_lifts_the_zero_row_refusal(self):
        """Rule 1 comes first and is not liftable, by history or by limit."""
        allowed, reason = claudeskills.prunable(self.prunes(1), [], "box-a",
                                                recent_prunes=0, limit=99)
        self.assertEqual(allowed, [])
        self.assertIn("0 rows", reason)

    def test_a_human_limit_still_authorises_a_batch_despite_history(self):
        allowed, reason = claudeskills.prunable(self.prunes(10), self.table(21), "box-a",
                                                limit=99, recent_prunes=8)
        self.assertIsNone(reason)
        self.assertEqual(len(allowed), 10)

    def test_prunable_still_touches_no_filesystem(self):
        """Pure: it must not read the thing it is guarding against, nor its own cache."""
        allowed, reason = claudeskills.prunable(self.prunes(1), self.table(3), "box-a",
                                                recent_prunes=0)
        self.assertIsNone(reason)
        self.assertEqual(len(allowed), 1)
        self.assertFalse(os.path.exists(self.state_path()))

    # --- the machine-local counter behind it -------------------------------------------

    def test_a_missing_state_file_means_no_prunes_recorded(self):
        self.assertEqual(claudeskills.recent_prunes(path=self.state_path()), 0)

    def test_an_unreadable_state_file_means_no_prunes_recorded(self):
        """Pure cache: garbage must degrade to 'none recorded', never crash a sweep."""
        os.makedirs(os.path.dirname(self.state_path()))
        with open(self.state_path(), "w", encoding="utf-8") as handle:
            handle.write("{not json at all")
        self.assertEqual(claudeskills.recent_prunes(path=self.state_path()), 0)

    def test_a_recorded_batch_is_read_back(self):
        claudeskills.record_prunes(2, now=1000.0, path=self.state_path())
        self.assertEqual(claudeskills.recent_prunes(now=1000.0, path=self.state_path()), 2)

    def test_records_accumulate_across_calls(self):
        claudeskills.record_prunes(2, now=1000.0, path=self.state_path())
        claudeskills.record_prunes(1, now=1100.0, path=self.state_path())
        self.assertEqual(claudeskills.recent_prunes(now=1200.0, path=self.state_path()), 3)

    def test_records_older_than_the_window_stop_counting(self):
        """A rolling window, not a permanent ceiling: the machine heals on its own."""
        claudeskills.record_prunes(3, now=1000.0, path=self.state_path())
        later = 1000.0 + claudeskills.PRUNE_WINDOW_SECONDS + 1
        self.assertEqual(claudeskills.recent_prunes(now=later, path=self.state_path()), 0)

    def test_recording_zero_writes_nothing(self):
        claudeskills.record_prunes(0, now=1000.0, path=self.state_path())
        self.assertFalse(os.path.exists(self.state_path()))

    def test_the_state_file_lives_outside_the_board(self):
        """Machine-local per D-D: a per-machine file inside the synced board makes two
        machines overwrite each other, and anything lock-shaped in it wedges the other."""
        from lib import paths
        target = os.path.realpath(claudeskills.prune_state_file())
        self.assertFalse(target.startswith(os.path.realpath(paths.root()) + os.sep),
                         "%s is inside the board and would be committed" % target)
        self.assertTrue(target.endswith(".json"))

    def test_an_unwritable_state_dir_does_not_crash_the_sweep(self):
        blocker = os.path.join(self.tmp, "blocked")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("a file where a directory would have to go\n")
        claudeskills.record_prunes(1, now=1000.0,
                                   path=os.path.join(blocker, "skill-prunes.json"))

    def test_a_sweep_loop_cannot_drain_the_table_a_batch_at_a_time(self):
        """The reproduction, run against the fix.

        21 links against a table shrinking three rows at a sweep — each individual batch is
        within the flat cap, so before the fix all six sweeps proceeded and 21 links became 3
        in ~13 minutes with no refusal. Only the zero-rows rule saved the last three.
        """
        path = self.state_path()
        live, declared, pruned, refusals = 21, 21, 0, 0
        for sweep in range(28):                      # ~28 sweeps in an hour at 127 s
            now = 1000.0 + sweep * 127
            declared = max(3, declared - 3)          # the table shrinking under the sweep
            table = self.table(declared)
            stale = live - declared
            if not stale:
                continue
            allowed, reason = claudeskills.prunable(
                self.prunes(stale), table, "box-a",
                recent_prunes=claudeskills.recent_prunes(now=now, path=path))
            if reason:
                refusals += 1
                continue
            claudeskills.record_prunes(len(allowed), now=now, path=path)
            pruned += len(allowed)
            live -= len(allowed)
        self.assertLessEqual(
            pruned, claudeskills.PRUNE_FLOOR,
            "the sweep removed %d links in one hour against a cap of %d; the cap is still "
            "bounding the rate rather than the total" % (pruned, claudeskills.PRUNE_FLOOR))
        self.assertTrue(refusals, "a drain this size must produce a reported refusal")
        with open(path, encoding="utf-8") as handle:
            self.assertIsInstance(json.load(handle), dict)


class TestValidate(SkillsTest):
    def test_a_good_skill_validates(self):
        target = self.make_skill("alpha-src")
        self.assertIsNone(claudeskills.validate(target))

    def test_missing_skill_md_is_a_fault(self):
        target = os.path.join(self.tmp, "empty")
        os.makedirs(target)
        self.assertIn("SKILL.md", claudeskills.validate(target))

    def test_missing_description_is_a_fault(self):
        target = os.path.join(self.tmp, "nodesc")
        os.makedirs(target)
        with open(os.path.join(target, "SKILL.md"), "w", encoding="utf-8") as handle:
            handle.write("---\nname: x\n---\n\nBody.\n")
        self.assertIn("description", claudeskills.validate(target))

    def test_unparseable_frontmatter_is_a_fault_not_an_exception(self):
        """Spec §3: 'SKILL.md missing or frontmatter unparseable -> report broken'."""
        target = os.path.join(self.tmp, "unterminated")
        os.makedirs(target)
        with open(os.path.join(target, "SKILL.md"), "w", encoding="utf-8") as handle:
            handle.write("---\nname: x\ndescription: y\n")   # never closed
        self.assertIn("frontmatter", claudeskills.validate(target))

    def test_a_folded_scalar_description_is_valid(self):
        """The false positive that mattered: lib/frontmatter cannot read this, YAML can.

        two real skills are both shaped like this and both load
        fine in Claude Code, but were reported broken by the first validator.
        """
        target = os.path.join(self.tmp, "folded")
        os.makedirs(target)
        with open(os.path.join(target, "SKILL.md"), "w", encoding="utf-8") as handle:
            handle.write("---\nname: folded\ndescription: >\n"
                         "  First line with no colon at all\n"
                         "  and a second continuation line.\n---\n\nBody.\n")
        self.assertIsNone(claudeskills.validate(target))

    def test_a_continuation_line_is_not_mistaken_for_a_key(self):
        target = os.path.join(self.tmp, "sneaky")
        os.makedirs(target)
        with open(os.path.join(target, "SKILL.md"), "w", encoding="utf-8") as handle:
            handle.write("---\nname: sneaky\ndescription: >\n"
                         "  indented: this looks like a key but is prose\n---\n\nBody.\n")
        self.assertIsNone(claudeskills.validate(target))

    def test_a_body_heading_after_the_block_is_not_read_as_frontmatter(self):
        target = os.path.join(self.tmp, "bodykeys")
        os.makedirs(target)
        with open(os.path.join(target, "SKILL.md"), "w", encoding="utf-8") as handle:
            handle.write("---\nname: n\n---\n\ndescription: in the body, too late.\n")
        self.assertIn("description", claudeskills.validate(target))

    def test_content_before_the_block_is_a_fault(self):
        target = os.path.join(self.tmp, "preamble")
        os.makedirs(target)
        with open(os.path.join(target, "SKILL.md"), "w", encoding="utf-8") as handle:
            handle.write("Oops prose first.\n---\nname: n\ndescription: d\n---\n")
        self.assertIn("does not open with", claudeskills.validate(target))

    def test_every_real_globally_linked_skill_validates(self):
        """Regression guard against the class of bug this validator just had.

        Reads the live ~/.claude/skills. Skipped when it is absent so the suite stays
        portable, but on this machine it is the only test that would have caught the
        false positives, because the fixtures were all hand-written and too simple.
        """
        live = os.path.expanduser("~/.claude/skills")
        if not os.path.isdir(live):
            self.skipTest("no live ~/.claude/skills on this machine")
        faults = []
        for name in sorted(os.listdir(live)):
            path = os.path.join(live, name)
            if name.startswith(".") or not os.path.exists(path):
                continue          # dangling links are a different fault, tested elsewhere
            if name in claudeskills.HARNESS_DIRS:
                continue          # Claude Code's own store, not a skill
            fault = claudeskills.validate(os.path.realpath(path))
            if fault:
                faults.append("%s: %s" % (name, fault))
        self.assertEqual(faults, [])

    def test_broken_skill_shows_up_in_the_plan(self):
        target = os.path.join(self.tmp, "broken")
        os.makedirs(target)
        open(os.path.join(target, "SKILL.md"), "w").close()
        os.symlink(target, os.path.join(self.live, "broken"))
        rows = [claudeskills.Row("broken", target, "", "all")]
        actions = claudeskills.plan(rows, "box-a", self.live, set())
        self.assertIn(("broken", "broken"), self.kinds(actions))


class TestProvenance(SkillsTest):
    def state(self):
        return os.path.join(self.tmp, "skill-links.json")

    def test_missing_file_means_owns_nothing(self):
        self.assertEqual(claudeskills.created_links(self.state()), set())

    def test_corrupt_file_means_owns_nothing(self):
        with open(self.state(), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertEqual(claudeskills.created_links(self.state()), set())

    def test_wrong_shape_means_owns_nothing(self):
        with open(self.state(), "w", encoding="utf-8") as handle:
            json.dump(["alpha"], handle)
        self.assertEqual(claudeskills.created_links(self.state()), set())

    def test_record_then_read_roundtrips(self):
        claudeskills.record_created(["alpha", "beta"], self.state())
        self.assertEqual(claudeskills.created_links(self.state()), {"alpha", "beta"})

    def test_record_merges_rather_than_replaces(self):
        claudeskills.record_created(["alpha"], self.state())
        claudeskills.record_created(["beta"], self.state())
        self.assertEqual(claudeskills.created_links(self.state()), {"alpha", "beta"})

    def test_record_is_idempotent(self):
        claudeskills.record_created(["alpha"], self.state())
        claudeskills.record_created(["alpha"], self.state())
        self.assertEqual(claudeskills.created_links(self.state()), {"alpha"})

    def test_record_leaves_no_tmp_file_behind(self):
        claudeskills.record_created(["alpha"], self.state())
        self.assertFalse(os.path.exists(self.state() + ".tmp"))

    def test_seed_claims_exactly_declared_intersect_live(self):
        """Links matching a declared row are provably Lodestar-managed; nothing else is."""
        target = self.make_skill("dev/Alpha/.claude/skills/alpha")
        rows = [claudeskills.Row("alpha", target, "Alpha", "all"),
                claudeskills.Row("ghost", os.path.join(self.tmp, "nope"), "", "all")]
        os.symlink(target, os.path.join(self.live, "alpha"))
        os.symlink(target, os.path.join(self.live, "stray"))   # live but undeclared
        count = claudeskills.seed_created(rows, "box-a", self.live, self.state())
        self.assertEqual(count, 1)
        self.assertEqual(claudeskills.created_links(self.state()), {"alpha"})

    def test_seed_does_not_claim_links_for_other_machines(self):
        target = self.make_skill("dev/Beta")
        rows = [claudeskills.Row("beta", target, "Beta", "box-b")]
        os.symlink(target, os.path.join(self.live, "beta"))
        claudeskills.seed_created(rows, "box-a", self.live, self.state())
        self.assertEqual(claudeskills.created_links(self.state()), set())

    def test_seed_is_a_noop_once_the_file_exists(self):
        """Seeding twice must not re-claim links a human deliberately removed from the file."""
        claudeskills.record_created(["alpha"], self.state())
        target = self.make_skill("dev/Gamma", name="gamma")
        rows = [claudeskills.Row("gamma", target, "Gamma", "all")]
        os.symlink(target, os.path.join(self.live, "gamma"))
        self.assertEqual(claudeskills.seed_created(rows, "box-a", self.live, self.state()), 0)
        self.assertEqual(claudeskills.created_links(self.state()), {"alpha"})

    def test_remove_drops_from_the_owned_set(self):
        """The only production subtractive mutator of the owned set, previously untested."""
        claudeskills.record_created(["alpha", "beta"], self.state())
        claudeskills.remove_created(["alpha"], self.state())
        self.assertEqual(claudeskills.created_links(self.state()), {"beta"})

    def test_remove_of_an_unowned_name_is_a_noop(self):
        claudeskills.record_created(["alpha"], self.state())
        claudeskills.remove_created(["ghost"], self.state())
        self.assertEqual(claudeskills.created_links(self.state()), {"alpha"})

    def test_remove_with_empty_names_writes_nothing(self):
        claudeskills.remove_created([], self.state())
        self.assertFalse(os.path.exists(self.state()))

    def test_remove_against_a_missing_file_leaves_the_owned_set_empty(self):
        """Owns nothing already; removing must not fabricate ownership of anything."""
        claudeskills.remove_created(["alpha"], self.state())
        self.assertEqual(claudeskills.created_links(self.state()), set())


class TestPruneJournalling(SkillsTest):
    def setUp(self):
        super().setUp()
        self.seen = []
        self.target = self.make_skill("dev/Stray")
        self.link = os.path.join(self.live, "stray")
        os.symlink(self.target, self.link)
        self.action = claudeskills.Action("prune", "stray", "not in the table")

    def test_successful_prune_reports_skill_and_target(self):
        claudeskills.apply([self.action], self.live, {}, allow_prune=True,
                           on_prune=lambda s, t: self.seen.append((s, t)))
        self.assertEqual(self.seen, [("stray", self.target)])

    def test_dry_run_journals_nothing(self):
        claudeskills.apply([self.action], self.live, {}, allow_prune=True, dry_run=True,
                           on_prune=lambda s, t: self.seen.append((s, t)))
        self.assertEqual(self.seen, [])
        self.assertTrue(os.path.islink(self.link))

    def test_prune_not_allowed_journals_nothing(self):
        claudeskills.apply([self.action], self.live, {}, allow_prune=False,
                           on_prune=lambda s, t: self.seen.append((s, t)))
        self.assertEqual(self.seen, [])

    def test_a_failing_journal_write_does_not_fail_the_prune(self):
        """Bookkeeping must never break the operation, per record_prunes' own contract."""
        def boom(_skill, _target):
            raise RuntimeError("journal unavailable")
        result = claudeskills.apply([self.action], self.live, {}, allow_prune=True,
                                    on_prune=boom)
        self.assertIn("pruned stray", result)
        self.assertEqual(result.faults, [])
        self.assertFalse(os.path.exists(self.link))

    def test_no_on_prune_callback_is_fine(self):
        result = claudeskills.apply([self.action], self.live, {}, allow_prune=True)
        self.assertIn("pruned stray", result)


class TestCreateProvenanceHook(SkillsTest):
    """apply()'s on_create, mirroring on_prune. Important 1 in the 2026-09-03 review.

    `record_created` had zero production call sites: links were created at the `create` and
    `repoint` branches below without ever being recorded, so on a machine where
    `~/.claude/skills` did not exist yet -- the fresh-machine case D18 was written for --
    `seed_created` claimed 0, `created_links()` returned `set()` forever, and prune became
    dead code there. `on_create` closes that the same way `on_prune` already closes the
    matching gap on removal.
    """

    def setUp(self):
        super().setUp()
        self.seen = []
        self.target = self.make_skill("alpha-src")
        self.row = claudeskills.Row("alpha", self.target, "", "all")

    def test_a_successful_create_records_provenance(self):
        actions = [claudeskills.Action("create", "alpha", "link -> %s" % self.target)]
        claudeskills.apply(actions, self.live, {"alpha": self.row},
                           on_create=lambda skill: self.seen.append(skill))
        self.assertEqual(self.seen, ["alpha"])

    def test_a_successful_repoint_records_provenance(self):
        other = self.make_skill("other-src", name="other")
        os.symlink(other, os.path.join(self.live, "alpha"))
        actions = [claudeskills.Action("repoint", "alpha", "points elsewhere")]
        claudeskills.apply(actions, self.live, {"alpha": self.row},
                           on_create=lambda skill: self.seen.append(skill))
        self.assertEqual(self.seen, ["alpha"])

    def test_dry_run_records_nothing(self):
        """dry_run must stay fully inert -- no provenance mutation either."""
        actions = [claudeskills.Action("create", "alpha", "link -> %s" % self.target)]
        claudeskills.apply(actions, self.live, {"alpha": self.row}, dry_run=True,
                           on_create=lambda skill: self.seen.append(skill))
        self.assertEqual(self.seen, [])

    def test_no_callback_is_fine(self):
        actions = [claudeskills.Action("create", "alpha", "link -> %s" % self.target)]
        done = claudeskills.apply(actions, self.live, {"alpha": self.row})
        self.assertIn("linked alpha", done)

    def test_a_failing_callback_does_not_fail_the_create(self):
        """The link already exists by then; bookkeeping must not read back as a failure."""
        def boom(_skill):
            raise RuntimeError("provenance unavailable")
        actions = [claudeskills.Action("create", "alpha", "link -> %s" % self.target)]
        done = claudeskills.apply(actions, self.live, {"alpha": self.row}, on_create=boom)
        self.assertIn("linked alpha", done)
        self.assertEqual(done.faults, [])
        self.assertTrue(os.path.islink(os.path.join(self.live, "alpha")))

    def test_prune_never_triggers_on_create(self):
        """The callback must be kind-specific: a prune must never invoke on_create."""
        os.symlink(self.target, os.path.join(self.live, "alpha"))
        actions = [claudeskills.Action("prune", "alpha", "not in the table")]
        claudeskills.apply(actions, self.live, {}, allow_prune=True,
                           on_create=lambda skill: self.seen.append(skill))
        self.assertEqual(self.seen, [])


NOT_GLOBAL_TABLE = """# Skills

| Skill | Target | Repo | Machines |
|-------|--------|------|----------|
| alpha | ~/dev/Alpha/.claude/skills/alpha | Alpha | all |

## Not global

- `Mod/skills/*` — project-scoped modding skills; global install would fire them
  on unrelated work.
- `Proj/docs/skill-drafts/*` — drafts, never authored into skills.
"""


class TestNotGlobal(SkillsTest):
    def write(self, body):
        path = os.path.join(self.tmp, "ng.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        return path

    def test_entries_parse_with_pattern_and_reason(self):
        entries, faults = claudeskills.not_global(self.write(NOT_GLOBAL_TABLE))
        self.assertEqual([p for p, _ in entries], ["Mod/skills/*", "Proj/docs/skill-drafts/*"])
        self.assertEqual(faults, [])

    def test_multi_line_reason_is_joined(self):
        entries, _ = claudeskills.not_global(self.write(NOT_GLOBAL_TABLE))
        self.assertIn("unrelated work", dict(entries)["Mod/skills/*"])

    def test_entry_without_a_reason_is_a_fault(self):
        body = NOT_GLOBAL_TABLE + "\n- `Bare/skills/*`\n"
        entries, faults = claudeskills.not_global(self.write(body))
        self.assertEqual(len(faults), 1)
        self.assertIn("Bare/skills/*", faults[0])
        self.assertNotIn("Bare/skills/*", [p for p, _ in entries])

    def test_a_bullet_without_backticks_is_a_fault_not_a_silent_skip(self):
        """The failure Important 5 describes: no backticks -> no match -> the old code just
        dropped the line, suppressing nothing while reading as protection that isn't there."""
        body = NOT_GLOBAL_TABLE + "\n- RimMaster/skills/* — no backticks around the pattern\n"
        entries, faults = claudeskills.not_global(self.write(body))
        self.assertEqual(len(faults), 1)
        self.assertIn("RimMaster/skills/*", faults[0])
        self.assertNotIn("RimMaster/skills/*", [p for p, _ in entries])
        # The two well-formed entries before it must still parse.
        self.assertEqual([p for p, _ in entries], ["Mod/skills/*", "Proj/docs/skill-drafts/*"])

    def test_no_section_means_no_entries_and_no_faults(self):
        entries, faults = claudeskills.not_global(self.write(TABLE))
        self.assertEqual((entries, faults), ([], []))

    def test_scan_suppresses_matching_paths_and_counts_them(self):
        self.make_skill("dev/Mod/skills/one", name="one")
        self.make_skill("dev/Mod/skills/two", name="two")
        keep = self.make_skill("dev/Keep", name="keep")
        found, unused, suppressed = claudeskills.scan_unlisted(
            os.path.join(self.tmp, "dev"), [], ignored=["Mod/skills/*"])
        self.assertEqual(found, [keep])
        self.assertEqual(suppressed, 2)
        self.assertEqual(unused, [])

    def test_a_pattern_matching_nothing_is_reported(self):
        self.make_skill("dev/Keep", name="keep")
        _found, unused, _suppressed = claudeskills.scan_unlisted(
            os.path.join(self.tmp, "dev"), [], ignored=["Typo/skills/*"])
        self.assertEqual(unused, ["Typo/skills/*"])

    def test_declared_targets_are_still_excluded(self):
        target = self.make_skill("dev/Alpha/.claude/skills/alpha")
        rows = [claudeskills.Row("alpha", target, "Alpha", "all")]
        found, _unused, _s = claudeskills.scan_unlisted(os.path.join(self.tmp, "dev"), rows)
        self.assertEqual(found, [])

    def test_unlinked_wrapper_keeps_its_old_contract(self):
        keep = self.make_skill("dev/Keep", name="keep")
        self.assertEqual(claudeskills.unlinked(os.path.join(self.tmp, "dev"), []), [keep])


class RelocateTargetsTest(unittest.TestCase):
    """A `Machine paths` override must move the skill target with the checkout."""

    def row(self, target, repo="review-sheet"):
        return claudeskills.Row(skill="review-sheets", target=target, repo=repo, machines="all")

    def test_an_overridden_checkout_moves_the_target(self):
        rows = [self.row("/h/dev/review-sheet")]
        out = claudeskills.relocate_targets(rows, {"review-sheet": "/h/dev/review-sheet"},
                                            {"review-sheet": "/h/dev/review-sheets"})
        self.assertEqual(out[0].target, "/h/dev/review-sheets")

    def test_a_subdirectory_target_keeps_its_tail(self):
        rows = [self.row("/h/dev/Big/skills/x", repo="Big")]
        out = claudeskills.relocate_targets(rows, {"Big": "/h/dev/Big"}, {"Big": "/h/dev/Huge"})
        self.assertEqual(out[0].target, "/h/dev/Huge/skills/x")

    def test_a_sibling_with_a_shared_prefix_is_not_rewritten(self):
        rows = [self.row("/h/dev/review-sheet-extra/x")]
        out = claudeskills.relocate_targets(rows, {"review-sheet": "/h/dev/review-sheet"},
                                            {"review-sheet": "/h/dev/review-sheets"})
        self.assertEqual(out[0].target, "/h/dev/review-sheet-extra/x")

    def test_no_override_and_no_repo_leave_the_row_alone(self):
        rows = [self.row("/h/dev/review-sheet"), self.row("/h/dev/loose", repo="")]
        out = claudeskills.relocate_targets(rows, {"review-sheet": "/h/dev/review-sheet"},
                                            {"review-sheet": "/h/dev/review-sheet"})
        self.assertEqual(out, rows)

    def test_the_sweeps_skills_step_routes_through_it(self):
        import inspect
        import claudeconf
        self.assertIn("relocate_targets", inspect.getsource(claudeconf.skills_state))


class HarnessDirsTest(SkillsTest):
    def test_the_harness_synced_store_is_not_reported_as_unmanaged(self):
        os.makedirs(os.path.join(self.live, "synced", "bucket"))
        actions = claudeskills.plan([], "k", self.live, set(), created=set())
        self.assertEqual([a for a in actions if a.skill == "synced"], [])


class SymlinkedRootTest(SkillsTest):
    def test_a_prune_root_that_is_itself_a_symlink_still_contains_its_links(self):
        real = os.path.join(self.tmp, "realdev")
        os.makedirs(os.path.join(real, "repo"))
        alias = os.path.join(self.tmp, "dev")
        os.symlink(real, alias)
        self.assertTrue(claudeskills._prunable(os.path.join(alias, "repo"), [alias]))
        self.assertTrue(claudeskills._prunable(os.path.join(real, "repo"), [alias]))
        self.assertFalse(claudeskills._prunable(os.path.join(self.tmp, "elsewhere"), [alias]))
