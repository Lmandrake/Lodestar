"""Other forms of the names in the private names.txt: a colleague is rarely written in full.

names.txt holds full names, and the `name` scanner matches those. Prose says "Pat", "Example's
team", "Pat E.", "Example, Pat" or `pexample`. This module derives those forms from the same list
and finds them, in the class `name_form`.

names.txt line syntax (the file stays private; only the code is public):

    Pat Example                      # first, last, first+initial, initial+last, Last-First, handles
    Patricia Example = Pat, Patty    # explicit nicknames, found like a first name

Rules, all erring toward a hit (a false positive costs one [[allow]] line; a miss is published):

* STRONG forms hit anywhere: `Pat E.`, `P. Example`, `Example, Pat`, handles (`pat.example`,
  `pexample`), and any bare first name / last name / nickname that is not also an ordinary word.
* AMBIGUOUS words (a name that is also a word: Will, Read, Mark, Grace, Young, Sydney...) hit only
  when the context marks a PERSON: Title case (not an identifier) AND one of: possessive
  (`Grace's`); a person word just before (`with`, `asked`, `Hi`, `colleague`, `Dr.`; `to` only
  with a send word on the line); a person verb just after (`said`, `offered`); a names.txt surname
  or an `X.` initial just after, or a names.txt first name just before (`Will Young`, `Brisa
  Jarrow`); an organisation (`Will (GSFC)`, `Mark at GSFC`). An email local part (`will@`) hits in
  any case. A work word merely somewhere on the line is NOT enough: that flooded the scan with
  `Read`, `Manager` and `Stack` in tool docs and code.
* Initial forms need a real first name or a standalone initial: `Zed Q`, `Q. Arbuckle`; never two
  bare letters (`w G`), a lower-case word (`H for`), or a sentence end (`HALT. Do`). A handle
  built from an initial and an ambiguous word (`m.line`, `p_read`) needs `@` or a work word.
* A field label before the name (`owner: Mark`, `| reviewer | Cam |`, `Attendees: Will, Grace`),
  an `@mention`, `Hi CAM,`, or a narrow person-work word on the line (meeting, standup,
  colleague, said, NASA...) also count. Names shorter than 3 letters additionally accept any work
  word on the line.
* Eponyms (`Wilson interval`, `Goodhart's law`, `Bradley-Terry`) and places (`Mt Wilson`,
  `America/Santiago`) are silent for SURNAMES only, never after an honorific.

The ambiguity list is deliberately explicit, small and reviewable; a name missing from it is
treated as rare, so the failure direction is a false hit.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, Iterable, List, Set, Tuple

_WORD = re.compile(r"[^\W\d_]+(?:['’]s\b)?")
_HANDLE_TOKEN = re.compile(r"[A-Za-z0-9._-]+")
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "md"}
_PARTICLES = {"de", "del", "della", "der", "di", "da", "dos", "du", "la", "le", "van", "von", "bin",
              "ibn", "al", "el", "st", "mc", "mac"}

# Names that are also ordinary words, months or surnames-as-nouns. Anything else is "rare".
AMBIGUOUS = frozenset("""
will mark grace art bill bob pat rob rose hope faith joy june april may august summer dawn dean drew
earl frank gene gill guy jack jay jim ray rich sue ted tom tim tony walt wade ward wes page pearl
penny pierce pat patch rusty sandy sage skip sky stone storm tag victor violet wayne amber ash
barb bud buck carol chad chase chip christian clay cliff cole colin curt dale dan daisy dash
don duke earnest ernest fern flint forest gail garnet gay glen heather holly honey iris ivy jade
jasper jean jewel jo joan john jordan josh juliet kit lance lane lark laurel lee lily lou luck
mac mason max mike miles misty mitch nick noel olive pam parker patience peace penn pete peter
pip poppy prince queen rain reed rex rhodes ricky river robin rocky rod rory rowan ruby sam sean
sherry shirt silver star steele sterling sunny tab tara taylor terry tiger tyler val van vance
vern vic victory wally whit wilbur willow wren young brown white black green grey gray hall long
cook baker rich bell price king fox moore hill lake field fields wood woods ford bush rivers
marsh mills miller smith carter cross day east west north south bishop knight mason porter
shepherd stewart taylor turner walker ward weaver wells wright hunt hunter lord love little
strong sharp short swift small best good bright case cash chance church crane crow dove drake
eagle fish frost gold hawk hart hood hook lamb lion love lucky major mayor miles nash noble
park peak plant poole pool post power pride rice rock ross sands scott sea short stark stein
story street sweet swan temple tree vine ward wild wing winter wolf work
jan feb mar apr jun jul aug sep sept oct nov dec monday tuesday wednesday thursday friday
saturday sunday
a an do to for co oh ha lo ma om ly x him huh try lay sin tin yam goo read cam free hang constant
shell speed call line link stack manager dev diamond hammer song burns grant fore finch bridge
stock sun fan duck hatch fuller harsh hare pin blank carrier lan gene angel archer barber bard
barge bass bender berry bin bloom blossom booth boss bramble branch breed brook buddy bull bumble
burr coffee curry deacon dell diner drain elder ember eve galley gin grip hand harmony herald
justice man meadow melody miner moon pace panning para peacock raff rampant reg rink rush seal
sham sly son sparks spry stew stinger stork stride till trainer vane ware watt sir darwin london
sydney paris berlin boston austin phoenix aurora luna sol terra astra aria perl pascal julia ada
gates hand duck
""".split())

# Words just before a name that make it a person: `with Will`, `asked Mark`, `Hi Grace`.
PERSON_BEFORE = frozenset("""
with from by ask asks asked tell tells told thank thanks thanked cc ping pings pinged email emails
emailed met meet meeting hi hey dear per
colleague colleagues coworker collaborator manager director supervisor boss postdoc intern pi pm
lead dr prof mr ms mrs
""".split())

# Organisation words that, right after a name, place it at work: `Will (GSFC)`, `Mark at GSFC`.
# The deployment's own employer words come from `lib.boundary.org_words()`.
ORG = frozenset("""
nasa hq headquarters caltech gsfc goddard ames langley jsc msfc grc ksc lab labs centre center
""".split())
_ORG_LINK = frozenset("at from of in the".split())

# Nouns that turn a surname into the name of a method, law or place: `Wilson interval`.
_EPONYM_NOUN = frozenset("""
interval intervals score law laws act effect principle test distribution coefficient criterion
index transform equation theorem method model algorithm curve ratio rating
""".split())
_EPONYM_POSSESSIVE = frozenset("law laws principle theorem equation constant effect razor".split())
_EPONYM_PARTNER = frozenset("terry luce wilson".split())

_HONORIFIC = frozenset("dr prof mr ms mrs".split())
_NOT_VERB = frozenset("be not also only then still the a an be".split())
_CAPS_MARKER = frozenset("hi hey dear cc thanks".split())
# Function words: `Do's & Don'ts` is not a possessive person.
_FUNCTION = frozenset("a an do to for co oh ha lo ma om ly x him huh try".split())

# Words just after a name that make it a person: `Grace said`, `Emily offered`, `Li joined`.
PERSON_AFTER = frozenset("""
said says wrote writes asked asks asking told replied replies emailed suggested suggests noted
mentioned offered offers agreed proposed thinks thought wants wanted joined joins left retires
retired presented explained confirmed promised sent approved approves owns owned reviewed reviews
leads led attended commented signed declined accepted volunteered recommended decided
""".split())
# Auxiliaries: `Grace will review`, `Mark can send`: the name, an auxiliary, then a lower-case verb.
_AUX = frozenset("will would can could should must might may shall".split())
# Field labels that make the next value a person: `owner: Mark`, `| reviewer | Cam |`, `cc: SAM`.
_FIELD = re.compile(r"(?i)\b(?:owners?|assignees?|assigned|reviewers?|authors?|attendees?|contacts?|"
                    r"leads?|approvers?|requesters?|requested[ \t]+by|reported[ \t]+by|cc|to|from|"
                    r"speakers?|participants?|members?|pi|co-?i|poc)[ \t]*(?:[:=|]|\*\*:|:\*\*)")
# Work words that, on the same line, make an ambiguous name a person. Deliberately narrower than
# CONTEXT: `call`, `team`, `email`, `manager` and `pm` also describe tools, and flooded the scan.
PERSON_LINE = frozenset("""
colleague colleagues coworker coworkers collaborator collaborators meeting meetings attendee
attendees postdoc intern supervisor boss sync standup kickoff emailed pinged slack zoom cc thanks
nasa caltech gsfc goddard jsc msfc lab
""".split())
# A word that, before `to <Name>`, makes it a recipient: `an email to Pat`, `send it to Will`.
_SEND = frozenset("email emails emailed mail message reply wrote write send sent forward ping "
                  "slack note talk talked speak spoke introduce introduced assign assigned "
                  "assigning hand handed give gave delegate delegated escalate escalated".split())

# Words that put a name in a working context. Lower-case, matched as whole words.
CONTEXT = frozenset("""
colleague colleagues coworker coworkers collaborator collaborators team teams lab center centre
centers centres nasa hq headquarters caltech gsfc goddard ames langley jsc msfc meeting
meetings call calls email emails emailed e-mail slack zoom pm pi manager director supervisor
boss said says asked told wrote thanks cc ping pinged attendee attendees postdoc intern
sync standup kickoff fte ftes
""".split())



def _with_org_words() -> None:
    """Add this deployment's employer words (`lib.boundary`, data) to the org/work word sets."""
    global ORG, PERSON_LINE, CONTEXT
    try:
        from lib import boundary
    except ImportError:  # pragma: no cover
        import boundary  # type: ignore
    extra = frozenset(boundary.org_words())
    ORG, PERSON_LINE, CONTEXT = ORG | extra, PERSON_LINE | extra, CONTEXT | extra


