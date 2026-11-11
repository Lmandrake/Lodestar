"""Item names: `PROJECT _ VERB _ INTENTION _ TWIST`, 4-9 words, <= 64 chars, `A-Z 0-9 _` only.

The grammar's home on this fleet, for EVERY item class (tickets, review findings, decisions,
experiments, lanes, waves, helpers, agents, phases), not just tickets. Fleet rule:
`~/dev/Lodestar/claude/global/27-named-not-numbered.md`.

    PROJECT (a registry `Id` or `Name`, slot 1) _ VERB (slot 2, FIXED INDEX, free-form) _
    INTENTION (what it set out to do when filed, never live state) _
    TWIST (a true, specific, kind aside about how it is going; true as of filing).

    LODESTAR_WIRE_DESKRUN_WAYPOINT_NAMES_RESOLVE_REMOTELY
    WAYPOINT_UNSTICK_BRIDGE_THIRD_TIME_LUCKY
    LODESTAR_HARDEN_TENDERSLOTS_DELETED_STATE_RESETS_HIGH_WATER

🔴 **THE VERB IS FREE-FORM AND FOUND BY POSITION. THERE IS NO WORD BANK.** Owner's decision,
2026-11-05, after three rounds of measurement that each killed an alternative:

- **A word bank was the OLD design and is retired.** The 72-word `INTENT_WORDS` bank scattered
  one concept over up to 9 synonyms (repair = FIX/HARDEN/REPAIR/UNSTICK/CORRECT/MEND/...), 24 of
  its 72 words were never used once in 231 real names, and **no tool ever consumed the slot**.
  The "greppable by intent" claim was tested and FAILED. So any verb the filer types is accepted
  and the bank is gone from the gate; `LEGACY_INTENT_WORDS` below survives ONLY to keep reading
  the 231 names minted before this date, never to refuse a new one.
- **Finding the verb BY POSITION only works because slot 2 is pinned.** An earlier attempt let
  the subject float 1-4 words, which left the verb at no fixed index at all. Never reintroduce a
  variable-length subject ahead of the verb.
- **Marking the verb BY SHAPE (a gerund `-ING`) was tested and failed:** `BEARING`, `KEYRING`,
  `RULING`, `RECKONING`, `ROUTING`, `SENSING` are real subject nouns in this fleet — 19 collisions.

🔴 **WHAT SLOT 1 IS FOR: telling an item id from a code constant.** `is_named` is what
`tickets.ID_RE` uses to spot an id in free text, so a name shaped like `CACHE_MAX_AGE_S` must not
read as one. MEASURED 2026-11-05 over the 4-9-word UPPER_SNAKE tokens in `Lodestar/bin/**/*.py`
and `Machinist/tools/**/*.py` as they stood before this change (99 tokens, 87 of them not an
existing ticket id):

    rule                                 flagged   of which a real code identity
    bank (retired)                        7/87      2   (CLAUDE_CODE_SESSION_ID, ..._PROBE_SSID)
    project anchor alone                  9/87      6
    project anchor + legacy compat       11/87      7   <- what ships

⚠️ **The anchor measured WORSE on this axis, not better; the 19/99 -> 5/99 improvement predicted
for it does not reproduce.** The bank's misreads were accidents of vocabulary; the anchor's are
systematic, because an env-var key and a module constant both legitimately begin with their
project's name (`LODESTAR_MAIL_DUMP_DIR`, `FETCHER_STUCK_DEFAULT_S`, `LODESTAR_NETWATCH_SPEED_URL`).
Every straggler has an obvious NOUN in slot 2 where the grammar wants a verb — which a human sees
instantly and this function cannot. So the honest case for slot 1 is the owner's own one:
traceability to a parent, and no two same-named items in different projects. It is not a better
oracle. A tool that needs certainty should read the home's file list, not a regex over prose.

🔴 **WHY 64 AND NOT 44.** The owner asked (2026-11-05) that every name carry its parent project,
so an item is always traceable and can never be conflated with a same-named one elsewhere.
MEASURED that day: prepending the registry `Id` to the then-211 named tickets moved the median
from 36 chars to 47 and the max from 44 to 57, so 137 of 211 would have been refused by the old
cap. A real review-finding name runs 50. The cap is 64 so the project slot is affordable; the
word range widened by one at each end to match.

**Non-project items** mint an obvious throwaway slot on the fly — `PROJNOW` or `PROJTEMP`
(owner: "so it's very clear what you're doing"). Both are always-valid slot-1 values. Reuse the
SAME slot for the same topic rather than a fresh one each time: `throwaway_slot(topic)` /
`remember_throwaway(topic, slot)` keep that mapping in machine-local state.

**Legacy ids** (`LODE-35`, `D31`, `E3b`, `B58`, anything ending `_<digits>`) stay valid forever
and are never renamed — but are never cited bare either (owner, 2026-11-05): always with a
plain-language title attached, `D31 (any session may close an obligation)`.
"""
import json
import os
import re
from typing import Dict, Iterable, List, Optional, Tuple

