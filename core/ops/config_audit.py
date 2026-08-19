"""Strict, redacted configuration audit for one-pass deployment checks."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from core.privacy.redaction import redact_key_source, redact_path, redact_sensitive_data
from core.privacy.redaction import redact_url
from core.privacy.secret_inventory import build_secret_inventory

SCHEMA_VERSION = "mnemos.config_audit.v1"
OK_STATUSES = {"ok", "configured", "disabled", "optional", "accepted"}


@dataclass(frozen=True)
class ConfigAuditItem:
    item_id: str
    category: str
    status: str
    required: bool
    summary: str
    repair_action: str
    evidence: Mapping[str, Any]


def _cfg_get(config: Any, key: str, default: Any = None) -> Any:
    getter = getattr(config, "get", None)
    if callable(getter):
        return getter(key, default)
    data = getattr(config, "_data", {})
    current: Any = data
    for part in key.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return default
        current = current[part]
    return current


def _path_value(value: Any) -> Path | None:
    if value is None:
        return None
    try:
        return Path(value).expanduser()
    except TypeError:
        return None


def _secret_source_status(source: str) -> str:
    if source.startswith(("env:", "keyring:", "keyref:")):
        return "reference"
    if source in {"missing", ""}:
        return "missing"
    if source.startswith("invalid:"):
        return "plaintext-risk"
    if source.startswith("config:"):
        return "plaintext-risk"
    return "plaintext-risk"


def _summarize_model(
    kind: str, api_cfg: Any, *, required: bool, show_sensitive: bool = False
) -> ConfigAuditItem:
    provider = str(getattr(api_cfg, "provider", "") or "")
    base_url = str(getattr(api_cfg, "base_url", "") or "")
    model = str(getattr(api_cfg, "model", "") or "")
    source = str(getattr(api_cfg, "source", "") or "")
    configured = bool(getattr(api_cfg, "configured", False)) and bool(provider and base_url and model)
    source_status = _secret_source_status(source)
    if configured and source_status == "reference":
        status = "configured"
    elif configured:
        status = "plaintext-risk"
    else:
        status = "missing" if required else "optional"
    return ConfigAuditItem(
        item_id=f"model.{kind}",
        category="models",
        status=status,
        required=required,
        summary=f"{kind} endpoint {status}",
        repair_action=f"Configure {kind} provider/base_url/model and env: or keyring: api key source.",
        evidence={
            "provider": provider,
            "base_url": base_url if show_sensitive else redact_url(base_url),
            "model": model,
            "key_source": (
                source
                if show_sensitive and source_status != "plaintext-risk"
                else redact_key_source(source)
                if source_status != "plaintext-risk"
                else source_status
            ),
            "key_source_status": source_status,
        },
    )


def _path_item(
    item_id: str,
    label: str,
    path: Path | None,
    *,
    required: bool,
    show_sensitive: bool = False,
) -> ConfigAuditItem:
    exists = bool(path and path.exists())
    status = "ok" if exists else "missing"
    return ConfigAuditItem(
        item_id=item_id,
        category="paths",
        status=status,
        required=required,
        summary=f"{label} path {'exists' if exists else 'is missing'}",
        repair_action=f"Create or configure {label} path.",
        evidence={
            "path": str(path) if show_sensitive and path else redact_path(path) if path else "",
            "exists": exists,
        },
    )


def _plaintext_secret_items(config: Any) -> list[ConfigAuditItem]:
    data = getattr(config, "_data", {})
    items = []
    inventory = build_secret_inventory(data if isinstance(data, Mapping) else {})
    for finding in inventory.get("findings", []):
        if finding.get("status") != "plaintext-risk":
            continue
        path = str(finding.get("path", ""))
        items.append(
            ConfigAuditItem(
                item_id=f"secret.{path}",
                category="secrets",
                status="plaintext-risk",
                required=True,
                summary=f"{path} contains a non-reference secret-like value",
                repair_action=f"Move {path} to env:VAR, keyring:REF, or keyref:REF.",
                evidence={
                    "path": path,
                    "value_length": int(finding.get("value_length", 0) or 0),
                },
            )
        )
    return items


def _l1_storage_item(config: Any) -> ConfigAuditItem:
    token = str(getattr(config, "l1_storage_token", "") or "")
    api_url = str(getattr(config, "l1_storage_api_url", "") or "")
    enabled = bool(getattr(config, "l1_storage_enabled", False))
    has_legacy = bool(token or api_url or enabled)
    return ConfigAuditItem(
        item_id="legacy.l1_storage",
        category="legacy",
        status="legacy-risk" if has_legacy else "ok",
        required=True,
        summary=(
            "legacy external L1 storage is still configured"
            if has_legacy
            else "legacy external L1 storage is absent"
        ),
        repair_action="Migrate external L1 token/api_url to current raw vault storage and remove legacy token.",
        evidence={
            "enabled": enabled,
            "api_url_configured": bool(api_url),
            "token_configured": bool(token),
        },
    )


def _disk_budget_config_item(config: Any, *, strict: bool) -> ConfigAuditItem:
    required_keys = {
        "sqlite_wal_file_max_mb",
        "sqlite_wal_total_max_mb",
        "temp_total_max_mb",
        "temp_stale_minutes",
        "snapshot_total_max_mb",
        "snapshot_growth_max_mb_per_day",
        "raw_events_max_mb",
        "raw_events_growth_max_mb_per_day",
        "growth_sample_min_seconds",
    }
    raw = _cfg_get(config, "storage.disk_budget", {}) or {}
    budget = raw if isinstance(raw, Mapping) else {}
    missing = sorted(required_keys - set(budget))
    invalid = []
    for key in sorted(required_keys & set(budget)):
        try:
            if float(budget[key]) <= 0:
                invalid.append(key)
        except (TypeError, ValueError):
            invalid.append(key)
    status = "ok" if not missing and not invalid else "missing"
    return ConfigAuditItem(
        item_id="storage.disk_budget",
        category="storage",
        status=status,
        required=bool(strict),
        summary="SQLite/WAL/temp/snapshot/raw_events disk budget config",
        repair_action="Restore storage.disk_budget from config/config.example.json.",
        evidence={"missing": missing, "invalid": invalid},
    )


def _retention_items(config: Any) -> list[ConfigAuditItem]:
    retention = _cfg_get(config, "storage.retention_days", {}) or {}
    items = []
    if not isinstance(retention, Mapping):
        return [
            ConfigAuditItem(
                item_id="retention.storage",
                category="retention",
                status="missing",
                required=True,
                summary="storage.retention_days is not a mapping",
                repair_action="Restore storage.retention_days from config examples.",
                evidence={},
            )
        ]
    for key, value in sorted(retention.items()):
        ok = isinstance(value, int) and value > 0
        items.append(
            ConfigAuditItem(
                item_id=f"retention.{key}",
                category="retention",
                status="ok" if ok else "missing",
                required=True,
                summary=f"retention for {key}",
                repair_action=f"Set storage.retention_days.{key} to a positive integer.",
                evidence={"days": value if isinstance(value, int) else None},
            )
        )
    return items


def _daemon_items(config: Any) -> list[ConfigAuditItem]:
    required_services = ("capture_worker", "raw_projection", "distill_and_merge", "heartbeat", "eventbus")
    items = []
    for service in required_services:
        enabled = bool(_cfg_get(config, f"daemon.services.{service}", False))
        items.append(
            ConfigAuditItem(
                item_id=f"daemon.{service}",
                category="daemon",
                status="ok" if enabled else "missing",
                required=True,
                summary=f"daemon service {service} {'enabled' if enabled else 'disabled'}",
                repair_action=f"Enable daemon.services.{service} or document why this deployment disables it.",
                evidence={"enabled": enabled},
            )
        )
    return items


def _security_items(
    config: Any, *, show_sensitive: bool = False
) -> list[ConfigAuditItem]:
    from scripts.health_check import check_security

    security = check_security(config=config)
    secret_inventory = security.get("secret_inventory", {})
    plaintext_findings = [
        finding
        for finding in secret_inventory.get("findings", [])
        if finding.get("status") == "plaintext-risk"
    ]
    inventory_error = bool(secret_inventory.get("error"))
    plaintext_count = int(secret_inventory.get("plaintext_count", len(plaintext_findings)) or 0)
    items = [
        ConfigAuditItem(
            item_id="security.permissions",
            category="secrets",
            status="ok" if not security.get("permission_violations") else "plaintext-risk",
            required=True,
            summary="sensitive config/database path permissions",
            repair_action="Apply chmod repair actions from security health.",
            evidence={
                "violation_count": len(security.get("permission_violations", [])),
                "repair_actions": (
                    security.get("repair_actions", [])
                    if show_sensitive
                    else redact_sensitive_data(security.get("repair_actions", []))
                ),
            },
        ),
        ConfigAuditItem(
            item_id="security.secret_inventory",
            category="secrets",
            status="ok" if plaintext_count == 0 and not inventory_error else "plaintext-risk",
            required=True,
            summary="config file secret inventory scan",
            repair_action="Replace plaintext secret-like values with env:/keyring:/keyref: references.",
            evidence={
                "schema_version": secret_inventory.get("schema_version"),
                "plaintext_count": plaintext_count,
                "reference_count": int(secret_inventory.get("reference_count", 0) or 0),
                "plaintext_paths": [
                    str(finding.get("path", ""))
                    for finding in plaintext_findings[:20]
                ],
                "error": secret_inventory.get("error"),
            },
        ),
    ]
    legacy = security.get("legacy_key_rows", {})
    legacy_risk = int(legacy.get("enc_rows", 0) or 0) + int(legacy.get("plaintext_rows", 0) or 0)
    items.append(
        ConfigAuditItem(
            item_id="security.legacy_credential_rows",
            category="legacy",
            status="legacy-risk" if legacy_risk else "ok",
            required=True,
            summary="legacy credential_pool key rows",
            repair_action="Migrate credential_pool enc/plaintext rows to key references.",
            evidence={
                "enc_rows": int(legacy.get("enc_rows", 0) or 0),
                "plaintext_rows": int(legacy.get("plaintext_rows", 0) or 0),
                "keyref_rows": int(legacy.get("keyref_rows", 0) or 0),
            },
        )
    )
    keyring = security.get("keyring", {})
    if not isinstance(keyring, Mapping):
        keyring = {}
    keyring_backend = keyring.get("keyring", {})
    if not isinstance(keyring_backend, Mapping):
        keyring_backend = {
            "available": security.get("keyring_available"),
            "backend": security.get("keyring_backend"),
            "error": security.get("keyring_error"),
        }
    keyring_repair_actions = keyring.get("repair_actions", [])
    if not isinstance(keyring_repair_actions, list):
        keyring_repair_actions = []
    items.append(
        ConfigAuditItem(
            item_id="security.keyring",
            category="secrets",
            status=str(
                keyring.get(
                    "status",
                    "ok" if keyring_backend.get("available") else "optional",
                )
            ),
            required=False,
            summary=str(keyring.get("summary", "keyring backend availability")),
            repair_action=(
                "; ".join(str(action) for action in keyring_repair_actions[:4])
                or "Install or authorize a keyring backend, or explicitly accept env fallback."
            ),
            evidence={
                "schema_version": keyring.get("schema_version"),
                "available": bool(keyring_backend.get("available")),
                "backend": keyring_backend.get("backend"),
                "error": keyring_backend.get("error"),
                "env_fallback_accepted": bool(
                    keyring.get("env_fallback_accepted", False)
                ),
                "risk_level": keyring.get("risk_level"),
                "safe_but_not_best": bool(keyring.get("safe_but_not_best", False)),
                "requires_user_choice": bool(
                    keyring.get("requires_user_choice", False)
                ),
                "secret_reference_counts": keyring.get("secret_reference_counts", {}),
                "secret_inventory_plaintext_count": int(
                    keyring.get("secret_inventory_plaintext_count", 0) or 0
                ),
            },
        )
    )
    return items


def _migration_items(
    config: Any, *, show_sensitive: bool = False
) -> list[ConfigAuditItem]:
    from core.migrations.registry import MigrationLedger, MigrationRegistry

    try:
        plan = MigrationRegistry().plan(config)
        item = next(
            item
            for item in plan.items
            if item.migration_id == "config.stale_keys.v1"
        )
        stale_keys = tuple(item.stale_keys)
        return [
            ConfigAuditItem(
                item_id="legacy.config_stale_keys",
                category="legacy",
                status="legacy-risk" if stale_keys else "ok",
                required=True,
                summary=(
                    "stale config keys require migration"
                    if stale_keys
                    else "no stale config keys detected"
                ),
                repair_action="Run: mnemos migrate apply config.stale_keys.v1 --json",
                evidence={
                    "migration_id": "config.stale_keys.v1",
                    "stale_key_count": len(stale_keys),
                    "stale_keys": list(stale_keys),
                    "ledger_path": (
                        str(MigrationLedger.from_config(config).db_path)
                        if show_sensitive
                        else redact_path(MigrationLedger.from_config(config).db_path)
                    ),
                },
            )
        ]
    except (
        ImportError,
        KeyError,
        StopIteration,
        OSError,
        ValueError,
        TypeError,
        RuntimeError,
        sqlite3.Error,
    ) as exc:
        return [
            ConfigAuditItem(
                item_id="legacy.config_stale_keys",
                category="legacy",
                status="missing",
                required=True,
                summary="stale config migration plan could not be evaluated",
                repair_action="Run: mnemos migrate status --json and inspect the migration registry.",
                evidence={"error": exc.__class__.__name__},
            )
        ]


def build_config_audit_report(
    config: Any, *, strict: bool = False, show_sensitive: bool = False
) -> dict[str, Any]:
    from core.llm_config import (
        resolve_embedding_api_config,
        resolve_effective_llm_api_config,
        resolve_multimodal_api_config,
        resolve_reranker_api_config,
    )

    items: list[ConfigAuditItem] = [
        _summarize_model(
            "llm",
            resolve_effective_llm_api_config(config),
            required=bool(strict),
            show_sensitive=show_sensitive,
        ),
        _summarize_model(
            "embedding",
            resolve_embedding_api_config(config),
            required=bool(strict),
            show_sensitive=show_sensitive,
        ),
        _summarize_model(
            "reranker",
            resolve_reranker_api_config(config),
            required=bool(strict),
            show_sensitive=show_sensitive,
        ),
        _summarize_model(
            "multimodal",
            resolve_multimodal_api_config(config),
            required=False,
            show_sensitive=show_sensitive,
        ),
        _path_item(
            "path.database_dir",
            "database_dir",
            getattr(config, "database_dir", None),
            required=True,
            show_sensitive=show_sensitive,
        ),
        _path_item(
            "path.mnemos_vault",
            "mnemos vault",
            _path_value(_call_vault_dir(config, "mnemos")),
            required=True,
            show_sensitive=show_sensitive,
        ),
        _path_item(
            "path.raw_vault",
            "raw vault",
            _path_value(_call_vault_dir(config, "raw")),
            required=True,
            show_sensitive=show_sensitive,
        ),
        _l1_storage_item(config),
        _disk_budget_config_item(config, strict=strict),
    ]
    items.extend(_migration_items(config, show_sensitive=show_sensitive))
    items.extend(_retention_items(config))
    items.extend(_daemon_items(config))
    items.extend(_security_items(config, show_sensitive=show_sensitive))
    items.extend(_plaintext_secret_items(config))

    serialized = [
        item.__dict__ if show_sensitive else redact_sensitive_data(item.__dict__)
        for item in items
    ]
    required_failed = [
        item.item_id
        for item in items
        if item.required and item.status not in OK_STATUSES
    ]
    categories: dict[str, dict[str, int]] = {}
    for item in items:
        bucket = categories.setdefault(item.category, {"total": 0, "failed": 0})
        bucket["total"] += 1
        if item.required and item.status not in OK_STATUSES:
            bucket["failed"] += 1
    actual_artifact_path = (
        Path(getattr(config, "database_dir", Path.home() / ".mnemos"))
        / "config_audit.json"
    )
    artifact_path = (
        str(actual_artifact_path)
        if show_sensitive
        else redact_path(actual_artifact_path)
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "strict": bool(strict),
        "ok": not required_failed,
        "required_failed": required_failed,
        "artifact_path": artifact_path,
        "categories": categories,
        "items": serialized,
    }


def _call_vault_dir(config: Any, name: str) -> Any:
    method = getattr(config, "vault_dir", None)
    if callable(method):
        try:
            return method(name)
        except (KeyError, TypeError, ValueError, OSError):
            return None
    return None


def write_config_audit_artifact(report: Mapping[str, Any], config: Any) -> Path:
    from core.utils import secure_file

    path = Path(config.database_dir) / "config_audit.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    secure_file(path)
    return path


def format_config_audit_text(report: Mapping[str, Any]) -> str:
    lines = [
        "Mnemos Config Audit",
        "=" * 40,
        f"schema: {report['schema_version']}",
        f"strict: {report['strict']}",
        f"ok: {report['ok']}",
        f"artifact: {report['artifact_path']}",
    ]
    failures = report.get("required_failed", [])
    if failures:
        lines.append("required_failed:")
        lines.extend(f"  - {item}" for item in failures)
    lines.append("items:")
    for item in report.get("items", []):
        lines.append(
            f"  - {item['item_id']}: {item['status']} ({item['summary']})"
        )
    return "\n".join(lines) + "\n"
