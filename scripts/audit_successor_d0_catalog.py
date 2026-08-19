#!/usr/bin/env python3
"""Run the independent cognitive-successor D0 catalog verifier."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.successor_d0_verifier import main

if __name__ == "__main__":
    raise SystemExit(main())
