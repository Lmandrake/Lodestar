"""The outcome registry: enforce before surfacing, for EVERY store (2026-10-22, Option 1).

Owner, 2026-10-22: anything provably satisfied is closed in the same turn it is discovered,
before any reminder is composed; agents may close provably-satisfied obligations and beacons as
`done`. This module is the extensible core of that rule. It knows nothing about any particular
store: stores, check kinds and creators plug in by REGISTRATION, never by editing the resolver
(`bin/lib/reconcile.py`). Built-in registrations live in `lib/outcome_stores.py`.

Verdicts are three-valued, and only one of them ever closes anything:

  SATISFIED      the world shows the outcome
  NOT_SATISFIED  the verifier ran and the outcome is not there
  UNKNOWN        the verifier could not tell (not authed, timeout, network, no check at all):
                 recorded as "verification unavailable", never as "not done", never closes

A **check spec** is a JSON object `{"type": <kind>, ...params}` in a CLOSED vocabulary:
`validate_spec` refuses an unknown kind, a missing or extra param, or a bad value, by name.
Two kinds are special:

* `gap` — `{"type": "gap", "reason": "...", "review_by": "YYYY-MM-DD"}`: an explicit,
  recorded "no mechanical check exists for this". Creators attach one by default when no check
  is given (`gap()`), so an item is never silently unchecked; its verdict is always UNKNOWN.
* `cmd` — `{"type": "cmd", "argv": [...], "trusted": true}`: runs a command, exit 0 =
  satisfied. Accepted only with `trusted=True` (an owner-side CLI flag), and only EVALUATED
  for a store that declares `trusted_cmd_ok` (machine-local stores). A spec that arrived by
  git from another checkout must never run a command.

Verdicts are cached per canonical spec (`CACHE_FILE`, machine-local, per-kind TTL), and every
verdict the resolver obtains is recorded per item (`VERDICT_FILE`): an UNKNOWN is a fact
about the verifier, kept so `bin/truth_divergence.py` and reminders can say so.

How to add a CHECK KIND
-----------------------
    outcome.register_check("gh_pr_merged", {"repo": is_repo, "number": is_int}, run_fn,
                           ttl_min=10)

`run_fn(spec, limit_s) -> Result` must never raise for an operational failure: return
UNKNOWN. Return SATISFIED only for exact evidence of the outcome; a "closed", "cancelled" or
"terminal" state that is not the outcome itself is NOT_SATISFIED (a cancelled ticket does not
prove the ask was met). `local=True` marks a cheap, side-effect-free check (no cache).

How to add a STORE
------------------
Subclass `Store`, implement `items()` (and, if its items can be closed, `close()`), then
`outcome.register_store(MyStore())` at import of your module (or `register_store_factory`
for a lazily built one). That is all: `bin/reconcile.py`, `bin/truth_divergence.py`, the
handoff pointer adapter and the `item_state` check find it through the registry.

* `items(include_closed=False)` yields `Item`s: `id`, `state`, `live`, the decoded `check`
  (None, a gap, or a spec), a `version` token that changes whenever the stored item changes,
  `closable`, and a `label` that is safe to print in ANY session (ids, never titles).
* `close(item, result, by)` closes ONE item as DONE through the store's own verb, and only if
  its stored version still equals `item.version` (else raise `Stale`). It must never drop,
  cancel, lapse or otherwise end an item in any other way: satisfied is not abandoned.
* Optional: `lookup(id)`, `owns(id)` (cheap id-pattern test), `attach(id, spec)` (store a
  check), `record(item, result)` (default: the verdict file), `flush()` (commit/push once per
  run), `intrinsic` (a non-empty string when the store has its own built-in outcome rule, so an
  item without a check is still covered — e.g. alerts close on Lukas's reply).

How to make a CREATOR attach a check
------------------------------------
At the one function every new item goes through (not in a template), call
`outcome.creator_check(given_spec, default_reason)`: it validates a given spec, or returns an
explicit gap naming why none was given. A creator CLI offers `--check JSON` and `--gap REASON`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

try:
    from lib import paths
except ImportError:  # pragma: no cover
    import paths  # type: ignore

SATISFIED, NOT_SATISFIED, UNKNOWN = "SATISFIED", "NOT_SATISFIED", "UNKNOWN"
VERDICTS = (SATISFIED, NOT_SATISFIED, UNKNOWN)
TIMEOUT_S = 20
DEFAULT_TTL_MIN = 10
GAP_REVIEW_DAYS = 14
#: Bumped whenever a check kind's verdict rule changes: it is part of every cache key (an old
#: verdict is never reused) and of the `by` of every resolver close (LODE-23, 2026-10-27).
CHECKER_VERSION = "checks-2"
#: What an owner-word closure is: his words as recorded by an agent, not an authenticated act.
OWNER_WORD = "recorded owner statement; unauthenticated"
CACHE_FILE = ("outcome", "cache.json")
VERDICT_FILE = ("outcome", "verdicts.json")


@dataclass(frozen=True)
class Result:
    state: str
    reason: str = ""
    cached: bool = False

    @property
    def ok(self) -> bool:
        return self.state == SATISFIED


class Stale(Exception):
    """The stored item changed after its check ran: the close is skipped, never forced."""


class Refused(Exception):
    """A store refused an attach or close by name."""


# --------------------------------------------------------------------------- vocabulary

_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_LOGIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,100}$")
_DAY = re.compile(r"^\d{4}-\d\d-\d\d$")
_STORE = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")


def is_repo(v) -> bool:
    return isinstance(v, str) and bool(_REPO.match(v))


def is_login(v) -> bool:
    return isinstance(v, str) and bool(_LOGIN.match(v))


def is_pos_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def is_text(v) -> bool:
    return isinstance(v, str) and bool(v.strip()) and len(v) <= 300 and "\n" not in v


def is_day(v) -> bool:
    if not (isinstance(v, str) and _DAY.match(v)):
        return False
    try:
        date.fromisoformat(v)
        return True
    except ValueError:
        return False


def is_iso(v) -> bool:
    if not isinstance(v, str) or not v:
        return False
    try:
        datetime.fromisoformat(v)
        return True
    except ValueError:
        return False


def is_abs_path(v) -> bool:
    return isinstance(v, str) and os.path.isabs(os.path.expanduser(v)) and "\n" not in v


@dataclass
class CheckKind:
    name: str
    params: Dict[str, Callable[[Any], bool]]
    run: Callable[[Dict, float], Result]
    optional: Dict[str, Callable[[Any], bool]] = field(default_factory=dict)
    ttl_min: float = DEFAULT_TTL_MIN
    local: bool = False
    #: may a SATISFIED verdict of this kind close an item by itself? A bool, or a predicate on
    #: the spec. False = the kind establishes a FACT (a file exists, a commit is on a branch),
    #: not that the obligation is complete (LODE-23).
    closes: Any = True


_CHECKS: Dict[str, CheckKind] = {}


def register_check(name: str, params: Dict[str, Callable[[Any], bool]],
                   run: Callable[[Dict, float], Result], optional=None,
                   ttl_min: float = DEFAULT_TTL_MIN, local: bool = False,
                   closes: Any = True) -> None:
    """Add a check kind to the closed vocabulary. Re-registering a name replaces it.
    `closes=False` (or a predicate on the spec) marks a fact-grade kind: its SATISFIED is
    evidence, never by itself a closure."""
    if name in ("gap", "cmd") or not re.match(r"^[a-z][a-z0-9_]{1,40}$", name):
        raise ValueError("bad or reserved check kind name %r" % name)
    _CHECKS[name] = CheckKind(name, dict(params), run, dict(optional or {}), ttl_min, local,
                              closes)


def closure_grade(spec: Optional[Dict]) -> tuple:
    """(may close, reason): whether a SATISFIED verdict of this spec may close an item on its
    own. Facts are not completion: a draft exists, an unrelated commit landed (LODE-23)."""
    if not spec or is_gap(spec):
        return False, "no outcome check"
    kind = spec.get("type")
    if kind == "cmd":
        return True, ""
    ck = _CHECKS.get(kind or "")
    if ck is None:
        load_builtins()
        ck = _CHECKS.get(kind or "")
    if ck is None:
        return False, "unknown check kind %r" % kind
    try:
        ok = ck.closes(spec) if callable(ck.closes) else bool(ck.closes)
    except Exception:  # noqa: BLE001 - a grade that cannot be decided does not close
        ok = False
    return (True, "") if ok else (False, "%s establishes a fact, not completion" % kind)


def check_kinds() -> List[str]:
    load_builtins()
    return sorted(set(_CHECKS) | {"gap", "cmd"})


def validate_spec(spec, trusted: bool = False) -> Dict:
    """The spec if it is in the closed vocabulary, else ValueError naming the problem."""
    if isinstance(spec, str):
        spec = decode(spec)
    if not isinstance(spec, dict):
        raise ValueError("check spec must be an object")
    kind = spec.get("type")
    if kind == "gap":
        extra = set(spec) - {"type", "reason", "review_by"}
        if extra:
            raise ValueError("gap has unknown params: %s" % ", ".join(sorted(extra)))
        if not is_text(spec.get("reason")):
            raise ValueError("gap needs a one-line reason")
        if not is_day(spec.get("review_by")):
            raise ValueError("gap needs review_by YYYY-MM-DD")
        return {"type": "gap", "reason": spec["reason"].strip(), "review_by": spec["review_by"]}
    if kind == "cmd":
        if not trusted:
            raise ValueError("type 'cmd' is only accepted through a trusted (owner-side) flag")
        argv = spec.get("argv")
        if not (isinstance(argv, list) and argv and all(isinstance(x, str) for x in argv)):
            raise ValueError("cmd needs a non-empty argv list of strings")
        return {"type": "cmd", "argv": list(argv), "trusted": True}
    ck = _CHECKS.get(kind)
    if ck is None:
        load_builtins()  # store-specific kinds (conv_answered, ...) register with their store
        ck = _CHECKS.get(kind)
    if ck is None:
        raise ValueError("unknown check type %r (known: %s)" % (kind, ", ".join(check_kinds())))
    params = set(spec) - {"type"}
    missing = set(ck.params) - params
    extra = params - set(ck.params) - set(ck.optional)
    if missing:
        raise ValueError("%s is missing: %s" % (kind, ", ".join(sorted(missing))))
    if extra:
        raise ValueError("%s has unknown params: %s" % (kind, ", ".join(sorted(extra))))
    for k, ok in list(ck.params.items()) + list(ck.optional.items()):
        if k in spec and not ok(spec[k]):
            raise ValueError("%s: bad value for %s" % (kind, k))
    return dict(spec)


def is_gap(spec: Optional[Dict]) -> bool:
    return isinstance(spec, dict) and spec.get("type") == "gap"


def gap(reason: str, today: Optional[date] = None, days: int = GAP_REVIEW_DAYS) -> Dict:
    """An explicit coverage gap: 'no mechanical check for this item', with a review date."""
    today = today or date.today()
    reason = " ".join((reason or "no outcome check given").split())[:300]
    return {"type": "gap", "reason": reason,
            "review_by": (today + timedelta(days=days)).isoformat()}


def creator_check(given=None, default_reason: str = "no outcome check given at creation",
                  trusted: bool = False, today: Optional[date] = None) -> Dict:
    """What a creator stores: the validated given spec (dict or JSON text), else a gap."""
    if given in (None, "", {}):
        return gap(default_reason, today)
    return validate_spec(given, trusted=trusted)


def encode(spec: Optional[Dict]) -> str:
    """Compact canonical JSON, for a markdown cell or a frontmatter value. '' for None."""
    if not spec:
        return ""
    return json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def decode(text) -> Optional[Dict]:
    """A stored spec back to a dict; None for empty. ValueError for unparseable text."""
    if isinstance(text, dict):
        return text
    text = (text or "").strip()
    if not text:
        return None
    try:
        out = json.loads(text)
    except ValueError as e:
        raise ValueError("check is not JSON: %s" % e)
    if not isinstance(out, dict):
        raise ValueError("check must be a JSON object")
    return out


def spec_key(spec: Dict) -> str:
    return hashlib.sha256((CHECKER_VERSION + encode(spec)).encode("utf-8")).hexdigest()[:20]


# --------------------------------------------------------------------------- evaluation

def run(spec: Optional[Dict], limit: Optional[float] = None, allow_cmd: bool = False) -> Result:
    """Run one check now. Never raises; anything but a clear yes/no is UNKNOWN."""
    if not spec:
        return Result(UNKNOWN, "no check attached")
    limit = min(limit or TIMEOUT_S, TIMEOUT_S)
    try:
        if isinstance(spec, str):
            spec = decode(spec)
        kind = spec.get("type")
        if kind == "gap":
            return Result(UNKNOWN, "coverage gap: %s" % spec.get("reason", ""))
        if kind == "cmd":
            if spec.get("trusted") is not True:
                return Result(UNKNOWN, "cmd check is not trusted")
            if not allow_cmd:
                return Result(UNKNOWN, "cmd checks do not run for this store")
            validate_spec(spec, trusted=True)
            rc = subprocess.run(spec["argv"], capture_output=True, timeout=limit).returncode
            return (Result(SATISFIED, "check passed: %s" % " ".join(spec["argv"])[:100])
                    if rc == 0 else Result(NOT_SATISFIED, "command exited %d" % rc))
        validate_spec(spec)
        res = _CHECKS[kind].run(spec, limit)
        if not isinstance(res, Result) or res.state not in VERDICTS:
            return Result(UNKNOWN, "check %s returned no verdict" % kind)
        return res
    except ValueError as e:
        return Result(UNKNOWN, "invalid check: %s" % e)
    except subprocess.TimeoutExpired:
        return Result(UNKNOWN, "check timed out")
    except Exception as e:  # noqa: BLE001 - a broken verifier is UNKNOWN, never a crash
        return Result(UNKNOWN, "check could not run: %s" % type(e).__name__)


def _ttl_min(spec: Dict) -> float:
    ck = _CHECKS.get(spec.get("type") or "")
    if ck is None:
        return 0 if is_gap(spec) else DEFAULT_TTL_MIN
    return 0 if ck.local else ck.ttl_min


@contextlib.contextmanager
def _locked_json(parts):
    """(data, save) for one machine-local JSON file, under an exclusive bounded lock."""
    import fcntl
    try:
        from lib import flockwait
    except ImportError:  # pragma: no cover
        import flockwait  # type: ignore
    path = paths.state_file(*parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "a") as lock:
        flockwait.exclusive(lock, "outcome", timeout=10)
        try:
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                if not isinstance(data, dict):
                    data = {}
            except (OSError, ValueError):
                data = {}

            def save():
                tmp = path + ".tmp.%d" % os.getpid()
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, sort_keys=True, indent=0)
                os.replace(tmp, path)
            yield data, save
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _read_json(parts) -> Dict:
    try:
        with open(paths.state_file(*parts), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def evaluate(spec: Optional[Dict], limit: Optional[float] = None, use_cache: bool = True,
             now: Optional[datetime] = None, allow_cmd: bool = False) -> Result:
    """`run`, through the per-spec cache (a verdict younger than its kind's TTL is reused)."""
    if not spec:
        return Result(UNKNOWN, "no check attached")
    if isinstance(spec, str):
        try:
            spec = decode(spec)
        except ValueError as e:
            return Result(UNKNOWN, "invalid check: %s" % e)
    ttl = _ttl_min(spec)
    now = now or datetime.now(timezone.utc)
    key = spec_key(spec)
    if use_cache and ttl > 0:
        hit = _read_json(CACHE_FILE).get(key)
        if hit:
            try:
                if now - datetime.fromisoformat(hit["at"]) < timedelta(minutes=ttl):
                    return Result(hit["state"], hit.get("reason", ""), cached=True)
            except (KeyError, ValueError, TypeError):
                pass
    res = run(spec, limit, allow_cmd=allow_cmd)
    if ttl > 0 and not is_gap(spec):
        try:
            with _locked_json(CACHE_FILE) as (data, save):
                data[key] = {"state": res.state, "reason": res.reason,
                             "at": now.isoformat(timespec="seconds"), "type": spec.get("type")}
                cutoff = (now - timedelta(days=2)).isoformat()
                for k in [k for k, v in data.items() if (v.get("at") or "") < cutoff]:
                    data.pop(k, None)
                save()
        except Exception:  # noqa: BLE001 - a cache that cannot be written is only slower
            pass
    return res


def record_verdict(store: str, item_id: str, spec: Optional[Dict], res: Result,
                   now: Optional[datetime] = None) -> None:
    """Keep the latest verdict per item (machine-local), UNKNOWN included."""
    now = now or datetime.now(timezone.utc)
    with _locked_json(VERDICT_FILE) as (data, save):
        data["%s:%s" % (store, item_id)] = {
            "state": res.state, "reason": res.reason[:200],
            "at": now.isoformat(timespec="seconds"),
            "type": (spec or {}).get("type", ""), "spec": spec_key(spec) if spec else ""}
        save()


def verdicts() -> Dict[str, Dict]:
    return _read_json(VERDICT_FILE)


def verdict_for(store: str, item_id: str) -> Optional[Dict]:
    return verdicts().get("%s:%s" % (store, item_id))


# --------------------------------------------------------------------------- stores

@dataclass
class Item:
    store: str
    id: str
    state: str
    live: bool
    check: Optional[Dict] = None
    version: str = ""
    closable: bool = True
    label: str = ""
    raw: Any = None
    note: str = ""

    @property
    def coverage(self) -> str:
        if self.check is None:
            return "none"
        return "gap" if is_gap(self.check) else "check"


class Store:
    """One adapter per store. See the module docstring, 'How to add a STORE'."""
    name = ""
    trusted_cmd_ok = False
    intrinsic = ""

    def items(self, include_closed: bool = False) -> Iterable[Item]:
        raise NotImplementedError

    def owns(self, item_id: str) -> bool:
        return True

    def lookup(self, item_id: str) -> Optional[Item]:
        if not self.owns(item_id):
            return None
        for it in self.items(include_closed=True):
            if it.id == item_id:
                return it
        return None

    def close(self, item: Item, result: Result, by: str) -> str:
        raise Refused("%s items cannot be closed by the resolver" % self.name)

    def attach(self, item_id: str, spec: Dict) -> str:
        raise Refused("%s does not take attached checks" % self.name)

    def record(self, item: Item, result: Result, now: Optional[datetime] = None) -> None:
        record_verdict(self.name, item.id, item.check, result, now)

    def flush(self) -> List[str]:
        return []

    def may_close(self, item: Item) -> tuple:
        """(may close, reason) for a SATISFIED item. Default: the check kind's closure grade.
        A store narrows it to what its items mean (e.g. a reply closes only a reply owed)."""
        return closure_grade(item.check)


_STORES: Dict[str, Store] = {}
_FACTORIES: Dict[str, Callable[[], Store]] = {}
_BUILTINS = ["unloaded"]


def register_store(store: Store) -> Store:
    if not _STORE.match(store.name or ""):
        raise ValueError("bad store name %r" % store.name)
    _STORES[store.name] = store
    _FACTORIES.pop(store.name, None)
    return store


def register_store_factory(name: str, factory: Callable[[], Store]) -> None:
    """A store built on first use (keeps imports of heavy store modules lazy)."""
    if not _STORE.match(name or ""):
        raise ValueError("bad store name %r" % name)
    _FACTORIES[name] = factory


def unregister_store(name: str) -> None:
    _STORES.pop(name, None)
    _FACTORIES.pop(name, None)


def load_builtins() -> None:
    if _BUILTINS[0] == "loaded":
        return
    _BUILTINS[0] = "loaded"
    try:
        from lib import outcome_stores  # noqa: F401  (registers on import)
    except ImportError:  # pragma: no cover
        import outcome_stores  # type: ignore  # noqa: F401


def store_names() -> List[str]:
    load_builtins()
    return sorted(set(_STORES) | set(_FACTORIES))


def get_store(name: str) -> Optional[Store]:
    load_builtins()
    if name not in _STORES and name in _FACTORIES:
        register_store(_FACTORIES[name]())
    return _STORES.get(name)


def stores(names: Optional[Iterable[str]] = None) -> List[Store]:
    out = []
    for n in (list(names) if names else store_names()):
        s = get_store(n)
        if s is not None:
            out.append(s)
    return out


def find_item(item_id: str, names: Optional[Iterable[str]] = None) -> Optional[Item]:
    """The one item with this id in any registered store (first owning store wins)."""
    for s in stores(names):
        try:
            if not s.owns(item_id):
                continue
            it = s.lookup(item_id)
        except Exception:  # noqa: BLE001 - an unavailable store is not a match
            continue
        if it is not None:
            return it
    return None


@contextlib.contextmanager
def isolated(builtin_checks: bool = True):
    """Tests: a registry with no stores (and, optionally, no checks), restored afterwards."""
    saved = (dict(_STORES), dict(_FACTORIES), dict(_CHECKS), _BUILTINS[0])
    _STORES.clear()
    _FACTORIES.clear()
    if not builtin_checks:
        _CHECKS.clear()
    _BUILTINS[0] = "loaded"
    try:
        yield
    finally:
        _STORES.clear()
        _STORES.update(saved[0])
        _FACTORIES.clear()
        _FACTORIES.update(saved[1])
        _CHECKS.clear()
        _CHECKS.update(saved[2])
        _BUILTINS[0] = saved[3]


# --------------------------------------------------------------------------- built-in checks

def _gh_status(path: str, limit: float):
    """(HTTP code or None, parsed JSON body or None, short error)."""
    p = subprocess.run(["gh", "api", "-i", path], capture_output=True, text=True, timeout=limit)
    out = (p.stdout or "").lstrip()
    m = re.match(r"HTTP/\S+\s+(\d{3})", out)
    if not m:
        return None, None, " ".join((p.stderr or "").split())[:80] or "rc=%d" % p.returncode
    body = None
    parts = re.split(r"\r?\n\r?\n", out, maxsplit=1)
    if len(parts) == 2 and parts[1].strip():
        try:
            body = json.loads(parts[1])
        except ValueError:
            body = None
    return int(m.group(1)), body, ""


def _gh_collaborator(spec: Dict, limit: float) -> Result:
    what = "%s on %s" % (spec["login"], spec["repo"])
    code, _, err = _gh_status("repos/%s/collaborators/%s" % (spec["repo"], spec["login"]), limit)
    if code is None:
        return Result(UNKNOWN, "gh gave no HTTP status (%s)" % err)
    if code == 204:
        return Result(SATISFIED, "%s is a collaborator on %s" % (spec["login"], spec["repo"]))
    if code == 404:
        return Result(NOT_SATISFIED, "%s is not a collaborator" % what)
    return Result(UNKNOWN, "gh answered HTTP %d for %s" % (code, what))


def _gh_pr_merged(spec: Dict, limit: float) -> Result:
    what = "%s#%d" % (spec["repo"], spec["number"])
    code, body, err = _gh_status("repos/%s/pulls/%d" % (spec["repo"], spec["number"]), limit)
    if code is None:
        return Result(UNKNOWN, "gh gave no HTTP status (%s)" % err)
    if code != 200 or not isinstance(body, dict):
        return Result(UNKNOWN, "gh answered HTTP %d for %s" % (code, what))
    if body.get("merged") is True:
        return Result(SATISFIED, "%s is merged" % what)
    return Result(NOT_SATISFIED, "%s is %s, not merged" % (what, body.get("state") or "?"))


def _gh_issue_completed(spec: Dict, limit: float) -> Result:
    what = "%s#%d" % (spec["repo"], spec["number"])
    code, body, err = _gh_status("repos/%s/issues/%d" % (spec["repo"], spec["number"]), limit)
    if code is None:
        return Result(UNKNOWN, "gh gave no HTTP status (%s)" % err)
    if code != 200 or not isinstance(body, dict):
        return Result(UNKNOWN, "gh answered HTTP %d for %s" % (code, what))
    if body.get("state") == "closed" and body.get("state_reason") == "completed":
        return Result(SATISFIED, "%s closed as completed" % what)
    # closed as not_planned / duplicate is terminal, not satisfied
    return Result(NOT_SATISFIED, "%s is %s (%s)" % (what, body.get("state") or "?",
                                                    body.get("state_reason") or "open"))


def _git_contains(spec: Dict, limit: float) -> Result:
    repo = os.path.expanduser(spec["repo_path"])
    if not os.path.isdir(repo):
        return Result(UNKNOWN, "no checkout at %s on this machine" % spec["repo_path"])
    p = subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", spec["commit"],
                        spec["ref"]], capture_output=True, text=True, timeout=limit)
    if p.returncode == 0:
        return Result(SATISFIED, "%s is on %s" % (spec["commit"][:10], spec["ref"]))
    if p.returncode == 1:
        return Result(NOT_SATISFIED, "%s is not on %s (as last fetched)"
                      % (spec["commit"][:10], spec["ref"]))
    return Result(UNKNOWN, "git could not compare (%s)" % " ".join((p.stderr or "").split())[:60])


