# -*- coding: utf-8 -*-
"""Tests for integrations.sources.opencode_source."""

from __future__ import annotations

import base64
import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from core.sync_framework.agent_source import NativeSourceContractError, SessionInfo
from integrations.sources import opencode_source
from integrations.sources.opencode_source import OpenCodeSource


# ---------- existing JSON-focused tests ----------

def test_opencode_source_name():
    src = OpenCodeSource()
    assert src.name == "opencode"
    assert src.model_tag == "opencode"


def test_discover_sessions_finds_json_files():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".opencode"
        sessions_dir = base / "sessions"
        sessions_dir.mkdir(parents=True)

        # 写入两个标准 JSON 消息文件
        (sessions_dir / "sess-a.json").write_text(
            json.dumps([{"role": "user", "content": "hello"}]), encoding="utf-8"
        )
        (sessions_dir / "sess-b.json").write_text(
            json.dumps({"messages": [{"role": "assistant", "content": "hi"}]}), encoding="utf-8"
        )

        src = OpenCodeSource()
        with patch.object(type(src), "data_dir", new=base):
            found = src.discover_sessions()
        assert len(found) == 2
        ids = {s.session_id for s in found}
        assert ids == {"sess-a", "sess-b"}


def test_discover_sessions_returns_empty_when_no_data_dir():
    src = OpenCodeSource()
    # 强制 data_dir 为 None，避免受用户机器真实环境影响
    with patch.object(type(src), "data_dir", new=None):
        found = src.discover_sessions()
    assert found == []


def test_parse_turns_standard_array_format():
    src = OpenCodeSource()
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "test.json"
        path.write_text(
            json.dumps(
                [
                    {"role": "user", "content": "question"},
                    {"role": "assistant", "content": "answer"},
                ]
            ),
            encoding="utf-8",
        )

        turns = src.parse_turns(path)
        assert len(turns) == 1
        assert turns[0].user_content == "question"
        assert turns[0].assistant_content == "answer"


def test_parse_turns_object_wrapper_format():
    src = OpenCodeSource()
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "wrapped.json"
        path.write_text(
            json.dumps(
                {
                    "messages": [
                        {"sender": "user", "content": "hi"},
                        {"sender": "assistant", "content": "hello"},
                    ]
                }
            ),
            encoding="utf-8",
        )

        turns = src.parse_turns(path)
        assert len(turns) == 1
        assert turns[0].user_content == "hi"
        assert turns[0].assistant_content == "hello"


def test_parse_turns_preserves_raw_event_refs():
    src = OpenCodeSource()
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "mixed.json"
        path.write_text(
            json.dumps(
                [
                    {"role": "system", "content": "sys prompt"},
                    {"role": "user", "content": "hi"},
                    {"role": "tool", "content": "result"},
                ]
            ),
            encoding="utf-8",
        )

        turns = src.parse_turns(path)
        # user turn + raw refs
        assert len(turns) == 1
        assert turns[0].user_content == "hi"
        raw_refs = turns[0].raw_event_refs
        assert {
            ref.get("raw", {}).get("role")
            for ref in raw_refs
            if isinstance(ref.get("raw"), dict)
        } == {"system", "user", "tool"}


# ---------- SQLite / expanded tests ----------

