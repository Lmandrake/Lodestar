"""Lifecycle plan retirement: what this machine finished, and a receipt that travels.

Before this existed, `lifecycle.py converge` re-evaluated **every approved plan on every run
on every machine, for ever.** Measured 2026-10-01 on the Desktop: five approved plans, two
whose registry row had been deleted reporting `omitted` with no way to ever stop, and two
renames completed the previous day still taking the machine-local lock, re-reading their git
remotes and re-scanning their satellites on every hourly sweep. The plans store is append-only
and its own header forbids deleting a row, so **deletion is not available as the fix** — the
missing concept is completion.

Two stores, and the split is the whole design:

| | where | keyed by | answers |
|---|---|---|---|
| **completions** | `~/dev/.lodestar/lifecycle-completions.json`, machine-local | `plan_id` | what did THIS machine last do for this plan? |
| **receipts** | `sync/lifecycle-receipts/<key>.json`, committed | one file per machine | what has every machine done? |

🔴 **COMPLETION IS A PER-MACHINE FACT, AND A SHARED STATUS COLUMN WOULD BE WRONG.** A plan's
`Machines` column can name several machines — `rename-repo-a-20260101` may name two — so
one machine writing "done" into shared state would suppress the plan on a machine that has
never run it, leaving it permanently half-applied. That is the exact condition the whole repo
lifecycle loop exists to end, and it is why the authoritative record is machine-local and the
shared artifact is one *file per machine* that no peer writes.

**The receipt therefore carries evidence, not a verdict.** It records each plan's `status` and
the `fingerprint` that status was reached against, and nothing else; whether that amounts to
"complete" is re-derived by the reader against the plan as it stands now. Baking a `complete:
true` into the file would be a claim that silently survives the plan being revised, and this
repo has the scars to prove a stored verdict outlives the facts it was computed from.

**A revised plan un-retires itself for free.** `retires` requires the recorded fingerprint to
match the plan's current one, and `plans.fingerprint` covers exactly the fields that describe
the act — including `machines`. So changing a destination, a machine list or a disposition
makes every recorded completion stop applying, which is the same mechanism that lapses the
approval. Nothing has to remember to invalidate anything.

**Latest-wins, and that is a third duplicate rule on purpose.** CLAUDE.md §7 records three
append-only stores with three different rules and warns that their identical shape is the trap.
This store is neither append-only nor shared: it is one mutable row per plan holding the LAST
outcome, so a later write is a correction rather than an echo. Ask what a store *answers* — a
tombstone is terminal so the first retirement counts, a plan id is authored once so the first
is canonical, and "what did I last do" can only be the most recent.

**`omitted` retires a plan, but the skip is re-gated on the row still being absent.** A plan
whose repo id matches no registry row is dead — `cmd_propose` already warns at authoring time,
and every machine reports `unmanaged / omitted` and changes nothing. But a row *could* be added
later, and a completion record that outlived that would permanently skip a plan that had become
actionable. So `retires` takes `row_exists` and withholds retirement when a row has appeared.
`converged` needs no such gate: it is a statement about an act this machine performed, which
only a changed fingerprint can invalidate.

**Why not the other non-acting statuses.** `not-named` and `out-of-scope` are recorded (the
receipt needs them, or a peer reading "pending" cannot tell "has not run" from "deliberately
excluded") but deliberately do NOT retire. They are derived from `plan.machines` and the
registry's own `Machines` column, cost nothing to recompute, and are re-answered correctly on
every run — so retiring them would buy noise reduction at the price of a staleness hazard for
no saved work. `refused`, `stop`, `partial` and `busy` must obviously keep flagging.
"""

import json
import os
from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

from . import clock, paths

#: Bumped when the receipt's shape changes in a way a reader must notice.
SCHEMA = 1

#: The act was performed here. Only a changed fingerprint can invalidate it.
CONVERGED = "converged"
#: No registry row has this plan's repo id, so nothing on disk was touched anywhere.
OMITTED = "omitted"

#: Statuses that retire a plan on the machine that reached them. See the module docstring for
#: why `not-named` and `out-of-scope` are deliberately absent despite never doing any work.
TERMINAL = (CONVERGED, OMITTED)


