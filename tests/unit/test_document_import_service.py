# -*- coding: utf-8 -*-
"""Unit tests for DocumentImportService security contract."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.application.document_import_service import DocumentImportService


class _Config:
    def __init__(self, root: Path):
        self.database_dir = root / "db"
        self.obsidian_vault_path = str(root / "raw-vault")

    def get(self, key: str, default=None):
        if key == "document_process.max_file_size_mb":
            return 100
        return default


def test_document_import_preserves_tagged_content_for_authority_containment(tmp_path):
    sample = tmp_path / "unsafe.md"
    sample.write_text("# unsafe", encoding="utf-8")

    with patch("core.sync_framework.file_ingestor.FileIngestor") as ingestor_cls:
        ingestor = ingestor_cls.return_value
        ingestor.ingest_file.return_value = [
            SimpleNamespace(
                uid="raw-unsafe",
                metadata={
                    "raw_revision_id": "raw-unsafe",
                    "asset_kind": "trusted_user_document",
                    "source_authority": "external_content",
                },
            )
        ]
        ingestor.last_security_assessment = {
            "security_decision": "tagged_prompt_injection",
            "security_tags": ["x-security=prompt-injection"],
            "security_score": 1.0,
            "security_containment": "source_authority",
        }
        ingestor.last_ingestion_receipt = {
            "source_event_id": "raw-unsafe",
            "raw_event_id": "raw-unsafe",
            "provenance_id": "raw-unsafe",
        }
        ingestor.last_handoff_status = "pending"
        ingestor.last_projection_status = "pending"
        ingestor.last_queue_id = "capture-unsafe"
        ingestor.last_session_id = "file-unsafe"

        result = DocumentImportService(config=_Config(tmp_path)).import_document(sample)

    assert result["success"] is True
    assert result["quality_decision"] == "queued_for_quality_gate"
    assert result["security_decision"] == "tagged_prompt_injection"
    assert result["routing_result"]["status"] == "capture_outbox_pending"
    assert result["raw_revision_id"] == "raw-unsafe"
    ingestor.ingest_file.assert_called_once_with(
        sample.resolve(),
        agent_name="trusted_user_document",
        request_distillation=True,
        title="",
    )


def test_document_import_capture_exposes_ingestion_receipt(tmp_path):
    sample = tmp_path / "safe.md"
    sample.write_text("# safe", encoding="utf-8")

    with patch("core.sync_framework.file_ingestor.FileIngestor") as ingestor_cls:
        ingestor = ingestor_cls.return_value
        ingestor.ingest_file.return_value = [SimpleNamespace(uid="l1-u1")]
        ingestor.last_security_assessment = None
        ingestor.last_session_id = "file-session"
        ingestor.last_queue_id = "queue-1"
        ingestor.last_handoff_status = "not_requested"
        ingestor.last_projection_status = "pending"
        ingestor.last_ingestion_receipt = {
            "success": True,
            "source_event_id": "raw-event-1",
            "raw_event_id": "raw-event-1",
            "provenance_id": "raw-event-1",
        }

        result = DocumentImportService(config=_Config(tmp_path)).import_document(
            sample,
            mode="capture",
        )

    assert result["success"] is True
    assert result["ingestion_receipt"]["raw_event_id"] == "raw-event-1"
    assert result["source_event_id"] == "raw-event-1"
    assert result["raw_event_id"] == "raw-event-1"
    assert result["provenance_id"] == "raw-event-1"
    assert result["handoff_status"] == "not_requested"
    assert result["routing_result"]["status"] == "capture_only"
    ingestor.ingest_file.assert_called_once_with(
        sample.resolve(),
        agent_name="trusted_user_document",
        request_distillation=False,
        title="",
    )
