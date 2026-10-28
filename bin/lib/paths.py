"""Where everything lives.

One module owns Lodestar's layout so no other module hardcodes a path. `LODESTAR_ROOT`
overrides the repo root, which is what lets the test suite run against a temp board
instead of the real one.

🔴 **There are TWO roots, and for a long time only one of them could be redirected.**
`LODESTAR_ROOT` covers the repo; machine-local execution state lives *outside* any repo, under
`~/dev/.lodestar/`, deliberately — MULTI_MACHINE_SYNC.md specifies it there so a per-machine
lock, receipt or status file can never be synced to a peer that would then act on it. But all
thirteen of those paths were written as a hardcoded `expanduser`, so **no test could sandbox any
machine-local state at all** except by monkey-patching each function one at a time.

That is not theoretical. Measured 2026-10-01: an end-to-end CLI test redirected `LODESTAR_ROOT`
as every test here does, drove a real `converge`, and wrote two fixture plans
(`purge-throwaway-20261001`, `rehome-thing-e2e`) into the **live** completion store on this
Desktop. The derived fleet receipt then claimed this machine had converged a purge and a rehome
that never happened, and `SYNC.md` rendered it as fact. `LODESTAR_STATE_DIR` closes that: unset
it behaves exactly as before, so production is unchanged, and a test may now redirect the half
of the layout that was previously unreachable.
"""

import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_ROOT = os.path.dirname(os.path.dirname(_HERE))  # bin/lib -> bin -> repo
_DEFAULT_STATE = "~/dev/.lodestar"


def root() -> str:
    return os.environ.get("LODESTAR_ROOT") or _DEFAULT_ROOT


def state_dir() -> str:
    """Machine-local execution state. NOT in any repo, and not under `root()`.

    Overridable with `LODESTAR_STATE_DIR` so a test can isolate it; see the module docstring for
    the live corruption that absence caused. Kept a separate knob from `LODESTAR_ROOT` on
    purpose: the two are different in kind, and a test that redirects the board while
    deliberately exercising real machine-local state is a legitimate thing to write.
    """
    return os.path.expanduser(os.environ.get("LODESTAR_STATE_DIR") or _DEFAULT_STATE)


def state_file(*parts: str) -> str:
    """A path inside `state_dir()`. Every machine-local path goes through here."""
    return os.path.join(state_dir(), *parts)


def loops_dir() -> str:
    return os.path.join(root(), "loops")


def loop_file(loop_id: str) -> str:
    return os.path.join(loops_dir(), loop_id + ".md")


def journal_dir() -> str:
    return os.path.join(root(), "journal")


def machines_file() -> str:
    return os.path.join(root(), "machines.md")


def registry_file() -> str:
    return os.path.join(root(), "sync", "registry.md")


def board_file() -> str:
    return os.path.join(root(), "BOARD.md")


def tombstones_file() -> str:
    """Append-only record of retired repo ids. Committed and shared, unlike sync-status.

    Deliberately a SECOND store beside the registry's `State` column, because the two need
    opposite properties. A lifecycle disagreement between two peers must become a git merge
    CONFLICT, which it does when the state lives on the row — measured 2026-09-30: the same
    disagreement split across two files merges cleanly into a desired state saying one id is
    both active and retired. But a tombstone must survive the row being DELETED, which is
    exactly when a column cannot help. Appends never contradict each other, so merging this
    file is always safe.
    """
    return os.path.join(root(), "sync", "tombstones.md")


def plans_file() -> str:
    """Append-only lifecycle plans: what a move/removal will do, fleet-wide. Shared.

    Committed, because the whole point of §3C is that the originating machine decides and
    every other machine obeys without prompting — so the plan has to travel. Append-only for
    the same reason as tombstones: two peers authoring plans concurrently must merge, and
    appends cannot contradict each other.

    Deliberately a SEPARATE FILE from the approvals, not a second table in this one.
    `mdtable.parse` reads only the first contiguous table in a document, so a second table
    below it would be silently invisible — the exact trap already recorded for
    `claude/skills.md`'s rows-after-the-blank-line. And an approval must be a new append
    rather than an edit to the plan row, or recording it would rewrite a line another machine
    may also be rewriting.
    """
    return os.path.join(root(), "sync", "plans.md")


