"""GitHub Issues pilot (2026-10-23): one read model and one write path for agent coordination.

Design and evidence: docs/github-issues-pilot/{RESULTS,AGENT_PROTOCOL}.md. Used by
bin/issues_state.py (STATE issue + offline export), bin/issues_work.py (ready / claim / progress /
close / verify) and bin/tickets_to_issues.py (one-way mirror of markdown tickets).

Authority rules this module encodes:

* **Comments are the authority for claims, labels are only an index.** A claim is a comment carrying
  a `<!-- lodestar:claim ... -->` marker. Its start time is the comment's SERVER `createdAt` (no client
  clock in the race), it lasts `ttl` from the claimer's latest claim/progress comment, and among
  overlapping live claims the EARLIEST comment wins. The `claimed` label is advisory and may be stale.
* **Readiness is read from the issue, never from search.** Search drops `is:blocked` silently unless
  `advanced_search=true` and lags writes by ~20 s (RESULTS.md, Phase 1a). `blockedBy` nodes are read
  directly.
* **Done is proven.** A COMPLETED closure is SATISFIED only by evidence: the closing commit/PR GitHub
  recorded, or an evidence comment (commit/pr checked against the API, path checked on disk, an owner
  quote recorded as such). No evidence is NOT_SATISFIED ("UNVERIFIED"); a verifier that could not tell
  is UNKNOWN, which never counts as proven (lib/outcome.py vocabulary).
* Every gh failure is returned, never raised past the caller as success: reads raise GhError, and the
  CLIs fall back to the git-tracked export with its age printed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from lib.outcome import NOT_SATISFIED, SATISFIED, UNKNOWN
except Exception:  # noqa: BLE001  (run from a test without bin/ on the path)
    SATISFIED, NOT_SATISFIED, UNKNOWN = "SATISFIED", "NOT_SATISFIED", "UNKNOWN"

DEV = os.path.expanduser("~/dev")

def _private_host() -> str:
    """The deployment's private git host (`lib.boundary`), for a project declared `"private"`."""
    try:
        from lib import boundary
    except ImportError:  # pragma: no cover
        import boundary  # type: ignore
    return boundary.host() or "github.com"


#: Beside the code's own checkout: this deployment's further pilot projects (never exported).
LOCAL_PROJECTS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "sync", "issues-projects.json")


