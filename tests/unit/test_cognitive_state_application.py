from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.application.cognitive_state import CognitiveStateApplicationService
from core.cognitive.access_control import make_cognitive_access_envelope
from core.application.facade import DefaultMnemosServiceFacade
from core.cognitive.state_schema import initialize_cognitive_state_schema
from core.evidence.source_authority import SourceAuthority, SourceAuthorityCatalog
from tests.cognitive_decision_fixtures import (
    TEST_PROJECT_CONTRACT_TEXT,
    TEST_USER_GOAL_TEXT,
    decision_authority_catalog,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64


def _source() -> dict:
    return {
        "source_id": "raw-event-decision-1",
        "source_revision_id": "raw-revision-decision-1",
        "source_kind": "material_decision",
        "source_uri": "raw://decision/1",
        "content_hash": HASH_A,
        "evidence_refs": ["raw-event-decision-1#0:120"],
        "created_at": "2026-07-16T01:00:00+00:00",
        "privacy_level": "private",
        "access_control": _access_control(),
    }


def _access_control(source_id: str = "raw-event-decision-1") -> dict:
    return make_cognitive_access_envelope(
        owner_principal_id="mcp:codex:test",
        owner_agent="codex",
        scope_type="project",
        scope_id="mnemos",
        project="mnemos",
        purposes=("cognitive_state_read", "cognitive_state_write"),
        consent_provenance_refs=(source_id,),
        sensitivity="sensitive",
        retention_policy="cognitive_state",
        source_acl_lineage=("sha256:" + "b" * 64,),
    )


def _principal() -> PrincipalEnvelope:
    return PrincipalEnvelope(
        principal_id="mcp:codex:test",
        agent="codex",
        host_kind="test",
        capability_id="test-capability",
        capabilities=frozenset({"memory_read", "memory_write"}),
        allowed_projects=frozenset({"mnemos"}),
    )


def _decision_authorities():
    return decision_authority_catalog(
        source_id="raw-event-decision-1",
        source_revision_id="raw-revision-decision-1",
    )


def _trace() -> dict:
    _, authorities = _decision_authorities()
    project_authority = authorities[SourceAuthority.PROJECT_CONTRACT.value]
    user_authority = authorities[SourceAuthority.EXPLICIT_USER.value]
    return {
        "idempotency_key": "application-decision-1",
        "source": _source(),
        "scope": {"type": "project", "id": "mnemos"},
        "task": "Repair COG-036",
        "goal": "Atomic typed decision persistence",
        "constraints": ["no split commit"],
        "values": [
            {
                "key": "safety",
                "category": "safety_permission_privacy",
                "constraint": TEST_PROJECT_CONTRACT_TEXT,
                "source_authority_id": project_authority.source_authority_id,
                "source_id": project_authority.source_event_id,
                "source_revision_id": project_authority.source_event_id,
                "source_content_hash": project_authority.content_sha256,
                "evidence_refs": [
                    "audit:COG-036:safety",
                    project_authority.source_authority_id,
                ],
                "valid_from": "2026-07-12T00:00:00+00:00",
                "valid_until": "",
                "changed_decision": True,
            },
            {
                "key": "goal",
                "category": "explicit_user_goal",
                "constraint": TEST_USER_GOAL_TEXT,
                "source_authority_id": user_authority.source_authority_id,
                "source_id": user_authority.source_event_id,
                "source_revision_id": user_authority.source_event_id,
                "source_content_hash": user_authority.content_sha256,
                "evidence_refs": [
                    "raw-event-decision-1#0:120",
                    user_authority.source_authority_id,
                ],
                "valid_from": "2026-07-16T01:00:00+00:00",
                "valid_until": "",
                "changed_decision": True,
            },
        ],
        "candidates": [
            {
                "key": "split",
                "summary": "Use independent commits.",
                "supporting_evidence": [],
                "opposing_evidence": ["audit:COG-036"],
                "violated_value_keys": ["safety"],
            },
            {
                "key": "atomic",
                "summary": "Use one canonical unit of work.",
                "supporting_evidence": ["audit:COG-036"],
                "opposing_evidence": [],
                "violated_value_keys": [],
            },
        ],
        "selection_key": "atomic",
        "rejections": [
            {
                "candidate_key": "split",
                "reason_code": "hard_constraint_violation",
                "evidence_refs": ["audit:COG-036:safety"],
            }
        ],
        "model_spec": {
            "provider": "system",
            "model": "deterministic-rule",
            "route": "local",
            "version": "mnemos.cog036.v1",
            "config_hash": HASH_C,
        },
        "tool_specs": [
            {
                "name": "CognitiveStateStore",
                "version": "mnemos.cognitive_state_store.v2",
                "code_hash": HASH_B,
            }
        ],
        "prompt_spec": {
            "prompt_id": "none:deterministic-rule",
            "prompt_hash": HASH_C,
            "schema_hash": HASH_B,
        },
        "expected_outcomes": [
            {"metric": "partial_facade_commit", "operator": "equals", "value": 0}
        ],
        "evaluation_window": {
            "starts_at": "2026-07-16T01:00:00+00:00",
            "ends_at": "2026-07-17T01:00:00+00:00",
        },
        "approval": {
            "mode": "explicit_user",
            "decision": "approved",
            "evidence_ref": "raw-event-decision-1#0:120",
            "created_at": "2026-07-16T01:00:00+00:00",
        },
        "actions": [
            {
                "key": "apply",
                "action_type": "formal_write",
                "owner": "trusted_vault",
                "executor": "trusted_vault",
                "target_ref": "wiki://03-Tech/COG-036.md",
                "input_hash": HASH_C,
                "rollback_contract": "restore exact before hash",
                "expected_effect": "target hash equals input hash",
            }
        ],
    }


def _outcome() -> dict:
    source = {**_source(), "source_id": "raw-event-outcome-1"}
    source["source_revision_id"] = "raw-revision-outcome-1"
    source["source_uri"] = "raw://outcome/1"
    source["content_hash"] = "sha256:outcome-source-1"
    source["evidence_refs"] = ["test-run:atomic-boundaries"]
    source["access_control"] = _access_control("raw-event-outcome-1")
    return {
        "outcome_id": "outcome-1",
        "source": source,
        "scope": {"type": "project", "id": "mnemos"},
        "outcome": {
            "payload": {
                "metric": "partial_facade_commit",
                "baseline": 3,
                "measurement_window": "targeted-test-run",
                "attribution_method": "transaction failpoint matrix",
                "confounders": [],
                "maturity": "verified",
            }
        },
    }


def _belief_request() -> dict:
    source_id = "raw-event-belief-1"
    return {
        "claim": "SQLite backups remain until their retention expiry.",
        "claim_kind": "fact",
        "scope": {"type": "project", "id": "mnemos"},
        "source": {
            "source_id": source_id,
            "source_revision_id": "raw-revision-belief-1",
            "source_kind": "explicit_user_assertion",
            "source_uri": "raw://belief/1",
            "content_hash": "sha256:" + "c" * 64,
            "evidence_refs": ["raw-event-belief-1#0:64"],
            "created_at": "2026-07-17T08:00:00+00:00",
            "access_control": make_cognitive_access_envelope(
                owner_principal_id="mcp:codex:test",
                owner_agent="codex",
                scope_type="project",
                scope_id="mnemos",
                project="mnemos",
                purposes=("belief_read", "cognitive_state_write"),
                consent_provenance_refs=(source_id,),
                sensitivity="sensitive",
                retention_policy="cognitive_state",
                source_acl_lineage=("sha256:" + "d" * 64,),
            ),
        },
        "supporting_evidence": ["raw-event-belief-1#0:64"],
        "invalidation_conditions": ["retention policy changes"],
    }


def test_build_cognitive_state_is_zero_write_when_database_is_missing(
    tmp_path: Path,
) -> None:
    db = tmp_path / "missing" / "producer_consumer_ledger.db"

    result = CognitiveStateApplicationService(db).build_cognitive_state()

    assert result["success"] is True
    assert result["status"] == "not_initialized"
    assert result["zero_write"] is True
    assert not db.exists()
    assert not db.parent.exists()


def test_record_decision_seals_three_typed_revisions_with_one_envelope(
    tmp_path: Path,
) -> None:
    db = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db)
    service = CognitiveStateApplicationService(db)
    authority_catalog, _ = _decision_authorities()

    first = service.record_decision(
        _trace(),
        principal=_principal(),
        source_authority_catalog=authority_catalog,
    )
    replay = service.record_decision(
        _trace(),
        principal=_principal(),
        source_authority_catalog=authority_catalog,
    )

    assert first["status"] == "committed"
    assert replay["status"] == "existing"
    assert replay["revision_ids"] == first["revision_ids"]
    assert first["snapshot"]["payload"]["value_context_revision_id"] == (
        first["value_context"]["revision_id"]
    )
    assert first["decision"]["payload"]["snapshot_revision_id"] == (
        first["snapshot"]["revision_id"]
    )
    assert json.loads(first["decision"]["canonical_payload"]) == (
        first["decision"]["payload"]
    )
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM cognitive_state_revisions").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM cognitive_data_events").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM cognitive_state_outbox").fetchone()[0] == 1

    read_model = service.build_cognitive_state(
        {"scope_type": "project", "scope_id": "mnemos"},
        principal=_principal(),
        narrowing=AccessNarrowing(project="mnemos"),
        purpose="cognitive_state_read",
    )
    assert read_model["status"] == "available"
    assert {item["object_type"] for item in read_model["items"]} == {
        "value_context",
        "cognitive_state_snapshot",
        "decision_trace",
    }
    assert all(
        item["payload_hash"] == item["canonical_payload_hash"]
        for item in read_model["items"]
    )


