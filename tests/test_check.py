"""Tests for bin/check.py — the one-command health verdict.

`check.py` is a *composer*: it shells out to the tools that already exist and folds their
exit codes into one answer. So almost nothing here runs a real subprocess. The runner is
injected, which keeps the suite fast and — more importantly — means these tests can assert
things a real run cannot be made to produce on demand: a timeout, a crashed agent, a board
whose journal does NOT round-trip.

Two properties get more attention than the rest, because both are defects that were found
in the wild rather than imagined:

- **A skipped check must never read as a passing one.** Machinist's `tcc-audit.sh` silently
  omitted a permission and reported clean; `shadow.py --gate` prints "NOT CHECKED HERE" for
  the test suite for the same reason. So `--tests` is opt-in and its absence is loud.
- **check.py must not mutate the real board.** Everything root-relative runs against a
  snapshot, and the snapshot is asserted to be a copy rather than the original.
- **check.py must not cry wolf.** On 2026-09-01 its own sweep raced the live one, GitHub
  throttled the collision, and `git`'s "…and the repository exists." tail made throttling
  read as broken auth: 14 repos flagged, then 3 *different* ones, then 0. A transient fetch
  failure must therefore report UNPROVEN and exit 0, while one that survives a retry must
  still be flagged and exit 1. Both directions are tested here, because a health tool that
  cries wolf gets ignored — and an ignored tool is D13's corollary failing from the other
  side.
"""

import ast
import datetime
import io
import os
import shutil
import subprocess
import tempfile
import time
import unittest

import check as check_cmd


MACHINES_MD = "| Key | Role |\n|-----|------|\n| test-box | desktop |\n"

LOOP_MD = """---
id: demo
title: "A demo loop"
class: background
state: open
account:
repo:
est_close: 1h
actual_spent: 0m
opened: 2026-09-01
last_touch: 2026-09-01T10:00-07:00 desktop/cc
deadline:
---
Body prose, which the journal never carries.
"""


def check(name, tier=check_cmd.HEALTH, verdict=check_cmd.OK, detail="", output=""):
    return check_cmd.Check(name=name, tier=tier, verdict=verdict, detail=detail,
                           output=output)


class VerdictTest(unittest.TestCase):
    def test_exit_zero_is_ok(self):
        self.assertEqual(check_cmd.verdict_from_exit(0), check_cmd.OK)

    def test_a_non_zero_exit_fails(self):
        self.assertEqual(check_cmd.verdict_from_exit(1), check_cmd.FAIL)
        self.assertEqual(check_cmd.verdict_from_exit(2), check_cmd.FAIL)

    def test_a_timeout_fails_rather_than_being_swallowed(self):
        self.assertEqual(check_cmd.verdict_from_exit(None, timed_out=True),
                         check_cmd.FAIL)


class SummaryLineTest(unittest.TestCase):
    def test_the_preferred_substring_wins_over_the_last_line(self):
        text = "noise\n1 group(s): 1 passed, 0 failed\ngate PASSED for 2026-09-01\n"
        self.assertEqual(check_cmd.summary_line(text, ("group(s):",)),
                         "1 group(s): 1 passed, 0 failed")

    def test_it_falls_back_to_the_last_non_empty_line(self):
        self.assertEqual(check_cmd.summary_line("alpha\nbeta\n\n", ("zeta",)), "beta")

    def test_empty_output_does_not_crash(self):
        self.assertEqual(check_cmd.summary_line("", ("x",)), "no output")


class LaunchctlParseTest(unittest.TestCase):
    LIST = """{
\t"StandardOutPath" = "/home/user/Library/Logs/lodestar-sync.log";
\t"Label" = "com.lodestar.sync";
\t"LastExitStatus" = 0;
\t"PID" = 4321;
};
"""

    def test_it_reads_the_last_exit_status(self):
        parsed = check_cmd.parse_launchctl(self.LIST)
        self.assertEqual(parsed["LastExitStatus"], "0")
        self.assertEqual(parsed["PID"], "4321")
        self.assertEqual(parsed["Label"], "com.lodestar.sync")

    def test_a_missing_key_is_simply_absent(self):
        self.assertNotIn("PID", check_cmd.parse_launchctl(
            '{\n\t"LastExitStatus" = 0;\n};\n'))


class DecodeExitTest(unittest.TestCase):
    """`launchctl list <label>` reports a wait(2) status, not an exit code."""

    def test_zero_is_zero(self):
        self.assertEqual(check_cmd.decode_exit("0"), (0, "last exit 0"))

    def test_256_is_exit_one_not_exit_256(self):
        status, phrase = check_cmd.decode_exit("256")
        self.assertEqual(status, 1)
        self.assertIn("last exit 1", phrase)
        self.assertNotIn("256", phrase)

    def test_a_signal_is_named_as_a_signal(self):
        status, phrase = check_cmd.decode_exit("9")
        self.assertEqual(status, 9)
        self.assertIn("signal 9", phrase)

    def test_garbage_does_not_crash(self):
        status, phrase = check_cmd.decode_exit("wat")
        self.assertIsNone(status)
        self.assertIn("wat", phrase)


class SyncDetailTest(unittest.TestCase):
    CLEAN = ("DRY RUN: sweep as desktop — 17 repo(s)\n"
             "  ⏭ Lodestar                 changed within the last 10m\n"
             "   · Dialexis                 up to date\n"
             "\n  /tmp/x/SYNC.md   ·   status: /home/user/dev/.lodestar/"
             "sync-status.json\n")

    DIRTY = CLEAN.rstrip("\n") + ("\n  🚩 Fetcher                  conflict on rebase\n"
                                  "  🚩 1 repo(s) need you\n")

    def test_a_clean_sweep_reports_the_repo_count_not_the_status_path(self):
        detail = check_cmd.sync_detail(self.CLEAN)
        self.assertIn("17 repo(s)", detail)
        self.assertIn("0 flagged", detail)
        self.assertNotIn("sync-status.json", detail)

    def test_a_flagged_repo_survives_into_the_one_line_detail(self):
        detail = check_cmd.sync_detail(self.DIRTY)
        self.assertIn("1 flagged", detail)
        self.assertIn("Fetcher", detail)

    def test_unrecognisable_output_falls_back_rather_than_lying(self):
        self.assertEqual(check_cmd.sync_detail("something else entirely\n"),
                         "something else entirely")

    def test_a_wall_of_flagged_repos_is_capped_but_counted(self):
        """Observed for real: SSH went down and the sweep flagged 14 repos at once."""
        rows = "".join("  🚩 repo%d                 fetch failed\n" % i for i in range(14))
        detail = check_cmd.sync_detail(
            "DRY RUN: sweep as desktop — 25 repo(s)\n" + rows + "  🚩 14 repo(s) need you\n")
        self.assertIn("14 flagged", detail)
        self.assertIn("and 11 more", detail)
        self.assertNotIn("repo13", detail)


FETCH_FAIL_TAIL = ("fetch failed: ERROR: Repository not found. and the repository exists.")


def sweep_output(*rows, **kwargs):
    """A `sync.py --dry-run` stdout, with the rows given as raw report lines."""
    count = kwargs.get("count", 26)
    text = "DRY RUN: sweep as desktop — %d repo(s)\n" % count
    for row in rows:
        text += "  %s\n" % row
    return text + "\n  /tmp/x/SYNC.md   ·   status: /tmp/sync-status.json\n"


def fetch_row(name, detail=FETCH_FAIL_TAIL):
    return "🚩 %-24s %s" % (name, detail)


def clean_row(name):
    """A repo this sweep fetched fine — i.e. a candidate control for the retry."""
    return "· %-24s up to date" % name


class AttentionRowsTest(unittest.TestCase):
    """The sweep's own report lines, parsed back into (name, detail, kind)."""

    def test_a_flagged_row_and_a_contended_row_are_both_read(self):
        rows = check_cmd.attention_rows(sweep_output(
            fetch_row("Placard"),
            "🔓 Fetcher                  removed a stale .git/index.lock",
            "⏭ Lodestar                 changed within the last 10m",
            "· Dialexis                 up to date"))
        self.assertEqual([(r.name, r.kind) for r in rows],
                         [("Placard", check_cmd.FLAGGED_ROW),
                          ("Fetcher", check_cmd.CONTENDED_ROW)])
        self.assertIn("and the repository exists", rows[0].detail)

    def test_the_repo_count_tally_line_is_not_a_repo(self):
        rows = check_cmd.attention_rows(sweep_output("🚩 14 repo(s) need you",
                                                    "🔓 2 repo(s) hit git lock contention"))
        self.assertEqual(rows, [])

    def test_a_clean_sweep_has_no_attention_rows(self):
        self.assertEqual(check_cmd.attention_rows(SyncDetailTest.CLEAN), [])


