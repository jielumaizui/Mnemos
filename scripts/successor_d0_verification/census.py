"""Private implementation module for successor_d0_verification.census."""

from __future__ import annotations

from dataclasses import dataclass

from dataclasses import field as dataclass_field

from pathlib import Path

from typing import Any

from typing import Mapping

from typing import Sequence

import ast

import hashlib

import json

import re

try:

    import tomllib  # type: ignore[import-untyped]

except ImportError:  # pragma: no cover - Python 3.10 compatibility

    import tomli as tomllib  # type: ignore[import-untyped,no-redef]

from .wire import (
    _canonical_json_bytes,
    _sha256,
)


def _v1_slug(value: object) -> str:
    """Derive a v1 wire slug without importing generator implementation."""

    normalized = re.sub(r"[^a-z0-9._-]+", ".", str(value).strip().lower())
    return normalized.strip(".") or "unknown"


def _v1_stable_digest(value: object, *, length: int = 24) -> str:
    """Derive the v1 truncated identity from canonical JSON, independently."""

    canonical = _canonical_json_bytes(value)
    return hashlib.sha256(canonical[:-1]).hexdigest()[:length]


def _has_main_guard(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not isinstance(test, ast.Compare) or len(test.ops) != 1:
            continue
        values = [test.left, *test.comparators]
        has_name = any(isinstance(value, ast.Name) and value.id == "__name__" for value in values)
        has_main = any(
            isinstance(value, ast.Constant) and value.value == "__main__" for value in values
        )
        if has_name and has_main:
            return True
    return False


def _assignment_literal(tree: ast.AST, name: str) -> Any:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == name for target in targets):
                if node.value is None:
                    continue
                try:
                    return ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    return None
    return None


def _assignment_dict_keys(tree: ast.AST, name: str) -> list[str]:
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == name for target in targets):
            continue
        if not isinstance(node.value, ast.Dict):
            return []
        return sorted(
            str(key.value)
            for key in node.value.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        )
    return []


def _main_cli_dispatch(snapshot_root: Path) -> dict[str, Any]:
    tree = _parse_tree(snapshot_root / "mnemos_cli.py", snapshot_root)
    direct = _assignment_dict_keys(tree, "_COMMAND_ROUTES")
    subcommand = _assignment_dict_keys(tree, "_SUBCOMMAND_ROUTES")
    special: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name != "main":
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Compare):
                continue
            operands = [child.left, *child.comparators]
            has_command = any(
                isinstance(value, ast.Attribute)
                and isinstance(value.value, ast.Name)
                and value.value.id == "args"
                and value.attr == "command"
                for value in operands
            )
            if not has_command:
                continue
            special.update(
                str(value.value)
                for value in operands
                if isinstance(value, ast.Constant) and isinstance(value.value, str)
            )
    all_routes = sorted(set(direct) | set(subcommand) | special)
    return {
        "direct": direct,
        "subcommand": subcommand,
        "special": sorted(special),
        "all": all_routes,
    }


def _parse_tree(path: Path, snapshot_root: Path) -> ast.Module:
    relative = path.relative_to(snapshot_root).as_posix()
    return ast.parse(path.read_text(encoding="utf-8"), filename=relative)


@dataclass
class _StaticParser:
    path: tuple[str, ...]
    arguments: list[dict[str, Any]] = dataclass_field(default_factory=list)


@dataclass(frozen=True)
class _StaticSubparsers:
    parent: _StaticParser


