# Hephaestus Worker — 赫菲斯托斯之工坊
# 蒸馏 Worker — 自动处理 distill_queue，委托 Agent 执行蒸馏

"""
职责：
- 轮询 ~/.claude/distill_queue/ 中的待蒸馏任务
- 调用 AgentDelegate 将任务委托给可用 Agent
- 监控结果路径，处理完成的蒸馏输出
- 将结果移入 00-Inbox/，原文件归档

设计原则：同源复用 — 谁启动 Mnemos，蒸馏就交给谁。
"""

import json
import logging
import shutil
from pathlib import Path
from typing import List, Optional

from core.config import get_config
from core.prometheus_fire import AgentDelegate, DistillTask
from core.helios import AgentDetector

logger = logging.getLogger(__name__)


class HephaestusWorker:
    """蒸馏 Worker — 火神工坊

    自动处理 distill_queue，将原始对话蒸馏为结构化知识。
    """

    def __init__(
        self,
        queue_dir: Path = None,
        output_dir: Path = None,
        inbox_dir: Path = None,
        archive_dir: Path = None,
    ):
        self.delegate = AgentDelegate()
        self.config = get_config()
        self._queue_dir = queue_dir
        self._output_dir = output_dir
        self._inbox_dir = inbox_dir
        self._archive_dir = archive_dir

    @property
    def queue_dir(self) -> Path:
        """蒸馏队列目录"""
        if self._queue_dir:
            return self._queue_dir
        return Path.home() / ".claude" / "distill_queue"

    @property
    def output_dir(self) -> Path:
        """蒸馏输出目录（Agent 将结果写入这里）"""
        if self._output_dir:
            return self._output_dir
        return Path.home() / ".mnemos" / "distill_output"

    @property
    def inbox_dir(self) -> Path:
        """Wiki Inbox 目录"""
        if self._inbox_dir:
            return self._inbox_dir
        return self.config.wiki_dir / "00-Inbox"

    @property
    def archive_dir(self) -> Path:
        """已处理队列文件归档目录"""
        if self._archive_dir:
            return self._archive_dir
        return Path.home() / ".mnemos" / "distill_archive"

    def process_all(self) -> int:
        """处理队列中所有待蒸馏任务

        Returns:
            处理的任务数量
        """
        if not self.queue_dir.exists():
            logger.debug(f"蒸馏队列不存在: {self.queue_dir}")
            return 0

        task_files = sorted(self.queue_dir.glob("*.json"))
        processed = 0

        for task_file in task_files:
            try:
                if self.process_one_file(task_file):
                    processed += 1
            except Exception as e:
                logger.warning(f"处理蒸馏任务失败 {task_file.name}: {e}")
                continue

        return processed

    def process_one(self, session_id: str) -> bool:
        """处理指定 session_id 的蒸馏任务"""
        task_file = self.queue_dir / f"{session_id}.json"
        if not task_file.exists():
            logger.warning(f"任务不存在: {task_file}")
            return False
        return self.process_one_file(task_file)

    def process_one_file(self, task_file: Path) -> bool:
        """处理单个蒸馏任务文件"""
        # 读取任务
        try:
            data = json.loads(task_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"读取任务文件失败 {task_file}: {e}")
            return False

        session_id = data.get("session_id")
        if not session_id:
            logger.warning(f"任务缺少 session_id: {task_file}")
            return False

        # 检查是否已有输出
        output_path = self.output_dir / f"{session_id}.md"
        if output_path.exists() and output_path.stat().st_size > 100:
            logger.info(f"蒸馏结果已存在，直接处理: {session_id}")
            self._move_to_inbox(output_path, session_id, data)
            self._archive_task(task_file)
            return True

        # 构建任务
        task = DistillTask(
            session_id=session_id,
            messages=data.get("messages", []),
            meta=data.get("meta", {}),
        )

        # 委托给 Agent
        self.output_dir.mkdir(parents=True, exist_ok=True)
        ok = self.delegate.delegate(task, output_path)
        if not ok:
            logger.warning(f"无可用的 Agent 执行蒸馏，任务保留: {session_id}")
            return False

        # 不阻塞等待 Agent 完成（异步模式）
        # 结果将在下次 process_all() 或 daemon 轮询时处理
        logger.info(f"蒸馏任务已委托，等待 Agent 完成: {session_id}")

        # 将任务文件标记为"已委托"（重命名）
        delegated_file = task_file.with_suffix(".delegated")
        task_file.rename(delegated_file)

        return True

    def collect_completed(self) -> int:
        """收集已完成的蒸馏结果并移入 Inbox

        Returns:
            收集的任务数量
        """
        if not self.output_dir.exists():
            return 0

        collected = 0
        for output_file in self.output_dir.glob("*.md"):
            session_id = output_file.stem

            # 检查对应的任务文件是否存在（.json 或 .delegated）
            task_file = self.queue_dir / f"{session_id}.json"
            delegated_file = self.queue_dir / f"{session_id}.delegated"
            task_data = None

            if task_file.exists():
                try:
                    task_data = json.loads(task_file.read_text(encoding="utf-8"))
                except Exception:
                    pass
            elif delegated_file.exists():
                try:
                    task_data = json.loads(delegated_file.read_text(encoding="utf-8"))
                except Exception:
                    pass

            # 检查输出是否已完成（不是占位符）
            content = output_file.read_text(encoding="utf-8")
            if "MNEMOS_DISTILL_TASK" in content and len(content) < 200:
                continue  # Agent 尚未覆盖占位符

            # 移入 Inbox
            self._move_to_inbox(output_file, session_id, task_data or {})

            # 归档任务文件
            if task_file.exists():
                self._archive_task(task_file)
            if delegated_file.exists():
                self._archive_task(delegated_file)

            collected += 1

        return collected

    def _move_to_inbox(self, output_path: Path, session_id: str, task_data: dict):
        """将蒸馏结果移入 Wiki Inbox"""
        self.inbox_dir.mkdir(parents=True, exist_ok=True)

        # 读取内容
        content = output_path.read_text(encoding="utf-8")

        # 构建 Inbox 文件名
        source = task_data.get("meta", {}).get("source", "unknown")
        ts = task_data.get("meta", {}).get("timestamp", "")
        if not ts:
            from datetime import datetime
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")

        inbox_name = f"{ts}-{source}-{session_id[:8]}.md"
        inbox_path = self.inbox_dir / inbox_name

        # 添加 frontmatter
        meta = task_data.get("meta", {})
        fm = f"""---
session_id: {session_id}
source: {meta.get('source', 'unknown')}
working_dir: {meta.get('working_dir', '')}
distilled_at: {ts}
tags: [distilled, {meta.get('source', 'unknown')}]
---

"""
        full_content = fm + content
        inbox_path.write_text(full_content, encoding="utf-8")

        logger.info(f"蒸馏结果已移入 Inbox: {inbox_path}")

        # 删除输出文件
        output_path.unlink()

    def _archive_task(self, task_file: Path):
        """归档已处理的任务文件"""
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = self.archive_dir / task_file.name
        shutil.move(str(task_file), str(archive_path))

    def get_pending_count(self) -> int:
        """获取待处理任务数量"""
        if not self.queue_dir.exists():
            return 0
        return len(list(self.queue_dir.glob("*.json")))

    def get_delegated_count(self) -> int:
        """获取已委托但未完成的任务数量"""
        if not self.queue_dir.exists():
            return 0
        return len(list(self.queue_dir.glob("*.delegated")))

    def get_stats(self) -> dict:
        """获取 Worker 统计信息"""
        return {
            "pending": self.get_pending_count(),
            "delegated": self.get_delegated_count(),
            "queue_dir": str(self.queue_dir),
            "output_dir": str(self.output_dir),
            "inbox_dir": str(self.inbox_dir),
            "archive_dir": str(self.archive_dir),
        }
