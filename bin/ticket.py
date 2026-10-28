#!/usr/bin/env python3
"""Tickets for every Lodestar project, from any repo, in one line (D30, 2026-10-21).

    ticket new   --project P --type build|bug|repair|action|note "title"
                 [--body TEXT|-] [--owner O] [--due D] [--act-by D] [--effort 30m] [--next]
                 [--link commit:abc] [--source agent] [--likely-fix L] [--private] [--offline]
                 [--check JSON | --gap REASON]     # outcome check (lib/outcome.py); default: a gap
    ticket list  [--project P | --all] [--status S] [--type T] [--mine] [--json]
    ticket show  ID
    ticket move  ID proposed|open|active|blocked [--why TEXT]
    ticket close ID (--said "his words" | --evidence commit:<sha>) [--release]
    ticket drop  ID --said "his words"        # both name the dependents that lost a blocker
    ticket edit  ID [--due D] [--act-by D] [--owner O] [--effort E] [--next|--not-next]
                 [--snooze-until D] [--title T] [--note TEXT] [--check JSON | --gap REASON]
    ticket link  ID (--blocks ID2 | --blocked-by ID2 | --relates ID2 | --ref commit:abc
                     | --unblock ID2)            # a link that makes a wait cycle is refused
    ticket link  ID --blocked-on "a rainy day"   # a free-text condition: never clears by itself
    ticket link  ID --blocked-on "until 2026-11-01"   # (or a bare ISO date) clears when it passes
    ticket link  ID --blocked-on owner/Repo#12 [--host <private host>]
                 # or an issue URL (github.com / the private host): clears when closed, per gh;
                 # unreachable is "unknown", never "done"
    ticket link  ID --met "a rainy day" (--said "his words" | --evidence TEXT)   # end a condition
    ticket ready [--project P | --all]           # open, every blocker done; plus stale/dropped
                 # blocks and a "waiting on a condition" section (with ages) for non-ticket blockers
    ticket need  --from P[:ID] --to Q "title" [--type build] [--body TEXT]
    ticket inbox [--project P]
    ticket search WORDS... [--project P]
    ticket index --project P [--write]
    ticket check [--project P]
    ticket homes
    ticket stats [--json]      # filed/week by source, lead time, slip, needs turnaround (git log)
    ticket face  [--json]

Project names: the sync/registry.md Name or Id, or the ticket code (lodestar, LODE, PILLAR).
Homes, codes and the private rule: sync/ticket-homes.md. Each write is one file, one commit
(pathspec), pushed. In a github.com session, `list`/`show` of a private-host project print ids,
types, statuses and dates only.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib import tickets as tk  # noqa: E402


def _private_flags():
    """`--private`, plus this deployment's class tag as an alias (`lib.boundary`), so the flag
    an existing fleet already types keeps working."""
    from lib import boundary
    tag = boundary.private_class()
    return ("--private",) + (("--" + tag,) if tag and tag != "private" else ())


def _session_public() -> bool:
    """True when this session's cwd is a github.com checkout (titles of private projects withheld)."""
    try:
        from lib import reckoning
        top = reckoning.repo_top(os.getcwd())
        return bool(top) and not reckoning.private_only(top)
    except Exception:  # noqa: BLE001
        return True


def _withhold(t) -> bool:
    """Titles of a private-host (or overlay) ticket are withheld in a github.com session."""
    return _session_public() and not _homes_public(t)


_PUBLIC_CACHE = {}


def _homes_public(t) -> bool:
    d = os.path.dirname(t.get("_path") or "")
    if d not in _PUBLIC_CACHE:
        try:
            from lib import reckoning
            top = reckoning.repo_top(d)
            _PUBLIC_CACHE[d] = bool(top) and not reckoning.private_only(top)
        except Exception:  # noqa: BLE001
            _PUBLIC_CACHE[d] = False
    return _PUBLIC_CACHE[d]


def _line(t, withhold: bool) -> str:
    bits = [t["id"], t.get("status", ""), t.get("type", ""), t.get("owner", "")]
    if t.get("due"):
        bits.append("due " + t["due"])
    if t.get("next") == "yes":
        bits.append("NEXT")
    head = "  ".join(b for b in bits if b)
    return head if withhold else "%s  %s" % (head, t.get("title", ""))


