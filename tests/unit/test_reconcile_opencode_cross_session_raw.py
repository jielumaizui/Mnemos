"""Tests for the content-free OpenCode cross-session Raw reconciler."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from core.sync_framework.agent_source import AgentSource, SessionInfo, Turn
from core.sync_framework.raw_event_store import RawEventStore
from scripts import reconcile_opencode_cross_session_raw as reconciler


class _Config:
    def __init__(self, database_dir: Path):
        self.database_dir = database_dir
        self.data_dir = database_dir

    def get(self, _key, default=None):
        return default


class _FakeOpenCodeSource(AgentSource):
    name = "opencode"
    model_tag = "opencode"

    def __init__(self, pairs: dict[str, list[str]], source_path: Path):
        self._pairs = pairs
        self._source_path = source_path
        self.parse_turns_called = False

    def discover_sessions(self) -> list[SessionInfo]:
        return [
            SessionInfo(
                session_id=session_id,
                canonical_session_id=session_id,
                source_path=self._source_path,
                source_kind="sqlite",
                metadata={"native_session_id": session_id},
            )
            for session_id in sorted(self._pairs)
        ]

    def parse_turns(self, _session_path: Path) -> list[Turn]:
        self.parse_turns_called = True
        raise AssertionError("the reconciler must use parse_session")

    def parse_session(self, session_info: SessionInfo) -> list[Turn]:
        return [
            Turn(
                turn_number=index,
                user_content=f"source-user-{index}",
                assistant_content=f"source-assistant-{index}",
                metadata={"native_event_id": native_event_id},
            )
            for index, native_event_id in enumerate(self._pairs[session_info.session_id])
        ]


def _store(tmp_path: Path) -> RawEventStore:
    return RawEventStore(db_path=tmp_path / "raw_events.db", config=_Config(tmp_path))


def _upsert_native(
    store: RawEventStore,
    *,
    session_id: str,
    turn_number: int,
    native_event_id: str,
) -> str:
    return store.upsert_turn(
        source_agent="opencode",
        session_id=session_id,
        turn_number=turn_number,
        user_content=f"raw-user-{session_id}-{turn_number}",
        assistant_content=f"raw-assistant-{session_id}-{turn_number}",
        metadata={"native_event_id": native_event_id},
    )


def _seed_incident(store: RawEventStore) -> dict[str, str]:
    exact_a = _upsert_native(
        store,
        session_id="session-a",
        turn_number=0,
        native_event_id="opencode:message:a",
    )
    exact_b = _upsert_native(
        store,
        session_id="session-b",
        turn_number=0,
        native_event_id="opencode:message:b",
    )
    cross_session = _upsert_native(
        store,
        session_id="session-a",
        turn_number=1,
        native_event_id="opencode:message:b",
    )
    unobserved = _upsert_native(
        store,
        session_id="session-b",
        turn_number=1,
        native_event_id="opencode:message:historical-only",
    )
    return {
        "exact_a": exact_a,
        "exact_b": exact_b,
        "cross_session": cross_session,
        "unobserved": unobserved,
    }


def _source(tmp_path: Path) -> _FakeOpenCodeSource:
    return _FakeOpenCodeSource(
        {
            "session-a": ["opencode:message:a"],
            "session-b": ["opencode:message:b"],
        },
        tmp_path / "opencode.db",
    )


def test_inspect_classifies_exact_cross_session_and_unobserved_without_bodies(tmp_path: Path):
    store = _store(tmp_path)
    try:
        _seed_incident(store)
    finally:
        store.close()

    source = _source(tmp_path)
    report = reconciler.inspect_reconciliation(
        tmp_path / "raw_events.db", source=source
    )

    assert report["ok"] is True, report
    assert report["classification"] == {
        "cross_session_native_identity": 1,
        "current_parse_empty_sessions": 0,
        "exact_pair": 2,
        "expected_pairs_missing_from_raw": 0,
        "raw_native_pair_duplicates": 0,
        "unobserved_native_identity": 1,
        "unobserved_native_identity_in_apply_set": 0,
    }
    assert report["candidate_count"] == 1
    assert report["needs_apply_count"] == 1
    assert report["normal_visible_cross_session_count"] == 1
    assert len(report["candidate_receipt_hash"]) == 64
    assert source.parse_turns_called is False
    rendered = json.dumps(report, sort_keys=True)
    assert "raw-user" not in rendered
    assert "source-user" not in rendered


def test_apply_quarantines_only_proven_cross_session_rows_and_preserves_evidence(
    tmp_path: Path,
):
    store = _store(tmp_path)
    try:
        revisions = _seed_incident(store)
    finally:
        store.close()

    backup_dir = tmp_path / "backups"
    result = reconciler.apply_reconciliation(
        tmp_path / "raw_events.db",
        backup_dir=backup_dir,
        source=_source(tmp_path),
    )

    assert result["ok"] is True, result
    assert result["effect"]["observations_appended"] == 1
    assert result["after"]["normal_visible_cross_session_count"] == 0
    backup_path = Path(result["backup_path"])
    assert backup_path.is_file()
    with sqlite3.connect(backup_path) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
    assert receipt["status"] == "committed"
    assert receipt["candidate_receipt_hash"] == result["before"]["candidate_receipt_hash"]
    assert "raw-user" not in json.dumps(receipt, sort_keys=True)

    store = _store(tmp_path)
    try:
        headers = store.list_current_headers(source_agent="opencode")
        assert {header["revision_id"] for header in headers} == {
            revisions["exact_a"],
            revisions["exact_b"],
            revisions["unobserved"],
        }
        preserved = store.get_turn(revisions["cross_session"])
        assert preserved is not None
        assert preserved["metadata"]["support_latest_native_contract_state"] == "nonconforming"
        cross_observations = store.list_native_contract_observations(
            source_agent="opencode",
            session_id="session-a",
            turn_number=1,
            native_event_id="opencode:message:b",
        )
        assert cross_observations[-1]["contract_errors"] == [
            "cross_session_native_identity",
            "opencode_cross_session_reconciliation_v1",
        ]
        assert store.list_native_contract_observations(
            source_agent="opencode",
            session_id="session-b",
            turn_number=1,
            native_event_id="opencode:message:historical-only",
        ) == []
        conn = store._pool.get_conn()  # noqa: SLF001
        assert conn.execute("SELECT COUNT(*) FROM raw_turns").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM raw_turn_revisions").fetchone()[0] == 4
    finally:
        store.close()

    second = reconciler.apply_reconciliation(
        tmp_path / "raw_events.db",
        backup_dir=backup_dir,
        source=_source(tmp_path),
    )
    assert second["ok"] is True, second
    assert second["effect"]["observations_appended"] == 0
    assert (
        second["before"]["candidate_receipt_hash"]
        == result["before"]["candidate_receipt_hash"]
    )


def test_apply_refuses_missing_current_source_pairs_without_mutating_raw(tmp_path: Path):
    store = _store(tmp_path)
    try:
        _upsert_native(
            store,
            session_id="session-a",
            turn_number=0,
            native_event_id="opencode:message:a",
        )
    finally:
        store.close()

    source = _source(tmp_path)
    report = reconciler.inspect_reconciliation(
        tmp_path / "raw_events.db", source=source
    )
    assert report["ok"] is False
    assert report["classification"]["expected_pairs_missing_from_raw"] == 1

    with pytest.raises(reconciler.OpenCodeCrossSessionReconciliationError):
        reconciler.apply_reconciliation(
            tmp_path / "raw_events.db",
            backup_dir=tmp_path / "backups",
            source=source,
        )
    with sqlite3.connect(tmp_path / "raw_events.db") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM raw_native_contract_observations"
        ).fetchone()[0] == 0


def test_candidate_receipt_hash_ignores_unrelated_exact_current_revisions(tmp_path: Path):
    store = _store(tmp_path)
    try:
        _seed_incident(store)
    finally:
        store.close()

    before = reconciler.inspect_reconciliation(
        tmp_path / "raw_events.db", source=_source(tmp_path)
    )
    store = _store(tmp_path)
    try:
        store.upsert_turn(
            source_agent="opencode",
            session_id="session-a",
            turn_number=0,
            user_content="updated exact user",
            assistant_content="updated exact assistant",
            metadata={"native_event_id": "opencode:message:a"},
        )
    finally:
        store.close()
    after = reconciler.inspect_reconciliation(
        tmp_path / "raw_events.db", source=_source(tmp_path)
    )

    assert after["raw"]["identity_hash"] != before["raw"]["identity_hash"]
    assert after["candidate_receipt_hash"] == before["candidate_receipt_hash"]


def test_apply_rolls_back_if_a_contract_observation_cannot_be_written(
    tmp_path: Path,
    monkeypatch,
):
    store = _store(tmp_path)
    try:
        _seed_incident(store)
    finally:
        store.close()

    def _fail_record(*_args, **_kwargs):
        raise sqlite3.OperationalError("injected write failure")

    monkeypatch.setattr(
        reconciler.NativeRawContractLedger,
        "record_explicit",
        _fail_record,
    )
    backup_dir = tmp_path / "backups"
    with pytest.raises(
        reconciler.OpenCodeCrossSessionReconciliationError,
        match="apply_transaction_failed",
    ):
        reconciler.apply_reconciliation(
            tmp_path / "raw_events.db",
            backup_dir=backup_dir,
            source=_source(tmp_path),
        )

    with sqlite3.connect(tmp_path / "raw_events.db") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM raw_native_contract_observations"
        ).fetchone()[0] == 0
    receipt = json.loads(next(backup_dir.glob("*.json")).read_text(encoding="utf-8"))
    assert receipt["status"] == "rolled_back"


def test_cli_apply_requires_a_backup_directory(tmp_path: Path, monkeypatch, capsys):
    store = _store(tmp_path)
    try:
        _seed_incident(store)
    finally:
        store.close()
    monkeypatch.setattr(reconciler, "OpenCodeSource", lambda: _source(tmp_path))

    exit_code = reconciler.main(
        ["--db", str(tmp_path / "raw_events.db"), "--apply", "--json"]
    )

    assert exit_code == 1
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert output["error_code"] == "backup_directory_required"
