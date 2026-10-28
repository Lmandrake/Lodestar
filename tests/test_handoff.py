"""bin/handoff.py — the content contract and identity resolution.

The checks here are audit-driven: each one pins a failure mode measured in the
source corpus (RimWorld, 67 handoffs, 2026-09-17) — off-template files passing
--check trivially, prose pointers with no next action dying unread, traps
re-explained instead of cited, dirty-tree dumps shipped unattributed. The
dangerous direction is SILENT acceptance, so most tests assert refusal.
"""

import datetime
import os
import shutil
import subprocess
import tempfile
import unittest

import handoff


def doc(half_done="- `AN_ITEM` — parked at step 2; NEXT: run the probe",
        traps="- a tool lied about a count (filed: LESSONS)",
        extra=""):
    """A template-conforming handoff with substitutable judgment bodies."""
    bodies = {
        "The one thing to carry forward": "nothing this wave",
        "What the owner should see": "nothing",
        "What is half-done, and where it stops": half_done,
        "Traps learned": traps,
    }
    parts = ["# X_HANDOFF_000000000000 — READ FIRST on wake", ""]
    for t, _p in handoff.JUDGEMENT_SECTIONS:
        parts += ["## %s" % t, "", bodies.get(t, "x"), ""]
    parts += ["## Commits", "", "```", "(none)", "```", "",
              "## Tree state at wrap", "", "- ok", extra, ""]
    return "\n".join(parts)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._root, handoff.ROOT = handoff.ROOT, self.tmp
        handoff._ADAPTER[0] = "unloaded"
        self._env = {}
        for v in ("AGENT_SEAT", "HANDOFF_SEAT"):
            self._env[v] = os.environ.pop(v, None)

    def tearDown(self):
        handoff.ROOT = self._root
        handoff._ADAPTER[0] = "unloaded"
        for v, old in self._env.items():
            if old is not None:
                os.environ[v] = old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, text):
        p = os.path.join(self.tmp, "h.md")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        return p


class TestContentContract(Base):
    def test_conforming_handoff_scans_clean(self):
        self.assertEqual(handoff.todo_scan(self.write(doc())), [])

    def test_unfilled_markers_are_counted(self):
        p = self.write(doc(half_done=handoff.TODO, traps=handoff.TODO))
        probs = handoff.todo_scan(p)
        self.assertTrue(any("2 unfilled" in x for x in probs), probs)

    def test_missing_file_is_a_problem_not_a_pass(self):
        probs = handoff.todo_scan(os.path.join(self.tmp, "absent.md"))
        self.assertEqual(len(probs), 1)

    def test_off_template_file_is_refused(self):
        # A hand-written file with its own headers used to pass trivially.
        probs = handoff.todo_scan(self.write("# x\n\nall done, honest\n"))
        self.assertTrue(any("heading" in x for x in probs), probs)

    def test_pointer_without_next_is_refused(self):
        p = self.write(doc(half_done="- `AN_ITEM` — someone should look"))
        probs = handoff.todo_scan(p)
        self.assertTrue(any("NEXT:" in x for x in probs), probs)

    def test_next_on_a_wrapped_continuation_line_still_counts(self):
        p = self.write(doc(half_done="- `AN_ITEM` — parked;\n"
                                     "  NEXT: run the probe"))
        self.assertEqual(handoff.todo_scan(p), [])

    def test_unfiled_trap_is_refused(self):
        p = self.write(doc(traps="- a long re-explanation of a known trap"))
        probs = handoff.todo_scan(p)
        self.assertTrue(any("filed" in x for x in probs), probs)

    def test_see_citation_also_satisfies_the_trap_rule(self):
        p = self.write(doc(traps="- the probe lies at scale (see: docs/X.md)"))
        self.assertEqual(handoff.todo_scan(p), [])

    def test_unattributed_dirty_line_is_refused(self):
        p = self.write(doc(extra="\n```\n M some/file   %s\n```"
                                 % handoff.WHOSE))
        probs = handoff.todo_scan(p)
        self.assertTrue(any("WHOSE" in x for x in probs), probs)


