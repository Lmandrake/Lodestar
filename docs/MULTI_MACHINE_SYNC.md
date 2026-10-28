# Lodestar — Design: Multi-machine git auto-sync for `~/dev`

**Status:** design (2026-08-28). Executor: Claude Code. This document specifies *what* to build and
*why*; it deliberately stops short of final implementation.

## Goal

Every project under `~/dev` is its own git repo, same name on every machine (permanent Desktop +
mobile Laptop, possibly more). They should **pull and push on their own**, keeping all machines
current without the user thinking about it — while never losing or silently corrupting work.

## Constraints & context

- Remotes assumed to be the private git host (confirm). Non-interactive auth required for a daemon.
- Two-writer reality: the same repo may be edited on two machines. Conflicts must be *surfaced*,
  never auto-resolved.
- Reuse what exists: **Waypoint** already tracks active project roots by name → use it as the
  participation registry rather than inventing one. **Machinist** holds the launchd knowledge.
  **Lidocaine** is a working launchd plist precedent. **cocaffeinate** keeps the Laptop awake
  while a sync/agent runs with the lid closed.

## Non-negotiable safety principles

1. **Never `push --force`.** Ever.
2. **Never auto-resolve a conflict.** On conflict: abort cleanly (`rebase --abort`), leave the
   working tree exactly as it was, and **loudly flag** the repo as needing attention.
3. **Commit only quiescent trees.** Only auto-commit a dirty repo that has been *unchanged for N
   minutes* (debounce), so we never commit mid-edit or half-saved state.
4. **Opt-in, not opt-out.** A repo participates only if it's in the registry (see below). New/secret/
   huge repos are excluded by default.
5. **Guard secrets and large files.** Refuse to auto-`add` files matching secret patterns
   (`.env`, `*.pem`, key material) or larger than a threshold unless git-LFS-tracked. Rely on
   `.gitignore` first, this as a backstop.
6. **Skip unsafe states.** Detached HEAD, mid-merge, mid-rebase, or existing conflict markers →
   skip and flag; do not touch.

## Options & trade-offs

### Option A — Timer-based sweep (launchd) · **RECOMMENDED baseline**

A single script sweeps every registered repo on an interval: `pull --rebase --autostash`, then if
the tree is quiescent-dirty, `add -A` + a conventional auto-commit + `push`. Driven by a per-user
**launchd LaunchAgent** on macOS (runs in the login session → has Keychain/SSH access), every
2–5 minutes.

- **Pros:** simple; git remains the single source of truth (versioned, auditable); no new services
  or network infra; one process, not one-per-repo; predictable, bounded churn; easy to reason about.
- **Cons:** latency up to the interval; auto-commits add WIP noise to history; still needs a
  conflict strategy (covered above).
- **Mitigations:** conventional commit messages (`chore(sync): <host> <ISO8601>`) so noise is
  filterable/squashable; short interval for hot repos, long for cold ones (see cadence tiers).

### Option B — Event-driven watcher (fswatch / gitwatch) · alternative / upgrade

Watch each working tree with `fswatch` (macOS) and commit+push shortly after changes settle.

- **Pros:** near-real-time propagation; changes leave a machine seconds after you stop typing.
- **Cons:** one watcher process per repo (dozens); very noisy history unless debounced hard;
  more moving parts; the low latency rarely matters for research repos.
- **Verdict:** reserve for the few repos where latency genuinely matters. For Lodestar itself,
  latency is already handled by the CLAUDE.md ritual (push-after-every-update), so Option B is
  usually unnecessary.

### Option C — Sync the working tree via Dropbox / iCloud / Syncthite · **ANTI-PATTERN**

Letting a file-sync service replicate the `.git` directory. **Do not do this** — concurrent
file-sync corrupts git object stores and index state. Documented here only to rule it out.

### Cross-cutting choice — history hygiene

- *Auto-commit to the working branch* (simplest; accept WIP noise). **Recommended default.**
- *Auto-commit to a per-machine branch* (`sync/desktop`, `sync/laptop`) and merge deliberately —
  cleaner mainline history, but adds a merge step and defeats "hands-off." Offer as opt-in for
  repos where clean history matters.

## Recommended design

**A hybrid, registry-driven launchd sweep** with three cadence tiers:

| Tier | Repos | Cadence | Rationale |
|------|-------|---------|-----------|
| Hot | Lodestar; anything actively "attached" as a loop today | ~1–2 min (or ritual push-after-write) | freshness is load-bearing for the board |
| Warm | All other registered `~/dev` projects | ~5 min | keep machines current cheaply |
| Cold | Large / rarely-touched / data-heavy repos | on-demand or hourly | avoid churn and bandwidth |

### AS BUILT (2026-09-01): tiers throttle the FETCH, not the sweep

Lukas originally chose a single fast sweep over these tiers — correct at ~16 repos, wrong at 26.
The registry grew, and the arithmetic caught up: one sweep every **127 s** × **26 repos** is
~680 fetches an hour, ~16,000 a day, all SSH to one host. **GitHub throttles that, and its
refusal is indistinguishable from lost auth** — "Please make sure you have the correct access
rights / and the repository exists". The tell that it was throttling was that the *set* of
failing repos changed between runs: `sync.py --dry-run` reported 14 flagged, then 3 with a
different membership, then 0, while every repo fetched fine individually and `ssh -T
git@github.com` authenticated cleanly. `bin/touch.py` also failed a real pull this way.

So the tiers came back, but applied to the **fetch** rather than to the sweep. The distinction
is the whole design, and it is what makes the throttle safe:

