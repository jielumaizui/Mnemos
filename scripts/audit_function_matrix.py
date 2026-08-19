#!/usr/bin/env python3
"""Validate the Mnemos function acceptance matrix."""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MATRIX = ROOT / "docs" / "acceptance" / "function_matrix.json"
ALLOWED_STATUS = {"usable", "partial", "experimental", "outdated", "removed"}
REQUIRED_FIELDS = [
    "id",
    "group",
    "feature_name",
    "user_entrypoints",
    "code_entrypoints",
    "data_dependencies",
    "expected_input",
    "expected_output",
    "data_landing",
    "success_standard",
    "failure_or_degradation",
    "docs",
    "status",
    "validation_commands",
]


def _subparser_action(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _cli_commands() -> set[str]:
    sys.path.insert(0, str(ROOT))
    import mnemos_cli

    commands: set[str] = set()

    def walk(parser: argparse.ArgumentParser, prefix: list[str]) -> None:
        subparsers = _subparser_action(parser)
        if subparsers is None:
            if prefix:
                commands.add(" ".join(prefix))
            return
        for name, child in subparsers.choices.items():
            path = [*prefix, name]
            commands.add(" ".join(path))
            walk(child, path)

    walk(mnemos_cli.build_parser(), [])
    return commands


def _mcp_schema_tools() -> set[str]:
    sys.path.insert(0, str(ROOT))
    from integrations.agora_tools import schema

    return {tool["name"] for tool in schema.list_tools(lambda _name: "matrix")["tools"]}


def _mcp_registered_tools() -> set[str]:
    source = (ROOT / "integrations" / "agora.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_register_tools":
            for child in ast.walk(node):
                if isinstance(child, ast.Dict):
                    names = set()
                    for key in child.keys:
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            names.add(key.value)
                    if names:
                        return names
    return set()


def _cli_command_path(entrypoint: str, cli_commands: set[str]) -> str:
    """Return the argparse command path from a documented CLI entrypoint."""
    tokens = entrypoint.removeprefix("cli:").split()
    command_tokens = [token for token in tokens if not token.startswith("-")]
    while command_tokens:
        candidate = " ".join(command_tokens)
        if candidate in cli_commands:
            return candidate
        command_tokens.pop()
    return " ".join(tokens)


def _entry_exists(entrypoint: str, cli_commands: set[str], mcp_tools: set[str]) -> bool:
    if entrypoint.startswith("cli:"):
        return _cli_command_path(entrypoint, cli_commands) in cli_commands
    if entrypoint.startswith("mcp:"):
        return entrypoint.removeprefix("mcp:") in mcp_tools
    return True


def _path_exists(ref: str) -> bool:
    path = ref.split(":", 1)[0]
    if path.startswith(("cli:", "mcp:", "internal:", "daemon:", "manual:", "none")):
        return True
    return (ROOT / path).exists()


def _validate_feature(
    feature: dict[str, Any],
    *,
    cli_commands: set[str],
    mcp_tools: set[str],
) -> list[str]:
    errors: list[str] = []
    feature_id = str(feature.get("id", "<missing-id>"))
    for field in REQUIRED_FIELDS:
        if field not in feature:
            errors.append(f"{feature_id}: missing field {field}")
            continue
        value = feature[field]
        if value in ("", None, [], {}):
            errors.append(f"{feature_id}: empty field {field}")

    status = feature.get("status")
    if status not in ALLOWED_STATUS:
        errors.append(f"{feature_id}: invalid status {status!r}")

    for entrypoint in feature.get("user_entrypoints", []):
        if not _entry_exists(str(entrypoint), cli_commands, mcp_tools):
            errors.append(f"{feature_id}: unknown user entrypoint {entrypoint}")

    for ref in [*feature.get("code_entrypoints", []), *feature.get("docs", [])]:
        if not _path_exists(str(ref)):
            errors.append(f"{feature_id}: missing path {ref}")

    return errors


def validate(matrix_path: Path = DEFAULT_MATRIX) -> list[str]:
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    features = matrix.get("features", [])
    errors: list[str] = []
    if not features:
        errors.append("matrix has no features")

    cli_commands = _cli_commands()
    schema_tools = _mcp_schema_tools()
    registered_tools = _mcp_registered_tools()
    missing_from_schema = sorted(registered_tools - schema_tools)
    missing_from_register = sorted(schema_tools - registered_tools)
    if missing_from_schema:
        errors.append(f"MCP tools registered but not in schema: {missing_from_schema}")
    if missing_from_register:
        errors.append(f"MCP tools in schema but not registered: {missing_from_register}")

    seen_ids: set[str] = set()
    for feature in features:
        feature_id = str(feature.get("id", ""))
        if feature_id in seen_ids:
            errors.append(f"duplicate feature id {feature_id}")
        seen_ids.add(feature_id)
        errors.extend(
            _validate_feature(
                feature,
                cli_commands=cli_commands,
                mcp_tools=schema_tools,
            )
        )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "matrix",
        nargs="?",
        default=str(DEFAULT_MATRIX),
        help="Path to function_matrix.json",
    )
    args = parser.parse_args(argv)
    errors = validate(Path(args.matrix))
    if errors:
        print("Function matrix audit failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Function matrix audit passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
