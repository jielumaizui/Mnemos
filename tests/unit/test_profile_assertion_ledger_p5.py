from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest


def _access():
    from core.cognitive.access_control import make_cognitive_access_envelope

    return make_cognitive_access_envelope(
        owner_principal_id="mcp:codex:assertion-ledger",
        owner_agent="codex",
        scope_type="session",
        scope_id="assertion-ledger-session",
        session_id="assertion-ledger-session",
        purposes=("persona_preflight_read", "persona_usage_metrics"),
        consent_provenance_refs=("raw-revision:assertion-ledger",),
        sensitivity="sensitive",
        retention_policy="persona_retention",
        source_acl_lineage=("sha256:" + "a" * 64,),
        visibility="private",
    )


def _seed(tmp_path: Path):
    from core.persona.psyche import ProfileAssertion, ProfileSignal, SignalStore

    store = SignalStore(initialize_schema=True, db_path=tmp_path / "user_signals.db")
    signal_id = store.record_profile_signal(
        ProfileSignal(
            source_event_id="assertion-ledger-source",
            signal_type="explicit_preference",
            dimension="judgment_standard",
            value="每个结论必须附验证证据。",
            access_control=_access(),
        )
    )
    assertion_id = "pa_judgment_standard_evidence_contract"
    store.upsert_profile_assertion(
        ProfileAssertion(
            assertion_id=assertion_id,
            dimension="judgment_standard",
            claim="每个结论必须附验证证据。",
            supporting_signals=[f"profile_signals:{signal_id}"],
            confidence=0.9,
        )
    )
    revision_id = store.get_profile_assertion_revisions(assertion_id)[-1]["revision_id"]
    return store, assertion_id, revision_id


def test_assertion_revisions_reject_direct_update_and_delete(tmp_path: Path) -> None:
    store, assertion_id, revision_id = _seed(tmp_path)
    try:
        conn = store._pool.get_conn()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE profile_assertion_revisions SET claim=? WHERE revision_id=?",
                ("tampered", revision_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "DELETE FROM profile_assertion_revisions WHERE revision_id=?",
                (revision_id,),
            )
        assert store.get_profile_assertion_revisions(assertion_id)[-1]["claim"] != "tampered"
    finally:
        store.close()


def test_new_assertion_requires_identity_separate_from_mutable_claim(tmp_path: Path) -> None:
    from core.persona.psyche import ProfileAssertion, SignalStore

    store = SignalStore(initialize_schema=True, db_path=tmp_path / "user_signals.db")
    try:
        with pytest.raises(ValueError, match="stable assertion_id"):
            store.upsert_profile_assertion(
                ProfileAssertion(
                    assertion_id="",
                    dimension="judgment_standard",
                    claim="mutable claim text cannot be its identity",
                    supporting_signals=[],
                )
            )
    finally:
        store.close()


def test_correction_requires_current_head_and_keeps_assertion_identity(tmp_path: Path) -> None:
    from core.persona.psyche import ProfileAssertion

    store, assertion_id, revision_id = _seed(tmp_path)
    try:
        with pytest.raises(ValueError, match="expected_revision_id"):
            store.upsert_profile_assertion(
                ProfileAssertion(
                    assertion_id=assertion_id,
                    dimension="judgment_standard",
                    claim="每个结论必须附验证证据、失败边界和回归命令。",
                    supporting_signals=["profile_signals:1"],
                    confidence=0.95,
                )
            )
        store.upsert_profile_assertion(
            ProfileAssertion(
                assertion_id=assertion_id,
                dimension="judgment_standard",
                claim="每个结论必须附验证证据、失败边界和回归命令。",
                supporting_signals=["profile_signals:1"],
                confidence=0.95,
                expected_revision_id=revision_id,
            )
        )
        revisions = store.get_profile_assertion_revisions(assertion_id)
        assert len(revisions) == 2
        assert revisions[-1]["supersedes_revision_id"] == revision_id
        conn = store._pool.get_conn()
        head = conn.execute(
            "SELECT revision_id FROM profile_assertion_heads WHERE assertion_id=?",
            (assertion_id,),
        ).fetchone()[0]
        projection = conn.execute(
            "SELECT current_revision_id, claim FROM profile_assertions WHERE assertion_id=?",
            (assertion_id,),
        ).fetchone()
        assert projection[0] == head == revisions[-1]["revision_id"]
        assert projection[1] == revisions[-1]["claim"]
        with pytest.raises(ValueError, match="stale expected_revision_id"):
            store.upsert_profile_assertion(
                ProfileAssertion(
                    assertion_id=assertion_id,
                    dimension="judgment_standard",
                    claim="stale competing correction",
                    supporting_signals=["profile_signals:1"],
                    expected_revision_id=revision_id,
                )
            )
    finally:
        store.close()


