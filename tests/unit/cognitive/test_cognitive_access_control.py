from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.application.cognitive_state import CognitiveStateApplicationService
from core.cognitive.access_control import (
    authorize_cognitive_write,
    derive_strictest_cognitive_access,
    make_cognitive_access_envelope,
)
from core.cognitive.state_contract import CognitiveStateRevision, LocalConsumerCommand
from core.cognitive.state_schema import initialize_cognitive_state_schema
from core.cognitive.state_store import CognitiveStateStore
from core.cognitive.feedback_contract import reaction_input_hash
from core.ops.cognitive_data_contract import CognitiveDataEvent
from tests.unit.cognitive.feedback_attribution_fixtures import reaction_payload


def _principal(agent: str = "codex") -> PrincipalEnvelope:
    return PrincipalEnvelope(
        principal_id=f"mcp:{agent}:test",
        agent=agent,
        host_kind="test",
        capability_id="test-capability",
        capabilities=frozenset({"memory_read"}),
        allowed_projects=frozenset({"mnemos"}),
    )


def _access(*, session_id: str = "session-1") -> dict:
    return make_cognitive_access_envelope(
        owner_principal_id="mcp:codex:test",
        owner_agent="codex",
        scope_type="session",
        scope_id=session_id,
        session_id=session_id,
        project="mnemos",
        purposes=("cognitive_state_read", "cognitive_state_write"),
        consent_provenance_refs=("raw-event:session-1",),
        sensitivity="sensitive",
        retention_policy="inherit_source",
        source_acl_lineage=("sha256:" + "a" * 64,),
    )


def _revision(*, access_control: dict | None = None) -> CognitiveStateRevision:
    payload = reaction_payload()
    payload["reaction_id"] = "reaction-" + "a" * 32
    payload["source_event_ref"] = {
        "event_id": "access-event",
        "source_revision_id": "raw-revision-access",
        "content_hash": "sha256:" + "c" * 64,
    }
    payload["principal_ref"] = {
        "principal_id": "mcp:codex:test",
        "agent": "codex",
        "authorization_ref": "authz:access-control-test",
    }
    payload["scope"] = {
        "type": "session",
        "id": "session-1",
        "project": "mnemos",
        "session_id": "session-1",
    }
    payload["subject_ref"] = {"type": "access_control_test", "id": "session-1"}
    payload["delivery_ref"]["event_id"] = "delivery-access-control-test"
    payload["evidence"] = {
        "refs": ["raw-event:session-1#0:32"],
        "content_hashes": ["sha256:" + "b" * 64],
    }
    payload["exposure"]["session_id"] = "session-1"
    if access_control is not None:
        payload["access_control"] = access_control
    else:
        payload.pop("access_control")
    payload["reaction_input_hash"] = reaction_input_hash(payload)
    return CognitiveStateRevision.create(
        object_type="user_reaction_event",
        object_id="access-control-test",
        source_event_id="access-event",
        source_revision_id="raw-revision-access",
        source_content_hash="sha256:" + "c" * 64,
        scope_type="session",
        scope_id="session-1",
        evidence_refs=("raw-event:session-1#0:32",),
        payload=payload,
        created_at="2026-07-16T00:00:00+00:00",
    )


def _event() -> CognitiveDataEvent:
    return CognitiveDataEvent(
        event_id="access-event",
        source_id="raw-event:session-1",
        asset_id="",
        source_kind="test",
        source_uri="raw://session-1",
        content_hash="sha256:" + "c" * 64,
        canonical_subject="user_reaction_event:access-control-test",
        data_type="user_reaction_event",
        producer="cognitive_state_store",
        intended_consumers=("wiki",),
        privacy_level="private",
        confidence=1.0,
        evidence_refs=("raw-event:session-1#0:32",),
        dedupe_key="access-control-test",
        created_at="2026-07-16T00:00:00+00:00",
        retention_policy="cognitive_state",
    )


