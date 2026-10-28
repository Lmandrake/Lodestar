"""Tests for lib.attend — the lease that makes "interactive" a mechanism.

`bin/execute.py` is stamped "INTERACTIVE ONLY — never run from launchd", and a stamp is a hope.
A sidecar left running overnight satisfies every check that comment can make while being the
exact opposite of interactive: nobody is at the screen, and it holds a live path to Graph
mutations. So attendance has to be *derived from a recent human click*, and the two failures
below are what these tests exist to make impossible.

**1. Executing with nobody there.** A worker that acts because a process is running will send
mail from an unattended machine. So `attending()` refuses when no click has ever landed, and
refuses again once the last one has aged out — and the refusal must name the age, because
"refused" with no number is indistinguishable from a bug.

**2. Two sidecars acting on the same accepted bubbles.** That is a double-send, the worst
failure available in this design. `acquire()` is atomic (`O_EXCL`) and a second caller gets
read-only *while still serving the page* — refusing to start outright would push him to kill the
first process blind, which is how someone loses a session they were using.

The asymmetry between those two is deliberate: a *live* holder means read-only, and a holder
whose pid is gone is reclaimed. Nothing else can distinguish "someone is working" from "a
process died holding the door shut".

**And the tests below never derive their input from the bound they guard.** A previous safety
test computed `EXECUTING_TIMEOUT_SECONDS + 30` as its input and stayed green when the bound was
raised to infinity: it asserted "beyond the bound behaves correctly" while being structurally
incapable of noticing the bound's removal. So the ages here are absolute durations and the
constant is pinned separately.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "bin"))

from lib import attend  # noqa: E402

NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc)


def ago(**kwargs) -> str:
    return (NOW - timedelta(**kwargs)).isoformat()


class TestAttendanceIsRecentHumanContact(unittest.TestCase):
    """The pure half: does a click this recent count as someone being there?"""

    def test_a_click_a_minute_ago_is_attendance(self):
        got = attend.judge_attendance(ago(minutes=1), now=NOW)
        self.assertTrue(got["ok"], got["reason"])

    def test_a_click_sixteen_minutes_ago_is_not(self):
        """Absolute duration, deliberately not computed from CLICK_WINDOW_SECONDS."""
        got = attend.judge_attendance(ago(minutes=16), now=NOW)
        self.assertFalse(got["ok"])
        self.assertIn("960", got["reason"])

    def test_an_hour_ago_is_not_attendance(self):
        self.assertFalse(attend.judge_attendance(ago(hours=1), now=NOW)["ok"])

    def test_never_clicked_is_refused_and_says_so_differently(self):
        """A launchd-started sidecar has a pid and no human. It must not read as merely stale."""
        got = attend.judge_attendance(None, now=NOW)
        self.assertFalse(got["ok"])
        self.assertIn("no click", got["reason"].lower())
        self.assertIsNone(got["seconds"])

    def test_an_unparseable_stamp_refuses_rather_than_defaulting_to_now(self):
        got = attend.judge_attendance("last tuesday", now=NOW)
        self.assertFalse(got["ok"])
        self.assertIn("unreadable", got["reason"].lower())

    def test_a_click_in_the_future_is_refused(self):
        """Clock skew must not manufacture attendance that outlasts the window."""
        got = attend.judge_attendance((NOW + timedelta(hours=2)).isoformat(), now=NOW)
        self.assertFalse(got["ok"])

    def test_a_naive_stamp_is_read_as_utc_not_rejected(self):
        naive = (NOW - timedelta(minutes=2)).replace(tzinfo=None).isoformat()
        self.assertTrue(attend.judge_attendance(naive, now=NOW)["ok"])

    def test_the_window_is_fifteen_minutes(self):
        """Pinned separately from the tests above, which is what makes them falsifiable."""
        self.assertEqual(attend.CLICK_WINDOW_SECONDS, 900)

    def test_the_refusal_names_the_remedy(self):
        got = attend.judge_attendance(ago(minutes=16), now=NOW)
        self.assertIn("click", got["reason"].lower())


class TestTakeover(unittest.TestCase):
    """The pure half of mutual exclusion: may this process hold the door?"""

    def a_lease(self, pid=4242, last_click=None):
        return attend.Lease(pid=pid, machine="mt-002", started=ago(minutes=5),
                            last_click=last_click)

    def test_no_lease_at_all_means_interactive(self):
        got = attend.judge_takeover(None, our_pid=99, alive=False, readable=True)
        self.assertEqual(got["mode"], attend.INTERACTIVE)
        self.assertFalse(got["reclaim"])

    def test_our_own_lease_is_re_entrant(self):
        got = attend.judge_takeover(self.a_lease(pid=99), our_pid=99, alive=True, readable=True)
        self.assertEqual(got["mode"], attend.INTERACTIVE)
        self.assertFalse(got["reclaim"])

    def test_a_live_holder_makes_us_read_only_and_is_named(self):
        got = attend.judge_takeover(self.a_lease(pid=4242), our_pid=99, alive=True, readable=True)
        self.assertEqual(got["mode"], attend.READONLY)
        self.assertEqual(got["holder_pid"], 4242)
        self.assertIn("4242", got["reason"])

    def test_read_only_never_asks_him_to_kill_something_unnamed(self):
        got = attend.judge_takeover(self.a_lease(pid=4242), our_pid=99, alive=True, readable=True)
        self.assertIn("mt-002", got["reason"])

    def test_a_dead_holder_is_reclaimed(self):
        got = attend.judge_takeover(self.a_lease(pid=4242), our_pid=99, alive=False, readable=True)
        self.assertEqual(got["mode"], attend.INTERACTIVE)
        self.assertTrue(got["reclaim"])
        self.assertIn("4242", got["reason"])

    def test_an_unreadable_lease_cannot_prove_the_holder_is_gone(self):
        """The one case where we refuse interactive on no evidence — and hand him a path."""
        got = attend.judge_takeover(None, our_pid=99, alive=False, readable=False,
                                    path="/tmp/command.lease")
        self.assertEqual(got["mode"], attend.READONLY)
        self.assertIn("/tmp/command.lease", got["reason"])
        self.assertIn("delete", got["reason"].lower())


class TestLeaseOnDisk(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "command.lease")

    def tearDown(self):
        self.tmp.cleanup()

    def test_acquire_writes_a_lease_and_grants_interactive(self):
        got = attend.acquire(self.path, pid=1234, machine="mt-002", now=NOW)
        self.assertEqual(got["mode"], attend.INTERACTIVE)
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(attend.read(self.path).pid, 1234)

    def test_a_fresh_lease_has_no_click_so_nothing_may_execute_yet(self):
        """Holding the lease is not attendance. Starting a sidecar is not a human being there."""
        attend.acquire(self.path, pid=1234, now=NOW)
        got = attend.attending(self.path, pid=1234, now=NOW)
        self.assertFalse(got["ok"])
        self.assertIn("no click", got["reason"].lower())

    def test_a_click_makes_it_attended_and_survives_a_reread(self):
        attend.acquire(self.path, pid=1234, now=NOW)
        attend.click(self.path, pid=1234, now=NOW)
        self.assertTrue(attend.attending(self.path, pid=1234, now=NOW)["ok"])
        self.assertEqual(attend.read(self.path).last_click, NOW.isoformat())

    def test_a_click_ages_out(self):
        attend.acquire(self.path, pid=1234, now=NOW - timedelta(hours=1))
        attend.click(self.path, pid=1234, now=NOW - timedelta(minutes=20))
        self.assertFalse(attend.attending(self.path, pid=1234, now=NOW)["ok"])

    def test_a_click_does_not_clobber_who_holds_the_lease(self):
        attend.acquire(self.path, pid=1234, machine="mt-002", now=NOW)
        attend.click(self.path, pid=1234, now=NOW)
        lease = attend.read(self.path)
        self.assertEqual(lease.pid, 1234)
        self.assertEqual(lease.machine, "mt-002")

    def test_only_the_holder_may_record_a_click(self):
        """Otherwise a second sidecar keeps the first one's worker alive by proxy."""
        attend.acquire(self.path, pid=1234, now=NOW)
        with self.assertRaises(attend.NotHolding):
            attend.click(self.path, pid=5678, now=NOW)

    def test_a_non_holder_is_never_attending_however_recent_the_click(self):
        attend.acquire(self.path, pid=1234, now=NOW)
        attend.click(self.path, pid=1234, now=NOW)
        got = attend.attending(self.path, pid=5678, now=NOW)
        self.assertFalse(got["ok"])
        self.assertIn("1234", got["reason"])

    def test_a_second_acquire_against_a_live_pid_gets_read_only(self):
        attend.acquire(self.path, pid=1234, now=NOW)
        got = attend.acquire(self.path, pid=5678, now=NOW, alive=lambda pid: True)
        self.assertEqual(got["mode"], attend.READONLY)
        self.assertEqual(got["holder_pid"], 1234)

    def test_a_refused_acquire_leaves_the_existing_lease_untouched(self):
        attend.acquire(self.path, pid=1234, machine="mt-002", now=NOW)
        before = open(self.path).read()
        attend.acquire(self.path, pid=5678, now=NOW, alive=lambda pid: True)
        self.assertEqual(open(self.path).read(), before)

    def test_a_dead_holders_lease_is_reclaimed_and_the_click_does_not_carry_over(self):
        """Inheriting the dead holder's click would grant attendance nobody gave."""
        attend.acquire(self.path, pid=1234, now=NOW - timedelta(minutes=2))
        attend.click(self.path, pid=1234, now=NOW - timedelta(minutes=1))
        got = attend.acquire(self.path, pid=5678, now=NOW, alive=lambda pid: False)
        self.assertEqual(got["mode"], attend.INTERACTIVE)
        self.assertEqual(attend.read(self.path).pid, 5678)
        self.assertIsNone(attend.read(self.path).last_click)
        self.assertFalse(attend.attending(self.path, pid=5678, now=NOW)["ok"])

    def test_a_corrupt_lease_serves_read_only_and_names_the_file(self):
        with open(self.path, "w") as handle:
            handle.write("{not json")
        got = attend.acquire(self.path, pid=5678, now=NOW)
        self.assertEqual(got["mode"], attend.READONLY)
        self.assertIn(self.path, got["reason"])

    def test_release_by_the_holder_removes_the_lease(self):
        attend.acquire(self.path, pid=1234, now=NOW)
        self.assertTrue(attend.release(self.path, pid=1234))
        self.assertFalse(os.path.exists(self.path))

    def test_release_by_anyone_else_is_refused_and_leaves_it_alone(self):
        attend.acquire(self.path, pid=1234, now=NOW)
        self.assertFalse(attend.release(self.path, pid=5678))
        self.assertTrue(os.path.exists(self.path))

    def test_release_of_a_missing_lease_is_not_an_error(self):
        self.assertFalse(attend.release(self.path, pid=1234))

    def test_read_of_a_missing_lease_is_none_not_a_crash(self):
        self.assertIsNone(attend.read(self.path))

    def test_the_lease_records_enough_to_name_the_holder_to_a_human(self):
        attend.acquire(self.path, pid=1234, machine="mt-002", now=NOW)
        data = json.load(open(self.path))
        for field in ("pid", "machine", "started"):
            self.assertIn(field, data)

    def test_acquire_creates_the_directory_it_needs(self):
        nested = os.path.join(self.tmp.name, "deeper", "command.lease")
        self.assertEqual(attend.acquire(nested, pid=1234, now=NOW)["mode"], attend.INTERACTIVE)


class TestLivenessIsRealNotAssumed(unittest.TestCase):

    def test_this_process_is_alive(self):
        self.assertTrue(attend.pid_alive(os.getpid()))

    def test_an_impossible_pid_is_not(self):
        self.assertFalse(attend.pid_alive(2 ** 31 - 1))

    def test_a_nonsense_pid_is_not_alive_rather_than_raising(self):
        for bad in (0, -1, None, "seventeen"):
            self.assertFalse(attend.pid_alive(bad), bad)


if __name__ == "__main__":
    unittest.main()
