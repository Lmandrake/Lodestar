"""The Reckoning — Lodestar's obligation ledger (stage 1).

Spec: docs/superpowers/specs/2026-10-13-kairos-lodestar-obligations-design.md, with Lukas's
rulings of 2026-10-13 at the top of its Decisions section. In one paragraph: Bearing's
detectors (needs-you, the quiet-cycle judge) and the one-time Kairos import write PROPOSALS; only
Lukas turns one into an obligation (`accept`) or ends it (`done`, `drop`, `cancel`) — with one
delegation (owner, 2026-10-22): an agent or `bin/reconcile.py` may record `done` when the
obligation's attached outcome check (`check` field, `lib/outcome.py`) is SATISFIED; nothing but
his word ever drops, cancels or lapses one. Every proposal carries a `check`: a spec, or an
explicit coverage gap when its creator gave none (`make_event`). Every change
is one JSON line in an append-only, per-machine file in the private store
(`<private store>/obligations/events/<date>/<machine>.jsonl`, `lib.boundary`); state is a causal fold over
those lines, never stored. Lodestar (public) holds this code and, at most, ids and counts.

Four properties a fresh reader must not break:

* **Content lives only on the private host.** `store_root` refuses to write unless EVERY push URL
  of every remote of the checkout is on the private host (`boundary.host()`). `output_class`
  decides whether a surface may print titles (a private-only repo) or counts and ids only (anything else, because a
  github.com session's dialog is rendered into its committed `conversations/`).
* **Personal-class content never enters the store** (the reverse leak). Ruling 4 sends it to
  Hestia through her own door; `append` refuses it here, whoever the caller is.
* **Causality, not wall clock.** A transition names in `supersedes` the state heads it saw. Two
  unrelated heads in different states are a CONFLICT for a human, never resolved by `at`.
* **Detections are durable before anything is shown.** Bearing writes a detection to a
  machine-local outbox in the same step that records its alert; `drain` moves it into the
  ledger idempotently (observation ids are content-addressed).

The 14-day rule (ruling 7): an unaccepted proposal at 14 days gets ONE `final_alert` event,
recorded before the DM line that announces it; it stays proposed until he acknowledges it
(`ack`), and only an acknowledged proposal lapses. The verb for "not going to do it" is
`drop` (ruling 9, 08:25).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from lib import boundary, flockwait, machine, paths
except ImportError:  # pragma: no cover
    import boundary  # type: ignore
    import flockwait  # type: ignore
    import machine  # type: ignore
    import paths  # type: ignore

ROOT_ENV = "RECKONING_ROOT"


def private_host():
    """The private git host from `lib.boundary` (None: this deployment declares none)."""
    return boundary.host()


def default_root() -> str:
    return os.path.join(boundary.root_setting(), "obligations")


def private_class() -> str:
    """The class tag of private-sourced obligations, as this deployment's ledger stores it."""
    return boundary.private_class()
LAPSE_DAYS = 14
FINAL_ALERT_EVERY = None  # one final alert, ever (ruling 7)

#: Event types and the state each one PRODUCES (None = carries no state; it annotates).
STATE_OF = {
    "propose": "proposed", "accept": "open", "done": "done", "drop": "dropped",
    "cancel": "cancelled", "reopen": "open", "lapse": "lapsed",
}
ANNOTATIONS = ("observe", "amend", "snooze", "final_alert", "ack", "propose_cancel")
EVENT_TYPES = tuple(STATE_OF) + ANNOTATIONS
TERMINAL = ("done", "dropped", "cancelled", "lapsed")
LIVE = ("proposed", "open", "conflict")

#: Which human transitions are allowed from which folded state.
ALLOWED = {
    "accept": ("proposed", "conflict"),
    "done": ("proposed", "open", "conflict"),
    "drop": ("proposed", "open", "conflict"),
    "cancel": ("proposed", "open", "conflict"),
    "reopen": TERMINAL + ("conflict",),
    "snooze": ("proposed", "open", "conflict"),
    "amend": ("proposed", "open", "conflict"),
}
def classes():
    """What the store accepts; "personal" is refused."""
    return (private_class(), "unclassified")


class StoreUnavailable(Exception):
    """No ledger on this machine (no checkout), or the checkout may push somewhere off the private host."""


class Refused(Exception):
    """A write the ledger will not make: personal-class content, a bad id, a bad transition."""


# --------------------------------------------------------------------------- the boundary

SCHEMES = ("ssh", "https", "git+ssh", "ssh+git")


def is_private_url(url: str) -> bool:
    """True for an ssh/scp/https URL whose host is exactly the private host. Any other
    transport (a remote helper such as `ext::` or `bridge://`) is refused: git may hand it to
    a program that sends the bytes anywhere."""
    url = (url or "").strip()
    if not url or any(c.isspace() for c in url) or "::" in url:
        return False
    if "://" in url:
        scheme, rest = url.split("://", 1)
        if scheme.lower() not in SCHEMES:
            return False
        host = rest.split("/", 1)[0]
    else:  # scp form: [user@]host:path
        if ":" not in url or url.startswith("/"):
            return False
        host = url.split(":", 1)[0]
    host = host.rsplit("@", 1)[-1].split(":", 1)[0].lower()
    private = private_host()
    return bool(private) and host == private and _ssh_alias_ok(private)


_SSH_OK: Dict[str, bool] = {}


def _ssh_alias_ok(host: str) -> bool:
    """`Host <private>` / `HostName github.com` in ssh config would send a private-looking URL
    to a public host; URL text cannot see that, so ask ssh what it would really connect to."""
    if host not in _SSH_OK:
        try:
            out = subprocess.run(["ssh", "-G", host], capture_output=True, text=True,
                                 timeout=10)
            names = [l.split(None, 1)[1].strip().lower() for l in out.stdout.splitlines()
                     if l.lower().startswith("hostname ")]
            _SSH_OK[host] = out.returncode != 0 or not names or names[0] == host
        except (OSError, subprocess.SubprocessError):
            _SSH_OK[host] = True  # no ssh at all: an ssh push cannot happen either
    return _SSH_OK[host]


class GitTimeout(RuntimeError):
    """git did not answer in time: a busy machine, not a missing checkout."""


