# -*- coding: utf-8 -*-

from __future__ import annotations

from argparse import Namespace
import ast
import hashlib
from itertools import permutations
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
from unittest.mock import patch

import pytest

from core.app.raw_search import (
    raw_index_content_state,
    raw_index_projection_snapshot,
)
from core.sync_framework.native_raw_contract_ledger import NativeRawContractLedger
from core.sync_framework.raw_event_store import RawEventStore
from scripts import audit_raw_projection_backups as backup_audit
from scripts import project_raw_vault as prv
from scripts import raw_projection_contract as projection_contract
from scripts import raw_projection_plan_runtime as plan_runtime
from scripts import raw_projection_secure_io as secure_io


class _Cfg:
    def __init__(self, database_dir: Path):
        self.database_dir = database_dir

    def get(self, key, default=None):  # noqa: ARG002
        return default


def test_raw_projection_planning_has_no_lossy_authoritative_path_predicates() -> None:
    violations: list[str] = []
    for module in (prv, projection_contract):
        module_path = Path(str(module.__file__ or ""))
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {
                    "exists",
                    "is_file",
                    "is_dir",
                    "is_symlink",
                }
            ):
                violations.append(
                    f"{module_path.name}:{node.lineno}:{node.func.attr}"
                )

    assert violations == []


def _planned_stats(
    raw_dir: Path,
    store,
    chunks,
    db_path: Path,
    *,
    backup_dir: Path | None = None,
) -> dict:
    return {
        "raw_dir": str(raw_dir),
        "db_path": str(db_path),
        "projection_plan": prv.build_projection_plan(
            raw_dir,
            store,
            chunks,
            db_path=db_path,
            max_turn_chars=0,
            backup_dir=backup_dir,
        ),
    }


