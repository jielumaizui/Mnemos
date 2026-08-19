from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.sync_framework.raw_event_store import (
    RawEventIdentitySchemaMigrationRequired,
    RawEventStore,
)
from core.sync_framework.raw_session_identity_reconciliation import (
    RawSessionIdentityReconciliationError,
    build_receipt_material,
    incompatible_event_fingerprint,
    initialize_schema,
    record_receipt,
    receipt_allows_current_fingerprint,
    validate_schema,
)


class _Config:
    def __init__(self, database_dir: Path) -> None:
        self.database_dir = database_dir

    @staticmethod
    def get(_key: str, default=None):
        return default


def _identity_metadata() -> dict[str, object]:
    return {
        "identity_contract_version": "synthetic-artifact-v2",
        "identity_reconciliation_required": True,
        "legacy_canonical_session_ids": ["legacy-session"],
        "source_artifact_id": "artifact-current",
    }


def _seed_legacy(
    store: RawEventStore,
    turn_number: int = 0,
    *,
    user_content: str | None = None,
) -> None:
    store.upsert_turn(
        source_agent="openclaw",
        session_id="legacy-session",
        turn_number=turn_number,
        user_content=user_content or f"legacy-{turn_number}",
        assistant_content="payload",
    )


def _write_current(store: RawEventStore, turn_number: int = 0) -> str:
    return store.upsert_turn(
        source_agent="openclaw",
        session_id="current-session",
        turn_number=turn_number,
        user_content=f"current-{turn_number}",
        assistant_content="payload",
        metadata=_identity_metadata(),
    )


def _record_identity_receipt(
    conn: sqlite3.Connection,
    *,
    plan_character: str,
) -> None:
    material = build_receipt_material(
        conn,
        source_agent="openclaw",
        identity_contract_version="synthetic-artifact-v2",
        canonical_session_id="current-session",
        legacy_session_ids=["legacy-session"],
        source_artifact_id="artifact-current",
    )
    assert material is not None
    initialize_schema(conn)
    record_receipt(
        conn,
        material=material,
        plan_hash="sha256:" + (plan_character * 64),
        reconciled_at="2026-07-26T00:00:00+00:00",
    )


def test_exact_append_only_receipt_allows_identity_upgrade_and_rejects_drift(
    tmp_path: Path,
) -> None:
    raw_path = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=raw_path, config=_Config(tmp_path))
    _seed_legacy(store)
    with pytest.raises(
        RawEventIdentitySchemaMigrationRequired,
        match="source_session_identity_reconciliation_required",
    ):
        _write_current(store)
    store.close()

    with sqlite3.connect(raw_path) as conn:
        _record_identity_receipt(conn, plan_character="a")
        assert receipt_allows_current_fingerprint(
            conn,
            source_agent="openclaw",
            identity_contract_version="synthetic-artifact-v2",
            canonical_session_id="current-session",
            legacy_session_ids=["legacy-session"],
            source_artifact_id="artifact-current",
        )

    store = RawEventStore(db_path=raw_path, config=_Config(tmp_path))
    assert _write_current(store)
    _seed_legacy(store, user_content="changed-current-content")
    with pytest.raises(
        RawEventIdentitySchemaMigrationRequired,
        match="source_session_identity_reconciliation_required",
    ):
        _write_current(store)
    store.close()


def test_receipt_rejects_noncontent_raw_row_drift(tmp_path: Path) -> None:
    raw_path = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=raw_path, config=_Config(tmp_path))
    _seed_legacy(store)
    store.close()

    with sqlite3.connect(raw_path) as conn:
        _record_identity_receipt(conn, plan_character="d")
        conn.execute(
            "UPDATE raw_turns SET model_tag='preimage-drift' "
            "WHERE source_agent='openclaw' AND session_id='legacy-session'"
        )
        assert not receipt_allows_current_fingerprint(
            conn,
            source_agent="openclaw",
            identity_contract_version="synthetic-artifact-v2",
            canonical_session_id="current-session",
            legacy_session_ids=["legacy-session"],
            source_artifact_id="artifact-current",
        )


def test_receipt_rejects_noncurrent_revision_row_drift(
    tmp_path: Path,
) -> None:
    raw_path = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=raw_path, config=_Config(tmp_path))
    _seed_legacy(store)
    _seed_legacy(store, user_content="second-revision")
    store.close()

    with sqlite3.connect(raw_path) as conn:
        _record_identity_receipt(conn, plan_character="e")
        conn.execute(
            """
            UPDATE raw_turn_revisions
            SET created_at='2026-07-26T00:00:01+00:00'
            WHERE revision_id=(
                SELECT revision_id
                FROM raw_turn_revisions
                ORDER BY revision_number, revision_id
                LIMIT 1
            )
            """
        )
        assert not receipt_allows_current_fingerprint(
            conn,
            source_agent="openclaw",
            identity_contract_version="synthetic-artifact-v2",
            canonical_session_id="current-session",
            legacy_session_ids=["legacy-session"],
            source_artifact_id="artifact-current",
        )


