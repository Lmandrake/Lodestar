"""Tests for lib.lifelock — the machine-local lock guarding one repo id's lifecycle move.

Two invariants carry the whole design, and both have already bitten this repo once each:

1. **Keyed on the id, never on the path.** §3D.3 of the repo-lifecycle design: "a path-keyed
   lock is released and re-derived by the very rename it guards, so it fails exactly when it
   is needed." `TestKeyedOnIdNotPath` proves the lock survives a directory rename that is
   never passed to any function here — because nothing here takes a repo directory as an
   input to identity in the first place.

2. **`held()` releases on an exception, not just on a clean exit.** A converger that throws
   mid-move (a bad precondition caught late, a failed `os.rename`) must not leave the lock
   held, or every later sweep for that repo wedges until a human deletes the file by hand.
   `TestHeldReleasesEvenOnFailure` proves the exception path explicitly, not just the happy
   path.

Everything else mirrors `tests/test_attend.py`'s shape on purpose: same asymmetry (a live
holder is honoured, a dead one is reclaimed), same refusal-must-name-the-holder rule
(`attend.judge_takeover`'s own reasoning — a refusal that does not name a pid pushes a human
to kill something blind), same unreadable-lock-refuses-on-no-evidence branch. Liveness is
always injected via `alive=`, never read from the real process table, so none of this
depends on what is actually running on the machine executing the suite.
"""

import inspect
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from lib import lifelock

NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)


class TestKeyedOnIdNotPath(unittest.TestCase):
    """Invariant 1. The only path this module accepts is where the LOCK FILE lives (a
    testing seam so the suite never touches ~/dev/.lodestar/locks/); the repo's own
    checkout directory is never an input anywhere in the API. A rename of that directory
    must therefore have zero effect on which lock is in force."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-lifelock-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # Route the module's DEFAULT path resolution into the tmp dir, so these tests can
        # call acquire()/read() with no `path=` override at all — exactly as production code
        # would — while still never touching the real lock directory.
        locks_dir = os.path.join(self.tmp, "locks")

        def fake_lock_file(repo_id):
            safe = "".join(ch if (ch.isalnum() or ch in "-_.") else "-" for ch in repo_id)
            return os.path.join(locks_dir, safe + ".lock")

        patcher = mock.patch.object(lifelock.paths, "lifecycle_lock_file", fake_lock_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_same_lock_is_held_across_a_directory_rename(self):
        repo_id = "widget-7f3a"
        old_dir = os.path.join(self.tmp, "widget-old")
        new_dir = os.path.join(self.tmp, "widget-new")
        os.makedirs(old_dir)

        got = lifelock.acquire(repo_id, pid=111, now=NOW)
        self.assertTrue(got["ok"], got["reason"])

        # The checkout this id names gets renamed on disk. No function above was ever told
        # about `old_dir` or `new_dir` -- that absence is the point.
        os.rename(old_dir, new_dir)

        held = lifelock.read(repo_id)
        self.assertIsNotNone(held, "the lock must not have moved, released, or vanished")
        self.assertEqual(held.pid, 111)

        # A second acquire for the SAME id, after the rename, still sees the same live
        # holder rather than finding "no lock" because the directory moved.
        second = lifelock.acquire(repo_id, pid=222, now=NOW, alive=lambda pid: True)
        self.assertFalse(second["ok"])
        self.assertEqual(second["holder"].pid, 111)

    def test_two_different_ids_never_collide_on_one_lock(self):
        """The converse of invariant 1: identity comes ONLY from the id, so two distinct
        ids must never be blocked by each other's lock."""
        first = lifelock.acquire("repo-a", pid=111, now=NOW)
        second = lifelock.acquire("repo-b", pid=222, now=NOW)
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])

    def test_acquire_never_accepts_a_directory_argument(self):
        """If this ever grows a `repo_path=` or similar, the invariant above is void by
        construction. Guard the signature itself, not just behaviour."""
        params = set(inspect.signature(lifelock.acquire).parameters)
        for forbidden in ("repo_path", "directory", "dir", "checkout"):
            self.assertNotIn(forbidden, params)


