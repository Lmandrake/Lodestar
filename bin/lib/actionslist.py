"""ACTIONS v1: parse, lint, allocate ids, migrate v0 lists, render BOARD.md. Deterministic Python.

Spec: docs/superpowers/specs/2026-10-13-project-actions-design.md §4-§6 (branch
`project-actions-design`), ruled by D27 (2026-10-14) and D29 (2026-10-15, every project one
ACTIONS.md). `todolists.parse` hands a file to `parse_v1` when its front matter says `actions: v1`;
a file without that is v0 and keeps parsing exactly as before.

v1 line grammar (one top-level list line; indented continuation lines belong to it):

    - [ ] **Title.** prose, sources, NEXT: one action. (owner lukas) (due 2026-11-02) {a_7f3k2q}

Markers `[ ]` open, `[~]` in progress, `[x]` done, `[-]` dropped, `[?]` unverified, `[>]` pointer.
Tails (all optional, in parentheses): owner, due, est, kind, group, done. `(done legacy)` marks a
line that was already done when its list was migrated to v1 and carries no recorded date or
evidence: the honest value, never a guessed date (CLAUDE.md §7: typed fields stay typed).

Owner resolution in v1, first hit wins: the `(owner X)` tail; a `<!-- owner: X -->` line under the
section heading; the front matter `default_owner`; then the v0 inference (Owner: tags, a bold name
prefix, a heading that names him). The v0 fallback is deliberate: a migrated line keeps the exact
mine/not-mine reading Orders had before, even where migration could not write a tail.

Everything from `## Reference` to the end of the file is never an item.

Orders' item key is unchanged from v0 (a hash of list name and title, `todolists._key`), and the
title is computed with the v1-only tails stripped, so migrating a list does not re-key its items
and every snooze or override Orders holds still binds.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import subprocess
from datetime import date
from typing import Dict, Iterable, List, Optional, Set, Tuple

try:
    from lib import todolists as tl
except ImportError:  # pragma: no cover
    import todolists as tl  # type: ignore

RENDERER = "actions-board/1"
MARKERS = {" ": "open", "~": "progress", "x": "done", "-": "dropped", "?": "unverified",
           ">": "pointer"}
EVIDENCE = ("said", "mail", "calendar", "commit", "issuer", "legacy")
KINDS = ("decision", "deliver", "reply", "wait", "offer")
ID_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"

_BOX = re.compile(r"^(?P<indent>\s{0,3})[-*] \[(?P<box>[ xX~?>-])\]\s+(?P<text>.*)$")
_ANYBOX = re.compile(r"^\s{0,3}[-*] \[(?P<box>.)\]\s")
_HEAD = re.compile(r"^(#{1,6})\s+(.*)$")
_REFERENCE = re.compile(r"^##\s+Reference\s*$")
_SECTION_OWNER = re.compile(r"^<!--\s*owner:\s*([\w:.-]+)\s*-->\s*$")
_ID = re.compile(r"\{(a_[a-z0-9]{6})\}")
_ANY_ID = re.compile(r"\{a_[a-z0-9]{6}\}")
_OWNER_T = re.compile(r"\(owner\s+([\w:.+-]+)\)")
_KIND_T = re.compile(r"\(kind\s+([\w-]+)\)")
_GROUP_T = re.compile(r"\(group\s+([\w-]+)\)")
_DONE_T = re.compile(r"\(done\s+(?:(\d{4}-\d\d-\d\d)\s+)?([a-z]+)\)")
_POINTER = re.compile(r"→\s*(ob_[a-z0-9]+)")
_CONFLICT = re.compile(r"^(<{7}|={7}|>{7})(\s|$)", re.M)
# Only tails v0 never had are stripped from the title, so v0 and v1 titles of one line agree.
_V1_ONLY = re.compile(r"\s*(?:\((?:owner|kind|group)\s+[^)]*\)|\(done\s+[^)]*\)|\{a_[a-z0-9]{6}\})")


def front_matter(text: str) -> Tuple[Dict[str, str], int]:
    """(keys, index of the line after the closing ---); ({}, 0) when there is none."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, 0
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            keys = {}
            for raw in lines[1:i]:
                m = re.match(r"^([\w-]+):\s*(.*?)\s*(?:#.*)?$", raw)
                if m:
                    keys[m.group(1).lower()] = m.group(2)
            return keys, i + 1
    return {}, 0


