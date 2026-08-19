#!/usr/bin/env python3
"""Audit the complete repo-doc, prompt, and Desktop system-map asset contract."""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Sequence, cast

import yaml  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.ops.durable_io import DurableIOError, regular_file_sha256
from core.ops.durable_io import read_native_bytes

DEFAULT_MANIFEST_PATH = REPO_ROOT / "docs" / "acceptance" / "document_asset_manifest.json"
DEFAULT_DESKTOP_ROOT = Path.home() / "Desktop" / "mnemos系统图谱"
SCHEMA_VERSION = "mnemos.document_asset_manifest.v1"
REPORT_SCHEMA = "mnemos.document_asset_manifest_audit.v1"
PROMPT_ROOT = "prompts/"
SCHEMA_ROOT = "prompts/distill/_output_schemas/"


@dataclass(frozen=True)
class Finding:
    rule: str
    path: str
    message: str


def _finding(rule: str, path: str, message: str) -> Finding:
    return Finding(rule=rule, path=path, message=message)


def _read_utf8_asset(
    path: Path,
    *,
    findings: list[Finding],
    rule: str,
    label: str,
) -> str | None:
    try:
        return read_native_bytes(path).decode("utf-8")
    except (OSError, UnicodeError) as exc:
        findings.append(
            _finding(rule, label, f"{type(exc).__name__}: asset is not readable UTF-8")
        )
        return None


def _git_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return sorted(item.decode("utf-8") for item in result.stdout.split(b"\0") if item)


def discover_tracked_markdown(repo_root: Path = REPO_ROOT) -> list[str]:
    return [path for path in _git_files(repo_root) if path.endswith(".md")]


def discover_prompt_assets(repo_root: Path = REPO_ROOT) -> list[str]:
    return [
        path
        for path in _git_files(repo_root)
        if path.startswith(PROMPT_ROOT)
        and (path.endswith(".md") or (path.startswith(SCHEMA_ROOT) and path.endswith(".json")))
    ]


def discover_desktop_root(repo_root: Path = REPO_ROOT) -> Path:
    candidates = [Path.home(), *repo_root.resolve(strict=False).parents]
    for base in candidates:
        candidate = base / "Desktop" / "mnemos系统图谱"
        if candidate.is_dir():
            return candidate
    return Path.home() / "Desktop" / "mnemos系统图谱"


