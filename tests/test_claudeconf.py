"""Tests for bin/claudeconf.py — rendering ~/.claude/CLAUDE.md from the repo.

The live ~/.claude is never touched: CLAUDE_HOME and LIVE_CLAUDE_MD are redirected into a
temp directory for every test.
"""

import contextlib
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

import claudeconf
from tests import needs_private  # noqa: E402

MACHINES = """| Key | Role |
|-----|------|
| test-box | testbox |
"""


class ConfTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-conf-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self.root = os.path.join(self.tmp, "board")
        self.globals = os.path.join(self.root, "claude", "global")
        os.makedirs(os.path.join(self.globals, "machines"))
        with open(os.path.join(self.root, "machines.md"), "w", encoding="utf-8") as handle:
            handle.write(MACHINES)

        self.home = os.path.join(self.tmp, "claude-home")
        os.makedirs(self.home)

        self._saved_env = {
            k: os.environ.get(k) for k in ("LODESTAR_ROOT", "LODESTAR_MACHINE")
        }
        os.environ["LODESTAR_ROOT"] = self.root
        os.environ["LODESTAR_MACHINE"] = "test-box"

        self._saved_home = claudeconf.CLAUDE_HOME
        self._saved_live = claudeconf.LIVE_CLAUDE_MD
        # These fixtures' machines are unregistered, which `prosehome` treats as `work` and so
        # snapshots memory to the private store. This suite pins the in-repo layout (tower's);
        # the work-Mac routing is TestMemoryGoesToPrivateOnAWorkMac below.
        self._saved_memdir = claudeconf.memory_snapshot_dir
        claudeconf.memory_snapshot_dir = lambda: os.path.join(claudeconf.snapshot_dir(), "memory")
        claudeconf.CLAUDE_HOME = self.home
        claudeconf.LIVE_CLAUDE_MD = os.path.join(self.home, "CLAUDE.md")
        self.addCleanup(self._restore)

        stack = contextlib.ExitStack()
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.addCleanup(stack.close)

        self.section("machines/test-box.md", "# Machine\n\nA test box.\n")
        self.section("10-first.md", "# First\n\nShared one.\n")
        self.section("20-second.md", "# Second\n\nShared two.\n")

    def _restore(self):
        claudeconf.memory_snapshot_dir = self._saved_memdir
        claudeconf.CLAUDE_HOME = self._saved_home
        claudeconf.LIVE_CLAUDE_MD = self._saved_live
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        import lib.machine as machine_mod
        machine_mod._key_cache = None

    def section(self, relative, text):
        path = os.path.join(self.globals, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def live(self):
        with open(claudeconf.LIVE_CLAUDE_MD, encoding="utf-8") as handle:
            return handle.read()

    def run_cmd(self, *argv):
        return claudeconf.main(list(argv))


class TestRender(ConfTest):
    def test_writes_the_live_file(self):
        self.assertEqual(self.run_cmd("render"), 0)
        self.assertTrue(os.path.exists(claudeconf.LIVE_CLAUDE_MD))

    def test_machine_section_comes_first(self):
        self.run_cmd("render")
        text = self.live()
        self.assertLess(text.index("# Machine"), text.index("# First"))

    def test_shared_sections_are_in_filename_order(self):
        self.run_cmd("render")
        text = self.live()
        self.assertLess(text.index("# First"), text.index("# Second"))

    def test_banner_is_an_html_comment_so_it_costs_no_context(self):
        self.run_cmd("render")
        self.assertTrue(self.live().startswith("<!--"))

    def test_render_is_idempotent(self):
        self.run_cmd("render")
        first = self.live()
        self.run_cmd("render")
        self.assertEqual(first, self.live())

    def test_a_missing_machine_section_refuses_rather_than_guessing(self):
        # A desktop section claims no battery and no lid; wrong on a laptop.
        os.unlink(os.path.join(self.globals, "machines", "test-box.md"))
        import lib.machine as machine_mod
        machine_mod._key_cache = None
        with self.assertRaises(SystemExit) as caught:
            self.run_cmd("render")
        self.assertIn("no machine section", str(caught.exception))

    def test_another_machine_gets_its_own_section(self):
        self.section("machines/other-box.md", "# Machine\n\nThe laptop.\n")
        os.environ["LODESTAR_MACHINE"] = "other-box"
        import lib.machine as machine_mod
        machine_mod._key_cache = None
        self.run_cmd("render")
        self.assertIn("The laptop.", self.live())
        self.assertNotIn("A test box.", self.live())


class TestCheck(ConfTest):
    def test_passes_right_after_a_render(self):
        self.run_cmd("render")
        self.run_cmd("snapshot")
        self.assertEqual(self.run_cmd("check"), 0)

    def test_fails_when_the_live_file_is_missing(self):
        self.assertEqual(self.run_cmd("check"), 1)

    def test_fails_when_the_live_file_was_hand_edited(self):
        self.run_cmd("render")
        self.run_cmd("snapshot")
        with open(claudeconf.LIVE_CLAUDE_MD, "a", encoding="utf-8") as handle:
            handle.write("\n# Sneaky\n\nEdited in place.\n")
        self.assertEqual(self.run_cmd("check"), 1)

    def test_fails_when_a_source_section_changed(self):
        self.run_cmd("render")
        self.run_cmd("snapshot")
        self.section("10-first.md", "# First\n\nShared one, revised.\n")
        self.assertEqual(self.run_cmd("check"), 1)

    def test_fails_when_settings_json_drifts(self):
        self.run_cmd("render")
        with open(os.path.join(self.home, "settings.json"), "w", encoding="utf-8") as h:
            h.write('{"theme": "dark"}\n')
        self.run_cmd("snapshot")
        self.assertEqual(self.run_cmd("check"), 0)
        with open(os.path.join(self.home, "settings.json"), "w", encoding="utf-8") as h:
            h.write('{"theme": "light"}\n')
        self.assertEqual(self.run_cmd("check"), 1)


class TestImport(ConfTest):
    def test_import_of_a_rendered_file_is_a_no_op(self):
        # The reverse recovery path must not corrupt the sources it came from.
        self.run_cmd("render")
        before = {
            name: open(os.path.join(self.globals, name), encoding="utf-8").read()
            for name in ("10-first.md", "20-second.md")
        }
        self.assertEqual(self.run_cmd("import"), 0)
        for name, text in before.items():
            self.assertEqual(
                open(os.path.join(self.globals, name), encoding="utf-8").read(), text, name)

    def test_import_recovers_a_hand_edit_into_the_repo(self):
        self.run_cmd("render")
        text = self.live().replace("Shared one.", "Shared one, edited live.")
        with open(claudeconf.LIVE_CLAUDE_MD, "w", encoding="utf-8") as handle:
            handle.write(text)
        self.assertEqual(self.run_cmd("import"), 0)
        self.assertIn("edited live",
                      open(os.path.join(self.globals, "10-first.md"), encoding="utf-8").read())
        self.assertEqual(self.run_cmd("check"), 0)

    def test_import_refuses_when_the_section_count_disagrees(self):
        self.run_cmd("render")
        with open(claudeconf.LIVE_CLAUDE_MD, "a", encoding="utf-8") as handle:
            handle.write("\n# Extra\n\nUnmapped.\n")
        with self.assertRaises(SystemExit) as caught:
            self.run_cmd("import")
        self.assertIn("Reconcile by hand", str(caught.exception))

    def test_import_refuses_a_file_with_no_headers(self):
        with open(claudeconf.LIVE_CLAUDE_MD, "w", encoding="utf-8") as handle:
            handle.write("just prose, no headers\n")
        with self.assertRaises(SystemExit) as caught:
            self.run_cmd("import")
        self.assertIn("no '# ' section headers", str(caught.exception))


class TestSnapshot(ConfTest):
    def _memory(self, project, name, text):
        directory = os.path.join(self.home, "projects", project, "memory")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_copies_settings_and_statusline(self):
        with open(os.path.join(self.home, "settings.json"), "w", encoding="utf-8") as h:
            h.write("{}\n")
        with open(os.path.join(self.home, "statusline.sh"), "w", encoding="utf-8") as h:
            h.write("#!/bin/sh\necho hi\n")
        self.run_cmd("snapshot")
        snap = claudeconf.snapshot_dir()
        self.assertTrue(os.path.exists(os.path.join(snap, "settings.json")))
        self.assertTrue(os.path.exists(os.path.join(snap, "statusline.sh")))

    def test_the_snapshot_directory_is_scoped_to_this_machine(self):
        self.assertEqual(os.path.basename(claudeconf.snapshot_dir()), "test-box")

    def test_copies_auto_memory_per_project(self):
        self._memory("-proj-a", "MEMORY.md", "- one\n")
        self._memory("-proj-b", "MEMORY.md", "- two\n")
        self.run_cmd("snapshot")
        base = os.path.join(claudeconf.snapshot_dir(), "memory")
        self.assertTrue(os.path.exists(os.path.join(base, "-proj-a", "MEMORY.md")))
        self.assertTrue(os.path.exists(os.path.join(base, "-proj-b", "MEMORY.md")))

    def test_skips_empty_memory_directories(self):
        os.makedirs(os.path.join(self.home, "projects", "-empty", "memory"))
        self._memory("-proj-a", "MEMORY.md", "- one\n")
        self.run_cmd("snapshot")
        base = os.path.join(claudeconf.snapshot_dir(), "memory")
        self.assertFalse(os.path.exists(os.path.join(base, "-empty")))
        self.assertTrue(os.path.exists(os.path.join(base, "-proj-a")))


class TestTwoMachinesDoNotCollide(ConfTest):
    """Regression: a shared snapshot directory made two machines overwrite each other.

    When the Laptop came online, both machines wrote their own settings.json to the same
    committed path, so each sweep reverted the other's and the board churned chore(sync)
    commits indefinitely. Snapshots are now keyed by machine.
    """

    def _snapshot_as(self, key, settings_text):
        os.environ["LODESTAR_MACHINE"] = key
        import lib.machine as machine_mod
        machine_mod._key_cache = None
        with open(os.path.join(self.home, "settings.json"), "w", encoding="utf-8") as handle:
            handle.write(settings_text)
        claudeconf.snapshot_all()
        return claudeconf.snapshot_dir()

    def test_each_machine_gets_its_own_directory(self):
        desktop = self._snapshot_as("box-a", '{"theme": "dark"}\n')
        laptop = self._snapshot_as("box-b", '{"theme": "light"}\n')
        self.assertNotEqual(desktop, laptop)

    def test_one_machine_does_not_overwrite_the_other(self):
        desktop = self._snapshot_as("box-a", '{"theme": "dark"}\n')
        self._snapshot_as("box-b", '{"theme": "light"}\n')
        with open(os.path.join(desktop, "settings.json"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), '{"theme": "dark"}\n')

    def test_memory_files_with_the_same_project_key_stay_separate(self):
        # Project keys derive from the repo path, so both machines produce identical keys.
        for key, text in (("box-a", "- desktop note\n"), ("box-b", "- laptop note\n")):
            os.environ["LODESTAR_MACHINE"] = key
            import lib.machine as machine_mod
            machine_mod._key_cache = None
            directory = os.path.join(self.home, "projects", "-shared", "memory")
            os.makedirs(directory, exist_ok=True)
            with open(os.path.join(directory, "MEMORY.md"), "w", encoding="utf-8") as handle:
                handle.write(text)
            claudeconf.snapshot_all()

        for key, expected in (("box-a", "- desktop note\n"), ("box-b", "- laptop note\n")):
            path = os.path.join(self.root, "claude", "snapshot", key,
                                "memory", "-shared", "MEMORY.md")
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), expected, key)