class TestBullets(Base):
    def test_continuation_lines_fold_into_their_bullet(self):
        got = handoff._bullets("- one\n  more\n- two\n\nprose\n")
        self.assertEqual(got, ["- one more", "- two"])

    def test_no_bullets_means_no_complaints(self):
        self.assertEqual(handoff._bullets("nothing this wave\n"), [])


class TestIdentity(Base):
    def cfg(self, **over):
        c = dict(handoff.DEFAULTS)
        c.update(over)
        return c

    def test_env_var_wins(self):
        os.environ["AGENT_SEAT"] = "bench"
        self.assertEqual(handoff.seat(self.cfg()), "BENCH")

    def test_empty_corpus_infers_solo(self):
        self.assertEqual(handoff.seat(self.cfg()), "SOLO")

    def test_default_seat_config_wins_over_inference(self):
        self.assertEqual(handoff.seat(self.cfg(default_seat="pilot")), "PILOT")

    def test_multi_seat_corpus_refuses_rather_than_guessing(self):
        # Guessing wrong files one agent's reboot under another's name and
        # hands it their unfinished work (measured live: one seat read as another).
        d = os.path.join(self.tmp, "handoffs")
        os.makedirs(d)
        open(os.path.join(d, "BENCH_HANDOFF_202601010000.md"), "w").close()
        with self.assertRaises(SystemExit):
            handoff.seat(self.cfg())


class TestConfig(Base):
    def test_no_config_file_yields_defaults(self):
        self.assertEqual(handoff.config(), handoff.DEFAULTS)

    def test_unknown_key_is_refused_not_ignored(self):
        with open(os.path.join(self.tmp, ".handoff.json"), "w") as fh:
            fh.write('{"handof_dir": "typo"}')
        with self.assertRaises(SystemExit):
            handoff.config()

    def test_known_key_overrides_default(self):
        with open(os.path.join(self.tmp, ".handoff.json"), "w") as fh:
            fh.write('{"cull_days": 60}')
        self.assertEqual(handoff.config()["cull_days"], 60)


class TestDirtyCap(Base):
    def test_under_cap_all_lines_carry_marker(self):
        got = handoff.dirty_block([" M a/x", " M b"], 5)
        self.assertEqual(len(got), 2)
        self.assertTrue(all(handoff.WHOSE in l for l in got))

    def test_over_cap_truncates_and_summary_has_no_marker(self):
        dirty = [" M Transient/f%d" % i for i in range(30)] + \
                ["?? src/a", " M src/b", " M rootfile"]
        got = handoff.dirty_block(dirty, 20)
        self.assertEqual(len(got), 21)
        self.assertTrue(all(handoff.WHOSE in l for l in got[:20]))
        self.assertNotIn(handoff.WHOSE, got[20])
        self.assertIn("13 more uncommitted paths", got[20])
        self.assertIn("Transient 10", got[20])
        self.assertIn("src 2", got[20])
        self.assertIn("(root) 1", got[20])

    def test_default_cap_is_20(self):
        self.assertEqual(handoff.DEFAULTS["dirty_cap"], 20)


class TestFamilyAndAge(Base):
    def test_seat_family_strips_reboot(self):
        self.assertEqual(handoff.seat_family("BENCH_REBOOT"), "BENCH")
        self.assertEqual(handoff.seat_family("BENCH"), "BENCH")

    def test_filename_ts_parses_or_none(self):
        self.assertEqual(handoff.filename_ts("BENCH_HANDOFF_202609011230.md"),
                         "2026-09-01T12:30:00Z")
        self.assertIsNone(handoff.filename_ts("BENCH_HANDOFF_x.md"))
        self.assertIsNone(handoff.filename_ts("BENCH_HANDOFF_202613991230.md"))


def _git(cwd, *a, env=None):
    e = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
             GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    e.update(env or {})
    return subprocess.run(("git",) + a, cwd=cwd, env=e, check=True,
                          capture_output=True, text=True).stdout


