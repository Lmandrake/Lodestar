#!/usr/bin/env python3
"""SessionEnd hook: write this session's conversation record into the project it concerned.

Wired into `claude/settings-base.json` under `hooks.SessionEnd`, so it converges onto every
machine whose settings Lodestar manages. Reads the hook payload on stdin — `transcript_path`,
`cwd`, `session_id`, `reason` — and renders the record via `lib/conversations.py`.

Four constraints shape every line of this, all of them measured or documented:

1. **It never runs git.** `SessionEnd` shares a 1.5 s budget by default, and this path would
   be taking a git lock in a tree that concurrent agents and the hourly sweep also write.
   `conversations/*.md` is sweepable, so the sweep commits it within the hour. CLAUDE.md
   already records what a merge in a checkout another agent is editing costs.

2. **It always exits 0.** Exit 2 surfaces stderr to the user as an error notice, and a
   failure to record a conversation is not worth interrupting someone over. Everything else
   goes to the debug log only, so this is nearly invisible either way — which is exactly why
   `sync.conversations_step` re-derives records unattended rather than trusting this hook.

3. **It tolerates an unflushed transcript.** There is no documented guarantee that
   `transcript_path` is complete when `SessionEnd` fires — the async-lag warning sits on the
   common field, and unlike `Stop` there is no `last_assistant_message` to fall back on. The
   writer is idempotent, so a partial render here is simply superseded later.

4. **It does not fire at all on SIGKILL or power loss.** The sweep is the backstop for that,
   and for `reason="clear"` mid-session, and for any session nobody handed off.

`reason` is recorded but never gates the write: `/clear` and `/resume` both end a session
whose dialog is just as worth keeping as one exited at the prompt.
"""

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))  # bin/hooks -> bin

try:
    from lib import conversations
except Exception as error:  # pragma: no cover - a broken import must not break exit
    print("conversation-record: cannot import lib.conversations: %s" % error,
          file=sys.stderr)
    sys.exit(0)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception as error:
        print("conversation-record: unreadable payload: %s" % error, file=sys.stderr)
        return 0

    transcript = payload.get("transcript_path")
    if not transcript or not os.path.isfile(transcript):
        print("conversation-record: no readable transcript_path", file=sys.stderr)
        return 0

    try:
        where = conversations.attribute(transcript)
    except Exception as error:
        print("conversation-record: attribution failed: %s" % error, file=sys.stderr)
        return 0

    if not where.root:
        # The common, correct case for a session launched from an unrooted directory.
        print("conversation-record: not recorded (%s)" % where.reason, file=sys.stderr)
        return 0

    try:
        target, changed = conversations.write_record(where.root, transcript)
    except Exception as error:
        print("conversation-record: write failed: %s" % error, file=sys.stderr)
        return 0

    print("conversation-record: %s %s (%s, reason=%s)"
          % ("wrote" if changed else "current", target, where.kind,
             payload.get("reason", "?")),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
