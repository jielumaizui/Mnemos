"""Tests for the independent active-source→Raw evidence seam."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

import core.agent_kit.source_capture_verification as verification_module
from core.agent_kit.source_capture_verification import verify_source_capture
from core.agent_kit.source_support_manifest import get_agent_source_support_manifest
from core.sync_framework.native_raw_contract_ledger import NativeRawContractLedger
from core.sync_framework.raw_event_store import RawEventStore
from daemon.agent_sync_cursor import AgentSyncCursorStore


class _Config:
    def __init__(self, database_dir: Path):
        self.database_dir = database_dir
        self.data_dir = database_dir

    def get(self, _key, default=None):
        return default


_TURN_FINGERPRINTS = {
    0: "c" * 64,
    1: "d" * 64,
}


def _coverage(
    source_name: str = "codex",
    *,
    cursor_db_path: Path | None = None,
    capture_cursor_fields: dict[str, object] | None = None,
) -> dict:
    manifest = get_agent_source_support_manifest()
    if cursor_db_path is not None:
        fields = (
            AgentSyncCursorStore(cursor_db_path.parent)
            .source_capture_fingerprint_state(source_name)
            .to_cursor_fields()
        )
    elif capture_cursor_fields is not None:
        fields = dict(capture_cursor_fields)
    else:
        raise AssertionError("coverage requires independently read capture state")
    return {
        "schema_version": "mnemos.agent_source_coverage.v2",
        "support_manifest_hash": manifest.manifest_hash,
        "sources": {
            source_name: {
                "owner": "daemon.raw_sync",
                "owner_service": "raw_sync",
                "native_turns": 2,
                "error": "",
                "native_source_snapshot_hash": "a" * 64,
                "cursor": {
                    "kind": "continuous_tail_reconcile_v1",
                    "denominator_complete": True,
                    "denominator_observed_sessions": 1,
                    "discovered_sessions": 1,
                    "denominator_turns": 2,
                    **fields,
                },
            }
        },
    }


def _cursor_db(
    path: Path,
    revision_ids: list[str],
    *,
    source_name: str = "codex",
) -> None:
    store = AgentSyncCursorStore(path.parent)
    store.begin_source_denominator(source_name, ["session-1"])
    store.record_denominator_session(
        source_name,
        "session-1",
        turn_count=2,
        turn_numbers=[0, 1],
        turn_fingerprints=_TURN_FINGERPRINTS,
        artifact_evidence_hash="sha256:" + ("b" * 64),
    )
    store.record_raw_capture_receipts(
        source_name,
        "session-1",
        [
            (number, revision_id, _TURN_FINGERPRINTS[number])
            for number, revision_id in enumerate(revision_ids)
        ],
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            UPDATE source_capture_expected_turns
            SET generation_id='capture-gen-test'
            WHERE source_name=?
            """,
            (source_name,),
        )
        connection.execute(
            """
            UPDATE source_capture_raw_receipts
            SET generation_id='capture-gen-test'
            WHERE source_name=?
            """,
            (source_name,),
        )
        connection.execute(
            """
            UPDATE source_capture_generations
            SET generation_id='capture-gen-test'
            WHERE source_name=?
            """,
            (source_name,),
        )
    capture_state = store.source_capture_fingerprint_state(source_name)
    store.bind_native_source_snapshot(
        source_name,
        "a" * 64,
        expected_capture_state=capture_state,
    )


