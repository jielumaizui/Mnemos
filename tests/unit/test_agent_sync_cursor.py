# -*- coding: utf-8 -*-
"""Durability and denominator-reconciliation tests for AgentSyncCursorStore."""

from __future__ import annotations

import stat
import sqlite3
import hashlib
import json
from pathlib import Path

import pytest

import daemon.agent_sync_cursor as cursor_module
from daemon.agent_sync_cursor import (
    CURSOR_SCHEMA_VERSION,
    LEGACY_CURSOR_SCHEMA_VERSION,
    AgentSyncCursorError,
    AgentSyncCursorStore,
    migrate_historical_cursor_schema,
)

_EVIDENCE_HASH = "sha256:" + ("a" * 64)


def _fingerprints(numbers) -> dict[int, str]:
    return {int(number): f"{int(number):064x}" for number in numbers}


def _row_set_hash(rows: list[tuple[object, ...]]) -> str:
    rendered = json.dumps(
        [list(row) for row in sorted(rows)],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def test_new_cursor_schema_late_failure_rolls_back_every_user_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LateSchemaAbort(BaseException):
        pass

    original = cursor_module._initialize_cursor_schema_v5

    def fail_after_complete_schema(connection: sqlite3.Connection) -> None:
        original(connection)
        raise LateSchemaAbort("sentinel late cursor schema failure")

    monkeypatch.setattr(
        cursor_module,
        "_initialize_cursor_schema_v5",
        fail_after_complete_schema,
    )

    with pytest.raises(LateSchemaAbort, match="sentinel late cursor schema failure"):
        AgentSyncCursorStore(tmp_path)._connect()  # noqa: SLF001

    with sqlite3.connect(tmp_path / "agent_sync_cursors.db") as connection:
        user_objects = connection.execute("""
            SELECT type, name
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """).fetchall()
    assert user_objects == []


def test_cursor_security_check_does_not_skip_uninspectable_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = AgentSyncCursorStore(tmp_path)
    target = store.path
    original_lstat = Path.lstat

    def denied(path: Path, *args: object, **kwargs: object):
        if path == target:
            raise PermissionError("sentinel")
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", denied)

    with pytest.raises(
        AgentSyncCursorError,
        match="cannot secure agent sync cursor ledger",
    ):
        store.begin_source_denominator("codex", ["canonical-a"])


@pytest.mark.parametrize(
    ("table_name", "column_name"),
    [
        ("source_capture_expected_turns", "turn_fingerprint"),
        ("source_capture_raw_receipts", "turn_fingerprint"),
    ],
)
def test_cursor_v5_rejects_either_missing_turn_fingerprint_column(
    tmp_path: Path,
    table_name: str,
    column_name: str,
) -> None:
    store = AgentSyncCursorStore(tmp_path)
    store.begin_source_denominator("codex", [])
    with sqlite3.connect(store.path) as connection:
        connection.execute(f'ALTER TABLE "{table_name}" DROP COLUMN "{column_name}"')

    with pytest.raises(
        AgentSyncCursorError,
        match="invalid agent sync capture evidence schema",
    ):
        store.begin_source_denominator("codex", [])


def test_denominator_progress_survives_restart_and_resets_on_roster_change(tmp_path: Path):
    store = AgentSyncCursorStore(tmp_path)
    initial = store.begin_source_denominator("codex", ["canonical-a", "canonical-b"])
    assert initial.complete is False
    assert initial.session_count == 2

    first = store.record_denominator_session(
        "codex",
        "canonical-a",
        turn_count=101,
        turn_fingerprints=_fingerprints(range(101)),
        artifact_evidence_hash=_EVIDENCE_HASH,
    )
    assert first.complete is False
    assert first.observed_session_count == 1
    assert first.observed_turn_count == 101

    restarted = AgentSyncCursorStore(tmp_path)
    complete = restarted.record_denominator_session(
        "codex",
        "canonical-b",
        turn_count=4,
        turn_fingerprints=_fingerprints(range(4)),
        artifact_evidence_hash=_EVIDENCE_HASH,
    )
    assert complete.complete is True
    assert complete.observed_session_count == 2
    assert complete.observed_turn_count == 105
    assert complete.completed_at
    assert stat.S_IMODE(restarted.path.stat().st_mode) == 0o600

    unchanged = restarted.begin_source_denominator("codex", ["canonical-b", "canonical-a"])
    assert unchanged.complete is True
    assert unchanged.observed_turn_count == 105

    restarted.advance_session_raw_cursor("codex", "canonical-a", next_turn_number=101)
    changed = restarted.begin_source_denominator("codex", ["canonical-a", "canonical-c"])
    assert changed.complete is False
    assert changed.observed_session_count == 0
    assert changed.observed_turn_count == 0
    assert restarted.get_session_raw_cursor("codex", "canonical-a").next_turn_number is None


def test_session_raw_cursor_is_monotonic_after_restart(tmp_path: Path):
    store = AgentSyncCursorStore(tmp_path)
    advanced = store.advance_session_raw_cursor("codex", "canonical-session", next_turn_number=100)
    assert advanced.next_turn_number == 100

    restarted = AgentSyncCursorStore(tmp_path)
    stale = restarted.advance_session_raw_cursor("codex", "canonical-session", next_turn_number=20)
    assert stale.next_turn_number == 100
    next_cursor = restarted.advance_session_raw_cursor(
        "codex", "canonical-session", next_turn_number=101
    )
    assert next_cursor.next_turn_number == 101


def test_explicit_reconciliation_reset_clears_only_one_derived_source_generation(
    tmp_path: Path,
):
    store = AgentSyncCursorStore(tmp_path)
    store.begin_source_denominator("codex", ["canonical-a"])
    store.record_denominator_session(
        "codex",
        "canonical-a",
        turn_count=3,
        turn_fingerprints=_fingerprints(range(3)),
        artifact_evidence_hash=_EVIDENCE_HASH,
    )
    store.advance_session_raw_cursor("codex", "canonical-a", next_turn_number=3)
    store.advance_reconciliation_after("codex", "canonical-a")

    untouched = store.advance_session_raw_cursor("kimi", "canonical-other", next_turn_number=5)
    assert untouched.next_turn_number == 5

    reset = store.reset_source_reconciliation("codex")

    assert reset.source_name == "codex"
    assert reset.session_raw_cursor_count == 1
    assert reset.reconciliation_cursor_count == 1
    assert reset.denominator_state_count == 1
    assert reset.denominator_session_count == 1
    assert store.get_session_raw_cursor("codex", "canonical-a").next_turn_number is None
    assert store.get_session_raw_cursor("kimi", "canonical-other").next_turn_number == 5
    with pytest.raises(AgentSyncCursorError, match="source denominator generation is missing"):
        store.source_denominator_progress("codex")

    rebuilt = store.begin_source_denominator("codex", ["canonical-a"])
    assert rebuilt.complete is False


def test_receipt_generation_tracks_current_turn_domain_and_drops_removed_turns(tmp_path: Path):
    store = AgentSyncCursorStore(tmp_path)
    generation = store.begin_source_denominator("codex", ["canonical-a"])
    assert generation.generation_id
    store.record_denominator_session(
        "codex",
        "canonical-a",
        turn_count=2,
        turn_numbers=[4, 9],
        turn_fingerprints=_fingerprints([4, 9]),
        artifact_evidence_hash=_EVIDENCE_HASH,
    )
    store.record_raw_capture_receipts(
        "codex",
        "canonical-a",
        [
            (4, "revision-four", _fingerprints([4])[4]),
            (9, "revision-nine", _fingerprints([9])[9]),
        ],
    )
    store.record_denominator_session(
        "codex",
        "canonical-a",
        turn_count=1,
        turn_numbers=[4],
        turn_fingerprints=_fingerprints([4]),
        artifact_evidence_hash=_EVIDENCE_HASH,
    )

    with sqlite3.connect(store.path) as conn:
        expected = conn.execute(
            "SELECT turn_number FROM source_capture_expected_turns ORDER BY turn_number"
        ).fetchall()
        receipts = conn.execute(
            "SELECT turn_number, raw_revision_id FROM source_capture_raw_receipts ORDER BY turn_number"
        ).fetchall()
    assert expected == [(4,)]
    assert receipts == [(4, "revision-four")]


def test_changed_turn_fingerprint_reopens_only_that_exact_capture_receipt(
    tmp_path: Path,
) -> None:
    store = AgentSyncCursorStore(tmp_path)
    store.begin_source_denominator("codex", ["canonical-a"])
    before = {
        0: "1" * 64,
        1: "2" * 64,
    }
    store.record_denominator_session(
        "codex",
        "canonical-a",
        turn_count=2,
        turn_numbers=[0, 1],
        turn_fingerprints=before,
        artifact_evidence_hash=_EVIDENCE_HASH,
    )
    store.record_raw_capture_receipts(
        "codex",
        "canonical-a",
        [
            (0, "revision-zero", before[0]),
            (1, "revision-one", before[1]),
        ],
    )
    assert store.pending_session_turn_numbers("codex", "canonical-a") == []

    after = {
        0: "3" * 64,
        1: before[1],
    }
    store.record_denominator_session(
        "codex",
        "canonical-a",
        turn_count=2,
        turn_numbers=[0, 1],
        turn_fingerprints=after,
        artifact_evidence_hash="sha256:" + ("b" * 64),
    )

    assert store.pending_session_turn_numbers("codex", "canonical-a") == [0]
    store.record_raw_capture_receipts(
        "codex",
        "canonical-a",
        [(0, "revision-zero-repaired", after[0])],
    )
    assert store.pending_session_turn_numbers("codex", "canonical-a") == []


def test_snapshot_binding_requires_every_exact_turn_fingerprint_receipt(
    tmp_path: Path,
) -> None:
    store = AgentSyncCursorStore(tmp_path)
    store.begin_source_denominator("codex", ["canonical-a"])
    fingerprint = "4" * 64
    store.record_denominator_session(
        "codex",
        "canonical-a",
        turn_count=1,
        turn_numbers=[0],
        turn_fingerprints={0: fingerprint},
        artifact_evidence_hash=_EVIDENCE_HASH,
    )

    with pytest.raises(
        AgentSyncCursorError,
        match="complete source capture generation is required",
    ):
        store.bind_native_source_snapshot(
            "codex",
            "a" * 64,
            expected_capture_state=store.source_capture_fingerprint_state("codex"),
        )

    store.record_raw_capture_receipts(
        "codex",
        "canonical-a",
        [(0, "revision-zero", fingerprint)],
    )
    state = store.source_capture_fingerprint_state("codex")
    store.bind_native_source_snapshot(
        "codex",
        "a" * 64,
        expected_capture_state=state,
    )


def test_capture_fingerprint_state_binds_generation_denominator_and_receipts(
    tmp_path: Path,
) -> None:
    store = AgentSyncCursorStore(tmp_path)
    progress = store.begin_source_denominator("codex", ["canonical-a"])
    fingerprints = {0: "1" * 64, 1: "2" * 64}
    store.record_denominator_session(
        "codex",
        "canonical-a",
        turn_count=2,
        turn_numbers=[0, 1],
        turn_fingerprints=fingerprints,
        artifact_evidence_hash=_EVIDENCE_HASH,
    )
    store.record_raw_capture_receipts(
        "codex",
        "canonical-a",
        [
            (0, "revision-zero", fingerprints[0]),
            (1, "revision-one", fingerprints[1]),
        ],
    )

    state = store.source_capture_fingerprint_state("codex")

    assert state.generation_id == progress.generation_id
    assert state.roster_hash == progress.roster_hash
    assert state.generation_eligible is True
    assert state.expected_turn_count == 2
    assert state.receipt_count == 2
    assert state.exact_receipt_count == 2
    assert state.pending_turn_count == 0
    assert state.orphan_receipt_count == 0
    assert state.denominator_session_set_hash == _row_set_hash(
        [
            (
                "canonical-a",
                2,
                "parsed",
                "native_turns_parsed",
                _EVIDENCE_HASH,
            )
        ]
    )
    assert state.expected_turn_fingerprint_set_hash == _row_set_hash(
        [
            ("canonical-a", 0, fingerprints[0]),
            ("canonical-a", 1, fingerprints[1]),
        ]
    )
    assert state.receipt_binding_set_hash == _row_set_hash(
        [
            ("canonical-a", 0, "revision-zero", fingerprints[0]),
            ("canonical-a", 1, "revision-one", fingerprints[1]),
        ]
    )


def test_capture_fact_change_invalidates_snapshot_and_stale_compare_and_bind(
    tmp_path: Path,
) -> None:
    store = AgentSyncCursorStore(tmp_path)
    store.begin_source_denominator("codex", ["canonical-a"])
    original = {0: "1" * 64}
    store.record_denominator_session(
        "codex",
        "canonical-a",
        turn_count=1,
        turn_numbers=[0],
        turn_fingerprints=original,
        artifact_evidence_hash=_EVIDENCE_HASH,
    )
    store.record_raw_capture_receipts(
        "codex",
        "canonical-a",
        [(0, "revision-zero", original[0])],
    )
    stale_state = store.source_capture_fingerprint_state("codex")
    store.bind_native_source_snapshot(
        "codex",
        "a" * 64,
        expected_capture_state=stale_state,
    )

    changed = {0: "3" * 64}
    store.record_denominator_session(
        "codex",
        "canonical-a",
        turn_count=1,
        turn_numbers=[0],
        turn_fingerprints=changed,
        artifact_evidence_hash="sha256:" + ("b" * 64),
    )
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("""
            SELECT native_source_snapshot_hash
            FROM source_capture_generations WHERE source_name='codex'
            """).fetchone() == ("",)
    store.record_raw_capture_receipts(
        "codex",
        "canonical-a",
        [(0, "revision-repaired", changed[0])],
    )

    with pytest.raises(
        AgentSyncCursorError,
        match="capture state changed before snapshot binding",
    ):
        store.bind_native_source_snapshot(
            "codex",
            "b" * 64,
            expected_capture_state=stale_state,
        )

    repaired_state = store.source_capture_fingerprint_state("codex")
    store.bind_native_source_snapshot(
        "codex",
        "c" * 64,
        expected_capture_state=repaired_state,
    )
    store.record_raw_capture_receipts(
        "codex",
        "canonical-a",
        [(0, "revision-replayed", changed[0])],
    )
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("""
            SELECT native_source_snapshot_hash
            FROM source_capture_generations WHERE source_name='codex'
            """).fetchone() == ("",)


@pytest.mark.parametrize(
    ("turn_count", "disposition", "reason"),
    [
        (1, "parsed", "native_turns_parsed"),
        (0, "typed_empty", "valid_empty_native_session"),
        (0, "evidence_excluded", "provider_request_failure_artifact"),
    ],
)
def test_cursor_v4_rejects_every_unbound_session_disposition(
    tmp_path: Path,
    turn_count: int,
    disposition: str,
    reason: str,
) -> None:
    store = AgentSyncCursorStore(tmp_path)
    store.begin_source_denominator("codex", ["session"])

    with pytest.raises(
        AgentSyncCursorError,
        match="denominator artifact evidence hash is invalid",
    ):
        store.record_denominator_session(
            "codex",
            "session",
            turn_count=turn_count,
            turn_fingerprints=_fingerprints(range(turn_count)),
            disposition=disposition,
            disposition_reason=reason,
        )


def test_v1_cursor_requires_explicit_backup_first_migration_and_fresh_rebuild(tmp_path: Path):
    path = tmp_path / "agent_sync_cursors.db"
    roster_hash = AgentSyncCursorStore._roster_hash(["canonical-a"])
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE cursor_schema (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE session_raw_cursors (
                source_name TEXT NOT NULL, canonical_session_id TEXT NOT NULL,
                next_turn_number INTEGER NOT NULL, last_raw_commit_at TEXT NOT NULL,
                PRIMARY KEY (source_name, canonical_session_id)
            );
            CREATE TABLE source_reconciliation_cursors (
                source_name TEXT PRIMARY KEY, after_canonical_session_id TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE source_denominator_state (
                source_name TEXT PRIMARY KEY, roster_hash TEXT NOT NULL, session_count INTEGER NOT NULL,
                observed_session_count INTEGER NOT NULL, observed_turn_count INTEGER NOT NULL,
                complete INTEGER NOT NULL, completed_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE source_denominator_sessions (
                source_name TEXT NOT NULL, canonical_session_id TEXT NOT NULL, roster_hash TEXT NOT NULL,
                turn_count INTEGER NOT NULL, observed_at TEXT NOT NULL,
                PRIMARY KEY (source_name, canonical_session_id)
            );
            """)
        conn.execute(
            "INSERT INTO cursor_schema VALUES ('schema_version', ?)",
            (LEGACY_CURSOR_SCHEMA_VERSION,),
        )
        conn.execute(
            "INSERT INTO source_denominator_state VALUES ('codex', ?, 1, 1, 1, 1, '', '')",
            (roster_hash,),
        )

    with pytest.raises(AgentSyncCursorError, match="legacy agent sync cursor schema"):
        AgentSyncCursorStore(tmp_path).get_session_raw_cursor("codex", "session")

    with sqlite3.connect(path) as conn:
        migrate_historical_cursor_schema(conn)
        assert conn.execute(
            "SELECT value FROM cursor_schema WHERE key='schema_version'"
        ).fetchone() == (CURSOR_SCHEMA_VERSION,)
        assert conn.execute("SELECT COUNT(*) FROM source_capture_raw_receipts").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM source_denominator_state").fetchone() == (1,)

    store = AgentSyncCursorStore(tmp_path)
    with pytest.raises(AgentSyncCursorError, match="explicit reset required"):
        store.begin_source_denominator("codex", ["canonical-a"])
    store.reset_source_reconciliation("codex")
    rebuilt = store.begin_source_denominator("codex", ["canonical-a"])
    assert rebuilt.generation_id
