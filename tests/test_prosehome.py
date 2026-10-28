"""prosehome.place: the one decision about where a repo's session prose may be written."""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin"))

from lib import privatestore, prosehome  # noqa: E402
from tests.test_reckoning import make_checkout  # noqa: E402
from tests import needs_private, real_home_key  # noqa: E402

GITHUB = "git@github.com:someone/Thing.git"
PRIVATE_URL = "git@git.private.example:someone/Thing.git"


class ProseHomeBase(unittest.TestCase):
    def setUp(self):
        self.d = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        self.store = make_checkout(os.path.join(self.d, "private-store"), PRIVATE_URL)
        self.github = make_checkout(os.path.join(self.d, "Thing"), GITHUB)
        self._env = {k: os.environ.get(k) for k in ("LODESTAR_PRIVATE_ROOT", "LODESTAR_MACHINE")}
        os.environ["LODESTAR_PRIVATE_ROOT"] = self.store
        os.environ["LODESTAR_MACHINE"] = "mt-002"   # a `work` Mac in machines.md
        prosehome.reset_cache()
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        prosehome.reset_cache()


class PlaceTest(ProseHomeBase):
    def test_a_github_repo_goes_to_the_overlay(self):
        got = prosehome.place(self.github, "conversations")
        self.assertEqual(got.where, prosehome.STORE)
        self.assertEqual(got.path, os.path.join(self.store, "projects", "Thing", "conversations"))

    def test_a_private_repo_keeps_its_prose(self):
        repo = make_checkout(os.path.join(self.d, "Internal"), PRIVATE_URL)
        got = prosehome.place(repo, "handoffs")
        self.assertEqual((got.where, got.path),
                         (prosehome.REPO, os.path.join(repo, "handoffs")))

    def test_a_repo_with_a_github_pushurl_beside_a_private_fetch_is_not_private(self):
        repo = make_checkout(os.path.join(self.d, "Mixed"), PRIVATE_URL)
        os.system("git -C %s remote set-url --add --push origin %s" % (repo, GITHUB))
        self.assertEqual(prosehome.place(repo, "x").where, prosehome.STORE)
        self.assertNotEqual(prosehome.place(repo, "x").path, os.path.join(repo, "x"))

    def test_a_work_machine_without_a_checkout_withholds(self):
        os.environ["LODESTAR_PRIVATE_ROOT"] = os.path.join(self.d, "absent")
        got = prosehome.place(self.github, "conversations")
        self.assertEqual((got.where, got.path), (prosehome.WITHHELD, None))

    @needs_private("machines.md")
    def test_tower_without_a_checkout_writes_in_the_repo(self):
        os.environ["LODESTAR_PRIVATE_ROOT"] = os.path.join(self.d, "absent")
        os.environ["LODESTAR_MACHINE"] = real_home_key()
        got = prosehome.place(self.github, "conversations")
        self.assertEqual((got.where, got.path),
                         (prosehome.REPO, os.path.join(self.github, "conversations")))

    def test_an_unknown_machine_counts_as_work(self):
        os.environ["LODESTAR_PRIVATE_ROOT"] = os.path.join(self.d, "absent")
        os.environ["LODESTAR_MACHINE"] = "never-registered"
        self.assertEqual(prosehome.place(self.github, "x").where, prosehome.WITHHELD)

    def test_a_private_store_that_could_push_to_github_withholds_even_on_tower(self):
        bad = make_checkout(os.path.join(self.d, "bad-private"), GITHUB)
        os.environ["LODESTAR_PRIVATE_ROOT"] = bad
        os.environ["LODESTAR_MACHINE"] = "tower"
        self.assertEqual(prosehome.place(self.github, "x").where, prosehome.WITHHELD)

    def test_ensure_dir_refuses_a_withheld_placement(self):
        os.environ["LODESTAR_PRIVATE_ROOT"] = os.path.join(self.d, "absent")
        with self.assertRaises(privatestore.Unavailable):
            prosehome.ensure_dir(prosehome.place(self.github, "x"))

    def test_ensure_dir_refuses_a_symlink_out_of_the_checkout(self):
        os.makedirs(os.path.join(self.store, "projects"))
        outside = os.path.join(self.d, "outside")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(self.store, "projects", "Thing"))
        with self.assertRaises(privatestore.Rejected):
            prosehome.ensure_dir(prosehome.place(self.github, "conversations"))


if __name__ == "__main__":
    unittest.main()
