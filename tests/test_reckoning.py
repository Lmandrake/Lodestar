"""Tests for bin/lib/reckoning.py — the Reckoning, Lodestar's obligation ledger (stage 1).

People, projects and subjects in these fixtures (Zebulon Example, Orchard) are invented.

Spec: docs/superpowers/specs/2026-10-13-kairos-lodestar-obligations-design.md. Every store here
is a temp git checkout whose `origin` names git.private.example (never contacted); the two-Mac
acceptance test moves commits between clones through a local bare repo given by PATH on the
command line, so no remote config ever names a non-private host.
"""

import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin"))

from lib import reckoning as rk  # noqa: E402
from tests import needs_private  # noqa: E402

PRIVATE_URL = "git@git.private.example:mandrake/private-store.git"
T0 = datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc)


def git(repo, *args):
    return subprocess.run(["git", "-C", repo] + list(args), check=True, capture_output=True,
                          text=True).stdout


def make_checkout(path, url=PRIVATE_URL):
    os.makedirs(path, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@example.invalid")
    git(path, "config", "user.name", "t")
    if url:
        git(path, "remote", "add", "origin", url)
    return path


def at(days=0, minutes=0):
    return (T0 + timedelta(days=days, minutes=minutes)).isoformat()


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.repo = make_checkout(os.path.join(self.d, "private-store"))
        self.root = os.path.join(self.repo, "obligations")
        self.state_dir = os.path.join(self.d, "state")
        self._env = {k: os.environ.get(k) for k in ("LODESTAR_STATE_DIR", rk.ROOT_ENV)}
        os.environ["LODESTAR_STATE_DIR"] = self.state_dir
        os.environ[rk.ROOT_ENV] = self.root

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.d, ignore_errors=True)

    def propose(self, message="m1", title="Reply on the draft plan", when=None, **kw):
        obs = rk.obs_id("mail", rk.private_class(), message, "needs_you", "fp")
        ev = rk.make_event("propose", rk.ob_id(obs), "mt-a/bearing", at=when or at(),
                           obs=obs, title=title, owner="lukas", kind="reply", **kw)
        rk.append([ev], self.root, machine_key="mt-a")
        return ev

    def state(self, as_of=None):
        return rk.fold(rk.read_events(self.root)[0], as_of or T0 + timedelta(hours=1))


class BoundaryTest(Base):
    def test_private_urls(self):
        self.assertTrue(rk.is_private_url("git@git.private.example:mandrake/x.git"))
        self.assertTrue(rk.is_private_url("https://git.private.example/mandrake/x.git"))
        self.assertTrue(rk.is_private_url("ssh://git@git.private.example:22/mandrake/x.git"))
        for bad in ("git@github.com:Lmandrake/x.git", "https://github.com/x/y",
                    "git@git.private.example.evil.com:x/y", "/tmp/bare.git", "",
                    "https://evil.com/git.private.example/x"):
            self.assertFalse(rk.is_private_url(bad), bad)

    def test_a_github_pushurl_beside_a_private_fetch_url_refuses(self):
        git(self.repo, "remote", "set-url", "--push", "origin", "git@github.com:L/x.git")
        with self.assertRaises(rk.StoreUnavailable):
            rk.store_root(self.root)

    def test_a_second_non_private_remote_refuses(self):
        git(self.repo, "remote", "add", "mirror", "git@github.com:L/x.git")
        with self.assertRaises(rk.StoreUnavailable):
            rk.store_root(self.root)

    def test_no_checkout_is_no_ledger(self):
        with self.assertRaises(rk.StoreUnavailable):
            rk.store_root(os.path.join(self.d, "nowhere", "obligations"))

    def test_personal_class_refused_at_the_store(self):
        obs = rk.obs_id("imessage", "me", "g1", "judge")
        ev = rk.make_event("propose", rk.ob_id(obs), "x", obs=obs, title="dentist",
                           **{"class": "personal"})
        with self.assertRaises(rk.Refused):
            rk.append([ev], self.root, machine_key="mt-a")
        self.assertFalse(os.path.exists(os.path.join(self.root, "events")))

    def test_tampered_or_misderived_ids_refused(self):
        obs = rk.obs_id("mail", rk.private_class(), "m", "needs_you")
        ev = rk.make_event("propose", "ob_" + "0" * 20, "x", obs=obs, title="t")
        with self.assertRaises(rk.Refused):
            rk.append([ev], self.root, machine_key="mt-a")
        ev = rk.make_event("propose", rk.ob_id(obs), "x", obs=obs, title="t")
        ev["title"] = "changed"
        with self.assertRaises(rk.Refused):
            rk.append([ev], self.root, machine_key="mt-a")

    def test_output_class(self):
        other = make_checkout(os.path.join(self.d, "Lodestar"), "git@github.com:L/Lodestar.git")
        bare = make_checkout(os.path.join(self.d, "none"), url=None)
        self.assertEqual(rk.output_class(self.repo), "content")
        self.assertEqual(rk.output_class(other), "counts")
        self.assertEqual(rk.output_class(bare), "counts")
        self.assertEqual(rk.output_class(self.d), "counts")  # not a repo at all


