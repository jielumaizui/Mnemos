# -*- coding: utf-8 -*-
"""
Claude Code 会话自动监听器 v4.0 - 跨平台重构版

监听 Claude Code 的 session 文件变化，自动同步到 Memos（L1 原始池）。
支持增量同步、防重、防抖。

⚠️ @deprecated — 待迁移到 SyncFramework
  新架构：ClaudeSource（AgentSource 接口）+ SyncEngine（统一协调）
  当前文件作为兼容层保留，新功能应使用 integrations/sources/claude_source.py

迁移自: memos-client/claude_live_sync.py
改造点:
- import 改为重构项目路径
- SQLite 路径用 get_config().data_dir / "live_sync.db"（_LazyPath）
- Claude 数据目录用 get_config().claude_data_dir 替代硬编码
- watchdog 跨平台 Observer
- 防抖 5 秒保留，线程管理改进
- SQLite timeout=10
- datetime 用 UTC
"""

from __future__ import annotations

import os
import sys
import json
import time
import hashlib
import sqlite3
import re
import threading
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Optional

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    _WATCHDOG_AVAILABLE = True
except ImportError:
    _WATCHDOG_AVAILABLE = False
    # Fallback: define stub so the class can still be imported
    class FileSystemEventHandler:  # type: ignore[no-redef]
        pass

from integrations.styx import MemosClient
from core.task_id_parser import TagBuilder, TaskIdParser
from core.kia.ingest_helpers import is_noise_message
from core.config import get_config

logger = logging.getLogger(__name__)


# ==================== _LazyPath ====================

class _LazyPath:
    """Lazy path that resolves get_config() only on access."""
    __slots__ = ('_base', '_segments')

    def __init__(self, base: str = "data_dir", *segments):
        self._base = base
        self._segments = segments

    def __truediv__(self, other):
        return _LazyPath(self._base, *self._segments, other)

    def __rtruediv__(self, other):
        raise NotImplementedError

    def _resolve(self) -> Path:
        config = get_config()
        if self._base == "data_dir":
            result = config.data_dir
        elif self._base == "wiki_dir":
            result = config.wiki_dir
        elif self._base == "claude_data_dir":
            result = config.claude_data_dir
        else:
            result = config.data_dir
        for seg in self._segments:
            result = result / seg
        return result

    def __str__(self):
        return str(self._resolve())

    def __repr__(self):
        return f"LazyPath({self._base}:{'/'.join(self._segments)})"

    def __fspath__(self):
        return str(self._resolve())

    def __getattr__(self, name):
        return getattr(self._resolve(), name)

    def __hash__(self):
        return hash(self._resolve())

    def __eq__(self, other):
        return self._resolve() == other

    def __iter__(self):
        return iter(self._resolve())


# ==================== 路径常量 ====================

DB_PATH = _LazyPath("data_dir", "live_sync.db")
BUFFER_DIR = _LazyPath("data_dir", "l1_buffer")
CLAUDE_PROJECTS_DIR = _LazyPath("claude_data_dir", "projects")


def _utcnow() -> datetime:
    """返回带时区的当前 UTC 时间"""
    return datetime.now(timezone.utc)


# ==================== ClaudeSessionHandler ====================