def _resign_plan(plan: dict) -> None:
    unsigned = {key: value for key, value in plan.items() if key != "plan_hash"}
    plan["plan_hash"] = prv._sha256_text(  # noqa: SLF001
        json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _ref(
    *,
    event_id: str,
    session_id: str,
    turn_number: int = 0,
    source_agent: str = "codex",
    reference_count: int = 0,
    hit_count: int = 0,
    result_count: int = 0,
    survival_score: float = 0.0,
    freshness_score: float = 0.0,
    pinned: int = 0,
) -> prv.TurnRef:
    return prv.TurnRef(
        event_id=event_id,
        source_agent=source_agent,
        session_id=session_id,
        turn_number=turn_number,
        conversation_at=f"2026-06-28T10:{turn_number:02d}:00",
        captured_at=f"2026-06-28T10:{turn_number:02d}:01",
        completeness_status="complete",
        search_count=0,
        result_count=result_count,
        hit_count=hit_count,
        view_count=0,
        reference_count=reference_count,
        freshness_score=freshness_score,
        confidence=1.0,
        survival_score=survival_score,
        pinned=pinned,
        retention_state="active",
    )


def test_projection_plan_rejects_caller_shaped_partial_chunk_denominator(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        for turn_number in range(2):
            store.upsert_turn(
                source_agent="codex",
                session_id="partial-plan-denominator",
                turn_number=turn_number,
                user_content=f"user {turn_number}",
                assistant_content=f"assistant {turn_number}",
            )
        refs = prv._fetch_refs(store)  # noqa: SLF001
        partial_chunks = prv.build_projection_chunks(
            refs,
            chunk_turns=1,
            max_chunks=1,
        )

        assert len(refs) == 2
        assert len(partial_chunks) == 1
        with pytest.raises(
            RuntimeError,
            match="complete canonical Raw denominator",
        ):
            prv.build_projection_plan(
                raw_dir,
                store,
                partial_chunks,
                db_path=db_path,
                max_turn_chars=0,
            )
    finally:
        store.close()


def test_build_projection_chunks_prioritizes_reference_hit_then_survival() -> None:
    refs = [
        _ref(event_id="fresh", session_id="fresh", survival_score=99.0, freshness_score=1.0),
        _ref(event_id="hit", session_id="hit", hit_count=10, survival_score=10.0),
        _ref(event_id="ref", session_id="ref", reference_count=1, survival_score=1.0),
    ]

    chunks = prv.build_projection_chunks(refs, chunk_turns=5, max_chunks=2)

    assert [chunk.session_id for chunk in chunks] == ["ref", "hit"]


def test_partial_capture_lowers_projection_priority(tmp_path: Path) -> None:
    store = RawEventStore(db_path=tmp_path / "raw_events.db", config=_Cfg(tmp_path))
    try:
        current = store.upsert_turn(
            source_agent="codex",
            session_id="degraded",
            turn_number=0,
            user_content="partial user",
            assistant_content="partial assistant",
            completeness={
                "visible_text": "full",
                "truncated": True,
                "loss_reasons": ["native_stream_ended"],
            },
        )
        other = store.upsert_turn(
            source_agent="kimi",
            session_id="healthy",
            turn_number=0,
            user_content="healthy user",
            assistant_content="healthy assistant",
            completeness={"visible_text": "full", "truncated": False},
        )
        refs = prv._fetch_refs(store)  # noqa: SLF001
        chunks = prv.build_projection_chunks(refs, chunk_turns=1, max_chunks=1)
    finally:
        store.close()

    by_event = {ref.event_id: ref for ref in refs}
    assert by_event[current].completeness_status == "partial"
    assert by_event[current].confidence == 0.4
    assert by_event[current].survival_score == 0.4
    assert by_event[other].survival_score == 1.0
    assert [chunk.session_id for chunk in chunks] == ["healthy"]


def test_projection_excludes_latest_nonconforming_native_contract(tmp_path: Path) -> None:
    store = RawEventStore(db_path=tmp_path / "raw_events.db", config=_Cfg(tmp_path))
    try:
        healthy = store.upsert_turn(
            source_agent="opencode",
            session_id="projection-contract",
            turn_number=0,
            user_content="healthy user",
            assistant_content="healthy assistant",
            metadata={"native_event_id": "healthy-native"},
        )
        quarantined = store.upsert_turn(
            source_agent="opencode",
            session_id="projection-contract",
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

        refs = prv._fetch_refs(store, include_eligible_delete=True)  # noqa: SLF001
        assert [ref.event_id for ref in refs] == [healthy]
    finally:
        store.close()


def test_default_projection_has_no_fixed_file_cap() -> None:
    args = prv.build_parser().parse_args([])

    assert args.max_files == 0
    assert args.max_turn_chars == 0


def test_canonical_plan_rejects_nonzero_max_files_even_when_denominator_fits(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    writer = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        writer.upsert_turn(
            source_agent="codex",
            session_id="canonical-max-files",
            turn_number=0,
            user_content="one current row",
            assistant_content="still cannot authorize a canonical cap",
        )
    finally:
        writer.close()

    args = Namespace(
        raw_dir=str(raw_dir),
        db_path=str(db_path),
        backup_dir="",
        max_files=1,
        chunk_turns=5,
        max_turn_chars=0,
        include_eligible_delete=False,
    )
    with pytest.raises(
        ValueError,
        match="canonical Raw projection requires --max-files=0",
    ):
        prv.plan_projection(args)

    assert not raw_dir.exists()


def test_projection_plan_is_read_only_and_rejects_missing_canonical_raw(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "missing" / "raw_events.db"
    raw_dir = tmp_path / "missing" / "raw"
    args = Namespace(
        raw_dir=str(raw_dir),
        db_path=str(db_path),
        backup_dir="",
        max_files=0,
        chunk_turns=5,
        max_turn_chars=0,
        include_eligible_delete=False,
    )

    with pytest.raises(ValueError, match="canonical raw database is missing"):
        prv.plan_projection(args)

    assert not db_path.exists()
    assert not db_path.parent.exists()
    assert not raw_dir.exists()


def test_projection_plan_does_not_label_uninspectable_canonical_raw_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.ops.durable_io import DurableIOError

    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    args = Namespace(
        raw_dir=str(raw_dir),
        db_path=str(db_path),
        backup_dir="",
        max_files=0,
        chunk_turns=5,
        max_turn_chars=0,
        include_eligible_delete=False,
    )
    original_stat = Path.stat

    def denied(path: Path, *args: object, **kwargs: object):
        if path == db_path:
            raise PermissionError("sentinel")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)

    with pytest.raises(DurableIOError, match="durable_path_inspection_failed"):
        prv.plan_projection(args)


def test_projection_recovery_does_not_treat_uninspectable_root_as_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.ops.durable_io import DurableIOError

    raw_dir = tmp_path / "raw"
    original_stat = Path.stat

    def denied(path: Path, *args: object, **kwargs: object):
        if path == raw_dir:
            raise PermissionError("sentinel")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)

    with pytest.raises(DurableIOError, match="durable_path_inspection_failed"):
        prv.recover_interrupted_projection(raw_dir)


def test_projection_target_inspection_failure_never_becomes_safe_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    component = raw_dir / "codex"
    original_lstat = Path.lstat

    def denied(path: Path, *args: object, **kwargs: object):
        if path == component:
            raise PermissionError("sentinel")
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", denied)

    with pytest.raises(ValueError, match="inspection is unavailable"):
        prv._safe_projection_target(  # noqa: SLF001
            raw_dir,
            "codex/session.md",
        )


def test_uninspectable_projection_journal_never_becomes_empty_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    journal = raw_dir / prv.PROJECTION_JOURNAL_NAME
    original_lstat = Path.lstat

    def denied(path: Path, *args: object, **kwargs: object):
        if path == journal:
            raise PermissionError("sentinel")
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", denied)

    with pytest.raises(ValueError, match="inspection is unavailable"):
        prv._load_projection_journal(raw_dir)  # noqa: SLF001


def test_unreadable_managed_projection_file_never_becomes_unmanaged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    managed = raw_dir / "managed.md"
    managed.write_text(
        "---\nmnemos_type: raw_retention_projection\n---\n",
        encoding="utf-8",
    )
    original_read_native_bytes = prv.read_native_bytes

    def denied(path: Path):
        if path == managed:
            raise PermissionError("sentinel")
        return original_read_native_bytes(path)

    monkeypatch.setattr(prv, "read_native_bytes", denied)

    with pytest.raises(
        ValueError,
        match="managed Raw projection file is unreadable",
    ):
        prv._managed_projection_paths(raw_dir)  # noqa: SLF001


def test_invalid_utf8_projection_marker_never_grants_delete_ownership(
    tmp_path: Path,
) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    managed = raw_dir / "managed.md"
    payload = (
        b"---\nmnemos_type: raw_retention_projection\n---\n"
        b"valid-prefix\n\xffcorrupt\n"
    )
    managed.write_bytes(payload)

    with pytest.raises(
        ValueError,
        match="managed Raw projection file is not valid UTF-8",
    ):
        prv._managed_projection_paths(raw_dir)  # noqa: SLF001

    with pytest.raises(
        RuntimeError,
        match="Raw projection stale target is not valid UTF-8",
    ):
        secure_io._secure_delete_managed_file(  # noqa: SLF001
            raw_dir,
            managed.name,
            expected_hash=hashlib.sha256(payload).hexdigest(),
        )
    assert managed.read_bytes() == payload


def test_managed_probe_tolerates_multibyte_char_straddling_prefix_boundary(
    tmp_path: Path,
) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    # 4095 ASCII bytes + first byte of a 3-byte UTF-8 char at offset 4095:
    # slicing the probe at 4096 bytes cuts the char in half.
    plain = raw_dir / "plain.md"
    plain.write_bytes(b"a" * 4095 + "中".encode("utf-8") + b"tail\n")
    assert prv._managed_projection_paths(raw_dir) == set()  # noqa: SLF001

    marker = b"---\nmnemos_type: raw_retention_projection\n---\n"
    managed = raw_dir / "managed.md"
    managed.write_bytes(
        marker + b"b" * (4096 - len(marker) - 1) + "中".encode("utf-8") + b"tail\n"
    )
    assert prv._managed_projection_paths(raw_dir) == {"managed.md"}  # noqa: SLF001


def test_secure_delete_managed_file_tolerates_multibyte_prefix_boundary(
    tmp_path: Path,
) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    marker = b"---\nmnemos_type: raw_retention_projection\n---\n"
    stale = raw_dir / "stale.md"
    payload = (
        marker + b"c" * (4096 - len(marker) - 1) + "中".encode("utf-8") + b"tail\n"
    )
    stale.write_bytes(payload)

    deleted = secure_io._secure_delete_managed_file(  # noqa: SLF001
        raw_dir,
        stale.name,
        expected_hash=hashlib.sha256(payload).hexdigest(),
    )
    assert deleted is True
    assert not stale.exists()


def test_projection_epoch_never_treats_uninspectable_wal_as_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.ops.durable_io import DurableIOError

    db_path = tmp_path / "raw_events.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE raw_probe (value TEXT)")

    def denied(_paths):
        raise DurableIOError("durable_path_inspection_failed")

    monkeypatch.setattr(prv, "physical_scope_signature", denied)

    with pytest.raises(
        DurableIOError,
        match="canonical_raw_epoch_inspection_failed",
    ):
        prv.ReadOnlyProjectionSource(db_path)


def test_projection_plan_preserves_existing_raw_database_epoch(tmp_path: Path) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    writer = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        writer.upsert_turn(
            source_agent="codex",
            session_id="read-only-plan",
            turn_number=0,
            user_content="hello",
            assistant_content="world",
        )
    finally:
        writer.close()

    def epoch() -> dict[str, tuple[int, int, int, str] | None]:
        result: dict[str, tuple[int, int, int, str] | None] = {}
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{db_path}{suffix}")
            if not path.exists():
                result[suffix] = None
                continue
            stat = path.stat()
            result[suffix] = (
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_ctime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        return result

    before = epoch()
    args = Namespace(
        raw_dir=str(raw_dir),
        db_path=str(db_path),
        backup_dir="",
        max_files=0,
        chunk_turns=5,
        max_turn_chars=0,
        include_eligible_delete=False,
    )
    source, _chunks, _stats = prv.plan_projection(args)
    source.close()

    assert epoch() == before
    assert not raw_dir.exists()


def test_projection_plan_never_cleans_transaction_recovery_state(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    writer = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        writer.upsert_turn(
            source_agent="codex",
            session_id="read-only-transaction-inspection",
            turn_number=0,
            user_content="inspect only",
            assistant_content="never recover during dry run",
        )
    finally:
        writer.close()
    raw_dir.mkdir()
    tombstone = raw_dir / (
        f"{prv.PROJECTION_TRANSACTION_TOMBSTONE_PREFIX}{'a' * 32}"
    )
    debris = tombstone / "new" / "codex" / "staged.md"
    debris.parent.mkdir(parents=True)
    debris.write_text(
        "---\nmnemos_type: raw_retention_projection\n---\nkeep",
        encoding="utf-8",
    )
    args = Namespace(
        raw_dir=str(raw_dir),
        db_path=str(db_path),
        backup_dir="",
        max_files=0,
        chunk_turns=1,
        max_turn_chars=0,
        include_eligible_delete=False,
    )

    source, _chunks, stats = prv.plan_projection(args)
    source.close()
    assert debris.read_text(encoding="utf-8").endswith("keep")
    plan = stats["projection_plan"]
    assert all(
        prv.PROJECTION_TRANSACTION_TOMBSTONE_PREFIX not in path
        for field in (
            "previously_managed_paths",
            "changed_paths",
            "unchanged_paths",
            "stale_paths",
        )
        for path in plan[field]
    )

    transaction_dir = raw_dir / prv.PROJECTION_TRANSACTION_DIR
    transaction_dir.mkdir()
    prv._write_projection_transaction_state(  # noqa: SLF001
        transaction_dir,
        status="preparing",
        plan_hash="b" * 64,
        generation_hash="c" * 64,
        manifest_hash="",
    )
    state_before = (transaction_dir / "state.json").read_bytes()
    with pytest.raises(RuntimeError, match="requires explicit recovery"):
        prv.plan_projection(args)
    assert (transaction_dir / "state.json").read_bytes() == state_before
    assert debris.read_text(encoding="utf-8").endswith("keep")


def test_projection_plan_rejects_missing_current_revision_in_denominator(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    writer = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        revision_id = writer.upsert_turn(
            source_agent="codex",
            session_id="missing-current",
            turn_number=0,
            user_content="must not disappear",
            assistant_content="from the plan",
        )
        logical_event_id = writer.get_turn(revision_id)["logical_event_id"]
        connection = writer._pool.get_conn()  # noqa: SLF001
        connection.execute(
            "UPDATE raw_turns SET current_revision_id=NULL WHERE event_id=?",
            (logical_event_id,),
        )
        connection.commit()
    finally:
        writer.close()
    args = Namespace(
        raw_dir=str(tmp_path / "raw"),
        db_path=str(db_path),
        backup_dir="",
        max_files=0,
        chunk_turns=1,
        max_turn_chars=0,
        include_eligible_delete=False,
    )

    with pytest.raises(ValueError, match="current revision identity is invalid"):
        prv.plan_projection(args)


def test_projection_plan_fails_closed_on_duplicate_journal_keys(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    writer = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        writer.upsert_turn(
            source_agent="codex",
            session_id="journal-duplicate",
            turn_number=0,
            user_content="journal",
            assistant_content="must be authoritative",
        )
        chunks = prv.build_projection_chunks(
            prv._fetch_refs(writer),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )
        prv.write_projection(
            raw_dir,
            writer,
            chunks,
            db_path=db_path,
            max_turn_chars=0,
        )
    finally:
        writer.close()
    journal_path = raw_dir / prv.PROJECTION_JOURNAL_NAME
    valid = json.loads(journal_path.read_text(encoding="utf-8"))
    journal_path.write_text(
        (
            '{"schema_version":"mnemos.raw_projection.v2",'
            '"projection_contract":"lossless-visible-v1",'
            f'"generation_hash":{json.dumps(valid["generation_hash"])},'
            f'"files":{json.dumps(valid["files"])},'
            f'"files":{json.dumps(valid["files"])}}}'
        ),
        encoding="utf-8",
    )
    before = journal_path.read_bytes()
    args = Namespace(
        raw_dir=str(raw_dir),
        db_path=str(db_path),
        backup_dir="",
        max_files=0,
        chunk_turns=1,
        max_turn_chars=0,
        include_eligible_delete=False,
    )

    with pytest.raises(ValueError, match="projection journal"):
        source, _chunks, _stats = prv.plan_projection(args)
        source.close()
    assert journal_path.read_bytes() == before


def test_projection_rejects_symlinked_parent_before_write_or_stale_delete(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.md"
    victim.write_text(
        "---\nmnemos_type: raw_retention_projection\n---\noutside\n",
        encoding="utf-8",
    )
    raw_dir.mkdir()
    (raw_dir / "escape").symlink_to(outside, target_is_directory=True)
    revision_id = "rawrev-" + "b" * 40
    files = {
        "escape/victim.md": {
            "content_hash": hashlib.sha256(victim.read_bytes()).hexdigest(),
            "logical_event_ids": ["a" * 32],
            "revision_ids": [revision_id],
            "revision_set_hash": prv._sha256_text(  # noqa: SLF001
                json.dumps([revision_id], ensure_ascii=False, separators=(",", ":"))
            ),
        }
    }
    journal = {
        "schema_version": "mnemos.raw_projection.v2",
        "projection_contract": prv.PROJECTION_CONTRACT,
        "generation_hash": prv._sha256_text(  # noqa: SLF001
            json.dumps(
                files,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        ),
        "files": files,
    }
    (raw_dir / prv.PROJECTION_JOURNAL_NAME).write_text(
        json.dumps(journal, ensure_ascii=False),
        encoding="utf-8",
    )
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        with pytest.raises(ValueError, match="unsafe"):
            prv.write_projection(
                raw_dir,
                store,
                [],
                db_path=db_path,
                max_turn_chars=0,
            )
    finally:
        store.close()
    assert victim.read_text(encoding="utf-8").endswith("outside\n")


def test_projection_rejects_symlinked_generated_parent_before_external_write(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    outside = tmp_path / "outside"
    outside.mkdir()
    raw_dir.mkdir()
    (raw_dir / "codex").symlink_to(outside, target_is_directory=True)
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="escape",
            turn_number=0,
            user_content="must stay inside",
            assistant_content="safe",
        )
        chunks = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )
        with pytest.raises(ValueError, match="unsafe"):
            prv.write_projection(
                raw_dir,
                store,
                chunks,
                db_path=db_path,
                max_turn_chars=0,
            )
    finally:
        store.close()
    assert list(outside.rglob("*.md")) == []


def test_projection_plan_freezes_exact_write_set_and_rejects_target_drift(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    writer = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        writer.upsert_turn(
            source_agent="codex",
            session_id="frozen-plan",
            turn_number=0,
            user_content="planned",
            assistant_content="bytes",
        )
    finally:
        writer.close()
    args = Namespace(
        raw_dir=str(raw_dir),
        db_path=str(db_path),
        backup_dir="",
        max_files=0,
        chunk_turns=1,
        max_turn_chars=0,
        include_eligible_delete=False,
    )
    source, chunks, stats = prv.plan_projection(args)
    try:
        plan = stats["projection_plan"]
        assert plan["schema_version"] == "mnemos.raw_projection_plan.v2"
        assert len(plan["changed_paths"]) == 1
        assert plan["index_changed_paths"] == plan["changed_paths"]
        assert plan["journal_write"] is True
        assert plan["write_set_empty"] is False

        target = raw_dir / plan["changed_paths"][0]
        target.parent.mkdir(parents=True)
        target.write_text("drift after plan", encoding="utf-8")

        with pytest.raises(RuntimeError, match="preconditions changed"):
            prv.apply_projection(args, source, chunks, stats)
        assert target.read_text(encoding="utf-8") == "drift after plan"
    finally:
        source.close()


def test_apply_rejects_tampered_plan_before_any_projection_effect(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    writer = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        writer.upsert_turn(
            source_agent="codex",
            session_id="tampered-plan",
            turn_number=0,
            user_content="do not publish",
            assistant_content="from a forged plan",
        )
    finally:
        writer.close()
    args = Namespace(
        raw_dir=str(raw_dir),
        db_path=str(db_path),
        backup_dir="",
        max_files=0,
        chunk_turns=1,
        max_turn_chars=0,
        include_eligible_delete=False,
    )
    source, chunks, stats = prv.plan_projection(args)
    try:
        forged = dict(stats["projection_plan"])
        forged["plan_hash"] = "f" * 64
        assert forged["plan_hash"] != stats["projection_plan"]["plan_hash"]
        with pytest.raises(RuntimeError, match="malformed or tampered"):
            prv.validate_projection_plan(forged)
        assert not raw_dir.exists()
    finally:
        source.close()


def test_validator_rejects_self_signed_empty_write_set_forgery(tmp_path: Path) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    writer = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        writer.upsert_turn(
            source_agent="codex",
            session_id="self-signed-forgery",
            turn_number=0,
            user_content="must be published",
            assistant_content="cannot be caller-skipped",
        )
        chunks = prv.build_projection_chunks(
            prv._fetch_refs(writer),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )
        plan = prv.build_projection_plan(
            raw_dir,
            writer,
            chunks,
            db_path=db_path,
            max_turn_chars=0,
        )
    finally:
        writer.close()

    forged = dict(plan)
    forged["write_set_empty"] = True
    _resign_plan(forged)

    with pytest.raises(RuntimeError, match="malformed or tampered"):
        prv.validate_projection_plan(forged)


def test_projection_root_symlink_is_rejected_before_external_effect(tmp_path: Path) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    outside = tmp_path / "outside"
    outside.mkdir()
    raw_dir.symlink_to(outside, target_is_directory=True)
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="root-symlink",
            turn_number=0,
            user_content="inside only",
            assistant_content="never outside",
        )
        chunks = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )
        with pytest.raises(ValueError, match="root is unsafe"):
            prv.write_projection(
                raw_dir,
                store,
                chunks,
                db_path=db_path,
                max_turn_chars=0,
            )
    finally:
        store.close()
    assert list(outside.iterdir()) == []


def test_projection_root_ancestor_symlink_is_rejected_before_external_effect(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)
    raw_dir = linked_parent / "raw"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="ancestor-symlink",
            turn_number=0,
            user_content="inside only",
            assistant_content="never follow an ancestor link",
        )
        chunks = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )
        with pytest.raises(ValueError, match="root is unsafe"):
            prv.write_projection(
                raw_dir,
                store,
                chunks,
                db_path=db_path,
                max_turn_chars=0,
            )
    finally:
        store.close()
    assert list(outside.iterdir()) == []


def test_target_drift_after_staging_is_preserved_fail_closed(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="target-drift",
            turn_number=0,
            user_content="planned bytes",
            assistant_content="must not overwrite concurrent bytes",
        )
        chunks = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )
        target = prv._chunk_path(raw_dir, chunks[0])  # noqa: SLF001
        calls = {"count": 0}

        def inject_concurrent_target() -> None:
            calls["count"] += 1
            if calls["count"] == 1:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("concurrent owner bytes", encoding="utf-8")

        with pytest.raises(
            RuntimeError,
            match="target preimage changed before publish",
        ):
            prv.write_projection(
                raw_dir,
                store,
                chunks,
                db_path=db_path,
                max_turn_chars=0,
                before_publish=inject_concurrent_target,
            )
        assert target.read_text(encoding="utf-8") == "concurrent owner bytes"
        assert (raw_dir / prv.PROJECTION_TRANSACTION_DIR).is_dir()
    finally:
        store.close()


def test_post_publish_source_epoch_drift_retains_plan_bound_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    writer = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        writer.upsert_turn(
            source_agent="codex",
            session_id="post-publish-epoch",
            turn_number=0,
            user_content="frozen source",
            assistant_content="rollback if source moves",
        )
    finally:
        writer.close()
    args = Namespace(
        raw_dir=str(raw_dir),
        db_path=str(db_path),
        backup_dir="",
        max_files=0,
        chunk_turns=1,
        max_turn_chars=0,
        include_eligible_delete=False,
    )
    source, chunks, stats = prv.plan_projection(args)
    real_write_projection = prv.write_projection

    def publish_then_mutate_source(*write_args, **write_kwargs):
        result = real_write_projection(*write_args, **write_kwargs)
        with sqlite3.connect(db_path) as connection:
            connection.execute("UPDATE raw_metrics SET view_count=view_count + 1")
            connection.commit()
        return result

    monkeypatch.setattr(prv, "write_projection", publish_then_mutate_source)
    try:
        with pytest.raises(RuntimeError, match="evidence epoch changed"):
            prv.apply_projection(args, source, chunks, stats)
    finally:
        source.close()
    assert len(prv.managed_projection_paths(raw_dir)) == 1
    assert (raw_dir / prv.PROJECTION_JOURNAL_NAME).is_file()
    assert (raw_dir / prv.PROJECTION_TRANSACTION_DIR).is_dir()

    recovery = prv.recover_interrupted_projection(raw_dir)

    assert recovery["recovered"] is True
    assert recovery["plan_hash"] == stats["projection_plan"]["plan_hash"]
    assert not (raw_dir / prv.PROJECTION_TRANSACTION_DIR).exists()


def test_apply_requires_a_frozen_projection_plan_before_any_effect(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    writer = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        writer.upsert_turn(
            source_agent="codex",
            session_id="missing-plan",
            turn_number=0,
            user_content="do not apply",
            assistant_content="without a plan",
        )
    finally:
        writer.close()
    args = Namespace(
        raw_dir=str(raw_dir),
        db_path=str(db_path),
        backup_dir="",
        max_files=0,
        chunk_turns=1,
        max_turn_chars=0,
        include_eligible_delete=False,
    )
    source, chunks, stats = prv.plan_projection(args)
    stats.pop("projection_plan")
    try:
        with pytest.raises(RuntimeError, match="plan is missing"):
            prv.apply_projection(args, source, chunks, stats)
    finally:
        source.close()
    assert not raw_dir.exists()


def test_cli_recovery_binds_reviewed_plan_and_backup_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_recover_interrupted_projection = prv.recover_interrupted_projection
    raw_dir = tmp_path / "raw"
    backup_dir = tmp_path / "backups"
    expected_plan_hash = "a" * 64
    observed: dict[str, object] = {}

    def recover(path, **kwargs):
        observed["path"] = path
        observed.update(kwargs)
        return {
            "recovered": True,
            "plan_hash": expected_plan_hash,
            "generation_hash": "b" * 64,
            "recovery_receipt_path": str(backup_dir / "receipt.json"),
        }

    monkeypatch.setattr(prv, "recover_interrupted_projection", recover)
    monkeypatch.setattr(prv, "get_config", lambda: object())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "project_raw_vault.py",
            "--apply",
            "--raw-dir",
            str(raw_dir),
            "--backup-dir",
            str(backup_dir),
            "--expected-plan-hash",
            expected_plan_hash,
            "--json",
        ],
    )

    assert prv.main() == 0
    assert observed == {
        "path": raw_dir,
        "expected_plan_hash": expected_plan_hash,
        "expected_backup_dir": backup_dir,
    }

    db_path = tmp_path / "raw_events.db"
    retained_raw_dir = tmp_path / "retained-raw"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="recovery-plan-binding",
            turn_number=0,
            user_content="retain exact transaction",
            assistant_content="reject a different reviewed plan",
        )
        chunks = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )
        plan = prv.build_projection_plan(
            retained_raw_dir,
            store,
            chunks,
            db_path=db_path,
            max_turn_chars=0,
        )

        def retain_prepared_transaction() -> None:
            raise KeyboardInterrupt("retain exact prepared transaction")

        with pytest.raises(KeyboardInterrupt, match="retain exact prepared"):
            prv.write_projection(
                retained_raw_dir,
                store,
                chunks,
                db_path=db_path,
                max_turn_chars=0,
                projection_plan=plan,
                before_publish=retain_prepared_transaction,
                retain_transaction=True,
            )
    finally:
        store.close()

    transaction_dir = retained_raw_dir / prv.PROJECTION_TRANSACTION_DIR
    before = {
        path.relative_to(transaction_dir).as_posix(): path.read_bytes()
        for path in transaction_dir.rglob("*")
        if path.is_file()
    }
    with pytest.raises(RuntimeError, match="recovery plan hash does not match"):
        real_recover_interrupted_projection(
            retained_raw_dir,
            expected_plan_hash="f" * 64,
        )
    assert {
        path.relative_to(transaction_dir).as_posix(): path.read_bytes()
        for path in transaction_dir.rglob("*")
        if path.is_file()
    } == before


def test_plan_binds_backup_scope_and_rejects_changed_apply_target(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    authorized = tmp_path / "authorized"
    unauthorized = tmp_path / "unauthorized"
    writer = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        writer.upsert_turn(
            source_agent="codex",
            session_id="backup-scope",
            turn_number=0,
            user_content="scope",
            assistant_content="bound",
        )
    finally:
        writer.close()
    plan_args = Namespace(
        raw_dir=str(raw_dir),
        db_path=str(db_path),
        backup_dir=str(authorized),
        max_files=0,
        chunk_turns=1,
        max_turn_chars=0,
        include_eligible_delete=False,
    )
    source, chunks, stats = prv.plan_projection(plan_args)
    apply_args = Namespace(**{**vars(plan_args), "backup_dir": str(unauthorized)})
    try:
        with pytest.raises(RuntimeError, match="preconditions changed"):
            prv.apply_projection(apply_args, source, chunks, stats)
    finally:
        source.close()
    assert not raw_dir.exists()
    assert not authorized.exists()
    assert not unauthorized.exists()


def test_apply_rejects_raw_epoch_drift_after_plan_before_any_effect(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    writer = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        revision_id = writer.upsert_turn(
            source_agent="codex",
            session_id="epoch-drift",
            turn_number=0,
            user_content="frozen",
            assistant_content="source",
        )
    finally:
        writer.close()
    args = Namespace(
        raw_dir=str(raw_dir),
        db_path=str(db_path),
        backup_dir="",
        max_files=0,
        chunk_turns=1,
        max_turn_chars=0,
        include_eligible_delete=False,
    )
    source, chunks, stats = prv.plan_projection(args)
    try:
        mutator = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
        try:
            mutator.record_access(revision_id, "view")
        finally:
            mutator.close()

        with pytest.raises(RuntimeError, match="evidence epoch changed"):
            prv.apply_projection(args, source, chunks, stats)
        assert not raw_dir.exists()
    finally:
        source.close()


def test_apply_rechecks_source_epoch_after_current_plan_before_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    writer = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        writer.upsert_turn(
            source_agent="codex",
            session_id="epoch-race",
            turn_number=0,
            user_content="planned",
            assistant_content="must not publish stale",
        )
    finally:
        writer.close()
    args = Namespace(
        raw_dir=str(raw_dir),
        db_path=str(db_path),
        backup_dir="",
        max_files=0,
        chunk_turns=1,
        max_turn_chars=0,
        include_eligible_delete=False,
    )
    source, chunks, stats = prv.plan_projection(args)
    real_build_plan = prv.build_projection_plan
    calls = {"count": 0}

    def race_after_plan(*plan_args, **plan_kwargs):
        plan = real_build_plan(*plan_args, **plan_kwargs)
        calls["count"] += 1
        if calls["count"] == 1:
            with sqlite3.connect(db_path) as conn:
                conn.execute("UPDATE raw_metrics SET view_count=view_count + 1")
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return plan

    monkeypatch.setattr(prv, "build_projection_plan", race_after_plan)
    try:
        with pytest.raises(RuntimeError, match="evidence epoch changed"):
            prv.apply_projection(args, source, chunks, stats)
    finally:
        source.close()
    assert not raw_dir.exists()


def test_replanned_same_generation_second_apply_has_zero_total_write_set(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    writer = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        writer.upsert_turn(
            source_agent="codex",
            session_id="second-apply",
            turn_number=0,
            user_content="stable generation",
            assistant_content="stable generation",
        )
    finally:
        writer.close()
    args = Namespace(
        raw_dir=str(raw_dir),
        db_path=str(db_path),
        backup_dir=str(tmp_path / "backup"),
        max_files=0,
        chunk_turns=1,
        max_turn_chars=0,
        include_eligible_delete=False,
    )

    first_source, first_chunks, first_stats = prv.plan_projection(args)
    try:
        first = prv.apply_projection(args, first_source, first_chunks, first_stats)
    finally:
        first_source.close()
    second_source, second_chunks, second_stats = prv.plan_projection(args)
    try:
        assert (
            second_stats["projection_plan"]["generation_hash"]
            == first_stats["projection_plan"]["generation_hash"]
        )
        assert second_stats["projection_plan"]["write_set_empty"] is True
        assert not hasattr(prv, "_atomic_write_text")
        with (
            patch.object(
                prv,
                "_secure_atomic_write_text",
                wraps=prv._secure_atomic_write_text,  # noqa: SLF001
            ) as secure_atomic_write,
            patch.object(
                prv,
                "_remove_projection_transaction",
                wraps=prv._remove_projection_transaction,  # noqa: SLF001
            ) as remove_transaction,
            patch.object(
                prv,
                "update_raw_index_changes",
                wraps=prv.update_raw_index_changes,
            ) as update_index,
        ):
            second = prv.apply_projection(args, second_source, second_chunks, second_stats)
        secure_atomic_write.assert_not_called()
        remove_transaction.assert_not_called()
        update_index.assert_not_called()
        assert second["written_files"] == 0
        assert second["deleted_stale_files"] == 0
        assert second["bytes_written"] == 0
        assert second["post_apply_zero_delta"] is True
        assert first["applied_plan_hash"] != second["applied_plan_hash"]
    finally:
        second_source.close()


def test_restart_discards_manifestless_preparing_transaction_before_replan(
    tmp_path: Path,
) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    transaction_dir = raw_dir / prv.PROJECTION_TRANSACTION_DIR
    transaction_dir.mkdir()
    plan_hash = "a" * 64
    generation_hash = "b" * 64
    prv._write_projection_transaction_state(  # noqa: SLF001
        transaction_dir,
        status="preparing",
        plan_hash=plan_hash,
        generation_hash=generation_hash,
        manifest_hash="",
    )
    (transaction_dir / "new").mkdir()
    (transaction_dir / "new" / "partial.md").write_text(
        "staging only",
        encoding="utf-8",
    )

    recovery = prv.recover_interrupted_projection(raw_dir)

    assert recovery == {"recovered": False, "plan_hash": ""}
    assert not transaction_dir.exists()


def test_recovery_rejects_live_transaction_owner_without_deleting_prestate(
    tmp_path: Path,
) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    transaction_dir = raw_dir / prv.PROJECTION_TRANSACTION_DIR
    lock_fd = prv._acquire_projection_transaction_lock(raw_dir)  # noqa: SLF001
    transaction_dir.mkdir()
    repo_root = Path(prv.__file__).resolve().parents[1]
    child = """
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from scripts import project_raw_vault as prv
try:
    prv.recover_interrupted_projection(Path(sys.argv[2]))
except RuntimeError as exc:
    if str(exc) == "Raw projection transaction owner is active":
        raise SystemExit(73)
    raise
raise SystemExit(74)
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", child, str(repo_root), str(raw_dir)],
            check=False,
        )
        assert result.returncode == 73
        assert transaction_dir.is_dir()
        assert list(transaction_dir.iterdir()) == []
    finally:
        prv._release_projection_transaction_lock(lock_fd)  # noqa: SLF001

    assert prv.recover_interrupted_projection(raw_dir) == {
        "recovered": False,
        "plan_hash": "",
    }
    assert not transaction_dir.exists()


def test_zero_delta_apply_does_not_recreate_missing_obsidian_directory(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="zero-delta-no-obsidian",
            turn_number=0,
            user_content="stable",
            assistant_content="no unplanned mkdir",
        )
        chunks = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )
        args = Namespace(backup_dir="", max_turn_chars=0)
        first_stats = _planned_stats(raw_dir, store, chunks, db_path)
        prv.apply_projection(args, store, chunks, first_stats)
        (raw_dir / ".obsidian").rmdir()
        second_stats = _planned_stats(raw_dir, store, chunks, db_path)
        assert second_stats["projection_plan"]["write_set_empty"] is True

        with patch.object(
            prv,
            "_ensure_safe_projection_root",
            wraps=prv._ensure_safe_projection_root,  # noqa: SLF001
        ) as ensure_root:
            second = prv.apply_projection(args, store, chunks, second_stats)

        ensure_root.assert_not_called()
        assert not (raw_dir / ".obsidian").exists()
        assert second["post_apply_zero_delta"] is True
    finally:
        store.close()


def test_zero_delta_apply_does_not_clean_projection_tombstones_without_recovery(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="zero-delta-tombstone",
            turn_number=0,
            user_content="stable projection",
            assistant_content="historical cleanup needs explicit recovery",
        )
        chunks = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )
        args = Namespace(backup_dir="", max_turn_chars=0)
        prv.apply_projection(
            args,
            store,
            chunks,
            _planned_stats(raw_dir, store, chunks, db_path),
        )
        tombstone = raw_dir / (
            f"{prv.PROJECTION_TRANSACTION_TOMBSTONE_PREFIX}{'b' * 32}"
        )
        debris = tombstone / "new" / "codex" / "preserved.md"
        debris.parent.mkdir(parents=True)
        debris.write_text("preserve until explicit recovery\n", encoding="utf-8")
        debris_before = debris.read_bytes()
        zero_stats = _planned_stats(raw_dir, store, chunks, db_path)
        assert zero_stats["projection_plan"]["write_set_empty"] is True

        with (
            patch.object(
                prv,
                "_acquire_projection_transaction_lock",
                wraps=prv._acquire_projection_transaction_lock,  # noqa: SLF001
            ) as acquire_lock,
            patch.object(
                prv,
                "_cleanup_projection_transaction_tombstones",
                wraps=prv._cleanup_projection_transaction_tombstones,  # noqa: SLF001
            ) as cleanup_tombstones,
            patch.object(
                prv,
                "_remove_projection_transaction",
                wraps=prv._remove_projection_transaction,  # noqa: SLF001
            ) as remove_transaction,
        ):
            result = prv.apply_projection(
                args,
                store,
                chunks,
                zero_stats,
            )

        acquire_lock.assert_not_called()
        cleanup_tombstones.assert_not_called()
        remove_transaction.assert_not_called()
        assert result["post_apply_zero_delta"] is True
        assert debris.read_bytes() == debris_before
        assert tombstone.is_dir()
    finally:
        store.close()


def test_zero_delta_post_write_failure_does_not_clean_tombstones_without_owner(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="zero-delta-post-write-failure",
            turn_number=0,
            user_content="stable projection",
            assistant_content="failed verification still cannot clean",
        )
        chunks = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )
        args = Namespace(backup_dir="", max_turn_chars=0)
        prv.apply_projection(
            args,
            store,
            chunks,
            _planned_stats(raw_dir, store, chunks, db_path),
        )
        tombstone = raw_dir / (
            f"{prv.PROJECTION_TRANSACTION_TOMBSTONE_PREFIX}{'c' * 32}"
        )
        debris = tombstone / "new" / "codex" / "preserved.md"
        debris.parent.mkdir(parents=True)
        debris.write_text("preserve after failed no-op\n", encoding="utf-8")
        debris_before = debris.read_bytes()
        zero_stats = _planned_stats(raw_dir, store, chunks, db_path)
        callback_count = 0

        def fail_after_noop_write() -> None:
            nonlocal callback_count
            callback_count += 1
            if callback_count == 5:
                raise RuntimeError("epoch drift after noop write")

        with (
            patch.object(
                prv,
                "_acquire_projection_transaction_lock",
                wraps=prv._acquire_projection_transaction_lock,  # noqa: SLF001
            ) as acquire_lock,
            patch.object(
                prv,
                "_cleanup_projection_transaction_tombstones",
                wraps=prv._cleanup_projection_transaction_tombstones,  # noqa: SLF001
            ) as cleanup_tombstones,
            patch.object(
                prv,
                "_remove_projection_transaction",
                wraps=prv._remove_projection_transaction,  # noqa: SLF001
            ) as remove_transaction,
            patch.object(
                store,
                "assert_epoch_current",
                side_effect=fail_after_noop_write,
                create=True,
            ),
            pytest.raises(RuntimeError, match="epoch drift after noop write"),
        ):
            prv.apply_projection(
                args,
                store,
                chunks,
                zero_stats,
            )

        acquire_lock.assert_not_called()
        cleanup_tombstones.assert_not_called()
        remove_transaction.assert_not_called()
        assert debris.read_bytes() == debris_before
        assert tombstone.is_dir()
    finally:
        store.close()


def test_apply_preserves_foreign_prepared_transaction_from_different_plan(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    backup_dir = tmp_path / "backups"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="foreign-prepared-plan",
            turn_number=0,
            user_content="old generation",
            assistant_content="must remain recoverable",
        )
        old_chunks = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )
        old_plan = prv.build_projection_plan(
            raw_dir,
            store,
            old_chunks,
            db_path=db_path,
            max_turn_chars=0,
            backup_dir=backup_dir,
        )
        old_receipt = {
            "schema_version": "mnemos.raw_projection_change_set.v1",
            "status": "planned",
            "plan_hash": old_plan["plan_hash"],
            "generation_hash": old_plan["generation_hash"],
            "backup_dir": old_plan["backup_dir"],
            "changed_paths": old_plan["changed_paths"],
            "stale_paths": old_plan["stale_paths"],
            "index_changed_paths": old_plan["index_changed_paths"],
            "index_deleted_paths": old_plan["index_deleted_paths"],
        }

        def leave_old_prepared_transaction() -> None:
            prv._write_change_manifest(  # noqa: SLF001
                backup_dir,
                old_receipt,
                receipt_kind="plan",
            )
            raise KeyboardInterrupt("leave old prepared transaction")

        with pytest.raises(KeyboardInterrupt, match="old prepared"):
            prv.write_projection(
                raw_dir,
                store,
                old_chunks,
                db_path=db_path,
                max_turn_chars=0,
                transaction_backup_dir=backup_dir,
                projection_plan=old_plan,
                before_publish=leave_old_prepared_transaction,
                retain_transaction=True,
            )
        transaction_dir = raw_dir / prv.PROJECTION_TRANSACTION_DIR
        old_transaction_bytes = {
            path.relative_to(transaction_dir).as_posix(): path.read_bytes()
            for path in transaction_dir.rglob("*")
            if path.is_file()
        }
        old_receipt_path = (
            backup_dir / f"raw-projection-plan-{old_plan['plan_hash']}.json"
        )
        old_receipt_bytes = old_receipt_path.read_bytes()

        store.upsert_turn(
            source_agent="codex",
            session_id="foreign-prepared-plan",
            turn_number=0,
            user_content="new generation",
            assistant_content="must not consume the old transaction",
        )
        new_chunks = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )
        new_stats = _planned_stats(
            raw_dir,
            store,
            new_chunks,
            db_path,
            backup_dir=backup_dir,
        )

        with pytest.raises(
            RuntimeError,
            match="interrupted transaction requires recovery",
        ):
            prv.apply_projection(
                Namespace(backup_dir=str(backup_dir), max_turn_chars=0),
                store,
                new_chunks,
                new_stats,
            )

        assert {
            path.relative_to(transaction_dir).as_posix(): path.read_bytes()
            for path in transaction_dir.rglob("*")
            if path.is_file()
        } == old_transaction_bytes
        assert old_receipt_path.read_bytes() == old_receipt_bytes
        assert list(backup_dir.glob("raw-projection-abort-*.json")) == []
        assert list(backup_dir.glob("raw-projection-commit-*.json")) == []
        assert list(backup_dir.glob("raw-projection-recovery-*.json")) == []
    finally:
        store.close()


def test_failed_apply_does_not_resolve_foreign_transaction_created_after_precheck(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    reviewed_backup_dir = tmp_path / "reviewed-backups"
    foreign_backup_dir = tmp_path / "foreign-backups"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="foreign-after-precheck",
            turn_number=0,
            user_content="reviewed generation",
            assistant_content="foreign transaction must survive failed cleanup",
        )
        chunks = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )
        reviewed_stats = _planned_stats(
            raw_dir,
            store,
            chunks,
            db_path,
            backup_dir=reviewed_backup_dir,
        )
        foreign_plan = prv.build_projection_plan(
            raw_dir,
            store,
            chunks,
            db_path=db_path,
            max_turn_chars=0,
            backup_dir=foreign_backup_dir,
        )
        original_write_projection = prv.write_projection

        def create_foreign_transaction_after_precheck(
            _raw_dir: Path,
            _store: RawEventStore,
            _chunks: list[prv.ProjectionChunk],
            **kwargs: object,
        ) -> dict[str, object]:
            before_publish = kwargs["before_publish"]
            assert callable(before_publish)
            before_publish()

            def retain_foreign_prepared_transaction() -> None:
                raise KeyboardInterrupt("foreign transaction appeared after precheck")

            return original_write_projection(
                raw_dir,
                store,
                chunks,
                db_path=db_path,
                max_turn_chars=0,
                transaction_backup_dir=foreign_backup_dir,
                projection_plan=foreign_plan,
                before_publish=retain_foreign_prepared_transaction,
                retain_transaction=True,
            )

        with patch.object(
            prv,
            "write_projection",
            side_effect=create_foreign_transaction_after_precheck,
        ):
            with pytest.raises(
                KeyboardInterrupt,
                match="foreign transaction appeared after precheck",
            ):
                prv.apply_projection(
                    Namespace(
                        backup_dir=str(reviewed_backup_dir),
                        max_turn_chars=0,
                    ),
                    store,
                    chunks,
                    reviewed_stats,
                )

        transaction_dir = raw_dir / prv.PROJECTION_TRANSACTION_DIR
        transaction = prv._load_projection_transaction(  # noqa: SLF001
            raw_dir,
            allow_cleanup=False,
        )
        assert transaction_dir.is_dir()
        assert transaction["plan_hash"] == foreign_plan["plan_hash"]
        assert transaction["backup_dir"] == str(foreign_backup_dir.resolve())
        assert list(reviewed_backup_dir.glob("raw-projection-abort-*.json")) == []
    finally:
        store.close()


def test_restart_rejects_prepared_transaction_without_manifest(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    transaction_dir = raw_dir / prv.PROJECTION_TRANSACTION_DIR
    transaction_dir.mkdir()
    prv._write_projection_transaction_state(  # noqa: SLF001
        transaction_dir,
        status="prepared",
        plan_hash="a" * 64,
        generation_hash="b" * 64,
        manifest_hash="c" * 64,
    )

    with pytest.raises(RuntimeError, match="prepared transaction manifest is missing"):
        prv.recover_interrupted_projection(raw_dir)
    assert transaction_dir.is_dir()


def test_real_process_exit_during_preparation_is_safely_discarded_on_restart(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="prepare-exit",
            turn_number=0,
            user_content="durable source",
            assistant_content="partial staging is not publication",
        )
    finally:
        store.close()
    repo_root = Path(prv.__file__).resolve().parents[1]
    child = """
import os
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from core.sync_framework.raw_event_store import RawEventStore
from scripts import project_raw_vault as prv
class Cfg:
    def __init__(self, database_dir):
        self.database_dir = database_dir
    def get(self, key, default=None):
        return default
db_path = Path(sys.argv[2])
raw_dir = Path(sys.argv[3])
store = RawEventStore(db_path=db_path, config=Cfg(db_path.parent))
chunks = prv.build_projection_chunks(
    prv._fetch_refs(store),
    chunk_turns=1,
    max_chunks=None,
)
original = prv._secure_atomic_write_text
def exit_after_first_staged_chunk(root, relative_path, text):
    original(root, relative_path, text)
    if str(relative_path).startswith("new/") and str(relative_path).endswith(".md"):
        os._exit(77)
prv._secure_atomic_write_text = exit_after_first_staged_chunk
prv.write_projection(
    raw_dir,
    store,
    chunks,
    db_path=db_path,
    max_turn_chars=0,
)
"""

    result = subprocess.run(
        [sys.executable, "-c", child, str(repo_root), str(db_path), str(raw_dir)],
        check=False,
    )

    assert result.returncode == 77
    transaction_dir = raw_dir / prv.PROJECTION_TRANSACTION_DIR
    assert json.loads(
        (transaction_dir / "state.json").read_text(encoding="utf-8")
    )["status"] == "preparing"
    assert prv.managed_projection_paths(raw_dir) == []
    assert not (raw_dir / prv.PROJECTION_JOURNAL_NAME).exists()
    recovery = prv.recover_interrupted_projection(raw_dir)
    assert recovery == {"recovered": False, "plan_hash": ""}
    assert not transaction_dir.exists()

    restarted = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        chunks = prv.build_projection_chunks(
            prv._fetch_refs(restarted),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )
        applied = prv.write_projection(
            raw_dir,
            restarted,
            chunks,
            db_path=db_path,
            max_turn_chars=0,
        )
        assert applied["written_files"] == 1
    finally:
        restarted.close()


def test_real_process_exit_before_initial_state_replace_is_safely_discarded(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="prestate-exit",
            turn_number=0,
            user_content="durable source",
            assistant_content="state temp is not publication",
        )
    finally:
        store.close()
    repo_root = Path(prv.__file__).resolve().parents[1]
    child = """
import os
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from core.sync_framework.raw_event_store import RawEventStore
from scripts import project_raw_vault as prv
class Cfg:
    def __init__(self, database_dir):
        self.database_dir = database_dir
    def get(self, key, default=None):
        return default
db_path = Path(sys.argv[2])
raw_dir = Path(sys.argv[3])
store = RawEventStore(db_path=db_path, config=Cfg(db_path.parent))
chunks = prv.build_projection_chunks(prv._fetch_refs(store), chunk_turns=1, max_chunks=None)
original_replace = prv.os.replace
def exit_before_state_replace(source, target, *args, **kwargs):
    if target == "state.json":
        os._exit(78)
    return original_replace(source, target, *args, **kwargs)
prv.os.replace = exit_before_state_replace
prv.write_projection(raw_dir, store, chunks, db_path=db_path, max_turn_chars=0)
"""

    result = subprocess.run(
        [sys.executable, "-c", child, str(repo_root), str(db_path), str(raw_dir)],
        check=False,
    )

    assert result.returncode == 78
    transaction_dir = raw_dir / prv.PROJECTION_TRANSACTION_DIR
    entries = list(transaction_dir.iterdir())
    assert len(entries) == 1
    assert entries[0].name.startswith(".state.json.")
    recovery = prv.recover_interrupted_projection(raw_dir)
    assert recovery == {"recovered": False, "plan_hash": ""}
    assert not transaction_dir.exists()


def test_real_process_exit_during_transaction_removal_leaves_only_tombstone(
    tmp_path: Path,
) -> None:
    raw_dir = tmp_path / "raw"
    transaction_dir = raw_dir / prv.PROJECTION_TRANSACTION_DIR
    transaction_dir.mkdir(parents=True)
    (transaction_dir / "state.json").write_text("removal debris", encoding="utf-8")
    repo_root = Path(prv.__file__).resolve().parents[1]
    child = """
import os
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from scripts import project_raw_vault as prv
transaction_dir = Path(sys.argv[2])
prv.shutil.rmtree = lambda *args, **kwargs: os._exit(80)
prv._remove_projection_transaction(transaction_dir)
"""

    result = subprocess.run(
        [sys.executable, "-c", child, str(repo_root), str(transaction_dir)],
        check=False,
    )

    assert result.returncode == 80
    assert not transaction_dir.exists()
    tombstones = list(
        raw_dir.glob(f"{prv.PROJECTION_TRANSACTION_TOMBSTONE_PREFIX}*")
    )
    assert len(tombstones) == 1
    recovery = prv.recover_interrupted_projection(raw_dir)
    assert recovery == {"recovered": False, "plan_hash": ""}
    assert list(
        raw_dir.glob(f"{prv.PROJECTION_TRANSACTION_TOMBSTONE_PREFIX}*")
    ) == []


def test_checkpoint_snapshot_plan_renders_bound_production_db_identity(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    snapshot_path = tmp_path / "snapshot" / "raw_events.db"
    raw_dir = tmp_path / "raw"
    writer = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        writer.upsert_turn(
            source_agent="codex",
            session_id="checkpoint-identity",
            turn_number=0,
            user_content="same source bytes",
            assistant_content="same projection identity",
        )
    finally:
        writer.close()
    live_args = Namespace(
        raw_dir=str(raw_dir),
        db_path=str(db_path),
        canonical_db_identity="",
        backup_dir="",
        max_files=0,
        chunk_turns=1,
        max_turn_chars=0,
        include_eligible_delete=False,
    )
    live_source, live_chunks, live_stats = prv.plan_projection(live_args)
    try:
        prv.apply_projection(live_args, live_source, live_chunks, live_stats)
    finally:
        live_source.close()
    snapshot_path.parent.mkdir()
    snapshot_path.write_bytes(db_path.read_bytes())
    snapshot_args = Namespace(
        **{
            **vars(live_args),
            "db_path": str(snapshot_path),
            "canonical_db_identity": str(db_path),
        }
    )

    snapshot_source, _snapshot_chunks, snapshot_stats = prv.plan_projection(snapshot_args)
    try:
        assert (
            snapshot_stats["projection_plan"]["generation_hash"]
            == live_stats["projection_plan"]["generation_hash"]
        )
        assert snapshot_stats["projection_plan"]["write_set_empty"] is True
        assert snapshot_stats["canonical_db_identity"] == str(db_path)
    finally:
        snapshot_source.close()


def test_build_projection_chunks_without_limit_keeps_all_chunks() -> None:
    refs = [
        _ref(
            event_id=f"event-{turn}",
            session_id="sess-full",
            turn_number=turn,
            survival_score=float(turn),
        )
        for turn in range(12)
    ]

    chunks = prv.build_projection_chunks(refs, chunk_turns=5, max_chunks=None)

    assert len(chunks) == 3
    assert sorted((chunk.start_turn, chunk.end_turn) for chunk in chunks) == [
        (0, 4),
        (5, 9),
        (10, 11),
    ]


def test_chunk_path_stays_unique_when_session_slug_is_truncated(tmp_path: Path) -> None:
    prefix = "codex-20260628-obsidian-mnemos-index-repair"
    chunk_a = prv.ProjectionChunk(
        source_agent="codex",
        session_id=f"{prefix}-final",
        chunk_index=0,
        refs=[
            _ref(
                event_id="aaaaaaaaaa111111",
                session_id=f"{prefix}-final",
                source_agent="codex",
            )
        ],
    )
    chunk_b = prv.ProjectionChunk(
        source_agent="codex",
        session_id=prefix,
        chunk_index=0,
        refs=[
            _ref(
                event_id="bbbbbbbbbb222222",
                session_id=prefix,
                source_agent="codex",
            )
        ],
    )

    assert prv._chunk_path(tmp_path, chunk_a) != prv._chunk_path(tmp_path, chunk_b)  # noqa: SLF001


def test_chunk_path_uses_agent_date_structure(tmp_path: Path) -> None:
    chunk = prv.ProjectionChunk(
        source_agent="codex",
        session_id="sess-1",
        chunk_index=0,
        refs=[
            _ref(
                event_id="aaaaaaaaaa111111",
                session_id="sess-1",
                source_agent="codex",
            )
        ],
    )

    path = prv._chunk_path(tmp_path, chunk)  # noqa: SLF001

    assert path.parent == tmp_path / "codex" / "2026-06-28"
    assert path.name.startswith("codex_sess-1_aaaaaaaaaa_t0001-0001")


def test_chunk_body_sequence_follows_canonical_ref_sequence() -> None:
    chunk = prv.ProjectionChunk(
        source_agent="codex",
        session_id="sequence",
        chunk_index=0,
        refs=[
            _ref(event_id="event-a", session_id="sequence", turn_number=0),
            _ref(event_id="event-b", session_id="sequence", turn_number=1),
        ],
    )

    class _Store:
        @staticmethod
        def get_turn(event_id: str):
            return {
                "event_id": event_id,
                "turn_number": 1 if event_id == "event-a" else 0,
            }

    turns = prv._load_turns(_Store(), chunk)  # noqa: SLF001

    assert [turn["event_id"] for turn in turns] == chunk.event_ids


def test_load_turns_rejects_a_missing_canonical_chunk_turn() -> None:
    event_id = "rawrev-" + "a" * 40
    chunk = prv.ProjectionChunk(
        source_agent="codex",
        session_id="missing-turn",
        chunk_index=0,
        refs=[_ref(event_id=event_id, session_id="missing-turn")],
    )

    class _Store:
        @staticmethod
        def get_turn(_event_id: str):
            return None

    with pytest.raises(RuntimeError, match="complete canonical event sequence"):
        prv._load_turns(_Store(), chunk)  # noqa: SLF001


def test_load_turns_rejects_a_misbound_canonical_chunk_turn() -> None:
    event_id = "rawrev-" + "b" * 40
    chunk = prv.ProjectionChunk(
        source_agent="codex",
        session_id="misbound-turn",
        chunk_index=0,
        refs=[_ref(event_id=event_id, session_id="misbound-turn")],
    )

    class _Store:
        @staticmethod
        def get_turn(_event_id: str):
            return {"event_id": "rawrev-" + "c" * 40}

    with pytest.raises(RuntimeError, match="complete canonical event sequence"):
        prv._load_turns(_Store(), chunk)  # noqa: SLF001


def test_build_projection_chunks_keeps_source_agent_coverage() -> None:
    refs = [
        _ref(
            event_id=f"codex-{turn}",
            source_agent="codex",
            session_id=f"codex-{turn}",
            turn_number=turn,
            survival_score=90.0,
        )
        for turn in range(20)
    ]
    refs.extend(
        [
            _ref(
                event_id="kimi-0",
                source_agent="kimi",
                session_id="kimi-0",
                survival_score=40.0,
            ),
            _ref(
                event_id="hermes-0",
                source_agent="hermes",
                session_id="hermes-0",
                survival_score=40.0,
            ),
        ]
    )

    chunks = prv.build_projection_chunks(refs, chunk_turns=1, max_chunks=5)

    assert {chunk.source_agent for chunk in chunks} == {"codex", "kimi", "hermes"}
    assert sum(1 for chunk in chunks if chunk.source_agent == "codex") == 3


def test_fetch_refs_excludes_eligible_delete_by_default(tmp_path: Path) -> None:
    store = RawEventStore(db_path=tmp_path / "raw_events.db", config=_Cfg(tmp_path))
    try:
        active = store.upsert_turn(
            source_agent="codex",
            session_id="active",
            turn_number=0,
            user_content="active user",
            assistant_content="active assistant",
        )
        stale = store.upsert_turn(
            source_agent="codex",
            session_id="stale",
            turn_number=0,
            user_content="stale user",
            assistant_content="stale assistant",
        )
        conn = store._pool.get_conn()  # noqa: SLF001
        conn.execute(
            "UPDATE raw_metrics SET retention_state = 'eligible_delete' WHERE event_id = ?",
            (store.get_turn(stale)["logical_event_id"],),
        )
        conn.commit()

        refs = prv._fetch_refs(store)  # noqa: SLF001
        all_refs = prv._fetch_refs(store, include_eligible_delete=True)  # noqa: SLF001

        assert {ref.event_id for ref in refs} == {active}
        assert {ref.event_id for ref in all_refs} == {active, stale}
    finally:
        store.close()


def test_write_projection_creates_lossless_hashed_agent_date_chunk(tmp_path: Path) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        event_id = store.upsert_turn(
            source_agent="codex",
            session_id="sess-1",
            turn_number=0,
            user_content="u" * 80,
            assistant_content="assistant content",
            reasoning="reasoning content",
            timestamp="2026-06-28T10:00:00",
            completeness={"visible_text": "full", "truncated": False},
            tool_calls=[{"name": "tool"}],
        )
        store.record_access(event_id, "reference", consumer="test")
        refs = prv._fetch_refs(store)  # noqa: SLF001
        chunks = prv.build_projection_chunks(refs, chunk_turns=5, max_chunks=1)

        stats = prv.write_projection(
            raw_dir,
            store,
            chunks,
            db_path=db_path,
            max_turn_chars=0,
        )

        md_files = sorted(raw_dir.rglob("*.md"))
        chunk_text = md_files[0].read_text(encoding="utf-8")
        assert stats["projected_files"] == 1
        assert stats["truncated_chunks"] == 0
        assert stats["written_files"] == 1
        assert (raw_dir / ".obsidian").is_dir()
        assert not list(raw_dir.glob("*.md"))
        assert md_files[0].parent == raw_dir / "codex" / "2026-06-28"
        assert event_id in chunk_text
        assert "u" * 80 in chunk_text
        assert "projection truncated" not in chunk_text
        assert "Lossless Raw projection" in chunk_text
        assert "mnemos-raw-event-v2" in chunk_text
        assert "mnemos-raw-field-v2" in chunk_text
    finally:
        store.close()


def test_projection_descriptors_discard_rendered_raw_bodies(tmp_path: Path) -> None:
    db_path = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="descriptor-memory",
            turn_number=0,
            user_content="x" * 1_000_000,
            assistant_content="y" * 1_000_000,
        )
        chunks = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )

        descriptors, _chunks_by_path = prv._artifact_descriptors(  # noqa: SLF001
            tmp_path / "raw",
            store,
            chunks,
            db_path=db_path,
            max_turn_chars=0,
        )

        assert len(descriptors) == 1
        descriptor = next(iter(descriptors.values()))
        assert descriptor.text == ""
        assert len(descriptor.sha256) == 64
    finally:
        store.close()


def test_write_projection_rejects_corrupted_published_chunk(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="corrupt-publish",
            turn_number=0,
            user_content="user",
            assistant_content="assistant",
            timestamp="2026-06-28T10:00:00",
        )
        chunks = prv.build_projection_chunks(prv._fetch_refs(store), chunk_turns=1, max_chunks=1)
        original_publish = prv._secure_publish_staged_file  # noqa: SLF001

        def corrupt_after_publish(*args, **kwargs) -> None:
            original_publish(*args, **kwargs)
            relative_path = args[1]
            if str(relative_path).endswith(".md"):
                target = raw_dir / relative_path
                with target.open("r+b") as handle:
                    handle.write(b"\\x00")
                    handle.flush()

        monkeypatch.setattr(
            prv,
            "_secure_publish_staged_file",
            corrupt_after_publish,
        )

        with pytest.raises(
            RuntimeError,
            match="publish hash verification failed",
        ):
            prv.write_projection(raw_dir, store, chunks, db_path=db_path, max_turn_chars=0)
        target = prv._chunk_path(raw_dir, chunks[0])  # noqa: SLF001
        assert target.read_bytes().startswith(b"\\x00")
        assert (raw_dir / prv.PROJECTION_TRANSACTION_DIR).is_dir()

        assert not (raw_dir / prv.PROJECTION_JOURNAL_NAME).exists()
    finally:
        store.close()


def test_recovery_rejects_self_signed_manifest_that_diverges_from_embedded_plan(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="manifest-plan-binding",
            turn_number=0,
            user_content="bind every recovery effect",
            assistant_content="a new manifest signature is not authority",
        )
        chunks = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )
        args = Namespace(backup_dir="", max_turn_chars=0)
        stats = _planned_stats(raw_dir, store, chunks, db_path)
        with patch.object(
            prv,
            "update_raw_index_changes",
            side_effect=RuntimeError("retain published transaction"),
        ):
            with pytest.raises(RuntimeError, match="retain published transaction"):
                prv.apply_projection(args, store, chunks, stats)

        transaction_dir = raw_dir / prv.PROJECTION_TRANSACTION_DIR
        manifest_path = transaction_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        changed_path = next(iter(manifest["changed_files"]))
        manifest["changed_files"][changed_path]["target_hash"] = "d" * 64
        unsigned = {
            key: value for key, value in manifest.items() if key != "manifest_hash"
        }
        manifest["manifest_hash"] = prv._sha256_text(  # noqa: SLF001
            json.dumps(
                unsigned,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        state = json.loads(
            (transaction_dir / "state.json").read_text(encoding="utf-8")
        )
        prv._write_projection_transaction_state(  # noqa: SLF001
            transaction_dir,
            status=state["status"],
            plan_hash=state["plan_hash"],
            generation_hash=state["generation_hash"],
            manifest_hash=manifest["manifest_hash"],
        )

        with pytest.raises(RuntimeError, match="changed file is not plan-bound"):
            prv.recover_interrupted_projection(raw_dir)
        assert transaction_dir.is_dir()
    finally:
        store.close()


def test_apply_projection_preserves_unrelated_vault_files_and_writes_change_manifest(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    backup_dir = tmp_path / "backup"
    raw_dir.mkdir()
    (raw_dir / ".obsidian").mkdir()
    (raw_dir / ".obsidian" / "workspace.json").write_text("{}", encoding="utf-8")
    (raw_dir / "old.md").write_text("old", encoding="utf-8")
    (raw_dir / "nested").mkdir()
    (raw_dir / "nested" / "old2.md").write_text("old2", encoding="utf-8")
    (raw_dir / "nested" / ".mnemos_session_index.json").write_text("{}", encoding="utf-8")

    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="sess-1",
            turn_number=0,
            user_content="hello",
            assistant_content="world",
            timestamp="2026-06-28T10:00:00",
        )
        chunks = prv.build_projection_chunks(  # noqa: SLF001
            prv._fetch_refs(store),
            chunk_turns=5,
            max_chunks=1,
        )
        args = Namespace(
            backup_dir=str(backup_dir),
            max_files=2,
            chunk_turns=5,
            max_turn_chars=0,
        )
        stats = {
            "raw_dir": str(raw_dir),
            "db_path": str(db_path),
            "existing_md_files": 2,
            "candidate_turns": 1,
            "projected_chunks": 1,
            "projected_files": 1,
            "max_files": 1,
            "chunk_turns": 5,
            "projection_plan": prv.build_projection_plan(
                raw_dir,
                store,
                chunks,
                db_path=db_path,
                max_turn_chars=0,
                backup_dir=backup_dir,
            ),
        }

        with patch.object(
            prv, "update_raw_index_changes", wraps=prv.update_raw_index_changes
        ) as update_index:
            result = prv.apply_projection(args, store, chunks, stats)

        update_index.assert_called_once()
        assert update_index.call_args.kwargs["changed_paths"]
        assert update_index.call_args.kwargs["deleted_paths"] == []
        assert result["moved_old_files"] == 0
        assert result["moved_old_md_files"] == 0
        assert result["unrelated_files_moved"] == 0
        assert Path(result["backup_plan_manifest_path"]).exists()
        assert Path(result["backup_manifest_path"]).exists()
        assert Path(result["backup_plan_manifest_path"]).name.startswith(
            "raw-projection-plan-"
        )
        assert Path(result["backup_manifest_path"]).name.startswith(
            "raw-projection-commit-"
        )
        assert (raw_dir / ".obsidian" / "workspace.json").exists()
        assert (raw_dir / "old.md").read_text(encoding="utf-8") == "old"
        assert (raw_dir / "nested" / "old2.md").read_text(encoding="utf-8") == "old2"
        assert (raw_dir / "nested" / ".mnemos_session_index.json").exists()
        assert len(list(raw_dir.rglob("*.md"))) == 3
        assert (raw_dir / "old.md").exists()
        assert (raw_dir / "codex" / "2026-06-28").is_dir()
    finally:
        store.close()


def test_rebuild_raw_index_indexes_projection_files(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    chunk_dir = raw_dir / "codex" / "2026-06-28"
    chunk_dir.mkdir(parents=True)
    (chunk_dir / "chunk.md").write_text(
        "---\nmnemos_type: raw_retention_projection\n---\n# Chunk\ncontent",
        encoding="utf-8",
    )

    stats = prv.rebuild_raw_index(raw_dir, db_path=tmp_path / "raw_index.db")

    assert stats["indexed"] == 1


def test_rebuild_raw_index_is_incremental_not_a_full_rebuild(tmp_path: Path) -> None:
    with patch.object(prv, "RawIndex") as index_class:
        index_class.return_value.apply_projection_write_set.return_value = {
            "indexed": 1,
            "removed": 0,
            "failed": 0,
            "orphan_fts_removed": 0,
            "orphan_tags_removed": 0,
        }
        chunk_dir = tmp_path / "codex" / "2026-06-28"
        chunk_dir.mkdir(parents=True)
        (chunk_dir / "chunk.md").write_text(
            "---\nmnemos_type: raw_retention_projection\n---\n# Chunk\ncontent",
            encoding="utf-8",
        )

        stats = prv.rebuild_raw_index(tmp_path)

    assert stats["indexed"] == 1
    index_class.return_value.apply_projection_write_set.assert_called_once()
    index_class.return_value.sync_index.assert_not_called()
    index_class.return_value.close.assert_called_once_with()


def test_incremental_raw_index_defaults_to_the_projection_vault(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    chunk_dir = raw_dir / "codex" / "2026-06-28"
    chunk_dir.mkdir(parents=True)
    (chunk_dir / "chunk.md").write_text(
        "---\nmnemos_type: raw_retention_projection\n---\n# Chunk\ncontent",
        encoding="utf-8",
    )

    with patch.object(prv, "RawIndex") as index_class:
        index_class.return_value.apply_projection_write_set.return_value = {
            "indexed": 1,
            "removed": 0,
            "failed": 0,
        }
        stats = prv.update_raw_index_changes(
            raw_dir,
            changed_paths=("codex/2026-06-28/chunk.md",),
            deleted_paths=(),
        )

    assert stats == {"indexed": 1, "removed": 0, "failed": 0}
    assert index_class.call_args.kwargs["db_path"] == raw_dir / ".raw_index.db"


def test_incremental_raw_index_does_not_initialize_a_second_raw_store(
    tmp_path: Path,
) -> None:
    raw_dir = tmp_path / "raw"
    chunk = raw_dir / "codex" / "2026-06-28" / "chunk.md"
    chunk.parent.mkdir(parents=True)
    chunk.write_text(
        "---\nmnemos_type: raw_retention_projection\n---\n# Chunk\ncontent",
        encoding="utf-8",
    )

    stats = prv.update_raw_index_changes(
        raw_dir,
        changed_paths=("codex/2026-06-28/chunk.md",),
        deleted_paths=(),
    )

    assert stats == {"indexed": 1, "removed": 0, "failed": 0}
    assert not (raw_dir / "raw_events.db").exists()


def test_index_write_set_reads_nonempty_wal_through_stable_temporary_snapshot(
    tmp_path: Path,
) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    index_path = raw_dir / ".raw_index.db"
    tags = (
        "raw-retention-projection",
        "source=codex",
        "canonical=raw_events",
    )
    content = (
        "---\n"
        "mnemos_type: raw_retention_projection\n"
        "session_id: wal-session\n"
        "source: codex\n"
        "tags: [raw-retention-projection, source=codex, canonical=raw_events]\n"
        "---\n"
        "# WAL-backed current index\n"
    )
    target = raw_dir / "codex" / "day" / "chunk.md"
    target.parent.mkdir(parents=True)
    target.write_text(content, encoding="utf-8")
    index = prv.RawIndex(
        raw_dir=raw_dir,
        db_path=index_path,
        raw_event_store=False,
    )
    try:
        assert index.index_file(target) is True
        connection = index._connect()  # noqa: SLF001
        connection.execute(
            "UPDATE raw_index SET indexed_at=indexed_at WHERE file_path=?",
            ("codex/day/chunk.md",),
        )
        connection.commit()
        assert Path(f"{index_path}-wal").stat().st_size > 0

        def epoch() -> dict[str, tuple[int, int, int, str] | None]:
            result: dict[str, tuple[int, int, int, str] | None] = {}
            for suffix in ("", "-wal", "-shm"):
                path = Path(f"{index_path}{suffix}")
                if not path.exists():
                    result[suffix] = None
                    continue
                metadata = path.stat()
                result[suffix] = (
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            return result

        before = epoch()
        artifact = prv.ProjectionArtifact(
            relative_path="codex/day/chunk.md",
            text=content,
            sha256=hashlib.sha256(content.encode()).hexdigest(),
            event_ids=("rawrev-" + "a" * 40,),
            logical_event_ids=("b" * 32,),
            revision_set_hash="c" * 64,
            source_agent="codex",
            session_id="wal-session",
            tags=tags,
        )
        expected_state_hash = prv._sha256_text(  # noqa: SLF001
            json.dumps(
                prv._expected_index_state(artifact, raw_dir),  # noqa: SLF001
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        (
            changed,
            deleted,
            preimage_hash,
            state_preimages,
            orphan_counts,
            schema_state,
            schema_signature_hash,
            missing_objects,
        ) = prv._raw_index_write_set(  # noqa: SLF001
            raw_dir,
            {"codex/day/chunk.md": expected_state_hash},
            index_db_path=index_path,
        )

        assert changed == []
        assert deleted == []
        assert orphan_counts == {"raw_fts": 0, "raw_tags": 0}
        assert schema_state == "canonical"
        assert len(schema_signature_hash) == 64
        assert missing_objects == []
        assert len(preimage_hash) == 64
        assert len(state_preimages["codex/day/chunk.md"]) == 64
        assert epoch() == before
    finally:
        index.close()


def test_index_write_set_streams_rows_without_fetchall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    index_path = raw_dir / ".raw_index.db"
    relative_path = "codex/day/chunk.md"
    content = (
        "---\n"
        "mnemos_type: raw_retention_projection\n"
        "session_id: stream\n"
        "source: codex\n"
        "tags: [raw-retention-projection]\n"
        "---\n"
        "body"
    )
    target = raw_dir / relative_path
    target.parent.mkdir(parents=True)
    target.write_text(content, encoding="utf-8")
    index = prv.RawIndex(
        raw_dir=raw_dir,
        db_path=index_path,
        raw_event_store=False,
    )
    try:
        assert index.index_file(target) is True
    finally:
        index.close()

    expected_state = raw_index_content_state(
        raw_dir,
        relative_path,
        content,
    )
    expected_hash = prv._sha256_text(  # noqa: SLF001
        json.dumps(
            expected_state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )

    original_connect = plan_runtime.connect_readonly_sqlite

    class StreamingCursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def __iter__(self):
            yield from self.cursor

        def fetchall(self):
            raise AssertionError("index inspection must remain streaming")

    class Connection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, query):
            return StreamingCursor(self.connection.execute(query))

        def close(self):
            self.connection.close()

    monkeypatch.setattr(
        plan_runtime,
        "connect_readonly_sqlite",
        lambda *args, **kwargs: Connection(original_connect(*args, **kwargs)),
    )

    (
        changed,
        deleted,
        _preimage_hash,
        _state_preimages,
        orphan_counts,
        schema_state,
        schema_signature_hash,
        missing_objects,
    ) = prv._raw_index_write_set(  # noqa: SLF001
        raw_dir,
        {relative_path: expected_hash},
        index_db_path=index_path,
    )

    assert changed == []
    assert deleted == []
    assert orphan_counts == {"raw_fts": 0, "raw_tags": 0}
    assert schema_state == "canonical"
    assert len(schema_signature_hash) == 64
    assert missing_objects == []


def test_projection_plan_detects_partial_fts_or_tag_index_generation(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="index-consumers",
            turn_number=0,
            user_content="all consumers",
            assistant_content="must commit",
        )
        chunks = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )
        args = Namespace(backup_dir="", max_turn_chars=0)
        stats = _planned_stats(raw_dir, store, chunks, db_path)
        prv.apply_projection(args, store, chunks, stats)
        index_path = raw_dir / ".raw_index.db"
        with sqlite3.connect(index_path) as conn:
            conn.execute("DELETE FROM raw_tags")
            conn.execute("DELETE FROM raw_fts")
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        replay_plan = prv.build_projection_plan(
            raw_dir,
            store,
            chunks,
            db_path=db_path,
            max_turn_chars=0,
        )
        assert len(replay_plan["index_changed_paths"]) == 1
        assert replay_plan["write_set_empty"] is False
    finally:
        store.close()


def test_projection_plan_hashes_the_complete_raw_index_preimage(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    index_path = raw_dir / ".raw_index.db"
    index = prv.RawIndex(
        raw_dir=raw_dir,
        db_path=index_path,
        raw_event_store=False,
    )
    index.close()
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        chunks: list[prv.ProjectionChunk] = []
        baseline = prv.build_projection_plan(
            raw_dir,
            store,
            chunks,
            db_path=db_path,
            max_turn_chars=0,
        )
        with sqlite3.connect(index_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO raw_index(
                    file_path, abs_path, session_id, content, source, tags, mtime
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "third-party/note.md",
                    str(raw_dir / "third-party" / "note.md"),
                    "third-party-session",
                    "unrelated note",
                    "third-party",
                    '["unrelated"]',
                    1.0,
                ),
            )
            row_id = int(cursor.lastrowid)
            conn.execute(
                """
                INSERT INTO raw_fts(rowid, content, session_id, source)
                VALUES (?, ?, ?, ?)
                """,
                (row_id, "unrelated note", "third-party-session", "third-party"),
            )
            conn.execute(
                "INSERT INTO raw_tags(file_path, tag) VALUES (?, ?)",
                ("third-party/note.md", "unrelated"),
            )
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        current = prv.build_projection_plan(
            raw_dir,
            store,
            chunks,
            db_path=db_path,
            max_turn_chars=0,
        )

        assert current["index_preimage_hash"] != baseline["index_preimage_hash"]
        assert current["index_orphan_row_counts"] == {"raw_fts": 0, "raw_tags": 0}
    finally:
        store.close()


def test_projection_apply_repairs_planned_orphan_index_consumers(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    index_path = raw_dir / ".raw_index.db"
    index = prv.RawIndex(
        raw_dir=raw_dir,
        db_path=index_path,
        raw_event_store=False,
    )
    index.close()
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        chunks: list[prv.ProjectionChunk] = []
        args = Namespace(backup_dir="", max_turn_chars=0)
        initial = _planned_stats(raw_dir, store, chunks, db_path)
        prv.apply_projection(args, store, chunks, initial)

        with sqlite3.connect(index_path) as conn:
            conn.execute(
                """
                INSERT INTO raw_fts(rowid, content, session_id, source)
                VALUES (?, ?, ?, ?)
                """,
                (999, "orphan fts", "ghost-session", "ghost"),
            )
            conn.execute(
                "INSERT INTO raw_tags(file_path, tag) VALUES (?, ?)",
                ("ghost.md", "orphan"),
            )
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        stats = _planned_stats(raw_dir, store, chunks, db_path)
        plan = stats["projection_plan"]
        assert plan["changed_paths"] == []
        assert plan["stale_paths"] == []
        assert plan["index_changed_paths"] == []
        assert plan["index_deleted_paths"] == []
        assert plan["index_orphan_row_counts"] == {"raw_fts": 1, "raw_tags": 1}
        assert plan["write_set_empty"] is False

        applied = prv.apply_projection(args, store, chunks, stats)

        assert applied["post_apply_zero_delta"] is True
        assert applied["raw_index"]["orphan_fts_removed"] == 1
        assert applied["raw_index"]["orphan_tags_removed"] == 1
        with sqlite3.connect(index_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM raw_fts").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM raw_tags").fetchone()[0] == 0
    finally:
        store.close()


def test_projection_apply_repairs_a_partial_index_schema(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    index_path = raw_dir / ".raw_index.db"
    with sqlite3.connect(index_path) as conn:
        conn.execute(
            """
            CREATE TABLE raw_tags(
                file_path TEXT NOT NULL,
                tag TEXT NOT NULL,
                PRIMARY KEY(file_path, tag)
            )
            """
        )
        conn.execute(
            "INSERT INTO raw_tags(file_path, tag) VALUES (?, ?)",
            ("partial.md", "orphan"),
        )
        conn.commit()

    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        chunks: list[prv.ProjectionChunk] = []
        args = Namespace(backup_dir="", max_turn_chars=0)
        stats = _planned_stats(raw_dir, store, chunks, db_path)
        plan = stats["projection_plan"]

        assert plan["index_schema_state"] == "partial"
        assert plan["index_schema_missing_objects"] == [
            "index:idx_raw_date",
            "index:idx_raw_mtime",
            "index:idx_raw_session",
            "index:idx_raw_tags_file",
            "index:idx_raw_tags_tag",
            "table:raw_fts",
            "table:raw_index",
        ]
        assert plan["index_orphan_row_counts"] == {"raw_fts": 0, "raw_tags": 1}
        assert plan["write_set_empty"] is False

        applied = prv.apply_projection(args, store, chunks, stats)

        assert applied["post_apply_zero_delta"] is True
        with sqlite3.connect(index_path) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE name IN ('raw_index', 'raw_fts', 'raw_tags')
                    """
                )
            }
            assert tables == {"raw_index", "raw_fts", "raw_tags"}
            assert conn.execute("SELECT COUNT(*) FROM raw_tags").fetchone()[0] == 0
    finally:
        store.close()


def test_projection_apply_repairs_an_existing_empty_index_schema(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    index_path = raw_dir / ".raw_index.db"
    sqlite3.connect(index_path).close()

    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        chunks: list[prv.ProjectionChunk] = []
        args = Namespace(backup_dir="", max_turn_chars=0)
        stats = _planned_stats(raw_dir, store, chunks, db_path)
        plan = stats["projection_plan"]

        assert plan["index_schema_state"] == "absent"
        assert plan["index_schema_missing_objects"] == [
            "index:idx_raw_date",
            "index:idx_raw_mtime",
            "index:idx_raw_session",
            "index:idx_raw_tags_file",
            "index:idx_raw_tags_tag",
            "table:raw_fts",
            "table:raw_index",
            "table:raw_tags",
        ]
        assert plan["write_set_empty"] is False

        applied = prv.apply_projection(args, store, chunks, stats)

        assert applied["post_apply_zero_delta"] is True
        with sqlite3.connect(index_path) as connection:
            assert raw_index_projection_snapshot(connection)["schema"]["state"] == "canonical"
    finally:
        store.close()


def test_projection_plan_validator_rejects_absent_schema_without_full_repair_set(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    sqlite3.connect(raw_dir / ".raw_index.db").close()
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        plan = prv.build_projection_plan(
            raw_dir,
            store,
            [],
            db_path=db_path,
            max_turn_chars=0,
        )
        forged = dict(plan)
        forged["index_schema_missing_objects"] = []
        _resign_plan(forged)

        with pytest.raises(
            RuntimeError,
            match="malformed or tampered",
        ):
            prv.validate_projection_plan(forged)
    finally:
        store.close()


def test_raw_index_orphan_cleanup_rolls_back_all_consumers_on_late_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    index_path = raw_dir / ".raw_index.db"
    index = prv.RawIndex(
        raw_dir=raw_dir,
        db_path=index_path,
        raw_event_store=False,
    )
    index.close()
    with sqlite3.connect(index_path) as conn:
        conn.execute(
            """
            INSERT INTO raw_fts(rowid, content, session_id, source)
            VALUES (999, 'orphan fts', 'ghost-session', 'ghost')
            """
        )
        conn.execute(
            "INSERT INTO raw_tags(file_path, tag) VALUES ('ghost.md', 'orphan')"
        )
        conn.commit()

    index = prv.RawIndex(
        raw_dir=raw_dir,
        db_path=index_path,
        raw_event_store=False,
    )
    try:
        def fail_after_first_consumer(connection):
            connection.execute(
                "DELETE FROM raw_fts "
                "WHERE rowid NOT IN (SELECT id FROM raw_index)"
            )
            raise RuntimeError("injected late cleanup failure")

        monkeypatch.setattr(
            prv.RawIndex,
            "_remove_orphan_rows_in_transaction",
            staticmethod(fail_after_first_consumer),
        )
        with pytest.raises(RuntimeError, match="injected late cleanup failure"):
            index.remove_orphan_rows()
    finally:
        index.close()

    with sqlite3.connect(index_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM raw_fts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM raw_tags").fetchone()[0] == 1


@pytest.mark.parametrize(
    ("mutation_sql", "expected_detail"),
    [
        (
            """
            DROP TABLE raw_fts;
            CREATE TABLE raw_fts(
                content TEXT,
                session_id TEXT,
                source TEXT
            );
            """,
            "raw_fts:not_canonical_fts5",
        ),
        (
            """
            DROP INDEX idx_raw_session;
            CREATE INDEX idx_raw_session ON raw_index(date);
            """,
            "idx_raw_session:definition",
        ),
        (
            """
            DROP INDEX idx_raw_tags_file;
            DROP INDEX idx_raw_tags_tag;
            DROP TABLE raw_tags;
            CREATE TABLE raw_tags(file_path TEXT, tag TEXT);
            """,
            "raw_tags:columns",
        ),
        (
            """
            DROP TABLE raw_index;
            CREATE TABLE raw_index(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL UNIQUE,
                abs_path TEXT NOT NULL,
                session_id TEXT,
                date TEXT,
                created_at TEXT,
                content TEXT,
                frontmatter TEXT,
                turn_number INTEGER,
                source TEXT,
                tags TEXT,
                mtime REAL,
                indexed_at REAL DEFAULT (unixepoch()),
                CHECK(turn_number IS NULL OR turn_number >= 0)
            );
            CREATE INDEX idx_raw_session ON raw_index(session_id);
            CREATE INDEX idx_raw_date ON raw_index(date);
            CREATE INDEX idx_raw_mtime ON raw_index(mtime);
            """,
            "raw_index:definition",
        ),
        (
            """
            DROP INDEX idx_raw_session;
            CREATE INDEX idx_raw_session ON raw_index(session_id)
            WHERE session_id IS NOT NULL;
            """,
            "idx_raw_session:definition",
        ),
        (
            """
            DROP INDEX idx_raw_tags_file;
            DROP INDEX idx_raw_tags_tag;
            DROP TABLE raw_tags;
            CREATE TABLE raw_tags(
                file_path TEXT NOT NULL,
                tag TEXT NOT NULL,
                PRIMARY KEY(file_path, tag)
            ) WITHOUT ROWID;
            CREATE INDEX idx_raw_tags_tag ON raw_tags(tag);
            CREATE INDEX idx_raw_tags_file ON raw_tags(file_path);
            """,
            "raw_tags:definition",
        ),
    ],
)
def test_projection_plan_rejects_counterfeit_same_name_index_schema(
    tmp_path: Path,
    mutation_sql: str,
    expected_detail: str,
) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    index_path = raw_dir / ".raw_index.db"
    index = prv.RawIndex(
        raw_dir=raw_dir,
        db_path=index_path,
        raw_event_store=False,
    )
    index.close()
    with sqlite3.connect(index_path) as connection:
        connection.executescript(mutation_sql)
        connection.commit()

    store = RawEventStore(
        db_path=tmp_path / "raw_events.db",
        config=_Cfg(tmp_path),
    )
    try:
        with pytest.raises(
            RuntimeError,
            match=expected_detail,
        ):
            _planned_stats(raw_dir, store, [], store.db_path)
    finally:
        store.close()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("abs_path", "/counterfeit/raw/path.md"),
        ("date", "1900-01-01"),
        ("created_at", "1900-01-01T00:00"),
        ("frontmatter", "---\nsource: counterfeit\n---"),
        ("turn_number", 999),
    ],
)
def test_projection_plan_detects_each_search_visible_raw_index_column_drift(
    tmp_path: Path,
    column: str,
    value: object,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="column-drift",
            turn_number=0,
            user_content="canonical",
            assistant_content="metadata",
        )
        chunks = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )
        args = Namespace(backup_dir="", max_turn_chars=0)
        initial = _planned_stats(raw_dir, store, chunks, db_path)
        prv.apply_projection(args, store, chunks, initial)
        baseline = prv.build_projection_plan(
            raw_dir,
            store,
            chunks,
            db_path=db_path,
            max_turn_chars=0,
        )
        relative_path = next(iter(baseline["desired_index_state_hashes"]))
        with sqlite3.connect(raw_dir / ".raw_index.db") as connection:
            connection.execute(
                f"UPDATE raw_index SET {column} = ? WHERE file_path = ?",
                (value, relative_path),
            )
            connection.commit()

        drifted = prv.build_projection_plan(
            raw_dir,
            store,
            chunks,
            db_path=db_path,
            max_turn_chars=0,
        )

        assert drifted["index_preimage_hash"] != baseline["index_preimage_hash"]
        assert drifted["index_changed_paths"] == [relative_path]
        assert drifted["write_set_empty"] is False
    finally:
        store.close()


@pytest.mark.parametrize("late_write_kind", ["valid_row", "extra_orphan"])
def test_atomic_index_apply_rejects_late_complete_preimage_drift_before_effects(
    tmp_path: Path,
    late_write_kind: str,
) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    index_path = raw_dir / ".raw_index.db"
    index = prv.RawIndex(
        raw_dir=raw_dir,
        db_path=index_path,
        raw_event_store=False,
    )
    index.close()
    with sqlite3.connect(index_path) as connection:
        connection.execute(
            """
            INSERT INTO raw_fts(rowid, content, session_id, source)
            VALUES (900, 'reviewed orphan', 'reviewed', 'ghost')
            """
        )
        connection.execute(
            "INSERT INTO raw_tags(file_path, tag) "
            "VALUES ('reviewed-orphan.md', 'reviewed')"
        )
        connection.commit()

    store = RawEventStore(
        db_path=tmp_path / "raw_events.db",
        config=_Cfg(tmp_path),
    )
    try:
        plan = prv.build_projection_plan(
            raw_dir,
            store,
            [],
            db_path=store.db_path,
            max_turn_chars=0,
        )
        assert plan["index_orphan_row_counts"] == {
            "raw_fts": 1,
            "raw_tags": 1,
        }

        with sqlite3.connect(index_path) as connection:
            if late_write_kind == "valid_row":
                cursor = connection.execute(
                    """
                    INSERT INTO raw_index(
                        file_path, abs_path, session_id, content,
                        source, tags, mtime
                    )
                    VALUES (
                        'late-valid.md', '/late-valid.md', 'late',
                        'late valid', 'foreign', '[]', 1.0
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO raw_fts(rowid, content, session_id, source)
                    VALUES (?, 'late valid', 'late', 'foreign')
                    """,
                    (int(cursor.lastrowid),),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO raw_fts(rowid, content, session_id, source)
                    VALUES (901, 'late orphan', 'late', 'ghost')
                    """
                )
                connection.execute(
                    "INSERT INTO raw_tags(file_path, tag) "
                    "VALUES ('late-orphan.md', 'late')"
                )
            connection.commit()

        with pytest.raises(
            RuntimeError,
            match="complete_preimage_changed_before_apply",
        ):
            prv.update_raw_index_changes(
                raw_dir,
                changed_paths=plan["index_changed_paths"],
                deleted_paths=plan["index_deleted_paths"],
                cleanup_orphans=True,
                index_db_path=index_path,
                expected_preimage_hash=plan["index_preimage_hash"],
                expected_schema_state=plan["index_schema_state"],
                expected_schema_signature_hash=plan[
                    "index_schema_signature_hash"
                ],
                expected_orphan_counts=plan["index_orphan_row_counts"],
                expected_post_state_hashes={},
            )

        with sqlite3.connect(index_path) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM raw_fts WHERE rowid = 900"
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT COUNT(*) FROM raw_tags "
                "WHERE file_path = 'reviewed-orphan.md'"
            ).fetchone()[0] == 1
            if late_write_kind == "valid_row":
                assert connection.execute(
                    "SELECT COUNT(*) FROM raw_index "
                    "WHERE file_path = 'late-valid.md'"
                ).fetchone()[0] == 1
            else:
                assert connection.execute(
                    "SELECT COUNT(*) FROM raw_fts WHERE rowid = 901"
                ).fetchone()[0] == 1
                assert connection.execute(
                    "SELECT COUNT(*) FROM raw_tags "
                    "WHERE file_path = 'late-orphan.md'"
                ).fetchone()[0] == 1
    finally:
        store.close()


def test_partial_schema_orphan_failure_rolls_back_then_recovers_to_zero_delta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    index_path = raw_dir / ".raw_index.db"
    with sqlite3.connect(index_path) as connection:
        connection.executescript(
            """
            CREATE TABLE raw_tags(
                file_path TEXT NOT NULL,
                tag TEXT NOT NULL,
                PRIMARY KEY(file_path, tag)
            );
            CREATE INDEX idx_raw_tags_tag ON raw_tags(tag);
            CREATE INDEX idx_raw_tags_file ON raw_tags(file_path);
            INSERT INTO raw_tags(file_path, tag)
            VALUES ('reviewed-orphan.md', 'reviewed');
            """
        )
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="partial-schema-recovery",
            turn_number=0,
            user_content="publish before index",
            assistant_content="then recover",
        )
        chunks = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )
        args = Namespace(backup_dir="", max_turn_chars=0)
        stats = _planned_stats(raw_dir, store, chunks, db_path)
        assert stats["projection_plan"]["index_schema_state"] == "partial"
        original_cleanup = (
            prv.RawIndex._remove_orphan_rows_in_transaction
        )

        def fail_after_cleanup(connection):
            original_cleanup(connection)
            raise RuntimeError("late combined index failure")

        with monkeypatch.context() as injected:
            injected.setattr(
                prv.RawIndex,
                "_remove_orphan_rows_in_transaction",
                staticmethod(fail_after_cleanup),
            )
            with pytest.raises(
                RuntimeError,
                match="late combined index failure",
            ):
                prv.apply_projection(args, store, chunks, stats)

        with sqlite3.connect(index_path) as connection:
            user_objects = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE name NOT LIKE 'sqlite_%'
                    """
                )
            }
            assert user_objects == {
                "raw_tags",
                "idx_raw_tags_tag",
                "idx_raw_tags_file",
            }
            assert connection.execute(
                "SELECT COUNT(*) FROM raw_tags"
            ).fetchone()[0] == 1
        assert (raw_dir / prv.PROJECTION_TRANSACTION_DIR).is_dir()

        recovery = prv.recover_interrupted_projection(raw_dir)
        assert recovery["recovered"] is True
        replay_stats = _planned_stats(raw_dir, store, chunks, db_path)
        replay = prv.apply_projection(
            args,
            store,
            chunks,
            replay_stats,
        )
        final_plan = prv.build_projection_plan(
            raw_dir,
            store,
            chunks,
            db_path=db_path,
            max_turn_chars=0,
        )
        assert replay["post_apply_zero_delta"] is True
        assert final_plan["write_set_empty"] is True
    finally:
        store.close()


def test_multi_path_index_failure_rolls_back_whole_generation_then_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        for session_id in ("atomic-index-a", "atomic-index-b"):
            store.upsert_turn(
                source_agent="codex",
                session_id=session_id,
                turn_number=0,
                user_content=session_id,
                assistant_content="atomic",
            )
        chunks = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )
        args = Namespace(backup_dir="", max_turn_chars=0)
        stats = _planned_stats(raw_dir, store, chunks, db_path)
        original_index_file = prv.RawIndex._index_file
        calls = {"count": 0}

        def fail_after_second_path(index, path, cursor, **kwargs):
            calls["count"] += 1
            result = original_index_file(
                index,
                path,
                cursor,
                **kwargs,
            )
            return result if calls["count"] < 2 else False

        with monkeypatch.context() as injected:
            injected.setattr(
                prv.RawIndex,
                "_index_file",
                fail_after_second_path,
            )
            with pytest.raises(
                RuntimeError,
                match="raw_index_projection_path_failed",
            ):
                prv.apply_projection(args, store, chunks, stats)

        index_path = raw_dir / ".raw_index.db"
        with sqlite3.connect(index_path) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE name IN ('raw_index', 'raw_fts', 'raw_tags')"
            ).fetchone()[0] == 0
        assert (raw_dir / prv.PROJECTION_TRANSACTION_DIR).is_dir()

        recovery = prv.recover_interrupted_projection(raw_dir)
        assert recovery["recovered"] is True
        replay_stats = _planned_stats(raw_dir, store, chunks, db_path)
        replay = prv.apply_projection(
            args,
            store,
            chunks,
            replay_stats,
        )
        final_plan = prv.build_projection_plan(
            raw_dir,
            store,
            chunks,
            db_path=db_path,
            max_turn_chars=0,
        )
        assert replay["post_apply_zero_delta"] is True
        assert final_plan["write_set_empty"] is True
    finally:
        store.close()


