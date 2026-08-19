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
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from contextlib import contextmanager
from typing import Dict, Generator, List, Optional

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognitive.decision_trace import (
    MaterialActionRequest,
    authorize_exact_project_contract_action,
    find_pending_material_action_authorization,
)
from core.cognitive.state_contract import sha256_json
from core.config import get_config
from core.trust.vault_mutation_service import (
    TRUSTED_MARKDOWN_ACTION_TYPE,
    TRUSTED_MARKDOWN_EXECUTOR,
    TRUSTED_MARKDOWN_OWNER,
    TrustedVaultMutationService,
    commit_trusted_markdown,
    trusted_markdown_material_action_binding,
)
from core.trust.models import sha256_text
from core.kia.dialog_reminder_renderer import ReminderRenderer

# Constants extracted from magic numbers
DIALOG_REMINDER_QUEUE_DURATION_BUCKET_MONTH_DAYS = 30
DIALOG_REMINDER_DECISION_CONTRACT_ID = (
    "project-contract:dialog-reminder-markdown-actions"
)
DIALOG_REMINDER_DECISION_CONTRACT_REVISION = (
    "mnemos.dialog_reminder_markdown_actions.v1"
)
DIALOG_REMINDER_DECISION_CONTRACT_TEXT = (
    "The dialog reminder workflow may mutate only the exact page selected for "
    "a reminder banner or the user's recorded reminder response."
)
DIALOG_REMINDER_DECISION_PRODUCER_HASH = sha256_json(
    {
        "module": "core.kia.dialog_reminder",
        "producer": "_write_or_propose_page",
        "version": DIALOG_REMINDER_DECISION_CONTRACT_REVISION,
    }
)

logger = logging.getLogger(__name__)

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None
    logger.error("PyYAML 未安装，页面横幅的 frontmatter 更新功能不可用")


def _row_get(row: sqlite3.Row, key: str, default: Any = "") -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _write_or_propose_page(
    page_path: Path,
    content: str,
    *,
    source: str,
    proposed_action: str,
    evidence_refs: list[str] | None = None,
    expected_existing_hash: str | None = None,
) -> bool:
    allowed_action = proposed_action in {
        "downgrade_status",
        "inject_banner",
        "remove_banner",
        "replace_banner",
    } or proposed_action.startswith("update_frontmatter_")
    if source != "dialog_reminder" or not allowed_action:
        raise ValueError("unsupported dialog reminder Markdown action")
    refs = evidence_refs or [str(page_path)]
    service = TrustedVaultMutationService(wiki_base=page_path.parent)
    binding = trusted_markdown_material_action_binding(
        target_path=page_path,
        content=content,
        proposed_action=proposed_action,
        expected_existing_hash=expected_existing_hash,
    )
    state_db_path = (
        service.config.db_path.parent / "producer_consumer_ledger.db"
    ).resolve(strict=False)
    request = MaterialActionRequest(
        owner=TRUSTED_MARKDOWN_OWNER,
        executor_id=TRUSTED_MARKDOWN_EXECUTOR,
        action_type=TRUSTED_MARKDOWN_ACTION_TYPE,
        target_ref=binding["target_ref"],
        input_hash=binding["input_hash"],
        expected_state_db=str(state_db_path),
    )
    material_action = find_pending_material_action_authorization(
        state_db_path=state_db_path,
        owner=request.owner,
        executor_id=request.executor_id,
        action_type=request.action_type,
        target_ref=request.target_ref,
        input_hash=request.input_hash,
    )
    if material_action is None:
        decision_created_at = datetime.now().astimezone().isoformat()
        material_action = authorize_exact_project_contract_action(
            expected_request=request,
            state_db_path=state_db_path,
            contract_id=DIALOG_REMINDER_DECISION_CONTRACT_ID,
            contract_revision_id=DIALOG_REMINDER_DECISION_CONTRACT_REVISION,
            contract_text=DIALOG_REMINDER_DECISION_CONTRACT_TEXT,
            source_namespace="dialog-reminder-markdown-action",
            source_facts={
                "schema_version": "mnemos.dialog_reminder_markdown_facts.v1",
                "decision_created_at": decision_created_at,
                "source": source,
                "proposed_action": proposed_action,
                "page_path": str(page_path.expanduser().resolve(strict=False)),
                "content_hash": sha256_text(content),
                "expected_existing_hash": str(expected_existing_hash or ""),
                "evidence_refs": list(refs),
            },
            decision_checks={
                "registered_reminder_action": allowed_action,
                "dialog_reminder_source": source == "dialog_reminder",
                "evidence_refs_present": bool(refs),
            },
            evidence_refs=tuple(
                dict.fromkeys(
                    (f"dialog-reminder-action:{proposed_action}", *refs)
                )
            ),
            task=f"Apply dialog reminder page action {proposed_action}",
            goal="Mutate only the exact page selected by the reminder workflow.",
            constraints=(
                "Only registered banner and reminder-response actions are allowed.",
                "The page path, content, and before hash cannot drift.",
            ),
            created_at=decision_created_at,
            producer="dialog-reminder",
            producer_version=DIALOG_REMINDER_DECISION_CONTRACT_REVISION,
            producer_code_hash=DIALOG_REMINDER_DECISION_PRODUCER_HASH,
            evaluator_id="dialog-reminder-markdown-evaluator",
            approved_candidate_key="apply_exact_dialog_reminder_page_update",
            approved_candidate_summary=(
                "Apply the exact page update selected by the reminder workflow."
            ),
            rejected_candidate_key="reject_unbound_dialog_reminder_update",
            rejected_candidate_summary=(
                "Reject an unregistered or drifted reminder page update."
            ),
            approved_reason_code="dialog_reminder_page_binding_verified",
            rejected_reason_code="dialog_reminder_page_binding_rejected",
            committed_metric="dialog_reminder_page_receipt",
            rejected_metric="unbound_dialog_reminder_page_update_count",
        )
    trusted = service.submit_markdown(
        target_path=page_path,
        content=content,
        source=source,
        actor="system",
        evidence_refs=refs,
        proposed_action=proposed_action,
        expected_existing_hash=expected_existing_hash,
        metadata={"path": str(page_path)},
        material_action=material_action,
    )
    if trusted.intercepted:
        return True
    commit_trusted_markdown(
        trusted,
        target_path=page_path,
        content=content,
        material_action=material_action,
    )
    return True


