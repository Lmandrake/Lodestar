"""Repo mirrors: one repo on github.com and on the private git host, kept level by the sweep.

Design: `docs/superpowers/specs/2026-10-19-repo-mirrors-design.md` (v2). Owner rulings, both
2026-10-19: **two-way sync**, and **every Mac that reaches both hosts pushes** (races are detected and
retried next run, never flagged).

The shape, and why (each D-number is the design doc's):

* **D1** the registry `Mirror` column names the second home, `https://` only.
* **D2** replication runs in a machine-local BARE repo, `~/dev/.lodestar/mirrors/<id>.git`,
  never in the working checkout. The checkout's remotes are never touched, so the existing
  `push_url_mismatches` / `declared_remote_audit` stay correct and `lifecycle` has nothing to
  "repair". Both URLs are passed literally; `git ls-remote --get-url` (which applies any
  `insteadOf`) must resolve to the declared host before any network call.
* **D3** two-way by fast-forward or a clean merge, never force, never rewrite. Origin is
  always pushed before mirror, so a crash between them leaves "mirror behind".
* **D4** no skip cache: every eligible run observes both tips live.
* **D5** outcomes come from `--porcelain` push status and from explicit error text, never
  from grepping for "non-fast-forward".
* **D6** commits crossing INTO the github.com side are scanned with `proseguard.markers`
  (messages and added lines); a hit holds everything.

A mirror state never changes a repo's ordinary sweep outcome: `run()` returns its own
`Outcome` list, which the sweep records under `mirrors` in sync-status.json.

Python 3.9 compatible (launchd runs /usr/bin/python3).
"""

import fcntl
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from . import boundary, machine, paths, proseguard, registry, skillclone

GITHUB = "github.com"


def supported_pair() -> frozenset:
    """github.com and this deployment's private host (`lib.boundary`); github.com alone when
    none is declared, which no Remote/Mirror pair can satisfy."""
    return frozenset({GITHUB, boundary.host() or GITHUB})


def _mail_class() -> frozenset:
    """Mail-class repos: their Remote is the private host and a github.com side is forbidden by
    ruling. The private store itself is one."""
    return frozenset({"mail", boundary.store_name().lower()})

CALL_TIMEOUT = 60.0      # seconds, per git call
REPO_DEADLINE = 300.0    # seconds, per repo
UNVERIFIED_AMBER = 24 * 3600
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

GITHUB_NAME = "Lukas Mandrake"
NOREPLY_EMAIL = "313496996+lmandrake@users.noreply.github.com"


def github_email() -> str:
    """The merge commit's address on the github.com side: the configured public address, else
    the noreply identity."""
    return boundary.public_email() or NOREPLY_EMAIL


def own_emails() -> frozenset:
    return frozenset({github_email().lower(), NOREPLY_EMAIL}
                     | set(boundary.owner_addresses()))

# States (D5).
INELIGIBLE = "ineligible"
LEVEL = "level"
PUSHED = "pushed"
RACED = "raced"
TRANSPORT_UNKNOWN = "transport_unknown"
REFUSED = "refused"
CONFLICT = "conflict"
HELD = "held"
CONFIG_ERROR = "configuration_error"
DRY_RUN = "dry_run"

# Directions of a push.
MIRROR_BEHIND = "mirror-behind"
ORIGIN_BEHIND = "origin-behind"
MERGED = "merged"
MIRROR_CREATED = "mirror-created"

# How loudly a state is surfaced.
SILENT = "silent"
NOTE = "note"
AMBER = "amber"
FLAGGED = "flagged"

#: Explicit refusals (D5 `refused`). Anything else that fails is `transport_unknown`.
_REFUSAL_TEXT = (
    "permission denied", "authentication failed", "repository not found",
    "could not read username", "could not read password", "terminal prompts disabled",
    "access denied", "archived", "read-only", "returned error: 401", "returned error: 403",
    "returned error: 404", "does not appear to be a git repository", "not authorized",
)