def _raw_db(
    path: Path,
    root: Path,
    *,
    source_name: str = "codex",
    runtime_receipt: dict | None = None,
    runtime_canary_text_only: bool = False,
    runtime_result_call_id: str = "runtime-probe-call",
    oversized_runtime_call: bool = False,
    oversized_runtime_call_array: bool = False,
    oversized_runtime_arguments: bool = False,
) -> list[str]:
    from core.agent_kit.runtime_receipts import runtime_probe_contract
    from core.ops.health_contract import CANONICAL_HEALTH_CHECK_IDS_HASH

    runtime_call_payload = {
        "id": "runtime-probe-call",
        "name": "agent_runtime_probe",
        "arguments": {
            "health_check_ids_hash": CANONICAL_HEALTH_CHECK_IDS_HASH,
            "sample": runtime_probe_contract()["sample"],
        },
    }
    if oversized_runtime_arguments:
        runtime_call_payload["arguments"] = json.dumps(
            {
                "padding": "x" * 262_145,
                "health_check_ids_hash": CANONICAL_HEALTH_CHECK_IDS_HASH,
                "sample": runtime_probe_contract()["sample"],
            },
            sort_keys=True,
        )
    runtime_call: object = runtime_call_payload
    if oversized_runtime_call:
        runtime_call = json.dumps(
            {"padding": "x" * 262_145, "call": runtime_call},
            sort_keys=True,
        )
    if oversized_runtime_call_array:
        runtime_call = json.dumps(
            [{"padding": "x" * 262_145}, runtime_call],
            sort_keys=True,
        )
    store = RawEventStore(db_path=path, config=_Config(root))
    try:
        revision_ids = [
            store.upsert_turn(
                source_agent=source_name,
                session_id="session-1",
                turn_number=turn_number,
                user_content=(
                    json.dumps(
                        {
                            "name": "agent_runtime_probe",
                            "arguments": {
                                "health_check_ids_hash": CANONICAL_HEALTH_CHECK_IDS_HASH,
                                "sample": runtime_probe_contract()["sample"],
                            },
                        },
                        sort_keys=True,
                    )
                    if runtime_receipt is not None and runtime_canary_text_only and turn_number == 1
                    else f"safe user {turn_number}"
                ),
                assistant_content=(
                    json.dumps(runtime_receipt, sort_keys=True)
                    if runtime_receipt is not None and runtime_canary_text_only and turn_number == 1
                    else f"safe assistant {turn_number}"
                ),
                metadata={"native_turn_fingerprint": _TURN_FINGERPRINTS[turn_number]},
                tool_calls=(
                    [runtime_call]
                    if runtime_receipt is not None
                    and not runtime_canary_text_only
                    and turn_number == 1
                    else []
                ),
                tool_results=(
                    [{"tool_call_id": runtime_result_call_id, "output": runtime_receipt}]
                    if runtime_receipt is not None
                    and not runtime_canary_text_only
                    and turn_number == 1
                    else []
                ),
            )
            for turn_number in range(2)
        ]
        ledger = NativeRawContractLedger()
        manifest = get_agent_source_support_manifest()
        conn = store._pool.get_conn()  # noqa: SLF001
        revision_records = [
            (revision_id, store.get_turn(revision_id)["logical_event_id"])
            for revision_id in revision_ids
        ]
        for index, (revision_id, logical_event_id) in enumerate(revision_records):
            ledger.record_explicit(
                conn,
                logical_event_id=logical_event_id,
                revision_id=revision_id,
                support_manifest_hash=manifest.manifest_hash,
                contract_state="conformant",
                contract_errors=[],
                observed_at=f"2026-07-13T00:00:0{index}+00:00",
            )
            ledger.refresh_effective_state(
                conn,
                logical_event_id=logical_event_id,
                observed_at=f"2026-07-13T00:00:0{index}+00:00",
            )
        conn.commit()
        return revision_ids
    finally:
        store.close()


def test_independent_verifier_accepts_ingestion_only_raw_denominator(tmp_path: Path):
    cursor_db = tmp_path / "agent_sync_cursors.db"
    raw_db = tmp_path / "raw_events.db"
    revision_ids = _raw_db(raw_db, tmp_path, source_name="gemini")
    _cursor_db(cursor_db, revision_ids, source_name="gemini")

    result = verify_source_capture(
        source_name="gemini",
        coverage=_coverage("gemini", cursor_db_path=cursor_db),
        cursor_db_path=cursor_db,
        raw_db_path=raw_db,
    )

    assert result["ok"] is True, result
    assert result["source_name"] == "gemini"
    assert result["capture_completeness"]["raw_committed_turns"] == 2


def test_independent_verifier_rejects_cursor_fingerprint_not_bound_to_raw_revision(
    tmp_path: Path,
) -> None:
    cursor_db = tmp_path / "agent_sync_cursors.db"
    raw_db = tmp_path / "raw_events.db"
    revision_ids = _raw_db(raw_db, tmp_path)
    _cursor_db(cursor_db, revision_ids)
    forged = "e" * 64
    with sqlite3.connect(cursor_db) as connection:
        connection.execute(
            """
            UPDATE source_capture_expected_turns
            SET turn_fingerprint=?
            WHERE canonical_session_id='session-1' AND turn_number=0
            """,
            (forged,),
        )
        connection.execute(
            """
            UPDATE source_capture_raw_receipts
            SET turn_fingerprint=?
            WHERE canonical_session_id='session-1' AND turn_number=0
            """,
            (forged,),
        )

    result = verify_source_capture(
        source_name="codex",
        coverage=_coverage(cursor_db_path=cursor_db),
        cursor_db_path=cursor_db,
        raw_db_path=raw_db,
    )

    assert result["ok"] is False
    assert "raw_capture_receipt_binding_mismatch" in result["errors"]


