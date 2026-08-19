#!/usr/bin/env python3
"""Generate config/config.example.* and config/.env.example from DEFAULT_CONFIG.

Run from repo root:
    python3 scripts/generate_config_examples.py
"""

import json
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts._config_example_data import ENV_VAR_GROUPS, EXAMPLE_CONFIG  # noqa: E402

CONFIG_DIR = REPO_ROOT / "config"

JSON_PATH = CONFIG_DIR / "config.example.json"
YAML_PATH = CONFIG_DIR / "config.example.yaml"
ENV_PATH = CONFIG_DIR / ".env.example"


def write_json_example() -> None:
    JSON_PATH.write_text(
        json.dumps(EXAMPLE_CONFIG, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_yaml_example() -> None:
    lines = [
        "# Mnemos configuration example (YAML)",
        "# This file is auto-generated from core/config.py::DEFAULT_CONFIG.",
        "# Uncomment and modify the values you want to override.",
        "",
    ]
    for key, value in EXAMPLE_CONFIG.items():
        lines.append(f"# === {key} ===")
        section = yaml.safe_dump(
            {key: value},
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )
        lines.extend(section.rstrip().splitlines())
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    YAML_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_env_example() -> None:
    lines = [
        "# Mnemos - Local AI Knowledge System Environment Configuration",
        "# This file is auto-generated from core/config.py and core/llm_config.py.",
        "# Copy this file to .env and fill in your actual values.",
        "",
    ]
    for section, vars in ENV_VAR_GROUPS:
        lines.append(f"# {'-' * 60}")
        lines.append(f"# {section}")
        lines.append(f"# {'-' * 60}")
        for name, desc in vars:
            if name.startswith("MNEMOS_<"):
                lines.append(f"# {desc}")
            else:
                lines.append(f"# {desc}")
                lines.append(f"# {name}=")
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    write_json_example()
    write_yaml_example()
    write_env_example()
    print(f"Generated:\n  {JSON_PATH}\n  {YAML_PATH}\n  {ENV_PATH}")


if __name__ == "__main__":
    main()
