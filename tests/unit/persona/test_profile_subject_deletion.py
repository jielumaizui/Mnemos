from __future__ import annotations

import sqlite3

import pytest

from core.cognitive.access_control import make_cognitive_access_envelope
from core.privacy.data_ownership import DataOwnershipManager
from core.persona.cognitive_profile import (
    ProfileAssertion,
    ProfileSignal,
    ProfileUsageLog,
)
from core.persona.profile_subject_deletion import delete_profile_subject_scope
from core.persona.psyche import SignalStore


class _OwnershipConfig:
    def __init__(self, root):
        self.mnemos_dir = root
        self.data_dir = root
        self.database_dir = root / "db"
        self.database_dir.mkdir(parents=True)

    def get(self, _key, default=None):
        return default


def _access() -> dict:
    return make_cognitive_access_envelope(
        owner_principal_id="mcp:codex:persona-delete",
        owner_agent="codex",
        scope_type="session",
        scope_id="persona-delete-session",
        session_id="persona-delete-session",
        project="mnemos",
        purposes=(
            "persona_preflight_read",
            "persona_summary_read",
            "persona_usage_metrics",
        ),
        consent_provenance_refs=("raw:persona-delete",),
        sensitivity="sensitive",
        retention_policy="persona_retention",
        source_acl_lineage=("sha256:" + "d" * 64,),
        visibility="private",
    )


def _read_principal():
    from core.access_policy import PrincipalEnvelope

    return PrincipalEnvelope(
        principal_id="mcp:codex:persona-delete",
        agent="codex",
        host_kind="codex",
        capability_id="persona-delete",
        capabilities=frozenset({"memory_read"}),
        allowed_projects=frozenset({"mnemos"}),
    )


def _read_narrowing():
    from core.access_policy import AccessNarrowing

    return AccessNarrowing(
        session_id="persona-delete-session",
        project="mnemos",
    )


def _read_token(store) -> str:
    _profile, access = store.build_authorized_user_cognitive_profile_v2(
        principal=_read_principal(),
        narrowing=_read_narrowing(),
        purpose="persona_preflight_read",
        consumer="preflight_builder",
    )
    return str(access["read_authorization_token"])


def test_profile_subject_delete_removes_signal_assertion_and_usage_with_receipts(tmp_path):
    database = tmp_path / "user_signals.db"
    store = SignalStore(initialize_schema=True, db_path=database)
    try:
        signal_id = store.record_profile_signal(
            ProfileSignal(
                source_event_id="raw-persona-delete",
                signal_type="preference",
                dimension="detail",
                value="needs deletion",
                access_control=_access(),
            )
        )
        assertion_id = store.upsert_profile_assertion(
            ProfileAssertion(
                assertion_id="persona-delete-assertion",
                dimension="detail",
                claim="derived assertion that must be deleted",
                supporting_signals=[f"profile_signals:{signal_id}"],
            )
        )
        from core.persona.profile_effect import compare_profile_effect

        revision_id = str(store.get_profile_assertion_revisions(assertion_id)[-1]["revision_id"])
        store.record_profile_usage(
            ProfileUsageLog(
                consumer="preflight_builder",
                profile_fields_used=[assertion_id],
                read_purpose="persona_preflight_read",
                read_authorization_token=_read_token(store),
                target_receipt=compare_profile_effect(
                    owner="preflight_builder",
                    target_type="test_target",
                    target_id="profile_subject_deletion",
                    matched_assertion_revisions={assertion_id: revision_id},
                    baseline_output="before",
                    persona_enabled_output="after",
                    expected_delta={"kind": "test_delta"},
                    receipt_id="profile-subject-deletion-target",
                ),
                outcome="derived usage that must be deleted",
            ),
            principal=_read_principal(),
            narrowing=_read_narrowing(),
        )
        result = delete_profile_subject_scope(
            db_path=database,
            request_id="delete-persona-test",
            scope_kind="session",
            scope_value="persona-delete-session",
        )
    finally:
        store.close()

    assert result == {
        "status": "applied",
        "target_count": 5,
        "receipt_count": 5,
        "profile_signals_deleted": 1,
        "profile_assertions_deleted": 1,
        "profile_usage_logs_deleted": 1,
        "profile_read_authorizations_deleted": 1,
        "profile_usage_outboxes_deleted": 1,
        "unresolved_legacy_count": 0,
        "unmapped_legacy_persona_count": 0,
        "verified": True,
    }
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM profile_signals").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM profile_assertions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM profile_assertion_revisions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM profile_assertion_heads").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM profile_assertion_revision_delete_permits"
            ).fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM profile_usage_log").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM profile_read_authorizations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM profile_usage_outbox").fetchone()[0] == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM profile_subject_deletion_receipts").fetchone()[0]
            == 5
        )


