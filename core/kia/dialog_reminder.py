# -*- coding: utf-8 -*-
"""
Dialog Reminder — 对话界面提醒系统

解决"在 Obsidian 中生成页面但用户永远不看"的问题。
- DialogReminderQueue: 多渠道、分层级的提醒队列管理
- PageBannerInjector: Wiki 页面横幅注入/移除

设计原则：
- 对话界面推送是唯一可靠的主动触达渠道
- 所有推送必须带交互选项 [选择：xxx]
- 页面横幅是被动展示，不是弹窗
- 冷却期防止信息过载
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from core.config import get_config

logger = logging.getLogger(__name__)


# ========== 数据类 ==========

@dataclass
class ReminderEntry:
    """提醒记录"""
    reminder_id: str = ""
    issue_id: str = ""
    page_path: str = ""
    severity: str = "medium"        # critical / high / medium / low
    status: str = "pending"         # pending / pushed / resolved / deferred / ignored
    content: str = ""               # 推送内容（Markdown）
    choices: List[str] = field(default_factory=list)
    pushed_at: str = ""
    resolved_at: str = ""
    resolved_choice: str = ""
    defer_until: str = ""
    cooldown_until: str = ""
    created_at: str = ""

    def to_dict(self) -> Dict:
        return {
            "reminder_id": self.reminder_id,
            "issue_id": self.issue_id,
            "page_path": self.page_path,
            "severity": self.severity,
            "status": self.status,
            "content": self.content,
            "choices": json.dumps(self.choices, ensure_ascii=False),
            "pushed_at": self.pushed_at,
            "resolved_at": self.resolved_at,
            "resolved_choice": self.resolved_choice,
            "defer_until": self.defer_until,
            "cooldown_until": self.cooldown_until,
            "created_at": self.created_at,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ReminderEntry":
        return cls(
            reminder_id=row["reminder_id"],
            issue_id=row["issue_id"],
            page_path=row["page_path"] or "",
            severity=row["severity"],
            status=row["status"],
            content=row["content"] or "",
            choices=json.loads(row["choices"] or "[]"),
            pushed_at=row["pushed_at"] or "",
            resolved_at=row["resolved_at"] or "",
            resolved_choice=row["resolved_choice"] or "",
            defer_until=row["defer_until"] or "",
            cooldown_until=row["cooldown_until"] or "",
            created_at=row["created_at"] or "",
        )


# ========== DialogReminderQueue ==========

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS dialog_reminders (
    reminder_id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL,
    page_path TEXT,
    severity TEXT NOT NULL,
    status TEXT NOT NULL,
    content TEXT,
    choices TEXT,                  -- JSON array
    pushed_at TIMESTAMP,
    resolved_at TIMESTAMP,
    resolved_choice TEXT,
    defer_until TIMESTAMP,
    cooldown_until TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_reminders_status ON dialog_reminders(status);
CREATE INDEX IF NOT EXISTS idx_reminders_page ON dialog_reminders(page_path);
CREATE INDEX IF NOT EXISTS idx_reminders_severity ON dialog_reminders(severity);
CREATE INDEX IF NOT EXISTS idx_reminders_defer ON dialog_reminders(defer_until);
"""


