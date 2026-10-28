"""Tests for lib/satellites.py -- guard 7: a moved checkout is not a completed rename.

Every fixture builds a fake `home`, `repo_root` and `waypoint_root` under a tempdir so
`find()` never touches the real `~/.claude/`, this repo's own `claude/snapshot/`, or the
real `~/dev/Waypoint`. `follow()` mutates only the fixture, never anything real.
"""

import inspect
import json
import os
import shutil
import stat
import tempfile
import unittest

from lib import satellites


OLD = "/home/user/dev/OldName"
NEW = "/home/user/dev/NewName"


class SatellitesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="lodestar-satellites-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.repo_root = os.path.join(self.tmp, "repo")
        self.waypoint_root = os.path.join(self.tmp, "waypoint")
        os.makedirs(self.home)
        os.makedirs(self.repo_root)
        os.makedirs(self.waypoint_root)

        saved = {k: os.environ.get(k) for k in ("LODESTAR_MACHINE",)}
        def _restore():
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(_restore)
        os.environ["LODESTAR_MACHINE"] = "mt-testbox"

    # -- fixture builders -----------------------------------------------------------

    def make_skill_symlink(self, name, target):
        skills_dir = os.path.join(self.home, ".claude", "skills")
        os.makedirs(skills_dir, exist_ok=True)
        link_path = os.path.join(skills_dir, name)
        os.symlink(target, link_path)
        return link_path

    def record_created(self, names):
        state_dir = os.path.join(self.home, "dev", ".lodestar")
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, "skill-links.json"), "w", encoding="utf-8") as fh:
            json.dump({"created": list(names)}, fh)

    def make_project_memory(self, repo_path):
        name = satellites.project_memory_name(repo_path)
        path = os.path.join(self.home, ".claude", "projects", name)
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "MEMORY.md"), "w", encoding="utf-8") as fh:
            fh.write("memory\n")
        return path

    def make_snapshot_memory(self, machine_name, repo_path):
        name = satellites.project_memory_name(repo_path)
        path = os.path.join(self.repo_root, "claude", "snapshot", machine_name, "memory", name)
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "MEMORY.md"), "w", encoding="utf-8") as fh:
            fh.write("memory\n")
        return path

    def make_waypoint_row(self, filename, name, path, machine="", status="active",
                          waypoint=""):
        file_path = os.path.join(self.waypoint_root, filename)
        is_launches = filename == "launches.md"
        if not os.path.exists(file_path):
            if is_launches:
                header = "| Name | Waypoint | Path | Command | Added | Status | Notes | Machine |\n" \
                         "|------|----------|------|---------|-------|--------|-------|---------|\n"
            else:
                header = "| Name | Path | Added | Status | Notes | Machine |\n" \
                         "|------|------|-------|--------|-------|---------|\n"
            with open(file_path, "w", encoding="utf-8") as fh:
                fh.write(header)
        if is_launches:
            row = "| %s | %s | %s |  | 2026-09-30 | %s |  | %s |\n" % (
                name, waypoint, path, status, machine)
        else:
            row = "| %s | %s | 2026-09-30 | %s |  | %s |\n" % (name, path, status, machine)
        with open(file_path, "a", encoding="utf-8") as fh:
            fh.write(row)

    def make_waypoint_exe(self):
        """A stub `waypoint` so the executable-present check passes.

        It is never meant to RUN: every test that gets this far injects a runner. If one
        forgets, the stub exits 99 with a message saying so rather than quietly doing nothing —
        and because it lives in the fixture tempdir it could not reach the real tables even if
        it did run.
        """
        exe = os.path.join(self.waypoint_root, satellites.WAYPOINT_EXE)
        with open(exe, "w", encoding="utf-8") as fh:
            fh.write('#!/bin/sh\necho "STUB: the real Waypoint CLI must never run in a test" '
                     '>&2\nexit 99\n')
        os.chmod(exe, 0o755)
        return exe

    def exe(self):
        return os.path.join(self.waypoint_root, satellites.WAYPOINT_EXE)

    #: A machine that is definitely not this one, derived from this one rather than written as
    #: a plausible literal — CLAUDE.md §7 records a hardcoded machine name in a fixture turning
    #: out to BE this machine on a case-insensitive filesystem, which broke a safety property
    #: that was perfectly intact.
    @property
    def foreign(self):
        return satellites.machine.key() + "-elsewhere"

    def find(self):
        return satellites.find(OLD, NEW, home=self.home, repo_root=self.repo_root,
                                waypoint_root=self.waypoint_root)

    def removals(self, repo_path=OLD):
        return satellites.removals(repo_path, home=self.home, repo_root=self.repo_root,
                                   waypoint_root=self.waypoint_root)

    def by_kind(self, sats, kind):
        return [s for s in sats if s.kind == kind]


class Recorder(object):
    """An injected `runner` that records the argv and returns a canned result.

    No test may start the real Waypoint CLI or touch `~/dev/Waypoint`'s tables — his live shell
    shortcuts. This is the same shape `purge.delete_remote`'s runner has and for the same
    reason: untestable destructive code stays untested, and untested destructive code is where
    the inert features hide.
    """

    def __init__(self, code=0, out="", err=""):
        self.code, self.out, self.err = code, out, err
        self.calls = []

    def __call__(self, argv):
        self.calls.append(tuple(argv))
        return self.code, self.out, self.err


