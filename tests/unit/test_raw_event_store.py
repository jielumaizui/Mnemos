# -*- coding: utf-8 -*-

from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timedelta
import sqlite3
import zlib

import pytest

import core.sync_framework.raw_event_store as raw_store_module
from core.ops.readiness_query_budget import connect_readonly_sqlite
from core.sync_framework.agent_source import SessionInfo, Turn
from core.sync_framework import raw_current_projection_reconciliation
from core.sync_framework import raw_event_reader
from core.sync_framework import native_raw_recovery_evidence
from core.sync_framework.native_raw_recovery_evidence import (
    NativeRawRecoveryEvidenceError,
    compare_raw_conservation,
    raw_conservation_evidence,
)
from core.sync_framework.native_raw_contract_ledger import NativeRawContractLedger
from core.sync_framework.raw_event_store import RawEventStore, classify_completeness
from core.sync_framework.raw_event_identity import (
    RawEventIdentitySchemaMigrationRequired,
)
from core.sync_framework.raw_event_reader import decode_raw_revision_snapshot
from core.sync_framework.source_support import build_native_raw_metadata


class _Cfg:
    def __init__(self, database_dir: Path):
        self.database_dir = database_dir

    def get(self, key, default=None):  # noqa: ARG002
        return default


def test_canonical_raw_snapshot_rejects_invalid_utf8_instead_of_replacing_bytes() -> None:
    snapshot = zlib.compress(
        b'{"user_content":"before\xffafter","assistant_content":""}'
    )

    with pytest.raises(ValueError, match="invalid canonical Raw revision snapshot"):
        decode_raw_revision_snapshot(snapshot)


def test_native_contract_refresh_rejects_corrupt_current_snapshot_without_state_write(
    tmp_path: Path,
) -> None:
    store = RawEventStore(config=_Cfg(tmp_path))
    try:
        revision_id = store.upsert_turn(
            source_agent="codex",
            session_id="corrupt-contract-refresh",
            turn_number=0,
            user_content="original",
            assistant_content="",
            metadata={"native_event_id": "corrupt-contract-refresh"},
        )
        conn = store._pool.get_conn()  # noqa: SLF001
        logical_event_id = str(
            conn.execute(
                "SELECT logical_event_id FROM raw_turn_revisions WHERE revision_id=?",
                (revision_id,),
            ).fetchone()[0]
        )
        conn.execute(
            "UPDATE raw_turn_revisions SET snapshot_blob=? WHERE revision_id=?",
            (
                zlib.compress(
                    b'{"completeness_status":"complete",'
                    b'"metadata":{"native":"before\xffafter"}}'
                ),
                revision_id,
            ),
        )
        ledger = NativeRawContractLedger()
        ledger.record_explicit(
            conn,
            logical_event_id=logical_event_id,
            revision_id=revision_id,
            support_manifest_hash="manifest",
            contract_state="conformant",
            contract_errors=[],
            observed_at="2026-07-29T00:00:00+00:00",
        )
        before = conn.execute(
            "SELECT completeness_status, metadata_json, updated_at "
            "FROM raw_turns WHERE event_id=?",
            (logical_event_id,),
        ).fetchone()

        with pytest.raises(
            ValueError,
            match="native raw current snapshot is invalid",
        ):
            ledger.refresh_effective_state(
                conn,
                logical_event_id=logical_event_id,
                observed_at="2026-07-29T00:00:00+00:00",
            )

        after = conn.execute(
            "SELECT completeness_status, metadata_json, updated_at "
            "FROM raw_turns WHERE event_id=?",
            (logical_event_id,),
        ).fetchone()
        assert after == before
    finally:
        store.close()


