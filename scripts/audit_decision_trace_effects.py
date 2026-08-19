#!/usr/bin/env python3
"""Read-only COG-036 audit for decisions, material commands, and effects."""

from __future__ import annotations

import argparse
import ast
from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
from typing import Any, Callable, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cognitive.decision_trace_migration import (  # noqa: E402
    SourceDomain,
    configured_source_domains,
    default_source_domains,
)
from core.cognitive.material_effect_schema import (  # noqa: E402
    configured_material_effect_databases,
)
from core.config import get_config  # noqa: E402
from scripts.audit_decision_trace_effect_contracts import (  # noqa: E402, F401
    ACTION_LEDGER_DIRECT_SQL_SITES,
    ACTION_LEDGER_OBSERVATION_CALL,
    ACTION_LEDGER_OBSERVATION_CALL_COUNTS,
    ACTION_LEDGER_OBSERVATION_CALLERS,
    ACTION_LEDGER_PERSISTENCE_CALL,
    ACTION_LEDGER_PERSISTENCE_CALLERS,
    CANONICAL_GUARD_CALLS,
    CANONICAL_GUARD_MODULE,
    DELEGATED_SINK_CONTRACTS,
    REPORT_SCHEMA_VERSION,
    SINK_CONTRACTS,
    ZERO_METRICS,
    _ACTION_LEDGER_DML_PATTERN,
)
from scripts.audit_decision_trace_effect_runtime import (  # noqa: E402, F401
    _audit_dead_letter_supersessions,
    _audit_external_domains,
    _audit_live_store,
    _audit_target_effect_journals,
    _receipt_matches,
)


def audit_decision_trace_effects(
    *,
    state_db: Path,
    strict: bool,
    root: Path = ROOT,
    database_dir: Path | None = None,
    source_domains: Iterable[SourceDomain] | None = None,
    target_effect_databases: Iterable[Path] | None = None,
) -> dict[str, Any]:
    """Rebuild every denominator without calling runtime verification helpers."""

    resolved_database_dir = Path(database_dir or Path(state_db).parent)
    sink_audit = _audit_sink_contracts(root)
    live = _audit_live_store(Path(state_db))
    target_effect_audit = _audit_target_effect_journals(
        state_db=Path(state_db),
        journal_paths=tuple(
            target_effect_databases
            or (
                resolved_database_dir / "policy_patches.db",
                resolved_database_dir / "user_signals.db",
                resolved_database_dir / "cognitive_graph.db",
                resolved_database_dir / "knowledge_graph.db",
            )
        ),
    )
    external = _audit_external_domains(
        database_dir=resolved_database_dir,
        state_db=Path(state_db),
        source_domains=source_domains,
    )
    metrics = {name: int(live["metrics"].get(name, 0)) for name in ZERO_METRICS}
    metrics["action_without_decision"] += int(external["uncovered_count"])
    failures = list(live["failures"])
    failures.extend(sink_audit["failures"])
    failures.extend(target_effect_audit["failures"])
    failures.extend(external["failures"])
    failures.extend(f"{name}={value}" for name, value in metrics.items() if value)
    ok = not failures
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "ok": ok,
        "strict": bool(strict),
        "metrics": metrics,
        "live": {key: value for key, value in live.items() if key not in {"metrics", "failures"}},
        "external_domains": {key: value for key, value in external.items() if key != "failures"},
        "target_effect_audit": {
            key: value for key, value in target_effect_audit.items() if key != "failures"
        },
        "sink_audit": sink_audit,
        "errors": failures,
    }