def _local_projects(path: str = LOCAL_PROJECTS) -> Dict[str, Dict[str, object]]:
    """Projects declared in `sync/issues-projects.json`: `{name: {host, repo, board, export,
    tickets, code, aliases?, canonical?, since?, status_map}}`. `host: "private"` resolves to
    the private host; `export`/`tickets` are `~`-expanded. No file: no extra projects."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    out: Dict[str, Dict[str, object]] = {}
    for name, cfg in data.items():
        if name.startswith("_") or not isinstance(cfg, dict):
            continue
        cfg = dict(cfg)
        if cfg.get("host") == "private":
            cfg["host"] = _private_host()
        for k in ("export", "tickets"):
            if k in cfg:
                cfg[k] = os.path.expanduser(str(cfg[k]))
        out[name.lower()] = cfg
    return out


def _board_repo(default: str = "Lmandrake/Lodestar") -> str:
    """owner/repo of this board's own GitHub repo, from the registry's declared `Remote` for id
    `lodestar`, so a rehome (e.g. to `Lodestar-dev`) carries the issues pilot with it instead of
    leaving it aimed at a name another repository may now hold. No registry row: `default`."""
    try:
        from lib import registry
    except ImportError:  # pragma: no cover
        import registry  # type: ignore
    try:
        rows = [e for e in registry.entries() if e.repo_id == "lodestar" and e.remote]
    except Exception:  # an unreadable registry must not break import; the default is the export's own name
        rows = []
    if rows:
        m = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$", rows[0].remote)
        if m:
            return m.group(1)
    return default


#: The pilot's projects. Board ids are looked up by number at run time.
PROJECTS: Dict[str, Dict[str, object]] = {
    "lodestar": {"host": "github.com", "repo": _board_repo(), "board": 3,
                 "export": os.path.join(DEV, "Lodestar", "docs", "github-issues-pilot", "export"),
                 "tickets": os.path.join(DEV, "Lodestar", "tickets"), "code": "LODE",
                 # board Status option for each derived state
                 "status_map": {"ready": "Ready", "claimed": "Claimed", "blocked": "Blocked",
                                "review": "Review", "done": "Done"}},
}
PROJECTS.update(_local_projects())

KINDS = ("code", "bug", "experiment-run", "knowledge-product", "deliverable", "presentation",
         "decision", "epic")
PILOT = "pilot"
CLAIMED = "claimed"
STATE_LABEL = "state"
DEFAULT_TTL = "4h"
MAX_TTL_S = 24 * 3600
MARKER_RE = re.compile(r"<!--\s*lodestar:([a-z-]+)((?:\s+[a-z_]+=[^\s>]*)*)\s*-->")
KV_RE = re.compile(r"([a-z_]+)=([^\s>]*)")
EVIDENCE_KINDS = ("commit", "pr", "path", "url", "said", "issue")
_TTL_RE = re.compile(r"^(\d+)([mhd])$")


class GhError(Exception):
    """gh could not answer (network, auth, HTTP, GraphQL error). Never a 'no'."""


# --------------------------------------------------------------------------- gh transport

def project(name: str) -> Dict[str, object]:
    if "/" in name:     # ad-hoc repo: owner/repo (github.com) or host/owner/repo; no export, no board
        parts = name.strip("/").split("/")
        if len(parts) not in (2, 3):
            raise SystemExit("ad-hoc project must be owner/repo or host/owner/repo, not %r" % name)
        host = parts[0] if len(parts) == 3 else "github.com"
        return {"name": name, "host": host, "repo": "/".join(parts[-2:]), "status_map": {}}
    key = name.lower()
    for pname, pcfg in PROJECTS.items():
        if key == pname or key == str(pcfg.get("code", "")).lower() or key in pcfg.get("aliases", ()):
            key = pname
            break
    if key not in PROJECTS:
        raise SystemExit("unknown project %r (pilot projects: %s)" % (name, ", ".join(PROJECTS)))
    cfg = dict(PROJECTS[key])
    cfg["name"] = key
    return cfg


def canonical_issues_project(tickets_dir: str) -> Optional[str]:
    """The project name whose markdown tickets in `tickets_dir` are an export of its issues, or None."""
    d = os.path.realpath(tickets_dir)
    for name, cfg in PROJECTS.items():
        if cfg.get("canonical") == "issues" and os.path.realpath(str(cfg["tickets"])) == d:
            return name
    return None


def _is_mutation(args: Sequence[str], stdin: Optional[str]) -> bool:
    a = list(args)
    if a[:1] == ["api"]:
        if "-X" in a and a[a.index("-X") + 1].upper() != "GET":
            return True
        if "graphql" in a and stdin and re.search(r'"query":\s*"\s*mutation', stdin):
            return True
        return False
    return a[:2] not in (["issue", "list"], ["issue", "view"], ["label", "list"])


def gh(host: str, args: Sequence[str], stdin: Optional[str] = None, timeout: int = 60,
       tries: int = 3) -> str:
    """Run `gh` against one host; return stdout or raise GhError.

    Reads retry on transport errors. A MUTATION is never retried automatically: a timeout may
    have committed it (consult finding 17: a retried create made a duplicate), so the caller
    re-reads and decides. A secondary rate limit waits >= 60 s (GitHub's documented minimum)."""
    if _is_mutation(args, stdin):
        tries = 1
    env = dict(os.environ, GH_HOST=host, GH_PROMPT_DISABLED="1", NO_COLOR="1")
    last = ""
    for attempt in range(tries):
        try:
            p = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True,
                               timeout=timeout, env=env)
        except (OSError, subprocess.TimeoutExpired) as e:
            last = str(e)
        else:
            if p.returncode == 0:
                return p.stdout
            last = (p.stderr or p.stdout).strip()[-500:]
            if not re.search(r"timeout|connection|EOF|502|503|504|secondary rate", last, re.I):
                break
        if attempt + 1 < tries:
            time.sleep(60 if re.search(r"secondary rate|abuse", last, re.I) else 2 * (attempt + 1))
    raise GhError("gh %s on %s: %s" % (" ".join(args[:2]), host, last))


def graphql(host: str, query: str, variables: Optional[Dict] = None) -> Dict:
    out = gh(host, ["api", "graphql", "--input", "-"],
             stdin=json.dumps({"query": query, "variables": variables or {}}))
    try:
        data = json.loads(out)
    except ValueError as e:
        raise GhError("unparseable GraphQL answer: %s" % e)
    if data.get("errors"):
        raise GhError("GraphQL: %s" % "; ".join(e.get("message", "?") for e in data["errors"]))
    return data["data"]


def rest(host: str, method: str, path: str, fields: Optional[Dict[str, object]] = None,
         typed: Optional[Dict[str, object]] = None) -> object:
    args = ["api", "-X", method, path]
    for k, v in (fields or {}).items():
        args += ["-f", "%s=%s" % (k, v)]
    for k, v in (typed or {}).items():
        args += ["-F", "%s=%s" % (k, v)]
    out = gh(host, args)
    return json.loads(out) if out.strip() else None