# ========== 数据类 ==========


@dataclass
class ReminderEntry:
    """提醒记录"""

    reminder_id: str = ""
    issue_id: str = ""
    page_path: str = ""
    severity: str = "medium"  # critical / high / medium / low
    status: str = "pending"  # pending / routed / pushed / resolved / deferred / ignored / dismissed / expired
    content: str = ""  # 推送内容（Markdown）
    choices: List[str] = field(default_factory=list)
    pushed_at: str = ""
    resolved_at: str = ""
    resolved_choice: str = ""
    defer_until: str = ""
    cooldown_until: str = ""
    delivery_event_id: str = ""
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
            "delivery_event_id": self.delivery_event_id,
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
            delivery_event_id=_row_get(row, "delivery_event_id", "") or "",
            created_at=row["created_at"] or "",
        )


class _PolicyConfigAdapter:
    """Expose policy/effective-policy objects through the config.get contract."""

    def __init__(self, policy: Any):
        self._policy = policy

    def get(self, key: str, default: Any = None) -> Any:
        get_effective = getattr(self._policy, "get_effective", None)
        if callable(get_effective):
            return get_effective(key, default)
        get_value = getattr(self._policy, "get", None)
        if callable(get_value):
            return get_value(key, default)
        return default


def _severity_delivery_level(severity: str) -> str:
    severity = str(severity or "").strip().lower()
    if severity == "critical":
        return "force_open"
    if severity == "high":
        return "warn"
    return "hint"


def _severity_fit_score(severity: str) -> float:
    severity = str(severity or "").strip().lower()
    return {
        "critical": 0.95,
        "high": 0.9,
        "medium": 0.8,
        "low": 0.7,
    }.get(severity, 0.75)


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
    delivery_event_id TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_reminders_status ON dialog_reminders(status);
CREATE INDEX IF NOT EXISTS idx_reminders_issue ON dialog_reminders(issue_id);
CREATE INDEX IF NOT EXISTS idx_reminders_page ON dialog_reminders(page_path);
CREATE INDEX IF NOT EXISTS idx_reminders_severity ON dialog_reminders(severity);
CREATE INDEX IF NOT EXISTS idx_reminders_defer ON dialog_reminders(defer_until);

