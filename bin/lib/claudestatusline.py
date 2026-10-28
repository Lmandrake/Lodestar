"""Render `~/.claude/statusline.sh` per machine, from `claude/statusline/` in this repo.

**Why this exists.** The status line was unmanaged and the three machines had drifted into three
*unrelated* scripts — one machine running a vendor example script, another an independently
hand-written one, a third none at all, sharing 25 non-comment lines of trivia. Anything added to
one reached exactly that machine for ever, because `claudeconf.SNAPSHOT_FILES` copies this file
live→repo **only** and nothing ever writes it back.

**Two halves, and the split is deliberate.** `render(key)` is a pure function from a machine key
to text — it reads only this repo, writes nothing, and can be eyeballed for any machine from any
machine. `converge(...)` is the half that touches `~/.claude`, and it is the only thing here that
writes. Keeping them apart is what made it possible to ship and inspect the composition for all
three machines before anything live was at risk.

🔴 **`provenance_path` must NEVER be `claude/snapshot/<key>/statusline.sh`.** That path is in
`claudeconf.SNAPSHOT_FILES`, and `sync.claude_config_step` runs `snapshot_all()` *before* any
converge — so the snapshot taken seconds earlier would make `live == previous` unconditionally,
every sweep would read as a clean fast-forward, and the first run on any machine would overwrite a
hand-written script with **no capture and no flag**. `claudeconf.py`'s own comment makes the same
point from the other side: CLAUDE.md can safely use the plain path precisely because it is
deliberately *absent* from `SNAPSHOT_FILES`. Provenance here is `statusline.rendered.sh`, mirroring
`settings.rendered.json`, and `tests/` pins it structurally rather than by comment.

🔴 **Byte-determinism is a hard requirement, not a nicety.** The converger writes only when the
render differs from what it last wrote, which is how the sweep reaches quiescence. Anything
time-varying in the output — a timestamp, a duration, a hash of something mutable — would make
every sweep see a difference, rewrite the file, and commit, for ever. So: no clock reads, blocks
composed in sorted-filename order, exactly one trailing newline per block. `tests/` pins it.

**Composition.** `base.sh` carries `# @@SLOT: <name> @@` markers. Each `blocks/NN-*.sh` declares
`# applies:` (a `machine.selector_matches` selector) and `# slot:`. Applicable blocks replace
their slot marker in filename order. The base names no block, so adding one does not touch it.
`claude/statusline/README.md` is the contract.

**Faults are reported, never silently dropped** — the convention `registry` and `claudeskills`
already follow. A block with no `slot:`, an unknown slot, or an `applies:` token that names
neither a machine key nor a label is a fault; `render` raises rather than quietly emitting a
script with a piece missing, because a status line that is silently short a segment is exactly
the invisible-state defect this repo keeps rediscovering.
"""

import hashlib
import json
import os
import re
import stat
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

from . import clock, machine, paths

# The slots `base.sh` offers. A block may not invent one: an unknown slot means the block would
# never be emitted anywhere, which must be a loud fault rather than a silent omission.
PRELUDE = "prelude"
COMPUTE = "compute"
SLOTS = (PRELUDE, COMPUTE)

SLOT_MARKER = re.compile(r"^# @@SLOT:\s*(\w+)\s*@@\s*$", re.MULTILINE)

_HEADER = re.compile(r"^#\s*(applies|slot)\s*:\s*(.*?)\s*$", re.MULTILINE)

BASE_NAME = "base.sh"
BLOCKS_DIR = "blocks"

# Which machines the SWEEP may converge. A selector file rather than an environment variable on
# purpose: enabling a machine is then a committed, reviewable decision that reaches the fleet like
# any other source, instead of a machine-local export that no other machine can see and no report
# mentions. That is the same argument `claude/skills.md` makes — a hand-install reaches one
# machine and nothing ever tells you.
#
# Absent or empty means NO machine, deliberately. This lands inert and is flipped on one box at a
# time, the always-on desktop first, the WSL box last.
ENABLED_NAME = "ENABLED"


class Block(NamedTuple):
    """One composable fragment: its filename, its selector, its slot and its body."""
    name: str
    applies: str
    slot: str
    body: str


class StatuslineFault(Exception):
    """A source file is malformed. Raised rather than returning a short script."""


