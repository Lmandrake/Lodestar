"""Render a Claude Code session into a readable record in the project it concerned.

Claude Code keeps every conversation at the machine level, at
`~/.claude/projects/<mangled-cwd>/<session-uuid>.jsonl`. Nothing about that store is tied
to the project the conversation was about: not in the project directory, not in its git
history, not visible from any other machine. Measured on the Laptop 2026-10-04: 925 MB
across 782 sessions in 43 project directories, essentially all of it six weeks old.

This module renders the **dialog** — Lukas's turns, Claude's replies, and a one-line note
of each action taken — into `conversations/` inside the project itself. Lukas's ruling of
2026-10-04 set the target: *"I don't want your reasoning, I want a record of what we talk
about so I can track our work together."* So reasoning blocks, tool OUTPUT and the
re-injected hook preamble are all dropped; this is a record to read, not a replay.

Measured composition of 16 real Lodestar sessions, which is why rendering beats copying:
dialog turns are **4.6%** of transcript bytes. Re-injected hook/skill text is 39%, tool
calls plus their results 29%, reasoning 15%. A 2.01 MB session renders to 39 KB.

🔴 **THE RENDER IS A PURE FUNCTION OF THE TRANSCRIPT. NO CLOCK READ, ANYWHERE.**
A "generated at <now>" line would change the file's bytes on every sweep, so the sweep
would commit it hourly for ever and Lodestar would never pass the sweep's own quiescence
gate — the identical trap `claudestatusline.receipt_path` already records. Timestamps
*from* the transcript are stable and welcome. `tests/test_conversations.py` pins this by
rendering twice and comparing bytes, and by asserting no `time`/`datetime.now` call
reaches the render path.

**Idempotence is the whole answer to a lagging transcript.** `SessionEnd` carries no
guarantee that `transcript_path` has been flushed (the documented async-lag warning is
attached to the common field, and unlike `Stop` there is no `last_assistant_message` to
fall back on), and it does not fire at all on SIGKILL or power loss. So no writer here is
one-shot: whoever runs last and sees the most complete file wins, and re-running is always
safe. That is what lets a hook, the handoff ritual and the hourly sweep all drive the same
function without coordinating.
"""

import io
import json
import os
import re
from typing import Dict, List, NamedTuple, Optional, Tuple

try:  # keep importable both as `lib.conversations` and as a plain sibling
    from lib import gitutil, prosehome, registry, satellites
except ImportError:  # pragma: no cover - exercised only by direct-path imports
    import gitutil  # type: ignore
    import prosehome  # type: ignore
    import registry  # type: ignore
    import satellites  # type: ignore


#: Where the records go, inside the project. `handoffs/` is the existing precedent for a
#: per-repo artifact directory, and this sits beside it deliberately.
RECORD_DIR = "conversations"

#: Drive-replicated project roots. A `.git` directory inside one corrupts it, so those
#: projects get a plain file and never a repo. Nothing in the codebase encodes this —
#: registry rows for Drive projects are simply absent — so the prefix check lives here.
DRIVE_PREFIXES = ("~/GDrive", "~/My Drive")

_SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_ANSI = re.compile(r"\x1b\[[0-9;]*m|\[[0-9]+m")
_COMMAND_NAME = re.compile(r"<command-name>(.*?)</command-name>", re.S)
_COMMAND_STDOUT = re.compile(r"<local-command-stdout>.*?</local-command-stdout>", re.S)
_LOCAL_CAVEAT = re.compile(r"<local-command-caveat>.*?</local-command-caveat>", re.S)

#: Which `input` key carries the human-meaningful target of a tool call, in preference
#: order. `description` is Bash's own one-line summary and is written for exactly this.
_ACTION_HINTS = ("description", "file_path", "notebook_path", "pattern", "query", "url",
                 "command", "skill", "prompt")

#: How many action lines to print under one reply before collapsing the rest to a count.
#: A turn that fires 200 tool calls would otherwise bury the dialog it belongs to.
ACTION_CAP = 8


class Turn(NamedTuple):
    """One block of the record: who spoke, what they said, what it did."""
    role: str           # "user" or "assistant"
    text: str
    actions: List[str]


class Attribution(NamedTuple):
    """Where a transcript's record belongs, and why."""
    root: Optional[str]   # project directory to write into, None when nowhere
    kind: str             # "repo", "drive", or "unrooted"
    reason: str