def _store_with_private_revision(tmp_path: Path) -> CognitiveStateStore:
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    store = CognitiveStateStore(db_path)
    revision = _revision(access_control=_access())
    command = LocalConsumerCommand.create(
        revision_id=revision.revision_id,
        consumer_id="wiki",
        command_type="project_user_reaction_event",
        payload={"revision_id": revision.revision_id},
        created_at=revision.created_at,
    )
    store.unit_of_work().commit(
        revisions=(revision,),
        event=_event(),
        commands=(command,),
    )
    return store


def test_cognitive_state_rejects_an_object_without_access_envelope() -> None:
    with pytest.raises(ValueError, match="access_control"):
        _revision()


def test_authorized_state_read_filters_before_returning_the_body(tmp_path: Path) -> None:
    store = _store_with_private_revision(tmp_path)

    missing_principal, missing_summary = store.authorized_current_revisions(
        principal=None,
        narrowing=AccessNarrowing(session_id="session-1", project="mnemos"),
        purpose="cognitive_state_read",
    )
    cross_agent, cross_agent_summary = store.authorized_current_revisions(
        principal=_principal("claude"),
        narrowing=AccessNarrowing(session_id="session-1", project="mnemos"),
        purpose="cognitive_state_read",
    )
    wrong_session, wrong_session_summary = store.authorized_current_revisions(
        principal=_principal(),
        narrowing=AccessNarrowing(session_id="session-2", project="mnemos"),
        purpose="cognitive_state_read",
    )
    allowed, allowed_summary = store.authorized_current_revisions(
        principal=_principal(),
        narrowing=AccessNarrowing(session_id="session-1", project="mnemos"),
        purpose="cognitive_state_read",
    )

    assert missing_principal == ()
    assert missing_summary["denied_by_reason"] == {"principal_required": 1}
    assert cross_agent == ()
    assert cross_agent_summary["denied_by_reason"] == {"owner_agent_mismatch": 1}
    assert wrong_session == ()
    assert wrong_session_summary["denied_by_reason"] == {"session_scope_mismatch": 1}
    assert len(allowed) == 1
    assert allowed[0].payload["subject_ref"] == {
        "type": "access_control_test",
        "id": "session-1",
    }
    assert allowed_summary["authorized_count"] == 1


def test_state_authorization_queries_small_header_before_any_revision_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store_with_private_revision(tmp_path)
    statements: list[str] = []
    original_connect = store._connect

    def traced_connect(*, read_only: bool = False) -> sqlite3.Connection:
        connection = original_connect(read_only=read_only)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(store, "_connect", traced_connect)

    denied, _summary = store.authorized_current_revisions(
        principal=_principal("claude"),
        narrowing=AccessNarrowing(session_id="session-1", project="mnemos"),
        purpose="cognitive_state_read",
    )

    normalized = [" ".join(statement.split()).lower() for statement in statements]
    assert denied == ()
    assert any("join typed_search_state_headers as search" in sql for sql in normalized)
    assert not any("json_extract(r.payload_json" in sql for sql in normalized)
    assert not any(
        "select * from cognitive_state_revisions" in sql for sql in normalized
    )

    statements.clear()
    allowed, _summary = store.authorized_current_revisions(
        principal=_principal(),
        narrowing=AccessNarrowing(session_id="session-1", project="mnemos"),
        purpose="cognitive_state_read",
    )
    normalized = [" ".join(statement.split()).lower() for statement in statements]
    header_query = next(
        index
        for index, sql in enumerate(normalized)
        if "from cognitive_state_heads as h join typed_search_state_headers as search" in sql
    )
    body_query = next(
        index
        for index, sql in enumerate(normalized)
        if "select * from cognitive_state_revisions" in sql
    )
    assert len(allowed) == 1
    assert header_query < body_query


