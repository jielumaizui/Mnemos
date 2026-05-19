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
from datetime import datetime
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
            self._alert_no_agent(task)
            return False

        logger.info(f"委托蒸馏任务 {task.session_id} 给 Agent: {agent.name}")

        # 预构建完整蒸馏 prompt，放入 meta 供所有适配器复用
        # 同源复用：确保每个 Agent 收到完全相同的 DISTILLATION_PROMPT
        if "full_prompt" not in task.meta:
            try:
                full_prompt = self.build_distill_prompt(task)
                task.meta["full_prompt"] = full_prompt
                logger.debug(f"已预构建蒸馏 prompt ({len(full_prompt)} chars)")
            except Exception as e:
                logger.warning(f"预构建蒸馏 prompt 失败，依赖适配器自行构建: {e}")

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

    def _alert_no_agent(self, task: DistillTask):
        """无可用 Agent 时触发告警

        1. 写入告警日志
        2. 生成提醒文件到 distill_queue
        3. 提供 doctor 检测提示
        """
        # 1. 写入告警日志
        alert_dir = Path.home() / ".mnemos" / "alerts"
        alert_dir.mkdir(parents=True, exist_ok=True)
        alert_file = alert_dir / f"no_agent_{datetime.now().strftime('%Y%m%d')}.log"
        alert_entry = (
            f"[{datetime.now().isoformat()}] "
            f"无可用 Agent，蒸馏任务积压: {task.session_id}\n"
            f"  来源: {task.meta.get('source', 'unknown')}\n"
            f"  消息数: {len(task.messages)}\n"
            f"  建议: 运行 `mnemos doctor` 检查 Agent 状态\n"
            f"  或设置环境变量 MNEMOS_HOST_AGENT=claude\n"
        )
        with open(alert_file, "a", encoding="utf-8") as f:
            f.write(alert_entry)

        # 2. 生成提醒文件（Agent 下次启动时会看到）
        reminder_dir = Path.home() / ".mnemos" / "reminders"
        reminder_dir.mkdir(parents=True, exist_ok=True)
        reminder_file = reminder_dir / "agent_needed.md"
        reminder_content = f"""# Mnemos 告警: Agent 不可用

**时间**: {datetime.now().isoformat()}
**状态**: 有 {self._count_pending_tasks()} 个蒸馏任务等待处理，但未检测到可用 Agent。

## 待处理任务

- Session: `{task.session_id}`
- 来源: {task.meta.get('source', 'unknown')}
- 消息数: {len(task.messages)}

## 解决方案

1. 运行诊断：`mnemos doctor`
2. 安装 Agent hooks：`mnemos agent install`
3. 手动设置宿主 Agent：`export MNEMOS_HOST_AGENT=claude`
4. 检查 Claude Code / Cursor 是否已安装

---
此提醒会在 Agent 恢复后自动清除。
"""
        reminder_file.write_text(reminder_content, encoding="utf-8")
        logger.warning(f"Agent 不可用告警已写入: {alert_file}")

    def _count_pending_tasks(self) -> int:
        """统计待处理任务数"""
        queue_dir = Path.home() / ".claude" / "distill_queue"
        if not queue_dir.exists():
            return 0
        return len(list(queue_dir.glob("*.json")))

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

        使用统一的 DISTILLATION_PROMPT 作为单一 truth source，
        确保所有 Agent 收到的蒸馏任务使用完全相同的 prompt。
        """
        try:
            from core.hephaestus.distillation_prompts import DISTILLATION_PROMPT
            from core.hephaestus.distillation_engine import build_session_text

            session_text = build_session_text(task.messages)
            if not session_text:
                # 无有效内容时的回退
                return self._build_fallback_prompt(task)

            prompt = DISTILLATION_PROMPT.replace("{session_id}", task.session_id)
            prompt = prompt.replace("{session_content}", session_text)
            return prompt
        except Exception as e:
            logger.warning(f"构建完整蒸馏 prompt 失败，使用回退: {e}")
            return self._build_fallback_prompt(task)

    def _build_fallback_prompt(self, task: DistillTask) -> str:
        """回退：轻量蒸馏 prompt（当完整 prompt 不可用时）"""
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
            "输出严格 JSON 格式（见 distillation_prompts.py 中的 DISTILLATION_PROMPT 完整要求）。",
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
