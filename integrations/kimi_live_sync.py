# -*- coding: utf-8 -*-
"""
KimiLiveSync — Kimi 实时同步 + 历史批量导入

对齐 ClaudeLiveSync 设计：
- discover_sessions() 发现所有可同步会话
- _sync_session() 同步单个会话（整会话导入，Kimi 归档文件不适合行级增量）
- import_all() 一次性导入所有历史记录
- start_watching() 后台轮询新/修改的会话

持久化：~/.mnemos/kimi_sync_state.json 记录已导入的 session_id
"""

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from core.config import get_config
from core.mnemos_bus import publish_event
from integrations.sources.kimi_source import KimiSource

logger = logging.getLogger(__name__)


class KimiLiveSync:
    """Kimi 实时同步入口"""

    def __init__(self, polling_interval: int = 60):
        self.polling_interval = polling_interval
        self.source = KimiSource()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        # 记录已处理的 session（持久化到文件）
        self._state_path = get_config().mnemos_dir / "kimi_sync_state.json"
        self._processed: Dict[str, Dict] = self._load_state()

    def _load_state(self) -> Dict[str, Dict]:
        """加载已处理状态"""
        if self._state_path.exists():
            try:
                return json.loads(self._state_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _save_state(self):
        """保存已处理状态"""
        try:
            self._state_path.write_text(
                json.dumps(self._processed, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[KimiLiveSync] 状态保存失败: {e}")

    def import_all(self, force: bool = False) -> Dict:
        """
        一次性导入所有历史 Kimi 会话。

        Args:
            force: 是否强制重新导入已处理的会话

        Returns:
            导入统计
        """
        sessions = self.source.discover_sessions()
        imported = 0
        skipped = 0
        failed = 0

        for session in sessions:
            sid = session.session_id
            if not force and sid in self._processed:
                # 检查文件是否有修改
                last_mtime = self._processed[sid].get("mtime", 0)
                if session.mtime <= last_mtime:
                    skipped += 1
                    continue

            try:
                if self._sync_session(session):
                    imported += 1
                else:
                    skipped += 1
            except Exception as e:
                logger.error(f"[KimiLiveSync] 导入失败 {sid}: {e}")
                failed += 1

        self._save_state()

        return {
            "total_sessions": len(sessions),
            "imported": imported,
            "skipped": skipped,
            "failed": failed,
            "timestamp": datetime.now().isoformat(),
        }

    def start_watching(self):
        """启动文件监控（后台线程轮询）"""
        if self._running:
            logger.info("[KimiLiveSync] 已在运行")
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._watch_loop,
            name="KimiLiveSync",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"[KimiLiveSync] 启动轮询 (间隔 {self.polling_interval}s)")

    def stop_watching(self):
        """停止文件监控"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("[KimiLiveSync] 已停止")

    def _watch_loop(self):
        """轮询主循环"""
        while self._running:
            try:
                self.import_all(force=False)
            except Exception as e:
                logger.error(f"[KimiLiveSync] 扫描失败: {e}")

            end_time = time.time() + self.polling_interval
            while self._running and time.time() < end_time:
                time.sleep(min(1, end_time - time.time()))

    def _sync_session(self, session) -> bool:
        """
        同步单个 session 到 amphora。

        Returns:
            True 表示成功入队，False 表示跳过或失败。
        """
        sid = session.session_id
        session_path = session.source_path

        # 解析 turns
        turns = self.source.parse_turns(session_path)
        if not turns:
            return False

        # 转换为 messages 格式（和 kimi_adapter.on_session_end 保持一致）
        messages = []
        for turn in turns:
            if turn.user_content:
                messages.append({"role": "user", "content": turn.user_content})
            if turn.assistant_content:
                messages.append({"role": "assistant", "content": turn.assistant_content})

        if len(messages) < 1:
            return False

        # 入队 amphora（和实时 session 行为一致）
        try:
            from core.kia.amphora import enqueue
            enqueue(
                session_id=f"kimi:{sid}",
                messages=messages,
                meta={
                    "source": "kimi",
                    "working_dir": session.working_dir,
                    "imported_at": datetime.now().isoformat(),
                }
            )
        except Exception as e:
            logger.warning(f"[KimiLiveSync] amphora 入队失败 {sid}: {e}")
            return False

        # 发布事件（对齐 ClaudeLiveSync）
        try:
            publish_event("memory_synced", "kimi_live_sync", {
                "session_id": sid,
                "working_dir": session.working_dir,
                "message_count": len(messages),
                "turn_count": len(turns),
                "has_reasoning": any(t.metadata.get("reasoning") for t in turns),
            })
        except Exception as e:
            logger.warning(f"[KimiLiveSync] 事件发布失败: {e}")

        # 更新状态
        self._processed[sid] = {
            "mtime": session.mtime,
            "imported_at": datetime.now().isoformat(),
            "message_count": len(messages),
            "turn_count": len(turns),
        }

        logger.info(
            f"[KimiLiveSync] 同步 {sid}: "
            f"{len(turns)} turns, {len(messages)} 条消息已入队"
        )
        return True

    def get_status(self) -> Dict:
        """获取同步状态"""
        sessions = self.source.discover_sessions()
        return {
            "running": self._running,
            "polling_interval": self.polling_interval,
            "total_sessions": len(sessions),
            "imported_sessions": len(self._processed),
            "state_file": str(self._state_path),
        }


def main():
    """CLI 入口：一次性导入所有历史记录"""
    import argparse
    parser = argparse.ArgumentParser(description="Kimi 历史会话批量导入")
    parser.add_argument("--force", action="store_true", help="强制重新导入")
    parser.add_argument("--watch", action="store_true", help="启动后台轮询")
    args = parser.parse_args()

    sync = KimiLiveSync()

    if args.watch:
        sync.start_watching()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            sync.stop_watching()
    else:
        result = sync.import_all(force=args.force)
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
