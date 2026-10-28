"""Drive all three lifecycle executors against REAL throwaway repos.

`_do_rename`, `_do_rehome` and `_do_quarantine` shipped complete and covered only at the
module level: `relocate.move`, `relocate.quarantine`, `satellites.find` and
`gitutil.remote_endpoints` each had unit tests, but no test had ever run the lifecycle
*executor* that calls them — so none had touched a real filesystem or a real git config.
That is this repo's signature defect (see CLAUDE.md §7: a subsystem with green tests and no
production caller looks exactly like a working feature), and the 2026-09-30 handoff recorded
rehome and quarantine as "written but has never run".

**Writing these found a bug that could never not have fired.** `_do_rehome` tested
`getattr(result, "returncode", 1) != 0`, but `GitResult` exposes `.code` — so the default
always won and every rehome refused while its `set-url` had actually succeeded. Unit tests on
the callees were green throughout; the attribute name lives in the caller.

Everything here is local: `git init` repos under a temp directory with fake remote URLs.
No network, no bare remote, no fetch or push is ever attempted — `_do_rehome` only rewrites
and reads local config.

**The stores and the home directory are redirected, and that is load-bearing.** The executors
call `transitions.record` / `transitions.finish` / `tombstones.append` with no `path` argument
and `satellites.find` with no `home`, so they resolve the real machine-local stores under
`~/dev/.lodestar/` and the real `~/.claude`. A test that let them do that would write
throwaway rows into live state, plant a tombstone for a fake repo id, and read Lukas's actual
auto-memory directories. `paths.lifecycle_transitions_file`, `paths.tombstones_file`,
`lifecycle.HOLDING_ROOT` and `satellites.find`'s three roots are all patched per test.

`satellites.find` is wrapped rather than stubbed — the real finder logic runs, against a fake
home — because stubbing it would make the `partial` path assert on nothing but the stub. The
wrapper also records the kwargs the production caller used, so `RenameSatelliteScanTest` can
assert that `_do_rename` really passes both roots rather than inferring it from the source.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

import time

import lifecycle
from lib import (freshness, paths, plans, satellites,  # noqa: E402
                 tombstones, transitions)
from tests import needs_private  # noqa: E402

PUBLIC = "https://github.com/example/repo.git"
INTERNAL = "git@internal.example.com:example/repo.git"
STRAY_PUSH = "https://github.com/example/mirror.git"


def git(repo, *args):
    subprocess.run(["git", "-C", repo] + list(args), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def plan(kind, **over):
    """A Plan with every field populated, so a NamedTuple field added later fails loudly."""
    fields = dict(
        plan_id="exercise-%s-20260930" % kind,
        kind=kind,
        repo_id="throwaway",
        machines=("all",),
        old="",
        new="",
        disposition="",
        created_at="2026-09-30T16:00:00.000000-07:00",
        created_by="test",
        note="exercising the executor",
    )
    fields.update(over)
    return plans.Plan(**fields)


class ExecutorTestCase(unittest.TestCase):
    """Temp dir, redirected stores, and a real git repo factory."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-executors-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self.transitions_file = os.path.join(self.tmp, "transitions.md")
        self.tombstones_file = os.path.join(self.tmp, "tombstones.md")
        self.holding = os.path.join(self.tmp, "quarantine")
        self.home = os.path.join(self.tmp, "home")
        self.waypoint = os.path.join(self.home, "dev", "Waypoint")
        os.makedirs(os.path.join(self.home, ".claude", "projects"))

        self._patch(paths, "lifecycle_transitions_file", lambda: self.transitions_file)
        self._patch(paths, "tombstones_file", lambda: self.tombstones_file)
        self._patch(lifecycle, "HOLDING_ROOT", self.holding)

        # Wrap, don't stub: the real four finders run, against a fake home / repo / Waypoint.
        #
        # 🔴 The wrapper OVERRIDES whatever roots the production caller passes, and records
        # them. Overriding is the isolation: since 2026-10-01 `_do_rename` passes the real
        # `paths.root()` and the real `~/dev/Waypoint`, so a wrapper that honoured them would
        # read this repo's committed snapshot-memory tree and his 60 live shell shortcuts.
        # Recording is the proof: `self.find_kwargs` is what lets a test assert the production
        # caller supplied both roots at all, which a source-text check can only approximate.
        real_find = satellites.find
        self.find_kwargs = []

        def wrapped_find(old, new, **kwargs):
            self.find_kwargs.append(dict(kwargs))
            return real_find(old, new, home=self.home, repo_root=None,
                             waypoint_root=self.waypoint)

        self._patch(satellites, "find", wrapped_find)

    def _patch(self, module, name, value):
        original = getattr(module, name)
        setattr(module, name, value)
        self.addCleanup(setattr, module, name, original)

    def waypoint_row(self, filename, name, path, machine="", status="active"):
        """A shortcut row inside the FIXTURE's Waypoint root, never the real one.

        🔴 No test may touch `~/dev/Waypoint`'s tables — 60 live shell shortcuts. This writes
        into `self.waypoint` under the tempdir, and that root has no `waypoint` executable
        unless a test plants a stub, so even a forgotten runner cannot reach the real CLI.
        """
        os.makedirs(self.waypoint, exist_ok=True)
        file_path = os.path.join(self.waypoint, filename)
        if not os.path.exists(file_path):
            with open(file_path, "w", encoding="utf-8") as fh:
                fh.write("| Name | Path | Added | Status | Notes | Machine |\n"
                         "|------|------|-------|--------|-------|---------|\n")
        with open(file_path, "a", encoding="utf-8") as fh:
            fh.write("| %s | %s | 2026-10-01 | %s |  | %s |\n" % (name, path, status, machine))
        return file_path

    def repo(self, name="throwaway", origin=PUBLIC):
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        subprocess.run(["git", "init", "--quiet", "-b", "main", path], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if origin:
            git(path, "remote", "add", "origin", origin)
        return path


class RenameSatelliteScanTest(ExecutorTestCase):
    """🔴 Ruling 2, 2026-10-01: the rename path's satellite scan is WIRED.

    `satellites.find`'s `repo_root` and `waypoint_root` have no defaults, so until this the
    production caller passed neither and `snapshot-memory` and `shortcut` were found by
    **nothing** — two of four kinds inert behind a report that said four kinds were checked
    independently. Asserted through the recorded call rather than from the source text, because
    a source check cannot tell a passed argument from a mentioned one.

    His ruling fixed the ORDER too: shortcut rows had to become followable first. These tests
    are the second half of that order and are meaningless without the first.
    """

    def test_do_rename_really_passes_both_roots_to_the_census(self):
        old = self.repo("Old")
        new = os.path.join(self.tmp, "New")

        lifecycle._do_rename(plan("rename", old=old, new=new), old, new, "fp", False, [])

        self.assertEqual(1, len(self.find_kwargs), "the census must be called exactly once")
        passed = self.find_kwargs[0]
        for name in ("repo_root", "waypoint_root"):
            self.assertIn(name, passed, "%s was not passed, so that kind is found by nothing"
                                        % name)
            self.assertTrue(passed[name], "%s was passed empty: %r" % (name, passed[name]))
        self.assertTrue(passed["waypoint_root"].endswith(os.path.join("dev", "Waypoint")))

    def test_a_shortcut_row_is_FOLLOWED_end_to_end_and_the_rename_converges(self):
        """The composition ruling 1 exists for: census -> followable -> `follow` -> converged.

        The fixture's Waypoint root holds no `waypoint` executable, so the follow is refused
        and the run is `partial` — which is itself the honest outcome and the second assertion
        here. What this proves is that the shortcut row is now REACHED and attempted at all;
        before ruling 1 it was unfollowable by construction and before ruling 2 it was not even
        found.
        """
        old = self.repo("Old")
        new = os.path.join(self.tmp, "New")
        self.waypoint_row("waypoints.md", "OLDSHORTCUT", old)

        state, lines = lifecycle._do_rename(
            plan("rename", old=old, new=new), old, new, "fp", False, [])
        text = "\n".join(lines)

        self.assertIn("1 found", text, "the shortcut row must be FOUND now")
        self.assertEqual("partial", state)
        self.assertIn("Waypoint CLI is not present", text,
                      "the refusal must name the real cause, not a fixed sentence")
        self.assertIn(os.path.join(self.waypoint, "waypoint"), text)

    def test_a_shortcut_row_that_CAN_be_driven_is_followed_and_converges(self):
        """With a CLI present and the runner injected, the row is followed and the rename
        reports `converged` rather than `partial` — the whole point of ruling 1."""
        old = self.repo("Old")
        new = os.path.join(self.tmp, "New")
        self.waypoint_row("waypoints.md", "OLDSHORTCUT", old)
        exe = os.path.join(self.waypoint, "waypoint")
        with open(exe, "w", encoding="utf-8") as fh:
            fh.write('#!/bin/sh\necho "STUB must not run" >&2\nexit 99\n')
        os.chmod(exe, 0o755)

        calls = []
        real_follow = satellites.follow
        self._patch(satellites, "follow",
                    lambda sat, **kw: real_follow(
                        sat, runner=lambda argv: (calls.append(tuple(argv)) or (0, "", "")),
                        **kw))

        state, lines = lifecycle._do_rename(
            plan("rename", old=old, new=new), old, new, "fp", False, [])

        self.assertEqual("converged", state)
        self.assertIn("1 found, 1 followed, 0 need a person", "\n".join(lines))
        self.assertEqual([(exe, "--machine", "", "add", "OLDSHORTCUT", new)], calls)


class RenameExecutorTest(ExecutorTestCase):
    """`_do_rename` HAS run in production (the Desktop converged), but never in a test.

    It is the executor with the most ways to stop short of `converged`, and the only one whose
    `partial` is reached from satellite state rather than from the move itself.
    """

    def memory_dir(self, path):
        """Create the `~/.claude/projects/` auto-memory directory Claude Code would make."""
        directory = os.path.join(self.home, ".claude", "projects",
                                 satellites.project_memory_name(path))
        os.makedirs(directory)
        return directory

    def test_clean_rename_moves_the_directory_and_converges(self):
        old = self.repo("Old")
        new = os.path.join(self.tmp, "New")

        state, lines = lifecycle._do_rename(
            plan("rename", old=old, new=new), old, new, "fp", False, [])

        self.assertEqual(state, "converged")
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.isdir(os.path.join(new, ".git")))
        self.assertTrue(transitions.load(
            self.transitions_file)[plan("rename").plan_id].finished_at)

    def test_a_followable_satellite_is_followed_and_still_converges(self):
        old = self.repo("Old")
        new = os.path.join(self.tmp, "New")
        self.memory_dir(old)

        state, lines = lifecycle._do_rename(
            plan("rename", old=old, new=new), old, new, "fp", False, [])

        self.assertEqual(state, "converged")
        self.assertIn("1 found, 1 followed, 0 need a person", "\n".join(lines))
        self.assertTrue(os.path.isdir(os.path.join(
            self.home, ".claude", "projects", satellites.project_memory_name(new))))

    def test_an_UNFOLLOWABLE_satellite_makes_it_PARTIAL_with_the_directory_moved(self):
        # 🔴 The live Desktop case: the destination auto-memory directory already exists, so
        # following it would merge two conversation histories. A moved directory is NOT a
        # completed rename — and `partial` exits non-zero so the sweep keeps flagging it
        # rather than reporting SYNCED with a capability quietly missing.
        old = self.repo("Old")
        new = os.path.join(self.tmp, "New")
        self.memory_dir(old)
        self.memory_dir(new)          # destination occupied -> followable: False

        state, lines = lifecycle._do_rename(
            plan("rename", old=old, new=new), old, new, "fp", False, [])

        self.assertEqual(state, "partial")
        self.assertTrue(os.path.isdir(os.path.join(new, ".git")),
                        "partial still means the directory moved")
        blob = "\n".join(lines)
        self.assertIn("needs a person", blob)
        self.assertIn("not reported as converged", blob)

    def test_PARTIAL_leaves_the_transition_OPEN(self):
        old = self.repo("Old")
        new = os.path.join(self.tmp, "New")
        self.memory_dir(old)
        self.memory_dir(new)

        lifecycle._do_rename(plan("rename", old=old, new=new), old, new, "fp", False, [])

        row = transitions.load(self.transitions_file)[plan("rename").plan_id]
        self.assertFalse(row.finished_at,
                         "an unresolved satellite must leave the transition open")

    def test_present_at_BOTH_paths_is_STOP_and_moves_nothing(self):
        # Two directories claiming one id is a conflict to report, never to resolve by
        # picking a winner.
        old = self.repo("Old")
        new = self.repo("New")

        state, lines = lifecycle._do_rename(
            plan("rename", old=old, new=new), old, new, "fp", False, [])

        self.assertEqual(state, "stop")
        self.assertTrue(os.path.isdir(old), "both must be preserved")
        self.assertTrue(os.path.isdir(new))
        self.assertIn("needs a person", "\n".join(lines))
        self.assertFalse(os.path.exists(self.transitions_file))

    def test_dry_run_with_an_unfollowable_satellite_says_WOULD_BE_PARTIAL(self):
        # 🔴 A preview must never be the more optimistic of the two runs. `left` is known
        # without any write, so reporting "would-converge" here would hide the real outcome.
        old = self.repo("Old")
        new = os.path.join(self.tmp, "New")
        self.memory_dir(old)
        self.memory_dir(new)

        state, _ = lifecycle._do_rename(
            plan("rename", old=old, new=new), old, new, "fp", True, [])

        self.assertEqual(state, "would-be-partial")
        self.assertTrue(os.path.isdir(old), "a dry run must not move anything")
        self.assertFalse(os.path.exists(self.transitions_file))

    def test_dry_run_with_nothing_in_the_way_says_WOULD_CONVERGE(self):
        old = self.repo("Old")
        new = os.path.join(self.tmp, "New")

        state, lines = lifecycle._do_rename(
            plan("rename", old=old, new=new), old, new, "fp", True, [])

        self.assertEqual(state, "would-converge")
        self.assertIn("would move", "\n".join(lines))
        self.assertTrue(os.path.isdir(old))

    def test_an_occupied_destination_is_STOP_not_a_precondition_refusal(self):
        # Worth pinning because the intuitive answer is wrong. `transitions.observe` reports
        # BOTH for anything at the destination — it does not ask whether that thing is a repo
        # — so `stop` fires BEFORE `relocate.preconditions` ever runs and its
        # DESTINATION_OCCUPIED refusal is unreachable from here. Preserving both is the same
        # outcome by a stricter route, so this is the safe ordering, not a bug; a test
        # asserting "refused" here is asserting its own wrong premise.
        old = self.repo("Old")
        new = os.path.join(self.tmp, "New")
        os.makedirs(new)
        with open(os.path.join(new, "someone-elses.txt"), "w", encoding="utf-8") as fh:
            fh.write("do not merge into me")

        state, lines = lifecycle._do_rename(
            plan("rename", old=old, new=new), old, new, "fp", False, [])

        self.assertEqual(state, "stop")
        self.assertTrue(os.path.isfile(os.path.join(new, "someone-elses.txt")),
                        "a refusal must not disturb the destination")
        self.assertTrue(os.path.isdir(os.path.join(old, ".git")))
        self.assertFalse(os.path.exists(self.transitions_file))

    def test_a_LINKED_WORKTREE_source_is_REFUSED_before_recording(self):
        # A linked worktree's git directory is recorded by path, so a filesystem rename breaks
        # it with exit 128 and it then needs `git worktree repair`. This is the precondition
        # path proper: the destination does not exist, so `observe` reports NOT_STARTED and
        # `preconditions` is genuinely reached.
        main = self.repo("Main")
        subprocess.run(["git", "-C", main, "commit", "--allow-empty", "-m", "seed",
                        "--author", "t <t@e>"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       env=dict(os.environ, GIT_COMMITTER_NAME="t",
                                GIT_COMMITTER_EMAIL="t@e"))
        linked = os.path.join(self.tmp, "Linked")
        git(main, "worktree", "add", "--quiet", "-b", "side", linked)
        new = os.path.join(self.tmp, "Moved")

        state, lines = lifecycle._do_rename(
            plan("rename", old=linked, new=new), linked, new, "fp", False, [])

        self.assertEqual(state, "refused")
        self.assertIn("linked worktree", "\n".join(lines))
        self.assertTrue(os.path.isdir(linked), "a refusal must leave the worktree in place")
        self.assertFalse(os.path.exists(new))
        self.assertFalse(os.path.exists(self.transitions_file),
                         "a precondition refusal must record no transition")

    def test_a_MISSING_DESTINATION_PARENT_is_REFUSED(self):
        old = self.repo("Old")
        new = os.path.join(self.tmp, "no", "such", "parent", "New")

        state, lines = lifecycle._do_rename(
            plan("rename", old=old, new=new), old, new, "fp", False, [])

        self.assertEqual(state, "refused")
        self.assertIn("does not exist", "\n".join(lines))
        self.assertTrue(os.path.isdir(old))
        self.assertFalse(os.path.exists(self.transitions_file))


class RehomeExecutorTest(ExecutorTestCase):

    def test_clean_rehome_converges_and_moves_both_endpoints(self):
        repo = self.repo()

        state, lines = lifecycle._do_rehome(
            plan("rehome", new=INTERNAL), repo, "fp", False, [])

        self.assertEqual(state, "converged")
        fetch_url, push_urls = _endpoints(repo)
        self.assertEqual(fetch_url, INTERNAL)
        self.assertEqual(list(push_urls), [INTERNAL])
        self.assertTrue(any("endpoints after" in line for line in lines))

    def test_a_stray_pushurl_makes_it_PARTIAL_not_converged(self):
        # 🔴 The whole reason this executor verifies rather than shrugging. `set-url` does
        # not clear a separately-set pushurl, so a repo believed moved to the private host can
        # still push to github.com. A "converged" here would be a confidentiality claim the
        # executor cannot deliver.
        repo = self.repo()
        git(repo, "remote", "set-url", "--push", "--add", "origin", STRAY_PUSH)

        state, lines = lifecycle._do_rehome(
            plan("rehome", new=INTERNAL), repo, "fp", False, [])

        self.assertEqual(state, "partial")
        blob = "\n".join(lines)
        self.assertIn(STRAY_PUSH, blob)
        self.assertIn("--push --delete", blob)

    def test_PARTIAL_leaves_the_transition_OPEN(self):
        # `finish` is only reached on the converged path. An unfinished transition is how a
        # later sweep knows this repo is still mid-move; closing it would lose that.
        repo = self.repo()
        git(repo, "remote", "set-url", "--push", "--add", "origin", STRAY_PUSH)

        lifecycle._do_rehome(plan("rehome", new=INTERNAL), repo, "fp", False, [])

        open_rows = [t for t in transitions.load(self.transitions_file).values()
                     if not t.finished_at]
        self.assertEqual([t.repo_id for t in open_rows], ["throwaway"])

    def test_converged_CLOSES_the_transition(self):
        repo = self.repo()

        lifecycle._do_rehome(plan("rehome", new=INTERNAL), repo, "fp", False, [])

        rows = transitions.load(self.transitions_file)
        self.assertTrue(rows[plan("rehome").plan_id].finished_at,
                        "a converged rehome must not leave an open transition")

    def test_dry_run_writes_NOTHING_and_reports_the_current_endpoints(self):
        # A dry run that recorded a transition would make the store claim a move began.
        repo = self.repo()

        state, lines = lifecycle._do_rehome(
            plan("rehome", new=INTERNAL), repo, "fp", True, [])

        self.assertEqual(state, "would-converge")
        self.assertIn(PUBLIC, "\n".join(lines))
        self.assertFalse(os.path.exists(self.transitions_file),
                         "a dry run must not create the transitions store")
        self.assertEqual(_endpoints(repo)[0], PUBLIC, "a dry run must not touch config")


class QuarantineExecutorTest(ExecutorTestCase):

    def test_quarantine_moves_the_checkout_and_records_holding_BEFORE_the_move(self):
        repo = self.repo()
        marker = os.path.join(repo, "evidence.txt")
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("still on this machine")

        state, lines = lifecycle._do_quarantine(
            plan("quarantine"), None, repo, "fp", False, [])

        self.assertEqual(state, "converged")
        self.assertFalse(os.path.exists(repo), "the active checkout must be gone")
        held = os.path.join(self.holding, "throwaway")
        self.assertTrue(os.path.isfile(os.path.join(held, "evidence.txt")),
                        "the material must be intact at the holding path")
        self.assertEqual(
            transitions.load(self.transitions_file)[plan("quarantine").plan_id].holding_path,
            held, "the holding path must be recorded, or the material is lost")
        self.assertIn("Confidentiality eviction incomplete", "\n".join(lines),
                      "§3E: the report must not claim an eviction it did not deliver")

    def test_quarantine_alone_does_NOT_tombstone(self):
        # Only `retire` is terminal. Tombstoning a quarantine would retire an id whose
        # disposition is still open.
        repo = self.repo()

        lifecycle._do_quarantine(plan("quarantine"), None, repo, "fp", False, [])

        self.assertFalse(os.path.exists(self.tombstones_file))

    def test_retire_tombstones_the_id(self):
        repo = self.repo()

        state, lines = lifecycle._do_quarantine(
            plan("retire"), None, repo, "fp", False, [])

        self.assertEqual(state, "converged")
        self.assertIn("throwaway", tombstones.load(self.tombstones_file))
        self.assertIn("tombstoned throwaway", "\n".join(lines))

    def test_an_occupied_holding_location_is_REFUSED_not_merged(self):
        # Whatever is already there was quarantined earlier and has not been dealt with.
        repo = self.repo()
        os.makedirs(os.path.join(self.holding, "throwaway"))

        state, lines = lifecycle._do_quarantine(
            plan("quarantine"), None, repo, "fp", False, [])

        self.assertEqual(state, "refused")
        self.assertIn("already occupied", "\n".join(lines))
        self.assertTrue(os.path.isdir(repo), "a refusal must leave the checkout in place")
        self.assertFalse(os.path.exists(self.transitions_file),
                         "a refusal before the move must record no transition")

    def test_dry_run_moves_nothing(self):
        repo = self.repo()

        state, lines = lifecycle._do_quarantine(
            plan("quarantine"), None, repo, "fp", True, [])

        self.assertEqual(state, "would-converge")
        self.assertTrue(os.path.isdir(repo))
        self.assertFalse(os.path.exists(self.holding))
        self.assertFalse(os.path.exists(self.transitions_file))

    def test_nothing_at_the_path_is_REFUSED(self):
        state, lines = lifecycle._do_quarantine(
            plan("quarantine"), None, os.path.join(self.tmp, "absent"), "fp", False, [])

        self.assertEqual(state, "refused")
        self.assertIn("nothing at", "\n".join(lines))


class MacOSTempPathTest(ExecutorTestCase):
    """The symlink guard must tolerate macOS's `/var` -> `/private/var`.

    Walking the whole ancestor chain for a symlinked destination refused EVERY move on
    macOS and failed ten tests at once (CLAUDE.md §7). Every test above runs under
    `tempfile.mkdtemp`, so they all depend on that fix — this one states the dependency
    out loud so a reintroduction names its own cause instead of appearing as nine failures.
    """

    def test_a_temp_dir_under_a_symlinked_ancestor_is_quarantinable(self):
        # Build the symlinked ancestor ourselves rather than hoping `mkdtemp` lands under one:
        # that holds on macOS (/var -> /private/var) and nowhere else, and this suite also runs
        # with TMPDIR on a plain ext4 path.
        real_parent = os.path.join(self.tmp, "real-parent")
        os.makedirs(real_parent)
        link_parent = os.path.join(self.tmp, "link-parent")
        os.symlink(real_parent, link_parent)
        self.assertTrue(os.path.islink(link_parent))
        path = os.path.join(link_parent, "throwaway")
        os.makedirs(path)
        subprocess.run(["git", "init", "--quiet", "-b", "main", path], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        git(path, "remote", "add", "origin", PUBLIC)
        repo = path

        state, _ = lifecycle._do_quarantine(
            plan("quarantine"), None, repo, "fp", False, [])

        self.assertEqual(state, "converged",
                         "the symlink guard must check the leaf and immediate parent only")


class BoardFreshnessPrecisionTest(unittest.TestCase):
    """🔴 `board_freshness` must compare a FLOAT now against a float mtime.

    It used `clock.epoch()`, which truncates to whole seconds, against
    `os.path.getmtime`, which does not — so a FETCH_HEAD written during the current second
    read as up to 1s IN THE FUTURE and `freshness.gate` refused it as clock skew. The
    catch-22: the refusal tells the operator to run `git pull --rebase` first, and doing
    that immediately before `converge` is exactly what sets FETCH_HEAD's mtime to now.
    Measured on this repo 2026-09-30 — a pull then a converge refused with
    "last_fetch is 0s in the future".

    This asserts the property at the arithmetic, not through a real pull, so it cannot flake
    on sub-second timing the way the bug itself did.
    """

    def test_a_truncated_now_manufactures_a_future_fetch_and_gate_refuses_it(self):
        # The bug, stated as arithmetic: this is what the old code computed.
        mtime = 1790812345.7
        truncated_now = int(mtime)          # what clock.epoch() returned

        verdict = freshness.gate(truncated_now, mtime, 0, False)

        self.assertFalse(verdict.ok)
        self.assertIn("future", verdict.reason)

    def test_a_float_now_accepts_the_same_just_written_fetch(self):
        mtime = 1790812345.7
        real_now = mtime                    # what time.time() returns at that instant

        verdict = freshness.gate(real_now, mtime, 0, False)

        self.assertTrue(verdict.ok, verdict.reason)

    def test_board_freshness_passes_a_FLOAT_now_not_a_truncated_int(self):
        # The regression guard proper, and it must not be timing-dependent: an attempt to
        # provoke the bug by back-dating FETCH_HEAD is flaky, because whether a truncated
        # `now` reads as "future" depends on the current second's fraction. The property
        # itself is exact — `clock.epoch()` returns int, `time.time()` returns float — so
        # capture what `board_freshness` actually hands the gate and assert its type.
        seen = {}
        real_gate = freshness.gate

        def capturing(now, last_fetch, behind, diverged, **kw):
            seen["now"] = now
            return real_gate(now, last_fetch, behind, diverged, **kw)

        original = freshness.gate
        freshness.gate = capturing
        self.addCleanup(setattr, freshness, "gate", original)

        lifecycle.board_freshness()

        self.assertIn("now", seen, "board_freshness did not call freshness.gate")
        self.assertIsInstance(seen["now"], float)
        self.assertNotIsInstance(seen["now"], int)

    def test_a_GENUINELY_future_fetch_is_still_refused(self):
        # The fix must not weaken the guard it sits next to. An hour in the future is skew.
        now = 1790812345.0

        verdict = freshness.gate(now, now + 3600, 0, False)

        self.assertFalse(verdict.ok)
        self.assertIn("future", verdict.reason)


def _endpoints(repo):
    from lib import gitutil
    return gitutil.remote_endpoints(repo)


if __name__ == "__main__":
    unittest.main()


class RenameRemoteSyncTest(ExecutorTestCase):
    """`rename` must put `origin` on the registry's declared `Remote` — Lukas, 2026-09-30.

    *"Anything lodestar managed should have a matching dir and repo name, period. Keep them in
    sync. So rename MUST rename both."*

    The gap this pins was measured, not reasoned about. The converger renamed the Laptop's
    `~/dev/OpenCodeCLI` to `~/dev/DispatchOpenCode` and left `origin` on
    `…/OpenCodeCLI.git`; the Desktop, renamed by a human following a ~30-step document that
    happened to include `git remote set-url`, was correct. So the automation built to replace
    those steps silently dropped one, and GitHub's redirect of a renamed repository is what
    kept it invisible — both URLs `ls-remote` to the same HEAD.

    The case that matters most is `test_an_already_moved_checkout_still_gets_its_remote_fixed`:
    two machines had already converged when the gap was found, so the repair has to reach a
    checkout whose directory is *already* in place or it needs a hand-run `set-url` per
    machine — which is exactly how the old half-applied states became permanent.
    """

    DECLARED = "git@github.com:example/NewName.git"
    OLD_URL = "git@github.com:example/OldName.git"

    def declare(self, remote):
        """Patch the registry lookup, so no fixture registry file is needed."""
        self._patch(lifecycle, "declared_remote", lambda repo_id: remote)

    def origin_of(self, repo):
        from lib import gitutil
        return gitutil.remote_endpoints(repo)

    def test_a_stale_origin_is_rewritten_to_the_declared_remote(self):
        self.declare(self.DECLARED)
        old = self.repo("Old", origin=self.OLD_URL)
        new = os.path.join(self.tmp, "New")

        state, lines = lifecycle._do_rename(
            plan("rename", old=old, new=new), old, new, "fp", False, [])

        self.assertEqual("converged", state, "\n".join(lines))
        fetch, pushes = self.origin_of(new)
        self.assertEqual(self.DECLARED, fetch)
        self.assertEqual([self.DECLARED], pushes)

    def test_an_already_moved_checkout_still_gets_its_remote_fixed(self):
        """The self-heal path: the Laptop and tower had already converged when this was found.

        `transitions.observe` reports DESTINATION here, so the move block is skipped entirely.
        The remote step must sit outside it, or the two machines that already renamed stay
        pointed at the old URL for ever and the repair needs a person at each one.
        """
        self.declare(self.DECLARED)
        old = os.path.join(self.tmp, "Old")          # gone already
        new = self.repo("New", origin=self.OLD_URL)  # directory already in place

        state, lines = lifecycle._do_rename(
            plan("rename", old=old, new=new), old, new, "fp", False, [])

        self.assertEqual("converged", state, "\n".join(lines))
        self.assertEqual(self.DECLARED, self.origin_of(new)[0],
                         "an already-renamed machine must still have its origin repaired")

    def test_a_correct_origin_is_left_alone_and_still_converges(self):
        """The Desktop's case. Idempotent: re-running must not rewrite what is already right."""
        self.declare(self.DECLARED)
        new = self.repo("New", origin=self.DECLARED)
        old = os.path.join(self.tmp, "Old")

        state, lines = lifecycle._do_rename(
            plan("rename", old=old, new=new), old, new, "fp", False, [])

        self.assertEqual("converged", state, "\n".join(lines))
        self.assertEqual(self.DECLARED, self.origin_of(new)[0])
        self.assertTrue(any("already matches" in line for line in lines),
                        "a no-op must say so rather than claiming a rewrite: %s" % lines)

    def test_https_and_ssh_for_one_repo_are_not_a_divergence(self):
        """Identity, never string equality.

        This machine reaches GitHub over ssh and the WSL box over https. A strict `!=` would
        rewrite a correct remote on every sweep AND force one machine onto the other's access
        method — which on tower means replacing the only scheme it can actually use.
        """
        self.declare("git@github.com:example/NewName.git")
        new = self.repo("New", origin="https://github.com/example/NewName.git")
        old = os.path.join(self.tmp, "Old")

        state, lines = lifecycle._do_rename(
            plan("rename", old=old, new=new), old, new, "fp", False, [])

        self.assertEqual("converged", state, "\n".join(lines))
        self.assertEqual("https://github.com/example/NewName.git", self.origin_of(new)[0],
                         "the same repo over a different scheme must not be rewritten")

    def test_a_row_declaring_no_remote_changes_nothing_and_does_not_block(self):
        """Three live rows declare no remote. A URL is never guessed from a directory name."""
        self.declare("")
        new = self.repo("New", origin=self.OLD_URL)
        old = os.path.join(self.tmp, "Old")

        state, lines = lifecycle._do_rename(
            plan("rename", old=old, new=new), old, new, "fp", False, [])

        self.assertEqual("converged", state, "\n".join(lines))
        self.assertEqual(self.OLD_URL, self.origin_of(new)[0],
                         "with nothing declared the origin must be left exactly as found")

    def test_a_stray_push_url_leaves_the_rename_partial(self):
        """§3D guard 8, arriving in the rename path.

        `git remote set-url origin <new>` does NOT clear a separately-set push URL, so a repo
        believed renamed can still push to the old name. `partial` exits non-zero on purpose,
        so this flags on every sweep until a person resolves it.
        """
        self.declare(self.DECLARED)
        new = self.repo("New", origin=self.OLD_URL)
        git(new, "remote", "set-url", "--push", "--add", "origin", STRAY_PUSH)
        old = os.path.join(self.tmp, "Old")

        state, lines = lifecycle._do_rename(
            plan("rename", old=old, new=new), old, new, "fp", False, [])

        self.assertEqual("partial", state, "\n".join(lines))
        self.assertTrue(any(STRAY_PUSH in line for line in lines),
                        "the refusal must name the endpoint that is still wrong: %s" % lines)

    def test_a_dry_run_reports_the_rewrite_and_writes_nothing(self):
        self.declare(self.DECLARED)
        old = self.repo("Old", origin=self.OLD_URL)
        new = os.path.join(self.tmp, "New")

        state, lines = lifecycle._do_rename(
            plan("rename", old=old, new=new), old, new, "fp", True, [])

        self.assertEqual("would-converge", state, "\n".join(lines))
        self.assertTrue(any(self.DECLARED in line for line in lines),
                        "a preview must name the URL it would set: %s" % lines)
        self.assertEqual(self.OLD_URL, self.origin_of(old)[0],
                         "a dry run must not touch git config")
        self.assertFalse(os.path.isdir(new))

    @needs_private("sync/registry.md")
    def test_declared_remote_reads_the_real_registry_and_never_guesses(self):
        """The live row is the fixture here: a wrong id must yield "" rather than a computed URL."""
        self.assertEqual("", lifecycle.declared_remote("no-such-repo-id-exists"))
        self.assertTrue(
            lifecycle.declared_remote("opencodecli").endswith("DispatchOpenCode.git"),
            "the live registry declares the new name for this id")


class EvictionAbsenceTest(ExecutorTestCase):
    """"Nothing here to evict" is the DESIRED END STATE, not a failure — Lukas, 2026-10-01.

    `relocate.quarantine` on a missing path returns `ok: False`, and `_do_quarantine` mapped that
    straight to `refused`. So a retirement aimed at `all` machines was **permanently refused on
    every machine that already lacked the directory**, flagging on every sweep for ever — measured
    on the Desktop, where a long-retired repo's checkout no longer exists.

    Note the asymmetry with rename, which is why this stayed invisible: rename asks
    `transitions.observe`, so an already-moved checkout reports `DESTINATION` and converges.
    Eviction had no equivalent observation, so absence and failure were one answer.

    🔴 **The guard is the whole design, because the obvious fix is dangerous.** Reporting
    `converged` on any missing path would make a mistyped `--old` report success on every machine
    while the real directory sat untouched — a green light on an act that never happened, in the
    one place it is most expensive. So absence counts as done **only at the path the registry row
    itself declares**; anywhere else it stays a refusal, because that is a typo until proven
    otherwise.
    """

    def row_for(self, path):
        from lib import registry
        return registry.Entry(name="Throwaway", path=path, branch="main", sync=True,
                              notes="", repo_id="throwaway",
                              remote="git@example.com:x/throwaway.git")

    def absent(self):
        return os.path.join(self.tmp, "GoneAlready")

    def test_absence_at_the_registry_path_converges_without_moving_anything(self):
        gone = self.absent()
        state, lines = lifecycle._do_quarantine(
            plan("quarantine"), self.row_for(gone), gone, "fp", False, [])

        self.assertEqual("converged", state, "\n".join(lines))
        self.assertTrue(any("already absent" in line for line in lines),
                        "the report must say why it converged: %s" % lines)
        self.assertFalse(os.path.exists(self.holding),
                         "nothing may be created in the holding area for a repo that is not here")

    def test_a_path_the_registry_does_NOT_declare_is_still_refused(self):
        """The guard. A hand-typed path that does not exist is a typo until proven otherwise."""
        declared = os.path.join(self.tmp, "Declared")
        typo = os.path.join(self.tmp, "Dcelared")
        state, lines = lifecycle._do_quarantine(
            plan("quarantine", old=typo), self.row_for(declared), typo, "fp", False, [])

        self.assertEqual("refused", state, "\n".join(lines))
        self.assertTrue(any("does not declare" in line or "typo" in line for line in lines),
                        "the refusal must name the reason: %s" % lines)

    def test_no_registry_row_means_absence_cannot_be_trusted(self):
        gone = self.absent()
        state, _ = lifecycle._do_quarantine(
            plan("quarantine"), None, gone, "fp", False, [])
        self.assertEqual("refused", state,
                         "with no declared path there is nothing to check the absence against")

    def test_a_retirement_still_tombstones_when_the_directory_is_already_gone(self):
        """Otherwise a retirement every machine had already done would never be recorded at all.

        Duplicate rows are safe — `tombstones.load` is earliest-wins — and a retirement with NO
        tombstone is a worse failure than a retirement with two.
        """
        gone = self.absent()
        state, lines = lifecycle._do_quarantine(
            plan("retire"), self.row_for(gone), gone, "fp", False, [])

        self.assertEqual("converged", state, "\n".join(lines))
        self.assertIn("throwaway", tombstones.load(self.tombstones_file))

    def test_a_quarantine_of_an_absent_repo_does_not_tombstone(self):
        gone = self.absent()
        lifecycle._do_quarantine(plan("quarantine"), self.row_for(gone), gone, "fp", False, [])
        self.assertFalse(os.path.exists(self.tombstones_file),
                         "only retire is terminal, absent or not")

    def test_a_dry_run_on_an_absent_repo_previews_converge_and_writes_nothing(self):
        gone = self.absent()
        state, lines = lifecycle._do_quarantine(
            plan("retire"), self.row_for(gone), gone, "fp", True, [])

        self.assertEqual("would-converge", state, "\n".join(lines))
        self.assertFalse(os.path.exists(self.tombstones_file))
        self.assertFalse(os.path.exists(self.transitions_file))

    def test_a_repo_that_IS_present_still_moves(self):
        """Regression guard: the absence branch must not swallow the real work."""
        repo = self.repo()
        state, _ = lifecycle._do_quarantine(
            plan("quarantine"), self.row_for(repo), repo, "fp", False, [])
        self.assertEqual("converged", state)
        self.assertFalse(os.path.exists(repo))
        self.assertTrue(os.path.isdir(os.path.join(self.holding, "throwaway")))


class RehomeRecordsNothingItCannotDoTest(ExecutorTestCase):
    """A rehome against a missing checkout must not write a transition record first.

    🔴 **Measured in production 2026-10-01, exercising both executors against one real repo.**
    A quarantine converged and finished its record; the still-approved rehome then ran in the
    SAME pass against the now-missing directory, wrote its transition, and only then failed at
    `set-url`. `transitions` is keyed by **repo_id**, so that unfinished record clobbered the
    quarantine's finished one — and every later plan for the repo reported `stop`: *"an old
    record is evidence that something was started, not permission to finish an intent that has
    been superseded."* One ordinary failure wedged the repo and needed a person.

    So two plans for one repo share a single transition slot. That is worth knowing on its own,
    but the fix is narrower: never record state for an act that cannot begin.
    """

    def test_a_missing_checkout_converges_without_writing_a_record(self):
        gone = os.path.join(self.tmp, "NotHere")
        state, lines = lifecycle._do_rehome(
            plan("rehome", new="git@example.com:x/Thing.git"), gone, "fp", False, [])

        self.assertEqual("converged", state, "\n".join(lines))
        self.assertFalse(os.path.exists(self.transitions_file),
                         "a record for an act that never began is what wedges the repo")
        self.assertTrue(any("does not hold the checkout" in line for line in lines), lines)

    def test_a_dry_run_on_a_missing_checkout_previews_converge(self):
        gone = os.path.join(self.tmp, "NotHere")
        state, _ = lifecycle._do_rehome(
            plan("rehome", new="git@example.com:x/Thing.git"), gone, "fp", True, [])
        self.assertEqual("would-converge", state)
        self.assertFalse(os.path.exists(self.transitions_file))

    def test_a_present_checkout_still_repoints_and_still_records(self):
        """Regression guard: the record-before-mutate discipline must survive the fix."""
        repo = self.repo(origin=PUBLIC)
        state, lines = lifecycle._do_rehome(
            plan("rehome", new=INTERNAL), repo, "fp", False, [])

        self.assertEqual("converged", state, "\n".join(lines))
        from lib import gitutil
        self.assertEqual(INTERNAL, gitutil.remote_endpoints(repo)[0])
        self.assertIn(plan("rehome").plan_id, transitions.load(self.transitions_file),
                      "a real rehome must still record before it mutates")