class FoldTest(Base):
    def test_lifecycle_and_typed_due(self):
        ev = self.propose()
        ob = ev["ob"]
        with self.assertRaises(rk.Refused):  # a partial date is refused, never completed
            rk.transition(ob, "accept", "mt-a/cli", self.root, machine_key="mt-a", due="2026-10")
        rk.transition(ob, "accept", "mt-a/cli", self.root, machine_key="mt-a")
        rec = self.state()[ob]
        self.assertEqual((rec["state"], rec["due"]), ("open", ""))
        rk.transition(ob[:10], "amend", "mt-a/cli", self.root, machine_key="mt-a",
                      due="2026-10-01")
        self.assertTrue(self.state(T0 + timedelta(days=2))[ob]["overdue"])
        rk.transition(ob, "done", "mt-a/cli", self.root, machine_key="mt-a", said="sent it")
        rec = self.state()[ob]
        self.assertEqual(rec["state"], "done")
        self.assertEqual(rec["said"], ["sent it"])
        with self.assertRaises(rk.Refused):
            rk.transition(ob, "accept", "mt-a/cli", self.root, machine_key="mt-a")

    def test_drop_is_its_own_state(self):
        ob = self.propose()["ob"]
        rk.transition(ob, "drop", "mt-a/cli", self.root, machine_key="mt-a")
        self.assertEqual(self.state()[ob]["state"], "dropped")
        rk.transition(ob, "reopen", "mt-a/cli", self.root, machine_key="mt-a")
        self.assertEqual(self.state()[ob]["state"], "open")

    def test_order_independence(self):
        p = self.propose()
        ob = p["ob"]
        a = rk.make_event("accept", ob, "mt-a/cli", at=at(minutes=1), supersedes=[p["id"]])
        m = rk.make_event("amend", ob, "mt-a/cli", at=at(minutes=2), supersedes=[a["id"]],
                          due="2026-10-20")
        d = rk.make_event("done", ob, "mt-b/cli", at=at(minutes=3), supersedes=[a["id"]])
        r = rk.make_event("reopen", ob, "mt-a/cli", at=at(minutes=2), supersedes=[d["id"]])
        results = set()
        for perm in itertools.permutations([p, a, m, d, r]):
            rec = rk.fold(perm, T0)[ob]
            results.add((rec["state"], rec["due"], tuple(rec["heads"])))
        self.assertEqual(len(results), 1)
        self.assertEqual(next(iter(results))[0], "open")

    def test_slow_clock_reopen_that_saw_done_wins(self):
        p = self.propose()
        a = rk.make_event("accept", p["ob"], "mt-a", at=at(minutes=1), supersedes=[p["id"]])
        d = rk.make_event("done", p["ob"], "mt-a", at=at(minutes=30), supersedes=[a["id"]])
        r = rk.make_event("reopen", p["ob"], "mt-b", at=at(minutes=20),  # clock 10 min slow
                          supersedes=[d["id"]])
        self.assertEqual(rk.fold([p, a, d, r], T0)[p["ob"]]["state"], "open")

    def test_concurrent_unseen_reopen_is_a_conflict_until_resolved(self):
        p = self.propose()
        a = rk.make_event("accept", p["ob"], "mt-a", at=at(minutes=1), supersedes=[p["id"]])
        d = rk.make_event("done", p["ob"], "mt-a", at=at(minutes=30), supersedes=[a["id"]])
        r = rk.make_event("drop", p["ob"], "mt-b", at=at(minutes=40), supersedes=[a["id"]])
        rec = rk.fold([p, a, d, r], T0)[p["ob"]]
        self.assertEqual(rec["state"], "conflict")
        self.assertEqual({c["state"] for c in rec["conflict"]}, {"done", "dropped"})
        rk.append([a, d, r], self.root, machine_key="mt-a")
        rk.transition(p["ob"], "done", "mt-a/cli", self.root, machine_key="mt-a")
        self.assertEqual(self.state()[p["ob"]]["state"], "done")

    def test_amend_that_did_not_see_done_cannot_reopen(self):
        p = self.propose()
        a = rk.make_event("accept", p["ob"], "mt-a", at=at(minutes=1), supersedes=[p["id"]])
        d = rk.make_event("done", p["ob"], "mt-a", at=at(minutes=3), supersedes=[a["id"]])
        m = rk.make_event("amend", p["ob"], "mt-b", at=at(minutes=9), supersedes=[a["id"]],
                          due="2026-11-01")
        rec = rk.fold([p, a, d, m], T0)[p["ob"]]
        self.assertEqual((rec["state"], rec["due"]), ("done", "2026-11-01"))

    def test_torn_final_line_ignored_then_repaired(self):
        # transition() stamps the wall clock; propose at the wall clock too, or the torn line sits
        # in an older day's file that the repair never touches (failed from the day after T0).
        ev = self.propose(when=rk.now_iso())
        path = rk.append([], self.root) or None
        day_file = [os.path.join(dp, f) for dp, _, fs in os.walk(self.root) for f in fs][0]
        with open(day_file, "a") as fh:
            fh.write('{"ev": "accept", "ob": "%s"' % ev["ob"])
        events, warnings = rk.read_events(self.root)
        self.assertEqual(len(events), 1)
        self.assertTrue(any("torn" in w for w in warnings))
        rk.transition(ev["ob"], "accept", "mt-a/cli", self.root, machine_key="mt-a")
        events, warnings = rk.read_events(self.root)
        self.assertEqual((len(events), warnings), (2, []))
        self.assertIsNone(path)


