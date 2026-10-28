"""Purge — remove a managed checkout fleet-wide, retire the id, delete the hosted repo.

Lukas's scoping, 2026-10-01: *"Remove the managed checkout on every machine, retire the id
with a permanent tombstone so nothing re-clones it, and delete the hosted repository on
github.com or the private host so there is no route back."*

And the limit he set in the same breath: *"You do not need to worry about time machine and
other backups. That's too far, outside your jurisdiction."*

🔴 **SO THE HONEST WORD IS REMOVAL, AND THIS MODULE SAYS SO IN ITS OWN OUTPUT.** The design
doc's §E phrasing — *"the material is gone from the defined storage scope"* — is only true
with the scope named, and the scope is **two things**: this machine's working area, and the
hosted repository. Backups, filesystem snapshots, search indexes, other clones and forks,
and cached or mirrored views are all outside it and none of them is touched. Nothing here
claims erasure or unrecoverability, because this mechanism cannot deliver either, and
`SCOPE` rides on every report so the claim and the act stay the same size. The repo has
paid for the opposite habit already: a green check on an inert feature is the most
expensive kind of wrong, and `relocate.quarantine` ends its own success message with
*"Confidentiality eviction incomplete."* for exactly this reason.

**The working-area half GREW on 2026-10-01 and the sentence grew with it.** Lukas ruled that
a purge follows and removes its satellites rather than reporting them and leaving them
dangling, so skill symlinks this machine created, the `~/.claude/projects/` auto-memory
directory and this machine's own in-repo snapshot-memory copy are now inside the scope and
are removed (`lib/satellites.removals`). Widening the act without widening the sentence would
have been the understatement this module exists to prevent — the same defect as an overclaim,
pointing the other way.

**It grew a second time the same day, and the sentence grew again.** Lukas also ruled that a
purge follows **Waypoint shortcut rows**, which this module's scope sentence had listed as out
of scope on the belief that no mechanical edit of them was safe. That belief was false:
`~/dev/Waypoint/waypoint remove <Name>` (and `launch remove`) are non-interactive and each
regenerate the managed shell block afterwards. So a row this machine owns — the universal row,
or one scoped to this machine, not `blocked` — is removed, and **the login file's managed block
is rewritten** as part of doing it. Rows belonging to another machine, and rows recording a
human's standing exclusion, stay out and are named. Backups remain his explicit
non-jurisdiction.

### Why purge is its own thing

§4 of the design assigns rename/rehome/retire/merge to the converger or the one-shot
pattern and then says purge fits **neither**: *"Both existing patterns deliberately retain
material; erasure is not obtainable by renaming a rescue directory."* `relocate.quarantine`
moves a checkout to a holding directory and is honest that the material is still on the
machine. Purge is the one operation that removes rather than relocates, so the ordinary
approval is not the whole safety argument any more — the **arming flag** is the rest of it,
and it lives in the caller (`bin/lifecycle.py`'s `converge --arm-purge`).

### What this module does and does not decide

It decides whether a purge is **performable** and computes exactly what would be done — the
same split `lib/relocate.py` draws. Whether the purge is *wanted* is `lib/plans.py` and the
origin approval. Whether it may *execute* is the arming flag.

### The hosted repository is named in the PLAN, never read from the checkout

Two independent reasons, both load-bearing:

1. **By the time the remote step runs, the checkout is gone**, so there is no `origin` left
   to read. A target read from git config would work on the first machine and be
   unavailable on every retry.
2. A target computed from a directory name is a guessed URL, which is how a repo gets
   pointed at — or deleted on — a host nobody chose. `lifecycle.declared_remote`'s
   docstring already refuses to guess for exactly this reason.

So a purge plan's `new` cell holds a **coordinate**, `HOST/OWNER/REPO`, and it is
cross-checked against the registry's declared `Remote` by identity before anything runs. A
mismatch is a refusal naming both sides. That cross-check is the guard against the single
worst failure available here — a typo in a plan deleting a repo nobody meant to touch — and
it also closes the collision CLAUDE.md §7 records between a stale declared `Remote` and a
rehome: if the two disagree, a human decides which is right rather than this picking one.

⚠️ `new` must therefore **never** be passed through `lifecycle.tilde()`. That helper calls
`os.path.abspath`, which resolved a rehome's URL against the current working directory and
collapsed `https://` to `https:/` — see CLAUDE.md §7. `purge` joins `rehome` in that
exemption.

### What `gh` actually takes — verified, not assumed

Verified on a development machine by reading `gh repo delete --help` (gh 2.100.0) and
by two **read-only** `gh repo view` calls:

- `gh repo delete <HOST/OWNER/REPO> --yes` is the non-interactive form. gh's own help says
  *"For safety, when no repository argument is provided, the `--yes` flag is ignored and you
  will be prompted for confirmation. To delete the current repository non-interactively,
  specify it explicitly"* — so the positional argument is required, not optional, for an
  unattended run.
- The required token scope is **`delete_repo`**, named by that same help text.
- The `[HOST/]OWNER/REPO` positional form resolves against an enterprise host:
  `gh repo view the private host/OWNER/REPO --json name` returned the repo, as did the
  github.com equivalent. (gh documents that shape for `GH_REPO`; this call is the
  confirmation that the positional argument accepts it too.)

**What was NOT verified, and could not be:** that the delete itself succeeds. No delete was
run, by anything, at any point.

🔴 **MEASURED SCOPE GAP:** `gh auth status` can report `delete_repo` on the
github.com token and **not** on the private-host one. So a purge of a private-hosted repo would fail at the remote step today until
`gh auth refresh -h the private host -s delete_repo` is run by the human whose account it
is. `scope_hint` carries that remedy so the refusal names it instead of returning a bare gh
error.
"""

