# The Handoff Ritual — `bin/handoff.py`

**One sentence:** when a long-running agent session ends, this tool captures the judgment
that would otherwise die with its context — and, unlike a diary, *enforces that the next
session actually consumes it*.

It is project-agnostic, stdlib-only Python 3.9+, and designed to be copied into any repo
(see [Propagating to a new project](#propagating-to-a-new-project)). Lodestar is its home;
projects carry copies.

---

## Why this exists — the measurements

The design is not taste; it is a three-arm audit of 67 real handoffs from the RimWorld
project (2026-09-17, session-linked in the decision log, D25):

| Finding | Number | Design consequence |
|---|---|---|
| Pointers ("here's what's half-done") consumed by the successor | **25%** (19 of 76) | A read-side step now exists: `--wake` |
| Pickup when the pointer carried a concrete imperative `NEXT:` action | **near-100%** | `--check` refuses a pointer without `NEXT:` |
| Pickup for prose warnings without one | **~0%** | same |
| Recent handoffs shipping an *unattributed* dirty-tree dump | **7 of 11** | every dirty line gets a `<<< WHOSE? >>>` marker `--check` can see |
| Mechanical bulk (git log / status dumps) per handoff | **~52%**, one file 844 lines | commits capped at 20 + a `git log` range pointer |
| Lesson-claims stranded in handoffs that no durable doc ever received | **43 of 171** | `--harvest` extracts them all in one command |
| Hand-written files bypassing the checker entirely | happened | `--check` verifies the canonical headings, not just markers |

The one-line summary of the audit: **writing the handoff was disciplined; reading it was
optional; and everything optional died.** So the ritual now has two halves, and the read
half is the one this tool exists to enforce.

---

## The ritual at a glance

```
  AGENT, at a stopping point            NEXT SESSION, first turn
  ─────────────────────────            ─────────────────────────
  handoff.py            ──writes──►    handoff.py --wake
  fill 4 judgment sections             reads pointers + live status
  handoff.py --check    (refuses       picks up / closes / declines
  commit + push          bad shape)    EACH ONE, out loud
  say "HANDOFF READY"

  WEEKLY RETROSPECTIVE, per repo        HOUSEKEEPING, same sitting
  ─────────────────────────────         ─────────────────────────
  handoff.py --harvest                  handoff.py --cull
  file lessons of superseded            lists handoffs superseded
  handoffs into the lessons inbox       >= 7 days, lessons filed
  (no owner drain needed)               --apply deletes; git archives
```

### What the owner types

Nothing, usually. Two phrases cover everything:

- **"prepare handoff"** (any wording) — the agent runs the write side end-to-end and its
  last line will be `HANDOFF READY`. That is the signal the session is safe to kill.
  Agents should also self-trigger this the moment they would otherwise say *"that's all I
  have for now"* — reporting idleness **is** the handoff moment.
- **"wake"** — forces a fresh session through the read side, though a well-configured
  project puts `--wake` in its session-start ritual so this is automatic.

`HANDOFF READY` is said **once**. A signal repeated on every idle turn is not a signal;
with nothing committed since the last handoff the tool prints `ALREADY HANDED OFF` and
refuses to write a second one saying the same nothing.

### What the agent runs

```sh
bin/handoff.py             # write the skeleton (refuses if gates fail; --force overrides)
# ... fill the four <<< WRITE THIS >>> sections ...
bin/handoff.py --check     # exit 0 = say HANDOFF READY yourself; the script never does
bin/handoff.py --wake      # first turn after a reboot: predecessor's handoff + pointer status
# on wake and before HANDOFF READY: TaskStop list-running; bin/stale_agents.py <id>...; TaskStop each FINISHED id (never LIVE)
bin/handoff.py --harvest   # all lesson sections since the last drain -> one file
bin/handoff.py --cull      # list deletable handoffs (rule below); add --apply to delete
```

`--wake` leads with a `!!!` block when a session (a `conversations/` record, often in
`<private store>/projects/<Repo>/`, or a `~/.claude/projects/` transcript) started after the newest
handoff and left none, naming its absolute path; the status line adds a "hand off soon" line at 60%
context and "HAND OFF NOW" at 85% (`claude/statusline/blocks/45-handoff-nudge.sh`).

---

## The content contract

The skeleton is mostly written by the script (commits, tree state — the mechanical ~half
the audit found agents were hand-assembling at their most expensive moment). Four sections
are left as loud `<<< WRITE THIS >>>` markers, because a script cannot write them:

1. **The one thing to carry forward** — the single most important thing learned.
2. **What the owner should see** — findings needing a human eye or veto.
3. **What is half-done, and where it stops** — every bullet must be
   `` - `ITEM_OR_TOPIC` — state; NEXT: <one imperative action> ``.
4. **Traps learned** — ONE line each, ending `(filed: LESSONS)` or `(see: <doc>)`.
   The trap goes into the lessons file *when it is learned*; the handoff cites it.
   Re-explaining a recorded trap is refused — that duplication is how two copies drift.

`--check` refuses, with the measured reason: unfilled markers · missing canonical headings
(so a hand-written file cannot bypass the contract) · a half-done bullet without `NEXT:` ·
a trap without a citation · any dirty-tree line still carrying `<<< WHOSE? >>>` instead of
naming whose uncommitted work it is.

**`--check` files lessons itself** (2026-10-19, after 17 handoffs sat blocked from `--cull`):
every carry-forward/Traps unit (a bullet, or a whole wrapped paragraph) with no citation and
not already in `lessons_file` (normalized-text dedup) is appended to the inbox as
`- <date> (handoff <name>): …`, the handoff line gets `(filed: <INBOX>)`, and `--check` exits 1
until the inbox is committed. **Not when the handoff is on the private host** (`private_store`) and
the inbox is in a github.com repo: nothing is copied (names could leak), and an unfiled
carry-forward is refused instead — file a de-identified line by hand and cite it.

**Drafts waiting on the owner are surfaced, never carried** (LODE-20, 2026-10-23). `--wake`
opens with an `OWNER NEEDED` block for every Gmail or Outlook draft older than 12 h (from the
Companion's `~/dev/.lodestar/companion/drafts-waiting.json`, or a direct small Gmail read when that
is older than 30 min; ids and ages only, `bin/lib/handoff_drafts.py`). `--check` refuses a
half-done pointer that holds a draft "pending his word", and a draft named elsewhere that the
DRAFTS ledger does not list. `LODESTAR_HANDOFF_DRAFTS=off` disables both (the test suite sets it).

Two things it never does, by design: it **never says HANDOFF READY** (only the agent's
judgment can; a script that could emit the reboot signal would eventually emit it wrongly),
and `--cull` **never deletes without `--apply`** (and even then, git history is the archive),
nor at all in a follower repo (registry row Send off).

---

## Configuration — `.handoff.json` (optional)

Zero-config works: handoffs land in `handoffs/`, and a repo with no competing agent
identities runs as seat `SOLO`. With multiple identities in the corpus and no
identity source, it **refuses rather than guesses** — a wrong guess files one agent's
reboot under another's name and hands it their unfinished work (this happened; see D25).

```json
{
  "handoff_dir": "handoffs",
  "lessons_file": "LESSONS.md",
  "seat_env": ["AGENT_SEAT", "HANDOFF_SEAT"],
  "default_seat": null,
  "cull_days": 7,
  "commits_cap": 20,
  "adapter": null
}
```

| Key | Meaning |
|---|---|
| `handoff_dir` | repo-relative home of `<SEAT>_HANDOFF_<utcstamp>.md` files and harvests |
| `lessons_file` | the durable lessons inbox: a file, a directory of one-file-per-lesson files, or a list of either. `--harvest` detects the last **drain** as the newest commit in which one of them *shrank* (filing adds; only curation deletes lines or files). A commit whose subject contains `[no-drain]` (e.g. splitting the inbox into files) is never a drain |
| `seat_env` | env vars consulted, in order, for agent identity |
| `default_seat` | pin an identity (e.g. `"SOLO"`) and skip inference |
| `cull_days` | grace, in days, **measured from supersession** (the newer same-seat handoff's stamp, not this file's age), before a handoff whose lessons are filed is cull-eligible. Default 7 (owner card 2026-10-19). "Filed" = no lesson content, or every carry-forward/Traps line cited `(filed: …)`/`(see: …)` or present in text ever added to `lessons_file`, or the handoff is named in a harvest file committed and then deleted. Follower repos (registry Send off) are listed but never deleted |
| `commits_cap` | commit lines shown before collapsing to a `git log` range pointer |
| `adapter` | repo-relative path to a Python adapter module (below) |
| `private_store` | `true`: handoffs live in `<private store>/projects/<Repo>/<handoff_dir>/` (private host) whenever this repo can push to github.com — never in its own tree (ruling 5, 2026-10-13; Lodestar sets it). A work machine with no usable private-store is refused rather than written in-repo. Commit and push the handoff in the private store; `--owed` measures this repo's commits from the overlay commit's time |

Unknown keys are **refused, not ignored** — a typo that silently changes nothing is worse
than a crash.

## Adapter — plugging in a project's own machinery

A project with a work ledger, a hardware lock, or richer session state supplies a small
module (any subset of four hooks):

```python
# bin/handoff_adapter.py  — example
def seat():
    """Identity, when env vars aren't how this project does it."""

def gates():
    """Extra refuse-to-reboot conditions. Return a list of problem strings —
    e.g. ['BRIDGE still held — release it first']. Keep it SHORT: a gate
    that fires every time is a gate nobody reads."""
    return []

def pointer_state(ids):
    """{pointer_id: current state} from your ledger, so --wake shows LIVE
    status next to the predecessor's snapshot instead of trusting it."""
    return {}

def extra_sections():
    """[(title, [lines])] appended to the skeleton — e.g. items closed this
    window, from your ledger. Mechanical facts only; judgment stays human."""
    return []
```

The RimWorld project's `handoff.py` (this tool's ancestor and reference customer) wires
the equivalents of all four into its `rimflow` ledger and game-bridge lock.

---

## Propagating to a new project

**Nothing to do — a project is already covered.** Superseded D25's "projects receive copies";
see D26 (2026-09-21). The ritual reaches every session on both machines through two
user-scope surfaces Lodestar already owns and converges:

- the **`handoff` skill** — `docs/handoff/SKILL.md`, declared in `claude/skills.md` as
  `Machines: all`, symlinked to `~/.claude/skills/handoff`. One copy, so it cannot drift from
  itself.
- the **machine-level item** — `claude/global/55-handoff-on-reboot.md`, rendered into
  `~/.claude/CLAUDE.md`, which names the trigger phrases and the non-negotiables.

`bin/handoff.py` is **not copied per repo.** `_find_root()` is `git rev-parse --show-toplevel`
against the working directory, so the single installed copy resolves whichever repo the session
is in (verified from Hestia and Dialexis: each reported its own repo, seat `SOLO`). A repo needs
no files, no config and no instructions of its own; `handoffs/` and `LESSONS.md` appear on
first use.

Only two things are ever per-project, and both are optional: a `.handoff.json` when the
defaults are wrong, and an adapter when the project has its own ledger or lock to gate on.

The audit's central lesson is why the skill leads with the read side: **a handoff ritual with no
enforced read side converges on write-only theater** — 60% of its pointers will die, politely,
in a file everyone agrees is important.

---

## Provenance

- Decision: `docs/DECISIONS.md` D25 (2026-09-18).
- Evidence: three-arm corpus audit in a transient working folder
  (consumption, payload, harvest arms; 67 handoffs, 76 graded pointers, 171 graded
  lesson-claims) — Transient shelf-life applies; the numbers that matter are in the
  table above and in D25.
- Ancestor: `Rimworld/src/RimMandrake/Utils/handoff.py` (project-specific; keeps its
  rimflow/bridge gates). The two share the content contract; this one is the portable
  core. Tests: `tests/test_handoff.py`.