class TestProjectMemoryName(unittest.TestCase):
    def test_slashes_and_underscores_both_fold_to_dashes(self):
        # Measured 2026-09-30: an underscore in a dirname
        # ("Alpha_Project") mangles the same as a slash does, not merely slashes.
        self.assertEqual(
            satellites.project_memory_name("/home/user/dev/Alpha_Project"),
            "-home-user-dev-Alpha-Project")
        self.assertEqual(
            satellites.project_memory_name("/home/user/dev/ACME_Travel_Expense_Report"),
            "-home-user-dev-ACME-Travel-Expense-Report")

    def test_case_is_preserved(self):
        self.assertIn("Alpha", satellites.project_memory_name("/home/user/dev/Alpha_Project"))


class TestSkillSymlink(SatellitesTest):
    def test_lodestar_created_link_is_followable(self):
        link = self.make_skill_symlink("mine", OLD)
        self.record_created(["mine"])

        sats = self.by_kind(self.find(), satellites.SKILL_SYMLINK)
        self.assertEqual(len(sats), 1)
        self.assertTrue(sats[0].followable)
        self.assertEqual(sats[0].where, link)
        self.assertEqual(sats[0].wants, NEW)

    def test_link_into_a_subdirectory_carries_the_remainder(self):
        sub_old = os.path.join(OLD, ".claude", "skills", "foo")
        self.make_skill_symlink("foo", sub_old)
        self.record_created(["foo"])

        sats = self.by_kind(self.find(), satellites.SKILL_SYMLINK)
        self.assertEqual(len(sats), 1)
        self.assertEqual(sats[0].wants, os.path.join(NEW, ".claude", "skills", "foo"))

    def test_link_not_created_by_lodestar_is_reported_but_not_followable(self):
        self.make_skill_symlink("someone-elses", OLD)
        self.record_created([])  # provenance file exists and claims nothing

        sats = self.by_kind(self.find(), satellites.SKILL_SYMLINK)
        self.assertEqual(len(sats), 1)
        self.assertFalse(sats[0].followable)

    def test_link_pointing_elsewhere_is_not_reported(self):
        self.make_skill_symlink("unrelated", "/home/user/dev/SomethingElse")
        sats = self.by_kind(self.find(), satellites.SKILL_SYMLINK)
        self.assertEqual(sats, [])

    def test_case_only_difference_in_target_still_matches(self):
        # APFS is case-insensitive: a target differing only by case from OLD is the same repo.
        self.make_skill_symlink("cased", OLD.replace("OldName", "oldname"))
        self.record_created(["cased"])
        sats = self.by_kind(self.find(), satellites.SKILL_SYMLINK)
        self.assertEqual(len(sats), 1)
        self.assertTrue(sats[0].followable)

    def test_follow_relinks(self):
        link = self.make_skill_symlink("mine", OLD)
        self.record_created(["mine"])
        sat = self.by_kind(self.find(), satellites.SKILL_SYMLINK)[0]

        result = satellites.follow(sat)
        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        self.assertEqual(os.readlink(link), NEW)

    def test_follow_dry_run_does_not_mutate(self):
        link = self.make_skill_symlink("mine", OLD)
        self.record_created(["mine"])
        sat = self.by_kind(self.find(), satellites.SKILL_SYMLINK)[0]

        result = satellites.follow(sat, dry_run=True)
        self.assertTrue(result["ok"])
        self.assertFalse(result["changed"])
        self.assertEqual(os.readlink(link), OLD)

    def test_follow_refuses_unfollowable_link(self):
        link = self.make_skill_symlink("someone-elses", OLD)
        self.record_created([])
        sat = self.by_kind(self.find(), satellites.SKILL_SYMLINK)[0]

        result = satellites.follow(sat)
        self.assertFalse(result["ok"])
        self.assertFalse(result["changed"])
        self.assertEqual(os.readlink(link), OLD)  # untouched


class TestProjectMemory(SatellitesTest):
    def test_found_and_followable_when_destination_is_free(self):
        old_dir = self.make_project_memory(OLD)
        sats = self.by_kind(self.find(), satellites.PROJECT_MEMORY)
        self.assertEqual(len(sats), 1)
        self.assertTrue(sats[0].followable)
        self.assertEqual(sats[0].target, old_dir)

    def test_not_followable_when_destination_already_occupied(self):
        self.make_project_memory(OLD)
        self.make_project_memory(NEW)  # a session already ran at the new path
        sats = self.by_kind(self.find(), satellites.PROJECT_MEMORY)
        self.assertEqual(len(sats), 1)
        self.assertFalse(sats[0].followable)

    def test_nothing_found_when_no_memory_directory_exists(self):
        sats = self.by_kind(self.find(), satellites.PROJECT_MEMORY)
        self.assertEqual(sats, [])

    def test_follow_moves_the_directory(self):
        old_dir = self.make_project_memory(OLD)
        sat = self.by_kind(self.find(), satellites.PROJECT_MEMORY)[0]

        result = satellites.follow(sat)
        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        self.assertFalse(os.path.isdir(old_dir))
        self.assertTrue(os.path.isfile(os.path.join(sat.wants, "MEMORY.md")))


