"""Suite-wide guard: no test may drive the GUI of the Mac it runs on.

Measured 2026-10-15: a full `pytest tests` run pulled window focus away (and opened two
viewer apps) while Lukas was working, because code under test reached `open`, `osascript` or
`webbrowser.open` for real. This guard makes every such launch fail LOUDLY, in-process and in
child processes alike, and fails the test that tried it, even when the code swallows the error.

- In-process: `subprocess.Popen` (so run/call/check_output too), `os.system`, `os.popen` and
  `webbrowser.open*` refuse a launch of `open` or `osascript`.
- Child processes: a shim dir first on PATH holds same-named shims that log and exit 97, and
  BROWSER points at it, so a script run as a subprocess cannot reach the real ones by name.
  (A child that names `/usr/bin/open` absolutely is not caught; stub its runner instead.)

A test that needs to check such a call must stub the module's runner/opener itself.
"""
import os
import re
import shlex
import subprocess
import tempfile
import webbrowser

import sys

try:  # optional: run_tests.py (stdlib unittest) needs only pytest_configure below
    import pytest
except ImportError:  # pragma: no cover
    pytest = None

# Some test modules import `lib`/bin scripts without inserting bin/ themselves and only worked
# when an earlier module had; put it on the path once so every file runs on its own.
_BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

#: Launchers that raise windows or drive the UI. axprobe `--press`/AXRaise and the M365 reconnect
#: script both touch the screen (bin/lib/sense.py), so they are refused too.
# LODE-20: handoff.py --wake/--check read the drafts ledger and a personal mailbox; no test reads
# real mail or the machine's live state (tests/test_handoff_drafts.py injects fixtures).
os.environ["LODESTAR_HANDOFF_DRAFTS"] = "off"
# The machine-wide sweep/Tender lock (bin/lib/sweeplock.py): a test that forgets to isolate it
# must never queue behind, or block, the live hourly sweep on ~/dev/.lodestar/sweep.lock.
if not os.environ.get("LODESTAR_SWEEP_LOCK"):
    import tempfile as _tempfile
    os.environ["LODESTAR_SWEEP_LOCK"] = os.path.join(
        _tempfile.mkdtemp(prefix="lodestar-sweeplock-"), "sweep.lock")

GUI_COMMANDS = frozenset({"open", "osascript", "axprobe", "m365-reconnect.sh", "cliclick"})
_SHELL_HEAD = re.compile(r"(?:^|[;&|(]\s*|\$\(\s*)(?:\S*/)?(open|osascript|axprobe|m365-reconnect\.sh|cliclick)(?=\s|$)")

_violations = []
_shim_dir = tempfile.mkdtemp(prefix="lodestar-nogui-")
_shim_log = os.path.join(_shim_dir, "launches.log")


class GuiLaunchRefused(RuntimeError):
    pass


def _gui_head(cmd):
    """The GUI command `cmd` would launch, or None."""
    if isinstance(cmd, (bytes, bytearray)):
        cmd = cmd.decode(errors="replace")
    if isinstance(cmd, str):
        m = _SHELL_HEAD.search(cmd.strip())
        return m.group(1) if m else None
    try:
        head = os.fsdecode(list(cmd)[0])
    except (TypeError, IndexError):
        return None
    return os.path.basename(head) if os.path.basename(head) in GUI_COMMANDS else None


def _refuse(what, detail):
    msg = "test tried to launch %r for real (%s); stub the runner/opener" % (what, detail)
    _violations.append(msg)
    raise GuiLaunchRefused(msg)


_real_popen_init = subprocess.Popen.__init__
_real_system = os.system
_real_popen = os.popen


def _guarded_popen_init(self, args, *a, **kw):
    head = _gui_head(args)
    if head:
        _refuse(head, "subprocess %r" % (args,))
    return _real_popen_init(self, args, *a, **kw)


def _guarded_system(cmd):
    head = _gui_head(cmd)
    if head:
        _refuse(head, "os.system %r" % (cmd,))
    return _real_system(cmd)


def _guarded_os_popen(cmd, *a, **kw):
    head = _gui_head(cmd)
    if head:
        _refuse(head, "os.popen %r" % (cmd,))
    return _real_popen(cmd, *a, **kw)


def _guarded_browser(url, *a, **kw):
    _refuse("webbrowser.open", "url %r" % (url,))


def _write_shims():
    for name in sorted(GUI_COMMANDS) + ["nogui-browser"]:
        path = os.path.join(_shim_dir, name)
        with open(path, "w") as fh:
            fh.write('#!/bin/sh\necho "%s $* :: $PYTEST_CURRENT_TEST" >> %s\n'
                     'echo "lodestar test guard: refused %s" >&2\nexit 97\n'
                     % (name, shlex.quote(_shim_log), name))
        os.chmod(path, 0o755)


def pytest_configure(config):
    if os.environ.get("LODESTAR_ALLOW_GUI") == "1":
        return
    _write_shims()
    os.environ["PATH"] = _shim_dir + os.pathsep + os.environ.get("PATH", "")
    os.environ["BROWSER"] = os.path.join(_shim_dir, "nogui-browser")
    subprocess.Popen.__init__ = _guarded_popen_init
    os.system = _guarded_system
    os.popen = _guarded_os_popen
    for name in ("open", "open_new", "open_new_tab"):
        setattr(webbrowser, name, _guarded_browser)


def _shim_lines():
    try:
        with open(_shim_log) as fh:
            return fh.read().splitlines()
    except FileNotFoundError:
        return []


def _no_gui_launches():
    """Fail the test that launched a GUI command, even if its code swallowed the refusal."""
    before_v, before_s = len(_violations), len(_shim_lines())
    yield
    new = _violations[before_v:] + ["child process: " + s for s in _shim_lines()[before_s:]]
    if new:
        pytest.fail("GUI launch during test (focus steal guard):\n  " + "\n  ".join(new),
                    pytrace=False)


if pytest is not None:
    _no_gui_launches = pytest.fixture(autouse=True)(_no_gui_launches)
