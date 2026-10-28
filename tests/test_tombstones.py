"""Tests for lib/tombstones.py — the append-only store that makes retirement terminal.

A tombstone must survive the thing it describes (the registry row) being deleted, so
these tests write directly to a temp file via the `path` argument rather than routing
through LODESTAR_ROOT — the module's whole point is that it does not depend on the
registry being present or correct.
"""

import os
import shutil
import tempfile
import unittest

from lib import tombstones


class TombstoneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-tombstones-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "tombstones.md")


class TestLoadAndAppend(TombstoneTest):
    def test_missing_file_loads_as_empty(self):
        self.assertEqual(tombstones.load(self.path), {})
        self.assertFalse(tombstones.is_retired("Alpha", self.path))

    def test_append_then_load_round_trips(self):
        ok = tombstones.append("Alpha", reason="merged into Beta",
                                superseded_by="Beta", at="2026-09-30T10:00-07:00",
                                path=self.path)
        self.assertTrue(ok)

        store = tombstones.load(self.path)
        self.assertIn("Alpha", store)
        stone = store["Alpha"]
        self.assertEqual(stone.repo_id, "Alpha")
        self.assertEqual(stone.at, "2026-09-30T10:00-07:00")
        self.assertEqual(stone.reason, "merged into Beta")
        self.assertEqual(stone.superseded_by, "Beta")
        self.assertTrue(tombstones.is_retired("Alpha", self.path))

    def test_append_creates_the_file_with_its_header(self):
        tombstones.append("Alpha", path=self.path)
        with open(self.path, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("Tombstones", text)
        self.assertIn("| Id | At | Reason | Superseded by |", text)

    def test_reason_with_comma_and_emoji_round_trips(self):
        reason = "retired, folded into the monorepo \U0001F480"
        tombstones.append("Gamma", reason=reason, path=self.path)
        stone = tombstones.load(self.path)["Gamma"]
        self.assertEqual(stone.reason, reason)


class TestIdempotence(TombstoneTest):
    def test_reretiring_is_a_no_op(self):
        first = tombstones.append("Alpha", reason="first reason", path=self.path)
        second = tombstones.append("Alpha", reason="second reason", path=self.path)
        self.assertTrue(first)
        self.assertFalse(second)

        with open(self.path, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertEqual(text.count("| Alpha |"), 1)
        # The original reason survives -- the second call wrote nothing at all.
        self.assertEqual(tombstones.load(self.path)["Alpha"].reason, "first reason")



class TestEarliestWins(TombstoneTest):
    def test_earliest_at_wins_when_the_same_id_appears_twice(self):
        # Simulates two peers retiring the same id independently, then merging --
        # append() itself refuses a duplicate, so this writes the merge conflict's
        # resolved shape directly.
        header_and_rows = (
            "| Id | At | Reason | Superseded by |\n"
            "|----|----|--------|----------------|\n"
            "| Alpha | 2026-09-30T12:00-07:00 | second peer's retirement |  |\n"
            "| Alpha | 2026-09-28T09:00-07:00 | first peer's retirement |  |\n"
        )
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(header_and_rows)

        stone = tombstones.load(self.path)["Alpha"]
        self.assertEqual(stone.reason, "first peer's retirement")
        self.assertEqual(stone.at, "2026-09-28T09:00-07:00")


class TestValidation(TombstoneTest):
    def test_empty_repo_id_is_refused(self):
        with self.assertRaises(ValueError):
            tombstones.append("", path=self.path)

    def test_self_supersession_is_refused(self):
        with self.assertRaises(ValueError):
            tombstones.append("Alpha", superseded_by="Alpha", path=self.path)


class TestSupersedeCycles(unittest.TestCase):
    def _stone(self, repo_id, superseded_by=""):
        return tombstones.Tombstone(repo_id=repo_id, at="2026-09-30T10:00-07:00",
                                     reason="", superseded_by=superseded_by)

    def test_no_cycle_in_a_clean_chain(self):
        store = {
            "Alpha": self._stone("Alpha", "Beta"),
            "Beta": self._stone("Beta", "Gamma"),
            "Gamma": self._stone("Gamma"),
        }
        self.assertEqual(tombstones.supersede_cycles(store), [])

    def test_two_node_cycle_is_detected(self):
        store = {
            "Alpha": self._stone("Alpha", "Beta"),
            "Beta": self._stone("Beta", "Alpha"),
        }
        cycles = tombstones.supersede_cycles(store)
        self.assertEqual(len(cycles), 1)
        self.assertEqual(set(cycles[0]), {"Alpha", "Beta"})

    def test_three_node_cycle_is_detected(self):
        store = {
            "Alpha": self._stone("Alpha", "Beta"),
            "Beta": self._stone("Beta", "Gamma"),
            "Gamma": self._stone("Gamma", "Alpha"),
        }
        cycles = tombstones.supersede_cycles(store)
        self.assertEqual(len(cycles), 1)
        self.assertEqual(set(cycles[0]), {"Alpha", "Beta", "Gamma"})

    def test_a_lead_in_chain_is_excluded_from_the_reported_cycle(self):
        store = {
            "X": self._stone("X", "Alpha"),
            "Alpha": self._stone("Alpha", "Beta"),
            "Beta": self._stone("Beta", "Alpha"),
        }
        cycles = tombstones.supersede_cycles(store)
        self.assertEqual(len(cycles), 1)
        self.assertEqual(set(cycles[0]), {"Alpha", "Beta"})


if __name__ == "__main__":
    unittest.main()