def _path_exists(spec: Dict, limit: float) -> Result:
    p = os.path.expanduser(spec["path"])
    if os.path.exists(p):
        return Result(SATISFIED, "%s exists" % spec["path"])
    parent = os.path.dirname(p)
    if not os.path.isdir(parent):
        return Result(UNKNOWN, "parent of %s is not on this machine" % spec["path"])
    return Result(NOT_SATISFIED, "%s does not exist" % spec["path"])


def _item_state(spec: Dict, limit: float) -> Result:
    """Another store's item is in exactly the named state. 'done' is not 'dropped': a
    terminal state that is not the outcome does not satisfy (GPT review, 2026-10-22)."""
    s = get_store(spec["store"])
    if s is None:
        return Result(UNKNOWN, "no store %r registered" % spec["store"])
    try:
        it = s.lookup(spec["id"])
    except Exception as e:  # noqa: BLE001
        return Result(UNKNOWN, "store %s unavailable: %s" % (spec["store"], type(e).__name__))
    if it is None:
        return Result(UNKNOWN, "%s %s not found on this machine" % (spec["store"], spec["id"]))
    if it.id != spec["id"]:      # a prefix lookup found some other item: not this one
        return Result(UNKNOWN, "%s %s resolved to a different id" % (spec["store"], spec["id"]))
    if it.state == spec["state"]:
        return Result(SATISFIED, "%s %s is %s" % (spec["store"], spec["id"], it.state))
    return Result(NOT_SATISFIED, "%s %s is %s, not %s" % (spec["store"], spec["id"], it.state,
                                                          spec["state"]))


