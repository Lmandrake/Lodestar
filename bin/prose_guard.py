#!/usr/bin/env python3
"""Lodestar's private-text guard: board prose never reaches github.com (ruling 5, 2026-10-13).

    bin/prose_guard.py --staged     # the pre-commit hook: refuse an unsafe staged change
    bin/prose_guard.py --outgoing   # the pre-push hook: refuse unpushed commits that fail
    bin/prose_guard.py --check      # report unsafe changes in the working tree; exit 1 if any
    bin/prose_guard.py --fix        # move loop prose / journal notes out of the working tree
    bin/prose_guard.py --migrate    # one-time: everything at HEAD (needs the private store)

Loop prose lives in `<private store>/projects/Lodestar/loops/<id>.md`; edit it there. A
loop file in Lodestar holds frontmatter and one pointer line. See bin/lib/proseguard.py.
"""

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib import proseguard  # noqa: E402


def _repo() -> str:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                         text=True)
    if out.returncode != 0:
        sys.exit("not inside a git repository")
    return os.path.realpath(out.stdout.strip())


def _changed(repo: str):
    out = subprocess.run(["git", "status", "--porcelain", "-z", "--untracked-files=all"],
                         cwd=repo, capture_output=True, text=True).stdout
    paths, parts, i = [], out.split("\0"), 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if code[0] in "RC":
            i += 1  # the rename's source follows
        if "D" not in code:
            paths.append(path)
    return paths


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--fix", action="store_true")
    mode.add_argument("--outgoing", action="store_true",
                      help="the pre-push hook: refuse unpushed commits that fail the guard")
    mode.add_argument("--migrate", action="store_true",
                      help="one-time: every loop body and journal note at HEAD to the private store")
    args = parser.parse_args(argv)

    repo = _repo()
    if not proseguard.applies(repo):
        return 0
    if args.migrate:
        print(proseguard.migrate(repo))
        return 0
    if args.fix:
        lines = proseguard.fix(repo, _changed(repo))
        for line in lines:
            print(line)
        print("%d file(s) repaired" % len(lines))
        return 0

    try:
        if args.outgoing:
            found = proseguard.outgoing_violations(repo)
        elif args.staged:
            found = proseguard.violations(repo, proseguard.staged_paths(repo), staged=True)
        else:
            found = proseguard.violations(repo, _changed(repo))
    except proseguard.GuardError as error:
        print("REFUSED: the private-text guard could not check (%s)" % error, file=sys.stderr)
        return 1
    if not found:
        return 0
    print("REFUSED by Lodestar's private-text guard (ruling 5, 2026-10-13): Lodestar's remote is "
          "github.com.", file=sys.stderr)
    for path, why in found:
        print("  %s — %s" % (path, why), file=sys.stderr)
    print("\nLoop prose and journal notes: `python3 bin/prose_guard.py --fix`, re-stage, commit.\n"
          "Edit loop prose in <private store>/projects/Lodestar/loops/<id>.md from now on.\n"
          "A work path or marker: move the text to the private store and leave an id here.",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
