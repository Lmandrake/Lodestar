"""Guard: no test module may leak environment into the modules that run after it.

`bin/lib/paths.py` resolves the whole board off `LODESTAR_ROOT`, and almost every module
here points that variable at a temp board in `setUp`. That makes the suite's isolation
depend entirely on those `setUp`s putting the previous value back — and when one of them
does not, the failure lands somewhere else entirely, on a module that never touched the
environment at all.

That is not hypothetical. On 2026-09-01 `tests/test_sense.py` leaked `LODESTAR_ROOT`, and
what broke was `tests.test_sync.TestRegistryParsing` — three tests that read the real
committed `sync/registry.md` and have no `setUp` of their own. They saw a temp board,
`registry.entries()` returned `[]`, and the report blamed the registry. `run_tests.py sync`
passed alone; `run_tests.py sense sync` failed. The mechanism was subtle enough to be worth
naming, because it is the one a careful save/restore still permits:

    self._saved = {k: os.environ.get(k) for k in (...)}   # in setUp
    self.addCleanup(self._restore)                         # reads self._saved at teardown

`addCleanup` of a *bound method* defers the read. Two tests re-entered `setUp()` from inside
the test body, which retook the snapshot — this time capturing the temp values that the
first `setUp` had installed. Both restores then faithfully put the temp values back. The
save/restore was correct; the thing it was reading had been overwritten underneath it.

So the checks below are aimed at that shape, not just at its symptom:

* `EnvRestoredTest` is the runtime backstop. This module sorts last, so anything the suite
  leaks is still set when it runs. It catches leaks from modules that do not exist yet.
* `NoSetUpReEntryTest` and `SnapshotOwnershipTest` are static, and forbid the two ways a
  deferred snapshot read can be made to see the wrong values: re-entering `setUp`, and a
  subclass rebinding the attribute its base is going to restore from.

The durable fix in a module is to stop deferring the read — pass the snapshot *by value*
into the cleanup (`self.addCleanup(_restore_env, saved)` with a module-level function), so
nothing that happens afterwards can change what gets restored. `tests/test_sense.py` is the
worked example.
"""

import ast
import os
import unittest

from tests import PRISTINE_ENV, env_snapshot

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

#: `unittest`'s default discovery pattern, and the order it runs modules in.
_PATTERN_PREFIX = "test"


def _test_modules():
    return sorted(name for name in os.listdir(TESTS_DIR)
                  if name.startswith(_PATTERN_PREFIX) and name.endswith(".py"))


