# Typhon — 百头巨龙，盖亚之子
# OpenClaw 适配器 — 多爪并行，SQLite 驱动的 Agent 集成

import json
import logging
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

from integrations.olympus import AgentAdapter, AgentRegistry



logger = logging.getLogger(__name__)
class OpenClawAdapter(AgentAdapter):
    """OpenClaw Agent 适配器

    OpenClaw 采用 SQLite 作为输入接口：
    - Mnemos 读取 OpenClaw SQLite 数据库获取会话历史
    - 蒸馏任务写入 SQLite 任务表等待 OpenClaw 处理
    """

    @property
    def name(self) -> str:
        return "openclaw"

    @property
    def priority(self) -> int:
        return 3

    def _sqlite_path(self) -> Path:
        """OpenClaw SQLite 数据库路径"""
        from core.config import get_config
        custom = getattr(get_config(), "openclaw_sqlite_path", None)
        if custom:
            return Path(custom)
        return Path.home() / ".openclaw" / "sessions.db"

    def is_available(self) -> bool:
        """检测 OpenClaw 是否安装"""
        return self.get_data_dir().exists()

    def get_data_dir(self) -> Optional[Path]:
        return Path.home() / ".openclaw"

    def on_session_start(self, working_dir: str, user_message: str = "") -> Dict:
        knowledge = self.inject_knowledge("general", context_text=user_message)
        try:
            from core.mnemos_bus import EventBus
            bus = EventBus()
            bus.publish("session.start", self.name, {
                "working_dir": working_dir,
                "user_message": user_message,
                "knowledge_loaded": knowledge.get("loaded", False),
            })
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error", exc_info=True)
            pass
        return {"agent": self.name, "knowledge": knowledge}

    def on_session_end(self, working_dir: str, session_messages: List[Dict] = None) -> Dict:
        sid = None
        if session_messages:
            try:
                from core.kia.amphora import enqueue
                from datetime import datetime
                wd = working_dir or __import__('os').getcwd()
                dir_hash = __import__('hashlib').md5(wd.encode()).hexdigest()[:8]
                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                sid = f"openclaw:{dir_hash}:{ts}"
                enqueue(
                    session_id=sid,
                    messages=session_messages,
                    meta={"source": "openclaw", "working_dir": wd}
                )
            except Exception as e:
                logger.warning(f"OpenClaw 蒸馏入队失败: {e}")
        try:
            from core.mnemos_bus import EventBus
            bus = EventBus()
            bus.publish("session.end", self.name, {
                "working_dir": working_dir,
                "session_id": sid,
                "messages": session_messages or [],
                "meta": {"source": self.name, "working_dir": working_dir or __import__('os').getcwd()},
            })
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error", exc_info=True)
            pass
        return {"saved": True, "distill_task_id": sid}

    def install_hooks(self) -> bool:
        """安装 OpenClaw 的 session hooks

        在 SQLite 数据库中创建 config 表，存储 session 回调配置。
        """
        try:
            data_dir = self.get_data_dir()
            data_dir.mkdir(parents=True, exist_ok=True)
            db_path = self._sqlite_path()

            # 1. 确保 SQLite 数据库存在
            with sqlite3.connect(str(db_path), timeout=10) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS mnemos_config (
                        key TEXT PRIMARY KEY,
                        value TEXT,
                        updated_at TEXT
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS mnemos_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_type TEXT,
                        payload TEXT,
                        created_at TEXT
                    )
                """)

                from datetime import datetime, timezone
                now = datetime.now(timezone.utc).isoformat()

                # 2. 写入回调配置
                wrapper_path = data_dir / "mnemos_wrapper.py"
                conn.execute(
                    "INSERT OR REPLACE INTO mnemos_config (key, value, updated_at) VALUES (?, ?, ?)",
                    ("session_start_wrapper", str(wrapper_path), now)
                )
                conn.execute(
                    "INSERT OR REPLACE INTO mnemos_config (key, value, updated_at) VALUES (?, ?, ?)",
                    ("session_end_wrapper", str(wrapper_path), now)
                )
                conn.execute(
                    "INSERT OR REPLACE INTO mnemos_config (key, value, updated_at) VALUES (?, ?, ?)",
                    ("mnemos_enabled", "true", now)
                )
                conn.commit()

            # 3. 生成 wrapper 脚本
            wrapper_path.write_text(self._generate_wrapper_script(), encoding="utf-8")

            logger.info(f"[Typhon] OpenClaw hooks 已安装到 {db_path}")
            return True
        except Exception as e:
            logger.warning(f"[Typhon] 安装 OpenClaw hooks 失败: {e}")
            return False

    def _generate_wrapper_script(self) -> str:
        """生成 OpenClaw wrapper 脚本"""
        return '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemos-OpenClaw Wrapper
接收 OpenClaw 的 session 事件，写入 Mnemos 事件总线
"""