def _independent_cli_census(snapshot_root: Path) -> dict[str, Any]:
    """Interpret only the declarative argparse builder AST.

    Importing a historical ``mnemos_cli`` would execute code selected by the
    manifest.  This deliberately small interpreter accepts the statement and
    expression forms present in the frozen ``build_parser`` function and fails
    closed on an unresolved parser operation.
    """

    tree = _parse_tree(snapshot_root / "mnemos_cli.py", snapshot_root)
    builder = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "build_parser"
        ),
        None,
    )
    if builder is None:
        raise ValueError("mnemos_cli.build_parser is missing")

    environment: dict[str, Any] = {}
    parsers: dict[tuple[str, ...], _StaticParser] = {}
    children: dict[tuple[str, ...], set[tuple[str, ...]]] = {}
    parser_helpers = {
        "add_agent_mcp_grant_parser": (
            "core/cli/commands/agent.py",
            "add_agent_mcp_grant_parser",
        ),
        "add_model_call_ledger_migration_args": (
            "core/cli/commands/migrate.py",
            "add_model_call_ledger_migration_args",
        ),
    }

    def evaluate(node: ast.AST | None) -> Any:
        if node is None:
            return None
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in environment:
                return {"unresolved_name": node.id}
            return environment[node.id]
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            values = [evaluate(item) for item in node.elts]
            return tuple(values) if isinstance(node, ast.Tuple) else values
        if isinstance(node, ast.Dict):
            return {evaluate(key): evaluate(value) for key, value in zip(node.keys, node.values)}
        if isinstance(node, ast.Attribute):
            return {"expression": ast.unparse(node)}
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in parser_helpers
        ):
            relative, function_name = parser_helpers[node.func.id]
            helper_tree = _parse_tree(
                snapshot_root / relative,
                snapshot_root,
            )
            helper = next(
                (
                    item
                    for item in helper_tree.body
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name == function_name
                ),
                None,
            )
            if helper is None:
                raise ValueError(f"argparse helper is missing: {function_name}")
            parameters = [*helper.args.posonlyargs, *helper.args.args]
            if len(node.args) != len(parameters):
                raise ValueError(f"argparse helper signature changed: {function_name}")
            arguments = [evaluate(item) for item in node.args]
            if not all(isinstance(item, (_StaticParser, _StaticSubparsers)) for item in arguments):
                raise ValueError(f"argparse helper receiver is unresolved: {function_name}")
            saved_environment = dict(environment)
            environment.clear()
            environment.update(
                {parameter.arg: argument for parameter, argument in zip(parameters, arguments)}
            )
            try:
                execute(helper.body)
            finally:
                environment.clear()
                environment.update(saved_environment)
            return None
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            return {"expression": ast.unparse(node)}

        method = node.func.attr
        receiver = evaluate(node.func.value)
        positional = [evaluate(item) for item in node.args]
        keywords = {item.arg: evaluate(item.value) for item in node.keywords if item.arg}
        if method == "ArgumentParser" and isinstance(node.func.value, ast.Name):
            parser = _StaticParser(())
            parsers[()] = parser
            children.setdefault((), set())
            return parser
        if method == "add_subparsers" and isinstance(receiver, _StaticParser):
            return _StaticSubparsers(receiver)
        if method == "add_parser" and isinstance(receiver, _StaticSubparsers):
            if not positional or not isinstance(positional[0], str) or not positional[0]:
                raise ValueError(f"unresolved add_parser name at line {node.lineno}")
            path = (*receiver.parent.path, positional[0])
            if path in parsers:
                raise ValueError(f"duplicate argparse path: {' '.join(path)}")
            parser = _StaticParser(path)
            parsers[path] = parser
            children.setdefault(path, set())
            children.setdefault(receiver.parent.path, set()).add(path)
            return parser
        if method == "add_argument" and isinstance(receiver, _StaticParser):
            if not positional or not isinstance(positional[0], str):
                raise ValueError(f"unresolved add_argument at line {node.lineno}")
            options = [item for item in positional if isinstance(item, str)]
            optional = bool(options and options[0].startswith("-"))
            explicit_dest = keywords.get("dest")
            if isinstance(explicit_dest, str) and explicit_dest:
                destination_name = explicit_dest
            elif optional:
                preferred = next((item for item in options if item.startswith("--")), options[0])
                destination_name = preferred.lstrip("-").replace("-", "_")
            else:
                destination_name = options[0]
            receiver.arguments.append(
                {
                    "origin_command": " ".join(receiver.path),
                    "optional": optional,
                    "boolean": keywords.get("action") in {"store_true", "store_false"},
                    "choices": keywords.get("choices"),
                    "option_strings": options if optional else [],
                    "dest": destination_name,
                    "action": keywords.get("action"),
                    "required": keywords.get("required", False),
                    "nargs": keywords.get("nargs"),
                }
            )
            return None
        # Non-parser calls used as values (for example a custom type) are
        # opaque and never executed.
        return {"expression": ast.unparse(node)}

    def assign(target: ast.AST, value: Any) -> None:
        if isinstance(target, ast.Name):
            environment[target.id] = value
            return
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and isinstance(environment.get(target.value.id), _StaticParser)
            and target.attr in {"description", "epilog"}
        ):
            return
        if isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (list, tuple)):
            if len(target.elts) != len(value):
                raise ValueError("argparse builder loop destructuring changed")
            for child, item in zip(target.elts, value):
                assign(child, item)
            return
        raise ValueError(
            f"unsupported argparse assignment at line {getattr(target, 'lineno', 'unknown')}"
        )

    def execute(statements: Sequence[ast.stmt]) -> None:
        for statement in statements:
            if isinstance(statement, ast.Assign):
                value = evaluate(statement.value)
                for target in statement.targets:
                    assign(target, value)
            elif isinstance(statement, ast.AnnAssign):
                assign(statement.target, evaluate(statement.value))
            elif isinstance(statement, ast.Expr):
                evaluate(statement.value)
            elif isinstance(statement, ast.For):
                iterable = evaluate(statement.iter)
                if not isinstance(iterable, (list, tuple)):
                    raise ValueError(f"unresolved argparse loop at line {statement.lineno}")
                for item in iterable:
                    assign(statement.target, item)
                    execute(statement.body)
                if statement.orelse:
                    execute(statement.orelse)
            elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Import)):
                continue
            elif isinstance(statement, ast.Return):
                break
            else:
                raise ValueError(
                    f"unsupported build_parser statement {type(statement).__name__} "
                    f"at line {statement.lineno}"
                )

    execute(builder.body)
    if () not in parsers:
        raise ValueError("argparse root parser was not statically reconstructed")
    leaves = sorted(path for path in parsers if path and not children.get(path))
    effective_arguments: list[dict[str, Any]] = []
    leaf_argument_counts: dict[str, int] = {}
    leaf_boolean_counts: dict[str, int] = {}
    leaf_argument_signatures: list[str] = []
    selector_record_ids: dict[str, set[str]] = {}
    for leaf in leaves:
        leaf_arguments: list[dict[str, Any]] = []
        for depth in range(0, len(leaf) + 1):
            parent = leaf[:depth]
            if parent in parsers:
                leaf_arguments.extend(parsers[parent].arguments)
        effective_arguments.extend(leaf_arguments)
        leaf_name = " ".join(leaf)
        command_record_id = f"surface:cli.{_v1_slug(leaf_name)}"
        selector_record_ids.setdefault(f"cli:{leaf_name}", set()).add(command_record_id)
        leaf_argument_counts[leaf_name] = len(leaf_arguments)
        leaf_boolean_counts[leaf_name] = sum(bool(item["boolean"]) for item in leaf_arguments)
        for item in leaf_arguments:
            identity = {
                "command": leaf_name,
                "origin": item["origin_command"],
                "dest": item["dest"],
                "options": item["option_strings"],
            }
            facet_record_id = f"surface:cli-facet.{_v1_stable_digest(identity)}"
            for option in item["option_strings"]:
                selector_record_ids.setdefault(f"cli:{leaf_name} {option}", set()).add(
                    facet_record_id
                )
            signature = {
                "command": leaf_name,
                "option_strings": item["option_strings"],
                "dest": item["dest"],
                "action": item["action"],
                "choices": item["choices"],
                "required": item["required"],
                "nargs": item["nargs"],
            }
            leaf_argument_signatures.append(
                _canonical_json_bytes(signature).decode("utf-8").rstrip("\n")
            )
    defined_arguments = [argument for parser in parsers.values() for argument in parser.arguments]
    choice_arguments = [
        item for item in defined_arguments if isinstance(item["choices"], (list, tuple))
    ]
    effective_choice_arguments = [
        item for item in effective_arguments if isinstance(item["choices"], (list, tuple))
    ]
    return {
        "top": sorted(path[0] for path in parsers if len(path) == 1),
        "nodes": sorted(" ".join(path) for path in parsers if path),
        "leaves": [" ".join(path) for path in leaves],
        "parameter_action_count": len(defined_arguments),
        "optional_action_count": sum(bool(item["optional"]) for item in defined_arguments),
        "positional_action_count": sum(not item["optional"] for item in defined_arguments),
        "boolean_action_count": sum(bool(item["boolean"]) for item in defined_arguments),
        "choice_action_count": len(choice_arguments),
        "choice_value_count": sum(len(item["choices"]) for item in choice_arguments),
        "effective_parameter_facet_count": len(effective_arguments),
        "effective_optional_facet_count": sum(
            bool(item["optional"]) for item in effective_arguments
        ),
        "effective_positional_facet_count": sum(
            not item["optional"] for item in effective_arguments
        ),
        "effective_boolean_facet_count": sum(bool(item["boolean"]) for item in effective_arguments),
        "effective_choice_facet_count": len(effective_choice_arguments),
        "effective_choice_value_count": sum(
            len(item["choices"]) for item in effective_choice_arguments
        ),
        "leaf_argument_counts": leaf_argument_counts,
        "leaf_boolean_counts": leaf_boolean_counts,
        "leaf_argument_signatures": sorted(leaf_argument_signatures),
        "selector_record_ids": {
            selector: sorted(record_ids)
            for selector, record_ids in sorted(selector_record_ids.items())
        },
    }