class TestSnapshotMemory(SatellitesTest):
    def test_own_machine_subtree_is_followable(self):
        self.make_snapshot_memory("mt-testbox", OLD)
        sats = self.by_kind(self.find(), satellites.SNAPSHOT_MEMORY)
        self.assertEqual(len(sats), 1)
        self.assertTrue(sats[0].followable)

    def test_other_machines_subtree_is_reported_but_not_followable(self):
        self.make_snapshot_memory("some-other-box", OLD)
        sats = self.by_kind(self.find(), satellites.SNAPSHOT_MEMORY)
        self.assertEqual(len(sats), 1)
        self.assertFalse(sats[0].followable)
        self.assertIn("some-other-box", sats[0].detail)

    def test_multiple_machine_subtrees_all_reported_independently(self):
        self.make_snapshot_memory("mt-testbox", OLD)
        self.make_snapshot_memory("some-other-box", OLD)
        self.make_snapshot_memory("a-third-box", OLD)
        sats = self.by_kind(self.find(), satellites.SNAPSHOT_MEMORY)
        self.assertEqual(len(sats), 3)
        followable_machines = sorted(os.path.basename(os.path.dirname(os.path.dirname(s.where)))
                                      for s in sats if s.followable)
        self.assertEqual(followable_machines, ["mt-testbox"])

    def test_follow_refuses_another_machines_subtree(self):
        old_dir = self.make_snapshot_memory("some-other-box", OLD)
        sat = self.by_kind(self.find(), satellites.SNAPSHOT_MEMORY)[0]

        result = satellites.follow(sat)
        self.assertFalse(result["ok"])
        self.assertTrue(os.path.isdir(old_dir))  # untouched

    def test_follow_moves_own_subtree(self):
        old_dir = self.make_snapshot_memory("mt-testbox", OLD)
        sat = self.by_kind(self.find(), satellites.SNAPSHOT_MEMORY)[0]

        result = satellites.follow(sat)
        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        self.assertFalse(os.path.isdir(old_dir))
        self.assertTrue(os.path.isfile(os.path.join(sat.wants, "MEMORY.md")))


class TestShortcut(SatellitesTest):
    def test_waypoints_row_is_found_and_FOLLOWABLE_by_upserting_the_path(self):
        """🔴 Lukas's ruling 2026-10-01, replacing "never followable, in either direction".

        `waypoint add NAME PATH` is an upsert — measured in `cmd_add`, which updates the
        existing `(NAME, machine)` row in place — so a rename has a mechanical edit after all.
        """
        self.make_waypoint_row("waypoints.md", "OLDNAME", OLD)
        sats = self.by_kind(self.find(), satellites.SHORTCUT)
        self.assertEqual(len(sats), 1)
        self.assertTrue(sats[0].followable)
        self.assertIn("OLDNAME", sats[0].detail)
        self.assertEqual((self.exe(), "--machine", "", "add", "OLDNAME", NEW), sats[0].argv)

    def test_launches_row_with_explicit_path_is_followable_via_launch_add(self):
        self.make_waypoint_row("launches.md", "OldLaunch", OLD)
        sats = self.by_kind(self.find(), satellites.SHORTCUT)
        self.assertEqual(len(sats), 1)
        self.assertTrue(sats[0].followable)
        self.assertEqual((self.exe(), "--machine", "", "launch", "add", "OldLaunch",
                          "--path", NEW), sats[0].argv)

    def test_row_for_an_unrelated_path_is_not_reported(self):
        self.make_waypoint_row("waypoints.md", "SOMETHING_ELSE", "/home/user/dev/Other")
        sats = self.by_kind(self.find(), satellites.SHORTCUT)
        self.assertEqual(sats, [])

    def test_multiple_machine_scoped_rows_for_the_same_name_are_each_reported(self):
        self.make_waypoint_row("waypoints.md", "OLDNAME", OLD, machine="")
        self.make_waypoint_row("waypoints.md", "OLDNAME", OLD, machine=self.foreign)
        sats = self.by_kind(self.find(), satellites.SHORTCUT)
        self.assertEqual(len(sats), 2)

    def test_follow_refuses_a_shortcut_found_for_a_REMOVAL(self):
        """The structural marker still cuts both ways: a removal row names no destination, so
        there is nothing for `follow` to repoint it to."""
        self.make_waypoint_row("waypoints.md", "OLDNAME", OLD)
        sat = self.by_kind(self.removals(), satellites.SHORTCUT)[0]
        result = satellites.follow(sat, runner=Recorder(code=0))
        self.assertFalse(result["ok"])
        self.assertFalse(result["changed"])
        self.assertIn("REMOVAL", result["reason"])


