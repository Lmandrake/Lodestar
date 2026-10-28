"""Tests for lib/completions.py — plan retirement, and the receipt that lets a peer read it.

Run:
    PYTHONPATH=bin python3 -m unittest tests.test_completions

Every test writes to a temp path. The real stores are
`~/dev/.lodestar/lifecycle-completions.json` (machine-local) and
`sync/lifecycle-receipts/<key>.json` (committed), and a test that touched either would either
retire a live plan or commit a fabricated receipt into the repo.

The properties, in order of how expensive getting them wrong is:

1. **A revised plan un-retires itself.** Retirement is gated on the plan's fingerprint, so
   changing a destination, machine list or disposition makes every recorded completion stop
   applying — the same mechanism that lapses the approval.
2. **`omitted` is withdrawn the moment a registry row appears.** Otherwise a plan that became
   actionable stays skipped for ever, silently.
3. **The receipt carries no clock read.** A timestamp would change its bytes on every sweep,
   commit a file on every pass for ever, and keep Lodestar from passing the sweep's own
   quiescence gate. A second-precision stamp is invisible to a write-twice-and-compare test
   inside one second, so the guard here is STRUCTURAL: no ISO date or time anywhere in the
   payload, and no time-shaped key.
4. **No machine asserts another's state.** One file per machine key, and a machine with no
   receipt reads as `pending (no receipt)` rather than being assumed to match its peers.
"""

import json
import os
import re
import shutil
import tempfile
import unittest

from lib import completions, plans


def a_plan(**over):
    base = dict(plan_id="rename-alpha-20260930", kind="rename", repo_id="alpha",
                machines=("all",), old="~/dev/Alpha", new="~/dev/Beta", disposition="",
                created_at="2026-09-30T16:00:00-07:00", created_by="test", note="")
    base.update(over)
    return plans.Plan(**base)


class CompletionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-completions-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "lifecycle-completions.json")
        self.receipts = os.path.join(self.tmp, "lifecycle-receipts")