class TestGitModes(Base):
    def setUp(self):
        super().setUp()
        _git(self.tmp, "init", "-q", "-b", "main")
        os.environ["AGENT_SEAT"] = "BENCH"
        self.d = os.path.join(self.tmp, "handoffs")
        os.makedirs(self.d)

    def tearDown(self):
        os.environ.pop("AGENT_SEAT", None)
        super().tearDown()

    def commit(self, name, text="x\n"):
        with open(os.path.join(self.tmp, name), "w") as fh:
            fh.write(text)
        _git(self.tmp, "add", "--", name)
        _git(self.tmp, "commit", "-q", "-m", name, "--", name)

    def test_owed_no_handoff_with_commits(self):
        self.commit("a.txt")
        code, msg = handoff.owed_status(handoff.config())
        self.assertEqual(code, 1)
        self.assertIn("no handoff on record for BENCH", msg)

    def test_owed_lifecycle(self):
        self.commit("a.txt")
        self.commit("handoffs/BENCH_HANDOFF_202601010000.md")
        code, msg = handoff.owed_status(handoff.config())
        self.assertEqual(code, 0, msg)
        self.assertIn("no handoff owed", msg)
        self.commit("b.txt")
        self.commit("c.txt")
        code, msg = handoff.owed_status(handoff.config())
        self.assertEqual(code, 1)
        self.assertIn("HANDOFF OWED: 2 commit(s)", msg)

    def test_cull_family_protection_and_filename_age(self):
        old = "2026-01-01T00:00:00+00:00"
        env = {"GIT_AUTHOR_DATE": old, "GIT_COMMITTER_DATE": old}
        for n in ("BENCH_HANDOFF_202601010000.md",
                  "BENCH_REBOOT_HANDOFF_202601020000.md"):
            with open(os.path.join(self.d, n), "w") as fh:
                fh.write("x\n")
        _git(self.tmp, "add", "--", "handoffs")
        _git(self.tmp, "commit", "-q", "-m", "h", env=env)
        # lessons drain after both (inbox shrinks), then a fresh-looking
        # move-style commit touching the older file's mtime clock is not
        # needed: filename age alone must make it a candidate.
        self.commit("LESSONS.md", "a\nb\n")
        self.commit("LESSONS.md", "a\n")
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            handoff.cull(handoff.config(), False)
        out = buf.getvalue()
        # BENCH_REBOOT 0102 is the family's newest -> protected;
        # BENCH 0101 is superseded in the same family -> candidate.
        firsts = [l.split()[0] for l in out.splitlines() if l.split()]
        self.assertIn("BENCH_HANDOFF_202601010000.md", firsts)
        self.assertFalse([f for f in firsts if f.startswith("BENCH_REBOOT_HANDOFF")], out)


