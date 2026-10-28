#!/usr/bin/env python3
"""
Agent handoff ritual — portable, project-agnostic. Full doc: docs/HANDOFF.md.

When a long-running agent session ends (context full, wave complete, machine
reboot), the work that dies with it is never the code — that is committed — but
the JUDGMENT: what is half-done and where exactly it stops, what the owner has
not yet seen, which instrument lied this week. This tool makes capturing that
judgment cheap, makes CONSUMING it enforced, and makes harvesting it into
durable lessons a single command.

It is the generalisation of the RimWorld project's handoff.py, rebuilt on a
three-arm audit of 67 real handoffs (2026-09-17): pointer pickup was 25%
overall but near-100% when the pointer carried a concrete `NEXT:` action, so
the shape that gets consumed is now the only shape `--check` accepts.

THE DIVISION OF LABOUR IS THE POINT. A script can list commits and dirty
files; it cannot say which finding the owner needs to see or which trap cost
two hours. Those sections are emitted as loud `<<< WRITE THIS >>>` markers,
and `--check` refuses to call the handoff ready while any survives.

IT NEVER SAYS "HANDOFF READY" ITSELF. Only the agent does, after filling the
judgment sections and passing `--check`. That phrase is the owner's signal to
reboot the session; a script that could emit it would eventually emit it
wrongly.

    handoff.py                write the skeleton (gates first, --force to skip)
    handoff.py --check        gates + content-contract scan; exit 1 if not ready
    handoff.py --wake         the READ side: latest handoff + pointer status, led by
                              a warning naming any session (conversation record or
                              transcript) that started after it and left no handoff
    handoff.py --harvest [--out PATH]  lesson sections since the last drain ->
                              one file. WRITES harvest_<date>.md INTO the handoff
                              dir by default (it then looks like corpus clutter:
                              use --out to put it elsewhere, or move it out)
    handoff.py --owed         is a handoff owed? commits since this seat's newest
                              committed handoff -> "HANDOFF OWED" exit 1, else
                              exit 0. For after an idle auto-compaction: did a
                              HANDOFF READY happen since my last work?
    handoff.py --cull [--apply]  handoffs superseded >= cull_days ago whose
                              lessons are filed (never in a follower repo)
    handoff.py --since <sha>  window start override (default: the last handoff)

Pointers reference work; they are not a ledger (2026-10-22). On --wake (after one resolver
pass, bin/reconcile.py) and --check, each pointer's live state is read through Lodestar's
outcome registry (bin/lib/handoff_pointers.py): its id or `ref: <store>:<id>` names an item
in a registered store, or `check: {...}` carries an inline outcome check. --check refuses a
pointer that references finished work. Outside Lodestar's tree the registry is simply absent.

Drafts waiting on the owner are never carried (LODE-20, 2026-10-23). --wake leads with an
OWNER NEEDED block for every Gmail or Outlook draft older than 12 h (bin/lib/handoff_drafts.py,
from the Companion DRAFTS ledger or a direct Gmail read), and --check refuses a half-done
pointer that holds a draft "pending his word", or a named draft the ledger does not list.

Configuration is optional (`.handoff.json` at the repo root) and a project may
plug in identity, extra gates, pointer states and extra sections via a small
adapter module. Zero-config single-agent repos just work as seat SOLO.
Stdlib only, Python 3.9+.
"""

import argparse
import datetime
import io
import json
import os
import re
import subprocess
import sys

TODO = "<<< WRITE THIS >>>"
WHOSE = "<<< WHOSE? >>>"

DEFAULTS = {
    "handoff_dir": "handoffs",       # where handoff files live, repo-relative
    "lessons_file": "LESSONS.md",    # lessons inbox: path, dir, or list (for --harvest)
    "seat_env": ["AGENT_SEAT", "HANDOFF_SEAT"],
    "default_seat": None,            # set e.g. "SOLO" to pin identity
    "cull_days": 7,                  # grace after SUPERSESSION (owner card 2026-10-19)
    "commits_cap": 20,
    "dirty_cap": 20,                 # max uncommitted-path lines in "Tree state"
    "adapter": None,                 # repo-relative path to an adapter module
    # true: handoffs live in the private-hosted overlay
    # <private store>/projects/<Repo>/<handoff_dir>, never in this repo's tree, whenever
    # the repo can push to github.com (ruling 5, 2026-10-13). See lib/prosehome.py.
    "private_store": False,
}

# The sections a script cannot produce. Each is a real question the next
# session will ask on wake, in the order it will ask them.
JUDGEMENT_SECTIONS = [
    ("The one thing to carry forward",
     "The single most important thing learned. Not a list — the thing that "
     "would cost the next session hours if it had to rediscover it. If nothing "
     "qualifies, write 'nothing this wave' and mean it."),
    ("What the owner should see",
     "Findings that need the owner's eye or decision: a number nobody ruled "
     "on, a change they can veto, anything shipped deliberately with a flag "
     "raised. Empty is a legitimate answer."),
    ("What is half-done, and where it stops",
     "Anything left mid-flight, one bullet each: `- ITEM_OR_TOPIC — state; "
     "NEXT: <one imperative action>`. A pointer without a NEXT: measured ~0% "
     "pickup; with one, near-100%."),
    ("Traps learned",
     "Instruments that lied, silent failures, commands that ate their own "
     "input. ONE line each, ending with where it now lives — append it to the "
     "lessons file the moment it is learned, then cite `(filed: LESSONS)` or "
     "`(see: <doc>)`. Never re-explain a trap that is already recorded."),
]

# --check verifies these headings exist. A hand-written file with its own
# headers used to pass trivially because it contained no TODO marker; the
# template is the contract.
CANONICAL_HEADINGS = [t for t, _ in JUDGEMENT_SECTIONS] + [
    "Commits",
    "Tree state at wrap",
]


# ---------------------------------------------------------------- plumbing --

def sh(*args, **kw):
    try:
        return subprocess.run(args, cwd=kw.get("cwd") or ROOT,
                              capture_output=True, text=True,
                              timeout=kw.get("timeout", 60)).stdout.strip()
    except Exception as e:  # noqa: BLE001 — a broken git is a gate, not a crash
        return "<could not run %s: %s>" % (args[0], e)


def _find_root():
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit("REFUSED: not inside a git repository. The handoff "
                         "ritual is built on git provenance.")
    return out.stdout.strip()


ROOT = None  # set in main(); tests set it directly


def _legacy_store_key():
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    from lib import boundary
    return boundary.store_name().replace("-", "_")