class ControlCandidateTest(unittest.TestCase):
    """The retry needs a control: a repo this same sweep provably fetched seconds ago."""

    def test_clean_synced_and_contended_repos_are_all_candidates(self):
        names = check_cmd.control_candidates(sweep_output(
            clean_row("Dialexis"),
            "✅ Placard                  pushed 1",
            "🔓 Fetcher                  retried once"))
        self.assertEqual(names, ["Dialexis", "Placard", "Fetcher"])

    def test_a_skipped_repo_is_not_a_control(self):
        """A skip can happen *before* the fetch: wrong branch, no upstream. It proves nothing."""
        self.assertEqual(check_cmd.control_candidates(sweep_output(
            "⏭ Lodestar                 changed within the last 10m")), [])

    def test_a_flagged_repo_is_not_a_control(self):
        self.assertEqual(check_cmd.control_candidates(sweep_output(fetch_row("Placard"))), [])

    def test_the_live_result_offers_every_unflagged_repo_as_a_control(self):
        """Not "clean means fetched": sync.py now fetches a quiet repo on a tier cadence."""
        path = os.path.join(self.dir, "SYNC.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(sync_md([("·", "Dialexis", "clean", "ok"),
                                  ("✅", "Placard", "synced", "pushed"),
                                  ("⏭", "Lodestar", "skipped", "settling"),
                                  ("🚩", "Ghost", "flagged", FETCH_FAIL_TAIL)]))
        sweep = check_cmd.read_live_sweep(path)
        self.assertIn("Dialexis", sweep.controls)
        self.assertIn("Placard", sweep.controls)
        self.assertNotIn("Ghost", sweep.controls)

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lodestar-check-ctl-")
        self.addCleanup(shutil.rmtree, self.dir, True)


class FetchFailureTest(unittest.TestCase):
    """Which flagged details are worth one retry, and which are the sweep's real verdict."""

    def test_the_throttled_shape_is_recognised(self):
        self.assertTrue(check_cmd.is_fetch_failure(FETCH_FAIL_TAIL))

    def test_a_conflict_is_not_a_fetch_failure(self):
        self.assertFalse(check_cmd.is_fetch_failure(
            "conflict on rebase — aborted and left as it was; needs you"))

    def test_a_missing_clone_is_not_a_fetch_failure(self):
        """The documented second-machine case: never cloned there. A retry cannot fix it."""
        self.assertFalse(check_cmd.is_fetch_failure("missing — needs you; not touched"))


def sync_md(rows, dry_run=False, remote_column=True):
    """A `SYNC.md` as bin/sync.py writes it. `rows` are (icon, name, outcome, detail).

    The `Remote` column is the one sync.py grew on 2026-09-01, and it is included by default
    precisely so these tests prove the reader survives a column being added — the table is
    read by header name, not by position.
    """
    text = ("# Sync health — desktop\n\n<!-- Generated by bin/sync.py. -->\n\n"
            "Last sweep: **2026-09-01T12:00:00-07:00**%s\n\n"
            "## All repos\n\nSome prose about tiers.\n\n" % ("  · DRY RUN" if dry_run else ""))
    if remote_column:
        text += "| | Repo | Outcome | Remote | Detail |\n|-|------|---------|--------|------|\n"
    else:
        text += "| | Repo | Outcome | Detail |\n|-|------|---------|--------|\n"
    for icon, name, outcome, detail in rows:
        cells = [icon, "`%s`" % name, outcome]
        if remote_column:
            cells.append("hot · verified just now")
        cells.append(detail)
        text += "| %s |\n" % " | ".join(cells)
    return text + "\n"


class LiveSweepTest(unittest.TestCase):
    """Reading the live agent's own recent result beats racing it with a second sweep."""

    ONE_CLEAN = [("·", "Dialexis", "clean", "up to date")]

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lodestar-check-live-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "SYNC.md")

    def write(self, rows=None, age=10.0, dry_run=False, text=None):
        body = text if text is not None else sync_md(
            rows if rows is not None else self.ONE_CLEAN, dry_run=dry_run)
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(body)
        stamp = time.time() - age
        os.utime(self.path, (stamp, stamp))
        return self.path

    def test_an_absent_report_is_simply_no_evidence(self):
        self.assertIsNone(check_cmd.read_live_sweep(os.path.join(self.dir, "nope.md")))

    def test_a_fresh_live_sweep_is_read(self):
        sweep = check_cmd.read_live_sweep(self.write(age=41))
        self.assertIsNotNone(sweep)
        self.assertTrue(sweep.live)
        self.assertIn("41s", sweep.header)
        self.assertIn("1 repo(s)", sweep.header)

    def test_a_dry_run_report_is_not_live_evidence(self):
        """Someone ran a dry run by hand. What it would have done is not what happened."""
        self.assertIsNone(check_cmd.read_live_sweep(self.write(age=5, dry_run=True)))

    def test_a_stale_result_is_not_evidence_about_now(self):
        self.assertIsNone(check_cmd.read_live_sweep(
            self.write(age=check_cmd.LIVE_SWEEP_MAX_AGE + 60)))

    def test_a_report_with_no_repo_table_is_no_evidence_rather_than_a_crash(self):
        self.assertIsNone(check_cmd.read_live_sweep(
            self.write(text="# Sync health\n\nLast sweep: **now**\n\nnothing else\n")))

    def test_a_report_with_no_last_sweep_line_is_refused(self):
        self.assertIsNone(check_cmd.read_live_sweep(
            self.write(text=sync_md(self.ONE_CLEAN).replace("Last sweep:", "Swept:"))))

    def test_flagged_and_contended_repos_survive_into_rows(self):
        sweep = check_cmd.read_live_sweep(self.write(age=20, rows=[
            ("🚩", "Placard", "flagged", FETCH_FAIL_TAIL),
            ("🔓", "Fetcher", "lock-contention", "retried once"),
            ("⏭", "Lodestar", "skipped", "settling"),
            ("·", "Dialexis", "clean", "up to date")]))
        self.assertEqual({(r.name, r.kind) for r in sweep.rows},
                         {("Placard", check_cmd.FLAGGED_ROW),
                          ("Fetcher", check_cmd.CONTENDED_ROW)})
        self.assertEqual(sweep.skipped, 1)
        self.assertIn("4 repo(s)", sweep.header)

    def test_an_escaped_pipe_in_a_detail_is_read_back_as_a_pipe(self):
        sweep = check_cmd.read_live_sweep(self.write(rows=[
            ("🚩", "Placard", "flagged", "fetch failed: a \\| b")]))
        self.assertEqual(sweep.rows[0].detail, "fetch failed: a | b")

    def test_it_reads_a_table_without_the_remote_column_too(self):
        """Robust to the column sync.py added, and to its absence on the other machine."""
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(sync_md(self.ONE_CLEAN, remote_column=False))
        sweep = check_cmd.read_live_sweep(self.path)
        self.assertEqual(sweep.controls, ["Dialexis"])


class FakeProbe:
    """A stand-in for one `git fetch`. Records which repos were actually retried."""

    def __init__(self, failures=(), note="still: and the repository exists."):
        self.failures = set(failures)
        self.note = note
        self.probed = []

    def __call__(self, name):
        self.probed.append(name)
        if name in self.failures:
            return False, self.note
        return True, "fetched cleanly"


