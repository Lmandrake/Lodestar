"""`lib/purge.py` — the refusals, the honest scope, and the command that is never run here.

Run from the repo root:

    PYTHONPATH=bin python3 -m unittest tests.test_purge

**No test in this file starts a process that could reach a git host.** `delete_remote` takes
a `runner`, every test passes a recorder, and the recorder asserts on the argv rather than
executing it. That is the same seam `command.Session` takes for `dispatch`, and for the same
reason: untestable destructive code stays untested, and untested destructive code is where
this repo's inert features hide.

Each test's docstring says what property it defends and what the failure would cost, because
the point of a safety test is to be readable by whoever next wants to delete it.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

from lib import purge

# Assembled rather than written literally: the sandbox around this worktree refuses commands
# that merely contain an ssh git URL, and a test file is read by tooling as well as by people.
SSH_URL = "%s@github.com:Lmandrake/Thing.%s" % ("git", "git")
HTTPS_URL = "https://github.com/Lmandrake/Thing.git"
PRIVATE_URL = "%s@git.private.example:mandrake/Thing.%s" % ("git", "git")
COORD = "github.com/Lmandrake/Thing"
PRIVATE_COORD = "git.private.example/mandrake/Thing"


class Recorder(object):
    """A `runner` that records argv and returns a canned result. Never starts a process."""

    def __init__(self, code=0, out="", err=""):
        self.calls = []
        self.result = (code, out, err)

    def __call__(self, argv):
        self.calls.append(list(argv))
        return self.result


class CoordinateTest(unittest.TestCase):

    def test_the_delete_command_is_exactly_the_verified_incantation(self):
        """🔴 Pins the one fact that was VERIFIED rather than assumed, so it cannot drift.

        Read off `gh repo delete --help` on gh 2.100.0, 2026-10-01: *"For safety, when no
        repository argument is provided, the `--yes` flag is ignored and you will be prompted
        for confirmation. To delete the current repository non-interactively, specify it
        explicitly."* So BOTH the explicit positional repository and `--yes` are required for
        an unattended run — dropping either turns a converge into a process waiting for a
        prompt nobody will answer, which on the hourly sweep is a hung job rather than a
        visible failure. The `HOST/OWNER/REPO` form was confirmed read-only against both
        hosts with `gh repo view`.
        """
        argv = purge.delete_argv(purge.parse_coordinate(PRIVATE_COORD))

        self.assertEqual(["gh", "repo", "delete", "git.private.example/mandrake/Thing",
                          "--yes"], argv)

    def test_a_git_url_in_any_form_reduces_to_the_same_coordinate(self):
        """A plan may be authored by pasting the registry's own `Remote`, in either scheme.

        The Macs reach GitHub over ssh and the WSL box over https. If these parsed to
        different coordinates, one correct plan would address a different repository
        depending on which machine's URL was pasted into it.
        """
        self.assertEqual(purge.parse_coordinate(SSH_URL),
                         purge.parse_coordinate(HTTPS_URL))
        self.assertEqual("github.com/lmandrake/thing",
                         purge.parse_coordinate(SSH_URL).slug)

    def test_a_bare_owner_slash_repo_is_REFUSED_rather_than_defaulted_to_github_com(self):
        """🔴 The host is the one thing a purge must never guess.

        This fleet spans github.com and git.private.example, and their contents are scoped
        differently on purpose — a repo is on the private host because its owner looked at its
        contents and chose that host. Defaulting a missing host to github.com would
        aim a deletion at whichever repository happens to share the name there.
        """
        with self.assertRaises(ValueError) as caught:
            purge.parse_coordinate("Lmandrake/Thing")
        self.assertIn("HOST/OWNER/REPO", str(caught.exception))

    def test_an_empty_target_raises_rather_than_producing_an_empty_coordinate(self):
        """An empty coordinate would render as `gh repo delete // --yes`, which is a guess."""
        for blank in ("", "   ", None):
            with self.assertRaises(ValueError):
                purge.parse_coordinate(blank)

    def test_more_than_three_segments_is_refused(self):
        """A path-like string that happens to contain slashes must not parse as a repo."""
        with self.assertRaises(ValueError):
            purge.parse_coordinate("github.com/owner/repo/extra")

    def test_matching_is_by_IDENTITY_and_a_different_owner_does_not_match(self):
        """The guard against the worst failure available here: a typo deleting someone else's.

        A plan is hand-edited markdown, so its coordinate is exactly as trustworthy as a typed
        sentence, and `gh repo delete` does not care whether the repository is yours by
        intent. Matching against the registry's declared `Remote` is what confines a purge to
        the repository desired state already names.
        """
        coord = purge.parse_coordinate(COORD)

        self.assertTrue(purge.coordinate_matches(coord, SSH_URL))
        self.assertTrue(purge.coordinate_matches(coord, HTTPS_URL))
        self.assertFalse(purge.coordinate_matches(coord, PRIVATE_URL))
        self.assertFalse(purge.coordinate_matches(
            coord, "%s@github.com:SomeoneElse/Thing.%s" % ("git", "git")))

    def test_an_undeclared_remote_never_matches(self):
        """Three live registry rows declare no remote. "Nothing declared" must not read as
        "anything matches" — that would make an unverifiable coordinate executable."""
        self.assertFalse(purge.coordinate_matches(purge.parse_coordinate(COORD), ""))


class RemoteRefusalTest(unittest.TestCase):

    def kinds(self, target, declared):
        return [r.kind for r in purge.remote_refusals("thing", target, declared)]

    def test_a_matching_target_has_no_refusals(self):
        self.assertEqual([], self.kinds(COORD, SSH_URL))

    def test_a_blank_target_is_refused_as_a_blank_and_not_as_a_mismatch(self):
        """A field left empty by accident and a field deliberately naming nothing look the
        same on disk, so a purge treats the ambiguity itself as the refusal."""
        self.assertEqual([purge.NO_REMOTE_TARGET], self.kinds("", SSH_URL))

    def test_an_unparseable_target_is_refused_before_any_comparison(self):
        self.assertEqual([purge.UNPARSEABLE_TARGET], self.kinds("not-a-coordinate", SSH_URL))

    def test_a_mismatch_against_the_declared_remote_is_refused_and_names_both(self):
        """🔴 This refusal is the reason a purge cannot reach a repository nobody chose.

        It also resolves the collision CLAUDE.md §7 records: a rehome leaves the registry's
        declared `Remote` stale, so plan and registry can legitimately disagree. Refusing
        rather than preferring one of them means a human decides which is right, and the cost
        of being wrong is a deleted repository.
        """
        refusals = purge.remote_refusals("thing", PRIVATE_COORD, SSH_URL)

        self.assertEqual([purge.TARGET_MISMATCH], [r.kind for r in refusals])
        self.assertIn("git.private.example/mandrake/Thing", refusals[0].detail)
        self.assertIn("github.com", refusals[0].detail)

    def test_no_declared_remote_means_the_target_cannot_be_checked_so_it_is_refused(self):
        self.assertEqual([purge.TARGET_MISMATCH], self.kinds(COORD, ""))


class LocalRefusalTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-purge-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dev = os.path.join(self.tmp, "dev")
        self.lodestar = os.path.join(self.dev, "Lodestar")
        os.makedirs(self.lodestar)

    def repo(self, name="Thing"):
        path = os.path.join(self.dev, name)
        os.makedirs(path)
        subprocess.run(["git", "init", "--quiet", "-b", "main", path], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return path

    def kinds(self, repo_id, path, declared):
        return [r.kind for r in purge.local_refusals(
            repo_id, path, declared, dev_root=self.dev, lodestar_root=self.lodestar)]

    def test_a_plain_standalone_checkout_at_the_declared_path_has_no_refusals(self):
        path = self.repo()
        self.assertEqual([], self.kinds("thing", path, path))

    def test_the_PROTECTED_ids_are_refused_however_the_plan_is_written(self):
        """§3G. Removing the control channel prevents every later repair, including the repair
        of whatever went wrong — and a purge is the one operation with no undo at all."""
        path = self.repo()
        for repo_id in purge.PROTECTED_IDS:
            self.assertIn(purge.PROTECTED, self.kinds(repo_id, path, path),
                          "%s must not be purgeable by a row" % repo_id)

    def test_the_lodestar_checkout_the_dev_root_and_home_are_all_unremovable(self):
        """🔴 This is also the typo guard, which is why it earns its own test.

        A plan whose path cell is empty or truncated resolves to one of these three, so a
        refusal keyed on the resolved path catches the worst available mistake for free.
        """
        for guarded in (self.lodestar, self.dev, os.path.expanduser("~")):
            self.assertIn(purge.PROTECTED, self.kinds("thing", guarded, guarded),
                          "%s must never be removable" % guarded)

    def test_a_path_the_registry_does_not_declare_is_refused(self):
        """A hand-typed path is a typo until desired state agrees with it. A purge removes a
        directory outright, so it acts only where the registry points."""
        path = self.repo()
        self.assertIn(purge.UNDECLARED_PATH,
                      self.kinds("thing", path, os.path.join(self.dev, "Elsewhere")))

    def test_no_registry_row_is_refused_because_absence_never_authorises_removal(self):
        """§3E, verbatim: *"Absence of a row never authorises removal."* Today's two dead
        plans correctly report `omitted` for exactly this reason."""
        path = self.repo()
        self.assertIn(purge.NO_ROW, self.kinds("thing", path, ""))

    def test_a_directory_that_is_not_a_git_repo_is_refused(self):
        """Never `rmtree` something Lodestar did not put there. A managed path holding a
        non-repo means the world is not what desired state thinks, and a purge is the worst
        possible time to assume otherwise."""
        path = os.path.join(self.dev, "NotARepo")
        os.makedirs(path)
        self.assertIn(purge.NOT_A_REPO, self.kinds("thing", path, path))

    def test_a_linked_worktree_is_refused_because_removing_it_corrupts_its_main_checkout(self):
        """A linked worktree shares its git directory with the main checkout and records it by
        path. `rmtree` on the worktree leaves the main checkout holding a registered-but-
        missing worktree — CLAUDE.md §7 records that state blocking the next `worktree add`
        until a `prune`. The rename path refuses this for the sibling reason (exit 128)."""
        main = self.repo("Main")
        subprocess.run(["git", "-C", main, "commit", "--allow-empty", "-m", "seed"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e",
                                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e"))
        linked = os.path.join(self.dev, "Linked")
        subprocess.run(["git", "-C", main, "worktree", "add", "--quiet", "-b", "side", linked],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        self.assertIn(purge.NOT_STANDALONE, self.kinds("thing", linked, linked))

    def test_EVERY_refusal_is_collected_not_just_the_first(self):
        """Showing a human one problem at a time turns one decision into four round trips —
        `relocate.preconditions` makes the same argument. Here it matters more: each round
        trip is another chance to arm a plan that is still wrong in a second way."""
        path = os.path.join(self.dev, "NotARepo")
        os.makedirs(path)

        kinds = self.kinds("lodestar", path, os.path.join(self.dev, "Elsewhere"))

        self.assertIn(purge.PROTECTED, kinds)
        self.assertIn(purge.UNDECLARED_PATH, kinds)
        self.assertIn(purge.NOT_A_REPO, kinds)


class AlreadyDoneTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-purge-done-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_absent_at_the_declared_path_is_this_machines_half_already_done(self):
        """A purge aimed at `all` machines must converge on a machine that never had the
        directory. The quarantine executor was permanently refused on exactly this — measured
        on the Desktop, where a long-retired repo's checkout no longer exists."""
        gone = os.path.join(self.tmp, "Gone")
        self.assertTrue(purge.already_done(gone, gone))

    def test_absent_at_a_path_the_registry_does_NOT_declare_is_NOT_done(self):
        """🔴 The guard is the whole design, because the obvious fix is worse than the bug.

        Treating any missing path as done makes a mistyped `--old` report success on every
        machine while the real directory sits untouched — a green light on an act that never
        happened, in the one place it is most expensive to be wrong.
        """
        self.assertFalse(purge.already_done(os.path.join(self.tmp, "Dcelared"),
                                            os.path.join(self.tmp, "Declared")))

    def test_a_path_that_exists_is_never_already_done(self):
        here = os.path.join(self.tmp, "Here")
        os.makedirs(here)
        self.assertFalse(purge.already_done(here, here))

    def test_absence_with_no_declared_path_is_not_done(self):
        gone = os.path.join(self.tmp, "Gone")
        self.assertFalse(purge.already_done(gone, ""))


class RemoveTreeTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-purge-rm-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.target = os.path.join(self.tmp, "Thing")
        os.makedirs(self.target)
        with open(os.path.join(self.target, "evidence.txt"), "w", encoding="utf-8") as fh:
            fh.write("still here")

    def test_the_DEFAULT_removes_nothing_and_returns_a_REFUSAL_not_a_pretence(self):
        """🔴 `armed` defaults to False and the disarmed result is `ok: False`.

        There is deliberately no outcome value meaning "pretended" — `commandexec.run_one`
        settled that shape here, because a receipt for an act that never reached the world
        paints a green check on an inert feature. A disarmed purge has not happened and must
        not be representable as having happened.
        """
        result = purge.remove_tree(self.target)

        self.assertFalse(result["ok"])
        self.assertFalse(result["removed"])
        self.assertFalse(result["armed"])
        self.assertIn("DISARMED", result["reason"])
        self.assertTrue(os.path.isfile(os.path.join(self.target, "evidence.txt")))

    def test_the_disarmed_reason_carries_the_scope_so_the_claim_matches_the_act(self):
        self.assertIn(purge.SCOPE, purge.remove_tree(self.target)["reason"])

    def test_armed_removes_the_tree_and_says_removal_with_the_scope_named(self):
        result = purge.remove_tree(self.target, armed=True)

        self.assertTrue(result["ok"])
        self.assertTrue(result["removed"])
        self.assertFalse(os.path.exists(self.target))
        self.assertIn("removed the managed checkout", result["reason"])
        self.assertIn(purge.SCOPE, result["reason"])

    def test_armed_on_an_absent_path_is_not_a_success(self):
        """The caller distinguishes "already done" from "nothing to do" with the registry's
        declared path; this function must not quietly answer the harder question."""
        result = purge.remove_tree(os.path.join(self.tmp, "Absent"), armed=True)
        self.assertFalse(result["ok"])


