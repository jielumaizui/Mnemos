#!/usr/bin/env python3
"""Root wrapper for scripts/auto_setup.py."""

from pathlib import Path
import runpy


if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).parent / "scripts" / "auto_setup.py"), run_name="__main__")