class TestStore(CompletionsTest):
    def test_missing_file_loads_as_empty(self):
        self.assertEqual(completions.load(self.path), {})

    def test_record_then_load_round_trips(self):
        completions.record("p1", "alpha", "fp1", completions.CONVERGED, at="then",
                           path=self.path)
        store = completions.load(self.path)
        self.assertEqual(sorted(store), ["p1"])
        self.assertEqual(store["p1"].status, "converged")
        self.assertEqual(store["p1"].repo_id, "alpha")
        self.assertEqual(store["p1"].fingerprint, "fp1")

    def test_the_latest_write_wins_because_the_store_answers_what_happened_LAST(self):
        """A third duplicate rule, on purpose — CLAUDE.md §7 warns their shape is identical.

        A tombstone is terminal so the first retirement counts; a plan id is authored once so
        the first row is canonical; "what did this machine last do" can only be the newest.
        Earliest-wins here would pin a plan to the first outcome it ever reached, so a repo
        that was `refused` and later converged would report `refused` for ever.
        """
        completions.record("p1", "alpha", "fp1", "refused", at="first", path=self.path)
        completions.record("p1", "alpha", "fp1", completions.CONVERGED, at="second",
                           path=self.path)
        store = completions.load(self.path)
        self.assertEqual(len(store), 1)
        self.assertEqual(store["p1"].status, "converged")
        self.assertEqual(store["p1"].at, "second")

    def test_forget_drops_the_row_so_the_next_converge_re_evaluates(self):
        completions.record("p1", "alpha", "fp1", completions.CONVERGED, path=self.path)
        self.assertTrue(completions.forget("p1", self.path))
        self.assertEqual(completions.load(self.path), {})
        self.assertFalse(completions.forget("p1", self.path))

    def test_record_refuses_an_unnamed_plan_and_an_unnamed_status(self):
        with self.assertRaises(ValueError):
            completions.record("", "alpha", "fp1", completions.CONVERGED, path=self.path)
        with self.assertRaises(ValueError):
            completions.record("p1", "alpha", "fp1", "", path=self.path)

    def test_a_crash_mid_write_cannot_leave_a_partial_file(self):
        completions.record("p1", "alpha", "fp1", completions.CONVERGED, path=self.path)
        self.assertEqual([n for n in os.listdir(self.tmp) if ".tmp" in n], [])

    def test_a_corrupt_store_RAISES_rather_than_reading_as_empty(self):
        """Reading it as empty is safe but SILENT, which is the whole problem.

        An empty read simply restores the behaviour this module replaces — re-running every
        completed plan on every sweep — on one machine, with nothing anywhere saying so.
        """
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with self.assertRaises(completions.Unreadable):
            completions.load(self.path)

    def test_a_json_list_raises_rather_than_reading_as_empty(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(["p1"], handle)
        with self.assertRaises(completions.Unreadable):
            completions.load(self.path)


class TestRetires(CompletionsTest):
    """The one question `converge` asks before paying for the lock, git reads and satellites."""

    def test_no_record_at_all_does_not_retire(self):
        retired, why = completions.retires(None, "fp1", row_exists=True)
        self.assertFalse(retired)
        self.assertIn("no completion recorded", why)

    def test_a_converged_record_at_the_same_fingerprint_retires(self):
        entry = completions.record("p1", "alpha", "fp1", completions.CONVERGED,
                                   path=self.path)
        retired, why = completions.retires(entry, "fp1", row_exists=True)
        self.assertTrue(retired, why)

    def test_a_REVISED_PLAN_UN_RETIRES_ITSELF(self):
        """The invalidation nothing has to remember to perform.

        `plans.fingerprint` covers kind, repo id, machines, old, new and disposition — so a
        changed destination or machine list moves the fingerprint, the recorded completion
        stops matching, and the plan is re-evaluated. The same move lapses the approval, so the
        two mechanisms cannot drift apart.
        """
        entry = completions.record("p1", "alpha", "fp1", completions.CONVERGED,
                                   path=self.path)
        retired, why = completions.retires(entry, "fp2-revised", row_exists=True)
        self.assertFalse(retired)
        self.assertIn("fp2-revised", why)
        self.assertIn("fp1", why)

    def test_the_two_failures_are_named_apart_rather_than_collapsed_to_false(self):
        """"never completed here" and "completed, then the plan changed" are different facts.

        A human reading a bare "pending" goes looking for a bug in the executor. `plans.approved`
        makes the identical argument for its own pair.
        """
        _, never = completions.retires(None, "fp1", row_exists=True)
        entry = completions.record("p1", "alpha", "fp1", completions.CONVERGED,
                                   path=self.path)
        _, lapsed = completions.retires(entry, "fp2", row_exists=True)
        self.assertNotEqual(never, lapsed)
        self.assertIn("no completion", never)
        self.assertIn("changed", lapsed)

    def test_an_absent_current_fingerprint_does_not_manufacture_a_mismatch(self):
        """Matches `transitions.recovery_action`'s own rule, for the same reason."""
        entry = completions.record("p1", "alpha", "fp1", completions.CONVERGED,
                                   path=self.path)
        self.assertTrue(completions.retires(entry, "", row_exists=True)[0])

    def test_omitted_retires_while_no_registry_row_exists(self):
        entry = completions.record("p1", "ghost", "fp1", completions.OMITTED, path=self.path)
        self.assertTrue(completions.retires(entry, "fp1", row_exists=False)[0])

    def test_OMITTED_IS_WITHDRAWN_THE_MOMENT_A_REGISTRY_ROW_APPEARS(self):
        """The hazard that makes `omitted` safe to treat as terminal at all.

        `omitted` means "no registry row has this id, so nothing on disk was touched anywhere".
        That is genuinely dead — but a row COULD be added later, and a completion that outlived
        that would permanently skip a plan that had become actionable, with nothing reporting
        it. `converged` needs no such gate: it describes an act this machine performed.
        """
        entry = completions.record("p1", "ghost", "fp1", completions.OMITTED, path=self.path)
        retired, why = completions.retires(entry, "fp1", row_exists=True)
        self.assertFalse(retired)
        self.assertIn("a row exists now", why)

    def test_the_non_acting_statuses_deliberately_do_NOT_retire(self):
        """`not-named` and `out-of-scope` do no work, and still must be re-answered every run.

        They derive from `plan.machines` and the registry's own `Machines` column, cost nothing
        to recompute, and — unlike `omitted` — the registry column is not covered by the
        fingerprint, so retiring them would buy noise reduction at the price of a staleness
        hazard for zero saved work.
        """
        for status in ("not-named", "out-of-scope"):
            entry = completions.record("p1", "alpha", "fp1", status, path=self.path)
            retired, why = completions.retires(entry, "fp1", row_exists=True)
            self.assertFalse(retired, "%s must not retire a plan" % status)
            self.assertIn("not terminal", why)

    def test_a_failure_status_never_retires(self):
        for status in ("refused", "stop", "partial", "busy", "unsupported"):
            entry = completions.record("p1", "alpha", "fp1", status, path=self.path)
            self.assertFalse(completions.retires(entry, "fp1", row_exists=True)[0],
                             "%s must keep flagging every sweep" % status)

    def test_TERMINAL_is_exactly_the_two_statuses_this_file_reasons_about(self):
        """A pin, so adding a status to TERMINAL has to come with its own argument.

        Without it, a future session widening TERMINAL would silently retire plans on
        statuses whose staleness gates have never been considered.
        """
        self.assertEqual(tuple(completions.TERMINAL), ("converged", "omitted"))


class TestReceiptIsByteStable(CompletionsTest):
    """The committed artifact. Its determinism is a sweep requirement, not tidiness."""

    def a_store(self):
        completions.record("rename-alpha-20260930", "alpha", "fp1", completions.CONVERGED,
                           at="2026-10-01T14:00:00-07:00", path=self.path)
        completions.record("rehome-beta-20260930", "beta", "fp2", "not-named",
                           at="2026-10-01T14:00:01-07:00", path=self.path)
        return completions.load(self.path)

    def test_NO_DATE_OR_TIME_APPEARS_ANYWHERE_IN_THE_PAYLOAD(self):
        """The structural guard, and the only one that actually works here.

        A second-precision timestamp is invisible to a write-twice-and-compare test that runs
        inside one second — LESSONS.md records exactly that trap. So this asserts the property
        rather than a symptom: nothing ISO-date-shaped and nothing time-shaped in the serialized
        text, and no key whose name promises a clock read.

        Note a plan id legitimately ENDS in a date (`rename-alpha-20260930`), which is why the
        pattern tests for an ISO date with separators or a `hh:mm:ss`, not for a run of digits.
        """
        text = completions.receipt_text(self.a_store(), "mt-001")
        self.assertIsNone(re.search(r"\d{4}-\d{2}-\d{2}", text),
                          "an ISO date in the receipt would churn the file every sweep")
        self.assertIsNone(re.search(r"\d{2}:\d{2}", text),
                          "a clock time in the receipt would churn the file every sweep")

        banned = {"at", "date", "time", "now", "when", "stamp", "timestamp",
                  "created_at", "started_at", "finished_at", "swept_at", "generated_at",
                  "written_at", "converged_at"}

        def walk(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    self.assertNotIn(key.lower(), banned,
                                     "key %r promises a clock read" % key)
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(json.loads(text))

    def test_the_machine_local_at_field_exists_but_is_dropped_from_the_receipt(self):
        """Proves the previous test is not vacuous: the data it must exclude is really there."""
        store = self.a_store()
        self.assertTrue(store["rename-alpha-20260930"].at,
                        "the machine-local store does keep a timestamp")
        self.assertNotIn("2026-10-01", completions.receipt_text(store, "mt-001"))

    def test_identical_input_produces_identical_bytes(self):
        store = self.a_store()
        self.assertEqual(completions.receipt_text(store, "mt-001"),
                         completions.receipt_text(store, "mt-001"))

    def test_an_unchanged_receipt_is_not_rewritten(self):
        """The skip is what keeps the sweep quiet — an untouched mtime has nothing to commit."""
        store = self.a_store()
        target = os.path.join(self.receipts, "mt-001.json")
        self.assertTrue(completions.write_receipt("mt-001", store, target))
        self.assertFalse(completions.write_receipt("mt-001", store, target),
                         "a second write of the same content must report no change")

    def test_a_changed_status_does_rewrite(self):
        target = os.path.join(self.receipts, "mt-001.json")
        self.assertTrue(completions.write_receipt("mt-001", self.a_store(), target))
        completions.record("rehome-beta-20260930", "beta", "fp2", completions.CONVERGED,
                           path=self.path)
        self.assertTrue(completions.write_receipt(
            "mt-001", completions.load(self.path), target))

    def test_the_receipt_carries_a_schema_integer(self):
        payload = completions.receipt_payload(self.a_store(), "mt-001")
        self.assertIsInstance(payload["schema"], int)
        self.assertEqual(payload["schema"], completions.SCHEMA)

    def test_the_receipt_carries_evidence_and_NOT_a_complete_verdict(self):
        """A stored verdict silently outlives the facts it was computed from.

        `complete` depends on the plan's CURRENT fingerprint, which a revision moves — so it is
        re-derived by the reader. Writing it down would make a receipt keep asserting "done"
        about an act that has since changed.
        """
        payload = completions.receipt_payload(self.a_store(), "mt-001")
        entry = payload["plans"]["rename-alpha-20260930"]
        self.assertEqual(sorted(entry), ["fingerprint", "repo_id", "status"])

    def test_the_receipt_is_sweepable_rather_than_source(self):
        """It must ride the ordinary hourly sweep, which is the entire reason it is JSON.

        `gitutil.is_source` withholds code, `docs/` and the named root docs from the
        auto-commit — and withholds every `.sh`, which is why the statusline needed a `.json`
        receipt in the first place. Note `partition_paths` returns `(source, sweepable)`,
        source FIRST.
        """
        from lib import gitutil
        relative = "sync/lifecycle-receipts/mt-001.json"
        self.assertFalse(gitutil.is_source(relative))
        source, sweepable = gitutil.partition_paths([relative])
        self.assertEqual(source, [])
        self.assertEqual(sweepable, [relative])


class TestReceiptPathIsPerMachine(unittest.TestCase):
    def test_each_machine_key_gets_its_own_file(self):
        """One file per machine, so no machine can assert another's state and two writers merge.

        This is the structural reason a shared status column would be wrong: a plan's
        `Machines` column can name several machines, so one machine writing "done" into shared
        state would suppress the plan where it has never run.
        """
        from lib import paths
        self.assertNotEqual(paths.lifecycle_receipt_file("mt-001"),
                            paths.lifecycle_receipt_file("tower"))
        self.assertTrue(
            paths.lifecycle_receipt_file("mt-001").endswith("mt-001.json"))

    def test_a_machine_key_cannot_escape_the_receipts_directory(self):
        """`machines.md` is hand-edited, so a key carrying a separator must not be a path."""
        from lib import paths
        target = paths.lifecycle_receipt_file("../../etc/evil")
        self.assertEqual(os.path.dirname(target), paths.lifecycle_receipts_dir())
        with self.assertRaises(ValueError):
            paths.lifecycle_receipt_file("///")

    def test_the_machine_local_store_is_outside_the_repo(self):
        """Nothing under the repo is safe to hold private per-machine state in.

        `gitutil.SOURCE_DIRS` is `("docs",)` and the source suffixes are code extensions, so a
        JSON file anywhere in the tree is auto-committed on the next hourly sweep — which is
        exactly what makes the receipt travel, and exactly why the authoritative per-machine
        store must not live there.
        """
        from lib import paths
        self.assertFalse(
            os.path.abspath(paths.lifecycle_completions_file()).startswith(
                os.path.abspath(paths.root()) + os.sep))


class TestLoadReceipts(CompletionsTest):
    def write(self, key, plans_payload, machine=None):
        os.makedirs(self.receipts, exist_ok=True)
        payload = {"schema": 1, "machine": machine or key, "plans": plans_payload}
        with open(os.path.join(self.receipts, "%s.json" % key), "w", encoding="utf-8") as fh:
            json.dump(payload, fh)

    def test_a_missing_directory_is_empty_not_an_error(self):
        self.assertEqual(completions.load_receipts(self.receipts), {})

    def test_each_file_is_keyed_by_its_own_machine_field(self):
        self.write("mt-001", {"p1": {"status": "converged", "fingerprint": "fp1"}})
        self.write("tower", {"p1": {"status": "not-named", "fingerprint": "fp1"}})
        found = completions.load_receipts(self.receipts)
        self.assertEqual(sorted(found), ["tower", "mt-001"])

    def test_ONE_CORRUPT_RECEIPT_DOES_NOT_BLANK_THE_WHOLE_VIEW(self):
        """A peer's broken file must cost only that peer's row.

        Skipping it makes that machine read as having no receipt, which is the honest answer;
        raising would make one bad file hide every other machine's real state.
        """
        self.write("mt-001", {"p1": {"status": "converged", "fingerprint": "fp1"}})
        os.makedirs(self.receipts, exist_ok=True)
        with open(os.path.join(self.receipts, "tower.json"), "w", encoding="utf-8") as fh:
            fh.write("{broken")
        found = completions.load_receipts(self.receipts)
        self.assertEqual(sorted(found), ["mt-001"])

    def test_non_json_files_are_ignored(self):
        os.makedirs(self.receipts, exist_ok=True)
        with open(os.path.join(self.receipts, "README.md"), "w", encoding="utf-8") as fh:
            fh.write("notes")
        self.assertEqual(completions.load_receipts(self.receipts), {})


class TestSummaryLines(unittest.TestCase):
    """The one derived, human-readable line per plan. Rendered from receipts, never inferred."""

    FLEET = ("tower", "mt-002", "mt-001")

    def receipts(self, **by_machine):
        return {key: {"schema": 1, "machine": key, "plans": value}
                for key, value in by_machine.items()}

    def test_the_required_shape_complete_on_some_pending_on_others(self):
        lines = completions.summary_lines(
            self.receipts(
                **{"mt-001": {"p1": {"status": "converged", "fingerprint": "fp1"}},
                   "mt-002": {"p1": {"status": "converged", "fingerprint": "fp1"}}}),
            {"p1": "fp1"}, self.FLEET)
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertIn("complete on", line)
        self.assertIn("`mt-002`", line)
        self.assertIn("`mt-001`", line)
        self.assertIn("pending on", line)
        self.assertIn("`tower`", line)
        self.assertLess(line.index("complete on"), line.index("pending on"))

    def test_A_MACHINE_WITH_NO_RECEIPT_IS_PENDING_AND_SAYS_SO(self):
        """Never assumed to match its peers — the absence is reported as the absence it is."""
        lines = completions.summary_lines(
            self.receipts(**{"mt-001": {"p1": {"status": "converged",
                                                  "fingerprint": "fp1"}}}),
            {"p1": "fp1"}, self.FLEET)
        self.assertIn("no receipt", lines[0])

    def test_a_deliberately_excluded_machine_is_NOT_called_pending(self):
        """`not-named` is a decision, not an outstanding task.

        Rendering it as "pending" would be a line that passes every mechanical check and still
        tells a reader the opposite of the truth — the failure mode CLAUDE.md §7 keeps finding
        on generated surfaces.
        """
        lines = completions.summary_lines(
            self.receipts(
                **{"mt-001": {"p1": {"status": "converged", "fingerprint": "fp1"}},
                   "mt-002": {"p1": {"status": "converged", "fingerprint": "fp1"}},
                   "tower": {"p1": {"status": "not-named", "fingerprint": "fp1"}}}),
            {"p1": "fp1"}, self.FLEET)
        self.assertIn("not applicable on", lines[0])
        self.assertNotIn("pending on", lines[0])

    def test_A_RECEIPT_AGAINST_A_SUPERSEDED_FINGERPRINT_READS_AS_PENDING(self):
        """A revised plan must not count a machine's old completion as current.

        This is the reader-side half of the same rule `retires` enforces locally, and it is why
        the receipt stores the fingerprint rather than a `complete` boolean.
        """
        lines = completions.summary_lines(
            self.receipts(**{"mt-001": {"p1": {"status": "converged",
                                                  "fingerprint": "OLD"}}}),
            {"p1": "NEW"}, self.FLEET)
        self.assertIn("pending on", lines[0])
        self.assertNotIn("complete on", lines[0])
        self.assertIn("OLD", lines[0], "the superseded fingerprint is named, not just implied")

    def test_a_failure_on_one_machine_is_pending_with_its_status_shown(self):
        lines = completions.summary_lines(
            self.receipts(**{"mt-001": {"p1": {"status": "partial",
                                                  "fingerprint": "fp1"}}}),
            {"p1": "fp1"}, self.FLEET)
        self.assertIn("partial", lines[0])

    def test_a_receipt_naming_an_UNKNOWN_plan_is_still_rendered(self):
        """A mechanism whose surface hides a row is this repo's signature defect.

        A peer can legitimately be ahead of this checkout, so the plan set is the union of what
        this machine can see and what every receipt mentions.
        """
        lines = completions.summary_lines(
            self.receipts(**{"tower": {"p-future": {"status": "converged",
                                                       "fingerprint": "fpX"}}}),
            {}, self.FLEET)
        self.assertEqual(len(lines), 1)
        self.assertIn("p-future", lines[0])
        self.assertIn("no such plan", lines[0])

    def test_the_output_is_stable_across_calls(self):
        receipts = self.receipts(
            **{"mt-001": {"p1": {"status": "converged", "fingerprint": "fp1"}}})
        first = completions.summary_lines(receipts, {"p1": "fp1"}, self.FLEET)
        second = completions.summary_lines(receipts, {"p1": "fp1"}, self.FLEET)
        self.assertEqual(first, second)

    def test_a_machine_with_a_receipt_but_absent_from_machines_md_is_included(self):
        """An undeclared machine's receipt is evidence; dropping it would hide a real run."""
        lines = completions.summary_lines(
            self.receipts(**{"newbox": {"p1": {"status": "converged",
                                               "fingerprint": "fp1"}}}),
            {"p1": "fp1"}, ("mt-001",))
        self.assertIn("`newbox`", lines[0])


if __name__ == "__main__":
    unittest.main()