class Unreadable(Exception):
    """The completion store exists but will not parse.

    An exception rather than a silent `{}`, for a different reason than
    `transitions.Unreadable`: reading this as empty is *safe* (every plan simply gets
    re-evaluated, which is the behaviour this module replaces) but it is also **silent**, so a
    corrupt file would quietly restore the old re-run-for-ever behaviour on one machine and
    nothing would ever say so. A loud failure is the only way that becomes visible.
    """


class Completion(NamedTuple):
    plan_id: str
    repo_id: str
    #: The plan's fingerprint when this outcome was reached. A mismatch retires nothing.
    fingerprint: str
    status: str
    #: Local-time stamp, for a human reading `lifecycle.py status`. Machine-local ONLY — it
    #: must never reach the receipt, which has to be byte-stable across sweeps.
    at: str

    def as_dict(self) -> Dict[str, str]:
        return dict(self._asdict())


# --- the machine-local store ------------------------------------------------------------

def load(path: Optional[str] = None) -> Dict[str, Completion]:
    """plan_id -> Completion. A missing file is {}; an unreadable one raises."""
    target = path or paths.lifecycle_completions_file()
    if not os.path.exists(target):
        return {}
    try:
        with open(target, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (ValueError, OSError) as exc:
        raise Unreadable(
            "the completion store at %s exists but will not parse (%s). Reading it as empty "
            "would silently restore re-running every completed plan on every sweep, so it "
            "fails loudly instead. Inspect or delete it by hand." % (target, exc))
    if not isinstance(data, dict):
        raise Unreadable(
            "the completion store at %s parsed to %s, not an object of plan_id -> completion."
            % (target, type(data).__name__))

    result = {}  # type: Dict[str, Completion]
    for key, row in data.items():
        if not isinstance(row, dict):
            raise Unreadable(
                "the entry for %r in %s is %s, not an object" % (key, target, type(row).__name__))
        result[str(row.get("plan_id") or key)] = Completion(
            plan_id=str(row.get("plan_id") or key),
            repo_id=str(row.get("repo_id", "")),
            fingerprint=str(row.get("fingerprint", "")),
            status=str(row.get("status", "")),
            at=str(row.get("at", "")),
        )
    return result


def _save(store: Dict[str, Completion], target: str) -> None:
    directory = os.path.dirname(target)
    if directory:
        os.makedirs(directory, exist_ok=True)
    payload = {plan_id: c.as_dict() for plan_id, c in sorted(store.items())}
    tmp = target + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, target)


def record(plan_id: str, repo_id: str, fingerprint: str, status: str,
           at: Optional[str] = None, path: Optional[str] = None) -> Completion:
    """Remember this machine's outcome for one plan. Latest-wins — see the module docstring."""
    if not plan_id:
        raise ValueError("a completion must name a plan_id — the store is keyed by it")
    if not status:
        raise ValueError("a completion must name the status it is recording")
    target = path or paths.lifecycle_completions_file()
    store = load(target)
    entry = Completion(plan_id=plan_id, repo_id=repo_id, fingerprint=fingerprint,
                       status=status, at=at or clock.now())
    store[plan_id] = entry
    _save(store, target)
    return entry


def forget(plan_id: str, path: Optional[str] = None) -> bool:
    """Drop one plan's record, so the next converge re-evaluates it. False when there was none.

    The deliberate un-retirement: a `converged` record that a human believes is wrong is
    cleared here rather than by editing the append-only plans store.
    """
    target = path or paths.lifecycle_completions_file()
    store = load(target)
    if plan_id not in store:
        return False
    del store[plan_id]
    _save(store, target)
    return True


