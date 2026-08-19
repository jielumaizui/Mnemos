"""Private implementation module for successor_d0_generation.cli_inventory."""

from __future__ import annotations

from collections import defaultdict

from dataclasses import dataclass

from dataclasses import field

from typing import Any

from typing import Mapping

from typing import Sequence

import ast

import re

from .model import (
    _record,
    _slug,
    _stable_digest,
    canonical_json,
)

from .snapshot import (
    _CatalogContext,
)

from .static_python import (
    _literal_assignment,
)


@dataclass
class _StaticCliParser:
    path: tuple[str, ...]
    arguments: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class _StaticCliSubparsers:
    parent: _StaticCliParser


class _StaticCliInterpreter:
    """Interpret the frozen declarative argparse builders without executing them."""

    _HELPERS = {
        "add_agent_mcp_grant_parser": (
            "core/cli/commands/agent.py",
            "add_agent_mcp_grant_parser",
        ),
        "add_model_call_ledger_migration_args": (
            "core/cli/commands/migrate.py",
            "add_model_call_ledger_migration_args",
        ),
    }

    def __init__(self, context: _CatalogContext) -> None:
        self.context = context
        self.parsers: dict[tuple[str, ...], _StaticCliParser] = {}
        self.children: dict[tuple[str, ...], set[tuple[str, ...]]] = defaultdict(set)

    @staticmethod
    def _expression(node: ast.AST) -> str:
        return f"expression:{ast.unparse(node)}"

    @classmethod
    def _contains_parser_value(cls, value: Any) -> bool:
        if isinstance(value, (_StaticCliParser, _StaticCliSubparsers)):
            return True
        if isinstance(value, (list, tuple, set, frozenset)):
            return any(cls._contains_parser_value(item) for item in value)
        if isinstance(value, Mapping):
            return any(cls._contains_parser_value(item) for item in value.values())
        return False

    def _argument_contract(
        self,
        parser: _StaticCliParser,
        positional: Sequence[Any],
        keywords: Mapping[str, Any],
        *,
        line: int,
    ) -> dict[str, Any]:
        if not positional or not isinstance(positional[0], str):
            raise ValueError(f"unresolved add_argument selector at line {line}")
        selectors = list(positional)
        if any(not isinstance(item, str) for item in selectors):
            raise ValueError(f"non-literal add_argument selector at line {line}")
        option_strings = [item for item in selectors if item.startswith("-")]
        if option_strings:
            long_options = [item for item in option_strings if item.startswith("--")]
            destination_source = long_options[0] if long_options else option_strings[0]
            inferred_dest = destination_source.lstrip("-").replace("-", "_")
        else:
            inferred_dest = selectors[0]
        destination = keywords.get("dest", inferred_dest)
        if not isinstance(destination, str) or not destination:
            raise ValueError(f"unresolved add_argument dest at line {line}")
        action_name = keywords.get("action")
        action_types = {
            None: "_StoreAction",
            "store": "_StoreAction",
            "store_true": "_StoreTrueAction",
            "store_false": "_StoreFalseAction",
            "append": "_AppendAction",
        }
        if action_name not in action_types:
            raise ValueError(f"unsupported argparse action {action_name!r} at line {line}")
        if "default" in keywords:
            default = keywords["default"]
        elif action_name == "store_true":
            default = False
        elif action_name == "store_false":
            default = True
        else:
            default = None
        return {
            "origin_command": " ".join(parser.path),
            "option_strings": option_strings,
            "dest": destination,
            "required": bool(keywords.get("required", False)),
            "nargs": keywords.get("nargs"),
            "choices": keywords.get("choices"),
            "default": default,
            "action": action_types[action_name],
            "type": keywords.get("type"),
            "metavar": keywords.get("metavar"),
            "help": keywords.get("help"),
            "mutex_groups": [],
        }

    def _evaluate(self, node: ast.AST | None, environment: dict[str, Any]) -> Any:
        if node is None:
            return None
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return environment.get(node.id, self._expression(node))
        if isinstance(node, ast.List):
            return [self._evaluate(item, environment) for item in node.elts]
        if isinstance(node, ast.Tuple):
            return tuple(self._evaluate(item, environment) for item in node.elts)
        if isinstance(node, ast.Set):
            return sorted(
                (self._evaluate(item, environment) for item in node.elts),
                key=canonical_json,
            )
        if isinstance(node, ast.Dict):
            return {
                self._evaluate(key, environment): self._evaluate(value, environment)
                for key, value in zip(node.keys, node.values)
            }
        if isinstance(node, ast.Attribute):
            return self._expression(node)
        if not isinstance(node, ast.Call):
            try:
                return ast.literal_eval(node)
            except (TypeError, ValueError):
                return self._expression(node)

        if isinstance(node.func, ast.Name):
            helper = self._HELPERS.get(node.func.id)
            positional = [self._evaluate(item, environment) for item in node.args]
            keywords = {
                item.arg: self._evaluate(item.value, environment)
                for item in node.keywords
                if item.arg is not None
            }
            if helper is not None:
                self._invoke_function(*helper, positional=positional, keywords=keywords)
                return None
            if node.func.id != "_build_docstring" and (
                self._contains_parser_value(positional) or self._contains_parser_value(keywords)
            ):
                raise ValueError(
                    f"unregistered argparse helper {node.func.id!r} at line {node.lineno}"
                )
            return self._expression(node)
        if not isinstance(node.func, ast.Attribute):
            return self._expression(node)

        method = node.func.attr
        if method == "ArgumentParser" and isinstance(node.func.value, ast.Name):
            if self.parsers:
                raise ValueError(f"multiple argparse roots at line {node.lineno}")
            parser = _StaticCliParser(())
            self.parsers[()] = parser
            self.children.setdefault((), set())
            return parser
        receiver = self._evaluate(node.func.value, environment)
        positional = [self._evaluate(item, environment) for item in node.args]
        if any(item.arg is None for item in node.keywords):
            if isinstance(receiver, (_StaticCliParser, _StaticCliSubparsers)):
                raise ValueError(f"expanded parser keyword at line {node.lineno}")
            return self._expression(node)
        keywords = {
            str(item.arg): self._evaluate(item.value, environment) for item in node.keywords
        }
        if method == "add_subparsers" and isinstance(receiver, _StaticCliParser):
            return _StaticCliSubparsers(receiver)
        if method == "add_parser" and isinstance(receiver, _StaticCliSubparsers):
            if not positional or not isinstance(positional[0], str) or not positional[0]:
                raise ValueError(f"unresolved add_parser name at line {node.lineno}")
            path = (*receiver.parent.path, positional[0])
            if path in self.parsers:
                raise ValueError(f"duplicate argparse path: {' '.join(path)}")
            parser = _StaticCliParser(path)
            self.parsers[path] = parser
            self.children.setdefault(path, set())
            self.children[receiver.parent.path].add(path)
            return parser
        if method == "add_argument" and isinstance(receiver, _StaticCliParser):
            receiver.arguments.append(
                self._argument_contract(
                    receiver,
                    positional,
                    keywords,
                    line=node.lineno,
                )
            )
            return None
        if isinstance(receiver, (_StaticCliParser, _StaticCliSubparsers)):
            raise ValueError(f"unsupported argparse method {method!r} at line {node.lineno}")
        if self._contains_parser_value(positional) or self._contains_parser_value(keywords):
            raise ValueError(f"unregistered argparse call at line {node.lineno}")
        return self._expression(node)

    def _assign(self, target: ast.AST, value: Any, environment: dict[str, Any]) -> None:
        if isinstance(target, ast.Name):
            environment[target.id] = value
            return
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and isinstance(
                environment.get(target.value.id),
                (_StaticCliParser, _StaticCliSubparsers),
            )
            and target.attr in {"description", "epilog", "required"}
        ):
            return
        if isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (list, tuple)):
            if len(target.elts) != len(value):
                raise ValueError(f"argparse loop destructuring changed at line {target.lineno}")
            for child, item in zip(target.elts, value):
                self._assign(child, item, environment)
            return
        raise ValueError(f"unsupported argparse assignment at line {getattr(target, 'lineno', 0)}")

    def _execute(self, statements: Sequence[ast.stmt], environment: dict[str, Any]) -> None:
        for statement in statements:
            if isinstance(statement, ast.Assign):
                value = self._evaluate(statement.value, environment)
                for target in statement.targets:
                    self._assign(target, value, environment)
            elif isinstance(statement, ast.AnnAssign):
                self._assign(
                    statement.target, self._evaluate(statement.value, environment), environment
                )
            elif isinstance(statement, ast.Expr):
                self._evaluate(statement.value, environment)
            elif isinstance(statement, ast.For):
                iterable = self._evaluate(statement.iter, environment)
                if not isinstance(iterable, (list, tuple)):
                    raise ValueError(f"unresolved argparse loop at line {statement.lineno}")
                for item in iterable:
                    self._assign(statement.target, item, environment)
                    self._execute(statement.body, environment)
                if statement.orelse:
                    self._execute(statement.orelse, environment)
            elif isinstance(
                statement,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.Import, ast.ImportFrom),
            ):
                continue
            elif isinstance(statement, ast.Return):
                return
            else:
                raise ValueError(
                    f"unsupported argparse builder statement {type(statement).__name__} "
                    f"at line {statement.lineno}"
                )

    def _invoke_function(
        self,
        relative: str,
        function_name: str,
        *,
        positional: Sequence[Any],
        keywords: Mapping[str, Any],
    ) -> None:
        tree = self.context.parse_python(relative)
        if tree is None:
            raise ValueError(f"static argparse helper source cannot be parsed: {relative}")
        function = next(
            (
                node
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == function_name
            ),
            None,
        )
        if function is None:
            raise ValueError(f"static argparse helper is missing: {relative}:{function_name}")
        parameters = [*function.args.posonlyargs, *function.args.args]
        if len(positional) > len(parameters):
            raise ValueError(f"too many arguments for static argparse helper {function_name}")
        environment = {argument.arg: value for argument, value in zip(parameters, positional)}
        for name, value in keywords.items():
            if name not in {argument.arg for argument in parameters}:
                raise ValueError(f"unknown static argparse helper argument {name}")
            environment[name] = value
        required = parameters[: len(parameters) - len(function.args.defaults)]
        if any(argument.arg not in environment for argument in required):
            raise ValueError(f"missing static argparse helper argument for {function_name}")
        self._execute(function.body, environment)

    def collect(self) -> tuple[dict[tuple[str, ...], _StaticCliParser], set[str]]:
        source = "mnemos_cli.py"
        tree = self.context.parse_python(source)
        if tree is None:
            raise ValueError("mnemos_cli.py cannot be parsed")
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
        self._execute(builder.body, {})
        if () not in self.parsers:
            raise ValueError("argparse root parser was not statically reconstructed")
        command_paths = {" ".join(path) for path in self.parsers if path}
        return self.parsers, command_paths


