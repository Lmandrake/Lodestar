"""A rehome must bring the registry's declared `Remote` with it — Lukas, 2026-10-01.

He chose this over the alternative of making the registry authoritative and refusing a rehome
that disagrees with it. The hazard it closes was measured end-to-end on this branch before a
line was changed, through `propose -> approve -> converge` against a throwaway `git init` repo:

    origin after     : git@git.private.example:mandrake/Thing.git
    registry Remote  : git@github.com:example/Thing.git          <- stale
    == rename, which runs _rename_remote_step ==
    converged
       remote: set origin to git@github.com:example/Thing.git     <- reverted, silently

Two individually correct steps composing into a rollback of a confidentiality decision, and the
rename reporting `converged` while it did it.

**Everything here is local and throwaway.** `LODESTAR_ROOT` points at a temp board with its own
`machines.md`, `sync/registry.md`, plans and approvals; repos are `git init` directories with
fake remote URLs; no network, no fetch, no push, and the live registry is never written.

🔴 **The end-to-end class is not a nicety.** CLAUDE.md §7 records that no fixture could have
caught the corrupted-URL bug in this exact path, because a fixture hands the URL straight to the
executor and never goes through `propose` — which is where `tilde()` was mangling it. So the
`propose -> approve -> converge` exercise is the only test shape that can see that whole class of
defect, and `EndToEndTest` keeps it under assertion.
"""

import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest

import lifecycle
from lib import journal, mdtable, paths, plans, registry
from tests import needs_private, real_key  # noqa: E402

PUBLIC = "git@github.com:example/Thing.git"
INTERNAL = "git@git.private.example:mandrake/Thing.git"

HEADER = ("| Name | Path | Branch | Send | Receive | Machines | Id | Former | Remote | State "
          "| Notes |\n"
          "|------|------|--------|------|---------|----------|----|--------|--------|-------"
          "|-------|\n")


def row(name="Thing", path="~/dev/Thing", repo_id="thing", remote=PUBLIC, notes="a note"):
    return ("| %s | %s | main | on | on |  | %s |  | %s | active | %s |\n"
            % (name, path, repo_id, remote, notes))


