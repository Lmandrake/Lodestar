"""Tests for the WIRING of plan retirement — the production callers, not the library.

Run:
    PYTHONPATH=bin python3 -m unittest tests.test_lifecycle_retirement

`tests/test_completions.py` proves the library behaves. This file exists because of the
repeated defect CLAUDE.md §7 names outright: **a subsystem with green tests and no production
caller looks exactly like a working feature.** `lib/enroll.py` shipped with 13 passing tests
and zero callers; SWEEP's group engine shipped with ~40 and a dispatch path that never reached
it; the `Former` column was parsed by Phase 1 and read by nothing for a month. So what is
asserted here is *reachability, placement and refusal*:

1. `lifecycle._converge_one` reads the completion store and short-circuits BEFORE the lock.
2. The check sits ABOVE the `omitted` branch, or it can never retire the plans it was built for.
3. `cmd_converge` records a completion on a real run and **nothing at all** on a dry run.
4. `cmd_converge` writes this machine's receipt, and `sync.report` renders it into SYNC.md.
5. The retired status is a SUCCESS — a finished plan must stop driving the non-zero exit.

Every path is redirected to a temp directory. The real stores are
`~/dev/.lodestar/lifecycle-completions.json` and `sync/lifecycle-receipts/<key>.json`, and a
test that let a caller reach either would retire a live plan or commit a fabricated receipt.
"""

import json
import os
import re
import shutil
import tempfile
import unittest

import lifecycle
import sync
from lib import completions, machine, paths, plans, registry, transitions
from tests import needs_private  # noqa: E402


def a_plan(**over):
    base = dict(plan_id="rename-alpha-20260930", kind="rename", repo_id="alpha",
                machines=("all",), old="~/dev/Alpha", new="~/dev/Beta", disposition="",
                created_at="2026-09-30T16:00:00-07:00", created_by="test", note="")
    base.update(over)
    return plans.Plan(**base)


def an_entry(**over):
    base = dict(name="Alpha", path="~/dev/Alpha", branch="main", sync=True, notes="",
                send=True, receive=True, machines=(), repo_id="alpha", former=(),
                remote="git@github.com:Lmandrake/Alpha.git", state="active")
    base.update(over)
    return registry.Entry(**base)


