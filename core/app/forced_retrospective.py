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
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.app.obsidian_opener import open_obsidian
from core.cognitive.state_contract import sha256_json
from core.config import get_config
from core.kia.charon import REMINDERS_DIR
from core.trust.formal_markdown import (
    TrustedMarkdownDecisionPolicy,
    submit_or_write_markdown_with_decision,
)

# Constants extracted from magic numbers
FORCED_RETROSPECTIVE_DURATION_BUCKET_WEEK_DAYS = 7
SAFE_TOPIC = 30
FORCED_RETROSPECTIVE__ROW_TO_RECAP_ROW = 7
RECAP_EVIDENCE_LABELS = {
    "budget": "预算/资源消耗",
    "participants": "参与人数",
    "conversion_rate": "转化率",
}

# 启动补偿单次最大处理数，防止停机后一次性弹出过多窗口阻塞启动
MAX_STARTUP_COMPENSATION_TASKS = 20

logger = logging.getLogger(__name__)
RECAP_REMINDER_MARKDOWN_POLICY = TrustedMarkdownDecisionPolicy(
    contract_id="project-contract:forced-retrospective-reminder-page",
    contract_revision_id="mnemos.forced_retrospective_reminder_page.v1",
    contract_text=(
        "ForcedRetrospective may write only the exact reminder page derived "
        "from the current durable recap task and its handoff evidence."
    ),
    source_namespace="forced-retrospective-reminder",
    producer="forced-retrospective",
    producer_code_hash=sha256_json(
        {
            "module": "core.app.forced_retrospective",
            "producer": "ForcedRetrospective._create_recap_page",
            "version": "mnemos.forced_retrospective_reminder_page.v1",
        }
    ),
    evaluator_id="forced-retrospective-reminder-evaluator",
    constraints=(
        "The recap task identity, due state, content, and target must remain exact.",
        "A drifted or missing recap task leaves the existing reminder unchanged.",
    ),
    approved_candidate_key="write_exact_recap_reminder",
    approved_candidate_summary="Write the exact reminder for the durable recap task.",
    rejected_candidate_key="retain_existing_recap_reminder",
    rejected_candidate_summary="Retain current Markdown if task or content facts drift.",
    approved_reason_code="recap_reminder_binding_verified",
    rejected_reason_code="recap_reminder_binding_rejected",
    committed_metric="recap_reminder_markdown_committed",
    rejected_metric="unbound_recap_reminder_count",
)


