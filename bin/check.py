#!/usr/bin/env python3
"""One command that answers "is this system healthy?" — and says what it did NOT check.

    check.py                 # every verification except the test suite
    check.py --tests         # …including ./run_tests.py (slower; can be minutes)
    check.py --strict        # warnings become failures
    check.py --verbose       # print each tool's full output, not just its summary line

It **composes**; it re-implements nothing. Every row below is an existing tool's own exit
code and its own summary line — with one deliberate exception, `sync.py`, which exits 0 even
when it flags a repo, so a flagged row is escalated to WARN here rather than read as OK:

| Row | Tool | Where it runs |
|---|---|---|
| launchagents | `launchctl list <label>` | — |
| machine liveness | `heartbeat.facts()` | in-process, read-only |
| `board.py --check` | Phase 0 primitive | board snapshot |
| `board.py --rebuild` round-trip | Phase 0 🔒 gate | board snapshot |
| `airlock.py` | Phase 2 linter | the real board (read-only) |
| the sync sweep | the live agent's own result, else `sync.py --dry-run` | board snapshot |
| claudeconf captures | `claudeconf.pending_captures()` | in-process, read-only |
| claudeconf skills | `claudeconf.skills_state(dry_run=True)` | in-process, writes nothing |
| `shadow.py --dry-run` | Phase 3 consumer, writes nothing | the real board |
| `airlock.py --gate` | 🔒 Phase 2 gate | the real board |
| `shadow.py --gate --days 1` | 🔒 Phase 4 go-live gate | the real board |
| `run_tests.py` | the suite | opt-in only |

**Two rows call `claudeconf` in-process instead of shelling out, and that is still composing.**
`pending_captures()` is a glob over a committed directory and `skills_state(dry_run=True)` is
explicitly non-writing, so there is no exit code to read and no mutation for a subprocess
boundary to contain. What that boundary *was* buying — a crash in someone else's module cannot
take this command down — is bought back by grading a raised exception UNPROVEN, which is the
same answer this file already gives for any evidence it could not obtain.

**The suite is opt-in, and its absence is stated as loudly as a failure.** That is not
convenience, it is the lesson of two separate defects found on 2026-09-01: Machinist's
`tcc-audit.sh` omitted a permission and still reported clean, and `shadow.py --gate` prints
"NOT CHECKED HERE — a gate that runs the suite can hang" for exactly this reason. A check
that silently omits something is worse than no check, so a skipped row is named in the
footer with "this is not a pass" spelled out.

**Health and gates are different questions, and one exit code cannot carry both.** A tool
erroring means something is *broken*. An unmet 🔒 gate means a *phase has not been reached* —
today `shadow.py --gate --days 1` fails because the shadow day has not been run, which is
not a fault. Collapsing the two would make `check.py` exit non-zero forever and nobody would
run it. So:

    0   everything checked passed
    1   a health check FAILED (something is broken) — or, with --strict, warned
    3   health is fine but ≥1 🔒 gate is unmet (a phase, not a fault)

Exit 2 is left to the repo's existing meaning (an unknown machine), and `check.py` does not
use it: an unregistered machine is *reported* here rather than fatal, because a health tool
that dies on its first line has told you nothing.

**"Unproven" is a third answer, and it is the whole reason this file grew a retry.** Measured
2026-09-01: `check.py`'s own sweep fetched 26 repos while the `com.lodestar.sync` LaunchAgent
was fetching the same 26 every 127 s, GitHub throttled the collision, and git reported it as
`fetch failed: … and the repository exists.` — the tail of "Repository not found / make sure
you have the correct access rights and the repository exists", which reads as a missing repo
or dead auth. Within minutes: 14 repos flagged, then 3 *different* ones, then 0. Every repo
retried by hand fetched fine. **The varying membership was the tell.**

A tool that reports fourteen broken repos and is wrong gets ignored, and an ignored health
tool is D13's corollary failing from the other direction — so the strong claim is no longer
made from one observation:

1. **Read the live sweep rather than racing it.** If the real `SYNC.md` holds a *non-dry-run*
   result younger than ~3 minutes, that result is used and no second sweep runs. It is
   strictly better evidence — the same measurement, taken by the process that owns it, against
   the real board — and it removes the extra 26-repo fetch burst that caused the misreport in
   the first place. `--sweep` forces the second sweep anyway.
2. **Retry only the repos that failed, once, after a 20 s delay, against a control.** A
   transient failure that clears becomes UNPROVEN, not flagged, and never reaches exit 1.
3. **The control is what makes "it failed twice" mean anything.** A retry with no control is
   an uncontrolled measurement, and this file's first version proved why: forced back-to-back
   sweeps throttled GitHub so hard that the *retry* was throttled too, and 19 provably-fine
   repos "failed twice". So a repo the sweep fetched fine seconds ago is fetched before and
   after the retry. Control good on both sides → a repo still failing is FAIL, loudly. Control
   bad on either side → UNPROVEN, because the retry measured the network, not the repo. No
   control at all, because *nothing* fetched → FAIL: that is an outage or lost auth.
4. **Nothing is ever dropped.** A repo whose retry could not run — no registry row, budget
   spent, control unusable — stays in the flagged count, is named in the report, and never
   silently becomes healthy. UNPROVEN qualifies the claim; it does not hide the row.

UNPROVEN is rendered `?`, counted separately from ok/warn/fail, and spelled out in the footer
the same way a skipped check is: not a pass, not a failure, and not something to infer from.

**Per D13 this never blocks and never prompts.** Every subprocess gets `stdin=DEVNULL` and a
timeout, so a tool that would wait for a passphrase fails fast and visibly instead of hanging
the check. There is no `input()` anywhere and a test asserts that.

**It does not mutate the real board.** Anything that writes root-relative files —
`board.py --rebuild`, and `sync.py`'s `SYNC.md` — runs with `LODESTAR_ROOT` pointed at a
throwaway snapshot of the board, removed on the way out. `airlock.py` and `shadow.py
--dry-run` are read-only and run against the real thing so their verdict is the real verdict.

**The one side effect worth knowing about**, and only when it does run its own sweep (so:
`--sweep`, or no live result to read): `sync.py --dry-run` writes
`~/dev/.lodestar/sync-status.json`, whose path is machine-local and not root-relative, so the
snapshot cannot redirect it. It lands with `dry_run: true` until the live sweep overwrites it
~127 s later. Nothing here reads that file back — see `read_live_sweep` for why the
root-relative `SYNC.md` is the honest source instead. The LaunchAgent row is still evaluated
FIRST, so its liveness reading precedes anything this command does.

The retry's `git fetch` is the only git command this file runs. It is read-only with respect
to the board, strictly less work than the sweep it is checking, and there is no `commit`,
`push`, `pull` or force anywhere in the module — a test asserts that by parsing it.
"""

import argparse
import filecmp
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib import clock, machine, mdtable, paths, registry  # noqa: E402

# Verdicts. WARN exists so "reportable state" and "broken" stay distinguishable — an absent
# LaunchAgent is the motivating case. UNPROVEN exists so "I could not obtain evidence" stays
# distinguishable from both — a transient fetch failure is the motivating case.
OK = "ok"
WARN = "warn"
FAIL = "fail"
SKIPPED = "skipped"
UNPROVEN = "unproven"

HEALTH = "health"
GATE = "gate"

LABEL = {OK: "OK", WARN: "WARN", FAIL: "FAIL", SKIPPED: "SKIP", UNPROVEN: "?"}

# How loud each verdict is, for folding several findings about one row into one answer. A
# proven WARN outranks an UNPROVEN: "something is reportable" beats "something is unknown".
RANK = {SKIPPED: 0, OK: 0, UNPROVEN: 1, WARN: 2, FAIL: 3}

EXIT_OK = 0
EXIT_UNHEALTHY = 1
EXIT_GATE_UNMET = 3

DEFAULT_TIMEOUT = 120
SYNC_TIMEOUT = 300      # 17 `git fetch`es over the network; ~14 s observed, 300 is slack
TESTS_TIMEOUT = 900

SYNC_LABEL = "com.lodestar.sync"

# The sweep fires every 127 s, so a live result older than one interval plus slack is no
# longer evidence about *now* and this runs its own sweep instead.
LIVE_SWEEP_MAX_AGE = 180

# One retry, of only the repos that failed. 20 s because that is the observed figure that
# works: "a re-sweep 20 s later returned all 26 clean" (docs/SYNC_AGENT.md). A 5 s delay was
# tried first and measured — it sits inside the throttle window, so every retry failed too
# and the row claimed 19 broken repos that were all fine. The budget caps the whole pass so a
# slow network cannot stretch a health check into minutes; repos it does not reach are
# reported as unretried, never as healthy.
RETRY_DELAY_SECONDS = 20.0
RETRY_BUDGET_SECONDS = 120
FETCH_TIMEOUT = 60