def transcript_root(home: Optional[str] = None) -> str:
    """`~/.claude/projects`. Honours `CLAUDE_HOME` so tests never touch the real store."""
    claude_home = os.environ.get("CLAUDE_HOME")
    if claude_home:
        return os.path.join(os.path.expanduser(claude_home), "projects")
    base = home or os.path.expanduser("~")
    return os.path.join(base, ".claude", "projects")


def transcript_dir(project_path: str, home: Optional[str] = None) -> str:
    """The `~/.claude/projects/<mangled>` directory Claude Code uses for a project path.

    The mangling is `satellites.project_memory_name` — already public and already the one
    copy of this convention. A second implementation here is exactly how `mdtable`'s
    reader drifted from its five writers for months.
    """
    return os.path.join(transcript_root(home), satellites.project_memory_name(project_path))


def is_drive_path(path: str) -> bool:
    """True when a path lies under a Google-Drive-replicated root.

    Checked by prefix because there is no other signal: Drive projects are kept out of
    `sync/registry.md` by hand, so their absence from the registry is indistinguishable
    from a repo nobody registered.
    """
    resolved = os.path.realpath(os.path.expanduser(path))
    for prefix in DRIVE_PREFIXES:
        expanded = os.path.realpath(os.path.expanduser(prefix))
        if resolved == expanded or resolved.startswith(expanded + os.sep):
            return True
    return False


def _records(jsonl_path: str):
    """Every parseable record, in order. A truncated final line is skipped, not fatal.

    A transcript read while Claude Code is still appending to it can end mid-line, and
    that is the normal case for the `SessionEnd` hook rather than an error — the write is
    idempotent precisely so a later, complete read supersedes this one.
    """
    with io.open(jsonl_path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def cwds(jsonl_path: str) -> List[str]:
    """Distinct `cwd` values the session saw, in first-seen order.

    A session's cwd genuinely varies — both `/Lodestar` and `/Lodestar/bin` appear inside
    one measured transcript — so this returns all of them and `attribute` resolves each to
    a repo root rather than trusting the first.
    """
    seen: List[str] = []
    for record in _records(jsonl_path):
        value = record.get("cwd")
        if value and value not in seen:
            seen.append(value)
    return seen


def repo_root_of(path: str) -> Optional[str]:
    """The git work-tree root containing `path`, or None.

    `git rev-parse --show-toplevel` works from any subdirectory, which is what makes the
    varying-cwd case harmless. There is no existing helper for this — `gitutil.is_repo`
    answers a bool and `registry.by_path` only matches a declared root exactly.
    """
    if not os.path.isdir(path):
        return None
    code, out, _ = gitutil.run(path, "rev-parse", "--show-toplevel")
    if code != 0:
        return None
    root = out.strip()
    return os.path.realpath(root) if root else None


def _is_former_name(token: str) -> bool:
    """True for a bare directory name, false for a git URL.

    The registry's `Former` column deliberately mixes prior NAMES with prior REMOTE URLS
    in one cell (`('RimMaster', 'Rimworld', 'git@github.com:...')`), so a consumer that
    wants one has to reject the other. A URL carries `:` or `/`; a directory name does not.
    """
    return bool(token) and ":" not in token and "/" not in token


def former_root_of(path: str) -> Optional[str]:
    """The current root of a repo this path used to live under, via the registry's `Former`.

    🔴 **Without this, a renamed repo's entire history is silently dropped.** Measured
    2026-10-04: all 30 of RimMandrake's transcripts — 231 MB, its whole recorded history —
    carry `cwd: ~/dev/RimMaster`, the name before the rename. That directory no longer
    exists, so `repo_root_of` correctly returns None and attribution reported `unrooted`
    for every one of them. The repo was plainly the busiest on the machine and its record
    count was zero, which is the invisible-failure shape CLAUDE.md §7 keeps a tally of.

    `Former` exists for exactly this and had one consumer (`skillclone.former_instance`);
    this is the second. Resolution is by sibling path — a former name is joined to the
    row's own parent directory — so `~/dev/RimMaster/design/x` maps to `~/dev/RimMandrake`.

    Deliberately consulted only AFTER `repo_root_of` fails, so a real repo standing at the
    old path today wins over a historical rename. A registry that cannot be read yields
    None rather than raising: attribution must degrade to "unrooted", never to a crash.
    """
    target = os.path.realpath(os.path.expanduser(path))
    try:
        rows = registry.entries()
    except Exception:
        return None
    for entry in rows:
        current = os.path.realpath(os.path.expanduser(entry.path))
        parent = os.path.dirname(current)
        for token in entry.former:
            if not _is_former_name(token):
                continue
            old = os.path.join(parent, token)
            if target == old or target.startswith(old + os.sep):
                return current if os.path.isdir(current) else None
    return None


def attribute(jsonl_path: str) -> Attribution:
    """Decide which project directory a transcript's record belongs in.

    Three cases, and the third is a refusal rather than a guess:

    1. a git repo — the record goes in it, and the hourly sweep commits it;
    2. a Drive-replicated folder — a plain file, never a `git init`;
    3. neither — skipped. Roughly 210 MB of the measured store is sessions launched from
       `~/checkout`, `~/dev` or `~` itself, which are throwaway by Lukas's standing rule
       that a session declares a project or declares itself untracked.

    Deliberately derived from the records' own `cwd`, never from the directory name: the
    mangling replaces every non-alphanumeric character, so `~/dev/my-repo` and
    `~/dev/my_repo` collide, and a record keyed off the dirname would land in the wrong
    project. Reading `cwd` cannot collide.
    """
    seen = cwds(jsonl_path)
    if not seen:
        return Attribution(None, "unrooted", "transcript carries no cwd")
    for cwd in seen:
        root = repo_root_of(cwd)
        if root:
            return Attribution(root, "repo", "git root of %s" % cwd)
    for cwd in seen:
        renamed = former_root_of(cwd)
        if renamed:
            return Attribution(renamed, "repo", "%s was renamed to %s"
                               % (cwd, os.path.basename(renamed)))
    for cwd in seen:
        if is_drive_path(cwd):
            return Attribution(os.path.realpath(os.path.expanduser(cwd)), "drive",
                               "Drive-replicated folder, no git")
    return Attribution(None, "unrooted", "no git repo and not under Drive: %s" % seen[0])


def session_id(jsonl_path: str) -> str:
    """The session's own id, preferring the record field over the filename."""
    for record in _records(jsonl_path):
        value = record.get("sessionId")
        if value:
            return str(value)
    return os.path.splitext(os.path.basename(jsonl_path))[0]


def started_at(jsonl_path: str) -> Optional[str]:
    """The first `timestamp` in the transcript, verbatim. Never the clock."""
    for record in _records(jsonl_path):
        value = record.get("timestamp")
        if value:
            return str(value)
    return None


def _stamp(iso: Optional[str]) -> str:
    """`YYYY-MM-DD-HHMM` from a transcript timestamp, or a stable placeholder.

    Returns `undated` rather than today's date when the transcript carries no timestamp:
    substituting the clock here would reintroduce exactly the churn this module forbids.
    """
    if not iso:
        return "undated"
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})", iso)
    if not match:
        return "undated"
    year, month, day, hour, minute = match.groups()
    return "%s-%s-%s-%s%s" % (year, month, day, hour, minute)