class ShortcutScopingTest(SatellitesTest):
    """🔴 Which shortcut row is THIS machine's to edit.

    Getting this wrong edits another machine's shortcut — a row naming a path that does not
    and should not exist on this disk. The four refusals are each about standing rather than
    capability, which is why none of them can be lifted by making the tool more capable.
    """

    def only(self, sats):
        self.assertEqual(1, len(sats), "expected exactly one shortcut, got %r" % (sats,))
        return sats[0]

    def test_a_row_scoped_to_THIS_machine_is_followable_with_that_scope(self):
        here = satellites.machine.key()
        self.make_waypoint_row("waypoints.md", "MINE", OLD, machine=here)

        sat = self.only(self.by_kind(self.find(), satellites.SHORTCUT))

        self.assertTrue(sat.followable)
        self.assertEqual((self.exe(), "--machine", here, "add", "MINE", NEW), sat.argv)

    def test_a_row_scoped_to_ANOTHER_machine_is_reported_and_NOT_followable(self):
        """That machine edits it on its own converge, exactly as it does its own
        snapshot-memory subtree. Acting from here would rewrite a row for a path this disk
        does not have."""
        self.make_waypoint_row("waypoints.md", "THEIRS", OLD, machine=self.foreign)

        sat = self.only(self.by_kind(self.find(), satellites.SHORTCUT))

        self.assertFalse(sat.followable, "another machine's row is not ours to edit")
        self.assertEqual((), sat.argv, "an unfollowable row must carry no command")
        self.assertIn(self.foreign, sat.detail)

    def test_a_BLOCKED_row_is_reported_and_NOT_followable(self):
        """🔴 A block is a standing human exclusion, recorded so neither a person nor a later
        `add` re-adds the name. Removing the row deletes the decision; re-adding over it needs
        `--force`, which is a human's override and never ours."""
        self.make_waypoint_row("waypoints.md", "EXCLUDED", OLD, status="blocked")

        for sat in (self.only(self.by_kind(self.find(), satellites.SHORTCUT)),
                    self.only(self.by_kind(self.removals(), satellites.SHORTCUT))):
            self.assertFalse(sat.followable)
            self.assertEqual((), sat.argv)
            self.assertIn("blocked", sat.detail)

    def test_a_LAUNCH_row_resolving_through_a_waypoint_is_NOT_followable(self):
        """Its `Waypoint` cell takes precedence over `Path`, so the path matched here is inert
        — and `launch add --path` CLEARS the reference (`l.waypoint, l.path = wp_ref,
        path_val`), breaking a working alias to tidy a dead cell."""
        self.make_waypoint_row("launches.md", "Aliased", OLD, waypoint="SOMEWP")

        sat = self.only(self.by_kind(self.find(), satellites.SHORTCUT))

        self.assertFalse(sat.followable)
        self.assertEqual((), sat.argv)
        self.assertIn("SOMEWP", sat.detail)

    def test_every_unfollowable_shortcut_carries_an_EMPTY_argv(self):
        """Structural, so a future change that flips one bool cannot also hand the runner a
        command: there is nothing there to run."""
        self.make_waypoint_row("waypoints.md", "THEIRS", OLD, machine=self.foreign)
        self.make_waypoint_row("waypoints.md", "EXCLUDED", OLD, status="blocked")
        self.make_waypoint_row("launches.md", "Aliased", OLD, waypoint="SOMEWP")

        sats = self.by_kind(self.find(), satellites.SHORTCUT) + \
            self.by_kind(self.removals(), satellites.SHORTCUT)

        self.assertEqual(6, len(sats))
        for sat in sats:
            self.assertFalse(sat.followable)
            self.assertEqual((), sat.argv)


class ShortcutArgvTest(SatellitesTest):
    """The exact invocation, asserted without a process ever starting."""

    def argv(self, filename, name, removal=False, **row):
        # Rows accumulate in the fixture file, so select this call's row by Name rather than
        # by position -- a helper that took sats[0] would silently describe an earlier row.
        self.make_waypoint_row(filename, name, OLD, **row)
        sats = [s for s in self.by_kind(self.removals() if removal else self.find(),
                                        satellites.SHORTCUT)
                if s.where.split(":", 1)[1].startswith(name + "#")]
        self.assertEqual(1, len(sats))
        return sats[0].argv

    def test_MACHINE_is_passed_on_EVERY_invocation_including_the_universal_row(self):
        """🔴 The single most dangerous thing to get wrong here.

        Measured in `cmd_remove`: `--machine` defaults to `None`, and `None` means *every
        machine* — `kept = [w for w in wps if w.name.upper() != name.upper()]`. So omitting the
        flag on a universal row would also delete every machine-scoped row sharing that Name,
        including rows belonging to machines this one has no standing over. `--machine ""`
        selects the blank-scoped row and nothing else, and it must precede the subcommand.
        """
        cases = [
            self.argv("waypoints.md", "A"),
            self.argv("waypoints.md", "B", removal=True),
            self.argv("launches.md", "C"),
            self.argv("launches.md", "D", removal=True),
        ]
        for argv in cases:
            self.assertIn("--machine", argv, "a missing --machine acts on ALL machines")
            flag = argv.index("--machine")
            self.assertEqual(1, flag, "--machine must come straight after the executable")
            self.assertEqual("", argv[flag + 1], "the universal row is selected by an EMPTY scope")
            # And before the subcommand, which is where argparse requires a global flag.
            subcommands = [i for i, part in enumerate(argv)
                           if part in ("add", "remove", "launch")]
            self.assertTrue(subcommands and min(subcommands) > flag + 1,
                            "--machine must precede the subcommand: %r" % (argv,))

    def test_a_removal_DELETES_the_row_and_a_rename_UPSERTS_its_path(self):
        self.assertEqual((self.exe(), "--machine", "", "remove", "W"),
                         self.argv("waypoints.md", "W", removal=True))
        self.assertEqual((self.exe(), "--machine", "", "add", "W2", NEW),
                         self.argv("waypoints.md", "W2"))

    def test_a_launch_row_uses_the_launch_subcommand_in_both_directions(self):
        self.assertEqual((self.exe(), "--machine", "", "launch", "remove", "L"),
                         self.argv("launches.md", "L", removal=True))
        self.assertEqual((self.exe(), "--machine", "", "launch", "add", "L2", "--path", NEW),
                         self.argv("launches.md", "L2"))

    def test_the_executable_is_inside_waypoint_root_and_not_a_bare_PATH_lookup(self):
        """Measured 2026-10-01: `which waypoint` finds nothing on this machine, so a bare
        "waypoint" argv would fail with a diagnosis pointing at the wrong thing. Naming the
        path inside `waypoint_root` is also what keeps a fixture from reaching the real CLI."""
        argv = self.argv("waypoints.md", "W")
        self.assertEqual(os.path.join(self.waypoint_root, "waypoint"), argv[0])
        self.assertTrue(os.path.isabs(argv[0]))