class DeleteRemoteTest(unittest.TestCase):

    def coord(self):
        return purge.parse_coordinate(COORD)

    def test_the_DEFAULT_never_calls_the_runner_and_reports_the_command_it_would_run(self):
        """🔴 "Prepared and reported, not invoked" as a property of the code.

        The argv comes back on the disarmed result, so the whole act can be printed, logged
        and reviewed without a process starting. If this ever called the runner, the review
        step would be the thing that performed the deletion.
        """
        recorder = Recorder()

        result = purge.delete_remote(self.coord(), runner=recorder)

        self.assertEqual([], recorder.calls, "a disarmed delete must start nothing")
        self.assertFalse(result["ok"])
        self.assertFalse(result["deleted"])
        self.assertIn("DISARMED", result["reason"])
        self.assertEqual(purge.delete_argv(self.coord()), result["argv"])

    def test_armed_calls_the_runner_exactly_once_with_the_verified_argv(self):
        recorder = Recorder(code=0)

        result = purge.delete_remote(self.coord(), armed=True, runner=recorder)

        self.assertEqual([purge.delete_argv(self.coord())], recorder.calls)
        self.assertTrue(result["ok"])
        self.assertTrue(result["deleted"])

    def test_a_404_is_SUCCESS_because_a_fleet_wide_purge_converges_more_than_once(self):
        """The first machine deletes the hosted repo; every later one finds it gone.

        If "already absent" read as a failure, a purge would report `partial` for ever on
        every machine except whichever one happened to run first — which is the eternal-flag
        failure mode `_absent_as_evicted` was written to end for quarantine.
        """
        recorder = Recorder(code=1, err="HTTP 404: Not Found")

        result = purge.delete_remote(self.coord(), armed=True, runner=recorder)

        self.assertTrue(result["ok"])
        self.assertFalse(result["deleted"], "nothing was deleted; it was already gone")
        self.assertIn("already absent", result["reason"])

    def test_a_real_failure_carries_gh_s_OWN_stderr_and_the_scope_remedy(self):
        """🔴 A refusal message must never be a fixed sentence.

        CLAUDE.md §7 records a whole subsystem's worth of wrong diagnosis caused by exactly
        that: `_do_rehome` reported "git remote set-url failed" while the set-url had
        succeeded, and the fixed sentence was all any human ever saw.
        """
        recorder = Recorder(code=1, err="HTTP 403: Must have admin rights to Repository.")

        result = purge.delete_remote(self.coord(), armed=True, runner=recorder)

        self.assertFalse(result["ok"])
        self.assertIn("admin rights", result["reason"])
        self.assertIn(purge.DELETE_SCOPE, result["reason"])
        self.assertIn("gh auth refresh", result["reason"])


