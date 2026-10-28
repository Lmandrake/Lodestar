"""Tests for lib/conversations.py -- rendering a session transcript into a project record.

The live `~/.claude` is never touched: CLAUDE_HOME is redirected into a temp directory, the
same way tests/test_claudesettings.py does it. This module writes files, so a leaked path
here would scribble into a real project.

Stdlib only and Python 3.9-compatible, matching run_tests.py's constraint (launchd invokes
/usr/bin/python3, 3.9.6 on this machine).
"""

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from lib import conversations, gitutil, prosehome
from tests import needs_private  # noqa: E402


def _line(**fields):
    return json.dumps(fields)


class ConversationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-conv-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.claude = os.path.join(self.tmp, "claude-home")
        os.makedirs(self.claude)
        self._old_env = {k: os.environ.get(k)
                         for k in ("CLAUDE_HOME", "LODESTAR_PRIVATE_ROOT", "LODESTAR_MACHINE")}
        os.environ["CLAUDE_HOME"] = self.claude
        # The legacy in-repo behaviour, as on tower: no private checkout and no private access.
        # `TestPrivatePlacement` below covers a work Mac.
        os.environ["LODESTAR_PRIVATE_ROOT"] = os.path.join(self.tmp, "no-private-store")
        os.environ["LODESTAR_MACHINE"] = "tower"
        prosehome.reset_cache()
        self.addCleanup(self._restore_home)

    def _restore_home(self):
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        prosehome.reset_cache()

    def transcript(self, lines, name="session.jsonl"):
        path = os.path.join(self.tmp, name)
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        return path

    def git_repo(self, name="proj"):
        root = os.path.join(self.tmp, name)
        os.makedirs(root)
        subprocess.run(["git", "init", "-q", "-b", "main", root],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return os.path.realpath(root)

    def simple(self, cwd, **extra):
        """A minimal but realistic two-turn transcript."""
        lines = [
            _line(type="permission-mode", permissionMode="default", sessionId="abcd1234-ef"),
            _line(type="user", cwd=cwd, gitBranch="main", sessionId="abcd1234-ef",
                  timestamp="2026-10-04T09:15:30.000Z",
                  message={"role": "user", "content": "please fix the parser"}),
            _line(type="assistant", cwd=cwd, sessionId="abcd1234-ef",
                  timestamp="2026-10-04T09:15:40.000Z",
                  message={"role": "assistant", "content": [
                      {"type": "thinking", "thinking": "SECRET REASONING"},
                      {"type": "text", "text": "On it."},
                      {"type": "tool_use", "name": "Read",
                       "input": {"file_path": "/x/parser.py"}},
                  ]}),
            _line(type="user", cwd=cwd, sessionId="abcd1234-ef",
                  message={"role": "user", "content": [
                      {"type": "tool_result", "content": "WHOLE FILE CONTENTS"}]}),
            _line(type="assistant", cwd=cwd, sessionId="abcd1234-ef",
                  message={"role": "assistant", "content": [
                      {"type": "text", "text": "Fixed it."}]}),
        ]
        lines.extend(extra.get("tail", []))
        return self.transcript(lines)


class TestNoClock(ConversationTest):
    """The one hard invariant: the render is a pure function of the transcript."""

    def test_rendering_twice_produces_identical_bytes(self):
        root = self.git_repo()
        path = self.simple(root)
        first = conversations.render(path)
        second = conversations.render(path)
        self.assertEqual(first, second)

    def test_the_render_contains_no_date_other_than_the_transcripts_own(self):
        # A "generated at <now>" line would dirty the file every sweep and commit it
        # hourly for ever. Only the transcript's own 2026-10-04 may appear.
        root = self.git_repo()
        text = conversations.render(self.simple(root))
        import re
        for found in re.findall(r"\d{4}-\d{2}-\d{2}", text):
            self.assertEqual(found, "2026-10-04")

    @needs_private("machines.md")
    def test_writing_twice_reports_unchanged_the_second_time(self):
        root = self.git_repo()
        path = self.simple(root)
        target, changed = conversations.write_record(root, path)
        self.assertTrue(changed)
        again, changed_again = conversations.write_record(root, path)
        self.assertEqual(target, again)
        self.assertFalse(changed_again, "a second write must be a no-op, or the sweep "
                                        "commits this file on every hourly pass for ever")

    def test_an_undated_transcript_does_not_borrow_todays_date(self):
        root = self.git_repo()
        path = self.transcript([
            _line(type="user", cwd=root, sessionId="nodate01",
                  message={"role": "user", "content": "hello"})])
        self.assertIn("undated", conversations.record_name(path))


class TestWhatIsKept(ConversationTest):
    def test_dialog_survives_and_reasoning_and_tool_output_do_not(self):
        root = self.git_repo()
        text = conversations.render(self.simple(root))
        self.assertIn("please fix the parser", text)
        self.assertIn("On it.", text)
        self.assertIn("Fixed it.", text)
        self.assertNotIn("SECRET REASONING", text)
        self.assertNotIn("WHOLE FILE CONTENTS", text)

    def test_a_tool_call_becomes_one_action_line(self):
        root = self.git_repo()
        text = conversations.render(self.simple(root))
        self.assertIn("Read — /x/parser.py", text)

    def test_the_speaker_is_named(self):
        root = self.git_repo()
        self.assertIn("## Lukas", conversations.render(self.simple(root)))

    def test_system_reminders_are_stripped(self):
        root = self.git_repo()
        path = self.transcript([
            _line(type="user", cwd=root, sessionId="sr01",
                  message={"role": "user", "content":
                           "real question<system-reminder>INJECTED NOISE</system-reminder>"})])
        text = conversations.render(path)
        self.assertIn("real question", text)
        self.assertNotIn("INJECTED NOISE", text)

    def test_sidechain_and_meta_records_are_dropped(self):
        root = self.git_repo()
        path = self.transcript([
            _line(type="user", cwd=root, sessionId="sc01", isSidechain=True,
                  message={"role": "user", "content": "SUBAGENT CHATTER"}),
            _line(type="user", cwd=root, sessionId="sc01", isMeta=True,
                  message={"role": "user", "content": "HARNESS BOOKKEEPING"}),
            _line(type="user", cwd=root, sessionId="sc01",
                  message={"role": "user", "content": "the real turn"}),
        ])
        text = conversations.render(path)
        self.assertIn("the real turn", text)
        self.assertNotIn("SUBAGENT CHATTER", text)
        self.assertNotIn("HARNESS BOOKKEEPING", text)

    def test_a_slash_command_collapses_and_its_output_is_dropped(self):
        root = self.git_repo()
        path = self.transcript([
            _line(type="user", cwd=root, sessionId="cmd01",
                  message={"role": "user", "content":
                           "<command-name>/model</command-name><command-args></command-args>"}),
            _line(type="user", cwd=root, sessionId="cmd01",
                  message={"role": "user", "content":
                           "<local-command-stdout>Set model to X</local-command-stdout>"}),
        ])
        text = conversations.render(path)
        self.assertIn("_(ran /model)_", text)
        self.assertNotIn("Set model to X", text)

    def test_consecutive_assistant_text_merges_into_one_reply(self):
        root = self.git_repo()
        path = self.transcript([
            _line(type="assistant", cwd=root, sessionId="m01",
                  message={"role": "assistant", "content": [{"type": "text", "text": "one"}]}),
            _line(type="assistant", cwd=root, sessionId="m01",
                  message={"role": "assistant", "content": [{"type": "text", "text": "two"}]}),
        ])
        self.assertEqual(conversations.render(path).count("## Claude"), 1)

    def test_text_after_a_tool_call_starts_a_new_block(self):
        """Measured: merging through a tool call piled 149 actions into one block.

        ACTION_CAP then hid 141 of them and the survivors sat detached from the prose
        that motivated them. The record has to keep the said/did/said rhythm.
        """
        root = self.git_repo()
        path = self.transcript([
            _line(type="assistant", cwd=root, sessionId="rh01",
                  message={"role": "assistant", "content": [
                      {"type": "text", "text": "first I look"},
                      {"type": "tool_use", "name": "Read", "input": {"file_path": "/a"}}]}),
            _line(type="assistant", cwd=root, sessionId="rh01",
                  message={"role": "assistant", "content": [
                      {"type": "text", "text": "now I report"},
                      {"type": "tool_use", "name": "Read", "input": {"file_path": "/b"}}]}),
        ])
        blocks = conversations.turns(path)
        self.assertEqual(len(blocks), 2)
        self.assertEqual([len(b.actions) for b in blocks], [1, 1],
                         "each action must stay with the reply that made it")

    def test_an_api_error_message_is_not_rendered_as_speech(self):
        root = self.git_repo()
        path = self.transcript([
            _line(type="assistant", cwd=root, sessionId="ae01", isApiErrorMessage=True,
                  message={"role": "assistant", "content": [
                      {"type": "text", "text": "API Error: Connection lost mid-response."}]}),
            _line(type="assistant", cwd=root, sessionId="ae01",
                  message={"role": "assistant", "content": [
                      {"type": "text", "text": "real reply"}]}),
        ])
        text = conversations.render(path)
        self.assertIn("real reply", text)
        self.assertNotIn("API Error", text)

    def test_a_truncated_final_line_is_tolerated(self):
        # The normal case for a SessionEnd hook: the transcript is still being flushed.
        root = self.git_repo()
        path = os.path.join(self.tmp, "partial.jsonl")
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write(_line(type="user", cwd=root, sessionId="t01",
                               message={"role": "user", "content": "complete turn"}) + "\n")
            handle.write('{"type":"assistant","message":{"content":[{"type":"te')
        text = conversations.render(path)
        self.assertIn("complete turn", text)


class TestAttribution(ConversationTest):
    def test_a_repo_session_is_attributed_to_its_root(self):
        root = self.git_repo()
        where = conversations.attribute(self.simple(root))
        self.assertEqual(where.kind, "repo")
        self.assertEqual(os.path.realpath(where.root or ""), root)

    def test_a_subdirectory_cwd_resolves_to_the_repo_root(self):
        root = self.git_repo()
        sub = os.path.join(root, "bin")
        os.makedirs(sub)
        where = conversations.attribute(self.simple(sub))
        self.assertEqual(os.path.realpath(where.root or ""), root)

    def test_a_cwd_that_moves_mid_session_still_finds_the_repo(self):
        # Measured in a real transcript: both /Lodestar and /Lodestar/bin appear.
        root = self.git_repo()
        sub = os.path.join(root, "bin")
        os.makedirs(sub)
        path = self.transcript([
            _line(type="user", cwd=sub, sessionId="mv01",
                  message={"role": "user", "content": "first"}),
            _line(type="user", cwd=root, sessionId="mv01",
                  message={"role": "user", "content": "second"}),
        ])
        self.assertEqual(os.path.realpath(conversations.attribute(path).root or ""), root)

    def test_a_non_repo_path_is_skipped_rather_than_guessed(self):
        plain = os.path.join(self.tmp, "not-a-repo")
        os.makedirs(plain)
        where = conversations.attribute(self.simple(plain))
        self.assertEqual(where.kind, "unrooted")
        self.assertIsNone(where.root)

    def test_a_transcript_with_no_cwd_is_unrooted(self):
        path = self.transcript([
            _line(type="user", sessionId="nc01",
                  message={"role": "user", "content": "hi"})])
        self.assertEqual(conversations.attribute(path).kind, "unrooted")

    def test_two_project_paths_that_mangle_alike_do_not_cross_file(self):
        """`my-repo` and `my_repo` mangle to one directory name; cwd disambiguates.

        This is why attribution reads the records' own `cwd` instead of the directory
        name: the mangling replaces every non-alphanumeric character, so a record keyed
        off the dirname would land in whichever project was looked up first.
        """
        dashed = self.git_repo("my-repo")
        scored = self.git_repo("my_repo")
        self.assertEqual(conversations.transcript_dir(dashed),
                         conversations.transcript_dir(scored),
                         "precondition: these must collide, or the test proves nothing")
        self.assertEqual(
            os.path.realpath(conversations.attribute(
                self.simple(dashed, )).root or ""), dashed)

    def test_a_renamed_repos_history_is_attributed_to_its_current_root(self):
        """Measured 2026-10-04: all 30 of RimMandrake's transcripts carried `cwd` under
        `~/dev/RimMaster`, the pre-rename name, so its entire 231 MB history attributed to
        nothing. The registry's `Former` column exists for this; before the fix the busiest
        repo on the machine reported zero records and nothing said why.

        Uses the real `registry.Entry` type so a field rename is caught here, while
        substituting the rows so the test never depends on the live registry's contents.
        """
        from lib import registry

        current = self.git_repo("NewName")
        old = os.path.join(os.path.dirname(current), "OldName")
        row = registry.Entry(name="NewName", path=current, branch="main", sync=True,
                             notes="", send=True, receive=True, machines=(),
                             repo_id="newname",
                             former=("OldName", "git@github.com:x/OldName.git"),
                             remote="git@github.com:x/NewName.git", state="active")
        original = conversations.registry.entries
        conversations.registry.entries = lambda *a, **k: [row]
        self.addCleanup(setattr, conversations.registry, "entries", original)

        self.assertFalse(os.path.isdir(old), "precondition: the old path must be gone")
        where = conversations.attribute(self.simple(os.path.join(old, "src")))
        self.assertEqual(where.kind, "repo")
        self.assertEqual(os.path.realpath(where.root or ""), current)
        self.assertIn("renamed", where.reason)

    def test_a_former_remote_url_is_not_mistaken_for_a_directory_name(self):
        """`Former` mixes prior names and prior remote URLs in one cell."""
        self.assertTrue(conversations._is_former_name("RimMaster"))
        self.assertFalse(conversations._is_former_name("git@github.com:x/RimMaster.git"))
        self.assertFalse(conversations._is_former_name("https://github.com/x/y.git"))

    def test_a_real_repo_at_the_old_path_wins_over_the_rename(self):
        """A rename is consulted only after a live repo lookup fails."""
        from lib import registry

        current = self.git_repo("NewName2")
        old = self.git_repo("OldName2")       # someone re-created the old name
        row = registry.Entry(name="NewName2", path=current, branch="main", sync=True,
                             notes="", send=True, receive=True, machines=(),
                             repo_id="newname2", former=("OldName2",),
                             remote="", state="active")
        original = conversations.registry.entries
        conversations.registry.entries = lambda *a, **k: [row]
        self.addCleanup(setattr, conversations.registry, "entries", original)

        where = conversations.attribute(self.simple(old))
        self.assertEqual(os.path.realpath(where.root or ""), old,
                         "a directory that is a repo today owns its own sessions")

    def test_an_unreadable_registry_degrades_to_unrooted_rather_than_raising(self):
        def boom(*args, **kwargs):
            raise RuntimeError("registry unavailable")

        original = conversations.registry.entries
        conversations.registry.entries = boom
        self.addCleanup(setattr, conversations.registry, "entries", original)
        plain = os.path.join(self.tmp, "nowhere")
        os.makedirs(plain)
        self.assertEqual(conversations.attribute(self.simple(plain)).kind, "unrooted")

    def test_sessions_for_filters_out_another_projects_transcript(self):
        mine = self.git_repo("mine")
        theirs = self.git_repo("theirs")
        directory = conversations.transcript_dir(mine)
        os.makedirs(directory)
        for name, cwd in (("a.jsonl", mine), ("b.jsonl", theirs)):
            with io.open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
                handle.write(_line(type="user", cwd=cwd, sessionId=name[0],
                                   message={"role": "user", "content": "x"}) + "\n")
        kept = conversations.sessions_for(mine)
        self.assertEqual([os.path.basename(p) for p in kept], ["a.jsonl"])

    def test_subagent_transcripts_are_not_collected(self):
        root = self.git_repo()
        directory = conversations.transcript_dir(root)
        os.makedirs(os.path.join(directory, "subagents"))
        with io.open(os.path.join(directory, "subagents", "agent-x.jsonl"), "w",
                     encoding="utf-8") as handle:
            handle.write(_line(type="user", cwd=root, sessionId="ag",
                               message={"role": "user", "content": "delegated"}) + "\n")
        self.assertEqual(conversations.transcripts_for(root), [])


class TestDrive(ConversationTest):
    def test_a_drive_folder_is_recognised_by_prefix(self):
        fake_home = os.path.join(self.tmp, "home")
        drive = os.path.join(fake_home, "GDrive", "Project")
        os.makedirs(drive)
        old = os.environ.get("HOME")
        os.environ["HOME"] = fake_home
        try:
            self.assertTrue(conversations.is_drive_path(drive))
            self.assertFalse(conversations.is_drive_path(self.tmp))
        finally:
            if old is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old

    @needs_private("machines.md")
    def test_writing_into_a_drive_folder_creates_no_git_directory(self):
        folder = os.path.join(self.tmp, "drive-project")
        os.makedirs(folder)
        path = self.simple(folder)
        conversations.write_record(folder, path)
        self.assertFalse(os.path.exists(os.path.join(folder, ".git")),
                         "Drive replicating a .git directory corrupts it")
        self.assertTrue(os.path.isdir(os.path.join(folder, conversations.RECORD_DIR)))


class TestSweepClassification(ConversationTest):
    """The record must stay auto-committable, or nothing ever reaches the remote."""

    def test_a_record_is_sweepable_not_source(self):
        relative = "%s/2026-10-04-0915-abcd1234.md" % conversations.RECORD_DIR
        self.assertFalse(gitutil.is_source(relative),
                         "a source-classified record would need an authored commit and "
                         "would never be picked up by the hourly sweep")
        source, sweepable = gitutil.partition_paths([relative])
        self.assertEqual((source, sweepable), ([], [relative]))

    @needs_private("machines.md")
    def test_a_record_trips_neither_the_secret_nor_the_size_guard(self):
        root = self.git_repo()
        target, _ = conversations.write_record(root, self.simple(root))
        relative = os.path.relpath(target, root)
        problems = gitutil.offending_paths(root, candidates=[relative])
        self.assertEqual(problems, [],
                         "a flagged path aborts the WHOLE repo's sweep commit, not just "
                         "this file")


class TestRecordNaming(ConversationTest):
    def test_the_name_carries_the_transcripts_time_and_session(self):
        root = self.git_repo()
        self.assertEqual(conversations.record_name(self.simple(root)),
                         "2026-10-04-0915-abcd1234.md")

    @needs_private("machines.md")
    def test_the_path_lands_in_the_record_directory(self):
        root = self.git_repo()
        path = conversations.record_path(root, self.simple(root))
        self.assertEqual(os.path.dirname(path),
                         os.path.join(root, conversations.RECORD_DIR))


class TestPrivatePlacement(ConversationTest):
    """Ruling 5, 2026-10-13: on a work Mac a github.com repo's records go to the private host."""

    def setUp(self):
        super().setUp()
        from tests.test_reckoning import make_checkout
        self.store = os.path.realpath(make_checkout(os.path.join(self.tmp, "private-store"),
                                                  "git@git.private.example:x/private-store.git"))
        os.environ["LODESTAR_PRIVATE_ROOT"] = self.store
        os.environ["LODESTAR_MACHINE"] = "mt-002"
        prosehome.reset_cache()

    def github_repo(self, name="proj", url="git@github.com:x/proj.git"):
        root = self.git_repo(name)
        subprocess.run(["git", "-C", root, "remote", "add", "origin", url], check=True)
        return root

    def test_a_github_repo_record_lands_in_the_overlay_and_not_in_the_repo(self):
        root = self.github_repo()
        target, changed = conversations.write_record(root, self.simple(root))
        self.assertTrue(changed)
        self.assertEqual(os.path.dirname(target),
                         os.path.join(self.store, "projects", "proj", "conversations"))
        self.assertTrue(os.path.isfile(target))
        self.assertFalse(os.path.exists(os.path.join(root, conversations.RECORD_DIR)))

    def test_a_private_hosted_repo_keeps_its_records(self):
        root = self.github_repo("inner", "git@git.private.example:x/inner.git")
        target, _ = conversations.write_record(root, self.simple(root))
        self.assertEqual(os.path.dirname(target), os.path.join(root, conversations.RECORD_DIR))

    def test_a_work_mac_without_private_store_writes_nothing_and_raises(self):
        os.environ["LODESTAR_PRIVATE_ROOT"] = os.path.join(self.tmp, "gone")
        root = self.github_repo()
        with self.assertRaises(conversations.Withheld):
            conversations.write_record(root, self.simple(root))
        self.assertFalse(os.path.exists(os.path.join(root, conversations.RECORD_DIR)))

    def test_the_sweep_step_reports_a_withheld_repo_as_a_problem(self):
        os.environ["LODESTAR_PRIVATE_ROOT"] = os.path.join(self.tmp, "gone")
        root = self.github_repo()
        mangled = conversations.transcript_dir(root)
        os.makedirs(mangled)
        shutil.copy(self.simple(root), os.path.join(mangled, "s.jsonl"))
        import sync
        from lib import registry

        class Row:
            name, path = "proj", root
        result = sync.conversations_step([Row()], dry_run=False)
        self.assertEqual(result.outcome, sync.FLAGGED)
        self.assertIn("withheld", " ".join(result.actions))
        self.assertFalse(os.path.exists(os.path.join(root, conversations.RECORD_DIR)))


class TestReview(ConversationTest):
    """The pre-public review Lukas's ruling 3 relies on."""

    def cli(self):
        """`bin/conversations.py`, which shares its basename with `lib/conversations.py`.

        Both are importable and they are different modules — `bin/` is on `sys.path`, so a
        bare `import conversations` reaches the CLI while `from lib import conversations`
        reaches the library. Asserting they are distinct keeps a future refactor from
        quietly collapsing one into the other.
        """
        import conversations as cli
        self.assertIsNot(cli, conversations)
        return cli

    @needs_private("machines.md")
    def test_a_word_is_matched_on_its_boundaries_not_as_a_substring(self):
        """Measured: without \\b, "itar" matched inside "military" and produced 5 of 7
        hits on a real repo. A review that cries wolf is one the reader skips."""
        cli = self.cli()
        root = os.path.join(self.tmp, "reviewed")
        os.makedirs(os.path.join(root, conversations.RECORD_DIR))
        with io.open(os.path.join(root, conversations.RECORD_DIR, "r.md"), "w",
                     encoding="utf-8") as handle:
            handle.write("a military objective and an arbitrary choice\n")
        self.assertEqual(cli.cmd_review(root), 0,
                         "'military' must not match the word 'itar'")

    def test_a_real_standalone_term_is_still_reported(self):
        cli = self.cli()
        root = os.path.join(self.tmp, "reviewed2")
        os.makedirs(os.path.join(root, conversations.RECORD_DIR))
        with io.open(os.path.join(root, conversations.RECORD_DIR, "r.md"), "w",
                     encoding="utf-8") as handle:
            handle.write("this document is ITAR controlled\n")
        self.assertEqual(cli.cmd_review(root), 1,
                         "a standalone term must still be surfaced for review")

    @needs_private("machines.md")
    def test_a_repo_with_no_records_is_not_an_error(self):
        cli = self.cli()
        root = os.path.join(self.tmp, "bare")
        os.makedirs(root)
        self.assertEqual(cli.cmd_review(root), 0)


if __name__ == "__main__":
    unittest.main()
