# The Lodestar tour

Lodestar is an operating layer for one person who runs a fleet of AI coding agents across several
machines. This tour walks the parts that are hardest to get right and most worth copying. Every
section has commands you can paste and a short account of why it is built the way it is.

Everything runs against an invented fleet, so nothing here touches your home directory:

```sh
git clone <this repo> && cd Lodestar
python3 run_tests.py            # the suite
python3 bin/demo_fleet.py --tour   # about 45 seconds; narrates the sections below
```

`demo_fleet.py` builds a sandbox of three machines (`studio`, `laptop`, `tower`), seven bare
remotes standing in for a git host, and a Lodestar board. It then runs the real commands with each
machine's `HOME` pointed into the sandbox. The sandbox path is printed at the end and kept, so you
can `cd` into any machine and keep poking. One shortcut is narrated in the output: a clean repo's
fetch waits out a 15-minute tier, and the tour deletes the receivers' fetch-state record instead
of waiting.

Requires Python 3.11+ and git. Stdlib only.

---

## 1. Multi-machine sync

### The problem

Every project is its own git repo, with the same name on every machine. They should pull and push
by themselves, so that work started on the laptop is on the desk machine when you sit down, and
nothing is lost or silently corrupted on the way.

### The registry

`sync/registry.md` is a markdown table, one row per repo: its id, the remote, and which machines
carry it (`Machines` takes roles or machine keys; blank means all). `machines.md` names the
machines and their labels (`mac`, `laptop`, `linux`). The table is the single declaration of the
fleet. Because it is markdown in git, it merges, diffs and reviews like code.

### The sweep

```sh
python3 bin/sync.py --quiescence 0      # what a timer runs hourly on each machine
```

One sweep per machine does, for each registered repo: fetch, fast-forward if behind, commit
sweepable files (not source code; you commit that yourself) and push. It never force-pushes,
never merges for you and never overwrites a dirty tree. It clones at most one absent repo per run,
so a fresh machine converges over several sweeps. The tour shows it:

```
  after 5 sweep round(s):
  studio  ~/dev holds: Atelier, Fieldnotes, Lantern, Lodestar, Orchard, Tidepool
  laptop  ~/dev holds: Atelier, Fieldnotes, Lantern, Lodestar, Orchard, Tidepool
  tower   ~/dev holds: Atelier, Fieldnotes, Lodestar, Orchard, Quarry, Tidepool
```

Three details carry most of the design:

- **Tiers throttle the fetch, not the sweep.** A hot repo is fetched every sweep, a cold one
  rarely. Sweeping is cheap; the network is the cost.
- **A quiescence gate.** A repo touched in the last few minutes is left alone, so the sweep never
  commits half of what an agent is still writing. The tour passes `--quiescence 0` to disable it.
- **A flag means "nothing from this machine is reaching the fleet."** A repo that cannot be
  synced says so in the sweep report (the `🚩` lines above) rather than failing quietly.

### Work travels

In the tour the laptop writes a note into one repo. Its next sweep commits and pushes it; the
other two fast-forward it. All three print `first-pick.md present: True`.

### The plan lifecycle

Renaming a repo on a host is easy. Making five machines agree, including the ones that are
asleep, is the hard part. `bin/lifecycle.py` handles five kinds of change, called plans:

| Kind | Meaning |
|------|---------|
| `rename` | the repo has a new name; every checkout moves and every `origin` is repointed |
| `rehome` | the repo moves to a different host; the declared remote and push URLs change |
| `retire` | stop managing it; a permanent tombstone stops anything re-cloning it |
| `quarantine` | take the checkout out of the working area but keep a local copy |
| `purge` | remove the checkout fleet-wide, tombstone the id, delete the hosted repo |

The flow is the same for each:

```sh
python3 bin/lifecycle.py propose --kind rename --id fieldnotes ...   # authors a plan; approves nothing
python3 bin/lifecycle.py approve rename-fieldnotes-demo --yes        # the origin gate: asked once
python3 bin/lifecycle.py status                                      # who has converged, who is pending
python3 bin/lifecycle.py converge                                    # run by every sweep; never prompts
```

Section 3 of the tour does it for real. The studio proposes and approves
`Fieldnotes -> Notebook`; the plan and its approval are committed to the board; then the laptop
and tower pull the board and converge on their next sweep, without being asked:

```
  studio  ~/dev/Notebook=True ~/dev/Fieldnotes=False origin=<workdir>/remotes/Notebook.git
  laptop  ~/dev/Notebook=True ~/dev/Fieldnotes=False origin=<workdir>/remotes/Notebook.git
  tower   ~/dev/Notebook=True ~/dev/Fieldnotes=False origin=<workdir>/remotes/Notebook.git
```