class ClaudeSessionHandler(FileSystemEventHandler):
    """Claude 会话文件变化处理器 - L1 原始池完整记录 + 自动 Ingest"""

    AUTO_INGEST_CLEAN = False  # 蒸馏由 distill_worker 定时处理
    INGEST_BATCH_SIZE = 10
    INGEST_INTERVAL = 60
    DEBOUNCE_SECONDS = 5.0

    def __init__(self):
        config = get_config()
        token = config.memos_token
        if not token:
            raise ValueError("MEMOS_TOKEN 未配置（config.yaml 或环境变量）")

        self.client = MemosClient(
            token=token,
            agent="claude",
        )

        # 持久化防重数据库
        db_path = Path(DB_PATH)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._init_db()

        # 加载已处理记录（进程重启后恢复）
        self.processed_sessions = self._load_processed_sessions()
        logger.info(f"[L1 Sync] 已加载 {len(self.processed_sessions)} 条历史处理记录")

        self.last_save_time = 0
        self.min_save_interval = 5
        self.last_ingest_time = 0
        self.pending_ingest: List[Dict] = []
        self._ensure_buffer_dir()

        # 防抖机制：文件稳定 5 秒后再处理
        self._pending_timers: Dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    # -------------------- DB --------------------

    def _get_conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._db_path), timeout=10)

    def _init_db(self):
        """初始化防重数据库"""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS processed_sessions (
                    session_id TEXT PRIMARY KEY,
                    line_count INTEGER,
                    content_hash TEXT,
                    memos_uids TEXT,
                    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    def _load_processed_sessions(self) -> Dict[str, int]:
        """从数据库加载已处理记录"""
        sessions = {}
        try:
            with self._get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT session_id, line_count FROM processed_sessions")
                for row in cursor.fetchall():
                    sessions[row[0]] = row[1]
        except Exception as e:
            logger.warning(f"[L1 Sync] 加载历史记录失败: {e}")
        return sessions

    def _is_content_duplicate(self, session_id: str, messages: List[Dict]) -> Optional[str]:
        """
        增强防重：检查内容是否已存在。
        返回: memos_uid 如果已存在，None 如果不存在
        """
        content_for_hash = json.dumps(messages, sort_keys=True, ensure_ascii=False)
        content_hash = hashlib.md5(content_for_hash.encode()).hexdigest()[:16]

        try:
            with self._get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT memos_uids FROM processed_sessions WHERE content_hash = ?",
                    (content_hash,)
                )
                row = cursor.fetchone()
                if row:
                    uids = json.loads(row[0]) if row[0] else []
                    return uids[0] if uids else "exists"
        except Exception as e:
            logger.warning(f"[L1 Sync] 检查重复失败: {e}")

        return None

    def _record_processed(self, session_id: str, line_count: int,
                          messages: Optional[List[Dict]], memos_uids: List[str]):
        """持久化记录已处理的 session"""
        content_hash = None
        if messages is not None:
            content_for_hash = json.dumps(messages, sort_keys=True, ensure_ascii=False)
            content_hash = hashlib.md5(content_for_hash.encode()).hexdigest()[:16]

        try:
            with self._get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT OR REPLACE INTO processed_sessions
                    (session_id, line_count, content_hash, memos_uids, processed_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (session_id, line_count, content_hash, json.dumps(memos_uids),
                      _utcnow().isoformat()))
                conn.commit()
        except Exception as e:
            logger.warning(f"[L1 Sync] 记录处理状态失败: {e}")

    # -------------------- Buffer --------------------

    def _ensure_buffer_dir(self):
        """确保缓冲目录存在"""
        buf = Path(BUFFER_DIR)
        buf.mkdir(parents=True, exist_ok=True)

    def _save_to_buffer(self, session_id: str, messages: List[Dict], tags: List[str]):
        """保存到本地 buffer 作为备份"""
        buf = Path(BUFFER_DIR)
        buffer_file = buf / f"{session_id}.json"

        data = {
            "session_id": session_id,
            "synced_at": _utcnow().isoformat(),
            "message_count": len(messages),
            "tags": tags,
            "messages": messages,
        }

        try:
            buffer_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding='utf-8',
            )
        except Exception as e:
            logger.warning(f"[L1 Sync] Buffer 保存失败: {e}")

    # -------------------- Watchdog --------------------

    def on_modified(self, event):
        if event.is_directory:
            return

        if not event.src_path.endswith('.jsonl'):
            return

        filepath = event.src_path

        # 防抖：取消旧定时器，重新等待文件稳定
        with self._lock:
            old_timer = self._pending_timers.pop(filepath, None)
            if old_timer is not None:
                old_timer.cancel()

            timer = threading.Timer(
                self.DEBOUNCE_SECONDS,
                self._process_file_debounced,
                [filepath],
            )
            timer.daemon = True
            timer.start()
            self._pending_timers[filepath] = timer

    def _process_file_debounced(self, filepath: str):
        """文件稳定后执行处理"""
        with self._lock:
            self._pending_timers.pop(filepath, None)
        try:
            self.process_session_file(filepath)
        except Exception as e:
            logger.warning(f"[L1 Sync] 处理失败: {e}")

    # -------------------- Message Parsing --------------------

    def _parse_messages(self, lines: List[str]) -> List[Dict]:
        """解析 JSONL 行列表为标准消息"""
        messages = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                standardized = self._standardize_message(msg)
                if standardized:
                    messages.append(standardized)
            except json.JSONDecodeError:
                continue
        return messages

    def _standardize_message(self, msg) -> Optional[Dict]:
        """标准化消息格式 - 适配 Claude Code 的 JSONL 格式"""
        if not isinstance(msg, dict):
            return None

        # Claude Code 格式：消息嵌套在 'message' 字段中
        message_data = msg.get('message', msg)

        role = message_data.get('role', '')
        if not role:
            role = msg.get('type', '')

        # 处理 content（可能是字符串或数组）
        raw_content = message_data.get('content', '')
        if isinstance(raw_content, list):
            content_parts = []
            for part in raw_content:
                if isinstance(part, dict):
                    if part.get('type') == 'text':
                        content_parts.append(part.get('text', ''))
                    elif 'content' in part:
                        content_parts.append(str(part['content']))
            content = '\n'.join(content_parts)
        else:
            content = str(raw_content)

        if not role or not content:
            return None

        # 噪声过滤
        if is_noise_message(content):
            return None

        # 提取工具调用信息
        tool_calls = message_data.get('tool_calls', msg.get('tool_calls', []))
        tool_results = msg.get('toolUseResult') or msg.get('tool_results')

        standardized = {
            "role": role,
            "content": content,
            "timestamp": msg.get('timestamp') or _utcnow().isoformat(),
        }

        if tool_calls:
            standardized["tool_calls"] = [
                {
                    "name": t.get('name', t.get('function', {}).get('name', 'unknown')),
                    "input": t.get('input', t.get('arguments', t.get('function', {}).get('arguments', {}))),
                }
                for t in tool_calls[:10]
            ]

        if tool_results:
            if isinstance(tool_results, dict):
                standardized["tool_results"] = [{
                    "stdout": str(tool_results.get('stdout', ''))[:500],
                    "stderr": str(tool_results.get('stderr', ''))[:200],
                }]

        # 保留 reasoning/thinking 内容
        for part in (raw_content if isinstance(raw_content, list) else []):
            if isinstance(part, dict) and part.get('type') in ('thinking', 'reasoning'):
                standardized["reasoning"] = part.get('thinking', part.get('text', ''))[:2000]
                break

        return standardized

    # -------------------- Tag Building --------------------

    def _build_five_dimension_tags(self, session_id: str, messages: List[Dict]) -> List[str]:
        """构建七维标签（L1 对齐：source | time | model | scope | status | content_type | layer）

        TODO: 迁移到 SyncEngine._build_tags() 统一生成。
        """
        # 解析 task_id（从消息内容中）
        task_id = None
        for msg in messages:
            if msg.get('role') == 'user':
                content = msg.get('content', '')
                parsed = TaskIdParser.parse(content)
                if parsed:
                    task_id = parsed
                    break

        # 检测是否为私有请求
        is_private = False
        for msg in messages:
            if msg.get('role') == 'user':
                content = msg.get('content', '')
                if TaskIdParser.is_private_request(content):
                    is_private = True
                    break

        scope = "private" if is_private else "public"

        tags = TagBuilder.build_tags(
            source="claude",
            model="claude-code",
            task_id=task_id,
            scope=scope,
        )

        # L1 七维标签补充（对齐 SyncEngine 规范）
        tags.append("status=raw")
        tags.append("content_type=session-record")
        tags.append("layer=L1")  # 修正：level → layer

        tags.extend([
            f"session={session_id}",
            f"msg-count={len(messages)}",
        ])

        # 检测是否包含代码
        has_code = any(
            '```' in msg.get('content', '')
            for msg in messages
        )
        if has_code:
            tags.append("has-code=true")

        # 检测是否包含工具调用
        has_tools = any(
            msg.get('tool_calls') for msg in messages
        )
        if has_tools:
            tags.append("has-tools=true")

        # 回流防护：检测 wiki 上下文引用
        has_wiki_context = any(
            "<wiki-context" in msg.get('content', '')
            for msg in messages
        )
        if has_wiki_context:
            tags.append("skip-distill=true")

        return tags

    def _build_delta_tags(self, session_id: str, messages: List[Dict],
                          start_line: int, end_line: int) -> List[str]:
        """构建增量标签"""
        tags = self._build_five_dimension_tags(session_id, messages)
        tags = [t if t != "type:session-record" else "type:session-delta" for t in tags]
        tags.append(f"delta-range:{start_line}-{end_line}")
        return tags

    # -------------------- Core Processing --------------------

    def process_session_file(self, filepath: str):
        """增量模式处理会话文件"""
        filepath = Path(filepath)
        session_id = filepath.stem

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception as e:
            logger.error(f"[L1 Sync] 读取文件失败: {e}")
            return

        if len(lines) < 2:
            return

        last_count = self.processed_sessions.get(session_id, 0)
        if len(lines) <= last_count:
            return  # 没有新内容

        is_new_session = last_count == 0

        if is_new_session:
            self._handle_new_session(session_id, lines)
        else:
            self._handle_delta_session(session_id, lines, last_count)

    def _handle_new_session(self, session_id: str, lines: List[str]):
        """首次保存：解析全部消息，保存完整 session"""
        messages = self._parse_messages(lines)
        if len(messages) < 2:
            return

        # 防重：内容 hash 检查
        existing_uid = self._is_content_duplicate(session_id, messages)
        if existing_uid:
            logger.info(f"[L1 Sync] 内容已存在，跳过: {session_id[:16]}...")
            self.processed_sessions[session_id] = len(lines)
            return

        tags = self._build_five_dimension_tags(session_id, messages)
        try:
            memories = self.client.save_session_full(
                session_id=session_id,
                messages=messages,
                tags=tags,
                visibility="PUBLIC",
            )
            memos_uids = [m.uid for m in memories] if memories else []
            self._record_processed(session_id, len(lines), messages, memos_uids)
            self.processed_sessions[session_id] = len(lines)
            self.last_save_time = time.time()
            self._save_to_buffer(session_id, messages, tags)

            logger.info(
                f"[L1 Sync] 新 session: {session_id[:16]}... "
                f"({len(messages)} 条消息, {len(memos_uids)} 分片)"
            )
        except Exception as e:
            logger.error(f"[L1 Sync] 上传失败: {e}")

    def _handle_delta_session(self, session_id: str, lines: List[str], last_count: int):
        """增量保存：只解析新增行，追加保存"""
        new_messages = self._parse_messages(lines[last_count:])
        if not new_messages:
            # 行数增加但无有效消息，更新计数避免重复检查
            self.processed_sessions[session_id] = len(lines)
            self._record_processed(session_id, len(lines), None, [])
            return

        tags = self._build_delta_tags(session_id, new_messages, last_count, len(lines))
        try:
            memories = self._save_session_delta(
                session_id=session_id,
                messages=new_messages,
                start_line=last_count,
                end_line=len(lines),
                tags=tags,
            )
            memos_uids = [m.uid for m in memories] if memories else []
            self._record_processed(session_id, len(lines), None, memos_uids)
            self.processed_sessions[session_id] = len(lines)
            self.last_save_time = time.time()

            logger.info(
                f"[L1 Sync] 增量: {session_id[:16]}... "
                f"(+{len(new_messages)} 条消息, {len(memos_uids)} 分片)"
            )
        except Exception as e:
            logger.error(f"[L1 Sync] 增量上传失败: {e}")

    def _save_session_delta(self, session_id: str, messages: List[Dict],
                            start_line: int, end_line: int,
                            tags: List[str]) -> List:
        """保存 session 增量"""
        delta_payload = {
            "session_id": session_id,
            "delta_from_line": start_line,
            "delta_to_line": end_line,
            "message_count": len(messages),
            "messages": messages,
        }
        content = json.dumps(delta_payload, ensure_ascii=False, indent=2)
        delta_tags = tags + [f"delta-range:{start_line}-{end_line}", "type=session-delta"]

        content_bytes = len(content.encode('utf-8'))
        if content_bytes > 8000:
            return self.client.save_long_content(
                content=content,
                tags=delta_tags,
                visibility="PUBLIC",
                title=f"session-{session_id}",
            )
        else:
            return [self.client.save(content, delta_tags, "PUBLIC")]

    # -------------------- Status --------------------

    def get_ingest_status(self) -> Dict:
        """获取 Ingest 状态"""
        return {
            "pending_count": len(self.pending_ingest),
            "auto_ingest_enabled": self.AUTO_INGEST_CLEAN,
            "batch_size": self.INGEST_BATCH_SIZE,
            "interval_seconds": self.INGEST_INTERVAL,
            "last_ingest": (
                datetime.fromtimestamp(self.last_ingest_time, tz=timezone.utc).isoformat()
                if self.last_ingest_time else None
            ),
        }