# How many known-good repos to try as the retry's control. More than one because the first
# candidate may not be a real git remote at all — `~/.claude` is a row in the sweep and has
# no registry entry — and a control that cannot be fetched is not a failed control.
CONTROL_CANDIDATES = 3


def worst(*verdicts: str) -> str:
    return max(verdicts, key=lambda verdict: RANK.get(verdict, 0))

# What a board snapshot needs to be a faithful stand-in for the checks that write.
# `claude/global` is here because sync.py's ~/.claude step renders CLAUDE.md from it to
# report drift; `claude/snapshot` is 550 KB of backup and is deliberately not copied.
SNAPSHOT_PATHS = ("machines.md", "BOARD.md", "PAUSE", "untracked-scope.json", "loops", "journal", "sync",
                  os.path.join("claude", "global"))

# Required = its absence has an operational consequence worth a WARN. Optional = absence is
# the documented state today (the shadow agent is "not yet installed" per SHADOW_AGENT.md).
AGENTS = (
    ("com.lodestar.sync", True),
    ("com.lodestar.shadow", False),
    ("com.machinist.m365reconnect", False),
)


class Check(NamedTuple):
    name: str
    tier: str
    verdict: str
    detail: str
    output: str = ""
    hint: str = ""


# --- running things ----------------------------------------------------------------------


def run_python(argv: Sequence[str], root: Optional[str] = None,
               timeout: int = DEFAULT_TIMEOUT) -> Tuple[Optional[int], str, bool]:
    """Run one of the repo's scripts. Returns (exit code, combined output, timed_out).

    `stdin=DEVNULL` is the D13 guard: `git fetch` inside `sync.py` can ask for a passphrase,
    and a health check that stops to be answered is a health check nobody runs twice.
    """
    env = dict(os.environ)
    if root:
        env["LODESTAR_ROOT"] = root
    command = [sys.executable] + [str(a) for a in argv]
    try:
        result = subprocess.run(command, cwd=paths.root(), env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=timeout)
    except subprocess.TimeoutExpired as expired:
        partial = expired.output or b""
        return None, partial.decode("utf-8", "replace"), True
    except OSError as error:
        return 127, "could not run %s: %s" % (" ".join(command), error), False
    return result.returncode, result.stdout.decode("utf-8", "replace"), False


SYSTEMD_UNITS = {SYNC_LABEL: ("lodestar-sync.timer", "lodestar-sync.service")}