class RetryTest(unittest.TestCase):
    def setUp(self):
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)

    def test_nothing_to_retry_costs_neither_a_sleep_nor_a_fetch(self):
        probe = FakeProbe()
        result = check_cmd.retry_fetch_failures([], probe, sleep=self.sleep)
        self.assertEqual((result.cleared, result.persistent, result.unretried), ([], [], []))
        self.assertEqual(probe.probed, [])
        self.assertEqual(self.slept, [])

    def test_a_transient_failure_clears_on_the_retry(self):
        probe = FakeProbe()
        result = check_cmd.retry_fetch_failures(["Placard", "Fetcher"], probe,
                                                sleep=self.sleep)
        self.assertEqual(result.cleared, ["Placard", "Fetcher"])
        self.assertEqual(result.persistent, [])

    def test_a_persistent_failure_is_kept_with_its_message(self):
        probe = FakeProbe(failures=["Placard"])
        result = check_cmd.retry_fetch_failures(["Placard", "Fetcher"], probe,
                                                sleep=self.sleep)
        self.assertEqual(result.cleared, ["Fetcher"])
        self.assertEqual([n for n, _ in result.persistent], ["Placard"])
        self.assertIn("repository exists", result.persistent[0][1])

    def test_only_the_failing_repos_are_retried_never_the_whole_sweep(self):
        probe = FakeProbe()
        check_cmd.retry_fetch_failures(["Placard"], probe, sleep=self.sleep)
        self.assertEqual(probe.probed, ["Placard"])

    def test_the_retry_is_bounded_to_exactly_one_attempt_each(self):
        probe = FakeProbe(failures=["Placard"])
        check_cmd.retry_fetch_failures(["Placard"], probe, sleep=self.sleep)
        self.assertEqual(probe.probed, ["Placard"])

    def test_it_waits_once_before_retrying_rather_than_hammering(self):
        check_cmd.retry_fetch_failures(["Placard", "Fetcher"], FakeProbe(),
                                       sleep=self.sleep, delay=3)
        self.assertEqual(self.slept, [3])

    def test_a_working_control_is_probed_on_both_sides_of_the_retry(self):
        """Bracketed on purpose: good before and good after means conditions held throughout."""
        probe = FakeProbe(failures=["Placard"])
        result = check_cmd.retry_fetch_failures(["Placard"], probe, controls=["Dialexis"],
                                                sleep=self.sleep)
        self.assertEqual(probe.probed, ["Dialexis", "Placard", "Dialexis"])
        self.assertTrue(result.control_ok)
        self.assertEqual(result.control, "Dialexis")

    def test_a_control_that_fails_first_invalidates_the_retry(self):
        """The measured case: throttling so hard that the retry itself measures nothing."""
        probe = FakeProbe(failures=["Placard", "Dialexis"])
        result = check_cmd.retry_fetch_failures(["Placard"], probe, controls=["Dialexis"],
                                                sleep=self.sleep)
        self.assertFalse(result.control_ok)
        self.assertEqual([n for n, _ in result.persistent], ["Placard"])

    def test_a_control_that_fails_afterwards_invalidates_it_too(self):
        calls = []

        def probe(name):
            calls.append(name)
            if name == "Dialexis":
                return len(calls) == 1, "closed by remote host"   # good before, bad after
            return False, "and the repository exists."
        result = check_cmd.retry_fetch_failures(["Placard"], probe, controls=["Dialexis"],
                                                sleep=self.sleep)
        self.assertFalse(result.control_ok)
        self.assertIn("changed underneath", result.control_note)

    def test_an_unfetchable_control_candidate_falls_through_to_the_next(self):
        """`~/.claude` is a row in the sweep with no registry entry. It is not a failed control."""
        probe = FakeProbe(failures=["~/.claude"])
        result = check_cmd.retry_fetch_failures(["Placard"], probe,
                                                controls=["~/.claude", "Dialexis"],
                                                sleep=self.sleep)
        self.assertEqual(result.control, "Dialexis")
        self.assertTrue(result.control_ok)

    def test_no_control_at_all_is_recorded_as_such(self):
        result = check_cmd.retry_fetch_failures(["Placard"], FakeProbe(failures=["Placard"]),
                                                controls=[], sleep=self.sleep)
        self.assertEqual(result.control, "")
        self.assertFalse(result.control_ok)
        self.assertIn("no working control", result.control_note)

    def test_the_closing_bracket_is_skipped_when_there_is_nothing_to_validate(self):
        probe = FakeProbe()
        check_cmd.retry_fetch_failures(["Placard"], probe, controls=["Dialexis"],
                                       sleep=self.sleep)
        self.assertEqual(probe.probed, ["Dialexis", "Placard"])

    def test_a_spent_budget_leaves_repos_unretried_and_says_so(self):
        probe = FakeProbe()
        clock = iter([100.0, 999.0, 999.0])   # started, then well past the budget
        result = check_cmd.retry_fetch_failures(
            ["A", "B", "C"], probe, sleep=self.sleep, budget=1,
            now=lambda: next(clock))
        self.assertEqual(probe.probed, ["A"])   # at least one always happens
        self.assertEqual(result.unretried, ["B", "C"])


class SyncCheckTest(unittest.TestCase):
    """The sync row end to end: parse, retry the failures, and grade honestly."""

    def setUp(self):
        self.slept = []

    def sync(self, output=None, code=0, timed_out=False, failures=(), live=None,
             sweeping=False, force_sweep=False):
        self.probe = FakeProbe(failures=failures)

        def run(argv, root=None, timeout=None):
            self.ran = (tuple(argv), root, timeout)
            return (None, output or "", True) if timed_out else (code, output or "", False)

        self.ran = None
        return check_cmd.sync_check(run, "/tmp/snapshot", report_file=live,
                                    probe=self.probe, sleep=self.slept.append,
                                    sweeping=sweeping, force_sweep=force_sweep)

    def test_a_clean_sweep_is_ok_and_retries_nothing(self):
        row = self.sync(output=SyncDetailTest.CLEAN)
        self.assertEqual(row.verdict, check_cmd.OK)
        self.assertEqual(self.probe.probed, [])

    def test_a_fetch_failure_that_clears_on_retry_is_unproven_not_flagged(self):
        row = self.sync(output=sweep_output(fetch_row("Placard"), fetch_row("Fetcher"),
                                            "🚩 2 repo(s) need you"))
        self.assertEqual(row.verdict, check_cmd.UNPROVEN)
        self.assertEqual(sorted(self.probe.probed), ["Fetcher", "Placard"])
        self.assertIn("0 flagged", row.detail)
        self.assertIn("2 fetch failure(s)", row.detail)
        self.assertIn("transient", row.detail.lower())

    def test_a_fetch_failure_that_persists_beside_a_working_control_is_flagged(self):
        row = self.sync(output=sweep_output(clean_row("Dialexis"), fetch_row("Placard"),
                                           fetch_row("Fetcher")),
                        failures=["Placard"])
        self.assertEqual(row.verdict, check_cmd.FAIL)
        self.assertIn("Placard", row.detail)
        self.assertIn("1 flagged", row.detail)
        self.assertIn("control Dialexis", row.detail)
        self.assertNotIn("Fetcher", row.detail)   # it cleared, so it is not news

    def test_a_repo_failing_twice_while_the_control_also_fails_is_unproven(self):
        """The measured false alarm: 19 provably-fine repos "failed twice" under throttling.

        They stay in the flagged count and are all named — nothing is dropped — but the row
        does not get to call them broken, because the retry demonstrably measured nothing.
        """
        row = self.sync(output=sweep_output(clean_row("Dialexis"), fetch_row("Placard"),
                                           fetch_row("Fetcher")),
                        failures=["Placard", "Fetcher", "Dialexis"])
        self.assertEqual(row.verdict, check_cmd.UNPROVEN)
        self.assertIn("2 flagged", row.detail)          # never dropped
        self.assertIn("Placard", row.detail)
        self.assertIn("proves", row.detail.lower())

    def test_a_sweep_where_nothing_fetched_at_all_is_a_loud_failure(self):
        """No control exists because no remote worked. That is an outage, not throttling."""
        row = self.sync(output=sweep_output(fetch_row("A"), fetch_row("B")),
                        failures=["A", "B"])
        self.assertEqual(row.verdict, check_cmd.FAIL)
        self.assertIn("2 flagged", row.detail)
        self.assertIn("no working control", row.detail)

    def test_a_non_fetch_flagged_repo_still_only_warns_and_is_never_retried(self):
        row = self.sync(output=sweep_output(
            "🚩 Fetcher                  conflict on rebase — needs you"))
        self.assertEqual(row.verdict, check_cmd.WARN)
        self.assertEqual(self.probe.probed, [])
        self.assertIn("Fetcher", row.detail)

    def test_lock_contention_still_warns(self):
        row = self.sync(output=sweep_output(
            "🔓 Placard                  removed a stale .git/index.lock"))
        self.assertEqual(row.verdict, check_cmd.WARN)

    def test_a_warning_outranks_an_unproven_fetch_in_the_same_sweep(self):
        row = self.sync(output=sweep_output(
            fetch_row("Placard"),
            "🚩 Fetcher                  conflict on rebase — needs you"))
        self.assertEqual(row.verdict, check_cmd.WARN)
        self.assertIn("cleared", row.detail)

    def test_a_non_zero_sweep_exit_still_fails(self):
        row = self.sync(output="boom\n", code=2)
        self.assertEqual(row.verdict, check_cmd.FAIL)

    def test_a_hung_sweep_is_a_timeout_not_a_hang(self):
        row = self.sync(timed_out=True)
        self.assertEqual(row.verdict, check_cmd.FAIL)
        self.assertIn("timed out", row.detail)

    def test_a_concurrent_live_sweep_is_named_as_weak_evidence(self):
        row = self.sync(output=sweep_output(fetch_row("Placard")), sweeping=True)
        self.assertIn("weak evidence", row.detail)
        self.assertIn("mid-sweep", row.detail)

    def test_an_unresolvable_repo_is_kept_flagged_rather_than_dropped(self):
        """A repo the retry cannot even find must never quietly become healthy."""
        def probe(name):
            return False, "not in sync/registry.md, so it could not be retried"
        row = check_cmd.sync_check(
            lambda argv, root=None, timeout=None: (
                0, sweep_output(fetch_row("Ghost")), False),
            "/tmp/snapshot", report_file=None, probe=probe,
            sleep=self.slept.append)
        self.assertEqual(row.verdict, check_cmd.FAIL)
        self.assertIn("Ghost", row.detail)


