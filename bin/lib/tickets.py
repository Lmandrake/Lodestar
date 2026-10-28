"""Tickets: one markdown file per ticket in each project's own git home. Deterministic Python.

Design notes are kept in the private store; decision D30 in
`docs/DECISIONS.md` (supersedes D29 T1/T2, keeps T3: mail-born obligations stay in the Reckoning).

    tickets/LODE-12.md     flat frontmatter (lib/frontmatter.py) + markdown body + "## Log"

* **Where a project's tickets live** is declared in `sync/ticket-homes.md` (Name | Code | Home):
  `repo` (the project repo's own `tickets/`), `overlay` (the private-store overlay
  `projects/<Repo>/tickets/`, placed by `prosehome.place`: private work on a github.com repo),
  `redirect:<Name>` (another project's home), `none` (never written). An undeclared project is
  refused by name. A `repo` home on github.com is "public": every write passes the text guard
  (proseguard.MARKERS) and `--private` sends that one ticket to the overlay instead.
* **Ids** are `<CODE>-<n>`. A number is claimed by a non-force push of an empty commit to
  `refs/tickets/claims/<ID>` on the project's own remote: git refuses the second claimer
  (measured on github.com and the private forge, 2026-10-21), which retries with n+1. Offline:
  `<CODE>-x<5 base32>`, unique by construction.
* **Many agents**: one file per ticket, a per-code flock, commits with a pathspec (never sweeping
  another agent's staged work), push without pull; a rejected push leaves the commit for the sweep.
* **Commit messages carry the id and verb only**, never a title (github.com history is public).
* **Blockers are not only tickets** (LODE-37, 2026-10-24). `blocked_by` holds comma-separated
  entries of four kinds, told apart by shape (`classify`): a ticket id; a **date** (`until
  YYYY-MM-DD`, clears when today reaches it); an **external issue** (stored as the full URL on
  github.com or the private host, clears when `gh api --hostname` says closed, "unknown" when
  unreachable, never guessed done); anything else is a **free-text condition** that never clears
  by itself and is ended with `link ID --met TEXT --said/--evidence`. Only ticket ids take part in
  the cycle check and the inverse `blocks` side.
* **Every ticket carries a `check`** (2026-10-22, `lib/outcome.py`): an outcome check spec in
  compact JSON, or an explicit coverage gap when its creator gave none (`create`). The resolver
  (`bin/reconcile.py`) closes a live ticket as `done` when its check is SATISFIED, through
  `move(..., expect_version=...)`; it never drops one.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

try:
    from lib import frontmatter, gitutil, mdtable, paths, registry
except ImportError:  # pragma: no cover
    import frontmatter  # type: ignore
    import gitutil  # type: ignore
    import mdtable  # type: ignore
    import paths  # type: ignore
    import registry  # type: ignore
try:
    from lib import ticketname
except ImportError:  # pragma: no cover
    import ticketname  # type: ignore

TYPES = ("build", "bug", "repair", "action", "note")
STATUSES = ("proposed", "open", "active", "blocked", "done", "dropped")
LIVE = ("open", "active", "blocked")
TERMINAL = ("done", "dropped")
SOURCES = ("manual", "agent", "bearing-proposed", "migrated")   # plus need:<project>, migrated:<x>
FIELDS = ("id", "title", "project", "type", "status", "owner", "created", "updated", "due",
          "act_by", "effort", "next", "snoozed_until", "blocks", "blocked_by", "relates", "needs",
          "requested_by", "links", "source", "check", "former_id")
LINK_FIELDS = ("blocks", "blocked_by", "relates", "needs")
ME = ("lukas", "mandrake")
HOMES_FILE = "sync/ticket-homes.md"
CLAIM_PREFIX = "refs/tickets/claims/"
LEGACY_ID_RE = re.compile(r"^([A-Z][A-Z0-9]{1,5})-(\d+|x[a-z2-7]{5})$")   # LODE-35: retired form
NAMED_ID_RE = re.compile(r"^[A-Z0-9]+(?:_[A-Z0-9]+){2,6}$")                 # SUBJECT_INTENT_TWIST


class _IdRe:
    """`ID_RE` for both forms: group(1) is the project code for a legacy id only."""
    def match(self, s):
        return LEGACY_ID_RE.match(s or "") or (
            NAMED_ID_RE.match(s or "") and ticketname.is_named(s))


ID_RE = _IdRe()
_DATE = re.compile(r"^\d{4}-\d\d-\d\d(?:T\d\d:\d\d)?$")
_EFFORT = re.compile(r"^(\d+)\s*(m|h|d)$")
FACE_HORIZON_DAYS = 7
FACE_SLOTS = 14


class Refused(Exception):
    """A ticket operation refused, by name. Never a silent partial write."""


class Home(NamedTuple):
    project: str      # registry Name
    pid: str          # registry Id
    code: str
    kind: str         # repo | overlay
    dir: str          # the tickets/ directory
    repo: str         # the git checkout the files are committed in
    claim_repo: str   # the checkout whose remote holds this code's claim refs
    public: bool      # the home pushes to github.com: text guard applies


# --------------------------------------------------------------------------- homes

def _root(root: Optional[str] = None) -> str:
    return root or paths.root()


def homes_table(root: Optional[str] = None) -> List[Dict[str, str]]:
    path = os.path.join(_root(root), HOMES_FILE)
    if not os.path.exists(path):
        raise Refused("no %s: the ticket homes table is missing" % path)
    with open(path, encoding="utf-8") as fh:
        rows = mdtable.parse(fh.read())
    codes: Dict[str, str] = {}
    for r in rows:
        c = (r.get("code") or "").strip()
        if c and c in codes and codes[c] != r.get("name"):
            raise Refused("ticket code %s is declared twice (%s, %s)" % (c, codes[c], r.get("name")))
        codes[c] = r.get("name") or ""
    return rows


def _entries(root: Optional[str] = None):
    path = os.path.join(_root(root), "sync", "registry.md")
    return {e.name: e for e in registry.entries(path)}


def _row(project: str, rows: List[Dict[str, str]], entries) -> Dict[str, str]:
    want = project.strip().lower()
    for r in rows:
        name = r.get("name") or ""
        e = entries.get(name)
        keys = {name.lower(), (r.get("code") or "").lower()}
        if e is not None and e.repo_id:
            keys.add(e.repo_id.lower())
        if want in keys:
            return r
    raise Refused("no ticket home declared for %r: add a row to %s" % (project, HOMES_FILE))


def _is_public(repo: str) -> bool:
    try:
        from lib import reckoning
    except ImportError:  # pragma: no cover
        import reckoning  # type: ignore
    return not reckoning.private_only(repo)


def _overlay(name: str, repo: str) -> Tuple[str, str]:
    """(tickets dir, private-store checkout) for a project's overlay, or Refused by name."""
    try:
        from lib import privatestore, prosehome
    except ImportError:  # pragma: no cover
        import privatestore  # type: ignore
        import prosehome  # type: ignore
    place = prosehome.place(repo, "tickets")
    if place.where == prosehome.WITHHELD:
        raise Refused("tickets for %s are withheld on this machine: %s" % (name, place.reason))
    if place.where == prosehome.REPO:
        return place.path, repo
    return place.path, privatestore.root()


def homes_for(project: str, root: Optional[str] = None, private: bool = False,
              _seen: Tuple[str, ...] = ()) -> List[Home]:
    """The project's homes on this machine, primary first. `private` puts the overlay first."""
    rows, entries = homes_table(root), _entries(root)
    r = _row(project, rows, entries)
    name, code, kind = r.get("name") or "", (r.get("code") or "").strip(), (r.get("home") or "").strip()
    if kind.startswith("redirect:"):
        target = kind.split(":", 1)[1].strip()
        if target in _seen:
            raise Refused("ticket home redirect loop at %s" % name)
        return homes_for(target, root, private, _seen + (name,))
    if kind in ("", "none"):
        raise Refused("%s keeps no tickets (%s): %s" % (name, HOMES_FILE, r.get("notes") or "home none"))
    if not code or not re.match(r"^[A-Z][A-Z0-9]{1,5}$", code):
        raise Refused("%s has no valid ticket code in %s" % (name, HOMES_FILE))
    e = entries.get(name)
    if e is None:
        raise Refused("%s is in %s but not in sync/registry.md" % (name, HOMES_FILE))
    repo = os.path.realpath(os.path.expanduser(e.path))
    if not os.path.isdir(os.path.join(repo, ".git")) and not os.path.isfile(os.path.join(repo, ".git")):
        raise Refused("%s has no git checkout at %s" % (name, repo))
    pid = e.repo_id or name.lower()
    if kind == "overlay":
        d, top = _overlay(name, repo)
        return [Home(name, pid, code, "overlay", d, top, repo, _is_public(top))]
    if kind != "repo":
        raise Refused("%s: unknown ticket home %r" % (name, kind))
    public = _is_public(repo)
    primary = Home(name, pid, code, "repo", os.path.join(repo, "tickets"), repo, repo, public)
    out = [primary]
    if public:
        try:
            d, top = _overlay(name, repo)
            if top != repo:
                out.append(Home(name, pid, code, "overlay", d, top, repo, False))
        except Refused:
            if private:
                raise
    if private:
        if len(out) < 2:
            raise Refused("%s has no private overlay on this machine for a --private ticket" % name)
        out = [out[1], out[0]]
    return out


