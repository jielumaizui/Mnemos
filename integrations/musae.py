# Musae — 缪斯们，艺术与科学的九位女神
# OpenCode 适配器 — 代码艺术，通过 MCP 协议接入

import json
import logging
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
        return {"agent": "opencode", "knowledge": knowledge}

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
        return {"saved": True, "distill_task_id": sid}

    def install_hooks(self) -> bool:
        """OpenCode 通过 MCP 配置接入，无需传统 hook"""
        config = {
            "mcpServers": {
                "mnemos": {
                    "command": "python3",
                    "args": [
                        str(Path(__file__).parent / "agora.py")
                    ]
                }
            }
        }
        print("请在 OpenCode 配置中添加 MCP 服务器：")
        print(json.dumps(config, indent=2, ensure_ascii=False))
        return True

    def collect_signals(self, days: int = 7) -> List[Dict]:
        """OpenCode 信号采集（待实现，需 OpenCode 暴露历史接口）"""
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
        """委托 OpenCode 执行蒸馏

        OpenCode 原生支持 MCP，蒸馏任务通过 MCP 工具调用完成。
        此适配器将任务写入 OpenCode 的观测目录，由 MCP handler 处理。
        """
        try:
            task = json.loads(task_path.read_text(encoding="utf-8"))
            task_dir = self.get_data_dir() / "mnemos_tasks"
            task_dir.mkdir(parents=True, exist_ok=True)
            task_file = task_dir / f"{task.get('session_id', 'unknown')}.json"
            task["output_path"] = str(output_path)
            task["type"] = "distillation"
            task_file.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except Exception as e:
            logger.warning(f"委托 OpenCode 蒸馏失败: {e}")
            return False


AgentRegistry.register(OpenCodeAdapter)
