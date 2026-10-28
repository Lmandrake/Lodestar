"""Read a project's own to-do list (ACTIONS.md, else TODO.md), read-only, for Orders.

Spec: docs/superpowers/specs/2026-10-13-what-now-design.md §2 (project lists are their own
single source of truth; Lodestar never writes one) and §3 (an unowned line is NOT his by
default). Rulings 2026-10-13: BOARD.md vs ACTIONS.md is undecided, so BOARD.md is never read here.

Existing lines parse as they are, no convention is imposed:

* `- [ ]` open, `- [~]` in progress, `- [x]` done; a file with no checkbox at all is read as plain
  top-level bullets, with `~~struck~~` meaning done.
* Owner: `Owner: **Name**`, a leading `**Name:**` (or `**Name, Other:**`) prefix, or a standalone
  sentence that is only names (`Lukas.`, `Lukas + Zebulon.`, an invented name). Unowned lines are his only when a
  heading above them names him or the file's front matter says `owner: lukas`.
* Optional typed tails: `(due 2026-10-20)` / `due|by|deadline 2026-10-20 [13:00]`, `(est 15m)`,
  `{t_ab12}`. Typed fields stay typed (CLAUDE.md §7): a partial or relative date yields NO due date,
  and a date with a clock time inside a bold title is an appointment, not a deadline.

The working tree is what is read, never a committed sha, so an uncommitted `[x]` counts.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import date, datetime
from typing import Dict, List, Optional

LIST_NAMES = ("ACTIONS.md", "TODO.md")
OPEN = ("open", "progress")
ME = ("lukas", "mandrake")

_BOX = re.compile(r"^(?P<indent>\s{0,3})[-*] \[(?P<box>[ xX~])\]\s+(?P<text>.*)$")
_PLAIN = re.compile(r"^(?P<indent>)[-*] (?P<text>.*)$")
_HEAD = re.compile(r"^(#{1,6})\s+(.*)$")
_OWNER_TAG = re.compile(r"Owner:\s*\*{0,2}(?P<who>[A-Z][\w.' ]{0,40}?(?:\s*(?:,|\+|&|and)\s*"
                        r"[A-Z][\w.' ]{0,30}?)*)\*{0,2}(?=[.;,)\s]|$)")
_NAME = r"[A-Z][a-z]+(?:\s[A-Z][a-z]+)?"
_OWNER_SENTENCE = re.compile(r"(?:^|[.;:]\s+)(?P<who>%s(?:\s*(?:\+|&|,|and)\s*%s)*)\.(?=\s|$)"
                             % (_NAME, _NAME))
_DUE_TAIL = re.compile(r"\(due\s+(\d{4}-\d\d-\d\d)(?:[ T](\d{1,2}:\d\d))?\)", re.I)
_DUE_WORD = re.compile(r"\b(?:due|by|deadline|before)\s+(\d{4}-\d\d-\d\d)\b(?:[ T,]+(\d{1,2}:\d\d))?",
                       re.I)
_EST = re.compile(r"\(est\s+(\d+)\s*(m|min|mins|h|hr|hrs)\)", re.I)
_TID = re.compile(r"\{(t_[a-z0-9]{2,})\}")
_FRONT_OWNER = re.compile(r"^owner:\s*(\w+)\s*$", re.I | re.M)
_NOT_NAMES = {"Done", "Decision", "Status", "Source", "Sources", "Next", "Note", "Notes", "Owner",
              "Open", "Closed", "Blocked", "Pending", "Waiting", "Issue", "See", "Yes", "No"}


def list_path(repo: str) -> Optional[str]:
    """The project's list: ACTIONS.md if present, else TODO.md, else none."""
    for name in LIST_NAMES:
        path = os.path.join(repo, name)
        if os.path.isfile(path):
            return path
    return None


def _valid(day: str) -> str:
    try:
        return date.fromisoformat(day).isoformat()
    except ValueError:
        return ""


def _names(who: str) -> List[str]:
    parts = re.split(r"\s*(?:,|\+|&|\band\b)\s*", who.strip())
    return [p.strip() for p in parts if p.strip()]


def _is_name_list(text: str) -> bool:
    names = _names(text)
    return bool(names) and len(names) <= 4 and all(
        re.fullmatch(_NAME, n) and n.split()[0] not in _NOT_NAMES for n in names)


def _mine(names: List[str]) -> bool:
    return any(any(m in n.lower() for m in ME) for n in names)


def _owners(first: str, body: str) -> List[str]:
    tag = _OWNER_TAG.search(body)
    if tag:
        return _names(tag.group("who"))
    lead_colon = re.match(r"^\*\*(?P<who>[^*]+?):\*\*", first)
    if lead_colon and _is_name_list(lead_colon.group("who")):
        return _names(lead_colon.group("who"))
    clean = re.sub(r"~~|\*\*", "", body)
    for match in _OWNER_SENTENCE.finditer(clean):
        if _is_name_list(match.group("who")):
            return _names(match.group("who"))
    return []


def _title(first: str) -> str:
    text = re.sub(r"^~~(.*?)~~\s*$", r"\1", first.strip())
    lead_colon = re.match(r"^\*\*(?P<who>[^*]+?):\*\*\s*(?P<rest>.*)$", text)
    if lead_colon and _is_name_list(lead_colon.group("who")):
        text = lead_colon.group("rest")
    lead = re.match(r"^\*\*(?P<bold>.+?)\*\*", text)
    if lead:
        return lead.group("bold").strip().rstrip(":.").strip()
    words = re.sub(r"[*`~]", "", text).split()
    title = " ".join(words[:14])
    return title + (" …" if len(words) > 14 else "")


