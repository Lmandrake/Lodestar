"""Tests for lib.machine's host declarations -- which git remote hosts a machine can reach.

Mirrors tests/test_claude_labels.py's shape: this is the same kind of feature (a per-machine
declared fact, read from machines.md, with an explicit safe default for silence).
"""

import os
import tempfile
import unittest

from lib import machine
from tests import needs_private, real_home_key, real_key  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HOSTED = """| Key | Role | Locks | Labels | Hosts | Notes |
|-----|------|-------|--------|-------|-------|
| test-box | testbox | | mac, work | github.com, git.private.example | reaches both |
| home-box | home | flag | windows-wsl, home | github.com | reaches only github.com |
| bare-box | bare | | | | no hosts declared |
"""


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


class TestHostsTable(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-hosts-")
        self.path = _write(os.path.join(self.tmp, "machines.md"), HOSTED)

    def test_hosts_are_read_per_machine_in_declared_order(self):
        table = machine.hosts_table(self.path)
        self.assertEqual(table["test-box"], ["github.com", "git.private.example"])
        self.assertEqual(table["home-box"], ["github.com"])
        self.assertEqual(table["bare-box"], [])

    def test_a_table_with_no_hosts_column_reads_as_undeclared(self):
        old = _write(os.path.join(self.tmp, "old.md"),
                     "| Key | Role |\n|-----|------|\n| a | b |\n")
        self.assertEqual(machine.hosts_table(old), {"a": []})


class TestHostReachable(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-hosts-")
        self.path = _write(os.path.join(self.tmp, "machines.md"), HOSTED)

    def test_reaches_a_declared_host(self):
        self.assertTrue(machine.host_reachable("github.com", "test-box", self.path))
        self.assertTrue(machine.host_reachable(
            "git.private.example", "test-box", self.path))

    def test_does_not_reach_an_undeclared_host(self):
        self.assertFalse(machine.host_reachable(
            "git.private.example", "home-box", self.path))

    def test_comparison_is_case_insensitive(self):
        self.assertTrue(machine.host_reachable(
            "GitHub.COM", "home-box", self.path))
        self.assertTrue(machine.host_reachable(
            "github.com", "HOME-BOX", self.path))

    def test_a_blank_cell_reads_as_reachable(self):
        # bare-box declares no hosts at all -- silence must mean "assume reachable", never
        # "reaches nothing", or an undeclared machine would lose its whole fleet of repos.
        self.assertTrue(machine.host_reachable(
            "git.private.example", "bare-box", self.path))
        self.assertTrue(machine.host_reachable(
            "anything.example.com", "bare-box", self.path))

    def test_an_unregistered_key_reads_as_reachable(self):
        self.assertTrue(machine.host_reachable(
            "git.private.example", "nobody-heard-of", self.path))


@needs_private("machines.md", "sync/private-store.json")
class TestRealMachinesFile(unittest.TestCase):
    """The whole point of the feature: tower genuinely cannot reach the private host."""

    def test_tower_does_not_reach_the_private_host_but_the_macs_do(self):
        from lib import boundary, paths
        real = paths.machines_file()
        host = boundary.load(boundary.DEFAULT_CONFIG)["host"]  # the real deployment, not the fixture
        self.assertFalse(machine.host_reachable(
            host, real_home_key(), real))
        self.assertTrue(machine.host_reachable(
            host, real_key("desktop"), real))
        self.assertTrue(machine.host_reachable(
            host, real_key("laptop"), real))

    def test_fixture_sanity_check_would_flip_red_if_tower_were_given_the_private_host(self):
        # Confirms the assertion above is actually discriminating: build a fixture identical
        # to the real machines.md except tower's Hosts cell also lists the private host, and
        # verify the equivalent assertion goes RED there.
        from lib import boundary, paths
        host = boundary.load(boundary.DEFAULT_CONFIG)["host"]
        home_key = real_home_key()
        with open(paths.machines_file(), "r", encoding="utf-8") as handle:
            real_text = handle.read()
        given_private = real_text.replace(
            "| %s | home | flag | windows-wsl, home | github.com |" % home_key,
            "| %s | home | flag | windows-wsl, home | github.com, %s |" % (home_key, host),
        )
        self.assertNotEqual(given_private, real_text, "replacement did not match the real row")
        tmp = tempfile.mkdtemp(prefix="lodestar-hosts-sanity-")
        fixture = _write(os.path.join(tmp, "machines.md"), given_private)
        self.assertTrue(machine.host_reachable(host, home_key, fixture))


if __name__ == "__main__":
    unittest.main()