class TestRestoreMemory(ConfTest):
    def _snapshot_memory(self, project, name, text):
        directory = os.path.join(claudeconf.snapshot_dir(), "memory", project)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_restores_a_missing_file(self):
        self._snapshot_memory("-proj-a", "MEMORY.md", "- from the repo\n")
        self.run_cmd("restore-memory")
        target = os.path.join(self.home, "projects", "-proj-a", "memory", "MEMORY.md")
        self.assertTrue(os.path.exists(target))

    def test_does_not_clobber_a_live_file_without_force(self):
        # Claude rewrites these mid-session; a silent overwrite would lose real notes.
        self._snapshot_memory("-proj-a", "MEMORY.md", "- from the repo\n")
        directory = os.path.join(self.home, "projects", "-proj-a", "memory")
        os.makedirs(directory)
        with open(os.path.join(directory, "MEMORY.md"), "w", encoding="utf-8") as handle:
            handle.write("- newer, local\n")
        self.run_cmd("restore-memory")
        with open(os.path.join(directory, "MEMORY.md"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "- newer, local\n")

    def test_force_overwrites(self):
        self._snapshot_memory("-proj-a", "MEMORY.md", "- from the repo\n")
        directory = os.path.join(self.home, "projects", "-proj-a", "memory")
        os.makedirs(directory)
        with open(os.path.join(directory, "MEMORY.md"), "w", encoding="utf-8") as handle:
            handle.write("- local\n")
        self.run_cmd("restore-memory", "--force")
        with open(os.path.join(directory, "MEMORY.md"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "- from the repo\n")


class TestRealRepo(unittest.TestCase):
    """Guards against the real committed sources drifting from the real live file.

    These skip on a machine claudeconf does not yet manage — a fresh clone, e.g. the Laptop
    before its CLAUDE.md is reconciled. Failing there would be noise, not signal.
    """

    def _managed(self):
        if not os.path.exists(claudeconf.LIVE_CLAUDE_MD):
            return False
        with open(claudeconf.LIVE_CLAUDE_MD, encoding="utf-8") as handle:
            return handle.read(200).startswith("<!-- GENERATED by")

    def test_the_real_live_file_matches_the_real_sources(self):
        if not self._managed():
            self.skipTest("~/.claude/CLAUDE.md is not claudeconf-managed on this machine")
        self.assertEqual(claudeconf.main(["check"]), 0,
                         "run bin/claudeconf.py render or snapshot")

    def test_this_machine_has_a_section(self):
        if not self._managed():
            self.skipTest("~/.claude/CLAUDE.md is not claudeconf-managed on this machine")
        self.assertTrue(os.path.exists(claudeconf.machine_section()))

    @needs_private("machines.md")
    def test_the_machine_section_is_not_in_the_shared_set(self):
        # A Desktop-only claim leaking into the shared sections would be wrong on the Laptop.
        shared = claudeconf.shared_sections()
        self.assertNotIn(claudeconf.machine_section(), shared)
        for path in shared:
            with open(path, encoding="utf-8") as handle:
                self.assertNotIn("Work Desktop", handle.read(), path)


class TestConverge(ConfTest):
    def test_current_when_live_matches_sources(self):
        self.run_cmd("render")
        outcome, _ = claudeconf.converge_claudemd()
        self.assertEqual(outcome, claudeconf.CURRENT)

    def test_absent_live_file_is_rendered(self):
        outcome, _ = claudeconf.converge_claudemd()
        self.assertEqual(outcome, claudeconf.FAST_FORWARD)
        self.assertIn("# Machine", self.live())

    def test_source_change_fast_forwards_an_untouched_live_file(self):
        self.run_cmd("render")
        claudeconf.converge_claudemd()           # establishes provenance
        self.section("30-third.md", "# Third\n\nNew shared rule.\n")
        outcome, _ = claudeconf.converge_claudemd()
        self.assertEqual(outcome, claudeconf.FAST_FORWARD)
        self.assertIn("# Third", self.live())

    def test_external_edit_is_captured_and_not_overwritten(self):
        self.run_cmd("render")
        claudeconf.converge_claudemd()
        with open(claudeconf.LIVE_CLAUDE_MD, "a", encoding="utf-8") as handle:
            handle.write("\n# Remembered\n\nA hash-memory append.\n")
        outcome, _ = claudeconf.converge_claudemd()
        self.assertEqual(outcome, claudeconf.DRIFTED)
        self.assertIn("# Remembered", self.live())        # not reverted
        self.assertEqual(len(claudeconf.pending_captures()), 1)
        with open(claudeconf.pending_captures()[0], encoding="utf-8") as handle:
            self.assertIn("# Remembered", handle.read())

    def test_I1_drift_does_not_advance_provenance(self):
        """The regression that would silently revert a # memory 127s later."""
        self.run_cmd("render")
        claudeconf.converge_claudemd()
        with open(claudeconf.provenance_path(), encoding="utf-8") as handle:
            before = handle.read()
        with open(claudeconf.LIVE_CLAUDE_MD, "a", encoding="utf-8") as handle:
            handle.write("\n# Remembered\n\nkeep me.\n")
        claudeconf.converge_claudemd()
        with open(claudeconf.provenance_path(), encoding="utf-8") as handle:
            after = handle.read()
        self.assertEqual(before, after)
        # And a second sweep must still refuse to render.
        outcome, _ = claudeconf.converge_claudemd()
        self.assertEqual(outcome, claudeconf.DRIFTED)
        self.assertIn("# Remembered", self.live())

    def test_I2_repeated_sweeps_capture_once(self):
        self.run_cmd("render")
        claudeconf.converge_claudemd()
        with open(claudeconf.LIVE_CLAUDE_MD, "a", encoding="utf-8") as handle:
            handle.write("\n# Remembered\n\nkeep me.\n")
        for _ in range(5):
            claudeconf.converge_claudemd()
        self.assertEqual(len(claudeconf.pending_captures()), 1)

    def test_claude_md_is_not_in_snapshot_files(self):
        """Structural guard for I1: SNAPSHOT_FILES is copied unconditionally."""
        self.assertNotIn("CLAUDE.md", claudeconf.SNAPSHOT_FILES)

    def test_missing_machine_section_blocks_instead_of_raising(self):
        os.unlink(os.path.join(self.globals, "machines", "test-box.md"))
        import lib.machine as machine_mod
        machine_mod._key_cache = None
        outcome, detail = claudeconf.converge_claudemd()
        self.assertEqual(outcome, claudeconf.BLOCKED)
        self.assertIn("no machine section", detail)

    def test_dry_run_writes_nothing(self):
        outcome, _ = claudeconf.converge_claudemd(dry_run=True)
        self.assertEqual(outcome, claudeconf.FAST_FORWARD)
        self.assertFalse(os.path.exists(claudeconf.LIVE_CLAUDE_MD))


class TestAdopt(ConfTest):
    def _slurp(self, path):
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def _capture_with(self, extra):
        self.run_cmd("render")
        claudeconf.converge_claudemd()
        with open(claudeconf.LIVE_CLAUDE_MD, "a", encoding="utf-8") as handle:
            handle.write(extra)
        claudeconf.converge_claudemd()
        return claudeconf.pending_captures()[0]

    def test_diff_finds_the_added_chunk(self):
        path = self._capture_with("\n# Remembered\n\nA new rule.\n")
        chunks = claudeconf.capture_diff(self._slurp(path), claudeconf.render_text())
        self.assertEqual(len(chunks), 1)
        self.assertIn("# Remembered", chunks[0])

    def test_diff_is_empty_when_sources_already_cover_the_capture(self):
        self.run_cmd("render")
        target = os.path.join(claudeconf.adopt_dir(), "20260101T000000-CLAUDE.md")
        claudeconf._write(target, claudeconf.render_text())
        self.assertEqual(
            claudeconf.capture_diff(self._slurp(target), claudeconf.render_text()), [])

    def test_a_hash_comment_in_a_fenced_block_is_not_a_section_boundary(self):
        # A # memory routinely carries a shell snippet. Splitting on it would shred the chunk.
        path = self._capture_with(
            "\n# Remembered\n\nRun this:\n\n```sh\n# not a heading\necho hi\n```\n")
        chunks = claudeconf.capture_diff(self._slurp(path), claudeconf.render_text())
        self.assertEqual(len(chunks), 1)
        self.assertIn("echo hi", chunks[0])
        self.assertIn("# not a heading", chunks[0])

    def test_file_chunk_appends_to_a_shared_module(self):
        claudeconf.file_chunk("# Remembered\n\nA new rule.\n", "40-adopted.md")
        text = self._slurp(os.path.join(self.globals, "40-adopted.md"))
        self.assertIn("# Remembered", text)

    def test_file_chunk_appends_to_the_machine_section_preserving_it(self):
        claudeconf.file_chunk("# Local\n\nBox-specific.\n", "machines/test-box.md")
        text = self._slurp(os.path.join(self.globals, "machines", "test-box.md"))
        self.assertIn("# Local", text)
        self.assertIn("A test box.", text)     # existing content preserved

    def test_mark_adopted_moves_the_capture_to_done(self):
        path = self._capture_with("\n# Remembered\n\nx\n")
        moved = claudeconf.mark_adopted(path)
        self.assertIn(os.path.join("done", ""), moved)
        self.assertEqual(claudeconf.pending_captures(), [])
        self.assertTrue(os.path.exists(moved))      # retained, not deleted

    def test_render_is_current_again_after_adopting(self):
        path = self._capture_with("\n# Remembered\n\nA new rule.\n")
        chunks = claudeconf.capture_diff(self._slurp(path), claudeconf.render_text())
        claudeconf.file_chunk(chunks[0], "40-adopted.md")
        claudeconf.mark_adopted(path)
        claudeconf.finish_adopt()
        outcome, _ = claudeconf.converge_claudemd()
        self.assertEqual(outcome, claudeconf.CURRENT)

    def test_without_finish_adopt_the_machine_would_stay_flagged_forever(self):
        # A filed chunk does not reproduce the live file's ad-hoc spacing, so the closing
        # render is required, not cosmetic. This pins that reasoning.
        path = self._capture_with("\n# Remembered\n\nA new rule.\n")
        chunks = claudeconf.capture_diff(self._slurp(path), claudeconf.render_text())
        claudeconf.file_chunk(chunks[0], "40-adopted.md")
        claudeconf.mark_adopted(path)
        outcome, _ = claudeconf.converge_claudemd()          # no finish_adopt
        self.assertEqual(outcome, claudeconf.DRIFTED)

    def test_finish_adopt_keeps_the_content_it_overwrites(self):
        path = self._capture_with("\n# Remembered\n\nA new rule.\n")
        chunks = claudeconf.capture_diff(self._slurp(path), claudeconf.render_text())
        claudeconf.file_chunk(chunks[0], "40-adopted.md")
        moved = claudeconf.mark_adopted(path)
        claudeconf.finish_adopt()
        # Twice over: verbatim in done/, and filed into a section.
        self.assertIn("A new rule.", self._slurp(moved))
        self.assertIn("A new rule.", self._slurp(os.path.join(self.globals, "40-adopted.md")))
        self.assertIn("A new rule.", self.live())

    def test_adopt_command_reports_nothing_when_the_queue_is_empty(self):
        self.assertEqual(self.run_cmd("adopt"), 0)

    def test_adopt_command_clears_a_capture_that_needs_no_classification(self):
        self.run_cmd("render")
        target = os.path.join(claudeconf.adopt_dir(), "20260101T000000-CLAUDE.md")
        claudeconf._write(target, claudeconf.render_text())
        self.assertEqual(self.run_cmd("adopt"), 0)
        self.assertEqual(claudeconf.pending_captures(), [])
        outcome, _ = claudeconf.converge_claudemd()
        self.assertEqual(outcome, claudeconf.CURRENT)

    def test_adopt_command_does_not_render_while_chunks_are_unfiled(self):
        # Rendering here would drop the memory from the live file before it propagates.
        self._capture_with("\n# Remembered\n\nA new rule.\n")
        self.assertEqual(self.run_cmd("adopt"), 0)
        self.assertIn("# Remembered", self.live())
        self.assertEqual(len(claudeconf.pending_captures()), 1)


class Entry(object):
    """Stand-in for registry.Entry — only .name and .path are read by skills_import."""

    def __init__(self, name, path):
        self.name = name
        self.path = path


class TestSkillsImport(ConfTest):
    def setUp(self):
        super(TestSkillsImport, self).setUp()
        self.live_skills = os.path.join(self.home, "skills")
        os.makedirs(self.live_skills)

    def _slurp(self, path):
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def _skill(self, relative):
        target = os.path.join(self.tmp, relative)
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, "SKILL.md"), "w", encoding="utf-8") as handle:
            handle.write("---\nname: s\ndescription: d\n---\n\nBody.\n")
        return target

    def test_live_skills_dir_follows_a_redirected_claude_home(self):
        # A module constant would have kept pointing at the real ~/.claude/skills.
        self.assertEqual(claudeconf.live_skills_dir(), self.live_skills)

    def test_import_seeds_a_row_per_symlink(self):
        target = self._skill(os.path.join("dev", "Alpha", ".claude", "skills", "alpha"))
        os.symlink(target, os.path.join(self.live_skills, "alpha"))
        text = self._slurp(claudeconf.skills_import(self.live_skills, []))
        self.assertIn("| alpha |", text)
        self.assertIn("| all |", text)

    def test_import_attributes_the_owning_repo_from_registry_paths(self):
        target = self._skill(os.path.join("dev", "Alpha", ".claude", "skills", "alpha"))
        os.symlink(target, os.path.join(self.live_skills, "alpha"))
        entries = [Entry("Alpha", os.path.join(self.tmp, "dev", "Alpha"))]
        text = self._slurp(claudeconf.skills_import(self.live_skills, entries))
        self.assertIn("| Alpha |", text)

    def test_import_prefers_the_longest_matching_repo_path(self):
        target = self._skill(os.path.join("dev", "Alpha", "skills", "alpha"))
        os.symlink(target, os.path.join(self.live_skills, "alpha"))
        entries = [Entry("Dev", os.path.join(self.tmp, "dev")),
                   Entry("Alpha", os.path.join(self.tmp, "dev", "Alpha"))]
        text = self._slurp(claudeconf.skills_import(self.live_skills, entries))
        self.assertIn("| Alpha |", text)
        self.assertNotIn("| Dev |", text)

    def test_import_records_a_real_directory_as_a_comment_not_a_row(self):
        os.makedirs(os.path.join(self.live_skills, "orphan"))
        text = self._slurp(claudeconf.skills_import(self.live_skills, []))
        self.assertNotIn("| orphan |", text)
        self.assertIn("orphan", text)          # noted, so it is not forgotten

    def test_the_seeded_table_round_trips_through_the_parser(self):
        target = self._skill(os.path.join("dev", "Alpha", ".claude", "skills", "alpha"))
        os.symlink(target, os.path.join(self.live_skills, "alpha"))
        path = claudeconf.skills_import(self.live_skills, [])
        from lib import claudeskills
        parsed = claudeskills.rows(path)
        self.assertEqual([row.skill for row in parsed], ["alpha"])

    def test_skills_state_says_what_to_run_when_there_is_no_table(self):
        lines, faults = claudeconf.skills_state(dry_run=True)
        self.assertEqual(faults, [])
        self.assertIn("--import", " ".join(lines))

    def test_import_preserves_the_not_global_section_verbatim(self):
        """Important 2 in the 2026-09-03 review: `--import` used to discard this section
        with no `--force` and no warning whenever the row count did not shrink -- 28 rows in,
        28 rows out is exactly the case the shrink guard cannot see. Its reasons are
        hand-authored and mandatory per spec; nothing here can be regenerated from a link
        scan, so it must survive untouched."""
        path = os.path.join(self.root, "claude", "skills.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                "| Skill | Target | Repo | Machines |\n|-|-|-|-|\n"
                "| alpha | ~/dev/Alpha | Alpha | all |\n"
                "\n## Not global\n\n"
                "- `RimMaster/skills/*` — 26 game-modding skills, scoped by design.\n"
            )
        target = self._skill(os.path.join("dev", "Alpha", ".claude", "skills", "alpha"))
        os.symlink(target, os.path.join(self.live_skills, "alpha"))

        text = self._slurp(claudeconf.skills_import(self.live_skills, []))
        self.assertIn("## Not global", text)
        self.assertIn("RimMaster/skills/*", text)
        self.assertIn("scoped by design", text)

    def test_import_with_no_existing_not_global_section_behaves_as_before(self):
        target = self._skill(os.path.join("dev", "Alpha", ".claude", "skills", "alpha"))
        os.symlink(target, os.path.join(self.live_skills, "alpha"))
        text = self._slurp(claudeconf.skills_import(self.live_skills, []))
        self.assertNotIn("## Not global", text)