def config():
    cfg = dict(DEFAULTS)
    p = os.path.join(ROOT, ".handoff.json")
    if os.path.isfile(p):
        try:
            user = json.load(io.open(p, encoding="utf-8"))
        except ValueError as e:
            raise SystemExit("REFUSED: %s is not valid JSON (%s). A silently "
                             "ignored config is worse than a crash." % (p, e))
        # The key's older spelling (the store's repo name, `_` for `-`) is still read, so a
        # `.handoff.json` written before the rename keeps working on every machine.
        legacy = _legacy_store_key()
        if legacy and legacy in user and "private_store" not in user:
            user["private_store"] = user.pop(legacy)
        unknown = sorted(set(user) - set(DEFAULTS))
        if unknown:
            raise SystemExit("REFUSED: unknown key(s) in .handoff.json: %s — "
                             "a typo here would silently change nothing."
                             % ", ".join(unknown))
        cfg.update(user)
    return cfg


_ADAPTER = ["unloaded"]


def adapter(cfg):
    """The project's optional adapter module, or None. May define any subset
    of: seat() -> str, gates() -> [str], pointer_state(ids) -> {id: str},
    extra_sections() -> [(title, [lines])]."""
    if _ADAPTER[0] != "unloaded":
        return _ADAPTER[0]
    _ADAPTER[0] = None
    if cfg.get("adapter"):
        path = os.path.join(ROOT, cfg["adapter"])
        if not os.path.isfile(path):
            raise SystemExit("REFUSED: .handoff.json names adapter %s but no "
                             "such file exists." % cfg["adapter"])
        import importlib.util
        spec = importlib.util.spec_from_file_location("handoff_adapter", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _ADAPTER[0] = mod
    return _ADAPTER[0]


def _to_utc_z(iso_ts):
    """A `%cI` git timestamp (local offset) rewritten to Z-suffixed UTC, so
    string comparison against other normalized timestamps is chronological.
    Without this, '13:07:06Z' vs '06:19:54-07:00' compares '13' > '06' and
    gets the ORDER of two moments wrong (measured live, RimWorld 2026-09-08)."""
    try:
        dt = datetime.datetime.fromisoformat(iso_ts)
    except ValueError:
        return iso_ts
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ corpus --

_PRIVATE_DIRS = {}


def handoff_dir(cfg):
    if not cfg.get("private_store"):
        return os.path.join(ROOT, cfg["handoff_dir"])
    key = (ROOT, cfg["handoff_dir"])
    if key not in _PRIVATE_DIRS:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        from lib import prosehome
        placement = prosehome.place(ROOT, cfg["handoff_dir"])
        if placement.where == prosehome.WITHHELD:
            raise SystemExit("REFUSED: this repo's handoffs live on the private host "
                             "(.handoff.json private_store) and %s. Nothing was written."
                             % placement.reason)
        _PRIVATE_DIRS[key] = placement.path
    return _PRIVATE_DIRS[key]


def _corpus_git(cfg):
    """(cwd, pathspec) for asking git about the handoff files: ROOT and the configured dir,
    or — for a private overlay — the overlay's own checkout and its path inside it."""
    d = handoff_dir(cfg)
    if d == os.path.join(ROOT, cfg["handoff_dir"]):
        return ROOT, cfg["handoff_dir"]
    probe = d
    while probe and not os.path.isdir(probe):
        probe = os.path.dirname(probe)
    top = sh("git", "rev-parse", "--show-toplevel", cwd=probe)
    if not top or top.startswith("<"):
        return probe, "."
    return top, os.path.relpath(os.path.realpath(d), os.path.realpath(top))


ROOT_MARK = "handoff-root: "


def window_start(cfg, prev_name):
    """The ROOT commit a handoff's window is measured from: the commit that added it, or —
    for a handoff kept in the private overlay — the ROOT commit it recorded when it was built
    (`<!-- handoff-root: <sha> -->`), falling back to the last ROOT commit before the overlay
    commit for a file written before the mark existed."""
    cwd, spec = _corpus_git(cfg)
    rel = os.path.join(spec, prev_name)
    if cwd == ROOT:
        return sh("git", "log", "-1", "--format=%h", "--", rel)
    try:
        text = io.open(os.path.join(handoff_dir(cfg), prev_name), encoding="utf-8").read()
    except (IOError, OSError):
        text = ""
    m = re.search(r"<!-- %s([0-9a-f]{7,40}) -->" % re.escape(ROOT_MARK), text)
    if m:
        return sh("git", "rev-parse", "--short", m.group(1))
    ts = sh("git", "log", "-1", "--format=%cI", "--", rel, cwd=cwd)
    if not ts or ts.startswith("<"):
        return ""
    return sh("git", "rev-list", "-1", "--abbrev-commit", "--before=%s" % ts, "HEAD")


def corpus(cfg, all_seats=False, seat_name=None):
    """Handoff filenames on disk, unordered — filenames are not a clock."""
    d = handoff_dir(cfg)
    if not os.path.isdir(d):
        return []
    names = [fn for fn in os.listdir(d)
             if "_HANDOFF_" in fn and fn.endswith(".md")]
    if all_seats:
        return names
    s = seat_name or seat(cfg)
    return [fn for fn in names if fn.startswith(s + "_HANDOFF_")]


def corpus_commit_times(cfg):
    """{filename: newest commit ts, normalized} for ALL seats, in ONE git call.
    A per-file `git log` across a large corpus on a slow mount measures in
    minutes (hit live on a 67-file corpus, 2026-09-18)."""
    cwd, spec = _corpus_git(cfg)
    out = sh("git", "log", "--format=@@%cI", "--name-only", "--",
             os.path.join(spec, "*_HANDOFF_*.md"), cwd=cwd)
    times, ts = {}, ""
    for line in out.splitlines():
        if line.startswith("@@"):
            ts = _to_utc_z(line[2:].strip())
        elif line.strip():
            fn = os.path.basename(line.strip())
            if fn not in times:  # log is newest-first; first sighting wins
                times[fn] = ts
    return times


def previous_handoff(cfg):
    """(filename, ts) of this seat's newest COMMITTED handoff, or (None, None).

    Newest by COMMIT TIME: not by mtime (a shared worktree gets touched by
    others), not on-disk-newest (the file being written right now is
    uncommitted and would select itself), not alphabetical (filenames are not
    a clock)."""
    times = corpus_commit_times(cfg)
    best = (None, None)
    for fn in corpus(cfg):
        ts = times.get(fn)
        if ts and (best[1] is None or ts > best[1]):
            best = (fn, ts)
    return best


# ---------------------------------------------------------------- identity --

def seat(cfg):
    """Who is handing off. Guessing wrong files one agent's reboot under
    another's name and hands it their unfinished work — so this infers SOLO
    only when the corpus proves no second identity exists, and otherwise
    REFUSES rather than guessing."""
    for var in cfg["seat_env"]:
        v = os.environ.get(var)
        if v:
            return v.strip().upper()
    ad = adapter(cfg)
    if ad and hasattr(ad, "seat"):
        v = ad.seat()
        if v:
            return v.strip().upper()
    if cfg.get("default_seat"):
        return str(cfg["default_seat"]).upper()
    seats = {fn.split("_HANDOFF_")[0] for fn in corpus(cfg, all_seats=True)}
    if seats <= {"SOLO"}:
        return "SOLO"
    raise SystemExit(
        "REFUSED: cannot tell which agent identity is handing off, and this "
        "repo's corpus\nalready holds handoffs from: %s.\n"
        "Set one of %s, or `default_seat` in .handoff.json, or an adapter "
        "seat()." % (", ".join(sorted(seats)), "/".join(cfg["seat_env"])))


# ------------------------------------------------------------------- gates --

def gates(cfg):
    """Everything that must be true before a reboot is safe. Returns
    [problems]. Deliberately short in the portable core — a gate that fires
    every time is a gate nobody reads; projects add theirs via the adapter."""
    bad = []
    upstream = sh("git", "rev-parse", "--abbrev-ref", "@{u}")
    if upstream and not upstream.startswith("<"):
        unpushed = sh("git", "log", "--oneline", "@{u}..HEAD")
        if unpushed:
            bad.append("UNPUSHED commits — committed-but-unpushed survives "
                       "exactly one disk:\n      "
                       + unpushed.replace("\n", "\n      "))
    ad = adapter(cfg)
    if ad and hasattr(ad, "gates"):
        bad.extend(ad.gates())
    return bad


def window_is_empty(since_sha):
    """True when NOTHING has been committed since the last handoff. Saying
    HANDOFF READY is a signal; repeated on every idle turn it stops being one,
    so the steady state after a handoff is silence."""
    if not since_sha:
        return False
    return not sh("git", "log", "--oneline", "%s..HEAD" % since_sha)


def seat_family(prefix):
    """Seat name with a trailing `_REBOOT` stripped, so `BENCH` and
    `BENCH_REBOOT` are one family sharing a single newest-protected file."""
    return prefix[:-len("_REBOOT")] if prefix.endswith("_REBOOT") else prefix


def filename_ts(fn):
    """UTC-Z timestamp parsed from `<SEAT>_HANDOFF_YYYYMMDDHHMM.md`, or None.
    cull ages by this, not by commit time: `git log` here runs without
    --follow, so a file move (the 2026-09-19 relocation) resets every file's
    newest-commit clock to the move commit."""
    m = re.search(r"_HANDOFF_(\d{12})\.md$", fn)
    if not m:
        return None
    try:
        dt = datetime.datetime.strptime(m.group(1), "%Y%m%d%H%M")
    except ValueError:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def dirty_block(dirty, cap):
    """Lines for the uncommitted list: the first `cap` carry the WHOSE marker
    (so --check still demands attribution of what is shown); the remainder is
    one summary line, per top-level dir, WITHOUT the marker. Uncapped, this
    list was 80% of a measured 11.5 MB handoff corpus."""
    shown = ["%s   %s" % (l, WHOSE) for l in dirty[:cap]]
    rest = dirty[cap:]
    if not rest:
        return shown
    counts = {}
    for l in rest:
        path = l[3:].strip().split(" -> ")[-1].strip('"')
        top = path.split("/")[0] if "/" in path else "(root)"
        counts[top] = counts.get(top, 0) + 1
    by = ", ".join("%s %d" % kv for kv in
                   sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    return shown + ["... %d more uncommitted paths (by top-level dir: %s): "
                    "git status --short" % (len(rest), by)]


def owed_status(cfg):
    """(exit_code, message): is a handoff owed for this seat? The commit that
    ADDED the newest committed handoff is the window start."""
    s = seat(cfg)
    prev_name, prev_ts = previous_handoff(cfg)
    if not prev_name:
        if sh("git", "rev-parse", "--verify", "-q", "HEAD"):
            return 1, "no handoff on record for %s" % s
        return 0, "no handoff on record for %s, and no commits" % s
    since = window_start(cfg, prev_name)
    if window_is_empty(since):
        return 0, "no handoff owed: nothing committed since %s" % prev_name
    n = len(sh("git", "log", "--oneline", "%s..HEAD" % since).splitlines())
    return 1, "HANDOFF OWED: %d commit(s) since %s (%s)" % (n, prev_name,
                                                           prev_ts)


# --------------------------------------------------------- content contract --

def _section(text, title):
    """Body of `## <title>...` up to the next `## ` heading, or None."""
    m = re.search(r"^## %s.*?$(.*?)(?=^## |\Z)" % re.escape(title), text,
                  re.M | re.S)
    return m.group(1) if m else None


def _bullets(body):
    """Top-level `- ` bullets with continuation lines folded in — a check that
    only reads a bullet's first line misses a marker that wrapped."""
    out, cur = [], None
    for line in body.splitlines():
        if line.startswith("- "):
            if cur is not None:
                out.append(cur)
            cur = line
        elif cur is not None and line.strip():
            cur += " " + line.strip()
        elif not line.strip():
            # A blank line ends the bullet: trailing prose is not part of it.
            if cur is not None:
                out.append(cur)
            cur = None
    if cur is not None:
        out.append(cur)
    return out


def todo_scan(path):
    """Everything --check demands of the handoff's CONTENT. Every check maps
    to a failure mode measured in the 2026-09-17 corpus audit: unfilled
    markers, off-template files, unattributed dirty-tree dumps, prose pointers
    with no next action, traps re-explained instead of cited."""
    if not os.path.isfile(path):
        return ["the handoff file does not exist yet — run without --check "
                "first"]
    text = io.open(path, encoding="utf-8").read()
    bad = []
    n = text.count(TODO)
    if n:
        bad.append("%d unfilled section(s) still carry %s" % (n, TODO))
    n = text.count(WHOSE)
    if n:
        bad.append("%d uncommitted-file line(s) still carry %s — say whose "
                   "each one is" % (n, WHOSE))
    missing = [h for h in CANONICAL_HEADINGS
               if not re.search(r"^## %s" % re.escape(h), text, re.M)]
    if missing:
        bad.append("canonical heading(s) missing — the template is the "
                   "contract: " + ", ".join(missing))

    body = _section(text, "What is half-done, and where it stops")
    if body:
        loose = [b for b in _bullets(body) if "NEXT:" not in b]
        if loose:
            bad.append("half-done pointer(s) without a `NEXT:` action "
                       "(measured ~0% pickup without one):\n      "
                       + "\n      ".join(b[:120] for b in loose[:6]))

    body = _section(text, "Traps learned")
    if body:
        loose = [b for b in _bullets(body)
                 if not re.search(r"\((filed|see):", b)]
        if loose:
            bad.append("trap(s) with no `(filed: ...)`/`(see: ...)` citation "
                       "— append each to the lessons file (or cite where it "
                       "already lives):\n      "
                       + "\n      ".join(b[:120] for b in loose[:6]))
    return bad


# ------------------------------------------------------------------- build --

def build(cfg, since_sha, prev_name):
    s = seat(cfg)
    rng = ("%s..HEAD" % since_sha) if since_sha else "-30"
    commits = sh("git", "log", "--oneline", "--format=%h %s", rng)
    dirty = [l for l in sh("git", "status", "--short").splitlines() if l]
    upstream = sh("git", "rev-parse", "--abbrev-ref", "@{u}")
    no_upstream = not upstream or upstream.startswith("<") or "fatal" in upstream

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M")
    d = handoff_dir(cfg)
    if cfg.get("private_store") and d != os.path.join(ROOT, cfg["handoff_dir"]):
        from lib import privatestore
        privatestore._safe_dir(privatestore.root(), d)  # refuses a symlink out of the checkout
    elif not os.path.isdir(d):
        os.makedirs(d)
    out = os.path.join(d, "%s_HANDOFF_%s.md" % (s, stamp))

    L = ["# %s_HANDOFF_%s — READ FIRST on wake" % (s, stamp), ""]
    if prev_name:
        L += ["Follows `%s`. Everything below is committed%s unless a line "
              "says otherwise." % (prev_name[:-3],
                                   "" if no_upstream else " and pushed"), ""]
    else:
        L += ["First handoff for this seat.", ""]

    for title, prompt in JUDGEMENT_SECTIONS:
        L += ["## %s" % title, "", "<!-- %s -->" % prompt]
        if title.startswith("What is half-done"):
            L += ["- %s; NEXT: %s" % (TODO, TODO), ""]
        else:
            L += [TODO, ""]

    ad = adapter(cfg)
    if ad and hasattr(ad, "extra_sections"):
        for title, lines in ad.extra_sections():
            L += ["## %s" % title, ""] + list(lines) + [""]

    L += ["## Commits", "", "```"]
    # Git is the provenance: past the cap a dump stops being read and starts
    # being scrolled (one audited handoff carried 556 commit lines). The range
    # pointer reproduces the rest in one command.
    clines = (commits or "(none)").splitlines()
    cap = int(cfg["commits_cap"])
    if len(clines) > cap:
        L += clines[:cap]
        L.append("... %d more: git log --oneline %s" % (len(clines) - cap, rng))
    else:
        L += clines
    L += ["```", ""]

    L += ["## Tree state at wrap", ""]
    if no_upstream:
        L.append("- ⚠️ no upstream remote — this work exists on ONE disk")
    else:
        L.append("- upstream: %s, pushed" % upstream)
    L.append("")
    if dirty:
        # This sentence must NOT contain WHOSE itself: todo_scan counts the marker
        # across the whole file, so interpolating it here made --check unpassable on
        # any dirty tree however carefully each line was attributed.
        L += ["Uncommitted (replace each marker below with whose it is — yours, "
              "another agent's, generated):", "", "```"]
        L += dirty_block(dirty, int(cfg["dirty_cap"]))
        L += ["```"]
    else:
        L.append("Working tree clean.")
    L.append("")

    head = sh("git", "rev-parse", "HEAD")
    if head and not head.startswith("<") and out != os.path.join(ROOT, cfg["handoff_dir"],
                                                              os.path.basename(out)):
        # Kept outside this repo (private_store): record the repo commit it covers, so the
        # window is measured from it rather than from a cross-repo commit time.
        L += ["", "<!-- %s%s -->" % (ROOT_MARK, head)]
    io.open(out, "w", encoding="utf-8").write("\n".join(L) + "\n")
    return out


# ------------------------------------------------------ stranded sessions --

_RECORD_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})-([0-9A-Za-z]+)\.md$")


