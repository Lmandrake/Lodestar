"""Tests for bin/lib/tickets.py and bin/ticket.py: homes, id claims, writes, links, needs, face.

Every repo here is a temp checkout with a temp bare remote, so claims and pushes are real git.
People named here (Zebulon) are invented.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin"))

from lib import tickets as tk  # noqa: E402
from tests import needs_private  # noqa: E402


def _legacy(fn):                 # these tests exercise the retired CODE-<n> ids
    def wrapped(*a, **k):
        k.setdefault("legacy", True)
        return fn(*a, **k)
    return wrapped


tk.create, tk.need = _legacy(tk.create), _legacy(tk.need)


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, text=True, check=True).stdout


def make_repo(base, name):
    bare = os.path.join(base, name + ".git")
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", bare], check=True)
    work = os.path.join(base, name)
    subprocess.run(["git", "init", "-q", "-b", "main", work], check=True)
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "user.name", "T")
    git(work, "remote", "add", "origin", bare)
    with open(os.path.join(work, "README.md"), "w") as fh:
        fh.write("x\n")
    git(work, "add", "README.md")
    git(work, "commit", "-q", "-m", "init")
    git(work, "push", "-q", "-u", "origin", "main")
    return work, bare


REGISTRY = """| Name | Path | Branch | Send | Receive | Machines | Id | Former | Remote | State | Mirror | Notes |
|------|------|--------|------|---------|----------|------|------|------|------|------|-------|
| Alpha | {a} | main | on | on |  | alpha |  |  | active |  |  |
| Beta | {b} | main | on | on |  | beta |  |  | active |  |  |
| Team | {a} | main | on | on |  | team |  |  | active |  |  |
| Gone | {a} | main | on | on |  | gone |  |  | active |  |  |
"""

HOMES = """| Name | Code | Home | Notes |
|------|------|------|-------|
| Alpha | ALP | repo | |
| Beta | BETA | repo | |
| Team | | redirect:Alpha | |
| Gone | | none | not ours |
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.a, self.abare = make_repo(self.tmp, "alpha")
        self.b, _ = make_repo(self.tmp, "beta")
        self.root = os.path.join(self.tmp, "lodestar")
        os.makedirs(os.path.join(self.root, "sync"))
        with open(os.path.join(self.root, "sync", "registry.md"), "w") as fh:
            fh.write(REGISTRY.format(a=self.a, b=self.b))
        with open(os.path.join(self.root, "sync", "ticket-homes.md"), "w") as fh:
            fh.write(HOMES)
        self.env = {k: os.environ.get(k) for k in ("LODESTAR_ROOT", "LODESTAR_STATE_DIR")}
        os.environ["LODESTAR_ROOT"] = self.root
        os.environ["LODESTAR_STATE_DIR"] = os.path.join(self.tmp, "state")
        # temp remotes are not the private host: treat them as private-host homes unless a test says not
        self._pub, self._ov = tk._is_public, tk._overlay
        tk._is_public = lambda repo: False

    def tearDown(self):
        tk._is_public, tk._overlay = self._pub, self._ov
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestHomes(Base):
    def test_resolve_by_name_id_code_and_redirect(self):
        for p in ("Alpha", "alpha", "ALP", "team"):
            h = tk.homes_for(p, self.root)[0]
            self.assertEqual((h.code, h.dir), ("ALP", os.path.join(os.path.realpath(self.a), "tickets")))

    def test_undeclared_and_none_refuse_by_name(self):
        with self.assertRaisesRegex(tk.Refused, "no ticket home declared for 'nope'"):
            tk.homes_for("nope", self.root)
        with self.assertRaisesRegex(tk.Refused, "Gone keeps no tickets"):
            tk.homes_for("gone", self.root)

    def test_duplicate_code_refused(self):
        with open(os.path.join(self.root, "sync", "ticket-homes.md"), "a") as fh:
            fh.write("| Other | ALP | repo | |\n")
        with self.assertRaisesRegex(tk.Refused, "declared twice"):
            tk.homes_table(self.root)


