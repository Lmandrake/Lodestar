"""Converge ~/.claude/settings.json across machines: repo-authoritative, live-as-floor.

Two functions live here. `differences()`/`report()` are the original report-only pair the
sweep already calls. Everything below them renders and converges the live file, on the same
provenance contract as `claudeconf.converge_claudemd`.

**Why a plain merge is unbuildable.** The requirement was to sync settings.json and to lean toward
the most permissive of the machines. If each machine independently wrote
`merge(mine, theirs)`, then any scalar the two disagree on gets rewritten by both of them,
forever, once per sweep (~127 s). Merging is not idempotent across a pair of writers. So the
repo holds the authority — a committed `claude/settings-base.json` of shared keys, plus an
optional per-machine `claude/settings-overlay/<key>.json` — and each machine renders
`live = live ⊕ base ⊕ overlay` from files it does not race anyone for. Same shape as
CLAUDE.md, same reason.

**Live is the floor; base is the authority.** Render never *deletes* a key that only the
live file has. Claude Code writes to settings.json itself — granting a permission at a
prompt appends to `permissions.allow`, and new releases add their own keys — so a render
that emitted only `base ⊕ overlay` would silently delete settings the user chose. Starting
from live and layering the repo on top means the worst case is "a machine keeps a setting
the fleet does not share", never "the user's grant vanished". The cost is that base cannot
*remove* anything; removal is a hand edit of the live file. That trade is deliberate:
"permissive" means do not lose capabilities.

**Provenance, and why it is NOT `snapshot/<key>/settings.json`.** Claude Code mutating the
live file is the CLAUDE.md problem again, so the same protection applies: we only overwrite
the live file when it still matches what we last wrote. But `claudeconf.SNAPSHOT_FILES`
already contains `settings.json` and copies live -> `claude/snapshot/<key>/settings.json`
*unconditionally on every sweep*. Using that path as provenance would defeat the invariant
completely: grant a permission, the next sweep's snapshot makes provenance equal live, the
sweep after that reads `live == previous`, concludes fast-forward, and overwrites the grant.
That is exactly the failure D18 spells out as the reason CLAUDE.md must never join
SNAPSHOT_FILES. So provenance is a *separate* file, `settings.rendered.json`, written only
by `converge_settings` and `adopt_settings`. The unconditional snapshot stays a pure backup.

**Two things are refused rather than resolved, and both refusals are reported.** A key that
switches off a permission prompt (`NEVER_SHARE`) never enters base and is never applied from
it, in either direction — the machine that carries `bypassPermissions` is a machine someone
chose that for, and every other machine is a machine nobody did. A union key whose two sides
are different *shapes* is not merged either: `_union` keeps the live side and raises a fault,
and `converge_settings` reports BLOCKED without writing, because combining an object with a
list means discarding one of them and neither this module nor the sweep is entitled to pick.
Silence is the failure mode to design against in both cases: an unreported refusal reads
exactly like the setting having been applied.

**`permissions.deny` is unioned, NOT intersected, and that is not a bug.** Read literally,
"keep the most permissive settings" would mean dropping deny rules the other machine lacks
— which here means deleting the rules that block reads of `.env`, `*.pem`, `*.key`,
`.ssh/**`, and local auth-token caches. That is plainly not what was asked for.
"Permissive" means "do not lose capabilities", and being able to leak a private key is not
a capability. Unioning both lists gives maximum allow AND every protection retained, which
is the only reading under which both halves of the request are true at once. Do not
"fix" this into an intersection.
"""

import copy
import datetime
import glob
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from . import clock, machine, paths

CLAUDE_HOME = os.path.expanduser("~/.claude")

# ---------------------------------------------------------------------------
# Key classification. Dotted paths, matched against the full path from the root,
# so `env.ANTHROPIC_BASE_URL` cannot be confused with a top-level key of the same name.
# ---------------------------------------------------------------------------

# Never written, never rendered, never compared for drift. These carry authentication:
# `apiKeyHelper` is the script that mints the token and `ANTHROPIC_BASE_URL` is the
# per-subscription endpoint (…/<subscription-id>). A wrong value here does not
# degrade Claude Code, it locks the user out of it on that machine. They are read from the
# live file and written back verbatim, and are stripped out of base and overlay defensively
# so a value committed there by accident can never reach a live file.
#
# **The model ids belong to the same family, and leaving them out was a live hazard.** Two
# machines can point at different model endpoints, so a model id names a model that exists on *one*
# endpoint. Base carried one endpoint's model ids while another machine runs another endpoint's
# against its own URL — and `ANTHROPIC_BASE_URL` being protected while the
# model names were not is the worst of both: a render kept the endpoint and replaced every model
# served on it. The documented remedy for the resulting DRIFTED flag
# (`claudeconf.py settings --adopt`) would therefore have repointed this machine at models its
# endpoint does not serve, which is the same lock-out this set exists to prevent, arriving
# through the model name instead of the URL. Found 2026-09-23 with the drift still unresolved,
# so it was never applied; `tests/test_claudesettings.py` now pins the render.
#
# All five, not only the three that differed: `ANTHROPIC_DEFAULT_HAIKU_MODEL` and
# `ANTHROPIC_SMALL_FAST_MODEL` may coincide, and a value that coincides is not a
# value that is shared — the moment one account's haiku id moves, the coincidence becomes the
# same bug with no code change to blame it on.
NEVER_SYNC = frozenset([
    "apiKeyHelper",
    "env.ANTHROPIC_BASE_URL",
    "env.ANTHROPIC_MODEL",
    "env.ANTHROPIC_DEFAULT_OPUS_MODEL",
    "env.ANTHROPIC_DEFAULT_SONNET_MODEL",
    "env.ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "env.ANTHROPIC_SMALL_FAST_MODEL",
])