def _start_stamp(iso):
    """`YYYYMMDDHHMM` in UTC from a transcript timestamp, or None."""
    if not iso:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc).strftime("%Y%m%d%H%M")


def latest_handoff_stamp(cfg):
    """Newest handoff build time across ALL seats, `YYYYMMDDHHMM` UTC, from the
    filename stamp (the moment it was written; a commit can come later)."""
    stamps = [m.group(1) for m in
              (re.search(r"_HANDOFF_(\d{12})\.md$", fn)
               for fn in corpus(cfg, all_seats=True)) if m]
    return max(stamps) if stamps else None


def _transcript_dirs(conv):
    """Claude Code's transcript dirs for this repo. Its mangling is of the LOGICAL
    cwd, and git's ROOT is the real path: where ~/dev is a symlink (tower) the
    two differ, so every spelling whose realpath is ROOT is tried."""
    real = os.path.realpath(ROOT)
    cands = [ROOT, real]
    pwd = os.environ.get("PWD")
    if pwd:
        cands.append(pwd)
    cands.append(os.path.join(os.path.expanduser("~"), "dev", os.path.basename(real)))
    out = []
    for c in cands:
        if c and os.path.realpath(c) == real:
            d = conv.transcript_dir(c)
            if d not in out and os.path.isdir(d):
                out.append(d)
    return out