CREATE TABLE IF NOT EXISTS dialog_reminder_corrections (
    source_event_id TEXT PRIMARY KEY,
    reminder_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class DialogReminderQueue:
    """对话界面提醒队列

    三级触发机制：
    1. critical: 即时加入队列，用户当前对话中插入
    2. high/medium: 问题发现时不推送，等待"触发"
       - 触发方式 A: 用户对话涉及该知识 → 立即推送
       - 触发方式 B: 24h 内未触发 → 用户下次对话时兜底推送
    3. 每次对话最多推送 N 条（避免信息过载），N 由 delivery profile 控制
    """

    SEVERITY_PRIORITY = {
        "critical": 1,
        "high": 2,
        "medium": 3,
        "low": 4,
    }

    def __init__(self, db_path: str | None = None, policy=None):
        self.db_path = (
            Path(db_path) if db_path else (get_config().database_dir / "dialog_reminder.db")
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._policy = policy
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(DB_SCHEMA)
            self._ensure_column(conn, "dialog_reminders", "delivery_event_id", "TEXT DEFAULT ''")
            conn.commit()

    @staticmethod
    def _ensure_column(
        conn: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row  # noqa
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
        finally:
            try:
                if sys.exc_info()[0] is None:
                    conn.commit()
                else:
                    conn.rollback()
            finally:
                conn.close()

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
                if row["status"] in ("resolved", "ignored", "dismissed", "expired"):
                    # 重新打开
                    conn.execute(
                        """UPDATE dialog_reminders
                           SET status = 'pending', content = ?, choices = ?,
                               severity = ?, created_at = ?, defer_until = '',
                               cooldown_until = '', pushed_at = '', resolved_at = '',
                               resolved_choice = ''
                           WHERE reminder_id = ?""",
                        (
                            content,
                            json.dumps(choices, ensure_ascii=False),
                            severity,
                            now,
                            reminder_id,
                        ),
                    )
                    conn.commit()
                    logger.info("提醒重新打开: %s", reminder_id)
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
                (
                    reminder_id,
                    issue_id,
                    page_path,
                    severity,
                    content,
                    json.dumps(choices, ensure_ascii=False),
                    now,
                ),
            )
            conn.commit()
            logger.info("新提醒入队: %s [%s] %s", reminder_id, severity, page_path)
            return reminder_id

    # ---------- 触发推送 ----------

    def on_knowledge_triggered(
        self,
        page_path: str,
        *,
        principal: PrincipalEnvelope | None = None,
    ) -> List[ReminderEntry]:
        """
        用户对话触发了某知识页面时调用。

        返回待推送的提醒列表（已按严重度排序，最多 delivery profile 条）。
        """
        pending = self._get_triggerable_for_page(page_path)
        if not pending:
            return []

        # 按严重度排序
        pending.sort(key=lambda r: self.SEVERITY_PRIORITY.get(r.severity, 99))

        to_push = pending[: self._get_max_per_session()]
        return self._mark_pushed(to_push, principal=principal)

    def _get_max_per_session(self) -> int:
        """从 delivery profile 读取每次对话最大推送数。"""
        try:
            return max(1, int(self._get_delivery_policy().per_task_total))
        # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
            from core.cognitive.delivery_router import DeliveryBudgetPolicy

            return max(1, int(DeliveryBudgetPolicy().per_task_total))

    def _get_overflow_defer_hours(self) -> int:
        """从 delivery profile 读取预算溢出后的推迟时间。"""
        try:
            return max(1, int(self._get_delivery_policy().overflow_defer_hours))
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
            from core.cognitive.delivery_router import DeliveryBudgetPolicy

            return max(1, int(DeliveryBudgetPolicy().overflow_defer_hours))

    def _get_delivery_policy(self):
        """读取统一投递预算；旧 app.push_max_items 只作为兼容兜底。"""
        from core.cognitive.delivery_router import DeliveryBudgetPolicy

        policy = self._policy
        if policy is None:
            from core.kia.adaptive_config import AdaptiveConfig

            policy = AdaptiveConfig()
        return DeliveryBudgetPolicy.from_config(_PolicyConfigAdapter(policy))

    def on_user_active(
        self,
        max_results: int | None = None,
        *,
        principal: PrincipalEnvelope | None = None,
    ) -> List[ReminderEntry]:
        """
        用户活跃时兜底推送（未触发知识的问题）。

        获取 24h 内未被推送过的 pending 问题，
        按严重度 + 时效性排序，最多推送 delivery profile 条。
        """
        if max_results is None:
            max_results = self._get_max_per_session()
        max_results = max(1, int(max_results))

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
        to_push = self._mark_pushed(to_push, principal=principal)

        # 剩余标记为 deferred
        for rem in reminders[max_results:]:
            self.defer(rem.reminder_id, hours=self._get_overflow_defer_hours())

        return to_push

    # ---------- 查询 ----------

    def list_reminders(
        self,
        status: str = "all",
        limit: int = 50,
    ) -> List[ReminderEntry]:
        """列出提醒，支持按状态筛选。

        Args:
            status: pending / pushed / resolved / deferred / ignored / all
            limit: 最大返回数
        """
        conditions = []
        params = []
        if status and status != "all":
            conditions.append("status = ?")
            params.append(status)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"""SELECT * FROM dialog_reminders {where}
                    ORDER BY
                        CASE severity
                            WHEN 'critical' THEN 1
                            WHEN 'high' THEN 2
                            WHEN 'medium' THEN 3
                            ELSE 4
                        END,
                        created_at DESC
                    LIMIT ?"""  # nosec B608
        params.append(limit)  # type: ignore[arg-type]

        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
            return [ReminderEntry.from_row(r) for r in rows]

    def get_pending(
        self,
        page_path: str | None = None,
        severity: str | None = None,
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
                    LIMIT ?"""  # nosec B608
        params.append(limit)  # type: ignore[arg-type]

        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
            return [ReminderEntry.from_row(r) for r in rows]

    def get_by_issue(self, issue_id: str) -> Optional[ReminderEntry]:
        """通过 issue_id 获取未关闭提醒。"""
        with self._conn() as conn:
            row = conn.execute(
                """SELECT * FROM dialog_reminders
                   WHERE issue_id = ? AND status NOT IN ('resolved', 'ignored')
                   ORDER BY
                       CASE severity
                           WHEN 'critical' THEN 1
                           WHEN 'high' THEN 2
                           WHEN 'medium' THEN 3
                           ELSE 4
                       END,
                       created_at DESC
                   LIMIT 1""",
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

        entry = ReminderEntry(
            reminder_id=f"agg-{self._hash(page_path)}",
            issue_id=",".join(r.issue_id for r in pending),
            page_path=page_path,
            severity=max_severity,
            content=aggregated_content,
            choices=["查看详情", "忽略全部"],
        )
        # 持久化聚合提醒，否则 resolve/ignore 会找不到记录
        now = datetime.now(timezone.utc).isoformat()[:19]
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO dialog_reminders
                   (reminder_id, issue_id, page_path, severity, status,
                    content, choices, created_at)
                   VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)""",
                (
                    entry.reminder_id,
                    entry.issue_id,
                    entry.page_path,
                    entry.severity,
                    entry.content,
                    json.dumps(entry.choices, ensure_ascii=False),
                    now,
                ),
            )
            conn.commit()
        return entry

    # ---------- 用户响应 ----------

    def record_user_response(
        self,
        reminder_id: str,
        action: str,
        *,
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing,
        choice: str = "",
        reason: str = "",
        hours: int = 24,
        supersedes_event_id: str = "",
    ) -> dict[str, Any]:
        """Record an authenticated card response before applying queue state."""

        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM dialog_reminders WHERE reminder_id=?",
                (reminder_id,),
            ).fetchone()
        if row is None:
            raise ValueError("dialog reminder does not exist")
        normalized_action = str(action or "").strip().lower()
        from core.cognitive.feedback_entrypoints import (
            record_dialog_reminder_feedback,
        )

        canonical = record_dialog_reminder_feedback(
            database_dir=self.db_path.parent,
            reminder_snapshot=ReminderEntry.from_row(row).to_dict(),
            action=normalized_action,
            reason=reason or choice,
            principal=principal,
            narrowing=narrowing,
            supersedes_event_id=supersedes_event_id,
        )
        if normalized_action == "resolve":
            updated = self.resolve(reminder_id, choice)
        elif normalized_action == "ignore":
            updated = self.ignore(reminder_id)
        elif normalized_action == "dismiss":
            updated = self.dismiss(reminder_id, reason=reason or "dismissed")
        elif normalized_action == "defer":
            updated = self.defer(reminder_id, hours=hours)
        else:
            raise ValueError("unsupported dialog reminder response")
        if not updated:
            raise RuntimeError("dialog reminder response was not applied")
        return {
            "success": True,
            "reminder_id": reminder_id,
            "action": normalized_action,
            "canonical_feedback": canonical,
        }

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
            updated = cursor.rowcount > 0
        return updated

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
            updated = cursor.rowcount > 0
        return updated

    def dismiss(
        self,
        reminder_id: str,
        reason: str = "dismissed",
        source_event_id: str = "",
    ) -> bool:
        """Dismiss a reminder without treating it as accepted."""
        now = datetime.now(timezone.utc).isoformat()[:19]
        with self._conn() as conn:
            if source_event_id and conn.execute(
                "SELECT 1 FROM dialog_reminder_corrections WHERE source_event_id=?",
                (source_event_id,),
            ).fetchone():
                return True
            cursor = conn.execute(
                """UPDATE dialog_reminders
                   SET status = 'dismissed',
                       resolved_choice = ?,
                       resolved_at = ?
                   WHERE reminder_id = ?""",
                (reason, now, reminder_id),
            )
            if cursor.rowcount > 0 and source_event_id:
                conn.execute(
                    """
                    INSERT INTO dialog_reminder_corrections (
                        source_event_id, reminder_id, reason, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (source_event_id, reminder_id, reason, now),
                )
            conn.commit()
            updated = cursor.rowcount > 0
        return updated

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
            updated = cursor.rowcount > 0
        return updated

    def schedule_at(self, reminder_id: str, when: str) -> bool:
        """Schedule a reminder at an explicit ISO timestamp."""
        scheduled = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
        scheduled_iso = scheduled.isoformat()
        with self._conn() as conn:
            cursor = conn.execute(
                """
                UPDATE dialog_reminders
                SET status='deferred', defer_until=?
                WHERE reminder_id=?
                """,
                (scheduled_iso, reminder_id),
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

    def expire_stale_pending(
        self,
        days: int = DIALOG_REMINDER_QUEUE_DURATION_BUCKET_MONTH_DAYS,
        *,
        limit: int | None = None,
        severity: str = "",
    ) -> int:
        """Expire old pending/deferred reminders so the queue cannot grow forever."""
        if limit is not None and int(limit) <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(0, int(days)))).isoformat()
        conditions = ["status IN ('pending', 'deferred', 'routed')", "created_at < ?"]
        params: list[Any] = [cutoff]
        if severity:
            conditions.append("severity = ?")
            params.append(severity)
        select_query = f"""
            SELECT reminder_id, status FROM dialog_reminders
            WHERE {' AND '.join(conditions)}
            ORDER BY created_at ASC
        """  # nosec B608
        if limit is not None:
            select_query += " LIMIT ?"
            params.append(max(1, int(limit)))
        now = datetime.now(timezone.utc).isoformat()[:19]
        with self._conn() as conn:
            rows = conn.execute(select_query, params).fetchall()
            for row in rows:
                reminder_id = str(row["reminder_id"])
                timeout_reason = (
                    "presentation_timeout"
                    if str(row["status"]) == "routed"
                    else f"expired after {days} days"
                )
                conn.execute(
                    """UPDATE dialog_reminders
                       SET status = 'expired',
                           resolved_at = ?,
                           resolved_choice = ?
                       WHERE reminder_id = ?""",
                    (now, timeout_reason, reminder_id),
                )
            return len(rows)

    def cleanup_resolved(
        self, retention_days: int = DIALOG_REMINDER_QUEUE_DURATION_BUCKET_MONTH_DAYS
    ) -> int:
        """清理已关闭的旧记录"""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with self._conn() as conn:
            cursor = conn.execute(
                """DELETE FROM dialog_reminders
                   WHERE status IN ('resolved', 'ignored', 'dismissed', 'expired')
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

    def _mark_pushed(
        self,
        reminders: List[ReminderEntry],
        *,
        principal: PrincipalEnvelope | None,
    ) -> List[ReminderEntry]:
        """Create host delivery commands without claiming host presentation."""
        pushed: List[ReminderEntry] = []
        with self._conn() as conn:
            for rem in reminders:
                event_id = self._route_delivery(rem, principal=principal)
                if not str(event_id or "").strip():
                    defer_until = (
                        datetime.now(timezone.utc)
                        + timedelta(hours=self._get_overflow_defer_hours())
                    ).isoformat()[:19]
                    conn.execute(
                        """UPDATE dialog_reminders
                           SET status = 'deferred', defer_until = ?
                           WHERE reminder_id = ?""",
                        (defer_until, rem.reminder_id),
                    )
                    continue
                conn.execute(
                    """UPDATE dialog_reminders
                       SET status = 'routed', pushed_at = NULL, delivery_event_id = ?
                       WHERE reminder_id = ?""",
                    (event_id, rem.reminder_id),
                )
                rem.status = "routed"
                rem.pushed_at = ""
                rem.delivery_event_id = event_id
                pushed.append(rem)
                from core.ops.runtime_flow_telemetry import record_runtime_consumed

                record_runtime_consumed(
                    "reminder_to_dialog_nudge",
                    source="core/kia/dialog_reminder.py",
                    item_id=str(rem.reminder_id),
                    metadata={
                        "transition": "dialog_nudge_routed_not_presented",
                        "delivery_event_id": event_id,
                    },
                    config_or_path=self.db_path.parent,
                )
            conn.commit()
        return pushed

    def _route_delivery(
        self,
        reminder: ReminderEntry,
        *,
        principal: PrincipalEnvelope | None,
    ) -> str | None:
        """Write reminder delivery through the unified delivery router.

        Returns:
            delivery event id when delivered, None when router suppresses it,
            or "" when routing is unavailable and existing push behavior should continue.
        """
        try:
            from core.cognitive.delivery_router import KnowledgeDeliveryRouter

            router = KnowledgeDeliveryRouter(
                db_path=self.db_path.with_name("delivery_events.db"),
                database_dir=self.db_path.parent,
                policy=self._get_delivery_policy(),
            )
            decision = router.route_candidate(
                source="dialog_reminder",
                subject=reminder.page_path or reminder.issue_id,
                channel="dialog_reminder",
                target=reminder.page_path,
                evidence_refs=[reminder.page_path] if reminder.page_path else [],
                task_fit_score=_severity_fit_score(reminder.severity),
                requested_level=_severity_delivery_level(reminder.severity),
                task_key="dialog_reminder",
                cooldown_key=reminder.page_path or reminder.issue_id,
                active_risk=reminder.severity in {"critical", "high"},
                metadata={
                    "reminder_id": reminder.reminder_id,
                    "issue_id": reminder.issue_id,
                    "severity": reminder.severity,
                    "choices": reminder.choices,
                },
                principal=principal,
            )
            if decision.decision != "deliver":
                logger.info(
                    "提醒投递被 DeliveryRouter 抑制: %s %s",
                    reminder.reminder_id,
                    decision.reason,
                )
                return None
            return str(decision.event_id)
        except (
            OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError,
            sqlite3.Error
        ):
            logger.debug("[dialog_reminder] delivery route failed", exc_info=True)
            return ""

    def record_presentation(
        self,
        reminder_id: str,
        *,
        principal: PrincipalEnvelope,
        rendered_content_hash: str,
    ) -> Dict:
        """Acknowledge a real host render for one previously routed reminder."""

        reminder = self._get_by_id(reminder_id)
        if reminder is None or not reminder.delivery_event_id:
            raise ValueError("reminder has no routed delivery event")
        if reminder.status not in {"routed", "pushed"}:
            raise ValueError("reminder is not awaiting presentation acknowledgement")
        from core.cognitive.delivery_router import KnowledgeDeliveryRouter

        receipt = KnowledgeDeliveryRouter(
            db_path=self.db_path.with_name("delivery_events.db"),
            database_dir=self.db_path.parent,
            policy=self._get_delivery_policy(),
        ).record_presentation(
            reminder.delivery_event_id,
            host_agent=principal.agent,
            rendered_content_hash=rendered_content_hash,
        )
        now = datetime.now(timezone.utc).isoformat()[:19]
        with self._conn() as conn:
            conn.execute(
                """UPDATE dialog_reminders
                   SET status='pushed', pushed_at=?
                   WHERE reminder_id=? AND delivery_event_id=?""",
                (now, reminder_id, reminder.delivery_event_id),
            )
            conn.commit()
        from core.ops.runtime_flow_telemetry import record_runtime_consumed

        record_runtime_consumed(
            "reminder_to_dialog_nudge",
            source="core/kia/dialog_reminder.py",
            item_id=str(reminder_id),
            metadata={
                "transition": "dialog_nudge_presented",
                "delivery_event_id": reminder.delivery_event_id,
                "presentation_receipt_hash": receipt["receipt_hash"],
            },
            config_or_path=self.db_path.parent,
        )
        return {"success": True, "reminder_id": reminder_id, **receipt}

    def _get_by_id(self, reminder_id: str) -> ReminderEntry | None:
        if not reminder_id:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM dialog_reminders WHERE reminder_id = ?",
                (reminder_id,),
            ).fetchone()
            return ReminderEntry.from_row(row) if row else None

    def _generate_reminder_id(self, issue_id: str, page_path: str) -> str:
        raw = f"{issue_id}:{page_path}"
        return f"rem-{self._hash(raw)}"

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


# ========== BannerActionHandler ==========


class BannerActionHandler:
    """处理用户在页面横幅任务列表中做出的选择。

    支持三种标准动作：
    - 忽略：调用 queue.ignore()，并加入忽略规则
    - 仍有效：标记为 resolved，并更新页面 frontmatter 的 last_validated
    - 已更新：标记为 resolved，并更新页面 frontmatter 的 last_updated
    """

    CHOICE_IGNORE = {"忽略", "忽略全部", "不再提醒", "skip", "dismiss"}
    CHOICE_VALIDATE = {"仍有效", "有效", "继续有效", "validate", "valid"}
    CHOICE_UPDATE = {"已更新", "已处理", "已修复", "updated", "fixed", "已补充"}

    def __init__(self, queue: "DialogReminderQueue"):
        self.queue = queue

    def execute(self, choice: str, reminder_id: str, page_path: Path, issue_id: str = "") -> bool:
        """根据用户选择分发到对应处理函数"""
        choice_clean = choice.strip()
        if choice_clean in self.CHOICE_IGNORE:
            return self._handle_ignore(reminder_id, issue_id)
        if choice_clean in self.CHOICE_VALIDATE:
            return self._handle_validate(reminder_id, page_path)
        if choice_clean in self.CHOICE_UPDATE:
            return self._handle_update(reminder_id, page_path)
        # 未知选择：仅做 resolve 记录
        return self.queue.resolve(reminder_id, choice_clean)

    def _handle_ignore(self, reminder_id: str, issue_id: str) -> bool:
        """忽略：标记提醒为 ignored"""
        return self.queue.ignore(reminder_id)

    def _handle_validate(self, reminder_id: str, page_path: Path) -> bool:
        """仍有效：resolve 并记录 last_validated"""
        ok = self.queue.resolve(reminder_id, "仍有效")
        if ok:
            self._update_frontmatter_date(page_path, "last_validated")
        return ok

    def _handle_update(self, reminder_id: str, page_path: Path) -> bool:
        """已更新：resolve 并记录 last_updated，稳定状态降级为待验证"""
        ok = self.queue.resolve(reminder_id, "已更新")
        if ok:
            self._update_frontmatter_date(page_path, "last_updated")
            self._downgrade_status(page_path, "稳定", "待验证")
        return ok

    @staticmethod
    def _update_frontmatter_date(page_path: Path, key: str) -> bool:
        """安全更新页面 frontmatter 中的日期字段"""
        if not page_path.exists() or yaml is None:
            return False
        try:
            text = page_path.read_text(encoding="utf-8")
            if not text.startswith("---"):
                return False
            parts = text.split("---", 2)
            if len(parts) < 3:
                return False
            fm = yaml.safe_load(parts[1]) or {}
            fm[key] = datetime.now().strftime("%Y-%m-%d")
            new_fm = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False)
            new_text = f"---\n{new_fm}---{parts[2]}"
            return _write_or_propose_page(
                page_path,
                new_text,
                source="dialog_reminder",
                proposed_action=f"update_frontmatter_{key}",
                expected_existing_hash=sha256_text(text),
            )
        except (OSError, UnicodeError, RuntimeError, ValueError, TypeError, KeyError) as e:
            logger.warning("[BannerActionHandler] 更新 frontmatter 失败 %s: %s", page_path, e)
            return False

    @staticmethod
    def _downgrade_status(page_path: Path, from_status: str, to_status: str) -> bool:
        """把页面 frontmatter 中的指定状态降级"""
        if not page_path.exists() or yaml is None:
            return False
        try:
            text = page_path.read_text(encoding="utf-8")
            if not text.startswith("---"):
                return False
            parts = text.split("---", 2)
            if len(parts) < 3:
                return False
            fm = yaml.safe_load(parts[1]) or {}
            current_status = fm.get("状态") or fm.get("status", "")
            if current_status == from_status:
                fm["status"] = to_status
                # 同时更新中文键，如果存在
                if "状态" in fm:
                    fm["状态"] = to_status
                new_fm = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False)
                new_text = f"---\n{new_fm}---{parts[2]}"
                return _write_or_propose_page(
                    page_path,
                    new_text,
                    source="dialog_reminder",
                    proposed_action="downgrade_status",
                    expected_existing_hash=sha256_text(text),
                )
            return False
        except (OSError, UnicodeError, RuntimeError, ValueError, TypeError, KeyError) as e:
            logger.warning("[BannerActionHandler] 降级状态失败 %s: %s", page_path, e)
            return False


