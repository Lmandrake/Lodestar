"""Tests for bin/statusline-segment.sh — the "L*" statusline token.

The property under all of these: **the segment must never cost a reader a wrong belief, and
must never cost the host script a hang.** It runs every 10 s inside a statusline that already
spends 0.6-0.7 s per run, it is read at a glance rather than studied, and its whole job is to
answer "am I enrolled, is the sweep alive, am I in sync".

Two defects here were found by LOOKING at the output, not by any assertion — which is why
they get tests now rather than only a comment:

1. The ASCII dirty glyph was "*", so a dirty tree rendered `L**` — the segment's own literal
   prefix plus the mark, reading as one ambiguous token.
2. "your sweep is dead" and "another machine went dark" were the SAME glyph in two colours,
   so they were indistinguishable under NO_COLOR or to a red-green colourblind reader, while
   meaning entirely different things.

Both are the `.wlab` / `.s-partial` family recorded in CLAUDE.md: present in the output,
carrying no information. A test cannot see a picture, but it CAN pin the two invariants that
made these bugs possible — glyph distinctness, and no glyph colliding with the prefix.

Everything runs against temp git repos and a temp heartbeat line via
`LODESTAR_STATUSLINE_LINE`. No test touches the real board, the real heartbeat file, or the
network.
"""

import os
import re
import subprocess
import tempfile
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "bin", "statusline-segment.sh")

# Two hours, mirroring lib.heartbeat.SILENT_AFTER_SECONDS. Deliberately NOT imported: this
# test asserts the shell script's own threshold, and importing the Python constant would make
# the test derive its input from the very fact it is guarding — the vacuous-bound defect
# recorded in CLAUDE.md, where a test computed `TIMEOUT + 30` and stayed green when the bound
# was removed. If these two numbers drift apart, this test should fail.
SILENT_AFTER = 7200

ANSI = re.compile(r"\033\[[0-9;]*m")


def strip(text):
    return ANSI.sub("", text)


