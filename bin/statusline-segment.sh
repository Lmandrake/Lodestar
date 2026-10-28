#!/bin/bash
# Lodestar's statusline segment: "L*" plus git-style sync symbols.
#
# Prints ONE short token to stdout, or nothing at all, then exits 0. Always.
#
# **Why this file is in Lodestar and not in ~/.claude/statusline.sh.** That script is
# captured by `claudeconf.py`'s SNAPSHOT_FILES live->repo only — there is no base/overlay
# renderer for it, so a hand edit there reaches exactly ONE machine for ever (the same
# defect as a hand-installed skill). Keeping the logic here means it syncs to every machine
# through the ordinary sweep, and each machine's statusline.sh needs only a one-line call
# that never changes again.
#
# **Two halves, two sources, deliberately.** Measured 2026-09-30: the sweep's own
# `repos[X].outcome` said Lodestar was "flagged -- conflict on rebase ... needs you" while
# the repo was 0 ahead / 0 behind with no rebase in progress. The verdict was 36 minutes old
# and already false. So:
#   * The HEARTBEAT comes from the sweep, where staleness cannot lie -- the age of its own
#     output IS the measurement of whether the sweep is still running.
#   * The SYNC symbols come from live git, because a lagging answer here produces a red
#     alarm for a problem that was fixed an hour ago, and an alarm that cries wolf is worse
#     than no alarm.
#
# **Cost discipline.** This runs every few seconds (statusLine.refreshInterval). The host
# script may already be slow (an unbounded apiKeyHelper call, for one),
# so the budget here is "do not make it worse": 4 cheap forks, ~40ms measured, NO network,
# NO fetch, NO lock wait, and no JSON parse. `lib/heartbeat.facts()` is the rich answer but
# costs 88ms with Python startup, which is why the sweep pre-digests it to one line.
#
# Usage:  statusline-segment.sh [session-cwd]
# Falls back to $PWD, but the caller SHOULD pass the session's directory -- the host script
# has an existing bug where `git branch --show-current` runs in the script's cwd rather than
# the session's, and this segment must not inherit it.

DIR="${1:-$PWD}"
# Overridable so the dead-heartbeat and foreign-machine-silent branches can actually be
# exercised. Without this the only way to see them is to wait two hours or stop the sweep,
# which means they would ship unlooked-at — and a state with no `.bub.s-partial`-style rule
# is this repo's recurring invisible bug.
LS_LINE_FILE="${LODESTAR_STATUSLINE_LINE:-$HOME/dev/.lodestar/heartbeat-line}"

# Match lib/heartbeat.SILENT_AFTER_SECONDS. Two hours, not one: the sweep fires on
# StartInterval 3607, so a one-hour threshold would false-alarm on ordinary jitter.
LS_SILENT_AFTER=7200

# --- glyphs -----------------------------------------------------------------------------
# Degrade exactly like the host script does (it checks LANG/LC_ALL for UTF-8), but under our
# own env namespace rather than extending the host script's.
if [ -n "$LODESTAR_STATUSLINE_ASCII" ] || \
   { [ "${LANG}${LC_ALL}" = "${LANG}${LC_ALL}" ] && \
     ! printf '%s' "${LC_ALL:-${LANG:-}}" | grep -qi 'utf-*8'; }; then
    # NOT "*" for dirty: the segment's own literal prefix is `L*`, so a "*" mark renders
    # `L**`, which reads as one ambiguous token rather than a label plus a flag. Found by
    # looking at the output, which no assertion here would have caught.
    LS_AHEAD=">" LS_BEHIND="<" LS_DIVERGED="<>" LS_DIRTY="+" LS_PAUSED="=" LS_DEAD="!"
    LS_FLEET="o"
else
    LS_AHEAD="↑" LS_BEHIND="↓" LS_DIVERGED="⇅" LS_DIRTY="●" LS_PAUSED="⏸" LS_DEAD="⚠"
    LS_FLEET="○"
fi
# LS_DEAD and LS_FLEET must be DIFFERENT GLYPHS, not one glyph in two colours. "your sweep
# stopped" and "a different machine went dark" need different responses, and a distinction
# carried only by red-vs-yellow is no distinction at all under NO_COLOR or for a red-green
# colourblind reader. Colour still rides along as a second, redundant channel — never as the
# only one.

# --- colour ----------------------------------------------------------------------------
# Honour NO_COLOR and a dumb terminal, which the host script does not.
if [ -n "$NO_COLOR" ] || [ "$TERM" = "dumb" ]; then
    LS_G="" LS_Y="" LS_R="" LS_DIM="" LS_O=""
else
    LS_G=$'\033[32m' LS_Y=$'\033[33m' LS_R=$'\033[31m'
    LS_DIM=$'\033[2m' LS_O=$'\033[0m'
fi

