"""Tests for the repo-lifecycle checks wired into bin/sync.py -- REPORTING ONLY.

Three things land here, none of which may move, delete, or rewrite a remote:

- `skillclone.remote_host` -- the host portion of a remote URL, used to ask
  `lib/machine.py`'s `host_reachable` whether this machine has any declared route to a
  repo's remote at all.
- `sync.UNREACHABLE` -- a terminal, non-flagged outcome for a repo on a host this machine
  does not declare. It must never touch fetch-state and must never fetch or push.
- `sync.push_url_mismatches` -- a report-only scan for a checkout whose push endpoint has
  drifted from its fetch endpoint (`git remote set-url origin <new>` does not touch a
  separately-configured pushurl; see `gitutil.remote_endpoints` and `tests/test_pushurls.py`).

Reuses `tests.test_sync.SweepTest`'s repo/board scaffolding rather than inventing new
fixtures, per instructions.
"""

import json
import os
import unittest

import sync as sync_cmd
from lib import enroll, gitutil, registry, skillclone, validate
from tests.test_sync import SweepTest, git


class RemoteHostTest(unittest.TestCase):
    """Pure function: the host portion of `normalize_remote`'s output."""

    def test_scp_like_url(self):
        self.assertEqual(
            skillclone.remote_host("git@github.com:Lmandrake/Lodestar.git"), "github.com")

    def test_ssh_scheme_url(self):
        self.assertEqual(
            skillclone.remote_host("ssh://git@github.com/Lmandrake/Lodestar"), "github.com")

    def test_https_scheme_url(self):
        self.assertEqual(
            skillclone.remote_host("https://github.com/lmandrake/lodestar.git"), "github.com")

    def test_empty_string_is_empty(self):
        self.assertEqual(skillclone.remote_host(""), "")

    def test_private_host_url(self):
        # A private-host origin -- github.com is NOT a substring of it, so a naive pattern
        # match over "github.com" would miss this one entirely.
        self.assertEqual(
            skillclone.remote_host("https://git.private.example/owner/Beta.git"),
            "git.private.example")


class OrderAndIconTest(unittest.TestCase):
    """UNREACHABLE is deliberate non-participation: grouped with PAUSED/ABSENT, never flagged."""

    def test_unreachable_sorts_after_paused(self):
        self.assertGreater(sync_cmd.ORDER[sync_cmd.UNREACHABLE], sync_cmd.ORDER[sync_cmd.PAUSED])

    def test_unreachable_sorts_before_absent(self):
        self.assertLess(sync_cmd.ORDER[sync_cmd.UNREACHABLE], sync_cmd.ORDER[sync_cmd.ABSENT])

    def test_unreachable_has_an_icon(self):
        self.assertIn(sync_cmd.UNREACHABLE, sync_cmd.ICON)
        self.assertTrue(sync_cmd.ICON[sync_cmd.UNREACHABLE].strip())


HOSTS_MACHINES = """| Key | Role | Hosts |
|-----|------|-------|
| test-box | testbox | github.com |
| other-box | otherbox | |
"""

UNDECLARED_PRIVATE_URL = "https://git.private.example/owner/Beta.git"
DECLARED_GITHUB_URL = "git@github.com:Lmandrake/Lodestar.git"