_with_org_words()

_PRE_CHARS = " \t*_#>-|\"'([`~"
_WINDOW = 8       # words either side that may carry a work word


def _ascii_fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")


def parse_line(line: str) -> Tuple[str, List[str]]:
    """`Full Name = nick, nick` -> (full name, nicknames)."""
    full, _eq, nicks = line.partition("=")
    return full.strip(), [n.strip() for n in nicks.split(",") if n.strip()]


class NameForms:
    """Derived forms of every entry in names.txt. Holds nothing but derived strings, in memory."""

    def __init__(self, lines: Iterable[str]):
        self.bare: Dict[str, str] = {}           # casefolded word -> kind (first/last/nick)
        self.handles: Set[str] = set()      # hit anywhere
        self.initial_handles: Dict[str, str] = {}   # `p.read` -> `read`: soft if the word is
        self.weak: Set[str] = set()         # flast / firstl: also ordinary words; need context
        self.learned: Set[str] = set()      # name words seen in lower case in the scanned tree
        self.first_initial: Dict[str, Set[str]] = {}    # first -> {last initials}
        self.initial_last: Dict[str, Set[str]] = {}     # last -> {first initials}
        self.last_first: List[Tuple[str, str]] = []
        self.pairs: List[Tuple[str, str]] = []
        for line in lines:
            full, nicks = parse_line(line)
            toks = [_ascii_fold(t) for t in re.findall(r"[^\W\d_]+", full)]
            toks = [t for t in toks if t]
            while len(toks) > 1 and toks[-1].casefold() in _SUFFIXES:
                toks.pop()
            if not toks:
                continue
            first = toks[0].casefold()
            self.bare.setdefault(first, "first")
            last = None
            if len(toks) > 1:
                last = toks[-1].casefold()
                self.bare.setdefault(last, "last")
                if len(toks) > 2 and toks[-2].casefold() in _PARTICLES:
                    self.bare.setdefault((toks[-2] + toks[-1]).casefold(), "last")
                self.first_initial.setdefault(first, set()).add(last[0].upper())
                self.initial_last.setdefault(last, set()).add(first[0].upper())
                self.pairs.append((first, last))
                f, l = first, last
                self.handles.add(f + l)
                self.handles.add(l + f)
                self.weak.update((f[0] + l, f + l[0]))
                for sep in (".", "_", "-"):
                    self.handles.update((f + sep + l, l + sep + f, f[0] + sep + l, f + sep + l[0]))
                    self.initial_handles.setdefault(f[0] + sep + l, l)
                    self.initial_handles.setdefault(f + sep + l[0], f)
                for tok in toks[1:-1]:
                    self.bare.setdefault(tok.casefold(), "mid")
            for nick in nicks:
                n = _ascii_fold(nick).casefold()
                if n:
                    self.bare.setdefault(n, "nick")
                    if last:
                        self.first_initial.setdefault(n, set()).add(last[0].upper())
                        for sep in ("", ".", "_", "-"):
                            self.handles.add(n + sep + last)
        # a 1-2 letter flast/f.l handle is noise; keep handles of 5+ characters
        self.handles = {h for h in self.handles if len(h) >= 5}
        self.weak = {h for h in self.weak if len(h) >= 6}
        # a middle name is never reported on its own
        self.bare = {w: k for w, k in self.bare.items() if k != "mid"}
        self.count = len(self.bare)

    # ---- matching --------------------------------------------------------------------------

    def find(self, text: str) -> List[Tuple[int, str]]:
        out: List[Tuple[int, str]] = []
        seen: Set[Tuple[int, int]] = set()

        def add(start: int, end: int) -> None:
            if (start, end) not in seen:
                seen.add((start, end))
                out.append((start, text[start:end]))

        words = [(m.start(), m.end(), m.group(0)) for m in _WORD.finditer(text)]
        lows = [self._strip(w[2]).casefold() for w in words]
        for i, (start, end, raw) in enumerate(words):
            low = lows[i]
            end_word = start + len(self._strip(raw))
            # Last, First
            if i + 1 < len(words) and (low, lows[i + 1]) in self._last_first_set():
                gap = text[end_word:words[i + 1][0]]
                if re.fullmatch(r"[ \t]*,[ \t]*", gap):
                    add(start, words[i + 1][1])
            # First L. / First L: a Title-case first name, then a standalone capital initial
            lasts = self.first_initial.get(low)
            if lasts and i + 1 < len(words) and len(low) >= 2 \
                    and re.fullmatch(r"[A-Z][a-z]+", self._strip(raw)):
                nxt = words[i + 1]
                gap = text[end_word:nxt[0]]
                word = nxt[2]
                if gap.strip(" \t") == "" and gap and len(word) == 1 and word.isupper():
                    after = text[nxt[1]:nxt[1] + 1]
                    dotted = after == "."
                    if self._ambiguous(low):
                        ok = dotted and word in lasts      # `Cam L.` only for a real Cam L...
                    else:
                        ok = word in lasts or dotted
                    if ok and not after.isalnum():
                        add(start, nxt[1] + (1 if dotted else 0))
            # F. Last / FLast: the initial stands alone (`P. Example`, `PExample`), never the
            # tail of a word (`PATH for`, `HALT. Do`), and the surname is Title case.
            firsts = self.initial_last.get(low)
            if firsts and re.fullmatch(r"[A-Z][a-z]+", self._strip(raw)):
                m = re.search(r"(?:\A|[^A-Za-z0-9])([A-Z])(\.?[ \t]?)\Z", text[max(0, start - 5):start])
                if m and m.group(1) in firsts and (m.group(2).startswith(".")
                                                    or not self._ambiguous(low)):
                    add(start - len(m.group(1) + m.group(2)), end_word)
            # bare
            if low in self.bare and self._bare_hit(words, lows, i, text):
                add(start, end_word)
        # handles
        for m in _HANDLE_TOKEN.finditer(text):
            raw_tok = m.group(0).strip("._-")
            tok = raw_tok.casefold()
            soft = self.initial_handles.get(tok)
            if soft is not None and self._ambiguous(soft):
                # `m.line`, `p_read`, `c-speed`: an initial on an ordinary word is code, not
                # a person, unless addressed (`@p.read`) or on a line about work
                if text[max(0, m.start() - 1):m.start()] == "@" or text[m.end():m.end() + 1] == "@" \
                        or self._line_has_context(text, m.start(), m.end()) \
                        or self._field_before(text, m.start()):
                    add(m.start(), m.end())
            elif len(tok) >= 5 and tok in self.handles:
                add(m.start(), m.end())
            elif tok in self.weak and (re.fullmatch(r"[A-Z][A-Z][a-z]+", raw_tok)
                                       or text[max(0, m.start() - 1):m.start()] == "@"
                                       or self._line_has_context(text, m.start(), m.end())):
                add(m.start(), m.end())
        return sorted(out)

    def learn(self, lowercase_counts, threshold: int = 3) -> None:
        """A name word that the scanned tree itself uses in lower case `threshold` or more times
        is an ordinary word there (grant, green, page): treat it as ambiguous for this scan."""
        self.learned = {w for w in self.bare if lowercase_counts.get(w, 0) >= threshold}

    @staticmethod
    def _line_has_context(text: str, a: int, b: int) -> bool:
        lo = text.rfind("\n", 0, a) + 1
        hi = text.find("\n", b)
        line = text[lo:hi if hi >= 0 else len(text)]
        return any(w.casefold() in CONTEXT for w in re.findall(r"[A-Za-z-]+", line))

    @staticmethod
    def _strip(word: str) -> str:
        return re.sub(r"['’]s\Z", "", word)

    def _last_first_set(self) -> Set[Tuple[str, str]]:
        cached = getattr(self, "_lf", None)
        if cached is None:
            cached = self._lf = {(l, f) for f, l in self.pairs}
        return cached

    def _ambiguous(self, low: str) -> bool:
        return low in AMBIGUOUS or low in self.learned

    _ROLE = frozenset("colleague colleagues coworker collaborator pi pm postdoc intern manager "
                      "director supervisor boss".split())

    def _bare_hit(self, words, lows, i: int, text: str) -> bool:
        start, end, raw = words[i]
        low = lows[i]
        word = self._strip(raw)
        if len(low) < 2:
            return False        # one letter alone is never reported as a name
        if self._in_email_local(text, start, end):
            return True         # an email local part: `grace@`, `grace.dev@`, `build.grace2@`
        if text[start - 1:start] == "@" and len(low) >= 3 and low not in _FUNCTION \
                and not re.match(r"[A-Za-z0-9.@{]", text[start - 2:start - 1] or " ") \
                and not re.match(r"[-_(/@]|\.[A-Za-z_]|[ \t]*[{;]", text[end:end + 4]):
            return True         # a mention: `@mark`, not `{@link}` or `@pytest.mark`
        capital = word[:1].isupper()
        short = len(low) < 3
        ambiguous = self._ambiguous(low)
        if not ambiguous and not short:
            if self.bare.get(low) == "last" and self._eponym_or_place(words, lows, i, text):
                return False
            if not capital and self._identifier(text, start, end):
                return False    # `wilson_ci`, `wilson(k, n)`, `flux.2-klein-4b`, `ernie-5.0`
            return len(low) >= 4 or capital        # rare and distinctive: any case
        if re.fullmatch(r"[A-Z]{2,}", word) and not text[end:end + 1] in ("_", ".") \
                and text[start - 1:start] not in ("_", ".", "/"):
            p = self._neighbour(words, i, -1, text, r"[ \t]*[:,]?[ \t]+")
            if p is not None and lows[p] in _CAPS_MARKER:
                return True     # `Hi CAM,`, `CC: SAM`: an explicit address, even in capitals
        if not capital or not re.fullmatch(r"[A-Z][a-z]+|[A-Z][a-z]*[A-Z][a-z]+", word):
            return False        # CONSTANTS and lower case are words here, not people
        if re.match(r"[ \t]*\([ \t]*(\w+)", text[end:end + 12]) and \
                re.match(r"[ \t]*\([ \t]*(\w+)", text[end:end + 12]).group(1).casefold() in ORG:
            return True         # `Mark(GSFC)`
        if self._identifier(text, start, end) or re.match(r"\.[a-z]", text[end:end + 2]):
            return False        # an identifier, attribute or call: `m.Read`, `Read(`, `Page.navigate`
        if low in _FUNCTION:
            # `An`, `Do`, `To`: only an explicit address makes these a person (`with An`, `Hi Do`)
            p = self._neighbour(words, i, -1, text, r"[ \t]*[,:]?[ \t]+")
            return (p is not None and lows[p] in PERSON_BEFORE and lows[p] != "per") \
                or self._field_before(text, start, adjacent=True)
        if self._marks_person(words, lows, i, text):
            return True
        if self._field_before(text, start):
            return True         # `owner: Mark`, `| reviewer | Cam |`, `Attendees: Will, Grace`
        return self._person_line(words, lows, i, text)

    @staticmethod
    def _in_email_local(text: str, start: int, end: int) -> bool:
        """The word sits inside the local part of an address: `grace@x.org`, `a.grace2@x.org`."""
        m = re.match(r"[A-Za-z0-9._+-]*@[A-Za-z0-9-]+\.[A-Za-z]", text[end:end + 80])
        if not m:
            return False
        head = re.search(r"[A-Za-z0-9._+-]*\Z", text[max(0, start - 64):start]).group(0)
        return not head.startswith((".", "@")) and "@" not in head

    @staticmethod
    def _field_before(text: str, start: int, adjacent: bool = False) -> bool:
        lo = text.rfind("\n", 0, start) + 1
        before = text[lo:start]
        m = None
        for m in _FIELD.finditer(before):
            pass
        if m is None:
            return False
        if adjacent:
            return bool(re.fullmatch(r"[ \t*_|\[\]\"'`>-]*", before[m.end():]))
        # only names, separators and markup between the label and this word
        return bool(re.fullmatch(r"[ \t*_|\[\]\"'`>-]*(?:[A-Z][A-Za-z.'’-]*[ \t,;/&*_|\[\]\"'`-]*"
                                 r"(?:and[ \t]+)?)*", before[m.end():]))

    def _person_line(self, words, lows, i: int, text: str) -> bool:
        short = len(lows[i]) < 3
        if not short and self._sentence_start(text, words[i][0]):
            return False        # `Read this when a human says...`: an imperative, not a person
        """A narrow person-work word within _WINDOW words on the same line (`Li joined the
        standup`). Short names need it; ambiguous ones accept it only mid-sentence."""
        for step in (-1, 1):
            k, n = i + step, 0
            while 0 <= k < len(lows) and n < _WINDOW:
                a, b = sorted((words[i][0], words[k][0]))
                if "\n" in text[a:b]:
                    break
                if lows[k] in PERSON_LINE or (short and lows[k] in CONTEXT):
                    return True
                k, n = k + step, n + 1
        return False

    @staticmethod
    def _identifier(text: str, start: int, end: int) -> bool:
        before, after = text[start - 1:start], text[end:end + 2]
        return (before in ("_", ".") or after[:1] in ("_", "(") or after[:1].isdigit()
                or (after[:1] == "." and after[1:2].islower())          # `keith.github.io`
                or (after[:1] in ("-", ".") and after[1:2].isdigit()))

    def _eponym_or_place(self, words, lows, i: int, text: str) -> bool:
        """`Wilson interval`, `Goodhart's law`, `Bradley-Terry`, `Mt Wilson`, `America/Santiago`:
        a name used as the name of a thing, not of a person in the text."""
        start, end, raw = words[i]
        if re.search(r"\b(?:Mt\.?|Mount|America|Europe|Australia|Asia|Africa|Pacific)[ /]\Z",
                     text[max(0, start - 12):start]):
            return True
        h = self._neighbour(words, i, -1, text, r"\.[ \t]+")
        if h is not None and lows[h] in _HONORIFIC:
            return False                                    # `Dr. Arbuckle's model` is a person's
        n = self._neighbour(words, i, 1, text, r"['’]s[ \t]+|[ \t]+|-")
        if n is not None and text[words[i][1]:words[n][0]] != "-":
            possessive = self._strip(raw) != raw
            if lows[n] in (_EPONYM_POSSESSIVE if possessive else _EPONYM_NOUN):
                return True
        if text[end:end + 1] == "-" and n is not None and words[n][2][:1].isupper() \
                and lows[n] in _EPONYM_PARTNER:
            return True
        return False

    def _neighbour(self, words, i: int, step: int, text: str, gap: str):
        """The word `step` away on the same line if only `gap` (a regex) separates them."""
        k = i + step
        if not 0 <= k < len(words):
            return None
        a, b = (words[k][1], words[i][0]) if step < 0 else (words[i][1], words[k][0])
        between = text[a:b]
        if "\n" in between or not re.fullmatch(gap, between):
            return None
        return k

    def _marks_person(self, words, lows, i: int, text: str) -> bool:
        """Context that makes a Title-case ambiguous word a person, not a word."""
        raw = words[i][2]
        if self._strip(raw) != raw and lows[i] not in _FUNCTION:
            q = self._neighbour(words, i, -1, text, r"[0-9]*[ \t]+")
            if q is None or not words[q][2][:1].isupper() or self._rare_first(lows[q]) \
                    or self._sentence_start(text, words[q][0]):
                return True     # possessive: `Grace's`; not `Do's`, `Orchard Lane's`, `M4 Max's`
        h = self._neighbour(words, i, -1, text, r"\.[ \t]+")
        if h is not None and lows[h] in _HONORIFIC:
            return True                                     # `Dr. Read`
        p = self._neighbour(words, i, -1, text, r"[ \t]*[*_\"'(]*[ \t]*|[ \t]*[,:]?[ \t]+[*_]*")
        if p is not None:
            pl = lows[p]
            if pl in PERSON_BEFORE or pl in self._ROLE:
                return True                                 # `with Grace`, `colleague Will`
            if pl == "to" and self._line_words(text, words[i][0]) & _SEND:
                return True                                 # `an email to Pat`
            if (words[p][2][:1].isupper() and self.bare.get(pl) == "first"
                    and not self._ambiguous(pl) and len(pl) >= 3
                    and self.bare.get(lows[i]) == "last"):
                return True                                 # `Brisa Jarrow`
        for step in (-1, 1):                                # `Quentin and Will`, `Will, Quentin`
            k = self._neighbour(words, i, step, text, r"['’]?s?,?[ \t]+|,[ \t]+")
            if k is not None and lows[k] == "and":
                k = self._neighbour(words, k, step, text, r"['’]?s?,?[ \t]+")
            if k is not None and words[k][2][:1].isupper() and self._rare_first(lows[k]):
                return True
        n = self._neighbour(words, i, 1, text, r"[ \t]+[*_]*|[*_]+[ \t]+|[ \t]*\(|,[ \t]+")
        if n is not None:
            nl, nraw = lows[n], words[n][2]
            if nl in PERSON_AFTER or nl in self._ROLE or nl in ORG:
                return True                                 # `Grace said`, `Will (GSFC)`
            if nl in _AUX and not self._ambiguous_aux(lows[i]):
                v = self._neighbour(words, n, 1, text, r"[ \t]+")
                if v is not None and words[v][2][:1].islower() and lows[v] not in _NOT_VERB:
                    return True                             # `Grace will review the plan`
            if len(nraw) == 1 and nraw.isupper() and text[words[n][1]:words[n][1] + 1] == "." \
                    and re.fullmatch(r"[ \t]+", text[words[i][1]:words[n][0]]):
                return True                                 # `Zed Q.`
            if (nraw[:1].isupper() and self.bare.get(nl) == "last" and len(nl) >= 3
                    and (not self._ambiguous(nl) or (lows[i], nl) in self._pair_set())):
                return True                                 # `Will Wexford`
            if nl in _ORG_LINK:
                m = self._neighbour(words, n, 1, text, r"[ \t]+")
                if m is not None and lows[m] == "the":
                    m = self._neighbour(words, m, 1, text, r"[ \t]+")
                if m is not None and lows[m] in ORG:
                    return True                             # `Mark at GSFC`, `Grace from the GSFC lab`
        return False

    @staticmethod
    def _sentence_start(text: str, at: int) -> bool:
        return bool(re.search(r"(?:\A|\n|[.!?:][ \t]+)[ \t*_#>|\"'(-]*\Z", text[max(0, at - 12):at]))

    @staticmethod
    def _ambiguous_aux(low: str) -> bool:
        return low in _FUNCTION or low in ("free", "read", "stack", "line", "link", "call", "shell")

    def _rare_first(self, low: str) -> bool:
        return self.bare.get(low) in ("first", "nick") and len(low) >= 3 and not self._ambiguous(low)

    @staticmethod
    def _line_words(text: str, at: int) -> Set[str]:
        lo = text.rfind("\n", 0, at) + 1
        hi = text.find("\n", at)
        return {w.casefold() for w in re.findall(r"[A-Za-z]+", text[lo:hi if hi >= 0 else len(text)])}

    def _pair_set(self) -> Set[Tuple[str, str]]:
        cached = getattr(self, "_ps", None)
        if cached is None:
            cached = self._ps = set(self.pairs)
        return cached
