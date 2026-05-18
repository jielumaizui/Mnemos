# Prometheus Fire — 普罗米修斯之火
# 点燃 Agent 执行蒸馏 — 任务委托层

"""
职责：
- 将 distill_queue 中的原始对话打包为结构化蒸馏任务
- 通过 Agent 适配器下发任务
- 监控结果路径，等待 Agent 完成

设计原则：Mnemos 本身不直接调用 LLM API，
所有"脑力工作"委托给本地 AI Agent 完成。
"""

import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional

from core.helios import AgentDetector

logger = logging.getLogger(__name__)


class DistillTask:
    """蒸馏任务结构"""

    def __init__(self, session_id: str, messages: List[Dict], meta: Dict):
        self.session_id = session_id
        self.messages = messages
        self.meta = meta

    def to_dict(self) -> Dict:
        return {
            "session_id": self.session_id,
            "messages": self.messages,
            "meta": self.meta,
        }


class AgentDelegate:
    """任务委托层 — 普罗米修斯之火

    将蒸馏任务委托给本地 AI Agent 执行。
    """

    def __init__(self, detector: AgentDetector = None):
        self.detector = detector or AgentDetector()

    def delegate(self, task: DistillTask, output_path: Path) -> bool:
        """委托 Agent 执行蒸馏任务

        Args:
            task: 蒸馏任务
            output_path: Agent 应将结果写入的路径

        Returns:
            是否成功下发任务
        """
        agent = self.detector.select_best()
        if not agent:
            logger.warning("无可用 Agent 执行蒸馏，任务将留在队列中等待")
            return False

        logger.info(f"委托蒸馏任务 {task.session_id} 给 Agent: {agent.name}")

        # 写入任务文件
        task_path = self._write_task_file(task)
        if not task_path:
            return False

        # 下发任务
        ok = agent.delegate_distillation(task_path, output_path)
        if ok:
            logger.info(f"蒸馏任务已下发: {task.session_id} -> {agent.name}")
        else:
            logger.warning(f"蒸馏任务下发失败: {task.session_id}")
        return ok

    def _write_task_file(self, task: DistillTask) -> Optional[Path]:
        """将任务写入临时文件"""
        try:
            task_dir = Path.home() / ".mnemos" / "distill_tasks"
            task_dir.mkdir(parents=True, exist_ok=True)
            task_path = task_dir / f"{task.session_id}.json"
            task_path.write_text(
                json.dumps(task.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return task_path
        except Exception as e:
            logger.warning(f"写入任务文件失败: {e}")
            return None

    def wait_for_result(
        self, output_path: Path, timeout: int = 300, poll_interval: int = 5
    ) -> Optional[str]:
        """等待 Agent 完成蒸馏并返回结果

        Args:
            output_path: Agent 输出文件路径
            timeout: 最大等待时间（秒）
            poll_interval: 轮询间隔（秒）

        Returns:
            蒸馏结果的文本内容，或 None（超时）
        """
        elapsed = 0
        while elapsed < timeout:
            if output_path.exists() and output_path.stat().st_size > 0:
                # 检查文件是否包含 Agent 的完成标记
                content = output_path.read_text(encoding="utf-8")
                if self._is_complete(content):
                    return content
            time.sleep(poll_interval)
            elapsed += poll_interval

        logger.warning(f"等待蒸馏结果超时: {output_path}")
        return None

    def _is_complete(self, content: str) -> bool:
        """判断蒸馏结果是否已完成

        Agent 完成时应在文件末尾添加标记，
        或文件大小超过某个阈值。
        """
        # 简单判断：内容非空且不是占位符
        if not content or content.strip() == "":
            return False
        # 如果内容包含 MNEMOS_DISTILL_TASK 占位符，说明 Agent 尚未覆盖
        if "MNEMOS_DISTILL_TASK" in content and len(content) < 200:
            return False
        return True

    def build_distill_prompt(self, task: DistillTask) -> str:
        """构建蒸馏提示词

        生成给 Agent 的 prompt，指导 Agent 如何蒸馏对话。
        """
        messages = task.messages
        meta = task.meta

        lines = [
            "# Mnemos 蒸馏任务",
            "",
            f"**Session ID**: {task.session_id}",
            f"**来源**: {meta.get('source', 'unknown')}",
            f"**工作目录**: {meta.get('working_dir', '')}",
            "",
            "## 指令",
            "请对以下对话进行蒸馏，提取核心知识、经验教训和可复用的模式。",
            "",
            "### 输出格式要求",
            "1. 核心主题（一句话概括）",
            "2. 关键知识点（ bullet list ）",
            "3. 经验教训（哪些做对了、哪些可以改进）",
            "4. 可复用模式（抽象为通用规则）",
            "5. 相关标签（便于检索）",
            "",
            "请保持输出格式稳定，同一对话多次蒸馏结果应一致。",
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
