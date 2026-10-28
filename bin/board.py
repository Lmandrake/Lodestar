#!/usr/bin/env python3
"""Regenerate BOARD.md from loops/ and journal/.

    board.py                 # render BOARD.md
    board.py --rebuild       # re-derive loop state from the journal, then render
    board.py --check         # render and report whether BOARD.md is already current

`--rebuild` is the event-sourcing recovery path. It restores each loop's *frontmatter* from
the journal and **preserves the existing body**, because prose never enters the journal
(decision D-F). A loop present in the journal but missing its file is recreated with state
only and an empty body.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib import frontmatter, journal, loops, paths, render, selfsync, shadow  # noqa: E402


def newest_ts(events):
    return events[-1].get("ts") if events else None


def rebuild(events):
    """Rewrite loop frontmatter from the journal, preserving bodies. Returns a report."""
    derived = loops.derive(events)
    paths.ensure_dir(paths.loops_dir())
    written, created = [], []
    for loop_id, fields in sorted(derived.items()):
        path = paths.loop_file(loop_id)
        body = ""
        if os.path.exists(path):
            try:
                _, body = frontmatter.read_file(path)
            except (OSError, ValueError):
                body = ""  # unreadable body; state is still recoverable
        else:
            created.append(loop_id)
        frontmatter.write_file(path, fields, body)
        written.append(loop_id)
    return written, created


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rebuild", action="store_true",
                       help="re-derive loop state from the journal before rendering")
    parser.add_argument("--check", action="store_true",
                       help="do not write; exit 1 if BOARD.md is out of date")
    parser.add_argument("--sync", action="store_true",
                       help="pull first, and commit+push the regenerated board")
    args = parser.parse_args(argv)

    if args.sync and not args.check:
        ok, note = selfsync.pull()
        print("pull: %s" % note)
        if not ok:
            return 1

    events, problems = journal.load()

    if args.rebuild:
        written, created = rebuild(events)
        print("rebuilt %d loop file(s) from %d journal event(s)" % (len(written), len(events)))
        if created:
            print("  recreated from journal (body empty): %s" % ", ".join(created))
        print("  note: bodies are preserved, not reconstructed — prose is not journaled")

    # The shadow line is derived from the same events, so `board.py` and `shadow.py` render a
    # byte-identical BOARD.md and never fight over it.
    text = render.board(loops.all_loops(), newest_ts(events), problems,
                        shadow.board_line(events))
    target = paths.board_file()

    if args.check:
        current = ""
        if os.path.exists(target):
            with open(target, encoding="utf-8") as handle:
                current = handle.read()
        if current == text:
            print("BOARD.md is current")
            return 0
        print("BOARD.md is out of date — run board.py to regenerate")
        return 1

    with open(target, "w", encoding="utf-8") as handle:
        handle.write(text)
    print("wrote %s" % target)

    for path, reason in problems:
        print("  journal problem: %s — %s" % (path, reason), file=sys.stderr)

    if args.sync:
        ok, note = selfsync.commit_and_push("chore(board): regenerate BOARD.md")
        print("push: %s" % note)
        if not ok:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