class RedirectedTest(unittest.TestCase):
    """Redirect every store a retirement touches, and restore it afterwards."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-retirement-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.completions_file = os.path.join(self.tmp, "completions.json")
        self.transitions_file = os.path.join(self.tmp, "transitions.json")
        self.receipts_dir = os.path.join(self.tmp, "receipts")
        self.journaled = os.path.join(self.tmp, "journaled.json")

        self.patch(paths, "lifecycle_completions_file", lambda: self.completions_file)
        self.patch(paths, "lifecycle_transitions_file", lambda: self.transitions_file)
        self.patch(paths, "lifecycle_receipts_dir", lambda: self.receipts_dir)
        self.patch(paths, "lifecycle_receipt_file",
                   lambda key: os.path.join(self.receipts_dir, "%s.json" % key))
        self.patch(paths, "lifecycle_journal_state_file", lambda: self.journaled)

    def patch(self, module, name, value):
        original = getattr(module, name)
        setattr(module, name, value)
        self.addCleanup(setattr, module, name, original)

    def a_completion(self, status, plan=None, fingerprint=None):
        plan = plan or a_plan()
        completions.record(plan.plan_id, plan.repo_id,
                           fingerprint or plans.fingerprint(plan), status,
                           path=self.completions_file)


@needs_private("machines.md")
class TestConvergeReachesTheCompletionStore(RedirectedTest):
    """Reachability and placement — the two things a library test cannot establish."""

    def setUp(self):
        super().setUp()
        self.patch(lifecycle, "_row_for", lambda repo_id: an_entry())

        def exploding_lock(*args, **kwargs):
            raise AssertionError(
                "the machine-local lock was taken for a plan that is already complete — the "
                "short-circuit is below the expensive work instead of above it")

        self.patch(lifecycle.lifelock, "held", exploding_lock)

    def test_a_converged_record_retires_the_plan_without_taking_the_lock(self):
        """The whole point: the lock, git remote reads and satellite scan are not re-paid.

        The exploding lock is what makes this a placement assertion rather than a status
        assertion — a short-circuit written below `lifelock.held` would return the same status
        and still re-pay for everything this exists to avoid.
        """
        self.a_completion(completions.CONVERGED)
        status, lines = lifecycle._converge_one(a_plan(), dry_run=True)
        self.assertEqual(status, lifecycle.COMPLETE)
        self.assertIn("already converged", "\n".join(lines))

    def test_the_report_names_the_command_that_un_retires_it(self):
        """A skip a human cannot reverse is worse than the re-run it replaced."""
        self.a_completion(completions.CONVERGED)
        _, lines = lifecycle._converge_one(a_plan(), dry_run=True)
        self.assertIn("reopen rename-alpha-20260930", "\n".join(lines))

    def test_a_REVISED_plan_is_re_evaluated_rather_than_skipped(self):
        """Proves the retirement is fingerprint-gated through the real caller.

        The lock explodes here deliberately: reaching it is the evidence that the plan was NOT
        retired. Changing the destination moves the fingerprint, which is the same move that
        lapses the approval.
        """
        self.a_completion(completions.CONVERGED, fingerprint="a-superseded-fingerprint")
        with self.assertRaises(AssertionError):
            lifecycle._converge_one(a_plan(), dry_run=True)

    def test_an_unreadable_completion_store_STOPS_rather_than_acting(self):
        """Reading it as empty would re-run completed work; acting past a corrupt store is worse.

        A `stop` here is a refusal before anything is touched, which is the same discipline
        `cmd_converge` already applies to an unreadable transition store.
        """
        with open(self.completions_file, "w", encoding="utf-8") as handle:
            handle.write("{broken")
        status, lines = lifecycle._converge_one(a_plan(), dry_run=True)
        self.assertEqual(status, "stop")
        self.assertIn("REFUSED", "\n".join(lines))


@needs_private("machines.md")
class TestThePlacementAboveOmitted(RedirectedTest):
    """🔴 The check must sit ABOVE the `omitted` / `not-named` / `out-of-scope` branches.

    Those are statuses a completion can HOLD. A short-circuit placed after them would be
    unreachable for exactly the plans this feature was built to retire — the two throwaway
    plans in `sync/plans.md` whose registry row has been deleted and which reported `omitted`
    on every run on every machine with no way to ever stop.
    """

    def test_an_omitted_record_retires_a_plan_whose_row_is_gone(self):
        self.patch(lifecycle, "_row_for", lambda repo_id: None)
        self.a_completion(completions.OMITTED)
        status, _ = lifecycle._converge_one(a_plan(), dry_run=True)
        self.assertEqual(status, lifecycle.COMPLETE)

    def test_WITHOUT_a_record_the_same_plan_still_reports_omitted(self):
        """Proves the previous test is the record working, not the row lookup."""
        self.patch(lifecycle, "_row_for", lambda repo_id: None)
        status, _ = lifecycle._converge_one(a_plan(), dry_run=True)
        self.assertEqual(status, "omitted")

    def test_an_omitted_record_is_WITHDRAWN_once_a_registry_row_appears(self):
        """The staleness hazard that makes `omitted` safe to retire at all, through the caller.

        A row arriving later makes the plan actionable; a completion that outlived that would
        permanently skip it with nothing reporting the fact.
        """
        self.patch(lifecycle, "_row_for", lambda repo_id: an_entry())
        self.a_completion(completions.OMITTED)
        status, _ = lifecycle._converge_one(a_plan(), dry_run=True)
        self.assertNotEqual(status, lifecycle.COMPLETE)


@needs_private("machines.md")
class TestTheConflictGuardReplacesTheAccidentalOne(RedirectedTest):
    """The single `repo_id` slot was mutual exclusion by accident. Keying by plan removes it.

    So the protection is explicit now, and it is asserted through the caller because the
    refusal has to be visible in a `--dry-run` as well as at write time — a preview that is the
    more optimistic of the two runs is backwards, which is why `_do_rename` already reports
    `would-be-partial`.
    """

    def setUp(self):
        super().setUp()
        self.patch(lifecycle, "_row_for", lambda repo_id: an_entry())

    def test_an_unfinished_transition_for_a_RIVAL_plan_stops_the_converge(self):
        transitions.record(transitions.Transition(
            plan_id="quarantine-alpha-20260930", repo_id="alpha", kind="quarantine",
            old_path="/x/Alpha", new_path="", holding_path="/x/held/Alpha",
            fingerprint="other", started_at="2026-10-01T10:00:00-07:00"),
            self.transitions_file)
        status, lines = lifecycle._converge_one(a_plan(), dry_run=True)
        self.assertEqual(status, "stop")
        joined = "\n".join(lines)
        self.assertIn("quarantine-alpha-20260930", joined)
        self.assertIn("/x/held/Alpha", joined,
                      "the refusal must name the holding path, or the material is unfindable")

    def test_a_FINISHED_transition_for_a_rival_plan_no_longer_wedges_the_repo(self):
        """🔴 The measured failure this whole re-key exists to end.

        2026-10-01: a quarantine converged and finished its record, a rehome in the same pass
        overwrote it, failed, and left an unfinished row — after which every later plan for the
        repo reported `stop` for ever and a person had to clear it. Under the old key the
        finished record WAS what the next plan found, and two different plans can never share a
        fingerprint, so the mismatch rule stopped it.

        Reaching the dry-run rename path here (satellites reported, no refusal) is the proof
        that a completed transition is now history rather than an obstruction.
        """
        transitions.record(transitions.Transition(
            plan_id="quarantine-alpha-20260930", repo_id="alpha", kind="quarantine",
            old_path="/x/Alpha", new_path="", holding_path="/x/held/Alpha",
            fingerprint="other", started_at="2026-10-01T10:00:00-07:00"),
            self.transitions_file)
        transitions.finish("quarantine-alpha-20260930", path=self.transitions_file)
        status, lines = lifecycle._converge_one(a_plan(), dry_run=True)
        self.assertNotEqual(status, "stop", "\n".join(lines))


class TestCmdConvergeRecordsAndReceipts(RedirectedTest):
    """The recording wrapper. `_converge_one` is stubbed so only the wrapper is under test."""

    def setUp(self):
        super().setUp()
        self.plans_file = os.path.join(self.tmp, "plans.md")
        self.approvals_file = os.path.join(self.tmp, "approvals.md")
        self.patch(paths, "plans_file", lambda: self.plans_file)
        self.patch(paths, "approvals_file", lambda: self.approvals_file)

        self.plan = a_plan()
        plans.append_plan(self.plan, self.plans_file)
        plans.append_approval(plans.Approval(
            plan_id=self.plan.plan_id, fingerprint=plans.fingerprint(self.plan),
            machine=machine.key(), at="2026-09-30T17:00:00-07:00", by="test"),
            self.approvals_file)

        class Verdict(object):
            ok = True
            reason = "stubbed fresh"

        self.patch(lifecycle, "board_freshness", lambda now=None: Verdict())
        self.patch(lifecycle.validate, "faults", lambda *a, **k: [])
        self.patch(lifecycle.journal, "append", lambda *a, **k: None)
        self.status = "converged"
        # Captured BEFORE the patch: the guard below needs the real signature, and reading it
        # back off `lifecycle` after patching compares the stub against itself.
        self.real_converge_one = lifecycle._converge_one
        # Signature mirrors the real `_converge_one`, INCLUDING `armed` and `runner` which
        # `purge` added. A stub that is merely callable is not a stub that matches; see
        # `test_the_stub_signature_matches_the_real_converge_one` below.
        self.patch(lifecycle, "_converge_one",
                   lambda plan, dry_run, armed=False, runner=None: (self.status, ["stubbed"]))

    class Args(object):
        """A hand-rolled stand-in for `main()`'s argparse namespace.

        🔴 **It rots silently whenever `converge` gains a flag, and it did**: `--arm-purge`
        landed on the same day in a different worktree, and merging the two produced seven
        `AttributeError: 'Args' object has no attribute 'arm_purge'` failures that read as a
        broken merge rather than a stale fake. The parser lives inside `main()` and cannot be
        borrowed, so the fake is unavoidable — but `test_the_fake_covers_every_flag_the_command_reads`
        below makes the next one fail once, by name, instead of seven times by traceback.

        The fix is NOT `getattr(args, "arm_purge", False)` in the command: CLAUDE.md §7 records
        what a defensive default over an attribute we control costs — a wrong attribute name
        becomes a permanent silent answer instead of a crash. For a destructive flag that answer
        would be "not armed", which is safe today and hides a real wiring gap tomorrow.
        """

        def __init__(self, dry_run):
            self.dry_run = dry_run
            self.ignore_freshness = False
            self.arm_purge = False

    def test_the_stub_signature_matches_the_real_converge_one(self):
        """The `_converge_one` stub must accept every parameter the real one does.

        The same rot as the Args fake, one layer down: `purge` gave `_converge_one` an `armed`
        and a `runner`, and the stub lambda rejected them with a `TypeError` that named the
        lambda rather than the cause. Compared against the live signature so it cannot go
        stale, and deliberately one-directional -- a stub accepting MORE than the real
        function is harmless, accepting fewer is the bug.
        """
        import inspect
        real = set(inspect.signature(self.real_converge_one).parameters)
        stub = set(inspect.signature(lifecycle._converge_one).parameters)
        self.assertIsNot(self.real_converge_one, lifecycle._converge_one,
                         "the stub is not installed, so this test would compare the real "
                         "function against itself and pass vacuously")
        self.assertTrue(real, "found no parameters at all -- the scan itself is broken")
        self.assertEqual(sorted(real - stub), [],
                         "_converge_one takes %s, which the stub does not accept"
                         % sorted(real - stub))

    def test_the_fake_covers_every_flag_the_command_reads(self):
        """Every `args.X` in `cmd_converge` must exist on the fake, or the next flag added to
        `converge` breaks these tests by traceback instead of by name.

        Derived from the source rather than listed, so it cannot go stale the way the fake it
        guards did. Comments and docstrings are stripped first: this repo has measured a test
        passing on a comment, and `cmd_converge`'s docstring discusses flags it does not read.
        """
        import inspect
        body = inspect.getsource(lifecycle.cmd_converge)
        body = re.sub(r'"""(?:.|\n)*?"""', "", body)
        body = re.sub(r"#.*", "", body)
        wanted = set(re.findall(r"\bargs\.([A-Za-z_][A-Za-z_0-9]*)", body))
        self.assertTrue(wanted, "found no args.X at all -- the scan itself is broken")
        missing = sorted(n for n in wanted if not hasattr(self.Args(False), n))
        self.assertEqual(missing, [],
                         "cmd_converge reads %s, which the Args fake does not provide"
                         % (missing,))

    def run_converge(self, dry_run):
        import io
        import sys
        captured, real = io.StringIO(), sys.stdout
        sys.stdout = captured
        try:
            code = lifecycle.cmd_converge(self.Args(dry_run))
        finally:
            sys.stdout = real
        return code, captured.getvalue()

    def test_a_real_run_records_the_completion(self):
        self.run_converge(dry_run=False)
        store = completions.load(self.completions_file)
        self.assertIn(self.plan.plan_id, store)
        self.assertEqual(store[self.plan.plan_id].status, "converged")
        self.assertEqual(store[self.plan.plan_id].fingerprint,
                         plans.fingerprint(self.plan),
                         "the fingerprint is what makes a revised plan un-retire itself")

    def test_A_DRY_RUN_RECORDS_NOTHING_AND_WRITES_NO_RECEIPT(self):
        """🔴 A design decision, not an omission — this repo settled it once already.

        `bubbles.state_of` maps an `ok` receipt to DONE, so a receipt for an act that never
        happened paints the card done while the world is untouched; there is deliberately no
        outcome value meaning "pretended". Here the cost is worse: a completion written from a
        preview would retire a plan that has never run, and the next real sweep would skip it
        in silence.
        """
        self.run_converge(dry_run=True)
        self.assertFalse(os.path.exists(self.completions_file),
                         "a preview must not write a completion")
        self.assertFalse(os.path.exists(self.receipts_dir),
                         "a preview must not write a receipt")

    def test_a_real_run_writes_this_machines_receipt(self):
        self.run_converge(dry_run=False)
        target = os.path.join(self.receipts_dir, "%s.json" % machine.key())
        self.assertTrue(os.path.isfile(target), "the travelling half must actually be written")
        with open(target, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["machine"], machine.key())
        self.assertEqual(payload["plans"][self.plan.plan_id]["status"], "converged")

    def test_it_writes_ONLY_this_machines_receipt(self):
        """No machine may assert another's state. One file per key is the whole mechanism."""
        self.run_converge(dry_run=False)
        self.assertEqual(sorted(os.listdir(self.receipts_dir)),
                         ["%s.json" % machine.key()])

    def test_a_failure_status_is_recorded_too_so_a_peer_can_see_it(self):
        """A peer reading "pending" must be able to tell "has not run" from "failed here"."""
        self.status = "partial"
        code, _ = self.run_converge(dry_run=False)
        self.assertEqual(code, 1, "partial must still drive the non-zero exit")
        store = completions.load(self.completions_file)
        self.assertEqual(store[self.plan.plan_id].status, "partial")
        self.assertFalse(completions.retires(store[self.plan.plan_id],
                                             plans.fingerprint(self.plan), True)[0])

    def test_A_RETIRED_PLAN_IS_A_SUCCESS_AND_DOES_NOT_FLAG_THE_SWEEP(self):
        """The whole user-visible point. `sync.lifecycle_step` maps a non-zero exit to FLAGGED.

        If `complete` counted as a failure, retiring a plan would swap one permanent noise
        source for another.
        """
        self.status = lifecycle.COMPLETE
        code, _ = self.run_converge(dry_run=False)
        self.assertEqual(code, 0)

    def test_a_retired_plan_is_not_re_recorded_on_every_sweep(self):
        """Rewriting the record would move its `at` stamp hourly for no new fact.

        That costs nothing in the repo — the store is machine-local — but it is the same
        class of churn the receipt's missing timestamp exists to prevent, and a stamp that
        moves on every sweep stops answering "when did this actually finish".
        """
        self.a_completion(completions.CONVERGED, plan=self.plan)
        before = completions.load(self.completions_file)[self.plan.plan_id].at
        self.status = lifecycle.COMPLETE
        self.run_converge(dry_run=False)
        after = completions.load(self.completions_file)[self.plan.plan_id].at
        self.assertEqual(before, after)


