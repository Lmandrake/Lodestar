# Lodestar

*The fixed star you steer by.*

Lodestar is an operating layer that lets one person run a fleet of AI coding agents across several
machines. It is a git repository of plain markdown, JSON and stdlib Python that every agent session
on every machine checks into. It keeps the machines converged, keeps each agent's configuration and
skills consistent, tracks work on a shared board, and refuses to let private prose reach a public
history.

Built by one person and his agents, for himself, in daily use. N = 1: it is shared because the
parts are worth copying, not because it is a turnkey product. Expect paths and habits that assume
the author's layout (`~/dev/<Repo>` siblings).

## What it does

- **Multi-machine sync.** A markdown registry declares the fleet. An hourly sweep on each machine
  fetches, fast-forwards, commits and pushes, never forcing. Renames, rehomes, retirements,
  quarantines and purges are approved once and every machine converges on its own, including ones
  that were asleep. A deliberately rewritten upstream halts the sweep instead of merging old
  history back in.
- **CLAUDE.md management.** Global rules live as fragments, with per-machine overlays selected by
  label. Each machine's `~/.claude/CLAUDE.md` is rendered, checked for drift and snapshotted.
- **Skill auto-linking.** A table drives the `~/.claude/skills` symlinks across machines;
  `check.py` flags a dangling row that Claude Code would silently ignore.
- **Board, loops, tickets, handoffs.** An append-only journal and per-loop files merge cleanly
  across machines. A handoff gate makes "saved" mean "pushed".
- **A private-prose boundary.** Pre-commit and pre-push guards, a private overlay, and a
  default-deny exporter with scanners, canary tests and a cold-read gate. This repository was
  exported through it.

## 60-second quickstart

Needs Python 3.11+ and git. Nothing to install.

```sh
git clone <this repo> Lodestar && cd Lodestar
python3 run_tests.py                 # the test suite
python3 bin/demo_fleet.py --tour     # three invented machines, real commands, about 45 seconds
```

The tour builds a sandbox with a studio, a laptop and a tower, converges them, renames a repo
fleet-wide, renders three different `CLAUDE.md` files, links skills and shows `check.py` catching a
broken one. It touches nothing outside a temporary directory.

## Where to go next

- [docs/TOUR.md](docs/TOUR.md): the guided walkthrough, with commands and the reasons behind them.
- [docs/MULTI_MACHINE_SYNC.md](docs/MULTI_MACHINE_SYNC.md): the sync design.
- [docs/HANDOFF.md](docs/HANDOFF.md): the handoff ritual.
- [docs/LESSONS-selected.md](docs/LESSONS-selected.md): a selection of hard-won lessons.
- [CLAUDE.md](CLAUDE.md): the operating ritual an agent follows in this repo.

## Credit

Please credit me if you find this work useful.
