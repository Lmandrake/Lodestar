"""Tests for lib/mirrors.py -- github.com <-> private-host replication (design 2026-10-19 v2).

Every remote is a temp bare repo. The registry URLs stay the real-shaped ones
(git@github.com:..., https://git.private.example/...) and a throwaway global git config maps
them onto the bare repos with `insteadOf`, so the host check (D2) is exercised for real; the
one seam is `mirrors._resolved_host`, which maps a temp path back to the host it stands for.
"""

import contextlib
import io
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest

from lib import mirrors, registry, validate

ORIGIN_URL = "git@github.com:Lmandrake/Thing.git"
MIRROR_URL = "https://git.private.example/mandrake/Thing.git"

MACHINES = """| Key | Role | Locks | Labels | Hosts | Notes |
|-----|------|-------|--------|-------|-------|
| test-box | testbox | | mac | github.com, git.private.example | both |
| home-box | home | flag | home | github.com | github only |
"""


def git(cwd, *args, **kwargs):
    return subprocess.run(["git", "-C", cwd] + list(args), check=kwargs.get("check", True),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE
                          ).stdout.decode().strip()


class MirrorCase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="lodestar-mirrors-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._env = {k: os.environ.get(k) for k in
                     ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "LODESTAR_STATE_DIR")}
        self.addCleanup(self._restore)
        self.gitconfig = os.path.join(self.tmp, "gitconfig")
        open(self.gitconfig, "w").close()
        os.environ["GIT_CONFIG_GLOBAL"] = self.gitconfig
        os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
        os.environ["LODESTAR_STATE_DIR"] = os.path.join(self.tmp, "state")
        self.machines = os.path.join(self.tmp, "machines.md")
        with open(self.machines, "w", encoding="utf-8") as handle:
            handle.write(MACHINES)

        self.origin = self.bare("origin.git")
        self.mirror = self.bare("mirror.git")
        self.route(ORIGIN_URL, self.origin)
        self.route(MIRROR_URL, self.mirror)
        self.hosts = {self.origin: "github.com", self.mirror: "git.private.example"}
        saved = mirrors._resolved_host
        self.addCleanup(setattr, mirrors, "_resolved_host", saved)
        mirrors._resolved_host = self.fake_host
        self.addCleanup(setattr, mirrors, "_before_push", None)
        saved_timeout = mirrors.CALL_TIMEOUT
        self.addCleanup(setattr, mirrors, "CALL_TIMEOUT", saved_timeout)

        self.work = self.clone(self.origin, "work")
        self.commit(self.work, "README.md", "seed\n", "seed")
        git(self.work, "push", "-q", "origin", "main")
        git(self.work, "push", "-q", self.mirror, "main")
        self.entry = registry.Entry(
            name="Thing", path=self.work, branch="main", sync=True, notes="", send=True,
            receive=True, machines=(), repo_id="thing", remote=ORIGIN_URL, state="active",
            mirror=MIRROR_URL)

    def _restore(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def fake_host(self, resolved):
        for path, host in self.hosts.items():
            if resolved.rstrip("/") == path:
                return host
        return mirrors.skillclone.remote_host(resolved)

    def bare(self, name):
        path = os.path.join(self.tmp, name)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", path], check=True)
        return path

    def route(self, url, target):
        """Point `url` at `target`, replacing any earlier route for the same url."""
        existing = git(self.tmp, "config", "--global", "--get-regexp", r"^url\..*\.insteadof$",
                       check=False)
        for line in existing.splitlines():
            name, _, value = line.partition(" ")
            if value == url:
                git(self.tmp, "config", "--global", "--unset-all", name)
        git(self.tmp, "config", "--global", "url.%s.insteadOf" % target, url)

    def clone(self, source, name):
        path = os.path.join(self.tmp, name)
        subprocess.run(["git", "clone", "-q", source, path], check=True,
                       stderr=subprocess.DEVNULL)
        git(path, "config", "user.email", "t@example.com")
        git(path, "config", "user.name", "T")
        git(path, "config", "commit.gpgsign", "false")
        git(path, "checkout", "-q", "-B", "main")
        return path

    def commit(self, repo, filename, text, message, email=None):
        with open(os.path.join(repo, filename), "w", encoding="utf-8") as handle:
            handle.write(text)
        git(repo, "add", filename)
        args = ["commit", "-q", "-m", message]
        if email:
            args = ["-c", "user.email=" + email] + args
        git(repo, *args)
        return git(repo, "rev-parse", "HEAD")

    def on_mirror(self, filename, text, message="private side", email="colleague@example.org"):
        side = os.path.join(self.tmp, "private-%d" % len(os.listdir(self.tmp)))
        subprocess.run(["git", "clone", "-q", self.mirror, side], check=True,
                       stderr=subprocess.DEVNULL)
        git(side, "config", "user.name", "C")
        git(side, "config", "commit.gpgsign", "false")
        oid = self.commit(side, filename, text, message, email=email)
        git(side, "push", "-q", "origin", "main")
        return oid

    def on_origin(self, filename, text, message="github side"):
        git(self.work, "pull", "-q", "--ff-only", "origin", "main", check=False)
        oid = self.commit(self.work, filename, text, message)
        git(self.work, "push", "-q", "origin", "main")
        return oid

    def tip(self, bare):
        return git(bare, "rev-parse", "refs/heads/main", check=False)

    def replicate(self, **kwargs):
        return mirrors.replicate(self.entry, cache_root=os.path.join(self.tmp, "cache"), **kwargs)


class TestReplication(MirrorCase):
    def test_level(self):
        outcome = self.replicate()
        self.assertEqual(outcome.state, mirrors.LEVEL, outcome.detail)
        self.assertEqual(outcome.origin_tip, self.tip(self.origin))

    def test_mirror_behind_is_pushed(self):
        new = self.on_origin("a.txt", "a\n")
        outcome = self.replicate()
        self.assertEqual((outcome.state, outcome.direction),
                         (mirrors.PUSHED, mirrors.MIRROR_BEHIND), outcome.detail)
        self.assertEqual(self.tip(self.mirror), new)
        self.assertEqual(self.replicate().state, mirrors.LEVEL)

    def test_origin_behind_clean_gate_pushes_to_origin(self):
        new = self.on_mirror("b.txt", "plain words\n")
        outcome = self.replicate()
        self.assertEqual((outcome.state, outcome.direction),
                         (mirrors.PUSHED, mirrors.ORIGIN_BEHIND), outcome.detail)
        self.assertEqual(self.tip(self.origin), new)
        self.assertEqual(outcome.colleague_commits, 1)
        self.assertIn("colleague", outcome.detail)

    def test_diverged_clean_merge_levels_both(self):
        self.on_origin("a.txt", "a\n")
        self.on_mirror("b.txt", "b\n")
        outcome = self.replicate()
        self.assertEqual((outcome.state, outcome.direction),
                         (mirrors.PUSHED, mirrors.MERGED), outcome.detail)
        self.assertEqual(self.tip(self.origin), self.tip(self.mirror))
        merge = self.tip(self.origin)
        self.assertEqual(len(git(self.origin, "rev-list", "--parents", "-n1", merge).split()), 3)
        self.assertEqual(git(self.origin, "log", "-1", "--format=%ae", merge),
                         "owner@public.example")
        self.assertEqual(self.replicate().state, mirrors.LEVEL)

    def test_conflict_pushes_nothing(self):
        self.on_origin("README.md", "github\n")
        self.on_mirror("README.md", "private\n")
        before = (self.tip(self.origin), self.tip(self.mirror))
        outcome = self.replicate()
        self.assertEqual(outcome.state, mirrors.CONFLICT, outcome.detail)
        self.assertIn("needs you", outcome.detail)
        self.assertEqual((self.tip(self.origin), self.tip(self.mirror)), before)

    def test_gate_hit_is_held(self):
        before = self.tip(self.origin)
        self.on_mirror("c.txt", "ask jane.doe@work.private.example about it\n")
        outcome = self.replicate()
        self.assertEqual(outcome.state, mirrors.HELD, outcome.detail)
        self.assertEqual(self.tip(self.origin), before)

    def test_gate_reads_commit_messages_too(self):
        before = self.tip(self.origin)
        self.on_mirror("d.txt", "fine\n", message="per jane.doe@work.private.example")
        self.assertEqual(self.replicate().state, mirrors.HELD)
        self.assertEqual(self.tip(self.origin), before)

    def test_gate_holds_a_diverged_merge(self):
        self.on_origin("a.txt", "a\n")
        before = (self.tip(self.origin), None)
        self.on_mirror("b.txt", "jane.doe@work.private.example\n")
        self.assertEqual(self.replicate().state, mirrors.HELD)
        self.assertEqual(self.tip(self.origin), before[0])

    def test_recreated_empty_mirror_is_recreated(self):
        shutil.rmtree(self.mirror)
        self.bare("mirror.git")
        outcome = self.replicate()
        self.assertEqual((outcome.state, outcome.direction),
                         (mirrors.PUSHED, mirrors.MIRROR_CREATED), outcome.detail)
        self.assertEqual(self.tip(self.mirror), self.tip(self.origin))

    def test_missing_on_origin_is_configuration_error(self):
        shutil.rmtree(self.origin)
        self.bare("origin.git")
        outcome = self.replicate()
        self.assertEqual(outcome.state, mirrors.CONFIG_ERROR, outcome.detail)
        self.assertFalse(self.tip(self.origin).startswith(self.tip(self.mirror)[:8]))

    def test_raced_then_level(self):
        self.on_origin("a.txt", "a\n")
        racer = {"done": False}

        def race(side):
            if side != "mirror" or racer["done"]:
                return
            racer["done"] = True
            newer = self.on_origin("race.txt", "r\n")   # another Mac moved origin ...
            git(self.work, "push", "-q", self.mirror, "%s:refs/heads/main" % newer)  # ... and mirrored it

        mirrors._before_push = race
        outcome = self.replicate()
        self.assertEqual(outcome.state, mirrors.RACED, outcome.detail)
        mirrors._before_push = None
        self.assertEqual(self.replicate().state, mirrors.LEVEL)

    def test_unreachable_mirror_is_transport_unknown(self):
        self.route(MIRROR_URL, "https://127.0.0.1:1/x.git")
        self.hosts["https://127.0.0.1:1/x.git"] = "git.private.example"
        outcome = self.replicate()
        self.assertEqual(outcome.state, mirrors.TRANSPORT_UNKNOWN, outcome.detail)

    def test_insteadof_to_another_host_is_configuration_error(self):
        self.route(MIRROR_URL, "https://gitlab.example.com/mandrake/Thing.git")
        outcome = self.replicate()
        self.assertEqual(outcome.state, mirrors.CONFIG_ERROR, outcome.detail)
        self.assertIn("gitlab.example.com", outcome.detail)

    def test_follow_tags_config_publishes_no_tag(self):
        git(self.tmp, "config", "--global", "push.followTags", "true")
        self.on_origin("a.txt", "a\n")
        git(self.work, "tag", "-a", "-m", "t", "v1")
        git(self.work, "push", "-q", "origin", "v1")
        self.assertEqual(self.replicate().state, mirrors.PUSHED)
        self.assertEqual(git(self.mirror, "tag"), "")

    def test_deadline_kills_a_hung_git(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)  # never accepted: the TLS handshake hangs
        self.addCleanup(listener.close)
        hung = "https://127.0.0.1:%d/x.git" % listener.getsockname()[1]
        self.route(MIRROR_URL, hung)
        self.hosts[hung] = "git.private.example"
        mirrors.CALL_TIMEOUT = 1.0
        started = time.monotonic()
        outcome = self.replicate()
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(outcome.state, mirrors.TRANSPORT_UNKNOWN, outcome.detail)

    def test_dry_run_pushes_nothing(self):
        self.on_origin("a.txt", "a\n")
        before = self.tip(self.mirror)
        outcome = self.replicate(dry_run=True)
        self.assertEqual(outcome.state, mirrors.DRY_RUN)
        self.assertEqual(self.tip(self.mirror), before)

    def test_working_checkout_remotes_are_untouched(self):
        before = git(self.work, "config", "--get-regexp", r"^remote\.")
        self.on_origin("a.txt", "a\n")
        self.replicate()
        self.assertEqual(git(self.work, "config", "--get-regexp", r"^remote\."), before)


class TestRunAndSurfacing(MirrorCase):
    def run_all(self, key="test-box", role="testbox", **kwargs):
        return mirrors.run([self.entry], role, key, machines_path=self.machines,
                           cache_root=os.path.join(self.tmp, "cache"),
                           memory_path=os.path.join(self.tmp, "memory.json"), **kwargs)

    def test_ineligible_machine_makes_no_git_call(self):
        def boom(*args, **kwargs):
            raise AssertionError("no git call expected")
        saved = mirrors._run
        mirrors._run = boom
        try:
            (outcome,) = self.run_all(key="home-box", role="home")
        finally:
            mirrors._run = saved
        self.assertEqual(outcome.state, mirrors.INELIGIBLE)
        self.assertEqual(outcome.surface, mirrors.SILENT)

    def test_level_records_confirmed_at_and_is_silent(self):
        (outcome,) = self.run_all()
        self.assertEqual((outcome.state, outcome.surface), (mirrors.LEVEL, mirrors.SILENT))
        self.assertTrue(outcome.confirmed_at)

    def test_transport_unknown_turns_amber_after_a_day_unconfirmed(self):
        self.run_all(now=1000.0)
        self.route(MIRROR_URL, "https://127.0.0.1:1/x.git")
        self.hosts["https://127.0.0.1:1/x.git"] = "git.private.example"
        (soon,) = self.run_all(now=1000.0 + 3600)
        self.assertEqual((soon.state, soon.surface), (mirrors.TRANSPORT_UNKNOWN, mirrors.NOTE))
        (later,) = self.run_all(now=1000.0 + 25 * 3600)
        self.assertEqual(later.surface, mirrors.AMBER)
        self.assertIn("unverified", later.detail)

    def test_refused_flags_at_most_once_a_day(self):
        shutil.rmtree(self.mirror)  # local path gone: "does not appear to be a git repository"
        (first,) = self.run_all(now=86400.0 * 100)
        self.assertEqual((first.state, first.surface), (mirrors.REFUSED, mirrors.FLAGGED))
        (second,) = self.run_all(now=86400.0 * 100 + 60)
        self.assertEqual(second.surface, mirrors.NOTE)

    def test_dry_run_records_nothing(self):
        self.run_all(dry_run=True)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "memory.json")))

    def test_payload_and_summary_line(self):
        outcomes = self.run_all()
        payload = mirrors.status_payload(outcomes)
        self.assertEqual(payload["repos"]["thing"]["state"], "level")
        self.assertEqual(payload["actions"], "mirrors: 1 level")


