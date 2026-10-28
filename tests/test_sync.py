"""Tests for bin/sync.py — the sweep, its guards, and the soft-lock.

Every repo here is a throwaway clone of a local bare remote, so pull/commit/push and
conflict-abort are exercised for real with no network and no risk to Lukas's repos.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

import sync as sync_cmd
from lib import clock, frontmatter, gitutil, paths, registry
from tests import needs_private  # noqa: E402

MACHINES = """| Key | Role |
|-----|------|
| test-box | testbox |
| other-box | otherbox |
"""


def git(repo, *args):
    subprocess.run(["git", "-C", repo] + list(args), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class SweepTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-sync-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self.root = os.path.join(self.tmp, "board")
        os.makedirs(os.path.join(self.root, "loops"))
        os.makedirs(os.path.join(self.root, "sync"))
        with open(os.path.join(self.root, "machines.md"), "w", encoding="utf-8") as handle:
            handle.write(MACHINES)

        self._saved = {
            k: os.environ.get(k)
            for k in ("LODESTAR_ROOT", "LODESTAR_MACHINE", "LODESTAR_RUNTIME", "HOME")
        }
        os.environ["LODESTAR_ROOT"] = self.root
        os.environ["LODESTAR_MACHINE"] = "test-box"
        os.environ["LODESTAR_RUNTIME"] = "cc"
        # sync-status.json lands under $HOME/dev/.lodestar; keep it in the sandbox.
        os.environ["HOME"] = self.tmp
        self.addCleanup(self._restore)

        stack = contextlib.ExitStack()
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.addCleanup(stack.close)

        self.repos = {}

    def _restore(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def make_repo(self, name, branch="main", seed=True, days_old=0):
        """A clone of a local bare remote.

        `days_old` backdates the seed commit, which is how a test asks for a warm or cold
        repo: sync.py derives the fetch tier from the newest commit's own timestamp.
        """
        remote = os.path.join(self.tmp, name + ".git")
        subprocess.run(["git", "init", "--bare", "-b", branch, remote], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        path = os.path.join(self.tmp, name)
        subprocess.run(["git", "clone", remote, path], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        git(path, "config", "user.email", "t@example.com")
        git(path, "config", "user.name", "T")
        git(path, "config", "commit.gpgsign", "false")
        if seed:
            self.write(path, "README.md", "seed\n")
            git(path, "add", "-A")
            env = dict(os.environ)
            if days_old:
                stamp = time.time() - days_old * 86400
                iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(stamp))
                env["GIT_AUTHOR_DATE"] = iso
                env["GIT_COMMITTER_DATE"] = iso
            subprocess.run(["git", "-C", path, "commit", "-m", "seed"], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
            git(path, "push", "-u", "origin", branch)
        self.repos[name] = (path, remote, branch)
        return path

    def push_from_elsewhere(self, name, filename="elsewhere.md"):
        """Land a commit on `name`'s bare remote from a second clone.

        Used to prove whether a sweep really contacted the remote: the file can only
        appear locally if the sweep fetched and then pulled.
        """
        other = os.path.join(self.tmp, name + "-other")
        subprocess.run(["git", "clone", self.repos[name][1], other], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        git(other, "config", "user.email", "o@example.com")
        git(other, "config", "user.name", "O")
        self.write(other, filename)
        git(other, "add", "-A")
        git(other, "commit", "-m", "elsewhere")
        git(other, "push")
        return other

    def set_fetch_state(self, ages):
        """Seed ~/dev/.lodestar/fetch-state.json with {repo name: seconds since fetch}."""
        target = paths.fetch_state_file()
        os.makedirs(os.path.dirname(target), exist_ok=True)
        now = time.time()
        payload = {"repos": {name: {"last_fetch": now - ago}
                             for name, ago in ages.items()}}
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return target

    def fetch_state(self):
        with open(paths.fetch_state_file(), encoding="utf-8") as handle:
            return json.load(handle)

    def actions(self, name):
        return self.status()["repos"][name]["actions"]

    def fetched(self, name):
        return any(action == "fetched" for action in self.actions(name))

    def write(self, repo, name, text="hello\n", age_seconds=0):
        path = os.path.join(repo, name)
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        if age_seconds:
            old = time.time() - age_seconds
            os.utime(path, (old, old))
        return path

    def set_registry(self, rows):
        lines = ["| Name | Path | Branch | Sync | Notes |",
                 "|------|------|--------|------|-------|"]
        for row in rows:
            lines.append("| %s | %s | %s | %s | %s |" % (
                row["name"], row["path"], row.get("branch", ""),
                row.get("sync", "on"), row.get("notes", "")))
        with open(os.path.join(self.root, "sync", "registry.md"), "w",
                  encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

    def register_all(self):
        self.set_registry([
            {"name": name, "path": path, "branch": branch}
            for name, (path, _, branch) in sorted(self.repos.items())
        ])

    def run_sweep(self, *argv):
        return sync_cmd.main(list(argv))

    def status(self):
        with open(paths.sync_status_file(), encoding="utf-8") as handle:
            return json.load(handle)

    def outcome(self, name):
        return self.status()["repos"][name]["outcome"]

    def detail(self, name):
        return self.status()["repos"][name]["detail"]

    def add_loop(self, loop_id, repo_path, owner, minutes_ago=1):
        stamp = clock.parse(clock.now())
        stamp = stamp.replace(minute=stamp.minute)  # keep the offset
        touch = "%s %s" % (
            clock.parse(clock.now()).isoformat(timespec="minutes"), owner)
        if minutes_ago:
            import datetime
            moved = clock.parse(clock.now()) - datetime.timedelta(minutes=minutes_ago)
            touch = "%s %s" % (moved.isoformat(timespec="minutes"), owner)
        frontmatter.write_file(
            os.path.join(self.root, "loops", loop_id + ".md"),
            {"id": loop_id, "class": "active", "state": "open", "repo": repo_path,
             "last_touch": touch},
            "",
        )


class TestCleanAndPush(SweepTest):
    def test_a_clean_repo_reports_clean(self):
        self.make_repo("alpha")
        self.register_all()
        self.assertEqual(self.run_sweep(), 0)
        self.assertEqual(self.outcome("alpha"), sync_cmd.CLEAN)

    def test_commits_and_pushes_a_quiescent_change(self):
        path = self.make_repo("alpha")
        self.write(path, "notes.md", "work\n", age_seconds=3600)
        self.register_all()
        self.run_sweep()
        self.assertEqual(self.outcome("alpha"), sync_cmd.SYNCED)
        self.assertEqual(gitutil.ahead_behind(path), (0, 0))
        self.assertFalse(gitutil.is_dirty(path))

    def test_the_commit_message_names_the_machine(self):
        path = self.make_repo("alpha")
        self.write(path, "notes.md", "work\n", age_seconds=3600)
        self.register_all()
        self.run_sweep()
        code, out, _ = gitutil.run(path, "log", "-1", "--format=%s")
        self.assertTrue(out.startswith("chore(sync): testbox "), out)

    def test_pulls_remote_work(self):
        path = self.make_repo("alpha")
        other = os.path.join(self.tmp, "alpha-other")
        subprocess.run(["git", "clone", self.repos["alpha"][1], other], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        git(other, "config", "user.email", "o@example.com")
        git(other, "config", "user.name", "O")
        self.write(other, "elsewhere.md")
        git(other, "add", "-A")
        git(other, "commit", "-m", "elsewhere")
        git(other, "push")

        self.register_all()
        self.run_sweep()
        self.assertTrue(os.path.exists(os.path.join(path, "elsewhere.md")))


class TestGuards(SweepTest):
    def test_a_recent_change_is_left_to_settle(self):
        path = self.make_repo("alpha")
        self.write(path, "fresh.md", "typing right now\n")
        self.register_all()
        self.run_sweep()
        self.assertEqual(self.outcome("alpha"), sync_cmd.SKIPPED)
        self.assertIn("settle", self.detail("alpha"))
        self.assertTrue(gitutil.is_dirty(path))  # untouched

    def test_a_staged_index_is_flagged_not_committed(self):
        path = self.make_repo("alpha")
        self.write(path, "README.md", "mid-thought\n", age_seconds=3600)
        git(path, "add", "README.md")
        self.register_all()
        self.run_sweep()
        self.assertEqual(self.outcome("alpha"), sync_cmd.FLAGGED)
        self.assertIn("dirty-index", self.detail("alpha"))

    def test_a_secret_blocks_the_commit(self):
        path = self.make_repo("alpha")
        self.write(path, ".env", "SECRET=1\n", age_seconds=3600)
        self.register_all()
        self.run_sweep()
        self.assertEqual(self.outcome("alpha"), sync_cmd.FLAGGED)
        self.assertIn("refused to auto-add", self.detail("alpha"))
        self.assertTrue(gitutil.is_dirty(path))

    def test_a_secret_inside_a_new_directory_still_blocks(self):
        # Regression for the -uall bypass: git collapses an untracked dir to "config/".
        path = self.make_repo("alpha")
        self.write(path, "config/credentials.json", "{}\n", age_seconds=3600)
        self.register_all()
        self.run_sweep()
        self.assertEqual(self.outcome("alpha"), sync_cmd.FLAGGED)

    def test_a_repo_on_the_wrong_branch_is_skipped(self):
        path = self.make_repo("alpha")
        git(path, "checkout", "-b", "experiment")
        self.register_all()
        self.run_sweep()
        self.assertEqual(self.outcome("alpha"), sync_cmd.SKIPPED)
        self.assertIn("registered for", self.detail("alpha"))

    def test_a_detached_head_is_flagged(self):
        path = self.make_repo("alpha")
        code, out, _ = gitutil.run(path, "rev-parse", "HEAD")
        git(path, "checkout", "--detach", out.strip())
        self.register_all()
        self.run_sweep()
        self.assertEqual(self.outcome("alpha"), sync_cmd.FLAGGED)
        self.assertIn("detached", self.detail("alpha"))

    def test_a_repo_not_cloned_here_is_absent_not_flagged(self):
        """LAPTOP_PARITY §6 defect 1: the registry is one shared file for the whole fleet.

        A machine holding a subset of the repos used to report a permanent 🚩 per missing
        repo on every sweep forever. That wall of unread flags is the failure D13's corollary
        names, and one genuine conflict would sit in it undetected. Absent-by-machine is a
        different fact from broken, so it gets its own non-flagged outcome — and the sweep
        must still exit 0 and still sweep everything else.
        """
        self.make_repo("alpha")
        self.set_registry([
            {"name": "ghost", "path": os.path.join(self.tmp, "nope"), "branch": "main"},
            {"name": "alpha", "path": self.repos["alpha"][0], "branch": "main"},
        ])
        self.assertEqual(self.run_sweep(), 0)
        self.assertEqual(self.outcome("ghost"), sync_cmd.ABSENT)
        self.assertIn("not cloned", self.detail("ghost"))
        self.assertEqual(self.outcome("alpha"), sync_cmd.CLEAN)

    def test_absent_is_ordered_below_flagged_so_it_cannot_bury_a_real_flag(self):
        self.assertGreater(sync_cmd.ORDER[sync_cmd.ABSENT], sync_cmd.ORDER[sync_cmd.FLAGGED])

    def test_a_genuinely_broken_repo_is_still_flagged(self):
        """The distinction has to cut only where intended: not-a-repo is still a fault."""
        self.make_repo("alpha")
        stray = os.path.join(self.tmp, "stray")
        os.makedirs(stray)                      # exists, but is not a git repo
        self.set_registry([
            {"name": "stray", "path": stray, "branch": "main"},
            {"name": "alpha", "path": self.repos["alpha"][0], "branch": "main"},
        ])
        self.assertEqual(self.run_sweep(), 0)
        self.assertEqual(self.outcome("stray"), sync_cmd.FLAGGED)

    def test_a_conflict_is_flagged_and_the_tree_restored(self):
        # The load-bearing safety property of the whole sweep.
        path = self.make_repo("alpha")
        other = os.path.join(self.tmp, "alpha-other")
        subprocess.run(["git", "clone", self.repos["alpha"][1], other], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        git(other, "config", "user.email", "o@example.com")
        git(other, "config", "user.name", "O")
        self.write(other, "README.md", "theirs\n")
        git(other, "commit", "-am", "theirs")
        git(other, "push")

        self.write(path, "README.md", "ours\n")
        git(path, "commit", "-am", "ours")

        self.register_all()
        self.run_sweep()
        self.assertEqual(self.outcome("alpha"), sync_cmd.FLAGGED)
        self.assertIn("conflict", self.detail("alpha"))
        self.assertEqual(gitutil.state(path), gitutil.OK)
        with open(os.path.join(path, "README.md"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "ours\n")

    def test_a_repo_with_no_upstream_is_skipped(self):
        path = self.make_repo("alpha")
        git(path, "checkout", "-b", "solo")
        self.set_registry([{"name": "alpha", "path": path, "branch": "solo"}])
        self.run_sweep()
        self.assertEqual(self.outcome("alpha"), sync_cmd.SKIPPED)
        self.assertIn("upstream", self.detail("alpha"))


class TestSoftLock(SweepTest):
    def test_another_machines_open_loop_blocks_the_commit(self):
        path = self.make_repo("alpha")
        self.write(path, "notes.md", "work\n", age_seconds=3600)
        self.add_loop("alpha-loop", path, "otherbox/cc", minutes_ago=1)
        self.register_all()
        self.run_sweep()
        self.assertEqual(self.outcome("alpha"), sync_cmd.SKIPPED)
        self.assertIn("soft-lock", self.detail("alpha"))
        self.assertTrue(gitutil.is_dirty(path))

    def test_our_own_loop_does_not_block_us(self):
        path = self.make_repo("alpha")
        self.write(path, "notes.md", "work\n", age_seconds=3600)
        self.add_loop("alpha-loop", path, "testbox/cc", minutes_ago=1)
        self.register_all()
        self.run_sweep()
        self.assertEqual(self.outcome("alpha"), sync_cmd.SYNCED)

    def test_a_stale_claim_does_not_block(self):
        path = self.make_repo("alpha")
        self.write(path, "notes.md", "work\n", age_seconds=3600)
        self.add_loop("alpha-loop", path, "otherbox/cc", minutes_ago=120)
        self.register_all()
        self.run_sweep()
        self.assertEqual(self.outcome("alpha"), sync_cmd.SYNCED)

    def test_a_closed_loop_does_not_block(self):
        path = self.make_repo("alpha")
        self.write(path, "notes.md", "work\n", age_seconds=3600)
        frontmatter.write_file(
            os.path.join(self.root, "loops", "done.md"),
            {"id": "done", "class": "active", "state": "closed", "repo": path,
             "last_touch": "%s otherbox/cc"
                           % clock.parse(clock.now()).isoformat(timespec="minutes")},
            "",
        )
        self.register_all()
        self.run_sweep()
        self.assertEqual(self.outcome("alpha"), sync_cmd.SYNCED)


class TestDryRun(SweepTest):
    def test_dry_run_changes_nothing(self):
        path = self.make_repo("alpha")
        self.write(path, "notes.md", "work\n", age_seconds=3600)
        code, out, _ = gitutil.run(path, "rev-parse", "HEAD")
        before = out.strip()

        self.register_all()
        self.assertEqual(self.run_sweep("--dry-run"), 0)

        code, out, _ = gitutil.run(path, "rev-parse", "HEAD")
        self.assertEqual(out.strip(), before)
        self.assertTrue(gitutil.is_dirty(path))

    def test_dry_run_says_what_it_would_do(self):
        path = self.make_repo("alpha")
        self.write(path, "notes.md", "work\n", age_seconds=3600)
        self.register_all()
        self.run_sweep("--dry-run")
        actions = self.status()["repos"]["alpha"]["actions"]
        self.assertTrue(any("would commit" in a for a in actions), actions)
        self.assertTrue(any("would push" in a for a in actions), actions)

    def test_dry_run_is_recorded_in_the_status_file(self):
        self.make_repo("alpha")
        self.register_all()
        self.run_sweep("--dry-run")
        self.assertTrue(self.status()["dry_run"])

    def test_dry_run_still_reports_a_blocked_secret(self):
        path = self.make_repo("alpha")
        self.write(path, ".env", "x\n", age_seconds=3600)
        self.register_all()
        self.run_sweep("--dry-run")
        self.assertEqual(self.outcome("alpha"), sync_cmd.FLAGGED)


class TestControls(SweepTest):
    def test_pause_sentinel_halts_the_sweep(self):
        path = self.make_repo("alpha")
        self.write(path, "notes.md", "work\n", age_seconds=3600)
        self.register_all()
        with open(paths.pause_sentinel(), "w", encoding="utf-8") as handle:
            handle.write("halted\n")
        self.assertEqual(self.run_sweep(), 0)
        self.assertTrue(gitutil.is_dirty(path))  # nothing happened
        self.assertFalse(os.path.exists(paths.sync_status_file()))

    def test_only_limits_the_sweep(self):
        self.make_repo("alpha")
        self.make_repo("beta")
        self.register_all()
        self.run_sweep("--only", "alpha")
        self.assertIn("alpha", self.status()["repos"])
        self.assertNotIn("beta", self.status()["repos"])

    def test_sync_off_rows_are_not_swept(self):
        alpha = self.make_repo("alpha")
        beta = self.make_repo("beta")
        self.set_registry([
            {"name": "alpha", "path": alpha, "branch": "main", "sync": "on"},
            {"name": "beta", "path": beta, "branch": "main", "sync": "off",
             "notes": "third-party remote"},
        ])
        self.run_sweep()
        self.assertIn("alpha", self.status()["repos"])
        self.assertNotIn("beta", self.status()["repos"])

    def test_quiescence_window_is_configurable(self):
        path = self.make_repo("alpha")
        self.write(path, "notes.md", "work\n", age_seconds=120)
        self.register_all()
        self.run_sweep("--quiescence", "10")
        self.assertEqual(self.outcome("alpha"), sync_cmd.SKIPPED)
        self.run_sweep("--quiescence", "1")
        self.assertEqual(self.outcome("alpha"), sync_cmd.SYNCED)

    def test_report_file_is_written_and_flags_are_visible(self):
        path = self.make_repo("alpha")
        self.write(path, ".env", "x\n", age_seconds=3600)
        self.register_all()
        self.run_sweep()
        with open(paths.sync_report_file(), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("Needs you", text)
        self.assertIn("alpha", text)

    def test_empty_registry_reports_and_exits_nonzero(self):
        self.set_registry([])
        self.assertEqual(self.run_sweep(), 1)

    def test_an_unregistered_machine_exits_2(self):
        self.make_repo("alpha")
        self.register_all()
        os.environ["LODESTAR_MACHINE"] = "who-am-i"
        import lib.machine as machine_mod
        machine_mod._key_cache = None
        self.assertEqual(self.run_sweep(), 2)


class TestClaudeConfigStep(SweepTest):
    """The ~/.claude backup folded into the sweep."""

    def setUp(self):
        super(TestClaudeConfigStep, self).setUp()
        import claudeconf
        self.claudeconf = claudeconf
        self.home = os.path.join(self.tmp, "claude-home")
        os.makedirs(os.path.join(self.home, "projects", "-proj", "memory"))
        # Not `self._saved` — that name belongs to SweepTest's env dict, and shadowing it
        # breaks the parent's cleanup and leaks LODESTAR_ROOT into every later test.
        self._saved_conf = (claudeconf.CLAUDE_HOME, claudeconf.LIVE_CLAUDE_MD)
        # test-box is unregistered, so `prosehome` treats it as `work`; this suite pins the
        # in-repo memory layout. Work-Mac routing: tests/test_claudeconf.py.
        self._saved_memdir = claudeconf.memory_snapshot_dir
        claudeconf.memory_snapshot_dir = lambda: os.path.join(claudeconf.snapshot_dir(), "memory")
        self.addCleanup(setattr, claudeconf, "memory_snapshot_dir", self._saved_memdir)
        claudeconf.CLAUDE_HOME = self.home
        claudeconf.LIVE_CLAUDE_MD = os.path.join(self.home, "CLAUDE.md")
        self.addCleanup(self._restore_conf)

        os.makedirs(os.path.join(self.root, "claude", "global", "machines"))
        self._section("machines/test-box.md", "# Machine\n\nTest box.\n")
        self._section("10-shared.md", "# Shared\n\nRule.\n")
        with open(os.path.join(self.home, "settings.json"), "w", encoding="utf-8") as h:
            h.write('{"theme": "dark"}\n')
        with open(os.path.join(self.home, "projects", "-proj", "memory", "MEMORY.md"),
                  "w", encoding="utf-8") as h:
            h.write("- a memory\n")

    def _restore_conf(self):
        self.claudeconf.CLAUDE_HOME, self.claudeconf.LIVE_CLAUDE_MD = self._saved_conf

    def _section(self, relative, text):
        path = os.path.join(self.root, "claude", "global", relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def _render_live(self):
        with open(self.claudeconf.LIVE_CLAUDE_MD, "w", encoding="utf-8") as handle:
            handle.write(self.claudeconf.render_text())

    def test_snapshots_settings_and_memory_into_the_repo(self):
        self.make_repo("alpha")
        self.register_all()
        self._render_live()
        self.run_sweep()
        snap = self.claudeconf.snapshot_dir()
        self.assertTrue(os.path.exists(os.path.join(snap, "settings.json")))
        self.assertTrue(os.path.exists(
            os.path.join(snap, "memory", "-proj", "MEMORY.md")))
        self.assertEqual(os.path.basename(snap), "test-box")  # per-machine, not shared

    def test_flags_claude_md_drift_rather_than_overwriting_it(self):
        # A hand edit must survive: an automatic render would silently destroy it.
        self.make_repo("alpha")
        self.register_all()
        self._render_live()
        with open(self.claudeconf.LIVE_CLAUDE_MD, "a", encoding="utf-8") as handle:
            handle.write("\n# Hand edit\n\nMine.\n")
        self.run_sweep()
        self.assertEqual(self.outcome("~/.claude"), sync_cmd.FLAGGED)
        with open(self.claudeconf.LIVE_CLAUDE_MD, encoding="utf-8") as handle:
            self.assertIn("Mine.", handle.read())

    def test_drift_names_the_command_that_resolves_it(self):
        # A flag nobody can act on is D13's corollary failing.
        self.make_repo("alpha")
        self.register_all()
        self._render_live()
        with open(self.claudeconf.LIVE_CLAUDE_MD, "a", encoding="utf-8") as handle:
            handle.write("\n# Hand edit\n\nMine.\n")
        self.run_sweep()
        self.assertIn("adopt", self.detail("~/.claude"))

    def test_drift_is_captured_into_the_adopt_queue(self):
        self.make_repo("alpha")
        self.register_all()
        self._render_live()
        with open(self.claudeconf.LIVE_CLAUDE_MD, "a", encoding="utf-8") as handle:
            handle.write("\n# Hand edit\n\nMine.\n")
        self.run_sweep()
        captures = self.claudeconf.pending_captures()
        self.assertEqual(len(captures), 1)
        with open(captures[0], encoding="utf-8") as handle:
            self.assertIn("Mine.", handle.read())

    def test_a_source_change_reaches_an_untouched_live_file(self):
        # The whole point: an edit made on the other machine lands here by itself.
        self.make_repo("alpha")
        self.register_all()
        self._render_live()
        self.run_sweep()                       # establishes provenance
        self._section("20-new.md", "# Newly Shared\n\nArrived from the other machine.\n")
        self.run_sweep()
        with open(self.claudeconf.LIVE_CLAUDE_MD, encoding="utf-8") as handle:
            self.assertIn("# Newly Shared", handle.read())
        self.assertEqual(self.outcome("~/.claude"), sync_cmd.SYNCED)

    def test_a_missing_machine_section_flags_instead_of_killing_the_sweep(self):
        # This is a fresh-clone situation, e.g. the Laptop before it is registered.
        self.make_repo("alpha")
        self.register_all()
        os.unlink(os.path.join(self.root, "claude", "global", "machines", "test-box.md"))
        self.assertEqual(self.run_sweep(), 0)
        self.assertEqual(self.outcome("~/.claude"), sync_cmd.FLAGGED)
        self.assertEqual(self.outcome("alpha"), sync_cmd.CLEAN)  # repos still swept

    def test_resnapshotting_unchanged_files_does_not_reset_mtimes(self):
        # The hazard: the sweep snapshots every ~2 minutes. If copy2 stamped the copies with
        # "now", the snapshot inside the board would look freshly changed on every sweep,
        # Lodestar would never be quiescent, and it would therefore never auto-commit again.
        # copy2 preserves the source mtime, so an unchanged file produces no diff at all.
        snapshot = self.claudeconf.snapshot_dir()
        watched = ("settings.json", os.path.join("memory", "-proj", "MEMORY.md"))

        self.claudeconf.snapshot_all()
        first = [os.stat(os.path.join(snapshot, p)).st_mtime for p in watched]
        time.sleep(0.05)
        self.claudeconf.snapshot_all()
        second = [os.stat(os.path.join(snapshot, p)).st_mtime for p in watched]
        self.assertEqual(first, second)

    def test_a_changed_memory_file_is_picked_up_on_the_next_sweep(self):
        self.claudeconf.snapshot_all()
        live = os.path.join(self.home, "projects", "-proj", "memory", "MEMORY.md")
        with open(live, "w", encoding="utf-8") as handle:
            handle.write("- a memory, revised\n")
        self.claudeconf.snapshot_all()
        saved = os.path.join(self.claudeconf.snapshot_dir(), "memory", "-proj", "MEMORY.md")
        with open(saved, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "- a memory, revised\n")

    def test_dry_run_does_not_snapshot(self):
        self.make_repo("alpha")
        self.register_all()
        self._render_live()
        self.run_sweep("--dry-run")
        snap = self.claudeconf.snapshot_dir()
        self.assertFalse(os.path.exists(os.path.join(snap, "settings.json")))
        actions = self.status()["repos"]["~/.claude"]["actions"]
        self.assertTrue(any("would snapshot" in a for a in actions), actions)

    def test_only_flag_skips_the_claude_step(self):
        self.make_repo("alpha")
        self.register_all()
        self.run_sweep("--only", "alpha")
        self.assertNotIn("~/.claude", self.status()["repos"])


class TestLockContention(SweepTest):
    """The 2026-09-01 wedge: a stale `.git/index.lock` silently blocking every commit.

    The point of every test here is that lock trouble is *visible*. It must never be
    reported as `clean`, and it must never stop the sweep reaching the next repo.
    """

    def setUp(self):
        super(TestLockContention, self).setUp()
        # The stale-lock removal announces itself on stderr by design; keep the test
        # runner's output readable without weakening what is asserted.
        stack = contextlib.ExitStack()
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.addCleanup(stack.close)

    def lock_path(self, repo):
        return os.path.join(repo, ".git", "index.lock")

    def plant_lock(self, repo, contents="", age_seconds=0):
        path = self.lock_path(repo)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(contents)
        if age_seconds:
            old = time.time() - age_seconds
            os.utime(path, (old, old))
        return path

    def patch_sleep(self, side_effect=None):
        original = gitutil._sleep

        def fake(seconds):
            if side_effect:
                side_effect()

        gitutil._sleep = fake
        self.addCleanup(lambda: setattr(gitutil, "_sleep", original))

    def test_a_lock_that_clears_mid_sweep_is_reported_not_hidden(self):
        path = self.make_repo("alpha")
        self.write(path, "notes.md", "work\n", age_seconds=3600)
        lock = self.plant_lock(path)  # fresh: assumed live, so retried rather than removed
        self.patch_sleep(lambda: os.path.exists(lock) and os.unlink(lock))
        self.register_all()
        self.run_sweep()

        self.assertEqual(self.outcome("alpha"), sync_cmd.CONTENDED)
        self.assertNotEqual(self.outcome("alpha"), sync_cmd.CLEAN)
        self.assertIn("retried", self.detail("alpha"))
        self.assertFalse(gitutil.is_dirty(path))  # the work still landed
        self.assertEqual(gitutil.ahead_behind(path), (0, 0))

    def test_a_stale_lock_is_removed_and_said_out_loud(self):
        path = self.make_repo("alpha")
        self.write(path, "notes.md", "work\n", age_seconds=3600)
        lock = self.plant_lock(path, age_seconds=86400)
        self.register_all()
        self.run_sweep()

        self.assertEqual(self.outcome("alpha"), sync_cmd.CONTENDED)
        self.assertIn("removed a stale", self.detail("alpha"))
        self.assertFalse(os.path.exists(lock))
        self.assertFalse(gitutil.is_dirty(path))

    def test_a_held_lock_is_flagged_and_nothing_is_touched(self):
        path = self.make_repo("alpha")
        self.write(path, "notes.md", "work\n", age_seconds=3600)
        lock = self.plant_lock(path, contents="a real writer", age_seconds=86400)
        self.register_all()
        self.run_sweep()

        self.assertEqual(self.outcome("alpha"), sync_cmd.FLAGGED)
        self.assertIn("needs you", self.detail("alpha"))
        self.assertTrue(os.path.exists(lock))  # removing a live lock corrupts the repo
        self.assertTrue(gitutil.is_dirty(path))

    def test_a_fresh_lock_that_never_clears_is_flagged_not_silent(self):
        # Fresh, so it is presumed live and never removed: the retries run out and the
        # sweep has to say so. This is the path that used to fail five times in silence.
        path = self.make_repo("alpha")
        self.write(path, "notes.md", "work\n", age_seconds=3600)
        lock = self.plant_lock(path)
        self.patch_sleep()
        self.register_all()
        self.run_sweep()

        self.assertEqual(self.outcome("alpha"), sync_cmd.FLAGGED)
        self.assertIn("index.lock", self.detail("alpha"))
        self.assertIn("needs you", self.detail("alpha"))
        self.assertTrue(os.path.exists(lock))
        self.assertTrue(gitutil.is_dirty(path))

    def test_the_sweep_continues_past_a_wedged_repo(self):
        alpha = self.make_repo("alpha")
        beta = self.make_repo("beta")
        self.write(alpha, "notes.md", "work\n", age_seconds=3600)
        self.write(beta, "notes.md", "work\n", age_seconds=3600)
        self.plant_lock(alpha, contents="a real writer", age_seconds=86400)
        self.register_all()
        self.assertEqual(self.run_sweep(), 0)

        self.assertEqual(self.outcome("alpha"), sync_cmd.FLAGGED)
        self.assertEqual(self.outcome("beta"), sync_cmd.SYNCED)
        self.assertFalse(gitutil.is_dirty(beta))

    def test_the_health_report_names_the_contention(self):
        path = self.make_repo("alpha")
        self.write(path, "notes.md", "work\n", age_seconds=3600)
        self.plant_lock(path, age_seconds=86400)
        self.register_all()
        self.run_sweep()

        with open(paths.sync_report_file(), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("alpha", text)
        self.assertIn("index.lock", text)
        self.assertIn(sync_cmd.CONTENDED, text)

    def test_a_wedged_repo_appears_in_the_needs_you_section(self):
        path = self.make_repo("alpha")
        self.write(path, "notes.md", "work\n", age_seconds=3600)
        self.plant_lock(path, contents="a real writer", age_seconds=86400)
        self.register_all()
        self.run_sweep()

        with open(paths.sync_report_file(), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("Needs you", text)
        self.assertIn("index.lock", text)

    def test_dry_run_reports_a_stale_lock_without_removing_it(self):
        path = self.make_repo("alpha")
        self.write(path, "notes.md", "work\n", age_seconds=3600)
        lock = self.plant_lock(path, age_seconds=86400)
        self.register_all()
        self.run_sweep("--dry-run")

        self.assertEqual(self.outcome("alpha"), sync_cmd.CONTENDED)
        self.assertIn("would remove", self.detail("alpha"))
        self.assertTrue(os.path.exists(lock))

    def test_contention_outranks_synced_in_the_report_order(self):
        # It must not be buried below the routine rows a human scrolls past.
        self.assertLess(sync_cmd.ORDER[sync_cmd.CONTENDED],
                        sync_cmd.ORDER[sync_cmd.SYNCED])
        self.assertLess(sync_cmd.ORDER[sync_cmd.FLAGGED],
                        sync_cmd.ORDER[sync_cmd.CONTENDED])


class TestTierDerivation(SweepTest):
    """Tiers come from each repo's own newest commit, so nothing is hand-maintained."""

    def entry(self, name, path):
        return registry.Entry(name=name, path=path, branch="main", sync=True, notes="")

    def test_a_just_committed_repo_is_hot(self):
        path = self.make_repo("alpha")
        self.assertEqual(sync_cmd.tier_for(self.entry("alpha", path)), sync_cmd.HOT)

    def test_a_repo_committed_three_days_ago_is_warm(self):
        path = self.make_repo("alpha", days_old=3)
        self.assertEqual(sync_cmd.tier_for(self.entry("alpha", path)), sync_cmd.WARM)

    def test_an_ancient_repo_is_cold(self):
        path = self.make_repo("alpha", days_old=40)
        self.assertEqual(sync_cmd.tier_for(self.entry("alpha", path)), sync_cmd.COLD)

    def test_lodestar_is_hot_however_old_its_last_commit(self):
        # It is the board *and* the airlock: inbound captures and requests land there, so a
        # quiet week must never slow down how fast it notices them.
        path = self.make_repo("Lodestar", days_old=400)
        self.assertEqual(sync_cmd.tier_for(self.entry("Lodestar", path)), sync_cmd.HOT)

    def test_a_repo_whose_age_cannot_be_read_is_treated_as_hot(self):
        # Unknown must fail towards fetching. Skipping a fetch on a guess is how inbound
        # work goes unnoticed for hours.
        ghost = os.path.join(self.tmp, "nope")
        self.assertEqual(sync_cmd.tier_for(self.entry("ghost", ghost)), sync_cmd.HOT)

    def test_promotion_needs_no_registry_edit(self):
        # The whole point of deriving: committing to a cold repo makes it hot immediately.
        path = self.make_repo("alpha", days_old=40)
        entry = self.entry("alpha", path)
        self.assertEqual(sync_cmd.tier_for(entry), sync_cmd.COLD)
        self.write(path, "now.md", "work\n")
        git(path, "add", "-A")
        git(path, "commit", "-m", "work")
        self.assertEqual(sync_cmd.tier_for(entry), sync_cmd.HOT)

    def test_the_intervals_grow_with_the_tier(self):
        self.assertLess(sync_cmd.FETCH_INTERVAL[sync_cmd.HOT],
                        sync_cmd.FETCH_INTERVAL[sync_cmd.WARM])
        self.assertLess(sync_cmd.FETCH_INTERVAL[sync_cmd.WARM],
                        sync_cmd.FETCH_INTERVAL[sync_cmd.COLD])

    def test_every_interval_is_longer_than_one_sweep(self):
        # Otherwise the throttle saves nothing: the LaunchAgent fires every 127s.
        for tier, interval in sync_cmd.FETCH_INTERVAL.items():
            self.assertGreater(interval, 127, tier)


