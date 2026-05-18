"""
Time Capsule - 知识时间胶囊

基于知识的时效性和创建日期，设置未来回顾提醒：
1. 自动提醒：版本绑定知识到期、上下文相关知识过期
2. 手动设置：用户可为知识设置"半年后回顾""一年后验证"等提醒
3. 周期性回顾：每周/每月生成"到期回顾清单"

设计原则：
- 与免疫系统联动（过期检测）
- 与推送系统联动（到期时主动提醒）
- 用户可控：可调整提醒时间、标记已完成
"""

import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from core.config import get_config
import logging

logger = logging.getLogger(__name__)


@dataclass
class CapsuleReminder:
    """提醒项"""
    capsule_id: int = 0
    page_path: str = ""
    page_title: str = ""
    reminder_type: str = ""       # auto_expiry / auto_version / manual_review / periodic
    scheduled_date: str = ""      # YYYY-MM-DD
    reason: str = ""              # 提醒原因
    status: str = "pending"       # pending / dismissed / completed / snoozed
    created_at: str = ""
    completed_at: str = ""


class TimeCapsule:
    """时间胶囊系统"""

    # 自动提醒规则（基于时效性）
    AUTO_REMINDER_DAYS = {
        "版本绑定": [30, 60, 90],      # 30天、60天、90天提醒检查版本
        "上下文相关": [90, 180],        # 90天、180天提醒验证是否仍然有效
        "稳定": [365],                  # 1年提醒回顾
    }

    def __init__(self, wiki_base: str = None, db_path: str = None):
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else (
            get_config().wiki_dir
        )
        self.inbox = self.wiki_base / "00-Inbox"
        self.db_path = Path(db_path) if db_path else (
            self.wiki_base / ".kg" / "capsule.db"
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        schema = """
        CREATE TABLE IF NOT EXISTS capsules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_path TEXT NOT NULL,
            page_title TEXT,
            reminder_type TEXT NOT NULL,
            scheduled_date TEXT NOT NULL,
            reason TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_capsule_date ON capsules(scheduled_date);
        CREATE INDEX IF NOT EXISTS idx_capsule_status ON capsules(status);
        CREATE INDEX IF NOT EXISTS idx_capsule_page ON capsules(page_path);
        """
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.executescript(schema)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    # ========== 自动提醒生成 ==========

    def scan_for_auto_reminders(self) -> int:
        """
        扫描所有 Wiki 页面，自动生成提醒

        Returns:
            新增的提醒数量
        """
        count = 0
        if not self.inbox.exists():
            return count

        for page in self.inbox.glob("*.md"):
            try:
                content = page.read_text(encoding="utf-8")
                fm = self._extract_frontmatter(content)
                if not fm:
                    continue

                temporal = fm.get("时效性", "")
                created = fm.get("创建日期", "")
                version_tag = fm.get("版本标记", "")

                if temporal in self.AUTO_REMINDER_DAYS and created:
                    for days in self.AUTO_REMINDER_DAYS[temporal]:
                        try:
                            created_date = datetime.strptime(str(created), "%Y-%m-%d")
                            reminder_date = created_date + timedelta(days=days)

                            # 只生成未来的提醒
                            if reminder_date >= datetime.now():
                                reason = f"{temporal} 知识已创建 {days} 天，建议检查是否仍然有效"
                                if temporal == "版本绑定" and version_tag:
                                    reason += f"（版本标记: {version_tag}）"

                                if self._add_reminder(
                                    str(page), page.stem, "auto_expiry",
                                    reminder_date.strftime("%Y-%m-%d"), reason
                                ):
                                    count += 1
                        except ValueError:
                            continue
            except Exception:
                continue

        return count

    def _add_reminder(self, page_path: str, page_title: str,
                      reminder_type: str, scheduled_date: str,
                      reason: str) -> bool:
        """添加提醒（避免重复）"""
        try:
            with self._conn() as conn:
                # 检查是否已存在相同提醒
                existing = conn.execute(
                    """SELECT 1 FROM capsules
                       WHERE page_path=? AND reminder_type=? AND scheduled_date=?
                       AND status='pending'""",
                    (page_path, reminder_type, scheduled_date)
                ).fetchone()

                if existing:
                    return False

                conn.execute(
                    """INSERT INTO capsules
                       (page_path, page_title, reminder_type, scheduled_date, reason)
                       VALUES (?, ?, ?, ?, ?)""",
                    (page_path, page_title, reminder_type, scheduled_date, reason)
                )
                conn.commit()
                return True
        except sqlite3.Error:
            return False

    # ========== 手动提醒 ==========

    def set_manual_reminder(self, page_path: str, days_from_now: int,
                            reason: str = "") -> bool:
        """
        手动设置提醒

        Args:
            page_path: 知识页面路径
            days_from_now: N 天后提醒
            reason: 提醒原因
        """
        if not Path(page_path).exists():
            return False

        scheduled = (datetime.now() + timedelta(days=days_from_now)).strftime("%Y-%m-%d")
        title = Path(page_path).stem

        return self._add_reminder(
            page_path, title, "manual_review", scheduled,
            reason or f"用户设置 {days_from_now} 天后回顾"
        )

    # ========== 查询提醒 ==========

    def get_due_reminders(self, days_ahead: int = 7) -> List[CapsuleReminder]:
        """
        获取即将到期的提醒

        Args:
            days_ahead: 提前 N 天获取

        Returns:
            提醒列表
        """
        until = (datetime.now() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
        today = datetime.now().strftime("%Y-%m-%d")

        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM capsules
                   WHERE scheduled_date <= ? AND scheduled_date >= ?
                   AND status = 'pending'
                   ORDER BY scheduled_date""",
                (until, today)
            ).fetchall()

        return [self._row_to_reminder(row) for row in rows]

    def get_overdue_reminders(self) -> List[CapsuleReminder]:
        """获取已逾期的提醒"""
        today = datetime.now().strftime("%Y-%m-%d")

        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM capsules
                   WHERE scheduled_date < ? AND status = 'pending'
                   ORDER BY scheduled_date""",
                (today,)
            ).fetchall()

        return [self._row_to_reminder(row) for row in rows]

    def get_all_reminders(self, page_path: str = None,
                          status: str = None) -> List[CapsuleReminder]:
        """获取所有提醒"""
        query = "SELECT * FROM capsules WHERE 1=1"
        params = []

        if page_path:
            query += " AND page_path=?"
            params.append(page_path)
        if status:
            query += " AND status=?"
            params.append(status)

        query += " ORDER BY scheduled_date"

        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()

        return [self._row_to_reminder(row) for row in rows]

    # ========== 提醒操作 ==========

    def dismiss_reminder(self, capsule_id: int) -> bool:
        """忽略提醒"""
        return self._update_status(capsule_id, "dismissed")

    def complete_reminder(self, capsule_id: int) -> bool:
        """完成提醒"""
        try:
            with self._conn() as conn:
                conn.execute(
                    """UPDATE capsules
                       SET status='completed', completed_at=?
                       WHERE id=?""",
                    (datetime.now().isoformat()[:19], capsule_id)
                )
                conn.commit()
                return True
        except sqlite3.Error:
            return False

    def snooze_reminder(self, capsule_id: int, days: int = 7) -> bool:
        """推迟提醒"""
        new_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
        try:
            with self._conn() as conn:
                conn.execute(
                    """UPDATE capsules
                       SET scheduled_date=?, status='pending'
                       WHERE id=?""",
                    (new_date, capsule_id)
                )
                conn.commit()
                return True
        except sqlite3.Error:
            return False

    def _update_status(self, capsule_id: int, status: str) -> bool:
        try:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE capsules SET status=? WHERE id=?",
                    (status, capsule_id)
                )
                conn.commit()
                return True
        except sqlite3.Error:
            return False

    # ========== 报告 ==========

    def generate_reminder_report(self) -> str:
        """生成提醒报告"""
        due = self.get_due_reminders(days_ahead=30)
        overdue = self.get_overdue_reminders()

        lines = [
            "# 知识时间胶囊",
            f"生成时间: {datetime.now().strftime('%Y-%m-%d')}",
            "",
            f"**即将到期**: {len(due)} 条",
            f"**已逾期**: {len(overdue)} 条",
            "",
        ]

        if overdue:
            lines.extend(["## 已逾期", ""])
            for r in overdue[:10]:
                lines.append(f"- [ ] **{r.page_title}** ({r.reminder_type})")
                lines.append(f"  应回顾日期: {r.scheduled_date}")
                lines.append(f"  原因: {r.reason}")
                lines.append("")

        if due:
            lines.extend(["## 即将到期", ""])
            for r in due[:10]:
                days = (datetime.strptime(r.scheduled_date, "%Y-%m-%d") - datetime.now()).days
                lines.append(f"- [ ] **{r.page_title}** ({r.reminder_type})")
                lines.append(f"  应回顾日期: {r.scheduled_date}（还有 {days} 天）")
                lines.append(f"  原因: {r.reason}")
                lines.append("")

        if not due and not overdue:
            lines.append("✅ 暂无到期提醒\n")

        return "\n".join(lines)

    # ========== 辅助方法 ==========

    def _row_to_reminder(self, row: sqlite3.Row) -> CapsuleReminder:
        return CapsuleReminder(
            capsule_id=row["id"],
            page_path=row["page_path"],
            page_title=row["page_title"] or "",
            reminder_type=row["reminder_type"],
            scheduled_date=row["scheduled_date"],
            reason=row["reason"] or "",
            status=row["status"],
            created_at=row["created_at"],
            completed_at=row["completed_at"] or "",
        )

    @staticmethod
    def _extract_frontmatter(content: str) -> Optional[Dict]:
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    import yaml
                    return yaml.safe_load(parts[1]) or {}
                except Exception as e:
                    logger.warning(f"忽略异常: {e}")
        return {}


# ========== 便捷函数 ==========

def set_reminder(page_path: str, days: int = 90) -> bool:
    """便捷函数：为页面设置提醒"""
    capsule = TimeCapsule()
    return capsule.set_manual_reminder(page_path, days)


def get_due() -> List[CapsuleReminder]:
    """便捷函数：获取到期提醒"""
    capsule = TimeCapsule()
    return capsule.get_due_reminders()