def git(repo, *args):
    subprocess.run(["git", "-C", repo] + list(args), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def plan(kind="rehome", **over):
    fields = dict(
        plan_id="exercise-%s-20261001" % kind,
        kind=kind,
        repo_id="thing",
        machines=("all",),
        old="",
        new="",
        disposition="",
        created_at="2026-10-01T09:00:00.000000-07:00",
        created_by="test",
        note="exercising the declaration step",
    )
    fields.update(over)
    return plans.Plan(**fields)


class BoardTestCase(unittest.TestCase):
    """A whole throwaway Lodestar board under `LODESTAR_ROOT`, plus machine-local redirects."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-rehome-decl-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.board = os.path.join(self.tmp, "board")
        os.makedirs(os.path.join(self.board, "sync"))
        os.makedirs(os.path.join(self.board, ".git"))
        # `board_freshness` reads `.git/FETCH_HEAD`'s mtime; a missing one is refused as
        # *unknown*, which would stop `converge` before any executor ran.
        open(os.path.join(self.board, ".git", "FETCH_HEAD"), "w").close()

        self._env("LODESTAR_ROOT", self.board)
        self._env("GIT_CONFIG_GLOBAL", os.devnull)  # global pushInsteadOf rewrites push URLs
        # 🔴 One knob rather than a list. This setUp previously patched the machine-local
        # stores one at a time, and on 2026-10-01 it missed `lifecycle_completions_file` --
        # which did not exist when these patches were written -- so a real `converge` wrote
        # fixture plans into the LIVE store on the Desktop and the committed fleet receipt then
        # claimed this machine had converged plans that never existed. Redirecting the whole
        # state directory covers every store added later, including ones nobody here knows about.
        self._env("LODESTAR_STATE_DIR", os.path.join(self.tmp, "state"))
        self._env("LODESTAR_MACHINE", "testbox")
        with open(os.path.join(self.board, "machines.md"), "w", encoding="utf-8") as fh:
            fh.write("| Key | Role |\n|-----|------|\n| testbox | desktop |\n")

        # Machine-local stores stay in the temp tree: the executors call `transitions.record`
        # and friends with no path, so unredirected they write throwaway rows into live state.
        self._patch(paths, "lifecycle_transitions_file",
                    lambda: os.path.join(self.tmp, "transitions.json"))
        self._patch(paths, "lifecycle_journal_state_file",
                    lambda: os.path.join(self.tmp, "journaled.json"))
        self._patch(paths, "lifecycle_lock_file",
                    lambda repo_id: os.path.join(self.tmp, "%s.lock" % repo_id))
        self._patch(paths, "tombstones_file", lambda: os.path.join(self.board, "sync",
                                                                   "tombstones.md"))
        self._patch(journal, "append", lambda *a, **k: None)

    def _env(self, name, value):
        original = os.environ.get(name)
        os.environ[name] = value
        self.addCleanup(lambda: os.environ.__setitem__(name, original)
                        if original is not None else os.environ.pop(name, None))

    def _patch(self, module, name, value):
        original = getattr(module, name)
        setattr(module, name, value)
        self.addCleanup(setattr, module, name, original)

    def write_registry(self, body):
        path = os.path.join(self.board, "sync", "registry.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# Registry\n\nprose above the table\n\n" + HEADER + body)
        return path

    def registry_path(self):
        return os.path.join(self.board, "sync", "registry.md")

    def declared(self, repo_id="thing"):
        for entry in registry.entries(self.registry_path()):
            if entry.repo_id == repo_id:
                return entry.remote
        return None

    def repo(self, name="Thing", origin=PUBLIC):
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        subprocess.run(["git", "init", "--quiet", "-b", "main", path], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if origin:
            git(path, "remote", "add", "origin", origin)
        return path

    def origin_of(self, path):
        from lib import gitutil
        return gitutil.remote_endpoints(path)


class SurgicalWriteTest(BoardTestCase):
    """`registry.set_declared_remote` writes ONE cell of ONE row, or refuses.

    This is the property the whole feature rests on, and the one with the largest blast radius:
    `sync/registry.md` is shared, hand-edited desired state that three machines touch, and one
    live row carries a thousand-word `Notes` cell. A writer that re-rendered the table would be
    a worse bug than the stale declaration it was built to fix.
    """

    def test_only_the_remote_cell_of_the_named_row_changes(self):
        """Byte-for-byte everywhere else. A column shift here reads as a valid file."""
        path = self.write_registry(row(remote=PUBLIC) + row(name="Other", repo_id="other",
                                                            remote="git@example.com:x/O.git"))
        before = open(path, encoding="utf-8").read()

        result = registry.set_declared_remote("thing", INTERNAL, path)

        self.assertTrue(result["ok"], result["reason"])
        self.assertTrue(result["changed"])
        after = open(path, encoding="utf-8").read()
        changed = [(a, b) for a, b in zip(before.split("\n"), after.split("\n")) if a != b]
        self.assertEqual(1, len(changed), "exactly one line may change: %s" % (changed,))
        self.assertEqual(before.replace(PUBLIC, INTERNAL), after)

    def test_every_other_cell_of_the_edited_row_survives_verbatim(self):
        """Including the long `Notes` cell, which is where the real registry keeps its reasons."""
        notes = "a long note with reasons a human wrote and must not lose"
        path = self.write_registry(row(notes=notes))

        registry.set_declared_remote("thing", INTERNAL, path)

        parsed = mdtable.parse(open(path, encoding="utf-8").read())[0]
        self.assertEqual(INTERNAL, parsed["remote"])
        self.assertEqual(notes, parsed["notes"])
        self.assertEqual("thing", parsed["id"])
        self.assertEqual("main", parsed["branch"])
        self.assertEqual("on", parsed["send"])

    def test_a_literal_pipe_in_another_cell_is_not_split_or_unescaped(self):
        r"""The escape five modules write and the reader ignored for months (CLAUDE.md §7).

        A `Notes` cell containing `\|` must come back out of this writer still escaped. Reading
        it with `_cells` and writing it back would turn it into a bare `|` and shift every later
        column — the measured `tombstones.append` corruption, arriving through a new writer.
        """
        path = self.write_registry(row(notes=r"first \| second \| third"))

        result = registry.set_declared_remote("thing", INTERNAL, path)

        self.assertTrue(result["ok"], result["reason"])
        text = open(path, encoding="utf-8").read()
        self.assertIn(r"first \| second \| third", text)
        parsed = mdtable.parse(text)
        self.assertEqual(1, len(parsed), "an unescaped pipe would have made extra columns")
        self.assertEqual("first | second | third", parsed[0]["notes"])
        self.assertEqual(INTERNAL, parsed[0]["remote"])

    def test_a_pipe_in_the_new_value_is_escaped_rather_than_breaking_the_row(self):
        """A git URL cannot normally contain `|`, so this is defence, not a use case.

        It is cheap and it is the difference between a rejected value and a silently shifted
        table, which is the failure mode this file format actually has.
        """
        path = self.write_registry(row())

        result = registry.set_declared_remote("thing", "git@h:x/a|b.git", path)

        self.assertTrue(result["ok"], result["reason"])
        parsed = mdtable.parse(open(path, encoding="utf-8").read())
        self.assertEqual(1, len(parsed), "the row must still have its original columns")
        self.assertEqual("git@h:x/a|b.git", parsed[0]["remote"])
        self.assertEqual("active", parsed[0]["state"], "no cell may have shifted")

    def test_an_unknown_id_refuses_and_writes_nothing(self):
        """Absence of a row never authorises inventing one. The file must be untouched."""
        path = self.write_registry(row())
        before = open(path, encoding="utf-8").read()

        result = registry.set_declared_remote("no-such-id", INTERNAL, path)

        self.assertFalse(result["ok"])
        self.assertIn("no registry row", result["reason"])
        self.assertEqual(before, open(path, encoding="utf-8").read())

    def test_a_duplicated_id_refuses_rather_than_picking_one(self):
        """Two rows claiming one id is `validate`'s `duplicate-id` fault, which blocks converge.

        Reaching this writer with one anyway must not resolve it by writing the first match —
        the registry schema's own rule is that "last row wins" is the failure, not the fallback.
        """
        path = self.write_registry(row() + row(name="ThingCopy", path="~/dev/ThingCopy"))
        before = open(path, encoding="utf-8").read()

        result = registry.set_declared_remote("thing", INTERNAL, path)

        self.assertFalse(result["ok"])
        self.assertIn("2 rows claim", result["reason"])
        self.assertEqual(before, open(path, encoding="utf-8").read())

    def test_a_table_without_a_remote_column_refuses_instead_of_adding_one(self):
        """Adding a column would rewrite the header, the separator and all 53 live rows."""
        path = os.path.join(self.board, "sync", "registry.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("| Name | Path | Id |\n|---|---|---|\n| Thing | ~/dev/Thing | thing |\n")
        before = open(path, encoding="utf-8").read()

        result = registry.set_declared_remote("thing", INTERNAL, path)

        self.assertFalse(result["ok"])
        self.assertIn("no remote column", result["reason"])
        self.assertEqual(before, open(path, encoding="utf-8").read())

    def test_a_short_row_refuses_rather_than_being_padded(self):
        """Padding to reach the column shifts every cell after the gap — the measured corruption."""
        path = os.path.join(self.board, "sync", "registry.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(HEADER + "| Thing | ~/dev/Thing | main | on | on |  | thing |\n")
        before = open(path, encoding="utf-8").read()

        result = registry.set_declared_remote("thing", INTERNAL, path)

        self.assertFalse(result["ok"])
        self.assertIn("no remote cell", result["reason"])
        self.assertEqual(before, open(path, encoding="utf-8").read())

    def test_an_already_correct_declaration_is_ok_but_not_changed(self):
        """Idempotent. A rewrite on every sweep is churn in a file three machines merge."""
        path = self.write_registry(row(remote=INTERNAL))
        before = open(path, encoding="utf-8").read()

        result = registry.set_declared_remote("thing", INTERNAL, path)

        self.assertTrue(result["ok"])
        self.assertFalse(result["changed"])
        self.assertEqual(before, open(path, encoding="utf-8").read())

    def test_a_newline_in_the_value_is_refused_not_flattened(self):
        """A cell is one line, and a URL with a newline in it is not a URL."""
        path = self.write_registry(row())
        result = registry.set_declared_remote("thing", "git@h:x/a.git\nmalicious", path)
        self.assertFalse(result["ok"])
        self.assertIn("newline", result["reason"])

    def test_the_machine_paths_table_below_is_unreachable_from_here(self):
        """`mdtable.first_table` is scoped like `parse`: only the first contiguous table.

        The second table has different columns and a row keyed by repo NAME, so a writer that
        scanned the whole document could match there and write into the wrong schema.
        """
        path = self.write_registry(
            row() + "\n## Machine paths\n\n| Repo | Machine | Path |\n|---|---|---|\n"
                    "| Thing | home | ~/dev/Elsewhere |\n")

        registry.set_declared_remote("thing", INTERNAL, path)

        text = open(path, encoding="utf-8").read()
        self.assertIn("| Thing | home | ~/dev/Elsewhere |", text)
        self.assertEqual(("~/dev/Elsewhere"),
                         registry.machine_paths(text)[("Thing", "home")])


class ParseBackGuardTest(BoardTestCase):
    """The write is parsed back BEFORE it lands, and refuses rather than corrupting.

    A column shift in this format produces a file that still parses, still renders and still
    looks hand-written — it is only wrong once cells are addressed by their headers. So the only
    honest proof that one cell moved is that no other one did, and the check has to run on the
    candidate text while the real file is still untouched.
    """

    def test_a_corrupting_edit_is_refused_and_the_file_is_left_alone(self):
        path = self.write_registry(row())
        before = open(path, encoding="utf-8").read()

        # Force the corruption the guard exists for: an "escape" that emits a bare pipe turns
        # one row into two extra columns, which is exactly the measured `tombstones` shift.
        original = mdtable.escape
        mdtable.escape = lambda text: text
        self.addCleanup(setattr, mdtable, "escape", original)

        result = registry.set_declared_remote("thing", "git@h:x/a|b|c.git", path)

        self.assertFalse(result["ok"], "a shifted table must not be written")
        self.assertIn("more than the remote cell", result["reason"])
        self.assertEqual(before, open(path, encoding="utf-8").read(),
                         "the refusal must leave desired state exactly as found")


class DeclarationStepTest(BoardTestCase):
    """`_rehome_declaration_step` — what it writes, what it skips, and what it refuses."""

    def test_a_stale_declaration_is_brought_onto_the_plans_endpoint(self):
        self.write_registry(row(remote=PUBLIC))

        ok, lines = lifecycle._rehome_declaration_step(plan(new=INTERNAL), False, [])

        self.assertTrue(ok, "\n".join(lines))
        self.assertEqual(INTERNAL, self.declared())

    def test_a_declaration_of_the_same_repo_over_another_scheme_is_left_alone(self):
        """Identity, never string equality — the same argument `_rename_remote_step` makes.

        This machine reaches GitHub over ssh and tower only over https. Rewriting the shared
        declaration to one machine's scheme would force the other onto an access method it
        cannot use, and would churn the file on every sweep.
        """
        self.write_registry(row(remote="https://github.com/example/Thing.git"))

        ok, lines = lifecycle._rehome_declaration_step(
            plan(new="git@github.com:example/Thing.git"), False, [])

        self.assertTrue(ok, "\n".join(lines))
        self.assertEqual("https://github.com/example/Thing.git", self.declared(),
                         "the same repo over a different scheme is not a divergence")
        self.assertTrue(any("already declares" in line for line in lines), lines)

    def test_a_blank_declaration_is_filled_in_from_the_approved_plan(self):
        """Three live rows declare no remote, and this is not the guessed URL that is forbidden.

        `_rename_remote_step` must never compute a URL from a directory name. An explicitly
        approved rehome endpoint is the opposite of a guess, and recording it is what lets the
        declared-vs-actual audit check anything afterwards.
        """
        self.write_registry(row(remote=""))

        ok, lines = lifecycle._rehome_declaration_step(plan(new=INTERNAL), False, [])

        self.assertTrue(ok, "\n".join(lines))
        self.assertEqual(INTERNAL, self.declared())

    def test_no_registry_row_is_reported_and_does_not_block(self):
        """With nothing declared, nothing will later rewrite origin from it, so there is no
        collision to prevent — and `_converge_one` has already said `omitted` upstream."""
        self.write_registry(row(name="Other", path="~/dev/Other", repo_id="other"))

        ok, lines = lifecycle._rehome_declaration_step(plan(new=INTERNAL), False, [])

        self.assertTrue(ok)
        self.assertTrue(any("no row has Id thing" in line for line in lines), lines)

    def test_a_plan_with_no_new_endpoint_is_REFUSED(self):
        """There is nothing to declare and nothing to repoint to. This ADDS a refusal.

        It cannot hide anything: the `set-url` that followed would have failed on an empty URL
        anyway, so the only behaviour change is that the no-checkout branch can no longer report
        `converged` for a plan that names no endpoint at all.
        """
        self.write_registry(row())

        ok, lines = lifecycle._rehome_declaration_step(plan(new=""), False, [])

        self.assertFalse(ok)
        self.assertTrue(any("names no new endpoint" in line for line in lines), lines)
        self.assertEqual(PUBLIC, self.declared(), "a refusal must write nothing")

    def test_a_dry_run_names_the_rewrite_and_writes_nothing(self):
        path = self.write_registry(row(remote=PUBLIC))
        before = open(path, encoding="utf-8").read()

        ok, lines = lifecycle._rehome_declaration_step(plan(new=INTERNAL), True, [])

        self.assertTrue(ok)
        self.assertTrue(any(INTERNAL in line and "would set" in line for line in lines), lines)
        self.assertEqual(before, open(path, encoding="utf-8").read())

    def test_a_failed_registry_write_REFUSES_rather_than_repointing_origin(self):
        """🔴 The ordering property, stated as a status.

        If desired state cannot be updated, the rehome must not proceed — origin on the new host
        with the registry naming the old one IS the hazard, and `_rename_remote_step` would then
        roll the repo back on the next rename. A `partial` here would be the same hazard wearing
        a warning label.
        """
        self.write_registry(row())
        self._patch(registry, "set_declared_remote",
                    lambda *a, **k: {"ok": False, "changed": False,
                                     "reason": "simulated write failure"})

        ok, lines = lifecycle._rehome_declaration_step(plan(new=INTERNAL), False, [])

        self.assertFalse(ok)
        blob = "\n".join(lines)
        self.assertIn("simulated write failure", blob,
                      "the refusal must carry the underlying reason, not a fixed sentence")
        self.assertIn("later rename would put this repo back", blob)


class RehomeExecutorDeclarationTest(BoardTestCase):
    """`_do_rehome` as a whole: the declaration travels with the repoint, both ways."""

    def test_a_converged_rehome_leaves_origin_and_the_declaration_IN_STEP(self):
        self.write_registry(row())
        repo = self.repo(origin=PUBLIC)

        state, lines = lifecycle._do_rehome(plan(new=INTERNAL), repo, "fp", False, [])

        self.assertEqual("converged", state, "\n".join(lines))
        self.assertEqual(INTERNAL, self.origin_of(repo)[0])
        self.assertEqual(INTERNAL, self.declared())

    def test_a_machine_holding_NO_checkout_still_updates_desired_state(self):
        """🔴 The declaration is fleet-wide, not a per-machine fact.

        `_do_rehome` reports a no-action converge where there is no checkout (so an ordinary
        failure cannot wedge the repo's single transition slot). If the declaration rode inside
        that branch, which machine happened to hold the directory would decide whether desired
        state was ever recorded — which is how a half-applied state becomes permanent.
        """
        self.write_registry(row())
        gone = os.path.join(self.tmp, "NotHere")

        state, lines = lifecycle._do_rehome(plan(new=INTERNAL), gone, "fp", False, [])

        self.assertEqual("converged", state, "\n".join(lines))
        self.assertEqual(INTERNAL, self.declared())
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "transitions.json")),
                         "a record for an act that never began is what wedges the repo")

    def test_a_stray_pushurl_is_still_PARTIAL_with_the_declaration_already_written(self):
        """The pre-existing §3D guard 8 refusal must survive this change unweakened.

        The declaration is written first on purpose, so `partial` here means desired state names
        the approved endpoint while one real endpoint does not — which is the accurate report,
        and it keeps flagging every sweep until a person clears the pushurl.
        """
        self.write_registry(row())
        repo = self.repo(origin=PUBLIC)
        git(repo, "remote", "set-url", "--push", "--add", "origin",
            "https://github.com/example/mirror.git")

        state, lines = lifecycle._do_rehome(plan(new=INTERNAL), repo, "fp", False, [])

        self.assertEqual("partial", state, "\n".join(lines))
        self.assertIn("--push --delete", "\n".join(lines))
        self.assertEqual(INTERNAL, self.declared())

    def test_a_FAILED_registry_write_leaves_ORIGIN_UNTOUCHED(self):
        """🔴 The ordering property where it actually matters, asserted on git config.

        Written after the `set-url`, a failed registry write leaves origin on the new host with
        desired state naming the old one — the exact hazard this change exists to close, created
        at the one moment nobody is watching, and `_rename_remote_step` would then roll the repo
        back. Written first, the repoint never happens and the repo is exactly as found.

        `test_a_failed_registry_write_REFUSES_rather_than_repointing_origin` asserts the step's
        verdict; this asserts the consequence, which is the thing a confidentiality decision
        depends on.
        """
        self.write_registry(row())
        repo = self.repo(origin=PUBLIC)
        self._patch(registry, "set_declared_remote",
                    lambda *a, **k: {"ok": False, "changed": False,
                                     "reason": "simulated write failure"})

        state, lines = lifecycle._do_rehome(plan(new=INTERNAL), repo, "fp", False, [])

        self.assertEqual("refused", state, "\n".join(lines))
        self.assertEqual(PUBLIC, self.origin_of(repo)[0],
                         "origin must not move while desired state still names the old host")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "transitions.json")),
                         "a refusal before the mutation must record no transition")

    def test_a_dry_run_touches_neither_git_config_nor_the_registry(self):
        self.write_registry(row())
        repo = self.repo(origin=PUBLIC)
        before = open(self.registry_path(), encoding="utf-8").read()

        state, lines = lifecycle._do_rehome(plan(new=INTERNAL), repo, "fp", True, [])

        self.assertEqual("would-converge", state, "\n".join(lines))
        self.assertEqual(PUBLIC, self.origin_of(repo)[0])
        self.assertEqual(before, open(self.registry_path(), encoding="utf-8").read())

    def test_rerunning_a_converged_rehome_changes_nothing(self):
        """The converger re-evaluates every approved plan on every run — nothing retires one."""
        self.write_registry(row())
        repo = self.repo(origin=PUBLIC)
        lifecycle._do_rehome(plan(new=INTERNAL), repo, "fp", False, [])
        after_first = open(self.registry_path(), encoding="utf-8").read()

        state, lines = lifecycle._do_rehome(plan(new=INTERNAL), repo, "fp", False, [])

        self.assertEqual("converged", state, "\n".join(lines))
        self.assertEqual(after_first, open(self.registry_path(), encoding="utf-8").read(),
                         "an idempotent re-run must not churn a file three machines merge")


class RenameNoLongerRollsBackTest(BoardTestCase):
    """🔴 The composed hazard, as one test: rehome then rename must not revert the host.

    Measured on this branch before any change, through the real CLI:

        origin after rehome : git@git.private.example:mandrake/Thing.git
        registry Remote     : git@github.com:example/Thing.git
        origin after rename : git@github.com:example/Thing.git   <- rolled back, `converged`

    Neither executor was wrong on its own. `_rename_remote_step` putting origin on the declared
    `Remote` is Lukas's own 2026-09-30 ruling and must stay; the defect was that the declaration
    never learned about the rehome. This test fails if either half regresses.
    """

    def test_a_rename_after_a_rehome_keeps_the_new_host(self):
        self.write_registry(row(remote=PUBLIC))
        repo = self.repo("Thing", origin=PUBLIC)

        state, lines = lifecycle._do_rehome(plan(new=INTERNAL), repo, "fp", False, [])
        self.assertEqual("converged", state, "\n".join(lines))

        moved = os.path.join(self.tmp, "Renamed")
        state, lines = lifecycle._do_rename(
            plan("rename", old=repo, new=moved), repo, moved, "fp", False, [])

        self.assertEqual("converged", state, "\n".join(lines))
        self.assertEqual(INTERNAL, self.origin_of(moved)[0],
                         "the rename must not roll origin back to the pre-rehome endpoint")
        self.assertEqual(INTERNAL, self.declared())


@needs_private("sync/registry.md")
class RealRegistryShapeTest(unittest.TestCase):
    """The writer, against a COPY of the live `sync/registry.md`. Read-only on the real file.

    Every fixture above is a two-row table with a short `Notes` cell, and the live registry is
    nothing like that: 57 rows, one of them carrying a 2,177-character `Notes` cell full of a
    human's reasons. A locator that works on a toy table and fails on the real one is exactly
    the shape of defect this repo keeps finding, so the real document is the fixture here.

    It copies and writes to the copy rather than asserting a no-op on the live file, because a
    no-op cannot prove the surgical property — only a real write whose diff is one line can.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-real-registry-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # `paths.registry_file()` honours `LODESTAR_ROOT`, and a leaked one from another test
        # module would silently point this at a two-row fixture — which is a vacuous pass of
        # the one test here whose whole value is running against real data. `_DEFAULT_ROOT` is
        # derived from this checkout's own layout and cannot be redirected.
        self.live = os.path.join(paths._DEFAULT_ROOT, "sync", "registry.md")
        self.assertIn("| Name | Path | Branch |", open(self.live, encoding="utf-8").read(),
                      "this must be the real registry, not a fixture")
        self.copy = os.path.join(self.tmp, "registry.md")
        shutil.copy2(self.live, self.copy)
        self.before = open(self.copy, encoding="utf-8").read()
        # A fixed (role, key) so the Machine-paths override is deterministic rather than
        # whichever machine happens to run the suite.
        self.rows = registry.entries(self.copy, role="desktop", key=real_key("desktop"))

    def rows_of(self, path):
        return registry.entries(path, role="desktop", key=real_key("desktop"))

    def test_a_write_to_the_longest_row_changes_one_line_and_nothing_else(self):
        """The row with the longest Notes cell (over a thousand words) is the worst case."""
        target = max((r for r in self.rows if r.repo_id), key=lambda r: len(r.notes))
        rid = target.repo_id
        self.assertGreater(len(target.notes), 1000,
                           "this test is only meaningful on the long-Notes row")

        result = registry.set_declared_remote(
            rid, "git@git.private.example:mandrake/Renamed.git", self.copy)

        self.assertTrue(result["ok"], result["reason"])
        after = open(self.copy, encoding="utf-8").read()
        changed = [(a, b) for a, b in zip(self.before.split("\n"), after.split("\n")) if a != b]
        self.assertEqual(1, len(changed), "exactly one line may change")
        rows = self.rows_of(self.copy)
        self.assertEqual(len(self.rows), len(rows), "the row count must not move")
        got = registry.by_id(rows)[rid]
        self.assertEqual("git@git.private.example:mandrake/Renamed.git", got.remote)
        self.assertEqual(target, got._replace(remote=target.remote),
                         "no other field of the edited row may move")
        self.assertEqual([r for r in self.rows if r.repo_id != rid],
                         [r for r in rows if r.repo_id != rid],
                         "no other row may change")

    def test_the_rows_that_declare_no_remote_can_be_filled_in_cleanly(self):
        """Measured 2026-10-01: four such rows, not the three recorded elsewhere."""
        blank = [r for r in self.rows if r.repo_id and not r.remote]
        self.assertTrue(blank, "expected at least one row declaring no remote")
        target = blank[0]

        result = registry.set_declared_remote(target.repo_id, "git@example.com:x/y.git",
                                              self.copy)

        self.assertTrue(result["ok"], result["reason"])
        got = registry.by_id(self.rows_of(self.copy))[target.repo_id]
        self.assertEqual("git@example.com:x/y.git", got.remote)
        self.assertEqual(target, got._replace(remote=""))

    def test_the_registry_is_SWEEPABLE_so_the_declaration_actually_travels(self):
        """The whole fix rests on this, so it is checked rather than assumed.

        `gitutil.partition_paths` withholds source from the hourly auto-commit and merely
        *reports* it in `SYNC.md`. If `sync/registry.md` were withheld, a rehome would update
        desired state on one machine and the edit would sit dirty for ever — the other machines
        would keep their stale declaration and the rollback hazard would survive the fix, with a
        green `converged` on the machine that did the work.
        """
        from lib import gitutil
        source, sweepable = gitutil.partition_paths(["sync/registry.md", "bin/lifecycle.py"])
        self.assertEqual(["sync/registry.md"], sweepable,
                         "a withheld registry cannot carry the declaration to another machine")
        self.assertEqual(["bin/lifecycle.py"], source,
                         "partition_paths returns (source, sweepable) — source FIRST")

    def test_the_live_registry_itself_is_never_written(self):
        """A test that wrote shared desired state would be worse than the bug it guards."""
        digest = open(self.live, encoding="utf-8").read()
        rid = next(r.repo_id for r in self.rows if r.repo_id)
        registry.set_declared_remote(rid, "git@example.com:x/y.git", self.copy)
        self.assertEqual(digest, open(self.live, encoding="utf-8").read())


class EndToEndTest(BoardTestCase):
    """`propose -> approve -> converge` through `lifecycle.main`, against a real git repo.

    🔴 **A fixture structurally cannot reach the bugs in this path.** CLAUDE.md §7 records why:
    `cmd_propose` applied `tilde()` to `new` for every plan kind, and for a rehome `new` is a git
    URL — `os.path.abspath` resolved it against the cwd and stored
    `~/dev/Lodestar/git@github…`, while an `https://` target collapsed to `https:/` and could not
    be recovered at all. A fixture hands the URL straight to the executor and never goes through
    `propose`, which is how that survived. So the CLI round trip is the test, not a demo of it.
    """

    def run_cli(self, *argv):
        """`lifecycle.main`, with its (deliberately verbose) consequence text captured.

        Kept on `self.cli_output` rather than discarded: the approve prompt is a safety surface
        and one test below asserts what it says.
        """
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = lifecycle.main(list(argv))
        self.cli_output = out.getvalue()
        return code

    def exercise(self, target):
        """propose -> approve -> converge one rehome to `target`. Returns the repo path."""
        repo = self.repo("Thing", origin=PUBLIC)
        self.write_registry(row(path=repo, remote=PUBLIC))
        self.assertEqual(0, self.run_cli("propose", "--kind", "rehome", "--id", "thing",
                                         "--old", repo, "--new", target,
                                         "--plan-id", "rehome-thing-e2e"))
        self.assertEqual(0, self.run_cli("approve", "rehome-thing-e2e", "--yes"))
        self.assertEqual(0, self.run_cli("converge"))
        return repo

    def test_an_ssh_rehome_moves_origin_AND_the_declaration(self):
        repo = self.exercise(INTERNAL)

        self.assertEqual(INTERNAL, plans.load_plans()["rehome-thing-e2e"].new,
                         "propose must store a URL verbatim, never through a path helper")
        self.assertEqual(INTERNAL, self.origin_of(repo)[0])
        self.assertEqual(INTERNAL, self.declared())

    def test_an_https_rehome_survives_the_round_trip_intact(self):
        """The worst case of the `tilde()` bug: `abspath` collapsed `//` to `/`, unrecoverably.

        Asserted at both ends — the stored plan AND the endpoint git actually holds — because a
        URL that is mangled on the way in produces a `set-url` that succeeds on garbage.
        """
        target = "https://git.private.example/mandrake/Thing.git"
        repo = self.exercise(target)

        self.assertEqual(target, plans.load_plans()["rehome-thing-e2e"].new)
        self.assertEqual(target, self.origin_of(repo)[0])
        self.assertEqual(target, self.declared())
        self.assertNotIn("https:/g", open(self.registry_path(), encoding="utf-8").read())

    def test_the_approve_prompt_names_the_shared_registry_edit(self):
        """A rehome now also authorises an edit to fleet-wide desired state.

        The consequence text is the entire basis on which that is authorised (§3C: every other
        machine then acts without ever asking), so a registry rewrite the prompt never mentioned
        would be approving blind.
        """
        repo = self.repo("Thing", origin=PUBLIC)
        self.write_registry(row(path=repo, remote=PUBLIC))
        self.run_cli("propose", "--kind", "rehome", "--id", "thing", "--old", repo,
                     "--new", INTERNAL, "--plan-id", "rehome-thing-text")
        self.assertIn("sync/registry.md", self.cli_output,
                      "propose must show it too, not only approve")

        out = io.StringIO()
        lifecycle.print_consequence(plans.load_plans()["rehome-thing-text"], out=out)
        text = out.getvalue()

        self.assertIn("sync/registry.md", text)
        self.assertIn(INTERNAL, text)
        self.assertIn(PUBLIC, text, "the prompt must name what is being replaced")

    def test_converge_is_idempotent_across_runs(self):
        """Nothing retires a completed plan, so every approved plan is re-evaluated each run."""
        self.exercise(INTERNAL)
        after_first = open(self.registry_path(), encoding="utf-8").read()

        self.assertEqual(0, self.run_cli("converge"))

        self.assertEqual(after_first, open(self.registry_path(), encoding="utf-8").read())


if __name__ == "__main__":
    unittest.main()