class TestFetchThrottle(SweepTest):
    """Fetch is rate-limited per repo; the local half of the sweep never is.

    The correctness property: `sync.py` pulls before it pushes, so a repo that has anything
    to push MUST be fetched this sweep whatever its tier says. Only a clean, non-diverged
    repo — where a fetch could discover nothing but *inbound* work — is throttled.
    """

    def test_a_dirty_repo_is_fetched_even_when_its_interval_has_not_elapsed(self):
        path = self.make_repo("alpha", days_old=40)  # cold: 6h interval
        self.write(path, "notes.md", "work\n", age_seconds=3600)
        self.set_fetch_state({"alpha": 60})  # fetched a minute ago; nowhere near due
        self.register_all()
        self.run_sweep()

        self.assertTrue(self.fetched("alpha"), self.actions("alpha"))
        self.assertEqual(self.outcome("alpha"), sync_cmd.SYNCED)
        self.assertEqual(gitutil.ahead_behind(path), (0, 0))  # it pushed

    def test_a_dirty_repo_still_receives_inbound_work_off_interval(self):
        # The reason the local half runs every sweep: you cannot push without pulling.
        path = self.make_repo("alpha", days_old=40)
        self.push_from_elsewhere("alpha")
        self.write(path, "notes.md", "work\n", age_seconds=3600)
        self.set_fetch_state({"alpha": 60})
        self.register_all()
        self.run_sweep()

        self.assertTrue(os.path.exists(os.path.join(path, "elsewhere.md")))

    def test_a_clean_repo_that_is_ahead_is_fetched_too(self):
        path = self.make_repo("alpha", days_old=40)
        self.write(path, "committed.md", "work\n")
        git(path, "add", "-A")
        git(path, "commit", "-m", "local work")
        self.assertEqual(gitutil.ahead_behind(path), (1, 0))
        self.set_fetch_state({"alpha": 60})
        self.register_all()
        self.run_sweep()

        self.assertTrue(self.fetched("alpha"), self.actions("alpha"))
        self.assertEqual(gitutil.ahead_behind(path), (0, 0))

    def test_a_clean_up_to_date_cold_repo_is_skipped_and_says_so(self):
        path = self.make_repo("alpha", days_old=40)
        self.push_from_elsewhere("alpha")  # inbound work it will NOT see this sweep
        self.set_fetch_state({"alpha": 3600})  # an hour ago; cold wants six
        self.register_all()
        self.run_sweep()

        self.assertEqual(self.outcome("alpha"), sync_cmd.CLEAN)
        self.assertFalse(self.fetched("alpha"), self.actions("alpha"))
        self.assertTrue(any("skipped" in a for a in self.actions("alpha")),
                        self.actions("alpha"))
        self.assertFalse(os.path.exists(os.path.join(path, "elsewhere.md")))

    def test_a_hot_repo_past_its_interval_is_fetched(self):
        self.make_repo("alpha")  # committed just now -> hot, 15m interval
        self.set_fetch_state({"alpha": 3600})
        self.register_all()
        self.run_sweep()
        self.assertTrue(self.fetched("alpha"), self.actions("alpha"))

    def test_a_hot_repo_inside_its_interval_is_skipped(self):
        self.make_repo("alpha")
        self.set_fetch_state({"alpha": 60})
        self.register_all()
        self.run_sweep()
        self.assertFalse(self.fetched("alpha"), self.actions("alpha"))

    def test_a_warm_repo_is_skipped_at_half_an_hour_and_fetched_at_two(self):
        self.make_repo("alpha", days_old=3)
        self.register_all()
        self.set_fetch_state({"alpha": 1800})
        self.run_sweep()
        self.assertFalse(self.fetched("alpha"), self.actions("alpha"))
        self.set_fetch_state({"alpha": 7200})
        self.run_sweep()
        self.assertTrue(self.fetched("alpha"), self.actions("alpha"))

    def test_a_missing_fetch_state_file_fetches_everything_once(self):
        self.make_repo("alpha", days_old=40)
        self.make_repo("beta", days_old=40)
        self.assertFalse(os.path.exists(paths.fetch_state_file()))
        self.register_all()

        self.assertEqual(self.run_sweep(), 0)  # no crash
        self.assertTrue(self.fetched("alpha"), self.actions("alpha"))
        self.assertTrue(self.fetched("beta"), self.actions("beta"))
        self.assertEqual(
            set(self.fetch_state()["repos"]), {"alpha", "beta"})

    def test_an_unreadable_fetch_state_file_fetches_rather_than_crashing(self):
        self.make_repo("alpha", days_old=40)
        target = paths.fetch_state_file()
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")
        self.register_all()
        self.assertEqual(self.run_sweep(), 0)
        self.assertTrue(self.fetched("alpha"), self.actions("alpha"))

    def test_the_second_sweep_skips_what_the_first_one_fetched(self):
        self.make_repo("alpha")  # hot: 15m
        self.register_all()
        self.run_sweep()
        self.assertTrue(self.fetched("alpha"), self.actions("alpha"))
        self.run_sweep()
        self.assertFalse(self.fetched("alpha"), self.actions("alpha"))

    def test_only_does_not_forget_the_other_repos_timestamps(self):
        self.make_repo("alpha")
        self.make_repo("beta")
        self.register_all()
        self.run_sweep()
        self.run_sweep("--only", "alpha")
        self.assertIn("beta", self.fetch_state()["repos"])

    def test_dry_run_writes_no_fetch_state(self):
        self.make_repo("alpha")
        self.register_all()
        self.run_sweep("--dry-run")
        self.assertFalse(os.path.exists(paths.fetch_state_file()))

    def test_dry_run_does_not_disturb_existing_fetch_state(self):
        self.make_repo("alpha")
        self.register_all()
        self.set_fetch_state({"alpha": 3600})
        before = self.fetch_state()
        self.run_sweep("--dry-run")
        self.assertEqual(self.fetch_state(), before)

    def test_the_fetch_state_file_lives_outside_the_board(self):
        # Decision D-D and the .git/index.lock lesson: a per-machine file inside a synced
        # repo makes two machines fight, and a lock inside it wedges the other machine.
        self.assertNotIn(os.path.realpath(self.root),
                         os.path.realpath(paths.fetch_state_file()))


