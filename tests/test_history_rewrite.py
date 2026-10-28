"""A checkout whose upstream history was rewritten must halt, never rebase old history back on.

After the 2026-10-13 scrub of github.com/Lmandrake/Lodestar, a machine still holding the old
history sees thousands of local commits "ahead" of a rewritten upstream. Rebasing them would
replay the scrubbed text onto the new history and push it straight back. So the sweep (and the
interactive `selfsync.pull`) detect it and stop with a flag that names the reset command.

Two detections, because a rewrite may or may not change the root commit:

- **no common ancestor** with upstream at all;
- **upstream retires a commit this checkout still contains**: `sync/history-rewrite.md` on the
  upstream tip lists 40-hex shas of commits the rewrite replaced.
"""

import os
import subprocess
import unittest

import sync as sync_cmd
from lib import gitutil, registry, selfsync
from tests.test_sync import SweepTest, git


def out(repo, *args):
    return subprocess.run(["git", "-C", repo] + list(args), check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          universal_newlines=True).stdout.strip()


class _RewriteBase(SweepTest):
    def entry(self, name, path):
        return registry.Entry(name=name, path=path, branch="main", sync=True, notes="")

    def commit(self, repo, name, text="x\n"):
        self.write(repo, name, text)
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "add " + name)
        return out(repo, "rev-parse", "HEAD")

    def rewrite_upstream(self, name, keep_root=True, retire=None):
        """Replace the remote's history from a second clone; return that clone's path."""
        path, remote, _ = self.repos[name]
        other = os.path.join(self.tmp, name + "-rewriter")
        subprocess.run(["git", "clone", remote, other], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        git(other, "config", "user.email", "r@example.com")
        git(other, "config", "user.name", "R")
        root = out(other, "rev-list", "--max-parents=0", "HEAD")
        if keep_root:
            git(other, "reset", "--hard", root)
        else:
            git(other, "checkout", "--orphan", "fresh")
            git(other, "rm", "-rfq", ".")
        self.commit(other, "scrubbed.md", "clean\n")
        if retire:
            self.commit(other, os.path.join("sync", "history-rewrite.md"),
                        "retired:\n" + "".join("- %s\n" % sha for sha in retire)
                        + "command: python3 reset-me\n")
        git(other, "push", "--force", "origin", "HEAD:main")
        return other


class RewriteTest(_RewriteBase):
    def test_unrelated_history_is_flagged_not_rebased(self):
        path = self.make_repo("alpha")
        self.commit(path, "old.md")
        git(path, "push")
        self.commit(path, "local.md")
        before = out(path, "rev-parse", "HEAD")
        self.rewrite_upstream("alpha", keep_root=False)

        result = sync_cmd.sweep_one(self.entry("alpha", path), "testbox", 0, dry_run=False,
                                    fetch_state={})

        self.assertEqual(result.outcome, sync_cmd.FLAGGED)
        self.assertIn("rewritten", result.detail)
        self.assertEqual(out(path, "rev-parse", "HEAD"), before)
        self.assertFalse(gitutil.rebase_in_progress(path))

    def test_a_retired_commit_in_local_history_is_flagged_and_names_the_command(self):
        path = self.make_repo("alpha")
        retired = self.commit(path, "old.md", "secret\n")
        git(path, "push")
        self.commit(path, "local.md")
        before = out(path, "rev-parse", "HEAD")
        self.rewrite_upstream("alpha", keep_root=True, retire=[retired])

        result = sync_cmd.sweep_one(self.entry("alpha", path), "testbox", 0, dry_run=False,
                                    fetch_state={})

        self.assertEqual(result.outcome, sync_cmd.FLAGGED)
        self.assertIn("rewritten", result.detail)
        self.assertIn("python3 reset-me", result.detail)
        self.assertEqual(out(path, "rev-parse", "HEAD"), before)
        self.assertFalse(os.path.exists(os.path.join(path, "scrubbed.md")))

    def test_a_stale_checkout_behind_and_ahead_is_flagged_even_with_no_new_local_work(self):
        # The Desktop case: its HEAD predates the old tip, but contains a retired commit.
        path = self.make_repo("alpha")
        retired = self.commit(path, "old.md", "secret\n")
        git(path, "push")
        self.rewrite_upstream("alpha", keep_root=True, retire=[retired])

        result = sync_cmd.sweep_one(self.entry("alpha", path), "testbox", 0, dry_run=False,
                                    fetch_state={})

        self.assertEqual(result.outcome, sync_cmd.FLAGGED)
        self.assertIn("rewritten", result.detail)

    def test_an_ordinary_divergence_still_rebases(self):
        path = self.make_repo("alpha")
        self.push_from_elsewhere("alpha")
        self.commit(path, "local.md")

        result = sync_cmd.sweep_one(self.entry("alpha", path), "testbox", 0, dry_run=False,
                                    fetch_state={})

        self.assertNotIn("rewritten", result.detail)
        self.assertTrue(os.path.exists(os.path.join(path, "elsewhere.md")))

    def test_a_marker_naming_commits_we_do_not_have_is_harmless(self):
        path = self.make_repo("alpha")
        other = self.push_from_elsewhere("alpha")
        self.commit(other, os.path.join("sync", "history-rewrite.md"),
                    "retired:\n- %s\n" % ("ab" * 20))
        git(other, "push")
        self.commit(path, "local.md")

        gitutil.fetch(path)
        self.assertIsNone(gitutil.upstream_rewrite(path))

    def test_selfsync_pull_refuses_a_rewritten_upstream(self):
        path = self.make_repo("alpha")
        self.commit(path, "old.md")
        git(path, "push")
        self.commit(path, "local.md")
        before = out(path, "rev-parse", "HEAD")
        self.rewrite_upstream("alpha", keep_root=False)

        ok, note = selfsync.pull(path)

        self.assertFalse(ok)
        self.assertIn("rewritten", note)
        self.assertEqual(out(path, "rev-parse", "HEAD"), before)


class RecoverTest(_RewriteBase):
    """bin/rewrite_recover.py: in place, nothing lost, no old object left to push."""

    def test_recovery_moves_onto_the_new_history_and_keeps_local_state(self):
        import rewrite_recover
        path = self.make_repo("alpha")
        self.commit(path, "keep.md", "one\n")
        self.commit(path, os.path.join("conversations", "c.md"), "dialog\n")
        retired = self.commit(path, "old.md", "secret\n")
        git(path, "push")
        git(path, "branch", "side")
        git(path, "config", "--local", "lodestar.id", "lid-123")
        # the rewritten upstream: keep.md survives, conversations/ is gone and ignored
        other = self.rewrite_upstream("alpha", keep_root=True, retire=[retired])
        self.commit(other, "keep.md", "one\n")
        self.commit(other, ".gitignore", "conversations/\n")
        git(other, "push")
        self.write(path, "keep.md", "one\nlocal edit\n")
        self.write(path, os.path.join("journal", "new.json"), "{}\n")  # untracked

        self.assertEqual(rewrite_recover.main([path]), 0)

        self.assertEqual(out(path, "rev-parse", "HEAD"), out(path, "rev-parse", "origin/main"))
        self.assertIsNone(gitutil.upstream_rewrite(path))
        with open(os.path.join(path, "keep.md")) as fh:
            self.assertEqual(fh.read(), "one\nlocal edit\n")
        self.assertTrue(os.path.exists(os.path.join(path, "conversations", "c.md")))
        self.assertTrue(os.path.exists(os.path.join(path, "journal", "new.json")))
        self.assertFalse(os.path.exists(os.path.join(path, "old.md")))
        self.assertEqual(out(path, "for-each-ref", "--format=%(refname)", "refs/heads"),
                         "refs/heads/main")
        self.assertEqual(out(path, "config", "--local", "lodestar.id"), "lid-123")
        missing = subprocess.run(["git", "-C", path, "cat-file", "-e", retired],
                                 stderr=subprocess.DEVNULL)
        self.assertNotEqual(missing.returncode, 0)
        carry = os.path.join(self.tmp, "dev", ".lodestar", "rewrite-carryover")
        stamp = os.listdir(carry)[0]
        self.assertTrue(os.path.getsize(os.path.join(carry, stamp, "old-history.bundle")))
        self.assertFalse(os.path.exists(os.path.join(path, ".git", "lodestar-pause")))

    def test_recovery_refuses_when_nothing_was_rewritten(self):
        import rewrite_recover
        path = self.make_repo("alpha")
        before = out(path, "rev-parse", "HEAD")
        self.assertEqual(rewrite_recover.main([path]), 0)
        self.assertEqual(out(path, "rev-parse", "HEAD"), before)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "dev", ".lodestar",
                                                     "rewrite-carryover")))