@pytest.mark.parametrize(
    "corruption",
    ("null_current", "dangling_current", "zero_revision_set"),
)
def test_receipt_material_rejects_invalid_current_revision(
    tmp_path: Path,
    corruption: str,
) -> None:
    raw_path = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=raw_path, config=_Config(tmp_path))
    _seed_legacy(store)
    store.close()

    with sqlite3.connect(raw_path) as conn:
        if corruption == "null_current":
            conn.execute(
                "UPDATE raw_turns SET current_revision_id=NULL "
                "WHERE source_agent='openclaw'"
            )
        elif corruption == "dangling_current":
            conn.execute(
                "UPDATE raw_turns SET current_revision_id='rawrev-missing' "
                "WHERE source_agent='openclaw'"
            )
        else:
            conn.execute("DELETE FROM raw_turn_revisions")
            conn.execute(
                "UPDATE raw_turns SET current_revision_id=NULL "
                "WHERE source_agent='openclaw'"
            )
        with pytest.raises(
            RawSessionIdentityReconciliationError,
            match="current_revision_invalid",
        ):
            build_receipt_material(
                conn,
                source_agent="openclaw",
                identity_contract_version="synthetic-artifact-v2",
                canonical_session_id="current-session",
                legacy_session_ids=["legacy-session"],
                source_artifact_id="artifact-current",
            )


def test_fingerprint_streams_history_rows_and_blob_bytes(
    tmp_path: Path,
) -> None:
    raw_path = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=raw_path, config=_Config(tmp_path))
    for turn_number in range(3):
        _seed_legacy(
            store,
            turn_number=turn_number,
            user_content=f"{turn_number}-" + ("payload" * 50_000),
        )
    store.close()

    class _BlobProxy:
        def __init__(self, blob, reads: list[int]) -> None:
            self._blob = blob
            self._reads = reads

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self._blob.close()

        def read(self, size: int):
            self._reads.append(size)
            assert size <= 1024 * 1024
            return self._blob.read(size)

    class _CursorProxy:
        def __init__(self, cursor, *, history_query: bool) -> None:
            self._cursor = cursor
            self._history_query = history_query

        def __iter__(self):
            return iter(self._cursor)

        def fetchall(self):
            if self._history_query:
                raise AssertionError("history query must not call fetchall")
            return self._cursor.fetchall()

        def fetchone(self):
            return self._cursor.fetchone()

    class _ConnectionProxy:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self._conn = conn
            self.history_queries = 0
            self.blob_reads: list[int] = []

        def execute(self, sql: str, parameters=()):
            history_query = "FROM raw_turns AS t" in sql
            if history_query:
                self.history_queries += 1
            return _CursorProxy(
                self._conn.execute(sql, parameters),
                history_query=history_query,
            )

        def blobopen(self, *args, **kwargs):
            return _BlobProxy(
                self._conn.blobopen(*args, **kwargs),
                self.blob_reads,
            )

    with sqlite3.connect(raw_path) as conn:
        proxy = _ConnectionProxy(conn)
        fingerprint = incompatible_event_fingerprint(
            proxy,  # type: ignore[arg-type]
            source_agent="openclaw",
            session_ids=["legacy-session"],
            identity_contract_version="synthetic-artifact-v2",
        )
        assert fingerprint["historical_event_count"] == 3
        assert proxy.history_queries == 1
        assert proxy.blob_reads
        assert max(proxy.blob_reads) == 1024 * 1024


def test_reconciliation_ledger_rejects_update_and_delete(tmp_path: Path) -> None:
    raw_path = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=raw_path, config=_Config(tmp_path))
    _seed_legacy(store)
    store.close()
    with sqlite3.connect(raw_path) as conn:
        material = build_receipt_material(
            conn,
            source_agent="openclaw",
            identity_contract_version="synthetic-artifact-v2",
            canonical_session_id="current-session",
            legacy_session_ids=["legacy-session"],
            source_artifact_id="artifact-current",
        )
        assert material is not None
        initialize_schema(conn)
        receipt_id = record_receipt(
            conn,
            material=material,
            plan_hash="sha256:" + ("b" * 64),
        )
        second_receipt_id = record_receipt(
            conn,
            material=material,
            plan_hash="sha256:" + ("c" * 64),
        )
        assert second_receipt_id != receipt_id
        assert (
            conn.execute(
                "SELECT count(*) FROM raw_session_identity_reconciliations"
            ).fetchone()[0]
            == 2
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="raw_session_identity_reconciliation_append_only",
        ):
            conn.execute(
                "UPDATE raw_session_identity_reconciliations "
                "SET source_artifact_id='tampered' WHERE receipt_id=?",
                (receipt_id,),
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="raw_session_identity_reconciliation_append_only",
        ):
            conn.execute(
                "DELETE FROM raw_session_identity_reconciliations "
                "WHERE receipt_id=?",
                (receipt_id,),
            )


def test_receipt_material_requires_complete_context(tmp_path: Path) -> None:
    raw_path = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=raw_path, config=_Config(tmp_path))
    _seed_legacy(store)
    store.close()
    with sqlite3.connect(raw_path) as conn:
        with pytest.raises(
            RawSessionIdentityReconciliationError,
            match="context_incomplete",
        ):
            build_receipt_material(
                conn,
                source_agent="openclaw",
                identity_contract_version="synthetic-artifact-v2",
                canonical_session_id="current-session",
                legacy_session_ids=["legacy-session"],
                source_artifact_id="",
            )


def test_schema_validator_rejects_same_named_noop_trigger(tmp_path: Path) -> None:
    raw_path = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=raw_path, config=_Config(tmp_path))
    store.close()
    with sqlite3.connect(raw_path) as conn:
        initialize_schema(conn)
        conn.execute(
            "DROP TRIGGER raw_session_identity_reconciliations_no_update"
        )
        conn.execute(
            """
            CREATE TRIGGER raw_session_identity_reconciliations_no_update
            BEFORE UPDATE ON raw_session_identity_reconciliations
            BEGIN
                SELECT 1;
            END
            """
        )
        with pytest.raises(
            RawSessionIdentityReconciliationError,
            match="trigger_mismatch",
        ):
            validate_schema(conn)