def record_name(jsonl_path: str) -> str:
    """The record's filename: `YYYY-MM-DD-HHMM-<short-session-id>.md`.

    The session id is what keeps two machines from colliding, which is why Lukas chose one
    file per session over one per day.
    """
    return "%s-%s.md" % (_stamp(started_at(jsonl_path)), session_id(jsonl_path)[:8])


class Withheld(Exception):
    """This machine may not write this project's records anywhere (see `prosehome.place`)."""


def record_dir(project_root: str) -> str:
    """Where this project's records live on this machine.

    🔴 **Not always inside the project** (ruling 5, 2026-10-13). A dialog on a work Mac can
    carry private text, so a repo that can push to github.com keeps its records in the private-hosted
    overlay `<private store>/projects/<Repo>/conversations/`, never in its own tree;
    `prosehome.place` is the one rule. A Drive folder has no git remote and keeps them in place.
    Raises `Withheld` when the rule leaves nowhere safe to write: never a silent fallback.
    """
    if is_drive_path(project_root):
        return os.path.join(project_root, RECORD_DIR)
    placement = prosehome.place(project_root, RECORD_DIR)
    if placement.where == prosehome.WITHHELD:
        raise Withheld("conversation records for %s withheld: %s"
                       % (os.path.basename(project_root), placement.reason))
    return placement.path


def record_path(project_root: str, jsonl_path: str) -> str:
    return os.path.join(record_dir(project_root), record_name(jsonl_path))