#: Test seams. `_resolved_host` maps a resolved URL to its host; `_before_push` is called with
#: the side about to be pushed ("origin"/"mirror") and exists so a test can stage a race.
_before_push = None  # type: Optional[Callable[[str], None]]


def _resolved_host(url: str) -> str:
    return skillclone.remote_host(url)


class Outcome(NamedTuple):
    repo_id: str
    name: str
    mirror: str
    state: str
    detail: str
    direction: str = ""
    origin_tip: str = ""
    mirror_tip: str = ""
    colleague_commits: int = 0
    surface: str = SILENT
    confirmed_at: Optional[str] = None

    @property
    def label(self) -> str:
        return "%s:%s" % (self.state, self.direction) if self.direction else self.state


# --- row rules (shared with lib/validate.py, so runtime and validator cannot drift) -------------

def is_mail_class(entry: registry.Entry) -> bool:
    base = os.path.basename(entry.path.rstrip("/")).lower()
    return entry.repo_id.lower() in _mail_class() or base in _mail_class()


def row_faults(entry: registry.Entry) -> List[Tuple[str, str]]:
    """[(kind, sentence)] for the row-local D7 rules. [] for a row with no Mirror."""
    if not entry.mirror:
        return []
    who = entry.repo_id or entry.name
    found = []
    if is_mail_class(entry):
        found.append(("mirror-on-mail-class",
                      "row '%s' is Mail-class (its home is the private host by ruling) and may never carry a "
                      "Mirror" % who))
        return found
    if not entry.mirror.lower().startswith("https://"):
        found.append(("mirror-not-https",
                      "row '%s' Mirror must be an https:// URL (ssh comes from local "
                      "insteadOf), got %s" % (who, entry.mirror)))
    if skillclone.same_remote(entry.mirror, entry.remote):
        found.append(("mirror-equals-remote",
                      "row '%s' Mirror is the same repo as its Remote" % who))
    hosts = {skillclone.remote_host(entry.remote), skillclone.remote_host(entry.mirror)}
    if not entry.remote or hosts != supported_pair():
        found.append(("mirror-host-pair",
                      "row '%s' Remote and Mirror must be one github.com and one "
                      "%s (the only supported pair); got %s and %s"
                      % (who, boundary.host() or "private-host", entry.remote or "(no Remote)",
                         entry.mirror)))
    return found


def replicators(entry: registry.Entry, machines_path: Optional[str] = None) -> List[str]:
    """Machine keys that may replicate this row: carried there, Send on, BOTH hosts declared."""
    if not entry.send:
        return []
    roles = machine.roles(machines_path)
    hosts = machine.hosts_table(machines_path)
    found = []
    for key, role in sorted(roles.items()):
        if not registry.on_machine(entry, role or None, key):
            continue
        declared = set(hosts.get(key.lower(), []))
        if supported_pair() <= declared:
            found.append(key)
    return found


def eligible(entry: registry.Entry, role: Optional[str], key: str,
             machines_path: Optional[str] = None) -> Tuple[bool, str]:
    """Whether THIS machine replicates the row. Decided from declared facts, no network."""
    if entry.state != "active":
        return False, "row is %s" % entry.state
    if not entry.send:
        return False, "Send is off"
    if not registry.on_machine(entry, role, key):
        return False, "this machine is not in the row's Machines"
    declared = set(machine.hosts(key, machines_path))
    missing = sorted(supported_pair() - declared)
    if missing:
        return False, "this machine does not declare %s" % ", ".join(missing)
    return True, ""


# --- git, with a hard deadline ------------------------------------------------------------------

class _Clock(object):
    def __init__(self, budget: float):
        self.end = time.monotonic() + budget

    def remaining(self) -> float:
        return self.end - time.monotonic()