import os
import re
import shutil
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from . import gitutil, machine, paths, relocate, skillclone

#: Rides on every purge report. The scope is stated because the claim must be the same size
#: as the act — see the module docstring.
SCOPE = ("Removal scope: this machine's working area — the managed checkout plus the "
         "satellites of it this machine created or owns (skill symlinks, the auto-memory "
         "directory, this machine's in-repo snapshot-memory copy, and Waypoint shortcut rows "
         "this machine owns, whose removal also regenerates the managed block in the shell "
         "login file) — and the hosted repository. NOT in scope and NOT attempted: backups, "
         "filesystem snapshots, search indexes, other clones or forks, cached or mirrored "
         "views, shortcut rows scoped to another machine or marked `blocked`, and any "
         "quarantine copy. This is removal within that scope, not erasure.")

#: Ids the ordinary row mechanism may not remove — design §3G. Breaking the control channel
#: prevents every later repair, and a purge is the one operation with no undo at all.
PROTECTED_IDS = ("lodestar", "waypoint")

#: The source path does not exist. Whether that is "already done" or "a typo" is the
#: caller's question, because only the registry's declared path can tell them apart.
MISSING_SOURCE = "missing-source"
#: The path is not the one the registry declares for this id. A typo until proven otherwise.
UNDECLARED_PATH = "undeclared-path"
#: There is no registry row, so there is nothing to check the path against.
NO_ROW = "no-row"
#: The path exists but is not a git repository. Never `rmtree` an arbitrary directory.
NOT_A_REPO = "not-a-repo"
#: A linked worktree or submodule — removing it corrupts the checkout that shares its gitdir.
NOT_STANDALONE = "not-standalone"
#: This id, or this path, is exempt from the row mechanism (design §3G).
PROTECTED = "protected"
#: The plan names no hosted repository to delete.
NO_REMOTE_TARGET = "no-remote-target"
#: The plan's coordinate will not parse as HOST/OWNER/REPO.
UNPARSEABLE_TARGET = "unparseable-target"
#: The plan's coordinate is not the remote the registry declares for this id.
TARGET_MISMATCH = "target-mismatch"

KINDS = (MISSING_SOURCE, UNDECLARED_PATH, NO_ROW, NOT_A_REPO, NOT_STANDALONE, PROTECTED,
         NO_REMOTE_TARGET, UNPARSEABLE_TARGET, TARGET_MISMATCH)


class Refusal(NamedTuple):
    kind: str
    detail: str


