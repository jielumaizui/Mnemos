"""Private implementation module for successor_d0_generation.runtime_inventory."""

from __future__ import annotations

from collections import defaultdict

from typing import Any

from typing import Mapping

from typing import Sequence

import ast

from .model import (
    _record,
    _slug,
    _stable_digest,
    canonical_json,
    canonical_json_bytes,
    sha256_bytes,
)

from .snapshot import (
    _CatalogContext,
)

from .static_python import (
    _literal_assignment,
    _safe_signature,
)


def _function_return_dict_keys(tree: ast.AST, function_name: str) -> set[str]:
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != function_name:
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Return) or not isinstance(child.value, ast.Dict):
                continue
            keys: set[str] = set()
            for key in child.value.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.add(key.value)
            if keys:
                return keys
    return set()


def _collect_mcp_surfaces(
    context: _CatalogContext,
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    agora_path = "integrations/agora.py"
    schema_path = "integrations/agora_tools/schema.py"
    policy_path = "core/access_policy.py"
    agora = context.parse_python(agora_path)
    schema_tree = context.parse_python(schema_path)
    policy_tree = context.parse_python(policy_path)
    if agora is None or schema_tree is None or policy_tree is None:
        return [], {}

    registered = _function_return_dict_keys(agora, "_register_tools")
    categories = _literal_assignment(agora, "_TOOL_CATEGORIES", class_name="MCPServer")
    policies = _literal_assignment(policy_tree, "MCP_TOOL_POLICIES")
    schemas = _literal_assignment(schema_tree, "tools", function_name="list_tools")
    if not isinstance(categories, dict):
        categories = {}
        context.finding(
            "ENUMERATOR_FAILED", "MCP category registry is not static", source_ref=agora_path
        )
    if not isinstance(policies, dict):
        policies = {}
        context.finding(
            "ENUMERATOR_FAILED", "MCP policy registry is not static", source_ref=policy_path
        )
    if not isinstance(schemas, list):
        schemas = []
        context.finding(
            "ENUMERATOR_FAILED", "MCP schema registry is not static", source_ref=schema_path
        )
    schema_by_name = {
        str(item.get("name")): item
        for item in schemas
        if isinstance(item, Mapping) and str(item.get("name") or "")
    }
    category_by_name: dict[str, list[str]] = defaultdict(list)
    for category, names in categories.items():
        if isinstance(names, (list, tuple)):
            for name in names:
                category_by_name[str(name)].append(str(category))
    tool_sets = {
        "registered": registered,
        "schema": set(schema_by_name),
        "category": set(category_by_name),
        "policy": {str(name) for name in policies},
    }
    if len({frozenset(values) for values in tool_sets.values()}) != 1:
        context.finding(
            "REGISTRY_SET_MISMATCH",
            "MCP registration, schema, category, and policy sets differ",
            source_ref=agora_path,
            evidence={name: sorted(values) for name, values in tool_sets.items()},
        )

    evidence = [
        context.evidence(agora_path, anchor="MCPServer._register_tools"),
        context.evidence(schema_path, anchor="list_tools"),
        context.evidence(policy_path, anchor="MCP_TOOL_POLICIES"),
    ]
    records: list[dict[str, Any]] = []
    selector_map: dict[str, list[str]] = defaultdict(list)
    for name in sorted(set().union(*tool_sets.values())):
        categories_for_tool = category_by_name.get(name, [])
        complete = (
            all(name in values for values in tool_sets.values()) and len(categories_for_tool) == 1
        )
        if not complete:
            context.finding(
                "MCP_TOOL_INCOMPLETE",
                f"MCP tool is not closed across all registries: {name}",
                source_ref=agora_path,
                record_id=f"surface:mcp.{_slug(name)}",
            )
        record_id = f"surface:mcp.{_slug(name)}"
        schema = schema_by_name.get(name)
        records.append(
            _record(
                "surfaces",
                record_id=record_id,
                discovery_key=f"mcp:tool:{name}",
                record_status="DISCOVERED" if complete else "INVALID",
                evidence_refs=evidence,
                kind="mcp_tool",
                canonical_selector=f"mcp:{name}",
                surface_family_id="surface-family:mcp.tool",
                facet_contract={
                    "tool_name": name,
                    "input_schema": (
                        schema.get("inputSchema") if isinstance(schema, Mapping) else None
                    ),
                    "input_schema_sha256": (
                        sha256_bytes(canonical_json_bytes(schema.get("inputSchema")))
                        if isinstance(schema, Mapping)
                        else None
                    ),
                    "category": categories_for_tool[0] if len(categories_for_tool) == 1 else None,
                    "policy": policies.get(name),
                    "registered": name in registered,
                },
                principal_policy_ref=(f"MCP_TOOL_POLICIES.{name}" if name in policies else None),
                input_contract_ref=(f"integrations.agora_tools.schema:{name}" if schema else None),
                output_contract_ref="mcp.CallToolResult",
                lifecycle="active",
                decision_ref=None,
            )
        )
        selector_map[f"mcp:{name}"].append(record_id)

    protocol_methods: set[str] = set()
    for node in ast.walk(agora):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        if not isinstance(node.ops[0], ast.Eq) or len(node.comparators) != 1:
            continue
        values = (node.left, node.comparators[0])
        for value in values:
            if isinstance(value, ast.Constant) and value.value in {
                "initialize",
                "notifications/initialized",
                "tools/list",
                "tools/call",
            }:
                protocol_methods.add(str(value.value))
    expected_protocol = {
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
    }
    if protocol_methods != expected_protocol:
        context.finding(
            "REGISTRY_SET_MISMATCH",
            "MCP protocol method inventory differs from the required four methods",
            source_ref=agora_path,
            evidence={"actual": sorted(protocol_methods), "expected": sorted(expected_protocol)},
        )
    for method in sorted(protocol_methods | expected_protocol):
        record_id = f"surface:mcp-protocol.{_slug(method)}"
        records.append(
            _record(
                "surfaces",
                record_id=record_id,
                discovery_key=f"mcp:protocol:{method}",
                record_status="DISCOVERED" if method in protocol_methods else "MISSING",
                evidence_refs=[context.evidence(agora_path, anchor="MCPServer.handle_request")],
                kind="mcp_protocol",
                canonical_selector=f"mcp-protocol:{method}",
                surface_family_id="surface-family:mcp.protocol",
                facet_contract={
                    "method": method,
                    "notification": method == "notifications/initialized",
                },
                principal_policy_ref=None,
                input_contract_ref="json-rpc-2.0",
                output_contract_ref="json-rpc-2.0",
                lifecycle="active",
                decision_ref=None,
            )
        )
    return records, dict(selector_map)


def _collect_facade_surfaces(context: _CatalogContext) -> list[dict[str, Any]]:
    relative = "core/application/contracts.py"
    tree = context.parse_python(relative)
    if tree is None:
        return []
    facade = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "MnemosServiceFacade"
        ),
        None,
    )
    if facade is None:
        context.finding(
            "ENUMERATOR_FAILED", "MnemosServiceFacade Protocol is missing", source_ref=relative
        )
        return []
    evidence = [context.evidence(relative, anchor="MnemosServiceFacade")]
    records: list[dict[str, Any]] = []
    for method in facade.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if method.name.startswith("_"):
            continue
        record_id = f"surface:facade.{_slug(method.name)}"
        records.append(
            _record(
                "surfaces",
                record_id=record_id,
                discovery_key=f"facade:MnemosServiceFacade.{method.name}",
                record_status="DISCOVERED",
                evidence_refs=evidence,
                kind="application_facade",
                canonical_selector=f"facade:{method.name}",
                surface_family_id="surface-family:application.facade",
                facet_contract={
                    "method": method.name,
                    "async": isinstance(method, ast.AsyncFunctionDef),
                    "arguments": _safe_signature(method),
                    "returns": ast.unparse(method.returns) if method.returns is not None else None,
                    "doc": ast.get_docstring(method),
                },
                principal_policy_ref=None,
                input_contract_ref=f"MnemosServiceFacade.{method.name}",
                output_contract_ref=f"MnemosServiceFacade.{method.name}",
                lifecycle="active",
                decision_ref=None,
            )
        )
    return records