class UnreachableHostTest(SweepTest):
    """`sweep_one`'s new guard: never fetch/push a repo on a host this machine can't reach."""

    def setUp(self):
        super(UnreachableHostTest, self).setUp()
        # test-box declares only github.com; a private host is therefore undeclared for it.
        with open(os.path.join(self.root, "machines.md"), "w", encoding="utf-8") as handle:
            handle.write(HOSTS_MACHINES)

    def entry(self, name, path, remote=""):
        return registry.Entry(name=name, path=path, branch="main", sync=True, notes="",
                              remote=remote)

    def test_an_undeclared_host_produces_unreachable(self):
        path = self.make_repo("alpha")
        entry = self.entry("alpha", path, remote=UNDECLARED_PRIVATE_URL)
        result = sync_cmd.sweep_one(entry, "testbox", 10, dry_run=False, fetch_state={})
        self.assertEqual(result.outcome, sync_cmd.UNREACHABLE)
        self.assertIn("git.private.example", result.detail)

    def test_a_declared_host_does_not_produce_unreachable(self):
        path = self.make_repo("alpha")
        entry = self.entry("alpha", path, remote=DECLARED_GITHUB_URL)
        result = sync_cmd.sweep_one(entry, "testbox", 10, dry_run=False, fetch_state={})
        self.assertNotEqual(result.outcome, sync_cmd.UNREACHABLE)
        # The checkout's REAL remote (the local bare repo make_repo created) is unrelated to
        # the declared URL. That used to sweep CLEAN; since the push guard (owner card
        # 2026-10-28) an origin that is not the declared Remote is flagged and never pushed.
        self.assertEqual(result.outcome, sync_cmd.FLAGGED)
        self.assertIn("NOT pushed", result.detail)

    def test_unreachable_is_not_counted_as_a_flag(self):
        path = self.make_repo("alpha")
        entry = self.entry("alpha", path, remote=UNDECLARED_PRIVATE_URL)
        result = sync_cmd.sweep_one(entry, "testbox", 10, dry_run=False, fetch_state={})
        self.assertNotEqual(result.outcome, sync_cmd.FLAGGED)

    def test_unreachable_leaves_fetch_state_untouched(self):
        path = self.make_repo("alpha")
        entry = self.entry("alpha", path, remote=UNDECLARED_PRIVATE_URL)
        fetch_state = {"alpha": {"last_fetch": 12345.0, "tier": sync_cmd.COLD}}
        before = dict(fetch_state["alpha"])

        result = sync_cmd.sweep_one(entry, "testbox", 10, dry_run=False, fetch_state=fetch_state)

        self.assertEqual(result.outcome, sync_cmd.UNREACHABLE)
        self.assertEqual(fetch_state["alpha"], before)
        self.assertFalse(result.fetched)

    def test_unreachable_never_actually_fetches(self):
        # Belt and braces on top of the fetch-state assertion: nothing in `actions` claims a
        # fetch happened either.
        path = self.make_repo("alpha")
        entry = self.entry("alpha", path, remote=UNDECLARED_PRIVATE_URL)
        result = sync_cmd.sweep_one(entry, "testbox", 10, dry_run=False, fetch_state={})
        self.assertNotIn("fetched", result.actions)

    def test_declared_remote_takes_precedence_over_the_observed_origin(self):
        """The exact hazard this ordering exists to avoid.

        `make_repo` gives the checkout a real, working origin (a local bare repo, whose
        `normalize_remote` host reduces to "" -- a plain filesystem path has no host at all).
        If the sweep judged reachability from THAT observed origin, this repo would never be
        flagged unreachable no matter what the registry declares. Setting `entry.remote` to
        an undeclared host must still win: the declaration is intent, checked BEFORE whatever
        the checkout happens to have configured.
        """
        path = self.make_repo("alpha")
        observed = skillclone.observe_remote(path)
        self.assertEqual(skillclone.remote_host(observed), "",
                         "the local bare-repo origin must be the hostless case this test needs")

        entry = self.entry("alpha", path, remote=UNDECLARED_PRIVATE_URL)
        result = sync_cmd.sweep_one(entry, "testbox", 10, dry_run=False, fetch_state={})
        self.assertEqual(result.outcome, sync_cmd.UNREACHABLE)

    def test_no_declared_remote_falls_back_to_the_observed_origin_harmlessly(self):
        # entry.remote is blank; the fallback (a hostless local path) never matches anything
        # in machines.md, so the guard is a no-op rather than a false unreachable.
        path = self.make_repo("alpha")
        entry = self.entry("alpha", path, remote="")
        result = sync_cmd.sweep_one(entry, "testbox", 10, dry_run=False, fetch_state={})
        self.assertNotEqual(result.outcome, sync_cmd.UNREACHABLE)


