"""Poison-test runner entrypoint for `make verify-guards`."""

from __future__ import annotations

import unittest


def main() -> None:
    loader = unittest.TestLoader()
    suite = loader.discover("tests/fault_injection", pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)


if __name__ == "__main__":
    main()