class OutboxTest(Base):
    def item(self, subject="Please review the plan", eid="mail:AAA", thread="conv-1"):
        return {"event_id": eid, "subject": subject, "threads": [thread], "who": "Zebulon",
                "pointer": "https://outlook/x"}

    def test_drain_is_idempotent_and_survives_a_crash_after_append(self):
        box = rk.outbox_path()
        det = rk.needs_you_detection(self.item(), "Orchard", "fp1", at=at())
        rk.outbox_put(det)
        rk.outbox_put(det)  # a retry queues nothing new
        self.assertEqual(len(rk._read_outbox(box)), 1)
        # Crash simulation: the append happened, the outbox rewrite did not.
        rk.append(rk._detection_events(det, {}), self.root, machine_key="mt-a")
        out = rk.drain(machine_key="mt-a")
        self.assertEqual(out, {"drained": 0, "queued": 0})
        state = self.state()
        self.assertEqual(len(state), 1)
        rec = next(iter(state.values()))
        self.assertEqual((rec["state"], rec["project"], rec["origin"]),
                         ("proposed", "Orchard", "needs_you"))

    def test_unavailable_store_keeps_the_queue(self):
        rk.outbox_put(rk.needs_you_detection(self.item(), "Orchard", "fp1", at=at()))
        git(self.repo, "remote", "set-url", "origin", "git@github.com:L/x.git")
        out = rk.drain(machine_key="mt-a")
        self.assertEqual(out["queued"], 1)
        self.assertIn("unavailable", out)

    def test_attachment_is_conservative(self):
        rk.outbox_put(rk.needs_you_detection(self.item(), "Orchard", "fp1", at=at()))
        # same thread, same ask, a later message: attaches
        rk.outbox_put(rk.needs_you_detection(self.item(eid="mail:BBB"), "Orchard", "fp1",
                                             at=at(minutes=5)))
        # same thread, a different ask: a new proposal, marked possibly the same
        rk.outbox_put(rk.needs_you_detection(self.item("Budget numbers?", "mail:CCC"),
                                             "Orchard", "fp2", at=at(minutes=6)))
        self.assertEqual(rk.drain(machine_key="mt-a")["drained"], 3)
        state = self.state()
        self.assertEqual(len(state), 2)
        first = [r for r in state.values() if len(r["observations"]) == 2]
        second = [r for r in state.values() if r["possibly_same_as"]]
        self.assertEqual(len(first), 1)
        self.assertEqual(second[0]["possibly_same_as"], [first[0]["ob"]])

    def test_a_new_ask_in_a_finished_thread_mints_a_new_obligation(self):
        rk.outbox_put(rk.needs_you_detection(self.item(), "Orchard", "fp1", at=at()))
        rk.drain(machine_key="mt-a")
        ob = next(iter(self.state()))
        rk.transition(ob, "done", "mt-a/cli", self.root, machine_key="mt-a")
        rk.outbox_put(rk.needs_you_detection(self.item(eid="mail:ZZZ"), "Orchard", "fp1",
                                             at=at(days=30)))
        rk.drain(machine_key="mt-a")
        state = self.state()
        self.assertEqual(len(state), 2)
        self.assertEqual(state[ob]["state"], "done")

    def judge_member(self, eid="teams:c:1"):
        return {"event_id": eid, "threads": ["conv-9"], "source_label": "mail",
                "subject": "Review", "pointer": "p", "scope": "PILLAR"}

    def test_judge_verdicts(self):
        m = self.judge_member()
        rk.outbox_put(rk.judge_detection(m, "new_obligation", "c1", "b1", "send the plan by Fri",
                                         "plan due Friday", at=at()))
        rk.outbox_put(rk.judge_detection(self.judge_member("mail:2"), "corrected", "c1", "b1",
                                         "moved to Monday", "now due Monday", at=at(minutes=1)))
        rk.outbox_put(rk.judge_detection(self.judge_member("mail:3"), "cancelled", "c1", "b1",
                                         "Cancel tomorrow's review", "review cancelled",
                                         at=at(minutes=2)))
        rk.drain(machine_key="mt-a")
        state = self.state()
        self.assertEqual(len(state), 1)
        rec = next(iter(state.values()))
        # A cancellation is evidence beside the obligation, never a close (spec fixture).
        self.assertEqual(rec["state"], "proposed")
        self.assertEqual(rec["note"], "now due Monday")
        self.assertEqual(len(rec["cancel_evidence"]), 1)

    def test_cancelled_with_nothing_to_cancel_is_a_visible_notice(self):
        rk.outbox_put(rk.judge_detection(self.judge_member(), "cancelled", "c1", "b1",
                                         "q", "the call is off", at=at()))
        rk.drain(machine_key="mt-a")
        rec = next(iter(self.state().values()))
        self.assertEqual(rec["kind"], "cancel_notice")


