# -*- coding: utf-8 -*-
"""Persistence for finalized retrospective records."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from core.app.retrospective_models import RETROSPECTIVE_SCHEMA, RetrospectiveRecord
from core.cognitive.decision_trace import (
    MaterialActionAuthorization,
    MaterialActionRequest,
    authorize_exact_project_contract_action,
    resolve_material_action_authorization,
)
from core.cognitive.state_contract import sha256_json
from core.config import get_config
from core.trust.markdown_adapter import read_markdown_text
from core.trust.models import sha256_text
from core.trust.vault_mutation_service import (
    TRUSTED_MARKDOWN_ACTION_TYPE,
    TRUSTED_MARKDOWN_EXECUTOR,
    TRUSTED_MARKDOWN_OWNER,
    trusted_markdown_material_action_binding,
)


RETROSPECTIVE_DECISION_CONTRACT_ID = (
    "project-contract:confirmed-retrospective-write"
)
RETROSPECTIVE_DECISION_CONTRACT_REVISION = (
    "mnemos.confirmed_retrospective_write.v1"
)
RETROSPECTIVE_DECISION_CONTRACT_TEXT = (
    "A user-confirmed retrospective may write only its exact validated record "
    "to its deterministic retrospective page."
)
RETROSPECTIVE_DECISION_PRODUCER_HASH = sha256_json(
    {
        "module": "core.app.retrospective_store",
        "producer": "authorize_confirmed_retrospective_write",
        "version": RETROSPECTIVE_DECISION_CONTRACT_REVISION,
    }
)


@dataclass(frozen=True)
class RetrospectiveWritePlan:
    """Exact deterministic Wiki mutation prepared before recap finalization."""

    page_path: Path
    target_path: Path
    content: str
    expected_existing_hash: str | None
    binding: Dict[str, str]


def authorize_confirmed_retrospective_write(
    record: RetrospectiveRecord,
    plan: RetrospectiveWritePlan,
    *,
    state_db_path: Path,
    confirmed_by_user: bool,
    source_agent: str,
) -> MaterialActionAuthorization:
    """Seal the exact confirmed recap decision before the Wiki sink runs."""

    if not confirmed_by_user or record.status != "confirmed":
        raise PermissionError("retrospective Wiki write requires user confirmation")
    request = MaterialActionRequest(
        owner=TRUSTED_MARKDOWN_OWNER,
        executor_id=TRUSTED_MARKDOWN_EXECUTOR,
        action_type=TRUSTED_MARKDOWN_ACTION_TYPE,
        target_ref=plan.binding["target_ref"],
        input_hash=plan.binding["input_hash"],
        expected_state_db=str(Path(state_db_path).resolve(strict=False)),
    )
    return authorize_exact_project_contract_action(
        expected_request=request,
        state_db_path=state_db_path,
        contract_id=RETROSPECTIVE_DECISION_CONTRACT_ID,
        contract_revision_id=RETROSPECTIVE_DECISION_CONTRACT_REVISION,
        contract_text=RETROSPECTIVE_DECISION_CONTRACT_TEXT,
        source_namespace="confirmed-retrospective-write",
        source_facts={
            "schema_version": "mnemos.confirmed_retrospective_write_facts.v1",
            "confirmed_by_user": True,
            "source_agent": str(source_agent or ""),
            "record": record.to_dict(),
            "page_path": plan.page_path.as_posix(),
            "target_path": str(plan.target_path.resolve(strict=False)),
            "content_hash": sha256_text(plan.content),
            "expected_existing_hash": str(plan.expected_existing_hash or ""),
        },
        decision_checks={
            "explicit_user_confirmation": (
                confirmed_by_user and record.status == "confirmed"
            ),
            "deterministic_plan_binding": bool(
                plan.binding.get("target_ref") and plan.binding.get("input_hash")
            ),
        },
        evidence_refs=tuple(
            dict.fromkeys(
                (
                    f"recap:{record.draft.recap_id}",
                    f"recap-task:{record.draft.task_id}",
                    *(str(ref) for ref in record.draft.evidence_refs),
                )
            )
        ),
        task=f"Finalize retrospective {record.draft.recap_id}",
        goal="Persist only the exact retrospective record confirmed by the user.",
        constraints=(
            "The recap must be in the confirmed state.",
            "The deterministic target, rendered content, and before hash cannot drift.",
        ),
        created_at=datetime.fromisoformat(record.reviewed_at).astimezone().isoformat(),
        producer="retrospective-finalize",
        producer_version=RETROSPECTIVE_DECISION_CONTRACT_REVISION,
        producer_code_hash=RETROSPECTIVE_DECISION_PRODUCER_HASH,
        evaluator_id="confirmed-retrospective-write-evaluator",
        approved_candidate_key="write_exact_user_confirmed_retrospective",
        approved_candidate_summary=(
            "Write the exact validated retrospective confirmed by the user."
        ),
        rejected_candidate_key="retain_unconfirmed_or_drifted_retrospective",
        rejected_candidate_summary=(
            "Reject an unconfirmed recap or a target/content revision that drifted."
        ),
        approved_reason_code="confirmed_retrospective_binding_verified",
        rejected_reason_code="confirmed_retrospective_binding_rejected",
        committed_metric="retrospective_page_commit_receipt",
        rejected_metric="unconfirmed_retrospective_write_count",
    )


class RetrospectiveStore:
    """Save finalized recaps into the Wiki and update recap task state."""

    def __init__(self, wiki_base: str | Path | None = None, db_path: str | Path | None = None):
        config = get_config()
        self.database_dir = Path(config.database_dir).expanduser()
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else config.wiki_dir
        self.db_path = Path(db_path).expanduser() if db_path else config.database_dir / "recap_tasks.db"
        self.last_trusted_push: Dict | None = None

    def save(
        self,
        record: RetrospectiveRecord,
        *,
        material_action: MaterialActionAuthorization | None = None,
    ) -> Path:
        """Compatibility wrapper returning the planned page path."""
        return Path(
            self.save_with_receipt(
                record,
                material_action=material_action,
            )["page_path"]
        )

    def prepare_write(self, record: RetrospectiveRecord) -> RetrospectiveWritePlan:
        """Build the exact target/content/before binding without writing."""

        page_path = self._page_path(record)
        abs_path = self.wiki_base / page_path
        frontmatter = self._frontmatter(record, str(page_path))
        body = self._body(record)
        content = self._render_page(frontmatter, body)
        expected_existing_hash = (
            sha256_text(read_markdown_text(abs_path))
            if abs_path.is_file()
            else None
        )
        binding = trusted_markdown_material_action_binding(
            target_path=abs_path,
            content=content,
            proposed_action="save_retrospective",
            expected_existing_hash=expected_existing_hash,
        )
        return RetrospectiveWritePlan(
            page_path=page_path,
            target_path=abs_path,
            content=content,
            expected_existing_hash=expected_existing_hash,
            binding=binding,
        )

    def save_with_receipt(
        self,
        record: RetrospectiveRecord,
        *,
        material_action: MaterialActionAuthorization | None = None,
        plan: RetrospectiveWritePlan | None = None,
    ) -> Dict:
        """Write or propose a recap and return its exact durable state."""
        prepared = plan or self.prepare_write(record)
        current = self.prepare_write(record)
        if prepared != current:
            raise PermissionError(
                "retrospective write plan changed after its material decision"
            )
        page_path = prepared.page_path
        abs_path = prepared.target_path
        content = prepared.content
        page_existed = abs_path.is_file()
        from core.trust.vault_mutation_service import (
            TrustedVaultMutationService,
            commit_trusted_markdown,
        )

        trusted_service = TrustedVaultMutationService(wiki_base=self.wiki_base)
        if material_action is None:
            state_db_path = (
                trusted_service.config.db_path.parent
                / "producer_consumer_ledger.db"
            ).resolve(strict=False)
            try:
                material_action, _ = resolve_material_action_authorization(
                    None,
                    owner=TRUSTED_MARKDOWN_OWNER,
                    executor_id=TRUSTED_MARKDOWN_EXECUTOR,
                    action_type=TRUSTED_MARKDOWN_ACTION_TYPE,
                    target_ref=prepared.binding["target_ref"],
                    input_hash=prepared.binding["input_hash"],
                    expected_state_db=state_db_path,
                )
            except PermissionError as exc:
                if "canonical material-action authorization is required" not in str(exc):
                    raise
                material_action = authorize_confirmed_retrospective_write(
                    record,
                    prepared,
                    state_db_path=state_db_path,
                    confirmed_by_user=record.status == "confirmed",
                    source_agent=record.source_agent or record.owner_agent,
                )
        trusted_push = trusted_service.submit_markdown(
            target_path=abs_path,
            content=content,
            source="retrospective_store",
            actor=record.owner_agent or record.source_agent or "system",
            source_session_id=record.session_id,
            evidence_refs=[
                ref
                for ref in [
                    f"recap:{record.draft.recap_id}",
                    f"task:{record.draft.task_id}",
                    *record.draft.evidence_refs,
                ]
                if ref
            ],
            proposed_action="save_retrospective",
            expected_existing_hash=prepared.expected_existing_hash,
            metadata={
                "entrypoint": "recap_finalize",
                "recap_id": record.draft.recap_id,
                "task_id": record.draft.task_id,
                "write_policy": record.write_policy,
            },
            material_action=material_action,
        )
        self.last_trusted_push = trusted_push.to_dict()
        if not trusted_push.intercepted:
            commit_trusted_markdown(
                trusted_push,
                target_path=abs_path,
                content=content,
                material_action=material_action,
            )
            from core.wiki_projection_lifecycle import WikiProjectionLedger

            WikiProjectionLedger(
                self.database_dir / "wiki_projection.db"
            ).record_mutation(
                abs_path,
                mutation_type="update" if page_existed else "create",
            )
            self._mark_recap_status(record.draft.task_id, "confirmed")
            status = "committed"
        else:
            status = "proposal_pending"
        return {
            "schema_version": "mnemos.retrospective_persist_receipt.v1",
            "status": status,
            "terminal": status == "committed",
            "page_path": str(page_path),
            "proposal_id": trusted_push.proposal_id,
            "trusted_push": trusted_push.to_dict(),
        }

    def mark_recap_confirmed(self, task_id: str) -> None:
        """Close the source recap task only after its page is committed."""
        self._mark_recap_status(task_id, "confirmed")

    def mark_action_status(
        self,
        recap_id: str,
        action_id: str,
        status: str,
        evidence: str = "",
    ) -> None:
        """Record an action status update in a lightweight SQLite table."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS recap_action_status (
                    recap_id TEXT NOT NULL,
                    action_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    evidence TEXT DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (recap_id, action_id)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO recap_action_status
                    (recap_id, action_id, status, evidence, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(recap_id, action_id) DO UPDATE SET
                    status = excluded.status,
                    evidence = excluded.evidence,
                    updated_at = excluded.updated_at
                """,
                (recap_id, action_id, status, evidence, datetime.now().isoformat()),
            )

    def _page_path(self, record: RetrospectiveRecord) -> Path:
        date = record.reviewed_at[:10] if record.reviewed_at else datetime.now().strftime("%Y-%m-%d")
        title_slug = self._slug(record.draft.title or record.draft.task_id)[:60]
        recap_slug = self._slug(record.draft.recap_id)[:32]
        slug = f"{title_slug}-{recap_slug}" if recap_slug else title_slug
        return Path("06-Retrospectives") / "复盘" / f"{date}-{slug}.md"

    @staticmethod
    def _slug(value: str) -> str:
        ascii_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower()).strip("-._")
        if ascii_slug:
            return ascii_slug[:80]
        chinese_slug = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", value.strip()).strip("-._")
        return (chinese_slug or "retrospective")[:80]

    @staticmethod
    def _frontmatter(record: RetrospectiveRecord, page_path: str) -> Dict:
        draft = record.draft
        action_items = [item.to_dict() for item in draft.action_items]
        completion_state = record.completion_state
        if draft.next_handling == "no_action_needed":
            completion_state = "no_action_needed"
        elif action_items:
            completion_state = "action_pending"
        frontmatter = {
            "mnemos_type": "retrospective",
            "schema": RETROSPECTIVE_SCHEMA,
            "title": draft.title,
            "recap_id": draft.recap_id,
            "task_id": draft.task_id,
            "status": record.status,
            "completion_state": completion_state,
            "source": record.source_agent or record.owner_agent or record.source,
            "recap_source": record.source,
            "source_agent": record.source_agent,
            "owner_agent": record.owner_agent,
            "source_agents": record.source_agents,
            "session_id": record.session_id,
            "project": record.project,
            "task_type": record.task_type,
            "subtype": record.subtype,
            "severity": record.severity,
            "write_policy": record.write_policy,
            "trigger_reason": record.trigger_reason,
            "created_at": draft.created_at,
            "reviewed_at": record.reviewed_at,
            "follow_up_at": record.follow_up_at,
            "goal": draft.goal,
            "actual": draft.actual,
            "delta": draft.delta,
            "next_handling": draft.next_handling,
            "no_action_reason": draft.no_action_reason,
            "root_type": draft.root_type,
            "root_confidence": 0.75,
            "lesson_quality": "draft" if draft.missing_fields else "validated",
            "action_count": len(action_items),
            "evidence_refs": draft.evidence_refs,
            "related_pages": record.related_pages,
            "activation_rules": draft.activation_rules,
            "consumption_targets": draft.consumption_targets,
            "consume_priority": "high" if record.severity in ("critical", "high") else "medium",
            "consume_cooldown_days": 7,
            "last_consumed_at": "",
            "consumption_outcomes": [],
            "page_path": page_path,
            "tags": ["mnemos/retrospective", "mnemos/forced"],
        }
        source_agent = str(record.source_agent or record.owner_agent or "").strip().lower()
        if source_agent:
            frontmatter.update(
                {
                    "scope": (
                        "project"
                        if record.project
                        else "private"
                        if record.session_id
                        else "agent"
                    ),
                    "source_agent": source_agent,
                    "acl_schema_version": 1,
                    "acl_metadata_complete": True,
                    "acl_reconciliation_status": "provenance_write",
                }
            )
        return frontmatter

    @staticmethod
    def _body(record: RetrospectiveRecord) -> str:
        draft = record.draft
        action_rows = RetrospectiveStore._action_rows(draft.action_items)
        activation_rules = json.dumps(draft.activation_rules, ensure_ascii=False, indent=2)
        consumption_targets = ", ".join(draft.consumption_targets) or "（暂无）"
        evidence_refs = "\n".join(f"- {ref}" for ref in draft.evidence_refs) or "- （暂无）"
        return "\n".join(
            [
                f"# {draft.title}",
                "",
                "## 一句话教训",
                "",
                draft.lesson or "（待补充）",
                "",
                "## 1. 目标与实际",
                "",
                f"- 目标：{draft.goal or '（待补充）'}",
                f"- 实际：{draft.actual or '（待补充）'}",
                f"- 差距：{draft.delta or '（待补充）'}",
                "- 影响：",
                "",
                "## 2. 事实证据",
                "",
                evidence_refs,
                "",
                "## 3. 根因分析",
                "",
                f"- 直接原因：{draft.root_cause or '（待补充）'}",
                f"- 根因类型：{', '.join(draft.root_type) if draft.root_type else '（待补充）'}",
                "- 系统因素：",
                "- 错误假设：",
                "- 可控因素：",
                "- 不可控因素：",
                "",
                "## 4. 下次行动",
                "",
                "| 行动 | Owner | 截止时间 | 验收指标 | 回看时间 | 状态 |",
                "|------|-------|----------|----------|----------|------|",
                action_rows,
                "",
                "## 5. 应固化到哪里",
                "",
                "- [ ] SOP / AGENTS.md / CLAUDE.md",
                "- [ ] 测试 / gate / lint / checklist",
                "- [ ] Wiki 概念页",
                "- [ ] 用户画像 / 偏好",
                "- [ ] 项目记忆",
                "",
                "## 6. 消费规则",
                "",
                f"- 消费目标：{consumption_targets}",
                "",
                "```json",
                activation_rules,
                "```",
                "",
                "## 7. 复盘后的认知更新",
                "",
                "- 我们之前的假设：",
                "- 这次证伪/强化了什么：",
                "- 以后 AI 应如何调整建议：",
                "",
                "## 8. 纠偏记录",
                "",
                "| 时间 | 反馈 | 原结论 | 修正后结论 | 证据 |",
                "|------|------|--------|------------|------|",
                "",
                "## 9. 回看记录",
                "",
                "- 第一次回看：",
                "- 是否有效：",
                "- 是否需要升级为规则：",
            ]
        )

    @staticmethod
    def _action_rows(action_items) -> str:
        if not action_items:
            return "| 无需行动 |  |  | 用户确认无需行动 |  | archived |"
        rows: List[str] = []
        for item in action_items:
            rows.append(
                "| {action} | {owner} | {deadline} | {metric} | {follow_up_at} | {status} |".format(
                    action=RetrospectiveStore._escape_table(item.action),
                    owner=RetrospectiveStore._escape_table(item.owner),
                    deadline=RetrospectiveStore._escape_table(item.deadline),
                    metric=RetrospectiveStore._escape_table(item.metric),
                    follow_up_at=RetrospectiveStore._escape_table(item.follow_up_at),
                    status=RetrospectiveStore._escape_table(item.status),
                )
            )
        return "\n".join(rows)

    @staticmethod
    def _escape_table(value: str) -> str:
        return str(value or "").replace("|", "\\|").replace("\n", "<br>")

    @staticmethod
    def _render_page(frontmatter: Dict, body: str) -> str:
        try:
            import yaml

            fm = yaml.safe_dump(
                frontmatter,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            ).rstrip()
        except ImportError:
            fm = "\n".join(f"{key}: {value}" for key, value in frontmatter.items())
        return f"---\n{fm}\n---\n\n{body}\n"

    def _mark_recap_status(self, task_id: str, status: str) -> None:
        if not task_id:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.execute(
                "UPDATE recap_tasks SET status = ? WHERE task_id = ?",
                (status, task_id),
            )
