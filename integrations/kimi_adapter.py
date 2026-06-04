# Kimi Adapter — Kimi Code CLI 适配器
# 基于文件系统轮询：读取 ~/.kimi/sessions/ 下的 JSONL 会话文件

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from integrations.active import (
    generated_wrapper,
    json_mcp_configured,
    kimi_hooks_configured,
    upsert_json_mcp_server,
    upsert_kimi_hooks,
    wrapper_uses_active_bridge,
)
from integrations.olympus import AgentAdapter, AgentRegistry

logger = logging.getLogger(__name__)


class KimiAdapter(AgentAdapter):
    """Kimi Code CLI 适配器

    Kimi 采用 JSON Lines 格式存储会话：
    - ~/.kimi/sessions/{workspace_id}/{session_id}/context_*.jsonl
    - 每行一个 JSON 对象，包含 role 和 content
    """

    @property
    def name(self) -> str:
        return "kimi"

    @property
    def priority(self) -> int:
        return 6

    def is_available(self) -> bool:
        """检测 Kimi 是否安装"""
        return self.get_data_dir().exists()

    def get_data_dir(self) -> Optional[Path]:
        return Path.home() / ".kimi"

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
            logger.warning("Caught unexpected error at kimi_adapter.py", exc_info=True)
        return {"agent": self.name, "knowledge": knowledge}

    def on_session_end(self, working_dir: str, session_messages: List[Dict] = None) -> Dict:
        sid = None
        if session_messages:
            try:
                from core.kia.amphora import enqueue
                wd = working_dir or __import__("os").getcwd()
                dir_hash = __import__("hashlib").md5(wd.encode()).hexdigest()[:8]
                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                sid = f"kimi:{dir_hash}:{ts}"
                enqueue(
                    session_id=sid,
                    messages=session_messages,
                    meta={"source": "kimi", "working_dir": wd}
                )
            except Exception as e:
                logger.warning(f"Kimi 蒸馏入队失败: {e}")
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
            logger.warning("Caught unexpected error at kimi_adapter.py", exc_info=True)
        return {"saved": True, "distill_task_id": sid}

    def is_hooks_installed(self) -> bool:
        """检查 Kimi config.toml 中是否已注册 Mnemos hooks"""
        config_path = self.get_data_dir() / "config.toml"
        wrapper_path = self.get_data_dir() / "mnemos_wrapper.py"
        return (
            kimi_hooks_configured(config_path, wrapper_path)
            and wrapper_uses_active_bridge(wrapper_path)
        )

    def is_mcp_configured(self) -> bool:
        return json_mcp_configured(self.get_data_dir() / "mcp.json")

    def install_mcp_server(self) -> bool:
        return upsert_json_mcp_server(self.get_data_dir() / "mcp.json")

    def install_hooks(self) -> bool:
        """在 Kimi config.toml 中注册 session hooks"""
        try:
            config_path = self.get_data_dir() / "config.toml"
            if not config_path.exists():
                config_path.parent.mkdir(parents=True, exist_ok=True)
                config_path.write_text("", encoding="utf-8")

            wrapper_path = self.get_data_dir() / "mnemos_wrapper.py"

            # 生成 wrapper 脚本
            wrapper_path.write_text(generated_wrapper(self.name), encoding="utf-8")
            upsert_kimi_hooks(config_path, wrapper_path)
            self.install_mcp_server()

            logger.info(f"[KimiAdapter] Hooks 已安装到 {config_path}")
            return True
        except Exception as e:
            logger.warning(f"[KimiAdapter] 安装 hooks 失败: {e}")
            return False

    def _generate_wrapper_script(self) -> str:
        return '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mnemos-Kimi Wrapper"""
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
        bus.publish("session.start", "kimi", {
            "working_dir": args.working_dir,
            "user_message": args.user_message,
        })
    elif args.session_end:
        bus.publish("session.end", "kimi", {
            "working_dir": args.working_dir,
            "messages": [],
            "meta": {"source": "kimi", "working_dir": args.working_dir},
        })

if __name__ == "__main__":
    main()
'''

    def collect_signals(self, days: int = 7) -> List[Dict]:
        """从 Kimi sessions 目录采集信号"""
        signals = []
        sessions_dir = self.get_data_dir() / "sessions"
        if not sessions_dir.exists():
            return signals

        cutoff = datetime.now().timestamp() - days * 86400
        for workspace_dir in sessions_dir.iterdir():
            if not workspace_dir.is_dir():
                continue
            for session_dir in workspace_dir.iterdir():
                if not session_dir.is_dir():
                    continue
                # 检查是否有最近的 context 文件
                context_files = sorted(session_dir.glob("context_*.jsonl"))
                if not context_files:
                    continue
                latest = max(context_files, key=lambda p: p.stat().st_mtime)
                if latest.stat().st_mtime < cutoff:
                    continue

                try:
                    messages = []
                    with open(latest, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            msg = json.loads(line)
                            # 过滤系统提示
                            role = msg.get("role", "")
                            if role.startswith("_"):
                                continue
                            messages.append({
                                "role": role,
                                "content": msg.get("content", ""),
                            })
                    if messages:
                        signals.append({
                            "source": "kimi",
                            "session_id": session_dir.name,
                            "workspace": workspace_dir.name,
                            "timestamp": datetime.fromtimestamp(latest.stat().st_mtime).isoformat(),
                            "messages": messages,
                            "file": str(latest),
                        })
                except Exception as e:
                    logger.debug(f"读取 Kimi session 失败 {latest}: {e}")

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
        """委托 Kimi 执行蒸馏任务

        策略：将任务写入 ~/.kimi/inbox/ 目录，Kimi 可读取并处理。
        """
        try:
            inbox_dir = self.get_data_dir() / "inbox"
            inbox_dir.mkdir(parents=True, exist_ok=True)

            task = json.loads(task_path.read_text(encoding="utf-8"))
            task_file = inbox_dir / f"mnemos_distill_{task.get('session_id', 'unknown')}.json"
            task_file.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")

            # 写入输出占位符
            output_path.parent.mkdir(parents=True, exist_ok=True)
            placeholder = (
                f"<!-- MNEMOS_DISTILL_TASK: {task.get('session_id')} -->\n"
                f"<!-- 请将蒸馏结果写入此文件 -->\n"
            )
            output_path.write_text(placeholder, encoding="utf-8")
            return True
        except Exception as e:
            logger.warning(f"委托 Kimi 蒸馏失败: {e}")
            return False


AgentRegistry.register(KimiAdapter)
