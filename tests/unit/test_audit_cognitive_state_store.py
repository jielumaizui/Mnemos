from __future__ import annotations

from pathlib import Path

from scripts.audit_cognitive_state_store import build_report


def test_cognitive_state_audit_proves_all_acceptance_metrics(tmp_path: Path) -> None:
    report = build_report(
        state_db_path=tmp_path / "missing-state.db",
        action_db_path=tmp_path / "missing-action.db",
    )

    assert report["ok"] is True
    assert report["ddl_owners"]["cognitive_state"] == [
        "core/cognitive/state_schema_ddl.py"
    ]
    assert report["synthetic"]["rollback_clean"] is True
    assert report["synthetic"]["action_update_delete_rejected"] is True
    assert all(
        report["synthetic"]["metrics"][metric] == 0
        for metric in (
            "metadata_only_cognition",
            "consumed_without_event",
            "aggregate_consumed_with_missing_consumer",
            "multiple_current_revision",
            "mutable_action_evidence",
            "semantic_revision_without_envelope",
            "envelope_without_semantic_revision",
            "partial_facade_commit",
            "outbox_without_source_commit",
            "effect_receipt_reciprocity_gap",
            "effect_receipt_evidence_gap",
            "revision_hash_mismatch",
            "outbox_hash_mismatch",
        )
    )


def test_cognitive_state_audit_rejects_a_second_ddl_owner(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "core" / "cognitive").mkdir(parents=True)
    (root / "core" / "ops").mkdir(parents=True)
    (root / "core" / "cognitive" / "state_schema_ddl.py").write_text(
        "CREATE TABLE cognitive_state_revisions (revision_id TEXT);\n"
        "CREATE TABLE cognitive_data_events (event_id TEXT);\n",
        encoding="utf-8",
    )
    (root / "core" / "ops" / "action_ledger_schema.py").write_text(
        "CREATE TABLE action_ledger (action_id TEXT);\n",
        encoding="utf-8",
    )
    (root / "core" / "rogue.py").write_text(
        "DDL = '''CREATE TABLE cognitive_state_revisions (id TEXT);'''\n",
        encoding="utf-8",
    )

    report = build_report(
        state_db_path=tmp_path / "missing-state.db",
        action_db_path=tmp_path / "missing-action.db",
        root=root,
    )

    assert report["ok"] is False
    assert report["ddl_owners"]["cognitive_state"] == [
        "core/cognitive/state_schema_ddl.py",
        "core/rogue.py",
    ]
    assert any("cognitive state DDL owner drift" in error for error in report["errors"])