def test_atomic_index_apply_rejects_unplanned_row_drift_and_rolls_back_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    managed = raw_dir / "managed.md"
    protected = raw_dir / "third-party.md"
    managed.write_text("---\nsession_id: managed\ndate: 2026-07-29\n---\nold", encoding="utf-8")
    protected.write_text(
        "---\nsession_id: third-party\ndate: 2026-07-28\n---\nprotected",
        encoding="utf-8",
    )
    index = prv.RawIndex(
        raw_dir=raw_dir,
        db_path=raw_dir / ".raw_index.db",
        raw_event_store=False,
    )
    try:
        assert index.index_file(managed) is True
        assert index.index_file(protected) is True
        connection = index._connect()  # noqa: SLF001
        before = raw_index_projection_snapshot(connection)
        protected_before = connection.execute(
            """
            SELECT date, content
            FROM raw_index
            WHERE file_path = 'third-party.md'
            """
        ).fetchone()
        managed_before = connection.execute(
            "SELECT content FROM raw_index WHERE file_path = 'managed.md'"
        ).fetchone()[0]

        managed.write_text(
            "---\nsession_id: managed\ndate: 2026-07-30\n---\nnew",
            encoding="utf-8",
        )
        desired_state = raw_index_content_state(
            raw_dir,
            "managed.md",
            managed.read_text(encoding="utf-8"),
        )
        desired_hash = hashlib.sha256(
            json.dumps(
                desired_state,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        original_index_file = prv.RawIndex._index_file

        def corrupt_unplanned_row(index_owner, path, cursor, **kwargs):
            result = original_index_file(index_owner, path, cursor, **kwargs)
            cursor.execute(
                "UPDATE raw_index SET date = '1900-01-01' "
                "WHERE file_path = 'third-party.md'"
            )
            return result

        monkeypatch.setattr(
            prv.RawIndex,
            "_index_file",
            corrupt_unplanned_row,
        )
        with pytest.raises(
            RuntimeError,
            match="unplanned_preimage_changed_during_apply",
        ):
            index.apply_projection_write_set(
                changed_paths=("managed.md",),
                deleted_paths=(),
                cleanup_orphans=False,
                expected_preimage_hash=before["preimage_hash"],
                expected_schema_state=before["schema"]["state"],
                expected_schema_signature_hash=before["schema"]["signature_hash"],
                expected_orphan_counts=before["orphan_counts"],
                expected_post_state_hashes={"managed.md": desired_hash},
            )

        assert connection.execute(
            """
            SELECT date, content
            FROM raw_index
            WHERE file_path = 'third-party.md'
            """
        ).fetchone() == protected_before
        assert connection.execute(
            "SELECT content FROM raw_index WHERE file_path = 'managed.md'"
        ).fetchone()[0] == managed_before
    finally:
        index.close()


def test_incremental_raw_index_rejects_ambiguous_database_arguments(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="only one"):
        prv.update_raw_index_changes(
            tmp_path / "raw",
            changed_paths=(),
            deleted_paths=(),
            index_db_path=tmp_path / "one.db",
            db_path=tmp_path / "two.db",
        )


def test_canonical_raw_rejects_a_truncating_profile(tmp_path: Path) -> None:
    store = RawEventStore(db_path=tmp_path / "raw_events.db", config=_Cfg(tmp_path))
    try:
        chunk = prv.ProjectionChunk(
            source_agent="codex",
            session_id="sess",
            chunk_index=0,
            refs=[_ref(event_id="missing", session_id="sess")],
        )
        with pytest.raises(ValueError, match="max-turn-chars=0"):
            prv.render_chunk(
                store,
                chunk,
                db_path=tmp_path / "raw_events.db",
                max_turn_chars=1,
            )
    finally:
        store.close()


def test_incremental_projection_rewrites_only_changed_chunk_and_keeps_prior_generation_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="session-a",
            turn_number=0,
            user_content="first-a",
            assistant_content="answer-a",
            timestamp="2026-06-28T10:00:00",
        )
        store.upsert_turn(
            source_agent="codex",
            session_id="session-b",
            turn_number=0,
            user_content="first-b",
            assistant_content="answer-b",
            timestamp="2026-06-28T10:01:00",
        )
        chunks = prv.build_projection_chunks(prv._fetch_refs(store), chunk_turns=1, max_chunks=None)
        first = prv.write_projection(raw_dir, store, chunks, db_path=db_path, max_turn_chars=0)
        assert first["written_files"] == 2
        session_b_file = next(path for path in raw_dir.rglob("*.md") if "session-b" in path.name)
        session_b_mtime = session_b_file.stat().st_mtime_ns
        unrelated = raw_dir / "user-note.md"
        unrelated.write_text("do not move", encoding="utf-8")
        unrelated_mtime = unrelated.stat().st_mtime_ns

        store.upsert_turn(
            source_agent="codex",
            session_id="session-a",
            turn_number=0,
            user_content="updated-a",
            assistant_content="answer-a",
            timestamp="2026-06-28T10:00:00",
        )
        changed_chunks = prv.build_projection_chunks(
            prv._fetch_refs(store), chunk_turns=1, max_chunks=None
        )
        second = prv.write_projection(
            raw_dir, store, changed_chunks, db_path=db_path, max_turn_chars=0
        )
        assert second["written_files"] == 1
        assert second["unchanged_files"] == 1
        assert second["deleted_stale_files"] == 0
        assert second["journal_written"] is True
        assert second["unrelated_files_moved"] == 0
        assert session_b_file.stat().st_mtime_ns == session_b_mtime
        assert unrelated.read_text(encoding="utf-8") == "do not move"
        assert unrelated.stat().st_mtime_ns == unrelated_mtime

        no_change = prv.write_projection(
            raw_dir, store, changed_chunks, db_path=db_path, max_turn_chars=0
        )
        assert no_change["written_files"] == 0
        assert no_change["journal_written"] is False

        full_raw_dir = tmp_path / "raw-full-comparator"
        full = prv.write_projection(
            full_raw_dir, store, changed_chunks, db_path=db_path, max_turn_chars=0
        )
        incremental_journal = json.loads(
            (raw_dir / prv.PROJECTION_JOURNAL_NAME).read_text(encoding="utf-8")
        )
        full_journal = json.loads(
            (full_raw_dir / prv.PROJECTION_JOURNAL_NAME).read_text(encoding="utf-8")
        )
        assert full["written_files"] == 2
        assert incremental_journal["generation_hash"] == full_journal["generation_hash"]
        assert incremental_journal["files"] == full_journal["files"]

        target = next(path for path in raw_dir.rglob("*.md") if "session-b" in path.name)
        previous_generation = target.read_bytes()
        store.upsert_turn(
            source_agent="codex",
            session_id="session-b",
            turn_number=0,
            user_content="updated-b",
            assistant_content="answer-b",
            timestamp="2026-06-28T10:01:00",
        )
        failure_chunks = prv.build_projection_chunks(
            prv._fetch_refs(store), chunk_turns=1, max_chunks=None
        )
        monkeypatch.setattr(
            prv,
            "_secure_publish_staged_file",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("crash")),
        )
        with pytest.raises(OSError, match="crash"):
            prv.write_projection(raw_dir, store, failure_chunks, db_path=db_path, max_turn_chars=0)
        assert target.read_bytes() == previous_generation
    finally:
        store.close()