def _env() -> Dict[str, str]:
    env = dict(os.environ)
    for name in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                 "GIT_COMMITTER_EMAIL", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(name, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["LC_ALL"] = "C"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=20")
    return env


#: Applied to every call: no hooks, no signing, no tag following, whatever the user config says.
_SAFE = ("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false",
         "-c", "push.gpgSign=false", "-c", "push.followTags=false", "-c", "tag.gpgSign=false")


def _run(cwd: str, args: Sequence[str], clock: _Clock) -> Tuple[int, str, str]:
    """git in `cwd`. Killed (whole process group) at min(CALL_TIMEOUT, what the repo has left).

    `subprocess.run(timeout=)` kills only git itself and then waits on pipes a grandchild
    (ssh, a credential helper) still holds, so a hung transport would outlive the deadline.
    """
    budget = min(CALL_TIMEOUT, clock.remaining())
    if budget <= 0:
        return 124, "", "repo deadline (%ss) exhausted" % int(REPO_DEADLINE)
    command = ["git"] + list(_SAFE) + list(args)
    try:
        proc = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=_env(), start_new_session=True)
    except OSError as error:
        return 127, "", str(error)
    try:
        out, err = proc.communicate(timeout=budget)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.communicate()
        return 124, "", "timed out after %.0fs: git %s" % (budget, " ".join(args[:2]))
    return (proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))


def _classify_failure(err: str) -> str:
    lowered = err.lower()
    if any(text in lowered for text in _REFUSAL_TEXT):
        return REFUSED
    return TRANSPORT_UNKNOWN


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line and not line.lower().startswith("hint:"):
            return line[:200]
    return ""


class _Stop(Exception):
    def __init__(self, state: str, detail: str):
        Exception.__init__(self, detail)
        self.state = state
        self.detail = detail


# --- the bare cache -----------------------------------------------------------------------------

def cache_dir() -> str:
    return paths.state_file("mirrors")


def state_path() -> str:
    return paths.state_file("mirrors-state.json")


def _ensure_bare(path: str, clock: _Clock) -> None:
    if os.path.isfile(os.path.join(path, "HEAD")):
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    code, _, err = _run(os.path.dirname(path), ["init", "--bare", "-q", path], clock)
    if code != 0:
        raise _Stop(CONFIG_ERROR, "could not create the mirror cache %s: %s"
                    % (path, _first_line(err)))


def _check_host(bare: str, url: str, clock: _Clock) -> None:
    code, out, err = _run(bare, ["ls-remote", "--get-url", url], clock)
    if code != 0:
        raise _Stop(CONFIG_ERROR, "cannot resolve %s: %s" % (url, _first_line(err)))
    resolved = out.strip()
    want = skillclone.remote_host(url)
    got = _resolved_host(resolved)
    if got != want:
        raise _Stop(CONFIG_ERROR, "%s resolves (insteadOf) to host %s, not %s"
                    % (url, got or "(none)", want))


def _tip(bare: str, url: str, branch: str, side: str, clock: _Clock) -> Optional[str]:
    code, out, err = _run(bare, ["ls-remote", url, "refs/heads/" + branch], clock)
    if code != 0:
        raise _Stop(_classify_failure(err), "%s ls-remote: %s" % (side, _first_line(err)))
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "refs/heads/" + branch:
            return parts[0]
    return None


def _have(bare: str, oid: str, clock: _Clock) -> bool:
    code, _, _ = _run(bare, ["cat-file", "-e", oid + "^{commit}"], clock)
    return code == 0


def _fetch(bare: str, url: str, branch: str, side: str, oid: str, clock: _Clock) -> None:
    if _have(bare, oid, clock):
        _run(bare, ["update-ref", "refs/lodestar/%s/%s" % (side, branch), oid], clock)
        return
    refspec = "+refs/heads/%s:refs/lodestar/%s/%s" % (branch, side, branch)
    code, _, err = _run(bare, ["fetch", "-q", "--no-tags", "--no-recurse-submodules", url,
                               refspec], clock)
    if code != 0:
        raise _Stop(_classify_failure(err), "%s fetch: %s" % (side, _first_line(err)))
    if not _have(bare, oid, clock):
        # The branch moved between ls-remote and fetch: another pusher. Next run recomputes.
        raise _Stop(RACED, "%s moved while being fetched" % side)