class TestPrivatePrivateOverlay(Base):
    """`.handoff.json` private_store: handoffs live in the private overlay, never in this repo."""

    commit = TestGitModes.commit

    def setUp(self):
        super().setUp()
        _git(self.tmp, "init", "-q", "-b", "main")
        os.environ["AGENT_SEAT"] = "BENCH"
        self.store = os.path.join(self.tmp, "private-store")
        os.makedirs(self.store)
        _git(self.store, "init", "-q", "-b", "main")
        _git(self.store, "remote", "add", "origin", "git@git.private.example:x/private-store.git")
        _git(self.tmp, "remote", "add", "origin", "git@github.com:x/repo.git")
        self._envs = {k: os.environ.get(k) for k in ("LODESTAR_PRIVATE_ROOT", "LODESTAR_MACHINE")}
        os.environ["LODESTAR_PRIVATE_ROOT"] = self.store
        os.environ["LODESTAR_MACHINE"] = "mt-002"
        handoff._PRIVATE_DIRS.clear()
        from lib import prosehome
        prosehome.reset_cache()
        with open(os.path.join(self.tmp, ".handoff.json"), "w") as fh:
            fh.write('{"private_store": true}')
        self.over = os.path.join(self.store, "projects", os.path.basename(self.tmp), "handoffs")

    def tearDown(self):
        for k, v in self._envs.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        handoff._PRIVATE_DIRS.clear()
        os.environ.pop("AGENT_SEAT", None)
        super().tearDown()

    def jcommit(self, name):
        path = os.path.join(self.over, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("x\n")
        _git(self.store, "add", "--", path)
        _git(self.store, "commit", "-q", "-m", name, "--", path)

    def test_the_dir_is_the_overlay(self):
        self.assertEqual(os.path.realpath(handoff.handoff_dir(handoff.config())),
                         os.path.realpath(self.over))

    def test_owed_lifecycle_measures_this_repos_commits_from_the_overlay_commit(self):
        import time
        self.commit("a.txt")
        time.sleep(1.1)  # commit times have one-second resolution
        self.jcommit("BENCH_HANDOFF_202601010000.md")
        code, msg = handoff.owed_status(handoff.config())
        self.assertEqual(code, 0, msg)
        time.sleep(1.1)
        self.commit("b.txt")
        code, msg = handoff.owed_status(handoff.config())
        self.assertEqual(code, 1, msg)
        self.assertIn("HANDOFF OWED: 1 commit(s)", msg)

    def test_the_recorded_root_commit_wins_over_the_overlay_commit_time(self):
        """GPT review 2026-10-13: built at R1, R2 committed, THEN the overlay commit. The
        window must start at R1, so R2 is owed."""
        self.commit("a.txt")
        r1 = _git(self.tmp, "rev-parse", "HEAD").strip()
        path = os.path.join(self.over, "BENCH_HANDOFF_202601010000.md")
        os.makedirs(self.over, exist_ok=True)
        with open(path, "w") as fh:
            fh.write("x\n\n<!-- %s%s -->\n" % (handoff.ROOT_MARK, r1))
        self.commit("b.txt")
        _git(self.store, "add", "--", path)
        _git(self.store, "commit", "-q", "-m", "h", "--", path)
        code, msg = handoff.owed_status(handoff.config())
        self.assertEqual(code, 1, msg)
        self.assertIn("HANDOFF OWED: 1 commit(s)", msg)

    def test_a_work_machine_without_the_store_refuses(self):
        os.environ["LODESTAR_PRIVATE_ROOT"] = os.path.join(self.tmp, "gone")
        handoff._PRIVATE_DIRS.clear()
        with self.assertRaises(SystemExit):
            handoff.handoff_dir(handoff.config())


class TestStrandedSessions(Base):
    """--wake warns when a session started after the newest handoff and left none.
    The root is reached through a SYMLINK (tower's ~/dev is one; the Macs' is
    not): Claude Code mangles the logical path, git reports the real one."""

    def setUp(self):
        super().setUp()
        import json
        self.json = json
        self.real = os.path.join(self.tmp, "real", "Proj")
        os.makedirs(os.path.join(self.real, "handoffs"))
        _git(self.real, "init", "-q", "-b", "main")
        self.link = os.path.join(self.tmp, "dev")
        os.symlink(os.path.join(self.tmp, "real"), self.link)
        self.logical = os.path.join(self.link, "Proj")
        handoff.ROOT = os.path.realpath(self.real)
        self.records = os.path.join(self.tmp, "overlay", "conversations")
        os.makedirs(self.records)
        from lib import conversations
        self.conv = conversations
        self._rd = conversations.record_dir
        conversations.record_dir = lambda root: self.records
        self._saved = {k: os.environ.get(k) for k in ("CLAUDE_HOME", "PWD", "CLAUDE_CODE_SESSION_ID")}
        os.environ["CLAUDE_HOME"] = os.path.join(self.tmp, "claude-home")
        os.environ["PWD"] = self.logical
        os.environ["AGENT_SEAT"] = "SOLO"
        self.tdir = conversations.transcript_dir(self.logical)
        os.makedirs(self.tdir)
        with open(os.path.join(self.real, "handoffs",
                               "SOLO_HANDOFF_202610170900.md"), "w") as fh:
            fh.write(doc())

    def tearDown(self):
        self.conv.record_dir = self._rd
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        os.environ.pop("AGENT_SEAT", None)
        super().tearDown()

    def transcript(self, sid, ts, cwd=None):
        p = os.path.join(self.tdir, sid + ".jsonl")
        with open(p, "w") as fh:
            fh.write(self.json.dumps({"type": "user", "sessionId": sid,
                                      "timestamp": ts, "cwd": cwd or self.logical}) + "\n")
        return p

    def record(self, name):
        p = os.path.join(self.records, name)
        open(p, "w").close()
        return p

    def test_the_symlinked_root_still_finds_the_transcript_dir(self):
        self.assertNotEqual(self.tdir, self.conv.transcript_dir(handoff.ROOT))
        self.assertEqual(handoff._transcript_dirs(self.conv), [self.tdir])

    def test_a_later_transcript_is_flagged_by_absolute_path(self):
        p = self.transcript("aaaaaaaa-1", "2026-10-17T10:00:00.000Z")
        rows = handoff.stranded_sessions(handoff.config())
        self.assertEqual(rows, [("202610171000", p)])
        self.assertTrue(os.path.isabs(rows[0][1]))

    def test_the_current_session_is_not_flagged(self):
        self.transcript("bbbbbbbb-1", "2026-10-17T10:00:00Z")
        self.assertEqual(handoff.stranded_sessions(handoff.config(), "bbbbbbbb-1"), [])

    def test_a_session_started_before_the_handoff_is_not_flagged(self):
        # The author of the handoff keeps writing after it; it started before it.
        self.transcript("cccccccc-1", "2026-10-17T08:00:00Z")
        self.assertEqual(handoff.stranded_sessions(handoff.config()), [])

    def test_a_record_is_preferred_over_its_transcript(self):
        self.transcript("dddddddd-1", "2026-10-17T10:00:00Z")
        r = self.record("2026-10-17-1000-dddddddd.md")
        self.assertEqual(handoff.stranded_sessions(handoff.config()),
                         [("202610171000", r)])

    def test_a_record_from_another_machine_with_no_transcript_is_flagged(self):
        r = self.record("2026-10-17-1130-eeeeeeee.md")
        self.record("2026-10-17-0800-ffffffff.md")      # before the handoff
        self.record("notes.md")                         # not a record name
        self.assertEqual(handoff.stranded_sessions(handoff.config()),
                         [("202610171130", r)])

    def test_another_repos_transcript_in_a_colliding_dir_is_not_flagged(self):
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        _git(other, "init", "-q", "-b", "main")
        self.transcript("gggggggg-1", "2026-10-17T10:00:00Z", cwd=other)
        self.assertEqual(handoff.stranded_sessions(handoff.config()), [])

    def test_wake_prints_the_block_before_the_handoff(self):
        import contextlib
        import io as _io
        _git(self.real, "add", "--", "handoffs")
        _git(self.real, "commit", "-q", "-m", "h")
        p = self.transcript("hhhhhhhh-1", "2026-10-17T10:00:00Z")
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            handoff.wake(handoff.config())
        out = buf.getvalue()
        self.assertIn("ended without a handoff", out)
        self.assertIn(p, out)
        self.assertLess(out.index(p), out.index("READ FIRST on wake"))

    def test_no_later_session_means_no_block(self):
        self.assertEqual(handoff.stranded_block(handoff.config()), [])


if __name__ == "__main__":
    unittest.main()


def _stamp(days_ago):
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days_ago)
    return t.strftime("%Y%m%d%H%M")