def _audit_sink_contracts(root: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    failures: list[str] = []
    for relative_path, function_name, required_call, effect_calls in SINK_CONTRACTS:
        path = root / relative_path
        row: dict[str, Any] = {
            "file": relative_path,
            "function": function_name,
            "required_call": required_call,
            "effect_calls": list(effect_calls),
            "status": "missing",
        }
        if not path.is_file():
            failures.append(f"material sink file is missing: {relative_path}")
            rows.append(row)
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            failures.append(f"material sink AST unavailable: {relative_path}: {exc}")
            rows.append(row)
            continue
        functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function_name
        ]
        analyses = []
        provenance_failures: list[str] = []
        for function in functions:
            guard_matcher, provenance_error = _guard_matcher(
                tree,
                function,
                required_call,
            )
            if provenance_error:
                provenance_failures.append(provenance_error)
            analyses.append(
                _permit_dominance(
                    function,
                    guard_call=required_call,
                    effect_calls=effect_calls,
                    guard_matcher=guard_matcher,
                )
            )
        effect_count = sum(value.effect_count for value in analyses)
        violations = [line for value in analyses for line in value.violation_lines]
        guarded = (
            bool(functions) and effect_count > 0 and not violations and not provenance_failures
        )
        row["status"] = "guarded" if guarded else "unguarded"
        row["line"] = min((node.lineno for node in functions), default=0)
        row["effect_count"] = effect_count
        row["violation_lines"] = sorted(set(violations))
        row["guard_provenance_errors"] = sorted(set(provenance_failures))
        if not guarded:
            failures.append(
                "material sink is not permit dominated: "
                f"{relative_path}:{function_name}:"
                f"effects={effect_count}:lines={sorted(set(violations))}:"
                f"provenance={sorted(set(provenance_failures))}"
            )
        rows.append(row)
    delegated = _audit_delegated_sink_contracts(root)
    direct_by_key = {
        (row["file"], row["function"]): row
        for row in rows
    }
    for row in delegated["rows"]:
        delegate_target = direct_by_key.get((row["file"], row["delegate"]))
        row["delegate_guarded"] = (
            delegate_target is not None
            and delegate_target["status"] == "guarded"
        )
        if not row["delegate_guarded"]:
            delegated["failures"].append(
                "delegated material sink target is not directly guarded: "
                f"{row['file']}:{row['function']}->{row['delegate']}"
            )
    failures.extend(delegated["failures"])
    internal_callsite_audit = _audit_action_ledger_persistence_call_sites(root)
    failures.extend(internal_callsite_audit["failures"])
    schema_owner_audit = _audit_material_effect_schema_owners(root)
    failures.extend(schema_owner_audit["failures"])
    return {
        "denominator": len(SINK_CONTRACTS) + len(DELEGATED_SINK_CONTRACTS),
        "direct_sink_count": len(SINK_CONTRACTS),
        "delegated_sink_count": len(DELEGATED_SINK_CONTRACTS),
        "guarded": (
            sum(1 for row in rows if row["status"] == "guarded")
            + sum(
                1
                for row in delegated["rows"]
                if row["status"] == "delegated_guarded"
            )
        ),
        "bypass_count": (
            sum(1 for row in rows if row["status"] != "guarded")
            + sum(
                1
                for row in delegated["rows"]
                if row["status"] != "delegated_guarded"
            )
        ),
        "sinks": rows,
        "delegated_sinks": delegated["rows"],
        "internal_callsite_audit": {
            key: value for key, value in internal_callsite_audit.items() if key != "failures"
        },
        "material_effect_schema_ownership": {
            key: value for key, value in schema_owner_audit.items() if key != "failures"
        },
        "failures": failures,
    }


def _audit_delegated_sink_contracts(root: Path) -> dict[str, Any]:
    """Verify non-writing entrypoints hand authorization to one guarded sink."""

    rows: list[dict[str, Any]] = []
    failures: list[str] = []
    forbidden_direct_calls = {
        "execute",
        "executemany",
        "executescript",
        "commit",
        "write",
        "unlink",
        "replace",
        "sql_write",
    }
    for relative_path, function_name, delegate, authorization_parameter in (
        DELEGATED_SINK_CONTRACTS
    ):
        row: dict[str, Any] = {
            "file": relative_path,
            "function": function_name,
            "delegate": delegate,
            "authorization_parameter": authorization_parameter,
            "status": "missing",
        }
        path = root / relative_path
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            failures.append(
                f"delegated material sink AST unavailable: {relative_path}: {exc}"
            )
            rows.append(row)
            continue
        functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function_name
        ]
        delegate_calls = [
            call
            for function in functions
            for call in ast.walk(function)
            if isinstance(call, ast.Call) and _call_name(call) == delegate
        ]
        direct_calls = [
            call
            for function in functions
            for call in ast.walk(function)
            if isinstance(call, ast.Call)
            and _call_name(call) in forbidden_direct_calls
        ]
        declared_parameters = {
            argument.arg
            for function in functions
            for argument in (
                *function.args.posonlyargs,
                *function.args.args,
                *function.args.kwonlyargs,
            )
        }
        authorization_parameter_declared = (
            authorization_parameter in declared_parameters
        )
        handoff_ok = len(delegate_calls) == 1 and any(
            keyword.arg == authorization_parameter
            and isinstance(keyword.value, ast.Name)
            and keyword.value.id == authorization_parameter
            for keyword in delegate_calls[0].keywords
        )
        guarded = (
            len(functions) == 1
            and authorization_parameter_declared
            and handoff_ok
            and not direct_calls
        )
        row.update(
            {
                "status": "delegated_guarded" if guarded else "unguarded",
                "line": min((node.lineno for node in functions), default=0),
                "delegate_call_count": len(delegate_calls),
                "direct_effect_lines": sorted(call.lineno for call in direct_calls),
                "authorization_parameter_declared": (
                    authorization_parameter_declared
                ),
                "authorization_handoff": handoff_ok,
            }
        )
        if not guarded:
            failures.append(
                "delegated material sink contract failed: "
                f"{relative_path}:{function_name}:delegate={delegate}:"
                f"calls={len(delegate_calls)}:handoff={handoff_ok}:"
                f"parameter_declared={authorization_parameter_declared}:"
                f"direct_effect_lines={row['direct_effect_lines']}"
            )
        rows.append(row)
    return {"rows": rows, "failures": failures}


