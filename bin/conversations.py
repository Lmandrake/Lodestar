#!/usr/bin/env python3
"""Write the readable record of a Claude Code conversation into the project it concerned.

One installed copy serves every repo: the target is resolved from the working directory
the same way `bin/handoff.py` does it, so nothing is copied into a project.

    bin/conversations.py                  # this repo's most recent session
    bin/conversations.py --all            # every session attributable to this repo
    bin/conversations.py --session <id>   # one named session
    bin/conversations.py --repo ~/dev/X   # another project
    bin/conversations.py --status         # what is recorded and what is not
    bin/conversations.py --review         # scan records before making a repo public
    bin/conversations.py --dry-run        # say what would change, write nothing

**Where the records go** is `lib.conversations.record_dir`: inside the repo only when the repo
pushes solely to the private host (or on a machine with no private access); otherwise in the private-hosted
overlay `<private store>/projects/<Repo>/conversations/` (ruling 5, 2026-10-13).

**It never commits; who does depends on where the record landed.**
- In the private-hosted overlay (every github.com repo on a work machine, ruling 5): the hourly sweep
  commits and pushes it in the private store (a send-on repo).
- In-tree in a send-on repo: the hourly sweep commits it with the rest of the board state.
- In-tree in a send-off repo (this machine only follows it): the sweep never commits there, so
  the repo's own handoff must (RimMandrake: `conversations_land.py --apply`).
Committing here would mean taking a git lock in a path that also runs from
a `SessionEnd` hook under a 1.5 s budget, racing whatever peer holds `index.lock` — the
hazard CLAUDE.md's merge rules already record. Writing the file is this tool's whole job.

Re-running is always safe and usually free: the writer compares before writing, so an
unchanged transcript produces no write at all.
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from lib import conversations  # noqa: E402

#: Content worth a human's eye before a repo becomes public. Deliberately broad and
#: deliberately not a gate: this prints candidates for review, it does not certify a
#: record as safe. The owner's ruling put the obligation on the visibility
#: change itself (a review as for code and configs), so the
#: honest contract here is "here is what to look at", never "this is clean".
#: Plain words, matched case-insensitively. Deliberately NOT a set of provider-specific
#: credential regexes: those go stale, miss anything unanticipated, and invite the reader
#: to treat a clean run as proof. A short vocabulary of words that tend to sit near
#: material worth a second look is honest about being a prompt rather than a detector.
REVIEW_WORDS = (
    "private key", "credential", "password", "passphrase",
    "api key", "access token", "bearer",
    "proprietary", "not for distribution", "itar", "export control",
)


def resolve_repo(explicit=None):
    """The project directory to act on: an explicit `--repo`, else the cwd's repo root."""
    if explicit:
        path = os.path.realpath(os.path.expanduser(explicit))
        if not os.path.isdir(path):
            sys.exit("no such directory: %s" % path)
        return path
    root = conversations.repo_root_of(os.getcwd())
    if root:
        return root
    here = os.path.realpath(os.getcwd())
    if conversations.is_drive_path(here):
        return here
    sys.exit("not inside a git repository or a Drive project, and no --repo given.\n"
             "A session launched from an unrooted directory is throwaway by standing rule; "
             "pass --repo to name a project explicitly.")


def cmd_status(repo):
    sessions = conversations.sessions_for(repo)
    directory = conversations.transcript_dir(repo)
    print("repo:        %s" % repo)
    print("transcripts: %s" % directory)
    try:
        print("records:     %s" % conversations.record_dir(repo))
    except conversations.Withheld as error:
        print("records:     NONE — %s" % error)
        return 1
    if not os.path.isdir(directory):
        print("\nNo Claude Code transcript directory for this project yet.")
        return 0
    seen = len(conversations.transcripts_for(repo))
    print("%d transcript(s) present, %d attributable to this project\n" % (seen, len(sessions)))
    missing = 0
    for path in sessions:
        target = conversations.record_path(repo, path)
        _, changed = conversations.write_record(repo, path, dry_run=True)
        if changed:
            missing += 1
        mark = "needs write" if changed else "current"
        print("  %-12s %s" % (mark, os.path.basename(target)))
    print("\n%d record(s) would change." % missing)
    return 0