class TestConvergeNeverRaises(ConfTest):
    """converge_claudemd's docstring promises "never raises". It caught only SystemExit.

    The consequence was total: sync.py calls it unguarded, so a single non-UTF-8 byte in
    ~/.claude/CLAUDE.md killed `sync.main` before any repo was swept — no SYNC.md, no
    heartbeat, no status file — every 127 s forever. And because SYNC.md is the documented
    liveness signal, its timestamp simply froze at the old value rather than reporting the
    fault. Only the launchd .err log showed anything.
    """

    def test_a_non_utf8_byte_in_the_live_file_blocks_rather_than_raising(self):
        self.run_cmd("render")
        with open(claudeconf.LIVE_CLAUDE_MD, "wb") as handle:
            handle.write(b"# Machine\n\n\xff\xfe not utf-8\n")
        outcome, detail = claudeconf.converge_claudemd()
        self.assertEqual(outcome, claudeconf.BLOCKED)
        self.assertTrue(detail)

    def test_a_non_utf8_byte_in_a_section_blocks_rather_than_raising(self):
        with open(os.path.join(self.globals, "10-first.md"), "wb") as handle:
            handle.write(b"# First\n\n\xff\xfe\n")
        outcome, _ = claudeconf.converge_claudemd()
        self.assertEqual(outcome, claudeconf.BLOCKED)

    def test_a_directory_where_a_section_should_be_blocks_rather_than_raising(self):
        os.makedirs(os.path.join(self.globals, "50-notes.md"))
        outcome, _ = claudeconf.converge_claudemd()
        self.assertEqual(outcome, claudeconf.BLOCKED)


