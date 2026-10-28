"""Tests for lib.journal — the append-only, conflict-free event log."""

import json
import os
import shutil
import tempfile
import unittest

from lib import journal, paths, proseguard

MACHINES = """| Key | Role |
|-----|------|
| test-box | testbox |
"""


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="lodestar-journal-")
        self.addCleanup(shutil.rmtree, self.root, True)

        self.machines = os.path.join(self.root, "machines.md")
        with open(self.machines, "w", encoding="utf-8") as handle:
            handle.write(MACHINES)

        self._saved = {
            k: os.environ.get(k)
            for k in ("LODESTAR_ROOT", "LODESTAR_MACHINE", "LODESTAR_RUNTIME")
        }
        os.environ["LODESTAR_ROOT"] = self.root
        os.environ["LODESTAR_MACHINE"] = "test-box"
        os.environ["LODESTAR_RUNTIME"] = "cc"
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _append(self, loop, event, **kwargs):
        return journal.append(loop, event, machines_path=self.machines, **kwargs)

    def _write_raw(self, day, name, text):
        directory = os.path.join(self.root, "journal", day)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path


class TestAppend(JournalTest):
    def test_writes_a_file_under_todays_directory(self):
        path = self._append("lodestar", "open")
        self.assertTrue(os.path.exists(path))
        self.assertTrue(path.startswith(os.path.join(self.root, "journal")))
        self.assertTrue(path.endswith(".json"))

    def test_records_the_documented_schema(self):
        path = self._append("lodestar", "touch", note="drafted section 2", tokens=18400)
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
        self.assertEqual(record["loop"], "lodestar")
        self.assertEqual(record["event"], "touch")
        self.assertEqual(record["machine"], "testbox")
        self.assertEqual(record["runtime"], "cc")
        self.assertNotIn("note", record)  # ruling 5: prose lives in the private store
        self.assertRegex(record["note_ref"], r"^jnote_[0-9a-f]{12}$")
        self.assertEqual(proseguard.note_text(record), "drafted section 2")
        self.assertEqual(record["tokens"], 18400)
        self.assertIn("ts", record)

    def test_omits_optional_fields_when_absent(self):
        path = self._append("lodestar", "touch")
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
        self.assertNotIn("note", record)
        self.assertNotIn("tokens", record)

    def test_rejects_an_undocumented_event_type(self):
        with self.assertRaises(journal.UnknownEvent):
            self._append("lodestar", "frobnicate")

    def test_skill_prune_is_an_accepted_event_type(self):
        """claudeskills journals prunes; append() raises UnknownEvent for anything unlisted."""
        self.assertIn("skill-prune", journal.EVENT_TYPES)

    def test_handoff_is_an_accepted_event_type(self):
        """The reboot ritual in claude/global/55-handoff-on-reboot.md instructs every session to
        "append a `handoff` event to journal/<date>/" before a restart. append() raised
        UnknownEvent for it, so the one event the ritual names could not be written — the
        instruction and the vocabulary disagreed, and the instruction governs every session.
        """
        self.assertIn("handoff", journal.EVENT_TYPES)
        path = self._append("lodestar", "handoff", note="where the work stands")
        self.assertTrue(os.path.exists(path))

    def test_a_handoff_does_not_reopen_or_close_a_loop(self):
        """It records a moment, never a state change: derive must leave state alone and only
        move last_touch, the same way `run` does."""
        from lib import loops
        events = [
            {"ts": "2026-09-15T10:00:00.000000-07:00", "loop": "x", "event": "close",
             "state": "closed"},
            {"ts": "2026-09-15T11:00:00.000000-07:00", "loop": "x", "event": "handoff"},
        ]
        derived = loops.derive(events)
        self.assertEqual(derived["x"]["state"], "closed",
                         "a handoff must not resurrect a closed loop")

    def test_every_event_gets_a_unique_filename(self):
        # This is the whole conflict-free premise: same loop, same second, same machine.
        written = {self._append("lodestar", "touch") for _ in range(25)}
        self.assertEqual(len(written), 25)

    def test_leaves_no_temp_file_behind(self):
        self._append("lodestar", "open")
        for _, _, files in os.walk(os.path.join(self.root, "journal")):
            self.assertEqual([f for f in files if ".tmp" in f], [])

    def test_extra_fields_are_merged(self):
        path = self._append("lodestar", "close", extra={"spent": "75m"})
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["spent"], "75m")


