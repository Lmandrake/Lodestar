#!/usr/bin/env python3
"""PreToolUse hook (Bash): refuse commands that delete or move files under a `handoffs/` directory.

Incident 2026-10-19: subagents deleted 40 handoff files outside `handoff.py --cull`, losing 18
live pointers. The only sanctioned route is `~/dev/Lodestar/bin/handoff.py --cull --apply`, which
deletes in-process (never through Bash, so this hook cannot see it) after checking supersession
and lessons-filing.

Pragmatic, not airtight: a command segment (split on ; && || | newline) that both names a
`handoffs` path component and contains a delete/move verb (rm, unlink, mv, git rm/mv, find
-delete, shred, trash, os.remove/unlink/rename, shutil.rmtree/move, Path.unlink) is refused.

Override (one shot, per command): append the shell comment `# handoff-delete-ok`.
Contract: payload on stdin; exit 0 allow, exit 2 refuse with reason on stderr. Unparseable -> 0.
"""

import json
import re
import sys

OVERRIDE = "# handoff-delete-ok"
SEGMENT_SPLIT = re.compile(r"\|\||&&|[;|\n]")
HANDOFFS = re.compile(r"(?:^|[/\s'\"=(,\[~])handoffs(?:[/\s'\"*)\],]|$)")
VERBS = re.compile(
    r"(?:^|[\s;&|(`'\"])(?:rm|unlink|rmdir|mv|shred|trash|srm)(?=[\s'\"]|$)"
    r"|\bgit\s+(?:-C\s+\S+\s+)?(?:rm|mv|clean)\b"
    r"|\bfind\b[^\n]*(?:-delete|-exec\s+(?:rm|mv|unlink))"
    r"|\bos\.(?:remove|unlink|rename|replace|rmdir)\b"
    r"|\bshutil\.(?:rmtree|move)\b"
    r"|\.unlink\(|\.rmdir\(|\.rename\(|\.replace\("
)


DELETERS = {"rm", "unlink", "rmdir", "mv", "shred", "trash", "srm"}
INTERPRETERS = re.compile(r"^(?:python[\d.]*|node|perl|ruby|php)$")


def _regex_hit(command: str) -> bool:
    """Old quote-blind check; used only as fallback and for inline interpreter code."""
    return any(HANDOFFS.search(seg) and VERBS.search(seg) for seg in SEGMENT_SPLIT.split(command))


def _lexed_hit(command: str):
    """True/False by shell_lex (quoted text is data); raises if the lexer is unavailable."""
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (os.path.expanduser(os.path.join(os.environ.get("MACHINIST_DIR", ""), "tools", "hooks")),
              os.path.join(here, "..", "..", "..", "Machinist", "tools", "hooks")):
        if os.path.isfile(os.path.join(d, "shell_lex.py")):
            sys.path.insert(0, d)
            break
    import shell_lex
    for words in shell_lex.commands(command):
        k = shell_lex.resolve(words)
        if k is None:
            continue
        base = os.path.basename(words[k])
        args = words[k + 1:]
        touches = any(HANDOFFS.search(" " + a + " ") for a in args)
        if base in DELETERS:
            if touches:
                return True
        elif base == "git":
            i = 0
            while i < len(args) and args[i].startswith("-"):
                i += 2 if args[i] in ("-C", "-c") else 1
            sub = args[i] if i < len(args) else ""
            rest = args[i + 1:]
            if sub in ("rm", "mv", "clean") and any(HANDOFFS.search(" " + a + " ") for a in rest):
                if sub == "rm" and "--cached" in rest:
                    continue  # index only; deletes nothing on disk
                return True
        elif base == "find":
            if touches and ("-delete" in args or any(
                    a == "-exec" and j + 1 < len(args) and os.path.basename(args[j + 1]) in DELETERS
                    for j, a in enumerate(args))):
                return True
        elif INTERPRETERS.match(base):
            if "-c" in args or "-e" in args:
                code = " ".join(args)
                if _regex_hit(code):
                    return True
            elif not [a for a in args if not a.startswith("-")]:
                if _regex_hit(command):  # code arrives on stdin/heredoc
                    return True
            # else: runs a script file; the rest are its arguments, i.e. data
    return False


def decide(command: str):
    """Return a refusal reason, or None to allow."""
    if OVERRIDE in command:
        return None
    try:
        hit = _lexed_hit(command)
    except Exception:
        hit = _regex_hit(command)
    if hit:
        if True:
            return (
                "REFUSED: this command deletes or moves files under a handoffs/ directory. "
                "Handoffs carry live pointers; 40 were deleted by hand on 2026-10-19. "
                "Use the sanctioned route: python3 ~/dev/Lodestar/bin/handoff.py --cull "
                "(dry run) then --cull --apply. If the owner truly asked for this deletion, "
                "append the comment `%s` to the command." % OVERRIDE
            )
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
        if event.get("tool_name") != "Bash":
            return 0
        command = str((event.get("tool_input") or {}).get("command") or "")
    except Exception:
        return 0
    reason = decide(command)
    if reason:
        sys.stderr.write(reason + "\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