def cmd_review(repo):
    """Print review candidates in this repo's records. Exit 1 when any are found."""
    try:
        directory = conversations.record_dir(repo)
    except conversations.Withheld as error:
        print(error)
        return 1
    if not os.path.isdir(directory):
        print("no records in %s" % directory)
        return 0
    # 🔴 Word boundaries, not bare substrings. Measured on a 29-record send-off repo:
    # without `\b`, "itar" matched inside "mil-itar-y" and produced 5 of 7 hits. A review
    # that cries wolf is one the reader learns to skip, which is the unread-flags failure
    # sync.py's own ABSENT comment already records.
    compiled = [(re.compile(r"\b%s\b" % re.escape(word), re.I), word)
                for word in REVIEW_WORDS]
    hits = 0
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".md"):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except OSError as error:
            print("  unreadable: %s (%s)" % (name, error))
            continue
        for number, line in enumerate(lines, 1):
            for pattern, label in compiled:
                if pattern.search(line):
                    hits += 1
                    print("  %s:%d  %s" % (name, number, label))
                    break
    print()
    if hits:
        print("%d line(s) to look at before this repo is published." % hits)
        print("This is a list of candidates, not a verdict — read them.")
        return 1
    print("No pattern matches. That is not a certification: a record can quote anything "
          "that was discussed, and only you can judge it.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Write the readable record of a Claude Code conversation into the "
                    "project it concerned.")
    parser.add_argument("--repo", help="project directory (default: the cwd's repo root)")
    parser.add_argument("--session", help="render only this session id (prefix matches)")
    parser.add_argument("--all", action="store_true",
                        help="render every session for this project — this is the backfill")
    parser.add_argument("--status", action="store_true",
                        help="report what is recorded and what is not; writes nothing")
    parser.add_argument("--review", action="store_true",
                        help="scan this project's records for things to check before "
                             "making the repo public")
    parser.add_argument("--speaker", default="Lukas",
                        help="how to label the human's turns (default: Lukas)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be written, write nothing")
    args = parser.parse_args(argv)

    repo = resolve_repo(args.repo)
    if args.status:
        return cmd_status(repo)
    if args.review:
        return cmd_review(repo)

    sessions = conversations.sessions_for(repo)
    if not sessions:
        print("no transcripts attributable to %s" % repo)
        return 0

    if args.session:
        sessions = [p for p in sessions
                    if conversations.session_id(p).startswith(args.session)]
        if not sessions:
            sys.exit("no session matching %r for %s" % (args.session, repo))
    elif not args.all:
        sessions = sessions[-1:]

    try:
        where = conversations.record_dir(repo)
    except conversations.Withheld as error:
        # Never a fallback into the repo: that is the leak the rule exists to stop.
        print("NOT WRITTEN: %s" % error, file=sys.stderr)
        return 2
    if not where.startswith(repo + os.sep):
        print("records go to %s (private host; this repo can push to github.com)" % where)
    wrote = 0
    for path in sessions:
        target, changed = conversations.write_record(
            repo, path, speaker=args.speaker, dry_run=args.dry_run)
        relative = os.path.relpath(target, repo) if target.startswith(repo + os.sep) \
            else target
        if changed:
            wrote += 1
            print("%s %s" % ("would write" if args.dry_run else "wrote", relative))
        else:
            print("current     %s" % relative)
    if not args.dry_run and wrote:
        print("\n%d record(s) written, not committed. Overlay (private store) or send-on "
              "repo: the hourly sweep commits them. Send-off repo with in-tree "
              "records: its own handoff must commit them (RimMandrake: "
              "conversations_land.py --apply)." % wrote)
    return 0


if __name__ == "__main__":
    sys.exit(main())