def stranded_sessions(cfg, current=None):
    """[(start_stamp, abs_path)] for sessions in this repo that STARTED after the
    newest handoff and are not the current one: each ended (or is still running)
    without a handoff. Conversation records (bin/conversations.py, often in the
    private overlay) are preferred; a transcript with no record is listed itself.
    Deterministic: filenames, mtimes and first-record timestamps only."""
    since = latest_handoff_stamp(cfg)
    if not since:
        return []
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        from lib import conversations as conv
    except ImportError:
        return []
    cur = (current or "")[:8]
    found = {}
    try:
        rdir = conv.record_dir(ROOT)
    except Exception:  # noqa: BLE001 — Withheld, or no overlay on this machine
        rdir = None
    if rdir and os.path.isdir(rdir):
        for fn in os.listdir(rdir):
            m = _RECORD_RE.match(fn)
            if not m:
                continue
            stamp, sid = "".join(m.groups()[:5]), m.group(6)[:8]
            if stamp > since and sid != cur:
                found[sid] = (stamp, os.path.join(rdir, fn))
    real = os.path.realpath(ROOT)
    since_epoch = (datetime.datetime.strptime(since, "%Y%m%d%H%M")
                   .replace(tzinfo=datetime.timezone.utc).timestamp())
    for d in _transcript_dirs(conv):
        for fn in os.listdir(d):
            if not fn.endswith(".jsonl"):
                continue
            p = os.path.join(d, fn)
            try:
                if os.path.getmtime(p) <= since_epoch:
                    continue  # last written before the handoff: started before it too
            except OSError:
                continue
            sid = conv.session_id(p)[:8]
            if sid == cur or sid in found:
                continue
            stamp = _start_stamp(conv.started_at(p))
            if not stamp or stamp <= since:
                continue
            where = conv.attribute(p)
            if where.root and os.path.realpath(where.root) == real:
                found[sid] = (stamp, p)
    return sorted(found.values())


