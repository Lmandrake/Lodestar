"""Tests for lib.validate -- the semantic gate between a merged commit and any action.

Every fixture is written to its own temp file and passed to `validate.faults` by
explicit path, the same style as `test_registry_schema.py` and `test_tombstones.py`.
`LODESTAR_ROOT` is also pointed at a fresh temp directory in `setUp` as a second guard,
so a bug that ever made one of these calls fall back to a default path would land on
an empty scratch board rather than the real one.
"""

import os
import shutil
import tempfile
import unittest

from lib import plans as plans_lib, validate
from tests import needs_private, real_home_key, real_key  # noqa: E402

REGISTRY_HEADER = "| Name | Path | Branch | Send | Receive | Machines | Id | Former | Remote | State |"
REGISTRY_SEP = "|------|------|--------|------|---------|----------|----|--------|--------|-------|"

TOMBSTONES_HEADER = "| Id | At | Reason | Superseded by |"
TOMBSTONES_SEP = "|----|----|--------|----------------|"

MACHINES_HEADER = "| Key | Role | Labels |"
MACHINES_SEP = "|-----|------|--------|"
MACHINES_ROWS = [
    "| mt-001 | desktop | mac, work |",
    "| mb-999999 | laptop | mac |",
]

PLANS_HEADER = "| Plan id | Kind | Repo id | Machines | Old | New | Disposition | Created at | Created by | Note |"
PLANS_SEP = "|---------|------|---------|----------|-----|-----|--------------|------------|------------|------|"

APPROVALS_HEADER = "| Plan id | Fingerprint | Machine | At | By |"
APPROVALS_SEP = "|---------|-------------|---------|----|----|"


class ValidateFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-validate-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._saved_root = os.environ.get("LODESTAR_ROOT")
        os.environ["LODESTAR_ROOT"] = self.tmp
        self.addCleanup(self._restore_root)

    def _restore_root(self):
        if self._saved_root is None:
            os.environ.pop("LODESTAR_ROOT", None)
        else:
            os.environ["LODESTAR_ROOT"] = self._saved_root

    def _write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def _registry(self, rows):
        return self._write("registry.md", "\n".join([REGISTRY_HEADER, REGISTRY_SEP] + rows) + "\n")

    def _tombstones(self, rows):
        return self._write("tombstones.md",
                            "\n".join([TOMBSTONES_HEADER, TOMBSTONES_SEP] + rows) + "\n")

    def _machines(self, rows=MACHINES_ROWS):
        return self._write("machines.md", "\n".join([MACHINES_HEADER, MACHINES_SEP] + rows) + "\n")

    def _plans(self, rows):
        return self._write("plans.md", "\n".join([PLANS_HEADER, PLANS_SEP] + rows) + "\n")

    def _approvals(self, rows):
        return self._write("approvals.md", "\n".join([APPROVALS_HEADER, APPROVALS_SEP] + rows) + "\n")


class TestCleanStateHasNoFaults(ValidateFixture):
    def test_no_faults_on_a_consistent_merged_state(self):
        registry_path = self._registry([
            # Scoped by ROLE, not by label. This row said `mac` until 2026-09-30, which was
            # wrong: `registry.on_machine` matches only (role, key), so a label here matches
            # NO machine on the whole fleet -- measured -- and the row would silently never be
            # carried anywhere. A clean state cannot contain a dead scope.
            "| Alpha | ~/dev/Alpha | main | on | on | desktop | alpha |  |  | active |",
            "| Beta | ~/dev/Beta | main | on | on |  | beta |  |  | active |",
            "| Gamma | ~/dev/Gamma | main | on | on |  | gamma |  |  | retired |",
        ])
        tombstones_path = self._tombstones([
            "| gamma | 2026-09-20T10:00-07:00 | folded into beta |  |",
        ])
        machines_path = self._machines()

        found = validate.faults(registry_path, machines_path, tombstones_path)
        self.assertEqual(found, [])