Why it is built this way:

- **Approval is once, at the origin.** The plan carries a fingerprint of exactly what was
  approved. If the plan text changes, the fingerprint changes and the approval no longer applies.
  Offline machines obey on wake, so `approve` prints the fleet-wide consequence first.
- **State lives in separate files, and that was a bug once.** A retirement list kept apart from
  the registry can merge into a contradiction (one id both renamed and retired) with every merge
  exiting 0. So contradictions between the registry, machines and tombstones are validated, and a
  plan that would act on a contradictory board refuses.
- **Receipts, not inference.** Each machine records what it did. `status` reports "complete on A,
  B; pending on C (no receipt)". A report never says "evicted" when it means "quarantined".
- **Omission is not deletion.** A repo missing from the registry is left alone. Only an approved
  `retire` or `purge` removes anything, and purge is armed separately.
- **Honest scope.** A purge removes the working checkout and the hosted repository. Backups, other
  clones and forks are outside its reach, and its output says so rather than claiming erasure.

### The upstream-rewrite halt

Sometimes a repo's history is deliberately rewritten (to remove something that must not be in
it). Every other clone then has history that diverges from upstream, and a normal sync would try
to merge the old history back in, undoing the rewrite.

So the sweep halts on that repo and flags **"upstream history was rewritten"**. It does not
merge, rebase or push. `bin/rewrite_recover.py` is the way out. It is stdlib-only, because it runs
against an old checkout whose own libraries are out of date. It pauses the sweep for that repo,
bundles every local ref and a patch of uncommitted changes into a carryover folder (the old
history is kept on that machine only, and never pushed), resets main onto upstream, expires the
reflog so no old object is left to push, restores machine-local files and lifts the pause. It
loses nothing and it cannot reintroduce the history it exists to remove.

---

## 2. CLAUDE.md management

### The problem

Every Claude Code session reads `~/.claude/CLAUDE.md`. On several machines that file drifts, is
lost with the machine, and differs for good reasons (a laptop has a battery; a headless box has no
GUI). Two failures are silent: Cowork-style runtimes skip a `CLAUDE.md` that is a symlink, and skip
`@import`s that resolve outside the working directory. So the live file must be a real file.

### Fragments, overlays, render

The repo holds the rules as small markdown fragments in `claude/global/`, plus per-machine overlays
in `claude/global/machines/<key>.md`. A fragment can carry an `applies:` header that selects
machines by key or label:

```
<!-- applies: mac -->
# Mac machines only
```

```sh
python3 bin/claudeconf.py preview     # what WOULD this machine's file be; read-only
python3 bin/claudeconf.py render      # write ~/.claude/CLAUDE.md
python3 bin/claudeconf.py check       # exit 1 if the live file drifted or a snapshot is stale
python3 bin/claudeconf.py snapshot    # copy settings, statusline and memory into the repo
python3 bin/claudeconf.py import      # recovery: rebuild the fragments FROM a live file
```

The tour renders all three machines from one set of fragments. The tower has no `mac` label, so it
never receives the Mac section:

```
  studio  ~/.claude/CLAUDE.md headings: # Machine | # Speed is the default | # Mac machines only | # Save and push as you go
  laptop  ~/.claude/CLAUDE.md headings: # Machine | # Speed is the default | # Mac machines only | # Save and push as you go
  tower   ~/.claude/CLAUDE.md headings: # Machine | # Speed is the default | # Save and push as you go
```

Design reasons:

- **One source, rendered, never symlinked.** The file each session reads is real and
  machine-specific; the truth is the fragments.
- **Drift is detected, not assumed away.** `check` compares the live file with a fresh render, so
  hand edits on a machine are caught rather than lost or silently overwritten.
- **Snapshots make `~/.claude` recoverable.** It is not a git repo, so settings, the status line
  and auto-memory are copied into the board and versioned there. The same machinery manages
  `settings.json` (a shared base plus per-machine overlays) and a generated status line.
- **An edit that cannot delete is a feature.** Removing a key from the shared base removes it from
  nowhere; the lessons record this as a trap, so the tooling says so instead of implying success.

---

## 3. Skill auto-linking

### The problem

A Claude Code skill is a directory under `~/.claude/skills/`. A skill installed by hand reaches
exactly one machine, and when its source repo is renamed or not cloned, the link dangles. Claude
Code does not error on a dangling link; the skill just silently does not exist.

### The registry drives the links

`claude/skills.md` is a table: skill name, target path, owning repo, machines.