HOMES_CACHE_S = 3600


def all_homes(root: Optional[str] = None, cached: bool = False) -> List[Home]:
    """Every home on this machine (skipping projects that cannot be placed here).

    `cached` (the Companion collector's tick): reuse a machine-local list for up to an hour,
    rebuilt when either table changes. Resolving every home costs ~150 git spawns (7.6 s at load
    10, measured 2026-10-21), and placements change only when a remote or table does."""
    if cached:
        import json
        import time
        cache = os.path.join(paths.state_dir(), "tickets", "homes-cache.json")
        tables = [os.path.join(_root(root), f) for f in (HOMES_FILE, "sync/registry.md")]
        stamp = [os.path.getmtime(t) if os.path.exists(t) else 0 for t in tables]
        got = {}
        try:
            with open(cache, encoding="utf-8") as fh:
                got = json.load(fh)
            if got.get("stamp") == stamp and time.time() - got.get("at", 0) < HOMES_CACHE_S:
                return [Home(*h) for h in got["homes"]]
        except (OSError, ValueError, TypeError):
            got = {}
        # a rebuild that failed (git timeouts at load 40) must not be retried every tick, and the
        # stale list is far better than no ticket row: retry in 10 minutes, serve stale meanwhile
        try:
            if got.get("homes") and time.time() - os.path.getmtime(cache + ".failed") < 600:
                return [Home(*h) for h in got["homes"]]
        except OSError:
            pass
        try:
            homes = all_homes(root)
        except Exception:  # noqa: BLE001 - GitTimeout and friends
            if not got.get("homes"):
                raise
            try:
                open(cache + ".failed", "w").close()
            except OSError:
                pass
            return [Home(*h) for h in got["homes"]]
        try:
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            tmp = cache + ".tmp.%d" % os.getpid()
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"stamp": stamp, "at": time.time(), "homes": [list(h) for h in homes]}, fh)
            os.replace(tmp, cache)
        except OSError:
            pass
        return homes
    out, seen = [], set()
    for r in homes_table(root):
        kind = (r.get("home") or "").strip()
        if kind in ("", "none") or kind.startswith("redirect:"):
            continue
        try:
            for h in homes_for(r.get("name") or "", root):
                if h.dir not in seen:
                    seen.add(h.dir)
                    out.append(h)
        except Refused:
            continue
    return out


def home_for_repo(repo: str, root: Optional[str] = None) -> List[Home]:
    """The homes of the project whose checkout is `repo` (Orders' reader), or []."""
    real = os.path.realpath(os.path.expanduser(repo))
    entries = _entries(root)
    for r in homes_table(root):
        e = entries.get(r.get("name") or "")
        if e is not None and os.path.realpath(os.path.expanduser(e.path)) == real:
            try:
                return homes_for(r["name"], root)
            except Refused:
                return []
    return []


# --------------------------------------------------------------------------- read

def parse(text: str) -> Tuple[Dict[str, str], str]:
    return frontmatter.parse(text)


def read(path: str) -> Dict[str, str]:
    with open(path, encoding="utf-8") as fh:
        fields, body = parse(fh.read())
    fields = dict(fields)
    fields["_path"] = path
    fields["_body"] = body
    return fields


def _ticket_files(d: str) -> List[str]:
    try:
        names = os.listdir(d)
    except OSError:
        return []
    return sorted(os.path.join(d, n) for n in names
                  if n.endswith(".md") and ID_RE.match(n[:-3]))


def load(homes: Sequence[Home]) -> List[Dict[str, str]]:
    out = []
    for h in homes:
        for p in _ticket_files(h.dir):
            try:
                t = read(p)
            except (OSError, ValueError):
                continue
            t["_home"] = h.kind
            t["_code"] = h.code
            out.append(t)
    return out


def _num(tid: str) -> int:
    m = LEGACY_ID_RE.match(tid)
    return int(m.group(2)) if m and m.group(2).isdigit() else 0


def find(tid: str, root: Optional[str] = None) -> Tuple[Dict[str, str], Home]:
    if not ID_RE.match(tid or ""):
        raise Refused("not a ticket id: %r" % tid)
    m = LEGACY_ID_RE.match(tid)
    if not m:                      # a named id carries no project code: look in every home
        for h in all_homes(root):
            p = os.path.join(h.dir, tid + ".md")
            if os.path.exists(p):
                return read(p), h
        raise Refused("%s not found in any ticket home on this machine" % tid)
    for r in homes_table(root):
        if (r.get("code") or "").strip() == m.group(1):
            for h in homes_for(r["name"], root):
                p = os.path.join(h.dir, tid + ".md")
                if os.path.exists(p):
                    return read(p), h
            raise Refused("%s not found in %s's homes on this machine" % (tid, r["name"]))
    raise Refused("no project uses ticket code %s" % m.group(1))


def split(v: str) -> List[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


# --------------------------------------------------------------------------- validate

def guard_text(home: Home, *texts: str) -> None:
    """A public (github.com) home refuses private markers by name. A backstop: private-work projects
    never have a public home at all (D30)."""
    if not home.public:
        return
    try:
        from lib import proseguard
    except ImportError:  # pragma: no cover
        import proseguard  # type: ignore
    lines = [ln for t in texts for ln in (t or "").splitlines()]
    hits = proseguard.markers(lines)
    if hits:
        raise Refused("refused: %s pushes to github.com and this text carries %s; "
                      "file it with --private (overlay) or rephrase" % (home.project, ", ".join(hits)))


def _valid_date(v: str) -> bool:
    if not v:
        return True
    if not _DATE.match(v):
        return False
    try:
        datetime.strptime(v[:10], "%Y-%m-%d")
    except ValueError:
        return False
    return True


def problems(t: Dict[str, str], known: Optional[Iterable[str]] = None) -> List[str]:
    out = []
    tid = t.get("id") or ""
    if not ID_RE.match(tid):
        out.append("bad id %r" % tid)
    if t.get("_path") and os.path.basename(t["_path"]) != tid + ".md":
        out.append("file name is not %s.md" % tid)
    if not (t.get("title") or "").strip():
        out.append("no title")
    if t.get("type") not in TYPES:
        out.append("type %r not in %s" % (t.get("type"), "/".join(TYPES)))
    if t.get("status") not in STATUSES:
        out.append("status %r not in %s" % (t.get("status"), "/".join(STATUSES)))
    for f in ("due", "act_by", "snoozed_until"):
        if not _valid_date(t.get(f) or ""):
            out.append("%s %r is not YYYY-MM-DD[THH:MM]" % (f, t.get(f)))
    if t.get("effort") and not _EFFORT.match(t["effort"]):
        out.append("effort %r is not Nm/Nh/Nd" % t["effort"])
    if t.get("status") in TERMINAL and not (t.get("close_reason") or "").strip():
        out.append("%s without close_reason" % t.get("status"))
    if t.get("check"):
        try:
            from lib import outcome
        except ImportError:  # pragma: no cover
            import outcome  # type: ignore
        try:
            outcome.validate_spec(t["check"])
        except ValueError as e:
            out.append("check: %s" % e)
    if known is not None:
        known = set(known)
        for f in LINK_FIELDS:
            for x in split(t.get(f) or ""):
                if f == "blocked_by" and kind_of(x) != "ticket":
                    continue
                if x not in known:
                    out.append("%s -> %s dangling" % (f, x))
    return out


# --------------------------------------------------------------------------- git

_TRANSIENT = ("Connection reset", "Connection closed", "kex_exchange_identification",
              "Could not resolve host", "Operation timed out", "timed out after")


def _git(repo: str, *args: str, timeout: int = 120, tries: int = 4) -> Tuple[int, str, str]:
    """git, retried with backoff on a transient network failure (GitHub resets bursts of ssh
    connections: measured 2026-10-21 filing 13 tickets in a row). Never retries a refusal."""
    import time
    for i in range(tries):
        rc, out, err = gitutil.run(repo, *args, timeout=timeout)
        if rc == 0 or not any(m in err for m in _TRANSIENT) or i == tries - 1:
            return rc, out, err
        time.sleep(2 * (i + 1) ** 2)
    return rc, out, err


def _remote(repo: str) -> Optional[str]:
    code, out, _ = _git(repo, "remote")
    names = out.split()
    return "origin" if "origin" in names else (names[0] if names else None)


def remote_claims(repo: str, code: str) -> List[int]:
    remote = _remote(repo)
    if not remote:
        raise Refused("%s has no remote to claim ticket ids on; pass --offline" % repo)
    rc, out, err = _git(repo, "ls-remote", remote, CLAIM_PREFIX + code + "-*", timeout=60)
    if rc != 0:
        raise Refused("cannot read claims from %s (%s); pass --offline" % (repo, err.strip()[:160]))
    nums = []
    for line in out.splitlines():
        ref = line.split("\t")[-1]
        nums.append(_num(ref[len(CLAIM_PREFIX):]))
    return nums


def _empty_commit(repo: str, message: str) -> str:
    rc, tree, err = _git(repo, "hash-object", "-t", "tree", "-w", "/dev/null")
    if rc != 0:
        raise Refused("git hash-object failed: %s" % err.strip())
    rc, sha, err = _git(repo, "commit-tree", tree.strip(), "-m", message)
    if rc != 0:
        raise Refused("git commit-tree failed: %s" % err.strip())
    return sha.strip()


def allocate(homes: Sequence[Home], n: int = 1, offline: bool = False) -> List[str]:
    """`n` fresh ids for the homes' code. Claimed atomically on the project's remote."""
    code = homes[0].code
    local = [_num(os.path.basename(p)[:-3]) for h in homes for p in _ticket_files(h.dir)]
    if offline:
        taken = {os.path.basename(p)[:-3] for h in homes for p in _ticket_files(h.dir)}
        out = []
        while len(out) < n:
            tid = "%s-x%s" % (code, "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz234567")
                                            for _ in range(5)))
            if tid not in taken and tid not in out:
                out.append(tid)
        return out
    repo = homes[0].claim_repo
    remote = _remote(repo)
    start = max([0] + local + remote_claims(repo, code)) + 1
    for attempt in range(25):
        ids = ["%s-%d" % (code, start + i) for i in range(n)]
        sha = _empty_commit(repo, "ticket claim %s..%s" % (ids[0], ids[-1]) if n > 1
                            else "ticket claim %s" % ids[0])
        specs = ["%s:%s%s" % (sha, CLAIM_PREFIX, i) for i in ids]
        rc, out, err = _git(repo, "push", "--atomic", "--quiet", remote, *specs, timeout=120)
        if rc == 0:
            return ids
        if "rejected" in err or "already exists" in err or "fetch first" in err:
            start = max([start + n] + remote_claims(repo, code)) + 1 if n > 1 else start + 1
            continue
        raise Refused("cannot claim %s on %s (%s); pass --offline" % (ids[0], remote, err.strip()[:200]))
    raise Refused("could not claim an id for %s after 25 tries" % code)