class SyncCheckLiveTest(unittest.TestCase):
    """Reading the live sweep's own result instead of racing it."""

    CLEAN_ROWS = [("·", "Dialexis", "clean", "up to date")]

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lodestar-check-livecheck-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "SYNC.md")
        self.calls = []

    def write(self, rows, age=30.0):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(sync_md(rows))
        stamp = time.time() - age
        os.utime(self.path, (stamp, stamp))
        return self.path

    def run_stub(self, argv, root=None, timeout=None):
        self.calls.append(tuple(argv))
        return 0, sweep_output(), False

    def sync(self, rows, force_sweep=False, failures=()):
        self.probe = FakeProbe(failures=failures)
        return check_cmd.sync_check(self.run_stub, "/tmp/snapshot",
                                    report_file=self.write(rows), probe=self.probe,
                                    sleep=lambda _s: None, force_sweep=force_sweep)

    def test_a_fresh_live_result_replaces_the_second_sweep_entirely(self):
        row = self.sync(self.CLEAN_ROWS)
        self.assertEqual(self.calls, [], "it raced the live sweep instead of reading it")
        self.assertEqual(row.verdict, check_cmd.OK)
        self.assertIn("live sweep", row.detail)

    def test_a_flagged_fetch_in_the_live_result_is_retried_too(self):
        row = self.sync([("🚩", "Placard", "flagged", FETCH_FAIL_TAIL)])
        self.assertEqual(self.probe.probed, ["Placard"])
        self.assertEqual(row.verdict, check_cmd.UNPROVEN)

    def test_sweep_forces_its_own_run_even_when_a_live_result_exists(self):
        self.sync(self.CLEAN_ROWS, force_sweep=True)
        self.assertTrue(self.calls)
        self.assertIn("--dry-run", self.calls[0])


class TestsDetailTest(unittest.TestCase):
    def test_the_count_and_the_verdict_both_appear(self):
        detail = check_cmd.tests_detail("Ran 548 tests in 29.595s\n\nOK\n")
        self.assertIn("548", detail)
        self.assertIn("OK", detail)

    def test_a_failure_is_never_hidden_behind_the_count(self):
        detail = check_cmd.tests_detail(
            "Ran 548 tests in 30.204s\n\nFAILED (errors=1)\n")
        self.assertIn("548", detail)
        self.assertIn("FAILED", detail)

    def test_missing_verdict_falls_back_to_the_count(self):
        self.assertIn("548", check_cmd.tests_detail("Ran 548 tests in 1s\n"))


class SystemdAgentRowTest(unittest.TestCase):
    """CHECK_FIX_LAUNCHAGENTS_ON_WSL: on Linux the sweep is a systemd timer, not a LaunchAgent."""

    def fake(self, body):
        d = tempfile.mkdtemp(prefix="lodestar-fakesystemctl-")
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "systemctl")
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)
        return path

    SHOW = ("printf 'Id=lodestar-sync.timer\\nLoadState=%s\\nActiveState=%s\\n\\n"
            "Id=lodestar-sync.service\\nActiveState=inactive\\nExecMainStatus=%s\\nMainPID=0\\n'")

    def test_an_active_timer_with_a_clean_last_run_is_ok(self):
        sc = self.fake(self.SHOW % ("loaded", "active", "0"))
        code, text = check_cmd.systemd_as_launchctl("com.lodestar.sync", sc)
        verdict, detail = check_cmd.agent_state("com.lodestar.sync", True, code, text)
        self.assertEqual(verdict, check_cmd.OK, detail)

    def test_a_failed_last_run_warns(self):
        sc = self.fake(self.SHOW % ("loaded", "active", "1"))
        code, text = check_cmd.systemd_as_launchctl("com.lodestar.sync", sc)
        self.assertEqual(check_cmd.agent_state("com.lodestar.sync", True, code, text)[0],
                         check_cmd.WARN)

    def test_a_missing_timer_is_not_loaded(self):
        sc = self.fake(self.SHOW % ("not-found", "inactive", "0"))
        self.assertEqual(check_cmd.systemd_as_launchctl("com.lodestar.sync", sc)[0], 113)

    def test_an_optional_label_without_a_unit_is_ok(self):
        code, text = check_cmd.systemd_as_launchctl("com.lodestar.shadow", "/nonexistent")
        self.assertEqual(check_cmd.agent_state("com.lodestar.shadow", False, code, text)[0],
                         check_cmd.OK)


class AgentRowTest(unittest.TestCase):
    def test_a_loaded_agent_that_last_exited_zero_is_ok(self):
        verdict, detail = check_cmd.agent_state(
            "com.lodestar.sync", True, 0, '{\n\t"LastExitStatus" = 0;\n};\n')
        self.assertEqual(verdict, check_cmd.OK)
        self.assertIn("last exit 0", detail)

    def test_a_loaded_agent_with_a_non_zero_last_exit_warns(self):
        # 256, not 1: this is the real wait-status shape launchctl emits for `exit(1)`,
        # observed from a launchd agent 2026-09-01.
        verdict, detail = check_cmd.agent_state(
            "com.machinist.m365reconnect", False, 0,
            '{\n\t"LastExitStatus" = 256;\n};\n')
        self.assertEqual(verdict, check_cmd.WARN)
        self.assertIn("last exit 1", detail)

    def test_an_absent_optional_agent_is_a_state_not_a_problem(self):
        verdict, detail = check_cmd.agent_state(
            "com.lodestar.shadow", False, 113,
            'Could not find service "com.lodestar.shadow" in domain for port\n')
        self.assertEqual(verdict, check_cmd.OK)
        self.assertIn("not loaded", detail)

    def test_an_absent_required_agent_warns_and_never_hard_fails(self):
        """Absence is reportable, per the brief — but it must not be quiet."""
        verdict, detail = check_cmd.agent_state("com.lodestar.sync", True, 113, "")
        self.assertEqual(verdict, check_cmd.WARN)
        self.assertIn("not loaded", detail)


class PendingCapturesRowTest(unittest.TestCase):
    """The adopt queue (D18). A capture means the sweep refused to render — say so."""

    def row(self, paths):
        return check_cmd.pending_captures_check(lambda: paths)

    def test_an_empty_queue_is_a_pass(self):
        entry = self.row([])
        self.assertEqual(entry.verdict, check_cmd.OK)
        self.assertEqual(entry.tier, check_cmd.HEALTH)

    def test_a_pending_capture_warns_rather_than_failing(self):
        """"Needs you" is not "broken". FAIL here would make exit 1 mean two things."""
        entry = self.row(["/b/claude/adopt/mt-1/20260901T101500-CLAUDE.md"])
        self.assertEqual(entry.verdict, check_cmd.WARN)
        self.assertIn("1 capture", entry.detail)

    def test_the_count_and_the_clearing_command_both_appear(self):
        entry = self.row(["/b/a/20260901T101500-CLAUDE.md",
                          "/b/a/20260901T104500-CLAUDE.md"])
        self.assertIn("2 capture", entry.detail)
        self.assertIn("claudeconf.py adopt", entry.hint)

    def test_the_rows_are_named_not_just_counted(self):
        entry = self.row(["/board/claude/adopt/mt-1/20260901T101500-CLAUDE.md"])
        self.assertIn("20260901T101500-CLAUDE.md", entry.detail)
        # The full paths stay available under --verbose; the row itself shows basenames.
        self.assertIn("/board/claude/adopt/mt-1", entry.output)

    def test_many_captures_are_capped_on_the_row_but_never_dropped(self):
        paths = ["/b/a/2026090%dT100000-CLAUDE.md" % n for n in range(1, 8)]
        entry = self.row(paths)
        self.assertIn("7 capture", entry.detail)
        self.assertIn("more", entry.detail)
        for path in paths:
            self.assertIn(path, entry.output)

    def test_a_raising_queue_is_unproven_and_never_kills_the_row(self):
        def boom():
            raise RuntimeError("adopt dir is not a directory")
        entry = check_cmd.pending_captures_check(boom)
        self.assertEqual(entry.verdict, check_cmd.UNPROVEN)
        self.assertIn("adopt dir is not a directory", entry.detail)


