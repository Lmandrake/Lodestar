"""Tests for lib/plans.py — plans, fingerprints and approvals for repo lifecycle ops.

The load-bearing property under test throughout is the fingerprint's field split
(design doc §3C): it must cover `kind`, `repo_id`, `machines` (order-independent),
`old`, `new`, `disposition`, and it must NOT cover `plan_id`, `created_at`,
`created_by` or `note`. Both directions are asserted explicitly, field by field,
because a test that only checks "the covered fields matter" would stay green if an
uncovered field were accidentally folded in too.
"""

import os
import shutil
import tempfile
import unittest

from lib import plans


def make_plan(**overrides):
    base = dict(
        plan_id="rename-widget-20260930",
        kind="rename",
        repo_id="widget",
        machines=("mt-001", "mt-002"),
        old="Widget",
        new="Gadget",
        disposition="",
        created_at="2026-09-30T10:00:00-07:00",
        created_by="mt-001",
        note="renaming for clarity",
    )
    base.update(overrides)
    return plans.Plan(**base)


class PlansTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-plans-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.plans_path = os.path.join(self.tmp, "plans.md")
        self.approvals_path = os.path.join(self.tmp, "approvals.md")


class TestFingerprintCoveredFields(PlansTest):
    """Mutating any of these must change the fingerprint."""

    def test_kind_changes_fingerprint(self):
        a = make_plan(kind="rename")
        b = make_plan(kind="rehome")
        self.assertNotEqual(plans.fingerprint(a), plans.fingerprint(b))

    def test_repo_id_changes_fingerprint(self):
        a = make_plan(repo_id="widget")
        b = make_plan(repo_id="gadget")
        self.assertNotEqual(plans.fingerprint(a), plans.fingerprint(b))

    def test_machines_membership_changes_fingerprint(self):
        a = make_plan(machines=("mt-001",))
        b = make_plan(machines=("mt-001", "mt-002"))
        self.assertNotEqual(plans.fingerprint(a), plans.fingerprint(b))

    def test_old_changes_fingerprint(self):
        a = make_plan(old="Widget")
        b = make_plan(old="OldWidget")
        self.assertNotEqual(plans.fingerprint(a), plans.fingerprint(b))

    def test_new_changes_fingerprint(self):
        a = make_plan(new="Gadget")
        b = make_plan(new="Gizmo")
        self.assertNotEqual(plans.fingerprint(a), plans.fingerprint(b))

    def test_disposition_changes_fingerprint(self):
        a = make_plan(kind="quarantine", disposition="")
        b = make_plan(kind="quarantine", disposition="purge")
        self.assertNotEqual(plans.fingerprint(a), plans.fingerprint(b))

    def test_machine_order_does_not_matter(self):
        a = make_plan(machines=("mt-001", "mt-002"))
        b = make_plan(machines=("mt-002", "mt-001"))
        self.assertEqual(plans.fingerprint(a), plans.fingerprint(b))


class TestFingerprintUncoveredFields(PlansTest):
    """Mutating any of these must NOT change the fingerprint."""

    def test_plan_id_does_not_change_fingerprint(self):
        a = make_plan(plan_id="rename-widget-20260930")
        b = make_plan(plan_id="rename-widget-20261001")
        self.assertEqual(plans.fingerprint(a), plans.fingerprint(b))

    def test_created_at_does_not_change_fingerprint(self):
        a = make_plan(created_at="2026-09-30T10:00:00-07:00")
        b = make_plan(created_at="2026-10-15T22:00:00-07:00")
        self.assertEqual(plans.fingerprint(a), plans.fingerprint(b))

    def test_created_by_does_not_change_fingerprint(self):
        a = make_plan(created_by="mt-001")
        b = make_plan(created_by="mt-002")
        self.assertEqual(plans.fingerprint(a), plans.fingerprint(b))

    def test_note_does_not_change_fingerprint(self):
        a = make_plan(note="renaming for clarity")
        b = make_plan(note="totally different rationale, unrelated edit")
        self.assertEqual(plans.fingerprint(a), plans.fingerprint(b))

    def test_unrelated_field_change_bundle_leaves_fingerprint_stable(self):
        # An "unrelated journal or board commit" in spec language: everything that
        # isn't the act itself changes at once, and the fingerprint must not move.
        a = make_plan()
        b = make_plan(plan_id="rename-widget-20261225",
                       created_at="2027-01-01T00:00:00-07:00",
                       created_by="tower",
                       note="")
        self.assertEqual(plans.fingerprint(a), plans.fingerprint(b))