def test_public_facade_rejects_outcome_without_prior_prediction(
    tmp_path: Path,
    monkeypatch,
) -> None:
    initialize_cognitive_state_schema(tmp_path / "producer_consumer_ledger.db")
    config = SimpleNamespace(database_dir=tmp_path)
    monkeypatch.setattr("core.config.get_config", lambda: config)
    facade = DefaultMnemosServiceFacade()

    result = facade.apply_outcome(
        _outcome(),
        principal=_principal(),
        source_authority_catalog=SourceAuthorityCatalog(),
    )

    assert result["success"] is False
    with sqlite3.connect(tmp_path / "producer_consumer_ledger.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM cognitive_state_revisions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM cognitive_data_events").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM cognitive_state_outbox").fetchone()[0] == 0


def test_public_facade_revises_and_explains_a_canonical_belief(tmp_path, monkeypatch):
    initialize_cognitive_state_schema(tmp_path / "producer_consumer_ledger.db")
    monkeypatch.setattr(
        "core.config.get_config",
        lambda: SimpleNamespace(database_dir=tmp_path),
    )
    facade = DefaultMnemosServiceFacade()

    committed = facade.revise_belief(_belief_request(), principal=_principal())
    explained = facade.explain_belief(
        committed["belief_id"],
        principal=_principal(),
        narrowing=AccessNarrowing(project="mnemos"),
    )

    assert committed["success"] is True
    assert committed["status"] == "committed"
    assert committed["projection_effect_id"].startswith("belief-effect-")
    assert explained["success"] is True
    assert explained["status"] == "ok"
    assert explained["belief"]["stance"] == "supported"
    assert explained["belief"]["supporting_evidence"] == (
        "raw-event-belief-1#0:64",
    )