class TestBlocksLoseNothing(ConfTest):
    """_blocks only emitted text from the first '# ' heading onward.

    Everything above it vanished, so capture_diff reported "nothing new", cmd_adopt marked the
    capture adopted, and finish_adopt then rendered over the live file — destroying the content
    for real. That is exactly the outcome D18 calls the wrong one: the bytes survive in git
    while the file that actually takes effect silently reverted.
    """

    def _slurp(self, path):
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_content_above_the_first_heading_is_offered_as_a_chunk(self):
        self.run_cmd("render")
        claudeconf.converge_claudemd()
        original = self.live()
        with open(claudeconf.LIVE_CLAUDE_MD, "w", encoding="utf-8") as handle:
            handle.write(original.replace("# Machine", "A loose note.\n\n# Machine", 1))
        claudeconf.converge_claudemd()
        capture = claudeconf.pending_captures()[0]
        chunks = claudeconf.capture_diff(self._slurp(capture), claudeconf.render_text())
        self.assertTrue(any("A loose note." in chunk for chunk in chunks),
                        "pre-heading content was dropped; adopt would then destroy it")

    def test_a_file_with_no_heading_at_all_still_yields_its_content(self):
        blocks = claudeconf._blocks("just prose, no headings\n")
        self.assertTrue(blocks)
        self.assertIn("just prose", "".join(blocks))

    def test_an_unbalanced_fence_before_the_first_heading_loses_nothing(self):
        blocks = claudeconf._blocks("```\nopened and never closed\n\n# Machine\n\nx\n")
        self.assertIn("opened and never closed", "".join(blocks))

    def test_no_non_blank_line_is_ever_dropped(self):
        text = ("preamble line\n\n# One\n\nbody one\n\n```sh\n# not a heading\n```\n\n"
                "# Two\n\nbody two\n")
        joined = "".join(claudeconf._blocks(text))
        for line in text.split("\n"):
            if line.strip():
                self.assertIn(line.strip(), joined, "dropped: %r" % line)

    def test_crlf_alone_does_not_make_every_section_look_new(self):
        """A file differing only in line endings reported every block as unclassified —
        following adopt's instructions then duplicated the whole ritual into a section."""
        expected = claudeconf.render_text()
        crlf = expected.replace("\n", "\r\n")
        self.assertEqual(claudeconf.capture_diff(crlf, expected), [])


