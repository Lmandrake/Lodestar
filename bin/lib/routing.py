"""Per-project keyword/sender routing, with autonomous list maintenance.

See docs/superpowers/specs/2026-10-08-bearing-cycling-design.md's Routing section. One central
registry (priority/routing.md) rather than scattering a keyword list into every project's own
loop file — simpler to maintain for the same reason sync/registry.md is one file, not one per
machine.

List maintenance is autonomous and NOT approval-gated (Lukas's explicit ruling, "ask if you NEED
to ask, but if you don't... don't"), with one guardrail that IS mandatory: a rule only gets added
after the SAME distinct message has recurred more than once for the same project — a single
coincidence is not a pattern — and every add is appended to routing-audit.md so a misfire is
cheaply discoverable after the fact even though it was never going to be pre-approved.

Two separate matching mechanisms, not one, following a 2026-10-08 ChatGPT review
(ledger `20261008T231723-Lodestar-code_review`, confirmed/critical) that caught a real bug in
an earlier version of this file: a learned SENDER ADDRESS was being written into the same
substring-matched `keywords` list used for free body text. An email address is not a safe
substring key — `ann@example.com` as a "keyword" matches `joann@example.com`, matches
`ann@example.com.evil`, and matches any body text that merely quotes the address regardless of
who actually sent it. Senders are therefore matched by exact (lowercased) equality against a
separate `senders` column, never by substring.

**Known, accepted limitation, not fixed here (same review, lower-priority findings):**
"exactly one project registered" removes destination ambiguity but does not establish that a
repeated sender is actually relevant to that project — two unrelated personal emails from the
same address would still auto-add under the one-project rule. This is the trade-off Lukas's own
ruling accepts (speed over a gate); it is not silently patched over. Also: removing a learned
sender from `routing.md` by hand does NOT reset its seen-count in `.routing-unfiled.json`, so an
immediately-following message from the same sender re-adds it. To truly suppress a learned
rule, delete its entry from `.routing-unfiled.json` as well as from `routing.md`.
"""

import json
import os
import re
from datetime import datetime, timezone
from typing import Dict, List, NamedTuple, Optional, Tuple

from . import boundary

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_PATH = os.path.join(REPO_ROOT, "priority", "routing.md")
DEFAULT_AUDIT_PATH = os.path.join(REPO_ROOT, "priority", "routing-audit.md")
DEFAULT_UNFILED_PATH = os.path.join(REPO_ROOT, "priority", ".routing-unfiled.json")

_ROW_RE = re.compile(
    r"^\|\s*(?P<project>[^|]+?)\s*\|\s*(?P<dir>[^|]+?)\s*\|\s*(?P<keywords>[^|]*?)\s*\|"
    r"\s*(?P<senders>[^|]*?)\s*\|\s*$")


class Project(NamedTuple):
    name: str
    dir: str
    keywords: Tuple[str, ...]
    senders: Tuple[str, ...]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def _has_term(haystack: str, term: str) -> bool:
    """`term` (already normalized) occurs in `haystack` as a whole word or phrase: not preceded
    or followed by a letter or digit. Substring matching made `req` hit "required", `esto` hit
    "restore" and `pillar` hit "Caterpillar" — measured 2026-10-09, word boundaries halved the
    beacon candidates on a live cycle (51 -> 27). The same rule lives in routing.py and
    beaconmatch.py on purpose; change both."""
    if not term:
        return False
    return re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(term), haystack) is not None


def _norm_sender(address: str) -> str:
    """Senders are matched by exact equality, not substring, but still case-folded — real-world
    mail addresses are near-universally treated case-insensitively even though RFC 5321 §2.4
    technically permits a case-sensitive local part. Does NOT strip plus-tags or a leading
    `Name <...>` wrapper: `mail_triage.py`'s JSON already supplies a bare address
    (`sender_email`), so there is nothing to strip here, and guessing a display-name-stripping
    rule for a shape this data never actually has would be unverified complexity."""
    return (address or "").strip().lower()


#: Sender addresses are private text (colleagues' addresses), and routing.md is in Lodestar's
#: github.com history. Since ruling 5 (2026-10-13) learned senders, exclusions naming work
#: colleagues and the auto-add audit live in this private-store overlay, beside the same tables'
#: shape; `load_projects` / `load_people_exclusions` union it into the default table.
OVERLAY_NAME = "routing.md"
OVERLAY_AUDIT_NAME = "routing-audit.md"


