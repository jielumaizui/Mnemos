"""Independent target-store audit for the operational incident pipeline."""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from core.ops.operational_incident import (
    SCHEMA_HASH,
    SCHEMA_VERSION,
    DistillationFailureEvidence,
    OperationalIncidentStore,
    initialize_operational_incident_schema,
    validate_operational_incident_schema,
)
from core.ops.operational_incident_replay import (
    execute_distillation_failure_replay,
    plan_distillation_failure_replay,
)
from core.utils import read_bytes_value, read_text_value

AUDIT_SCHEMA_VERSION = "mnemos.operational_incident_pipeline_audit.v1"


def _scalar(conn: sqlite3.Connection, query: str) -> int:
    row = conn.execute(query).fetchone()
    return int(row[0] if row else 0)


def _static_residuals(repo_root: Path) -> dict[str, int]:
    hephaestus = repo_root / "core" / "hephaestus"
    legacy_callsite = 0
    direct_bypass = 0
    for path in hephaestus.rglob("*.py"):
        text = read_text_value(path)
        legacy_callsite += text.count("_trigger_distill_failure_recap")
        direct_bypass += text.count("手动修复后复制到 Wiki 00-Inbox")
    required_paths = (
        repo_root / "core" / "ops" / "operational_incident.py",
        repo_root / "core" / "ops" / "operational_incident_diagnostics.py",
        repo_root / "core" / "ops" / "operational_incident_replay.py",
        repo_root / "scripts" / "diagnose_operational_incident.py",
        repo_root / "scripts" / "reconcile_operational_incidents.py",
        repo_root / "scripts" / "replay_distillation_failure.py",
        repo_root / "daemon" / "operational_incident_service.py",
    )
    failure_entrypoint = read_text_value(
        repo_root / "core" / "hephaestus" / "distillation_failure.py"
    )
    daemon_intervals = read_text_value(repo_root / "daemon" / "intervals.py")
    daemon_registry = read_text_value(repo_root / "daemon" / "service_registry.py")
    bootstrap = read_text_value(repo_root / "core" / "db_init.py")
    full_score = read_text_value(repo_root / "scripts" / "run_full_score_gates.py")
    incident_gate = (
        full_score.split('"contracts.operational_incident_pipeline"', 1)[1][:600]
        if '"contracts.operational_incident_pipeline"' in full_score
        else ""
    )
    return {
        "legacy_distill_recap_callsite": legacy_callsite,
        "direct_wiki_bypass_advice": direct_bypass,
        "required_phase6_path_missing": sum(1 for path in required_paths if not path.is_file()),
        "incident_entrypoint_missing": int(
            "def record_distillation_failure(" not in failure_entrypoint
        ),
        "daemon_consumer_missing": int(
            '"operational_incidents"' not in daemon_intervals
            or '"operational_incidents": "service_operational_incidents"' not in daemon_registry
        ),
        "bootstrap_owner_missing": int(
            '"operational_incidents", _ensure_operational_incidents' not in bootstrap
        ),
        "full_score_gate_missing": int(
            '"contracts.operational_incident_pipeline"' not in full_score
        ),
        "full_score_gate_runtime_mode_missing": int(
            not incident_gate or '"--self-test"' not in incident_gate
        ),
    }


def audit_operational_incident_static(repo_root: str | Path) -> dict[str, Any]:
    """Audit code-path closure without depending on initialized runtime state."""

    metrics = _static_residuals(Path(repo_root))
    findings = [{"metric": name, "count": count} for name, count in metrics.items() if count != 0]
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "static",
        "ok": not findings,
        "store_schema_version": SCHEMA_VERSION,
        "store_schema_hash": SCHEMA_HASH,
        "metrics": metrics,
        "findings": findings,
    }