class TestLoad(JournalTest):
    def test_empty_board_loads_cleanly(self):
        events, problems = journal.load()
        self.assertEqual(events, [])
        self.assertEqual(problems, [])

    def test_returns_events_oldest_first_across_days(self):
        self._write_raw("2026-08-29", "100-aaa.json",
                        json.dumps({"ts": "2026-08-29T09:00:00-07:00",
                                    "loop": "b", "event": "touch"}))
        self._write_raw("2026-08-28", "200-bbb.json",
                        json.dumps({"ts": "2026-08-28T09:00:00-07:00",
                                    "loop": "a", "event": "open"}))
        events, problems = journal.load()
        self.assertEqual([e["loop"] for e in events], ["a", "b"])
        self.assertEqual(problems, [])

    def test_ordering_is_by_timestamp_not_directory_name(self):
        # A later-named file holding an earlier timestamp must still replay first.
        self._write_raw("2026-08-28", "999-zzz.json",
                        json.dumps({"ts": "2026-08-28T08:00:00-07:00",
                                    "loop": "early", "event": "open"}))
        self._write_raw("2026-08-28", "111-aaa.json",
                        json.dumps({"ts": "2026-08-28T10:00:00-07:00",
                                    "loop": "late", "event": "touch"}))
        events, _ = journal.load()
        self.assertEqual([e["loop"] for e in events], ["early", "late"])

    def test_a_corrupt_file_is_reported_not_raised(self):
        self._write_raw("2026-08-28", "100-aaa.json", "{ this is not json")
        self._append("lodestar", "open")
        events, problems = journal.load()
        self.assertEqual(len(events), 1)  # the good event still loads
        self.assertEqual(len(problems), 1)
        self.assertIn("100-aaa.json", problems[0][0])

    def test_a_json_file_that_is_not_an_event_is_reported(self):
        self._write_raw("2026-08-28", "100-aaa.json", json.dumps(["a", "list"]))
        self._write_raw("2026-08-28", "101-bbb.json", json.dumps({"no": "loop key"}))
        events, problems = journal.load()
        self.assertEqual(events, [])
        self.assertEqual(len(problems), 2)

    def test_ignores_non_json_files(self):
        self._write_raw("2026-08-28", ".gitkeep", "")
        self._write_raw("2026-08-28", "100-aaa.json.tmp", "half written")
        events, problems = journal.load()
        self.assertEqual(events, [])
        self.assertEqual(problems, [])

    def test_an_event_missing_its_timestamp_still_loads(self):
        self._write_raw("2026-08-28", "100-aaa.json",
                        json.dumps({"loop": "x", "event": "open"}))
        events, problems = journal.load()
        self.assertEqual(len(events), 1)
        self.assertEqual(problems, [])

    def test_rapid_successive_events_replay_in_the_order_written(self):
        # Regression: with second-precision timestamps these tied and the tiebreak was a
        # random uuid, so replay order -- and therefore derived state -- was nondeterministic.
        for index in range(20):
            self._append("lodestar", "touch", note="step-%02d" % index)
        events, problems = journal.load()
        self.assertEqual(problems, [])
        self.assertEqual(
            [proseguard.note_text(event) for event in events],
            ["step-%02d" % index for index in range(20)],
        )

    def test_timestamps_carry_sub_second_precision(self):
        path = self._append("lodestar", "touch")
        with open(path, encoding="utf-8") as handle:
            self.assertIn(".", json.load(handle)["ts"].split("T")[1])

    def test_for_loop_filters_by_loop_id(self):
        self._append("alpha", "open")
        self._append("beta", "open")
        self._append("alpha", "touch")
        self.assertEqual(len(journal.for_loop("alpha")), 2)
        self.assertEqual(len(journal.for_loop("beta")), 1)
        self.assertEqual(journal.for_loop("gamma"), [])

    def test_paths_module_agrees_with_the_override(self):
        self.assertEqual(paths.root(), self.root)


class TestClaudeskillsJournalPruneWritesARealEvent(JournalTest):
    """`claudeskills.journal_prune` end to end, against a real journal root.

    The five TestPruneJournalling cases in test_claudeskills.py all inject a lambda for
    `on_prune`, so the actual `journal.append(...)` call inside `journal_prune` (Important 3
    in the 2026-09-03 review) was never exercised by any test in the suite. Its own
    exceptions are swallowed there AND again in `apply`, so drift in `append`'s signature,
    `EVENT_TYPES`, or `paths.journal_dir()` would make spec §5 / D27 a silent no-op: link
    deleted, no record, green suite. This reuses the JournalTest fixture (redirects the
    journal root via LODESTAR_ROOT) rather than inventing a second mechanism.
    """

    def test_journal_prune_writes_a_real_skill_prune_event(self):
        from lib import claudeskills
        claudeskills.journal_prune("visual-intent", "/some/target")

        events, problems = journal.load()
        self.assertEqual(problems, [])
        self.assertEqual(len(events), 1)
        record = events[0]
        self.assertEqual(record["event"], "skill-prune")
        self.assertEqual(record["loop"], "lodestar")
        self.assertEqual(record["skill"], "visual-intent")
        self.assertEqual(record["target"], "/some/target")


if __name__ == "__main__":
    unittest.main()