class SegmentCase(unittest.TestCase):
    """Shared fixtures: a temp dir, a git repo factory, and a line-file factory."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-statusline-")
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_segment(self, directory, line=None, env=None):
        """Run the script and return its stdout. Asserts exit 0 and empty stderr.

        Exit 0 and a silent stderr are part of the contract, not incidental: the host script
        interpolates this into a printf and would render a diagnostic straight onto his
        status line.
        """
        environ = dict(os.environ)
        # Never let a test read the real heartbeat file. Point at a path inside the sandbox
        # by default, so an absent-file test is the default rather than a special case.
        environ["LODESTAR_STATUSLINE_LINE"] = line or os.path.join(self.tmp, "absent")
        environ.pop("NO_COLOR", None)
        environ.pop("LODESTAR_STATUSLINE_ASCII", None)
        if env:
            environ.update(env)
        proc = subprocess.run([SCRIPT, directory], capture_output=True, text=True,
                              env=environ, timeout=30)
        self.assertEqual(proc.returncode, 0,
                         "segment must always exit 0; stderr=%r" % proc.stderr)
        self.assertEqual(proc.stderr, "",
                         "segment must never write to stderr; got %r" % proc.stderr)
        return proc.stdout

    def write_line(self, name, epoch=None, fresh=3, silent=0, raw=None):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            if raw is not None:
                handle.write(raw)
            else:
                stamp = int(time.time()) if epoch is None else epoch
                handle.write("2 %d %d %d\n" % (stamp, fresh, silent))
        return path

    def repo(self, name="repo", enrolled=True, upstream=True):
        """A real git repo, optionally enrolled and optionally with a pushed upstream."""
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        self.git(path, "init", "-q")
        self.git(path, "config", "user.email", "t@example.invalid")
        self.git(path, "config", "user.name", "t")
        if enrolled:
            self.git(path, "config", "--local", "lodestar.id", name)
        with open(os.path.join(path, "seed"), "w", encoding="utf-8") as handle:
            handle.write("seed\n")
        self.git(path, "add", "seed")
        self.git(path, "commit", "-qm", "seed")
        if upstream:
            bare = os.path.join(self.tmp, name + ".git")
            self.git(self.tmp, "init", "-q", "--bare", bare)
            self.git(path, "remote", "add", "origin", bare)
            branch = self.git(path, "rev-parse", "--abbrev-ref", "HEAD").strip()
            self.git(path, "push", "-q", "-u", "origin", branch)
        return path

    def git(self, cwd, *args):
        proc = subprocess.run(["git", "-C", cwd] + list(args),
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0,
                         "git %s failed: %s" % (" ".join(args), proc.stderr))
        return proc.stdout

    def commit(self, path, name):
        with open(os.path.join(path, name), "w", encoding="utf-8") as handle:
            handle.write(name + "\n")
        self.git(path, "add", name)
        self.git(path, "commit", "-qm", name)


class EnrollmentTest(SegmentCase):
    def test_an_unenrolled_directory_says_so_rather_than_printing_nothing(self):
        """A session that drifted into untracked work must not look like a tracked one.

        This is the ritual's only visible enforcement — printing nothing would make an
        accidental throwaway indistinguishable from normal work, which §0 says must never
        happen by accident.
        """
        plain = self.repo("plain", enrolled=False)
        self.assertEqual(strip(self.run_segment(plain)), "L?")

    def test_a_directory_outside_any_repo_is_untracked_not_an_error(self):
        outside = os.path.join(self.tmp, "bare-dir")
        os.makedirs(outside)
        self.assertEqual(strip(self.run_segment(outside)), "L?")

    def test_an_enrolled_clean_in_sync_repo_is_the_quiet_case(self):
        """Healthy prints the bare label and NOTHING else. Every mark is a deviation."""
        clean = self.repo("clean")
        fresh = self.write_line("fresh")
        self.assertEqual(strip(self.run_segment(clean, line=fresh)), "L*")

    def test_a_linked_worktree_reads_as_enrolled(self):
        """`lodestar.id` lives in the COMMON git dir, which a linked worktree shares.

        CLAUDE.md records the inverse bug: a duplicate-identity check that did not resolve
        `--git-common-dir` first saw an ordinary worktree as a conflict and halted work on a
        healthy repo. Here the same omission would make a worktree read as untracked.
        """
        main = self.repo("wt-main")
        linked = os.path.join(self.tmp, "wt-linked")
        self.git(main, "worktree", "add", "-q", linked, "-b", "side")
        fresh = self.write_line("fresh")
        self.assertNotEqual(strip(self.run_segment(linked, line=fresh)), "L?")


class SyncSymbolTest(SegmentCase):
    def test_unpushed_commits_show_a_count(self):
        path = self.repo("ahead")
        self.commit(path, "one")
        self.commit(path, "two")
        self.assertIn("2", strip(self.run_segment(path, line=self.write_line("f"))))

    def test_a_dirty_tree_is_marked(self):
        path = self.repo("dirty")
        with open(os.path.join(path, "scratch"), "w", encoding="utf-8") as handle:
            handle.write("x\n")
        out = strip(self.run_segment(path, line=self.write_line("f")))
        self.assertNotEqual(out, "L*", "a dirty tree must not read as the quiet case")

    def test_diverged_is_distinct_from_merely_ahead(self):
        """Diverged is the one sync state that needs a human, so it must not read as 'ahead'.

        It is also the exact state the sweep reported 36 minutes after it had been resolved —
        the false alarm that moved these symbols onto live git in the first place.
        """
        path = self.repo("div")
        self.commit(path, "local")
        # Move the upstream out from under it, so both sides have moved.
        other = os.path.join(self.tmp, "other")
        self.git(self.tmp, "clone", "-q", os.path.join(self.tmp, "div.git"), other)
        self.git(other, "config", "user.email", "t@example.invalid")
        self.git(other, "config", "user.name", "t")
        self.commit(other, "remote-side")
        self.git(other, "push", "-q")
        self.git(path, "fetch", "-q")
        line = self.write_line("f")
        diverged = strip(self.run_segment(path, line=line))
        ahead = strip(self.run_segment(self.ahead_only(), line=line))
        self.assertNotEqual(diverged, ahead,
                            "diverged and ahead must not render identically")

    def ahead_only(self):
        path = self.repo("ahead-only")
        self.commit(path, "one")
        return path


class HeartbeatTest(SegmentCase):
    def test_an_absent_line_file_is_unknown_not_a_fault(self):
        """Absent means "no sweep has run here yet" — an ordinary state on a fresh machine."""
        clean = self.repo("hb-absent")
        self.assertEqual(strip(self.run_segment(clean)), "L*")

    def test_a_malformed_line_is_unknown_rather_than_epoch_zero(self):
        """The dangerous failure is treating garbage as 0, which is 1970 and reads as dead."""
        clean = self.repo("hb-bad")
        bad = self.write_line("bad", raw="garbage not a number at all\n")
        self.assertEqual(strip(self.run_segment(clean, line=bad)), "L*")

    def test_an_empty_line_file_is_unknown(self):
        clean = self.repo("hb-empty")
        empty = self.write_line("empty", raw="")
        self.assertEqual(strip(self.run_segment(clean, line=empty)), "L*")

    def test_a_stale_line_marks_the_sweep_dead(self):
        clean = self.repo("hb-dead")
        dead = self.write_line("dead", epoch=int(time.time()) - (SILENT_AFTER + 600))
        self.assertNotEqual(strip(self.run_segment(clean, line=dead)), "L*",
                            "a sweep silent past the threshold must be visible")

    def test_a_line_just_inside_the_threshold_is_still_healthy(self):
        """Grace is deliberate: one missed hour is a hiccup, not silence."""
        clean = self.repo("hb-grace")
        recent = self.write_line("recent", epoch=int(time.time()) - (SILENT_AFTER - 600))
        self.assertEqual(strip(self.run_segment(clean, line=recent)), "L*")

    def test_own_death_and_a_foreign_machine_going_dark_use_DIFFERENT_GLYPHS(self):
        """The invariant that the colour-only collision violated.

        "your sweep stopped" and "a different machine went dark" need different responses, so
        a distinction carried only by red-vs-yellow is no distinction at all under NO_COLOR.
        Compared with colour STRIPPED, which is what makes this fire if someone reunifies the
        glyphs and leaves the colours to carry the meaning.
        """
        clean = self.repo("hb-glyph")
        mine_dead = self.write_line("mine", epoch=int(time.time()) - (SILENT_AFTER + 600))
        theirs_dark = self.write_line("theirs", fresh=2, silent=1)
        self.assertNotEqual(
            strip(self.run_segment(clean, line=mine_dead)),
            strip(self.run_segment(clean, line=theirs_dark)),
            "own-sweep-dead and foreign-machine-silent must differ without colour")


class RenderingTest(SegmentCase):
    def test_no_color_suppresses_every_escape_sequence(self):
        path = self.repo("nc")
        out = self.run_segment(path, line=self.write_line("f"), env={"NO_COLOR": "1"})
        self.assertNotIn("\033", out, "NO_COLOR must remove all ANSI escapes")

    def test_a_dumb_terminal_suppresses_colour_too(self):
        path = self.repo("dumb")
        out = self.run_segment(path, line=self.write_line("f"), env={"TERM": "dumb"})
        self.assertNotIn("\033", out)

    def test_no_ascii_glyph_collides_with_the_L_star_prefix(self):
        """The `L**` defect. A mark equal to the prefix's own "*" makes one ambiguous token.

        Checked across the states that emit a mark, in ASCII mode with colour off — the exact
        configuration in which the collision is unrecoverable, because there is no second
        channel left to tell the two apart.
        """
        ascii_env = {"NO_COLOR": "1", "LODESTAR_STATUSLINE_ASCII": "1"}
        dirty = self.repo("ascii-dirty")
        with open(os.path.join(dirty, "scratch"), "w", encoding="utf-8") as handle:
            handle.write("x\n")
        states = [
            self.run_segment(dirty, line=self.write_line("f"), env=ascii_env),
            self.run_segment(dirty,
                             line=self.write_line("d2",
                                                  epoch=int(time.time()) - SILENT_AFTER * 2),
                             env=ascii_env),
            self.run_segment(dirty, line=self.write_line("s2", fresh=2, silent=1),
                             env=ascii_env),
        ]
        for out in states:
            body = out.strip()
            self.assertTrue(body.startswith("L"), "expected an L-prefixed token, got %r" % body)
            # Everything after the literal "L*" prefix is marks; none may be another "*".
            marks = body[2:] if body[:2] == "L*" else body[1:]
            self.assertNotIn("*", marks,
                             "a mark equal to the prefix's own '*' renders an ambiguous "
                             "token (the L** defect): %r" % body)

    def test_output_is_a_single_line_with_no_backslashes(self):
        """The host interpolates this into `printf '%b'`, which EATS backslash escapes.

        A stray backslash would be silently reinterpreted — and a newline would break the
        status line into two, displacing everything below it.
        """
        path = self.repo("oneline")
        out = self.run_segment(path, line=self.write_line("f"))
        self.assertNotIn("\\", out)
        self.assertNotIn("\n", out.rstrip("\n"))


class CostTest(SegmentCase):
    def test_the_segment_stays_well_under_the_refresh_interval(self):
        """It runs every 10 s beside a host script already costing 0.6-0.7 s.

        The bound is loose on purpose — this is a guard against someone adding a fetch, a
        network call or a Python startup to a path that must only ever read one small file and
        ask git two cheap questions. It is not a benchmark, and CI jitter must not redden it.
        """
        path = self.repo("cost")
        line = self.write_line("f")
        start = time.time()
        for _ in range(3):
            self.run_segment(path, line=line)
        each = (time.time() - start) / 3
        self.assertLess(each, 1.5,
                        "segment took %.2fs per run — something blocking was added" % each)


if __name__ == "__main__":
    unittest.main()