def is_v1(text: str) -> bool:
    return front_matter(text)[0].get("actions", "").lower() == "v1"


def _strip_v1(first: str) -> str:
    return _V1_ONLY.sub("", first).rstrip()


def parse_v1(text: str, name: str = "ACTIONS.md") -> List[Dict]:
    """Every v1 item, in the same dict shape `todolists.parse` returns, plus the typed v1 fields."""
    fm, start = front_matter(text)
    default_owner = fm.get("default_owner", "").lower()
    lines = text.splitlines()
    items: List[Dict] = []
    seen: Dict[str, int] = {}
    heads: List[tuple] = []
    section_owner = ""
    in_code = False
    i = start
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            in_code = not in_code
        if in_code:
            i += 1
            continue
        if _REFERENCE.match(line):
            break
        head = _HEAD.match(line)
        if head:
            level = len(head.group(1))
            heads = [h for h in heads if h[0] < level] + [(level, head.group(2))]
            section_owner = ""
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines):
                so = _SECTION_OWNER.match(lines[j].strip())
                if so:
                    section_owner = so.group(1).lower()
            i += 1
            continue
        match = _BOX.match(line)
        if not match:
            i += 1
            continue
        indent = len(match.group("indent"))
        first = match.group("text")
        body_lines = [first]
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if not nxt.strip() or _HEAD.match(nxt) or _BOX.match(nxt):
                break
            if len(nxt) - len(nxt.lstrip()) <= indent:
                break
            body_lines.append(nxt.strip())
            j += 1
        body = " ".join(body_lines)
        box = match.group("box").lower()
        state = MARKERS[box]
        owner_t = _OWNER_T.search(first)
        owner = (owner_t.group(1).lower() if owner_t else "") or section_owner or default_owner
        v0_first = _strip_v1(first)
        if owner:
            owners = [owner]
            mine = owner in tl.ME
        else:
            owners = tl._owners(v0_first, _strip_v1(body))
            if owners:
                mine = tl._mine(owners)
            else:
                mine = any(tl._mine([h[1]]) for h in heads)
        due, due_time = "", ""
        tail = tl._DUE_TAIL.search(body) or tl._DUE_WORD.search(body)
        if tail:
            due = tl._valid(tail.group(1))
            due_time = (tail.group(2) or "") if due else ""
        est = tl._EST.search(body)
        est_min = 0
        if est:
            est_min = int(est.group(1)) * (60 if est.group(2).lower().startswith("h") else 1)
        aid = _ID.search(first)
        done = _DONE_T.search(first)
        kind = _KIND_T.search(first)
        group = _GROUP_T.search(first)
        pointer = _POINTER.search(first) if state == "pointer" else None
        title = tl._title(v0_first)
        items.append({
            "line": i + 1, "state": state, "title": title, "owners": owners, "boxed": True,
            "mine": mine, "due": due, "due_time": due_time, "est_min": est_min,
            "tid": aid.group(1) if aid else "", "text": body,
            "section": heads[-1][1] if heads else "", "key": tl._key(name, title, seen),
            "version": 1, "id": aid.group(1) if aid else "", "owner": owner,
            "done_date": (done.group(1) or "") if done else "",
            "evidence": done.group(2) if done else "",
            "kind": kind.group(1) if kind else "", "group": group.group(1) if group else "",
            "pointer": pointer.group(1) if pointer else "", "indent": indent,
            "has_next": "NEXT:" in body,
        })
        i = j
    return items


# ---------------------------------------------------------------- ids


def ids_in(text: str) -> Set[str]:
    return set(m.group(0)[1:-1] for m in _ANY_ID.finditer(text))