class TestFingerprintValidation(PlansTest):
    def test_unknown_kind_raises(self):
        bad = make_plan(kind="teleport")
        with self.assertRaises(ValueError):
            plans.fingerprint(bad)

    def test_make_plan_id_rejects_unknown_kind(self):
        with self.assertRaises(ValueError):
            plans.make_plan_id("teleport", "widget", "2026-09-30T10:00:00-07:00")

    def test_make_plan_id_shape(self):
        plan_id = plans.make_plan_id("rename", "OpenCodeCLI", "2026-09-30T10:00:00-07:00")
        self.assertEqual(plan_id, "rename-opencodecli-20260930")


class TestApproved(PlansTest):
    def test_no_approval_recorded(self):
        plan = make_plan()
        ok, reason = plans.approved(plan, {})
        self.assertFalse(ok)
        self.assertIn("no approval", reason)
        self.assertIn(plan.plan_id, reason)

    def test_matching_approval_is_approved(self):
        plan = make_plan()
        approval = plans.Approval(
            plan_id=plan.plan_id,
            fingerprint=plans.fingerprint(plan),
            machine="mt-001",
            at="2026-09-30T10:05:00-07:00",
            by="mandrake",
        )
        ok, reason = plans.approved(plan, {plan.plan_id: approval})
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_lapsed_approval_names_the_lapse(self):
        original = make_plan()
        approval = plans.Approval(
            plan_id=original.plan_id,
            fingerprint=plans.fingerprint(original),
            machine="mt-001",
            at="2026-09-30T10:05:00-07:00",
            by="mandrake",
        )
        # The destination changed after approval -- the fingerprint must no longer
        # match, and the reason must say "lapsed", not just "not approved".
        revised = make_plan(new="SomethingElse")
        ok, reason = plans.approved(revised, {original.plan_id: approval})
        self.assertFalse(ok)
        self.assertIn("lapsed", reason)
        self.assertNotIn("no approval", reason)

    def test_lapse_vs_missing_reasons_are_distinguishable(self):
        plan = make_plan()
        _, missing_reason = plans.approved(plan, {})

        approval = plans.Approval(
            plan_id=plan.plan_id,
            fingerprint="0000000000000000",
            machine="mt-001",
            at="2026-09-30T10:05:00-07:00",
            by="mandrake",
        )
        _, lapsed_reason = plans.approved(plan, {plan.plan_id: approval})
        self.assertNotEqual(missing_reason, lapsed_reason)