def overlay_path(name: str = OVERLAY_NAME) -> Optional[str]:
    """`<private store>/projects/Lodestar/<name>`, or None where there is no private store."""
    from . import privatestore, proseguard
    try:
        return os.path.join(proseguard.store_dir("", None), name)
    except privatestore.Unavailable:
        return None


def _parse_rows(path: str) -> List["Project"]:
    projects = []
    if not path or not os.path.exists(path):
        return projects
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = _ROW_RE.match(line.rstrip("\n"))
            if not m:
                continue
            project, pdir = m.group("project"), m.group("dir")
            if project.lower() == "project" or set(project) <= {"-"}:
                continue
            kw = tuple(k.strip() for k in m.group("keywords").split(",") if k.strip())
            senders = tuple(s.strip() for s in m.group("senders").split(",") if s.strip())
            projects.append(Project(name=project, dir=pdir, keywords=kw, senders=senders))
    return projects


def _with_overlay(projects: List["Project"]) -> List["Project"]:
    extra = {p.name: p for p in _parse_rows(overlay_path() or "")}
    out = []
    for p in projects:
        o = extra.get(p.name)
        if o is None:
            out.append(p)
            continue
        out.append(p._replace(
            keywords=p.keywords + tuple(k for k in o.keywords if k not in p.keywords),
            senders=p.senders + tuple(s for s in o.senders if s not in p.senders)))
    return out


def load_projects(path: Optional[str] = None) -> List[Project]:
    """Parse the `| project | dir | keywords | senders |` mdtable. Header/separator rows are
    skipped by requiring the project column to not be the literal header word or a `---`
    separator — same shape every other Lodestar mdtable parser uses. The default table is
    unioned with the private overlay (`overlay_path`); an explicit `path` is read alone."""
    if path is None or os.path.realpath(path) == os.path.realpath(DEFAULT_PATH):
        return _with_overlay(_parse_rows(DEFAULT_PATH if path is None else path))
    path = path or DEFAULT_PATH
    projects = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = _ROW_RE.match(line.rstrip("\n"))
            if not m:
                continue
            project, pdir = m.group("project"), m.group("dir")
            if project.lower() == "project" or set(project) <= {"-"}:
                continue
            kw = tuple(k.strip() for k in m.group("keywords").split(",") if k.strip())
            senders = tuple(s.strip() for s in m.group("senders").split(",") if s.strip())
            projects.append(Project(name=project, dir=pdir, keywords=kw, senders=senders))
    return projects


def match(text: str, projects: List[Project]) -> Optional[str]:
    """First project (in list order) whose keyword list has a case-insensitive SUBSTRING hit
    against free text (subject/body). Never used for sender addresses — see `match_sender`."""
    haystack = _norm(text)
    for project in projects:
        if any(_has_term(haystack, _norm(kw)) for kw in project.keywords):
            return project.name
    return None


def match_sender(sender: Optional[str], projects: List[Project]) -> Optional[str]:
    """First project whose `senders` list has an EXACT (case-folded) match. Exact, not
    substring, is the whole point — see the module docstring for why a substring match on an
    address is unsafe."""
    if not sender:
        return None
    needle = _norm_sender(sender)
    for project in projects:
        if needle in (_norm_sender(s) for s in project.senders):
            return project.name
    return None


#: Source → data-boundary class. The private class tag and the store's row name come from
#: `lib.boundary` (deployment data). Anything unknown is private-class: an unclassified source
#: must fail toward the private host, never toward a github.com destination.
PRIVATE = boundary.private_class()
STORE_ROW = boundary.store_name()
SOURCE_CLASS = {"mail": PRIVATE, "slack": PRIVATE, "teams": PRIVATE, "calendar": PRIVATE,
                # Meeting transcripts are private-class (2026-10-14); a therapy recording never
                # becomes an item at all (bin/bearing.py `_PLAUD_PRIVATE`).
                "plaud": PRIVATE,
                "imessage": "personal", "gmail": "personal",
                # A Kairos action the one-time Reckoning import classed as personal (ruling 4,
                # 2026-10-13): it reaches Hestia only through her door, never the private ledger.
                "kairos-personal": "personal"}