def _daemon_cli_modes(snapshot_root: Path) -> list[str]:
    path = snapshot_root / "daemon/entrypoint_support.py"
    tree = _parse_tree(path, snapshot_root)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "add_argument" or not node.args:
            continue
        try:
            selector = ast.literal_eval(node.args[0])
        except (ValueError, TypeError):
            continue
        if selector != "command":
            continue
        for keyword in node.keywords:
            if keyword.arg == "choices":
                value = ast.literal_eval(keyword.value)
                return sorted(str(item) for item in value)
    raise ValueError("daemon command choices were not found")


def _mcp_census(snapshot_root: Path) -> dict[str, Any]:
    schema_tree = _parse_tree(snapshot_root / "integrations/agora_tools/schema.py", snapshot_root)
    schema_tools: set[str] = set()
    schema_hashes: dict[str, str] = {}
    for node in ast.walk(schema_tree):
        if not isinstance(node, ast.Dict):
            continue
        fields: dict[str, ast.AST] = {}
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                fields[key.value] = value
        if "name" not in fields or "inputSchema" not in fields:
            continue
        try:
            name = ast.literal_eval(fields["name"])
        except (ValueError, TypeError):
            continue
        if isinstance(name, str):
            schema_tools.add(name)
            try:
                input_schema = ast.literal_eval(fields["inputSchema"])
            except (ValueError, TypeError) as exc:
                raise ValueError(f"MCP inputSchema is not static for {name}") from exc
            schema_hashes[name] = _sha256(_canonical_json_bytes(input_schema))

    agora_tree = _parse_tree(snapshot_root / "integrations/agora.py", snapshot_root)
    category_map = _assignment_literal(agora_tree, "_TOOL_CATEGORIES") or {}
    categorized = {
        str(name): str(category) for category, names in category_map.items() for name in names
    }
    registered: set[str] = set()
    protocol_methods: set[str] = set()
    for node in ast.walk(agora_tree):
        if (
            not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            or node.name != "_register_tools"
        ):
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Dict):
                continue
            for key in child.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    registered.add(key.value)
    for node in ast.walk(agora_tree):
        if (
            not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            or node.name != "handle_request"
        ):
            continue
        protocol_methods.update(
            str(child.value)
            for child in ast.walk(node)
            if isinstance(child, ast.Constant)
            and isinstance(child.value, str)
            and child.value
            in {
                "initialize",
                "notifications/initialized",
                "tools/list",
                "tools/call",
            }
        )

    policy_tree = _parse_tree(snapshot_root / "core/access_policy.py", snapshot_root)
    policies = _assignment_literal(policy_tree, "MCP_TOOL_POLICIES") or {}
    category_counts = {
        category: sum(value == category for value in categorized.values())
        for category in sorted(set(categorized.values()))
    }
    tool_signatures = []
    for name in sorted(set(schema_tools) | registered | set(categorized) | set(policies)):
        tool_signatures.append(
            _canonical_json_bytes(
                {
                    "name": name,
                    "input_schema_sha256": schema_hashes.get(name),
                    "registered": name in registered,
                    "category": categorized.get(name),
                    "policy": policies.get(name),
                }
            )
            .decode("utf-8")
            .rstrip("\n")
        )
    return {
        "schema_tools": sorted(schema_tools),
        "registered_tools": sorted(registered),
        "categorized_tools": sorted(categorized),
        "policy_tools": sorted(str(name) for name in policies),
        "protocol_methods": sorted(protocol_methods),
        "category_counts": category_counts,
        "policy_counts": {
            policy: sum(value == policy for value in policies.values())
            for policy in sorted(set(policies.values()))
        },
        "tool_signatures": tool_signatures,
    }


def _facade_methods(snapshot_root: Path) -> list[str]:
    tree = _parse_tree(snapshot_root / "core/application/contracts.py", snapshot_root)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "MnemosServiceFacade":
            return sorted(
                child.name
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not child.name.startswith("_")
            )
    raise ValueError("MnemosServiceFacade Protocol was not found")