class PushEndpointMismatchTest(SweepTest):
    """`sync.push_url_mismatches` -- report-only, ssh vs. https for the same repo tolerated."""

    def entry(self, name, path):
        return registry.Entry(name=name, path=path, branch="main", sync=True, notes="")

    def test_a_genuine_mismatch_is_detected(self):
        path = self.make_repo("alpha")
        git(path, "remote", "set-url", "--push", "origin",
            "https://example.com/somebody-elses/repo.git")

        mismatches = sync_cmd.push_url_mismatches([self.entry("alpha", path)])

        self.assertEqual(len(mismatches), 1)
        name, fetch_url, differing = mismatches[0]
        self.assertEqual(name, "alpha")
        self.assertEqual(differing, ["https://example.com/somebody-elses/repo.git"])
        self.assertNotEqual(fetch_url, differing[0])

    def test_ssh_vs_https_for_the_same_repo_is_not_a_mismatch(self):
        path = self.make_repo("alpha")
        # Rewrite the remote to a public host/owner/repo pair so an ssh and an https spelling
        # of the SAME identity exist to compare -- a local bare-repo path has no such pair.
        git(path, "remote", "set-url", "origin", "https://github.com/example/repo.git")
        git(path, "remote", "set-url", "--push", "origin", "git@github.com:example/repo.git")

        mismatches = sync_cmd.push_url_mismatches([self.entry("alpha", path)])

        self.assertEqual(mismatches, [],
                         "ssh and https for the same host/owner/repo must not be reported")

    def test_an_absent_repo_is_skipped_not_flagged(self):
        ghost = os.path.join(self.tmp, "nope")
        mismatches = sync_cmd.push_url_mismatches([self.entry("ghost", ghost)])
        self.assertEqual(mismatches, [])

    def test_no_origin_configured_is_skipped(self):
        path = self.make_repo("alpha", seed=False)
        git(path, "remote", "remove", "origin")
        mismatches = sync_cmd.push_url_mismatches([self.entry("alpha", path)])
        self.assertEqual(mismatches, [])

    def test_the_report_has_its_own_section(self):
        path = self.make_repo("alpha")
        git(path, "remote", "set-url", "--push", "origin",
            "https://example.com/somebody-elses/repo.git")
        self.register_all()
        self.run_sweep()
        with open(sync_cmd.paths.sync_report_file(), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("Push/fetch endpoint mismatches", text)
        self.assertIn("example.com/somebody-elses/repo.git", text)

    def test_the_report_says_so_when_none_are_found(self):
        self.make_repo("alpha")
        self.register_all()
        self.run_sweep()
        with open(sync_cmd.paths.sync_report_file(), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("Push/fetch endpoint mismatches", text)
        self.assertIn("None found", text)


class FreshnessGateReportedTest(SweepTest):
    """The control-repo freshness gate: reported, never enforced, in this phase."""

    def test_freshness_is_reported_in_the_status_file_and_never_skips_a_repo(self):
        self.make_repo("alpha")
        self.register_all()
        self.run_sweep()
        payload = self.status()
        self.assertIn("freshness", payload)
        self.assertIn("ok", payload["freshness"])
        self.assertIn("reason", payload["freshness"])
        # Report-only: a repo with nothing to do must still land as CLEAN, never skipped on
        # the strength of the freshness verdict.
        self.assertEqual(self.outcome("alpha"), sync_cmd.CLEAN)

    def test_freshness_line_appears_in_the_report(self):
        self.make_repo("alpha")
        self.register_all()
        self.run_sweep()
        with open(sync_cmd.paths.sync_report_file(), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("Freshness gate", text)
        self.assertIn("report-only", text)


class EnrollmentReportedTest(SweepTest):
    """`lib/enroll.py` must have a PRODUCTION caller, and this is the test that pins it.

    That module shipped with thirteen passing tests, nothing calling it, and not one stamped
    checkout on the machine — the failure mode CLAUDE.md names in capitals, because green
    tests plus no caller is indistinguishable from a working feature. These tests fail if the
    caller is removed again, which a unit test of `enroll` itself never would.
    """

    def test_enroll_has_a_caller_outside_its_own_module_and_tests(self):
        """The structural assertion. Grep-free: it reads the sweep's own source."""
        with open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(sync_cmd.__file__))), "bin", "sync.py"),
                encoding="utf-8") as handle:
            source = handle.read()
        # Strip comments first: this repo has a recorded case of a presence check passing on a
        # comment that merely *mentioned* the thing it was guarding.
        code = "\n".join(line.split("#", 1)[0] for line in source.split("\n"))
        self.assertIn("enroll.read_stamp", code)
        self.assertIn("enroll.conflicts", code)

    def registry_with_ids(self, rows):
        """Write a registry carrying the new `Id` column.

        `SweepTest.set_registry` deliberately writes the LEGACY schema (no Id), which is what
        proves the parser's backward tolerance and is depended on by thousands of tests. So
        these tests write their own rather than changing that fixture underneath everyone.
        """
        lines = ["| Name | Path | Branch | Send | Receive | Id | Notes |",
                 "|------|------|--------|------|---------|------|-------|"]
        for name, path, repo_id in rows:
            lines.append("| %s | %s | main | on | on | %s |  |" % (name, path, repo_id))
        with open(os.path.join(self.root, "sync", "registry.md"), "w",
                  encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

    def test_an_unstamped_checkout_is_reported_as_unenrolled(self):
        path = self.make_repo("alpha")
        self.registry_with_ids([("alpha", path, "alpha-id")])
        self.run_sweep()
        payload = self.status()
        self.assertIn("enrollment", payload)
        self.assertIn("alpha", payload["enrollment"]["unenrolled"])
        self.assertEqual(payload["enrollment"]["enrolled"], [])

    def test_a_stamped_checkout_is_reported_as_enrolled(self):
        path = self.make_repo("alpha")
        self.registry_with_ids([("alpha", path, "alpha-id")])
        self.assertTrue(enroll.stamp(path, "alpha-id"))
        self.run_sweep()
        payload = self.status()
        self.assertIn("alpha", payload["enrollment"]["enrolled"])
        self.assertNotIn("alpha", payload["enrollment"]["unenrolled"])

    def test_a_row_with_no_id_is_neither_enrolled_nor_unenrolled(self):
        """A blank Id is a schema-migration fact, not an enrolment failure.

        Counting it as unenrolled would put every row on the list until the migration runs,
        which is the same check-versus-outage distinction `validate.blocking` draws for
        `missing-id`.
        """
        self.make_repo("alpha")
        self.register_all()  # legacy schema: no Id column at all
        self.run_sweep()
        payload = self.status()
        self.assertEqual(payload["enrollment"]["enrolled"], [])
        self.assertEqual(payload["enrollment"]["unenrolled"], [])

    def test_the_section_names_the_bootstrap_script(self):
        self.make_repo("alpha")
        self.register_all()
        self.run_sweep()
        with open(sync_cmd.paths.sync_report_file(), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("## Enrollment", text)
        self.assertIn("enroll_checkouts.py", text)

    def test_a_raising_enrollment_check_does_not_take_the_sweep_down(self):
        self.make_repo("alpha")
        self.register_all()
        original = sync_cmd.enrollment_state

        def exploding(_entries):
            raise RuntimeError("git is on fire")

        sync_cmd.enrollment_state = exploding
        try:
            self.assertEqual(self.run_sweep(), 0)
        finally:
            sync_cmd.enrollment_state = original
        self.assertEqual(self.outcome("alpha"), sync_cmd.CLEAN)


class QuietPathTest(SweepTest):
    """The `--quiet` print path, which launchd actually runs and no test had exercised.

    Found 2026-09-30 by running a dry sweep by hand: `contended` was computed only inside the
    non-quiet arm and read in the quiet arm's last line, so every quiet sweep raised
    UnboundLocalError *after* completing all of its real work. Exit code said failure while
    the commits and pushes had landed — invisible in the board, red in a log nobody reads.
    3661 passing tests never went near it.
    """

    def test_a_quiet_sweep_exits_zero(self):
        self.make_repo("alpha")
        self.register_all()
        self.assertEqual(self.run_sweep("--quiet"), 0)

    def test_a_quiet_dry_run_exits_zero(self):
        self.make_repo("alpha")
        self.register_all()
        self.assertEqual(self.run_sweep("--quiet", "--dry-run"), 0)

    def test_a_quiet_sweep_with_a_contended_repo_exits_zero(self):
        """The specific variable that was unbound. A repo holding a guarded lock takes the
        CONTENDED branch, which is what the quiet arm's final line reports on."""
        path = self.make_repo("alpha")
        lock = os.path.join(path, ".git", "index.lock")
        with open(lock, "w", encoding="utf-8") as handle:
            handle.write("")
        self.register_all()
        self.assertEqual(self.run_sweep("--quiet"), 0)

    def test_a_verbose_sweep_still_exits_zero(self):
        self.make_repo("alpha")
        self.register_all()
        self.assertEqual(self.run_sweep(), 0)


class DesiredStateFaultsReportedTest(SweepTest):
    """`validate.faults` wired into the sweep: reported, never enforced, in this phase.

    This section exists because a CLEAN git merge can leave the desired state
    self-contradictory, so the section is rendered even when empty — an absent section reads
    as "not checked", which is the failure this whole check exists to prevent.
    """

    def test_the_section_is_rendered_even_when_there_are_no_faults(self):
        text = sync_cmd.report([], "desktop", False, [], (), None, None, [])
        self.assertIn("## Desired-state faults", text)
        self.assertIn("None found.", text)

    def test_a_blocking_fault_is_rendered_with_its_kind_and_subject(self):
        found = [validate.Fault("duplicate-id", "widget",
                                "id 'widget' is on more than one row")]
        text = sync_cmd.report([], "desktop", False, [], (), None, None, found)
        self.assertIn("duplicate-id", text)
        self.assertIn("widget", text)
        self.assertIn("is on more than one row", text)

    def test_missing_id_is_reported_but_not_blocking_so_it_stays_out_of_the_report(self):
        """Every row is blank until the schema migration runs.

        Treating `missing-id` as blocking would bury the real faults under ~50 rows of noise,
        which is the difference between a check and an outage.
        """
        found = [validate.Fault("missing-id", "Widget", "row 'Widget' has a blank Id")]
        text = sync_cmd.report([], "desktop", False, [], (), None, None, found)
        self.assertIn("## Desired-state faults", text)
        self.assertIn("None found.", text)
        self.assertNotIn("missing-id", text)

    def test_status_file_carries_only_the_blocking_faults(self):
        found = [validate.Fault("missing-id", "Widget", "blank Id"),
                 validate.Fault("duplicate-id", "widget", "on two rows")]
        # write_status returns the PATH it wrote, not the JSON. HOME is sandboxed by
        # SweepTest, so this lands in the temp tree rather than the real ~/dev/.lodestar.
        target = sync_cmd.write_status([], "desktop", True, None, None, found)
        with open(target, encoding="utf-8") as handle:
            payload = json.load(handle)
        kinds = [entry["kind"] for entry in payload["desired_state_faults"]]
        self.assertEqual(kinds, ["duplicate-id"])

    def test_a_validator_that_raises_is_reported_and_does_not_crash_the_sweep(self):
        """A broken check must report that it checked nothing, never take the sweep down.

        The dangerous failure is the silent one: a validator that raises and is swallowed
        leaves an empty section that reads exactly like a clean bill of health.
        """
        self.make_repo("alpha")
        self.register_all()
        original = validate.faults

        def exploding(*args, **kwargs):
            raise RuntimeError("tombstones file is unreadable")

        validate.faults = exploding
        try:
            self.run_sweep()
        finally:
            validate.faults = original

        with open(sync_cmd.paths.sync_report_file(), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("validator-failed", text)
        self.assertIn("checked nothing", text)
        # The sweep still did its real work.
        self.assertEqual(self.outcome("alpha"), sync_cmd.CLEAN)


if __name__ == "__main__":
    unittest.main()