def sources_dir(root: Optional[str] = None) -> str:
    """Where the base and blocks live. Inside the repo, so they sync like any other source."""
    return os.path.join(root or paths.claude_dir(), "statusline")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def load_blocks(directory: Optional[str] = None) -> List[Block]:
    """Every block, in composition order — sorted by filename, which is why they carry `NN-`.

    Sorted rather than globbed-as-returned because directory order is filesystem-dependent and
    would make the rendered bytes differ between machines for no reason.
    """
    where = os.path.join(directory or sources_dir(), BLOCKS_DIR)
    if not os.path.isdir(where):
        return []
    found = []
    for name in sorted(os.listdir(where)):
        if not name.endswith(".sh"):
            continue
        text = _read(os.path.join(where, name))
        header = {key: value for key, value in _HEADER.findall(text)}
        slot = header.get("slot", "").strip()
        if not slot:
            raise StatuslineFault(
                "%s declares no `# slot:` — it would never be emitted anywhere" % name)
        if slot not in SLOTS:
            raise StatuslineFault(
                "%s declares slot %r, which base.sh does not offer (have: %s)"
                % (name, slot, ", ".join(SLOTS)))
        # An absent `applies:` means every machine. That is the safe direction and the same one
        # `machine.labels` argues for: a block reaching one machine too many is visible, while a
        # block silently reaching none is not.
        found.append(Block(name=name,
                           applies=header.get("applies", "").strip() or "all",
                           slot=slot,
                           body=text))
    return found


def unknown_tokens(blocks: Sequence[Block],
                   machines_path: Optional[str] = None) -> Dict[str, List[str]]:
    """Block name -> the `applies:` tokens that name neither a machine key nor a label.

    Separate from `load_blocks` because it needs `machines.md`, and a caller that only wants to
    read the sources should not have to have a machine table. Same reasoning as
    `validate._registry_machine_tokens` being distinct from the registry parser — and note that
    this repo has already been bitten by deriving a validator from the wrong consumer, so this
    one is derived from `machine.selector_matches`, which is what actually reads these tokens.
    """
    known = machine.known_selector_tokens(machines_path)
    out = {}
    for block in blocks:
        bad = [token
               for alternative in block.applies.split(",")
               for token in [part.strip().lower() for part in alternative.split("+")]
               if token and token != "all" and token not in known]
        if bad:
            out[block.name] = bad
    return out


def applicable(blocks: Sequence[Block], key: str,
               machines_path: Optional[str] = None) -> List[Block]:
    """The subset of `blocks` this machine should receive, in composition order."""
    these = machine.labels(key, machines_path)
    return [block for block in blocks
            if machine.selector_matches(block.applies, key, these)]


def banner(key: str) -> str:
    """The header written into every rendered script.

    Deliberately carries NO timestamp and no version. A generated-at line would differ on every
    sweep, so the converger would rewrite and commit the file for ever and the board would never
    be quiescent — the same trap `lib/render.py` documents for `BOARD.md`, arriving here.

    It names the kill switch on purpose: a hand edit makes live differ from what was last
    written, which the converger reads as DRIFTED and refuses to overwrite. On a machine with no
    network path from any Mac, that is the only lever a human has.
    """
    return (
        "# ===========================================================================\n"
        "# GENERATED by Lodestar for machine `%s` — edits here are a KILL SWITCH.\n"
        "#\n"
        "# Source: ~/dev/Lodestar/claude/statusline/ (base.sh + blocks/). Change it there\n"
        "# and the hourly sweep re-renders every machine within the hour.\n"
        "#\n"
        "# Editing THIS file by hand is safe and deliberate: the sweep compares it against\n"
        "# what it last wrote, so your edit reads as drift. It captures a copy into\n"
        "# claude/adopt/%s/ and REFUSES to overwrite you, every sweep, until a human\n"
        "# classifies the capture. Use that if a render is wrong on a machine you cannot\n"
        "# easily reach.\n"
        "# ===========================================================================\n"
    ) % (key, key)


