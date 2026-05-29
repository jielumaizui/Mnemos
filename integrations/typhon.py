# Typhon — 百头巨龙，盖亚之子
# OpenClaw 适配器 — 多爪并行，JSONL 驱动的 Agent 集成
#
# OpenClaw 数据目录结构：
#   ~/.openclaw/
#     ├── agents/main/sessions/          # 会话 JSONL 文件
#     │   ├── sessions.json              # 会话索引
#     │   ├── {session-id}.jsonl         # 会话消息
#     │   └── {session-id}.trajectory.jsonl  # 运行时轨迹
#     └── openclaw.json                  # 全局配置

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from integrations.olympus import AgentAdapter, AgentRegistry

logger = logging.getLogger(__name__)


class OpenClawAdapter(AgentAdapter):
    """OpenClaw Agent 适配器

    OpenClaw 采用 JSON Lines 格式存储会话：
    - ~/.openclaw/agents/main/sessions/{session-id}.jsonl
    - 每行一个 JSON 对象，包含 type、role、content 等
    """

    @property
    def name(self) -> str:
        return "openclaw"

    @property
    def priority(self) -> int:
        return 3

    def _sessions_dir(self) -> Path:
        """OpenClaw 会话目录"""
        return self.get_data_dir() / "agents" / "main" / "sessions"

    def _sessions_index(self) -> Path:
        """OpenClaw 会话索引文件"""
        return self._sessions_dir() / "sessions.json"

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
            logger.warning("Caught unexpected error at typhon.py", exc_info=True)
        return {"agent": self.name, "knowledge": knowledge}

    def on_session_end(self, working_dir: str, session_messages: List[Dict] = None) -> Dict:
        sid = None
        if session_messages:
            try:
                from core.kia.amphora import enqueue
                wd = working_dir or __import__("os").getcwd()
                dir_hash = __import__("hashlib").md5(wd.encode()).hexdigest()[:8]
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
                "meta": {"source": self.name, "working_dir": working_dir or __import__("os").getcwd()},
            })
        except Exception:
            logger.warning("Caught unexpected error at typhon.py", exc_info=True)
        return {"saved": True, "distill_task_id": sid}

    # ── hooks 安装（保持 SQLite 方式，用于 Mnemos 内部事件通信）──

    def install_hooks(self) -> bool:
        """安装 OpenClaw 的 session hooks"""
        try:
            data_dir = self.get_data_dir()
            data_dir.mkdir(parents=True, exist_ok=True)
            db_path = data_dir / "sessions.db"

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
                now = datetime.now().isoformat()
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

            wrapper_path.write_text(self._generate_wrapper_script(), encoding="utf-8")
            logger.info(f"[Typhon] OpenClaw hooks 已安装到 {db_path}")
            return True
        except Exception as e:
            logger.warning(f"[Typhon] 安装 OpenClaw hooks 失败: {e}")
            return False

    def _generate_wrapper_script(self) -> str:
        return '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mnemos-OpenClaw Wrapper"""
import sys
import os
import argparse
from pathlib import Path

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
    elif args.session_end:
        bus.publish("session.end", "openclaw", {
            "working_dir": args.working_dir,
            "messages": [],
            "meta": {"source": "openclaw", "working_dir": args.working_dir},
        })

if __name__ == "__main__":
    main()
'''

    # ── 信号采集（从 JSONL 读取）──

    def collect_signals(self, days: int = 7) -> List[Dict]:
        """从 OpenClaw JSONL 会话文件采集信号"""
        signals = []
        sessions_dir = self._sessions_dir()
        if not sessions_dir.exists():
            return signals

        cutoff = datetime.now().timestamp() - days * 86400

        for session_file in sessions_dir.glob("*.jsonl"):
            # 跳过 trajectory 和临时文件
            if session_file.name.endswith(".trajectory.jsonl") or session_file.name.endswith(".tmp"):
                continue

            try:
                mtime = session_file.stat().st_mtime
                if mtime < cutoff:
                    continue

                messages = []
                session_meta = {}
                with open(session_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        etype = event.get("type", "")
                        if etype == "session":
                            session_meta = event
                        elif etype == "message":
                            msg = event.get("message", {})
                            role = msg.get("role", "")
                            content = self._extract_content(msg)
                            if role and content:
                                messages.append({
                                    "role": role,
                                    "content": content,
                                    "timestamp": event.get("timestamp", ""),
                                })

                if messages:
                    signals.append({
                        "source": "openclaw",
                        "session_id": session_meta.get("id", session_file.stem),
                        "timestamp": datetime.fromtimestamp(mtime).isoformat(),
                        "messages": messages,
                        "file": str(session_file),
                        "cwd": session_meta.get("cwd", ""),
                    })
            except Exception as e:
                logger.debug(f"读取 OpenClaw session 失败 {session_file}: {e}")

        return signals

    def _extract_content(self, msg: Dict) -> str:
        """从 OpenClaw message 对象提取文本内容"""
        content = msg.get("content", "")
        if isinstance(content, list):
            # OpenClaw content 是数组格式，如 [{"type":"text","text":"..."}]
            texts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    texts.append(item.get("text", ""))
            return "\n".join(texts)
        elif isinstance(content, str):
            return content
        return ""

    # ── 知识注入 ──

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

    # ── 蒸馏委托 ──

    def delegate_distillation(self, task_path: Path, output_path: Path) -> bool:
        """委托 OpenClaw 执行蒸馏

        策略：
        1. 将蒸馏任务写入 ~/.openclaw/inbox/
        2. 同时在 sessions.db 中写入通知标记（供 wrapper 脚本读取）
        3. 生成完整 prompt 供 OpenClaw 处理
        """
        try:
            task = json.loads(task_path.read_text(encoding="utf-8"))

            # 1. 写入 inbox
            inbox_dir = self.get_data_dir() / "inbox"
            inbox_dir.mkdir(parents=True, exist_ok=True)
            task_file = inbox_dir / f"mnemos_distill_{task.get('session_id', 'unknown')}.json"
            task_file.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")

            # 2. 写入通知标记到 sessions.db
            db_path = self.get_data_dir() / "sessions.db"
            if db_path.exists():
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

            # 3. 生成完整 prompt
            prompt_content = self._build_distill_prompt(task)
            prompt_file = inbox_dir / f"mnemos_distill_{task.get('session_id', 'unknown')}.md"
            prompt_file.write_text(prompt_content, encoding="utf-8")

            # 4. 写入输出占位符
            output_path.parent.mkdir(parents=True, exist_ok=True)
            placeholder = (
                f"<!-- MNEMOS_DISTILL_TASK: {task.get('session_id')} -->\n"
                f"<!-- 蒸馏任务已下发到 OpenClaw inbox -->\n"
                f"<!-- 任务文件: {task_file} -->\n"
                f"<!-- Prompt 文件: {prompt_file} -->\n"
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
