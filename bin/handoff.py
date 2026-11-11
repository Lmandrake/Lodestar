#!/usr/bin/env python3
"""Forwarder: the handoff tool now lives in MandrakeWorkflows. Full doc: docs/HANDOFF.md.

**The tool moved on 2026-11-10** (owner's ruling: the handoff skill AND its tool go to
MandrakeWorkflows, where his workflow skills live) to:

    ~/dev/MandrakeWorkflows/tools/handoff/handoff.py

This file stays because the ritual is wired into things that must not break for even one
commit: the SessionStart hook on two machines (`bin/session_start.py`), the handoff delete
guard, `compaction_watch.py`, `companion_rail.py`, `bin/lib/stopping.py`, and roughly 180
references across this repo and the private store — plus every `python3 ~/dev/Lodestar/bin/handoff.py`
line already written into CLAUDE.md files, skills, tickets and old handoffs on three machines.
A redirect that keeps all of those true costs 40 lines; rewriting them all at once, in repos
other sessions are editing, is how a ritual breaks silently on the machine nobody is watching.

So: **both paths work, and there is exactly one implementation.** This is not a copy — it
imports the real module and calls its `main()`, so there is nothing here to drift. Behaviour,
exit codes and stdout are identical.

New code should name the MandrakeWorkflows path. Nothing is urgent about changing old code.

It also sets LODESTAR_BIN, so the moved tool finds *this* checkout's libraries (the
private-store boundary, the pointer registry, the drafts reader) rather than guessing — the
same modules as before, still here, still optional. No boundary logic moved.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.expanduser("~"), "dev", "MandrakeWorkflows", "tools", "handoff")


def main():
    # Point the moved tool back at this checkout's bin/, whichever checkout that is.
    os.environ.setdefault("LODESTAR_BIN", HERE)
    if not os.path.isfile(os.path.join(TOOL, "handoff.py")):
        sys.stderr.write(
            "REFUSED: the handoff tool moved to %s on 2026-11-10 and is not there.\n"
            "Clone MandrakeWorkflows (sync/registry.md row 'MandrakeWorkflows'), or run the\n"
            "ritual by hand: docs/HANDOFF.md holds the content contract.\n" % TOOL)
        return 1
    if TOOL not in sys.path:
        sys.path.insert(0, TOOL)
    import handoff as real
    return real.main()


if __name__ == "__main__":
    sys.exit(main())