def approvals_file() -> str:
    """Append-only approvals for lifecycle plans, each naming a plan id and a fingerprint.

    Separate from the plan itself so approving never rewrites the plan row (see
    `plans_file`). The fingerprint is what makes the approval lapse when the affected paths,
    identities or dispositions change, while leaving it valid across an unrelated journal or
    board commit.
    """
    return os.path.join(root(), "sync", "approvals.md")


def lifecycle_transitions_file() -> str:
    """This machine's in-flight lifecycle transitions. Outside the repo, machine-local.

    Written BEFORE any mutation so a crash is recoverable (§3D.6): recovery inspects reality
    and compares it to this record rather than guessing. Machine-local because which
    directory this machine has moved, and how far, is a per-machine fact — committing it
    would make two machines overwrite each other (decision D-D).

    It is NOT a cache and must never be treated as safe to delete: it is the only record of
    where material was moved to, so losing it turns a recoverable half-applied transition
    into material at an unknown location. A missing entry means "no transition in flight for
    that id", never "finish whatever reality suggests".
    """
    return state_file("lifecycle-transitions.json")


def lifecycle_completions_file() -> str:
    """The last converge outcome this machine reached for each PLAN. Machine-local.

    This is the completion record that retires a finished plan. Without it `cmd_converge`
    re-evaluates every approved plan on every run on every machine for ever — measured
    2026-10-01, when two renames completed weeks earlier still took the lock, re-read their
    git remotes and re-scanned their satellites on every sweep, and two plans whose registry
    row had been deleted reported `omitted` with no way to ever stop.

    🔴 **Machine-local, deliberately, and this is the whole reason a shared status column
    would be wrong.** A plan's `Machines` column can name several machines, so one machine
    asserting "done" must never suppress the plan on a machine that has not run it. The
    cross-machine view is built from the per-machine *receipts* instead — see
    `lifecycle_receipt_file`.

    🔴 **NOT the same file as `lifecycle_journal_state_file`, and they must not be merged.**
    That one exists only to de-duplicate journal events and says out loud that it is safe to
    delete — losing it costs one repeated event. Losing this one costs a re-run of completed
    work on every machine, so it is state rather than a de-duplication cache. The two happen
    to hold similar-looking data, which is exactly the shape of mistake CLAUDE.md §7 records
    for three append-only stores with three different duplicate rules.
    """
    return state_file("lifecycle-completions.json")


def lifecycle_receipts_dir() -> str:
    """Per-machine lifecycle completion receipts. COMMITTED, one file per machine key.

    `lifecycle_completions_file` is machine-local and therefore unreadable by a peer, so
    completion needs a channel that travels. The proven precedent is
    `claudestatusline.receipt_path` (`claude/snapshot/<key>/statusline-receipt.json`), created
    because `gitutil.is_source` withholds every `.sh` from the sweep and the rendered script
    could never reach another machine.

    Three properties, each load-bearing:

    - **One file per machine key**, so no machine can assert another's state and two machines
      writing at once merge as two separate files rather than a conflict.
    - **`.json` under `sync/`**, which `gitutil.is_source` classifies as *sweepable* — not a
      source suffix, not a root doc, and `sync` is not in `SOURCE_DIRS`. So the ordinary hourly
      sweep commits it with no authored message needed.
    - **Byte-deterministic for unchanged input.** A timestamp in the payload would change the
      bytes every sweep, commit a file on every pass for ever, and keep Lodestar from passing
      the sweep's own quiescence gate. Same trap `claudestatusline.receipt_path` records.
    """
    return os.path.join(root(), "sync", "lifecycle-receipts")


