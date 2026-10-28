"""The private-text guard: board prose never reaches Lodestar's github.com history (ruling 5).

Every work-looking string below is synthetic (example names, made-up ids)."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin"))

from lib import frontmatter, gitutil, journal, loops, paths, proseguard  # noqa: E402
from tests.test_reckoning import make_checkout  # noqa: E402
from tests.test_sendreceive import SendReceiveTest  # noqa: E402

FAKE_ADDRESS = "zed.example@work.private.example"
FAKE_ITEM = "AAMkAGI2TG93AAA" + "x" * 30
LOOP = "---\nid: alpha\ntitle: Alpha\nclass: background\nstate: open\n---\n"


def git(repo, *args):
    return subprocess.run(["git", "-C", repo] + list(args), check=True, capture_output=True,
                          text=True).stdout


class GuardBase(unittest.TestCase):
    def setUp(self):
        self.d = os.path.realpath(tempfile.mkdtemp(prefix="lodestar-guard-"))
        self.addCleanup(shutil.rmtree, self.d, True)
        self.repo = os.path.join(self.d, "Lodestar")
        os.makedirs(os.path.join(self.repo, "loops"))
        with open(os.path.join(self.repo, "machines.md"), "w") as handle:
            handle.write("| Key | Role | Labels |\n|---|---|---|\n"
                         "| mt-002 | laptop | mac, work, laptop |\n"
                         "| tower | home | windows-wsl, home |\n")
        git(self.d, "init", "-q", "-b", "main", self.repo)
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "t")
        self.store = make_checkout(os.path.join(self.d, "private-store"),
                                 "git@git.private.example:x/private-store.git")
        self._env = {k: os.environ.get(k) for k in
                     ("LODESTAR_ROOT", "LODESTAR_PRIVATE_ROOT", "LODESTAR_MACHINE",
                      "LODESTAR_STATE_DIR")}
        os.environ["LODESTAR_ROOT"] = self.repo
        os.environ["LODESTAR_PRIVATE_ROOT"] = self.store
        os.environ["LODESTAR_MACHINE"] = "mt-002"
        os.environ["LODESTAR_STATE_DIR"] = os.path.join(self.d, "state")
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def write(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def no_store(self):
        os.environ["LODESTAR_PRIVATE_ROOT"] = os.path.join(self.d, "absent")


class FixTest(GuardBase):
    def test_loop_prose_moves_to_the_store_and_the_file_keeps_a_pointer(self):
        self.write("loops/alpha.md", LOOP + "Met with a colleague about the SOW.\n")
        report = proseguard.fix(self.repo, ["loops/alpha.md"])
        self.assertEqual(len(report), 1)
        _, body = frontmatter.read_file(os.path.join(self.repo, "loops/alpha.md"))
        self.assertEqual(body, proseguard.pointer("alpha"))
        self.assertEqual(proseguard.read_body("alpha"), "Met with a colleague about the SOW.\n")
        self.assertEqual(proseguard.violations(self.repo, ["loops/alpha.md"]), [])

    def test_a_stale_whole_body_rewrite_does_not_duplicate_the_store(self):
        proseguard.write_body("alpha", "old\nnew entry\n")
        self.write("loops/alpha.md", LOOP + "old\n")
        proseguard.fix(self.repo, ["loops/alpha.md"])
        self.assertEqual(proseguard.read_body("alpha"), "old\nnew entry\n")

    def test_an_appended_body_replaces_its_prefix(self):
        proseguard.write_body("alpha", "old\n")
        self.write("loops/alpha.md", LOOP + "old\nappended\n")
        proseguard.fix(self.repo, ["loops/alpha.md"])
        self.assertEqual(proseguard.read_body("alpha"), "old\nappended\n")

    def test_diverged_prose_keeps_both(self):
        proseguard.write_body("alpha", "one version\n")
        self.write("loops/alpha.md", LOOP + "another version\n")
        proseguard.fix(self.repo, ["loops/alpha.md"])
        stored = proseguard.read_body("alpha")
        self.assertIn("one version", stored)
        self.assertIn("another version", stored)
        self.assertIn("reconcile by hand", stored)

    def test_without_a_store_prose_goes_to_a_local_stash_never_away(self):
        self.no_store()
        self.write("loops/alpha.md", LOOP + "some prose\n")
        report = proseguard.fix(self.repo, ["loops/alpha.md"])
        self.assertIn("local stash", report[0])
        _, body = frontmatter.read_file(os.path.join(self.repo, "loops/alpha.md"))
        self.assertTrue(proseguard.is_clean_body(body))
        stash = proseguard.stash_dir("loops")
        kept = "".join(open(os.path.join(stash, n)).read() for n in os.listdir(stash))
        self.assertIn("some prose", kept)

    def test_a_hand_written_journal_note_moves_and_leaves_a_ref(self):
        self.write("journal/2026-10-13/1-abc.json",
                   json.dumps({"loop": "alpha", "event": "touch", "note": "a quoted line",
                               "ts": "2026-10-13T10:00:00-07:00"}) + "\n")
        proseguard.fix(self.repo, ["journal/2026-10-13/1-abc.json"])
        with open(os.path.join(self.repo, "journal/2026-10-13/1-abc.json")) as handle:
            record = json.load(handle)
        self.assertNotIn("note", record)
        self.assertEqual(proseguard.note_text(record), "a quoted line")

    def test_journal_append_never_writes_a_note_into_lodestar(self):
        path = journal.append("alpha", "touch", note="drafted section 2")
        with open(path) as handle:
            record = json.load(handle)
        self.assertNotIn("note", record)
        self.assertEqual(proseguard.note_text(record), "drafted section 2")

    def test_journal_append_without_a_store_marks_the_note_withheld(self):
        self.no_store()
        path = journal.append("alpha", "touch", note="drafted section 2")
        with open(path) as handle:
            record = json.load(handle)
        self.assertNotIn("note", record)
        self.assertTrue(record["note_withheld"])

    def test_upsert_with_a_body_and_no_store_writes_nothing(self):
        from lib import privatestore
        self.no_store()
        with self.assertRaises(privatestore.Unavailable):
            loops.upsert("alpha", {}, body="prose")
        self.assertFalse(os.path.exists(paths.loop_file("alpha")))


class ViolationsTest(GuardBase):
    def test_shapes(self):
        self.write("loops/alpha.md", LOOP + "prose\n")
        self.write("journal/d/1.json", json.dumps({"note": "x"}))
        self.write("loops/beta.md", LOOP.replace("alpha", "beta") + proseguard.pointer("beta"))
        found = dict(proseguard.violations(
            self.repo, ["loops/alpha.md", "journal/d/1.json", "loops/beta.md"]))
        self.assertIn("loops/alpha.md", found)
        self.assertIn("journal/d/1.json", found)
        self.assertNotIn("loops/beta.md", found)

    def test_forbidden_paths(self):
        for rel in ("conversations/x.md", "handoffs/SOLO_HANDOFF_1.md", ".consult/transcript.md",
                    "airlock/captures/d/x.md", "airlock/review/zap-idmap.json",
                    "claude/snapshot/mt-002/memory/p/MEMORY.md"):
            self.write(rel, "x\n")
            self.assertTrue(proseguard.violations(self.repo, [rel]), rel)
        for rel in ("airlock/review/mockups/build.py",
                    "claude/snapshot/tower/memory/p/MEMORY.md"):
            self.write(rel, "x\n")
            self.assertEqual(proseguard.violations(self.repo, [rel]), [], rel)

    def test_markers_on_added_lines_only(self):
        self.write("priority/notes.md", "old line %s\n" % FAKE_ADDRESS)
        git(self.repo, "add", "priority/notes.md")
        git(self.repo, "commit", "-qm", "seed")
        self.write("priority/notes.md", "old line %s\nnew clean line\n" % FAKE_ADDRESS)
        self.assertEqual(proseguard.violations(self.repo, ["priority/notes.md"]), [])
        self.write("priority/notes.md", "old line %s\nid %s\n" % (FAKE_ADDRESS, FAKE_ITEM))
        found = proseguard.violations(self.repo, ["priority/notes.md"])
        self.assertIn("Outlook item id", found[0][1])

    def test_lukas_own_address_and_source_files_are_not_markers(self):
        self.write("claude/global/x.md", "commits carry owner@work.private.example\n")
        self.write("bin/lib/thing.py", "EXAMPLE = %r\n" % FAKE_ADDRESS)
        self.assertEqual(proseguard.violations(
            self.repo, ["claude/global/x.md", "bin/lib/thing.py"]), [])

    def test_a_deletion_is_never_a_violation(self):
        self.assertEqual(proseguard.violations(self.repo, ["conversations/gone.md"]), [])


class HookTest(GuardBase):
    def hook(self):
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "bin", "prose_guard.py")
        return subprocess.run([sys.executable, script, "--staged"], cwd=self.repo,
                              capture_output=True, text=True)

    def test_the_hook_refuses_staged_prose_and_passes_a_clean_commit(self):
        self.write("loops/alpha.md", LOOP + "prose\n")
        git(self.repo, "add", "loops/alpha.md")
        refused = self.hook()
        self.assertEqual(refused.returncode, 1)
        self.assertIn("loops/alpha.md", refused.stderr)
        proseguard.fix(self.repo, ["loops/alpha.md"])
        git(self.repo, "add", "loops/alpha.md")
        self.assertEqual(self.hook().returncode, 0)

    def test_the_hook_ignores_other_repos(self):
        other = os.path.join(self.d, "Other")
        git(self.d, "init", "-q", "-b", "main", other)
        with open(os.path.join(other, "conversations.md"), "w") as handle:
            handle.write(FAKE_ADDRESS)
        git(other, "add", "conversations.md")
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "bin", "prose_guard.py")
        out = subprocess.run([sys.executable, script, "--staged"], cwd=other,
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0)

    def test_install_hook_sets_the_path_once_and_never_overrides(self):
        os.makedirs(os.path.join(self.repo, ".githooks"))
        self.write(".githooks/pre-commit", "#!/bin/sh\n")
        self.assertIn("installed", proseguard.install_hook(self.repo))
        self.assertIsNone(proseguard.install_hook(self.repo))
        git(self.repo, "config", "core.hooksPath", "elsewhere")
        self.assertIn("NOT installed", proseguard.install_hook(self.repo))

    def test_install_hook_rewrites_an_absolute_spelling_of_its_own_dir(self):
        os.makedirs(os.path.join(self.repo, ".githooks"))
        self.write(".githooks/pre-commit", "#!/bin/sh\n")
        git(self.repo, "config", "core.hooksPath", os.path.join(self.repo, ".githooks"))
        self.assertIn("rewritten", proseguard.install_hook(self.repo))
        self.assertIsNone(proseguard.install_hook(self.repo))


class RestoreTest(GuardBase):
    def commit(self, rel, text):
        self.write(rel, text)
        git(self.repo, "add", rel)
        git(self.repo, "commit", "-qm", rel)

    def test_a_leftover_record_change_is_preserved_then_restored(self):
        self.commit("conversations/r.md", "v1\n")
        self.write("conversations/r.md", "v2 with more dialog\n")
        report = proseguard.fix(self.repo, ["conversations/r.md"])
        self.assertIn("restored to HEAD", report[0])
        with open(os.path.join(self.repo, "conversations/r.md")) as handle:
            self.assertEqual(handle.read(), "v1\n")
        kept = os.path.join(self.store, "projects", "Lodestar", "conversations", "r.md")
        with open(kept) as handle:
            self.assertEqual(handle.read(), "v2 with more dialog\n")

    def test_a_second_different_change_never_overwrites_the_first(self):
        self.commit(".consult/transcript.md", "a\n")
        self.write(".consult/transcript.md", "a\nb\n")
        proseguard.fix(self.repo, [".consult/transcript.md"])
        self.write(".consult/transcript.md", "a\nc\n")
        proseguard.fix(self.repo, [".consult/transcript.md"])
        folder = os.path.join(self.store, "projects", "Lodestar", "consult")
        texts = sorted(open(os.path.join(folder, n)).read() for n in os.listdir(folder))
        self.assertEqual(texts, ["a\nb\n", "a\nc\n"])

    def test_migrate_moves_every_body_and_note_and_copies_records(self):
        self.commit("loops/alpha.md", LOOP + "old prose\n")
        self.commit("journal/d/1.json", json.dumps({"loop": "alpha", "note": "n",
                                                    "ts": "2026-10-01T00:00:00"}) + "\n")
        self.commit("handoffs/SOLO_HANDOFF_1.md", "h\n")
        counts = proseguard.migrate(self.repo)
        self.assertEqual(counts, {"loops": 1, "notes": 1, "copied": 1, "unreadable": 0})
        self.assertEqual(proseguard.violations(self.repo, ["loops/alpha.md", "journal/d/1.json"]),
                         [])
        self.assertTrue(os.path.exists(os.path.join(self.repo, "handoffs/SOLO_HANDOFF_1.md")))


class ReviewCounterexamplesTest(GuardBase):
    """Counterexamples from the GPT review of 2026-10-13, each turned into a test."""

    def commit_all(self, msg="c", *extra):
        git(self.repo, "add", "-A")
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", msg] + list(extra), check=True,
                       capture_output=True)

    def test_an_added_line_starting_with_plus_plus_is_still_scanned(self):
        self.write("BOARD.md", "x\n")
        self.commit_all()
        self.write("BOARD.md", "x\n++ %s\n" % FAKE_ADDRESS)
        self.assertTrue(proseguard.violations(self.repo, ["BOARD.md"]))

    def test_a_binary_looking_board_change_is_scanned_whole(self):
        self.write("BOARD.md", "x\n")
        self.commit_all()
        self.write("BOARD.md", "\0 %s\n" % FAKE_ADDRESS)
        self.assertTrue(proseguard.violations(self.repo, ["BOARD.md"]))

    def test_malformed_and_non_object_journal_events_fail_closed(self):
        self.write("journal/d/a.json", '{"note":"internal decision"')
        self.write("journal/d/b.json", json.dumps([{"note": "x"}]))
        found = dict(proseguard.violations(self.repo, ["journal/d/a.json", "journal/d/b.json"]))
        self.assertEqual(set(found), {"journal/d/a.json", "journal/d/b.json"})

    def test_a_pointer_carrying_prose_in_its_path_is_not_a_pointer(self):
        self.write("loops/alpha.md", LOOP + proseguard.POINTER_PREFIX + "Secret_launch_Friday\n")
        self.assertTrue(proseguard.violations(self.repo, ["loops/alpha.md"]))

    def test_a_loop_renamed_into_a_subdirectory_keeps_the_shape_rule(self):
        self.write("loops/archive/x.md", LOOP + "prose\n")
        self.assertTrue(proseguard.violations(self.repo, ["loops/archive/x.md"]))

    def test_a_marker_in_a_filename_is_refused(self):
        self.write("journal/d/%s.json" % FAKE_ADDRESS, "{}\n")
        self.assertTrue(proseguard.violations(self.repo, ["journal/d/%s.json" % FAKE_ADDRESS]))

    def test_a_no_verify_commit_and_a_marker_message_are_caught_at_the_push(self):
        self.write("BOARD.md", "x\n")
        self.commit_all()
        bare = os.path.join(self.d, "remote.git")
        git(self.d, "init", "-q", "--bare", bare)
        git(self.repo, "remote", "add", "origin", bare)
        git(self.repo, "push", "-q", "origin", "main")
        self.assertEqual(proseguard.outgoing_violations(self.repo), [])
        self.write("loops/alpha.md", LOOP + "prose\n")
        self.commit_all("touch alpha", "--no-verify")
        self.write("BOARD.md", "y\n")
        self.commit_all("met %s" % FAKE_ADDRESS)
        found = proseguard.outgoing_violations(self.repo)
        whats = " ".join(w for w, _ in found)
        self.assertIn("loops/alpha.md", whats)
        self.assertIn("<message>", whats)

    def test_a_shorter_hand_edit_is_kept_not_silently_dropped(self):
        proseguard.write_body("alpha", "Do not deploy.\n")
        self.write("loops/alpha.md", LOOP + "deploy.\n")
        proseguard.fix(self.repo, ["loops/alpha.md"])
        self.assertIn("reconcile by hand", proseguard.read_body("alpha"))

    def test_a_staged_version_is_preserved_too_and_head_is_restored(self):
        self.write("conversations/r.md", "A\n")
        self.commit_all()
        self.write("conversations/r.md", "B\n")
        git(self.repo, "add", "conversations/r.md")
        self.write("conversations/r.md", "C\n")
        proseguard.fix(self.repo, ["conversations/r.md"])
        with open(os.path.join(self.repo, "conversations/r.md")) as handle:
            self.assertEqual(handle.read(), "A\n")
        self.assertEqual(git(self.repo, "diff", "--cached", "--name-only"), "")
        folder = os.path.join(self.store, "projects", "Lodestar", "conversations")
        texts = sorted(open(os.path.join(folder, n)).read() for n in os.listdir(folder))
        self.assertEqual(texts, ["B\n", "C\n"])

    def test_three_different_preserved_versions_all_survive(self):
        for text in ("A\n", "B\n", "C\n"):
            os.makedirs(os.path.join(self.repo, "handoffs"), exist_ok=True)
            self.write("handoffs/h.md", text)
            proseguard.preserve(self.repo, "handoffs/h.md", "handoffs/h.md")
        folder = os.path.join(self.store, "projects", "Lodestar", "handoffs")
        self.assertEqual(len(os.listdir(folder)), 3)

    def test_a_repointed_remote_is_seen_at_once(self):
        from lib import prosehome
        repo = make_checkout(os.path.join(self.d, "Moving"), "git@git.private.example:x/m.git")
        self.assertEqual(prosehome.place(repo, "x").where, prosehome.REPO)
        git(repo, "remote", "set-url", "origin", "git@github.com:x/m.git")
        self.assertEqual(prosehome.place(repo, "x").where, prosehome.STORE)


class SweepGuardTest(SendReceiveTest):
    """The hourly sweep moves prose out, withholds what is still unsafe, and flags it."""

    def setUp(self):
        super().setUp()
        self.store = make_checkout(os.path.join(self.tmp, "private-store"),
                                 "git@git.private.example:x/private-store.git")
        self._jp = os.environ.get("LODESTAR_PRIVATE_ROOT")
        os.environ["LODESTAR_PRIVATE_ROOT"] = self.store
        self.addCleanup(lambda: os.environ.__setitem__("LODESTAR_PRIVATE_ROOT", self._jp)
                        if self._jp else os.environ.pop("LODESTAR_PRIVATE_ROOT", None))

    def test_the_sweep_commits_a_pointer_and_withholds_a_marker(self):
        path = self.make_repo("lode")
        board = self.root
        with open(os.path.join(board, "machines.md")) as handle:
            self.write(path, "machines.md", handle.read(), age_seconds=3600)
        os.makedirs(os.path.join(path, "sync"), exist_ok=True)
        self.root = path
        os.environ["LODESTAR_ROOT"] = path
        self.write(path, "loops/alpha.md", LOOP + "prose from a meeting\n", age_seconds=3600)
        self.write(path, "priority/leak.md", "from %s\n" % FAKE_ADDRESS, age_seconds=3600)
        self.write(path, "notes.md", "fine\n", age_seconds=3600)
        self.register("lode")
        old = os.path.getmtime(os.path.join(path, "notes.md"))
        os.utime(os.path.join(path, "sync", "registry.md"), (old, old))
        self.run_sweep()
        files = self.remote_files("lode")
        self.assertIn("loops/alpha.md", files, self.detail("lode"))
        self.assertIn("notes.md", files)
        self.assertNotIn("priority/leak.md", files)
        remote = self.repos["lode"][1]
        blob = subprocess.run(["git", "--git-dir", remote, "show", "HEAD:loops/alpha.md"],
                              capture_output=True, text=True).stdout
        self.assertNotIn("prose from a meeting", blob)
        self.assertIn(proseguard.POINTER_PREFIX, blob)
        self.assertEqual(proseguard.read_body("alpha"), "prose from a meeting\n")
        self.assertIn("WITHHELD", self.detail("lode"))


if __name__ == "__main__":
    unittest.main()