@needs_private("bin/lib/reckqueue.py")
class FinalAlertTest(Base):
    def test_fourteen_days_one_final_alert_then_ack_lapses(self):
        ob = self.propose()["ob"]
        self.assertEqual(rk.tick(self.root, T0 + timedelta(days=13), "mt-a/bearing",
                                 machine_key="mt-a"), [])
        with self.assertRaises(rk.Refused):
            rk.transition(ob, "ack", "mt-a/cli", self.root, machine_key="mt-a")
        fired = rk.tick(self.root, T0 + timedelta(days=14), "mt-a/bearing", machine_key="mt-a")
        self.assertEqual(len(fired), 1)
        self.assertEqual(rk.tick(self.root, T0 + timedelta(days=40), "mt-a/bearing",
                                 machine_key="mt-a"), [])  # ONE final alert, ever
        rec = self.state(T0 + timedelta(days=60))[ob]
        self.assertEqual(rec["state"], "proposed")  # never removed before the ack
        self.assertEqual(rk.counts(self.state(T0 + timedelta(days=60)))["awaiting_ack"], 1)
        rk.transition(ob, "ack", "mt-a/cli", self.root, machine_key="mt-a")
        self.assertEqual(self.state()[ob]["state"], "lapsed")

    def test_accept_after_the_final_alert_is_fine(self):
        ob = self.propose()["ob"]
        rk.tick(self.root, T0 + timedelta(days=15), "mt-a/bearing", machine_key="mt-a")
        rk.transition(ob, "accept", "mt-a/cli", self.root, machine_key="mt-a")
        self.assertEqual(self.state()[ob]["state"], "open")

    def test_dm_line_on_change_only_and_counts_only(self):
        self.propose(title="SENTINEL-7f3a title")
        line = rk.dm_line(self.root, T0)
        self.assertIn("+1 proposed", line)
        self.assertNotIn("SENTINEL", line)
        self.assertIsNone(rk.dm_line(self.root, T0))
        rk.tick(self.root, T0 + timedelta(days=14), "mt-a/bearing", machine_key="mt-a")
        line = rk.dm_line(self.root, T0 + timedelta(days=14))
        self.assertIn("FINAL ALERT", line)
        self.assertIsNone(rk.dm_line(self.root, T0 + timedelta(days=14)))

    def test_unconfirmed_dm_is_offered_again(self):
        self.propose()
        self.assertIsNotNone(rk.dm_line(self.root, T0, confirm=False))
        self.assertIsNotNone(rk.dm_line(self.root, T0))


class StickyTest(Base):
    @needs_private("bin/lib/reckqueue.py")
    def test_counts_only_outside_a_private_repo(self):
        self.propose(title="SENTINEL-91c2 do the thing")
        lodestar = make_checkout(os.path.join(self.d, "Lodestar"), "git@github.com:L/L.git")
        lines = rk.sticky_line(cwd=lodestar, root=self.root, as_of=T0)
        self.assertIn("1 proposed", lines[0])
        self.assertNotIn("SENTINEL", "\n".join(lines))
        self.assertNotIn("ob_", "\n".join(lines))
        lines = rk.sticky_line(cwd=self.repo, root=self.root, as_of=T0)
        self.assertIn("SENTINEL-91c2", "\n".join(lines))

    def test_no_ledger_is_one_line_not_an_error(self):
        lines = rk.sticky_line(cwd=self.d, root=os.path.join(self.d, "x", "obligations"))
        self.assertEqual(len(lines), 1)
        self.assertIn("no ledger", lines[0])