def render(key: str,
           directory: Optional[str] = None,
           machines_path: Optional[str] = None) -> str:
    """The full text of `key`'s `~/.claude/statusline.sh`.

    Raises `StatuslineFault` for a malformed source rather than emitting a script with a piece
    missing. A short status line looks like a working one, which is the whole family of bug this
    repo keeps paying for.
    """
    where = directory or sources_dir()
    base_path = os.path.join(where, BASE_NAME)
    if not os.path.exists(base_path):
        raise StatuslineFault("no %s in %s" % (BASE_NAME, where))
    base = _read(base_path)

    blocks = load_blocks(where)
    bad = unknown_tokens(blocks, machines_path)
    if bad:
        raise StatuslineFault(
            "unknown `applies:` token(s): "
            + "; ".join("%s -> %s" % (name, ", ".join(tokens))
                        for name, tokens in sorted(bad.items())))

    offered = set(SLOT_MARKER.findall(base))
    missing = [block.name for block in blocks if block.slot not in offered]
    if missing:
        raise StatuslineFault(
            "%s has no marker for the slot these blocks declare: %s"
            % (BASE_NAME, ", ".join(sorted(missing))))

    mine = applicable(blocks, key, machines_path)
    by_slot = {slot: [block for block in mine if block.slot == slot] for slot in SLOTS}

    def _fill(match):
        slot = match.group(1)
        chosen = by_slot.get(slot, [])
        if not chosen:
            # Keep a comment rather than nothing, so reading the rendered file tells you a slot
            # exists and simply had no blocks for this machine — an empty gap reads as a bug.
            return "# (no %s blocks for this machine)" % slot
        return "\n".join(block.body.rstrip("\n") for block in chosen)

    body = SLOT_MARKER.sub(_fill, base)

    # The shebang must stay the first line, so the banner goes after it rather than on top.
    lines = body.split("\n", 1)
    if lines[0].startswith("#!"):
        return lines[0] + "\n" + banner(key) + (lines[1] if len(lines) > 1 else "")
    return banner(key) + body


# ---------------------------------------------------------------------------------------------
# The converger. Everything below here writes; everything above it does not.
# ---------------------------------------------------------------------------------------------

# Same strings the other two convergers return, so `sync.claude_config_step` can treat all three
# alike. Defined here rather than imported from `claudeconf`, which imports THIS module — the
# import would be circular, and duplicating four short string constants is the cheaper of the two
# evils. `tests/` asserts they still match claudeconf's.
CURRENT = "current"
FAST_FORWARD = "fast_forward"
DRIFTED = "drifted"
BLOCKED = "blocked"

LIVE_STATUSLINE = os.path.expanduser("~/.claude/statusline.sh")

# rwxr-xr-x. A status line that is not executable fails SILENTLY — Claude Code shows nothing at
# all, which reads as a broken render rather than a broken permission. Measured 2026-10-01: an
# atomic tmp-then-`os.replace` write creates a fresh inode at the process umask, so the execute
# bit is lost on every write unless it is set explicitly afterwards.
SCRIPT_MODE = (stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)


def enabled_selector(directory: Optional[str] = None) -> str:
    """The selector naming which machines the sweep may converge. "" means none.

    Comment lines (`#`) and blanks are ignored, so the file can explain itself. Several lines are
    joined with commas, which `machine.selector_matches` reads as any-of — so one key per line is
    the natural way to roll out machine by machine.
    """
    path = os.path.join(directory or sources_dir(), ENABLED_NAME)
    text = _read_text(path)
    if text is None:
        return ""
    tokens = [line.split("#", 1)[0].strip() for line in text.splitlines()]
    return ",".join(token for token in tokens if token)


def is_enabled(key: Optional[str] = None,
               directory: Optional[str] = None,
               machines_path: Optional[str] = None) -> bool:
    """Should the sweep converge this machine's status line?

    Checked by the SWEEP, not by `converge` — so `converge` stays callable by hand and by tests
    without a gate in the way, and the gate has exactly one consumer that is easy to find.
    """
    selector = enabled_selector(directory)
    if not selector:
        return False
    key = (key or machine.key()).lower()
    return machine.selector_matches(selector, key, machine.labels(key, machines_path))


def snapshot_dir(key: str) -> str:
    return os.path.join(paths.claude_dir(), "snapshot", key)