class TestReopenIsAProductionCaller(RedirectedTest):
    """`completions.forget` with no caller would be the fifth inert feature in this loop."""

    def setUp(self):
        super().setUp()
        self.patch(paths, "plans_file", lambda: os.path.join(self.tmp, "plans.md"))
        plans.append_plan(a_plan(), paths.plans_file())

    class Args(object):
        def __init__(self, plan_id):
            self.plan_id = plan_id

    def test_reopen_clears_the_record_so_the_next_converge_re_evaluates(self):
        self.a_completion(completions.CONVERGED)
        self.assertEqual(lifecycle.cmd_reopen(self.Args("rename-alpha-20260930")), 0)
        self.assertEqual(completions.load(self.completions_file), {})

    def test_reopening_a_plan_with_no_record_is_non_zero_rather_than_a_false_success(self):
        self.assertEqual(lifecycle.cmd_reopen(self.Args("rename-alpha-20260930")), 1)

    def test_it_is_reachable_from_the_command_line(self):
        parser_names = []

        def fake(args):
            parser_names.append(args.plan_id)
            return 0

        self.patch(lifecycle, "cmd_reopen", fake)
        self.assertEqual(lifecycle.main(["reopen", "some-plan"]), 0)
        self.assertEqual(parser_names, ["some-plan"])