class TestCreate(Base):
    def test_new_claims_commits_pushes(self):
        tid, path, state = tk.create("alpha", "Fix the widget", "bug", "it breaks",
                                     likely_fix="reset the cache", root=self.root)
        self.assertEqual(tid, "ALP-1")
        self.assertEqual(state, "pushed")
        t = tk.read(path)
        self.assertEqual((t["type"], t["status"], t["project"], t["likely_fix"]),
                         ("bug", "open", "alpha", "reset the cache"))
        self.assertIn("## Log", t["_body"])
        self.assertIn("ticket: ALP-1 new", git(self.a, "log", "-1", "--format=%s"))
        self.assertNotIn("widget", git(self.a, "log", "-1", "--format=%B"))   # no title in history
        self.assertIn("refs/tickets/claims/ALP-1", git(self.a, "ls-remote", "origin"))
        self.assertEqual(tk.create("alpha", "Second", "note", root=self.root)[0], "ALP-2")

    def test_claim_race_skips_a_taken_number(self):
        # another machine claimed ALP-1 and ALP-2 but has not pushed the files yet
        other = os.path.join(self.tmp, "clone")
        subprocess.run(["git", "clone", "-q", self.abare, other], check=True)
        tree = git(other, "hash-object", "-t", "tree", "-w", "/dev/null").strip()
        sha = git(other, "commit-tree", tree, "-m", "c").strip()
        git(other, "push", "-q", "origin", "%s:refs/tickets/claims/ALP-1" % sha,
            "%s:refs/tickets/claims/ALP-2" % sha)
        self.assertEqual(tk.create("alpha", "Mine", "build", root=self.root)[0], "ALP-3")

    def test_claim_rejected_retries(self):
        real = tk.remote_claims
        calls = {"n": 0}

        def stale(repo, code):        # the first read misses a claim pushed a moment later
            calls["n"] += 1
            return [] if calls["n"] == 1 else real(repo, code)
        other = os.path.join(self.tmp, "clone2")
        subprocess.run(["git", "clone", "-q", self.abare, other], check=True)
        tree = git(other, "hash-object", "-t", "tree", "-w", "/dev/null").strip()
        sha = git(other, "commit-tree", tree, "-m", "c").strip()
        git(other, "push", "-q", "origin", "%s:refs/tickets/claims/ALP-1" % sha)
        tk.remote_claims = stale
        try:
            self.assertEqual(tk.create("alpha", "Race", "build", root=self.root)[0], "ALP-2")
        finally:
            tk.remote_claims = real

    def test_named_ticket(self):
        tid, path, _ = tk.create("alpha", "Widget", "bug", root=self.root, name="WIDGET_HANG_UNSTICK_THIRD_TIME_LUCKY", legacy=False)
        self.assertEqual(tid, "WIDGET_HANG_UNSTICK_THIRD_TIME_LUCKY")
        self.assertTrue(path.endswith(tid + ".md"))
        self.assertEqual(tk.find(tid, self.root)[0]["id"], tid)
        with self.assertRaisesRegex(tk.Refused, "already"):
            tk.create("alpha", "Again", "bug", root=self.root, name=tid, legacy=False)
        with self.assertRaisesRegex(tk.Refused, "needs a name"):
            tk.create("alpha", "x", "bug", root=self.root, legacy=False)

    def test_rename_legacy_to_named_moves_file_and_fixes_links(self):
        a = tk.create("alpha", "A", "build", root=self.root, legacy=True)[0]
        b = tk.create("alpha", "B", "build", root=self.root, legacy=True)[0]
        tk.link(b, "blocked_by", a, root=self.root) if False else tk.update(b, "link", self.root, blocked_by=a)
        nid, _ = tk.rename(a, "WIDGET_HANG_UNSTICK_THIRD_TIME_LUCKY", root=self.root)
        t, _h = tk.find(nid, self.root)
        self.assertEqual((t["id"], t["former_id"]), (nid, a))
        self.assertFalse(any(p.endswith(a + ".md") for h in tk.all_homes(self.root)
                             for p in tk._ticket_files(h.dir)))
        self.assertEqual(tk.find(b, self.root)[0]["blocked_by"], nid)
        with self.assertRaisesRegex(tk.Refused, "legacy"):
            tk.rename(nid, "WIDGET_HANG_UNSTICK_FOURTH_TIME_LUCKY", root=self.root)
        with self.assertRaisesRegex(tk.Refused, "bad ticket name"):
            tk.rename(b, "NOT_A_GOOD_NAME", root=self.root)

    def test_agent_engineering_tickets_stay_out_of_orders(self):
        mk = lambda **k: dict({"id": "X", "source": "agent", "type": "build", "status": "open"}, **k)
        self.assertTrue(tk.engineering_backlog(mk()))
        self.assertTrue(tk.engineering_backlog(mk(type="bug")))
        self.assertFalse(tk.engineering_backlog(mk(due="2026-11-01")))
        self.assertFalse(tk.engineering_backlog(mk(next="yes")))
        self.assertFalse(tk.engineering_backlog(mk(type="action")))
        self.assertFalse(tk.engineering_backlog(mk(source="manual")))
        self.assertFalse(tk.engineering_backlog(mk(source="migrated:actions")))

    def test_offline_id(self):
        tid, _, _ = tk.create("alpha", "Plane", "action", offline=True, root=self.root, push=False)
        self.assertRegex(tid, r"^ALP-x[a-z2-7]{5}$")

    def test_no_remote_refuses(self):
        git(self.a, "remote", "remove", "origin")
        with self.assertRaisesRegex(tk.Refused, "no remote"):
            tk.create("alpha", "x", "note", root=self.root)

    def test_validation(self):
        with self.assertRaisesRegex(tk.Refused, "type"):
            tk.create("alpha", "x", "chore", root=self.root)
        with self.assertRaisesRegex(tk.Refused, "due"):
            tk.create("alpha", "x", "action", due="next week", root=self.root)
        with self.assertRaisesRegex(tk.Refused, "no title"):
            tk.create("alpha", "  ", "note", root=self.root)
        self.assertNotIn("refs/tickets/claims", git(self.a, "ls-remote", "origin"))  # no id burned

    def test_public_home_guard(self):
        tk._is_public = lambda repo: True
        with self.assertRaisesRegex(tk.Refused, "github.com.*--private"):
            tk.create("alpha", "Ask someone@work.private.example about it", "note", root=self.root)
        tid, _, _ = tk.create("alpha", "Clean tool item", "build", root=self.root)
        self.assertEqual(tid, "ALP-1")

    def test_private_flag_uses_overlay_and_same_sequence(self):
        tk._is_public = lambda repo: os.path.realpath(repo) == os.path.realpath(self.a)
        ov, _ = make_repo(self.tmp, "overlay")
        tk._overlay = lambda name, repo: (os.path.join(ov, "projects", name, "tickets"), ov)
        t1 = tk.create("alpha", "public one", "build", root=self.root)
        t2 = tk.create("alpha", "private one", "action", private=True, root=self.root)
        self.assertEqual((t1[0], t2[0]), ("ALP-1", "ALP-2"))
        self.assertTrue(t2[1].startswith(ov))
        self.assertEqual(len(tk.load(tk.homes_for("alpha", self.root))), 2)
        self.assertEqual(tk.find("ALP-2", self.root)[1].kind, "overlay")