def test_native_contract_ledger_rejects_malformed_observation_payload(
    tmp_path: Path,
) -> None:
    store = RawEventStore(config=_Cfg(tmp_path))
    try:
        revision_id = store.upsert_turn(
            source_agent="codex",
            session_id="corrupt-contract-observation",
            turn_number=0,
            user_content="original",
            assistant_content="",
            metadata={"native_event_id": "corrupt-contract-observation"},
        )
        conn = store._pool.get_conn()  # noqa: SLF001
        logical_event_id = str(
            conn.execute(
                "SELECT logical_event_id FROM raw_turn_revisions WHERE revision_id=?",
                (revision_id,),
            ).fetchone()[0]
        )
        conn.execute(
            """
            INSERT INTO raw_native_contract_observations (
                observation_id, logical_event_id, observed_revision_id,
                support_manifest_hash, contract_state,
                contract_errors_json, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "malformed-observation",
                logical_event_id,
                revision_id,
                "manifest",
                "conformant",
                "{not-json",
                "2026-07-29T00:00:00+00:00",
            ),
        )
        ledger = NativeRawContractLedger()

        with pytest.raises(
            ValueError,
            match="native raw contract observation is invalid",
        ):
            ledger.latest(conn, logical_event_id)
        with pytest.raises(
            ValueError,
            match="native raw contract observation is invalid",
        ):
            ledger.list_for_event(conn, logical_event_id=logical_event_id)
    finally:
        store.close()


def test_raw_store_late_schema_failure_restores_exact_preimage_and_closes_pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LateSchemaAbort(BaseException):
        pass

    database = tmp_path / "raw_events.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE preimage_sentinel (value TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO preimage_sentinel(value) VALUES ('unchanged')")

    closed_pools: list[object] = []
    original_close = raw_store_module.SqlitePool.close

    def observed_close(pool: object) -> None:
        closed_pools.append(pool)
        original_close(pool)  # type: ignore[arg-type]

    def fail_after_coupled_schema(_connection: sqlite3.Connection) -> None:
        raise LateSchemaAbort("sentinel late raw schema failure")

    monkeypatch.setattr(raw_store_module.SqlitePool, "close", observed_close)
    monkeypatch.setattr(
        raw_store_module,
        "ensure_subject_deletion_schema",
        fail_after_coupled_schema,
    )

    with pytest.raises(LateSchemaAbort, match="sentinel late raw schema failure"):
        RawEventStore(config=_Cfg(tmp_path))

    assert len(closed_pools) == 1
    with sqlite3.connect(database) as connection:
        user_objects = connection.execute("""
            SELECT type, name
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """).fetchall()
        sentinel_rows = connection.execute("SELECT value FROM preimage_sentinel").fetchall()
    assert user_objects == [("table", "preimage_sentinel")]
    assert sentinel_rows == [("unchanged",)]


def test_raw_upsert_late_failure_rolls_back_every_coupled_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LateRawWriteAbort(BaseException):
        pass

    class _NativeCodex:
        name = "codex"

        def completeness_capabilities(self):
            return {"source_fidelity": "full"}

    store = RawEventStore(config=_Cfg(tmp_path))
    session = SessionInfo(
        session_id="atomic-raw-upsert",
        source_path=tmp_path / "atomic.jsonl",
    )
    turn = Turn(
        turn_number=0,
        user_content="atomic user",
        assistant_content="atomic assistant",
    )

    def fail_after_revision_write(*_args, **_kwargs) -> None:
        raise LateRawWriteAbort("late native contract projection failure")

    monkeypatch.setattr(
        raw_store_module._NATIVE_RAW_CONTRACT_LEDGER,  # noqa: SLF001
        "record",
        fail_after_revision_write,
    )
    try:
        with pytest.raises(
            LateRawWriteAbort,
            match="late native contract projection failure",
        ):
            store.upsert_turn(
                source_agent="codex",
                session_id=session.session_id,
                turn_number=turn.turn_number,
                user_content=turn.user_content,
                assistant_content=turn.assistant_content,
                metadata=build_native_raw_metadata(_NativeCodex(), session, turn),
                completeness={"visible_text": "full", "truncated": False},
            )

        connection = store._pool.get_conn()  # noqa: SLF001
        assert connection.in_transaction is False
        for table in (
            "raw_turns",
            "raw_turn_revisions",
            "raw_metrics",
            "raw_native_contract_observations",
        ):
            count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert count == 0
    finally:
        store.close()


@pytest.mark.parametrize(
    "operation",
    (
        "upsert",
        "provenance",
        "intentional_no_observation",
        "survival_refresh",
        "purge",
        "delete_raw_event",
    ),
)
def test_raw_write_transaction_never_reacquires_the_pool_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """A caller-owned BEGIN IMMEDIATE must survive every nested Raw helper."""
    store = RawEventStore(config=_Cfg(tmp_path))
    try:
        revision_id = store.upsert_turn(
            source_agent="codex",
            session_id="transaction-owner",
            turn_number=0,
            user_content="transaction owner user",
            assistant_content="transaction owner assistant",
        )
        logical_event_id = store.get_logical_event_id(revision_id)
        if operation == "purge":
            connection = store._pool.get_conn()  # noqa: SLF001
            connection.execute(
                "UPDATE raw_metrics SET retention_state='eligible_delete' " "WHERE event_id=?",
                (logical_event_id,),
            )
            connection.commit()

        pool = store._pool  # noqa: SLF001
        original_get_conn = pool.get_conn
        nested_transaction_reacquisition_count = 0

        def reject_nested_connection_acquisition() -> sqlite3.Connection:
            nonlocal nested_transaction_reacquisition_count
            live_connections = [
                *pool._conns.values(),  # noqa: SLF001
                *pool._transient_conns,  # noqa: SLF001
            ]
            if any(connection.in_transaction for connection in live_connections):
                nested_transaction_reacquisition_count += 1
                raise AssertionError("transaction owner reacquired SqlitePool.get_conn")
            return original_get_conn()

        monkeypatch.setattr(pool, "get_conn", reject_nested_connection_acquisition)
        monkeypatch.setattr(
            store._provenance,  # noqa: SLF001
            "_get_connection",
            reject_nested_connection_acquisition,
        )

        if operation == "upsert":
            store.upsert_turn(
                source_agent="codex",
                session_id="transaction-owner",
                turn_number=1,
                user_content="second user",
                assistant_content="second assistant",
            )
        elif operation == "provenance":
            store.record_provenance_edge(
                source_revision_id=revision_id,
                span_start=0,
                span_end=4,
                consumer_type="distill_chunk",
                consumer_id="transaction-owner:chunk-0",
            )
        elif operation == "intentional_no_observation":
            store.record_intentional_no_observation(
                source_revision_id=revision_id,
                reason="no_supported_signal",
            )
        elif operation == "survival_refresh":
            store.refresh_survival_scores(force=True)
        elif operation == "purge":
            store.purge_eligible_delete()
        else:
            store.delete_subject_scope(
                request_id="transaction-owner-delete",
                scope_kind="raw_event_id",
                scope_value=revision_id,
            )
        assert nested_transaction_reacquisition_count == 0
    finally:
        store.close()


def test_classify_completeness():
    assert classify_completeness({"visible_text": "full", "truncated": False}) == "complete"
    assert classify_completeness({"visible_text": "host_provided"}) == "derived"
    assert classify_completeness({"truncated": True}) == "partial"
    assert (
        classify_completeness({"loss_reasons": ["host_session_messages_may_be_compressed"]})
        == "partial"
    )
    assert classify_completeness({}, {"source_fidelity": "derived"}) == "derived"
    assert classify_completeness({}, {"source_fidelity": "experimental"}) == "derived"
    assert classify_completeness({}, {"source_fidelity": "unknown"}) == "partial"
    assert (
        classify_completeness({}, {"support_fidelity_contract_state": "observed_mismatch"})
        == "partial"
    )


def test_upsert_round_trips_compressed_content(tmp_path):
    store = RawEventStore(config=_Cfg(tmp_path))
    try:
        event_id = store.upsert_turn(
            source_agent="kimi",
            session_id="sess-1",
            turn_number=0,
            user_content="用户原文",
            assistant_content="助手原文",
            reasoning="完整推理",
            completeness={"visible_text": "full", "truncated": False},
        )
        row = store.get_turn(event_id)
        assert row is not None
        assert row["user_content"] == "用户原文"
        assert row["assistant_content"] == "助手原文"
        assert row["reasoning"] == "完整推理"
        assert row["completeness_status"] == "complete"
        assert row["raw_bytes"] > 0
    finally:
        store.close()


def test_raw_event_store_persists_structured_turn_fields_exactly_once(tmp_path):
    store = RawEventStore(config=_Cfg(tmp_path))
    structured = {
        "tool_calls": [{"name": "read", "sentinel": "one-copy"}],
        "tool_results": [{"output": "one-copy"}],
        "reasoning": "one-copy",
        "attachments": [{"name": "one-copy"}],
        "raw_event_refs": [{"event_type": "one-copy"}],
        "source_files": ["/tmp/one-copy"],
        "completeness": {"visible_text": "full", "sentinel": "one-copy"},
    }
    try:
        revision_id = store.upsert_turn(
            source_agent="codex",
            session_id="single-copy",
            turn_number=0,
            user_content="u",
            assistant_content="a",
            metadata={"owner": "test", **structured},
            tool_calls=structured["tool_calls"],
            tool_results=structured["tool_results"],
            reasoning=structured["reasoning"],
            attachments=structured["attachments"],
            raw_event_refs=structured["raw_event_refs"],
            source_files=structured["source_files"],
            completeness=structured["completeness"],
        )
        row = store.get_turn(revision_id)

        assert row is not None
        assert row["metadata"]["owner"] == "test"
        assert not (set(row["metadata"]) & set(structured))
        for key, value in structured.items():
            assert row[key] == value
    finally:
        store.close()


def test_view_does_not_increment_hit_count(tmp_path):
    store = RawEventStore(config=_Cfg(tmp_path))
    try:
        event_id = store.upsert_turn(
            source_agent="claude",
            session_id="sess-1",
            turn_number=0,
            user_content="hello",
            assistant_content="world",
        )
        store.record_access(event_id, "view", consumer="obsidian")
        metrics = store.get_metrics(event_id)
        assert metrics is not None
        assert metrics["view_count"] == 1
        assert metrics["hit_count"] == 0
    finally:
        store.close()


def test_complete_capture_not_downgraded_by_partial_sync(tmp_path):
    store = RawEventStore(config=_Cfg(tmp_path))
    try:
        event_id = store.upsert_turn(
            source_agent="codex",
            session_id="sess-1",
            turn_number=0,
            user_content="full user",
            assistant_content="full assistant",
            completeness={"visible_text": "full", "truncated": False},
            origin="sync_engine",
        )
        lower_revision_id = store.upsert_turn(
            source_agent="codex",
            session_id="sess-1",
            turn_number=0,
            user_content="partial user",
            assistant_content="partial assistant",
            completeness={
                "visible_text": "host_provided",
                "loss_reasons": ["host_session_messages_may_be_compressed"],
            },
            origin="capture_service",
        )
        row = store.get_turn(event_id)
        assert row is not None
        assert row["user_content"] == "full user"
        assert row["assistant_content"] == "full assistant"
        assert row["completeness_status"] == "complete"
        lower_revision = store.get_turn(lower_revision_id)
        assert lower_revision is not None
        assert lower_revision["user_content"] == "partial user"
        assert lower_revision["assistant_content"] == "partial assistant"
        revisions = store.list_revisions(
            source_agent="codex",
            session_id="sess-1",
            turn_number=0,
        )
        assert [revision["revision_id"] for revision in revisions] == [
            event_id,
            lower_revision_id,
        ]
        assert revisions[1]["supersedes_revision_id"] == event_id
    finally:
        store.close()


def test_native_event_identity_survives_turn_renumber_and_prevents_collision(tmp_path):
    store = RawEventStore(config=_Cfg(tmp_path))
    try:
        first = store.upsert_turn(
            source_agent="codex",
            session_id="native-renumber",
            turn_number=0,
            user_content="first user",
            assistant_content="first assistant",
            metadata={"native_event_id": "message-001"},
        )
        # A parser may insert an earlier record and renumber the same native
        # event.  It must retain the one revision chain.
        renumbered = store.upsert_turn(
            source_agent="codex",
            session_id="native-renumber",
            turn_number=7,
            user_content="first user",
            assistant_content="first assistant",
            metadata={"native_event_id": "message-001"},
        )
        distinct = store.upsert_turn(
            source_agent="codex",
            session_id="native-renumber",
            turn_number=0,
            user_content="second user",
            assistant_content="second assistant",
            metadata={"native_event_id": "message-002"},
        )

        assert renumbered == first
        assert distinct != first
        assert (
            store.find_event_id(
                source_agent="codex",
                session_id="native-renumber",
                turn_number=999,
                native_event_id="message-001",
            )
            == first
        )
        # Without native identity, the reused ordinal is intentionally
        # ambiguous rather than selecting an arbitrary event.
        assert (
            store.find_event_id(
                source_agent="codex",
                session_id="native-renumber",
                turn_number=0,
            )
            is None
        )
        assert store.get_turn(first)["metadata"]["logical_event_identity_kind"] == "native_event_id"
    finally:
        store.close()


def test_new_native_revision_renumber_updates_current_projection(tmp_path):
    store = RawEventStore(config=_Cfg(tmp_path))
    try:
        first = store.upsert_turn(
            source_agent="claude",
            session_id="native-revision-renumber",
            turn_number=0,
            user_content="first user",
            assistant_content="first assistant",
            metadata={"native_event_id": "message-001"},
        )
        updated = store.upsert_turn(
            source_agent="claude",
            session_id="native-revision-renumber",
            turn_number=7,
            user_content="updated user",
            assistant_content="updated assistant",
            metadata={"native_event_id": "message-001"},
        )
        current_revision_id = store.find_event_id(
            source_agent="claude",
            session_id="native-revision-renumber",
            turn_number=999,
            native_event_id="message-001",
        )

        assert current_revision_id is not None
        logical_event_id = store.get_logical_event_id(current_revision_id)
        assert updated != first
        with sqlite3.connect(store.db_path) as connection:
            assert (
                connection.execute(
                    """
                SELECT turn_number, current_revision_id
                FROM raw_turns WHERE event_id=?
                """,
                    (logical_event_id,),
                ).fetchone()
                == (7, updated)
            )
        evidence = raw_conservation_evidence(store.db_path)
        assert all(
            item["current_revision_projection_valid"] is True
            for item in evidence["raw_turns"]["turn_bindings"]
        )
    finally:
        store.close()


def test_native_replay_repairs_current_projection_before_lower_quality_revision(tmp_path):
    class _NativeCodex:
        name = "codex"

        def completeness_capabilities(self):
            return {"source_fidelity": "full"}

    session = SessionInfo(
        session_id="native-projection-repair",
        source_path=tmp_path / "native.jsonl",
    )
    initial_turn = Turn(
        turn_number=3,
        user_content="canonical user",
        assistant_content="canonical assistant",
        native_event_id="message-001",
    )
    store = RawEventStore(config=_Cfg(tmp_path))
    try:
        revision_id = store.upsert_turn(
            source_agent="codex",
            session_id=session.session_id,
            turn_number=initial_turn.turn_number,
            user_content=initial_turn.user_content,
            assistant_content=initial_turn.assistant_content,
            model_tag="canonical-model",
            metadata=build_native_raw_metadata(
                _NativeCodex(),
                session,
                initial_turn,
            ),
            completeness={"visible_text": "full", "truncated": False},
        )
        current_revision_id = store.find_event_id(
            source_agent="codex",
            session_id="native-projection-repair",
            turn_number=999,
            native_event_id="message-001",
        )
        assert current_revision_id is not None
        logical_event_id = store.get_logical_event_id(current_revision_id)
        with sqlite3.connect(store.db_path) as connection:
            connection.execute(
                """
                UPDATE raw_turns
                SET turn_number=99, model_tag='tampered'
                WHERE event_id=?
                """,
                (logical_event_id,),
            )
        invalid = raw_conservation_evidence(store.db_path)

        lower_turn = Turn(
            turn_number=77,
            user_content="lower quality user",
            assistant_content="lower quality assistant",
            native_event_id="message-001",
        )
        replayed = store.upsert_turn(
            source_agent="codex",
            session_id=session.session_id,
            turn_number=lower_turn.turn_number,
            user_content=lower_turn.user_content,
            assistant_content=lower_turn.assistant_content,
            model_tag="canonical-model",
            metadata=build_native_raw_metadata(
                _NativeCodex(),
                session,
                lower_turn,
            ),
            completeness={
                "visible_text": "host_provided",
                "loss_reasons": ["synthetic_lower_quality"],
            },
            origin="capture_service",
        )

        assert replayed != revision_id
        with sqlite3.connect(store.db_path) as connection:
            row = connection.execute(
                """
                SELECT turn_number, model_tag, content_hash,
                       full_content_hash, current_revision_id
                FROM raw_turns WHERE event_id=?
                """,
                (logical_event_id,),
            ).fetchone()
            revision = connection.execute(
                """
                SELECT content_hash, full_content_hash
                FROM raw_turn_revisions WHERE revision_id=?
                """,
                (revision_id,),
            ).fetchone()
        assert row == (
            3,
            "canonical-model",
            revision[0],
            revision[1],
            revision_id,
        )
        evidence = raw_conservation_evidence(store.db_path)
        assert compare_raw_conservation(invalid, evidence) is True
        assert all(
            item["current_revision_projection_valid"] is True
            and item["effective_projection_valid"] is True
            for item in evidence["raw_turns"]["turn_bindings"]
        )
    finally:
        store.close()


def test_native_replay_refuses_unversioned_current_bytes_without_reconciliation(
    tmp_path,
):
    class _NativeCodex:
        name = "codex"

        def completeness_capabilities(self):
            return {"source_fidelity": "full"}

    session = SessionInfo(
        session_id="native-unversioned-current",
        source_path=tmp_path / "native.jsonl",
    )
    turn = Turn(
        turn_number=3,
        user_content="canonical user",
        assistant_content="canonical assistant",
        native_event_id="message-001",
    )
    store = RawEventStore(config=_Cfg(tmp_path))
    try:
        revision_id = store.upsert_turn(
            source_agent="codex",
            session_id=session.session_id,
            turn_number=turn.turn_number,
            user_content=turn.user_content,
            assistant_content=turn.assistant_content,
            metadata=build_native_raw_metadata(
                _NativeCodex(),
                session,
                turn,
            ),
            completeness={"visible_text": "full", "truncated": False},
        )
        logical_event_id = store.get_logical_event_id(revision_id)
        replacement = zlib.compress(b"unversioned current user")
        with sqlite3.connect(store.db_path) as connection:
            connection.execute(
                """
                UPDATE raw_turns
                SET user_content_blob=?, content_hash='unversioned-hash',
                    full_content_hash='unversioned-full-hash'
                WHERE event_id=?
                """,
                (replacement, logical_event_id),
            )

        with pytest.raises(
            RawEventIdentitySchemaMigrationRequired,
            match=("raw_current_revision_projection_" "reconciliation_required"),
        ):
            store.upsert_turn(
                source_agent="codex",
                session_id=session.session_id,
                turn_number=turn.turn_number,
                user_content=turn.user_content,
                assistant_content=turn.assistant_content,
                metadata=build_native_raw_metadata(
                    _NativeCodex(),
                    session,
                    turn,
                ),
                completeness={
                    "visible_text": "full",
                    "truncated": False,
                },
            )

        with sqlite3.connect(store.db_path) as connection:
            row = connection.execute(
                """
                SELECT user_content_blob, content_hash, full_content_hash,
                       current_revision_id
                FROM raw_turns WHERE event_id=?
                """,
                (logical_event_id,),
            ).fetchone()
            revision_count = connection.execute(
                """
                SELECT COUNT(*) FROM raw_turn_revisions
                WHERE logical_event_id=?
                """,
                (logical_event_id,),
            ).fetchone()[0]
        assert row == (
            replacement,
            "unversioned-hash",
            "unversioned-full-hash",
            revision_id,
        )
        assert revision_count == 1
    finally:
        store.close()


def test_nonconforming_native_contract_is_hidden_from_current_paths_but_preserved(tmp_path):
    store = RawEventStore(config=_Cfg(tmp_path))
    try:
        healthy = store.upsert_turn(
            source_agent="codex",
            session_id="contract-visibility",
            turn_number=0,
            user_content="healthy user",
            assistant_content="healthy assistant",
            metadata={"native_event_id": "healthy-native"},
        )
        quarantined = store.upsert_turn(
            source_agent="codex",
            session_id="contract-visibility",
            turn_number=1,
            user_content="quarantined user",
            assistant_content="quarantined assistant",
            metadata={"native_event_id": "quarantined-native"},
        )
        logical_event_id = store.get_turn(quarantined)["logical_event_id"]
        conn = store._pool.get_conn()  # noqa: SLF001
        ledger = NativeRawContractLedger()
        ledger.record_explicit(
            conn,
            logical_event_id=logical_event_id,
            revision_id=quarantined,
            support_manifest_hash="test-native-contract-manifest",
            contract_state="nonconforming",
            contract_errors=["cross_session_native_identity"],
            observed_at="2026-07-13T00:00:00+00:00",
        )
        ledger.refresh_effective_state(
            conn,
            logical_event_id=logical_event_id,
            observed_at="2026-07-13T00:00:00+00:00",
        )
        conn.commit()

        headers = store.list_current_headers(source_agent="codex", session_id="contract-visibility")
        assert [header["revision_id"] for header in headers] == [healthy]
        assert (
            store.find_event_id(
                source_agent="codex",
                session_id="contract-visibility",
                turn_number=1,
                native_event_id="quarantined-native",
            )
            is None
        )

        # Quarantine applies only to ordinary discovery.  The immutable
        # revision, its header, and its ledger history remain auditable.
        preserved = store.get_turn(quarantined)
        assert preserved is not None
        assert preserved["user_content"] == "quarantined user"
        assert preserved["metadata"]["support_latest_native_contract_state"] == "nonconforming"
        assert store.get_revision_header(quarantined) is not None
        assert [
            revision["revision_id"]
            for revision in store.list_revisions(
                source_agent="codex",
                session_id="contract-visibility",
                turn_number=1,
                native_event_id="quarantined-native",
            )
        ] == [quarantined]
        assert (
            len(
                store.list_native_contract_observations(
                    source_agent="codex",
                    session_id="contract-visibility",
                    turn_number=1,
                    native_event_id="quarantined-native",
                )
            )
            == 1
        )

        lifecycle = store.refresh_survival_scores(now=datetime(2026, 7, 13, 12, 0, 0), force=True)
        assert lifecycle["updated"] == 1
        conn.execute(
            "UPDATE raw_metrics SET retention_state='eligible_delete' WHERE event_id=?",
            (logical_event_id,),
        )
        conn.commit()
        assert store.purge_eligible_delete()["purged"] == 0
        assert store.get_turn(quarantined) is not None
    finally:
        store.close()


def test_malformed_latest_native_contract_fails_closed_for_current_paths(tmp_path):
    store = RawEventStore(config=_Cfg(tmp_path))
    try:
        revision_id = store.upsert_turn(
            source_agent="codex",
            session_id="malformed-contract",
            turn_number=0,
            user_content="preserved user",
            assistant_content="preserved assistant",
            metadata={"native_event_id": "malformed-native"},
        )
        logical_event_id = store.get_turn(revision_id)["logical_event_id"]
        conn = store._pool.get_conn()  # noqa: SLF001
        conn.execute(
            """
            INSERT INTO raw_native_contract_observations (
                observation_id, logical_event_id, observed_revision_id,
                support_manifest_hash, contract_state, contract_errors_json, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "rawcontract-malformed-current-observation",
                logical_event_id,
                revision_id,
                "test-native-contract-manifest",
                "conformant",
                "{not-json",
                "2026-07-13T00:00:00+00:00",
            ),
        )
        conn.commit()

        assert store.list_current_headers(session_id="malformed-contract") == []
        assert (
            store.find_event_id(
                source_agent="codex",
                session_id="malformed-contract",
                turn_number=0,
                native_event_id="malformed-native",
            )
            is None
        )
        preserved = store.get_turn(revision_id)
        assert preserved is not None
        assert preserved["user_content"] == "preserved user"
        assert preserved["metadata"]["support_native_contract_certifying"] is False
        assert preserved["metadata"]["support_latest_native_contract_state"] == (
            "nonconforming"
        )
        assert preserved["metadata"]["support_latest_native_contract_errors"] == [
            "native_contract_observation_invalid"
        ]
        assert preserved["native_contract_observation_failure"] == {
            "code": "native_contract_observation_invalid",
            "certifying": False,
        }
        assert store.get_turn(logical_event_id) is None
        assert store.get_revision_header(revision_id) is not None
        with pytest.raises(
            ValueError,
            match="native raw contract observation is invalid",
        ):
            store.list_native_contract_observations(
                source_agent="codex",
                session_id="malformed-contract",
                turn_number=0,
                native_event_id="malformed-native",
            )
    finally:
        store.close()