def _make_db(db_path: Path, session_id: str = "sess-1"):
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            title TEXT,
            directory TEXT,
            time_created INTEGER,
            time_updated INTEGER
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            data TEXT
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY,
            message_id TEXT,
            session_id TEXT,
            time_created INTEGER,
            data TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO session VALUES (?, 't', '/tmp', 1000, 2000)",
        (session_id,),
    )
    conn.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, 3000, 3000, ?)",
        ("m1", session_id, json.dumps({"role": "user"})),
    )
    conn.execute(
        "INSERT INTO part VALUES (?, ?, ?, 4000, ?)",
        ("p1", "m1", session_id, json.dumps({"type": "text", "text": "hello"})),
    )
    conn.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, 5000, 5000, ?)",
        ("m2", session_id, json.dumps({"role": "assistant"})),
    )
    conn.execute(
        "INSERT INTO part VALUES (?, ?, ?, 6000, ?)",
        ("p2", "m2", session_id, json.dumps({"type": "text", "text": "hi"})),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def source(tmp_path):
    src = OpenCodeSource()
    with patch.object(type(src), "data_dir", new=tmp_path):
        yield src


class TestOpenCodeSourceExpanded:
    def test_trigger_strategy(self, source):
        strategy = source.trigger_strategy
        assert strategy["type"] == "hybrid"
        assert "opencode.db" in strategy["pattern"]

    def test_completeness_capabilities(self, source):
        caps = source.completeness_capabilities()
        assert caps["visible_text"] is True
        assert caps["reasoning"] is True

    def test_discover_sessions_sqlite(self, source, tmp_path):
        _make_db(tmp_path / "opencode.db")
        infos = source.discover_sessions()
        assert len(infos) == 1
        assert infos[0].session_id == "sess-1"

    def test_discover_sessions_fallback_json(self, source, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        (sessions_dir / "chat.json").write_text("[]", encoding="utf-8")
        infos = source.discover_sessions()
        assert len(infos) == 1
        assert infos[0].session_id == "chat"

    def test_discover_sessions_no_db_no_dir(self, source):
        assert source.discover_sessions() == []

    def test_parse_session_from_sqlite_uses_discovered_identity(self, source, tmp_path):
        db_path = tmp_path / "opencode.db"
        _make_db(db_path)

        info = source.discover_sessions()[0]
        turns = source.parse_session(info)
        assert len(turns) == 1
        assert turns[0].user_content == "hello"
        assert turns[0].assistant_content == "hi"

    def test_parse_session_sqlite_does_not_depend_on_discovery_order(self, source, tmp_path):
        db_path = tmp_path / "opencode.db"
        _make_db(db_path)
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO session VALUES (?, 't2', '/tmp', 7000, 8000)", ("sess-2",))
        conn.execute(
            "INSERT INTO message VALUES (?, ?, 9000, 9000, ?)",
            ("m3", "sess-2", json.dumps({"role": "user"})),
        )
        conn.execute(
            "INSERT INTO part VALUES (?, ?, ?, 9001, ?)",
            ("p3", "m3", "sess-2", json.dumps({"type": "text", "text": "second user"})),
        )
        conn.execute(
            "INSERT INTO message VALUES (?, ?, 9002, 9002, ?)",
            ("m4", "sess-2", json.dumps({"role": "assistant"})),
        )
        conn.execute(
            "INSERT INTO part VALUES (?, ?, ?, 9003, ?)",
            ("p4", "m4", "sess-2", json.dumps({"type": "text", "text": "second assistant"})),
        )
        conn.commit()
        conn.close()

        infos = {item.session_id: item for item in source.discover_sessions()}
        second = source.parse_session(infos["sess-2"])
        first = source.parse_session(infos["sess-1"])

        assert (second[0].user_content, second[0].assistant_content) == (
            "second user",
            "second assistant",
        )
        assert (first[0].user_content, first[0].assistant_content) == ("hello", "hi")

    def test_parse_turns_sqlite_fails_closed_without_session_identity(self, source, tmp_path):
        db_path = tmp_path / "opencode.db"
        _make_db(db_path)

        with pytest.raises(
            NativeSourceContractError,
            match="native_opencode_session_identity_required",
        ):
            source.parse_turns(db_path)

    def test_parse_turns_invalid_json(self, source, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not json", encoding="utf-8")
        with pytest.raises(
            NativeSourceContractError,
            match="native_opencode_json_read_failed",
        ):
            source.parse_turns(path)

    def test_parse_turns_invalid_utf8_preserves_exact_bytes_as_nonconforming_raw(
        self,
        source,
        tmp_path,
    ):
        path = tmp_path / "invalid-utf8.json"
        payload = b'{"messages":[{"role":"user","content":"before\xffafter"}]}'
        path.write_bytes(payload)

        turns = source.parse_turns(path)

        assert len(turns) == 1
        assert turns[0].user_content == ""
        assert turns[0].assistant_content == ""
        assert turns[0].raw_event_refs == [
            {
                "event_type": "native_json_artifact",
                "raw_base64": base64.b64encode(payload).decode("ascii"),
                "raw_encoding": "base64",
                "decode_error": "invalid_utf8",
            }
        ]
        assert turns[0].completeness == {
            "visible_text": "unavailable",
            "tool_calls": "unavailable",
            "tool_results": "unavailable",
            "reasoning": "unavailable",
            "attachments": "unavailable",
            "truncated": False,
            "loss_reasons": ["native_json_invalid_utf8"],
        }

    def test_get_session_state(self, source, tmp_path):
        db_path = tmp_path / "opencode.db"
        _make_db(db_path)
        info = SessionInfo(session_id="sess-1", source_path=db_path)
        state = source.get_session_state(info)
        assert state is not None
        assert state["size"] == 2
        assert state["fingerprint"].startswith("sha256:")
        assert (
            state["fingerprint_contract"]
            == "opencode-exact-session-rows-sha256-v1"
        )

    def test_build_extra_tags(self, source):
        from core.sync_framework.agent_source import Turn

        turn = Turn(turn_number=0, user_content="q", assistant_content="a")
        assert source.build_extra_tags(turn) == ["source=opencode"]

    def test_ms_to_iso(self):
        assert opencode_source._ms_to_iso(0) is None
        assert opencode_source._ms_to_iso(None) is None
        iso = opencode_source._ms_to_iso(1_000_000)
        assert iso is not None
        assert "1970" in iso

    def test_extract_text_and_reasoning(self):
        parts = [
            {"type": "text", "text": "a"},
            {"type": "reasoning", "text": "b"},
            {"type": "other", "text": "c"},
        ]
        assert opencode_source._extract_text(parts) == "a"
        assert opencode_source._extract_reasoning(parts) == "b"

    def test_extract_tool_calls(self):
        parts = [
            {
                "type": "tool",
                "tool": "bash",
                "callID": "c1",
                "state": {"status": "calling", "input": {"cmd": "ls"}},
            }
        ]
        assert opencode_source._extract_tool_calls(parts)[0]["name"] == "bash"

    def test_extract_tool_results(self):
        parts = [
            {
                "type": "tool",
                "tool": "bash",
                "callID": "c1",
                "state": {"status": "completed", "output": "done"},
            }
        ]
        assert opencode_source._extract_tool_results(parts)[0]["output"] == "done"

    def test_extract_messages_variants(self):
        assert opencode_source._extract_messages([{"role": "user"}]) == [{"role": "user"}]
        assert opencode_source._extract_messages({"messages": [{"role": "user"}]}) == [
            {"role": "user"}
        ]
        assert opencode_source._extract_messages({"role": "user", "content": "hi"}) == [
            {"role": "user", "content": "hi"}
        ]
        assert opencode_source._extract_messages({}) == []


# ---------- further expanded tests for higher coverage ----------

class TestOpenCodeDataDir:
    def test_data_dir_fallback_to_config(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        share = home / ".local" / "share" / "opencode"
        share.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        src = OpenCodeSource()
        assert src.data_dir == share

    def test_data_dir_fallback_to_config_db(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        config = home / ".config" / "opencode"
        config.mkdir(parents=True)
        (config / "opencode.db").write_bytes(b"")
        monkeypatch.setenv("HOME", str(home))
        src = OpenCodeSource()
        assert src.data_dir == config

    def test_data_dir_prefers_explicit_db_path(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        db_dir = tmp_path / "custom-opencode"
        db_dir.mkdir()
        db_path = db_dir / "opencode.db"
        db_path.write_bytes(b"")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("OPENCODE_DB_PATH", str(db_path))

        src = OpenCodeSource()
        assert src.data_dir == db_dir

    def test_data_dir_returns_none_when_missing(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("OPENCODE_DB_PATH", raising=False)
        src = OpenCodeSource()
        assert src.data_dir is None


class TestOpenCodeDiscoverJsonFallback:
    def test_discover_from_json_search_paths(self, source, tmp_path):
        for sub in ("sessions", "history", "chats", "logs", "mnemos_tasks"):
            d = tmp_path / sub
            d.mkdir()
            (d / f"{sub}-chat.json").write_text("[]", encoding="utf-8")
        found = source.discover_sessions()
        assert len(found) == 5

    def test_discover_from_json_root_files(self, source, tmp_path):
        (tmp_path / "mychat.json").write_text("[]", encoding="utf-8")
        # config files should be skipped
        (tmp_path / "settings.json").write_text("{}", encoding="utf-8")
        found = source.discover_sessions()
        assert len(found) == 1
        assert found[0].session_id == "mychat"

    def test_discover_skips_hidden_files(self, source, tmp_path):
        (tmp_path / ".hidden.json").write_text("[]", encoding="utf-8")
        found = source.discover_sessions()
        assert found == []


class TestOpenCodeSQLiteFailures:
    def test_discover_from_sqlite_connect_failure(self, source, tmp_path):
        db_path = tmp_path / "opencode.db"
        db_path.write_text("not a db", encoding="utf-8")
        with patch.object(type(source), "db_path", new=db_path):
            with pytest.raises(
                NativeSourceContractError,
                match="native_opencode_session_discovery_failed",
            ):
                source.discover_sessions()

    def test_discover_from_sqlite_no_session_table(self, source, tmp_path):
        conn = sqlite3.connect(tmp_path / "opencode.db")
        conn.execute("CREATE TABLE message (id TEXT)")
        conn.close()
        with patch.object(type(source), "db_path", new=tmp_path / "opencode.db"):
            with pytest.raises(
                NativeSourceContractError,
                match="native_opencode_session_schema_missing",
            ):
                source.discover_sessions()

    def test_parse_turns_from_sqlite_no_db_path(self, source, tmp_path):
        with patch.object(type(source), "db_path", new=None):
            with pytest.raises(
                NativeSourceContractError,
                match="native_opencode_json_read_failed",
            ):
                source.parse_turns(tmp_path / "opencode.db")

    def test_parse_turns_from_sqlite_missing_tables(self, source, tmp_path):
        conn = sqlite3.connect(tmp_path / "opencode.db")
        conn.execute("CREATE TABLE session (id TEXT)")
        conn.close()
        with pytest.raises(
            NativeSourceContractError,
            match="native_opencode_session_identity_required",
        ):
            source.parse_turns(tmp_path / "opencode.db")


class TestOpenCodeMessagesToTurnsExtended:
    def test_messages_to_turns_preserves_system_and_tool(self, source):
        messages = [
            {"id": "m0", "role": "system", "parts": [{"type": "text", "text": "sys"}]},
            {"id": "m1", "role": "user", "parts": [{"type": "text", "text": "q"}]},
            {"id": "m2", "role": "assistant", "parts": [{"type": "text", "text": "a"}]},
            {"id": "m3", "role": "tool", "parts": [{"type": "text", "text": "tool result"}]},
        ]
        turns = source._messages_to_turns(messages)
        assert len(turns) == 1
        assert turns[0].user_content == "q"
        assert any(ref["role"] == "system" for ref in turns[0].raw_event_refs)
        assert any(ref["role"] == "tool" for ref in turns[0].raw_event_refs)

    def test_messages_to_turns_non_list_parts(self, source):
        messages = [
            {"id": "m1", "role": "user", "parts": "not a list"},
            {"id": "m2", "role": "assistant", "parts": [{"type": "text", "text": "a"}]},
        ]
        turns = source._messages_to_turns(messages)
        assert len(turns) == 1
        assert turns[0].user_content == ""


class TestOpenCodeHelperEdges:
    def test_extract_tool_calls_skips_non_tool(self):
        assert opencode_source._extract_tool_calls([{"type": "text"}]) == []

    def test_extract_tool_calls_skips_non_calling_status(self):
        parts = [{"type": "tool", "tool": "bash", "state": {"status": "completed"}}]
        assert opencode_source._extract_tool_calls(parts) == []

    def test_extract_tool_results_skips_non_completed(self):
        parts = [{"type": "tool", "tool": "bash", "state": {"status": "calling"}}]
        assert opencode_source._extract_tool_results(parts) == []

    def test_extract_messages_unknown_structure(self):
        assert opencode_source._extract_messages("string") == []
        assert opencode_source._extract_messages(123) == []


class TestOpenCodeGetSessionStateExtended:
    def test_get_session_state_no_db_path(self, source, tmp_path):
        info = SessionInfo(session_id="s1", source_path=tmp_path / "x.db")
        with pytest.raises(
            NativeSourceContractError,
            match="native_opencode_session_artifact_missing",
        ):
            source.get_session_state(info)

    def test_get_session_state_missing_db_file(self, source, tmp_path):
        info = SessionInfo(session_id="s1", source_path=tmp_path / "opencode.db")
        with pytest.raises(
            NativeSourceContractError,
            match="native_opencode_session_artifact_missing",
        ):
            source.get_session_state(info)

    def test_get_session_state_connect_failure(self, source, tmp_path):
        info = SessionInfo(session_id="s1", source_path=tmp_path / "opencode.db")
        db_path = tmp_path / "opencode.db"
        db_path.write_text("not a db", encoding="utf-8")
        with pytest.raises(
            NativeSourceContractError,
            match="native_opencode_artifact_evidence_failed",
        ):
            source.get_session_state(info)

    def test_get_session_state_query_failure(self, source, tmp_path):
        conn = sqlite3.connect(tmp_path / "opencode.db")
        conn.execute("CREATE TABLE session (id TEXT)")
        conn.close()
        info = SessionInfo(session_id="s1", source_path=tmp_path / "opencode.db")
        with pytest.raises(
            NativeSourceContractError,
            match="native_opencode_message_schema_incomplete",
        ):
            source.get_session_state(info)