def _collect_static_cli_surfaces(
    context: _CatalogContext,
) -> tuple[list[dict[str, Any]], dict[str, list[str]], set[str], dict[str, Any]]:
    source = "mnemos_cli.py"
    interpreter = _StaticCliInterpreter(context)
    parsers, command_paths = interpreter.collect()
    leaves = sorted(
        (parser for path, parser in parsers.items() if path and not interpreter.children.get(path)),
        key=lambda parser: parser.path,
    )
    effective_by_command: dict[str, list[dict[str, Any]]] = {}
    for parser in leaves:
        effective: list[dict[str, Any]] = []
        for depth in range(0, len(parser.path) + 1):
            ancestor = parsers.get(parser.path[:depth])
            if ancestor is not None:
                effective.extend(dict(argument) for argument in ancestor.arguments)
        effective_by_command[" ".join(parser.path)] = effective

    evidence = [
        context.evidence(source, anchor="build_parser AST"),
        context.evidence(
            "core/cli/commands/agent.py",
            anchor="add_agent_mcp_grant_parser AST",
        ),
        context.evidence(
            "core/cli/commands/migrate.py",
            anchor="add_model_call_ledger_migration_args AST",
        ),
    ]
    records: list[dict[str, Any]] = []
    selector_map: dict[str, list[str]] = defaultdict(list)
    for command, arguments in sorted(effective_by_command.items()):
        record_id = f"surface:cli.{_slug(command)}"
        records.append(
            _record(
                "surfaces",
                record_id=record_id,
                discovery_key=f"argparse:leaf:{command}",
                record_status="DISCOVERED",
                evidence_refs=evidence,
                kind="cli",
                canonical_selector=f"cli:{command}",
                surface_family_id=f"surface-family:cli.{_slug(command.split()[0])}",
                facet_contract={
                    "kind": "command",
                    "arguments": arguments,
                    "enumeration_mode": "pure_ast_no_legacy_execution",
                },
                principal_policy_ref=None,
                input_contract_ref=None,
                output_contract_ref=None,
                lifecycle="active",
                decision_ref=None,
            )
        )
        selector_map[f"cli:{command}"].append(record_id)
        for argument in arguments:
            identity = {
                "command": command,
                "origin": argument.get("origin_command"),
                "dest": argument.get("dest"),
                "options": argument.get("option_strings", []),
            }
            argument_id = f"surface:cli-facet.{_stable_digest(identity)}"
            options = [str(item) for item in argument.get("option_strings", [])]
            canonical_option = options[0] if options else f"<{argument.get('dest', 'arg')}>"
            records.append(
                _record(
                    "surfaces",
                    record_id=argument_id,
                    discovery_key=f"argparse:facet:{command}:{canonical_json(identity)}",
                    record_status="ADJUDICATION_REQUIRED",
                    evidence_refs=evidence,
                    kind="cli_argument_facet",
                    canonical_selector=f"cli:{command} {canonical_option}",
                    surface_family_id=record_id,
                    facet_contract={"kind": "argument", **argument},
                    principal_policy_ref=None,
                    input_contract_ref=None,
                    output_contract_ref=None,
                    lifecycle="active",
                    decision_ref=None,
                )
            )
            for option in options:
                selector_map[f"cli:{command} {option}"].append(argument_id)

    defined_arguments = [argument for parser in parsers.values() for argument in parser.arguments]
    effective_arguments = [
        argument for arguments in effective_by_command.values() for argument in arguments
    ]
    top_count = sum(" " not in path for path in command_paths)
    metrics = {
        "enumeration_mode": "pure_ast_no_legacy_execution",
        "parameter_definition_basis": "source_defined_non_help_actions",
        "command_node_count": len(command_paths),
        "top_command_count": top_count,
        "leaf_count": len(leaves),
        "parameter_action_count": len(defined_arguments),
        "optional_action_count": sum(
            bool(argument["option_strings"]) for argument in defined_arguments
        ),
        "positional_action_count": sum(
            not argument["option_strings"] for argument in defined_arguments
        ),
        "boolean_action_count": sum(
            argument["action"] in {"_StoreTrueAction", "_StoreFalseAction"}
            for argument in defined_arguments
        ),
        "choice_action_count": sum(
            isinstance(argument["choices"], (list, tuple)) for argument in defined_arguments
        ),
        "choice_value_count": sum(
            len(argument["choices"])
            for argument in defined_arguments
            if isinstance(argument["choices"], (list, tuple))
        ),
        "effective_parameter_facet_count": len(effective_arguments),
        "effective_optional_facet_count": sum(
            bool(argument["option_strings"]) for argument in effective_arguments
        ),
        "effective_positional_facet_count": sum(
            not argument["option_strings"] for argument in effective_arguments
        ),
        "effective_boolean_facet_count": sum(
            argument["action"] in {"_StoreTrueAction", "_StoreFalseAction"}
            for argument in effective_arguments
        ),
        "effective_choice_facet_count": sum(
            isinstance(argument["choices"], (list, tuple)) for argument in effective_arguments
        ),
        "effective_choice_value_count": sum(
            len(argument["choices"])
            for argument in effective_arguments
            if isinstance(argument["choices"], (list, tuple))
        ),
    }
    expected = {
        "command_node_count": 226,
        "top_command_count": 59,
        "leaf_count": 179,
        "parameter_action_count": 408,
        "optional_action_count": 347,
        "positional_action_count": 61,
        "boolean_action_count": 178,
        "choice_action_count": 12,
        "choice_value_count": 62,
        "effective_parameter_facet_count": 426,
        "effective_optional_facet_count": 365,
        "effective_positional_facet_count": 61,
        "effective_boolean_facet_count": 178,
        "effective_choice_facet_count": 12,
        "effective_choice_value_count": 62,
    }
    actual_baseline = {key: metrics[key] for key in expected}
    if actual_baseline != expected:
        context.finding(
            "INVENTORY_BASELINE_DRIFT",
            "pure-AST argparse census differs from the D0 design baseline",
            source_ref=source,
            evidence={"expected": expected, "actual": actual_baseline},
        )
    return records, dict(selector_map), command_paths, metrics