def test_belief_facade_rejects_caller_owned_identity_without_partial_rows(
    tmp_path,
    monkeypatch,
):
    db = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db)
    monkeypatch.setattr(
        "core.config.get_config",
        lambda: SimpleNamespace(database_dir=tmp_path),
    )
    request = _belief_request()
    request["stance"] = "supported"

    result = DefaultMnemosServiceFacade().revise_belief(
        request,
        principal=_principal(),
    )

    assert result["success"] is False
    assert result["error_code"] == "invalid_request"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM cognitive_state_revisions").fetchone()[0] == 0


@pytest.mark.parametrize(
    "forbidden_field",
    ["decision_id", "snapshot_id", "value_context_id", "action_refs", "effect_refs"],
)
def test_record_decision_rejects_caller_owned_canonical_identity_without_partial_rows(
    tmp_path: Path,
    forbidden_field: str,
) -> None:
    db = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db)
    service = CognitiveStateApplicationService(db)
    trace = _trace()
    trace[forbidden_field] = "forged-canonical-identity"
    authority_catalog, _ = _decision_authorities()

    with pytest.raises(ValueError, match="server-owned"):
        service.record_decision(
            trace,
            principal=_principal(),
            source_authority_catalog=authority_catalog,
        )

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM cognitive_state_revisions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM cognitive_data_events").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM cognitive_state_outbox").fetchone()[0] == 0


def test_facade_rejects_unsealed_snapshot_reference_without_partial_rows(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db)
    monkeypatch.setattr(
        "core.config.get_config",
        lambda: SimpleNamespace(database_dir=tmp_path),
    )
    trace = _trace()
    trace["snapshot_id"] = "forged-snapshot"

    authority_catalog, _ = _decision_authorities()
    result = DefaultMnemosServiceFacade().record_decision(
        trace,
        principal=_principal(),
        source_authority_catalog=authority_catalog,
    )

    assert result["success"] is False
    assert result["error_code"] == "invalid_request"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM cognitive_state_revisions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM cognitive_data_events").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM cognitive_state_outbox").fetchone()[0] == 0


def test_state_write_rejects_a_caller_supplied_output_acl_without_partial_rows(
    tmp_path: Path,
) -> None:
    db = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db)
    trace = _trace()
    trace["access_control"] = _access_control()
    authority_catalog, _ = _decision_authorities()

    with pytest.raises(ValueError, match="server-owned"):
        CognitiveStateApplicationService(db).record_decision(
            trace,
            principal=_principal(),
            source_authority_catalog=authority_catalog,
        )

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM cognitive_state_revisions").fetchone()[0] == 0


def test_state_write_rejects_a_different_server_principal_without_partial_rows(
    tmp_path: Path,
) -> None:
    db = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db)
    different_principal = PrincipalEnvelope(
        principal_id="mcp:codex:other-capability",
        agent="codex",
        host_kind="test",
        capability_id="other-capability",
        capabilities=frozenset({"memory_write"}),
        allowed_projects=frozenset({"mnemos"}),
    )
    authority_catalog, _ = _decision_authorities()

    with pytest.raises(PermissionError, match="owner_principal_mismatch"):
        CognitiveStateApplicationService(db).record_decision(
            _trace(),
            principal=different_principal,
            source_authority_catalog=authority_catalog,
        )

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM cognitive_state_revisions").fetchone()[0] == 0
