# Lodestar

*The fixed star you steer by.*

Lodestar is an operating layer for one person who runs a fleet of AI coding agents across several
machines. It is a git repository of plain markdown, JSON and stdlib Python that every agent session
on every machine checks into. It keeps the machines converged, keeps each agent's configuration and
skills consistent, tracks work on a shared board, and refuses to let private prose reach a public
history. It is for anyone who runs Claude Code (or a similar agent) on more than one computer and
has felt the sprawl: drifting config files, work stranded on the wrong machine, handoffs that say
"saved" when nothing was pushed.

It was built by one person and his agents, for himself, and is in daily use. It is shared because
its parts are worth copying, not because it is a turnkey product (see Status below).

## What is worth your attention

- **Machines converge without a central server.** A markdown table declares the fleet. An hourly
  sweep on each machine fetches, fast-forwards, commits and pushes, and never forces. A rename,
  retirement or purge is approved once and every machine carries it out on its own, including one
  that was asleep at the time. If upstream history is deliberately rewritten, the sweep halts
  instead of merging the old history back in.
- **"Saved" means "pushed."** The handoff ritual ends a session by writing pointers in a fixed
  shape (`` `ID` — state; NEXT: <one imperative action> ``) and refuses to call the handoff ready
  until everything is committed and pushed. The format came from an audit of 67 real handoffs: see
  [docs/HANDOFF.md](docs/HANDOFF.md) for the three-arm measurement behind it.
- **Config is rendered, not copied.** Each machine's `~/.claude/CLAUDE.md` is assembled from
  fragments and per-machine overlays, drift-checked and snapshotted. A single table drives the
  skill symlinks, and `check.py` flags a row Claude Code would silently ignore.
- **Private prose cannot reach a public history by construction.** Loop bodies and journal notes
  live outside the repo; pre-commit and pre-push hooks refuse what slips through; and a
  default-deny exporter with scanners, canary tests and a cold-read by a second model gates every
  release. This repository was exported through that pipeline. Zero scanner hits was not a clean
  export: the first real run of the exporter on a sibling repo scanned clean and an independent
  read still found 26 issues ([docs/TOUR.md](docs/TOUR.md), section 5).
- **A record of instruments that were wrong.** Most of the lessons here came from checkers, not
  from the things checked: a verifier that proved parsing and not content (236 of 319 files parsed
  and lacked the promised field), a probe that truncated input and invented five missing messages.
  A selection is in [docs/LESSONS-selected.md](docs/LESSONS-selected.md).

## Quick start

Needs Python 3.11+ and git. Nothing to install.

```sh
git clone https://github.com/Lmandrake/Lodestar.git && cd Lodestar
python3 run_tests.py                 # the test suite
python3 bin/demo_fleet.py --tour     # three invented machines, real commands, about 45 seconds
```

The tour builds a sandbox with a studio, a laptop and a tower, converges them, renames a repo
fleet-wide, renders three different `CLAUDE.md` files, links skills and shows `check.py` catching a
broken row. It touches nothing outside a temporary directory, and prints the sandbox path so you
can keep poking. [docs/TOUR.md](docs/TOUR.md) explains each step and why it is built that way.

## Map of the repository

| Path | What is there |
|---|---|
| `bin/` | The command-line tools (`sync.py`, `claudeconf.py`, `handoff.py`, `publish.py`, `check.py`, `demo_fleet.py` and others) and `bin/lib/`, the library modules behind them |
| `sync/` | The sweep's support files: LaunchAgent and systemd units |
| `.githooks/` | The pre-commit and pre-push private-prose guards |
| `examples/demo-fleet/` | The invented fleet the tour runs against |
| `tests/`, `run_tests.py` | The test suite, stdlib only |
| `docs/` | The tour, the sync design, the handoff ritual, selected lessons, the ticket and handoff skill docs |
| `CLAUDE.md` | The operating ritual an agent follows when working in this repository |

Read next: [docs/TOUR.md](docs/TOUR.md) (guided walkthrough),
[docs/MULTI_MACHINE_SYNC.md](docs/MULTI_MACHINE_SYNC.md) (sync design),
[docs/HANDOFF.md](docs/HANDOFF.md) (handoff ritual),
[docs/LESSONS-selected.md](docs/LESSONS-selected.md) (lessons).

## Status and limits

- **One user.** N = 1. Many rules exist because of one incident on one machine, and much of it
  encodes one person's working rhythm.
- **Not portable as-is.** Real use assumes the author's layout (`~/dev/<Repo>` siblings), his
  machines and his accounts. The demo fleet shows the mechanisms without any of that.
- **Parts are missing from this tree on purpose.** The fleet registry, the author's real
  configuration, his board and his intake pipeline for mail and chat stay private, so some
  modules you will see referenced are stubs or absent here.
- **Not a library.** Nothing is packaged or versioned. Copy ideas and files; do not depend on it.
- **Python.** The demo and tests need 3.11+. The author's own sweeps run on the system Python 3.9.

## License

MIT. See `LICENSE`.

Please credit me if you find this work useful.
