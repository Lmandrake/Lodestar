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

🔴 **Not every drift `EnvRestoredTest` sees is a leak.** Two files pre-set the test
environment, and only `tests/__init__.py` runs before `PRISTINE_ENV`; `tests/conftest.py`
runs after it. A variable pre-set in conftest alone is therefore *missing from the baseline*,
which looks exactly like a leak and has the opposite fix. Measured 2026-11-10:
`LODESTAR_TOMBSTONE_ROOT` and `LODESTAR_PEER_NUDGE` sat in that state, and the guard's old
advice ("the `mkdtemp` prefix names the guilty module") sent readers to
`tests/test_tombstones.py`, which sets no environment variable and merely shares a temp-dir
prefix. A prefix names a string, not a setter. `EnvRestoredTest` now decides which cause it
is from the two files' contents, and
`test_every_variable_conftest_pre_sets_is_also_in_the_baseline` catches the ordering gap
statically, as itself, before it can be misread.
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


def _preset_in(filename, key):
    """Whether `tests/<filename>` names `key` at all (it only ever names one to pre-set it)."""
    try:
        with open(os.path.join(TESTS_DIR, filename), "r", encoding="utf-8") as handle:
            return key in handle.read()
    except OSError:  # pragma: no cover - defensive
        return False


def _drift_diagnosis(key):
    """Which of the two causes a drifted `key` has, as advice the reader can act on.

    The two look identical in the drift line and lead to opposite fixes, so the guard decides
    between them instead of making the reader guess. Measured 2026-11-10: the old message said
    the leaked value's `mkdtemp` prefix names the guilty module, which sent a reader to
    `tests/test_tombstones.py` for `lodestar-tombstones-` — a module that sets no environment
    variable and merely happens to use a matching temp-dir prefix.
    """
    if _preset_in("conftest.py", key) and not _preset_in("__init__.py", key):
        return ("BASELINE DEFECT, not a leaking module: tests/conftest.py sets %s, but "
                "tests/__init__.py does not pre-set it before PRISTINE_ENV, so the baseline "
                "was captured without it. Nothing leaked. Fix: add the same "
                "`if not os.environ.get(%r)` block to tests/__init__.py beside the sweep-lock "
                "and untracked ones, which makes conftest's own line a no-op." % (key, key))
    return ("LEAK: no pre-baseline line in tests/__init__.py claims %s, so a test module set "
            "it and did not put it back. A `tempfile.mkdtemp` path's prefix is a HINT ONLY — "
            "it names the prefix string, not the setter, and an innocent module can share it. "
            "Confirm by grepping the suite for %r (the module that *assigns* it), then "
            "`run_tests.py <that-module> sync`, and fix that module's cleanup to take the "
            "saved snapshot BY VALUE — see this file's docstring." % (key, key))


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
            lines = ["the watched environment is not as the process started:"]
            for key, (was, became) in drift.items():
                lines.append("  %s: %r -> %r" % (key, was, became))
            lines.append("")
            lines.append("Two different causes produce identical-looking lines above, and they "
                         "have opposite fixes. Per variable:")
            for key in drift:
                lines.append("  %s\n    %s" % (key, _drift_diagnosis(key)))
            self.fail("\n".join(lines))

    def test_every_variable_conftest_pre_sets_is_also_in_the_baseline(self):
        """The ordering gap, caught statically — before it can be read as a leak.

        Two files pre-set the test environment and only one of them runs before
        `PRISTINE_ENV`: `tests/__init__.py` is imported first and feeds the baseline,
        `tests/conftest.py` runs after it. So a variable added to conftest alone makes the
        runtime check above fail forever, in a module that did nothing wrong. Measured
        2026-11-10: `LODESTAR_TOMBSTONE_ROOT` and `LODESTAR_PEER_NUDGE` were both in that
        state, and the first was read as a leak from `tests/test_tombstones.py`.

        Static, because the runtime check reports this as a leak rather than as itself.
        """
        with open(os.path.join(TESTS_DIR, "conftest.py"), "r", encoding="utf-8") as handle:
            conftest = ast.parse(handle.read(), filename="conftest.py")
        preset = set()
        for node in ast.walk(conftest):
            if (isinstance(node, ast.Subscript)
                    and isinstance(node.value, ast.Attribute)
                    and node.value.attr == "environ"
                    and isinstance(node.slice, ast.Constant)
                    and isinstance(node.slice.value, str)
                    and (node.slice.value.startswith("LODESTAR_")
                         or node.slice.value in ("HOME", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID"))):
                preset.add(node.slice.value)
        missing = sorted(key for key in preset if not _preset_in("__init__.py", key))
        self.assertEqual(
            [], missing,
            "tests/conftest.py pre-sets these, but tests/__init__.py does not, so "
            "PRISTINE_ENV is captured without them and the runtime check above fails "
            "forever — reported as a leak from whichever module shares the temp-dir "
            "prefix. Add the same `if not os.environ.get(...)` block to "
            "tests/__init__.py, before PRISTINE_ENV: %s" % ", ".join(missing))

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