class Coordinate(NamedTuple):
    """A hosted repository, as `gh` addresses it."""
    host: str
    owner: str
    repo: str

    @property
    def slug(self) -> str:
        return "%s/%s/%s" % (self.host, self.owner, self.repo)


def parse_coordinate(text: str) -> Coordinate:
    """`HOST/OWNER/REPO`, or any git URL, as a `Coordinate`. Raises ValueError otherwise.

    A URL is accepted and reduced through `skillclone.normalize_remote`, so a plan authored
    with the registry's own `Remote` string works and cannot drift from the identity
    comparison used against it. A bare `HOST/OWNER/REPO` keeps its case — GitHub is
    case-insensitive on owner and repo, but echoing back what a human typed makes the
    consequence print easier to check than a silently lowercased one.

    **Exactly three segments, no more and no fewer.** `owner/repo` with no host is refused
    rather than defaulted to github.com: the whole point of this field is that the host is
    the thing a purge must not guess, and this fleet spans two hosts whose contents are
    scoped differently.
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("a purge names its hosted repository explicitly; this is empty")

    candidate = raw
    if "://" in raw or ("@" in raw.split("/")[0] and ":" in raw):
        candidate = skillclone.normalize_remote(raw)

    parts = [part for part in candidate.strip("/").split("/") if part]
    if len(parts) != 3:
        raise ValueError(
            "%r does not read as HOST/OWNER/REPO (got %d segment(s)). A purge must name the "
            "host, because this fleet spans github.com and the private host and a guessed "
            "host is how the wrong repository gets deleted." % (raw, len(parts)))
    host, owner, repo = parts
    if repo.endswith(".git"):
        repo = repo[:-4]
    return Coordinate(host=host, owner=owner, repo=repo)


def coordinate_matches(coord: Coordinate, declared_remote: str) -> bool:
    """Is `coord` the same repository the registry declares? Identity, never string equality.

    Routed through `skillclone.same_remote` so ssh and https forms of one repo compare equal
    — one machine may reach GitHub over ssh and another over https, and a strict comparison
    would make a correct plan unexecutable on one of them.
    """
    if not declared_remote:
        return False
    return skillclone.same_remote("https://" + coord.slug, declared_remote)


def protected_paths(dev_root: Optional[str] = None, lodestar_root: Optional[str] = None
                    ) -> List[str]:
    """Paths no purge may remove, however a plan is written.

    The Lodestar checkout is the control channel: removing it prevents every later repair,
    including the repair of whatever went wrong. The dev root and the home directory are here
    because a plan with an empty or truncated path would otherwise resolve to one of them —
    a refusal that costs nothing and prevents the worst possible typo.
    """
    root = os.path.realpath(dev_root or os.path.expanduser("~/dev"))
    return [os.path.realpath(lodestar_root or paths.root()),
            root,
            os.path.realpath(os.path.expanduser("~"))]


def local_refusals(repo_id: str, repo_path: str, declared_path: str, *,
                   dev_root: Optional[str] = None,
                   lodestar_root: Optional[str] = None) -> List[Refusal]:
    """EVERY reason the local removal must not happen. Empty means performable.

    All checks run, never just the first — `relocate.preconditions` makes the same choice and
    for the same reason: showing a human one problem at a time turns one decision into four
    round trips.

    `MISSING_SOURCE` is reported rather than swallowed, because absence means two completely
    different things and only the caller holds the evidence to tell them apart: absent **at
    the path the registry declares** is this machine's share of the purge already done, while
    absent anywhere else is a typo until proven otherwise. `already_done` answers that
    question; this function only states the fact.
    """
    found = []  # type: List[Refusal]

    if repo_id and repo_id.strip().lower() in PROTECTED_IDS:
        found.append(Refusal(PROTECTED,
                             "%s is exempt from the row mechanism (design §3G): it is the "
                             "control channel or its launcher, and removing it prevents every "
                             "later repair — including the repair of whatever went wrong here."
                             % repo_id))

    resolved = os.path.realpath(os.path.abspath(os.path.expanduser(repo_path or "")))
    for guarded in protected_paths(dev_root, lodestar_root):
        if resolved == guarded:
            found.append(Refusal(PROTECTED,
                                 "%s resolves to %s, which is never removable by a plan. A "
                                 "truncated or empty path in a plan resolves here, so this "
                                 "refusal is also the typo guard." % (repo_path, guarded)))

    if not declared_path:
        found.append(Refusal(NO_ROW,
                             "no registry row declares a path for %s, so there is nothing to "
                             "check %s against. Absence of a row never authorises removal."
                             % (repo_id, repo_path)))
    elif not _same_path(declared_path, repo_path):
        found.append(Refusal(UNDECLARED_PATH,
                             "the plan names %s but the registry declares %s for %s. A purge "
                             "removes a directory outright, so it acts only on the path "
                             "desired state names." % (repo_path, declared_path, repo_id)))

    if not os.path.lexists(repo_path or ""):
        found.append(Refusal(MISSING_SOURCE,
                             "there is nothing at %s" % repo_path))
    elif not gitutil.is_repo(repo_path):
        found.append(Refusal(NOT_A_REPO,
                             "%s exists but is not a git repository, so it is not a managed "
                             "checkout this may remove. Whatever is there was not put there by "
                             "Lodestar." % repo_path))
    elif not relocate.is_standalone(repo_path):
        found.append(Refusal(NOT_STANDALONE,
                             "%s is a linked worktree or a submodule — it shares its git "
                             "directory with another checkout, so removing this tree corrupts "
                             "that one too. This needs a person." % repo_path))

    return found


def remote_refusals(repo_id: str, target: str, declared_remote: str) -> List[Refusal]:
    """EVERY reason the hosted repository must not be deleted. Empty means performable.

    The mismatch check is the one that matters. A plan is hand-written markdown, so the
    coordinate in it is exactly as trustworthy as a typed sentence — and `gh repo delete`
    does not care whether the repository it is pointed at is yours by intent. Comparing
    against the registry's declared `Remote` means a purge can only reach the repository
    desired state already says this id lives at.
    """
    found = []  # type: List[Refusal]
    if not (target or "").strip():
        found.append(Refusal(NO_REMOTE_TARGET,
                             "the plan names no hosted repository, so there is nothing to "
                             "delete and no way to tell that apart from a field left blank by "
                             "accident. Name it as HOST/OWNER/REPO."))
        return found

    try:
        coord = parse_coordinate(target)
    except ValueError as exc:
        found.append(Refusal(UNPARSEABLE_TARGET, str(exc)))
        return found

    if not declared_remote:
        found.append(Refusal(TARGET_MISMATCH,
                             "the plan names %s but the registry declares no Remote for %s, so "
                             "there is nothing to check it against. A URL is never trusted from "
                             "the plan alone." % (coord.slug, repo_id)))
    elif not coordinate_matches(coord, declared_remote):
        found.append(Refusal(TARGET_MISMATCH,
                             "the plan names %s but the registry declares %s for %s. These are "
                             "different repositories. Deleting a hosted repo has no undo, so "
                             "this refuses rather than choosing which of the two is right — if "
                             "a rehome moved this repo, correct the registry's Remote first."
                             % (coord.slug, declared_remote, repo_id)))
    return found


def already_done(repo_path: str, declared_path: str) -> bool:
    """Is this machine's local share of the purge already complete?

    True only when there is nothing at `repo_path` **and** `repo_path` is the path the
    registry declares. The second half is the whole guard: treating any missing path as done
    would make a mistyped plan report success on every machine while the real directory sat
    untouched — a green light on an act that never happened. The same asymmetry
    `lifecycle._absent_as_evicted` already defends for quarantine, which was measured on a
    machine where a retired checkout no longer existed.
    """
    if os.path.lexists(repo_path or ""):
        return False
    return bool(declared_path) and _same_path(declared_path, repo_path)


def local_material(repo_path: str) -> Dict[str, object]:
    """What would be destroyed that exists nowhere else — reported, never used to refuse.

    `uncommitted` counts porcelain entries with `-uall` (without it a new directory collapses
    to one line and the real count is hidden — the trap already recorded for
    `gitutil.changed_paths`). `unpushed` counts commits on any local branch that no remote
    branch contains, which is the only form of "exists nowhere else" git can answer cheaply.

    🔴 **This informs the approval; it does not block the act.** A purge that refused on a
    dirty tree would be refused for ever on whichever machine happens to have a stray file,
    which is precisely the permanently-half-applied state this whole loop exists to end. So
    the numbers are printed in the dry run and in the origin consequence, where a human can
    act on them, and the live run proceeds. `unknown` is True when git could not answer —
    never a zero, because a fabricated zero here is a claim that nothing is being lost.
    """
    if not gitutil.is_repo(repo_path or ""):
        return {"uncommitted": 0, "unpushed": 0, "unknown": True,
                "detail": "not a git repository, so there is nothing git can tell us"}

    code, out, err = gitutil.run(repo_path, "status", "--porcelain", "-uall")
    if code != 0:
        return {"uncommitted": 0, "unpushed": 0, "unknown": True,
                "detail": "git status failed: %s" % (err.strip() or "no stderr")}
    uncommitted = len([line for line in out.split("\n") if line.strip()])

    code, out, err = gitutil.run(repo_path, "log", "--branches", "--not", "--remotes",
                                 "--oneline")
    if code != 0:
        return {"uncommitted": uncommitted, "unpushed": 0, "unknown": True,
                "detail": "git log failed: %s" % (err.strip() or "no stderr")}
    unpushed = len([line for line in out.split("\n") if line.strip()])

    return {"uncommitted": uncommitted, "unpushed": unpushed, "unknown": False,
            "detail": "%d uncommitted path(s), %d commit(s) on a local branch that no remote "
                      "branch contains" % (uncommitted, unpushed)}


def remove_tree(repo_path: str, *, armed: bool = False) -> Dict[str, object]:
    """Remove the checkout. **Refuses unless explicitly armed**, and reports that refusal.

    🔴 `armed=False` is the default and it returns a REFUSAL, never a pretended success.
    `commandexec.run_one` settled this shape in this repo: there is deliberately no outcome
    value meaning "pretended", because a receipt for an act that never reached the world
    paints a green check on an inert feature. A disarmed purge has not happened, and says so.
    """
    if not armed:
        return {"ok": False, "removed": False, "armed": False,
                "reason": "DISARMED: would remove %s. Nothing was changed. %s"
                          % (repo_path, SCOPE)}
    if not os.path.lexists(repo_path or ""):
        return {"ok": False, "removed": False, "armed": True,
                "reason": "there is nothing at %s to remove" % repo_path}
    try:
        shutil.rmtree(repo_path)
    except (OSError, shutil.Error) as exc:
        return {"ok": False, "removed": False, "armed": True,
                "reason": "removing %s failed: %s. Whatever remains is still at that path."
                          % (repo_path, exc)}
    return {"ok": True, "removed": True, "armed": True,
            "reason": "removed the managed checkout at %s. %s" % (repo_path, SCOPE)}


def delete_argv(coord: Coordinate) -> List[str]:
    """The exact command a remote deletion would run. Computed and reportable on its own.

    Kept separate from any execution so the whole plan can be printed, logged and tested
    without a process ever being started — which is what makes "prepared and reported, not
    invoked" a property of the code rather than a promise about how it is called.

    `--yes` plus an explicit positional repository is required, not merely convenient: gh's
    own help says `--yes` is **ignored** when no repository argument is given, and it prompts
    instead. An unattended converger has no one to answer that prompt.
    """
    return ["gh", "repo", "delete", coord.slug, "--yes"]


#: The gh token scope a hosted-repository deletion requires. Named by `gh repo delete --help`
#: itself: *"Deletion requires authorization with the `delete_repo` scope."*
DELETE_SCOPE = "delete_repo"


def token_scopes(host: str,
                 runner: Optional[Callable[[Sequence[str]], Tuple[int, str, str]]] = None
                 ) -> Optional[List[str]]:
    """The scopes gh reports for `host`'s token, or **None when that could not be read**.

    🔴 `None` is not an empty list and must never be flattened into one. An empty list is the
    positive finding "this token carries no scopes"; `None` is "nobody measured". The repo has
    a standing rule about this — *an honest `-1` beats a plausible wrong figure* — and here the
    difference decides whether a preview is allowed to say a step will fail.

    Read-only: `gh auth status` prints what a human reads and mutates nothing. Parsed by
    locating the column-0 host header and then the `Token scopes:` line inside that block,
    because gh reports every authenticated host in one output and the two tokens on one machine can differ in exactly this scope.
    """
    call = runner if runner is not None else _run_gh
    code, out, err = call(["gh", "auth", "status"])
    text = (out or "") + "\n" + (err or "")
    if code != 0 and "Token scopes" not in text:
        return None

    wanted = (host or "").strip().lower()
    in_block = False
    for line in text.split("\n"):
        if line and not line[0].isspace():
            in_block = line.strip().lower() == wanted
            continue
        if in_block and "token scopes:" in line.lower():
            return re.findall(r"'([^']+)'", line)
    return None


def can_delete_on(host: str,
                  runner: Optional[Callable[[Sequence[str]], Tuple[int, str, str]]] = None
                  ) -> Dict[str, object]:
    """Does this machine's gh token for `host` carry `delete_repo`? Three answers, not two.

    `{"known": False}` means unmeasured, and a caller must not turn that into a prediction of
    failure — manufacturing a refusal out of ignorance is how a working path gets blocked.
    `{"known": True, "ok": False}` is a positive finding and is worth acting on: observed in practice, the github.com token can carry `delete_repo` while the
    private host token does not.
    """
    scopes = token_scopes(host, runner)
    if scopes is None:
        return {"known": False, "ok": False, "scopes": [],
                "detail": "could not read `gh auth status` for %s, so whether the token "
                          "carries `%s` is UNMEASURED — not absent" % (host, DELETE_SCOPE)}
    if DELETE_SCOPE in scopes:
        return {"known": True, "ok": True, "scopes": scopes,
                "detail": "the gh token for %s carries `%s`" % (host, DELETE_SCOPE)}
    return {"known": True, "ok": False, "scopes": scopes,
            "detail": "the gh token for %s does NOT carry `%s` (it has: %s), so a deletion "
                      "there cannot succeed from this machine"
                      % (host, DELETE_SCOPE, ", ".join(scopes) or "no scopes at all")}


def scope_hint(coord: Coordinate) -> str:
    """The remedy to print when gh refuses for want of the `delete_repo` scope.

    Observed in practice: the github.com token can carry `delete_repo` while the
    private host token does not. A bare gh error would send the next reader hunting a
    bug in the converger instead of running one command.
    """
    return ("If gh refuses for want of authorization, the required scope is `delete_repo`: "
            "`gh auth refresh -h %s -s delete_repo`. That is an interactive login and "
            "belongs to the account holder." % coord.host)


def host_reachable(coord: Coordinate, machine_key: Optional[str] = None,
                   machines_path: Optional[str] = None) -> bool:
    """Can THIS machine reach the host that holds this repository?

    A home machine may declare `github.com` only and have no connectivity to the private host
    at all (design §3B). A purge of a private-hosted repo therefore cannot complete
    its remote half there, and that is deliberate non-participation rather than a fault — the
    same meaning `UNREACHABLE` already carries in the sweep. A blank `Hosts` column reads as
    reachable, which is the safe direction `machine.host_reachable` already argues for.
    """
    return machine.host_reachable(coord.host, machine_key, machines_path)


def delete_remote(coord: Coordinate, *, armed: bool = False,
                  runner: Optional[Callable[[Sequence[str]], Tuple[int, str, str]]] = None
                  ) -> Dict[str, object]:
    """Delete the hosted repository. **Refuses unless explicitly armed.**

    `runner` takes the argv and returns `(code, out, err)`; it exists so every test can drive
    this without a process ever starting, the same reason `command.Session` takes `dispatch`.
    Untestable code stays untested, and untested destructive code is where the inert features
    hide.

    A refusal always carries the underlying stderr, never a fixed sentence — CLAUDE.md §7
    records a whole system's worth of wrong diagnosis caused by a refusal message that named
    the wrong cause.
    """
    argv = delete_argv(coord)
    if not armed:
        return {"ok": False, "deleted": False, "armed": False, "argv": argv,
                "reason": "DISARMED: would run `%s`. Nothing was sent to %s."
                          % (" ".join(argv), coord.host)}

    call = runner if runner is not None else _run_gh
    code, out, err = call(argv)
    if code == 0:
        return {"ok": True, "deleted": True, "armed": True, "argv": argv,
                "reason": "deleted the hosted repository %s" % coord.slug}

    text = (err or "") + (out or "")
    if "404" in text or "Not Found" in text or "not found" in text:
        # Idempotence: the second machine to converge finds it already gone. That is the
        # desired end state, not a failure — and it must not read as one, or a fleet-wide
        # purge would flag for ever on every machine except the first.
        return {"ok": True, "deleted": False, "armed": True, "argv": argv,
                "reason": "%s is already absent on %s — nothing left to delete there"
                          % (coord.slug, coord.host)}
    return {"ok": False, "deleted": False, "armed": True, "argv": argv,
            "reason": "`%s` failed (exit %d): %s. %s"
                      % (" ".join(argv), code, (err or out).strip() or "no output",
                         scope_hint(coord))}


def _run_gh(argv: Sequence[str]) -> Tuple[int, str, str]:
    """The production runner. Reached only from `delete_remote(armed=True)`."""
    import subprocess
    try:
        done = subprocess.run(list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=120)
    except OSError as exc:
        return 127, "", str(exc)
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after 120s: %s" % " ".join(argv)
    return (done.returncode,
            done.stdout.decode("utf-8", "replace"),
            done.stderr.decode("utf-8", "replace"))


def _same_path(left: str, right: str) -> bool:
    """Compare two declared paths without requiring either to exist.

    `realpath` rather than `samefile`, because a purge compares a path that is *about to stop
    existing* — or has already stopped — against a declared one, and `os.path.samefile`
    raises on an absent path. `realpath` also resolves tower's symlinked `~/dev`, which is
    the trap CLAUDE.md §7 opens with.
    """
    return (os.path.realpath(os.path.abspath(os.path.expanduser(left or "")))
            == os.path.realpath(os.path.abspath(os.path.expanduser(right or ""))))


# --- who may arm a purge unattended ---------------------------------------------------------

#: The committed declaration naming which machines the sweep may arm a purge on. Under `sync/`
#: rather than beside the code because it is fleet-wide desired state a human edits and reviews
#: in a diff, exactly like `registry.md` and `plans.md`.
ARMED_DECLARATION = "PURGE_ARMED"


def armed_selector(root: Optional[str] = None) -> str:
    """The selector naming which machines the sweep may arm. "" means NONE.

    Mirrors `claudestatusline.enabled_selector` deliberately, including joining several lines
    with commas so that `machine.selector_matches` reads them as any-of and a rollout can go one
    box at a time. A missing file is "" rather than an error: the declaration is allowed not to
    exist, and not existing must mean nobody.
    """
    path = os.path.join(root or paths.root(), "sync", ARMED_DECLARATION)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except (IOError, OSError):
        return ""
    tokens = [line.split("#", 1)[0].strip() for line in text.splitlines()]
    return ",".join(token for token in tokens if token)


def sweep_may_arm(key: Optional[str] = None, root: Optional[str] = None,
                  machines_path: Optional[str] = None) -> bool:
    """May the UNATTENDED sweep arm a purge on this machine?

    🔴 **The empty-selector guard is the whole safety property, and it is NOT what the selector
    grammar does by itself.** `machine.selector_matches` documents that an empty spec matches
    *every* machine — correct for "which machines does this skill apply to", and catastrophic
    here: a truncated, emptied or absent declaration would arm the entire fleet for an act with
    no undo. So an empty selector is refused before the grammar is consulted. This is the same
    guard `claudestatusline.is_enabled` holds, and it matters more here by exactly the margin
    between a wrong status line and a deleted repository.

    Consulted by the SWEEP only. `converge --arm-purge` stays available by hand on every machine
    regardless of this file, so the declaration can never be the thing that stops a human who is
    present from finishing a purge — it only decides whether nobody needs to be.
    """
    selector = armed_selector(root)
    if not selector:
        return False
    key = (key or machine.key()).lower()
    return machine.selector_matches(selector, key, machine.labels(key, machines_path))