# --- is this a Lodestar-managed checkout? ------------------------------------------------
# `lodestar.id` is the enrollment stamp every enrolled checkout carries. Resolve the COMMON
# git dir first so an ordinary linked worktree reads as enrolled rather than as a stranger.
LS_ID=$(git -C "$DIR" config --local lodestar.id 2>/dev/null)
if [ -z "$LS_ID" ]; then
    # Not on the board. Say so rather than printing nothing: a session that drifted into
    # untracked work must not look identical to a tracked one.
    printf '%s' "${LS_DIM}L?${LS_O}"
    exit 0
fi

# --- heartbeat: is the sweep still running at all? ---------------------------------------
# One line, written by every non-dry sweep: "<schema> <epoch> <fresh> <silent>".
# Absent means "never swept on this machine" -> unknown, not broken. Unreadable means the
# same. Neither is a fault worth shouting about, so both fall through to no heartbeat mark.
LS_HB=""
if [ -r "$LS_LINE_FILE" ]; then
    read -r _ls_schema _ls_epoch _ls_fresh _ls_silent < "$LS_LINE_FILE" 2>/dev/null
    case "$_ls_epoch" in
        ''|*[!0-9]*) : ;;   # not a number -- treat as unknown, never as zero
        *)
            _ls_now=$(date +%s)
            _ls_age=$(( _ls_now - _ls_epoch ))
            if [ "$_ls_age" -gt "$LS_SILENT_AFTER" ]; then
                LS_HB="${LS_R}${LS_DEAD}${LS_O}"
            elif [ -n "$_ls_silent" ] && [ "$_ls_silent" -gt 0 ] 2>/dev/null; then
                # This machine is sweeping, but another machine on the fleet has gone dark.
                # That is the exact ambiguity lib/heartbeat.py was built to resolve, so it
                # earns a mark -- in yellow, because it is someone else's machine, not yours.
                LS_HB="${LS_Y}${LS_FLEET}${LS_O}"
            fi
            ;;
    esac
fi

# --- paused? ----------------------------------------------------------------------------
# A paused repo is deliberately not participating, which is not a fault. Checked before the
# sync symbols because while paused they describe work the sweep is not trying to move.
LS_PAUSE_MARK=""
LS_GITDIR=$(git -C "$DIR" rev-parse --git-common-dir 2>/dev/null)
case "$LS_GITDIR" in
    /*) : ;;
    ?*) LS_GITDIR="$DIR/$LS_GITDIR" ;;
esac
if [ -n "$LS_GITDIR" ] && [ -e "$LS_GITDIR/lodestar-pause" ]; then
    LS_PAUSE_MARK="${LS_Y}${LS_PAUSED}${LS_O}"
fi

# --- sync: live, because a lagging answer here is worse than none ------------------------
LS_SYNC=""
LS_COUNTS=$(git -C "$DIR" rev-list --left-right --count '@{u}...HEAD' 2>/dev/null)
if [ -n "$LS_COUNTS" ]; then
    LS_BEHIND_N=${LS_COUNTS%%[	 ]*}
    LS_AHEAD_N=${LS_COUNTS##*[	 ]}
    if [ "${LS_BEHIND_N:-0}" -gt 0 ] && [ "${LS_AHEAD_N:-0}" -gt 0 ]; then
        LS_SYNC="${LS_R}${LS_DIVERGED}${LS_O}"
    elif [ "${LS_AHEAD_N:-0}" -gt 0 ]; then
        LS_SYNC="${LS_Y}${LS_AHEAD}${LS_AHEAD_N}${LS_O}"
    elif [ "${LS_BEHIND_N:-0}" -gt 0 ]; then
        LS_SYNC="${LS_Y}${LS_BEHIND}${LS_BEHIND_N}${LS_O}"
    fi
fi

# Dirty tree. `--porcelain` with no -uall on purpose: this is a yes/no question, so letting
# a new directory collapse to `dir/` is cheaper and changes nothing. (The guards in
# lib/gitutil DO need -uall, for a different question -- do not copy this choice there.)
if [ -n "$(git -C "$DIR" status --porcelain 2>/dev/null | head -1)" ]; then
    LS_SYNC="${LS_SYNC}${LS_Y}${LS_DIRTY}${LS_O}"
fi

# --- compose -----------------------------------------------------------------------------
# Green L* means enrolled, sweeping, clean, in sync -- the quiet healthy case. Every mark
# after it is a specific deviation. No mark is ever drawn for "fine".
if [ -z "$LS_SYNC" ] && [ -z "$LS_HB" ] && [ -z "$LS_PAUSE_MARK" ]; then
    printf '%s' "${LS_G}L*${LS_O}"
else
    printf '%s' "${LS_DIM}L${LS_O}*${LS_PAUSE_MARK}${LS_HB}${LS_SYNC}"
fi
exit 0
