"""Generated claude/snapshot files must not sit dirty and block the pull (2026-10-19)."""

import os

from lib import gitutil, selfsync
from tests.test_history_rewrite import _RewriteBase, out


class GeneratedSnapshotTest(_RewriteBase):
    def dirty(self, path):
        self.write(path, "claude/snapshot/k1/statusline.sh", "#!/bin/sh\necho hi\n")
        self.write(path, "claude/snapshot/k1/CLAUDE.md", "provenance\n")
        self.write(path, "claude/snapshot/k1/memory/x/MEMORY.md", "mem\n")
        self.write(path, "unrelated.txt", "mine\n")

    def test_commits_only_the_named_generated_paths(self):
        path = self.make_repo("alpha")
        self.dirty(path)
        done = gitutil.commit_generated_snapshots(path)
        self.assertEqual(sorted(done), ["claude/snapshot/k1/CLAUDE.md",
                                        "claude/snapshot/k1/statusline.sh"])
        left = sorted(gitutil.changed_paths(path))
        self.assertIn("unrelated.txt", left)
        self.assertTrue(any(p.startswith("claude/snapshot/k1/memory") for p in left))
        self.assertNotIn("claude/snapshot/k1/statusline.sh", left)

    def test_nothing_to_do_is_quiet(self):
        path = self.make_repo("alpha")
        self.assertEqual(gitutil.commit_generated_snapshots(path), [])

    def test_pull_succeeds_over_a_dirty_generated_snapshot(self):
        path = self.make_repo("alpha")
        other = self.push_from_elsewhere("alpha")
        self.write(other, "claude/snapshot/k1/statusline.rendered.sh", "upstream\n")
        gitutil.run(other, "add", "-A")
        gitutil.run(other, "commit", "-m", "up")
        gitutil.run(other, "push")
        self.write(path, "claude/snapshot/k1/statusline.sh", "local\n")
        ok, note = selfsync.pull(path)
        self.assertTrue(ok, note)
        self.assertTrue(os.path.exists(os.path.join(path, "elsewhere.md")))