class SkillsRowTest(unittest.TestCase):
    """Skill wiring. A dangling symlink does not error — Claude Code just never sees it."""

    def row(self, lines, faults):
        return check_cmd.skills_check(lambda: (lines, faults))

    def test_all_linked_and_valid_is_a_pass_and_says_the_count(self):
        entry = self.row(["21 skill(s) linked and valid"], [])
        self.assertEqual(entry.verdict, check_cmd.OK)
        self.assertIn("21 skill(s)", entry.detail)

    def test_a_dangling_link_warns_rather_than_failing(self):
        entry = self.row([], ["dangling: measuring-large-artifacts -> ~/dev/… not cloned"])
        self.assertEqual(entry.verdict, check_cmd.WARN)
        self.assertIn("measuring-large-artifacts", entry.detail)

    def test_a_broken_skill_md_warns_too(self):
        entry = self.row([], ["broken: placard SKILL.md frontmatter unparseable"])
        self.assertEqual(entry.verdict, check_cmd.WARN)
        self.assertIn("1 wiring fault", entry.detail)

    def test_the_hint_says_why_a_dangling_link_is_invisible(self):
        """The reason this row exists: nothing else on the machine reports the absence."""
        entry = self.row([], ["dangling: fetcher -> ~/dev/Fetcher/.claude/skills/fetcher"])
        self.assertIn("does not error", entry.hint)

    def test_faults_are_capped_on_the_row_and_all_of_them_reach_verbose(self):
        faults = ["dangling: skill-%d" % n for n in range(6)]
        entry = self.row(["some informational line"], faults)
        self.assertIn("6 wiring fault", entry.detail)
        self.assertIn("more", entry.detail)
        for fault in faults:
            self.assertIn(fault, entry.output)
        self.assertIn("some informational line", entry.output)

    def test_an_unseeded_table_is_informational_not_a_fault(self):
        """No `claude/skills.md` means seeding has not happened — that is not breakage."""
        entry = self.row(["no claude/skills.md yet — run: claudeconf.py skills --import"], [])
        self.assertEqual(entry.verdict, check_cmd.OK)
        self.assertIn("skills --import", entry.detail)

    def test_unlinked_skills_arrive_as_lines_and_never_grade(self):
        entry = self.row(["26 unlinked skill(s) in ~/dev (reported, never linked)"], [])
        self.assertEqual(entry.verdict, check_cmd.OK)

    def test_nothing_at_all_to_report_still_renders_a_detail(self):
        entry = self.row([], [])
        self.assertEqual(entry.verdict, check_cmd.OK)
        self.assertTrue(entry.detail.strip())

    def test_a_raising_reconcile_is_unproven_not_a_dead_check(self):
        """`skills_state` can raise on an odd repo; that must not end the whole run."""
        def boom():
            raise ValueError("skills.md table has no Target column")
        entry = check_cmd.skills_check(boom)
        self.assertEqual(entry.verdict, check_cmd.UNPROVEN)
        self.assertIn("Target column", entry.detail)

    def test_the_skill_state_is_only_ever_read_never_applied(self):
        """check.py reports on the wiring and must never be the thing that changes it."""
        import claudeconf
        seen = {}

        def fake_state(**kwargs):
            seen.update(kwargs)
            return [], []

        real = claudeconf.skills_state
        claudeconf.skills_state = fake_state
        try:
            check_cmd._skills_state()
        finally:
            claudeconf.skills_state = real
        self.assertEqual(seen, {"dry_run": True})


def hb(key, role, age_seconds, known=True, as_of="2026-09-01T09:00:00-07:00"):
    """One row shaped like `heartbeat.machines()` produces them.

    Faithful to the real field set rather than trimmed to what `machines_check` reads, so a
    row built here stays a plausible stand-in if the reader starts consulting another field.
    """
    return {"key": key, "role": role, "known": known,
            "as_of": None if age_seconds is None and as_of is None else as_of,
            "age_seconds": age_seconds,
            "silent": age_seconds is None or age_seconds > 2 * 3600,
            "runtime": "shell", "repos": None, "flagged": None}


def hb_facts(*rows, **kwargs):
    installed = kwargs.pop("installed", True)
    assert not kwargs, kwargs
    return {"installed": installed, "machines": list(rows), "known": len(rows),
            "fresh": [], "silent": [], "age_seconds": None, "stale": False,
            "threshold_seconds": 2 * 3600, "today": "2026-09-01", "summary": "stub"}


def _restore_env(saved):
    """Put the watched environment back, reading a snapshot passed BY VALUE.

    Deliberately a module-level function taking the dict as an argument rather than a bound
    method reading `self._saved` at teardown — see tests/test_zz_env_isolation.py for the leak
    that shape permits.
    """
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


class MachineLivenessRowTest(unittest.TestCase):
    """The reading half of the heartbeat (docs/LAPTOP_PARITY.md §4 item 1, §6 defect 2).

    The property being defended in both directions at once, which is why the threshold gets a
    test of its own: **silence must stop reading as health**, and **a laptop being shut for a
    weekend must not read as a fault**. A row that satisfies only the first cries wolf every
    Monday and stops being read, which is the same failure the sync row above was rewritten to
    stop making.

    `this` is passed explicitly throughout so nothing here calls `machine.key()` — these tests
    must not depend on which machine runs them, and must not touch the environment to say so.
    """

    def row(self, *rows, **kwargs):
        this = kwargs.pop("this", "self-box")
        return check_cmd.machines_check(lambda: hb_facts(*rows, **kwargs), this=this)

    def test_a_peer_seen_recently_passes_and_its_age_is_on_the_row(self):
        """"OK" alone would be the same uninformative silence this row exists to break."""
        entry = self.row(hb("other-box", "laptop", 3 * 3600))
        self.assertEqual(entry.verdict, check_cmd.OK)
        self.assertEqual(entry.tier, check_cmd.HEALTH)
        self.assertIn("laptop", entry.detail)
        self.assertIn("other-box", entry.detail)
        self.assertIn("3.0h ago", entry.detail)

    def test_a_peer_quiet_past_the_threshold_warns_and_never_fails(self):
        entry = self.row(hb("other-box", "laptop", 9 * 86400))
        self.assertEqual(entry.verdict, check_cmd.WARN)
        self.assertIn("QUIET", entry.detail)
        self.assertIn("9.0d", entry.detail)

    def test_the_threshold_clears_an_ordinary_weekend(self):
        """The defence of the number itself: Friday afternoon to Monday morning is ~67 h.

        `heartbeat.SILENT_AFTER_SECONDS` is 2 h, which would fire on this and therefore on
        every ordinary evening off. That is the reason this row does not reuse it.
        """
        entry = self.row(hb("other-box", "laptop", 67 * 3600))
        self.assertEqual(entry.verdict, check_cmd.OK)
        self.assertGreater(check_cmd.QUIET_AFTER_SECONDS, 67 * 3600)

    def test_the_threshold_is_days_not_hours(self):
        """A one-hour threshold would be indefensible for a machine that sleeps nightly."""
        self.assertGreaterEqual(check_cmd.QUIET_AFTER_SECONDS, 2 * 86400)

    def test_never_seen_is_a_different_finding_from_gone_quiet(self):
        """Same verdict, different problem, different fix — so it must not read as an age."""
        never = self.row(hb("other-box", "laptop", None, as_of=None))
        quiet = self.row(hb("other-box", "laptop", 9 * 86400))
        self.assertEqual(never.verdict, check_cmd.WARN)
        self.assertIn("NEVER", never.detail)
        self.assertNotIn("NEVER", quiet.detail)
        self.assertIn("never had the agent installed", never.hint)
        self.assertNotIn("never had the agent installed", quiet.hint)

    def test_a_quiet_peer_is_pointed_at_the_launchagent_not_at_a_fault_here(self):
        entry = self.row(hb("other-box", "laptop", 9 * 86400))
        self.assertIn("not a fault", entry.hint)
        self.assertIn("LaunchAgent", entry.hint)

    def test_no_heartbeat_anywhere_is_unproven_and_never_a_failure(self):
        """A fresh clone. "Never written" is not "written and old"; only the second warns."""
        entry = self.row(hb("other-box", "laptop", None, as_of=None), installed=False)
        self.assertEqual(entry.verdict, check_cmd.UNPROVEN)
        self.assertNotEqual(entry.verdict, check_cmd.FAIL)
        self.assertIn("ever written a heartbeat", entry.detail)

    def test_unreadable_heartbeat_data_is_unproven_not_a_dead_row(self):
        def boom():
            raise ValueError("journal/2026-09-01 is not a directory")
        entry = check_cmd.machines_check(boom, this="self-box")
        self.assertEqual(entry.verdict, check_cmd.UNPROVEN)
        self.assertIn("not a directory", entry.detail)

    def test_a_timestamp_that_will_not_parse_is_unproven_for_that_machine(self):
        entry = self.row(hb("other-box", "laptop", None, as_of="yesterday-ish"))
        self.assertEqual(entry.verdict, check_cmd.UNPROVEN)
        self.assertIn("will not parse", entry.detail)

    def test_a_proven_warning_outranks_an_unproven_row(self):
        """`worst()`'s ordering, exercised where this row can actually produce both."""
        entry = self.row(hb("a-box", "laptop", 9 * 86400),
                         hb("b-box", "mini", None, as_of="nonsense"))
        self.assertEqual(entry.verdict, check_cmd.WARN)

    def test_this_machine_is_excluded_rather_than_measured(self):
        """You are running check.py on it; `sync freshness` owns its local sweep."""
        entry = self.row(hb("self-box", "desktop", None, as_of=None),
                         hb("other-box", "laptop", 600))
        self.assertEqual(entry.verdict, check_cmd.OK)
        self.assertNotIn("self-box", entry.detail)
        self.assertIn("1 other machine(s)", entry.detail)

    def test_an_unregistered_key_is_named_but_never_grades(self):
        """machines.md already makes an unlisted machine fail loudly on itself."""
        entry = self.row(hb("other-box", "laptop", 600),
                         hb("mystery-box", "?", 9 * 86400, known=False))
        self.assertEqual(entry.verdict, check_cmd.OK)
        self.assertIn("mystery-box", entry.detail)
        self.assertIn("unregistered", entry.detail)

    def test_a_single_machine_board_has_nothing_to_measure_and_says_so(self):
        entry = self.row(hb("self-box", "desktop", 600))
        self.assertEqual(entry.verdict, check_cmd.OK)
        self.assertIn("nothing to measure", entry.detail)

    def test_every_machine_reaches_verbose_output_even_when_the_row_is_capped(self):
        rows = [hb("box-%d" % n, "laptop", 9 * 86400) for n in range(6)]
        entry = self.row(*rows)
        self.assertIn("more", entry.detail)
        for row in rows:
            self.assertIn(row["key"], entry.output)

    def test_the_row_reads_the_real_facts_shape(self):
        """The contract hand-built dicts cannot cover: `facts()` actually feeds this row.

        Without this, `heartbeat.facts()` could rename a field and every test above would
        still pass while the live row silently reported nothing.
        """
        from lib import heartbeat

        tmp = tempfile.mkdtemp(prefix="lodestar-check-hb-")
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(os.path.join(tmp, "journal", "2026-09-01"))
        with open(os.path.join(tmp, "machines.md"), "w", encoding="utf-8") as handle:
            handle.write("| Key | Role |\n|-----|------|\n"
                         "| self-box | desktop |\n| other-box | laptop |\n")
        stamp = (datetime.datetime.now().astimezone()
                 - datetime.timedelta(hours=5)).isoformat(timespec="microseconds")
        with open(os.path.join(tmp, "journal", "2026-09-01", "other-box.heartbeat"),
                  "w", encoding="utf-8") as handle:
            handle.write('{"event": "heartbeat", "machine": "laptop", '
                         '"machine_key": "other-box", "ts": "%s"}\n' % stamp)

        saved = {key: os.environ.get(key) for key in ("LODESTAR_ROOT",)}
        self.addCleanup(_restore_env, saved)
        os.environ["LODESTAR_ROOT"] = tmp

        entry = check_cmd.machines_check(heartbeat.facts, this="self-box")
        self.assertEqual(entry.verdict, check_cmd.OK, entry.detail)
        self.assertIn("other-box", entry.detail)
        self.assertIn("5.0h ago", entry.detail)