class TestSweepIntegration(MirrorCase):
    """An unreachable mirror changes nothing about the repo's own sweep outcome."""

    def setUp(self):
        MirrorCase.setUp(self)
        import sync as sync_cmd
        self.sync = sync_cmd
        self.board = os.path.join(self.tmp, "board")
        os.makedirs(os.path.join(self.board, "loops"))
        os.makedirs(os.path.join(self.board, "sync"))
        shutil.copy(self.machines, os.path.join(self.board, "machines.md"))
        saved = {k: os.environ.get(k) for k in
                 ("LODESTAR_ROOT", "LODESTAR_MACHINE", "LODESTAR_RUNTIME", "HOME")}

        def restore():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.addCleanup(restore)
        os.environ.update({"LODESTAR_ROOT": self.board, "LODESTAR_MACHINE": "test-box",
                           "LODESTAR_RUNTIME": "cc", "HOME": self.tmp})
        with open(os.path.join(self.board, "sync", "registry.md"), "w") as handle:
            handle.write("| Name | Path | Branch | Send | Receive | Machines | Id | Former | "
                         "Remote | State | Mirror | Notes |\n"
                         "|---|---|---|---|---|---|---|---|---|---|---|---|\n"
                         "| Thing | %s | main | on | on |  | thing |  | %s | active | %s | n |\n"
                         % (self.work, ORIGIN_URL, MIRROR_URL))

    def sweep(self):
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.sync.main(["--only", "Thing"])
        with open(os.path.join(self.tmp, "state", "sync-status.json")) as handle:
            return json.load(handle)

    def test_unreachable_mirror_leaves_repo_outcome_unchanged(self):
        self.route(MIRROR_URL, "https://127.0.0.1:1/x.git")
        self.hosts["https://127.0.0.1:1/x.git"] = "git.private.example"
        status = self.sweep()
        self.assertEqual(status["repos"]["Thing"]["outcome"], self.sync.CLEAN)
        self.assertEqual(status["mirrors"]["repos"]["thing"]["state"],
                         mirrors.TRANSPORT_UNKNOWN)
        self.assertIn("mirrors:", status["mirrors"]["actions"])

    def test_sweep_levels_a_mirror(self):
        new = self.on_origin("a.txt", "a\n")
        status = self.sweep()
        self.assertEqual(status["mirrors"]["repos"]["thing"]["state"],
                         "pushed:" + mirrors.MIRROR_BEHIND)
        self.assertEqual(self.tip(self.mirror), new)


