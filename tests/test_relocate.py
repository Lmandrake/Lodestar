"""Tests for lib/relocate.py — the guards between an approved plan and the filesystem.

Real `git init` fixtures in a temp directory, because the two most important refusals cannot
be tested against a fake: `not-standalone` depends on what `git rev-parse --git-common-dir`
actually returns for a linked worktree, and this repo has a recorded lesson that a linked
worktree exposes its MAIN checkout's identity — which is precisely the case a hand-built
fixture would get wrong.

Nothing here touches anything outside its temp directory.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

from lib import relocate


def git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=T", "-C", cwd] + list(args),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)


def kinds(refusals):
    return sorted(r.kind for r in refusals)


class RelocateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-relocate-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def a_repo(self, name="Alpha"):
        """A plain standalone checkout with one commit."""
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        git(path, "init", "-b", "main")
        with open(os.path.join(path, "README.md"), "w") as handle:
            handle.write("hello\n")
        git(path, "add", "README.md")
        git(path, "commit", "-m", "first")
        return path

    def at(self, *parts):
        return os.path.join(self.tmp, *parts)


class TestIsStandalone(RelocateTest):
    def test_a_plain_checkout_is_standalone(self):
        self.assertTrue(relocate.is_standalone(self.a_repo()))

    def test_a_non_repo_directory_is_not(self):
        plain = self.at("NotARepo")
        os.makedirs(plain)
        self.assertFalse(relocate.is_standalone(plain))

    def test_a_linked_worktree_is_not_standalone(self):
        """🔴 The refusal that would otherwise cost a `git worktree repair`.

        A linked worktree's `.git` is a FILE pointing at the main checkout, and
        `--git-common-dir` resolves back there — so renaming the worktree directory breaks it
        with exit 128. This fixture is a real `git worktree add` because the whole point is
        what git actually reports.
        """
        main = self.a_repo("Main")
        linked = self.at("Linked")
        git(main, "worktree", "add", linked, "-b", "side")

        self.assertTrue(os.path.isfile(os.path.join(linked, ".git")),
                        "fixture check: a linked worktree's .git is a FILE, not a directory")
        self.assertTrue(relocate.is_standalone(main), "the main checkout is still standalone")
        self.assertFalse(relocate.is_standalone(linked))


class TestPreconditionsCollectEveryRefusal(RelocateTest):
    def test_a_clean_move_has_no_refusals(self):
        self.assertEqual(relocate.preconditions(self.a_repo(), self.at("Beta")), [])

    def test_every_refusal_is_reported_not_just_the_first(self):
        """Showing one problem at a time turns one decision into several round trips.

        This fixture is wrong in three independent ways at once; all three must come back.
        """
        source = self.at("Missing")                 # missing-source
        occupied = self.at("deep", "Taken")
        os.makedirs(occupied)                        # destination-occupied
        # ...and the destination's parent is reached through a symlink.
        os.symlink(self.at("deep"), self.at("link"))
        through_link = os.path.join(self.at("link"), "Taken")

        found = relocate.preconditions(source, through_link)
        self.assertIn(relocate.MISSING_SOURCE, kinds(found))
        self.assertIn(relocate.DESTINATION_OCCUPIED, kinds(found))
        self.assertIn(relocate.SYMLINKED_DESTINATION, kinds(found))
        self.assertGreaterEqual(len(found), 3)

    def test_every_refusal_detail_names_a_real_path(self):
        """A refusal with no path in it cannot be acted on."""
        for refusal in relocate.preconditions(self.at("Missing"), self.at("Beta")):
            self.assertTrue(any(ch in refusal.detail for ch in (os.sep,)),
                            "refusal %r must name a path" % (refusal.kind,))

    def test_all_refusal_kinds_are_declared(self):
        self.assertEqual(len(set(relocate.KINDS)), len(relocate.KINDS))


class TestIndividualRefusals(RelocateTest):
    def test_missing_source(self):
        found = relocate.preconditions(self.at("Nope"), self.at("Beta"))
        self.assertEqual(kinds(found), [relocate.MISSING_SOURCE])

    def test_not_a_repo(self):
        plain = self.at("Plain")
        os.makedirs(plain)
        found = relocate.preconditions(plain, self.at("Beta"))
        self.assertEqual(kinds(found), [relocate.NOT_A_REPO])

    def test_not_standalone_refuses_a_linked_worktree(self):
        main = self.a_repo("Main")
        linked = self.at("Linked")
        git(main, "worktree", "add", linked, "-b", "side")
        found = relocate.preconditions(linked, self.at("Moved"))
        self.assertIn(relocate.NOT_STANDALONE, kinds(found))

    def test_destination_occupied(self):
        source = self.a_repo()
        os.makedirs(self.at("Beta"))
        found = relocate.preconditions(source, self.at("Beta"))
        self.assertEqual(kinds(found), [relocate.DESTINATION_OCCUPIED])

    def test_a_dangling_symlink_at_the_destination_still_counts_as_occupied(self):
        """`exists` follows the link and reports the destination free; `lexists` does not."""
        source = self.a_repo()
        os.symlink(self.at("nowhere-at-all"), self.at("Beta"))
        self.assertFalse(os.path.exists(self.at("Beta")), "fixture must be DANGLING")
        found = relocate.preconditions(source, self.at("Beta"))
        self.assertIn(relocate.DESTINATION_OCCUPIED, kinds(found))

    def test_destination_parent_missing(self):
        source = self.a_repo()
        found = relocate.preconditions(source, self.at("no", "such", "Beta"))
        self.assertIn(relocate.DESTINATION_PARENT_MISSING, kinds(found))

    def test_path_swap(self):
        source = self.a_repo()
        found = relocate.preconditions(source, source)
        self.assertEqual(kinds(found), [relocate.PATH_SWAP])

    def test_nested_managed_root_when_the_destination_is_inside_another_repo(self):
        source = self.a_repo("Alpha")
        other = self.a_repo("Other")
        found = relocate.preconditions(source, os.path.join(other, "Inner"),
                                       managed_paths=[other])
        self.assertIn(relocate.NESTED_MANAGED_ROOT, kinds(found))

    def test_nested_managed_root_when_another_repo_is_inside_the_destination(self):
        source = self.a_repo("Alpha")
        os.makedirs(self.at("Wrapper"))
        inner = self.a_repo(os.path.join("Wrapper", "Inner"))
        found = relocate.preconditions(source, self.at("Wrapper", "Dest"),
                                       managed_paths=[inner])
        # Dest does not contain Inner, so this specific pair is fine...
        self.assertNotIn(relocate.NESTED_MANAGED_ROOT, kinds(found))
        # ...but moving to Wrapper itself would swallow it.
        found = relocate.preconditions(source, self.at("Wrapper"), managed_paths=[inner])
        self.assertIn(relocate.NESTED_MANAGED_ROOT, kinds(found))

    def test_a_repos_own_managed_path_is_not_read_as_nesting(self):
        """The row being moved is itself in managed_paths; that must not self-refuse."""
        source = self.a_repo("Alpha")
        found = relocate.preconditions(source, self.at("Beta"), managed_paths=[source])
        self.assertEqual(found, [])


class TestCaseOnlyRenameIsRefused(RelocateTest):
    """🔴 APFS is case-insensitive, so this is not a move.

    This repo has been bitten twice: mixed-case Graph ids silently overwrote 14 of 106 files,
    and a test fixture's hardcoded machine name turned out to BE this machine. Renaming in
    place here either no-ops or destroys the source, so it is refused rather than attempted.
    """

    def test_case_only_rename_is_refused(self):
        source = self.a_repo("Alpha")
        found = relocate.preconditions(source, self.at("alpha"))
        self.assertIn(relocate.CASE_ONLY, kinds(found))

    def test_a_real_rename_that_also_changes_case_is_allowed(self):
        source = self.a_repo("OpenCodeCLI")
        self.assertEqual(relocate.preconditions(source, self.at("DispatchOpenCode")), [])

    def test_move_refuses_a_case_only_rename_and_leaves_the_source_alone(self):
        source = self.a_repo("Alpha")
        result = relocate.move(source, self.at("alpha"))
        self.assertFalse(result["ok"])
        self.assertTrue(os.path.isdir(source))


class TestMove(RelocateTest):
    def test_a_clean_move_relocates_the_checkout(self):
        source = self.a_repo("Alpha")
        result = relocate.move(source, self.at("Beta"))
        self.assertTrue(result["ok"])
        self.assertTrue(result["moved"])
        self.assertFalse(os.path.lexists(source))
        self.assertTrue(os.path.isdir(os.path.join(self.at("Beta"), ".git")))

    def test_the_moved_checkout_is_still_a_working_repo(self):
        """A rename that lands but breaks git is not a successful move."""
        source = self.a_repo("Alpha")
        relocate.move(source, self.at("Beta"))
        head = subprocess.run(["git", "-C", self.at("Beta"), "rev-parse", "HEAD"],
                              stdout=subprocess.PIPE)
        self.assertEqual(head.returncode, 0)

    def test_move_refuses_when_any_precondition_fails(self):
        source = self.a_repo("Alpha")
        os.makedirs(self.at("Beta"))
        result = relocate.move(source, self.at("Beta"))
        self.assertFalse(result["ok"])
        self.assertFalse(result["moved"])
        self.assertTrue(os.path.isdir(source), "the source must be untouched")
        self.assertIn(relocate.DESTINATION_OCCUPIED, kinds(result["refusals"]))

    def test_move_never_merges_into_an_occupied_destination(self):
        """🔴 The sweep auto-commits under a managed path, so a merge pushes one repo's
        files under another's name. Refusing is the only safe answer."""
        source = self.a_repo("Alpha")
        os.makedirs(self.at("Beta"))
        with open(self.at("Beta", "theirs.txt"), "w") as handle:
            handle.write("not mine\n")

        relocate.move(source, self.at("Beta"))
        self.assertEqual(sorted(os.listdir(self.at("Beta"))), ["theirs.txt"],
                         "nothing from the source may appear in the destination")
        self.assertTrue(os.path.exists(os.path.join(source, "README.md")))

    def test_dry_run_evaluates_everything_and_mutates_nothing(self):
        source = self.a_repo("Alpha")
        result = relocate.move(source, self.at("Beta"), dry_run=True)
        self.assertTrue(result["ok"])
        self.assertFalse(result["moved"])
        self.assertTrue(os.path.isdir(source), "dry run must leave the source in place")
        self.assertFalse(os.path.lexists(self.at("Beta")),
                         "dry run must not create the destination")

    def test_dry_run_still_reports_refusals(self):
        result = relocate.move(self.at("Nope"), self.at("Beta"), dry_run=True)
        self.assertFalse(result["ok"])
        self.assertIn(relocate.MISSING_SOURCE, kinds(result["refusals"]))