def test_exact_raw_readers_separate_forensic_bytes_from_automatic_admissibility(
    tmp_path,
):
    store = RawEventStore(config=_Cfg(tmp_path))
    try:
        revision_id = store.upsert_turn(
            source_agent="codex",
            session_id="forensic-reader-contract",
            turn_number=0,
            user_content="preserved forensic user",
            assistant_content="preserved forensic assistant",
        )
        logical_event_id = store.get_turn(revision_id)["logical_event_id"]
        conn = store._pool.get_conn()  # noqa: SLF001
        conn.execute(
            """
            INSERT INTO raw_native_contract_observations (
                observation_id, logical_event_id, observed_revision_id,
                support_manifest_hash, contract_state, contract_errors_json,
                observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "rawcontract-explicit-forensic-quarantine",
                logical_event_id,
                revision_id,
                "test-native-contract-manifest",
                "nonconforming",
                '["native_identity_ambiguous"]',
                "2026-07-30T00:00:00+00:00",
            ),
        )
        conn.commit()

        forensic = raw_event_reader.read_raw_revisions_forensic_readonly(
            store.db_path,
            [revision_id],
        )
        assert [turn.revision_id for turn in forensic] == [revision_id]
        assert forensic[0].user_content == "preserved forensic user"
        with pytest.raises(
            raw_event_reader.CanonicalRawReadError,
            match="not admissible",
        ):
            raw_event_reader.read_admissible_raw_revisions_readonly(
                store.db_path,
                [revision_id],
            )
    finally:
        store.close()


def test_admissible_exact_reader_rejects_stale_revision_after_later_quarantine(
    tmp_path,
):
    store = RawEventStore(config=_Cfg(tmp_path))
    try:
        first_revision = store.upsert_turn(
            source_agent="codex",
            session_id="stale-admissible-revision",
            turn_number=0,
            user_content="first user",
            assistant_content="first assistant",
        )
        logical_event_id = store.get_turn(first_revision)["logical_event_id"]
        conn = store._pool.get_conn()  # noqa: SLF001
        ledger = NativeRawContractLedger()
        ledger.record_explicit(
            conn,
            logical_event_id=logical_event_id,
            revision_id=first_revision,
            support_manifest_hash="first-manifest",
            contract_state="conformant",
            contract_errors=[],
            observed_at="2026-07-30T00:00:00+00:00",
        )
        conn.commit()
        second_revision = store.upsert_turn(
            source_agent="codex",
            session_id="stale-admissible-revision",
            turn_number=0,
            user_content="second user",
            assistant_content="second assistant",
        )
        assert second_revision != first_revision
        ledger.record_explicit(
            conn,
            logical_event_id=logical_event_id,
            revision_id=second_revision,
            support_manifest_hash="second-manifest",
            contract_state="nonconforming",
            contract_errors=["native_identity_ambiguous"],
            observed_at="2026-07-30T00:01:00+00:00",
        )
        conn.commit()
        assert ledger.latest(conn, logical_event_id)["observed_revision_id"] == (
            second_revision
        )
        with pytest.raises(
            ValueError,
            match="does not bind requested revision",
        ):
            ledger.latest_for_revision(
                conn,
                logical_event_id=logical_event_id,
                revision_id=first_revision,
            )

        assert [
            turn.revision_id
            for turn in raw_event_reader.read_raw_revisions_forensic_readonly(
                store.db_path,
                [first_revision],
            )
        ] == [first_revision]
        with pytest.raises(
            raw_event_reader.CanonicalRawReadError,
            match="not admissible",
        ):
            raw_event_reader.read_admissible_raw_revisions_readonly(
                store.db_path,
                [first_revision],
            )
    finally:
        store.close()


def test_typed_current_reader_and_count_fail_closed_on_dangling_pointer(tmp_path):
    store = RawEventStore(config=_Cfg(tmp_path))
    try:
        revision_id = store.upsert_turn(
            source_agent="codex",
            session_id="typed-current-pointer",
            turn_number=0,
            user_content="current user",
            assistant_content="current assistant",
        )
        logical_event_id = store.get_turn(revision_id)["logical_event_id"]
        conn = store._pool.get_conn()  # noqa: SLF001
        conn.execute(
            "UPDATE raw_turns SET current_revision_id=? WHERE event_id=?",
            ("rawrev-" + ("f" * 40), logical_event_id),
        )
        conn.commit()

        with pytest.raises(
            raw_event_reader.CanonicalRawReadError,
            match="current revision",
        ):
            raw_event_reader.count_current_raw_turns_readonly(store.db_path)
        with pytest.raises(
            raw_event_reader.CanonicalRawReadError,
            match="current revision",
        ):
            list(raw_event_reader.iter_current_raw_turns_readonly(store.db_path))
    finally:
        store.close()


class _NoRawBodyFetchallCursor:
    def __init__(self, cursor: sqlite3.Cursor, sql: str):
        self._cursor = cursor
        self._sql = " ".join(sql.lower().split())

    @property
    def description(self):
        return self._cursor.description

    def __iter__(self):
        return iter(self._cursor)

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        if self._sql.startswith("select * from raw_"):
            raise AssertionError("raw body queries must be consumed incrementally")
        return self._cursor.fetchall()


class _NoRawBodyFetchallConnection:
    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection

    def execute(self, sql, parameters=()):
        return _NoRawBodyFetchallCursor(
            self._connection.execute(sql, parameters),
            str(sql),
        )

    def rollback(self):
        return self._connection.rollback()


@contextmanager
def _readonly_without_raw_body_fetchall(path: Path):
    with connect_readonly_sqlite(path) as connection:
        yield _NoRawBodyFetchallConnection(connection)


def test_current_projection_and_conservation_stream_raw_body_queries(
    tmp_path,
    monkeypatch,
):
    store = RawEventStore(config=_Cfg(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="streaming-recovery",
            turn_number=0,
            user_content="streamed user",
            assistant_content="streamed assistant",
        )
    finally:
        store.close()

    monkeypatch.setattr(
        raw_current_projection_reconciliation,
        "connect_readonly_sqlite",
        _readonly_without_raw_body_fetchall,
    )
    monkeypatch.setattr(
        native_raw_recovery_evidence,
        "connect_readonly_sqlite",
        _readonly_without_raw_body_fetchall,
    )

    plan = raw_current_projection_reconciliation.plan_current_projection_reconciliation(
        store.db_path
    )
    evidence = native_raw_recovery_evidence.raw_conservation_evidence(store.db_path)

    assert plan["ok"] is True
    assert evidence["raw_turns"]["row_count"] == 1
    assert evidence["raw_turn_revisions"]["row_count"] == 1


def test_conservation_rejects_cross_owner_supersedes_link(tmp_path):
    store = RawEventStore(config=_Cfg(tmp_path))
    try:
        first_revision = store.upsert_turn(
            source_agent="codex",
            session_id="supersedes-owner-a",
            turn_number=0,
            user_content="first user",
            assistant_content="first assistant",
        )
        foreign_revision = store.upsert_turn(
            source_agent="codex",
            session_id="supersedes-owner-b",
            turn_number=0,
            user_content="foreign user",
            assistant_content="foreign assistant",
        )
        conn = store._pool.get_conn()  # noqa: SLF001
        conn.execute(
            """
            UPDATE raw_turn_revisions
            SET supersedes_revision_id=?
            WHERE revision_id=?
            """,
            (foreign_revision, first_revision),
        )
        conn.commit()

        with pytest.raises(
            NativeRawRecoveryEvidenceError,
            match="raw_conservation_evidence_failed",
        ):
            raw_conservation_evidence(store.db_path)
    finally:
        store.close()


def test_runtime_projection_repair_rejects_malformed_contract_observation(
    tmp_path,
):
    store = RawEventStore(config=_Cfg(tmp_path))
    try:
        revision_id = store.upsert_turn(
            source_agent="codex",
            session_id="runtime-projection-contract",
            turn_number=0,
            user_content="runtime user",
            assistant_content="runtime assistant",
        )
        logical_event_id = store.get_turn(revision_id)["logical_event_id"]
        conn = store._pool.get_conn()  # noqa: SLF001
        conn.execute(
            """
            INSERT INTO raw_native_contract_observations (
                observation_id, logical_event_id, observed_revision_id,
                support_manifest_hash, contract_state, contract_errors_json,
                observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "rawcontract-runtime-malformed",
                logical_event_id,
                revision_id,
                "manifest-hash",
                "conformant",
                '["contradictory-error"]',
                "2026-07-30T00:00:00+00:00",
            ),
        )
        conn.execute(
            "UPDATE raw_turns SET turn_number=999 WHERE event_id=?",
            (logical_event_id,),
        )
        conn.commit()

        with pytest.raises(
            RawEventIdentitySchemaMigrationRequired,
            match="raw_current_revision_projection_reconciliation_required",
        ):
            store._repair_current_projection_if_invalid(  # noqa: SLF001
                conn,
                logical_event_id=logical_event_id,
                revision_id=revision_id,
            )
    finally:
        store.close()


