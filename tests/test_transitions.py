"""Tests for lib/transitions.py — the record that makes an interrupted move recoverable.

Every test writes to a temp path via the `path` argument. The real store lives at
`~/dev/.lodestar/lifecycle-transitions.json` and its own docstring says it is not safe to
delete, so a test that touched it could destroy a genuine in-flight record.

The properties worth guarding, in order of how expensive getting them wrong is:

1. A **fingerprint mismatch dominates** — a superseded plan stops even at `NOT_STARTED`.
2. **`BOTH` stops unconditionally**, matching fingerprint or not.
3. An **unreadable store raises** rather than reading as "nothing in flight".
"""

import json
import os
import shutil
import tempfile
import unittest

from lib import transitions


def a_transition(**over):
    base = dict(plan_id="rename-alpha-20260930", repo_id="alpha", kind="rename",
                old_path="/tmp/nope/Alpha", new_path="/tmp/nope/Beta",
                holding_path="", fingerprint="abc123", started_at="2026-09-30T10:00-07:00")
    base.update(over)
    return transitions.Transition(**base)


class TransitionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-transitions-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "lifecycle-transitions.json")

    def a_dir(self, name):
        target = os.path.join(self.tmp, name)
        os.makedirs(target, exist_ok=True)
        return target


class TestObserve(TransitionTest):
    def test_only_at_old_path_is_not_started(self):
        old = self.a_dir("Alpha")
        new = os.path.join(self.tmp, "Beta")
        self.assertEqual(transitions.observe(old, new), transitions.NOT_STARTED)

    def test_only_at_new_path_is_moved(self):
        old = os.path.join(self.tmp, "Alpha")
        new = self.a_dir("Beta")
        self.assertEqual(transitions.observe(old, new), transitions.MOVED)

    def test_at_both_paths_is_both(self):
        self.assertEqual(
            transitions.observe(self.a_dir("Alpha"), self.a_dir("Beta")), transitions.BOTH)

    def test_at_neither_path_is_neither(self):
        self.assertEqual(
            transitions.observe(os.path.join(self.tmp, "Alpha"),
                                os.path.join(self.tmp, "Beta")), transitions.NEITHER)

    def test_a_dangling_symlink_at_the_destination_counts_as_present(self):
        """`exists` follows the link and reports the destination free. `lexists` does not.

        An occupied destination that measures as empty is how a move lands on top of
        something, so this is a correctness property and not a nicety.
        """
        old = self.a_dir("Alpha")
        new = os.path.join(self.tmp, "Beta")
        os.symlink(os.path.join(self.tmp, "gone-nowhere"), new)

        self.assertFalse(os.path.exists(new), "fixture should be a DANGLING link")
        self.assertTrue(os.path.lexists(new))
        self.assertEqual(transitions.observe(old, new), transitions.BOTH)


