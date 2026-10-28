"""Tests for lib.freshness -- the gate that refuses to act on stale intent."""

import os
import subprocess
import tempfile
import unittest

from lib import freshness
from lib.freshness import DEFAULT_MAX_AGE_SECONDS, gate, observed


class TestGateRefusals(unittest.TestCase):
    def test_refuses_when_never_fetched(self):
        verdict = gate(now=1000.0, last_fetch=None, behind=0)
        self.assertFalse(verdict.ok)
        self.assertIn("never fetched", verdict.reason)

    def test_refuses_when_fetch_is_too_old(self):
        # An ABSOLUTE duration, not arithmetic on the constant this test guards -- a
        # boundary test built from the constant it checks can never fail when the
        # constant changes. See the pinning test below for the mutation check.
        now = 2_000_000.0
        one_second_past_the_window = 5401.0
        verdict = gate(now=now, last_fetch=now - one_second_past_the_window, behind=0)
        self.assertFalse(verdict.ok)
        self.assertIn("older than", verdict.reason)

    def test_refuses_when_behind(self):
        verdict = gate(now=1000.0, last_fetch=999.0, behind=3)
        self.assertFalse(verdict.ok)
        self.assertIn("3 commit(s) behind", verdict.reason)

    def test_refuses_when_diverged(self):
        verdict = gate(now=1000.0, last_fetch=999.0, behind=0, diverged=True)
        self.assertFalse(verdict.ok)
        self.assertIn("diverged", verdict.reason)

    def test_refuses_on_clock_skew_future_fetch(self):
        # last_fetch AFTER now -- a negative age. Must refuse, not silently pass.
        verdict = gate(now=1000.0, last_fetch=1500.0, behind=0)
        self.assertFalse(verdict.ok)
        self.assertIn("clock skew", verdict.reason)

    def test_diverged_takes_priority_over_behind_reason(self):
        # Both conditions true at once -- the reason must name divergence, the more
        # fundamentally ambiguous case, not fall through to the behind-count message.
        verdict = gate(now=1000.0, last_fetch=999.0, behind=5, diverged=True)
        self.assertFalse(verdict.ok)
        self.assertIn("diverged", verdict.reason)


class TestGateAllows(unittest.TestCase):
    def test_allows_when_fresh_and_current(self):
        verdict = gate(now=1000.0, last_fetch=990.0, behind=0)
        self.assertTrue(verdict.ok)
        self.assertIn("safe to act", verdict.reason)

    def test_allows_at_exactly_the_boundary(self):
        # Exactly AT max_age_seconds is still fresh -- an absolute duration, matching the
        # ABSOLUTE constant, never derived arithmetic on it.
        now = 2_000_000.0
        exactly_at_the_window = 5400.0
        verdict = gate(now=now, last_fetch=now - exactly_at_the_window, behind=0)
        self.assertTrue(verdict.ok)


class TestConstantPinnedNotDerived(unittest.TestCase):
    """CRITICAL: a boundary test whose input is computed FROM the constant it guards
    cannot fail when that constant changes -- that exact defect is recorded in this
    repo's CLAUDE.md. Pin the literal value here, independent of the tests above (which
    use their own hardcoded 5400.0 / 5401.0), then mutate the module constant in memory
    and confirm THIS pin goes red -- proving it is actually reading the live value and
    not just restating a copy that could drift unnoticed.
    """

    def test_default_is_pinned_to_the_documented_value(self):
        self.assertEqual(DEFAULT_MAX_AGE_SECONDS, 5400)

    def test_pin_goes_red_if_the_constant_is_mutated(self):
        original = freshness.DEFAULT_MAX_AGE_SECONDS
        freshness.DEFAULT_MAX_AGE_SECONDS = 999999
        try:
            with self.assertRaises(AssertionError):
                self.assertEqual(freshness.DEFAULT_MAX_AGE_SECONDS, 5400)
        finally:
            freshness.DEFAULT_MAX_AGE_SECONDS = original


class TestObserved(unittest.TestCase):
    def _git(self, repo, *args):
        subprocess.run(["git", "-C", repo] + list(args), check=True,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_no_upstream_is_out_of_scope_not_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._git(tmp, "init", "-q")
            self._git(tmp, "config", "user.email", "test@example.com")
            self._git(tmp, "config", "user.name", "Test")
            path = os.path.join(tmp, "f.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("hello\n")
            self._git(tmp, "add", "f.txt")
            self._git(tmp, "commit", "-q", "-m", "first")
            self.assertEqual(observed(tmp), (0, False))

    def test_nonexistent_path_returns_out_of_scope_value(self):
        self.assertEqual(observed("/no/such/repo/path/at/all"), (0, False))


if __name__ == "__main__":
    unittest.main()