class TestRowColumnCount(ValidateFixture):
    def test_a_repo_row_under_the_machine_paths_heading_is_a_fault(self):
        row = "| Alpha | ~/dev/Alpha | main | on | on |  | alpha |  |  | active |"
        text = "\n".join([REGISTRY_HEADER, REGISTRY_SEP, row, "", "## Machine paths", "",
                          "| Repo | Machine | Path |", "|---|---|---|",
                          "| Wisps | ~/dev/wisps | main | on | on |  | wisps |  |  | retired |"]) + "\n"
        path = self._write("registry.md", text)
        found = validate.faults(path, self._machines(), self._tombstones([]))
        fault = next(f for f in found if f.kind == "row-column-count")
        self.assertIn("Wisps", fault.subject)
        self.assertIn(fault, validate.blocking(found))

    def test_well_formed_tables_have_no_column_fault(self):
        path = self._registry(["| Alpha | ~/dev/Alpha | main | on | on |  | alpha |  |  | active |"])
        found = validate.faults(path, self._machines(), self._tombstones([]))
        self.assertNotIn("row-column-count", [f.kind for f in found])


class TestUnpartitionedRows(ValidateFixture):
    def test_a_later_table_with_the_wrong_shape_is_unclaimed_by_either_parser(self):
        row = "| Alpha | ~/dev/Alpha | main | on | on |  | alpha |  |  | active |"
        text = "\n".join([REGISTRY_HEADER, REGISTRY_SEP, row, "",
                          "## Something else", "",
                          "| Foo | Bar |", "|-----|-----|",
                          "| x | y |"]) + "\n"
        path = self._write("registry.md", text)
        found = validate.faults(path, self._machines(), self._tombstones([]))
        fault = next(f for f in found if f.kind == "unpartitioned-row")
        self.assertIn("x", fault.subject)
        self.assertIn(fault, validate.blocking(found))

    def test_a_row_shaped_line_with_no_header_above_it_is_unclaimed(self):
        row = "| Alpha | ~/dev/Alpha | main | on | on |  | alpha |  |  | active |"
        text = "| orphan | row |\n" + "\n".join([REGISTRY_HEADER, REGISTRY_SEP, row]) + "\n"
        path = self._write("registry.md", text)
        found = validate.faults(path, self._machines(), self._tombstones([]))
        fault = next(f for f in found if f.kind == "unpartitioned-row")
        self.assertIn("orphan", fault.subject)

    def test_the_real_machine_paths_table_is_fully_claimed(self):
        row = "| Alpha | ~/dev/Alpha | main | on | on |  | alpha |  |  | active |"
        text = "\n".join([REGISTRY_HEADER, REGISTRY_SEP, row, "", "## Machine paths", "",
                          "| Repo | Machine | Path |", "|---|---|---|",
                          "| Alpha | home | ~/elsewhere |"]) + "\n"
        path = self._write("registry.md", text)
        found = validate.faults(path, self._machines(), self._tombstones([]))
        self.assertNotIn("unpartitioned-row", [f.kind for f in found])

    def test_the_real_registry_has_no_unpartitioned_rows(self):
        found = validate.faults()
        self.assertEqual([f for f in found if f.kind == "unpartitioned-row"], [])


class TestDuplicateId(ValidateFixture):
    def test_an_id_on_two_rows_is_reported(self):
        registry_path = self._registry([
            "| Alpha | ~/dev/Alpha | main | on | on |  | dup |  |  | active |",
            "| AlphaTwo | ~/dev/AlphaTwo | main | on | on |  | dup |  |  | active |",
        ])
        found = validate.faults(registry_path, self._machines(), self._tombstones([]))
        kinds = [f.kind for f in found]
        self.assertIn("duplicate-id", kinds)
        fault = next(f for f in found if f.kind == "duplicate-id")
        self.assertEqual(fault.subject, "dup")
        self.assertIn("Alpha", fault.detail)
        self.assertIn("AlphaTwo", fault.detail)


class TestCollidingPath(ValidateFixture):
    def test_two_rows_claiming_one_path_is_reported(self):
        shared = os.path.join(self.tmp, "shared-repo")
        registry_path = self._registry([
            "| Alpha | %s | main | on | on |  | alpha |  |  | active |" % shared,
            "| Beta | %s | main | on | on |  | beta |  |  | active |" % shared,
        ])
        found = validate.faults(registry_path, self._machines(), self._tombstones([]))
        fault = next(f for f in found if f.kind == "colliding-path")
        self.assertIn(os.path.realpath(shared), fault.subject)
        self.assertIn("Alpha", fault.detail)
        self.assertIn("Beta", fault.detail)