def audit_operational_incident_pipeline(
    db_path: str | Path,
    *,
    repo_root: str | Path,
) -> dict[str, Any]:
    """Recompute every Phase 6 gap without trusting producer status fields."""

    path = Path(db_path)
    root = Path(repo_root)
    if not path.is_file():
        raise RuntimeError("operational incident target store is uninitialized")
    uri = f"{path.resolve(strict=True).as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=10) as conn:
        validate_operational_incident_schema(conn)
        registry = conn.execute("""
            SELECT schema_version, schema_hash
            FROM operational_incident_schema_registry
            WHERE component='operational_incident'
            """).fetchone()
        metrics = {
            "occurrence_without_incident": _scalar(
                conn,
                """
                SELECT COUNT(*) FROM incident_occurrences AS occurrence
                LEFT JOIN operational_incidents AS incident
                  ON incident.incident_id=occurrence.incident_id
                WHERE incident.incident_id IS NULL
                """,
            ),
            "occurrence_without_ingest_receipt": _scalar(
                conn,
                """
                SELECT COUNT(*) FROM incident_occurrences AS occurrence
                LEFT JOIN incident_ingest_receipts AS receipt
                  ON receipt.occurrence_id=occurrence.occurrence_id
                 AND receipt.incident_id=occurrence.incident_id
                 AND receipt.artifact_path=occurrence.artifact_path
                 AND receipt.artifact_hash=occurrence.artifact_hash
                 AND receipt.status='committed'
                WHERE receipt.receipt_id IS NULL
                """,
            ),
            "incident_without_diagnostic_task": _scalar(
                conn,
                """
                SELECT COUNT(*) FROM operational_incidents AS incident
                LEFT JOIN diagnostic_tasks AS task
                  ON task.incident_id=incident.incident_id
                WHERE task.task_id IS NULL
                """,
            ),
            "diagnostic_without_report": _scalar(
                conn,
                """
                SELECT COUNT(*) FROM diagnostic_tasks AS task
                LEFT JOIN root_cause_reports AS report
                  ON report.diagnostic_task_id=task.task_id
                WHERE report.report_id IS NULL
                """,
            ),
            "diagnosis_without_evidence": _scalar(
                conn,
                """
                SELECT COUNT(*) FROM root_cause_reports
                WHERE evidence_refs_json='[]'
                   OR reproduction_command=''
                   OR repair=''
                   OR verification=''
                """,
            ),
            "notification_without_report": _scalar(
                conn,
                """
                SELECT COUNT(*) FROM incident_notification_commands AS command
                LEFT JOIN root_cause_reports AS report
                  ON report.report_id=command.report_id
                WHERE report.report_id IS NULL
                """,
            ),
            "delivered_without_receipt": _scalar(
                conn,
                """
                SELECT COUNT(*) FROM incident_notification_commands AS command
                LEFT JOIN incident_notification_receipts AS receipt
                  ON receipt.command_id=command.command_id
                WHERE command.status='delivered' AND receipt.receipt_id IS NULL
                """,
            ),
            "receipt_without_delivered": _scalar(
                conn,
                """
                SELECT COUNT(*) FROM incident_notification_receipts AS receipt
                JOIN incident_notification_commands AS command
                  ON command.command_id=receipt.command_id
                WHERE command.status!='delivered'
                """,
            ),
            "resolved_without_committed_replay": _scalar(
                conn,
                """
                SELECT COUNT(*) FROM operational_incidents AS incident
                LEFT JOIN incident_resolutions AS resolution
                  ON resolution.incident_id=incident.incident_id
                LEFT JOIN incident_replay_receipts AS receipt
                  ON receipt.receipt_id=resolution.replay_receipt_id
                 AND receipt.status='committed'
                 AND receipt.executor='formal_distillation_replay.v1'
                LEFT JOIN incident_replay_commands AS command
                  ON command.command_id=receipt.command_id
                 AND command.input_binding_hash=receipt.input_binding_hash
                WHERE incident.status='resolved'
                  AND (
                    resolution.resolution_id IS NULL
                    OR receipt.receipt_id IS NULL
                    OR command.command_id IS NULL
                  )
                """,
            ),
            "committed_replay_binding_gap": _scalar(
                conn,
                """
                SELECT COUNT(*) FROM incident_replay_receipts AS receipt
                JOIN incident_replay_commands AS command
                  ON command.command_id=receipt.command_id
                WHERE receipt.status='committed'
                  AND (
                    receipt.input_binding_hash!=command.input_binding_hash
                    OR receipt.input_binding_hash NOT LIKE 'sha256:%'
                    OR receipt.executor!='formal_distillation_replay.v1'
                  )
                """,
            ),
            "unresolved_with_retrospective": _scalar(
                conn,
                """
                SELECT COUNT(*) FROM incident_retrospectives AS retrospective
                JOIN operational_incidents AS incident
                  ON incident.incident_id=retrospective.incident_id
                WHERE incident.status!='resolved'
                """,
            ),
            "active_fingerprint_duplicate": _scalar(
                conn,
                """
                SELECT COUNT(*) FROM (
                    SELECT fingerprint FROM operational_incidents
                    WHERE status!='resolved'
                    GROUP BY fingerprint HAVING COUNT(*) > 1
                )
                """,
            ),
            "occurrence_contract_gap": _scalar(
                conn,
                """
                SELECT COUNT(*) FROM incident_occurrences
                WHERE missing_evidence_json!='[]'
                   OR prompt_hash NOT LIKE 'sha256:%'
                   OR visible_input_sha256 NOT LIKE 'sha256:%'
                   OR response_hash NOT LIKE 'sha256:%'
                   OR source_event_refs_json='[]'
                   OR artifact_acl!='distillation_failure_diagnostic_restricted_v1'
                   OR retention_class!='unresolved_incident_hold_v1'
                """,
            ),
            "report_binding_gap": _scalar(
                conn,
                """
                SELECT COUNT(*) FROM root_cause_reports
                WHERE evidence_refs_json NOT LIKE '%execution-spec:%'
                   OR evidence_refs_json NOT LIKE '%prompt:%'
                   OR evidence_refs_json NOT LIKE '%provider:%'
                   OR evidence_refs_json NOT LIKE '%model:%'
                   OR evidence_refs_json NOT LIKE '%schema:%'
                   OR evidence_refs_json NOT LIKE '%parser:%'
                   OR evidence_refs_json NOT LIKE '%validator:%'
                   OR evidence_refs_json NOT LIKE '%source-event:%'
                """,
            ),
            "confirmed_report_without_root_proof": _scalar(
                conn,
                """
                SELECT COUNT(*) FROM root_cause_reports AS report
                WHERE report.root_cause_status='confirmed'
                  AND NOT EXISTS (
                    SELECT 1 FROM incident_diagnostic_evidence AS evidence
                    WHERE evidence.incident_id=report.incident_id
                      AND evidence.diagnostic_task_id=report.diagnostic_task_id
                      AND evidence.producer='formal_diagnostic_reproducer.v1'
                      AND evidence.before_status='failed'
                      AND evidence.after_status='passed'
                      AND report.evidence_refs_json LIKE
                        '%diagnostic-evidence:' || evidence.evidence_id || ':'
                        || evidence.evidence_hash || '%'
                  )
                """,
            ),
            "artifact_access_orphan": _scalar(
                conn,
                """
                SELECT COUNT(*) FROM incident_artifact_access_events AS access
                LEFT JOIN incident_occurrences AS occurrence
                  ON occurrence.occurrence_id=access.occurrence_id
                WHERE occurrence.occurrence_id IS NULL
                   OR access.incident_id!=occurrence.incident_id
                   OR access.artifact_hash!=occurrence.artifact_hash
                """,
            ),
            "legacy_open_quarantine": _scalar(
                conn,
                "SELECT COUNT(*) FROM legacy_incident_quarantine",
            ),
        }
        counts = {
            "incidents": _scalar(conn, "SELECT COUNT(*) FROM operational_incidents"),
            "occurrences": _scalar(conn, "SELECT COUNT(*) FROM incident_occurrences"),
            "ingest_receipts": _scalar(conn, "SELECT COUNT(*) FROM incident_ingest_receipts"),
            "diagnostic_tasks": _scalar(conn, "SELECT COUNT(*) FROM diagnostic_tasks"),
            "diagnostic_evidence": _scalar(
                conn, "SELECT COUNT(*) FROM incident_diagnostic_evidence"
            ),
            "root_cause_reports": _scalar(conn, "SELECT COUNT(*) FROM root_cause_reports"),
            "notification_commands": _scalar(
                conn, "SELECT COUNT(*) FROM incident_notification_commands"
            ),
            "notification_receipts": _scalar(
                conn, "SELECT COUNT(*) FROM incident_notification_receipts"
            ),
            "replay_receipts": _scalar(conn, "SELECT COUNT(*) FROM incident_replay_receipts"),
            "resolutions": _scalar(conn, "SELECT COUNT(*) FROM incident_resolutions"),
            "retrospectives": _scalar(conn, "SELECT COUNT(*) FROM incident_retrospectives"),
            "artifact_access_events": _scalar(
                conn, "SELECT COUNT(*) FROM incident_artifact_access_events"
            ),
            "legacy_migrations": _scalar(conn, "SELECT COUNT(*) FROM legacy_incident_migrations"),
            "legacy_quarantine": _scalar(conn, "SELECT COUNT(*) FROM legacy_incident_quarantine"),
        }
    metrics.update(_static_residuals(root))
    findings = [{"metric": name, "count": value} for name, value in metrics.items() if value != 0]
    registry_ok = registry == (SCHEMA_VERSION, SCHEMA_HASH)
    if not registry_ok:
        findings.append({"metric": "schema_registry_mismatch", "count": 1})
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ok": not findings,
        "store_schema_version": SCHEMA_VERSION,
        "store_schema_hash": SCHEMA_HASH,
        "registry_ok": registry_ok,
        "mode": "target_store",
        "counts": counts,
        "metrics": metrics,
        "findings": findings,
    }


