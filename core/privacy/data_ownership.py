# -*- coding: utf-8 -*-
"""User data ownership, export, freeze, and delete contracts."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping
import uuid

from core.privacy.data_ownership_contracts import (  # noqa: F401
    DATA_DELETE_DECISION_CONTRACT_ID,
    DATA_DELETE_DECISION_CONTRACT_REVISION,
    DATA_DELETE_DECISION_CONTRACT_TEXT,
    DATA_DELETE_DECISION_PRODUCER_HASH,
    DATA_DOMAINS,
    DATA_OWNERSHIP_SCHEMA_VERSION,
    DATA_SCOPES,
    DELETE_STATUSES,
    DataDeleteRequest,
    DataDomainInventory,
    DataExportManifest,
    DataFreezeRequest,
    DataSubjectRef,
    DeletionProof,
    _canonical_model_call_ledger_retired_prompt_storage_count,
    _configured_cognitive_graph_db_paths,
    _configured_embedding_cache_db,
    _configured_event_bus_db,
    _configured_observation_db_paths,
    _configured_persona_db_paths,
    _configured_raw_event_db,
    _configured_reflection_db_paths,
    _configured_scoring_db_paths,
    _configured_wiki_metrics_db,
    _configured_wiki_projection_db,
    _count_sqlite_rows,
    _database_dir,
    _deletion_operation_id,
    _hash_text,
    _mnemos_dir,
    _now_iso,
    _redact_persisted_request_payload,
    _vault_dir,
    parse_scope,
)
from core.privacy.data_ownership_delete_adapters import (
    DataOwnershipDeletionAdaptersMixin,
)
from core.privacy.data_ownership_delete_workflow import (
    DataOwnershipDeletionWorkflowMixin,
)


class DataOwnershipManager(
    DataOwnershipDeletionWorkflowMixin,
    DataOwnershipDeletionAdaptersMixin,
):
    """Inventory, export, freeze, and delete facade."""

    def __init__(
        self,
        config: Any,
        *,
        initialize: bool = True,
        event_bus: Any | None = None,
    ):
        self.config = config
        self.event_bus = event_bus
        self.db_path = _mnemos_dir(config) / "data_ownership.db"
        if initialize:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._ensure_schema()

    def _ensure_schema(self) -> None:
        with sqlite3.connect(str(self.db_path), timeout=5) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS data_ownership_requests (
                    request_id TEXT PRIMARY KEY,
                    schema_version TEXT NOT NULL,
                    request_type TEXT NOT NULL,
                    scope_kind TEXT NOT NULL,
                    scope_value_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_data_ownership_scope "
                "ON data_ownership_requests(request_type, scope_kind, status)"
            )

    def _domain_inventory(
        self, *, model_call_ledger_retired_prompt_storage: int | None = None
    ) -> tuple[DataDomainInventory, ...]:
        db_dir = _database_dir(self.config)
        from core.runtime_paths import RuntimePaths

        model_call_ledger = RuntimePaths.from_config(self.config).model_call_ledger_db
        retired_prompt_storage = (
            _canonical_model_call_ledger_retired_prompt_storage_count(model_call_ledger)
            if model_call_ledger_retired_prompt_storage is None
            else max(0, int(model_call_ledger_retired_prompt_storage))
        )
        raw_vault = _vault_dir(self.config, "raw")
        mnemos_vault = _vault_dir(self.config, "mnemos")
        action_ledger = db_dir / "action_ledger.db"
        domains = [
            DataDomainInventory(
                "raw",
                (str(db_dir / "raw_events.db"), str(raw_vault or "")),
                _count_sqlite_rows(db_dir / "raw_events.db", "raw_turns"),
                ("distill", "raw_projection", "search"),
                "export raw rows and retained projection files",
                "block projection and consumer access for matching subject",
                "delete raw rows after freeze, snapshot, and confirmation",
            ),
            DataDomainInventory(
                "wiki",
                (str(mnemos_vault or ""), str(_configured_wiki_projection_db(self.config))),
                (
                    len(list(mnemos_vault.rglob("*.md")))
                    if mnemos_vault and mnemos_vault.exists()
                    else 0
                ),
                ("context_aware_search", "preflight_inject", "obsidian_ui"),
                "export markdown plus source refs",
                "mark source refs frozen and stop rebuild consumption",
                "delete only ACL-authorized, lifecycle-registered pages with typed receipts",
            ),
            DataDomainInventory(
                "embedding_cache",
                (str(_configured_embedding_cache_db(self.config)),),
                _count_sqlite_rows(_configured_embedding_cache_db(self.config), "embedding_cache"),
                ("embedding_index", "context_aware_search", "preflight_inject"),
                "export cache metadata only; never expose cached vector bodies",
                "freeze provider dispatch before cache invalidation",
                "globally flush unattributable embeddings with a typed receipt and after oracle",
            ),
            DataDomainInventory(
                "metadata",
                (str(db_dir / "sync_log.db"), str(_configured_event_bus_db(self.config))),
                _count_sqlite_rows(db_dir / "sync_log.db", "sync_log")
                + _count_sqlite_rows(_configured_event_bus_db(self.config), "events"),
                ("sync", "events", "doctor"),
                "export metadata rows and source summaries",
                "stop scheduler/backfill consumption for matching subject",
                "delete or redact subject metadata while preserving proof hashes",
            ),
            DataDomainInventory(
                "evidence_refs",
                (str(_configured_wiki_metrics_db(self.config)), str(mnemos_vault or "")),
                _count_sqlite_rows(_configured_wiki_metrics_db(self.config), "page_metrics"),
                ("quality_gate", "knowledge_graph", "preflight_inject"),
                "export evidence refs and provenance ids",
                "block source refs from future distillation and retrieval",
                "invalidate derived refs and rebuild affected consumers",
            ),
            DataDomainInventory(
                "persona",
                tuple(str(path) for path in _configured_persona_db_paths(self.config))
                + (str(mnemos_vault or ""),),
                sum(
                    _count_sqlite_rows(path, "profile_signals")
                    for path in _configured_persona_db_paths(self.config)
                ),
                ("persona_prompt", "delivery_router", "preflight_inject"),
                "export persona deltas and evidence refs",
                "exclude matching signals from future prompts",
                "delete matching persona signals and derived aggregates",
            ),
            DataDomainInventory(
                "reflection",
                tuple(str(path) for path in _configured_reflection_db_paths(self.config))
                + (str(mnemos_vault or ""),),
                sum(
                    _count_sqlite_rows(path, "reflection_records")
                    for path in _configured_reflection_db_paths(self.config)
                ),
                ("guard_check", "check_pending_recaps", "preflight_inject"),
                "export reflection records and recap refs",
                "hold reminders and downstream delivery",
                "delete matching reflection records and stale recap links",
            ),
            DataDomainInventory(
                "scoring",
                tuple(str(path) for path in _configured_scoring_db_paths(self.config)),
                sum(
                    _count_sqlite_rows(path, "scorer_training_queue")
                    + _count_sqlite_rows(path, "ground_truth_signals")
                    + _count_sqlite_rows(path, "scorer_models")
                    + _count_sqlite_rows(path, "bayesian_feedback")
                    + _count_sqlite_rows(path, "governed_training_samples")
                    + _count_sqlite_rows(path, "governed_scorer_models")
                    + _count_sqlite_rows(path, "feedback_prompt_state")
                    for path in _configured_scoring_db_paths(self.config)
                ),
                ("scorecard", "quality_gate", "adaptive_config"),
                "export score samples and model refs",
                "exclude subject samples from retraining",
                "exclude governed samples, deactivate model heads, and delete legacy bodies",
            ),
            DataDomainInventory(
                "action_ledger",
                (str(action_ledger),),
                _count_sqlite_rows(action_ledger, "action_ledger"),
                ("doctor", "health", "audit_scripts"),
                "export action summaries with secret redaction",
                "retain ledger but block consumer replay",
                "redact target refs while preserving proof hash",
            ),
            DataDomainInventory(
                "model_call_ledger",
                (str(model_call_ledger),),
                _count_sqlite_rows(model_call_ledger, "model_call_entries")
                + retired_prompt_storage,
                ("cost_audit", "quality_gate", "distill"),
                "export non-reversible model-call metadata only",
                "stop replay/model consumption",
                "delete or redact model-call metadata by subject",
            ),
            DataDomainInventory(
                "consumer_access_log",
                (str(db_dir / "raw_events.db"), str(action_ledger)),
                _count_sqlite_rows(db_dir / "raw_events.db", "raw_access_log"),
                ("privacy_audit", "data_delete", "scorecard"),
                "export consumer access log summaries",
                "block new access for frozen subject",
                "preserve proof-only hash after delete",
            ),
            DataDomainInventory(
                "agent_source_metadata",
                (str(db_dir / "sync_log.db"),),
                _count_sqlite_rows(db_dir / "sync_log.db", "sessions"),
                ("sync", "backfill", "agent_kit"),
                "export agent/session metadata",
                "block backfill and sync for matching subject",
                "delete matching source metadata after trace invalidation",
            ),
            DataDomainInventory(
                "cognitive_state",
                (str(db_dir / "producer_consumer_ledger.db"),),
                _count_sqlite_rows(
                    db_dir / "producer_consumer_ledger.db",
                    "cognitive_state_revisions",
                ),
                ("wiki", "cognitive_graph", "context_aware_search", "preflight_inject"),
                "export only authorized state objects and immutable deletion receipts",
                "block matching state writes and prompt retrieval before body access",
                "enqueue typed tombstones and verify every required projection receipt",
            ),
            DataDomainInventory(
                "observation",
                tuple(str(path) for path in _configured_observation_db_paths(self.config)),
                sum(
                    _count_sqlite_rows(path, "observations")
                    for path in _configured_observation_db_paths(self.config)
                ),
                ("preflight_inject", "reflection", "cognitive_state"),
                "export only ACL-authorized observation objects",
                "block subject observation reads before body hydration",
                "delete ACL-matched observations after state tombstones commit",
            ),
            DataDomainInventory(
                "cognitive_graph",
                tuple(str(path) for path in _configured_cognitive_graph_db_paths(self.config)),
                sum(
                    _count_sqlite_rows(path, "cognitive_relations")
                    + _count_sqlite_rows(path, "canonical_nodes")
                    + _count_sqlite_rows(path, "sync_outbox")
                    for path in _configured_cognitive_graph_db_paths(self.config)
                ),
                ("cognitive_graph", "preflight_inject", "relation_embeddings"),
                "export only ACL-authorized graph objects and opaque receipts",
                "block graph reads and rebuilds for subject-deleted object IDs",
                "delete ACL-matched graph objects with typed receipts",
            ),
        ]
        return tuple(domains)

    def inventory(self) -> dict[str, Any]:
        from core.runtime_paths import RuntimePaths

        model_call_ledger = RuntimePaths.from_config(self.config).model_call_ledger_db
        retired_prompt_storage = _canonical_model_call_ledger_retired_prompt_storage_count(
            model_call_ledger
        )
        domains = self._domain_inventory(
            model_call_ledger_retired_prompt_storage=retired_prompt_storage
        )
        errors: list[str] = []
        for domain in domains:
            errors.extend(domain.validate())
        if retired_prompt_storage:
            errors.append("model_call_ledger_retired_prompt_storage")
        return {
            "schema_version": DATA_OWNERSHIP_SCHEMA_VERSION,
            "status": "ok" if not errors else "degraded",
            "domains": [domain.as_dict() for domain in domains],
            "counts": {
                "domains": len(domains),
                "records": sum(domain.estimated_records for domain in domains),
                "blocked_records": retired_prompt_storage,
            },
            "errors": errors,
        }

    def export(
        self, scope: str, *, dry_run: bool = True, output_dir: Path | None = None
    ) -> DataExportManifest:
        kind, value = parse_scope(scope)
        subject = DataSubjectRef(kind, value)
        export_id = f"export-{uuid.uuid4().hex[:12]}"
        output_path = ""
        domains = self._domain_inventory()
        if not dry_run:
            target_dir = output_dir or (_mnemos_dir(self.config) / "exports")
            target_dir.mkdir(parents=True, exist_ok=True)
            output_path = str(target_dir / f"{export_id}.json")
        manifest = DataExportManifest(
            export_id=export_id,
            subject=subject,
            generated_at=_now_iso(),
            dry_run=dry_run,
            domains=domains,
            output_path=output_path,
            redaction_policy="secret_redacted_summary",
            action_ledger_ref=str(_database_dir(self.config) / "action_ledger.db"),
        )
        if not dry_run:
            Path(output_path).write_text(
                json.dumps(manifest.as_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return manifest

    def _record_request(
        self, request_type: str, subject: DataSubjectRef, payload: Mapping[str, Any]
    ) -> str:
        request_id = str(payload.get("request_id") or f"data-{uuid.uuid4().hex[:12]}")
        persisted_payload = _redact_persisted_request_payload(payload)
        persisted_json = json.dumps(
            persisted_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        immutable = (
            DATA_OWNERSHIP_SCHEMA_VERSION,
            request_type,
            subject.scope_kind,
            _hash_text(subject.scope_value),
            str(payload.get("status", "")),
            persisted_json,
        )
        with sqlite3.connect(str(self.db_path), timeout=5) as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO data_ownership_requests (
                        request_id, schema_version, request_type, scope_kind,
                        scope_value_hash, status, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (request_id, *immutable, str(payload.get("created_at") or _now_iso())),
                )
            except sqlite3.IntegrityError:
                existing = conn.execute(
                    """
                    SELECT schema_version, request_type, scope_kind,
                           scope_value_hash, status, payload_json
                    FROM data_ownership_requests WHERE request_id=?
                    """,
                    (request_id,),
                ).fetchone()
                if existing is None or tuple(existing) != immutable:
                    raise ValueError("data ownership request_id is immutable")
        return request_id

    def freeze(self, scope: str, *, reason: str = "user_request") -> DataFreezeRequest:
        kind, value = parse_scope(scope)
        subject = DataSubjectRef(kind, value)
        # The model-call ledger is the provider-dispatch authority.  Record the
        # local request only after its durable barrier is committed, so a
        # returned ``frozen`` status can never mean "UI-only freeze".
        from core.telemetry.prompt_call_log import ModelCallLedger

        ModelCallLedger.for_config(self.config).freeze_subject_scope(
            subject.scope_kind,
            subject.scope_value,
        )
        request = DataFreezeRequest(
            request_id=f"freeze-{uuid.uuid4().hex[:12]}",
            subject=subject,
            status="frozen",
            created_at=_now_iso(),
            affected_domains=tuple(sorted(DATA_DOMAINS)),
            reason=reason,
            action_ledger_ref=str(_database_dir(self.config) / "action_ledger.db"),
        )
        self._record_request("freeze", subject, request.as_dict())
        return request

    def _has_freeze(self, subject: DataSubjectRef) -> bool:
        with sqlite3.connect(str(self.db_path), timeout=5) as conn:
            row = conn.execute(
                """
                SELECT 1 FROM data_ownership_requests
                WHERE request_type = 'freeze'
                  AND scope_kind = ?
                  AND scope_value_hash = ?
                  AND status = 'frozen'
                LIMIT 1
                """,
                (subject.scope_kind, _hash_text(subject.scope_value)),
            ).fetchone()
        return row is not None

    def create_delete_snapshot(
        self,
        scope: str,
        *,
        retention_days: int = 30,
    ):
        """Create a retained recovery point for one already-frozen subject."""

        kind, value = parse_scope(scope)
        subject = DataSubjectRef(kind, value)
        if not self._has_freeze(subject):
            raise PermissionError("data delete snapshot requires a prior freeze request")
        from core.backup.snapshot_manager import MnemosSnapshotManager

        return MnemosSnapshotManager(self.config).create_data_delete_snapshot(
            scope_kind=subject.scope_kind,
            scope_value=subject.scope_value,
            retention_days=retention_days,
        )


def audit_data_ownership_contract(*, strict: bool = False) -> list[str]:
    errors: list[str] = []
    required_scopes = {
        "all",
        "agent",
        "session",
        "project",
        "path",
        "source",
        "time",
        "wiki_page",
        "persona_signal",
        "raw_event_id",
    }
    if not required_scopes <= DATA_SCOPES:
        errors.append(f"missing data scopes: {sorted(required_scopes - DATA_SCOPES)}")
    required_domains = {
        "raw",
        "wiki",
        "embedding_cache",
        "metadata",
        "evidence_refs",
        "persona",
        "reflection",
        "scoring",
        "action_ledger",
        "model_call_ledger",
        "consumer_access_log",
        "agent_source_metadata",
        "cognitive_state",
        "observation",
        "cognitive_graph",
    }
    if not required_domains <= DATA_DOMAINS:
        errors.append(f"missing data domains: {sorted(required_domains - DATA_DOMAINS)}")
    sample_subject = DataSubjectRef("session", "sample")
    sample_delete = DataDeleteRequest(
        request_id="delete-contract",
        subject=sample_subject,
        status="dry_run_planned",
        created_at=_now_iso(),
        dry_run=True,
        affected_domains=("raw", "wiki", "action_ledger"),
        derived_impacts=("invalidate derived wiki/source refs",),
        requires_freeze=True,
        requires_snapshot=True,
        requires_confirmation=True,
    )
    errors.extend(sample_delete.validate())
    sample_proof = DeletionProof(
        proof_id="proof-contract",
        subject_hash=_hash_text("session:sample"),
        status="verified",
        deleted_at=_now_iso(),
        affected_domains=("raw", "wiki"),
        affected_consumers=("search", "preflight_inject"),
        verification_results={"rows_deleted": 0},
    )
    errors.extend(sample_proof.validate())
    if strict:
        for status in (
            "requested",
            "dry_run_planned",
            "frozen",
            "deleting",
            "deleted",
            "blocked",
            "partially_deleted",
            "verified",
        ):
            if status not in DELETE_STATUSES:
                errors.append(f"missing delete status: {status}")
    return errors


def build_data_ownership_health(config: Any | None = None) -> dict[str, Any]:
    if config is None:
        from core.config import get_config

        config = get_config()
    manager = DataOwnershipManager(config, initialize=False)
    audit_errors = audit_data_ownership_contract(strict=True)
    inventory = manager.inventory()
    errors = audit_errors + list(inventory.get("errors") or [])
    return {
        "schema_version": DATA_OWNERSHIP_SCHEMA_VERSION,
        "status": "ok" if not errors else "degraded",
        "ledger_path": str(manager.db_path),
        "counts": inventory["counts"],
        "domains": inventory["domains"],
        "errors": errors,
    }