register_check("gh_collaborator", {"repo": is_repo, "login": is_login}, _gh_collaborator)
register_check("gh_pr_merged", {"repo": is_repo, "number": is_pos_int}, _gh_pr_merged)
register_check("gh_issue_completed", {"repo": is_repo, "number": is_pos_int},
               _gh_issue_completed)
def _item_state_closes(spec: Dict) -> bool:
    """Only the target store's own DONE state is an outcome; 'open', 'closed' (a beacon he
    ended) or 'dropped' is a fact about that item, not completion of this one."""
    s = get_store(spec.get("store") or "")
    return s is not None and spec.get("state") in getattr(s, "done_states", ())


# Fact-grade kinds (closes=False): a commit on a branch or a path on disk does not say WHAT
# was done (an unrelated commit, a draft, a stale artifact): evidence for a human, never a
# closure by itself (LODE-23).
register_check("git_contains", {"repo_path": is_abs_path,
                                "commit": lambda v: isinstance(v, str) and bool(_SHA.match(v)),
                                "ref": lambda v: isinstance(v, str) and bool(_REF.match(v))},
               _git_contains, local=True, closes=False)
register_check("path_exists", {"path": is_abs_path}, _path_exists, local=True, closes=False)
register_check("item_state", {"store": lambda v: isinstance(v, str) and bool(_STORE.match(v)),
                              "id": is_text, "state": is_text}, _item_state, local=True,
               closes=_item_state_closes)


def gap_overdue(spec: Optional[Dict], today: Optional[date] = None) -> bool:
    if not is_gap(spec):
        return False
    try:
        return date.fromisoformat(spec["review_by"]) < (today or date.today())
    except (KeyError, ValueError):
        return True


def monotonic_deadline(budget_s: Optional[float]) -> Optional[float]:
    return time.monotonic() + budget_s if budget_s else None
