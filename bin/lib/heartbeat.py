"""One committed event per machine per HOUR: "this machine is still here."

**Why this exists.** A sweep that finds nothing dirty commits *nothing at all*, and both
`SYNC.md` and `~/dev/.lodestar/sync-status.json` are machine-local by deliberate design —
decision D-D, because a committed per-machine status file would make two machines overwrite
each other and churn a commit every two minutes. The consequence, named in
`docs/LAPTOP_PARITY.md` §4 as the one *genuine* hole rather than a correctly-paid cost: the
Desktop cannot tell **"the Laptop is off"** from **"the Laptop is sweeping perfectly and
finding nothing dirty."** This module makes silence mean silence, using the smallest
committed artifact that can carry the fact.

**Daily was not enough.** A once-a-day heartbeat proves "alive today", nothing more — it
cannot distinguish "swept five minutes ago" from "swept once right after midnight and has
been dark for twenty hours since". Lukas hit exactly that ambiguity misdiagnosing a machine
twice, which is why the cadence moved from once a day to once an hour: ~24 small files a day
per machine instead of one, so the freshest heartbeat can actually answer "did it sweep
recently" rather than only "did it sweep at all today".

**What it writes.** `journal/<date>/<machine-key>-<hour>.heartbeat` — a few hundred bytes of
JSON, `<hour>` a zero-padded local hour `"00"`-`"23"`. Four properties, each load-bearing:

1. **One file per event, and the *path* is the at-most-once key.** Two machines write two
   different filenames (the key), so git merges them without a conflict exactly like every
   other journal event (D5). The same machine in the same hour resolves to the same path,
   which makes "at most one per machine per hour" structural rather than hopeful — the key
   extends from `<machine-key>` to `<machine-key>-<hour>` for exactly this reason. That cap
   still matters: the sweep fires every ~127 s, so an unguarded heartbeat would be ~680 files
   a day, drowning the journal and making D13's corollary — non-blocking is only acceptable
   if the report is *seen* — fail by sheer volume. Hourly is a genuine cap of ~24 a day, not
   merely "more often".

2. **It is deliberately NOT a `journal.load()` event, and that is the design decision here.**
   `journal.load()` reads `*.json`; a `.heartbeat` file is invisible to it. That is on
   purpose. `BOARD.md`'s header is literally `As of <newest journal event>` (`lib/render.py`)
   and nothing re-renders the board from the launchd sweep — so a heartbeat that *was* a
   loadable event would silently make `BOARD.md` stale, break `board.py --check`, and break
   `check.py`'s byte-for-byte `--rebuild` round-trip. It would also have to carry a `loop` id
   into `loops.derive`, which would either invent a `loops/<id>.md` that no human opened or
   falsely animate a real loop's `last_touch` from a machine that touched nothing. Liveness is
   a fact about a **machine**, not about a loop, so it lives *beside* the loop-shaped log
   rather than inside it — same directory, same append-only discipline, same
   conflict-freedom, zero effect on derived state.

3. **Age always comes from the event's own `ts`, never from file mtime.** A `git checkout` —
   or the fresh clone this whole feature exists to observe — resets mtimes, and an
   mtime-based age would then read as confidently current. Same rule the rest of the STATUS
   surface follows.

4. **Absence reads as absence.** A machine listed in `machines.md` with no heartbeat inside
   `SILENT_AFTER_SECONDS` is reported `silent`, never blank and never OK. That is the entire
   point; a reader that renders "no news" as health would reproduce the bug this closes.

Writing is best-effort by construction (`emit` returns `None` and never raises): per D13 the
sweep logs and continues, and a heartbeat that could not be written must not take twenty-four
repos' worth of syncing down with it. The *absence* is what surfaces, in the STATUS row.

**The honest cost of the hourly cadence.** ~24 files/machine/day × 2 machines is ~17,500
files a year, and `journal/` has **no retention or pruning of any kind** — `journal.load()`
(`lib/journal.py`) walks every day directory on every call and nothing ever removes an old
one. That was a tolerable cost at one file/machine/day; at 24x it is the same shape of cost,
24x larger, and still unaddressed. This module does not build pruning — it only refuses to
hide the number. If the day comes that `journal.load()`'s directory walk or git's own
per-file overhead becomes the bottleneck, retention is the fix that has been deferred, not one
that was never needed.
"""

import json
import os
from typing import Any, Dict, List, Optional

from . import clock, machine, paths

# Not `.json`: that is exactly what keeps this out of `journal.load()`. See §2 above.
SUFFIX = ".heartbeat"

# The longest HEALTHY gap between two heartbeats from the same machine, plus grace — same
# convention `lib/status.py`'s STALE_AFTER table uses. `due()` guards emission to once per
# local hour, so a machine that is genuinely up produces a fresh heartbeat at least once every
# ~60 minutes: whichever sweep first notices the hour has rolled over, give or take the ~127 s
# sweep cadence. One whole missed hour is real grace for a single hiccup — a laptop asleep for
# a few minutes across the boundary, one failed write, a slow git operation blocking the sweep
# — so it must not read as silence. A SECOND missed hour is not a hiccup: past 2 h, "the
# sweep is not running" is the likelier story than "between ticks", which is exactly the
# sharper, hour-scale answer this module exists to give over the old calendar-day threshold
# (a machine could be legitimately quiet for 20+ hours in a day and still be perfectly
# healthy — that ambiguity is the bug this cadence change closes).
SILENT_AFTER_SECONDS = 2 * 3600

