"""Read-only planning for legacy distillation-failure incident migration."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from core.ops.offline_migration_lock import offline_migration_lock
from core.cognitive.decision_trace_contracts import MaterialActionRequest
from core.cognitive.decision_trace_material import MaterialActionCoordinator
from core.cognitive.decision_trace_project_contract import (
    authorize_exact_project_contract_action,
)
from core.cognitive.state_store import CognitiveStateStore
from core.ops.operational_incident import (
    SCHEMA_HASH,
    SCHEMA_VERSION,
    DistillationFailureEvidence,
    OperationalIncidentStore,
    initialize_operational_incident_schema,
    validate_operational_incident_schema,
)
from core.ops.operational_incident_reconcile_lifecycle import (
    find_exact_move_receipt,
    find_source_create_receipt,
)
from core.trust.models import sha256_text
from core.trust.config import load_trusted_push_config
from core.trust.vault_mutation_service import (
    TRUSTED_MARKDOWN_ACTION_TYPE,
    TRUSTED_MARKDOWN_EXECUTOR,
    TRUSTED_MARKDOWN_OWNER,
    TrustedVaultMutationResult,
    TrustedVaultMutationService,
    commit_trusted_markdown_move,
    trusted_markdown_material_action_binding,
)
from core.wiki_projection_lifecycle import WikiProjectionLedger
from core.wiki_projection_publisher import publish_wiki_mutation
from core.utils import read_bytes_value, read_text_value

PLAN_SCHEMA_VERSION = "mnemos.operational_incident_reconciliation_plan.v1"
APPLY_SCHEMA_VERSION = "mnemos.operational_incident_reconciliation_apply.v1"


class _ScopedEventConfig:
    """Minimal exact-root config for offline migration event publication."""

    def __init__(self, database_dir: Path):
        self.database_dir = database_dir
        self.data_dir = database_dir
        self.mnemos_dir = database_dir

    @staticmethod
    def get(_key: str, default: Any = None) -> Any:
        return default


def _authorize_reminder_archive(
    *,
    database_dir: Path,
    source_path: Path,
    target_path: Path,
    content: str,
    content_hash: str,
) -> Any:
    """Create one exact project-contract capability for the reviewed archive move."""

    source_hash = sha256_text(content)
    binding = trusted_markdown_material_action_binding(
        target_path=target_path,
        content=content,
        proposed_action="archive migrated legacy distillation reminder",
        expected_existing_hash=None,
        source_path=str(source_path),
        source_content_hash=source_hash,
    )
    state_db = database_dir / "producer_consumer_ledger.db"
    facts = {
        "source_path": str(source_path),
        "target_path": str(target_path),
        "source_content_hash": source_hash,
        "reviewed_content_hash": content_hash,
        "migration_schema": APPLY_SCHEMA_VERSION,
    }
    return authorize_exact_project_contract_action(
        expected_request=MaterialActionRequest(
            owner=TRUSTED_MARKDOWN_OWNER,
            executor_id=TRUSTED_MARKDOWN_EXECUTOR,
            action_type=TRUSTED_MARKDOWN_ACTION_TYPE,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
            expected_state_db=str(state_db),
        ),
        state_db_path=state_db,
        contract_id="operational-incident-reminder-archive",
        contract_revision_id=content_hash,
        contract_text=(
            "Archive exactly one reviewed machine-failure reminder after backup; "
            "preserve Wiki lifecycle identity and do not infer retrospective consumption."
        ),
        source_namespace="operational_incident_reconciliation",
        source_facts=facts,
        decision_checks={
            "source_hash_matches_reviewed_plan": f"sha256:{source_hash}" == content_hash,
            "source_exists": source_path.is_file(),
            "target_absent": not target_path.exists(),
        },
        evidence_refs=(content_hash, f"source:{source_path}", f"target:{target_path}"),
        task="archive reviewed operational incident reminder",
        goal="preserve one-to-one reminder migration disposition",
        constraints=("exact source hash", "target absent", "append-only lifecycle"),
        created_at=datetime.now(timezone.utc).isoformat(),
        producer="operational_incident_reconciliation",
        producer_version=APPLY_SCHEMA_VERSION,
        producer_code_hash=_sha256_bytes(read_bytes_value(Path(__file__))),
        evaluator_id="operational_incident_reminder_archive.v1",
        approved_candidate_key="archive_exact_reviewed_reminder",
        approved_candidate_summary="Move the exact reviewed reminder through trusted lifecycle.",
        rejected_candidate_key="leave_reminder_unchanged",
        rejected_candidate_summary="Do not move when any reviewed binding differs.",
        approved_reason_code="exact_reviewed_archive_contract",
        rejected_reason_code="archive_binding_mismatch",
        committed_metric="operational_incident_reminder_archive_committed",
        rejected_metric="operational_incident_reminder_archive_rejected",
    )


def _archive_reminder_with_lifecycle(
    *,
    database_dir: Path,
    wiki_dir: Path,
    reminder_path: Path,
    archive_path: Path,
    content_hash: str,
    processing_recorder: Callable[[TrustedVaultMutationResult], None] | None = None,
) -> dict[str, Any]:
    """Move one formal reminder through trusted write and Wiki lifecycle owners."""

    content = read_text_value(reminder_path)
    trusted_config = replace(
        load_trusted_push_config(wiki_base=wiki_dir),
        db_path=database_dir / "trusted_push.db",
    )
    material_action = _authorize_reminder_archive(
        database_dir=database_dir,
        source_path=reminder_path,
        target_path=archive_path,
        content=content,
        content_hash=content_hash,
    )
    trusted_result = TrustedVaultMutationService(
        wiki_base=wiki_dir,
        config=trusted_config,
    ).submit_markdown(
        target_path=archive_path,
        content=content,
        source="operational_incident_reconciliation",
        actor="system",
        evidence_refs=(content_hash,),
        proposed_action="archive migrated legacy distillation reminder",
        metadata={
            "migration": APPLY_SCHEMA_VERSION,
            "source_path": str(reminder_path),
            "source_content_hash": sha256_text(content),
            "operation": "move_markdown",
        },
        material_action=material_action,
    )
    if trusted_result.intercepted:
        raise RuntimeError(
            "trusted move is awaiting an enforce-mode proposal decision; archive not applied"
        )
    if processing_recorder is not None:
        processing_recorder(trusted_result)
    return _commit_reminder_with_lifecycle(
        database_dir=database_dir,
        wiki_dir=wiki_dir,
        reminder_path=reminder_path,
        archive_path=archive_path,
        content_hash=content_hash,
        content=content,
        trusted_result=trusted_result,
    )


def _commit_reminder_with_lifecycle(
    *,
    database_dir: Path,
    wiki_dir: Path,
    reminder_path: Path,
    archive_path: Path,
    content_hash: str,
    content: str,
    trusted_result: TrustedVaultMutationResult,
    trusted_effect_already_committed: bool = False,
) -> dict[str, Any]:
    """Idempotently finish the trusted effect and its published Wiki move."""

    from core.mnemos_bus import EventBus

    ledger = WikiProjectionLedger(database_dir / "wiki_projection.db")
    identity = ledger.page_identity(reminder_path)
    move_receipt = find_exact_move_receipt(
        ledger,
        database_dir=database_dir,
        reminder_path=reminder_path,
        archive_path=archive_path,
        content_hash=content_hash,
    )
    event_bus = EventBus(
        config=_ScopedEventConfig(database_dir),
        run_startup_maintenance=False,
        recover_pending=False,
    )
    if identity is None and move_receipt is None:
        if not reminder_path.is_file():
            event_bus.close()
            raise RuntimeError("trusted moved reminder lacks its source Wiki identity")
        create_receipt = ledger.record_mutation(reminder_path, mutation_type="create")
    else:
        create_receipt = find_source_create_receipt(
            ledger,
            database_dir=database_dir,
            reminder_path=reminder_path,
        )
    if create_receipt is not None and not create_receipt.event_trace_id:
        publish_wiki_mutation(
            create_receipt,
            ledger=ledger,
            source="operational_incident_reconciliation",
            event_bus=event_bus,
        )
    moved = trusted_effect_already_committed or commit_trusted_markdown_move(
            trusted_result,
            source_path=reminder_path,
            target_path=archive_path,
            content=content,
        )
    if not moved:
        event_bus.close()
        raise RuntimeError("trusted reminder move did not commit")
    if move_receipt is None:
        move_receipt = ledger.record_mutation(
            archive_path,
            mutation_type="move",
            previous_path=reminder_path,
            expected_content_sha256=content_hash,
        )
    try:
        published = publish_wiki_mutation(
            move_receipt,
            ledger=ledger,
            source="operational_incident_reconciliation",
            event_bus=event_bus,
        )
    finally:
        event_bus.close()
    return {
        "trusted_proposal_id": str(getattr(trusted_result, "proposal_id", "") or ""),
        "material_command_id": trusted_result.material_command_id,
        "mutation_id": move_receipt.mutation_id,
        "event_trace_id": str(published["event_trace_id"]),
    }


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(payload)


def _canonical_hash(value: Any, *, missing_label: str) -> str:
    text = str(value or "").strip().lower()
    if (
        text.startswith("sha256:")
        and len(text) == 71
        and all(char in "0123456789abcdef" for char in text.removeprefix("sha256:"))
    ):
        return text
    if len(text) == 64 and all(char in "0123456789abcdef" for char in text):
        return f"sha256:{text}"
    return f"missing:{missing_label}"


def _artifact_refs(value: str, artifacts: list[dict[str, Any]]) -> list[str]:
    return sorted(
        item["relative_path"]
        for item in artifacts
        if Path(str(item["relative_path"])).name in value
    )


def _artifact_inventory(database_dir: Path) -> list[dict[str, Any]]:
    failed_dir = database_dir / "distill_failed"
    if not failed_dir.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(item for item in failed_dir.iterdir() if item.is_file()):
        raw = read_bytes_value(path)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = {}
        parse_metadata = data.get("parse_metadata")
        metadata = parse_metadata if isinstance(parse_metadata, dict) else {}
        responses = metadata.get("responses")
        latest = (
            responses[-1]
            if isinstance(responses, list) and responses and isinstance(responses[-1], dict)
            else {}
        )
        items.append(
            {
                "relative_path": path.relative_to(database_dir).as_posix(),
                "artifact_hash": _sha256_bytes(raw),
                "session_id_hash": _sha256_json(str(data.get("session_id") or "")),
                "source": str(data.get("source") or "unknown"),
                "failure_class": str(data.get("failure_class") or "legacy_distill_failure"),
                "legacy_error_fingerprint": str(data.get("error_fingerprint") or ""),
                "provider": str(latest.get("provider") or "unobserved"),
                "model": str(latest.get("model") or "unobserved"),
                "input_spec_hash": str(metadata.get("input_spec_hash") or ""),
                "prompt_hash": str(metadata.get("prompt_hash") or ""),
                "classification": "historical_incomplete",
                "incident_ingest_status": str(
                    (data.get("incident_ingest") or {}).get("status") or ""
                )
                if isinstance(data.get("incident_ingest"), dict)
                else "",
            }
        )
    return items


def _historical_recap_inventory(
    database_dir: Path,
    artifacts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    db_path = database_dir / "recap_tasks.db"
    if not db_path.is_file():
        return []
    uri = f"{db_path.resolve(strict=True).as_uri()}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True, timeout=10) as conn:
        exists = conn.execute("""
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='recap_tasks'
            """).fetchone()
        if not exists:
            return []
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(recap_tasks)")}
        required = {"task_id", "topic", "source", "status"}
        if not required.issubset(columns):
            raise RuntimeError("legacy recap schema is not classifiable")
        context_expr = "context" if "context" in columns else "''"
        rows = conn.execute(f"""
            SELECT task_id, topic, source, status, {context_expr} AS context
            FROM recap_tasks
            WHERE source='system'
              AND status IN ('pending','reminded')
              AND (topic LIKE '%蒸馏%' OR {context_expr} LIKE '%distill_failed%')
            ORDER BY task_id
            """).fetchall()  # nosec B608
        result: list[dict[str, Any]] = []
        for row in rows:
            task_id = str(row[0])
            recap_ids = [task_id]
            session_table = conn.execute("""
                SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='retrospective_sessions'
                """).fetchone()
            if session_table:
                recap_ids.extend(
                    str(value[0])
                    for value in conn.execute(
                        """
                        SELECT recap_id FROM retrospective_sessions
                        WHERE task_id=?
                        """,
                        (task_id,),
                    ).fetchall()
                    if str(value[0]).strip()
                )
            recap_ids = list(dict.fromkeys(recap_ids))
            placeholders = ",".join("?" for _ in recap_ids)
            consumption_counts: dict[str, int] = {}
            for table in (
                "recap_consumption_plans",
                "recap_consumption_commands",
                "recap_consumption_receipts",
            ):
                exists = conn.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type='table' AND name=?
                    """,
                    (table,),
                ).fetchone()
                consumption_counts[table] = (
                    int(
                        conn.execute(
                            f"""
                            SELECT COUNT(*) FROM {table}
                            WHERE recap_id IN ({placeholders})
                            """,  # nosec B608
                            tuple(recap_ids),
                        ).fetchone()[0]
                    )
                    if exists
                    else 0
                )
            consumption_counts["retrospective_sessions"] = (
                len(recap_ids) - 1 if session_table else 0
            )
            skip_table = conn.execute("""
                SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='recap_skip_events'
                """).fetchone()
            consumption_counts["recap_skip_events"] = (
                int(
                    conn.execute(
                        "SELECT COUNT(*) FROM recap_skip_events WHERE task_id=?",
                        (task_id,),
                    ).fetchone()[0]
                )
                if skip_table
                else 0
            )
            result.append(
                {
                    "task_id": task_id,
                    "topic_hash": _sha256_json(str(row[1])),
                    "status": str(row[3]),
                    "context_hash": _sha256_json(str(row[4])),
                    "artifact_refs": _artifact_refs(str(row[4]), artifacts),
                    "consumption_counts": consumption_counts,
                    "classification": "legacy_machine_failure_recap",
                }
            )
    return result