class ShortcutRunnerTest(SatellitesTest):
    """Driving the CLI: what reaches it, what comes back, and what never runs."""

    def sat(self, removal=False, filename="waypoints.md", name="OLDNAME"):
        self.make_waypoint_row(filename, name, OLD)
        sats = [s for s in self.by_kind(self.removals() if removal else self.find(),
                                        satellites.SHORTCUT)
                if s.where.split(":", 1)[1].startswith(name + "#")]
        self.assertEqual(1, len(sats))
        return sats[0]

    def act(self, sat, **kwargs):
        return (satellites.remove(sat, **kwargs) if satellites.is_removal(sat)
                else satellites.follow(sat, **kwargs))

    def test_a_successful_run_reports_CHANGED_and_names_the_shell_block(self):
        self.make_waypoint_exe()
        sat = self.sat(removal=True)
        recorder = Recorder(code=0)

        result = satellites.remove(sat, runner=recorder)

        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        self.assertEqual([sat.argv], recorder.calls)
        self.assertIn("shell block", result["reason"],
                      "following a row regenerates the managed block; hiding that is dishonest")

    def test_a_FAILED_run_carries_the_tools_own_stderr_and_not_a_fixed_SENTENCE(self):
        """CLAUDE.md §7 records a whole system's wrong diagnosis caused by a refusal message
        that named the wrong cause. The underlying error is the only thing a human sees."""
        self.make_waypoint_exe()
        sat = self.sat()
        recorder = Recorder(code=1, err="'OLDNAME' is blocked (a deliberate exclusion).")

        result = satellites.follow(sat, runner=recorder)

        self.assertFalse(result["ok"])
        self.assertFalse(result["changed"])
        self.assertIn("deliberate exclusion", result["reason"])
        self.assertIn("exit 1", result["reason"])

    def test_an_ABSENT_executable_is_UNFOLLOWED_rather_than_assumed_fine(self):
        sat = self.sat()  # no make_waypoint_exe()
        result = satellites.follow(sat, runner=Recorder(code=0))

        self.assertFalse(result["ok"])
        self.assertIn(self.exe(), result["reason"], "the refusal must name the path it looked at")

    def test_a_NON_EXECUTABLE_CLI_is_unfollowed_and_the_refusal_says_chmod(self):
        exe = self.make_waypoint_exe()
        os.chmod(exe, 0o644)
        sat = self.sat()

        result = satellites.follow(sat, runner=Recorder(code=0))

        self.assertFalse(result["ok"])
        self.assertIn("chmod", result["reason"])

    def test_a_DRY_RUN_runs_nothing_and_is_no_more_optimistic_than_the_real_run(self):
        """A preview must never be the better of the two. With the CLI missing, the real run
        refuses — so the dry run must refuse too, or `--dry-run` would report
        `would-converge` for a run that will report `partial`."""
        recorder = Recorder(code=0)

        sat = self.sat()
        missing = satellites.follow(sat, dry_run=True, runner=recorder)
        self.assertFalse(missing["ok"], "no CLI, so the real run refuses and so must this")

        self.make_waypoint_exe()
        present = satellites.follow(self.sat(name="SECOND"), dry_run=True, runner=recorder)
        self.assertTrue(present["ok"])
        self.assertFalse(present["changed"])
        self.assertIn("would run", present["reason"])

        self.assertEqual([], recorder.calls, "a dry run must send nothing to the CLI")

    def test_an_UNFOLLOWABLE_row_never_reaches_the_runner_at_all(self):
        self.make_waypoint_exe()
        self.make_waypoint_row("waypoints.md", "THEIRS", OLD, machine=self.foreign)
        sat = self.by_kind(self.removals(), satellites.SHORTCUT)[0]
        recorder = Recorder(code=0)

        result = satellites.remove(sat, runner=recorder)

        self.assertFalse(result["ok"])
        self.assertEqual([], recorder.calls)

    def test_a_FORGOTTEN_runner_cannot_reach_the_real_tables(self):
        """🔴 The property that makes this safe to have built at all.

        A fixture's `waypoint_root` is a tempdir, so the argv names an executable that is not
        there and the refusal fires before any process starts. No test can touch his 60 live
        shell shortcuts even by forgetting to inject a runner. (The stub this fixture CAN
        create is itself inside the tempdir and exits 99.)
        """
        sat = self.sat(removal=True)

        result = satellites.remove(sat)  # deliberately no runner

        self.assertFalse(result["ok"])
        self.assertTrue(sat.argv[0].startswith(self.tmp),
                        "the argv must point inside the fixture, not at the real CLI")