class TestMoves(Base):
    def setUp(self):
        super().setUp()
        self.t1 = tk.create("alpha", "One", "build", root=self.root)[0]
        self.t2 = tk.create("alpha", "Two", "build", root=self.root)[0]

    def test_close_needs_reason(self):
        with self.assertRaisesRegex(tk.Refused, "--said"):
            tk.move(self.t1, "done", root=self.root)
        f, _ = tk.move(self.t1, "done", said="done, shipped", root=self.root)
        self.assertEqual((f["status"], f["close_reason"]), ("done", "said: done, shipped"))
        f, _ = tk.move(self.t1, "open", root=self.root)
        self.assertNotIn("close_reason", f)

    def test_illegal_move(self):
        tk.move(self.t1, "dropped", said="no", root=self.root)
        with self.assertRaisesRegex(tk.Refused, "cannot move"):
            tk.move(self.t1, "active", root=self.root)

    def test_link_both_sides_and_blocks(self):
        tk.link(self.t2, "blocked_by", self.t1, root=self.root)
        a, b = tk.find(self.t1, self.root)[0], tk.find(self.t2, self.root)[0]
        self.assertEqual((a["blocks"], b["blocked_by"], b["status"]), (self.t2, self.t1, "blocked"))
        tk.link(self.t1, "ref", "commit:abc123", root=self.root)
        self.assertEqual(tk.find(self.t1, self.root)[0]["links"], "commit:abc123")

    def test_check_finds_dangling(self):
        tk.update(self.t1, "edit", root=self.root, relates="ALP-99")
        t = tk.find(self.t1, self.root)[0]
        self.assertIn("relates -> ALP-99 dangling", tk.problems(t, {self.t1, self.t2}))