def _is_ancestor(bare: str, older: str, newer: str, clock: _Clock) -> bool:
    code, _, _ = _run(bare, ["merge-base", "--is-ancestor", older, newer], clock)
    return code == 0


def _push(bare: str, url: str, oid: str, branch: str, side: str, clock: _Clock) -> None:
    """Push one explicit OID. Raises _Stop unless the porcelain line for the ref says it landed."""
    if _before_push is not None:
        _before_push(side)
    ref = "refs/heads/" + branch
    code, out, err = _run(bare, ["push", "--porcelain", "--no-follow-tags",
                                 "--no-recurse-submodules", "--no-verify", url,
                                 "%s:%s" % (oid, ref)], clock)
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or not parts[1].endswith(":" + ref):
            continue
        flag, summary = parts[0][:1], parts[2]
        if flag in (" ", "*", "="):
            return
        if flag == "!":
            if summary.startswith("[rejected]"):
                raise _Stop(RACED, "%s moved under the push (%s); next run recomputes"
                            % (side, summary))
            if summary.startswith("[remote failure]"):
                raise _Stop(TRANSPORT_UNKNOWN, "%s push: %s" % (side, summary))
            raise _Stop(REFUSED, "%s push refused: %s" % (side, summary))
        raise _Stop(REFUSED, "%s push: unexpected porcelain flag %r (%s)" % (side, flag, summary))
    if code == 124:
        raise _Stop(TRANSPORT_UNKNOWN, "%s push: %s" % (side, _first_line(err)))
    raise _Stop(_classify_failure(err) if code else TRANSPORT_UNKNOWN,
                "%s push: %s" % (side, _first_line(err) or "no porcelain status for %s" % ref))


# --- the gate (D6) ------------------------------------------------------------------------------

def _gate(bare: str, base: Optional[str], tip: str, clock: _Clock) -> Tuple[List[str], int]:
    """(marker labels found, colleague-authored commit count) for commits base..tip."""
    span = "%s..%s" % (base, tip) if base else tip
    code, log, err = _run(bare, ["log", "--format=%x00%ae%x01%B", span], clock)
    if code != 0:
        raise _Stop(CONFIG_ERROR, "gate could not read %s: %s" % (span, _first_line(err)))
    lines = []  # type: List[str]
    colleagues = 0
    for record in log.split("\x00")[1:]:
        email, _, body = record.partition("\x01")
        if email.strip().lower() not in own_emails():
            colleagues += 1
        lines.extend(body.splitlines())
    diff_args = ["diff", "--no-color", "--no-ext-diff", "--unified=0", base or EMPTY_TREE, tip]
    code, diff, err = _run(bare, diff_args, clock)
    if code != 0:
        raise _Stop(CONFIG_ERROR, "gate could not diff %s: %s" % (span, _first_line(err)))
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            lines.append(line[1:])
    return proseguard.markers(lines), colleagues