class TestFinishAdoptCapturesFirst(ConfTest):
    def test_an_edit_made_after_the_last_capture_is_not_lost(self):
        """finish_adopt rendered over the live file using only the captures as evidence.

        Anything written in the window since the last sweep — up to 127 s, or any length if a
        `#` memory lands while a session runs adopt — was destroyed with no copy anywhere.
        """
        self.run_cmd("render")
        claudeconf.converge_claudemd()
        with open(claudeconf.LIVE_CLAUDE_MD, "a", encoding="utf-8") as handle:
            handle.write("\n# Late\n\nArrived after the capture.\n")
        claudeconf.finish_adopt()
        saved = []
        for base in (claudeconf.adopt_dir(), os.path.join(claudeconf.adopt_dir(), "done")):
            if not os.path.isdir(base):
                continue
            for name in sorted(os.listdir(base)):
                path = os.path.join(base, name)
                if os.path.isfile(path):
                    with open(path, encoding="utf-8") as handle:
                        saved.append(handle.read())
        self.assertIn("Arrived after the capture.", "".join(saved),
                      "finish_adopt overwrote the live file without preserving a late edit")


class TestAdoptLeavesNoResidue(ConfTest):
    def test_finish_adopt_does_not_recapture_what_it_just_adopted(self):
        """Otherwise "captures awaiting classification" becomes a flag nobody can ever clear.

        finish_adopt now re-captures before rendering, to close a window where a late edit was
        destroyed. That made the I2 idempotence check matter across `done/` too: it only looked
        at pending captures, so the instant mark_adopted moved the file, the content looked new
        again and a fresh pending capture appeared on every adopt.
        """
        self.run_cmd("render")
        claudeconf.converge_claudemd()
        with open(claudeconf.LIVE_CLAUDE_MD, "a", encoding="utf-8") as handle:
            handle.write("\n# Remembered\n\nA rule.\n")
        claudeconf.converge_claudemd()
        capture = claudeconf.pending_captures()[0]
        with open(capture, encoding="utf-8") as handle:
            chunks = claudeconf.capture_diff(handle.read(), claudeconf.render_text())
        for chunk in chunks:
            claudeconf.file_chunk(chunk, "40-adopted.md")
        claudeconf.mark_adopted(capture)
        claudeconf.finish_adopt()
        self.assertEqual(claudeconf.pending_captures(), [],
                         "adopt left a capture behind, so the queue can never drain")
        outcome, _ = claudeconf.converge_claudemd()
        self.assertEqual(outcome, claudeconf.CURRENT)