# ========== PageBannerInjector ==========


class PageBannerInjector:
    """页面横幅注入器

    在 Wiki 页面正文开头插入提醒横幅，用户处理后可一键移除。
    """

    MARKER_START = "<!-- mnemos-reminder -->"
    MARKER_END = "<!-- /mnemos-reminder -->"

    def inject_banner(
        self,
        page_path: Path,
        content_lines: List[str],
        issue_id: str = "",
        reminder_id: str = "",
    ) -> bool:
        """
        在页面中注入横幅。

        Args:
            page_path: Wiki 页面路径
            content_lines: 横幅内容行列表（不含 marker）
            issue_id: 关联的问题 ID
            reminder_id: 关联的提醒 ID，用于处理用户打勾后的 resolve

        Returns:
            是否成功注入
        """
        if not page_path.exists():
            return False

        text = page_path.read_text(encoding="utf-8")
        original_hash = sha256_text(text)

        # 构造 marker 属性（优先保存 reminder_id，同时兼容 issue_id）
        attrs = []
        if reminder_id:
            attrs.append(f"reminder_id={reminder_id}")
        if issue_id:
            attrs.append(f"issue_id={issue_id}")
        marker_attr = ":" + ":".join(attrs) if attrs else ""
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

        return _write_or_propose_page(
            page_path,
            text,
            source="dialog_reminder",
            proposed_action="inject_banner",
            expected_existing_hash=original_hash,
        )

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
            return _write_or_propose_page(
                page_path,
                new_text,
                source="dialog_reminder",
                proposed_action="remove_banner",
                expected_existing_hash=sha256_text(text),
            )
        return False

    def has_banner(self, page_path: Path) -> bool:
        """检查页面是否已有横幅"""
        if not page_path.exists():
            return False
        return self.MARKER_START in page_path.read_text(encoding="utf-8")

    def process_banners(
        self,
        wiki_base: Optional[Path] = None,
        queue: Optional["DialogReminderQueue"] = None,
    ) -> Dict[str, int]:
        """
        扫描 Wiki 页面中的所有横幅任务列表，处理用户打勾的选项。

        处理规则（按用户要求只有三种情况）：
        - 0 个勾选：继续等待，不做处理
        - 1 个勾选：调用 DialogReminderQueue.resolve() 并移除横幅
        - 2 个及以上勾选：视为冲突，保留横幅并提示"请只选择一项"

        Returns:
            统计字典：{"resolved": n, "conflict": n, "skipped": n, "errors": n}
        """
        from core.config import get_config

        wiki_base = wiki_base or Path(get_config().wiki_dir)
        if not wiki_base.exists():
            return {"resolved": 0, "conflict": 0, "skipped": 0, "errors": 0}

        if queue is None:
            queue = DialogReminderQueue()

        stats = {"resolved": 0, "conflict": 0, "skipped": 0, "errors": 0}
        md_files = list(wiki_base.rglob("*.md"))

        for page_path in md_files:
            try:
                text = page_path.read_text(encoding="utf-8")
                if self.MARKER_START not in text:
                    continue

                banner_info = self._extract_banner_info(text)
                if not banner_info:
                    continue

                checked = banner_info["checked"]
                banner_info["unchecked"]

                # 0 个勾选：继续等待
                if len(checked) == 0:
                    stats["skipped"] += 1
                    continue

                # 2 个及以上勾选：冲突
                if len(checked) >= 2:
                    new_banner = self._build_conflict_banner(banner_info)
                    self._replace_banner_in_page(page_path, new_banner)
                    stats["conflict"] += 1
                    continue

                # 1 个勾选：按选择执行对应动作并移除
                choice = checked[0]
                reminder_id = banner_info.get("reminder_id") or banner_info.get("issue_id")
                issue_id = banner_info.get("issue_id", "")
                if reminder_id:
                    handler = BannerActionHandler(queue)
                    handler.execute(choice, reminder_id, page_path, issue_id=issue_id)
                self.remove_banner(page_path)
                stats["resolved"] += 1

            except (OSError, UnicodeError, RuntimeError, ValueError, TypeError, KeyError) as e:
                logger.warning("处理页面横幅失败 %s: %s", page_path, e)
                stats["errors"] += 1

        return stats

    def _extract_banner_info(self, text: str) -> Optional[Dict]:
        """从页面文本中提取横幅信息和任务列表状态"""
        pattern = re.compile(
            rf"{re.escape(self.MARKER_START)}(?P<attrs>.*?)\n(?P<body>.*?){re.escape(self.MARKER_END)}",  # noqa: E501
            re.DOTALL,
        )
        match = pattern.search(text)
        if not match:
            return None

        attrs_str = match.group("attrs").strip()
        body = match.group("body")

        info: Dict[str, Any] = {
            "reminder_id": "",
            "issue_id": "",
            "checked": [],
            "unchecked": [],
            "body_lines": [],
            "raw_attrs": attrs_str,
        }

        # 解析 marker 属性
        for attr in attrs_str.strip(":").split(":"):
            if "=" in attr:
                key, value = attr.split("=", 1)
                if key in ("reminder_id", "issue_id"):
                    info[key] = value

        # 解析任务列表
        task_pattern = re.compile(r"^>\s+-\s+\[(?P<state>[ xX])\]\s*(?P<text>.+)$", re.MULTILINE)
        for task_match in task_pattern.finditer(body):
            state = task_match.group("state").strip().lower()
            task_text = task_match.group("text").strip()
            if state == "x":
                info["checked"].append(task_text)
            else:
                info["unchecked"].append(task_text)

        # 保留非任务列表的横幅正文行（用于重建横幅）
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith(">") and not re.match(r">\s+-\s+\[[ xX]\]", stripped):
                info["body_lines"].append(line)

        return info

    def _build_conflict_banner(self, banner_info: Dict) -> str:
        """构建冲突提示横幅（保留原有内容，追加冲突提示）"""
        attrs = banner_info.get("raw_attrs", "")
        body_lines = list(banner_info.get("body_lines", []))

        # 移除旧的冲突提示行，避免重复追加
        body_lines = [
            line for line in body_lines if "请只选择一项" not in line and "当前选择了" not in line
        ]

        checked = banner_info.get("checked", [])
        body_lines.append(">")
        body_lines.append(f"> ⚠️ **当前选择了 {len(checked)} 项，请只选择一项。**")
        body_lines.append(">")

        # 重置所有任务列表为未勾选，让用户重新选择
        all_tasks = checked + banner_info.get("unchecked", [])
        for task in all_tasks:
            body_lines.append(f"> - [ ] {task}")

        banner = f"{self.MARKER_START}{attrs}\n"
        banner += "\n".join(body_lines)
        if not banner.endswith("\n"):
            banner += "\n"
        banner += f"{self.MARKER_END}\n\n"
        return banner

    def _replace_banner_in_page(self, page_path: Path, new_banner: str) -> bool:
        """用新横幅替换页面中的旧横幅"""
        text = page_path.read_text(encoding="utf-8")
        new_text = self._replace_banner(text, new_banner)
        if new_text != text:
            return _write_or_propose_page(
                page_path,
                new_text,
                source="dialog_reminder",
                proposed_action="replace_banner",
                expected_existing_hash=sha256_text(text),
            )
        return False

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


# ========== 便捷函数 ==========


def get_dialog_reminder_queue(db_path: str | None = None) -> DialogReminderQueue:
    """获取 DialogReminderQueue 单例"""
    return DialogReminderQueue(db_path=db_path)


def get_page_banner_injector() -> PageBannerInjector:
    """获取 PageBannerInjector 单例"""
    return PageBannerInjector()


def get_reminder_renderer() -> ReminderRenderer:
    """获取 ReminderRenderer 单例"""
    return ReminderRenderer()
