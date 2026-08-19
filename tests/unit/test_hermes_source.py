# -*- coding: utf-8 -*-
"""Tests for integrations.sources.hermes_source."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from core.sync_framework.agent_source import NativeSourceContractError, Turn
from integrations.sources.hermes_source import HermesSource


@pytest.fixture
def source(tmp_path):
    src = HermesSource()
    with patch.object(type(src), "data_dir", new=tmp_path):
        yield src


class TestHermesSource:
    def test_name_and_model_tag(self, source):
        assert source.name == "hermes"
        assert source.model_tag == "hermes"

    def test_data_dir_returns_none_when_no_hermes_home(self, source):
        # patched data_dir should be the tmp_path
        assert source.data_dir == source.data_dir

    def test_trigger_strategy(self, source):
        strategy = source.trigger_strategy
        assert strategy["type"] == "watchdog"
        assert "modified" in strategy["events"]
        assert strategy["recursive"] is True
        caps = source.completeness_capabilities()
        assert caps["tool_calls"] is True
        assert caps["tool_results"] is True

    def test_discover_sessions_jsonl(self, source, tmp_path):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        (sessions / "sess-a.jsonl").write_text("{}", encoding="utf-8")
        (sessions / "sess-b.jsonl").write_text("{}", encoding="utf-8")
        (sessions / "sessions.json").write_text("{}", encoding="utf-8")

        infos = source.discover_sessions()
        assert len(infos) == 2
        assert {i.session_id for i in infos} == {"sess-a", "sess-b"}

    def test_discover_sessions_json(self, source, tmp_path):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        (sessions / "sess.json").write_text("{}", encoding="utf-8")

        infos = source.discover_sessions()
        assert len(infos) == 1
        assert infos[0].session_id == "sess"

    def test_discover_sessions_missing_dir(self, source):
        # tmp_path/sessions does not exist
        assert source.discover_sessions() == []

    def test_discover_sessions_preserves_same_stem_distinct_formats(self, source, tmp_path):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        (sessions / "dup.jsonl").write_text("{}", encoding="utf-8")
        (sessions / "dup.json").write_text("{}", encoding="utf-8")

        infos = source.discover_sessions()
        assert len(infos) == 2
        assert {info.source_path.suffix for info in infos} == {".json", ".jsonl"}
        assert len({info.canonical_session_id for info in infos}) == 2

    def test_parse_turns_jsonl(self, source, tmp_path):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        path = sessions / "s1.jsonl"
        path.write_text(
            json.dumps({"role": "system", "content": "sys"}) + "\n"
            + json.dumps(
                {"role": "user", "content": "hello", "timestamp": "2026-06-24T12:00:00Z"}
            )
            + "\n"
            + json.dumps(
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "reasoning text"},
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "Shell",
                            "input": {"command": "pwd"},
                        },
                        {"type": "text", "text": "hi there"},
                    ],
                }
            )
            + "\n"
            + json.dumps(
                {
                    "role": "tool",
                    "tool_call_id": "tool-1",
                    "name": "Shell",
                    "content": "stdout",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        turns = source.parse_turns(path)
        assert len(turns) == 1
        assert turns[0].user_content == "hello"
        assert turns[0].assistant_content == "hi there"
        assert turns[0].timestamp == "2026-06-24T12:00:00Z"
        assert turns[0].reasoning == "reasoning text"
        assert turns[0].metadata.get("reasoning") == "reasoning text"
        assert turns[0].tool_calls[0]["name"] == "Shell"
        assert turns[0].tool_results[0]["output"] == "stdout"
        assert turns[0].completeness["tool_calls"] == "full"
        assert turns[0].completeness["tool_results"] == "full"

    def test_parse_turns_jsonl_empty_lines_and_consecutive_users(self, source, tmp_path):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        path = sessions / "multi.jsonl"
        path.write_text(
            "\n"
            + json.dumps({"role": "user", "content": "first"}) + "\n\n"
            + json.dumps({"role": "user", "content": "second"}) + "\n"
            + json.dumps({"role": "assistant", "content": "reply"}) + "\n",
            encoding="utf-8",
        )

        turns = source.parse_turns(path)
        assert len(turns) == 2
        assert turns[0].user_content == "first"
        assert turns[0].assistant_content == ""
        assert turns[1].user_content == "second"
        assert turns[1].assistant_content == "reply"

    def test_parse_turns_jsonl_assistant_string_content(self, source, tmp_path):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        path = sessions / "str.jsonl"
        path.write_text(
            json.dumps({"role": "user", "content": "q"}) + "\n"
            + json.dumps({"role": "assistant", "content": "a"}) + "\n",
            encoding="utf-8",
        )

        turns = source.parse_turns(path)
        assert len(turns) == 1
        assert turns[0].assistant_content == "a"

    def test_parse_turns_jsonl_unknown_block(self, source, tmp_path):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        path = sessions / "unknown.jsonl"
        path.write_text(
            json.dumps({"role": "user", "content": "q"}) + "\n"
            + json.dumps(
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "a"},
                        {"type": "weird", "data": "x"},
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        turns = source.parse_turns(path)
        assert len(turns) == 1
        assert turns[0].assistant_content == "a"
        assert any("weird" in loss for loss in turns[0].completeness.get("loss_reasons", []))

    def test_parse_turns_json(self, source, tmp_path):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        path = sessions / "s2.json"
        path.write_text(
            json.dumps(
                {
                    "session_id": "s2",
                    "model": "hermes-3",
                    "messages": [
                        {"role": "user", "content": "question", "timestamp": "2026-06-24T12:00:00Z"},
                        {
                            "role": "assistant",
                            "content": "answer",
                            "reasoning": "because",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": "{\"path\":\"README.md\"}",
                                    },
                                }
                            ],
                        },
                        {
                            "role": "tool",
                            "tool_call_id": "call-1",
                            "name": "read_file",
                            "content": [{"type": "text", "text": "contents"}],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        turns = source.parse_turns(path)
        assert len(turns) == 1
        assert turns[0].user_content == "question"
        assert turns[0].assistant_content == "answer"
        assert turns[0].timestamp == "2026-06-24T12:00:00Z"
        assert turns[0].reasoning == "because"
        assert turns[0].metadata.get("session_id") == "s2"
        assert turns[0].metadata.get("model") == "hermes-3"
        assert turns[0].tool_calls[0]["name"] == "read_file"
        assert turns[0].tool_results[0]["output"] == "contents"

    def test_parse_turns_json_preserves_top_level_container_residual_once(
        self, source, tmp_path
    ):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        path = sessions / "container.json"
        sentinel = {"must": ["survive", 9]}
        path.write_text(
            json.dumps(
                {
                    "session_id": "container",
                    "model": "hermes",
                    "messages": [
                        {"role": "user", "content": "q"},
                        {"role": "assistant", "content": "a"},
                    ],
                    "system_prompt": sentinel,
                    "tools": [{"extra": sentinel}],
                    "platform": "cli",
                }
            ),
            encoding="utf-8",
        )

        turns = source.parse_turns(path)
        serialized = json.dumps(
            [ref for turn in turns for ref in turn.raw_event_refs],
            ensure_ascii=False,
            sort_keys=True,
        )

        assert serialized.count('"survive"') == 2
        assert serialized.count("native_session_container_residual") == 1
        assert '"system_prompt"' in serialized
        assert '"tools"' in serialized

    def test_parse_turns_json_preserves_container_without_messages(
        self, source, tmp_path
    ):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        path = sessions / "container-only.json"
        path.write_text(
            json.dumps({"session_id": "container-only", "messages": [], "platform": "cli"}),
            encoding="utf-8",
        )

        turns = source.parse_turns(path)

        assert len(turns) == 1
        assert turns[0].raw_event_refs[0]["raw"] == {"platform": "cli"}

    def test_parse_turns_json_system_tool_unknown(self, source, tmp_path):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        path = sessions / "mixed.json"
        path.write_text(
            json.dumps(
                {
                    "session_id": "mixed",
                    "messages": [
                        {"role": "system", "content": "sys"},
                        {"role": "user", "content": "hello"},
                        {"role": "assistant", "content": "hi"},
                        {"role": "tool", "content": "result"},
                        {"role": "unknown_role", "content": "?"},
                    ],
                }
            ),
            encoding="utf-8",
        )

        turns = source.parse_turns(path)
        assert len(turns) == 1
        assert turns[0].user_content == "hello"
        assert turns[0].assistant_content == "hi"
        loss_reasons = turns[0].completeness.get("loss_reasons", [])
        assert any("unknown_role" in r for r in loss_reasons)
        raw_refs = turns[0].raw_event_refs
        assert turns[0].tool_results[0]["output"] == "result"
        assert any(r.get("event_type") == "tool_result" for r in raw_refs)
        assert any(r.get("event_type") == "unknown" for r in raw_refs)

    def test_parse_turns_json_invalid_messages(self, source, tmp_path):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        path = sessions / "bad.json"
        path.write_text(json.dumps({"messages": "not a list"}), encoding="utf-8")

        with pytest.raises(
            NativeSourceContractError,
            match="native_hermes_messages_invalid",
        ):
            source.parse_turns(path)

    def test_parse_turns_json_message_not_dict(self, source, tmp_path):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        path = sessions / "not_dict.json"
        path.write_text(
            json.dumps({"messages": ["not a dict", {"role": "user", "content": "q"}]}),
            encoding="utf-8",
        )

        turns = source.parse_turns(path)
        assert len(turns) == 1
        assert turns[0].user_content == "q"

    def test_parse_turns_json_assistant_list_string_part(self, source, tmp_path):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        path = sessions / "list_str.json"
        path.write_text(
            json.dumps(
                {
                    "messages": [
                        {"role": "user", "content": "q"},
                        {"role": "assistant", "content": ["part1", "part2"]},
                    ]
                }
            ),
            encoding="utf-8",
        )

        turns = source.parse_turns(path)
        assert len(turns) == 1
        assert turns[0].assistant_content == "part1\n\npart2"

    def test_parse_turns_json_read_failure(self, source, tmp_path):
        path = tmp_path / "missing.json"
        with pytest.raises(
            NativeSourceContractError,
            match="native_hermes_json_read_failed",
        ):
            source.parse_turns(path)

    def test_parse_turns_jsonl_read_failure(self, source, tmp_path):
        path = tmp_path / "missing.jsonl"
        with pytest.raises(
            NativeSourceContractError,
            match="native_hermes_jsonl_read_failed",
        ):
            source.parse_turns(path)

    def test_parse_turns_jsonl_consecutive_users_saves_previous_turn(self, source, tmp_path):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        path = sessions / "multi.jsonl"
        path.write_text(
            json.dumps({"role": "user", "content": "u1"}) + "\n"
            + json.dumps({"role": "assistant", "content": "a1"}) + "\n"
            + json.dumps({"role": "user", "content": "u2"}) + "\n"
            + json.dumps({"role": "assistant", "content": "a2"}) + "\n",
            encoding="utf-8",
        )

        turns = source.parse_turns(path)
        assert len(turns) == 2
        assert turns[0].user_content == "u1"
        assert turns[0].assistant_content == "a1"
        assert turns[1].user_content == "u2"
        assert turns[1].assistant_content == "a2"

    def test_parse_turns_json_consecutive_users_saves_previous_turn(self, source, tmp_path):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        path = sessions / "multi.json"
        path.write_text(
            json.dumps(
                {
                    "messages": [
                        {"role": "user", "content": "u1"},
                        {"role": "assistant", "content": "a1"},
                        {"role": "user", "content": "u2"},
                        {"role": "assistant", "content": "a2"},
                    ]
                }
            ),
            encoding="utf-8",
        )

        turns = source.parse_turns(path)
        assert len(turns) == 2
        assert turns[0].user_content == "u1"
        assert turns[0].assistant_content == "a1"
        assert turns[1].user_content == "u2"
        assert turns[1].assistant_content == "a2"

    def test_parse_turns_jsonl_read_error_on_directory(self, source, tmp_path):
        # Passing a directory to open() raises IsADirectoryError (OSError subclass)
        with pytest.raises(
            NativeSourceContractError,
            match="native_hermes_jsonl_read_failed",
        ):
            source.parse_turns(tmp_path)

    def test_parse_turns_json_read_error_on_directory(self, source, tmp_path):
        with pytest.raises(
            NativeSourceContractError,
            match="native_hermes_jsonl_read_failed",
        ):
            source.parse_turns(tmp_path)

    def test_parse_turns_json_invalid(self, source, tmp_path):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        path = sessions / "bad.jsonl"
        path.write_text("not json\n", encoding="utf-8")

        with pytest.raises(
            NativeSourceContractError,
            match="native_hermes_jsonl_decode_failed",
        ):
            source.parse_turns(path)

    def test_completeness_capabilities(self, source):
        caps = source.completeness_capabilities()
        assert caps["visible_text"] is True
        assert caps["reasoning"] is True

    def test_build_extra_tags_with_reasoning(self, source):
        turn = Turn(
            turn_number=0,
            user_content="q",
            assistant_content="a",
            metadata={"reasoning": "r"},
        )
        assert source.build_extra_tags(turn) == ["has-reasoning=true"]

    def test_build_extra_tags_without_reasoning(self, source):
        turn = Turn(
            turn_number=0,
            user_content="q",
            assistant_content="a",
            metadata={},
        )
        assert source.build_extra_tags(turn) == []


class TestHermesDataDir:
    """Tests for HermesSource.data_dir that must not use the patched fixture."""

    def test_data_dir_uses_hermes_home_env(self, tmp_path, monkeypatch):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        # Ensure HOME does not accidentally provide a fallback
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "no_home")

        src = HermesSource()
        assert src.data_dir == sessions

    def test_data_dir_falls_back_to_home_dot_hermes(self, tmp_path, monkeypatch):
        hermes_dir = tmp_path / ".hermes"
        sessions = hermes_dir / "sessions"
        sessions.mkdir(parents=True)
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        src = HermesSource()
        assert src.data_dir == sessions

    def test_data_dir_falls_back_to_parent_when_sessions_missing(self, tmp_path, monkeypatch):
        hermes_dir = tmp_path / ".hermes"
        hermes_dir.mkdir()
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        src = HermesSource()
        assert src.data_dir == hermes_dir

    def test_data_dir_returns_none_when_no_hermes_dir(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        src = HermesSource()
        assert src.data_dir is None

    def test_discover_sessions_returns_empty_when_data_dir_none(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        src = HermesSource()
        assert src.discover_sessions() == []