class TwoMacsAcceptanceTest(unittest.TestCase):
    """The spec's done-means sequence: detect, crash between outbox and append, the next cycle
    drains it, accept on one Mac, done on the other, sync, both fold it closed; a later reopen
    with a slow clock still wins; a concurrent unseen close surfaces as a conflict."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.bare = os.path.join(self.d, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.bare], check=True)
        self.macs = {}
        for name in ("mt-desk", "mt-lap"):
            repo = make_checkout(os.path.join(self.d, name, "private-store"))
            self.macs[name] = repo
        git(self.macs["mt-desk"], "commit", "-q", "--allow-empty", "-m", "init")
        git(self.macs["mt-desk"], "push", "-q", self.bare, "main")
        git(self.macs["mt-lap"], "pull", "-q", self.bare, "main")
        self._env = {k: os.environ.get(k) for k in ("LODESTAR_STATE_DIR", rk.ROOT_ENV)}

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.d, ignore_errors=True)

    def on(self, mac):
        os.environ["LODESTAR_STATE_DIR"] = os.path.join(self.d, mac, "state")
        os.environ[rk.ROOT_ENV] = os.path.join(self.macs[mac], "obligations")
        return os.environ[rk.ROOT_ENV]

    def sweep(self, mac):
        repo = self.macs[mac]
        git(repo, "add", "-A")
        if git(repo, "status", "--porcelain").strip():
            git(repo, "commit", "-q", "-m", "sweep %s" % mac)
        git(repo, "pull", "-q", "--rebase", self.bare, "main")
        git(repo, "push", "-q", self.bare, "HEAD:main")

    def state(self, mac):
        return rk.fold(rk.read_events(self.on(mac))[0])

    def test_close_on_one_mac_stays_closed_on_both(self):
        self.on("mt-desk")
        item = {"event_id": "mail:X1", "subject": "Plan", "threads": ["conv"], "who": "Z",
                "pointer": "p"}
        rk.outbox_put(rk.needs_you_detection(item, "Orchard", "fp"))
        # crash: the process dies here, between outbox and ledger append. Next cycle:
        self.assertEqual(rk.drain(machine_key="mt-desk")["drained"], 1)
        ob = next(iter(self.state("mt-desk")))
        self.on("mt-desk")
        rk.transition(ob, "accept", "mt-desk/cli", machine_key="mt-desk")
        self.sweep("mt-desk")
        self.sweep("mt-lap")
        self.on("mt-lap")
        rk.transition(ob, "done", "mt-lap/cli", machine_key="mt-lap")
        self.sweep("mt-lap")
        self.sweep("mt-desk")
        self.assertEqual(self.state("mt-desk")[ob]["state"], "done")
        self.assertEqual(self.state("mt-lap")[ob]["state"], "done")
        # A reopen on the desk, which SAW the done, wins whatever its clock says.
        self.on("mt-desk")
        rk.transition(ob, "reopen", "mt-desk/cli", machine_key="mt-desk")
        self.sweep("mt-desk")
        self.sweep("mt-lap")
        self.assertEqual(self.state("mt-lap")[ob]["state"], "open")
        # Concurrent, unseen: done on the laptop, drop on the desk, before either syncs.
        self.on("mt-lap")
        rk.transition(ob, "done", "mt-lap/cli", machine_key="mt-lap")
        self.on("mt-desk")
        rk.transition(ob, "drop", "mt-desk/cli", machine_key="mt-desk")
        self.sweep("mt-lap")
        self.sweep("mt-desk")
        self.sweep("mt-lap")
        self.assertEqual(self.state("mt-desk")[ob]["state"], "conflict")
        self.assertEqual(self.state("mt-lap")[ob]["state"], "conflict")


if __name__ == "__main__":
    unittest.main()


class CliTest(Base):
    def run_cli(self, *args, cwd=None):
        import io
        import contextlib
        import reckon
        out, err = io.StringIO(), io.StringIO()
        old = os.getcwd()
        os.chdir(cwd or self.repo)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = reckon.main(list(args))
        finally:
            os.chdir(old)
        return code, out.getvalue(), err.getvalue()

    @needs_private("bin/reckon.py")
    def test_the_verbs(self):
        code, out, _ = self.run_cli("add", "SENTINEL-c0de write the memo", "--due", "2026-10-20")
        self.assertEqual(code, 0)
        ob = out.split()[0]
        self.assertEqual(self.run_cli("accept", ob, "--said", "yes take it")[0], 0)
        code, out, _ = self.run_cli("list")
        self.assertIn("SENTINEL-c0de", out)
        lodestar = make_checkout(os.path.join(self.d, "Lodestar"), "git@github.com:L/L.git")
        code, out, _ = self.run_cli("list", cwd=lodestar)
        self.assertNotIn("SENTINEL", out)
        code, out, _ = self.run_cli("--sticky", cwd=lodestar)
        self.assertIn("1 open", out)
        self.assertNotIn("SENTINEL", out)
        code, out, _ = self.run_cli("show", ob, cwd=lodestar)
        self.assertNotIn("SENTINEL", out)
        self.assertEqual(self.run_cli("drop", ob)[0], 0)
        self.assertEqual(self.run_cli("drop", ob)[0], 2)  # already dropped: refused
        code, _, err = self.run_cli("add", "x", "--personal")
        self.assertEqual(code, 2)
        self.assertIn("Hestia", err)


KAIROS = """# Actions