def test_parser_artifact_offset_is_used_when_native_event_id_is_missing(tmp_path):
    store = RawEventStore(config=_Cfg(tmp_path))
    try:
        main = store.upsert_turn(
            source_agent="codex",
            session_id="fallback-artifacts",
            turn_number=0,
            user_content="same visible content",
            assistant_content="same visible answer",
            metadata={"source_artifact_id": "main-context.jsonl"},
        )
        child = store.upsert_turn(
            source_agent="codex",
            session_id="fallback-artifacts",
            turn_number=0,
            user_content="same visible content",
            assistant_content="same visible answer",
            metadata={"source_artifact_id": "subagent-context.jsonl"},
        )

        assert main != child
        assert store.get_turn(main)["metadata"]["logical_event_identity_kind"] == (
            "parser_artifact_offset"
        )
        assert store.get_turn(child)["metadata"]["logical_event_identity_kind"] == (
            "parser_artifact_offset"
        )
    finally:
        store.close()


def test_weekly_survival_refresh_marks_old_unused_raw_as_eligible_delete(tmp_path):
    store = RawEventStore(config=_Cfg(tmp_path))
    try:
        now = datetime(2026, 6, 28, 10, 0, 0)
        old = now - timedelta(days=45)
        event_id = store.upsert_turn(
            source_agent="kimi",
            session_id="old-unused",
            turn_number=0,
            user_content="old",
            assistant_content="unused",
            timestamp=old.isoformat(),
            completeness={"visible_text": "full", "truncated": False},
        )

        summary = store.refresh_survival_scores(now=now, force=True)
        metrics = store.get_metrics(event_id)

        assert summary["updated"] == 1
        assert summary["eligible_delete"] == 1
        assert metrics is not None
        assert metrics["retention_state"] == "eligible_delete"
        assert metrics["hit_count"] == 0

    finally:
        store.close()


