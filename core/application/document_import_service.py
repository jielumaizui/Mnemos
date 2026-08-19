"""Unified backend for user-specified document imports."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from core.config import get_config
from core.document_import import (
    DOCUMENT_MAX_FILE_SIZE_KEY,
    file_sha256,
    scan_trusted_document_privacy,
    validate_trusted_user_document,
)
from core.cognitive.state_contract import sha256_json
from core.ops.action_ledger import authorize_primary_action_ledger_record
from core.privacy.ingestion_security import assess_ingestion_security, attach_security_fields

IMPORT_MODES = {"parse", "capture", "distill", "watch"}
_DOCUMENT_IMPORT_LEDGER_CONTRACT = (
    "Append an exact document-import result to ActionLedger only when the "
    "mode, source identity, result status, and canonical receipt evidence agree."
)
_DOCUMENT_IMPORT_LEDGER_PRODUCER_HASH = sha256_json(
    {
        "module": "core.application.document_import_service",
        "contract": "mnemos.document_import_action_ledger.v1",
    }
)


class DocumentImportService:
    """Apply one import contract for CLI, daemon file ingest, and MCP document_process."""

    def __init__(self, *, config: Any = None):
        self.config = config or get_config()

    def import_document(
        self,
        file_path: str | Path,
        *,
        mode: str = "distill",
        title: str = "",
        agent_name: str = "trusted_user_document",
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        normalized_mode = _normalize_mode(mode)
        validation = validate_trusted_user_document(file_path, config=self.config)
        if not validation.ok or validation.path is None:
            result = _base_failure_result(file_path, normalized_mode, validation)
            self._record_ledger(result, status="failed_terminal")
            return result

        source_hash = file_sha256(validation.path)
        privacy_scan = scan_trusted_document_privacy(validation.path)
        base = _base_success_result(
            validation.path,
            normalized_mode,
            source_hash,
            validation.size_bytes,
            validation.max_size_mb,
            privacy_scan,
            dry_run=dry_run,
        )
        if dry_run:
            base.update(
                {
                    "success": True,
                    "message": "dry-run: 已通过路径、大小和隐私预扫描，未解析、未入库、未蒸馏",
                    "parse_result": {"status": "not_run"},
                    "quality_decision": "dry_run",
                    "routing_result": {"status": "dry_run"},
                }
            )
            self._record_ledger(base, status="verified")
            return base

        if normalized_mode == "watch":
            base.update(
                {
                    "success": True,
                    "message": "watch 模式由 daemon file_ingestor 监听服务执行；本次仅完成 trusted_user_document 预检",
                    "parse_result": {"status": "not_run"},
                    "quality_decision": "accepted_for_watch",
                    "routing_result": {"status": "daemon_file_ingestor"},
                }
            )
            self._record_ledger(base, status="queued")
            return base

        if normalized_mode in {"capture", "distill"}:
            result = self._capture(
                validation.path,
                base,
                agent_name,
                title=title,
                request_distillation=normalized_mode == "distill",
            )
            self._record_ledger(result, status="queued" if result.get("success") else "failed_recoverable")
            return result

        processor, doc, parse_result = self._parse(validation.path, title)
        base["parse_result"] = parse_result
        if doc is None:
            base.update({"success": False, "message": "文档解析失败，无法提取内容", "quality_decision": "parse_error"})
            self._record_ledger(base, status="failed_terminal")
            return base
        security = assess_ingestion_security(doc.content or "")
        attach_security_fields(base, security)

        if normalized_mode == "parse":
            base.update(
                {
                    "success": True,
                    "message": "parse-only: 已解析文档，未写入 L1 或 Wiki",
                    "quality_decision": "parse_only",
                    "routing_result": {"status": "not_routed"},
                }
            )
            self._record_ledger(base, status="produced")
            return base

        raise RuntimeError(f"unhandled document import mode: {normalized_mode}")

    def _parse(self, src_path: Path, title: str) -> tuple[Any, Optional[Any], Dict[str, Any]]:
        from core.hephaestus.document_processor import DocumentProcessor

        processor = DocumentProcessor()
        doc = processor.process_document(src_path)
        if doc is not None and title:
            doc.title = title
        return processor, doc, _build_parse_result(doc)

    def _capture(
        self,
        src_path: Path,
        base: Dict[str, Any],
        agent_name: str,
        *,
        title: str,
        request_distillation: bool,
    ) -> Dict[str, Any]:
        from core.sync_framework.file_ingestor import FileIngestor

        ingestor = FileIngestor(config=self.config)
        saved = ingestor.ingest_file(
            src_path,
            agent_name=agent_name,
            request_distillation=request_distillation,
            title=title,
        )
        if ingestor.last_security_assessment:
            base.update(ingestor.last_security_assessment)
        if ingestor.last_ingestion_receipt:
            base["ingestion_receipt"] = ingestor.last_ingestion_receipt
        if not saved:
            base.update(
                {
                    "success": False,
                    "message": "文档 capture 失败：不支持的文件类型、无法提取文本或 canonical raw 写入失败",
                    "parse_result": {"status": "capture_failed"},
                    "quality_decision": "capture_failed",
                    "routing_result": {"status": "capture_failed"},
                }
            )
            return base
        storage_uid = getattr(saved[0], "uid", None) if saved else None
        saved_meta = getattr(saved[0], "metadata", {}) if saved else {}
        handoff_status = ingestor.last_handoff_status or saved_meta.get("handoff_status", "")
        projection_status = ingestor.last_projection_status or saved_meta.get(
            "projection_status", ""
        )
        routing_status = "capture_outbox_pending" if request_distillation else "capture_only"
        base.update(
            {
                "success": True,
                "message": (
                    "canonical raw 已接受；capture outbox 等待 durable Amphora handoff"
                    if request_distillation
                    else "canonical raw 已接受；未请求蒸馏"
                ),
                "parse_result": {"status": "captured_to_canonical_raw"},
                "l1_uid": storage_uid,
                "queue_id": ingestor.last_queue_id or ingestor.last_session_id or "",
                "capture_queue_ref": ingestor.last_queue_id or "",
                "ingestion_status": "accepted",
                "handoff_status": handoff_status,
                "projection_status": projection_status,
                "asset_kind": saved_meta.get("asset_kind", "trusted_user_document"),
                "asset_id": saved_meta.get("asset_id", ""),
                "asset_title": saved_meta.get("asset_title", title),
                "raw_revision_id": saved_meta.get("raw_revision_id", storage_uid),
                "source_event_id": (ingestor.last_ingestion_receipt or {}).get(
                    "source_event_id", ""
                ),
                "raw_event_id": (ingestor.last_ingestion_receipt or {}).get("raw_event_id", ""),
                "provenance_id": (ingestor.last_ingestion_receipt or {}).get(
                    "provenance_id", ""
                ),
                "quality_decision": (
                    "queued_for_quality_gate" if request_distillation else "captured"
                ),
                "routing_result": {"status": routing_status},
                "pipeline": (
                    "trusted_user_document → canonical raw → capture outbox → Amphora → quality gate → Wiki"
                    if request_distillation
                    else "trusted_user_document → canonical raw → raw projection"
                ),
            }
        )
        return base

    def _record_ledger(self, result: Dict[str, Any], *, status: str) -> None:
        try:
            from core.system_contracts import ActionLedger, make_action_record

            source_hash = result.get("source_hash") or "unknown"
            record = make_action_record(
                actor="mnemos",
                action_type="document_import",
                target=f"trusted_user_document:{source_hash}",
                status=status,
                evidence_refs=(
                    f"mode:{result.get('mode', '')}",
                    f"config:{DOCUMENT_MAX_FILE_SIZE_KEY}",
                    f"privacy:{result.get('privacy_scan', {}).get('status', '')}",
                ),
                after_ref=f"wiki_paths:{len(result.get('wiki_paths', []))}",
                verification={
                    "success": bool(result.get("success")),
                    "content_size": result.get("content_size"),
                    "quality_decision": result.get("quality_decision"),
                },
                rollback_ref="manual:delete_imported_l1_and_wiki_paths",
            )
            state_db = Path(self.config.database_dir) / "producer_consumer_ledger.db"
            material_action = authorize_primary_action_ledger_record(
                record,
                state_db_path=state_db,
                contract_id="project-contract:document-import-action-ledger",
                contract_revision_id="mnemos.document_import_action_ledger.v1",
                contract_text=_DOCUMENT_IMPORT_LEDGER_CONTRACT,
                source_namespace="document-import-action-ledger",
                source_facts={
                    "mode": str(result.get("mode") or ""),
                    "source_hash": str(source_hash),
                    "raw_revision_id": str(result.get("raw_revision_id") or ""),
                    "status": status,
                    "success": bool(result.get("success")),
                },
                decision_checks={
                    "mode_is_registered": str(result.get("mode") or "")
                    in IMPORT_MODES,
                    "status_matches_result": status
                    in {
                        "failed_recoverable",
                        "failed_terminal",
                        "produced",
                        "queued",
                        "verified",
                    },
                    "source_identity_is_bound": bool(source_hash),
                },
                evidence_refs=tuple(str(value) for value in record.evidence_refs),
                task="Append the exact document-import result to ActionLedger",
                goal="Preserve an immutable, source-bound document import receipt.",
                constraints=(
                    "Do not append a row for a different import mode or source hash.",
                    "The authorization governs only this ActionLedger append.",
                ),
                producer="document-import-service",
                producer_version="mnemos.document_import_action_ledger.v1",
                producer_code_hash=_DOCUMENT_IMPORT_LEDGER_PRODUCER_HASH,
                evaluator_id="document-import-action-ledger-evaluator",
                approved_candidate_key="append_bound_document_import_receipt",
                approved_candidate_summary=(
                    "Append the exact validated document-import result."
                ),
                rejected_candidate_key="omit_unbound_document_import_receipt",
                rejected_candidate_summary=(
                    "Do not append a result whose mode, source, or status is unbound."
                ),
                approved_reason_code="document_import_receipt_binding_verified",
                rejected_reason_code="document_import_receipt_binding_rejected",
                committed_metric="document_import_action_ledger_receipt",
                rejected_metric="unbound_document_import_ledger_count",
            )
            ledger_id = ActionLedger.from_config(
                self.config,
                initialize=True,
            ).record(record, material_action=material_action)
            result["action_ledger_ref"] = ledger_id
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
            result["action_ledger_ref"] = ""


def _normalize_mode(mode: str) -> str:
    normalized = (mode or "distill").strip().lower()
    if normalized not in IMPORT_MODES:
        raise ValueError(f"unsupported document import mode: {mode}")
    return normalized


def _base_failure_result(file_path: str | Path, mode: str, validation: Any) -> Dict[str, Any]:
    return {
        "success": False,
        "mode": mode,
        "content_source": "external_file",
        "user_supplied": True,
        "trusted_user_document": True,
        "source_path": str(file_path),
        "source_hash": "",
        "content_size": validation.size_bytes,
        "max_file_size_mb": validation.max_size_mb,
        "max_file_size_config_key": validation.config_key,
        "message": validation.message,
        "rejection_reason": validation.reason,
        "parse_result": {"status": "not_run"},
        "l1_uid": None,
        "queue_id": "",
        "wiki_paths": [],
        "quality_decision": "rejected",
        "routing_result": {"status": "not_routed"},
        "ingestion_status": "rejected",
        "handoff_status": "not_created",
        "projection_status": "not_created",
        "privacy_scan": {"status": "not_run"},
        "security_tags": ["x-security=not-run"],
        "security_score": 0.0,
        "security_risk": "unknown",
        "security_decision": "not_run",
        "security_categories": [],
        "security_reason": "not_run",
    }


def _base_success_result(
    src_path: Path,
    mode: str,
    source_hash: str,
    size_bytes: int,
    max_size_mb: int,
    privacy_scan: Dict[str, Any],
    *,
    dry_run: bool,
) -> Dict[str, Any]:
    return {
        "success": False,
        "mode": mode,
        "dry_run": dry_run,
        "content_source": "external_file",
        "user_supplied": True,
        "trusted_user_document": True,
        "source_path": str(src_path),
        "source_hash": source_hash,
        "content_size": size_bytes,
        "max_file_size_mb": max_size_mb,
        "max_file_size_config_key": DOCUMENT_MAX_FILE_SIZE_KEY,
        "privacy_scan": privacy_scan,
        "l1_uid": None,
        "queue_id": "",
        "wiki_paths": [],
        "ingestion_status": "pending",
        "handoff_status": "not_created",
        "projection_status": "not_created",
        "security_tags": ["x-security=not-run"],
        "security_score": 0.0,
        "security_risk": "unknown",
        "security_decision": "not_run",
        "security_categories": [],
        "security_reason": "not_run",
    }


def _build_parse_result(doc: Any) -> Dict[str, Any]:
    if doc is None:
        return {"status": "parse_failed"}
    meta = doc.metadata or {}
    toc = _extract_toc(doc.content)
    return {
        "status": "parsed",
        "title": doc.title,
        "doc_type": doc.doc_type.value if hasattr(doc.doc_type, "value") else str(doc.doc_type),
        "pages": meta.get("pages", meta.get("slides", meta.get("chapters", 0))),
        "word_count": len(doc.content.split()) if doc.content else 0,
        "has_toc": bool(toc),
        "toc": toc[:20],
        "content_preview": doc.content[:2000] if doc.content else "",
        "metadata": meta,
        "summary": doc.summary,
        "validation_status": doc.validation_status,
    }


def _extract_toc(content: str) -> list[str]:
    return [line.strip().lstrip("# ") for line in (content or "").split("\n") if line.strip().startswith("#")]
