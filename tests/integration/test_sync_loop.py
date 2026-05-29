# -*- coding: utf-8 -*-
"""
P1-2 长链路测试 — 同步链路

链路：FakeAgentSource → SyncEngine → fake MemosClient
      → sync_log 记录 → distill_status=pending

策略：临时 SQLite sync_log，mock MemosClient HTTP 调用，
      FakeAgentSource 从内存解析 turns。
断言目标：sync_log 字段完整、去重生效、噪声过滤。
"""

import json
import sqlite3
from pathlib import Path
from typing import List, Optional, Dict, Any
from unittest.mock import MagicMock
from dataclasses import dataclass

import pytest


@dataclass
class FakeAgentSource:
    """测试用的 AgentSource 实现。"""
    _name: str = "test_agent"
    _model_tag: str = "test"
    _turns: List[dict] = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def model_tag(self) -> str:
        return self._model_tag

    def discover_sessions(self):
        return []

    def parse_turns(self, session_path: Path):
        from core.sync_framework.agent_source import Turn
        turns = []
        if self._turns:
            for t in self._turns:
                turns.append(Turn(
                    turn_number=t["turn_number"],
                    user_content=t.get("user_content", ""),
                    assistant_content=t.get("assistant_content", ""),
                    timestamp=t.get("timestamp"),
                ))
        return turns

    def on_session_start(self, session_id: str, context: Dict[str, Any]) -> Dict[str, Any]:
        return {}

    def on_session_end(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        pass

    def build_extra_tags(self, turn):
        return []


class TestSyncEngineLoop:
    """SyncEngine 完整同步链路。"""

    @pytest.fixture
    def sync_db(self, tmp_path):
        """只返回临时 DB 路径，schema 由 SyncEngine._init_db() 自动创建。"""
        return tmp_path / "sync_log.db"

    def _make_engine(self, sync_db, monkeypatch):
        from core.sync_framework.sync_engine import SyncEngine

        # 阻止 EventBus 加载 200万+ pending 事件导致超时
        monkeypatch.setattr(
            "core.mnemos_bus.EventBus._recover_pending",
            lambda self: None,
        )
        # mock publish_event 避免 sqlite locked 和生产事件污染
        monkeypatch.setattr(
            "core.mnemos_bus.publish_event",
            lambda *args, **kwargs: None,
        )

        # mock MemosClient 避免真实 HTTP
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.uid = "memo-123"
        mock_result.id = 123
        mock_client.save.return_value = mock_result
        mock_client.save_long_content.return_value = [
            MagicMock(uid="memo-001", id=1),
            MagicMock(uid="memo-002", id=2),
        ]
        mock_client._sanitize = lambda content: content  # 脱敏直接透传

        engine = SyncEngine(
            client=mock_client,
            db_path=str(sync_db),
        )
        return engine, mock_client

    def _make_session_info(self, tmp_path, session_id: str, turns: List[dict]):
        from core.sync_framework.agent_source import SessionInfo
        # 写入一个假的会话文件
        source_path = tmp_path / f"{session_id}.json"
        source_path.write_text(json.dumps(turns), encoding="utf-8")
        return SessionInfo(
            session_id=session_id,
            source_path=source_path,
            working_dir=str(tmp_path),
        )

    def test_sync_session_creates_sync_log(self, sync_db, tmp_path, monkeypatch):
        engine, mock_client = self._make_engine(sync_db, monkeypatch)

        turns = [
            {"turn_number": 1, "user_content": "Hello", "assistant_content": "world", "timestamp": "2024-01-01T00:00:00Z"},
            {"turn_number": 2, "user_content": "How are you?", "assistant_content": "Fine thanks", "timestamp": "2024-01-01T00:01:00Z"},
        ]
        source = FakeAgentSource(_turns=turns)
        session_info = self._make_session_info(tmp_path, "sess-sync-001", turns)

        results = engine.sync_session(source, session_info)

        assert len(results) == 2
        assert all(r.action in ("new", "updated") for r in results)

        # sync_log 应有 2 条记录
        with sqlite3.connect(str(sync_db)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT session_id, turn_number, status, distill_status, memos_uids FROM sync_log WHERE session_id=?",
                ("sess-sync-001",),
            ).fetchall()
            assert len(rows) == 2
            assert rows[0]["status"] in ("new", "updated")
            assert rows[0]["distill_status"] == "pending"
            assert rows[0]["memos_uids"]  # 不应为空

        # MemosClient 被调用
        assert mock_client.save.called or mock_client.save_long_content.called

    def test_dedup_skips_same_content(self, sync_db, tmp_path, monkeypatch):
        engine, _ = self._make_engine(sync_db, monkeypatch)

        turns = [
            {"turn_number": 1, "user_content": "same", "assistant_content": "content", "timestamp": "2024-01-01T00:00:00Z"},
        ]
        source = FakeAgentSource(_turns=turns)
        session_info = self._make_session_info(tmp_path, "sess-dedup", turns)

        # 第一次同步
        r1 = engine.sync_session(source, session_info)
        assert all(r.action == "new" for r in r1)

        # 第二次同步相同内容
        r2 = engine.sync_session(source, session_info)
        assert all(r.action == "skipped" for r in r2)

    def test_noise_filter_skips_short_content(self, sync_db, tmp_path, monkeypatch):
        engine, mock_client = self._make_engine(sync_db, monkeypatch)

        turns = [
            {"turn_number": 1, "user_content": "ok", "assistant_content": "", "timestamp": "2024-01-01T00:00:00Z"},
            {"turn_number": 2, "user_content": "嗯", "assistant_content": "", "timestamp": "2024-01-01T00:01:00Z"},
        ]
        source = FakeAgentSource(_turns=turns)
        session_info = self._make_session_info(tmp_path, "sess-noise", turns)

        results = engine.sync_session(source, session_info)

        # 噪声消息应被过滤为 noise
        noise_count = sum(1 for r in results if r.action == "noise")
        assert noise_count >= 1
