# -*- coding: utf-8 -*-
"""Failure persistence and event helpers for distillation."""

from __future__ import annotations

import json
import logging
import hashlib
import sqlite3
import uuid
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.file_ops import sha256_file
from core.hephaestus.distillation_models import KnowledgeFragment
from core.utils import atomic_write_text, read_text_value
from core.privacy.content_redaction import redact_persistence_value
from core.wiki_projection_publisher import publish_wiki_page_updated  # noqa: F401

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DistillationFailureRecord:
    """The durable artifact and canonical incident created for one failure."""

    artifact_path: Path
    incident: Any


def _sha256_file(path: Path) -> str:
    return "sha256:" + sha256_file(path)


def _sha256_json(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical_hash(
    value: Any,
    *,
    missing_label: str,
    derived_contract: Any | None = None,
) -> str:
    text = str(value or "").strip().lower()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", text):
        return text
    if re.fullmatch(r"[0-9a-f]{64}", text):
        return f"sha256:{text}"
    if derived_contract is not None:
        return _sha256_json(derived_contract)
    return f"missing:{missing_label}"


def _distill_error_codes(
    errors: List[str],
    parse_metadata: Optional[Dict[str, Any]],
) -> tuple[str, ...]:
    """Classify variable error text into stable operational codes."""

    metadata = parse_metadata or {}
    codes: set[str] = set()
    if metadata.get("transport_empty"):
        codes.add("transport_empty")
    failure_path = str(metadata.get("failure_path") or metadata.get("path") or "").lower()
    joined = "\n".join(str(error).lower() for error in errors)
    observed = f"{failure_path}\n{joined}"
    rules = (
        ("provider_failure", ("provider", "api error", "http ", "request failed")),
        ("non_json_response", ("non-json", "not json", "json decode", "invalid json")),
        ("correction_exhausted", ("correction", "self-correction")),
        ("source_authority_rejected", ("source authority", "authority catalog")),
        ("artifact_catalog_rejected", ("artifact catalog", "artifact ref")),
        (
            "schema_validation_failed",
            ("schema", "contract", "required", "frontmatter", "title", "core_content"),
        ),
    )
    for code, needles in rules:
        if any(needle in observed for needle in needles):
            codes.add(code)
    if not codes:
        stable_path = re.sub(r"[^a-z0-9]+", "_", failure_path).strip("_")
        codes.add(
            f"unclassified_{stable_path}" if stable_path else "unclassified_distillation_failure"
        )
    return tuple(sorted(codes))


def _normalize_source_family(source: str, metadata: Dict[str, Any]) -> str:
    """Reduce variable source labels and paths to a stable producer family."""

    explicit = str(metadata.get("source_family") or "").strip().lower()
    if explicit:
        return re.sub(r"[^a-z0-9_-]+", "_", explicit).strip("_") or "unknown"
    value = str(source or "").strip().lower()
    for family in ("codex", "claude", "gemini", "kimi", "opencode", "cursor"):
        if family in value:
            return family
    if "/" in value or "\\" in value or value.startswith(("file:", "document:")):
        return "trusted_user_document"
    normalized = re.sub(r"[^a-z0-9_-]+", "_", value).strip("_")
    return normalized or "unknown"


def _distill_error_fingerprint(
    errors: List[str],
    parse_metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Hash stable symptom codes, never variable validation strings."""

    payload = {
        "error_codes": _distill_error_codes(errors, parse_metadata),
        "failure_path": re.sub(
            r"[^a-z0-9]+",
            "_",
            str(
                (parse_metadata or {}).get("failure_path")
                or (parse_metadata or {}).get("path")
                or "unobserved"
            ).lower(),
        ).strip("_"),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]


def cleanup_failed_distill(
    database_dir: Path,
    ttl_days: int = 30,
    max_count: int = 1000,
) -> Dict[str, int]:
    """
    清理 distill_failed/ 目录，防止失败文件无限堆积。

    策略：
    1. 删除超过 ttl_days 的文件。
    2. 如果文件总数仍超过 max_count，按修改时间删除最旧的直到符合上限。

    返回 {"removed": int, "remaining": int}
    """
    failed_dir = database_dir / "distill_failed"
    if not failed_dir.exists():
        return {"removed": 0, "remaining": 0, "protected": 0}

    protected_paths: set[Path] = set()
    registered_paths: set[Path] = set()
    incident_db = database_dir / "operational_incidents.db"
    if not incident_db.is_file():
        remaining = len([path for path in failed_dir.iterdir() if path.is_file()])
        if remaining:
            logger.warning(
                "[distill_failed] incident evidence registry is absent; cleanup blocked"
            )
        return {
            "removed": 0,
            "remaining": remaining,
            "protected": remaining,
            "blocked": int(remaining > 0),
        }
    try:
        from core.ops.operational_incident import OperationalIncidentStore

        store = OperationalIncidentStore(incident_db)
        protected_paths = store.protected_artifact_paths()
        registered_paths = store.registered_artifact_paths()
    except (OSError, RuntimeError, sqlite3.Error):
        remaining = len([p for p in failed_dir.iterdir() if p.is_file()])
        logger.error(
            "[distill_failed] incident evidence registry unreadable; cleanup blocked",
            exc_info=True,
        )
        return {
            "removed": 0,
            "remaining": remaining,
            "protected": remaining,
            "blocked": 1,
        }

    for path in failed_dir.iterdir():
        if not path.is_file():
            continue
        resolved = path.resolve(strict=False)
        if resolved in registered_paths:
            continue
        try:
            payload = json.loads(read_text_value(path))
        except (OSError, UnicodeError, json.JSONDecodeError):
            protected_paths.add(resolved)
            continue
        ingest = payload.get("incident_ingest") if isinstance(payload, dict) else None
        if (
            isinstance(ingest, dict)
            and ingest.get("schema_version") == "mnemos.operational_incident_ingest.v1"
            and ingest.get("status") == "pending"
        ):
            protected_paths.add(resolved)

    now = datetime.now().timestamp()
    ttl_seconds = ttl_days * 86400
    files = []
    for path in failed_dir.iterdir():
        if not path.is_file():
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        files.append((path, mtime))

    removed = 0
    for path, mtime in files:
        if path.resolve(strict=False) in protected_paths:
            continue
        if now - mtime > ttl_seconds:
            try:
                path.unlink()
                removed += 1
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                ImportError,
                AttributeError,
                RuntimeError,
            ):
                logger.warning("[distill_failed] 删除过期文件失败: %s", path, exc_info=True)

    # 如果数量仍超过上限，按时间由旧到新删除
    files = [
        (p, m) for p, m in files if p.exists() and p.resolve(strict=False) not in protected_paths
    ]
    if len(files) > max_count:
        files.sort(key=lambda x: x[1])
        for path, _ in files[: len(files) - max_count]:
            try:
                path.unlink()
                removed += 1
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                ImportError,
                AttributeError,
                RuntimeError,
            ):
                logger.warning("[distill_failed] 删除文件失败: %s", path, exc_info=True)

    remaining = len([p for p in failed_dir.iterdir() if p.is_file()])
    if removed:
        logger.info("[distill_failed] 清理完成: 删除 %d 个文件, 剩余 %d 个", removed, remaining)
    protected = len(
        [
            path
            for path in failed_dir.iterdir()
            if path.is_file() and path.resolve(strict=False) in protected_paths
        ]
    )
    return {"removed": removed, "remaining": remaining, "protected": protected}


def save_failed_distill(
    session_id: str,
    fragments: List[KnowledgeFragment],
    validation_errors: List[str],
    database_dir: Path,
    source: str = "",
    raw_response: str = "",
    exc_info: str = "",
    parse_metadata: Optional[Dict[str, Any]] = None,
    producer: str = "conversation_distillation",
    severity: str = "high",
) -> Path:
    """将校验失败的蒸馏结果保存到 distill_failed/ 目录，供人工排查。"""
    failed_dir = database_dir / "distill_failed"
    failed_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    filename = f"failed-{session_id}-{timestamp}-{uuid.uuid4().hex[:12]}.json"
    path = failed_dir / filename

    data: Dict[str, Any] = {
        "session_id": session_id,
        "source": source,
        "saved_at": datetime.now().isoformat(),
        "failure_class": "distill_validation",
        "producer": producer,
        "severity": severity,
        "incident_ingest": {
            "schema_version": "mnemos.operational_incident_ingest.v1",
            "status": "pending",
        },
        "error_fingerprint": _distill_error_fingerprint(
            validation_errors,
            parse_metadata,
        ),
        "validation_errors": validation_errors,
        "parse_metadata": parse_metadata
        or {
            "path": "not_available",
            "correction_attempts": 0,
        },
        "raw_output": {
            "stored_in": str(path),
            "available": bool(raw_response),
            "length": len(raw_response or ""),
        },
        "raw_response": raw_response,
        "exc_info": exc_info,
        "fragments": [
            {
                "form": f.form,
                "title": f.title,
                "frontmatter": f.frontmatter,
                "background": f.background,
                "core_content": (
                    f.core_content[:2000] + "..." if len(f.core_content) > 2000 else f.core_content
                ),
                "boundaries": f.boundaries,
                "anti_patterns": f.anti_patterns,
                "related_concepts": f.related_concepts,
            }
            for f in fragments
        ],
    }
    redacted = redact_persistence_value(data)
    data = dict(redacted.value)
    data["privacy_redaction"] = {
        "policy": redacted.policy,
        "counts": {name: count for name, count in redacted.counts},
        "total": redacted.total,
    }
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)
    logger.warning("[Distillation] 校验失败的蒸馏结果已保存: %s", path)
    return path


def record_distillation_failure(
    session_id: str,
    fragments: List[KnowledgeFragment],
    validation_errors: List[str],
    database_dir: Path,
    source: str = "",
    raw_response: str = "",
    exc_info: str = "",
    parse_metadata: Optional[Dict[str, Any]] = None,
    *,
    producer: str = "conversation_distillation",
    severity: str = "high",
) -> DistillationFailureRecord:
    """Persist one failure occurrence and schedule diagnosis, never a recap."""

    artifact_path = save_failed_distill(
        session_id=session_id,
        fragments=fragments,
        validation_errors=validation_errors,
        database_dir=database_dir,
        source=source,
        raw_response=raw_response,
        exc_info=exc_info,
        parse_metadata=parse_metadata,
        producer=producer,
        severity=severity,
    )
    from core.ops.operational_incident import (
        DistillationFailureEvidence,
        OperationalIncidentStore,
    )

    metadata = dict(parse_metadata or {})
    responses = metadata.get("responses")
    response = (
        dict(responses[-1])
        if isinstance(responses, list) and responses and isinstance(responses[-1], dict)
        else {}
    )
    error_codes = _distill_error_codes(validation_errors, metadata)
    execution_spec_hash = _canonical_hash(
        metadata.get("execution_spec_hash"),
        missing_label="execution_spec_hash",
    )
    provider = str(response.get("provider") or metadata.get("provider") or "missing:provider")
    model = str(response.get("model") or metadata.get("model") or "missing:model")
    route = str(
        response.get("route")
        or metadata.get("route")
        or metadata.get("failure_path")
        or metadata.get("path")
        or "missing:route"
    )
    schema_hash = _canonical_hash(
        metadata.get("schema_hash"),
        missing_label="schema_hash",
        derived_contract={"schema": "distill_output_v4"},
    )
    parser_hash = _canonical_hash(
        metadata.get("parser_hash"),
        missing_label="parser_hash",
        derived_contract={
            "parser": "distillation_response_parser.v1",
            "parse_path": str(response.get("parse_path") or metadata.get("path") or ""),
        },
    )
    validator_hash = _canonical_hash(
        metadata.get("validator_hash"),
        missing_label="validator_hash",
        derived_contract={
            "validator": "distillation_contract_validator.v4",
            "error_codes": list(error_codes),
        },
    )
    redacted_errors = redact_persistence_value(list(validation_errors)).value
    db_path = Path(database_dir) / "operational_incidents.db"
    artifact_hash = _sha256_file(artifact_path)
    source_event_refs = tuple(
        str(value) for value in metadata.get("source_event_refs", ()) if str(value).strip()
    )
    prompt_hash = _canonical_hash(
        metadata.get("prompt_hash"),
        missing_label="prompt_hash",
    )
    visible_input_sha256 = _canonical_hash(
        metadata.get("visible_input_sha256"),
        missing_label="visible_input_sha256",
    )
    response_hash = _canonical_hash(
        metadata.get("response_hash"),
        missing_label="response_hash",
    )
    missing_evidence = tuple(
        name
        for name, value in (
            ("execution_spec_hash", execution_spec_hash),
            ("prompt_hash", prompt_hash),
            ("provider", provider),
            ("model", model),
            ("route", route),
            ("schema_hash", schema_hash),
            ("parser_hash", parser_hash),
            ("validator_hash", validator_hash),
            ("visible_input_sha256", visible_input_sha256),
            ("response_hash", response_hash),
        )
        if str(value).startswith("missing:")
    ) + (() if source_event_refs else ("source_event_refs",))
    evidence = DistillationFailureEvidence(
        session_id=session_id,
        source_family=_normalize_source_family(source, metadata),
        producer=producer,
        severity=severity,
        failure_class="distill_validation",
        error_codes=error_codes,
        validation_errors=tuple(str(item) for item in redacted_errors),
        execution_spec_hash=execution_spec_hash,
        prompt_hash=prompt_hash,
        provider=provider,
        model=model,
        route=route,
        schema_hash=schema_hash,
        parser_hash=parser_hash,
        validator_hash=validator_hash,
        visible_input_sha256=visible_input_sha256,
        response_hash=response_hash,
        source_event_refs=source_event_refs,
        artifact_path=artifact_path,
        artifact_hash=artifact_hash,
        artifact_acl="distillation_failure_diagnostic_restricted_v1",
        retention_class="unresolved_incident_hold_v1",
        raw_response_available=bool(raw_response),
        raw_response_length=len(raw_response or ""),
        missing_evidence=missing_evidence,
    )
    try:
        incident = OperationalIncidentStore(db_path).record_distillation_failure(evidence)
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        logger.error(
            "[Distillation] failure artifact remains pending incident ingestion: %s",
            artifact_path,
            exc_info=True,
        )
        return DistillationFailureRecord(artifact_path=artifact_path, incident=None)
    logger.warning(
        "[Distillation] failure recorded as operational incident %s occurrence %s",
        incident.incident_id,
        incident.occurrence_id,
    )
    return DistillationFailureRecord(
        artifact_path=artifact_path,
        incident=incident,
    )