def test_independent_verifier_accepts_explicit_verified_empty_source(tmp_path: Path):
    cursor_db = tmp_path / "agent_sync_cursors.db"
    raw_db = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_db, config=_Config(tmp_path)).close()
    store = AgentSyncCursorStore(tmp_path)
    store.begin_source_denominator("aider", [])
    store.bind_native_source_snapshot(
        "aider",
        "a" * 64,
        expected_capture_state=store.source_capture_fingerprint_state("aider"),
    )
    coverage = _coverage("aider", cursor_db_path=cursor_db)
    entry = coverage["sources"]["aider"]
    entry["native_turns"] = 0
    entry["native_sessions"] = 0
    entry["cursor"].update(
        {
            "denominator_observed_sessions": 0,
            "discovered_sessions": 0,
            "denominator_turns": 0,
        }
    )

    result = verify_source_capture(
        source_name="aider",
        coverage=coverage,
        cursor_db_path=cursor_db,
        raw_db_path=raw_db,
    )

    assert result["ok"] is True, result
    assert result["capture_completeness"]["discovered_sessions"] == 0
    assert result["capture_completeness"]["raw_committed_turns"] == 0


def test_independent_verifier_rejects_unbound_snapshot_hash(tmp_path: Path):
    cursor_db = tmp_path / "agent_sync_cursors.db"
    raw_db = tmp_path / "raw_events.db"
    _cursor_db(cursor_db, _raw_db(raw_db, tmp_path))
    forged_coverage = _coverage(cursor_db_path=cursor_db)
    forged_coverage["sources"]["codex"]["native_source_snapshot_hash"] = "b" * 64

    result = verify_source_capture(
        source_name="codex",
        coverage=forged_coverage,
        cursor_db_path=cursor_db,
        raw_db_path=raw_db,
    )

    assert result["ok"] is False
    assert "cursor_snapshot_binding_mismatch" in result["errors"]


def test_independent_verifier_requires_exact_cursor_and_raw_denominator(tmp_path: Path):
    cursor_db = tmp_path / "agent_sync_cursors.db"
    raw_db = tmp_path / "raw_events.db"
    _cursor_db(cursor_db, _raw_db(raw_db, tmp_path))

    result = verify_source_capture(
        source_name="codex",
        coverage=_coverage(cursor_db_path=cursor_db),
        cursor_db_path=cursor_db,
        raw_db_path=raw_db,
    )

    assert result["ok"] is True, result
    assert result["capture_completeness"]["raw_committed_turns"] == 2
    assert result["capture_completeness"]["raw_revision_ids_hash"]

    with sqlite3.connect(raw_db) as conn:
        conn.execute("DELETE FROM raw_turns WHERE session_id='session-1' AND turn_number=1")

    rejected = verify_source_capture(
        source_name="codex",
        coverage=_coverage(cursor_db_path=cursor_db),
        cursor_db_path=cursor_db,
        raw_db_path=raw_db,
    )
    assert rejected["ok"] is False
    assert "raw_capture_receipt_unverified" in rejected["errors"]


def test_independent_verifier_rejects_forged_capture_set_hash(tmp_path: Path):
    cursor_db = tmp_path / "agent_sync_cursors.db"
    raw_db = tmp_path / "raw_events.db"
    _cursor_db(cursor_db, _raw_db(raw_db, tmp_path))
    coverage = _coverage(cursor_db_path=cursor_db)
    coverage["sources"]["codex"]["cursor"]["capture_expected_turn_fingerprint_set_hash"] = "f" * 64

    result = verify_source_capture(
        source_name="codex",
        coverage=coverage,
        cursor_db_path=cursor_db,
        raw_db_path=raw_db,
    )

    assert result["ok"] is False
    assert "cursor_capture_state_mismatch" in result["errors"]


def test_independent_verifier_reads_one_cursor_sqlite_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor_db = tmp_path / "agent_sync_cursors.db"
    raw_db = tmp_path / "raw_events.db"
    _cursor_db(cursor_db, _raw_db(raw_db, tmp_path))
    original = verification_module._read_only_connection
    cursor_open_count = 0
    cursor_snapshot_transaction_seen = False

    @contextmanager
    def tracked_connection(path: Path):
        nonlocal cursor_open_count, cursor_snapshot_transaction_seen
        if Path(path) == cursor_db:
            cursor_open_count += 1
        with original(path) as connection:
            yield connection
            if Path(path) == cursor_db:
                cursor_snapshot_transaction_seen = connection.in_transaction

    monkeypatch.setattr(
        verification_module,
        "_read_only_connection",
        tracked_connection,
    )

    result = verify_source_capture(
        source_name="codex",
        coverage=_coverage(cursor_db_path=cursor_db),
        cursor_db_path=cursor_db,
        raw_db_path=raw_db,
    )

    assert result["ok"] is True, result
    assert cursor_open_count == 1
    assert cursor_snapshot_transaction_seen is True