def systemd_as_launchctl(label: str, systemctl: str = "systemctl") -> Tuple[int, str]:
    """Linux/WSL: answer `launchctl list <label>` from systemd, in launchctl's own shape.

    The sweep on tower is `lodestar-sync.timer` (system scope, hourly) running
    `lodestar-sync.service`, not a LaunchAgent. A label with no unit there (shadow, the M365
    reconnect) is "not loaded", which is the honest answer and only an OK for the optional ones.
    """
    units = SYSTEMD_UNITS.get(label)
    if not units:
        return 113, "no systemd unit for %s on this machine" % label
    timer, service = units
    try:
        got = subprocess.run([systemctl, "show", timer, service, "--property=Id,ActiveState,"
                              "LoadState,ExecMainStatus,MainPID"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15)
    except (subprocess.TimeoutExpired, OSError) as error:
        return 1, "systemctl failed: %s" % error
    blocks = got.stdout.decode("utf-8", "replace").strip().split("\n\n")
    facts = [dict(l.split("=", 1) for l in b.splitlines() if "=" in l) for b in blocks]
    by_id = {f.get("Id"): f for f in facts}
    t, svc = by_id.get(timer, {}), by_id.get(service, {})
    if got.returncode != 0 or t.get("LoadState") != "loaded" or t.get("ActiveState") != "active":
        return 113, "%s is %s/%s" % (timer, t.get("LoadState", "?"), t.get("ActiveState", "?"))
    lines = ['"LastExitStatus" = %d;' % (int(svc.get("ExecMainStatus") or 0) << 8)]
    if svc.get("ActiveState") == "active" and svc.get("MainPID", "0") != "0":
        lines.append('"PID" = %s;' % svc["MainPID"])
    return 0, "\n".join(lines)


def run_launchctl(label: str) -> Tuple[int, str]:
    """`launchctl list <label>`. Exit 113 and a message when the label is not loaded."""
    if sys.platform.startswith("linux"):
        return systemd_as_launchctl(label)
    try:
        result = subprocess.run(["/bin/launchctl", "list", label],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=15)
    except (subprocess.TimeoutExpired, OSError) as error:
        return 1, "launchctl failed: %s" % error
    return result.returncode, result.stdout.decode("utf-8", "replace")


def git_fetch(repo: str, timeout: int = FETCH_TIMEOUT) -> Tuple[bool, str]:
    """One `git fetch` in `repo`, for retrying a single failing repo. (ok, note).

    Three guards, not one, because `stdin=DEVNULL` alone does not make git non-interactive:
    ssh reads a passphrase from the *terminal*, not from stdin, so `GIT_TERMINAL_PROMPT=0`
    and ssh's own `BatchMode=yes` are what actually turn "would hang forever" into "fails in
    a second, visibly". Both are set only when absent, so an existing `GIT_SSH_COMMAND` (a
    custom key, a proxy) still wins.
    """
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    try:
        result = subprocess.run(["git", "-C", repo, "fetch", "--quiet"], env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "the retry timed out after %ds" % timeout
    except OSError as error:
        return False, "could not run git: %s" % error
    text = result.stdout.decode("utf-8", "replace").strip()
    if result.returncode == 0:
        return True, "fetched cleanly on the retry"
    return False, text.split("\n")[-1] if text else "exit %d, no output" % result.returncode


# --- reading what came back --------------------------------------------------------------


def verdict_from_exit(code: Optional[int], timed_out: bool = False) -> str:
    if timed_out:
        return FAIL
    return OK if code == 0 else FAIL


def summary_line(output: str, prefer: Sequence[str] = ()) -> str:
    """The one line worth putting in a table: the last one matching `prefer`, else the last.

    Deliberately reads each tool's *own* words rather than re-deriving a verdict. If
    `airlock.py` changes how it phrases "2 unreachable", this row changes with it.
    """
    lines = [line.strip() for line in output.replace("\r", "").split("\n") if line.strip()]
    if not lines:
        return "no output"
    for needle in prefer:
        matched = [line for line in lines if needle in line]
        if matched:
            return matched[-1]
    return lines[-1]


def parse_launchctl(text: str) -> Dict[str, str]:
    """Pull `"Key" = value;` pairs out of `launchctl list <label>`'s plist-ish dump."""
    found = {}  # type: Dict[str, str]
    for line in text.split("\n"):
        line = line.strip().rstrip(";")
        if "=" not in line or not line.startswith('"'):
            continue
        key, _, value = line.partition("=")
        key = key.strip().strip('"')
        value = value.strip().strip('"')
        if key and value:
            found[key] = value
    return found


def decode_exit(raw: str) -> Tuple[Optional[int], str]:
    """Turn `launchctl list`'s `LastExitStatus` into an exit code plus a human string.

    The per-label dump reports a **wait(2) status**, not an exit code: a job that exited 1
    shows `256`. Printing 256 verbatim looks like a fault in the reader's own tooling, and
    worse, comparing it to `"0"` would be the only comparison that ever worked. So decode it
    the way `launchctl list` with no argument already does.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None, "last exit %s" % raw
    if value == 0:
        return 0, "last exit 0"
    signal = value & 0x7F
    if signal:
        return signal, "killed by signal %d" % signal
    return value >> 8, "last exit %d" % (value >> 8)


def agent_running(launchctl: Callable[[str], Tuple[int, str]],
                  label: str = SYNC_LABEL) -> bool:
    """Is that LaunchAgent executing *right now*? `launchctl list` shows a PID only then."""
    code, text = launchctl(label)
    return code == 0 and "PID" in parse_launchctl(text)


def agent_state(label: str, required: bool, code: int, text: str) -> Tuple[str, str]:
    """(verdict, detail) for one LaunchAgent.

    **Absence is a state, not an error** — the brief's wording, and the right shape: the
    shadow agent is documented as not installed, and a laptop is *supposed* to lack it.
    A required agent that is absent still only WARNs, because `check.py` reporting an
    install decision as a broken system would train people to ignore its exit code.
    """
    if code != 0:
        return (WARN if required else OK,
                "not loaded%s" % (" — nothing is sweeping this machine" if required else ""))
    fields = parse_launchctl(text)
    raw = fields.get("LastExitStatus")
    running = "PID" in fields
    if raw is None:
        return OK, "loaded, no exit recorded yet%s" % (", running now" if running else "")
    status, phrase = decode_exit(raw)
    detail = "loaded, %s%s" % (phrase, ", running now" if running else "")
    if status != 0:
        return WARN, detail
    return OK, detail


# --- the board snapshot ------------------------------------------------------------------


def snapshot_board(root: str, into: str) -> str:
    """Copy the parts of the board the writing checks need. Returns the snapshot root.

    A copy rather than the original because `board.py --rebuild` rewrites every loop file
    and `sync.py` rewrites `SYNC.md`. Missing paths are skipped: `PAUSE` is normally absent,
    and its absence is not a snapshot failure.
    """
    target = os.path.join(into, "board")
    paths.ensure_dir(target)
    for relpath in SNAPSHOT_PATHS:
        source = os.path.join(root, relpath)
        destination = os.path.join(target, relpath)
        if not os.path.exists(source):
            continue
        if os.path.isdir(source):
            shutil.copytree(source, destination, symlinks=True,
                            ignore=shutil.ignore_patterns("__pycache__"),
                            ignore_dangling_symlinks=True)
        else:
            paths.ensure_dir(os.path.dirname(destination))
            shutil.copy2(source, destination)
    return target


def differing_files(left: str, right: str, subpaths: Sequence[str]) -> List[str]:
    """Repo-relative paths that differ between two roots, or exist in only one of them."""
    differing = []  # type: List[str]
    for subpath in subpaths:
        names = set()
        for root in (left, right):
            base = os.path.join(root, subpath)
            if os.path.isdir(base):
                for directory, _dirs, files in os.walk(base):
                    for name in files:
                        full = os.path.join(directory, name)
                        names.add(os.path.relpath(full, root).replace(os.sep, "/"))
            elif os.path.exists(base):
                names.add(subpath.replace(os.sep, "/"))
        for relative in sorted(names):
            a, b = os.path.join(left, relative), os.path.join(right, relative)
            if not (os.path.exists(a) and os.path.exists(b)):
                differing.append(relative)
            elif not filecmp.cmp(a, b, shallow=False):
                differing.append(relative)
    return sorted(differing)


# --- the individual checks ---------------------------------------------------------------


def launchagent_check(launchctl: Callable[[str], Tuple[int, str]]) -> Check:
    verdicts, details = [], []
    for label, required in AGENTS:
        code, text = launchctl(label)
        verdict, detail = agent_state(label, required, code, text)
        verdicts.append(verdict)
        details.append("%s %s" % (label, detail))
    worst = WARN if WARN in verdicts else OK
    return Check("launchagents", HEALTH, worst, " · ".join(details))


def liveness_check() -> Check:
    """Is the sync sweep actually producing output, not merely loaded?

    `launchctl` says the job exists and last exited 0; a job that exits 0 doing nothing looks
    identical. `SYNC.md`'s mtime is the liveness signal SYNC_AGENT.md names, so this reads it
    directly rather than inferring liveness from the LaunchAgent row.
    """
    report = paths.sync_report_file()
    if not os.path.exists(report):
        return Check("sync freshness", HEALTH, WARN, "no SYNC.md — the sweep has not run here")
    age = time.time() - os.stat(report).st_mtime
    detail = "SYNC.md written %s ago" % _human(age)
    # The sweep runs every 127 s; three cadences of silence is a wedge, not jitter.
    return Check("sync freshness", HEALTH, OK if age <= 400 else WARN, detail,
                 hint="" if age <= 400 else "expected within ~2 min; check "
                                            + ("`journalctl -u lodestar-sync`"
                                               if sys.platform.startswith("linux")
                                               else "~/Library/Logs/lodestar-sync.err"))


def _human(seconds: float) -> str:
    if seconds < 90:
        return "%ds" % int(seconds)
    if seconds < 5400:
        return "%dm" % int(seconds / 60)
    return "%.1fh" % (seconds / 3600.0)


def _human_gap(seconds: float) -> str:
    """Like `_human`, but in days once the gap is one — this row's numbers are days.

    `_human` renders four days as "96.0h", which the reader has to divide in their head at
    exactly the moment the row is trying to say "this machine has been quiet for days". Kept
    as a separate function rather than folded into `_human`, because `_human` also renders the
    sync row's seconds-and-minutes ages, where hours are the right unit and already correct.
    """
    if seconds >= 36 * 3600:
        return "%.1fd" % (seconds / 86400.0)
    return _human(seconds)


# --- the machine liveness row -------------------------------------------------------------
#
# `docs/LAPTOP_PARITY.md` §4 item 1 is the structural blind spot this closes: a sweep that
# finds nothing dirty commits *nothing at all*, and `SYNC.md` is machine-local by decision
# D-D — so from here, "the Laptop is powered off and its agent is unloaded" and "the Laptop is
# sweeping perfectly and finding nothing dirty" are the *same observation*. `lib/heartbeat.py`
# built the writing half (one committed event per machine per calendar day). This row is the
# reading half. Without it the heartbeat is write-only: the data lands in the journal and
# nothing ever says a machine went quiet, so the hole is still open in practice.
#
# It composes rather than re-deriving: `heartbeat.facts()` already resolves the roster, finds
# the newest readable heartbeat per machine, and computes ages from each event's own `ts`
# rather than from file mtime (which a `git checkout` resets, making a stale clone read as
# confidently current). Re-deriving any of that here would be a second implementation to keep
# in step with the first.

MACHINES_ROW = "machine liveness"

# The longest gap a HEALTHY peer can produce, plus grace — the convention `lib/status.py` sets
# out and `lib/heartbeat.py` follows. Deliberately a much larger number than heartbeat's own.
#
# `heartbeat.SILENT_AFTER_SECONDS` is 2 h, which is the right threshold for the question the
# STATUS surface asks: "did this machine sweep recently?" — it is guarded to emit at most once
# an hour, so 2 h is one whole missed cycle of grace before silence means something. It is the
# wrong threshold here. A laptop is *supposed* to be shut overnight, all weekend, and while
# travelling, so even 2 h fires within a single evening off — closed Friday afternoon and
# reopened Monday morning is ~67 h of entirely healthy silence. A row that warns every evening
# is a row nobody reads, and an ignored health row is D13's corollary failing in precisely the
# way the sync row above already had to be rewritten to stop doing.
#
# So four days. That clears a normal weekend with a full day of slack — a late Monday start, a
# Monday holiday — and past it the machine has missed a whole working week's worth of chances to
# speak, at which point "its agent is not running" is a likelier story than "it was closed". A
# week of travel with the laptop off will warn, and that is the correct trade rather than a
# defect: WARN is reportable state, it is never FAIL, and it never moves the exit code off 0.
QUIET_AFTER_SECONDS = 4 * 24 * 3600


def _machine_facts():
    """Import `lib/heartbeat.py` on demand, for the same reason `_claudeconf` does.

    A module-scope import would turn a syntax error in someone else's file into "the health
    check does not start". Late, the worst case is this one row reporting UNPROVEN — which is
    also the honest answer when the heartbeat data cannot be read at all.
    """
    from lib import heartbeat
    return heartbeat.facts()


def machines_check(facts: Optional[Callable[[], Dict]] = None,
                   this: Optional[str] = None) -> Check:
    """How long since every OTHER machine in `machines.md` last said it was alive?

    Grading, and why each verdict is the one it is:

    - **seen inside the threshold → OK**, naming each machine and its age. The age is the
      point: "OK" alone would be the same uninformative silence this row exists to break.
    - **quiet past `QUIET_AFTER_SECONDS` → WARN.** Not FAIL: a shut laptop is silent *and*
      healthy, so this can only ever say "the Desktop cannot see it", never "it is broken".
    - **never seen at all → WARN, said differently.** A machine with a row in `machines.md`
      and no heartbeat on any day did not go quiet; it most likely never had the sweep
      installed. Same verdict, different problem and a different fix, so it gets its own
      wording rather than being folded into an age that would have to be printed as ∞.
    - **no heartbeat anywhere → UNPROVEN.** `facts()["installed"]` is False until *some*
      machine has written one, and "never written" is a different fact from "written and old"
      — only the second is a warning. A fresh clone is the motivating case.
    - **unreadable → UNPROVEN.** A corrupt file, a timestamp that will not parse, an import
      that fails: a check that could not run has not passed, and must not be reported as
      either. Same treatment the `claudeconf` rows give their own local state.

    **This machine is excluded, not measured.** You are running `check.py` on it, so its
    liveness is not in question, and whether its *own* sweep is emitting is already the
    `sync freshness` row's job. An unregistered key that has been writing heartbeats is named
    but never graded — `machines.md` already makes that machine fail loudly on itself.
    """
    try:
        data = (facts or _machine_facts)() or {}
        me = this if this is not None else machine.key()
        rows = [row for row in (data.get("machines") or []) if row.get("key") != me]
    except Exception as error:      # local committed state; see UNPROVEN in the module docstring
        return Check(MACHINES_ROW, HEALTH, UNPROVEN,
                     "could not read the heartbeats: %s" % error,
                     hint="this row reads only the committed journal, so an error here is a "
                          "repo in an odd shape rather than a network problem — it says "
                          "nothing either way about whether the other machines are alive")

    if not data.get("installed"):
        return Check(MACHINES_ROW, HEALTH, UNPROVEN,
                     "no machine has ever written a heartbeat — %d other machine(s) "
                     "registered, none of them measurable" % len(rows),
                     hint="`lib/heartbeat.py` emits one event per machine per calendar day "
                          "from the sync sweep; until the first one lands anywhere, this row "
                          "cannot distinguish a quiet machine from a healthy one. On a fresh "
                          "clone that is expected — re-run after the sweep has fired once.")

    known = [row for row in rows if row.get("known")]
    unknown = [row for row in rows if not row.get("known")]
    stray = ""
    if unknown:
        stray = "also saw %d unregistered key(s): %s — no row in machines.md" % (
            len(unknown), _named([str(row.get("key")) for row in unknown]))

    output = "\n".join(
        "%-9s %-11s %s" % (row.get("role"), row.get("key"),
                           "never" if not row.get("as_of")
                           else "%s (%s)" % (_age(row), row.get("as_of")))
        for row in rows)

    if not known:
        return Check(MACHINES_ROW, HEALTH, OK,
                     " · ".join(part for part in
                                ("no other machine is registered in machines.md — nothing to "
                                 "measure", stray) if part), output)

    never, unreadable, quiet, fresh = [], [], [], []
    for row in known:
        if not row.get("as_of"):
            never.append(row)
        elif row.get("age_seconds") is None:
            unreadable.append(row)
        elif row["age_seconds"] > QUIET_AFTER_SECONDS:
            quiet.append(row)
        else:
            fresh.append(row)

    # Loudest first, the same ordering `sweep_detail` uses: the news, not the reassurance.
    phrases = ["%s %s NEVER — no heartbeat on any day" % (row.get("role"), row.get("key"))
               for row in never]
    phrases += ["%s %s QUIET for %s" % (row.get("role"), row.get("key"), _age(row))
                for row in quiet]
    phrases += ["%s %s last heartbeat's timestamp will not parse (%s)"
                % (row.get("role"), row.get("key"), row.get("as_of")) for row in unreadable]
    phrases += ["%s %s %s ago" % (row.get("role"), row.get("key"), _age(row))
                for row in fresh]

    detail = " · ".join(part for part in
                        ["%d other machine(s), %d seen within %s"
                         % (len(known), len(fresh), _human_gap(QUIET_AFTER_SECONDS)),
                         _named(phrases), stray] if part)

    verdict = OK
    if quiet or never:
        verdict = worst(verdict, WARN)
    if unreadable:
        verdict = worst(verdict, UNPROVEN)

    hints = []
    if never:
        hints.append("a machine registered in machines.md with no heartbeat ever most likely "
                     "never had the agent installed — a different problem from going quiet, "
                     "and a different fix: check `launchctl list %s` and "
                     "bin/laptop_bootstrap.py on that machine, not anything here"
                     % SYNC_LABEL)
    if quiet:
        hints.append("a shut laptop is silent AND healthy, so this is not a fault — it says "
                     "the sweep on that machine has had no chance to speak for over %s. Past "
                     "that, an unloaded LaunchAgent is the likelier story than a long weekend."
                     % _human_gap(QUIET_AFTER_SECONDS))
    if unreadable:
        hints.append("the heartbeat exists but its age cannot be computed, so it proves "
                     "nothing either way — ages come from the event's own `ts`, never from "
                     "file mtime, so a hand-edited or truncated event reads as unknown")
    return Check(MACHINES_ROW, HEALTH, verdict, detail, output, " · ".join(hints))


def _age(row: Dict) -> str:
    seconds = row.get("age_seconds")
    return "unknown" if seconds is None else _human_gap(seconds)


# --- the sync sweep row ------------------------------------------------------------------
#
# `sync.py` exits 0 even when it flags a repo, so its exit code is not the whole answer:
# reading "OK" over fourteen "🚩 Needs you" rows is D13's corollary failing one way, and
# reading "14 broken repos" over fourteen throttled fetches is the same corollary failing the
# other way. Both were observed on 2026-09-01. So the row is derived from the sweep's own rows
# — and a fetch failure, the one outcome that is routinely transient, gets one retry before
# anything is asserted about it.

FLAGGED_ROW = "flagged"          # sync.py's FLAGGED outcome: "🚩 Needs you"
CONTENDED_ROW = "lock-contention"  # sync.py's CONTENDED outcome: "🔓"
SKIPPED_ROW = "skipped"

# Stand-in for a `\|` inside a SYNC.md table cell, so splitting on `|` cannot tear the row.
_CELL_PIPE = "\x00"


class Attention(NamedTuple):
    """One repo the sweep wants read: its name, the sweep's own words, and which kind."""
    name: str
    detail: str
    kind: str


class Sweep(NamedTuple):
    """A sweep result, from either source, reduced to what the row needs.

    `controls` are repos this same sweep fetched successfully seconds ago. They are the
    control group for the retry, and without them a retry is an uncontrolled measurement.
    """
    header: str
    rows: List[Attention]
    skipped: int
    output: str
    live: bool
    controls: Sequence[str] = ()


class Retry(NamedTuple):
    """What one retry pass established — including whether it establishes anything.

    `control_ok` is the difference between a measurement and a guess. Measured 2026-09-01:
    three forced back-to-back sweeps throttled GitHub so hard that the *retry* was throttled
    too, and 19 provably-fine repos "failed twice". A repo that fails twice while a repo known
    to work fetches fine on either side of the retry is real news; a repo that fails twice
    while the control fails too says nothing at all about that repo.
    """
    cleared: List[str]                   # failed once, fetched fine on the retry
    persistent: List[Tuple[str, str]]    # failed twice: (name, the second failure)
    unretried: List[str]                 # never retried, so still unproven either way
    control: str = ""                    # the known-good repo used as the control
    control_ok: bool = False             # it fetched before AND after the retry
    control_note: str = ""


def is_fetch_failure(detail: str) -> bool:
    """Is this flagged row a failed `git fetch` — the one outcome worth retrying?

    Deliberately matches every fetch failure rather than trying to spot the throttled ones.
    `check.py` cannot tell throttling from a dead key by reading the message: GitHub returns
    the *same* "…and the repository exists." tail for both. That is the whole point — the
    retry is what tells them apart, so the message only has to route the row to it.
    """
    return "fetch failed" in (detail or "").lower()


def attention_rows(output: str) -> List[Attention]:
    """Parse the sweep's stdout rows back into structured attention items.

    The tally lines it prints at the end ("🚩 14 repo(s) need you") carry the same icon as a
    repo row and must not be mistaken for a repo called "14".
    """
    rows = []  # type: List[Attention]
    for raw in output.replace("\r", "").split("\n"):
        line = raw.strip()
        for icon, kind in (("🚩", FLAGGED_ROW), ("🔓", CONTENDED_ROW)):
            if not line.startswith(icon):
                continue
            rest = line[len(icon):].strip()
            fields = rest.split(None, 1)
            if not fields:
                break
            if fields[0].isdigit() and "repo(s)" in rest:
                break  # the sweep's own tally, not a repo
            rows.append(Attention(fields[0].strip(),
                                  fields[1].strip() if len(fields) > 1 else "", kind))
            break
    return rows


def control_candidates(output: str) -> List[str]:
    """Repos this sweep fetched successfully: `·` clean, `✅` synced, `🔓` contended.

    Those three outcomes are only reachable in `sync.py` *after* its `fetch` step returned 0,
    which is what makes them a control. `⏭ skipped` deliberately is not: a skip can happen
    before the fetch (wrong branch, no upstream) as easily as after it.
    """
    names = []  # type: List[str]
    for raw in output.replace("\r", "").split("\n"):
        line = raw.strip()
        for icon in ("·", "✅", "🔓"):
            if not line.startswith(icon):
                continue
            fields = line[len(icon):].strip().split(None, 1)
            if fields and not (fields[0].isdigit() and "repo(s)" in line):
                names.append(fields[0].strip())
            break
    return names


def parse_own_sweep(output: str) -> Sweep:
    """What `sync.py --dry-run` just told us."""
    lines = [line.strip() for line in output.replace("\r", "").split("\n")]
    header = next((line for line in lines if "sweep as" in line), "")
    skipped = len([line for line in lines if line.startswith("⏭")])
    return Sweep(header.replace("DRY RUN: ", ""), attention_rows(output), skipped,
                 output, False, control_candidates(output))


def read_live_sweep(report_file: Optional[str], max_age: int = LIVE_SWEEP_MAX_AGE,
                    now: Optional[float] = None) -> Optional[Sweep]:
    """The live agent's own recent result, read out of `SYNC.md`, or None if not trustworthy.

    **Reading this beats running a second sweep, and that is the fix rather than a shortcut.**
    It is the same measurement taken moments ago by the process that owns it, against the real
    board rather than a snapshot; it costs no network at all; and it removes the extra 26-repo
    fetch burst which — colliding with the LaunchAgent's own burst every 127 s — is what made
    GitHub throttle and made `check.py` report a varying set of "broken" repos in the first
    place. Racing a measurement in order to measure it is the bug.

    **Why `SYNC.md` and not `~/dev/.lodestar/sync-status.json`**, which is the more obvious
    choice being structured JSON: the status file is machine-local rather than root-relative,
    so `check.py`'s own dry run overwrites the real one and marks it `dry_run: true`. That is
    self-perpetuating — measured while building this: after one `--sweep`, four consecutive
    runs each found only their own leftover, refused it, swept again, and re-poisoned it. Every
    run paid the 26-repo burst it was trying to avoid. `SYNC.md` is root-relative, so a dry run
    under `LODESTAR_ROOT=<snapshot>` writes the snapshot's copy and the real file is only ever
    written by a real sweep. It is also the liveness signal `docs/SYNC_AGENT.md` already names.

    A report whose "Last sweep" line carries `· DRY RUN` is refused — someone ran one by hand,
    and a dry run is not evidence about what the sweep did. So is a table that does not look
    like the repo table. Anything missing, stale or unparseable returns None and the caller
    sweeps for itself: an absent answer is never dressed up as a clean one.
    """
    if not report_file or not os.path.exists(report_file):
        return None
    try:
        stat = os.stat(report_file)
        with open(report_file, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return None
    age = max(0.0, (now if now is not None else time.time()) - stat.st_mtime)
    if age > max_age:
        return None
    stamp = next((line for line in text.split("\n")
                  if line.strip().startswith("Last sweep:")), "")
    if not stamp or "DRY RUN" in stamp:
        return None

    rows, skipped, described = [], 0, []
    verified, others = [], []
    # `sync.py` escapes a pipe inside a detail as `\|`, and a naive cell split would tear the
    # row apart there — so the escape is neutralised before splitting and restored after.
    for row in mdtable.parse(text.replace("\\|", _CELL_PIPE)):
        name = row.get("repo", "").strip().strip("`")
        outcome = row.get("outcome", "").strip()
        detail = row.get("detail", "").replace(_CELL_PIPE, "|").strip()
        if not name or not outcome:
            continue
        described.append("%-14s %-16s %s" % (outcome, name, detail))
        if outcome == FLAGGED_ROW:
            rows.append(Attention(name, detail, FLAGGED_ROW))
            continue
        if outcome == CONTENDED_ROW:
            rows.append(Attention(name, detail, CONTENDED_ROW))
        elif outcome == SKIPPED_ROW:
            skipped += 1
        # Anything the sweep did not flag can serve as the retry's control, preferring the
        # ones it says it actually reached. Deliberately not "clean means freshly fetched":
        # the sweep now fetches a quiet repo on a tier cadence rather than every time, so
        # `clean` no longer implies a fetch. A control only has to be a repo not under
        # suspicion — its own probe is what measures the network.
        if "verified" in row.get("remote", ""):
            verified.append(name)
        else:
            others.append(name)
    if not described:
        return None   # not the table we thought it was
    header = "live sweep %s ago — %d repo(s)" % (_human(age), len(described))
    return Sweep(header, rows, skipped, "\n".join(described), True, verified + others)


def sweep_detail(sweep: Sweep, cleared: Sequence[str] = (), note: str = "") -> str:
    """Fold a sweep into one line, keeping every attention row it still stands behind.

    A plain last-line summary is not enough: the sweep prints its worst rows FIRST and its
    status paths last, so "the last line" is the one line that never carries news. Repos in
    `cleared` are dropped from the flagged count on purpose — they failed once and fetched
    fine seconds later, so counting them as flagged is the false alarm this row exists to
    stop making. They are not dropped from the *report*: `note` says how many there were.
    """
    if not sweep.header:
        return summary_line(sweep.output, ("need you", "sweep as"))
    cleared = set(cleared)
    flagged = [row for row in sweep.rows
               if row.kind == FLAGGED_ROW and row.name not in cleared]
    contended = [row for row in sweep.rows if row.kind == CONTENDED_ROW]
    parts = [sweep.header,
             "%d flagged, %d lock-contended, %d skipped"
             % (len(flagged), len(contended), sweep.skipped)]
    # Capped: a sweep where SSH really is down flags every repo at once, and 14 rows on one
    # line is unreadable. The full list is one `--verbose` away.
    parts.extend("%s %s" % (row.name, row.detail) for row in flagged[:3])
    if len(flagged) > 3:
        parts.append("and %d more (--verbose for all)" % (len(flagged) - 3))
    if note:
        parts.append(note)
    return " · ".join(parts)


def sync_detail(output: str, cleared: Sequence[str] = (), note: str = "") -> str:
    return sweep_detail(parse_own_sweep(output), cleared, note)


def retry_note(retry: Retry, delay: float = RETRY_DELAY_SECONDS) -> str:
    """The clause that says what the retry proved, in the honest tense for each outcome."""
    parts = []  # type: List[str]
    if retry.cleared:
        parts.append("%d fetch failure(s) cleared on one retry %ds later — transient, and "
                     "NOT evidence of a broken remote or lost auth"
                     % (len(retry.cleared), int(delay)))
    if retry.persistent:
        named = ", ".join("%s (%s)" % (name, message)
                          for name, message in retry.persistent[:2])
        if len(retry.persistent) > 2:
            named += " and %d more" % (len(retry.persistent) - 2)
        if retry.control_ok:
            parts.append("%d still failing after the retry while the control %s fetched fine "
                         "either side of it, so this is real: %s"
                         % (len(retry.persistent), retry.control, named))
        else:
            parts.append("%d still failing after the retry, but %s — so the retry proves "
                         "NOTHING about them and they stay unproven: %s"
                         % (len(retry.persistent), retry.control_note, named))
    if retry.unretried:
        parts.append("%d NOT retried — the %ds retry budget ran out, so they stay unproven: "
                     "%s" % (len(retry.unretried), RETRY_BUDGET_SECONDS,
                             ", ".join(retry.unretried[:3])))
    return " · ".join(parts)


def make_fetch_probe(registry_path: Optional[str] = None) -> Callable[[str],
                                                                     Tuple[bool, str]]:
    """A `probe(name) -> (ok, note)` that fetches one registered repo by name.

    The registry is read once, here, rather than per repo. A name with no row cannot be
    retried at all, and that answers "unproven" with a False — keeping the row flagged is the
    only safe direction, because the alternative is a failure quietly becoming a pass.
    """
    by_name = {entry.name: entry.path for entry in registry.entries(registry_path)}

    def probe(name: str) -> Tuple[bool, str]:
        path = by_name.get(name)
        if not path:
            return False, "no row in sync/registry.md, so the retry could not run"
        return git_fetch(path)

    return probe


def retry_fetch_failures(names: Sequence[str], probe: Callable[[str], Tuple[bool, str]],
                         controls: Sequence[str] = (),
                         sleep: Optional[Callable[[float], None]] = None,
                         delay: float = RETRY_DELAY_SECONDS,
                         budget: float = RETRY_BUDGET_SECONDS,
                         now: Optional[Callable[[], float]] = None) -> Retry:
    """Retry exactly the repos that failed, once each, against a control.

    Bounded three ways on purpose — one attempt per repo, one shared wait, one total time
    budget — because the failure mode being diagnosed is *too much concurrent fetching*. A
    retry loop that hammers the remote manufactures the very throttling it is trying to rule
    out; that was measured, not imagined. Repos the budget does not reach come back as
    `unretried`, never as cleared.

    **The control is bracketed: fetched before the retry and again after it.** A control that
    works on both sides means conditions were good throughout, so a repo that still failed in
    between failed for its own reasons and is real news. A control that fails on either side
    means the retry was measuring the network, not the repos, and it is reported as proving
    nothing. Without that, "it failed twice" is an uncontrolled measurement — which is how
    this function's first version confidently flagged 19 healthy repos.
    """
    sleep = sleep or time.sleep
    now = now or time.time
    cleared = []  # type: List[str]
    persistent = []  # type: List[Tuple[str, str]]
    unretried = []  # type: List[str]
    if not names:
        return Retry(cleared, persistent, unretried)

    sleep(delay)
    started = now()

    control, control_ok = "", False
    control_note = ("no repo fetched successfully in this sweep, so there was no working "
                    "control to compare against")
    for candidate in list(controls)[:CONTROL_CANDIDATES]:
        ok, note = probe(candidate)
        control, control_note = candidate, note
        if ok:
            control_ok = True
            break

    for index, name in enumerate(names):
        if index and now() - started > budget:
            unretried.append(name)
            continue
        ok, message = probe(name)
        if ok:
            cleared.append(name)
        else:
            persistent.append((name, message))

    if control_ok and persistent:
        # The closing bracket. Only worth paying for when there is a claim to validate.
        ok, note = probe(control)
        if not ok:
            control_ok = False
            control_note = ("%s fetched fine before the retry and failed after it (%s), so "
                            "conditions changed underneath the retry" % (control, note))
    return Retry(cleared, persistent, unretried, control, control_ok, control_note)


def sync_check(run, snapshot: Optional[str], report_file: Optional[str] = None,
               probe: Optional[Callable[[str], Tuple[bool, str]]] = None,
               sleep: Optional[Callable[[float], None]] = None,
               sweeping: bool = False, force_sweep: bool = False) -> Check:
    """The sync row: read or run a sweep, retry only its fetch failures, then grade.

    Grading, loudest first:

    - a sweep that could not run at all (non-zero exit, timeout) → FAIL
    - a repo whose fetch failed twice **while the control worked** → FAIL. Not throttling.
    - a fetch failure that nothing fetched alongside → FAIL. No control means no working
      remote at all, which is an outage or lost auth, and worth waking someone for.
    - anything else the sweep flagged, or lock contention → WARN, exactly as before
    - a fetch failure that cleared, that the budget never reached, or that the control could
      not vouch for → UNPROVEN, exit 0, every repo still named
    """
    if force_sweep:
        sweep = None
    else:
        sweep = read_live_sweep(report_file)

    concurrent = ""
    if sweep is None:
        code, output, timed_out = run(["bin/sync.py", "--dry-run"], root=snapshot,
                                      timeout=SYNC_TIMEOUT)
        name = "sync.py --dry-run"
        if timed_out:
            return Check(name, HEALTH, FAIL, "timed out after %ds" % SYNC_TIMEOUT, output)
        if code != 0:
            return Check(name, HEALTH, FAIL,
                         summary_line(output, ("need you", "sweep as")), output)
        sweep = parse_own_sweep(output)
        if sweeping:
            # Said plainly rather than hidden: this run fetched 26 repos while the agent was
            # fetching the same 26, which is the collision GitHub throttles.
            concurrent = ("ran while %s was mid-sweep, so a fetch failure here is weak "
                          "evidence" % SYNC_LABEL)
    else:
        name = "sync sweep (live agent result)"

    pending = [row.name for row in sweep.rows
               if row.kind == FLAGGED_ROW and is_fetch_failure(row.detail)]
    retry = Retry([], [], [])
    if pending:
        retry = retry_fetch_failures(pending, probe or make_fetch_probe(),
                                     controls=sweep.controls, sleep=sleep)

    others = [row for row in sweep.rows
              if not (row.kind == FLAGGED_ROW and is_fetch_failure(row.detail))]
    verdict = WARN if others else OK
    if retry.cleared or retry.unretried:
        verdict = worst(verdict, UNPROVEN)
    if retry.persistent:
        # A repo that failed twice is never dropped — it stays in the flagged count and is
        # named. What the control decides is whether the report gets to call it *broken*.
        verdict = worst(verdict, FAIL if retry.control_ok or not retry.control else UNPROVEN)

    note = " · ".join(part for part in (concurrent, retry_note(retry)) if part)
    detail = sweep_detail(sweep, retry.cleared, note)

    if retry.persistent and (retry.control_ok or not retry.control):
        hint = ("a fetch that fails twice while another repo fetches fine is not throttling — "
                "check `ssh -o BatchMode=yes -T git@github.com` and that each named repo "
                "still exists on the remote")
    elif retry.persistent or retry.cleared or retry.unretried:
        hint = ("unproven, not broken: GitHub refuses bursts of concurrent SSH connections "
                "and git reports that as \"…and the repository exists.\", which reads like "
                "dead auth. See the troubleshooting table in docs/SYNC_AGENT.md.")
    else:
        hint = ("a flagged repo is `sync.py`'s own \"needs you\". A repo simply not cloned on "
                "this machine is NOT one of these any more — since D18 that is the `○ absent` "
                "outcome, which this check ignores by design. So a flag here is a real fault: "
                "a conflict, an unsafe git state, or a directory that exists but is not a repo")
    return Check(name, HEALTH, verdict, detail, sweep.output, hint)


# --- the ~/.claude convergence rows (D18) ------------------------------------------------
#
# `claudeconf.py` converges `~/.claude` inside every sweep, and two of its outcomes are states
# a *person* has to clear rather than faults the sweep can fix: a captured `CLAUDE.md` waiting
# to be classified, and a skill whose wiring never materialized on this machine. The sweep
# writes both into `SYNC.md` and moves on, and neither reaches the sync row here — so without a
# row each they stay invisible until somebody runs a command by hand, which is D13's corollary
# from the quiet side. Both are WARN and never FAIL: each is a "needs you", not a breakage.

CAPTURES_ROW = "claudeconf captures"
SKILLS_ROW = "claudeconf skills"

# How many names one table row carries before deferring the rest to --verbose. Same reasoning
# as the sync row's cap: a machine missing every skill repo would otherwise print a wall.
NAMED_LIMIT = 3


def _claudeconf():
    """Import `bin/claudeconf.py` on demand, not at module scope.

    Late on purpose. `check.py` must survive `claudeconf` being mid-edit or mid-refactor —
    a module-scope import would turn a syntax error in someone else's file into "the health
    check does not start", which is the loudest possible way to tell you nothing.
    """
    import claudeconf   # noqa: E402  (bin/ went on sys.path at the top of this module)
    return claudeconf


def _skills_state():
    """`claudeconf.skills_state` in the only mode this file is allowed to use.

    `dry_run=True` lives here, once, rather than at the call site: `check.py` reports on the
    skill wiring and must never be the thing that changes it.
    """
    return _claudeconf().skills_state(dry_run=True)


def _named(items: Sequence[str], limit: int = NAMED_LIMIT) -> str:
    shown = " · ".join(items[:limit])
    if len(items) > limit:
        shown += " · and %d more (--verbose for all)" % (len(items) - limit)
    return shown


def pending_captures_check(captures: Optional[Callable[[], Sequence[str]]] = None) -> Check:
    """WARN while this machine is holding `CLAUDE.md` content nobody has filed yet.

    A pending capture means the sweep found the live file drifted, saved it verbatim, and —
    per D18's invariant I1 — deliberately did NOT render. So the machine stays pinned to its
    own `CLAUDE.md` until a session runs `claudeconf.py adopt`: it keeps working locally, but
    shared edits made anywhere else stop arriving, and nothing else says so out loud.
    """
    try:
        pending = list((captures or _claudeconf().pending_captures)())
    except Exception as error:      # the adopt queue is local state; see UNPROVEN above
        return Check(CAPTURES_ROW, HEALTH, UNPROVEN,
                     "could not read the adopt queue: %s" % error,
                     hint="this row reads only local state, so an error here is a repo in an "
                          "odd shape rather than a network problem — run `claudeconf.py "
                          "check` by hand to see it directly")
    if not pending:
        return Check(CAPTURES_ROW, HEALTH, OK, "no captures awaiting classification")
    names = [os.path.basename(path) for path in pending]
    return Check(CAPTURES_ROW, HEALTH, WARN,
                 "%d capture(s) awaiting classification: %s"
                 % (len(pending), _named(names)),
                 "\n".join(pending),
                 hint="run `claudeconf.py adopt` in a session — it files each capture into "
                      "claude/global/ and re-renders. Until then the sweep keeps this "
                      "machine's CLAUDE.md exactly as it is (D18 I1), so a shared edit made "
                      "on another machine is not reaching this one.")


def skills_check(state: Optional[Callable[[], Tuple[Sequence[str],
                                                    Sequence[str]]]] = None) -> Check:
    """WARN on a skill that did not materialize here; stay quiet about curation.

    Both faults are silent by nature, which is the whole reason the row exists. A **dangling**
    link means the target repo is not cloned on this machine — and Claude Code does not error
    on a dangling skill symlink, it simply does not see the skill, so the capability is absent
    from every session here with nothing anywhere saying so. A **broken** skill has a
    `SKILL.md` that will not load. Neither stops anything else working, so neither is FAIL.

    An **unlinked** skill is not a fault and never grades here. D18 has `claudeconf` report the
    gap between `~/dev`'s 55 `SKILL.md` files and the 21 global links *without* linking them,
    because the gap is deliberate curation; and an absent `claude/skills.md` only means seeding
    has not happened yet. Both arrive as informational `lines` and grade OK.
    """
    try:
        lines, faults = (state or _skills_state)()
    except Exception as error:      # a skills table in an odd state must not end the run
        return Check(SKILLS_ROW, HEALTH, UNPROVEN,
                     "could not read the skill state: %s" % error,
                     hint="the skill rows could not be reconciled at all, so this says "
                          "nothing either way about whether the skills are wired — run "
                          "`claudeconf.py skills` to see the error in full")
    lines, faults = list(lines or []), list(faults or [])
    output = "\n".join(lines + faults)
    if faults:
        return Check(SKILLS_ROW, HEALTH, WARN,
                     "%d wiring fault(s): %s" % (len(faults), _named(faults)), output,
                     hint="a dangling link is a repo that is not cloned here; Claude Code "
                          "does not error on one, so the skill silently does not exist in "
                          "any session on this machine. `claudeconf.py skills` names the "
                          "target, and a registry-participating repo is cloned by the sweep.")
    return Check(SKILLS_ROW, HEALTH, OK, _named(lines) if lines else "nothing to report",
                 output)


def tests_detail(output: str) -> str:
    """`Ran N tests` alone is ambiguous — the verdict line is the point.

    `run_tests.py` prints the count and the verdict on separate lines, and picking either one
    alone produces a row that reads like the opposite of what happened. Both, or neither.
    """
    ran = summary_line(output, ("Ran ",))
    verdict = next((line.strip() for line in reversed(output.split("\n"))
                    if line.strip().startswith(("OK", "FAILED"))), "")
    if not verdict:
        return ran
    return "%s — %s" % (ran, verdict)


def sandbox_measured_version() -> str:
    from lib import sandbox
    return sandbox.MEASURED_VERSION


def sandbox_check(measure=None) -> Check:
    """Re-measure Stage A's containment instead of trusting the flags in lib/sandbox.py.

    Phase 4's containment was designed around `--allowed-tools ''`, which was then measured
    and found to run tools anyway. The lesson written into `docs/SANDBOX_MEASUREMENT.md` is
    that no verdict may rest on a CLI flag unless that flag was measured on this machine at
    this version — so this row *measures*, and the pinned version is only a record of when
    it was last true.

    The verdict mapping reuses vocabulary this file already has, and the interesting case is
    `INVALID` → `UNPROVEN`. A containment probe whose subprocess never launched wrote no
    file, which looks identical to success; the first probe harness failed exactly that way
    on all five cases and read as "containment held 5/5". `UNPROVEN` is the only honest row
    for it: nothing was shown to be broken, and nothing was shown to work.
    """
    from lib import sandbox

    # Captured BEFORE the default is substituted: afterwards `measure` is always truthy, so
    # "did the caller inject this?" is unanswerable and the guard below would never fire.
    injected = measure is not None
    measure = measure or sandbox.measure_containment
    try:
        result = measure()
    except sandbox.ContainmentError as exc:
        return Check("sandbox containment", HEALTH, UNPROVEN,
                     "could not build the sandbox: %s" % exc,
                     hint="lib/sandbox.py needs auth keys out of ~/.claude/settings.json, "
                          "which claude-config-sync owns; check whether a key was renamed")

    # Record it, so anything else that needs to know (bin/execute.py --gate) can read a recent
    # measurement instead of spending five more model calls. Usable only while it names the
    # installed build AND is inside the freshness window — see sandbox.read_measurement.
    # Recording a failure matters as much as recording a pass.
    #
    # ONLY when we took the measurement ourselves. A caller that injected `measure` handed us a
    # result, and a result we did not take is not something to persist as evidence about this
    # machine. That is not hypothetical fastidiousness: with this recording unconditionally,
    # the unit tests' fabricated "build 2.2.0, contained OK" was written straight into the real
    # ~/dev/.lodestar record, and the execution gate then read it as real.
    if not injected:
        try:
            sandbox.record_measurement(sandbox.measurement_file(), result)
        except (IOError, OSError):
            pass  # a health check that dies because it could not write a cache is worse than
                  # one that reports the health it already measured

    version = result["version"]
    containment = result["containment"]
    inventory = result["inventory"]
    measured = "measured against %s" % version["measured"]

    if containment["verdict"] == "INVALID":
        return Check("sandbox containment", HEALTH, UNPROVEN,
                     "%s — %s" % (containment["verdict"], containment["reason"]),
                     hint="silence is not containment; re-run to settle it")
    if not containment["ok"]:
        return Check("sandbox containment", HEALTH, FAIL,
                     "%s — %s (%s)" % (containment["verdict"], containment["reason"],
                                       measured),
                     hint="Phase 4 must not be flipped on while this fails; see "
                          "docs/SANDBOX_MEASUREMENT.md")
    read_probe = result.get("read_probe") or {"ok": True, "reason": ""}
    if not read_probe["ok"]:
        return Check("sandbox containment", HEALTH, FAIL,
                     "the read probe failed — %s (%s)" % (read_probe["reason"], measured),
                     hint="Phase 4 must not be flipped on while this fails; see "
                          "docs/SANDBOX_MEASUREMENT.md")

    base = ("write and read probes both denied, positive control wrote (%s)" % measured)

    # ADVISORY, never a verdict. The inventory is a self-report and was measured
    # confabulating twice — once as prose with a fake `<invoke>` block, once as a clean
    # list naming `AgentTool`, which is not a tool in this build. It is still printed,
    # because a name nobody denied is the one thing it can tell you that the behavioural
    # probes cannot, and it is how `LSP` was found. Names already in the deny list are
    # dropped: reciting them back is noise.
    advisory = []
    unknown = inventory.get("unknown_tools") or []
    if unknown:
        advisory.append("canary also named %s, in no deny list — a self-report, so "
                        "confirm behaviourally before believing it" % ", ".join(unknown))

    # WARN-worthy: genuine drift only.
    notes = []
    if version["drifted"]:
        notes.append(version["note"])
    new_stale = [r for r in result["stale_deny_rules"]
                 if r not in sandbox.KNOWN_STALE_DENY_RULES]
    if new_stale:
        notes.append("deny rule(s) newly matching no known tool: %s — the tool set has "
                     "moved under the list" % ", ".join(new_stale))

    detail = base + ("" if not advisory else " · " + "; ".join(advisory))
    if notes:
        return Check("sandbox containment", HEALTH, WARN,
                     "%s — but %s" % (detail, "; ".join(notes)),
                     hint="containment held where it is measurable; re-audit "
                          "DENIED_TOOLS against this build")
    return Check("sandbox containment", HEALTH, OK, detail)


def tool_check(name: str, tier: str, argv: Sequence[str], run, root: Optional[str],
               timeout: int, prefer: Sequence[str] = (), hint: str = "",
               detail_fn=None, verdict_fn=None) -> Check:
    code, output, timed_out = run(argv, root=root, timeout=timeout)
    if timed_out:
        return Check(name, tier, FAIL, "timed out after %ds" % timeout, output, hint)
    detail = detail_fn(output) if detail_fn else summary_line(output, prefer)
    verdict = verdict_fn(code, output) if verdict_fn else verdict_from_exit(code, timed_out)
    return Check(name, tier, verdict, detail, output, hint)


def board_roundtrip_check(run, real_root: str, snapshot: str) -> Check:
    """🔒 The Phase 0 gate: does the journal still reconstruct every loop's state?

    Run in the snapshot, then the rebuilt loop files and `BOARD.md` are compared byte-for-byte
    against the real ones. A difference means the journal and the loop files have drifted —
    which is the one failure that would make the whole event-sourced recovery story false.
    """
    code, output, timed_out = run(["bin/board.py", "--rebuild"], root=snapshot,
                                  timeout=DEFAULT_TIMEOUT)
    if timed_out:
        return Check("board.py --rebuild round-trip", HEALTH, FAIL,
                     "timed out after %ds" % DEFAULT_TIMEOUT, output)
    if code != 0:
        return Check("board.py --rebuild round-trip", HEALTH, FAIL,
                     summary_line(output), output)
    drifted = differing_files(real_root, snapshot, ["loops", "BOARD.md"])
    rebuilt = summary_line(output, ("rebuilt",))
    if drifted:
        shown = ", ".join(drifted[:4])
        if len(drifted) > 4:
            shown += " and %d more" % (len(drifted) - 4)
        return Check("board.py --rebuild round-trip", HEALTH, FAIL,
                     "%d file(s) do NOT round-trip: %s" % (len(drifted), shown), output,
                     hint="loop state and the journal disagree; read the diff before "
                          "trusting `board.py --rebuild` as a recovery path")
    return Check("board.py --rebuild round-trip", HEALTH, OK,
                 "%s; loops/ and BOARD.md byte-identical" % rebuilt, output)


# --- the consolidated verdict ------------------------------------------------------------


def summarize(checks: Sequence[Check], strict: bool = False) -> Tuple[int, str]:
    health = [c for c in checks if c.tier == HEALTH]
    gates = [c for c in checks if c.tier == GATE]
    skipped = [c for c in checks if c.verdict == SKIPPED]
    failed = [c for c in health if c.verdict == FAIL]
    warned = [c for c in health if c.verdict == WARN]
    unproven = [c for c in checks if c.verdict == UNPROVEN]
    unmet = [c for c in gates if c.verdict == FAIL]
    ok = [c for c in checks if c.verdict == OK]

    lines = [""]
    lines.append("  %d check(s): %d ok, %d warn, %d fail, %d unproven, %d not checked"
                 % (len(checks), len(ok), len(warned), len(failed), len(unproven),
                    len(skipped)))
    if gates:
        lines.append("  %d gate(s): %d met, %d unmet"
                     % (len(gates), len(gates) - len(unmet), len(unmet)))
    lines.append("")

    if skipped:
        lines.append("  ⚠ NOT CHECKED (%d): %s."
                     % (len(skipped), ", ".join(c.name for c in skipped)))
        lines.append("    This is not a pass. Nothing above says anything about it either "
                     "way.")
        for entry in skipped:
            if entry.detail:
                lines.append("    · %s — %s" % (entry.name, entry.detail))
        lines.append("")

    if unproven:
        # Treated exactly like a skipped check, for the same reason: the reader must not be
        # able to infer a pass OR a failure from it. This is the row that used to shout
        # "14 repos broken" at a throttled network.
        lines.append("  ❓ UNPROVEN (%d): %s."
                     % (len(unproven), ", ".join(c.name for c in unproven)))
        lines.append("    Not a pass and not a failure — the evidence was too weak to assert "
                     "either.")
        for entry in unproven:
            lines.append("    · %s — %s" % (entry.name, entry.detail))
            if entry.hint:
                lines.append("      %s" % entry.hint)
        lines.append("")

    if failed:
        for entry in failed:
            lines.append("  🚩 %s — %s" % (entry.name, entry.detail))
            if entry.hint:
                lines.append("     %s" % entry.hint)
        lines.append("")
    if warned:
        for entry in warned:
            lines.append("  ⚠ %s — %s" % (entry.name, entry.detail))
            if entry.hint:
                lines.append("     %s" % entry.hint)
        lines.append("")
    if unmet:
        for entry in unmet:
            lines.append("  🔒 gate unmet: %s — %s" % (entry.name, entry.detail))
            if entry.hint:
                lines.append("     %s" % entry.hint)
        lines.append("    An unmet gate is a phase not yet reached, not a broken system.")
        lines.append("")

    if failed or (strict and warned):
        code = EXIT_UNHEALTHY
        reasons = ["%d check(s) failed" % len(failed)] if failed else []
        if strict and warned:
            reasons.append("%d warning(s), and --strict counts those" % len(warned))
        lines.append("verdict: UNHEALTHY — %s (exit %d)" % ("; ".join(reasons), code))
    elif unmet:
        code = EXIT_GATE_UNMET
        lines.append("verdict: HEALTHY, %d gate(s) unmet — nothing checked is broken "
                     "(exit %d)" % (len(unmet), code))
    elif unproven:
        # Deliberately NOT exit 1, and deliberately not escalated by --strict either. --strict
        # promotes warnings, and a warning is a claim; an unproven row is the absence of one.
        # Turning "I could not get evidence" into "your system is broken" is precisely the
        # crying-wolf this row was rewritten to stop.
        code = EXIT_OK
        lines.append("verdict: NOTHING BROKEN — everything proven passed, %d row(s) unproven "
                     "(exit %d)" % (len(unproven), code))
    else:
        code = EXIT_OK
        lines.append("verdict: HEALTHY — everything checked passed (exit %d)" % code)
    if skipped:
        lines.append("         …but %d thing(s) were NOT checked; see above."
                     % len(skipped))
    if unproven:
        lines.append("         …and %d row(s) came back UNPROVEN; re-run to settle them."
                     % len(unproven))
    return code, "\n".join(lines) + "\n"


def render(checks: Sequence[Check], verbose: bool) -> str:
    width = max([len(c.name) for c in checks] + [12])
    lines = []
    for entry in checks:
        marker = "🔒 " if entry.tier == GATE else "   "
        lines.append("  %-4s  %s%-*s  %s" % (LABEL[entry.verdict], marker, width,
                                             entry.name, entry.detail))
        if verbose and entry.output.strip():
            for line in entry.output.strip().split("\n"):
                lines.append("            | %s" % line)
    return "\n".join(lines) + "\n"


# --- main --------------------------------------------------------------------------------


def main(argv=None, run=None, launchctl=None, stream=None, report_file=None,
         probe=None, sleep=None, captures=None, skills=None, machines=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tests", action="store_true",
                        help="also run ./run_tests.py. OFF by default because a check that "
                             "runs the suite can hang; when it is off, the footer says so "
                             "rather than implying the suite passed.")
    parser.add_argument("--strict", action="store_true",
                        help="treat warnings as failures (exit 1)")
    parser.add_argument("--verbose", action="store_true",
                        help="print each tool's full output under its row")
    parser.add_argument("--sweep", action="store_true",
                        help="run `sync.py --dry-run` even when the live agent's own recent "
                             "result is available. Slower, and it adds a second 26-repo "
                             "fetch burst that GitHub may throttle — which is why reading "
                             "the live result is the default.")
    parser.add_argument("--sandbox", action="store_true",
                        help="re-measure Stage A's containment for real: three `claude -p` "
                             "calls (a positive control, a write attempt, a tool "
                             "inventory). OFF by default because it is slow and spends "
                             "tokens; when it is off the row says NOT CHECKED rather than "
                             "implying the sandbox holds.")
    parser.add_argument("--date", metavar="YYYY-MM-DD",
                        help="the date airlock.py --gate should judge (default today)")
    args = parser.parse_args(argv)

    run = run or run_python
    launchctl = launchctl or run_launchctl
    out = stream or sys.stdout
    report_file = report_file or paths.sync_report_file()

    real_root = paths.root()
    date = args.date or clock.today()
    try:
        role = machine.role()
    except machine.UnknownMachine:
        # Reported, not fatal: a health tool that dies on line one has told you nothing.
        role = "unknown (this machine has no row in machines.md)"

    print("Lodestar health check — %s/%s · %s" % (role, machine.runtime(), clock.now()),
          file=out)
    print("board: %s" % real_root, file=out)

    workspace = tempfile.mkdtemp(prefix="lodestar-check-")
    try:
        snapshot = snapshot_board(real_root, workspace)
        print("snapshot (writing checks run here): %s\n" % snapshot, file=out)

        checks = [
            # FIRST: the LaunchAgent's own liveness, before this command does anything.
            launchagent_check(launchctl),
            liveness_check(),
            # `liveness_check` answers "is the sweep alive HERE"; this one answers it for
            # every other machine, which nothing else on this box can see (LAPTOP_PARITY §4).
            machines_check(machines),
            tool_check("board.py --check", HEALTH, ["bin/board.py", "--check"],
                       run, snapshot, DEFAULT_TIMEOUT, ("BOARD.md",),
                       hint="run `bin/board.py` to regenerate it"),
            board_roundtrip_check(run, real_root, snapshot),
            tool_check("airlock.py", HEALTH, ["bin/airlock.py"], run, real_root,
                       DEFAULT_TIMEOUT, ("error(s)", "airlock clean")),
            # Reads ~/dev/.lodestar/sync-status.json BEFORE any dry run of its own can
            # overwrite it, and asks launchctl whether the agent is mid-sweep right now.
            sync_check(run, snapshot, report_file=report_file, probe=probe, sleep=sleep,
                       sweeping=agent_running(launchctl), force_sweep=args.sweep),
            # The two halves of the sweep's ~/.claude convergence that need a person, which
            # the sync row above cannot carry: read in-process, and writing nothing.
            pending_captures_check(captures),
            skills_check(skills),
            tool_check("shadow.py --dry-run", HEALTH, ["bin/shadow.py", "--dry-run"],
                       run, real_root, DEFAULT_TIMEOUT, ("would write", "DRY RUN")),
            tool_check("airlock.py --gate %s" % date, GATE,
                       ["bin/airlock.py", "--gate", date], run, real_root,
                       DEFAULT_TIMEOUT, ("group(s):", "gate "),
                       hint="this judges ONE day. Before that day's first sensing capture "
                            "lands it fails legitimately — check the time before reading "
                            "it as a regression."),
            tool_check("shadow.py --gate --days 1", GATE,
                       ["bin/shadow.py", "--gate", "--days", "1"], run, real_root,
                       DEFAULT_TIMEOUT, ("criteria", "gate "),
                       hint="the Phase 4 go-live gate; unmet until a shadow day has been "
                            "run and its proposals reviewed (see docs/SHADOW_AGENT.md)"),
        ]

        if args.sandbox:
            checks.append(sandbox_check())
        else:
            checks.append(Check("sandbox containment", HEALTH, SKIPPED,
                                "pass --sandbox to re-measure it; three model calls, so "
                                "it is not free",
                                hint="the containment was last measured against %s — see "
                                     "docs/SANDBOX_MEASUREMENT.md"
                                     % sandbox_measured_version()))

        if args.tests:
            checks.append(tool_check("run_tests.py", HEALTH, ["run_tests.py"], run,
                                     real_root, TESTS_TIMEOUT,
                                     detail_fn=tests_detail))
        else:
            checks.append(Check("run_tests.py", HEALTH, SKIPPED,
                                "pass --tests to run it; a check that runs the suite "
                                "can hang"))

        print(render(checks, args.verbose), file=out, end="")
        code, footer = summarize(checks, args.strict)
        print(footer, file=out, end="")
        return code
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
