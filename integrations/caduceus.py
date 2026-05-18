# Caduceus — 双蛇杖，赫尔墨斯的信使工具
# Hermes Agent 适配器 — 轮询采集与任务委托

import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

from integrations.olympus import AgentAdapter, AgentRegistry

logger = logging.getLogger(__name__)


class HermesAdapter(AgentAdapter):
    """Hermes Agent 适配器

    Hermes 采用 Poll 机制：
    - Mnemos 轮询 Hermes 输出目录 ~/.hermes/sessions/ 采集信号
    - 蒸馏任务写入 ~/.hermes/inbox/ 等待 Hermes 处理
    """

    @property
    def name(self) -> str:
        return "hermes"

    @property
    def priority(self) -> int:
        return 2

    def is_available(self) -> bool:
        """检测 Hermes 是否安装"""
        hermes_dir = Path.home() / ".hermes"
        return hermes_dir.exists()

    def get_data_dir(self) -> Optional[Path]:
        return Path.home() / ".hermes"

    def on_session_start(self, working_dir: str, user_message: str = "") -> Dict:
        knowledge = self.inject_knowledge("general", context_text=user_message)
        return {"agent": "hermes", "knowledge": knowledge}

    def on_session_end(self, working_dir: str, session_messages: List[Dict] = None) -> Dict:
        sid = None
        if session_messages:
            try:
                from core.kia.amphora import enqueue
                from datetime import datetime
                wd = working_dir or os.getcwd()
                dir_hash = __import__('hashlib').md5(wd.encode()).hexdigest()[:8]
                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                sid = f"hermes:{dir_hash}:{ts}"
                enqueue(
                    session_id=sid,
                    messages=session_messages,
                    meta={"source": "hermes", "working_dir": wd}
                )
            except Exception as e:
                logger.warning(f"Hermes 蒸馏入队失败: {e}")
        return {"saved": True, "distill_task_id": sid}

    def install_hooks(self) -> bool:
        """Hermes 无原生 hook，需在 Hermes 配置中手动添加 Mnemos 回调"""
        print("请在 Hermes 配置中添加 Mnemos 集成：")
        print(f"  信号输出目录: {self.get_data_dir() / 'sessions'}")
        print(f"  任务收件箱: {self.get_data_dir() / 'inbox'}")
        return True

    def collect_signals(self, days: int = 7) -> List[Dict]:
        """轮询 Hermes 输出目录采集信号"""
        signals = []
        sessions_dir = self.get_data_dir() / "sessions"
        if not sessions_dir.exists():
            return signals

        from datetime import datetime, timedelta
        cutoff = datetime.now() - timedelta(days=days)

        for json_file in sorted(sessions_dir.glob("*.json"), reverse=True):
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                ts_str = data.get("timestamp", "")
                if ts_str:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if ts < cutoff:
                        break
                signals.append({
                    "source": "hermes",
                    "session_id": data.get("session_id"),
                    "timestamp": ts_str,
                    "task_type": data.get("task_type", "unknown"),
                    "raw": data,
                })
            except Exception as e:
                logger.warning(f"解析 Hermes 会话失败 {json_file}: {e}")
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
        """委托 Hermes 执行蒸馏

        策略：将任务写入 ~/.hermes/inbox/，Hermes 轮询处理。
        """
        try:
            task = json.loads(task_path.read_text(encoding="utf-8"))
            inbox_dir = self.get_data_dir() / "inbox"
            inbox_dir.mkdir(parents=True, exist_ok=True)
            task_file = inbox_dir / f"mnemos_distill_{task.get('session_id', 'unknown')}.json"
            task["output_path"] = str(output_path)
            task["type"] = "distillation"
            task_file.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except Exception as e:
            logger.warning(f"委托 Hermes 蒸馏失败: {e}")
            return False


AgentRegistry.register(HermesAdapter)