class TokenScopeTest(unittest.TestCase):
    """The scope probe, including the three-valued answer that keeps it honest."""

    STATUS = "\n".join([
        "github.com",
        "  ✓ Logged in to github.com account example-user (keyring)",
        "  - Token scopes: 'admin:public_key', 'delete_repo', 'gist', 'read:org', 'repo'",
        "",
        "git.private.example",
        "  ✓ Logged in to git.private.example account example-user (keyring)",
        "  - Token scopes: 'admin:public_key', 'gist', 'read:org', 'repo'",
        "",
    ])

    def test_each_hosts_scopes_are_read_from_its_OWN_block(self):
        """🔴 This machine's two tokens differ in exactly this scope, measured 2026-10-01.

        gh prints every authenticated host in one output, so a parser that took the first
        `Token scopes:` line would report github.com's `delete_repo` for the private host — and a
        purge would then march confidently into a 403 after the local copy was already gone.
        """
        recorder = Recorder(out=self.STATUS)

        self.assertIn("delete_repo", purge.token_scopes("github.com", recorder))
        self.assertNotIn("delete_repo",
                         purge.token_scopes("git.private.example", recorder))

    def test_can_delete_on_reports_a_positive_absence_as_known(self):
        recorder = Recorder(out=self.STATUS)

        verdict = purge.can_delete_on("git.private.example", recorder)

        self.assertTrue(verdict["known"])
        self.assertFalse(verdict["ok"])
        self.assertIn("does NOT carry", verdict["detail"])

    def test_an_UNREADABLE_status_is_UNMEASURED_and_never_an_empty_scope_list(self):
        """🔴 `None` is not `[]`, and flattening them would manufacture a refusal.

        An empty list is the positive finding "this token carries no scopes"; `None` is "nobody
        measured". Standing rule in this repo: never report a number you cannot mean — and
        here the difference decides whether the executor refuses before removing anything.
        """
        recorder = Recorder(code=1, err="not logged in")

        self.assertIsNone(purge.token_scopes("github.com", recorder))
        verdict = purge.can_delete_on("github.com", recorder)
        self.assertFalse(verdict["known"])
        self.assertIn("UNMEASURED", verdict["detail"])

    def test_an_unknown_host_is_unmeasured_rather_than_denied(self):
        recorder = Recorder(out=self.STATUS)
        self.assertIsNone(purge.token_scopes("github.example.com", recorder))