def _audit_material_effect_schema_owners(root: Path) -> dict[str, Any]:
    literal_patterns = {
        "material_target_effects": re.compile(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
            r"material_target_effects(?![A-Za-z0-9_])",
            re.IGNORECASE,
        ),
        "mnemos_material_effect_schema_registry": re.compile(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
            r"mnemos_material_effect_schema_registry(?![A-Za-z0-9_])",
            re.IGNORECASE,
        ),
    }
    template_patterns = {
        "material_target_effects": (
            re.compile(
                r"^TABLE_NAME\s*=\s*['\"]material_target_effects['\"]",
                re.MULTILINE,
            ),
            re.compile(
                r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?\{TABLE_NAME\}",
                re.IGNORECASE,
            ),
        ),
        "mnemos_material_effect_schema_registry": (
            re.compile(
                r"^REGISTRY_TABLE\s*=\s*['\"]" r"mnemos_material_effect_schema_registry['\"]",
                re.MULTILINE,
            ),
            re.compile(
                r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?" r"\{REGISTRY_TABLE\}",
                re.IGNORECASE,
            ),
        ),
    }
    observed: list[tuple[str, str]] = []
    failures: list[str] = []
    for path in _production_python_paths(root):
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            failures.append(f"material-effect schema source unavailable: {path}: {exc}")
            continue
        relative = str(path.relative_to(root))
        for component, pattern in literal_patterns.items():
            declaration, template = template_patterns[component]
            if pattern.search(source) or (declaration.search(source) and template.search(source)):
                observed.append((component, relative))
    expected = [
        ("material_target_effects", "core/cognitive/material_effect_schema.py"),
        (
            "mnemos_material_effect_schema_registry",
            "core/cognitive/material_effect_schema.py",
        ),
    ]
    if sorted(observed) != sorted(expected):
        failures.append(
            "material-effect DDL owners must be exact: "
            f"expected={sorted(expected)!r} actual={sorted(observed)!r}"
        )
    return {
        "expected_owners": [
            {"component": component, "file": file_name} for component, file_name in expected
        ],
        "observed_owners": [
            {"component": component, "file": file_name} for component, file_name in sorted(observed)
        ],
        "failures": failures,
    }


def _audit_action_ledger_persistence_call_sites(root: Path) -> dict[str, Any]:
    observed: list[tuple[str, str, int, str]] = []
    failures: list[str] = []
    for path in _production_python_paths(root):
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            failures.append(f"internal persistence callsite source unavailable: {path}: {exc}")
            continue
        if ACTION_LEDGER_PERSISTENCE_CALL not in source:
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            failures.append(f"internal persistence callsite AST unavailable: {path}: {exc}")
            continue
        relative = str(path.relative_to(root))
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }

        class Visitor(ast.NodeVisitor):
            """Collect direct internal persistence accesses with function context."""

            def __init__(self) -> None:
                self.function_stack: list[str] = []

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                """Visit a synchronous function under its local audit context."""

                self.function_stack.append(node.name)
                self.generic_visit(node)
                self.function_stack.pop()

            def visit_AsyncFunctionDef(
                self,
                node: ast.AsyncFunctionDef,
            ) -> None:
                """Visit an asynchronous function under its audit context."""

                self.function_stack.append(node.name)
                self.generic_visit(node)
                self.function_stack.pop()

            def visit_Attribute(self, node: ast.Attribute) -> None:
                """Record access to the internal persistence capability."""

                if node.attr == ACTION_LEDGER_PERSISTENCE_CALL:
                    parent = parents.get(node)
                    access_kind = (
                        "direct_call"
                        if isinstance(parent, ast.Call) and parent.func is node
                        else "attribute_access"
                    )
                    observed.append(
                        (
                            relative,
                            (self.function_stack[-1] if self.function_stack else "<module>"),
                            int(node.lineno),
                            access_kind,
                        )
                    )
                self.generic_visit(node)

        Visitor().visit(tree)
    counts = Counter(
        (path, function)
        for path, function, _line, access_kind in observed
        if access_kind == "direct_call" and (path, function) in ACTION_LEDGER_PERSISTENCE_CALLERS
    )
    actual_callers = frozenset(counts)
    missing = sorted(ACTION_LEDGER_PERSISTENCE_CALLERS - actual_callers)
    unexpected = sorted(
        (path, function, line, access_kind)
        for path, function, line, access_kind in observed
        if access_kind != "direct_call" or (path, function) not in ACTION_LEDGER_PERSISTENCE_CALLERS
    )
    duplicates = sorted(
        (path, function, count) for (path, function), count in counts.items() if count != 1
    )
    if missing:
        failures.append("missing exact ActionLedger persistence callsites: " + repr(missing))
    if unexpected:
        failures.append("unexpected ActionLedger persistence access: " + repr(unexpected))
    if duplicates:
        failures.append("non-unique ActionLedger persistence callsites: " + repr(duplicates))
    direct_sql_audit = _audit_action_ledger_direct_sql_sites(root)
    failures.extend(direct_sql_audit["failures"])
    observation_audit = _audit_action_ledger_observation_call_sites(root)
    failures.extend(observation_audit["failures"])
    return {
        "call": ACTION_LEDGER_PERSISTENCE_CALL,
        "expected_callers": [
            {"file": path, "function": function}
            for path, function in sorted(ACTION_LEDGER_PERSISTENCE_CALLERS)
        ],
        "observed_calls": [
            {
                "file": path,
                "function": function,
                "line": line,
                "access_kind": access_kind,
            }
            for path, function, line, access_kind in sorted(observed)
        ],
        "direct_sql_audit": {
            key: value for key, value in direct_sql_audit.items() if key != "failures"
        },
        "diagnostic_observation_audit": {
            key: value for key, value in observation_audit.items() if key != "failures"
        },
        "failures": failures,
    }