class TestSyncRendersTheFleetView(RedirectedTest):
    """SYNC.md is the only surface anyone reads.

    CLAUDE.md §7: *"a check nobody can see is the same as no check"* — the desired-state
    faults section printed `None found.` while two checks were firing, because the section
    rendered only a subset. A completion mechanism whose cross-machine view never reaches
    SYNC.md would be the same defect by a new route.
    """

    def setUp(self):
        super().setUp()
        self.patch(paths, "plans_file", lambda: os.path.join(self.tmp, "plans.md"))
        plans.append_plan(a_plan(), paths.plans_file())
        os.makedirs(self.receipts_dir, exist_ok=True)
        with open(os.path.join(self.receipts_dir, "mt-001.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"schema": 1, "machine": "mt-001",
                       "plans": {"rename-alpha-20260930": {
                           "status": "converged", "repo_id": "alpha",
                           "fingerprint": plans.fingerprint(a_plan())}}}, handle)

    def rendered(self):
        return sync.report([], "desktop", dry_run=True, excluded_entries=[])

    def test_the_section_is_rendered_with_the_required_shape(self):
        text = self.rendered()
        self.assertIn("## Lifecycle plan completion", text)
        self.assertIn("complete on", text)
        self.assertIn("`mt-001`", text)

    @needs_private("machines.md")
    def test_machines_with_no_receipt_are_reported_as_pending(self):
        text = self.rendered()
        self.assertIn("pending on", text)
        self.assertIn("no receipt", text)

    def test_the_section_is_derived_from_the_receipts_and_not_from_this_machines_store(self):
        """A machine-local store cannot be read by a peer, so rendering from it would be a lie.

        Proved by giving the local store a DIFFERENT answer than the receipt on disk and
        checking the receipt is what reaches the page.
        """
        self.a_completion("partial")
        text = self.rendered()
        self.assertIn("complete on", text)
        self.assertNotIn("partial", text)

    def test_a_broken_receipts_directory_never_costs_the_sweep(self):
        """A derived report is not allowed to fail a sweep — the house rule for the repo map."""
        def boom(*args, **kwargs):
            raise RuntimeError("deliberate")

        self.patch(completions, "section_lines", boom)
        self.assertIn("could not read the lifecycle receipts", self.rendered())


if __name__ == "__main__":
    unittest.main()
