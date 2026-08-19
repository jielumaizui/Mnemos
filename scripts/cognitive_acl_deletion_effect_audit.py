"""Post-effect evidence checks for the COG-043 physical-effect matrix."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3
from typing import Any
from unittest.mock import patch

from scripts.cognitive_acl_deletion_effect_contracts import (
    _REFLECTION_ID,
    _SCORING_OBJECT_TYPES,
    _SESSION_ID,
)
from scripts.cognitive_acl_deletion_effect_fixtures import (
    _MatrixConfig,
    _runtime_principal,
    _seed_historical_scoring_feedback_fixture,
    _sql_count,
)


def _active_subject_residuals(
    config: _MatrixConfig,
    *,
    page: Path,
    state_store: Any,
    state_revision_id: str,
    seeded: dict[str, Any],
) -> dict[str, int]:
    from core.embeddings.cache import EmbeddingCache
    from core.ops.action_ledger_subject_provenance import LINK_TABLE as ACTION_LINK_TABLE
    from core.ops.event_subject_provenance import LINK_TABLE as EVENT_LINK_TABLE
    from core.privacy.object_provenance import scope_selector_hash
    from core.scoring.subject_provenance import LINK_TABLE as SCORING_LINK_TABLE
    from core.sync_framework.raw_event_store import RawEventStore
    from core.telemetry.prompt_call_log import ModelCallLedger

    selector_hash = scope_selector_hash("session", _SESSION_ID)
    raw = RawEventStore(db_path=config.database_dir / "raw_events.db", config=config)
    try:
        raw_rows = len(raw.list_current_headers(session_id=_SESSION_ID))
    finally:
        raw.close()
    raw_access_rows = _sql_count(
        config.database_dir / "raw_events.db", "SELECT COUNT(*) FROM raw_access_log"
    )
    sync_rows = _sql_count(
        config.database_dir / "sync_log.db",
        "SELECT COUNT(*) FROM sync_log WHERE session_id=?",
        (_SESSION_ID,),
    ) + _sql_count(
        config.database_dir / "sync_log.db",
        "SELECT COUNT(*) FROM user_signals WHERE session_id=?",
        (_SESSION_ID,),
    )
    persona_rows = sum(
        (
            _sql_count(
                config.database_dir / "user_signals.db",
                "SELECT COUNT(*) FROM profile_signals WHERE id=?",
                (seeded["persona_signal_id"],),
            ),
            _sql_count(
                config.database_dir / "user_signals.db",
                "SELECT COUNT(*) FROM profile_assertions WHERE assertion_id=?",
                (seeded["persona_assertion_id"],),
            ),
            _sql_count(
                config.database_dir / "user_signals.db",
                "SELECT COUNT(*) FROM profile_usage_log WHERE id=?",
                (seeded["persona_usage_id"],),
            ),
        )
    )
    reflection_rows = sum(
        (
            _sql_count(
                config.database_dir / "reflections.db",
                "SELECT COUNT(*) FROM reflection_records WHERE id=?",
                (_REFLECTION_ID,),
            ),
            _sql_count(
                config.database_dir / "reflections.db",
                "SELECT COUNT(*) FROM cognitive_shifts WHERE source_event_id=?",
                (seeded["reflection_shift_source"],),
            ),
            _sql_count(
                config.database_dir / "reflections.db",
                "SELECT COUNT(*) FROM layer5_experiences WHERE id=?",
                (seeded["layer5_experience_id"],),
            ),
        )
    )
    graph_rows = _sql_count(
        config.database_dir / "cognitive_graph.db",
        "SELECT COUNT(*) FROM cognitive_relations WHERE id=? AND stale=0",
        (seeded["graph_relation_id"],),
    ) + _sql_count(
        config.database_dir / "cognitive_graph.db",
        "SELECT COUNT(*) FROM sync_outbox WHERE id=?",
        (seeded["graph_outbox_id"],),
    )
    event_rows = _sql_count(
        config.mnemos_dir / "events.db",
        f"SELECT COUNT(*) FROM {EVENT_LINK_TABLE} WHERE scope_kind='session' AND scope_value_hash=?",
        (selector_hash,),
    )
    action_rows = _sql_count(
        config.database_dir / "action_ledger.db",
        f"SELECT COUNT(*) FROM {ACTION_LINK_TABLE} WHERE scope_kind='session' AND scope_value_hash=?",
        (selector_hash,),
    )
    scoring_rows = sum(
        _sql_count(
            path,
            f"SELECT COUNT(*) FROM {SCORING_LINK_TABLE} "
            "WHERE scope_kind='session' AND scope_value_hash=?",
            (selector_hash,),
        )
        for path in (
            config.database_dir / "mnemos.db",
            config.database_dir / "feedback_channel.db",
        )
    )
    metrics_rows = _sql_count(
        config.database_dir / "wiki_metrics.db",
        "SELECT COUNT(*) FROM page_metrics WHERE wiki_path IN (?, ?)",
        (str(page), str(page.relative_to(config.wiki_dir))),
    )
    model_rows = int(
        ModelCallLedger.for_config(config).run_summary(seeded["model_run_id"])["exists"]
    )
    state_rows = int(
        state_store.current_revision("cognitive_update_receipt", "cog043-effect-matrix-state")
        is not None
    )
    observation_rows = _sql_count(
        config.database_dir / "observations.db",
        "SELECT COUNT(*) FROM observations WHERE id=?",
        (seeded["observation_id"],),
    )
    del state_revision_id
    return {
        "raw": raw_rows,
        "consumer_access_log": raw_access_rows,
        "wiki": int(page.exists()),
        "embedding_cache": int(
            EmbeddingCache(
                db_path=config.database_dir / "embedding_cache.db",
                model_version="effect-matrix",
            ).get_stats()["total_entries"]
        ),
        "metadata": event_rows,
        "evidence_refs": metrics_rows,
        "persona": persona_rows,
        "reflection": reflection_rows,
        "scoring": scoring_rows,
        "action_ledger": action_rows,
        "model_call_ledger": model_rows,
        "agent_source_metadata": sync_rows,
        "cognitive_state": state_rows,
        "observation": observation_rows,
        "cognitive_graph": graph_rows,
    }


def _projection_residuals(
    config: _MatrixConfig,
    page: Path,
    *,
    seeded: dict[str, Any],
) -> dict[str, int]:
    from core.wiki_projection_lifecycle import WikiProjectionLedger

    ledger = WikiProjectionLedger(config.database_dir / "wiki_projection.db")
    reconciliation = ledger.reconciliation_report()
    rel_path = page.relative_to(config.wiki_dir).as_posix()
    meta_path = config.database_dir / "embedding_index" / "wiki_meta.json"
    if meta_path.is_file():
        try:
            search_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            search_meta = {rel_path: {}}
    else:
        search_meta = {}
    navigation_refs = 0
    navigation_dir = config.wiki_dir / "05-MOCs" / "Mnemos-Navigation"
    if navigation_dir.is_dir():
        for nav in navigation_dir.glob("*.md"):
            if rel_path in nav.read_text(encoding="utf-8", errors="ignore"):
                navigation_refs += 1
    knowledge_graph_refs = 0
    kg_path = config.database_dir / "knowledge_graph.db"
    if kg_path.is_file():
        knowledge_graph_refs = _sql_count(
            kg_path,
            "SELECT COUNT(*) FROM entity_sources WHERE source_page IN (?, ?)",
            (str(page), rel_path),
        )
        knowledge_graph_refs += _sql_count(
            kg_path,
            """SELECT COUNT(*) FROM relations
               WHERE source IN (?, ?) OR target IN (?, ?)""",
            (str(page), rel_path, str(page), rel_path),
        )
    cognitive_graph_refs = _sql_count(
        config.database_dir / "cognitive_graph.db",
        "SELECT COUNT(*) FROM cognitive_relations WHERE stale=0 AND (source LIKE ? OR target LIKE ?)",
        (f"%{rel_path}", f"%{rel_path}"),
    )
    relation_embedding_residuals = 0
    if kg_path.is_file():
        relation_embedding_residuals = _sql_count(
            kg_path,
            """SELECT COUNT(*) FROM relation_context_embeddings AS embedding
               LEFT JOIN relations AS relation ON relation.id=embedding.relation_id
               WHERE relation.id IS NULL""",
        )
        relation_embedding_residuals += _sql_count(
            kg_path,
            """SELECT COUNT(*) FROM relation_context_embeddings AS embedding
               JOIN relations AS relation ON relation.id=embedding.relation_id
               WHERE embedding.relation_id=?
                  OR relation.source IN (?, ?)
                  OR relation.target IN (?, ?)""",
            (
                seeded["kg_relation_id"],
                str(page),
                rel_path,
                str(page),
                rel_path,
            ),
        )
        relation_embedding_residuals += _sql_count(
            kg_path,
            "SELECT COUNT(*) FROM kg_embedding_outbox WHERE relation_id=?",
            (seeded["kg_relation_id"],),
        )
    return {
        "projection_ledger": int(reconciliation["projection_gap"]),
        "knowledge_graph": knowledge_graph_refs,
        "cognitive_graph": cognitive_graph_refs,
        "relation_embeddings": relation_embedding_residuals,
        "wiki_search_index": int(rel_path in search_meta),
        "wiki_metrics": _sql_count(
            config.database_dir / "wiki_metrics.db",
            "SELECT COUNT(*) FROM page_metrics WHERE wiki_path IN (?, ?)",
            (str(page), rel_path),
        ),
        "moc_navigation": navigation_refs,
        "scoring_model": _sql_count(
            config.database_dir / "mnemos.db",
            "SELECT COUNT(*) FROM scorer_models WHERE id=?",
            (seeded["scoring_model_id"],),
        ),
        "bayesian_state": _sql_count(
            config.database_dir / "mnemos.db",
            "SELECT COUNT(*) FROM bayesian_scorer_state WHERE dimension='kg'",
        ),
    }


def _public_acl_rejected() -> bool:
    from core.cognitive.access_control import make_cognitive_access_envelope

    try:
        make_cognitive_access_envelope(
            owner_principal_id="audit:public-rejection",
            owner_agent="codex",
            scope_type="session",
            scope_id="audit-public-rejection",
            session_id="audit-public-rejection",
            project="mnemos",
            purposes=("audit_read",),
            consent_provenance_refs=("sha256:" + "c" * 64,),
            sensitivity="sensitive",
            retention_policy="audit",
            source_acl_lineage=("sha256:" + "d" * 64,),
            visibility="public",
        )
    except ValueError:
        return True
    return False


def _acl_decision_matrix(access_control: dict[str, Any]) -> dict[str, Any]:
    from core.access_policy import AccessNarrowing
    from core.cognitive.access_control import authorize_cognitive_access

    correct = _runtime_principal(principal_id="audit:cog043-effect-matrix")
    cases = {
        "authorized": authorize_cognitive_access(
            access_control,
            principal=correct,
            narrowing=AccessNarrowing(session_id=_SESSION_ID, project="mnemos"),
            purpose="cognitive_state_read",
        ),
        "cross_agent": authorize_cognitive_access(
            access_control,
            principal=_runtime_principal(agent="claude"),
            narrowing=AccessNarrowing(session_id=_SESSION_ID, project="mnemos"),
            purpose="cognitive_state_read",
        ),
        "cross_project": authorize_cognitive_access(
            access_control,
            principal=correct,
            narrowing=AccessNarrowing(session_id=_SESSION_ID, project="other"),
            purpose="cognitive_state_read",
        ),
        "cross_session": authorize_cognitive_access(
            access_control,
            principal=correct,
            narrowing=AccessNarrowing(
                session_id="another-session",
                project="mnemos",
            ),
            purpose="cognitive_state_read",
        ),
        "wrong_purpose": authorize_cognitive_access(
            access_control,
            principal=correct,
            narrowing=AccessNarrowing(session_id=_SESSION_ID, project="mnemos"),
            purpose="not_permitted",
        ),
        "missing_principal": authorize_cognitive_access(
            access_control,
            principal=None,
            narrowing=AccessNarrowing(session_id=_SESSION_ID, project="mnemos"),
            purpose="cognitive_state_read",
        ),
    }
    leaks = {
        name: int(decision.allowed) for name, decision in cases.items() if name != "authorized"
    }
    return {
        "case_denominator": list(cases),
        "authorized_success": int(cases["authorized"].allowed),
        "decisions": {
            name: {"allowed": decision.allowed, "reason": decision.reason}
            for name, decision in cases.items()
        },
        "cross_scope_leak": sum(leaks.values()),
        "leaks": leaks,
    }


def _multi_source_acl_merge_matrix(
    access_control: dict[str, Any],
) -> dict[str, bool]:
    from core.cognitive.access_control import (
        cognitive_access_hash,
        derive_strictest_cognitive_access,
        make_cognitive_access_envelope,
    )

    second_source = make_cognitive_access_envelope(
        owner_principal_id="audit:cog043-effect-matrix",
        owner_agent="codex",
        scope_type="observation",
        scope_id="cog043-merge-source",
        session_id=_SESSION_ID,
        project="mnemos",
        purposes=("cognitive_state_read",),
        consent_provenance_refs=("sha256:" + "f" * 64,),
        sensitivity="restricted",
        retention_policy="shorter_source_retention",
        source_acl_lineage=("sha256:" + "e" * 64,),
        visibility="private",
    )
    sources = (access_control, second_source)
    merged = derive_strictest_cognitive_access(
        sources,
        owner_principal_id="audit:cog043-effect-matrix",
        owner_agent="codex",
        scope_type="reflection",
        scope_id="cog043-merged-reflection",
        purposes=("cognitive_state_read",),
        retention_policy="derived_shortest_retention",
    )
    broader = derive_strictest_cognitive_access(
        sources,
        owner_principal_id="audit:cog043-effect-matrix",
        owner_agent="codex",
        scope_type="reflection",
        scope_id="cog043-overbroad-reflection",
        purposes=("reflection_read",),
        retention_policy="derived_shortest_retention",
    )
    expected_lineage = {cognitive_access_hash(source) for source in sources}
    return {
        "compatible_merge_resolved": (
            merged["scope"]["resolution"] == "resolved" and merged["visibility"] == "private"
        ),
        "source_lineage_complete": expected_lineage.issubset(set(merged["source_acl_lineage"])),
        "purpose_intersection_enforced": merged["purposes"] == ["cognitive_state_read"],
        "strictest_sensitivity_inherited": merged["sensitivity"] == "restricted",
        "broader_purpose_restricted": (
            broader["scope"]["resolution"] == "restricted_unknown"
            and broader["visibility"] == "restricted"
        ),
    }


def _freeze_resurrection_barrier(
    config: _MatrixConfig,
    *,
    access_control: dict[str, Any],
    event_bus: Any,
) -> dict[str, Any]:
    from core.access_policy import AccessNarrowing
    from core.app.context_search import ContextAwareSearch
    from core.cognitive.access_control import make_cognitive_access_envelope
    from core.cognitive.models import Dimension, Observation, ObservationType, SourceType
    from core.cognitive.observation_store import ObservationStore
    from core.cognitive.state_contract import (
        CognitiveStateRevision,
        LocalConsumerCommand,
        sha256_json,
    )
    from core.cognitive.state_store import CognitiveStateStore
    from core.cognitive_graph.store import CognitiveGraphStore
    from core.mnemos_bus import Event
    from core.ops.cognitive_data_contract import CognitiveDataEvent
    from core.persona.cognitive_profile import ProfileSignal
    from core.persona.psyche import SignalStore
    from core.reflection.models import ReflectionRecord, ReflectionTrigger
    from core.reflection.reflection_store import ReflectionStore
    from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2
    from core.scoring.bayesian_scorer import BayesianScorer
    from core.scoring.feedback_channel import FeedbackFatigueGuard
    from core.scoring.subject_provenance import record_scoring_derived_object
    from core.system_contracts import ActionLedger, make_action_record

    def object_access(
        *,
        scope_type: str,
        scope_id: str,
        purposes: tuple[str, ...],
    ) -> dict[str, Any]:
        return make_cognitive_access_envelope(
            owner_principal_id="audit:cog043-effect-matrix",
            owner_agent="codex",
            scope_type=scope_type,
            scope_id=scope_id,
            session_id=_SESSION_ID,
            project="mnemos",
            purposes=purposes,
            consent_provenance_refs=("sha256:" + "a" * 64,),
            sensitivity="sensitive",
            retention_policy=f"{scope_type}_retention",
            source_acl_lineage=("sha256:" + "b" * 64,),
            visibility="private",
        )

    blocked: dict[str, bool] = {}
    object_blocked: dict[str, bool] = {}

    def attempt(domain: str, operation: Any) -> None:
        try:
            operation()
        except PermissionError:
            blocked[domain] = True
        else:
            blocked[domain] = False

    def attempt_object(object_type: str, operation: Any) -> None:
        try:
            operation()
        except PermissionError:
            object_blocked[object_type] = True
        else:
            object_blocked[object_type] = False

    resurrection_hash = sha256_json({"resurrection": "blocked"})
    state_revision = CognitiveStateRevision.create(
        object_type="cognitive_update_receipt",
        object_id="cog043-resurrection-state",
        source_event_id="cde-cog043-resurrection",
        source_revision_id="raw-cog043-resurrection",
        source_content_hash=resurrection_hash,
        scope_type="session",
        scope_id=_SESSION_ID,
        evidence_refs=("raw-event:cog043-resurrection#0:1",),
        payload={
            "input_refs": ["raw-event:cog043-resurrection#0:1"],
            "attribution": {"action": "resurrection_probe"},
            "target_command_ref": "command:cog043-resurrection",
            "before_hash": sha256_json({"before": "none"}),
            "after_hash": sha256_json({"after": "must-not-exist"}),
            "effect_receipt_ref": "pending",
            "access_control": access_control,
        },
    )
    state_event = CognitiveDataEvent(
        event_id=state_revision.source_event_id,
        source_id=state_revision.source_revision_id,
        asset_id="asset-cog043-resurrection",
        source_kind="audit",
        source_uri="audit://cog043/resurrection",
        content_hash=resurrection_hash,
        canonical_subject=state_revision.object_id,
        data_type=state_revision.object_type,
        producer="audit",
        intended_consumers=("wiki",),
        privacy_level="private",
        confidence=1.0,
        evidence_refs=state_revision.evidence_refs,
        dedupe_key="cog043-resurrection",
        created_at="2026-07-17T00:00:02+00:00",
    )
    state_command = LocalConsumerCommand.create(
        revision_id=state_revision.revision_id,
        consumer_id="wiki",
        command_type="project_cognition_update_receipt",
        payload={"projection": "wiki"},
    )
    attempt(
        "cognitive_state",
        lambda: CognitiveStateStore(config)
        .unit_of_work()
        .commit(
            revisions=(state_revision,),
            event=state_event,
            commands=(state_command,),
        ),
    )

    observation_id = "cog043-resurrection-observation"
    attempt(
        "observation",
        lambda: ObservationStore(
            str(config.database_dir / "observations.db"),
            ownership_config=config,
        ).save(
            Observation(
                id=observation_id,
                dimension=Dimension.ATTENTION,
                observation_type=ObservationType.FREQUENCY,
                value={"private": "must not resurrect"},
                source_type=SourceType.RAW,
                source_id="raw-cog043-resurrection",
                access_control=object_access(
                    scope_type="observation",
                    scope_id=observation_id,
                    purposes=("observation_read",),
                ),
            )
        ),
    )

    reflection_id = "cog043-resurrection-reflection"
    attempt(
        "reflection",
        lambda: ReflectionStore(
            str(config.database_dir / "reflections.db"),
            ownership_config=config,
        ).save_record(
            ReflectionRecord(
                id=reflection_id,
                created_at=datetime.now(),
                trigger=ReflectionTrigger.MANUAL,
                user_query="must not resurrect",
                access_control=object_access(
                    scope_type="reflection",
                    scope_id=reflection_id,
                    purposes=("reflection_read",),
                ),
            )
        ),
    )

    attempt(
        "cognitive_graph",
        lambda: CognitiveGraphStore(
            str(config.database_dir / "cognitive_graph.db"),
            ownership_config=config,
        ).add_relation(
            f"session://{_SESSION_ID}",
            "kg://Cog043MustNotResurrect",
            "derived_from",
            access_control=access_control,
        ),
    )

    persona = SignalStore(
        db_path=config.database_dir / "user_signals.db",
        config=config,
    )
    try:
        attempt(
            "persona",
            lambda: persona.record_profile_signal(
                ProfileSignal(
                    source_event_id="raw-cog043-resurrection",
                    signal_type="preference",
                    dimension="detail",
                    value="must not resurrect",
                    access_control=access_control,
                )
            ),
        )
    finally:
        persona.close()

    attempt_object(
        "scoring_training_queue_ground_truth",
        lambda: AdaptiveScorerV2.enqueue_training_sample(
            session_id="cog043-resurrection-scoring",
            dimension="kg",
            features={"private": "must not resurrect"},
            expected_score=1.0,
            source="effect_matrix_resurrection_probe",
            db_path=str(config.database_dir / "mnemos.db"),
            subject_provenance=access_control,
        ),
    )

    attempt_object(
        "scoring_search_session",
        lambda: ContextAwareSearch(wiki_base=str(config.wiki_dir)).record_authorized_search(
            "cog043 resurrection search",
            [],
            principal=_runtime_principal(principal_id="audit:cog043-effect-matrix"),
            narrowing=AccessNarrowing(session_id=_SESSION_ID, project="mnemos"),
        ),
    )

    attempt_object(
        "scoring_feedback_event",
        lambda: _seed_historical_scoring_feedback_fixture(
            config.database_dir / "mnemos.db",
            feedback_event_id="cog043-resurrection-feedback",
            access_control=access_control,
        ),
    )

    attempt_object(
        "scoring_feedback_prompt",
        lambda: FeedbackFatigueGuard(
            db_path=config.database_dir / "feedback_channel.db"
        ).record_prompt(
            "cog043-resurrection-feedback-prompt",
            subject_provenance=access_control,
        ),
    )

    attempt_object(
        "scoring_bayesian_feedback_state",
        lambda: BayesianScorer(
            dimensions=["resurrection"],
            db_path=config.database_dir / "mnemos.db",
        ).feedback(
            "resurrection",
            True,
            context={"private": "must not resurrect"},
            subject_provenance=access_control,
        ),
    )

    def write_scoring_model() -> None:
        scoring_path = config.database_dir / "mnemos.db"
        with sqlite3.connect(scoring_path) as conn:
            source = conn.execute("""SELECT object_type, object_id FROM scoring_object_provenance
                   WHERE object_type IN ('training_queue', 'ground_truth')
                     AND state='tracked'
                   ORDER BY object_type, object_id""").fetchall()
            cursor = conn.execute(
                """INSERT INTO scorer_models(
                       dimension, model_version, model_type, model_blob, model_hash,
                       train_samples, is_active, created_at, meta_json
                   ) VALUES ('resurrection', 'blocked', 'json', X'00',
                             'sha256:blocked', 1, 1, ?, '{}')""",
                (datetime.now().isoformat(),),
            )
            record_scoring_derived_object(
                conn,
                object_type="model",
                object_id=str(cursor.lastrowid),
                source_refs=tuple((str(row[0]), str(row[1])) for row in source),
            )

    attempt_object("scoring_model", write_scoring_model)
    blocked["scoring"] = bool(object_blocked) and all(object_blocked.values())

    attempt(
        "action_ledger",
        lambda: ActionLedger(
            config.database_dir / "action_ledger.db",
        ).record(
            make_action_record(
                actor="effect-matrix-resurrection",
                action_type="data_delete",
                target="cog043-resurrection-action",
                evidence_refs=("sha256:" + "f" * 64,),
                rollback_ref="manual-effect-matrix-rollback",
                subject_provenance=access_control,
            )
        ),
    )

    attempt(
        "event_bus",
        lambda: event_bus.publish(
            Event(
                event_type="cog043_effect_matrix_subject",
                source="effect_matrix_resurrection",
                payload={"private": "must not resurrect"},
                trace_id="cog043-resurrection-event",
                subject_provenance=access_control,
            )
        ),
    )

    denominator = sorted(blocked)
    return {
        "domain_denominator": denominator,
        "blocked": blocked,
        "resurrection_gap": sum(int(not blocked[name]) for name in denominator),
        "object_denominator": sorted(object_blocked),
        "object_blocked": object_blocked,
        "object_resurrection_gap": sum(int(not object_blocked[name]) for name in object_blocked),
    }


def _pre_body_authorization_matrix(
    config: _MatrixConfig,
    *,
    state_store: Any,
    seeded: dict[str, Any],
) -> dict[str, Any]:
    from core.access_policy import AccessNarrowing
    from core.cognitive.observation_store import ObservationStore
    from core.cognitive_graph.store import CognitiveGraphStore
    from core.persona.psyche import SignalStore
    from core.reflection.reflection_store import ReflectionStore

    correct = _runtime_principal(principal_id="audit:cog043-effect-matrix")
    correct_narrowing = AccessNarrowing(session_id=_SESSION_ID, project="mnemos")
    denied_narrowing = AccessNarrowing(
        session_id="another-session",
        project="mnemos",
    )
    observation_store = ObservationStore(str(config.database_dir / "observations.db"))
    reflection_store = ReflectionStore(str(config.database_dir / "reflections.db"))
    graph_store = CognitiveGraphStore(str(config.database_dir / "cognitive_graph.db"))
    persona_store = SignalStore(db_path=config.database_dir / "user_signals.db")

    allowed_state, _ = state_store.authorized_current_revisions(
        principal=correct,
        narrowing=correct_narrowing,
        purpose="cognitive_state_read",
    )
    allowed_observation, _ = observation_store.authorized_query(
        principal=correct,
        narrowing=correct_narrowing,
        purpose="observation_read",
    )
    allowed_reflection, _ = reflection_store.authorized_get_by_id(
        _REFLECTION_ID,
        principal=correct,
        narrowing=correct_narrowing,
        purpose="reflection_read",
    )
    allowed_layer5, _ = reflection_store.authorized_get_experiences(
        principal=correct,
        narrowing=correct_narrowing,
        purpose="reflection_experience_read",
    )
    allowed_graph, _ = graph_store.authorized_get_relation(
        seeded["graph_relation_id"],
        principal=correct,
        narrowing=correct_narrowing,
    )
    allowed_persona, _ = persona_store._cognitive_profiles.get_authorized_assertions(
        principal=correct,
        narrowing=correct_narrowing,
        purpose="persona_preflight_read",
    )
    allowed = {
        "cognitive_state": bool(allowed_state),
        "observation": bool(allowed_observation),
        "reflection": allowed_reflection is not None,
        "reflection_layer5": bool(allowed_layer5),
        "cognitive_graph": allowed_graph is not None,
        "persona": bool(allowed_persona),
    }

    def forbidden_body(*_args: Any, **_kwargs: Any):
        raise AssertionError("denied cognitive body was hydrated")

    denied: dict[str, bool] = {}
    with patch("core.cognitive.state_store._revision_from_row", side_effect=forbidden_body):
        items, _summary = state_store.authorized_current_revisions(
            principal=correct,
            narrowing=denied_narrowing,
            purpose="cognitive_state_read",
        )
        denied["cognitive_state"] = not items
    with patch.object(observation_store, "_row_to_obs", side_effect=forbidden_body):
        items, _summary = observation_store.authorized_query(
            principal=correct,
            narrowing=denied_narrowing,
            purpose="observation_read",
        )
        denied["observation"] = not items
    with patch.object(reflection_store, "_row_to_record", side_effect=forbidden_body):
        item, _summary = reflection_store.authorized_get_by_id(
            _REFLECTION_ID,
            principal=correct,
            narrowing=denied_narrowing,
            purpose="reflection_read",
        )
        denied["reflection"] = item is None
    with patch.object(
        reflection_store,
        "_experience_from_row",
        side_effect=forbidden_body,
    ):
        items, _summary = reflection_store.authorized_get_experiences(
            principal=correct,
            narrowing=denied_narrowing,
            purpose="reflection_experience_read",
        )
        denied["reflection_layer5"] = not items
    with patch.object(graph_store, "_row_to_relation", side_effect=forbidden_body):
        relation, _summary = graph_store.authorized_get_relation(
            seeded["graph_relation_id"],
            principal=correct,
            narrowing=denied_narrowing,
        )
        denied["cognitive_graph"] = relation is None
    with patch.object(
        persona_store._cognitive_profiles,
        "_assertion_from_row",
        side_effect=forbidden_body,
    ):
        items, _summary = persona_store._cognitive_profiles.get_authorized_assertions(
            principal=correct,
            narrowing=denied_narrowing,
            purpose="persona_preflight_read",
        )
        denied["persona"] = not items
    persona_store.close()
    return {
        "domain_denominator": sorted(allowed),
        "allowed": allowed,
        "denied_before_body": denied,
        "pre_body_authorization_gap": sum(
            int(not allowed[name] or not denied.get(name, False)) for name in allowed
        ),
    }


def _authorized_typed_object_presence(
    config: _MatrixConfig,
    *,
    state_store: Any,
    seeded: dict[str, Any],
) -> dict[str, bool]:
    from core.access_policy import AccessNarrowing
    from core.cognitive.observation_store import ObservationStore
    from core.cognitive_graph.store import CognitiveGraphStore
    from core.persona.psyche import SignalStore
    from core.reflection.reflection_store import ReflectionStore

    principal = _runtime_principal(principal_id="audit:cog043-effect-matrix")
    narrowing = AccessNarrowing(session_id=_SESSION_ID, project="mnemos")
    observations = ObservationStore(str(config.database_dir / "observations.db"))
    reflections = ReflectionStore(str(config.database_dir / "reflections.db"))
    graph = CognitiveGraphStore(str(config.database_dir / "cognitive_graph.db"))
    persona = SignalStore(db_path=config.database_dir / "user_signals.db")
    try:
        state_items, _ = state_store.authorized_current_revisions(
            principal=principal,
            narrowing=narrowing,
            purpose="cognitive_state_read",
        )
        observation_items, _ = observations.authorized_query(
            principal=principal,
            narrowing=narrowing,
            purpose="observation_read",
        )
        reflection_item, _ = reflections.authorized_get_by_id(
            _REFLECTION_ID,
            principal=principal,
            narrowing=narrowing,
            purpose="reflection_read",
        )
        layer5_items, _ = reflections.authorized_get_experiences(
            principal=principal,
            narrowing=narrowing,
            purpose="reflection_experience_read",
        )
        graph_item, _ = graph.authorized_get_relation(
            seeded["graph_relation_id"],
            principal=principal,
            narrowing=narrowing,
        )
        persona_items, _ = persona._cognitive_profiles.get_authorized_assertions(
            principal=principal,
            narrowing=narrowing,
            purpose="persona_preflight_read",
        )
    finally:
        persona.close()
    return {
        "cognitive_state": bool(state_items),
        "observation": bool(observation_items),
        "reflection": reflection_item is not None,
        "reflection_layer5": bool(layer5_items),
        "cognitive_graph": graph_item is not None,
        "persona": bool(persona_items),
    }


def _active_acl_inventory(config: _MatrixConfig) -> dict[str, Any]:
    from core.cognitive.access_control import validate_cognitive_access_envelope

    specs: dict[
        str,
        tuple[Path, str] | tuple[Path, str, tuple[Any, ...]],
    ] = {
        "cognitive_state": (
            config.database_dir / "producer_consumer_ledger.db",
            """SELECT json_extract(r.payload_json, '$.access_control')
               FROM cognitive_state_heads h
               JOIN cognitive_state_revisions r ON r.revision_id=h.revision_id""",
        ),
        "observation": (
            config.database_dir / "observations.db",
            "SELECT access_control FROM observations",
        ),
        "reflection_record": (
            config.database_dir / "reflections.db",
            "SELECT access_control FROM reflection_records",
        ),
        "reflection_shift": (
            config.database_dir / "reflections.db",
            "SELECT access_control FROM cognitive_shifts",
        ),
        "reflection_layer5": (
            config.database_dir / "reflections.db",
            "SELECT access_control FROM layer5_experiences",
        ),
        "persona_signal": (
            config.database_dir / "user_signals.db",
            "SELECT access_control FROM profile_signals",
        ),
        "persona_assertion": (
            config.database_dir / "user_signals.db",
            "SELECT access_control FROM profile_assertions",
        ),
        "persona_usage": (
            config.database_dir / "user_signals.db",
            "SELECT access_control FROM profile_usage_log",
        ),
        "cognitive_graph_relation": (
            config.database_dir / "cognitive_graph.db",
            "SELECT access_control FROM cognitive_relations WHERE stale=0",
        ),
        "cognitive_graph_node": (
            config.database_dir / "cognitive_graph.db",
            "SELECT access_control FROM canonical_nodes",
        ),
        "cognitive_graph_outbox": (
            config.database_dir / "cognitive_graph.db",
            "SELECT access_control FROM sync_outbox",
        ),
        "action_ledger": (
            config.database_dir / "action_ledger.db",
            """SELECT access_json FROM action_ledger_object_provenance
               WHERE state='tracked'""",
        ),
        "event_bus": (
            config.mnemos_dir / "events.db",
            """SELECT access_json FROM event_object_provenance
               WHERE state='tracked'""",
        ),
    }
    for object_type in _SCORING_OBJECT_TYPES:
        specs[f"scoring_{object_type}"] = (
            config.database_dir
            / ("feedback_channel.db" if object_type == "feedback_prompt" else "mnemos.db"),
            """SELECT access_json FROM scoring_object_provenance
               WHERE state IN ('tracked', 'derived') AND object_type=?""",
            (object_type,),
        )
    counts: dict[str, int] = {}
    gaps: dict[str, int] = {}
    authorization_gaps: dict[str, int] = {}
    for name, spec in specs.items():
        path, query = spec[:2]
        params = spec[2] if len(spec) > 2 else ()
        rows: list[tuple[Any, ...]] = []
        if path.is_file():
            try:
                with sqlite3.connect(path) as conn:
                    rows = conn.execute(query, params).fetchall()
            except sqlite3.Error:
                gaps[name] = 1
                authorization_gaps[name] = 1
                counts[name] = 0
                continue
        counts[name] = len(rows)
        invalid = 0
        unauthorized = 0
        for row in rows:
            try:
                access = validate_cognitive_access_envelope(json.loads(str(row[0] or "")))
                if not access.get("source_acl_lineage"):
                    invalid += 1
                if (
                    access["scope"]["resolution"] != "resolved"
                    or access["consent"]["status"] != "granted"
                    or access["visibility"] == "restricted"
                ):
                    unauthorized += 1
            except (TypeError, ValueError, json.JSONDecodeError):
                invalid += 1
                unauthorized += 1
        gaps[name] = invalid
        authorization_gaps[name] = unauthorized
    required_nonempty = {
        "cognitive_state",
        "observation",
        "reflection_record",
        "reflection_shift",
        "reflection_layer5",
        "persona_signal",
        "persona_assertion",
        "persona_usage",
        "cognitive_graph_relation",
        "cognitive_graph_node",
        "cognitive_graph_outbox",
        "action_ledger",
        "event_bus",
        *(f"scoring_{object_type}" for object_type in _SCORING_OBJECT_TYPES),
    }
    coverage_gap = sorted(name for name in required_nonempty if counts.get(name, 0) == 0)
    return {
        "denominator": sorted(specs),
        "active_counts": counts,
        "lineage_gaps": gaps,
        "authorization_gaps": authorization_gaps,
        "active_count": sum(counts.values()),
        "active_acl_lineage_gap": sum(gaps.values()),
        "active_acl_authorization_gap": sum(authorization_gaps.values()),
        "coverage_gap": coverage_gap,
        "object_type_denominator": {
            "scoring": list(_SCORING_OBJECT_TYPES),
            "reflection": ["record", "shift", "layer5"],
            "cognitive_graph": ["relation", "node", "outbox"],
        },
    }
