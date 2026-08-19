#!/usr/bin/env python3
"""Root wrapper for scripts/verify_installation.py."""

from pathlib import Path
import runpy


if __name__ == "__main__":
    runpy.run_path(
        str(Path(__file__).parent / "scripts" / "verify_installation.py"), run_name="__main__"
    )
