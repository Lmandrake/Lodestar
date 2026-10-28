"""Two report-only checks over the registry's declared `Remote` column.

Both exist because of one measured gap. `sync/registry.md` declares a `Remote` per row, and
until 2026-09-30 **nothing in the fleet ever compared a checkout against it**: SYNC.md's
"Push/fetch endpoint mismatches" section compares a checkout's push URL against its own fetch
URL, so a checkout internally consistent but pointing at a repo the registry never declared
passes it cleanly. It read "None found" on both Macs while the Laptop's `~/dev/DispatchOpenCode`
had `origin` = `git@github.com:Lmandrake/OpenCodeCLI.git` against a registry declaring
`.../DispatchOpenCode.git`.

- `sync.declared_remote_audit` is the per-machine observation: what each present checkout really
  points at, against what the registry declares.
- `validate._name_remote_mismatch` is the machine-independent invariant, from the owner's ruling
  of 2026-09-30: "Anything lodestar managed should have a matching dir and repo name, period."

Neither gates anything, and several tests below exist specifically to pin that: a section's
presence in SYNC.md must never be readable as enforcement (CLAUDE.md §7).

The audit tests use real `git init` repos with real `git remote add`, never a stub, because the
module they exercise reads git through `gitutil.remote_endpoints` — the defect class CLAUDE.md
names in capitals is a subsystem whose tests never drive the thing that calls git.
"""

import ast
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest

import sync as sync_cmd
from lib import registry, validate

REGISTRY_HEADER = ("| Name | Path | Branch | Send | Receive | Machines | Id | Former "
                   "| Remote | State |")
REGISTRY_SEP = "|------|------|--------|------|---------|----------|----|--------|--------|----|"

MACHINES = """| Key | Role | Labels |
|-----|------|--------|
| test-box | testbox | mac |
| other-box | otherbox | mac |
"""