class SummarizeTest(unittest.TestCase):
    def test_all_healthy_and_all_gates_met_exits_zero(self):
        code, text = check_cmd.summarize(
            [check("a"), check("b", tier=check_cmd.GATE)])
        self.assertEqual(code, check_cmd.EXIT_OK)
        self.assertIn("HEALTHY", text)

    def test_a_health_failure_exits_one(self):
        code, text = check_cmd.summarize([check("a", verdict=check_cmd.FAIL)])
        self.assertEqual(code, check_cmd.EXIT_UNHEALTHY)
        self.assertIn("UNHEALTHY", text)

    def test_an_unmet_gate_alone_exits_three_not_one(self):
        """A gate is a phase verdict. Conflating it with breakage makes check.py useless."""
        code, text = check_cmd.summarize(
            [check("a"), check("g", tier=check_cmd.GATE, verdict=check_cmd.FAIL)])
        self.assertEqual(code, check_cmd.EXIT_GATE_UNMET)
        self.assertIn("gate", text.lower())

    def test_a_health_failure_outranks_an_unmet_gate(self):
        code, _ = check_cmd.summarize(
            [check("a", verdict=check_cmd.FAIL),
             check("g", tier=check_cmd.GATE, verdict=check_cmd.FAIL)])
        self.assertEqual(code, check_cmd.EXIT_UNHEALTHY)

    def test_a_warning_alone_exits_zero_but_strict_makes_it_fail(self):
        code, _ = check_cmd.summarize([check("a", verdict=check_cmd.WARN)])
        self.assertEqual(code, check_cmd.EXIT_OK)
        code, _ = check_cmd.summarize([check("a", verdict=check_cmd.WARN)], strict=True)
        self.assertEqual(code, check_cmd.EXIT_UNHEALTHY)

    def test_a_skipped_check_is_named_and_never_counted_as_passing(self):
        code, text = check_cmd.summarize(
            [check("run_tests.py", verdict=check_cmd.SKIPPED,
                   detail="pass --tests to run it")])
        self.assertEqual(code, check_cmd.EXIT_OK)
        self.assertIn("NOT CHECKED", text)
        self.assertIn("run_tests.py", text)
        # The whole point: the summary must not let a reader infer a pass.
        self.assertIn("not a pass", text.lower())

    def test_the_ok_count_excludes_skipped_checks(self):
        _code, text = check_cmd.summarize(
            [check("a"), check("b", verdict=check_cmd.SKIPPED)])
        self.assertIn("1 ok", text)
        self.assertIn("1 not checked", text)


class UnprovenSummaryTest(unittest.TestCase):
    """UNPROVEN is a third answer: neither "it passed" nor "it is broken"."""

    def unproven(self):
        return check("sync sweep", verdict=check_cmd.UNPROVEN,
                     detail="2 fetch failure(s) cleared on one retry")

    def test_an_unproven_row_alone_does_not_exit_one(self):
        code, text = check_cmd.summarize([check("a"), self.unproven()])
        self.assertEqual(code, check_cmd.EXIT_OK, text)

    def test_strict_does_not_turn_absent_evidence_into_a_failure(self):
        """--strict escalates warnings. An unproven row is not a warning about anything."""
        code, _ = check_cmd.summarize([self.unproven()], strict=True)
        self.assertEqual(code, check_cmd.EXIT_OK)

    def test_it_is_named_loudly_and_never_reads_as_a_pass(self):
        _code, text = check_cmd.summarize([check("a"), self.unproven()])
        self.assertIn("UNPROVEN", text)
        self.assertIn("sync sweep", text)
        self.assertIn("not a pass", text.lower())
        self.assertNotIn("everything checked passed", text)

    def test_it_is_counted_separately_from_ok_warn_and_fail(self):
        _code, text = check_cmd.summarize([check("a"), self.unproven()])
        self.assertIn("1 ok", text)
        self.assertIn("1 unproven", text)
        self.assertIn("0 warn", text)
        self.assertIn("0 fail", text)

    def test_a_real_failure_alongside_it_still_exits_one(self):
        code, _ = check_cmd.summarize([self.unproven(),
                                       check("b", verdict=check_cmd.FAIL)])
        self.assertEqual(code, check_cmd.EXIT_UNHEALTHY)

    def test_an_unmet_gate_alongside_it_still_exits_three(self):
        code, _ = check_cmd.summarize(
            [self.unproven(), check("g", tier=check_cmd.GATE, verdict=check_cmd.FAIL)])
        self.assertEqual(code, check_cmd.EXIT_GATE_UNMET)

    def test_it_renders_with_its_own_marker_rather_than_looking_ok(self):
        text = check_cmd.render([self.unproven()], verbose=False)
        self.assertNotIn(" OK ", text)
        self.assertIn(check_cmd.LABEL[check_cmd.UNPROVEN], text)


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="lodestar-check-src-")
        self.addCleanup(shutil.rmtree, self.root, True)
        os.makedirs(os.path.join(self.root, "loops"))
        os.makedirs(os.path.join(self.root, "sync"))
        self.write("machines.md", MACHINES_MD)
        self.write("BOARD.md", "# Board\n")
        self.write(os.path.join("loops", "demo.md"), LOOP_MD)
        self.write(os.path.join("sync", "registry.md"), "| Name |\n|------|\n")

    def write(self, relpath, text):
        target = os.path.join(self.root, relpath)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_a_snapshot_is_a_copy_and_not_the_original(self):
        into = tempfile.mkdtemp(prefix="lodestar-check-dst-")
        self.addCleanup(shutil.rmtree, into, True)
        snapshot = check_cmd.snapshot_board(self.root, into)
        self.assertNotEqual(os.path.realpath(snapshot), os.path.realpath(self.root))
        self.assertTrue(os.path.exists(os.path.join(snapshot, "loops", "demo.md")))
        self.assertTrue(os.path.exists(os.path.join(snapshot, "machines.md")))

    def test_writing_into_the_snapshot_leaves_the_original_alone(self):
        into = tempfile.mkdtemp(prefix="lodestar-check-dst-")
        self.addCleanup(shutil.rmtree, into, True)
        snapshot = check_cmd.snapshot_board(self.root, into)
        with open(os.path.join(snapshot, "loops", "demo.md"), "w",
                  encoding="utf-8") as handle:
            handle.write("clobbered\n")
        with open(os.path.join(self.root, "loops", "demo.md"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), LOOP_MD)

    def test_an_absent_optional_path_is_skipped_rather_than_fatal(self):
        into = tempfile.mkdtemp(prefix="lodestar-check-dst-")
        self.addCleanup(shutil.rmtree, into, True)
        snapshot = check_cmd.snapshot_board(self.root, into)  # no PAUSE in the source
        self.assertFalse(os.path.exists(os.path.join(snapshot, "PAUSE")))


