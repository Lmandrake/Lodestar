"""Lodestar's test package.

The only thing that lives here is the pristine-environment snapshot, and it lives here
because this is the *earliest* code in the suite: `unittest` imports the `tests` package
before it imports a single test module, and it imports every test module before it runs a
single test. So the snapshot below is the environment as the process started, which is the
only trustworthy baseline for `tests/test_zz_env_isolation.py` to compare against at the
end of the run. Taking it inside a test module instead would mean taking it after some
other module had already had a chance to change it.

See `tests/test_zz_env_isolation.py` for what is guarded and the leak that motivated it.
"""

import os

#: Non-Lodestar variables the suite redirects (`HOME`) or that `bin/` reads. Every variable
#: starting with `LODESTAR_` is watched by prefix instead of by name, so adding a new one to
#: `bin/` covers it here automatically rather than silently leaving a hole.
WATCHED_EXACT = ("HOME", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID")
WATCHED_PREFIX = "LODESTAR_"


def env_snapshot():
    """The watched slice of `os.environ`, right now."""
    return {key: value for key, value in os.environ.items()
            if key.startswith(WATCHED_PREFIX) or key in WATCHED_EXACT}


# --- No test may read the live Dialexis (measured 2026-10-15) --------------------------------
#
# Panel and kanban renders resolved names through `lib.context`, which shells out to the real
# `~/dev/Dialexis/run.py entity` (35 lookups per render, ~2,600 per suite run), so their output
# depended on the live graph. Point the default at an empty directory: `Resolver.available` is
# False and the render takes the no-Dialexis path. A test about Dialexis passes its own
# `dialexis=`/`lookup=`. Set before PRISTINE_ENV, so the baseline includes it.
def _empty_dialexis():
    import atexit
    import shutil
    import tempfile
    root = tempfile.mkdtemp(prefix="lodestar-tests-no-dialexis-")
    atexit.register(shutil.rmtree, root, True)
    return root


if os.environ.get("LODESTAR_TESTS_LIVE_DIALEXIS") != "1":
    os.environ["LODESTAR_DIALEXIS"] = _empty_dialexis()


# --- The private boundary is a fixture, never this deployment's (2026-10-27) ------------------
#
# `lib.boundary` reads the private host, store root, class tag and owner addresses from
# `sync/private-store.json`. Every test reads `tests/fixtures/private-store.json` instead
# (invented, reserved-domain values), so no test names or depends on a real deployment, and a
# public clone runs the same suite. Before PRISTINE_ENV, so the baseline includes it.
os.environ["LODESTAR_PRIVATE_CONFIG"] = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                     "fixtures", "private-store.json")

# --- No test may touch the real private store (ruling 5, 2026-10-13) ------------------------
#
# Board prose, conversation records and handoffs are routed into the private store by default,
# so an un-isolated test would write into the real one. Every run gets ONE throwaway checkout
# whose remote is on the fixture's private host; tests that redirect `LODESTAR_ROOT` to a temp dir get their own
# overlay inside it automatically (`projects/<basename of the temp root>/`). A test that needs
# "no store" or a different one sets `LODESTAR_PRIVATE_ROOT` itself and restores it.
def _throwaway_private_store():
    import atexit
    import shutil
    import subprocess
    import tempfile
    root = os.path.realpath(tempfile.mkdtemp(prefix="lodestar-tests-private-store-"))
    atexit.register(shutil.rmtree, root, True)
    subprocess.run(["git", "init", "-q", "-b", "main", root], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "-C", root, "remote", "add", "origin",
                    "git@git.private.example:lodestar-tests/private-store.git"], check=True)
    return root


os.environ["LODESTAR_PRIVATE_ROOT"] = _throwaway_private_store()


#: The watched environment as the process started. Never reassign this.
# Burning v2 sits behind a flag (bin/lib/burnclass.py): a flag file on this machine must not
# flip the v1 tests. Tests of v2 set the variable themselves. Before PRISTINE_ENV, as above.
os.environ["LODESTAR_BURNING_V2"] = "0"
# LODE-20: no test reads real mail (conftest sets it too); set before PRISTINE_ENV so the baseline has it.
os.environ["LODESTAR_HANDOFF_DRAFTS"] = "off"
# The machine-wide sweep/Tender lock must never be the live one (bin/lib/sweeplock.py). Set here, before
# PRISTINE_ENV, so the isolation baseline includes it (conftest.py sets it too, after the baseline).
if not os.environ.get("LODESTAR_SWEEP_LOCK"):
    import tempfile as _tf
    os.environ["LODESTAR_SWEEP_LOCK"] = os.path.join(_tf.mkdtemp(prefix="lodestar-sweeplock-"), "sweep.lock")

PRISTINE_ENV = env_snapshot()





# --- Tests that read Lodestar's own private state (public release, 2026-10-26) ----------------
#
# `machines.md`, `sync/registry.md`, `claude/skills.md`, `claude/settings-*` and `claude/global/`
# describe one real fleet and are not part of the public export, so a test pinning them skips
# there instead of failing. Locally they always exist, so nothing skips on a real checkout.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def needs_private(*rels):
    """`unittest.skipUnless` every repo-relative path in `rels` exists in this checkout."""
    import unittest
    missing = [r for r in rels if not os.path.exists(os.path.join(REPO_ROOT, r))]
    return unittest.skipUnless(not missing, "needs %s (private fleet state, not in the public "
                                            "release)" % ", ".join(missing))


def real_key(role):
    """The machine key that holds `role` in the real machines.md (for the `needs_private` tests
    that pin the real fleet; real keys are not written into the public tests)."""
    from lib import machine
    keys = [k for k, r in machine.roles(os.path.join(REPO_ROOT, "machines.md")).items() if r == role]
    if len(keys) != 1:
        raise AssertionError("machines.md has %d machines with role %r" % (len(keys), role))
    return keys[0]


def real_home_key():
    """A machine key holding role `home` in the real machines.md (there may be several)."""
    from lib import machine
    keys = sorted(k for k, r in machine.roles(os.path.join(REPO_ROOT, "machines.md")).items() if r == "home")
    if not keys:
        raise AssertionError("machines.md has no machine with role 'home'")
    return keys[0]
