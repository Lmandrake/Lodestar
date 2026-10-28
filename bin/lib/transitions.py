"""In-flight lifecycle transitions — written before mutating, so a crash is recoverable.

The record exists to answer one question after an interruption: **is the thing I was told to
do already done, partly done, or not started?** Without it, recovery has to guess from the
filesystem, and every guess is wrong in a way that costs material.

**Recovery inspects reality and compares it to the record — it never infers intent from
reality alone.** `observe` reports one of four states and `recovery_action` decides:

| observed | meaning | action |
|---|---|---|
| only at the old path | the move never started | proceed |
| only at the new path | the move landed | verify the remaining steps |
| **at both** | two directories claim one identity | **stop** |
| at neither | it is somewhere else | consult the recorded holding location |

**`BOTH` is a stop, unconditionally.** Two directories claiming one id is the same conflict
`lib/enroll.py` refuses to resolve, for the same reason: picking a winner destroys whichever
copy loses, and there is no evidence here about which one holds the work. It stops even when
the fingerprint matches, because a matching fingerprint says the *plan* is current, not that
the *world* is sane.

**🔴 A stale record is evidence, never permission.** This is the invariant that dominates all
four rows above. The spec puts it as *"an old transition record is evidence, not permission to
finish a rehome that current intent has superseded"*: if the owner changed the destination
after this record was written, finishing the recorded move completes an operation he has since
revoked. So the record carries the plan fingerprint **as it was when the record was written**,
and a mismatch returns `STOP` even at `NOT_STARTED` — the one place where proceeding would
otherwise be exactly right. Getting that precedence backwards is how a converger cheerfully
finishes yesterday's decision.

**An unreadable store is not an empty one.** `paths.lifecycle_transitions_file` says out loud
that this is not a cache and not safe to delete, because it is the only record of where
material was moved to. Treating a corrupt file as `{}` would silently convert "the material
is at a recorded holding location" into "there is no material", which is the most expensive
mistake available here. So `load` raises `Unreadable` rather than returning a plausible empty
dict. A *missing* file genuinely does mean nothing is in flight, and a missing *entry* means
nothing is in flight for that id; those two are safe and are not errors.

🔴 **THE STORE IS KEYED BY `plan_id`, AND IT USED TO BE KEYED BY `repo_id`.** The old key gave
one repo a single slot, so two approved plans for one repo contended for one record — measured
2026-10-01 with a rehome and a quarantine both approved for one throwaway repo: the quarantine
converged and *finished* its record, the rehome then ran in the same pass, wrote its own record
over the top, failed, and left an unfinished record that made every later plan for that repo
report `STOP` for ever. One ordinary failure wedged the repo and needed a person.

**The single slot was also accidental mutual exclusion, and removing it without replacement
would be a regression.** It is the only reason two transitions for one repo could never both be
in flight, which matters because one plan's `old_path` can be another's `new_path`. So the
protection is now EXPLICIT rather than emergent: `conflicting` asks whether an *unfinished*
transition exists for the same repo under a *different* plan, `record` refuses one by raising
`Conflict`, and the converger reports it before acting. The difference from the old behaviour is
precisely the wedge: a **finished** record no longer blocks anything, because a completed
transition is history rather than an obstruction.

A row written under the old key is re-keyed on load from its own `plan_id` field, which
`Transition` has carried since it was written. Nothing migrates by hand.
"""

import json
import os
from typing import Dict, NamedTuple, Optional

from . import clock, paths

#: Present only at the old path — the move has not begun.
NOT_STARTED = "not-started"
#: Present only at the new path — the directory arrived; satellites may not have.
MOVED = "moved"
#: Present at BOTH paths. A conflict to report, never to resolve by picking a winner.
BOTH = "both-present"
#: Present at neither path — it is somewhere else, and only the record knows where.
NEITHER = "neither"

#: Carry on with the transition as recorded.
PROCEED = "proceed"
#: The relocation landed; check every remaining step independently before declaring it done.
VERIFY = "verify"
#: Refuse and report. Never a state to clear automatically.
STOP = "stop"
#: Read the recorded holding location. Never guess where material went.
CONSULT = "consult-holding"


class Unreadable(Exception):
    """The transition store exists but will not parse.

    Deliberately an exception rather than a silent `{}`: an empty result here reads as "no
    material is in flight", and acting on that when a holding location was actually recorded
    is how quarantined material becomes unfindable.
    """


class Conflict(Exception):
    """An unfinished transition for this repo already exists under a DIFFERENT plan id.

    Deliberately an exception rather than a silent overwrite. Overwriting is what the old
    `repo_id`-keyed store did, and it is how a completed quarantine's record was replaced by a
    failed rehome's and wedged the repo (module docstring). Two transitions in flight for one
    repo is a conflict about the world — one plan's `old_path` can be another's `new_path` —
    so it is reported, never resolved by picking a winner.
    """


class Transition(NamedTuple):
    plan_id: str
    repo_id: str
    kind: str
    old_path: str
    new_path: str
    holding_path: str
    #: The plan's fingerprint AT THE MOMENT this record was written. A mismatch against the
    #: current plan means intent moved on, and that dominates every observed state.
    fingerprint: str
    started_at: str
    finished_at: str = ""

    def as_dict(self) -> Dict[str, str]:
        return dict(self._asdict())


