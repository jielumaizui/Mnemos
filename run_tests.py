from __future__ import annotations

#!/usr/bin/env python3
"""
Test runner for Memos-Wiki

Usage:
    python run_tests.py              # Run all tests
    python run_tests.py unit         # Run unit tests only
    python run_tests.py integration  # Run integration tests only
    python run_tests.py e2e          # Run e2e tests only
"""

import sys
import os
import subprocess
from pathlib import Path

def run_tests(test_type: str = "all"):
    """Run tests with pytest or unittest"""

    test_dir = Path(__file__).parent / "tests"

    if not test_dir.exists():
        print("Error: tests/ directory not found")
        return 1

    # Determine test path
    if test_type == "all":
        test_path = str(test_dir)
    elif test_type in ["unit", "integration", "e2e"]:
        test_path = str(test_dir / test_type)
        if not Path(test_path).exists():
            print(f"Error: {test_path} not found")
            return 1
    else:
        print(f"Unknown test type: {test_type}")
        return 1

    # Try pytest first, fall back to unittest
    try:
        import pytest
        print(f"Running {test_type} tests with pytest...")
        args = ["-v", test_path]
        if test_type == "all":
            args.append("-x")  # Stop on first failure for full run
        return pytest.main(args)
    except ImportError:
        print("pytest not found, falling back to unittest...")
        # Run with unittest
        import unittest
        loader = unittest.TestLoader()
        suite = loader.discover(test_path, pattern="test_*.py")
        runner = unittest.TextTestRunner(verbosity=2)
        result = runner.run(suite)
        return 0 if result.wasSuccessful() else 1

def main():
    """Main entry point"""
    test_type = sys.argv[1] if len(sys.argv) > 1 else "all"

    print("=" * 60)
    print(f"Memos-Wiki Test Runner - {test_type.upper()}")
    print("=" * 60)
    print()

    exit_code = run_tests(test_type)

    print()
    print("=" * 60)
    if exit_code == 0:
        print("All tests passed!")
    else:
        print("Some tests failed")
    print("=" * 60)

    return exit_code

if __name__ == "__main__":
    sys.exit(main())
