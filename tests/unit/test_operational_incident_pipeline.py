"""Behavior contracts for the operational incident pipeline."""

from pathlib import Path

import hashlib
import pytest
import sqlite3
import stat
from types import SimpleNamespace

from core.ops.operational_incident import (
    DialogReminderIncidentNotificationAdapter,
    DistillationFailureEvidence,
    OperationalIncidentStore,
    initialize_operational_incident_schema,
)
from core.ops.operational_incident_audit import audit_operational_incident_pipeline
from core.ops.operational_incident_reconcile import (
    apply_operational_incident_reconciliation,
    ingest_pending_incident_artifacts,
    plan_operational_incident_reconciliation,
)
from core.ops.operational_incident_replay import (
    execute_distillation_failure_replay,
    plan_distillation_failure_replay,
)
from core.hephaestus.distillation_failure import (
    cleanup_failed_distill,
    record_distillation_failure,
    save_failed_distill,
)


def _valid_diagnostic_fragment() -> dict:
    return {
        "form": "方法论",
        "title": "蒸馏格式契约修复验证方法",
        "frontmatter": {"摘要": "用于验证蒸馏格式修复结果", "领域": "知识管理"},
        "background": "格式故障诊断",
        "core_content": "## 核心验证\n\n" + "修复后的结构化内容能够通过生产硬校验。" * 8,
        "boundaries": {"applies": "格式修复", "not_applies": "无关故障"},
        "anti_patterns": [],
        "related_concepts": ["蒸馏合同"],
    }