#: Where an item lands when no project row matches: the private store's own row. iMessage
#: there is Lukas's 2026-10-09 ruling ("Goes to my personal ... data repo"): a colleague texting
#: on a personal channel must not default onto Hestia's github.com remote. A genuinely personal
#: iMessage lands in the store too — accepted rough edge, see docs/bearing/SKILL.md.
DEFAULT_BY_SOURCE = {s: STORE_ROW for s in ("mail", "slack", "teams", "calendar", "imessage",
                                             "gmail", "plaud")}

#: Personal-class sources whose content may never be written into a github.com repo's tree
#: (Lukas, 2026-10-15: Gmail "just gets routed to my personal folder", read every cycle; still
#: never github.com). They default to the store like iMessage, a project row on a github.com
#: checkout files through the store's overlay (`filing_dir`), and Hestia's tracked tree
#: refuses them (`accepts`). Hestia's gitignored inbox door (`bearingfile.hestia_drop`,
#: `door=True`) still takes their extracted actions, the same way it takes iMessage's.
NEVER_GITHUB = ("gmail",)
DEFAULT_ROWS = ("Hestia", STORE_ROW)

#: Destinations that only accept some source classes, by directory. Hestia's remote is
#: github.com, so no private-class item may be written anywhere under it (docs/bearing/SKILL.md,
#: "Boundary, enforced on every write"). The private store accepts both classes by his ruling.
HESTIA_DIR = "~/dev/Hestia"
BOUNDED_DIRS = ((HESTIA_DIR, ("personal",)),)


class Resolution(NamedTuple):
    filed_to: str
    reason: str
    associations: Tuple[str, ...]
    blocked: Tuple[str, ...]


def source_class(source: str) -> str:
    return SOURCE_CLASS.get(source, PRIVATE)