def _collect_console_script_surfaces(context: _CatalogContext) -> list[dict[str, Any]]:
    source = "pyproject.toml"
    text = context.read_text(source)
    if text is None:
        return []
    scripts: dict[str, str] = {}
    in_project_scripts = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_project_scripts = line == "[project.scripts]"
            continue
        if not in_project_scripts or not line or line.startswith("#"):
            continue
        match = re.fullmatch(
            r"(?P<name>[A-Za-z0-9_.-]+)\s*=\s*[\"'](?P<target>[^\"']+)[\"']",
            line,
        )
        if match is None:
            context.finding(
                "SCHEMA_INVALID",
                f"unparsed project.scripts declaration: {line}",
                source_ref=source,
            )
            continue
        scripts[match.group("name")] = match.group("target")
    if scripts.get("mnemos") != "mnemos_cli:main":
        context.finding(
            "CONSOLE_SCRIPT_GAP",
            "project.scripts must expose exact mnemos -> mnemos_cli:main entry",
            source_ref=source,
            evidence={"project_scripts": scripts},
        )
    records: list[dict[str, Any]] = []
    for name, target in sorted(scripts.items()):
        expected = name != "mnemos" or target == "mnemos_cli:main"
        records.append(
            _record(
                "surfaces",
                record_id=f"surface:console-script.{_slug(name)}",
                discovery_key=f"console-script:{name}",
                record_status="DISCOVERED" if expected else "CONFLICT",
                evidence_refs=[context.evidence(source, anchor="project.scripts")],
                kind="console_script",
                canonical_selector=f"console-script:{name}",
                surface_family_id="surface-family:console-script",
                facet_contract={"name": name, "target": target},
                principal_policy_ref=None,
                input_contract_ref="argv",
                output_contract_ref="process-exit",
                lifecycle="active",
                decision_ref=None,
            )
        )
    return records