class TestValidator(unittest.TestCase):
    HEADER = ("| Name | Path | Branch | Send | Receive | Machines | Id | Former | Remote | State "
              "| Mirror | Notes |\n|---|---|---|---|---|---|---|---|---|---|---|---|\n")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-mirror-validate-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.machines = os.path.join(self.tmp, "machines.md")
        with open(self.machines, "w") as handle:
            handle.write(MACHINES)
        self.tombstones = os.path.join(self.tmp, "tombstones.md")

    def kinds(self, *rows):
        path = os.path.join(self.tmp, "registry.md")
        with open(path, "w") as handle:
            handle.write(self.HEADER + "".join(
                "| %s | ~/dev/%s | main | %s | on | %s | %s |  | %s | active | %s | n |\n"
                % (name, name, send, machines, name.lower(), remote, mirror)
                for name, remote, mirror, machines, send in rows))
        found = validate.faults(path, self.machines, self.tombstones)
        blocking = {f.kind for f in validate.blocking(found) if f.kind.startswith("mirror")}
        every = {f.kind for f in found if f.kind.startswith("mirror")}
        return blocking, every

    def row(self, name="Thing", remote="git@github.com:Lmandrake/Thing.git",
            mirror="https://git.private.example/mandrake/Thing.git", machines="", send="on"):
        return (name, remote, mirror, machines, send)

    def test_a_good_row_has_no_fault(self):
        self.assertEqual(self.kinds(self.row()), (set(), set()))

    def test_ssh_mirror_is_blocking(self):
        blocking, _ = self.kinds(self.row(mirror="ssh://git.private.example/mandrake/Thing.git"))
        self.assertIn("mirror-not-https", blocking)

    def test_mirror_equal_to_remote_is_blocking(self):
        blocking, _ = self.kinds(self.row(mirror="https://github.com/Lmandrake/Thing.git"))
        self.assertIn("mirror-equals-remote", blocking)

    def test_unsupported_host_pair_is_blocking(self):
        blocking, _ = self.kinds(self.row(mirror="https://gitlab.com/x/Thing.git"))
        self.assertIn("mirror-host-pair", blocking)

    def test_no_capable_machine_is_blocking(self):
        blocking, _ = self.kinds(self.row(machines="home"))
        self.assertIn("mirror-unreplicable", blocking)
        blocking, _ = self.kinds(self.row(send="off"))
        self.assertIn("mirror-unreplicable", blocking)

    def test_shared_mirror_is_blocking(self):
        blocking, _ = self.kinds(self.row(), self.row(name="Other",
                                                      remote="git@github.com:Lmandrake/Other.git"))
        self.assertIn("mirror-duplicate", blocking)

    def test_mail_class_row_is_blocking(self):
        blocking, _ = self.kinds(self.row(name="Mail",
                                          remote="git@github.com:Lmandrake/Mail.git",
                                          mirror="https://git.private.example/mandrake/Mail.git"))
        self.assertIn("mirror-on-mail-class", blocking)

    def test_name_mismatch_is_non_blocking(self):
        blocking, every = self.kinds(self.row(
            mirror="https://git.private.example/mandrake/Elsewhere.git"))
        self.assertNotIn("mirror-name-mismatch", blocking)
        self.assertIn("mirror-name-mismatch", every)


if __name__ == "__main__":
    unittest.main()