class TestAppendAndLoadPlans(PlansTest):
    def test_missing_file_loads_as_empty(self):
        self.assertEqual(plans.load_plans(self.plans_path), {})

    def test_append_then_load_round_trips(self):
        plan = make_plan()
        ok = plans.append_plan(plan, path=self.plans_path)
        self.assertTrue(ok)

        loaded = plans.load_plans(self.plans_path)
        self.assertIn(plan.plan_id, loaded)
        got = loaded[plan.plan_id]
        self.assertEqual(got.plan_id, plan.plan_id)
        self.assertEqual(got.kind, plan.kind)
        self.assertEqual(got.repo_id, plan.repo_id)
        self.assertEqual(set(got.machines), set(plan.machines))
        self.assertEqual(got.old, plan.old)
        self.assertEqual(got.new, plan.new)
        self.assertEqual(got.disposition, plan.disposition)
        self.assertEqual(got.created_at, plan.created_at)
        self.assertEqual(got.created_by, plan.created_by)
        self.assertEqual(got.note, plan.note)

    def test_duplicate_plan_id_append_returns_false(self):
        plan = make_plan()
        first = plans.append_plan(plan, path=self.plans_path)
        second = plans.append_plan(plan, path=self.plans_path)
        self.assertTrue(first)
        self.assertFalse(second)
        with open(self.plans_path, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertEqual(text.count(plan.plan_id), 1)

    def test_append_creates_file_with_header(self):
        plans.append_plan(make_plan(), path=self.plans_path)
        with open(self.plans_path, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("Lifecycle plans", text)
        self.assertIn("| Plan id | Kind | Repo id | Machines | Old | New | Disposition"
                       " | Created at | Created by | Note |", text)

    def test_empty_plan_id_is_refused(self):
        with self.assertRaises(ValueError):
            plans.append_plan(make_plan(plan_id=""), path=self.plans_path)

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            plans.append_plan(make_plan(kind="teleport"), path=self.plans_path)

    def test_note_with_newline_round_trips_as_one_line(self):
        # A literal "|" inside a cell is a different, pre-existing limitation shared
        # by every module using this escaping convention (mdtable.parse's cell split
        # is a plain str.split("|") with no escape-awareness) -- not something this
        # module introduces or is asked to fix. Newline-collapsing is the guarantee
        # `_escape` actually provides, matching tombstones.py's own tested scope.
        note = "first line\nsecond line"
        plan = make_plan(note=note)
        plans.append_plan(plan, path=self.plans_path)
        got = plans.load_plans(self.plans_path)[plan.plan_id]
        self.assertEqual(got.note, "first line second line")

    def test_all_machines_literal_round_trips(self):
        plan = make_plan(machines=("all",))
        plans.append_plan(plan, path=self.plans_path)
        got = plans.load_plans(self.plans_path)[plan.plan_id]
        self.assertEqual(got.machines, ("all",))


class TestLoadPlansEarliestWins(PlansTest):
    def test_earliest_created_at_wins_when_plan_id_duplicated(self):
        header_and_rows = (
            "| Plan id | Kind | Repo id | Machines | Old | New | Disposition"
            " | Created at | Created by | Note |\n"
            "|---------|------|---------|----------|-----|-----|--------------"
            "|------------|------------|------|\n"
            "| rename-widget-20260930 | rename | widget | all | Widget | Gadget2 |"
            "  | 2026-09-30T12:00:00-07:00 | mt-002 | second peer's revision |\n"
            "| rename-widget-20260930 | rename | widget | all | Widget | Gadget |"
            "  | 2026-09-30T09:00:00-07:00 | mt-001 | first peer's plan |\n"
        )
        with open(self.plans_path, "w", encoding="utf-8") as handle:
            handle.write(header_and_rows)

        plan = plans.load_plans(self.plans_path)["rename-widget-20260930"]
        self.assertEqual(plan.new, "Gadget")
        self.assertEqual(plan.created_by, "mt-001")


class TestAppendAndLoadApprovals(PlansTest):
    def test_missing_file_loads_as_empty(self):
        self.assertEqual(plans.load_approvals(self.approvals_path), {})

    def test_append_then_load_round_trips(self):
        approval = plans.Approval(
            plan_id="rename-widget-20260930",
            fingerprint="abc123abc123abcd",
            machine="mt-001",
            at="2026-09-30T10:05:00-07:00",
            by="mandrake",
        )
        ok = plans.append_approval(approval, path=self.approvals_path)
        self.assertTrue(ok)

        loaded = plans.load_approvals(self.approvals_path)
        self.assertIn(approval.plan_id, loaded)
        got = loaded[approval.plan_id]
        self.assertEqual(got, approval)

    def test_append_creates_file_with_header(self):
        approval = plans.Approval(
            plan_id="rename-widget-20260930",
            fingerprint="abc123abc123abcd",
            machine="mt-001",
            at="2026-09-30T10:05:00-07:00",
            by="mandrake",
        )
        plans.append_approval(approval, path=self.approvals_path)
        with open(self.approvals_path, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("Lifecycle plan approvals", text)
        self.assertIn("| Plan id | Fingerprint | Machine | At | By |", text)

    def test_empty_plan_id_is_refused(self):
        approval = plans.Approval(plan_id="", fingerprint="abc", machine="mt-001",
                                   at="2026-09-30T10:05:00-07:00", by="mandrake")
        with self.assertRaises(ValueError):
            plans.append_approval(approval, path=self.approvals_path)

    def test_empty_fingerprint_is_refused(self):
        approval = plans.Approval(plan_id="p1", fingerprint="", machine="mt-001",
                                   at="2026-09-30T10:05:00-07:00", by="mandrake")
        with self.assertRaises(ValueError):
            plans.append_approval(approval, path=self.approvals_path)

    def test_empty_machine_is_refused(self):
        approval = plans.Approval(plan_id="p1", fingerprint="abc", machine="",
                                   at="2026-09-30T10:05:00-07:00", by="mandrake")
        with self.assertRaises(ValueError):
            plans.append_approval(approval, path=self.approvals_path)


class TestLoadApprovalsLatestWins(PlansTest):
    """🔴 LATEST wins for approvals — the opposite of `load_plans` and `tombstones.load`.

    This class previously asserted earliest-wins, copied from those two stores, and that was
    pinning a real bug. Measured 2026-09-30 on the live store: approving a plan, correcting
    the plan, and re-approving left both rows on disk, and earliest-wins returned the **lapsed**
    approval — so `approved()` reported "lapsed" for ever against a fingerprint no longer in
    force, and `cmd_approve`'s own documented re-approval path was dead code.

    The three stores answer different questions, and their identical shape is the trap:
    a tombstone is terminal so the first wins; a plan id is authored once so the first is
    canonical; an approval is **re-takeable by design**, because a lapsing fingerprint is the
    whole mechanism, so the most recent human decision governs.
    """

    def test_latest_at_wins_when_plan_id_duplicated(self):
        header_and_rows = (
            "| Plan id | Fingerprint | Machine | At | By |\n"
            "|---------|-------------|---------|----|----|\n"
            "| p1 | fp-second | mt-002 | 2026-09-30T12:00:00-07:00 | someone-else |\n"
            "| p1 | fp-first | mt-001 | 2026-09-30T09:00:00-07:00 | mandrake |\n"
        )
        with open(self.approvals_path, "w", encoding="utf-8") as handle:
            handle.write(header_and_rows)

        approval = plans.load_approvals(self.approvals_path)["p1"]
        self.assertEqual(approval.fingerprint, "fp-second")
        self.assertEqual(approval.by, "someone-else")

    def test_re_approving_a_corrected_plan_takes_effect(self):
        """The end-to-end workflow that earliest-wins made impossible.

        Approve, revise the plan so the approval lapses, re-approve — the plan must then read
        as approved. This is the behaviour, not the resolution rule, so it survives a change of
        implementation.
        """
        plan = make_plan()
        plans.append_plan(plan, path=self.plans_path)
        plans.append_approval(
            plans.Approval(plan_id=plan.plan_id, fingerprint=plans.fingerprint(plan),
                           machine="mt-001", at="2026-09-30T09:00:00-07:00", by="mandrake"),
            path=self.approvals_path)

        revised = plan._replace(new="/home/user/dev/Corrected")
        ok, why = plans.approved(revised, plans.load_approvals(self.approvals_path))
        self.assertFalse(ok, "revising the destination must lapse the standing approval")
        self.assertIn("lapsed", why)

        plans.append_approval(
            plans.Approval(plan_id=plan.plan_id, fingerprint=plans.fingerprint(revised),
                           machine="mt-001", at="2026-09-30T10:00:00-07:00", by="mandrake"),
            path=self.approvals_path)

        ok, why = plans.approved(revised, plans.load_approvals(self.approvals_path))
        self.assertTrue(ok, "re-approving the corrected plan must take effect, not stay "
                            "shadowed by the lapsed approval: %s" % why)

    def test_the_superseded_approval_row_is_still_on_disk(self):
        """Latest-wins changes which row is AUTHORITATIVE, never whether history is kept.

        The store stays append-only: an audit must still be able to see that an earlier
        approval was taken and then superseded.
        """
        plan = make_plan()
        for stamp, fp in (("2026-09-30T09:00:00-07:00", "fp-old"),
                          ("2026-09-30T10:00:00-07:00", "fp-new")):
            plans.append_approval(
                plans.Approval(plan_id=plan.plan_id, fingerprint=fp, machine="mt-001",
                               at=stamp, by="mandrake"),
                path=self.approvals_path)
        with open(self.approvals_path, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("fp-old", text)
        self.assertIn("fp-new", text)


class TestEndToEndApprovalLapse(PlansTest):
    """An integration path through append_plan/append_approval/approved, exercising
    the whole point of the module: revising a plan's destination lapses a standing
    approval, while touching only its note does not.
    """

    def test_revision_lapses_but_note_edit_does_not(self):
        plan = make_plan()
        plans.append_plan(plan, path=self.plans_path)

        loaded_plan = plans.load_plans(self.plans_path)[plan.plan_id]
        approval = plans.Approval(
            plan_id=plan.plan_id,
            fingerprint=plans.fingerprint(loaded_plan),
            machine="mt-001",
            at="2026-09-30T10:05:00-07:00",
            by="mandrake",
        )
        plans.append_approval(approval, path=self.approvals_path)

        approvals = plans.load_approvals(self.approvals_path)
        ok, _ = plans.approved(loaded_plan, approvals)
        self.assertTrue(ok)

        # Touching only the note leaves the approval standing.
        noted = loaded_plan._replace(note="a clarifying note, nothing more")
        ok, _ = plans.approved(noted, approvals)
        self.assertTrue(ok)

        # Changing the destination lapses it.
        revised = loaded_plan._replace(new="SomethingElse")
        ok, reason = plans.approved(revised, approvals)
        self.assertFalse(ok)
        self.assertIn("lapsed", reason)


if __name__ == "__main__":
    unittest.main()
