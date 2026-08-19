#!/usr/bin/env python3
"""Validate Mnemos reliability, security, performance, and migration controls."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MATRIX = ROOT / "docs" / "acceptance" / "ops_resilience_matrix.json"
SCHEMA_VERSION = "mnemos.ops_resilience_matrix.v1"
REQUIRED_CATEGORIES = {"reliability", "security", "performance", "migration"}
REQUIRED_FIELDS = [
    "id",
    "category",
    "risk",
    "contract",
    "observable_entrypoints",
    "code_refs",
    "tests",
    "degradation_or_recovery",
    "validation_commands",
]


def _path_exists(ref: str) -> bool:
    if ref.startswith(("cli:", "mcp:", "manual:", "state:", "metric:")):
        return True
    path = ref.split(":", 1)[1] if ref.startswith("script:") else ref.split(":", 1)[0]
    return (ROOT / path).exists()


def _validate_refs(control_id: str, field: str, refs: list[str]) -> list[str]:
    errors: list[str] = []
    if not refs:
        errors.append(f"{control_id}: {field} must not be empty")
    for ref in refs:
        if not _path_exists(ref):
            errors.append(f"{control_id}: missing path in {field}: {ref}")
    return errors


def _validate_commands(control_id: str, commands: list[str]) -> list[str]:
    errors: list[str] = []
    if not commands:
        errors.append(f"{control_id}: validation_commands must not be empty")
    allowed = ("python3 -m pytest", "python3 scripts/", "python3 mnemos_cli.py")
    for command in commands:
        if not str(command).startswith(allowed):
            errors.append(f"{control_id}: unsupported validation command: {command}")
    return errors


def _validate_control(control: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    control_id = str(control.get("id", "<missing-id>"))
    for field in REQUIRED_FIELDS:
        if field not in control:
            errors.append(f"{control_id}: missing field {field}")
            continue
        if control[field] in ("", None, [], {}):
            errors.append(f"{control_id}: empty field {field}")

    category = str(control.get("category", ""))
    if category not in REQUIRED_CATEGORIES:
        errors.append(f"{control_id}: category must be one of {sorted(REQUIRED_CATEGORIES)}")

    for field in ("observable_entrypoints", "code_refs", "tests"):
        value = control.get(field, [])
        if not isinstance(value, list):
            errors.append(f"{control_id}: {field} must be list")
            continue
        errors.extend(_validate_refs(control_id, field, value))

    commands = control.get("validation_commands", [])
    if not isinstance(commands, list):
        errors.append(f"{control_id}: validation_commands must be list")
    else:
        errors.extend(_validate_commands(control_id, commands))
    return errors


def validate(matrix_path: Path = DEFAULT_MATRIX) -> list[str]:
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    if matrix.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"matrix schema_version must be {SCHEMA_VERSION}")
    if not matrix.get("updated"):
        errors.append("matrix updated must be set")

    controls = matrix.get("controls", [])
    if not isinstance(controls, list):
        return errors + ["matrix controls must be a list"]
    if not controls:
        errors.append("matrix has no controls")

    seen_ids: set[str] = set()
    seen_categories: set[str] = set()
    for control in controls:
        control_id = str(control.get("id", ""))
        if control_id in seen_ids:
            errors.append(f"duplicate control id {control_id}")
        seen_ids.add(control_id)
        category = str(control.get("category", ""))
        if category:
            seen_categories.add(category)
        errors.extend(_validate_control(control))

    missing_categories = REQUIRED_CATEGORIES - seen_categories
    if missing_categories:
        errors.append(f"missing categories: {', '.join(sorted(missing_categories))}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "matrix",
        nargs="?",
        default=str(DEFAULT_MATRIX),
        help="Path to ops_resilience_matrix.json",
    )
    args = parser.parse_args(argv)
    errors = validate(Path(args.matrix))
    if errors:
        print("Ops resilience matrix audit failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Ops resilience matrix audit passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