def default_for(source: str) -> str:
    return DEFAULT_BY_SOURCE.get(source, STORE_ROW)


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def _under(path: str, root: str) -> bool:
    path, root = _real(path), _real(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def allowed_classes(dir_path: str) -> Tuple[str, ...]:
    for root, classes in BOUNDED_DIRS:
        if _under(dir_path, root):
            return classes
    return (PRIVATE, "personal")


def accepts(dir_path: Optional[str], source: str, door: bool = False) -> bool:
    """May an item from `source` be written into `dir_path`? Used by EVERY writer. `door` is
    Hestia's gitignored inbox (`bearingfile.hestia_drop`), the one Hestia path a NEVER_GITHUB
    source may use."""
    if not dir_path:
        return True
    if source in NEVER_GITHUB and not door and _under(dir_path, HESTIA_DIR):
        return False
    return source_class(source) in allowed_classes(dir_path)


def work_metadata_paths() -> Tuple[str, str]:
    """(sync/registry.md, sync/ticket-homes.md) of the board: the project metadata that already
    says which projects are workplace work. A function so tests can point it at fixtures."""
    from . import paths
    return paths.registry_file(), os.path.join(paths.root(), "sync", "ticket-homes.md")


def is_work_project(dir_path: Optional[str]) -> bool:
    """Is the project at `dir_path` workplace work, by existing metadata rather than a list in
    code? True when its registry row's ticket home is `overlay` (ticket-homes.md: "workplace work on a
    github.com repo", ruling 5), following `redirect:` rows. A project on the private host needs
    no flag: `filing_dir` already writes it in place. Owner decision 2026-10-27: an iMessage goes
    by what its matched project is, not by its channel (IMESSAGE_ROUTING_DECIDE_PROVENANCE_POLICY).
    Unreadable metadata answers True: fail toward the private store."""
    if not dir_path:
        return False
    from . import mdtable, registry
    reg, homes = work_metadata_paths()
    try:
        want = _real(dir_path)
        names = [e.name for e in registry.entries(reg) if _under(want, e.path)]
        if not names:
            return False
        with open(homes, encoding="utf-8") as fh:
            rows = {(r.get("name") or "").strip(): (r.get("home") or "").strip()
                    for r in mdtable.parse(fh.read())}
    except (OSError, ValueError):
        return True
    for name in names:
        seen = set()
        home = rows.get(name, "")
        while home.startswith("redirect:") and name not in seen:
            seen.add(name)
            name = home.split(":", 1)[1].strip()
            home = rows.get(name, "")
        if home == "overlay":
            return True
    return False


#: `filing_dir`'s answer when this machine may not write a private-class item anywhere (a work
#: machine with no usable private store). Not a path: every writer and reader tests for it first.
WITHHELD = "\0withheld"

_FILING_CACHE: Dict[tuple, Optional[str]] = {}


def reset_filing_cache() -> None:
    _FILING_CACHE.clear()


def filing_dir(dir_path: Optional[str], source: str) -> Optional[str]:
    """Where Bearing writes (and reads back) an item from `source` filed for project dir
    `dir_path` — the ONE decision every Bearing writer and reader calls (ruling 5,
    2026-10-13: no private text in a github.com repo).

    None for no dir; `dir_path` unchanged for a personal-class source (except NEVER_GITHUB
    ones and any source filed for a workplace project (`is_work_project`), placed like
    private-class: neither ever lands in a github.com tree), or when `dir_path` is
    not a git checkout (a Drive folder, a missing dir). Otherwise `prosehome.place` decides:
    the repo itself when it pushes only to the private host (or on a machine with no private
    access), the overlay `<private store>/projects/<Repo>` for any other repo, or `WITHHELD`.
    Admission (`accepts`, Hestia's boundary) is a separate question and is unchanged."""
    if not dir_path:
        return None
    path = os.path.expanduser(dir_path)
    guarded = source in NEVER_GITHUB or (source_class(source) == "personal"
                                         and is_work_project(path))
    if source_class(source) == "personal" and not guarded:
        return path
    from . import privatestore, prosehome, reckoning
    if guarded and os.path.isdir(_real(path)) and \
            reckoning.repo_top(_real(path)) is not None:
        # Same placement as private-class, except a github.com checkout is NEVER written in
        # place: prosehome's "no private access here, so nothing to leak" does not apply.
        placement = prosehome.place(_real(path), "")
        if placement.where == prosehome.STORE:
            return placement.path.rstrip(os.sep)
        if placement.where == prosehome.REPO and prosehome.repo_is_private(_real(path)):
            return path
        return WITHHELD
    key = (_real(path), os.environ.get(privatestore.ROOT_ENV), os.environ.get("LODESTAR_MACHINE"))
    if key not in _FILING_CACHE:
        real = key[0]
        if not os.path.isdir(real) or reckoning.repo_top(real) is None:
            got = None
        else:
            placement = prosehome.place(real, "")
            if placement.where == prosehome.WITHHELD:
                got = WITHHELD
            elif placement.where == prosehome.STORE:
                got = placement.path.rstrip(os.sep)
            else:
                got = None
        _FILING_CACHE[key] = got
    got = _FILING_CACHE[key]
    return path if got is None else got


def ensure_filing_dir(dir_path: str, filing: str, sub: str = "") -> str:
    """Create `<filing>/<sub>` for a filing_dir answer. An overlay goes through
    `prosehome.ensure_dir`, which refuses a directory that escapes the private-store checkout."""
    from . import prosehome
    target = os.path.join(filing, sub) if sub else filing
    if filing == WITHHELD:
        raise ValueError("withheld: nothing may be written")
    # Authorisation comes from the placement, never from canonical-path equality: an overlay
    # symlinked back to the project would otherwise read as "in place" (GPT review 2026-10-13).
    if filing != os.path.expanduser(dir_path):
        return prosehome.ensure_dir(prosehome.Placement(prosehome.STORE, target, "overlay"))
    os.makedirs(target, exist_ok=True)
    return target


def project_rows(projects: List[Project]) -> List[Project]:
    return [p for p in projects if p.name not in DEFAULT_ROWS]


def dir_for(name: str, projects: List[Project]) -> Optional[str]:
    for project in projects:
        if project.name == name:
            return os.path.expanduser(project.dir)
    return None


def match_all(text: str, sender: Optional[str], projects: List[Project]) -> List[str]:
    """Every row the item matches, in table order: sender-rule rows first, then keyword rows,
    no duplicates. `match`/`match_sender` stop at the first row; this is for scoped-beacon
    gating, where an item about two projects must be able to alert both."""
    out: List[str] = []
    needle = _norm_sender(sender) if sender else None
    if needle:
        for project in projects:
            if needle in (_norm_sender(s) for s in project.senders) and project.name not in out:
                out.append(project.name)
    haystack = _norm(text)
    for project in projects:
        if project.name in out:
            continue
        if any(_has_term(haystack, _norm(kw)) for kw in project.keywords):
            out.append(project.name)
    return out


#: Reasons that are EVIDENCE about the item itself. A filing for any other reason (a source
#: default, or `people`) is provisional: it may be rerouted once real evidence appears, it never
#: pulls a reply after it (`bearingfile.thread_project`), and it never votes in `people_affinity`
#: — so an affinity guess can never confirm itself.
STRONG_REASONS = ("sender", "keyword", "thread", "body-keyword")

#: `people` routing reads at most this many participants. A message to 40 people (a town
#: hall, a 41-person list) is a broadcast: who received it says nothing about which project
#: it belongs to. 15, not 10, because a working team's own recurring meeting can invite a dozen or so.
PEOPLE_MAX = 15
PEOPLE_MIN_VOTES = 3
PEOPLE_MIN_SHARE = 0.6
PEOPLE_MIN_VOTERS = 2


def is_strong(reason: Optional[str]) -> bool:
    return (reason or "").split(" ", 1)[0].split(";", 1)[0] in STRONG_REASONS


_EXCLUDE_HEADING = re.compile(r"^#+\s*people never routed by affinity", re.I)
_TWO_COL = re.compile(r"^\|\s*(?P<who>[^|]+?)\s*\|\s*(?P<why>[^|]*?)\s*\|\s*$")


def load_people_exclusions(path: Optional[str] = None) -> Tuple[str, ...]:
    """Addresses `people` routing must never use, from routing.md's `## People never routed by
    affinity` table (`| address | why |`). Lukas's own addresses are there (he writes about
    every project) and so is anyone the SKILL already keeps off a sender rule on purpose."""
    default = path is None or os.path.realpath(path) == os.path.realpath(DEFAULT_PATH)
    path = path or DEFAULT_PATH
    sources = [path] + ([overlay_path() or ""] if default else [])
    out: List[str] = []
    for source in sources:
        if not source or not os.path.exists(source):
            continue
        inside = False
        with open(source, encoding="utf-8") as f:
            for line in f:
                if line.startswith("#"):
                    inside = bool(_EXCLUDE_HEADING.match(line))
                    continue
                if not inside:
                    continue
                m = _TWO_COL.match(line.rstrip("\n"))
                if not m:
                    continue
                who = m.group("who").strip().lower()
                if who in ("address", "") or set(who) <= {"-"} or who in out:
                    continue
                out.append(who)
    return tuple(out)


def participants(addresses, exclude=()) -> List[str]:
    skip = {_norm_sender(a) for a in exclude}
    out: List[str] = []
    for address in addresses or ():
        norm = _norm_sender(address)
        if norm and "@" in norm and norm not in skip and norm not in out \
                and not is_automated_sender(norm):
            out.append(norm)
    return out


def people_affinity(entries, projects: List[Project], exclude=(),
                    min_votes: int = PEOPLE_MIN_VOTES,
                    min_share: float = PEOPLE_MIN_SHARE) -> Dict[str, str]:
    """address -> the ONE project row that person's filed history belongs to.

    Derived each cycle from the filing ledger, never written into routing.md, so it can never
    harden into a sender rule nobody chose. A person qualifies for project P only if:
      * at least `min_votes` STRONG filings (sender/keyword/thread/body-keyword) to P list them
        as a participant (sender, To or Cc; a message with more than PEOPLE_MAX participants is
        ignored as a broadcast);
      * NO strong filing lists them on any other project row — the SKILL's own rule, "never
        seed a sender who writes about more than one project", applied to the history;
      * P is at least `min_share` of every filing they appear on, defaults included, so
        someone whose mail is mostly not about any tracked project (most personal mail) never
        drags the rest of it into the one project they once wrote about.

    Known, accepted limitation, not fixed here (2026-10-12 GPT review finding 5a): two people
    who are never seen apart can each independently clear these thresholds purely because they
    share the exact same underlying messages — "two agreeing voters" is not the same as "two
    independent corroborations." See `docs/bearing/people-affinity-limitations.md`.
    """
    rows = {p.name for p in project_rows(projects)}
    skip = {_norm_sender(a) for a in exclude}
    votes: Dict[str, Dict[str, int]] = {}
    total: Dict[str, int] = {}
    for entry in entries:
        people = entry.get("people") or []
        if not people or len(people) > PEOPLE_MAX:
            continue
        reason = entry.get("reason") or ""
        if reason.startswith("people"):
            continue  # a guess never votes for itself
        name = entry.get("filed_to")
        for person in people:
            person = _norm_sender(person)
            if person in skip:
                continue
            total[person] = total.get(person, 0) + 1
            if name in rows and is_strong(reason):
                votes.setdefault(person, {})
                votes[person][name] = votes[person].get(name, 0) + 1
    out: Dict[str, str] = {}
    for person, by_project in votes.items():
        if len(by_project) != 1:
            continue
        (name, count), = by_project.items()
        if count >= min_votes and count >= min_share * total.get(person, count):
            out[person] = name
    return out


def resolve(text: str, sender: Optional[str], source: str,
            projects: List[Project], thread: Optional[str] = None,
            body: str = "", people: Optional[List[str]] = None,
            affinity: Optional[Dict[str, str]] = None,
            thread_corroborated: bool = True) -> Resolution:
    """Where the item is filed, and every project it is associated with.

    Precedence, first allowed hit wins for filing:
      1. sender (exact address), then keyword in `text` (subject + preview) — `sender` /
         `keyword`, unchanged since 2026-10-08;
      2. `thread` — the project an earlier message of the same mail thread was filed to
         (`bearingfile.thread_project`; 2026-10-12, a reply whose own subject lacks the
         keyword followed its parent nowhere);
      3. keyword in `body` (the quoted thread below a reply) — `body-keyword`, last because
         quoted text and signatures carry other projects' words;
      4. `people` (2026-10-12, private-class sources only): at least two participants have an
         affinity (`people_affinity`) and all of them agree on ONE project. Provisional,
         like a default;
      5. the source default.
    A match the destination's boundary refuses is recorded in `blocked` and skipped, at every
    step: a thread or body hit is never a way past the Hestia boundary.

    `thread_corroborated=False` (2026-10-12, GPT review finding 6) demotes step 2: the caller
    is saying this `thread` hit is bare cross-source subject-string equality with no other
    evidence (`bearingfile.thread_corroboration`), so it is held as a fallback rather than
    locked in immediately — a confident people-affinity vote (step 4) is allowed to outrank it,
    and body-keyword (step 3) too. It still wins over the plain source default if nothing
    stronger turns up. A corroborated thread (the default) keeps its original precedence,
    unchanged.
    """
    sender_hits = set(match_all("", sender, projects)) if sender else set()
    blocked: List[str] = []
    allowed: List[str] = []

    def _admit(names):
        for name in names:
            if name in allowed or name in blocked:
                continue
            if accepts(dir_for(name, projects), source):
                allowed.append(name)
            else:
                blocked.append(name)

    _admit(match_all(text, sender, projects))
    filed_to = reason = None
    if allowed:
        filed_to = allowed[0]
        reason = "sender" if filed_to in sender_hits else "keyword"
    weak_thread = None
    if thread and thread not in DEFAULT_ROWS and dir_for(thread, projects) is not None:
        _admit([thread])
        if filed_to is None and thread in allowed:
            if thread_corroborated:
                filed_to, reason = thread, "thread"
            else:
                weak_thread = thread  # deferred: a confident people vote may outrank it
    if body:
        _admit(match_all(body, None, projects))
        if filed_to is None and allowed:
            filed_to, reason = allowed[0], "body-keyword"
    if people and affinity and source_class(source) == PRIVATE and len(people) <= PEOPLE_MAX:
        voters = [affinity[_norm_sender(p)] for p in people if _norm_sender(p) in affinity]
        voted = sorted(set(voters))
        # TWO agreeing participants, never one: on a replay, a single voter filed social,
        # personal and all-staff mail into projects; two agreeing voters did not.
        if len(voted) == 1 and len(voters) >= PEOPLE_MIN_VOTERS:
            _admit(voted)
            if filed_to is None and voted[0] in allowed:
                filed_to, reason = voted[0], "people"
    if filed_to is None and weak_thread is not None:
        filed_to, reason = weak_thread, "thread"
    if filed_to is None:
        filed_to = default_for(source)
        reason = "default:%s" % source
    associations = [filed_to] + [n for n in allowed if n != filed_to]
    return Resolution(filed_to=filed_to, reason=reason, associations=tuple(associations),
                      blocked=tuple(blocked))


def project_for_path(path: str, projects: List[Project],
                     companions: Optional[List[Tuple[str, str]]] = None) -> Optional[str]:
    """The routing row whose dir contains `path` — longest dir wins. Realpath on both sides
    (a machine whose ~/dev is a symlink, so comparing an expanded path to a realpath fails).
    No row matches: a `companions` dir containing `path` names its private project's row."""
    best, best_len = None, -1
    for project in projects:
        if _under(path, project.dir):
            length = len(_real(project.dir))
            if length > best_len:
                best, best_len = project.name, length
    if best is None:
        for cdir, private in companions or ():
            name = _companion_row(private, projects)
            if name and _under(path, cdir):
                length = len(_real(cdir))
                if length > best_len:
                    best, best_len = name, length
    return best


def _companion_row(private: str, projects: List[Project]) -> Optional[str]:
    """The row a companion's private half names: a row name, or a path that IS a row's dir
    (exactly; a dir merely inside the store must not inherit all of the store's notes)."""
    for project in projects:
        if project.name == private:
            return project.name
    if os.sep in private or private.startswith("~"):
        for project in projects:
            if _real(project.dir) == _real(private):
                return project.name
    return None


_COMPANION_HEADING = re.compile(r"^#+\s*companion directories", re.I)


def load_companions(path: Optional[str] = None) -> List[Tuple[str, str]]:
    """`(dir, private)` pairs from routing.md's `## Companion directories` table
    (`| dir | private | why |`): a shareable checkout and its private half, which is either its
    own parallel repo or a directory in the shared private store (owner, 2026-10-24: "handle
    both"). `private` is a row name or a path. When it is (or is the dir of) a project row, a
    session in `dir` sees that row's Bearing notes, beacons and sightings; a pairing with no row
    is recorded for every other reader. Bearing never files into `dir`."""
    path = path or DEFAULT_PATH
    out: List[Tuple[str, str]] = []
    if not os.path.exists(path):
        return out
    inside = False
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("#"):
                inside = bool(_COMPANION_HEADING.match(line))
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")] if inside else []
            if len(cells) < 2 or cells[0].lower() == "dir" or set(cells[0]) <= {"-"}:
                continue
            out.append((cells[0], cells[1]))
    return out


def descendants(name: str, projects: List[Project]) -> List[str]:
    """`name` plus every row whose dir sits under its dir (a private-store session also sees
    the rows nested inside it)."""
    root = dir_for(name, projects)
    if root is None:
        return []
    return [p.name for p in projects if _under(p.dir, root)]


def _load_unfiled(path: str) -> Dict[str, List[str]]:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    # Tolerate the pre-fix int-counter shape (a key mapping to an int rather than a list of
    # message ids) by treating it as "seen, no known ids" — never crash loading an old file.
    return {k: (v if isinstance(v, list) else []) for k, v in data.items()}


def _save_unfiled(path: str, data: Dict[str, List[str]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def record_unfiled(key: str, message_id: Optional[str] = None,
                   unfiled_path: Optional[str] = None) -> int:
    """Bump and return the DISTINCT-message count seen for `key`.

    Counts distinct `message_id`s, not calls — a 2026-10-08 ChatGPT review caught that counting
    calls lets the exact same unread email, re-seen on every cron cycle until it's read, satisfy
    an "more than once" threshold entirely on its own. `message_id` should always be supplied
    when the caller has one (mail items carry a stable graph message id); if it's genuinely
    unavailable, each call is treated as its own distinct occurrence (falls back to the old,
    weaker behavior) — callers without a stable id should prefer not to drive auto-add at all.
    """
    path = unfiled_path or DEFAULT_UNFILED_PATH
    data = _load_unfiled(path)
    seen_ids = data.get(key, [])
    if message_id is None:
        seen_ids.append("__no_id_%d__" % len(seen_ids))
    elif message_id not in seen_ids:
        seen_ids.append(message_id)
    data[key] = seen_ids
    _save_unfiled(path, data)
    return len(seen_ids)


OVERLAY_HEADER = ("# Lodestar routing — private overlay\n\n"
                  "Learned senders and colleague exclusions, unioned into Lodestar's "
                  "`priority/routing.md` by `bin/lib/routing.py` (ruling 5, 2026-10-13).\n\n"
                  "| project | dir | keywords | senders |\n|---|---|---|---|\n")


def _append_sender(project_name: str, sender: str, path: str) -> None:
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(OVERLAY_HEADER)
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    rows = [_ROW_RE.match(l.rstrip("\n")) for l in lines]
    if not any(m and m.group("project").strip() == project_name for m in rows):
        # The overlay starts with no rows: give this project one (`-` = dir lives in Lodestar).
        at = max((i for i, l in enumerate(lines) if _ROW_RE.match(l.rstrip("\n"))),
                 default=len(lines) - 1) + 1
        lines.insert(at, "| %s | - |  |  |\n" % project_name)
    out = []
    for line in lines:
        m = _ROW_RE.match(line.rstrip("\n"))
        if m and m.group("project").strip() == project_name:
            existing = [s.strip() for s in m.group("senders").split(",") if s.strip()]
            if sender not in existing:
                existing.append(sender)
            out.append("| %s | %s | %s | %s |\n" % (
                m.group("project").strip(), m.group("dir").strip(),
                m.group("keywords").strip(), ", ".join(existing)))
        else:
            out.append(line)
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(out)


def _append_audit(project_name: str, learned: str, trigger: str, audit_path: str) -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(audit_path, "a", encoding="utf-8") as f:
        # The trigger used to be the mail's subject: private text in github.com history. Since
        # ruling 5 (2026-10-13) only its length is kept; the message id is in the unfiled log.
        f.write("- %s — auto-added sender `%s` to **%s** (triggered by a %d-character "
                "subject)\n" % (stamp, learned, project_name, len(trigger or "")))


# Noise-filtering, not routing — colocated here rather than in bin/mail_triage.py on purpose.
# bin/bearing.py runs under plain `python3` (no venv), but mail_triage.py imports `graphaccess`
# at module level, so importing it from bearing.py raises `ModuleNotFoundError` immediately
# (confirmed 2026-10-08, not a hypothetical). Copied verbatim from mail_triage.AUTOMATED rather
# than refactoring mail_triage.py's import structure, per the plan's "don't add new dependencies
# without a real reason" constraint — this is the smaller change. If the two lists ever drift,
# that is itself worth noticing (a case diffing them cheaply, since it's one tuple here and one
# tuple there).
def _extra_automated_senders() -> List[str]:
    """Deployment-specific sender markers, one per line in `sync/automated-senders.txt` beside
    this checkout (never exported). No file: none."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "sync", "automated-senders.txt")
    try:
        with open(path, encoding="utf-8") as fh:
            return [l.strip().lower() for l in fh if l.strip() and not l.startswith("#")]
    except OSError:
        return []


AUTOMATED_SENDERS = (
    "no-reply", "noreply", "donotreply", "do-not-reply", "notification", "notifications",
    "mailer-daemon", "postmaster", "automated", "alerts", "alert@", "bounce",
    "@jira", "confluence", "jenkins", "github", "gitlab", "sharepointonline",
    "calendar-notification", "quarantine", "listserv", "majordomo",
    "servicenow", "portal.dev", "read.ai", "@e.read",
) + tuple(_extra_automated_senders())


def is_automated_sender(address: Optional[str]) -> bool:
    addr = (address or "").lower()
    return any(marker in addr for marker in AUTOMATED_SENDERS)


def maybe_autoadd_sender(key: str, project_name: str, sender: str, trigger: str,
                         message_id: Optional[str] = None, path: Optional[str] = None,
                         audit_path: Optional[str] = None,
                         unfiled_path: Optional[str] = None) -> bool:
    """Add `sender` (exact-match) to `project_name`'s `senders` list once `key` has recurred,
    as a DISTINCT message, more than once.

    Audit is written BEFORE the mutation (a 2026-10-08 review's second finding: writing the
    mutation first means a failed audit-append after a successful add leaves an unaudited rule
    change in place; writing audit first means the worst failure mode is an audit line for a
    rule that never actually got applied, which is the safer direction to fail in).
    """
    count = record_unfiled(key, message_id, unfiled_path)
    if count <= 1:
        return False
    def _is(given, default):
        return given is None or os.path.realpath(given) == os.path.realpath(default)

    if _is(path, DEFAULT_PATH):
        # A learned sender is a colleague's address: it goes to the private overlay, never into
        # Lodestar's github.com routing.md. No private store here -> learn nothing (and say so in
        # the return value) rather than write it to github.com.
        path = overlay_path()
        if _is(audit_path, DEFAULT_AUDIT_PATH):
            audit_path = overlay_path(OVERLAY_AUDIT_NAME)
        if path is None or audit_path is None:
            return False
        from . import privatestore, proseguard
        privatestore._safe_dir(privatestore.root(), os.path.dirname(path))
    _append_audit(project_name, sender, trigger, audit_path or DEFAULT_AUDIT_PATH)
    _append_sender(project_name, sender, path)
    return True
