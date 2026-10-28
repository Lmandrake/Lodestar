"""Auto-rebase of a diverged repo, resolving `sync/remotes.md` conflicts toward SSH.

Owner ruling 2026-10-09 by question card: "Auto-rebase, prefer SSH". The board sat diverged six
hours over one `sync/remotes.md` row (`https://` on the laptop, `git@` on tower).
"""

import os
import subprocess
import unittest

import sync as sync_cmd
from lib import gitutil, skillclone

from tests.test_sync import SweepTest, git

HEADER = "# Repo remotes\n\n| Name | Remote |\n|------|--------|\n"
SSH = "git@github.com:Lmandrake/GeoFM.git"
HTTPS = "https://github.com/Lmandrake/GeoFM.git"


def remotes(*rows):
    return HEADER + "".join("| %s | %s |\n" % row for row in rows)


class ResolverTest(unittest.TestCase):
    def conflict(self, ours, theirs):
        return (HEADER + "| Alpha | git@github.com:L/Alpha.git |\n<<<<<<< HEAD\n" + ours
                + "=======\n" + theirs + ">>>>>>> abc (laptop)\n| Zeta | git@github.com:L/Zeta.git |\n")

    def test_https_vs_ssh_resolves_to_ssh_either_side(self):
        for ours, theirs in ((SSH, HTTPS), (HTTPS, SSH)):
            text, why = skillclone.resolve_remotes_conflict(
                self.conflict("| GeoFM | %s |\n" % ours, "| GeoFM | %s |\n" % theirs))
            self.assertEqual(why, "")
            self.assertIn("| GeoFM | %s |" % SSH, text)
            self.assertNotIn("<<<<<<<", text)
            self.assertNotIn(HTTPS, text)

    def test_a_row_only_one_side_has_is_kept(self):
        text, _ = skillclone.resolve_remotes_conflict(self.conflict(
            "| GeoFM | %s |\n" % HTTPS, "| GeoFM | %s |\n| Glyph | git@github.com:L/Glyph.git |\n" % SSH))
        self.assertIn("| Glyph |", text)

    def test_two_https_forms_refuse(self):
        text, why = skillclone.resolve_remotes_conflict(self.conflict(
            "| GeoFM | %s |\n" % HTTPS, "| GeoFM | https://github.com/Lmandrake/geofm |\n"))
        self.assertIsNone(text)
        self.assertIn("GeoFM", why)

    def test_two_ssh_forms_refuse(self):
        text, _ = skillclone.resolve_remotes_conflict(self.conflict(
            "| GeoFM | %s |\n" % SSH, "| GeoFM | ssh://git@github.com/Lmandrake/GeoFM |\n"))
        self.assertIsNone(text)

    def test_different_repos_refuse(self):
        text, _ = skillclone.resolve_remotes_conflict(self.conflict(
            "| GeoFM | %s |\n" % SSH, "| GeoFM | https://github.com/Other/GeoFM.git |\n"))
        self.assertIsNone(text)

    def test_diff3_base_section_is_ignored(self):
        text, _ = skillclone.resolve_remotes_conflict(
            HEADER + "<<<<<<< HEAD\n| GeoFM | %s |\n||||||| base\n| GeoFM | old |\n=======\n"
                     "| GeoFM | %s |\n>>>>>>> x\n" % (SSH, HTTPS))
        self.assertEqual(text, remotes(("GeoFM", SSH)))


class UpdateRemotesPrefersSshTest(SweepTest):
    def entry(self, name, path):
        return type("E", (), {"name": name, "path": path})()

    def test_ssh_checkout_upgrades_an_https_row_and_https_never_downgrades(self):
        path = self.make_repo("GeoFM")
        target = os.path.join(self.tmp, "remotes.md")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(remotes(("GeoFM", HTTPS)))
        git(path, "remote", "set-url", "origin", SSH)
        mapping, changed = skillclone.update_remotes([self.entry("GeoFM", path)], target)
        self.assertTrue(changed)
        self.assertEqual(mapping["GeoFM"], SSH)

        with open(target, "w", encoding="utf-8") as handle:
            handle.write(remotes(("GeoFM", SSH)))
        git(path, "remote", "set-url", "origin", HTTPS)
        mapping, changed = skillclone.update_remotes([self.entry("GeoFM", path)], target)
        self.assertFalse(changed)
        self.assertEqual(mapping["GeoFM"], SSH)