class TestActiveButRetired(ValidateFixture):
    """The semantic-resurrection case: the merge scenario this module exists for."""

    def test_a_row_pasted_back_as_active_over_a_tombstoned_id_is_reported(self):
        # Mirrors the real merge: one peer retired `alpha`, another (unaware of the
        # retirement) kept a still-active row for the same id, under a NEW name --
        # the row that survives a clean merge and would silently re-permit the repo.
        registry_path = self._registry([
            "| AlphaRenamed | ~/dev/AlphaRenamed | main | on | on |  | alpha |  |  | active |",
        ])
        tombstones_path = self._tombstones([
            "| alpha | 2026-09-29T09:00-07:00 | retired, superseded elsewhere |  |",
        ])
        found = validate.faults(registry_path, self._machines(), tombstones_path)
        fault = next(f for f in found if f.kind == "active-but-retired")
        self.assertEqual(fault.subject, "alpha")
        self.assertIn("AlphaRenamed", fault.detail)

    def test_a_retired_row_with_no_matching_id_is_not_reported(self):
        registry_path = self._registry([
            "| Beta | ~/dev/Beta | main | on | on |  | beta |  |  | active |",
        ])
        tombstones_path = self._tombstones([
            "| alpha | 2026-09-29T09:00-07:00 | unrelated retirement |  |",
        ])
        found = validate.faults(registry_path, self._machines(), tombstones_path)
        self.assertEqual([f for f in found if f.kind == "active-but-retired"], [])


class TestUnknownState(ValidateFixture):
    def test_a_state_that_is_neither_active_nor_retired_is_reported(self):
        registry_path = self._registry([
            "| Alpha | ~/dev/Alpha | main | on | on |  | alpha |  |  | vacationing |",
        ])
        found = validate.faults(registry_path, self._machines(), self._tombstones([]))
        fault = next(f for f in found if f.kind == "unknown-state")
        self.assertEqual(fault.subject, "alpha")
        self.assertIn("vacationing", fault.detail)


class TestSupersedeCycle(ValidateFixture):
    def test_a_two_node_cycle_is_reported_once(self):
        tombstones_path = self._tombstones([
            "| cyca | 2026-09-20T10:00-07:00 | reason a | cycb |",
            "| cycb | 2026-09-20T10:05-07:00 | reason b | cyca |",
        ])
        found = validate.faults(self._registry([]), self._machines(), tombstones_path)
        cycle_faults = [f for f in found if f.kind == "supersede-cycle"]
        self.assertEqual(len(cycle_faults), 1)
        self.assertEqual(cycle_faults[0].subject, "cyca,cycb")


class TestSupersedeUnknown(ValidateFixture):
    def test_superseded_by_naming_nothing_anywhere_is_reported(self):
        tombstones_path = self._tombstones([
            "| ghostref | 2026-09-20T10:00-07:00 | folded away | ghostly |",
        ])
        found = validate.faults(self._registry([]), self._machines(), tombstones_path)
        fault = next(f for f in found if f.kind == "supersede-unknown")
        self.assertEqual(fault.subject, "ghostly")
        self.assertIn("ghostref", fault.detail)

    def test_superseded_by_naming_a_live_registry_id_is_not_reported(self):
        registry_path = self._registry([
            "| Beta | ~/dev/Beta | main | on | on |  | beta |  |  | active |",
        ])
        tombstones_path = self._tombstones([
            "| alpha | 2026-09-20T10:00-07:00 | folded into beta | beta |",
        ])
        found = validate.faults(registry_path, self._machines(), tombstones_path)
        self.assertEqual([f for f in found if f.kind == "supersede-unknown"], [])

    def test_superseded_by_naming_another_tombstone_is_not_reported(self):
        tombstones_path = self._tombstones([
            "| alpha | 2026-09-20T10:00-07:00 | folded into gamma | gamma |",
            "| gamma | 2026-09-15T08:00-07:00 | retired earlier |  |",
        ])
        found = validate.faults(self._registry([]), self._machines(), tombstones_path)
        self.assertEqual([f for f in found if f.kind == "supersede-unknown"], [])