class TestDeps(Base):
    def setUp(self):
        super().setUp()
        self.t1, self.t2, self.t3 = (tk.create("alpha", n, "build", root=self.root)[0]
                                     for n in ("One", "Two", "Three"))
        self.x = tk.create("beta", "Other", "build", root=self.root)[0]

    def st(self, tid):
        return tk.find(tid, self.root)[0]["status"]

    def ready(self):
        ts = tk.load(tk.homes_for("alpha", self.root))
        r, u, d = tk.frontier(ts, tk.lookup_from(ts, self.root))
        return [t["id"] for t in r], [t["id"] for t, _ in u], [t["id"] for t, _ in d]

    def test_cycle_refused_including_cross_project(self):
        tk.link(self.t2, "blocked_by", self.t1, root=self.root)
        tk.link(self.x, "blocked_by", self.t2, root=self.root)
        with self.assertRaisesRegex(tk.Refused, "cycle: %s waits on %s waits on %s waits on %s" % (
                self.t1, self.x, self.t2, self.t1)):
            tk.link(self.t1, "blocked_by", self.x, root=self.root)
        with self.assertRaisesRegex(tk.Refused, "cycle"):
            tk.link(self.x, "blocks", self.t1, root=self.root)
        with self.assertRaisesRegex(tk.Refused, "cycle"):
            tk.link(self.t1, "blocked_by", self.t1, root=self.root)
        self.assertNotIn("blocked_by", tk.find(self.t1, self.root)[0])
        tk.link(self.t1, "relates", self.x, root=self.root)       # relates is not a wait

    def test_ready_view_and_close_names_dependents(self):
        tk.link(self.t2, "blocked_by", self.t1, root=self.root)
        tk.link(self.t3, "blocked_by", self.t1, root=self.root)
        tk.link(self.t3, "blocked_by", self.x, root=self.root)
        self.assertEqual(self.ready(), ([self.t1], [], []))
        tk.move(self.t1, "done", said="shipped", root=self.root)
        rows = {r["id"]: r["kind"] for r in tk.dependents_after(self.t1, self.root)}
        self.assertEqual(rows, {self.t2: "ready", self.t3: "waiting"})
        self.assertEqual(self.st(self.t2), "blocked")                # named, not released
        self.assertEqual(self.ready(), ([], [self.t2], []))
        moved = tk.release(tk.dependents_after(self.t1, self.root), self.t1, root=self.root)
        self.assertEqual((moved, self.st(self.t2)), ([self.t2], "open"))
        self.assertIn("left blocked: %s done" % self.t1, tk.find(self.t2, self.root)[0]["_body"])
        self.assertEqual(self.ready(), ([self.t2], [], []))

    def test_dropped_blocker_never_releases(self):
        tk.link(self.t2, "blocked_by", self.t1, root=self.root)
        tk.move(self.t1, "dropped", said="not doing it", root=self.root)
        rows = tk.dependents_after(self.t1, self.root)
        self.assertEqual([(r["id"], r["kind"]) for r in rows], [(self.t2, "confirm")])
        self.assertEqual(tk.release(rows, self.t1, root=self.root), [])
        self.assertEqual(self.st(self.t2), "blocked")
        self.assertEqual(self.ready(), ([self.t3], [], [self.t2]))
        tk.move(self.t2, "open", root=self.root)                     # by hand: allowed
        self.assertEqual(self.ready(), ([self.t3], [], [self.t2]))   # still asks to confirm
        tk.link(self.t2, "unblock", self.t1, root=self.root)
        self.assertNotIn("blocked_by", tk.find(self.t2, self.root)[0])
        self.assertNotIn("blocks", tk.find(self.t1, self.root)[0])
        self.assertEqual(self.ready(), ([self.t2, self.t3], [], []))

    def test_link_to_done_blocker_does_not_block(self):
        tk.move(self.t1, "done", said="ok", root=self.root)
        tk.link(self.t2, "blocked_by", self.t1, root=self.root)
        self.assertEqual(self.st(self.t2), "open")

    def test_check_link_problems(self):
        tk.move(self.t1, "blocked", root=self.root)                    # blocked, no blocker
        tk.link(self.t2, "blocked_by", self.t3, root=self.root)
        tk.move(self.t3, "done", said="ok", root=self.root)             # stale block
        ts = tk.load(tk.homes_for("alpha", self.root))
        get = tk.lookup_from(ts, self.root)
        probs = {t["id"]: tk.link_problems(t, get) for t in ts}
        self.assertEqual(probs[self.t1], ["blocked with no blocked_by"])
        self.assertRegex(probs[self.t2][0], "stale block")
        tk.update(self.t1, "edit", root=self.root, blocked_by="ALP-99")
        t = tk.find(self.t1, self.root)[0]
        self.assertIn("blocked_by -> ALP-99 dangling", tk.problems(t, {x["id"] for x in ts}))

    def test_cli_close_prints_dependents(self):
        import contextlib
        import io
        import ticket as cli
        tk.link(self.t2, "blocked_by", self.t1, root=self.root)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["close", self.t1, "--said", "done", "--release"]), 0)
        self.assertIn("%s: every blocker done, released to open" % self.t2, out.getvalue())
        self.assertEqual(self.st(self.t2), "open")


