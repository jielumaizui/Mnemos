# -*- coding: utf-8 -*-
"""Tests for integrations.sources.openclaw_source."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from core.sync_framework.agent_source import SessionInfo
from core.sync_framework.raw_event_store import (
    RawEventIdentitySchemaMigrationRequired,
    RawEventStore,
)
from core.sync_framework.sync_engine import SyncEngine
from integrations.sources.openclaw_source import OpenClawSource


@pytest.fixture
def source(tmp_path):
    src = OpenClawSource()
    with patch.object(type(src), "data_dir", new=tmp_path / ".openclaw"):
        yield src


class _OpenClawRawConfig:
    """Hermetic config for multi-format OpenClaw Native-to-Raw verification."""

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


def _write_completed_jsonl(path: Path, session_id: str, pairs: list[tuple[str, str]]) -> None:
    """Write the native completed-event shape used by trajectory session logs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "type": "model.completed",
            "sessionId": session_id,
            "ts": f"2026-07-12T00:00:0{index}Z",
            "data": {
                "messagesSnapshot": [
                    {"role": "user", "content": [{"type": "text", "text": user}]},
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": assistant}],
                    },
                ]
            },
        }
        for index, (user, assistant) in enumerate(pairs)
    ]
    path.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )


def _write_normal_jsonl(path: Path, session_id: str, pairs: list[tuple[str, str]]) -> None:
    """Write ordinary role/content JSONL records for the normal-session parser."""
    path.parent.mkdir(parents=True, exist_ok=True)
    records = []
    for index, (user, assistant) in enumerate(pairs):
        records.extend(
            [
                {
                    "sessionId": session_id,
                    "role": "user",
                    "content": user,
                    "ts": f"2026-07-12T00:01:{index:02d}Z",
                },
                {
                    "sessionId": session_id,
                    "role": "assistant",
                    "content": assistant,
                    "ts": f"2026-07-12T00:01:{index:02d}Z",
                },
            ]
        )
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )


