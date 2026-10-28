"""The built-in stores of the outcome registry (`lib/outcome.py`), registered on import.

Each adapter is the whole contract between a store and the resolver: what its items are, which
of them are live, their decoded `check`, a version token, and a version-guarded close through
the store's own DONE verb. Nothing here drops, cancels, lapses or ends a standing watch: those
are his word only. A new store is a new adapter registered the same way (see `lib/outcome.py`,
"How to add a STORE"); the resolver does not change.

  alerts     Bearing's conversation alerts (machine-local `bearing-conversations.json`)
  reckoning  the obligation ledger (private host, `<private store>/obligations`)
  beacons    beacons and sightings (`priority/items.md`)
  tickets    every project's `tickets/<ID>.md` on this machine

Also the `conv_answered` check kind: Lukas wrote in a conversation after a given time (what
Bearing's alert memory records). Labels are ids and short hashes only: safe in any session.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from typing import Dict, Iterable, List, Optional

try:
    from lib import outcome
except ImportError:  # pragma: no cover
    import outcome  # type: ignore

Item, Result, Stale, Store = outcome.Item, outcome.Result, outcome.Stale, outcome.Store


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]


def _decode(raw) -> tuple:
    """(spec or None, note) from a stored check (dict, JSON text or empty)."""
    try:
        spec = outcome.decode(raw)
    except ValueError as e:
        return None, "invalid check: %s" % e
    if spec is None:
        return None, ""
    try:
        return outcome.validate_spec(spec, trusted=bool(spec.get("trusted"))), ""
    except ValueError as e:
        return None, "invalid check: %s" % e


# --------------------------------------------------------------------------- conv_answered

def _conv_answered(spec: Dict, limit: float) -> Result:
    from lib import bearingalerts
    try:
        rec = bearingalerts._load(bearingalerts.state_path())["convs"].get(spec["conversation"])
    except Exception as e:  # noqa: BLE001
        return Result(outcome.UNKNOWN, "conversation memory unreadable: %s" % type(e).__name__)
    if not rec:
        return Result(outcome.UNKNOWN, "conversation not in this machine's memory")
    after = datetime.fromisoformat(spec["after"])
    mine = rec.get("last_self") or ""
    try:
        if mine and datetime.fromisoformat(mine) > after:
            return Result(outcome.SATISFIED, "Lukas wrote in the conversation at %s" % mine)
    except ValueError:
        return Result(outcome.UNKNOWN, "conversation memory has a bad timestamp")
    return Result(outcome.NOT_SATISFIED, "no message from Lukas in the conversation since the ask")


# Fact-grade by default (LODE-23): "Lukas wrote in the conversation" is not "the ask is met"
# ("I'll look tomorrow" is a reply). Only a store whose item IS a reply owed lets it close.
outcome.register_check("conv_answered", {"conversation": outcome.is_text, "after": outcome.is_iso},
                       _conv_answered, local=True, closes=False)

#: obligation kinds whose outcome IS a reply from Lukas (needs-you detections)
REPLY_KINDS = ("reply",)


def _reply_grade(item: Item, reply_owed: bool) -> tuple:
    if (item.check or {}).get("type") == "conv_answered":
        return ((True, "") if reply_owed else
                (False, "a reply in the conversation does not complete a %s" % (item.raw or "task")))
    return outcome.closure_grade(item.check)


# --------------------------------------------------------------------------- alerts

class AlertsStore(Store):
    name = "alerts"
    trusted_cmd_ok = True        # machine-local file; a cmd spec arrives only via --trusted-cmd
    intrinsic = "closes on Lukas's later reply in the same conversation"

    def __init__(self, path: Optional[str] = None):
        self.path = path
        self._pending: List = []

    def _p(self) -> str:
        from lib import bearingalerts
        return self.path or bearingalerts.state_path()

    def items(self, include_closed: bool = False) -> Iterable[Item]:
        from lib import bearingalerts
        convs = bearingalerts._load(self._p())["convs"]
        for conv, rec in convs.items():
            live = bool(rec.get("alerts"))
            if not live and not (include_closed and rec.get("closed_at")):
                continue
            spec, note = _decode(rec.get("check"))
            stamp = bearingalerts._stamp(rec)
            yield Item(self.name, conv, "open" if live else "closed", live, spec,
                       json.dumps(list(stamp)), True,
                       "alert %s/%s" % (rec.get("source") or "?", _h(conv)), stamp, note)

    def owns(self, item_id: str) -> bool:
        return ":" in item_id and not item_id.startswith(("ob_", "beacon:"))

    def lookup(self, item_id: str) -> Optional[Item]:
        for it in self.items(include_closed=True):
            if it.id == item_id:
                return it
        return None

    def close(self, item: Item, result: Result, by: str) -> str:
        from lib import bearingalerts
        lines = bearingalerts.apply_verdicts([(item.id, item.raw, result)], self._p())
        if not lines:
            raise Stale("%s changed since its check ran" % item.label)
        return "closed %s: %s" % (item.label, result.reason)

    def record(self, item: Item, result: Result, now=None) -> None:
        outcome.record_verdict(self.name, item.id, item.check, result, now)
        self._pending.append((item.id, item.raw, result))  # also onto the alert (flush)

    def flush(self) -> List[str]:
        if self._pending:
            from lib import bearingalerts
            bearingalerts.apply_verdicts(self._pending, self._p())
            self._pending = []
        return []

    def may_close(self, item: Item) -> tuple:
        return _reply_grade(item, True)     # an alert IS "a reply is owed here"

    def attach(self, item_id: str, spec: Dict, trusted: bool = False) -> str:
        from lib import bearingalerts
        n = bearingalerts.attach_check("", spec, self._p(), conv_id=item_id, trusted=trusted)
        if not n:
            raise outcome.Refused("no open alert %s" % item_id)
        return "attached to alert %s" % _h(item_id)


# --------------------------------------------------------------------------- reckoning

def _voided(spec: Optional[Dict], rec: Dict) -> Optional[Dict]:
    """A "that reply didn't finish it" correction (`void_before`) voids a reply
    before that time: a conv_answered check then needs a reply after it."""
    vb = rec.get("void_before") or ""
    if spec and spec.get("type") == "conv_answered" and vb:
        try:
            if datetime.fromisoformat(vb) > datetime.fromisoformat(spec["after"]):
                return dict(spec, after=vb)
        except (ValueError, TypeError):
            return spec
    return spec


class ReckoningStore(Store):
    name = "reckoning"
    done_states = ("done",)

    def __init__(self, root: Optional[str] = None):
        self.root = root
        self._state = None

    def _fold(self):
        from lib import reckoning as rk
        if self._state is None:
            self._state = rk.fold(rk.read_events(rk.store_root(self.root))[0])
        return self._state

    def items(self, include_closed: bool = False) -> Iterable[Item]:
        from lib import reckoning as rk
        self._state = None
        for ob, rec in self._fold().items():
            live = rec["state"] in ("proposed", "open")
            if not live and not include_closed:
                continue
            spec, note = _decode(rec.get("check"))
            yield Item(self.name, ob, rec["state"], live, _voided(spec, rec), rk.version(rec),
                       live, "obligation %s" % ob[:12], rec.get("kind") or "", note)

    def owns(self, item_id: str) -> bool:
        return item_id.startswith("ob_")

    def lookup(self, item_id: str) -> Optional[Item]:
        from lib import reckoning as rk
        self._state = None
        try:
            rec = rk.resolve(self._fold(), item_id)
        except rk.Refused:
            return None
        spec, note = _decode(rec.get("check"))
        live = rec["state"] in ("proposed", "open")
        return Item(self.name, rec["ob"], rec["state"], live, _voided(spec, rec), rk.version(rec),
                    live,
                    "obligation %s" % rec["ob"][:12], rec.get("kind") or "", note)

    def may_close(self, item: Item) -> tuple:
        return _reply_grade(item, item.raw in REPLY_KINDS)

    def close(self, item: Item, result: Result, by: str) -> str:
        from lib import reckoning as rk
        try:
            rk.close_if_satisfied(item.id, item.version, by,
                                  "outcome check satisfied: %s" % result.reason, self.root)
        except rk.Refused as e:
            raise Stale(str(e))
        self._state = None
        return "done %s: %s" % (item.label, result.reason)

    def attach(self, item_id: str, spec: Dict) -> str:
        from lib import reckoning as rk, machine
        spec = outcome.validate_spec(spec)
        rk.transition(item_id, "amend", "%s/outcome" % machine.key(), root=self.root, check=spec)
        return "check attached to obligation %s" % item_id[:12]


# --------------------------------------------------------------------------- beacons

class BeaconsStore(Store):
    name = "beacons"
    done_states = ()   # a closed beacon may be his word on a watch, not proof of an outcome

    def __init__(self, path: Optional[str] = None, sync: bool = True, push: bool = True):
        self.path = path
        self.sync = sync and path is None
        self.push = push
        self._closed = 0

    def items(self, include_closed: bool = False) -> Iterable[Item]:
        from lib import priority
        for it in priority.load(self.path):
            live = it.status == priority.OPEN
            if not live and not include_closed:
                continue
            spec, note = _decode(it.check)
            yield Item(self.name, it.id, it.status, live, spec, priority.version(it), live,
                       "%s %s" % ("beacon" if it.sticky else "sighting", it.id), None, note)

    def owns(self, item_id: str) -> bool:
        return item_id.count("-") >= 2 and not item_id.startswith("ob_")

    def close(self, item: Item, result: Result, by: str) -> str:
        from lib import priority
        r = priority.close_if(item.id, "outcome check satisfied (%s): %s" % (by, result.reason),
                              item.version, self.path)
        if not r.get("ok"):
            raise Stale(r.get("reason") or "not closed")
        self._closed += 1
        return "closed %s: %s" % (item.label, result.reason)

    def attach(self, item_id: str, spec: Dict) -> str:
        from lib import priority
        r = priority.set_check(item_id, spec, self.path)
        if not r.get("ok"):
            raise outcome.Refused(r.get("reason") or "not attached")
        return r["reason"]

    def flush(self) -> List[str]:
        if not (self._closed and self.sync):
            return []
        self._closed = 0
        from lib import gitutil, paths, selfsync
        if not self.push:  # commit by pathspec only; the next push carries it
            repo = paths.root()
            if gitutil.add_paths(repo, ["priority/items.md"]).code == 0:
                gitutil.commit(repo, "priority: outcome-check close", paths=["priority/items.md"])
            return []
        ok, note = selfsync.commit_and_push("priority: outcome-check close",
                                            paths_to_add=["priority/items.md"])
        return [] if ok else ["beacons: %s" % note]


# --------------------------------------------------------------------------- tickets

class TicketsStore(Store):
    name = "tickets"
    done_states = ("done",)

    def __init__(self, root: Optional[str] = None, push: bool = True, homes=None):
        self.root = root
        self.push = push
        self.homes = homes

    def _homes(self):
        from lib import tickets as tk
        if self.homes is not None:
            return self.homes
        return tk.all_homes(self.root, cached=self.root is None)

    def _item(self, t) -> Item:
        from lib import tickets as tk
        spec, note = _decode(t.get("check"))
        live = t.get("status") in tk.LIVE or t.get("status") == "proposed"
        return Item(self.name, t["id"], t.get("status", ""), live, spec, tk.version(t),
                    t.get("status") in tk.LIVE, "ticket %s" % t["id"], None, note)

    def items(self, include_closed: bool = False) -> Iterable[Item]:
        from lib import tickets as tk
        for t in tk.load(self._homes()):
            it = self._item(t)
            if it.live or include_closed:
                yield it

    def owns(self, item_id: str) -> bool:
        from lib import tickets as tk
        return bool(tk.ID_RE.match(item_id))

    def lookup(self, item_id: str) -> Optional[Item]:
        from lib import tickets as tk
        try:
            t, _ = tk.find(item_id, self.root)
        except tk.Refused:
            return None
        return self._item(t)

    def close(self, item: Item, result: Result, by: str) -> str:
        from lib import tickets as tk
        try:
            tk.move(item.id, "done", evidence="outcome check (%s): %s" % (by, result.reason),
                    root=self.root, push=self.push, expect_version=item.version)
        except tk.Stale as e:
            raise Stale(str(e))
        except tk.Refused as e:
            raise outcome.Refused(str(e))
        return "done %s: %s" % (item.label, result.reason)

    def attach(self, item_id: str, spec: Dict) -> str:
        from lib import tickets as tk
        spec = outcome.validate_spec(spec)
        tk.update(item_id, "check %s" % spec["type"], self.root, self.push,
                  check=outcome.encode(spec))
        return "check attached to ticket %s" % item_id


outcome.register_store_factory("alerts", AlertsStore)
outcome.register_store_factory("reckoning", ReckoningStore)
outcome.register_store_factory("beacons", BeaconsStore)
outcome.register_store_factory("tickets", TicketsStore)

# Tender's check kinds (`tender_receipt`, `tender_recovered`) register with its harvest module.
try:
    from lib import tenderharvest  # noqa: F401,E402
except ImportError:  # pragma: no cover
    import tenderharvest  # type: ignore  # noqa: F401,E402
