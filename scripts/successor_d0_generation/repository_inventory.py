"""Private implementation module for successor_d0_generation.repository_inventory."""

from __future__ import annotations

from collections import defaultdict

from typing import Any

from typing import Mapping

import ast

from .model import (
    _DDL_PATTERN,
    _record,
    _stable_digest,
)

from .snapshot import (
    _CatalogContext,
)


def _is_main_guard(node: ast.If) -> bool:
    if not isinstance(node.test, ast.Compare) or len(node.test.ops) != 1:
        return False
    if not isinstance(node.test.ops[0], ast.Eq) or len(node.test.comparators) != 1:
        return False
    values = (node.test.left, node.test.comparators[0])
    has_name = any(isinstance(value, ast.Name) and value.id == "__name__" for value in values)
    has_main = any(
        isinstance(value, ast.Constant) and value.value == "__main__" for value in values
    )
    return has_name and has_main


def _top_level_wrapper_calls(tree: ast.Module) -> list[str]:
    calls: list[str] = []
    for node in tree.body:
        candidate: ast.AST | None = None
        if isinstance(node, ast.Expr):
            candidate = node.value
        elif isinstance(node, ast.Raise):
            candidate = node.exc
        if isinstance(candidate, ast.Call):
            calls.append(ast.unparse(candidate.func))
    return calls


def _module_imports(tree: ast.Module) -> set[str]:
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def _collect_script_surfaces(
    context: _CatalogContext,
) -> tuple[list[dict[str, Any]], dict[str, list[str]], int]:
    script_paths = sorted(
        path.relative_to(context.root).as_posix()
        for path in (context.root / "scripts").rglob("*.py")
        if path.is_file()
    )
    trees: dict[str, ast.Module] = {}
    imports_by_path: dict[str, set[str]] = {}
    for relative in script_paths:
        tree = context.parse_python(relative)
        if tree is not None:
            trees[relative] = tree
            imports_by_path[relative] = _module_imports(tree)
    callers_by_module: dict[str, list[str]] = defaultdict(list)
    for caller, imports in imports_by_path.items():
        for imported in imports:
            if imported == "scripts" or not imported.startswith("scripts."):
                continue
            target = imported.replace(".", "/") + ".py"
            callers_by_module[target].append(caller)

    records: list[dict[str, Any]] = []
    selector_map: dict[str, list[str]] = defaultdict(list)
    unclassified = 0
    for relative in script_paths:
        tree = trees.get(relative)
        guarded = bool(
            tree and any(isinstance(node, ast.If) and _is_main_guard(node) for node in tree.body)
        )
        wrapper_calls = _top_level_wrapper_calls(tree) if tree is not None else []
        executable = bool(context.path(relative).stat().st_mode & 0o111)
        if guarded:
            classification = "script_entry"
            status_value = "DISCOVERED"
        elif wrapper_calls or executable:
            classification = "script_unguarded_wrapper"
            status_value = "ADJUDICATION_REQUIRED"
            unclassified += 1
        else:
            classification = "script_helper"
            status_value = "ADJUDICATION_REQUIRED"
            unclassified += 1
        record_id = f"surface:script.{_stable_digest(relative)}"
        records.append(
            _record(
                "surfaces",
                record_id=record_id,
                discovery_key=f"script-module:{relative}",
                record_status=status_value,
                evidence_refs=[context.evidence(relative, anchor="__main__")],
                kind="script_module",
                source_path=relative,
                script_classification=classification,
                canonical_selector=f"script:{relative}",
                surface_family_id="surface-family:script.module",
                facet_contract={
                    "path": relative,
                    "guarded_main": guarded,
                    "executable": executable,
                    "unguarded_top_level_calls": wrapper_calls,
                    "candidate_callers": sorted(set(callers_by_module.get(relative, []))),
                },
                principal_policy_ref=None,
                input_contract_ref="argparse:unknown",
                output_contract_ref=None,
                lifecycle="active",
                decision_ref=None,
            )
        )
        selector_map[f"script:{relative}"].append(record_id)
    return records, dict(selector_map), unclassified


