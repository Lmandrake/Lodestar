---
name: handoff
description: Run the agent handoff ritual — capture the judgment that dies when a session's context ends, and consume the predecessor's on wake. Use when the user says "prepare for agent reboot", "prepare for shutdown", "prep for reboot", "we're rebooting the agent", "prepare handoff", or "wake"; when you are about to say "that's all I have for now" (reporting idleness IS the handoff moment); on the first turn of a fresh session in a repo that has handoffs; and whenever a long session is ending because context is filling, a wave of work has closed, or the machine is being restarted. Also use when asked how handoffs work, why a handoff was refused, to harvest stranded lessons into a durable doc, or to cull old handoff files.
---

# The handoff ritual

**When a long agent session ends, the work that dies is never the code — that is committed. It
is the JUDGMENT: what is half-done and exactly where it stops, what the owner has not yet
seen, which instrument lied this week.** This ritual makes capturing that cheap and makes
*consuming* it enforced.

The tool is `~/dev/Lodestar/bin/handoff.py` — portable, stdlib-only, and it resolves the
current repo from the working directory (`git rev-parse --show-toplevel`). One installed copy
serves every project; nothing is copied per repo. Full reference:
`~/dev/Lodestar/docs/HANDOFF.md`.

```sh
python3 ~/dev/Lodestar/bin/handoff.py            # write the skeleton (gates first)
python3 ~/dev/Lodestar/bin/handoff.py --check    # exit 0 before you may say HANDOFF READY
python3 ~/dev/Lodestar/bin/handoff.py --wake     # the READ side, first turn of a session
python3 ~/dev/Lodestar/bin/handoff.py --harvest  # stranded lessons -> one file to file + delete
python3 ~/dev/Lodestar/bin/handoff.py --cull     # superseded 7d+ and lessons filed; --apply deletes
```

Zero config works: handoffs land in `handoffs/`, lessons in `LESSONS.md`, identity `SOLO`.
With several agent identities in the corpus and no identity source it **refuses rather than
guessing** — a wrong guess files one agent's reboot under another's name and hands it their
unfinished work. Per-repo overrides go in `.handoff.json`; unknown keys are refused, not
ignored.

---

## Why the shape is what it is — measured, not taste

A three-arm audit of 67 real handoffs (2026-09-17) found that **writing handoffs was
disciplined, reading them was optional, and everything optional died.**

| Finding | Number |
|---|---|
| Pointers ("here's what's half-done") the successor actually picked up | **25%** |
| Pickup when the pointer carried a concrete imperative `NEXT:` action | **near-100%** |
| Pickup for prose warnings with no next action | **~0%** |
| Handoffs shipping an *unattributed* dirty-tree dump the checker could not see | **7 of 11** |
| Lesson-claims stranded in handoffs no durable doc ever received | **43 of 171** |
| Mechanical bulk (git log/status dumps) per handoff | **~52%**, one file 844 lines |

So: a read side that obliges a response to every pointer, a checker that refuses the shapes
measured not to work, and a harvest that turns the lessons drain into a read instead of an
excavation. **A handoff ritual with no enforced read side converges on write-only theater.**

---

## The write side — when a session is ending

Trigger on either: the user asks for it in any wording, **or** you are about to say *"that's
all I have for now"*. Reporting idleness **is** the handoff moment; do not wait to be asked.

Do it at a real stopping point — subagents all reported, everything committed and pushed,
nothing mid-edit. The tool gates on this and refuses if the session is not safe to leave.

1. **Run the tool.** It writes everything a script can know — the commits (capped at 20 lines;
   git is the provenance), what is uncommitted, window state — and leaves four sections marked
   `<<< WRITE THIS >>>`.
2. **Fill the four judgment sections.** This is the whole point: a script can list what
   changed; it cannot say which finding the owner needs or which trap cost two hours.
   - **The one thing to carry forward** — the single thing that would cost the next session
     hours to rediscover. Not a list. "Nothing this wave" is a legitimate answer, meant.
   - **What the owner should see** — findings needing a human eye or veto: a number nobody
     ruled on, something shipped deliberately with a flag raised. Empty is legitimate.
   - **What is half-done, and where it stops** — one bullet each, and the shape is enforced:
     `` - `ID_OR_TOPIC` — state; NEXT: <one imperative action> ``. A pointer with no `NEXT:`
     measured ~0% pickup. Anything left mid-flight with no line here is a trap.
   - **Traps learned** — instruments that lied, silent failures, commands that ate their own
     input. **ONE line each**, ending `(filed: LESSONS)` or `(see: <doc>)`. File the trap to
     the durable doc the moment it is learned, then *cite* it. Never re-explain a recorded
     trap — that duplication is how two copies drift.
