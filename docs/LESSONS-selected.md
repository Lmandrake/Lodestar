# Selected lessons

Ten lessons from running a multi-machine agent fleet, each one a belief that was wrong. Dates are
omitted; the numbers are real.

## A registry row's own note is not evidence the scoping happened
A row said, in its own text, to scope it to two machines "if it is ever widened". Its `Machines`
column was blank, which means all machines. Private project files synced to a machine they were
meant never to reach, for a day, and it surfaced only as a generic "repository not found" flag. Read
the column the code reads, not the prose beside it.

## `--verify` that proves a file parses proves nothing about its fields
A calendar dump reported "319 files, 319 parsed, 0 skipped" while 236 of them lacked the one field
its docstring promised every file carried. The parser tolerated the absence, so the gate stayed green
and the data was silently unusable for its main purpose. Verify the fields a consumer needs.

## A probe that truncates its input manufactures a gap
Reconciling 958 files against 918 server ids, a census read the first 4,000 characters of each file
and reported five messages missing. Those files had huge recipient lists pushing the id line past
character 4,000. Reading whole files: 0 missing. The instrument, not the data, had the defect.

## `N errors during collection` means zero tests ran
pytest aborts the whole session and exits 2, and the tail of the log reads like an ordinary failure
tally. Four collection errors looked like four bad tests. Always check the summary says `passed`.

## A subsystem with green tests and no caller looks exactly like a working feature
A 145-line module shipped with 13 passing tests and zero production callers; not one checkout had
been changed by it. Two cheap checks expose this: grep for callers excluding `tests/`, and look
directly at the world it was supposed to change.

## A field named `bytes` must be `len(s.encode())`
A receipt said 13,479 where `git cat-file -s` said 13,493, because the text contained non-ASCII glyphs
and `len()` counts characters. The number answered a different question than its name implied. It
recurred in a second report the same day, because only the first had been grepped.

## A mutation harness must prove the mutation inverted the behaviour
A mutator moved a call site and located both lines by substring; the pattern matched the function's
own `def` line, so before and after read the same number. The mutation was real but measured against
the wrong landmark. Assert the relationship, print both numbers, match the call site uniquely.

## A test premise that expires because the feature worked can leave the test vacuous
A test asserted a key was absent from a snapshot, as proof that nothing else could supply it. Once the
feature worked, the key appeared and the test went red. Deleting that line is wrong: the remaining
equality check would also pass if the feature were removed. Measure what each assertion distinguishes
before editing it.

## Committed conflict markers are invisible to every git-state check
Git flags an unmerged index entry, not file contents. Once markers are committed the index is clean,
so `git status` and every wrapper built on it report a healthy tree with `<<<<<<<` in the source.
Only a content grep finds it.

## `gh repo rename --repo` leaves the local `origin` on the old name
Given an explicit repository, the rename succeeds on the host and touches nothing local. The old URL
keeps working through a redirect, so nothing fails and the drift is invisible until an audit compares
`origin` with the declared remote. Follow every such rename with `git remote set-url` and an update of
the declared remote.