def _audit_action_ledger_observation_call_sites(root: Path) -> dict[str, Any]:
    observed: list[tuple[str, str, int, str]] = []
    failures: list[str] = []
    for path in _production_python_paths(root):
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            failures.append(f"ActionLedger observation source unavailable: {path}: {exc}")
            continue
        if ACTION_LEDGER_OBSERVATION_CALL not in source:
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            failures.append(f"ActionLedger observation AST unavailable: {path}: {exc}")
            continue
        relative = str(path.relative_to(root))
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }

        class Visitor(ast.NodeVisitor):
            """Collect ActionLedger observation call sites with function context."""

            def __init__(self) -> None:
                self.function_stack: list[str] = []

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                """Visit a synchronous function under its local audit context."""

                self.function_stack.append(node.name)
                self.generic_visit(node)
                self.function_stack.pop()

            def visit_AsyncFunctionDef(
                self,
                node: ast.AsyncFunctionDef,
            ) -> None:
                """Visit an asynchronous function under its audit context."""

                self.function_stack.append(node.name)
                self.generic_visit(node)
                self.function_stack.pop()

            def visit_Attribute(self, node: ast.Attribute) -> None:
                """Record access to the typed observation persistence method."""

                if node.attr == ACTION_LEDGER_OBSERVATION_CALL:
                    parent = parents.get(node)
                    observed.append(
                        (
                            relative,
                            self.function_stack[-1] if self.function_stack else "<module>",
                            int(node.lineno),
                            (
                                "direct_call"
                                if isinstance(parent, ast.Call) and parent.func is node
                                else "attribute_access"
                            ),
                        )
                    )
                self.generic_visit(node)

        Visitor().visit(tree)

    counts = Counter(
        (path, function)
        for path, function, _line, access_kind in observed
        if access_kind == "direct_call" and (path, function) in ACTION_LEDGER_OBSERVATION_CALLERS
    )
    missing = sorted(ACTION_LEDGER_OBSERVATION_CALLERS - frozenset(counts))
    unexpected = sorted(
        value
        for value in observed
        if value[3] != "direct_call"
        or (value[0], value[1]) not in ACTION_LEDGER_OBSERVATION_CALLERS
    )
    duplicates = sorted(
        (
            path,
            function,
            count,
            ACTION_LEDGER_OBSERVATION_CALL_COUNTS[(path, function)],
        )
        for (path, function), count in counts.items()
        if count != ACTION_LEDGER_OBSERVATION_CALL_COUNTS[(path, function)]
    )
    if missing:
        failures.append(
            "missing exact ActionLedger diagnostic observation callsites: " + repr(missing)
        )
    if unexpected:
        failures.append(
            "unexpected ActionLedger diagnostic observation access: " + repr(unexpected)
        )
    if duplicates:
        failures.append(
            "non-unique ActionLedger diagnostic observation callsites: " + repr(duplicates)
        )
    return {
        "call": ACTION_LEDGER_OBSERVATION_CALL,
        "expected_callers": [
            {
                "file": path,
                "function": function,
                "expected_call_count": ACTION_LEDGER_OBSERVATION_CALL_COUNTS[(path, function)],
            }
            for path, function in sorted(ACTION_LEDGER_OBSERVATION_CALLERS)
        ],
        "observed_calls": [
            {
                "file": path,
                "function": function,
                "line": line,
                "access_kind": access_kind,
            }
            for path, function, line, access_kind in sorted(observed)
        ],
        "failures": failures,
    }


def _production_python_paths(root: Path) -> tuple[Path, ...]:
    paths: set[Path] = set(root.glob("*.py"))
    for base_name in ("core", "daemon", "scripts", "integrations"):
        base = root / base_name
        if base.is_dir():
            paths.update(base.rglob("*.py"))
    return tuple(sorted(path for path in paths if path.is_file()))