```
| Skill        | Target                           | Repo     | Machines |
|--------------|----------------------------------|----------|----------|
| field-sketch | ~/dev/Atelier/skills/field-sketch | Atelier  | all      |
| tide-tables  | ~/dev/Tidepool/skills/tide-tables | Tidepool | all      |
| lantern      | ~/dev/Lantern                    | Lantern  | laptop   |
| retired-skill| ~/dev/Atelier/skills/retired-skill |        | all      |
```

The sweep reads the table and makes `~/.claude/skills/<name>` a symlink to the target, on the
machines the row names. A row follows its repo: when the repo is cloned, the link appears; when a
rename plan converges, the target is repointed. In the tour the laptop gets the `lantern` skill and
the tower does not:

```
  laptop  field-sketch -> .../laptop/dev/Atelier/skills/field-sketch, lantern -> .../laptop/dev/Lantern, tide-tables -> ...
  tower   field-sketch -> .../tower/dev/Atelier/skills/field-sketch, tide-tables -> ...
```

### Health

```sh
python3 bin/claudeconf.py skills     # link what can be linked, list what cannot
python3 bin/check.py --verbose       # the whole health report; the skills row is one of many
```

The demo table has one deliberate bad row, `retired-skill`, whose target does not exist. Both
commands catch it:

```
    ⚠ dangling: retired-skill — target missing and no registered repo
      WARN  claudeconf skills   1 wiring fault(s): dangling: retired-skill ...
         a dangling link is a repo that is not cloned here; Claude Code does not error on one,
         so the skill silently does not exist in any session on this machine
```

(`check.py` also reports rows about services that do not exist in a sandbox; the skills row is the
point.) Reasons: the table is the declaration, so a skill cannot be half-installed; the validator is
derived from the consumer, since the `Machines` column here takes keys or labels while the registry's
takes roles or keys; and a dangling row is a visible warning, never a quiet absence.

---

## 4. Board, loops, tickets and the handoff ritual

### The board

Work is tracked as **loops**: one markdown file each, with typed frontmatter (state, deadline,
time-to-close estimate) in `loops/`, and an append-only **journal** of events in `journal/`.
`BOARD.md` is rendered from them by `bin/board.py`. Because the journal is append-only and the
files are per-loop, two machines almost never conflict, and git merge is the sync. Every loop must
carry a time-to-close estimate, and there is no cap on active loops. The tour prints the board:

```
    ## 🔴 Active — 2
    | `demo-notebook-rename` | Rename Fieldnotes to Notebook on every machine | ... | 30m |
    | `demo-sync-health`     | Keep the three demo machines converged         | ... | 1h  |
    ## 🟢 Background — 1
    | `demo-skill-cleanup`   | Remove the retired-skill row once nothing needs it | 15m |
```

```sh
python3 bin/board.py --check      # is BOARD.md current?
python3 bin/board.py --rebuild    # re-derive loop state from the journal
```

### Tickets

A ticket is one markdown file in the owning project's own git home, so a bug found in one repo can
be filed in another with one line and travels with that repo:

```sh
python3 bin/ticket.py new --project lodestar --type repair "Drop the retired-skill row from claude/skills.md"
python3 bin/ticket.py list --project lodestar
```
```
    LODE-1  open  repair  demo  Drop the retired-skill row from claude/skills.md
    LODE-2  open  build   demo  Give the tower a battery-free overlay note
```

### The handoff ritual

An agent session ends and its judgment dies with its context. `bin/handoff.py` captures it:

```sh
python3 bin/handoff.py --check    # gates and a content scan; exit 1 if not ready
python3 bin/handoff.py --wake     # first turn of the next session: the last handoff and pointer status
```

A handoff is refused unless everything is committed and pushed. Each open item is written as a
pointer with a state and one imperative next action, and the successor must answer every pointer on
waking. The point is that "nothing is saved until it is pushed" is enforced by a gate, not by
good intentions. See `docs/HANDOFF.md`.

---

## 5. The private-prose boundary: a security case study

Lodestar is a public repo describing a private working life. The board is written by agents that
read mail and chat, and prose written by a model cannot be reliably classified as safe. So the
rule is **structural, enforced at commit time, and a separate tool governs what ever leaves**.

### Layer 1: proseguard, markers and the two hooks

`bin/lib/proseguard.py` makes prose unwelcome in the public history by construction:

- A loop file carries only its frontmatter and a fixed pointer line. The body lives in a private
  store, outside the repo.
- A journal event carries no free-text note, only a reference to one in the private store.
- Some paths (conversation records, handoffs, consult transcripts) are never committed at all.
- No added line may carry a **marker**: an address on a protected domain, an Outlook item id, a
  Teams thread id. This is a backstop, not the rule, because a regex cannot see paraphrase. That is
  why the first three rules are structural.