def claim_name(homes: Sequence[Home], name: str, offline: bool = False) -> str:
    """Claim a SUBJECT_INTENT_TWIST id: refuse a name any home holds, then take the ref atomically."""
    taken = {os.path.basename(p)[:-3] for h in all_homes() for p in _ticket_files(h.dir)}
    probs = ticketname.problems(name, taken)
    if probs:
        raise Refused("bad ticket name %s: %s" % (name, "; ".join(probs)))
    if offline:
        return name
    repo = homes[0].claim_repo
    remote = _remote(repo)
    if not remote:
        raise Refused("%s has no remote to claim ticket ids on; pass --offline" % repo)
    sha = _empty_commit(repo, "ticket claim %s" % name)
    rc, out, err = _git(repo, "push", "--atomic", "--quiet", remote,
                        "%s:%s%s" % (sha, CLAIM_PREFIX, name), timeout=120)
    if rc != 0:
        if "rejected" in err or "already exists" in err:
            raise Refused("ticket name %s is already claimed: give it its own twist" % name)
        raise Refused("cannot claim %s on %s (%s); pass --offline" % (name, remote, err.strip()[:200]))
    return name


def _lock(code: str):
    try:
        from lib import flockwait
    except ImportError:  # pragma: no cover
        import flockwait  # type: ignore
    d = os.path.join(paths.state_dir(), "tickets")
    os.makedirs(d, exist_ok=True)
    fh = open(os.path.join(d, "%s.lock" % code), "w")
    flockwait.exclusive(fh, label="tickets " + code, timeout=90)
    return fh


def commit(home: Home, files: Sequence[str], message: str, push: bool = True) -> str:
    """Commit exactly `files` in the home's checkout, then push. Returns one status word."""
    rels = [os.path.relpath(f, home.repo) for f in files]
    rc, _, err = _git(home.repo, "add", "-A", "--", *rels)    # -A: a renamed ticket's old path is a deletion
    if rc != 0:
        raise Refused("git add failed in %s: %s" % (home.repo, err.strip()[:200]))
    rc, out, err = gitutil.commit(home.repo, message, paths=rels)
    if rc != 0:
        if "nothing to commit" in (out + err):
            return "unchanged"
        raise Refused("git commit failed in %s: %s" % (home.repo, (out + err).strip()[:200]))
    if not push:
        return "committed"
    if not gitutil.has_upstream(home.repo):
        return "committed (no upstream: not pushed)"
    try:
        from lib import pushguard
    except ImportError:  # pragma: no cover
        import pushguard  # type: ignore
    refused = pushguard.refusal_for(home.repo)
    if refused:
        return "committed (%s)" % refused
    rc, _, err = _git(home.repo, "push", "--quiet", timeout=300)
    return "pushed" if rc == 0 else "committed (push pending: %s)" % err.strip().splitlines()[-1][:120]


# --------------------------------------------------------------------------- write

def _now() -> datetime:
    return datetime.now().astimezone()


def _stamp(now: Optional[datetime] = None) -> str:
    return (now or _now()).isoformat(timespec="seconds")


def _by() -> str:
    try:
        from lib import machine
        return "%s/ticket" % machine.key()
    except Exception:  # noqa: BLE001
        return "ticket"


def _log_line(msg: str, now: Optional[datetime] = None) -> str:
    return "- %s %s: %s" % ((now or _now()).strftime("%Y-%m-%dT%H:%M"), _by(), msg)


def render(fields: Dict[str, str], body: str) -> str:
    order = [f for f in FIELDS if f in fields] + sorted(
        k for k in fields if k not in FIELDS and not k.startswith("_"))
    return frontmatter.dump({k: fields[k] for k in order}, body)


def _with_log(body: str, msg: str, now: Optional[datetime] = None) -> str:
    body = (body or "").rstrip("\n")
    if "\n## Log" not in "\n" + body:
        body = body + ("\n\n" if body.strip() else "\n") + "## Log"
    return body + "\n" + _log_line(msg, now) + "\n"


#: Set by bin/issues_state.py while it regenerates a canonical-issues project's ticket export.
EXPORT_ENV = "LODESTAR_TICKET_EXPORT"


def issues_canonical(tickets_dir: str) -> Optional[str]:
    """Project name if this tickets dir is a read-only export of GitHub Issues (a `canonical: issues` project)."""
    try:
        from lib import gh_issues
    except ImportError:  # pragma: no cover
        import gh_issues  # type: ignore
    return gh_issues.canonical_issues_project(tickets_dir)


def issues_pointer(project: str) -> str:
    return ("%s work is tracked in GitHub Issues (canonical since 2026-10-23); tickets/ is a read-only "
            "export. Use: python3 ~/dev/Lodestar/bin/issues_work.py ready|new|claim|close --project %s"
            % (project.upper(), project))


def _write(path: str, fields: Dict[str, str], body: str) -> None:
    proj = issues_canonical(os.path.dirname(path))
    if proj and os.environ.get(EXPORT_ENV) != "1":
        raise Refused(issues_pointer(proj))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(render(fields, body))
    os.replace(tmp, path)


def new_fields(tid: str, home: Home, title: str, ttype: str, owner: str = "lukas",
               status: str = "open", source: str = "manual", now: Optional[datetime] = None,
               **extra: str) -> Dict[str, str]:
    stamp = _stamp(now)
    f = {"id": tid, "title": " ".join(title.split()), "project": home.pid, "type": ttype,
         "status": status, "owner": owner or "lukas", "created": stamp, "updated": stamp,
         "source": source}
    for k, v in extra.items():
        if v not in (None, ""):
            f[k] = str(v)
    return f


def create(project: str, title: str, ttype: str, body: str = "", owner: str = "lukas",
           status: str = "open", source: str = "manual", private: bool = False,
           offline: bool = False, push: bool = True, root: Optional[str] = None,
           name: str = "", legacy: bool = False, **extra: str) -> Tuple[str, str, str]:
    """File one ticket. Returns (id, path, git status word).

    `name` is the SUBJECT_INTENT_TWIST id (lib/ticketname.py). Without it the call is refused;
    `legacy=True` (tests, imports) mints the retired `CODE-<n>` form."""
    if not name and not legacy:
        raise Refused("a ticket needs a name: SUBJECT_INTENT_TWIST, e.g. TICKET_NAMES_ADOPT_NO_MORE_NUMBERS "
                      "(`ticket namecheck --bank` lists the intent words)")
    if ttype not in TYPES:
        raise Refused("type %r not in %s" % (ttype, "/".join(TYPES)))
    if status not in STATUSES or status in TERMINAL:
        raise Refused("a new ticket's status is proposed/open/active/blocked, not %r" % status)
    try:
        from lib import outcome
    except ImportError:  # pragma: no cover
        import outcome  # type: ignore
    try:  # creator boundary (2026-10-22): a check, or an explicit gap — never nothing
        extra["check"] = outcome.encode(outcome.creator_check(
            extra.get("check"), "%s ticket: no mechanical outcome check given" % ttype))
    except ValueError as e:
        raise Refused("bad --check: %s" % e)
    homes = homes_for(project, root, private=private)
    proj = issues_canonical(homes[0].dir) if homes else None
    if proj and os.environ.get(EXPORT_ENV) != "1":   # refuse before an id is allocated
        raise Refused(issues_pointer(proj))
    home = homes[0]
    guard_text(home, title, body, extra.get("links", ""), extra.get("likely_fix", ""),
               extra.get("check", ""))
    # validate BEFORE claiming a number: a refused ticket must not burn an id
    probs = problems(new_fields(name or "%s-0" % home.code, home, title, ttype, owner, status,
                                source, **extra))
    if probs:
        raise Refused("invalid ticket: %s" % "; ".join(probs))
    fh = _lock(home.code)
    try:
        tid = claim_name(homes, name, offline) if name else allocate(homes, 1, offline)[0]
        fields = new_fields(tid, home, title, ttype, owner, status, source, **extra)
        path = os.path.join(home.dir, tid + ".md")
        _write(path, fields, _with_log("\n" + (body.strip() + "\n" if body.strip() else ""),
                                       "new (%s)" % source))
        state = commit(home, [path], "ticket: %s new" % tid, push)
    finally:
        fh.close()
    return tid, path, state