def _reminder_inventory(
    wiki_dir: Path | None,
    artifacts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if wiki_dir is None:
        return []
    reminders = Path(wiki_dir) / "08-Reminders"
    if not reminders.is_dir():
        return []
    result: list[dict[str, Any]] = []
    for path in sorted(reminders.rglob("*.md")):
        raw = read_bytes_value(path)
        text = raw.decode("utf-8", errors="ignore")
        if "蒸馏失败" not in text and "distill_failed" not in text:
            continue
        result.append(
            {
                "relative_path": path.relative_to(Path(wiki_dir)).as_posix(),
                "content_hash": _sha256_bytes(raw),
                "artifact_refs": _artifact_refs(text, artifacts),
                "classification": "legacy_notification_projection",
            }
        )
    return result


def _disposition_keys(incident_db: Path) -> set[tuple[str, str, str]]:
    if not incident_db.is_file():
        return set()
    uri = f"{incident_db.resolve(strict=True).as_uri()}?mode=ro&immutable=1"
    try:
        with sqlite3.connect(uri, uri=True, timeout=10) as conn:
            validate_operational_incident_schema(conn)
            rows = conn.execute("""
                SELECT source_type, source_id, content_hash
                FROM legacy_incident_migrations
                WHERE status IN ('migrated','superseded','archived')
                """).fetchall()
            rows.extend(conn.execute("""
                    SELECT source_type, source_id, content_hash
                    FROM legacy_incident_quarantine
                    """).fetchall())
    except (RuntimeError, sqlite3.Error):
        return set()
    return {(str(row[0]), str(row[1]), str(row[2])) for row in rows}


def _open_quarantine_count(incident_db: Path) -> int:
    if not incident_db.is_file():
        return 0
    uri = f"{incident_db.resolve(strict=True).as_uri()}?mode=ro&immutable=1"
    try:
        with sqlite3.connect(uri, uri=True, timeout=10) as conn:
            validate_operational_incident_schema(conn)
            return int(
                conn.execute("SELECT COUNT(*) FROM legacy_incident_quarantine").fetchone()[0]
            )
    except (RuntimeError, sqlite3.Error):
        return 0


def _processing_migration_count(incident_db: Path) -> int:
    if not incident_db.is_file():
        return 0
    uri = f"{incident_db.resolve(strict=True).as_uri()}?mode=ro&immutable=1"
    try:
        with sqlite3.connect(uri, uri=True, timeout=10) as conn:
            validate_operational_incident_schema(conn)
            return int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM legacy_incident_migration_events AS intent
                    LEFT JOIN legacy_incident_migrations AS terminal
                      ON terminal.source_type=intent.source_type
                     AND terminal.source_id=intent.source_id
                     AND terminal.content_hash=intent.content_hash
                    WHERE intent.event_type='processing'
                      AND terminal.migration_id IS NULL
                    """
                ).fetchone()[0]
            )
    except (RuntimeError, sqlite3.Error):
        return 0


def plan_operational_incident_reconciliation(
    database_dir: str | Path,
    *,
    wiki_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Inventory historical evidence without creating DBs, WAL files, or receipts."""

    root = Path(database_dir).expanduser().resolve(strict=False)
    resolved_wiki = Path(wiki_dir).expanduser().resolve(strict=False) if wiki_dir else None
    artifacts = _artifact_inventory(root)
    recaps = _historical_recap_inventory(root, artifacts)
    reminders = _reminder_inventory(
        resolved_wiki,
        artifacts,
    )
    incident_db = root / "operational_incidents.db"
    incident_schema_state = "absent"
    if incident_db.is_file():
        uri = f"{incident_db.resolve(strict=True).as_uri()}?mode=ro&immutable=1"
        try:
            with sqlite3.connect(uri, uri=True, timeout=10) as conn:
                row = conn.execute("""
                    SELECT schema_version, schema_hash
                    FROM operational_incident_schema_registry
                    WHERE component='operational_incident'
                    """).fetchone()
            incident_schema_state = (
                "canonical" if row == (SCHEMA_VERSION, SCHEMA_HASH) else "migration_required"
            )
        except sqlite3.Error:
            incident_schema_state = "migration_required"
    disposed = _disposition_keys(incident_db)
    artifacts = [
        item
        for item in artifacts
        if ("artifact", item["relative_path"], item["artifact_hash"]) not in disposed
    ]
    recaps = [
        item for item in recaps if ("recap", item["task_id"], item["context_hash"]) not in disposed
    ]
    reminders = [
        item
        for item in reminders
        if ("reminder", item["relative_path"], item["content_hash"]) not in disposed
    ]
    plan_core = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "target_schema_version": SCHEMA_VERSION,
        "target_schema_hash": SCHEMA_HASH,
        "database_scope_hash": _sha256_json(str(root)),
        "wiki_scope_hash": _sha256_json(str(resolved_wiki) if resolved_wiki else ""),
        "incident_schema_state": incident_schema_state,
        "artifacts": artifacts,
        "legacy_recaps": recaps,
        "legacy_reminders": reminders,
        "open_quarantine_count": _open_quarantine_count(incident_db),
        "processing_migration_count": _processing_migration_count(incident_db),
    }
    return {
        **plan_core,
        "plan_hash": _sha256_json(plan_core),
        "artifact_count": len(artifacts),
        "legacy_recap_count": len(recaps),
        "legacy_reminder_count": len(reminders),
        "apply_required": bool(
            incident_schema_state != "canonical"
            or artifacts
            or recaps
            or reminders
            or plan_core["processing_migration_count"]
        ),
    }