def _main_command_literals(test: ast.AST) -> set[str]:
    commands: set[str] = set()
    for node in ast.walk(test):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        if not isinstance(node.ops[0], ast.Eq) or len(node.comparators) != 1:
            continue
        pairs = ((node.left, node.comparators[0]), (node.comparators[0], node.left))
        for attribute, value in pairs:
            if (
                isinstance(attribute, ast.Attribute)
                and isinstance(attribute.value, ast.Name)
                and attribute.value.id == "args"
                and attribute.attr == "command"
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                commands.add(value.value)
    return commands


def _special_dispatch_specs(main_node: ast.FunctionDef) -> dict[str, list[dict[str, Any]]]:
    variants: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for statement in main_node.body:
        if not isinstance(statement, ast.If):
            continue
        commands = _main_command_literals(statement.test)
        if not commands:
            continue
        handlers: set[str] = set()
        for node in ast.walk(statement):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "_call_command" and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    handlers.add(first.value)
            elif isinstance(node.func, ast.Name) and node.func.id.startswith("_handle_"):
                handlers.add(node.func.id)
        for command in commands:
            variants[command].append(
                {
                    "handlers": sorted(handlers),
                    "predicate": ast.unparse(statement.test),
                }
            )
    return {
        command: [
            {
                "route_kind": "special_predicate_registry",
                "variants": sorted(rows, key=canonical_json),
            }
        ]
        for command, rows in variants.items()
    }


def _collect_cli_dispatch_surfaces(
    context: _CatalogContext,
    command_paths: set[str],
) -> list[dict[str, Any]]:
    source = "mnemos_cli.py"
    tree = context.parse_python(source)
    if tree is None:
        return []
    top_commands = {path for path in command_paths if path and " " not in path}
    command_routes = _literal_assignment(tree, "_COMMAND_ROUTES")
    subcommand_routes = _literal_assignment(tree, "_SUBCOMMAND_ROUTES")
    main_node = next(
        (node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"),
        None,
    )
    if not isinstance(command_routes, dict) or not isinstance(subcommand_routes, dict):
        context.finding(
            "CLI_DISPATCH_SCHEMA_INVALID",
            "CLI dispatch route registries are not literal dictionaries",
            source_ref=source,
        )
        return []

    routes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for command, raw in command_routes.items():
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            context.finding(
                "CLI_DISPATCH_SCHEMA_INVALID",
                f"invalid _COMMAND_ROUTES entry for {command}",
                source_ref=source,
            )
            continue
        routes[str(command)].append(
            {
                "route_kind": "direct_registry",
                "handler": str(raw[0]),
                "exit_mode": raw[1],
            }
        )
    for command, raw in subcommand_routes.items():
        command_name = str(command)
        if isinstance(raw, dict):
            nested: list[dict[str, Any]] = []
            for subcommand, target in sorted(raw.items(), key=lambda item: str(item[0])):
                if not isinstance(target, (list, tuple)) or len(target) != 2:
                    context.finding(
                        "CLI_DISPATCH_SCHEMA_INVALID",
                        f"invalid nested _SUBCOMMAND_ROUTES entry for {command_name}",
                        source_ref=source,
                    )
                    continue
                nested.append(
                    {
                        "subcommand": str(subcommand),
                        "handler": str(target[0]),
                        "exit_mode": target[1],
                    }
                )
            routes[command_name].append(
                {"route_kind": "nested_subcommand_registry", "targets": nested}
            )
        elif isinstance(raw, (list, tuple)) and len(raw) == 2:
            routes[command_name].append(
                {
                    "route_kind": "subcommand_registry",
                    "handler": str(raw[0]),
                    "exit_mode": raw[1],
                }
            )
        else:
            context.finding(
                "CLI_DISPATCH_SCHEMA_INVALID",
                f"invalid _SUBCOMMAND_ROUTES entry for {command_name}",
                source_ref=source,
            )
    if main_node is None:
        context.finding("CLI_DISPATCH_SCHEMA_INVALID", "CLI main() is missing", source_ref=source)
    else:
        for command, specs in _special_dispatch_specs(main_node).items():
            routes[command].extend(specs)

    route_commands = set(routes)
    missing = sorted(top_commands - route_commands)
    orphan = sorted(route_commands - top_commands)
    duplicates = sorted(command for command, specs in routes.items() if len(specs) != 1)
    if len(top_commands) != 59:
        context.finding(
            "INVENTORY_BASELINE_DRIFT",
            "top-level argparse command census differs from the D0 design baseline",
            source_ref=source,
            evidence={
                "expected": 59,
                "actual": len(top_commands),
                "commands": sorted(top_commands),
            },
        )
    if missing or orphan or duplicates:
        context.finding(
            "CLI_DISPATCH_GAP",
            "top-level argparse commands and dispatch routes do not form a one-to-one set",
            source_ref=source,
            evidence={
                "parser_top_command_count": len(top_commands),
                "dispatch_top_command_count": len(route_commands),
                "missing_dispatch": missing,
                "orphan_dispatch": orphan,
                "duplicate_dispatch": duplicates,
            },
        )

    evidence = [context.evidence(source, anchor="main dispatch registries")]
    records: list[dict[str, Any]] = []
    for command in sorted(top_commands | route_commands):
        parser_declared = command in top_commands
        dispatch_specs = routes.get(command, [])
        complete = parser_declared and len(dispatch_specs) == 1
        records.append(
            _record(
                "surfaces",
                record_id=f"surface:cli-dispatch.{_slug(command)}",
                discovery_key=f"cli-dispatch:{command}",
                record_status="DISCOVERED" if complete else "ADJUDICATION_REQUIRED",
                evidence_refs=evidence,
                kind="cli_dispatch_route",
                canonical_selector=f"cli-dispatch:{command}",
                surface_family_id="surface-family:cli.dispatch",
                facet_contract={
                    "command": command,
                    "parser_declared": parser_declared,
                    "dispatch_specs": dispatch_specs,
                    "parser_top_command_count": len(top_commands),
                    "dispatch_top_command_count": len(route_commands),
                },
                principal_policy_ref=None,
                input_contract_ref=f"argparse-command:{command}",
                output_contract_ref="dispatch-return-or-process-exit",
                lifecycle="active",
                decision_ref=None,
            )
        )
    return records


def _collect_daemon_modes(
    context: _CatalogContext,
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    relative = "daemon/entrypoint_support.py"
    tree = context.parse_python(relative)
    if tree is None:
        return [], {}
    choices: list[str] = []
    controlled_modes: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "add_argument" and node.args:
                try:
                    first = ast.literal_eval(node.args[0])
                except (TypeError, ValueError):
                    first = None
                if first == "command":
                    for keyword in node.keywords:
                        if keyword.arg == "choices":
                            try:
                                choices = [str(item) for item in ast.literal_eval(keyword.value)]
                            except (TypeError, ValueError):
                                choices = []
        if isinstance(node, ast.Set):
            try:
                values = {str(item) for item in ast.literal_eval(node)}
            except (TypeError, ValueError):
                continue
            if {"start", "stop", "status", "run"} <= values:
                controlled_modes = values
    if not choices:
        context.finding(
            "ENUMERATOR_FAILED", "daemon command choices were not found", source_ref=relative
        )
    evidence = [context.evidence(relative, anchor="main")]
    records: list[dict[str, Any]] = []
    selector_map: dict[str, list[str]] = defaultdict(list)
    for mode in choices:
        record_id = f"surface:daemon-mode.{_slug(mode)}"
        records.append(
            _record(
                "surfaces",
                record_id=record_id,
                discovery_key=f"daemon-cli:mode:{mode}",
                record_status="DISCOVERED",
                evidence_refs=evidence,
                kind="daemon_mode",
                canonical_selector=f"daemon:{mode}",
                surface_family_id="surface-family:daemon.command",
                facet_contract={
                    "mode": mode,
                    "controlled_raw_sync_supported": mode in controlled_modes,
                },
                principal_policy_ref=None,
                input_contract_ref=None,
                output_contract_ref=None,
                lifecycle="active",
                decision_ref=None,
            )
        )
        selector_map[f"daemon:{mode}"].append(record_id)
        if mode in controlled_modes:
            facet_id = f"surface:daemon-mode.{_slug(mode)}.controlled-raw-sync-only"
            records.append(
                _record(
                    "surfaces",
                    record_id=facet_id,
                    discovery_key=f"daemon-cli:mode:{mode}:controlled-raw-sync-only",
                    record_status="ADJUDICATION_REQUIRED",
                    evidence_refs=evidence,
                    kind="daemon_mode_facet",
                    canonical_selector=f"daemon:{mode} --controlled-raw-sync-only",
                    surface_family_id="surface-family:daemon.command",
                    facet_contract={"mode": mode, "controlled_raw_sync_only": True},
                    principal_policy_ref=None,
                    input_contract_ref=None,
                    output_contract_ref=None,
                    lifecycle="active",
                    decision_ref=None,
                )
            )
            selector_map[f"daemon:{mode} --controlled-raw-sync-only"].append(facet_id)
    return records, dict(selector_map)