# ==================== 启动监控 ====================

def start_monitoring(auto_ingest: bool = True):
    """启动监控（跨平台）"""
    if not _WATCHDOG_AVAILABLE:
        logger.error("[L1 Sync] watchdog 未安装，无法启动文件监控。请运行: pip install watchdog")
        return

    watch_dir = Path(CLAUDE_PROJECTS_DIR)

    if not watch_dir.exists():
        logger.error(f"[L1 Sync] 目录不存在: {watch_dir}")
        return

    logger.info(f"[L1 Sync] 开始监控: {watch_dir}")
    logger.info("[L1 Sync] 使用五维标签: source | time | model | scope | processed")
    logger.info(f"[L1 Sync] 自动 Ingest: {'开启' if auto_ingest else '关闭'}")
    logger.info("[L1 Sync] 按 Ctrl+C 停止")

    event_handler = ClaudeSessionHandler()
    event_handler.AUTO_INGEST_CLEAN = auto_ingest
    observer = Observer()
    observer.schedule(event_handler, str(watch_dir), recursive=True)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        logger.info("\n[L1 Sync] 已停止")
        status = event_handler.get_ingest_status()
        if status["pending_count"] > 0:
            logger.info(f"[L1 Sync] 还有 {status['pending_count']} 条记录待 Ingest")

    observer.join()


# 兼容别名
ClaudeLiveSync = ClaudeSessionHandler


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    import argparse

    parser = argparse.ArgumentParser(description="Claude Live Sync - L1 原始池")
    parser.add_argument("--no-auto-ingest", action="store_true",
                        help="禁用自动 Ingest（默认关闭）")
    parser.add_argument("--status", action="store_true",
                        help="显示 Ingest 状态")
    args = parser.parse_args()

    if args.status:
        handler = ClaudeSessionHandler()
        status = handler.get_ingest_status()
        print(json.dumps(status, indent=2, ensure_ascii=False))
    else:
        start_monitoring(auto_ingest=not args.no_auto_ingest)