def git(repo, *args):
    subprocess.run(["git", "-C", repo] + list(args), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class AuditFixture(unittest.TestCase):
    """A dev root of real git checkouts, each with whatever origin the test wants."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-declared-remotes-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def checkout(self, name, origin=None, push_url=None):
        """A real git repo at <tmp>/<name>, optionally with an origin (and a separate pushurl)."""
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        git(path, "init", "-q")
        if origin:
            git(path, "remote", "add", "origin", origin)
        if push_url:
            git(path, "remote", "set-url", "--push", "origin", push_url)
        return path

    def row(self, name, declared, path=None):
        """A registry Entry. `path` defaults to <tmp>/<name>, present or not."""
        return registry.Entry(
            name=name, path=path if path is not None else os.path.join(self.tmp, name),
            branch="main", sync=True, notes="", remote=declared, repo_id=name.lower())


class TestAgreement(AuditFixture):
    def test_an_origin_matching_the_declaration_agrees(self):
        self.checkout("Alpha", "git@github.com:Lmandrake/Alpha.git")
        audit = sync_cmd.declared_remote_audit(
            [self.row("Alpha", "git@github.com:Lmandrake/Alpha.git")])
        self.assertEqual(["Alpha"], audit.agreeing)
        self.assertEqual([], audit.diverging)
        self.assertEqual(1, audit.examined)

    def test_ssh_checkout_against_an_https_declaration_is_NOT_a_finding(self):
        """The load-bearing tolerance. 10 live rows declare `https://` while the Macs hold them
        as `git@` — string equality here would report every one of them and bury the real
        finding, exactly as `push_url_mismatches`' docstring already argues for its own compare.
        """
        self.checkout("Custos", "git@github.com:Lmandrake/Custos.git")
        audit = sync_cmd.declared_remote_audit(
            [self.row("Custos", "https://github.com/Lmandrake/Custos.git")])
        self.assertEqual(["Custos"], audit.agreeing)
        self.assertEqual([], audit.diverging)

    def test_a_case_difference_in_the_owner_is_not_a_finding(self):
        self.checkout("Alpha", "git@github.com:lmandrake/alpha.git")
        audit = sync_cmd.declared_remote_audit(
            [self.row("Alpha", "git@github.com:Lmandrake/Alpha.git")])
        self.assertEqual(["Alpha"], audit.agreeing)


class TestDivergence(AuditFixture):
    def test_the_real_DispatchOpenCode_shape_is_caught(self):
        """The measured gap, reproduced: a checkout whose origin names a DIFFERENT repository
        than the registry declares. This is what `push_url_mismatches` cannot see, because the
        checkout's push and fetch URLs agree with each other perfectly.
        """
        self.checkout("DispatchOpenCode", "git@github.com:Lmandrake/OpenCodeCLI.git")
        row = self.row("DispatchOpenCode", "git@github.com:Lmandrake/DispatchOpenCode.git")
        audit = sync_cmd.declared_remote_audit([row])

        self.assertEqual([], audit.agreeing)
        self.assertEqual(1, len(audit.diverging))
        name, declared, fetch_url, differing = audit.diverging[0]
        self.assertEqual("DispatchOpenCode", name)
        self.assertEqual("git@github.com:Lmandrake/DispatchOpenCode.git", declared)
        self.assertEqual("git@github.com:Lmandrake/OpenCodeCLI.git", fetch_url)
        self.assertEqual(["git@github.com:Lmandrake/OpenCodeCLI.git"], differing)

    def test_that_same_shape_passes_the_pre_existing_push_check(self):
        """Why a second section was needed at all. Asserted on the same fixture so the two
        checks cannot be confused for one: the old check truthfully finds nothing here.
        """
        self.checkout("DispatchOpenCode", "git@github.com:Lmandrake/OpenCodeCLI.git")
        row = self.row("DispatchOpenCode", "git@github.com:Lmandrake/DispatchOpenCode.git")
        self.assertEqual([], sync_cmd.push_url_mismatches([row]))
        self.assertEqual(1, len(sync_cmd.declared_remote_audit([row]).diverging))

    def test_a_divergent_PUSH_url_is_caught_while_fetch_agrees(self):
        """`git remote set-url origin <new>` leaves a separately-configured pushurl behind, so a
        half-rehomed repo fetches from the declared place and pushes to the old one.
        """
        self.checkout("Beta", "git@github.com:Lmandrake/Beta.git",
                      push_url="git@github.com:Lmandrake/OldBeta.git")
        audit = sync_cmd.declared_remote_audit(
            [self.row("Beta", "git@github.com:Lmandrake/Beta.git")])
        self.assertEqual(1, len(audit.diverging))
        _, _, fetch_url, differing = audit.diverging[0]
        self.assertEqual("git@github.com:Lmandrake/Beta.git", fetch_url)
        self.assertEqual(["git@github.com:Lmandrake/OldBeta.git"], differing)

    def test_a_different_HOST_diverges(self):
        """github.com vs git.private.example for the same repo name is the confidentiality-
        relevant case, and `normalize_remote` keeps the host, so identity comparison sees it."""
        self.checkout("Mail", "git@github.com:Lmandrake/Mail.git")
        audit = sync_cmd.declared_remote_audit(
            [self.row("Mail", "git@git.private.example:mandrake/Mail.git")])
        self.assertEqual(1, len(audit.diverging))


class TestTheOtherBuckets(AuditFixture):
    def test_a_row_with_no_declared_remote_is_reported_with_its_observed_origin(self):
        self.checkout("orgtool", "git@github.com:Lmandrake/orgtool.git")
        audit = sync_cmd.declared_remote_audit([self.row("orgtool", "")])
        self.assertEqual([("orgtool", "git@github.com:Lmandrake/orgtool.git")],
                         audit.undeclared)
        self.assertEqual([], audit.agreeing)
        self.assertEqual([], audit.diverging)

    def test_a_row_with_neither_a_declaration_nor_an_origin_still_reports(self):
        self.checkout("cocaffeinate")
        audit = sync_cmd.declared_remote_audit([self.row("cocaffeinate", "")])
        self.assertEqual([("cocaffeinate", "")], audit.undeclared)

    def test_a_checkout_with_no_origin_is_reported_separately(self):
        self.checkout("Gamma")  # a real git repo, no remotes at all
        audit = sync_cmd.declared_remote_audit(
            [self.row("Gamma", "git@github.com:Lmandrake/Gamma.git")])
        self.assertEqual([("Gamma", "git@github.com:Lmandrake/Gamma.git")], audit.no_origin)
        self.assertEqual([], audit.agreeing)

    def test_a_directory_that_is_not_a_git_repo_reports_as_no_origin(self):
        os.makedirs(os.path.join(self.tmp, "Delta"))
        audit = sync_cmd.declared_remote_audit(
            [self.row("Delta", "git@github.com:Lmandrake/Delta.git")])
        self.assertEqual([("Delta", "git@github.com:Lmandrake/Delta.git")], audit.no_origin)

    def test_an_absent_checkout_is_skipped_and_not_examined(self):
        """A row carried here but not yet cloned is not a finding — the audit observes, and
        there is nothing to observe. It must not be silently dropped either, or `examined` would
        read as a census of the registry."""
        audit = sync_cmd.declared_remote_audit(
            [self.row("NotCloned", "git@github.com:Lmandrake/NotCloned.git")])
        self.assertEqual(["NotCloned"], audit.skipped)
        self.assertEqual(0, audit.examined)
        self.assertEqual([], audit.agreeing)
        self.assertEqual([], audit.diverging)
        self.assertEqual([], audit.no_origin)

    def test_the_buckets_are_disjoint_and_examined_is_their_sum(self):
        self.checkout("Alpha", "git@github.com:Lmandrake/Alpha.git")
        self.checkout("Beta", "git@github.com:Lmandrake/Wrong.git")
        self.checkout("Gamma")
        self.checkout("Delta", "git@github.com:Lmandrake/Delta.git")
        audit = sync_cmd.declared_remote_audit([
            self.row("Alpha", "git@github.com:Lmandrake/Alpha.git"),
            self.row("Beta", "git@github.com:Lmandrake/Beta.git"),
            self.row("Gamma", "git@github.com:Lmandrake/Gamma.git"),
            self.row("Delta", ""),
            self.row("Epsilon", "git@github.com:Lmandrake/Epsilon.git"),
        ])
        self.assertEqual(1, len(audit.agreeing))
        self.assertEqual(1, len(audit.diverging))
        self.assertEqual(1, len(audit.no_origin))
        self.assertEqual(1, len(audit.undeclared))
        self.assertEqual(1, len(audit.skipped))
        self.assertEqual(4, audit.examined)


class TestTheAuditCannotMutateAnything(AuditFixture):
    """Report-only is a property of the code, not a sentence in a docstring."""

    def _function_source(self, name):
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "bin", "sync.py"), "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        self.fail("sync.%s not found" % name)

    def test_the_only_git_it_runs_is_the_read(self):
        """`gitutil.remote_endpoints` is two `git remote get-url` reads. If any other gitutil
        name ever appears in this function it can write, and the section's "gates nothing"
        promise stops being structural."""
        node = self._function_source("declared_remote_audit")
        called = set()
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                if isinstance(inner.func.value, ast.Name):
                    called.add("%s.%s" % (inner.func.value.id, inner.func.attr))
        gitcalls = sorted(name for name in called if name.startswith("gitutil."))
        self.assertEqual(["gitutil.remote_endpoints"], gitcalls)

    def test_it_opens_no_file_and_writes_no_path(self):
        node = self._function_source("declared_remote_audit")
        names = {inner.id for inner in ast.walk(node) if isinstance(inner, ast.Name)}
        attrs = {inner.attr for inner in ast.walk(node) if isinstance(inner, ast.Attribute)}
        for forbidden in ("open", "write", "replace", "rename", "makedirs", "unlink",
                          "rmtree", "set_url"):
            self.assertNotIn(forbidden, names)
            self.assertNotIn(forbidden, attrs)


class TestTheSectionReportsAndGatesNothing(AuditFixture):
    def _section(self, audit):
        text = sync_cmd.report([], "testbox", True, [], [], None, [], [], None, audit)
        self.assertIn("## Declared vs. actual remotes", text)
        body = text.split("## Declared vs. actual remotes", 1)[1]
        # Up to the next heading, so an assertion cannot accidentally read a neighbour section.
        return body.split("\n## ", 1)[0]

    def _clean(self):
        self.checkout("Alpha", "git@github.com:Lmandrake/Alpha.git")
        return sync_cmd.declared_remote_audit(
            [self.row("Alpha", "git@github.com:Lmandrake/Alpha.git")])

    def test_the_section_says_in_its_own_text_that_it_gates_nothing(self):
        section = self._section(self._clean())
        self.assertIn("gates nothing", section)
        self.assertIn("Phase 3", section)

    def test_the_section_is_rendered_even_when_entirely_clean(self):
        """An absent section reads as "not checked", which is the failure this whole pair of
        checks exists to end — the old section's honest "None found" was read as "no drift
        anywhere" while drift sat on the Laptop."""
        section = self._section(self._clean())
        self.assertIn("No divergence", section)
        self.assertIn("examined", section)

    def test_a_divergence_is_named_with_both_urls(self):
        self.checkout("DispatchOpenCode", "git@github.com:Lmandrake/OpenCodeCLI.git")
        audit = sync_cmd.declared_remote_audit(
            [self.row("DispatchOpenCode", "git@github.com:Lmandrake/DispatchOpenCode.git")])
        section = self._section(audit)
        self.assertIn("DispatchOpenCode", section)
        self.assertIn("OpenCodeCLI.git", section)
        self.assertIn("1 checkout(s) point somewhere the registry does not declare", section)

    def test_the_section_survives_an_audit_that_raised(self):
        audit = sync_cmd.RemoteAudit([], [], [], [], [], "audit failed, checked nothing: boom")
        section = self._section(audit)
        self.assertIn("checked nothing", section)
        self.assertIn("read nothing below as a clean result", section)

    def test_a_divergence_changes_no_repo_outcome_and_adds_no_repo_row(self):
        """The audit is not allowed to invent a repo. A synthetic Result would enter the
        All-repos table, the repo count and the flagged count, so a *reporter* would change what
        the sweep says about the fleet."""
        self.checkout("DispatchOpenCode", "git@github.com:Lmandrake/OpenCodeCLI.git")
        audit = sync_cmd.declared_remote_audit(
            [self.row("DispatchOpenCode", "git@github.com:Lmandrake/DispatchOpenCode.git")])
        with_audit = sync_cmd.report([], "testbox", True, [], [], None, [], [], None, audit)
        without = sync_cmd.report([], "testbox", True, [], [], None, [], [], None, None)
        self.assertNotIn("## Declared vs. actual remotes", without)
        # Every line the two share outside the new section is byte-identical.
        tail = with_audit.split("## Declared vs. actual remotes", 1)[1].split("\n## ", 1)[1]
        self.assertTrue(without.endswith(tail))

    def test_main_never_offers_a_way_to_act_on_this(self):
        """No flag, no auto-repair. Comments stripped first: a test can pass on a comment, and
        this file's own module docstring names that trap."""
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "bin", "sync.py")
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        stripped = ast.unparse(ast.parse(source)) if hasattr(ast, "unparse") else source
        for forbidden in ("--fix-remotes", "--rehome", "--repair-remotes"):
            self.assertNotIn(forbidden, stripped)