def test_independent_verifier_excludes_quarantined_native_contract_rows(tmp_path: Path):
    cursor_db = tmp_path / "agent_sync_cursors.db"
    raw_db = tmp_path / "raw_events.db"
    _cursor_db(cursor_db, _raw_db(raw_db, tmp_path))
    store = RawEventStore(db_path=raw_db, config=_Config(tmp_path))
    try:
        quarantined = store.upsert_turn(
            source_agent="codex",
            session_id="session-1",
            turn_number=2,
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
    finally:
        store.close()

    result = verify_source_capture(
        source_name="codex",
        coverage=_coverage(cursor_db_path=cursor_db),
        cursor_db_path=cursor_db,
        raw_db_path=raw_db,
    )

    assert result["ok"] is True, result
    assert result["capture_completeness"]["raw_committed_turns"] == 2


def test_verifier_uses_current_generation_receipts_not_historical_visible_rows(tmp_path: Path):
    cursor_db = tmp_path / "agent_sync_cursors.db"
    raw_db = tmp_path / "raw_events.db"
    _cursor_db(cursor_db, _raw_db(raw_db, tmp_path))
    store = RawEventStore(db_path=raw_db, config=_Config(tmp_path))
    try:
        # This visible historical row shares the same host/session but is not
        # part of the frozen current generation.  Row-count verification used
        # to make it a false failure; a receipt-bound verifier must ignore it.
        store.upsert_turn(
            source_agent="codex",
            session_id="session-1",
            turn_number=7,
            user_content="historical user",
            assistant_content="historical assistant",
        )
    finally:
        store.close()

    result = verify_source_capture(
        source_name="codex",
        coverage=_coverage(cursor_db_path=cursor_db),
        cursor_db_path=cursor_db,
        raw_db_path=raw_db,
    )

    assert result["ok"] is True, result
    assert result["capture_completeness"]["raw_committed_turns"] == 2


def test_verifier_accepts_native_receipt_when_quality_current_revision_differs(tmp_path: Path):
    cursor_db = tmp_path / "agent_sync_cursors.db"
    raw_db = tmp_path / "raw_events.db"
    _cursor_db(cursor_db, _raw_db(raw_db, tmp_path))
    store = RawEventStore(db_path=raw_db, config=_Config(tmp_path))
    try:
        # A later non-native write may legitimately win Raw's quality ordering.
        # The immutable native receipt must remain valid while its latest
        # native-contract observation still binds the captured revision.
        store.upsert_turn(
            source_agent="codex",
            session_id="session-1",
            turn_number=0,
            user_content="higher-quality current user",
            assistant_content="higher-quality current assistant",
        )
    finally:
        store.close()

    result = verify_source_capture(
        source_name="codex",
        coverage=_coverage(cursor_db_path=cursor_db),
        cursor_db_path=cursor_db,
        raw_db_path=raw_db,
    )

    assert result["ok"] is True, result


def test_verifier_accepts_canonical_native_identity_after_ordinal_repair(tmp_path: Path):
    cursor_db = tmp_path / "agent_sync_cursors.db"
    raw_db = tmp_path / "raw_events.db"
    _cursor_db(cursor_db, _raw_db(raw_db, tmp_path))
    with sqlite3.connect(raw_db) as conn:
        # The immutable revision and its latest native-contract observation are
        # unchanged; only an old raw projection ordinal is repaired.
        conn.execute(
            "UPDATE raw_turns SET turn_number=9 WHERE source_agent='codex' AND turn_number=0"
        )

    result = verify_source_capture(
        source_name="codex",
        coverage=_coverage(cursor_db_path=cursor_db),
        cursor_db_path=cursor_db,
        raw_db_path=raw_db,
    )

    assert result["ok"] is True, result


def test_verifier_rejects_a_receipt_when_latest_native_observation_changes(tmp_path: Path):
    cursor_db = tmp_path / "agent_sync_cursors.db"
    raw_db = tmp_path / "raw_events.db"
    _cursor_db(cursor_db, _raw_db(raw_db, tmp_path))
    store = RawEventStore(db_path=raw_db, config=_Config(tmp_path))
    try:
        changed = store.upsert_turn(
            source_agent="codex",
            session_id="session-1",
            turn_number=0,
            user_content="changed current user",
            assistant_content="changed current assistant",
        )
        logical_event_id = store.get_turn(changed)["logical_event_id"]
        ledger = NativeRawContractLedger()
        conn = store._pool.get_conn()  # noqa: SLF001
        ledger.record_explicit(
            conn,
            logical_event_id=logical_event_id,
            revision_id=changed,
            support_manifest_hash=get_agent_source_support_manifest().manifest_hash,
            contract_state="conformant",
            contract_errors=[],
            observed_at="2026-07-13T00:01:00+00:00",
        )
        ledger.refresh_effective_state(
            conn,
            logical_event_id=logical_event_id,
            observed_at="2026-07-13T00:01:00+00:00",
        )
        conn.commit()
    finally:
        store.close()

    result = verify_source_capture(
        source_name="codex",
        coverage=_coverage(cursor_db_path=cursor_db),
        cursor_db_path=cursor_db,
        raw_db_path=raw_db,
    )

    assert result["ok"] is False
    assert "raw_capture_receipt_binding_mismatch" in result["errors"]


def test_runtime_store_binds_safe_probe_and_source_capture_evidence(tmp_path: Path):
    from core.agent_kit.authorization import AgentAuthorizationStore
    from core.agent_kit.runtime_receipts import AgentRuntimeReceiptStore, runtime_probe_contract
    from core.ops.health_contract import CANONICAL_HEALTH_CHECK_IDS_HASH

    cursor_db = tmp_path / "agent_sync_cursors.db"
    raw_db = tmp_path / "raw_events.db"
    receipt_db = tmp_path / "agent_authorization.db"
    authorization = AgentAuthorizationStore(receipt_db)
    authorization.set_state("codex", "user_authorized")
    store = AgentRuntimeReceiptStore(receipt_db)
    store.record_health_check("codex", CANONICAL_HEALTH_CHECK_IDS_HASH)
    assert (
        store.record_probe(
            "codex",
            health_check_ids_hash=CANONICAL_HEALTH_CHECK_IDS_HASH,
            sample=runtime_probe_contract()["sample"],
        )["success"]
        is True
    )
    runtime_receipt = store.evaluate("codex")
    revision_ids = _raw_db(
        raw_db,
        tmp_path,
        runtime_receipt=runtime_receipt,
    )
    _cursor_db(cursor_db, revision_ids)
    evidence = verify_source_capture(
        source_name="codex",
        coverage=_coverage(cursor_db_path=cursor_db),
        cursor_db_path=cursor_db,
        raw_db_path=raw_db,
        runtime_receipt=runtime_receipt,
    )
    assert evidence["ok"] is True, evidence

    receipt = store.record_source_capture(
        "codex",
        coverage=_coverage(cursor_db_path=cursor_db),
        cursor_db_path=cursor_db,
        raw_db_path=raw_db,
    )

    assert receipt["success"] is True
    assert receipt["health_check_ids_hash"] == CANONICAL_HEALTH_CHECK_IDS_HASH
    assert receipt["support_manifest_hash"] == get_agent_source_support_manifest().manifest_hash
    assert receipt["native_source_snapshot_hash"] == "a" * 64
    assert receipt["capture_completeness"]["raw_committed"] is True
    assert receipt["capture_completeness"]["runtime_canary_verified"] is True

    with sqlite3.connect(raw_db) as connection:
        connection.execute("DELETE FROM raw_turns WHERE source_agent='codex' AND turn_number=1")
    rejected = store.record_source_capture(
        "codex",
        coverage=_coverage(cursor_db_path=cursor_db),
        cursor_db_path=cursor_db,
        raw_db_path=raw_db,
    )
    assert rejected["success"] is False
    assert rejected["source_capture_state"] == "source_capture_invalid"

    with pytest.raises(TypeError):
        store.record_source_capture("codex", evidence=evidence)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "source_name",
    ("codex", "claude", "hermes", "opencode", "openclaw", "crush", "kiro", "kimi"),
)
def test_host_verifier_binds_runtime_canary_to_exact_raw_generation(
    tmp_path: Path,
    source_name: str,
):
    from core.agent_kit.authorization import AgentAuthorizationStore
    from core.agent_kit.runtime_receipts import AgentRuntimeReceiptStore, runtime_probe_contract
    from core.ops.health_contract import CANONICAL_HEALTH_CHECK_IDS_HASH

    cursor_db = tmp_path / "agent_sync_cursors.db"
    raw_db = tmp_path / "raw_events.db"
    receipt_db = tmp_path / "agent_authorization.db"
    authorization = AgentAuthorizationStore(receipt_db)
    authorization.set_state(source_name, "user_authorized")
    receipt_store = AgentRuntimeReceiptStore(receipt_db)
    receipt_store.record_health_check(source_name, CANONICAL_HEALTH_CHECK_IDS_HASH)
    runtime_receipt = receipt_store.record_probe(
        source_name,
        health_check_ids_hash=CANONICAL_HEALTH_CHECK_IDS_HASH,
        sample=runtime_probe_contract()["sample"],
    )
    revision_ids = _raw_db(
        raw_db,
        tmp_path,
        source_name=source_name,
        runtime_receipt=runtime_receipt,
    )
    _cursor_db(cursor_db, revision_ids, source_name=source_name)

    evidence = verify_source_capture(
        source_name=source_name,
        coverage=_coverage(source_name, cursor_db_path=cursor_db),
        cursor_db_path=cursor_db,
        raw_db_path=raw_db,
        runtime_receipt=runtime_receipt,
    )

    assert evidence["ok"] is True, evidence
    assert evidence["capture_completeness"]["runtime_canary_verified"] is True
    assert (
        evidence["capture_completeness"]["runtime_canary_hash"]
        == runtime_receipt["runtime_canary_hash"]
    )
    assert evidence["capture_completeness"]["runtime_canary_raw_revision_ids_hash"]