def _homes(a):
    if getattr(a, "all", False) or not getattr(a, "project", None):
        return tk.all_homes()
    return tk.homes_for(a.project)


def _check_arg(a):
    """--check JSON (validated) or --gap REASON as a spec dict; None when neither was given."""
    from lib import outcome
    if getattr(a, "check", None):
        try:
            return outcome.validate_spec(a.check)
        except ValueError as e:
            raise tk.Refused("bad --check: %s" % e)
    if getattr(a, "gap", None):
        return outcome.gap(a.gap)
    return None


def _body(text):
    if text == "-":
        return sys.stdin.read()
    return text or ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ticket", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="verb", required=True)

    p = sub.add_parser("new")
    p.add_argument("--name", required=True,
                   help="SUBJECT_INTENT_TWIST id, e.g. FETCHER_429_RETRY_STILL_DROPPING (see namecheck)")
    p.add_argument("title", nargs="+")
    p.add_argument("--project", required=True)
    p.add_argument("--type", required=True, choices=tk.TYPES)
    p.add_argument("--body", default="")
    p.add_argument("--owner", default="lukas")
    p.add_argument("--status", default="open", choices=("proposed", "open", "active", "blocked"))
    p.add_argument("--due", default="")
    p.add_argument("--act-by", default="")
    p.add_argument("--effort", default="")
    p.add_argument("--next", action="store_true")
    p.add_argument("--link", action="append", default=[])
    p.add_argument("--source", default="manual")
    p.add_argument("--likely-fix", default="")
    p.add_argument("--check", default="", help="outcome check spec, JSON (lib/outcome.py)")
    p.add_argument("--gap", default="", help="why no outcome check exists (recorded)")
    p.add_argument(*_private_flags(), dest="private", action="store_true",
                   help="file in the project's private-store overlay")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--no-push", action="store_true")

    p = sub.add_parser("list")
    p.add_argument("--project")
    p.add_argument("--all", action="store_true")
    p.add_argument("--status")
    p.add_argument("--type")
    p.add_argument("--mine", action="store_true")
    p.add_argument("--closed", action="store_true", help="include done/dropped")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("show")
    p.add_argument("id")

    for verb in ("move", "close", "drop"):
        p = sub.add_parser(verb)
        p.add_argument("id")
        if verb == "move":
            p.add_argument("status", choices=("proposed", "open", "active", "blocked"))
        p.add_argument("--said", default="")
        p.add_argument("--evidence", default="")
        p.add_argument("--why", default="")
        if verb == "close":
            p.add_argument("--release", action="store_true",
                           help="move dependents whose blockers are now all done to open")
        p.add_argument("--no-push", action="store_true")

    p = sub.add_parser("ready")
    p.add_argument("--project")
    p.add_argument("--all", action="store_true")

    p = sub.add_parser("edit")
    p.add_argument("id")
    for f in ("due", "act-by", "owner", "effort", "snooze-until", "title", "likely-fix", "note"):
        p.add_argument("--" + f)
    p.add_argument("--next", action="store_true")
    p.add_argument("--not-next", action="store_true")
    p.add_argument("--check", default=None, help="outcome check spec, JSON (lib/outcome.py)")
    p.add_argument("--gap", default=None, help="why no outcome check exists (recorded)")
    p.add_argument("--no-push", action="store_true")

    p = sub.add_parser("link")
    p.add_argument("id")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--blocks")
    g.add_argument("--blocked-by", help="a ticket id (a date, issue ref or text is routed to --blocked-on)")
    g.add_argument("--blocked-on", help="free-text condition, 'until YYYY-MM-DD', or an issue ref/URL")
    g.add_argument("--met", help="end a non-ticket blocker (needs --said or --evidence)")
    g.add_argument("--relates")
    g.add_argument("--ref")
    g.add_argument("--unblock", help="remove a blocked-by (the answer to a dropped blocker)")
    p.add_argument("--host", default="github.com", choices=tk.gh_hosts(),
                   help="host for a bare owner/repo#N")
    p.add_argument("--said", default="")
    p.add_argument("--evidence", default="")
    p.add_argument("--no-push", action="store_true")

    p = sub.add_parser("rename")
    p.add_argument("old")
    p.add_argument("new", help="SUBJECT_INTENT_TWIST name")
    p.add_argument("--no-push", action="store_true")

    p = sub.add_parser("namecheck")
    p.add_argument("name", nargs="?")
    p.add_argument("--bank", action="store_true")

    p = sub.add_parser("need")
    p.add_argument("--name", required=True, help="SUBJECT_INTENT_TWIST id")
    p.add_argument("title", nargs="+")
    p.add_argument("--from", dest="src", required=True)
    p.add_argument("--to", required=True)
    p.add_argument("--type", default="build", choices=tk.TYPES)
    p.add_argument("--body", default="")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--no-push", action="store_true")

    p = sub.add_parser("inbox")
    p.add_argument("--project")

    p = sub.add_parser("search")
    p.add_argument("words", nargs="+")
    p.add_argument("--project")

    p = sub.add_parser("index")
    p.add_argument("--project", required=True)
    p.add_argument("--write", action="store_true")

    p = sub.add_parser("check")
    p.add_argument("--project")

    sub.add_parser("homes")

    p = sub.add_parser("migrate-actions")
    p.add_argument("--project", required=True)
    p.add_argument("--write", action="store_true")
    p.add_argument("--names", help="file: legacy item id -> SUBJECT_INTENT_TWIST name (JSON or 'id NAME' lines); required with --write")

    p = sub.add_parser("stats")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("face")
    p.add_argument("--json", action="store_true")

    a = ap.parse_args(argv)
    _issues_pointer(a)
    try:
        return _run(a)
    except tk.Refused as exc:
        print("ticket: %s" % exc, file=sys.stderr)
        return 2