def test_multi_chunk_publish_exception_retains_and_rolls_forward_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        for session_id in ("rollback-a", "rollback-b"):
            store.upsert_turn(
                source_agent="codex",
                session_id=session_id,
                turn_number=0,
                user_content=f"old-{session_id}",
                assistant_content="stable",
            )
        initial_chunks = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )
        prv.write_projection(
            raw_dir,
            store,
            initial_chunks,
            db_path=db_path,
            max_turn_chars=0,
        )
        for session_id in ("rollback-a", "rollback-b"):
            store.upsert_turn(
                source_agent="codex",
                session_id=session_id,
                turn_number=0,
                user_content=f"new-{session_id}",
                assistant_content="stable",
            )
        changed_chunks = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )
        real_publish = prv._secure_publish_staged_file  # noqa: SLF001
        calls = {"count": 0}

        def fail_second_replace(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("crash during second chunk")
            return real_publish(*args, **kwargs)

        monkeypatch.setattr(prv, "_secure_publish_staged_file", fail_second_replace)
        with pytest.raises(OSError, match="second chunk"):
            prv.write_projection(
                raw_dir,
                store,
                changed_chunks,
                db_path=db_path,
                max_turn_chars=0,
            )

        assert (raw_dir / prv.PROJECTION_TRANSACTION_DIR).is_dir()
        recovery = prv.recover_interrupted_projection(raw_dir)
        assert recovery["recovered"] is True
        assert not (raw_dir / prv.PROJECTION_TRANSACTION_DIR).exists()
        for path in raw_dir.rglob("*.md"):
            assert "new-rollback-" in path.read_text(encoding="utf-8")
        post_recovery_plan = prv.build_projection_plan(
            raw_dir,
            store,
            changed_chunks,
            db_path=db_path,
            max_turn_chars=0,
        )
        assert post_recovery_plan["changed_paths"] == []
        assert post_recovery_plan["stale_paths"] == []
        assert post_recovery_plan["journal_write"] is False
    finally:
        store.close()


def test_process_termination_mid_publish_is_reconciled_to_one_generation_on_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        for session_id in ("termination-a", "termination-b"):
            store.upsert_turn(
                source_agent="codex",
                session_id=session_id,
                turn_number=0,
                user_content=f"old-{session_id}",
                assistant_content="stable",
            )
        initial_chunks = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )
        prv.write_projection(
            raw_dir,
            store,
            initial_chunks,
            db_path=db_path,
            max_turn_chars=0,
        )
        old_journal = (raw_dir / prv.PROJECTION_JOURNAL_NAME).read_bytes()

        for session_id in ("termination-a", "termination-b"):
            store.upsert_turn(
                source_agent="codex",
                session_id=session_id,
                turn_number=0,
                user_content=f"new-{session_id}",
                assistant_content="stable",
            )
        changed_chunks = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )
        real_publish = prv._secure_publish_staged_file  # noqa: SLF001
        markdown_writes = {"count": 0}

        def terminate_before_second_chunk(*args, **kwargs) -> None:
            relative_path = str(args[1])
            if relative_path.endswith(".md"):
                markdown_writes["count"] += 1
                if markdown_writes["count"] == 2:
                    raise KeyboardInterrupt("simulated process termination")
            real_publish(*args, **kwargs)

        with monkeypatch.context() as interrupted:
            interrupted.setattr(
                prv,
                "_secure_publish_staged_file",
                terminate_before_second_chunk,
            )
            with pytest.raises(KeyboardInterrupt, match="process termination"):
                prv.write_projection(
                    raw_dir,
                    store,
                    changed_chunks,
                    db_path=db_path,
                    max_turn_chars=0,
                )

        assert (raw_dir / prv.PROJECTION_JOURNAL_NAME).read_bytes() == old_journal
        recovery = prv.recover_interrupted_projection(raw_dir)
        assert recovery["recovered"] is True
        restart_stats = _planned_stats(raw_dir, store, changed_chunks, db_path)
        assert restart_stats["projection_plan"]["write_set_empty"] is False
        restarted = prv.apply_projection(
            Namespace(backup_dir="", max_turn_chars=0),
            store,
            changed_chunks,
            restart_stats,
        )
        final_plan = prv.build_projection_plan(
            raw_dir,
            store,
            changed_chunks,
            db_path=db_path,
            max_turn_chars=0,
        )

        assert restarted["post_apply_zero_delta"] is True
        assert final_plan["write_set_empty"] is True
        assert json.loads(
            (raw_dir / prv.PROJECTION_JOURNAL_NAME).read_text(encoding="utf-8")
        )["generation_hash"] == final_plan["generation_hash"]
    finally:
        store.close()


