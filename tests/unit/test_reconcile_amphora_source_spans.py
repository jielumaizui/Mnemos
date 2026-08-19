from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.kia import amphora
from core.kia.amphora_source_span_migration import (
    apply_source_span_migrations,
)
from core.kia.amphora_types import SYSTEM_OWNED_META_KEYS
from core.ops.cognitive_data_contract import CognitiveDataEvent
from core.ops.durable_io import DurableIOError
from core.ops.producer_consumer_ledger import DEFAULT_MATRIX, ProducerConsumerLedger
from core.pipeline_receipts import DistillationEnqueueReceipt
from core.sync_framework.capture_schema import CaptureQueueSchema
from core.sync_framework.raw_event_identity_aliases import apply_reconciliation
from core.sync_framework.raw_event_store import RawEventStore
from scripts import reconcile_amphora_source_spans


def _config(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(database_dir=tmp_path)


def _raw_turn(
    tmp_path: Path,
    *,
    turn_number: int,
    user: str,
    assistant: str,
    content_hash: str,
    source_agent: str = "codex",
    session_id: str = "session-1",
    metadata: dict[str, object] | None = None,
) -> str:
    store = RawEventStore(db_path=tmp_path / "raw_events.db")
    try:
        return store.upsert_turn(
            source_agent=source_agent,
            session_id=session_id,
            turn_number=turn_number,
            user_content=user,
            assistant_content=assistant,
            content_hash=content_hash,
            metadata=metadata,
            completeness={"visible_text": "full"},
        )
    finally:
        store.close()


def _sync_event(
    ledger: ProducerConsumerLedger,
    *,
    event_id: str,
    turn_number: int,
    content_hash: str,
) -> None:
    ledger.record_data_event(
        CognitiveDataEvent(
            event_id=event_id,
            source_id=f"legacy:turn:{turn_number}",
            asset_id=f"legacy:turn:{turn_number}",
            source_kind="sync_engine",
            source_uri=f"sync://codex/session-1/turn/{turn_number}",
            content_hash=content_hash,
            canonical_subject=f"codex:session-1:turn:{turn_number}",
            data_type="synced_turn",
            producer="sync_engine",
            intended_consumers=("amphora", "distill"),
            privacy_level="local",
            confidence=1.0,
            evidence_refs=(f"legacy:turn:{turn_number}",),
            dedupe_key=f"sync:session-1:{turn_number}",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
    )


@pytest.fixture
def isolated_amphora(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(amphora, "_DB_PATH", tmp_path / "distill_queue.db")
    return tmp_path


def _linked_legacy_task(
    tmp_path: Path,
) -> tuple[DistillationEnqueueReceipt, tuple[str, str]]:
    revisions = (
        _raw_turn(
            tmp_path,
            turn_number=0,
            user="first question",
            assistant="first answer",
            content_hash="raw-hash-0",
        ),
        _raw_turn(
            tmp_path,
            turn_number=1,
            user="second question",
            assistant="second answer",
            content_hash="raw-hash-1",
        ),
    )
    ledger = ProducerConsumerLedger(_config(tmp_path), initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    _sync_event(
        ledger,
        event_id="cde-span-0",
        turn_number=0,
        content_hash="raw-hash-0",
    )
    _sync_event(
        ledger,
        event_id="cde-span-1",
        turn_number=1,
        content_hash="raw-hash-1",
    )
    receipt = amphora.enqueue_with_receipt(
        "session-1",
        [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "second question"},
            {"role": "assistant", "content": "second answer"},
        ],
        {
            "source": "codex",
            "capture_source": "sync_engine",
            "cognitive_sync_event_ids": ["cde-span-0", "cde-span-1"],
        },
    )
    return receipt, revisions


def _legacy_capture_handoff(
    tmp_path: Path,
    *,
    user: str,
    assistant: str,
    content_hash: str,
) -> DistillationEnqueueReceipt:
    receipt = amphora.enqueue_with_receipt(
        "session-1",
        [
            {"role": "user", "content": user},
            {"role": "assistant", "content": assistant},
        ],
        {
            "source": "codex",
            "capture_source": "capture_worker",
            "handoff_receipt_id": "capture-handoff-legacy",
        },
    )
    CaptureQueueSchema.initialize(tmp_path / "capture_queue.db")
    payload = {
        "user_content": user,
        "assistant_content": assistant,
        "timestamp": "2026-01-01T00:00:00+00:00",
        "model": "codex",
        "metadata": {"full_content_hash": content_hash},
        "tool_calls": [],
        "tool_results": [],
        "reasoning": "",
        "attachments": [],
        "raw_event_refs": [],
        "source_files": [],
        "completeness": {"visible_text": "host_provided"},
    }
    messages = [
        {"role": "user", "content": user},
        {"role": "assistant", "content": assistant},
    ]
    with sqlite3.connect(tmp_path / "capture_queue.db") as conn:
        conn.execute(
            """
            INSERT INTO capture_events (
                id, dedupe_key, source_agent, session_id, turn_number,
                payload_json, content_hash, raw_revision_id, status, created_at
            ) VALUES (1, 'legacy-capture-1', 'codex', 'session-1', 0, ?, ?, '',
                      'done', '2026-01-01T00:00:00+00:00')
            """,
            (json.dumps(payload), content_hash),
        )
        conn.execute(
            """
            INSERT INTO capture_distillation_handoffs (
                receipt_id, source_agent, session_id, input_revision, status,
                event_ids_json, messages_json, meta_json, downstream_task_id,
                created_at, updated_at
            ) VALUES (?, 'codex', 'session-1', ?, 'committed', '[1]', ?, '{}', ?,
                      '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')
            """,
            (
                "capture-handoff-legacy",
                receipt.input_revision,
                json.dumps(messages),
                receipt.task_id,
            ),
        )
    return receipt


def test_plan_reconstructs_exact_role_local_spans_from_linked_events(
    isolated_amphora: Path,
) -> None:
    legacy, revisions = _linked_legacy_task(isolated_amphora)

    plan = reconcile_amphora_source_spans.build_source_span_reconciliation_plan(
        _config(isolated_amphora)
    )

    assert plan["ok"] is True, plan["blocked_by_reason"]
    assert plan["missing_span_tasks"] == 1
    assert plan["candidate_tasks"] == 1
    assert plan["blocked_by_reason"] == {}
    candidate = plan["objects"][0]
    assert candidate["legacy_task_id"] == legacy.task_id
    assert [
        message["source_span"]["revision_id"] for message in candidate["canonical_messages"]
    ] == [revisions[0], revisions[0], revisions[1], revisions[1]]
    assert [
        (message["source_span"]["span_start"], message["source_span"]["span_end"])
        for message in candidate["canonical_messages"]
    ] == [
        (0, len("first question")),
        (len("first question"), len("first questionfirst answer")),
        (0, len("second question")),
        (len("second question"), len("second questionsecond answer")),
    ]


def test_plan_fails_closed_when_linked_raw_does_not_equal_legacy_messages(
    isolated_amphora: Path,
) -> None:
    _linked_legacy_task(isolated_amphora)
    task = amphora.list_pending()[0]
    Path(task["messages_path"]).write_text(
        json.dumps([{"role": "user", "content": "different bytes"}]),
        encoding="utf-8",
    )

    plan = reconcile_amphora_source_spans.build_source_span_reconciliation_plan(
        _config(isolated_amphora)
    )

    assert plan["ok"] is False
    assert plan["candidate_tasks"] == 0
    assert plan["blocked_by_reason"] == {"visible_messages_differ": 1}


def test_plan_preserves_ordered_raw_refs_with_distinct_same_turn_events(
    isolated_amphora: Path,
) -> None:
    store = RawEventStore(db_path=isolated_amphora / "raw_events.db")
    try:
        first = store.upsert_turn(
            source_agent="codex",
            session_id="session-1",
            turn_number=0,
            user_content="first same-turn message",
            assistant_content="",
            content_hash="same-turn-first",
            metadata={"native_event_id": "message-a"},
            completeness={"visible_text": "full"},
        )
        second = store.upsert_turn(
            source_agent="codex",
            session_id="session-1",
            turn_number=0,
            user_content="second same-turn message",
            assistant_content="second answer",
            content_hash="same-turn-second",
            metadata={"native_event_id": "message-b"},
            completeness={"visible_text": "full"},
        )
        logical_ids = {
            revision_id: store.get_logical_event_id(revision_id) for revision_id in (first, second)
        }
    finally:
        store.close()
    amphora.enqueue_with_receipt(
        "session-1",
        [
            {"role": "user", "content": "first same-turn message"},
            {"role": "user", "content": "second same-turn message"},
            {"role": "assistant", "content": "second answer"},
        ],
        {
            "source": "codex",
            "capture_source": "capture_worker",
            "raw_event_refs": [
                {
                    "revision_id": first,
                    "logical_event_id": logical_ids[first],
                    "turn_number": 0,
                    "content_hash": "same-turn-first",
                    "span_start": 0,
                    "span_end": len("first same-turn message"),
                },
                {
                    "revision_id": second,
                    "logical_event_id": logical_ids[second],
                    "turn_number": 0,
                    "content_hash": "same-turn-second",
                    "span_start": 0,
                    "span_end": len("second same-turn messagesecond answer"),
                },
            ],
        },
    )

    plan = reconcile_amphora_source_spans.build_source_span_reconciliation_plan(
        _config(isolated_amphora)
    )

    assert plan["ok"] is True, plan["blocked_by_reason"]
    assert [
        message["source_span"]["revision_id"]
        for message in plan["objects"][0]["canonical_messages"]
    ] == [first, second, second]


def test_plan_keeps_exact_immutable_alias_revision_when_visible_canonical_differs(
    isolated_amphora: Path,
) -> None:
    original = _raw_turn(
        isolated_amphora,
        turn_number=0,
        user="historical exact bytes",
        assistant="historical answer",
        content_hash="historical-hash",
    )
    canonical = _raw_turn(
        isolated_amphora,
        turn_number=0,
        user="different current bytes",
        assistant="different current answer",
        content_hash="canonical-hash",
        metadata={"native_event_id": "native-message-0"},
    )
    alias_result = apply_reconciliation(isolated_amphora / "raw_events.db")
    assert alias_result["applied_count"] == 1
    with sqlite3.connect(isolated_amphora / "raw_events.db") as conn:
        original_logical_id = str(
            conn.execute(
                "SELECT logical_event_id FROM raw_turn_revisions WHERE revision_id=?",
                (original,),
            ).fetchone()[0]
        )
    amphora.enqueue_with_receipt(
        "session-1",
        [
            {"role": "user", "content": "historical exact bytes"},
            {"role": "assistant", "content": "historical answer"},
        ],
        {
            "source": "codex",
            "capture_source": "capture_worker",
            "raw_event_refs": [
                {
                    "revision_id": original,
                    "logical_event_id": original_logical_id,
                    "turn_number": 0,
                    "content_hash": "historical-hash",
                    "span_start": 0,
                    "span_end": len("historical exact byteshistorical answer"),
                }
            ],
        },
    )

    plan = reconcile_amphora_source_spans.build_source_span_reconciliation_plan(
        _config(isolated_amphora)
    )

    assert plan["ok"] is True, plan["blocked_by_reason"]
    assert {
        message["source_span"]["revision_id"]
        for message in plan["objects"][0]["canonical_messages"]
    } == {original}
    assert canonical != original


def test_plan_accepts_unique_exact_raw_revision_committed_just_after_enqueue(
    isolated_amphora: Path,
) -> None:
    amphora.enqueue_with_receipt(
        "session-1",
        [
            {"role": "user", "content": "queued before raw"},
            {"role": "assistant", "content": "raw follows immediately"},
            {"role": "user", "content": "anchored question"},
            {"role": "assistant", "content": "anchored answer"},
        ],
        {
            "source": "codex",
            "capture_source": "sync_engine",
            "cognitive_sync_event_ids": ["cde-post-anchor"],
        },
    )
    revision = _raw_turn(
        isolated_amphora,
        turn_number=0,
        user="queued before raw",
        assistant="raw follows immediately",
        content_hash="post-enqueue-hash",
        metadata={"native_event_id": "post-enqueue-message"},
    )
    anchor_revision = _raw_turn(
        isolated_amphora,
        turn_number=1,
        user="anchored question",
        assistant="anchored answer",
        content_hash="anchor-hash",
        metadata={"native_event_id": "anchor-message"},
    )
    ledger = ProducerConsumerLedger(_config(isolated_amphora), initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    _sync_event(
        ledger,
        event_id="cde-post-anchor",
        turn_number=1,
        content_hash="anchor-hash",
    )

    plan = reconcile_amphora_source_spans.build_source_span_reconciliation_plan(
        _config(isolated_amphora)
    )

    assert plan["ok"] is True, plan["blocked_by_reason"]
    assert {
        message["source_span"]["revision_id"]
        for message in plan["objects"][0]["canonical_messages"]
    } == {revision, anchor_revision}


def test_plan_rejects_two_indistinguishable_raw_alignments(
    isolated_amphora: Path,
) -> None:
    revisions = []
    for native_event_id in ("ambiguous-a", "ambiguous-b"):
        revisions.append(
            _raw_turn(
                isolated_amphora,
                turn_number=0,
                user="same visible question",
                assistant="same visible answer",
                content_hash=f"hash-{native_event_id}",
                metadata={"native_event_id": native_event_id},
            )
        )
    with sqlite3.connect(isolated_amphora / "raw_events.db") as conn:
        conn.executemany(
            "UPDATE raw_turn_revisions SET created_at=? WHERE revision_id=?",
            [("2026-01-01T00:00:00+00:00", revision) for revision in revisions],
        )
    amphora.enqueue_with_receipt(
        "session-1",
        [
            {"role": "user", "content": "same visible question"},
            {"role": "assistant", "content": "same visible answer"},
        ],
        {"source": "codex", "capture_source": "sync_engine"},
    )

    plan = reconcile_amphora_source_spans.build_source_span_reconciliation_plan(
        _config(isolated_amphora)
    )

    assert plan["ok"] is False
    assert plan["candidate_tasks"] == 0
    assert plan["blocked_by_reason"] == {"temporal_raw_preimage_ambiguous": 1}


def test_plan_resolves_legacy_capture_handoff_without_stored_raw_revision(
    isolated_amphora: Path,
) -> None:
    revision = _raw_turn(
        isolated_amphora,
        turn_number=0,
        user="legacy capture question",
        assistant="legacy capture answer",
        content_hash="legacy-capture-hash",
        metadata={"native_event_id": "legacy-capture-native"},
    )
    _legacy_capture_handoff(
        isolated_amphora,
        user="legacy capture question",
        assistant="legacy capture answer",
        content_hash="legacy-capture-hash",
    )

    plan = reconcile_amphora_source_spans.build_source_span_reconciliation_plan(
        _config(isolated_amphora)
    )

    assert plan["ok"] is True, plan["blocked_by_reason"]
    assert {
        message["source_span"]["revision_id"]
        for message in plan["objects"][0]["canonical_messages"]
    } == {revision}


def test_capture_raw_backfill_is_reviewed_backed_up_and_replayed_exactly(
    isolated_amphora: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _legacy_capture_handoff(
        isolated_amphora,
        user="capture-only question",
        assistant="capture-only answer",
        content_hash="capture-only-hash",
    )
    RawEventStore(db_path=isolated_amphora / "raw_events.db").close()
    reviewed = reconcile_amphora_source_spans.build_source_span_reconciliation_plan(
        _config(isolated_amphora)
    )
    assert reviewed["ok"] is False
    assert reviewed["blocked_by_reason"] == {"capture_raw_revision_missing": 1}
    assert reviewed["capture_raw_backfill_events"] == 1
    assert reviewed["capture_raw_backfill_tasks"] == 1
    monkeypatch.setattr(
        reconcile_amphora_source_spans,
        "_runtime_writers_are_inactive",
        lambda _database_dir: True,
    )

    result = reconcile_amphora_source_spans.reconcile_capture_raw_backfills(
        _config(isolated_amphora),
        apply=True,
        backup_dir=isolated_amphora / "capture-raw-backups",
        expected_manifest_hash=reviewed["capture_raw_backfill_manifest_hash"],
    )

    assert result["ok"] is True, result
    assert result["status"] == "verified"
    assert result["applied"]["raw_revisions_created_or_reused"] == 1
    assert result["applied"]["raw_provenance_edges"] == 1
    assert Path(result["backup"]["raw_events"]["path"]).is_file()
    assert Path(result["backup"]["capture_queue"]["path"]).is_file()
    post = reconcile_amphora_source_spans.build_source_span_reconciliation_plan(
        _config(isolated_amphora)
    )
    assert post["ok"] is True, post["blocked_by_reason"]
    assert post["candidate_tasks"] == 1
    assert post["capture_raw_backfill_events"] == 0


@pytest.mark.parametrize("backup_kind", ["reviewed", "capture_raw"])
def test_backup_leaf_failure_removes_only_the_new_partial_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backup_kind: str,
) -> None:
    database_dir = tmp_path / backup_kind / "database"
    database_dir.mkdir(parents=True)
    database_names = (
        ("distill_queue.db", "producer_consumer_ledger.db")
        if backup_kind == "reviewed"
        else ("raw_events.db", "capture_queue.db", "distill_queue.db")
    )
    for name in database_names:
        with sqlite3.connect(database_dir / name) as connection:
            connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
            connection.execute("INSERT INTO sentinel VALUES ('reviewed')")
    backup_root = tmp_path / backup_kind / "backups"
    original_backup = reconcile_amphora_source_spans._backup_database  # noqa: SLF001
    calls = 0

    def fail_after_second_complete_backup(
        source: Path,
        target: Path,
        **kwargs: object,
    ):
        nonlocal calls
        result = original_backup(source, target, **kwargs)
        calls += 1
        if calls == 2:
            raise RuntimeError("injected multi-file backup failure")
        return result

    monkeypatch.setattr(
        reconcile_amphora_source_spans,
        "_backup_database",
        fail_after_second_complete_backup,
    )
    plan = {
        "inventory_hash": "sha256:reviewed",
        "object_manifest_hash": "sha256:objects",
        "objects": [],
        "capture_raw_backfill_manifest_hash": "sha256:capture",
        "capture_raw_backfill_events": 1,
    }

    with pytest.raises(RuntimeError, match="injected multi-file backup failure"):
        if backup_kind == "reviewed":
            reconcile_amphora_source_spans._backup_reviewed_inventory(  # noqa: SLF001
                database_dir=database_dir,
                backup_dir=backup_root,
                plan=plan,
            )
        else:
            reconcile_amphora_source_spans._backup_capture_raw_inventory(  # noqa: SLF001
                database_dir=database_dir,
                backup_dir=backup_root,
                plan=plan,
            )

    assert backup_root.is_dir()
    assert list(backup_root.iterdir()) == []


def test_backup_leaf_collision_is_never_deleted_as_owned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_dir = tmp_path / "database"
    database_dir.mkdir()
    with sqlite3.connect(database_dir / "distill_queue.db") as connection:
        connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel VALUES ('reviewed')")
    backup_root = tmp_path / "backups"
    original_open = reconcile_amphora_source_spans.os.open
    collisions: list[Path] = []

    def collide(
        path: Path,
        flags: int,
        mode: int = 0o777,
        *args: object,
        **kwargs: object,
    ) -> int:
        candidate = Path(path)
        if (
            flags & reconcile_amphora_source_spans.os.O_EXCL
            and candidate.name == "distill_queue.db"
            and not collisions
        ):
            descriptor = original_open(
                candidate,
                reconcile_amphora_source_spans.os.O_CREAT
                | reconcile_amphora_source_spans.os.O_EXCL
                | reconcile_amphora_source_spans.os.O_WRONLY,
                0o600,
            )
            reconcile_amphora_source_spans.os.write(
                descriptor,
                b"foreign-backup-collision",
            )
            reconcile_amphora_source_spans.os.close(descriptor)
            collisions.append(candidate)
        return original_open(candidate, flags, mode, *args, **kwargs)

    monkeypatch.setattr(reconcile_amphora_source_spans.os, "open", collide)
    with pytest.raises(FileExistsError):
        reconcile_amphora_source_spans._backup_reviewed_inventory(  # noqa: SLF001
            database_dir=database_dir,
            backup_dir=backup_root,
            plan={
                "inventory_hash": "sha256:reviewed",
                "object_manifest_hash": "sha256:objects",
                "objects": [],
            },
        )

    assert len(collisions) == 1
    assert collisions[0].read_bytes() == b"foreign-backup-collision"


def test_failed_backup_cleanup_preserves_foreign_leaf_entry(
    isolated_amphora: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _legacy, _revisions = _linked_legacy_task(isolated_amphora)
    reviewed = reconcile_amphora_source_spans.build_source_span_reconciliation_plan(
        _config(isolated_amphora)
    )
    backup_root = isolated_amphora / "source-span-backups"
    real_normalize = reconcile_amphora_source_spans.normalize_private_sqlite_copy
    foreign_paths: list[Path] = []

    def inject_foreign_then_fail(path: Path) -> None:
        real_normalize(path)
        foreign = path.parent / "foreign-during-cleanup"
        foreign.write_bytes(b"foreign")
        foreign_paths.append(foreign)
        raise RuntimeError("injected source span backup failure")

    monkeypatch.setattr(
        reconcile_amphora_source_spans,
        "normalize_private_sqlite_copy",
        inject_foreign_then_fail,
    )
    with pytest.raises(
        RuntimeError,
        match="injected source span backup failure",
    ):
        reconcile_amphora_source_spans._backup_reviewed_inventory(  # noqa: SLF001
            database_dir=isolated_amphora,
            backup_dir=backup_root,
            plan=reviewed,
        )

    assert len(foreign_paths) == 1
    assert foreign_paths[0].read_bytes() == b"foreign"


def test_backup_rejects_replaced_manifest_before_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_dir = tmp_path / "database"
    database_dir.mkdir()
    with sqlite3.connect(database_dir / "distill_queue.db") as connection:
        connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel VALUES ('reviewed')")
    backup_root = tmp_path / "backups"
    real_fsync_directory = reconcile_amphora_source_spans.fsync_directory
    replacements: list[Path] = []

    def replace_manifest_after_sync(path: Path) -> None:
        real_fsync_directory(path)
        manifest = path / "backup_manifest.json"
        if manifest.exists() and not replacements:
            manifest.unlink()
            manifest.write_bytes(b"foreign-source-span-manifest")
            replacements.append(manifest)

    monkeypatch.setattr(
        reconcile_amphora_source_spans,
        "fsync_directory",
        replace_manifest_after_sync,
    )
    with pytest.raises(
        DurableIOError,
        match="durable_target_preimage_changed",
    ):
        reconcile_amphora_source_spans._backup_reviewed_inventory(  # noqa: SLF001
            database_dir=database_dir,
            backup_dir=backup_root,
            plan={
                "inventory_hash": "sha256:reviewed",
                "object_manifest_hash": "sha256:objects",
                "objects": [],
            },
        )

    assert len(replacements) == 1
    assert replacements[0].read_bytes() == b"foreign-source-span-manifest"


def test_source_span_backup_rejects_symlinked_root(tmp_path: Path) -> None:
    database_dir = tmp_path / "database"
    database_dir.mkdir()
    with sqlite3.connect(database_dir / "distill_queue.db") as connection:
        connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel VALUES ('reviewed')")
    external = tmp_path / "foreign-backup-root"
    external.mkdir()
    backup_link = tmp_path / "backup-link"
    backup_link.symlink_to(external, target_is_directory=True)

    with pytest.raises(RuntimeError, match="backup directory is unsafe"):
        reconcile_amphora_source_spans._backup_reviewed_inventory(  # noqa: SLF001
            database_dir=database_dir,
            backup_dir=backup_link,
            plan={
                "inventory_hash": "sha256:reviewed",
                "object_manifest_hash": "sha256:objects",
                "objects": [],
            },
        )

    assert list(external.iterdir()) == []


def test_apply_preserves_legacy_bytes_and_creates_append_only_verified_generation(
    isolated_amphora: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy, revisions = _linked_legacy_task(isolated_amphora)
    before = amphora.list_pending()[0]
    legacy_messages_path = Path(before["messages_path"])
    legacy_bytes = legacy_messages_path.read_bytes()
    reviewed = reconcile_amphora_source_spans.build_source_span_reconciliation_plan(
        _config(isolated_amphora)
    )
    monkeypatch.setattr(
        reconcile_amphora_source_spans,
        "_runtime_writers_are_inactive",
        lambda _database_dir: True,
    )

    result = reconcile_amphora_source_spans.reconcile_source_spans(
        _config(isolated_amphora),
        apply=True,
        backup_dir=isolated_amphora / "backups",
        expected_inventory_hash=reviewed["inventory_hash"],
    )

    assert result["ok"] is True, result
    assert result["status"] == "verified"
    assert result["applied"]["legacy_tasks_retired"] == 1
    assert result["applied"]["canonical_tasks_created"] == 1
    assert legacy_messages_path.read_bytes() == legacy_bytes
    with sqlite3.connect(isolated_amphora / "distill_queue.db") as conn:
        conn.row_factory = sqlite3.Row
        old = conn.execute(
            "SELECT * FROM distillation_tasks WHERE task_id=?", (legacy.task_id,)
        ).fetchone()
        migration = conn.execute("SELECT * FROM amphora_source_span_migrations").fetchone()
        new = conn.execute(
            "SELECT * FROM distillation_tasks WHERE task_id=?",
            (migration["canonical_task_id"],),
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE amphora_source_span_migrations SET created_at='changed'")
    assert old["status"] == "intentional_skip"
    assert old["terminal_reason"].startswith("superseded_by_verified_source_span_migration:")
    assert new["status"] == "pending"
    canonical_meta = json.loads(new["meta"])
    assert canonical_meta["messages_revision"] == migration["canonical_input_revision"]
    assert SYSTEM_OWNED_META_KEYS.intersection(canonical_meta) == {"messages_revision"}
    canonical_messages = json.loads(Path(new["messages_path"]).read_text(encoding="utf-8"))
    assert {message["source_span"]["revision_id"] for message in canonical_messages} == set(
        revisions
    )
    assert migration["legacy_task_id"] == legacy.task_id
    assert migration["legacy_object_hash"] == reviewed["objects"][0]["legacy_object_hash"]
    assert migration["raw_preimage_hash"] == reviewed["objects"][0]["raw_preimage_hash"]
    assert Path(result["backup"]["database"]["path"]).is_file()
    assert Path(result["backup"]["messages_manifest_path"]).is_file()

    replay = reconcile_amphora_source_spans.reconcile_source_spans(
        _config(isolated_amphora),
        apply=False,
    )
    assert replay["ok"] is True
    assert replay["missing_span_tasks"] == 0
    assert replay["verified_migrations"] == 1

    manifest_path = Path(str(result["backup"]["manifest_path"]))
    manifest_bytes = manifest_path.read_bytes()
    sentinel_manifest = isolated_amphora / "foreign-backup-manifest.json"
    sentinel_manifest.write_bytes(manifest_bytes)
    manifest_path.unlink()
    manifest_path.symlink_to(sentinel_manifest)
    unsafe_manifest = reconcile_amphora_source_spans.reconcile_source_spans(
        _config(isolated_amphora),
        apply=False,
    )
    assert unsafe_manifest["ok"] is False
    assert unsafe_manifest["verified_migrations"] == 0
    assert unsafe_manifest["blocked_by_reason"]["migration_backup_manifest_missing"] == 1
    manifest_path.unlink()
    manifest_path.write_bytes(manifest_bytes)

    with sqlite3.connect(isolated_amphora / "distill_queue.db") as conn:
        meta = json.loads(
            conn.execute(
                "SELECT meta FROM distillation_tasks WHERE task_id=?",
                (migration["canonical_task_id"],),
            ).fetchone()[0]
        )
        meta["messages_revision"] = "forged-stale-revision"
        conn.execute(
            "UPDATE distillation_tasks SET meta=? WHERE task_id=?",
            (
                json.dumps(meta, ensure_ascii=False),
                migration["canonical_task_id"],
            ),
        )
    corrupted = reconcile_amphora_source_spans.reconcile_source_spans(
        _config(isolated_amphora),
        apply=False,
    )
    assert corrupted["ok"] is False
    assert corrupted["verified_migrations"] == 0
    assert corrupted["blocked_by_reason"]["migration_task_binding_mismatch"] == 1


def test_source_span_planner_never_follows_symlinked_legacy_messages(
    isolated_amphora: Path,
) -> None:
    legacy, _revisions = _linked_legacy_task(isolated_amphora)
    task = next(item for item in amphora.list_pending() if item["task_id"] == legacy.task_id)
    messages_path = Path(task["messages_path"])
    sentinel = isolated_amphora / "foreign-legacy-messages.json"
    sentinel.write_bytes(messages_path.read_bytes())
    messages_path.unlink()
    messages_path.symlink_to(sentinel)

    reviewed = reconcile_amphora_source_spans.build_source_span_reconciliation_plan(
        _config(isolated_amphora)
    )

    assert reviewed["ok"] is False
    assert reviewed["candidate_tasks"] == 0
    assert reviewed["blocked_by_reason"]["legacy_messages_invalid"] == 1
    assert sentinel.read_bytes()


def test_source_span_backup_never_follows_replaced_legacy_message_asset(
    isolated_amphora: Path,
) -> None:
    legacy, _revisions = _linked_legacy_task(isolated_amphora)
    reviewed = reconcile_amphora_source_spans.build_source_span_reconciliation_plan(
        _config(isolated_amphora)
    )
    item = reviewed["objects"][0]
    messages_path = Path(str(item["messages_path"]))
    sentinel = isolated_amphora / "foreign-backup-source.json"
    sentinel.write_bytes(messages_path.read_bytes())
    messages_path.unlink()
    messages_path.symlink_to(sentinel)
    backup_root = isolated_amphora / "backups"

    with pytest.raises(
        ValueError,
        match="source span messages asset (?:is outside owner|is unsafe)",
    ):
        reconcile_amphora_source_spans._backup_reviewed_inventory(  # noqa: SLF001
            database_dir=isolated_amphora,
            backup_dir=backup_root,
            plan=reviewed,
        )

    assert backup_root.is_dir()
    assert list(backup_root.iterdir()) == []
    assert sentinel.read_bytes()


def test_existing_canonical_generation_unreadable_messages_never_equal_empty(
    isolated_amphora: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy, _revisions = _linked_legacy_task(isolated_amphora)
    reviewed = reconcile_amphora_source_spans.build_source_span_reconciliation_plan(
        _config(isolated_amphora)
    )
    item = reviewed["objects"][0]
    assert not SYSTEM_OWNED_META_KEYS.intersection(item["canonical_meta"])
    existing = amphora.enqueue_with_receipt(
        str(item["session_id"]),
        list(item["canonical_messages"]),
        {
            **dict(item["canonical_meta"]),
            "source": str(item["source_agent"]),
            "input_revision": str(item["canonical_input_revision"]),
        },
    )
    assert existing.task_id == item["canonical_task_id"]
    existing_task = next(
        task for task in amphora.list_pending() if task["task_id"] == existing.task_id
    )
    messages_path = Path(existing_task["messages_path"])
    before = messages_path.read_bytes()
    manifest = isolated_amphora / "reviewed-backup-manifest.json"
    manifest.write_text('{"reviewed":true}\n', encoding="utf-8")
    manifest_file_hash = "sha256:" + hashlib.sha256(manifest.read_bytes()).hexdigest()
    original_secure_read_bytes = amphora.secure_read_bytes

    def denied(root: Path, relative_path: str | Path):
        if (Path(root) / Path(relative_path)).absolute() == messages_path.absolute():
            raise PermissionError("sentinel")
        return original_secure_read_bytes(root, relative_path)

    monkeypatch.setattr(amphora, "secure_read_bytes", denied)

    with pytest.raises(
        amphora.AmphoraTaskPayloadUnavailableError,
        match="amphora_task_messages_unreadable",
    ):
        apply_source_span_migrations(
            objects=reviewed["objects"],
            inventory_hash=reviewed["inventory_hash"],
            backup_manifest_path=manifest,
            backup_manifest_hash="sha256:reviewed",
            backup_manifest_file_hash=manifest_file_hash,
        )

    assert messages_path.read_bytes() == before
    with sqlite3.connect(isolated_amphora / "distill_queue.db") as conn:
        assert conn.execute(
            "SELECT status FROM distillation_tasks WHERE task_id=?",
            (legacy.task_id,),
        ).fetchone() == ("pending",)
        assert conn.execute("SELECT COUNT(*) FROM amphora_source_span_migrations").fetchone() == (
            0,
        )


def test_source_span_apply_strips_forged_system_metadata_defense_in_depth(
    isolated_amphora: Path,
) -> None:
    _legacy, _revisions = _linked_legacy_task(isolated_amphora)
    reviewed = reconcile_amphora_source_spans.build_source_span_reconciliation_plan(
        _config(isolated_amphora)
    )
    item = dict(reviewed["objects"][0])
    canonical_meta = dict(item["canonical_meta"])
    canonical_meta.update(
        {
            "messages_revision": "forged-stale-revision",
            "terminal_receipt_outbox": {"status": "committed"},
            "failed_terminal_receipt_outbox": {"status": "committed"},
            "message_cleanup_outbox": {"status": "committed"},
        }
    )
    item["canonical_meta"] = canonical_meta
    manifest = isolated_amphora / "reviewed-backup-manifest.json"
    manifest.write_text('{"reviewed":true}\n', encoding="utf-8")
    manifest_file_hash = "sha256:" + hashlib.sha256(manifest.read_bytes()).hexdigest()

    applied = apply_source_span_migrations(
        objects=[item],
        inventory_hash=reviewed["inventory_hash"],
        backup_manifest_path=manifest,
        backup_manifest_hash="sha256:reviewed",
        backup_manifest_file_hash=manifest_file_hash,
    )

    assert applied["canonical_tasks_created"] == 1
    with sqlite3.connect(isolated_amphora / "distill_queue.db") as conn:
        row = conn.execute(
            "SELECT input_revision, meta FROM distillation_tasks WHERE task_id=?",
            (str(item["canonical_task_id"]),),
        ).fetchone()
    meta = json.loads(row[1])
    assert row[0] == item["canonical_input_revision"]
    assert meta["messages_revision"] == item["canonical_input_revision"]
    assert SYSTEM_OWNED_META_KEYS.intersection(meta) == {"messages_revision"}


def test_source_span_rollback_never_unlinks_replaced_canonical_message(
    isolated_amphora: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _legacy, _revisions = _linked_legacy_task(isolated_amphora)
    reviewed = reconcile_amphora_source_spans.build_source_span_reconciliation_plan(
        _config(isolated_amphora)
    )
    item = reviewed["objects"][0]
    manifest = isolated_amphora / "reviewed-backup-manifest.json"
    manifest.write_text('{"reviewed":true}\n', encoding="utf-8")
    manifest_file_hash = "sha256:" + hashlib.sha256(manifest.read_bytes()).hexdigest()
    with sqlite3.connect(isolated_amphora / "distill_queue.db") as connection:
        connection.execute("""
            CREATE TRIGGER reject_source_span_migration_replacement
            BEFORE INSERT ON amphora_source_span_migrations
            BEGIN
                SELECT RAISE(ABORT, 'injected source span receipt failure');
            END
            """)
    original_write = amphora._write_messages
    canonical_path = amphora._messages_dir() / f"{item['canonical_task_id']}.json"

    def replace_after_publish(task_id, messages, **kwargs):
        publication = original_write(task_id, messages, **kwargs)
        path = getattr(publication, "path", publication)
        path.unlink()
        path.write_bytes(b"foreign-source-span-replacement")
        return publication

    monkeypatch.setattr(amphora, "_write_messages", replace_after_publish)
    with pytest.raises(
        DurableIOError,
        match="durable_target_preimage_changed",
    ):
        apply_source_span_migrations(
            objects=reviewed["objects"],
            inventory_hash=reviewed["inventory_hash"],
            backup_manifest_path=manifest,
            backup_manifest_hash="sha256:reviewed",
            backup_manifest_file_hash=manifest_file_hash,
        )

    assert canonical_path.read_bytes() == b"foreign-source-span-replacement"


def test_source_span_rejects_replaced_canonical_message_before_commit(
    isolated_amphora: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy, _revisions = _linked_legacy_task(isolated_amphora)
    reviewed = reconcile_amphora_source_spans.build_source_span_reconciliation_plan(
        _config(isolated_amphora)
    )
    item = reviewed["objects"][0]
    manifest = isolated_amphora / "reviewed-backup-manifest.json"
    manifest.write_text('{"reviewed":true}\n', encoding="utf-8")
    manifest_file_hash = "sha256:" + hashlib.sha256(manifest.read_bytes()).hexdigest()
    original_write = amphora._write_messages
    canonical_path = amphora._messages_dir() / f"{item['canonical_task_id']}.json"

    def replace_after_publish(task_id, messages, **kwargs):
        publication = original_write(task_id, messages, **kwargs)
        path = getattr(publication, "path", publication)
        path.unlink()
        path.write_bytes(b"foreign-source-span-before-commit")
        return publication

    monkeypatch.setattr(amphora, "_write_messages", replace_after_publish)
    with pytest.raises(
        DurableIOError,
        match="durable_target_preimage_changed",
    ):
        apply_source_span_migrations(
            objects=reviewed["objects"],
            inventory_hash=reviewed["inventory_hash"],
            backup_manifest_path=manifest,
            backup_manifest_hash="sha256:reviewed",
            backup_manifest_file_hash=manifest_file_hash,
        )

    with sqlite3.connect(isolated_amphora / "distill_queue.db") as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM distillation_tasks WHERE task_id=?",
                (item["canonical_task_id"],),
            ).fetchone()
            is None
        )
        assert connection.execute(
            "SELECT status FROM distillation_tasks WHERE task_id=?",
            (legacy.task_id,),
        ).fetchone() == ("pending",)
    assert canonical_path.read_bytes() == b"foreign-source-span-before-commit"


def test_uninspectable_unowned_canonical_message_path_is_never_overwritten(
    isolated_amphora: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy, _revisions = _linked_legacy_task(isolated_amphora)
    reviewed = reconcile_amphora_source_spans.build_source_span_reconciliation_plan(
        _config(isolated_amphora)
    )
    item = reviewed["objects"][0]
    target = amphora._messages_dir() / f"{item['canonical_task_id']}.json"
    target.write_bytes(b"unowned-preexisting")
    manifest = isolated_amphora / "reviewed-backup-manifest.json"
    manifest.write_text('{"reviewed":true}\n', encoding="utf-8")
    manifest_file_hash = "sha256:" + hashlib.sha256(manifest.read_bytes()).hexdigest()
    original_lstat = Path.lstat

    def denied(path: Path, *args: object, **kwargs: object):
        if path == target:
            raise PermissionError("sentinel")
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", denied)

    with pytest.raises(
        RuntimeError,
        match="amphora_message_path_unavailable",
    ):
        apply_source_span_migrations(
            objects=reviewed["objects"],
            inventory_hash=reviewed["inventory_hash"],
            backup_manifest_path=manifest,
            backup_manifest_hash="sha256:reviewed",
            backup_manifest_file_hash=manifest_file_hash,
        )

    assert target.read_bytes() == b"unowned-preexisting"
    with sqlite3.connect(isolated_amphora / "distill_queue.db") as conn:
        assert conn.execute(
            "SELECT status FROM distillation_tasks WHERE task_id=?",
            (legacy.task_id,),
        ).fetchone() == ("pending",)
        assert conn.execute(
            "SELECT COUNT(*) FROM distillation_tasks WHERE task_id=?",
            (str(item["canonical_task_id"]),),
        ).fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM amphora_source_span_migrations").fetchone() == (
            0,
        )
