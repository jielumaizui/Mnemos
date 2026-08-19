# -*- coding: utf-8 -*-
"""Hermetic end-to-end coverage for the manifest-owned continuous source owner."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from core.agent_kit.source_support_manifest import get_agent_source_support_manifest
from core.agent_kit.source_support_manifest import (
    build_native_source_snapshot,
    validate_native_source_snapshot,
)
from core.sync_framework.agent_source import AgentSource, SessionInfo, Turn
from core.sync_framework.raw_event_store import RawEventStore
from core.sync_framework.sync_engine import SyncEngine
from daemon import heartbeat, raw_sync
from daemon.agent_source_coverage import (
    coverage_state_path,
    load_source_coverage_state,
    source_coverage_for_heartbeat,
    write_source_coverage_state,
)
from daemon.agent_sync_cursor import AgentSyncCursorStore
from scripts.audit_agent_source_coverage import audit_agent_source_coverage
from scripts import audit_raw_projection_fidelity
from scripts import project_raw_vault


class _Config:
    def __init__(self, root: Path):
        self.data_dir = root
        self.database_dir = root
        self.wiki_dir = root / "wiki"
        self.raw_dir = root / "raw"
        self.obsidian_vault_path = self.raw_dir

    def get(self, key, default=None):
        values = {
            "storage.max_content_bytes": 200_000,
            "capture.reasoning_mode": "artifact_summary",
            "raw_event_store.enabled": True,
            "raw_projection.enabled": True,
            "daemon.services.raw_sync": True,
        }
        return values.get(key, default)


class _SyntheticActiveSource(AgentSource):
    def __init__(self, name: str, root: Path, now_ts: float, source_fidelity: str):
        self._name = name
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)
        self._session_path = root / f"{name}.jsonl"
        self._session_path.write_text("synthetic-safe", encoding="utf-8")
        self._now_ts = now_ts
        self._source_fidelity = source_fidelity
        self._runtime_receipt: dict | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def model_tag(self) -> str:
        return "synthetic-active-source"

    @property
    def data_dir(self) -> Path:
        return self._root

    def discover_sessions(self) -> list[SessionInfo]:
        return [
            SessionInfo(
                session_id=f"{self.name}-synthetic-session",
                source_path=self._session_path,
                mtime=self._now_ts - 1,
            )
        ]

    def parse_turns(self, _session_path: Path) -> list[Turn]:
        from core.agent_kit.runtime_receipts import runtime_probe_contract
        from core.ops.health_contract import CANONICAL_HEALTH_CHECK_IDS_HASH

        tool_calls = []
        tool_results = []
        if self._runtime_receipt is not None:
            tool_calls = [
                {
                    "id": f"{self.name}-runtime-probe",
                    "name": "agent_runtime_probe",
                    "arguments": {
                        "health_check_ids_hash": CANONICAL_HEALTH_CHECK_IDS_HASH,
                        "sample": runtime_probe_contract()["sample"],
                    },
                }
            ]
            tool_results = [
                {
                    "tool_call_id": f"{self.name}-runtime-probe",
                    "output": self._runtime_receipt,
                }
            ]
        return [
            Turn(
                turn_number=0,
                user_content=f"synthetic-safe user turn for {self.name}",
                assistant_content=f"synthetic-safe assistant turn for {self.name}",
                tool_calls=tool_calls,
                tool_results=tool_results,
                completeness={"visible_text": "full", "truncated": False},
            )
        ]

    def bind_runtime_receipt(self, receipt: dict) -> None:
        self._runtime_receipt = dict(receipt)
        self._session_path.write_text(
            json.dumps({"runtime_receipt_id": receipt["receipt_id"]}),
            encoding="utf-8",
        )

    def completeness_capabilities(self) -> dict[str, object]:
        return {
            "visible_text": True,
            "tool_calls": True,
            "tool_results": True,
            "reasoning": True,
            "attachments": True,
            "raw_files": True,
            "source_fidelity": self._source_fidelity,
        }


def test_native_snapshot_to_canonical_raw_projection_reverse_audit_is_lossless(
    tmp_path: Path,
) -> None:
    """One production-shaped generation crosses every COG-009/026 owner."""
    now_ts = 1_800_000_000.0
    config = _Config(tmp_path)
    source = _SyntheticActiveSource(
        "codex",
        tmp_path / "native-codex",
        now_ts,
        get_agent_source_support_manifest().require_active_source("codex").source_fidelity,
    )
    snapshot = build_native_source_snapshot(
        "codex",
        resolved_roots=[source.data_dir],
        cursor={"kind": "continuous", "since_hours": 0},
        native_denominator={"sessions": 1, "turns": 1},
    )
    assert validate_native_source_snapshot(snapshot) == []
    assert snapshot.native_denominator == {"sessions": 1, "turns": 1}

    raw_db_path = tmp_path / "raw_events.db"
    raw_store = RawEventStore(db_path=raw_db_path, config=config)
    backend = Mock()
    backend.list_by_tags.return_value = []
    backend.save.return_value = []
    engine = SyncEngine(
        backend=backend,
        db_path=str(tmp_path / "sync_log.db"),
        config=config,
        raw_store=raw_store,
    )
    with (
        patch("core.sync_framework.registry.SourceRegistry") as registry_class,
        patch("core.sync_framework.sync_engine.SyncEngine", return_value=engine),
    ):
        registry_class.return_value.list_sources.return_value = [source]
        sync_report = raw_sync.run_service(
            lambda _service, _error: None,
            engine_factory=lambda: engine,
            now_func=lambda: now_ts,
        )
    assert sync_report["synced"] == 1
    assert sync_report["errors"] == 0

    try:
        refs = project_raw_vault._fetch_refs(raw_store)  # noqa: SLF001
        assert len(refs) == snapshot.native_denominator["turns"]
        chunks = project_raw_vault.build_projection_chunks(
            refs,
            chunk_turns=5,
            max_chunks=None,
        )
        project_raw_vault.write_projection(
            config.raw_dir,
            raw_store,
            chunks,
            db_path=raw_db_path,
            max_turn_chars=0,
        )
    finally:
        raw_store.close()

    reverse = audit_raw_projection_fidelity.audit_raw_projection_fidelity(
        raw_dir=config.raw_dir,
        db_path=raw_db_path,
        include_gap_evidence=True,
    )
    assert reverse["ok"] is True, reverse
    assert reverse["missing_event_ids"] == 0
    assert reverse["duplicate_event_ids"] == 0
    assert reverse["field_hash_mismatch_count"] == 0
    assert reverse["gap_generation"]["classification"] == "in_sync"


def test_twelve_manifest_sources_capture_to_raw_and_survive_heartbeat_restart(tmp_path: Path):
    """Twelve synthetic native turns prove owner->Raw->heartbeat continuity in isolation."""
    now_ts = 1_800_000_000.0
    observed_at = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    config = _Config(tmp_path)
    manifest = get_agent_source_support_manifest()
    sources = [
        _SyntheticActiveSource(
            name,
            tmp_path / name,
            now_ts,
            manifest.require_active_source(name).source_fidelity,
        )
        for name in manifest.active_source_names
    ]

    backend = Mock()
    backend.list_by_tags.return_value = []
    backend.save.return_value = []
    raw_db_path = tmp_path / "raw_events.db"
    raw_store = RawEventStore(db_path=raw_db_path, config=config)
    engine = SyncEngine(
        backend=backend,
        db_path=str(tmp_path / "sync_log.db"),
        config=config,
        raw_store=raw_store,
    )
    coverage_path = coverage_state_path(config.database_dir)

    with (
        patch("core.sync_framework.registry.SourceRegistry") as registry_class,
        patch("core.sync_framework.sync_engine.SyncEngine", return_value=engine),
    ):
        registry_class.return_value.list_sources.return_value = sources
        report = raw_sync.run_service(
            lambda _service, _error: None,
            engine_factory=lambda: engine,
            now_func=lambda: now_ts,
            coverage_state_sink=lambda coverage: write_source_coverage_state(
                coverage_path,
                coverage,
            ),
        )

    assert report["synced"] == 12
    assert report["errors"] == 0
    assert set(report["source_snapshots"]) == set(manifest.active_source_names)
    coverage = load_source_coverage_state(coverage_path)
    assert set(coverage["sources"]) == set(manifest.active_source_names)
    assert all(
        coverage["sources"][name]["status"] == "captured" for name in manifest.active_source_names
    )

    reopened = RawEventStore(db_path=raw_db_path, config=config)
    try:
        row_count = (
            reopened._pool.get_conn()
            .execute("SELECT COUNT(*) FROM raw_turns")  # noqa: SLF001
            .fetchone()[0]
        )
    finally:
        reopened.close()
    assert row_count == 12

    # Simulate daemon restart: there is no in-memory raw_sync result, so the
    # heartbeat must restore the durable coverage state before its next poll.
    restarted_heartbeat = heartbeat.build_heartbeat_snapshot(
        instance_identity={"instance_id": "synthetic-restart"},
        intervals={"raw_sync": 600},
        service_results={},
        service_error_state={},
        cfg=config,
        service_enabled=lambda _cfg, _service: True,
        persisted_source_coverage=coverage,
    )
    heartbeat_path = tmp_path / "daemon_heartbeat.json"
    heartbeat_path.write_text(json.dumps(restarted_heartbeat), encoding="utf-8")
    audit = audit_agent_source_coverage(
        config=config,
        heartbeat_path=heartbeat_path,
        now=observed_at,
    )

    assert (
        source_coverage_for_heartbeat(coverage)
        == restarted_heartbeat["services"]["raw_sync"]["source_coverage"]
    )
    assert audit["ok"] is True, audit["findings"]
    assert all(
        audit["source_status"][name]["raw_capture_verified"] is True
        for name in manifest.active_source_names
    )

    with sqlite3.connect(raw_db_path) as connection:
        connection.execute("DELETE FROM raw_turns WHERE source_agent='gemini'")
    missing_ingestion_raw = audit_agent_source_coverage(
        config=config,
        heartbeat_path=heartbeat_path,
        now=observed_at,
    )
    assert missing_ingestion_raw["ok"] is False
    assert any(
        item["code"] == "raw_capture_unverified" and item["source"] == "gemini"
        for item in missing_ingestion_raw["findings"]
    )

    # Host full-power capture proof is deliberately a second phase: it binds the
    # safe host probe to the frozen source denominator and independently
    # reconciles each canonical Raw session/revision set without transcript
    # bodies.  This is an all-eight host contract test, not a live claim.
    from core.agent_kit.authorization import AgentAuthorizationStore
    from core.agent_kit.runtime_receipts import AgentRuntimeReceiptStore, runtime_probe_contract
    from core.ops.health_contract import CANONICAL_HEALTH_CHECK_IDS_HASH

    receipt_db = tmp_path / "agent_authorization.db"
    authorization = AgentAuthorizationStore(receipt_db)
    receipt_store = AgentRuntimeReceiptStore(receipt_db)
    sources_by_name = {source.name: source for source in sources}
    for name in manifest.host_agent_names:
        authorization.set_state(name, "user_authorized")
        receipt_store.record_health_check(name, CANONICAL_HEALTH_CHECK_IDS_HASH)
        runtime_receipt = receipt_store.record_probe(
            name,
            health_check_ids_hash=CANONICAL_HEALTH_CHECK_IDS_HASH,
            sample=runtime_probe_contract()["sample"],
        )
        assert runtime_receipt["success"] is True
        sources_by_name[name].bind_runtime_receipt(runtime_receipt)

    with (
        patch("core.sync_framework.registry.SourceRegistry") as registry_class,
        patch("core.sync_framework.sync_engine.SyncEngine", return_value=engine),
    ):
        registry_class.return_value.list_sources.return_value = sources
        canary_report = raw_sync.run_service(
            lambda _service, _error: None,
            engine_factory=lambda: engine,
            now_func=lambda: now_ts + 60,
            coverage_state_sink=lambda current: write_source_coverage_state(
                coverage_path,
                current,
            ),
        )
    assert canary_report["errors"] == 0, canary_report
    coverage = load_source_coverage_state(coverage_path)
    for name in manifest.host_agent_names:
        receipt = receipt_store.record_source_capture(
            name,
            coverage=coverage,
            cursor_db_path=tmp_path / "agent_sync_cursors.db",
            raw_db_path=raw_db_path,
        )
        assert receipt["success"] is True, receipt


class _AliasHistorySource(AgentSource):
    """Synthetic native history with aliases, stale files, and long sessions."""

    name = "codex"

    def __init__(self, root: Path, *, sessions: int, turns_per_session: int):
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)
        self._sessions = []
        self._turns = {}
        for index in range(sessions):
            path = self._root / f"session-{index:02d}.jsonl"
            path.write_text("synthetic-safe", encoding="utf-8")
            canonical = f"canonical-{index:02d}"
            self._sessions.append(
                SessionInfo(
                    session_id=f"alias-{index:02d}",
                    canonical_session_id=canonical,
                    source_path=path,
                    mtime=1.0 if index == 0 else 1_800_000_000.0 + index,
                )
            )
            self._turns[path.name] = [
                Turn(
                    turn_number=turn_number,
                    user_content=f"synthetic-safe user {index}/{turn_number}",
                    assistant_content=f"synthetic-safe assistant {index}/{turn_number}",
                    completeness={"visible_text": "full", "truncated": False},
                )
                for turn_number in range(turns_per_session)
            ]

    @property
    def model_tag(self) -> str:
        return "synthetic-host"

    @property
    def data_dir(self) -> Path:
        return self._root

    def discover_sessions(self) -> list[SessionInfo]:
        return list(self._sessions)

    def parse_turns(self, session_path: Path) -> list[Turn]:
        return list(self._turns[session_path.name])

    def completeness_capabilities(self) -> dict[str, object]:
        return {
            "visible_text": True,
            "tool_calls": True,
            "tool_results": True,
            "reasoning": True,
            "attachments": True,
            "raw_files": True,
            "source_fidelity": "full",
        }


def _continuous_limits(*, sessions: int, turns: int) -> dict[str, int]:
    return {
        "tail_sessions_per_source": sessions,
        "reconciliation_sessions_per_source": sessions,
        "turns_per_session": turns,
    }


def _run_continuous_cycle(
    *,
    config: _Config,
    source: AgentSource,
    cursor_store: AgentSyncCursorStore,
    limits: dict[str, int],
    backend: Mock,
):
    """Run one service cycle with a fresh engine to model daemon restart/replay."""

    def make_engine():
        engine = SyncEngine(
            backend=backend,
            db_path=str(config.database_dir / "sync_log.db"),
            config=config,
            raw_store=RawEventStore(db_path=config.database_dir / "raw_events.db", config=config),
        )
        # The test proves Raw/cursor closure; the full-session handoff has its
        # own integration coverage and must not depend on Amphora here.
        engine.enqueue_session_for_distillation = Mock(return_value={})  # type: ignore[method-assign]
        return engine

    with (
        patch("core.sync_framework.registry.SourceRegistry") as registry_class,
        patch("core.sync_framework.sync_engine.SyncEngine", side_effect=make_engine),
    ):
        registry_class.return_value.list_sources.return_value = [source]
        return raw_sync.run_service(
            lambda _service, _error: None,
            engine_factory=make_engine,
            continuous_sync_limits_func=lambda: limits,
            cursor_store=cursor_store,
        )


def test_continuous_reconciliation_captures_over_ten_old_aliased_sessions_across_restarts(
    tmp_path: Path,
):
    config = _Config(tmp_path)
    source = _AliasHistorySource(tmp_path / "native", sessions=12, turns_per_session=1)
    cursor_store = AgentSyncCursorStore(config.database_dir)
    backend = Mock()
    backend.list_by_tags.return_value = []
    backend.save.return_value = []

    for _ in range(12):
        report = _run_continuous_cycle(
            config=config,
            source=source,
            cursor_store=AgentSyncCursorStore(config.database_dir),
            limits=_continuous_limits(sessions=1, turns=1),
            backend=backend,
        )
        assert report["errors"] == 0

    store = RawEventStore(db_path=config.database_dir / "raw_events.db", config=config)
    try:
        rows = (
            store._pool.get_conn()
            .execute(  # noqa: SLF001
                "SELECT session_id, turn_number FROM raw_turns WHERE source_agent='codex'"
            )
            .fetchall()
        )
    finally:
        store.close()
    assert set(rows) == {(f"canonical-{index:02d}", 0) for index in range(12)}
    assert cursor_store.get_session_raw_cursor("codex", "canonical-00").next_turn_number == 1


def test_continuous_cursor_captures_over_one_hundred_turns_without_replay_duplicates(
    tmp_path: Path,
):
    config = _Config(tmp_path)
    source = _AliasHistorySource(tmp_path / "native", sessions=1, turns_per_session=205)
    cursor_store = AgentSyncCursorStore(config.database_dir)
    backend = Mock()
    backend.list_by_tags.return_value = []
    backend.save.return_value = []

    limits = _continuous_limits(sessions=1, turns=50)
    for _ in range(5):
        report = _run_continuous_cycle(
            config=config,
            source=source,
            cursor_store=AgentSyncCursorStore(config.database_dir),
            limits=limits,
            backend=backend,
        )
        assert report["errors"] == 0

    assert cursor_store.get_session_raw_cursor("codex", "canonical-00").next_turn_number == 205
    store = RawEventStore(db_path=config.database_dir / "raw_events.db", config=config)
    try:
        before = (
            store._pool.get_conn()
            .execute("SELECT COUNT(*) FROM raw_turns")  # noqa: SLF001
            .fetchone()[0]
        )
        revisions_before = (
            store._pool.get_conn()
            .execute("SELECT COUNT(*) FROM raw_turn_revisions")  # noqa: SLF001
            .fetchone()[0]
        )
    finally:
        store.close()

    replay = _run_continuous_cycle(
        config=config,
        source=source,
        cursor_store=AgentSyncCursorStore(config.database_dir),
        limits=limits,
        backend=backend,
    )
    assert replay["errors"] == 0
    store = RawEventStore(db_path=config.database_dir / "raw_events.db", config=config)
    try:
        after = (
            store._pool.get_conn()
            .execute("SELECT COUNT(*) FROM raw_turns")  # noqa: SLF001
            .fetchone()[0]
        )
        revisions_after = (
            store._pool.get_conn()
            .execute("SELECT COUNT(*) FROM raw_turn_revisions")  # noqa: SLF001
            .fetchone()[0]
        )
    finally:
        store.close()
    assert (before, revisions_before) == (205, 205)
    assert (after, revisions_after) == (before, revisions_before)