def observe(old_path: str, new_path: str) -> str:
    """Which of the four states reality is in. Pure filesystem, no record consulted.

    `lexists`, not `exists`: a dangling symlink at the destination is very much *something
    in the way*, and `exists` follows the link and reports the destination as free. An
    occupied destination that looks empty is how a move lands on top of something.
    """
    old_there = os.path.lexists(old_path)
    new_there = os.path.lexists(new_path)
    if old_there and new_there:
        # A compatibility link left at the old path, resolving to the moved checkout itself, is
        # the move finished rather than a second claimant (one machine keeps a junction at
        # the old path for hardcoded paths). Only a LINK qualifies, and only one that lands
        # exactly on a real directory at the destination; two real directories are still BOTH.
        if (os.path.islink(old_path) and not os.path.islink(new_path)
                and os.path.isdir(new_path)
                and os.path.realpath(old_path) == os.path.realpath(new_path)):
            return MOVED
        return BOTH
    if old_there:
        return NOT_STARTED
    if new_there:
        return MOVED
    return NEITHER


def load(path: Optional[str] = None) -> Dict[str, Transition]:
    """plan_id -> Transition. A missing file is {}; an unreadable one raises.

    The asymmetry is the point — see the module docstring and `Unreadable`.

    **Rows written under the old `repo_id` key are re-keyed here**, from the `plan_id` the row
    itself has always carried. A row with no `plan_id` keeps the file's key for both fields, so
    a hand-written or pre-`plan_id` row still loads rather than being silently dropped.

    Two rows resolving to one key RAISES rather than letting one win: the loser would be the
    only record of where that transition's material went, and this store exists precisely so
    that is never guessed.
    """
    target = path or paths.lifecycle_transitions_file()
    if not os.path.exists(target):
        return {}
    try:
        with open(target, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (ValueError, OSError) as exc:
        raise Unreadable(
            "the transition store at %s exists but will not parse (%s). It is the only record "
            "of where in-flight material was moved to, so it must not be read as empty. Inspect "
            "it by hand before any further lifecycle action." % (target, exc))
    if not isinstance(data, dict):
        raise Unreadable(
            "the transition store at %s parsed to %s, not an object of plan_id -> transition. "
            "Refusing to read that as 'nothing in flight'." % (target, type(data).__name__))

    result = {}  # type: Dict[str, Transition]
    for key, row in data.items():
        if not isinstance(row, dict):
            raise Unreadable(
                "the entry for %r in %s is %s, not an object — refusing to guess past it"
                % (key, target, type(row).__name__))
        plan_id = str(row.get("plan_id") or key)
        if plan_id in result:
            raise Unreadable(
                "two entries in %s resolve to plan id %r. One of them is the only record of "
                "where that transition's material went, so neither may be discarded. Inspect "
                "the file by hand." % (target, plan_id))
        result[plan_id] = Transition(
            plan_id=plan_id,
            repo_id=str(row.get("repo_id") or key),
            kind=str(row.get("kind", "")),
            old_path=str(row.get("old_path", "")),
            new_path=str(row.get("new_path", "")),
            holding_path=str(row.get("holding_path", "")),
            fingerprint=str(row.get("fingerprint", "")),
            started_at=str(row.get("started_at", "")),
            finished_at=str(row.get("finished_at", "")),
        )
    return result


def _save(store: Dict[str, Transition], target: str) -> None:
    """tmp + os.replace, so a crash mid-write cannot truncate the one record that matters."""
    directory = os.path.dirname(target)
    if directory:
        os.makedirs(directory, exist_ok=True)
    payload = {plan_id: t.as_dict() for plan_id, t in sorted(store.items())}
    tmp = target + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, target)


def conflicting(store: Dict[str, Transition], plan_id: str,
                repo_id: str) -> Optional[Transition]:
    """The UNFINISHED transition for `repo_id` under a different plan, if there is one.

    🔴 **This is the explicit replacement for protection that used to be accidental.** While
    the store was keyed by `repo_id` a repo had exactly one slot, so two transitions could not
    both be in flight — not by design, but because the second overwrote the first. Keying by
    plan id removes that side effect, and one plan's `old_path` can be another plan's
    `new_path`, so the check has to be made on purpose or it is not made at all.

    **Only UNFINISHED records conflict**, and that is the wedge fix: a finished record is
    history. Under the old key a completed quarantine's record was replaced by a failed
    rehome's, and the leftover unfinished row then stopped every later plan for the repo for
    ever.
    """
    for other in sorted(store.values()):
        if other.plan_id == plan_id:
            continue
        if other.repo_id == repo_id and not other.finished_at:
            return other
    return None


