#!/usr/bin/env python3
"""Validate Mnemos cognitive behavior acceptance scenarios."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_MATRIX = ROOT / "docs" / "acceptance" / "cognitive_behavior_scenarios.json"
SCHEMA_VERSION = "mnemos.cognitive_behavior_scenarios.v1"
REQUIRED_SCENARIOS = {
    "repeated_error_preflight_guard",
    "user_preference_persona_prompt",
    "long_term_project_recall",
    "risk_guard_interrupt",
    "proactive_predictive_push",
    "recap_followup_nudge",
    "conflict_dispute_prompt",
    "behavior_intent_distillation_flow",
    "blindspot_missing_knowledge",
    "cold_start_module_activation",
}
REQUIRED_FIELDS = [
    "id",
    "behavior_goal",
    "user_scenario",
    "primary_tools",
    "behavior_delta",
    "evidence_fields",
    "user_explanation",
    "feedback_or_correction_tools",
    "code_refs",
    "tests",
    "docs",
]


def _registered_tools() -> set[str]:
    from integrations.agora import MCPServer

    return set(MCPServer().tools)


def _path_exists(ref: str) -> bool:
    path = ref.split(":", 1)[0]
    if path.startswith(("cli:", "mcp:", "manual:", "state:", "metric:")):
        return True
    return (ROOT / path).exists()


def _validate_paths(scenario_id: str, field: str, refs: list[str]) -> list[str]:
    errors: list[str] = []
    if not refs:
        errors.append(f"{scenario_id}: {field} must not be empty")
    for ref in refs:
        if not _path_exists(ref):
            errors.append(f"{scenario_id}: missing path in {field}: {ref}")
    return errors


def _validate_tools(
    scenario_id: str,
    field: str,
    tools: list[str],
    available_tools: set[str],
) -> list[str]:
    errors: list[str] = []
    if not tools:
        errors.append(f"{scenario_id}: {field} must not be empty")
    for tool in tools:
        if tool not in available_tools:
            errors.append(f"{scenario_id}: unknown MCP tool in {field}: {tool}")
    return errors


def _validate_scenario(
    scenario: dict[str, Any],
    available_tools: set[str],
) -> list[str]:
    errors: list[str] = []
    scenario_id = str(scenario.get("id", "<missing-id>"))
    for field in REQUIRED_FIELDS:
        if field not in scenario:
            errors.append(f"{scenario_id}: missing field {field}")
            continue
        if scenario[field] in ("", None, [], {}):
            errors.append(f"{scenario_id}: empty field {field}")

    for field in ("primary_tools", "feedback_or_correction_tools"):
        value = scenario.get(field, [])
        if not isinstance(value, list):
            errors.append(f"{scenario_id}: {field} must be list")
            continue
        errors.extend(_validate_tools(scenario_id, field, value, available_tools))

    for field in ("evidence_fields", "code_refs", "tests", "docs"):
        value = scenario.get(field, [])
        if not isinstance(value, list):
            errors.append(f"{scenario_id}: {field} must be list")
            continue
        if field == "evidence_fields":
            if not value:
                errors.append(f"{scenario_id}: evidence_fields must not be empty")
        else:
            errors.extend(_validate_paths(scenario_id, field, value))

    return errors


def validate(matrix_path: Path = DEFAULT_MATRIX) -> list[str]:
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    if matrix.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"matrix schema_version must be {SCHEMA_VERSION}")
    if not matrix.get("updated"):
        errors.append("matrix updated must be set")

    scenarios = matrix.get("scenarios", [])
    if not isinstance(scenarios, list):
        return errors + ["matrix scenarios must be a list"]
    if not scenarios:
        errors.append("matrix has no scenarios")

    available_tools = _registered_tools()
    seen_ids: set[str] = set()
    for scenario in scenarios:
        scenario_id = str(scenario.get("id", ""))
        if scenario_id in seen_ids:
            errors.append(f"duplicate scenario id {scenario_id}")
        seen_ids.add(scenario_id)
        errors.extend(_validate_scenario(scenario, available_tools))

    missing = REQUIRED_SCENARIOS - seen_ids
    extra = seen_ids - REQUIRED_SCENARIOS
    if missing:
        errors.append(f"missing required scenarios: {', '.join(sorted(missing))}")
    if extra:
        errors.append(f"unknown scenarios: {', '.join(sorted(extra))}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "matrix",
        nargs="?",
        default=str(DEFAULT_MATRIX),
        help="Path to cognitive_behavior_scenarios.json",
    )
    args = parser.parse_args(argv)
    errors = validate(Path(args.matrix))
    if errors:
        print("Cognitive behavior scenario audit failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Cognitive behavior scenario audit passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