def test_profile_subject_delete_removes_pending_usage_outbox_after_crash(tmp_path):
    database = tmp_path / "user_signals.db"
    store = SignalStore(initialize_schema=True, db_path=database)
    try:
        signal_id = store.record_profile_signal(
            ProfileSignal(
                source_event_id="raw-persona-pending-delete",
                signal_type="preference",
                dimension="detail",
                value="pending outbox needs deletion",
                access_control=_access(),
            )
        )
        assertion_id = store.upsert_profile_assertion(
            ProfileAssertion(
                assertion_id="persona-pending-delete-assertion",
                dimension="detail",
                claim="pending outbox derived assertion",
                supporting_signals=[f"profile_signals:{signal_id}"],
            )
        )
        from core.persona.profile_effect import compare_profile_effect

        revision_id = str(store.get_profile_assertion_revisions(assertion_id)[-1]["revision_id"])
        usage = ProfileUsageLog(
            consumer="preflight_builder",
            profile_fields_used=[assertion_id],
            read_purpose="persona_preflight_read",
            read_authorization_token=_read_token(store),
            target_receipt=compare_profile_effect(
                owner="preflight_builder",
                target_type="test_target",
                target_id="profile_pending_subject_deletion",
                matched_assertion_revisions={assertion_id: revision_id},
                baseline_output="before",
                persona_enabled_output="after",
                expected_delta={"kind": "test_delta"},
                receipt_id="profile-pending-subject-deletion-target",
            ),
        )

        def crash_after_outbox(phase: str) -> None:
            if phase == "after_usage_outbox_commit":
                raise RuntimeError("simulated crash")

        with pytest.raises(RuntimeError, match="simulated crash"):
            store._cognitive_profiles.record_usage(
                usage,
                principal=_read_principal(),
                narrowing=_read_narrowing(),
                _failpoint=crash_after_outbox,
            )
        result = delete_profile_subject_scope(
            db_path=database,
            request_id="delete-pending-persona-test",
            scope_kind="session",
            scope_value="persona-delete-session",
        )
    finally:
        store.close()

    assert result["status"] == "applied"
    assert result["target_count"] == 4
    assert result["receipt_count"] == 4
    assert result["profile_usage_logs_deleted"] == 0
    assert result["profile_read_authorizations_deleted"] == 1
    assert result["profile_usage_outboxes_deleted"] == 1
    assert result["verified"] is True
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM profile_usage_outbox").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM profile_usage_log").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM profile_read_authorizations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM profile_signals").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM profile_assertions").fetchone()[0] == 0


def test_profile_subject_delete_refuses_to_verify_legacy_unscoped_rows(tmp_path):
    database = tmp_path / "user_signals.db"
    store = SignalStore(initialize_schema=True, db_path=database)
    try:
        with sqlite3.connect(database) as conn:
            conn.execute(
                """
                INSERT INTO profile_signals (
                    source_event_id, signal_type, dimension, value, observed_at, access_control
                ) VALUES (?, ?, ?, ?, ?, '')
                """,
                ("legacy-event", "legacy", "legacy", "legacy body", "2026-07-16T00:00:00"),
            )
            conn.commit()
        result = delete_profile_subject_scope(
            db_path=database,
            request_id="delete-persona-legacy",
            scope_kind="session",
            scope_value="persona-delete-session",
        )
    finally:
        store.close()

    assert result["status"] == "no_targets"
    assert result["unresolved_legacy_count"] == 1
    assert result["verified"] is False


def test_profile_subject_delete_refuses_to_verify_unmapped_legacy_persona_rows(tmp_path):
    database = tmp_path / "user_signals.db"
    store = SignalStore(initialize_schema=True, db_path=database)
    try:
        with sqlite3.connect(database) as conn:
            conn.execute(
                "INSERT INTO session_signals (session_id, timestamp, agent) VALUES (?, ?, ?)",
                ("different-session", "2026-07-16T00:00:00", "codex"),
            )
            conn.commit()
        result = delete_profile_subject_scope(
            db_path=database,
            request_id="delete-persona-unmapped-legacy",
            scope_kind="session",
            scope_value="persona-delete-session",
        )
    finally:
        store.close()

    assert result["status"] == "no_targets"
    assert result["unmapped_legacy_persona_count"] == 1
    assert result["unresolved_legacy_count"] == 1
    assert result["verified"] is False


def test_profile_write_is_blocked_after_matching_ownership_freeze(tmp_path):
    config = _OwnershipConfig(tmp_path)
    database = config.database_dir / "user_signals.db"
    store = SignalStore(initialize_schema=True, db_path=database, config=config)
    try:
        DataOwnershipManager(config).freeze("session:persona-delete-session")
        with pytest.raises(PermissionError, match="matching frozen data ownership scope"):
            store.record_profile_signal(
                ProfileSignal(
                    source_event_id="raw-persona-delete",
                    signal_type="preference",
                    dimension="detail",
                    value="must not be persisted after freeze",
                    access_control=_access(),
                )
            )
    finally:
        store.close()

    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM profile_signals").fetchone()[0] == 0
