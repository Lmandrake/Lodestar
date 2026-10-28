"""Fresh-machine simulation: the arrival sequence a never-converged machine walks through.

`docs/LAPTOP_PARITY.md` opens with this project's own lesson — *a propagation leg you did
not watch is one you cannot claim*. The Laptop (`mt-002`) is about to run the D18
`~/.claude` convergence for the first time, and the existing tests exercise the pieces of
that convergence rather than the **order** a fresh machine actually meets them in. If it
misbehaves there it misbehaves unattended, on the machine the Desktop cannot inspect
(`LAPTOP_PARITY.md` §"The Desktop CANNOT determine").

So this module simulates the whole leg as one ordered story, against the real Laptop's real
starting state: a pre-existing, **unmanaged** `~/.claude/CLAUDE.md` (its live file begins
`# Resources` and was never claudeconf-managed — `claude/incoming/mt-002-CLAUDE.md`), no
per-machine provenance copy, and no skill symlinks.

    1. First convergence captures and changes nothing      (Row 6, first bullet)
    2. Repeated sweeps stay put                            (I1 + I2, ~127 s apart, forever)
    3. Adoption resolves it                                (Row 6, second bullet)
    4. Steady state propagates a new shared module          (the silent fast-forward)
    5. A `#` memory written after steady state is safe      (the whole point of I1)
    6. Skills materialize without ever creating a dead link (Row 4, the self-healing half)

Nothing here touches the real `~/.claude` or the real board: `CLAUDE_HOME`,
`LIVE_CLAUDE_MD`, `LODESTAR_ROOT` and `LODESTAR_MACHINE` are all redirected into a temp
tree, following `tests/test_claudeconf.py`'s `ConfTest`. Two deliberate departures from
that fixture, both required by `tests/test_zz_env_isolation.py`: every cleanup is registered
*before* the thing it restores is mutated, and the saved values are passed to the cleanup
**by value** rather than read off `self` at teardown — see that module's docstring for the
leak that shape caused on 2026-09-01.

Assertion messages name the guarantee that broke, not the values that differed. Whoever
reads a failure here is most likely holding a Laptop that did something wrong.
"""

import contextlib
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

import claudeconf

#: A laptop-shaped key, so a Desktop-only claim leaking into a shared section is visible.
MACHINE_KEY = "fresh-box"

MACHINES = """| Key | Role |
|-----|------|
| %s | laptop |
""" % MACHINE_KEY

#: The state the Laptop is actually in: hand-written, never rendered, no banner. Shaped like
#: `claude/incoming/mt-002-CLAUDE.md` — a fleet-wide reference section and a machine fact
#: that would be a lie on the Desktop, which is why adoption cannot be mechanical.
#:
#: The skills path below is deliberately stale sample text, NOT a live claim. That clone never
#: existed on this laptop; the real machine section was corrected on 2026-09-23 to point at the
#: installed `skill-creator` plugin. Left unchanged because the fixture's job is to look like
#: unmanaged drift — fixing it would only make the sample less representative.
UNMANAGED_LIVE = """# Resources

The Anthropic skills clone lives at ~/Drive/skills/ — consult it when authoring
a skill.

# This machine

This machine is the Laptop: it has a battery and a lid, and Drive is at ~/My Drive/.
"""

#: Written to the live file after steady state, standing in for a `#` memory or a
#: `/revise-claude-md` edit landing mid-session.
LATE_MEMORY = "\n# Remembered\n\nThe Laptop keeps its own GDrive path.\n"

_ENV_KEYS = ("LODESTAR_ROOT", "LODESTAR_MACHINE")

SKILL_MD = "---\nname: %s\ndescription: a simulated skill\n---\n\nBody.\n"


def _restore_env(saved):
    """Put the watched environment back, from a snapshot taken **by value**.

    A module-level function taking the snapshot as an argument, rather than a bound method
    reading `self._saved`: that defers the read to teardown, and anything that rewrites the
    attribute in between makes the restore faithfully put the wrong values back. That is the
    exact mechanism that leaked `LODESTAR_ROOT` into `tests/test_sync.py` on 2026-09-01.
    """
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    import lib.machine as machine_mod
    machine_mod._key_cache = None


def _restore_claudeconf(home, live):
    """Point claudeconf back at the real ~/.claude. Same by-value reasoning as above.

    This one is not merely hygiene: this module is the only one that both redirects
    CLAUDE_HOME and creates symlinks, so a restore that runs early — or restores a temp
    value — is how a test suite eats the real global skill set.
    """
    claudeconf.CLAUDE_HOME = home
    claudeconf.LIVE_CLAUDE_MD = live