def _audit_action_ledger_direct_sql_sites(root: Path) -> dict[str, Any]:
    observed: list[tuple[str, str, int, str]] = []
    failures: list[str] = []
    for path in _production_python_paths(root):
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            failures.append(f"ActionLedger SQL source unavailable: {path}: {exc}")
            continue
        if "action_ledger" not in source.lower():
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            failures.append(f"ActionLedger SQL AST unavailable: {path}: {exc}")
            continue
        relative = str(path.relative_to(root))

        class Visitor(ast.NodeVisitor):
            """Collect direct ActionLedger DML literals with function context."""

            def __init__(self) -> None:
                self.function_stack: list[str] = []

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                """Visit a synchronous function under its local audit context."""

                self.function_stack.append(node.name)
                self.generic_visit(node)
                self.function_stack.pop()

            def visit_AsyncFunctionDef(
                self,
                node: ast.AsyncFunctionDef,
            ) -> None:
                """Visit an asynchronous function under its audit context."""

                self.function_stack.append(node.name)
                self.generic_visit(node)
                self.function_stack.pop()

            def visit_Constant(self, node: ast.Constant) -> None:
                """Record ActionLedger DML embedded in a string constant."""

                if isinstance(node.value, str):
                    for match in _ACTION_LEDGER_DML_PATTERN.finditer(node.value):
                        raw_kind = match.group("kind").lower()
                        kind = raw_kind.split()[0]
                        observed.append(
                            (
                                relative,
                                (self.function_stack[-1] if self.function_stack else "<module>"),
                                int(node.lineno),
                                kind,
                            )
                        )
                self.generic_visit(node)

        Visitor().visit(tree)
    actual_sites = frozenset((path, function, kind) for path, function, _line, kind in observed)
    missing = sorted(ACTION_LEDGER_DIRECT_SQL_SITES - actual_sites)
    unexpected = sorted(actual_sites - ACTION_LEDGER_DIRECT_SQL_SITES)
    duplicates = sorted(
        (path, function, kind, count)
        for (path, function, kind), count in Counter(
            (path, function, kind) for path, function, _line, kind in observed
        ).items()
        if count != 1
    )
    if missing:
        failures.append("missing exact ActionLedger SQL sites: " + repr(missing))
    if unexpected:
        failures.append("unexpected ActionLedger direct SQL sites: " + repr(unexpected))
    if duplicates:
        failures.append("non-unique ActionLedger direct SQL sites: " + repr(duplicates))
    return {
        "expected_sites": [
            {"file": path, "function": function, "kind": kind}
            for path, function, kind in sorted(ACTION_LEDGER_DIRECT_SQL_SITES)
        ],
        "observed_sites": [
            {"file": path, "function": function, "line": line, "kind": kind}
            for path, function, line, kind in sorted(observed)
        ],
        "failures": failures,
    }


def _guard_matcher(
    tree: ast.Module,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    required_call: str,
) -> tuple[Callable[[ast.Call], bool], str]:
    direct_imports, module_imports = _canonical_guard_imports(tree)
    local_bindings = _function_local_bindings(function)

    def canonical_origin(call: ast.Call) -> str:
        """Resolve a call to its canonical imported guard capability."""

        target = call.func
        if isinstance(target, ast.Name) and target.id not in local_bindings:
            return direct_imports.get(target.id, "")
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id not in local_bindings
            and target.value.id in module_imports
            and target.attr in CANONICAL_GUARD_CALLS
        ):
            return target.attr
        return ""

    if required_call in CANONICAL_GUARD_CALLS:
        imported = required_call in set(direct_imports.values())
        imported = imported or bool(module_imports)
        if not imported:
            return (lambda _call: False), (
                f"{required_call} is not imported from {CANONICAL_GUARD_MODULE}"
            )
        return (
            lambda call: canonical_origin(call) == required_call,
            "",
        )

    return (lambda _call: False), (f"local authorization delegate is forbidden: {required_call}")