class ValidateFixture(unittest.TestCase):
    """Fixtures for `validate.faults`, pointed at a temp board so a bug cannot read the real one."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-name-invariant-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._saved = {k: os.environ.get(k) for k in ("LODESTAR_ROOT", "LODESTAR_MACHINE")}
        os.environ["LODESTAR_ROOT"] = self.tmp
        os.environ["LODESTAR_MACHINE"] = "test-box"
        self.addCleanup(self._restore)
        self.machines_path = self._write("machines.md", MACHINES)

    def _restore(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def _registry(self, rows, extra=""):
        body = "\n".join([REGISTRY_HEADER, REGISTRY_SEP] + rows) + "\n" + extra
        return self._write("registry.md", body)

    def _faults(self, rows, extra="", kind=None):
        found = validate.faults(self._registry(rows, extra), self.machines_path,
                                self._write("tombstones.md",
                                            "| Id | At | Reason | Superseded by |\n"
                                            "|----|----|--------|----------------|\n"))
        if kind is not None:
            return [fault for fault in found if fault.kind == kind]
        return [fault for fault in found
                if fault.kind in ("name-remote-mismatch", "path-remote-mismatch")]


class TestTheNameInvariant(ValidateFixture):
    def test_a_matching_row_is_clean(self):
        self.assertEqual([], self._faults([
            "| Alpha | ~/dev/Alpha | main | on | on |  | alpha |  "
            "| git@github.com:Lmandrake/Alpha.git | active |",
        ]))

    def test_the_live_RimMaster_shape_flags_both_halves(self):
        """The shape that WAS live on the fleet: row `RimMaster`, remote `.../RimMandrake.git`.

        Both halves of the invariant fail, and each is reported under its own kind so a reader can
        tell "the row is misnamed" from "the directory is misnamed".

        🔴 **This is a historical fixture now — the live instance is gone.** The row was renamed to
        `RimMandrake` on 2026-09-30 and `validate.faults()` on the real registry returns 0. Do not
        read this docstring as evidence that a live example still exists to lean on; it is kept
        because the shape is the one the check was built for, not because the fleet still has it."""
        found = self._faults([
            "| RimMaster | ~/dev/RimMaster | main | on | on |  | rimmaster |  "
            "| git@github.com:Lmandrake/RimMandrake.git | active |",
        ])
        self.assertEqual(["name-remote-mismatch", "path-remote-mismatch"],
                         sorted(fault.kind for fault in found))
        for fault in found:
            self.assertEqual("rimmaster", fault.subject)
            self.assertIn("rimmandrake", fault.detail)
            self.assertIn("2026-09-30", fault.detail)

    def test_the_subject_is_the_Id_not_the_Name(self):
        """§7: a repo's identity is its Id. A fault keyed on the Name would move the moment
        someone renames the row, which is precisely the act being reported on."""
        found = self._faults([
            "| Wrong | ~/dev/Wrong | main | on | on |  | stable-id |  "
            "| git@github.com:Lmandrake/Right.git | active |",
        ])
        self.assertTrue(found)
        self.assertEqual({"stable-id"}, {fault.subject for fault in found})

    def test_the_Name_falls_back_as_subject_when_there_is_no_Id(self):
        found = self._faults([
            "| Wrong | ~/dev/Wrong | main | on | on |  |  |  "
            "| git@github.com:Lmandrake/Right.git | active |",
        ])
        self.assertEqual({"Wrong"}, {fault.subject for fault in found})

    def test_only_the_path_half_flags_when_the_directory_alone_differs(self):
        found = self._faults([
            "| Alpha | ~/dev/alpha-checkout | main | on | on |  | alpha |  "
            "| git@github.com:Lmandrake/Alpha.git | active |",
        ])
        self.assertEqual(["path-remote-mismatch"], [fault.kind for fault in found])
        self.assertIn("alpha-checkout", found[0].detail)

    def test_only_the_name_half_flags_when_the_row_name_alone_differs(self):
        found = self._faults([
            "| AlphaRow | ~/dev/Alpha | main | on | on |  | alpha |  "
            "| git@github.com:Lmandrake/Alpha.git | active |",
        ])
        self.assertEqual(["name-remote-mismatch"], [fault.kind for fault in found])

    def test_a_case_only_difference_is_tolerated(self):
        """`normalize_remote` lowercases, so this check is necessarily case-insensitive. GitHub
        treats the two as one repo, so a case finding would be unactionable noise."""
        self.assertEqual([], self._faults([
            "| measuring-large-artifacts | ~/dev/measuring-large-artifacts | main | on | on |  "
            "| mla |  | git@github.com:Lmandrake/Measuring-Large-Artifacts.git | active |",
        ]))

    def test_the_declared_url_form_does_not_matter(self):
        for url in ("git@github.com:Lmandrake/Alpha.git",
                    "https://github.com/Lmandrake/Alpha.git",
                    "https://github.com/Lmandrake/Alpha",
                    "ssh://git@github.com/Lmandrake/Alpha.git",
                    "git@git.private.example:mandrake/Alpha.git"):
            self.assertEqual([], self._faults([
                "| Alpha | ~/dev/Alpha | main | on | on |  | alpha |  | %s | active |" % url,
            ]), url)

    def test_a_row_with_no_declared_remote_is_not_a_name_fault(self):
        """Nothing to compare against. "This row declares no Remote" is a different fact; saying
        it here would attach a naming verdict to a row that has made no naming claim."""
        self.assertEqual([], self._faults([
            "| cocaffeinate | ~/dev/something-else | main | off | off |  | cocaffeinate |  "
            "|  | active |",
        ]))

    def test_a_retired_row_is_still_checked(self):
        """Deliberately unfiltered by State: a state filter here is a second place for a
        lifecycle rule to drift out of step with `_active_but_retired`, and the cost of the
        extra line is one non-blocking report row."""
        found = self._faults([
            "| Old | ~/dev/Old | main | off | off |  | old |  "
            "| git@github.com:Lmandrake/Older.git | retired |",
        ])
        self.assertEqual(2, len(found))

    def test_a_trailing_slash_on_the_path_does_not_invent_a_fault(self):
        self.assertEqual([], self._faults([
            "| Alpha | ~/dev/Alpha/ | main | on | on |  | alpha |  "
            "| git@github.com:Lmandrake/Alpha.git | active |",
        ]))


class TestItIsNonBlocking(ValidateFixture):
    def test_neither_kind_blocks(self):
        """Safety-relevant, and the reason survives its own example.

        When written there WAS a live instance (`RimMaster`), so a blocking verdict would have
        refused every sweep on every machine over a condition tolerated for months — the exact
        shape of the `_unknown_machine` false positive LESSONS.md records. That instance was
        resolved on 2026-09-30, so the argument now rests on the general case rather than a
        present fault: the next name/remote divergence will also be a long-tolerated condition
        discovered by this check, and refusing every sweep on discovery is still the wrong
        response."""
        found = self._faults([
            "| RimMaster | ~/dev/RimMaster | main | on | on |  | rimmaster |  "
            "| git@github.com:Lmandrake/RimMandrake.git | active |",
        ])
        self.assertEqual(2, len(found))
        self.assertEqual([], validate.blocking(found))

    def test_both_kinds_are_named_in_the_non_blocking_set(self):
        self.assertIn("name-remote-mismatch", validate._NON_BLOCKING)
        self.assertIn("path-remote-mismatch", validate._NON_BLOCKING)

    def test_a_genuine_contradiction_still_blocks(self):
        """The set must not have been widened into "nothing blocks"."""
        found = validate.faults(
            self._registry([
                "| Alpha | ~/dev/Alpha | main | on | on |  | dup |  "
                "| git@github.com:Lmandrake/Alpha.git | active |",
                "| Beta | ~/dev/Beta | main | on | on |  | dup |  "
                "| git@github.com:Lmandrake/Beta.git | active |",
            ]), self.machines_path, None)
        self.assertIn("duplicate-id", {fault.kind for fault in validate.blocking(found)})


class TestMachinePathOverridesAreNotDrift(ValidateFixture):
    """The fault list must be the same on every machine that merged the same commit — the
    property `validate.faults`' own docstring asserts. A Machine-paths override replaces one
    row's Path on one machine (`Alpha` is `~/dev/AlphaGame` on `home`, `beta-tool` is
    `~/dev/beta-tools`), so reading the resolved path would both make this module
    machine-dependent and report a human's declared choice as drift.
    """

    OVERRIDE = textwrap.dedent("""
        ## Machine paths

        | Repo | Machine | Path |
        |------|---------|------|
        | Alpha | testbox | ~/dev/alpha-somewhere-else |
        """)

    def test_an_override_for_THIS_machine_produces_no_fault(self):
        rows = ["| Alpha | ~/dev/Alpha | main | on | on |  | alpha |  "
                "| git@github.com:Lmandrake/Alpha.git | active |"]
        self.assertEqual([], self._faults(rows, extra=self.OVERRIDE))

    def test_the_override_really_is_in_effect_for_this_machine(self):
        """Guards against the test above passing vacuously because the override never applied.
        Without this, a typo in the Machine-paths fixture would look like a clean result."""
        path = self._registry(
            ["| Alpha | ~/dev/Alpha | main | on | on |  | alpha |  "
             "| git@github.com:Lmandrake/Alpha.git | active |"],
            extra=self.OVERRIDE)
        resolved = {entry.name: entry.path for entry in registry.entries(path)}
        self.assertTrue(resolved["Alpha"].endswith("alpha-somewhere-else"),
                        "override did not apply; the sibling test would be vacuous")
        declared = {entry.name: entry.path
                    for entry in registry.entries(path, role="", key="")}
        self.assertTrue(declared["Alpha"].endswith(os.path.join("dev", "Alpha")))

    def test_the_fault_list_is_identical_with_and_without_the_override(self):
        rows = ["| Alpha | ~/dev/Alpha | main | on | on |  | alpha |  "
                "| git@github.com:Lmandrake/Alpha.git | active |"]
        plain = self._faults(rows)
        overridden = self._faults(rows, extra=self.OVERRIDE)
        self.assertEqual(plain, overridden)


class TestRemoteRepoName(unittest.TestCase):
    def test_the_url_forms_a_registry_uses(self):
        cases = {
            "git@github.com:Lmandrake/DispatchOpenCode.git": "dispatchopencode",
            "https://github.com/Lmandrake/Custos.git": "custos",
            "git@git.private.example:owner/Archive.git": "archive",
            "https://git.private.example/owner/Beta.git": "beta",
            "git@github.com:Lmandrake/measuring-large-artifacts.git": (
                "measuring-large-artifacts"),
        }
        for url, expected in cases.items():
            self.assertEqual(expected, validate.remote_repo_name(url), url)

    def test_a_url_without_dot_git_still_parses(self):
        self.assertEqual("alpha",
                         validate.remote_repo_name("https://github.com/Lmandrake/Alpha"))

    def test_a_nested_group_path_takes_the_last_component(self):
        self.assertEqual("repo",
                         validate.remote_repo_name("git@gitlab.example:group/sub/Repo.git"))

    def test_an_empty_or_unparseable_url_degrades_to_empty_never_a_name(self):
        for url in ("", "   ", None):
            self.assertEqual("", validate.remote_repo_name(url))


class TestAgainstTheLiveRegistry(unittest.TestCase):
    """Run against the real board. Report-only, so these assert shape rather than cleanliness —
    a figure written into a test becomes a number a future session quotes, and the live registry
    changes under it."""

    def test_the_live_registry_produces_only_non_blocking_name_faults(self):
        found = [fault for fault in validate.faults()
                 if fault.kind in ("name-remote-mismatch", "path-remote-mismatch")]
        self.assertEqual([], validate.blocking(found))

    def test_every_live_name_fault_names_a_real_row(self):
        names = {entry.name for entry in registry.entries()}
        ids = {entry.repo_id for entry in registry.entries() if entry.repo_id}
        for fault in validate.faults():
            if fault.kind in ("name-remote-mismatch", "path-remote-mismatch"):
                self.assertTrue(fault.subject in ids or fault.subject in names,
                                "fault subject %r matches no live row" % fault.subject)


if __name__ == "__main__":
    unittest.main()


class AdvisoryFaultsAreRenderedTest(unittest.TestCase):
    """A non-blocking fault must still reach SYNC.md — "does not gate" is not "is not reported".

    The `## Desired-state faults` section printed only `validate.blocking(...)`, so both
    name-vs-remote checks fired in `validate.faults()` and appeared nowhere. SYNC.md is the only
    surface anyone reads, which makes that this repo's signature defect: a complete, tested
    subsystem whose production surface never shows it (CLAUDE.md §7). Measured on the live
    registry 2026-09-30 — 2 faults returned, section read "None found".
    """

    def _render(self, faults):
        import sync
        return sync.report([], "desktop", True, (), desired_state_faults=faults)

    def test_a_non_blocking_fault_appears_in_the_report(self):
        import sync
        from lib import validate
        fault = validate.Fault(kind="name-remote-mismatch", subject="RimMaster",
                               detail="row names a remote whose repository is 'rimmandrake'")
        self.assertIn("name-remote-mismatch", self._render([fault]),
                      "a non-blocking fault that reaches no surface is the same as no check")
        self.assertIn("rimmandrake", self._render([fault]))

    def test_the_section_distinguishes_blocking_from_advisory(self):
        text = self._render([])
        self.assertIn("None found.", text)
        self.assertIn("None advisory.", text)

    def test_missing_id_is_still_withheld_for_volume_even_though_it_is_advisory(self):
        """The pre-existing decision is honoured, not edited past.

        `missing-id` fired on every row before the schema migration; burying the real faults
        under ~50 rows of noise "is the difference between a check and an outage"
        (tests/test_lifecycle_sweep.py). It fires 0 times now, so this guards a return of that
        condition rather than filtering anything live.
        """
        import sync
        from lib import validate
        text = self._render([validate.Fault("missing-id", "Widget", "row has a blank Id")])
        self.assertNotIn("missing-id", text)
        self.assertIn("missing-id", sync.ADVISORY_VOLUME_SUPPRESSED)

    def test_the_advisory_wording_says_it_gates_nothing(self):
        """The §3D property: a section's presence must never read as a gate."""
        text = self._render([])
        self.assertIn("do **not** gate anything", text)
