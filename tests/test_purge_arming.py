"""Who may arm a purge unattended — `sync/PURGE_ARMED` and `purge.sweep_may_arm`.

Lukas's ruling, 2026-10-01: interactive-only arming meant the home WSL box could never finish
a fleet-wide purge, so a purge would be reported done while material stayed on that disk —
*"I don't like this. I want it to work. Make it work."*

🔴 **The property these tests exist for is the EMPTY-SELECTOR GUARD, and it is not what the
selector grammar does on its own.** `machine.selector_matches` documents that an empty spec
matches EVERY machine. That is the right default for "which machines does this skill apply to"
and catastrophic for "which machines may delete a repository without being asked": a truncated,
emptied, or absent declaration would arm the whole fleet for an act with no undo. So the guard
sits in front of the grammar, and the tests below drive it from both sides — including through
a REAL empty file and a REAL missing file, not only through a stubbed selector, because the
file-reading half is where an emptied file actually arrives.
"""

import os
import re
import sys
import inspect
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "bin"))

from lib import machine, purge  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Sandbox(unittest.TestCase):
    """A temp root carrying its own sync/PURGE_ARMED and machines.md."""

    MACHINES = (
        "| Key | Role | Labels | Hosts |\n"
        "|-----|------|--------|-------|\n"
        "| mt-001 | desktop | mac, work | all |\n"
        "| mt-002 | laptop | mac, work | all |\n"
        "| tower | home | windows-wsl, home | github.com |\n"
    )

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="lodestar-purge-arming-")
        os.makedirs(os.path.join(self.root, "sync"))
        self.machines = os.path.join(self.root, "machines.md")
        with open(self.machines, "w", encoding="utf-8") as handle:
            handle.write("# Machines\n\n" + self.MACHINES)
        import shutil
        self.addCleanup(shutil.rmtree, self.root, True)

    def declare(self, text):
        with open(os.path.join(self.root, "sync", purge.ARMED_DECLARATION),
                  "w", encoding="utf-8") as handle:
            handle.write(text)

    def may(self, key):
        return purge.sweep_may_arm(key, root=self.root, machines_path=self.machines)


class TheEmptyDeclarationArmsNobody(Sandbox):
    """The single most expensive thing in this file to get wrong."""

    def test_a_MISSING_file_arms_nobody(self):
        self.assertEqual(purge.armed_selector(self.root), "")
        for key in ("mt-001", "mt-002", "tower"):
            self.assertFalse(self.may(key), "a missing declaration armed %s" % key)

    def test_an_EMPTY_file_arms_nobody(self):
        self.declare("")
        for key in ("mt-001", "mt-002", "tower"):
            self.assertFalse(self.may(key), "an empty declaration armed %s" % key)

    def test_a_COMMENTS_ONLY_file_arms_nobody(self):
        """The shipped file is exactly this: 30 lines of explanation and no selector."""
        self.declare("# why this exists\n#   mt-001\n\n   \n# nothing listed yet\n")
        self.assertEqual(purge.armed_selector(self.root), "")
        for key in ("mt-001", "mt-002", "tower"):
            self.assertFalse(self.may(key), "a comments-only declaration armed %s" % key)

    def test_a_WHITESPACE_ONLY_file_arms_nobody(self):
        self.declare("\n\n \t \n\n")
        self.assertFalse(self.may("mt-001"))

    def test_the_bare_grammar_would_have_said_YES_to_all_three(self):
        """🔴 Proves the guard is doing real work rather than agreeing with the grammar.

        If this ever starts failing because `selector_matches("")` returns False, the guard has
        become redundant — but do NOT then delete it: `selector_matches` is shared with skills
        and registry rows, where empty-means-everything is the correct and deliberate reading.
        The guard's job is to refuse to inherit that reading here.
        """
        for key in ("mt-001", "mt-002", "tower"):
            self.assertTrue(machine.selector_matches("", key, []),
                            "the grammar no longer treats empty as all — re-read the docstring")

    def test_the_shipped_declaration_says_what_we_think_it_says(self):
        """The real committed declaration, not a fixture.

        It read `""` when the mechanism landed and now reads `all`, by Lukas's ruling of
        2026-10-01 — *"I want it to work. Make it work."* Pinned to the literal value rather
        than to "non-empty", so widening or narrowing it is a deliberate edit here and shows up
        beside the reason. This test used to assert emptiness and told its own reader to
        replace it with the specific value if a machine was ever declared; that is what this is.
        """
        self.assertEqual(purge.armed_selector(REPO), "all",
                         "sync/PURGE_ARMED changed. If deliberate, update this expectation and "
                         "say why in the file; if not, the fleet's purge arming just moved.")