def test_survival_refresh_accepts_timezone_aware_timestamps(tmp_path):
    store = RawEventStore(config=_Cfg(tmp_path))
    try:
        event_id = store.upsert_turn(
            source_agent="codex",
            session_id="tz-aware",
            turn_number=0,
            user_content="hello",
            assistant_content="world",
            timestamp="2026-06-20T01:05:00+00:00",
        )

        summary = store.refresh_survival_scores(
            now=datetime(2026, 6, 28, 10, 0, 0),
            force=True,
        )

        assert summary["updated"] == 1
        assert store.get_metrics(event_id)["last_survival_recalc_at"].startswith("2026-06-28")
    finally:
        store.close()


def test_survival_refresh_uses_hits_not_views_for_retention(tmp_path):
    store = RawEventStore(config=_Cfg(tmp_path))
    try:
        now = datetime(2026, 6, 28, 10, 0, 0)
        old = now - timedelta(days=45)
        viewed = store.upsert_turn(
            source_agent="codex",
            session_id="old-viewed",
            turn_number=0,
            user_content="old",
            assistant_content="viewed",
            timestamp=old.isoformat(),
        )
        hit = store.upsert_turn(
            source_agent="codex",
            session_id="old-hit",
            turn_number=0,
            user_content="old",
            assistant_content="hit",
            timestamp=old.isoformat(),
        )
        store.record_access(viewed, "view", consumer="obsidian")
        store.record_access(hit, "hit", consumer="search")

        store.refresh_survival_scores(now=now, force=True)

        viewed_metrics = store.get_metrics(viewed)
        hit_metrics = store.get_metrics(hit)
        assert viewed_metrics["retention_state"] == "eligible_delete"
        assert hit_metrics["retention_state"] == "active"
        assert viewed_metrics["hit_count"] == 0
        assert hit_metrics["hit_count"] == 1

    finally:
        store.close()


