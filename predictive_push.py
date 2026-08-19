#!/usr/bin/env python3
"""Compatibility wrapper for contextual predictive push.

Compatibility wrapper. Prefer: IntelligenceApplicationService.predictive_push()
or the Agora MCP `predictive_push` tool.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Contextual predictive push wrapper.",
    )
    parser.add_argument(
        "--context",
        default="",
        help="User input / current context to match against",
    )
    parser.add_argument(
        "--working-dir",
        default="",
        help="Current working directory",
    )
    args = parser.parse_args()

    if len(sys.argv) == 1:
        parser.print_help()
        return 0

    # Allow running from any cwd as long as the script lives in the repo root.
    repo_root = Path(__file__).resolve().parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from core.application.intelligence import IntelligenceApplicationService

    service = IntelligenceApplicationService()
    result = service.predictive_push(
        user_input=args.context,
        working_dir=args.working_dir,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