def test_restart_adopts_durable_prepared_to_aborting_state_temp(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    backup_dir = tmp_path / "backups"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="aborting-state-temp",
            turn_number=0,
            user_content="planned receipt is durable",
            assistant_content="publication never began",
        )
        chunks = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )
        plan = prv.build_projection_plan(
            raw_dir,
            store,
            chunks,
            db_path=db_path,
            max_turn_chars=0,
            backup_dir=backup_dir,
        )
        planned_receipt = {
            "schema_version": "mnemos.raw_projection_change_set.v1",
            "status": "planned",
            "plan_hash": plan["plan_hash"],
            "generation_hash": plan["generation_hash"],
            "backup_dir": plan["backup_dir"],
            "changed_paths": plan["changed_paths"],
            "stale_paths": plan["stale_paths"],
            "index_changed_paths": plan["index_changed_paths"],
            "index_deleted_paths": plan["index_deleted_paths"],
        }

        def interrupt_after_plan_receipt() -> None:
            prv._write_change_manifest(  # noqa: SLF001
                backup_dir,
                planned_receipt,
                receipt_kind="plan",
            )
            raise KeyboardInterrupt("crash before aborting state replace")

        with pytest.raises(KeyboardInterrupt, match="aborting state replace"):
            prv.write_projection(
                raw_dir,
                store,
                chunks,
                db_path=db_path,
                max_turn_chars=0,
                transaction_backup_dir=backup_dir,
                projection_plan=plan,
                before_publish=interrupt_after_plan_receipt,
                retain_transaction=True,
            )

        transaction_dir = raw_dir / prv.PROJECTION_TRANSACTION_DIR
        state = json.loads(
            (transaction_dir / "state.json").read_text(encoding="utf-8")
        )
        assert state["status"] == "prepared"
        pending_state = {**state, "status": "aborting"}
        state_temp = transaction_dir / f".state.json.{'a' * 32}.tmp"
        state_temp.write_text(
            json.dumps(
                pending_state,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        recovery = prv.recover_interrupted_projection(
            raw_dir,
            expected_plan_hash=plan["plan_hash"],
            expected_backup_dir=backup_dir,
        )

        assert recovery["recovered"] is True
        assert recovery["recovery_action"] == "aborted_before_publish"
        assert prv.managed_projection_paths(raw_dir) == []
        assert not (raw_dir / prv.PROJECTION_JOURNAL_NAME).exists()
        assert not transaction_dir.exists()
        assert len(list(backup_dir.glob("raw-projection-plan-*.json"))) == 1
        assert len(list(backup_dir.glob("raw-projection-abort-*.json"))) == 1
        assert backup_audit._manifest_metadata(backup_dir)["valid"] is True  # noqa: SLF001
    finally:
        store.close()


def test_repeated_recovery_crashes_promote_each_durable_state_transition(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="repeated-recovery-crash",
            turn_number=0,
            user_content="survive more than one restart",
            assistant_content="without ambiguous state temps",
        )
        chunks = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )

        def stop_at_prepared_state() -> None:
            raise KeyboardInterrupt("leave a durable prepared transaction")

        with pytest.raises(KeyboardInterrupt, match="durable prepared"):
            prv.write_projection(
                raw_dir,
                store,
                chunks,
                db_path=db_path,
                max_turn_chars=0,
                before_publish=stop_at_prepared_state,
                retain_transaction=True,
            )
    finally:
        store.close()

    repo_root = Path(prv.__file__).resolve().parents[1]
    first_crash = """
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from scripts import project_raw_vault as prv
raw_dir = Path(sys.argv[2])
original_replace = prv.os.replace
def exit_before_publishing_state_replace(source, target, *args, **kwargs):
    if target == "state.json":
        source_path = raw_dir / prv.PROJECTION_TRANSACTION_DIR / str(source)
        if json.loads(source_path.read_text(encoding="utf-8"))["status"] == "publishing":
            os._exit(85)
    return original_replace(source, target, *args, **kwargs)
prv.os.replace = exit_before_publishing_state_replace
prv.recover_interrupted_projection(raw_dir)
"""
    result = subprocess.run(
        [sys.executable, "-c", first_crash, str(repo_root), str(raw_dir)],
        check=False,
    )
    assert result.returncode == 85
    transaction_dir = raw_dir / prv.PROJECTION_TRANSACTION_DIR
    assert json.loads(
        (transaction_dir / "state.json").read_text(encoding="utf-8")
    )["status"] == "prepared"
    assert len(list(transaction_dir.glob(".state.json.*.tmp"))) == 1

    second_crash = """
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from scripts import project_raw_vault as prv
raw_dir = Path(sys.argv[2])
original_replace = prv.os.replace
state_replaces = 0
def exit_before_published_state_replace(source, target, *args, **kwargs):
    global state_replaces
    if target == "state.json":
        state_replaces += 1
        if state_replaces == 2:
            source_path = raw_dir / prv.PROJECTION_TRANSACTION_DIR / str(source)
            if json.loads(source_path.read_text(encoding="utf-8"))["status"] == "published":
                os._exit(86)
    return original_replace(source, target, *args, **kwargs)
prv.os.replace = exit_before_published_state_replace
prv.recover_interrupted_projection(raw_dir)
"""
    result = subprocess.run(
        [sys.executable, "-c", second_crash, str(repo_root), str(raw_dir)],
        check=False,
    )
    assert result.returncode == 86
    assert json.loads(
        (transaction_dir / "state.json").read_text(encoding="utf-8")
    )["status"] == "publishing"
    assert len(list(transaction_dir.glob(".state.json.*.tmp"))) == 1

    recovery = prv.recover_interrupted_projection(raw_dir)
    assert recovery["recovered"] is True
    assert recovery["recovery_action"] == "rolled_forward_for_replan"
    assert not transaction_dir.exists()
    assert len(prv.managed_projection_paths(raw_dir)) == 1


def test_real_process_exit_before_publish_replace_leaves_no_vault_debris(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="publish-temp-exit",
            turn_number=0,
            user_content="staged copy",
            assistant_content="must stay transaction-local",
        )
    finally:
        store.close()
    repo_root = Path(prv.__file__).resolve().parents[1]
    child = """
import os
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from core.sync_framework.raw_event_store import RawEventStore
from scripts import project_raw_vault as prv
class Cfg:
    def __init__(self, database_dir):
        self.database_dir = database_dir
    def get(self, key, default=None):
        return default
db_path = Path(sys.argv[2])
raw_dir = Path(sys.argv[3])
store = RawEventStore(db_path=db_path, config=Cfg(db_path.parent))
chunks = prv.build_projection_chunks(prv._fetch_refs(store), chunk_turns=1, max_chunks=None)
original_replace = prv.os.replace
def exit_before_publish_replace(source, target, *args, **kwargs):
    if str(source).endswith(".publish") and str(target).endswith(".md"):
        os._exit(81)
    return original_replace(source, target, *args, **kwargs)
prv.os.replace = exit_before_publish_replace
prv.write_projection(raw_dir, store, chunks, db_path=db_path, max_turn_chars=0)
"""

    result = subprocess.run(
        [sys.executable, "-c", child, str(repo_root), str(db_path), str(raw_dir)],
        check=False,
    )

    assert result.returncode == 81
    transaction_dir = raw_dir / prv.PROJECTION_TRANSACTION_DIR
    assert transaction_dir.is_dir()
    assert [
        path
        for path in raw_dir.rglob("*.publish")
        if transaction_dir not in path.parents
    ] == []
    recovery = prv.recover_interrupted_projection(raw_dir)
    assert recovery["recovered"] is True
    assert list(raw_dir.rglob("*.publish")) == []
    assert not transaction_dir.exists()


def test_commit_receipt_failure_preserves_immutable_planned_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    backup_dir = tmp_path / "backups" / "raw-vault-projection-metadata"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="receipt-failure",
            turn_number=0,
            user_content="preserve the plan",
            assistant_content="even if commit receipt fails",
        )
        chunks = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )
        stats = _planned_stats(
            raw_dir,
            store,
            chunks,
            db_path,
            backup_dir=backup_dir,
        )
        real_write_receipt = prv._write_change_manifest  # noqa: SLF001

        def fail_commit_receipt(*args, **kwargs):
            if kwargs.get("receipt_kind") == "commit":
                raise OSError("commit receipt unavailable")
            return real_write_receipt(*args, **kwargs)

        monkeypatch.setattr(prv, "_write_change_manifest", fail_commit_receipt)
        with pytest.raises(OSError, match="commit receipt unavailable"):
            prv.apply_projection(
                Namespace(backup_dir=str(backup_dir), max_turn_chars=0),
                store,
                chunks,
                stats,
            )

        plan_receipts = list(backup_dir.glob("raw-projection-plan-*.json"))
        assert len(plan_receipts) == 1
        assert list(backup_dir.glob("raw-projection-commit-*.json")) == []
        planned = json.loads(plan_receipts[0].read_text(encoding="utf-8"))
        assert planned["status"] == "planned"
        assert planned["plan_hash"] == stats["projection_plan"]["plan_hash"]
        assert (raw_dir / prv.PROJECTION_TRANSACTION_DIR).is_dir()
        with pytest.raises(RuntimeError, match="backup scope does not match"):
            prv.recover_interrupted_projection(
                raw_dir,
                expected_backup_dir=tmp_path / "wrong-backup-scope",
            )
        recovery = prv.recover_interrupted_projection(
            raw_dir,
            expected_plan_hash=stats["projection_plan"]["plan_hash"],
            expected_backup_dir=backup_dir,
        )
        assert recovery["recovered"] is True
        assert Path(recovery["recovery_receipt_path"]).exists()
        assert not (raw_dir / prv.PROJECTION_TRANSACTION_DIR).exists()
        assert prv.build_projection_plan(
            raw_dir,
            store,
            chunks,
            db_path=db_path,
            max_turn_chars=0,
            backup_dir=backup_dir,
        )["write_set_empty"] is True
    finally:
        store.close()


