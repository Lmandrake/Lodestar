"""Tests for the Send/Receive split, .lodestarignore, adopt-before-clone, and per-repo pause.

Same throwaway-repo rig as test_sync.py: every repo is a clone of a local bare remote under a
temp dir, so fast-forward, rebase, commit and push run for real with no network.
"""

import contextlib
import io
import json
import os
import subprocess
import time
import unittest
from unittest import mock

import sync as sync_cmd
from lib import gitutil, machine, registry, repopause, skillclone

from tests.test_sync import SweepTest, git
from tests import needs_private, real_home_key, real_key  # noqa: E402


class SendReceiveTest(SweepTest):
    def set_registry(self, rows, machine_paths=()):
        lines = ["| Name | Path | Branch | Send | Receive | Machines | Notes |",
                 "|------|------|--------|------|---------|----------|-------|"]
        for row in rows:
            lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
                row["name"], row["path"], row.get("branch", "main"),
                row.get("send", "on"), row.get("receive", "on"),
                row.get("machines", ""), row.get("notes", "")))
        if machine_paths:
            lines += ["", "## Machine paths", "", "| Repo | Machine | Path |",
                      "|------|---------|------|"]
            lines += ["| %s | %s | %s |" % item for item in machine_paths]
        with open(os.path.join(self.root, "sync", "registry.md"), "w",
                  encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

    def register(self, name, **switches):
        path, _, branch = self.repos[name]
        row = {"name": name, "path": path, "branch": branch}
        row.update(switches)
        self.set_registry([row])

    def remote_files(self, name):
        remote = self.repos[name][1]
        out = subprocess.run(["git", "--git-dir", remote, "ls-tree", "-r", "--name-only",
                              "HEAD"], stdout=subprocess.PIPE, check=True).stdout
        return set(out.decode().split())

    def head(self, path):
        return gitutil.run(path, "rev-parse", "HEAD").out.strip()


class TestSend(SendReceiveTest):
    def test_a_peers_staged_file_is_not_included_in_the_send_commit(self):
        """The race: a peer stages a file after the state check, before the commit."""
        path = self.make_repo("alpha")
        self.write(path, "board.md", "sweep me\n", age_seconds=3600)
        self.write(path, "peer.md", "mine, staged on purpose\n", age_seconds=3600)
        self.register("alpha")

        real_quiescent = gitutil.quiescent

        def quiescent_then_peer_stages(repo, seconds, candidates=None):
            git(repo, "add", "peer.md")
            return real_quiescent(repo, seconds, [c for c in candidates if c != "peer.md"])

        # The peer's file must not even be a candidate for this test to prove the pathspec,
        # so pretend it was already withheld (e.g. by .lodestarignore) for this sweep.
        real_split = gitutil.split_ignored

        def split(repo, paths):
            keep, drop = real_split(repo, paths)
            return [p for p in keep if p != "peer.md"], drop + ["peer.md"]

        with mock.patch.object(gitutil, "quiescent", side_effect=quiescent_then_peer_stages), \
                mock.patch.object(gitutil, "split_ignored", side_effect=split):
            self.run_sweep()

        files = gitutil.run(path, "show", "--name-only", "--format=", "HEAD").out.split()
        self.assertEqual(files, ["board.md"])
        staged = gitutil.run(path, "diff", "--cached", "--name-only").out.split()
        self.assertEqual(staged, ["peer.md"])  # still staged, for its owner
        self.assertNotIn("peer.md", self.remote_files("alpha"))

    def test_deletions_are_synced(self):
        path = self.make_repo("alpha")
        self.write(path, "gone.md", "about to go\n")
        git(path, "add", "gone.md")
        git(path, "commit", "-m", "add gone")
        git(path, "push")
        os.unlink(os.path.join(path, "gone.md"))
        # Quiescence reads mtimes of dirty paths; a deletion has none, so it is quiescent.
        self.register("alpha")
        self.run_sweep()
        self.assertEqual(self.outcome("alpha"), sync_cmd.SYNCED, self.detail("alpha"))
        self.assertNotIn("gone.md", self.remote_files("alpha"))
        self.assertFalse(gitutil.is_dirty(path))

    def test_lodestarignore_and_builtins_are_honoured(self):
        path = self.make_repo("alpha")
        self.write(path, ".lodestarignore", "generated/\n*.log\n!keep.log\n", age_seconds=3600)
        self.write(path, "generated/health.json", "{}\n", age_seconds=3600)
        self.write(path, "run.log", "noise\n", age_seconds=3600)
        self.write(path, "keep.log", "wanted\n", age_seconds=3600)
        self.write(path, "~$deck.pptx", "office lock\n", age_seconds=3600)
        self.write(path, "Transient/codebase_health.json", "{}\n", age_seconds=3600)
        self.write(path, "notes.md", "real work\n", age_seconds=3600)
        self.register("alpha")
        self.run_sweep()
        files = self.remote_files("alpha")
        self.assertIn("notes.md", files)
        self.assertIn("keep.log", files)
        self.assertIn(".lodestarignore", files)
        for withheld in ("generated/health.json", "run.log", "~$deck.pptx",
                         "Transient/codebase_health.json"):
            self.assertNotIn(withheld, files)
        self.assertIn("withheld", self.detail("alpha"))

    def test_an_ignored_file_being_rewritten_does_not_hold_the_repo_open(self):
        path = self.make_repo("alpha")
        self.write(path, "notes.md", "settled\n", age_seconds=3600)
        self.write(path, "Transient/codebase_health.html", "rewritten every minute\n")
        self.register("alpha")
        self.run_sweep()
        self.assertIn("notes.md", self.remote_files("alpha"))

    def test_sweep_commits_carry_their_own_author_and_list_the_paths(self):
        path = self.make_repo("alpha")
        self.write(path, "a.md", "a\n", age_seconds=3600)
        self.write(path, "b.md", "b\n", age_seconds=3600)
        self.register("alpha")
        self.run_sweep()
        author = gitutil.run(path, "log", "-1", "--format=%an <%ae>").out.strip()
        self.assertEqual(author, gitutil.SWEEP_AUTHOR)
        body = gitutil.run(path, "log", "-1", "--format=%b").out
        self.assertIn("- a.md", body)
        self.assertIn("- b.md", body)

    def test_send_off_commits_nothing_and_says_so(self):
        path = self.make_repo("alpha")
        self.write(path, "notes.md", "someone else's\n", age_seconds=3600)
        before = self.head(path)
        self.register("alpha", send="off")
        self.run_sweep()
        self.assertEqual(self.head(path), before)
        self.assertEqual(self.outcome("alpha"), sync_cmd.SKIPPED)
        self.assertIn("send off", self.detail("alpha"))

    def test_send_off_does_not_push_local_commits(self):
        path = self.make_repo("alpha")
        self.write(path, "x.md", "x\n")
        git(path, "add", "x.md")
        git(path, "commit", "-m", "human commit")
        self.register("alpha", send="off")
        self.run_sweep()
        self.assertNotIn("x.md", self.remote_files("alpha"))
        self.assertIn("not pushed", self.detail("alpha"))


class TestReceive(SendReceiveTest):
    def test_behind_with_a_dirty_tracked_file_flags_and_pulls_nothing(self):
        path = self.make_repo("alpha")
        self.push_from_elsewhere("alpha")
        self.write(path, "README.md", "an agent's unsaved thought\n", age_seconds=3600)
        before = self.head(path)
        self.register("alpha", send="off")
        self.run_sweep()
        self.assertEqual(self.outcome("alpha"), sync_cmd.FLAGGED)
        self.assertIn("uncommitted tracked changes", self.detail("alpha"))
        self.assertEqual(self.head(path), before)
        self.assertFalse(os.path.exists(os.path.join(path, "elsewhere.md")))
        with open(os.path.join(path, "README.md"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "an agent's unsaved thought\n")
        self.assertEqual(gitutil.run(path, "stash", "list").out.strip(), "")

    def test_behind_with_only_untracked_files_fast_forwards(self):
        path = self.make_repo("alpha")
        self.push_from_elsewhere("alpha")
        self.write(path, "scratch.md", "untracked\n")  # fresh: not quiescent, not committed
        self.register("alpha", send="off")
        self.run_sweep()
        self.assertTrue(os.path.exists(os.path.join(path, "elsewhere.md")))
        self.assertTrue(os.path.exists(os.path.join(path, "scratch.md")))

    def test_diverged_with_send_off_is_flagged_and_not_rebased(self):
        path = self.make_repo("alpha")
        self.push_from_elsewhere("alpha")
        self.write(path, "mine.md", "local\n")
        git(path, "add", "mine.md")
        git(path, "commit", "-m", "local work")
        before = self.head(path)
        self.register("alpha", send="off")
        self.run_sweep()
        self.assertEqual(self.outcome("alpha"), sync_cmd.FLAGGED)
        self.assertIn("diverged", self.detail("alpha"))
        self.assertEqual(self.head(path), before)
        self.assertNotIn("mine.md", self.remote_files("alpha"))

    def test_diverged_with_send_on_and_a_clean_tree_is_rebased_and_pushed(self):
        path = self.make_repo("alpha")
        self.push_from_elsewhere("alpha")
        self.write(path, "mine.md", "local\n")
        git(path, "add", "mine.md")
        git(path, "commit", "-m", "local work")
        self.register("alpha")
        self.run_sweep()
        self.assertEqual(self.outcome("alpha"), sync_cmd.SYNCED, self.detail("alpha"))
        self.assertEqual(gitutil.ahead_behind(path), (0, 0))
        self.assertIn("mine.md", self.remote_files("alpha"))
        self.assertIn("elsewhere.md", self.remote_files("alpha"))

    def test_receive_off_leaves_inbound_work_on_the_remote(self):
        path = self.make_repo("alpha")
        self.push_from_elsewhere("alpha")
        self.register("alpha", receive="off")
        self.run_sweep()
        self.assertFalse(os.path.exists(os.path.join(path, "elsewhere.md")))
        self.assertIn("receive off", self.detail("alpha"))

    def test_the_sweep_never_uses_pull_with_autostash(self):
        path = self.make_repo("alpha")
        self.push_from_elsewhere("alpha")
        self.write(path, "notes.md", "work\n", age_seconds=3600)
        self.register("alpha")
        with mock.patch.object(gitutil, "pull_rebase",
                               side_effect=AssertionError("autostash pull used")):
            self.run_sweep()
        self.assertTrue(os.path.exists(os.path.join(path, "elsewhere.md")))
        self.assertIn("notes.md", self.remote_files("alpha"))


class TestRegistry(SendReceiveTest):
    def test_the_legacy_sync_column_still_reads(self):
        target = os.path.join(self.root, "sync", "registry.md")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("| Name | Path | Branch | Sync | Notes |\n"
                         "|------|------|--------|------|-------|\n"
                         "| a | ~/a | main | on | |\n| b | ~/b | main | off | |\n")
        rows = {e.name: e for e in registry.entries(target, role="x")}
        self.assertTrue(rows["a"].send and rows["a"].receive and rows["a"].sync)
        self.assertFalse(rows["b"].send or rows["b"].receive or rows["b"].sync)

    def test_machines_column_and_machine_path_override(self):
        self.set_registry([
            {"name": "work", "path": "~/dev/work", "machines": "desktop,laptop"},
            {"name": "RimMaster", "path": "~/dev/RimMaster", "send": "off"},
        ], machine_paths=[("RimMaster", "home", "~/dev/Rimworld")])
        home = registry.participating(role="home", key="tower")
        self.assertEqual([e.name for e in home], ["RimMaster"])
        self.assertEqual(home[0].path, os.path.expanduser("~/dev/Rimworld"))
        self.assertFalse(home[0].send)
        self.assertTrue(home[0].receive)
        self.assertEqual([e.name for e in registry.elsewhere(role="home", key="tower")],
                         ["work"])
        desk = registry.participating(role="desktop", key="mt-1")
        self.assertEqual(sorted(e.name for e in desk), ["RimMaster", "work"])
        self.assertEqual([e for e in desk if e.name == "RimMaster"][0].path,
                         os.path.expanduser("~/dev/RimMaster"))

    @needs_private("sync/registry.md", "sync/private-store.json")
    def test_the_real_registry_keeps_private_host_repos_off_home(self):
        from lib import boundary
        real = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "sync", "registry.md")
        host = boundary.load(boundary.DEFAULT_CONFIG)["host"]
        private = [e.name for e in registry.entries(real, role="home", key=real_home_key()) if host in (e.remote or "")]
        self.assertGreaterEqual(len(private), 3, "the real registry has private-host rows")
        names = [e.name for e in registry.participating(real, role="home", key=real_home_key())]
        for name in private:
            self.assertNotIn(name, names)
        # The non-vacuity control: without it, a filter returning NOTHING would satisfy every
        # assertNotIn above and this test would pass while checking actually nothing. Renamed
        # from `RimMaster` on 2026-10-01 — the row became `RimMandrake` the night before and
        # this assertion is the only one here that reads the REAL registry, so it was the only
        # one that went red. The synthetic-fixture rows above still say `RimMaster` on purpose:
        # they build their own table and never consult `sync/registry.md`.
        self.assertIn("RimMandrake", names)

    def test_remote_normalisation(self):
        same = ["git@github.com:Lmandrake/RimMandrake.git",
                "https://github.com/lmandrake/rimmandrake",
                "ssh://git@github.com/Lmandrake/RimMandrake.git/"]
        for url in same:
            self.assertEqual(skillclone.normalize_remote(url), "github.com/lmandrake/rimmandrake")
        self.assertFalse(skillclone.same_remote(same[0], "git@github.com:Lmandrake/Other.git"))
        self.assertTrue(skillclone.is_ssh_url(same[0]))
        self.assertFalse(skillclone.is_ssh_url(same[1]))


class TestAdoptBeforeClone(SendReceiveTest):
    def setUp(self):
        super(TestAdoptBeforeClone, self).setUp()
        self.dev = os.path.join(self.tmp, "dev")
        os.makedirs(self.dev)
        self._dev_saved = os.environ.get("LODESTAR_DEV_ROOT")
        os.environ["LODESTAR_DEV_ROOT"] = self.dev
        self.addCleanup(self._restore_dev)
        self.remote = os.path.join(self.tmp, "RimMandrake.git")
        subprocess.run(["git", "init", "--bare", "-b", "main", self.remote], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        seed = os.path.join(self.tmp, "seed")
        subprocess.run(["git", "clone", self.remote, seed], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        git(seed, "config", "user.email", "t@example.com")
        git(seed, "config", "user.name", "T")
        self.write(seed, "README.md", "rim\n")
        git(seed, "add", "-A")
        git(seed, "commit", "-m", "seed")
        git(seed, "push", "-u", "origin", "main")
        with open(os.path.join(self.root, "sync", "remotes.md"), "w",
                  encoding="utf-8") as handle:
            handle.write("| Name | Remote |\n|------|--------|\n| RimMaster | %s |\n"
                         % self.remote)

    def _restore_dev(self):
        if self._dev_saved is None:
            os.environ.pop("LODESTAR_DEV_ROOT", None)
        else:
            os.environ["LODESTAR_DEV_ROOT"] = self._dev_saved

    def checkout(self, name):
        path = os.path.join(self.dev, name)
        subprocess.run(["git", "clone", self.remote, path], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return path

    def test_a_differently_named_checkout_is_adopted_not_duplicated(self):
        rimworld = self.checkout("Rimworld")
        self.set_registry([{"name": "RimMaster", "path": os.path.join(self.dev, "RimMaster"),
                            "send": "off"}])
        self.run_sweep()
        self.assertFalse(os.path.exists(os.path.join(self.dev, "RimMaster")))
        rows = registry.participating(role="testbox", key="test-box")
        self.assertEqual(rows[0].path, rimworld)
        self.assertIn("adopted", " ".join(self.actions("clones")))
        self.assertIn("RimMaster", self.status()["repos"])
        self.assertEqual(self.status()["repos"]["RimMaster"]["path"], rimworld)

    def test_dry_run_reports_the_adoption_and_writes_nothing(self):
        self.checkout("Rimworld")
        self.set_registry([{"name": "RimMaster", "path": os.path.join(self.dev, "RimMaster")}])
        before = open(os.path.join(self.root, "sync", "registry.md"), encoding="utf-8").read()
        self.run_sweep("--dry-run")
        after = open(os.path.join(self.root, "sync", "registry.md"), encoding="utf-8").read()
        self.assertEqual(before, after)
        self.assertIn("would adopt", " ".join(self.actions("clones")))

    def test_two_candidate_checkouts_are_refused_not_guessed(self):
        self.checkout("Rimworld")
        self.checkout("Rimworld_old")
        self.set_registry([{"name": "RimMaster", "path": os.path.join(self.dev, "RimMaster")}])
        self.run_sweep()
        self.assertFalse(os.path.exists(os.path.join(self.dev, "RimMaster")))
        self.assertIn("2 checkouts", " ".join(self.actions("clones")))

    def test_discover_lists_unregistered_checkouts_and_names_twins(self):
        self.checkout("Rimworld")
        other = os.path.join(self.dev, "Loose")
        subprocess.run(["git", "init", "-q", other], check=True)
        self.set_registry([{"name": "RimMaster", "path": os.path.join(self.dev, "RimMaster")}])
        found = dict((os.path.basename(path), note) for path, _, note in sync_cmd.discover())
        self.assertEqual(set(found), {"Rimworld", "Loose"})
        self.assertIn("RimMaster", found["Rimworld"])


class TestLockPolicy(SendReceiveTest):
    def test_flag_policy_never_deletes_a_stale_lock(self):
        path = self.make_repo("alpha")
        lock = os.path.join(path, ".git", "index.lock")
        open(lock, "w").close()
        old = time.time() - 3600
        os.utime(lock, (old, old))
        with mock.patch.object(gitutil, "ALLOW_LOCK_REMOVAL", False), \
                mock.patch.object(gitutil, "lock_holders", return_value=[]), \
                contextlib.redirect_stderr(io.StringIO()):
            removed, note = gitutil.remove_stale_lock(path)
        self.assertFalse(removed)
        self.assertIn("never deletes", note)
        self.assertTrue(os.path.exists(lock))

    def test_the_policy_is_read_from_machines_md(self):
        target = os.path.join(self.root, "machines.md")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("| Key | Role | Locks |\n|-----|------|-------|\n"
                         "| test-box | testbox | flag |\n")
        self.assertEqual(machine.lock_policy(target), machine.LOCKS_FLAG)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("| Key | Role |\n|-----|------|\n| test-box | testbox |\n")
        self.assertEqual(machine.lock_policy(target), machine.LOCKS_REMOVE)


class TestPause(SendReceiveTest):
    def pause(self, path, until_ago=None, set_ago=0, directions=("send", "receive")):
        now = time.time()
        payload = repopause.set_pause(path, 3600, reason="heavy work", set_by="test",
                                      directions=directions, now=now - set_ago)
        if until_ago is not None:
            payload["until"] = repopause._iso(now - until_ago)
            with open(repopause.pause_path(path), "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
        return payload

    def test_a_pause_blocks_send_and_receive(self):
        path = self.make_repo("alpha")
        self.push_from_elsewhere("alpha")
        self.write(path, "notes.md", "work\n", age_seconds=3600)
        before = self.head(path)
        self.pause(path)
        self.register("alpha")
        self.run_sweep()
        self.assertEqual(self.outcome("alpha"), sync_cmd.PAUSED)
        self.assertIn("heavy work", self.detail("alpha"))
        self.assertEqual(self.head(path), before)
        self.assertNotIn("notes.md", self.remote_files("alpha"))
        self.assertTrue(gitutil.is_dirty(path))  # still dirty: nothing committed
        # The pause file is inside .git: it never dirties the tree.
        self.assertNotIn("lodestar-pause", " ".join(gitutil.changed_paths(path)))

    def test_a_send_only_pause_still_receives(self):
        path = self.make_repo("alpha")
        self.push_from_elsewhere("alpha")
        self.pause(path, directions=("send",))
        self.register("alpha")
        self.run_sweep()
        self.assertTrue(os.path.exists(os.path.join(path, "elsewhere.md")))

    def test_expiry_on_a_quiet_repo_resumes(self):
        path = self.make_repo("alpha", days_old=1)
        self.write(path, "notes.md", "work\n", age_seconds=7200)
        self.pause(path, until_ago=60, set_ago=9 * 3600)
        self.register("alpha")
        self.run_sweep()
        self.assertIsNone(repopause.read(path))
        self.assertIn("notes.md", self.remote_files("alpha"))

    def test_expiry_on_an_active_repo_extends(self):
        path = self.make_repo("alpha", days_old=1)
        self.write(path, "notes.md", "still typing\n", age_seconds=60)
        self.pause(path, until_ago=60, set_ago=9 * 3600)
        self.register("alpha")
        self.run_sweep()
        self.assertEqual(self.outcome("alpha"), sync_cmd.PAUSED)
        self.assertIn("extended (active)", self.detail("alpha"))
        until = repopause._epoch(repopause.read(path)["until"])
        self.assertGreater(until, time.time() + 50 * 60)

    def test_the_24h_cap_resumes_anyway_and_says_so_loudly(self):
        path = self.make_repo("alpha", days_old=1)
        self.write(path, "notes.md", "work\n", age_seconds=3600)
        self.write(path, "fresh.md", "active\n", age_seconds=60)
        self.pause(path, until_ago=60, set_ago=25 * 3600)
        self.register("alpha")
        self.run_sweep()
        self.assertIsNone(repopause.read(path))
        self.assertEqual(self.outcome("alpha"), sync_cmd.FLAGGED)
        self.assertIn("force-lifted", self.detail("alpha"))

    def test_the_cli_pauses_resumes_and_lists_by_path(self):
        path = self.make_repo("alpha")
        self.register("alpha")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(sync_cmd.main(["--pause", path, "2h", "--reason", "refactor"]), 0)
            self.assertEqual(sync_cmd.main(["--paused"]), 0)
        self.assertIn("refactor", out.getvalue())
        self.assertEqual(repopause.read(path)["directions"], ["send", "receive"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sync_cmd.main(["--resume", "alpha"]), 0)
        self.assertIsNone(repopause.read(path))


if __name__ == "__main__":
    unittest.main()
