# -*- coding: utf-8 -*-
"""Tests for integrations.sources.codex_source."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from core.sync_framework.agent_source import NativeSourceContractError, SessionInfo
from integrations.sources.codex_source import CodexSource


@pytest.fixture
def source(tmp_path):
    src = CodexSource()
    with patch.object(type(src), "data_dir", new=tmp_path / "sessions"):
        yield src


class TestCodexSource:
    def test_name_and_model_tag(self, source):
        assert source.name == "codex"
        assert source.model_tag == "codex"

    def test_trigger_strategy(self, source):
        strategy = source.trigger_strategy
        assert strategy["type"] == "watchdog"
        assert strategy["events"] == ["created", "modified", "moved"]

    def test_completeness_capabilities(self, source):
        caps = source.completeness_capabilities()
        assert caps["visible_text"] is True
        assert caps["tool_calls"] is True
        assert caps["tool_results"] is True
        assert caps["reasoning"] == "summary_or_encrypted"
        assert caps["attachments"] == "available"

    def test_discover_sessions(self, source, tmp_path):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        (sessions / "rollout-uuid-1.jsonl").write_text("{}", encoding="utf-8")
        (sessions / "rollout-123e4567-e89b-12d3-a456-426614174000.jsonl").write_text(
            "{}", encoding="utf-8"
        )

        infos = source.discover_sessions()
        assert len(infos) == 2
        ids = [i.session_id for i in infos]
        assert "rollout-uuid-1" in ids
        uuid_entry = [sid for sid in ids if sid != "rollout-uuid-1"][0]
        assert "123e4567" in uuid_entry

    def test_current_append_only_thread_remains_in_runtime_discovery(
        self,
        source,
        tmp_path,
        monkeypatch,
    ):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        active_id = "123e4567-e89b-12d3-a456-426614174000"
        closed_id = "223e4567-e89b-12d3-a456-426614174000"
        (sessions / f"rollout-{active_id}.jsonl").write_text(
            "active",
            encoding="utf-8",
        )
        (sessions / f"rollout-{closed_id}.jsonl").write_text(
            "closed",
            encoding="utf-8",
        )
        monkeypatch.setenv("CODEX_THREAD_ID", active_id)

        infos = source.discover_sessions()
        assert {item.session_id for item in infos} == {active_id, closed_id}
        assert source.current_active_session_id() == active_id

    def test_discover_sessions_missing_dir(self, source):
        assert source.discover_sessions() == []

    def test_parse_turns_response_item(self, source, tmp_path):
        path = tmp_path / "rollout.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hello"}],
                        "future_metadata": {"keep": True},
                    },
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "hi"},
                            {"type": "tool_call", "name": "bash"},
                        ],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        turns = source.parse_turns(path)
        assert len(turns) == 1
        assert turns[0].user_content == "hello"
        assert turns[0].assistant_content == "hi"
        assert turns[0].tool_calls[0]["name"] == "bash"
        assert any(
            ref.get("event_type") == "tool_call"
            for ref in turns[0].raw_event_refs
        )
        assert any(
            ref.get("event_type") == "response_item_residual"
            and ref.get("raw", {}).get("payload", {}).get("future_metadata")
            == {"keep": True}
            for ref in turns[0].raw_event_refs
        )
        encoded_refs = json.dumps(turns[0].raw_event_refs)
        assert "hello" not in encoded_refs
        assert '"hi"' not in encoded_refs

    def test_parse_turns_top_level_tool_and_reasoning_items(self, source, tmp_path):
        path = tmp_path / "rollout.jsonl"
        path.write_text(
            json.dumps(
                {"type": "event_msg", "payload": {"type": "user_message", "message": "q"}}
            )
            + "\n"
            + json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "reasoning",
                        "id": "rs-1",
                        "summary": [{"text": "reasoning summary"}],
                        "encrypted_content": "opaque",
                    },
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "id": "fc-1",
                        "call_id": "call-1",
                        "name": "exec_command",
                        "arguments": "{\"cmd\":\"pwd\"}",
                    },
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "call-1",
                        "output": "stdout",
                    },
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "a"}],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        turns = source.parse_turns(path)

        assert len(turns) == 1
        turn = turns[0]
        assert turn.tool_calls[0]["name"] == "exec_command"
        assert turn.tool_results[0]["output"] == "stdout"
        assert turn.reasoning == "reasoning summary"
        assert turn.completeness["tool_calls"] == "full"
        assert turn.completeness["tool_results"] == "full"
        assert turn.completeness["reasoning"] == "full"

    def test_parse_turns_attachment_blocks(self, source, tmp_path):
        path = tmp_path / "rollout.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "see file"},
                            {
                                "type": "input_file",
                                "filename": "notes.md",
                                "mime_type": "text/markdown",
                            },
                        ],
                    },
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok"}],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        turn = source.parse_turns(path)[0]

        assert turn.attachments[0]["name"] == "notes.md"
        assert turn.completeness["attachments"] == "full"

    def test_parse_turns_event_msg_user(self, source, tmp_path):
        path = tmp_path / "rollout.jsonl"
        path.write_text(
            json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "q"}})
            + "\n"
            + json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "a"}],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        turns = source.parse_turns(path)
        assert len(turns) == 1
        assert turns[0].user_content == "q"
        assert turns[0].assistant_content == "a"

    def test_get_session_state(self, source, tmp_path):
        path = tmp_path / "rollout-test.jsonl"
        path.write_text("data\n", encoding="utf-8")
        info = SessionInfo(session_id="s1", source_path=path)
        state = source.get_session_state(info)
        assert state is not None
        assert state["file_count"] == 1
        assert "fingerprint" in state

    def test_build_extra_tags(self, source):
        from core.sync_framework.agent_source import Turn

        turn = Turn(turn_number=0, user_content="q", assistant_content="a")
        assert source.build_extra_tags(turn) == []


class TestCodexDataDir:
    @pytest.fixture(autouse=True)
    def reset_config_fixture(self):
        from core.config import reset_config

        reset_config()
        yield
        reset_config()

    def test_data_dir_from_config(self, tmp_path, monkeypatch):
        from core.config import reload_config

        home = tmp_path / "home"
        home.mkdir()
        codex_dir = tmp_path / "codex"
        codex_dir.mkdir()
        mnemos_dir = tmp_path / "mnemos"
        mnemos_dir.mkdir()
        configs = mnemos_dir / "configs"
        configs.mkdir()
        (configs / "main.json").write_text(
            json.dumps({"integrations": {"codex": {"codex_home": str(codex_dir)}}}),
            encoding="utf-8",
        )
        monkeypatch.setenv("MNEMOS_DIR", str(mnemos_dir))
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("CODEX_HOME", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        reload_config()
        try:
            src = CodexSource()
            assert src.data_dir == codex_dir
        finally:
            reload_config()

    def test_data_dir_from_env_codex_home(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        codex_dir = tmp_path / "codex-env"
        codex_dir.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("CODEX_HOME", str(codex_dir))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.delenv("MNEMOS_CONFIG", raising=False)
        src = CodexSource()
        assert src.data_dir == codex_dir

    def test_data_dir_from_env_xdg(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        xdg = tmp_path / "xdg"
        xdg.mkdir()
        codex_dir = xdg / "codex"
        codex_dir.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
        monkeypatch.delenv("CODEX_HOME", raising=False)
        monkeypatch.delenv("MNEMOS_CONFIG", raising=False)
        src = CodexSource()
        assert src.data_dir == codex_dir

    def test_data_dir_returns_none_when_missing(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("CODEX_HOME", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.delenv("MNEMOS_CONFIG", raising=False)
        src = CodexSource()
        assert src.data_dir is None


class TestCodexDiscoverSessionsExtended:
    def test_discover_sessions_empty_dir(self, source, tmp_path):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        assert source.discover_sessions() == []

    def test_discover_sessions_returns_uuid(self, source, tmp_path):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        (sessions / "rollout-123e4567-e89b-12d3-a456-426614174000.jsonl").write_text(
            "{}", encoding="utf-8"
        )
        infos = source.discover_sessions()
        assert len(infos) == 1
        assert infos[0].session_id == "123e4567-e89b-12d3-a456-426614174000"

    def test_non_uuid_rollouts_with_same_basename_keep_distinct_canonical_ids(
        self,
        source,
        tmp_path,
    ):
        sessions = tmp_path / "sessions"
        for branch in ("first", "second"):
            directory = sessions / branch
            directory.mkdir(parents=True)
            (directory / "rollout-custom.jsonl").write_text(
                "{}",
                encoding="utf-8",
            )

        infos = source.discover_sessions()

        assert len(infos) == 2
        assert {info.session_id for info in infos} == {"rollout-custom"}
        assert len({info.canonical_session_id for info in infos}) == 2


class TestCodexParseRolloutExtended:
    def test_parse_rollout_read_failure(self, source, tmp_path):
        path = tmp_path / "missing" / "rollout.jsonl"
        with pytest.raises(
            NativeSourceContractError,
            match="native_codex_rollout_read_failed",
        ):
            source._parse_rollout(path)

    def test_parse_rollout_json_decode_error(self, source, tmp_path):
        path = tmp_path / "rollout.jsonl"
        path.write_text("not json\n", encoding="utf-8")
        with pytest.raises(
            NativeSourceContractError,
            match="native_codex_jsonl_decode_failed",
        ):
            source._parse_rollout(path)

    def test_parse_rollout_tool_output_block(self, source, tmp_path):
        path = tmp_path / "rollout.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "done"},
                            {"type": "tool_output", "output": "stdout"},
                        ],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        messages = source._parse_rollout(path)
        assert len(messages) == 1
        assert messages[0]["role"] == "assistant"
        assert messages[0]["tool_results"][0]["output"] == "stdout"
        assert any(ref["event_type"] == "tool_output" for ref in messages[0]["raw_event_refs"])

    def test_parse_rollout_event_msg_non_user_message(self, source, tmp_path):
        path = tmp_path / "rollout.jsonl"
        path.write_text(
            json.dumps({"type": "event_msg", "payload": {"type": "other_event", "message": "x"}})
            + "\n"
            + json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok"}],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        messages = source._parse_rollout(path)
        # non-user event_msg 应作为 raw_event_ref 附加到后续 message
        assert len(messages) == 1
        assert messages[0]["role"] == "assistant"
        assert any(ref["event_type"] == "event_msg" for ref in messages[0]["raw_event_refs"])

    def test_parse_rollout_unknown_event_type(self, source, tmp_path):
        path = tmp_path / "rollout.jsonl"
        path.write_text(
            json.dumps({"type": "unknown_type", "payload": {}}) + "\n"
            + json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok"}],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        messages = source._parse_rollout(path)
        assert len(messages) == 1
        assert any(ref["event_type"] == "unknown_type" for ref in messages[0]["raw_event_refs"])


class TestCodexPairMessagesExtended:
    def test_pair_messages_with_raw_event_refs(self, source, tmp_path):
        messages = [
            {"role": "user", "content": "q", "raw_event_refs": [{"event_type": "prefill"}]},
            {
                "role": "assistant",
                "content": "a",
                "raw_event_refs": [{"event_type": "tool_call"}],
            },
        ]
        turns = source._pair_messages_to_turns(messages, tmp_path / "rollout.jsonl")
        assert len(turns) == 1
        assert any(ref["event_type"] == "prefill" for ref in turns[0].raw_event_refs)
        assert any(ref["event_type"] == "tool_call" for ref in turns[0].raw_event_refs)

    def test_pair_messages_trailing_user(self, source, tmp_path):
        messages = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
            {"role": "user", "content": "follow up"},
        ]
        turns = source._pair_messages_to_turns(messages, tmp_path / "rollout.jsonl")
        assert len(turns) == 2
        assert turns[1].user_content == "follow up"
        assert turns[1].assistant_content == ""


class TestCodexGetSessionStateExtended:
    def test_get_session_state_empty_dir(self, source, tmp_path):
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        info = SessionInfo(session_id="s1", source_path=empty_dir / "rollout.jsonl")
        with pytest.raises(
            NativeSourceContractError,
            match="native_session_state_read_failed",
        ):
            source.get_session_state(info)

    def test_get_session_state_does_not_include_sibling_rollout(
        self,
        source,
        tmp_path,
    ):
        directory = tmp_path / "sessions"
        directory.mkdir()
        target = directory / "rollout-target.jsonl"
        sibling = directory / "rollout-sibling.jsonl"
        target.write_text("target", encoding="utf-8")
        sibling.write_text("before", encoding="utf-8")
        info = SessionInfo(session_id="target", source_path=target)
        before = source.get_session_state(info)

        sibling.write_text("after-with-different-size", encoding="utf-8")

        assert source.get_session_state(info) == before