class TestSkillsFaultsAreReported(ConfTest):
    def setUp(self):
        super(TestSkillsFaultsAreReported, self).setUp()
        self.live_skills = os.path.join(self.home, "skills")
        os.makedirs(self.live_skills)

    def _table(self, target, repo=""):
        from lib import claudeskills
        path = os.path.join(self.root, "claude", "skills.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("| Skill | Target | Repo | Machines |\n|-|-|-|-|\n"
                         "| alpha | %s | %s | all |\n" % (target, repo))
        return claudeskills.rows(path)

    def test_a_real_directory_occupying_a_link_name_is_a_fault(self):
        """It was reported nowhere: not in lines, not in faults. A declared skill silently
        did not link, which is the exact invisible capability loss this module exists to end."""
        target = os.path.join(self.tmp, "src")
        os.makedirs(target)
        with open(os.path.join(target, "SKILL.md"), "w", encoding="utf-8") as handle:
            handle.write("---\nname: alpha\ndescription: d\n---\n\nBody.\n")
        os.makedirs(os.path.join(self.live_skills, "alpha"))
        self._table(target)
        _, faults = claudeconf.skills_state(dry_run=True)
        self.assertTrue(faults, "an unmanaged declared skill produced no fault")

    def test_a_skill_whose_repo_is_absent_is_a_fault_not_a_success(self):
        """It reported SYNCED while a declared capability was entirely absent."""
        self._table(os.path.join(self.tmp, "not-there"), repo="Alpha")
        _, faults = claudeconf.skills_state(dry_run=True)
        self.assertTrue(faults, "an absent skill repo produced no fault")