3. **Update the Lodestar board.** Refresh `loops/<loop-id>.md` with where the work stands and
   append a `handoff` event to `journal/<date>/`. The board is what a session on the *other
   machine* or in Cowork can read; the handoff file is the detail behind it.
4. **Write the durable lessons into the project's own `CLAUDE.md`.** That is what the next
   agent in this repo reads before touching anything. Distil only what a fresh agent would
   otherwise get wrong — a non-obvious invariant, a trap already fallen into, a belief
   verified false. **Merge into the existing sections**, never append a parallel log. If the
   project has no repo (Drive-based), the lessons go in the loop file instead. Lessons about
   *how the user wants to work* are a different thing and belong in auto-memory.
5. **Sweep stale background agents.** Finished subagents can stay listed RUNNING in the task
   panel for hours and survive `/clear`. `TaskStop list-running` (bogus id; the error lists
   running ids), then `python3 ~/dev/Lodestar/bin/stale_agents.py <id>...`, then `TaskStop` each
   FINISHED id. Never stop a LIVE one; name it in the handoff.
6. **`--check`, then commit and push everything.**
7. **Say `HANDOFF READY` as your last line**, with the local time read from `date` — not
   guessed, not the session's start time. Under it, a short list of what was written and
   where. Then start nothing new.

### Drive-based projects: the tool refuses, and that is correct

A project under `~/GDrive` or `~/My Drive` has no git repo, because Drive replicates `.git`
and corrupts it. So `handoff.py` in every form answers:

```
REFUSED: not inside a git repository. The handoff ritual is built on git provenance.
```

**Note it exits 0.** A refusal that looks like success to `&&` or to a glanced-at exit code,
so read the words, and never report `--check` as passed when it never ran. Say in the handoff
that there was no checker and that you verified by hand instead.

What to do instead, measured on a Drive-only study folder 2026-10-06:

1. **The loop file IS the handoff.** Write the four judgment sections into
   `loops/<loop-id>.md` directly, including the pointers in `` `ID` — state; NEXT: <action> ``
   form, and append a `handoff` event to the journal. `touch.py` has no `handoff` in its
   `--event` choices even though the journal accepts one, so append it directly:
   ```python
   import sys; sys.path.insert(0, "bin")
   from lib import journal
   journal.append("<loop-id>", "handoff", note="...")
   ```
   Then `bin/board.py --sync` and commit the named board paths.
2. **`touch.py --body` destroys the existing body.** For anything longer than a line, edit
   `loops/<loop-id>.md` and then `--sync`. This is already in Lodestar's own CLAUDE.md; it is
   repeated here because a Drive handoff is *all* body, so it is the one case where the whole
   record is at risk.
3. **The conversation record needs `--repo`, and nothing will commit it.**
   ```sh
   python3 ~/dev/Lodestar/bin/conversations.py --repo "/path/to/Drive/project"
   ```
   It writes into that folder's `conversations/`. Its closing line says the hourly sweep
   commits overlay and send-on records; that is **false here** — there is no repo for a sweep to commit
   to. Drive replication is what makes it durable, so confirm the file exists on disk and do
   not wait for a commit that cannot happen.

**Attach the loop at the START of a session in a Drive folder, not at handoff time.** The
global ritual's step 0 asks which loop this is, and a folder with no repo gives you no
reminder. One such folder had been worked for months before anyone created a loop file for
it — so months of judgment had no home and is simply gone.

### The non-negotiables

- **The script never says `HANDOFF READY`. Only you do**, after `--check` passes and you judge
  the session genuinely closed. A script that could emit the reboot signal would eventually
  emit it wrongly.
- **Never say it before everything is pushed.** If a step fails — a tree that won't commit, no
  loop attached, a repo outside `~/dev` — say **`HANDOFF INCOMPLETE`** instead and name
  exactly what is unsaved. The screen is not storage.
- **Say it once.** With nothing committed since the last handoff the tool reports `ALREADY
  HANDED OFF` and writes nothing. A signal repeated on every idle turn is not a signal.