class TestNonTicketBlockers(Base):
    """LODE-37: free-text, date and external-issue blockers live in blocked_by beside ticket ids."""

    def setUp(self):
        super().setUp()
        self.t1, self.t2 = (tk.create("alpha", n, "build", root=self.root)[0] for n in ("One", "Two"))
        self.ext = "https://git.private.example/owner/Beta/issues/12"
        self._gh = tk.ext_state
        tk._EXT_CACHE.clear()

    def tearDown(self):
        tk.ext_state = self._gh
        tk._EXT_CACHE.clear()
        super().tearDown()

    def ready(self):
        ts = tk.load(tk.homes_for("alpha", self.root))
        r, u, d = tk.frontier(ts, tk.lookup_from(ts, self.root))
        return [t["id"] for t in r], [t["id"] for t, _ in u]

    def f(self, tid):
        return tk.find(tid, self.root)[0]

    def test_classify(self):
        self.assertEqual(tk.classify("ALP-3")[0], "ticket")
        self.assertEqual(tk.classify("2026-11-01"), ("date", "until 2026-11-01"))
        self.assertEqual(tk.classify("Until 2026-11-01"), ("date", "until 2026-11-01"))
        self.assertEqual(tk.classify("a rainy day"), ("text", "a rainy day"))
        self.assertEqual(tk.classify("owner/Beta#12", "git.private.example"),
                         ("ext", self.ext))
        self.assertEqual(tk.classify("https://github.com/o/r/pull/5")[1], "https://github.com/o/r/issues/5")
        with self.assertRaisesRegex(tk.Refused, "real date"):
            tk.classify("until 2026-13-45")
        with self.assertRaisesRegex(tk.Refused, "comma"):
            tk.classify("rain, or snow")

    def test_condition_blocks_and_never_resolves(self):
        tk.link(self.t2, "blocked_on", "a rainy day", root=self.root)
        t = self.f(self.t2)
        self.assertEqual((t["blocked_by"], t["status"]), ("a rainy day", "blocked"))
        self.assertEqual(self.ready(), ([self.t1], []))
        ts = tk.load(tk.homes_for("alpha", self.root))
        rows = tk.waiting_rows(ts, tk.lookup_from(ts, self.root))
        self.assertEqual([(r[0]["id"], r[1][0][:2]) for r in rows], [(self.t2, ("a rainy day", "waiting"))])
        self.assertEqual(rows[0][1][0][2], "0h")
        self.assertEqual(tk.problems(t, {self.t1, self.t2}), [])        # not a dangling id
        self.assertEqual(tk.link_problems(t, tk.lookup_from(ts, self.root)), [])

    def test_blocked_by_routes_non_ids(self):
        tk.link(self.t2, "blocked_by", "a rainy day", root=self.root)
        self.assertEqual(self.f(self.t2)["blocked_by"], "a rainy day")

    def test_met_needs_who_and_logs_it(self):
        tk.link(self.t2, "blocked_on", "a rainy day", root=self.root)
        with self.assertRaisesRegex(tk.Refused, "--said"):
            tk.link(self.t2, "met", "a rainy day", root=self.root)
        with self.assertRaisesRegex(tk.Refused, "no non-ticket blocker"):
            tk.link(self.t2, "met", "sunshine", root=self.root, said="x")
        tk.link(self.t2, "met", "a rainy day", root=self.root, said="it rained")
        t = self.f(self.t2)
        self.assertNotIn("blocked_by", t)
        self.assertIn("met a rainy day (said: it rained)", t["_body"])
        self.assertEqual(t["status"], "blocked")                         # stale block, ready says so
        self.assertEqual(self.ready(), ([self.t1], []))                   # blocked + no blockers: not listed
        tk.link(self.t1, "blocked_on", "second", root=self.root)
        tk.link(self.t1, "met", "SECOND", root=self.root, evidence="saw it")
        self.assertIn("evidence: saw it", self.f(self.t1)["_body"])

    def test_met_cannot_end_a_ticket_blocker(self):
        tk.link(self.t2, "blocked_by", self.t1, root=self.root)
        with self.assertRaisesRegex(tk.Refused, "no non-ticket blocker"):
            tk.link(self.t2, "met", self.t1, root=self.root, said="x")

    def test_date_clears_itself_and_ready_reports_it(self):
        tk.link(self.t2, "blocked_on", "until 2026-11-01", root=self.root)
        t = self.f(self.t2)
        before = datetime(2026, 10, 30).astimezone()
        after = datetime(2026, 11, 1, 8).astimezone()
        self.assertEqual(tk.blocker_states(t, now=before), [("until 2026-11-01", "waiting")])
        self.assertEqual(tk.blocker_states(t, now=after), [("until 2026-11-01", "done")])
        self.assertEqual(tk.cleared_nonticket(t, after), ["until 2026-11-01"])
        self.assertEqual(tk.cleared_nonticket(t, before), [])
        ts = tk.load(tk.homes_for("alpha", self.root))
        self.assertEqual(tk.frontier(ts, tk.lookup_from(ts, self.root), after)[1][0][0]["id"], self.t2)
        self.assertEqual([r[0]["id"] for r in tk.cleared_rows(ts, after)], [self.t2])
        link_dated = tk.link(self.t1, "blocked_on", "2020-01-01", root=self.root)   # already past
        self.assertEqual(self.f(self.t1)["status"], "open")

    def test_external_ref_resolution_and_unknown(self):
        tk.link(self.t2, "blocked_on", "owner/Beta#12", root=self.root, host="git.private.example")
        self.assertEqual(self.f(self.t2)["blocked_by"], self.ext)
        seen = []

        def fake(cmd, **kw):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=self.out, stderr="")
        real = subprocess.run
        try:
            subprocess.run = fake
            for out, want in (("closed\n", "done"), ("open\n", "open"), ("garbage", "unknown")):
                tk._EXT_CACHE.clear()
                self.out = out
                self.assertEqual(tk.ext_state(self.ext), want)
            self.assertIn("git.private.example", seen[0])                  # --hostname is the private host
            self.assertEqual(seen[0][seen[0].index("--hostname") + 1], "git.private.example")

            def boom(cmd, **kw):
                raise OSError("offline")
            subprocess.run = boom
            tk._EXT_CACHE.clear()
            self.assertEqual(tk.ext_state(self.ext), "unknown")            # never guessed done
            ts = tk.load(tk.homes_for("alpha", self.root))
            self.assertEqual(self.ready(), ([self.t1], []))
        finally:
            subprocess.run = real

    def test_external_closed_makes_dependent_ready(self):
        tk.link(self.t2, "blocked_on", self.ext, root=self.root)
        tk.move(self.t2, "open", root=self.root)
        tk.ext_state = lambda e: "done"
        self.assertEqual(self.ready(), ([self.t1, self.t2], []))

    def test_last_blocker_of_any_kind_makes_ready(self):
        tk.link(self.t2, "blocked_by", self.t1, root=self.root)
        tk.link(self.t2, "blocked_on", "a rainy day", root=self.root)
        tk.move(self.t1, "done", said="ok", root=self.root)
        rows = tk.dependents_after(self.t1, self.root)
        self.assertEqual((rows[0]["kind"], rows[0]["waiting"]), ("waiting", ["a rainy day"]))
        tk.link(self.t2, "met", "a rainy day", root=self.root, said="rain")
        tk.move(self.t2, "open", root=self.root)
        self.assertEqual(self.ready(), ([self.t2], []))

    def test_cycle_check_still_covers_ticket_links(self):
        tk.link(self.t2, "blocked_by", self.t1, root=self.root)
        tk.link(self.t1, "blocked_on", "a rainy day", root=self.root)
        with self.assertRaisesRegex(tk.Refused, "cycle"):
            tk.link(self.t1, "blocked_by", self.t2, root=self.root)

    def test_describe_withholds_in_public_session(self):
        self.assertEqual(tk.describe("a rainy day", True), "a condition (text withheld)")
        self.assertEqual(tk.describe(self.ext, True), "private-host issue #12")
        self.assertEqual(tk.describe(self.ext), "owner/Beta#12@git.private.example")

    def test_cli_ready_sections(self):
        import contextlib
        import io
        import ticket as cli
        tk.link(self.t2, "blocked_on", "a rainy day", root=self.root)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["ready", "--project", "alpha"]), 0)
        text = out.getvalue()
        self.assertIn("waiting on a condition:", text)
        self.assertIn('"a rainy day"  [waiting, waiting 0h]', text)
        self.assertIn("1 waiting on a condition", text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["link", self.t2, "--met", "a rainy day", "--said", "rained"]), 0)
        self.assertNotIn("blocked_by", self.f(self.t2))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["link", self.t2, "--blocked-on", "o/r#5", "--host",
                                       "git.private.example"]), 0)
        self.assertRegex(out.getvalue(), r"(o/r#5@git.private.example|private-host issue #5)")


