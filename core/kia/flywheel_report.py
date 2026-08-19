"""Deterministic Wiki report writer for the cognitive decision flywheel."""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from core.cognitive.state_contract import sha256_json
from core.frontmatter import write_frontmatter
from core.trust.formal_markdown import (
    TrustedMarkdownDecisionPolicy,
    submit_or_write_markdown_with_decision,
)
from core.trust.markdown_adapter import read_markdown_text
from core.trust.models import sha256_text
from core.trust.vault_mutation_service import TrustedVaultMutationResult


FLYWHEEL_REPORT_MARKDOWN_POLICY = TrustedMarkdownDecisionPolicy(
    contract_id="project-contract:flywheel-cycle-report",
    contract_revision_id="mnemos.flywheel_cycle_report.v1",
    contract_text=(
        "The cognitive decision flywheel may write only the exact cycle report and "
        "execution summary rendered from one exact persisted cycle result."
    ),
    source_namespace="flywheel-cycle-report",
    producer="cognitive-decision-flywheel-report",
    producer_code_hash=sha256_json(
        {
            "module": "core.kia.flywheel_report",
            "producer": "write_flywheel_report",
            "version": "mnemos.flywheel_cycle_report.v1",
        }
    ),
    evaluator_id="flywheel-cycle-report-evaluator",
    constraints=(
        "Cycle result, action list, errors, target, preimage, and rendered bytes remain exact.",
        "The summary may be written only after the full report reached the formal Wiki.",
    ),
    approved_candidate_key="write_exact_flywheel_report_artifact",
    approved_candidate_summary="Write the exact flywheel cycle report artifact.",
    rejected_candidate_key="retain_flywheel_report_state",
    rejected_candidate_summary="Retain report state when cycle facts or bytes drift.",
    approved_reason_code="flywheel_report_binding_verified",
    rejected_reason_code="flywheel_report_binding_rejected",
    committed_metric="flywheel_report_artifact_committed",
    rejected_metric="unbound_flywheel_report_artifact_count",
)


@dataclass(frozen=True)
class FlywheelReportWriteResult:
    """Paths exist only for writes that actually reached the formal Wiki."""

    report_path: Optional[Path]
    report_receipt: TrustedVaultMutationResult
    summary_path: Optional[Path] = None
    summary_receipt: Optional[TrustedVaultMutationResult] = None