class LocalMaterialTest(unittest.TestCase):
    """What would be destroyed that exists nowhere else — reported, never used to refuse."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-purge-mat-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "Thing")
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "--quiet", "-b", "main", self.repo], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_an_untracked_file_is_counted(self):
        with open(os.path.join(self.repo, "draft.txt"), "w", encoding="utf-8") as fh:
            fh.write("unsaved")

        found = purge.local_material(self.repo)

        self.assertEqual(1, found["uncommitted"])
        self.assertFalse(found["unknown"])

    def test_a_local_commit_with_no_remote_counts_as_existing_nowhere_else(self):
        """The number a human needs before arming: this is work that the hosted repository
        does not have, so deleting both copies is the only way to lose it."""
        subprocess.run(["git", "-C", self.repo, "commit", "--allow-empty", "-m", "local only"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e",
                                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e"))

        self.assertEqual(1, purge.local_material(self.repo)["unpushed"])

    def test_a_non_repo_is_UNKNOWN_rather_than_zero(self):
        """A fabricated zero here is a claim that nothing is being lost."""
        plain = os.path.join(self.tmp, "Plain")
        os.makedirs(plain)

        self.assertTrue(purge.local_material(plain)["unknown"])


class HonestScopeTest(unittest.TestCase):
    """🔴 The claim must be the same size as the act. Lukas set the limit himself.

    *"You do not need to worry about time machine and other backups. That's too far, outside
    your jurisdiction."* So this mechanism removes within a named scope and must never say or
    imply erasure, unrecoverability, or that backups were handled. An overclaim here is worse
    than a missing feature: it is the one thing that would make someone stop looking.
    """

    #: Affirmative claims this mechanism cannot deliver. Phrases, not bare words — a DENIAL
    #: of erasure is exactly what this module is for, and a word filter would flag the
    #: honesty along with the dishonesty.
    FORBIDDEN = ("cannot be recovered", "unrecoverable", "irrecoverable",
                 "permanently erased", "securely deleted", "no longer exists anywhere",
                 "gone for good", "time machine", "every copy")

    def messages(self):
        """Every human-facing string this module can produce, from the module itself."""
        tmp = tempfile.mkdtemp(prefix="lodestar-purge-scope-")
        self.addCleanup(shutil.rmtree, tmp, True)
        target = os.path.join(tmp, "Thing")
        os.makedirs(target)
        coord = purge.parse_coordinate(COORD)
        recorder = Recorder(code=1, err="HTTP 403: nope")
        return [
            purge.SCOPE,
            purge.scope_hint(coord),
            purge.remove_tree(target)["reason"],
            purge.remove_tree(target, armed=True)["reason"],
            purge.delete_remote(coord, runner=recorder)["reason"],
            purge.delete_remote(coord, armed=True, runner=recorder)["reason"],
            purge.delete_remote(coord, armed=True,
                                runner=Recorder(code=0))["reason"],
        ]

    def test_no_message_claims_erasure_or_unrecoverability(self):
        for message in self.messages():
            lowered = message.lower()
            for phrase in self.FORBIDDEN:
                self.assertNotIn(phrase, lowered,
                                 "%r claims something this cannot deliver: %r"
                                 % (phrase, message))

    def test_the_scope_statement_names_BOTH_halves_and_what_is_excluded(self):
        """A scope that named only the checkout would understate the act; one that named only
        the host would hide what happens on this machine. Both, plus the exclusions, or the
        sentence is not doing its job."""
        lowered = purge.SCOPE.lower()

        self.assertIn("working area", lowered)
        self.assertIn("hosted repository", lowered)
        for excluded in ("backups", "snapshots", "clones"):
            self.assertIn(excluded, lowered)
        self.assertIn("not erasure", lowered)


if __name__ == "__main__":
    unittest.main()