class FreshMachineTest(unittest.TestCase):
    """A machine that has never run the convergence, with the repo already cloned.

    The repo half of a fresh machine is *not* empty — `claude/global/machines/<key>.md`
    already exists there (`LAPTOP_PARITY.md` Row 6: the real file holds 39 lines of Laptop
    specifics and is "correct and waiting"). What is missing is everything per-machine that
    only a local run produces: the provenance copy, the adopt queue, the skill links.
    """

    def setUp(self):
        # realpath: on macOS mkdtemp hands back /var/..., which is a symlink to
        # /private/var/... Without this, a path comparison between something we built and
        # something claudeconf resolved can pass for the wrong reason, or fail for one.
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="lodestar-fresh-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self.root = os.path.join(self.tmp, "board")
        self.globals = os.path.join(self.root, "claude", "global")
        os.makedirs(os.path.join(self.globals, "machines"))
        self._write(os.path.join(self.root, "machines.md"), MACHINES)

        self.home = os.path.join(self.tmp, "claude-home")
        self.live_skills = os.path.join(self.home, "skills")
        # Claude Code creates ~/.claude/skills itself, so a fresh machine has the directory
        # and none of the links. Empty, not absent, is the honest starting state.
        os.makedirs(self.live_skills)

        # Redirect the two machine-local state files into the tmpdir. These live OUTSIDE the
        # board (`~/dev/.lodestar/...`) by design, so LODESTAR_ROOT does not reach them and
        # they are not covered by any other redirection in this setUp.
        #
        # This is not hypothetical tidiness. Every test here calls
        # `claudeconf.skills_state(dry_run=False)`, which now records provenance on each link
        # it creates and consults the prune-rate budget. Without these patches a full suite
        # run wrote this module's fixture skill names (`alpha`, `orphan`) into the real
        # `~/dev/.lodestar/skill-links.json`, replacing the machine's actual owned set — which
        # silently made every real link unprunable — and spent the real 3-per-hour prune
        # budget. Observed on the Desktop 2026-09-03 after the provenance work landed.
        from lib import claudeskills as _claudeskills
        for _name, _target in (("links_state_file", "skill-links.json"),
                               ("prune_state_file", "skill-prunes.json")):
            _patch = mock.patch.object(_claudeskills, _name,
                                       return_value=os.path.join(self.tmp, _target))
            _patch.start()
            self.addCleanup(_patch.stop)

        # Cleanups first, mutation second. Registered in this order deliberately: if the
        # setUp below this line raises, the environment is still put back.
        saved_env = dict((key, os.environ.get(key)) for key in _ENV_KEYS)
        self.addCleanup(_restore_env, saved_env)
        self.addCleanup(_restore_claudeconf,
                        claudeconf.CLAUDE_HOME, claudeconf.LIVE_CLAUDE_MD)

        os.environ["LODESTAR_ROOT"] = self.root
        os.environ["LODESTAR_MACHINE"] = MACHINE_KEY
        claudeconf.CLAUDE_HOME = self.home
        claudeconf.LIVE_CLAUDE_MD = os.path.join(self.home, "CLAUDE.md")
        import lib.machine as machine_mod
        machine_mod._key_cache = None

        stack = contextlib.ExitStack()
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.addCleanup(stack.close)

        # What a fresh clone of the board already carries.
        self.section("machines/%s.md" % MACHINE_KEY, "# Machine\n\nA laptop.\n")
        self.section("10-ritual.md", "# The Lodestar operating ritual\n\nAttach to a loop.\n")
        self.section("20-saving.md", "# Save and push progress as you go\n\nCommit often.\n")

    # ---- fixture helpers -------------------------------------------------------------

    @staticmethod
    def _write(path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    @staticmethod
    def _read(path):
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def section(self, relative, text):
        return self._write(os.path.join(self.globals, relative), text)

    def live(self):
        return self._read(claudeconf.LIVE_CLAUDE_MD)

    def sweep(self):
        """One unattended pass, as the launchd sweep runs it every ~127 s."""
        return claudeconf.converge_claudemd()

    # ---- the arrival sequence, as reusable steps -------------------------------------

    def arrive(self):
        """Put the machine in its real pre-convergence state. Returns the live bytes."""
        self._write(claudeconf.LIVE_CLAUDE_MD, UNMANAGED_LIVE)
        self.assertFalse(
            os.path.exists(claudeconf.provenance_path()),
            "a machine that has never converged must have no provenance copy; the "
            "simulation is not starting from the state it claims to simulate")
        self.assertEqual(
            claudeconf.pending_captures(), [],
            "a machine that has never converged must have an empty adopt queue; the "
            "simulation is not starting from the state it claims to simulate")
        return UNMANAGED_LIVE

    def adopt_everything(self):
        """Do what `claudeconf.py adopt` asks a session to do, and return the chunks filed.

        The split is the judgment `cmd_adopt` deliberately refuses to make: the reference
        section is fleet-wide, the battery-and-lid paragraph is a lie on the Desktop.
        """
        pending = claudeconf.pending_captures()
        self.assertEqual(
            len(pending), 1,
            "adoption needs exactly the one capture the first sweep took; found %d"
            % len(pending))
        capture = pending[0]
        chunks = claudeconf.capture_diff(self._read(capture), claudeconf.render_text())
        self.assertEqual(
            len(chunks), 2,
            "the capture holds one fleet-wide block and one machine-specific block, so "
            "adoption must be offered both; capture_diff offered %d" % len(chunks))
        claudeconf.file_chunk(chunks[0], "40-adopted.md")
        claudeconf.file_chunk(chunks[1], "machines/%s.md" % MACHINE_KEY)
        done = claudeconf.mark_adopted(capture)
        claudeconf.finish_adopt()
        return chunks, done

    # ---- assertions that name the guarantee ------------------------------------------

    def assertLiveUnchanged(self, before, guarantee):
        self.assertEqual(
            before, self.live(),
            "%s — the live ~/.claude/CLAUDE.md was rewritten. This is the file that "
            "actually takes effect in a session, so a change here is a behaviour change "
            "the user did not ask for." % guarantee)

    def assertNoProvenance(self, guarantee):
        self.assertFalse(
            os.path.exists(claudeconf.provenance_path()),
            "%s — invariant I1 broken: provenance advanced on drift. The next sweep ~127 s "
            "later would read live == previous, conclude fast-forward, and overwrite the "
            "edit it had just captured. The content would survive in git while the file "
            "that takes effect silently reverted." % guarantee)

    def assertNoBrokenLinks(self, guarantee):
        """No symlink under the live skills dir may point at a path that does not exist.

        A dangling skill symlink does not error — Claude Code simply does not see the skill
        — so this is the failure mode that costs a capability with no signal anywhere
        (`LAPTOP_PARITY.md` Row 4).
        """
        dangling = []
        for name in sorted(os.listdir(self.live_skills)):
            path = os.path.join(self.live_skills, name)
            if os.path.islink(path) and not os.path.exists(path):
                dangling.append("%s -> %s" % (name, os.readlink(path)))
        self.assertEqual(
            [], dangling,
            "%s — a dangling skill symlink was left under ~/.claude/skills. It does not "
            "error; the skill just silently stops existing on this machine:\n  %s"
            % (guarantee, "\n  ".join(dangling)))


class TestStep1FirstConvergenceCaptures(FreshMachineTest):
    """`LAPTOP_PARITY.md` Row 6: "The machine keeps the CLAUDE.md it has."

    Save it, then wait to sort it out. This is the only step that runs before anybody has
    looked at the machine, so it is the one that has to be unconditionally non-destructive.
    """

    def test_the_first_sweep_reports_drifted(self):
        before = self.arrive()
        outcome, detail = self.sweep()
        self.assertEqual(
            outcome, claudeconf.DRIFTED,
            "a pre-existing unmanaged CLAUDE.md matches neither the render nor any "
            "provenance copy, so the first sweep must report DRIFTED and capture. Got %r "
            "(%s)." % (outcome, detail))
        self.assertLiveUnchanged(before, "the first sweep on a fresh machine must render nothing")

    def test_the_first_sweep_changes_the_live_file_byte_for_byte_not_at_all(self):
        before = self.arrive()
        self.sweep()
        self.assertLiveUnchanged(
            before, "the first sweep must be byte-identical-preserving, not merely "
                    "content-preserving")

    def test_the_first_sweep_takes_exactly_one_capture_of_the_original(self):
        self.arrive()
        self.sweep()
        pending = claudeconf.pending_captures()
        self.assertEqual(
            len(pending), 1,
            "the first sweep must leave exactly one capture for `claudeconf.py adopt` to "
            "classify; found %d. Zero means the machine's own CLAUDE.md was never saved "
            "into the repo at all." % len(pending))
        self.assertEqual(
            self._read(pending[0]), UNMANAGED_LIVE,
            "the capture must be the live file verbatim — it is the only copy of what the "
            "machine had, and the only way to review a classification that turns out wrong")

    def test_the_capture_lands_in_this_machines_queue_not_a_shared_one(self):
        self.arrive()
        self.sweep()
        self.assertEqual(
            os.path.basename(claudeconf.adopt_dir()), MACHINE_KEY,
            "the adopt queue is per-machine (decision D-D): a shared committed path would "
            "make two machines overwrite each other's captures")

    def test_the_first_sweep_does_not_create_provenance(self):
        self.arrive()
        self.sweep()
        self.assertNoProvenance("after the very first sweep on a fresh machine")

    def test_a_dry_run_first_sweep_writes_nothing_at_all(self):
        before = self.arrive()
        outcome, _ = claudeconf.converge_claudemd(dry_run=True)
        self.assertEqual(
            outcome, claudeconf.DRIFTED,
            "a dry run must predict the same outcome the real sweep produces, or the "
            "shadow-mode report describes a run that would not have happened")
        self.assertEqual(
            claudeconf.pending_captures(), [],
            "a dry run must not write a capture; the queue it reports on would then "
            "include entries no real sweep ever made")
        self.assertLiveUnchanged(before, "a dry-run sweep")


class TestStep2RepeatedSweepsStayPut(FreshMachineTest):
    """The sweep runs every ~127 s and nobody is watching. Drift persists by design.

    Both invariants are load-bearing *here* rather than in the abstract: I1 because the
    second sweep is the one that would revert the file, and I2 because at ~680 sweeps a day
    a naive capture writes 680 committed files into the board.
    """

    SWEEPS = 5

    def test_every_later_sweep_still_reports_drifted(self):
        self.arrive()
        self.sweep()
        for index in range(self.SWEEPS):
            outcome, detail = self.sweep()
            self.assertEqual(
                outcome, claudeconf.DRIFTED,
                "sweep %d of an unattended machine changed its verdict to %r (%s). Drift "
                "persists until a human adopts it; anything else means the machine "
                "resolved a judgment call on its own."
                % (index + 2, outcome, detail))

    def test_repeated_sweeps_never_touch_the_live_file(self):
        before = self.arrive()
        for _ in range(self.SWEEPS + 1):
            self.sweep()
        self.assertLiveUnchanged(
            before, "%d unattended sweeps in a row" % (self.SWEEPS + 1))

    def test_repeated_sweeps_capture_once_I2(self):
        self.arrive()
        for _ in range(self.SWEEPS + 1):
            self.sweep()
        pending = claudeconf.pending_captures()
        self.assertEqual(
            len(pending), 1,
            "invariant I2 broken: capture is not idempotent. %d captures after %d sweeps; "
            "at ~680 sweeps a day that is ~680 committed files a day, and the adopt queue "
            "stops being reviewable." % (len(pending), self.SWEEPS + 1))

    def test_repeated_sweeps_never_create_provenance_I1(self):
        self.arrive()
        for index in range(self.SWEEPS + 1):
            self.sweep()
            self.assertNoProvenance("at sweep %d of an unadopted machine" % (index + 1))

    def test_the_machine_keeps_its_own_content_across_every_sweep(self):
        self.arrive()
        for index in range(self.SWEEPS + 1):
            self.sweep()
            self.assertIn(
                "it has a battery and a lid", self.live(),
                "the machine's own CLAUDE.md content vanished from the live file at sweep "
                "%d. Unattended, this is a silent capability change in every session that "
                "machine opens." % (index + 1))

    def test_claude_md_is_not_snapshotted_unconditionally(self):
        """Structural guard for I1 on the path a fresh machine takes first.

        `snapshot_all` copies SNAPSHOT_FILES into the repo on every sweep, with no
        provenance reasoning at all. CLAUDE.md joining that tuple would advance the
        provenance copy on drift by a completely different route than converge_claudemd,
        and the next sweep would fast-forward over the edit.
        """
        self.assertNotIn(
            "CLAUDE.md", claudeconf.SNAPSHOT_FILES,
            "CLAUDE.md must never join SNAPSHOT_FILES: that tuple is copied "
            "unconditionally every sweep, which would advance provenance on drift and "
            "hand the next sweep a licence to revert a # memory")

    def test_snapshotting_a_fresh_machine_does_not_advance_provenance(self):
        """And the same thing behaviourally, not just structurally."""
        before = self.arrive()
        self.sweep()
        claudeconf.snapshot_all()
        self.assertNoProvenance("after a snapshot_all on an unadopted machine")
        self.assertLiveUnchanged(before, "snapshot_all, which only ever copies inward")


class TestStep3AdoptionResolvesIt(FreshMachineTest):
    """Row 6, second bullet: a session files the capture, then renders. Then it is settled."""

    def test_adoption_leaves_the_machine_current(self):
        self.arrive()
        self.sweep()
        self.adopt_everything()
        outcome, detail = self.sweep()
        self.assertEqual(
            outcome, claudeconf.CURRENT,
            "after adoption the machine must report CURRENT. Got %r (%s) — a machine stuck "
            "on DRIFTED forever turns the flag into wallpaper, which is D13's corollary "
            "failing." % (outcome, detail))

    def test_the_live_file_carries_both_the_shared_and_the_machine_content(self):
        self.arrive()
        self.sweep()
        self.adopt_everything()
        text = self.live()
        self.assertIn(
            "The Anthropic skills clone lives at", text,
            "content filed into a shared module must reach the live file, or adoption "
            "silently deleted a rule the machine had")
        self.assertIn(
            "it has a battery and a lid", text,
            "content filed into the machine section must reach the live file, or adoption "
            "silently deleted this machine's own facts")

    def test_the_machine_specific_claim_stays_out_of_the_shared_modules(self):
        self.arrive()
        self.sweep()
        self.adopt_everything()
        for path in claudeconf.shared_sections():
            self.assertNotIn(
                "it has a battery and a lid", self._read(path),
                "a machine-specific claim was filed into %s, a shared module. It will "
                "propagate to the Desktop, where it is simply false — the failure mode "
                "D18 keeps a human in the loop to prevent."
                % os.path.basename(path))

    def test_the_capture_survives_in_done(self):
        self.arrive()
        self.sweep()
        _, done = self.adopt_everything()
        self.assertIn(
            os.path.join("adopt", MACHINE_KEY, "done", ""), done,
            "an adopted capture belongs in adopt/<key>/done/, so the verbatim original "
            "stays reviewable; it landed at %s" % done)
        self.assertEqual(
            self._read(done), UNMANAGED_LIVE,
            "the retained capture must still be the byte-for-byte original — it is the "
            "only way to audit a classification that turns out to be wrong")
        self.assertEqual(
            claudeconf.pending_captures(), [],
            "adoption must clear the pending queue, or the machine keeps reporting work "
            "that is already done")

    def test_adoption_without_the_closing_render_would_flag_forever(self):
        """`finish_adopt` is required, not cosmetic — pinned on the fresh-machine path.

        A filed chunk does not reproduce the live file's ad-hoc spacing byte-for-byte, so
        skipping the render leaves the machine DRIFTED on every sweep for the rest of time.
        """
        self.arrive()
        self.sweep()
        capture = claudeconf.pending_captures()[0]
        chunks = claudeconf.capture_diff(self._read(capture), claudeconf.render_text())
        claudeconf.file_chunk(chunks[0], "40-adopted.md")
        claudeconf.file_chunk(chunks[1], "machines/%s.md" % MACHINE_KEY)
        claudeconf.mark_adopted(capture)
        outcome, _ = self.sweep()
        self.assertEqual(
            outcome, claudeconf.DRIFTED,
            "filing chunks alone must NOT make the machine look current: if it did, the "
            "capture-then-render contract would be satisfied by an accident of spacing "
            "rather than by the closing render, and the guarantee would be untested")

    def test_the_adopt_command_refuses_to_render_while_chunks_are_unfiled(self):
        """Rendering here would drop the machine's content before it propagated."""
        before = self.arrive()
        self.sweep()
        self.assertEqual(
            claudeconf.main(["adopt"]), 0,
            "`claudeconf.py adopt` must report unfiled chunks without failing; a nonzero "
            "exit here is a fresh machine's first interactive step looking broken")
        self.assertLiveUnchanged(
            before, "`claudeconf.py adopt` with chunks still unclassified")
        self.assertEqual(
            len(claudeconf.pending_captures()), 1,
            "an unclassified capture must stay pending, or the content is dropped from the "
            "queue before anyone decided where it belongs")

    def test_adoption_is_idempotent_across_the_next_sweeps(self):
        self.arrive()
        self.sweep()
        self.adopt_everything()
        settled = self.live()
        for index in range(3):
            outcome, _ = self.sweep()
            self.assertEqual(
                outcome, claudeconf.CURRENT,
                "sweep %d after adoption reported %r; a settled machine must stay settled "
                "without a human touching it again" % (index + 1, outcome))
            self.assertLiveUnchanged(settled, "sweep %d after adoption" % (index + 1))


class TestStep4SteadyStatePropagates(FreshMachineTest):
    """The payoff: Lukas's manual `render` errand (Row 6) disappears.

    A rule edited on the *other* machine and pulled in must arrive here with no human step.
    """

    NEW_SHARED = "# Airlock\n\nCheck the airlock before starting work.\n"

    def _settled(self):
        self.arrive()
        self.sweep()
        self.adopt_everything()
        return self.live()

    def test_a_new_shared_module_fast_forwards(self):
        self._settled()
        self.section("30-airlock.md", self.NEW_SHARED)
        outcome, detail = self.sweep()
        self.assertEqual(
            outcome, claudeconf.FAST_FORWARD,
            "a shared module pulled from the other machine must fast-forward onto an "
            "untouched live file. Got %r (%s) — if this is DRIFTED, fleet-wide rules stop "
            "propagating and every machine needs a manual render again."
            % (outcome, detail))

    def test_the_new_rule_reaches_the_live_file_with_no_human_step(self):
        self._settled()
        self.section("30-airlock.md", self.NEW_SHARED)
        self.sweep()
        self.assertIn(
            "Check the airlock before starting work.", self.live(),
            "the fast-forward reported success but the new rule is not in the file that "
            "takes effect; the propagation leg is exactly what this simulation exists to "
            "watch")

    def test_the_fast_forward_preserves_the_adopted_machine_content(self):
        self._settled()
        self.section("30-airlock.md", self.NEW_SHARED)
        self.sweep()
        self.assertIn(
            "it has a battery and a lid", self.live(),
            "a fast-forward must not drop previously adopted machine-specific content; "
            "the render is the sum of the sections, not the newest one")

    def test_the_sweep_after_a_fast_forward_is_current_and_idempotent(self):
        self._settled()
        self.section("30-airlock.md", self.NEW_SHARED)
        self.sweep()
        after = self.live()
        for index in range(3):
            outcome, _ = self.sweep()
            self.assertEqual(
                outcome, claudeconf.CURRENT,
                "sweep %d after a fast-forward reported %r; a machine that keeps "
                "fast-forwarding is rewriting the live file every ~127 s"
                % (index + 1, outcome))
            self.assertLiveUnchanged(after, "sweep %d after a fast-forward" % (index + 1))

    def test_a_fast_forward_advances_provenance_so_the_next_edit_is_still_detectable(self):
        self._settled()
        self.section("30-airlock.md", self.NEW_SHARED)
        self.sweep()
        self.assertEqual(
            self._read(claudeconf.provenance_path()), self.live(),
            "provenance must equal the live file after a render, or the very next sweep "
            "mistakes our own render for an external edit and captures it — turning every "
            "propagated rule into a spurious adopt-queue entry")


class TestStep5AMemoryAfterSteadyStateIsSafe(FreshMachineTest):
    """A `#` memory or `/revise-claude-md` writes straight to the live file mid-session.

    This is the case D18 exists for, and the one that has to hold on a machine nobody is
    watching: the sweep is 127 s away and it must not win.
    """

    def _settled(self):
        self.arrive()
        self.sweep()
        self.adopt_everything()

    def test_a_late_memory_is_captured(self):
        self._settled()
        with open(claudeconf.LIVE_CLAUDE_MD, "a", encoding="utf-8") as handle:
            handle.write(LATE_MEMORY)
        outcome, detail = self.sweep()
        self.assertEqual(
            outcome, claudeconf.DRIFTED,
            "a `#` memory written after steady state must be captured, not treated as "
            "noise. Got %r (%s)." % (outcome, detail))
        pending = claudeconf.pending_captures()
        self.assertEqual(
            len(pending), 1,
            "the late memory must produce exactly one capture so it can be adopted; found "
            "%d" % len(pending))
        self.assertIn(
            "The Laptop keeps its own GDrive path.", self._read(pending[0]),
            "the capture must contain the memory that was just written, or the only copy "
            "of it is the live file the next sweep might overwrite")

    def test_a_late_memory_is_not_reverted(self):
        self._settled()
        with open(claudeconf.LIVE_CLAUDE_MD, "a", encoding="utf-8") as handle:
            handle.write(LATE_MEMORY)
        after_edit = self.live()
        self.sweep()
        self.assertIn(
            "The Laptop keeps its own GDrive path.", self.live(),
            "the sweep reverted a `#` memory. This is the failure D18 was designed against: "
            "the content survives in git while the file that actually takes effect loses "
            "it, so the memory stops applying and the user is fighting the machine")
        self.assertLiveUnchanged(after_edit, "a sweep immediately after a `#` memory")

    def test_the_memory_survives_many_later_sweeps(self):
        self._settled()
        with open(claudeconf.LIVE_CLAUDE_MD, "a", encoding="utf-8") as handle:
            handle.write(LATE_MEMORY)
        after_edit = self.live()
        for index in range(6):
            self.sweep()
            self.assertLiveUnchanged(
                after_edit,
                "sweep %d after a `#` memory (~%d s of unattended sweeping)"
                % (index + 1, (index + 1) * 127))

    def test_a_late_memory_does_not_advance_provenance(self):
        self._settled()
        before = self._read(claudeconf.provenance_path())
        with open(claudeconf.LIVE_CLAUDE_MD, "a", encoding="utf-8") as handle:
            handle.write(LATE_MEMORY)
        self.sweep()
        self.assertEqual(
            before, self._read(claudeconf.provenance_path()),
            "invariant I1 broken after steady state: provenance advanced on drift, so the "
            "next sweep reads live == previous, calls it a fast-forward, and overwrites the "
            "memory it just captured")

    def test_the_late_memory_can_itself_be_adopted(self):
        self._settled()
        with open(claudeconf.LIVE_CLAUDE_MD, "a", encoding="utf-8") as handle:
            handle.write(LATE_MEMORY)
        self.sweep()
        capture = claudeconf.pending_captures()[0]
        chunks = claudeconf.capture_diff(self._read(capture), claudeconf.render_text())
        self.assertEqual(
            len(chunks), 1,
            "only the newly appended block is unclassified; already-adopted content must "
            "not be re-offered, or every adoption grows the next one's diff. Got %d chunks."
            % len(chunks))
        claudeconf.file_chunk(chunks[0], "machines/%s.md" % MACHINE_KEY)
        claudeconf.mark_adopted(capture)
        claudeconf.finish_adopt()
        outcome, _ = self.sweep()
        self.assertEqual(
            outcome, claudeconf.CURRENT,
            "the machine must return to CURRENT after the late memory is adopted; "
            "otherwise the second round of adoption does not close and the flag is "
            "permanent")
        self.assertIn(
            "The Laptop keeps its own GDrive path.", self.live(),
            "adopting the memory must keep it in the live file, not merely in the repo")


class TestStep6SkillsOnAFreshMachine(FreshMachineTest):
    """Row 4's self-healing half, and the reason it matters.

    A dangling skill symlink does not error — Claude Code simply does not see the skill — so
    the failure this step guards is a capability quietly missing on a machine we cannot
    inspect. On a fresh machine every target is absent, which is the worst case: the code
    must report and clone, never link.
    """

    def setUp(self):
        super(TestStep6SkillsOnAFreshMachine, self).setUp()
        self.alpha_target = os.path.join(self.tmp, "dev", "Alpha", ".claude", "skills", "alpha")
        self.orphan_target = os.path.join(self.tmp, "dev", "Orphan", "skills", "orphan")
        self._write(os.path.join(self.root, "claude", "skills.md"),
                    "| Skill | Target | Repo | Machines |\n"
                    "|-------|--------|------|----------|\n"
                    "| alpha | %s | Alpha | all |\n"
                    "| orphan | %s |  | all |\n"
                    % (self.alpha_target, self.orphan_target))
        # `alpha`'s repo participates in sync, so its absence is clonable. `orphan`'s is not
        # in the registry at all, so its absence is a fault a human has to look at.
        self._write(os.path.join(self.root, "sync", "registry.md"),
                    "| Name | Path | Branch | Sync | Notes |\n"
                    "|------|------|--------|------|-------|\n"
                    "| Alpha | %s | main | on |  |\n"
                    % os.path.join(self.tmp, "dev", "Alpha"))

    def _make_target(self, target, name):
        os.makedirs(target, exist_ok=True)
        self._write(os.path.join(target, "SKILL.md"), SKILL_MD % name)
        return target

    def test_a_fresh_machine_starts_with_no_skill_links(self):
        self.assertEqual(
            sorted(os.listdir(self.live_skills)), [],
            "the simulation is not starting from a fresh machine's skill state")

    def test_absent_targets_produce_faults_and_clone_needs_not_links(self):
        lines, faults = claudeconf.skills_state(dry_run=False)
        self.assertTrue(
            any("orphan" in fault for fault in faults),
            "a skill whose target is missing with no registered repo must be reported as a "
            "fault — nothing else will ever make it appear. Faults were: %r" % (faults,))
        self.assertTrue(
            any("cloning" in line and "alpha" in line for line in lines),
            "a skill whose target is missing but whose repo participates in sync must be "
            "reported as needing a clone, by name, which is how Row 4 heals itself. Lines "
            "were: %r" % (lines,))

    def test_no_link_is_created_for_a_target_that_does_not_exist(self):
        claudeconf.skills_state(dry_run=False)
        self.assertEqual(
            sorted(os.listdir(self.live_skills)), [],
            "a link was created for a target that does not exist on this machine. It would "
            "not error — the skill would simply be invisible — so this is precisely the "
            "silent capability loss Row 4 names")
        self.assertNoBrokenLinks("after a sweep with every target absent")

    def test_repeated_sweeps_with_absent_targets_stay_link_free(self):
        for index in range(4):
            claudeconf.skills_state(dry_run=False)
            self.assertNoBrokenLinks(
                "at sweep %d with every skill target still absent" % (index + 1))
            self.assertEqual(
                sorted(os.listdir(self.live_skills)), [],
                "sweep %d created a link although no target exists yet" % (index + 1))

    def test_nothing_is_pruned_on_a_fresh_machine(self):
        """The bulk-delete guard's worst case is a machine with nothing to compare against."""
        real = self._make_target(self.alpha_target, "alpha")
        os.symlink(real, os.path.join(self.live_skills, "alpha"))
        claudeconf.skills_state(dry_run=False)
        self.assertTrue(
            os.path.islink(os.path.join(self.live_skills, "alpha")),
            "a link the table declares must never be pruned; pruning a declared skill is "
            "the table being misread as an instruction to delete")

    def test_links_appear_once_the_targets_exist(self):
        self._make_target(self.alpha_target, "alpha")
        self._make_target(self.orphan_target, "orphan")
        lines, faults = claudeconf.skills_state(dry_run=False)
        self.assertEqual(
            faults, [],
            "with every target present and valid there is nothing for a human to fix, so "
            "faults must be empty; got %r" % (faults,))
        for name, target in (("alpha", self.alpha_target), ("orphan", self.orphan_target)):
            link = os.path.join(self.live_skills, name)
            self.assertTrue(
                os.path.islink(link),
                "%s has a present, valid target, so the sweep must materialize its link — "
                "an unlinked skill is a capability the machine does not have. Lines were: "
                "%r" % (name, lines))
            self.assertEqual(
                os.path.realpath(link), os.path.realpath(target),
                "%s's link points somewhere other than the target the table declares" % name)
        self.assertNoBrokenLinks("after linking targets that exist")

    def test_a_target_arriving_later_is_linked_on_the_next_sweep(self):
        """The clone-then-link sequence a real Laptop walks, one sweep apart."""
        claudeconf.skills_state(dry_run=False)
        self.assertNoBrokenLinks("before the repo arrived")
        self._make_target(self.alpha_target, "alpha")      # stands in for the clone
        claudeconf.skills_state(dry_run=False)
        self.assertTrue(
            os.path.islink(os.path.join(self.live_skills, "alpha")),
            "a target that arrived between sweeps must be linked by the next one, or the "
            "clone Row 4 performs never becomes a usable skill")
        self.assertNoBrokenLinks("after the repo arrived and was linked")

    def test_a_dry_run_creates_nothing(self):
        self._make_target(self.alpha_target, "alpha")
        self._make_target(self.orphan_target, "orphan")
        claudeconf.skills_state(dry_run=True)
        self.assertEqual(
            sorted(os.listdir(self.live_skills)), [],
            "a dry run must not touch ~/.claude/skills; shadow mode reports on runs that "
            "did not happen and must not make them happen")

    def test_an_invalid_skill_is_reported_and_not_linked(self):
        """A cloned-but-unloadable skill: the state right after a partial clone.

        Note what this can and cannot assert. `skills_state` renders a fault as
        `"<kind>: <detail>"` and drops `Action.skill`, so a `broken` fault names the reason
        but not the skill — three of `claudeskills.validate`'s four frontmatter messages
        carry no path either, so on the Laptop `SYNC.md` would read
        `⚠ skills: broken: SKILL.md frontmatter has no description` against a 21-row table
        with nothing to say which row. Reported to the module's owner rather than asserted
        here, so this test pins the behaviour that does hold: it is a fault, and it is not
        linked.
        """
        os.makedirs(self.alpha_target)
        self._write(os.path.join(self.alpha_target, "SKILL.md"), "no frontmatter here\n")
        self._make_target(self.orphan_target, "orphan")
        _, faults = claudeconf.skills_state(dry_run=False)
        self.assertTrue(
            any(fault.startswith("broken:") for fault in faults),
            "a target that exists but would not load must be reported as broken; linking it "
            "would look like success and deliver nothing. Faults were: %r" % (faults,))
        self.assertFalse(
            os.path.exists(os.path.join(self.live_skills, "alpha")),
            "a skill that fails validation must not be linked — a link to an unloadable "
            "skill is indistinguishable from a working one until a session needs it")
        self.assertTrue(
            os.path.islink(os.path.join(self.live_skills, "orphan")),
            "one broken skill must not stop the valid ones from being linked; an "
            "all-or-nothing sweep would let a single bad SKILL.md disable the whole set")

    def test_no_table_is_reported_rather_than_acted_on(self):
        os.unlink(os.path.join(self.root, "claude", "skills.md"))
        lines, faults = claudeconf.skills_state(dry_run=False)
        self.assertEqual(
            faults, [],
            "a board with no skills table yet is not a fault, it is a machine that has not "
            "been seeded; got %r" % (faults,))
        self.assertIn(
            "--import", " ".join(lines),
            "with no table the machine must say what to run; silence is indistinguishable "
            "from a step that did not execute, which is the distinction check.py preserves")


class TestTheWholeArrivalSequence(FreshMachineTest):
    """All six steps in order, in one process, as the Laptop will actually meet them.

    The per-step classes each restart from a fresh temp machine, which is the right way to
    localize a failure but cannot catch an ordering fault — state carried from step 2 into
    step 4, say. This one runs the story straight through and asserts at every hinge.
    """

    def test_the_laptops_first_day(self):
        # 1 · arrives with its own CLAUDE.md; the sweep saves it and changes nothing.
        original = self.arrive()
        outcome, _ = self.sweep()
        self.assertEqual(outcome, claudeconf.DRIFTED,
                         "step 1: the first sweep must capture, not render")
        self.assertLiveUnchanged(original, "step 1: the first sweep")
        self.assertEqual(len(claudeconf.pending_captures()), 1,
                         "step 1: exactly one capture must be queued for adoption")
        self.assertNoProvenance("step 1")

        # 2 · nobody looks at it for eleven minutes.
        for _ in range(5):
            outcome, _ = self.sweep()
            self.assertEqual(outcome, claudeconf.DRIFTED,
                             "step 2: an unattended machine must not resolve its own drift")
        self.assertEqual(len(claudeconf.pending_captures()), 1,
                         "step 2: invariant I2 — repeated sweeps must capture only once")
        self.assertLiveUnchanged(original, "step 2: six unattended sweeps")
        self.assertNoProvenance("step 2")

        # 3 · a session adopts it.
        _, done = self.adopt_everything()
        outcome, _ = self.sweep()
        self.assertEqual(outcome, claudeconf.CURRENT,
                         "step 3: adoption must leave the machine CURRENT")
        self.assertIn("The Anthropic skills clone lives at", self.live(),
                      "step 3: shared content must reach the live file")
        self.assertIn("it has a battery and a lid", self.live(),
                      "step 3: machine-specific content must reach the live file")
        self.assertEqual(self._read(done), original,
                         "step 3: the verbatim capture must survive in done/")

        # 4 · the other machine adds a fleet-wide rule; it arrives with no human step.
        self.section("30-airlock.md", "# Airlock\n\nCheck the airlock first.\n")
        outcome, _ = self.sweep()
        self.assertEqual(outcome, claudeconf.FAST_FORWARD,
                         "step 4: a new shared module must fast-forward, not drift")
        self.assertIn("Check the airlock first.", self.live(),
                      "step 4: the propagated rule must reach the live file")
        settled = self.live()
        outcome, _ = self.sweep()
        self.assertEqual(outcome, claudeconf.CURRENT,
                         "step 4: the sweep after a fast-forward must be CURRENT")
        self.assertLiveUnchanged(settled, "step 4: the sweep after a fast-forward")

        # 5 · a `#` memory lands mid-session, 127 s before the next sweep.
        with open(claudeconf.LIVE_CLAUDE_MD, "a", encoding="utf-8") as handle:
            handle.write(LATE_MEMORY)
        with_memory = self.live()
        outcome, _ = self.sweep()
        self.assertEqual(outcome, claudeconf.DRIFTED,
                         "step 5: a late `#` memory must be captured")
        self.assertLiveUnchanged(with_memory, "step 5: the sweep after a `#` memory")
        self.assertEqual(len(claudeconf.pending_captures()), 1,
                         "step 5: the memory must be queued for adoption")

        # 6 · and none of it ever left a dead skill link behind.
        self.assertNoBrokenLinks("step 6: after the whole arrival sequence")


if __name__ == "__main__":
    unittest.main()