def _collect_external_entry_challengers(
    context: _CatalogContext,
) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    count = 0
    for path in sorted(context.root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(context.root).as_posix()
        if relative.startswith(("scripts/", "tests/")):
            continue
        executable = bool(path.stat().st_mode & 0o111)
        guarded = False
        if path.suffix == ".py":
            tree = context.parse_python(relative)
            guarded = bool(
                tree
                and any(isinstance(node, ast.If) and _is_main_guard(node) for node in tree.body)
            )
        if not guarded and not executable:
            continue
        count += 1
        identity = {"path": relative, "guarded_main": guarded, "executable": executable}
        records.append(
            _record(
                "surfaces",
                record_id=f"surface:repo-entry-challenger.{_stable_digest(identity)}",
                discovery_key=f"repo-entry-challenger:{relative}",
                record_status="ADJUDICATION_REQUIRED",
                evidence_refs=[context.evidence(relative, anchor="__main__")],
                kind="repo_entry_challenger",
                canonical_selector=f"repo-entry:{relative}",
                surface_family_id="surface-family:repo.entry-challenger",
                facet_contract=identity,
                principal_policy_ref=None,
                input_contract_ref=None,
                output_contract_ref=None,
                lifecycle="active",
                decision_ref=None,
            )
        )
    return records, count


def _collect_schema_surfaces(context: _CatalogContext) -> tuple[list[dict[str, Any]], int]:
    relative = "docs/acceptance/schema_owner_manifest.json"
    payload = context.load_json(relative)
    if not isinstance(payload, Mapping) or not isinstance(payload.get("entries"), list):
        return [], 0
    entries = payload["entries"]
    declared_paths: set[str] = set()
    records: list[dict[str, Any]] = []
    unknown_owner_count = 0
    for entry in entries:
        if not isinstance(entry, Mapping) or not str(entry.get("path") or ""):
            context.finding("SCHEMA_INVALID", "schema owner entry is invalid", source_ref=relative)
            continue
        path = str(entry["path"])
        declared_paths.add(path)
        actual_hash = context.sha256(path)
        declared_hash = str(entry.get("source_sha256") or "")
        declared_hash = (
            declared_hash if declared_hash.startswith("sha256:") else f"sha256:{declared_hash}"
        )
        current = actual_hash == declared_hash
        if not current:
            context.finding(
                "STALE_SOURCE",
                f"schema owner source hash is stale: {path}",
                source_ref=relative,
                evidence={"declared": declared_hash, "actual": actual_hash},
            )
        owner_status = str(entry.get("owner_status") or "UNKNOWN")
        if owner_status != "REGISTERED":
            unknown_owner_count += 1
        record_id = f"surface:schema-owner.{_stable_digest(path)}"
        records.append(
            _record(
                "surfaces",
                record_id=record_id,
                discovery_key=f"schema-owner:{path}",
                record_status=("STALE_SOURCE" if not current else owner_status),
                evidence_refs=[
                    context.evidence(relative, anchor=path),
                    context.evidence(path, anchor="DDL"),
                ],
                kind="schema_owner_seed",
                source_path=path,
                canonical_selector=f"schema-owner:{path}",
                surface_family_id="surface-family:schema.owner",
                facet_contract={
                    "path": path,
                    "ddl_objects": entry.get("ddl_objects", []),
                    "ddl_operations": entry.get("ddl_operations", []),
                    "owner_status": owner_status,
                    "release_blocking": bool(entry.get("release_blocking", True)),
                },
                principal_policy_ref=None,
                input_contract_ref=None,
                output_contract_ref="schema-ddl",
                lifecycle="active",
                decision_ref=None,
            )
        )

    reverse_paths: set[str] = set()
    for prefix in ("core", "scripts", "daemon"):
        base = context.root / prefix
        if not base.exists():
            continue
        for candidate_path in base.rglob("*.py"):
            if not candidate_path.is_file():
                continue
            try:
                text = candidate_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            if _DDL_PATTERN.search(text):
                reverse_paths.add(candidate_path.relative_to(context.root).as_posix())
    if reverse_paths != declared_paths:
        context.finding(
            "STALE_SOURCE",
            "schema owner manifest path inventory differs from reverse DDL discovery",
            source_ref=relative,
            evidence={
                "missing_from_manifest": sorted(reverse_paths - declared_paths),
                "stale_in_manifest": sorted(declared_paths - reverse_paths),
            },
        )
        unknown_owner_count += len(reverse_paths - declared_paths)
    return records, unknown_owner_count