class TestIndependence(SatellitesTest):
    """The point of guard 7: one kind's absence, error, or unreadability must never
    suppress the other three. Plant all four kinds, break exactly one, and require the
    other three still come back in full. Delete this test and the guard is gone even if
    the code still LOOKS like it checks four kinds independently.
    """

    def _plant_all_four(self):
        self.make_skill_symlink("mine", OLD)
        self.record_created(["mine"])
        self.make_project_memory(OLD)
        self.make_snapshot_memory("mt-testbox", OLD)
        self.make_waypoint_row("waypoints.md", "OLDNAME", OLD)

    def test_all_four_kinds_present_when_nothing_is_broken(self):
        self._plant_all_four()
        sats = self.find()
        found_kinds = set(s.kind for s in sats)
        self.assertEqual(found_kinds, set(satellites.KINDS))

    def test_unreadable_skills_dir_does_not_suppress_the_other_three(self):
        self._plant_all_four()
        skills_dir = os.path.join(self.home, ".claude", "skills")
        old_mode = os.stat(skills_dir).st_mode
        os.chmod(skills_dir, 0)
        self.addCleanup(os.chmod, skills_dir, old_mode)

        try:
            sats = self.find()
        finally:
            os.chmod(skills_dir, old_mode)  # restore before tempdir cleanup removes it

        found_kinds = set(s.kind for s in sats)
        self.assertNotIn(satellites.SKILL_SYMLINK, found_kinds)
        self.assertEqual(found_kinds, set(satellites.KINDS) - {satellites.SKILL_SYMLINK})
        self.assertEqual(len(self.by_kind(sats, satellites.PROJECT_MEMORY)), 1)
        self.assertEqual(len(self.by_kind(sats, satellites.SNAPSHOT_MEMORY)), 1)
        self.assertEqual(len(self.by_kind(sats, satellites.SHORTCUT)), 1)

    def test_missing_repo_root_snapshot_dir_does_not_suppress_the_other_three(self):
        self.make_skill_symlink("mine", OLD)
        self.record_created(["mine"])
        self.make_project_memory(OLD)
        self.make_waypoint_row("waypoints.md", "OLDNAME", OLD)
        # Deliberately no claude/snapshot/ directory at all under repo_root.

        sats = self.find()
        found_kinds = set(s.kind for s in sats)
        self.assertEqual(found_kinds, set(satellites.KINDS) - {satellites.SNAPSHOT_MEMORY})
        self.assertEqual(len(self.by_kind(sats, satellites.SKILL_SYMLINK)), 1)
        self.assertEqual(len(self.by_kind(sats, satellites.PROJECT_MEMORY)), 1)
        self.assertEqual(len(self.by_kind(sats, satellites.SHORTCUT)), 1)

    def test_missing_waypoint_root_does_not_suppress_the_other_three(self):
        self.make_skill_symlink("mine", OLD)
        self.record_created(["mine"])
        self.make_project_memory(OLD)
        self.make_snapshot_memory("mt-testbox", OLD)

        sats = satellites.find(OLD, NEW, home=self.home, repo_root=self.repo_root,
                                waypoint_root=None)
        found_kinds = set(s.kind for s in sats)
        self.assertEqual(found_kinds, set(satellites.KINDS) - {satellites.SHORTCUT})


class TestFollowRefusesUnfollowable(SatellitesTest):
    """Directly targets the guard inside follow() itself: a manually-constructed
    followable=False satellite must be refused even when everything about its `where`
    and `wants` would otherwise make the underlying operation succeed.
    """

    def test_manually_built_unfollowable_symlink_satellite_is_refused(self):
        link = self.make_skill_symlink("mine", OLD)
        sat = satellites.Satellite(kind=satellites.SKILL_SYMLINK, where=link,
                                    detail="manually marked unfollowable for this test",
                                    target=OLD, wants=NEW, followable=False)
        result = satellites.follow(sat)
        self.assertFalse(result["ok"])
        self.assertFalse(result["changed"])
        self.assertEqual(os.readlink(link), OLD)  # untouched

    def test_manually_built_unfollowable_rename_satellite_is_refused(self):
        old_dir = self.make_project_memory(OLD)
        wants = os.path.join(os.path.dirname(old_dir), satellites.project_memory_name(NEW))
        sat = satellites.Satellite(kind=satellites.PROJECT_MEMORY, where=old_dir,
                                    detail="manually marked unfollowable for this test",
                                    target=old_dir, wants=wants, followable=False)
        result = satellites.follow(sat)
        self.assertFalse(result["ok"])
        self.assertFalse(result["changed"])
        self.assertTrue(os.path.isdir(old_dir))  # untouched
        self.assertFalse(os.path.isdir(wants))


# ==========================================================================================
# removals() and remove() -- the purge direction. Lukas, 2026-10-01.
# ==========================================================================================