def ids_ever(repo: str, names: Iterable[str] = ("ACTIONS.md", "TODO.md")) -> Set[str]:
    """Every id any list file in this repo has ever held, from its full git history."""
    out: Set[str] = set()
    for name in names:
        try:
            log = subprocess.run(["git", "-C", repo, "log", "--all", "-p", "--follow", "--", name],
                                 capture_output=True, text=True, timeout=60).stdout
        except (OSError, subprocess.TimeoutExpired):
            log = ""
        out |= ids_in(log)
    return out


def new_id(taken: Set[str]) -> str:
    while True:
        aid = "a_" + "".join(secrets.choice(ID_ALPHABET) for _ in range(6))
        if aid not in taken:
            taken.add(aid)
            return aid


# ---------------------------------------------------------------- check


def check(text: str, other_lists: Iterable[str] = ()) -> List[Tuple[str, int, str]]:
    """(kind, line, message) for every fault; kinds: conflict, lint, possible-task-list, info."""
    out: List[Tuple[str, int, str]] = []
    if _CONFLICT.search(text):
        line = text[:_CONFLICT.search(text).start()].count("\n") + 1
        return [("conflict", line, "conflict markers: refusing to parse or render")]
    if not is_v1(text):
        out.append(("lint", 1, "no `actions: v1` front matter (read as v0)"))
        return out
    lines = text.splitlines()
    in_ref = False
    for n, raw in enumerate(lines, 1):
        if _REFERENCE.match(raw):
            in_ref = True
        m = _ANYBOX.match(raw)
        if m and not in_ref and m.group("box").lower() not in MARKERS:
            out.append(("lint", n, "unknown marker [%s]" % m.group("box")))
    counts: Dict[str, List[int]] = {}
    for it in parse_v1(text):
        if it["id"]:
            counts.setdefault(it["id"], []).append(it["line"])
        else:
            out.append(("lint", it["line"], "item has no {a_xxxxxx} id"))
        if it["state"] == "done" and not it["evidence"]:
            out.append(("lint", it["line"], "[x] without (done YYYY-MM-DD <evidence>)"))
        if it["evidence"] and it["evidence"] not in EVIDENCE:
            out.append(("lint", it["line"], "unknown evidence class %r" % it["evidence"]))
        if it["evidence"] and it["evidence"] != "legacy" and not it["done_date"]:
            out.append(("lint", it["line"], "done tail needs a date unless it is `legacy`"))
        if it["state"] == "unverified" and (not it["owner"] or not it["has_next"]):
            out.append(("lint", it["line"], "[?] needs (owner X) and a NEXT:"))
        if it["state"] == "pointer" and not it["pointer"]:
            out.append(("lint", it["line"], "[>] needs → ob_… (issue pointers wait for the adapter)"))
        if it["kind"] and it["kind"] not in KINDS:
            out.append(("lint", it["line"], "unknown kind %r" % it["kind"]))
    for aid, where in counts.items():
        if len(where) > 1:
            out.append(("lint", where[1], "duplicate id %s (lines %s)" % (aid, where)))
    for path in other_lists:
        out.append(("possible-task-list", 0, path))
    return out


def other_task_lists(repo: str) -> List[str]:
    """Tracked markdown files outside the list itself with live `- [ ]` lines (advisory only)."""
    try:
        got = subprocess.run(["git", "-C", repo, "grep", "-lE", r"^\s{0,3}[-*] \[ \]", "--", "*.md"],
                             capture_output=True, text=True, timeout=60).stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        return []
    skip = ("ACTIONS.md", "BOARD.md")
    return [p for p in got if p not in skip and not p.split("/")[0] in
            ("conversations", "handoffs", "bearing", "archive")]


# ---------------------------------------------------------------- migrate v0 -> v1


def _handle(owners: List[str]) -> str:
    if tl._mine(owners):
        return "lukas"
    first = owners[0].split()[0].lower()
    return re.sub(r"[^\w.-]", "", first)


def _semantics(items: List[Dict]) -> List[Tuple]:
    def cls(s):
        return "open" if s in tl.OPEN else s if s == "progress" else "closed"
    return [(cls(e["state"]), e["state"] == "progress", e["mine"], e["due"], e["due_time"],
             e["est_min"], e["title"], e["key"]) for e in items]