def version(t: Dict[str, str]) -> str:
    """A token that changes whenever the ticket file's fields or body change."""
    import hashlib
    fields = {k: v for k, v in t.items() if not k.startswith("_")}
    return hashlib.sha256(render(fields, t.get("_body", "")).encode("utf-8")).hexdigest()[:16]


class Stale(Refused):
    """The ticket changed since the caller read it (`expect_version` guard)."""


def update(tid: str, msg: str, root: Optional[str] = None, push: bool = True,
           body_append: str = "", expect_version: str = "",
           **changes: Optional[str]) -> Tuple[Dict[str, str], str]:
    """Change fields of one ticket, append a log line, commit. A None value removes a field.
    With `expect_version`, refuse (Stale) unless the file is unchanged since it was read."""
    t, home = find(tid, root)
    fh = _lock(home.code)
    try:
        t, home = find(tid, root)          # re-read under the lock
        if expect_version and version(t) != expect_version:
            raise Stale("%s changed since it was read" % tid)
        fields = {k: v for k, v in t.items() if not k.startswith("_")}
        for k, v in changes.items():
            if v is None:
                fields.pop(k, None)
            else:
                fields[k] = str(v)
        fields["updated"] = _stamp()
        body = t["_body"]
        if body_append:
            guard_text(home, body_append)
            body = body.rstrip("\n").replace("\n## Log", "\n" + body_append.strip() + "\n\n## Log", 1) \
                if "\n## Log" in body else body + "\n" + body_append.strip() + "\n"
        guard_text(home, *(v for v in changes.values() if v))
        probs = problems(dict(fields, _path=t["_path"]))
        if probs:
            raise Refused("refused %s: %s" % (tid, "; ".join(probs)))
        _write(t["_path"], fields, _with_log(body, msg))
        state = commit(home, [t["_path"]], "ticket: %s %s" % (tid, msg.split()[0]), push)
    finally:
        fh.close()
    fields["_path"] = t["_path"]
    return fields, state


def rename(old: str, new: str, root: Optional[str] = None, push: bool = True) -> Tuple[str, str]:
    """Give a ticket a SUBJECT_INTENT_TWIST name. The file moves (git mv), `former_id` keeps the
    old id findable, and every ticket in every home that lists the old id in a link field, plus
    `id:` itself, follows. Only legacy-form ids rename, and only to a name `ticketname` accepts.
    Free-text mentions of the old id in bodies and docs are left (they still resolve via
    `former_id` in `search`). Returns (new id, git status word)."""
    if not LEGACY_ID_RE.match(old or ""):
        raise Refused("%s is not a legacy-form id; only CODE-n tickets rename" % old)
    t, home = find(old, root)
    taken = {os.path.basename(p)[:-3] for h in all_homes(root) for p in _ticket_files(h.dir)}
    probs = ticketname.problems(new, taken)
    if probs:
        raise Refused("bad ticket name %s: %s" % (new, "; ".join(probs)))
    fh = _lock(home.code)
    try:
        t, home = find(old, root)
        src = t["_path"]
        dst = os.path.join(os.path.dirname(src), new + ".md")
        fields = {k: v for k, v in t.items() if not k.startswith("_")}
        fields["id"] = new
        fields["former_id"] = old
        fields["updated"] = _stamp()
        _write(dst, fields, _with_log(t["_body"], "renamed from %s" % old))
        os.remove(src)
        touched = [src, dst]
        for h in all_homes(root):
            for p in _ticket_files(h.dir):
                if p == dst:
                    continue
                u = read(p)
                changed = {}
                for f in LINK_FIELDS:
                    items = split(u.get(f) or "")
                    if old in items:
                        changed[f] = ", ".join(new if x == old else x for x in items)
                if changed:
                    uf = {k: v for k, v in u.items() if not k.startswith("_")}
                    uf.update(changed)
                    _write(p, uf, u["_body"])
                    touched.append(p)
        by_repo = {}
        for p in touched:
            for h in all_homes(root):
                if p.startswith(h.dir + os.sep):
                    by_repo.setdefault(h.repo, (h, []))[1].append(p)
        state = "committed"
        for repo, (h, files) in by_repo.items():
            state = commit(h, files, "ticket: %s renamed to %s" % (old, new), push)
    finally:
        fh.close()
    return new, state


_MOVES = {"proposed": ("open", "dropped"),
          "open": ("active", "blocked", "done", "dropped"),
          "active": ("open", "blocked", "done", "dropped"),
          "blocked": ("open", "active", "done", "dropped"),
          "done": ("open",), "dropped": ("open",)}


def move(tid: str, status: str, said: str = "", why: str = "", root: Optional[str] = None,
         push: bool = True, evidence: str = "",
         expect_version: str = "") -> Tuple[Dict[str, str], str]:
    t, _ = find(tid, root)
    cur = t.get("status")
    if status not in STATUSES:
        raise Refused("status %r not in %s" % (status, "/".join(STATUSES)))
    if status == cur:
        return t, "unchanged"
    if status not in _MOVES.get(cur, ()):
        raise Refused("%s is %s; it cannot move to %s (allowed: %s)" % (
            tid, cur, status, ", ".join(_MOVES.get(cur, ()))))
    reason = ("said: " + said.strip()) if said.strip() else (
        "evidence: " + evidence.strip() if evidence.strip() else why.strip())
    ch: Dict[str, Optional[str]] = {"status": status}
    if status in TERMINAL:
        if not reason:
            raise Refused("%s %s needs --said \"his words\" or --evidence commit:<sha>" % (
                "close" if status == "done" else "drop", tid))
        ch["close_reason"] = reason
        ch["closed"] = _stamp()
    elif cur in TERMINAL:
        ch["close_reason"] = None
        ch["closed"] = None
    tail = ""
    if cur == "blocked":
        # leaving blocked by hand is allowed; the log records what the blockers were at the time
        bs = blocker_states(t, root=root, external=False)
        tail = "; left blocked: %s" % (", ".join("%s %s" % b for b in bs) if bs else "no blockers")
    verb = {"done": "close", "dropped": "drop", "open": "reopen" if cur in TERMINAL else
            ("accept" if cur == "proposed" else "open")}.get(status, status)
    if said.strip():   # LODE-23: his words as recorded by an agent, not an authenticated act
        tail += " [recorded owner statement; unauthenticated]"
    return update(tid, "%s%s%s" % (verb, (" (%s)" % reason) if reason else "", tail), root, push,
                  expect_version=expect_version, **ch)


# --------------------------------------------------------------------------- dependencies
#
# A blocker that is done releases nothing by itself: dependents are NAMED, and move only by a
# human or an explicit --release. A dropped blocker never releases a dependent at all; the
# dependent is named for a human decision ("confirm"), resolved with `link ID --unblock X`.

def _find_or_none(tid: str, root: Optional[str] = None) -> Optional[Dict[str, str]]:
    try:
        return find(tid, root)[0]
    except Refused:
        return None


def gh_hosts() -> Tuple[str, ...]:
    """github.com, plus this deployment's private git host (`lib.boundary`) when it has one."""
    try:
        from lib import boundary
    except ImportError:  # pragma: no cover
        import boundary  # type: ignore
    private = boundary.host()
    return ("github.com",) + ((private,) if private and private != "github.com" else ())


def private_host() -> Optional[str]:
    hosts = gh_hosts()
    return hosts[1] if len(hosts) > 1 else None


def _ext_url() -> "re.Pattern":
    alt = "|".join(re.escape(h) for h in gh_hosts())
    return re.compile(r"^https?://(%s)/([\w.-]+/[\w.-]+)/(?:issues|pull)/(\d+)/?$" % alt)
_EXT_SHORT = re.compile(r"^([\w.-]+/[\w.-]+)#(\d+)$")
_UNTIL = re.compile(r"^(?:until\s+)?(\d{4}-\d\d-\d\d)$", re.I)