@dataclass
class RecapTask:
    """复盘待办"""

    task_id: str
    severity: str  # critical / high / medium / low
    topic: str
    source: str  # system / user
    created_at: str
    due_date: Optional[str] = None
    target_page: str = "08-Reminders/复盘提醒-default"
    user_request: str = ""
    age_days: float = 0
    same_type_count: int = 0
    user_promised: bool = False
    current_file: str = ""
    status: str = "pending"
    context: str = ""
    suggested_points: str = ""


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
            self._db_path = get_config().database_dir / "recap_tasks.db"
        self._init_db()

    def _init_db(self):
        """初始化复盘任务表（自动迁移 schema）"""
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
                    target_page TEXT NOT NULL DEFAULT '08-Reminders/复盘提醒-default',
                    user_request TEXT DEFAULT '',
                    age_days REAL DEFAULT 0,
                    same_type_count INTEGER DEFAULT 0,
                    user_promised INTEGER DEFAULT 0,
                    current_file TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending'
                )
            """)
            # Explicit bootstrap ensures the current event context columns.
            self._migrate_add_column(conn, "context", "TEXT", "''")
            self._migrate_add_column(conn, "suggested_points", "TEXT", "''")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS recap_task_events (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT DEFAULT '',
                    actor TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_recap_task_events_task
                ON recap_task_events(task_id, created_at)
            """)

    @staticmethod
    def _migrate_add_column(conn: sqlite3.Connection, col_name: str, col_type: str, default: str):
        """安全添加列（旧数据库兼容）"""
        cursor = conn.execute("PRAGMA table_info(recap_tasks)")
        existing = {row[1] for row in cursor.fetchall()}
        if col_name not in existing:
            conn.execute(
                f"ALTER TABLE recap_tasks ADD COLUMN {col_name} {col_type} DEFAULT {default}"
            )

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
        if recap.age_days >= FORCED_RETROSPECTIVE_DURATION_BUCKET_WEEK_DAYS:
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
            reasons.append("related_file(+2)")

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
            success = open_obsidian(page_path=recap.target_page)
            if success:
                logger.info(
                    "强制打开 Obsidian: %s (score=%s, reason=%s)",
                    recap.topic,
                    decision.score,
                    decision.reason,
                )
                self._update_status(recap.task_id, "reminded")
            else:
                logger.warning("强制打开 Obsidian 失败: %s", recap.topic)
                # 打开失败，降级为对话提醒
                decision.should_force_open = False
                decision.channel = "dialog_reminder"
        else:
            logger.debug(
                "对话轻提醒: %s (score=%s, reason=%s)", recap.topic, decision.score, decision.reason
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
            conn.execute(
                """
                INSERT OR REPLACE INTO recap_tasks
                (task_id, severity, topic, source, created_at,
                 due_date, target_page, user_request, status)
                VALUES (?, 'high', ?, 'user', ?, ?, ?, ?, 'pending')
            """,
                (
                    task_id,
                    user_request,
                    now.isoformat(),
                    due_date.isoformat(),
                    target_page,
                    user_request,
                ),
            )

        logger.info("用户预约复盘: %s → %s", user_request, due_date.isoformat())
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

    def list_user_reminders(self) -> List[RecapTask]:
        """列出所有用户预约"""
        with sqlite3.connect(str(self._db_path), timeout=10) as conn:
            cursor = conn.execute(
                "SELECT task_id, severity, topic, source, created_at, "
                "due_date, target_page, user_request, age_days, "
                "same_type_count, user_promised, current_file, status, "
                "context, suggested_points "
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
        context: str = "",
        suggested_points: str = "",
    ) -> str:
        """系统生成复盘待办，并创建提醒页面。

        如果已存在同主题（topic）的 pending 系统复盘，则更新上下文而不是
        重复创建新页面，避免 08-Reminders 下堆满无差别模板页。
        """
        now = datetime.now()

        # 1. 检查是否已有同主题 pending 任务
        existing = self._find_pending_duplicate(topic)
        if existing:
            task_id, target_page = existing
            self._update_recap_content(
                task_id, severity=severity, context=context, suggested_points=suggested_points
            )
            logger.info(
                "[ForcedRetrospective] 合并到已有复盘任务: %s -> %s, 页面: %s",
                topic,
                task_id,
                target_page,
            )
            return task_id

        # 2. 生成新提醒页面
        task_id = f"system-recap-{now.strftime('%Y%m%d%H%M%S%f')}"
        temp_recap = RecapTask(
            task_id=task_id,
            severity=severity,
            topic=topic,
            source="system",
            created_at=now.isoformat(),
            due_date=now.isoformat(),
            context=context,
            suggested_points=suggested_points,
        )
        target_page = self._generate_reminder_page(temp_recap)

        with sqlite3.connect(str(self._db_path), timeout=10) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO recap_tasks
                (task_id, severity, topic, source, created_at,
                 due_date, target_page, context, suggested_points, status)
                VALUES (?, ?, ?, 'system', ?, ?, ?, ?, ?, 'pending')
            """,
                (
                    task_id,
                    severity,
                    topic,
                    now.isoformat(),
                    now.isoformat(),
                    target_page,
                    context,
                    suggested_points,
                ),
            )

        logger.info(
            "[ForcedRetrospective] 系统复盘任务: %s -> %s, 页面: %s", topic, task_id, target_page
        )
        return task_id

    def _find_pending_duplicate(self, topic: str) -> Optional[Tuple[str, str]]:
        """查找同主题且状态为 pending 的系统复盘任务，返回 (task_id, target_page)"""
        with sqlite3.connect(str(self._db_path), timeout=10) as conn:
            cursor = conn.execute(
                "SELECT task_id, target_page FROM recap_tasks "
                "WHERE source = 'system' AND status = 'pending' AND topic = ? "
                "ORDER BY created_at ASC LIMIT 1",
                (topic,),
            )
            row = cursor.fetchone()
            return (row[0], row[1]) if row else None

    def _update_recap_content(
        self,
        task_id: str,
        severity: str = "",
        context: str = "",
        suggested_points: str = "",
    ):
        """更新已有复盘任务的上下文与建议，避免重复页面"""
        updates = []
        params: List[str] = []
        if severity:
            updates.append("severity = ?")
            params.append(severity)
        if context:
            updates.append("context = ?")
            params.append(context)
        if suggested_points:
            updates.append("suggested_points = ?")
            params.append(suggested_points)
        if not updates:
            return
        params.append(task_id)
        with sqlite3.connect(str(self._db_path), timeout=10) as conn:
            set_clause = ", ".join(updates)
            conn.execute(
                " ".join([
                    "UPDATE recap_tasks SET",
                    set_clause,
                    "WHERE task_id = ?",
                ]),
                params,
            )

    def _create_from_session_end(self, session_id: str, skip_reason: str) -> Optional[str]:
        """当 session 被蒸馏系统跳过时，自动创建系统复盘任务。"""
        if skip_reason not in ("skipped_low_quality", "skipped_by_pipeline"):
            return None
        severity = "medium" if skip_reason == "skipped_low_quality" else "high"
        topic = f"Session {session_id[:8]} 被跳过: {skip_reason}"

        if skip_reason == "skipped_low_quality":
            context = (
                f"Session `{session_id[:8]}...` 因内容质量不足被蒸馏系统跳过。\n\n"
                "可能原因：对话内容过短、信息密度低、或主要为闲聊。"
                "但这并不意味着对话没有价值——可能是某个关键技术决策、"
                "或者用户在讨论中提到了值得记录的经验点。"
            )
            suggested_points = (
                "- 回顾本次对话，判断是否有被遗漏的技术决策或经验\n"
                '- 分析为什么内容被判定为"低质量"——是否有改进表达的方式\n'
                "- 如果有价值点，建议手动补充到 Wiki 对应位置"
            )
        else:  # skipped_by_pipeline
            context = (
                f"Session `{session_id[:8]}...` 因蒸馏管道故障（如 API 断线、Agent 不可用）"
                "导致未能完成自动蒸馏。\n\n"
                "对话内容可能包含重要信息，需要人工复盘确认。"
            )
            suggested_points = (
                "- 检查对话中是否有重要的技术决策、架构设计或问题解决方案\n"
                "- 确认 API 故障期间是否有信息丢失\n"
                "- 如果有价值内容，建议手动触发蒸馏或补充到 Wiki"
            )

        task_id = self.create_system_recap(
            topic=topic,
            severity=severity,
            context=context,
            suggested_points=suggested_points,
        )
        logger.info("[ForcedRetrospective] Session skip 触发复盘: %s -> %s", topic, task_id)
        return task_id

    def _generate_reminder_page(self, recap: RecapTask) -> str:
        """生成复盘提醒页面，返回 Obsidian 内部路径（不含 .md）"""
        reminders_dir = Path(str(REMINDERS_DIR))
        reminders_dir.mkdir(parents=True, exist_ok=True)
        wiki_dir = reminders_dir.parent

        # 生成安全文件名
        safe_topic = re.sub(r"[^\w\s-]", "", recap.topic).strip()[:SAFE_TOPIC]
        safe_topic = re.sub(r"\s+", "-", safe_topic)
        page_name = f"复盘提醒-{recap.task_id}-{safe_topic}.md"
        page_path = reminders_dir / page_name

        due_str = recap.due_date[:10] if recap.due_date else "尽快"
        created_str = (
            recap.created_at[:10] if recap.created_at else datetime.now().strftime("%Y-%m-%d")
        )
        context = self._sanitize_reminder_context(recap.context)
        if context:
            context = self._translate_evidence_terms(context)
        else:
            context = "（暂无详细背景，请在对话中补充本次任务、预期结果和实际结果。）"
        suggested_points = (
            self._sanitize_suggested_points(recap.suggested_points)
            or "- 先确认本次复盘对象是什么\n- 再补充实际影响、根因和下次动作"
        )
        suggested_points = self._translate_evidence_terms(suggested_points)
        evidence_excerpt = self._extract_recap_evidence_excerpt(
            self._sanitize_reminder_context(recap.context)
        )
        focus = self._build_recap_focus(recap, evidence_excerpt)
        agent_prompt = self._build_agent_handoff_prompt(recap, focus)
        evidence_section = ""
        if evidence_excerpt:
            evidence_section = "\n\n### 结构化证据摘录\n\n" + "\n".join(evidence_excerpt)
        handoff_sentence = (
            f'Agent 应先用 `recap_start(task_id="{recap.task_id}")` 接续这条提醒；'
            "如果工具返回 `not_found`，就直接根据本页“已知背景和证据”继续复盘，"
            "再根据你的补充完成三问一确认并写入 `06-Retrospectives/复盘/`。"
        )

        content = f"""---
title: "复盘提醒：{recap.topic}"
task_id: {recap.task_id}
created_at: {created_str}
due_date: {due_str}
severity: {recap.severity}
source: {recap.source}
type: retrospective-reminder
status: {recap.status}
---

# 🔔 复盘提醒：{recap.topic}

> 这是一条待确认的复盘提醒，不是已经完成的复盘结论。先确认下面的对象和证据，再让 Agent 生成结构化复盘。

> ⏰ **建议完成时间**：{due_str}
> 📊 **优先级**：{recap.severity}
> 🆔 **任务编号**：`{recap.task_id}`

---

## 一、本次要复盘什么

- **复盘对象**：{recap.topic}
- **触发原因**：{focus}
- **任务编号**：`{recap.task_id}`
- **当前状态**：{recap.status}

## 二、已知背景和证据

{context}{evidence_section}

---

## 三、建议复盘重点

{suggested_points}

建议至少回答三件事：

1. 当时想达成什么，实际发生了什么？
2. 偏差或问题为什么会出现？
3. 下次遇到类似情况，具体怎么提前发现或避免？

---

## 四、如何和 Agent 继续

先从下面这句话开始：

```text
{agent_prompt}
```

{handoff_sentence}

---

## 五、相关链接

- [📂 06-Retrospectives](/06-Retrospectives/) — 历史复盘归档
- [📂 00-Inbox](/00-Inbox/) — 待处理知识

---

> 完成复盘后，Agent 会更新任务状态；你不需要手动编辑这个提醒页。
"""
        submit_or_write_markdown_with_decision(
            decision_policy=RECAP_REMINDER_MARKDOWN_POLICY,
            decision_facts={
                "schema_version": "mnemos.recap_reminder_facts.v1",
                "task_id": recap.task_id,
                "severity": recap.severity,
                "topic": recap.topic,
                "source": recap.source,
                "created_at": recap.created_at,
                "due_date": recap.due_date or "",
                "status": recap.status,
                "target_page": str(page_path),
            },
            decision_task=f"Write recap reminder {recap.task_id}",
            decision_goal="Materialize the exact reminder for this durable recap task.",
            decision_created_at=datetime.now().astimezone().isoformat(),
            wiki_base=wiki_dir,
            target_path=page_path,
            content=content,
            source="forced_retrospective",
            actor=recap.source or "system",
            source_session_id=recap.task_id,
            evidence_refs=[f"recap_task:{recap.task_id}"],
            proposed_action="create_recap_reminder_page",
            metadata={"topic": recap.topic, "channel": "dialog_reminder"},
        )
        # 返回 wiki 根目录相对路径（不含 .md，Obsidian 内部路径格式）
        rel_path = str(page_path.relative_to(wiki_dir))
        if rel_path.endswith(".md"):
            rel_path = rel_path[:-3]
        return rel_path

    @staticmethod
    def _extract_recap_evidence_excerpt(context: str) -> List[str]:
        """从旧版背景文本里摘出能让用户快速理解的结构化证据。"""
        if not context:
            return []
        excerpts: List[str] = []
        pattern = re.compile(
            r"^-\s*\[(?P<severity>[^\]]+)\]\s*(?P<area>[^:：]+)[:：]\s*"
            r"预期\s*(?P<expected>.*?)\s*/\s*实际\s*(?P<actual>.*?)\s*[—-]\s*"
            r"(?P<gap>.+)$"
        )
        for raw_line in context.splitlines():
            match = pattern.match(raw_line.strip())
            if not match:
                continue
            area = match.group("area").strip()
            expected = match.group("expected").strip()
            actual = match.group("actual").strip()
            severity = match.group("severity").strip()
            gap = match.group("gap").strip()
            area_label = ForcedRetrospective._readable_evidence_area(area)
            area_text = f"{area_label}（{area}）" if area_label != area else area
            excerpts.extend(
                [
                    f"- 偏差项：{area_text}",
                    f"  - 预期 {expected} / 实际 {actual}",
                    f"  - 严重程度：{severity}；说明：{gap}",
                ]
            )
        return excerpts

    @staticmethod
    def _sanitize_reminder_context(context: str) -> str:
        """移除旧提醒页自动生成区块，保证再次刷新页面时不会重复追加。"""
        if not context:
            return ""
        return re.split(r"\n+### 结构化证据摘录\b", context, maxsplit=1)[0].strip()

    @staticmethod
    def _sanitize_suggested_points(suggested_points: str) -> str:
        """移除旧提醒页自动生成的通用三问模板。"""
        if not suggested_points:
            return ""
        return re.split(r"\n+建议至少回答三件事：", suggested_points, maxsplit=1)[
            0
        ].strip()

    @staticmethod
    def _build_recap_focus(recap: RecapTask, evidence_excerpt: List[str]) -> str:
        if evidence_excerpt:
            first = evidence_excerpt[0].lstrip("- ").strip()
            second = evidence_excerpt[1].lstrip("- ").strip() if len(evidence_excerpt) > 1 else ""
            return f"{first}，{second}" if second else first
        for line in recap.suggested_points.splitlines():
            stripped = line.strip().lstrip("- ").strip()
            if stripped:
                return stripped
        return recap.topic

    @staticmethod
    def _build_agent_handoff_prompt(recap: RecapTask, focus: str) -> str:
        return (
            f"请复盘 task_id={recap.task_id}，主题是“{recap.topic}”。"
            f"重点看：{focus}。请先读取这条复盘提醒的背景和证据，再问我需要补充的信息。"
        )

    @staticmethod
    def _readable_evidence_area(area: str) -> str:
        return RECAP_EVIDENCE_LABELS.get(area, area)

    @staticmethod
    def _translate_evidence_terms(text: str) -> str:
        translated = ForcedRetrospective._normalize_evidence_terms(text)
        for raw, label in RECAP_EVIDENCE_LABELS.items():
            translated = re.sub(
                rf"(?<![（(])\b{re.escape(raw)}\b(?![）)])",
                f"{label}（{raw}）",
                translated,
            )
        return ForcedRetrospective._normalize_evidence_terms(translated)

    @staticmethod
    def _normalize_evidence_terms(text: str) -> str:
        normalized = text
        for raw, label in RECAP_EVIDENCE_LABELS.items():
            nested = f"{label}（{label}（{raw}））"
            while nested in normalized:
                normalized = normalized.replace(nested, f"{label}（{raw}）")
        return normalized

    def get_pending_system_recaps(self) -> List[RecapTask]:
        """获取所有待处理的系统复盘待办"""
        now = datetime.now()
        recaps = []

        with sqlite3.connect(str(self._db_path), timeout=10) as conn:
            cursor = conn.execute(
                "SELECT task_id, severity, topic, source, created_at, "
                "due_date, target_page, user_request, age_days, "
                "same_type_count, user_promised, current_file, status, "
                "context, suggested_points "
                "FROM recap_tasks WHERE source = 'system' AND status = 'pending' "
                "ORDER BY CASE severity "
                "WHEN 'critical' THEN 1 WHEN 'high' THEN 2 "
                "WHEN 'medium' THEN 3 ELSE 4 END, created_at ASC"
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

    def get_recap_task(self, task_id: str) -> Optional[RecapTask]:
        """按 task_id 获取复盘任务。"""
        with sqlite3.connect(str(self._db_path), timeout=10) as conn:
            cursor = conn.execute(
                "SELECT task_id, severity, topic, source, created_at, "
                "due_date, target_page, user_request, age_days, "
                "same_type_count, user_promised, current_file, status, "
                "context, suggested_points "
                "FROM recap_tasks WHERE task_id = ?",
                (task_id,),
            )
            row = cursor.fetchone()
        return self._row_to_recap(row) if row else None

    def mark_recap_status(
        self,
        task_id: str,
        status: str,
        reason: str = "",
        actor: str = "system",
    ) -> bool:
        """公开的复盘任务状态更新入口。"""
        return self._update_status(task_id, status, reason=reason, actor=actor)

    def list_recap_tasks(
        self,
        status: str = "pending",
        severity: str = "",
        source: str = "",
        limit: int = 50,
    ) -> List[RecapTask]:
        """List recap tasks ordered by severity and age."""
        conditions = []
        params: List[str | int] = []
        if status and status != "all":
            conditions.append("status = ?")
            params.append(status)
        if severity:
            conditions.append("severity = ?")
            params.append(severity)
        if source:
            conditions.append("source = ?")
            params.append(source)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(max(1, int(limit or 50)))
        with sqlite3.connect(str(self._db_path), timeout=10) as conn:
            cursor = conn.execute(
                f"""
                SELECT task_id, severity, topic, source, created_at,
                       due_date, target_page, user_request, age_days,
                       same_type_count, user_promised, current_file, status,
                       context, suggested_points
                FROM recap_tasks
                {where}
                ORDER BY CASE severity
                    WHEN 'critical' THEN 1
                    WHEN 'high' THEN 2
                    WHEN 'medium' THEN 3
                    ELSE 4
                END, created_at ASC
                LIMIT ?
                """,  # nosec B608
                params,
            )
            return [self._row_to_recap(row) for row in cursor.fetchall()]

    def close_pending_recaps(
        self,
        status: str,
        *,
        severity: str = "",
        source: str = "system",
        reason: str = "",
        actor: str = "cli",
        limit: int | None = None,
    ) -> int:
        """Resolve or dismiss pending recap tasks with event records."""
        if status not in {"resolved", "dismissed", "ignored"}:
            raise ValueError(f"unsupported recap close status: {status}")
        tasks = self.list_recap_tasks(
            status="pending",
            severity=severity,
            source=source,
            limit=limit or 1000,
        )
        changed = 0
        for task in tasks[:limit or len(tasks)]:
            if self.mark_recap_status(task.task_id, status, reason=reason, actor=actor):
                changed += 1
        return changed

    @staticmethod
    def _is_user_active_time() -> bool:
        """粗略判断当前是否为用户活跃时段（08:00-22:00）。"""
        now = datetime.now()
        return 8 <= now.hour < 22

    # ============================================================
    # 启动补偿（蓝图 §9 关键边界）
    # ============================================================

    def startup_compensation(
        self, max_tasks: Optional[int] = None
    ) -> List[RecapTask]:
        """
        启动补偿：扫描已过期的 user_reminder 任务。

        用户电脑关机/盒盖期间过期的预约，开机后立即补发。
        用户预约：直接打开 Obsidian（不走权重）。
        系统提醒：走组合权重判断。

        Args:
            max_tasks: 本次最多处理任务数，防止停机后一次性弹出过多窗口。
        """
        now = datetime.now()
        expired = []
        max_tasks = max_tasks if max_tasks is not None else MAX_STARTUP_COMPENSATION_TASKS

        with sqlite3.connect(str(self._db_path), timeout=10) as conn:
            # 过期的用户预约
            cursor = conn.execute(
                "SELECT task_id, severity, topic, source, created_at, "
                "due_date, target_page, user_request, age_days, "
                "same_type_count, user_promised, current_file, status, "
                "context, suggested_points "
                "FROM recap_tasks "
                "WHERE source = 'user' AND status = 'pending' "
                "AND due_date <= ? "
                "ORDER BY due_date ASC "
                "LIMIT ?",
                (now.isoformat(), max_tasks),
            )
            user_expired = [self._row_to_recap(row) for row in cursor.fetchall()]

            # 过期的系统提醒（3天以上未处理）
            three_days_ago = (now - timedelta(days=3)).isoformat()
            cursor = conn.execute(
                "SELECT task_id, severity, topic, source, created_at, "
                "due_date, target_page, user_request, age_days, "
                "same_type_count, user_promised, current_file, status, "
                "context, suggested_points "
                "FROM recap_tasks "
                "WHERE source = 'system' AND status = 'pending' "
                "AND created_at <= ? "
                "ORDER BY created_at ASC "
                "LIMIT ?",
                (three_days_ago, max_tasks),
            )
            system_expired = [self._row_to_recap(row) for row in cursor.fetchall()]

        # [P1-38] 用户预约：仅在用户活跃时段自动打开，避免夜间/会议期间弹窗
        if self._is_user_active_time():
            for recap in user_expired:
                if open_obsidian(page_path=recap.target_page):
                    self._update_status(recap.task_id, "reminded")
                    logger.info("启动补偿 - 用户预约: %s", recap.topic)
                else:
                    logger.warning("启动补偿打开 Obsidian 失败，保持待提醒: %s", recap.topic)
            expired.extend(user_expired)
        else:
            logger.info("启动补偿 - 非活跃时段，跳过 %s 个用户预约弹窗", len(user_expired))

        # 系统提醒：走组合权重；总处理数不超过 max_tasks
        system_budget = max(0, max_tasks - len(user_expired))
        system_expired = system_expired[:system_budget]
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
                "due_date, target_page, user_request, age_days, "
                "same_type_count, user_promised, current_file, status, "
                "context, suggested_points "
                "FROM recap_tasks "
                "WHERE source = 'user' AND status = 'pending' "
                "AND due_date <= ?",
                (now.isoformat(),),
            )
            for row in cursor.fetchall():
                recap = self._row_to_recap(row)
                # 用户预约：直接打开
                opened = open_obsidian(page_path=recap.target_page)
                if opened:
                    self._update_status(recap.task_id, "reminded")
                decisions.append(
                    ForceDecision(
                        should_force_open=opened,
                        score=0,
                        reason="user_scheduled" if opened else "open_obsidian_failed",
                        channel="force_open" if opened else "dialog_reminder",
                    )
                )

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
        keywords = re.findall(r"[一-龥]+|[a-z]{2,}", topic_lower)
        return any(kw in file_lower for kw in keywords)

    def _count_same_type(self, conn: sqlite3.Connection, topic: str) -> int:
        """统计7天内同类问题出现次数"""
        seven_days_ago = (
            datetime.now() - timedelta(days=FORCED_RETROSPECTIVE_DURATION_BUCKET_WEEK_DAYS)
        ).isoformat()
        cursor = conn.execute(
            "SELECT COUNT(*) FROM recap_tasks "
            "WHERE topic = ? AND created_at >= ? AND status != 'cancelled'",
            (topic, seven_days_ago),
        )
        return cursor.fetchone()[0]  # type: ignore[no-any-return]

    def _update_status(
        self,
        task_id: str,
        status: str,
        reason: str = "",
        actor: str = "system",
    ) -> bool:
        """更新任务状态"""
        with sqlite3.connect(str(self._db_path), timeout=10) as conn:
            now = datetime.now().isoformat()
            context_suffix = ""
            if reason or actor != "system":
                context_suffix = (
                    f"\nrecap_status_update: status={status}; "
                    f"reason={reason}; actor={actor}; at={now}"
                )
            cursor = conn.execute(
                """
                UPDATE recap_tasks
                SET status = ?,
                    context = COALESCE(context, '') || ?
                WHERE task_id = ?
                """,
                (
                    status,
                    context_suffix,
                    task_id,
                ),
            )
            if cursor.rowcount:
                event_id = f"recap-task-event-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
                conn.execute(
                    """
                    INSERT INTO recap_task_events
                    (event_id, task_id, action, status, reason, actor, created_at)
                    VALUES (?, ?, 'status_update', ?, ?, ?, ?)
                    """,
                    (event_id, task_id, status, reason, actor, now),
                )
                return True
            return False

    @staticmethod
    def _row_to_recap(row) -> RecapTask:
        # Rows produced before context columns were introduced remain readable.
        n = len(row)
        return RecapTask(
            task_id=row[0],
            severity=row[1],
            topic=row[2],
            source=row[3],
            created_at=row[4],
            due_date=row[5],
            target_page=row[6],
            user_request=row[FORCED_RETROSPECTIVE__ROW_TO_RECAP_ROW],
            age_days=row[8] if n > 8 else 0,
            same_type_count=row[9] if n > 9 else 0,
            user_promised=bool(row[10]) if n > 10 else False,
            current_file=row[11] if n > 11 else "",
            status=row[12] if n > 12 else "pending",
            context=row[13] if n > 13 else "",
            suggested_points=row[14] if n > 14 else "",
        )