class TestSkillsImportGuard(ConfTest):
    def setUp(self):
        super(TestSkillsImportGuard, self).setUp()
        self.live_skills = os.path.join(self.home, "skills")
        os.makedirs(self.live_skills)

    def test_import_refuses_to_shrink_a_curated_table(self):
        """The table is the one hand-curated, fleet-wide, non-derivable artifact here.

        `--import` rewrote it wholesale from the local link set with no guard, so running it on
        a machine with no links replaced 21 curated rows with zero — and the resulting sweep
        message then told the next session to run the same command again.
        """
        from lib import claudeskills
        path = os.path.join(self.root, "claude", "skills.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rows = "\n".join("| s%d | ~/dev/X/s%d |  | all |" % (i, i) for i in range(21))
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("| Skill | Target | Repo | Machines |\n|-|-|-|-|\n" + rows + "\n")
        with self.assertRaises(SystemExit):
            claudeconf.skills_import(self.live_skills, [])
        self.assertEqual(len(claudeskills.rows(path)), 21, "the curated table was destroyed")

    def test_force_allows_it(self):
        from lib import claudeskills
        path = os.path.join(self.root, "claude", "skills.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("| Skill | Target | Repo | Machines |\n|-|-|-|-|\n"
                         "| s0 | ~/dev/X/s0 |  | all |\n")
        claudeconf.skills_import(self.live_skills, [], force=True)
        self.assertEqual(claudeskills.rows(path), [])


class TestSkillsStateProvenanceGlue(ConfTest):
    """The glue in claudeconf.skills_state that would have caught Important 1.

    `record_created` had zero production call sites before this review: links were created
    without ever being recorded, so on a fresh machine (`~/.claude/skills` absent) the owned
    set stayed empty forever and every removed row's link became `undeclared` -- reported,
    never pruned. These tests exercise `skills_state` itself rather than `claudeskills.apply`
    in isolation, because the missing wiring lived in the glue between them, not in either
    module alone.

    `links_state_file` and `prune_state_file` are patched to a tmpdir path for every test in
    this class: `skills_state` reads/writes provenance and prune-rate state at their real,
    machine-local default locations (`~/dev/.lodestar/...`) whenever no path is threaded
    through, and this class deliberately provokes writes to that state (create, prune) that
    must never land on the real machine running this suite.
    """

    def setUp(self):
        super(TestSkillsStateProvenanceGlue, self).setUp()
        self.live_skills = os.path.join(self.home, "skills")
        os.makedirs(self.live_skills)
        self.table_path = os.path.join(self.root, "claude", "skills.md")
        os.makedirs(os.path.dirname(self.table_path), exist_ok=True)

        from lib import claudeskills
        self.claudeskills = claudeskills
        self.links_state = os.path.join(self.tmp, "skill-links.json")
        self.prune_state = os.path.join(self.tmp, "skill-prunes.json")
        patch_links = mock.patch.object(claudeskills, "links_state_file",
                                        return_value=self.links_state)
        patch_prunes = mock.patch.object(claudeskills, "prune_state_file",
                                         return_value=self.prune_state)
        patch_links.start()
        patch_prunes.start()
        self.addCleanup(patch_links.stop)
        self.addCleanup(patch_prunes.stop)

    def _write_table(self, rows_text):
        with open(self.table_path, "w", encoding="utf-8") as handle:
            handle.write("| Skill | Target | Repo | Machines |\n|-|-|-|-|\n" + rows_text)

    def _skill(self, relative, name="alpha"):
        target = os.path.join(self.tmp, relative)
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, "SKILL.md"), "w", encoding="utf-8") as handle:
            handle.write("---\nname: %s\ndescription: d\n---\n\nBody.\n" % name)
        return target

    def test_a_successful_create_records_provenance(self):
        """The heart of Important 1: apply()'s create path must feed record_created."""
        target = self._skill("dev/Alpha")
        self._write_table("| alpha | %s | Alpha | all |\n"
                          % target.replace(os.path.expanduser("~"), "~", 1))
        claudeconf.skills_state(dry_run=False)
        self.assertIn("alpha", self.claudeskills.created_links())

    def test_dry_run_records_no_provenance(self):
        """dry_run must stay fully inert: no create, so no provenance write either."""
        target = self._skill("dev/Alpha")
        self._write_table("| alpha | %s | Alpha | all |\n"
                          % target.replace(os.path.expanduser("~"), "~", 1))
        claudeconf.skills_state(dry_run=True)
        self.assertEqual(self.claudeskills.created_links(), set())
        self.assertFalse(os.path.exists(self.links_state))

    def test_undeclared_line_is_emitted_for_an_unowned_stray_link(self):
        target = self._skill("dev/Alpha")
        stray = self._skill("dev/Stray", name="stray")
        self._write_table("| alpha | %s | Alpha | all |\n"
                          % target.replace(os.path.expanduser("~"), "~", 1))
        os.symlink(stray, os.path.join(self.live_skills, "stray"))

        # The stray link lives under a tmpdir, not the real ~/dev, so the location guard
        # (a second, independent check -- see plan()'s docstring) would otherwise classify
        # it `unmanaged` rather than `undeclared`. That guard is exercised directly in
        # test_claudeskills.py; here the point is the `undeclared` reporting path itself.
        with mock.patch.object(self.claudeskills, "_prunable", return_value=True):
            lines, _faults = claudeconf.skills_state(dry_run=True)

        self.assertTrue(any("undeclared" in line for line in lines),
                        "an unowned stray link must be reported, not silently ignored: %r"
                        % lines)

    def test_linked_and_valid_count_reports_alongside_other_lines(self):
        """Item 4: `if settled and not lines` used to suppress this count as soon as any
        other line existed -- including the new `undeclared` line right above it."""
        target = self._skill("dev/Alpha")
        stray = self._skill("dev/Stray", name="stray")
        self._write_table("| alpha | %s | Alpha | all |\n"
                          % target.replace(os.path.expanduser("~"), "~", 1))
        os.symlink(target, os.path.join(self.live_skills, "alpha"))       # already linked
        os.symlink(stray, os.path.join(self.live_skills, "stray"))        # undeclared

        with mock.patch.object(self.claudeskills, "_prunable", return_value=True):
            lines, _faults = claudeconf.skills_state(dry_run=True)

        self.assertTrue(any("undeclared" in line for line in lines))
        self.assertTrue(any("linked and valid" in line for line in lines),
                        "the undeclared line suppressed the healthy-count line: %r" % lines)

    def test_record_removal_both_journals_and_drops_provenance(self):
        """`_record_removal` is a closure inside skills_state and cannot be called directly,
        so this drives a real prune through skills_state and checks both of its effects."""
        target = self._skill("dev/Alpha")
        stray = self._skill("dev/Stray", name="stray")
        self._write_table("| alpha | %s | Alpha | all |\n"
                          % target.replace(os.path.expanduser("~"), "~", 1))
        os.symlink(stray, os.path.join(self.live_skills, "stray"))
        self.claudeskills.record_created(["stray"], self.links_state)

        seen = []
        with mock.patch.object(self.claudeskills, "_prunable", return_value=True), \
             mock.patch.object(self.claudeskills, "journal_prune",
                               side_effect=lambda s, t: seen.append((s, t))):
            claudeconf.skills_state(dry_run=False)

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], "stray")
        self.assertFalse(os.path.lexists(os.path.join(self.live_skills, "stray")),
                         "the stray link should have been pruned")
        self.assertNotIn("stray", self.claudeskills.created_links(),
                         "provenance must be dropped after a successful prune")

    def test_owned_set_reaches_both_plan_calls_around_a_clone(self):
        """The re-plan after cloning must see the SAME provenance the first plan saw.

        Regression shape for Important 1: if the re-plan ever passed a different `created`
        (or dropped it, e.g. back to None) an undeclared link's fate could flip between the
        two plan() calls inside one sweep.
        """
        from lib import registry, skillclone
        self._write_table("| alpha | ~/dev/Alpha | Alpha | all |\n")
        self.claudeskills.record_created(["alpha"], self.links_state)

        seen_created = []

        def fake_plan(table, key, live_dir, registered, created=None):
            seen_created.append(created)
            return [self.claudeskills.Action("clone", "alpha", "not present; clone Alpha")]

        fake_entry = registry.Entry(name="Alpha", path=os.path.join(self.tmp, "dev", "Alpha"),
                                    branch="", sync=True, notes="")
        with mock.patch.object(self.claudeskills, "plan", side_effect=fake_plan), \
             mock.patch.object(registry, "participating", return_value=[fake_entry]), \
             mock.patch.object(skillclone, "read_remotes", return_value={}), \
             mock.patch.object(skillclone, "clone_missing", return_value=[]):
            claudeconf.skills_state(dry_run=False, clone=True)

        self.assertEqual(len(seen_created), 2,
                         "expected one plan() call before cloning and a re-plan after")
        self.assertEqual(seen_created[0], seen_created[1],
                         "the two plan() calls in one sweep saw different provenance: %r"
                         % seen_created)
        self.assertEqual(seen_created[0], {"alpha"})




