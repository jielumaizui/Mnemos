#!/usr/bin/env python3
"""mypy 错误预算门禁。

运行 mypy 并比较当前错误数与预算；超过预算时失败，防止类型债务回退。
预算文件为项目根目录的 .mypy_budget.json，格式：{"budget": 0, "updated_at": "..."}。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

PROJECT_ROOT = Path(__file__).parent.parent
BUDGET_FILE = PROJECT_ROOT / ".mypy_budget.json"
DEFAULT_BUDGET = 0
MYPY_TARGETS = [
    "core/",
    "integrations/",
    "daemon/",
    "scripts/",
    "mnemos_cli.py",
    "mnemos_daemon.py",
]
MYPY_ARGS = ["--ignore-missing-imports"]


def _load_budget() -> int:
    try:
        data = json.loads(BUDGET_FILE.read_text(encoding="utf-8"))
        return int(data.get("budget", DEFAULT_BUDGET))
    except (OSError, json.JSONDecodeError, ValueError):
        return DEFAULT_BUDGET


def _save_budget(budget: int) -> None:
    data = {
        "budget": budget,
        "updated_at": datetime.now().isoformat(),
    }
    BUDGET_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _run_mypy() -> Tuple[int, str]:
    cmd = [sys.executable, "-m", "mypy", *MYPY_TARGETS, *MYPY_ARGS]
    proc = subprocess.run(
        cmd,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    output = proc.stdout + proc.stderr
    match = re.search(r"Found (\d+) errors? in", output)
    if match:
        return int(match.group(1)), output
    if proc.returncode != 0:
        # mypy failed to run; treat as an error so the gate does not silently pass.
        return 1, output
    return 0, output


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="mypy error budget gate")
    parser.add_argument(
        "--update",
        action="store_true",
        help="将预算更新为当前 mypy 错误数",
    )
    args = parser.parse_args(argv)

    count, output = _run_mypy()
    if count is None:
        print("FAIL: could not determine mypy error count")
        print(output)
        return 1
    if args.update:
        _save_budget(count)
        print(f"Updated mypy budget to {count}")
        return 0

    budget = _load_budget()
    print(f"mypy errors: {count} (budget: {budget})")
    if count > budget:
        print(f"FAIL: mypy errors ({count}) exceed budget ({budget})")
        print(output)
        return 1

    print("OK: mypy error count within budget")
    return 0


if __name__ == "__main__":
    sys.exit(main())