def audit_operational_incident_reference(repo_root: str | Path) -> dict[str, Any]:
    """Exercise the Phase 6 chain in an isolated store, then audit its effects."""

    from core.hephaestus.distillation_failure import save_failed_distill
    from core.hephaestus.distillation_models import KnowledgeFragment
    from core.hephaestus.distillation_text import build_session_text
    from core.sync_framework.raw_event_store import RawEventStore

    with tempfile.TemporaryDirectory(prefix="mnemos-phase6-reference-") as raw_root:
        root = Path(raw_root)
        db_path = root / "operational_incidents.db"
        initialize_operational_incident_schema(db_path)
        store = OperationalIncidentStore(db_path)
        messages = [{"role": "user", "content": "phase six canonical replay input"}]
        visible_input = build_session_text(messages, lossless=True)
        visible_hash = "sha256:" + hashlib.sha256(visible_input.encode("utf-8")).hexdigest()
        raw_db = root / "raw_events.db"
        raw_store = RawEventStore(db_path=raw_db)
        try:
            raw_revision_id = raw_store.upsert_turn(
                source_agent="codex",
                session_id="reference-session-0",
                turn_number=0,
                user_content="phase six canonical replay input",
                assistant_content="",
                completeness={"visible_text": "full"},
            )
        finally:
            raw_store.close()
        records = []
        diagnostic_before = KnowledgeFragment(
            form="方法论",
            title="过短",
            frontmatter={"摘要": "用于验证蒸馏格式修复结果", "领域": "知识管理"},
            background="格式故障诊断",
            core_content="## 核心验证\n\n" + "修复后的结构化内容能够通过生产硬校验。" * 8,
            boundaries={"applies": "格式修复", "not_applies": "无关故障"},
            anti_patterns=[],
            related_concepts=["蒸馏合同"],
        )
        for index in range(10):
            artifact_path = save_failed_distill(
                session_id=f"reference-artifact-{index}",
                fragments=[diagnostic_before],
                validation_errors=["标题过短"],
                database_dir=root,
                raw_response="{}",
            )
            artifact_hash = "sha256:" + hashlib.sha256(
                read_bytes_value(artifact_path)
            ).hexdigest()
            records.append(
                store.record_distillation_failure(
                    DistillationFailureEvidence(
                        session_id=f"reference-session-{index}",
                        source_family="reference",
                        producer="conversation_distillation",
                        severity="high",
                        failure_class="distill_validation",
                        error_codes=("schema_validation_failed",),
                        validation_errors=("title is required",),
                        execution_spec_hash="sha256:" + "1" * 64,
                        prompt_hash="sha256:" + "2" * 64,
                        provider="reference-provider",
                        model="reference-model",
                        route="reference-route",
                        schema_hash="sha256:" + "3" * 64,
                        parser_hash="sha256:" + "4" * 64,
                        validator_hash="sha256:" + "5" * 64,
                        visible_input_sha256=visible_hash,
                        response_hash="sha256:" + "6" * 64,
                        source_event_refs=(raw_revision_id,),
                        artifact_path=artifact_path,
                        artifact_hash=artifact_hash,
                        artifact_acl=("distillation_failure_diagnostic_restricted_v1"),
                        retention_class="unresolved_incident_hold_v1",
                        raw_response_available=True,
                        raw_response_length=2,
                    )
                )
            )
        incident_ids = {record.incident_id for record in records}
        incident_id = records[0].incident_id
        initial_report = store.diagnose_next()
        valid_fragment = {
            "form": "方法论",
            "title": "蒸馏格式契约修复验证方法",
            "frontmatter": {"摘要": "用于验证蒸馏格式修复结果", "领域": "知识管理"},
            "background": "格式故障诊断",
            "core_content": "## 核心验证\n\n" + "修复后的结构化内容能够通过生产硬校验。" * 8,
            "boundaries": {"applies": "格式修复", "not_applies": "无关故障"},
            "anti_patterns": [],
            "related_concepts": ["蒸馏合同"],
        }
        diagnostic_evidence = store.execute_diagnostic_reproducer(
            incident_id,
            occurrence_id=records[0].occurrence_id,
            evidence_kind="isolated_reference_failed_to_passed",
            source_refs=(f"raw-revision:{raw_revision_id}",),
            reproducer_id="distillation_fragment_contract.v1",
            before_input={**valid_fragment, "title": "过短"},
            after_input=valid_fragment,
        )
        confirmed_report = store.append_root_cause_report(
            incident_id,
            root_cause_status="confirmed",
            root_cause_code="schema_contract_mismatch",
            evidence_refs=[
                *initial_report["evidence_refs"],
                (
                    "diagnostic-evidence:"
                    f"{diagnostic_evidence['evidence_id']}:"
                    f"{diagnostic_evidence['evidence_hash']}"
                ),
            ],
            reproduction_command="isolated formal replay",
            repair="Align the extraction contract.",
            verification="Require an evidence-bound replay receipt.",
        )

        class _Adapter:
            def deliver(self, payload: dict[str, Any], *, idempotency_key: str) -> str:
                return f"reference:{idempotency_key}:{payload['incident_id']}"

        notification = store.dispatch_next_notification(_Adapter())
        occurrence_id = records[0].occurrence_id
        replay_plan = plan_distillation_failure_replay(
            db_path,
            occurrence_id=occurrence_id,
        )
        replay = execute_distillation_failure_replay(
            db_path,
            occurrence_id=occurrence_id,
            expected_plan_hash=str(replay_plan["plan_hash"]),
            expected_artifact_hash=str(replay_plan["artifact_hash"]),
            raw_db=raw_db,
            runner=lambda _session, _messages, _meta: SimpleNamespace(
                extraction_contract_valid=True,
                judgment="knowledge",
                error="",
                extraction_output_hash="sha256:" + "7" * 64,
                input_spec=SimpleNamespace(
                    visible_input_sha256=visible_hash,
                    source_event_ids=(raw_revision_id,),
                ),
            ),
        )
        resolution = store.resolve_incident(
            incident_id,
            repair_ref="reference:repair",
            verification_ref="reference:verification",
        )
        report = audit_operational_incident_pipeline(
            db_path,
            repo_root=repo_root,
        )
        reference_metrics = {
            "same_root_incident_count": len(incident_ids),
            "occurrence_count": len(store.list_occurrences(incident_id)),
            "diagnostic_task_count": len(store.list_diagnostic_tasks(incident_id)),
            "initial_report_investigating": int(
                initial_report["root_cause_status"] == "investigating"
            ),
            "confirmed_report_revision": int(
                confirmed_report["root_cause_status"] == "confirmed"
                and confirmed_report["report_revision"] == 2
            ),
            "notification_receipt_committed": int(
                notification is not None and notification["status"] == "delivered"
            ),
            "replay_receipt_committed": int(replay["status"] == "committed"),
            "resolution_committed": int(resolution["status"] == "resolved"),
        }
        expected = {
            "same_root_incident_count": 1,
            "occurrence_count": 10,
            "diagnostic_task_count": 1,
            "initial_report_investigating": 1,
            "confirmed_report_revision": 1,
            "notification_receipt_committed": 1,
            "replay_receipt_committed": 1,
            "resolution_committed": 1,
        }
        reference_findings = [
            {
                "metric": name,
                "actual": reference_metrics[name],
                "expected": value,
            }
            for name, value in expected.items()
            if reference_metrics[name] != value
        ]
        return {
            **report,
            "mode": "isolated_reference",
            "ok": report["ok"] and not reference_findings,
            "reference_metrics": reference_metrics,
            "reference_findings": reference_findings,
            "gate_scope": "isolated_runtime_contract",
            "contract_gate_eligible": report["ok"] and not reference_findings,
            "production_effect_verified": False,
            "production_release_eligible": False,
        }


__all__ = [
    "AUDIT_SCHEMA_VERSION",
    "audit_operational_incident_reference",
    "audit_operational_incident_static",
    "audit_operational_incident_pipeline",
]