def write_flywheel_report(
    *,
    wiki_base: Path,
    db_path: Path | str,
    results: Dict[str, Any],
    render_cycle: Callable[[Dict[str, Any]], str],
) -> FlywheelReportWriteResult:
    """Write the full cycle report and, when applicable, its action summary."""

    report_dir = wiki_base / "06-Retrospectives" / "flywheel"
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    report_path = report_dir / f"flywheel_report_{today}.md"
    executed = results.get("executed", {})
    actions = executed.get("actions", [])
    errors = executed.get("errors", [])
    rendered_cycle = render_cycle(results)
    lines = [rendered_cycle, "", "---", "", "## 自动执行日志", ""]
    if actions:
        lines.append(f"本次自动执行 {len(actions)} 项操作：")
        lines.extend(f"{index}. {action}" for index, action in enumerate(actions, 1))
    else:
        lines.append("本次无自动执行操作。")
    if errors:
        lines.extend(["", "### 执行错误", ""])
        lines.extend(f"- ❌ {error}" for error in errors)
    report_content = write_frontmatter(
            {
                "mnemos_type": "system_report",
                "report_type": "cognitive_decision_flywheel",
                "report_id": f"flywheel-report-{today}",
                "generated_at": now.isoformat(),
                "source_count": 1,
                "sources": [f"sqlite:{db_path}"],
                "evidence_level": "single",
                "knowledge_stage": "P2",
                "status": "active",
            },
            "\n".join(lines) + "\n",
        )
    existing_report = (
        read_markdown_text(report_path) if report_path.is_file() else None
    )
    report_receipt = submit_or_write_markdown_with_decision(
        decision_policy=FLYWHEEL_REPORT_MARKDOWN_POLICY,
        decision_facts={
            "schema_version": "mnemos.flywheel_cycle_report_facts.v1",
            "db_path": str(db_path),
            "report_date": today,
            "result_sections": sorted(str(key) for key in results),
            "rendered_cycle_hash": sha256_text(rendered_cycle),
            "actions": [str(action) for action in actions],
            "errors": [str(error) for error in errors],
            "artifact_kind": "cycle_report",
        },
        decision_task=f"Write cognitive flywheel cycle report for {today}",
        decision_goal="Publish the exact persisted flywheel cycle result for audit.",
        decision_created_at=now.isoformat(),
        wiki_base=wiki_base,
        target_path=report_path,
        content=report_content,
        source="cognitive_decision_flywheel",
        actor="system",
        evidence_refs=[f"sqlite:{db_path}"],
        proposed_action="write_cycle_report",
        expected_existing_hash=(
            sha256_text(existing_report) if existing_report is not None else None
        ),
        metadata={"report_type": "cognitive_decision_flywheel"},
    )
    written_report = report_path if not report_receipt.intercepted and report_path.is_file() else None
    summary_path: Optional[Path] = None
    summary_receipt: Optional[TrustedVaultMutationResult] = None
    if actions and written_report is not None:
        summary_path = report_dir / f"flywheel_execution_{today}.md"
        summary_lines = [
            f"# Flywheel 执行摘要 {today}",
            "",
            f"- 周期时间: {now.strftime('%Y-%m-%d %H:%M')}",
            f"- 执行操作数: {len(actions)}",
            f"- 错误数: {len(errors)}",
            "",
            "## 操作清单",
            "",
            *(f"- {action}" for action in actions),
            "",
            "## 验证与追踪",
            "",
            f"- 完整周期报告：[[{report_path.stem}]]",
            "- 操作数来自本轮实际执行结果；错误数只统计执行阶段返回的失败，不包含尚未进入执行器的候选。",
            "- 每项自动操作仍需在目标文件或状态库中留下可核验结果；仅出现在本摘要中不视为完成。",
            "- 若错误数大于零，应先查看完整报告中的错误明细并重试失败动作，不得以删除摘要或降低门禁方式清零。",
            "- 本页用于快速确认本轮发生了什么；因果证据、候选输入和决策理由以完整周期报告为准。",
        ]
        summary_content = write_frontmatter(
                {
                    "mnemos_type": "system_report",
                    "report_type": "cognitive_decision_flywheel_summary",
                    "report_id": f"flywheel-execution-{today}",
                    "generated_at": now.isoformat(),
                    "source_count": 1,
                    "sources": [f"wiki:{report_path.relative_to(wiki_base).as_posix()}"],
                    "evidence_level": "single",
                    "knowledge_stage": "P2",
                    "status": "active",
                },
                "\n".join(summary_lines) + "\n",
            )
        existing_summary = (
            read_markdown_text(summary_path) if summary_path.is_file() else None
        )
        summary_receipt = submit_or_write_markdown_with_decision(
            decision_policy=FLYWHEEL_REPORT_MARKDOWN_POLICY,
            decision_facts={
                "schema_version": "mnemos.flywheel_cycle_report_facts.v1",
                "db_path": str(db_path),
                "report_path": str(report_path),
                "report_hash": sha256_text(report_content),
                "actions": list(actions),
                "errors": list(errors),
                "artifact_kind": "execution_summary",
            },
            decision_task=f"Write cognitive flywheel execution summary for {today}",
            decision_goal="Publish the exact action summary linked to its committed report.",
            decision_created_at=now.isoformat(),
            wiki_base=wiki_base,
            target_path=summary_path,
            content=summary_content,
            source="cognitive_decision_flywheel",
            actor="system",
            evidence_refs=[f"wiki:{report_path.relative_to(wiki_base).as_posix()}"],
            proposed_action="write_cycle_summary",
            expected_existing_hash=(
                sha256_text(existing_summary) if existing_summary is not None else None
            ),
            metadata={"report_type": "cognitive_decision_flywheel_summary"},
        )
        if summary_receipt.intercepted or not summary_path.is_file():
            summary_path = None
    return FlywheelReportWriteResult(
        report_path=written_report,
        report_receipt=report_receipt,
        summary_path=summary_path,
        summary_receipt=summary_receipt,
    )