def _canonical_guard_imports(
    tree: ast.Module,
) -> tuple[dict[str, str], set[str]]:
    direct: dict[str, str] = {}
    modules: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound = alias.asname or alias.name
                direct.pop(bound, None)
                modules.discard(bound)
                if node.module == CANONICAL_GUARD_MODULE and alias.name in CANONICAL_GUARD_CALLS:
                    direct[bound] = alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".", 1)[0]
                direct.pop(bound, None)
                modules.discard(bound)
                if alias.name == CANONICAL_GUARD_MODULE:
                    modules.add(bound)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            direct.pop(node.name, None)
            modules.discard(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets: list[ast.AST] = []
            if isinstance(node, ast.Assign):
                targets.extend(node.targets)
            else:
                targets.append(node.target)
            for target in targets:
                for child in ast.walk(target):
                    if isinstance(child, ast.Name):
                        direct.pop(child.id, None)
                        modules.discard(child.id)
    return direct, modules


def _function_local_bindings(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> set[str]:
    names = {
        argument.arg
        for argument in (
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
        )
    }
    if function.args.vararg is not None:
        names.add(function.args.vararg.arg)
    if function.args.kwarg is not None:
        names.add(function.args.kwarg.arg)
    for node in ast.walk(function):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node is not function:
                names.add(node.name)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name.split(".", 1)[0])
    return names


@dataclass(frozen=True)
class _DominanceResult:
    guarded: bool
    alive: bool
    effect_count: int
    violation_lines: tuple[int, ...]


def _permit_dominance(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    guard_call: str,
    effect_calls: tuple[str, ...],
    guard_matcher: Callable[[ast.Call], bool] | None = None,
) -> _DominanceResult:
    return _analyze_statements(
        function.body,
        guarded=False,
        guard_call=guard_call,
        effect_calls=set(effect_calls),
        guard_matcher=guard_matcher,
    )


def _analyze_statements(
    statements: Iterable[ast.stmt],
    *,
    guarded: bool,
    guard_call: str,
    effect_calls: set[str],
    guard_matcher: Callable[[ast.Call], bool] | None,
) -> _DominanceResult:
    current_guarded = guarded
    alive = True
    effect_count = 0
    violations: list[int] = []
    for statement in statements:
        if not alive:
            break
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            definition_calls = [
                call
                for expression in _definition_header_expressions(statement)
                for call in _expression_calls(expression)
            ]
            definition_effects = _effect_calls(
                definition_calls,
                effect_calls,
            )
            effect_count += len(definition_effects)
            if definition_effects and not current_guarded:
                violations.extend(call.lineno for call in definition_effects)
            if any(
                _is_guard_call(call, guard_call, guard_matcher)
                for expression in _definition_header_expressions(statement)
                for call in _unconditional_expression_calls(expression)
            ):
                current_guarded = True
            continue
        if isinstance(statement, ast.ClassDef):
            header_calls = [
                call
                for expression in _definition_header_expressions(statement)
                for call in _expression_calls(expression)
            ]
            header_effects = _effect_calls(header_calls, effect_calls)
            effect_count += len(header_effects)
            if header_effects and not current_guarded:
                violations.extend(call.lineno for call in header_effects)
            class_guarded = current_guarded or any(
                _is_guard_call(call, guard_call, guard_matcher)
                for expression in _definition_header_expressions(statement)
                for call in _unconditional_expression_calls(expression)
            )
            class_body = _analyze_statements(
                statement.body,
                guarded=class_guarded,
                guard_call=guard_call,
                effect_calls=effect_calls,
                guard_matcher=guard_matcher,
            )
            effect_count += class_body.effect_count
            violations.extend(class_body.violation_lines)
            alive = class_body.alive
            current_guarded = class_body.guarded
            continue
        if isinstance(statement, ast.If):
            test_calls = _expression_calls(statement.test)
            test_effects = _effect_calls(test_calls, effect_calls)
            effect_count += len(test_effects)
            if test_effects and not current_guarded:
                violations.extend(call.lineno for call in test_effects)
            branch_guarded = current_guarded or any(
                _is_guard_call(call, guard_call, guard_matcher)
                for call in _unconditional_expression_calls(statement.test)
            )
            body = _analyze_statements(
                statement.body,
                guarded=branch_guarded,
                guard_call=guard_call,
                effect_calls=effect_calls,
                guard_matcher=guard_matcher,
            )
            other = (
                _analyze_statements(
                    statement.orelse,
                    guarded=branch_guarded,
                    guard_call=guard_call,
                    effect_calls=effect_calls,
                    guard_matcher=guard_matcher,
                )
                if statement.orelse
                else _DominanceResult(
                    branch_guarded,
                    True,
                    0,
                    (),
                )
            )
            effect_count += body.effect_count + other.effect_count
            violations.extend(body.violation_lines)
            violations.extend(other.violation_lines)
            continuing = [value for value in (body, other) if value.alive]
            alive = bool(continuing)
            current_guarded = bool(continuing) and all(value.guarded for value in continuing)
            continue
        if isinstance(statement, (ast.With, ast.AsyncWith)):
            context_calls = [
                call for item in statement.items for call in _expression_calls(item.context_expr)
            ]
            context_effects = _effect_calls(context_calls, effect_calls)
            effect_count += len(context_effects)
            if context_effects and not current_guarded:
                violations.extend(call.lineno for call in context_effects)
            body_guarded = current_guarded or any(
                _is_guard_call(call, guard_call, guard_matcher)
                for item in statement.items
                for call in _unconditional_expression_calls(item.context_expr)
            )
            body = _analyze_statements(
                statement.body,
                guarded=body_guarded,
                guard_call=guard_call,
                effect_calls=effect_calls,
                guard_matcher=guard_matcher,
            )
            effect_count += body.effect_count
            violations.extend(body.violation_lines)
            # A context manager may suppress an exception raised by a guard in
            # its body.  Only guards evaluated before __enter__ may dominate
            # statements after the with block.  Conservatively keep the later
            # path alive because __exit__ is caller-defined.
            alive = True
            current_guarded = body_guarded
            continue
        if isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
            header = statement.test if isinstance(statement, ast.While) else statement.iter
            header_calls = _expression_calls(header)
            header_effects = _effect_calls(header_calls, effect_calls)
            effect_count += len(header_effects)
            if header_effects and not current_guarded:
                violations.extend(call.lineno for call in header_effects)
            loop_guarded = current_guarded or any(
                _is_guard_call(call, guard_call, guard_matcher)
                for call in _unconditional_expression_calls(header)
            )
            body = _analyze_statements(
                statement.body,
                guarded=loop_guarded,
                guard_call=guard_call,
                effect_calls=effect_calls,
                guard_matcher=guard_matcher,
            )
            other = _analyze_statements(
                statement.orelse,
                guarded=current_guarded,
                guard_call=guard_call,
                effect_calls=effect_calls,
                guard_matcher=guard_matcher,
            )
            effect_count += body.effect_count + other.effect_count
            violations.extend(body.violation_lines)
            violations.extend(other.violation_lines)
            current_guarded = current_guarded and other.guarded
            alive = True
            continue
        if isinstance(statement, ast.Try):
            paths = [
                _analyze_statements(
                    statement.body,
                    guarded=current_guarded,
                    guard_call=guard_call,
                    effect_calls=effect_calls,
                    guard_matcher=guard_matcher,
                ),
                *[
                    _analyze_statements(
                        handler.body,
                        guarded=current_guarded,
                        guard_call=guard_call,
                        effect_calls=effect_calls,
                        guard_matcher=guard_matcher,
                    )
                    for handler in statement.handlers
                ],
            ]
            if statement.orelse:
                paths.append(
                    _analyze_statements(
                        statement.orelse,
                        guarded=paths[0].guarded,
                        guard_call=guard_call,
                        effect_calls=effect_calls,
                        guard_matcher=guard_matcher,
                    )
                )
            effect_count += sum(value.effect_count for value in paths)
            for value in paths:
                violations.extend(value.violation_lines)
            continuing = [value for value in paths if value.alive]
            merged_guard = bool(continuing) and all(value.guarded for value in continuing)
            if statement.finalbody:
                # finally runs for normal paths, exceptions, and paths that
                # return/raise before a later guard in the try body.  Analyze
                # its effects from the try-entry state so an early exit cannot
                # manufacture dominance.  Separately compute the state of the
                # paths that can continue after the try.
                final_all_paths = _analyze_statements(
                    statement.finalbody,
                    guarded=current_guarded,
                    guard_call=guard_call,
                    effect_calls=effect_calls,
                    guard_matcher=guard_matcher,
                )
                effect_count += final_all_paths.effect_count
                violations.extend(final_all_paths.violation_lines)
                final_continuing = _analyze_statements(
                    statement.finalbody,
                    guarded=merged_guard,
                    guard_call=guard_call,
                    effect_calls=effect_calls,
                    guard_matcher=guard_matcher,
                )
                alive = bool(continuing) and final_continuing.alive
                current_guarded = final_continuing.guarded if alive else False
            else:
                current_guarded = merged_guard
                alive = bool(continuing)
            continue
        if isinstance(statement, ast.Match):
            subject_calls = _expression_calls(statement.subject)
            subject_effects = _effect_calls(subject_calls, effect_calls)
            effect_count += len(subject_effects)
            if subject_effects and not current_guarded:
                violations.extend(call.lineno for call in subject_effects)
            subject_guarded = current_guarded or any(
                _is_guard_call(call, guard_call, guard_matcher)
                for call in _unconditional_expression_calls(statement.subject)
            )
            match_paths: list[_DominanceResult] = []
            exhaustive = False
            for case in statement.cases:
                if case.guard is not None:
                    guard_calls = _expression_calls(case.guard)
                    guard_effects = _effect_calls(guard_calls, effect_calls)
                    effect_count += len(guard_effects)
                    if guard_effects and not subject_guarded:
                        violations.extend(call.lineno for call in guard_effects)
                match_paths.append(
                    _analyze_statements(
                        case.body,
                        guarded=subject_guarded,
                        guard_call=guard_call,
                        effect_calls=effect_calls,
                        guard_matcher=guard_matcher,
                    )
                )
                if _is_unguarded_match_default(case):
                    exhaustive = True
            if not exhaustive:
                match_paths.append(_DominanceResult(subject_guarded, True, 0, ()))
            effect_count += sum(value.effect_count for value in match_paths)
            for value in match_paths:
                violations.extend(value.violation_lines)
            continuing = [value for value in match_paths if value.alive]
            alive = bool(continuing)
            current_guarded = bool(continuing) and all(value.guarded for value in continuing)
            continue
        calls = _expression_calls(statement)
        effects = _effect_calls(calls, effect_calls)
        effect_count += len(effects)
        if effects and not current_guarded:
            violations.extend(call.lineno for call in effects)
        if not isinstance(statement, ast.Assert) and any(
            _is_guard_call(call, guard_call, guard_matcher)
            for call in _unconditional_expression_calls(statement)
        ):
            current_guarded = True
        if isinstance(statement, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
            alive = False
    return _DominanceResult(
        guarded=current_guarded,
        alive=alive,
        effect_count=effect_count,
        violation_lines=tuple(violations),
    )


def _is_unguarded_match_default(case: ast.match_case) -> bool:
    """Return whether a case is an unconditional capture/wildcard default."""

    return (
        case.guard is None
        and isinstance(case.pattern, ast.MatchAs)
        and case.pattern.pattern is None
    )


def _definition_header_expressions(
    statement: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> tuple[ast.expr, ...]:
    """Expressions executed immediately when a def/class statement runs."""

    expressions: list[ast.expr] = list(statement.decorator_list)
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
        expressions.extend(statement.args.defaults)
        expressions.extend(value for value in statement.args.kw_defaults if value is not None)
        arguments = (
            *statement.args.posonlyargs,
            *statement.args.args,
            *statement.args.kwonlyargs,
        )
        expressions.extend(
            argument.annotation for argument in arguments if argument.annotation is not None
        )
        if statement.args.vararg and statement.args.vararg.annotation is not None:
            expressions.append(statement.args.vararg.annotation)
        if statement.args.kwarg and statement.args.kwarg.annotation is not None:
            expressions.append(statement.args.kwarg.annotation)
        if statement.returns is not None:
            expressions.append(statement.returns)
    else:
        expressions.extend(statement.bases)
        expressions.extend(keyword.value for keyword in statement.keywords)
    return tuple(expressions)


def _expression_calls(node: ast.AST) -> list[ast.Call]:
    calls: list[ast.Call] = []

    class Visitor(ast.NodeVisitor):
        """Collect calls in an expression without descending into closures."""

        def visit_FunctionDef(self, child: ast.FunctionDef) -> None:
            """Do not descend into a nested synchronous function."""

            return None

        def visit_AsyncFunctionDef(self, child: ast.AsyncFunctionDef) -> None:
            """Do not descend into a nested asynchronous function."""

            return None

        def visit_Lambda(self, child: ast.Lambda) -> None:
            """Do not descend into a deferred lambda body."""

            return None

        def visit_Call(self, child: ast.Call) -> None:
            """Record a call and inspect only its eagerly evaluated children."""

            calls.append(child)
            self.generic_visit(child)

    Visitor().visit(node)
    return calls


def _is_guard_call(
    call: ast.Call,
    guard_call: str,
    guard_matcher: Callable[[ast.Call], bool] | None,
) -> bool:
    if guard_matcher is not None:
        return bool(guard_matcher(call))
    return _call_name(call) == guard_call


def _unconditional_expression_calls(node: ast.AST) -> list[ast.Call]:
    """Return calls guaranteed to run when this expression/statement runs.

    The result is intentionally conservative for authorization: calls inside
    assertions, lambdas, comprehensions, conditional branches, and short-
    circuited boolean operands cannot establish permit dominance.
    """

    calls: list[ast.Call] = []

    def visit(current: ast.AST | None) -> None:
        """Collect calls guaranteed to run along the current expression path."""

        if current is None:
            return
        if isinstance(
            current,
            (
                ast.Lambda,
                ast.ListComp,
                ast.SetComp,
                ast.DictComp,
                ast.GeneratorExp,
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.ClassDef,
            ),
        ):
            return
        if isinstance(current, ast.BoolOp):
            if not current.values:
                return
            visit(current.values[0])
            for prior, value in zip(current.values, current.values[1:]):
                known, truth = _literal_truth(prior)
                continues = known and (
                    (isinstance(current.op, ast.And) and truth)
                    or (isinstance(current.op, ast.Or) and not truth)
                )
                if not continues:
                    break
                visit(value)
            return
        if isinstance(current, ast.IfExp):
            visit(current.test)
            known, truth = _literal_truth(current.test)
            if known:
                visit(current.body if truth else current.orelse)
            return
        if isinstance(current, ast.Call):
            visit(current.func)
            for argument in current.args:
                visit(argument)
            for keyword in current.keywords:
                visit(keyword.value)
            calls.append(current)
            return
        for child in ast.iter_child_nodes(current):
            visit(child)

    visit(node)
    return calls


def _literal_truth(node: ast.AST) -> tuple[bool, bool]:
    if isinstance(node, ast.Constant):
        return True, bool(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        known, truth = _literal_truth(node.operand)
        return known, not truth
    return False, False


def _effect_calls(
    calls: Iterable[ast.Call],
    effects: set[str],
) -> list[ast.Call]:
    return [
        call
        for call in calls
        if _call_name(call) in effects or ("sql_write" in effects and _is_sql_write(call))
    ]


def _call_name(call: ast.Call) -> str:
    target = call.func
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return ""


def _is_sql_write(call: ast.Call) -> bool:
    if _call_name(call) not in {"execute", "executemany", "executescript"}:
        return False
    if not call.args:
        return False
    sql = _literal_text(call.args[0]).lstrip().upper()
    return sql.startswith(("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER", "DROP"))


def _literal_text(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            str(value.value)
            for value in node.values
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
        )
    return ""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--database-dir", type=Path)
    parser.add_argument("--state-db", type=Path)
    parser.add_argument("--delivery-db", type=Path)
    parser.add_argument("--trusted-push-db", type=Path)
    return parser


def main() -> int:
    """Run the strict DecisionTrace effect audit and print its report."""

    args = _parser().parse_args()
    config = get_config()
    explicit_root = args.database_dir or (args.state_db.parent if args.state_db else None)
    database_dir = Path(explicit_root or config.database_dir).expanduser()
    state_db = Path(args.state_db or database_dir / "producer_consumer_ledger.db").expanduser()
    source_domains = (
        default_source_domains(
            database_dir=database_dir,
            delivery_db_path=args.delivery_db,
            trusted_push_db_path=args.trusted_push_db,
        )
        if explicit_root is not None
        else configured_source_domains(
            config=config,
            database_dir=database_dir,
            delivery_db_path=args.delivery_db,
            trusted_push_db_path=args.trusted_push_db,
        )
    )
    report = audit_decision_trace_effects(
        state_db=state_db,
        strict=args.strict,
        database_dir=database_dir,
        source_domains=source_domains,
        target_effect_databases=(
            (
                database_dir / "policy_patches.db",
                database_dir / "user_signals.db",
                database_dir / "cognitive_graph.db",
                database_dir / "knowledge_graph.db",
            )
            if explicit_root is not None
            else configured_material_effect_databases(config)
        ),
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(
            "decision-trace effect audit passed"
            if report["ok"]
            else "decision-trace effect audit failed"
        )
        for error in report["errors"]:
            print(f"- {error}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