def _git(repo: str, *args: str) -> Optional[str]:
    try:
        out = subprocess.run(["git", "-C", repo] + list(args), capture_output=True,
                             text=True, timeout=20)
    except subprocess.TimeoutExpired:
        # 2026-10-15: at load ~85 a 20 s rev-parse timed out and was reported as "no private store
        # checkout here", which read as a broken install. Say what actually happened.
        raise GitTimeout("git %s timed out after 20 s in %s (machine busy?)" % (args[0], repo))
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def repo_top(path: str) -> Optional[str]:
    out = _git(path, "rev-parse", "--show-toplevel")
    return out.strip() if out else None


def push_urls(repo: str) -> List[str]:
    """Every push destination of every remote (a `pushurl` beside a private fetch URL counts)."""
    remotes = (_git(repo, "remote") or "").split()
    urls: List[str] = []
    for name in remotes:
        out = _git(repo, "remote", "get-url", "--push", "--all", name) or ""
        urls.extend(u.strip() for u in out.splitlines() if u.strip())
        fetch = (_git(repo, "remote", "get-url", "--all", name) or "").splitlines()
        urls.extend(u.strip() for u in fetch if u.strip())
    # A push default or a branch push remote may name a URL directly instead of a remote.
    config = _git(repo, "config", "--get-regexp",
                  r"^(remote\.pushdefault|branch\..*\.(pushremote|remote))$") or ""
    for line in config.splitlines():
        parts = line.split(None, 1)
        value = parts[1].strip() if len(parts) == 2 else ""
        if value and value != "." and value not in remotes:
            urls.append(value)
    return urls


def private_only(repo: str) -> bool:
    urls = push_urls(repo)
    return bool(urls) and all(is_private_url(u) for u in urls)


def _contained(path: str, top: str) -> bool:
    """`path` (a directory) really lies inside checkout `top`: no symlink out of it, and no
    nested repository of its own that would commit the bytes somewhere else."""
    real, real_top = os.path.realpath(path), os.path.realpath(top)
    if not (real == real_top or real.startswith(real_top + os.sep)):
        return False
    if _git_between(real, real_top):
        return False                     # a nested repo: refused without asking git
    if _fresh_top(real_top):
        return True                      # verified checkout, nothing nested: no spawn
    inner = repo_top(path)
    return inner is not None and os.path.realpath(inner) == real_top


# ---- the safety-verdict cache (audit 2026-10-19, proposal 6) --------------------------------
# A click (handled, snooze) once spawned ~12 git/ssh processes for this boundary and timed out
# at load 36. The verdict "checkout T pushes only to the private host" is a pure function of a few
# config files, so it is remembered in machine-local state (never synced) for SAFETY_TTL_S,
# keyed on T, the stat of each of those files and the GIT_* environment. Only a PASS is cached;
# a refusal, a stale or missing entry or an unreadable cache runs the live check. The symlink and nested-repo containment checks stay live (filesystem, no spawn).

SAFETY_TTL_S = 600.0
SAFETY_CACHE_NAME = "reckoning-safety-cache.json"


def _safety_cache_path() -> str:
    return paths.state_file(SAFETY_CACHE_NAME)


def _config_files(top: str) -> List[str]:
    home = os.path.expanduser("~")
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return [os.path.join(top, ".git", "config"), os.path.join(home, ".gitconfig"),
            os.path.join(xdg, "git", "config"), "/etc/gitconfig",
            os.path.join(home, ".ssh", "config"), os.path.join(home, ".ssh"),
            "/etc/ssh/ssh_config"]


def _fingerprint(top: str) -> Optional[List]:
    """stat of every file that can change where `top` pushes; None = not cacheable."""
    if not os.path.isdir(os.path.join(top, ".git")):
        return None                      # a gitfile/worktree checkout: always live
    # git reads its environment too, so the GIT_* variables are part of the key
    out: List = [["env"] + sorted("%s=%s" % kv for kv in os.environ.items()
                                  if kv[0].startswith("GIT_"))]
    for f in _config_files(top):
        try:
            st = os.stat(f)
            out.append([f, st.st_mtime_ns, st.st_size, st.st_ino])
        except OSError:
            out.append([f, None])
    return out


def _git_between(real: str, real_top: str) -> bool:
    """A `.git` in any directory from `real` up to (not including) `real_top`."""
    d = real
    while d != real_top and d.startswith(real_top + os.sep):
        if os.path.lexists(os.path.join(d, ".git")):
            return True
        d = os.path.dirname(d)
    return False


def _load_cache() -> Dict:
    try:
        with open(_safety_cache_path()) as fh:
            c = json.load(fh)
        return c if isinstance(c, dict) and isinstance(c.get("tops"), dict) else {"tops": {}}
    except (OSError, ValueError):
        return {"tops": {}}


def _fresh_top(real_top: str) -> bool:
    e = _load_cache()["tops"].get(real_top)
    if not isinstance(e, dict):
        return False
    import time as _t
    age = _t.time() - float(e.get("at") or 0)
    if not (0 <= age < SAFETY_TTL_S):
        return False
    fp = _fingerprint(real_top)
    return fp is not None and fp == e.get("fp")


def _cached_top(parent: str) -> Optional[str]:
    """The deepest fresh verified checkout containing `parent` with no repo nested between."""
    real = os.path.realpath(parent)
    best = None
    for t in _load_cache()["tops"]:
        if (real == t or real.startswith(t + os.sep)) and (best is None or len(t) > len(best)):
            best = t
    if best is None or _git_between(real, best) or not _fresh_top(best):
        return None
    return best