def test_prepublish_failure_pairs_planned_receipt_with_abort_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    backup_dir = tmp_path / "backups"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="abort-receipt",
            turn_number=0,
            user_content="plan first",
            assistant_content="abort only before publication",
        )
        chunks = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )
        stats = _planned_stats(
            raw_dir,
            store,
            chunks,
            db_path,
            backup_dir=backup_dir,
        )
        monkeypatch.setattr(
            prv,
            "_publish_projection_transaction",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("prepublish failure")
            ),
        )

        with pytest.raises(RuntimeError, match="prepublish failure"):
            prv.apply_projection(
                Namespace(backup_dir=str(backup_dir), max_turn_chars=0),
                store,
                chunks,
                stats,
            )

        assert len(list(backup_dir.glob("raw-projection-plan-*.json"))) == 1
        assert len(list(backup_dir.glob("raw-projection-abort-*.json"))) == 1
        assert backup_audit._manifest_metadata(backup_dir)["valid"] is True  # noqa: SLF001
        assert not (raw_dir / prv.PROJECTION_TRANSACTION_DIR).exists()
        assert prv.managed_projection_paths(raw_dir) == []
    finally:
        store.close()


def test_plan_receipt_post_commit_error_still_gets_abort_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    backup_dir = tmp_path / "backups"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="plan-return-error",
            turn_number=0,
            user_content="receipt is already committed",
            assistant_content="return assignment is not the authority",
        )
        chunks = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )
        stats = _planned_stats(
            raw_dir,
            store,
            chunks,
            db_path,
            backup_dir=backup_dir,
        )
        real_write_receipt = prv._write_change_manifest  # noqa: SLF001

        def fail_after_plan_commit(*args, **kwargs):
            result = real_write_receipt(*args, **kwargs)
            if kwargs.get("receipt_kind") == "plan":
                raise OSError("injected after plan receipt commit")
            return result

        monkeypatch.setattr(
            prv,
            "_write_change_manifest",
            fail_after_plan_commit,
        )
        with pytest.raises(OSError, match="after plan receipt commit"):
            prv.apply_projection(
                Namespace(backup_dir=str(backup_dir), max_turn_chars=0),
                store,
                chunks,
                stats,
            )

        assert len(list(backup_dir.glob("raw-projection-plan-*.json"))) == 1
        assert len(list(backup_dir.glob("raw-projection-abort-*.json"))) == 1
        assert backup_audit._manifest_metadata(backup_dir)["valid"] is True  # noqa: SLF001
        assert not (raw_dir / prv.PROJECTION_TRANSACTION_DIR).exists()
        assert prv.managed_projection_paths(raw_dir) == []
    finally:
        store.close()


