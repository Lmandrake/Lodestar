"""SWEEP_TIMEOUTS_RECONCILE_TEN_MINUTES_APART: the WSL-box systemd limit must outlast sync.py's
own watchdog plus the sweep-lock wait, or systemd kills the unit blind before the watchdog can
record where it was stuck."""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin"))
import sync  # noqa: E402
from lib import sweeplock  # noqa: E402

UNIT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "sync", "systemd", "lodestar-sync.service")


class SweepTimeoutsTest(unittest.TestCase):
    def test_systemd_outlasts_the_watchdog_and_the_lock_wait(self):
        text = open(UNIT, encoding="utf-8").read()
        m = re.search(r"^TimeoutStartSec=(\d+)min", text, re.M)
        self.assertIsNotNone(m, "unit has no TimeoutStartSec in minutes")
        limit = int(m.group(1)) * 60
        need = sync.HARD_DEADLINE_S + sweeplock.SWEEP_WAIT_S
        self.assertGreater(limit, need, "systemd would kill the sweep before its watchdog fires")

    def test_soft_deadline_is_before_the_hard_one(self):
        self.assertLess(sync.SOFT_DEADLINE_S, sync.HARD_DEADLINE_S)


if __name__ == "__main__":
    unittest.main()