`.githooks/pre-commit` runs `bin/prose_guard.py --staged`, and `.githooks/pre-push` runs
`--outgoing`, which checks the commits about to be pushed, so a commit made with hooks bypassed
still cannot leave. `--fix` moves prose to the private store without deleting anything.

### Layer 2: the private overlay

The private store (a separate private repo, found by `bin/lib/privatestore.py`, configured in `sync/private-store.json`, default
the private store) holds the things that must exist but must not be public: loop bodies, journal
notes, the lists of names, terms and hosts the exporter scans for, and the publish manifests. Code
reads it through one door and degrades to a clear refusal when it is absent. The suite passes with
an empty store, which is how CI and a fresh clone run it.

### Layer 3: publish.py, default-deny

`bin/publish.py` exports a repo as a clean public tree. It fails closed:

```sh
python3 bin/publish.py build  <repo>   # pushed, clean HEAD -> manifest-filtered tree
python3 bin/publish.py scan   <repo>   # every scanner class; exit 1 on any unallowed hit
python3 bin/publish.py review <repo>   # cold second-model read of the tree: a hard gate
python3 bin/publish.py rule   <repo> --owner-said "<his words>"   # owner rules over its findings
python3 bin/publish.py diff   <repo>   # built tree vs last released tree: the human gate
python3 bin/publish.py release <repo> --approved-by "<the owner's words>"
```

- **Default-deny manifests.** A per-repo manifest names what `include`s; everything else stays home.
  The builder refuses an include pattern that matches nothing, so a typo cannot silently drop a
  file. Files that must be rewritten (a stub for an excluded module, a public `CLAUDE.md`, the
  licence) are `[[generate]]` entries with inline text or a source file.
- **Scanners.** The tree, and every file path, is scanned for protected-domain addresses,
  private host names, full names and name forms (first names, handles, `Last, First`), program
  codes, staffing figures, home paths, machine ids, any e-mail address, export-control words and
  secrets (via gitleaks). Text is Unicode-normalised and zero-width characters are stripped first.
- **Allowances are content-hash bound.** A hit that is a true false positive is allowed by naming
  the path, the file's exact sha256, the scanner class and the match. Edit the file and the
  allowance lapses and the hit blocks again. An allowance can never quietly outlive the thing it
  excused.
- **Canary tests.** The scanner's own tests plant synthetic names, addresses and hosts and assert
  that each class fires, so a scanner that has gone blind fails the suite instead of reporting
  "0 hits".
- **The cold-read gate.** Zero scanner hits is not a clean export. The first real export of a
  sibling repo scanned clean and an independent model read of it still found 26 issues: first names
  with a place, meeting dates, staffing levels. `release` therefore refuses without a receipt that a
  different model read exactly this tree and answered `VERDICT: CLEAN`; any failure to obtain a
  verdict is a refusal, never a pass.
- **The owner ruling.** When the cold read reports findings the owner has judged harmless, `rule`
  records his words verbatim against that exact receipt (tree hash, manifest hash, answer hash and
  review id). A rebuild or a new review lapses it; a transport or parse failure leaves no receipt to
  rule over, and a CLEAN one needs none. The release output and its record say "released on owner
  ruling over N findings" with his words, never CLEAN.
- **The human diff.** `diff` shows the owner what changed since the last release. Default-deny
  plus this diff is the real control; the regexes are the safety net under it.
- **Release is mechanical and recorded.** A pushed commit to a staging clone with a pre-push hook that re-checks, and a line in
  an append-only releases file.

This repository's own public tree went through every step above.

---

## 6. The evaluation discipline

Most of the repo's lessons come from instruments that were wrong, not from the things measured.
The habits that survived:

- **Never grade your own work with something you can edit.** If a checker fails, fix the thing
  checked, or report that the checker broke. A checker edited until it passes is no longer a checker.
- **A green test with no caller is not a feature.** After shipping a module, count its callers
  outside `tests/` and look at the world it was meant to change.
- **A mutation harness must show the mutation inverted the behaviour,** not merely that it ran.
  Print both measurements.
- **`N errors during collection` means zero tests ran.** Read the summary line, not the tail.
- **A probe that truncates manufactures gaps.** Reading 4,000 characters of each of 958 files
  reported five "missing" messages; reading whole files reported none.
- **`--verify` that proves parsing proves nothing about content.** 236 of 319 files parsed fine and
  lacked the one field the tool promised.
- **A check that passed before and fails now is a stop,** to be surfaced with both states, not
  quietly fixed.
- **A scanner needs a cold reader.** Lists catch what you thought of; an independent read catches
  the rest.

A selection of these, rewritten to stand alone, is in [LESSONS-selected.md](LESSONS-selected.md).