def test_host_verifier_rejects_runtime_receipt_without_raw_canary(tmp_path: Path):
    from core.agent_kit.authorization import AgentAuthorizationStore
    from core.agent_kit.runtime_receipts import AgentRuntimeReceiptStore, runtime_probe_contract
    from core.ops.health_contract import CANONICAL_HEALTH_CHECK_IDS_HASH

    cursor_db = tmp_path / "agent_sync_cursors.db"
    raw_db = tmp_path / "raw_events.db"
    receipt_db = tmp_path / "agent_authorization.db"
    authorization = AgentAuthorizationStore(receipt_db)
    authorization.set_state("codex", "user_authorized")
    receipt_store = AgentRuntimeReceiptStore(receipt_db)
    receipt_store.record_health_check("codex", CANONICAL_HEALTH_CHECK_IDS_HASH)
    runtime_receipt = receipt_store.record_probe(
        "codex",
        health_check_ids_hash=CANONICAL_HEALTH_CHECK_IDS_HASH,
        sample=runtime_probe_contract()["sample"],
    )
    revision_ids = _raw_db(raw_db, tmp_path)
    _cursor_db(cursor_db, revision_ids)

    evidence = verify_source_capture(
        source_name="codex",
        coverage=_coverage(cursor_db_path=cursor_db),
        cursor_db_path=cursor_db,
        raw_db_path=raw_db,
        runtime_receipt=runtime_receipt,
    )

    assert evidence["ok"] is False
    assert evidence["capture_completeness"]["runtime_canary_verified"] is False
    assert "runtime_canary_raw_call_missing" in evidence["errors"]
    assert "runtime_canary_raw_result_missing" in evidence["errors"]


