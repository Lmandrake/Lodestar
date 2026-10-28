"""The public-mirror exporter (bin/lib/publish/): every scanner class blocks a release and leaves
the public remote untouched; a clean tree goes out as one noreply commit.

Every fixture is synthetic and lives in a temp dir; the real private-store lists are never read.
Canary strings are assembled at run time so this file itself carries none of them.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin"))

try:
    import tomllib  # noqa: F401
except ImportError:  # pragma: no cover
    raise unittest.SkipTest("publish needs Python 3.11+ (tomllib)")

from lib.publish import build as build_mod  # noqa: E402
from lib.publish import common  # noqa: E402
from lib.publish import release as release_mod  # noqa: E402
from lib.publish import review as review_mod  # noqa: E402
from lib.publish import scan as scan_mod  # noqa: E402
from lib.publish.common import NOREPLY, PublishError  # noqa: E402
from tests.test_reckoning import make_checkout  # noqa: E402

HAVE_GITLEAKS = shutil.which("gitleaks") is not None
NASA = "nasa" + ".gov"
CANARIES = {
    "marker": "contact canary.person@work.private.example today",
    "outlook": "item AAMk" + "AGI2TG93AAAx" * 3 + " here",
    "name": "a note from Quentin Arbuckle about it",
    "home_path": "see /Users/" + "mandrake/dev/x",
    "term": "funded by ZORBLAX this year",
    "machine_id": "ran on mt-" + "123456 last night",
    "email": "write to someone@" + "gmail.com",
    "host": "the gateway acct-quokka-77 answered",
    "export_control": "this is " + "IT" + "AR material",
}
EXPECT = {"marker": "marker", "outlook": "marker", "name": {"name", "name_form"}, "home_path": "home_path",
          "term": "term", "machine_id": "machine_id", "email": "email", "host": "host",
          "export_control": "export_control"}
FAKE_AWS = "aws_access_key_id = " + "AKIA" + "QYLPMN5HHHFPZAM2\n"

MANIFEST = """repo = "Demo"
public_remote = "%(remote)s"
require_gitleaks = %(gl)s
include = ["README.md", "src/**", "docs/"]
exclude = ["src/private/**"]
"""


def git(repo, *args, env=None):
    full = dict(os.environ)
    full.update(env or {})
    return subprocess.run(["git", "-C", repo] + list(args), check=True, capture_output=True,
                          text=True, env=full).stdout


class PublishBase(unittest.TestCase):
    def setUp(self):
        self.d = os.path.realpath(tempfile.mkdtemp(prefix="lodestar-publish-"))
        self.addCleanup(shutil.rmtree, self.d, True)
        self.store = make_checkout(os.path.join(self.d, "private-store"),
                                 "git@git.private.example:x/private-store.git")
        self.pub = os.path.join(self.store, "publish")
        os.makedirs(self.pub)
        for name, body in (("names", "Quentin Arbuckle\n"), ("terms", "ZORBLAX\n"),
                           ("hosts", "acct-quokka-77\n")):
            with open(os.path.join(self.pub, name + ".txt"), "w") as fh:
                fh.write("# synthetic\n" + body)
        self.sources = os.path.join(self.d, "src")
        self.origin = os.path.join(self.d, "origin.git")
        self.public = os.path.join(self.d, "public.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.origin], check=True)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.public], check=True)
        self.repo = os.path.join(self.sources, "Demo")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "remote", "add", "origin", self.origin)
        self.write("README.md", "# Demo\n\nA clean demo.\n")
        self.write("src/demo.py", "print('hello')\n")
        self.write("src/private/secret.md", "kept out\n")
        self.write("docs/guide.md", "Guide.\n")
        self.write("notes.md", "not included\n")
        self.commit_push("init")
        self.manifest(remote=self.public)
        # Bound per call (setUp may run again inside a subTest loop), so every original survives.
        saved = {k: os.environ.get(k) for k in
                 ("LODESTAR_PRIVATE_ROOT", common.SOURCES_ENV, "LODESTAR_STATE_DIR",
                  review_mod.REVIEW_ENV)}
        self.addCleanup(self._restore, saved)
        os.environ["LODESTAR_PRIVATE_ROOT"] = self.store
        os.environ[common.SOURCES_ENV] = self.sources
        os.environ["LODESTAR_STATE_DIR"] = os.path.join(self.d, "state")
        self.set_reviewer("VERDICT: CLEAN")

    def set_reviewer(self, answer):
        """A synthetic cold reader: a script that prints `answer` (the real one is a model)."""
        script = os.path.join(self.d, "reviewer.py")
        with open(script, "w") as fh:
            fh.write("import sys\nprint(%r)\n" % answer)
        os.environ[review_mod.REVIEW_ENV] = "%s %s {prompt}" % (sys.executable, script)

    @staticmethod
    def _restore(saved):
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def write(self, rel, text, mode="w"):
        full = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, mode) as fh:
            fh.write(text)

    def commit_push(self, msg="c"):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", msg)
        git(self.repo, "push", "-q", "-u", "origin", "main")

    def manifest(self, remote="", gl=None, extra=""):
        gl = HAVE_GITLEAKS if gl is None else gl
        with open(os.path.join(self.pub, "Demo.toml"), "w") as fh:
            fh.write(MANIFEST % {"remote": remote, "gl": "true" if gl else "false"} + extra)

    def public_refs(self):
        return subprocess.run(["git", "-C", self.public, "for-each-ref"], capture_output=True,
                              text=True, check=True).stdout.strip()

    def scan(self):
        publish = common.private_dir()
        man = build_mod.manifest_mod.load("Demo", publish)
        report = scan_mod.scan_tree(common.tree_dir("Demo"), man, scan_mod.load_lists(publish))
        scan_mod.write_report(publish, report)
        return report

    def full_release(self, words="ship it"):
        build_mod.build("Demo")
        report = self.scan()
        review_mod.review("Demo")
        release_mod.diff("Demo")
        return report, release_mod.release("Demo", words)


class CleanControl(PublishBase):
    def test_clean_tree_passes_and_releases_one_noreply_commit(self):
        report, rec = self.full_release("Lukas: release it")
        self.assertTrue(report["passed"], report["hits"])
        self.assertTrue(rec["pushed"])
        log = git(self.public, "log", "--format=%H|%ae|%ce|%an|%B", "main")
        self.assertEqual(log.count(NOREPLY), 2)
        self.assertEqual(int(git(self.public, "rev-list", "--count", "main").strip()), 1)
        src_head = git(self.repo, "rev-parse", "HEAD").strip()
        self.assertNotIn(src_head[:7], log)
        files = git(self.public, "ls-tree", "-r", "--name-only", "main").split()
        self.assertEqual(sorted(files), ["README.md", "docs/guide.md", "src/demo.py"])
        rel = [json.loads(l) for l in open(os.path.join(self.pub, "releases.jsonl"))]
        self.assertEqual(rel[-1]["approved_by"], "Lukas: release it")
        self.assertEqual(rel[-1]["source_head"], src_head)

    def test_exclude_beats_include(self):
        build_mod.build("Demo")
        tree = common.tree_dir("Demo")
        self.assertFalse(os.path.exists(os.path.join(tree, "src/private/secret.md")))
        self.assertFalse(os.path.exists(os.path.join(tree, "notes.md")))
        self.assertTrue(os.path.exists(os.path.join(tree, "src/demo.py")))

    def test_exclude_beats_explicit_include_of_same_file(self):
        with open(os.path.join(self.pub, "Demo.toml"), "w") as fh:
            fh.write('repo = "Demo"\ninclude = ["README.md", "notes.md"]\n'
                     'exclude = ["notes.md"]\nrequire_gitleaks = false\n')
        build_mod.build("Demo")
        self.assertFalse(os.path.exists(os.path.join(common.tree_dir("Demo"), "notes.md")))

    def test_second_release_is_a_fast_forward_and_unchanged_tree_refused(self):
        self.full_release()
        self.write("src/demo.py", "print('two')\n")
        self.commit_push("two")
        self.full_release()
        self.assertEqual(int(git(self.public, "rev-list", "--count", "main").strip()), 2)
        with self.assertRaisesRegex(PublishError, "nothing changed"):
            self.full_release()

    def test_no_public_remote_commits_locally_and_pushes_nothing(self):
        self.manifest(remote="")
        _report, rec = self.full_release()
        self.assertFalse(rec["pushed"])
        self.assertEqual(self.public_refs(), "")


class Canaries(PublishBase):
    def _plant_and_try(self, rel, text):
        self.write(rel, text)
        self.commit_push("plant")
        build_mod.build("Demo")
        report = self.scan()
        release_mod.diff("Demo")
        with self.assertRaises(PublishError):
            release_mod.release("Demo", "ship it")
        self.assertEqual(self.public_refs(), "", "the public remote was touched")
        return report

    def _canary(self, key):
        expect = EXPECT[key] if isinstance(EXPECT[key], set) else {EXPECT[key]}
        report = self._plant_and_try("docs/c_%s.md" % key, "x\n%s\n" % CANARIES[key])
        self.assertFalse(report["passed"])
        self.assertEqual({h["class"] for h in report["hits"]}, expect, report["hits"])
        self.assertEqual(report["hits"][0]["path"], "docs/c_%s.md" % key)

    def test_canary_marker(self):
        self._canary("marker")

    def test_canary_outlook_id(self):
        self._canary("outlook")

    def test_canary_name(self):
        self._canary("name")

    def test_canary_home_path(self):
        self._canary("home_path")

    def test_canary_term(self):
        self._canary("term")

    def test_canary_machine_id(self):
        self._canary("machine_id")

    def test_canary_email(self):
        self._canary("email")

    def test_canary_host(self):
        self._canary("host")

    def test_canary_export_control(self):
        self._canary("export_control")

    def test_nasa_host_and_own_address_are_not_exempt(self):
        report = self._plant_and_try("docs/a.md", "owner@work.private.example\n"
                                     "https://example.ames." + NASA + "/x\n")
        classes = {h["class"] for h in report["hits"]}
        self.assertIn("marker", classes)
        self.assertIn("nasa_host", classes)

    def test_canary_in_a_path_blocks(self):
        report = self._plant_and_try("docs/ZORBLAX-plan.md", "fine\n")
        self.assertIn("term", {h["class"] for h in report["hits"]})

    def test_name_across_a_line_break_and_odd_case(self):
        report = self._plant_and_try("docs/a.md", "said quentin\n  ARBUCKLE yesterday\n")
        self.assertIn("name", {h["class"] for h in report["hits"]})

    def test_zero_width_and_fullwidth_evasion(self):
        report = self._plant_and_try("docs/a.md", "ZOR​BLAX and Ｑuentin Arbuckle\n")
        self.assertEqual({h["class"] for h in report["hits"]},
                         {"term", "name", "name_form"})

    def test_reserved_and_noreply_emails_pass(self):
        self.write("docs/a.md", "a@example.com b@x.example.org %s c@foo.invalid\n" % NOREPLY)
        self.commit_push()
        build_mod.build("Demo")
        self.assertTrue(self.scan()["passed"])

    @unittest.skipUnless(HAVE_GITLEAKS, "gitleaks not installed")
    def test_fake_aws_key_blocks_via_gitleaks(self):
        report = self._plant_and_try("src/cfg.ini", FAKE_AWS)
        self.assertIn("secret", {h["class"] for h in report["hits"]})
        self.assertNotIn("QYLPMN5HHHFPZAM2", json.dumps(report))

    @unittest.skipUnless(HAVE_GITLEAKS, "gitleaks not installed")
    def test_in_tree_gitleaks_ignores_do_not_exempt(self):
        self.write("src/.gitleaksignore", "*\n")
        self.write("src/.gitleaks.toml", '[allowlist]\npaths = [".*"]\n')
        report = self._plant_and_try("src/cfg.ini", FAKE_AWS.strip() + "  # gitleaks:allow\n")
        self.assertIn("secret", {h["class"] for h in report["hits"]})

    def test_gitleaks_missing_fails_closed_when_required(self):
        self.manifest(remote=self.public, gl=True)
        build_mod.build("Demo")
        with mock.patch.object(scan_mod.shutil, "which", return_value=None):
            with self.assertRaisesRegex(PublishError, "gitleaks missing"):
                self.scan()

    def test_pre_push_hook_blocks_a_hand_made_commit(self):
        self.full_release()
        stage = common.staging_dir("Demo")
        with open(os.path.join(stage, "leak.md"), "w") as fh:
            fh.write(CANARIES["marker"] + "\n")
        env = release_mod._clean_env()
        git(stage, "add", "leak.md", env=env)
        git(stage, "commit", "-q", "-m", "sneak", env=env)
        before = self.public_refs()
        out = subprocess.run(["git", "-C", stage, "push", "origin", "HEAD:refs/heads/main"],
                             capture_output=True, text=True, env=env)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("pre-push scan found", out.stderr)
        self.assertEqual(self.public_refs(), before)

    def test_pre_push_hook_refuses_tags_and_other_branches(self):
        self.full_release()
        stage = common.staging_dir("Demo")
        env = release_mod._clean_env()
        git(stage, "tag", "v1", env=env)
        before = self.public_refs()
        for spec in ("refs/tags/v1", "HEAD:refs/heads/other"):
            with self.subTest(spec=spec):
                out = subprocess.run(["git", "-C", stage, "push", "origin", spec],
                                     capture_output=True, text=True, env=env)
                self.assertNotEqual(out.returncode, 0)
                self.assertEqual(self.public_refs(), before)

    def test_pre_push_hook_blocks_a_foreign_identity(self):
        self.full_release()
        stage = common.staging_dir("Demo")
        with open(os.path.join(stage, "ok.md"), "w") as fh:
            fh.write("fine\n")
        env = dict(release_mod._clean_env(), GIT_AUTHOR_EMAIL="me@example.com")
        git(stage, "add", "ok.md", env=env)
        git(stage, "commit", "-q", "-m", "x", env=env)
        out = subprocess.run(["git", "-C", stage, "push", "origin", "HEAD:refs/heads/main"],
                             capture_output=True, text=True, env=release_mod._clean_env())
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("not the noreply", out.stderr)


class Evasion(unittest.TestCase):
    """Scanner matching on its own, no repo: shapes that once produced a confident zero."""

    def setUp(self):
        self.lists = scan_mod.Lists(["Quentin Arbuckle"], ["ZORBLAX", "R&TD"],
                                    ["acct-quokka-77"], {})

    def classes(self, text):
        return {h[0] for h in scan_mod.text_hits(scan_mod.normalise(text), self.lists)}

    def test_name_shapes(self):
        for text in ("**Quentin** Arbuckle", "quentin_arbuckle", "QuentinArbuckle",
                     "Quentin Arbuckle's", "Quentin\n\nArbuckle"):
            with self.subTest(text=text):
                self.assertIn("name", self.classes(text))

    def test_name_words_far_apart_do_not_join(self):
        self.assertNotIn("name", self.classes("Quentin is not Arbuckle"))

    def test_utf16_and_binary_strings_are_read(self):
        for data in ("x ZORBLAX y".encode("utf-16"), "x ZORBLAX y".encode("utf-16-le"),
                     b"\x89PNG\x00\x01tEXtComment\x00ZORBLAX\x00\xff\xfe"):
            with self.subTest(data=data[:12]):
                hits = {h[0] for t in scan_mod.decodings(data)
                        for h in scan_mod.text_hits(t, self.lists)}
                self.assertIn("term", hits)

    def test_encoded_shapes(self):
        cases = {'{"n":"Quentin\\u0020Arbuckle"}': "name", "Quentin%20Arbuckle": "name",
                 '{"n":"UXVlbnRpbiBBcmJ1Y2tsZQ=="}': "name", "someone&#64;gmail.com": "email",
                 "someone[at]gmail.com": "email", "Quen\u034ftin Arbuckle": "name",
                 "Quen\u200btin Arbuckle": "name", "ITAR_material": "export_control",
                 "mt-123456_backup": "machine_id"}
        for text, cls in cases.items():
            with self.subTest(text=text):
                hits = {h[0] for t in scan_mod.decodings(text.encode("utf-8"))
                        for h in scan_mod.text_hits(t, self.lists)}
                self.assertIn(cls, hits)

    def test_term_shapes(self):
        for text in ("ZORBLAX_E9", "R&TD funds", "(ZORBLAX)"):
            with self.subTest(text=text):
                self.assertIn("term", self.classes(text))


class ReviewOne(PublishBase):
    """Regressions from the 2026-10-25 second-model review (kept in the private store)."""

    def _push(self, stage, spec="HEAD:refs/heads/main"):
        return subprocess.run(["git", "-C", stage, "push", "origin", spec], capture_output=True,
                              text=True, env=release_mod._clean_env())

    def _commit(self, stage, rel, text, msg="x"):
        env = release_mod._clean_env()
        full = os.path.join(stage, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        if text is None:
            git(stage, "rm", "-q", rel, env=env)
        else:
            with open(full, "w") as fh:
                fh.write(text)
            git(stage, "add", rel, env=env)
        git(stage, "commit", "-q", "-m", msg, env=env)

    def test_leak_added_then_deleted_in_one_push_is_refused(self):
        self.full_release()
        stage = common.staging_dir("Demo")
        self._commit(stage, "docs/leak.txt", "ZORBLAX\n")
        self._commit(stage, "docs/leak.txt", None)
        before = self.public_refs()
        out = self._push(stage)
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(self.public_refs(), before)

    def test_replace_refs_cannot_hide_a_blob_from_the_hook(self):
        self.full_release()
        stage = common.staging_dir("Demo")
        self._commit(stage, "docs/leak.txt", "ZORBLAX\n")
        env = release_mod._clean_env()
        bad = git(stage, "rev-parse", "HEAD:docs/leak.txt", env=env).strip()
        good = subprocess.run(["git", "-C", stage, "hash-object", "-w", "--stdin"], input="safe\n",
                              capture_output=True, text=True, env=env, check=True).stdout.strip()
        git(stage, "replace", bad, good, env=env)
        before = self.public_refs()
        self.assertNotEqual(self._push(stage).returncode, 0)
        self.assertEqual(self.public_refs(), before)

    @unittest.skipUnless(HAVE_GITLEAKS, "gitleaks not installed")
    def test_gitleaks_default_path_allowlist_does_not_exempt(self):
        self.manifest(remote=self.public, extra='\nbinaries = []\n'.replace("binaries = []", ""))
        for rel in ("docs/gitleaks.toml", "docs/package-lock.json", "docs/leak.svg"):
            self.write(rel, FAKE_AWS)
        self.commit_push()
        build_mod.build("Demo")
        paths = {h["path"] for h in self.scan()["hits"] if h["class"] == "secret"}
        self.assertEqual(paths, {"docs/gitleaks.toml", "docs/package-lock.json", "docs/leak.svg"})

    def test_replacement_cannot_grow_a_file_past_the_limit(self):
        self.write("src/big.txt", "x" * 20000 + "\n")
        self.commit_push()
        self.manifest(remote=self.public, extra=(
            '\n[[replace]]\npaths = "src/big.txt"\nfind = "x"\nwith = "%s"\n' % ("x" * 101)))
        with self.assertRaisesRegex(PublishError, "after replacements"):
            build_mod.build("Demo")

    def test_tree_hash_is_unambiguous(self):
        a, b = os.path.join(self.d, "ta"), os.path.join(self.d, "tb")
        os.makedirs(a)
        os.makedirs(b)
        h = common.sha256_bytes(b"abc")
        with open(os.path.join(a, "a %s\n100644 b" % h), "wb") as fh:
            fh.write(b"safe\n")
        with open(os.path.join(b, "a"), "wb") as fh:
            fh.write(b"abc")
        with open(os.path.join(b, "b"), "wb") as fh:
            fh.write(b"safe\n")
        self.assertNotEqual(common.tree_hash(a), common.tree_hash(b))

    def test_staging_attributes_cannot_change_committed_bytes(self):
        self.full_release()
        self.write("docs/guide.md", "Guide.\r\nTwo.\r\n")
        self.commit_push()
        stage = common.staging_dir("Demo")
        with open(os.path.join(stage, ".git", "info", "attributes"), "w") as fh:
            fh.write("docs/guide.md text eol=lf\n")
        self.full_release()   # attributes are removed, so the CRLF bytes survive unchanged
        blob = subprocess.run(["git", "-C", self.public, "cat-file", "blob", "main:docs/guide.md"],
                              capture_output=True, check=True).stdout
        self.assertEqual(blob, b"Guide.\r\nTwo.\r\n")

    def test_inherited_git_env_is_dropped(self):
        with mock.patch.dict(os.environ, {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "audit.probe",
                                          "GIT_CONFIG_VALUE_0": "inherited"}):
            self.assertEqual(common.git(self.repo, "config", "--get", "audit.probe",
                                        check=False).strip(), b"")

    def test_pushurl_differing_from_manifest_is_refused(self):
        self.full_release()
        other = os.path.join(self.d, "other.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", other], check=True)
        stage = common.staging_dir("Demo")
        git(stage, "config", "remote.origin.pushurl", other, env=release_mod._clean_env())
        self.write("src/demo.py", "print('three')\n")
        self.commit_push()
        with self.assertRaisesRegex(PublishError, "push"):
            self.full_release()
        self.assertEqual(subprocess.run(["git", "-C", other, "for-each-ref"], capture_output=True,
                                        text=True).stdout, "")

    def test_lapsed_allowance_blocks(self):
        self.manifest(remote=self.public, extra=(
            '\n[[allow]]\npath = "docs/gone.md"\nsha256 = "%s"\nclass = "email"\n'
            'reason = "stale"\n' % ("0" * 64)))
        build_mod.build("Demo")
        self.assertFalse(self.scan()["passed"])

    def test_list_with_no_usable_entry_fails_closed(self):
        open(os.path.join(self.pub, "names.txt"), "w").write("---\n")
        build_mod.build("Demo")
        with self.assertRaisesRegex(PublishError, "no usable entry"):
            self.scan()

    def test_stale_tracking_ref_does_not_count_as_pushed(self):
        self.write("README.md", "# local only\n")
        git(self.repo, "commit", "-qam", "local")
        head = git(self.repo, "rev-parse", "HEAD").strip()
        git(self.repo, "update-ref", "refs/remotes/origin/main", head)
        with self.assertRaisesRegex(PublishError, "unpushed"):
            build_mod.build("Demo")

    def test_truncated_cat_file_output_refused(self):
        sha = "a" * 40
        with mock.patch.object(common, "git", return_value=("%s blob 10\nabc" % sha).encode()):
            with self.assertRaisesRegex(PublishError, "truncated"):
                common.cat_blobs(self.repo, [sha])


class Allowances(PublishBase):
    def _allow(self, sha):
        self.manifest(remote=self.public, extra=(
            '\n[[allow]]\npath = "docs/a.md"\nsha256 = "%s"\nclass = "email"\n'
            'match = "someone@gmail.com"\nreason = "synthetic fixture"\n' % sha))

    def test_allowance_covers_then_lapses_when_content_changes(self):
        self.write("docs/a.md", CANARIES["email"] + "\n")
        self.commit_push()
        build_mod.build("Demo")
        sha = common.sha256_file(os.path.join(common.tree_dir("Demo"), "docs/a.md"))
        self._allow(sha)
        build_mod.build("Demo")
        report = self.scan()
        self.assertTrue(report["passed"], report["hits"])
        self.assertEqual(len(report["allowed"]), 1)
        self.write("docs/a.md", CANARIES["email"] + "\nmore\n")
        self.commit_push()
        build_mod.build("Demo")
        report = self.scan()
        self.assertFalse(report["passed"])
        self.assertEqual(len(report["lapsed_allowances"]), 1)

    def test_allowance_does_not_cover_another_class(self):
        self.write("docs/a.md", CANARIES["email"] + "\n" + CANARIES["term"] + "\n")
        self.commit_push()
        build_mod.build("Demo")
        sha = common.sha256_file(os.path.join(common.tree_dir("Demo"), "docs/a.md"))
        self._allow(sha)
        build_mod.build("Demo")
        self.assertEqual({h["class"] for h in self.scan()["hits"]}, {"term"})


class Refusals(PublishBase):
    def test_dirty_source_refused(self):
        self.write("README.md", "# changed\n")
        with self.assertRaisesRegex(PublishError, "dirty"):
            build_mod.build("Demo")

    def test_untracked_file_refused(self):
        self.write("stray.txt", "x\n")
        with self.assertRaisesRegex(PublishError, "dirty"):
            build_mod.build("Demo")

    def test_unpushed_source_refused(self):
        self.write("README.md", "# changed\n")
        git(self.repo, "commit", "-qam", "local")
        with self.assertRaisesRegex(PublishError, "unpushed"):
            build_mod.build("Demo")

    def test_symlink_refused(self):
        os.symlink("README.md", os.path.join(self.repo, "docs", "link.md"))
        self.commit_push()
        with self.assertRaisesRegex(PublishError, "symlink"):
            build_mod.build("Demo")

    def test_submodule_refused(self):
        sha = git(self.repo, "rev-parse", "HEAD").strip()
        git(self.repo, "update-index", "--add", "--cacheinfo", "160000,%s,src/sub" % sha)
        git(self.repo, "commit", "-qm", "sub")
        git(self.repo, "push", "-q", "origin", "main")
        with self.assertRaisesRegex(PublishError, "submodule"):
            build_mod.build("Demo")

    def test_unlisted_binary_refused(self):
        self.write("src/blob.bin", b"\x00\x01\x02", mode="wb")
        self.commit_push()
        with self.assertRaisesRegex(PublishError, "unlisted binary"):
            build_mod.build("Demo")

    def test_large_file_refused(self):
        self.write("src/big.txt", "a" * ((1 << 20) + 1))
        self.commit_push()
        with self.assertRaisesRegex(PublishError, "over max_file_bytes"):
            build_mod.build("Demo")

    def test_gitattributes_refused(self):
        self.write("src/.gitattributes", "* export-subst\n")
        self.commit_push()
        with self.assertRaisesRegex(PublishError, "may not be exported"):
            build_mod.build("Demo")

    def test_stale_include_glob_refused(self):
        text = open(os.path.join(self.pub, "Demo.toml")).read().replace(
            '"docs/"', '"docs/", "gone/**"')
        open(os.path.join(self.pub, "Demo.toml"), "w").write(text)
        with self.assertRaisesRegex(PublishError, "select nothing"):
            build_mod.build("Demo")

    def test_no_private_store_fails_closed(self):
        os.environ["LODESTAR_PRIVATE_ROOT"] = os.path.join(self.d, "nowhere")
        with self.assertRaisesRegex(PublishError, "private store unavailable"):
            build_mod.build("Demo")

    def test_empty_names_list_fails_closed(self):
        open(os.path.join(self.pub, "names.txt"), "w").write("# nothing\n")
        build_mod.build("Demo")
        with self.assertRaisesRegex(PublishError, "empty"):
            self.scan()

    def test_release_refuses_without_scan_of_identical_tree(self):
        build_mod.build("Demo")
        self.scan()
        release_mod.diff("Demo")
        with open(os.path.join(common.tree_dir("Demo"), "README.md"), "a") as fh:
            fh.write("edited after scan\n")
        with self.assertRaisesRegex(PublishError, "tree changed since build"):
            release_mod.release("Demo", "ship it")
        self.assertEqual(self.public_refs(), "")

    def test_release_refuses_without_diff(self):
        build_mod.build("Demo")
        self.scan()
        with self.assertRaisesRegex(PublishError, "diff not shown"):
            release_mod.release("Demo", "ship it")

    def _ready(self):
        build_mod.build("Demo")
        self.assertTrue(self.scan()["passed"])
        release_mod.diff("Demo")

    def test_release_refuses_without_cold_read(self):
        self._ready()
        with self.assertRaisesRegex(PublishError, "no cold-read receipt"):
            release_mod.release("Demo", "ship it")
        self.assertEqual(self.public_refs(), "")

    def test_release_refuses_when_cold_read_has_findings(self):
        self._ready()
        self.set_reviewer("FINDING: README.md:3 | person | a first name\nVERDICT: FINDINGS 1")
        rec = review_mod.review("Demo")
        self.assertEqual((rec["verdict"], rec["findings"]), ("FINDINGS", 1))
        with self.assertRaisesRegex(PublishError, "cold read reported 1"):
            release_mod.release("Demo", "ship it")
        self.assertEqual(self.public_refs(), "")

    FINDING1 = "FINDING: README.md:3 | person | a first name\nVERDICT: FINDINGS 1"
    WORDS = "Lukas: that name is a public author, ship it"

    def _findings_receipt(self):
        self._ready()
        self.set_reviewer(self.FINDING1)
        return review_mod.review("Demo")

    def test_owner_ruling_allows_release_and_record_says_so(self):
        self._findings_receipt()
        ruling = review_mod.rule("Demo", self.WORDS)
        self.assertEqual((ruling["findings"], ruling["owner_said"]), (1, self.WORDS))
        self.assertTrue(ruling["path"].endswith(".ruling.json"))
        rec = release_mod.release("Demo", "ship it")
        self.assertTrue(rec["pushed"])
        self.assertEqual(rec["cold_read"], "FINDINGS")
        self.assertIn("released on owner ruling over 1 findings", rec["gate"])
        self.assertIn(self.WORDS, rec["gate"])
        self.assertNotIn("CLEAN", rec["gate"])
        with open(os.path.join(self.pub, "releases.jsonl")) as fh:
            line = json.loads(fh.read().splitlines()[-1])
        self.assertEqual(line["owner_ruling"]["owner_said"], self.WORDS)
        self.assertIn("owner ruling over 1 findings", line["gate"])

    def test_clean_release_record_says_clean_and_has_no_ruling(self):
        _, rec = self.full_release()
        self.assertEqual(rec["gate"], "cold read CLEAN")
        self.assertNotIn("owner_ruling", rec)

    def test_ruling_lapses_when_the_tree_changes(self):
        self._findings_receipt()
        review_mod.rule("Demo", self.WORDS)
        self.write("README.md", "# Demo\n\nchanged\n")
        self.commit_push("change")
        build_mod.build("Demo")
        self.scan()
        release_mod.diff("Demo")
        review_mod.review("Demo")          # same FINDINGS answer, new tree
        with self.assertRaisesRegex(PublishError, "cold read reported 1"):
            release_mod.release("Demo", "ship it")
        self.assertEqual(self.public_refs(), "")

    def test_ruling_lapses_on_a_new_review(self):
        self._findings_receipt()
        review_mod.rule("Demo", self.WORDS)
        self.set_reviewer("FINDING: README.md:3 | person | a first name\n"
                          "FINDING: src/demo.py:1 | person | another\nVERDICT: FINDINGS 2")
        review_mod.review("Demo")
        with self.assertRaisesRegex(PublishError, "cold read reported 2"):
            release_mod.release("Demo", "ship it")
        # A stale ruling file (e.g. restored by hand) is still refused by the binding.
        review_mod.rule("Demo", self.WORDS)
        ruling = review_mod.ruling_path(common.private_dir(), "Demo", common.tree_hash(
            common.tree_dir("Demo")))
        kept = open(ruling).read()
        self.set_reviewer(self.FINDING1)
        review_mod.review("Demo")
        self.assertFalse(os.path.exists(ruling))
        with open(ruling, "w") as fh:
            fh.write(kept)
        with self.assertRaisesRegex(PublishError, "ruling lapsed"):
            release_mod.release("Demo", "ship it")
        # Even an identical answer re-reviewed is a new review: a restored ruling does not carry.
        review_mod.rule("Demo", self.WORDS)
        kept = open(ruling).read()
        review_mod.review("Demo")
        with open(ruling, "w") as fh:
            fh.write(kept)
        with self.assertRaisesRegex(PublishError, "ruling lapsed"):
            release_mod.release("Demo", "ship it")
        self.assertEqual(self.public_refs(), "")

    def test_ruling_lapses_if_the_answer_is_edited(self):
        rec = self._findings_receipt()
        review_mod.rule("Demo", self.WORDS)
        with open(rec["path"][:-5] + ".answer.md", "a") as fh:
            fh.write("\nedited\n")
        with self.assertRaisesRegex(PublishError, "answer"):
            release_mod.release("Demo", "ship it")

    def test_cannot_rule_over_missing_clean_or_malformed_receipts(self):
        self._ready()
        with self.assertRaisesRegex(PublishError, "no cold-read receipt"):
            review_mod.rule("Demo", self.WORDS)
        # A transport or parse failure writes no receipt, so there is nothing to rule over.
        self.set_reviewer("")
        with self.assertRaises(PublishError):
            review_mod.review("Demo")
        with self.assertRaisesRegex(PublishError, "no cold-read receipt"):
            review_mod.rule("Demo", self.WORDS)
        self.set_reviewer("VERDICT: CLEAN")
        review_mod.review("Demo")
        with self.assertRaisesRegex(PublishError, "nothing to rule"):
            review_mod.rule("Demo", self.WORDS)
        rec = self._findings_receipt()
        for bad in ({"verdict": None}, {"verdict": "FINDINGS", "findings": 0},
                    {"answer_sha256": ""}, {"verdict": "MAYBE"}):
            data = dict(json.load(open(rec["path"])), **bad)
            data.pop("path", None)
            common.write_json(rec["path"], data)
            with self.assertRaisesRegex(PublishError, "malformed", msg=bad):
                review_mod.rule("Demo", self.WORDS)
        self.assertFalse(os.path.exists(rec["path"][:-5] + ".ruling.json"))

    def test_rule_needs_real_words(self):
        self._findings_receipt()
        for words in ("", "   ", "ok"):
            with self.assertRaisesRegex(PublishError, "owner's own words"):
                review_mod.rule("Demo", words)

    def test_rule_cli(self):
        self._findings_receipt()
        import publish as cli
        self.assertEqual(cli.main(["rule", "Demo", "--owner-said", "x"]), 2)
        self.assertEqual(cli.main(["rule", "Demo", "--owner-said", self.WORDS]), 0)

    def test_cold_read_receipt_is_bound_to_the_tree(self):
        self._ready()
        review_mod.review("Demo")
        self.write("README.md", "# Demo\n\nchanged\n")
        self.commit_push("change")
        build_mod.build("Demo")
        self.scan()
        release_mod.diff("Demo")
        with self.assertRaisesRegex(PublishError, "no cold-read receipt"):
            release_mod.release("Demo", "ship it")

    def test_unparseable_or_inconsistent_answers_refuse(self):
        self._ready()
        for answer in ("", "looks fine to me", "VERDICT: CLEAN\nthanks",
                       "FINDING: a:1 | x | y\nVERDICT: CLEAN", "VERDICT: FINDINGS 2"):
            self.set_reviewer(answer)
            with self.assertRaises(PublishError, msg=answer):
                review_mod.review("Demo")

    def test_review_needs_a_passing_scan_and_a_transport(self):
        build_mod.build("Demo")
        with self.assertRaisesRegex(PublishError, "PASSING scan"):
            review_mod.review("Demo")
        self.assertTrue(self.scan()["passed"])
        os.environ[review_mod.REVIEW_ENV] = "/nonexistent-reviewer {prompt}"
        with self.assertRaises(PublishError):
            review_mod.review("Demo")

    def test_slices_split_large_trees_and_flag_binaries(self):
        root = os.path.join(self.d, "t")
        os.makedirs(root)
        for n in range(3):
            with open(os.path.join(root, "f%d.md" % n), "w") as fh:
                fh.write("line\n" * 100)
        with open(os.path.join(root, "b.png"), "wb") as fh:
            fh.write(b"\x89PNG\0\0")
        parts, binaries = review_mod.slices(root, limit=700)
        self.assertGreater(len(parts), 1)
        self.assertEqual(binaries, ["b.png"])
        self.assertIn("===== FILE: f0.md", "".join(parts))
        self.assertTrue(all(len(p) < 1200 for p in parts))

    def test_release_refuses_without_owner_words(self):
        with self.assertRaisesRegex(PublishError, "owner"):
            release_mod.release("Demo", "  ")

    def test_replacement_applies_and_stale_one_refused(self):
        self.manifest(remote=self.public, extra=(
            '\n[[replace]]\npaths = "src/*.py"\nfind = "hello"\nwith = "hi"\n'))
        build_mod.build("Demo")
        self.assertIn("hi", open(os.path.join(common.tree_dir("Demo"), "src/demo.py")).read())
        self.manifest(remote=self.public, extra=(
            '\n[[replace]]\npaths = "src/*.py"\nfind = "absent"\nwith = "x"\n'))
        with self.assertRaisesRegex(PublishError, "changed nothing"):
            build_mod.build("Demo")

    def test_export_subst_not_expanded(self):
        # The exporter reads blobs, not `git archive`, so $Format$ stays literal.
        self.write("src/v.txt", "$Format:%H$\n")
        self.commit_push()
        build_mod.build("Demo")
        self.assertEqual(open(os.path.join(common.tree_dir("Demo"), "src/v.txt")).read(),
                         "$Format:%H$\n")

    def test_unknown_manifest_field_refused(self):
        self.manifest(remote=self.public, extra='\nincldue = ["x"]\n')
        with self.assertRaisesRegex(PublishError, "unknown field"):
            build_mod.build("Demo")


if __name__ == "__main__":
    unittest.main()
