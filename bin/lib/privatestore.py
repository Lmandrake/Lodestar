"""Where private text goes instead of Lodestar's public history (ruling 5, 2026-10-13: "Stop
it"). Lodestar keeps ids, counts and pointers; the text lives in the private store, a separate
checkout on the private git host (`lib.boundary`: root and host come from configuration), which
the hourly sweep carries between the machines that may hold it.

* **Beacon evidence.** A verified quote and the judge's note are appended to
  `bearing/evidence/<date>/<machine>.jsonl` there; `priority/items.md` gets only
  `[<tag>:jev_…] — <state>`. `expand` turns a segment back into text for a reader that runs on
  a machine holding the store (the judge prompt), and leaves it as an id anywhere else.
* **Airlock captures.** `mirror` copies a capture file into `lodestar-airlock/` there; the
  Lodestar copy is gitignored, so it never reaches the public remote.

Every write is refused unless every push URL of the checkout is on the private host
(`reckoning.private_only`), so a misconfigured remote fails closed instead of leaking.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import date
from typing import Dict, List, Optional

try:
    from lib import boundary, machine, reckoning
except ImportError:  # pragma: no cover
    import boundary  # type: ignore
    import machine  # type: ignore
    import reckoning  # type: ignore

ROOT_ENV = boundary.ROOT_ENV


def ref_open() -> str:
    """The opening of an evidence reference in a board file: `[<tag>:`."""
    return "[%s:" % boundary.ref_tag()


def ref_re() -> "re.Pattern":
    return re.compile(r"\[%s:(jev_[0-9a-f]{12})\]" % re.escape(boundary.ref_tag()))


class Unavailable(Exception):
    pass


class NoCheckout(Unavailable):
    """No private store on this machine at all: nothing private happens here."""


class Rejected(Unavailable):
    """A checkout exists but could push elsewhere or escapes itself: fail closed, never fall
    back to a Lodestar path."""


def root(path: Optional[str] = None) -> str:
    path = os.path.expanduser(path or boundary.root_setting())
    # The fingerprinted PASS cache (reckoning's safety cache: keyed on every git/ssh config
    # file's stat and GIT_* env, 600 s TTL) first: this was ~4 git spawns per call and ~116 per
    # Bearing cycle, at ~257 ms each under the 2026-10-22 exec tax, and its 20 s timeouts
    # sank whole cycles. A miss runs the live check and remembers only a pass.
    cached = reckoning._cached_top(path) if os.path.isdir(path) else None
    if cached is not None and reckoning._contained(path, cached):
        return path
    top = reckoning.repo_top(path) if os.path.isdir(path) else None
    if top is None:
        raise NoCheckout("no private store checkout at %s" % path)
    if not reckoning.private_only(top):
        raise Rejected("%s may push somewhere other than %s" % (top, reckoning.private_host() or "the private host"))
    reckoning._remember(top)
    if not reckoning._contained(path, top):
        raise Rejected("%s does not stay inside %s" % (path, top))
    return path


def _safe_dir(base: str, directory: str) -> None:
    os.makedirs(directory, exist_ok=True)
    if not reckoning._contained(directory, reckoning.repo_top(base) or base):
        raise Rejected("%s leaves the private checkout (symlink or nested repo)" % directory)


def _norm(text: str) -> str:
    return " ".join((text or "").split())  # whitespace only: case can carry meaning


def evidence_id(beacon_id: str, quote: str, label: str = "match") -> str:
    """Deterministic on (beacon, judgment, quote): the same quote judged the same way twice is
    one piece of evidence, while a changed judgment (new -> cancelled) is a new one."""
    return "jev_" + hashlib.sha256(("%s\x1f%s\x1f%s" % (beacon_id, label, _norm(quote)))
                                   .encode("utf-8")).hexdigest()[:12]


def ref(evid: str) -> str:
    return "%s%s]" % (ref_open(), evid)


def put_evidence(beacon_id: str, quote: str, note: str, event_id: str = "",
                 pointer: str = "", source: str = "", store: Optional[str] = None,
                 machine_key: Optional[str] = None, day: Optional[str] = None) -> str:
    """Record one piece of evidence in the private store; returns its id. Idempotent on the id."""
    base = root(store)
    evid = evidence_id(beacon_id, quote, state_label(note))
    if evid in load_evidence(base):
        return evid
    day = day or date.today().isoformat()
    path = os.path.join(base, "bearing", "evidence", day,
                        "%s.jsonl" % (machine_key or machine.key()))
    _safe_dir(base, os.path.dirname(path))
    record = {"id": evid, "beacon_id": beacon_id, "date": day, "quote": quote, "note": note,
              "event_id": event_id, "pointer": pointer, "source": source}
    with reckoning._locked(path + ".lock"):
        reckoning._repair_torn(path)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    return evid


def load_evidence(store: Optional[str] = None) -> Dict[str, Dict]:
    try:
        base = os.path.expanduser(store) if store else root(None)
    except Unavailable:
        return {}
    folder = os.path.join(base, "bearing", "evidence")
    out: Dict[str, Dict] = {}
    if not os.path.isdir(folder):
        return out
    for dirpath, dirs, names in sorted(os.walk(folder)):
        dirs.sort()
        for name in sorted(names):
            if not name.endswith(".jsonl"):
                continue
            with open(os.path.join(dirpath, name), encoding="utf-8") as fh:
                lines = fh.read().split("\n")
            for line in lines[:-1]:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                out.setdefault(rec.get("id", ""), rec)
    return out


def expand(segment: str, store: Optional[str] = None,
           cache: Optional[Dict[str, Dict]] = None) -> str:
    """`<date>: [<tag>:jev_x] — state` -> `<date>: "quote" — note`, where the store is readable."""
    if not ref_re().search(segment or ""):
        return segment
    known = cache if cache is not None else load_evidence(store)

    def sub(match):
        rec = known.get(match.group(1))
        if not rec:
            return match.group(0)
        return '"%s"%s' % (rec.get("quote", ""), " — %s" % rec["note"] if rec.get("note") else "")

    text = ref_re().sub(sub, segment)
    # The state label after the reference is redundant once the note is back.
    return re.sub(r'(" — [^—]+) — (new obligation|corrected|cancelled|match)$', r"\1", text)


def mirror(src: str, rel: str, store: Optional[str] = None) -> Optional[str]:
    """Copy a Lodestar-local (gitignored) private-text file into `lodestar-airlock/<rel>` in the
    store. Best-effort: returns the destination, or None when there is no store here."""
    norm = os.path.normpath(rel or "")
    if not rel or os.path.isabs(rel) or norm == ".." or norm.startswith(".." + os.sep):
        return None
    try:
        base = root(store)
        dest = os.path.join(base, "lodestar-airlock", norm)
        _safe_dir(base, os.path.dirname(dest))
    except Unavailable:
        return None
    shutil.copy2(src, dest)
    return dest


def migrate_note(beacon_id: str, note: str, store: Optional[str] = None,
                 keep_opening: bool = True) -> str:
    """Move every quoted evidence segment of a note to the store; return the new note. A
    segment with no quote (a hand-typed opening note) is left as written; a quoted one moves
    wherever it sits. `keep_opening` is accepted for old callers and no longer changes that."""
    from lib import priority
    segments = priority.evidence(note)
    if not segments:
        return note
    out = []
    for seg in segments:
        # A quoted segment moves wherever it sits, even as the opening note: position does
        # not make a quote safe. An unquoted opening note (typed by hand) stays.
        m = re.match(r'^(\d{4}-\d{2}-\d{2}): "(.*?)"(.*)$', seg, re.S)
        if not m or ref_re().search(seg):
            out.append(seg)
            continue
        day, quote = m.group(1), m.group(2)
        why = m.group(3).strip()
        why = why[1:].strip() if why.startswith("—") else why
        evid = put_evidence(beacon_id, quote, why, store=store, day=day)
        out.append("%s: %s — %s" % (day, ref(evid), state_label(why)))
    return "; ".join(out)


def state_label(note: str) -> str:
    low = (note or "").lower()
    for label in ("new obligation", "corrected", "cancelled"):
        if low.startswith(label):
            return label
    return "match"