def _daemon_services(snapshot_root: Path) -> dict[str, Any]:
    interval_tree = _parse_tree(snapshot_root / "daemon/intervals.py", snapshot_root)
    interval_names: set[str] = set()
    for node in ast.walk(interval_tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "build_default_intervals":
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Return) or not isinstance(child.value, ast.Dict):
                continue
            for key in child.value.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    interval_names.add(key.value)
    registry_tree = _parse_tree(snapshot_root / "daemon/service_registry.py", snapshot_root)
    direct = _assignment_literal(registry_tree, "DIRECT_SERVICE_TARGETS") or {}
    configured = _assignment_literal(registry_tree, "CFG_SERVICE_TARGETS") or {}
    handlers = {**direct, **configured}
    return {
        "intervals": sorted(interval_names),
        "handlers": sorted(str(name) for name in handlers),
        "aliases": sorted(set(handlers) - interval_names),
        "handler_gap": sorted(interval_names - set(handlers)),
    }


def _chronos_steps(snapshot_root: Path) -> dict[str, str]:
    steps: dict[str, str] = {}
    for relative in (
        "core/kia/chronos_scheduler_support.py",
        "core/kia/chronos_builtin_steps.py",
    ):
        tree = _parse_tree(snapshot_root / relative, snapshot_root)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function_name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr if isinstance(node.func, ast.Attribute) else ""
            )
            if function_name != "ScheduledStep":
                continue
            keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
            try:
                name = ast.literal_eval(keywords["name"])
            except (KeyError, ValueError, TypeError):
                continue
            trigger = keywords.get("trigger")
            if isinstance(trigger, ast.Call):
                trigger_name = (
                    trigger.func.id
                    if isinstance(trigger.func, ast.Name)
                    else trigger.func.attr if isinstance(trigger.func, ast.Attribute) else "unknown"
                )
            else:
                trigger_name = "unknown"
            normalized = {
                "CronTrigger": "cron",
                "ConditionTrigger": "condition",
                "PassiveTrigger": "passive",
            }.get(trigger_name, trigger_name.lower())
            steps[str(name)] = normalized
    chronos_tree = _parse_tree(snapshot_root / "core/kia/chronos.py", snapshot_root)
    for name, _event_type in _assignment_literal(chronos_tree, "EVENT_TRIGGER_ROUTES") or ():
        steps[str(name)] = "event"
    return dict(sorted(steps.items()))


def _event_policy(snapshot_root: Path) -> dict[str, list[str]]:
    tree = _parse_tree(snapshot_root / "core/mnemos_bus.py", snapshot_root)
    persistent = sorted(
        str(item) for item in (_assignment_literal(tree, "_PERSISTENT_EVENT_TYPES") or ())
    )
    no_persist = sorted(
        str(item) for item in (_assignment_literal(tree, "_NO_PERSIST_EVENT_TYPES") or ())
    )
    return {"persistent": persistent, "no_persist": no_persist}