class DifferingFilesTest(unittest.TestCase):
    def setUp(self):
        self.a = tempfile.mkdtemp(prefix="lodestar-check-a-")
        self.b = tempfile.mkdtemp(prefix="lodestar-check-b-")
        self.addCleanup(shutil.rmtree, self.a, True)
        self.addCleanup(shutil.rmtree, self.b, True)

    def put(self, root, relpath, text):
        target = os.path.join(root, relpath)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_identical_trees_differ_nowhere(self):
        self.put(self.a, "loops/x.md", "same")
        self.put(self.b, "loops/x.md", "same")
        self.assertEqual(check_cmd.differing_files(self.a, self.b, ["loops"]), [])

    def test_a_changed_file_is_named(self):
        self.put(self.a, "loops/x.md", "one")
        self.put(self.b, "loops/x.md", "two")
        self.assertEqual(check_cmd.differing_files(self.a, self.b, ["loops"]),
                         ["loops/x.md"])

    def test_a_file_only_one_side_has_is_named(self):
        self.put(self.a, "loops/x.md", "one")
        self.put(self.b, "loops/y.md", "one")
        self.assertEqual(check_cmd.differing_files(self.a, self.b, ["loops"]),
                         ["loops/x.md", "loops/y.md"])

    def test_a_single_file_subpath_works_too(self):
        self.put(self.a, "BOARD.md", "one")
        self.put(self.b, "BOARD.md", "two")
        self.assertEqual(check_cmd.differing_files(self.a, self.b, ["BOARD.md"]),
                         ["BOARD.md"])


class FakeRunner:
    """Stand-in for the subprocess layer. Records every invocation."""

    def __init__(self, results=None, default=(0, "fine\n")):
        self.results = results or {}
        self.default = default
        self.calls = []

    def __call__(self, argv, root=None, timeout=None):
        self.calls.append((tuple(argv), root, timeout))
        key = next((k for k in self.results if k in " ".join(argv)), None)
        code, output = self.results[key] if key else self.default
        if code is None:
            return None, output, True
        return code, output, False


def fake_launchctl(mapping):
    def run(label):
        if label not in mapping:
            return 113, 'Could not find service "%s" in domain for port\n' % label
        return 0, mapping[label]
    return run