class TestQuarantine(RelocateTest):
    def test_quarantine_moves_the_checkout_out_of_the_working_area(self):
        source = self.a_repo("Alpha")
        holding = self.at("holding")
        result = relocate.quarantine(source, holding)
        self.assertTrue(result["ok"])
        self.assertTrue(result["moved"])
        self.assertFalse(os.path.lexists(source))
        self.assertTrue(os.path.isdir(os.path.join(holding, "Alpha")))
        self.assertEqual(result["holding_path"], os.path.join(holding, "Alpha"))

    def test_the_report_says_quarantined_not_evicted(self):
        """§3E: a report never claims a confidentiality property it did not deliver.

        The material is still on this machine, so "evicted" would be a false claim.
        """
        result = relocate.quarantine(self.a_repo("Alpha"), self.at("holding"))
        self.assertIn("quarantine", result["reason"].lower())
        self.assertIn("incomplete", result["reason"].lower())
        self.assertNotIn("evicted", result["reason"].lower())

    def test_quarantine_returns_the_holding_path_even_on_a_dry_run(self):
        """The caller must be able to RECORD the location before the move needs it."""
        result = relocate.quarantine(self.a_repo("Alpha"), self.at("holding"), dry_run=True)
        self.assertTrue(result["ok"])
        self.assertFalse(result["moved"])
        self.assertEqual(result["holding_path"], os.path.join(self.at("holding"), "Alpha"))
        self.assertTrue(os.path.isdir(self.at("Alpha")))

    def test_an_occupied_holding_location_refuses_rather_than_overwriting(self):
        source = self.a_repo("Alpha")
        holding = self.at("holding")
        os.makedirs(os.path.join(holding, "Alpha"))
        with open(os.path.join(holding, "Alpha", "earlier.txt"), "w") as handle:
            handle.write("quarantined before\n")

        result = relocate.quarantine(source, holding)
        self.assertFalse(result["ok"])
        self.assertTrue(os.path.isdir(source), "the source must be untouched")
        self.assertEqual(os.listdir(os.path.join(holding, "Alpha")), ["earlier.txt"])

    def test_quarantining_nothing_is_a_refusal_not_a_success(self):
        result = relocate.quarantine(self.at("Nope"), self.at("holding"))
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
