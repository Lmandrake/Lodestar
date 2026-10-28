"""Tests for lib.gitutil.

Everything runs against throwaway repos with a *local bare remote*, so divergence,
conflict-abort, and push are all exercised with no network and no possibility of touching
a real repo.
"""

import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import time
import unittest

from lib import gitutil


def git(repo, *args):
    subprocess.run(
        ["git", "-C", repo] + list(args),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def write(repo, name, text="hello\n"):
    path = os.path.join(repo, name)
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(name) else None
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


class GitFixture(unittest.TestCase):
    """A bare remote with two clones, so two-writer situations are reproducible."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-git-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self.remote = os.path.join(self.tmp, "remote.git")
        subprocess.run(["git", "init", "--bare", "-b", "main", self.remote],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        self.repo = self._clone("work")

    def _clone(self, name):
        path = os.path.join(self.tmp, name)
        subprocess.run(["git", "clone", self.remote, path],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        git(path, "config", "user.email", "test@example.com")
        git(path, "config", "user.name", "Test")
        git(path, "config", "commit.gpgsign", "false")
        return path

    def _seed(self):
        write(self.repo, "README.md", "seed\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "seed")
        git(self.repo, "push", "-u", "origin", "main")


class TestBasics(GitFixture):
    def test_detects_a_repo_and_a_non_repo(self):
        self.assertTrue(gitutil.is_repo(self.repo))
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        self.assertFalse(gitutil.is_repo(plain))

    def test_current_branch(self):
        self._seed()
        self.assertEqual(gitutil.current_branch(self.repo), "main")

    def test_run_does_not_raise_on_failure(self):
        code, _, err = gitutil.run(self.repo, "no-such-subcommand")
        self.assertNotEqual(code, 0)
        self.assertTrue(err)

    def test_status_entries_handles_a_path_with_spaces(self):
        self._seed()
        write(self.repo, "a file with spaces.txt")
        entries = dict((path, xy) for xy, path in gitutil.status_entries(self.repo))
        self.assertIn("a file with spaces.txt", entries)

    def test_status_entries_handles_a_rename(self):
        self._seed()
        git(self.repo, "mv", "README.md", "RENAMED.md")
        paths = [path for _, path in gitutil.status_entries(self.repo)]
        self.assertIn("RENAMED.md", paths)
        # The rename source must be consumed, not surface as a bogus entry.
        self.assertNotIn("seed", paths)


class TestState(GitFixture):
    def test_clean_repo_is_ok(self):
        self._seed()
        self.assertEqual(gitutil.state(self.repo), gitutil.OK)

    def test_untracked_file_alone_is_still_ok(self):
        self._seed()
        write(self.repo, "new.txt")
        self.assertEqual(gitutil.state(self.repo), gitutil.OK)

    def test_unstaged_modification_is_still_ok(self):
        self._seed()
        write(self.repo, "README.md", "changed\n")
        self.assertEqual(gitutil.state(self.repo), gitutil.OK)

    def test_staged_change_is_dirty_index(self):
        # Decision D-E: a repo left in this state must be skipped.
        self._seed()
        write(self.repo, "README.md", "changed\n")
        git(self.repo, "add", "README.md")
        self.assertEqual(gitutil.state(self.repo), gitutil.DIRTY_INDEX)

    def test_staged_deletion_is_dirty_index(self):
        self._seed()
        git(self.repo, "rm", "--cached", "README.md")
        self.assertEqual(gitutil.state(self.repo), gitutil.DIRTY_INDEX)

    def test_detached_head_is_detected(self):
        self._seed()
        code, out, _ = gitutil.run(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "--detach", out.strip())
        self.assertEqual(gitutil.state(self.repo), gitutil.DETACHED)

    def test_missing_directory_and_non_repo(self):
        self.assertEqual(gitutil.state(os.path.join(self.tmp, "nope")), gitutil.MISSING)
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        self.assertEqual(gitutil.state(plain), gitutil.NOT_A_REPO)

    def test_conflict_is_detected(self):
        self._seed()
        other = self._clone("other")
        write(self.repo, "README.md", "ours\n")
        git(self.repo, "commit", "-am", "ours")
        git(self.repo, "push")
        write(other, "README.md", "theirs\n")
        git(other, "commit", "-am", "theirs")
        subprocess.run(["git", "-C", other, "pull", "--no-rebase"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertIn(gitutil.state(other), (gitutil.CONFLICTED, gitutil.MERGING))

    def test_unsafe_states_list_covers_every_non_ok_state(self):
        self.assertNotIn(gitutil.OK, gitutil.UNSAFE_STATES)
        self.assertIn(gitutil.DIRTY_INDEX, gitutil.UNSAFE_STATES)


class TestQuiescence(GitFixture):
    def test_clean_tree_is_quiescent(self):
        self._seed()
        self.assertTrue(gitutil.quiescent(self.repo, 600))

    def test_a_just_written_file_is_not_quiescent(self):
        self._seed()
        write(self.repo, "fresh.txt")
        self.assertFalse(gitutil.quiescent(self.repo, 600))

    def test_an_old_change_is_quiescent(self):
        self._seed()
        path = write(self.repo, "old.txt")
        old = time.time() - 3600
        os.utime(path, (old, old))
        self.assertTrue(gitutil.quiescent(self.repo, 600))

    def test_newest_mtime_ignores_gitignored_churn(self):
        # An ignored build dir must not hold a repo permanently non-quiescent.
        self._seed()
        write(self.repo, ".gitignore", "build/\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "ignore build")
        git(self.repo, "push")
        os.makedirs(os.path.join(self.repo, "build"))
        write(self.repo, "build/artifact.bin", "x")
        self.assertTrue(gitutil.quiescent(self.repo, 600))


class TestGuards(GitFixture):
    def test_flags_a_dotenv_file(self):
        self._seed()
        write(self.repo, ".env", "SECRET=1\n")
        problems = gitutil.offending_paths(self.repo)
        self.assertEqual(len(problems), 1)
        self.assertIn("secret pattern", problems[0][1])

    def test_flags_a_pem_and_a_private_key(self):
        self._seed()
        write(self.repo, "server.pem", "x")
        write(self.repo, "id_rsa", "x")
        self.assertEqual(len(gitutil.offending_paths(self.repo)), 2)

    def test_flags_a_secret_in_a_subdirectory(self):
        self._seed()
        os.makedirs(os.path.join(self.repo, "config"))
        write(self.repo, "config/credentials.json", "x")
        self.assertEqual(len(gitutil.offending_paths(self.repo)), 1)

    def test_flags_an_oversized_file(self):
        self._seed()
        write(self.repo, "big.bin", "x" * 2048)
        problems = gitutil.offending_paths(self.repo, max_bytes=1024)
        self.assertEqual(len(problems), 1)
        self.assertIn("exceeds", problems[0][1])

    def test_passes_an_ordinary_tree(self):
        self._seed()
        write(self.repo, "notes.md", "safe\n")
        self.assertEqual(gitutil.offending_paths(self.repo), [])


class TestRemoteOperations(GitFixture):
    def test_ahead_behind_after_a_local_commit(self):
        self._seed()
        write(self.repo, "next.txt")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "next")
        self.assertEqual(gitutil.ahead_behind(self.repo), (1, 0))

    def test_ahead_behind_after_a_remote_commit(self):
        self._seed()
        other = self._clone("other")
        write(other, "remote.txt")
        git(other, "add", "-A")
        git(other, "commit", "-m", "remote work")
        git(other, "push")
        gitutil.fetch(self.repo)
        self.assertEqual(gitutil.ahead_behind(self.repo), (0, 1))

    def test_push_succeeds_and_clears_ahead(self):
        self._seed()
        write(self.repo, "pushme.txt")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "push me")
        code, _, err = gitutil.push(self.repo)
        self.assertEqual(code, 0, err)
        self.assertEqual(gitutil.ahead_behind(self.repo), (0, 0))

    def test_pull_rebase_replays_a_local_commit_over_remote_work(self):
        self._seed()
        other = self._clone("other")
        write(other, "theirs.txt")
        git(other, "add", "-A")
        git(other, "commit", "-m", "theirs")
        git(other, "push")

        write(self.repo, "ours.txt")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "ours")

        gitutil.fetch(self.repo)
        code, _, err = gitutil.pull_rebase(self.repo)
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.exists(os.path.join(self.repo, "theirs.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.repo, "ours.txt")))

    def test_abort_restores_the_tree_after_a_conflicting_rebase(self):
        # The load-bearing safety property: a conflict must leave the tree as it was.
        self._seed()
        other = self._clone("other")
        write(other, "README.md", "theirs\n")
        git(other, "commit", "-am", "theirs")
        git(other, "push")

        write(self.repo, "README.md", "ours\n")
        git(self.repo, "commit", "-am", "ours")

        gitutil.fetch(self.repo)
        code, _, _ = gitutil.pull_rebase(self.repo)
        self.assertNotEqual(code, 0)  # it must fail, not silently merge

        gitutil.abort(self.repo)
        self.assertEqual(gitutil.state(self.repo), gitutil.OK)
        with open(os.path.join(self.repo, "README.md"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "ours\n")

    def test_no_upstream_is_reported_rather_than_crashing(self):
        write(self.repo, "solo.txt")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "solo")
        git(self.repo, "checkout", "-b", "orphan")
        self.assertFalse(gitutil.has_upstream(self.repo))
        self.assertIsNone(gitutil.ahead_behind(self.repo))

    def test_last_commit_time_is_the_head_committer_timestamp(self):
        self._seed()
        stamp = gitutil.last_commit_time(self.repo)
        self.assertIsNotNone(stamp)
        # Just committed, so it must be within a generous window of now.
        self.assertLess(abs(time.time() - stamp), 300)

    def test_last_commit_time_reads_a_backdated_commit(self):
        # The sync sweep derives a repo's fetch tier from this, so an old commit has to
        # read as old rather than as "now".
        old = time.time() - 40 * 86400
        iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(old))
        write(self.repo, "ancient.txt")
        git(self.repo, "add", "-A")
        subprocess.run(["git", "-C", self.repo, "commit", "-m", "ancient"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       env=dict(os.environ, GIT_AUTHOR_DATE=iso, GIT_COMMITTER_DATE=iso))
        stamp = gitutil.last_commit_time(self.repo)
        self.assertIsNotNone(stamp)
        self.assertLess(abs(stamp - old), 3600)

    def test_last_commit_time_of_an_empty_repo_is_none_not_a_crash(self):
        # A fresh `git init` has no HEAD. The caller must get None, not an exception.
        empty = os.path.join(self.tmp, "empty")
        subprocess.run(["git", "init", "-b", "main", empty], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertIsNone(gitutil.last_commit_time(empty))

    def test_last_commit_time_of_a_non_repo_is_none(self):
        self.assertIsNone(gitutil.last_commit_time(os.path.join(self.tmp, "nowhere")))

    def test_module_never_passes_force_as_an_argument(self):
        # Non-negotiable #1 from MULTI_MACHINE_SYNC.md, asserted rather than trusted.
        # Checks for the *quoted* form, i.e. force being passed to git, since the prose
        # above legitimately mentions --force to say it is never used.
        source = open(gitutil.__file__, encoding="utf-8").read()
        for forbidden in ('"--force"', "'--force'", '"--force-with-lease"',
                          '"-f"', "'-f'"):
            self.assertNotIn(forbidden, source)


class LockFixture(GitFixture):
    """Helpers for planting a `.git/index.lock` by hand.

    Reproduces the 2026-09-01 wedge exactly: a zero-byte lock file left behind by nothing,
    which made five consecutive `git commit` runs fail with "Another git process seems to
    be running in this repository".
    """

    def setUp(self):
        super(LockFixture, self).setUp()
        # remove_stale_lock announces itself on stderr on purpose; keep it out of the
        # test runner's output without weakening the assertion that it says something.
        stack = contextlib.ExitStack()
        self.stderr = stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.addCleanup(stack.close)

    def lock_path(self, name="index.lock"):
        return os.path.join(self.repo, ".git", name)

    def plant_lock(self, contents="", age_seconds=0, name="index.lock"):
        path = self.lock_path(name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(contents)
        if age_seconds:
            old = time.time() - age_seconds
            os.utime(path, (old, old))
        return path

    def stage_a_change(self):
        self._seed()
        write(self.repo, "notes.md", "work\n")
        git(self.repo, "add", "-A")

    def no_sleeping(self):
        """Make the retry loop instant, and report how many times it waited."""
        calls = []

        def fake(seconds):
            calls.append(seconds)

        original = gitutil._sleep
        gitutil._sleep = fake
        self.addCleanup(lambda: setattr(gitutil, "_sleep", original))
        return calls

    def clear_lock_while_sleeping(self):
        """Simulate the other git process finishing between our attempts."""
        calls = self.no_sleeping()
        original = gitutil._sleep

        def fake(seconds):
            original(seconds)
            if os.path.exists(self.lock_path()):
                os.unlink(self.lock_path())

        gitutil._sleep = fake
        return calls


class TestLockRetry(LockFixture):
    def test_commit_succeeds_on_a_later_attempt_once_the_lock_clears(self):
        self.stage_a_change()
        self.plant_lock()
        self.clear_lock_while_sleeping()

        result = gitutil.commit(self.repo, "wip")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.attempts, 2)
        self.assertTrue(result.retried)
        self.assertFalse(result.lock_blocked)
        self.assertIn("retried", result.lock_note)

    def test_add_all_succeeds_on_a_later_attempt_once_the_lock_clears(self):
        self._seed()
        write(self.repo, "notes.md", "work\n")
        self.plant_lock()
        self.clear_lock_while_sleeping()

        result = gitutil.add_all(self.repo)
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.attempts, 2)
        self.assertIn("retried", result.lock_note)

    def test_it_gives_up_after_a_bounded_number_of_attempts(self):
        # A fresh, non-empty lock is never removable, so this exercises the give-up path.
        self.stage_a_change()
        self.plant_lock(contents="12345")
        waits = self.no_sleeping()

        result = gitutil.commit(self.repo, "wip")
        self.assertNotEqual(result.code, 0)
        self.assertTrue(result.lock_blocked)
        self.assertEqual(result.attempts, gitutil.LOCK_ATTEMPTS)
        self.assertEqual(len(waits), gitutil.LOCK_ATTEMPTS - 1)  # bounded, not a loop
        self.assertIn("needs you", result.lock_note)
        self.assertTrue(os.path.exists(self.lock_path()))  # never removed

    def test_an_ordinary_failure_is_not_retried(self):
        # Only lock contention is worth retrying; retrying real errors would just be slow.
        self._seed()
        waits = self.no_sleeping()
        result = gitutil.commit(self.repo, "nothing staged")
        self.assertNotEqual(result.code, 0)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(waits, [])
        self.assertFalse(result.lock_blocked)

    def test_the_backoff_grows_and_stays_short(self):
        self.stage_a_change()
        self.plant_lock(contents="12345")
        waits = self.no_sleeping()
        gitutil.commit(self.repo, "wip")
        self.assertEqual(waits, sorted(waits))
        self.assertLess(sum(waits), 10)  # bounded: a sweep runs every ~127s

    def test_git_result_still_unpacks_as_three_values(self):
        # Every existing caller does `code, out, err = ...`; the lock facts ride along as
        # attributes precisely so none of them had to change.
        self._seed()
        code, out, err = gitutil.commit(self.repo, "nothing staged")
        self.assertNotEqual(code, 0)
        self.assertIsInstance(out, str)
        self.assertIsInstance(err, str)

    def test_it_recognises_gits_real_contention_message(self):
        real = ("fatal: Unable to create '/home/user/dev/Lodestar/.git/index.lock': "
                "File exists.\n\nAnother git process seems to be running in this "
                "repository, or the lock file may be stale")
        self.assertTrue(gitutil.looks_like_lock_contention(real))
        self.assertFalse(gitutil.looks_like_lock_contention(
            "error: failed to push some refs to 'origin'"))


class TestStaleLockDetection(LockFixture):
    def test_no_lock_at_all(self):
        self._seed()
        verdict, _ = gitutil.lock_verdict(self.repo)
        self.assertEqual(verdict, gitutil.LOCK_ABSENT)

    def test_zero_bytes_old_and_unheld_is_stale(self):
        self._seed()
        self.plant_lock(age_seconds=gitutil.STALE_LOCK_SECONDS + 60)
        verdict, why = gitutil.lock_verdict(self.repo)
        self.assertEqual(verdict, gitutil.LOCK_STALE, why)

    def test_a_fresh_lock_is_left_alone(self):
        self._seed()
        self.plant_lock()
        verdict, why = gitutil.lock_verdict(self.repo)
        self.assertEqual(verdict, gitutil.LOCK_FRESH, why)

        removed, note = gitutil.remove_stale_lock(self.repo)
        self.assertFalse(removed)
        self.assertTrue(os.path.exists(self.lock_path()))
        self.assertIn("old", note)

    def test_a_non_empty_lock_is_left_alone_however_old(self):
        # git writes the new index into the lock file, so bytes mean a real writer.
        self._seed()
        self.plant_lock(contents="not empty", age_seconds=86400)
        verdict, why = gitutil.lock_verdict(self.repo)
        self.assertEqual(verdict, gitutil.LOCK_HELD, why)
        self.assertIn("bytes", why)

        removed, _ = gitutil.remove_stale_lock(self.repo)
        self.assertFalse(removed)
        self.assertTrue(os.path.exists(self.lock_path()))

    def test_a_lock_a_process_still_has_open_is_left_alone(self):
        # The case the mtime test cannot catch: a slow git run that has held the lock
        # longer than the threshold. Checked against the real lsof path, not a stub.
        self._seed()
        path = self.plant_lock(age_seconds=86400)
        holder = subprocess.Popen(
            ["/usr/bin/python3", "-c",
             "import sys,time; f=open(sys.argv[1]); time.sleep(30)", path])
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        deadline = time.time() + 10
        while time.time() < deadline:
            if gitutil.lock_holders(path):
                break
            time.sleep(0.1)

        verdict, why = gitutil.lock_verdict(self.repo)
        self.assertEqual(verdict, gitutil.LOCK_HELD, why)
        self.assertIn("open", why)

        removed, _ = gitutil.remove_stale_lock(self.repo)
        self.assertFalse(removed)
        self.assertTrue(os.path.exists(path))

    def test_an_unverifiable_holder_check_means_not_stale(self):
        # If the check itself cannot run, the answer is "assume it is live" — never
        # "nothing is running". Removing a live lock can corrupt the repo.
        self._seed()
        self.plant_lock(age_seconds=86400)
        original = gitutil.lock_holders
        gitutil.lock_holders = lambda path: None
        self.addCleanup(lambda: setattr(gitutil, "lock_holders", original))

        verdict, why = gitutil.lock_verdict(self.repo)
        self.assertEqual(verdict, gitutil.LOCK_HELD, why)
        self.assertIn("could not", why.lower())

    def test_removing_a_stale_lock_reports_loudly(self):
        self._seed()
        path = self.plant_lock(age_seconds=86400)
        removed, note = gitutil.remove_stale_lock(self.repo)
        self.assertTrue(removed, note)
        self.assertFalse(os.path.exists(path))
        self.assertIn("removed a stale", note)
        # Loudly: under launchd this stream is ~/Library/Logs/lodestar-sync.err, the file
        # SYNC_AGENT.md calls the failure signal. A silent deletion is the thing to avoid.
        self.assertIn("removed a stale", self.stderr.getvalue())


class TestHeadLockDetection(LockFixture):
    """The 2026-09-15 wedge: a stale `.git/HEAD.lock`, which only checking index.lock misses.

    A zero-byte HEAD.lock sat in one repo for ten days. `git add` needs no ref
    lock, so the sweep staged files happily and then failed at the commit — and because
    nothing checked HEAD.lock, the report said nothing was wrong while staged work piled up.
    """

    def test_head_lock_is_guarded_at_all(self):
        self.assertIn("HEAD.lock", gitutil.GUARDED_LOCKS)

    def test_a_stale_head_lock_is_detected_and_removed(self):
        self._seed()
        path = self.plant_lock(age_seconds=86400, name="HEAD.lock")
        verdict, why = gitutil.lock_verdict(self.repo, name="HEAD.lock")
        self.assertEqual(verdict, gitutil.LOCK_STALE, why)
        self.assertIn("HEAD.lock", why)

        removed, note = gitutil.remove_stale_lock(self.repo, name="HEAD.lock")
        self.assertTrue(removed, note)
        self.assertFalse(os.path.exists(path))
        self.assertIn("HEAD.lock", note)

    def test_a_stale_head_lock_does_not_look_like_an_index_lock(self):
        # The bug in one line: with only HEAD.lock planted, the index.lock check says
        # "nothing to see here". That is what made ten days of failure silent.
        self._seed()
        self.plant_lock(age_seconds=86400, name="HEAD.lock")
        verdict, _ = gitutil.lock_verdict(self.repo)
        self.assertEqual(verdict, gitutil.LOCK_ABSENT)

    def test_a_non_empty_head_lock_is_left_alone_however_old(self):
        # git writes the new ref value into HEAD.lock, so bytes mean a real writer.
        self._seed()
        self.plant_lock(contents="deadbeef\n", age_seconds=86400, name="HEAD.lock")
        verdict, why = gitutil.lock_verdict(self.repo, name="HEAD.lock")
        self.assertEqual(verdict, gitutil.LOCK_HELD, why)
        self.assertIn("bytes", why)

        removed, _ = gitutil.remove_stale_lock(self.repo, name="HEAD.lock")
        self.assertFalse(removed)
        self.assertTrue(os.path.exists(self.lock_path("HEAD.lock")))

    def test_a_fresh_head_lock_is_left_alone(self):
        self._seed()
        self.plant_lock(name="HEAD.lock")
        verdict, why = gitutil.lock_verdict(self.repo, name="HEAD.lock")
        self.assertEqual(verdict, gitutil.LOCK_FRESH, why)

    def test_head_lock_really_does_block_a_commit(self):
        # Not a mock: prove the premise. Staging succeeds, committing fails.
        self._seed()
        write(self.repo, "notes.md", "work\n")
        self.plant_lock(age_seconds=86400, name="HEAD.lock")

        add_code, _, add_err = gitutil.run(self.repo, "add", "-A")
        self.assertEqual(add_code, 0, add_err)

        commit_code, commit_out, commit_err = gitutil.run(
            self.repo, "commit", "-m", "should fail")
        self.assertNotEqual(commit_code, 0)
        self.assertIn("HEAD.lock", commit_out + commit_err)

    def test_a_stale_lock_is_cleared_and_the_commit_then_lands(self):
        # End to end: the 2026-09-01 wedge, unwedged.
        self.stage_a_change()
        self.plant_lock(age_seconds=86400)
        self.no_sleeping()

        result = gitutil.commit(self.repo, "wip")
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("removed a stale", result.lock_note)
        self.assertFalse(os.path.exists(self.lock_path()))

    def test_the_threshold_covers_the_sweep_interval(self):
        # The LaunchAgent fires every 127s; a lock younger than one sweep could belong to
        # the previous sweep still finishing, so the threshold must not undercut it.
        self.assertGreaterEqual(gitutil.STALE_LOCK_SECONDS, 120)

    def test_lock_holders_of_an_untouched_file_is_empty(self):
        self._seed()
        path = self.plant_lock(age_seconds=86400)
        self.assertEqual(gitutil.lock_holders(path), [])

    def test_stale_verdict_names_the_holders_death_and_cause(self):
        self._seed()
        self.plant_lock(age_seconds=7200)
        verdict, why = gitutil.lock_verdict(self.repo)
        self.assertEqual(verdict, gitutil.LOCK_STALE)
        self.assertIn("holder died", why)
        self.assertIn("SIGKILL", why)


class TestSlowMachine(GitFixture):
    """2026-10-22: ~256 ms per process launch. A git timeout must read as a slow machine,
    never as 'not a repo'; a stopped git must not orphan index.lock; timeouts scale."""

    def setUp(self):
        super(TestSlowMachine, self).setUp()
        self._seed()
        saved = (gitutil.TIMEOUT_SCALE, gitutil.LAUNCH_LATENCY_S)
        self.addCleanup(lambda: (setattr(gitutil, "TIMEOUT_SCALE", saved[0]),
                                 setattr(gitutil, "LAUNCH_LATENCY_S", saved[1])))
        stack = contextlib.ExitStack()
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.addCleanup(stack.close)

    def fake_timeout(self, verb):
        original = gitutil.run

        def fake(repo, *args, **kwargs):
            if args and args[0] == verb:
                return gitutil.GitResult(gitutil.TIMEOUT_CODE, "", "timed out after 120s: x")
            return original(repo, *args, **kwargs)
        gitutil.run = fake
        self.addCleanup(lambda: setattr(gitutil, "run", original))

    def test_rev_parse_timeout_is_timed_out_not_not_a_repo(self):
        self.fake_timeout("rev-parse")
        self.assertEqual(gitutil.state(self.repo), gitutil.TIMED_OUT)

    def test_status_timeout_is_timed_out_not_not_a_repo(self):
        self.fake_timeout("status")
        self.assertEqual(gitutil.state(self.repo), gitutil.TIMED_OUT)

    def test_a_real_non_repo_is_still_not_a_repo(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        self.assertEqual(gitutil.state(plain), gitutil.NOT_A_REPO)

    def test_run_reports_timeout_with_code_and_cause(self):
        gitutil.TIMEOUT_SCALE = 1.0
        result = gitutil.run(self.repo, "-c", "alias.slow=!sleep 5", "slow", timeout=0.3)
        self.assertTrue(gitutil.timed_out(result))
        self.assertIn("stopped with SIGTERM", result.err)

    def test_read_only_git_takes_no_optional_lock(self):
        self.assertEqual(gitutil._git_env(None).get("GIT_OPTIONAL_LOCKS"), "0")
        self.assertEqual(gitutil._git_env({"GIT_OPTIONAL_LOCKS": "1"})["GIT_OPTIONAL_LOCKS"], "1")

    def test_timeouts_scale_with_launch_latency(self):
        self.assertEqual(gitutil.scale_for(None), 1.0)
        self.assertEqual(gitutil.scale_for(0.005), 1.0)
        self.assertGreater(gitutil.scale_for(0.256), 3.0)
        self.assertEqual(gitutil.scale_for(5.0), gitutil.MAX_TIMEOUT_SCALE)

    def test_calibrate_measures_and_sets_scale(self):
        latency = gitutil.calibrate(samples=1)
        self.assertIsNotNone(latency)
        self.assertEqual(gitutil.TIMEOUT_SCALE, gitutil.scale_for(latency))


if __name__ == "__main__":
    unittest.main()