def provenance_path(key: str) -> str:
    """The rendered script as of the last moment this machine and the repo agreed.

    🔴 **`statusline.rendered.sh`, never `statusline.sh`** — see the module docstring. The name
    mirrors `claudesettings.provenance_path`'s `settings.rendered.json`, which exists for exactly
    this reason: `snapshot/<key>/settings.json` is an unconditional copy-in, so using it as
    provenance would launder a drifted write into agreement.
    """
    return os.path.join(snapshot_dir(key), "statusline.rendered.sh")


def adopt_dir(key: str) -> str:
    return paths.claude_adopt_dir(key)


def receipt_path(key: str) -> str:
    """A tiny JSON record of what this machine's converge last did.

    🔴 **This exists because the rendered script itself CANNOT travel.** `gitutil.is_source`
    withholds every `.sh` from the sweep's auto-commit, and both `snapshot/<key>/statusline.sh`
    and `snapshot/<key>/statusline.rendered.sh` end in `.sh` — so on a machine with no human to
    commit them by hand they sit uncommitted for ever. a headless WSL box is exactly that machine, and no
    Mac has a network path to it, so without this there is no way to learn what it rendered or
    whether the converge even succeeded.

    `.json` is not a source suffix, so this one file rides the ordinary sweep.

    It deliberately carries NO timestamp. It is rewritten every sweep, so a clock read would
    change the bytes every hour, defeat `_write_text`'s skip, and commit a file on every pass for
    ever. Freshness is already answered by `lib/heartbeat`; the git commit that carries this is
    timestamped by git. Same trap as `banner()`, arriving by a second route.
    """
    return os.path.join(snapshot_dir(key), "statusline-receipt.json")


def write_receipt(key: str, outcome: str, detail: str,
                  rendered: Optional[str] = None,
                  blocks: Optional[Sequence[Block]] = None) -> bool:
    """Record the outcome so another machine can read it. True if the bytes changed.

    Written for EVERY outcome including BLOCKED, because a machine that cannot render is
    precisely the one whose state must be visible from elsewhere.
    """
    payload = {
        "schema": 1,
        "machine": key,
        "outcome": outcome,
        "detail": detail,
        # UTF-8 byte length, NOT len(rendered). The script contains block glyphs and an
        # ellipsis, so a character count differs from the on-disk size by ~14 — and a field
        # called "bytes" that a reader cannot compare against `ls -l` or `git cat-file -s` is
        # a number nobody can mean. Caught 2026-10-01 comparing against the Laptop's push.
        "render_bytes": len(rendered.encode("utf-8")) if rendered is not None else None,
        "render_sha256": (hashlib.sha256(rendered.encode("utf-8")).hexdigest()
                          if rendered is not None else None),
        # Which blocks this machine actually composed. The whole point of the selector machinery
        # is that this differs per machine, so it is the first thing to check when a render looks
        # wrong — and it is how "no private content reached the home box" is confirmable from here.
        "blocks": [block.name for block in blocks] if blocks is not None else None,
    }
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    return _write_text(receipt_path(key), text)


def _stamp() -> str:
    """Filename-safe, sortable, second precision — the shape claudeconf's captures use.

    The UTC offset is dropped rather than sanitised. Keeping it meant stripping `:` and `-` from
    `2026-10-01T10:30:00-07:00` and ending with `...1030000700`, where the offset's digits run
    into the time and its sign is gone — unsortable and unreadable. These filenames only ever need
    to sort against each other on one machine, which local time already does.
    """
    return clock.now(timespec="seconds")[:19].replace(":", "").replace("-", "")