class RestoreTest(_RewriteBase):
    """The RESTORE.txt walkthrough, executed as written, proves the original state comes back."""

    def test_restore_txt_commands_reproduce_the_original_state(self):
        import rewrite_recover
        path = self.make_repo("alpha")
        self.commit(path, "keep.md", "one\n")
        retired = self.commit(path, "old.md", "secret\n")
        git(path, "push")
        git(path, "branch", "side")
        original_main = out(path, "rev-parse", "main")
        original_side = out(path, "rev-parse", "side")
        self.write(path, "keep.md", "one\nlocal edit\n")        # uncommitted, tracked
        other = self.rewrite_upstream("alpha", keep_root=True, retire=[retired])
        self.commit(other, "keep.md", "one\n")
        git(other, "push")

        self.assertEqual(rewrite_recover.main([path]), 0)
        # recovery really pruned: the old commit is gone from the repo itself
        self.assertNotEqual(subprocess.run(["git", "-C", path, "cat-file", "-e", original_main],
                                           stderr=subprocess.DEVNULL).returncode, 0)

        carry_root = os.path.join(self.tmp, "dev", ".lodestar", "rewrite-carryover")
        carry = os.path.join(carry_root, os.listdir(carry_root)[0])
        with open(os.path.join(carry, "RESTORE.txt")) as fh:
            cmds = [l[2:].strip() for l in fh if l.startswith("$ ")]
        self.assertEqual(len(cmds), 3)
        for cmd in cmds:
            subprocess.run(cmd, shell=True, check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)

        self.assertEqual(out(path, "rev-parse", "refs/old/heads/main"), original_main)
        self.assertEqual(out(path, "rev-parse", "refs/old/heads/side"), original_side)
        restored = os.path.join(carry, "restored")
        self.assertEqual(out(restored, "rev-parse", "HEAD"), original_main)
        with open(os.path.join(restored, "keep.md")) as fh:
            self.assertEqual(fh.read(), "one\nlocal edit\n")
        self.assertTrue(os.path.exists(os.path.join(restored, "old.md")))


if __name__ == "__main__":
    unittest.main()