- **Attribute every uncommitted file.** A `<<< WHOSE? >>>` marker must be replaced with whose
  work it is; `--check` refuses while any survives.

---

## The read side — the first turn of a session

**This is the half that was missing, and the reason 60% of pointers died.**

```sh
python3 ~/dev/Lodestar/bin/handoff.py --wake
```

🔴 **If it opens with `OWNER NEEDED: N draft(s) waiting on Lukas's word over 12 h`, put each draft
to him in your first reply (send / edit / drop) before anything else** (a draft left unsaid
gets carried from handoff to handoff instead of reaching him). On the write side, **a draft awaiting his word
is never a half-done pointer**: name it in "What the owner should see", make sure it is a real
Gmail/Outlook draft or hold-queue row (the DRAFTS ledger lists only those), and say it to him.
`--check` refuses a pointer that carries one, or a named draft the ledger does not list.

It prints the predecessor's handoff with each pointer's live state beside it, so you compare
against reality rather than trusting a snapshot. Then, **for every open pointer, out loud in
your first reply: pick it up, close it, or say why not.** Silence is how they died.

If it opens with a `!!!` block, a session after that handoff ended without one: read the record
it names before trusting the handoff, and say so in your first reply. A status-line "hand off soon"
(60% context) or "HAND OFF NOW" (85%) means start this ritual.

If it prints "nothing to wake from", there is no handoff in this repo yet — say nothing and
carry on.

Then sweep stale background agents (a finished one can sit listed RUNNING across `/clear`):
`TaskStop list-running`, `python3 ~/dev/Lodestar/bin/stale_agents.py <id>...`, `TaskStop` each
FINISHED id, never a LIVE one. `--wake` prints this reminder too.

---

## Housekeeping

**The cull rule (owner card, 2026-10-19: "7 days after superseded").** A handoff is
cull-eligible when all three hold:

1. a newer **committed** handoff of the same seat family exists (`X` and `X_REBOOT` are one);
2. `cull_days` (default **7**, per-repo `.handoff.json` may override) have passed since that
   **newer** handoff's stamp: the grace runs from supersession, not from the file's own age;
3. its lessons are **filed**: its carry-forward and Traps sections hold no lesson content
   ("nothing this wave", empty), or every lesson line either carries a `(filed: …)` /
   `(see: …)` citation or appears (markup and wrapping ignored) in text ever added to the
   repo's `lessons_file` inbox (a line filed and later drained still counts), or the handoff is
   named in a harvest file that was committed and then **deleted in a later commit**.

No human drain is needed. `--cull` only lists; `--cull --apply` deletes; git is the archive. In a
**follower repo** (registry row Send off, e.g. RimMandrake) it lists but **refuses `--apply`**:
this machine never deletes there.

- **`--harvest`** writes every lesson section since the last inbox shrink into
  `harvest_<date>.md` (or `--out`). To turn it into a filing receipt: commit it, file every keeper
  into the lessons inbox, then delete it in a commit. **Delete a harvest only after filing**: the
  deletion is what marks every handoff it names as filed.
- **The weekly retrospective** does this per repo: file lessons from superseded handoffs into the
  inbox, then `handoff.py --cull --apply` and commit the deletions with named paths.

⚠️ **A misconfigured `handoff_dir` fails silently on the read side.** a project kept
its handoffs in a non-default subfolder with no `.handoff.json`, so the default resolved to
a nonexistent `./handoffs` and `--wake` answered "nothing to wake from" for a week while a real
handoff sat there. Fixed 2026-10-06. **When `--wake` says there
is nothing, confirm the directory it looked in** before believing it: `handoffs/` at the repo
root unless `.handoff.json` says otherwise.

## Who commits the conversation record

`conversations.py` never commits. Where the record lands decides who does:

- **work machine, github.com repo** (ruling 5, 2026-10-13): the record goes to
  `<private store>/projects/<Repo>/conversations/`, never the github.com repo; the sweep
  commits it in the private store.
- **Send-on repo, record in-tree**: the hourly sweep commits it.
- **Send-off repo (this machine only follows it), record in-tree**: the sweep never commits
  there. The repo's own handoff must; RimMandrake runs
  `python3 src/RimMandrake/Utils/conversations_land.py --apply` right after `conversations.py`.
