#!/usr/bin/env python3
"""Run Lodestar's test suite.

Single entry point so `bin/` lands on sys.path in exactly one place. Stdlib only,
Python 3.9-compatible — same constraints as bin/ itself, since launchd invokes
/usr/bin/python3 (3.9 on a stock macOS).

Usage:
    ./run_tests.py            # everything
    ./run_tests.py frontmatter journal   # only matching test modules
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(REPO_ROOT, "bin"))
# The machine-local state store (bin/lib/statestore.py) is never the live one under test: any
# test that forgets to pass its own path writes to a throwaway file, not ~/dev/.lodestar/.
if not os.environ.get("LODESTAR_STATE_DB"):
    import tempfile
    os.environ["LODESTAR_STATE_DB"] = os.path.join(tempfile.mkdtemp(prefix="lodestar-db-"), "test.db")
# Same for the machine-wide sweep/Tender lock (bin/lib/sweeplock.py): never the live one.
if not os.environ.get("LODESTAR_SWEEP_LOCK"):
    import tempfile
    os.environ["LODESTAR_SWEEP_LOCK"] = os.path.join(
        tempfile.mkdtemp(prefix="lodestar-sweeplock-"), "sweep.lock")


# The GUI guard (tests/conftest.py) is a pytest hook; unittest never calls it, so under this
# runner `test_nogui_guard` really launched browsers at https://example.com (owner, 2026-10-20).
if os.environ.get("LODESTAR_ALLOW_GUI") != "1":
    sys.path.insert(0, REPO_ROOT)
    from tests import conftest as _conftest
    _conftest.pytest_configure(None)


def main(argv):
    loader = unittest.TestLoader()
    if argv:
        names = ["tests.test_%s" % a if not a.startswith("test_") else "tests.%s" % a
                 for a in argv]
        suite = unittest.TestSuite()
        for name in names:
            try:
                suite.addTests(loader.loadTestsFromName(name))
            except unittest.SkipTest as skip:
                # discover() turns a module-level SkipTest into a skip; named loads let it escape.
                print("skipped %s: %s" % (name, skip))
    else:
        suite = loader.discover(os.path.join(REPO_ROOT, "tests"), top_level_dir=REPO_ROOT)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
