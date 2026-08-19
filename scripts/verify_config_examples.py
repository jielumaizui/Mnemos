#!/usr/bin/env python3
"""Verify that config examples cover DEFAULT_CONFIG keys and public env vars.

Exit code 0 if coverage is above the thresholds, otherwise 1.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.config import DEFAULT_CONFIG
from core.config_registry import CONFIG_REGISTRY
from scripts._config_example_data import public_env_vars

CONFIG_DIR = REPO_ROOT / "config"

CONFIG_COVERAGE_THRESHOLD = 0.95
ENV_COVERAGE_THRESHOLD = 0.95
STRICT_COVERAGE_THRESHOLD = 1.0


def _load_json_example() -> Dict[str, Any]:
    data: Dict[str, Any] = json.loads(
        (CONFIG_DIR / "config.example.json").read_text(encoding="utf-8")
    )
    return data


def _load_yaml_example() -> Dict[str, Any]:
    data: Dict[str, Any] = yaml.safe_load(
        (CONFIG_DIR / "config.example.yaml").read_text(encoding="utf-8")
    )
    if data is None:
        return {}
    return data


def _extract_env_map_vars() -> List[str]:
    """Return public env ownership from the canonical registry."""
    return sorted(CONFIG_REGISTRY.env_targets)


def _env_example_vars() -> Set[str]:
    text = (CONFIG_DIR / ".env.example").read_text(encoding="utf-8")
    # Capture VAR_NAME appearing before '=' or as a standalone commented key.
    names: Set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("# ===") or line.startswith("# ---"):
            continue
        # Strip leading '# ' if present.
        body = line.lstrip("#").strip()
        m = re.match(r"^([A-Z][A-Z0-9_]*)", body)
        if m:
            names.add(m.group(1))
    return names


def _check_config_coverage(example: Dict[str, Any], label: str) -> Tuple[List[str], float]:
    expected = CONFIG_REGISTRY.flatten_tree(DEFAULT_CONFIG)
    actual = CONFIG_REGISTRY.flatten_tree(example)
    missing = sorted(set(expected) - set(actual))
    coverage = 1.0 - len(missing) / len(expected)
    lines = [
        f"{label}: {len(expected) - len(missing)}/{len(expected)} ({coverage:.0%})"
    ]
    if missing:
        lines.append(f"  missing keys: {missing}")
    return lines, coverage


def _check_env_coverage(env_map_vars: List[str]) -> Tuple[List[str], float]:
    documented = _env_example_vars()
    required = set(env_map_vars) | set(public_env_vars())
    missing = sorted(required - documented)
    coverage = 1.0 - len(missing) / len(required) if required else 1.0
    lines = [
        f".env.example: {len(required) - len(missing)}/{len(required)} env vars ({coverage:.0%})"
    ]
    if missing:
        lines.append(f"  missing vars: {missing}")
    return lines, coverage


def _parse_args(argv: List[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify config example coverage against DEFAULT_CONFIG and public env vars."
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="require 100% coverage for config examples and .env.example",
    )
    return parser.parse_args(argv)


def main(argv: List[str] | None = None) -> int:
    args = _parse_args(argv)
    config_threshold = (
        STRICT_COVERAGE_THRESHOLD if args.strict else CONFIG_COVERAGE_THRESHOLD
    )
    env_threshold = STRICT_COVERAGE_THRESHOLD if args.strict else ENV_COVERAGE_THRESHOLD

    json_example = _load_json_example()
    yaml_example = _load_yaml_example()
    env_map_vars = _extract_env_map_vars()

    json_lines, json_cov = _check_config_coverage(json_example, "config.example.json")
    yaml_lines, yaml_cov = _check_config_coverage(yaml_example, "config.example.yaml")
    env_lines, env_cov = _check_env_coverage(env_map_vars)

    print("\n".join(json_lines + yaml_lines + env_lines))

    ok = True
    if json_cov < config_threshold:
        print(f"ERROR: config.example.json coverage < {config_threshold:.0%}")
        ok = False
    if yaml_cov < config_threshold:
        print(f"ERROR: config.example.yaml coverage < {config_threshold:.0%}")
        ok = False
    if env_cov < env_threshold:
        print(f"ERROR: .env.example coverage < {env_threshold:.0%}")
        ok = False

    if ok:
        mode = "strict " if args.strict else ""
        print(f"OK: config examples meet {mode}coverage thresholds.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
