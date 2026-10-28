"""Ticket statistics from git history across ticket homes (LODE-13; design 2026-10-21-tickets.md
section 9 items 1-3). Deterministic, no model calls.

Measured: tickets filed per ISO week by source; lead time (filed -> done); slip (done date minus
`due`, for done tickets that carried a due); needs turnaround (a `need:<project>` ticket: filed ->
accepted out of `proposed`, and filed -> done). All dates come from commit author dates of the
ticket file's own history, one `git log -p` per home. A home that cannot be read on this machine
is reported as unreachable with the reason, never as zero.
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timezone
from statistics import median
from typing import Dict, List, Optional

_FIELD = re.compile(r"^\+(status|source|due|type): ?(.*)$")
_DIFF = re.compile(r"^diff --git a/(\S+) b/(\S+)$")
_ID = re.compile(r"^(?:[A-Z][A-Z0-9]{1,5}-(?:\d+|x[a-z2-7]{5})|[A-Z0-9]+(?:_[A-Z0-9]+){2,6})$")
_NON_OPEN = ("proposed",)


def _dt(s: str) -> Optional[datetime]:
    s = (s or "").strip().strip("\"'")
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def scan_repo(repo: str, reldir: str = "tickets") -> Dict[str, dict]:
    """Per-ticket history from git: {id: {filed, source, type, due, status, timeline[(dt, status)]}}."""
    out = subprocess.run(
        ["git", "-C", repo, "log", "--reverse", "--no-merges", "--no-renames", "-p", "-U0",
         "--format=%x01%H%x09%aI", "--", reldir + "/"],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise OSError((out.stderr or "git log failed").strip().splitlines()[-1])
    tickets: Dict[str, dict] = {}
    when, cur, in_front = None, None, False
    for line in out.stdout.splitlines():
        if line.startswith("\x01"):
            when, cur = _dt(line.split("\t", 1)[1]), None
            continue
        m = _DIFF.match(line)
        if m:
            base = os.path.basename(m.group(2))
            tid = base[:-3] if base.endswith(".md") and os.path.dirname(m.group(2)) == reldir else ""
            cur = tid if _ID.match(tid) else None
            in_front = False
            continue
        if cur is None or when is None:
            continue
        if line.startswith("new file mode"):
            tickets.setdefault(cur, {"id": cur, "filed": when, "timeline": []})
            continue
        if line.startswith("deleted file mode"):
            tickets.pop(cur, None)
            cur = None
            continue
        t = tickets.get(cur)
        if t is None:
            continue
        fm = _FIELD.match(line)
        if fm:
            k, v = fm.group(1), fm.group(2).strip().strip("\"'")
            if k == "status":
                t["timeline"].append((when, v))
                t["status"] = v
            else:
                t[k] = v
    return tickets


def _days(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 86400.0


def _summ(xs: List[float]) -> dict:
    if not xs:
        return {"n": 0}
    xs = sorted(xs)
    p90 = xs[min(len(xs) - 1, int(round(0.9 * (len(xs) - 1))))]
    return {"n": len(xs), "median": round(median(xs), 2), "mean": round(sum(xs) / len(xs), 2),
            "p90": round(p90, 2), "max": round(xs[-1], 2)}


def _norm_source(s: str) -> str:
    s = (s or "unknown").strip() or "unknown"
    return s.split(":", 1)[0]


def week_of(d: datetime) -> str:
    y, w, _ = d.isocalendar()
    return "%d-W%02d" % (y, w)


def compute(tickets: List[dict], now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    weeks: Dict[str, Dict[str, int]] = {}
    lead, slips, need_acc, need_done = [], [], [], []
    late = past_due_open = with_due = 0
    for t in tickets:
        src = _norm_source(t.get("source"))
        weeks.setdefault(week_of(t["filed"]), {}).setdefault(src, 0)
        weeks[week_of(t["filed"])][src] += 1
        tl = t["timeline"]
        closed = None
        if t.get("status") == "done":
            closed = next((w for w, s in reversed(tl) if s == "done"), None)
        if closed:
            lead.append(_days(t["filed"], closed))
        due = _dt(t.get("due", "")) if t.get("due") else None
        if due:
            if closed:
                with_due += 1
                dd = _days(due, closed) if "T" in t["due"] else \
                    (closed.date() - due.date()).days
                slips.append(dd)
                late += dd > 0
            elif t.get("status") not in ("done", "dropped") and due < now:
                past_due_open += 1
        if (t.get("source") or "").startswith("need:"):
            acc = next((w for w, s in tl if s not in _NON_OPEN and s != "dropped"), None)
            if acc:
                need_acc.append(_days(t["filed"], acc))
            if closed:
                need_done.append(_days(t["filed"], closed))
    need_total = sum(1 for t in tickets if (t.get("source") or "").startswith("need:"))
    return {
        "tickets": len(tickets),
        "per_week": {w: dict(sorted(weeks[w].items())) for w in sorted(weeks)},
        "lead_time_days": _summ(lead),
        "slip_days": dict(_summ(slips), done_with_due=with_due, late=late, open_past_due=past_due_open),
        "needs": {"filed": need_total, "accepted_days": _summ(need_acc), "done_days": _summ(need_done),
                  "awaiting_accept": need_total - len(need_acc)},
    }


def gather(homes_table, homes_for, scan=scan_repo) -> dict:
    """Walk every declared home. `homes_for(name)` raises on an unplaceable project (-> unreachable)."""
    reachable, unreachable, seen, tickets = [], [], set(), []
    for r in homes_table:
        name = r["name"]
        try:
            hs = homes_for(name)
        except Exception as exc:  # noqa: BLE001
            code = (r.get("home") or "").strip()
            if code in ("", "none") or code.startswith("redirect:"):
                continue                                  # keeps no tickets of its own: not a gap
            unreachable.append({"project": name, "code": (r.get("code") or "").strip(),
                                "reason": str(exc)})
            continue
        for h in hs:
            key = os.path.realpath(h.dir)
            if key in seen:
                continue
            seen.add(key)
            rel = os.path.relpath(key, os.path.realpath(h.repo))
            try:
                found = scan(h.repo, rel)
            except OSError as exc:
                unreachable.append({"project": name, "code": h.code, "reason": str(exc)})
                continue
            reachable.append({"project": name, "code": h.code, "kind": h.kind, "tickets": len(found)})
            tickets.extend(found.values())
    out = compute(tickets)
    out["homes_read"] = reachable
    out["homes_unreachable"] = sorted(unreachable, key=lambda u: u["project"])
    return out


def _s(d: dict, unit: str = "d") -> str:
    if not d.get("n"):
        return "no data"
    return "n=%d  median %s%s  mean %s%s  p90 %s%s  max %s%s" % (
        d["n"], d["median"], unit, d["mean"], unit, d["p90"], unit, d["max"], unit)


def render(st: dict) -> str:
    L = ["ticket stats: %d tickets across %d homes read" % (st["tickets"], len(st["homes_read"]))]
    srcs = sorted({s for w in st["per_week"].values() for s in w})
    L += ["", "filed per week by source:", "  %-9s %s  total" % ("week", "  ".join("%-10s" % s for s in srcs))]
    for w, row in st["per_week"].items():
        L.append("  %-9s %s  %d" % (w, "  ".join("%-10d" % row.get(s, 0) for s in srcs), sum(row.values())))
    L += ["", "lead time (filed -> done): " + _s(st["lead_time_days"])]
    sl = st["slip_days"]
    L.append("slip (done minus due; positive = late): " + _s(sl))
    L.append("  %d done with a due date, %d late; %d open past due" % (
        sl["done_with_due"], sl["late"], sl["open_past_due"]))
    n = st["needs"]
    L += ["needs turnaround (%d filed, %d still proposed):" % (n["filed"], n["awaiting_accept"]),
          "  filed -> accepted: " + _s(n["accepted_days"]), "  filed -> done:     " + _s(n["done_days"])]
    if st["homes_unreachable"]:
        L += ["", "UNREACHABLE from this machine (not counted, not zero):"]
        L += ["  %-6s %-26s %s" % (u["code"], u["project"], u["reason"]) for u in st["homes_unreachable"]]
    return "\n".join(L)