def _collect_source_surfaces(
    context: _CatalogContext,
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    relative = "core/agent_kit/agent_source_support_manifest.json"
    payload = context.load_json(relative)
    if not isinstance(payload, Mapping) or not isinstance(payload.get("sources"), list):
        return [], {}
    evidence = [context.evidence(relative, anchor="sources")]
    records: list[dict[str, Any]] = []
    selector_map: dict[str, list[str]] = defaultdict(list)
    for source in payload["sources"]:
        if not isinstance(source, Mapping) or not str(source.get("name") or ""):
            context.finding(
                "SCHEMA_INVALID", "source manifest entry is invalid", source_ref=relative
            )
            continue
        name = str(source["name"])
        parser_value = source.get("parser")
        parser: dict[str, Any] = dict(parser_value) if isinstance(parser_value, Mapping) else {}
        parser_path = str(parser.get("module") or "").replace(".", "/") + ".py"
        parser_exists = (
            bool(parser.get("module"))
            and context.read_bytes(parser_path, required=False) is not None
        )
        if not parser_exists:
            context.finding(
                "STALE_SOURCE",
                f"source parser is missing for {name}: {parser_path}",
                source_ref=relative,
            )
        record_id = f"surface:source.{_slug(name)}"
        records.append(
            _record(
                "surfaces",
                record_id=record_id,
                discovery_key=f"source-manifest:{name}",
                record_status="DISCOVERED" if parser_exists else "STALE_SOURCE",
                evidence_refs=[
                    *evidence,
                    context.evidence(parser_path, anchor="parser"),
                ],
                kind="agent_source",
                selector=name,
                canonical_selector=f"source:{name}",
                surface_family_id="surface-family:source.ingestion",
                facet_contract={
                    "name": name,
                    "role": source.get("role"),
                    "aliases": source.get("aliases", []),
                    "parser": dict(parser),
                    "native": source.get("native"),
                    "capability": source.get("capability"),
                    "authorization": source.get("authorization"),
                    "continuous": source.get("continuous"),
                    "backfill": source.get("backfill"),
                    "runtime_probe": source.get("runtime_probe"),
                    "raw_contract": source.get("raw_contract"),
                    "retention": source.get("retention"),
                    "active_entrypoint": source.get("active_entrypoint"),
                    "active_setup": source.get("active_setup"),
                },
                principal_policy_ref=f"source:{name}:authorization",
                input_contract_ref=f"source:{name}:native",
                output_contract_ref=f"source:{name}:raw_contract",
                lifecycle=(
                    source.get("retirement", {}).get("state", "unknown")
                    if isinstance(source.get("retirement"), Mapping)
                    else "unknown"
                ),
                decision_ref=None,
            )
        )
        selector_map[f"source:{name}"].append(record_id)
        selector_map[f"agent_kit:{name}"].append(record_id)
    return records, dict(selector_map)


def _literal_return_dict(tree: ast.AST, function_name: str) -> dict[str, Any] | None:
    for node in ast.walk(tree):
        if (
            not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            or node.name != function_name
        ):
            continue
        for child in node.body:
            if isinstance(child, ast.Return) and isinstance(child.value, ast.Dict):
                try:
                    value = ast.literal_eval(child.value)
                except (TypeError, ValueError):
                    keys: list[str] = []
                    for key in child.value.keys:
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            keys.append(key.value)
                    return {key: None for key in keys}
                return dict(value)
    return None


def _collect_daemon_services(
    context: _CatalogContext,
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    interval_path = "daemon/intervals.py"
    registry_path = "daemon/service_registry.py"
    intervals_tree = context.parse_python(interval_path)
    registry_tree = context.parse_python(registry_path)
    if intervals_tree is None or registry_tree is None:
        return [], {}
    intervals = _literal_return_dict(intervals_tree, "build_default_intervals") or {}
    direct = _literal_assignment(registry_tree, "DIRECT_SERVICE_TARGETS")
    configured = _literal_assignment(registry_tree, "CFG_SERVICE_TARGETS")
    if not isinstance(direct, dict) or not isinstance(configured, dict):
        context.finding(
            "ENUMERATOR_FAILED",
            "daemon service registries are not static",
            source_ref=registry_path,
        )
        return [], {}
    targets = {**direct, **configured}
    interval_names = set(intervals)
    target_names = set(targets)
    legacy_aliases = target_names - interval_names
    missing_handlers = interval_names - target_names
    if missing_handlers:
        context.finding(
            "REGISTRY_SET_MISMATCH",
            "daemon intervals have no registered handler",
            source_ref=registry_path,
            evidence={"missing_handlers": sorted(missing_handlers)},
        )
    evidence = [
        context.evidence(interval_path, anchor="build_default_intervals"),
        context.evidence(registry_path, anchor="DIRECT_SERVICE_TARGETS"),
    ]
    records: list[dict[str, Any]] = []
    selector_map: dict[str, list[str]] = defaultdict(list)
    for name in sorted(interval_names | target_names):
        alias = name in legacy_aliases
        record_id = f"surface:daemon-service.{_slug(name)}"
        status_value = "ALIAS" if alias else ("DISCOVERED" if name in targets else "INVALID")
        records.append(
            _record(
                "surfaces",
                record_id=record_id,
                discovery_key=f"daemon-service:{name}",
                record_status=status_value,
                evidence_refs=evidence,
                kind="daemon_service_alias" if alias else "daemon_service",
                canonical_selector=f"daemon:{name}",
                surface_family_id="surface-family:daemon.service",
                facet_contract={
                    "service_name": name,
                    "default_interval_seconds": intervals.get(name),
                    "handler": targets.get(name),
                    "configured_handler": name in configured,
                    "legacy_alias": alias,
                },
                principal_policy_ref=None,
                input_contract_ref=None,
                output_contract_ref=None,
                lifecycle="legacy_alias" if alias else "active",
                decision_ref=None,
            )
        )
        selector_map[f"daemon:{name}"].append(record_id)
    return records, dict(selector_map)


def _call_name(node: ast.AST | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _collect_chronos_surfaces(
    context: _CatalogContext,
) -> tuple[list[dict[str, Any]], dict[str, list[str]], list[tuple[str, str]]]:
    paths = (
        "core/kia/chronos_scheduler_support.py",
        "core/kia/chronos_builtin_steps.py",
    )
    records: list[dict[str, Any]] = []
    selector_map: dict[str, list[str]] = defaultdict(list)
    seen: set[str] = set()
    for relative in paths:
        tree = context.parse_python(relative)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node.func) != "ScheduledStep":
                continue
            keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
            name_node = keywords.get("name")
            if not isinstance(name_node, ast.Constant) or not isinstance(name_node.value, str):
                context.finding(
                    "DYNAMIC_TRIGGER_UNCLASSIFIED",
                    f"Chronos ScheduledStep has a dynamic name in {relative}",
                    source_ref=relative,
                )
                continue
            name = name_node.value
            trigger_node = keywords.get("trigger")
            trigger_kind = (
                _call_name(trigger_node.func) if isinstance(trigger_node, ast.Call) else "UNKNOWN"
            )
            if name in seen:
                context.finding(
                    "DUPLICATE_ID",
                    f"duplicate Chronos step name: {name}",
                    source_ref=relative,
                )
                continue
            seen.add(name)
            record_id = f"surface:chronos.{_slug(name)}"
            records.append(
                _record(
                    "surfaces",
                    record_id=record_id,
                    discovery_key=f"chronos:step:{name}",
                    record_status="DISCOVERED",
                    evidence_refs=[context.evidence(relative, anchor=name)],
                    kind="chronos_step",
                    canonical_selector=f"scheduler:{name}",
                    surface_family_id="surface-family:chronos.step",
                    facet_contract={
                        "step_name": name,
                        "trigger_kind": trigger_kind,
                        "enabled_expression": (
                            ast.unparse(keywords["enabled"]) if "enabled" in keywords else "True"
                        ),
                        "timeout_expression": (
                            ast.unparse(keywords["timeout"]) if "timeout" in keywords else None
                        ),
                    },
                    principal_policy_ref=None,
                    input_contract_ref=None,
                    output_contract_ref=None,
                    lifecycle="active",
                    decision_ref=None,
                )
            )
            selector_map[f"scheduler:{name}"].append(record_id)
    chronos_path = "core/kia/chronos.py"
    chronos_tree = context.parse_python(chronos_path)
    routes: list[tuple[str, str]] = []
    if chronos_tree is not None:
        value = _literal_assignment(
            chronos_tree,
            "EVENT_TRIGGER_ROUTES",
            class_name="KnowledgeScheduler",
        )
        if isinstance(value, (list, tuple)):
            routes = [(str(item[0]), str(item[1])) for item in value if len(item) == 2]
    for step_name, event_type in routes:
        if step_name in seen:
            context.finding(
                "DUPLICATE_ID",
                f"duplicate Chronos step name across schedule and event routes: {step_name}",
                source_ref=chronos_path,
            )
            continue
        seen.add(step_name)
        record_id = f"surface:chronos.{_slug(step_name)}"
        records.append(
            _record(
                "surfaces",
                record_id=record_id,
                discovery_key=f"chronos:event-step:{step_name}:{event_type}",
                record_status="DISCOVERED",
                evidence_refs=[context.evidence(chronos_path, anchor="EVENT_TRIGGER_ROUTES")],
                kind="chronos_step",
                canonical_selector=f"scheduler:{step_name}",
                surface_family_id="surface-family:chronos.step",
                facet_contract={
                    "step_name": step_name,
                    "trigger_kind": "EventTrigger",
                    "event_type": event_type,
                    "enabled_expression": "True",
                    "timeout_expression": None,
                },
                principal_policy_ref=None,
                input_contract_ref=f"event:{event_type}",
                output_contract_ref=None,
                lifecycle="active",
                decision_ref=None,
            )
        )
        selector_map[f"scheduler:{step_name}"].append(record_id)
    if len(seen) != 26:
        context.finding(
            "INVENTORY_BASELINE_DRIFT",
            "Chronos scheduled and event-trigger step census differs from the D0 baseline",
            source_ref=chronos_path,
            evidence={"expected": 26, "actual": len(seen), "step_names": sorted(seen)},
        )
    dynamic_id = "surface:chronos.dynamic-task-registration"
    records.append(
        _record(
            "surfaces",
            record_id=dynamic_id,
            discovery_key="chronos:dynamic-task-registration",
            record_status="ADJUDICATION_REQUIRED",
            evidence_refs=[
                context.evidence("core/kia/chronos_scheduler_support.py", anchor="register")
            ],
            kind="dynamic_trigger_registry",
            canonical_selector="scheduler:<dynamic-step-name>",
            surface_family_id="surface-family:chronos.step",
            facet_contract={"registry": "SchedulerSupportMixin.register", "closed_name_set": False},
            principal_policy_ref=None,
            input_contract_ref=None,
            output_contract_ref=None,
            lifecycle="active",
            decision_ref=None,
        )
    )
    return records, dict(selector_map), routes


def _module_string_constants(tree: ast.AST) -> dict[str, str]:
    values: dict[str, str] = {}
    for node in getattr(tree, "body", []):
        targets: Sequence[ast.AST] = ()
        value: ast.AST | None = None
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = (node.target,)
            value = node.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                values[target.id] = value.value
    return values


def _enclosing_for_values(tree: ast.AST) -> dict[str, list[str]]:
    values: dict[str, list[str]] = defaultdict(list)
    for node in ast.walk(tree):
        if not isinstance(node, ast.For) or not isinstance(node.target, ast.Name):
            continue
        try:
            iterable = ast.literal_eval(node.iter)
        except (TypeError, ValueError):
            continue
        if isinstance(iterable, (list, tuple, set, frozenset)):
            values[node.target.id].extend(str(item) for item in iterable if isinstance(item, str))
    return dict(values)


def _function_argument_defaults(tree: ast.AST) -> dict[str, str]:
    defaults: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        positional = [*node.args.posonlyargs, *node.args.args]
        offset = len(positional) - len(node.args.defaults)
        for argument, default in zip(positional[offset:], node.args.defaults):
            if isinstance(default, ast.Constant) and isinstance(default.value, str):
                defaults[argument.arg] = default.value
    return defaults


def _subscribe_call_records(
    context: _CatalogContext,
    relative: str,
) -> list[tuple[str, str, str, int]]:
    tree = context.parse_python(relative)
    if tree is None:
        return []
    constants = _module_string_constants(tree)
    loop_values = _enclosing_for_values(tree)
    argument_defaults = _function_argument_defaults(tree)
    records: list[tuple[str, str, str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "subscribe" or not node.args:
            continue
        event_node = node.args[0]
        events: list[str] = []
        if isinstance(event_node, ast.Constant) and isinstance(event_node.value, str):
            events = [event_node.value]
        elif isinstance(event_node, ast.Name):
            if event_node.id in constants:
                events = [constants[event_node.id]]
            elif event_node.id in loop_values:
                events = loop_values[event_node.id]
            elif event_node.id in argument_defaults:
                events = [argument_defaults[event_node.id]]
        if not events:
            continue
        handler = ast.unparse(node.args[1]) if len(node.args) > 1 else "UNKNOWN"
        consumer_id = ""
        for keyword in node.keywords:
            if keyword.arg == "consumer_id":
                consumer_id = ast.unparse(keyword.value)
        for event in events:
            records.append((event, handler, consumer_id, getattr(node, "lineno", 0)))
    return records


def _collect_event_surfaces(
    context: _CatalogContext,
    chronos_routes: Sequence[tuple[str, str]],
) -> list[dict[str, Any]]:
    bus_path = "core/mnemos_bus.py"
    tree = context.parse_python(bus_path)
    if tree is None:
        return []
    event_types = _literal_assignment(tree, "EVENT_TYPES")
    persistent = _literal_assignment(tree, "_PERSISTENT_EVENT_TYPES", class_name="EventBus")
    no_persist = _literal_assignment(tree, "_NO_PERSIST_EVENT_TYPES", class_name="EventBus")
    event_types_set = {str(item) for item in event_types or ()}
    persistent_set = {str(item) for item in persistent or ()}
    no_persist_set = {str(item) for item in no_persist or ()}
    if persistent_set & no_persist_set or event_types_set != persistent_set | no_persist_set:
        context.finding(
            "REGISTRY_SET_MISMATCH",
            "EventBus policy sets do not exactly partition EVENT_TYPES",
            source_ref=bus_path,
            evidence={
                "event_types": sorted(event_types_set),
                "persistent": sorted(persistent_set),
                "no_persist": sorted(no_persist_set),
            },
        )
    records: list[dict[str, Any]] = []
    for event_type in sorted(event_types_set | persistent_set | no_persist_set):
        policy = (
            "persistent"
            if event_type in persistent_set
            else ("no_persist" if event_type in no_persist_set else "unknown")
        )
        records.append(
            _record(
                "surfaces",
                record_id=f"surface:event-policy.{_slug(event_type)}",
                discovery_key=f"eventbus:policy:{event_type}",
                record_status="DISCOVERED" if policy != "unknown" else "INVALID",
                evidence_refs=[context.evidence(bus_path, anchor="EventBus event policy")],
                kind="event_policy",
                canonical_selector=f"event:{event_type}",
                surface_family_id="surface-family:eventbus.policy",
                facet_contract={"event_type": event_type, "persistence_policy": policy},
                principal_policy_ref=None,
                input_contract_ref="Event",
                output_contract_ref="HandlerOutcome",
                lifecycle="active",
                decision_ref=None,
            )
        )

    subscription_paths = sorted(
        {
            path.relative_to(context.root).as_posix()
            for base in ("core", "daemon", "integrations", "scripts")
            for path in (context.root / base).rglob("*.py")
        }
        | {"mnemos_daemon.py"}
    )
    subscriptions: list[tuple[str, str, str, str, int]] = []
    for relative in subscription_paths:
        subscriptions.extend(
            (event, handler, consumer, relative, line)
            for event, handler, consumer, line in _subscribe_call_records(context, relative)
        )
    for step_name, event_type in chronos_routes:
        subscriptions.append(
            (
                event_type,
                f"trigger_event:{step_name}",
                f"chronos:{step_name}",
                "core/kia/chronos_builtin_steps.py",
                0,
            )
        )

    concrete = [item for item in subscriptions if item[0] != "*"]
    wildcard = [item for item in subscriptions if item[0] == "*"]
    unregistered_topics = sorted(
        {event for event, _handler, _consumer, _relative, _line in concrete} - event_types_set
    )
    if unregistered_topics:
        context.finding(
            "EVENT_POLICY_GAP",
            "concrete EventBus subscriptions use topics absent from EVENT_TYPES policy registry",
            source_ref=bus_path,
            evidence={
                "unregistered_topic_count": len(unregistered_topics),
                "unregistered_topics": unregistered_topics,
            },
        )
    if len(unregistered_topics) != 6:
        context.finding(
            "INVENTORY_BASELINE_DRIFT",
            "EventBus unregistered-topic census differs from the D0 design baseline",
            source_ref=bus_path,
            evidence={
                "expected_unregistered_topic_count": 6,
                "actual_unregistered_topic_count": len(unregistered_topics),
                "unregistered_topics": unregistered_topics,
            },
        )
    if len(concrete) != 33 or len(wildcard) != 1:
        context.finding(
            "INVENTORY_BASELINE_DRIFT",
            "EventBus subscription census differs from the D0 design baseline",
            source_ref=bus_path,
            evidence={"concrete_count": len(concrete), "wildcard_count": len(wildcard)},
        )
    seen_keys: dict[tuple[str, str, str, str], int] = defaultdict(int)
    for event, handler, consumer, relative, line in sorted(subscriptions):
        base_key = (event, handler, consumer, relative)
        seen_keys[base_key] += 1
        ordinal = seen_keys[base_key]
        identity = {
            "event_type": event,
            "handler": handler,
            "consumer_id_expression": consumer,
            "path": relative,
            "ordinal": ordinal,
        }
        digest = _stable_digest(identity)
        policy_gap = event != "*" and event not in event_types_set
        records.append(
            _record(
                "surfaces",
                record_id=f"surface:event-subscription.{digest}",
                discovery_key=f"eventbus:subscription:{canonical_json(identity)}",
                record_status=(
                    "ADJUDICATION_REQUIRED" if event == "*" or policy_gap else "DISCOVERED"
                ),
                evidence_refs=[context.evidence(relative, anchor=f"line:{line}" if line else "")],
                kind="event_subscription_wildcard" if event == "*" else "event_subscription",
                canonical_selector=f"event-subscription:{event}:{consumer or handler}",
                surface_family_id="surface-family:eventbus.subscription",
                facet_contract={
                    **identity,
                    "policy_registry_status": (
                        "wildcard" if event == "*" else ("missing" if policy_gap else "registered")
                    ),
                },
                principal_policy_ref=None,
                input_contract_ref=f"event:{event}",
                output_contract_ref="HandlerOutcome",
                lifecycle="active",
                decision_ref=None,
            )
        )
    return records


def _collect_health_surfaces(context: _CatalogContext) -> list[dict[str, Any]]:
    relative = "core/ops/health_contract.py"
    tree = context.parse_python(relative)
    if tree is None:
        return []
    check_ids = _literal_assignment(tree, "CANONICAL_HEALTH_CHECK_IDS")
    if not isinstance(check_ids, (list, tuple)):
        context.finding(
            "ENUMERATOR_FAILED", "canonical health check IDs are not static", source_ref=relative
        )
        return []
    evidence = [context.evidence(relative, anchor="CANONICAL_HEALTH_CHECK_IDS")]
    return [
        _record(
            "surfaces",
            record_id=f"surface:health.{_slug(check_id)}",
            discovery_key=f"health:check:{check_id}",
            record_status="DISCOVERED",
            evidence_refs=evidence,
            kind="health_check",
            canonical_selector=f"health:{check_id}",
            surface_family_id="surface-family:health.check",
            facet_contract={"check_id": str(check_id)},
            principal_policy_ref=None,
            input_contract_ref="health.snapshot",
            output_contract_ref="health.check.result",
            lifecycle="active",
            decision_ref=None,
        )
        for check_id in check_ids
    ]


def _collect_kia_surfaces(context: _CatalogContext) -> list[dict[str, Any]]:
    relative = "core/kia/module_registry.py"
    tree = context.parse_python(relative)
    if tree is None:
        return []
    module_ids = _literal_assignment(tree, "KIA_MODULE_IDS")
    if not isinstance(module_ids, (list, tuple)):
        context.finding(
            "ENUMERATOR_FAILED", "KIA module ID registry is not static", source_ref=relative
        )
        module_ids = ()
    evidence = [context.evidence(relative, anchor="KIA_MODULE_IDS")]
    records = [
        _record(
            "surfaces",
            record_id=f"surface:kia.{_slug(module_id)}",
            discovery_key=f"kia:module:{module_id}",
            record_status="DISCOVERED",
            evidence_refs=evidence,
            kind="kia_module",
            canonical_selector=f"kia:{module_id}",
            surface_family_id="surface-family:kia.module",
            facet_contract={"module_id": str(module_id), "default": True},
            principal_policy_ref=None,
            input_contract_ref=None,
            output_contract_ref="PluggableModule",
            lifecycle="active",
            decision_ref=None,
        )
        for module_id in module_ids
    ]
    records.append(
        _record(
            "surfaces",
            record_id="surface:kia.dynamic-module-factory",
            discovery_key="kia:dynamic-module-factory",
            record_status="ADJUDICATION_REQUIRED",
            evidence_refs=[context.evidence(relative, anchor="build_kia_module_registry")],
            kind="dynamic_trigger_registry",
            canonical_selector="kia:<dynamic-module-id>",
            surface_family_id="surface-family:kia.module",
            facet_contract={"parameter": "module_factories", "closed_name_set": False},
            principal_policy_ref=None,
            input_contract_ref=None,
            output_contract_ref="PluggableModule",
            lifecycle="active",
            decision_ref=None,
        )
    )
    return records
