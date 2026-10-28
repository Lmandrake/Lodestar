"""Tests for lib.gitutil's source/sweepable partition — what the sweep may auto-commit.

Measured 2026-09-15: 304 `chore(sync)` commits exist and 8 of them swallowed source changes.
Those 8 are the whole problem, and the harm is not untidy history. `lib/reversal.py` records it:
the sweep commits "by a different actor, after the receipt was written, under a sha the receipt
never saw, possibly bundled with unrelated drift", and reverting such a sha reverts someone
else's work. So a bundled auto-commit breaks the Phase 4 reversal protocol.

The fix is narrow on purpose. 296 of the 304 are board state — journal events, heartbeats,
loop files, generated artifacts — and those must keep committing every 127s, because that is
the whole promise of the sweep. Only source stops being auto-committed.

Two cases below are load-bearing and easy to break: `loops/<id>.md` is board state despite
being Markdown, and `BOARD.md` is generated. If either is ever classified as source, the board
stops syncing across machines and the sweep's reason for existing is gone.
"""

import unittest

from lib import gitutil


class IsSourceTest(unittest.TestCase):
    def assertSource(self, path):
        self.assertTrue(gitutil.is_source(path), "%s should need an authored commit" % path)

    def assertSweepable(self, path):
        self.assertFalse(gitutil.is_source(path), "%s should keep auto-committing" % path)

    # -- the 8 real offenders, so the fix is pinned to the measurement ----------------

    def test_the_files_the_sweep_actually_swallowed_are_source(self):
        for path in (
            "bin/away.py",
            "bin/lib/away.py",
            "tests/test_away.py",
            "bin/lib/gitutil.py",
            "bin/sync.py",
            "bin/lib/distrust.py",
            "bin/zap_sheet.py",
            "bin/graph_token_watch.py",
            "docs/ROADMAP.md",
            "docs/superpowers/plans/2026-09-11-judge-and-zap-sync.md",
        ):
            self.assertSource(path)

    # -- board state: must keep flowing ----------------------------------------------

    def test_a_loop_file_is_board_state_even_though_it_is_markdown(self):
        """If this flips, the board stops syncing between machines."""
        self.assertSweepable("loops/zap-surface.md")

    def test_the_generated_board_is_board_state(self):
        self.assertSweepable("BOARD.md")

    def test_journal_events_and_heartbeats_are_board_state(self):
        self.assertSweepable("journal/2026-09-15/1700000000-aaaaaa.json")
        self.assertSweepable("journal/2026-09-15/mt-002-11.heartbeat")

    def test_airlock_artifacts_are_board_state(self):
        self.assertSweepable("airlock/review/zap.html")
        self.assertSweepable("airlock/review/zap-decisions.json")
        self.assertSweepable("airlock/captures/2026-09-15/mail-triage--desktop--044952.md")

    def test_the_claude_snapshot_is_board_state(self):
        self.assertSweepable("claude/snapshot/mt-002/settings.json")

    def test_machine_local_sync_health_is_board_state(self):
        self.assertSweepable("SYNC.md")

    # -- code, wherever it lives ------------------------------------------------------

    def test_code_extensions_are_source_anywhere_in_the_tree(self):
        for path in ("tools/hooks/validate-question-card.py", "scripts/deploy.sh",
                     "src/app.ts", "src/app.js", "main.go", "lib.rs"):
            self.assertSource(path)

    def test_a_shell_script_inside_a_swept_directory_is_still_source(self):
        """Extension wins over location: a script is a script."""
        self.assertSource("airlock/helper.sh")

    # -- authored root docs ----------------------------------------------------------

    def test_the_named_root_docs_are_source(self):
        for path in ("README.md", "USING.md", "CLAUDE.md", "AGENTS.md"):
            self.assertSource(path)

    def test_an_unremarkable_markdown_file_is_not_source(self):
        """Only NAMED root docs and docs/ are authored prose; the rest sweeps."""
        self.assertSweepable("machines.md")
        self.assertSweepable("notes/scratch.md")


class PartitionTest(unittest.TestCase):
    def test_it_splits_into_source_and_sweepable_preserving_order(self):
        source, sweepable = gitutil.partition_paths([
            "journal/2026-09-15/a.json",
            "bin/away.py",
            "loops/zap-surface.md",
            "docs/ROADMAP.md",
        ])
        self.assertEqual(source, ["bin/away.py", "docs/ROADMAP.md"])
        self.assertEqual(sweepable, ["journal/2026-09-15/a.json", "loops/zap-surface.md"])

    def test_nothing_in_nothing_out(self):
        self.assertEqual(gitutil.partition_paths([]), ([], []))

    def test_an_all_board_change_leaves_no_source(self):
        source, sweepable = gitutil.partition_paths(["BOARD.md", "journal/x/y.heartbeat"])
        self.assertEqual(source, [])
        self.assertEqual(len(sweepable), 2)

    def test_an_all_source_change_leaves_nothing_to_sweep(self):
        source, sweepable = gitutil.partition_paths(["bin/sync.py", "tests/test_sync.py"])
        self.assertEqual(len(source), 2)
        self.assertEqual(sweepable, [])


if __name__ == "__main__":
    unittest.main()