def test_single_revision_authorization_uses_header_before_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store_with_private_revision(tmp_path)
    revision_id = store.current_revisions()[0].revision_id
    statements: list[str] = []
    original_connect = store._connect

    def traced_connect(*, read_only: bool = False) -> sqlite3.Connection:
        connection = original_connect(read_only=read_only)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(store, "_connect", traced_connect)
    denied, reason = store.authorized_revision(
        revision_id,
        principal=_principal("claude"),
        narrowing=AccessNarrowing(session_id="session-1", project="mnemos"),
        purpose="cognitive_state_read",
    )

    normalized = [" ".join(statement.split()).lower() for statement in statements]
    assert denied is None
    assert reason == "owner_agent_mismatch"
    assert any("from typed_search_state_headers as search" in sql for sql in normalized)
    assert not any("json_extract(r.payload_json" in sql for sql in normalized)
    assert not any(
        "select * from cognitive_state_revisions" in sql for sql in normalized
    )


def test_application_read_model_has_no_default_public_path(tmp_path: Path) -> None:
    store = _store_with_private_revision(tmp_path)
    service = CognitiveStateApplicationService(store.db_path)

    denied = service.build_cognitive_state(
        {"scope_type": "session", "scope_id": "session-1"}
    )
    allowed = service.build_cognitive_state(
        {"scope_type": "session", "scope_id": "session-1"},
        principal=_principal(),
        narrowing=AccessNarrowing(session_id="session-1", project="mnemos"),
        purpose="cognitive_state_read",
    )

    assert denied["status"] == "access_denied"
    assert denied["items"] == []
    assert denied["access"]["denied_by_reason"] == {"principal_required": 1}
    assert allowed["status"] == "available"
    assert len(allowed["items"]) == 1


def test_derived_access_uses_the_strictest_source_when_scopes_conflict() -> None:
    derived = derive_strictest_cognitive_access(
        (_access(session_id="session-1"), _access(session_id="session-2")),
        owner_principal_id="mcp:codex:test",
        owner_agent="codex",
        scope_type="session",
        scope_id="session-1",
        purposes=("cognitive_state_read",),
        retention_policy="inherit_source",
    )

    assert derived["visibility"] == "restricted"
    assert derived["scope"]["resolution"] == "restricted_unknown"
    assert derived["declassification"]["state"] == "not_requested"


def test_derived_access_cannot_rebind_a_source_to_a_different_scope_or_agent() -> None:
    source = _access(session_id="session-1")

    different_scope = derive_strictest_cognitive_access(
        (source,),
        owner_principal_id="mcp:codex:derived",
        owner_agent="codex",
        scope_type="session",
        scope_id="session-2",
        purposes=("cognitive_state_read",),
        retention_policy="inherit_source",
    )
    different_agent = derive_strictest_cognitive_access(
        (source,),
        owner_principal_id="mcp:claude:derived",
        owner_agent="claude",
        scope_type="session",
        scope_id="session-1",
        purposes=("cognitive_state_read",),
        retention_policy="inherit_source",
    )

    assert different_scope["scope"]["resolution"] == "restricted_unknown"
    assert different_agent["scope"]["resolution"] == "restricted_unknown"


def test_derived_access_can_bind_a_new_object_in_the_same_session_scope() -> None:
    source = _access(session_id="session-1")

    derived = derive_strictest_cognitive_access(
        (source,),
        owner_principal_id="mcp:codex:test",
        owner_agent="codex",
        scope_type="reflection",
        scope_id="reflection-1",
        purposes=("cognitive_state_read",),
        retention_policy="reflection_retention",
    )

    assert derived["scope"] == {
        "scope_type": "reflection",
        "scope_id": "reflection-1",
        "project": "mnemos",
        "session_id": "session-1",
        "resolution": "resolved",
    }
    assert derived["visibility"] == "private"


def test_cognitive_write_requires_the_server_principal_and_source_purpose() -> None:
    allowed = authorize_cognitive_write(
        _access(),
        principal=PrincipalEnvelope(
            principal_id="mcp:codex:test",
            agent="codex",
            host_kind="test",
            capability_id="test-capability",
            capabilities=frozenset({"memory_write"}),
            allowed_projects=frozenset({"mnemos"}),
        ),
        scope_type="session",
        scope_id="session-1",
    )
    denied = authorize_cognitive_write(
        _access(),
        principal=_principal(),
        scope_type="session",
        scope_id="session-1",
    )

    assert allowed.allowed is True
    assert denied.reason == "principal_write_capability_missing"
