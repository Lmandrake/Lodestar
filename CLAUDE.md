# Lodestar: operating ritual for agents working in this repo

Lodestar keeps a fleet of machines and the agents on them consistent. If you are a coding agent
arriving here, read this file, then `docs/TOUR.md`.

## Orient

- Python 3.11+, stdlib only, no installs. `python3 run_tests.py` runs the suite; read the summary
  line, because `N errors during collection` means zero tests ran.
- `python3 bin/demo_fleet.py --tour` runs the real commands against an invented three-machine fleet.
  Use it to see behaviour before reading code.
- Many paths assume `~/dev/<Repo>` siblings. The private store (loop bodies, journal notes, publish
  lists) is found by `bin/lib/privatestore.py`, configured by `sync/private-store.json`
  (`bin/lib/boundary.py` documents the keys); without either the suite still passes and the guards still
  refuse unsafe changes.

## Working rules

1. **Pull before, push after.** The board is shared state. Commit named paths only, never blanket-stage,
   never force-push, never merge in a checkout other agents are editing.
2. **Keep state true the moment you learn it.** A finding goes into the ticket, loop or lessons file
   when learned, not only into a chat reply.
3. **Every loop carries a time-to-close estimate.** There is no cap on active loops.
4. **Do the uncontested thing; check first only for shared state or anything irreversible.** Never
   re-verify your own successful command. But when the question is "is this true", check.
5. **Spend the smallest model that can do the step.** A cheap producer needs a mechanical check on its
   output, and a failed check escalates.
6. **Never grade your own work with something you can edit.** A check that passed before and fails now
   is a stop: report both states.

## Standing facts

- **A flagged row in the sweep report means nothing from that machine is reaching the fleet.** Read
  the sweep status before anything else.
- **The sweep does not commit source.** Code and `docs/` are withheld from it; commit them yourself.
- **Prose never reaches the public history.** Loop bodies and journal notes live in the private store.
  If the pre-commit guard refuses, run `python3 bin/prose_guard.py --fix`; never bypass the hook.
- **A skill installed by hand reaches one machine.** Add a row to the skills table instead.
- **A dry run writes no receipt.** A receipt marks work done.
- **A subsystem with green tests and no caller is not a feature.** Count callers outside `tests/`.
- **Count by parsing, never by matching text.** Use the format's parser.
- **A shared working directory means the branch is not yours.** Re-derive your base commit.

## Handoff

When a long stretch of work ends, run `python3 bin/handoff.py --check`. A handoff is ready only when
everything is committed and pushed, helpers have reported, and each open pointer reads
`ID: state; NEXT: one imperative action`. On waking, `python3 bin/handoff.py --wake`.