def test_backup_scope_symlink_swap_is_rejected_before_projection_effect(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    backup_dir = tmp_path / "backups"
    outside = tmp_path / "outside"
    backup_dir.mkdir()
    outside.mkdir()
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="receipt-scope-swap",
            turn_number=0,
            user_content="bound scope",
            assistant_content="never follow replacement symlink",
        )
        chunks = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )
        stats = _planned_stats(
            raw_dir,
            store,
            chunks,
            db_path,
            backup_dir=backup_dir,
        )
        checks = {"count": 0}

        def swap_before_planned_receipt() -> None:
            checks["count"] += 1
            if checks["count"] == 5:
                backup_dir.rename(tmp_path / "original-backups")
                backup_dir.symlink_to(outside, target_is_directory=True)

        store.assert_epoch_current = swap_before_planned_receipt
        with pytest.raises(RuntimeError, match="receipt directory is unsafe"):
            prv.apply_projection(
                Namespace(backup_dir=str(backup_dir), max_turn_chars=0),
                store,
                chunks,
                stats,
            )

        assert list(outside.iterdir()) == []
        assert prv.managed_projection_paths(raw_dir) == []
        assert not (raw_dir / prv.PROJECTION_JOURNAL_NAME).exists()
        assert not (raw_dir / prv.PROJECTION_TRANSACTION_DIR).exists()
    finally:
        store.close()


def test_receipt_temp_from_process_exit_is_cleaned_before_retry(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    backup_dir = tmp_path / "backups"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="receipt-link-exit",
            turn_number=0,
            user_content="prepared transaction",
            assistant_content="rebuild exact planned receipt",
        )
    finally:
        store.close()
    repo_root = Path(prv.__file__).resolve().parents[1]
    child = """
import os
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from argparse import Namespace
from core.sync_framework.raw_event_store import RawEventStore
from scripts import project_raw_vault as prv
class Cfg:
    def __init__(self, database_dir):
        self.database_dir = database_dir
    def get(self, key, default=None):
        return default
db_path = Path(sys.argv[2])
raw_dir = Path(sys.argv[3])
backup_dir = Path(sys.argv[4])
store = RawEventStore(db_path=db_path, config=Cfg(db_path.parent))
chunks = prv.build_projection_chunks(prv._fetch_refs(store), chunk_turns=1, max_chunks=None)
stats = {
    "raw_dir": str(raw_dir),
    "db_path": str(db_path),
    "projection_plan": prv.build_projection_plan(
        raw_dir,
        store,
        chunks,
        db_path=db_path,
        max_turn_chars=0,
        backup_dir=backup_dir,
    ),
}
prv.os.link = lambda *args, **kwargs: os._exit(82)
prv.apply_projection(
    Namespace(backup_dir=str(backup_dir), max_turn_chars=0),
    store,
    chunks,
    stats,
)
"""

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            child,
            str(repo_root),
            str(db_path),
            str(raw_dir),
            str(backup_dir),
        ],
        check=False,
    )

    assert result.returncode == 82
    assert len(list(backup_dir.glob(".*.tmp"))) == 1
    assert (raw_dir / prv.PROJECTION_TRANSACTION_DIR).is_dir()
    recovery = prv.recover_interrupted_projection(
        raw_dir,
        expected_backup_dir=backup_dir,
    )
    assert recovery["recovered"] is True
    assert list(backup_dir.glob(".*.tmp")) == []
    assert len(list(backup_dir.glob("raw-projection-plan-*.json"))) == 1
    assert len(list(backup_dir.glob("raw-projection-recovery-*.json"))) == 1
    assert backup_audit._manifest_metadata(backup_dir)["valid"] is True  # noqa: SLF001


def test_same_generation_second_publish_has_zero_filesystem_write_set(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    backup_dir = tmp_path / "backup"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="idempotent-plan",
            turn_number=0,
            user_content="stable",
            assistant_content="stable",
        )
        chunks = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )
        first = prv.write_projection(
            raw_dir,
            store,
            chunks,
            db_path=db_path,
            max_turn_chars=0,
            backup_dir=backup_dir,
        )
        assert first["written_files"] == 1

        assert not hasattr(prv, "_atomic_write_text")
        second = prv.write_projection(
            raw_dir,
            store,
            chunks,
            db_path=db_path,
            max_turn_chars=0,
            backup_dir=backup_dir,
        )

        assert second["written_files"] == 0
        assert second["deleted_stale_files"] == 0
        assert second["journal_written"] is False
        assert second["bytes_written"] == 0
    finally:
        store.close()


def test_restart_replays_missing_index_effect_after_chunks_were_published(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="index-replay",
            turn_number=0,
            user_content="publish before index",
            assistant_content="recover after restart",
        )
        chunks = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )
        args = Namespace(backup_dir="", max_turn_chars=0)
        stats = _planned_stats(raw_dir, store, chunks, db_path)

        with patch.object(
            prv,
            "update_raw_index_changes",
            side_effect=RuntimeError("crash after chunk publish"),
        ):
            with pytest.raises(RuntimeError, match="crash after chunk publish"):
                prv.apply_projection(args, store, chunks, stats)

        assert (raw_dir / prv.PROJECTION_TRANSACTION_DIR).is_dir()
        recovery = prv.recover_interrupted_projection(raw_dir)
        assert recovery["recovered"] is True
        stats = _planned_stats(raw_dir, store, chunks, db_path)
        with patch.object(
            prv,
            "update_raw_index_changes",
            wraps=prv.update_raw_index_changes,
        ) as update_index:
            replay = prv.apply_projection(args, store, chunks, stats)

        assert replay["written_files"] == 0
        assert replay["journal_written"] is False
        assert len(update_index.call_args.kwargs["changed_paths"]) == 1
    finally:
        store.close()


def test_restart_replays_stale_index_removal_after_chunk_was_deleted(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        for session_id in ("keep", "remove"):
            store.upsert_turn(
                source_agent="codex",
                session_id=session_id,
                turn_number=0,
                user_content=session_id,
                assistant_content=session_id,
            )
        chunks = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )
        args = Namespace(backup_dir="", max_turn_chars=0)
        stats = _planned_stats(raw_dir, store, chunks, db_path)
        prv.apply_projection(args, store, chunks, stats)
        removed_path = next(
            path.relative_to(raw_dir).as_posix()
            for path in raw_dir.rglob("*.md")
            if "remove" in path.name
        )
        conn = store._pool.get_conn()  # noqa: SLF001
        conn.execute(
            """
            UPDATE raw_metrics
            SET retention_state='eligible_delete'
            WHERE event_id=(
                SELECT event_id
                FROM raw_turns
                WHERE session_id='remove'
            )
            """
        )
        conn.commit()
        kept_chunks = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )

        stats = _planned_stats(raw_dir, store, kept_chunks, db_path)
        with patch.object(
            prv,
            "update_raw_index_changes",
            side_effect=RuntimeError("crash after stale chunk delete"),
        ):
            with pytest.raises(RuntimeError, match="crash after stale chunk delete"):
                prv.apply_projection(args, store, kept_chunks, stats)
        assert not (raw_dir / removed_path).exists()
        assert (raw_dir / prv.PROJECTION_TRANSACTION_DIR).is_dir()

        recovery = prv.recover_interrupted_projection(raw_dir)
        assert recovery["recovered"] is True
        stats = _planned_stats(raw_dir, store, kept_chunks, db_path)
        with patch.object(
            prv,
            "update_raw_index_changes",
            wraps=prv.update_raw_index_changes,
        ) as update_index:
            replay = prv.apply_projection(args, store, kept_chunks, stats)

        assert replay["written_files"] == 0
        assert replay["deleted_stale_files"] == 0
        assert update_index.call_args.kwargs["deleted_paths"] == [removed_path]
    finally:
        store.close()


def test_apply_fails_closed_when_any_planned_index_path_is_not_committed(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="index-failure",
            turn_number=0,
            user_content="index me",
            assistant_content="or fail",
        )
        chunks = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )
        args = Namespace(backup_dir="", max_turn_chars=0)
        stats = _planned_stats(raw_dir, store, chunks, db_path)

        with patch.object(
            prv,
            "update_raw_index_changes",
            return_value={"indexed": 0, "removed": 0, "failed": 1},
        ):
            with pytest.raises(RuntimeError, match="did not commit every planned path"):
                prv.apply_projection(args, store, chunks, stats)
    finally:
        store.close()


def test_project_raw_vault_cli_bootstraps_without_circular_import() -> None:
    repo_root = Path(prv.__file__).resolve().parents[1]

    result = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "project_raw_vault.py"),
            "--help",
        ],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--expected-plan-hash" in result.stdout