def _clean(text: str) -> str:
    """Strip injected machinery that is not dialog."""
    text = _SYSTEM_REMINDER.sub("", text)
    text = _LOCAL_CAVEAT.sub("", text)
    text = _ANSI.sub("", text)
    return text.strip()


def _action_line(block: Dict) -> str:
    """One line describing a tool call: what it was, and what it was aimed at."""
    name = block.get("name") or "tool"
    payload = block.get("input")
    hint = ""
    if isinstance(payload, dict):
        for key in _ACTION_HINTS:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                hint = " ".join(value.split())
                break
    if len(hint) > 90:
        hint = hint[:87] + "..."
    return "%s — %s" % (name, hint) if hint else str(name)


def turns(jsonl_path: str) -> List[Turn]:
    """The dialog, as alternating blocks with actions attached to the reply that made them.

    What is dropped, and why each one matters:

    - `isSidechain` — a subagent's conversation, not ours;
    - `isMeta` — harness bookkeeping;
    - `tool_result` blocks — the 20% that is tool OUTPUT, which Lukas did not ask for;
    - `thinking` blocks — reasoning, explicitly excluded by his ruling;
    - `<system-reminder>` spans and hook attachments — the 39% of re-injected preamble,
      identical in every session and recoverable from the skills themselves;
    - `local-command-stdout` — the terminal's echo of a slash command, not a turn.

    A slash command collapses to `_(ran /name)_`, because the invocation is worth seeing
    and its expansion is not.

    🔴 **Consecutive assistant text merges only until a tool call intervenes.** Merging
    straight through a `tool_use` was measured on a real 2.43 MB session and collapsed one
    turn into a single block carrying **149 actions**, so `ACTION_CAP` hid 141 of them and
    the surviving 8 sat detached from the prose that motivated them. Breaking the block at
    each action run preserves the real rhythm — said, did, said — which is the record Lukas
    asked for, and it keeps the cap for the genuinely runaway case rather than the normal one.
    """
    out: List[List] = []              # mutable [role, text, actions] until the final pass
    current: Optional[List] = None
    acted = False                     # has the open assistant block already run a tool?
    cards: Dict[str, list] = {}       # AskUserQuestion tool_use id -> its questions

    def open_block(role: str, text: str) -> List:
        block = [role, text, []]
        out.append(block)
        return block

    for record in _records(jsonl_path):
        if record.get("isSidechain") or record.get("isMeta"):
            continue
        # A transport failure is not something either of us said. Measured leaking into a
        # real record as "API Error: Connection lost mid-response" under a `## Claude`.
        if record.get("isApiErrorMessage"):
            continue
        kind = record.get("type")
        message = record.get("message") or {}
        content = message.get("content")

        if kind == "attachment":
            # A message Lukas typed while the agent was mid-turn.
            att = record.get("attachment")
            if isinstance(att, dict) and att.get("type") == "queued_command":
                prompt = att.get("prompt")
                if isinstance(prompt, list):
                    prompt = "\n".join(b.get("text", "") for b in prompt if isinstance(b, dict))
                text = _clean(prompt or "")
                if text and not text.startswith("<task-notification>"):
                    current = open_block("user", text)
                    acted = False
            continue

        if kind == "user":
            if isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                # Tool output is dropped, except his answers to an AskUserQuestion card.
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result" \
                            and b.get("tool_use_id") in cards:
                        qs = "\n".join("- " + str(q.get("question", ""))[:400]
                                       for q in cards.pop(b["tool_use_id"]) if isinstance(q, dict))
                        res = b.get("content")
                        if isinstance(res, list):
                            res = "\n".join(x.get("text", "") for x in res if isinstance(x, dict))
                        current = open_block("user", "_(card answer)_\n%s\n%s" % (qs, str(res or "").strip()))
                        acted = False
                continue
            if isinstance(content, str):
                raw = content
            elif isinstance(content, list):
                raw = "".join(b.get("text", "") for b in content
                              if isinstance(b, dict) and b.get("type") == "text")
            else:
                continue
            if _COMMAND_STDOUT.search(raw or ""):
                continue
            named = _COMMAND_NAME.search(raw or "")
            text = "_(ran %s)_" % named.group(1).strip() if named else _clean(raw or "")
            if text:
                current = open_block("user", text)
                acted = False

        elif kind == "assistant" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and (block.get("text") or "").strip():
                    cleaned = _clean(block["text"])
                    if not cleaned:
                        continue
                    if current is not None and current[0] == "assistant" and not acted:
                        current[1] = (current[1] + "\n\n" + cleaned).strip()
                    else:
                        current = open_block("assistant", cleaned)
                        acted = False
                elif block.get("type") == "tool_use":
                    if block.get("name") == "AskUserQuestion":
                        cards[block.get("id")] = (block.get("input") or {}).get("questions") or []
                    if current is None or current[0] != "assistant":
                        current = open_block("assistant", "")
                    current[2].append(_action_line(block))
                    acted = True

    return [Turn(role, text, actions) for role, text, actions in out]