# Read from the live file and written back verbatim, exactly like NEVER_SYNC — but for the
# opposite reason. These do not break the machine they land on; they make it *quieter*, by
# removing a permission prompt on a machine where nobody asked for that.
#
#   permissions.defaultMode                  — `bypassPermissions` here means no prompts at all.
#   permissions.disableBypassPermissionsMode  — the inverse switch; sharing it fleet-wide would
#                                               take away a machine's ability to opt in locally.
#   skipDangerousModePermissionPrompt         — drops the confirmation on the dangerous mode.
#
# `seed_base` refuses to put them in the committed base, `render` refuses to apply one that got
# there anyway, and `seed_report` names every refusal. Two layers, because they fail
# differently: seeding is a human command run once in a while, while base is a committed file
# that a hand edit or a bad three-way merge can change without anyone running seed at all. The
# real case is not hypothetical — a laptop's snapshot can carry
# `permissions.defaultMode: bypassPermissions`, so a seed run on *that* machine would have
# published prompt-free operation to the whole fleet, and `skipDangerousModePermissionPrompt`
# is in the committed base today and would otherwise land on a machine that never had it.
#
# Wanting one of these on a machine is legitimate; the live file is where it goes, by hand.
# Render carries a locally-set value through untouched (live is the floor), so refusing to
# *deliver* one never turns into deleting one somebody chose.
NEVER_SHARE = frozenset([
    "permissions.defaultMode",
    "permissions.disableBypassPermissionsMode",
    "skipDangerousModePermissionPrompt",
])

# Merged as a set-union, never replaced. A list becomes the sorted unique union of both
# sides; a dict becomes the key union, with `True` winning over `False` on a boolean
# collision — "a plugin enabled anywhere in the fleet is enabled".
#
# Every list-valued key here means *a set of grants*, and that is the test for membership: a
# non-union list is replaced wholesale by base, which is the one outcome the module docstring
# promises cannot happen ("never the user's grant vanished"). `permissions.ask`,
# `permissions.additionalDirectories`, `disabledMcpjsonServers` and `enabledMcpjsonServers`
# were missing and are exactly that shape — one `/permissions` interaction away from a live
# entry being deleted on the next sweep.
#
# Deliberately *not* "union every list-valued key by default". Union sorts, and sorting is only
# meaning-preserving for a set: `hooks.<Event>` is a list of matcher objects whose order
# decides execution order, so a default-union would silently reorder it on every machine and
# no comparison in this module could tell that apart from a legitimate change. An omission
# from this set loses one list entry on a render, is visible in the diff, and is fixed by
# adding a line here; a wrongly-unioned ordered list corrupts behaviour invisibly and forever.
# So: declare set-shaped keys explicitly, and add new ones as Claude Code grows them.
UNION_KEYS = frozenset([
    "permissions.allow",
    "permissions.deny",          # unioned on purpose — see the module docstring
    "permissions.ask",
    "permissions.additionalDirectories",
    "disabledMcpjsonServers",
    "enabledMcpjsonServers",
    "enabledPlugins",
    "companyAnnouncements",
    "extraKnownMarketplaces",
])

# Excluded from the seeded base: legitimately per-machine, or not yet worth sharing.
#
# This filter applies ONLY to `seed_base`. `render` honours whatever is actually in base, so
# promoting a key fleet-wide is "delete it from this set and re-seed", and demoting one is
# "delete it from settings-base.json". The first cut errs toward local because the two
# mistakes are not symmetric: a wrongly-local key means one machine keeps its own value,
# which is invisible and cheap; a wrongly-shared key actively overwrites a deliberate
# per-machine choice on every sweep.
#
#   theme      — a display preference, per screen and per lighting, not per fleet.
#   statusLine — same command on both machines but `refreshInterval` may differ
#                per machine, which is a per-machine cost/latency choice, and the script it
#                points at is snapshotted separately per machine anyway.
#   (`hooks` was here and was PROMOTED on 2026-09-01 — see the note below the set.)
#   permissions.defaultMode, permissions.disableBypassPermissionsMode
#              — how much this machine prompts before acting. Also in `NEVER_SHARE`, which is
#                what actually stops a render from applying one; they are named here too so
#                the seed report reads them as a deliberate local choice rather than an
#                omission, and so removing them from NEVER_SHARE alone cannot quietly make
#                them fleet-wide.
MACHINE_LOCAL_DEFAULT = frozenset([
    "theme",
    "statusLine",
    "permissions.defaultMode",
    "permissions.disableBypassPermissionsMode",
])

# `hooks` was machine-local until 2026-09-01, on a stated precondition: a hook value is an
# executable path plus a timeout, so its correctness depends on a *different* sync surface —
# the registry having cloned that repo on the target machine — having already succeeded.
# Settings sync cannot verify that, and a `SessionStart` hook carries a
# timeout, so being wrong costs seconds of every session start on a machine
# where the repo is missing. The precondition was "promote it once every machine is confirmed
# to have the repos the hooks reference", and it is now met and checked rather than assumed:
# the only hook in the fleet points at a session-start script in a sibling checkout, its repo is
# `sync: on` in sync/registry.md, and the file is present and executable on each machine. It is also skill-bearing, so an absent clone
# self-heals via the sweep rather than waiting for a human.
#
# Note what promotion alone does NOT do, because it is the trap: `hooks` is not in
# UNION_KEYS — deliberately, since `hooks.<Event>` is an ORDERED list of matchers and a
# union sorts — so `seed_base` takes it from `live` only. Seeding on a machine that has no
# hook therefore yields a base with no hook, whatever this set says. The value in
# `claude/settings-base.json` was taken from the machine that actually has it.


# Convergence outcomes. Same four strings as claudeconf, deliberately: they land in sync.py's
# report and in journal events side by side, and two vocabularies for one concept would be
# read as two concepts.
CURRENT = "current"
FAST_FORWARD = "fast_forward"
DRIFTED = "drifted"
BLOCKED = "blocked"
# Settings sync is not set up on this fleet yet: no committed base to render from. Distinct
# from BLOCKED because it is a state to report, not a fault to chase.
UNCONFIGURED = "unconfigured"

# Excluded from comparison entirely, not merely from writing. Retained for `differences()`,
# whose behaviour predates the render path and whose callers depend on it.
EXCLUDED_ENV = frozenset(["ANTHROPIC_BASE_URL"])


# ---------------------------------------------------------------------------
# Report-only comparison (unchanged behaviour; sync.py calls report()).
# ---------------------------------------------------------------------------


