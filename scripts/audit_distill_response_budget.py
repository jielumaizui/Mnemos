#!/usr/bin/env python3
"""Audit distillation response token budget defaults."""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import DEFAULT_CONFIG  # noqa: E402
from core.hephaestus import response_budget  # noqa: E402

EXPECTED_RESPONSE_TOKENS = {
    "response_tokens": 6000,
    "response_tokens_default": 6000,
    "response_tokens_medium": 8000,
    "response_tokens_long": 12000,
    "response_tokens_retry_max": 16000,
}


def _distill_section(config: dict[str, Any], label: str) -> tuple[dict[str, Any], list[str]]:
    distill = config.get("distill")
    if not isinstance(distill, dict):
        return {}, [f"{label} missing distill section"]
    return distill, []


def _validate_distill_values(
    distill: dict[str, Any],
    *,
    label: str,
) -> list[str]:
    errors: list[str] = []
    for key, expected in EXPECTED_RESPONSE_TOKENS.items():
        actual = distill.get(key)
        if actual != expected:
            errors.append(f"{label} distill.{key} expected {expected}, got {actual!r}")
    return errors


def validate_config_defaults() -> list[str]:
    distill, errors = _distill_section(DEFAULT_CONFIG, "DEFAULT_CONFIG")
    if errors:
        return errors
    return _validate_distill_values(distill, label="DEFAULT_CONFIG")


def validate_resolver_defaults() -> list[str]:
    errors: list[str] = []
    constants = {
        "DEFAULT_RESPONSE_TOKENS": response_budget.DEFAULT_RESPONSE_TOKENS,
        "DEFAULT_RESPONSE_TOKENS_MEDIUM": response_budget.DEFAULT_RESPONSE_TOKENS_MEDIUM,
        "DEFAULT_RESPONSE_TOKENS_LONG": response_budget.DEFAULT_RESPONSE_TOKENS_LONG,
        "DEFAULT_RESPONSE_TOKENS_RETRY_MAX": response_budget.DEFAULT_RESPONSE_TOKENS_RETRY_MAX,
    }
    expected_constants = {
        "DEFAULT_RESPONSE_TOKENS": 6000,
        "DEFAULT_RESPONSE_TOKENS_MEDIUM": 8000,
        "DEFAULT_RESPONSE_TOKENS_LONG": 12000,
        "DEFAULT_RESPONSE_TOKENS_RETRY_MAX": 16000,
    }
    for name, expected_value in expected_constants.items():
        actual_constant = constants[name]
        if actual_constant != expected_value:
            errors.append(
                f"response_budget.{name} expected {expected_value}, got {actual_constant!r}"
            )

    cases: list[tuple[str, response_budget.ResponseTokenLimits, tuple[int, int, str]]] = [
        (
            "default",
            response_budget.resolve_response_token_limits({}, input_tokens=1200),
            (6000, 16000, "default"),
        ),
        (
            "medium",
            response_budget.resolve_response_token_limits({}, input_tokens=9000),
            (8000, 16000, "medium"),
        ),
        (
            "long",
            response_budget.resolve_response_token_limits({}, input_tokens=24000),
            (12000, 16000, "long"),
        ),
        (
            "retry",
            response_budget.resolve_response_token_limits(
                {},
                input_tokens=1200,
                previous_finish_reason="length",
            ),
            (16000, 16000, "retry"),
        ),
    ]
    for label, limits, expected_limits in cases:
        actual_limits = (limits.initial, limits.retry_max, limits.tier)
        if actual_limits != expected_limits:
            errors.append(
                f"resolver {label} expected {expected_limits}, got {actual_limits}"
            )
    return errors


def validate_engine_fallback_constant() -> list[str]:
    path = ROOT / "core" / "hephaestus" / "distillation_engine.py"
    try:
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except OSError as exc:
        return [f"core/hephaestus/distillation_engine.py unreadable: {exc}"]
    except SyntaxError as exc:
        return [f"core/hephaestus/distillation_engine.py syntax error: {exc}"]

    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "RESPONSE_TOKENS" for target in node.targets):
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, int):
            if value.value != 6000:
                return [
                    "distillation_engine.RESPONSE_TOKENS expected 6000, "
                    f"got {value.value!r}"
                ]
            return []
        return ["distillation_engine.RESPONSE_TOKENS must be an integer literal"]
    return ["distillation_engine.RESPONSE_TOKENS missing"]


def validate_config_examples() -> list[str]:
    errors: list[str] = []
    example_paths = [
        ("config.example.json", ROOT / "config" / "config.example.json"),
        ("config.example.yaml", ROOT / "config" / "config.example.yaml"),
    ]
    for label, path in example_paths:
        if not path.exists():
            errors.append(f"{label} missing at {path}")
            continue
        if path.suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        distill, section_errors = _distill_section(data, label)
        errors.extend(section_errors)
        if not section_errors:
            errors.extend(_validate_distill_values(distill, label=label))
    return errors


def validate_docs() -> list[str]:
    guide = ROOT / "docs" / "AGENT_GUIDE.md"
    text = guide.read_text(encoding="utf-8")
    required_snippets = [
        "| `response_tokens` | 6000 |",
        "| `response_tokens_default` / `medium` / `long` / `retry_max` | 6000 / 8000 / 12000 / 16000 |",
    ]
    return [f"docs/AGENT_GUIDE.md missing snippet: {snippet}" for snippet in required_snippets if snippet not in text]


def validate() -> list[str]:
    errors: list[str] = []
    errors.extend(validate_config_defaults())
    errors.extend(validate_resolver_defaults())
    errors.extend(validate_engine_fallback_constant())
    errors.extend(validate_config_examples())
    errors.extend(validate_docs())
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable result")
    args = parser.parse_args(argv)

    errors = validate()
    if args.json:
        print(json.dumps({"ok": not errors, "errors": errors}, ensure_ascii=False, indent=2))
    elif errors:
        print("Distill response budget audit failed:")
        for error in errors:
            print(f"- {error}")
    else:
        print("Distill response budget audit passed")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