def stranded_block(cfg):
    """Lines warning that a session appears to have ended without a handoff, or []."""
    rows = stranded_sessions(cfg, os.environ.get("CLAUDE_CODE_SESSION_ID"))
    if not rows:
        return []
    L = ["!" * 66,
         "%d SESSION(S) STARTED AFTER THE LATEST HANDOFF (%s UTC) AND LEFT NONE —"
         % (len(rows), latest_handoff_stamp(cfg)),
         "one appears to have ended without a handoff. Read it before trusting the one below:"]
    L += ["  %s  %s" % (st, p) for st, p in rows[-5:]]
    if len(rows) > 5:
        L.append("  ... and %d older" % (len(rows) - 5))
    L.append("!" * 66)
    return L


# ------------------------------------------------------------------- modes --

def _registry():
    """Lodestar's pointer resolver (bin/lib/handoff_pointers.py), or None where it is absent:
    this script stays portable and stdlib-only."""
    here = os.path.dirname(os.path.abspath(__file__))
    if not os.path.isfile(os.path.join(here, "lib", "handoff_pointers.py")):
        return None
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        from lib import handoff_pointers
        return handoff_pointers
    except Exception:  # noqa: BLE001 - a broken resolver must not break the ritual
        return None


def _reconcile_first():
    """One resolver pass before any pointer is shown: provably satisfied work closes first."""
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "reconcile.py")
    if not os.path.isfile(script):
        return []
    try:
        p = subprocess.run([sys.executable or "python3", script, "--budget", "4", "--no-push"],
                           capture_output=True, text=True, timeout=12)
        return [l for l in p.stdout.splitlines() if l.strip()] if p.returncode == 0 else []
    except Exception:  # noqa: BLE001
        return []


def _drafts_mod():
    """Lodestar's drafts-waiting reader (bin/lib/handoff_drafts.py, LODE-20), or None where it
    is absent or switched off (LODESTAR_HANDOFF_DRAFTS=off: the test suite, which reads no mail)."""
    if os.environ.get("LODESTAR_HANDOFF_DRAFTS", "").lower() in ("off", "0", "no"):
        return None
    here = os.path.dirname(os.path.abspath(__file__))
    if not os.path.isfile(os.path.join(here, "lib", "handoff_drafts.py")):
        return None
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        from lib import handoff_drafts
        return handoff_drafts
    except Exception:  # noqa: BLE001 - a broken reader must not break the ritual
        return None


def drafts_block(mod=None, only_if_stale=False):
    """--wake's first lines: drafts waiting on Lukas over 12 h, as OWNER NEEDED (LODE-20)."""
    mod = mod or _drafts_mod()
    if mod is None:
        return []
    try:
        led = mod.load()
        if only_if_stale and not mod.stale(led):
            return []
        return mod.block(led)
    except Exception as exc:  # noqa: BLE001
        return ["drafts ledger: unreadable (%s); ask Lukas whether a draft is waiting on him"
                % type(exc).__name__]


def draft_problems(path, mod=None):
    """--check: a draft pending his word must be surfaced and be in the DRAFTS ledger."""
    mod = mod or _drafts_mod()
    if mod is None or not path or not os.path.isfile(path):
        return []
    text = io.open(path, encoding="utf-8").read()
    if not mod.PENDING.search(text):
        return []
    try:
        return mod.problems(text, mod.load())
    except Exception:  # noqa: BLE001
        return []


def pointer_problems(path):
    """--check: pointers that hand the next session finished work (registry, when present)."""
    reg = _registry()
    if reg is None or not path or not os.path.isfile(path):
        return []
    body = _section(io.open(path, encoding="utf-8").read(),
                    "What is half-done, and where it stops") or ""
    try:
        return reg.problems(_bullets(body))
    except Exception:  # noqa: BLE001
        return []


def _issues_state_line():
    """Refresh the pilot's GitHub Issues STATE when this repo is a pilot repo (<= 10 s, never
    raises, never changes wake's exit code). Silent in any other repo."""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        from lib import issues_refresh
        line = issues_refresh.wake_line(ROOT)
        if line:
            print(line)
    except BaseException as e:  # noqa: BLE001
        if isinstance(e, KeyboardInterrupt):
            raise
        print("issues STATE: skipped (%s)" % type(e).__name__)


STALE_AGENTS_NUDGE = (
    "Stale background agents: finished ones can stay listed RUNNING (survive /clear). "
    "1) TaskStop id `list-running` (error lists running ids)  "
    "2) python3 ~/dev/Lodestar/bin/stale_agents.py <id>...  "
    "3) TaskStop each FINISHED id; never a LIVE one (name those in the handoff).")


def wake(cfg):
    """The READ side of the ritual: print this seat's latest committed handoff
    and oblige a response to every pointer. In the source audit, 46 of 76
    pointers were never consumed — a handoff nobody is obliged to read is
    write-only by construction. Run on the FIRST turn after a reboot."""
    _issues_state_line()
    print(STALE_AGENTS_NUDGE + "\n")
    prev_name, _ = previous_handoff(cfg)
    if not prev_name:
        for line in drafts_block(only_if_stale=True):
            print(line)
        print("no committed handoff for %s — nothing to wake from" % seat(cfg))
        return 0
    for line in drafts_block():
        print(line)
    for line in stranded_block(cfg):
        print(line)
    closed = _reconcile_first()
    if closed:
        print("[closed at wake: outcome already true]")
        for line in closed:
            print("  " + line)
    text = io.open(os.path.join(handoff_dir(cfg), prev_name),
                   encoding="utf-8").read()
    print(text)
    body = _section(text, "What is half-done, and where it stops") or ""
    pointers = _bullets(body)
    if pointers:
        ids = [m.group(1) for m in
               (re.match(r"- `([^`]+)`", b) for b in pointers) if m]
        states = {}
        ad = adapter(cfg)
        if ad and hasattr(ad, "pointer_state") and ids:
            states = ad.pointer_state(ids) or {}
        reg = _registry()
        if reg is not None:  # the project adapter's answer, where it gave one, wins
            for k, v in reg.statuses(pointers).items():
                states.setdefault(k, v)
        print("=" * 66)
        print("POINTER STATUS:")
        for b in pointers:
            m = re.match(r"- `([^`]+)`", b)
            key = m.group(1) if m else b[2:60]
            print("  %-48s %s" % (key, states.get(key, "unresolved — decide "
                                                       "from the text")))
        print("%d pointer(s) — pick each up, close it, or say in your first"
              % len(pointers))
        print("reply why not. Silence is how 60% of pointers died.")
    return 0