class TestStore(TransitionTest):
    def test_missing_file_loads_as_empty(self):
        self.assertEqual(transitions.load(self.path), {})

    def test_record_then_load_round_trips(self):
        transitions.record(a_transition(), self.path)
        store = transitions.load(self.path)
        self.assertEqual(sorted(store), ["rename-alpha-20260930"])
        row = store["rename-alpha-20260930"]
        self.assertEqual(row.new_path, "/tmp/nope/Beta")
        self.assertEqual(row.fingerprint, "abc123")
        self.assertEqual(row.finished_at, "")
        self.assertEqual(row.repo_id, "alpha",
                         "the repo id is still carried; only the KEY moved to the plan id")

    def test_recording_the_same_plan_again_supersedes_rather_than_duplicating(self):
        transitions.record(a_transition(), self.path)
        transitions.record(a_transition(new_path="/tmp/nope/Gamma", fingerprint="def456"),
                           self.path)
        store = transitions.load(self.path)
        self.assertEqual(len(store), 1)
        self.assertEqual(store["rename-alpha-20260930"].new_path, "/tmp/nope/Gamma")

    def test_finish_stamps_and_keeps_the_row(self):
        transitions.record(a_transition(), self.path)
        self.assertTrue(transitions.finish("rename-alpha-20260930",
                                           at="2026-09-30T11:00-07:00", path=self.path))
        store = transitions.load(self.path)
        self.assertIn("rename-alpha-20260930", store,
                      "the row is KEPT so 'completed here' stays distinguishable from "
                      "'never attempted here'")
        self.assertEqual(store["rename-alpha-20260930"].finished_at,
                         "2026-09-30T11:00-07:00")
        self.assertEqual(transitions.in_flight(store), {})

    def test_finish_on_an_unknown_id_is_false_not_an_error(self):
        self.assertFalse(transitions.finish("nobody", path=self.path))

    def test_forget_drops_the_row(self):
        transitions.record(a_transition(), self.path)
        self.assertTrue(transitions.forget("rename-alpha-20260930", self.path))
        self.assertEqual(transitions.load(self.path), {})
        self.assertFalse(transitions.forget("rename-alpha-20260930", self.path))

    def test_in_flight_excludes_finished(self):
        transitions.record(a_transition(plan_id="p-alpha", repo_id="alpha"), self.path)
        transitions.record(a_transition(plan_id="p-beta", repo_id="beta"), self.path)
        transitions.finish("p-alpha", path=self.path)
        self.assertEqual(sorted(transitions.in_flight(transitions.load(self.path))),
                         ["p-beta"])

    def test_record_refuses_a_transition_with_no_repo_id(self):
        with self.assertRaises(ValueError):
            transitions.record(a_transition(repo_id=""), self.path)

    def test_record_refuses_a_transition_with_no_plan_id(self):
        """The store is keyed by it, so an unnamed plan would land under an empty key."""
        with self.assertRaises(ValueError):
            transitions.record(a_transition(plan_id=""), self.path)

    def test_a_crash_mid_write_cannot_truncate_the_store(self):
        """Writes go through tmp + os.replace, so no reader ever sees a partial file."""
        transitions.record(a_transition(), self.path)
        leftovers = [n for n in os.listdir(self.tmp) if ".tmp" in n]
        self.assertEqual(leftovers, [], "the tmp file must be renamed away, not left behind")