@pytest.mark.parametrize(
    "module_order",
    tuple(
        permutations(
            (
                "scripts.project_raw_vault",
                "scripts.raw_projection_secure_io",
                "scripts.raw_projection_transaction_runtime",
                "scripts.raw_projection_plan_runtime",
            )
        )
    ),
)
def test_projection_modules_import_in_any_fresh_process_order(
    module_order: tuple[str, ...],
) -> None:
    repo_root = Path(prv.__file__).resolve().parents[1]
    import_script = (
        "import importlib\n"
        f"modules = {module_order!r}\n"
        "for module in modules:\n"
        "    importlib.import_module(module)\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", import_script],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_secure_atomic_control_file_write_and_unlink_are_directory_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fsync_targets: list[str] = []
    real_fsync = os.fsync

    def recording_fsync(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        fsync_targets.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(descriptor)

    monkeypatch.setattr(secure_io.os, "fsync", recording_fsync)
    secure_io._secure_atomic_write_text(  # noqa: SLF001
        tmp_path,
        "recovery-intent.json",
        '{"plan_hash":"abc"}\n',
    )
    target = tmp_path / "recovery-intent.json"
    digest = hashlib.sha256(target.read_bytes()).hexdigest()

    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert fsync_targets[:2] == ["file", "directory"]

    assert secure_io._secure_unlink_file(  # noqa: SLF001
        tmp_path,
        target.name,
        expected_hash=digest,
    )
    assert fsync_targets[-1] == "directory"
    assert not target.exists()


def test_secure_atomic_control_file_short_write_keeps_preimage_and_no_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "recovery-intent.json"
    target.write_text('{"generation":"old"}\n', encoding="utf-8")

    monkeypatch.setattr(secure_io.os, "write", lambda _fd, _content: 0)
    with pytest.raises(OSError, match="Raw projection write made no progress"):
        secure_io._secure_atomic_write_text(  # noqa: SLF001
            tmp_path,
            target.name,
            '{"generation":"new"}\n',
        )

    assert target.read_text(encoding="utf-8") == '{"generation":"old"}\n'
    assert list(tmp_path.glob(f".{target.name}.*.tmp")) == []


# ---------------------------------------------------------------------------
# Paged projection (max_file_bytes) tests
# ---------------------------------------------------------------------------

_PAGED_NOTICE_SPLIT = "byte-hashed.\n\n"


def _paged_store(tmp_path: Path, *, turn_count: int = 5, big_field: bool = False):
    db_path = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    for turn_number in range(turn_count):
        assistant = f"assistant {turn_number}\n" + f"line-{turn_number:04d}\n" * 60
        if big_field and turn_number == 2:
            assistant = "oversized assistant\n" + "payload-%04d\n" * 500
        store.upsert_turn(
            source_agent="codex",
            session_id="paged-session",
            turn_number=turn_number,
            user_content=f"user {turn_number} " + "u" * 600,
            assistant_content=assistant,
            reasoning=f"reasoning {turn_number} " + "r" * 400,
            tool_calls=[{"name": "tool", "arguments": {"x": turn_number}}],
            timestamp=f"2026-06-28T10:0{turn_number}:00",
        )
    return db_path, store


def _paged_chunk(store):
    return prv.build_projection_chunks(
        prv._fetch_refs(store),  # noqa: SLF001
        chunk_turns=5,
        max_chunks=None,
    )[0]


def _part_cap_for(store, chunk, db_path: Path, block_count: int) -> int:
    """Cap fitting exactly ``block_count`` turn blocks per part (3 total)."""
    _preamble, blocks = prv._render_chunk_atoms(  # noqa: SLF001
        store, chunk, db_path=db_path, max_turn_chars=0
    )
    part_preamble = prv._render_part_preamble(  # noqa: SLF001
        chunk,
        prv._chunk_relative_path(chunk),  # noqa: SLF001
        db_path,
        part_index=1,
        part_count=3,
    )
    return len(part_preamble.encode("utf-8")) + sum(
        block.byte_length for block in blocks[:block_count]
    )


def _reassemble_single_text(single_text: str, parts: list[tuple[str, str]]) -> str:
    preamble = single_text[: single_text.index(_PAGED_NOTICE_SPLIT) + len(_PAGED_NOTICE_SPLIT)]
    body = ""
    for _suffix, part_text in parts[1:]:
        content = part_text[part_text.index(_PAGED_NOTICE_SPLIT) + len(_PAGED_NOTICE_SPLIT) :]
        if content.startswith(prv.FIELD_CONT_MARKER_PREFIX):
            content = content[content.index("\n") + 1 :]
        body += content
    return (preamble + body).rstrip() + "\n"


def test_render_chunk_parts_single_file_is_byte_identical_to_render_chunk(
    tmp_path: Path,
) -> None:
    db_path, store = _paged_store(tmp_path, turn_count=2)
    try:
        chunk = _paged_chunk(store)
        single_text, truncated = prv.render_chunk(
            store, chunk, db_path=db_path, max_turn_chars=0
        )
        assert truncated is False
        single_bytes = len(single_text.encode("utf-8"))
        for cap in (0, single_bytes, single_bytes + 4096):
            parts = prv.render_chunk_parts(
                store,
                chunk,
                db_path=db_path,
                max_turn_chars=0,
                max_file_bytes=cap,
            )
            assert parts == [("", single_text)]
    finally:
        store.close()


def test_render_chunk_parts_pages_oversized_chunk_at_turn_boundaries(tmp_path: Path) -> None:
    db_path, store = _paged_store(tmp_path)
    try:
        chunk = _paged_chunk(store)
        single_text, _ = prv.render_chunk(store, chunk, db_path=db_path, max_turn_chars=0)
        cap = _part_cap_for(store, chunk, db_path, 2)
        parts = prv.render_chunk_parts(
            store,
            chunk,
            db_path=db_path,
            max_turn_chars=0,
            max_file_bytes=cap,
        )

        assert [suffix for suffix, _text in parts] == [
            "",
            ".part-001",
            ".part-002",
            ".part-003",
        ]
        for _suffix, part_text in parts[1:]:
            assert len(part_text.encode("utf-8")) <= cap
            assert prv.FIELD_CONT_MARKER_PREFIX not in part_text
        event_ids = chunk.event_ids
        assert event_ids[0] in parts[1][1] and event_ids[1] in parts[1][1]
        assert event_ids[2] not in parts[1][1]
        assert event_ids[2] in parts[2][1] and event_ids[3] in parts[2][1]
        assert event_ids[4] in parts[3][1] and event_ids[3] not in parts[3][1]
        assert _reassemble_single_text(single_text, parts) == single_text
    finally:
        store.close()


def test_render_chunk_parts_index_page_binds_every_part(tmp_path: Path) -> None:
    db_path, store = _paged_store(tmp_path)
    try:
        chunk = _paged_chunk(store)
        cap = _part_cap_for(store, chunk, db_path, 2)
        parts = prv.render_chunk_parts(
            store,
            chunk,
            db_path=db_path,
            max_turn_chars=0,
            max_file_bytes=cap,
        )
        index_text = parts[0][1]
        base_relative = prv._chunk_relative_path(chunk)  # noqa: SLF001
        base_stem = Path(base_relative).name[: -len(".md")]

        frontmatter_end = index_text.index("\n---\n", 4)
        import yaml

        payload = yaml.safe_load(index_text[4:frontmatter_end])
        assert payload["mnemos_type"] == "raw_retention_projection_index"
        assert payload["part_count"] == 3
        assert payload["event_ids"] == chunk.event_ids
        assert payload["logical_event_ids"] == chunk.logical_event_ids
        assert payload["source"] == chunk.source_agent
        assert payload["session_id"] == chunk.session_id
        assert payload["turn_start"] == chunk.start_turn + 1
        assert payload["turn_end"] == chunk.end_turn + 1
        assert [entry["path"] for entry in payload["parts"]] == [
            f"{base_relative[: -len('.md')]}.part-{index:03d}.md" for index in range(1, 4)
        ]
        for entry, (suffix, part_text) in zip(payload["parts"], parts[1:]):
            assert entry["path"].endswith(f"{suffix}.md")
            assert entry["bytes"] == len(part_text.encode("utf-8"))
            assert entry["sha256"] == prv._sha256_text(part_text)  # noqa: SLF001
        body = index_text[frontmatter_end + len("\n---\n") :]
        links = [line for line in body.splitlines() if line.startswith(("1.", "2.", "3."))]
        assert links == [
            f"1. [[{base_stem}.part-001]]",
            f"2. [[{base_stem}.part-002]]",
            f"3. [[{base_stem}.part-003]]",
        ]
        part_page = parts[1][1]
        part_payload = yaml.safe_load(part_page[4 : part_page.index("\n---\n", 4)])
        assert part_payload["mnemos_type"] == "raw_retention_projection"
        assert part_payload["part_index"] == 1
        assert part_payload["part_count"] == 3
        assert part_payload["chunk_file"] == base_relative
        assert "# codex / paged-session (part 1/3)" in part_page
    finally:
        store.close()


def test_render_chunk_parts_splits_oversized_turn_at_field_boundaries(tmp_path: Path) -> None:
    db_path = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="field-split",
            turn_number=0,
            user_content="u" * 900,
            assistant_content="a" * 900,
            reasoning="r" * 900,
            tool_calls=[{"name": "tool", "arguments": {"x": "y" * 900}}],
            timestamp="2026-06-28T10:00:00",
        )
        chunk = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=5,
            max_chunks=None,
        )[0]
        single_text, _ = prv.render_chunk(store, chunk, db_path=db_path, max_turn_chars=0)
        _preamble, blocks = prv._render_chunk_atoms(  # noqa: SLF001
            store, chunk, db_path=db_path, max_turn_chars=0
        )
        part_preamble = prv._render_part_preamble(  # noqa: SLF001
            chunk,
            prv._chunk_relative_path(chunk),  # noqa: SLF001
            db_path,
            part_index=1,
            part_count=5,
        )
        # Each field fits one part, but no two fields share a part.
        cap = len(part_preamble.encode("utf-8")) + max(
            segment.byte_length for segment in blocks[0].fields
        )
        parts = prv.render_chunk_parts(
            store,
            chunk,
            db_path=db_path,
            max_turn_chars=0,
            max_file_bytes=cap,
        )

        assert len(parts) > 2
        for _suffix, part_text in parts[1:]:
            assert prv.FIELD_CONT_MARKER_PREFIX not in part_text
            assert part_text.count(prv.FIELD_MARKER_PREFIX) == part_text.count(
                prv.FIELD_MARKER_END
            )
        assert _reassemble_single_text(single_text, parts) == single_text
    finally:
        store.close()


def test_render_chunk_parts_splits_oversized_field_at_line_boundaries(tmp_path: Path) -> None:
    db_path, store = _paged_store(tmp_path, turn_count=1, big_field=False)
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="paged-session",
            turn_number=1,
            user_content="second user",
            assistant_content="overflow\n" + "line-%04d\n" * 500,
            reasoning="second reasoning",
            timestamp="2026-06-28T10:01:00",
        )
        chunk = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=5,
            max_chunks=None,
        )[0]
        single_text, _ = prv.render_chunk(store, chunk, db_path=db_path, max_turn_chars=0)
        cap = 2500
        parts = prv.render_chunk_parts(
            store,
            chunk,
            db_path=db_path,
            max_turn_chars=0,
            max_file_bytes=cap,
        )

        assert len(parts) > 3
        part_bodies = []
        for _suffix, part_text in parts[1:]:
            assert len(part_text.encode("utf-8")) <= cap
            part_bodies.append(
                part_text[part_text.index(_PAGED_NOTICE_SPLIT) + len(_PAGED_NOTICE_SPLIT) :]
            )
        assert any(body.startswith(prv.FIELD_CONT_MARKER_PREFIX) for body in part_bodies)
        # The part carrying the oversized Assistant open marker must not close it.
        open_part = next(
            body
            for body in part_bodies
            if "### Assistant" in body and "overflow" in body
        )
        assistant_open = open_part.index("### Assistant")
        assert prv.FIELD_MARKER_END not in open_part[assistant_open:]
        # Continuation markers never cut a line: reassembly restores the bytes.
        assert _reassemble_single_text(single_text, parts) == single_text
        # The open marker's sha256 still verifies against the reassembled field.
        expected_assistant = "overflow\n" + "line-%04d\n" * 500
        marker_line = open_part[assistant_open:].splitlines()[2]
        marker = json.loads(marker_line[len(prv.FIELD_MARKER_PREFIX) : -4])
        assert marker["sha256"] == prv._sha256_text(expected_assistant)  # noqa: SLF001
    finally:
        store.close()


def test_take_line_slice_packs_lines_then_cuts_oversized_line_at_char_boundaries() -> None:
    take = prv._take_line_slice  # noqa: SLF001

    # Whole lines are preferred and never cut while another line fits.
    assert take("ab\ncd\nef", 3) == ("ab\n", "cd\nef")
    assert take("", 3) == ("", "")
    # A single oversized line is cut inside the line at the byte budget.
    assert take("abcdef", 3) == ("abc", "def")
    assert take("abcdefghij\nrest", 5) == ("abcde", "fghij\nrest")
    # Multibyte characters are never torn: the cut backs off to a boundary.
    assert take("中中中", 4) == ("中", "中中")
    assert take("é中x", 2) == ("é", "中x")
    # One whole character is the atomic minimum when the budget is smaller.
    assert take("中中中", 2) == ("中", "中中")
    head, tail = take("中" * 3000, 2500)
    assert len(head.encode("utf-8")) == 2499
    assert len(head.encode("utf-8")) > 2500 - 3
    # Slices always restore the original value byte-for-byte.
    for value, budget in (
        ("ab\ncd\nef", 3),
        ("abcdef", 3),
        ("中中中", 4),
        ("中中中", 2),
        ("abcdefghij\nrest", 5),
    ):
        slice_text, rest = take(value, budget)
        assert slice_text + rest == value
        assert (
            len(slice_text.encode("utf-8")) <= budget
            or len(slice_text.encode("utf-8")) <= 4
        )


def test_render_chunk_parts_splits_single_oversized_line_within_cap(tmp_path: Path) -> None:
    db_path = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        cap = 2500
        oversized_line = "x" * (3 * cap)  # one line, three times the cap
        store.upsert_turn(
            source_agent="kimi",
            session_id="long-line",
            turn_number=0,
            user_content="short user",
            assistant_content=oversized_line,
            reasoning="short reasoning",
            timestamp="2026-05-21T10:00:00",
        )
        chunk = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=5,
            max_chunks=None,
        )[0]
        single_text, _ = prv.render_chunk(store, chunk, db_path=db_path, max_turn_chars=0)
        parts = prv.render_chunk_parts(
            store,
            chunk,
            db_path=db_path,
            max_turn_chars=0,
            max_file_bytes=cap,
        )

        assert len(parts) >= 5
        part_bodies = []
        for _suffix, part_text in parts[1:]:
            assert len(part_text.encode("utf-8")) <= cap
            part_bodies.append(
                part_text[part_text.index(_PAGED_NOTICE_SPLIT) + len(_PAGED_NOTICE_SPLIT) :]
            )
        open_part = next(body for body in part_bodies if "### Assistant" in body)
        assistant_open = open_part.index("### Assistant")
        assert prv.FIELD_MARKER_END not in open_part[assistant_open:]
        continuation_parts = [
            body for body in part_bodies if body.startswith(prv.FIELD_CONT_MARKER_PREFIX)
        ]
        assert len(continuation_parts) >= 2
        assert continuation_parts[-1].rstrip().endswith(prv.FIELD_MARKER_END)
        assert _reassemble_single_text(single_text, parts) == single_text
        # The open marker's sha256 still verifies against the reassembled field.
        marker_line = open_part[assistant_open:].splitlines()[2]
        marker = json.loads(marker_line[len(prv.FIELD_MARKER_PREFIX) : -4])
        assert marker["sha256"] == prv._sha256_text(oversized_line)  # noqa: SLF001
    finally:
        store.close()


def test_render_chunk_parts_never_cuts_a_multibyte_character(tmp_path: Path) -> None:
    db_path = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=db_path, config=_Cfg(tmp_path))
    try:
        cap = 2500
        oversized_line = "中" * 3000 + "雪" * 1000  # 12000 bytes, one line
        store.upsert_turn(
            source_agent="kimi",
            session_id="long-line-multibyte",
            turn_number=0,
            user_content="short user",
            assistant_content=oversized_line,
            reasoning="short reasoning",
            timestamp="2026-05-21T10:00:00",
        )
        chunk = prv.build_projection_chunks(
            prv._fetch_refs(store),  # noqa: SLF001
            chunk_turns=5,
            max_chunks=None,
        )[0]
        single_text, _ = prv.render_chunk(store, chunk, db_path=db_path, max_turn_chars=0)
        parts = prv.render_chunk_parts(
            store,
            chunk,
            db_path=db_path,
            max_turn_chars=0,
            max_file_bytes=cap,
        )

        encoded_line = oversized_line.encode("utf-8")
        cut_positions: list[int] = []
        consumed = 0
        for _suffix, part_text in parts[1:]:
            assert len(part_text.encode("utf-8")) <= cap
            assert "\ufffd" not in part_text
            body = part_text[part_text.index(_PAGED_NOTICE_SPLIT) + len(_PAGED_NOTICE_SPLIT) :]
            if body.startswith(prv.FIELD_CONT_MARKER_PREFIX):
                body = body[body.index("\n") + 1 :]
            if "### Assistant" in body:
                # First slice: skip everything up to the field open marker line.
                marker_start = body.index(prv.FIELD_MARKER_PREFIX, body.index("### Assistant"))
                body = body[body.index("\n", marker_start) + 1 :]
            # A slice of the oversized line is a maximal run of its characters.
            slice_length = 0
            while slice_length < len(body) and body[slice_length] in "中雪":
                slice_length += 1
            if not slice_length:
                continue
            slice_bytes = len(body[:slice_length].encode("utf-8"))
            assert encoded_line[consumed : consumed + slice_bytes] == body[
                :slice_length
            ].encode("utf-8")
            consumed += slice_bytes
            if consumed < len(encoded_line):
                cut_positions.append(consumed)
        assert consumed == len(encoded_line)
        # Every cut landed on a whole-character boundary in the original bytes.
        for position in cut_positions:
            assert encoded_line[position] & 0xC0 != 0x80
        assert _reassemble_single_text(single_text, parts) == single_text
    finally:
        store.close()


def test_paged_projection_plan_and_journal_bind_every_part(tmp_path: Path) -> None:
    db_path, store = _paged_store(tmp_path)
    raw_dir = tmp_path / "raw"
    try:
        chunk = _paged_chunk(store)
        chunks = [chunk]
        cap = _part_cap_for(store, chunk, db_path, 2)
        rendered = prv.render_chunk_parts(
            store,
            chunk,
            db_path=db_path,
            max_turn_chars=0,
            max_file_bytes=cap,
        )
        base_relative = prv._chunk_relative_path(chunk)  # noqa: SLF001
        expected_paths = {
            prv._part_relative_path(base_relative, suffix)  # noqa: SLF001
            for suffix, _text in rendered
        }
        plan = prv.build_projection_plan(
            raw_dir,
            store,
            chunks,
            db_path=db_path,
            max_turn_chars=0,
            max_file_bytes=cap,
        )
        assert set(plan["desired_file_hashes"]) == expected_paths
        prv.validate_projection_plan(plan)

        stats = prv.write_projection(
            raw_dir,
            store,
            chunks,
            db_path=db_path,
            max_turn_chars=0,
            max_file_bytes=cap,
        )
        assert stats["projected_files"] == len(expected_paths)
        journal = json.loads((raw_dir / prv.PROJECTION_JOURNAL_NAME).read_text(encoding="utf-8"))
        assert set(journal["files"]) == expected_paths
        for relative_path in expected_paths:
            metadata = journal["files"][relative_path]
            assert metadata["content_hash"] == hashlib.sha256(
                (raw_dir / relative_path).read_bytes()
            ).hexdigest()
            assert metadata["revision_ids"] == chunk.event_ids
            assert metadata["logical_event_ids"] == chunk.logical_event_ids
        index_page = (raw_dir / base_relative).read_text(encoding="utf-8")
        assert "raw_retention_projection_index" in index_page
        part_files = sorted(raw_dir.rglob("*.part-*.md"))
        assert len(part_files) == len(expected_paths) - 1
        for part_file in part_files:
            assert len(part_file.read_bytes()) <= cap

        again = prv.write_projection(
            raw_dir,
            store,
            chunks,
            db_path=db_path,
            max_turn_chars=0,
            max_file_bytes=cap,
        )
        assert again["written_files"] == 0
        assert again["journal_written"] is False
    finally:
        store.close()


def test_paged_projection_rewrites_to_single_file_when_cap_disabled(tmp_path: Path) -> None:
    db_path, store = _paged_store(tmp_path)
    raw_dir = tmp_path / "raw"
    try:
        chunks = [_paged_chunk(store)]
        cap = _part_cap_for(store, chunks[0], db_path, 2)
        prv.write_projection(
            raw_dir,
            store,
            chunks,
            db_path=db_path,
            max_turn_chars=0,
            max_file_bytes=cap,
        )
        assert any(".part-" in path.name for path in raw_dir.rglob("*.md"))

        rewritten = prv.write_projection(
            raw_dir,
            store,
            chunks,
            db_path=db_path,
            max_turn_chars=0,
            max_file_bytes=0,
        )
        single_text, _ = prv.render_chunk(store, chunks[0], db_path=db_path, max_turn_chars=0)
        md_files = sorted(raw_dir.rglob("*.md"))
        assert len(md_files) == 1
        assert ".part-" not in md_files[0].name
        assert md_files[0].read_text(encoding="utf-8") == single_text
        assert rewritten["deleted_stale_files"] >= 1
    finally:
        store.close()


def test_max_file_bytes_cli_default_and_zero_disables_paging() -> None:
    args = prv.build_parser().parse_args([])

    assert args.max_file_bytes == prv.DEFAULT_MAX_FILE_BYTES == 2097152
    assert prv.build_parser().parse_args(["--max-file-bytes", "0"]).max_file_bytes == 0