def _load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(read_native_bytes(path).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("document asset manifest must be a JSON object")
    return payload


def _valid_exclusion_paths(
    payload: dict[str, Any],
    *,
    repo_root: Path,
    tracked_markdown: set[str],
    findings: list[Finding] | None = None,
) -> set[str]:
    valid: set[str] = set()
    exclusions = payload.get("exclusions", [])
    if not isinstance(exclusions, list):
        if findings is not None:
            findings.append(_finding("invalid_exclusions", "exclusions", "must be a list"))
        return valid
    for index, entry in enumerate(exclusions):
        label = f"exclusions[{index}]"
        if not isinstance(entry, dict):
            if findings is not None:
                findings.append(_finding("invalid_exclusion", label, "must be an object"))
            continue
        path = entry.get("path")
        owner = entry.get("owner")
        reason = entry.get("reason")
        expires_at = entry.get("expires_at")
        valid_fields = all(
            isinstance(value, str) and value.strip() for value in (path, owner, reason, expires_at)
        )
        expiry_ok = False
        if valid_fields:
            try:
                expiry_ok = date.fromisoformat(str(expires_at)) >= date.today()
            except ValueError:
                expiry_ok = False
        if (
            not valid_fields
            or not expiry_ok
            or path not in tracked_markdown
            or not (repo_root / str(path)).is_file()
        ):
            if findings is not None:
                findings.append(
                    _finding(
                        "invalid_exclusion",
                        label,
                        "requires tracked path, owner, reason, and unexpired ISO date",
                    )
                )
            continue
        valid.add(str(path))
    return valid


def discover_reviewed_markdown(
    repo_root: Path = REPO_ROOT,
    manifest_path: Path | None = None,
) -> list[Path]:
    manifest_path = manifest_path or repo_root / "docs/acceptance/document_asset_manifest.json"
    tracked = set(discover_tracked_markdown(repo_root))
    try:
        payload = _load_manifest(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return [repo_root / path for path in sorted(tracked)]
    excluded = _valid_exclusion_paths(
        payload,
        repo_root=repo_root,
        tracked_markdown=tracked,
    )
    return [repo_root / path for path in sorted(tracked - excluded)]


def _sha256(path: Path) -> str:
    return "sha256:" + regular_file_sha256(path)


def _defined_symbols(path: Path) -> set[str]:
    tree = ast.parse(read_native_bytes(path).decode("utf-8"), filename=str(path))
    symbols: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    symbols.add(target.id)
        elif isinstance(node, ast.ClassDef):
            symbols.add(node.name)
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.add(f"{node.name}.{child.name}")
    return symbols


def _validate_consumer(
    consumer: Any,
    *,
    repo_root: Path,
    contract_path: str,
    findings: list[Finding],
) -> None:
    if not isinstance(consumer, dict):
        findings.append(
            _finding("invalid_prompt_consumer", contract_path, "consumer must be object")
        )
        return
    path_value = consumer.get("path")
    symbol = consumer.get("symbol")
    if not isinstance(path_value, str) or not isinstance(symbol, str):
        findings.append(
            _finding("invalid_prompt_consumer", contract_path, "consumer needs path and symbol")
        )
        return
    source = repo_root / path_value
    if not source.is_file():
        findings.append(
            _finding("missing_prompt_consumer", contract_path, f"missing consumer {path_value}")
        )
        return
    try:
        symbols = _defined_symbols(source)
    except (OSError, UnicodeError, SyntaxError) as exc:
        findings.append(
            _finding("prompt_consumer_parse_error", path_value, f"{type(exc).__name__}: {exc}")
        )
        return
    if symbol not in symbols:
        findings.append(
            _finding(
                "missing_prompt_consumer_symbol",
                contract_path,
                f"{path_value} does not define {symbol}",
            )
        )


def _audit_prompt_contracts(
    payload: dict[str, Any],
    *,
    repo_root: Path,
    discovered: set[str],
    findings: list[Finding],
) -> int:
    raw_contracts = payload.get("prompt_contracts")
    if not isinstance(raw_contracts, list):
        findings.append(_finding("invalid_prompt_contracts", "prompt_contracts", "must be a list"))
        return 0
    contracts: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(raw_contracts):
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            findings.append(
                _finding("invalid_prompt_contract", f"prompt_contracts[{index}]", "missing path")
            )
            continue
        path = str(entry["path"])
        if path in contracts:
            findings.append(_finding("duplicate_prompt_contract", path, "path registered twice"))
            continue
        contracts[path] = entry
    for path in sorted(discovered - set(contracts)):
        findings.append(_finding("unregistered_prompt_asset", path, "prompt asset lacks contract"))
    for path in sorted(set(contracts) - discovered):
        findings.append(_finding("stale_prompt_contract", path, "contract path is not tracked"))
    referenced_schemas: set[str] = set()
    for path in sorted(discovered & set(contracts)):
        entry = contracts[path]
        asset = repo_root / path
        expected_hash = entry.get("sha256")
        if not isinstance(expected_hash, str) or expected_hash != _sha256(asset):
            findings.append(_finding("stale_prompt_hash", path, "committed SHA-256 does not match"))
        consumers = entry.get("consumers")
        if not isinstance(consumers, list) or not consumers:
            findings.append(
                _finding("missing_prompt_consumer", path, "consumers must be non-empty")
            )
        else:
            for consumer in consumers:
                _validate_consumer(
                    consumer,
                    repo_root=repo_root,
                    contract_path=path,
                    findings=findings,
                )
        consumer_specs = consumers if isinstance(consumers, list) else []
        consumer_paths = [
            str(consumer.get("path"))
            for consumer in consumer_specs
            if isinstance(consumer, dict) and isinstance(consumer.get("path"), str)
        ]
        consumer_text_parts: list[str] = []
        for consumer_path in consumer_paths:
            consumer_asset = repo_root / consumer_path
            if not consumer_asset.is_file():
                continue
            consumer_source = _read_utf8_asset(
                consumer_asset,
                findings=findings,
                rule="prompt_consumer_parse_error",
                label=consumer_path,
            )
            if consumer_source is not None:
                consumer_text_parts.append(consumer_source)
        consumer_text = "\n".join(consumer_text_parts)
        if path == "prompts/agent_onboarding.md" and "agent_onboarding.md" not in consumer_text:
            findings.append(
                _finding(
                    "missing_prompt_consumer_binding", path, "consumer lacks direct path binding"
                )
            )
        elif path.startswith("prompts/distill/document/") and path.endswith(".md"):
            name = Path(path).stem
            if f'_load_document_prompt("{name}")' not in consumer_text:
                findings.append(
                    _finding(
                        "missing_prompt_consumer_binding",
                        path,
                        f'consumer lacks _load_document_prompt("{name}")',
                    )
                )
        elif path.startswith("prompts/distill/") and path.endswith(".md"):
            symbols = {
                str(consumer.get("symbol"))
                for consumer in consumer_specs
                if isinstance(consumer, dict)
            }
            if "TemplateRegistry.select" not in symbols or 'rglob("*.md")' not in consumer_text:
                findings.append(
                    _finding(
                        "missing_prompt_consumer_binding",
                        path,
                        "template must bind to TemplateRegistry recursive loader and selector",
                    )
                )
        output_schema = entry.get("output_schema")
        output_contract = entry.get("output_contract")
        allowed_output_contracts = {
            "inline_json",
            "json_schema",
            "markdown",
            "runtime_text",
            "schema_definition",
        }
        if output_contract not in allowed_output_contracts:
            findings.append(
                _finding("invalid_prompt_output_contract", path, "unknown output contract")
            )
        if output_contract == "json_schema" and not isinstance(output_schema, str):
            findings.append(
                _finding("missing_prompt_schema", path, "json_schema contract requires schema path")
            )
        if output_contract != "json_schema" and output_schema is not None:
            findings.append(
                _finding(
                    "unexpected_prompt_schema",
                    path,
                    "only json_schema contracts may name output_schema",
                )
            )
        if output_schema is not None:
            if isinstance(output_schema, str):
                referenced_schemas.add(output_schema)
            if not isinstance(output_schema, str) or output_schema not in discovered:
                findings.append(
                    _finding("missing_prompt_schema", path, f"missing schema {output_schema!r}")
                )
            else:
                try:
                    schema = json.loads(
                        read_native_bytes(repo_root / output_schema).decode("utf-8")
                    )
                    if not isinstance(schema, dict):
                        raise ValueError("schema must be an object")
                except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                    findings.append(_finding("invalid_prompt_schema", output_schema, str(exc)))
        if path.endswith(".json"):
            try:
                schema = json.loads(read_native_bytes(asset).decode("utf-8"))
                if not isinstance(schema, dict):
                    raise ValueError("schema must be an object")
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                findings.append(_finding("invalid_prompt_schema", path, str(exc)))
    for schema_path in sorted(
        path for path in discovered if path.startswith(SCHEMA_ROOT) and path.endswith(".json")
    ):
        if schema_path not in referenced_schemas:
            findings.append(
                _finding(
                    "orphan_prompt_schema", schema_path, "no prompt contract references schema"
                )
            )
    return len(discovered & set(contracts))


def _audit_prompt_version(
    payload: dict[str, Any], *, repo_root: Path, findings: list[Finding]
) -> None:
    version = payload.get("prompt_version")
    if not isinstance(version, dict):
        findings.append(_finding("invalid_prompt_version", "prompt_version", "must be object"))
        return
    _validate_consumer(
        version,
        repo_root=repo_root,
        contract_path="prompt_version",
        findings=findings,
    )


def _audit_desktop_assets(
    payload: dict[str, Any],
    *,
    repo_root: Path,
    desktop_root: Path,
    current_commit: str,
    findings: list[Finding],
) -> tuple[int, int]:
    try:
        discovered = {path for path in _git_files(desktop_root) if path.endswith((".md", ".json"))}
    except (OSError, subprocess.CalledProcessError) as exc:
        findings.append(_finding("desktop_discovery_error", str(desktop_root), str(exc)))
        return 0, 0
    raw_assets = payload.get("desktop_assets")
    if not isinstance(raw_assets, list):
        findings.append(_finding("invalid_desktop_assets", "desktop_assets", "must be a list"))
        return len(discovered), 0
    contracts: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(raw_assets):
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            findings.append(
                _finding("invalid_desktop_asset", f"desktop_assets[{index}]", "missing path")
            )
            continue
        path = str(entry["path"])
        if path in contracts:
            findings.append(_finding("duplicate_desktop_asset", path, "path registered twice"))
            continue
        contracts[path] = entry
    for path in sorted(discovered - set(contracts)):
        findings.append(_finding("unclassified_desktop_asset", path, "asset lacks classification"))
    for path in sorted(set(contracts) - discovered):
        findings.append(_finding("stale_desktop_asset", path, "classified asset is not tracked"))
    allowed_classes = {"current_contract", "generated_index", "current_state_evidence"}
    for path in sorted(discovered & set(contracts)):
        entry = contracts[path]
        classification = entry.get("classification")
        evidence = entry.get("evidence")
        evidence_shape_ok = (
            isinstance(evidence, list)
            if classification == "current_contract"
            else isinstance(evidence, str) and bool(evidence)
        )
        if classification not in allowed_classes or not evidence_shape_ok:
            findings.append(
                _finding("invalid_desktop_classification", path, "invalid class or evidence")
            )
            continue
        asset = desktop_root / path
        if classification == "current_contract":
            text = _read_utf8_asset(
                asset,
                findings=findings,
                rule="unreadable_desktop_asset",
                label=path,
            )
            if text is None:
                continue
            refs = evidence if isinstance(evidence, list) else []
            valid_refs = (
                len(refs) >= 2
                and all(isinstance(ref, str) and ref for ref in refs)
                and any(str(ref).endswith("#current_state") for ref in refs)
                and any(not str(ref).endswith("#current_state") for ref in refs)
            )
            if not valid_refs:
                findings.append(
                    _finding(
                        "invalid_desktop_current_evidence",
                        path,
                        "current contract needs current_state plus at least one repo anchor",
                    )
                )
                continue
            missing_refs: list[str] = []
            marker_line = next(
                (line for line in text.splitlines() if line.startswith("Current claim evidence:")),
                "",
            )
            for ref_value in refs:
                ref = str(ref_value)
                ref_path = ref.split("#", 1)[0]
                target = (
                    desktop_root / ref_path
                    if ref.endswith("#current_state")
                    else repo_root / ref_path
                )
                if not target.exists() or f"`{ref}`" not in marker_line:
                    missing_refs.append(ref)
            if not marker_line or missing_refs:
                findings.append(
                    _finding(
                        "missing_desktop_current_evidence",
                        path,
                        f"missing evidence refs: {missing_refs}",
                    )
                )
        elif classification == "generated_index":
            text = _read_utf8_asset(
                asset,
                findings=findings,
                rule="unreadable_desktop_asset",
                label=path,
            )
            if text is None:
                continue
            header = "\n".join(text.splitlines()[:12])
            has_commit_header = "当前源码基线:" in header or "Current source commit:" in header
            if not has_commit_header or current_commit[:8] not in header:
                findings.append(
                    _finding(
                        "stale_desktop_generated_commit",
                        path,
                        "generated index does not name current repo commit",
                    )
                )
        else:
            try:
                facts = json.loads(read_native_bytes(asset).decode("utf-8"))
                recorded = facts.get("current_state", {}).get("repo_git_commit")
            except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
                recorded = None
            if recorded != current_commit:
                findings.append(
                    _finding(
                        "stale_desktop_current_state",
                        path,
                        "current_state.repo_git_commit does not match repo HEAD",
                    )
                )
    return len(discovered), len(discovered & set(contracts))


def _audit_desktop_manifest_shape(payload: dict[str, Any], findings: list[Finding]) -> None:
    assets = payload.get("desktop_assets")
    if not isinstance(assets, list) or not assets:
        findings.append(
            _finding("invalid_desktop_assets", "desktop_assets", "must be a non-empty list")
        )
        return
    seen: set[str] = set()
    allowed = {"current_contract", "generated_index", "current_state_evidence"}
    for index, entry in enumerate(assets):
        label = f"desktop_assets[{index}]"
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            findings.append(_finding("invalid_desktop_asset", label, "missing path"))
            continue
        path = str(entry["path"])
        if path in seen:
            findings.append(_finding("duplicate_desktop_asset", path, "path registered twice"))
        seen.add(path)
        classification = entry.get("classification")
        evidence = entry.get("evidence")
        evidence_ok = (
            isinstance(evidence, list) and len(evidence) >= 2
            if classification == "current_contract"
            else isinstance(evidence, str) and bool(evidence)
        )
        if classification not in allowed or not evidence_ok:
            findings.append(
                _finding("invalid_desktop_classification", path, "invalid class or evidence")
            )


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: Any,
    node: Any,
    deep: bool = False,
) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ValueError("frontmatter keys must be strings")
        if key in mapping:
            raise ValueError(f"duplicate frontmatter key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _frontmatter_payload(text: str) -> dict[str, Any]:
    if not text.startswith("---\n"):
        raise ValueError("missing first frontmatter block")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError("unterminated first frontmatter block")
    loader = _UniqueKeyLoader(text[4:end])
    try:
        payload = loader.get_single_data()
    except (ValueError, yaml.YAMLError) as exc:
        raise ValueError(f"invalid first frontmatter block: {exc}") from exc
    finally:
        loader.dispose()
    if not isinstance(payload, dict):
        raise ValueError("first frontmatter block must be a mapping")
    return payload


def _audit_external_governing_assets(
    payload: dict[str, Any],
    *,
    repo_root: Path,
    desktop_root: Path,
    include_desktop: bool,
    findings: list[Finding],
) -> tuple[int, str | None, set[str], set[str]]:
    raw_assets = payload.get("external_governing_assets", [])
    if not isinstance(raw_assets, list):
        findings.append(
            _finding(
                "invalid_external_governing_assets",
                "external_governing_assets",
                "must be a list",
            )
        )
        return 0, None, set(), set()
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    active_owner_count = 0
    active_asset_id: str | None = None
    supersedes: set[str] = set()
    predecessor_ids: set[str] = set()
    reviewed = 0
    for index, entry in enumerate(raw_assets):
        label = f"external_governing_assets[{index}]"
        if not isinstance(entry, dict):
            findings.append(_finding("invalid_external_governing_asset", label, "must be object"))
            continue
        asset_id = entry.get("asset_id")
        path_value = entry.get("path")
        anchors = entry.get("required_anchors")
        current_generation_roots = entry.get("required_current_root_generations")
        supersedes_value = entry.get("supersedes")
        renamed_from = entry.get("renamed_from")
        valid = (
            isinstance(asset_id, str)
            and asset_id.startswith("desktop:")
            and isinstance(path_value, str)
            and bool(path_value)
            and Path(path_value).name == path_value
            and entry.get("profile") == "phase0_required"
            and entry.get("classification") == "governing_contract"
            and entry.get("governance_role") == "current_active"
            and isinstance(supersedes_value, list)
            and all(isinstance(item, str) and item for item in supersedes_value)
            and len(supersedes_value) == len(set(supersedes_value))
            and isinstance(renamed_from, dict)
            and isinstance(renamed_from.get("asset_id"), str)
            and renamed_from["asset_id"].startswith("desktop:")
            and isinstance(renamed_from.get("path"), str)
            and Path(renamed_from["path"]).name == renamed_from["path"]
            and isinstance(renamed_from.get("sha256"), str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", renamed_from["sha256"]) is not None
            and renamed_from["asset_id"] in supersedes_value
            and renamed_from["asset_id"] != asset_id
            and isinstance(anchors, list)
            and bool(anchors)
            and all(isinstance(anchor, str) and anchor for anchor in anchors)
            and isinstance(current_generation_roots, list)
            and bool(current_generation_roots)
            and len(current_generation_roots) == len(set(current_generation_roots))
            and all(
                isinstance(root_id, str)
                and re.fullmatch(r"COG-[0-9]{3}", root_id) is not None
                for root_id in current_generation_roots
            )
            and entry.get("final_byte_hash_owner") == "detached_closure_bundle_only"
        )
        if not valid:
            findings.append(
                _finding(
                    "invalid_external_governing_asset",
                    label,
                    "requires exact phase0 profile, class, Desktop basename, anchors and hash owner",
                )
            )
            continue
        asset_id = cast(str, asset_id)
        path_value = cast(str, path_value)
        anchors = cast(list[Any], anchors)
        current_generation_roots = cast(
            list[Any],
            current_generation_roots,
        )
        supersedes_value = cast(list[Any], supersedes_value)
        renamed_from = cast(dict[str, Any], renamed_from)
        if asset_id in seen_ids or path_value in seen_paths:
            findings.append(
                _finding("duplicate_external_governing_asset", path_value, "duplicate id or path")
            )
            continue
        seen_ids.add(asset_id)
        seen_paths.add(path_value)
        active_owner_count += 1
        active_asset_id = asset_id
        supersedes = set(supersedes_value)
        predecessor_ids = {str(renamed_from["asset_id"])}
        if include_desktop:
            asset = desktop_root.parent / path_value
            if not asset.is_file():
                findings.append(
                    _finding("missing_external_governing_asset", path_value, "Desktop file missing")
                )
                continue
            text = _read_utf8_asset(
                asset,
                findings=findings,
                rule="unreadable_external_governing_asset",
                label=path_value,
            )
            if text is None:
                continue
            try:
                frontmatter = _frontmatter_payload(text)
            except ValueError:
                frontmatter = {}
            expected_frontmatter = {
                "status": "ACTIVE",
                "governance_role": "current_active",
                "authority": "SOLE_GOVERNING_CONTRACT",
                "asset_id": asset_id,
                "supersedes": supersedes_value,
                "root_definition_owner": "THIS_DOCUMENT",
                "root_history_owner": "THIS_DOCUMENT",
                "current_index_policy": "GENERATED_ONLY",
                "final_byte_hash_owner": "DETACHED_CLOSURE_BUNDLE_ONLY",
                "renamed_from": renamed_from,
            }
            if any(frontmatter.get(key) != value for key, value in expected_frontmatter.items()):
                findings.append(
                    _finding(
                        "invalid_external_governing_authority_header",
                        path_value,
                        "active authority must be declared in the first frontmatter block",
                    )
                )
                continue
            predecessor_path = desktop_root.parent / str(renamed_from["path"])
            if predecessor_path.exists():
                findings.append(
                    _finding(
                        "active_external_governing_predecessor",
                        str(renamed_from["path"]),
                        "renamed predecessor must remain absent and non-gating",
                    )
                )
                continue
            missing = [str(anchor) for anchor in anchors if str(anchor) not in text]
            if missing:
                findings.append(
                    _finding(
                        "missing_external_governing_anchor",
                        path_value,
                        f"missing anchors: {missing}",
                    )
                )
                continue
            closure_path = (
                repo_root
                / "docs"
                / "acceptance"
                / "cognitive_root_closures.jsonl"
            )
            try:
                closure_records = [
                    json.loads(line)
                    for line in read_native_bytes(closure_path).decode("utf-8").splitlines()
                    if line.strip()
                ]
            except (OSError, UnicodeError, json.JSONDecodeError):
                findings.append(
                    _finding(
                        "unreadable_external_governing_generation",
                        path_value,
                        "current closure projection is unreadable",
                    )
                )
                continue
            generation_anchors: list[str] = []
            generation_invalid = False
            for root_id in current_generation_roots:
                matches = [
                    record
                    for record in closure_records
                    if isinstance(record, dict) and record.get("root_id") == root_id
                ]
                artifact = matches[0].get("machine_artifact") if len(matches) == 1 else None
                if (
                    not isinstance(artifact, str)
                    or artifact.count("#") != 1
                    or not artifact.rsplit("#", 1)[1]
                ):
                    generation_invalid = True
                    break
                generation_anchors.append(artifact.rsplit("#", 1)[1])
            if generation_invalid:
                findings.append(
                    _finding(
                        "unreadable_external_governing_generation",
                        path_value,
                        "configured current Root generation is missing or ambiguous",
                    )
                )
                continue
            missing_generations = [
                generation for generation in generation_anchors if generation not in text
            ]
            if missing_generations:
                findings.append(
                    _finding(
                        "stale_external_governing_generation",
                        path_value,
                        f"missing current machine generations: {missing_generations}",
                    )
                )
                continue
        reviewed += 1
    if active_owner_count != 1:
        findings.append(
            _finding(
                "external_governing_owner_count",
                "external_governing_assets",
                f"expected exactly one active governing contract, found {active_owner_count}",
            )
        )
    return reviewed, active_asset_id, supersedes, predecessor_ids


def _audit_external_historical_assets(
    payload: dict[str, Any],
    *,
    desktop_root: Path,
    include_desktop: bool,
    active_asset_id: str | None,
    supersedes: set[str],
    predecessor_ids: set[str],
    findings: list[Finding],
) -> int:
    raw_assets = payload.get("external_historical_assets", [])
    if not isinstance(raw_assets, list):
        findings.append(
            _finding(
                "invalid_external_historical_assets",
                "external_historical_assets",
                "must be a list",
            )
        )
        return 0
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    reviewed = 0
    for index, entry in enumerate(raw_assets):
        label = f"external_historical_assets[{index}]"
        if not isinstance(entry, dict):
            findings.append(_finding("invalid_external_historical_asset", label, "must be object"))
            continue
        asset_id = entry.get("asset_id")
        path_value = entry.get("path")
        anchors = entry.get("required_anchors")
        frozen_sha256 = entry.get("frozen_sha256")
        valid = (
            isinstance(asset_id, str)
            and asset_id.startswith("desktop:")
            and isinstance(path_value, str)
            and bool(path_value)
            and Path(path_value).name == path_value
            and entry.get("profile") == "historical_reference"
            and entry.get("classification") == "historical_source"
            and entry.get("governance_role") == "historical_provenance"
            and entry.get("gate_eligible") is False
            and isinstance(entry.get("superseded_by"), str)
            and isinstance(anchors, list)
            and bool(anchors)
            and all(isinstance(anchor, str) and anchor for anchor in anchors)
            and isinstance(frozen_sha256, str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", frozen_sha256) is not None
        )
        if not valid:
            findings.append(
                _finding(
                    "invalid_external_historical_asset",
                    label,
                    "requires exact historical role, supersession, anchors and frozen hash",
                )
            )
            continue
        assert isinstance(asset_id, str)
        assert isinstance(path_value, str)
        assert isinstance(anchors, list)
        assert isinstance(frozen_sha256, str)
        if asset_id in seen_ids or path_value in seen_paths:
            findings.append(
                _finding("duplicate_external_historical_asset", path_value, "duplicate id or path")
            )
            continue
        seen_ids.add(asset_id)
        seen_paths.add(path_value)
        if entry["superseded_by"] != active_asset_id or asset_id not in supersedes:
            findings.append(
                _finding(
                    "invalid_external_historical_supersession",
                    path_value,
                    "historical source must point to, and be named by, the single active contract",
                )
            )
            continue
        if include_desktop:
            asset = desktop_root.parent / path_value
            if not asset.is_file():
                findings.append(
                    _finding(
                        "missing_external_historical_asset", path_value, "Desktop file missing"
                    )
                )
                continue
            text = _read_utf8_asset(
                asset,
                findings=findings,
                rule="unreadable_external_historical_asset",
                label=path_value,
            )
            if text is None:
                continue
            try:
                frontmatter = _frontmatter_payload(text)
            except ValueError:
                frontmatter = {}
            expected_frontmatter = {
                "status": "SUPERSEDED_HISTORICAL_EVIDENCE",
                "governance_role": "historical_provenance",
                "asset_id": asset_id,
                "gate_eligible": False,
                "superseded_by": active_asset_id,
                "authority_for_current_state": "NONE",
                "mutation_policy": "FROZEN",
            }
            if any(frontmatter.get(key) != value for key, value in expected_frontmatter.items()):
                findings.append(
                    _finding(
                        "invalid_external_historical_authority_header",
                        path_value,
                        "historical authority must be declared in the first frontmatter block",
                    )
                )
                continue
            missing = [str(anchor) for anchor in anchors if str(anchor) not in text]
            if missing:
                findings.append(
                    _finding(
                        "missing_external_historical_anchor",
                        path_value,
                        f"missing anchors: {missing}",
                    )
                )
                continue
            try:
                actual_sha256 = "sha256:" + regular_file_sha256(asset)
            except (DurableIOError, OSError):
                findings.append(
                    _finding(
                        "unreadable_external_historical_asset",
                        path_value,
                        "historical asset changed during hash verification",
                    )
                )
                continue
            if frozen_sha256 != actual_sha256:
                findings.append(
                    _finding(
                        "stale_external_historical_hash",
                        path_value,
                        "frozen historical source hash does not match Desktop bytes",
                    )
                )
                continue
        reviewed += 1
    if seen_ids != supersedes - predecessor_ids:
        findings.append(
            _finding(
                "invalid_external_historical_supersession",
                "external_historical_assets",
                "active supersedes set must exactly match historical source ids",
            )
        )
    return reviewed


def audit_assets(
    *,
    repo_root: Path,
    manifest_path: Path,
    desktop_root: Path,
    current_commit: str,
    include_desktop: bool = True,
) -> dict[str, Any]:
    findings: list[Finding] = []
    try:
        payload = _load_manifest(manifest_path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        findings.append(_finding("manifest_load_error", str(manifest_path), str(exc)))
        payload = {}
    if payload.get("schema_version") != SCHEMA_VERSION:
        findings.append(
            _finding("invalid_manifest_schema", str(manifest_path), f"expected {SCHEMA_VERSION}")
        )
    tracked_markdown = set(discover_tracked_markdown(repo_root))
    exclusions = _valid_exclusion_paths(
        payload,
        repo_root=repo_root,
        tracked_markdown=tracked_markdown,
        findings=findings,
    )
    discovered_prompts = set(discover_prompt_assets(repo_root))
    prompt_reviewed = _audit_prompt_contracts(
        payload,
        repo_root=repo_root,
        discovered=discovered_prompts,
        findings=findings,
    )
    _audit_prompt_version(payload, repo_root=repo_root, findings=findings)
    desktop_skipped = not include_desktop
    _audit_desktop_manifest_shape(payload, findings)
    external_reviewed, active_asset_id, supersedes, predecessor_ids = (
        _audit_external_governing_assets(
            payload,
            repo_root=repo_root,
            desktop_root=desktop_root,
            include_desktop=include_desktop,
            findings=findings,
        )
    )
    historical_reviewed = _audit_external_historical_assets(
        payload,
        desktop_root=desktop_root,
        include_desktop=include_desktop,
        active_asset_id=active_asset_id,
        supersedes=supersedes,
        predecessor_ids=predecessor_ids,
        findings=findings,
    )
    if include_desktop:
        desktop_discovered, desktop_reviewed = _audit_desktop_assets(
            payload,
            repo_root=repo_root,
            desktop_root=desktop_root,
            current_commit=current_commit,
            findings=findings,
        )
    else:
        desktop_discovered = 0
        desktop_reviewed = 0
    counts = {
        "repo_markdown_discovered": len(tracked_markdown),
        "repo_markdown_reviewed": len(tracked_markdown - exclusions),
        "repo_markdown_excluded": len(exclusions),
        "prompt_assets_discovered": len(discovered_prompts),
        "prompt_assets_reviewed": prompt_reviewed,
        "desktop_assets_discovered": desktop_discovered,
        "desktop_assets_reviewed": desktop_reviewed,
        "external_governing_assets_reviewed": external_reviewed,
        "external_historical_assets_reviewed": historical_reviewed,
        "unverified": len(findings),
    }
    by_rule = {
        rule: sum(1 for finding in findings if finding.rule == rule)
        for rule in sorted({finding.rule for finding in findings})
    }
    return {
        "schema": REPORT_SCHEMA,
        "ok": not findings,
        "desktop_skipped": desktop_skipped,
        "counts": counts,
        "by_rule": by_rule,
        "findings": [asdict(finding) for finding in findings],
    }


def _current_commit(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--manifest-path", type=Path)
    parser.add_argument("--desktop-root", type=Path)
    parser.add_argument(
        "--desktop-mode",
        choices=("auto", "required", "skip"),
        default="auto",
        help="Auto-audit an available map, require it for release, or skip it in remote CI.",
    )
    parser.add_argument("--strict", action="store_true", help="Exit non-zero on findings.")
    parser.add_argument("--json", action="store_true", help="Emit JSON report.")
    args = parser.parse_args(argv)
    manifest_path = args.manifest_path or (
        args.repo_root / "docs/acceptance/document_asset_manifest.json"
    )
    try:
        desktop_root = args.desktop_root or discover_desktop_root(args.repo_root)
        include_desktop = args.desktop_mode == "required" or (
            args.desktop_mode == "auto" and desktop_root.is_dir()
        )
        if args.desktop_mode == "required" and not desktop_root.is_dir():
            raise OSError(f"required Desktop system map missing: {desktop_root}")
        report = audit_assets(
            repo_root=args.repo_root,
            manifest_path=manifest_path,
            desktop_root=desktop_root,
            current_commit=_current_commit(args.repo_root),
            include_desktop=include_desktop,
        )
    except (OSError, UnicodeError, subprocess.CalledProcessError, ValueError) as exc:
        report = {
            "schema": REPORT_SCHEMA,
            "ok": False,
            "counts": {"unverified": 1},
            "by_rule": {"audit_error": 1},
            "findings": [asdict(_finding("audit_error", str(args.repo_root), str(exc)))],
        }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif report["ok"]:
        counts = report["counts"]
        print(
            "Document asset manifest audit passed: "
            f"repo_md={counts['repo_markdown_reviewed']}, "
            f"prompt={counts['prompt_assets_reviewed']}, "
            f"desktop={counts['desktop_assets_reviewed']}, unverified=0."
        )
    else:
        print("Document asset manifest audit found issue(s):")
        for finding in report["findings"]:
            print(f"- {finding['path']} [{finding['rule']}] {finding['message']}")
    return 1 if args.strict and not report["ok"] else 0


if __name__ == "__main__":
    sys.exit(main())