def _merge_identity(entry: registry.Entry) -> Tuple[str, str]:
    """The github.com identity, or the repo's public noreply when its checkout carries one."""
    try:
        result = subprocess.run(["git", "-C", entry.path, "config", "--get", "user.email"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
        email = result.stdout.decode("utf-8", "replace").strip()
    except (OSError, subprocess.SubprocessError):
        email = ""
    if email.lower().endswith("@users.noreply.github.com"):
        return GITHUB_NAME, email
    return GITHUB_NAME, github_email()


def _merge(bare: str, entry: registry.Entry, o_tip: str, m_tip: str, branch: str,
           clock: _Clock) -> Optional[str]:
    """Merge commit oid, or None on conflict. Done in a throwaway worktree of the bare repo."""
    work = tempfile.mkdtemp(prefix="lodestar-mirror-merge-")
    os.rmdir(work)
    name, email = _merge_identity(entry)
    try:
        code, _, err = _run(bare, ["worktree", "add", "-q", "--detach", work, o_tip], clock)
        if code != 0:
            raise _Stop(CONFIG_ERROR, "could not make a merge worktree: %s" % _first_line(err))
        code, _, err = _run(work, ["-c", "user.name=" + name, "-c", "user.email=" + email,
                                   "merge", "--no-ff", "--no-edit", "-q", "-m",
                                   "Merge %s %s into %s (Lodestar mirror)"
                                   % (skillclone.remote_host(entry.mirror), branch,
                                      skillclone.remote_host(entry.remote)),
                                   m_tip], clock)
        if code != 0:
            _run(work, ["merge", "--abort"], clock)
            if code == 124:
                raise _Stop(TRANSPORT_UNKNOWN, "merge timed out")
            return None
        code, out, _ = _run(work, ["rev-parse", "HEAD"], clock)
        return out.strip() if code == 0 else None
    finally:
        _run(bare, ["worktree", "remove", "--force", work], _Clock(CALL_TIMEOUT))
        shutil.rmtree(work, ignore_errors=True)
        _run(bare, ["worktree", "prune"], _Clock(CALL_TIMEOUT))


# --- one repo -----------------------------------------------------------------------------------

def replicate(entry: registry.Entry, dry_run: bool = False,
              cache_root: Optional[str] = None) -> Outcome:
    """Bring `entry`'s branch level on Remote and Mirror. Never raises on a git failure."""
    clock = _Clock(REPO_DEADLINE)
    base = Outcome(entry.repo_id, entry.name, entry.mirror, "", "")

    def done(state, detail, **extra):
        return base._replace(state=state, detail=detail, **extra)

    faults = row_faults(entry)
    if faults:
        return done(CONFIG_ERROR, "; ".join(sentence for _, sentence in faults))
    branch = entry.branch
    if not branch:
        return done(CONFIG_ERROR, "a mirrored row must declare its Branch")
    if not entry.repo_id:
        return done(CONFIG_ERROR, "a mirrored row must have an Id")

    root = cache_root or cache_dir()
    os.makedirs(root, exist_ok=True)
    bare = os.path.join(root, entry.repo_id + ".git")
    lock_handle = open(os.path.join(root, entry.repo_id + ".lock"), "a")
    try:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return done(RACED, "another mirror run holds this repo's cache; next run recomputes")
        return _replicate_locked(entry, bare, branch, dry_run, clock, done)
    finally:
        lock_handle.close()


def _replicate_locked(entry, bare, branch, dry_run, clock, done) -> Outcome:
    origin, mirror = entry.remote, entry.mirror
    o_tip = m_tip = ""
    to_github = "origin" if skillclone.remote_host(origin) == GITHUB else "mirror"
    try:
        _ensure_bare(bare, clock)
        _check_host(bare, origin, clock)
        _check_host(bare, mirror, clock)
        o_tip = _tip(bare, origin, branch, "origin", clock) or ""
        m_tip = _tip(bare, mirror, branch, "mirror", clock) or ""

        if not o_tip:
            if m_tip:
                raise _Stop(CONFIG_ERROR, "branch %s is missing on origin but present on the "
                            "mirror; the origin side is never auto-created" % branch)
            raise _Stop(CONFIG_ERROR, "branch %s exists on neither side" % branch)

        _fetch(bare, origin, branch, "origin", o_tip, clock)
        if not m_tip:
            if to_github == "mirror":
                _hold_if_marked(bare, None, o_tip, clock)
            if dry_run:
                return done(DRY_RUN, "would create %s on the mirror" % branch,
                            direction=MIRROR_CREATED, origin_tip=o_tip)
            _push(bare, mirror, o_tip, branch, "mirror", clock)
            return done(PUSHED, "created %s on the mirror at %s" % (branch, o_tip[:8]),
                        direction=MIRROR_CREATED, origin_tip=o_tip, mirror_tip=o_tip)
        _fetch(bare, mirror, branch, "mirror", m_tip, clock)

        if o_tip == m_tip:
            return done(LEVEL, "level at %s" % o_tip[:8], origin_tip=o_tip, mirror_tip=m_tip)

        if _is_ancestor(bare, m_tip, o_tip, clock):
            colleagues = 0
            if to_github == "mirror":
                colleagues = _hold_if_marked(bare, m_tip, o_tip, clock)
            if dry_run:
                return done(DRY_RUN, "would push origin %s to the mirror" % o_tip[:8],
                            direction=MIRROR_BEHIND, origin_tip=o_tip, mirror_tip=m_tip)
            _push(bare, mirror, o_tip, branch, "mirror", clock)
            return done(PUSHED, "mirror %s -> %s" % (m_tip[:8], o_tip[:8]),
                        direction=MIRROR_BEHIND, origin_tip=o_tip, mirror_tip=o_tip,
                        colleague_commits=colleagues)

        if _is_ancestor(bare, o_tip, m_tip, clock):
            colleagues = 0
            if to_github == "origin":
                colleagues = _hold_if_marked(bare, o_tip, m_tip, clock)
            if dry_run:
                return done(DRY_RUN, "would push mirror %s to origin" % m_tip[:8],
                            direction=ORIGIN_BEHIND, origin_tip=o_tip, mirror_tip=m_tip)
            _push(bare, origin, m_tip, branch, "origin", clock)
            return done(PUSHED, "origin %s -> %s%s" % (o_tip[:8], m_tip[:8],
                                                     _colleague_note(colleagues)),
                        direction=ORIGIN_BEHIND, origin_tip=m_tip, mirror_tip=m_tip,
                        colleague_commits=colleagues)

        # Diverged.
        code, out, err = _run(bare, ["merge-base", o_tip, m_tip], clock)
        if code != 0:
            raise _Stop(CONFIG_ERROR, "origin and mirror share no history: %s"
                        % (_first_line(err) or "no merge base"))
        merge_base = out.strip()
        incoming = (merge_base, m_tip) if to_github == "origin" else (merge_base, o_tip)
        colleagues = _hold_if_marked(bare, incoming[0], incoming[1], clock)
        merged = _merge(bare, entry, o_tip, m_tip, branch, clock)
        if merged is None:
            raise _Stop(CONFLICT, "mirror and origin conflict on %s (origin %s, mirror %s) "
                        "— needs you" % (branch, o_tip[:8], m_tip[:8]))
        if dry_run:
            return done(DRY_RUN, "would merge origin %s and mirror %s" % (o_tip[:8], m_tip[:8]),
                        direction=MERGED, origin_tip=o_tip, mirror_tip=m_tip)
        _push(bare, origin, merged, branch, "origin", clock)
        _push(bare, mirror, merged, branch, "mirror", clock)
        return done(PUSHED, "merged origin %s + mirror %s -> %s%s"
                    % (o_tip[:8], m_tip[:8], merged[:8], _colleague_note(colleagues)),
                    direction=MERGED, origin_tip=merged, mirror_tip=merged,
                    colleague_commits=colleagues)
    except _Stop as stop:
        return done(stop.state, stop.detail, origin_tip=o_tip, mirror_tip=m_tip)


def _colleague_note(count: int) -> str:
    return "; %d colleague-authored commit(s) crossed" % count if count else ""


def _hold_if_marked(bare: str, base: Optional[str], tip: str, clock: _Clock) -> int:
    found, colleagues = _gate(bare, base, tip, clock)
    if found:
        raise _Stop(HELD, "private commits held — needs you: %s in commits bound for github.com"
                    % ", ".join(found))
    return colleagues


# --- the fleet: eligibility, surfacing, memory --------------------------------------------------

def _load_state(path: str) -> Dict[str, Dict[str, object]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(path: str, data: Dict[str, Dict[str, object]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


def memory_key(entry: registry.Entry) -> str:
    """confirmed_at is per (repo id, mirror URL): a changed URL starts fresh."""
    return "%s|%s" % (entry.repo_id, skillclone.normalize_remote(entry.mirror))


def _iso(stamp: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(stamp))


def _surface(outcome: Outcome, memory: Dict[str, object], now: float) -> Outcome:
    state = outcome.state
    if state in (LEVEL, PUSHED):
        memory["confirmed_at"] = now
        memory.pop("first_unverified", None)
        return outcome._replace(surface=SILENT, confirmed_at=_iso(now))
    confirmed = memory.get("confirmed_at")
    confirmed_iso = _iso(float(confirmed)) if isinstance(confirmed, (int, float)) else None
    outcome = outcome._replace(confirmed_at=confirmed_iso)
    if state in (INELIGIBLE, RACED, DRY_RUN):
        return outcome._replace(surface=SILENT)
    if state == TRANSPORT_UNKNOWN:
        since = memory.setdefault("first_unverified", now)
        anchor = max(float(confirmed) if isinstance(confirmed, (int, float)) else 0.0,
                     float(since))  # type: ignore[arg-type]
        hours = (now - anchor) / 3600.0
        if now - anchor >= UNVERIFIED_AMBER:
            return outcome._replace(surface=AMBER,
                                    detail="mirror unverified %d h — %s" % (hours, outcome.detail))
        return outcome._replace(surface=NOTE)
    if state == REFUSED:
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        if memory.get("refused_flagged_on") == today:
            return outcome._replace(surface=NOTE)
        memory["refused_flagged_on"] = today
        return outcome._replace(surface=FLAGGED)
    return outcome._replace(surface=FLAGGED)  # conflict, held, configuration_error


def run(entries: Sequence[registry.Entry], role: Optional[str], key: str,
        dry_run: bool = False, machines_path: Optional[str] = None,
        only: Optional[str] = None, now: Optional[float] = None,
        cache_root: Optional[str] = None,
        memory_path: Optional[str] = None) -> List[Outcome]:
    """Every row with a Mirror, in registry order. A dry run records nothing."""
    now = time.time() if now is None else now
    memory_path = memory_path or state_path()
    memory = _load_state(memory_path)
    results = []
    for entry in entries:
        if not entry.mirror or (only and only not in (entry.repo_id, entry.name)):
            continue
        ok, why = eligible(entry, role, key, machines_path)
        if not ok:
            results.append(Outcome(entry.repo_id, entry.name, entry.mirror, INELIGIBLE, why))
            continue
        try:
            outcome = replicate(entry, dry_run=dry_run, cache_root=cache_root)
        except Exception as error:  # a mirror bug must never take the sweep down
            outcome = Outcome(entry.repo_id, entry.name, entry.mirror, CONFIG_ERROR,
                              "mirror step raised: %s" % error)
        slot = memory.setdefault(memory_key(entry), {})
        results.append(_surface(outcome, slot, now))
    if not dry_run:
        _save_state(memory_path, memory)
    return results


def summary_line(outcomes: Sequence[Outcome]) -> str:
    """The one actions line: counts by state, then any non-silent repo by name."""
    if not outcomes:
        return "mirrors: none declared"
    counts = {}  # type: Dict[str, int]
    for outcome in outcomes:
        counts[outcome.label] = counts.get(outcome.label, 0) + 1
    parts = ["%d %s" % (counts[label], label) for label in sorted(counts)]
    loud = ["%s %s" % (o.repo_id or o.name, o.state) for o in outcomes if o.surface != SILENT]
    return "mirrors: %s%s" % (", ".join(parts), (" — " + "; ".join(loud)) if loud else "")


def status_payload(outcomes: Sequence[Outcome]) -> Dict[str, object]:
    return {
        "actions": summary_line(outcomes),
        "repos": {
            (o.repo_id or o.name): {
                "name": o.name, "mirror": o.mirror, "state": o.label, "detail": o.detail,
                "surface": o.surface, "origin_tip": o.origin_tip, "mirror_tip": o.mirror_tip,
                "colleague_commits": o.colleague_commits, "confirmed_at": o.confirmed_at,
            }
            for o in outcomes
        },
    }


def flagged(outcomes: Sequence[Outcome]) -> List[Outcome]:
    return [o for o in outcomes if o.surface in (FLAGGED, AMBER)]