def _flatten(payload: Any, prefix: str = "") -> Dict[str, str]:
    """One comparable scalar per key path. Lists are compared whole, as sorted JSON."""
    flat = {}
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = "%s.%s" % (prefix, key) if prefix else key
            flat.update(_flatten(value, path))
    elif isinstance(payload, list):
        flat[prefix] = json.dumps(sorted(payload, key=repr), sort_keys=True)
    else:
        flat[prefix] = json.dumps(payload)
    return flat


def _excluded(path: str) -> bool:
    return path.split(".")[-1] in EXCLUDED_ENV


def differences(live: Dict[str, Any], other: Dict[str, Any]) -> List[str]:
    left, right = _flatten(live), _flatten(other)
    out = []
    for path in sorted(set(left) | set(right)):
        if _excluded(path):
            continue
        if left.get(path) != right.get(path):
            if path not in left:
                out.append("%s: only on the other machine" % path)
            elif path not in right:
                out.append("%s: only here" % path)
            else:
                out.append("%s: differs" % path)
    return out


def _load(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def report(live_path: str, snapshot_root: str, own_key: str) -> Tuple[List[str], List[str]]:
    """Return (report_lines, faults). Never raises on a malformed peer snapshot."""
    lines, faults = [], []
    if not os.path.exists(live_path):
        return ["no live settings.json"], faults

    try:
        live = _load(live_path)
    except ValueError as error:
        return [], ["live settings.json is not valid JSON: %s" % error]

    peers = [p for p in sorted(glob.glob(os.path.join(snapshot_root, "*", "settings.json")))
             if os.path.basename(os.path.dirname(p)) != own_key]
    if not peers:
        return ["no other machine has snapshotted settings.json yet"], faults

    for peer in peers:
        key = os.path.basename(os.path.dirname(peer))
        try:
            other = _load(peer)
        except ValueError as error:
            faults.append("%s settings.json is not valid JSON: %s" % (key, error))
            continue
        # Freshness matters: a stale diff presented as live is worse than no diff.
        stamp = datetime.datetime.fromtimestamp(os.path.getmtime(peer)).strftime("%Y-%m-%d")
        diffs = differences(live, other)
        if not diffs:
            lines.append("%s (snapshot %s): identical" % (key, stamp))
        else:
            lines.append("%s (snapshot %s): %d key(s) differ -- %s"
                         % (key, stamp, len(diffs), "; ".join(diffs[:6])))
    return lines, faults


# ---------------------------------------------------------------------------
# Paths. Functions, not module constants, because CLAUDE_HOME and LODESTAR_ROOT are both
# redirected in tests — a constant computed at import time would keep pointing at the real
# ~/.claude while the test thought it had been redirected, and this module writes.
# ---------------------------------------------------------------------------


def live_settings_path() -> str:
    return os.path.join(CLAUDE_HOME, "settings.json")


def base_path() -> str:
    """The committed, fleet-wide half. One file, shared, and the source of authority."""
    return os.path.join(paths.claude_dir(), "settings-base.json")


def overlay_path(key: Optional[str] = None) -> str:
    """This machine's declared divergence from base. Optional; missing means `{}`.

    Per-machine path for the reason `claudeconf.snapshot_dir` documents: the moment two
    machines write their own state to one committed path, each sweep overwrites the other's
    and the repo fills with ping-pong commits (decision D-D).
    """
    return os.path.join(paths.claude_dir(), "settings-overlay",
                        (key or machine.key()) + ".json")


def fragments_dir() -> str:
    """Label-scoped settings: `claude/settings-fragments/<label>.json`.

    The layer between base and overlay. Base is what EVERY machine shares; an overlay is one
    machine's divergence; a fragment is what every machine of one KIND shares (`mac`, `work`,
    `windows-wsl`, …, from machines.md's Labels column). It exists because base had quietly
    become "what the two Macs share": its hooks called a Mac-only terminal helper script and its
    env pins cloud-provider model ids, neither of which means anything on the WSL box. Moving
    such keys from base into `mac.json` / `work.json` renders byte-identically on the Macs
    (they carry both labels) and stops them reaching a machine that is not one.

    (The helper-script half of that example is historical: it was deleted from base.
    NOT because the script was missing but because the terminal's scripting API was disabled,
    so it failed on every event while exiting 0. It remains a correct illustration of a Mac-only key in base. The env half
    still motivates `work.json`. See docs/CLAUDE_CONFIG_LABELS.md.)
    """
    return os.path.join(paths.claude_dir(), "settings-fragments")


def load_fragments(labels: List[str],
                   root: Optional[str] = None) -> Tuple[List[Tuple[str, Dict[str, Any]]], List[str]]:
    """The fragments for `labels`, in the machine's declared label order. Returns (loaded, faults).

    A missing fragment file is normal (most labels will never need one). An unreadable or
    non-object one is a FAULT, never a skip: skipping it would render the machine without
    that kind's settings and then record the result as provenance.
    """
    root = root or fragments_dir()
    loaded, faults = [], []  # type: List[Tuple[str, Dict[str, Any]]], List[str]
    for label in labels:
        path = os.path.join(root, label + ".json")
        payload, error = _load_json(path)
        if error:
            faults.append(error)
        elif payload is not None:
            loaded.append((label, payload))
    return loaded, faults


def _fragments_for(key: Optional[str]) -> Tuple[List[Dict[str, Any]], List[str]]:
    loaded, faults = load_fragments(machine.labels(key or machine.key()))
    return [payload for _, payload in loaded], faults


def provenance_path(key: Optional[str] = None) -> str:
    """The live settings.json as of the last moment the repo and this machine agreed.

    Deliberately NOT `snapshot/<key>/settings.json`, even though that file exists and holds
    the same bytes most of the time. That one is written unconditionally every sweep by
    `claudeconf.snapshot_all`, so using it as provenance would make provenance advance on
    drift — the I1 violation D18 describes, which ends with the sweep overwriting the
    permission the user just granted. This file is written only when we render, or when the
    live file already equals the render.
    """
    return os.path.join(paths.claude_dir(), "snapshot", key or machine.key(),
                        "settings.rendered.json")


def adopt_dir(key: Optional[str] = None) -> str:
    return paths.claude_adopt_dir(key or machine.key())


def pending_captures(key: Optional[str] = None) -> List[str]:
    return sorted(glob.glob(os.path.join(adopt_dir(key), "*-settings.json")))


# ---------------------------------------------------------------------------
# Merge primitives.
# ---------------------------------------------------------------------------


def _path(prefix: str, key: str) -> str:
    return "%s.%s" % (prefix, key) if prefix else key


def _token(item: Any) -> str:
    """A total, stable ordering key for anything JSON can hold.

    Sorting by JSON text rather than by the value keeps `sorted()` from raising on a list
    that mixes types, and makes the union's output byte-identical run to run. Determinism
    here is not cosmetic: a union that reordered would make every sweep see a changed file
    and rewrite it, which is the write war this module exists to avoid.
    """
    return json.dumps(item, sort_keys=True)


def _unique_sorted(items: List[Any]) -> List[Any]:
    seen, out = set(), []
    for item in items:
        token = _token(item)
        if token not in seen:
            seen.add(token)
            out.append(item)
    return sorted(out, key=_token)


def _kind(value: Any) -> str:
    """The shape a union has to agree on: dict, list, or scalar. Absent is not a shape."""
    if isinstance(value, dict):
        return "an object"
    if isinstance(value, list):
        return "a list"
    return "a scalar"


def _mismatch(path: str, existing: Any, incoming: Any,
              faults: Optional[List[str]]) -> None:
    if faults is not None:
        faults.append("union key %s: this side is %s and the side being applied is %s — kept "
                      "the local value rather than merging, because a union cannot combine two "
                      "shapes without discarding one"
                      % (path or "(root)", _kind(existing), _kind(incoming)))


def _union(existing: Any, incoming: Any, path: str = "",
           faults: Optional[List[str]] = None, authoritative: bool = False) -> Any:
    """Set-union of two union-key values. Order of arguments matters only on a conflict.

    Dicts: key union. `True` beats `False` (a plugin enabled anywhere is enabled), nested
    dicts recurse, and any other value collision keeps `existing` — first writer wins, which
    `seed_report` surfaces rather than resolving silently.

    **A shape disagreement is refused, not resolved.** This used to let whichever side was a
    dict win: `_union(["local-plugin"], {"shared": True})` returned `{'shared': True}` and
    destroyed the list, in both argument orders. That is reachable without anybody making a
    mistake — a Claude Code release changing a union key's shape, or two machines running
    versions that disagree — and the place it would happen is a render, where the side
    destroyed is the live file. So `existing` (the live/floor side) is kept and `faults` gets a
    line; `converge_settings` turns any fault into BLOCKED and writes nothing, which is the
    only honest answer to "these two are not the same kind of thing".

    A scalar against a list is *not* a mismatch: the scalar is coerced to a one-element list
    and unioned, so neither side is lost and the result still means the same set.

    `authoritative` marks the layer being applied as able to say "no". Render passes it for the
    overlay only. Without it a `False` could never win from anywhere — `bool(existing or value)`
    is symmetric — so no plugin could be disabled on any machine, including by the per-machine
    overlay whose entire job is expressing a local divergence. Base-vs-live stays
    most-permissive: that is the fleet-wide rule, deliberately.
    """
    if isinstance(existing, dict) != isinstance(incoming, dict):
        if existing is not None and incoming is not None:
            _mismatch(path, existing, incoming, faults)
            return copy.deepcopy(existing)

    if isinstance(existing, dict) or isinstance(incoming, dict):
        out = dict(existing) if isinstance(existing, dict) else {}
        source = incoming if isinstance(incoming, dict) else {}
        for key, value in source.items():
            child = _path(path, key)
            if key not in out:
                out[key] = copy.deepcopy(value)
            elif isinstance(out[key], bool) and isinstance(value, bool):
                out[key] = bool(value) if authoritative else bool(out[key] or value)
            elif isinstance(out[key], dict) and isinstance(value, dict):
                out[key] = _union(out[key], value, child, faults, authoritative)
            elif _kind(out[key]) != _kind(value):
                _mismatch(child, out[key], value, faults)
        return out

    left = list(existing) if isinstance(existing, list) else []
    right = list(incoming) if isinstance(incoming, list) else []
    if not isinstance(existing, list) and existing is not None:
        left = [existing]
    if not isinstance(incoming, list) and incoming is not None:
        right = [incoming]
    return _unique_sorted(left + right)


HOOKS_KEY = "hooks"


def _merge_hook_list(existing: Any, incoming: Any, path: str,
                     faults: Optional[List[str]] = None) -> Any:
    """Ordered merge of one `hooks.<Event>` matcher list. Never reorders, never drops.

    This list used to be a scalar to `_merge_into`, so whichever layer named an event last
    replaced every earlier layer's matchers for it: on 2026-10-19 the `mac` fragment's
    PreToolUse list erased base's four matchers (and the new handoff guard with them), and
    the Laptop overlay's Stop/SessionStart/SessionEnd erased base's — while convergence said
    "current", because live faithfully equalled that wrong render. It cannot be a UNION_KEYS
    entry either: union sorts, and matcher order is execution order.

    So: `existing` keeps its order; an incoming matcher whose non-`hooks` fields (`matcher`
    and anything else) equal an existing one's has its missing hook entries appended to that
    matcher; any other incoming matcher is appended at the end. Equality is by `_token`, so a
    hook carried by two layers appears once, and the merge is idempotent. Like the union keys
    this makes every layer a floor: removing a hook from base does not remove it from live.
    A non-list on either side is a shape fault — the existing side is kept.
    """
    if existing is None:
        return copy.deepcopy(incoming)
    if not isinstance(existing, list) or not isinstance(incoming, list):
        _mismatch(path, existing, incoming, faults)
        return copy.deepcopy(existing)

    def ident(matcher: Any) -> str:
        if isinstance(matcher, dict):
            return _token(dict((k, v) for k, v in matcher.items() if k != "hooks"))
        return _token(matcher)

    out = copy.deepcopy(existing)
    for matcher in incoming:
        token = _token(matcher)
        if any(_token(m) == token for m in out):
            continue
        twin = None
        if isinstance(matcher, dict) and isinstance(matcher.get("hooks"), list):
            for candidate in out:
                if (isinstance(candidate, dict) and isinstance(candidate.get("hooks"), list)
                        and ident(candidate) == ident(matcher)):
                    twin = candidate
                    break
        if twin is None:
            out.append(copy.deepcopy(matcher))
            continue
        have = set(_token(h) for h in twin["hooks"])
        for hook in matcher["hooks"]:
            if _token(hook) not in have:
                have.add(_token(hook))
                twin["hooks"].append(copy.deepcopy(hook))
    return out


def missing_hooks(layers: List[Optional[Dict[str, Any]]], rendered: Dict[str, Any]) -> List[str]:
    """`<Event>: <command>` for every hook entry a layer declares that `rendered` lacks."""
    def entries(payload: Any) -> set:
        found = set()
        events = payload.get(HOOKS_KEY) if isinstance(payload, dict) else None
        for event, matchers in (events or {}).items() if isinstance(events, dict) else []:
            for matcher in matchers if isinstance(matchers, list) else []:
                hooks = matcher.get("hooks") if isinstance(matcher, dict) else None
                for hook in hooks if isinstance(hooks, list) else []:
                    found.add((event, _token(hook)))
        return found

    have = entries(rendered)
    lost = set()
    for layer in layers:
        lost |= entries(layer) - have
    return ["%s: %s" % (event, json.loads(token).get("command", token)
                        if token.startswith("{") else token)
            for event, token in sorted(lost)]


def _merge_into(target: Dict[str, Any], source: Dict[str, Any], prefix: str = "",
                drop: Any = frozenset(), faults: Optional[List[str]] = None,
                authoritative: bool = False) -> None:
    """Layer `source` onto `target` in place. Scalars replace, union keys union.

    `NEVER_SYNC` and `NEVER_SHARE` are dropped unconditionally, so this is also the function
    that guarantees an `apiKeyHelper` or a `bypassPermissions` committed to base by mistake
    cannot reach a live file. `drop` is the extra per-call exclusion set
    (`MACHINE_LOCAL_DEFAULT` when seeding). `faults` collects shape disagreements a union
    refused to resolve, and `authoritative` marks `source` as a layer whose `False` wins — see
    `_union` for both.
    """
    if not isinstance(source, dict):
        return
    for key in source:
        path = _path(prefix, key)
        if path in NEVER_SYNC or path in NEVER_SHARE or path in drop:
            continue
        value = source[key]
        if path in UNION_KEYS:
            target[key] = _union(target.get(key), value, path, faults, authoritative)
        elif prefix == HOOKS_KEY:
            target[key] = _merge_hook_list(target.get(key), value, path, faults)
        elif isinstance(value, dict):
            if not isinstance(target.get(key), dict):
                target[key] = {}
            _merge_into(target[key], value, path, drop, faults, authoritative)
        else:
            target[key] = copy.deepcopy(value)


def _project(payload: Any, keep: Any, prefix: str = "") -> Dict[str, Any]:
    """A copy of `payload` containing only the subtrees at paths in `keep`."""
    out = {}
    if not isinstance(payload, dict):
        return out
    for key, value in payload.items():
        path = _path(prefix, key)
        if path in keep:
            out[key] = copy.deepcopy(value)
        elif isinstance(value, dict):
            child = _project(value, keep, path)
            if child:
                out[key] = child
    return out


def _paths_of(payload: Any, prefix: str = "") -> Dict[str, Any]:
    """Flatten to {dotted path: value}, stopping at union keys and at non-dicts."""
    flat = {}
    if not isinstance(payload, dict):
        return flat
    for key, value in payload.items():
        path = _path(prefix, key)
        if path in UNION_KEYS or not isinstance(value, dict):
            flat[path] = value
        else:
            flat.update(_paths_of(value, path))
    return flat


def _under(path: str, group: Any) -> bool:
    """True if `path` is in `group` or sits beneath a member of it.

    Needed because the classification sets name whole subtrees (`statusLine`, `hooks`) while
    the flattened view names leaves (`statusLine.refreshInterval`). Comparing only exact
    paths would report a machine-local subtree's leaves as unclassified.
    """
    if path in group:
        return True
    return any(path.startswith(member + ".") for member in group)


def _has(payload: Any, path: str) -> bool:
    """Whether a dotted path exists in a parsed settings object, subtrees included."""
    node = payload
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


# ---------------------------------------------------------------------------
# Seeding and rendering.
# ---------------------------------------------------------------------------


def seed_base(live: Dict[str, Any],
              peers: Optional[Dict[str, Dict[str, Any]]] = None,
              faults: Optional[List[str]] = None) -> Dict[str, Any]:
    """Build `claude/settings-base.json` from this machine's live file plus peer snapshots.

    Union keys are unioned across every machine, so nothing is lost from either. Scalars are
    taken from `live` only: this is the machine being worked on, so its values are the ones
    someone actually chose here. A scalar that exists *only* on a peer is deliberately NOT
    adopted — `seed_report` names it instead. Adopting it would silently change this
    machine's behaviour on the strength of a value nobody set here.

    `NEVER_SYNC`, `NEVER_SHARE` and `MACHINE_LOCAL_DEFAULT` are excluded entirely. The
    `NEVER_SHARE` exclusion is the one with teeth, and guarding only the *peer* direction was
    not enough: a laptop may carry
    `permissions.defaultMode: bypassPermissions`, so it is `live` there, and taking every
    local scalar unconditionally would have published prompt-free operation to the committed
    base — from which the next sweep on every other machine would have rendered it.

    `peers` maps machine key -> parsed settings. Entries that are not dicts are skipped
    rather than raising: a malformed peer snapshot is a report, never a fatal (D13).

    `faults` is optional and collects shape disagreements between this machine and a peer on a
    union key — `seed_report` passes one so a human seeding sees them.
    """
    base = {}  # type: Dict[str, Any]
    _merge_into(base, live or {}, drop=MACHINE_LOCAL_DEFAULT, faults=faults)
    for key in sorted(peers or {}):
        peer = (peers or {})[key]
        if not isinstance(peer, dict):
            continue
        _merge_into(base, _project(peer, UNION_KEYS), drop=MACHINE_LOCAL_DEFAULT, faults=faults)
    return base


def seed_report(live: Dict[str, Any],
                peers: Optional[Dict[str, Dict[str, Any]]] = None) -> List[str]:
    """Human-readable account of what `seed_base` chose, and what it declined to.

    Separate from `seed_base` so that function can keep the `-> dict` signature callers
    want, while the reasoning a human needs is still available and computed from exactly the
    same inputs.
    """
    peers = peers or {}
    shape_faults = []  # type: List[str]
    base = seed_base(live, peers, shape_faults)
    base_flat = _paths_of(base)
    live_flat = _paths_of(live or {})
    peer_flat = dict((key, _paths_of(peers[key])) for key in peers)
    lines = []

    for path in sorted(UNION_KEYS):
        if path not in base_flat:
            continue
        value = base_flat[path]
        mine = live_flat.get(path)
        size = len(value) if isinstance(value, (list, dict)) else 1
        mine_size = len(mine) if isinstance(mine, (list, dict)) else (0 if mine is None else 1)
        gained = size - mine_size
        lines.append("union %s: %d entries (%d here%s)"
                     % (path, size, mine_size,
                        ", +%d from peers" % gained if gained > 0 else ""))

    for path in sorted(base_flat):
        if path in UNION_KEYS:
            continue
        chosen = base_flat[path]
        disagree = []
        for key in sorted(peer_flat):
            other = peer_flat[key].get(path)
            if other is not None and _token(other) != _token(chosen):
                disagree.append("%s had %s" % (key, _token(other)))
        if disagree:
            lines.append("scalar %s: kept %s from here (%s)"
                         % (path, _token(chosen), "; ".join(disagree)))

    for key in sorted(peer_flat):
        for path in sorted(peer_flat[key]):
            if (_under(path, UNION_KEYS) or _under(path, NEVER_SYNC)
                    or _under(path, MACHINE_LOCAL_DEFAULT) or path in live_flat):
                continue
            lines.append("not adopted: %s = %s exists only on %s — a scalar nobody set here"
                         % (path, _token(peer_flat[key][path]), key))

    for path in sorted(MACHINE_LOCAL_DEFAULT):
        holders = [k for k in sorted(peers) if _has(peers[k], path)]
        if _has(live or {}, path):
            holders.insert(0, "here")
        if holders:
            lines.append("machine-local: %s stays out of base (set on %s)"
                         % (path, ", ".join(holders)))
    for path in sorted(NEVER_SYNC):
        lines.append("never synced: %s always comes from the live file" % path)
    # Named unconditionally, and named as a *refusal*. A silent refusal is nearly as bad as a
    # silent adoption: the person seeding on such a machine needs to read that the base they just
    # wrote does not carry the bypass mode their machine runs in, or they will assume it does.
    for path in sorted(NEVER_SHARE):
        holders = [k for k in sorted(peers) if _has(peers[k], path)]
        if _has(live or {}, path):
            holders.insert(0, "here")
        lines.append("never shared: %s refused — it suppresses a permission prompt, so base "
                     "must not carry it%s"
                     % (path, " (set on %s; stays there)" % ", ".join(holders) if holders
                        else ""))
    for fault in shape_faults:
        lines.append("shape disagreement, not merged: %s" % fault)
    return lines


def render(base: Optional[Dict[str, Any]], overlay: Optional[Dict[str, Any]],
           live: Optional[Dict[str, Any]],
           faults: Optional[List[str]] = None,
           fragments: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The settings the live file should hold. Deterministic and idempotent.

    `fragments` are the label-scoped layers (see `fragments_dir`), applied after base and
    before overlay, in the order given, with base's (non-authoritative) semantics. Omitted or
    empty, the render is exactly the pre-fragment one.

    Layer order is live, then base, then overlay: overlay beats base beats live for scalars,
    and all three are unioned for `UNION_KEYS`. `live` is the floor rather than a source of
    truth — see the module docstring on why render must never delete a live-only key.

    Pass `faults` (an empty list) to hear about shape disagreements a union refused to resolve.
    Optional and last, so the existing three-argument calls are unaffected; a caller that omits
    it still gets the safe behaviour (the live side is kept), just not the explanation.
    `converge_settings` passes one and reports BLOCKED if anything lands in it.

    The overlay layer is applied `authoritative=True`, which is what lets a per-machine `False`
    disable a plugin the fleet enables. Base is not: base-vs-live is most-permissive by design.

    `NEVER_SYNC` and `NEVER_SHARE` values are carried over from `live` verbatim and last, so
    nothing in base or overlay can reach them even by accident — the auth keys because a wrong
    value locks the user out, the suppressors because a wrong value switches off a permission
    prompt on a machine that never asked. Carrying them from live rather than deleting them
    keeps live-as-floor intact: a suppressor a human set on this machine survives the render.

    Idempotence is a hard requirement, not a nicety: `render` runs on every sweep against its
    own previous output, and a render that moved would rewrite the file every ~127 s forever.
    `render(base, overlay, render(base, overlay, live)) == render(base, overlay, live)` is
    pinned by a test.
    """
    out = {}  # type: Dict[str, Any]
    _merge_into(out, live or {}, faults=faults)
    _merge_into(out, base or {}, faults=faults)
    for fragment in fragments or []:
        _merge_into(out, fragment or {}, faults=faults)
    _merge_into(out, overlay or {}, faults=faults, authoritative=True)
    for path in sorted(NEVER_SYNC | NEVER_SHARE):
        parts = path.split(".")
        source = live or {}
        for part in parts[:-1]:
            source = source.get(part) if isinstance(source, dict) else None
            if source is None:
                break
        if isinstance(source, dict) and parts[-1] in source:
            target = out
            for part in parts[:-1]:
                if not isinstance(target.get(part), dict):
                    target[part] = {}
                target = target[part]
            target[parts[-1]] = copy.deepcopy(source[parts[-1]])
    return out


# ---------------------------------------------------------------------------
# Convergence.
# ---------------------------------------------------------------------------


def _dumps(payload: Dict[str, Any]) -> str:
    """`indent=2, sort_keys=True` so the file on disk is stable across writers.

    Comparison is done on parsed JSON (`_same`), so formatting cannot manufacture drift —
    but writing unstably would still churn git, and churn is what stops the sweep from ever
    reaching quiescence.
    """
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _comparable(payload: Any, prefix: str = "") -> Any:
    """Normalize for the drift comparison: drop NEVER_SYNC, sort union lists.

    Union lists are sorted because we always *write* them sorted and their meaning is a set;
    a reordering is not a change. `NEVER_SYNC` is dropped because render carries those from
    live verbatim, so they can never be clobbered — treating a rotated apiKeyHelper as drift
    would flag the file forever and block a render that was always safe.
    """
    if isinstance(payload, dict):
        out = {}
        for key, value in payload.items():
            path = _path(prefix, key)
            if path in NEVER_SYNC:
                continue
            if path in UNION_KEYS and isinstance(value, list):
                out[key] = _unique_sorted(value)
            else:
                out[key] = _comparable(value, path)
        return out
    return payload


def _same(left: Any, right: Any) -> bool:
    return _token(_comparable(left)) == _token(_comparable(right))


def _read_text(path: str) -> Optional[str]:
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _write_text(path: str, text: str) -> None:
    """Write atomically, and skip entirely when the content already matches.

    The skip is `claudeconf._write`'s, for `claudeconf._write`'s reason: re-stamping an
    unchanged file's mtime every sweep is the shape of a bug this repo has already paid for,
    where a snapshot that looked freshly changed on every sweep meant Lodestar was never
    quiescent and so never auto-committed again.
    """
    if os.path.exists(path):
        if _read_text(path) == text:
            return
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def _load_json(path: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return (payload, error). Never raises. Missing file is (None, None).

    The read gets its own `try`, because it used to have none and every ordinary I/O condition
    escaped a function documented as never raising: a non-UTF-8 byte anywhere in the live file,
    base, overlay or provenance (`UnicodeDecodeError`), the live path being a directory
    (`IsADirectoryError`), and mode-000 on any of them (`PermissionError`). `UnicodeDecodeError`
    *is* a `ValueError` and so was already in the caught set — but only around `json.loads`,
    which never saw the bytes. It is named explicitly below anyway: the reader has to be able
    to tell that a decode failure is handled without knowing that piece of the hierarchy.

    Read and parse keep separate messages because they are separate things to go and fix, and
    because "is not valid JSON" is the wording the existing report and tests use.
    """
    try:
        text = _read_text(path)
    except (OSError, ValueError, UnicodeDecodeError) as error:
        return None, "%s could not be read: %s" % (os.path.basename(path), error)
    if text is None:
        return None, None
    try:
        payload = json.loads(text)
    except ValueError as error:
        return None, "%s is not valid JSON: %s" % (os.path.basename(path), error)
    if not isinstance(payload, dict):
        return None, "%s is not a JSON object" % os.path.basename(path)
    return payload, None


def _stamp() -> str:
    # Filename-safe, sortable, second precision. Same as claudeconf._stamp: two captures in
    # one second would collide, but capture is idempotent so a same-second recapture cannot
    # happen.
    return re.sub(r"[^0-9T]", "", clock.now())[:15]


def _capture(text: str, key: Optional[str] = None) -> Tuple[Optional[str], str]:
    """Save the live file verbatim into the adopt queue. Idempotent (I2).

    Drift persists across sweeps by design — a scalar the user changed stays changed until a
    human resolves it — so a naive capture would write hundreds of files a day.
    """
    existing = pending_captures(key)
    if existing and _read_text(existing[-1]) == text:
        return None, "unchanged since %s" % os.path.basename(existing[-1])
    target = os.path.join(adopt_dir(key), "%s-settings.json" % _stamp())
    _write_text(target, text)
    return target, "captured %s" % os.path.basename(target)


def converge_settings(dry_run: bool = False,
                      key: Optional[str] = None) -> Tuple[str, str]:
    """Reconcile ~/.claude/settings.json with the repo. Returns (outcome, detail). NEVER raises.

    The blanket `except Exception` is the boundary, deliberately, and for the reason
    `claudeconf.converge_claudemd` gives for the identical one: any unexpected failure here
    must degrade to one flagged row. "Never raises" was documented and not true — a non-UTF-8
    byte in any of the four files it reads, a live path that is a directory, mode-000 on the
    live file, or `claude/adopt/<key>` occupied by a file all escaped.

    What that cost is worse than a bad row. `sync.py` wraps the call, so the sweep survived —
    but the settings surface went permanently FLAGGED with an opaque `settings step failed: …`
    *and `report()` never ran*, because it is the next statement. `report()` is the
    cross-machine peer diff, and that diff is the only thing surfacing a machine's
    `permissions.defaultMode: bypassPermissions`. An exception here did not degrade a feature,
    it hid a security finding.
    """
    try:
        return _converge_settings(dry_run, key)
    except SystemExit as error:
        return BLOCKED, str(error).split("\n")[0]
    except Exception as error:
        return BLOCKED, ("settings convergence failed: %s: %s"
                         % (type(error).__name__, str(error).split("\n")[0]))


def _converge_settings(dry_run: bool = False,
                       key: Optional[str] = None) -> Tuple[str, str]:
    """The body of `converge_settings`, which owns the never-raises boundary.

    Split out so the boundary is one unmissable wrapper rather than a claim spread over a long
    function — the same shape as `claudeconf._converge_claudemd`.

    **Invariant I1: provenance advances only when we rendered, or when the live file already
    equals the render.** Advancing it on DRIFTED would make the next sweep read
    `live == previous`, conclude fast-forward, and overwrite the change it had just captured;
    the content would survive in git while the file that actually takes effect silently
    reverted. See `provenance_path` for the second half of the same trap.

    One consequence of live-as-floor is worth stating, because it is the reason this does not
    degenerate into a permanent DRIFTED flag: when the user grants a permission at a prompt,
    Claude Code appends it to `permissions.allow`, which is a union key — so the next
    render *already contains it*, `live == expected`, and the outcome is CURRENT with nothing
    lost. DRIFTED is reserved for a real conflict, a scalar in the live file that disagrees
    with base, which is exactly the case a human should decide.
    """
    live_path = live_settings_path()
    live, error = _load_json(live_path)
    if error:
        return BLOCKED, "live %s" % error
    if live is None:
        return BLOCKED, "no live settings.json at %s" % live_path

    base, error = _load_json(base_path())
    if error:
        return BLOCKED, error
    if base is None:
        # Not the same thing as broken. A fleet that has never seeded a base has settings sync
        # switched off, and reporting that as a fault every ~127 s forever would build the wall
        # of unread flags D13's corollary warns about — the same reason `skills_state` reports a
        # missing skills table as a line rather than a fault. BLOCKED stays for real breakage,
        # like a live file that will not parse.
        return UNCONFIGURED, ("no %s yet — seed it before settings can converge"
                              % os.path.relpath(base_path(), paths.root()))

    overlay, error = _load_json(overlay_path(key))
    if error:
        return BLOCKED, error
    fragments, fragment_faults = _fragments_for(key)
    if fragment_faults:
        return BLOCKED, "; ".join(fragment_faults)

    shape_faults = []  # type: List[str]
    expected = render(base, overlay or {}, live, shape_faults, fragments)
    if shape_faults:
        # Refusing is the whole point. `render` already kept the live side, so nothing is lost
        # by returning here — and writing a merge that dropped one side of a union would be
        # exactly the silent deletion this outcome exists to prevent. A human resolves it.
        return BLOCKED, "; ".join(shape_faults)

    lost = missing_hooks([base] + list(fragments or []) + [overlay or {}], expected)
    if lost:
        # The render itself dropped a hook a layer declares. Saying CURRENT (or rendering)
        # here is how 2026-10-19's lie happened: live equalled a render that had lost base's
        # hooks. Refuse instead, naming what would be lost.
        return BLOCKED, "render drops %d declared hook(s): %s" % (len(lost), "; ".join(lost[:3]))

    previous, error = _load_json(provenance_path(key))
    if error:
        # An unreadable provenance file must not be treated as "no provenance": that would
        # read as a first run and fast-forward over whatever the live file holds.
        return BLOCKED, "%s — delete it to re-adopt" % error

    if _same(live, expected):
        if not dry_run:
            _write_text(provenance_path(key), _dumps(expected))   # I1: live == expected
        return CURRENT, "up to date"

    if previous is not None and _same(live, previous):
        if dry_run:
            return FAST_FORWARD, "would render %d key(s)" % len(expected)
        _write_text(live_path, _dumps(expected))
        _write_text(provenance_path(key), _dumps(expected))       # I1: we rendered
        return FAST_FORWARD, "rendered %d key(s)" % len(expected)

    if dry_run:
        return DRIFTED, "would capture an external edit"
    _, detail = _capture(_read_text(live_path) or "", key)        # I1: provenance untouched
    return DRIFTED, ("live settings.json disagrees with the repo — %s; "
                     "run: claudeconf.py settings --adopt" % detail)


def adopt_settings(key: Optional[str] = None) -> Dict[str, Any]:
    """Render unconditionally, after capturing the live file verbatim. The escape hatch.

    Unconditional writing is what the rest of this module exists to refuse, and it is correct
    here for `claudeconf.finish_adopt`'s reason: by the time this returns, the pre-existing
    live bytes are in the repo verbatim under `claude/adopt/<key>/`, so nothing can be lost —
    only moved somewhere a human can read it.

    It is also *required*, not merely convenient. The first run on any machine has no
    provenance, so `converge_settings` reports DRIFTED and captures; without an explicit way
    to accept the render, settings would never converge on that machine and the flag would
    become wallpaper. Raises on unusable inputs, unlike `converge_settings` — this one is
    called by a human, who should see why it refused.
    """
    live_path = live_settings_path()
    live, error = _load_json(live_path)
    if error:
        raise SystemExit(error)
    if live is None:
        raise SystemExit("no live settings.json at %s" % live_path)
    base, error = _load_json(base_path())
    if error:
        raise SystemExit(error)
    if base is None:
        raise SystemExit("no %s — seed it first" % base_path())
    overlay, error = _load_json(overlay_path(key))
    if error:
        raise SystemExit(error)
    fragments, fragment_faults = _fragments_for(key)
    if fragment_faults:
        raise SystemExit("; ".join(fragment_faults))

    shape_faults = []  # type: List[str]
    expected = render(base, overlay or {}, live, shape_faults, fragments)
    if shape_faults:
        # Even the escape hatch refuses this one. Everything else here is recoverable because
        # the live bytes are captured verbatim first; a union that discarded one of two shapes
        # would still be an unreviewed guess about which half mattered.
        raise SystemExit("; ".join(shape_faults))

    _capture(_read_text(live_path) or "", key)
    _write_text(live_path, _dumps(expected))
    _write_text(provenance_path(key), _dumps(expected))
    return expected


def peer_settings(own_key: Optional[str] = None) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Load every other machine's snapshotted settings.json. Returns (peers, faults).

    Reads `snapshot/<key>/settings.json` — the unconditional backup — not
    `settings.rendered.json`, because what a peer *has* is the interesting input to a seed,
    and the rendered file only exists once that machine has converged.
    """
    own_key = own_key or machine.key()
    peers, faults = {}, []
    pattern = os.path.join(paths.claude_dir(), "snapshot", "*", "settings.json")
    for path in sorted(glob.glob(pattern)):
        key = os.path.basename(os.path.dirname(path))
        if key == own_key:
            continue
        payload, error = _load_json(path)
        if error:
            faults.append("%s %s" % (key, error))
            continue
        if payload is not None:
            peers[key] = payload
    return peers, faults


def write_base(payload: Dict[str, Any]) -> str:
    _write_text(base_path(), _dumps(payload))
    return base_path()