def classify(entry: str, host: str = "github.com") -> Tuple[str, str]:
    """(kind, canonical entry) for one blocked_by entry. kind: ticket | date | ext | text.
    Raises Refused for something plainly a date or issue ref but malformed."""
    e = " ".join((entry or "").split())
    if not e:
        raise Refused("empty blocker")
    if ID_RE.match(e):
        return "ticket", e
    m = _UNTIL.match(e)
    if m:
        if not _valid_date(m.group(1)):
            raise Refused("%r is not a real date" % m.group(1))
        return "date", "until " + m.group(1)
    m = _ext_url().match(e)
    if m:
        return "ext", "https://%s/%s/issues/%d" % (m.group(1), m.group(2), int(m.group(3)))
    m = _EXT_SHORT.match(e)
    if m:
        if host not in gh_hosts():
            raise Refused("host %r is not one of %s" % (host, ", ".join(gh_hosts())))
        return "ext", "https://%s/%s/issues/%d" % (host, m.group(1), int(m.group(2)))
    if "," in e:
        raise Refused("a condition cannot contain a comma (the field is comma-separated); use ';'")
    return "text", e


def kind_of(entry: str) -> str:
    try:
        return classify(entry)[0]
    except Refused:
        return "text"


def ext_parts(entry: str) -> Tuple[str, str, int]:
    m = _ext_url().match(entry)
    return m.group(1), m.group(2), int(m.group(3))


_EXT_CACHE: Dict[str, str] = {}


def ext_state(entry: str) -> str:
    """'done' (issue closed), 'open', or 'unknown' (gh missing, offline, signed out, any error).
    Never 'done' unless gh said closed. Cached per process."""
    if entry in _EXT_CACHE:
        return _EXT_CACHE[entry]
    host, repo, num = ext_parts(entry)
    out = "unknown"
    try:
        r = subprocess.run(["gh", "api", "--hostname", host, "repos/%s/issues/%d" % (repo, num),
                            "--jq", ".state"], capture_output=True, text=True, timeout=20)
        word = r.stdout.strip().lower()
        if r.returncode == 0 and word in ("open", "closed"):
            out = "done" if word == "closed" else "open"
    except Exception:  # noqa: BLE001  (offline, no gh, timeout: unknown, not done)
        pass
    _EXT_CACHE[entry] = out
    return out


def describe(entry: str, withhold: bool = False) -> str:
    """Printable form of a blocker. In a github.com session a private-host ref shows only
    'private-host issue #N' and a free-text condition is not printed (it may carry private text)."""
    kind = kind_of(entry)
    if kind == "text":
        return "a condition (text withheld)" if withhold else '"%s"' % entry
    if kind == "ext":
        host, repo, num = ext_parts(entry)
        if withhold and host == private_host():
            return "private-host issue #%d" % num
        return "%s#%d@%s" % (repo, num, host)
    return entry


def blocker_states(t: Dict[str, str], lookup=None, root: Optional[str] = None,
                   now: Optional[datetime] = None, external: bool = True) -> List[Tuple[str, str]]:
    """[(blocker entry, status)] for every blocked_by entry of `t`. A ticket: its status or
    'missing'. A date: 'done' once today reaches it, else 'waiting'. An external issue: 'done'
    (closed) / 'open' / 'unknown' ('unchecked' when external=False). A condition: 'waiting'.
    `lookup(id)` returns a loaded ticket or None; without it each blocker is found on disk."""
    out = []
    today = (now or _now()).strftime("%Y-%m-%d")
    for x in split(t.get("blocked_by") or ""):
        kind = kind_of(x)
        if kind == "ticket":
            b = lookup(x) if lookup else _find_or_none(x, root)
            out.append((x, (b.get("status") or "?") if b else "missing"))
        elif kind == "date":
            out.append((x, "done" if today >= x[-10:] else "waiting"))
        elif kind == "ext":
            out.append((x, ext_state(x) if external else "unchecked"))
        else:
            out.append((x, "waiting"))
    return out