def last_drain_ts(cfg):
    """Commit time of the last lessons drain — the newest commit in which the
    inbox SHRANK (a file's deletions > additions, one `--numstat` walk). Filing
    only adds; only a curation drain removes lines or files. `lessons_file` may
    be one path or a list, and a path may be a directory of one-file-per-lesson
    files (deleting a lesson file is a drain). A commit whose subject carries
    `[no-drain]` — e.g. a migration that splits the inbox — is not a drain.
    None if no drain."""
    paths = cfg["lessons_file"]
    if isinstance(paths, str):
        paths = [paths]
    out = sh("git", "log", "--format=@@%cI %s", "--numstat", "--", *paths)
    ts, skip = "", False
    for line in out.splitlines():
        if line.startswith("@@"):
            ts, _, subject = line[2:].strip().partition(" ")
            skip = "[no-drain]" in subject
        elif line.strip() and not skip:
            parts = line.split("\t")
            if (len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit()
                    and int(parts[1]) > int(parts[0])):
                return _to_utc_z(ts)
    return None


def harvest(cfg, since_ts, out=None):
    """Extract every filled judgment 'carry forward' + 'Traps learned' section
    from ALL seats' handoffs committed after since_ts into one file — the
    pre-chewed input for a curation sitting. The source audit found 43 of 171
    lesson-claims stranded in handoffs no durable doc ever received; this
    makes the periodic drain a read, not an excavation."""
    if since_ts is None:
        since_ts = last_drain_ts(cfg)
    times = corpus_commit_times(cfg)
    rows = []
    for fn in sorted(corpus(cfg, all_seats=True)):
        ts = times.get(fn, "")
        if since_ts and ts and ts <= since_ts:
            continue
        text = io.open(os.path.join(handoff_dir(cfg), fn),
                       encoding="utf-8").read()
        secs = []
        for title in ("The one thing to carry forward", "Traps learned"):
            body = _section(text, title)
            if body and TODO not in body and body.strip():
                secs.append("### %s\n\n%s" % (title, body.strip()))
        if secs:
            rows.append("## %s (%s)\n\n%s"
                        % (fn[:-3], ts or "uncommitted", "\n\n".join(secs)))
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
    if out:
        out = os.path.abspath(out)
    else:
        out = os.path.join(handoff_dir(cfg), "harvest_%s.md" % stamp)
    hdr = ("# Handoff harvest %s — lesson sections since %s\n\n"
           "Input for a filing sitting: commit this file, file every keeper "
           "into the lessons inbox, then delete this file IN A COMMIT. That "
           "deletion is the receipt: every handoff named below then counts as "
           "lessons-filed for `--cull`. Delete it only after filing.\n\n"
           % (stamp, since_ts or "the beginning"))
    io.open(out, "w", encoding="utf-8").write(hdr + "\n\n".join(rows) + "\n")
    print("wrote %s — %d handoff(s) with lesson content since %s"
          % (os.path.relpath(out, ROOT) if out.startswith(ROOT) else out, len(rows), since_ts or "ever"))
    return 0


LESSON_SECTIONS = ("The one thing to carry forward", "Traps learned")
CITATION = re.compile(r"\((?:filed|see):", re.I)
EMPTY_LESSON = re.compile(r"^(nothing( this wave)?|none|n/?a|-+)\.?$")
MIN_LESSON_CHARS = 12

REGISTRY_PATH = None  # tests point this at a fixture; None = Lodestar's sync/registry.md