def test_purge_eligible_delete_physically_removes_only_expired_unused_raw(tmp_path):
    store = RawEventStore(config=_Cfg(tmp_path))
    try:
        now = datetime(2026, 6, 28, 10, 0, 0)
        old = now - timedelta(days=45)
        unused = store.upsert_turn(
            source_agent="codex",
            session_id="old-unused",
            turn_number=0,
            user_content="old",
            assistant_content="unused",
            timestamp=old.isoformat(),
        )
        hit = store.upsert_turn(
            source_agent="codex",
            session_id="old-hit",
            turn_number=1,
            user_content="old",
            assistant_content="hit",
            timestamp=old.isoformat(),
        )
        store.record_access(unused, "view", consumer="obsidian")
        store.record_access(hit, "hit", consumer="search")
        store.refresh_survival_scores(now=now, force=True)

        summary = store.purge_eligible_delete()

        assert summary["purged"] == 1
        assert summary["raw_turns_deleted"] == 1
        assert summary["raw_metrics_deleted"] == 1
        assert summary["raw_access_logs_deleted"] == 1
        assert store.get_turn(unused) is None
        assert store.get_metrics(unused) is None
        assert store.get_turn(hit) is not None
        assert store.get_metrics(hit)["retention_state"] == "active"
    finally:
        store.close()