class TestMemoryGoesToPrivateOnAWorkMac(ConfTest):
    """Ruling 5: a work Mac's auto-memory snapshot goes to the private store, never the repo."""

    def setUp(self):
        super().setUp()
        claudeconf.memory_snapshot_dir = self._saved_memdir  # the real rule
        from tests.test_reckoning import make_checkout
        self.store = make_checkout(os.path.join(self.tmp, "store"), "git@git.private.example:x/p.git")
        self._jp = os.environ.get("LODESTAR_PRIVATE_ROOT")
        os.environ["LODESTAR_PRIVATE_ROOT"] = self.store
        self.addCleanup(lambda: os.environ.__setitem__("LODESTAR_PRIVATE_ROOT", self._jp))
        directory = os.path.join(self.home, "projects", "-proj-a", "memory")
        os.makedirs(directory)
        with open(os.path.join(directory, "MEMORY.md"), "w") as handle:
            handle.write("x\n")

    def test_snapshot_lands_in_the_store(self):
        claudeconf.snapshot_all()
        self.assertFalse(os.path.exists(os.path.join(claudeconf.snapshot_dir(), "memory")))
        stored = os.path.join(self.store, "projects", "board", "claude-snapshot", "test-box",
                              "memory", "-proj-a", "MEMORY.md")
        self.assertTrue(os.path.exists(stored))

    def test_no_store_withholds_rather_than_writing_the_repo(self):
        os.environ["LODESTAR_PRIVATE_ROOT"] = os.path.join(self.tmp, "absent")
        _, files, _ = claudeconf.snapshot_all()
        self.assertEqual(files, -1)
        self.assertFalse(os.path.exists(os.path.join(claudeconf.snapshot_dir(), "memory")))

if __name__ == "__main__":
    unittest.main()