def rest_status(host: str, path: str) -> Tuple[Optional[int], object]:
    """GET returning (HTTP status, body); status None when gh gave no HTTP answer."""
    env = dict(os.environ, GH_HOST=host, GH_PROMPT_DISABLED="1")
    try:
        p = subprocess.run(["gh", "api", "-i", path], capture_output=True, text=True, timeout=30,
                           env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    m = re.match(r"HTTP/[\d.]+ (\d{3})", p.stdout)
    if not m:
        return None, None
    head, _, body = p.stdout.partition("\r\n\r\n")
    if not body:
        head, _, body = p.stdout.partition("\n\n")
    try:
        return int(m.group(1)), json.loads(body) if body.strip() else None
    except ValueError:
        return int(m.group(1)), None


# --------------------------------------------------------------------------- read model

_ISSUE_FIELDS = """
        number id databaseId title body state stateReason createdAt updatedAt closedAt url isPinned
        labels(first:40){nodes{name}}
        parent{number}
        subIssues(first:50){nodes{number state}}
        blockedBy(first:50){totalCount nodes{number state stateReason repository{nameWithOwner}}}
        blocking(first:30){nodes{number state}}
        comments(last:100){totalCount nodes{databaseId createdAt lastEditedAt body}}
        timelineItems(last:10,itemTypes:[CLOSED_EVENT,REOPENED_EVENT]){nodes{__typename
          ... on ReopenedEvent{createdAt}
          ... on ClosedEvent{createdAt stateReason
            closer{__typename ... on Commit{oid} ... on PullRequest{number merged}}}}}
"""

ISSUES_Q = """
query($o:String!,$n:String!,$after:String){
  rateLimit{cost remaining}
  repository(owner:$o,name:$n){
    issues(first:50, after:$after, orderBy:{field:CREATED_AT,direction:ASC}){
      pageInfo{hasNextPage endCursor}
      nodes{""" + _ISSUE_FIELDS + """}}}}
"""


def fetch(cfg: Dict[str, object], cost: Optional[List[int]] = None) -> List[Dict]:
    """Every issue of the project's repo, normalized. Raises GhError."""
    owner, name = str(cfg["repo"]).split("/")
    after, out = None, []
    while True:
        d = graphql(str(cfg["host"]), ISSUES_Q, {"o": owner, "n": name, "after": after})
        if cost is not None:
            cost.append(int(d["rateLimit"]["cost"]))
        conn = d["repository"]["issues"]
        out.extend(normalize(n) for n in conn["nodes"])
        if not conn["pageInfo"]["hasNextPage"]:
            return out
        after = conn["pageInfo"]["endCursor"]


def normalize(n: Dict) -> Dict:
    tl = [c for c in (n.get("timelineItems") or {}).get("nodes", []) if c]
    closed = [c for c in tl if c.get("__typename", "ClosedEvent") == "ClosedEvent"]
    reopened = [c["createdAt"] for c in tl if c.get("__typename") == "ReopenedEvent"]
    closer = (closed[-1].get("closer") if closed else None) or None
    if reopened and closed and closed[-1]["createdAt"] < reopened[-1]:
        closer = None
    comments = (n.get("comments") or {})
    return {
        "number": n["number"], "id": n["id"], "dbid": n["databaseId"], "title": n["title"],
        "body": n.get("body") or "", "state": n["state"], "state_reason": n.get("stateReason"),
        "created": n["createdAt"], "updated": n["updatedAt"], "closed": n.get("closedAt"),
        "url": n["url"], "pinned": bool(n.get("isPinned")),
        "reopened": max(reopened) if reopened else None,
        "labels": sorted(x["name"] for x in n["labels"]["nodes"]),
        "parent": (n.get("parent") or {}).get("number"),
        "sub_issues": [[s["number"], s["state"]] for s in n["subIssues"]["nodes"]],
        # [number, state, repo, stateReason]; an entry without the 4th element (an older export)
        # has an unread close reason and makes the issue `incomplete` if that blocker is closed
        "blocked_by": [[b["number"], b["state"], b["repository"]["nameWithOwner"], b.get("stateReason")]
                       for b in n["blockedBy"]["nodes"]],
        "blocking": [[b["number"], b["state"]] for b in n["blocking"]["nodes"]],
        "comment_count": comments.get("totalCount", 0),
        "comments_complete": comments.get("totalCount", 0) <= len(comments.get("nodes", [])),
        "blockers_complete": (n["blockedBy"].get("totalCount") is not None
                              and n["blockedBy"]["totalCount"] <= len(n["blockedBy"]["nodes"])),
        "comments": [{"id": c["databaseId"], "at": c["createdAt"], "body": c["body"],
                      "edited": c.get("lastEditedAt")} for c in comments.get("nodes", [])],
        "closer": ({"type": closer.get("__typename"), "oid": closer.get("oid"),
                    "number": closer.get("number"), "merged": closer.get("merged")}
                   if closer else None),
    }


# --------------------------------------------------------------------------- markers

def markers(text: str) -> List[Tuple[str, Dict[str, str]]]:
    return [(m.group(1), dict(KV_RE.findall(m.group(2)))) for m in MARKER_RE.finditer(text or "")]


def record(c: Dict) -> Optional[Tuple[str, Dict[str, str]]]:
    """The one authoritative marker of a comment: it must open the comment, and the comment must
    never have been edited (consult finding 5: an edited old comment could forge an early claim;
    a marker quoted mid-text is not a record)."""
    if c.get("edited"):
        return None
    m = MARKER_RE.match((c.get("body") or "").lstrip())
    return (m.group(1), dict(KV_RE.findall(m.group(2)))) if m else None


def marker(_marker_kind: str, **kv: object) -> str:
    parts = " ".join("%s=%s" % (k, quote(str(v))) for k, v in kv.items() if v not in (None, ""))
    return "<!-- lodestar:%s %s -->" % (_marker_kind, parts)


def parse_ttl(ttl: str) -> int:
    m = _TTL_RE.match(ttl or "")
    if not m:
        raise ValueError("ttl must look like 30m, 4h or 1d, not %r" % ttl)
    s = int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)]
    if s <= 0 or s > MAX_TTL_S:
        raise ValueError("ttl must be between 1m and 24h")
    return s