class TestCullRule(TestGitModes):
    """Owner card 2026-10-19: a handoff is cull-eligible once (1) a newer one of the same
    seat family exists, (2) cull_days (default 7) have passed since THAT newer one, and
    (3) its lessons are filed. No human drain is required."""

    test_cull_family_protection_and_filename_age = None  # runs once, in TestGitModes

    def handoff(self, name, carry="nothing this wave", traps="- none"):
        text = doc(traps=traps).replace("## The one thing to carry forward\n\nnothing this wave",
                                        "## The one thing to carry forward\n\n" + carry)
        self.commit(os.path.join("handoffs", name), text)

    def run_cull(self, apply_=False):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = handoff.cull(handoff.config(), apply_)
        # Candidate lines start with the filename; summary lines do not.
        names = {l.split()[0] for l in buf.getvalue().splitlines()
                 if l.split() and "_HANDOFF_" in l.split()[0]}
        return code, names if not apply_ else buf.getvalue()

    def test_default_grace_is_seven_days(self):
        self.assertEqual(handoff.DEFAULTS["cull_days"], 7)

    def test_grace_runs_from_supersession_not_file_age(self):
        old = "BENCH_HANDOFF_202601010000.md"
        self.handoff(old)
        self.handoff("BENCH_HANDOFF_%s.md" % _stamp(3))
        _, out = self.run_cull()
        self.assertNotIn(old, out)  # file is ancient, but superseded only 3 days ago

    def test_superseded_past_grace_with_no_lessons_is_eligible(self):
        old = "BENCH_HANDOFF_202601010000.md"
        new = "BENCH_HANDOFF_%s.md" % _stamp(8)
        self.handoff(old)
        self.handoff(new)
        _, out = self.run_cull()
        self.assertIn(old, out)
        self.assertNotIn(new, out)  # a seat's newest is never eligible

    def test_repo_config_overrides_grace(self):
        with open(os.path.join(self.tmp, ".handoff.json"), "w") as fh:
            fh.write('{"cull_days": 30}')
        old = "BENCH_HANDOFF_202601010000.md"
        self.handoff(old)
        self.handoff("BENCH_HANDOFF_%s.md" % _stamp(8))
        _, out = self.run_cull()
        self.assertNotIn(old, out)

    def test_unfiled_lesson_blocks_until_it_reaches_the_inbox(self):
        old = "BENCH_HANDOFF_202601010000.md"
        lesson = "**A green suite says nothing** about whether the picture is any good."
        self.handoff(old, carry=lesson)
        self.handoff("BENCH_HANDOFF_%s.md" % _stamp(8))
        _, out = self.run_cull()
        self.assertNotIn(old, out)
        # Filed (reworded markup, rewrapped) and later drained: still counts as filed.
        self.commit("LESSONS.md", "- A green suite says nothing about whether\n"
                                  "  the picture is any good. (2026-10-19)\n")
        self.commit("LESSONS.md", "")
        _, out = self.run_cull()
        self.assertIn(old, out)

    def test_cited_traps_count_as_filed(self):
        old = "BENCH_HANDOFF_202601010000.md"
        self.handoff(old, traps="- a tool lied about a count, badly and at length (filed: LESSONS)")
        self.handoff("BENCH_HANDOFF_%s.md" % _stamp(8))
        _, out = self.run_cull()
        self.assertIn(old, out)

    def test_uncited_trap_blocks(self):
        old = "BENCH_HANDOFF_202601010000.md"
        self.handoff(old, traps="- a tool lied about a count, badly and at length")
        self.handoff("BENCH_HANDOFF_%s.md" % _stamp(8))
        _, out = self.run_cull()
        self.assertNotIn(old, out)

    def test_a_committed_then_drained_harvest_files_what_it_names(self):
        old = "BENCH_HANDOFF_202601010000.md"
        self.handoff(old, carry="an unfiled insight that nobody copied anywhere at all")
        self.handoff("BENCH_HANDOFF_%s.md" % _stamp(8))
        harvest = os.path.join("handoffs", "harvest_20260102.md")
        self.commit(harvest, "# Handoff harvest\n\n## %s (2026-01-01T00:00:00Z)\n\nx\n" % old[:-3])
        _, out = self.run_cull()
        self.assertNotIn(old, out)  # committed but not yet drained
        _git(self.tmp, "rm", "-q", "--", harvest)
        _git(self.tmp, "commit", "-q", "-m", "drain harvest")
        _, out = self.run_cull()
        self.assertIn(old, out)

    def test_follower_repo_is_never_culled(self):
        old = "BENCH_HANDOFF_202601010000.md"
        self.handoff(old)
        self.handoff("BENCH_HANDOFF_%s.md" % _stamp(8))
        reg = os.path.join(self.tmp, "registry.md")
        with open(reg, "w") as fh:
            fh.write("| Name | Path | Branch | Send | Receive |\n|---|---|---|---|---|\n"
                     "| T | %s | main | off | on |\n" % self.tmp)
        saved, handoff.REGISTRY_PATH = handoff.REGISTRY_PATH, reg
        try:
            code, out = self.run_cull(apply_=True)
        finally:
            handoff.REGISTRY_PATH = saved
        self.assertEqual(code, 1)
        self.assertIn("FOLLOWER", out)
        self.assertTrue(os.path.exists(os.path.join(self.d, old)))

    def test_dry_run_deletes_nothing(self):
        old = "BENCH_HANDOFF_202601010000.md"
        self.handoff(old)
        self.handoff("BENCH_HANDOFF_%s.md" % _stamp(8))
        self.run_cull()
        self.assertTrue(os.path.exists(os.path.join(self.d, old)))