def _issues_pointer(a) -> None:
    """One stderr line when the project's issues are canonical (a `canonical: issues` project); reads
    still work, writes are refused by lib/tickets.py with the same pointer."""
    try:
        proj = getattr(a, "project", None)
        if not proj and getattr(a, "id", None):
            proj = a.id.split("-")[0]
        if not proj:
            return
        name = tk.issues_canonical(tk.homes_for(proj)[0].dir)
        if name:
            print("ticket: " + tk.issues_pointer(name), file=sys.stderr)
    except Exception:  # noqa: BLE001  (a pointer must never break a read)
        return


def _run(a) -> int:
    push = not getattr(a, "no_push", False)
    if a.verb == "rename":
        nid, state = tk.rename(a.old, a.new, push=push)
        print("renamed %s -> %s  (%s)" % (a.old, nid, state))
        return 0
    if a.verb == "namecheck":
        from lib import ticketname as tn
        if a.bank or not a.name:
            print(" ".join(sorted(tn.INTENT_WORDS)))
            return 0
        parts, probs = tn.parse(a.name), tn.problems(a.name)
        if parts:
            print("subject=%s  intent=%s  twist=%s" % tuple(("_".join(x) if isinstance(x, list) else x)
                                                           for x in parts))
        print("OK" if not probs else "REFUSED: " + "; ".join(probs))
        return 0 if not probs else 1
    if a.verb == "new":
        links = ", ".join(a.link)
        check = _check_arg(a)
        tid, path, state = tk.create(
            a.project, " ".join(a.title), a.type, _body(a.body), owner=a.owner, status=a.status,
            source=a.source, private=a.private, offline=a.offline, push=push, name=a.name, due=a.due,
            act_by=a.act_by, effort=a.effort, next="yes" if a.next else "", links=links,
            likely_fix=a.likely_fix, check=check)
        print("filed %s  %s  (%s)" % (tid, path, state))
        return 0

    if a.verb in ("list", "inbox", "search"):
        homes = _homes(a)
        ts = tk.load(homes)
        if a.verb == "inbox":
            ts = [t for t in ts if t.get("status") == "proposed"]
        elif a.verb == "search":
            words = [w.lower() for w in a.words]
            ts = [t for t in ts if all(w in (t.get("title", "") + " " + t.get("_body", "")).lower()
                                       for w in words)]
        else:
            if a.status:
                ts = [t for t in ts if t.get("status") == a.status]
            elif not a.closed:
                ts = [t for t in ts if t.get("status") not in tk.TERMINAL]
            if a.type:
                ts = [t for t in ts if t.get("type") == a.type]
            if a.mine:
                ts = [t for t in ts if tk.mine(t)]
        ts.sort(key=lambda t: (t.get("project", ""), tk._num(t["id"]), t["id"]))
        if getattr(a, "json", False):
            print(json.dumps([{k: v for k, v in t.items() if not k.startswith("_")} for t in ts],
                             indent=1))
            return 0
        for t in ts:
            print(_line(t, _withhold(t)))
        print("%d ticket%s" % (len(ts), "" if len(ts) == 1 else "s"))
        return 0

    if a.verb == "show":
        t, h = tk.find(a.id)
        if _session_public() and (h.kind == "overlay" or not h.public):
            print(_line(t, True))
            print("(text withheld in a github.com session; open %s)" % t["_path"])
            return 0
        print(tk.render({k: v for k, v in t.items() if not k.startswith("_")}, t["_body"]))
        print("# %s" % t["_path"])
        return 0

    if a.verb in ("move", "close", "drop"):
        status = {"close": "done", "drop": "dropped"}.get(a.verb) or a.status
        before = tk.find(a.id)[0]
        f, state = tk.move(a.id, status, said=a.said, why=a.why, evidence=a.evidence, push=push)
        print("%s %s  (%s)" % (a.id, f.get("status"), state))
        if status in ("open", "active") and before.get("status") == "blocked":
            left = [(x, s) for x, s in tk.blocker_states(before, external=False) if s != "done"]
            if left:
                print("warning: %s still has blockers not done: %s" % (
                    a.id, ", ".join("%s %s" % (tk.describe(x, _session_public()), s2)
                                    for x, s2 in left)))
        if status in tk.TERMINAL and state != "unchanged":
            rows = tk.dependents_after(a.id)
            moved = tk.release(rows, a.id, push=push) if getattr(a, "release", False) else []
            for r in rows:
                if r["kind"] == "ready":
                    print("  %s: every blocker done%s" % (
                        r["id"], ", released to open" if r["id"] in moved else
                        "; move it open? (ticket move %s open)" % r["id"]
                        if r["status"] == "blocked" else ", ready"))
                elif r["kind"] == "confirm":
                    print("  %s: blocker %s dropped; confirm (ticket link %s --unblock %s, or drop it)"
                          % (r["id"], a.id, r["id"], a.id))
                elif r["kind"] == "waiting":
                    print("  %s: still waits on %s" % (r["id"], ", ".join(
                        tk.describe(w, _session_public()) for w in r["waiting"])))
                else:
                    print("  %s: named in blocks but not found on this machine" % r["id"])
        return 0

    if a.verb == "ready":
        ts = tk.load(_homes(a))
        ready, unblocked, dropped = tk.frontier(ts, tk.lookup_from(ts))
        key = lambda t: (t.get("project", ""), tk._num(t["id"]), t["id"])
        for t in sorted(ready, key=key):
            print(_line(t, _withhold(t)))
        for t, bs in sorted(unblocked, key=lambda r: key(r[0])):
            print("%s  unblocked by %s, move it open?" % (
                _line(t, _withhold(t)), ", ".join(tk.describe(b, _withhold(t)) for b in bs)))
        for t, bs in sorted(dropped, key=lambda r: key(r[0])):
            print("%s  blocker %s dropped, confirm" % (_line(t, _withhold(t)), ", ".join(bs)))
        now = tk._now()
        for t, cs in sorted(tk.cleared_rows(ts, now), key=lambda r: key(r[0])):
            print("%s  cleared: %s" % (_line(t, _withhold(t)),
                                       ", ".join(tk.describe(c, _withhold(t)) for c in cs)))
        waits = sorted(tk.waiting_rows(ts, tk.lookup_from(ts), now), key=lambda r: key(r[0]))
        if waits:
            print("waiting on a condition:")
            for t, ws in waits:
                print("  " + _line(t, _withhold(t)))
                for x, st, age in ws:
                    print("    %s  [%s, waiting %s]" % (tk.describe(x, _withhold(t)), st, age))
        print("%d ready, %d unblocked, %d dropped-blocker, %d waiting on a condition" % (
            len(ready), len(unblocked), len(dropped), len(waits)))
        return 0

    if a.verb == "edit":
        ch = {}
        for f in ("due", "act_by", "owner", "effort", "snooze_until", "title", "likely_fix"):
            v = getattr(a, f)
            if v is not None:
                key = "snoozed_until" if f == "snooze_until" else f
                ch[key] = v if v != "" else None
        if a.next:
            ch["next"] = "yes"
        if a.not_next:
            ch["next"] = None
        if a.check is not None or a.gap is not None:
            from lib import outcome
            ch["check"] = outcome.encode(_check_arg(a))
        if not ch and not a.note:
            print("ticket: nothing to change", file=sys.stderr)
            return 2
        msg = "edit " + ", ".join(sorted(ch)) if ch else "note"
        f, state = tk.update(a.id, msg, push=push, body_append=a.note or "", **ch)
        print("%s edited  (%s)" % (a.id, state))
        return 0

    if a.verb == "link":
        for rel in ("blocks", "blocked_by", "blocked_on", "met", "relates", "ref", "unblock"):
            v = getattr(a, rel)
            if v:
                states = tk.link(a.id, rel, v, push=push, host=a.host, said=a.said,
                                 evidence=a.evidence)
                shown = v
                if rel in ("blocked_on", "met"):
                    try:
                        shown = tk.describe(tk.classify(v, a.host)[1], _session_public())
                    except tk.Refused:
                        shown = tk.describe(v, _session_public())
                print("%s %s %s  (%s)" % (a.id, rel.replace("_", "-"), shown, ", ".join(states)))
        return 0

    if a.verb == "need":
        src, _, from_ticket = a.src.partition(":")
        tid, path, state = tk.need(src, a.to, " ".join(a.title), _body(a.body), a.type,
                                   from_ticket=from_ticket, push=push, offline=a.offline, name=a.name)
        print("proposed %s in %s's inbox  %s  (%s)" % (tid, a.to, path, state))
        return 0

    if a.verb == "index":
        homes = tk.homes_for(a.project)
        text = tk.index_md(homes[0].project, [t for t in tk.load(homes[:1])])
        if a.write:
            path = os.path.join(homes[0].dir, "INDEX.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            print("%s  (%s)" % (path, tk.commit(homes[0], [path], "ticket: index")))
        else:
            sys.stdout.write(text)
        return 0

    if a.verb == "check":
        homes = tk.homes_for(a.project) if a.project else tk.all_homes()
        ts = tk.load(homes if a.project else tk.all_homes())
        everything = tk.load(tk.all_homes())
        known = {t["id"] for t in everything}
        get = tk.lookup_from(everything)
        seen, bad = {}, 0
        for t in ts:
            if t["id"] in seen:
                print("%s duplicate: %s and %s" % (t["id"], seen[t["id"]], t["_path"]))
                bad += 1
            seen[t["id"]] = t["_path"]
            for prob in tk.problems(t, known) + tk.link_problems(t, get):
                print("%s: %s" % (t["id"], prob))
                bad += 1
        print("%d tickets checked, %d problem%s" % (len(ts), bad, "" if bad == 1 else "s"))
        return 1 if bad else 0

    if a.verb == "homes":
        for r in tk.homes_table():
            try:
                hs = tk.homes_for(r["name"])
                where = "; ".join("%s %s%s" % (h.kind, h.dir, " (github.com)" if h.public else "")
                                  for h in hs)
            except tk.Refused as exc:
                where = "-- %s" % exc
            print("%-6s %-28s %s" % (r.get("code") or "", r["name"], where))
        return 0

    if a.verb == "stats":
        from lib import ticketstats
        st = ticketstats.gather(tk.homes_table(), tk.homes_for)
        print(json.dumps(st, indent=2) if a.json else ticketstats.render(st))
        return 0

    if a.verb == "migrate-actions":
        rep = tk.migrate_actions(a.project, write=a.write, names=tk.load_names(a.names) if a.names else None)
        print("migrate-actions %s: %d items %s from ACTIONS.md@%s%s" % (
            a.project, rep["items"], rep["by_status"], rep["commit"],
            ("; %s..%s (%s)" % (rep["ids"][0], rep["ids"][1], rep["git"])) if a.write else " (dry run)"))
        return 0

    if a.verb == "face":
        rows = tk.face_rows(tk.load(tk.all_homes()))
        if a.json:
            print(json.dumps(rows, indent=1))
        else:
            for r in rows:
                print("%-5s %-6s %s  %s" % (r["tier"], r["when"], r["ticket"],
                                            "" if _withhold({"_path": r["path"]}) else r["title"]))
            print("%d lamp%s" % (len(rows), "" if len(rows) == 1 else "s"))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
