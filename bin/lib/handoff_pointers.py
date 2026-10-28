"""Handoff pointers resolved through the outcome registry (2026-10-22, Option 1).

A pointer REFERENCES work; it is not a ledger of its own (GPT review, 2026-10-22). Its live
state is read from the store that owns the work, at wake and when the handoff is checked:

    - `LODE-12` — parked at step 2; NEXT: run the probe          (the id itself is an item)
    - `SIGNUP` — waiting on Zebulon (an invented name); ref: alerts:slack:D01; NEXT: …  (an explicit reference)
    - `GRANT` — asked IT; check: {"type":"gh_collaborator","repo":"o/r","login":"x"}; NEXT: …

`ref: <store>:<id>` names an item in any registered store; `check: {…}` is an inline outcome
check in the `lib/outcome.py` vocabulary (never `cmd`: a handoff file is synced text). A
pointer with neither, whose id matches no item, is a coverage gap: shown as such, decided from
its text. Labels are ids only, safe in any session.
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Tuple

try:
    from lib import outcome
except ImportError:  # pragma: no cover
    import outcome  # type: ignore

_KEY = re.compile(r"^- `([^`]+)`")
_REF = re.compile(r"\bref:\s*`?([a-z][a-z0-9_-]{0,30}):([^\s;`]+)`?")
_CHECK = re.compile(r"\bcheck:\s*`?(\{[^{}]*\})`?")

LIVE, DONE, SATISFIED, GONE, GAP, UNKNOWN = ("live", "done", "satisfied", "closed-other", "gap",
                                             "unknown")


def parse(bullet: str) -> Tuple[str, Optional[Tuple[str, str]], Optional[Dict], str]:
    """(key, (store, id) or None, check spec or None, problem) for one pointer bullet."""
    m = _KEY.match(bullet)
    key = m.group(1) if m else bullet[2:60]
    ref = None
    r = _REF.search(bullet)
    if r:
        ref = (r.group(1), r.group(2))
    spec, problem = None, ""
    c = _CHECK.search(bullet)
    if c:
        try:
            spec = outcome.validate_spec(json.loads(c.group(1)))
        except ValueError as e:
            problem = "pointer %s has an invalid check: %s" % (key, e)
    return key, ref, spec, problem


def _item(key: str, ref):
    if ref:
        store = outcome.get_store(ref[0])
        if store is None:
            return None, "no store %r" % ref[0]
        try:
            return store.lookup(ref[1]), ""
        except Exception as e:  # noqa: BLE001
            return None, "store %s unavailable (%s)" % (ref[0], type(e).__name__)
    return outcome.find_item(key), ""


def resolve(bullet: str, use_cache: bool = True) -> Tuple[str, str]:
    """(kind, one-line status) for one pointer. Kinds: live, done, satisfied, closed-other,
    gap, unknown. Never writes."""
    key, ref, spec, problem = parse(bullet)
    if problem:
        return UNKNOWN, problem
    item, why = _item(key, ref)
    if spec is not None:
        res = outcome.evaluate(spec, use_cache=use_cache)
        if res.state == outcome.SATISFIED:
            return SATISFIED, "SATISFIED (%s): close the work and drop this pointer" % res.reason
        if res.state == outcome.UNKNOWN:
            return UNKNOWN, "verification unavailable (%s); decide from the text" % res.reason
        return LIVE, "not yet: %s" % res.reason
    if item is None:
        if ref:
            return UNKNOWN, "%s:%s not found here%s" % (ref[0], ref[1], " (%s)" % why if why else "")
        return GAP, "unresolved: no linked item or check (coverage gap); decide from the text"
    if item.live:
        tail = ""
        if item.check is not None and not outcome.is_gap(item.check):
            res = outcome.evaluate(item.check, use_cache=use_cache,
                                   allow_cmd=getattr(outcome.get_store(item.store),
                                                     "trusted_cmd_ok", False))
            store = outcome.get_store(item.store)
            grade_ok = store.may_close(item)[0] if store is not None else False
            if res.state == outcome.SATISFIED and not grade_ok:
                tail = "; check holds but is a fact, not completion (LODE-23)"
            elif res.state == outcome.SATISFIED:
                return SATISFIED, "%s is %s but its check is SATISFIED (%s): run " \
                                  "bin/reconcile.py, drop this pointer" % (item.label, item.state,
                                                                           res.reason)
            else:
                tail = "; check %s" % res.state
        return LIVE, "LIVE: %s is %s%s" % (item.label, item.state, tail)
    store = outcome.get_store(item.store)
    if item.state in getattr(store, "done_states", ()):
        return DONE, "DONE: %s is %s; drop this pointer" % (item.label, item.state)
    return GONE, "CLOSED: %s is %s (not done: re-read before acting)" % (item.label, item.state)


def statuses(bullets: List[str], use_cache: bool = True) -> Dict[str, str]:
    out = {}
    for b in bullets:
        key = parse(b)[0]
        try:
            out[key] = resolve(b, use_cache)[1]
        except Exception as e:  # noqa: BLE001 - wake must still print the handoff
            out[key] = "unresolved (resolver failed: %s)" % type(e).__name__
    return out


def problems(bullets: List[str]) -> List[str]:
    """For `handoff.py --check`: a pointer may not hand the next session finished work."""
    bad = []
    for b in bullets:
        try:
            kind, line = resolve(b)
        except Exception:  # noqa: BLE001
            continue
        key = parse(b)[0]
        if kind in (DONE, SATISFIED):
            bad.append("pointer `%s` references finished work (%s); drop it" % (key, line))
        elif kind == UNKNOWN and line.startswith("pointer "):
            bad.append(line)
    return bad
