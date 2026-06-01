# -*- coding: utf-8 -*-
"""
ForcedRetrospective — 强制复盘决策引擎

实现蓝图 19-自动回顾 §8-§9：
- §8 组合权重决策算法：系统判断"这件事不处理比打断更严重"时强制打开 Obsidian
- §9 用户主动预约：用户说"1天后提醒我复盘"，到点直接打开 Obsidian

两类触发路径：
1. 系统生成提醒 → 组合权重判断（score >= 4 才强制打开，否则对话内轻提醒）
2. 用户主动预约 → 到点直接打开，不走权重
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class RecapTask:
    """复盘待办"""
    task_id: str
    severity: str  # critical / high / medium / low
    topic: str
    source: str  # system / user
    created_at: str
    due_date: Optional[str] = None
    target_page: str = "00-Dashboard.md"
    user_request: str = ""
    age_days: float = 0
    same_type_count: int = 0
    user_promised: bool = False
    current_file: str = ""
    status: str = "pending"


@dataclass
class ForceDecision:
    """强制打开决策结果"""
    should_force_open: bool
    score: int
    reason: str
    channel: str  # "force_open" / "dialog_reminder"


class ForcedRetrospective:
    """强制复盘决策引擎"""

    SCORE_THRESHOLD = 4  # 蓝图 §8.2 阈值

    def __init__(self, db_path: Optional[str] = None):
        if db_path:
            self._db_path = Path(db_path)
        else:
            from core.config import get_config
            self._db_path = get_config().data_dir / "recap_tasks.db"
        self._init_db()

    def _init_db(self):
        """初始化复盘任务表"""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self._db_path), timeout=10) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS recap_tasks (
                    task_id TEXT PRIMARY KEY,
                    severity TEXT NOT NULL DEFAULT 'medium',
                    topic TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'system',
                    created_at TEXT NOT NULL,
                    due_date TEXT,
                    target_page TEXT NOT NULL DEFAULT '00-Dashboard.md',
                    user_request TEXT DEFAULT '',
                    age_days REAL DEFAULT 0,
                    same_type_count INTEGER DEFAULT 0,
                    user_promised INTEGER DEFAULT 0,
                    current_file TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending'
                )
            """)

    # ============================================================
    # §8 组合权重决策算法
    # ============================================================

    def should_force_open(
        self,
        recap: RecapTask,
        user_context: Optional[Dict] = None,
    ) -> ForceDecision:
        """
        组合权重决策：系统判断是否强制打开 Obsidian。

        评分维度（蓝图 §8.1）：
        - 重要性：critical +3, high +2
        - 时间：age >= 7d +2, age >= 3d +1
        - 频率：7天内同类问题 >= 2 次 +2
        - 上下文关联：用户正在修改相关文件 +2
        - 承诺违约：用户说过"稍后复盘"且超 48h +1

        阈值：score >= 4 强制打开，< 4 对话内轻提醒
        """
        user_context = user_context or {}
        score = 0
        reasons = []

        # 重要性
        if recap.severity == "critical":
            score += 3
            reasons.append("severity=critical(+3)")
        elif recap.severity == "high":
            score += 2
            reasons.append("severity=high(+2)")

        # 时间
        if recap.age_days >= 7:
            score += 2
            reasons.append(f"age={recap.age_days:.0f}d(>=7,+2)")
        elif recap.age_days >= 3:
            score += 1
            reasons.append(f"age={recap.age_days:.0f}d(>=3,+1)")

        # 频率
        if recap.same_type_count >= 2:
            score += 2
            reasons.append(f"same_type={recap.same_type_count}(>=2,+2)")

        # 上下文关联
        current_file = user_context.get("current_file", recap.current_file)
        if current_file and self._is_related(recap.topic, current_file):
            score += 2
            reasons.append(f"related_file(+2)")

        # 承诺违约
        if recap.user_promised and recap.age_days >= 2:
            score += 1
            reasons.append("promise_broken(+1)")

        should_open = score >= self.SCORE_THRESHOLD
        channel = "force_open" if should_open else "dialog_reminder"
        reason = "; ".join(reasons) if reasons else "no signals"

        return ForceDecision(
            should_force_open=should_open,
            score=score,
            reason=reason,
            channel=channel,
        )

    def evaluate_and_open(
        self,
        recap: RecapTask,
        user_context: Optional[Dict] = None,
    ) -> ForceDecision:
        """
        评估并执行：如果决策为强制打开，立即调用 open_obsidian()。
        """
        decision = self.should_force_open(recap, user_context)

        if decision.should_force_open:
            from core.app.obsidian_opener import open_obsidian
            success = open_obsidian(page_path=recap.target_page)
            if success:
                logger.info(
                    f"强制打开 Obsidian: {recap.topic} "
                    f"(score={decision.score}, reason={decision.reason})"
                )
                self._update_status(recap.task_id, "reminded")
            else:
                logger.warning(f"强制打开 Obsidian 失败: {recap.topic}")
                # 打开失败，降级为对话提醒
                decision.should_force_open = False
                decision.channel = "dialog_reminder"
        else:
            logger.debug(
                f"对话轻提醒: {recap.topic} "
                f"(score={decision.score}, reason={decision.reason})"
            )

        return decision

    # ============================================================
    # §9 用户主动预约复盘
    # ============================================================

    def schedule_user_reminder(
        self,
        user_request: str,
        due_date: datetime,
        target_page: str = "00-Dashboard.md",
    ) -> str:
        """
        用户主动预约复盘提醒。

        规则（蓝图 §9）：
        - 用户自己约的，到点直接弹开 Obsidian，不走组合权重
        - created_by = "user"
        """
        task_id = f"user_reminder-recap-{due_date.strftime('%Y%m%d%H%M')}"
        now = datetime.now()

        with sqlite3.connect(str(self._db_path), timeout=10) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO recap_tasks
                (task_id, severity, topic, source, created_at,
                 due_date, target_page, user_request, status)
                VALUES (?, 'high', ?, 'user', ?, ?, ?, ?, 'pending')
            """, (
                task_id,
                user_request,
                now.isoformat(),
                due_date.isoformat(),
                target_page,
                user_request,
            ))

        logger.info(f"用户预约复盘: {user_request} → {due_date.isoformat()}")
        return task_id

    def cancel_user_reminder(self, task_id: str) -> bool:
        """取消用户预约"""
        with sqlite3.connect(str(self._db_path), timeout=10) as conn:
            cursor = conn.execute(
                "UPDATE recap_tasks SET status = 'cancelled' "
                "WHERE task_id = ? AND source = 'user'",
                (task_id,),
            )
            return cursor.rowcount > 0

    def reschedule_user_reminder(
        self,
        old_task_id: str,
        new_due_date: datetime,
    ) -> Optional[str]:
        """重新调度用户预约"""
        # 读取原任务
        with sqlite3.connect(str(self._db_path), timeout=10) as conn:
            cursor = conn.execute(
                "SELECT topic, target_page, user_request FROM recap_tasks "
                "WHERE task_id = ? AND source = 'user'",
                (old_task_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None

        # 取消原任务
        self.cancel_user_reminder(old_task_id)
        # 新建
        topic, target_page, user_request = row
        return self.schedule_user_reminder(user_request, new_due_date, target_page)

    def list_user_reminders(self) -> List[RecapTask]:
        """列出所有用户预约"""
        with sqlite3.connect(str(self._db_path), timeout=10) as conn:
            cursor = conn.execute(
                "SELECT task_id, severity, topic, source, created_at, "
                "due_date, target_page, user_request, status "
                "FROM recap_tasks WHERE source = 'user' AND status = 'pending' "
                "ORDER BY due_date ASC"
            )
            return [self._row_to_recap(row) for row in cursor.fetchall()]

    # ============================================================
    # 系统生成复盘待办
    # ============================================================

    def create_system_recap(
        self,
        topic: str,
        severity: str = "medium",
        target_page: str = "00-Dashboard.md",
    ) -> str:
        """系统生成复盘待办"""
        now = datetime.now()
        task_id = f"system-recap-{now.strftime('%Y%m%d%H%M%S')}"

        with sqlite3.connect(str(self._db_path), timeout=10) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO recap_tasks
                (task_id, severity, topic, source, created_at,
                 target_page, status)
                VALUES (?, ?, ?, 'system', ?, ?, 'pending')
            """, (task_id, severity, topic, now.isoformat(), target_page))

        return task_id

    def _create_from_session_end(self, session_id: str, skip_reason: str) -> Optional[str]:
        """当 session 被蒸馏系统跳过时，自动创建系统复盘任务。"""
        if skip_reason not in ("skipped_low_quality", "skipped_by_pipeline"):
            return None
        severity = "medium" if skip_reason == "skipped_low_quality" else "high"
        topic = f"Session {session_id[:8]} 被跳过: {skip_reason}"
        task_id = self.create_system_recap(
            topic=topic,
            severity=severity,
            target_page="00-Dashboard.md",
        )
        logger.info(f"[ForcedRetrospective] Session skip 触发复盘: {topic} -> {task_id}")
        return task_id

    def get_pending_system_recaps(self) -> List[RecapTask]:
        """获取所有待处理的系统复盘待办"""
        now = datetime.now()
        recaps = []

        with sqlite3.connect(str(self._db_path), timeout=10) as conn:
            cursor = conn.execute(
                "SELECT task_id, severity, topic, source, created_at, "
                "due_date, target_page, user_request, status "
                "FROM recap_tasks WHERE source = 'system' AND status = 'pending' "
                "ORDER BY created_at ASC"
            )
            for row in cursor.fetchall():
                recap = self._row_to_recap(row)
                # 计算年龄
                created = datetime.fromisoformat(recap.created_at)
                recap.age_days = (now - created).days
                # 统计同类问题频率
                recap.same_type_count = self._count_same_type(conn, recap.topic)
                recaps.append(recap)

        return recaps

    # ============================================================
    # 启动补偿（蓝图 §9 关键边界）
    # ============================================================

    def startup_compensation(self) -> List[RecapTask]:
        """
        启动补偿：扫描已过期的 user_reminder 任务。

        用户电脑关机/盒盖期间过期的预约，开机后立即补发。
        用户预约：直接打开 Obsidian（不走权重）。
        系统提醒：走组合权重判断。
        """
        now = datetime.now()
        expired = []

        with sqlite3.connect(str(self._db_path), timeout=10) as conn:
            # 过期的用户预约
            cursor = conn.execute(
                "SELECT task_id, severity, topic, source, created_at, "
                "due_date, target_page, user_request, status "
                "FROM recap_tasks "
                "WHERE source = 'user' AND status = 'pending' "
                "AND due_date <= ? "
                "ORDER BY due_date ASC",
                (now.isoformat(),),
            )
            user_expired = [self._row_to_recap(row) for row in cursor.fetchall()]

            # 过期的系统提醒（3天以上未处理）
            three_days_ago = (now - timedelta(days=3)).isoformat()
            cursor = conn.execute(
                "SELECT task_id, severity, topic, source, created_at, "
                "due_date, target_page, user_request, status "
                "FROM recap_tasks "
                "WHERE source = 'system' AND status = 'pending' "
                "AND created_at <= ? "
                "ORDER BY created_at ASC",
                (three_days_ago,),
            )
            system_expired = [self._row_to_recap(row) for row in cursor.fetchall()]

        # 用户预约：直接打开
        for recap in user_expired:
            from core.app.obsidian_opener import open_obsidian
            open_obsidian(page_path=recap.target_page)
            self._update_status(recap.task_id, "reminded")
            logger.info(f"启动补偿 - 用户预约: {recap.topic}")

        expired.extend(user_expired)

        # 系统提醒：走组合权重
        for recap in system_expired:
            created = datetime.fromisoformat(recap.created_at)
            recap.age_days = (now - created).days
            self.evaluate_and_open(recap)
            expired.append(recap)

        return expired

    # ============================================================
    # 定时检查（调度器调用）
    # ============================================================

    def check_due_reminders(self) -> List[ForceDecision]:
        """
        检查到期提醒（由 chronos 调度器定期调用）。

        用户预约到期 → 直接打开 Obsidian
        系统提醒到期 → 走组合权重
        """
        now = datetime.now()
        decisions = []

        with sqlite3.connect(str(self._db_path), timeout=10) as conn:
            # 用户预约到期
            cursor = conn.execute(
                "SELECT task_id, severity, topic, source, created_at, "
                "due_date, target_page, user_request, status "
                "FROM recap_tasks "
                "WHERE source = 'user' AND status = 'pending' "
                "AND due_date <= ?",
                (now.isoformat(),),
            )
            for row in cursor.fetchall():
                recap = self._row_to_recap(row)
                # 用户预约：直接打开
                from core.app.obsidian_opener import open_obsidian
                open_obsidian(page_path=recap.target_page)
                self._update_status(recap.task_id, "reminded")
                decisions.append(ForceDecision(
                    should_force_open=True,
                    score=0,
                    reason="user_scheduled",
                    channel="force_open",
                ))

        # 系统提醒：走组合权重
        for recap in self.get_pending_system_recaps():
            decision = self.evaluate_and_open(recap)
            decisions.append(decision)

        return decisions

    # ============================================================
    # 内部工具
    # ============================================================

    def _is_related(self, topic: str, current_file: str) -> bool:
        """判断复盘主题与当前文件是否相关"""
        if not current_file:
            return False
        topic_lower = topic.lower()
        file_lower = current_file.lower()
        # 提取关键词：中文字符、英文单词
        import re
        keywords = re.findall(r'[一-龥]+|[a-z]{2,}', topic_lower)
        return any(kw in file_lower for kw in keywords)

    def _count_same_type(self, conn: sqlite3.Connection, topic: str) -> int:
        """统计7天内同类问题出现次数"""
        seven_days_ago = (datetime.now() - timedelta(days=7)).isoformat()
        cursor = conn.execute(
            "SELECT COUNT(*) FROM recap_tasks "
            "WHERE topic = ? AND created_at >= ? AND status != 'cancelled'",
            (topic, seven_days_ago),
        )
        return cursor.fetchone()[0]

    def _update_status(self, task_id: str, status: str):
        """更新任务状态"""
        with sqlite3.connect(str(self._db_path), timeout=10) as conn:
            conn.execute(
                "UPDATE recap_tasks SET status = ? WHERE task_id = ?",
                (status, task_id),
            )

    @staticmethod
    def _row_to_recap(row) -> RecapTask:
        return RecapTask(
            task_id=row[0],
            severity=row[1],
            topic=row[2],
            source=row[3],
            created_at=row[4],
            due_date=row[5],
            target_page=row[6],
            user_request=row[7],
            status=row[8],
        )