class TestHeldReleasesEvenOnFailure(unittest.TestCase):
    """Invariant 2. A converger that raises mid-move must not wedge the lock."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-lifelock-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "widget-7f3a.lock")

    def test_a_clean_exit_releases(self):
        with lifelock.held("widget-7f3a", path=self.path, pid=111, now=NOW) as got:
            self.assertEqual(got.pid, 111)
        self.assertIsNone(lifelock.read("widget-7f3a", path=self.path))

    def test_an_exception_inside_the_block_still_releases(self):
        """The exact failure mode named in the task: a converger throws partway through a
        move and must not leave the lock held for the next sweep to trip over."""
        class ConvergerExploded(Exception):
            pass

        with self.assertRaises(ConvergerExploded):
            with lifelock.held("widget-7f3a", path=self.path, pid=111, now=NOW):
                # Simulate real work happening (the lock is genuinely held here)...
                self.assertEqual(lifelock.read("widget-7f3a", path=self.path).pid, 111)
                # ...then the converger blows up mid-move, before it reaches a clean return.
                raise ConvergerExploded("os.rename failed halfway through the satellites")

        self.assertIsNone(lifelock.read("widget-7f3a", path=self.path),
                          "the lock must be released even though the body raised")

    def test_after_a_release_on_exception_a_fresh_acquire_succeeds(self):
        """Releasing is not enough if the file is left in a state nothing can reacquire --
        confirm the NEXT sweep can actually get in."""
        class Boom(Exception):
            pass

        with self.assertRaises(Boom):
            with lifelock.held("widget-7f3a", path=self.path, pid=111, now=NOW):
                raise Boom()

        second = lifelock.acquire("widget-7f3a", path=self.path, pid=222, now=NOW)
        self.assertTrue(second["ok"], second["reason"])

    def test_held_raises_busy_naming_the_live_holder_and_never_acquires(self):
        lifelock.acquire("widget-7f3a", path=self.path, pid=111, now=NOW)
        with self.assertRaises(lifelock.Busy) as ctx:
            with lifelock.held("widget-7f3a", path=self.path, pid=222, now=NOW,
                               alive=lambda pid: True):
                self.fail("must never enter the body when the lock was not obtained")
        self.assertIn("111", str(ctx.exception))
        # The original holder must be untouched by the failed attempt.
        self.assertEqual(lifelock.read("widget-7f3a", path=self.path).pid, 111)


class TestAcquireReleaseRead(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-lifelock-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "widget-7f3a.lock")

    def test_a_fresh_acquire_succeeds_and_is_readable_back(self):
        got = lifelock.acquire("widget-7f3a", path=self.path, pid=1234, machine="mt-002",
                               now=NOW)
        self.assertTrue(got["ok"])
        self.assertFalse(got["reclaimed"])
        self.assertEqual(got["holder"].pid, 1234)
        held = lifelock.read("widget-7f3a", path=self.path)
        self.assertEqual(held.pid, 1234)
        self.assertEqual(held.repo_id, "widget-7f3a")
        self.assertEqual(held.machine, "mt-002")

    def test_re_entrant_acquire_by_the_same_pid_succeeds_without_reclaiming(self):
        lifelock.acquire("widget-7f3a", path=self.path, pid=1234, now=NOW)
        got = lifelock.acquire("widget-7f3a", path=self.path, pid=1234, now=NOW)
        self.assertTrue(got["ok"])
        self.assertFalse(got["reclaimed"])

    def test_a_second_acquire_against_a_live_pid_is_refused_and_names_the_holder(self):
        lifelock.acquire("widget-7f3a", path=self.path, pid=1234, machine="mt-002", now=NOW)
        got = lifelock.acquire("widget-7f3a", path=self.path, pid=5678, now=NOW,
                               alive=lambda pid: True)
        self.assertFalse(got["ok"])
        self.assertFalse(got["reclaimed"])
        self.assertEqual(got["holder"].pid, 1234)
        # Never an anonymous refusal -- attend.judge_takeover's whole reasoning, reused here.
        self.assertIn("1234", got["reason"])
        self.assertIn("mt-002", got["reason"])

    def test_a_refused_acquire_leaves_the_existing_lock_untouched(self):
        lifelock.acquire("widget-7f3a", path=self.path, pid=1234, machine="mt-002", now=NOW)
        before = open(self.path).read()
        lifelock.acquire("widget-7f3a", path=self.path, pid=5678, now=NOW,
                         alive=lambda pid: True)
        self.assertEqual(open(self.path).read(), before)

    def test_a_dead_holders_lock_is_reclaimed(self):
        lifelock.acquire("widget-7f3a", path=self.path, pid=1234, now=NOW - timedelta(hours=1))
        got = lifelock.acquire("widget-7f3a", path=self.path, pid=5678, now=NOW,
                               alive=lambda pid: False)
        self.assertTrue(got["ok"])
        self.assertTrue(got["reclaimed"])
        self.assertEqual(got["holder"].pid, 5678)
        self.assertIn("1234", got["reason"])
        held = lifelock.read("widget-7f3a", path=self.path)
        self.assertEqual(held.pid, 5678)

    def test_an_unreadable_lock_refuses_on_no_evidence_and_names_the_path_to_delete(self):
        with open(self.path, "w") as handle:
            handle.write("{not json")
        got = lifelock.acquire("widget-7f3a", path=self.path, pid=5678, now=NOW)
        self.assertFalse(got["ok"])
        self.assertFalse(got["reclaimed"])
        self.assertIsNone(got["holder"])
        self.assertIn(self.path, got["reason"])
        self.assertIn("delete", got["reason"].lower())

    def test_acquire_creates_the_directory_it_needs(self):
        nested = os.path.join(self.tmp, "deeper", "widget-7f3a.lock")
        got = lifelock.acquire("widget-7f3a", path=nested, pid=1234, now=NOW)
        self.assertTrue(got["ok"])
        self.assertTrue(os.path.exists(nested))

    def test_release_by_the_holder_removes_the_lock(self):
        lifelock.acquire("widget-7f3a", path=self.path, pid=1234, now=NOW)
        self.assertTrue(lifelock.release("widget-7f3a", path=self.path, pid=1234))
        self.assertFalse(os.path.exists(self.path))

    def test_release_by_anyone_else_is_refused_and_leaves_it_alone(self):
        lifelock.acquire("widget-7f3a", path=self.path, pid=1234, now=NOW)
        self.assertFalse(lifelock.release("widget-7f3a", path=self.path, pid=5678))
        self.assertTrue(os.path.exists(self.path))

    def test_release_of_a_missing_lock_is_not_an_error(self):
        self.assertFalse(lifelock.release("widget-7f3a", path=self.path, pid=1234))

    def test_read_of_a_missing_lock_is_none_not_a_crash(self):
        self.assertIsNone(lifelock.read("widget-7f3a", path=self.path))

    def test_the_lock_records_enough_to_name_the_holder_to_a_human(self):
        lifelock.acquire("widget-7f3a", path=self.path, pid=1234, machine="mt-002", now=NOW)
        data = json.load(open(self.path))
        for field in ("repo_id", "pid", "machine", "at"):
            self.assertIn(field, data)


if __name__ == "__main__":
    unittest.main()
