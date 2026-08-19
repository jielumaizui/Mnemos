# -*- coding: utf-8 -*-
"""Tests for integrations.sources.claude_source."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from core.sync_framework.agent_source import NativeSourceContractError, SessionInfo
from core.sync_framework.raw_event_store import RawEventStore
from core.sync_framework.sync_engine import SyncEngine
from daemon.raw_only_sync_engine import RawOnlySyncEngine
from integrations.sources.claude_source import ClaudeSource


@pytest.fixture
def source(tmp_path):
    src = ClaudeSource()
    with patch.object(type(src), "data_dir", new=tmp_path / "projects"):
        yield src


class _ClaudeRawConfig:
    """Hermetic config for the Claude native-artifact Raw regression test."""

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


class TestClaudeSource:
    def test_name_and_model_tag(self, source):
        assert source.name == "claude"
        assert source.model_tag == "claude-code"

    def test_trigger_strategy(self, source):
        strategy = source.trigger_strategy
        assert strategy["type"] == "watchdog"
        assert "modified" in strategy["events"]

    def test_completeness_capabilities(self, source):
        caps = source.completeness_capabilities()
        assert caps["visible_text"] is True
        assert caps["reasoning"] is True
        assert caps["tool_calls"] is True

    def test_discover_sessions(self, source, tmp_path):
        projects = tmp_path / "projects"
        sess_dir = projects / "proj-a"
        sess_dir.mkdir(parents=True)
        (sess_dir / "chat.jsonl").write_text("{}", encoding="utf-8")

        infos = source.discover_sessions()
        assert len(infos) == 1
        assert infos[0].session_id == "chat"

    def test_discover_sessions_recurses_beyond_legacy_depth_limit(self, source, tmp_path):
        projects = tmp_path / "projects"
        deep = projects / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        (deep / "chat.jsonl").write_text("{}", encoding="utf-8")

        infos = source.discover_sessions()
        assert len(infos) == 1
        assert infos[0].session_id == "chat"
        assert infos[0].source_kind == "project_transcript"

    def test_recursive_discovery_keeps_subagents_distinct_and_raw_canonical(
        self, source, tmp_path
    ):
        """Depth-2/4/6 projects and sibling subagents all reach one Raw turn each."""
        projects = tmp_path / "projects"

        def write_transcript(path: Path, label: str) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            messages = [
                {"message": {"role": "user", "content": f"user-{label}"}}
            ]
            if label == "4":
                messages.extend(
                    [
                        {
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "thinking",
                                        "thinking": "reasoning-subagent-1",
                                    },
                                ],
                            }
                        },
                        {
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "reasoning",
                                        "text": "reasoning-subagent-2",
                                    },
                                    {
                                        "type": "tool_use",
                                        "name": "bash",
                                        "input": {"cmd": "pwd"},
                                        "id": "tool-subagent",
                                    },
                                    {
                                        "type": "tool_result",
                                        "content": "tool-output-subagent",
                                        "tool_use_id": "tool-subagent",
                                    },
                                    {"type": "text", "text": "assistant-4"},
                                ],
                            }
                        },
                    ]
                )
            else:
                messages.append(
                    {
                        "message": {
                            "role": "assistant",
                            "content": f"assistant-{label}",
                        }
                    }
                )
            path.write_text(
                "\n".join(json.dumps(message) for message in messages) + "\n",
                encoding="utf-8",
            )

        paths = [
            projects / "project-two" / "shared.jsonl",
            projects / "project-four" / "nested" / "one" / "shared.jsonl",
            projects / "project-six" / "a" / "b" / "c" / "d" / "depth-6.jsonl",
            projects / "project-subagents" / "parent-session.jsonl",
            projects
            / "project-subagents"
            / "parent-session"
            / "subagents"
            / "worker-one.jsonl",
            projects
            / "project-subagents"
            / "parent-session"
            / "subagents"
            / "nested"
            / "worker-two.jsonl",
        ]
        for index, path in enumerate(paths):
            write_transcript(path, str(index))

        infos = source.discover_sessions()
        assert {info.source_path for info in infos} == set(paths)
        assert len({info.session_id for info in infos}) == len(paths)
        assert all(info.canonical_session_id == info.session_id for info in infos)

        subagents = [info for info in infos if info.source_kind == "subagent"]
        assert len(subagents) == 2
        assert {info.metadata["parent_session_id"] for info in subagents} == {"parent-session"}
        assert len({info.metadata["source_artifact_id"] for info in infos}) == len(paths)
        assert all(info.source_kind in {"project_transcript", "subagent"} for info in infos)

        config = _ClaudeRawConfig(tmp_path / "runtime")
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
            assert result.turn_stats["new"] == len(paths)

            rows = raw_store._pool.get_conn().execute(  # noqa: SLF001
                """
                SELECT session_id, turn_number, current_revision_id
                FROM raw_turns WHERE source_agent='claude'
                """
            ).fetchall()
            assert len(rows) == len(paths)
            assert {(row[0], row[1]) for row in rows} == {
                (info.session_id, 0) for info in infos
            }
            for session_id, _turn_number, revision_id in rows:
                stored = raw_store.get_turn(revision_id)
                expected = next(info for info in infos if info.session_id == session_id)
                assert stored is not None
                assert stored["metadata"]["source_kind"] == expected.source_kind
                assert stored["metadata"]["source_artifact_id"] == expected.metadata[
                    "source_artifact_id"
                ]
                if expected.source_kind == "subagent":
                    assert stored["metadata"]["parent_session_id"] == "parent-session"
                if expected.source_path == paths[4]:
                    assert stored["assistant_content"] == "assistant-4"
                    assert stored["reasoning"] == (
                        "reasoning-subagent-1\nreasoning-subagent-2"
                    )
                    assert stored["tool_calls"][0]["name"] == "bash"
                    assert stored["tool_results"][0]["stdout"] == "tool-output-subagent"
        finally:
            engine.close()

    def test_discover_sessions_missing_dir(self, source):
        assert source.discover_sessions() == []

    def test_parse_turns_text_and_reasoning(self, source, tmp_path):
        path = tmp_path / "chat.jsonl"
        path.write_text(
            json.dumps({"message": {"role": "user", "content": "hello"}}) + "\n"
            + json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "thinking..."},
                            {"type": "text", "text": "hi"},
                        ],
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )

        turns = source.parse_turns(path)
        assert len(turns) == 1
        assert turns[0].user_content == "hello"
        assert turns[0].assistant_content == "hi"
        assert turns[0].reasoning == "thinking..."
        assert turns[0].metadata.get("reasoning") == "thinking..."

    def test_parse_turns_tool_use_and_result(self, source, tmp_path):
        path = tmp_path / "chat.jsonl"
        path.write_text(
            json.dumps({"message": {"role": "user", "content": "run"}}) + "\n"
            + json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "name": "bash", "input": {"cmd": "ls"}, "id": "tu1"},
                            {"type": "tool_result", "content": "stdout text", "tool_use_id": "tu1"},
                            {"type": "text", "text": "done"},
                        ],
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )

        turns = source.parse_turns(path)
        assert len(turns) == 1
        assert turns[0].tool_calls
        assert turns[0].tool_calls[0]["name"] == "bash"
        assert turns[0].tool_results

    def test_parse_turns_invalid_jsonl(self, source, tmp_path):
        path = tmp_path / "chat.jsonl"
        path.write_text("not json\n", encoding="utf-8")
        with pytest.raises(
            NativeSourceContractError,
            match="native_claude_jsonl_decode_failed",
        ):
            source.parse_turns(path)

    def test_standardize_message_returns_none_for_empty(self, source):
        assert source._standardize_message({}) is None
        assert source._standardize_message([]) is None

    def test_build_extra_tags(self, source):
        from core.sync_framework.agent_source import Turn

        turn = Turn(
            turn_number=0,
            user_content="q",
            assistant_content="a",
            metadata={"tool_calls": [{"name": "x"}], "reasoning": "r"},
        )
        tags = source.build_extra_tags(turn)
        assert "has-tools=true" in tags
        assert "has-reasoning=true" in tags


class TestClaudeDataDir:
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
        custom = tmp_path / "custom-claude"
        projects = custom / "projects"
        projects.mkdir(parents=True)
        mnemos_dir = tmp_path / "mnemos"
        mnemos_dir.mkdir()
        configs = mnemos_dir / "configs"
        configs.mkdir()
        (configs / "main.json").write_text(
            json.dumps({"claude_data_dir": str(custom)}), encoding="utf-8"
        )
        monkeypatch.setenv("MNEMOS_DIR", str(mnemos_dir))
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("MNEMOS_CONFIG", raising=False)
        reload_config()
        try:
            src = ClaudeSource()
            assert src.data_dir == projects
        finally:
            reload_config()

    def test_data_dir_returns_root_when_projects_missing(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        claude_root = home / ".claude"
        claude_root.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("MNEMOS_CONFIG", raising=False)
        src = ClaudeSource()
        assert src.data_dir == claude_root

    def test_data_dir_falls_back_to_home(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        projects = home / ".claude" / "projects"
        projects.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("MNEMOS_CONFIG", raising=False)
        src = ClaudeSource()
        assert src.data_dir == projects

    def test_data_dir_returns_none_when_missing(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("MNEMOS_CONFIG", raising=False)
        src = ClaudeSource()
        assert src.data_dir is None

    def test_discover_sessions_empty_projects_dir(self, source, tmp_path):
        projects = tmp_path / "projects"
        projects.mkdir()
        assert source.discover_sessions() == []

    def test_discover_sessions_data_dir_none(self, source):
        with patch.object(type(source), "data_dir", new=None):
            assert source.discover_sessions() == []


class TestClaudeParseTurnsExtended:
    def test_parse_turns_read_failure(self, source, tmp_path):
        path = tmp_path / "missing" / "chat.jsonl"
        with pytest.raises(
            NativeSourceContractError,
            match="native_claude_transcript_read_failed",
        ):
            source.parse_turns(path)

    def test_parse_turns_blank_lines_and_json_error(self, source, tmp_path):
        path = tmp_path / "chat.jsonl"
        path.write_text(
            "\n\n"
            + "not json\n"
            + json.dumps({"message": {"role": "user", "content": "hello"}}) + "\n"
            + json.dumps({"message": {"role": "assistant", "content": "hi"}}) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(
            NativeSourceContractError,
            match="native_claude_jsonl_decode_failed",
        ):
            source.parse_turns(path)

    def test_parse_turns_tool_results_list(self, source, tmp_path):
        path = tmp_path / "chat.jsonl"
        path.write_text(
            json.dumps({"message": {"role": "user", "content": "run"}}) + "\n"
            + json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "name": "bash", "input": {"cmd": "ls"}, "id": "tu1"}
                        ],
                    }
                }
            )
            + "\n"
            + json.dumps(
                {
                    "message": {"role": "assistant", "content": ""},
                    "tool_results": [
                        {"stdout": "out", "stderr": "err", "tool_use_id": "tu1"}
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        turns = source.parse_turns(path)
        assert len(turns) == 1
        assert turns[0].tool_calls[0]["name"] == "bash"
        assert turns[0].tool_results[0]["stdout"] == "out"
        assert turns[0].tool_results[0]["stderr"] == "err"

    def test_parse_turns_consecutive_users(self, source, tmp_path):
        path = tmp_path / "chat.jsonl"
        path.write_text(
            json.dumps({"message": {"role": "user", "content": "first"}}) + "\n"
            + json.dumps({"message": {"role": "user", "content": "second"}}) + "\n"
            + json.dumps({"message": {"role": "assistant", "content": "ok"}}) + "\n",
            encoding="utf-8",
        )
        turns = source.parse_turns(path)
        assert len(turns) == 2
        assert turns[0].user_content == "first"
        assert turns[0].assistant_content == ""
        assert turns[1].user_content == "second"
        assert turns[1].assistant_content == "ok"

    def test_parse_turns_preserves_consecutive_assistant_payload_and_reasoning(
        self, source, tmp_path
    ):
        path = tmp_path / "chat.jsonl"
        path.write_text(
            json.dumps({"message": {"role": "user", "content": "run"}})
            + "\n"
            + json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "r1"},
                            {"type": "text", "text": "first visible"},
                        ],
                    }
                }
            )
            + "\n"
            + json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "r2"},
                            {"type": "reasoning", "text": "r3"},
                            {
                                "type": "tool_use",
                                "name": "bash",
                                "input": {"cmd": "ls"},
                                "id": "tu1",
                            },
                            {
                                "type": "tool_result",
                                "content": "stdout",
                                "tool_use_id": "tu1",
                            },
                            {"type": "text", "text": "done"},
                        ],
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )

        turns = source.parse_turns(path)

        assert len(turns) == 1
        assert turns[0].assistant_content == "first visible\ndone"
        assert turns[0].reasoning == "r1\nr2\nr3"
        assert turns[0].tool_calls[0]["name"] == "bash"
        assert turns[0].tool_results[0]["stdout"] == "stdout"

    def test_standalone_nontext_assistant_payload_reaches_canonical_raw(
        self, source, tmp_path
    ):
        path = tmp_path / "assistant-only.jsonl"
        path.write_text(
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "standalone-reasoning"},
                        ],
                    }
                }
            )
            + "\n"
            + json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "bash",
                                "input": {"cmd": "pwd"},
                                "id": "standalone-tool",
                            },
                            {"type": "future_native_block", "value": "opaque"},
                        ],
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )
        turns = source.parse_turns(path)
        assert len(turns) == 1

        config = _ClaudeRawConfig(tmp_path / "runtime-standalone")
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
        session = SessionInfo(
            session_id="assistant-only",
            source_path=path,
            canonical_session_id="assistant-only",
            source_kind="subagent",
            metadata={
                "source_artifact_id": "claude-artifact-assistant-only",
                "parent_session_id": "parent-session",
                "parent_relation": "native_subagent_path",
            },
        )
        try:
            result = engine.sync_single_turn(
                source,
                session,
                turns[0],
                incremental=False,
            )
            assert result.action != "failed"
            assert result.raw_event_id
            stored = raw_store.get_turn(result.raw_event_id)
            assert stored is not None
            assert stored["reasoning"] == "standalone-reasoning"
            assert stored["tool_calls"][0]["name"] == "bash"
            assert any(
                ref.get("type") == "future_native_block"
                for ref in stored["raw_event_refs"]
            )
            assert any(
                ref.get("type") == "native_tool_use"
                for ref in stored["raw_event_refs"]
            )
        finally:
            engine.close()

    def test_parse_turns_deduplicates_an_exact_replayed_native_pair(self, source, tmp_path):
        path = tmp_path / "chat.jsonl"
        user = {
            "message": {"role": "user", "content": "same user", "id": "user-1"}
        }
        assistant = {
            "message": {
                "role": "assistant",
                "content": "same assistant",
                "id": "assistant-1",
            }
        }
        path.write_text(
            "\n".join(json.dumps(message) for message in (user, assistant, user, assistant))
            + "\n",
            encoding="utf-8",
        )

        turns = source.parse_turns(path)

        assert len(turns) == 1
        assert turns[0].turn_number == 0
        assert turns[0].metadata["native_event_id"] == "claude:id:user-1"

    def test_parse_turns_preserves_conflicting_reused_native_id_with_fallback(self, source, tmp_path):
        path = tmp_path / "chat.jsonl"
        first_user = {
            "message": {"role": "user", "content": "first user", "id": "user-1"}
        }
        second_user = {
            "message": {"role": "user", "content": "second user", "id": "user-1"}
        }
        first_assistant = {
            "message": {"role": "assistant", "content": "first assistant", "id": "a-1"}
        }
        second_assistant = {
            "message": {"role": "assistant", "content": "second assistant", "id": "a-2"}
        }
        path.write_text(
            "\n".join(
                json.dumps(message)
                for message in (first_user, first_assistant, second_user, second_assistant)
            )
            + "\n",
            encoding="utf-8",
        )

        turns = source.parse_turns(path)

        assert len(turns) == 2

        config = _ClaudeRawConfig(tmp_path / "raw-runtime")
        store = RawEventStore(
            db_path=config.database_dir / "raw_events.db",
            config=config,
        )
        engine = RawOnlySyncEngine(raw_store=store)
        results = engine.sync_turns(
            source,
            SessionInfo(session_id="conflict-session", source_path=path),
            turns,
            incremental=False,
            enqueue_distillation=False,
        )
        assert len(results) == 2
        assert all(result.raw_event_id for result in results)
        conn = store._pool.get_conn()  # noqa: SLF001 - direct contract oracle
        persisted = [
            store.get_turn(str(result.raw_event_id))
            for result in results
        ]
        assert [item["user_content"] for item in persisted if item] == [
            "first user",
            "second user",
        ]
        assert [item["assistant_content"] for item in persisted if item] == [
            "first assistant",
            "second assistant",
        ]
        logical_ids = [
            str(item["logical_event_id"])
            for item in persisted
            if item
        ]
        assert len(logical_ids) == 2
        assert len(set(logical_ids)) == 2
        assert (
            conn.execute(
                "SELECT count(*) FROM raw_turns WHERE event_id IN (?, ?)",
                tuple(logical_ids),
            ).fetchone()[0]
            == 2
        )
        assert turns[0].metadata["native_event_id"] == "claude:id:user-1"
        assert "native_event_id" not in turns[1].metadata
        assert turns[1].metadata["parser_offset"] == "1"
        assert (
            "native_event_id_payload_conflict"
            not in turns[1].completeness["loss_reasons"]
        )
        assert {
            "event_type": "native_event_id_payload_conflict",
            "conflicting_native_event_id": "claude:id:user-1",
            "resolution": "parser_artifact_offset",
        } in turns[1].raw_event_refs
        states = conn.execute(
            """
            SELECT contract_state
            FROM raw_native_contract_observations
            WHERE observed_revision_id IN (?, ?)
            ORDER BY observed_revision_id
            """,
            tuple(result.raw_event_id for result in results),
        ).fetchall()
        assert states == [("conformant",), ("conformant",)]
        engine.close()

    def test_parse_turns_unknown_content_block(self, source, tmp_path):
        path = tmp_path / "chat.jsonl"
        path.write_text(
            json.dumps({"message": {"role": "user", "content": "hello"}}) + "\n"
            + json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "unknown_block", "data": "x"},
                            {"type": "text", "text": "hi"},
                        ],
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )
        turns = source.parse_turns(path)
        assert len(turns) == 1
        assert turns[0].assistant_content == "hi"
        assert any(ref["type"] == "unknown_block" for ref in turns[0].raw_event_refs)


class TestClaudeStandardizeMessage:
    def test_standardize_with_reasoning_key(self, source):
        msg = {
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "reasoning", "text": "because..."},
                    {"type": "text", "text": "answer"},
                ],
            },
            "timestamp": "2024-01-01T00:00:00Z",
        }
        std = source._standardize_message(msg)
        assert std["reasoning"] == "because..."
        assert std["content"] == "answer"
        assert std["timestamp"] == "2024-01-01T00:00:00Z"

    def test_standardize_with_content_key_in_unknown_part(self, source):
        msg = {
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "image", "content": "image-data"},
                ],
            }
        }
        std = source._standardize_message(msg)
        assert std["content"] == "image-data"

    def test_standardize_preserves_non_object_content_parts(self, source):
        msg = {
            "message": {
                "role": "assistant",
                "content": [
                    "visible string",
                    7,
                    {"type": "text", "text": "answer"},
                ],
            }
        }

        std = source._standardize_message(msg)

        assert std["content"] == "visible string\nanswer"
        assert std["raw_event_refs"] == [
            {"type": "non_object_content_block", "raw": 7}
        ]

    def test_standardize_tool_calls_nested_function(self, source):
        msg = {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "function": {"name": "bash", "arguments": {"cmd": "ls"}},
                        "id": "tc1",
                    }
                ],
            }
        }
        std = source._standardize_message(msg)
        assert std["tool_calls"][0]["name"] == "bash"
        assert std["tool_calls"][0]["input"] == {"cmd": "ls"}
        assert std["tool_calls"][0]["id"] == "tc1"

    def test_standardize_skips_non_dict_message(self, source):
        assert source._standardize_message("string") is None
        assert source._standardize_message([]) is None

    def test_standardize_returns_none_without_role_and_content(self, source):
        assert source._standardize_message({"foo": "bar"}) is None

    def test_standardize_empty_role_but_tool_calls(self, source):
        msg = {
            "message": {
                "role": "",
                "content": "",
                "tool_calls": [{"name": "bash"}],
            }
        }
        # role 为空时回退到 type；若没有 type 则返回 None
        assert source._standardize_message(msg) is None