def test_projection_rebuild_restores_exact_ledger_head(tmp_path: Path) -> None:
    store, assertion_id, revision_id = _seed(tmp_path)
    try:
        conn = store._pool.get_conn()
        conn.execute(
            "UPDATE profile_assertions SET claim=?, current_revision_id=? WHERE assertion_id=?",
            ("corrupted projection", "forged-revision", assertion_id),
        )
        conn.commit()
        report = store.rebuild_profile_assertion_projection(assertion_id)
        assert report["repaired"] is True
        assert report["revision_id"] == revision_id
        restored = (
            store._pool.get_conn()
            .execute(
                "SELECT current_revision_id, claim FROM profile_assertions WHERE assertion_id=?",
                (assertion_id,),
            )
            .fetchone()
        )
        assert restored[0] == revision_id
        assert restored[1] == "每个结论必须附验证证据。"
    finally:
        store.close()


def test_fresh_store_registers_the_canonical_assertion_schema(tmp_path: Path) -> None:
    from core.persona.profile_assertion_schema import inspect_profile_assertion_schema

    store, _assertion_id, _revision_id = _seed(tmp_path)
    try:
        state = inspect_profile_assertion_schema(store._pool.get_conn())
        assert state.ok is True
        assert state.registry_version == "mnemos.profile_assertion_ledger.v1"
        assert state.registry_hash.startswith("sha256:")
    finally:
        store.close()


def test_schema_registry_rejects_live_trigger_drift(tmp_path: Path) -> None:
    from core.persona.profile_assertion_schema import inspect_profile_assertion_schema

    store, _assertion_id, _revision_id = _seed(tmp_path)
    try:
        conn = store._pool.get_conn()
        conn.execute("DROP TRIGGER profile_assertion_revisions_no_update")
        state = inspect_profile_assertion_schema(conn)
        assert state.ok is False
        assert "profile_assertion_revisions_no_update_missing" in state.errors
        assert any(
            error.startswith("profile_assertion_live_schema_hash_mismatch:")
            for error in state.errors
        )
    finally:
        store.close()


def test_two_corrections_cannot_branch_from_the_same_expected_head(tmp_path: Path) -> None:
    from core.persona.psyche import ProfileAssertion, SignalStore

    seed_store, assertion_id, revision_id = _seed(tmp_path)
    database = seed_store.db_path
    seed_store.close()
    stores = [
        SignalStore(db_path=database),
        SignalStore(db_path=database),
    ]
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    outcomes_lock = threading.Lock()

    def correct(store: SignalStore, claim: str) -> None:
        barrier.wait()
        try:
            store.upsert_profile_assertion(
                ProfileAssertion(
                    assertion_id=assertion_id,
                    dimension="judgment_standard",
                    claim=claim,
                    supporting_signals=["profile_signals:1"],
                    expected_revision_id=revision_id,
                )
            )
            outcome = "committed"
        except ValueError as exc:
            outcome = str(exc)
        with outcomes_lock:
            outcomes.append(outcome)

    threads = [
        threading.Thread(target=correct, args=(stores[0], "correction-a")),
        threading.Thread(target=correct, args=(stores[1], "correction-b")),
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert all(not thread.is_alive() for thread in threads)
        assert outcomes.count("committed") == 1
        assert outcomes.count("stale expected_revision_id for assertion correction") == 1
        with sqlite3.connect(database) as conn:
            revisions = conn.execute(
                "SELECT revision_id, supersedes_revision_id "
                "FROM profile_assertion_revisions WHERE assertion_id=?",
                (assertion_id,),
            ).fetchall()
            assert len(revisions) == 2
            assert sum(parent == revision_id for _revision, parent in revisions) == 1
    finally:
        for store in stores:
            store.close()