def _key(path_name: str, title: str, seen: Dict[str, int]) -> str:
    norm = re.sub(r"\s+", " ", title.lower()).strip()
    base = hashlib.sha256(("%s\x1f%s" % (path_name, norm)).encode("utf-8")).hexdigest()[:16]
    seen[base] = seen.get(base, 0) + 1
    return "prj_%s" % base if seen[base] == 1 else "prj_%s_%d" % (base, seen[base])


def parse(text: str, name: str = "ACTIONS.md") -> List[Dict]:
    """Every to-do entry in `text`, open and closed, with its 1-based line number.

    A file whose front matter says `actions: v1` is parsed by `actionslist.parse_v1` (D27/D29)."""
    try:
        from lib import actionslist
    except ImportError:  # pragma: no cover
        import actionslist  # type: ignore
    if actionslist.is_v1(text):
        return actionslist.parse_v1(text, name)
    lines = text.splitlines()
    default_mine = False
    if lines and lines[0].strip() == "---":
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if end:
            front = "\n".join(lines[1:end])
            owner = _FRONT_OWNER.search(front)
            default_mine = bool(owner and owner.group(1).lower() in ME)
    has_boxes = any(_BOX.match(l) for l in lines)
    heads: List[tuple] = []  # (level, text)
    items: List[Dict] = []
    seen: Dict[str, int] = {}
    i = 0
    in_code = False
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            in_code = not in_code
        if in_code:
            i += 1
            continue
        head = _HEAD.match(line)
        if head:
            level = len(head.group(1))
            heads = [h for h in heads if h[0] < level] + [(level, head.group(2))]
            i += 1
            continue
        match = _BOX.match(line) if has_boxes else _PLAIN.match(line)
        if not match:
            i += 1
            continue
        indent = len(match.group("indent"))
        first = match.group("text")
        body_lines = [first]
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if not nxt.strip() or _HEAD.match(nxt):
                break
            lead = len(nxt) - len(nxt.lstrip())
            if has_boxes and _BOX.match(nxt):
                break  # a nested checkbox is its own item, never a continuation
            if lead <= indent:
                break
            body_lines.append(nxt.strip())
            j += 1
        body = " ".join(body_lines)
        if has_boxes:
            box = match.group("box")
            state = {" ": "open", "~": "progress"}.get(box, "done")
        else:
            state = "done" if first.strip().startswith("~~") else "open"
        owners = _owners(first, body)
        section_names_him = any(_mine([h[1]]) for h in heads)
        if owners:
            mine = _mine(owners)
        else:
            mine = default_mine or section_names_him
        due, due_time = "", ""
        tail = _DUE_TAIL.search(body) or _DUE_WORD.search(body)
        if tail:
            due = _valid(tail.group(1))
            due_time = (tail.group(2) or "") if due else ""
        est = _EST.search(body)
        est_min = 0
        if est:
            est_min = int(est.group(1)) * (60 if est.group(2).lower().startswith("h") else 1)
        tid = _TID.search(body)
        title = _title(first)
        items.append({"line": i + 1, "state": state, "title": title, "owners": owners,
                      "boxed": has_boxes,
                      "mine": mine, "due": due, "due_time": due_time, "est_min": est_min,
                      "tid": tid.group(1) if tid else "", "text": body,
                      "section": heads[-1][1] if heads else "",
                      "key": _key(name, title, seen)})
        i = j
    return items


def _ticket_list(repo: str, path: Optional[str]) -> Optional[Dict]:
    """The project's tickets (D30), when it has moved to them: its ACTIONS.md says
    `tracker: tickets`, or it has no list and its ticket home holds tickets. None otherwise."""
    if path:
        with open(path, encoding="utf-8") as fh:
            head = fh.read(400)
        if not re.search(r"(?m)^tracker:\s*tickets\s*$", head.split("\n---", 1)[0]):
            return None
    try:
        from lib import tickets as tk
    except ImportError:  # pragma: no cover
        import tickets as tk  # type: ignore
    try:
        homes = tk.home_for_repo(repo)
    except tk.Refused:
        return None
    if not homes:
        return None
    items = tk.orders_items(homes)
    if not path and not items:
        return None
    mtimes = [os.path.getmtime(i["path"]) for i in items] or [0]
    mtime = datetime.fromtimestamp(max(mtimes)).astimezone().isoformat(timespec="seconds")
    return {"path": path or homes[0].dir, "mtime": mtime, "items": items, "tickets": True}


def read(repo: str) -> Dict:
    """The project's list as parsed now: `{path, mtime, items}`; `{path: None}` if it has none.
    A project on tickets (D30) returns its tickets in the same item shape, `tickets: True`."""
    path = list_path(repo)
    got = _ticket_list(repo, path)
    if got is not None:
        return got
    if not path:
        return {"path": None, "items": [], "mtime": ""}
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    mtime = datetime.fromtimestamp(os.path.getmtime(path)).astimezone().isoformat(
        timespec="seconds")
    return {"path": path, "mtime": mtime,
            "items": parse(text, "%s/%s" % (os.path.basename(repo), os.path.basename(path)))}
