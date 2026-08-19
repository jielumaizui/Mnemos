from __future__ import annotations

import sqlite3

import pytest

from core.cognitive.access_control import make_cognitive_access_envelope
from core.ops.action_ledger_subject_provenance import delete_action_ledger_subject_scope
from core.system_contracts import ActionLedger, make_quality_gate_observation


def _provenance(session_id: str) -> dict:
    return make_cognitive_access_envelope(
        owner_principal_id="mcp:codex:action-ledger-test",
        owner_agent="codex",
        scope_type="session",
        scope_id=session_id,
        session_id=session_id,
        project="mnemos",
        purposes=("action_ledger_read",),
        consent_provenance_refs=("raw:action-ledger-test",),
        sensitivity="sensitive",
        retention_policy="test_retention",
        source_acl_lineage=("sha256:" + "b" * 64,),
        visibility="private",
    )


def _record(*, session_id: str = "ledger-session", subject_provenance=None):
    return make_quality_gate_observation(
        actor="action-ledger-test",
        target=f"private-target:{session_id}",
        evidence_refs=(f"private-evidence:{session_id}",),
        details={"private": session_id},
        subject_provenance=subject_provenance,
    )


def test_scoped_delete_tombstones_tracked_action_and_redacts_public_projection(tmp_path):
    ledger = ActionLedger(tmp_path / "action_ledger.db", initialize=True)
    record = _record(subject_provenance=_provenance("ledger-session"))
    action_id = ledger.record_observation(record)

    result = delete_action_ledger_subject_scope(
        db_path=ledger.db_path,
        request_id="delete-action-ledger-1",
        scope_kind="session",
        scope_value="ledger-session",
    )

    assert result["status"] == "applied"
    assert result["verified"] is True
    assert result["tombstoned_count"] == 1
    row = ledger.recent()[0]
    assert row["action_id"] == action_id
    assert row["target"] == "[redacted:subject-deleted]"
    assert row["actor"] == "[redacted:subject-deleted]"
    assert row["evidence_refs"] == []
    assert row["verification"]["redacted"] is True
    assert row["verification"]["record_hash"].startswith("sha256:")
    assert "ledger-session" not in str(row)

    # The original append-only evidence remains intact on disk; the public
    # facade is the only supported reader and is now tombstone-gated.
    with sqlite3.connect(ledger.db_path) as conn:
        stored_target = conn.execute(
            "SELECT target FROM action_ledger WHERE action_id=?", (action_id,)
        ).fetchone()[0]
    assert stored_target == "private-target:ledger-session"
    with pytest.raises(PermissionError, match="tombstoned"):
        ledger.record_observation(record)


def test_scoped_delete_never_guesses_an_unattributed_action_record(tmp_path):
    ledger = ActionLedger(tmp_path / "action_ledger.db", initialize=True)
    action_id = ledger.record_observation(_record())

    result = delete_action_ledger_subject_scope(
        db_path=ledger.db_path,
        request_id="delete-action-ledger-legacy",
        scope_kind="session",
        scope_value="ledger-session",
    )

    assert result["status"] == "applied"
    assert result["target_count"] == 0
    assert result["unresolved_legacy_count"] == 1
    assert result["verified"] is False
    assert ledger.recent()[0]["action_id"] == action_id
    assert ledger.recent()[0]["target"] == "private-target:ledger-session"