class TestUnknownMachine(ValidateFixture):
    def test_a_token_naming_no_key_and_no_label_is_reported(self):
        registry_path = self._registry([
            "| Alpha | ~/dev/Alpha | main | on | on | marsrover | alpha |  |  | active |",
        ])
        found = validate.faults(registry_path, self._machines(), self._tombstones([]))
        fault = next(f for f in found if f.kind == "unknown-machine")
        self.assertEqual(fault.subject, "marsrover")
        self.assertIn("Alpha", fault.detail)

    def test_a_role_is_not_reported(self):
        """A role is the registry column's native vocabulary."""
        registry_path = self._registry([
            "| Alpha | ~/dev/Alpha | main | on | on | desktop | alpha |  |  | active |",
        ])
        found = validate.faults(registry_path, self._machines(), self._tombstones([]))
        self.assertEqual([f for f in found if f.kind == "unknown-machine"], [])

    def test_a_LABEL_IS_reported_because_it_matches_no_machine(self):
        """Reversed 2026-09-30. This test previously asserted the opposite and was wrong.

        `registry.on_machine` matches a row against (role, key) only. Measured against the
        real fleet, a row scoped to the label `mac` is carried by NO machine -- not the
        Desktop, not the Laptop, not the home box -- so it is a rule that silently does
        nothing, which is precisely what this fault kind exists to surface. The label
        vocabulary belongs to `claude/skills.md`, a different column with the same name;
        conflating the two is the trap LESSONS.md records.
        """
        registry_path = self._registry([
            "| Alpha | ~/dev/Alpha | main | on | on | mac | alpha |  |  | active |",
        ])
        found = validate.faults(registry_path, self._machines(), self._tombstones([]))
        fault = next(f for f in found if f.kind == "unknown-machine")
        self.assertEqual(fault.subject, "mac")
        self.assertIn("never labels", fault.detail)


class TestNameRemoteMismatchApprovedRehome(ValidateFixture):
    """Ticket VALIDATE_CORRECT_DEV_SUFFIX_FALSE_FAULTS: an approved `rehome` plan is evidence
    that a row's private remote deliberately diverging from its Name/Path is a recorded human
    decision, not a half-applied rename -- see `_approved_rehome_remotes`."""

    REMOTE = "git@github.com:Lmandrake/Lodestar-dev.git"

    def _approved_plan_row(self, repo_id="lodestar", new=REMOTE):
        plan = plans_lib.Plan(
            plan_id="rehome-%s-20261028" % repo_id,
            kind="rehome",
            repo_id=repo_id,
            machines=("all",),
            old="",
            new=new,
            disposition="",
            created_at="2026-10-28T14:17:58-07:00",
            created_by="mt-123456",
            note="Public release 2026-10-28: private working repo renamed; mirror takes plain name.",
        )
        fingerprint = plans_lib.fingerprint(plan)
        plan_row = "| %s | rehome | %s | all |  | %s |  | 2026-10-28T14:17:58-07:00 | mt-123456 | note |" % (
            plan.plan_id, repo_id, new)
        approval_row = "| %s | %s | mt-123456 | 2026-10-28T14:18:04-07:00 | Lukas |" % (
            plan.plan_id, fingerprint)
        return plan_row, approval_row, fingerprint

    def test_a_row_matching_an_approved_plan_remote_produces_no_fault(self):
        plan_row, approval_row, _ = self._approved_plan_row()
        registry_path = self._registry([
            "| Lodestar | ~/dev/Lodestar | main | on | on |  | lodestar |  | %s | active |"
            % self.REMOTE,
        ])
        found = validate.faults(
            registry_path, self._machines(), self._tombstones([]),
            self._plans([plan_row]), self._approvals([approval_row]))
        self.assertEqual(
            [f for f in found if f.kind in ("name-remote-mismatch", "path-remote-mismatch")], [])

    def test_a_mismatch_with_no_plan_at_all_still_faults_both_kinds(self):
        registry_path = self._registry([
            "| Lodestar | ~/dev/Lodestar | main | on | on |  | lodestar |  | %s | active |"
            % self.REMOTE,
        ])
        found = validate.faults(
            registry_path, self._machines(), self._tombstones([]),
            self._plans([]), self._approvals([]))
        kinds = sorted(f.kind for f in found
                        if f.kind in ("name-remote-mismatch", "path-remote-mismatch"))
        self.assertEqual(kinds, ["name-remote-mismatch", "path-remote-mismatch"])

    def test_a_plan_that_exists_but_is_not_approved_still_faults_both_kinds(self):
        plan_row, _approval_row, _ = self._approved_plan_row()
        registry_path = self._registry([
            "| Lodestar | ~/dev/Lodestar | main | on | on |  | lodestar |  | %s | active |"
            % self.REMOTE,
        ])
        found = validate.faults(
            registry_path, self._machines(), self._tombstones([]),
            self._plans([plan_row]), self._approvals([]))
        kinds = sorted(f.kind for f in found
                        if f.kind in ("name-remote-mismatch", "path-remote-mismatch"))
        self.assertEqual(kinds, ["name-remote-mismatch", "path-remote-mismatch"])

    def test_declared_remote_in_a_different_form_than_the_row_still_counts_as_a_match(self):
        """The plan declares the ssh form; the row carries the https form of the SAME repo --
        `skillclone.normalize_remote` is what makes those compare equal, never string equality."""
        https_remote = "https://github.com/Lmandrake/Lodestar-dev.git"
        plan_row, approval_row, _ = self._approved_plan_row(new=self.REMOTE)
        registry_path = self._registry([
            "| Lodestar | ~/dev/Lodestar | main | on | on |  | lodestar |  | %s | active |"
            % https_remote,
        ])
        found = validate.faults(
            registry_path, self._machines(), self._tombstones([]),
            self._plans([plan_row]), self._approvals([approval_row]))
        self.assertEqual(
            [f for f in found if f.kind in ("name-remote-mismatch", "path-remote-mismatch")], [])

    def test_a_remote_that_does_not_match_the_approved_plan_still_faults(self):
        """An approval is evidence for ONE specific remote, never a blanket allowance for the
        id -- a row whose remote diverges from what was actually approved must still fault."""
        plan_row, approval_row, _ = self._approved_plan_row(new=self.REMOTE)
        wrong_remote = "git@github.com:Lmandrake/SomeOtherRepo-dev.git"
        registry_path = self._registry([
            "| Lodestar | ~/dev/Lodestar | main | on | on |  | lodestar |  | %s | active |"
            % wrong_remote,
        ])
        found = validate.faults(
            registry_path, self._machines(), self._tombstones([]),
            self._plans([plan_row]), self._approvals([approval_row]))
        kinds = sorted(f.kind for f in found
                        if f.kind in ("name-remote-mismatch", "path-remote-mismatch"))
        self.assertEqual(kinds, ["name-remote-mismatch", "path-remote-mismatch"])


