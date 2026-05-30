# Musae — 缪斯们，艺术与科学的九位女神
# OpenCode 适配器 — 代码艺术，通过 MCP 协议接入

import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional

from integrations.olympus import AgentAdapter, AgentRegistry



logger = logging.getLogger(__name__)
class OpenCodeAdapter(AgentAdapter):
    """OpenCode Agent 适配器

    OpenCode 原生支持 MCP 协议：
    - Mnemos MCP 服务器（agora.py）直接向 OpenCode 暴露工具
    - 蒸馏通过 MCP `tools/call` 调用 `hephaestus_worker`
    - 此适配器主要负责配置管理和信号采集
    """

    @property
    def name(self) -> str:
        return "opencode"

    @property
    def priority(self) -> int:
        return 5

    def is_available(self) -> bool:
        """检测 OpenCode 是否安装"""
        # 检测 OpenCode 配置目录或命令
        opencode_dir = Path.home() / ".opencode"
        if opencode_dir.exists():
            return True
        try:
            import subprocess
            result = subprocess.run(
                ["opencode", "--version"],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def get_data_dir(self) -> Optional[Path]:
        return Path.home() / ".opencode"

    def on_session_start(self, working_dir: str, user_message: str = "") -> Dict:
        knowledge = self.inject_knowledge("coding", context_text=user_message)
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
                sid = f"opencode:{dir_hash}:{ts}"
                enqueue(
                    session_id=sid,
                    messages=session_messages,
                    meta={"source": "opencode", "working_dir": wd}
                )
            except Exception as e:
                logger.warning(f"OpenCode 蒸馏入队失败: {e}")
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

    def is_hooks_installed(self) -> bool:
        """检查 OpenCode settings.json 中是否已安装 Mnemos hooks"""
        data_dir = self.get_data_dir()
        if not data_dir:
            return False
        settings_path = data_dir / "settings.json"
        wrapper_path = data_dir / "mnemos_wrapper.py"
        if not settings_path.exists() or not wrapper_path.exists():
            return False
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            hooks = settings.get("hooks", {})
            wrapper_str = str(wrapper_path)
            return wrapper_str in hooks.get("session_start", "") and wrapper_str in hooks.get("session_end", "")
        except Exception:
            return False

    def install_hooks(self) -> bool:
        """安装 OpenCode 的 session hooks

        写入 ~/.opencode/settings.json，添加 hooks 字段。
        """
        try:
            data_dir = self.get_data_dir()
            data_dir.mkdir(parents=True, exist_ok=True)
            settings_path = data_dir / "settings.json"

            # 1. 读取现有配置
            settings = {}
            if settings_path.exists():
                try:
                    settings = json.loads(settings_path.read_text(encoding="utf-8"))
                except Exception:
                    logging.getLogger(__name__).warning(f"Caught unexpected error at musae.py", exc_info=True)
                    settings = {}

            # 2. 生成 wrapper 脚本
            wrapper_path = data_dir / "mnemos_wrapper.py"
            wrapper_path.write_text(self._generate_wrapper_script(), encoding="utf-8")

            # 3. 写入 hooks 配置
            if "hooks" not in settings:
                settings["hooks"] = {}
            python_cmd = sys.executable
            settings["hooks"]["session_start"] = (
                f'{python_cmd} {wrapper_path} --session-start '
                f'--working-dir "$PWD" --user-message "$USER_MESSAGE"'
            )
            settings["hooks"]["session_end"] = (
                f'{python_cmd} {wrapper_path} --session-end '
                f'--working-dir "$PWD"'
            )

            # 4. 同时写入 MCP 配置
            if "mcpServers" not in settings:
                settings["mcpServers"] = {}
            settings["mcpServers"]["mnemos"] = {
                "command": python_cmd,
                "args": [str(Path(__file__).parent / "memos_mcp_server.py")]
            }

            settings_path.write_text(
                json.dumps(settings, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )

            logger.info(f"[Musae] OpenCode hooks 已安装到 {settings_path}")
            return True
        except Exception as e:
            logger.warning(f"[Musae] 安装 OpenCode hooks 失败: {e}")
            return False

    def _generate_wrapper_script(self) -> str:
        """生成 OpenCode wrapper 脚本"""
        return '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemos-OpenCode Wrapper
接收 OpenCode 的 session 事件，写入 Mnemos 事件总线
"""

import sys
import os
import argparse
from pathlib import Path

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
        bus.publish("session.start", "opencode", {
            "working_dir": args.working_dir,
            "user_message": args.user_message,
        })
        print("[Mnemos] session.start 事件已发布")
    elif args.session_end:
        bus.publish("session.end", "opencode", {
            "working_dir": args.working_dir,
            "messages": [],
            "meta": {"source": "opencode", "working_dir": args.working_dir},
        })
        print("[Mnemos] session.end 事件已发布")


if __name__ == "__main__":
    main()
'''

    def collect_signals(self, days: int = 7) -> List[Dict]:
        """OpenCode 信号采集 — 从蒸馏归档中过滤 opencode 记录"""
        signals = []
        from datetime import datetime, timedelta
        from core.config import get_config

        archive_dir = get_config().data_dir / "distill_archive"
        if not archive_dir.exists():
            return signals

        cutoff = datetime.now() - timedelta(days=days)
        for json_file in sorted(archive_dir.glob("*.json"), reverse=True):
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                meta = data.get("meta", {})
                if meta.get("source") != "opencode":
                    continue
                ts_str = meta.get("timestamp", "")
                if ts_str:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if ts < cutoff:
                        break

                messages = data.get("messages", [])
                user_msgs = [m for m in messages if m.get("role") == "user"]
                avg_len = sum(len(m.get("content", "")) for m in user_msgs) / max(len(user_msgs), 1) if user_msgs else 0

                signals.append({
                    "session_id": data.get("session_id", ""),
                    "timestamp": ts_str or datetime.now().isoformat(),
                    "task_type": meta.get("source", "opencode"),
                    "user_msg_count": len(user_msgs),
                    "avg_user_msg_length": avg_len,
                    "working_dir": meta.get("working_dir", ""),
                })
            except Exception as e:
                logger.warning(f"解析 OpenCode 归档失败 {json_file}: {e}")
                continue

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
        """委托 OpenCode 执行蒸馏

        OpenCode 原生支持 MCP，蒸馏任务通过 MCP 工具调用完成。
        此适配器将任务写入 OpenCode 的观测目录，由 MCP handler 处理。
        同时写入通知标记文件。
        """
        try:
            task = json.loads(task_path.read_text(encoding="utf-8"))
            task_dir = self.get_data_dir() / "mnemos_tasks"
            task_dir.mkdir(parents=True, exist_ok=True)
            task_file = task_dir / f"{task.get('session_id', 'unknown')}.json"
            task["output_path"] = str(output_path)
            task["type"] = "distillation"
            task_file.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
            # 写入通知标记
            notify_file = task_dir / ".mnemos_notify"
            from datetime import datetime
            notify_content = f"""# Mnemos 蒸馏任务通知

**Agent**: opencode
**时间**: {datetime.now().isoformat()}
**待处理任务**: {task.get('session_id', 'unknown')}
**任务文件**: {task_file}
**输出路径**: {output_path}

OpenCode 请检查 mnemos_tasks 目录处理此任务。
"""
            notify_file.write_text(notify_content, encoding="utf-8")
            # 构建完整 prompt 文件（供 OpenCode 直接阅读）
            prompt_content = self._build_distill_prompt(task)
            prompt_file = task_dir / f"mnemos_prompt_{task.get('session_id', 'unknown')}.md"
            prompt_file.write_text(prompt_content, encoding="utf-8")

            # 写入输出占位符（Agent 会覆盖）
            output_path.parent.mkdir(parents=True, exist_ok=True)
            placeholder = (
                f"<!-- MNEMOS_DISTILL_TASK: {task.get('session_id')} -->\n"
                f"<!-- 输出格式要求：\n"
                f"  1. 必须是有效的 JSON 对象\n"
                f"  2. 顶层字段：judgment (knowledge/skill/skip), fragments (数组)\n"
                f"  3. 每个 fragment 包含：form, title, frontmatter, background, core_content, boundaries, anti_patterns, related_concepts\n"
                f"  4. 完整 prompt 和格式要求见：{prompt_file}\n"
                f"-->\n"
                f"<!-- OpenCode 请阅读 {prompt_file} 并生成蒸馏结果 -->\n"
            )
            output_path.write_text(placeholder, encoding="utf-8")
            return True
        except Exception as e:
            logger.warning(f"委托 OpenCode 蒸馏失败: {e}")
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


AgentRegistry.register(OpenCodeAdapter)
