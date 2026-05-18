"""
Knowledge Scheduler - 知识调度器

使用 live_sync.db 存储远期/周期性任务：
- 中期任务（8-30天）：提前3天提醒
- 长期任务（>30天）：提前7天提醒
- 周期性任务：按周期自动提醒

启动时补偿扫描，避免漏掉。
"""
# Chronos — 时间之神 — 知识调度器，任务的时间线管理
# 原模块: knowledge_scheduler.py



import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional


@dataclass
class ScheduledTask:
    """调度任务"""
    task_id: str
    task_type: str
    subtype: str
    due_date: str           # ISO format
    reminder_date: str      # 提前提醒日期
    is_periodic: bool
    period: Optional[str]   # weekly/biweekly/monthly/quarterly
    status: str             # pending/reminded/completed/cancelled
    context: str            # 任务上下文摘要
    created_at: str
    reminded_at: Optional[str] = None


class KnowledgeScheduler:
    """知识调度器"""

    def __init__(self, db_path: Optional[str] = None):
        if db_path:
            self.DB_PATH = Path(db_path).expanduser()
        else:
            from core.config import get_config
            self.DB_PATH = get_config().data_dir / "live_sync.db"
        self._init_db()

    def _init_db(self):
        """初始化数据库表"""
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS knowledge_scheduled_tasks (
                    task_id TEXT PRIMARY KEY,
                    task_type TEXT NOT NULL,
                    subtype TEXT NOT NULL,
                    due_date TEXT NOT NULL,
                    reminder_date TEXT NOT NULL,
                    is_periodic INTEGER DEFAULT 0,
                    period TEXT,
                    status TEXT DEFAULT 'pending',
                    context TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    reminded_at TIMESTAMP,
                    completed_at TIMESTAMP
                )
            """)
            conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_kst_status
                ON knowledge_scheduled_tasks(status)
            """)
            conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_kst_reminder
                ON knowledge_scheduled_tasks(reminder_date)
            """)

    def schedule(self, task_type: str, subtype: str,
                 due_date: datetime, context: str = "",
                 is_periodic: bool = False, period: Optional[str] = None) -> str:
        """
        登记任务到调度器

        Args:
            task_type: 任务类型
            subtype: 子类型
            due_date: 执行日期
            context: 任务上下文
            is_periodic: 是否周期性
            period: 周期

        Returns:
            task_id
        """
        task_id = f"{task_type}-{subtype}-{due_date.strftime('%Y%m%d')}"

        # 计算提醒日期
        days_until = (due_date - datetime.now()).days
        if days_until <= 7:
            reminder_days = 1
        elif days_until <= 30:
            reminder_days = 3
        else:
            reminder_days = 7

        reminder_date = due_date - timedelta(days=reminder_days)

        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute(f"""
                INSERT OR REPLACE INTO knowledge_scheduled_tasks
                (task_id, task_type, subtype, due_date, reminder_date,
                 is_periodic, period, status, context, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """, (
                task_id, task_type, subtype,
                due_date.isoformat(), reminder_date.isoformat(),
                1 if is_periodic else 0, period,
                context, datetime.now().isoformat()
            ))

        return task_id

    def get_pending_reminders(self) -> List[ScheduledTask]:
        """
        获取到期的提醒任务

        Returns:
            到期任务列表
        """
        now = datetime.now().isoformat()

        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            cursor = conn.execute(f"""
                SELECT task_id, task_type, subtype, due_date, reminder_date,
                       is_periodic, period, status, context, created_at, reminded_at
                FROM knowledge_scheduled_tasks
                WHERE status = 'pending'
                  AND reminder_date <= ?
                ORDER BY reminder_date ASC
            """, (now,))

            tasks = []
            for row in cursor.fetchall():
                tasks.append(ScheduledTask(
                    task_id=row[0],
                    task_type=row[1],
                    subtype=row[2],
                    due_date=row[3],
                    reminder_date=row[4],
                    is_periodic=bool(row[5]),
                    period=row[6],
                    status=row[7],
                    context=row[8],
                    created_at=row[9],
                    reminded_at=row[10],
                ))

        return tasks

    def mark_reminded(self, task_id: str):
        """标记任务已提醒"""
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute(f"""
                UPDATE knowledge_scheduled_tasks
                SET status = 'reminded', reminded_at = ?
                WHERE task_id = ?
            """, (datetime.now().isoformat(), task_id))

    def mark_completed(self, task_id: str):
        """标记任务已完成"""
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute(f"""
                UPDATE knowledge_scheduled_tasks
                SET status = 'completed', completed_at = ?
                WHERE task_id = ?
            """, (datetime.now().isoformat(), task_id))

    def cancel(self, task_id: str):
        """取消任务"""
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute(f"""
                UPDATE knowledge_scheduled_tasks
                SET status = 'cancelled'
                WHERE task_id = ?
            """, (task_id,))

    def startup_compensation(self) -> List[ScheduledTask]:
        """
        启动补偿扫描
        检查是否有漏掉的提醒（系统关闭期间到期的）

        Returns:
            漏掉的任务列表
        """
        now = datetime.now().isoformat()

        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            # 1. 已经到期但还没提醒的
            cursor = conn.execute(f"""
                SELECT task_id, task_type, subtype, due_date, reminder_date,
                       is_periodic, period, status, context, created_at, reminded_at
                FROM knowledge_scheduled_tasks
                WHERE status = 'pending'
                  AND reminder_date <= ?
                ORDER BY reminder_date ASC
            """, (now,))

            missed = []
            for row in cursor.fetchall():
                missed.append(ScheduledTask(
                    task_id=row[0],
                    task_type=row[1],
                    subtype=row[2],
                    due_date=row[3],
                    reminder_date=row[4],
                    is_periodic=bool(row[5]),
                    period=row[6],
                    status=row[7],
                    context=row[8],
                    created_at=row[9],
                    reminded_at=row[10],
                ))

            # 2. 已经提醒但还没完成的（提醒超过3天）
            three_days_ago = (datetime.now() - timedelta(days=3)).isoformat()
            cursor = conn.execute(f"""
                SELECT task_id, task_type, subtype, due_date, reminder_date,
                       is_periodic, period, status, context, created_at, reminded_at
                FROM knowledge_scheduled_tasks
                WHERE status = 'reminded'
                  AND reminded_at <= ?
                ORDER BY reminded_at ASC
            """, (three_days_ago,))

            for row in cursor.fetchall():
                missed.append(ScheduledTask(
                    task_id=row[0],
                    task_type=row[1],
                    subtype=row[2],
                    due_date=row[3],
                    reminder_date=row[4],
                    is_periodic=bool(row[5]),
                    period=row[6],
                    status=row[7],
                    context=row[8],
                    created_at=row[9],
                    reminded_at=row[10],
                ))

        return missed

    def format_reminder(self, task: ScheduledTask) -> str:
        """格式化提醒消息"""
        due = datetime.fromisoformat(task.due_date.replace('Z', '+00:00'))
        days_until = (due - datetime.now()).days

        lines = [
            f"📅 **任务提醒**",
            f"",
            f"类型：{task.task_type}/{task.subtype}",
            f"执行日期：{task.due_date[:10]}（还有 {days_until} 天）",
        ]

        if task.is_periodic:
            lines.append(f"周期：{task.period}")

        if task.context:
            lines.append(f"上下文：{task.context}")

        lines.append("")
        lines.append("知识库已装载相关经验，请查看。")

        return "\n".join(lines)

    def list_all(self, status: Optional[str] = None) -> List[ScheduledTask]:
        """列出所有任务"""
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            if status:
                cursor = conn.execute(f"""
                    SELECT task_id, task_type, subtype, due_date, reminder_date,
                           is_periodic, period, status, context, created_at, reminded_at
                    FROM knowledge_scheduled_tasks
                    WHERE status = ?
                    ORDER BY due_date ASC
                """, (status,))
            else:
                cursor = conn.execute(f"""
                    SELECT task_id, task_type, subtype, due_date, reminder_date,
                           is_periodic, period, status, context, created_at, reminded_at
                    FROM knowledge_scheduled_tasks
                    ORDER BY due_date ASC
                """)

            tasks = []
            for row in cursor.fetchall():
                tasks.append(ScheduledTask(
                    task_id=row[0],
                    task_type=row[1],
                    subtype=row[2],
                    due_date=row[3],
                    reminder_date=row[4],
                    is_periodic=bool(row[5]),
                    period=row[6],
                    status=row[7],
                    context=row[8],
                    created_at=row[9],
                    reminded_at=row[10],
                ))

        return tasks

    def cleanup_old(self, days: int = 30):
        """清理已完成的旧任务"""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute(f"""
                DELETE FROM knowledge_scheduled_tasks
                WHERE status IN ('completed', 'cancelled')
                  AND completed_at <= ?
            """, (cutoff,))


# ========== 便捷函数 ==========

def schedule_task(task_type: str, subtype: str,
                  due_date: datetime, context: str = "",
                  is_periodic: bool = False, period: Optional[str] = None) -> str:
    """便捷函数：调度任务"""
    scheduler = KnowledgeScheduler()
    return scheduler.schedule(task_type, subtype, due_date, context, is_periodic, period)


def check_reminders() -> List[ScheduledTask]:
    """便捷函数：检查到期提醒"""
    scheduler = KnowledgeScheduler()
    return scheduler.get_pending_reminders()
