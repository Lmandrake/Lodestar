---
name: tickets
description: File, list, move, link and close TICKETS, Lodestar's ticket system for EVERY tracked project (one markdown file per ticket in the project's own git home). Use whenever anyone says "make a ticket", "file a ticket/bug/issue", "open a ticket in <project>", "track this", "add it to the backlog", "what's planned for <project>", "what tickets are open", "ticket for Lodestar about X", or asks another project for work ("ask Lodestar to build X": a cross-project need). Also when an agent finds a bug with a likely fix, a build/process improvement, an ongoing repair or upgrade, or a dated real-world action (book the flight, repay someone, submit the form) that should be planned rather than remembered. Lodestar DOES have a ticketing system; never answer "it only has loops".
---

# Tickets

## Names: SUBJECT_INTENT_TWIST (owner, 2026-10-25; policy from RimMandrake, 2026-10-10)

Every new ticket is minted with `--name`, never a number (`LODE-35` is the retired form; old ids
stay valid forever, never renamed). `SUBJECT_INTENT_TWIST`: 3-7 words, <= 44 chars, A-Z 0-9 `_`.
SUBJECT (1-4 greppable real nouns), INTENT (one bank word, what it set out to do, never live
state), TWIST (a true, specific, kind aside as of filing: `THIRD_TIME_LUCKY`, `STILL_RED`).
Not a guess, no in-joke needing this conversation, never mocking a person. The title still says
what done looks like. If the intent was wrong, file a new ticket and link it; never rename.
`ticket namecheck NAME` shows the parse and refuses bad names; `ticket namecheck --bank` lists
intent words. If a subject word is a bank word (`STALE_RENAME_GATES...` splits at RENAME),
rephrase. Rules: `bin/lib/ticketname.py` (copy of RimMandrake's `model.py`; keep in step).

Decision D30 (`~/dev/Lodestar/docs/DECISIONS.md`); design notes
are kept in the private store. Every project in
`~/dev/Lodestar/sync/ticket-homes.md` has tickets; the tool works from any directory.

## File one (one line)

```
python3 ~/dev/Lodestar/bin/ticket.py new --project lodestar --type build --name MODEL_DISPATCH_BUILD_CHEAPEST_FIRST "Intelligent model dispatch"
python3 ~/dev/Lodestar/bin/ticket.py new --project alpha --type action --due 2026-11-02 --name REVIEW_FORM_SUBMIT_BEFORE_DEADLINE "Submit the review form"
python3 ~/dev/Lodestar/bin/ticket.py new --project fetcher --type bug --likely-fix "retry on 429" --name SEARCH_RESULTS_FIX_SILENTLY_DROPPED "Search drops results"
```

(`ticket ...` is the same command in an interactive zsh.) Types: `build` (make the project or its
process better), `bug` (with `--likely-fix`), `repair` (ongoing repair or upgrade), `action` (a
dated real-world action, give `--due`/`--act-by`), `note` (a needed action, noted). You name it (below); it claims the
name, writes `tickets/<NAME>.md`, commits that one file and pushes. Print its one-line
answer to the user.

`--project` takes the registry Name, its Id or the code (`lodestar`, `LODE`, `PILLAR`). Options:
`--body TEXT` (or `--body -` for stdin), `--owner`, `--effort 30m`, `--next`, `--link commit:<sha>`
(also `pr:`, `handoff:<path>`, `loop:<id>`, `ob:<reckoning id>`), `--source agent` when an agent,
not Lukas, decided to file it.

## Another project's work: a need

```
python3 ~/dev/Lodestar/bin/ticket.py need --from opencodecli --to lodestar --name MODEL_DISPATCH_BUILD_CHEAPEST_FIRST "Intelligent model dispatch"
```

It lands in the target's inbox as `proposed`; the target accepts (`move ID open`) or declines
(`drop ID --said ...`). `ticket inbox --project lodestar` lists what waits.

## Work them

```
ticket list --project P [--mine] [--type bug] [--status blocked] [--closed]   ticket list --all
ticket show ID       ticket search WORDS       ticket inbox [--project P]
ticket move ID active|blocked|open           ticket edit ID --due D --next --note "..."
ticket close ID --said "his words" | --evidence commit:<sha>    ticket drop ID --said "..."
ticket link ID --blocked-by ID2 | --blocks ID2 | --relates ID2 | --ref commit:<sha>
ticket link ID --blocked-on "a rainy day" | "until 2026-11-01" | owner/repo#N [--host <private host>] | <issue URL>
ticket link ID --met "a rainy day" (--said "his words" | --evidence TEXT)
ticket link ID --unblock ID2   ticket ready [--project P | --all]   ticket close ID ... --release
ticket check [--project P]     ticket homes     ticket face   (the Companion TICKETS row)
```

## Dependencies

- `link --blocked-by` (or `--blocks`) writes both sides and moves an open or active dependent to
  `blocked` unless the blocker is already done. A link that would close a wait cycle is refused
  with the chain, across projects too.
- **Blockers need not be tickets** (LODE-37), same `blocked_by` field: `--blocked-on "a rainy day"`
  is a free-text condition that never resolves by itself; `ticket ready` lists it under "waiting on a
  condition" with its age, and `link ID --met "a rainy day" --said "..."` (or `--evidence`) ends it
  and logs who said so. `--blocked-on "until 2026-11-01"` (or a bare ISO date) clears when the date
  passes, and `ready` reports it cleared. `--blocked-on owner/Repo#12 --host <private host>`
  (or the issue URL; github.com and the private host only) clears when `gh` says the issue is
  closed; unreachable or offline reads "unknown", never "done". Commas are not allowed inside a
  condition. In a github.com session, private-host refs print as "private-host issue #N" and condition text
  is withheld. Only ticket ids join the cycle check and the `blocks` back-link.
- `ticket ready` lists status `open` tickets whose blockers are all done, then one line per
  `blocked` ticket whose blockers are all done ("unblocked by X, move it open?") and per live
  ticket with a dropped blocker ("blocker dropped, confirm").
- `close` and `drop` name each live dependent: every blocker done (move it open), still waiting,
  or blocker dropped (confirm). Nothing changes status unless `close --release` is given, which
  opens only the all-done ones. **A dropped blocker never releases a dependent**: a blocker
  ending is evidence, not proof. The human answers with `link ID --unblock X` (keep the work) or
  `drop ID` (it no longer makes sense).
- Moving out of `blocked` by hand is allowed and the log line records the blockers' states;
  `move ID open` warns when a blocker is not done.
- `ticket check` also flags blocked tickets with no `blocked_by`, `blocked_by` ids that resolve
  nowhere, stale blocks (every blocker done) and dropped blockers awaiting confirmation.

## Rules

- **Mail-born obligations are not tickets** (D29 T3, kept): reply-to-X, she-asked-for-Y stay in the
  Reckoning (`reckon.py`). A ticket may cite one with `--link ob:<id>`, never copy it.
- **private text never lands on github.com.** private-host projects (the private store and any repo
  pushing only to the private host) keep tickets in their repo; private work on a github.com repo keeps them in the
  private-store overlay; a github.com tool repo (Lodestar, Fetcher, …) refuses work markers and takes
  `--private` to file that one ticket in its overlay. In a github.com session, `list`/`show` of a
  private-host project print ids and dates only: do not work around that.
- **Closing needs a reason**: his words (`--said`) or evidence (`--evidence commit:<sha>`).
- **An undeclared project is refused by name**: add a row to `sync/ticket-homes.md` (Lodestar
  repo) with a unique code; Drive-only projects have no git repo and keep Lodestar board loops.
- **Commits carry the id and verb only** (`ticket: LODE-12 close`), never the title.