class DialogReminderQueue:
    """对话界面提醒队列

    三级触发机制：
    1. critical: 即时加入队列，用户当前对话中插入
    2. high/medium: 问题发现时不推送，等待"触发"
       - 触发方式 A: 用户对话涉及该知识 → 立即推送
       - 触发方式 B: 24h 内未触发 → 用户下次对话时兜底推送
    3. 每次对话最多推送 3 条（避免信息过载）
    """

    MAX_PER_SESSION = 3
    COOLDOWN_HOURS = 24
    DEFER_HOURS = 24

    SEVERITY_PRIORITY = {
        "critical": 1,
        "high": 2,
        "medium": 3,
        "low": 4,
    }

    def __init__(self, db_path: str = None):
        self.db_path = Path(db_path) if db_path else (
            get_config().data_dir / "dialog_reminder.db"
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.executescript(DB_SCHEMA)
            conn.commit()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # ---------- 入队 ----------

    def enqueue(
        self,
        issue_id: str,
        page_path: str,
        severity: str,
        content: str,
        choices: List[str],
    ) -> str:
        """
        将问题加入提醒队列。

        Returns:
            reminder_id
        """
        reminder_id = self._generate_reminder_id(issue_id, page_path)
        now = datetime.now(timezone.utc).isoformat()[:19]

        with self._conn() as conn:
            # 检查是否已存在同 issue 的 pending reminder
            row = conn.execute(
                "SELECT status FROM dialog_reminders WHERE reminder_id = ?",
                (reminder_id,),
            ).fetchone()

            if row:
                if row["status"] in ("resolved", "ignored"):
                    # 重新打开
                    conn.execute(
                        """UPDATE dialog_reminders
                           SET status = 'pending', content = ?, choices = ?,
                               severity = ?, created_at = ?, defer_until = '',
                               cooldown_until = '', pushed_at = '', resolved_at = '',
                               resolved_choice = ''
                           WHERE reminder_id = ?""",
                        (content, json.dumps(choices, ensure_ascii=False),
                         severity, now, reminder_id),
                    )
                    conn.commit()
                    logger.info(f"提醒重新打开: {reminder_id}")
                else:
                    # 更新内容
                    conn.execute(
                        """UPDATE dialog_reminders
                           SET content = ?, choices = ?, severity = ?
                           WHERE reminder_id = ?""",
                        (content, json.dumps(choices, ensure_ascii=False), severity, reminder_id),
                    )
                    conn.commit()
                return reminder_id

            # 新插入
            conn.execute(
                """INSERT INTO dialog_reminders
                   (reminder_id, issue_id, page_path, severity, status,
                    content, choices, created_at)
                   VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)""",
                (reminder_id, issue_id, page_path, severity, content,
                 json.dumps(choices, ensure_ascii=False), now),
            )
            conn.commit()
            logger.info(f"新提醒入队: {reminder_id} [{severity}] {page_path}")
            return reminder_id

    # ---------- 触发推送 ----------

    def on_knowledge_triggered(self, page_path: str) -> List[ReminderEntry]:
        """
        用户对话触发了某知识页面时调用。

        返回待推送的提醒列表（已按严重度排序，最多 2 条）。
        """
        pending = self._get_triggerable_for_page(page_path)
        if not pending:
            return []

        # 按严重度排序
        pending.sort(key=lambda r: self.SEVERITY_PRIORITY.get(r.severity, 99))

        # 标记为已推送并返回
        to_push = pending[:2]
        self._mark_pushed(to_push)
        return to_push

    def on_user_active(self, max_results: int = None) -> List[ReminderEntry]:
        """
        用户活跃时兜底推送（未触发知识的问题）。

        获取 24h 内未被推送过的 pending 问题，
        按严重度 + 时效性排序，最多推送 3 条。
        """
        max_results = max_results or self.MAX_PER_SESSION

        reminders = self._get_pending_for_push()
        if not reminders:
            return []

        # 按严重度 + 创建时间排序
        reminders.sort(
            key=lambda r: (
                self.SEVERITY_PRIORITY.get(r.severity, 99),
                r.created_at or "",
            )
        )

        to_push = reminders[:max_results]
        self._mark_pushed(to_push)

        # 剩余标记为 deferred
        for rem in reminders[max_results:]:
            self.defer(rem.reminder_id, hours=self.DEFER_HOURS)

        return to_push

    # ---------- 查询 ----------

    def get_pending(
        self,
        page_path: str = None,
        severity: str = None,
        limit: int = 50,
    ) -> List[ReminderEntry]:
        """获取待处理的提醒"""
        conditions = ["status = 'pending'"]
        params = []

        if page_path:
            conditions.append("page_path = ?")
            params.append(page_path)
        if severity:
            conditions.append("severity = ?")
            params.append(severity)

        where = "WHERE " + " AND ".join(conditions)
        query = f"""SELECT * FROM dialog_reminders {where}
                    ORDER BY
                        CASE severity
                            WHEN 'critical' THEN 1
                            WHEN 'high' THEN 2
                            WHEN 'medium' THEN 3
                            ELSE 4
                        END,
                        created_at DESC
                    LIMIT ?"""
        params.append(limit)

        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
            return [ReminderEntry.from_row(r) for r in rows]

    def get_by_issue(self, issue_id: str) -> Optional[ReminderEntry]:
        """通过 issue_id 获取提醒"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM dialog_reminders WHERE issue_id = ? AND status != 'resolved'",
                (issue_id,),
            ).fetchone()
            return ReminderEntry.from_row(row) if row else None

    def aggregate_for_page(self, page_path: str) -> Optional[ReminderEntry]:
        """
        聚合同一页面的多个问题为一条提醒。

        例：[[Docker Compose]] 同时有"孤立"+"内容过短"+"关键词稀疏"
        不推 3 条，推 1 条聚合提醒。
        """
        pending = self.get_pending(page_path=page_path)
        if len(pending) <= 1:
            return pending[0] if pending else None

        # 取最高严重度
        severities = [r.severity for r in pending]
        max_severity = min(severities, key=lambda s: self.SEVERITY_PRIORITY.get(s, 99))

        descriptions = []
        for r in pending:
            # 从 content 中提取第一行作为描述
            desc = r.content.strip().split("\n")[0] if r.content else r.issue_id
            descriptions.append(desc)

        aggregated_content = (
            f"📋 [[{Path(page_path).stem}]] 存在 {len(pending)} 个优化建议：\n\n"
            + "\n".join(f"- {d}" for d in descriptions)
        )

        return ReminderEntry(
            reminder_id=f"agg-{self._hash(page_path)}",
            issue_id=",".join(r.issue_id for r in pending),
            page_path=page_path,
            severity=max_severity,
            content=aggregated_content,
            choices=["查看详情", "忽略全部"],
        )

    # ---------- 用户响应 ----------

    def resolve(self, reminder_id: str, choice: str) -> bool:
        """用户做出选择后标记为已解决"""
        now = datetime.now(timezone.utc).isoformat()[:19]
        with self._conn() as conn:
            cursor = conn.execute(
                """UPDATE dialog_reminders
                   SET status = 'resolved', resolved_choice = ?, resolved_at = ?
                   WHERE reminder_id = ?""",
                (choice, now, reminder_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def ignore(self, reminder_id: str) -> bool:
        """用户选择忽略"""
        now = datetime.now(timezone.utc).isoformat()[:19]
        with self._conn() as conn:
            cursor = conn.execute(
                """UPDATE dialog_reminders
                   SET status = 'ignored', resolved_at = ?
                   WHERE reminder_id = ?""",
                (now, reminder_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def defer(self, reminder_id: str, hours: int = 24) -> bool:
        """推迟提醒"""
        defer_until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()[:19]
        with self._conn() as conn:
            cursor = conn.execute(
                """UPDATE dialog_reminders
                   SET status = 'deferred', defer_until = ?
                   WHERE reminder_id = ?""",
                (defer_until, reminder_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    # ---------- 冷却期 ----------

    def is_in_cooldown(self, reminder_id: str) -> bool:
        """检查提醒是否在冷却期内"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT cooldown_until FROM dialog_reminders WHERE reminder_id = ?",
                (reminder_id,),
            ).fetchone()
            if not row or not row["cooldown_until"]:
                return False
            try:
                dt = datetime.fromisoformat(row["cooldown_until"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt > datetime.now(timezone.utc)
            except ValueError:
                return False

    def set_cooldown(self, reminder_id: str, hours: int = 24) -> bool:
        """设置冷却期"""
        cooldown = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()[:19]
        with self._conn() as conn:
            cursor = conn.execute(
                "UPDATE dialog_reminders SET cooldown_until = ? WHERE reminder_id = ?",
                (cooldown, reminder_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    # ---------- 统计 ----------

    def count_by_status(self) -> Dict[str, int]:
        """按状态统计"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM dialog_reminders GROUP BY status"
            ).fetchall()
            return {row[0]: row[1] for row in rows}

    def cleanup_resolved(self, retention_days: int = 30) -> int:
        """清理已解决/已忽略的旧记录"""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with self._conn() as conn:
            cursor = conn.execute(
                """DELETE FROM dialog_reminders
                   WHERE status IN ('resolved', 'ignored')
                   AND resolved_at < ?""",
                (cutoff,),
            )
            conn.commit()
            return cursor.rowcount

    # ---------- 内部方法 ----------

    def _get_triggerable_for_page(self, page_path: str) -> List[ReminderEntry]:
        """获取某页面关联的可触发提醒"""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM dialog_reminders
                   WHERE page_path = ?
                   AND status = 'pending'
                   AND (defer_until IS NULL OR defer_until < ?)
                   AND (cooldown_until IS NULL OR cooldown_until < ?)
                   ORDER BY created_at DESC""",
                (page_path, now, now),
            ).fetchall()
            return [ReminderEntry.from_row(r) for r in rows]

    def _get_pending_for_push(self) -> List[ReminderEntry]:
        """获取所有待推送的提醒（兜底推送用）"""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM dialog_reminders
                   WHERE status = 'pending'
                   AND (defer_until IS NULL OR defer_until < ?)
                   AND (cooldown_until IS NULL OR cooldown_until < ?)
                   ORDER BY created_at DESC""",
                (now, now),
            ).fetchall()
            return [ReminderEntry.from_row(r) for r in rows]

    def _mark_pushed(self, reminders: List[ReminderEntry]):
        """标记提醒为已推送"""
        now = datetime.now(timezone.utc).isoformat()[:19]
        with self._conn() as conn:
            for rem in reminders:
                conn.execute(
                    """UPDATE dialog_reminders
                       SET status = 'pushed', pushed_at = ?
                       WHERE reminder_id = ?""",
                    (now, rem.reminder_id),
                )
            conn.commit()

    def _generate_reminder_id(self, issue_id: str, page_path: str) -> str:
        raw = f"{issue_id}:{page_path}"
        return f"rem-{self._hash(raw)}"

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.md5(text.encode("utf-8")).hexdigest()[:12]


# ========== PageBannerInjector ==========

class PageBannerInjector:
    """页面横幅注入器

    在 Wiki 页面正文开头插入提醒横幅，用户处理后可一键移除。
    """

    MARKER_START = "<!-- mnemos-reminder -->"
    MARKER_END = "<!-- /mnemos-reminder -->"

    def inject_banner(self, page_path: Path, content_lines: List[str], issue_id: str = "") -> bool:
        """
        在页面中注入横幅。

        Args:
            page_path: Wiki 页面路径
            content_lines: 横幅内容行列表（不含 marker）
            issue_id: 关联的问题 ID

        Returns:
            是否成功注入
        """
        if not page_path.exists():
            return False

        text = page_path.read_text(encoding="utf-8")

        # 构造横幅块
        marker_attr = f':issue_id={issue_id}' if issue_id else ''
        banner = f"{self.MARKER_START}{marker_attr}\n"
        banner += "\n".join(content_lines)
        if not banner.endswith("\n"):
            banner += "\n"
        banner += f"{self.MARKER_END}\n\n"

        # 检查是否已有横幅
        if self.MARKER_START in text:
            text = self._replace_banner(text, banner)
        else:
            # 在 frontmatter 之后插入
            text = self._insert_after_frontmatter(text, banner)

        page_path.write_text(text, encoding="utf-8")
        return True

    def remove_banner(self, page_path: Path) -> bool:
        """移除页面中的横幅"""
        if not page_path.exists():
            return False

        text = page_path.read_text(encoding="utf-8")
        pattern = re.compile(
            rf"{re.escape(self.MARKER_START)}.*?{re.escape(self.MARKER_END)}\n?\n?",
            re.DOTALL,
        )
        new_text = pattern.sub("", text)

        if new_text != text:
            page_path.write_text(new_text, encoding="utf-8")
            return True
        return False

    def has_banner(self, page_path: Path) -> bool:
        """检查页面是否已有横幅"""
        if not page_path.exists():
            return False
        return self.MARKER_START in page_path.read_text(encoding="utf-8")

    def _replace_banner(self, text: str, new_banner: str) -> str:
        """替换已有的横幅"""
        pattern = re.compile(
            rf"{re.escape(self.MARKER_START)}.*?{re.escape(self.MARKER_END)}\n?\n?",
            re.DOTALL,
        )
        return pattern.sub(new_banner, text)

    def _insert_after_frontmatter(self, text: str, banner: str) -> str:
        """在 frontmatter 之后插入横幅"""
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                return f"---{parts[1]}---\n\n{banner}{parts[2].lstrip()}"
        return banner + text


# ========== 提醒内容渲染器 ==========

class ReminderRenderer:
    """提醒内容渲染器

    生成带 `[选择：xxx]` 交互选项的对话内容，
    以及 Wiki 页面横幅内容。
    """

    @staticmethod
    def render_dialog(entry: ReminderEntry) -> str:
        """
        渲染带交互选项的对话推送内容。

        示例输出：
            <wiki-context type="reminder" severity="high" issue_id="rem-abc">
            📅 知识提醒：「Redis 连接池配置」

            此知识基于 Redis 6.0，你最近讨论了 Redis 7.2，建议确认是否仍有效。

            [选择：已更新] [选择：仍有效] [选择：忽略]
            </wiki-context>
        """
        severity_emoji = {
            "critical": "⚠️",
            "high": "📅",
            "medium": "📋",
            "low": "💡",
        }.get(entry.severity, "📋")

        lines = [
            f'<wiki-context type="reminder" severity="{entry.severity}" issue_id="{entry.reminder_id}">',
            f"{severity_emoji} {entry.content}",
            "",
        ]
        if entry.choices:
            choice_str = " ".join(f"[选择：{c}]" for c in entry.choices)
            lines.append(choice_str)
        lines.append("</wiki-context>")
        return "\n".join(lines)

    @staticmethod
    def render_banner(entry: ReminderEntry) -> List[str]:
        """
        渲染 Wiki 页面横幅内容行。

        示例输出：
            > ⚠️ **知识提醒**（自动生成，处理后可删除）
            >
            > Redis 连接池配置 已 180 天未更新，请确认是否仍有效。
            >
            > [已更新] [仍有效] [忽略]
        """
        severity_emoji = {
            "critical": "⚠️",
            "high": "📅",
            "medium": "📋",
            "low": "💡",
        }.get(entry.severity, "📋")

        lines = [
            f"> {severity_emoji} **知识提醒**（自动生成，处理后可删除）",
            ">",
            f"> {entry.content}",
            ">",
        ]
        if entry.choices:
            choice_str = " ".join(f"[{c}]" for c in entry.choices)
            lines.append(f"> {choice_str}")
        return lines

    @staticmethod
    def render_aggregated_dialog(page_title: str, entries: List[ReminderEntry]) -> str:
        """
        渲染聚合提醒（同一页面多个问题合并）。

        示例：
            <wiki-context type="reminder" severity="medium">
            📋 [[Docker Compose]] 存在 3 个优化建议：
            - 孤立页面（无关联）
            - 内容过短（80 字符）

            [查看详情] [忽略全部]
            </wiki-context>
        """
        lines = [
            '<wiki-context type="reminder" severity="medium">',
            f"📋 [[{page_title}]] 存在 {len(entries)} 个优化建议：",
            "",
        ]
        for e in entries:
            desc = e.content.strip().split("\n")[0] if e.content else e.issue_id
            lines.append(f"- {desc}")
        lines.extend([
            "",
            "[选择：查看详情] [选择：忽略全部]",
            "</wiki-context>",
        ])
        return "\n".join(lines)


# ========== 便捷函数 ==========

def get_dialog_reminder_queue(db_path: str = None) -> DialogReminderQueue:
    """获取 DialogReminderQueue 单例"""
    return DialogReminderQueue(db_path=db_path)


def get_page_banner_injector() -> PageBannerInjector:
    """获取 PageBannerInjector 单例"""
    return PageBannerInjector()


def get_reminder_renderer() -> ReminderRenderer:
    """获取 ReminderRenderer 单例"""
    return ReminderRenderer()
