# Sync participation registry (demo fleet)

Which repos the sweep (`bin/sync.py`) may pull, auto-commit and push. `Send` and `Receive` are the two switches; a blank `Machines` means every machine.

| Name | Path | Branch | Send | Receive | Machines | Id | Former | Remote | State | Mirror | Notes |
|------|------|--------|------|---------|----------|----|--------|--------|-------|--------|-------|
| Lodestar | ~/dev/Lodestar | main | on | on |  | lodestar |  | @REMOTES@/Lodestar.git | active |  | this board |
| Atelier | ~/dev/Atelier | main | on | on |  | atelier |  | @REMOTES@/Atelier.git | active |  | sketches and the field-sketch skill |
| Tidepool | ~/dev/Tidepool | main | on | on |  | tidepool |  | @REMOTES@/Tidepool.git | active |  | tide notes and the tide-tables skill |
| Lantern | ~/dev/Lantern | main | on | on | studio,laptop | lantern |  | @REMOTES@/Lantern.git | active |  | a skill that IS a repo root |
| Notebook | ~/dev/Notebook | main | on | on |  | fieldnotes | Fieldnotes, @REMOTES@/Fieldnotes.git | @REMOTES@/Notebook.git | active |  | renamed to Notebook by plan rename-fieldnotes-demo |
| Orchard | ~/dev/Orchard | main | on | on |  | orchard |  | @REMOTES@/Orchard.git | active |  | plain notes |
| Quarry | ~/dev/Quarry | main | on | on | tower | quarry |  | @REMOTES@/Quarry.git | active |  | lives on the tower only |

## Machine paths

| Repo | Machine | Path |
|------|---------|------|
