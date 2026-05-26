"""
ClaudeLiveSync — Claude 实时同步

【E14 全库修复】实时监控 Claude session 文件变化并同步到 Memos。
复用 integrations/sources/claude_source.py 的解析能力，提供独立轮询入口。
"""

import json
import time
import threading
import logging
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime

from core.config import get_config
from core.mnemos_bus import publish_event
from integrations.sources.claude_source import ClaudeSource

logger = logging.getLogger(__name__)


class ClaudeLiveSync:
    """Claude 实时同步入口"""

    def __init__(self, polling_interval: int = 30):
        self.polling_interval = polling_interval
        self.source = ClaudeSource()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        # 记录已处理的文件修改时间，用于增量同步
        self._processed_mtimes: Dict[str, float] = {}
        # 记录每个文件已处理的行数（JSONL 增量）
        self._processed_lines: Dict[str, int] = {}

    def start_watching(self):
        """启动文件监控（后台线程轮询）"""
        if self._running:
            logger.info("[ClaudeLiveSync] 已在运行")
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._watch_loop,
            name="ClaudeLiveSync",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"[ClaudeLiveSync] 启动轮询 (间隔 {self.polling_interval}s)")

    def stop_watching(self):
        """停止文件监控"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("[ClaudeLiveSync] 已停止")

    def _watch_loop(self):
        """轮询主循环"""
        while self._running:
            try:
                self._scan_sessions()
            except Exception as e:
                logger.error(f"[ClaudeLiveSync] 扫描失败: {e}")

            # 分段 sleep 以便快速响应 stop()
            end_time = time.time() + self.polling_interval
            while self._running and time.time() < end_time:
                time.sleep(min(1, end_time - time.time()))

    def _scan_sessions(self):
        """扫描 session 文件，检测变化"""
        sessions = self.source.discover_sessions()
        if not sessions:
            return

        for session in sessions:
            session_path = str(session.source_path)
            current_mtime = session.mtime
            last_mtime = self._processed_mtimes.get(session_path, 0)

            if current_mtime > last_mtime:
                self._sync_session(session)
                self._processed_mtimes[session_path] = current_mtime

    def _sync_session(self, session):
        """同步单个 session 的增量内容"""
        session_path = str(session.source_path)

        try:
            # 1. 读取文件总行数
            with open(session.source_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            total_lines = len(lines)
            processed = self._processed_lines.get(session_path, 0)

            if total_lines <= processed:
                return  # 无新内容

            # 2. 解析新增的行
            new_lines = lines[processed:]
            new_messages = []
            for line in new_lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    standardized = self.source._standardize_message(msg)
                    if standardized:
                        new_messages.append(standardized)
                except json.JSONDecodeError:
                    continue

            if not new_messages:
                self._processed_lines[session_path] = total_lines
                return

            # 3. 转换为 Turn 并同步
            turns = self.source.parse_turns(session.source_path)

            # 4. 发布事件 / 同步到 Memos
            synced_count = 0
            for turn in turns:
                if not turn.user_content and not turn.assistant_content:
                    continue

                # 发布同步事件
                try:
                    publish_event("memory_synced", "claude_live_sync", {
                        "session_id": session.session_id,
                        "working_dir": session.working_dir,
                        "turn_number": turn.turn_number,
                        "user_preview": turn.user_content[:200] if turn.user_content else "",
                        "assistant_preview": turn.assistant_content[:200] if turn.assistant_content else "",
                        "has_tools": bool(turn.metadata.get("tool_calls")),
                        "has_reasoning": bool(turn.metadata.get("reasoning")),
                    })
                    synced_count += 1
                except Exception as e:
                    logger.warning(f"[ClaudeLiveSync] 发布事件失败: {e}")

            # 5. 更新已处理行数
            self._processed_lines[session_path] = total_lines

            logger.info(
                f"[ClaudeLiveSync] 同步 {session.session_id}: "
                f"新增 {len(new_messages)} 条消息, {synced_count} turns"
            )

        except Exception as e:
            logger.error(f"[ClaudeLiveSync] 同步失败 {session_path}: {e}")

    def sync_now(self) -> Dict:
        """立即执行同步（非后台模式）"""
        sessions = self.source.discover_sessions()
        total_synced = 0
        total_sessions = 0

        for session in sessions:
            self._sync_session(session)
            total_sessions += 1

        return {
            "synced_sessions": total_sessions,
            "agent": "claude",
            "timestamp": datetime.now().isoformat(),
        }

    def get_status(self) -> Dict:
        """获取同步状态"""
        sessions = self.source.discover_sessions()
        return {
            "running": self._running,
            "polling_interval": self.polling_interval,
            "monitored_sessions": len(sessions),
            "processed_files": len(self._processed_mtimes),
            "last_scan": max(self._processed_mtimes.values()) if self._processed_mtimes else None,
        }

    def reset_tracking(self):
        """重置追踪状态（强制全量同步）"""
        self._processed_mtimes.clear()
        self._processed_lines.clear()
        logger.info("[ClaudeLiveSync] 追踪状态已重置，下次将全量同步")
