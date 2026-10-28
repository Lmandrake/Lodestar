"""Wiring: the conversation-record subsystem must have real production callers.

This repo has paid for this test four separate times — `lib/enroll.py`, SWEEP's group
engine, the inert `Former` column, and `_do_rehome`'s `getattr` default were each complete,
correct and reached by nothing but their own tests. CLAUDE.md §7 keeps the tally. A module
with green tests and no caller looks exactly like a working feature, so these assertions are
about the call graph rather than about behaviour.

Comments are stripped before every source check: a test that passes on the comment
*explaining* a requirement is the same defect arriving inside the test, and
`tests/test_tree_live.py` records that happening here already.
"""

import ast
import io
import json
import os
import unittest
from tests import needs_private  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _source(relative):
    with io.open(os.path.join(REPO, relative), "r", encoding="utf-8") as handle:
        return handle.read()


def _stripped(relative):
    """Source with comments and docstrings removed, via the AST."""
    tree = ast.parse(_source(relative))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                             ast.Module)) and ast.get_docstring(node):
            node.body = node.body[1:]
    return ast.unparse(tree)


def _function(relative, name):
    """The AST node of one top-level function."""
    for node in ast.parse(_source(relative)).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError("%s defines no function %r" % (relative, name))


def _code_of(relative, name):
    """One function's source with its docstring removed.

    Checking a function's text for a forbidden token has to exclude the prose that
    *explains* the prohibition, or the docstring saying "the sweep commits this" fails the
    test asserting the step never commits. Same defect as a test passing on a comment,
    arriving from the other direction.
    """
    node = _function(relative, name)
    if ast.get_docstring(node):
        node.body = node.body[1:]
    return ast.unparse(node)


def _calls_in(node):
    found = set()
    for inner in ast.walk(node):
        if isinstance(inner, ast.Call):
            target = inner.func
            if isinstance(target, ast.Name):
                found.add(target.id)
            elif isinstance(target, ast.Attribute):
                found.add(target.attr)
    return found


class TestSweepWiring(unittest.TestCase):
    def test_sync_defines_the_step(self):
        _function("bin/sync.py", "conversations_step")

    def test_main_actually_calls_the_step(self):
        self.assertIn("conversations_step", _calls_in(_function("bin/sync.py", "_sweep")),
                      "a step nothing calls is an inert feature, not a backstop")

    def test_the_step_is_called_before_the_sweep(self):
        """Order is load-bearing: records are sweepable, so they must exist before sweep().

        Written after the sweep, a record sits uncommitted for an hour — the exact gap the
        backstop exists to close.
        """
        main = _function("bin/sync.py", "_sweep")
        positions = {}
        for node in ast.walk(main):
            if isinstance(node, ast.Call):
                target = node.func
                name = target.id if isinstance(target, ast.Name) else None
                if name in ("conversations_step", "sweep") and name not in positions:
                    positions[name] = node.lineno
        self.assertIn("conversations_step", positions)
        self.assertIn("sweep", positions)
        self.assertLess(positions["conversations_step"], positions["sweep"],
                        "conversations_step must run before sweep() or its records miss "
                        "the commit they were written for")

    def test_the_step_writes_through_the_shared_writer(self):
        calls = _calls_in(_function("bin/sync.py", "conversations_step"))
        self.assertIn("write_record", calls)
        self.assertIn("sessions_for", calls)

    def test_the_step_never_runs_git_itself(self):
        """The sweep commits; this step only writes files."""
        body = _code_of("bin/sync.py", "conversations_step")
        for forbidden in ("gitutil.run", "commit", "git_add", "push"):
            self.assertNotIn(forbidden, body,
                             "the step must not touch git: the sweep that follows it does")


class TestHookWiring(unittest.TestCase):
    HOOK = "bin/hooks/conversation_record.py"

    def test_the_hook_exists_and_is_executable(self):
        path = os.path.join(REPO, self.HOOK)
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(os.access(path, os.X_OK), "a non-executable hook fails silently")

    @needs_private("claude/settings-base.json")
    def test_settings_base_registers_a_session_end_hook_pointing_at_it(self):
        with io.open(os.path.join(REPO, "claude/settings-base.json"),
                     "r", encoding="utf-8") as handle:
            base = json.load(handle)
        hooks = base.get("hooks", {})
        self.assertIn("SessionEnd", hooks,
                      "the fallback trigger Lukas chose reaches no machine until it is "
                      "declared in base")
        blob = json.dumps(hooks["SessionEnd"])
        self.assertIn("conversation_record.py", blob)
        self.assertIn("$HOME", blob,
                      "hook commands must be written as $HOME/... — literal /Users paths "
                      "cannot work on a WSL machine")

    def test_the_hook_never_runs_git(self):
        """A commit under SessionEnd's 1.5s budget would race peers for index.lock."""
        body = _stripped(self.HOOK)
        for forbidden in ("subprocess", "gitutil", "git "):
            self.assertNotIn(forbidden, body,
                             "the hook must not run git; the hourly sweep commits instead")

    def test_the_hook_cannot_exit_nonzero(self):
        """Exit 2 surfaces an error notice to the user; nothing here is worth that."""
        tree = ast.parse(_source(self.HOOK))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                target = node.func
                name = getattr(target, "attr", None) or getattr(target, "id", None)
                if name == "exit":
                    for arg in node.args:
                        self.assertTrue(
                            isinstance(arg, ast.Constant) and arg.value == 0
                            or isinstance(arg, ast.Call),
                            "a SessionEnd hook must not raise an error notice at the user")


class TestRitualWiring(unittest.TestCase):
    @needs_private("claude/global/55-handoff-on-reboot.md")
    def test_the_handoff_ritual_names_the_capture_step(self):
        """Lukas's primary trigger was 'as part of handoff'. It has to be written down."""
        text = _source("claude/global/55-handoff-on-reboot.md")
        self.assertIn("conversations.py", text,
                      "the handoff ritual is followed by reading it; a step absent from "
                      "the text is a step nobody runs")


if __name__ == "__main__":
    unittest.main()