def _reserve_backup_path(backup_dir: Path, stem: str) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"{stem}.{uuid4().hex}.sqlite"
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    return target


def _backup_database(source: Path, backup_dir: Path) -> dict[str, Any]:
    target = _reserve_backup_path(backup_dir, source.stem)
    with sqlite3.connect(str(source), timeout=10) as source_conn:
        if source_conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError(f"source database integrity failed: {source.name}")
        with sqlite3.connect(str(target), timeout=10) as backup_conn:
            source_conn.backup(backup_conn)
    with sqlite3.connect(str(target), timeout=10) as backup_conn:
        if backup_conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError(f"backup database integrity failed: {target.name}")
    return {
        "source": str(source),
        "backup": str(target),
        "backup_hash": _sha256_bytes(read_bytes_value(target)),
        "integrity": "ok",
    }


def _artifact_evidence(
    database_dir: Path,
    item: dict[str, Any],
) -> DistillationFailureEvidence:
    artifact_path = (database_dir / str(item["relative_path"])).resolve(strict=True)
    raw = read_bytes_value(artifact_path)
    if _sha256_bytes(raw) != item["artifact_hash"]:
        raise RuntimeError("legacy artifact changed after dry-run")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        data = {}
    metadata_value = data.get("parse_metadata")
    metadata = metadata_value if isinstance(metadata_value, dict) else {}
    responses = metadata.get("responses")
    latest = (
        responses[-1]
        if isinstance(responses, list) and responses and isinstance(responses[-1], dict)
        else {}
    )
    raw_response = data.get("raw_response")
    raw_text = raw_response if isinstance(raw_response, str) else ""
    errors_value = data.get("validation_errors")
    validation_errors = (
        tuple(str(value) for value in errors_value)
        if isinstance(errors_value, list)
        else ("historical artifact lacks structured validation errors",)
    )
    from core.hephaestus.distillation_failure import _distill_error_codes

    pending_ingest = (
        isinstance(data.get("incident_ingest"), dict)
        and data["incident_ingest"].get("status") == "pending"
    )
    bindings = {
        "execution_spec_hash": _canonical_hash(
            metadata.get("execution_spec_hash"), missing_label="execution_spec_hash"
        ),
        "prompt_hash": _canonical_hash(
            metadata.get("prompt_hash"), missing_label="prompt_hash"
        ),
        "provider": str(latest.get("provider") or item["provider"] or "missing:provider"),
        "model": str(latest.get("model") or item["model"] or "missing:model"),
        "route": str(
            latest.get("route")
            or metadata.get("failure_path")
            or metadata.get("path")
            or "missing:route"
        ),
        "schema_hash": _canonical_hash(
            metadata.get("schema_hash"), missing_label="schema_hash"
        ),
        "parser_hash": _canonical_hash(
            metadata.get("parser_hash"), missing_label="parser_hash"
        ),
        "validator_hash": _canonical_hash(
            metadata.get("validator_hash"), missing_label="validator_hash"
        ),
        "visible_input_sha256": _canonical_hash(
            metadata.get("visible_input_sha256"), missing_label="visible_input_sha256"
        ),
        "response_hash": _canonical_hash(
            metadata.get("response_hash"), missing_label="response_hash"
        ),
    }
    source_event_refs = tuple(
        str(value) for value in metadata.get("source_event_refs", ()) if str(value).strip()
    )
    missing_evidence = tuple(
        name for name, value in bindings.items() if str(value).startswith("missing:")
    ) + (() if source_event_refs else ("source_event_refs",))
    return DistillationFailureEvidence(
        session_id=str(data.get("session_id") or "historical-unobserved"),
        source_family=str(data.get("source") or item["source"] or "unknown"),
        producer=str(
            data.get("producer")
            or ("distillation_failure_pending_ingest" if pending_ingest else
                "legacy_distillation_failure_migration")
        ),
        severity=str(data.get("severity") or "high"),
        failure_class=str(data.get("failure_class") or "legacy_distill_failure"),
        error_codes=(
            _distill_error_codes(list(validation_errors), metadata)
            if pending_ingest
            else ("historical_incomplete",)
        ),
        validation_errors=validation_errors,
        execution_spec_hash=bindings["execution_spec_hash"],
        prompt_hash=bindings["prompt_hash"],
        provider=bindings["provider"],
        model=bindings["model"],
        route=bindings["route"],
        schema_hash=bindings["schema_hash"],
        parser_hash=bindings["parser_hash"],
        validator_hash=bindings["validator_hash"],
        visible_input_sha256=bindings["visible_input_sha256"],
        response_hash=bindings["response_hash"],
        source_event_refs=source_event_refs,
        artifact_path=artifact_path,
        artifact_hash=str(item["artifact_hash"]),
        artifact_acl="distillation_failure_diagnostic_restricted_v1",
        retention_class="unresolved_incident_hold_v1",
        raw_response_available=bool(raw_text),
        raw_response_length=len(raw_text),
        missing_evidence=missing_evidence,
    )


