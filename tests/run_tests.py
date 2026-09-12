#!/usr/bin/env python3
"""Standard-library-only entry point for the switchyard key-path suite.

Usage:
    python3 tests/run_tests.py            # run everything
    python3 tests/run_tests.py -v         # verbose
    python3 tests/run_tests.py test_validators test_sequencer

Every test creates its own ``tempfile.TemporaryDirectory``; the repository's
``data/`` tree is never read or written.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
for path in (str(PROJECT_ROOT / "src"), str(TESTS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


def main() -> int:
    args = [arg for arg in sys.argv[1:] if arg.startswith("-")]
    names = [arg for arg in sys.argv[1:] if not arg.startswith("-")]
    if names:
        loader = unittest.TestLoader()
        suite = unittest.TestSuite()
        for name in names:
            module_name = name if name.startswith("test_") else f"test_{name}"
            suite.addTests(loader.loadTestsFromName(module_name))
    else:
        suite = unittest.TestLoader().discover(str(TESTS_DIR), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2 if "-v" in args else 1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