SCHEMA = 2


def day_dir(date: Optional[str] = None) -> str:
    return os.path.join(paths.journal_dir(), date or clock.today())


def hour_key() -> str:
    """The current local hour, zero-padded `"00"`-`"23"` — the extension to the at-most-once
    key that makes the cap hourly rather than daily. No arguments, same idiom as
    `clock.today()`; pass an explicit `hour` to `path_for`/`due` to address a different slot
    without waiting on the wall clock.
    """
    return "%02d" % clock.parse(clock.now()).hour


def path_for(machine_key: str, date: Optional[str] = None, hour: Optional[str] = None) -> str:
    """Where this machine's heartbeat for this hour lives. Deterministic, hence at-most-once.

    The filename carries the machine key (so two machines can never collide) AND the hour
    (so the same machine gets a fresh slot every hour instead of one per day) — module
    docstring §1. `hour` defaults to the current local hour.
    """
    return os.path.join(day_dir(date), "%s-%s%s" % (machine_key, hour or hour_key(), SUFFIX))


def due(machine_key: Optional[str] = None, date: Optional[str] = None,
        hour: Optional[str] = None) -> bool:
    """Has this machine not yet spoken this hour? Checked before every write."""
    return not os.path.exists(path_for(machine_key or machine.key(), date, hour))


def emit(repos: Optional[int] = None, flagged: Optional[int] = None,
         machines_path: Optional[str] = None) -> Optional[str]:
    """Write this hour's heartbeat for this machine, or return None if one already exists.

    `repos` and `flagged` are the shape of the sweep that emitted it — enough to tell a
    healthy sweep from one drowning in flags without duplicating `SYNC.md`, which owns the
    per-repo detail and is machine-local anyway.

    Never raises: an unregistered machine, an unwritable journal, and a full disk all return
    None. The sweep must not die of a bookkeeping file (D13).
    """
    try:
        key = machine.key()
        # One `now` for `ts`, `date`, and `hour` so all three describe the same instant —
        # two separate clock reads could straddle an hour or midnight boundary and disagree.
        ts = clock.now(timespec="microseconds")
        moment = clock.parse(ts)
        date = moment.date().isoformat()
        hour = "%02d" % moment.hour
        record = {
            "event": "heartbeat",
            "schema": SCHEMA,
            "ts": ts,
            # The role, as in every journal event; the key too, because the role is what a
            # human reads and the key is what identifies the box in machines.md.
            "machine": machine.role(machines_path),
            "machine_key": key,
            "runtime": machine.runtime(),
            "date": date,
            "hour": hour,
        }  # type: Dict[str, Any]
        if repos is not None:
            record["repos"] = int(repos)
        if flagged is not None:
            record["flagged"] = int(flagged)

        path = path_for(key, date, hour)
        if os.path.exists(path):
            return None

        paths.ensure_dir(os.path.dirname(path))
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
        return path
    except (OSError, ValueError, machine.UnknownMachine):
        return None