class TestNeed(Base):
    def test_need_lands_proposed_in_target_and_links_back(self):
        src = tk.create("alpha", "Requester", "build", root=self.root)[0]
        tid, path, _ = tk.need("alpha", "beta", "Do a thing for alpha", from_ticket=src,
                               root=self.root)
        t = tk.read(path)
        self.assertEqual((tid, t["status"], t["source"], t["requested_by"]),
                         ("BETA-1", "proposed", "need:alpha", src))
        self.assertEqual(tk.find(src, self.root)[0]["needs"], "BETA-1")
        tk.move(tid, "open", root=self.root)      # accepted
        self.assertEqual(tk.find(tid, self.root)[0]["status"], "open")


class TestFace(unittest.TestCase):
    NOW = datetime(2026, 10, 21, 9, 0).astimezone()

    def t(self, tid, **kw):
        base = {"id": tid, "status": "open", "owner": "lukas", "type": "action", "title": tid}
        base.update(kw)
        return base

    def test_rules(self):
        d = lambda n: (self.NOW + timedelta(days=n)).strftime("%Y-%m-%d")
        rows = tk.face_rows([
            self.t("A-1", due=d(-1)),                       # late action: red
            self.t("A-2", due=d(1)),                        # tomorrow: amber
            self.t("A-3", due=d(4)),                        # in the horizon: soon
            self.t("A-4", due=d(20)),                       # beyond: not shown
            self.t("A-5"),                                  # undated action: not shown
            self.t("A-6", type="build"),                    # plain build: not shown
            self.t("A-7", type="build", next="yes"),        # next: shown
            self.t("A-8", type="bug", status="blocked"),    # blocked: shown
            self.t("A-9", type="build", due=d(-2)),         # overdue build: shown
            self.t("A-10", type="build", due=d(1)),         # dated build, not overdue: hidden
            self.t("A-11", due=d(1), status="proposed"),    # inbox: never
            self.t("A-12", due=d(1), owner="zebulon"),          # not his: never
            self.t("A-13", due=d(1), snoozed_until=d(2)),   # snoozed
        ], self.NOW)
        got = [(r["ticket"], r["tier"]) for r in rows]
        self.assertEqual(got, [("A-1", "red"), ("A-9", "amber"), ("A-2", "amber"),
                               ("A-3", "soon"), ("A-7", "soon"), ("A-8", "soon")])

    def test_orders_items_keep_migrated_key(self):
        tmp = tempfile.mkdtemp()
        d = os.path.join(tmp, "tickets")
        os.makedirs(d)
        with open(os.path.join(d, "ALP-1.md"), "w") as fh:
            fh.write("---\nid: ALP-1\ntitle: T\nproject: alpha\ntype: action\nstatus: active\n"
                     "owner: lukas\ndue: 2026-11-02\neffort: 2h\norders_key: abc\n---\nbody\n")
        h = tk.Home("Alpha", "alpha", "ALP", "repo", d, tmp, tmp, False)
        it = tk.orders_items([h])[0]
        self.assertEqual((it["key"], it["state"], it["mine"], it["due"], it["est_min"]),
                         ("abc", "progress", True, "2026-11-02", 120))