class ADeclaredMachineIsArmed(Sandbox):
    def test_a_machine_key_on_its_own_line(self):
        self.declare("tower\n")
        self.assertTrue(self.may("tower"))
        self.assertFalse(self.may("mt-001"))
        self.assertFalse(self.may("mt-002"))

    def test_several_lines_are_any_of(self):
        self.declare("# rollout\ntower   # home box first\nmt-002  # then the laptop\n")
        self.assertTrue(self.may("tower"))
        self.assertTrue(self.may("mt-002"))
        self.assertFalse(self.may("mt-001"))

    def test_a_label_selects_every_machine_carrying_it(self):
        self.declare("mac\n")
        self.assertTrue(self.may("mt-001"))
        self.assertTrue(self.may("mt-002"))
        self.assertFalse(self.may("tower"))

    def test_a_plus_requires_every_label(self):
        self.declare("mac+work\n")
        self.assertTrue(self.may("mt-001"))
        self.assertFalse(self.may("tower"))

    def test_all_arms_the_fleet_but_only_when_WRITTEN(self):
        """`all` is a deliberate sentence someone typed and committed. That is the difference
        between it and an empty file, which is an accident."""
        self.declare("all\n")
        for key in ("mt-001", "mt-002", "tower"):
            self.assertTrue(self.may(key))

    def test_an_unknown_token_arms_nobody_rather_than_everybody(self):
        self.declare("not-a-machine\n")
        for key in ("mt-001", "mt-002", "tower"):
            self.assertFalse(self.may(key), "an unknown selector armed %s" % key)

    def test_the_key_is_matched_case_insensitively(self):
        self.declare("TOWER\n")
        self.assertTrue(self.may("tower"))


class TheDeclarationIsSharedState(unittest.TestCase):
    """It must travel, and it must be the kind of file a human reviews in a diff."""

    def test_it_is_sweepable_so_a_revocation_actually_reaches_the_fleet(self):
        """A declaration the sweep withholds would arm a machine that could never be disarmed
        from here — the inverse of the statusline `.sh` problem, and far worse."""
        from lib import gitutil
        source, sweepable = gitutil.partition_paths(["sync/" + purge.ARMED_DECLARATION])
        self.assertEqual(source, [])
        self.assertEqual(sweepable, ["sync/" + purge.ARMED_DECLARATION])

    def test_it_exists_and_explains_itself(self):
        path = os.path.join(REPO, "sync", purge.ARMED_DECLARATION)
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("NO UNDO", text.upper())
        self.assertIn("approval", text.lower(),
                      "the file must say that approval is NOT what this removes")


class ArmingByHandIsUnaffected(unittest.TestCase):
    def test_the_declaration_has_exactly_one_consumer_and_it_is_the_sweep(self):
        """`converge --arm-purge` must keep working on every machine regardless of the file, so
        the declaration can never be the thing that stops a human who is present. Asserted by
        census: `sweep_may_arm` is read by the sweep and by nothing in the converge path.
        """
        import lifecycle
        self.assertNotIn("sweep_may_arm", inspect.getsource(lifecycle.cmd_converge))
        self.assertNotIn("sweep_may_arm", inspect.getsource(lifecycle._do_purge))
        body = inspect.getsource(lifecycle)
        body = re.sub(r'"""(?:.|\n)*?"""', "", body)
        body = re.sub(r"#.*", "", body)
        self.assertNotIn("sweep_may_arm", body,
                         "the hand-run converge path now consults the sweep's declaration")


if __name__ == "__main__":
    unittest.main()