def record(t: Transition, path: Optional[str] = None) -> None:
    """Write the record. Call this BEFORE mutating anything — that ordering IS the recovery.

    Overwrites any existing entry for the SAME plan id on purpose: a re-run of one plan
    supersedes its own stale record, and keeping both would leave two answers to "what is in
    flight for this plan".

    Raises `Conflict` when a different plan has an unfinished transition for the same repo —
    see `conflicting`. The refusal lands here, before the mutation, because `record` is the
    one call every executor makes before it touches anything.
    """
    if not t.repo_id:
        raise ValueError("a transition must name a repo_id")
    if not t.plan_id:
        raise ValueError("a transition must name a plan_id — the store is keyed by it")
    target = path or paths.lifecycle_transitions_file()
    store = load(target)
    clash = conflicting(store, t.plan_id, t.repo_id)
    if clash is not None:
        raise Conflict(
            "plan %s wants to start a %s on %s, but plan %s already has an unfinished %s for "
            "the same repo (started %s, old=%s new=%s holding=%s). Two transitions in flight "
            "for one repo can each be the other's source or destination, so this is reported "
            "rather than resolved. Finish or clear the first one by hand."
            % (t.plan_id, t.kind, t.repo_id, clash.plan_id, clash.kind,
               clash.started_at or "(unstamped)", clash.old_path or "(none)",
               clash.new_path or "(none)", clash.holding_path or "(none)"))
    store[t.plan_id] = t
    _save(store, target)


def finish(plan_id: str, at: Optional[str] = None, path: Optional[str] = None) -> bool:
    """Stamp the transition finished, keeping the row. False when there was nothing in flight.

    The row is kept rather than dropped so a later inspection can tell "completed here" from
    "never attempted here" — `forget` is the separate, deliberate erasure.
    """
    target = path or paths.lifecycle_transitions_file()
    store = load(target)
    existing = store.get(plan_id)
    if existing is None:
        return False
    store[plan_id] = existing._replace(finished_at=at or clock.now())
    _save(store, target)
    return True


def forget(plan_id: str, path: Optional[str] = None) -> bool:
    """Drop the record entirely. False when there was none."""
    target = path or paths.lifecycle_transitions_file()
    store = load(target)
    if plan_id not in store:
        return False
    del store[plan_id]
    _save(store, target)
    return True


def in_flight(store: Dict[str, Transition]) -> Dict[str, Transition]:
    """Only the transitions that have not been stamped finished. Keyed as the store is."""
    return {plan_id: t for plan_id, t in store.items() if not t.finished_at}


def recovery_action(t: Transition, observed: str, current_fingerprint: str) -> Dict[str, str]:
    """What to do about a recorded transition, given reality and the CURRENT plan.

    Precedence, and the order is the whole design:

    1. **`BOTH` stops**, whatever the fingerprint says. Two directories claiming one identity
       is a conflict about the world, and a current plan does not make it safe.
    2. **A fingerprint mismatch stops**, whatever reality says — including at `NOT_STARTED`,
       where proceeding would otherwise be correct. The record describes intent that has been
       superseded; finishing it completes a decision the owner has revoked.
    3. Only then does the observed state choose between proceed / verify / consult.
    """
    if observed not in (NOT_STARTED, MOVED, BOTH, NEITHER):
        raise ValueError("unknown observed state %r" % (observed,))

    if observed == BOTH:
        return {"action": STOP,
                "reason": "%s is present at BOTH %s and %s, so two directories claim the id "
                          "%r. That is a conflict to report, never to resolve by picking a "
                          "winner — whichever copy lost would take its work with it. Both are "
                          "preserved; decide by hand which is the real checkout."
                          % (t.repo_id, t.old_path, t.new_path, t.repo_id)}

    if current_fingerprint and t.fingerprint and current_fingerprint != t.fingerprint:
        return {"action": STOP,
                "reason": "the recorded transition for %s was written against plan fingerprint "
                          "%s but the current plan fingerprints as %s, so the affected paths, "
                          "identities or disposition have changed since. An old record is "
                          "evidence that something was started, not permission to finish an "
                          "intent that has been superseded. Re-approve the current plan."
                          % (t.repo_id, t.fingerprint, current_fingerprint)}

    if observed == NOT_STARTED:
        return {"action": PROCEED,
                "reason": "%s is still only at %s, so the relocation never began and the "
                          "recorded plan is still current." % (t.repo_id, t.old_path)}

    if observed == MOVED:
        return {"action": VERIFY,
                "reason": "%s is at %s and gone from %s, so the directory landed. A moved "
                          "directory is not a completed transition — check every satellite "
                          "independently before declaring it converged."
                          % (t.repo_id, t.new_path, t.old_path)}

    if t.holding_path:
        return {"action": CONSULT,
                "reason": "%s is at neither %s nor %s. The record says the material was moved "
                          "to %s — read that location. Do not re-create the checkout and do "
                          "not treat absence as completion."
                          % (t.repo_id, t.old_path, t.new_path, t.holding_path)}

    return {"action": CONSULT,
            "reason": "%s is at neither %s nor %s and the record names no holding location, so "
                      "where the material went is genuinely unknown. Refusing to guess: "
                      "re-cloning would fabricate a checkout and could overwrite work that is "
                      "sitting somewhere unrecorded. This needs a person."
                      % (t.repo_id, t.old_path, t.new_path)}