if __name__ == "__main__":
    unittest.main()


class TestCompanion(Base):
    def test_face_block_is_opaque_and_detail_has_text(self):
        tid = tk.create("alpha", "Book the flight", "action",
                        due=(datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"), root=self.root)[0]
        rows = tk.face_rows(tk.load(tk.homes_for("alpha", self.root)))
        block, detail = tk.face_block(rows, {})
        it = block["items"][0]
        self.assertTrue(it["id"].startswith("tk_"))
        self.assertNotIn("flight", str(block))                 # no title on the face
        self.assertEqual(detail["items"][it["id"]]["ticket"], tid)
        self.assertEqual(detail["items"][it["id"]]["gist"], "Book the flight")
        self.assertFalse(it["breathe"])
        gone, _ = tk.face_block(rows, {}, {it["id"]: {"closed": "x"}})
        self.assertEqual(gone["items"], [])

    @needs_private("bin/lib/burning.py")
    def test_worker_ticket_done(self):
        import json
        import burning as worker
        tid = tk.create("alpha", "Repay Zebulon", "action", due="2026-10-01", root=self.root)[0]
        rows = tk.face_rows(tk.load(tk.homes_for("alpha", self.root)))
        block, detail = tk.face_block(rows, {})
        out = os.path.join(self.tmp, "companion")
        os.makedirs(out)
        with open(os.path.join(out, "tickets-detail.json"), "w") as fh:
            json.dump(detail, fh)
        p = worker.Paths(out=out)
        oid = block["items"][0]["id"]
        self.assertTrue(worker._valid("ticket_done", oid))
        self.assertFalse(worker._valid("ticket_done", "bi_x"))
        res = worker.ticket_verb(p, "ticket_done", oid)
        self.assertEqual(res["status"], "ok", res)
        t = tk.find(tid, self.root)[0]
        self.assertEqual((t["status"], t["close_reason"]), ("done", "said: done (companion button)"))
        p.close()


class TestMigrateActionsNames(Base):
    ACTIONS = ("---\nactions: v1\ntracker: actions\nboard: none\n---\n\n# A\n\n## Now\n\n"
               "- [ ] **Ship the form**: send it. Owner: **Lukas**. (owner lukas) {a_58vt7c}\n"
               "- [ ] **Ask Zebulon** for the export. Owner: **Lukas**. (owner lukas) {a_2rdmuu}\n")

    def _seed(self):
        with open(os.path.join(self.a, "ACTIONS.md"), "w") as fh:
            fh.write(self.ACTIONS)
        git(self.a, "add", "ACTIONS.md")
        git(self.a, "commit", "-q", "-m", "actions")

    def test_write_refuses_without_names_and_never_mints_numbers(self):
        self._seed()
        rep = tk.migrate_actions("alpha", write=False, root=self.root)
        self.assertEqual(len(rep["unnamed"]), 2)
        with self.assertRaisesRegex(tk.Refused, "SUBJECT_INTENT_TWIST name"):
            tk.migrate_actions("alpha", write=True, root=self.root, offline=True)
        with self.assertRaisesRegex(tk.Refused, "bad ticket name"):
            tk.migrate_actions("alpha", write=True, root=self.root,
                               names={i: "ALP-%d" % n for n, i in enumerate(rep["unnamed"], 1)})
        self.assertFalse(os.path.exists(os.path.join(self.a, "tickets", "MIGRATION-actions.tsv")))

    def test_write_with_names_uses_them(self):
        self._seed()
        ids = tk.migrate_actions("alpha", write=False, root=self.root)["unnamed"]
        names = dict(zip(ids, ["ALPHA_FINISH_FORM_TODAY", "ALPHA_ASK_ZEBULON_EXPORT"]))
        rep = tk.migrate_actions("alpha", write=True, root=self.root, names=names)
        self.assertEqual(rep["ids"], ("ALPHA_FINISH_FORM_TODAY", "ALPHA_ASK_ZEBULON_EXPORT"))
        self.assertTrue(os.path.exists(os.path.join(self.a, "tickets", "ALPHA_ASK_ZEBULON_EXPORT.md")))