def _reference_last(text: str) -> str:
    """Move a mid-file `## Reference` block to the end: in v1 everything after it is not items."""
    lines = text.split("\n")
    at = next((i for i, l in enumerate(lines) if _REFERENCE.match(l)), None)
    if at is None:
        return text
    end = next((i for i in range(at + 1, len(lines)) if lines[i].startswith("## ")), None)
    if end is None:
        return text
    block = lines[at:end]
    rest = lines[:at] + lines[end:]
    while rest and not rest[-1].strip():
        rest.pop()
    while block and not block[-1].strip():
        block.pop()
    return "\n".join(rest + [""] + block) + "\n"


def migrate(text: str, name: str, taken: Set[str], extra_front: Optional[Dict[str, str]] = None
            ) -> Tuple[str, Dict]:
    """A v0 list rewritten as v1, plus a report. Raises ValueError if any item's meaning moved.

    Per item: the marker is normalised (`[X]`→`[x]`; a plain bullet → `[ ]`, a `~~struck~~`
    bullet → `[-]`), and the first line gains `(owner X)` when v0 inferred an owner, `(done
    legacy)` on a done line, and a fresh id. Nothing else in the file changes.
    """
    if is_v1(text):
        raise ValueError("already v1")
    if _CONFLICT.search(text):
        raise ValueError("conflict markers present")
    text = _reference_last(text)
    before = tl.parse(text, name)
    lines = text.split("\n")
    fm, body_start = front_matter(text)
    for e in before:
        idx = e["line"] - 1
        raw = lines[idx]
        if e["boxed"]:
            m = re.match(r"^(\s{0,3}[-*] )\[([ xX~])\](\s+)(.*)$", raw)
            pre, box, sp, rest = m.group(1), m.group(2), m.group(3), m.group(4)
            box = "x" if box in "xX" else box
        else:
            m = re.match(r"^([-*] )(.*)$", raw)
            pre, sp, rest = m.group(1), " ", m.group(2)
            if e["state"] == "done":
                box = "-"  # the ~~struck~~ text stays as written, so its title is unchanged
            else:
                box = " "
        rest = rest.rstrip()
        tails = []
        if e["owners"] and not _OWNER_T.search(rest):
            tails.append("(owner %s)" % _handle(e["owners"]))
        elif not e["owners"] and e["mine"] and not _OWNER_T.search(rest):
            tails.append("(owner lukas)")
        if box == "x" and not _DONE_T.search(rest):
            tails.append("(done legacy)")
        aid = _ID.search(rest)
        if not aid:
            tails.append("{%s}" % new_id(taken))
        lines[idx] = "%s[%s]%s%s%s" % (pre, box, sp, rest, (" " + " ".join(tails)) if tails else "")
    keys = dict(fm)
    keys.pop("owner", None)
    head = ["---", "actions: v1", "tracker: %s" % (extra_front or {}).get("tracker", "actions"),
            "board: %s" % (extra_front or {}).get("board", "none")]
    if fm.get("owner", "").lower() in tl.ME:
        head.append("default_owner: lukas")
    for k, v in keys.items():
        if k not in ("actions", "tracker", "board", "default_owner"):
            head.append("%s: %s" % (k, v))
    for k, v in (extra_front or {}).items():
        if k not in ("tracker", "board"):
            head.append("%s: %s" % (k, v))
    head.append("---")
    rest_lines = lines[body_start:] if body_start else lines
    new = "\n".join(head + ([""] if rest_lines and rest_lines[0].strip() else []) + rest_lines)
    after = parse_v1(new, name)
    sb, sa = _semantics(before), _semantics(after)
    if len(sb) != len(sa):
        raise ValueError("migration changed the item count %d -> %d" % (len(sb), len(sa)))
    if sb != sa:
        diffs = [(a, b) for a, b in zip(sb, sa) if a != b][:3]
        raise ValueError("migration changed %d item(s) (n %d -> %d): %r" % (
            sum(1 for a, b in zip(sb, sa) if a != b), len(sb), len(sa), diffs))
    report = {"items": len(after), "open_mine": sum(1 for e in after if e["state"] in tl.OPEN
                                                    and e["mine"]),
              "ids_added": sum(1 for e in before if not _ID.search(e["text"])),
              "legacy_done": sum(1 for e in after if e["evidence"] == "legacy")}
    return new, report


