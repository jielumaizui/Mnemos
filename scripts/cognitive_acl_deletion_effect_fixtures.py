"""Hermetic production-owner fixtures for the COG-043 effect matrix."""

from __future__ import annotations

from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterator
from unittest.mock import patch

from core.sync_framework.storage_backend import StorageBackend, StorageResult
from scripts.cognitive_acl_deletion_effect_contracts import (
    EFFECT_MATRIX_SCHEMA_VERSION,
    _ACTION_TARGET,
    _EVENT_TRACE_ID,
    _GRAPH_TARGET,
    _KG_RELATION_TARGET,
    _MODEL_RUN_ID,
    _REFLECTION_ID,
    _SCORE_SESSION_ID,
    _SCORING_EFFECT_KEYS,
    _SESSION_ID,
)


class _MatrixConfig:
    """Small explicit config implementing the production config protocol."""

    def __init__(self, root: Path):
        root = root.resolve(strict=False)
        self.root = root
        self.data_dir = root / "data"
        self.database_dir = self.data_dir / "db"
        self.mnemos_dir = self.database_dir
        self.wiki_dir = root / "wiki"
        self.obsidian_vault_path = root / "raw-vault"
        self.database_dir.mkdir(parents=True, exist_ok=True)
        self.wiki_dir.mkdir(parents=True, exist_ok=True)
        self.obsidian_vault_path.mkdir(parents=True, exist_ok=True)
        self._values = {
            "trusted_push.mode": "off",
            "embedding.use_rerank": False,
            "event_bus.max_retries": 2,
            "event_bus.dispatch_workers": 1,
            "event_bus.retry_base_seconds": 0,
            "event_bus.retry_max_seconds": 0,
            "event_bus.handler_timeout_seconds": 0,
            "llm.provider_prices": {"matrix": {"model": {"input": 0.1, "output": 0.2}}},
        }

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def vault_dir(self, name: str) -> Path:
        if name == "mnemos":
            return self.wiki_dir
        if name == "raw":
            return self.obsidian_vault_path
        raise KeyError(name)