class RemovalCensusTest(SatellitesTest):
    """`removals()` is `find()`'s census with no destination, and followability is per-kind.

    The two censuses produce the same type and opposite instructions, so each kind's
    followability has to be re-argued for removal rather than inherited by coincidence. These
    tests are that argument written down: what a removal may touch is what this machine
    created or owns, and nothing else.
    """

    def test_a_removal_satellite_names_NO_destination(self):
        """The structural marker that keeps the two censuses apart.

        `remove()` refuses anything with a destination and `follow()` refuses anything without
        one, so the marker — not a caller's memory — is what stops a rename satellite being
        deleted and a removal satellite being silently "relinked" to nothing.
        """
        self.make_project_memory(OLD)
        self.make_skill_symlink("thing", OLD)
        self.record_created(["thing"])

        for sat in self.removals():
            self.assertEqual("", sat.wants, sat)
            self.assertTrue(satellites.is_removal(sat), sat)

    def test_a_LODESTAR_CREATED_skill_symlink_is_followable(self):
        link = self.make_skill_symlink("mine", OLD)
        self.record_created(["mine"])

        sats = self.by_kind(self.removals(), satellites.SKILL_SYMLINK)

        self.assertEqual(1, len(sats))
        self.assertTrue(sats[0].followable)
        self.assertEqual(link, sats[0].where)

    def test_a_HAND_MADE_skill_symlink_is_reported_and_NOT_followable(self):
        """🔴 Provenance gates removal exactly as it gates repair.

        `~/dev/.lodestar/skill-links.json` records which links THIS machine created. A link a
        human made by hand points at something they chose to wire up, and the purge has no
        standing to delete it — CLAUDE.md §7 records that pruning only ever touches links
        Lodestar itself created, and that rule does not weaken because the act got bigger.
        """
        self.make_skill_symlink("theirs", OLD)
        self.record_created([])

        sats = self.by_kind(self.removals(), satellites.SKILL_SYMLINK)

        self.assertEqual(1, len(sats), "it must still be REPORTED")
        self.assertFalse(sats[0].followable)

    def test_the_auto_memory_directory_is_followable_with_NO_occupancy_question(self):
        """A rename can be blocked by an occupied destination; a removal has no destination.

        Inheriting the rename predicate here would have made a purge `partial` whenever a
        same-named directory happened to exist, for a reason that cannot apply.
        """
        self.make_project_memory(OLD)
        self.make_project_memory(NEW)  # would block a RENAME; irrelevant to a removal

        sats = self.by_kind(self.removals(), satellites.PROJECT_MEMORY)

        self.assertEqual(1, len(sats))
        self.assertTrue(sats[0].followable)

    def test_the_auto_memory_detail_SAYS_the_notes_go_with_it(self):
        """🔴 He accepted this loss; an accepted loss and a silent one differ only in the
        report. If the detail stops saying it, the acceptance stops meaning anything."""
        self.make_project_memory(OLD)

        sat = self.by_kind(self.removals(), satellites.PROJECT_MEMORY)[0]

        self.assertIn("outlive", sat.detail)

    def test_only_THIS_machines_snapshot_memory_is_followable(self):
        """Ownership is the only question for a removal.

        Another machine's snapshot subtree is that machine's to remove when its own converge
        runs — the same split the rename path makes, and the reason a purge aimed at `all`
        does not need one machine to clean up after the others.
        """
        self.make_snapshot_memory("mt-testbox", OLD)
        self.make_snapshot_memory("other-box", OLD)

        sats = self.by_kind(self.removals(), satellites.SNAPSHOT_MEMORY)
        by_owner = {("mine" if "mt-testbox" in s.where else "theirs"): s for s in sats}

        self.assertEqual(2, len(sats), "both are reported")
        self.assertTrue(by_owner["mine"].followable)
        self.assertFalse(by_owner["theirs"].followable)

    def test_a_waypoint_row_is_FOLLOWABLE_by_deleting_the_row(self):
        """🔴 Replaces "reported and NEVER followable", which rested on a false premise.

        Waypoint's tables really are hand-maintained and per-row machine-scoped — the second
        half is why `--machine` is always passed and why another machine's row is still
        refused — but `waypoint remove NAME` is a supported non-interactive edit, so there IS
        a mechanical removal for a row this machine owns.
        """
        self.make_waypoint_row("waypoints.md", "OLDNAME", OLD)

        sats = self.by_kind(self.removals(), satellites.SHORTCUT)

        self.assertEqual(1, len(sats))
        self.assertTrue(sats[0].followable)
        self.assertIn("dangle", sats[0].detail)
        self.assertEqual("", sats[0].wants, "a removal still names no destination")

    def test_the_census_visits_all_four_kinds_in_one_pass(self):
        """One traversal, not four opportunistic lookups: a kind finding nothing must not be
        indistinguishable from a kind that never ran."""
        self.make_skill_symlink("mine", OLD)
        self.record_created(["mine"])
        self.make_project_memory(OLD)
        self.make_snapshot_memory("mt-testbox", OLD)
        self.make_waypoint_row("waypoints.md", "OLDNAME", OLD)

        found = {sat.kind for sat in self.removals()}

        self.assertEqual(set(satellites.KINDS), found)

    def test_removals_RESOLVES_its_own_roots_unlike_find(self):
        """🔴 A purge has no undo, so a satellite it cannot SEE is a leftover nobody is told
        about.

        `find()`'s `repo_root`/`waypoint_root` have no defaults and its only production caller
        passes neither, so two of the four kinds are found by nothing in the rename path.
        `removals()` must not inherit that: called with only a path it still reaches a
        `repo_root` and a `waypoint_root`.
        """
        source = inspect.getsource(satellites.removals)
        self.assertIn("paths.root()", source)
        self.assertIn("Waypoint", source)

        # And prove it rather than reading it: with no roots given the defaults resolve and the
        # census still runs, finding nothing because nothing on this machine is named for a
        # path under the temp tree.
        self.assertEqual([], satellites.removals(os.path.join(self.tmp, "NeverExisted")))