class TestAutoFile(TestGitModes):
    """Upgrade 2026-10-19: lessons piled up unfiled (17 Lodestar handoffs blocked from
    --cull) because --check demanded citations of Traps only, while --cull demanded
    every carry-forward line too. --check now files them itself when the handoff and
    the inbox share a home, and refuses (copies nothing) when the handoff is on the work
    host and the inbox is not."""

    PARA = ("**A green suite says nothing** about whether the picture\n"
            "is any good; budget a real look before calling it done.")

    def handoff_file(self, carry="nothing this wave", traps="- none", where=None):
        text = doc(traps=traps).replace(
            "## The one thing to carry forward\n\nnothing this wave",
            "## The one thing to carry forward\n\n" + carry)
        p = os.path.join(where or self.d, "BENCH_HANDOFF_202601010000.md")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        return p

    def inbox(self):
        p = os.path.join(self.tmp, "LESSONS.md")
        return open(p, encoding="utf-8").read() if os.path.exists(p) else ""

    def quiet(self, fn, *a):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*a)

    def test_a_wrapped_paragraph_is_one_lesson_unit(self):
        units = handoff.lesson_units(doc().replace("nothing this wave", self.PARA),
                                     handoff.LESSON_SECTIONS[:1])
        self.assertEqual(len(units), 1, units)

    def test_one_citation_at_a_paragraphs_end_files_the_whole_paragraph(self):
        text = doc().replace("nothing this wave", self.PARA + " (filed: LESSONS)")
        self.assertEqual(handoff.lessons_filed(text, "x.md", "", set())[0], True)

    def test_unfiled_lessons_are_appended_to_the_inbox_and_cited(self):
        p = self.handoff_file(carry=self.PARA,
                              traps="- a tool lied about a count, badly and at length")
        probs = self.quiet(handoff.file_lessons, handoff.config(), p, "2026-10-19")
        self.assertTrue(any("commit it" in x for x in probs), probs)
        inbox = self.inbox()
        self.assertIn("- 2026-10-19 (handoff BENCH_HANDOFF_202601010000): **A green suite",
                      inbox)
        self.assertIn("a tool lied about a count", inbox)
        text = open(p, encoding="utf-8").read()
        self.assertIn("calling it done. (filed: LESSONS)", text)
        self.assertIn("at length (filed: LESSONS)", text)
        self.assertEqual(handoff.todo_scan(p), [])  # the Traps citation now holds
        # and --cull counts it filed
        self.assertTrue(handoff.lessons_filed(
            text, os.path.basename(p), handoff.lessons_corpus(handoff.config()), set())[0])

    def test_a_second_check_files_nothing_twice(self):
        p = self.handoff_file(carry=self.PARA)
        self.quiet(handoff.file_lessons, handoff.config(), p, "2026-10-19")
        first = self.inbox()
        self.assertEqual(self.quiet(handoff.file_lessons, handoff.config(), p), [])
        self.assertEqual(self.inbox(), first)

    def test_a_lesson_already_in_the_inbox_is_not_copied_again(self):
        self.commit("LESSONS.md", "- A green suite says nothing about whether the picture\n"
                                  "  is any good; budget a real look before calling it done.\n")
        p = self.handoff_file(carry=self.PARA)
        before = open(p, encoding="utf-8").read()
        self.assertEqual(self.quiet(handoff.file_lessons, handoff.config(), p), [])
        self.assertEqual(open(p, encoding="utf-8").read(), before)
        self.assertEqual(self.inbox().count("green suite"), 1)

    def test_a_handoff_outside_the_repo_copies_nothing_and_refuses(self):
        # private_store: the handoff is on the private host, the inbox is in a github.com repo.
        away = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, away, True)
        p = self.handoff_file(carry=self.PARA, where=away)
        before = open(p, encoding="utf-8").read()
        probs = self.quiet(handoff.file_lessons, handoff.config(), p)
        self.assertEqual(len(probs), 1, probs)
        self.assertIn("private host", probs[0])
        self.assertEqual(self.inbox(), "")
        self.assertEqual(open(p, encoding="utf-8").read(), before)

    def test_nothing_this_wave_files_nothing(self):
        p = self.handoff_file()
        self.assertEqual(self.quiet(handoff.file_lessons, handoff.config(), p), [])
        self.assertEqual(self.inbox(), "")