def blocker_age(t: Dict[str, str], entry: str, now: Optional[datetime] = None) -> str:
    """How long `entry` has blocked `t`, from the ticket's own log ('3d', '5h', '?')."""
    for ln in (t.get("_body") or "").splitlines():
        m = re.match(r"^- (\d{4}-\d\d-\d\dT\d\d:\d\d) \S+: link blocked_by " + re.escape(entry) + r"$", ln)
        if m:
            secs = ((now or _now()) - datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M").astimezone()
                    ).total_seconds()
            return "%dd" % (secs // 86400) if secs >= 86400 else "%dh" % max(0, secs // 3600)
    return "?"


def waiting_nonticket(t: Dict[str, str], lookup=None,
                      now: Optional[datetime] = None) -> List[Tuple[str, str, str]]:
    """[(entry, state, age)] for a live ticket's non-ticket blockers that have not cleared."""
    return [(x, s, blocker_age(t, x, now)) for x, s in blocker_states(t, lookup, now=now)
            if kind_of(x) != "ticket" and s != "done"]


def cleared_nonticket(t: Dict[str, str], now: Optional[datetime] = None) -> List[str]:
    """Date and external blockers that have cleared by themselves."""
    return [x for x, s in blocker_states(t, now=now) if kind_of(x) in ("date", "ext") and s == "done"]


def lookup_from(tickets: Sequence[Dict[str, str]], root: Optional[str] = None):
    """A lookup over loaded tickets that falls back to `find` (cross-project ids), cached."""
    idx: Dict[str, Optional[Dict[str, str]]] = {t["id"]: t for t in tickets if t.get("id")}

    def get(tid: str) -> Optional[Dict[str, str]]:
        if tid not in idx:
            idx[tid] = _find_or_none(tid, root)
        return idx[tid]
    return get


def frontier(tickets: Sequence[Dict[str, str]], lookup, now: Optional[datetime] = None) -> Tuple[List[Dict[str, str]],
                                                               List[Tuple[Dict[str, str], List[str]]],
                                                               List[Tuple[Dict[str, str], List[str]]]]:
    """(ready, unblocked, dropped). ready: status open and every blocker done. unblocked: status
    blocked and every blocker done (move it open?). dropped: a live ticket with a dropped blocker
    (confirm). A ticket with a dropped blocker is never ready."""
    ready, unblocked, dropped = [], [], []
    for t in tickets:
        st = t.get("status")
        bs = blocker_states(t, lookup, now=now)
        gone = [x for x, s in bs if s == "dropped"]
        if st in LIVE and gone:
            dropped.append((t, gone))
        elif st == "open" and all(s == "done" for _, s in bs):
            ready.append(t)
        elif st == "blocked" and bs and all(s == "done" for _, s in bs):
            unblocked.append((t, [x for x, _ in bs]))
    return ready, unblocked, dropped


def waiting_rows(tickets: Sequence[Dict[str, str]], lookup=None,
                 now: Optional[datetime] = None) -> List[Tuple[Dict[str, str], List[Tuple[str, str, str]]]]:
    """Live tickets with a non-ticket blocker that has not cleared: (ticket, [(entry, state,
    age)]). These need a person's or agent's judgment (conditions) or a later look (dates, refs)."""
    out = []
    for t in tickets:
        if t.get("status") in LIVE:
            w = waiting_nonticket(t, lookup, now)
            if w:
                out.append((t, w))
    return out


def cleared_rows(tickets: Sequence[Dict[str, str]],
                 now: Optional[datetime] = None) -> List[Tuple[Dict[str, str], List[str]]]:
    """Live tickets whose date or external blockers have cleared by themselves."""
    out = []
    for t in tickets:
        if t.get("status") in LIVE:
            c = cleared_nonticket(t, now)
            if c:
                out.append((t, c))
    return out


def link_problems(t: Dict[str, str], lookup) -> List[str]:
    """`ticket check` findings about blocks (dangling ids are reported by `problems`). Never
    touches the network: an external issue counts as unchecked here."""
    out = []
    bs = blocker_states(t, lookup, external=False)
    st = t.get("status")
    if st == "blocked":
        if not bs:
            out.append("blocked with no blocked_by")
        elif all(s == "done" for _, s in bs):
            out.append("stale block: every blocker done (%s); move it open?" % ", ".join(x for x, _ in bs))
    if st in LIVE:
        for x, s in bs:
            if s == "dropped":
                out.append("blocker %s dropped; confirm (link --unblock %s)" % (x, x))
    return out


def cycle_path(dependent: str, blocker: str, root: Optional[str] = None,
               lookup=None) -> Optional[List[str]]:
    """The wait chain that `dependent` blocked_by `blocker` would close into a cycle, else None.
    Walks blocked_by from the blocker across projects (ids resolve through the homes table)."""
    if dependent == blocker:
        return [dependent, dependent]
    # path runs dependent -> blocker -> ... -> dependent
    get = lookup or (lambda x: _find_or_none(x, root))
    stack, seen = [(blocker, [dependent, blocker])], set()
    while stack:
        n, path = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        t = get(n)
        if not t:
            continue
        for x in split(t.get("blocked_by") or ""):
            if kind_of(x) != "ticket":
                continue
            if x == dependent:
                return path + [x]
            stack.append((x, path + [x]))
    return None


def dependents_after(tid: str, root: Optional[str] = None) -> List[Dict[str, object]]:
    """After `tid` closed or dropped: each live dependent and what it means.
    kind: ready (every blocker done), confirm (tid was dropped), waiting (others remain),
    missing (named in `blocks` but not found here)."""
    t, _ = find(tid, root)
    gone = t.get("status") == "dropped"
    out: List[Dict[str, object]] = []
    for d_id in split(t.get("blocks") or ""):
        d = _find_or_none(d_id, root)
        if d is None:
            out.append({"id": d_id, "kind": "missing", "status": "", "waiting": []})
            continue
        if d.get("status") in TERMINAL:
            continue
        bs = blocker_states(d, root=root)
        rest = [x for x, s in bs if s != "done"]
        kind = "confirm" if gone else ("ready" if not rest else "waiting")
        out.append({"id": d_id, "kind": kind, "status": d.get("status") or "", "waiting": rest})
    return out


def release(rows: Sequence[Dict[str, object]], blocker: str, root: Optional[str] = None,
            push: bool = True) -> List[str]:
    """Move each `ready` dependent that is still blocked to open (the explicit --release)."""
    moved = []
    for r in rows:
        if r["kind"] == "ready" and r["status"] == "blocked":
            move(str(r["id"]), "open", why="released: every blocker done, last %s" % blocker,
                 root=root, push=push)
            moved.append(str(r["id"]))
    return moved


def link(tid: str, rel: str, other: str, root: Optional[str] = None,
         push: bool = True, host: str = "github.com", said: str = "",
         evidence: str = "") -> List[str]:
    """blocks / blocked_by / relates between two tickets (both sides written), a provenance
    ref (`rel` = ref, `other` = commit:<sha> etc.), `blocked_on` (a date, an issue ref or a
    free-text condition in blocked_by; one side only), `met` (end a condition, logged with who
    said so), or `unblock` (remove `tid` blocked_by
    `other` on both sides; the human's answer to a dropped blocker). A blocks link that would
    close a wait cycle is refused."""
    if rel == "ref":
        t, _ = find(tid, root)
        refs = split(t.get("links") or "")
        if other not in refs:
            refs.append(other)
        return [update(tid, "link %s" % other.split(":")[0], root, push, links=", ".join(refs))[1]]
    if rel == "unblock":
        a, _ = find(tid, root)
        if other not in split(a.get("blocked_by") or ""):
            raise Refused("%s is not blocked by %s" % (tid, other))
        out = []
        for t_id, k, x in ((tid, "blocked_by", other), (other, "blocks", tid)):
            t = _find_or_none(t_id, root)
            if t is None or x not in split(t.get(k) or ""):
                continue
            rest = [y for y in split(t.get(k) or "") if y != x]
            out.append(update(t_id, "unlink %s %s" % (k, x), root, push,
                              **{k: ", ".join(rest) if rest else None})[1])
        return out
    if rel == "blocked_on":
        kind, entry = classify(other, host)
        if kind == "ticket":
            return link(tid, "blocked_by", entry, root, push)
        a, _ = find(tid, root)
        cur = split(a.get("blocked_by") or "")
        if entry in cur:
            return ["unchanged"]
        cur.append(entry)
        ch = {"blocked_by": ", ".join(cur)}
        if a.get("status") in ("open", "active") and \
                blocker_states({"blocked_by": entry}, root=root)[0][1] != "done":
            ch["status"] = "blocked"
        return [update(tid, "link blocked_by %s" % entry, root, push, **ch)[1]]
    if rel == "met":
        if not (said.strip() or evidence.strip()):
            raise Refused("--met needs --said \"his words\" or --evidence <what shows it>")
        a, _ = find(tid, root)
        cur = split(a.get("blocked_by") or "")
        want = " ".join(other.split())
        try:
            want_c = classify(want, host)[1]
        except Refused:
            want_c = want
        hit = [x for x in cur if kind_of(x) != "ticket"
               and (x == want or x == want_c or x.lower() == want.lower())]
        if not hit:
            raise Refused("%s has no non-ticket blocker %r (blockers: %s)" % (
                tid, want, ", ".join(cur) or "none"))
        rest = [x for x in cur if x != hit[0]]
        why = ("said: " + said.strip()) if said.strip() else ("evidence: " + evidence.strip())
        return [update(tid, "met %s (%s)" % (hit[0], why), root, push,
                       blocked_by=", ".join(rest) if rest else None)[1]]
    inverse = {"blocks": "blocked_by", "blocked_by": "blocks", "relates": "relates"}
    if rel not in inverse:
        raise Refused("link kind %r not in blocks/blocked-by/blocked-on/met/relates/ref/unblock" % rel)
    if rel == "blocked_by" and kind_of(other) != "ticket":
        return link(tid, "blocked_on", other, root, push, host=host)
    a, _ = find(tid, root)
    b, _ = find(other, root)
    if rel != "relates":
        dep, blk = (tid, other) if rel == "blocked_by" else (other, tid)
        path = cycle_path(dep, blk, root)
        if path:
            raise Refused("refused: %s blocked-by %s makes a cycle: %s" % (
                dep, blk, " waits on ".join(path)))
    out = []
    for t, k, x in ((a, rel, other), (b, inverse[rel], tid)):
        cur = split(t.get(k) or "")
        if x not in cur:
            cur.append(x)
            ch = {k: ", ".join(cur)}
            blocker = b if t is a else a
            if k == "blocked_by" and t.get("status") in ("open", "active") \
                    and blocker.get("status") != "done":
                ch["status"] = "blocked"
            out.append(update(t["id"], "link %s %s" % (k, x), root, push, **ch)[1])
    return out


def need(from_project: str, to_project: str, title: str, body: str = "",
         ttype: str = "build", from_ticket: str = "", root: Optional[str] = None,
         push: bool = True, offline: bool = False, name: str = "", legacy: bool = False,
         **extra: str) -> Tuple[str, str, str]:
    """A cross-project request: a `proposed` ticket in the target's home (its inbox), linked
    back to the requester. Accept = `move ID open`; decline = `drop ID --said ...`."""
    src = homes_for(from_project, root)[0]
    tid, path, state = create(to_project, title, ttype, body, owner=extra.pop("owner", "lukas"),
                              status="proposed", source="need:%s" % src.pid,
                              requested_by=from_ticket or "project:%s" % src.pid,
                              offline=offline, push=push, root=root, name=name, legacy=legacy,
                              **extra)
    if from_ticket:
        t, _ = find(from_ticket, root)
        cur = split(t.get("needs") or "")
        update(from_ticket, "need %s" % tid, root, push, needs=", ".join(cur + [tid]))
    return tid, path, state


# --------------------------------------------------------------------------- views

def effort_min(v: str) -> int:
    m = _EFFORT.match((v or "").strip())
    if not m:
        return 0
    n = int(m.group(1))
    return n * {"m": 1, "h": 60, "d": 480}[m.group(2)]


def mine(t: Dict[str, str]) -> bool:
    return (t.get("owner") or "").lower() in ME


def _when(v: str) -> Optional[datetime]:
    if not v:
        return None
    try:
        if "T" in v:
            return datetime.strptime(v[:16], "%Y-%m-%dT%H:%M").astimezone()
        return datetime.strptime(v[:10], "%Y-%m-%d").replace(hour=17).astimezone()
    except ValueError:
        return None


def deadline(t: Dict[str, str]) -> Optional[datetime]:
    """act_by when set (the latest useful start), else due."""
    return _when(t.get("act_by") or "") or _when(t.get("due") or "")


def face_rows(tickets: Sequence[Dict[str, str]], now: Optional[datetime] = None,
              horizon_days: int = FACE_HORIZON_DAYS) -> List[Dict]:
    """What the Companion TICKETS row shows (design §6.2). Scheduled actions near their date;
    build/bug/repair/note only when next, blocked or overdue. Never proposed, never comms."""
    now = now or _now()
    out = []
    byid = {t["id"]: t for t in tickets if t.get("id")}
    for t in tickets:
        st = t.get("status")
        if st not in LIVE or not mine(t):
            continue
        snooze = _when(t.get("snoozed_until") or "")
        if snooze is not None and snooze > now:
            continue
        dl = deadline(t)
        ttype = t.get("type")
        tier, when, why, rank = None, "", "", 9
        left = (dl - now).total_seconds() if dl is not None else None
        if ttype == "action" and dl is not None:
            # a scheduled action: lit within the horizon, deadline-bearing first
            if left < 0:
                tier, when, why, rank = "red", "LATE", "past %s" % dl.strftime("%a %m-%d"), 0
            elif left <= 12 * 3600:
                tier, when, why, rank = "red", "TODAY", "by %s" % dl.strftime("%H:%M"), 0
            elif dl.date() <= (now + timedelta(days=1)).date():
                tier = "amber"
                when = "TODAY" if dl.date() == now.date() else "TMRW"
                why, rank = "by %s" % dl.strftime("%a %H:%M"), 1
            elif dl <= now + timedelta(days=horizon_days):
                tier, when, rank = "soon", dl.strftime("%a").upper(), 2
                why = "by %s" % dl.strftime("%a %m-%d")
        elif ttype != "action":
            # build / bug / repair / note: only when overdue, marked next, or blocked
            if dl is not None and left < 0:
                tier, when, why, rank = "amber", "LATE", "past %s" % dl.strftime("%a %m-%d"), 1
            elif st == "blocked":
                bs = blocker_states(t, byid.get, now=now, external=False)
                if bs and all(x == "done" for _, x in bs):
                    tier, when, why, rank = "soon", "CLR", "every blocker cleared", 2
                else:
                    tier, when, why, rank = "soon", "BLKD", "blocked", 3
            elif t.get("next") == "yes":
                tier, when, why, rank = "soon", "NEXT", "marked next", 3
        if tier is None:
            continue
        out.append({"ticket": t["id"], "tier": tier, "when": when, "why": why,
                    "project": t.get("project") or "", "title": t.get("title") or "",
                    "type": ttype, "status": st, "path": t.get("_path") or "",
                    "due": t.get("due") or "", "act_by": t.get("act_by") or "",
                    "_k": (rank, dl.timestamp() if dl else 9e12, t["id"])})
    out.sort(key=lambda r: r["_k"])
    for r in out:
        r.pop("_k")
    return out


def opaque(tid: str) -> str:
    import hashlib
    return "tk_" + hashlib.sha256(("ticket:" + tid).encode("utf-8")).hexdigest()[:10]


def index_md(project: str, tickets: Sequence[Dict[str, str]], today: Optional[str] = None) -> str:
    """tickets/INDEX.md: a generated glance file (never hand-edited)."""
    today = today or _now().date().isoformat()
    lines = ["# %s tickets" % project, "",
             "_Generated by `ticket index` on %s; do not edit. One file per ticket in this "
             "folder; git is the history._" % today, ""]
    order = ("active", "blocked", "open", "proposed")
    for st in order:
        rows = [t for t in tickets if t.get("status") == st]
        if not rows:
            continue
        lines += ["## %s (%d)" % (st, len(rows)), ""]
        rows.sort(key=lambda t: (t.get("due") or "9999", _num(t["id"]), t["id"]))
        for t in rows:
            tail = " · ".join(x for x in (t.get("type"), t.get("owner"),
                                          ("due " + t["due"]) if t.get("due") else "") if x)
            lines.append("- **%s** %s (%s)" % (t["id"], t.get("title") or "", tail))
        lines.append("")
    closed = [t for t in tickets if t.get("status") in TERMINAL]
    lines += ["## closed: %d done, %d dropped" % (
        sum(1 for t in closed if t["status"] == "done"),
        sum(1 for t in closed if t["status"] == "dropped")), ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- Orders reader

def engineering_backlog(t: Dict[str, str]) -> bool:
    """An agent-filed build/bug/repair/note ticket with no due date and not marked `next`.

    `ticket new` defaults the owner to lukas, so every such ticket looked like his order (owner,
    2026-10-26: a Tender code-review ticket sat in his list; "Don't even know what this is"). The
    backlog is real work and stays on `ticket list` and the project board; it only stays out of
    his Orders until it has a date or he says `next`. Dated or `next` tickets, and `action`
    tickets (a real-world act for him), still show."""
    return (t.get("source") == "agent" and t.get("type") in ("build", "bug", "repair", "note")
            and not (t.get("due") or "").strip() and (t.get("next") or "").lower() not in ("yes", "true"))


def orders_items(homes: Sequence[Home]) -> List[Dict]:
    """A project's tickets in the item shape `todolists.parse` returns, for Orders. Proposed
    tickets are not listed (the inbox is not a to-do); a migrated ticket keeps its Orders key.
    An epic is a long-term goal, not an order (owner 2026-10-25), so it is not listed either."""
    state_of = {"open": "open", "active": "progress", "blocked": "open", "proposed": "unverified",
                "done": "done", "dropped": "dropped"}
    out = []
    for t in load(homes):
        if t.get("kind") == "epic":
            continue
        if engineering_backlog(t):
            continue
        due = t.get("due") or ""
        out.append({
            "line": 1, "state": state_of.get(t.get("status") or "", "open"),
            "title": t.get("title") or "", "owners": [t.get("owner") or ""], "boxed": True,
            "mine": mine(t), "due": due[:10], "due_time": due[11:16] if "T" in due else "",
            "est_min": effort_min(t.get("effort") or ""), "tid": t["id"],
            "text": t.get("_body") or "", "section": t.get("section") or "",
            "key": t.get("orders_key") or "ticket:" + t["id"], "version": "ticket",
            "id": t["id"], "owner": t.get("owner") or "", "path": t["_path"],
            "open": t["_path"], "blocked": t.get("status") == "blocked",
        })
    return out


# --------------------------------------------------------------------------- migration

def _raw_item_lines(lines: List[str], start: int, indent: int) -> List[str]:
    """The item's own lines, verbatim: its first line and the continuation lines
    `actionslist.parse_v1` folds into it (same stop rule)."""
    try:
        from lib import actionslist as al
    except ImportError:  # pragma: no cover
        import actionslist as al  # type: ignore
    out = [lines[start]]
    j = start + 1
    while j < len(lines):
        nxt = lines[j]
        if not nxt.strip() or al._HEAD.match(nxt) or al._BOX.match(nxt):
            break
        if len(nxt) - len(nxt.lstrip()) <= indent:
            break
        out.append(nxt)
        j += 1
    return out


_STATUS_OF = {"open": "open", "progress": "active", "done": "done", "dropped": "dropped",
              "unverified": "proposed", "pointer": "open"}


def migration_plan(repo: str, project: str, root: Optional[str] = None) -> Tuple[List[Dict], str]:
    """(one record per ACTIONS v1 item, the list's commit). Pure: writes nothing."""
    try:
        from lib import actionslist as al
    except ImportError:  # pragma: no cover
        import actionslist as al  # type: ignore
    path = os.path.join(repo, "ACTIONS.md")
    text = al.read_text(path)
    if not al.is_v1(text):
        raise Refused("%s is not ACTIONS v1; run actions.py migrate first" % path)
    fm, _ = al.front_matter(text)
    if fm.get("tracker") == "tickets":
        raise Refused("%s already says tracker: tickets" % path)
    rc, sha, _ = _git(repo, "log", "-1", "--format=%h", "--", "ACTIONS.md")
    if _git(repo, "status", "--porcelain", "--", "ACTIONS.md")[1].strip():
        raise Refused("ACTIONS.md has uncommitted changes in %s: commit them first" % repo)
    lines = text.split("\n")
    name = "%s/ACTIONS.md" % os.path.basename(repo)
    plan = []
    for it in al.parse_v1(text, name):
        raw = _raw_item_lines(lines, it["line"] - 1, it["indent"])
        if not it["mine"]:
            owner = (it["owner"] or (it["owners"][0] if it["owners"] else "unassigned")).lower()
            owner = re.sub(r"[^a-z0-9._-]+", "-", owner).strip("-") or "unassigned"
            if owner in ME:
                owner = "unassigned"
        else:
            owner = "lukas"
        due = it["due"] + ("T" + it["due_time"] if it["due_time"] else "")
        status = _STATUS_OF[it["state"]]
        reason = ""
        if status == "done":
            reason = "legacy: done in ACTIONS.md before migration" + (
                " (%s %s)" % (it["done_date"], it["evidence"]) if it["done_date"] else
                (" (%s)" % it["evidence"] if it["evidence"] else ""))
        elif status == "dropped":
            reason = "legacy: dropped in ACTIONS.md before migration"
        plan.append({"item": it, "raw": raw, "owner": owner, "due": due, "status": status,
                     "type": "action" if due else "note", "reason": reason})
    order = {"active": 0, "open": 1, "proposed": 2, "done": 3, "dropped": 4}
    plan.sort(key=lambda p: (order[p["status"]], p["item"]["line"]))
    return plan, sha.strip()


def load_names(path: str) -> Dict[str, str]:
    """A --names file: JSON object {legacy_id: NAME} or lines `legacy_id<TAB or whitespace>NAME`."""
    text = open(os.path.expanduser(path), encoding="utf-8").read()
    if text.lstrip().startswith("{"):
        return {str(k): str(v) for k, v in json.loads(text).items()}
    out = {}
    for ln in text.splitlines():
        parts = ln.split()
        if len(parts) >= 2 and not ln.lstrip().startswith("#"):
            out[parts[0]] = parts[1]
    return out


def migrate_actions(project: str, write: bool = False, root: Optional[str] = None,
                    offline: bool = False, names: Optional[Dict[str, str]] = None) -> Dict:
    """ACTIONS.md items -> one ticket each, losslessly (design §7.2). Every item's lines are
    copied verbatim into its ticket body; `tickets/MIGRATION-actions.tsv` maps legacy id ->
    ticket; ACTIONS.md then says `tracker: tickets` and keeps its text (git holds the rest).
    Refuses if any ticket would change Orders' reading (key, open/closed, his, due, title).
    A write needs `names`: legacy item id -> SUBJECT_INTENT_TWIST name for EVERY item; numbered
    CODE-n ids are never minted (the dry run lists the items still needing a name)."""
    homes = homes_for(project, root)
    home = homes[0]
    e = _entries(root)[home.project]
    repo = os.path.realpath(os.path.expanduser(e.path))
    plan, sha = migration_plan(repo, project, root)
    report = {"items": len(plan), "by_status": {}, "commit": sha, "repo": repo}
    for p in plan:
        report["by_status"][p["status"]] = report["by_status"].get(p["status"], 0) + 1
    names = names or {}
    report["unnamed"] = [p["item"]["id"] for p in plan if p["item"]["id"] not in names]
    if not write:
        return report
    if report["unnamed"]:
        raise Refused("migrate-actions needs a SUBJECT_INTENT_TWIST name for every item (--names FILE); "
                      "missing: %s" % ", ".join(report["unnamed"][:10]))
    taken = {os.path.basename(p)[:-3] for h in all_homes(root) for p in _ticket_files(h.dir)}
    ids = [names[p["item"]["id"]] for p in plan]
    for tid in ids:
        probs = ticketname.problems(tid, taken)
        if probs:
            raise Refused("bad ticket name %s: %s" % (tid, "; ".join(probs)))
        taken.add(tid)
    fh = _lock(home.code)
    try:
        now = _now()
        files, rows = [], ["legacy_id\tticket\tline\tstate\tstatus\tactions_commit"]
        for tid, p in zip(ids, plan):
            it = p["item"]
            f = {"id": tid, "title": it["title"], "project": home.pid, "type": p["type"],
                 "status": p["status"], "owner": p["owner"], "created": _stamp(now),
                 "updated": _stamp(now), "source": "migrated:actions"}
            if p["due"]:
                f["due"] = p["due"]
            if it["est_min"]:
                f["effort"] = "%dm" % it["est_min"]
            if p["reason"]:
                f["close_reason"], f["closed"] = p["reason"], _stamp(now)
            if it["pointer"]:
                f["links"] = "ob:%s" % it["pointer"]
            f.update({"legacy_id": it["id"], "orders_key": it["key"],
                      "section": it["section"]})
            for k in ("kind", "group"):
                if it[k]:
                    f["legacy_" + k] = it[k]
            body = ("\nMigrated verbatim from `ACTIONS.md` line %d (commit %s), section \"%s\":\n\n"
                    "%s\n" % (it["line"], sha, it["section"], "\n".join(p["raw"])))
            body = _with_log(body, "migrated from ACTIONS.md line %d at %s" % (it["line"], sha), now)
            path = os.path.join(home.dir, tid + ".md")
            if os.path.exists(path):
                raise Refused("%s already exists; nothing written" % path)
            files.append((path, f, body))
            rows.append("%s\t%s\t%d\t%s\t%s\t%s" % (it["id"], tid, it["line"], it["state"],
                                                    p["status"], sha))
        # Orders must read the same thing before and after
        for (path, f, body), p in zip(files, plan):
            it = p["item"]
            t = dict(f, _path=path, _body=body)
            got = orders_items_one(t)
            want = (it["key"], it["state"] in ("open", "progress"), it["mine"], it["due"], it["title"])
            have = (got["key"], got["state"] in ("open", "progress"), got["mine"], got["due"], got["title"])
            if want != have:
                raise Refused("migration would change Orders for %s: %r -> %r" % (it["id"], want, have))
            rt = frontmatter.parse(render(f, body))[0]
            if rt.get("title") != it["title"]:
                raise Refused("title does not round-trip for %s" % it["id"])
            if any(line not in body for line in p["raw"]):
                raise Refused("body lost a line of %s" % it["id"])
        for path, f, body in files:
            _write(path, f, body)
        tsv = os.path.join(home.dir, "MIGRATION-actions.tsv")
        with open(tsv, "w", encoding="utf-8") as out:
            out.write("\n".join(rows) + "\n")
        actions = os.path.join(repo, "ACTIONS.md")
        text = open(actions, encoding="utf-8").read()
        new = re.sub(r"(?m)^tracker:\s*\w+\s*$", "tracker: tickets", text, count=1)
        if new == text:
            new = text.replace("---\n", "---\ntracker: tickets\n", 1)
        banner = ("\n> **Moved to tickets (Lodestar D30, %s).** Every item below is now a ticket in "
                  "`tickets/` (map: `tickets/MIGRATION-actions.tsv`); file and work them with "
                  "`python3 ~/dev/Lodestar/bin/ticket.py ... --project %s`. The item lines below are "
                  "a frozen copy as of commit %s, kept until he rules (design §10 R4); Orders reads "
                  "the tickets, not these lines.\n" % (now.date().isoformat(), home.pid, sha))
        end = new.index("\n---", 3) + 4
        new = new[:end] + "\n" + banner + new[end:]
        with open(actions, "w", encoding="utf-8") as out:
            out.write(new)
        commit_files = [p for p, _, _ in files] + [tsv, actions]
        board = os.path.join(repo, "BOARD.md")
        if re.search(r"(?m)^board:\s*generated\s*$", new):
            with open(board, "w", encoding="utf-8") as out:
                out.write(board_md(home.project, load([home]), sha))
            commit_files.append(board)
        report["ids"] = (ids[0], ids[-1])
        msg = "ticket: migrate ACTIONS.md (%d items, %s..%s)" % (len(ids), ids[0], ids[-1])
        in_home = lambda f: os.path.realpath(f).startswith(os.path.realpath(home.repo) + os.sep)
        mine = [f for f in commit_files if in_home(f)]
        theirs = [f for f in commit_files if not in_home(f)]
        report["git"] = commit(home, mine, msg)
        if theirs:   # overlay home: the tracker change lives in the project repo, committed there
            report["git_project"] = commit(home._replace(repo=repo), theirs, "tracker: ACTIONS.md moved to tickets")
    finally:
        fh.close()
    return report


def orders_items_one(t: Dict[str, str]) -> Dict:
    d = os.path.dirname(t["_path"])
    path = t["_path"]
    tmp = {"_path": path}
    tmp.update(t)
    state_of = {"open": "open", "active": "progress", "blocked": "open", "proposed": "unverified",
                "done": "done", "dropped": "dropped"}
    due = t.get("due") or ""
    return {"key": t.get("orders_key") or "ticket:" + t["id"],
            "state": state_of.get(t.get("status") or "", "open"), "mine": mine(t),
            "due": due[:10], "title": t.get("title") or "", "dir": d}


def board_md(project: str, tickets: Sequence[Dict[str, str]], since: str = "") -> str:
    """BOARD.md for a project on tickets: generated, never hand-edited (D27 D1 kept)."""
    head = ("<!-- generated by ~/dev/Lodestar/bin/ticket.py from tickets/; do not edit -->\n")
    return head + index_md(project, list(tickets))


# --------------------------------------------------------------------------- Companion

def _code_of(project_id: str) -> str:
    try:
        from lib import burning as bn
        return bn.FOCUS_ABBREV.get(project_id.replace("_", "").replace("-", "").lower()) or ""
    except Exception:  # noqa: BLE001
        return ""


def face_block(rows: Sequence[Dict], codes: Dict[str, str], local: Optional[Dict] = None,
               now: Optional[datetime] = None) -> Tuple[Dict, Dict]:
    """(now.json `plots.tickets`, tickets-detail.json). The face carries opaque ids, tier, the
    project's 4-letter code and the due word only; titles and paths go to the 0600 detail file.
    `local` holds the panel's own marks ({tk_id: {closed|snoozed_until}}) so a press shows at
    once. Lamps never breathe: tickets are scheduled actions, not alerts (design §6.1)."""
    now = now or _now()
    local = local or {}
    items, detail = [], {}
    for r in rows:
        oid = opaque(r["ticket"])
        mark = local.get(oid) or {}
        until = mark.get("snoozed_until") or ""
        if mark.get("closed") or (until and until > now.isoformat()):
            continue
        code = codes.get(r["ticket"].split("-")[0]) or _code_of(r["project"]) or \
            r["ticket"].split("-")[0][:4]
        items.append({"id": oid, "tier": r["tier"], "project": code, "when": r["when"],
                      "breathe": False, "breathe_until": None, "incident": "", "acked": None,
                      "draft": None, "period": None, "phase": None, "glyph": "K",
                      "source": "ticket", "seal": None})
        detail[oid] = {"ticket": r["ticket"], "tier": r["tier"], "why": r["why"],
                       "when": r["when"], "project": code, "project_name": r["project"],
                       "who": " · ".join(x for x in (r["ticket"], r["type"],
                                                     ("due " + r["due"]) if r["due"] else "") if x),
                       "gist": r["title"], "evidence": "ticket %s, status %s" % (r["ticket"], r["status"]),
                       "open": r["path"], "source": "ticket", "stance_label": "Ticket",
                       "actions_mail": False}
    shown = items[:FACE_SLOTS]
    rest = items[FACE_SLOTS:]
    block = {"scope": "TICKETS", "state": "ok", "checked_at": _stamp(now), "items": shown,
             "more": {"red": sum(1 for i in rest if i["tier"] == "red"),
                      "amber": sum(1 for i in rest if i["tier"] == "amber"),
                      "verify": sum(1 for i in rest if i["tier"] == "soon")},
             "total": len(items)}
    return block, {"built_at": _stamp(now), "scope": "TICKETS", "items": detail}