#: The throwaway slot-1 values, valid on every machine, for an item that belongs to no
#: registered project. Deliberately ugly so a reader can see at a glance that it is not a project.
THROWAWAY_SLOTS = ("PROJNOW", "PROJTEMP")

#: 🔴 NOT A GATE AND NEVER AGAIN ONE. The retired 2026-10-10 word bank, kept only so `is_named`
#: can still recognise the 231 names minted under the old `SUBJECT_INTENT_TWIST` rule (their
#: slot 1 is a subject noun, not a registry Id, so the project anchor cannot see them). Frozen:
#: do not add a word, and never consult it when minting. `namecheck` may offer it as a
#: suggestion; `problems()` must not.
LEGACY_INTENT_WORDS = frozenset("""
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
MAX_LEN = 64
MIN_WORDS, MAX_WORDS = 4, 9       # what a NEW name must be: PROJECT VERB INTENTION TWIST
LEGACY_MIN_WORDS = 3              # what `is_named` still READS: the pre-2026-11-05 names
LEGACY_MAX_LEN = 44               # ditto: the old cap
SHAPE = re.compile(r"^[A-Z0-9]+(?:_[A-Z0-9]+)*$")
LEGACY = re.compile(r"^[A-Z][A-Z0-9]{1,5}-(?:\d+|x[a-z2-7]{5})$")      # LODE-35, ALP-xab23c

_SLOT_CACHE = {}  # type: Dict[str, object]


def _slot(repo_id: str) -> str:
    """A registry `Id` or `Name` in slot 1: one word, so punctuation is squashed out.

    `my-notes` -> `MYNOTES`. A `MY_NOTES` prefix is NOT accepted, because slot 1 is one
    word by definition and splitting it there would put the verb at index 2.
    """
    return re.sub(r"[^A-Z0-9]", "", (repo_id or "").upper())


def slot_for(repo_id: str) -> str:
    """The slot-1 spelling of a registry `Id`, for a tool templating a name: `lodestar` ->
    `LODESTAR`. Returns "" when that id is not in the registry, so a caller cannot invent one."""
    one = _slot(repo_id)
    return one if one in project_slots() else ""


def project_slots() -> frozenset:
    """Every valid slot-1 value: registry `Id` and `Name`, plus the throwaway slots.

    A current Name is accepted alongside the stable Id: RimMandrake can mint RIMMANDRAKE
    while its Id stays rimmaster and existing RIMMASTER names keep working without renames.

    Read from `sync/registry.md` through `lib.registry` — never a hardcoded list of the 77 names,
    because a project added to the registry must become nameable without editing this file. Cached
    on the registry's (path, mtime, size) so `problems()` is not a file read per call; an
    unreadable registry falls back to the throwaway slots alone rather than guessing.
    """
    try:
        from . import paths, registry
    except ImportError:  # pragma: no cover - direct-import fallback, as elsewhere in bin/lib
        import paths     # type: ignore
        import registry  # type: ignore
    path = paths.registry_file()
    try:
        st = os.stat(path)
        # mtime_ns, not mtime: two writes inside one second are routine in a test that rewrites
        # the registry, and a 1-second key would serve the first read's answer to the second.
        key = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        key = (path, 0, 0)
    if _SLOT_CACHE.get("key") != key:
        try:
            found = {_slot(value) for e in registry.entries() if e.repo_id
                     for value in (e.repo_id, e.name)}
        except Exception:  # noqa: BLE001 - a naming check must never die on a registry fault
            found = set()
        _SLOT_CACHE["key"] = key
        _SLOT_CACHE["slots"] = frozenset(found | set(THROWAWAY_SLOTS)) - {""}
    return _SLOT_CACHE["slots"]  # type: ignore[return-value]


def parse(name: str) -> Optional[Tuple[str, str, List[str], List[str]]]:
    """`(project, verb, intention_words, twist_words)` by FIXED INDEX, or None if unparseable.

    Slots 1 and 2 are the only machine-knowable ones: project is word 0, verb is word 1, always.
    ⚠️ The intention/twist boundary is **editorial, not parseable** — nothing in the grammar marks
    it, and nothing in the fleet consumes it. The split returned here (everything up to the last
    word is the intention, the last word is the twist) is a DISPLAY convention for `namecheck`;
    never branch a tool on slots 3 and 4.
    """
    words = str(name or "").split("_")
    if len(words) < MIN_WORDS:
        return None
    return words[0], words[1], words[2:-1], words[-1:]


def _reads_as_old_form(name: str) -> bool:
    """True for a name the retired `SUBJECT_INTENT_TWIST` rule would have minted.

    The compatibility path, and the ONLY surviving use of `LEGACY_INTENT_WORDS`: the 231 names
    filed before 2026-11-05 carry a subject noun in slot 1, so `project_slots()` cannot see them,
    and `tickets.ID_RE` has to keep resolving every one of them or citations, `Closes:` trailers
    and `_ticket_files()` all break at once. Held to the OLD bounds (3-7 words, <= 44 chars) on
    purpose, so it cannot quietly become a second way to mint a new name.
    """
    words = name.split("_")
    if not LEGACY_MIN_WORDS <= len(words) <= 7 or len(name) > LEGACY_MAX_LEN:
        return False
    return any(words[i] in LEGACY_INTENT_WORDS for i in range(1, len(words) - 1))


def is_named(name: str) -> bool:
    """True when `name` is an item id rather than a code constant or a legacy `CODE-n`.

    The load-bearing consumer is `tickets.ID_RE` (`bin/lib/tickets.py`), which uses this to spot
    ids in free text — `blocked_by` entries, `Closes:` trailers, ticket filenames. So it accepts
    BOTH the current rule (a known slot-1 project) and the retired one (`_reads_as_old_form`).
    """
    s = str(name or "")
    if not SHAPE.match(s) or LEGACY.match(s):
        return False
    words = s.split("_")
    if not LEGACY_MIN_WORDS <= len(words) <= MAX_WORDS or len(s) > MAX_LEN:
        return False
    if not re.search(r"[A-Z]", words[-1]):
        return False
    return words[0] in project_slots() or _reads_as_old_form(s)


def problems(name: str, known: Iterable[str] = ()) -> List[str]:
    """Why `name` may not be minted as a NEW item id; `[]` means it may.

    🔴 The verb in slot 2 is never checked against a vocabulary. A refusal here is only ever
    about shape, length, a name already taken, a vague or mean word, or a slot 1 that is not a
    registry `Id` or `Name` (or `PROJNOW`/`PROJTEMP`).
    """
    s = str(name or "")
    if s in set(known):
        return ["%s already exists: give the new item its own name" % s]
    if not SHAPE.match(s):
        return ["only UPPER_SNAKE ASCII: A-Z, 0-9 and single underscores"]
    out = []
    words = s.split("_")
    if not MIN_WORDS <= len(words) <= MAX_WORDS:
        out.append("%d words; a name has %d-%d words: PROJECT_VERB_INTENTION_TWIST"
                   % (len(words), MIN_WORDS, MAX_WORDS))
    if len(s) > MAX_LEN:
        out.append("%d chars; too long, the cap is %d" % (len(s), MAX_LEN))
    if not re.search(r"[A-Z]", words[-1]):
        out.append("last word is a bare number: numbered ids are the retired form")
    banned = sorted(set(words) & BANNED_WORDS)
    if banned:
        out.append("vague or mean word(s) %s: a twist must be specific and kind" % ", ".join(banned))
    slots = project_slots()
    if words[0] not in slots:
        near = sorted(p for p in slots if p.startswith(words[0][:4]) and p not in THROWAWAY_SLOTS)
        out.append("slot 1 %r is not a project: it must be a registry Id or Name from sync/registry.md%s, "
                   "or %s for something that belongs to no project"
                   % (words[0], (" (did you mean %s?" % ", ".join(near[:3])) + ")" if near else "",
                      " / ".join(THROWAWAY_SLOTS)))
    return out


# --------------------------------------------------------------------------- throwaway slots
# Small and machine-local by design: this is a convenience so a later session reuses PROJNOW for
# the topic it already used PROJNOW for, not a shared registry. It never gates a name.

def _throwaway_path() -> str:
    try:
        from . import paths
    except ImportError:  # pragma: no cover
        import paths     # type: ignore
    return paths.state_file("itemnames", "throwaway-slots.json")


def _topic_key(topic: str) -> str:
    return "-".join(w for w in re.split(r"[^a-z0-9]+", (topic or "").lower()) if w)


def throwaway_slot(topic: str) -> str:
    """The throwaway slot already used for `topic`, or "" when the topic is new."""
    key = _topic_key(topic)
    if not key:
        return ""
    try:
        with open(_throwaway_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return ""
    value = data.get(key) if isinstance(data, dict) else None
    return value if value in THROWAWAY_SLOTS else ""


def remember_throwaway(topic: str, slot: str = THROWAWAY_SLOTS[0]) -> str:
    """Bind `topic` to a throwaway slot (first writer wins) and return the bound slot."""
    key = _topic_key(topic)
    if not key or slot not in THROWAWAY_SLOTS:
        return ""
    path = _throwaway_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    if data.get(key) in THROWAWAY_SLOTS:
        return data[key]
    data[key] = slot
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, sort_keys=True, indent=1)
    os.replace(tmp, path)
    return slot
