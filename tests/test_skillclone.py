"""Tests for lib/skillclone.py — cloning the repos a declared skill needs.

Cloning is the one step in the sweep that touches the network and writes new trees into
~/dev, unattended, where launchd cannot answer a prompt. So the tests here care less about
the happy path than about the three ways it could misbehave: prompting (hangs the sweep),
retrying without backoff (storms the remote), and cloning something nobody opted in to.

A real local git repo is used as the clone source, so success is genuinely exercised without
touching the network.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

from lib import skillclone


class Entry(object):
    """Stand-in for registry.Entry — only .name and .path are read."""

    def __init__(self, name, path):
        self.name = name
        self.path = path


class CloneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="lodestar-clone-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.attempts = os.path.join(self.tmp, "clone-attempts.json")

    def make_source_repo(self, name="Source"):
        """A real git repo with one commit, usable as a clone source over a path."""
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        env = dict(os.environ)
        env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"})
        for args in (["init", "-b", "main"], ["add", "-A"],
                     ["commit", "-m", "seed", "--allow-empty"]):
            subprocess.run(["git", "-C", path] + args, env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return path


class TestRemotesMap(CloneTest):
    def test_render_and_read_round_trip(self):
        path = os.path.join(self.tmp, "remotes.md")
        mapping = {"Alpha": "git@github.com:Someone/Alpha.git", "Beta": "ssh://x/Beta.git"}
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(skillclone.render_remotes(mapping))
        self.assertEqual(skillclone.read_remotes(path), mapping)

    def test_a_missing_map_is_empty_not_an_error(self):
        self.assertEqual(skillclone.read_remotes(os.path.join(self.tmp, "nope.md")), {})

    def test_url_for_prefers_a_recorded_url_and_says_so(self):
        url, source = skillclone.url_for("Alpha", {"Alpha": "ssh://recorded/Alpha.git"})
        self.assertEqual(url, "ssh://recorded/Alpha.git")
        self.assertEqual(source, "recorded")

    def test_url_for_falls_back_to_the_pattern_and_labels_it_guessed(self):
        url, source = skillclone.url_for("Alpha", {})
        self.assertIn("Alpha", url)
        self.assertEqual(source, "guessed")

    def test_observe_remote_reads_a_real_origin(self):
        source = self.make_source_repo()
        clone_path = os.path.join(self.tmp, "clone")
        ok, detail = skillclone.clone(source, clone_path)
        self.assertTrue(ok, detail)
        self.assertEqual(skillclone.observe_remote(clone_path), source)

    def test_observe_remote_on_a_non_repo_is_none_not_a_crash(self):
        self.assertIsNone(skillclone.observe_remote(os.path.join(self.tmp, "nope")))

    def test_update_remotes_ignores_repos_absent_here(self):
        """The machine that HAS a repo describes it; an absent one must not blank it out."""
        path = os.path.join(self.tmp, "remotes.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(skillclone.render_remotes({"Ghost": "ssh://kept/Ghost.git"}))
        entries = [Entry("Ghost", os.path.join(self.tmp, "not-here"))]
        mapping, changed = skillclone.update_remotes(entries, path)
        self.assertEqual(mapping["Ghost"], "ssh://kept/Ghost.git")
        self.assertFalse(changed)


class TestBackoff(CloneTest):
    def test_never_attempted_may_attempt(self):
        self.assertTrue(skillclone.may_attempt("Alpha", {}, now=1000.0))

    def test_a_recent_attempt_backs_off(self):
        self.assertFalse(skillclone.may_attempt("Alpha", {"Alpha": 1000.0}, now=1060.0))

    def test_an_old_attempt_may_retry(self):
        self.assertTrue(skillclone.may_attempt("Alpha", {"Alpha": 1000.0}, now=1000.0 + 3601))

    def test_unreadable_state_means_never_attempted_not_a_crash(self):
        with open(self.attempts, "w", encoding="utf-8") as handle:
            handle.write("{ not json")
        self.assertEqual(skillclone.load_attempts(self.attempts), {})

    def test_state_round_trips(self):
        skillclone.save_attempts({"Alpha": 1234.0}, self.attempts)
        self.assertEqual(skillclone.load_attempts(self.attempts), {"Alpha": 1234.0})


class TestClone(CloneTest):
    def test_a_real_clone_succeeds(self):
        source = self.make_source_repo()
        destination = os.path.join(self.tmp, "dest")
        ok, detail = skillclone.clone(source, destination)
        self.assertTrue(ok, detail)
        self.assertTrue(os.path.isdir(os.path.join(destination, ".git")))

    def test_an_existing_destination_is_refused_rather_than_merged_into(self):
        os.makedirs(os.path.join(self.tmp, "occupied"))
        ok, detail = skillclone.clone("ssh://x/y.git", os.path.join(self.tmp, "occupied"))
        self.assertFalse(ok)
        self.assertIn("already exists", detail)

    def test_a_bad_remote_fails_fast_instead_of_prompting(self):
        """The hang this guards against: launchd cannot answer a credential prompt."""
        ok, detail = skillclone.clone(os.path.join(self.tmp, "no-such-repo"),
                                      os.path.join(self.tmp, "dest"), timeout=30)
        self.assertFalse(ok)
        self.assertIn("failed", detail)

    def test_a_failed_clone_leaves_nothing_behind_to_block_the_retry(self):
        """The self-deadlock this prevents.

        A failed or killed clone leaves a partial directory. The exists-check would then
        refuse every future attempt with "already exists", so the repo could never arrive no
        matter how much backoff elapsed — and large repos, the likeliest to time out, are
        exactly the ones that would get stuck.
        """
        destination = os.path.join(self.tmp, "dest")
        skillclone.clone(os.path.join(self.tmp, "no-such-repo"), destination, timeout=30)
        self.assertFalse(os.path.exists(destination))

    def test_a_retry_after_a_failure_can_succeed(self):
        """End-to-end proof of the above: fail, then succeed to the same destination."""
        destination = os.path.join(self.tmp, "dest")
        ok, _ = skillclone.clone(os.path.join(self.tmp, "no-such-repo"), destination,
                                 timeout=30)
        self.assertFalse(ok)
        source = self.make_source_repo("Upstream")
        ok, detail = skillclone.clone(source, destination)
        self.assertTrue(ok, detail)
        self.assertTrue(os.path.isdir(os.path.join(destination, ".git")))

    def test_the_timeout_is_generous_enough_for_the_largest_real_repo(self):
        """A large repo can carry ~1 GB of .git. A limit it can never beat is not a safety limit."""
        self.assertGreaterEqual(skillclone.CLONE_TIMEOUT_SECONDS, 600)


class TestCloneMissing(CloneTest):
    def test_an_unregistered_repo_is_refused_and_said_so(self):
        results = skillclone.clone_missing(["Ghost"], [], {}, attempts_path=self.attempts)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertIn("registry", results[0].detail)

    def test_an_already_present_repo_is_silently_skipped(self):
        present = os.path.join(self.tmp, "Present")
        os.makedirs(present)
        results = skillclone.clone_missing(["Present"], [Entry("Present", present)], {},
                                           attempts_path=self.attempts)
        self.assertEqual(results, [])

    def test_a_successful_clone_reports_and_lands(self):
        source = self.make_source_repo("Upstream")
        destination = os.path.join(self.tmp, "Alpha")
        results = skillclone.clone_missing(
            ["Alpha"], [Entry("Alpha", destination)], {"Alpha": source},
            attempts_path=self.attempts)
        self.assertTrue(results[0].ok, results[0].detail)
        self.assertTrue(os.path.isdir(os.path.join(destination, ".git")))
        self.assertIn("recorded", results[0].detail)

    def test_a_success_clears_the_backoff(self):
        source = self.make_source_repo("Upstream")
        destination = os.path.join(self.tmp, "Alpha")
        skillclone.clone_missing(["Alpha"], [Entry("Alpha", destination)],
                                 {"Alpha": source}, attempts_path=self.attempts)
        self.assertNotIn("Alpha", skillclone.load_attempts(self.attempts))

    def test_the_attempt_is_recorded_before_the_clone_runs(self):
        """A clone killed mid-flight must still be throttled on the next sweep.

        Recording only on completion would leave a crash-looping clone unthrottled, which is
        the request storm this exists to prevent.
        """
        destination = os.path.join(self.tmp, "Alpha")
        skillclone.clone_missing(["Alpha"], [Entry("Alpha", destination)],
                                 {"Alpha": os.path.join(self.tmp, "no-such-repo")},
                                 now=5000.0, attempts_path=self.attempts)
        with open(self.attempts, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["Alpha"], 5000.0)

    def test_a_recent_failure_backs_off_instead_of_retrying(self):
        skillclone.save_attempts({"Alpha": 5000.0}, self.attempts)
        results = skillclone.clone_missing(
            ["Alpha"], [Entry("Alpha", os.path.join(self.tmp, "Alpha"))], {},
            now=5060.0, attempts_path=self.attempts)
        self.assertFalse(results[0].ok)
        self.assertIn("backing off", results[0].detail)

    def test_only_one_clone_is_started_per_sweep(self):
        entries = [Entry("A", os.path.join(self.tmp, "A")),
                   Entry("B", os.path.join(self.tmp, "B"))]
        results = skillclone.clone_missing(["A", "B"], entries, {}, dry_run=True,
                                           now=1.0, attempts_path=self.attempts)
        deferred = [r for r in results if "deferred" in r.detail]
        self.assertEqual(len(deferred), 1)

    def test_dry_run_writes_nothing_at_all(self):
        destination = os.path.join(self.tmp, "Alpha")
        source = self.make_source_repo("Upstream")
        results = skillclone.clone_missing(
            ["Alpha"], [Entry("Alpha", destination)], {"Alpha": source},
            dry_run=True, attempts_path=self.attempts)
        self.assertIn("would clone", results[0].detail)
        self.assertFalse(os.path.exists(destination))
        self.assertFalse(os.path.exists(self.attempts))


if __name__ == "__main__":
    unittest.main()


class CensusFormTest(unittest.TestCase):
    def test_private_ssh_remote_is_written_https(self):
        self.assertEqual("https://git.private.example/mandrake/Mail.git",
                         skillclone.census_form("git@git.private.example:mandrake/Mail.git"))

    def test_other_hosts_keep_their_form(self):
        url = "git@github.com:Lmandrake/Bridge.git"
        self.assertEqual(url, skillclone.census_form(url))
        self.assertEqual("", skillclone.census_form(""))