def _parse(filename):
    with open(os.path.join(TESTS_DIR, filename), "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=filename)


def _mentions_environ(node):
    """Whether `node` reads `os.environ`, decided structurally.

    Matched on the attribute node rather than on rendered source: `ast.dump` does not emit
    `os.environ` as text, so a substring test against it silently matches nothing and turns
    the check that depends on it into a test that always passes.
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr == "environ":
            return True
    return False


def _self_attr_targets(node):
    """Every `self.<attr>` assigned from `os.environ` inside `node`, as {attr: lineno}."""
    found = {}
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Assign):
            continue
        touches_env = _mentions_environ(sub.value)
        for target in sub.targets:
            if (isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                    and touches_env):
                found[target.attr] = sub.lineno
    return found


def _fixture_methods(class_node):
    return [node for node in class_node.body
            if isinstance(node, ast.FunctionDef)
            and node.name in ("setUp", "setUpClass")]


class EnvRestoredTest(unittest.TestCase):
    """The runtime backstop, and the reason this file is named to sort last.

    Deliberately a whole-suite check rather than a per-module one: a per-module assertion
    can only cover the modules someone remembered to wire it into, and the leak that
    prompted this came from the module nobody suspected. Anything the suite leaves behind
    is still in `os.environ` by the time this runs.
    """

    def test_the_watched_environment_is_as_the_process_started(self):
        now = env_snapshot()
        keys = set(PRISTINE_ENV) | set(now)
        drift = {key: (PRISTINE_ENV.get(key), now.get(key))
                 for key in sorted(keys)
                 if PRISTINE_ENV.get(key) != now.get(key)}
        if drift:
            lines = ["a test module leaked environment into the rest of the suite:"]
            for key, (was, became) in drift.items():
                lines.append("  %s: %r -> %r" % (key, was, became))
            lines.append("")
            lines.append("The leaked value is usually a `tempfile.mkdtemp` path whose prefix "
                         "names the guilty module (e.g. `lodestar-sense-`). Confirm with "
                         "`run_tests.py <that-module> sync`, then fix the module's cleanup to "
                         "take the saved snapshot BY VALUE — see this file's docstring.")
            self.fail("\n".join(lines))

    def test_this_module_still_sorts_last(self):
        """Otherwise the check above silently stops covering whatever was added after it.

        A guard that can be disabled by adding a file is not a guard, so the ordering
        assumption is asserted rather than written down as a comment.
        """
        modules = _test_modules()
        mine = os.path.basename(__file__)
        if mine.endswith(".pyc"):  # pragma: no cover - defensive
            mine = mine[:-1]
        self.assertEqual(
            modules[-1], mine,
            "%s must be the last test module `unittest` discovers, or it cannot observe "
            "leaks from the ones after it. Rename the new module to sort before it, or "
            "rename this guard." % mine)


class NoSetUpReEntryTest(unittest.TestCase):
    """`setUp` runs once per test, by contract. Calling it again is what did the damage."""

    FORBIDDEN = ("setUp", "setUpClass", "tearDown", "tearDownClass")

    def test_no_test_calls_a_fixture_method_on_itself(self):
        offenders = []
        for filename in _test_modules():
            for node in ast.walk(_parse(filename)):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if (isinstance(func, ast.Attribute)
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "self"
                        and func.attr in self.FORBIDDEN):
                    offenders.append("%s:%d self.%s()" % (filename, node.lineno, func.attr))
        self.assertEqual(
            [], offenders,
            "re-entering a fixture method retakes any snapshot it captures, so a cleanup "
            "that reads the snapshot at teardown restores the wrong values. Factor the "
            "shared work into a helper both the fixture and the test call, and reach a "
            "base class's version with super(), never self.\n  " + "\n  ".join(offenders))


class NoDeletingEnvCleanupTest(unittest.TestCase):
    """A cleanup that *deletes* a variable is only a restore if it was absent to begin with.

    The third shape, and the one the two static guards above do not see: the snapshot is
    never taken at all. `addCleanup(os.environ.pop, KEY, None)` reads correctly — set it,
    remove it afterwards — and is right exactly when `KEY` was unset when the process
    started, which is the case under a bare `run_tests.py` and the reason this survived.

    `bin/check.py` sets `LODESTAR_ROOT` for every script it runs, so under `check.py --tests`
    the same pop *deletes a variable the suite was launched with*. `EnvRestoredTest` then
    fails, in a different module, naming the suite rather than the pop. That is the 2026-09-01
    `check.py --tests` failure: green standalone, one failure under `check.py`, same 1573
    tests. Static because the runtime backstop can only see it when the ambient environment
    happens to carry the key — a guard that needs the bug's own trigger to be present is not
    a guard.
    """

    DELETERS = ("pop", "__delitem__", "clear")

    def test_no_cleanup_deletes_an_environment_variable(self):
        offenders = []
        for filename in _test_modules():
            for node in ast.walk(_parse(filename)):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (isinstance(func, ast.Attribute) and func.attr == "addCleanup"):
                    continue
                for arg in node.args:
                    if (isinstance(arg, ast.Attribute)
                            and arg.attr in self.DELETERS
                            and _mentions_environ(arg)):
                        offenders.append("%s:%d addCleanup(os.environ.%s, …)"
                                         % (filename, node.lineno, arg.attr))
        self.assertEqual(
            [], offenders,
            "deleting a variable is not restoring it: it is correct only when the variable "
            "was absent at process start, and `bin/check.py` sets LODESTAR_ROOT for every "
            "script it runs. Save the prior value and put it back BY VALUE — pop only when "
            "the save says it was absent.\n  " + "\n  ".join(offenders))


class SnapshotOwnershipTest(unittest.TestCase):
    """A subclass must not rebind the attribute its base restores from.

    The other way to make a deferred snapshot read see the wrong values, and one this repo
    has already tripped over once — `tests/test_sync.py` carries a comment about it at the
    point where `TestClaudeConfigStep` deliberately picks `_saved_conf` rather than reusing
    `_saved`. This turns that comment into something that fails.
    """

    def test_no_subclass_rebinds_an_env_snapshot_its_base_owns(self):
        offenders = []
        for filename in _test_modules():
            tree = _parse(filename)
            classes = {node.name: node for node in ast.walk(tree)
                       if isinstance(node, ast.ClassDef)}
            owned = {}
            for name, node in classes.items():
                attrs = {}
                for fixture in _fixture_methods(node):
                    attrs.update(_self_attr_targets(fixture))
                owned[name] = attrs

            for name, node in classes.items():
                for attr, lineno in owned[name].items():
                    for base in self._ancestors(node, classes):
                        if attr in owned.get(base, {}):
                            offenders.append(
                                "%s:%d %s.%s shadows %s.%s"
                                % (filename, lineno, name, attr, base, attr))
        self.assertEqual(
            [], offenders,
            "the base class's cleanup restores from this attribute; rebinding it in a "
            "subclass makes that cleanup put back the subclass's values. Pick a distinct "
            "name.\n  " + "\n  ".join(offenders))

    @staticmethod
    def _ancestors(class_node, classes):
        """In-module ancestor class names, breadth-first. Out-of-module bases are ignored."""
        seen = []
        pending = [base.id for base in class_node.bases if isinstance(base, ast.Name)]
        while pending:
            name = pending.pop(0)
            if name in seen or name not in classes:
                continue
            seen.append(name)
            pending.extend(base.id for base in classes[name].bases
                           if isinstance(base, ast.Name))
        return seen


if __name__ == "__main__":
    unittest.main()