def test_host_verifier_rejects_self_signed_canary_in_visible_text(tmp_path: Path):
    from core.agent_kit.authorization import AgentAuthorizationStore
    from core.agent_kit.runtime_receipts import AgentRuntimeReceiptStore, runtime_probe_contract
    from core.ops.health_contract import CANONICAL_HEALTH_CHECK_IDS_HASH

    cursor_db = tmp_path / "agent_sync_cursors.db"
    raw_db = tmp_path / "raw_events.db"
    receipt_db = tmp_path / "agent_authorization.db"
    authorization = AgentAuthorizationStore(receipt_db)
    authorization.set_state("codex", "user_authorized")
    receipt_store = AgentRuntimeReceiptStore(receipt_db)
    receipt_store.record_health_check("codex", CANONICAL_HEALTH_CHECK_IDS_HASH)
    runtime_receipt = receipt_store.record_probe(
        "codex",
        health_check_ids_hash=CANONICAL_HEALTH_CHECK_IDS_HASH,
        sample=runtime_probe_contract()["sample"],
    )
    revision_ids = _raw_db(
        raw_db,
        tmp_path,
        runtime_receipt=runtime_receipt,
        runtime_canary_text_only=True,
    )
    _cursor_db(cursor_db, revision_ids)

    evidence = verify_source_capture(
        source_name="codex",
        coverage=_coverage(cursor_db_path=cursor_db),
        cursor_db_path=cursor_db,
        raw_db_path=raw_db,
        runtime_receipt=runtime_receipt,
    )

    assert evidence["ok"] is False
    assert "runtime_canary_raw_call_missing" in evidence["errors"]
    assert "runtime_canary_raw_result_missing" in evidence["errors"]


