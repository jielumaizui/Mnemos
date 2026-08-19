#!/usr/bin/env python3
"""Execute every test file promised by the cognitive behavior scenario matrix."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_cognitive_behavior_scenarios import DEFAULT_MATRIX, validate

SCHEMA_VERSION = "mnemos.cognitive_behavior_execution.v1"
Runner = Callable[..., subprocess.CompletedProcess[str]]


def build_plan(matrix_path: Path = DEFAULT_MATRIX) -> dict[str, Any]:
    errors = validate(matrix_path)
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    scenarios = matrix.get("scenarios", [])
    test_files = sorted(
        {
            test_path
            for scenario in scenarios
            if isinstance(scenario, dict)
            for test_path in scenario.get("tests", [])
            if isinstance(test_path, str)
        }
    )
    if not test_files:
        errors.append("scenario test denominator is empty")
    return {
        "schema_version": SCHEMA_VERSION,
        "scenario_ids": [
            scenario.get("id") for scenario in scenarios if isinstance(scenario, dict)
        ],
        "test_files": test_files,
        "errors": errors,
    }


def execute(
    matrix_path: Path = DEFAULT_MATRIX,
    *,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    plan = build_plan(matrix_path)
    command = [sys.executable, "-m", "pytest", *plan["test_files"], "-q"]
    if plan["errors"]:
        return {**plan, "command": command, "returncode": None, "ok": False}
    result = runner(command, cwd=ROOT, text=True)
    return {
        **plan,
        "command": command,
        "returncode": result.returncode,
        "ok": result.returncode == 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--json", action="store_true", help="emit execution receipt")
    args = parser.parse_args(argv)
    report = execute(args.matrix.expanduser().resolve())
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif report["ok"]:
        print(
            "Cognitive behavior scenarios passed: "
            f"scenarios={len(report['scenario_ids'])} files={len(report['test_files'])}"
        )
    else:
        print(f"Cognitive behavior scenarios failed: {report['errors']}", file=sys.stderr)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