class TestOpenClawSource:
    def test_name_and_model_tag(self, source):
        assert source.name == "openclaw"
        assert source.model_tag == "openclaw"

    def test_trigger_strategy(self, source):
        strategy = source.trigger_strategy
        assert strategy["type"] == "polling"
        assert strategy["interval"] == 3600

    def test_completeness_capabilities(self, source):
        caps = source.completeness_capabilities()
        assert caps["visible_text"] is True
        assert caps["raw_files"] is True
        assert caps["source_fidelity"] == "full"

    def test_discover_sessions(self, source, tmp_path):
        corpus = tmp_path / ".openclaw" / "workspace" / "memory" / ".dreams" / "session-corpus"
        corpus.mkdir(parents=True)
        (corpus / "2026-06-24.txt").write_text("", encoding="utf-8")

        infos = source.discover_sessions()
        assert len(infos) == 1
        assert infos[0].session_id == "2026-06-24"

    def test_discover_sessions_keeps_trajectory_and_corpus_in_one_denominator(self, source, tmp_path):
        sessions = tmp_path / ".openclaw" / "agents" / "main" / "sessions"
        sessions.mkdir(parents=True)
        trajectory = sessions / "sess-1.trajectory.jsonl"
        trajectory.write_text("{}", encoding="utf-8")
        corpus = tmp_path / ".openclaw" / "workspace" / "memory" / ".dreams" / "session-corpus"
        corpus.mkdir(parents=True)
        (corpus / "2026-06-24.txt").write_text("", encoding="utf-8")

        infos = source.discover_sessions()

        assert {info.session_id for info in infos} == {"sess-1", "2026-06-24"}
        trajectory_info = next(info for info in infos if info.session_id == "sess-1")
        assert trajectory_info.source_path == trajectory
        assert trajectory_info.source_kind == "trajectory"

    def test_discover_sessions_missing_dir(self, source):
        assert source.discover_sessions() == []

    def test_parse_turns(self, source, tmp_path):
        path = tmp_path / "2026-06-24.txt"
        path.write_text(
            "[/sessions/abc-123#L1] User: hello\n"
            "[/sessions/abc-123#L2] Assistant: hi there\n"
            "[/sessions/abc-123#L3] User: bye\n"
            "[/sessions/abc-123#L4] Assistant: goodbye\n",
            encoding="utf-8",
        )

        turns = source.parse_turns(path)
        assert len(turns) == 2
        assert turns[0].user_content == "hello"
        assert turns[0].assistant_content == "hi there"
        assert turns[0].metadata["session_id"] == "abc-123"
        assert turns[0].timestamp == "2026-06-24T00:00:00"
        assert turns[1].user_content == "bye"

    def test_parse_turns_multiline_sessions(self, source, tmp_path):
        path = tmp_path / "2026-06-24.txt"
        path.write_text(
            "[/sessions/aaa#L1] User: u1\n"
            "[/sessions/aaa#L2] Assistant: a1\n"
            "[/sessions/bbb#L1] User: u2\n"
            "[/sessions/bbb#L2] Assistant: a2\n",
            encoding="utf-8",
        )

        turns = source.parse_turns(path)
        assert len(turns) == 2
        assert turns[0].metadata["session_id"] == "aaa"
        assert turns[1].metadata["session_id"] == "bbb"
        assert {
            (
                ref.get("raw", {}).get("path"),
                ref.get("raw", {}).get("line"),
            )
            for ref in turns[0].raw_event_refs
            if ref.get("event_type") == "corpus_line_provenance"
        } == {
            ("/sessions/aaa", 1),
            ("/sessions/aaa", 2),
        }

        exact_turns = source.parse_session(SessionInfo(session_id="bbb", source_path=path))
        assert [(turn.user_content, turn.assistant_content) for turn in exact_turns] == [
            ("u2", "a2")
        ]

        legacy_turns = source.parse_session(
            SessionInfo(
                session_id="2026-06-24",
                source_path=path,
                source_kind="corpus_fallback",
            )
        )
        assert [(turn.user_content, turn.assistant_content) for turn in legacy_turns] == [
            ("u1", "a1"),
            ("u2", "a2"),
        ]

    def test_parse_turns_invalid_line(self, source, tmp_path):
        path = tmp_path / "2026-06-24.txt"
        path.write_text(
            "not a valid line\n"
            "[/sessions/x#L1] User: hello\n"
            "[/sessions/x#L2] Assistant: hi\n",
            encoding="utf-8",
        )

        turns = source.parse_turns(path)
        assert len(turns) == 2
        assert turns[0].user_content == ""
        assert turns[0].assistant_content == ""
        assert turns[0].raw_event_refs == [
            {
                "line_number": 1,
                "raw": "not a valid line",
                "parse_error": "unmatched_corpus_line",
            }
        ]

    def test_corpus_preserves_non_utf8_line_in_fallback_session(
        self, source, tmp_path: Path
    ):
        path = tmp_path / "2026-07-13.txt"
        path.write_bytes(
            b"[/sessions/exact#L1] User: hello\n"
            b"[/sessions/exact#L2] Assistant: answer\n"
            b"{\xffbroken-corpus\n"
        )

        parsed = source._parse_corpus(path)
        exact = source._pair_messages(parsed["exact"], "exact")
        fallback = source._pair_messages(parsed["2026-07-13"], "2026-07-13")

        assert [(turn.user_content, turn.assistant_content) for turn in exact] == [
            ("hello", "answer")
        ]
        assert fallback[0].raw_event_refs == [
            {
                "line_number": 3,
                "raw_base64": "e/9icm9rZW4tY29ycHVz",
                "raw_encoding": "base64",
                "decode_error": "invalid_utf8",
            }
        ]

    def test_parse_normal_jsonl_nested_messages(self, source, tmp_path):
        path = tmp_path / "normal-session.jsonl"
        path.write_text(
            json.dumps(
                {
                    "data": {
                        "sessionId": "nested-native-session",
                        "messages": [
                            {"role": "user", "content": {"text": "nested user message"}},
                            {
                                "role": "assistant",
                                "content": {"text": "nested assistant answer"},
                            },
                        ],
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )

        discovered = source._candidate_from_jsonl(path, "normal_jsonl")
        turns = source.parse_turns(path)

        assert discovered is not None
        assert discovered.native_session_id == "nested-native-session"
        assert [(turn.user_content, turn.assistant_content) for turn in turns] == [
            ("nested user message", "nested assistant answer")
        ]

    def test_normal_jsonl_preserves_same_role_and_standalone_payload_to_raw(
        self, tmp_path: Path
    ):
        """Known, unknown, malformed, and non-UTF8 native events must remain recoverable."""
        base = tmp_path / ".openclaw"
        path = base / "agents" / "main" / "sessions" / "lossless.jsonl"
        path.parent.mkdir(parents=True)
        records = [
            {
                "sessionId": "lossless",
                "role": "user",
                "content": "first user without assistant",
                "ts": "2026-07-13T00:00:01Z",
            },
            {
                "sessionId": "lossless",
                "role": "user",
                "content": "second user",
                "ts": "2026-07-13T00:00:02Z",
            },
            {
                "sessionId": "lossless",
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "second answer"},
                    {"type": "future_block", "opaque": "future block payload"},
                ],
                "reasoning": "preserved reasoning",
                "future_field": {"opaque": "future field payload"},
            },
            {
                "sessionId": "lossless",
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "call-1", "name": "inspect", "arguments": {}},
                    "opaque-malformed-call",
                ],
            },
            {
                "sessionId": "lossless",
                "role": "tool",
                "tool_results": [{"id": "call-1", "output": "tool output"}],
            },
            {
                "sessionId": "lossless",
                "role": "future-role",
                "content": {"opaque": "future payload"},
            },
            {
                "sessionId": "lossless",
                "opaque": {"roleless": "must reach Raw"},
            },
            {
                "sessionId": "lossless",
                "data": {"message": "malformed nested message"},
            },
            {
                "sessionId": "lossless",
                "role": "assistant",
                "content": {"text": 0},
                "reasoning": 0,
                "messageId": "message-shape-1",
            },
        ]
        native = b"".join(
            (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
            for record in records
        )
        path.write_bytes(
            native
            + b"[\"non-object-json\"]\n"
            + b"{invalid-json\n"
            + b"{\xffbroken-json\n"
        )

        source = OpenClawSource()
        source._override_data_dir = base
        session = next(
            item
            for item in source.discover_sessions()
            if item.metadata["native_session_id"] == "lossless"
        )
        turns = source.parse_session(session)

        assert [(turn.user_content, turn.assistant_content) for turn in turns] == [
            ("first user without assistant", ""),
            ("second user", "second answer"),
        ]
        assert turns[1].tool_calls == [
            {"id": "call-1", "name": "inspect", "arguments": {}}
        ]
        assert turns[1].tool_results == [{"id": "call-1", "output": "tool output"}]
        assert turns[1].reasoning == "preserved reasoning"
        assert turns[0].timestamp == "2026-07-13T00:00:01Z"
        assert turns[1].timestamp == "2026-07-13T00:00:02Z"

        raw_only_path = base / "agents" / "main" / "sessions" / "raw-only.jsonl"
        raw_only_path.write_text(
            json.dumps(
                {
                    "sessionId": "raw-only",
                    "opaque": {"standalone": "must produce its own Raw turn"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        raw_only_turns = source.parse_turns(raw_only_path)
        assert len(raw_only_turns) == 1
        assert raw_only_turns[0].user_content == ""
        assert raw_only_turns[0].assistant_content == ""
        assert raw_only_turns[0].raw_event_refs == [
            {
                "event_type": "unparsed_normal_event",
                "raw": {
                    "sessionId": "raw-only",
                    "opaque": {
                        "standalone": "must produce its own Raw turn"
                    },
                },
            }
        ]

        edge_path = tmp_path / "normal-known-edge.jsonl"
        edge_path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "sessionId": "normal-edge",
                            "session_id": "conflicting-normal-edge",
                            "role": "user",
                            "content": "",
                        }
                    ),
                    json.dumps(
                        {
                            "sessionId": "normal-edge",
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [{"id": "snake-call"}],
                            "toolCalls": [{"id": "camel-call"}],
                            "tool_results": [{"id": "snake-result"}],
                            "toolResults": [{"id": "camel-result"}],
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        edge_turns = source.parse_turns(edge_path)
        assert len(edge_turns) == 1
        edge_refs = edge_turns[0].raw_event_refs
        assert any(
            ref.get("event_type") == "empty_normal_message"
            for ref in edge_refs
        )
        assert any(
            ref.get("event_type") == "conflicting_session_identity_fields"
            for ref in edge_refs
        )
        assert any(
            ref.get("event_type") == "conflicting_tool_call_fields"
            for ref in edge_refs
        )
        assert any(
            ref.get("event_type") == "conflicting_tool_result_fields"
            for ref in edge_refs
        )

        config = _OpenClawRawConfig(tmp_path / "runtime-lossless")
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
            rows = raw_store._pool.get_conn().execute(  # noqa: SLF001
                """
                SELECT current_revision_id FROM raw_turns
                WHERE source_agent='openclaw' AND session_id=?
                ORDER BY turn_number
                """,
                (session.session_id,),
            ).fetchall()
            stored = [raw_store.get_turn(row[0]) for row in rows]
            refs = [
                ref
                for turn in stored
                if turn is not None
                for ref in turn["raw_event_refs"]
            ]
            assert any(ref.get("role") == "future-role" for ref in refs)
            assert any(
                ref.get("event_type") == "unparsed_content_block"
                and ref.get("raw", {}).get("opaque") == "future block payload"
                for ref in refs
            )
            assert any(
                ref.get("event_type") == "normal_message_residual"
                and ref.get("raw", {}).get("future_field")
                == {"opaque": "future field payload"}
                for ref in refs
            )
            assert any(
                ref.get("event_type") == "malformed_tool_calls"
                and ref.get("raw") == "opaque-malformed-call"
                for ref in refs
            )
            assert any(
                ref.get("event_type") == "unparsed_normal_event"
                and ref.get("raw", {}).get("opaque")
                == {"roleless": "must reach Raw"}
                for ref in refs
            )
            assert any(
                ref.get("event_type") == "unparsed_normal_event"
                and ref.get("raw", {}).get("data", {}).get("message")
                == "malformed nested message"
                for ref in refs
            )
            assert any(
                ref.get("event_type") == "unparsed_content_block"
                and ref.get("raw") == {"text": 0}
                for ref in refs
            )
            assert any(
                ref.get("event_type") == "malformed_reasoning"
                and ref.get("raw") == 0
                for ref in refs
            )
            assert any(
                ref.get("event_type") == "normal_message_provenance"
                and ref.get("raw", {}).get("messageId") == "message-shape-1"
                for ref in refs
            )
            assert any(
                ref.get("decode_error") == "non_object_json"
                and ref.get("raw") == '["non-object-json"]'
                for ref in refs
            )
            assert any(
                ref.get("decode_error") == "invalid_json"
                and ref.get("raw") == "{invalid-json"
                for ref in refs
            )
            assert any(
                ref.get("decode_error") == "invalid_utf8"
                and ref.get("raw_base64") == "e/9icm9rZW4tanNvbg=="
                for ref in refs
            )
        finally:
            engine.close()

    def test_parse_native_trajectory(self, source, tmp_path):
        path = tmp_path / "sess-1.trajectory.jsonl"
        events = [
            {
                "type": "trace.metadata",
                "sessionId": "sess-1",
                "workspaceDir": "/workspace",
                "provider": "opencode",
                "modelId": "m1",
                "modelApi": "openai-completions",
                "data": {"model": {"name": "m1"}},
            },
            {
                "type": "model.completed",
                "ts": "2026-06-30T02:30:13.186Z",
                "sessionId": "sess-1",
                "session_id": "conflicting-sess-1",
                "data": {
                    "usage": {"input": 1, "output": 2},
                    "messagesSnapshot": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "hello"},
                                {"type": "file", "path": "README.md"},
                            ],
                        },
                        {
                            "role": "assistant",
                            "content": [
                                {"type": "text", "text": "world"},
                                {
                                    "type": "future_block",
                                    "opaque": "trajectory future payload",
                                },
                            ],
                        },
                    ],
                    "finalPromptText": "alternate hello",
                    "assistantTexts": ["alternate world"],
                    "toolResults": [{"id": "camel-result"}],
                    "tool_results": [{"id": "snake-result"}],
                    "future_completed_field": {
                        "opaque": "completed event residual"
                    },
                },
            },
            {
                "type": "trace.artifacts",
                "data": {"finalStatus": "success", "toolMetas": [{"name": "read"}]},
            },
            {
                "type": "model.completed",
                "ts": "2026-06-30T02:30:14.186Z",
                "sessionId": "sess-1",
                "data": {
                    "messagesSnapshot": [],
                    "reasoning": "reasoning-only completion",
                    "toolResults": [{"id": "tool-result-1", "output": "done"}],
                },
            },
        ]
        path.write_text(
            "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
            encoding="utf-8",
        )

        turns = source.parse_turns(path)

        assert len(turns) == 2
        assert turns[0].user_content == "hello"
        assert turns[0].assistant_content == "world"
        assert turns[0].metadata["provider"] == "opencode"
        assert turns[0].metadata["usage"] == {"input": 1, "output": 2}
        assert turns[0].tool_calls == [{"name": "read"}]
        assert turns[0].attachments[0]["path"] == "README.md"
        assert turns[0].completeness["attachments"] == "full"
        assert any(
            ref.get("event_type") == "unparsed_content_block"
            and ref.get("raw", {}).get("opaque") == "trajectory future payload"
            for ref in turns[0].raw_event_refs
        )
        assert any(
            ref.get("event_type") == "trajectory_completed_residual"
            and ref.get("raw", {}).get("future_completed_field")
            == {"opaque": "completed event residual"}
            for ref in turns[0].raw_event_refs
        )
        assert any(
            ref.get("event_type") == "trajectory_workspace_provenance"
            and ref.get("raw") == "/workspace"
            for ref in turns[0].raw_event_refs
        )
        assert any(
            ref.get("event_type") == "conflicting_session_identity_fields"
            for ref in turns[0].raw_event_refs
        )
        assert any(
            ref.get("event_type") == "conflicting_final_prompt"
            and ref.get("raw") == "alternate hello"
            for ref in turns[0].raw_event_refs
        )
        assert any(
            ref.get("event_type") == "conflicting_assistant_texts"
            and ref.get("raw") == ["alternate world"]
            for ref in turns[0].raw_event_refs
        )
        assert any(
            ref.get("event_type") == "conflicting_tool_result_fields"
            for ref in turns[0].raw_event_refs
        )
        assert turns[1].user_content == ""
        assert turns[1].assistant_content == ""
        assert turns[1].reasoning == "reasoning-only completion"
        assert turns[1].tool_results == [{"id": "tool-result-1", "output": "done"}]

        future_only = tmp_path / "future-only.trajectory.jsonl"
        future_only.write_text(
            json.dumps(
                {
                    "type": "future.event",
                    "sessionId": "future-native",
                    "eventId": "future-event-1",
                    "opaque": {"must": "survive without completion"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        future_session = SessionInfo(
            session_id="future-native",
            source_path=future_only,
            source_kind="trajectory",
            metadata={"native_session_id": "future-native"},
        )
        future_turns = source.parse_session(future_session)
        assert len(future_turns) == 1
        assert future_turns[0].user_content == ""
        assert future_turns[0].assistant_content == ""
        assert any(
            ref.get("event_type") == "future.event"
            and ref.get("raw", {}).get("opaque")
            == {"must": "survive without completion"}
            for ref in future_turns[0].raw_event_refs
        )

        empty_completed = tmp_path / "empty-completed.trajectory.jsonl"
        empty_completed.write_text(
            json.dumps(
                {
                    "type": "model.completed",
                    "sessionId": "empty-completed",
                    "data": {},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        empty_turns = source.parse_turns(empty_completed)
        assert len(empty_turns) == 1
        assert empty_turns[0].raw_event_refs == [
            {
                "event_type": "empty_trajectory_completion",
                "raw": {
                    "type": "model.completed",
                    "data": {},
                },
            }
        ]
        explicit_empty = tmp_path / "explicit-empty.trajectory.jsonl"
        explicit_empty_data = {
            "messagesSnapshot": [],
            "finalPromptText": "",
            "assistantTexts": [],
            "toolResults": [],
            "reasoning": "",
        }
        explicit_empty.write_text(
            json.dumps(
                {
                    "type": "model.completed",
                    "sessionId": "empty-completed",
                    "data": explicit_empty_data,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        explicit_turns = source.parse_turns(explicit_empty)
        assert explicit_turns[0].raw_event_refs == [
            {
                "event_type": "empty_trajectory_completion",
                "raw": {
                    "type": "model.completed",
                    "data": explicit_empty_data,
                },
            }
        ]
        empty_candidate = source._candidate_from_jsonl(
            empty_completed,
            "trajectory",
        )
        explicit_candidate = source._candidate_from_jsonl(
            explicit_empty,
            "trajectory",
        )
        assert empty_candidate is not None
        assert explicit_candidate is not None
        assert empty_candidate.content_hash != explicit_candidate.content_hash

    def test_get_session_state(self, source, tmp_path):
        corpus = tmp_path / "session-corpus"
        corpus.mkdir()
        path = corpus / "2026-06-24.txt"
        path.write_text("content", encoding="utf-8")
        info = SessionInfo(session_id="2026-06-24", source_path=path)
        state = source.get_session_state(info)
        assert state is not None
        assert state["file_count"] == 1

    def test_build_extra_tags(self, source):
        from core.sync_framework.agent_source import Turn

        turn = Turn(
            turn_number=0,
            user_content="q",
            assistant_content="a",
            metadata={"session_id": "abcdef12-3456"},
        )
        tags = source.build_extra_tags(turn)
        assert "source_fidelity=full" in tags
        assert "openclaw-session=abcdef12" in tags

    def test_multi_format_discovery_reconciles_to_canonical_raw(self, tmp_path: Path):
        """All native formats participate; only proven duplicates collapse."""
        base = tmp_path / ".openclaw"
        sessions_dir = base / "agents" / "main" / "sessions"
        corpus_dir = base / "workspace" / "memory" / ".dreams" / "session-corpus"

        _write_completed_jsonl(
            sessions_dir / "shared.trajectory.jsonl",
            "shared",
            [("shared user message", "shared assistant answer")],
        )
        _write_normal_jsonl(
            sessions_dir / "shared.jsonl",
            "shared",
            [("shared user message", "shared assistant answer")],
        )
        _write_normal_jsonl(
            sessions_dir / "normal-only.jsonl",
            "normal-only",
            [("normal-only user message", "normal-only assistant answer")],
        )
        _write_completed_jsonl(
            sessions_dir / "extended.trajectory.jsonl",
            "extended",
            [("first extended user", "first extended assistant")],
        )
        _write_normal_jsonl(
            sessions_dir / "extended.jsonl",
            "extended",
            [
                ("first extended user", "first extended assistant"),
                ("second extended user", "second extended assistant"),
            ],
        )
        _write_completed_jsonl(
            sessions_dir / "divergent.trajectory.jsonl",
            "divergent",
            [("trajectory-only user", "trajectory-only assistant")],
        )
        _write_normal_jsonl(
            sessions_dir / "divergent.jsonl",
            "divergent",
            [("normal-only divergence", "normal-only divergence answer")],
        )
        corpus_dir.mkdir(parents=True)
        (corpus_dir / "2026-07-12.txt").write_text(
            "[/agents/main/sessions/corpus-only#L1] User: corpus-only user message\n"
            "[/agents/main/sessions/corpus-only#L2] Assistant: corpus-only assistant answer\n"
            "[/agents/main/sessions/corpus-second#L1] User: corpus-second user message\n"
            "[/agents/main/sessions/corpus-second#L2] Assistant: corpus-second assistant answer\n",
            encoding="utf-8",
        )

        source = OpenClawSource()
        source._override_data_dir = base
        sessions = source.discover_sessions()

        assert {session.metadata["native_session_id"] for session in sessions} == {
            "shared",
            "normal-only",
            "extended",
            "divergent",
            "corpus-only",
            "corpus-second",
        }
        assert len([item for item in sessions if item.metadata["native_session_id"] == "shared"]) == 1
        assert len([item for item in sessions if item.metadata["native_session_id"] == "extended"]) == 1
        divergent = [item for item in sessions if item.metadata["native_session_id"] == "divergent"]
        assert len(divergent) == 2
        assert len({item.session_id for item in divergent}) == 2

        shared = next(item for item in sessions if item.session_id == "shared")
        assert shared.source_kind == "trajectory"
        assert shared.metadata["source_formats"] == ["normal_jsonl", "trajectory"]
        extended = next(item for item in sessions if item.session_id == "extended")
        assert extended.source_kind == "normal_jsonl"
        assert extended.metadata["canonical_selection"] == "content_extension"
        corpus = next(item for item in sessions if item.metadata["native_session_id"] == "corpus-only")
        assert corpus.source_kind == "corpus"

        config = _OpenClawRawConfig(tmp_path / "runtime")
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
            result = engine.sync_batch(source, sessions, incremental=False)
            assert result.failed == []
            expected_raw = {
                (session.session_id, turn.turn_number)
                for session in sessions
                for turn in source.parse_session(session)
            }
            rows = raw_store._pool.get_conn().execute(  # noqa: SLF001
                "SELECT session_id, turn_number, current_revision_id "
                "FROM raw_turns WHERE source_agent='openclaw'"
            ).fetchall()
            assert {(row[0], row[1]) for row in rows} == expected_raw
            observed: dict[str, set[tuple[str, str]]] = {}
            for session_id, _turn_number, revision_id in rows:
                stored = raw_store.get_turn(revision_id)
                expected = next(item for item in sessions if item.session_id == session_id)
                assert stored is not None
                assert stored["metadata"]["native_session_id"] == expected.metadata[
                    "native_session_id"
                ]
                assert stored["metadata"]["source_artifact_ids"] == expected.metadata[
                    "source_artifact_ids"
                ]
                observed.setdefault(
                    expected.metadata["native_session_id"],
                    set(),
                ).add(
                    (
                        stored["user_content"],
                        stored["assistant_content"],
                    )
                )
            assert observed == {
                "shared": {("shared user message", "shared assistant answer")},
                "normal-only": {
                    ("normal-only user message", "normal-only assistant answer")
                },
                "extended": {
                    ("first extended user", "first extended assistant"),
                    ("second extended user", "second extended assistant"),
                },
                "divergent": {
                    ("trajectory-only user", "trajectory-only assistant"),
                    ("normal-only divergence", "normal-only divergence answer"),
                },
                "corpus-only": {
                    ("corpus-only user message", "corpus-only assistant answer")
                },
                "corpus-second": {
                    ("corpus-second user message", "corpus-second assistant answer")
                },
            }
        finally:
            engine.close()

    def test_divergent_clone_identity_is_stable_when_artifact_mtimes_swap(
        self, tmp_path: Path
    ):
        """Same-format clone identity cannot depend on mutable artifact mtimes."""
        base = tmp_path / ".openclaw"
        sessions_dir = base / "agents" / "main" / "sessions"
        clone_a = sessions_dir / "clone-a.jsonl"
        clone_b = sessions_dir / "clone-b.jsonl"
        divergent = sessions_dir / "stable-divergent.trajectory.jsonl"
        _write_normal_jsonl(
            clone_a,
            "stable-divergent",
            [("clone user", "clone answer")],
        )
        _write_normal_jsonl(
            clone_b,
            "stable-divergent",
            [("clone user", "clone answer")],
        )
        for path, label, second in (
            (clone_a, "A", "10"),
            (clone_b, "B", "20"),
        ):
            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            for index, record in enumerate(records):
                record["messageId"] = f"clone-{label}-{index}"
                record["ts"] = f"2026-07-12T00:02:{second}.{index}Z"
            path.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )
        _write_completed_jsonl(
            divergent,
            "stable-divergent",
            [("divergent user", "divergent answer")],
        )
        os.utime(clone_a, (100, 100))
        os.utime(clone_b, (200, 200))

        source = OpenClawSource()
        source._override_data_dir = base
        before = source.discover_sessions()
        os.utime(clone_a, (200, 200))
        os.utime(clone_b, (100, 100))
        after = source.discover_sessions()

        def inventory(sessions: list[SessionInfo]) -> dict[str, dict[str, object]]:
            return {
                session.session_id: {
                    "source_path": session.source_path,
                    "canonical_session_id": session.canonical_session_id,
                    "source_kind": session.source_kind,
                    "metadata": session.metadata,
                }
                for session in sessions
            }

        assert inventory(before) == inventory(after)
        assert len(before) == 2
        assert all("::artifact::" in session.session_id for session in before)
        clone_session = next(
            session
            for session in before
            if session.metadata["source_artifact_count"] == 2
        )
        other_session = next(
            session for session in before if session is not clone_session
        )
        assert clone_session.metadata["identity_reconciliation_required"] is True
        assert (
            clone_session.metadata["identity_activation_state"]
            == "requires_raw_store_reconciliation_check"
        )
        assert set(clone_session.metadata["legacy_canonical_session_ids"]) <= set(
            clone_session.session_aliases
        )
        assert other_session.session_id in clone_session.session_aliases
        clone_turn = source.parse_session(clone_session)[0]
        provenance_ids = {
            ref.get("raw", {}).get("messageId")
            for ref in clone_turn.raw_event_refs
            if ref.get("event_type") == "normal_message_provenance"
        }
        assert provenance_ids == {
            "clone-A-0",
            "clone-A-1",
            "clone-B-0",
            "clone-B-1",
        }
        assert {str(clone_a), str(clone_b)} <= set(clone_turn.source_files)

        config = _OpenClawRawConfig(tmp_path / "runtime-identity-guard")
        raw_store = RawEventStore(
            db_path=config.database_dir / "raw_events.db",
            config=config,
        )
        raw_store.upsert_turn(
            source_agent="openclaw",
            session_id=other_session.session_id,
            turn_number=0,
            user_content="historical",
            assistant_content="identity",
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
            result = engine.sync_batch(source, [clone_session], incremental=False)
            assert len(result.failed) == 1
            assert (
                result.failed[0]["error"]
                == "source_session_identity_reconciliation_required"
            )
            direct_turn = source.parse_session(clone_session)[0]
            direct_result = engine.sync_single_turn(
                source,
                clone_session,
                direct_turn,
                incremental=False,
            )
            assert direct_result.action == "failed"
            assert (
                direct_result.error
                == "source_session_identity_reconciliation_required"
            )
            with pytest.raises(
                RawEventIdentitySchemaMigrationRequired,
                match="source_session_identity_reconciliation_required",
            ):
                raw_store.upsert_turn(
                    source_agent="openclaw",
                    session_id=clone_session.session_id,
                    turn_number=0,
                    user_content="bypass",
                    assistant_content="must fail",
                    metadata={
                        **clone_session.metadata,
                        "session_aliases": clone_session.session_aliases,
                    },
                    completeness={
                        "visible_text": "full",
                        "tool_calls": "unavailable",
                        "tool_results": "unavailable",
                        "reasoning": "unavailable",
                        "attachments": "unavailable",
                        "truncated": False,
                        "loss_reasons": [],
                    },
                    origin="capture_service",
                )
            count = raw_store._pool.get_conn().execute(  # noqa: SLF001
                "SELECT COUNT(*) FROM raw_turns WHERE source_agent='openclaw'"
            ).fetchone()[0]
            assert count == 1
        finally:
            engine.close()

    def test_unknown_payload_prevents_false_cross_artifact_equivalence(
        self, tmp_path: Path
    ):
        """Equal visible text cannot collapse an artifact with unique opaque payload."""
        base = tmp_path / ".openclaw"
        sessions_dir = base / "agents" / "main" / "sessions"
        plain = sessions_dir / "content-residual-plain.jsonl"
        opaque = sessions_dir / "content-residual-opaque.jsonl"
        _write_normal_jsonl(
            plain,
            "content-residual-divergent",
            [("same user", "same answer")],
        )
        opaque.parent.mkdir(parents=True, exist_ok=True)
        opaque.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "sessionId": "content-residual-divergent",
                            "role": "user",
                            "content": "same user",
                        }
                    ),
                    json.dumps(
                        {
                            "sessionId": "content-residual-divergent",
                            "role": "assistant",
                            "content": [
                                {"type": "text", "text": "same answer"},
                                {
                                    "type": "future_block",
                                    "opaque": "must remain distinct",
                                },
                            ],
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        wrapper_plain = sessions_dir / "wrapper-plain.jsonl"
        wrapper_opaque = sessions_dir / "wrapper-opaque.jsonl"
        wrapper_event = {
            "sessionId": "wrapper-residual-divergent",
            "data": {
                "eventId": "nested-envelope-event-1",
                "ts": "2026-07-13T01:00:00Z",
                "messages": [
                    {"role": "user", "content": "wrapper user"},
                    {"role": "assistant", "content": "wrapper answer"},
                ]
            },
        }
        wrapper_plain.write_text(
            json.dumps(wrapper_event) + "\n",
            encoding="utf-8",
        )
        wrapper_opaque.write_text(
            json.dumps(
                {
                    **wrapper_event,
                    "futureEnvelope": {"must": "remain distinct"},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        trajectory_data_plain = sessions_dir / "trajectory-data-plain.trajectory.jsonl"
        trajectory_data_opaque = sessions_dir / "trajectory-data-opaque.trajectory.jsonl"
        _write_completed_jsonl(
            trajectory_data_plain,
            "trajectory-data-divergent",
            [("trajectory user", "trajectory answer")],
        )
        _write_completed_jsonl(
            trajectory_data_opaque,
            "trajectory-data-divergent",
            [("trajectory user", "trajectory answer")],
        )
        data_event = json.loads(trajectory_data_opaque.read_text(encoding="utf-8"))
        data_event["data"]["futureOpaque"] = {"must": "remain distinct"}
        trajectory_data_opaque.write_text(
            json.dumps(data_event) + "\n",
            encoding="utf-8",
        )

        trajectory_outer_plain = sessions_dir / "trajectory-outer-plain.trajectory.jsonl"
        trajectory_outer_opaque = sessions_dir / "trajectory-outer-opaque.trajectory.jsonl"
        _write_completed_jsonl(
            trajectory_outer_plain,
            "trajectory-outer-divergent",
            [("trajectory outer user", "trajectory outer answer")],
        )
        _write_completed_jsonl(
            trajectory_outer_opaque,
            "trajectory-outer-divergent",
            [("trajectory outer user", "trajectory outer answer")],
        )
        outer_event = json.loads(trajectory_outer_opaque.read_text(encoding="utf-8"))
        outer_event["futureOuter"] = {"must": "remain distinct"}
        trajectory_outer_opaque.write_text(
            json.dumps(outer_event) + "\n",
            encoding="utf-8",
        )

        trajectory_usage_plain = sessions_dir / "trajectory-usage-plain.trajectory.jsonl"
        trajectory_usage_opaque = sessions_dir / "trajectory-usage-opaque.trajectory.jsonl"
        _write_completed_jsonl(
            trajectory_usage_plain,
            "trajectory-usage-divergent",
            [("trajectory usage user", "trajectory usage answer")],
        )
        _write_completed_jsonl(
            trajectory_usage_opaque,
            "trajectory-usage-divergent",
            [("trajectory usage user", "trajectory usage answer")],
        )
        usage_event = json.loads(trajectory_usage_opaque.read_text(encoding="utf-8"))
        usage_event["provider"] = 0
        usage_event["data"]["usage"] = 0
        trajectory_usage_opaque.write_text(
            "\n".join(
                [
                    json.dumps(usage_event),
                    json.dumps(
                        {
                            "type": "trace.artifacts",
                            "sessionId": "trajectory-usage-divergent",
                            "data": {
                                "finalStatus": 0,
                                "toolMetas": [],
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        trajectory_meta_a = sessions_dir / "trajectory-meta-a.trajectory.jsonl"
        trajectory_meta_b = sessions_dir / "trajectory-meta-b.trajectory.jsonl"
        _write_completed_jsonl(
            trajectory_meta_a,
            "trajectory-meta-divergent",
            [("trajectory metadata user", "trajectory metadata answer")],
        )
        _write_completed_jsonl(
            trajectory_meta_b,
            "trajectory-meta-divergent",
            [("trajectory metadata user", "trajectory metadata answer")],
        )
        for path, usage in (
            (trajectory_meta_a, {"input": 1}),
            (trajectory_meta_b, {"input": 2}),
        ):
            metadata_event = json.loads(path.read_text(encoding="utf-8"))
            metadata_event["data"]["usage"] = usage
            path.write_text(
                json.dumps(metadata_event) + "\n",
                encoding="utf-8",
            )

        trajectory_workspace_a = (
            sessions_dir / "trajectory-workspace-a.trajectory.jsonl"
        )
        trajectory_workspace_b = (
            sessions_dir / "trajectory-workspace-b.trajectory.jsonl"
        )
        _write_completed_jsonl(
            trajectory_workspace_a,
            "trajectory-workspace-equivalent",
            [("trajectory workspace user", "trajectory workspace answer")],
        )
        _write_completed_jsonl(
            trajectory_workspace_b,
            "trajectory-workspace-equivalent",
            [("trajectory workspace user", "trajectory workspace answer")],
        )
        for path, workspace in (
            (trajectory_workspace_a, "/workspace/a"),
            (trajectory_workspace_b, "/workspace/b"),
        ):
            workspace_event = json.loads(path.read_text(encoding="utf-8"))
            workspace_event["workspaceDir"] = workspace
            path.write_text(
                json.dumps(workspace_event) + "\n",
                encoding="utf-8",
            )

        source = OpenClawSource()
        source._override_data_dir = base
        sessions = source.discover_sessions()

        for native_id in (
            "content-residual-divergent",
            "wrapper-residual-divergent",
            "trajectory-data-divergent",
            "trajectory-outer-divergent",
            "trajectory-usage-divergent",
            "trajectory-meta-divergent",
        ):
            matching = [
                session
                for session in sessions
                if session.metadata["native_session_id"] == native_id
            ]
            assert len(matching) == 2
            assert all(
                session.metadata["identity_reconciliation_required"] is True
                for session in matching
            )
        workspace_sessions = [
            session
            for session in sessions
            if session.metadata["native_session_id"]
            == "trajectory-workspace-equivalent"
        ]
        assert len(workspace_sessions) == 1
        assert len(sessions) == 13
        assert all(
            "::artifact::" in session.session_id
            for session in sessions
            if session.metadata["native_session_id"]
            != "trajectory-workspace-equivalent"
        )
        opaque_session = next(session for session in sessions if session.source_path == opaque)
        turns = source.parse_session(opaque_session)
        assert turns[0].assistant_content == "same answer"
        assert any(
            ref.get("event_type") == "unparsed_content_block"
            and ref.get("raw", {}).get("opaque") == "must remain distinct"
            for ref in turns[0].raw_event_refs
        )

        wrapper_session = next(
            session for session in sessions if session.source_path == wrapper_opaque
        )
        wrapper_turns = source.parse_session(wrapper_session)
        assert any(
            ref.get("event_type") == "normal_event_envelope_residual"
            and ref.get("raw", {}).get("futureEnvelope")
            == {"must": "remain distinct"}
            for ref in wrapper_turns[0].raw_event_refs
        )
        assert any(
            ref.get("event_type") == "normal_data_provenance"
            and ref.get("raw", {}).get("eventId") == "nested-envelope-event-1"
            and ref.get("raw", {}).get("ts") == "2026-07-13T01:00:00Z"
            for ref in wrapper_turns[0].raw_event_refs
        )
        assert wrapper_turns[0].timestamp == "2026-07-13T01:00:00Z"

        trajectory_data_session = next(
            session
            for session in sessions
            if session.source_path == trajectory_data_opaque
        )
        trajectory_data_turns = source.parse_session(trajectory_data_session)
        assert any(
            ref.get("event_type") == "trajectory_completed_residual"
            and ref.get("raw", {}).get("futureOpaque")
            == {"must": "remain distinct"}
            for ref in trajectory_data_turns[0].raw_event_refs
        )

        trajectory_outer_session = next(
            session
            for session in sessions
            if session.source_path == trajectory_outer_opaque
        )
        trajectory_outer_turns = source.parse_session(trajectory_outer_session)
        assert any(
            ref.get("event_type") == "trajectory_event_envelope_residual"
            and ref.get("raw", {}).get("futureOuter")
            == {"must": "remain distinct"}
            for ref in trajectory_outer_turns[0].raw_event_refs
        )

        trajectory_usage_session = next(
            session
            for session in sessions
            if session.source_path == trajectory_usage_opaque
        )
        trajectory_usage_turns = source.parse_session(trajectory_usage_session)
        assert any(
            ref.get("event_type") == "malformed_usage"
            and ref.get("raw") == 0
            for ref in trajectory_usage_turns[0].raw_event_refs
        )
        assert any(
            ref.get("event_type") == "malformed_trajectory_metadata"
            and ref.get("field") == "provider"
            and ref.get("raw") == 0
            for ref in trajectory_usage_turns[0].raw_event_refs
        )
        assert any(
            ref.get("event_type") == "malformed_final_status"
            and ref.get("raw") == 0
            for ref in trajectory_usage_turns[0].raw_event_refs
        )
        metadata_sessions = [
            session
            for session in sessions
            if session.metadata["native_session_id"]
            == "trajectory-meta-divergent"
        ]
        assert {
            source.parse_session(session)[0].metadata["usage"]["input"]
            for session in metadata_sessions
        } == {1, 2}
        workspace_turn = source.parse_session(workspace_sessions[0])[0]
        assert {
            ref.get("raw")
            for ref in workspace_turn.raw_event_refs
            if ref.get("event_type") == "trajectory_workspace_provenance"
        } == {"/workspace/a", "/workspace/b"}