def test_host_verifier_rejects_probe_result_from_another_tool_call(tmp_path: Path):
    from core.agent_kit.authorization import AgentAuthorizationStore
    from core.agent_kit.runtime_receipts import AgentRuntimeReceiptStore, runtime_probe_contract
    from core.ops.health_contract import CANONICAL_HEALTH_CHECK_IDS_HASH

    cursor_db = tmp_path / "agent_sync_cursors.db"
    raw_db = tmp_path / "raw_events.db"
    receipt_db = tmp_path / "agent_authorization.db"
    authorization = AgentAuthorizationStore(receipt_db)
    authorization.set_state("codex", "user_authorized")
    receipt_store = AgentRuntimeReceiptStore(receipt_db)
    receipt_store.record_health_check("codex", CANONICAL_HEALTH_CHECK_IDS_HASH)
    runtime_receipt = receipt_store.record_probe(
        "codex",
        health_check_ids_hash=CANONICAL_HEALTH_CHECK_IDS_HASH,
        sample=runtime_probe_contract()["sample"],
    )
    revision_ids = _raw_db(
        raw_db,
        tmp_path,
        runtime_receipt=runtime_receipt,
        runtime_result_call_id="unrelated-call",
    )
    _cursor_db(cursor_db, revision_ids)

    evidence = verify_source_capture(
        source_name="codex",
        coverage=_coverage(cursor_db_path=cursor_db),
        cursor_db_path=cursor_db,
        raw_db_path=raw_db,
        runtime_receipt=runtime_receipt,
    )

    assert evidence["ok"] is False
    assert "runtime_canary_raw_call_result_mismatch" in evidence["errors"]


def test_host_verifier_bounds_json_encoded_raw_tool_evidence(tmp_path: Path):
    from core.agent_kit.authorization import AgentAuthorizationStore
    from core.agent_kit.runtime_receipts import AgentRuntimeReceiptStore, runtime_probe_contract
    from core.ops.health_contract import CANONICAL_HEALTH_CHECK_IDS_HASH

    cursor_db = tmp_path / "agent_sync_cursors.db"
    raw_db = tmp_path / "raw_events.db"
    receipt_db = tmp_path / "agent_authorization.db"
    authorization = AgentAuthorizationStore(receipt_db)
    authorization.set_state("codex", "user_authorized")
    receipt_store = AgentRuntimeReceiptStore(receipt_db)
    receipt_store.record_health_check("codex", CANONICAL_HEALTH_CHECK_IDS_HASH)
    runtime_receipt = receipt_store.record_probe(
        "codex",
        health_check_ids_hash=CANONICAL_HEALTH_CHECK_IDS_HASH,
        sample=runtime_probe_contract()["sample"],
    )
    revision_ids = _raw_db(
        raw_db,
        tmp_path,
        runtime_receipt=runtime_receipt,
        oversized_runtime_call=True,
    )
    _cursor_db(cursor_db, revision_ids)

    evidence = verify_source_capture(
        source_name="codex",
        coverage=_coverage(cursor_db_path=cursor_db),
        cursor_db_path=cursor_db,
        raw_db_path=raw_db,
        runtime_receipt=runtime_receipt,
    )

    assert evidence["ok"] is False
    assert "runtime_canary_raw_call_missing" in evidence["errors"]


def test_host_verifier_bounds_json_encoded_probe_arguments(tmp_path: Path):
    from core.agent_kit.authorization import AgentAuthorizationStore
    from core.agent_kit.runtime_receipts import AgentRuntimeReceiptStore, runtime_probe_contract
    from core.ops.health_contract import CANONICAL_HEALTH_CHECK_IDS_HASH

    cursor_db = tmp_path / "agent_sync_cursors.db"
    raw_db = tmp_path / "raw_events.db"
    receipt_db = tmp_path / "agent_authorization.db"
    authorization = AgentAuthorizationStore(receipt_db)
    authorization.set_state("codex", "user_authorized")
    receipt_store = AgentRuntimeReceiptStore(receipt_db)
    receipt_store.record_health_check("codex", CANONICAL_HEALTH_CHECK_IDS_HASH)
    runtime_receipt = receipt_store.record_probe(
        "codex",
        health_check_ids_hash=CANONICAL_HEALTH_CHECK_IDS_HASH,
        sample=runtime_probe_contract()["sample"],
    )
    revision_ids = _raw_db(
        raw_db,
        tmp_path,
        runtime_receipt=runtime_receipt,
        oversized_runtime_arguments=True,
    )
    _cursor_db(cursor_db, revision_ids)

    evidence = verify_source_capture(
        source_name="codex",
        coverage=_coverage(cursor_db_path=cursor_db),
        cursor_db_path=cursor_db,
        raw_db_path=raw_db,
        runtime_receipt=runtime_receipt,
    )

    assert evidence["ok"] is False
    assert "runtime_canary_raw_call_missing" in evidence["errors"]