def skeleton(project: str, today: Optional[str] = None) -> str:
    """An empty v1 list for a project that has none (D29: every project has one)."""
    today = today or date.today().isoformat()
    return ("---\nactions: v1\ntracker: actions\nboard: none\ndefault_owner: lukas\n---\n\n"
            "# %s — Actions\n\n"
            "Lodestar's per-project to-do list, ACTIONS v1 (Lodestar `docs/DECISIONS.md` D27, D29).\n"
            "One line per item: `- [ ] **Title.** prose, NEXT: one action. (due YYYY-MM-DD) "
            "{a_xxxxxx}`.\n"
            "Allocate ids with `python3 ~/dev/Lodestar/bin/actions.py id`; check with "
            "`actions.py check`.\n"
            "Mail-born obligations live in the Reckoning, never here (D29 T3).\n\n"
            "## Open\n\n## Reference\n\nCreated %s by the house organization.\n"
            % (project, today))


# ---------------------------------------------------------------- board


def render_board(text: str, today: Optional[str] = None) -> str:
    """BOARD.md from one read of ACTIONS.md (spec §5). Every open item appears at least once."""
    today = today or date.today().isoformat()
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    fm, start = front_matter(text)
    lines = text.splitlines()
    pre = []
    for raw in lines[start:]:
        if raw.startswith("## "):
            break
        pre.append(raw)
    items = parse_v1(text)
    live = [e for e in items if e["state"] in ("open", "progress")]

    def card(e):
        state = {"open": "open", "progress": "in progress"}.get(e["state"], e["state"])
        return "| %s | %s | %s | %s | [L%d](ACTIONS.md#L%d) |" % (
            e["title"].replace("|", "\\|"), state, e["owner"] or ("lukas" if e["mine"] else "—"),
            e["due"] or "", e["line"], e["line"])

    def table(rows):
        return ["| Item | State | Owner | Due | Line |", "|---|---|---|---|---|"] + rows

    out = ["<!-- generated by actions.py %s from ACTIONS.md sha256:%s; do not edit -->"
           % (RENDERER, digest)]
    out += pre
    shown: Set[int] = set()

    def section(title, rows_items):
        if not rows_items:
            return
        out.extend(["", "## " + title, ""] + table([card(e) for e in rows_items]))
        shown.update(e["line"] for e in rows_items)

    section("Decisions for Lukas", [e for e in live if e["kind"] == "decision"])
    for g in sorted(set(e["group"] for e in live if e["group"])):
        section(g, [e for e in live if e["group"] == g])
    section("Dated", sorted([e for e in live if e["due"]], key=lambda e: e["due"]))
    section("Open, other", [e for e in live if e["line"] not in shown
                            and not e["owner"].startswith("them:")])
    section("Theirs", [e for e in live if e["owner"].startswith("them:")])
    unv = [e for e in items if e["state"] == "unverified"]
    section("Unverified", unv)
    recent = [e for e in items if e["state"] == "done" and e["done_date"]
              and (date.fromisoformat(today) - date.fromisoformat(e["done_date"])).days <= 14]
    section("Done, last 14 days", sorted(recent, key=lambda e: e["done_date"], reverse=True))
    return "\n".join(out).rstrip() + "\n"


def list_file(repo: str) -> Optional[str]:
    return tl.list_path(repo)


def read_text(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def count(text: str, name: str = "ACTIONS.md") -> Dict[str, int]:
    items = tl.parse(text, name)
    return {"items": len(items),
            "open_mine": sum(1 for e in items if e["state"] in tl.OPEN and e["mine"]),
            "open": sum(1 for e in items if e["state"] in tl.OPEN)}


__all__ = ["parse_v1", "is_v1", "check", "migrate", "render_board", "skeleton", "ids_ever",
           "new_id", "other_task_lists", "count", "front_matter"]
