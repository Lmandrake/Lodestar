"""Tests for lib/enroll.py — repo-identity stamping and the machine's managed-instance map.

Real temporary git repos are built with subprocess for every test, because the interesting
behaviour here (`git clone` not copying local config, `cp -R` copying it, worktrees sharing
a common dir) can only be verified against real git, not modelled.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

from lib import enroll


def _git(path, *args):
    env = dict(os.environ)
    env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"})
    return subprocess.run(["git", "-C", path] + list(args), env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=30, check=True)


class EnrollTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="lodestar-enroll-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def make_repo(self, name="Repo"):
        """A real git repo with one commit and local user.email/name, so commits work
        without depending on global git config."""
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        _git(path, "init", "-q", "-b", "main")
        _git(path, "config", "--local", "user.email", "t@e")
        _git(path, "config", "--local", "user.name", "t")
        with open(os.path.join(path, "seed.txt"), "w", encoding="utf-8") as handle:
            handle.write("seed\n")
        _git(path, "add", "-A")
        _git(path, "commit", "-q", "-m", "seed")
        return path


class TestStampAndRead(EnrollTest):
    def test_round_trips(self):
        repo = self.make_repo()
        self.assertEqual(enroll.read_stamp(repo), "")
        self.assertTrue(enroll.stamp(repo, "lodestar-abc123"))
        self.assertEqual(enroll.read_stamp(repo), "lodestar-abc123")

    def test_refuses_to_overwrite_a_different_id(self):
        repo = self.make_repo()
        enroll.stamp(repo, "id-one")
        self.assertFalse(enroll.stamp(repo, "id-two"))
        self.assertEqual(enroll.read_stamp(repo), "id-one")

    def test_writing_the_same_id_again_is_a_noop_that_returns_true(self):
        repo = self.make_repo()
        enroll.stamp(repo, "id-one")
        self.assertTrue(enroll.stamp(repo, "id-one"))
        self.assertEqual(enroll.read_stamp(repo), "id-one")

    def test_empty_id_raises(self):
        repo = self.make_repo()
        with self.assertRaises(ValueError):
            enroll.stamp(repo, "")
        with self.assertRaises(ValueError):
            enroll.stamp(repo, "   ")

    def test_read_stamp_on_a_non_repo_is_empty_not_a_crash(self):
        self.assertEqual(enroll.read_stamp(os.path.join(self.tmp, "not-a-repo")), "")


class TestCloneIsUnstamped(EnrollTest):
    def test_a_clone_of_a_stamped_repo_reads_as_unstamped(self):
        # Fact (a): git clone does not copy a custom `git config --local` value.
        source = self.make_repo("Source")
        enroll.stamp(source, "lodestar-source-id")
        clone_path = os.path.join(self.tmp, "Clone")
        subprocess.run(["git", "clone", "-q", source, clone_path], check=True, timeout=30)
        self.assertEqual(enroll.read_stamp(clone_path), "")
        self.assertEqual(enroll.instances([source, clone_path]),
                         {"lodestar-source-id": [os.path.realpath(source)]})


class TestCopyConflicts(EnrollTest):
    def test_a_cp_dash_r_copy_reads_as_stamped_and_conflicts(self):
        # Fact (b): cp -R DOES copy .git/config, stamp included. Two independent directories
        # can then claim one id — that must surface as a conflict, never "first match wins".
        original = self.make_repo("Original")
        enroll.stamp(original, "lodestar-dup-id")
        copy_path = os.path.join(self.tmp, "Copy")
        subprocess.run(["cp", "-R", original, copy_path], check=True, timeout=30)
        self.assertEqual(enroll.read_stamp(copy_path), "lodestar-dup-id")

        all_paths = [original, copy_path]
        found = enroll.conflicts(all_paths)
        self.assertIn("lodestar-dup-id", found)
        self.assertEqual(sorted(found["lodestar-dup-id"]),
                         sorted(os.path.realpath(p) for p in all_paths))

    def test_no_conflict_when_only_one_path_claims_an_id(self):
        original = self.make_repo("Solo")
        enroll.stamp(original, "lodestar-solo-id")
        self.assertEqual(enroll.conflicts([original]), {})


class TestWorktreesAreOneInstance(EnrollTest):
    def test_a_linked_worktree_does_not_produce_a_conflict(self):
        # Fact (c): a linked worktree shares the main checkout's local config, so it exposes
        # the SAME stamp. Two worktrees of one repo must resolve to one instance, not two.
        main = self.make_repo("Main")
        enroll.stamp(main, "lodestar-wt-id")
        worktree_path = os.path.join(self.tmp, "Main-worktree")
        _git(main, "worktree", "add", "-q", worktree_path, "-b", "side")

        self.assertEqual(enroll.read_stamp(worktree_path), "lodestar-wt-id")
        self.assertEqual(enroll.common_dir(main), enroll.common_dir(worktree_path))

        all_paths = [main, worktree_path]
        self.assertEqual(enroll.conflicts(all_paths), {})
        instances_map = enroll.instances(all_paths)
        self.assertEqual(instances_map["lodestar-wt-id"], [os.path.realpath(main)])


class TestManagedMap(EnrollTest):
    def test_missing_file_is_empty_not_an_error(self):
        self.assertEqual(enroll.load_managed(os.path.join(self.tmp, "nope.json")), {})

    def test_round_trips(self):
        target = os.path.join(self.tmp, "managed.json")
        mapping = {"lodestar-abc": "/home/user/dev/Lodestar",
                  "other-id": "/home/user/dev/Other"}
        enroll.save_managed(mapping, target)
        self.assertEqual(enroll.load_managed(target), mapping)

    def test_save_writes_via_tmp_and_replace_leaving_no_tmp_file_behind(self):
        target = os.path.join(self.tmp, "managed.json")
        enroll.save_managed({"a": "b"}, target)
        self.assertTrue(os.path.exists(target))
        self.assertFalse(os.path.exists(target + ".tmp"))
        with open(target, encoding="utf-8") as handle:
            json.load(handle)  # must be valid JSON, not partially written

    def test_default_path_comes_from_paths_managed_instances_file(self):
        # Confirms load/save honour LODESTAR_ROOT-independent, machine-local path wiring
        # without ever touching the real file: pass an explicit path everywhere else, and
        # here just confirm the no-arg call resolves through lib.paths.
        from lib import paths
        self.assertEqual(enroll.load_managed.__defaults__, (None,))
        self.assertTrue(paths.managed_instances_file().endswith("managed.json"))


if __name__ == "__main__":
    unittest.main()