def test_purge_eligible_delete_removes_native_contract_observations(tmp_path):
    class _NativeCodex:
        name = "codex"

        def completeness_capabilities(self):
            return {"source_fidelity": "full"}

    store = RawEventStore(config=_Cfg(tmp_path))
    try:
        session = SessionInfo(session_id="native-purge", source_path=tmp_path / "native.jsonl")
        turn = Turn(turn_number=0, user_content="user", assistant_content="assistant")
        event_id = store.upsert_turn(
            source_agent="codex",
            session_id=session.session_id,
            turn_number=turn.turn_number,
            user_content=turn.user_content,
            assistant_content=turn.assistant_content,
            metadata=build_native_raw_metadata(_NativeCodex(), session, turn),
            completeness={"visible_text": "full", "truncated": False},
        )
        logical_event_id = store.get_turn(event_id)["logical_event_id"]
        conn = store._pool.get_conn()  # noqa: SLF001
        conn.execute(
            "UPDATE raw_metrics SET retention_state='eligible_delete' WHERE event_id=?",
            (logical_event_id,),
        )
        conn.commit()

        summary = store.purge_eligible_delete()
        orphan_count = conn.execute(
            "SELECT COUNT(*) FROM raw_native_contract_observations"
        ).fetchone()[0]

        assert summary["purged"] == 1
        assert summary["raw_native_contract_observations_deleted"] == 1
        assert orphan_count == 0
    finally:
        store.close()
