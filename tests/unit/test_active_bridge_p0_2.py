"""
Test active_bridge P0-2 fix: messages paired into turns, completeness marked.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from integrations.active_bridge import _messages_to_turns, _read_kimi_fallback_session


def test_pair_user_assistant():
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "how are you?"},
        {"role": "assistant", "content": "fine"},
    ]
    turns = _messages_to_turns(messages)
    assert len(turns) == 2
    assert turns[0]["user_content"] == "hello"
    assert turns[0]["assistant_content"] == "hi"
    assert turns[1]["user_content"] == "how are you?"
    assert turns[1]["assistant_content"] == "fine"


def test_orphan_assistant():
    messages = [
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "hello"},
    ]
    turns = _messages_to_turns(messages)
    assert len(turns) == 2
    assert turns[0]["user_content"] == ""
    assert turns[0]["assistant_content"] == "hi"
    assert turns[1]["user_content"] == "hello"
    assert turns[1]["assistant_content"] == ""


def test_tool_messages_as_raw_events():
    messages = [
        {"role": "user", "content": "call tool"},
        {"role": "assistant", "content": "ok"},
        {"role": "tool", "content": "result"},
    ]
    turns = _messages_to_turns(messages)
    assert len(turns) == 2
    assert turns[0]["assistant_content"] == "ok"
    assert turns[1]["raw_event_refs"][0]["role"] == "tool"


def test_empty_messages():
    assert _messages_to_turns([]) == []


def test_kimi_fallback_reads_active_context_jsonl():
    """[P006] 当前活跃文件 context.jsonl 必须被回读到，不能被 context_*.jsonl 模式漏掉。"""
    import os

    with tempfile.TemporaryDirectory() as td, patch("pathlib.Path.home", return_value=Path(td)):
        sessions_dir = Path(td) / ".kimi" / "sessions"
        session_dir = sessions_dir / "sess-active"
        session_dir.mkdir(parents=True)
        # 旧的归档文件（修改时间更早）
        old_file = session_dir / "context_20260101.jsonl"
        old_file.write_text(
            json.dumps({"role": "user", "content": "old question"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        # 当前活跃文件（修改时间更晚）
        active_file = session_dir / "context.jsonl"
        active_file.write_text(
            json.dumps({"role": "user", "content": "active question"}, ensure_ascii=False)
            + "\n"
            + json.dumps({"role": "assistant", "content": "active answer"}, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
        # 确保 active_file 修改时间最新
        new_mtime = active_file.stat().st_mtime + 10
        os.utime(active_file, (new_mtime, new_mtime))

        session_id, messages = _read_kimi_fallback_session()
        assert session_id == "sess-active"
        assert len(messages) == 2
        assert messages[0]["content"] == "active question"
        assert messages[1]["content"] == "active answer"


def test_kimi_fallback_prefers_latest_modified_file():
    """[P006] 当 context.jsonl 与 context_*.jsonl 同时存在时，选择最近修改的文件。"""
    with tempfile.TemporaryDirectory() as td, patch("pathlib.Path.home", return_value=Path(td)):
        sessions_dir = Path(td) / ".kimi" / "sessions"
        session_dir = sessions_dir / "sess-latest"
        session_dir.mkdir(parents=True)
        active_file = session_dir / "context.jsonl"
        active_file.write_text(
            json.dumps({"role": "user", "content": "active"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        old_file = session_dir / "context_20260101.jsonl"
        old_file.write_text(
            json.dumps({"role": "user", "content": "old"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        # 让归档文件比活跃文件更新
        import os

        new_mtime = active_file.stat().st_mtime + 10
        os.utime(old_file, (new_mtime, new_mtime))

        session_id, messages = _read_kimi_fallback_session()
        assert session_id == "sess-latest"
        assert len(messages) == 1
        assert messages[0]["content"] == "old"
