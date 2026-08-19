#!/usr/bin/env python3
from __future__ import annotations

"""Root entry point for the Mnemos layered test runner.

The implementation lives in scripts.run_tests so the root and scripts entry
points cannot drift.
"""

from scripts.run_tests import main


if __name__ == "__main__":
    raise SystemExit(main())