- id:           act_0001
  title:        Send the form
  next_action:  Email it
  project:      [[pillar]]
  due:          2026-09-15
  status:       today
  created:      2026-06-25
- id:           act_0002
  title:        Check on the shelf order
  next_action:  Follow up
  project:      null
  due:          null
  status:       active
- id:           act_0003
  title:        SENTINEL-kz letter to ZORRZ
  next_action:  Draft it
  project:      [[zorrz]]
  status:       active
- id:           act_0004
  title:        Old thing
  project:      PILLAR
  status:       done
"""


class KairosImportTest(Base):
    @needs_private("bin/lib/reckoning_kairos.py")
    def test_import_all_live_actions_personal_to_hestia_idempotent(self):
        from lib import reckoning_kairos as kz
        src = os.path.join(self.d, "actions.md")
        with open(src, "w") as fh:
            fh.write(KAIROS)
        hestia = os.path.join(self.d, "Hestia")
        out = kz.run(src, self.root, hestia_dir=hestia, at=at(), machine_key="mt-a")
        self.assertEqual((out["proposals"], out["to_hestia"], out["unclassified"]), (2, 1, 1))
        state = self.state()
        self.assertEqual(len(state), 2)
        self.assertFalse(any("SENTINEL-kz" in json.dumps(r) for r in state.values()))
        wam = [r for r in state.values() if r["title"] == "Send the form"][0]
        self.assertEqual((wam["state"], wam["due"], wam["today"], wam["project"]),
                         ("proposed", "2026-09-15", True, "pillar"))
        self.assertEqual(wam["sources"][0]["pointer"], "dialexis:agenda/actions.md#act_0001")
        files = os.listdir(os.path.join(hestia, "inbox"))
        self.assertEqual(len(files), 1)
        # Unclassified needs an explicit class to be accepted.
        move = [r for r in state.values() if r["class"] == "unclassified"][0]
        with self.assertRaises(rk.Refused):
            rk.transition(move["ob"], "accept", "x", self.root, machine_key="mt-a")
        rk.transition(move["ob"], "accept", "x", self.root, machine_key="mt-a",
                      **{"class": rk.private_class()})
        self.assertEqual(self.state()[move["ob"]]["class"], rk.private_class())
        again = kz.run(src, self.root, hestia_dir=hestia, at=at(days=1), machine_key="mt-a")
        self.assertEqual(again["proposals"], 0)
        self.assertEqual(len(os.listdir(os.path.join(hestia, "inbox"))), 1)
        # The 14-day clock starts at the import, not at the Kairos creation date.
        self.assertEqual(rk.tick(self.root, T0 + timedelta(days=13), machine_key="mt-a"), [])


class BackfillTest(Base):
    @needs_private("bin/reckon.py")
    def test_backfill_existing_needs_you_alerts_once(self):
        import reckon
        proj = os.path.join(self.d, "Orchard")
        os.makedirs(os.path.join(proj, "bearing"))
        with open(os.path.join(proj, "bearing", "notes.md"), "w") as fh:
            fh.write('- 2026-10-12 16:40 — [mail] Zebulon Example: "Confirm plan v6" — keyword · '
                     'needs-you — mail:AAA\n')
        routing_md = os.path.join(self.d, "routing.md")
        with open(routing_md, "w") as fh:
            fh.write("| project | dir | keywords | senders |\n|---|---|---|---|\n"
                     "| Orchard | %s | orchard | |\n" % proj)
        ledger = os.path.join(self.d, "filed.json")
        with open(ledger, "w") as fh:
            json.dump({"mail:AAA": {"event_id": "mail:AAA", "filed_to": "Orchard",
                                    "threads": ["mail-conv:c1"], "needs_you": True,
                                    "needs_you_fp": "abc"},
                       "mail:BBB": {"event_id": "mail:BBB", "filed_to": "Orchard"}}, fh)
        out = reckon.backfill_needs_you(ledger, routing_md, root=self.root)
        self.assertEqual((out["needs_you_alerts"], out["drained"]), (1, 1))
        rec = next(iter(self.state().values()))
        self.assertEqual((rec["title"], rec["project"]), ("Confirm plan v6", "Orchard"))
        # The same observation id a live cycle computes, so a re-run (or the live cycle) adds none.
        live = rk.needs_you_detection({"event_id": "mail:AAA", "threads": ["mail-conv:c1"]},
                                      "Orchard", "abc")
        self.assertEqual(live["obs"], rec["observations"][0])
        self.assertEqual(reckon.backfill_needs_you(ledger, routing_md, root=self.root)["drained"],
                         0)


class ReviewFindingsTest(Base):
    """GPT review 2026-10-13 of the stage-1 diff: each test is one counterexample it gave."""

    def test_push_default_and_branch_push_remote_urls_are_checked(self):
        git(self.repo, "config", "remote.pushDefault", "git@github.com:o/leak.git")
        self.assertFalse(rk.private_only(self.repo))
        git(self.repo, "config", "--unset", "remote.pushDefault")
        git(self.repo, "config", "branch.main.pushRemote", "https://github.com/o/leak.git")
        self.assertFalse(rk.private_only(self.repo))
        git(self.repo, "config", "branch.main.pushRemote", "origin")  # a named remote is fine
        self.assertTrue(rk.private_only(self.repo))

    def test_only_known_transports(self):
        self.assertFalse(rk.is_private_url("bridge://git.private.example/x"))
        self.assertFalse(rk.is_private_url("ext::ssh git.private.example"))

    def test_a_symlinked_events_dir_out_of_the_checkout_refuses(self):
        outside = make_checkout(os.path.join(self.d, "Lodestar"), "git@github.com:L/L.git")
        os.makedirs(self.root)
        os.symlink(os.path.join(outside, "leaked"), os.path.join(self.root, "events"))
        os.makedirs(os.path.join(outside, "leaked"))
        with self.assertRaises((rk.Refused, rk.StoreUnavailable)):
            self.propose(title="SENTINEL-sym")
        for dp, _, fs in os.walk(outside):
            for f in fs:
                if f.endswith(".jsonl"):
                    self.fail("wrote under the github.com checkout")

    def test_a_crash_between_ack_and_lapse_cannot_strand_it(self):
        ob = self.propose()["ob"]
        rk.tick(self.root, T0 + timedelta(days=14), machine_key="mt-a")
        rk.transition(ob, "ack", "mt-a/cli", self.root, machine_key="mt-a")
        events, _ = rk.read_events(self.root)
        self.assertEqual(sum(1 for e in events if e["ob"] == ob and e["ev"] in ("ack", "lapse")),
                         1)  # one event: nothing to crash between
        self.assertEqual(self.state()[ob]["state"], "lapsed")

    def test_an_amend_does_not_classify_an_unclassified_proposal(self):
        obs = rk.obs_id("kairos", "dialexis", "act_9", "kairos-import")
        ev = rk.make_event("propose", rk.ob_id(obs), "x", at=at(), obs=obs, title="t",
                           **{"class": "unclassified"})
        rk.append([ev], self.root, machine_key="mt-a")
        rk.transition(ev["ob"], "amend", "x", self.root, machine_key="mt-a", note="spelling")
        self.assertEqual(self.state()[ev["ob"]]["class"], "unclassified")
        with self.assertRaises(rk.Refused):
            rk.transition(ev["ob"], "accept", "x", self.root, machine_key="mt-a")

    def test_two_new_obligations_in_one_thread_and_beacon_stay_two(self):
        m = {"event_id": "mail:1", "threads": ["T"], "source_label": "mail", "scope": "P"}
        m2 = dict(m, event_id="mail:2")
        rk.outbox_put(rk.judge_detection(m, "new_obligation", "C1", "B", "approve the budget",
                                         "Approve budget", at=at()))
        rk.outbox_put(rk.judge_detection(m2, "new_obligation", "C2", "B", "send the slides",
                                         "Send slides", at=at(minutes=1)))
        rk.drain(self.root, machine_key="mt-a")
        self.assertEqual(sorted(r["title"] for r in self.state().values()),
                         ["Approve budget", "Send slides"])

    def test_an_unconnected_transition_cannot_close_a_proposal(self):
        p = self.propose()
        d = rk.make_event("done", p["ob"], "other", at=at(minutes=1),
                          supersedes=["ev_" + "0" * 20])
        rec = rk.fold([p, d], T0)[p["ob"]]
        self.assertEqual(rec["state"], "proposed")
        self.assertEqual(rec["orphans"], 1)
        with self.assertRaises(rk.Refused):  # and a transition must name what it saw
            bare = rk.make_event("done", p["ob"], "other", at=at(minutes=2))
            rk.append([bare], self.root, machine_key="mt-a")

    def test_a_later_observed_amend_wins_over_a_faster_clock(self):
        ob = self.propose()["ob"]
        rk.transition(ob, "accept", "x", self.root, machine_key="mt-a")
        rk.transition(ob, "amend", "x", self.root, machine_key="mt-a", due="2026-11-01")
        events, _ = rk.read_events(self.root)
        rec = rk.fold(events, T0)[ob]
        m2 = rk.make_event("amend", ob, "x", at=at(days=-3), supersedes=rec["heads"] +
                           rec["tips"], due="2026-11-10")  # slow clock, but saw M1
        self.assertEqual(rk.fold(events + [m2], T0)[ob]["due"], "2026-11-10")

    def test_two_macs_ticking_concurrently_record_one_final_alert(self):
        ob = self.propose()["ob"]
        a = rk.tick(self.root, T0 + timedelta(days=15), "mt-a/bearing", machine_key="mt-a")
        events, _ = rk.read_events(self.root)
        b_view = [e for e in events if e["ev"] != "final_alert"]
        rec = rk.fold(b_view, T0 + timedelta(days=16))[ob]
        b = rk._final_alert_event(rec)
        self.assertEqual(a[0]["id"], b["id"])

    @needs_private("bin/lib/reckqueue.py")
    def test_counts_mode_never_prints_warning_text_and_bad_shapes_are_skipped(self):
        self.propose()
        day_file = [os.path.join(dp, f) for dp, _, fs in os.walk(self.root) for f in fs
                    if f.endswith(".jsonl")][0]
        with open(day_file, "a") as fh:
            fh.write(json.dumps({"ev": "propose", "class": "SENTINEL-private"}) + "\n")
            fh.write("[]\nnull\n")
        events, warnings = rk.read_events(self.root)
        self.assertEqual(len(events), 1)
        lodestar = make_checkout(os.path.join(self.d, "Lodestar"), "git@github.com:L/L.git")
        out = "\n".join(rk.sticky_line(cwd=lodestar, root=self.root, as_of=T0))
        self.assertNotIn("SENTINEL", out)
        self.assertIn("3 unreadable", out)

    def test_due_can_be_cleared_and_a_bad_date_is_refused(self):
        ob = self.propose()["ob"]
        rk.transition(ob, "accept", "x", self.root, machine_key="mt-a", due="2026-10-20")
        rk.transition(ob, "amend", "x", self.root, machine_key="mt-a", due="")
        self.assertEqual(self.state()[ob]["due"], "")
        with self.assertRaises(rk.Refused):
            rk.transition(ob, "amend", "x", self.root, machine_key="mt-a", due="next friday")


class SafetyCacheTest(Base):
    """Audit 2026-10-19: a `handled` press ran ~12 git spawns (store_root twice, _checkout_of,
    _contained per event, ssh -G) and timed out under load. The private-only verdict is cached in
    machine-local state for SAFETY_TTL_S, keyed on the checkout and the stat of every file that
    can change where it pushes; a stale or missing entry falls back to the live check."""

    def no_spawn(self):
        real = subprocess.run

        def refuse(*a, **k):
            raise AssertionError("spawned %r on a warm cache" % (a[0] if a else k.get("args"),))
        from unittest import mock
        return mock.patch.object(rk.subprocess, "run", side_effect=refuse), real

    def test_a_warm_click_runs_no_git_or_ssh(self):
        ev = self.propose()
        patch, _ = self.no_spawn()
        with patch:
            rk.store_root(self.root)
            rk.transition(ev["ob"][:12], "snooze", "mt-a/companion", root=self.root,
                          machine_key="mt-a", until=at(days=1))
        self.assertTrue(os.path.exists(os.path.join(self.state_dir, rk.SAFETY_CACHE_NAME)))

    def test_a_changed_remote_is_seen_at_once(self):
        self.propose()
        git(self.repo, "remote", "set-url", "--push", "origin", "git@github.com:L/x.git")
        with self.assertRaises(rk.StoreUnavailable):
            rk.store_root(self.root)

    def test_a_stale_or_missing_cache_falls_back_to_the_live_check(self):
        self.propose()
        path = os.path.join(self.state_dir, rk.SAFETY_CACHE_NAME)
        with open(path) as fh:
            cache = json.load(fh)
        for entry in cache["tops"].values():
            entry["at"] -= rk.SAFETY_TTL_S + 1
        with open(path, "w") as fh:
            json.dump(cache, fh)
        patch, _ = self.no_spawn()
        with patch, self.assertRaises(AssertionError):
            rk.store_root(self.root)          # stale: it must ask git again
        rk.store_root(self.root)              # and re-warm
        os.remove(path)
        with patch, self.assertRaises(AssertionError):
            rk.store_root(self.root)          # missing: live again
        with open(path, "w") as fh:
            fh.write("{not json")
        rk.store_root(self.root)              # unreadable: live, then rewritten

    def test_a_nested_repo_made_after_caching_still_refuses(self):
        self.propose()
        os.makedirs(os.path.join(self.root, "events"), exist_ok=True)
        subprocess.run(["git", "init", "-q", os.path.join(self.root, "events")], check=True)
        with self.assertRaises((rk.Refused, rk.StoreUnavailable)):
            self.propose(message="m2", title="SENTINEL-nested")

    def test_a_refusal_is_never_cached(self):
        git(self.repo, "remote", "set-url", "origin", "git@github.com:L/x.git")
        with self.assertRaises(rk.StoreUnavailable):
            rk.store_root(self.root)
        git(self.repo, "remote", "set-url", "origin", PRIVATE_URL)
        rk.store_root(self.root)