def _enclosing_nodes(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _string_constants(tree: ast.AST) -> dict[str, str]:
    constants: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if node.value is None:
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            continue
        if not isinstance(value, str):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                constants[target.id] = value
    return constants


def _loop_values(for_node: ast.For, variable: str) -> list[str]:
    if isinstance(for_node.target, ast.Name) and for_node.target.id == variable:
        try:
            values = ast.literal_eval(for_node.iter)
        except (ValueError, TypeError):
            return []
        return [str(value) for value in values if isinstance(value, str)]
    return []


def _function_default(function: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> str | None:
    positional = [*function.args.posonlyargs, *function.args.args]
    offset = len(positional) - len(function.args.defaults)
    for index, argument in enumerate(positional):
        if argument.arg != name or index < offset:
            continue
        try:
            value = ast.literal_eval(function.args.defaults[index - offset])
        except (ValueError, TypeError):
            return None
        return value if isinstance(value, str) else None
    return None


def _event_subscriptions(snapshot_root: Path) -> dict[str, Any]:
    chronos_routes = (
        _assignment_literal(
            _parse_tree(snapshot_root / "core/kia/chronos.py", snapshot_root),
            "EVENT_TRIGGER_ROUTES",
        )
        or ()
    )
    edges: set[tuple[str, int, str]] = set()
    unresolved: list[str] = []
    search_roots = [
        *(snapshot_root / base for base in ("core", "daemon", "integrations", "scripts")),
        snapshot_root / "mnemos_daemon.py",
    ]
    for search_root in search_roots:
        paths = [search_root] if search_root.is_file() else sorted(search_root.rglob("*.py"))
        for path in paths:
            tree = _parse_tree(path, snapshot_root)
            parents = _enclosing_nodes(tree)
            constants = _string_constants(tree)
            relative = path.relative_to(snapshot_root).as_posix()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr != "subscribe" or not node.args:
                    continue
                argument = node.args[0]
                values: list[str] = []
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    values = [argument.value]
                elif isinstance(argument, ast.Name):
                    if argument.id in {"bus", "event_bus"}:
                        continue
                    if argument.id in constants:
                        values = [constants[argument.id]]
                    current: ast.AST | None = node
                    while current in parents and not values:
                        current = parents[current]
                        if isinstance(current, ast.For):
                            values = _loop_values(current, argument.id)
                            if (
                                not values
                                and isinstance(current.target, ast.Tuple)
                                and any(
                                    isinstance(item, ast.Name) and item.id == argument.id
                                    for item in current.target.elts
                                )
                            ):
                                values = [str(route[1]) for route in chronos_routes]
                        elif isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            default = _function_default(current, argument.id)
                            if default is not None:
                                values = [default]
                if not values:
                    unresolved.append(f"{relative}:{node.lineno}:{ast.unparse(argument)}")
                for value in values:
                    edges.add((relative, node.lineno, value))
    return {
        "edges": sorted(f"{path}:{line}:{event}" for path, line, event in edges),
        "topics": sorted({event for _path, _line, event in edges if event != "*"}),
        "wildcard_edges": sum(event == "*" for _path, _line, event in edges),
        "unresolved": sorted(unresolved),
    }


def _named_tuple(snapshot_root: Path, relative: str, name: str) -> list[str]:
    tree = _parse_tree(snapshot_root / relative, snapshot_root)
    value = _assignment_literal(tree, name) or ()
    return [str(item) for item in value]


def _tuple_object_hook(value: dict[str, Any]) -> Any:
    if set(value) == {"__mnemos_tuple__"} and isinstance(value.get("__mnemos_tuple__"), list):
        return tuple(value["__mnemos_tuple__"])
    return value


def _expected_coverage_edge_contracts(
    *,
    function_payload: Mapping[str, Any],
    cli_selector_record_ids: Mapping[str, Sequence[str]],
    cli_leaves: Sequence[str],
    daemon_cli_modes: Sequence[str],
    daemon_controlled_modes: Sequence[str],
    daemon_service_names: Sequence[str],
    mcp_tool_names: Sequence[str],
    source_ids: Sequence[str],
    chronos_steps: Sequence[str],
    script_modules: Sequence[str],
) -> list[str]:
    """Rebuild the exact semantic coverage-edge multiset from frozen sources."""

    selector_map: dict[str, set[str]] = {
        selector: {str(record_id) for record_id in record_ids}
        for selector, record_ids in cli_selector_record_ids.items()
    }

    def bind(selector: str, record_id: str) -> None:
        selector_map.setdefault(selector, set()).add(record_id)

    for mode in daemon_cli_modes:
        bind(f"daemon:{mode}", f"surface:daemon-mode.{_v1_slug(mode)}")
    for mode in daemon_controlled_modes:
        bind(
            f"daemon:{mode} --controlled-raw-sync-only",
            f"surface:daemon-mode.{_v1_slug(mode)}.controlled-raw-sync-only",
        )
    for name in daemon_service_names:
        bind(f"daemon:{name}", f"surface:daemon-service.{_v1_slug(name)}")
    for name in mcp_tool_names:
        bind(f"mcp:{name}", f"surface:mcp.{_v1_slug(name)}")
    for name in source_ids:
        record_id = f"surface:source.{_v1_slug(name)}"
        bind(f"source:{name}", record_id)
        bind(f"agent_kit:{name}", record_id)
    for name in chronos_steps:
        bind(f"scheduler:{name}", f"surface:chronos.{_v1_slug(name)}")
    for relative in script_modules:
        bind(
            f"script:{relative}",
            f"surface:script.{_v1_stable_digest(relative)}",
        )

    raw_features = function_payload.get("features", [])
    if not isinstance(raw_features, list):
        raise ValueError("function_matrix.features must be a list")
    features_by_capability: dict[str, Mapping[str, Any]] = {}
    for feature in raw_features:
        if not isinstance(feature, Mapping) or not str(feature.get("id") or ""):
            raise ValueError("function_matrix feature is invalid")
        capability_id = f"cap:{_v1_slug(feature['id'])}"
        features_by_capability[capability_id] = feature

    cli_leaf_set = {str(item) for item in cli_leaves}
    contracts: list[str] = []

    def add_contract(
        *,
        from_id: str,
        relation: str,
        to_id: str,
        facet: str,
        assertion_authority: str,
    ) -> None:
        contracts.append(
            _canonical_json_bytes(
                {
                    "assertion_authority": assertion_authority,
                    "facet": facet,
                    "from_id": from_id,
                    "relation": relation,
                    "to_id": to_id,
                }
            )
            .decode("utf-8")
            .rstrip("\n")
        )

    for capability_id, feature in sorted(features_by_capability.items()):
        raw_entrypoints = feature.get("user_entrypoints", [])
        if not isinstance(raw_entrypoints, list):
            raise ValueError(f"function_matrix {feature['id']} user_entrypoints must be a list")
        for raw_entrypoint in raw_entrypoints:
            entrypoint = str(raw_entrypoint)
            if entrypoint.startswith("cli:"):
                body = entrypoint.removeprefix("cli:")
                command_tokens = [token for token in body.split() if not token.startswith("-")]
                command = ""
                while command_tokens:
                    candidate = " ".join(command_tokens)
                    if candidate in cli_leaf_set:
                        command = candidate
                        break
                    command_tokens.pop()
                selectors = [f"cli:{command}"] if command else []
                if command:
                    selectors.extend(
                        f"cli:{command} {token}" for token in body.split() if token.startswith("-")
                    )
            elif entrypoint.startswith("script:"):
                match = re.search(r"scripts/[^\s]+?\.py", entrypoint)
                selectors = [f"script:{match.group(0)}"] if match else []
            else:
                selectors = [entrypoint]
            matched = {
                record_id for selector in selectors for record_id in selector_map.get(selector, ())
            }
            for surface_id in sorted(matched):
                add_contract(
                    from_id=surface_id,
                    relation="SURFACE_EXPOSES_CAPABILITY",
                    to_id=capability_id,
                    facet=entrypoint,
                    assertion_authority="MECHANICAL",
                )

        raw_commands = feature.get("validation_commands", [])
        if not isinstance(raw_commands, list):
            raise ValueError(f"function_matrix {feature['id']} validation_commands must be a list")
        oracle_ids = {
            f"oracle:function-validation.{_v1_stable_digest([capability_id, str(command)])}"
            for command in raw_commands
        }
        for oracle_id in sorted(oracle_ids):
            add_contract(
                from_id=capability_id,
                relation="CAPABILITY_VERIFIED_BY_ORACLE",
                to_id=oracle_id,
                facet="legacy_validation_command",
                assertion_authority="DECLARED_LEGACY",
            )
    return sorted(contracts)


def _independent_static_census(snapshot_root: Path) -> dict[str, Any]:
    """Re-enumerate key bounded sets without importing the catalog generator."""

    cli = _independent_cli_census(snapshot_root)
    cli_dispatch = _main_cli_dispatch(snapshot_root)
    project = tomllib.loads((snapshot_root / "pyproject.toml").read_text(encoding="utf-8"))
    console_script_map = {
        str(name): str(target)
        for name, target in project.get("project", {}).get("scripts", {}).items()
    }
    console_scripts = sorted(console_script_map)
    daemon_cli_modes = _daemon_cli_modes(snapshot_root)
    daemon_tree = _parse_tree(
        snapshot_root / "daemon/entrypoint_support.py",
        snapshot_root,
    )
    daemon_controlled_modes: list[str] = []
    for node in ast.walk(daemon_tree):
        if not isinstance(node, ast.Set):
            continue
        try:
            values = {str(item) for item in ast.literal_eval(node)}
        except (TypeError, ValueError):
            continue
        if {"start", "stop", "status", "run"} <= values:
            daemon_controlled_modes = sorted(values)
            break
    mcp = _mcp_census(snapshot_root)
    facade = _facade_methods(snapshot_root)
    daemon_services = _daemon_services(snapshot_root)
    chronos = _chronos_steps(snapshot_root)
    event_policy = _event_policy(snapshot_root)
    event_subscriptions = _event_subscriptions(snapshot_root)
    health = _named_tuple(
        snapshot_root,
        "core/ops/health_contract.py",
        "CANONICAL_HEALTH_CHECK_IDS",
    )
    kia_modules = _named_tuple(snapshot_root, "core/kia/module_registry.py", "KIA_MODULE_IDS")
    scripts = sorted((snapshot_root / "scripts").rglob("*.py"))
    script_main: list[str] = []
    script_helper: list[str] = []
    script_trees: dict[str, ast.Module] = {}
    for path in scripts:
        relative = path.relative_to(snapshot_root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        script_trees[relative] = tree
        (script_main if _has_main_guard(tree) else script_helper).append(relative)
    module_paths = {
        Path(relative).with_suffix("").as_posix().replace("/", "."): relative
        for relative in script_trees
    }
    path_modules = {path: module for module, path in module_paths.items()}
    dependencies: dict[str, set[str]] = {path: set() for path in script_trees}
    for relative, tree in script_trees.items():
        current_module = path_modules[relative]
        for node in ast.walk(tree):
            candidates: list[str] = []
            if isinstance(node, ast.Import):
                candidates.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parent_parts = current_module.split(".")[: -node.level]
                    base = ".".join([*parent_parts, *([base] if base else [])])
                candidates.append(base)
                candidates.extend(f"{base}.{alias.name}" for alias in node.names if base)
            for candidate in candidates:
                if candidate in module_paths:
                    dependencies[relative].add(module_paths[candidate])
    reachable = set(script_main)
    pending = list(script_main)
    while pending:
        current = pending.pop()
        for dependency in dependencies[current]:
            if dependency not in reachable:
                reachable.add(dependency)
                pending.append(dependency)
    reachable_helpers = sorted(set(script_helper) & reachable)
    unreachable_helpers = sorted(set(script_helper) - reachable)
    import_time_effect_candidates = sorted(
        relative
        for relative in script_helper
        if any(
            isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
            for node in script_trees[relative].body
        )
    )

    requirement_payload = json.loads(
        (snapshot_root / "docs/acceptance/cognitive_requirement_test_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    requirement_keys = sorted(
        str(item["requirement_id"]) for item in requirement_payload.get("requirements", [])
    )
    function_payload = json.loads(
        (snapshot_root / "docs/acceptance/function_matrix.json").read_text(encoding="utf-8")
    )
    capability_keys = sorted(str(item["id"]) for item in function_payload.get("features", []))
    cli_commands = set(cli["nodes"])
    cli_leaves = set(cli["leaves"])
    mapped_cli: set[str] = set()
    mapped_mcp: set[str] = set()
    validation_edges: set[tuple[str, str]] = set()
    validation_ref_count = 0
    feature_test_refs: dict[str, set[str]] = {}
    for feature in function_payload.get("features", []):
        feature_id = str(feature["id"])
        for entrypoint in feature.get("user_entrypoints", []):
            entrypoint = str(entrypoint)
            if entrypoint.startswith("cli:"):
                tokens = [
                    token
                    for token in entrypoint.removeprefix("cli:").split()
                    if not token.startswith("-")
                ]
                while tokens and " ".join(tokens) not in cli_commands:
                    tokens.pop()
                if tokens:
                    mapped_cli.add(" ".join(tokens))
            elif entrypoint.startswith("mcp:"):
                mapped_mcp.add(entrypoint.removeprefix("mcp:"))
        for command in feature.get("validation_commands", []):
            for test_path in re.findall(r"tests/[A-Za-z0-9_./-]+\.py", str(command)):
                validation_ref_count += 1
                validation_edges.add((feature_id, test_path))
                feature_test_refs.setdefault(feature_id, set()).add(test_path)
    validation_files = sorted({path for _feature, path in validation_edges})
    test_files = sorted(
        path.relative_to(snapshot_root).as_posix()
        for pattern in ("test_*.py", "*_test.py")
        for path in (snapshot_root / "tests").rglob(pattern)
        if path.is_file()
    )
    source_payload = json.loads(
        (snapshot_root / "core/agent_kit/agent_source_support_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    sources = sorted(str(item["name"]) for item in source_payload.get("sources", []))
    script_modules = [path.relative_to(snapshot_root).as_posix() for path in scripts]
    mcp_tool_union = sorted(
        set(mcp["schema_tools"])
        | set(mcp["registered_tools"])
        | set(mcp["categorized_tools"])
        | set(mcp["policy_tools"])
    )
    daemon_service_names = sorted(
        set(daemon_services["intervals"]) | set(daemon_services["handlers"])
    )
    expected_coverage_edges = _expected_coverage_edge_contracts(
        function_payload=function_payload,
        cli_selector_record_ids=cli["selector_record_ids"],
        cli_leaves=cli["leaves"],
        daemon_cli_modes=daemon_cli_modes,
        daemon_controlled_modes=daemon_controlled_modes,
        daemon_service_names=daemon_service_names,
        mcp_tool_names=mcp_tool_union,
        source_ids=sources,
        chronos_steps=sorted(chronos),
        script_modules=script_modules,
    )
    source_parser_paths = sorted(
        str(item.get("parser", {}).get("module", "")).replace(".", "/") + ".py"
        for item in source_payload.get("sources", [])
        if isinstance(item, dict)
        and isinstance(item.get("parser"), dict)
        and item.get("parser", {}).get("module")
    )
    schema_payload = json.loads(
        (snapshot_root / "docs/acceptance/schema_owner_manifest.json").read_text(encoding="utf-8")
    )
    schema_paths = sorted(str(item["path"]) for item in schema_payload.get("entries", []))
    schema_reverse_paths: list[str] = []
    # Intentionally expressed independently from the generator's matcher.
    # SQL qualifiers may appear between CREATE and the owned object kind.
    ddl_pattern = re.compile(
        r"\b(?:"
        r"CREATE(?:\s+(?:OR\s+REPLACE|TEMP|TEMPORARY|UNIQUE|VIRTUAL))*"
        r"\s+(?:TABLE|INDEX|VIEW|TRIGGER)"
        r"|ALTER\s+(?:TABLE|INDEX|VIEW|TRIGGER)"
        r"|DROP\s+(?:TABLE|INDEX|VIEW|TRIGGER)"
        r")\b",
        re.IGNORECASE,
    )
    for prefix in ("core", "scripts", "daemon"):
        for path in (snapshot_root / prefix).rglob("*.py"):
            try:
                source_text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            if ddl_pattern.search(source_text):
                schema_reverse_paths.append(path.relative_to(snapshot_root).as_posix())

    governance_payload = json.loads(
        (snapshot_root / "scripts/phase1_governance_data.json").read_text(encoding="utf-8"),
        object_hook=_tuple_object_hook,
    )
    requirement_specs: dict[str, dict[str, Any]] = {}
    for source_key in (
        "PHASE0_SUPPORT_REQUIREMENT_SPECS",
        "PHASE1_ROOT_REQUIREMENT_SPECS",
    ):
        for item in governance_payload.get(source_key, ()):
            if isinstance(item, dict) and item.get("requirement_id"):
                requirement_specs[str(item["requirement_id"])] = dict(item)
    changed_nodes = governance_payload.get("PHASE1_CHANGED_TEST_NODE_IDS_BY_ROOT", {})
    canonical_requirement_nodes: set[str] = set()
    for requirement_id, spec in requirement_specs.items():
        canonical_requirement_nodes.update(str(item) for item in spec.get("node_ids", ()))
        if isinstance(changed_nodes, dict):
            canonical_requirement_nodes.update(
                str(item) for item in changed_nodes.get(requirement_id, ())
            )

    behavior_payload = json.loads(
        (snapshot_root / "docs/acceptance/cognitive_behavior_scenarios.json").read_text(
            encoding="utf-8"
        )
    )
    resilience_payload = json.loads(
        (snapshot_root / "docs/acceptance/ops_resilience_matrix.json").read_text(encoding="utf-8")
    )
    runtime_payload = json.loads(
        (snapshot_root / "docs/acceptance/cognitive_runtime_interface_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    audit_payload = json.loads(
        (snapshot_root / "docs/acceptance/audit_artifact_registry.json").read_text(encoding="utf-8")
    )
    release_payload = json.loads(
        (snapshot_root / "docs/acceptance/cognitive_release_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    validation_keys = [
        f"function-matrix:cap:{feature['id']}:validation:{ordinal}"
        for feature in function_payload.get("features", [])
        for ordinal, _command in enumerate(feature.get("validation_commands", []), start=1)
    ]
    guarded_main_outside_scripts: list[str] = []
    repo_entry_challenger_paths: list[str] = []
    executable_files: list[str] = []
    challenger_paths = [
        path for base in ("core", "integrations") for path in (snapshot_root / base).rglob("*.py")
    ]
    challenger_paths.extend(
        path
        for path in snapshot_root.glob("*.py")
        if path.name not in {"mnemos_cli.py", "mnemos_daemon.py"}
    )
    for path in sorted(challenger_paths):
        relative = path.relative_to(snapshot_root).as_posix()
        try:
            tree = _parse_tree(path, snapshot_root)
        except (OSError, UnicodeError, SyntaxError):
            continue
        if _has_main_guard(tree):
            guarded_main_outside_scripts.append(relative)
    for path in sorted(snapshot_root.rglob("*")):
        if path.is_file() and path.stat().st_mode & 0o111:
            executable_files.append(path.relative_to(snapshot_root).as_posix())
        if not path.is_file():
            continue
        relative = path.relative_to(snapshot_root).as_posix()
        if relative.startswith(("scripts/", "tests/")):
            continue
        guarded = False
        if path.suffix == ".py":
            try:
                guarded = _has_main_guard(_parse_tree(path, snapshot_root))
            except (OSError, UnicodeError, SyntaxError):
                guarded = False
        executable = bool(path.stat().st_mode & 0o111)
        if guarded or executable:
            repo_entry_challenger_paths.append(relative)
    return {
        "console_scripts": console_scripts,
        "console_script_map": console_script_map,
        "cli_top": list(cli["top"]),
        "cli_nodes": list(cli["nodes"]),
        "cli_leaves": list(cli["leaves"]),
        "cli_parameter_counts": {
            key: int(cli[key])
            for key in (
                "parameter_action_count",
                "optional_action_count",
                "positional_action_count",
                "boolean_action_count",
                "choice_action_count",
                "choice_value_count",
            )
        },
        "cli_effective_facet_counts": {
            key: int(cli[key])
            for key in (
                "effective_parameter_facet_count",
                "effective_optional_facet_count",
                "effective_positional_facet_count",
                "effective_boolean_facet_count",
                "effective_choice_facet_count",
                "effective_choice_value_count",
            )
        },
        "cli_leaf_argument_counts": dict(cli["leaf_argument_counts"]),
        "cli_leaf_argument_signatures": list(cli["leaf_argument_signatures"]),
        "cli_dispatch_direct": list(cli_dispatch["direct"]),
        "cli_dispatch_subcommand": list(cli_dispatch["subcommand"]),
        "cli_dispatch_special": list(cli_dispatch["special"]),
        "cli_dispatch_all": list(cli_dispatch["all"]),
        "cli_dispatch_missing": sorted(set(cli["top"]) - set(cli_dispatch["all"])),
        "cli_dispatch_stale": sorted(set(cli_dispatch["all"]) - set(cli["top"])),
        "daemon_cli_modes": daemon_cli_modes,
        "daemon_controlled_modes": daemon_controlled_modes,
        "mcp_schema_tools": list(mcp["schema_tools"]),
        "mcp_registered_tools": list(mcp["registered_tools"]),
        "mcp_categorized_tools": list(mcp["categorized_tools"]),
        "mcp_policy_tools": list(mcp["policy_tools"]),
        "mcp_tool_signatures": list(mcp["tool_signatures"]),
        "mcp_protocol_methods": list(mcp["protocol_methods"]),
        "facade_methods": facade,
        "daemon_intervals": list(daemon_services["intervals"]),
        "daemon_handlers": list(daemon_services["handlers"]),
        "daemon_aliases": list(daemon_services["aliases"]),
        "daemon_handler_gap": list(daemon_services["handler_gap"]),
        "chronos_steps": sorted(chronos),
        "chronos_trigger_counts": {
            trigger: sum(value == trigger for value in chronos.values())
            for trigger in sorted(set(chronos.values()))
        },
        "event_policy_persistent": list(event_policy["persistent"]),
        "event_policy_no_persist": list(event_policy["no_persist"]),
        "event_subscription_edges": list(event_subscriptions["edges"]),
        "event_subscription_topics": list(event_subscriptions["topics"]),
        "event_subscription_wildcard_edges": event_subscriptions["wildcard_edges"],
        "event_subscription_unresolved": list(event_subscriptions["unresolved"]),
        "dynamic_trigger_selectors": [
            "kia:<dynamic-module-id>",
            "scheduler:<dynamic-step-name>",
        ],
        "health_checks": health,
        "kia_modules": kia_modules,
        "script_modules": script_modules,
        "script_main": script_main,
        "script_helper": script_helper,
        "script_reachable_helpers": reachable_helpers,
        "script_unreachable_helpers": unreachable_helpers,
        "script_import_time_effect_candidates": import_time_effect_candidates,
        "guarded_main_outside_scripts": guarded_main_outside_scripts,
        "repo_entry_challenger_paths": sorted(set(repo_entry_challenger_paths)),
        "executable_files": executable_files,
        "requirement_ids": requirement_keys,
        "legacy_feature_ids": capability_keys,
        "function_matrix_cli_mapped": sorted(mapped_cli),
        "function_matrix_cli_unmapped": sorted(cli_leaves - mapped_cli),
        "function_matrix_mcp_mapped": sorted(set(mcp["schema_tools"]) & mapped_mcp),
        "function_matrix_mcp_unmapped": sorted(set(mcp["schema_tools"]) - mapped_mcp),
        "function_matrix_validation_ref_count": validation_ref_count,
        "function_matrix_validation_edges": sorted(
            f"{feature_id}:{path}" for feature_id, path in validation_edges
        ),
        "expected_coverage_edge_contracts": expected_coverage_edges,
        "function_matrix_validation_files": validation_files,
        "function_matrix_validation_missing_files": sorted(
            path for path in validation_files if not (snapshot_root / path).is_file()
        ),
        "function_matrix_features_without_test_file_ref": sorted(
            set(capability_keys) - set(feature_test_refs)
        ),
        "pytest_files": sorted(set(test_files)),
        "source_ids": sources,
        "source_parser_paths": source_parser_paths,
        "schema_owner_paths": schema_paths,
        "schema_reverse_paths": sorted(set(schema_reverse_paths)),
        "canonical_requirement_node_ids": sorted(canonical_requirement_nodes),
        "behavior_scenario_ids": sorted(
            str(item["id"]) for item in behavior_payload.get("scenarios", [])
        ),
        "ops_resilience_control_ids": sorted(
            str(item["id"]) for item in resilience_payload.get("controls", [])
        ),
        "runtime_interface_ids": sorted(
            str(item["interface_id"]) for item in runtime_payload.get("interfaces", [])
        ),
        "audit_artifact_ids": sorted(
            str(item["artifact_id"]) for item in audit_payload.get("artifacts", [])
        ),
        "release_gate_ids": sorted(
            str(item)
            for item in release_payload.get("required_gate_denominator", {}).get("gate_ids", [])
        ),
        "release_certificate_ids": sorted(
            str(item["certificate_id"]) for item in release_payload.get("certificates", [])
        ),
        "function_validation_discovery_keys": sorted(validation_keys),
        "mcp_category_counts": dict(mcp["category_counts"]),
        "mcp_policy_counts": dict(mcp["policy_counts"]),
        "mcp_tool_union": mcp_tool_union,
    }