def lifecycle_receipt_file(machine_key: str) -> str:
    """This machine's committed completion receipt. See `lifecycle_receipts_dir`.

    The key is sanitised the same way `lifecycle_lock_file` sanitises a repo id, for the same
    reason: a machine key reaches this from `machines.md`, which is hand-edited, and a key
    carrying a `/` would write outside the directory.
    """
    safe = "".join(ch if (ch.isalnum() or ch in "-_.") else "-" for ch in machine_key)
    if not safe.strip("-."):
        raise ValueError(
            "machine key %r has no usable characters for a receipt filename" % (machine_key,))
    return os.path.join(lifecycle_receipts_dir(), "%s.json" % safe)


def lifecycle_journal_state_file() -> str:
    """The last converge status this machine JOURNALED for each plan. Machine-local.

    Exists so `lifecycle converge` journals a status CHANGE, not every run. The sweep converges
    before it sweeps Lodestar, so an event written on every run lands in the board minutes before
    the quiescence gate looks at it — and Lodestar then never settles while any plan is approved
    (observed once: five hours of uncommitted heartbeats). Safe to delete: losing it costs
    one repeated event per plan, never a lost one.
    """
    return state_file("lifecycle-journaled.json")


def lifecycle_lock_file(repo_id: str) -> str:
    """The machine-local lock for one repo id, held OUTSIDE the movable directory.

    Keyed on the **id**, never on the path: a path-keyed lock is released and re-derived by
    the very rename it is supposed to guard, so it fails exactly when it is needed (§3D.3).
    Outside the repo for the reason `attend.lease_path` records — anything lock-shaped inside
    a synced tree propagates this machine's pid to the other one, which is the
    `.git/index.lock` wedge of 2026-08-31.

    The hazard this actually closes is an hourly sweep overlapping a manual run on THIS
    machine. It is not a distributed lock and must not be described as one; two machines each
    moving their own copy is correct and needs no coordination.
    """
    safe = "".join(ch if (ch.isalnum() or ch in "-_.") else "-" for ch in repo_id)
    if not safe.strip("-"):
        raise ValueError("repo_id %r has no usable characters for a lock filename" % (repo_id,))
    return state_file("locks/%s.lock" % safe)


def managed_instances_file() -> str:
    """repo id -> the checkout THIS machine manages for it. Outside the repo.

    Machine-local for the same reason as fetch-state: which directory holds a repo is a
    per-machine fact, and committing it would make two machines overwrite each other
    (decision D-D).

    It is not a cache and must not be treated as one. It is the only record that can tell a
    half-applied transition ("moved, but satellites not yet followed") from a completed one,
    so a missing file means "nothing is enrolled here", never "re-derive it by guessing".
    """
    return state_file("managed.json")


def sync_report_file() -> str:
    """Machine-local sync health. Gitignored per decision D-D."""
    return os.path.join(root(), "SYNC.md")


def sync_status_file() -> str:
    """Machine-local sync state, outside the repo entirely.

    `~/dev/.lodestar/` is specified by MULTI_MACHINE_SYNC.md and is deliberately not
    inside the board, so it can never be committed or conflict.
    """
    return state_file("sync-status.json")


def heartbeat_line_file() -> str:
    """One pre-digested line for a shell statusline to read with builtins only.

    Machine-local for the same D-D reason as `sync_status_file`, and outside the repo so it
    can never dirty the tree a sweep is about to judge.

    It exists purely as a cost shim. `heartbeat.facts()` is the real answer but costs ~88 ms
    with Python startup, and its consumer runs every 10 s inside a statusline that already
    spends 0.6-0.7 s per run — so the sweep pays that once and leaves a line parseable with
    `read`. Deleting it is always safe: absent means "no sweep has run here yet", which the
    reader must treat as unknown rather than as a fault.
    """
    return state_file("heartbeat-line")


def fetch_state_file() -> str:
    """When each repo's remote was last contacted. Machine-local, outside the repo.

    Same reasoning as `sync_status_file`, and the same two lessons behind it: a per-machine
    file inside a synced board makes two machines overwrite each other (decision D-D), and
    anything lock-shaped inside it propagates and wedges the other machine (the
    `.git/index.lock` wedge of 2026-09-01).

    It is pure cache and must be safe to delete: a missing or unreadable file means "fetch
    everything once", never a crash.
    """
    return state_file("fetch-state.json")


