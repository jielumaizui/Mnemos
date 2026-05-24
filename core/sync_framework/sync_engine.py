# -*- coding: utf-8 -*-
"""
SyncEngine — 统一同步协调层

AgentSource 和 MemosClient 之间的统一协调层。
插件只负责：发现会话 + 解析消息。
引擎负责：防重、过滤、构建、标签、分片、存储、信号采集。

TODO: 当前为骨架实现，待逐步完善：
  - 统一防重数据库（从各 Agent 独立库迁移到 ~/.mnemos/sync_log.db）
  - 画像信号采集（写入 ~/.mnemos/user_signals.db）
  - TriggerDispatcher（WatchdogTrigger / PollingTrigger / HybridTrigger）
  - 分片决策（>7792 bytes 自动分片）
  - KIA Hook 集成
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

from integrations.styx import MemosClient
from core.config import get_config
from core.kia.ingest_helpers import is_noise_message
from core.task_id_parser import TagBuilder, TaskIdParser

from .agent_source import AgentSource, SessionInfo, Turn, SyncResult


class SyncEngine:
    """
    AgentSource 和 MemosClient 之间的统一协调层。

    设计原则：
    - 插件不可见内部逻辑，只提供原始数据
    - 所有同步数据统一经过此引擎，不绕路
    - 画像信号在同步成功后统一采集
    """

    def __init__(
        self,
        client: Optional[MemosClient] = None,
        db_path: Optional[str] = None,
    ):
        self.client = client or self._build_default_client()
        self.db_path = Path(db_path or "~/.mnemos/sync_log.db").expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ---------- 内部工厂 ----------

    def _build_default_client(self) -> MemosClient:
        config = get_config()
        return MemosClient(
            token=config.memos_token,
            base_url=config.memos_api_url,
            agent="sync-engine",
        )

    def _init_db(self):
        """初始化统一防重数据库"""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sync_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_name TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    turn_number INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    memos_uids TEXT,           -- JSON list
                    status TEXT DEFAULT 'synced',
                    synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(agent_name, session_id, turn_number)
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_sync_lookup
                ON sync_log(agent_name, session_id, turn_number)
            """)
            # 画像信号表（统一采集，所有 Agent 自动参与）
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    turn_number INTEGER,
                    content_length INTEGER,
                    has_code INTEGER,
                    has_tools INTEGER,
                    user_questions INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    # ---------- 公共 API ----------

    def sync_session(
        self,
        source: AgentSource,
        session_info: SessionInfo,
        incremental: bool = True,
    ) -> List[SyncResult]:
        """
        同步单个会话的所有轮次。

        Args:
            source: AgentSource 实例
            session_info: 会话信息
            incremental: 是否增量同步（只同步新增轮次）

        Returns:
            SyncResult 列表
        """
        turns = source.parse_turns(session_info.source_path)
        last_synced = self._get_last_synced_turn(source.name, session_info.session_id)
        results: List[SyncResult] = []

        # KIA Hook: session_start
        context = source.on_session_start(
            session_info.session_id,
            {"working_dir": session_info.working_dir, "agent": source.name},
        )

        for turn in turns:
            # 1. 增量跳过
            if incremental and turn.turn_number < last_synced:
                continue

            # 2. 噪音过滤（统一标准）
            if self._is_noise(turn):
                results.append(SyncResult(
                    session_id=session_info.session_id,
                    turn_number=turn.turn_number,
                    action="noise",
                ))
                continue

            # 3. 内容构建
            content = self._build_markdown(turn, session_info.session_id, source.model_tag)
            content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()[:16]

            # 4. 去重检查
            existing = self._check_synced(
                source.name, session_info.session_id, turn.turn_number
            )
            if existing and existing.get("content_hash") == content_hash:
                results.append(SyncResult(
                    session_id=session_info.session_id,
                    turn_number=turn.turn_number,
                    action="skipped",
                    content_hash=content_hash,
                ))
                continue

            # 5. 标签组装（七维标签 + 插件扩展 + 自动检测）
            tags = self._build_tags(source, turn, session_info)
            tags.extend(source.build_extra_tags(turn))

            # 6. 存储 + 分片
            title = f"{source.name}-{session_info.session_id[:8]}-turn{turn.turn_number + 1}"
            try:
                memories = self._save_content(content, tags, title)
                uids = [m.uid for m in memories] if memories else []
                status_str = "updated" if existing else "new"
                self._record_sync(
                    source.name, session_info.session_id,
                    turn.turn_number, content_hash, uids, status_str,
                )
                # 7. 画像信号采集
                self._collect_persona_signal(source, turn)

                results.append(SyncResult(
                    session_id=session_info.session_id,
                    turn_number=turn.turn_number,
                    action=status_str,
                    memos_uids=uids,
                    content_hash=content_hash,
                ))
            except Exception as e:
                results.append(SyncResult(
                    session_id=session_info.session_id,
                    turn_number=turn.turn_number,
                    action="failed",
                    content_hash=content_hash,
                    error=str(e),
                ))

        # KIA Hook: session_end
        all_messages = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": t.user_content if i % 2 == 0 else t.assistant_content}
            for i, t in enumerate(turns)
        ]
        source.on_session_end(session_info.session_id, all_messages)

        return results

    # ---------- 流水线步骤 ----------

    def _is_noise(self, turn: Turn) -> bool:
        """噪音过滤"""
        combined = f"{turn.user_content}\n{turn.assistant_content}"
        return is_noise_message(combined)

    def _build_markdown(self, turn: Turn, session_id: str, model_tag: str) -> str:
        """将 Turn 构建为 Markdown 内容"""
        lines = [
            f"## Turn {turn.turn_number + 1}",
            "",
            f"**User** ({model_tag}):",
            "",
            turn.user_content,
            "",
            "**Assistant**:",
            "",
            turn.assistant_content,
            "",
            "---",
            "",
        ]
        return "\n".join(lines)

    def _build_tags(
        self,
        source: AgentSource,
        turn: Turn,
        session_info: SessionInfo,
    ) -> List[str]:
        """构建七维标签 + 自动检测"""
        tags = [
            f"source={source.name}",
            f"time={datetime.now().strftime('%Y%m%d')}",
            f"model={source.model_tag}",
            "scope=public",
            "status=raw",
            "content_type=session-record",
            "layer=L1",
            f"session={session_info.session_id}",
            f"turn={turn.turn_number + 1}",
        ]
        combined = f"{turn.user_content}\n{turn.assistant_content}"
        if "```" in combined:
            tags.append("has-code=true")
        if "[TOOL_RESULT]" in combined or turn.metadata.get("tool_calls"):
            tags.append("has-tools=true")
        return tags

    def _save_content(self, content: str, tags: List[str], title: str):
        """保存内容到 Memos（含分片决策）"""
        content_bytes = len(content.encode("utf-8"))
        # TODO: 分片阈值应从配置读取（当前蓝图建议 7792 bytes）
        if content_bytes > 7792:
            # 分片保存
            return self.client.save_long_content(
                content=content,
                tags=tags,
                visibility="PUBLIC",
                title=title,
            )
        else:
            return [self.client.save(content, tags, "PUBLIC")]

    def _collect_persona_signal(self, source: AgentSource, turn: Turn):
        """采集用户行为信号，供画像系统分析"""
        combined = f"{turn.user_content}\n{turn.assistant_content}"
        signal = {
            "timestamp": datetime.now().isoformat(),
            "agent": source.name,
            "session_id": getattr(turn, "session_id", ""),
            "turn_number": turn.turn_number,
            "content_length": len(combined),
            "has_code": 1 if "```" in combined else 0,
            "has_tools": 1 if "[TOOL_RESULT]" in combined else 0,
            "user_questions": combined.count("?"),
        }
        try:
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO user_signals
                    (timestamp, agent, session_id, turn_number, content_length, has_code, has_tools, user_questions)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    signal["timestamp"], signal["agent"], signal["session_id"],
                    signal["turn_number"], signal["content_length"],
                    signal["has_code"], signal["has_tools"], signal["user_questions"],
                ))
                conn.commit()
        except Exception:
            # 信号采集失败不应阻塞同步流程
            pass

    # ---------- 数据库操作 ----------

    def _get_last_synced_turn(self, agent_name: str, session_id: str) -> int:
        """获取上次同步到的轮次号"""
        try:
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT MAX(turn_number) FROM sync_log WHERE agent_name = ? AND session_id = ?",
                    (agent_name, session_id),
                )
                row = cursor.fetchone()
                return (row[0] + 1) if row[0] is not None else 0
        except Exception:
            return 0

    def _check_synced(self, agent_name: str, session_id: str, turn_number: int) -> Optional[Dict]:
        """检查某轮次是否已同步"""
        try:
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT content_hash, memos_uids, status FROM sync_log WHERE agent_name = ? AND session_id = ? AND turn_number = ?",
                    (agent_name, session_id, turn_number),
                )
                row = cursor.fetchone()
                if row:
                    return {
                        "content_hash": row[0],
                        "memos_uids": json.loads(row[1]) if row[1] else [],
                        "status": row[2],
                    }
        except Exception:
            pass
        return None

    def _record_sync(
        self,
        agent_name: str,
        session_id: str,
        turn_number: int,
        content_hash: str,
        memos_uids: List[str],
        status: str,
    ):
        """记录同步状态"""
        try:
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT OR REPLACE INTO sync_log
                    (agent_name, session_id, turn_number, content_hash, memos_uids, status, synced_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    agent_name, session_id, turn_number, content_hash,
                    json.dumps(memos_uids), status, datetime.now().isoformat(),
                ))
                conn.commit()
        except Exception:
            pass
