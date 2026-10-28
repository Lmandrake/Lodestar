"""Tests for lib.machine, lib.mdtable, and lib.clock — the identity and time layer."""

import datetime
import os
import tempfile
import unittest

from lib import clock, machine, mdtable
from tests import needs_private  # noqa: E402

MACHINES_FIXTURE = """# Machines

Some prose that must be ignored.

| Key | Role | Notes |
|-----|------|-------|
| mt-001 | desktop | Work Desktop, always on AC |
| mb-999999 | laptop | Mobile laptop |

<!-- a trailing comment -->
"""


class EnvGuard(unittest.TestCase):
    """Restores the environment after each test, since these modules read it."""

    KEYS = ("LODESTAR_MACHINE", "LODESTAR_RUNTIME", "CLAUDECODE")

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.KEYS}
        machine._key_cache = None

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        machine._key_cache = None

    def _fixture(self, text=MACHINES_FIXTURE):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, encoding="utf-8"
        )
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name


class TestMdTable(unittest.TestCase):
    def test_parses_rows_keyed_by_lowercased_header(self):
        rows = mdtable.parse(MACHINES_FIXTURE)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["key"], "mt-001")
        self.assertEqual(rows[0]["role"], "desktop")
        self.assertEqual(rows[1]["notes"], "Mobile laptop")

    def test_returns_empty_list_when_there_is_no_table(self):
        self.assertEqual(mdtable.parse("# Just a heading\n\nprose\n"), [])

    def test_ignores_prose_after_the_table(self):
        rows = mdtable.parse(MACHINES_FIXTURE + "\n\nmore prose\n| not | a table |\n")
        self.assertEqual(len(rows), 2)

    def test_tolerates_a_row_with_missing_trailing_cells(self):
        rows = mdtable.parse("| a | b | c |\n|---|---|---|\n| 1 | 2 |\n")
        self.assertEqual(rows[0], {"a": "1", "b": "2", "c": ""})

    def test_skips_a_row_of_entirely_empty_cells(self):
        rows = mdtable.parse("| a | b |\n|---|---|\n|  |  |\n| 1 | 2 |\n")
        self.assertEqual(rows, [{"a": "1", "b": "2"}])

    def test_does_not_mistake_a_separator_for_a_header(self):
        # An alignment separator like |:---|---:| must not be read as the header row.
        rows = mdtable.parse("| a | b |\n|:---|---:|\n| 1 | 2 |\n")
        self.assertEqual(rows, [{"a": "1", "b": "2"}])


class TestClock(unittest.TestCase):
    def test_now_round_trips_through_parse(self):
        stamp = clock.now()
        self.assertEqual(clock.parse(stamp).isoformat(timespec="seconds"), stamp)

    def test_now_carries_an_offset(self):
        self.assertIsNotNone(clock.parse(clock.now()).tzinfo)

    def test_sort_key_orders_across_a_dst_boundary(self):
        # String sorting would put the -08:00 stamp first; real ordering is the reverse.
        winter = "2026-01-15T12:00:00-08:00"
        summer = "2026-07-15T12:00:00-07:00"
        self.assertLess(clock.sort_key(winter), clock.sort_key(summer))

    def test_sort_key_tolerates_missing_and_malformed_timestamps(self):
        # Journal replay must not crash on one bad event.
        for bad in (None, "", "not-a-timestamp"):
            self.assertIsInstance(clock.sort_key(bad), datetime.datetime)
        self.assertLess(clock.sort_key("garbage"), clock.sort_key(clock.now()))

    def test_parse_assumes_local_for_a_naive_timestamp(self):
        self.assertIsNotNone(clock.parse("2026-08-31T11:45:00").tzinfo)

    def test_today_and_epoch_shapes(self):
        self.assertRegex(clock.today(), r"^\d{4}-\d{2}-\d{2}$")
        self.assertGreater(clock.epoch(), 1_700_000_000)


class TestMachineKey(EnvGuard):
    def test_env_override_wins_and_is_normalized(self):
        os.environ["LODESTAR_MACHINE"] = "  MT-001  "
        self.assertEqual(machine.key(), "mt-001")

    def test_real_key_matches_waypoint_on_this_machine(self):
        # Guards decision D-B: if these ever disagree, the soft-lock is unreliable.
        if not os.path.exists(machine.WAYPOINT):
            self.skipTest("Waypoint not installed")
        self.assertEqual(machine.key(), machine._ask_waypoint())


class TestMachineRole(EnvGuard):
    def test_looks_up_the_role_for_this_key(self):
        os.environ["LODESTAR_MACHINE"] = "mb-999999"
        self.assertEqual(machine.role(self._fixture()), "laptop")

    def test_unregistered_machine_raises_rather_than_guessing(self):
        os.environ["LODESTAR_MACHINE"] = "unknown-box"
        with self.assertRaises(machine.UnknownMachine):
            machine.role(self._fixture())

    def test_error_names_the_key_and_the_file(self):
        os.environ["LODESTAR_MACHINE"] = "unknown-box"
        path = self._fixture()
        with self.assertRaises(machine.UnknownMachine) as caught:
            machine.role(path)
        self.assertIn("unknown-box", str(caught.exception))
        self.assertIn(path, str(caught.exception))

    @needs_private("machines.md")
    def test_this_machine_is_registered_in_the_real_machines_file(self):
        # The real board must know this box, or every touch here would exit 2. Asserts that
        # a role resolves, NOT that it is "desktop" -- hardcoding that made this the only
        # failure in the Laptop's first bootstrap run, which was this test's fault.
        self.assertIn(machine.role(), set(machine.roles().values()))


class TestRuntime(EnvGuard):
    def test_claude_code_env_means_cc(self):
        os.environ.pop("LODESTAR_RUNTIME", None)
        os.environ["CLAUDECODE"] = "1"
        self.assertEqual(machine.runtime(), "cc")

    def test_absence_of_claude_code_means_shell_not_cowork(self):
        # Regression: a plain terminal, cron, and the launchd sweep were all being stamped
        # "cowork" -- the runtime that is forbidden from mutating Dialexis. Cowork is never
        # inferred, only declared.
        os.environ.pop("LODESTAR_RUNTIME", None)
        os.environ.pop("CLAUDECODE", None)
        self.assertEqual(machine.runtime(), "shell")

    def test_cowork_must_declare_itself(self):
        os.environ.pop("CLAUDECODE", None)
        os.environ["LODESTAR_RUNTIME"] = "cowork"
        self.assertEqual(machine.runtime(), "cowork")

    def test_explicit_override_wins(self):
        os.environ["CLAUDECODE"] = "1"
        os.environ["LODESTAR_RUNTIME"] = "cowork"
        self.assertEqual(machine.runtime(), "cowork")


class TestStamp(EnvGuard):
    def test_stamp_is_role_slash_runtime(self):
        os.environ["LODESTAR_MACHINE"] = "mt-001"
        os.environ["LODESTAR_RUNTIME"] = "cc"
        self.assertEqual(machine.stamp(self._fixture()), "desktop/cc")


if __name__ == "__main__":
    unittest.main()