def ts(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def session_id() -> str:
    """This agent's identity in claims: <machine>/<first 8 of CLAUDE_CODE_SESSION_ID or pid>."""
    try:
        from lib import machine
        m = machine.key()
    except Exception:  # noqa: BLE001
        m = os.uname().nodename.split(".")[0].lower()
    sid = os.environ.get("LODESTAR_SESSION") or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    return "%s/%s" % (m, (sid or "pid%d" % os.getpid())[:8])


# --------------------------------------------------------------------------- claims

@dataclass
class Claim:
    session: str
    started: datetime        # server time of the claim comment
    expires: datetime        # last renewal by the holder + that renewal's ttl
    comment_id: int
    ttl_s: int


#: A claim is treated as live this long past its computed expiry, so a reader whose clock runs
#: fast cannot take over a lease the holder still believes it has (consult finding 6).
SKEW_S = 120
#: Version of the replay rules a claim was written under; stamped into every new claim.
CLAIM_RULES = "2"


def claims(issue: Dict, now: Optional[datetime] = None) -> Tuple[List[Claim], List[Claim]]:
    """([the holder] or [], expired grants) from a replay of the claim records.

    Lease semantics (consult findings 2-4, 6, 10): a claim comment is a GRANT only if no live
    grant exists when it is posted (server createdAt order, id tiebreak); a claim posted while
    another session holds a live grant is a failed attempt and never becomes the holder later.
    The holder renews with progress or a re-claim, each applying ITS ttl. A release ends the
    grant only if it names that grant's comment id (`claim=<id>`), or, without one, if it is the
    holder's own release. Evidence does not release (close does, after closing). Expiry carries
    SKEW_S of grace against clock skew.
    """
    now = now or now_utc()
    grant: Optional[Claim] = None
    ended: List[Claim] = []
    for c in sorted(issue.get("comments", []), key=lambda c: (c["at"], c["id"])):
        r = record(c)
        if not r:
            continue
        kind, kv = r
        at = ts(c["at"])
        s = kv.get("session")
        if grant and grant.expires + timedelta(seconds=SKEW_S) <= at:
            ended.append(grant)
            grant = None
        if kind == "claim" and s:
            try:
                ttl = parse_ttl(kv.get("ttl", DEFAULT_TTL))
            except ValueError:
                ttl = parse_ttl(DEFAULT_TTL)
            if grant is None:
                grant = Claim(s, at, at + timedelta(seconds=ttl), c["id"], ttl)
            elif kv.get("v") != CLAIM_RULES and grant.session != s:
                # A claim written under older rules (no v=2) that took over a lease those rules
                # saw as expired: honour it rather than silently strip it (trial r2.1).
                if at >= grant.expires:
                    ended.append(grant)
                    grant = Claim(s, at, at + timedelta(seconds=ttl), c["id"], ttl)
            elif grant.session == s:
                grant.ttl_s = ttl
                grant.expires = at + timedelta(seconds=ttl)
            # else: a failed attempt; ignored for ever
        elif kind == "progress" and grant and s == grant.session:
            grant.expires = at + timedelta(seconds=grant.ttl_s)
        elif kind == "release" and grant:
            target = kv.get("claim")
            if (target and target == str(grant.comment_id)) or (not target and s == grant.session
                                                                 and not kv.get("by")):
                ended.append(grant)
                grant = None
    if grant and grant.expires + timedelta(seconds=SKEW_S) <= now:
        ended.append(grant)
        grant = None
    return ([grant] if grant else []), ended


def holder(issue: Dict, now: Optional[datetime] = None) -> Optional[Claim]:
    live, _ = claims(issue, now)
    return live[0] if live else None


_PROG_LINE = re.compile(r"(?m)^- (Changed|Evidence|Next): (.*)$")


def last_progress(issue: Dict) -> Optional[Dict[str, str]]:
    """The newest progress (or note) record: {at, session, changed, evidence, next}, or None.
    STATE prints it under a claim so a fresh agent can take over (trial step 9)."""
    best = None
    for c in sorted(issue.get("comments", []), key=lambda c: (c["at"], c["id"])):
        r = record(c)
        if r and r[0] in ("progress", "note") and _PROG_LINE.search(c.get("body") or ""):
            best = (c, r[1])
    if not best:
        return None
    c, kv = best
    f = {k.lower(): v.strip().strip("`") for k, v in _PROG_LINE.findall(c["body"])}
    return {"at": c["at"], "session": kv.get("session", "?"), "changed": f.get("changed", ""),
            "evidence": f.get("evidence", ""), "next": f.get("next", "")}


# --------------------------------------------------------------------------- derived state

def kind(issue: Dict) -> Optional[str]:
    ks = [lab[5:] for lab in issue["labels"] if lab.startswith("kind:")]
    return ks[0] if ks else None


def open_blockers(issue: Dict) -> List[int]:
    return [b[0] for b in issue["blocked_by"] if b[1] == "OPEN"]


def blocker_names(issue: Dict, repo: str) -> List[str]:
    return [("#%d" % b[0]) if len(b) < 3 or b[2] == repo else "%s#%d" % (b[2], b[0])
            for b in issue["blocked_by"] if b[1] == "OPEN"]


#: The only blocker close reason that releases a dependent. NOT_PLANNED, DUPLICATE (superseded)
#: or no recorded reason never release one on their own: the dependent is named for a human
#: decision (`issues_work.py unblock ... --said`). A blocker closing is evidence, not proof.
RELEASING_REASON = "COMPLETED"


def dropped_blockers(issue: Dict) -> List[List]:
    """Blocked-by entries closed for a reason other than COMPLETED (closure reason known)."""
    return [b for b in issue["blocked_by"]
            if b[1] == "CLOSED" and len(b) > 3 and b[3] != RELEASING_REASON]


def blockers_read(issue: Dict) -> bool:
    """Every blocker was returned AND every closed one carries its close reason. Fails closed: a
    dict that does not say it is complete is not (F13: >30 blockers once failed open)."""
    return (bool(issue.get("blockers_complete", False))
            and not any(b[1] == "CLOSED" and len(b) < 4 for b in issue["blocked_by"]))


def is_state_issue(issue: Dict) -> bool:
    return STATE_LABEL in issue["labels"] and any(k == "state" for k, _ in markers(issue["body"]))


def derive(issue: Dict, now: Optional[datetime] = None) -> str:
    """ready | claimed | blocked | blocker-dropped | active | review | incomplete | done |
    closed-other | epic | state.

    `incomplete`: the read did not return every comment or blocker (or a closed blocker's close
    reason), so neither a claim nor a blocker can be ruled out; never ready (consult findings 1,
    13). `blocker-dropped`: a blocker closed not-planned/duplicate; never ready until a human
    removes the edge."""
    if is_state_issue(issue):
        return "state"
    if issue["state"] == "CLOSED":
        return "done" if issue["state_reason"] == "COMPLETED" else "closed-other"
    if kind(issue) == "epic":
        return "epic"
    if not issue.get("comments_complete", False) or not blockers_read(issue):
        return "incomplete"
    if open_blockers(issue) or "status:blocked" in issue["labels"]:
        return "blocked"
    if dropped_blockers(issue):
        return "blocker-dropped"
    if holder(issue, now):
        return "claimed"
    if "status:active" in issue["labels"]:
        return "active"
    if "status:review" in issue["labels"]:
        return "review"
    return "ready"


def ready(issues: Iterable[Dict], now: Optional[datetime] = None,
          include_unlabelled: bool = True) -> List[Dict]:
    """Open, not an epic or STATE, no open blocker, no live claim, not waiting on the owner.

    Order: `next` label first, then issues under an epic, then by number."""
    out = [i for i in issues if derive(i, now) == "ready"
           and "needs-owner" not in i["labels"]
           and (include_unlabelled or kind(i))]
    return sorted(out, key=lambda i: ("next" not in i["labels"], i["parent"] is None, i["number"]))


# --------------------------------------------------------------------------- dependency edges

_URL_REF = re.compile(r"^(?:https?://)?([a-z0-9-]+(?:\.[a-z0-9-]+)+)/([\w.-]+/[\w.-]+)(?:/issues/|#)(\d+)/?$",
                      re.I)
_REPO_REF = re.compile(r"^([\w.-]+/[\w.-]+)#(\d+)$")
_PROJ_REF = re.compile(r"^([a-z]+)#(\d+)$", re.I)


def parse_ref(text: object, cfg: Dict[str, object]) -> Tuple[str, str, int]:
    """(host, owner/repo, number) from N, #N, project#N, owner/repo#N, host/owner/repo#N or an
    issue URL. owner/repo of a pilot project takes that project's host; other repos the cfg host."""
    t = str(text).strip()
    m = re.match(r"^#?(\d+)$", t)
    if m:
        return str(cfg["host"]).lower(), str(cfg["repo"]), int(m.group(1))
    m = _URL_REF.match(t)
    if m:
        return m.group(1).lower(), m.group(2), int(m.group(3))
    m = _REPO_REF.match(t)
    if m:
        host = next((str(p["host"]) for p in PROJECTS.values()
                     if str(p["repo"]).lower() == m.group(1).lower()), str(cfg["host"]))
        return host.lower(), m.group(1), int(m.group(2))
    m = _PROJ_REF.match(t)
    if m:
        p = project(m.group(1))
        return str(p["host"]).lower(), str(p["repo"]), int(m.group(2))
    raise ValueError("not an issue reference: %r (use N, #N, project#N, owner/repo#N or a URL)" % t)


def ref_name(ref: Tuple[str, str, int], cfg: Dict[str, object]) -> str:
    host, repo, num = ref
    if host == str(cfg["host"]).lower() and repo.lower() == str(cfg["repo"]).lower():
        return "#%d" % num
    return "%s#%d" % (repo, num) if host == str(cfg["host"]).lower() else "%s/%s#%d" % (host, repo, num)


def edge_key(repo: str, num: int) -> str:
    return "%s#%d" % (repo.lower(), num)


#: Most nodes the cycle walk reads before it gives up (and refuses: an unfinished walk never passes).
WALK_LIMIT = 300


def would_cycle(dependent: Tuple[str, int], blocker: Tuple[str, int], read,
                limit: int = WALK_LIMIT) -> Tuple[str, str]:
    """Would `dependent` blocked-by `blocker` close a cycle? ("cycle"|"ok"|"unknown", why).

    Walks blocked-by edges (every state: a closed edge is still an edge) from `blocker`; a cycle
    exists iff `dependent` is reachable. `read((repo, num))` returns (entries, complete) with
    entries as normalize()'s blocked_by, or raises (GhError, or anything: unknown). Any node read
    incompletely, any failed read, or more than `limit` nodes makes the answer "unknown": the
    caller refuses, never passes."""
    dep = (dependent[0].lower(), dependent[1])
    start = (blocker[0].lower(), blocker[1])
    if start == dep:
        return "cycle", "self-edge"
    prev: Dict[Tuple[str, int], Optional[Tuple[str, int]]] = {start: None}
    queue = [start]
    while queue:
        node = queue.pop(0)
        if len(prev) > limit:
            return "unknown", "walk exceeded %d issues" % limit
        try:
            got = read(node)
        except Exception as e:  # noqa: BLE001  (any failure is unknown, never ok)
            return "unknown", "could not read %s#%d: %s" % (node[0], node[1], str(e)[:120])
        if got is None:
            return "unknown", "%s#%d could not be read" % node
        entries, complete = got
        if not complete:
            return "unknown", "%s#%d has more blockers than one read returns" % node
        for b in entries:
            nxt = (str(b[2]).lower(), int(b[0]))
            if nxt in prev:
                continue
            prev[nxt] = node
            if nxt == dep:
                path, cur = [], nxt
                while cur is not None:
                    path.append("%s#%d" % cur)
                    cur = prev[cur]
                return "cycle", " is blocked by ".join(path[::-1])
            queue.append(nxt)
    return "ok", "%d issue(s) walked" % len(prev)


_INFERRED_RE = re.compile(r"_Pilot-inferred relationship[^\n]*")
_BODY_CONFIRMED_RE = re.compile(r"_Blocked-by relationship confirmed by the owner[^\n]*")


def edge_records(issue: Dict) -> Dict[str, Tuple[Dict[str, str], Dict]]:
    """Newest `<!-- lodestar:edge op=block|unblock|confirm on=<repo#N> ... -->` record per blocker
    key, from unedited comments that open with the marker."""
    out: Dict[str, Tuple[Dict[str, str], Dict]] = {}
    for c in sorted(issue.get("comments", []), key=lambda c: (c["at"], c["id"])):
        r = record(c)
        if r and r[0] == "edge" and r[1].get("on") and r[1].get("op"):
            out[unquote(r[1]["on"]).lower()] = (r[1], c)
    return out


def edge_provenance(issue: Dict, entry: Sequence) -> Tuple[str, str]:
    """(status, text) for one blocked-by entry of `issue`.

    status: confirmed (owner's recorded words, or the body's owner-confirmed note) | declared (an
    agent's `block` record) | inferred (the body's "Pilot-inferred" note, unconfirmed: the owner
    confirms or removes it) | unrecorded (a native edge with no record). The body notes are
    per-issue, so they apply to every edge of that issue that has no newer record."""
    rec = edge_records(issue).get(edge_key(str(entry[2]), int(entry[0])))
    if rec:
        kv, c = rec
        said = unquote(kv.get("said", ""))
        if kv["op"] == "confirm":
            return "confirmed", 'owner: "%s" (quoted by %s, %s)' % (said, kv.get("session", "?"),
                                                                   c["at"][:10])
        if kv["op"] == "block":
            return "declared", "declared by %s %s%s" % (kv.get("session", "?"), c["at"][:10],
                                                        (' on "%s"' % said) if said else "")
    body = issue.get("body") or ""
    if _INFERRED_RE.search(body):
        return "inferred", "pilot-inferred, UNCONFIRMED: " + _INFERRED_RE.search(body).group(0).strip("_ ")
    if _BODY_CONFIRMED_RE.search(body):
        return "confirmed", "owner-confirmed (issue body note)"
    return "unrecorded", "native edge, no provenance recorded"


# --------------------------------------------------------------------------- done is proven

@dataclass
class Verdict:
    state: str
    reason: str
    evidence: List[str] = field(default_factory=list)


def evidence_refs(issue: Dict) -> List[Tuple[str, str, str]]:
    """(kind, ref, comment-at) from evidence records posted AFTER the last reopen (consult
    finding 7: evidence from an earlier closure must not prove a later one)."""
    since = issue.get("reopened") or ""
    out = []
    for c in sorted(issue.get("comments", []), key=lambda c: (c["at"], c["id"])):
        r = record(c)
        if r and r[0] == "evidence" and r[1].get("kind") in EVIDENCE_KINDS and c["at"] > since:
            out.append((r[1]["kind"], unquote(r[1].get("ref", "")), c["at"]))
    return out


def quote(v: str) -> str:
    """Lossless marker value (consult finding 12): percent-encode space, >, %, and -- ."""
    return (v.replace("%", "%25").replace(" ", "%20").replace(">", "%3E").replace("\t", "%09")
            .replace("\n", "%0A").replace("--", "%2D%2D"))


def unquote(v: str) -> str:
    import urllib.parse
    return urllib.parse.unquote(v)


def verify(issue: Dict, cfg: Dict[str, object], online: bool = True,
           cache: Optional[Dict[str, str]] = None, _depth: int = 0) -> Verdict:
    """Is this closure proven? Only CLOSED issues are judged.

    Scope, stated (consult finding 9): commit/pr evidence proves that the named commit exists on
    this host / the PR merged, NOT that it is the work; path proves an artifact exists here.
    Relevance is a human or reviewer judgment; the checker only refuses closures with nothing."""
    if issue["state"] != "CLOSED":
        return Verdict(UNKNOWN, "open: nothing to verify")
    if _depth > 3:
        return Verdict(UNKNOWN, "issue evidence chain deeper than 3")
    refs = [r for r in evidence_refs(issue) if not (r[0] == "issue" and r[1].lstrip("#")
                                                    == str(issue["number"]))]
    closer = issue.get("closer")
    shown = ["%s:%s" % (k, r) for k, r, _ in refs]
    if issue["state_reason"] != "COMPLETED":
        if any(k == "said" for k, _, _ in refs):
            return Verdict(SATISFIED, "SATISFIED(owner): closed %s on a recorded owner statement"
                           % (issue["state_reason"] or "?").lower(), shown)
        return Verdict(NOT_SATISFIED, "closed %s with no owner statement"
                       % (issue["state_reason"] or "?").lower(), shown)
    if closer and closer.get("type") == "Commit" and closer.get("oid"):
        return Verdict(SATISFIED, "closed by commit %s" % closer["oid"][:10],
                       ["commit:%s" % closer["oid"][:10]] + shown)
    if closer and closer.get("type") == "PullRequest":
        if closer.get("merged"):
            return Verdict(SATISFIED, "closed by merged PR #%s" % closer["number"],
                           ["pr:%s" % closer["number"]] + shown)
    if not refs:
        return Verdict(NOT_SATISFIED, "closed as completed with no evidence", shown)
    unknown = []
    for k, ref, _ in refs:
        st, why = _check_ref(k, ref, cfg, online, cache)
        if st == SATISFIED:
            return Verdict(SATISFIED, why, shown)
        if st == UNKNOWN:
            unknown.append(why)
    if unknown:
        return Verdict(UNKNOWN, "; ".join(unknown), shown)
    return Verdict(NOT_SATISFIED, "evidence given but none holds", shown)


def _check_ref(k: str, ref: str, cfg: Dict[str, object], online: bool,
               cache: Optional[Dict[str, str]]) -> Tuple[str, str]:
    host, repo = str(cfg["host"]), str(cfg["repo"])
    if k == "said":
        return SATISFIED, "SATISFIED(owner): owner statement recorded (identity not provable on GitHub)"
    if k == "path":
        if not ref.startswith("/") and not ref.startswith("~"):
            return NOT_SATISFIED, "path %r is not absolute" % ref
        p = os.path.expanduser(ref)
        if os.path.realpath(p) in ("/", os.path.expanduser("~"), os.path.realpath(DEV)):
            return NOT_SATISFIED, "path %r is a root, not an artifact" % ref
        if os.path.isdir(p) and not os.listdir(p):
            return NOT_SATISFIED, "path %r is an empty directory" % ref
        return ((SATISFIED, "artifact exists: %s" % ref) if os.path.exists(p)
                else (UNKNOWN, "artifact %s not on this machine" % ref))
    if k == "url":
        return UNKNOWN, "url evidence %s is recorded, not checkable" % ref
    if not online:
        return UNKNOWN, "offline: %s:%s not checked" % (k, ref)
    key = "%s:%s:%s" % (repo, k, ref)
    if cache is not None and key in cache:
        st = cache[key]
        return st, "%s:%s (%s, cached)" % (k, ref, st)
    if k == "commit":
        target_repo, sha = (ref.split("@", 1) if "@" in ref else (repo, ref))
        if not re.match(r"^[0-9a-f]{7,40}$", sha):
            return NOT_SATISFIED, "commit ref %r is not a sha" % ref
        code, _ = rest_status(host, "repos/%s/commits/%s" % (target_repo, sha))
        st = (SATISFIED if code == 200 else NOT_SATISFIED if code in (404, 422)
              else UNKNOWN)
    elif k == "pr":
        code, body = rest_status(host, "repos/%s/pulls/%s" % (repo, ref.lstrip("#")))
        st = (SATISFIED if code == 200 and isinstance(body, dict) and body.get("merged")
              else NOT_SATISFIED if code in (200, 404) else UNKNOWN)
    elif k == "issue":
        # consult finding 8: an issue reference proves only what THAT issue's proof proves
        num = ref.lstrip("#")
        if not num.isdigit():
            return NOT_SATISFIED, "issue ref %r is not a number" % ref
        seen = (cache or {}).setdefault("_issue_stack", "")
        if ("," + num + ",") in "," + seen + ",":
            return NOT_SATISFIED, "issue:%s is circular evidence" % num
        try:
            o, n = repo.split("/")
            from_ = graphql(host, "query($o:String!,$n:String!,$x:Int!){repository(owner:$o,name:$n)"
                            "{issue(number:$x){" + _ISSUE_FIELDS + "}}}",
                            {"o": o, "n": n, "x": int(num)})["repository"]["issue"]
        except GhError as e:
            return UNKNOWN, "issue:%s could not be read (%s)" % (num, str(e)[:80])
        if not from_:
            return NOT_SATISFIED, "issue:%s does not exist" % num
        if cache is not None:
            cache["_issue_stack"] = seen + "," + num
        v = verify(normalize(from_), cfg, online, cache, _depth=1)
        if cache is not None:
            cache["_issue_stack"] = seen
        return v.state, "issue:%s is %s (%s)" % (num, {"SATISFIED": "proven", "NOT_SATISFIED":
                                                      "UNVERIFIED", "UNKNOWN": "unchecked"}[v.state],
                                                v.reason)
    else:
        return UNKNOWN, "unknown evidence kind %s" % k
    if cache is not None and st != UNKNOWN:
        cache[key] = st
    return st, "%s:%s %s" % (k, ref, {"SATISFIED": "verified", "NOT_SATISFIED": "does not exist",
                                       "UNKNOWN": "could not be checked"}[st])


# --------------------------------------------------------------------------- export

def export_paths(cfg: Dict[str, object]) -> Tuple[str, str]:
    d = str(cfg["export"])
    return os.path.join(d, "issues.json"), os.path.join(d, "STATE.md")


def write_export(cfg: Dict[str, object], issues: List[Dict], state_md: str) -> List[str]:
    """Write issues.json + STATE.md; return the paths whose content changed."""
    jpath, spath = export_paths(cfg)
    os.makedirs(os.path.dirname(jpath), exist_ok=True)
    payload = {"repo": cfg["repo"], "host": cfg["host"],
               "data_as_of": max((i["updated"] for i in issues), default=None),
               "exported_at": now_utc().isoformat(timespec="seconds"),
               "issues": issues}
    changed = []
    for path, text, cmp in ((jpath, json.dumps(payload, indent=1, sort_keys=True) + "\n",
                             lambda t: re.sub(r'"exported_at": "[^"]*"', "", t)),
                            (spath, state_md, strip_volatile)):
        old = open(path).read() if os.path.exists(path) else None
        if old is None or cmp(old) != cmp(text):
            with open(path, "w") as f:
                f.write(text)
            changed.append(path)
    return changed


def read_export(cfg: Dict[str, object]) -> Tuple[List[Dict], Dict[str, object]]:
    jpath, _ = export_paths(cfg)
    with open(jpath) as f:
        d = json.load(f)
    return d["issues"], {k: d.get(k) for k in ("exported_at", "data_as_of", "repo")}


def load(cfg: Dict[str, object], offline: bool = False) -> Tuple[List[Dict], Optional[str]]:
    """(issues, None) live, or (issues, warning) from the export when offline or gh failed."""
    err = "offline requested"
    if not offline:
        try:
            return fetch(cfg), None
        except GhError as e:
            err = str(e)
    try:
        issues, meta = read_export(cfg)
    except (OSError, ValueError, KeyError) as e:
        raise GhError("%s; and no readable export (%s)" % (err, e))
    age = now_utc() - ts(str(meta["exported_at"]))
    return issues, ("OFFLINE (%s): read %s exported %s, %.1f h old. Claims and blockers may have "
                    "changed since; do not claim from this view." % (err[:120], export_paths(cfg)[0],
                                                                      meta["exported_at"],
                                                                      age.total_seconds() / 3600))


def strip_volatile(text: str) -> str:
    return re.sub(r"(?m)^.*lodestar:volatile.*$", "", text)


def digest(text: str) -> str:
    return hashlib.sha256(strip_volatile(text).encode()).hexdigest()[:12]