class DivergedRebaseTest(SweepTest):
    """A real diverged pair: the swept clone and a second clone that pushed first."""

    def diverge(self, ours, theirs):
        """Seed both files, then commit `ours` locally and push `theirs` from elsewhere.

        Each argument maps repo-relative path -> new content.
        """
        path = self.make_repo("boardrepo")
        self.write(path, "sync/remotes.md", remotes(("Alpha", "git@github.com:L/Alpha.git"),
                                                    ("GeoFM", "git@github.com:L/GeoFM-old.git")))
        self.write(path, "notes.md", "seed\n")
        git(path, "add", "sync/remotes.md", "notes.md")
        git(path, "commit", "-m", "seed files")
        git(path, "push")

        other = os.path.join(self.tmp, "boardrepo-other")
        subprocess.run(["git", "clone", self.repos["boardrepo"][1], other], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        git(other, "config", "user.email", "o@example.com")
        git(other, "config", "user.name", "O")
        for name, text in theirs.items():
            self.write(other, name, text)
        git(other, "add", *theirs)
        git(other, "commit", "-m", "theirs")
        git(other, "push")

        for name, text in ours.items():
            self.write(path, name, text)
        git(path, "add", *ours)
        git(path, "commit", "-m", "ours")
        self.register_all()
        self.run_sweep()
        return path

    def read(self, path, name):
        with open(os.path.join(path, name), encoding="utf-8") as handle:
            return handle.read()

    def assert_pushed(self, path):
        self.assertEqual(gitutil.ahead_behind(path), (0, 0))
        self.assertEqual(gitutil.state(path), gitutil.OK)

    def test_a_clean_rebase_pushes(self):
        path = self.diverge({"mine.md": "mine\n"}, {"yours.md": "yours\n"})
        self.assertEqual(self.outcome("boardrepo"), sync_cmd.SYNCED)
        self.assert_pushed(path)
        self.assertTrue(os.path.exists(os.path.join(path, "yours.md")))

    def test_https_vs_ssh_remotes_conflict_resolves_to_ssh_and_pushes(self):
        path = self.diverge(
            {"sync/remotes.md": remotes(("Alpha", "git@github.com:L/Alpha.git"),
                                        ("GeoFM", HTTPS))},
            {"sync/remotes.md": remotes(("Alpha", "git@github.com:L/Alpha.git"),
                                        ("GeoFM", SSH))})
        self.assertEqual(self.outcome("boardrepo"), sync_cmd.SYNCED, self.detail("boardrepo"))
        self.assertIn("auto-resolved", self.detail("boardrepo"))
        self.assert_pushed(path)
        self.assertEqual(self.read(path, "sync/remotes.md"),
                         remotes(("Alpha", "git@github.com:L/Alpha.git"), ("GeoFM", SSH)))

    def test_ssh_side_local_with_other_edits_keeps_its_commit(self):
        path = self.diverge(
            {"sync/remotes.md": remotes(("Alpha", "git@github.com:L/Alpha.git"),
                                        ("GeoFM", SSH)),
             "mine.md": "mine\n"},
            {"sync/remotes.md": remotes(("Alpha", "git@github.com:L/Alpha.git"),
                                        ("GeoFM", HTTPS))})
        self.assertEqual(self.outcome("boardrepo"), sync_cmd.SYNCED, self.detail("boardrepo"))
        self.assert_pushed(path)
        self.assertIn(SSH, self.read(path, "sync/remotes.md"))
        self.assertTrue(os.path.exists(os.path.join(path, "mine.md")))

    def test_an_unrelated_conflict_aborts_cleanly(self):
        path = self.diverge(
            {"sync/remotes.md": remotes(("Alpha", "git@github.com:L/Alpha.git"),
                                        ("GeoFM", HTTPS)),
             "notes.md": "ours\n"},
            {"sync/remotes.md": remotes(("Alpha", "git@github.com:L/Alpha.git"),
                                        ("GeoFM", SSH)),
             "notes.md": "theirs\n"})
        self.assertEqual(self.outcome("boardrepo"), sync_cmd.FLAGGED)
        self.assertIn("a human must resolve it", self.detail("boardrepo"))
        self.assertEqual(gitutil.state(path), gitutil.OK)
        self.assertFalse(gitutil.rebase_in_progress(path))
        self.assertEqual(self.read(path, "notes.md"), "ours\n")
        self.assertIn(HTTPS, self.read(path, "sync/remotes.md"))
        ahead, behind = gitutil.ahead_behind(path)
        self.assertTrue(ahead and behind)

    def test_an_undecidable_remotes_row_aborts_cleanly(self):
        path = self.diverge(
            {"sync/remotes.md": remotes(("Alpha", "git@github.com:L/Alpha.git"),
                                        ("GeoFM", HTTPS))},
            {"sync/remotes.md": remotes(("Alpha", "git@github.com:L/Alpha.git"),
                                        ("GeoFM", "https://github.com/lmandrake/geofm"))})
        self.assertEqual(self.outcome("boardrepo"), sync_cmd.FLAGGED)
        self.assertIn("a human must resolve it", self.detail("boardrepo"))
        self.assertFalse(gitutil.rebase_in_progress(path))
        self.assertEqual(self.read(path, "sync/remotes.md"),
                         remotes(("Alpha", "git@github.com:L/Alpha.git"), ("GeoFM", HTTPS)))


if __name__ == "__main__":
    unittest.main()