def test_host_verifier_bounds_json_encoded_raw_tool_array(tmp_path: Path):
    from core.agent_kit.authorization import AgentAuthorizationStore
    from core.agent_kit.runtime_receipts import AgentRuntimeReceiptStore, runtime_probe_contract
    from core.ops.health_contract import CANONICAL_HEALTH_CHECK_IDS_HASH

    cursor_db = tmp_path / "agent_sync_cursors.db"
    raw_db = tmp_path / "raw_events.db"
    receipt_db = tmp_path / "agent_authorization.db"
    authorization = AgentAuthorizationStore(receipt_db)
    authorization.set_state("codex", "user_authorized")
    receipt_store = AgentRuntimeReceiptStore(receipt_db)
    receipt_store.record_health_check("codex", CANONICAL_HEALTH_CHECK_IDS_HASH)
    runtime_receipt = receipt_store.record_probe(
        "codex",
        health_check_ids_hash=CANONICAL_HEALTH_CHECK_IDS_HASH,
        sample=runtime_probe_contract()["sample"],
    )
    revision_ids = _raw_db(
        raw_db,
        tmp_path,
        runtime_receipt=runtime_receipt,
        oversized_runtime_call_array=True,
    )
    _cursor_db(cursor_db, revision_ids)

    evidence = verify_source_capture(
        source_name="codex",
        coverage=_coverage(cursor_db_path=cursor_db),
        cursor_db_path=cursor_db,
        raw_db_path=raw_db,
        runtime_receipt=runtime_receipt,
    )

    assert evidence["ok"] is False
    assert "runtime_canary_raw_call_missing" in evidence["errors"]


def test_attestation_command_is_read_only_until_explicit_apply(tmp_path: Path, monkeypatch, capsys):
    from scripts import attest_agent_source_capture as command
    from core.agent_kit.authorization import AgentAuthorizationStore
    from core.agent_kit.runtime_receipts import AgentRuntimeReceiptStore, runtime_probe_contract
    from core.ops.health_contract import CANONICAL_HEALTH_CHECK_IDS_HASH

    cursor_db = tmp_path / "agent_sync_cursors.db"
    raw_db = tmp_path / "raw_events.db"
    receipt_db = tmp_path / "agent_authorization.db"
    coverage_path = tmp_path / "agent_source_coverage.json"
    authorization = AgentAuthorizationStore(receipt_db)
    authorization.set_state("codex", "user_authorized")
    receipt_store = AgentRuntimeReceiptStore(receipt_db)
    receipt_store.record_health_check("codex", CANONICAL_HEALTH_CHECK_IDS_HASH)
    runtime_receipt = receipt_store.record_probe(
        "codex",
        health_check_ids_hash=CANONICAL_HEALTH_CHECK_IDS_HASH,
        sample=runtime_probe_contract()["sample"],
    )
    _cursor_db(
        cursor_db,
        _raw_db(raw_db, tmp_path, runtime_receipt=runtime_receipt),
    )
    coverage_path.write_text(
        json.dumps(_coverage(cursor_db_path=cursor_db)),
        encoding="utf-8",
    )
    before_receipt_bytes = receipt_db.read_bytes()

    class _ReadOnlyConfig:
        database_dir = tmp_path

    monkeypatch.setattr(command, "Config", lambda **_kwargs: _ReadOnlyConfig())

    exit_code = command.main(
        [
            "--agent",
            "codex",
            "--coverage",
            str(coverage_path),
            "--cursor-db",
            str(cursor_db),
            "--raw-db",
            str(raw_db),
            "--receipt-db",
            str(receipt_db),
            "--json",
        ]
    )

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["apply"] is False
    assert output["ok"] is True
    assert output["runtime_receipt_state"] == "verified"
    assert receipt_db.read_bytes() == before_receipt_bytes


def test_attestation_command_rejects_host_without_runtime_canary_receipt(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    from scripts import attest_agent_source_capture as command

    cursor_db = tmp_path / "agent_sync_cursors.db"
    raw_db = tmp_path / "raw_events.db"
    coverage_path = tmp_path / "agent_source_coverage.json"
    _cursor_db(cursor_db, _raw_db(raw_db, tmp_path))
    coverage_path.write_text(
        json.dumps(_coverage(cursor_db_path=cursor_db)),
        encoding="utf-8",
    )

    class _ReadOnlyConfig:
        database_dir = tmp_path

    monkeypatch.setattr(command, "Config", lambda **_kwargs: _ReadOnlyConfig())

    exit_code = command.main(
        [
            "--agent",
            "codex",
            "--coverage",
            str(coverage_path),
            "--cursor-db",
            str(cursor_db),
            "--raw-db",
            str(raw_db),
            "--json",
        ]
    )

    assert exit_code == 1
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert "runtime_canary_receipt_invalid" in output["evidence"]["errors"]
    assert not (tmp_path / "agent_authorization.db").exists()