class TestMissingId(ValidateFixture):
    def test_a_blank_id_is_reported_but_not_blocking(self):
        registry_path = self._registry([
            "| Alpha | ~/dev/Alpha | main | on | on |  |  |  |  | active |",
        ])
        found = validate.faults(registry_path, self._machines(), self._tombstones([]))
        fault = next(f for f in found if f.kind == "missing-id")
        self.assertEqual(fault.subject, "Alpha")
        self.assertEqual(validate.blocking(found), [])


class TestBlocking(unittest.TestCase):
    def test_missing_id_is_excluded_and_everything_else_is_blocking(self):
        found = [
            validate.Fault("missing-id", "Alpha", "no id"),
            validate.Fault("duplicate-id", "dup", "dup"),
            validate.Fault("colliding-path", "/x", "collide"),
            validate.Fault("active-but-retired", "alpha", "resurrected"),
            validate.Fault("unknown-state", "beta", "weird state"),
            validate.Fault("supersede-cycle", "a,b", "cycle"),
            validate.Fault("supersede-unknown", "ghost", "dangling"),
            validate.Fault("unknown-machine", "marsrover", "unknown token"),
        ]
        result = validate.blocking(found)
        self.assertNotIn("missing-id", [f.kind for f in result])
        self.assertEqual(len(result), len(found) - 1)


