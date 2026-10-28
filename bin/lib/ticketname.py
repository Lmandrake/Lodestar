"""Ticket names: SUBJECT_INTENT_TWIST. Adopted from RimMandrake (2026-10-10 policy, adopted here
2026-10-25 at the owner's request). Grammar source of truth for Rim:
RimMandrake's `design/RimMandrake/ticket_naming_2026-10-10.md`; this is a copy of its
`model.py` rules, so keep the two in step.

    SUBJECT (1-4 words, greppable) _ INTENT (one bank word, what it set out to do) _ TWIST (a true,
    specific, kind aside about how it is going).  3-7 words, <= 44 chars, A-Z 0-9 _ only.

The intent records what the ticket set out to do when filed, never live state; the twist is true
as of filing. Legacy `LODE-35` ids stay valid forever and are never renamed.
"""
import re
from typing import Iterable, List, Optional, Tuple

INTENT_WORDS = frozenset("""
    AUDIT CENSUS MEASURE REMEASURE PROBE PROVE VERIFY VET JUDGE BENCHMARK BISECT
    DEBUG DIAGNOSE HUNT CHASE
    BUILD CODE WIRE REWIRE FILL POPULATE INSTALL DEPLOY FINISH POLISH WIDEN SHRINK
    FIX MEND REPAIR UNSTICK UNTANGLE UNBLOCK HARDEN ENFORCE SILENCE CORRECT
    RECONCILE SYNC UNIFY MERGE DEDUPE
    EXCISE CULL PRUNE DELETE RETIRE
    SALVAGE RESCUE RESTORE REVIVE ABSORB MIGRATE RENAME RESKIN REDRAW
    DRAIN TAME TUNE BALANCE
    DESIGN REDESIGN RETHINK SKETCH DECIDE ASK HARVEST DOCUMENT EXPLAIN MONITOR
    REBUILD RETRY
""".split())
BANNED_WORDS = frozenset("""
    MISC STUFF THINGS ETC LOL WIP TODO TBD IDK WHATEVER MAYBE FOO
    STUPID DUMB IDIOT MORON LAZY INCOMPETENT PATHETIC
""".split())
MAX_LEN = 44
MIN_WORDS, MAX_WORDS = 3, 7
SHAPE = re.compile(r"^[A-Z0-9]+(?:_[A-Z0-9]+)*$")
LEGACY = re.compile(r"^[A-Z][A-Z0-9]{1,5}-(?:\d+|x[a-z2-7]{5})$")      # LODE-35, ALP-xab23c


def parse(name: str) -> Optional[Tuple[List[str], str, List[str]]]:
    """(subject_words, intent, twist_words); None when there is no hinge. The hinge is the first
    bank word at position >= 1 with at least one word after it."""
    words = str(name or "").split("_")
    for i in range(1, len(words) - 1):
        if words[i] in INTENT_WORDS:
            return words[:i], words[i], words[i + 1:]
    return None


def is_named(name: str) -> bool:
    s = str(name or "")
    return bool(SHAPE.match(s)) and not LEGACY.match(s) and parse(s) is not None


def problems(name: str, known: Iterable[str] = ()) -> List[str]:
    """Why `name` may not be minted as a NEW ticket id; [] means it may."""
    s = str(name or "")
    if s in set(known):
        return ["%s already exists: give the new ticket its own name" % s]
    if not SHAPE.match(s):
        return ["only UPPER_SNAKE ASCII: A-Z, 0-9 and single underscores"]
    out = []
    words = s.split("_")
    if not MIN_WORDS <= len(words) <= MAX_WORDS:
        out.append("%d words; a name has %d-%d words" % (len(words), MIN_WORDS, MAX_WORDS))
    if len(s) > MAX_LEN:
        out.append("%d chars; too long, the cap is %d" % (len(s), MAX_LEN))
    if not re.search(r"[A-Z]", words[-1]):
        out.append("last word is a bare number: numbered ids are the retired form")
    banned = sorted(set(words) & BANNED_WORDS)
    if banned:
        out.append("vague or mean word(s) %s: a twist must be specific and kind" % ", ".join(banned))
    if parse(s) is None:
        if words[0] in INTENT_WORDS:
            out.append("starts with the intent: put the SUBJECT first, then the intent")
        elif words[-1] in INTENT_WORDS:
            out.append("ends on the intent: add a twist after it")
        else:
            out.append("no intent word: needs SUBJECT_INTENT_TWIST, intent from the bank "
                       "(`ticket namecheck --bank`)")
    return out