def retires(entry: Optional[Completion], current_fingerprint: str,
            row_exists: bool) -> Tuple[bool, str]:
    """(True, why) when this plan needs no further work HERE. Otherwise (False, why not).

    The reason is returned in both directions because the two failures a caller cares about —
    "never completed here" and "completed, but the plan has changed since" — are different
    facts, exactly as `plans.approved` argues for its own pair. Collapsing them to a bare bool
    is how a human reads "pending" and goes looking for a bug in the executor.

    `row_exists` gates only `omitted`; see the module docstring.
    """
    if entry is None:
        return False, "no completion recorded on this machine"
    if entry.status not in TERMINAL:
        return False, ("the last outcome here was %r, which is not terminal" % entry.status)
    if current_fingerprint and entry.fingerprint != current_fingerprint:
        return False, (
            "completed as %r against fingerprint %s, but the plan now fingerprints as %s — the "
            "act it describes has changed, so the completion does not apply"
            % (entry.status, entry.fingerprint or "(none)", current_fingerprint))
    if entry.status == OMITTED and row_exists:
        return False, (
            "recorded as `omitted` because no registry row had id %r, but a row exists now — "
            "so the plan has become actionable and the completion is withheld" % entry.repo_id)
    return True, "already %s here%s" % (entry.status, (" at %s" % entry.at) if entry.at else "")


# --- the receipt that travels -------------------------------------------------------------

def receipt_payload(store: Dict[str, Completion], machine_key: str) -> Dict[str, object]:
    """The committed, byte-stable shape. Evidence per plan — never a `complete` verdict.

    🔴 **NO CLOCK READ REACHES THIS PAYLOAD, and that is a hard requirement rather than a
    tidiness preference.** This file is rewritten on every sweep, so a timestamp would change
    its bytes every hour, commit it on every pass for ever, and keep Lodestar from ever
    passing the sweep's own quiescence gate. `claudestatusline.receipt_path` records the same
    trap from the same cause. `Completion.at` is therefore dropped here and kept only in the
    machine-local store.

    A second-precision timestamp is **invisible to a test that runs inside one second**, which
    is why `tests/test_completions.py` asserts the structural property — that no ISO date or
    time appears anywhere in the payload and no time-shaped key exists — rather than just
    writing twice and comparing.
    """
    return {
        "schema": SCHEMA,
        "machine": machine_key,
        "plans": {
            c.plan_id: {"fingerprint": c.fingerprint, "repo_id": c.repo_id, "status": c.status}
            for c in store.values()
        },
    }


def receipt_text(store: Dict[str, Completion], machine_key: str) -> str:
    return json.dumps(receipt_payload(store, machine_key), indent=2, sort_keys=True) + "\n"


def write_receipt(machine_key: str, store: Dict[str, Completion],
                  path: Optional[str] = None) -> bool:
    """Write this machine's receipt. True when the bytes actually changed.

    The skip-if-unchanged is what makes the sweep quiet: an unchanged receipt is not rewritten,
    so its mtime does not move and there is nothing for the sweep to commit.
    """
    target = path or paths.lifecycle_receipt_file(machine_key)
    text = receipt_text(store, machine_key)
    try:
        with open(target, "r", encoding="utf-8") as handle:
            if handle.read() == text:
                return False
    except OSError:
        pass
    directory = os.path.dirname(target)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = target + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, target)
    return True


def load_receipts(directory: Optional[str] = None) -> Dict[str, Dict[str, object]]:
    """machine_key -> payload, read from every `*.json` in the receipts directory.

    The key comes from the payload's own `machine` field, falling back to the filename. A
    missing directory is `{}`; an unparseable receipt is SKIPPED rather than fatal, because one
    peer's corrupt file must not blank the whole cross-machine view — the machine it belongs to
    then reads as having no receipt, which is the honest answer.
    """
    target = directory or paths.lifecycle_receipts_dir()
    found = {}  # type: Dict[str, Dict[str, object]]
    try:
        names = sorted(os.listdir(target))
    except OSError:
        return found
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(target, name), "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (ValueError, OSError):
            continue
        if not isinstance(payload, dict):
            continue
        key = str(payload.get("machine") or name[:-len(".json")])
        found[key] = payload
    return found


#: Reader-side verdicts. Deliberately three, not two: a machine deliberately left out of a
#: plan is not "pending", and reporting it as such is the kind of misleading-but-passing line
#: CLAUDE.md §7 keeps finding on generated surfaces.
COMPLETE = "complete"
NOT_APPLICABLE = "not applicable"
PENDING = "pending"