class MainTest(unittest.TestCase):
    """End-to-end over an injected runner, against a temp board."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="lodestar-check-main-")
        self.addCleanup(shutil.rmtree, self.root, True)
        os.makedirs(os.path.join(self.root, "loops"))
        os.makedirs(os.path.join(self.root, "journal"))
        os.makedirs(os.path.join(self.root, "sync"))
        with open(os.path.join(self.root, "machines.md"), "w", encoding="utf-8") as h:
            h.write(MACHINES_MD)
        with open(os.path.join(self.root, "BOARD.md"), "w", encoding="utf-8") as h:
            h.write("# Board\n")
        with open(os.path.join(self.root, "loops", "demo.md"), "w", encoding="utf-8") as h:
            h.write(LOOP_MD)
        self._saved = os.environ.get("LODESTAR_ROOT")
        os.environ["LODESTAR_ROOT"] = self.root
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved is None:
            os.environ.pop("LODESTAR_ROOT", None)
        else:
            os.environ["LODESTAR_ROOT"] = self._saved

    def run_main(self, argv=None, results=None, agents=None, report_file=None,
                 failures=(), probe=None, captures=None, skills=None, machines=None):
        """Every external edge is injected, including the live sweep's report.

        `report_file` defaults to a path that does not exist, so a run reads no live result
        and sweeps for itself. The real default is `$LODESTAR_ROOT/SYNC.md`, which the temp
        board also lacks — this is belt and braces, not a workaround.

        `captures`, `skills` and `machines` are stubbed clean by default for the same reason
        the runner is: left real they would read `~/.claude`, the live skill links and the real
        committed heartbeats, so the suite's verdict would depend on the machine it runs on —
        and on the Laptop's sleep schedule, which is not a property of this code.
        """
        runner = FakeRunner(results)
        self.probe = probe or FakeProbe(failures=failures)
        out = io.StringIO()
        code = check_cmd.main(argv or [], run=runner,
                              launchctl=fake_launchctl(
                                  agents if agents is not None
                                  else {"com.lodestar.sync":
                                        '{\n\t"LastExitStatus" = 0;\n};\n'}),
                              stream=out,
                              report_file=report_file or os.path.join(self.root, "absent"),
                              probe=self.probe, sleep=lambda _s: None,
                              captures=captures or (lambda: []),
                              skills=skills or (lambda: ([], [])),
                              machines=machines or (lambda: {"installed": True,
                                                             "machines": []}))
        return code, out.getvalue(), runner

    def write_live_report(self, rows, age=30.0):
        path = os.path.join(self.root, "live-SYNC.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(sync_md(rows))
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def test_everything_green_exits_zero(self):
        code, text, _runner = self.run_main()
        self.assertEqual(code, check_cmd.EXIT_OK, text)
        self.assertIn("HEALTHY", text)

    def test_the_suite_is_not_run_unless_asked_and_says_so(self):
        code, text, runner = self.run_main()
        self.assertEqual(code, check_cmd.EXIT_OK, text)
        self.assertNotIn("run_tests.py", " ".join(
            " ".join(argv) for argv, _r, _t in runner.calls))
        self.assertIn("NOT CHECKED", text)
        self.assertIn("--tests", text)

    def test_tests_opt_in_actually_runs_the_suite(self):
        _code, text, runner = self.run_main(["--tests"],
                                            results={"run_tests.py": (0, "OK\n")})
        joined = " ".join(" ".join(argv) for argv, _r, _t in runner.calls)
        self.assertIn("run_tests.py", joined)
        # Assert about THIS row, not the whole footer: `sandbox containment` is also
        # NOT CHECKED by default (it costs three model calls), so a global assertion
        # would now be testing an unrelated row's default.
        not_checked = text.split("NOT CHECKED", 1)[1] if "NOT CHECKED" in text else ""
        self.assertNotIn("run_tests.py", not_checked)

    def test_a_failing_verification_exits_unhealthy_and_names_it(self):
        code, text, _r = self.run_main(results={"airlock.py": (1, "boom\n")})
        self.assertEqual(code, check_cmd.EXIT_UNHEALTHY, text)
        self.assertIn("airlock.py", text)

    def test_an_unmet_gate_alone_exits_three(self):
        code, text, _r = self.run_main(
            results={"--gate --days 1": (1, "gate FAILED: 3 of 6 criteria unmet\n")})
        self.assertEqual(code, check_cmd.EXIT_GATE_UNMET, text)

    def test_a_hung_tool_is_a_timeout_not_a_hang(self):
        code, text, _r = self.run_main(results={"sync.py": (None, "")})
        self.assertEqual(code, check_cmd.EXIT_UNHEALTHY, text)
        self.assertIn("timed out", text)

    def test_never_a_live_sweep_every_sync_invocation_is_dry_run(self):
        _code, _text, runner = self.run_main()
        sync_calls = [argv for argv, _r, _t in runner.calls
                      if any("sync.py" in a for a in argv)]
        self.assertTrue(sync_calls)
        for argv in sync_calls:
            self.assertIn("--dry-run", argv)

    def test_never_a_live_shadow_run_either(self):
        _code, _text, runner = self.run_main()
        shadow_calls = [argv for argv, _r, _t in runner.calls
                        if any("shadow.py" in a for a in argv)]
        self.assertTrue(shadow_calls)
        for argv in shadow_calls:
            self.assertTrue("--dry-run" in argv or "--gate" in argv, argv)

    def test_root_mutating_tools_run_against_the_snapshot_not_the_board(self):
        _code, _text, runner = self.run_main()
        for argv, root, _t in runner.calls:
            joined = " ".join(argv)
            if "board.py" in joined or "sync.py" in joined:
                self.assertIsNotNone(root, joined)
                self.assertNotEqual(os.path.realpath(root),
                                    os.path.realpath(self.root), joined)

    def test_read_only_tools_run_against_the_real_board(self):
        _code, _text, runner = self.run_main()
        for argv, root, _t in runner.calls:
            joined = " ".join(argv)
            if "airlock.py" in joined or "shadow.py" in joined:
                self.assertEqual(os.path.realpath(root or self.root),
                                 os.path.realpath(self.root), joined)

    def test_an_unregistered_machine_reports_rather_than_exiting_two(self):
        with open(os.path.join(self.root, "machines.md"), "w", encoding="utf-8") as h:
            h.write("| Key | Role |\n|-----|------|\n| someone-else | laptop |\n")
        code, text, _r = self.run_main()
        self.assertIn(code, (check_cmd.EXIT_OK, check_cmd.EXIT_GATE_UNMET), text)
        self.assertIn("unknown", text.lower())

    def test_an_absent_shadow_agent_is_reported_and_does_not_fail_the_run(self):
        code, text, _r = self.run_main(agents={})
        self.assertIn("not loaded", text)
        self.assertNotEqual(code, check_cmd.EXIT_UNHEALTHY, text)

    def test_a_transient_fetch_failure_does_not_make_the_whole_run_unhealthy(self):
        """The false alarm this exists to kill: 14 flagged, then 3, then 0, all healthy."""
        code, text, _r = self.run_main(
            results={"sync.py": (0, sweep_output(fetch_row("Placard"),
                                                fetch_row("Fetcher"),
                                                "🚩 2 repo(s) need you"))})
        self.assertEqual(code, check_cmd.EXIT_OK, text)
        self.assertIn("UNPROVEN", text)
        self.assertIn("not a pass", text.lower())
        self.assertEqual(sorted(self.probe.probed), ["Fetcher", "Placard"])

    def test_a_fetch_failure_that_survives_the_retry_exits_unhealthy(self):
        code, text, _r = self.run_main(
            results={"sync.py": (0, sweep_output(clean_row("Dialexis"),
                                                fetch_row("Placard")))},
            failures=["Placard"])
        self.assertEqual(code, check_cmd.EXIT_UNHEALTHY, text)
        self.assertIn("Placard", text)

    def test_a_throttled_retry_is_unproven_rather_than_unhealthy(self):
        """Everything failing including the control: honest ignorance, not a false alarm."""
        code, text, _r = self.run_main(
            results={"sync.py": (0, sweep_output(clean_row("Dialexis"),
                                                fetch_row("Placard")))},
            failures=["Placard", "Dialexis"])
        self.assertEqual(code, check_cmd.EXIT_OK, text)
        self.assertIn("UNPROVEN", text)
        self.assertIn("Placard", text)

    def test_a_fresh_live_result_is_read_instead_of_racing_the_agent(self):
        report = self.write_live_report([("·", "Dialexis", "clean", "up to date")])
        code, text, runner = self.run_main(report_file=report)
        self.assertEqual(code, check_cmd.EXIT_OK, text)
        self.assertEqual([argv for argv, _r, _t in runner.calls
                          if any("sync.py" in a for a in argv)], [])
        self.assertIn("live sweep", text)

    def test_a_mid_run_agent_is_reported_as_weak_evidence(self):
        code, text, _r = self.run_main(
            agents={"com.lodestar.sync":
                    '{\n\t"LastExitStatus" = 0;\n\t"PID" = 4321;\n};\n'},
            results={"sync.py": (0, sweep_output(fetch_row("Placard")))})
        self.assertEqual(code, check_cmd.EXIT_OK, text)
        self.assertIn("weak evidence", text)

    def test_a_pending_capture_and_a_skill_fault_are_named_without_failing_the_run(self):
        """The exit-code contract: a WARN must not turn a clean machine into exit 1.

        Both rows are "needs you" conditions that persist for days by design — a `#` memory
        waits for the next session that runs `adopt`, and a missing skill repo waits for a
        clone. If either made `check.py` exit 1 the exit code would stop meaning "something
        is broken", and per D13's corollary the whole command would stop being run.
        """
        code, text, _r = self.run_main(
            captures=lambda: ["/b/claude/adopt/mt-1/20260901T101500-CLAUDE.md"],
            skills=lambda: ([], ["dangling: measuring-large-artifacts -> not cloned here"]))
        self.assertEqual(code, check_cmd.EXIT_OK, text)
        self.assertIn("claudeconf.py adopt", text)
        self.assertIn("measuring-large-artifacts", text)
        self.assertIn("WARN", text)

    def test_strict_is_the_only_thing_that_escalates_them(self):
        code, text, _r = self.run_main(
            ["--strict"],
            captures=lambda: ["/b/claude/adopt/mt-1/20260901T101500-CLAUDE.md"])
        self.assertEqual(code, check_cmd.EXIT_UNHEALTHY, text)

    def test_a_claudeconf_that_raises_leaves_the_run_alive_and_exiting_zero(self):
        """Two modules under concurrent edit; one raising must not take the check down."""
        def boom():
            raise RuntimeError("kaboom")
        code, text, _r = self.run_main(captures=boom, skills=boom)
        self.assertEqual(code, check_cmd.EXIT_OK, text)
        self.assertIn("UNPROVEN", text)
        self.assertIn("kaboom", text)

    def test_both_claude_rows_are_always_present(self):
        _code, text, _r = self.run_main()
        self.assertIn(check_cmd.CAPTURES_ROW, text)
        self.assertIn(check_cmd.SKILLS_ROW, text)

    def test_the_machine_liveness_row_is_always_present(self):
        _code, text, _r = self.run_main()
        self.assertIn(check_cmd.MACHINES_ROW, text)

    def test_a_quiet_machine_is_named_without_failing_the_run(self):
        """The exit-code contract again: a laptop shut for a week is not a broken system.

        If this made `check.py` exit 1, the exit code would stop meaning "something is broken"
        the first time Lukas took the Laptop on a trip — and per D13's corollary the command
        would stop being run at all.
        """
        code, text, _r = self.run_main(
            machines=lambda: {"installed": True,
                              "machines": [hb("laptop-box", "laptop", 9 * 86400)]})
        self.assertEqual(code, check_cmd.EXIT_OK, text)
        self.assertIn("QUIET", text)
        self.assertIn("WARN", text)

    def test_a_machine_that_has_never_spoken_is_named_without_failing_the_run(self):
        code, text, _r = self.run_main(
            machines=lambda: {"installed": True,
                              "machines": [hb("laptop-box", "laptop", None, as_of=None)]})
        self.assertEqual(code, check_cmd.EXIT_OK, text)
        self.assertIn("NEVER", text)

    def test_strict_escalates_a_quiet_machine_and_nothing_else_does(self):
        code, text, _r = self.run_main(
            ["--strict"],
            machines=lambda: {"installed": True,
                              "machines": [hb("laptop-box", "laptop", 9 * 86400)]})
        self.assertEqual(code, check_cmd.EXIT_UNHEALTHY, text)

    def test_unreadable_heartbeats_leave_the_run_alive_and_exiting_zero(self):
        def boom():
            raise RuntimeError("journal is a file, not a directory")
        code, text, _r = self.run_main(machines=boom)
        self.assertEqual(code, check_cmd.EXIT_OK, text)
        self.assertIn("UNPROVEN", text)
        self.assertIn("journal is a file", text)

    def test_the_board_snapshot_is_cleaned_up(self):
        _code, text, runner = self.run_main()
        snapshots = {root for argv, root, _t in runner.calls
                     if root and "board.py" in " ".join(argv)}
        self.assertTrue(snapshots)
        for path in snapshots:
            self.assertFalse(os.path.exists(path), "%s survived the run" % path)


class NonBlockingTest(unittest.TestCase):
    """D13: check.py reports and exits. It never blocks and never prompts."""

    def test_no_input_call_anywhere_in_the_module(self):
        """Parsed, not grepped — the docstring names `input()` while promising not to call it."""
        source = os.path.abspath(check_cmd.__file__)
        with open(source, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=source)
        called = {node.func.id for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        self.assertNotIn("input", called)
        self.assertNotIn("raw_input", called)

    def test_every_subprocess_gets_a_closed_stdin_and_a_timeout(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen.update(kwargs)
            seen["argv"] = argv

            class Result:
                returncode = 0
                stdout = b"fine\n"
            return Result()

        real = subprocess.run
        subprocess.run = fake_run
        try:
            check_cmd.run_python(["bin/airlock.py"], root=None, timeout=7)
        finally:
            subprocess.run = real
        self.assertEqual(seen.get("stdin"), subprocess.DEVNULL)
        self.assertEqual(seen.get("timeout"), 7)

    def test_the_retry_fetch_cannot_stop_to_ask_for_a_passphrase(self):
        """The retry runs `git fetch` directly, so it needs D13's guards of its own.

        `stdin=DEVNULL` is not enough on its own here: ssh reads a passphrase from the
        terminal rather than stdin, so `GIT_TERMINAL_PROMPT` and ssh's `BatchMode` are the
        parts that actually stop a fetch from hanging a health check.
        """
        seen = {}

        def fake_run(argv, **kwargs):
            seen.update(kwargs)
            seen["argv"] = argv

            class Result:
                returncode = 0
                stdout = b""
            return Result()

        real = subprocess.run
        subprocess.run = fake_run
        try:
            ok, _note = check_cmd.git_fetch("/tmp/nowhere")
        finally:
            subprocess.run = real
        self.assertTrue(ok)
        self.assertEqual(seen.get("stdin"), subprocess.DEVNULL)
        self.assertTrue(seen.get("timeout"))
        self.assertEqual(seen["env"].get("GIT_TERMINAL_PROMPT"), "0")
        self.assertIn("BatchMode=yes", seen["env"].get("GIT_SSH_COMMAND", ""))
        self.assertIn("fetch", seen["argv"])

    def test_the_retry_probe_never_mutates_the_board(self):
        """`git fetch` only. No pull, no commit, no push, no --force, anywhere."""
        source = os.path.abspath(check_cmd.__file__)
        with open(source, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=source)
        literals = {node.value for node in ast.walk(tree)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        for forbidden in ("commit", "push", "--force", "pull"):
            self.assertNotIn(forbidden, literals)


if __name__ == "__main__":
    unittest.main()