class RemoverTest(SatellitesTest):
    """`remove()` deletes a satellite of a purged checkout, or refuses with the reason."""

    def test_a_created_skill_symlink_is_removed_and_the_TARGET_is_untouched(self):
        """🔴 `where` is the link, `target` is what it points at.

        Reaching for `target` would delete a directory INSIDE the repository being purged — or,
        once the checkout is gone, nothing at all while reporting success. The fixture keeps the
        target alive precisely so the wrong one would be visible.
        """
        target = os.path.join(self.tmp, "LiveTarget")
        os.makedirs(target)
        link = self.make_skill_symlink("mine", target)
        self.record_created(["mine"])
        sat = self.by_kind(self.removals(target), satellites.SKILL_SYMLINK)[0]

        result = satellites.remove(sat)

        self.assertTrue(result["ok"] and result["changed"], result["reason"])
        self.assertFalse(os.path.lexists(link))
        self.assertTrue(os.path.isdir(target), "the link went, not what it pointed at")

    def test_the_auto_memory_directory_and_its_contents_are_removed(self):
        self.make_project_memory(OLD)
        sat = self.by_kind(self.removals(), satellites.PROJECT_MEMORY)[0]

        result = satellites.remove(sat)

        self.assertTrue(result["ok"] and result["changed"], result["reason"])
        self.assertFalse(os.path.exists(sat.target))

    def test_a_DRY_RUN_reports_what_it_would_do_and_removes_NOTHING(self):
        """A preview must know everything the real run knows, or `--dry-run` becomes the more
        optimistic of the two — the failure LESSONS.md records for the rename path."""
        memory = self.make_project_memory(OLD)
        link = self.make_skill_symlink("mine", OLD)
        self.record_created(["mine"])

        for sat in self.removals():
            if sat.followable:
                result = satellites.remove(sat, dry_run=True)
                self.assertTrue(result["ok"], result["reason"])
                self.assertFalse(result["changed"])
                self.assertIn("would remove", result["reason"])

        self.assertTrue(os.path.isdir(memory))
        self.assertTrue(os.path.lexists(link))

    def test_an_UNFOLLOWABLE_satellite_is_refused_outright(self):
        """Never attempted, the same contract `follow()` has. A hand-made symlink, another
        machine's subtree and a Waypoint row all land here and all belong to someone else."""
        link = self.make_skill_symlink("theirs", OLD)
        self.record_created([])
        sat = self.by_kind(self.removals(), satellites.SKILL_SYMLINK)[0]

        result = satellites.remove(sat)

        self.assertFalse(result["ok"])
        self.assertTrue(os.path.lexists(link), "someone else's link must survive")

    def test_a_RENAME_satellite_handed_to_remove_is_REFUSED(self):
        """🔴 The two censuses are type-identical, so this is the only thing standing between
        "relink this" and "delete this".

        A rename satellite's whole instruction is that the capability should be kept and
        re-aimed. Removing it would destroy exactly what the rename existed to preserve, and
        nothing about the call site would look wrong.
        """
        memory = self.make_project_memory(OLD)
        sat = self.by_kind(self.find(), satellites.PROJECT_MEMORY)[0]
        self.assertTrue(sat.followable, "the fixture must be a followable RENAME satellite")

        result = satellites.remove(sat)

        self.assertFalse(result["ok"])
        self.assertIn("RENAME", result["reason"])
        self.assertTrue(os.path.isdir(memory),
                        "the directory a rename would move must survive")

    def test_a_REMOVAL_satellite_handed_to_follow_is_REFUSED(self):
        """The marker cuts both ways: with no destination, `follow()` would be a no-op
        reported as success — a green check on nothing happening."""
        self.make_project_memory(OLD)
        sat = self.by_kind(self.removals(), satellites.PROJECT_MEMORY)[0]

        result = satellites.follow(sat)

        self.assertFalse(result["ok"])
        self.assertIn("REMOVAL", result["reason"])

    def test_a_skill_symlink_that_is_no_longer_a_symlink_is_REFUSED(self):
        """`os.remove` on a real file there would be deleting something nobody looked at."""
        link = self.make_skill_symlink("mine", OLD)
        self.record_created(["mine"])
        sat = self.by_kind(self.removals(), satellites.SKILL_SYMLINK)[0]
        os.remove(link)
        with open(link, "w", encoding="utf-8") as fh:
            fh.write("a real file someone put here between the census and the removal\n")

        result = satellites.remove(sat)

        self.assertFalse(result["ok"])
        self.assertTrue(os.path.isfile(link))

    def test_a_memory_path_that_is_a_SYMLINK_is_refused_rather_than_rmtree_d(self):
        """🔴 `rmtree` through a symlink is how a removal escapes the tree it was scoped to.

        `shutil.rmtree` refuses a symlink itself, but the refusal has to come from a check that
        NAMES the reason — a caller reading "Cannot call rmtree on a symbolic link" has no idea
        whether its own scoping was wrong.
        """
        outside = os.path.join(self.tmp, "Precious")
        os.makedirs(outside)
        with open(os.path.join(outside, "keep.txt"), "w", encoding="utf-8") as fh:
            fh.write("not ours\n")
        projects = os.path.join(self.home, ".claude", "projects")
        os.makedirs(projects, exist_ok=True)
        planted = os.path.join(projects, satellites.project_memory_name(OLD))
        os.symlink(outside, planted)
        sat = satellites.Satellite(kind=satellites.PROJECT_MEMORY, where=planted,
                                   detail="planted for this test", target=planted,
                                   wants="", followable=True)

        result = satellites.remove(sat)

        self.assertFalse(result["ok"])
        self.assertIn("not a plain directory", result["reason"])
        self.assertTrue(os.path.isfile(os.path.join(outside, "keep.txt")))

    def test_an_already_absent_memory_directory_is_a_no_op_SUCCESS(self):
        """A fleet purge runs twice on one machine after a retry; absence is the end state."""
        self.make_project_memory(OLD)
        sat = self.by_kind(self.removals(), satellites.PROJECT_MEMORY)[0]
        shutil.rmtree(sat.target)

        result = satellites.remove(sat)

        self.assertTrue(result["ok"])
        self.assertFalse(result["changed"])

    def test_a_waypoint_row_marked_followable_with_NO_argv_still_runs_nothing(self):
        """What replaced the old blanket "never machine-removed" refusal.

        A shortcut is followable now, so the guard can no longer be the kind — it is the
        absence of a command. Flipping `followable` by hand does not manufacture one, so a
        future change that sets the bool without going through the census mutates nothing.
        """
        sat = satellites.Satellite(kind=satellites.SHORTCUT, where="waypoints.md:X#0",
                                   detail="forced followable for this test", target=OLD,
                                   wants="", followable=True)
        recorder = Recorder(code=0)

        result = satellites.remove(sat, runner=recorder)

        self.assertFalse(result["ok"])
        self.assertIn("no command", result["reason"])
        self.assertEqual([], recorder.calls)


if __name__ == "__main__":
    unittest.main()