def _read(path: str, day: str) -> Optional[Dict[str, Any]]:
    """One heartbeat file, or None if it cannot be trusted.

    A corrupt file reads as *silence* rather than as a crash or as health: we cannot vouch
    for it, and the machine then surfaces as silent, which is the honest outcome.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    record = dict(record)
    if "machine_key" not in record:
        stem = os.path.basename(path)[:-len(SUFFIX)]
        # Strip a trailing "-HH" hour suffix if present, so a record that (unusually) has no
        # machine_key of its own still resolves to the machine name rather than "name-14".
        head, _, tail = stem.rpartition("-")
        record["machine_key"] = head if head and len(tail) == 2 and tail.isdigit() else stem
    record.setdefault("date", str(record.get("ts") or day)[:10])
    record["_path"] = path
    return record


def last_seen() -> Dict[str, Dict[str, Any]]:
    """The newest readable heartbeat per machine key, across every day directory."""
    newest = {}  # type: Dict[str, Dict[str, Any]]
    root = paths.journal_dir()
    if not os.path.isdir(root):
        return newest
    try:
        days = sorted(os.listdir(root))
    except OSError:
        return newest
    for day in days:
        directory = os.path.join(root, day)
        if not os.path.isdir(directory):
            continue
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            if not name.endswith(SUFFIX):
                continue
            record = _read(os.path.join(directory, name), day)
            if record is None:
                continue
            key = str(record["machine_key"])
            current = newest.get(key)
            if current is None or clock.sort_key(record.get("ts")) >= \
                    clock.sort_key(current.get("ts")):
                newest[key] = record
    return newest


def _age(timestamp: Optional[str]) -> Optional[float]:
    if not timestamp:
        return None
    try:
        then = clock.parse(str(timestamp))
    except (ValueError, TypeError):
        return None
    return (clock.parse(clock.now()) - then).total_seconds()


def machines(machines_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """One row per machine: who it is, when it last spoke, and whether that is a problem.

    Every machine in `machines.md` gets a row whether or not it has ever spoken — a machine
    dropped from the report for lack of data is the failure mode this module exists to end.
    A heartbeat from a key that is *not* in `machines.md` also gets a row (`known: False`),
    because a surprising row is better than a silently swallowed machine.

    `silent` is age-based, not calendar-based: `age_seconds is None or age_seconds >
    SILENT_AFTER_SECONDS`. That is the point of the hourly cadence — "quiet since midnight"
    is no longer conflated with "quiet for the last two hours"; only the second is reported.
    Unknown age (no heartbeat ever, or one that cannot be trusted) always reads `silent`,
    never blank and never OK — the property the module docstring's §4 names.
    """
    try:
        table = machine.roles(machines_path)
    except (OSError, ValueError):
        table = {}
    seen = last_seen()

    rows = []  # type: List[Dict[str, Any]]
    for key in set(table) | set(seen):
        record = seen.get(key) or {}
        as_of = record.get("ts")
        age = _age(as_of)
        rows.append({
            "key": key,
            "role": table.get(key) or str(record.get("machine") or "?"),
            "known": key in table,
            "as_of": as_of,
            "age_seconds": age,
            # Unknown age never reads as current — same rule as lib/status._source.
            "silent": age is None or age > SILENT_AFTER_SECONDS,
            "runtime": record.get("runtime"),
            "repos": record.get("repos"),
            "flagged": record.get("flagged"),
        })

    # Role first so the Desktop's own row leads and a human reads names, not keys; key breaks
    # ties so the order is reproducible on both machines.
    rows.sort(key=lambda row: (row["role"], row["key"]))
    return rows


def facts(machines_path: Optional[str] = None) -> Dict[str, Any]:
    """The whole MACHINES block, JSON-ready, for `lib/status.py` to render.

    `installed` is False until *some* machine has written a heartbeat, because "never
    written" and "written and old" are different facts and only the second is a warning
    (lib/status.py property 2). `age_seconds` is the freshest machine's — it answers "can
    this surface see anything at all?", while per-machine silence lives in the rows.
    """
    rows = machines(machines_path)
    fresh = [row["key"] for row in rows if not row["silent"]]
    silent = [row["key"] for row in rows if row["silent"]]
    ages = [row["age_seconds"] for row in rows if row["age_seconds"] is not None]
    freshest = min(ages) if ages else None
    installed = any(row["as_of"] for row in rows)

    if not installed:
        summary = "%d machine(s) known · no heartbeat has ever been written" % len(rows)
    else:
        summary = "%d known · %d fresh · %s" % (
            len(rows), len(fresh),
            "%d SILENT" % len(silent) if silent else "0 silent")

    return {
        "installed": installed,
        "today": clock.today(),
        "threshold_seconds": SILENT_AFTER_SECONDS,
        "age_seconds": freshest,
        "stale": installed and (freshest is None or freshest > SILENT_AFTER_SECONDS),
        "known": len(rows),
        "fresh": fresh,
        "silent": silent,
        "summary": summary,
        "machines": rows,
    }


def write_line(path: Optional[str] = None,
               machines_path: Optional[str] = None) -> Optional[str]:
    """Pre-digest `facts()` into one line a shell statusline can read with builtins.

    Format, space-separated: `<schema> <epoch> <fresh> <silent>`. Epoch rather than ISO
    because the reader is `read` plus shell arithmetic, and a date parse in POSIX shell is a
    fork this cannot afford (see `paths.heartbeat_line_file`).

    **Written on EVERY non-dry sweep, unlike `emit()`.** `emit()` is at-most-once per hour by
    design, so hooking this to its success would leave the line an hour stale at worst — and
    the age of this line is precisely what tells a reader the sweep is still running. Its
    freshness is the measurement, so it must be re-stamped whenever a sweep completes even
    when the hour's heartbeat already exists.

    Never raises, for the same reason `emit()` does not: the sweep must not die of a
    bookkeeping file (D13). Returns the path written, or None.
    """
    try:
        data = facts(machines_path)
        target = path or paths.heartbeat_line_file()
        # The sweep that is writing this line IS this machine being alive, so the stamp is
        # now — not the freshest heartbeat's `as_of`, which can be up to an hour old and
        # would make a healthy machine read as lagging by its own clock.
        epoch = int(clock.parse(clock.now()).timestamp())
        line = "%d %d %d %d\n" % (SCHEMA, epoch,
                                  len(data["fresh"]), len(data["silent"]))
        paths.ensure_dir(os.path.dirname(target))
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(line)
        # Atomic, so a statusline reading every 10 s can never catch a half-written line.
        os.replace(tmp, target)
        return target
    except Exception:
        return None