def clone_attempts_file() -> str:
    """When each repo was last clone-attempted here. Machine-local, outside the repo.

    Machine-local because whether a repo is cloned is per-machine, and committing that would
    make two machines overwrite each other (decision D-D). Pure cache: unreadable means
    "never attempted", which retries rather than crashes.
    """
    return state_file("clone-attempts.json")


def pause_sentinel() -> str:
    """Kill switch for unattended runs (RUNTIME_BRIDGE.md rail 4)."""
    return os.path.join(root(), "PAUSE")


def airlock_dir() -> str:
    """The Cowork↔CC handoff directory (RUNTIME_BRIDGE.md). Committed and synced.

    Unlike SYNC.md, this is deliberately shared rather than per-machine: the whole point
    is that what one runtime deposits, the other picks up.
    """
    return os.path.join(root(), "airlock")


def captures_dir() -> str:
    """What Cowork sensed. Dated subdirectories; read-only facts that accumulate."""
    return os.path.join(airlock_dir(), "captures")


def claude_dir() -> str:
    """The repo's mirror of ~/.claude."""
    return os.path.join(root(), "claude")


def claude_adopt_dir(key: str) -> str:
    """Per-machine, append-only queue of live CLAUDE.md files awaiting classification.

    Per-machine because the live files legitimately differ, and a shared committed path
    would make two machines overwrite each other (decision D-D).
    """
    return os.path.join(claude_dir(), "adopt", key)


def claude_skills_file() -> str:
    """The declarative skill table. Shared and fleet-wide, unlike the adopt queue."""
    return os.path.join(claude_dir(), "skills.md")


def requests_dir() -> str:
    """Things Cowork wants CC to do. Moved to done_dir() once processed."""
    return os.path.join(airlock_dir(), "requests")


def done_dir() -> str:
    """CC's audit trail of processed requests."""
    return os.path.join(airlock_dir(), "done")


def shadow_dir() -> str:
    """Shadow-mode propose-and-log records, in dated subdirectories.

    Committed on purpose: a proposal is the thing a human reviews, and the Phase 3 go-live
    gate reads the reviewed verdicts out of these files.
    """
    return os.path.join(airlock_dir(), "shadow")


def digest_file() -> str:
    """The rolling shadow-run digest. Committed, and re-rendered in full every run."""
    return os.path.join(airlock_dir(), "DIGEST.md")


def shadow_status_file() -> str:
    """What the last shadow run on THIS machine did. Outside the repo, like sync-status.

    Machine-local, so it can never be committed or conflict — and so a paused machine can
    leave a trace on every run without churning the git repo.
    """
    return state_file("shadow-status.json")


def shadow_lock_file() -> str:
    """The shadow runner's run lock. Deliberately OUTSIDE the repo.

    A lock file inside a git-synced repo would be committed and propagate to the other
    machine, wedging it — the same shape as the zero-byte `.git/index.lock` that silently
    blocked commits for minutes on 2026-08-31.
    """
    return state_file("shadow.lock")


def command_receipts_file() -> str:
    """Append-only record of what the command worker actually did. Outside the repo.

    Machine work is a per-machine fact, and `SOURCE_DIRS = ("docs",)` means anything non-`.py`
    under the board is committed and pushed within ~127s — so receipts kept inside it would sync
    to the other machine, which would then read this machine's work as its own.
    """
    return state_file("command-receipts.jsonl")


def command_lease_file() -> str:
    """Who holds the interactive command session. Outside the repo, like shadow.lock.

    Lock-shaped and pid-bearing, so committing it would push this machine's pid to the other one
    and wedge it — the `.git/index.lock` failure of 2026-08-31. It is per-machine by nature
    anyway: two boards open on two machines are two independent sessions.
    """
    return state_file("command.lease")


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path