def _remember(top: str) -> None:
    real_top = os.path.realpath(top)
    fp = _fingerprint(real_top)
    if fp is None:
        return
    import time as _t
    c = _load_cache()
    c["tops"][real_top] = {"at": _t.time(), "fp": fp}
    path = _safety_cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "w") as fh:
            json.dump(c, fh, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass                             # no cache is only slower, never less safe


def store_root(root: Optional[str] = None) -> str:
    """The ledger directory, or `StoreUnavailable` naming why there is none on this machine."""
    root = os.path.expanduser(root or os.environ.get(ROOT_ENV) or default_root())
    parent = root
    while parent and not os.path.isdir(parent):
        parent = os.path.dirname(parent)
    cached = _cached_top(parent) if parent else None
    if cached is not None:
        if os.path.isdir(root) and not _contained(root, cached):
            raise StoreUnavailable("%s does not stay inside %s" % (root, cached))
        return root
    try:
        top = repo_top(parent) if parent else None
    except GitTimeout as exc:
        raise StoreUnavailable(str(exc))
    if top is None:
        raise StoreUnavailable("no private store checkout here (%s)" % root)
    try:
        private = private_only(top)
    except GitTimeout as exc:
        raise StoreUnavailable(str(exc))
    if not private:
        raise StoreUnavailable("%s may push somewhere other than %s; refusing to write"
                               % (top, private_host() or "the private host"))
    if os.path.isdir(root) and not _contained(root, top):
        raise StoreUnavailable("%s does not stay inside %s" % (root, top))
    _remember(top)
    return root


def output_class(cwd: Optional[str] = None) -> str:
    """`content` only in a session whose repo pushes solely to the private host; else `counts`."""
    top = repo_top(cwd or os.getcwd())
    return "content" if top and private_only(top) else "counts"


# --------------------------------------------------------------------------- identities

def _h(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def obs_id(source: str, account: str, message: str, detector: str, cluster: str = "") -> str:
    """One detection of one possible ask in one message; content-addressed."""
    return "obs_" + _h(source, account, message, detector, cluster)[:20]


def ob_id(first_obs: str) -> str:
    return "ob_" + _h(first_obs)[:20]


def _canonical(event: Dict) -> str:
    body = {k: v for k, v in event.items() if k != "id"}
    return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def event_id(event: Dict) -> str:
    return "ev_" + _h(_canonical(event))[:20]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_at(text: str) -> datetime:
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def make_event(ev: str, ob: str, by: str, at: Optional[str] = None,
               supersedes: Sequence[str] = (), **fields) -> Dict:
    if ev not in EVENT_TYPES:
        raise Refused("unknown event type %r" % ev)
    event = {"ev": ev, "ob": ob, "at": at or now_iso(), "by": by,
             "supersedes": sorted(set(supersedes))}
    event.update({k: v for k, v in fields.items() if v is not None})
    if ev == "propose":
        event.setdefault("class", private_class())
        # Creator boundary (2026-10-22): every obligation enters with a check or a named gap.
        try:
            from lib import outcome
        except ImportError:  # pragma: no cover
            import outcome  # type: ignore
        try:  # the gap's review date follows the event's own clock, so ids stay deterministic
            today = parse_at(event["at"]).date()
        except (ValueError, TypeError):
            today = None
        try:
            event["check"] = outcome.creator_check(
                event.get("check"), "%s: no mechanical outcome check" % (
                    event.get("origin") or event.get("kind") or "proposal"), today=today)
        except ValueError as exc:
            raise Refused("bad outcome check: %s" % exc)
    event["id"] = event_id(event)
    return event


def valid_due(text: Optional[str]) -> str:
    """Typed field (CLAUDE.md §7): a full ISO date or empty. A partial date yields none."""
    text = (text or "").strip()
    try:
        return date.fromisoformat(text).isoformat() if len(text) == 10 else ""
    except ValueError:
        return ""


# --------------------------------------------------------------------------- the store

def _lock_path() -> str:
    return paths.state_file("locks", "reckoning.lock")


@contextlib.contextmanager
def _locked(lock_path: Optional[str] = None):
    import fcntl
    path = lock_path or _lock_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as handle:
        flockwait.exclusive(handle, "reckoning")  # bounded (2026-10-15 Bearing hang)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _repair_torn(path: str) -> bool:
    """Truncate a final line that has no newline (a torn write). Caller holds the lock."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False
    with open(path, "rb+") as fh:
        data = fh.read()
        if data.endswith(b"\n"):
            return False
        cut = data.rfind(b"\n") + 1
        fh.truncate(cut)
    return True


def _checkout_of(root: str) -> str:
    parent = root
    while parent and not os.path.isdir(parent):
        parent = os.path.dirname(parent)
    top = _cached_top(parent) or repo_top(parent)
    if top is None:
        raise StoreUnavailable("no checkout for %s" % root)
    return top


def _check(event: Dict) -> None:
    if not isinstance(event, dict):
        raise Refused("not an event object")
    if (event.get("ev") == "propose" or "class" in event) and event.get("class") not in classes():
        raise Refused("the ledger holds private-class obligations only; a %s-class one goes to "
                      "Hestia through her door (ruling 4, 2026-10-13)" % event.get("class"))
    if event.get("ev") not in EVENT_TYPES:
        raise Refused("unknown event type %r" % event.get("ev"))
    if event.get("id") != event_id(event):
        raise Refused("event id does not match its content")
    if event.get("ev") == "propose" and event.get("ob") != ob_id(event.get("obs", "")):
        raise Refused("a proposal's obligation id must be derived from its observation id")
    if (event.get("ev") in STATE_OF and event.get("ev") != "propose"
            and not event.get("supersedes")):
        raise Refused("a %s must name the state it was written against" % event.get("ev"))


def append(events: Iterable[Dict], root: Optional[str] = None,
           machine_key: Optional[str] = None, lock_path: Optional[str] = None) -> List[str]:
    """Append events to THIS machine's file for each event's local date. Returns the paths."""
    events = list(events)
    for event in events:
        _check(event)
    if not events:
        return []
    root = store_root(root)
    with _locked(lock_path):
        return _append_held(events, root, machine_key)


def _append_held(events: List[Dict], root: str, machine_key: Optional[str] = None) -> List[str]:
    """`append`'s write, for a caller that already holds the ledger lock."""
    for event in events:
        _check(event)
    top = _checkout_of(root)
    key = machine_key or machine.key()
    written: List[str] = []
    for event in events:
        day = parse_at(event["at"]).astimezone().date().isoformat()
        path = os.path.join(root, "events", day, "%s.jsonl" % key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not _contained(os.path.dirname(path), top):
            raise Refused("%s leaves the private checkout (symlink or nested repo)"
                          % os.path.dirname(path))
        _repair_torn(path)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        if path not in written:
            written.append(path)
    return written


def read_events(root: Optional[str] = None) -> Tuple[List[Dict], List[str]]:
    """Every event in the store, deduplicated by id, plus warnings (torn or bad lines)."""
    root = os.path.expanduser(root or os.environ.get(ROOT_ENV) or default_root())
    base = os.path.join(root, "events")
    seen: Dict[str, Dict] = {}
    warnings: List[str] = []
    if not os.path.isdir(base):
        return [], warnings
    for day in sorted(os.listdir(base)):
        folder = os.path.join(base, day)
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(folder, name)
            with open(path, encoding="utf-8") as fh:
                data = fh.read()
            lines = data.split("\n")
            if lines and lines[-1] != "":
                warnings.append("torn final line ignored: %s" % os.path.relpath(path, root))
            for n, line in enumerate(lines[:-1], 1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    _check(event)
                except Exception as exc:  # any bad line is a warning, never a dead ledger
                    warnings.append("bad line %s:%d (%s)" % (os.path.relpath(path, root), n,
                                                             exc))
                    continue
                seen.setdefault(event["id"], event)
    return list(seen.values()), warnings


# --------------------------------------------------------------------------- the fold

FIELDS = ("title", "owner", "kind", "due", "due_note", "project", "note", "origin",
          "estimate", "energy", "flavor", "today", "created", "class",
          # Orders: a "that reply didn't finish it" correction voids earlier evidence.
          "void_before",
          # The outcome registry (2026-10-22): a check spec or gap; `amend` replaces it.
          "check",
          # TO DO chores (lib/chores.py, burning v2 C3): where `due` came from (mail = read
          # from an issuer's text, never red; said = his word) and the template/instance.
          "due_source", "chore")


def _order_key(event: Dict, depth: Dict[str, int]) -> Tuple:
    return (depth.get(event["id"], 0), event.get("at", ""), event["id"])


def _depths(events: List[Dict]) -> Dict[str, int]:
    """Longest supersedes-chain depth of each event, over events present (cycle-safe)."""
    by_id = {e["id"]: e for e in events}
    depth: Dict[str, int] = {}

    def walk(eid: str, stack: frozenset) -> int:
        if eid in depth:
            return depth[eid]
        if eid in stack:
            return 0
        parents = [p for p in by_id[eid].get("supersedes", []) if p in by_id]
        d = 1 + max((walk(p, stack | {eid}) for p in parents), default=-1)
        depth[eid] = d
        return d

    for eid in by_id:
        walk(eid, frozenset())
    return depth


def fold(events: Iterable[Dict], as_of: Optional[datetime] = None) -> Dict[str, Dict]:
    """State of every obligation. Pure: the same event SET gives the same result in any order,
    and `as_of` is the only clock (used for age, overdue and snooze)."""
    as_of = as_of or datetime.now(timezone.utc)
    by_ob: Dict[str, List[Dict]] = {}
    for event in {e["id"]: e for e in events}.values():
        by_ob.setdefault(event["ob"], []).append(event)
    result: Dict[str, Dict] = {}
    for ob, evs in by_ob.items():
        proposes = [e for e in evs if e["ev"] == "propose"]
        if not proposes:
            continue  # transitions for an obligation this machine has not seen proposed yet
        depth = _depths(evs)
        evs.sort(key=lambda e: _order_key(e, depth))
        first = min(proposes, key=lambda e: (e.get("at", ""), e["id"]))
        rec: Dict = {"ob": ob, "proposed_at": first["at"], "sources": [], "observations": [],
                     "attach_keys": [], "events": len(evs), "snoozed_until": "",
                     "final_alert": "", "acked": False, "cancel_evidence": [],
                     "said": [], "possibly_same_as": first.get("possibly_same_as", [])}
        for name in FIELDS:
            rec[name] = first.get(name, "" if name != "today" else False)
        rec["class"] = first.get("class", private_class())
        for event in evs:
            if event.get("obs") and event["obs"] not in rec["observations"]:
                rec["observations"].append(event["obs"])
            if event.get("attach_key") and event["attach_key"] not in rec["attach_keys"]:
                rec["attach_keys"].append(event["attach_key"])
            for src in event.get("sources", []) or []:
                if src not in rec["sources"]:
                    rec["sources"].append(src)
            if event.get("said"):
                rec["said"].append(event["said"])
            kind = event["ev"]
            if kind == "amend" or kind in ("accept", "reopen"):
                for name in FIELDS:
                    if name in event and name not in ("origin",):
                        rec[name] = event[name]
            elif kind == "snooze":
                rec["snoozed_until"] = event.get("until", "")
            elif kind == "final_alert":
                rec["final_alert"] = rec["final_alert"] or event["at"]
            elif kind == "ack" or (kind == "lapse" and event.get("ack")):
                rec["acked"] = True
            elif kind == "propose_cancel":
                rec["cancel_evidence"].append({k: event.get(k, "") for k in
                                               ("at", "note", "quote", "obs")})
        # State: heads of the DAG of state-bearing events. A proposal is a root, never a head
        # once any transition exists (its descendants may arrive before their parents do).
        # Connectivity: a transition counts only if it descends from this obligation's own
        # proposal, so an event naming an unknown parent (not synced yet, or forged) cannot
        # close anything. Every proposal of one obligation is one root.
        root_ids = {e["id"] for e in proposes}
        connected = set(root_ids)
        changed = True
        while changed:
            changed = False
            for e in evs:
                if e["id"] not in connected and any(p in connected
                                                    for p in e.get("supersedes", [])):
                    connected.add(e["id"])
                    changed = True
        states = [e for e in evs if e["ev"] in STATE_OF]
        transitions = [e for e in states if e["ev"] != "propose"]
        rec["orphans"] = sum(1 for e in transitions if e["id"] not in connected)
        transitions = [e for e in transitions if e["id"] in connected]
        pool = transitions or proposes
        superseded = {p for e in pool for p in e.get("supersedes", [])}
        if superseded & root_ids:
            superseded |= root_ids
        heads = sorted((e for e in pool if e["id"] not in superseded),
                       key=lambda e: e["id"])
        head_states = sorted({STATE_OF[e["ev"]] for e in heads})
        rec["heads"] = [e["id"] for e in heads]
        referenced = {p for e in evs for p in e.get("supersedes", [])}
        rec["tips"] = sorted(e["id"] for e in evs if e["id"] not in referenced
                             and e["id"] in connected and e["ev"] != "propose")
        if len(head_states) == 1:
            rec["state"] = head_states[0]
            rec["conflict"] = []
        else:
            rec["state"] = "conflict"
            rec["conflict"] = [{"id": e["id"], "state": STATE_OF[e["ev"]], "by": e["by"],
                                "at": e["at"]} for e in heads]
        # a chore template he has accepted once is filed without asking again (owner 10-20)
        rec["accepted_ever"] = any(e["ev"] == "accept" and e["id"] in connected for e in evs)
        rec["age_days"] = (as_of - parse_at(first["at"])).total_seconds() / 86400.0
        # Last sign of life: a new observation or any word from him (not the tick's alert).
        rec["last_at"] = max((e["at"] for e in evs if e["ev"] != "final_alert" and e.get("at")),
                             key=parse_at, default=first["at"])
        rec["overdue"] = bool(rec["state"] == "open" and rec["due"]
                              and rec["due"] < as_of.astimezone().date().isoformat())
        rec["snoozed"] = bool(rec["snoozed_until"]
                              and rec["snoozed_until"] > as_of.astimezone().date().isoformat())
        result[ob] = rec
    return result


def counts(state: Dict[str, Dict], as_of: Optional[datetime] = None) -> Dict[str, int]:
    """`proposed` counts every proposal; `aging` is the subset quiet for reckqueue.AGING_DAYS
    (the sticky line shows it apart; its state is unchanged)."""
    try:
        from lib import reckqueue
    except ImportError:  # pragma: no cover
        import reckqueue  # type: ignore
    c = {"open": 0, "overdue": 0, "proposed": 0, "conflict": 0, "awaiting_ack": 0,
         "unclassified": 0, "aging": 0}
    for rec in state.values():
        if rec["state"] == "open":
            c["open"] += 1
            c["overdue"] += int(rec["overdue"])
        elif rec["state"] == "proposed":
            c["proposed"] += 1
            c["aging"] += int(reckqueue.is_aging(rec, as_of))
            if rec["final_alert"] and not rec["acked"]:
                c["awaiting_ack"] += 1
            if rec.get("class") == "unclassified":
                c["unclassified"] += 1
        elif rec["state"] == "conflict":
            c["conflict"] += 1
    return c


def resolve(state: Dict[str, Dict], prefix: str) -> Dict:
    prefix = prefix.strip()
    if not prefix.startswith("ob_"):
        prefix = "ob_" + prefix
    if len(prefix) < 3 + 6:
        raise Refused("give at least 6 characters of the id")
    hits = [rec for ob, rec in state.items() if ob.startswith(prefix)]
    if len(hits) != 1:
        raise Refused("%d obligations match %s" % (len(hits), prefix))
    return hits[0]


# --------------------------------------------------------------------------- transitions

def transition(prefix: str, ev: str, by: str, root: Optional[str] = None,
               as_of: Optional[datetime] = None, said: str = "",
               machine_key: Optional[str] = None, **fields) -> Dict:
    """A human verb: accept, done, drop, cancel, reopen, snooze, amend, ack. Writes against the
    obligation's current heads, so causality is recorded rather than inferred from clocks."""
    events, _ = read_events(store_root(root))
    state = fold(events, as_of)
    rec = resolve(state, prefix)
    seen = sorted(set(rec["heads"]) | set(rec["tips"]))
    if ev == "ack":
        if not rec["final_alert"] or rec["acked"] or rec["state"] != "proposed":
            raise Refused("%s has no final alert awaiting acknowledgement" % rec["ob"])
        # ONE event, so no crash can leave it acknowledged but not lapsed.
        lapse = make_event("lapse", rec["ob"], by, supersedes=seen, ack=True,
                           said=said or None,
                           note="acknowledged the final alert; lapsed unaccepted")
        append([lapse], root, machine_key)
        return {"ob": rec["ob"], "state": "lapsed"}
    if ev not in ALLOWED:
        raise Refused("unknown verb %s" % ev)
    if rec["state"] not in ALLOWED[ev]:
        raise Refused("%s is %s; cannot %s it" % (rec["ob"], rec["state"], ev))
    if ev == "accept" and rec.get("class") == "unclassified" and fields.get("class") != private_class():
        raise Refused("%s is unclassified: accept it with --%s, or send a personal one to "
                      "Hestia (reckon.py hestia %s)" % (rec["ob"], private_class(), rec["ob"][:12]))
    if "due" in fields and fields["due"] is not None:
        if fields["due"] and not valid_due(fields["due"]):
            raise Refused("due must be a full YYYY-MM-DD date (or empty to clear it)")
        fields["due"] = valid_due(fields["due"])
        # C10' (burning v2): a date given with his words is the first authority; any other
        # date set here is not his word, so it clears an earlier `said`
        fields["due_source"] = "said" if (said and fields["due"]) else ""
    keep = {k: v for k, v in fields.items()
            if v is not None and (v != "" or k in ("due", "due_source"))}
    if said:
        keep["said_auth"] = "recorded owner statement; unauthenticated"  # outcome.OWNER_WORD
    event = make_event(ev, rec["ob"], by, supersedes=seen, said=said or None, **keep)
    append([event], root, machine_key)
    return {"ob": rec["ob"], "state": STATE_OF.get(ev, rec["state"])}


def version(rec: Dict) -> str:
    """A token that changes whenever any event lands on the obligation (the resolver's guard)."""
    return "%d:%s" % (rec.get("events", 0), ",".join(sorted(set(rec["heads"]) | set(rec["tips"]))))


def close_if_satisfied(ob: str, expect_version: str, by: str, note: str,
                       root: Optional[str] = None, machine_key: Optional[str] = None,
                       lock_path: Optional[str] = None) -> Dict:
    """Record `done` for an obligation whose outcome check was SATISFIED, only if nothing
    landed on it since `expect_version` (re-folded under the ledger lock). Never any other
    verb: satisfied is not dropped, cancelled or lapsed. Refused(stale) otherwise."""
    root = store_root(root)
    with _locked(lock_path):
        rec = fold(read_events(root)[0]).get(ob)
        if rec is None:
            raise Refused("%s is not in the ledger" % ob)
        if version(rec) != expect_version:
            raise Refused("stale: %s changed since its check ran" % ob)
        if rec["state"] not in ("proposed", "open"):
            raise Refused("%s is %s; the resolver closes only proposed or open" % (ob, rec["state"]))
        seen = sorted(set(rec["heads"]) | set(rec["tips"]))
        event = make_event("done", ob, by, supersedes=seen, note=note[:300] or None,
                           closed_by="outcome-check")
        _append_held([event], root, machine_key)
    return {"ob": ob, "state": "done"}


def tick(root: Optional[str] = None, as_of: Optional[datetime] = None, by: str = "",
         machine_key: Optional[str] = None) -> List[Dict]:
    """Record the 14-day final alert for every proposal that reached it. Recorded BEFORE any
    DM shows it (a crash after this leaves it visible in every count, never lost)."""
    as_of = as_of or datetime.now(timezone.utc)
    events, _ = read_events(store_root(root))
    state = fold(events, as_of)
    fresh = []
    for rec in state.values():
        if rec["state"] != "proposed" or rec["final_alert"] or rec["snoozed"]:
            continue
        if rec["age_days"] >= LAPSE_DAYS:
            fresh.append(_final_alert_event(rec))
    append(fresh, root, machine_key)
    return fresh


def _final_alert_event(rec: Dict) -> Dict:
    """Identical on every machine (fixed author, time and parents), so two Macs ticking before
    they sync record ONE final alert, deduplicated by id."""
    when = parse_at(rec["proposed_at"]) + timedelta(days=LAPSE_DAYS)
    return make_event("final_alert", rec["ob"], "reckoning/tick",
                      at=when.isoformat(timespec="seconds"), supersedes=[])


def _by(role: str) -> str:
    try:
        return "%s/%s" % (machine.key(), role)
    except Exception:  # pragma: no cover
        return "unknown/%s" % role


# --------------------------------------------------------------------------- the outbox

def outbox_path() -> str:
    return paths.state_file("reckoning-outbox.jsonl")


def outbox_put(detection: Dict, path: Optional[str] = None) -> None:
    """Durably queue one detection (machine-local, outside every repo). Idempotent on obs."""
    path = path or outbox_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if detection["obs"] in {d.get("obs") for d in _read_outbox(path)}:
        return
    with _locked(path + ".lock"):
        _repair_torn(path)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(detection, sort_keys=True, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())


def _read_outbox(path: str) -> List[Dict]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    for line in lines[:-1]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _detection_events(det: Dict, state: Dict[str, Dict]) -> List[Dict]:
    """Turn one queued detection into ledger events against the current fold.

    `action` is propose (default), amend (judge `corrected`) or propose_cancel (judge
    `cancelled`). Attachment is conservative: only when exactly ONE live obligation already
    holds an observation with the same attach key (detector + thread + ask); otherwise a new
    proposal, marked "possibly the same as" any partial match."""
    action = det.get("action", "propose")
    key = det.get("attach_key", "")
    if str(det.get("origin", "")).startswith("chore:"):
        return _chore_events(det, state)
    live = [r for r in state.values() if r["state"] in LIVE]
    same = [r for r in live if key and key in r["attach_keys"]]
    thread = det.get("thread", "")
    near = [r["ob"] for r in live if thread and any(
        k.split("|")[1:2] == [thread] for k in r["attach_keys"])]
    fields = {k: det.get(k) for k in FIELDS if det.get(k) not in (None, "")}
    fields["due"] = valid_due(det.get("due"))
    common = dict(by=det.get("by") or _by("bearing"), at=det["at"],
                  obs=det["obs"], attach_key=key or None, sources=det.get("sources") or [])
    fresh_ask = action == "propose" and str(det.get("origin", "")).startswith("judge:")
    if len(same) == 1 and not fresh_ask:  # a judged NEW obligation is new by definition
        rec = same[0]
        if action == "propose":
            return [make_event("observe", rec["ob"], supersedes=rec["heads"] + rec["tips"],
                               **common)]
        if action == "amend":
            amend = {k: v for k, v in fields.items() if k in ("due", "due_note", "note") and v}
            return [make_event("amend", rec["ob"], supersedes=rec["heads"] + rec["tips"],
                               **common, **amend)]
        if action == "propose_cancel":
            return [make_event("propose_cancel", rec["ob"],
                               supersedes=rec["heads"] + rec["tips"],
                               note=det.get("note", ""), quote=det.get("quote", ""), **common)]
    twin = _restated(det, state) if action == "propose" and not fresh_ask else None
    if twin is not None:  # the same ask again (a reply quoting it, a reworded extraction)
        return [make_event("observe", twin["ob"], supersedes=twin["heads"] + twin["tips"],
                           **common)]
    if action == "propose_cancel":
        fields["kind"] = "cancel_notice"
        fields.setdefault("title", "Possible cancellation: %s" % det.get("title", ""))
    fields.setdefault("owner", "lukas")
    return [make_event("propose", ob_id(det["obs"]), possibly_same_as=sorted(set(near)) or None,
                       **common, **fields)]


def _chore_events(det: Dict, state: Dict[str, Dict]) -> List[Dict]:
    """A TO DO chore detection (lib/chores.py): repeat -> observe, issuer completion -> done
    for the matching chores only, else a proposal (accepted at once if he accepted the
    template before). May return [] (a completion matching no live chore)."""
    try:
        from lib import chores
    except ImportError:  # pragma: no cover
        import chores  # type: ignore
    common = dict(by=det.get("by") or _by("bearing"), at=det["at"])
    evs = chores.ledger_events(det, state, {}, common)
    if evs is not None:
        return evs
    fields = {k: det.get(k) for k in FIELDS if det.get(k) not in (None, "")}
    fields["due"] = valid_due(det.get("due"))
    fields.setdefault("owner", "lukas")
    near = []
    if det.get("thread"):  # no stable instance: a merge is only ever PROPOSED (G4)
        near = sorted(r["ob"] for r in state.values() if r["state"] in LIVE
                      and r.get("origin") == det.get("origin")
                      and any("|msg:" in k for k in r["attach_keys"]))
    prop = make_event("propose", ob_id(det["obs"]), common["by"], at=det["at"],
                      obs=det["obs"], attach_key=det.get("attach_key") or None,
                      sources=det.get("sources") or [], possibly_same_as=near or None,
                      **fields)
    return [prop] + chores.auto_accept(det, state, prop, common["by"])


def _restated(det: Dict, state: Dict[str, Dict]) -> Optional[Dict]:
    """Bearing-side dedup (upgrade #3, 2026-10-19): an EXTRACTED ask that restates one already
    in the ledger, in the same thread, on the same side (his ask vs. someone owing him), by
    `asksim.same_ask`. It becomes an observation on that record instead of a new proposal;
    a dropped, done, cancelled or lapsed one stays closed (his word on it stands), a live one
    gains the evidence. Other detectors are never merged here (needs-you beside an extraction
    stays "possibly the same as", the spec's conservative attachment)."""
    if not str(det.get("origin", "")).startswith("extract"):
        return None
    thread = det.get("thread", "")
    if not thread:
        return None
    try:
        from lib import asksim
    except ImportError:  # pragma: no cover
        import asksim  # type: ignore
    side = (det.get("owner") or "lukas") == "lukas"
    quote = det.get("quote") or ""
    hits = []
    for rec in state.values():
        if rec["state"] == "conflict" or not str(rec.get("origin", "")).startswith("extract"):
            continue
        if ((rec.get("owner") or "lukas") == "lukas") != side:
            continue
        srcs = [s for s in rec.get("sources") or [] if s.get("thread") == thread]
        if not srcs:
            continue
        if any(asksim.same_ask(rec.get("title") or "", det.get("title") or "",
                               s.get("quote") or "", quote) for s in srcs):
            hits.append(rec)
    if not hits:
        return None
    return min(hits, key=lambda r: (r["state"] not in LIVE, r["proposed_at"], r["ob"]))


def drain(root: Optional[str] = None, path: Optional[str] = None,
          machine_key: Optional[str] = None) -> Dict:
    """Move queued detections into the ledger. A crash after the append but before the outbox
    is rewritten re-reads an observation already present and skips it."""
    path = path or outbox_path()
    queued = _read_outbox(path)
    if not queued:
        return {"drained": 0, "queued": 0}
    try:
        root = store_root(root)
    except StoreUnavailable as exc:
        return {"drained": 0, "queued": len(queued), "unavailable": str(exc)}
    drained = 0
    refused: List[str] = []
    events, _ = read_events(root)  # read once; extended in memory as this drain appends
    present_obs = {e.get("obs") for e in events}
    consumed = set()  # a detection that rightly wrote nothing (an issuer close matching none)
    for det in queued:
        if det["obs"] in present_obs:
            continue
        try:
            new_events = _detection_events(det, fold(events))
            if not new_events:
                consumed.add(det["obs"])
            append(new_events, root, machine_key)
            events = events + new_events
            present_obs.add(det["obs"])
            drained += 1
        except Refused as exc:
            refused.append("%s: %s" % (det["obs"], exc))
    with _locked(path + ".lock"):
        # Drop exactly what is now in the ledger; anything queued meanwhile is kept.
        present = {e.get("obs") for e in read_events(root)[0]}
        keep = [d for d in _read_outbox(path) if d["obs"] not in present | consumed]
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for d in keep:
                fh.write(json.dumps(d, sort_keys=True, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    out = {"drained": drained, "queued": len(keep)}
    if refused:
        out["refused"] = refused
    return out


def needs_you_detection(item: Dict, filed_to: str, fingerprint: str,
                        at: Optional[str] = None) -> Dict:
    """A needs-you alert as a queued detection (work mail only; zero model calls)."""
    message = item.get("event_id", "")
    thread = (item.get("threads") or ["event:%s" % message])[0]
    obs = obs_id("mail", private_class(), message, "needs_you", fingerprint)
    check = None
    conv, asked = item.get("conversation") or "", item.get("date") or ""
    if conv and asked:
        try:
            when = parse_at(str(asked)).astimezone(timezone.utc).isoformat(timespec="seconds")
            check = {"type": "conv_answered", "conversation": conv[:300], "after": when}
        except ValueError:
            check = None
    return {"obs": obs, "at": at or now_iso(), "action": "propose", "class": private_class(),
            "check": check,
            "attach_key": "needs_you|%s|%s" % (thread, fingerprint), "thread": thread,
            "title": (item.get("subject") or "").strip()[:200] or "(no subject)",
            "kind": "reply", "owner": "lukas", "origin": "needs_you", "project": filed_to,
            "sources": [{"type": "mail", "thread": thread, "message": message,
                         "link": item.get("pointer", ""), "from": item.get("who", "")}]}


def judge_detection(member: Dict, state_name: str, cluster_id: str, beacon_id: str,
                    quote: str, note: str, at: Optional[str] = None) -> Dict:
    """A verified judge verdict (new_obligation / corrected / cancelled) as a detection."""
    action = {"new_obligation": "propose", "corrected": "amend",
              "cancelled": "propose_cancel"}[state_name]
    thread = (member.get("threads") or ["event:%s" % member.get("event_id", "")])[0]
    obs = obs_id(member.get("source_label", ""), private_class(), member.get("event_id", ""),
                 "judge", "%s|%s" % (cluster_id, state_name))
    title = note or (member.get("subject") or "")
    return {"obs": obs, "at": at or now_iso(), "action": action, "class": private_class(),
            "attach_key": "judge|%s|%s" % (thread, beacon_id), "thread": thread,
            "title": title.strip()[:200], "kind": "beacon", "owner": "lukas",
            "origin": "judge:%s" % state_name, "project": member.get("scope") or beacon_id,
            "note": note, "quote": quote[:200],
            "sources": [{"type": member.get("source_label", ""), "thread": thread,
                         "message": member.get("event_id", ""),
                         "link": member.get("pointer", ""), "quote": quote[:200]}]}


_EXTRACT_KIND = {"ask": "ask", "promise": "promise", "waiting": "waiting_for",
                 "deadline": "deadline"}


def extract_detection(entry: Dict, action: Dict, filed_to: str = "",
                      at: Optional[str] = None) -> Dict:
    """One extracted action item (bin/lib/bearingextract.py, 2026-10-14) as a detection.

    Content-addressed on (source, message, normalized action text), so re-extracting the same
    message is the same observation; the attach key is (conversation, action text), so the
    same ask restated later in a conversation attaches to the live proposal instead of minting
    a second one. A different detector on the same message (needs-you) is NOT merged: it is
    proposed beside it as "possibly the same as" (the spec's conservative attachment)."""
    message = entry.get("event_id", "")
    what = " ".join(str(action.get("what") or "").split())
    fp = _h(" ".join(what.lower().split()))[:12]
    thread = entry.get("conversation") or (entry.get("threads") or [""])[0] or \
        "event:%s" % message
    source = entry.get("source", "")
    obs = obs_id(source, private_class(), message, "extract", fp)
    kind = _EXTRACT_KIND.get(action.get("type"), "ask")
    owner = "lukas" if kind != "waiting_for" else (action.get("owner") or "someone")
    if str(owner).strip().lower() in ("lukas", "lukas mandrake", "me", "i"):
        owner = "lukas"
    quote = str(action.get("quote") or "")[:200]
    origin = "extract-history:%s" % action.get("type") if entry.get("history") else \
        "extract:%s" % action.get("type")
    return {"obs": obs, "at": at or now_iso(), "action": "propose",
            "class": private_class() if entry.get("class") != "unclassified" else "unclassified",
            "attach_key": "extract|%s|%s" % (thread, fp), "thread": thread,
            "title": what[:200] or "(untitled action)", "kind": kind, "owner": owner,
            "due": action.get("due") or "", "origin": origin, "project": filed_to,
            "quote": quote,
            "sources": [{"type": source, "thread": thread, "message": message,
                         "link": entry.get("pointer", ""), "from": entry.get("who", ""),
                         "quote": quote}]}


# --------------------------------------------------------------------------- surfaces

def sticky_line(cwd: Optional[str] = None, root: Optional[str] = None,
                as_of: Optional[datetime] = None) -> List[str]:
    """The session-start print. Counts and ids only unless the session's repo is private-only."""
    try:
        root = store_root(root)
    except StoreUnavailable as exc:
        return ["Reckoning: no ledger on this machine (%s)" % exc]
    events, warnings = read_events(root)
    state = fold(events, as_of)
    c = counts(state, as_of)
    line = "Reckoning: %d open (%d overdue), %d proposed" % (c["open"], c["overdue"],
                                                             c["proposed"] - c["aging"])
    if c["aging"]:  # quiet past reckqueue.AGING_DAYS: still proposed, kept out of the count
        line += " (+%d aging)" % c["aging"]
    if c["awaiting_ack"]:
        line += ", %d final alert%s awaiting ack" % (c["awaiting_ack"],
                                                     "" if c["awaiting_ack"] == 1 else "s")
    if c["conflict"]:
        line += ", %d conflict%s" % (c["conflict"], "" if c["conflict"] == 1 else "s")
    out = [line]
    if output_class(cwd) == "counts":
        if any(c.values()):
            out[0] += (" — read them from %s: python3 ~/dev/Lodestar/bin/reckon.py list"
                       % boundary.root_display())
        if warnings:  # a count only: a bad line's text may be private
            out.append("Reckoning: %d unreadable ledger line%s (details from %s)"
                       % (len(warnings), "" if len(warnings) == 1 else "s", boundary.root_display()))
        return out
    live = sorted((r for r in state.values() if r["state"] in LIVE),
                  key=_rank)
    for rec in live[:5]:
        out.append("  " + describe(rec))
    return out + ["Reckoning: %s" % w for w in warnings[:2]]


def _rank(rec: Dict) -> Tuple:
    order = {"conflict": 0, "open": 1, "proposed": 2}
    return (order.get(rec["state"], 9), not rec["overdue"],
            not (rec["final_alert"] and not rec["acked"]), rec["due"] or "9999",
            rec["proposed_at"])


def describe(rec: Dict) -> str:
    bits = [rec["ob"][:12], rec["state"]]
    if rec["overdue"]:
        bits.append("OVERDUE")
    if rec["due"]:
        bits.append("due %s" % rec["due"])
    if rec["final_alert"] and not rec["acked"]:
        bits.append("FINAL ALERT")
    if rec.get("class") == "unclassified":
        bits.append("unclassified")
    if rec["snoozed"]:
        bits.append("snoozed to %s" % rec["snoozed_until"])
    head = " ".join(bits)
    tail = " [%s]" % rec["project"] if rec.get("project") else ""
    return "%s — %s%s" % (head, rec.get("title") or "(untitled)", tail)


def notified_path() -> str:
    return paths.state_file("reckoning-notified.json")


def dm_line(root: Optional[str] = None, as_of: Optional[datetime] = None,
            path: Optional[str] = None, confirm: bool = True) -> Optional[str]:
    """One counts-and-ids line for the Bearing self-DM when something changed: new proposals,
    newly overdue, new final alerts, new conflicts. The last-notified sets are machine-local.
    Counts and ids only: the cron turn that sends it runs in Lodestar, a github.com repo."""
    try:
        root = store_root(root)
    except StoreUnavailable:
        return None
    state = fold(read_events(root)[0], as_of)
    now_sets = {
        "proposed": sorted(r["ob"] for r in state.values() if r["state"] == "proposed"),
        "overdue": sorted(r["ob"] for r in state.values() if r["overdue"]),
        "final": sorted(r["ob"] for r in state.values()
                        if r["state"] == "proposed" and r["final_alert"] and not r["acked"]),
        "conflict": sorted(r["ob"] for r in state.values() if r["state"] == "conflict"),
    }
    path = path or notified_path()
    try:
        with open(path, encoding="utf-8") as fh:
            before = json.load(fh)
    except (OSError, ValueError):
        before = {}
    new = {k: [o for o in v if o not in set(before.get(k, []))] for k, v in now_sets.items()}
    if confirm:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(now_sets, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
    if not any(new.values()):
        return None
    parts = []
    if new["proposed"]:
        parts.append("+%d proposed" % len(new["proposed"]))
    if new["overdue"]:
        parts.append("%d now overdue" % len(new["overdue"]))
    if new["final"]:
        parts.append("FINAL ALERT, 14 days unaccepted: %s (accept, drop, or ack to let it "
                     "lapse)" % ", ".join(o[:12] for o in new["final"]))
    if new["conflict"]:
        parts.append("%d conflict%s (closed on one Mac, reopened on the other): %s" % (
            len(new["conflict"]), "" if len(new["conflict"]) == 1 else "s",
            ", ".join(o[:12] for o in new["conflict"])))
    c = counts(state)
    return "Reckoning: %s — now %d open, %d proposed. Read: reckon.py list from %s" % (
        "; ".join(parts), c["open"], c["proposed"], boundary.root_display())