class TestStableOrdering(ValidateFixture):
    def test_repeated_runs_over_the_same_merged_state_agree_byte_for_byte(self):
        registry_path = self._registry([
            "| Alpha | ~/dev/Alpha | main | on | on | marsrover | dup |  |  | active |",
            "| AlphaTwo | ~/dev/AlphaTwo | main | on | on |  | dup |  |  | active |",
            "| Gamma | ~/dev/Gamma | main | on | on |  |  |  |  | vacationing |",
        ])
        tombstones_path = self._tombstones([
            "| dup | 2026-09-20T10:00-07:00 | retired elsewhere |  |",
            "| cyca | 2026-09-20T10:05-07:00 | reason a | cycb |",
            "| cycb | 2026-09-20T10:06-07:00 | reason b | cyca |",
            "| stray | 2026-09-20T10:07-07:00 | dangling | nowhere |",
        ])
        machines_path = self._machines()

        first = validate.faults(registry_path, machines_path, tombstones_path)
        second = validate.faults(registry_path, machines_path, tombstones_path)

        self.assertEqual(first, second)
        self.assertGreater(len(first), 3)  # several distinct fault kinds present
        keys = [(f.kind, f.subject) for f in first]
        self.assertEqual(keys, sorted(keys))


@needs_private("sync/registry.md")
class RegistryMachineVocabularyTest(unittest.TestCase):
    """The registry's `Machines` column takes ROLES and KEYS -- never labels.

    This class exists because the first real run of the validator reported all the
    work-internal rows as `unknown-machine`, purely because the check reused
    `machine.known_selector_tokens` (keys + LABELS) for a column whose vocabulary is
    keys + ROLES. Ten blocking false positives on every sweep, from reusing the wrong one
    of two identically-named columns -- the trap LESSONS.md already records.

    The authority is `registry.on_machine`, which matches a row against (role, key) and
    never against a label.
    """

    def test_the_real_registry_and_machines_file_have_no_unknown_machine_faults(self):
        """The regression test proper: the shipping files must validate clean.

        A fixture could not have caught the original bug, because the bug was about which
        real vocabulary the real column uses.
        """
        found = validate.faults()
        offenders = [f for f in found if f.kind == "unknown-machine"]
        self.assertEqual(
            offenders, [],
            "the real sync/registry.md must not report unknown machine tokens; got: %s"
            % [(f.subject, f.detail) for f in offenders])

    def test_a_role_token_is_accepted(self):
        tokens = validate._registry_machine_tokens(None)
        self.assertIn("desktop", tokens)
        self.assertIn("laptop", tokens)
        self.assertIn("home", tokens)

    def test_a_machine_key_is_also_accepted(self):
        # on_machine matches role OR key, so both must validate.
        tokens = validate._registry_machine_tokens(None)
        self.assertIn(real_key("desktop"), tokens)
        self.assertIn(real_home_key(), tokens)

    def test_a_label_is_NOT_a_valid_registry_token(self):
        """The discriminating assertion. `mac` and `work` are labels, valid only in
        claude/skills.md. If they leak into this vocabulary the two columns have been
        conflated again."""
        tokens = validate._registry_machine_tokens(None)
        self.assertNotIn("mac", tokens)
        self.assertNotIn("work", tokens)
        self.assertNotIn("windows-wsl", tokens)

    def test_a_genuinely_bogus_token_is_still_reported(self):
        """The check must not have been neutered into accepting everything."""
        tokens = validate._registry_machine_tokens(None)
        self.assertNotIn("nosuchmachine", tokens)


if __name__ == "__main__":
    unittest.main()


class TestDeadScope(unittest.TestCase):
    """A scoped beacon whose scope names no priority/routing.md row can never be a
    candidate — a silently dead beacon. Reported, non-blocking (docs/bearing/SKILL.md)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.items = os.path.join(self.dir, "items.md")
        self.routing = os.path.join(self.dir, "routing.md")
        with open(self.routing, "w") as f:
            f.write("| project | dir | keywords | senders |\n|---|---|---|---|\n"
                    "| PILLAR | ~/dev/PILLAR | pillar | |\n")
        from lib import priority
        priority.append("ok", source="manual", sticky=True, scope="PILLAR", path=self.items)
        priority.append("dead", source="manual", sticky=True, scope="Nope", path=self.items)
        priority.append("global", source="manual", sticky=True, path=self.items)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_only_the_unknown_scope_is_reported_and_not_blocking(self):
        found = validate.dead_scopes(self.items, self.routing)
        self.assertEqual([f.subject for f in found], ["Nope"])
        self.assertEqual(found[0].kind, "dead-scope")
        self.assertEqual(validate.blocking(found), [])
