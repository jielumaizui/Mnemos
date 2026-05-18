# Daedalus — 代达罗斯，巧匠
# Codex CLI 适配器 — 代码工匠的 Mnemos 集成

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

from integrations.olympus import AgentAdapter, AgentRegistry

logger = logging.getLogger(__name__)


class CodexAdapter(AgentAdapter):
    """Codex CLI Agent 适配器

    Codex 没有官方 hook 机制，采用 Shell Wrapper 方案：
    - `mnemos-codex` 命令包装 `codex`，在前后注入 Mnemos 逻辑
    - 蒸馏通过写入任务文件到 ~/.codex/mnemos/ 等待处理
    """

    @property
    def name(self) -> str:
        return "codex"

    @property
    def priority(self) -> int:
        return 4

    def is_available(self) -> bool:
        """检测 Codex CLI 是否安装"""
        try:
            result = subprocess.run(
                ["codex", "--version"],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def get_data_dir(self) -> Optional[Path]:
        return Path.home() / ".codex"

    def on_session_start(self, working_dir: str, user_message: str = "") -> Dict:
        """Codex 会话开始时，注入 KIA 知识"""
        knowledge = self.inject_knowledge("coding", context_text=user_message)
        return {"agent": "codex", "knowledge": knowledge}

    def on_session_end(self, working_dir: str, session_messages: List[Dict] = None) -> Dict:
        """Codex 会话结束时，保存上下文并入队蒸馏"""
        sid = None
        if session_messages:
            try:
                from core.kia.amphora import enqueue
                wd = working_dir or os.getcwd()
                dir_hash = __import__('hashlib').md5(wd.encode()).hexdigest()[:8]
                from datetime import datetime
                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                sid = f"codex:{dir_hash}:{ts}"
                enqueue(
                    session_id=sid,
                    messages=session_messages,
                    meta={
                        "source": "codex",
                        "working_dir": wd,
                    }
                )
            except Exception as e:
                logger.warning(f"Codex 蒸馏入队失败: {e}")

        return {
            "saved": True,
            "distill_task_id": sid,
        }

    def install_hooks(self) -> bool:
        """安装 mnemos-codex wrapper 到 PATH

        由于 Codex 没有原生 hook，我们提供 shell wrapper 脚本。
        用户需要手动将 wrapper 加入 PATH，或使用 `pip install mnemos` 时自动安装。
        """
        wrapper_path = Path(__file__).parent.parent / "bin" / "mnemos-codex"
        if not wrapper_path.exists():
            logger.warning(f"mnemos-codex wrapper 不存在: {wrapper_path}")
            return False
        print(f"请确保 {wrapper_path} 在您的 PATH 中")
        print("用法: mnemos-codex [原 codex 参数]")
        return True

    def collect_signals(self, days: int = 7) -> List[Dict]:
        """从 Codex 历史采集信号（待实现）"""
        # Codex CLI 目前不保存持久化历史，信号采集有限
        return []

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
        """委托 Codex 执行蒸馏

        策略：将任务写入 ~/.codex/mnemos_distill_tasks/，
        当用户下次运行 mnemos-codex 时，wrapper 会检测并执行蒸馏。
        """
        try:
            task = json.loads(task_path.read_text(encoding="utf-8"))
            task_dir = self.get_data_dir() / "mnemos_distill_tasks"
            task_dir.mkdir(parents=True, exist_ok=True)
            task_file = task_dir / f"{task.get('session_id', 'unknown')}.json"
            task["output_path"] = str(output_path)
            task_file.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except Exception as e:
            logger.warning(f"委托 Codex 蒸馏失败: {e}")
            return False


AgentRegistry.register(CodexAdapter)
