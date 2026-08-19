# -*- coding: utf-8 -*-
"""Tests for integrations.sources.kimi_source."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from core.sync_framework.agent_source import SessionInfo, Turn
from core.sync_framework.raw_event_store import (
    RawEventIdentitySchemaMigrationRequired,
    RawEventStore,
)
from core.sync_framework.sync_engine import SyncEngine
from integrations.sources.kimi_source import KimiSource


@pytest.fixture
def source(tmp_path):
    src = KimiSource()
    with patch.object(type(src), "data_dir", new=tmp_path):
        yield src


class _KimiRawConfig:
    """Hermetic config for Kimi artifact-to-Raw verification."""

    def __init__(self, root: Path):
        self.data_dir = root
        self.database_dir = root
        self.wiki_dir = root / "wiki"
        self.raw_dir = root / "raw"
        self.obsidian_vault_path = self.raw_dir

    def get(self, key, default=None):  # noqa: ANN001
        values = {
            "storage.max_content_bytes": 200_000,
            "capture.reasoning_mode": "artifact_summary",
            "raw_event_store.enabled": True,
            "raw_projection.enabled": True,
        }
        return values.get(key, default)


def _context_records(label: str) -> list[dict]:
    return [
        {
            "message_id": f"{label}-user",
            "role": "user",
            "content": f"user-{label}",
        },
        {
            "message_id": f"{label}-assistant",
            "role": "assistant",
            "content": [{"type": "text", "text": f"assistant-{label}"}],
        },
    ]


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )


def _wire_records(label: str) -> list[dict]:
    return [
        {
            "message": {
                "type": "TurnBegin",
                "payload": {
                    "user_input": [{"type": "text", "text": f"user-{label}"}],
                },
            }
        },
        {
            "message": {
                "type": "ContentPart",
                "payload": {"type": "text", "text": f"assistant-{label}"},
            }
        },
    ]


class TestKimiSource:
    def test_name_and_model_tag(self, source):
        assert source.name == "kimi"
        assert source.model_tag == "kimi-k2.5"

    def test_trigger_strategy(self, source):
        strategy = source.trigger_strategy
        assert strategy["type"] == "hybrid"
        assert "modified" in strategy["events"]
        assert strategy["recursive"] is True

    def test_completeness_capabilities(self, source):
        caps = source.completeness_capabilities()
        assert caps["visible_text"] is True
        assert caps["reasoning"] == "available"
        assert caps["tool_calls"] is True
        assert caps["attachments"] is True

    def test_discover_sessions_basic(self, source, tmp_path):
        sessions = tmp_path / "sessions"
        sess_dir = sessions / "sess-1"
        sess_dir.mkdir(parents=True)
        (sess_dir / "context.jsonl").write_text("{}", encoding="utf-8")

        infos = source.discover_sessions()
        assert len(infos) == 1
        assert infos[0].session_id.startswith("sess-1::main_context::")
        assert infos[0].session_aliases == ["sess-1"]
        assert infos[0].source_path.name == "context.jsonl"

    def test_artifact_discovery_keeps_main_subagent_and_wire_bodies_separate(
        self, source, tmp_path
    ):
        """Every Kimi native artifact reaches exactly one canonical Raw path.

        Context archives are one artifact only when they share an artifact
        directory.  Main/subagent wires are separate artifacts, so neither a
        main transcript nor a sibling subagent can silently absorb their body.
        """
        parent = tmp_path / "sessions" / "work-key" / "parent-session"
        archive_records = _context_records("archive-main")
        _write_jsonl(parent / "context_1.jsonl", archive_records)
        _write_jsonl(parent / "context.jsonl", archive_records + _context_records("main"))
        _write_jsonl(parent / "subagents" / "worker-a" / "context.jsonl", _context_records("sub-a"))
        _write_jsonl(parent / "subagents" / "worker-b" / "context.jsonl", _context_records("sub-b"))
        _write_jsonl(parent / "agents" / "main" / "wire.jsonl", _wire_records("main-wire"))
        _write_jsonl(parent / "subagents" / "worker-a" / "wire.jsonl", _wire_records("sub-wire"))

        infos = source.discover_sessions()
        assert {info.source_kind for info in infos} == {
            "main_context",
            "subagent_context",
            "main_wire",
            "subagent_wire",
        }
        assert len(infos) == 5
        assert len({info.session_id for info in infos}) == len(infos)
        assert len({info.metadata["source_artifact_id"] for info in infos}) == len(infos)

        main_context = next(info for info in infos if info.source_kind == "main_context")
        assert main_context.session_id.startswith("parent-session::main_context::")
        assert main_context.session_aliases == ["parent-session"]
        assert source.parse_session(main_context)[0].source_files == [
            str(parent / "context_1.jsonl"),
            str(parent / "context.jsonl"),
        ]
        assert [turn.user_content for turn in source.parse_session(main_context)] == [
            "user-archive-main",
            "user-main",
        ]
        assert "wire.jsonl" not in " ".join(source.parse_session(main_context)[0].source_files)

        children = [
            info
            for info in infos
            if info.source_kind in {"subagent_context", "subagent_wire"}
        ]
        assert {info.metadata["parent_session_id"] for info in children} == {"parent-session"}
        assert {info.metadata["parent_relation"] for info in children} == {
            "native_subagent_path",
            "native_subagent_wire_path",
        }
        related_artifacts = [
            info for info in infos if info.source_kind != "main_context"
        ]
        assert {
            info.metadata["canonical_parent_session_id"]
            for info in related_artifacts
        } == {main_context.session_id}
        assert {
            info.metadata["parent_source_artifact_id"]
            for info in related_artifacts
        } == {main_context.metadata["source_artifact_id"]}

        cold_source = KimiSource()
        cold_source._override_data_dir = tmp_path
        cold_infos = cold_source.discover_sessions()
        assert [
            (info.session_id, info.source_kind, info.metadata["source_artifact_id"])
            for info in cold_infos
        ] == [
            (info.session_id, info.source_kind, info.metadata["source_artifact_id"])
            for info in infos
        ]
        assert {
            info.session_id: source.get_session_state(info)["fingerprint"]
            for info in infos
        } == {
            info.session_id: cold_source.get_session_state(info)["fingerprint"]
            for info in cold_infos
        }

        config = _KimiRawConfig(tmp_path / "runtime")
        backend = Mock()
        backend.list_by_tags.return_value = []
        backend.save.return_value = []
        raw_store = RawEventStore(db_path=config.database_dir / "raw_events.db", config=config)
        engine = SyncEngine(
            backend=backend,
            db_path=str(config.database_dir / "sync_log.db"),
            config=config,
            raw_store=raw_store,
        )
        try:
            result = engine.sync_batch(source, infos, incremental=False)
            assert result.failed == []
            assert result.turn_stats["new"] == 6

            rows = raw_store._pool.get_conn().execute(  # noqa: SLF001
                """
                SELECT session_id, turn_number, current_revision_id
                FROM raw_turns WHERE source_agent='kimi'
                ORDER BY session_id, turn_number
                """
            ).fetchall()
            assert len(rows) == 6
            snapshot = list(rows)
            captured = [raw_store.get_turn(revision_id) for _sid, _turn, revision_id in rows]
            captured_pairs = {
                (item["user_content"], item["assistant_content"])
                for item in captured
                if item is not None
            }
            for label in ("archive-main", "main", "sub-a", "sub-b", "main-wire", "sub-wire"):
                assert (f"user-{label}", f"assistant-{label}") in captured_pairs
            assert len(captured_pairs) == 6

            engine.sync_batch(source, infos, incremental=True)
            rows_after = raw_store._pool.get_conn().execute(  # noqa: SLF001
                """
                SELECT session_id, turn_number, current_revision_id
                FROM raw_turns WHERE source_agent='kimi'
                ORDER BY session_id, turn_number
                """
            ).fetchall()
            assert rows_after == snapshot
        finally:
            engine.close()

    def test_main_artifact_identity_does_not_change_when_same_native_id_appears_elsewhere(
        self, source, tmp_path
    ):
        first = (
            tmp_path
            / "sessions"
            / "workspace-a"
            / "shared-session"
            / "context.jsonl"
        )
        _write_jsonl(first, _context_records("first"))

        before = source.discover_sessions()
        assert len(before) == 1
        first_id = before[0].session_id

        second = (
            tmp_path
            / "sessions"
            / "workspace-b"
            / "shared-session"
            / "context.jsonl"
        )
        _write_jsonl(second, _context_records("second"))

        after = source.discover_sessions()
        first_after = next(info for info in after if info.source_path == first)
        second_after = next(info for info in after if info.source_path == second)

        assert first_after.session_id == first_id
        assert first_after.session_id != "shared-session"
        assert second_after.session_id != first_after.session_id
        assert second_after.metadata["source_artifact_id"] != (
            first_after.metadata["source_artifact_id"]
        )
        assert first_after.metadata["native_session_id"] == "shared-session"
        assert first_after.session_aliases == ["shared-session"]

    def test_v2_artifact_identity_fails_closed_over_legacy_raw_session(
        self, source, tmp_path
    ):
        context = (
            tmp_path
            / "sessions"
            / "workspace"
            / "legacy-session"
            / "context.jsonl"
        )
        _write_jsonl(context, _context_records("current"))
        session = source.discover_sessions()[0]
        turn = source.parse_session(session)[0]
        config = _KimiRawConfig(tmp_path / "runtime-legacy-identity")
        raw_store = RawEventStore(
            db_path=config.database_dir / "raw_events.db",
            config=config,
        )
        raw_store.upsert_turn(
            source_agent="kimi",
            session_id="legacy-session",
            turn_number=0,
            user_content="historical",
            assistant_content="v1",
            completeness={
                "visible_text": "full",
                "tool_calls": "unavailable",
                "tool_results": "unavailable",
                "reasoning": "unavailable",
                "attachments": "unavailable",
                "truncated": False,
                "loss_reasons": [],
            },
        )
        backend = Mock()
        backend.list_by_tags.return_value = []
        backend.save.return_value = []
        engine = SyncEngine(
            backend=backend,
            db_path=str(config.database_dir / "sync_log.db"),
            config=config,
            raw_store=raw_store,
        )
        try:
            result = engine.sync_batch(source, [session], incremental=False)
            assert result.failed == [
                {
                    "session_id": session.session_id,
                    "error": "source_session_identity_reconciliation_required",
                }
            ]
            with pytest.raises(
                RawEventIdentitySchemaMigrationRequired,
                match="source_session_identity_reconciliation_required",
            ):
                raw_store.upsert_turn(
                    source_agent="kimi",
                    session_id=session.session_id,
                    turn_number=turn.turn_number,
                    user_content=turn.user_content,
                    assistant_content=turn.assistant_content,
                    metadata={
                        **session.metadata,
                        "session_aliases": session.session_aliases,
                    },
                    completeness=turn.completeness,
                    origin="capture_service",
                )
            count = raw_store._pool.get_conn().execute(  # noqa: SLF001
                "SELECT COUNT(*) FROM raw_turns WHERE source_agent='kimi'"
            ).fetchone()[0]
            assert count == 1
        finally:
            engine.close()

    def test_v2_child_artifact_binds_legacy_v1_qualified_identity(
        self, source, tmp_path
    ):
        context = (
            tmp_path
            / "sessions"
            / "workspace"
            / "parent-session"
            / "subagents"
            / "worker"
            / "context.jsonl"
        )
        _write_jsonl(context, _context_records("child"))
        session = source.discover_sessions()[0]

        legacy_ids = session.metadata["legacy_canonical_session_ids"]
        assert "parent-session" in legacy_ids
        legacy_qualified = next(
            identity
            for identity in legacy_ids
            if identity.startswith("parent-session::subagent_context::")
        )
        assert legacy_qualified != session.session_id

        config = _KimiRawConfig(tmp_path / "runtime-child-v1-identity")
        raw_store = RawEventStore(
            db_path=config.database_dir / "raw_events.db",
            config=config,
        )
        raw_store.upsert_turn(
            source_agent="kimi",
            session_id=legacy_qualified,
            turn_number=0,
            user_content="historical child",
            assistant_content="v1",
            completeness={
                "visible_text": "full",
                "tool_calls": "unavailable",
                "tool_results": "unavailable",
                "reasoning": "unavailable",
                "attachments": "unavailable",
                "truncated": False,
                "loss_reasons": [],
            },
        )
        engine = SyncEngine(
            backend=Mock(),
            db_path=str(config.database_dir / "sync_log.db"),
            config=config,
            raw_store=raw_store,
        )
        try:
            result = engine.sync_batch(source, [session], incremental=False)
            assert result.failed[0]["error"] == (
                "source_session_identity_reconciliation_required"
            )
        finally:
            engine.close()

    def test_reserved_subagents_reclassification_binds_fixed_point_legacy_identity(
        self, source, tmp_path
    ):
        session_named_subagents = (
            tmp_path
            / "sessions"
            / "workspace-key"
            / "subagents"
            / "context.jsonl"
        )
        worker_named_subagents = (
            tmp_path
            / "sessions"
            / "workspace-key"
            / "parent-session"
            / "subagents"
            / "subagents"
            / "context.jsonl"
        )
        _write_jsonl(session_named_subagents, _context_records("session-name"))
        _write_jsonl(worker_named_subagents, _context_records("worker-name"))

        sessions = source.discover_sessions()
        main = next(info for info in sessions if info.source_kind == "main_context")
        child = next(info for info in sessions if info.source_kind == "subagent_context")
        fixed_point_main_id = next(
            identity
            for identity in main.metadata["legacy_canonical_session_ids"]
            if identity.startswith("workspace-key::subagent_context::")
        )
        fixed_point_child_id = next(
            identity
            for identity in child.metadata["legacy_canonical_session_ids"]
            if identity.startswith("subagents::subagent_context::")
        )

        for index, (session, legacy_id) in enumerate(
            ((main, fixed_point_main_id), (child, fixed_point_child_id))
        ):
            config = _KimiRawConfig(tmp_path / f"runtime-fixed-point-{index}")
            raw_store = RawEventStore(
                db_path=config.database_dir / "raw_events.db",
                config=config,
            )
            raw_store.upsert_turn(
                source_agent="kimi",
                session_id=legacy_id,
                turn_number=0,
                user_content="historical",
                assistant_content="fixed-point-v1",
                completeness={
                    "visible_text": "full",
                    "tool_calls": "unavailable",
                    "tool_results": "unavailable",
                    "reasoning": "unavailable",
                    "attachments": "unavailable",
                    "truncated": False,
                    "loss_reasons": [],
                },
            )
            engine = SyncEngine(
                backend=Mock(),
                db_path=str(config.database_dir / "sync_log.db"),
                config=config,
                raw_store=raw_store,
            )
            try:
                result = engine.sync_batch(source, [session], incremental=False)
                assert result.failed == [
                    {
                        "session_id": session.session_id,
                        "error": "source_session_identity_reconciliation_required",
                    }
                ]
                count = raw_store._pool.get_conn().execute(  # noqa: SLF001
                    "SELECT COUNT(*) FROM raw_turns WHERE source_agent='kimi'"
                ).fetchone()[0]
                assert count == 1
            finally:
                engine.close()

    def test_session_state_fingerprint_depends_on_ordered_bytes_not_mtime(
        self, source, tmp_path
    ):
        session_dir = tmp_path / "sessions" / "workspace" / "stable-session"
        archive = session_dir / "context_1.jsonl"
        active = session_dir / "context.jsonl"
        _write_jsonl(archive, _context_records("archive"))
        _write_jsonl(active, _context_records("active"))

        info = source.discover_sessions()[0]
        before = source.get_session_state(info)
        assert before is not None

        stat = active.stat()
        os.utime(active, (stat.st_atime + 20, stat.st_mtime + 20))
        after = source.get_session_state(info)

        assert after is not None
        assert after["mtime"] != before["mtime"]
        assert after["fingerprint"] == before["fingerprint"]

    def test_context_archive_order_is_deterministic_for_unrecognized_segment_names(
        self, tmp_path
    ):
        first_dir = tmp_path / "first" / "sessions" / "workspace" / "session"
        second_dir = tmp_path / "second" / "sessions" / "workspace" / "session"
        for name, label in (
            ("context_1.jsonl", "numeric-one"),
            ("context_01.jsonl", "numeric-zero-one"),
            ("context_zeta.jsonl", "zeta"),
            ("context_alpha.jsonl", "alpha"),
            ("context.jsonl", "active"),
        ):
            _write_jsonl(first_dir / name, _context_records(label))
        for name, label in (
            ("context.jsonl", "active"),
            ("context_alpha.jsonl", "alpha"),
            ("context_zeta.jsonl", "zeta"),
            ("context_01.jsonl", "numeric-zero-one"),
            ("context_1.jsonl", "numeric-one"),
        ):
            _write_jsonl(second_dir / name, _context_records(label))

        first_source = KimiSource()
        first_source._override_data_dir = tmp_path / "first"
        second_source = KimiSource()
        second_source._override_data_dir = tmp_path / "second"
        first_info = first_source.discover_sessions()[0]
        second_info = second_source.discover_sessions()[0]

        assert [turn.user_content for turn in first_source.parse_session(first_info)] == [
            "user-numeric-zero-one",
            "user-numeric-one",
            "user-alpha",
            "user-zeta",
            "user-active",
        ]
        assert [turn.user_content for turn in second_source.parse_session(second_info)] == [
            "user-numeric-zero-one",
            "user-numeric-one",
            "user-alpha",
            "user-zeta",
            "user-active",
        ]
        assert first_source.get_session_state(first_info)["fingerprint"] == (
            second_source.get_session_state(second_info)["fingerprint"]
        )

    def test_discover_sessions_data_dir_already_sessions(self, source, tmp_path):
        # data_dir 本身已经是 sessions 目录
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        sess_dir = sessions / "sess-2"
        sess_dir.mkdir()
        (sess_dir / "context.jsonl").write_text("{}", encoding="utf-8")
        with patch.object(type(source), "data_dir", new=sessions):
            infos = source.discover_sessions()
        assert len(infos) == 1
        assert infos[0].session_id.startswith("sess-2::main_context::")
        assert infos[0].session_aliases == ["sess-2"]

    def test_workspace_named_subagents_is_not_misclassified_as_child_artifact(self, source, tmp_path):
        sessions = tmp_path / "sessions"
        context = sessions / "subagents" / "sess-3" / "context.jsonl"
        _write_jsonl(context, _context_records("workspace-name"))

        infos = source.discover_sessions()

        assert len(infos) == 1
        assert infos[0].source_kind == "main_context"
        assert infos[0].session_id.startswith("sess-3::main_context::")
        assert infos[0].session_aliases == ["sess-3"]

    def test_session_named_subagents_is_not_misclassified_as_child_artifact(
        self, source, tmp_path
    ):
        session = tmp_path / "sessions" / "workspace-key" / "subagents"
        context = session / "context.jsonl"
        wire = session / "agents" / "main" / "wire.jsonl"
        child = (
            tmp_path
            / "sessions"
            / "workspace-key"
            / "parent-session"
            / "subagents"
            / "subagents"
            / "context.jsonl"
        )
        _write_jsonl(context, _context_records("session-name"))
        _write_jsonl(wire, _wire_records("session-name-wire"))
        _write_jsonl(child, _context_records("worker-name"))

        infos = source.discover_sessions()

        assert {info.source_kind for info in infos} == {
            "main_context",
            "main_wire",
            "subagent_context",
        }
        main_infos = [
            info for info in infos if info.source_kind in {"main_context", "main_wire"}
        ]
        assert {
            info.metadata["native_session_id"] for info in main_infos
        } == {"subagents"}
        assert all(
            info.session_id.startswith(f"subagents::{info.source_kind}::")
            for info in main_infos
        )
        child_info = next(
            info for info in infos if info.source_kind == "subagent_context"
        )
        assert child_info.metadata["native_session_id"] == "parent-session"
        assert child_info.metadata["parent_session_id"] == "parent-session"
        assert child_info.source_path == child

    def test_discover_sessions_missing_dir(self, source):
        with patch.object(type(source), "data_dir", new=Path("/does/not/exist")):
            assert source.discover_sessions() == []

    def test_discover_sessions_data_dir_none(self, source):
        with patch.object(type(source), "data_dir", new=None):
            assert source.discover_sessions() == []

    def test_parse_turns_basic_jsonl(self, source, tmp_path):
        sess_dir = tmp_path / "sess-1"
        sess_dir.mkdir()
        path = sess_dir / "context.jsonl"
        path.write_text(
            json.dumps({"role": "user", "content": "hello"}) + "\n"
            + json.dumps({"role": "assistant", "content": [{"type": "text", "text": "hi"}]})
            + "\n",
            encoding="utf-8",
        )

        turns = source.parse_turns(path)
        assert len(turns) == 1
        assert turns[0].user_content == "hello"
        assert turns[0].assistant_content == "hi"

    def test_parse_turns_preserves_user_timestamp(self, source, tmp_path):
        sess_dir = tmp_path / "sess-1"
        sess_dir.mkdir()
        path = sess_dir / "context.jsonl"
        path.write_text(
            json.dumps(
                {
                    "role": "user",
                    "content": "hello",
                    "timestamp": "2026-06-24T12:00:00Z",
                }
            )
            + "\n"
            + json.dumps({"role": "assistant", "content": [{"type": "text", "text": "hi"}]})
            + "\n",
            encoding="utf-8",
        )

        turns = source.parse_turns(path)

        assert len(turns) == 1
        assert turns[0].timestamp == "2026-06-24T12:00:00Z"
        assert turns[0].metadata["timestamp"] == "2026-06-24T12:00:00Z"

    def test_known_context_and_wire_events_retain_complete_native_envelopes(
        self, source, tmp_path
    ):
        context = tmp_path / "context-envelope" / "context.jsonl"
        _write_jsonl(
            context,
            [
                {
                    "message_id": "user-1",
                    "role": "user",
                    "content": "hello",
                    "future_user_field": {"keep": 1},
                },
                {
                    "message_id": "assistant-1",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "world"}],
                    "future_assistant_field": {"keep": 2},
                },
            ],
        )
        wire = tmp_path / "wire-envelope" / "agents" / "main" / "wire.jsonl"
        _write_jsonl(
            wire,
            [
                {
                    "event_id": "turn-1",
                    "future_envelope": {"keep": 3},
                    "message": {
                        "type": "TurnBegin",
                        "payload": {
                            "user_input": [{"type": "text", "text": "wire-user"}],
                            "future_payload": {"keep": 4},
                        },
                    },
                },
                {
                    "event_id": "part-1",
                    "message": {
                        "type": "ContentPart",
                        "payload": {
                            "type": "text",
                            "text": "wire-assistant",
                            "future_payload": {"keep": 5},
                        },
                    },
                },
            ],
        )

        context_refs = source.parse_turns(context)[0].raw_event_refs
        wire_refs = source.parse_turns(wire)[0].raw_event_refs

        assert any(
            ref.get("event_type") == "native_context_message"
            and ref.get("raw", {}).get("future_user_field") == {"keep": 1}
            for ref in context_refs
        )
        assert any(
            ref.get("event_type") == "native_context_message"
            and ref.get("raw", {}).get("future_assistant_field") == {"keep": 2}
            for ref in context_refs
        )
        assert any(
            ref.get("event_type") == "native_wire_event"
            and ref.get("raw", {}).get("future_envelope") == {"keep": 3}
            for ref in wire_refs
        )
        assert any(
            ref.get("event_type") == "native_wire_event"
            and ref.get("raw", {})
            .get("message", {})
            .get("payload", {})
            .get("future_payload")
            == {"keep": 5}
            for ref in wire_refs
        )

    def test_parse_turns_with_archive_and_wire(self, source, tmp_path):
        sess_dir = tmp_path / "sess-1"
        sess_dir.mkdir()
        (sess_dir / "context_1.jsonl").write_text(
            json.dumps({"role": "user", "content": "q1"}) + "\n", encoding="utf-8"
        )
        (sess_dir / "context.jsonl").write_text(
            json.dumps(
                {"role": "assistant", "content": [{"type": "text", "text": "a1"}]}
            )
            + "\n",
            encoding="utf-8",
        )
        _write_jsonl(sess_dir / "wire.jsonl", _wire_records("wire"))

        turns = source.parse_turns(sess_dir / "context.jsonl")
        # context 只聚合同一 artifact 目录的 context 段，不混入 wire。
        assert len(turns) == 1
        assert turns[0].user_content == "q1"
        assert turns[0].assistant_content == "a1"
        assert all("wire.jsonl" not in sf for sf in turns[0].source_files)
        wire_turns = source.parse_turns(sess_dir / "wire.jsonl")
        assert [(turn.user_content, turn.assistant_content) for turn in wire_turns] == [
            ("user-wire", "assistant-wire")
        ]

    def test_context_archive_does_not_deduplicate_ambiguous_generic_ids(self, source, tmp_path):
        sess_dir = tmp_path / "sess-generic-id"
        _write_jsonl(
            sess_dir / "context_1.jsonl",
            [
                {"id": "session-id", "role": "user", "content": "first-user"},
                {
                    "id": "session-id",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "first-assistant"}],
                },
            ],
        )
        _write_jsonl(
            sess_dir / "context.jsonl",
            [
                {"id": "session-id", "role": "user", "content": "second-user"},
                {
                    "id": "session-id",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "second-assistant"}],
                },
            ],
        )

        turns = source.parse_turns(sess_dir / "context.jsonl")

        assert [(turn.user_content, turn.assistant_content) for turn in turns] == [
            ("first-user", "first-assistant"),
            ("second-user", "second-assistant"),
        ]
        assert all(
            "native_event_id_payload_conflict"
            not in turn.completeness["loss_reasons"]
            for turn in turns
        )
        assert all("native_event_id" not in turn.metadata for turn in turns)

    def test_context_archive_preserves_divergent_payload_under_same_native_ids(
        self, source, tmp_path
    ):
        sess_dir = tmp_path / "sess-native-id-conflict"
        _write_jsonl(
            sess_dir / "context_1.jsonl",
            [
                {
                    "event_id": "user-event",
                    "role": "user",
                    "content": "first-user",
                },
                {
                    "event_id": "assistant-event",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "first-assistant"}],
                },
            ],
        )
        _write_jsonl(
            sess_dir / "context_2.jsonl",
            [
                {
                    "event_id": "user-event",
                    "role": "user",
                    "content": "second-user",
                },
                {
                    "event_id": "assistant-event",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "second-assistant"}],
                },
            ],
        )
        (sess_dir / "context.jsonl").write_text(
            '{"content":"second-user", "role":"user", "event_id":"user-event"}\n'
            '{"content":[{"text":"second-assistant","type":"text"}],'
            '"role":"assistant","event_id":"assistant-event"}\n',
            encoding="utf-8",
        )

        turns = source.parse_turns(sess_dir / "context.jsonl")

        assert [(turn.user_content, turn.assistant_content) for turn in turns] == [
            ("first-user", "first-assistant"),
            ("second-user", "second-assistant"),
        ]
        assert "native_event_id_payload_conflict" in turns[1].completeness["loss_reasons"]
        assert any(
            ref.get("event_type") == "native_event_id_payload_conflict"
            for ref in turns[1].raw_event_refs
        )

    def test_context_archive_uses_json_value_equality_without_boolean_coercion(
        self, source, tmp_path
    ):
        sess_dir = tmp_path / "sess-json-value-equality"
        _write_jsonl(
            sess_dir / "context_1.jsonl",
            [
                {
                    "event_id": "user-event",
                    "role": "user",
                    "content": "first-user",
                    "metadata": {"n": 1},
                },
                {
                    "event_id": "assistant-event",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "first-assistant"}],
                    "metadata": {"n": 1},
                },
            ],
        )
        _write_jsonl(
            sess_dir / "context_2.jsonl",
            [
                {
                    "event_id": "user-event",
                    "role": "user",
                    "content": "first-user",
                    "metadata": {"n": 1.0},
                },
                {
                    "event_id": "assistant-event",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "first-assistant"}],
                    "metadata": {"n": 1.0},
                },
            ],
        )
        _write_jsonl(
            sess_dir / "context.jsonl",
            [
                {
                    "event_id": "user-event",
                    "role": "user",
                    "content": "first-user",
                    "metadata": {"n": True},
                },
                {
                    "event_id": "assistant-event",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "first-assistant"}],
                    "metadata": {"n": True},
                },
            ],
        )

        turns = source.parse_turns(sess_dir / "context.jsonl")

        assert len(turns) == 2
        assert "native_event_id_payload_conflict" not in turns[0].completeness[
            "loss_reasons"
        ]
        assert "native_event_id_payload_conflict" in turns[1].completeness[
            "loss_reasons"
        ]

        precision_dir = tmp_path / "sess-json-number-precision"
        precision_dir.mkdir()
        (precision_dir / "context_1.jsonl").write_text(
            '{"event_id":"large-user","role":"user","content":"large-user",'
            '"metadata":{"n":9007199254740992.0}}\n'
            '{"event_id":"large-assistant","role":"assistant",'
            '"content":[{"type":"text","text":"large-assistant"}],'
            '"metadata":{"n":9007199254740992.0}}\n',
            encoding="utf-8",
        )
        (precision_dir / "context.jsonl").write_text(
            '{"event_id":"large-user","role":"user","content":"large-user",'
            '"metadata":{"n":9007199254740993.0}}\n'
            '{"event_id":"large-assistant","role":"assistant",'
            '"content":[{"type":"text","text":"large-assistant"}],'
            '"metadata":{"n":9007199254740993.0}}\n',
            encoding="utf-8",
        )

        precision_turns = source.parse_turns(precision_dir / "context.jsonl")
        source_lines = [
            ref["source_line"]["raw_text"]
            for turn in precision_turns
            for ref in turn.raw_event_refs
            if ref.get("event_type") == "native_context_message"
        ]

        assert len(precision_turns) == 2
        assert "native_event_id_payload_conflict" in precision_turns[1].completeness[
            "loss_reasons"
        ]
        assert any("9007199254740992.0" in line for line in source_lines)
        assert any("9007199254740993.0" in line for line in source_lines)

    def test_parse_turns_read_failure(self, source, tmp_path):
        path = tmp_path / "nope" / "context.jsonl"
        assert source.parse_turns(path) == []

    def test_parse_turns_preserves_invalid_json_before_visible_turn(self, source, tmp_path):
        sess_dir = tmp_path / "sess-1"
        sess_dir.mkdir()
        path = sess_dir / "context.jsonl"
        path.write_text(
            "not json\n"
            + json.dumps({"role": "user", "content": "hello"}) + "\n"
            + json.dumps({"role": "assistant", "content": [{"type": "text", "text": "hi"}]})
            + "\n",
            encoding="utf-8",
        )

        turns = source.parse_turns(path)
        assert len(turns) == 2
        assert turns[0].raw_event_refs[0]["raw"]["decode_error"] == "invalid_json"
        assert turns[1].user_content == "hello"
        assert turns[1].assistant_content == "hi"

    def test_context_preserves_malformed_non_utf8_and_unknown_native_records(
        self, source, tmp_path
    ):
        path = tmp_path / "sess-malformed" / "context.jsonl"
        path.parent.mkdir(parents=True)
        path.write_bytes(
            b"\xff\xfe\n"
            b"{not-json\n"
            b"42\n"
            + json.dumps(
                {"role": "custom-role", "payload": {"important": True}}
            ).encode("utf-8")
            + b"\n"
            + json.dumps(
                {
                    "role": "user",
                    "content": [{"type": "text", "text": 0}],
                }
            ).encode("utf-8")
            + b"\n"
            + json.dumps(
                {
                    "role": "assistant",
                    "content": [
                        {"type": "think", "think": ["not", "text"]},
                        {"type": "text", "text": {"not": "text"}},
                    ],
                }
            ).encode("utf-8")
            + b"\n"
            + json.dumps(
                {
                    "role": "tool",
                    "content": [{"type": "text", "text": ["not", "text"]}],
                }
            ).encode("utf-8")
            + b"\n"
        )

        turns = source.parse_turns(path)
        refs = [ref for turn in turns for ref in turn.raw_event_refs]

        assert {
            ref.get("raw", {}).get("decode_error")
            for ref in refs
            if ref.get("event_type") == "native_jsonl_decode_error"
        } == {"invalid_utf8", "invalid_json", "non_object_json"}
        assert any(
            ref.get("event_type") == "unknown_context_role"
            and ref.get("raw", {}).get("payload") == {"important": True}
            for ref in refs
        )
        assert {
            ref.get("event_type")
            for ref in refs
            if str(ref.get("event_type", "")).startswith("malformed_")
        } >= {
            "malformed_user_text",
            "malformed_assistant_text",
            "malformed_assistant_think",
            "malformed_tool_text",
        }
        assert all(turn.completeness["truncated"] is False for turn in turns)

    def test_context_and_wire_preserve_invalid_json_scalars_and_timestamps(
        self, source, tmp_path
    ):
        context = tmp_path / "timestamp-context" / "context.jsonl"
        context.parent.mkdir(parents=True)
        context.write_text(
            '{"event_id":"surrogate","role":"user","content":"\\ud800"}\n'
            '{"role":"user","content":"nan","timestamp":NaN}\n'
            '{"event_id":"duplicate","role":"user","content":"first",'
            '"content":"second"}\n'
            + "[" * 2000
            + "0"
            + "]" * 2000
            + "\n"
            + '{"event_id":"deep","role":"user","content":"deep","metadata":'
            + "[" * 600
            + "0"
            + "]" * 600
            + "}\n"
            + json.dumps(
                {
                    "role": "user",
                    "content": "range-user",
                    "timestamp": 10**100,
                }
            )
            + "\n"
            + json.dumps(
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "range-assistant"}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        wire = tmp_path / "timestamp-wire" / "agents" / "main" / "wire.jsonl"
        _write_jsonl(
            wire,
            [
                {
                    "timestamp": 10**100,
                    "message": {
                        "type": "TurnBegin",
                        "payload": {
                            "user_input": [{"type": "text", "text": "wire-user"}]
                        },
                    },
                },
                {
                    "message": {
                        "type": "ContentPart",
                        "payload": {"type": "text", "text": "wire-assistant"},
                    }
                },
            ],
        )

        context_turns = source.parse_turns(context)
        wire_turns = source.parse_turns(wire)
        context_refs = [
            ref for turn in context_turns for ref in turn.raw_event_refs
        ]
        wire_refs = [ref for turn in wire_turns for ref in turn.raw_event_refs]

        assert {
            ref.get("raw", {}).get("decode_error")
            for ref in context_refs
            if ref.get("event_type") == "native_jsonl_decode_error"
        } == {
            "duplicate_json_key",
            "invalid_json",
            "invalid_unicode_scalar",
            "json_nesting_too_deep",
        }
        assert (
            context_turns[-1].user_content,
            context_turns[-1].assistant_content,
        ) == ("range-user", "range-assistant")
        assert any(
            ref.get("event_type") == "malformed_native_timestamp"
            and ref.get("raw", {}).get("value") == 10**100
            for ref in context_refs
        )
        assert [
            (turn.user_content, turn.assistant_content) for turn in wire_turns
        ] == [("wire-user", "wire-assistant")]
        assert any(
            ref.get("event_type") == "malformed_native_timestamp"
            and ref.get("raw", {}).get("value") == 10**100
            for ref in wire_refs
        )

    def test_extreme_json_number_persists_as_valid_canonical_raw(
        self, source, tmp_path
    ):
        context = (
            tmp_path
            / "sessions"
            / "workspace"
            / "extreme-number"
            / "context.jsonl"
        )
        context.parent.mkdir(parents=True)
        context.write_text(
            '{"event_id":"extreme-user","role":"user","content":"question",'
            '"metadata":{"n":1e999999999}}\n'
            '{"event_id":"extreme-assistant","role":"assistant",'
            '"content":[{"type":"text","text":"answer"}]}\n',
            encoding="utf-8",
        )
        session = source.discover_sessions()[0]
        config = _KimiRawConfig(tmp_path / "runtime-extreme-number")
        raw_store = RawEventStore(
            db_path=config.database_dir / "raw_events.db",
            config=config,
        )
        backend = Mock()
        backend.list_by_tags.return_value = []
        backend.save.return_value = []
        engine = SyncEngine(
            backend=backend,
            db_path=str(config.database_dir / "sync_log.db"),
            config=config,
            raw_store=raw_store,
        )
        try:
            result = engine.sync_batch(source, [session], incremental=False)
            assert result.failed == []
            raw_json = raw_store._pool.get_conn().execute(  # noqa: SLF001
                """
                SELECT raw_event_refs_json
                FROM raw_turns
                WHERE source_agent='kimi' AND session_id=?
                """,
                (session.session_id,),
            ).fetchone()[0]
            assert raw_store._pool.get_conn().execute(  # noqa: SLF001
                "SELECT json_valid(?)",
                (raw_json,),
            ).fetchone()[0] == 1
            decoded = json.loads(raw_json)
            user_envelope = next(
                ref
                for ref in decoded
                if ref.get("event_type") == "native_context_message"
                and ref.get("role") == "user"
            )
            assert user_envelope["raw"]["metadata"]["n"] == {
                "_mnemos_json_number": "1e999999999",
                "decode_warning": "non_finite_runtime_float",
            }
            assert "1e999999999" in user_envelope["source_line"]["raw_text"]
        finally:
            engine.close()

    def test_parse_turns_system_checkpoint_usage_between_messages(self, source, tmp_path):
        sess_dir = tmp_path / "sess-1"
        sess_dir.mkdir()
        path = sess_dir / "context.jsonl"
        path.write_text(
            json.dumps({"role": "user", "content": "hello"}) + "\n"
            + json.dumps({"role": "system", "content": "sys"}) + "\n"
            + json.dumps({"role": "_system_prompt", "content": "prompt"}) + "\n"
            + json.dumps({"role": "_checkpoint", "content": "c"}) + "\n"
            + json.dumps({"role": "_usage", "content": "u"}) + "\n"
            + json.dumps({"role": "assistant", "content": [{"type": "text", "text": "hi"}]})
            + "\n",
            encoding="utf-8",
        )

        turns = source.parse_turns(path)
        assert len(turns) == 1
        assert turns[0].user_content == "hello"
        assert turns[0].assistant_content == "hi"
        assert any(ref["role"] == "system" for ref in turns[0].raw_event_refs)

    def test_parse_turns_reasoning_think(self, source, tmp_path):
        sess_dir = tmp_path / "sess-1"
        sess_dir.mkdir()
        path = sess_dir / "context.jsonl"
        path.write_text(
            json.dumps({"role": "user", "content": "hello"}) + "\n"
            + json.dumps(
                {
                    "role": "assistant",
                    "content": [
                        {"type": "think", "think": "thinking..."},
                        {"type": "text", "text": "hi"},
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        turns = source.parse_turns(path)
        assert len(turns) == 1
        assert turns[0].reasoning == "thinking..."
        assert turns[0].metadata.get("reasoning") == "thinking..."

    def test_parse_turns_tool_use(self, source, tmp_path):
        sess_dir = tmp_path / "sess-1"
        sess_dir.mkdir()
        path = sess_dir / "context.jsonl"
        path.write_text(
            json.dumps({"role": "user", "content": "run"}) + "\n"
            + json.dumps(
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "name": "bash", "input": {"cmd": "ls"}, "id": "t1"}
                    ],
                }
            )
            + "\n"
            + json.dumps(
                {
                    "role": "tool",
                    "content": [{"type": "text", "text": "stdout"}],
                    "tool_call_id": "t1",
                    "name": "bash",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        turns = source.parse_turns(path)
        assert len(turns) == 1
        assert turns[0].tool_calls[0]["name"] == "bash"
        assert turns[0].tool_results[0]["content"] == "stdout"

    def test_parse_turns_user_list_content(self, source, tmp_path):
        sess_dir = tmp_path / "sess-1"
        sess_dir.mkdir()
        path = sess_dir / "context.jsonl"
        path.write_text(
            json.dumps(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "line1"},
                        {"type": "text", "text": "line2"},
                        {"type": "image", "url": "http://x"},
                    ],
                }
            )
            + "\n"
            + json.dumps({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
            + "\n",
            encoding="utf-8",
        )

        turns = source.parse_turns(path)
        assert len(turns) == 1
        assert turns[0].user_content == "line1\nline2"
        assert turns[0].attachments[0]["url"] == "http://x"
        assert turns[0].completeness["attachments"] == "full"

    def test_parse_turns_unknown_assistant_block(self, source, tmp_path):
        sess_dir = tmp_path / "sess-1"
        sess_dir.mkdir()
        path = sess_dir / "context.jsonl"
        path.write_text(
            json.dumps({"role": "user", "content": "hello"}) + "\n"
            + json.dumps(
                {
                    "role": "assistant",
                    "content": [
                        {"type": "unknown_type", "data": "x"},
                        {"type": "text", "text": "hi"},
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        turns = source.parse_turns(path)
        assert len(turns) == 1
        assert "assistant_unknown_block:unknown_type" in turns[0].completeness["loss_reasons"]
        assert turns[0].assistant_content == "hi"

    def test_parse_turns_consecutive_users(self, source, tmp_path):
        sess_dir = tmp_path / "sess-1"
        sess_dir.mkdir()
        path = sess_dir / "context.jsonl"
        path.write_text(
            json.dumps({"role": "user", "content": "first"}) + "\n"
            + json.dumps({"role": "user", "content": "second"}) + "\n"
            + json.dumps({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
            + "\n",
            encoding="utf-8",
        )

        turns = source.parse_turns(path)
        assert [(turn.user_content, turn.assistant_content) for turn in turns] == [
            ("first", ""),
            ("second", "ok"),
        ]

    def test_parse_turns_preserves_consecutive_assistant_payloads(self, source, tmp_path):
        sess_dir = tmp_path / "sess-consecutive-assistant"
        path = sess_dir / "context.jsonl"
        _write_jsonl(
            path,
            [
                {"role": "user", "content": "question"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "think", "think": "reason-one"},
                        {"type": "text", "text": "answer-one"},
                    ],
                },
                {
                    "role": "assistant",
                    "content": [
                        {"type": "think", "think": "reason-two"},
                        {"type": "text", "text": "answer-two"},
                    ],
                },
            ],
        )

        turns = source.parse_turns(path)

        assert len(turns) == 1
        assert turns[0].assistant_content == "answer-one\n\nanswer-two"
        assert turns[0].reasoning == "reason-one\nreason-two"

    def test_parse_turns_string_assistant_content(self, source, tmp_path):
        sess_dir = tmp_path / "sess-1"
        sess_dir.mkdir()
        path = sess_dir / "context.jsonl"
        path.write_text(
            json.dumps({"role": "user", "content": "hello"}) + "\n"
            + json.dumps({"role": "assistant", "content": "plain text"}) + "\n",
            encoding="utf-8",
        )

        turns = source.parse_turns(path)
        assert len(turns) == 1
        assert turns[0].assistant_content == "plain text"

    def test_parse_turns_tool_string_content(self, source, tmp_path):
        sess_dir = tmp_path / "sess-1"
        sess_dir.mkdir()
        path = sess_dir / "context.jsonl"
        path.write_text(
            json.dumps({"role": "user", "content": "run"}) + "\n"
            + json.dumps({"role": "assistant", "content": [{"type": "tool_use", "name": "bash", "id": "t1"}]})
            + "\n"
            + json.dumps({"role": "tool", "content": "result", "tool_call_id": "t1"}) + "\n",
            encoding="utf-8",
        )

        turns = source.parse_turns(path)
        assert len(turns) == 1
        assert turns[0].tool_results[0]["content"] == "result"

    def test_parse_turns_tool_unknown_item(self, source, tmp_path):
        sess_dir = tmp_path / "sess-1"
        sess_dir.mkdir()
        path = sess_dir / "context.jsonl"
        path.write_text(
            json.dumps({"role": "user", "content": "run"}) + "\n"
            + json.dumps({"role": "assistant", "content": [{"type": "tool_use", "name": "bash", "id": "t1"}]})
            + "\n"
            + json.dumps({"role": "tool", "content": [{"type": "image", "data": "x"}], "tool_call_id": "t1"}) + "\n",
            encoding="utf-8",
        )

        turns = source.parse_turns(path)
        assert len(turns) == 1
        assert turns[0].attachments[0]["role"] == "tool"
        assert any(ref["role"] == "tool" for ref in turns[0].raw_event_refs)

    def test_parse_wire_only_session(self, source, tmp_path):
        sess_dir = tmp_path / "agents" / "main"
        sess_dir.mkdir(parents=True)
        path = sess_dir / "wire.jsonl"
        path.write_text(
            json.dumps(
                {
                    "timestamp": 1_760_000_000,
                    "message": {
                        "type": "TurnBegin",
                        "payload": {
                            "user_input": [
                                {"type": "text", "text": "hello"},
                                {"type": "file", "path": "README.md"},
                            ]
                        },
                    },
                }
            )
            + "\n"
            + json.dumps(
                {
                    "message": {
                        "type": "ContentPart",
                        "payload": {"type": "think", "think": "thinking"},
                    }
                }
            )
            + "\n"
            + json.dumps(
                {
                    "message": {
                        "type": "ToolCall",
                        "payload": {
                            "id": "tool-1",
                            "function": {"name": "Shell", "arguments": "{\"cmd\":\"pwd\"}"},
                        },
                    }
                }
            )
            + "\n"
            + json.dumps(
                {
                    "message": {
                        "type": "ToolResult",
                        "payload": {"tool_call_id": "tool-1", "return_value": "ok"},
                    }
                }
            )
            + "\n"
            + json.dumps(
                {
                    "message": {
                        "type": "ContentPart",
                        "payload": {"type": "text", "text": "done"},
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )

        turns = source.parse_turns(path)

        assert len(turns) == 1
        turn = turns[0]
        assert turn.user_content == "hello"
        assert turn.assistant_content == "done"
        assert turn.reasoning == "thinking"
        assert turn.tool_calls[0]["name"] == "Shell"
        assert turn.tool_results[0]["content"] == "ok"
        assert turn.attachments[0]["path"] == "README.md"
        assert turn.completeness["tool_calls"] == "full"
        assert turn.completeness["attachments"] == "full"

    def test_wire_preserves_decode_errors_unknown_events_and_malformed_payloads(
        self, source, tmp_path
    ):
        path = tmp_path / "wire-malformed" / "agents" / "main" / "wire.jsonl"
        path.parent.mkdir(parents=True)
        path.write_bytes(
            b"\xff\n"
            b"{bad-json\n"
            b"[]\n"
            + json.dumps(
                {
                    "message": {
                        "type": "FutureEvent",
                        "payload": {"important": "value"},
                    }
                }
            ).encode("utf-8")
            + b"\n"
            + json.dumps(
                {
                    "message": {
                        "type": "ContentPart",
                        "payload": "malformed-content-part",
                    }
                }
            ).encode("utf-8")
            + b"\n"
            + json.dumps(
                {
                    "message": {
                        "type": "TurnBegin",
                        "payload": {
                            "user_input": [{"type": "text", "text": {"bad": 1}}]
                        },
                    }
                }
            ).encode("utf-8")
            + b"\n"
            + json.dumps(
                {
                    "message": {
                        "type": "ContentPart",
                        "payload": {"type": "think", "think": ["bad"]},
                    }
                }
            ).encode("utf-8")
            + b"\n"
            + json.dumps(
                {
                    "message": {
                        "type": "ContentPart",
                        "payload": {"type": "text", "text": {"bad": 2}},
                    }
                }
            ).encode("utf-8")
            + b"\n"
        )

        turns = source.parse_turns(path)
        refs = [ref for turn in turns for ref in turn.raw_event_refs]

        assert {
            ref.get("raw", {}).get("decode_error")
            for ref in refs
            if ref.get("event_type") == "native_jsonl_decode_error"
        } == {"invalid_utf8", "invalid_json", "non_object_json"}
        assert any(
            ref.get("event_type") == "FutureEvent"
            and ref.get("raw", {}).get("message", {}).get("payload")
            == {"important": "value"}
            for ref in refs
        )
        assert any(
            ref.get("event_type") == "malformed_wire_payload"
            and ref.get("raw") == "malformed-content-part"
            for ref in refs
        )
        assert {
            ref.get("event_type")
            for ref in refs
            if str(ref.get("event_type", "")).startswith("malformed_wire_")
        } >= {
            "malformed_wire_payload",
            "malformed_wire_user_text",
            "malformed_wire_think",
            "malformed_wire_text",
        }

    def test_discover_official_kimi_code_agents_main_wire_session(self, tmp_path):
        source = KimiSource()
        wire = (
            tmp_path
            / "sessions"
            / "work-key"
            / "session-1"
            / "agents"
            / "main"
            / "wire.jsonl"
        )
        wire.parent.mkdir(parents=True)
        wire.write_text(
            json.dumps(
                {
                    "message": {
                        "type": "TurnBegin",
                        "payload": {"user_input": [{"type": "text", "text": "hello"}]},
                    }
                }
            )
            + "\n"
            + json.dumps(
                {
                    "message": {
                        "type": "ContentPart",
                        "payload": {"type": "text", "text": "world"},
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )
        source._override_data_dir = tmp_path

        sessions = source.discover_sessions()

        assert len(sessions) == 1
        assert sessions[0].session_id.startswith("session-1::main_wire::")
        assert sessions[0].canonical_session_id == sessions[0].session_id
        assert sessions[0].session_aliases == ["session-1"]
        assert sessions[0].source_kind == "main_wire"
        assert sessions[0].metadata["parent_session_id"] == "session-1"
        assert sessions[0].source_path == wire
        assert sessions[0].working_dir == str(tmp_path / "sessions" / "work-key")
        turns = source.parse_session(sessions[0])
        assert len(turns) == 1
        assert turns[0].user_content == "hello"
        assert turns[0].assistant_content == "world"

    def test_context_file_sort_key(self):
        assert KimiSource._context_file_sort_key(Path("context.jsonl")) == (2, 0, "")
        assert KimiSource._context_file_sort_key(Path("context_1.jsonl")) == (
            0,
            1,
            "context_1.jsonl",
        )
        assert KimiSource._context_file_sort_key(Path("context_10.jsonl")) == (
            0,
            10,
            "context_10.jsonl",
        )
        assert KimiSource._context_file_sort_key(Path("other.jsonl")) == (
            1,
            0,
            "other.jsonl",
        )

    def test_get_session_state(self, source, tmp_path):
        sess_dir = tmp_path / "sess-1"
        sess_dir.mkdir()
        path = sess_dir / "context.jsonl"
        path.write_text("data\n", encoding="utf-8")
        info = SessionInfo(session_id="sess-1", source_path=path)
        state = source.get_session_state(info)
        assert state is not None
        assert state["file_count"] == 1
        assert state["size"] == 5
        assert "fingerprint" in state

    def test_get_session_state_no_files(self, source, tmp_path):
        sess_dir = tmp_path / "sess-1"
        sess_dir.mkdir()
        info = SessionInfo(session_id="sess-1", source_path=sess_dir / "context.jsonl")
        assert source.get_session_state(info) is None

    def test_build_extra_tags_with_reasoning(self, source):
        turn = Turn(turn_number=0, user_content="q", assistant_content="a", metadata={"reasoning": "r"})
        assert source.build_extra_tags(turn) == ["has-reasoning=true"]

    def test_build_extra_tags_without_reasoning(self, source):
        turn = Turn(turn_number=0, user_content="q", assistant_content="a", metadata={})
        assert source.build_extra_tags(turn) == []


class TestKimiDataDir:
    def test_data_dir_with_kimi_home_env(self, tmp_path, monkeypatch):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        monkeypatch.setenv("KIMI_HOME", str(tmp_path))
        monkeypatch.delenv("KIMI_CODE_HOME", raising=False)
        src = KimiSource()
        assert src.data_dir == sessions

    def test_data_dir_falls_back_to_home(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        sessions = home / ".kimi" / "sessions"
        sessions.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("KIMI_CODE_HOME", raising=False)
        monkeypatch.delenv("KIMI_HOME", raising=False)
        src = KimiSource()
        assert src.data_dir == sessions

    def test_data_dir_falls_back_to_parent(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        kimi_home = home / ".kimi"
        kimi_home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("KIMI_CODE_HOME", raising=False)
        monkeypatch.delenv("KIMI_HOME", raising=False)
        src = KimiSource()
        # sessions 不存在，回退到 .kimi
        assert src.data_dir == kimi_home

    def test_data_dir_falls_back_to_env_parent(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        env_parent = tmp_path / "kimi"
        env_parent.mkdir()
        # 让标准路径不存在，env 的 sessions 也不存在，但 env 父目录存在
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("KIMI_CODE_HOME", raising=False)
        monkeypatch.setenv("KIMI_HOME", str(env_parent))
        src = KimiSource()
        assert src.data_dir == env_parent

    def test_data_dir_returns_none_when_missing(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("KIMI_CODE_HOME", raising=False)
        monkeypatch.delenv("KIMI_HOME", raising=False)
        src = KimiSource()
        assert src.data_dir is None

    def test_data_dir_supports_kimi_code_home(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        kimi_code = tmp_path / "kimi-code"
        agents = kimi_code / "agents" / "main"
        agents.mkdir(parents=True)
        (agents / "wire.jsonl").write_text("{}", encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("KIMI_CODE_HOME", str(kimi_code))
        monkeypatch.delenv("KIMI_HOME", raising=False)

        src = KimiSource()

        assert src.data_dir == kimi_code
