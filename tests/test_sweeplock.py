"""Tests for bin/lib/sweeplock.py and its wiring into bin/sync.py (2026-10-26).

The lock is between two independent processes (the hourly sweep and Tender), so the tests that
matter run real second processes: a holder in a subprocess, a SIGKILLed holder, and a crowd of
mixed sweep/Tender processes whose critical sections must never overlap.
"""

import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

from lib import paths, sweeplock

BIN = os.path.dirname(os.path.dirname(os.path.abspath(sweeplock.__file__)))
LIB = os.path.join(BIN, "lib")

HOLDER = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, %(lib)r)
    import sweeplock
    with sweeplock.hold(sys.argv[1], 30, detail=sys.argv[2], path=sys.argv[3]) as grant:
        assert grant, grant.reason()
        print("held", flush=True)
        time.sleep(float(sys.argv[4]))
""") % {"lib": LIB}


class LockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-sweeplock-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "state", "sweep.lock")
        self.procs = []
        self.addCleanup(self._reap)

    def _reap(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            if proc.stdout:
                proc.stdout.close()

    def holder(self, role="sweep", detail="d", seconds=30.0, env=None):
        """A real second process holding the lock until killed or `seconds` pass."""
        proc = subprocess.Popen([sys.executable, "-c", HOLDER, role, detail, self.path,
                                 str(seconds)], stdout=subprocess.PIPE, env=env)
        self.procs.append(proc)
        self.assertEqual(proc.stdout.readline().strip(), b"held")
        return proc


class TestPath(unittest.TestCase):
    def test_default_path_agrees_with_paths_state_dir(self):
        """sweeplock re-derives the state dir (so Tender can vendor it); it must not drift."""
        with mock.patch.dict(os.environ, {"LODESTAR_STATE_DIR": "/x/state"}):
            os.environ.pop(sweeplock.PATH_ENV, None)
            self.assertEqual(sweeplock.lock_path(), paths.state_file("sweep.lock"))
        with mock.patch.dict(os.environ, {}):
            os.environ.pop(sweeplock.PATH_ENV, None)
            os.environ.pop("LODESTAR_STATE_DIR", None)
            self.assertEqual(sweeplock.lock_path(), paths.state_file("sweep.lock"))
            self.assertTrue(sweeplock.lock_path().endswith(
                os.path.join("dev", ".lodestar", "sweep.lock")))

    def test_explicit_env_wins(self):
        with mock.patch.dict(os.environ, {sweeplock.PATH_ENV: "/y/l.lock"}):
            self.assertEqual(sweeplock.lock_path(), "/y/l.lock")

    def test_vendorable_imports_nothing_but_flockwait(self):
        """Tender must not depend on the checkout: the module may import only stdlib + flockwait."""
        source = open(sweeplock.__file__, encoding="utf-8").read()
        self.assertNotIn("from . import paths", source)
        for line in source.splitlines():
            if line.startswith("from .") or line.strip().startswith("from . "):
                self.assertIn("flockwait", line)


class TestHold(LockTest):
    def test_acquire_writes_record_and_release_clears_it(self):
        with sweeplock.hold("tender", 1, detail="tdr-1", path=self.path) as grant:
            self.assertTrue(grant)
            record = sweeplock.read_record(self.path)
            self.assertEqual((record["role"], record["pid"], record["detail"]),
                             ("tender", os.getpid(), "tdr-1"))
            self.assertEqual(sweeplock.status(self.path)["state"], "held")
        self.assertIsNone(sweeplock.read_record(self.path))
        self.assertEqual(sweeplock.status(self.path)["state"], "free")

    def test_released_when_the_body_raises(self):
        with self.assertRaises(RuntimeError):
            with sweeplock.hold("sweep", 1, path=self.path):
                raise RuntimeError("boom")
        with sweeplock.hold("tender", 0, path=self.path) as grant:
            self.assertTrue(grant)

    def test_unknown_role_is_refused(self):
        with self.assertRaises(ValueError):
            with sweeplock.hold("cron", 1, path=self.path):
                pass

    def test_tender_refuses_while_a_sweep_holds_it_and_names_the_holder(self):
        proc = self.holder("sweep", "sync.py")
        with sweeplock.hold("tender", 0.3, path=self.path, poll=0.05) as grant:
            self.assertFalse(grant)
            self.assertTrue(grant.contended)
            self.assertEqual(grant.holder["role"], "sweep")
            self.assertEqual(grant.holder["pid"], proc.pid)
            self.assertIn("sweep (sync.py) pid %d" % proc.pid, grant.reason())
            self.assertGreaterEqual(grant.waited_s, 0.3)

    def test_sweep_waits_for_a_tender_run_then_gets_it(self):
        self.holder("tender", "tdr-x", seconds=0.8)
        start = time.monotonic()
        with sweeplock.hold("sweep", 20, path=self.path, poll=0.05) as grant:
            self.assertTrue(grant)
            self.assertGreater(time.monotonic() - start, 0.3)

    def test_dead_holder_never_leaves_a_stale_lock(self):
        proc = self.holder("tender", "tdr-dies")
        self.assertEqual(sweeplock.status(self.path)["state"], "held")
        proc.send_signal(signal.SIGKILL)
        proc.wait()
        info = sweeplock.status(self.path)
        self.assertEqual(info["state"], "free")
        self.assertIn("exited without clearing", info["note"])  # its record is left, as evidence
        with sweeplock.hold("sweep", 0, path=self.path) as grant:
            self.assertTrue(grant)
            self.assertEqual(grant.waited_s < 1, True)

    def test_a_broken_filesystem_is_an_error_not_contention(self):
        blocker = os.path.join(self.tmp, "afile")
        open(blocker, "w").close()
        with sweeplock.hold("tender", 0, path=os.path.join(blocker, "sweep.lock")) as grant:
            self.assertFalse(grant)
            self.assertFalse(grant.contended)
            self.assertIn("unusable", grant.reason())

    def test_git_children_do_not_inherit_the_lock(self):
        """A lingering child (a git daemon) must not keep the lock alive after its parent."""
        with sweeplock.hold("sweep", 0, path=self.path) as grant:
            self.assertTrue(grant)
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            self.procs.append(child)
        with sweeplock.hold("tender", 0, path=self.path) as grant:
            self.assertTrue(grant, "a child process kept the parent's flock")


class TestInheritance(LockTest):
    CHILD = textwrap.dedent("""
        import sys
        sys.path.insert(0, %(lib)r)
        import sweeplock
        with sweeplock.hold("sweep", 0.3, path=sys.argv[1], poll=0.05) as grant:
            print("%%s %%s" %% (bool(grant), grant.inherited))
    """) % {"lib": LIB}

    def run_child(self, env):
        out = subprocess.run([sys.executable, "-c", self.CHILD, self.path], env=env,
                             stdout=subprocess.PIPE, check=True).stdout.decode().strip()
        return out

    def test_tender_running_the_sweep_hands_its_lock_down(self):
        with sweeplock.hold("tender", 0, detail="restart-sweep", path=self.path) as grant:
            self.assertTrue(grant)
            self.assertEqual(self.run_child(sweeplock.child_env()), "True True")

    def test_a_stale_claim_in_the_environment_does_not_bypass_the_lock(self):
        proc = self.holder("tender", "someone-else")
        env = dict(os.environ)
        env[sweeplock.HELD_ENV] = str(os.getpid())  # alive, but not the holder
        self.assertEqual(self.run_child(env), "False False")
        proc.kill()
        proc.wait()
        env[sweeplock.HELD_ENV] = str(proc.pid)  # was the holder, now dead: lock is free
        self.assertEqual(self.run_child(env), "True False")


class TestConcurrency(LockTest):
    WORKER = textwrap.dedent("""
        import os, sys, time
        sys.path.insert(0, %(lib)r)
        import sweeplock
        role, path, log = sys.argv[1], sys.argv[2], sys.argv[3]
        with sweeplock.hold(role, 60, detail=role, path=path, poll=0.01) as grant:
            assert grant, grant.reason()
            start = time.time()
            with open(log, "a") as handle:
                handle.write("enter %%s %%d %%.6f\\n" %% (role, os.getpid(), start))
            time.sleep(0.05)
            with open(log, "a") as handle:
                handle.write("exit %%s %%d %%.6f\\n" %% (role, os.getpid(), time.time()))
    """) % {"lib": LIB}

    def test_mixed_sweeps_and_tenders_never_overlap(self):
        log = os.path.join(self.tmp, "log")
        roles = ["sweep", "tender"] * 6
        procs = [subprocess.Popen([sys.executable, "-c", self.WORKER, role, self.path, log])
                 for role in roles]
        self.procs.extend(procs)
        for proc in procs:
            self.assertEqual(proc.wait(timeout=120), 0)
        lines = open(log).read().split()
        events = [lines[i:i + 4] for i in range(0, len(lines), 4)]
        self.assertEqual(len(events), 2 * len(roles))
        # Strictly alternating enter/exit by the same pid: no two critical sections overlap.
        for i in range(0, len(events), 2):
            enter, leave = events[i], events[i + 1]
            self.assertEqual((enter[0], leave[0]), ("enter", "exit"), events)
            self.assertEqual(enter[2], leave[2], events)
        self.assertEqual(sorted(e[1] for e in events if e[0] == "enter"), sorted(roles))


class TestCli(LockTest):
    def cli(self, *args):
        env = dict(os.environ)
        env[sweeplock.PATH_ENV] = self.path
        return subprocess.run([sys.executable, os.path.join(LIB, "sweeplock.py")] + list(args),
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_status_and_run(self):
        out = self.cli("status", "--json")
        self.assertEqual(json.loads(out.stdout)["state"], "free")
        ran = self.cli("run", "--as", "tender", "--wait", "0", "--", sys.executable, "-c",
                       "import os; print(os.environ['%s'])" % sweeplock.HELD_ENV)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertTrue(ran.stdout.strip().isdigit())

    def test_run_refuses_with_tempfail_while_held(self):
        self.holder("sweep", "sync.py")
        ran = self.cli("run", "--as", "tender", "--wait", "0.2", "--", "true")
        self.assertEqual(ran.returncode, 75)
        self.assertIn(b"sweep lock held by sweep", ran.stderr)


# --- the sweep side, through sync.main ------------------------------------------------------

from tests.test_sync import SweepTest  # noqa: E402
import sync as sync_cmd  # noqa: E402


class TestSyncWiring(SweepTest):
    def setUp(self):
        super().setUp()
        self.lock = os.path.join(self.tmp, "dev", ".lodestar", "sweep.lock")
        patcher = mock.patch.dict(os.environ, {sweeplock.PATH_ENV: self.lock})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.procs = []
        self.addCleanup(lambda: [(p.kill(), p.wait(), p.stdout.close()) for p in self.procs])

    def tender_holds(self):
        proc = subprocess.Popen([sys.executable, "-c", HOLDER, "tender", "tdr-repair",
                                 self.lock, "60"], stdout=subprocess.PIPE)
        self.procs.append(proc)
        self.assertEqual(proc.stdout.readline().strip(), b"held")
        return proc

    def sweep_capturing(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.run_sweep(*argv)
        return code, out.getvalue()

    def test_sweep_skips_loudly_while_tender_holds_the_lock(self):
        self.make_repo("alpha")
        self.register_all()
        self.tender_holds()
        with mock.patch.object(sweeplock, "SWEEP_WAIT_S", 0.3):
            code, text = self.sweep_capturing("--quiet")
        self.assertEqual(code, 0)
        self.assertIn("sweep skipped: sweep lock held by tender (tdr-repair)", text)
        self.assertFalse(os.path.exists(paths.sync_status_file()), "the sweep touched repos")
        with open(sync_cmd.progress_file(), encoding="utf-8") as handle:
            progress = json.load(handle)
        self.assertTrue(progress["finished"])
        self.assertIn("tender", progress["skipped"])

    def test_sweep_holds_the_lock_while_it_runs(self):
        self.make_repo("alpha")
        self.register_all()
        seen = []
        real = sync_cmd._sweep

        def spy(args):
            seen.append(sweeplock.status(self.lock))
            return real(args)
        with mock.patch.object(sync_cmd, "_sweep", spy):
            self.assertEqual(self.run_sweep(), 0)
        self.assertEqual(seen[0]["state"], "held")
        self.assertEqual(seen[0]["holder"]["role"], "sweep")
        self.assertEqual(sweeplock.status(self.lock)["state"], "free")
        self.assertEqual(self.outcome("alpha"), sync_cmd.CLEAN)

    def test_dry_run_takes_no_lock_and_still_reports(self):
        self.make_repo("alpha")
        self.register_all()
        self.tender_holds()
        code, text = self.sweep_capturing("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("this dry run reports anyway", text)

    def test_a_broken_lock_never_silences_the_sweep(self):
        self.make_repo("alpha")
        self.register_all()
        blocker = os.path.join(self.tmp, "afile")
        open(blocker, "w").close()
        err = io.StringIO()
        with mock.patch.dict(os.environ, {sweeplock.PATH_ENV: os.path.join(blocker, "x.lock")}), \
                contextlib.redirect_stderr(err):
            self.assertEqual(self.run_sweep(), 0)
        self.assertIn("sweeping unlocked", err.getvalue())
        self.assertEqual(self.outcome("alpha"), sync_cmd.CLEAN)

    def test_a_sweep_started_by_tender_runs_under_tenders_lock(self):
        """Tender's restart-sweep runs sync.py as a child: it must not deadlock on its parent."""
        self.make_repo("alpha")
        self.register_all()
        with sweeplock.hold("tender", 0, detail="restart-sweep", path=self.lock) as grant:
            self.assertTrue(grant)
            with mock.patch.dict(os.environ, {sweeplock.HELD_ENV: str(os.getpid())}), \
                    mock.patch.object(sweeplock, "SWEEP_WAIT_S", 0.2):
                self.assertEqual(self.run_sweep(), 0)
        self.assertEqual(self.outcome("alpha"), sync_cmd.CLEAN)


if __name__ == "__main__":
    unittest.main()
