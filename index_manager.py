#!/usr/bin/env python3
"""Compatibility wrapper for embedding index management."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parent
    if not sys.argv[1:] or any(arg in {"-h", "--help"} for arg in sys.argv[1:]):
        print("Compatibility wrapper. Prefer: python3 scripts/build_embedding_index.py [--force]")
        return 0
    return subprocess.call(
        [sys.executable, str(root / "scripts" / "build_embedding_index.py"), *sys.argv[1:]]
    )


if __name__ == "__main__":
    raise SystemExit(main())