def _insert_migration(
    conn: sqlite3.Connection,
    *,
    source_type: str,
    source_id: str,
    content_hash: str,
    incident_id: str,
    occurrence_id: str = "",
    target_ref: str = "",
    status: str,
) -> None:
    migration_digest = hashlib.sha256(
        f"{source_type}\0{source_id}\0{content_hash}".encode("utf-8")
    ).hexdigest()
    migration_id = f"legacy-migration-{migration_digest[:24]}"
    conn.execute(
        """
        INSERT OR IGNORE INTO legacy_incident_migrations (
            migration_id, source_type, source_id, content_hash,
            incident_id, occurrence_id, target_ref, status, migrated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            migration_id,
            source_type,
            source_id,
            content_hash,
            incident_id,
            occurrence_id,
            target_ref,
            status,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def _insert_migration_event(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    content_hash: str,
    incident_id: str,
    target_ref: str,
    event_type: str,
) -> None:
    event_digest = hashlib.sha256(
        f"reminder\0{source_id}\0{content_hash}\0{event_type}".encode("utf-8")
    ).hexdigest()
    event_id = f"legacy-migration-event-{event_digest[:24]}"
    conn.execute(
        """
        INSERT OR IGNORE INTO legacy_incident_migration_events (
            event_id, source_type, source_id, content_hash, incident_id,
            target_ref, event_type, created_at
        ) VALUES (?, 'reminder', ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            source_id,
            content_hash,
            incident_id,
            target_ref,
            event_type,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def _processing_target_ref(
    archive_path: Path,
    trusted_result: TrustedVaultMutationResult,
) -> str:
    return json.dumps(
        {
            "archive_path": str(archive_path),
            "material_command_id": trusted_result.material_command_id,
            "material_target_ref": trusted_result.material_target_ref,
            "material_input_hash": trusted_result.material_input_hash,
            "material_effect_db_path": trusted_result.material_effect_db_path,
            "trusted_proposal_id": trusted_result.proposal_id,
            "trusted_mode": trusted_result.mode,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _rehydrate_processing_trusted_result(
    database_dir: Path,
    *,
    payload: dict[str, Any],
    reminder_path: Path,
    archive_path: Path,
    content: str,
) -> TrustedVaultMutationResult:
    command_id = str(payload.get("material_command_id") or "")
    if not command_id:
        raise RuntimeError("processing reminder lacks its material command")
    authorization = MaterialActionCoordinator(
        CognitiveStateStore(database_dir / "producer_consumer_ledger.db")
    ).bind_for_recovery(
        command_id,
        executor_id=TRUSTED_MARKDOWN_EXECUTOR,
    )
    return TrustedVaultMutationResult(
        action="write",
        mode=str(payload.get("trusted_mode") or "off"),
        proposal_id=str(payload.get("trusted_proposal_id") or ""),
        target_path=str(archive_path),
        content_hash=sha256_text(content),
        source_path=str(reminder_path),
        source_content_hash=sha256_text(content),
        proposed_action="archive migrated legacy distillation reminder",
        material_command_id=command_id,
        material_target_ref=str(payload.get("material_target_ref") or ""),
        material_input_hash=str(payload.get("material_input_hash") or ""),
        material_effect_db_path=str(payload.get("material_effect_db_path") or ""),
        material_action=authorization,
    )


def _trusted_move_terminal_proven(
    database_dir: Path,
    *,
    payload: dict[str, Any],
    reminder_path: Path,
    archive_path: Path,
    content: str,
) -> bool:
    effect_db = Path(str(payload.get("material_effect_db_path") or "")).resolve(strict=False)
    expected_effect_db = (database_dir / "trusted_push.db").resolve(strict=False)
    state_db = database_dir / "producer_consumer_ledger.db"
    command_id = str(payload.get("material_command_id") or "")
    if effect_db != expected_effect_db or not effect_db.is_file() or not state_db.is_file():
        return False
    with sqlite3.connect(f"{effect_db.as_uri()}?mode=ro", uri=True, timeout=10) as conn:
        intent = conn.execute(
            """
            SELECT effect_id, target_ref, input_hash, operation, target_path,
                   source_path, desired_content_hash
            FROM trusted_markdown_effect_intents WHERE command_id=?
            """,
            (command_id,),
        ).fetchone()
    if intent is None or tuple(intent[1:]) != (
        str(payload.get("material_target_ref") or ""),
        str(payload.get("material_input_hash") or ""),
        "move",
        str(archive_path.resolve(strict=False)),
        str(reminder_path.resolve(strict=False)),
        sha256_text(content),
    ):
        return False
    with sqlite3.connect(f"{state_db.resolve().as_uri()}?mode=ro", uri=True, timeout=10) as conn:
        terminal = conn.execute(
            """
            SELECT status, target_effect_id
            FROM cognitive_state_effect_receipts WHERE command_id=?
            """,
            (command_id,),
        ).fetchone()
    return terminal == ("committed", str(intent[0]))


def ingest_pending_incident_artifacts(
    database_dir: str | Path,
    *,
    limit: int = 25,
) -> dict[str, int]:
    """Replay new pending failure artifacts into an initialized incident store."""
    root = Path(database_dir).expanduser().resolve(strict=False)
    store = OperationalIncidentStore(root / "operational_incidents.db")
    registered = store.registered_artifact_paths()
    committed = 0
    failed = 0
    for item in _artifact_inventory(root):
        if committed + failed >= max(1, int(limit)):
            break
        artifact_path = (root / str(item["relative_path"])).resolve(strict=False)
        if (
            item.get("incident_ingest_status") != "pending"
            or artifact_path in registered
        ):
            continue
        try:
            store.record_distillation_failure(_artifact_evidence(root, item))
            registered.add(artifact_path)
            committed += 1
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            failed += 1
    return {"committed": committed, "failed": failed}


def _recover_processing_reminder_migrations(incident_db: Path) -> int:
    """Finalize only processing rows whose exact archive effect already exists."""
    database_dir = incident_db.parent
    recovered = 0
    with sqlite3.connect(str(incident_db), timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT intent.source_id, intent.content_hash, intent.target_ref,
                   intent.incident_id
            FROM legacy_incident_migration_events AS intent
            LEFT JOIN legacy_incident_migrations AS terminal
              ON terminal.source_type=intent.source_type
             AND terminal.source_id=intent.source_id
             AND terminal.content_hash=intent.content_hash
            WHERE intent.event_type='processing'
              AND terminal.migration_id IS NULL
            """
        ).fetchall()
        for row in rows:
            try:
                target = json.loads(str(row["target_ref"]))
                archive_path = Path(str(target["archive_path"])).resolve(strict=False)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            source_path = archive_path.parents[2] / str(row["source_id"])
            content_path = source_path if source_path.is_file() else archive_path
            if not content_path.is_file():
                continue
            raw = read_bytes_value(content_path)
            if _sha256_bytes(raw) != str(row["content_hash"]):
                continue
            content = raw.decode("utf-8")
            try:
                trusted_result = _rehydrate_processing_trusted_result(
                    database_dir,
                    payload=target,
                    reminder_path=source_path,
                    archive_path=archive_path,
                    content=content,
                )
                effect_committed = _trusted_move_terminal_proven(
                    database_dir,
                    payload=target,
                    reminder_path=source_path,
                    archive_path=archive_path,
                    content=content,
                )
                lifecycle = _commit_reminder_with_lifecycle(
                    database_dir=database_dir,
                    wiki_dir=archive_path.parents[2],
                    reminder_path=source_path,
                    archive_path=archive_path,
                    content_hash=str(row["content_hash"]),
                    content=content,
                    trusted_result=trusted_result,
                    trusted_effect_already_committed=effect_committed,
                )
            except (OSError, RuntimeError, ValueError, sqlite3.Error):
                continue
            if not _trusted_move_terminal_proven(
                database_dir,
                payload=target,
                reminder_path=source_path,
                archive_path=archive_path,
                content=content,
            ):
                continue
            terminal_target = json.dumps(
                {
                    "archive_path": str(archive_path),
                    **lifecycle,
                    "recovered_from_processing": True,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            _insert_migration(
                conn,
                source_type="reminder",
                source_id=str(row["source_id"]),
                content_hash=str(row["content_hash"]),
                incident_id=str(row["incident_id"]),
                target_ref=terminal_target,
                status="archived",
            )
            _insert_migration_event(
                conn,
                source_id=str(row["source_id"]),
                content_hash=str(row["content_hash"]),
                incident_id=str(row["incident_id"]),
                target_ref=terminal_target,
                event_type="archived",
            )
            recovered += 1
    return recovered


def _record_migration(
    incident_db: Path,
    **fields: Any,
) -> None:
    with sqlite3.connect(str(incident_db), timeout=10) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        _insert_migration(conn, **fields)


def _record_quarantine(
    incident_db: Path,
    *,
    source_type: str,
    source_id: str,
    content_hash: str,
    reason: str,
    evidence: dict[str, Any],
) -> None:
    quarantine_id = (
        "legacy-quarantine-"
        + hashlib.sha256(f"{source_type}\0{source_id}\0{content_hash}".encode("utf-8")).hexdigest()[
            :24
        ]
    )
    with sqlite3.connect(str(incident_db), timeout=10) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO legacy_incident_quarantine (
                quarantine_id, source_type, source_id, content_hash,
                reason, evidence_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                quarantine_id,
                source_type,
                source_id,
                content_hash,
                reason,
                json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def _artifact_migration_binding(
    incident_db: Path,
    source_id: str,
    content_hash: str,
) -> tuple[str, str] | None:
    with sqlite3.connect(str(incident_db), timeout=10) as conn:
        row = conn.execute(
            """
            SELECT incident_id, occurrence_id
            FROM legacy_incident_migrations
            WHERE source_type='artifact' AND source_id=? AND content_hash=?
            """,
            (source_id, content_hash),
        ).fetchone()
    if row is None:
        return None
    return str(row[0]), str(row[1])


def apply_operational_incident_reconciliation(
    database_dir: str | Path,
    *,
    expected_plan_hash: str,
    backup_dir: str | Path,
    wiki_dir: str | Path | None = None,
    daemon_check: Callable[[Path], bool] | None = None,
) -> dict[str, Any]:
    """Apply only an unchanged dry-run plan under the shared offline lock."""
    root = Path(database_dir).expanduser().resolve(strict=False)
    backup_root = Path(backup_dir).expanduser().resolve(strict=False)
    before = plan_operational_incident_reconciliation(root, wiki_dir=wiki_dir)
    if before["plan_hash"] != expected_plan_hash:
        raise RuntimeError("reconciliation plan changed; rerun dry-run")
    if not before["apply_required"]:
        return {
            "schema_version": APPLY_SCHEMA_VERSION,
            "applied": False,
            "expected_plan_hash": expected_plan_hash,
            "backups": [],
            "migrated_artifact_count": 0,
            "superseded_recap_count": 0,
            "archived_reminder_count": 0,
            "recovered_reminder_count": 0,
            "quarantined_recap_count": 0,
            "quarantined_reminder_count": 0,
            "source_disposition_conserved": True,
            "blocked_recap_ids": [],
            "blocked_reminder_paths": [],
            "post_plan": before,
            "ok": before["open_quarantine_count"] == 0,
        }
    lock_kwargs = {"daemon_check": daemon_check} if daemon_check is not None else {}
    backups: list[dict[str, Any]] = []
    migrated_artifacts = 0
    superseded_recaps = 0
    archived_reminders = 0
    quarantined_recaps = 0
    quarantined_reminders = 0
    blocked_recaps: list[str] = []
    blocked_reminders: list[str] = []
    incident_db = root / "operational_incidents.db"
    recap_db = root / "recap_tasks.db"
    with offline_migration_lock(root, **lock_kwargs):
        locked_plan = plan_operational_incident_reconciliation(root, wiki_dir=wiki_dir)
        if locked_plan["plan_hash"] != expected_plan_hash:
            raise RuntimeError("reconciliation plan changed after migration lock")
        if incident_db.is_file():
            backups.append(_backup_database(incident_db, backup_root))
        if recap_db.is_file() and locked_plan["legacy_recaps"]:
            backups.append(_backup_database(recap_db, backup_root))
        initialize_operational_incident_schema(incident_db)
        recovered_reminders = _recover_processing_reminder_migrations(incident_db)
        store = OperationalIncidentStore(incident_db)
        artifact_bindings: dict[str, tuple[str, str]] = {}
        for item in locked_plan["artifacts"]:
            existing = _artifact_migration_binding(
                incident_db,
                str(item["relative_path"]),
                str(item["artifact_hash"]),
            )
            if existing is None:
                try:
                    result = store.record_distillation_failure(_artifact_evidence(root, item))
                    existing = (result.incident_id, result.occurrence_id)
                except sqlite3.IntegrityError:
                    artifact_path = str((root / str(item["relative_path"])).resolve(strict=True))
                    with sqlite3.connect(str(incident_db), timeout=10) as conn:
                        row = conn.execute(
                            """
                            SELECT incident_id, occurrence_id
                            FROM incident_occurrences
                            WHERE artifact_path=? AND artifact_hash=?
                            """,
                            (artifact_path, str(item["artifact_hash"])),
                        ).fetchone()
                    if row is None:
                        raise
                    existing = (str(row[0]), str(row[1]))
                _record_migration(
                    incident_db,
                    source_type="artifact",
                    source_id=str(item["relative_path"]),
                    content_hash=str(item["artifact_hash"]),
                    incident_id=existing[0],
                    occurrence_id=existing[1],
                    status="migrated",
                )
                migrated_artifacts += 1
            artifact_bindings[str(item["relative_path"])] = existing
        for item in locked_plan["legacy_recaps"]:
            refs = list(item["artifact_refs"])
            bindings = {
                artifact_bindings.get(ref)
                or _artifact_migration_binding(
                    incident_db,
                    ref,
                    next(
                        str(artifact["artifact_hash"])
                        for artifact in _artifact_inventory(root)
                        if artifact["relative_path"] == ref
                    ),
                )
                for ref in refs
            }
            bindings.discard(None)
            incident_ids = {binding[0] for binding in bindings if binding is not None}
            consumption_counts = dict(item["consumption_counts"])
            if len(incident_ids) != 1 or any(consumption_counts.values()):
                blocked_recaps.append(str(item["task_id"]))
                _record_quarantine(
                    incident_db,
                    source_type="recap",
                    source_id=str(item["task_id"]),
                    content_hash=str(item["context_hash"]),
                    reason=(
                        "consumption_state_requires_manual_classification"
                        if any(consumption_counts.values())
                        else "artifact_binding_is_not_unique"
                    ),
                    evidence={
                        "artifact_refs": refs,
                        "incident_ids": sorted(incident_ids),
                        "consumption_counts": consumption_counts,
                        "preserved_status": str(item["status"]),
                    },
                )
                quarantined_recaps += 1
                continue
            incident_id = incident_ids.pop()
            with sqlite3.connect(str(incident_db), timeout=10) as conn:
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("ATTACH DATABASE ? AS recap", (str(recap_db),))
                conn.execute("BEGIN IMMEDIATE")
                updated = conn.execute(
                    """
                    UPDATE recap.recap_tasks
                    SET status='superseded_by_operational_incident'
                    WHERE task_id=? AND status IN ('pending','reminded')
                    """,
                    (str(item["task_id"]),),
                )
                if updated.rowcount != 1:
                    raise RuntimeError("legacy recap changed during reconciliation")
                if conn.execute("""
                        SELECT 1 FROM recap.sqlite_master
                        WHERE type='table' AND name='recap_task_events'
                        """).fetchone() is not None:
                    event_id = (
                        "incident-migration-"
                        + hashlib.sha256(str(item["task_id"]).encode("utf-8")).hexdigest()[:24]
                    )
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO recap.recap_task_events (
                            event_id, task_id, action, status, reason, actor, created_at
                        ) VALUES (?, ?, 'superseded', 'superseded_by_operational_incident',
                                  ?, 'operational-incident-reconcile', ?)
                        """,
                        (
                            event_id,
                            str(item["task_id"]),
                            f"migrated_to_operational_incident:{incident_id}",
                            datetime.now(timezone.utc).isoformat(),
                        ),
                    )
                _insert_migration(
                    conn,
                    source_type="recap",
                    source_id=str(item["task_id"]),
                    content_hash=str(item["context_hash"]),
                    incident_id=incident_id,
                    target_ref=f"operational-incident:{incident_id}",
                    status="superseded",
                )
            superseded_recaps += 1
        for item in locked_plan["legacy_reminders"]:
            refs = list(item["artifact_refs"])
            bindings = [
                artifact_bindings.get(ref)
                or _artifact_migration_binding(
                    incident_db,
                    ref,
                    next(
                        str(artifact["artifact_hash"])
                        for artifact in _artifact_inventory(root)
                        if artifact["relative_path"] == ref
                    ),
                )
                for ref in refs
            ]
            incident_ids = {binding[0] for binding in bindings if binding is not None}
            if len(incident_ids) != 1:
                blocked_reminders.append(str(item["relative_path"]))
                _record_quarantine(
                    incident_db,
                    source_type="reminder",
                    source_id=str(item["relative_path"]),
                    content_hash=str(item["content_hash"]),
                    reason="artifact_binding_is_not_unique",
                    evidence={
                        "artifact_refs": refs,
                        "incident_ids": sorted(incident_ids),
                    },
                )
                quarantined_reminders += 1
                continue
            reminder_path = Path(wiki_dir).expanduser().resolve(strict=False) / str(
                item["relative_path"]
            )
            if not reminder_path.is_file():
                blocked_reminders.append(str(item["relative_path"]))
                _record_quarantine(
                    incident_db,
                    source_type="reminder",
                    source_id=str(item["relative_path"]),
                    content_hash=str(item["content_hash"]),
                    reason="source_reminder_missing_at_apply",
                    evidence={"artifact_refs": refs},
                )
                quarantined_reminders += 1
                continue
            reminder_raw = read_bytes_value(reminder_path)
            if _sha256_bytes(reminder_raw) != str(item["content_hash"]):
                raise RuntimeError("legacy reminder changed during reconciliation")
            reminder_backup = backup_root / "legacy-reminders" / str(item["relative_path"])
            reminder_backup.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(
                reminder_backup,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            try:
                view = memoryview(reminder_raw)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
            finally:
                os.close(fd)
            backups.append(
                {
                    "source": str(reminder_path),
                    "backup": str(reminder_backup),
                    "backup_hash": _sha256_bytes(read_bytes_value(reminder_backup)),
                    "integrity": "ok",
                }
            )
            archive_path = (
                Path(wiki_dir).expanduser().resolve(strict=False)
                / "99-Archive"
                / "OperationalIncidentLegacy"
                / f"{str(item['content_hash']).removeprefix('sha256:')[:16]}-{reminder_path.name}"
            )
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            if archive_path.exists():
                raise RuntimeError("legacy reminder archive target already exists")
            incident_id = next(iter(incident_ids))
            with sqlite3.connect(str(incident_db), timeout=10) as conn:
                existing = conn.execute(
                    """
                    SELECT event_type, target_ref
                    FROM legacy_incident_migration_events
                    WHERE source_type='reminder' AND source_id=? AND content_hash=?
                      AND event_type='processing'
                    """,
                    (str(item["relative_path"]), str(item["content_hash"])),
                ).fetchone()
                if existing is not None:
                    raise RuntimeError(
                        "pending reminder migration could not be safely recovered"
                    )

            def record_processing(trusted_result: TrustedVaultMutationResult) -> None:
                with sqlite3.connect(str(incident_db), timeout=10) as processing_conn:
                    _insert_migration_event(
                        processing_conn,
                        source_id=str(item["relative_path"]),
                        content_hash=str(item["content_hash"]),
                        incident_id=incident_id,
                        target_ref=_processing_target_ref(archive_path, trusted_result),
                        event_type="processing",
                    )

            lifecycle = _archive_reminder_with_lifecycle(
                database_dir=root,
                wiki_dir=Path(wiki_dir).expanduser().resolve(strict=False),
                reminder_path=reminder_path,
                archive_path=archive_path,
                content_hash=str(item["content_hash"]),
                processing_recorder=record_processing,
            )
            with sqlite3.connect(str(incident_db), timeout=10) as conn:
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("BEGIN IMMEDIATE")
                terminal_target = json.dumps(
                    {"archive_path": str(archive_path), **lifecycle},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                _insert_migration(
                    conn,
                    source_type="reminder",
                    source_id=str(item["relative_path"]),
                    content_hash=str(item["content_hash"]),
                    incident_id=incident_id,
                    target_ref=terminal_target,
                    status="archived",
                )
                _insert_migration_event(
                    conn,
                    source_id=str(item["relative_path"]),
                    content_hash=str(item["content_hash"]),
                    incident_id=incident_id,
                    target_ref=terminal_target,
                    event_type="archived",
                )
            archived_reminders += 1
        with sqlite3.connect(str(incident_db), timeout=10) as conn:
            validate_operational_incident_schema(conn)
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("operational incident database integrity failed")
        if recap_db.is_file():
            with sqlite3.connect(str(recap_db), timeout=10) as conn:
                if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("recap database integrity failed")
    after = plan_operational_incident_reconciliation(root, wiki_dir=wiki_dir)
    source_total = (
        len(before["artifacts"]) + len(before["legacy_recaps"]) + len(before["legacy_reminders"])
    )
    disposition_total = (
        migrated_artifacts
        + superseded_recaps
        + archived_reminders
        + quarantined_recaps
        + quarantined_reminders
    )
    conserved = source_total == disposition_total
    return {
        "schema_version": APPLY_SCHEMA_VERSION,
        "applied": True,
        "expected_plan_hash": expected_plan_hash,
        "backups": backups,
        "migrated_artifact_count": migrated_artifacts,
        "superseded_recap_count": superseded_recaps,
        "archived_reminder_count": archived_reminders,
        "recovered_reminder_count": recovered_reminders,
        "quarantined_recap_count": quarantined_recaps,
        "quarantined_reminder_count": quarantined_reminders,
        "source_disposition_conserved": conserved,
        "blocked_recap_ids": blocked_recaps,
        "blocked_reminder_paths": blocked_reminders,
        "post_plan": after,
        "ok": (
            conserved
            and not blocked_recaps
            and not blocked_reminders
            and after["open_quarantine_count"] == 0
            and after["processing_migration_count"] == 0
        ),
    }


__all__ = [
    "PLAN_SCHEMA_VERSION",
    "APPLY_SCHEMA_VERSION",
    "apply_operational_incident_reconciliation",
    "plan_operational_incident_reconciliation",
]
