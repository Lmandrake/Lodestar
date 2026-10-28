"""Tests for lib.gitutil.remote_endpoints.

`git remote set-url origin <new>` only rewrites the fetch URL. If a repo has ever had an
explicit pushurl configured for the same remote — set separately, by hand or by an older
rehome procedure — that pushurl survives untouched. Verified in a fixture on this machine
2026-09-30: fetch moved to an internal host while push stayed on the public one, using
exactly the command the documented rehome procedure calls for. `remote_endpoints` exists so
a rehome can be checked against what git actually has configured, not against the one
config string the rehome command touched.

Everything here runs against throwaway `git init` repos with fake remote URLs — no network,
no bare remote, no real fetch or push is ever attempted. `remote_endpoints` only reads
config; it never talks to a server.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock

from lib import gitutil

PUBLIC = "https://github.com/example/repo.git"
INTERNAL = "git@internal.example.com:example/repo.git"
OTHER_PUSH = "https://github.com/example/mirror.git"


def git(repo, *args):
    subprocess.run(
        ["git", "-C", repo] + list(args),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class RemoteEndpointsTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-pushurls-")
        # The machine's global pushInsteadOf would rewrite the push URL under test.
        patcher = unittest.mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _repo(self, name="repo"):
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        subprocess.run(["git", "init", "--quiet", "-b", "main", path],
                        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return path

    def test_unconfigured_pushurl_reports_fetch_url_for_both(self):
        # Documented git behaviour: with no explicit pushurl, `--push --all` echoes the
        # fetch URL back. That is "push goes where fetch goes", not a misconfiguration —
        # remote_endpoints must report it plainly rather than hiding it as "no push URL".
        repo = self._repo()
        git(repo, "remote", "add", "origin", PUBLIC)

        fetch_url, push_urls = gitutil.remote_endpoints(repo)

        self.assertEqual(fetch_url, PUBLIC)
        self.assertEqual(push_urls, [PUBLIC])

    def test_explicit_differing_pushurl_is_visible(self):
        repo = self._repo()
        git(repo, "remote", "add", "origin", PUBLIC)
        git(repo, "remote", "set-url", "--push", "origin", OTHER_PUSH)

        fetch_url, push_urls = gitutil.remote_endpoints(repo)

        self.assertEqual(fetch_url, PUBLIC)
        self.assertEqual(push_urls, [OTHER_PUSH])
        self.assertNotEqual(fetch_url, push_urls[0])

    def test_regression_set_url_leaves_explicit_pushurl_behind(self):
        """The exact fixture this function exists for.

        origin and its pushurl both start on the public host. The documented rehome
        procedure — `git remote set-url origin <internal>`, nothing else — is run. Fetch
        moves. Push, having been set explicitly, does not: a repo can look rehomed to an
        internal host while every push still reaches the public one.
        """
        repo = self._repo()
        git(repo, "remote", "add", "origin", PUBLIC)
        git(repo, "remote", "set-url", "--push", "origin", PUBLIC)

        git(repo, "remote", "set-url", "origin", INTERNAL)

        fetch_url, push_urls = gitutil.remote_endpoints(repo)

        self.assertEqual(fetch_url, INTERNAL,
                          "fetch should have moved to the internal host")
        self.assertEqual(
            push_urls, [PUBLIC],
            "REGRESSION: 'git remote set-url origin <internal>' does not touch an "
            "explicit pushurl — push is still reaching %r while fetch now reads %r. "
            "A rehome believed complete on the strength of the fetch URL alone is "
            "exactly the hazard remote_endpoints exists to surface." % (PUBLIC, INTERNAL))

    def test_multiple_pushurls_returns_multiple_entries(self):
        repo = self._repo()
        git(repo, "remote", "add", "origin", PUBLIC)
        git(repo, "remote", "set-url", "--push", "origin", PUBLIC)
        git(repo, "remote", "set-url", "--push", "--add", "origin", OTHER_PUSH)

        _, push_urls = gitutil.remote_endpoints(repo)

        self.assertEqual(sorted(push_urls), sorted([PUBLIC, OTHER_PUSH]))

    def test_nonexistent_remote_returns_none_and_empty(self):
        repo = self._repo()
        git(repo, "remote", "add", "origin", PUBLIC)

        fetch_url, push_urls = gitutil.remote_endpoints(repo, remote="upstream")

        self.assertIsNone(fetch_url)
        self.assertEqual(push_urls, [])

    def test_non_repo_path_returns_none_and_empty(self):
        not_a_repo = os.path.join(self.tmp, "plain-dir")
        os.makedirs(not_a_repo)

        fetch_url, push_urls = gitutil.remote_endpoints(not_a_repo)

        self.assertIsNone(fetch_url)
        self.assertEqual(push_urls, [])


if __name__ == "__main__":
    unittest.main()