class _DeterministicEmbeddingClient:
    """Non-network vector provider used at the existing client seam."""

    def __init__(self) -> None:
        self.available = True

    def health_check(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "check_mode": "hermetic_deterministic",
            "network_checked": False,
        }

    @staticmethod
    def _vector(text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        vector = [0.0] * 1024
        for index, value in enumerate(digest):
            vector[index] = (float(value) + 1.0) / 256.0
        return vector

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.available:
            raise RuntimeError("injected embedding target failure")
        return [self._vector(text) for text in texts]

    def embed_single(self, text: str) -> list[float]:
        if not self.available:
            raise RuntimeError("injected embedding target failure")
        return self._vector(text)

    def rerank(self, *, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        del query
        return [(index, 1.0) for index in range(min(top_n, len(documents)))]


class _SyncBackend(StorageBackend):
    """Construction-only backend; SyncEngine owns the metadata schema."""

    def save(self, content: str, tags: list[str], title: str) -> list[StorageResult]:
        del content, tags, title
        raise AssertionError("effect matrix never writes through the sync backend")

    def search(self, query: str, limit: int | None = None) -> list[StorageResult]:
        del query, limit
        raise AssertionError("effect matrix never reads through the sync backend")

    def list_by_tags(
        self,
        tags: list[str],
        limit: int | None = None,
    ) -> list[StorageResult]:
        del tags, limit
        raise AssertionError("effect matrix never reads through the sync backend")

    def get_by_id(self, uid: str) -> StorageResult | None:
        del uid
        raise AssertionError("effect matrix never reads through the sync backend")

    def health_check(self) -> dict[str, Any]:
        return {"status": "ok", "mode": "construction_only"}

    def update_tags(
        self,
        uid: str,
        add_tags: list[str] | None = None,
        remove_tags: list[str] | None = None,
    ) -> StorageResult | None:
        del uid, add_tags, remove_tags
        raise AssertionError("effect matrix never writes through the sync backend")


def _required_lastrowid(cursor: sqlite3.Cursor) -> int:
    """Return an INSERT row id or fail the hermetic fixture immediately."""

    if cursor.lastrowid is None:
        raise RuntimeError("effect matrix INSERT did not produce a row id")
    return cursor.lastrowid


@contextmanager
def _effect_matrix_material_action_scope(
    config: _MatrixConfig,
) -> Iterator[None]:
    """Seal exact pre-action decisions for the isolated matrix seed only."""

    from core.cognitive.decision_trace import (
        DecisionCandidateEvaluation,
        DecisionRejectionEvaluation,
        MaterialActionRequest,
        ProjectContractDecisionContext,
        ProjectContractDecisionEvaluation,
        ProjectContractMaterialActionResolver,
        material_action_resolution_scope,
    )
    from core.cognitive.state_contract import sha256_json

    contract_id = "project-contract:cog043-effect-matrix-material-seed"
    contract_revision = "mnemos.cog043_effect_matrix_material_seed.v1"
    contract_text = (
        "The hermetic COG-043 effect matrix may seed only its isolated root, "
        "and every material seed effect requires an exact pre-action decision."
    )
    source_hash = sha256_json(
        {
            "schema_version": EFFECT_MATRIX_SCHEMA_VERSION,
            "root": str(config.root),
            "database_dir": str(config.database_dir),
            "wiki_dir": str(config.wiki_dir),
        }
    )
    source_id = "cog043-effect-matrix-invocation:" + source_hash.split(":", 1)[1][:32]
    source_facts_hash = sha256_json(
        {
            "schema_version": "mnemos.cog043_effect_matrix_source_facts.v1",
            "source_hash": source_hash,
            "root": str(config.root),
        }
    )
    allowed_families = {
        ("cognitive_graph", "cognitive_graph_store", "upsert_relation"),
        ("knowledge_graph", "knowledge_graph", "upsert_relation"),
        (
            "trusted_vault",
            "trusted_vault_mutation_service",
            "formal_markdown_mutation",
        ),
    }

    def evaluate_request(
        request: MaterialActionRequest,
    ) -> ProjectContractDecisionEvaluation:
        """Admit only matrix actions confined to the hermetic fixture root."""

        request_hash = sha256_json(
            {
                "owner": request.owner,
                "executor_id": request.executor_id,
                "action_type": request.action_type,
                "target_ref": request.target_ref,
                "input_hash": request.input_hash,
            }
        )
        request_ref = f"request-binding:{request_hash}"
        facts_ref = f"source-facts:{source_facts_hash}"
        family = (request.owner, request.executor_id, request.action_type)
        absolute_paths = [
            part
            for part in request.target_ref.replace("->", ":").split(":")
            if part.startswith("/")
        ]
        paths_inside = all(
            Path(value).expanduser().resolve(strict=False).is_relative_to(config.root)
            for value in absolute_paths
        )
        approved = (
            family in allowed_families and paths_inside and request.input_hash.startswith("sha256:")
        )
        approved_key = "seed_exact_matrix_effect"
        rejected_key = "reject_unbound_matrix_effect"
        common_refs = (request_ref, facts_ref, source_id)
        return ProjectContractDecisionEvaluation(
            request_binding_hash=request_hash,
            source_facts_hash=source_facts_hash,
            candidates=(
                DecisionCandidateEvaluation(
                    key=approved_key,
                    summary="Seed the exact isolated effect-matrix object.",
                    supporting_evidence=common_refs if approved else (),
                    opposing_evidence=() if approved else common_refs,
                    satisfies_value_keys=("safety", "project_contract"),
                ),
                DecisionCandidateEvaluation(
                    key=rejected_key,
                    summary="Reject an effect outside the reviewed matrix seed.",
                    supporting_evidence=common_refs if not approved else (),
                    opposing_evidence=() if not approved else common_refs,
                    satisfies_value_keys=("safety",),
                ),
            ),
            selection_key=approved_key if approved else rejected_key,
            rejections=(
                DecisionRejectionEvaluation(
                    candidate_key=rejected_key if approved else approved_key,
                    reason_code=(
                        "matrix_seed_binding_verified"
                        if approved
                        else "matrix_seed_binding_rejected"
                    ),
                    evidence_refs=common_refs,
                ),
            ),
            expected_outcomes=(
                {
                    "metric": (
                        "matrix_seed_effect_receipt" if approved else "unbound_matrix_effect_count"
                    ),
                    "operator": "equals",
                    "value": 1 if approved else 0,
                },
            ),
            approval_decision="approved" if approved else "rejected",
            approval_evidence_ref=facts_ref,
        )

    resolver = ProjectContractMaterialActionResolver(
        ProjectContractDecisionContext(
            state_db_path=config.database_dir / "producer_consumer_ledger.db",
            contract_id=contract_id,
            contract_revision_id=contract_revision,
            contract_text=contract_text,
            contract_evidence_ref=f"{contract_id}#{contract_revision}",
            source_id=source_id,
            source_revision_id=f"{source_id}:seed-v1",
            source_content_hash=source_hash,
            source_uri=f"cog043-effect-matrix://{source_hash.split(':', 1)[1][:16]}",
            evidence_refs=(source_id, "audit:COG-043"),
            task="Seed the isolated COG-043 physical-effect matrix",
            goal="Create exact disposable objects for deletion and resurrection checks.",
            constraints=(
                f"All durable targets must remain under {config.root}.",
                "Every material seed effect requires an exact permit.",
            ),
            created_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            scope_prefix="cog043-effect-matrix-seed",
            producer="cognitive-acl-deletion-effect-matrix",
            producer_version=contract_revision,
            producer_code_hash=sha256_json(
                {
                    "module": "scripts.cognitive_acl_deletion_effect_matrix",
                    "schema_version": EFFECT_MATRIX_SCHEMA_VERSION,
                    "contract": contract_revision,
                }
            ),
            evaluator_id="cog043-effect-matrix-seed-evaluator",
            evaluator=evaluate_request,
        )
    )
    with material_action_resolution_scope(resolver):
        yield


def _access_control() -> dict[str, Any]:
    from core.cognitive.access_control import make_cognitive_access_envelope

    return make_cognitive_access_envelope(
        owner_principal_id="audit:cog043-effect-matrix",
        owner_agent="codex",
        scope_type="session",
        scope_id=_SESSION_ID,
        session_id=_SESSION_ID,
        project="mnemos",
        purposes=(
            "cognitive_graph_read",
            "cognitive_state_read",
            "data_delete",
            "persona_preflight_read",
            "persona_summary_read",
            "persona_usage_metrics",
            "reflection_experience_read",
            "reflection_export",
            "reflection_feedback",
            "reflection_prompt",
            "reflection_read",
        ),
        consent_provenance_refs=("sha256:" + "a" * 64,),
        sensitivity="sensitive",
        retention_policy="cog043_effect_matrix",
        source_acl_lineage=("sha256:" + "b" * 64,),
        visibility="private",
    )


def _write_wiki_page(config: _MatrixConfig) -> Path:
    page = config.wiki_dir / "00-Inbox" / "cog043-subject.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    body = (
        "This hermetic page represents a derived cognitive object whose source, "
        "permissions, projections, cache entries, graph nodes, metrics, and search "
        "vectors must disappear together. The text is deliberately long enough to "
        "exercise the real chunked ANN index rather than an empty-page shortcut. "
        "No production content or network credential enters this fixture."
    )
    page.write_text(
        "\n".join(
            (
                "---",
                "标题: COG-043 对象级删除矩阵",
                "领域: Mnemos隐私",
                "摘要: 验证对象级删除传播的隔离认知页面",
                "scope: private",
                "source_agent: codex",
                f"session_id: {_SESSION_ID}",
                "project: mnemos",
                "acl_schema_version: 1",
                "acl_metadata_complete: true",
                "acl_reconciliation_status: server_principal",
                "---",
                "# COG-043 object deletion matrix",
                "",
                body,
            )
        ),
        encoding="utf-8",
    )
    return page


def _seed_relation_embedding_target(
    config: _MatrixConfig,
    *,
    page: Path,
    embedding_client: Any,
) -> dict[str, Any]:
    """Create one durable relation vector whose lineage includes the subject page."""

    from core.kia.knowledge_graph import KnowledgeGraph
    from core.kia.relation_schema import Relation, RelationType

    rel_path = page.relative_to(config.wiki_dir).as_posix()
    kg_path = config.database_dir / "knowledge_graph.db"
    graph = KnowledgeGraph(
        db_path=str(kg_path),
        wiki_base=str(config.wiki_dir),
        embedding_index_dir=str(config.database_dir / "embedding_index"),
        embedding_client=embedding_client,
        config=config,
    )
    try:
        created = graph.add_relation(
            Relation(
                source=rel_path,
                target=_KG_RELATION_TARGET,
                relation_type=RelationType.DEPENDS_ON,
                context=(
                    f"{rel_path} is the private source for {_KG_RELATION_TARGET}; "
                    "the relation and its durable vector must follow subject deletion."
                ),
                confidence=1.0,
            )
        )
        if not created or not graph._rel_emb_mgr.flush():
            raise RuntimeError("failed to seed the relation embedding deletion target")
    finally:
        graph.close()
    with sqlite3.connect(kg_path) as conn:
        row = conn.execute(
            """SELECT relation.id
               FROM relations AS relation
               JOIN relation_context_embeddings AS embedding
                 ON embedding.relation_id=relation.id
               WHERE relation.source=? AND relation.target=?""",
            (rel_path, _KG_RELATION_TARGET),
        ).fetchone()
    if row is None:
        raise RuntimeError("relation embedding target was not durably materialized")
    return {
        "kg_relation_id": int(row[0]),
        "kg_relation_source": rel_path,
        "kg_relation_target": _KG_RELATION_TARGET,
    }


def _relation_embedding_target_status(
    config: _MatrixConfig,
    *,
    seeded: dict[str, Any],
) -> dict[str, int]:
    relation_id = int(seeded["kg_relation_id"])
    kg_path = config.database_dir / "knowledge_graph.db"
    return {
        "relation_count": _sql_count(
            kg_path,
            "SELECT COUNT(*) FROM relations WHERE id=?",
            (relation_id,),
        ),
        "embedding_count": _sql_count(
            kg_path,
            "SELECT COUNT(*) FROM relation_context_embeddings WHERE relation_id=?",
            (relation_id,),
        ),
        "outbox_count": _sql_count(
            kg_path,
            "SELECT COUNT(*) FROM kg_embedding_outbox WHERE relation_id=?",
            (relation_id,),
        ),
    }


def _wait_for_bus(bus: Any, *, timeout: float = 30.0) -> dict[str, int]:
    deadline = time.monotonic() + timeout
    stable = 0
    last: dict[str, int] = {}
    while time.monotonic() < deadline:
        last = bus.stats()
        idle = last["pending"] == 0 and last["processing"] == 0 and last["queue_depth"] == 0
        stable = stable + 1 if idle else 0
        if stable >= 3:
            return last
        time.sleep(0.03)
    raise TimeoutError(f"EventBus did not drain in the effect matrix: {last}")


def _blocked_domain_result(domain: str) -> dict[str, Any]:
    result = {
        "status": "blocked",
        "target_count": 1,
        "verified": False,
        "error": f"injected_{domain}_target_failure",
    }
    if domain == "model_call_ledger":
        result.update(
            {
                "matched_run_count": 1,
                "deleted_entry_count": 0,
                "deleted_run_count": 0,
            }
        )
    return result


def _inject_domain_failure(stack: ExitStack, manager_type: Any, domain: str) -> None:
    method_by_domain = {
        "raw": "_apply_raw_subject_deletion",
        "wiki": "_apply_wiki_subject_deletion",
        "embedding_cache": "_apply_embedding_cache_subject_deletion",
        "metadata": "_apply_event_metadata_subject_deletion",
        "evidence_refs": "_apply_evidence_ref_deletion",
        "persona": "_apply_persona_subject_deletion",
        "reflection": "_apply_reflection_subject_deletion",
        "scoring": "_apply_scoring_subject_deletion",
        "action_ledger": "_apply_action_ledger_subject_deletion",
        "consumer_access_log": "_consumer_access_log_result",
        "agent_source_metadata": "_apply_agent_source_metadata_subject_deletion",
        "cognitive_state": "_plan_cognitive_state_tombstone",
        "observation": "_apply_observation_subject_deletion",
        "cognitive_graph": "_apply_cognitive_graph_subject_deletion",
    }
    if domain == "model_call_ledger":
        stack.enter_context(
            patch(
                "core.telemetry.prompt_call_log.ModelCallLedger.delete_subject_scope",
                return_value=_blocked_domain_result(domain),
            )
        )
        return
    method = method_by_domain.get(domain)
    if method is None:
        raise ValueError(f"unsupported COG-043 domain failure: {domain}")
    stack.enter_context(
        patch.object(
            manager_type,
            method,
            return_value=_blocked_domain_result(domain),
        )
    )


def _inject_wiki_consumer_failure(bus: Any, consumer: str, outcome_type: Any) -> None:
    """Replace one registered consumer with a deterministic retry outcome."""

    event_type = "wiki_page_updated"
    with bus._handlers_lock:
        handlers = bus._handlers.get(event_type, [])
        for index, handler in enumerate(handlers):
            key = (event_type, id(handler))
            if bus._handler_consumer_ids.get(key) != consumer:
                continue

            def injected_failure(_event: Any, *, _consumer: str = consumer):
                return outcome_type.retry(
                    _consumer,
                    f"injected {_consumer} target failure",
                )

            handlers[index] = injected_failure
            bus._handler_consumer_ids.pop(key, None)
            bus._handler_consumer_ids[(event_type, id(injected_failure))] = consumer
            return
    raise RuntimeError(f"required Wiki consumer was not registered: {consumer}")


def _seed_cognitive_state(
    config: _MatrixConfig,
    access_control: dict[str, Any],
) -> tuple[Any, str, dict[str, Any]]:
    from core.cognitive.state_contract import (
        CognitiveStateRevision,
        LocalConsumerCommand,
        sha256_json,
    )
    from core.cognitive.state_schema import initialize_cognitive_state_schema
    from core.cognitive.state_store import CognitiveStateStore
    from core.ops.cognitive_data_contract import CognitiveDataEvent

    initialize_cognitive_state_schema(config.database_dir / "producer_consumer_ledger.db")
    store = CognitiveStateStore(config)
    content_hash = sha256_json({"fixture": "cog043-effect-matrix"})
    revision = CognitiveStateRevision.create(
        object_type="cognitive_update_receipt",
        object_id="cog043-effect-matrix-state",
        source_event_id="cde-cog043-effect-matrix",
        source_revision_id="raw-cog043-effect-matrix",
        source_content_hash=content_hash,
        scope_type="session",
        scope_id=_SESSION_ID,
        evidence_refs=("raw-event:cog043-effect-matrix#0:1",),
        payload={
            "input_refs": ["raw-event:cog043-effect-matrix#0:1"],
            "attribution": {"action": "effect_matrix"},
            "target_command_ref": "command:cog043-effect-matrix",
            "before_hash": sha256_json({"before": "matrix"}),
            "after_hash": sha256_json({"after": "matrix"}),
            "effect_receipt_ref": "pending",
            "access_control": access_control,
        },
    )
    event = CognitiveDataEvent(
        event_id=revision.source_event_id,
        source_id=revision.source_revision_id,
        asset_id="asset-cog043-effect-matrix",
        source_kind="audit",
        source_uri="audit://cog043/effect-matrix",
        content_hash=content_hash,
        canonical_subject=revision.object_id,
        data_type=revision.object_type,
        producer="audit",
        intended_consumers=("wiki",),
        privacy_level="private",
        confidence=1.0,
        evidence_refs=revision.evidence_refs,
        dedupe_key="cog043-effect-matrix-state",
        created_at="2026-07-17T00:00:00+00:00",
    )
    command = LocalConsumerCommand.create(
        revision_id=revision.revision_id,
        consumer_id="wiki",
        command_type="project_cognition_update_receipt",
        payload={"projection": "wiki"},
    )
    store.unit_of_work().commit(revisions=(revision,), event=event, commands=(command,))

    corrected_content_hash = sha256_json({"fixture": "cog043-effect-matrix", "correction": 1})
    corrected = CognitiveStateRevision.create(
        object_type=revision.object_type,
        object_id=revision.object_id,
        source_event_id="cde-cog043-effect-matrix-correction",
        source_revision_id="raw-cog043-effect-matrix-correction",
        source_content_hash=corrected_content_hash,
        scope_type="session",
        scope_id=_SESSION_ID,
        evidence_refs=("raw-event:cog043-effect-matrix-correction#0:1",),
        payload={
            "input_refs": ["raw-event:cog043-effect-matrix-correction#0:1"],
            "attribution": {
                "action": "explicit_user_correction",
                "correction_of": revision.revision_id,
            },
            "target_command_ref": "command:cog043-effect-matrix-correction",
            "before_hash": revision.payload_hash,
            "after_hash": sha256_json({"after": "corrected-matrix"}),
            "effect_receipt_ref": "pending",
            "access_control": access_control,
        },
        supersedes_revision_id=revision.revision_id,
        correction_of_revision_id=revision.revision_id,
        created_at="2026-07-17T00:00:01+00:00",
    )
    corrected_event = CognitiveDataEvent(
        event_id=corrected.source_event_id,
        source_id=corrected.source_revision_id,
        asset_id="asset-cog043-effect-matrix-correction",
        source_kind="explicit_user_correction",
        source_uri="audit://cog043/effect-matrix/correction",
        content_hash=corrected_content_hash,
        canonical_subject=corrected.object_id,
        data_type=corrected.object_type,
        producer="audit",
        intended_consumers=("wiki",),
        privacy_level="private",
        confidence=1.0,
        evidence_refs=corrected.evidence_refs,
        dedupe_key="cog043-effect-matrix-state-correction",
        created_at="2026-07-17T00:00:01+00:00",
    )
    corrected_command = LocalConsumerCommand.create(
        revision_id=corrected.revision_id,
        consumer_id="wiki",
        command_type="project_cognition_update_receipt",
        payload={"projection": "wiki", "correction": True},
        created_at=corrected.created_at,
    )
    store.unit_of_work().commit(
        revisions=(corrected,),
        event=corrected_event,
        commands=(corrected_command,),
    )
    current = store.current_revision(corrected.object_type, corrected.object_id)
    correction = {
        "original_revision_id": revision.revision_id,
        "corrected_revision_id": corrected.revision_id,
        "original_revision_preserved": store.revision(revision.revision_id) is not None,
        "corrected_revision_was_current": (
            current is not None and current.revision_id == corrected.revision_id
        ),
        "correction_link_exact": (
            corrected.supersedes_revision_id == revision.revision_id
            and corrected.correction_of_revision_id == revision.revision_id
        ),
    }
    return store, corrected.revision_id, correction


def _commit_cognitive_tombstone(store: Any) -> None:
    commands = [
        item
        for item in store.pending_commands()
        if item["command_type"] == "tombstone_cognitive_state"
    ]
    if len(commands) != 1:
        raise RuntimeError(f"expected one cognitive tombstone command, got {len(commands)}")
    command = commands[0]
    payload = command["payload"]
    store.record_effect_receipt(
        command["command_id"],
        status="committed",
        target_effect_id=f"tombstone:wiki:{payload['request_id']}",
        before_hash=payload["before_hash"],
        after_hash=payload["tombstone_hash"],
        evidence_refs=(
            f"tombstone-command:{command['command_id']}",
            f"tombstone-oracle:wiki:{payload['tombstone_hash']}",
        ),
    )


def _seed_historical_scoring_feedback_fixture(
    scoring_path: Path,
    *,
    feedback_event_id: str,
    access_control: dict[str, Any],
) -> None:
    """Create one historical feedback object for the COG-043 delete oracle.

    COG-038 retired the runtime reaction-to-scorer writer.  COG-043 must still
    prove that an already persisted historical object is deleted and cannot be
    resurrected, so this audit fixture writes the exact historical body and
    its production object-level provenance in one transaction.  It does not
    create ground truth or a training sample.
    """

    from core.scoring.subject_provenance import record_scoring_subject_provenance

    with sqlite3.connect(scoring_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scorer_feedback_events (
                feedback_event_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                dimension TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """)
        conn.execute(
            """
            INSERT INTO scorer_feedback_events (
                feedback_event_id, session_id, dimension, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                feedback_event_id,
                _SCORE_SESSION_ID,
                "profile",
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        record_scoring_subject_provenance(
            conn,
            object_type="feedback_event",
            object_id=feedback_event_id,
            subject_provenance=access_control,
        )


def _seed_historical_scoring_training_fixture(
    scoring_path: Path,
    *,
    access_control: dict[str, Any],
) -> dict[str, int]:
    """Seed historical COG-048 objects only for the COG-043 delete oracle."""

    from core.scoring.subject_provenance import (
        ensure_scoring_subject_provenance_schema,
        record_scoring_derived_object,
        record_scoring_subject_provenance,
    )

    with sqlite3.connect(scoring_path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS ground_truth_signals (
                id INTEGER PRIMARY KEY,
                profile_id TEXT,
                session_id TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                signal_value TEXT,
                confidence REAL,
                latency_hours INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(session_id, signal_type) ON CONFLICT REPLACE
            );
            CREATE TABLE IF NOT EXISTS scorer_training_queue (
                id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                dimension TEXT NOT NULL,
                features_json TEXT NOT NULL,
                priority INTEGER DEFAULT 0,
                earliest_train_at TEXT,
                status TEXT DEFAULT 'pending',
                retry_count INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS scorer_models (
                id INTEGER PRIMARY KEY,
                dimension TEXT NOT NULL,
                model_version TEXT NOT NULL,
                model_type TEXT NOT NULL,
                model_blob BLOB NOT NULL,
                model_hash TEXT,
                train_samples INTEGER,
                is_active INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                meta_json TEXT
            );
            CREATE TABLE IF NOT EXISTS bayesian_scorer_state (
                dimension TEXT PRIMARY KEY,
                alpha REAL NOT NULL,
                beta REAL NOT NULL,
                prior_alpha REAL NOT NULL,
                prior_beta REAL NOT NULL,
                total_samples INTEGER DEFAULT 0,
                neg_likelihood REAL DEFAULT 0.3,
                last_updated TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bayesian_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dimension TEXT NOT NULL,
                is_positive INTEGER NOT NULL,
                weight REAL DEFAULT 1.0,
                context_json TEXT,
                created_at TEXT NOT NULL
            );
            """)
        ensure_scoring_subject_provenance_schema(conn)
        now = datetime.now(timezone.utc).isoformat()
        ground_truth_id = _required_lastrowid(
            conn.execute(
                "INSERT INTO ground_truth_signals "
                "(session_id, signal_type, signal_value, confidence, created_at) "
                "VALUES (?, 'historical_fixture', '1', 1.0, ?)",
                (_SCORE_SESSION_ID, now),
            )
        )
        queue_id = _required_lastrowid(
            conn.execute(
                "INSERT INTO scorer_training_queue "
                "(session_id, dimension, features_json, status, created_at, updated_at) "
                "VALUES (?, 'kg', '{\"matrix\":1}', 'completed', ?, ?)",
                (_SCORE_SESSION_ID, now, now),
            )
        )
        for object_type, object_id in (
            ("ground_truth", ground_truth_id),
            ("training_queue", queue_id),
        ):
            record_scoring_subject_provenance(
                conn,
                object_type=object_type,
                object_id=str(object_id),
                subject_provenance=access_control,
            )
        model_id = _required_lastrowid(
            conn.execute(
                "INSERT INTO scorer_models "
                "(dimension, model_version, model_type, model_blob, model_hash, "
                "train_samples, is_active, created_at, meta_json) "
                "VALUES ('kg', 'effect-matrix-model', 'json', X'00', "
                "'sha256:effect-matrix', 1, 1, ?, '{}')",
                (now,),
            )
        )
        record_scoring_derived_object(
            conn,
            object_type="model",
            object_id=str(model_id),
            source_refs=(
                ("training_queue", str(queue_id)),
                ("ground_truth", str(ground_truth_id)),
            ),
        )
        bayesian_feedback_id = _required_lastrowid(
            conn.execute(
                "INSERT INTO bayesian_feedback "
                "(dimension, is_positive, weight, context_json, created_at) "
                "VALUES ('kg', 1, 1.0, '{\"matrix\":true}', ?)",
                (now,),
            )
        )
        record_scoring_subject_provenance(
            conn,
            object_type="bayesian_feedback",
            object_id=str(bayesian_feedback_id),
            subject_provenance=access_control,
        )
        conn.execute(
            "INSERT INTO bayesian_scorer_state "
            "(dimension, alpha, beta, prior_alpha, prior_beta, total_samples, "
            "neg_likelihood, last_updated, updated_at) "
            "VALUES ('kg', 2.0, 1.0, 1.0, 1.0, 1, 0.3, ?, ?)",
            (now, now),
        )
        record_scoring_derived_object(
            conn,
            object_type="bayesian_state",
            object_id="kg",
            source_refs=(("bayesian_feedback", str(bayesian_feedback_id)),),
        )
        conn.commit()
    return {
        "queue_id": queue_id,
        "ground_truth_id": ground_truth_id,
        "model_id": model_id,
        "bayesian_feedback_id": bayesian_feedback_id,
    }


def _seed_non_wiki_domains(
    config: _MatrixConfig,
    *,
    access_control: dict[str, Any],
) -> dict[str, Any]:
    from core.access_policy import AccessNarrowing
    from core.app.context_search import ContextAwareSearch
    from core.cognitive_graph.store import CognitiveGraphStore
    from core.cognitive.access_control import make_cognitive_access_envelope
    from core.cognitive.models import Dimension, Observation, ObservationType, SourceType
    from core.cognitive.observation_store import ObservationStore
    from core.embeddings.cache import EmbeddingCache
    from core.mnemos_bus import Event
    from core.persona.cognitive_profile import ProfileAssertion, ProfileSignal, ProfileUsageLog
    from core.persona.psyche import SignalStore
    from core.reflection.models import CognitiveShift, ReflectionRecord, ReflectionTrigger
    from core.reflection.reflection_store import ReflectionStore
    from core.scoring.feedback_channel import FeedbackFatigueGuard
    from core.system_contracts import ActionLedger, make_data_inventory_observation
    from core.sync_framework.raw_event_store import RawEventStore
    from core.sync_framework.sync_engine import SyncEngine
    from core.telemetry.prompt_call_log import (
        ModelCallLedger,
        metered_provider_usage,
    )

    raw_path = config.database_dir / "raw_events.db"
    raw = RawEventStore(db_path=raw_path, config=config)
    try:
        raw_revision_id = raw.upsert_turn(
            source_agent="codex",
            session_id=_SESSION_ID,
            turn_number=1,
            user_content="cog043 matrix raw subject",
            assistant_content="cog043 matrix raw response",
            metadata={"project": "mnemos"},
            completeness={"visible_text": "full"},
        )
        raw.record_access(
            raw_revision_id,
            "search",
            query="cog043 matrix access",
            consumer="effect_matrix",
        )
    finally:
        raw.close()

    cache = EmbeddingCache(
        db_path=config.database_dir / "embedding_cache.db",
        model_version="effect-matrix",
    )
    cache.set("cog043 matrix cached derivative", [1.0])

    sync_path = config.database_dir / "sync_log.db"
    engine = SyncEngine(backend=_SyncBackend(), db_path=str(sync_path), config=config)
    engine.close()
    with sqlite3.connect(sync_path) as conn:
        conn.execute(
            """INSERT INTO sync_log
               (agent_name, session_id, turn_number, content_hash, status)
               VALUES (?, ?, ?, ?, ?)""",
            ("codex", _SESSION_ID, 1, "matrix-source-hash", "synced"),
        )
        conn.execute(
            """INSERT INTO user_signals
               (timestamp, agent, session_id, turn_number, content_length,
                has_code, has_tools, user_questions)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("2026-07-17T00:00:00+00:00", "codex", _SESSION_ID, 1, 1, 0, 0, 0),
        )

    persona_path = config.database_dir / "user_signals.db"
    persona = SignalStore(
        db_path=persona_path,
        config=config,
        initialize_schema=True,
    )
    try:
        signal_id = persona.record_profile_signal(
            ProfileSignal(
                source_event_id="raw-cog043-effect-matrix",
                signal_type="preference",
                dimension="detail",
                value="cog043 matrix private preference",
                access_control=access_control,
            )
        )
        assertion_id = persona.upsert_profile_assertion(
            ProfileAssertion(
                assertion_id="cog043-effect-matrix-assertion",
                dimension="detail",
                claim="cog043 matrix private assertion",
                supporting_signals=[f"profile_signals:{signal_id}"],
            )
        )
        from core.persona.profile_effect import compare_profile_effect

        revision_id = str(persona.get_profile_assertion_revisions(assertion_id)[-1]["revision_id"])
        read_principal = _runtime_principal(
            principal_id="audit:cog043-effect-matrix",
        )
        read_narrowing = AccessNarrowing(
            session_id=_SESSION_ID,
            project="mnemos",
        )
        _profile, read_access = persona.build_authorized_user_cognitive_profile_v2(
            principal=read_principal,
            narrowing=read_narrowing,
            purpose="persona_preflight_read",
            consumer="preflight_builder",
        )
        usage_id = persona.record_profile_usage(
            ProfileUsageLog(
                consumer="preflight_builder",
                profile_fields_used=[assertion_id],
                read_purpose="persona_preflight_read",
                read_authorization_token=str(read_access["read_authorization_token"]),
                target_receipt=compare_profile_effect(
                    owner="preflight_builder",
                    target_type="fixture_target",
                    target_id="cog043_effect_matrix",
                    matched_assertion_revisions={assertion_id: revision_id},
                    baseline_output="before",
                    persona_enabled_output="after",
                    expected_delta={"kind": "fixture_delta"},
                    receipt_id="cog043-effect-matrix-target",
                ),
                outcome="cog043 matrix usage",
            ),
            principal=read_principal,
            narrowing=read_narrowing,
        )
    finally:
        persona.close()

    reflection_path = config.database_dir / "reflections.db"
    reflection_store = ReflectionStore(
        str(reflection_path),
        ownership_config=config,
    )
    reflection_store.save_record(
        ReflectionRecord(
            id=_REFLECTION_ID,
            created_at=datetime.now(),
            trigger=ReflectionTrigger.MANUAL,
            user_query="cog043 matrix private reflection",
            access_control=access_control,
        )
    )
    reflection_shift_source = "raw-cog043-effect-matrix-shift"
    reflection_store.save_shift(
        CognitiveShift(
            dimension="attention",
            shift_type="explicit_correction",
            from_state="before",
            to_state="after",
            confidence=1.0,
            evidence=["raw-event:cog043-effect-matrix#0:1"],
            first_seen_at=datetime.now(),
            access_control=access_control,
        ),
        reflection_id=_REFLECTION_ID,
        source_event_id=reflection_shift_source,
    )
    layer5_experience_id = reflection_store.add_experience(
        {
            "type": "insight_pattern",
            "dimension": "attention",
            "summary": "cog043 matrix private Layer-5 experience",
            "source_event_id": "raw-cog043-effect-matrix-layer5",
            "access_control": access_control,
        }
    )

    observation_id = "cog043-effect-matrix-observation"
    observation_access = make_cognitive_access_envelope(
        owner_principal_id="audit:cog043-effect-matrix",
        owner_agent="codex",
        scope_type="observation",
        scope_id=observation_id,
        session_id=_SESSION_ID,
        project="mnemos",
        purposes=("observation_read", "preflight_inject"),
        consent_provenance_refs=("sha256:" + "a" * 64,),
        sensitivity="sensitive",
        retention_policy="observation_retention",
        source_acl_lineage=("sha256:" + "b" * 64,),
        visibility="private",
    )
    ObservationStore(
        str(config.database_dir / "observations.db"),
        ownership_config=config,
    ).save(
        Observation(
            id=observation_id,
            dimension=Dimension.ATTENTION,
            observation_type=ObservationType.FREQUENCY,
            value={"private": "cog043 matrix observation"},
            source_type=SourceType.RAW,
            source_id="raw-cog043-effect-matrix",
            access_control=observation_access,
        )
    )

    graph_path = config.database_dir / "cognitive_graph.db"
    graph = CognitiveGraphStore(str(graph_path), ownership_config=config)
    relation = graph.add_relation(
        source=f"session://{_SESSION_ID}",
        target=_GRAPH_TARGET,
        relation_type="derived_from",
        access_control=access_control,
    )
    graph_outbox = graph.add_sync_outbox(
        "reflection.completed",
        {"private": "cog043 matrix graph outbox"},
        access_control=access_control,
    )

    ledger = ActionLedger(config.database_dir / "action_ledger.db", initialize=True)
    action_record = make_data_inventory_observation(
        actor="effect-matrix",
        target=_ACTION_TARGET,
        evidence_refs=("sha256:" + "e" * 64,),
        details={"inventory_subject": "effect_matrix_subject_seed"},
        subject_provenance=access_control,
    )
    action = ledger.record_observation(action_record)

    scoring_path = config.database_dir / "mnemos.db"
    historical_training = _seed_historical_scoring_training_fixture(
        scoring_path,
        access_control=access_control,
    )
    ContextAwareSearch(wiki_base=str(config.wiki_dir)).record_authorized_search(
        "cog043 effect matrix authorized search",
        [],
        principal=_runtime_principal(principal_id="audit:cog043-effect-matrix"),
        narrowing=AccessNarrowing(session_id=_SESSION_ID, project="mnemos"),
    )
    feedback_event_id = "cog043-effect-matrix-feedback-event"
    _seed_historical_scoring_feedback_fixture(
        scoring_path,
        feedback_event_id=feedback_event_id,
        access_control=access_control,
    )
    FeedbackFatigueGuard(db_path=config.database_dir / "feedback_channel.db").record_prompt(
        "cog043-effect-matrix-feedback-prompt",
        subject_provenance=access_control,
    )
    scoring_model_id = historical_training["model_id"]

    model_ledger = ModelCallLedger.for_config(config)
    run_id = model_ledger.start_run(
        _MODEL_RUN_ID,
        subject_scope=("session", _SESSION_ID),
    )
    reservation = model_ledger.reserve(
        run_id=run_id,
        operation="distill_extract",
        provider="matrix",
        model="model",
        input_text="x",
        input_tokens=1,
    )
    reservation.mark_dispatched()
    usage = metered_provider_usage(
        {"prompt_tokens": 1, "completion_tokens": 0},
        request_id="cog043-effect-matrix-usage",
        output_required=True,
    )
    if usage is None:
        raise RuntimeError("effect matrix model usage was not created")
    reservation.settle(usage=usage)

    return {
        "raw_revision_id": raw_revision_id,
        "persona_signal_id": signal_id,
        "persona_assertion_id": assertion_id,
        "persona_usage_id": usage_id,
        "graph_relation_id": relation.id,
        "graph_outbox_id": graph_outbox.id,
        "observation_id": observation_id,
        "reflection_shift_source": reflection_shift_source,
        "layer5_experience_id": layer5_experience_id,
        "scoring_model_id": scoring_model_id,
        "action_id": action,
        "model_run_id": run_id,
        "event": Event(
            event_type="cog043_effect_matrix_subject",
            source="effect_matrix",
            payload={"kind": "subject_metadata"},
            trace_id=_EVENT_TRACE_ID,
            subject_provenance=access_control,
        ),
    }


def _domain_target_counts(first: Any, final: Any) -> dict[str, int]:
    first_results = first.verification_results
    final_results = final.verification_results

    def count(domain: str, *keys: str) -> int:
        for source in (first_results.get(domain, {}), final_results.get(domain, {})):
            for key in keys or ("target_count",):
                value = source.get(key)
                if value is not None and int(value or 0) > 0:
                    return int(value)
        return 0

    return {
        "raw": count("raw"),
        "wiki": count("wiki"),
        "embedding_cache": count("embedding_cache"),
        "metadata": count("metadata"),
        "evidence_refs": count("evidence_refs"),
        "persona": count("persona"),
        "reflection": count("reflection"),
        "scoring": count("scoring"),
        "action_ledger": count("action_ledger"),
        "model_call_ledger": count("model_call_ledger", "matched_run_count"),
        "consumer_access_log": count("consumer_access_log"),
        "agent_source_metadata": count("agent_source_metadata"),
        "cognitive_state": count("cognitive_state"),
        "observation": count("observation"),
        "cognitive_graph": count("cognitive_graph"),
    }


def _scoring_effect_counts(first: Any, final: Any) -> dict[str, int]:
    """Expose one physical-effect oracle for every persisted scoring subtype."""

    results = (
        first.verification_results.get("scoring", {}),
        final.verification_results.get("scoring", {}),
    )
    return {
        object_type: max(int(result.get(effect_key) or 0) for result in results)
        for object_type, effect_key in _SCORING_EFFECT_KEYS.items()
    }


def _sql_count(path: Path, query: str, params: tuple[Any, ...] = ()) -> int:
    if not path.is_file():
        return 0
    with sqlite3.connect(path) as conn:
        row = conn.execute(query, params).fetchone()
    return int(row[0] or 0) if row else 0


def _runtime_principal(*, agent: str = "codex", principal_id: str = ""):
    from core.access_policy import PrincipalEnvelope

    return PrincipalEnvelope(
        principal_id=principal_id or f"audit:{agent}:cog043-effect-matrix",
        agent=agent,
        host_kind="audit",
        capability_id="cog043-effect-matrix",
        capabilities=frozenset({"memory_read"}),
        allowed_projects=frozenset({"mnemos"}),
    )