def _verdict(entry: Optional[Dict[str, object]],
             current_fingerprint: str) -> Tuple[str, str]:
    """(verdict, note) for one machine's receipt entry for one plan. Pure, re-derived."""
    if entry is None or not isinstance(entry, dict):
        return PENDING, "no receipt"
    status = str(entry.get("status", "") or "unknown")
    recorded = str(entry.get("fingerprint", "") or "")
    if current_fingerprint and recorded and recorded != current_fingerprint:
        return PENDING, "%s, but against the superseded fingerprint %s" % (status, recorded)
    if status in ("not-named", "out-of-scope"):
        return NOT_APPLICABLE, status
    if status in TERMINAL:
        return COMPLETE, status
    return PENDING, status


def summary_lines(receipts: Dict[str, Dict[str, object]],
                  fingerprints: Dict[str, str],
                  machine_keys: Iterable[str],
                  plan_kinds: Optional[Dict[str, str]] = None) -> List[str]:
    """One derived line per plan: "complete on a, b; pending on c".

    Derived from the receipts on disk and from nothing else — no machine's state is inferred
    from another's, and a machine with no receipt reads as `pending (no receipt)` rather than
    being assumed to match its peers.

    `fingerprints` maps plan_id -> the plan's CURRENT fingerprint, so a receipt recorded
    against a since-revised plan is reported as pending rather than counted as done.

    The plan set is the UNION of the fingerprints given and every plan any receipt mentions, so
    a receipt naming a plan this machine cannot see is visible instead of silently dropped —
    this repo's signature defect is a working mechanism whose surface never shows it.
    """
    kinds = plan_kinds or {}
    keys = set(machine_keys) | set(receipts)
    plan_ids = set(fingerprints)
    for payload in receipts.values():
        entries = payload.get("plans")
        if isinstance(entries, dict):
            plan_ids |= set(entries)

    lines = []  # type: List[str]
    for plan_id in sorted(plan_ids):
        current = fingerprints.get(plan_id, "")
        grouped = {}  # type: Dict[Tuple[str, str], List[str]]
        for key in sorted(keys):
            entries = receipts.get(key, {}).get("plans")
            entry = entries.get(plan_id) if isinstance(entries, dict) else None
            verdict, note = _verdict(entry, current)
            grouped.setdefault((verdict, note), []).append(key)
        label = "`%s`" % plan_id
        if plan_id in kinds:
            label += " (%s)" % kinds[plan_id]
        elif plan_id not in fingerprints:
            label += " (no such plan in `sync/plans.md` on this machine)"
        lines.append("- %s: %s" % (label, _segments(grouped)))
    return lines


def _segments(grouped: Dict[Tuple[str, str], List[str]]) -> str:
    out = []  # type: List[str]
    for verdict in (COMPLETE, NOT_APPLICABLE, PENDING):
        parts = [(note, machines) for (v, note), machines in sorted(grouped.items())
                 if v == verdict]
        if not parts:
            continue
        rendered = ", ".join(
            "%s (%s)" % (", ".join("`%s`" % m for m in machines), note)
            for note, machines in parts)
        out.append("%s on %s" % (verdict, rendered))
    return "; ".join(out) if out else "no machines known"


def section_lines(receipts_dir: Optional[str] = None,
                  plans_store: Optional[Dict[str, object]] = None,
                  machine_keys: Optional[Sequence[str]] = None) -> List[str]:
    """`summary_lines` with the three inputs gathered from disk. The SYNC.md entry point.

    Kept separate from `summary_lines` so the rendering is testable without a registry, a
    machines table or a plans file — the house rule that untestable code stays untested, and
    untested surfaces are where the inert features hide.
    """
    from . import machine as machine_mod
    from . import plans as plans_mod

    store = plans_store if plans_store is not None else plans_mod.load_plans()
    fingerprints = {}  # type: Dict[str, str]
    kinds = {}  # type: Dict[str, str]
    for plan_id, plan in store.items():
        try:
            fingerprints[plan_id] = plans_mod.fingerprint(plan)
        except ValueError:
            fingerprints[plan_id] = ""
        kinds[plan_id] = "%s %s" % (plan.kind, plan.repo_id)
    keys = machine_keys if machine_keys is not None else sorted(machine_mod.roles())
    return summary_lines(load_receipts(receipts_dir), fingerprints, keys, kinds)
