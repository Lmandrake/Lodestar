"""The conftest GUI guard refuses `open`/`osascript`/`webbrowser.open`, in-process and in children."""
import os
import subprocess
import sys
import unittest
import webbrowser

from tests import conftest


@unittest.skipIf(os.environ.get("LODESTAR_ALLOW_GUI") == "1", "guard disabled")
@unittest.skipUnless(webbrowser.open is conftest._guarded_browser, "guard not installed: refusing to launch real GUI")
class NoGuiGuardTest(unittest.TestCase):
    def tearDown(self):
        conftest._violations.clear()      # the refusals below are the point, not a failure

    def test_subprocess_open_is_refused(self):
        with self.assertRaises(conftest.GuiLaunchRefused):
            subprocess.run(["/usr/bin/open", "/tmp"], check=False)

    def test_shell_osascript_is_refused(self):
        with self.assertRaises(conftest.GuiLaunchRefused):
            subprocess.run("cd /tmp && osascript -e 'beep'", shell=True, check=False)

    def test_webbrowser_is_refused(self):
        with self.assertRaises(conftest.GuiLaunchRefused):
            webbrowser.open("https://example.com")

    def test_child_by_name_hits_the_shim(self):
        before = len(conftest._shim_lines())
        rc = subprocess.run([sys.executable, "-c", "import subprocess,sys;"
                             "sys.exit(subprocess.run(['osascript','-e','beep']).returncode)"]).returncode
        self.assertEqual(rc, 97)
        lines = conftest._shim_lines()
        self.assertEqual(len(lines), before + 1)
        # Drop the line so the autouse check does not fail this test for the launch it proved.
        with open(conftest._shim_log, "w") as fh:
            fh.write("".join(l + "\n" for l in lines[:before]))

    def test_ordinary_commands_pass(self):
        self.assertEqual(subprocess.run(["true"]).returncode, 0)
        self.assertEqual(subprocess.run("echo opener | grep -q open", shell=True).returncode, 0)
