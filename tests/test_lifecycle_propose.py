"""`lifecycle.py propose` — what gets STORED, and what the operator is shown before approving.

🔴 **Found by exercising the rehome executor against a real registered repo, 2026-10-01.**
`cmd_propose` applied `tilde()` to both `old` and `new` for every kind. For a `rehome`, `new` is a
**git URL, not a path** — so `tilde()`'s `os.path.abspath` resolved it against the current working
directory and stored
`~/dev/Lodestar/git@git.private.example:mandrake/ThrowawayLifecycle.git`. Every rehome plan ever
authored through the CLI therefore carried a corrupted target, and `_do_rehome` would have run
`git remote set-url origin <that>` — pointing a repo at a remote that cannot exist.

**This compounds the bug fixed the day before, and that is the part worth remembering.**
`_do_rehome` used to read `.returncode` on a `GitResult` that exposes `.code`, so every rehome
returned a permanent *safe refusal*. Fixing that unmasked this one: a rehome would now proceed and
silently repoint the remote at garbage. **A fix that removes a refusal can convert a harmless
failure into a destructive success**, so the thing a refusal was hiding has to be checked before
the refusal is removed.

The plans store is append-only with earliest-row-wins, so a corrupted plan id cannot be superseded
— the row has to be corrected in place, which is the practice the 2026-09-30 path correction
already established.
"""

import os
import shutil
import tempfile
import unittest

import lifecycle
from lib import plans
from tests import needs_private  # noqa: E402


class Args(object):
    """The attribute bag `cmd_propose` reads, so no argparse plumbing is needed."""

    def __init__(self, **kw):
        self.plan_id = ""
        self.kind = "rename"
        self.repo_id = "throwaway"
        self.machines = "all"
        self.old = ""
        self.new = ""
        self.disposition = ""
        self.note = "test"
        for key, value in kw.items():
            setattr(self, key, value)


class ProposeStoresTheRightShapeTest(unittest.TestCase):

    URL = "git@git.private.example:mandrake/Thing.git"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-propose-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.plans_file = os.path.join(self.tmp, "plans.md")
        self.tombstones_file = os.path.join(self.tmp, "tombstones.md")
        from lib import paths
        self._patch(paths, "plans_file", lambda: self.plans_file)
        self._patch(paths, "tombstones_file", lambda: self.tombstones_file)
        # A plan for an unregistered id only WARNS, which keeps these tests off the real registry.
        self._patch(lifecycle, "_row_for", lambda repo_id: None)

    def _patch(self, module, name, value):
        original = getattr(module, name)
        setattr(module, name, value)
        self.addCleanup(setattr, module, name, original)

    def stored(self, **kw):
        code = lifecycle.cmd_propose(Args(**kw))
        self.assertEqual(0, code, "propose should have succeeded")
        store = plans.load_plans(self.plans_file)
        self.assertEqual(1, len(store), "expected exactly one plan")
        return list(store.values())[0]

    def test_a_rehome_target_is_stored_as_the_URL_it_was_given(self):
        plan = self.stored(kind="rehome", new=self.URL)
        self.assertEqual(self.URL, plan.new,
                         "a rehome target is a URL and must never be path-normalised")
        self.assertNotIn("/dev/Lodestar/", plan.new)
        self.assertFalse(plan.new.startswith("~"))

    def test_an_https_rehome_target_survives_too(self):
        url = "https://github.com/Lmandrake/Thing.git"
        self.assertEqual(url, self.stored(kind="rehome", new=url).new)

    def test_a_rename_still_tildes_both_paths(self):
        """The regression guard: the fix must not stop paths being stored ~-relative.

        A plan travels to machines with different home directories, so an expanded path is a
        one-machine fact wearing fleet-wide clothes — a WSL machine's checkouts live under `/mnt/...`.
        """
        home = os.path.expanduser("~")
        plan = self.stored(kind="rename",
                           old=os.path.join(home, "dev", "Old"),
                           new=os.path.join(home, "dev", "New"))
        self.assertEqual("~/dev/Old", plan.old)
        self.assertEqual("~/dev/New", plan.new)

    def test_a_quarantine_still_tildes_its_path(self):
        home = os.path.expanduser("~")
        plan = self.stored(kind="quarantine", old=os.path.join(home, "dev", "Gone"))
        self.assertEqual("~/dev/Gone", plan.old)

    def test_a_rehome_still_tildes_its_old_PATH_field(self):
        """`old` is a path for every kind — `_converge_one` resolves it as the repo's location."""
        home = os.path.expanduser("~")
        plan = self.stored(kind="rehome", old=os.path.join(home, "dev", "Here"), new=self.URL)
        self.assertEqual("~/dev/Here", plan.old)
        self.assertEqual(self.URL, plan.new)


@needs_private("machines.md")
class RehomeConsequenceNamesTheCurrentEndpointTest(unittest.TestCase):
    """The consequence print is the whole safety argument for an irreversible-adjacent act.

    It read `plan.old` as the "from" endpoint, but for a rehome `old` is the *path* field and is
    normally empty — so the operator was shown `repoint origin from  to <url>`, with nothing where
    the endpoint being abandoned should be. Approving on that is approving blind.
    """

    def test_the_from_endpoint_is_never_rendered_blank(self):
        from lib import registry
        row = registry.Entry(name="Thing", path="~/dev/Thing", branch="main", sync=True,
                             notes="", repo_id="throwaway",
                             remote="git@example.com:x/Thing.git")
        original = lifecycle._row_for
        lifecycle._row_for = lambda repo_id: row
        self.addCleanup(setattr, lifecycle, "_row_for", original)
        plan = plans.Plan(
            plan_id="rehome-x-20261001", kind="rehome", repo_id="throwaway",
            machines=("all",), old="", new="git@example.com:x/Thing.git",
            disposition="", created_at="2026-10-01T10:00:00-07:00",
            created_by="test", note="")
        text = "\n".join(detail for _, _, detail in lifecycle.consequence(plan))
        self.assertNotIn("origin from  to", text,
                         "an empty 'from' endpoint means the operator cannot see what changes")
        self.assertIn("git@example.com:x/Thing.git", text)