def _read_text(path: str) -> Optional[str]:
    """None means absent or unreadable. Both are 'we have nothing to compare against'."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return None


def _write_text(path: str, text: str, executable: bool = False) -> bool:
    """Atomic write, skipped entirely when the content already matches. True if it wrote.

    The skip is a quiescence requirement, not an optimization: this runs on every sweep, and a
    file whose mtime is re-stamped every time is how a repo stops being quiescent and therefore
    stops auto-committing. Same reasoning as `claudeconf._write`.
    """
    existing = _read_text(path)
    if existing == text:
        # Still assert the mode, even on a content match. A previous write by an older version —
        # or a hand `cp` — can leave the right bytes without the execute bit.
        if executable and os.path.exists(path):
            if stat.S_IMODE(os.stat(path).st_mode) != SCRIPT_MODE:
                os.chmod(path, SCRIPT_MODE)
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    if executable:
        os.chmod(tmp, SCRIPT_MODE)
    os.replace(tmp, path)
    return True


def newest_capture(key: str) -> Optional[str]:
    """The most recent captured statusline for this machine, or None.

    Used to make capturing idempotent: a machine that drifts and then sits there must not grow a
    capture per sweep. Mirrors claudeconf's I2.
    """
    where = adopt_dir(key)
    if not os.path.isdir(where):
        return None
    names = sorted(name for name in os.listdir(where) if name.endswith("-statusline.sh"))
    if not names:
        return None
    return _read_text(os.path.join(where, names[-1]))


def capture(live: str, key: str) -> Tuple[Optional[str], str]:
    """Save the live script verbatim, awaiting a human. Idempotent. Returns (path_or_None, detail).

    The filename suffix is `-statusline.sh` so these never collide with the `-CLAUDE.md` captures
    already queued in the same per-machine directory.
    """
    if newest_capture(key) == live:
        return None, "unchanged since the last capture"
    target = os.path.join(adopt_dir(key), "%s-statusline.sh" % _stamp())
    _write_text(target, live)
    return target, "captured %s" % os.path.basename(target)


def adopt_live(key: Optional[str] = None,
               live_path: Optional[str] = None,
               directory: Optional[str] = None,
               machines_path: Optional[str] = None,
               dry_run: bool = False) -> Tuple[bool, str]:
    """Accept whatever is live NOW as our starting point, so the next sweep may replace it.

    **Why this is a separate, explicit act and not something `converge` does for itself.** On a
    machine with no provenance, a live script differs from both the render and from nothing — so
    `converge` returns DRIFTED and refuses, which is correct: it cannot know whether that file is
    an old Lodestar render, a hand-written script someone depends on, or a vendor's example. Both
    other convergers behave identically, and `claudesettings`' own test is named
    `test_the_first_run_drifts_and_captures_rather_than_overwriting`.

    Adopting writes provenance equal to the CURRENT live bytes. That is the sentence "yes, this
    file is ours to replace" — and the very next converge will therefore fast-forward and
    overwrite it. So this captures the existing script into `claude/adopt/<key>/` first,
    unconditionally, even though nothing has gone wrong: the capture is the only copy of a script
    that is about to be replaced on purpose.

    Returns (did_something, detail). Never writes under `dry_run`.
    """
    key = (key or machine.key()).lower()
    live_path = live_path or LIVE_STATUSLINE
    live = _read_text(live_path)
    if live is None:
        return False, ("nothing to adopt: %s does not exist, so converge will simply write it"
                       % live_path)
    try:
        expected = render(key, directory=directory, machines_path=machines_path)
    except StatuslineFault as fault:
        return False, "refusing to adopt: sources are malformed: %s" % fault
    if live == expected:
        return False, "nothing to adopt: the live file already equals the render"
    if dry_run:
        return False, ("would adopt %d bytes as provenance; the next converge would then "
                       "replace them with the %d-byte render" % (len(live), len(expected)))
    where, _detail = capture(live, key)
    _write_text(provenance_path(key), live)
    return True, ("adopted; the existing script is kept at %s and the next converge will "
                  "replace the live file with the render"
                  % (os.path.basename(where) if where else "an existing capture"))


def converge(dry_run: bool = False,
             key: Optional[str] = None,
             live_path: Optional[str] = None,
             directory: Optional[str] = None,
             machines_path: Optional[str] = None) -> Tuple[str, str]:
    """Reconcile this machine's `~/.claude/statusline.sh` with the repo. NEVER raises.

    `key`, `live_path`, `directory` and `machines_path` exist so this is testable at all —
    without them a test writes the real status line on the machine running the suite. All four
    default to the production choice. Untestable code stays untested, and untested writing code
    is where this repo's inert features hide.

    The blanket `except Exception` is the point, not laziness. `sync.claude_config_step` runs the
    skills and settings convergers *after* this one, so an unexpected failure here must degrade to
    one flagged row rather than taking down the rest of config convergence on a machine nobody is
    looking at.

    Outcomes:
      CURRENT      live already equals the render. Nothing to do.
      FAST_FORWARD live is absent, or still exactly what we last wrote. Safe to write.
      DRIFTED      live differs from both. Capture it and REFUSE — never clobber a hand edit.
      BLOCKED      the sources are malformed, or something unexpected went wrong.
    """
    try:
        return _converge(dry_run, key, live_path, directory, machines_path)
    except Exception as error:  # noqa: BLE001 — see the docstring
        detail = "statusline converge failed: %s: %s" % (type(error).__name__, error)
        # An unexpected failure is the single most important thing to make visible elsewhere, and
        # the receipt is the only channel that reaches another machine. Guarded in turn, because a
        # failure to record a failure must not escalate into a raise out of a never-raises function.
        if not dry_run:
            try:
                write_receipt((key or machine.key()).lower(), BLOCKED, detail)
            except Exception:
                pass
        return BLOCKED, detail


def _converge(dry_run, key, live_path, directory, machines_path):
    key = (key or machine.key()).lower()
    live_path = live_path or LIVE_STATUSLINE

    def finish(outcome, detail, rendered=None, blocks=None):
        """Record the outcome where another machine can read it, then return it.

        Every exit goes through here, including BLOCKED. The rendered script itself cannot
        travel — `gitutil.is_source` withholds every `.sh` from the sweep — so this receipt is
        the ONLY thing that reaches another machine, and a box that fails to render is the one
        that most needs to be visible.
        """
        if not dry_run:
            try:
                write_receipt(key, outcome, detail, rendered=rendered, blocks=blocks)
            except Exception:
                # A bookkeeping file must never change the outcome of the act it describes.
                pass
        return outcome, detail

    try:
        expected = render(key, directory=directory, machines_path=machines_path)
        mine = applicable(load_blocks(directory), key, machines_path)
    except StatuslineFault as fault:
        return finish(BLOCKED, "sources are malformed: %s" % fault)

    live = _read_text(live_path)
    previous = _read_text(provenance_path(key))

    if live == expected:
        if not dry_run:
            # Record agreement so a later hand edit is detectable as drift, not as a first run.
            _write_text(provenance_path(key), expected)
            # And pass the live file through the same writer, which skips the content (it already
            # matches) but still asserts the execute bit. Without this the mode is only ever fixed
            # on a sweep that happens to change the content — so a file left non-executable by a
            # `cp`, or by an older version of this code, would stay broken for ever while every
            # sweep reported CURRENT. A status line that cannot execute shows nothing at all.
            _write_text(live_path, expected, executable=True)
        return finish(CURRENT, "statusline.sh matches the render", expected, mine)

    if live is None or live == previous:
        # `live is None` is the FRESH-MACHINE path, and it is a decision rather than an inherited
        # default. The two existing convergers disagree here: CLAUDE.md renders an absent live
        # file (`test_absent_live_file_is_rendered`), settings.json BLOCKS on one
        # (`test_a_missing_live_file_blocks`), and neither explains itself. This one follows
        # CLAUDE.md, because like CLAUDE.md it renders wholesale from repo-authoritative sources
        # rather than merging into whatever the user already had — and because the WSL box has no
        # statusline.sh at all, so blocking would mean that machine never gets one.
        if dry_run:
            return finish(FAST_FORWARD, ("would write statusline.sh (%s)"
                                         % ("absent" if live is None
                                            else "unchanged since we wrote it")))
        _write_text(live_path, expected, executable=True)
        _write_text(provenance_path(key), expected)
        return finish(FAST_FORWARD,
                      "wrote statusline.sh (%d bytes)" % len(expected.encode("utf-8")),
                      expected, mine)

    # DRIFTED. Provenance is deliberately NOT advanced — invariant I1 in the other two convergers.
    # Advancing it would make the next sweep read this hand edit as "what we last wrote" and
    # overwrite it, which is precisely the kill switch this design promises a human on a machine
    # with no network path from any Mac.
    if dry_run:
        return finish(DRIFTED,
                      "statusline.sh was edited outside Lodestar; would capture and refuse")
    _where, detail = capture(live, key)
    return finish(DRIFTED,
                  "statusline.sh was edited outside Lodestar; %s, refusing to overwrite" % detail,
                  expected, mine)