class TestFetchVisibility(SweepTest):
    """D13's corollary: a fetch the sweep did not perform must not read as freshness."""

    def test_clean_no_longer_claims_to_have_checked_the_remote(self):
        path = self.make_repo("alpha", days_old=40)
        self.set_fetch_state({"alpha": 3600})
        self.register_all()
        self.run_sweep()
        detail = self.detail("alpha")
        self.assertIn("no local changes", detail)
        self.assertIn("not checked", detail)
        self.assertNotIn("verified", detail)

    def test_a_fetched_clean_repo_says_it_verified_against_the_remote(self):
        self.make_repo("alpha")
        self.register_all()
        self.run_sweep()
        self.assertIn("verified against the remote", self.detail("alpha"))

    def test_the_skip_detail_names_the_tier_and_the_age(self):
        self.make_repo("alpha", days_old=40)
        self.set_fetch_state({"alpha": 3 * 3600})
        self.register_all()
        self.run_sweep()
        detail = self.detail("alpha")
        self.assertIn("cold", detail)
        self.assertIn("3h", detail)

    def test_the_status_json_carries_the_tier_and_the_fetch_age(self):
        self.make_repo("alpha", days_old=40)
        self.set_fetch_state({"alpha": 3600})
        self.register_all()
        self.run_sweep()
        row = self.status()["repos"]["alpha"]
        self.assertEqual(row["tier"], sync_cmd.COLD)
        self.assertFalse(row["fetched"])
        self.assertGreaterEqual(row["remote_age_seconds"], 3500)

    def test_the_report_has_a_remote_freshness_column(self):
        self.make_repo("alpha", days_old=40)
        self.make_repo("beta")
        self.set_fetch_state({"alpha": 3 * 3600, "beta": 60})
        self.register_all()
        self.run_sweep()
        with open(paths.sync_report_file(), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("Remote", text)
        self.assertIn("cold", text)
        self.assertIn("hot", text)
        self.assertIn("not checked", text)

    def test_the_report_explains_what_clean_does_and_does_not_mean(self):
        self.make_repo("alpha")
        self.register_all()
        self.run_sweep()
        with open(paths.sync_report_file(), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("tier", text.lower())


@needs_private("sync/registry.md")
class TestRegistryParsing(unittest.TestCase):
    def test_reads_the_real_registry(self):
        entries = registry.entries()
        self.assertTrue(entries, "the committed registry should not be empty")
        names = {entry.name for entry in entries}
        self.assertIn("Lodestar", names)

    def test_third_party_and_org_repos_are_off(self):
        off = {entry.name for entry in registry.excluded()}
        self.assertIn("cocaffeinate", off)

    def test_master_branch_repos_are_recorded_as_master(self):
        table = {entry.name: entry.branch for entry in registry.entries()}
        self.assertEqual(table["Ideogram"], "master")
        self.assertEqual(table["Placard"], "master")

    def test_paths_are_expanded(self):
        for entry in registry.entries():
            self.assertFalse(entry.path.startswith("~"), entry.path)

    def test_no_google_drive_paths_are_registered(self):
        # Drive replicating a .git directory corrupts it; those roots must never appear.
        for entry in registry.entries():
            self.assertNotIn("GDrive", entry.path)
            self.assertNotIn("My Drive", entry.path)


class TestSourceIsNotAutoCommitted(SweepTest):
    """The sweep owns board state; source needs an authored commit.

    Measured 2026-09-15: 8 of 304 `chore(sync)` commits had swallowed source. The cost is not
    untidy history — `lib/reversal.py` records that reverting a bundled sync sha reverts someone
    else's work, so this breaks the Phase 4 reversal protocol.
    """

    def porcelain(self, repo):
        """`-uall` so untracked entries name FILES, matching what the sweep itself sees.

        Plain porcelain collapses a new directory to `?? bin/` — still uncommitted, but it does
        not mention the file, so asserting against it measured the wrong thing.
        `gitutil.status_entries` already passes `-uall`, and its docstring says why: without it
        the secret and size guards would inspect a directory name and find nothing.
        """
        _, out, _ = gitutil.run(repo, "status", "--porcelain", "-uall")
        return out

    def subject(self, repo):
        _, out, _ = gitutil.run(repo, "log", "-1", "--format=%s")
        return out.strip()

    def test_a_source_only_change_is_left_uncommitted(self):
        path = self.make_repo("alpha")
        self.write(path, "bin/tool.py", "print(1)\n", age_seconds=3600)
        self.register_all()
        self.run_sweep()
        self.assertIn("bin/tool.py", self.porcelain(path),
                      "source must still be there for an authored commit")
        self.assertNotIn("chore(sync)", self.subject(path),
                         "the sweep must not have committed it")

    def test_the_report_names_the_withheld_file(self):
        path = self.make_repo("alpha")
        self.write(path, "bin/tool.py", "print(1)\n", age_seconds=3600)
        self.register_all()
        self.run_sweep()
        self.assertIn("bin/tool.py", self.detail("alpha"),
                      "a silently withheld file is worse than a swallowed one")

    def test_board_state_is_still_committed_alongside_withheld_source(self):
        path = self.make_repo("alpha")
        self.write(path, "notes.md", "board state\n", age_seconds=3600)
        self.write(path, "bin/tool.py", "print(1)\n", age_seconds=3600)
        self.register_all()
        self.run_sweep()
        remaining = self.porcelain(path)
        self.assertIn("bin/tool.py", remaining, "source stays")
        self.assertNotIn("notes.md", remaining, "board state should have been committed")

    def test_a_board_only_change_behaves_exactly_as_before(self):
        """296 of the 304 commits are this case and must not change."""
        path = self.make_repo("alpha")
        self.write(path, "notes.md", "board state\n", age_seconds=3600)
        self.register_all()
        self.run_sweep()
        self.assertEqual(self.outcome("alpha"), sync_cmd.SYNCED)
        self.assertFalse(gitutil.is_dirty(path))

    def test_the_commit_message_names_what_changed(self):
        path = self.make_repo("alpha")
        self.write(path, "notes.md", "board state\n", age_seconds=3600)
        self.register_all()
        self.run_sweep()
        subject = self.subject(path)
        self.assertTrue(subject.startswith("chore(sync): testbox "), subject)
        self.assertIn("notes.md", subject,
                      "a bare timestamp gives a reverter nothing to judge")

    def test_docs_prose_is_withheld_too(self):
        path = self.make_repo("alpha")
        self.write(path, "docs/ROADMAP.md", "plan\n", age_seconds=3600)
        self.register_all()
        self.run_sweep()
        self.assertIn("docs/ROADMAP.md", self.porcelain(path))


class TestTransientIsNotNeedsYou(SweepTest):
    """2026-10-22: 9, then 43 of 55 healthy repos flagged 🚩 "needs you" — git timeouts read
    as not-a-repo, and a DNS/VPN flap read as 'fetch failed: and the repository exists.'
    Transient conditions are BUSY/OFFLINE (amber), re-checked, and never flagged."""

    def setUp(self):
        super(TestTransientIsNotNeedsYou, self).setUp()
        stack = contextlib.ExitStack()
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.addCleanup(stack.close)

    def patch(self, obj, name, value):
        original = getattr(obj, name)
        setattr(obj, name, value)
        self.addCleanup(lambda: setattr(obj, name, original))

    def test_offline_fetch_is_offline_with_the_real_cause(self):
        self.make_repo("alpha")
        self.register_all()
        err = ("ssh: Could not resolve hostname github.com: nodename nor servname provided\n"
               "fatal: Could not read from remote repository.\n\n"
               "Please make sure you have the correct access rights\nand the repository exists.\n")
        self.patch(gitutil, "fetch", lambda repo: gitutil.GitResult(128, "", err))
        self.run_sweep()
        self.assertEqual(self.outcome("alpha"), sync_cmd.OFFLINE)
        self.assertIn("Could not resolve hostname", self.detail("alpha"))
        self.assertNotIn("and the repository exists", self.detail("alpha"))

    def test_missing_repository_is_still_flagged(self):
        self.make_repo("alpha")
        self.register_all()
        err = "ERROR: Repository not found.\nfatal: Could not read from remote repository.\n"
        self.patch(gitutil, "fetch", lambda repo: gitutil.GitResult(128, "", err))
        self.run_sweep()
        self.assertEqual(self.outcome("alpha"), sync_cmd.FLAGGED)
        self.assertIn("Repository not found", self.detail("alpha"))

    def test_fetch_timeout_is_busy(self):
        self.make_repo("alpha")
        self.register_all()
        self.patch(gitutil, "fetch", lambda repo: gitutil.GitResult(
            gitutil.TIMEOUT_CODE, "", "timed out after 180s: git fetch"))
        self.run_sweep()
        self.assertEqual(self.outcome("alpha"), sync_cmd.BUSY)

    def test_state_timeout_is_busy_not_flagged(self):
        self.make_repo("alpha")
        self.register_all()
        self.patch(gitutil, "state", lambda repo: gitutil.TIMED_OUT)
        self.run_sweep()
        self.assertEqual(self.outcome("alpha"), sync_cmd.BUSY)

    def test_a_transient_row_is_rechecked_and_clears_in_the_same_sweep(self):
        alpha = self.make_repo("alpha")
        self.register_all()
        real = gitutil.state
        calls = []

        def once_slow(repo):
            if repo != alpha:
                return real(repo)
            calls.append(repo)
            return gitutil.TIMED_OUT if len(calls) == 1 else real(repo)
        self.patch(gitutil, "state", once_slow)
        self.run_sweep()
        self.assertEqual(self.outcome("alpha"), sync_cmd.CLEAN)
        self.assertTrue(any("re-checked" in a for a in self.actions("alpha")))

    def test_soft_deadline_reports_unreached_repos_busy(self):
        self.make_repo("alpha")
        self.make_repo("beta")
        self.register_all()
        self.patch(sync_cmd, "SOFT_DEADLINE_S", 0)
        self.run_sweep()
        self.assertEqual(self.outcome("alpha"), sync_cmd.BUSY)
        self.assertIn("not reached", self.detail("beta"))

    def test_progress_record_says_finished_with_latency(self):
        self.make_repo("alpha")
        self.register_all()
        self.run_sweep()
        with open(sync_cmd.progress_file(), encoding="utf-8") as handle:
            record = json.load(handle)
        self.assertTrue(record["finished"])
        self.assertIn("launch_latency_ms", record)
        self.assertEqual(record["total"], 1)

    def test_watchdog_records_where_it_was_stuck_and_exits(self):
        sync_cmd.write_progress(repo="alpha", finished=False)
        exits = []
        dog = sync_cmd.Watchdog(0, 0, exit_fn=exits.append).start()
        dog._thread.join(5)
        self.assertEqual(exits, [sync_cmd.WATCHDOG_EXIT])
        with open(sync_cmd.progress_file(), encoding="utf-8") as handle:
            record = json.load(handle)
        self.assertIn("stuck at alpha", record["aborted"])

    def test_failure_cause_skips_git_boilerplate(self):
        self.assertEqual(sync_cmd.git_failure_cause(
            "fatal: unable to access 'x': Could not resolve host: github.com\n"),
            "fatal: unable to access 'x': Could not resolve host: github.com")
        self.assertTrue(sync_cmd.is_offline_failure("Could not resolve host: github.com"))
        self.assertFalse(sync_cmd.is_offline_failure("Permission denied (publickey)."))


class BoardNoteTest(unittest.TestCase):
    """`sync.board_note` — the board's own push health on the one-line `--quiet` sweep record.

    This exists because on 2026-09-30 the Desktop board sat ahead 15 / behind 42 for roughly
    fifteen hours: it committed hourly, could not push, and **exited 0 every time**. Nothing
    surfaced it. `SYNC.md` is gitignored (invisible from any other machine, and keeps no
    history), and the quiescence gate returns SKIPPED for Lodestar before divergence is ever
    evaluated — the board is almost never quiescent because it writes heartbeats every sweep.
    `--quiet` printed nothing at all, so an empty log was indistinguishable from a sweep that
    was not running.

    The calibration cases matter as much as the flag: if merely being *ahead* raised a flag,
    the line would fire on the normal transient state between this sweep's commit and its push,
    and a reader trained to ignore it is no better than no line at all.
    """

    def test_ahead_and_behind_is_flagged(self):
        note = sync_cmd.board_note((15, 42))
        self.assertIn("🚩", note)
        self.assertIn("15", note)
        self.assertIn("42", note)

    def test_in_sync_is_silent(self):
        self.assertEqual("", sync_cmd.board_note((0, 0)))

    def test_ahead_only_is_not_a_flag(self):
        note = sync_cmd.board_note((1, 0))
        self.assertNotIn("🚩", note,
                         "ahead-only is the normal state between commit and push; flagging it "
                         "trains the reader to ignore the line")
        self.assertIn("1", note)

    def test_behind_only_is_not_a_flag(self):
        note = sync_cmd.board_note((0, 3))
        self.assertNotIn("🚩", note, "behind-only fast-forwards on its own")

    def test_no_upstream_is_stated_not_guessed(self):
        self.assertIn("no upstream", sync_cmd.board_note(None))


if __name__ == "__main__":
    unittest.main()
