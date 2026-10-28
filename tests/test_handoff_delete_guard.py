"""Tests for bin/hooks/handoff_delete_guard.py."""

import json
import os
import subprocess
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(REPO, "bin", "hooks", "handoff_delete_guard.py")
sys.path.insert(0, os.path.join(REPO, "bin", "hooks"))
import handoff_delete_guard as guard  # noqa: E402
from tests import needs_private  # noqa: E402


def _lexer_present():
    here = os.path.dirname(os.path.abspath(__file__))
    return any(os.path.isfile(os.path.join(d, "shell_lex.py")) for d in (
        os.path.expanduser("~/dev/Machinist/tools/hooks"),
        os.path.join(here, "..", "..", "Machinist", "tools", "hooks")))


class GuardTest(unittest.TestCase):
    @unittest.skipUnless(_lexer_present(), "needs the external shell_lex module (quote-aware lexing); "
                                           "the regex fallback is quote-blind by design")
    def test_quote_blind_false_positives_allowed(self):
        for cmd in [
            "git rm --cached handoffs/x.md",
            "python3 ticket.py new --body 'mentions rm handoffs/a and git rm handoffs/x'",
            'echo "rm handoffs/a"',
        ]:
            self.assertIsNone(guard.decide(cmd), cmd)

    def test_refuses_deleting_forms(self):
        for cmd in [
            "rm ~/dev/Foo/handoffs/a.md",
            "rm -f handoffs/*.md",
            "cd x && git rm handoffs/a.md",
            "git -C /r rm -r handoffs",
            "mv handoffs/a.md /tmp/",
            "find ~/dev/Foo/handoffs -name '*.md' -delete",
            "find handoffs -type f -exec rm {} +",
            "unlink ./handoffs/a.md",
            "python3 -c \"import os; os.remove('/r/handoffs/a.md')\"",
            "python3 -c \"import shutil; shutil.rmtree('handoffs')\"",
            "python3 -c \"from pathlib import Path; Path('handoffs/a.md').unlink()\"",
        ]:
            self.assertIsNotNone(guard.decide(cmd), cmd)

    def test_allows_harmless_and_sanctioned(self):
        for cmd in [
            "ls handoffs/",
            "cat handoffs/a.md",
            "python3 ~/dev/Lodestar/bin/handoff.py --cull --apply",
            "rm /tmp/scratch.txt",
            "git rm src/old.py",
            "git add handoffs/a.md && git commit -m x",
            "mv notes.md archive/",
        ]:
            self.assertIsNone(guard.decide(cmd), cmd)

    def test_override_token(self):
        self.assertIsNone(guard.decide("rm handoffs/a.md  # handoff-delete-ok"))

    def _run(self, payload):
        return subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                              capture_output=True, text=True)

    def test_exit_codes_and_pointer(self):
        bad = self._run({"tool_name": "Bash", "tool_input": {"command": "rm handoffs/x.md"}})
        self.assertEqual(bad.returncode, 2)
        self.assertIn("handoff.py --cull", bad.stderr)
        self.assertEqual(self._run({"tool_name": "Bash", "tool_input": {"command": "ls"}}).returncode, 0)
        self.assertEqual(self._run({"tool_name": "Write", "tool_input": {}}).returncode, 0)
        junk = subprocess.run([sys.executable, HOOK], input="not json", capture_output=True, text=True)
        self.assertEqual(junk.returncode, 0)

    @needs_private("claude/settings-base.json")
    def test_wired_into_settings_base(self):
        with open(os.path.join(REPO, "claude", "settings-base.json")) as handle:
            base = json.load(handle)
        cmds = [h["command"] for g in base["hooks"]["PreToolUse"] if g.get("matcher") == "Bash"
                for h in g["hooks"]]
        # Since 2026-10-24 the guard runs inside Machinist's one-process Bash dispatcher.
        self.assertTrue(any(c.endswith("bin/hooks/handoff_delete_guard.py")
                            or c.endswith("Machinist/tools/hooks/bash_guard.py") for c in cmds))

    def test_reached_through_the_dispatcher(self):
        dispatcher = os.path.expanduser("~/dev/Machinist/tools/hooks/bash_guard.py")
        if not os.path.isfile(dispatcher):
            self.skipTest("Machinist not on this machine")
        body = json.dumps({"tool_name": "Bash", "tool_input": {"command": "rm handoffs/a.md"}})
        p = subprocess.run([sys.executable, dispatcher, "--checks", "handoff"], input=body,
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)
        self.assertIn("handoff.py --cull", p.stderr)


if __name__ == "__main__":
    unittest.main()
