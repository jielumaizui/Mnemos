# -*- coding: utf-8 -*-
"""
EvolutionTracker — 知识演化跟踪

TemporalEvolutionTracker  — 版本绑定 + 上下文时间范围检测
RecirculationGuard       — 防止 Wiki 引用内容再次蒸馏
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.config import get_config
from core.cognitive.state_contract import sha256_json
from core.frontmatter import parse_frontmatter
from core.trust.formal_markdown import (
    TrustedMarkdownDecisionPolicy,
    submit_or_write_markdown_with_decision,
)
from core.trust.markdown_adapter import read_markdown_text
from core.trust.models import sha256_text

# Constants extracted from magic numbers
TEMPORAL_SCOPE_DURATION_BUCKET_YEAR_DAYS = 365
TEMPORAL_SCOPE_DURATION_BUCKET_QUARTER_DAYS = 90
TEMPORAL_EVOLUTION_TRACKER_DURATION_BUCKET_MONTH_DAYS = 30
TEMPORAL_EVOLUTION_TRACKER_DURATION_BUCKET_HALF_YEAR_DAYS = 180
TEMPORAL_EVOLUTION_TRACKER__SHOULD_TRIGGER_PROACTIVE_DISTILL_LAST_DAYS = 30
logger = logging.getLogger(__name__)

EVOLUTION_STALE_MARKDOWN_POLICY = TrustedMarkdownDecisionPolicy(
    contract_id="project-contract:evolution-stale-marker",
    contract_revision_id="mnemos.evolution_stale_marker.v1",
    contract_text=(
        "TemporalEvolutionTracker may mark one exact Wiki page stale only from an "
        "exact evolution alert produced for that page and entity."
    ),
    source_namespace="evolution-stale-marker",
    producer="temporal-evolution-tracker",
    producer_code_hash=sha256_json(
        {
            "module": "core.hephaestus.evolution_tracker",
            "producer": "mark_page_stale",
            "version": "mnemos.evolution_stale_marker.v1",
        }
    ),
    evaluator_id="evolution-stale-marker-evaluator",
    constraints=(
        "Target, page preimage, alert type, entity, detail, and output bytes remain exact.",
        "The marker may not upgrade the alert beyond the tracker observation.",
    ),
    approved_candidate_key="apply_exact_stale_marker",
    approved_candidate_summary="Apply the exact evolution alert as page metadata.",
    rejected_candidate_key="retain_page_without_stale_marker",
    rejected_candidate_summary="Retain the page when alert or page binding drifts.",
    approved_reason_code="evolution_alert_binding_verified",
    rejected_reason_code="evolution_alert_binding_rejected",
    committed_metric="evolution_stale_marker_committed",
    rejected_metric="unbound_evolution_stale_marker_count",
)

EVOLUTION_REPORT_MARKDOWN_POLICY = TrustedMarkdownDecisionPolicy(
    contract_id="project-contract:evolution-report-artifact",
    contract_revision_id="mnemos.evolution_report_artifact.v1",
    contract_text=(
        "TemporalEvolutionTracker may publish only the exact report or knowledge-gap "
        "preload artifact rendered from its exact current alert set."
    ),
    source_namespace="evolution-report-artifact",
    producer="temporal-evolution-tracker",
    producer_code_hash=sha256_json(
        {
            "module": "core.hephaestus.evolution_tracker",
            "producers": ["_write_evolution_report", "_write_knowledge_gaps"],
            "version": "mnemos.evolution_report_artifact.v1",
        }
    ),
    evaluator_id="evolution-report-artifact-evaluator",
    constraints=(
        "Alert set, artifact kind, target preimage, and rendered bytes remain exact.",
        "Knowledge-gap preload content may not invent a gap absent from the alert set.",
    ),
    approved_candidate_key="publish_exact_evolution_artifact",
    approved_candidate_summary="Publish the exact artifact rendered from evolution alerts.",
    rejected_candidate_key="retain_evolution_artifact_state",
    rejected_candidate_summary="Retain artifact state when alert facts or bytes drift.",
    approved_reason_code="evolution_artifact_binding_verified",
    rejected_reason_code="evolution_artifact_binding_rejected",
    committed_metric="evolution_artifact_committed",
    rejected_metric="unbound_evolution_artifact_count",
)


def _evolution_alert_facts(alerts: List[EvolutionAlert]) -> list[dict[str, object]]:
    return [
        {
            "entity": alert.entity,
            "alert_type": alert.alert_type,
            "detail": alert.detail,
            "wiki_page": alert.wiki_page,
            "severity": alert.severity,
            "created_at": alert.created_at,
        }
        for alert in alerts
    ]


def _get_db_path() -> Path:
    return get_config().database_dir / "wiki_state.db"


# ========== 数据模型 ==========


@dataclass
class EvolutionAlert:
    """演化告警"""

    entity: str
    alert_type: str  # version_outdated / context_expired / rarely_accessed / contradicted
    detail: str = ""
    wiki_page: str = ""
    severity: float = 0.5  # 0-1
    created_at: str = ""


@dataclass
class TemporalScope:
    """时效范围"""

    scope_type: str  # permanent / stable / version-bound / contextual
    version: str = ""
    context_date: str = ""
    expires_after_days: int = 0  # 0 = never

    @property
    def is_expired(self) -> bool:
        if self.scope_type == "permanent":
            return False
        if self.scope_type == "stable":
            return False
        if not self.context_date:
            return False
        try:
            created = datetime.fromisoformat(self.context_date)
            age_days = (datetime.now() - created).days
            if self.scope_type == "version-bound":
                return (
                    age_days > TEMPORAL_SCOPE_DURATION_BUCKET_YEAR_DAYS
                )  # 版本绑定知识 1 年后标记过期
            if self.scope_type == "contextual":
                return (
                    age_days > self.expires_after_days
                    if self.expires_after_days > 0
                    else age_days > TEMPORAL_SCOPE_DURATION_BUCKET_QUARTER_DAYS
                )
        except ValueError:
            logging.getLogger(__name__).warning("Caught unexpected error", exc_info=True)
        return False


# ========== TemporalEvolutionTracker ==========


class TemporalEvolutionTracker:
    """时间演化跟踪器

    检测知识的时效性变化：
    1. 版本绑定检查（v1.x 相关知识在新版本下可能失效）
    2. 上下文过期检测（90 天无访问的上下文知识标记过期）
    3. 30 天无访问衰减
    """

    ALERT_TABLE = """
        CREATE TABLE IF NOT EXISTS evolution_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity TEXT NOT NULL,
            alert_type TEXT NOT NULL,
            detail TEXT DEFAULT '',
            wiki_page TEXT DEFAULT '',
            severity REAL DEFAULT 0.5,
            created_at TEXT,
            resolved INTEGER DEFAULT 0
        )
    """

    PROACTIVE_DISTILL_TABLE = """
        CREATE TABLE IF NOT EXISTS proactive_distill_log (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_triggered TEXT NOT NULL,
            trigger_reason TEXT DEFAULT '',
            entity TEXT DEFAULT ''
        )
    """

    def __init__(self):
        self._db_path = _get_db_path()
        self._init_db()

    def _init_db(self):
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self._db_path), timeout=5) as conn:
            conn.execute(self.ALERT_TABLE)
            conn.execute(self.PROACTIVE_DISTILL_TABLE)
            conn.commit()

    def check_entity_freshness(
        self, entity: str, wiki_page: Path, recent_sessions: List[Dict] | None = None
    ) -> Optional[EvolutionAlert]:
        """检查实体知识新鲜度

        Args:
            entity: 实体名称
            wiki_page: Wiki 页面路径
            recent_sessions: 近期会话列表（用于检测版本更新）

        Returns:
            EvolutionAlert 如果知识可能过时，否则 None
        """
        try:
            content = wiki_page.read_text(encoding="utf-8")
        except (OSError, IOError):
            logging.getLogger(__name__).warning(
                "Caught unexpected error at evolution_tracker.py", exc_info=True
            )
            return None

        scope = self._extract_temporal_scope(content)
        if not scope:
            return None

        # 版本绑定检查
        if scope.scope_type == "version-bound" and scope.is_expired:
            return EvolutionAlert(
                entity=entity,
                alert_type="version_outdated",
                detail=f"版本绑定知识已超过 1 年（版本: {scope.version}）",
                wiki_page=str(wiki_page),
                severity=0.7,
                created_at=datetime.now().isoformat(),
            )

        # 上下文过期检查
        if scope.scope_type == "contextual" and scope.is_expired:
            return EvolutionAlert(
                entity=entity,
                alert_type="context_expired",
                detail=f"上下文知识可能已过时（创建于 {scope.context_date}）",
                wiki_page=str(wiki_page),
                severity=0.5,
                created_at=datetime.now().isoformat(),
            )

        # 最近会话中是否有版本升级信号
        if recent_sessions and scope.version:
            for session in recent_sessions[-5:]:
                session_text = session.get("content", "")
                upgrade_pattern = rf'{re.escape(scope.version.split(".")[0])}\.\d+'
                if re.search(r"(升级|迁移|更新|upgrade|migrate|update)", session_text, re.I):
                    newer_versions = re.findall(upgrade_pattern, session_text)
                    if newer_versions and newer_versions[0] != scope.version:
                        return EvolutionAlert(
                            entity=entity,
                            alert_type="version_outdated",
                            detail=f"检测到新版本 {newer_versions[0]}（当前: {scope.version}）",
                            wiki_page=str(wiki_page),
                            severity=0.8,
                            created_at=datetime.now().isoformat(),
                        )

        # 30 天无访问衰减
        access_time = self._get_last_access(wiki_page)
        if access_time:
            age_days = (datetime.now() - access_time).days
            if age_days > TEMPORAL_EVOLUTION_TRACKER_DURATION_BUCKET_MONTH_DAYS:
                return EvolutionAlert(
                    entity=entity,
                    alert_type="rarely_accessed",
                    detail=f"知识页面 {age_days} 天未被访问",
                    wiki_page=str(wiki_page),
                    severity=min(
                        0.6, age_days / TEMPORAL_EVOLUTION_TRACKER_DURATION_BUCKET_HALF_YEAR_DAYS
                    ),
                    created_at=datetime.now().isoformat(),
                )

        return None

    def scan_all_pages(self, wiki_dir: Path) -> List[EvolutionAlert]:
        """扫描所有 Wiki 页面，检测过时知识并驱动系统调整

        修复后行为：
        1. 检测过时知识 → 标记 stale + 写报告 + git 留痕
        2. 检测知识缺口 → 写 KIA 预加载提示（不生成蒸馏任务）
        3. 发射 knowledge_stale 事件到 EventBus
        4. 每月最多一次主动蒸馏标记（覆盖率 < 10% + 30 天无对话）
        """
        alerts = []
        stale_pages = []
        gap_pages = []
        total_pages = 0

        from core.utils import WIKI_DIRS

        for subdir in WIKI_DIRS:
            md_dir = wiki_dir / subdir
            if not md_dir.exists():
                continue
            for md_file in md_dir.glob("*.md"):
                total_pages += 1
                entity = md_file.stem
                alert = self.check_entity_freshness(entity, md_file)
                if alert:
                    alerts.append(alert)
                    self._save_alert(alert)
                    # ── 驱动闭环：标记 stale ──
                    if alert.alert_type in ("version_outdated", "context_expired"):
                        if self._mark_stale(md_file, alert, wiki_dir):
                            stale_pages.append(alert)
                    elif alert.alert_type == "rarely_accessed":
                        gap_pages.append(alert)

        # ── 写入演化报告 ──
        if alerts:
            self._write_evolution_report(wiki_dir, alerts)

        # ── 知识缺口提示（预加载，不生成蒸馏任务）──
        if gap_pages:
            self._write_knowledge_gaps(wiki_dir, gap_pages)
            # ── 例外：每月最多一次主动蒸馏标记 ──
            if self._should_trigger_proactive_distill(len(gap_pages), total_pages):
                # 取最严重的一个触发主动蒸馏标记
                top_gap = max(gap_pages, key=lambda a: a.severity)
                self._record_proactive_distill(
                    f"coverage_low_{len(gap_pages)}_of_{total_pages}",
                    top_gap.entity,
                )
                logger.info(
                    "[EvolutionTracker] 触发主动蒸馏标记: %s (覆盖率 %.1f%%)",
                    top_gap.entity,
                    (len(gap_pages) / total_pages * 100) if total_pages else 0,
                )

        # ── 发射 EventBus 事件 ──
        if stale_pages:
            try:
                from core.mnemos_bus import publish_event

                publish_event(
                    "knowledge_stale",
                    "evolution",
                    {
                        "stale_count": len(stale_pages),
                        "entities": [a.entity for a in stale_pages],
                        "wiki_pages": [a.wiki_page for a in stale_pages],
                        "alert_types": list(set(a.alert_type for a in stale_pages)),
                    },
                )
            except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
                logger.debug("EventBus 发射 knowledge_stale 失败", exc_info=True)

        # ── git 自动留痕 ──
        if stale_pages:
            self._git_commit_stale(wiki_dir, stale_pages)

        # ── 记录 AdaptiveConfig 指标 ──
        try:
            if total_pages > 0:
                stale_rate = len(stale_pages) / total_pages
                from core.kia.adaptive_config import AdaptiveConfig

                ac = AdaptiveConfig()
                ac.record_usage("knowledge_graph", "stale_page_rate", stale_rate)
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
            logger.debug("AdaptiveConfig 指标记录失败", exc_info=True)

        return alerts

    def _should_trigger_proactive_distill(self, gap_count: int, total_pages: int) -> bool:
        """判断是否应触发主动蒸馏标记

        条件：覆盖率 < 10%（gap 占比高）+ 过去 30 天无触发记录
        每月最多一次。
        """
        if total_pages == 0 or (gap_count / total_pages) < 0.10:
            return False
        try:
            with sqlite3.connect(str(self._db_path), timeout=5) as conn:
                cursor = conn.execute(
                    "SELECT last_triggered FROM proactive_distill_log WHERE id = 1"
                )
                row = cursor.fetchone()
                if row is None:
                    return True
                last = datetime.fromisoformat(row[0])
                return (
                    datetime.now() - last
                ).days >= TEMPORAL_EVOLUTION_TRACKER__SHOULD_TRIGGER_PROACTIVE_DISTILL_LAST_DAYS
        except (sqlite3.Error, OSError, ValueError):
            logger.debug("检查主动蒸馏限频失败", exc_info=True)
            return False

    def _record_proactive_distill(self, reason: str, entity: str):
        """记录主动蒸馏触发时间"""
        try:
            with sqlite3.connect(str(self._db_path), timeout=5) as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO proactive_distill_log
                       (id, last_triggered, trigger_reason, entity)
                       VALUES (1, ?, ?, ?)""",
                    (datetime.now().isoformat(), reason, entity),
                )
                conn.commit()
        except (sqlite3.Error, OSError):
            logger.debug("记录主动蒸馏触发失败", exc_info=True)

    def _git_commit_stale(self, wiki_dir: Path, stale_pages: List[EvolutionAlert]):
        """对 stale 标记自动执行 git commit 留痕"""
        try:
            import subprocess

            # 检查是否在 git 仓库中
            result = subprocess.run(
                ["git", "-C", str(wiki_dir), "rev-parse", "--git-dir"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return

            # 检查是否有变更
            result = subprocess.run(
                ["git", "-C", str(wiki_dir), "diff", "--cached", "--name-only"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            staged = result.stdout.strip()
            if not staged:
                # 尝试 add 被标记的文件
                for alert in stale_pages:
                    if alert.wiki_page:
                        subprocess.run(
                            ["git", "-C", str(wiki_dir), "add", alert.wiki_page],
                            capture_output=True,
                            timeout=5,
                        )
                # 再次检查
                result = subprocess.run(
                    ["git", "-C", str(wiki_dir), "diff", "--cached", "--name-only"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if not result.stdout.strip():
                    return

            # 提交
            entities = ", ".join(a.entity for a in stale_pages[:3])
            msg = f"auto: mark {len(stale_pages)} page(s) as stale [{entities}]"
            subprocess.run(
                ["git", "-C", str(wiki_dir), "commit", "-m", msg],
                capture_output=True,
                timeout=10,
            )
            logger.info("[EvolutionTracker] git commit stale 标记: %s", msg)
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
            logger.debug("git 留痕失败", exc_info=True)

    def _mark_stale(self, wiki_page: Path, alert: EvolutionAlert, wiki_dir: Path) -> bool:
        """在 wiki 页面 frontmatter 中标记 stale: true

        Returns:
            True 如果成功标记，False 如果失败或已标记
        """
        try:
            content = wiki_page.read_text(encoding="utf-8")
            fm, body = parse_frontmatter(content)
            if fm is None:
                fm = {}
            # 已标记则跳过
            if fm.get("stale") is True:
                return False

            fm["stale"] = True
            fm["stale_warning"] = alert.detail
            fm["stale_at"] = datetime.now().isoformat()
            fm["stale_alert_type"] = alert.alert_type

            # 重建 frontmatter
            yaml_lines = ["---"]
            for k, v in fm.items():
                if isinstance(v, bool):
                    yaml_lines.append(f"{k}: {str(v).lower()}")
                elif isinstance(v, (list, tuple)):
                    yaml_lines.append(f"{k}:")
                    for item in v:
                        yaml_lines.append(f"  - {item}")
                else:
                    yaml_lines.append(f"{k}: {v}")
            yaml_lines.append("---")

            new_content = "\n".join(yaml_lines) + "\n" + body
            evidence_refs = [f"wiki_page:{wiki_page.relative_to(wiki_dir)}"]
            submit_or_write_markdown_with_decision(
                decision_policy=EVOLUTION_STALE_MARKDOWN_POLICY,
                decision_facts={
                    "schema_version": "mnemos.evolution_stale_marker_facts.v1",
                    "alert": {
                        "entity": alert.entity,
                        "alert_type": alert.alert_type,
                        "detail": alert.detail,
                        "severity": alert.severity,
                        "created_at": alert.created_at,
                    },
                },
                decision_task=f"Mark stale Wiki page {wiki_page.name}",
                decision_goal="Expose the exact temporal evolution warning on its source page.",
                decision_created_at=datetime.now(timezone.utc).isoformat(),
                wiki_base=wiki_dir,
                target_path=wiki_page,
                content=new_content,
                source="evolution_tracker",
                actor="system",
                evidence_refs=evidence_refs,
                proposed_action="mark_stale",
                expected_existing_hash=sha256_text(content),
                metadata={"alert_type": alert.alert_type, "entity": alert.entity},
            )
            logger.info("[EvolutionTracker] 标记 stale: %s (%s)", wiki_page.name, alert.alert_type)
            return True
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
            logger.warning("[EvolutionTracker] 标记 stale 失败: %s", wiki_page, exc_info=True)
            return False

    def _write_evolution_report(self, wiki_dir: Path, alerts: List[EvolutionAlert]):
        """将检测结果写入 wiki/06-Retrospectives/evolution/ 报告"""
        try:
            report_dir = wiki_dir / "06-Retrospectives" / "evolution"
            report_dir.mkdir(parents=True, exist_ok=True)

            today = datetime.now().strftime("%Y-%m-%d")
            report_file = report_dir / f"evolution_report_{today}.md"

            lines = [
                "# 知识演化报告",
                "",
                f"生成时间: {datetime.now().isoformat()}",
                f"检测总数: {len(alerts)}",
                "",
                "| 实体 | 告警类型 | 严重度 | 详情 |",
                "|------|---------|--------|------|",
            ]
            for a in alerts:
                lines.append(f"| {a.entity} | {a.alert_type} | {a.severity:.1f} | {a.detail} |")

            lines.extend(
                [
                    "",
                    "## 已自动处理项",
                    "",
                    "- 过期知识已自动标记 `stale: true`",
                    "- 知识缺口已生成预加载提示",
                    "",
                ]
            )

            existing_content = (
                read_markdown_text(report_file)
                if report_file.is_file()
                else None
            )
            rendered_content = "\n".join(lines)
            alert_facts = _evolution_alert_facts(alerts)
            evidence_refs = [
                f"evolution-alert-set:{sha256_json(alert_facts)}",
                *(
                    f"evolution-alert:{alert.entity}:{alert.alert_type}"
                    for alert in alerts
                ),
            ]
            result = submit_or_write_markdown_with_decision(
                decision_policy=EVOLUTION_REPORT_MARKDOWN_POLICY,
                decision_facts={
                    "schema_version": "mnemos.evolution_report_facts.v1",
                    "artifact_kind": "evolution_report",
                    "report_date": today,
                    "alerts": alert_facts,
                },
                decision_task=f"Write knowledge evolution report for {today}",
                decision_goal="Publish the exact current evolution-alert denominator.",
                decision_created_at=datetime.now(timezone.utc).isoformat(),
                wiki_base=wiki_dir,
                target_path=report_file,
                content=rendered_content,
                source="evolution_tracker",
                actor="system",
                evidence_refs=evidence_refs,
                proposed_action="write_evolution_report",
                expected_existing_hash=(
                    sha256_text(existing_content)
                    if existing_content is not None
                    else None
                ),
                metadata={"alert_count": len(alerts)},
            )
            if result.intercepted:
                logger.info(
                    "[EvolutionTracker] 演化报告已提交 proposal: %s",
                    result.proposal_id,
                )
            else:
                logger.info("[EvolutionTracker] 演化报告已写入: %s", report_file)
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
            logger.warning("[EvolutionTracker] 写演化报告失败", exc_info=True)

    def _write_knowledge_gaps(self, wiki_dir: Path, gap_alerts: List[EvolutionAlert]):
        """生成知识缺口预加载提示，写入 06-Retrospectives/knowledge_gaps.md

        注意：不生成蒸馏任务。缺口由 Agent 在下次对话中引导补充。
        """
        try:
            gaps_file = wiki_dir / "06-Retrospectives" / "knowledge_gaps.md"
            gaps_file.parent.mkdir(parents=True, exist_ok=True)

            lines = [
                "# 知识缺口预加载提示",
                "",
                f"更新时间: {datetime.now().isoformat()}",
                f"缺口数量: {len(gap_alerts)}",
                "",
                "> 本文件由 EvolutionTracker 自动生成，供 KIA preflight_inject 加载。",
                "> 当用户与以下领域相关对话时，Agent 可主动引导补充知识。",
                "",
            ]

            for alert in gap_alerts:
                lines.extend(
                    [
                        f"## {alert.entity}",
                        "",
                        f"- **缺口类型**: {alert.alert_type}",
                        f"- **详情**: {alert.detail}",
                        f"- **建议引导方向**: 询问用户关于 `{alert.entity}` 的最新进展或实践经验",
                        "",
                    ]
                )

            lines.extend(
                [
                    "---",
                    "",
                    "## 使用方式",
                    "",
                    "1. `preflight_inject` 在加载知识时读取本文件",
                    "2. 若用户当前对话涉及相关 domain，提示 Agent 主动引导",
                    "3. 对话自然被 CaptureWorker 捕获 → 蒸馏入库 → 缺口填补",
                    "",
                ]
            )

            existing_content = (
                read_markdown_text(gaps_file) if gaps_file.is_file() else None
            )
            rendered_content = "\n".join(lines)
            gap_facts = _evolution_alert_facts(gap_alerts)
            evidence_refs = [
                f"knowledge-gap-set:{sha256_json(gap_facts)}",
                *(
                    f"knowledge-gap:{alert.entity}:{alert.alert_type}"
                    for alert in gap_alerts
                ),
            ]
            result = submit_or_write_markdown_with_decision(
                decision_policy=EVOLUTION_REPORT_MARKDOWN_POLICY,
                decision_facts={
                    "schema_version": "mnemos.evolution_report_facts.v1",
                    "artifact_kind": "knowledge_gap_preload",
                    "alerts": gap_facts,
                },
                decision_task="Write the knowledge-gap preload artifact",
                decision_goal="Publish only the exact current knowledge-gap alert set.",
                decision_created_at=datetime.now(timezone.utc).isoformat(),
                wiki_base=wiki_dir,
                target_path=gaps_file,
                content=rendered_content,
                source="evolution_tracker",
                actor="system",
                evidence_refs=evidence_refs,
                proposed_action="write_knowledge_gap_preload",
                expected_existing_hash=(
                    sha256_text(existing_content)
                    if existing_content is not None
                    else None
                ),
                metadata={"gap_count": len(gap_alerts)},
            )
            if result.intercepted:
                logger.info(
                    "[EvolutionTracker] 知识缺口提示已提交 proposal: %s",
                    result.proposal_id,
                )
            else:
                logger.info("[EvolutionTracker] 知识缺口提示已写入: %s", gaps_file)
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
            logger.warning("[EvolutionTracker] 写知识缺口提示失败", exc_info=True)

    def _extract_temporal_scope(self, content: str) -> Optional[TemporalScope]:
        """从页面内容提取时效范围"""
        # 解析 frontmatter
        fm, _ = parse_frontmatter(content)
        if not fm:
            return None

        temporal = fm.get("时效性", fm.get("temporal", ""))
        version = fm.get("版本标记", fm.get("version", ""))
        created = fm.get("创建日期", fm.get("created", ""))

        if not temporal:
            return None

        scope_type = temporal.lower().replace("-", "_")
        if scope_type not in ("permanent", "stable", "version_bound", "contextual"):
            return None

        return TemporalScope(
            scope_type=scope_type,
            version=version,
            context_date=created,
        )

    @staticmethod
    def _get_last_access(page: Path) -> Optional[datetime]:
        """获取最后访问时间（使用 mtime 近似）"""
        try:
            mtime = page.stat().st_mtime
            return datetime.fromtimestamp(mtime)
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
            logging.getLogger(__name__).warning(
                "Caught unexpected error at evolution_tracker.py", exc_info=True
            )
            return None

    def _save_alert(self, alert: EvolutionAlert):
        try:
            with sqlite3.connect(str(self._db_path), timeout=5) as conn:
                conn.execute(
                    """INSERT INTO evolution_alerts
                       (entity, alert_type, detail, wiki_page, severity, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        alert.entity,
                        alert.alert_type,
                        alert.detail,
                        alert.wiki_page,
                        alert.severity,
                        alert.created_at,
                    ),
                )
                conn.commit()
        except (sqlite3.Error, OSError):
            logging.getLogger(__name__).warning("Caught unexpected error", exc_info=True)

    def get_unresolved_alerts(self) -> List[EvolutionAlert]:
        try:
            with sqlite3.connect(str(self._db_path), timeout=5) as conn:
                cursor = conn.execute(
                    "SELECT entity, alert_type, detail, wiki_page, severity, created_at "
                    "FROM evolution_alerts WHERE resolved = 0 "
                    "ORDER BY severity DESC LIMIT 20",
                )
                return [
                    EvolutionAlert(
                        entity=row[0],
                        alert_type=row[1],
                        detail=row[2],
                        wiki_page=row[3],
                        severity=row[4],
                        created_at=row[5],
                    )
                    for row in cursor
                ]
        except (sqlite3.Error, OSError):
            logging.getLogger(__name__).warning(
                "Caught unexpected error at evolution_tracker.py", exc_info=True
            )
            return []

    def resolve_alert(self, entity: str, alert_type: str):
        try:
            with sqlite3.connect(str(self._db_path), timeout=5) as conn:
                conn.execute(
                    "UPDATE evolution_alerts SET resolved = 1 "
                    "WHERE entity = ? AND alert_type = ?",
                    (entity, alert_type),
                )
                conn.commit()
        except (sqlite3.Error, OSError):
            logging.getLogger(__name__).warning(
                "Caught unexpected error at evolution_tracker.py", exc_info=True
            )


# ========== RecirculationGuard ==========


class RecirculationGuard:
    """回流防护 — 防止 Wiki 引用内容再次蒸馏

    检测机制：
    1. skip-distill=true 标签
    2. <wiki-context> 标记
    3. 已蒸馏内容的特征指纹
    """

    # Wiki 引用标记
    _WIKI_MARKERS = [
        "<wiki-context",
        "</wiki-context>",
        "<!-- wiki-injected",
        "skip-distill=true",
        "<!-- auto-maintained",
    ]

    # 已蒸馏页面的特征
    _DISTILLED_PATTERNS = [
        r"^类型:\s*\w+",  # frontmatter 中的类型字段
        r"^来源会话:\s*\w{8}",  # frontmatter 中的来源会话
        r"^证据级别:\s*\w+",  # 证据级别
        r"## 演化历史",  # 标准章节
    ]

    def should_skip(self, content: str) -> Tuple[bool, str]:
        """判断内容是否应跳过蒸馏

        Returns:
            (should_skip, reason)
        """
        if not content:
            return True, "空内容"

        # 1. Wiki 引用标记检测
        for marker in self._WIKI_MARKERS:
            if marker in content:
                return True, f"检测到 Wiki 引用标记: {marker}"

        # 2. 完整 Wiki 页面格式检测（frontmatter + 标准章节）
        has_frontmatter = content.strip().startswith("---")
        has_evolution = "## 演化历史" in content
        if has_frontmatter and has_evolution:
            return True, "内容已经是完整 Wiki 页面"

        # 3. skip-distill 标签
        if "skip-distill" in content:
            return True, "包含 skip-distill 标记"

        # 4. 内容与已有蒸馏结果高度相似
        # （此检查由 SyncEngine 在标签组装阶段完成，此处仅做基本检查）

        return False, ""

    def check_session(self, messages: List[Dict]) -> Tuple[bool, str]:
        """检查会话是否包含回流内容

        Returns:
            (has_recirculation, detail)
        """
        for msg in messages:
            content = msg.get("content", "")
            should, reason = self.should_skip(content)
            if should:
                return True, f"消息包含回流内容: {reason}"
        return False, ""