class TestUnreadableStoreRaises(TransitionTest):
    """An empty result here reads as 'no material is in flight'.

    This store is the only record of where quarantined material was moved to, so reading a
    corrupt file as `{}` silently converts "it is at a recorded holding location" into "there
    is none". That is the most expensive mistake available in this module, so it raises.
    """

    def test_corrupt_json_raises_rather_than_reading_as_empty(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json at all")
        with self.assertRaises(transitions.Unreadable):
            transitions.load(self.path)

    def test_a_json_list_raises_rather_than_reading_as_empty(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(["alpha"], handle)
        with self.assertRaises(transitions.Unreadable):
            transitions.load(self.path)

    def test_a_non_object_entry_raises_rather_than_being_skipped(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"alpha": "just a string"}, handle)
        with self.assertRaises(transitions.Unreadable):
            transitions.load(self.path)


class TestRecoveryAction(TransitionTest):
    def test_not_started_with_a_matching_fingerprint_proceeds(self):
        verdict = transitions.recovery_action(
            a_transition(), transitions.NOT_STARTED, "abc123")
        self.assertEqual(verdict["action"], transitions.PROCEED)

    def test_moved_with_a_matching_fingerprint_verifies_rather_than_completing(self):
        """A moved directory is not a completed rename — satellites are checked separately."""
        verdict = transitions.recovery_action(a_transition(), transitions.MOVED, "abc123")
        self.assertEqual(verdict["action"], transitions.VERIFY)

    def test_neither_with_a_holding_path_consults_it_by_name(self):
        verdict = transitions.recovery_action(
            a_transition(holding_path="/tmp/nope/.quarantine/alpha"),
            transitions.NEITHER, "abc123")
        self.assertEqual(verdict["action"], transitions.CONSULT)
        self.assertIn("/tmp/nope/.quarantine/alpha", verdict["reason"])

    def test_neither_with_no_holding_path_still_refuses_to_guess(self):
        verdict = transitions.recovery_action(
            a_transition(holding_path=""), transitions.NEITHER, "abc123")
        self.assertEqual(verdict["action"], transitions.CONSULT)
        self.assertIn("person", verdict["reason"])

    def test_an_unknown_observed_state_raises(self):
        with self.assertRaises(ValueError):
            transitions.recovery_action(a_transition(), "sideways", "abc123")


class TestBothStopsUnconditionally(TransitionTest):
    """Two directories claiming one id is a conflict about the WORLD.

    A current plan does not make it safe: there is no evidence here about which copy holds the
    work, so picking a winner destroys whichever one loses. Same refusal `lib/enroll.py` makes.
    """

    def test_both_stops_even_when_the_fingerprint_matches(self):
        verdict = transitions.recovery_action(a_transition(), transitions.BOTH, "abc123")
        self.assertEqual(verdict["action"], transitions.STOP)

    def test_both_stops_when_the_fingerprint_does_not_match(self):
        verdict = transitions.recovery_action(a_transition(), transitions.BOTH, "different")
        self.assertEqual(verdict["action"], transitions.STOP)

    def test_the_stop_reason_names_both_paths_so_a_human_can_look(self):
        verdict = transitions.recovery_action(a_transition(), transitions.BOTH, "abc123")
        self.assertIn("/tmp/nope/Alpha", verdict["reason"])
        self.assertIn("/tmp/nope/Beta", verdict["reason"])


class TestSupersededPlanDominates(TransitionTest):
    """🔴 The invariant that costs the most when inverted.

    The spec: *"an old transition record is evidence, not permission to finish a rehome that
    current intent has superseded."* If the owner changed the destination after the record was
    written, finishing the recorded move completes a decision he has revoked. So a mismatch
    stops even at `NOT_STARTED`, which is the one observed state where proceeding would
    otherwise be exactly right.
    """

    def test_a_changed_fingerprint_stops_at_not_started(self):
        verdict = transitions.recovery_action(
            a_transition(fingerprint="written-then"), transitions.NOT_STARTED, "current-now")
        self.assertEqual(
            verdict["action"], transitions.STOP,
            "NOT_STARTED + superseded plan must STOP, not proceed — this is the whole "
            "'evidence, not permission' rule")

    def test_a_changed_fingerprint_stops_at_moved(self):
        verdict = transitions.recovery_action(
            a_transition(fingerprint="written-then"), transitions.MOVED, "current-now")
        self.assertEqual(verdict["action"], transitions.STOP)

    def test_a_changed_fingerprint_stops_at_neither(self):
        verdict = transitions.recovery_action(
            a_transition(fingerprint="written-then", holding_path="/tmp/held"),
            transitions.NEITHER, "current-now")
        self.assertEqual(verdict["action"], transitions.STOP)

    def test_the_stop_reason_names_both_fingerprints(self):
        verdict = transitions.recovery_action(
            a_transition(fingerprint="written-then"), transitions.NOT_STARTED, "current-now")
        self.assertIn("written-then", verdict["reason"])
        self.assertIn("current-now", verdict["reason"])

    def test_an_absent_current_fingerprint_does_not_manufacture_a_mismatch(self):
        """A caller that cannot supply a current fingerprint must not be told the plan changed.

        Otherwise every recovery on a repo whose plan row has been tidied away reads as
        'superseded' and nothing is ever recoverable.
        """
        verdict = transitions.recovery_action(
            a_transition(fingerprint="written-then"), transitions.NOT_STARTED, "")
        self.assertEqual(verdict["action"], transitions.PROCEED)


class TestKeyedByPlanNotRepo(TransitionTest):
    """The re-key, and the wedge it exists to end.

    Measured 2026-10-01: a rehome and a quarantine were both approved for one throwaway repo.
    Plan ids sort `quarantine` first, so it converged and FINISHED its record; the rehome then
    ran in the same pass against the now-missing directory, wrote its own record over the top,
    and failed. The leftover unfinished record then made every later plan for that repo report
    `STOP` — "an old record is evidence that something was started, not permission to finish an
    intent that has been superseded" — and a person had to clear it by hand.

    So the property is specifically: **a FINISHED record for one plan must not obstruct a
    different plan for the same repo.**
    """

    def test_two_plans_for_one_repo_keep_separate_records(self):
        transitions.record(a_transition(plan_id="quarantine-thing", kind="quarantine",
                                        repo_id="thing"), self.path)
        transitions.finish("quarantine-thing", path=self.path)
        transitions.record(a_transition(plan_id="rehome-thing", kind="rehome",
                                        repo_id="thing"), self.path)
        store = transitions.load(self.path)
        self.assertEqual(sorted(store), ["quarantine-thing", "rehome-thing"],
                         "the finished quarantine's record must survive the rehome's")
        self.assertTrue(store["quarantine-thing"].finished_at)
        self.assertFalse(store["rehome-thing"].finished_at)

    def test_a_finished_record_for_another_plan_does_not_conflict(self):
        """The wedge, stated as the one question the converger asks."""
        transitions.record(a_transition(plan_id="quarantine-thing", repo_id="thing"),
                           self.path)
        transitions.finish("quarantine-thing", path=self.path)
        store = transitions.load(self.path)
        self.assertIsNone(transitions.conflicting(store, "rehome-thing", "thing"))

    def test_an_unfinished_record_for_another_plan_DOES_conflict(self):
        """The replacement for the protection the single repo-keyed slot gave by accident.

        Two transitions in flight for one repo can each be the other's source or destination,
        so this has to be refused on purpose now that the key no longer refuses it for us.
        """
        transitions.record(a_transition(plan_id="quarantine-thing", repo_id="thing"),
                           self.path)
        store = transitions.load(self.path)
        clash = transitions.conflicting(store, "rehome-thing", "thing")
        self.assertIsNotNone(clash)
        self.assertEqual(clash.plan_id, "quarantine-thing")

    def test_a_plans_own_unfinished_record_is_not_a_conflict_with_itself(self):
        """Otherwise re-running one plan after an interruption could never resume."""
        transitions.record(a_transition(plan_id="rehome-thing", repo_id="thing"), self.path)
        store = transitions.load(self.path)
        self.assertIsNone(transitions.conflicting(store, "rehome-thing", "thing"))

    def test_an_unfinished_record_for_a_DIFFERENT_repo_is_not_a_conflict(self):
        transitions.record(a_transition(plan_id="rename-other", repo_id="other"), self.path)
        store = transitions.load(self.path)
        self.assertIsNone(transitions.conflicting(store, "rehome-thing", "thing"))

    def test_record_RAISES_rather_than_overwriting_a_rival_plans_record(self):
        """`record` is called before every mutation, so the refusal lands before the act.

        That placement is the whole safety argument: a gate whose only failure mode is an
        exception must sit on the near side of the irreversible step — the lesson
        `commandexec.unreceiptable` was built from.
        """
        transitions.record(a_transition(plan_id="quarantine-thing", repo_id="thing"),
                           self.path)
        with self.assertRaises(transitions.Conflict) as caught:
            transitions.record(a_transition(plan_id="rehome-thing", repo_id="thing"),
                               self.path)
        message = str(caught.exception)
        self.assertIn("quarantine-thing", message)
        self.assertIn("rehome-thing", message)
        store = transitions.load(self.path)
        self.assertEqual(sorted(store), ["quarantine-thing"],
                         "the refused write must leave the existing record untouched")

    def test_a_row_written_under_the_old_repo_key_is_re_keyed_on_load(self):
        """No hand migration. The `plan_id` field predates this change and is used as the key.

        A real store was once this shape — one row under the key
        `repo-a` carrying `plan_id: rename-repo-a-20260101`.
        """
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"repo-a": {
                "plan_id": "rename-repo-a-20260101", "repo_id": "repo-a",
                "kind": "rename", "old_path": "/x/RepoA", "new_path": "/x/RepoA2",
                "holding_path": "", "fingerprint": "0123456789abcdef",
                "started_at": "2026-01-01T09:00:00-08:00",
                "finished_at": "2026-01-01T10:00:00-08:00"}}, handle)
        store = transitions.load(self.path)
        self.assertEqual(sorted(store), ["rename-repo-a-20260101"])
        self.assertEqual(store["rename-repo-a-20260101"].repo_id, "repo-a")

    def test_a_legacy_row_with_no_plan_id_keeps_the_files_key_rather_than_vanishing(self):
        """A row predating `plan_id`, or hand-written. Dropping it would lose a holding path."""
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"ancient": {"kind": "quarantine",
                                   "holding_path": "/x/.lodestar/quarantine/ancient"}}, handle)
        store = transitions.load(self.path)
        self.assertEqual(sorted(store), ["ancient"])
        self.assertEqual(store["ancient"].repo_id, "ancient")
        self.assertEqual(store["ancient"].holding_path, "/x/.lodestar/quarantine/ancient")

    def test_two_rows_resolving_to_one_plan_id_raise_rather_than_one_winning(self):
        """Either row may be the only record of where that material went."""
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"a": {"plan_id": "p", "repo_id": "a", "holding_path": "/x/a"},
                       "b": {"plan_id": "p", "repo_id": "b", "holding_path": "/x/b"}}, handle)
        with self.assertRaises(transitions.Unreadable):
            transitions.load(self.path)


if __name__ == "__main__":
    unittest.main()