def _failure_evidence(*, artifact_path: Path) -> DistillationFailureEvidence:
    try:
        artifact_payload = __import__("json").loads(artifact_path.read_text(encoding="utf-8"))
    except ValueError:
        artifact_payload = {}
    artifact_payload["validation_errors"] = ["标题过短"]
    artifact_payload["fragments"] = [{**_valid_diagnostic_fragment(), "title": "过短"}]
    artifact_path.write_text(
        __import__("json").dumps(artifact_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return DistillationFailureEvidence(
        session_id="session-incident-contract",
        source_family="codex",
        producer="conversation_distillation",
        severity="high",
        failure_class="distill_validation",
        error_codes=("schema_validation_failed",),
        validation_errors=("fragments[0].title is required",),
        execution_spec_hash="sha256:" + "1" * 64,
        prompt_hash="sha256:" + "a" * 64,
        provider="fixture-provider",
        model="fixture-model",
        route="fixture-route",
        schema_hash="sha256:" + "2" * 64,
        parser_hash="sha256:" + "3" * 64,
        validator_hash="sha256:" + "4" * 64,
        visible_input_sha256="sha256:" + "b" * 64,
        response_hash="sha256:" + "c" * 64,
        source_event_refs=("raw-event-fixture",),
        artifact_path=artifact_path,
        artifact_hash="sha256:" + hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
        artifact_acl="distillation_failure_diagnostic_restricted_v1",
        retention_class="unresolved_incident_hold_v1",
        raw_response_available=True,
        raw_response_length=128,
    )


def _diagnostic_proof(
    store: OperationalIncidentStore,
    incident_id: str,
    occurrence_id: str,
    label: str,
) -> str:
    valid = _valid_diagnostic_fragment()
    evidence = store.execute_diagnostic_reproducer(
        incident_id,
        occurrence_id=occurrence_id,
        evidence_kind=label,
        source_refs=(f"test-evidence:{label}",),
        reproducer_id="distillation_fragment_contract.v1",
        before_input={**valid, "title": "过短"},
        after_input=valid,
    )
    return f"diagnostic-evidence:{evidence['evidence_id']}:{evidence['evidence_hash']}"


def test_repeated_distillation_failures_create_one_incident_and_one_diagnostic_task(
    tmp_path,
):
    db_path = tmp_path / "operational_incidents.db"
    initialize_operational_incident_schema(db_path)
    store = OperationalIncidentStore(db_path)

    results = []
    for index in range(10):
        artifact_path = tmp_path / f"failed-{index}.json"
        artifact_path.write_text(f'{{"occurrence": {index}}}', encoding="utf-8")
        results.append(
            store.record_distillation_failure(_failure_evidence(artifact_path=artifact_path))
        )

    incident_ids = {result.incident_id for result in results}
    assert len(incident_ids) == 1
    incident_id = incident_ids.pop()
    assert len(store.list_occurrences(incident_id)) == 10
    assert len(store.list_diagnostic_tasks(incident_id)) == 1
    assert store.list_retrospectives(incident_id) == []


def test_same_session_failures_never_overwrite_their_artifacts(tmp_path):
    first = save_failed_distill(
        session_id="same-second-session",
        fragments=[],
        validation_errors=["title is required"],
        database_dir=tmp_path,
    )
    second = save_failed_distill(
        session_id="same-second-session",
        fragments=[],
        validation_errors=["title is required"],
        database_dir=tmp_path,
    )

    assert first != second
    assert first.is_file()
    assert second.is_file()
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    assert stat.S_IMODE(second.stat().st_mode) == 0o600


def test_uninitialized_incident_store_leaves_durable_pending_ingest_artifact(tmp_path):
    result = record_distillation_failure(
        session_id="pending-ingest-session",
        fragments=[],
        validation_errors=["title is required"],
        database_dir=tmp_path,
        source="codex",
    )

    assert result.incident is None
    payload = __import__("json").loads(result.artifact_path.read_text(encoding="utf-8"))
    assert payload["incident_ingest"] == {
        "schema_version": "mnemos.operational_incident_ingest.v1",
        "status": "pending",
    }
    cleanup = cleanup_failed_distill(tmp_path, ttl_days=0, max_count=0)
    assert cleanup == {
        "removed": 0,
        "remaining": 1,
        "protected": 1,
        "blocked": 1,
    }
    assert result.artifact_path.is_file()


def test_pending_ingest_is_committed_once_with_receipt(tmp_path):
    result = record_distillation_failure(
        session_id="pending-ingest-recovery",
        fragments=[],
        validation_errors=["provider request failed"],
        database_dir=tmp_path,
        source="codex",
        producer="conversation_distillation",
    )
    initialize_operational_incident_schema(tmp_path / "operational_incidents.db")

    first = ingest_pending_incident_artifacts(tmp_path)
    second = ingest_pending_incident_artifacts(tmp_path)

    assert first == {"committed": 1, "failed": 0}
    assert second == {"committed": 0, "failed": 0}
    with sqlite3.connect(tmp_path / "operational_incidents.db") as conn:
        occurrence = conn.execute(
            "SELECT artifact_path FROM incident_occurrences"
        ).fetchall()
        receipts = conn.execute(
            "SELECT occurrence_id, status FROM incident_ingest_receipts"
        ).fetchall()
    assert occurrence == [(str(result.artifact_path.resolve()),)]
    assert len(receipts) == 1
    assert receipts[0][1] == "committed"


def test_diagnostic_reproducer_rejects_self_reported_success(tmp_path):
    db_path = tmp_path / "operational_incidents.db"
    artifact_path = tmp_path / "diagnostic-proof.json"
    artifact_path.write_text("{}", encoding="utf-8")
    initialize_operational_incident_schema(db_path)
    store = OperationalIncidentStore(db_path)
    recorded = store.record_distillation_failure(_failure_evidence(artifact_path=artifact_path))
    store.diagnose_next()

    with pytest.raises(ValueError, match="not derived"):
        store.execute_diagnostic_reproducer(
            recorded.incident_id,
            occurrence_id=recorded.occurrence_id,
            evidence_kind="invalid-self-report",
            source_refs=("test:self-report",),
            reproducer_id="distillation_fragment_contract.v1",
            before_input={
                "form": "方法论",
                "title": "蒸馏格式契约修复验证方法",
                "frontmatter": {"摘要": "用于验证蒸馏格式修复结果", "领域": "知识管理"},
                "background": "格式故障诊断",
                "core_content": "## 核心验证\n\n" + "合法结构化内容。" * 12,
                "boundaries": {},
                "anti_patterns": [],
                "related_concepts": [],
            },
            after_input={
                "form": "方法论",
                "title": "蒸馏格式契约修复验证方法",
                "frontmatter": {"摘要": "用于验证蒸馏格式修复结果", "领域": "知识管理"},
                "background": "格式故障诊断",
                "core_content": "## 核心验证\n\n" + "合法结构化内容。" * 12,
                "boundaries": {},
                "anti_patterns": [],
                "related_concepts": [],
            },
        )

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM incident_diagnostic_evidence").fetchone()[0] == 0


def test_fragment_reproducer_rejects_provider_failure_incident(tmp_path):
    db_path = tmp_path / "operational_incidents.db"
    artifact_path = tmp_path / "provider-diagnostic.json"
    artifact_path.write_text("{}", encoding="utf-8")
    initialize_operational_incident_schema(db_path)
    store = OperationalIncidentStore(db_path)
    evidence = _failure_evidence(artifact_path=artifact_path)
    recorded = store.record_distillation_failure(
        DistillationFailureEvidence(
            **{
                **evidence.__dict__,
                "error_codes": ("provider_failure", "transport_empty"),
            }
        )
    )
    store.diagnose_next()

    with pytest.raises(ValueError, match="not relevant"):
        _diagnostic_proof(
            store,
            recorded.incident_id,
            recorded.occurrence_id,
            "unrelated-provider-proof",
        )


def test_fragment_reproducer_rejects_unrelated_same_class_fixture(tmp_path):
    db_path = tmp_path / "operational_incidents.db"
    artifact_path = tmp_path / "title-diagnostic.json"
    artifact_path.write_text("{}", encoding="utf-8")
    initialize_operational_incident_schema(db_path)
    store = OperationalIncidentStore(db_path)
    recorded = store.record_distillation_failure(
        _failure_evidence(artifact_path=artifact_path)
    )
    store.diagnose_next()
    valid = _valid_diagnostic_fragment()

    with pytest.raises(ValueError, match="not derived"):
        store.execute_diagnostic_reproducer(
            recorded.incident_id,
            occurrence_id=recorded.occurrence_id,
            evidence_kind="unrelated-summary-proof",
            source_refs=("test:unrelated-summary",),
            reproducer_id="distillation_fragment_contract.v1",
            before_input={**valid, "frontmatter": {"摘要": "", "领域": "知识管理"}},
            after_input=valid,
        )


def test_unresolved_incident_evidence_is_not_removed_by_generic_retention(tmp_path):
    artifact_path = save_failed_distill(
        session_id="retention-session",
        fragments=[],
        validation_errors=["title is required"],
        database_dir=tmp_path,
    )
    db_path = tmp_path / "operational_incidents.db"
    initialize_operational_incident_schema(db_path)
    store = OperationalIncidentStore(db_path)
    store.record_distillation_failure(_failure_evidence(artifact_path=artifact_path))

    result = cleanup_failed_distill(tmp_path, ttl_days=0, max_count=0)

    assert result["removed"] == 0
    assert result["protected"] == 1
    assert artifact_path.is_file()


def test_distillation_failure_entrypoint_creates_incident_without_creating_recap(tmp_path):
    initialize_operational_incident_schema(tmp_path / "operational_incidents.db")
    result = record_distillation_failure(
        session_id="direct-path-session",
        fragments=[],
        validation_errors=["structured output: title is required"],
        database_dir=tmp_path,
        source="codex",
        raw_response="{}",
        parse_metadata={
            "failure_path": "contract_rejected",
            "prompt_hash": "sha256:" + "a" * 64,
            "input_spec_hash": "sha256:" + "b" * 64,
            "responses": [
                {
                    "provider": "fixture-provider",
                    "model": "fixture-model",
                    "parse_path": "json",
                }
            ],
        },
    )

    assert result.artifact_path.is_file()
    assert result.incident.incident_id.startswith("incident-")
    assert not (tmp_path / "recap_tasks.db").exists()


def test_empty_transport_response_produces_evidence_backed_diagnosis_before_notification(
    tmp_path,
):
    db_path = tmp_path / "operational_incidents.db"
    artifact_path = tmp_path / "empty-response.json"
    artifact_path.write_text('{"transport_empty": true}', encoding="utf-8")
    initialize_operational_incident_schema(db_path)
    store = OperationalIncidentStore(db_path)
    evidence = _failure_evidence(artifact_path=artifact_path)
    recorded = store.record_distillation_failure(
        DistillationFailureEvidence(
            **{
                **evidence.__dict__,
                "error_codes": ("provider_failure", "transport_empty"),
                "validation_errors": ("provider returned an empty response",),
                "raw_response_available": False,
                "raw_response_length": 0,
            }
        )
    )

    report = store.diagnose_next()

    assert report is not None
    assert report["incident_id"] == recorded.incident_id
    assert report["root_cause_status"] == "investigating"
    assert report["root_cause_code"] == "symptom_provider_empty_response"
    assert f"occurrence:{recorded.occurrence_id}" in report["evidence_refs"]
    assert f"artifact:{evidence.artifact_hash}" in report["evidence_refs"]
    assert "查看原始响应" not in report["repair"]
    commands = store.list_notification_commands(recorded.incident_id)
    assert len(commands) == 1
    assert commands[0]["report_id"] == report["report_id"]


def test_notification_failure_stays_replayable_and_success_writes_one_receipt(tmp_path):
    db_path = tmp_path / "operational_incidents.db"
    artifact_path = tmp_path / "notification.json"
    artifact_path.write_text("{}", encoding="utf-8")
    initialize_operational_incident_schema(db_path)
    store = OperationalIncidentStore(db_path)
    recorded = store.record_distillation_failure(_failure_evidence(artifact_path=artifact_path))
    diagnosis = store.diagnose_next()
    store.append_root_cause_report(
        recorded.incident_id,
        root_cause_status="confirmed",
        root_cause_code="schema_contract_mismatch",
        evidence_refs=[
            *diagnosis["evidence_refs"],
            _diagnostic_proof(
                store,
                recorded.incident_id,
                recorded.occurrence_id,
                "notification-fixture-diagnosis",
            ),
        ],
        reproduction_command="formal replay audit",
        repair="repair audit contract",
        verification="verify audit replay receipt",
    )

    class FailingAdapter:
        def deliver(self, payload, *, idempotency_key):
            raise RuntimeError("notification unavailable")

    class SuccessfulAdapter:
        def deliver(self, payload, *, idempotency_key):
            assert idempotency_key.startswith("notify-")
            return "dialog-reminder:incident-card"

    failed = store.dispatch_next_notification(FailingAdapter())
    assert failed["status"] == "retry"
    assert store.list_notification_receipts(recorded.incident_id) == []
    assert store.list_notification_commands(recorded.incident_id)[0]["status"] == "pending"

    delivered = store.dispatch_next_notification(SuccessfulAdapter())
    assert delivered["status"] == "delivered"
    receipts = store.list_notification_receipts(recorded.incident_id)
    assert len(receipts) == 1
    assert receipts[0]["external_ref"] == "dialog-reminder:incident-card"


def test_dialog_notification_adapter_projects_only_incident_status():
    class Queue:
        def __init__(self):
            self.calls = []

        def enqueue(self, **kwargs):
            self.calls.append(kwargs)
            return "reminder-incident"

    queue = Queue()
    adapter = DialogReminderIncidentNotificationAdapter(queue)
    payload = {
        "incident_id": "incident-123",
        "report_id": "root-cause-123",
        "root_cause_status": "investigating",
        "root_cause_code": "root_cause_unresolved",
        "message": "Operational incident diagnosis is available.",
    }

    external_ref = adapter.deliver(payload, idempotency_key="notify-incident-123")

    assert external_ref == "dialog-reminder:reminder-incident"
    assert len(queue.calls) == 1
    notification = queue.calls[0]
    assert notification["issue_id"] == "incident-123"
    assert "root-cause-123" in notification["content"]
    assert "00-Inbox" not in notification["content"]
    assert "retrospective" not in notification["content"].lower()


def test_incident_requires_formal_replay_before_resolution_and_optional_retrospective(
    tmp_path,
):
    db_path = tmp_path / "operational_incidents.db"
    artifact_path = tmp_path / "replay.json"
    artifact_path.write_text("{}", encoding="utf-8")
    initialize_operational_incident_schema(db_path)
    store = OperationalIncidentStore(db_path)
    recorded = store.record_distillation_failure(_failure_evidence(artifact_path=artifact_path))
    diagnosis = store.diagnose_next()
    store.append_root_cause_report(
        recorded.incident_id,
        root_cause_status="confirmed",
        root_cause_code="schema_contract_mismatch",
        evidence_refs=[
            *diagnosis["evidence_refs"],
            _diagnostic_proof(
                store,
                recorded.incident_id,
                recorded.occurrence_id,
                "resolution-fixture-diagnosis",
            ),
        ],
        reproduction_command="formal replay audit",
        repair="repair audit contract",
        verification="verify audit replay receipt",
    )
    with pytest.raises(RuntimeError, match="committed replay receipt"):
        store.resolve_incident(
            recorded.incident_id,
            repair_ref="commit:phase6-fix",
            verification_ref="test:operational-incident",
        )

    replay = store.create_replay_command(
        recorded.incident_id,
        occurrence_id=recorded.occurrence_id,
    )
    with pytest.raises(ValueError, match="input binding mismatch"):
        store.record_replay_receipt(
            replay["command_id"],
            status="committed",
            output_hash="sha256:" + "8" * 64,
            input_binding_hash="sha256:" + "7" * 64,
            executor="formal_distillation_replay.v1",
        )
    store.record_replay_receipt(
        replay["command_id"],
        status="committed",
        output_hash="sha256:" + "9" * 64,
        input_binding_hash=replay["input_binding_hash"],
        executor="formal_distillation_replay.v1",
    )
    resolved = store.resolve_incident(
        recorded.incident_id,
        repair_ref="commit:phase6-fix",
        verification_ref="test:operational-incident",
    )

    assert resolved["status"] == "resolved"
    assert store.list_retrospectives(recorded.incident_id) == []
    retrospective = store.propose_retrospective(
        recorded.incident_id,
        reusable_lesson="Bind machine failures to evidence before notifying the user.",
    )
    assert retrospective["status"] == "proposed"
    assert len(store.list_retrospectives(recorded.incident_id)) == 1


def test_cluster_identity_ignores_variable_text_but_separates_contract_versions(tmp_path):
    db_path = tmp_path / "operational_incidents.db"
    initialize_operational_incident_schema(db_path)
    store = OperationalIncidentStore(db_path)
    first_artifact = tmp_path / "first.json"
    second_artifact = tmp_path / "second.json"
    third_artifact = tmp_path / "third.json"
    for path in (first_artifact, second_artifact, third_artifact):
        path.write_text("{}", encoding="utf-8")
    first = _failure_evidence(artifact_path=first_artifact)
    same_root = DistillationFailureEvidence(
        **{
            **first.__dict__,
            "session_id": "another-session",
            "validation_errors": ("/tmp/run-a/fragments[1].title is required: variable wording",),
            "artifact_path": second_artifact,
            "artifact_hash": ("sha256:" + hashlib.sha256(second_artifact.read_bytes()).hexdigest()),
        }
    )
    changed_contract = DistillationFailureEvidence(
        **{
            **first.__dict__,
            "artifact_path": third_artifact,
            "artifact_hash": ("sha256:" + hashlib.sha256(third_artifact.read_bytes()).hexdigest()),
            "schema_hash": "sha256:" + "8" * 64,
        }
    )

    first_result = store.record_distillation_failure(first)
    same_result = store.record_distillation_failure(same_root)
    changed_result = store.record_distillation_failure(changed_contract)

    assert same_result.incident_id == first_result.incident_id
    assert changed_result.incident_id != first_result.incident_id


def test_strict_incident_audit_recomputes_the_full_target_store_chain(tmp_path):
    db_path = tmp_path / "operational_incidents.db"
    artifact_path = tmp_path / "audit.json"
    artifact_path.write_text("{}", encoding="utf-8")
    initialize_operational_incident_schema(db_path)
    store = OperationalIncidentStore(db_path)
    recorded = store.record_distillation_failure(_failure_evidence(artifact_path=artifact_path))
    diagnosis = store.diagnose_next()
    store.append_root_cause_report(
        recorded.incident_id,
        root_cause_status="confirmed",
        root_cause_code="schema_contract_mismatch",
        evidence_refs=[
            *diagnosis["evidence_refs"],
            _diagnostic_proof(
                store,
                recorded.incident_id,
                recorded.occurrence_id,
                "audit-fixture-diagnosis",
            ),
        ],
        reproduction_command="formal replay audit",
        repair="repair audit contract",
        verification="verify audit replay receipt",
    )

    class Adapter:
        def deliver(self, payload, *, idempotency_key):
            return "dialog-reminder:audit"

    store.dispatch_next_notification(Adapter())
    replay = store.create_replay_command(
        recorded.incident_id,
        occurrence_id=recorded.occurrence_id,
    )
    store.record_replay_receipt(
        replay["command_id"],
        status="committed",
        output_hash="sha256:" + "c" * 64,
        input_binding_hash=replay["input_binding_hash"],
        executor="formal_distillation_replay.v1",
    )
    store.resolve_incident(
        recorded.incident_id,
        repair_ref="commit:audit",
        verification_ref="test:audit",
    )

    report = audit_operational_incident_pipeline(
        db_path,
        repo_root=Path(__file__).resolve().parents[2],
    )

    assert report["ok"] is True
    assert report["metrics"]["occurrence_without_incident"] == 0
    assert report["metrics"]["incident_without_diagnostic_task"] == 0
    assert report["metrics"]["diagnosis_without_evidence"] == 0
    assert report["metrics"]["delivered_without_receipt"] == 0
    assert report["metrics"]["resolved_without_committed_replay"] == 0
    assert report["metrics"]["unresolved_with_retrospective"] == 0
    assert report["metrics"]["legacy_distill_recap_callsite"] == 0
    assert report["metrics"]["direct_wiki_bypass_advice"] == 0


def test_operational_incident_reconciliation_plan_is_stable_and_read_only(tmp_path):
    artifact_path = save_failed_distill(
        session_id="legacy-session",
        fragments=[],
        validation_errors=["title is required"],
        database_dir=tmp_path,
        source="codex",
    )
    recap_db = tmp_path / "recap_tasks.db"
    with sqlite3.connect(recap_db) as conn:
        conn.execute("""
            CREATE TABLE recap_tasks (
                task_id TEXT PRIMARY KEY,
                topic TEXT NOT NULL,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                context TEXT NOT NULL
            )
            """)
        conn.execute(
            """
            INSERT INTO recap_tasks (task_id, topic, source, status, context)
            VALUES ('legacy-recap', '蒸馏失败：输出格式不合法', 'system', 'pending', ?)
            """,
            (f"artifact={artifact_path.name}",),
        )
    before = recap_db.read_bytes()

    first = plan_operational_incident_reconciliation(tmp_path)
    second = plan_operational_incident_reconciliation(tmp_path)

    assert first["plan_hash"] == second["plan_hash"]
    assert first["artifact_count"] == 1
    assert first["legacy_recap_count"] == 1
    assert first["apply_required"] is True
    assert recap_db.read_bytes() == before
    assert not (tmp_path / "operational_incidents.db").exists()


def test_reconciliation_plan_hash_binds_database_and_wiki_scope(tmp_path):
    first = plan_operational_incident_reconciliation(
        tmp_path / "first-database",
        wiki_dir=tmp_path / "first-wiki",
    )
    second = plan_operational_incident_reconciliation(
        tmp_path / "second-database",
        wiki_dir=tmp_path / "second-wiki",
    )

    assert first["database_scope_hash"] != second["database_scope_hash"]
    assert first["wiki_scope_hash"] != second["wiki_scope_hash"]
    assert first["plan_hash"] != second["plan_hash"]


def test_occurrence_rejects_artifact_hash_that_does_not_match_file(tmp_path):
    db_path = tmp_path / "operational_incidents.db"
    artifact_path = tmp_path / "tampered.json"
    artifact_path.write_text("{}", encoding="utf-8")
    initialize_operational_incident_schema(db_path)
    evidence = _failure_evidence(artifact_path=artifact_path)
    artifact_path.write_text('{"changed": true}', encoding="utf-8")

    with pytest.raises(RuntimeError, match="artifact hash"):
        OperationalIncidentStore(db_path).record_distillation_failure(evidence)


def test_reconciliation_apply_backs_up_and_closes_exact_legacy_recap(tmp_path):
    database_dir = tmp_path / "database"
    backup_dir = tmp_path / "backups"
    artifact_path = save_failed_distill(
        session_id="legacy-apply-session",
        fragments=[],
        validation_errors=["title is required"],
        database_dir=database_dir,
        source="codex",
    )
    recap_db = database_dir / "recap_tasks.db"
    with sqlite3.connect(recap_db) as conn:
        conn.execute("""
            CREATE TABLE recap_tasks (
                task_id TEXT PRIMARY KEY,
                topic TEXT NOT NULL,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                context TEXT NOT NULL
            )
            """)
        conn.execute(
            """
            INSERT INTO recap_tasks (task_id, topic, source, status, context)
            VALUES ('legacy-apply-recap', '蒸馏失败', 'system', 'pending', ?)
            """,
            (f"artifact={artifact_path.name}",),
        )
    plan = plan_operational_incident_reconciliation(database_dir)

    applied = apply_operational_incident_reconciliation(
        database_dir,
        expected_plan_hash=plan["plan_hash"],
        backup_dir=backup_dir,
        daemon_check=lambda _database_dir: True,
    )

    assert applied["ok"] is True
    assert applied["migrated_artifact_count"] == 1
    assert applied["superseded_recap_count"] == 1
    assert len(applied["backups"]) == 1
    assert Path(applied["backups"][0]["backup"]).is_file()
    with sqlite3.connect(recap_db) as conn:
        assert (
            conn.execute(
                "SELECT status FROM recap_tasks WHERE task_id='legacy-apply-recap'"
            ).fetchone()[0]
            == "superseded_by_operational_incident"
        )
    post = plan_operational_incident_reconciliation(database_dir)
    assert post["apply_required"] is False
    second = apply_operational_incident_reconciliation(
        database_dir,
        expected_plan_hash=post["plan_hash"],
        backup_dir=backup_dir,
        daemon_check=lambda _database_dir: True,
    )
    assert second["applied"] is False
    assert second["backups"] == []


def test_reconciliation_quarantines_recap_with_consumption_state(tmp_path):
    database_dir = tmp_path / "database"
    backup_dir = tmp_path / "backups"
    artifact_path = save_failed_distill(
        session_id="legacy-consumed-session",
        fragments=[],
        validation_errors=["title is required"],
        database_dir=database_dir,
        source="codex",
    )
    recap_db = database_dir / "recap_tasks.db"
    with sqlite3.connect(recap_db) as conn:
        conn.executescript("""
            CREATE TABLE recap_tasks (
                task_id TEXT PRIMARY KEY,
                topic TEXT NOT NULL,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                context TEXT NOT NULL
            );
            CREATE TABLE recap_consumption_plans (
                plan_id TEXT PRIMARY KEY,
                recap_id TEXT NOT NULL
            );
            """)
        conn.execute(
            """
            INSERT INTO recap_tasks (task_id, topic, source, status, context)
            VALUES ('legacy-consumed', '蒸馏失败', 'system', 'pending', ?)
            """,
            (f"artifact={artifact_path.name}",),
        )
        conn.execute("""
            INSERT INTO recap_consumption_plans (plan_id, recap_id)
            VALUES ('plan-legacy-consumed', 'legacy-consumed')
            """)
    plan = plan_operational_incident_reconciliation(database_dir)

    applied = apply_operational_incident_reconciliation(
        database_dir,
        expected_plan_hash=plan["plan_hash"],
        backup_dir=backup_dir,
        daemon_check=lambda _database_dir: True,
    )

    assert applied["ok"] is False
    assert applied["quarantined_recap_count"] == 1
    assert applied["source_disposition_conserved"] is True
    assert applied["post_plan"]["legacy_recap_count"] == 0
    assert applied["post_plan"]["open_quarantine_count"] == 1
    with sqlite3.connect(recap_db) as conn:
        assert (
            conn.execute(
                "SELECT status FROM recap_tasks WHERE task_id='legacy-consumed'"
            ).fetchone()[0]
            == "pending"
        )
    with sqlite3.connect(database_dir / "operational_incidents.db") as conn:
        row = conn.execute("""
            SELECT reason, evidence_json FROM legacy_incident_quarantine
            WHERE source_type='recap' AND source_id='legacy-consumed'
            """).fetchone()
    assert row[0] == "consumption_state_requires_manual_classification"
    assert '"recap_consumption_plans": 1' in row[1]


def test_reconciliation_archives_exact_legacy_reminder_with_backup(
    tmp_path,
):
    database_dir = tmp_path / "database"
    backup_dir = tmp_path / "backups"
    wiki_dir = tmp_path / "wiki"
    artifact_path = save_failed_distill(
        session_id="legacy-reminder-session",
        fragments=[],
        validation_errors=["title is required"],
        database_dir=database_dir,
        source="codex",
    )
    reminder = wiki_dir / "08-Reminders" / "legacy-distill.md"
    reminder.parent.mkdir(parents=True)
    reminder.write_text(
        f"# 蒸馏失败\n\nartifact={artifact_path.name}\n",
        encoding="utf-8",
    )
    plan = plan_operational_incident_reconciliation(
        database_dir,
        wiki_dir=wiki_dir,
    )

    applied = apply_operational_incident_reconciliation(
        database_dir,
        expected_plan_hash=plan["plan_hash"],
        backup_dir=backup_dir,
        wiki_dir=wiki_dir,
        daemon_check=lambda _database_dir: True,
    )

    assert applied["ok"] is True
    assert applied["archived_reminder_count"] == 1
    assert applied["source_disposition_conserved"] is True
    assert not reminder.exists()
    archives = list(
        (wiki_dir / "99-Archive" / "OperationalIncidentLegacy").glob("*-legacy-distill.md")
    )
    assert len(archives) == 1
    assert any(Path(item["backup"]).name == "legacy-distill.md" for item in applied["backups"])


def test_reconciliation_recovers_move_completed_before_disposition_commit(
    tmp_path,
    monkeypatch,
):
    import core.ops.operational_incident_reconcile as reconcile

    database_dir = tmp_path / "database"
    backup_dir = tmp_path / "backups"
    wiki_dir = tmp_path / "wiki"
    artifact_path = save_failed_distill(
        session_id="legacy-reminder-crash",
        fragments=[],
        validation_errors=["title is required"],
        database_dir=database_dir,
        source="codex",
    )
    reminder = wiki_dir / "08-Reminders" / "legacy-crash.md"
    reminder.parent.mkdir(parents=True)
    reminder.write_text(f"# 蒸馏失败\n\nartifact={artifact_path.name}\n", encoding="utf-8")
    plan = plan_operational_incident_reconciliation(database_dir, wiki_dir=wiki_dir)
    original_publish = reconcile.publish_wiki_mutation

    def publish_then_crash(receipt, **kwargs):
        if receipt.mutation_type == "move":
            raise RuntimeError("injected crash after trusted move")
        return original_publish(receipt, **kwargs)

    monkeypatch.setattr(reconcile, "publish_wiki_mutation", publish_then_crash)
    with pytest.raises(RuntimeError, match="injected crash"):
        apply_operational_incident_reconciliation(
            database_dir,
            expected_plan_hash=plan["plan_hash"],
            backup_dir=backup_dir,
            wiki_dir=wiki_dir,
            daemon_check=lambda _database_dir: True,
        )
    monkeypatch.setattr(reconcile, "publish_wiki_mutation", original_publish)

    recovery_plan = plan_operational_incident_reconciliation(database_dir, wiki_dir=wiki_dir)
    assert recovery_plan["processing_migration_count"] == 1
    recovered = apply_operational_incident_reconciliation(
        database_dir,
        expected_plan_hash=recovery_plan["plan_hash"],
        backup_dir=backup_dir,
        wiki_dir=wiki_dir,
        daemon_check=lambda _database_dir: True,
    )

    assert recovered["recovered_reminder_count"] == 1
    assert recovered["post_plan"]["processing_migration_count"] == 0
    assert recovered["post_plan"]["apply_required"] is False


def test_reconciliation_recovers_processing_before_physical_move(
    tmp_path,
    monkeypatch,
):
    import core.ops.operational_incident_reconcile as reconcile

    database_dir = tmp_path / "database"
    backup_dir = tmp_path / "backups"
    wiki_dir = tmp_path / "wiki"
    artifact_path = save_failed_distill(
        session_id="legacy-reminder-pre-move-crash",
        fragments=[],
        validation_errors=["title is required"],
        database_dir=database_dir,
        source="codex",
    )
    reminder = wiki_dir / "08-Reminders" / "legacy-pre-move.md"
    reminder.parent.mkdir(parents=True)
    reminder.write_text(f"# 蒸馏失败\n\nartifact={artifact_path.name}\n", encoding="utf-8")
    plan = plan_operational_incident_reconciliation(database_dir, wiki_dir=wiki_dir)
    original_commit = reconcile.commit_trusted_markdown_move

    def crash_before_move(*_args, **_kwargs):
        raise RuntimeError("injected crash before trusted move")

    monkeypatch.setattr(reconcile, "commit_trusted_markdown_move", crash_before_move)
    with pytest.raises(RuntimeError, match="injected crash"):
        apply_operational_incident_reconciliation(
            database_dir,
            expected_plan_hash=plan["plan_hash"],
            backup_dir=backup_dir,
            wiki_dir=wiki_dir,
            daemon_check=lambda _database_dir: True,
        )
    assert reminder.is_file()
    monkeypatch.setattr(reconcile, "commit_trusted_markdown_move", original_commit)

    recovery_plan = plan_operational_incident_reconciliation(database_dir, wiki_dir=wiki_dir)
    assert recovery_plan["processing_migration_count"] == 1
    recovered = apply_operational_incident_reconciliation(
        database_dir,
        expected_plan_hash=recovery_plan["plan_hash"],
        backup_dir=backup_dir,
        wiki_dir=wiki_dir,
        daemon_check=lambda _database_dir: True,
    )

    assert recovered["recovered_reminder_count"] == 1
    assert recovered["post_plan"]["processing_migration_count"] == 0
    assert not reminder.exists()


def test_reconciliation_publishes_parent_create_before_recovered_move(
    tmp_path,
    monkeypatch,
):
    import core.ops.operational_incident_reconcile as reconcile

    database_dir = tmp_path / "database"
    backup_dir = tmp_path / "backups"
    wiki_dir = tmp_path / "wiki"
    artifact_path = save_failed_distill(
        session_id="legacy-reminder-create-publish-crash",
        fragments=[],
        validation_errors=["title is required"],
        database_dir=database_dir,
        source="codex",
    )
    reminder = wiki_dir / "08-Reminders" / "legacy-create-publish.md"
    reminder.parent.mkdir(parents=True)
    reminder.write_text(f"# 蒸馏失败\n\nartifact={artifact_path.name}\n", encoding="utf-8")
    plan = plan_operational_incident_reconciliation(database_dir, wiki_dir=wiki_dir)
    original_publish = reconcile.publish_wiki_mutation

    def crash_before_create_attach(receipt, **kwargs):
        if receipt.mutation_type == "create":
            raise RuntimeError("injected crash before create event attach")
        return original_publish(receipt, **kwargs)

    monkeypatch.setattr(reconcile, "publish_wiki_mutation", crash_before_create_attach)
    with pytest.raises(RuntimeError, match="create event attach"):
        apply_operational_incident_reconciliation(
            database_dir,
            expected_plan_hash=plan["plan_hash"],
            backup_dir=backup_dir,
            wiki_dir=wiki_dir,
            daemon_check=lambda _database_dir: True,
        )
    monkeypatch.setattr(reconcile, "publish_wiki_mutation", original_publish)
    recovery_plan = plan_operational_incident_reconciliation(database_dir, wiki_dir=wiki_dir)
    recovered = apply_operational_incident_reconciliation(
        database_dir,
        expected_plan_hash=recovery_plan["plan_hash"],
        backup_dir=backup_dir,
        wiki_dir=wiki_dir,
        daemon_check=lambda _database_dir: True,
    )

    assert recovered["recovered_reminder_count"] == 1
    with sqlite3.connect(database_dir / "wiki_projection.db") as conn:
        traces = conn.execute(
            "SELECT mutation_type, event_trace_id FROM wiki_mutations ORDER BY sequence_no"
        ).fetchall()
    assert [row[0] for row in traces] == ["create", "move"]
    assert all(row[1] for row in traces)


def test_create_recovery_binds_current_page_identity_after_path_reuse(tmp_path):
    from core.ops.operational_incident_reconcile_lifecycle import (
        find_source_create_receipt,
    )
    from core.wiki_projection_lifecycle import WikiProjectionLedger

    database_dir = tmp_path / "database"
    source = tmp_path / "wiki" / "08-Reminders" / "reused.md"
    archived = tmp_path / "wiki" / "99-Archive" / "old-reused.md"
    source.parent.mkdir(parents=True)
    archived.parent.mkdir(parents=True)
    ledger = WikiProjectionLedger(database_dir / "wiki_projection.db")
    source.write_text("# old reminder", encoding="utf-8")
    old_create = ledger.record_mutation(source, mutation_type="create")
    ledger.attach_event(old_create.mutation_id, "trace-old-create")
    source.replace(archived)
    old_move = ledger.record_mutation(
        archived,
        mutation_type="move",
        previous_path=source,
    )
    ledger.attach_event(old_move.mutation_id, "trace-old-move")
    source.write_text("# new reminder", encoding="utf-8")
    current_create = ledger.record_mutation(source, mutation_type="create")

    selected = find_source_create_receipt(
        ledger,
        database_dir=database_dir,
        reminder_path=source,
    )

    assert selected is not None
    assert selected.mutation_id == current_create.mutation_id
    assert selected.event_trace_id == ""


def test_formal_replay_is_plan_bound_and_writes_terminal_receipt(tmp_path):
    from core.hephaestus.distillation_text import build_session_text
    from core.sync_framework.raw_event_store import RawEventStore

    db_path = tmp_path / "operational_incidents.db"
    artifact_path = tmp_path / "replay-plan.json"
    artifact_path.write_text("{}", encoding="utf-8")
    initialize_operational_incident_schema(db_path)
    store = OperationalIncidentStore(db_path)
    messages = [{"role": "user", "content": "canonical raw replay"}]
    visible_input = build_session_text(messages, lossless=True)
    raw_db = tmp_path / "raw_events.db"
    raw_store = RawEventStore(db_path=raw_db)
    try:
        raw_revision_id = raw_store.upsert_turn(
            source_agent="codex",
            session_id="session-incident-contract",
            turn_number=0,
            user_content="canonical raw replay",
            assistant_content="",
            completeness={"visible_text": "full"},
        )
    finally:
        raw_store.close()
    evidence = _failure_evidence(artifact_path=artifact_path)
    evidence = DistillationFailureEvidence(
        **{
            **evidence.__dict__,
            "visible_input_sha256": (
                "sha256:" + hashlib.sha256(visible_input.encode("utf-8")).hexdigest()
            ),
            "source_event_refs": (raw_revision_id,),
        }
    )
    recorded = store.record_distillation_failure(evidence)
    raw_store = RawEventStore(db_path=raw_db)
    try:
        superseding_revision = raw_store.upsert_turn(
            source_agent="codex",
            session_id="session-incident-contract",
            turn_number=0,
            user_content="newer canonical raw revision",
            assistant_content="",
            completeness={"visible_text": "full"},
        )
    finally:
        raw_store.close()
    assert superseding_revision != raw_revision_id
    store.diagnose_next()
    store.append_root_cause_report(
        recorded.incident_id,
        root_cause_status="confirmed",
        root_cause_code="schema_contract_mismatch",
        evidence_refs=[
            f"occurrence:{recorded.occurrence_id}",
            f"source-event:{raw_revision_id}",
            _diagnostic_proof(
                store,
                recorded.incident_id,
                recorded.occurrence_id,
                "formal-replay-fixture-diagnosis",
            ),
        ],
        reproduction_command="formal replay fixture",
        repair="repair fixture contract",
        verification="verify fixture replay receipt",
    )
    plan = plan_distillation_failure_replay(
        db_path,
        occurrence_id=recorded.occurrence_id,
    )
    substituted_raw_db = tmp_path / "substituted-raw.db"
    RawEventStore(db_path=substituted_raw_db).close()

    with pytest.raises(
        ValueError,
        match="does not contain every bound revision",
    ):
        execute_distillation_failure_replay(
            db_path,
            occurrence_id=recorded.occurrence_id,
            expected_plan_hash=plan["plan_hash"],
            expected_artifact_hash=plan["artifact_hash"],
            raw_db=substituted_raw_db,
            runner=lambda *_args: None,
        )
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM incident_replay_commands").fetchone()[0] == 0

    replay = execute_distillation_failure_replay(
        db_path,
        occurrence_id=recorded.occurrence_id,
        expected_plan_hash=plan["plan_hash"],
        expected_artifact_hash=plan["artifact_hash"],
        raw_db=raw_db,
        runner=lambda _session, _messages, _meta: SimpleNamespace(
            extraction_contract_valid=True,
            judgment="knowledge",
            error="",
            extraction_output_hash="sha256:" + "d" * 64,
            input_spec=SimpleNamespace(
                visible_input_sha256=evidence.visible_input_sha256,
                source_event_ids=(raw_revision_id,),
            ),
        ),
    )

    assert replay["status"] == "committed"
    assert replay["writes_wiki"] is False
    with pytest.raises(RuntimeError, match="terminal receipt"):
        execute_distillation_failure_replay(
            db_path,
            occurrence_id=recorded.occurrence_id,
            expected_plan_hash=plan_distillation_failure_replay(
                db_path,
                occurrence_id=recorded.occurrence_id,
            )["plan_hash"],
            expected_artifact_hash=plan["artifact_hash"],
            raw_db=raw_db,
            runner=lambda *_args: None,
        )


def test_daemon_worker_advances_diagnosis_before_notification(
    tmp_path,
    monkeypatch,
):
    from daemon.operational_incident_service import run_service

    db_path = tmp_path / "operational_incidents.db"
    artifact_path = tmp_path / "daemon-worker.json"
    artifact_path.write_text("{}", encoding="utf-8")
    initialize_operational_incident_schema(db_path)
    store = OperationalIncidentStore(db_path)
    recorded = store.record_distillation_failure(_failure_evidence(artifact_path=artifact_path))
    delivered = []

    def _enqueue(_self, **kwargs):
        delivered.append(kwargs)
        return "daemon-incident-reminder"

    monkeypatch.setattr(
        "core.kia.dialog_reminder.DialogReminderQueue.enqueue",
        _enqueue,
    )
    result = run_service(SimpleNamespace(database_dir=tmp_path), limit=10)

    assert result == {
        "status": "ok",
        "pending_ingest_committed": 0,
        "pending_ingest_failed": 0,
        "diagnosed": 1,
        "notifications_delivered": 1,
        "notification_retries": 0,
    }
    assert len(delivered) == 1
    assert delivered[0]["issue_id"] == recorded.incident_id
    receipts = store.list_notification_receipts(recorded.incident_id)
    assert len(receipts) == 1


def test_investigating_incident_accepts_append_only_confirmed_report_revision(tmp_path):
    db_path = tmp_path / "operational_incidents.db"
    artifact_path = tmp_path / "historical-incomplete.json"
    artifact_path.write_text("{}", encoding="utf-8")
    initialize_operational_incident_schema(db_path)
    store = OperationalIncidentStore(db_path)
    recorded = store.record_distillation_failure(
        _failure_evidence(artifact_path=artifact_path)
    )
    initial = store.diagnose_next()
    assert initial["root_cause_status"] == "investigating"

    with pytest.raises(
        ValueError,
        match="diagnostic proof",
    ):
        store.append_root_cause_report(
            recorded.incident_id,
            root_cause_status="confirmed",
            root_cause_code="schema_contract_mismatch",
            evidence_refs=initial["evidence_refs"],
            reproduction_command="formal replay fixture",
            repair="repair fixture contract",
            verification="verify fixture replay receipt",
        )

    confirmed = store.append_root_cause_report(
        recorded.incident_id,
        root_cause_status="confirmed",
        root_cause_code="schema_contract_mismatch",
        evidence_refs=[
            f"occurrence:{recorded.occurrence_id}",
            "source-event:raw-verified",
            _diagnostic_proof(
                store,
                recorded.incident_id,
                recorded.occurrence_id,
                "verified-schema-contract-diagnosis",
            ),
        ],
        reproduction_command=(
            "python3 scripts/replay_distillation_failure.py "
            f"--occurrence-id {recorded.occurrence_id} --dry-run --json"
        ),
        repair="Align the prompt, schema, parser, and validator contract.",
        verification="Run an evidence-bound formal replay and require a receipt.",
    )

    assert confirmed["report_revision"] == 2
    assert confirmed["root_cause_status"] == "confirmed"
    replay = store.create_replay_command(
        recorded.incident_id,
        occurrence_id=recorded.occurrence_id,
    )
    assert replay["status"] == "pending"


def test_confirmed_root_cause_code_must_match_registered_diagnostic_proof(tmp_path):
    db_path = tmp_path / "operational_incidents.db"
    artifact_path = tmp_path / "wrong-root-code.json"
    artifact_path.write_text("{}", encoding="utf-8")
    initialize_operational_incident_schema(db_path)
    store = OperationalIncidentStore(db_path)
    recorded = store.record_distillation_failure(
        _failure_evidence(artifact_path=artifact_path)
    )
    initial = store.diagnose_next()
    proof = _diagnostic_proof(
        store,
        recorded.incident_id,
        recorded.occurrence_id,
        "wrong-root-code-diagnosis",
    )

    with pytest.raises(ValueError, match="not bound"):
        store.append_root_cause_report(
            recorded.incident_id,
            root_cause_status="confirmed",
            root_cause_code="title_length_contract_bug",
            evidence_refs=[*initial["evidence_refs"], proof],
            reproduction_command="formal replay fixture",
            repair="repair fixture contract",
            verification="verify fixture replay receipt",
        )