import sys
import os
import argparse
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

# 确保能找到 Mnemos 模块
script_dir = Path(__file__).parent
sys.path.insert(0, str(script_dir.parent.parent))

from core.mnemos_bus import EventBus


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-start", action="store_true")
    parser.add_argument("--session-end", action="store_true")
    parser.add_argument("--working-dir", default=os.getcwd())
    parser.add_argument("--user-message", default="")
    args = parser.parse_args()

    bus = EventBus()

    if args.session_start:
        bus.publish("session.start", "openclaw", {
            "working_dir": args.working_dir,
            "user_message": args.user_message,
        })
        print("[Mnemos] session.start 事件已发布")
    elif args.session_end:
        bus.publish("session.end", "openclaw", {
            "working_dir": args.working_dir,
            "messages": [],
            "meta": {"source": "openclaw", "working_dir": args.working_dir},
        })
        print("[Mnemos] session.end 事件已发布")


if __name__ == "__main__":
    main()
'''

    def collect_signals(self, days: int = 7) -> List[Dict]:
        """从 OpenClaw SQLite 读取会话信号"""
        signals = []
        db_path = self._sqlite_path()
        if not db_path.exists():
            return signals

        try:
            with sqlite3.connect(str(db_path), timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                from datetime import datetime, timedelta
                cutoff = (datetime.now() - timedelta(days=days)).isoformat()
                cursor = conn.execute(
                    "SELECT * FROM sessions WHERE timestamp > ? ORDER BY timestamp DESC",
                    (cutoff,)
                )
                for row in cursor.fetchall():
                    signals.append({
                        "source": "openclaw",
                        "session_id": row["session_id"],
                        "timestamp": row["timestamp"],
                        "task_type": row.get("task_type", "unknown"),
                        "raw": dict(row),
                    })
        except Exception as e:
            logger.warning(f"读取 OpenClaw SQLite 失败: {e}")

        return signals

    def inject_knowledge(self, task_type: str, subtype: str = "", context_text: str = "") -> Dict:
        from core.kia.prophasis import PreFlightInjector
        from core.kia.kairos import TimeWindow, TimeWindowType
        injector = PreFlightInjector()
        time_window = TimeWindow(window=TimeWindowType.IMMEDIATE, days_until=0)
        knowledge = injector.inject(task_type, subtype, time_window, context_text)
        if not knowledge:
            return {"loaded": False}
        return {
            "loaded": True,
            "task_type": knowledge.task_type,
            "checklist": [c.item for c in knowledge.checklist],
        }

    def delegate_distillation(self, task_path: Path, output_path: Path) -> bool:
        """委托 OpenClaw 执行蒸馏

        策略：将任务写入 OpenClaw SQLite 的 mnemos_tasks 表。
        同时写入通知标记到 mnemos_events 表。
        """
        try:
            task = json.loads(task_path.read_text(encoding="utf-8"))
            db_path = self._sqlite_path()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(str(db_path), timeout=10) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS mnemos_tasks (
                        id TEXT PRIMARY KEY,
                        type TEXT,
                        payload TEXT,
                        output_path TEXT,
                        status TEXT DEFAULT 'pending',
                        created_at TEXT
                    )
                """)
                from datetime import datetime
                # 写入通知标记
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS mnemos_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_type TEXT,
                        payload TEXT,
                        created_at TEXT
                    )
                """)
                notify_payload = json.dumps({
                    "event_type": "distill.request",
                    "agent": "openclaw",
                    "session_id": task.get("session_id", "unknown"),
                    "output_path": str(output_path),
                }, ensure_ascii=False)
                conn.execute(
                    "INSERT INTO mnemos_events (event_type, payload, created_at) VALUES (?, ?, ?)",
                    ("distill.request", notify_payload, datetime.now().isoformat())
                )
                conn.execute(
                    "INSERT OR REPLACE INTO mnemos_tasks (id, type, payload, output_path, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        task.get("session_id", "unknown"),
                        "distillation",
                        json.dumps(task, ensure_ascii=False),
                        str(output_path),
                        "pending",
                        datetime.now().isoformat(),
                    )
                )
            # 构建完整 prompt 放入 payload，供 OpenClaw 读取
            prompt_content = self._build_distill_prompt(task)
            task["meta"]["full_prompt"] = prompt_content
            # 重新序列化 task（因为上面修改了 meta）
            conn.execute(
                "INSERT OR REPLACE INTO mnemos_tasks (id, type, payload, output_path, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    task.get("session_id", "unknown"),
                    "distillation",
                    json.dumps(task, ensure_ascii=False),
                    str(output_path),
                    "pending",
                    datetime.now().isoformat(),
                )
            )
            # 写入输出占位符（Agent 会覆盖）
            output_path.parent.mkdir(parents=True, exist_ok=True)
            placeholder = (
                f"<!-- MNEMOS_DISTILL_TASK: {task.get('session_id')} -->\n"
                f"<!-- 输出格式要求：\n"
                f"  1. 必须是有效的 JSON 对象\n"
                f"  2. 顶层字段：judgment (knowledge/skill/skip), fragments (数组)\n"
                f"  3. 每个 fragment 包含：form, title, frontmatter, background, core_content, boundaries, anti_patterns, related_concepts\n"
                f"  4. 完整 prompt 在 SQLite payload.meta.full_prompt 中\n"
                f"-->\n"
                f"<!-- OpenClaw 请处理 SQLite 中的任务并生成蒸馏结果 -->\n"
            )
            output_path.write_text(placeholder, encoding="utf-8")
            return True
        except Exception as e:
            logger.warning(f"委托 OpenClaw 蒸馏失败: {e}")
            return False

    def _build_distill_prompt(self, task: Dict) -> str:
        meta = task.get("meta", {})
        if meta.get("full_prompt"):
            return meta["full_prompt"]
        try:
            from core.hephaestus.distillation_prompts import DISTILLATION_PROMPT
            from core.hephaestus.distillation_engine import build_session_text

            messages = task.get("messages", [])
            session_text = build_session_text(messages)
            if not session_text:
                return self._build_fallback_prompt(task)
            return DISTILLATION_PROMPT.replace("{session_content}", session_text)
        except Exception as e:
            logger.warning(f"构建完整蒸馏 prompt 失败，使用回退: {e}")
            return self._build_fallback_prompt(task)

    def _build_fallback_prompt(self, task: Dict) -> str:
        meta = task.get("meta", {})
        messages = task.get("messages", [])
        lines = [
            "# Mnemos 蒸馏任务",
            "",
            f"**Session ID**: {task.get('session_id', 'unknown')}",
            f"**来源**: {meta.get('source', 'unknown')}",
            f"**工作目录**: {meta.get('working_dir', '')}",
            "",
            "## 指令",
            "请对以下对话进行蒸馏，提取核心知识、经验教训和可复用的模式。",
            "输出严格 JSON 格式（见 distillation_prompts.py 完整要求）。",
            "",
            "## 原始对话",
            "",
        ]
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            lines.append(f"### {role}")
            lines.append(content)
            lines.append("")
        return "\n".join(lines)


AgentRegistry.register(OpenClawAdapter)