def render(jsonl_path: str, speaker: str = "Lukas") -> str:
    """The record, as markdown. Pure function of the transcript — no clock, no environment.

    The header carries only facts the transcript itself supplies, so re-rendering an
    unchanged transcript reproduces identical bytes.
    """
    blocks = turns(jsonl_path)
    sid = session_id(jsonl_path)
    begun = started_at(jsonl_path)

    head = ["# Conversation — %s" % (begun[:10] if begun else "undated"),
            "",
            "- Session: `%s`" % sid]
    if begun:
        head.append("- Began: %s" % begun)
    branches = []
    for record in _records(jsonl_path):
        branch = record.get("gitBranch")
        if branch and branch not in branches:
            branches.append(branch)
    if branches:
        head.append("- Branch: %s" % ", ".join("`%s`" % b for b in branches))
    said = sum(1 for b in blocks if b.role == "user")
    acted = sum(len(b.actions) for b in blocks)
    head.append("- %d turn(s) from %s, %d reply block(s), %d action(s)"
                % (said, speaker, sum(1 for b in blocks if b.role == "assistant"), acted))
    head.append("")
    head.append("Rendered from the session transcript by Lodestar: the dialog and the "
                "actions taken, without tool output or reasoning.")

    body: List[str] = []
    for block in blocks:
        body.append("")
        body.append("## %s" % (speaker if block.role == "user" else "Claude"))
        if block.text:
            body.append("")
            body.append(block.text)
        if block.actions:
            body.append("")
            shown = block.actions[:ACTION_CAP]
            body.extend("- `%s`" % line for line in shown)
            if len(block.actions) > ACTION_CAP:
                body.append("- _...and %d more action(s)_"
                            % (len(block.actions) - ACTION_CAP))

    return "\n".join(head + body).rstrip() + "\n"


def write_record(project_root: str, jsonl_path: str, speaker: str = "Lukas",
                 dry_run: bool = False) -> Tuple[str, bool]:
    """Write the record. Returns `(path, changed)`; writes nothing when already current.

    Comparing before writing is not an optimisation, it is the no-churn invariant: the
    sweep re-derives every record it can see, so an unconditional write would dirty every
    file on every pass and commit them hourly for ever.
    """
    target = record_path(project_root, jsonl_path)
    text = render(jsonl_path, speaker=speaker)
    try:
        with io.open(target, "r", encoding="utf-8") as handle:
            if handle.read() == text:
                return target, False
    except (OSError, UnicodeDecodeError):
        pass
    if dry_run:
        return target, True
    directory = os.path.dirname(target)
    if is_drive_path(project_root):
        os.makedirs(directory, exist_ok=True)
    else:
        prosehome.ensure_dir(prosehome.place(project_root, RECORD_DIR))
    with io.open(target, "w", encoding="utf-8") as handle:
        handle.write(text)
    return target, True


def transcripts_for(project_root: str, home: Optional[str] = None) -> List[str]:
    """Every top-level transcript in this project's Claude Code directory, oldest first.

    `subagents/` is deliberately not walked: a delegated agent's conversation is work, not
    dialog, and the largest single one measured is 8.8 MB.
    """
    directory = transcript_dir(project_root, home)
    if not os.path.isdir(directory):
        return []
    found = [os.path.join(directory, name) for name in os.listdir(directory)
             if name.endswith(".jsonl")]
    return sorted(found, key=lambda p: (os.path.getmtime(p), p))


def sessions_for(project_root: str, home: Optional[str] = None) -> List[str]:
    """Transcripts that really belong to this project, confirmed by their own `cwd`.

    Filters `transcripts_for` through `attribute`, which is what makes the mangling
    collision harmless rather than silently cross-filing two projects' records.
    """
    root = os.path.realpath(project_root)
    kept = []
    for path in transcripts_for(project_root, home):
        where = attribute(path)
        if where.root and os.path.realpath(where.root) == root:
            kept.append(path)
    return kept