def _norm(text):
    """Lowercased, markup-free, whitespace-collapsed — so a lesson filed with
    different emphasis or wrapping still matches its handoff line."""
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"[*_`>#|]", "", text)
    text = re.sub(r"^\s*(?:[-*+]|\d+\.)\s+", "", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _lesson_spans(text, titles=LESSON_SECTIONS):
    """(unit, last_line_index) for each lesson-bearing unit of the given sections:
    each bullet with its indented continuation lines folded in, and each prose
    PARAGRAPH (consecutive non-blank, non-bullet lines) folded into one unit —
    so a wrapped paragraph is one lesson needing one citation, not one per line.
    Comments, code fences, unfilled markers and 'nothing this wave' are skipped.
    The index is the unit's last line in `text.splitlines()`, where a citation
    goes."""
    # Blank comments out but keep their newlines, so indices still address `text`.
    lines = re.sub(r"<!--.*?-->", lambda m: "\n" * m.group(0).count("\n"), text,
                   flags=re.S).splitlines()
    spans = []
    for title in titles:
        head = re.compile(r"^## %s" % re.escape(title))
        i = next((n for n, l in enumerate(lines) if head.match(l)), None)
        if i is None:
            continue
        cur, kind, last = None, None, None
        for n in range(i + 1, len(lines)):
            line = lines[n]
            if line.startswith("## "):
                break
            if TODO in line or line.strip().startswith("```"):
                continue
            if re.match(r"^(?:[-*+]|\d+\.)\s", line):
                if cur is not None:
                    spans.append((cur, last))
                cur, kind, last = line, "bullet", n
            elif not line.strip():
                if cur is not None:
                    spans.append((cur, last))
                cur, kind = None, None
            elif cur is not None and (kind == "prose" or line[:1].isspace()):
                cur, last = cur + " " + line.strip(), n
            else:
                if cur is not None:
                    spans.append((cur, last))
                cur, kind, last = line, "prose", n
        if cur is not None:
            spans.append((cur, last))
    return [(u, n) for u, n in spans
            if len(_norm(u)) >= MIN_LESSON_CHARS and not EMPTY_LESSON.match(_norm(u))]


def lesson_units(text, titles=LESSON_SECTIONS):
    """The lesson-bearing units of a handoff's carry-forward and Traps sections
    (see `_lesson_spans`)."""
    return [u for u, _ in _lesson_spans(text, titles)]


def _unfiled(units, corpus_text):
    return [u for u in units if not CITATION.search(u) and _norm(u) not in corpus_text]


def _inside_root(path):
    return os.path.realpath(path).startswith(os.path.realpath(ROOT) + os.sep)


def file_lessons(cfg, path, today=None):
    """Auto-file a handoff's unfiled lessons, so they stop piling up for a
    retrospective. Every carry-forward/Traps unit with no `(filed: …)`/`(see: …)`
    citation and not already in the lessons inbox (normalized-text dedup) is
    appended to the inbox, and the handoff line gets `(filed: <INBOX>)`.

    Only when the handoff and the inbox share a home: a handoff on the private host
    (`private_store`) may carry names a github.com inbox must never receive, so
    there nothing is copied and an unfiled carry-forward is a refusal instead —
    file a de-identified line by hand and cite it. (Uncited Traps are refused
    by `todo_scan` either way.) Returns [problems]."""
    if not path or not os.path.isfile(path):
        return []
    text = io.open(path, encoding="utf-8").read()
    corpus_text = lessons_corpus(cfg)
    loose = [(u, n) for u, n in _lesson_spans(text)
             if _unfiled([u], corpus_text)]
    if not loose:
        return []
    inbox = os.path.join(ROOT, _lesson_paths(cfg)[0])
    label = os.path.splitext(os.path.basename(inbox.rstrip(os.sep)))[0]
    if not (_inside_root(path) and _inside_root(inbox)):
        carry = set(lesson_units(text, LESSON_SECTIONS[:1]))
        loose = [u for u, _ in loose if u in carry]
        if not loose:
            return []
        return ["carry-forward lesson(s) not in the lessons inbox — this handoff "
                "lives on the private host, so nothing is copied automatically: file a "
                "de-identified line in %s and end the handoff's paragraph with "
                "`(filed: %s)`:\n      " % (_lesson_paths(cfg)[0], label)
                + "\n      ".join(u[:120] for u in loose[:6])]
    today = today or datetime.date.today().isoformat()
    stem = os.path.basename(path)[:-3]
    entries, seen = [], set()
    for u, _ in loose:
        key = _norm(u)
        if key in seen:
            continue
        seen.add(key)
        body = re.sub(r"^\s*(?:[-*+]|\d+\.)\s+", "", u).strip()
        entries.append("- %s (handoff %s): %s" % (today, stem, body))
    if os.path.isdir(inbox):
        for k, e in enumerate(entries, 1):
            io.open(os.path.join(inbox, "%s-%s-%d.md" % (today, stem, k)), "w",
                    encoding="utf-8").write(e + "\n")
    else:
        old = io.open(inbox, encoding="utf-8").read() if os.path.exists(inbox) else ""
        sep = "" if not old or old.endswith("\n\n") else ("\n" if old.endswith("\n") else "\n\n")
        io.open(inbox, "w", encoding="utf-8").write(old + sep + "\n".join(entries) + "\n")
    lines = text.splitlines(True)
    for _, n in loose:
        eol = "\n" if lines[n].endswith("\n") else ""
        lines[n] = lines[n].rstrip("\r\n").rstrip() + " (filed: %s)" % label + eol
    io.open(path, "w", encoding="utf-8").write("".join(lines))
    rel = os.path.relpath(inbox, ROOT)
    print("auto-filed %d lesson(s) from %s into %s and cited them `(filed: %s)`"
          % (len(entries), os.path.basename(path), rel, label))
    return ["auto-filed lesson(s) into %s — commit it (and the handoff), then "
            "re-run --check" % rel]


def _lesson_paths(cfg):
    paths = cfg["lessons_file"]
    return [paths] if isinstance(paths, str) else list(paths)


def lessons_corpus(cfg):
    """Normalized text of everything EVER filed in the lessons inbox: every line
    any commit added (so a lesson filed and later drained still counts) plus the
    working tree."""
    paths = _lesson_paths(cfg)
    added = [l[1:] for l in sh("git", "log", "-p", "--format=", "--no-color", "--",
                                 *paths).splitlines()
             if l.startswith("+") and not l.startswith("+++")]
    for rel in paths:
        p = os.path.join(ROOT, rel)
        files = ([os.path.join(dp, f) for dp, _, fs in os.walk(p) for f in fs]
                 if os.path.isdir(p) else [p])
        for f in files:
            try:
                added.append(io.open(f, encoding="utf-8", errors="replace").read())
            except (IOError, OSError):
                pass
    return _norm("\n".join(added))


def drained_harvest_names(cfg):
    """Handoff filenames named in a harvest file that was committed and then
    deleted in a later commit — deleting a harvest is the receipt that its
    keepers were filed."""
    cwd, spec = _corpus_git(cfg)
    out = sh("git", "log", "--diff-filter=D", "--format=@@%H", "--name-only", "--",
             os.path.join(spec, "harvest_*.md"), cwd=cwd)
    names, sha = set(), None
    for line in out.splitlines():
        if line.startswith("@@"):
            sha = line[2:].strip()
        elif line.strip() and sha:
            body = sh("git", "show", "%s^:%s" % (sha, line.strip()), cwd=cwd)
            names.update(m + ".md" for m in
                         re.findall(r"^## (\S+_HANDOFF_\d{12}) \(", body, re.M))
    return names


def lessons_filed(text, fn, corpus_text, drained):
    """(filed?, why). Filed = no lesson content, or named in a drained harvest,
    or every lesson unit is cited `(filed: …)`/`(see: …)` or found in the inbox."""
    units = lesson_units(text)
    if not units:
        return True, "no lessons"
    if fn in drained:
        return True, "harvest drained"
    missing = [u for u in units
               if not CITATION.search(u) and _norm(u) not in corpus_text]
    if missing:
        return False, "%d unfiled lesson line(s)" % len(missing)
    return True, "lessons in inbox"


def follower(root):
    """True when the sync registry row for this checkout has Send off: this
    machine follows that repo and must never delete its files. Unregistered,
    or no registry available: False."""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        from lib import registry
        rows = registry.by_path(REGISTRY_PATH)
    except Exception:  # noqa: BLE001 — no registry means no sync role to honour
        return False
    e = rows.get(os.path.realpath(root))
    return bool(e) and not e.send


def cull(cfg, apply_):
    """Handoffs whose job is done: superseded by a newer committed handoff of
    the same seat family at least cull_days ago (measured from the newer one's
    stamp, not this file's age), with their lessons filed. Without --apply this
    only lists; git is the archive either way. Never deletes in a follower repo."""
    now = datetime.datetime.now(datetime.timezone.utc)
    grace = datetime.timedelta(days=int(cfg["cull_days"]))
    times = corpus_commit_times(cfg)
    fams = {}
    for fn in corpus(cfg, all_seats=True):
        ts = times.get(fn, "")
        if ts:  # uncommitted handoffs neither supersede nor get culled
            # Order by the filename's own stamp: files added in one commit tie on
            # commit time, and a move commit resets it.
            fams.setdefault(seat_family(fn.split("_HANDOFF_")[0]), []).append(
                (filename_ts(fn) or ts, fn))
    corpus_text, drained = None, None
    cands, young, unfiled = [], 0, 0
    for st, rows in fams.items():
        rows.sort()
        for (_, fn), (sup_key, sup_fn) in zip(rows, rows[1:]):
            sup = datetime.datetime.fromisoformat(sup_key.replace("Z", "+00:00"))
            if now - sup < grace:
                young += 1
                continue
            if corpus_text is None:
                corpus_text, drained = lessons_corpus(cfg), drained_harvest_names(cfg)
            text = io.open(os.path.join(handoff_dir(cfg), fn), encoding="utf-8",
                           errors="replace").read()
            ok, why = lessons_filed(text, fn, corpus_text, drained)
            if not ok:
                unfiled += 1
                continue
            cands.append((fn, sup_fn, (now - sup).days, why))
    tail = "(%d within the %sd grace, %d with unfiled lessons)" % (
        young, cfg["cull_days"], unfiled)
    is_follower = follower(ROOT)
    if not cands:
        print("nothing to cull %s" % tail)
    for fn, sup_fn, days, why in sorted(cands):
        print("%s  (superseded by %s %dd ago; %s)" % (fn, sup_fn, days, why))
    if cands:
        print("%d candidate(s) %s." % (len(cands), tail))
    if is_follower:
        print("FOLLOWER repo (sync Send off): this machine never culls it%s."
              % ("; --apply refused" if apply_ else ""))
        return 1 if apply_ else 0
    if apply_ and cands:
        for fn, _, _, _ in cands:
            os.remove(os.path.join(handoff_dir(cfg), fn))
        print("deleted %d — commit the deletions with explicit paths; git is "
              "the archive." % len(cands))
    elif cands:
        print("`--cull --apply` deletes them.")
    return 0


# -------------------------------------------------------------------- main --

def main():
    global ROOT
    ROOT = _find_root()
    ap = argparse.ArgumentParser(
        description="Agent handoff ritual — see docs/HANDOFF.md")
    ap.add_argument("--check", action="store_true",
                    help="gates + content scan only; exit 1 if not ready")
    ap.add_argument("--wake", action="store_true",
                    help="print the latest handoff + pointer status — run on "
                         "the first turn after a reboot")
    ap.add_argument("--harvest", action="store_true",
                    help="extract lesson sections since the last lessons-file "
                         "drain into one file; WRITES harvest_<date>.md into "
                         "the handoff dir (use --out, or move it out)")
    ap.add_argument("--out", help="with --harvest: write the harvest file "
                    "here instead of into the handoff dir")
    ap.add_argument("--owed", action="store_true",
                    help="is a handoff owed? commits since this seat's newest "
                         "committed handoff; exit 1 if so")
    ap.add_argument("--cull", action="store_true",
                    help="list handoffs superseded >= cull_days ago whose "
                         "lessons are filed")
    ap.add_argument("--apply", action="store_true",
                    help="with --cull: actually delete the candidates")
    ap.add_argument("--since", help="commit to measure the window from")
    ap.add_argument("--force", action="store_true",
                    help="write the skeleton even if a gate fails")
    a = ap.parse_args()
    cfg = config()

    if a.harvest:
        since_ts = None
        if a.since:
            raw = sh("git", "log", "-1", "--format=%cI", a.since)
            since_ts = _to_utc_z(raw) if raw and not raw.startswith("<") \
                else a.since
        return harvest(cfg, since_ts, a.out)
    if a.owed:
        code, msg = owed_status(cfg)
        print(msg)
        return code
    if a.cull:
        return cull(cfg, a.apply)
    if a.wake:
        return wake(cfg)

    prev_name, _prev_ts = previous_handoff(cfg)
    since_sha = a.since
    if not since_sha and prev_name:
        raw = window_start(cfg, prev_name)
        since_sha = raw or None

    # --check validates the handoff THIS session wrote: the most recently
    # touched one — not the alphabetically last; filenames are not a clock.
    cands = [os.path.join(handoff_dir(cfg), fn) for fn in corpus(cfg)]
    newest = max(cands, key=os.path.getmtime) if cands else None

    if prev_name and window_is_empty(since_sha) and not a.force:
        print("ALREADY HANDED OFF — nothing committed since\n  %s"
              % os.path.join(handoff_dir(cfg), prev_name))
        print("\nThat handoff still stands. Do NOT write another and do NOT "
              "say HANDOFF READY\nagain; say it once, then stay quiet until "
              "real work comes in. --force overrides.")
        return 0

    problems = gates(cfg)

    if a.check:
        problems += (file_lessons(cfg, newest) + todo_scan(newest)
                     + pointer_problems(newest) + draft_problems(newest)) if newest \
            else ["no handoff file written yet"]
        if problems:
            print("NOT READY — %d thing(s) to fix:" % len(problems))
            for p in problems:
                print("  * %s" % p)
            print("\nFix these, then say HANDOFF READY yourself. This script "
                  "never says it for you.")
            return 1
        print("gates pass and the content contract holds in\n  %s" % newest)
        print("\n" + STALE_AGENTS_NUDGE)
        print("\nThe judgement is yours: if this really is a clean stopping "
              "point, say HANDOFF READY.")
        return 0

    if problems and not a.force:
        print("REFUSED — not safe to hand off yet:")
        for p in problems:
            print("  * %s" % p)
        print("\n--force writes the skeleton anyway (and records these as "
              "open).")
        return 1

    out = build(cfg, since_sha, prev_name)
    inside = out.startswith(ROOT + os.sep)
    print("wrote %s" % (os.path.relpath(out, ROOT) if inside else out))
    print(STALE_AGENTS_NUDGE)
    print("\nFill these in — a script cannot:")
    for title, prompt in JUDGEMENT_SECTIONS:
        print("  %-38s %s" % (title, prompt.split(".")[0] + "."))
    if inside:
        print("\nThen: handoff.py --check, commit, push, and say HANDOFF READY.")
    else:
        print("\nThen: handoff.py --check, commit and push it in %s (the private host — it "
              "never goes in this repo), push this repo too, and say HANDOFF READY."
              % _corpus_git(cfg)[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