> **The 127 s cadence exists to satisfy "commit and push as you go", so work is never lost.
> That promise needs `push`, not `fetch`.** Fetching only matters in order to *receive* the
> other machine's work.

Because `sync.py` pulls before it pushes, skipping a fetch is only safe when there is nothing
to push. Hence the split, per repo, per sweep:

- **The local half runs unconditionally, on every repo, every 127 s.** `git status` and
  `ahead/behind` against the already-known remote ref are local and free. **Anything dirty,
  ahead, or behind is fetched immediately whatever its tier** — you cannot push without
  pulling first. *Promptness of saving work is preserved exactly.*
- **Only a clean, non-diverged repo has its fetch deferred**, because there a fetch could
  discover nothing but inbound work. Roughly half the registry is in that state at any moment,
  which is where all the savings are.

| Tier | Derived from | Fetch interval (clean repos only) |
|------|--------------|-----------------------------------|
| `hot` | newest commit within **24 h** — plus `Lodestar`, always | **15 min** |
| `warm` | newest commit within **7 days** | **1 h** |
| `cold` | older than 7 days | **6 h** |

**Tiers are derived, never declared.** The input is the repo's own newest commit
(`git log -1 --format=%ct`, purely local), so **a repo promotes itself to hot the moment
someone commits in it — there is no registry column to keep current** and no way for the
tiering to be quietly wrong because nobody edited a table. An unreadable age (missing
directory, no commits) reads as `hot`: unknown must fail towards fetching.

`Lodestar` is pinned hot because it is the board *and* the airlock — inbound captures and
requests land there for the other runtime to pick up, so its freshness is load-bearing in a way
no other repo's is.

The 15-minute hot interval is the number that matters: it bounds how long after the Laptop
pushes that the Desktop notices. It cuts a hot repo from ~28 fetches an hour to 4, and the
whole registry from ~680/h to well under 100/h.

**State lives outside the board**, at `~/dev/.lodestar/fetch-state.json`, next to
`sync-status.json` and for the same two reasons: decision D-D (a per-machine file inside a
synced repo makes two machines overwrite each other) and the `.git/index.lock` lesson (anything
lock-shaped inside a synced repo propagates and wedges the other machine). It is pure cache and
**safe to delete** — missing or malformed reads as empty, which makes every repo due and
restores the old behaviour exactly. `--dry-run` never writes it.

**A deferred fetch is stated, never implied.** `clean` used to render as `up to date`, which
conflated "no local changes" (always freshly measured) with "the remote holds nothing new"
(only true if this sweep fetched). Under throttling the second could be six hours stale, so per
decision D13 the two halves are now separate: `SYNC.md` carries a `Remote` column reading
`hot · verified just now` or `cold · not checked for 3h`, and the `clean` detail reads
*"no local changes; remote not checked for 3h (cold tier, next check due in 3h)"*.

Sweep algorithm per repo (pseudocode, for CC to implement):

```
for repo in waypoint_registered_repos():
    if unsafe_state(repo): flag(repo, "unsafe git state"); continue
    if not on_participating_branch(repo): continue
    git -C repo fetch
    if diverged(repo):
        if git -C repo pull --rebase --autostash fails:
            git -C repo rebase --abort; flag(repo, "conflict — needs you"); continue
    if dirty(repo) and quiescent(repo, N_minutes) and no_secrets_or_bigfiles(repo):
        git -C repo add -A
        git -C repo commit -m "chore(sync): $HOST $(date -Iseconds)"
    if ahead(repo): git -C repo push   # never --force
    record_status(repo)   # last sync, ahead/behind, flags
```

### Conflict avoidance via loop ownership (ties into Lodestar)

The cheapest conflict is the one that never happens. Because Lodestar already records which
machine has a loop **attached and active**, the sweep can treat that as an **advisory soft-lock**:
if `loop.repo` is actively attached on Desktop, the Laptop's sweep still *pulls* but avoids
auto-committing edits there. Not a hard lock — just a nudge that dramatically cuts collisions,
since it encodes "work a given repo on one machine at a time," which is how the user works anyway.

### Auth

Non-interactive: SSH keys via ssh-agent / macOS Keychain, or a git credential helper. The launchd
agent must run in the login context so it inherits Keychain access. (Machinist should document the
exact the private git host SSH setup.)

### Observability

Write a machine-local `~/dev/.lodestar/sync-status.json` (last sync time, ahead/behind counts, and
any flags per repo) and surface a one-line rollup on the Lodestar `BOARD.md` — so "is everything
synced / what needs my attention" is answerable in the one place the user already looks. Anything
flagged (conflict, unsafe state, blocked push) must be visible there; silent failure is the enemy.

### Platform

- **SETTLED: fleet is all macOS** (Desktop + Laptop) → **launchd LaunchAgent on both**, modeled on
  Lidocaine's plist. No systemd/cron branch needed. (Still heed the hostname/machine-detection
  caveat from env_paths memory when the agent decides "who am I" for commit messages / soft-locks.)
- Use **cocaffeinate** to hold the Laptop awake through a lid-close only while a long sync/agent runs.

## Open questions / assumptions to confirm with the user

1. Remote host = the private git host for all repos? Any repos on a different remote or with no remote?
2. ~~Laptop OS~~ — SETTLED: macOS → launchd on both machines.
3. Which repos opt in initially? (Proposal: seed the registry from Waypoint's active roots.)
4. Any repos with large binaries that need git-LFS before they can safely auto-sync?
5. ~~History hygiene~~ — SETTLED: WIP commits on mainline.
```
