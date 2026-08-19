# -*- coding: utf-8 -*-
"""
KIAEventConsumer — KIA 高级功能事件消费者

订阅并持久化以下遥测/报告类事件：
- immune.report      → L3-Observations/immune/
- dna.computed       → .kg/dna.db + 页面 frontmatter dna_hash
- entropy.suggestions → .kg/entropy_suggestions.db + 06-Retrospectives/entropy/

这些事件原本只有审计 sink，导致大量 no_consumer 死信。本消费者让它们
真正落地为可观测的 DB 记录与 Wiki 报告。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from core.config import get_config
from core.cognitive.state_contract import sha256_json
from core.frontmatter import parse_frontmatter, write_frontmatter
from core.trust.formal_markdown import (
    TrustedMarkdownDecisionPolicy,
    submit_or_write_markdown_with_decision,
)
from core.trust.models import sha256_text

logger = logging.getLogger(__name__)


def compact_entropy_report_frontmatter(
    frontmatter: Dict[str, Any],
) -> Dict[str, Any]:
    """Keep entropy provenance verifiable without unbounded ACL headers."""

    compacted = dict(frontmatter)
    raw_ids = compacted.pop("source_row_ids", [])
    compacted.pop("sources", None)
    if not isinstance(raw_ids, list):
        raw_ids = []
    row_ids = [int(value) for value in raw_ids]
    if row_ids:
        compacted["source_row_id_first"] = min(row_ids)
        compacted["source_row_id_last"] = max(row_ids)
        compacted["source_row_id_count"] = len(row_ids)
        compacted["source_row_ids_hash"] = sha256_json(row_ids)
    source_db = str(compacted.get("source_db") or "")
    source_digest = str(compacted.get("source_payload_digest") or "")
    if source_db and source_digest:
        compacted["source_locator"] = (
            f"sqlite:{source_db}#entropy_suggestions?source_digest={source_digest}"
        )
    return compacted


KIA_DNA_FRONTMATTER_MARKDOWN_POLICY = TrustedMarkdownDecisionPolicy(
    contract_id="project-contract:kia-dna-frontmatter",
    contract_revision_id="mnemos.kia_dna_frontmatter.v1",
    contract_text=(
        "KIAEventConsumer may project only the exact durable DNA observation onto "
        "the exact source-page preimage named by the event."
    ),
    source_namespace="kia-dna-frontmatter",
    producer="kia-event-consumer",
    producer_code_hash=sha256_json(
        {
            "module": "core.kia.kia_event_consumer",
            "producer": "on_dna_computed",
            "version": "mnemos.kia_dna_frontmatter.v1",
        }
    ),
    evaluator_id="kia-dna-frontmatter-evaluator",
    constraints=(
        "Target, page preimage, DNA hash, domain, type, and rendered bytes remain exact.",
        "Event content may not be promoted beyond the fields saved by DNAEngine.",
    ),
    approved_candidate_key="project_exact_dna_frontmatter",
    approved_candidate_summary="Project the exact saved DNA fields to the source page.",
    rejected_candidate_key="retain_page_without_dna_projection",
    rejected_candidate_summary="Retain the page when DNA or page binding drifts.",
    approved_reason_code="kia_dna_binding_verified",
    rejected_reason_code="kia_dna_binding_rejected",
    committed_metric="kia_dna_frontmatter_committed",
    rejected_metric="unbound_kia_dna_frontmatter_count",
)

KIA_EVENT_REPORT_MARKDOWN_POLICY = TrustedMarkdownDecisionPolicy(
    contract_id="project-contract:kia-event-report",
    contract_revision_id="mnemos.kia_event_report.v1",
    contract_text=(
        "KIAEventConsumer may create only the exact immutable report rendered from "
        "one exact immune or entropy event payload digest."
    ),
    source_namespace="kia-event-report",
    producer="kia-event-consumer",
    producer_code_hash=sha256_json(
        {
            "module": "core.kia.kia_event_consumer",
            "producer": "_write_report",
            "version": "mnemos.kia_event_report.v1",
        }
    ),
    evaluator_id="kia-event-report-evaluator",
    constraints=(
        "Event type, payload digest, report identity, target, and rendered bytes remain exact.",
        "An existing same-identity report is an idempotent no-op, not a second write.",
    ),
    approved_candidate_key="create_exact_kia_event_report",
    approved_candidate_summary="Create the exact report rendered from the KIA event.",
    rejected_candidate_key="retain_kia_report_state",
    rejected_candidate_summary="Retain report state when event identity or bytes drift.",
    approved_reason_code="kia_event_report_binding_verified",
    rejected_reason_code="kia_event_report_binding_rejected",
    committed_metric="kia_event_report_committed",
    rejected_metric="unbound_kia_event_report_count",
)


class KIAEventConsumer:
    """KIA 事件消费者：把免疫/DNA/熵减事件持久化。"""

    def __init__(self, wiki_base: str | None = None):
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else get_config().wiki_dir
        self.entropy_dir = self.wiki_base / "06-Retrospectives" / "entropy"
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        (self.wiki_base / "L3-Observations" / "immune").mkdir(parents=True, exist_ok=True)
        self.entropy_dir.mkdir(parents=True, exist_ok=True)
        (self.wiki_base / ".kg").mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _payload_digest(payload: Dict[str, Any]) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _write_report(
        self,
        path: Path,
        frontmatter: Dict[str, Any],
        body_lines: list[str],
    ):
        if path.exists():
            return None
        rendered_content = write_frontmatter(
            frontmatter,
            "\n".join(body_lines) + "\n",
        )
        sources = [str(value) for value in frontmatter.get("sources", []) if value]
        source_locator = str(frontmatter.get("source_locator") or "").strip()
        if source_locator:
            sources.append(source_locator)
        return submit_or_write_markdown_with_decision(
            decision_policy=KIA_EVENT_REPORT_MARKDOWN_POLICY,
            decision_facts={
                "schema_version": "mnemos.kia_event_report_facts.v1",
                "report_type": str(frontmatter.get("report_type") or ""),
                "report_id": str(frontmatter.get("report_id") or ""),
                "source_event_type": str(frontmatter.get("source_event_type") or ""),
                "source_payload_digest": str(frontmatter.get("source_payload_digest") or ""),
            },
            decision_task=f"Write KIA event report {path.name}",
            decision_goal="Publish the exact immutable report derived from the event payload.",
            decision_created_at=datetime.now(timezone.utc).isoformat(),
            wiki_base=self.wiki_base,
            target_path=path,
            content=rendered_content,
            source="kia_event_consumer",
            actor="system",
            evidence_refs=sources,
            proposed_action="create_kia_event_report",
            metadata={"report_type": frontmatter.get("report_type", "")},
        )

    # ------------------------------------------------------------------
    # immune.report
    # ------------------------------------------------------------------

    def on_immune_report(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """处理免疫报告事件，生成 Markdown 报告。"""
        timestamp = datetime.now().isoformat()[:19]
        issue_count = payload.get("issue_count", 0)
        critical_count = payload.get("critical_count", 0)
        scanned_pages = payload.get("scanned_pages", 0)
        health_score = payload.get("health_score", 0)
        issues = payload.get("issues", [])

        if scanned_pages <= 0 and not issues:
            return {"status": "skipped", "reason": "no immune source data"}

        digest = self._payload_digest(payload)
        report_id = f"immune-{timestamp[:10]}-{digest[:10]}"
        report_path = (
            self.wiki_base
            / "L3-Observations"
            / "immune"
            / f"immune-report-{timestamp[:10]}-{digest[:10]}.md"
        )
        frontmatter = {
            "mnemos_type": "system_report",
            "report_type": "immune",
            "report_id": report_id,
            "generated_at": timestamp,
            "source_event_type": "immune.report",
            "source_payload_digest": digest,
            "source_fields": [
                "scanned_pages",
                "issue_count",
                "critical_count",
                "health_score",
                "issues",
            ],
            "source_count": 1,
            "sources": [f"event:immune.report:{digest}"],
            "evidence_level": "single",
            "knowledge_stage": "P2",
            "status": "active",
            "data_reliability": "event_payload",
            "scanned_pages": scanned_pages,
            "issue_count": issue_count,
            "critical_count": critical_count,
        }

        lines = [
            "# 知识免疫报告",
            "",
            f"- 扫描时间: {timestamp}",
            f"- 扫描页面: {scanned_pages}",
            f"- 问题总数: {issue_count}",
            f"- 严重问题: {critical_count}",
            f"- 健康分数: {health_score}/100",
            "",
        ]

        if issues:
            lines.extend(["## 问题清单", ""])
            for issue in issues:
                lines.extend(
                    [
                        f"### {issue.get('issue_type', 'unknown')} ({issue.get('severity', 'unknown')})",  # noqa: E501
                        f"- 页面: {issue.get('page', 'N/A')}",
                        f"- 描述: {issue.get('description', '')}",
                        f"- 建议: {issue.get('suggestion', '')}",
                        "",
                    ]
                )

        try:
            write_result = self._write_report(report_path, frontmatter, lines)
        except (OSError, IOError) as exc:
            logger.warning("[KIAEventConsumer] 免疫报告写入失败: %s", exc)
            return {"status": "error", "reason": str(exc)}

        if write_result is None:
            logger.info("[KIAEventConsumer] 免疫报告同日同源已存在: %s", report_path)
            return {
                "status": "duplicate",
                "report_path": str(report_path),
                "issue_count": issue_count,
                "critical_count": critical_count,
            }
        if write_result.intercepted:
            return {
                "status": "proposed",
                "report_path": str(report_path),
                "proposal_id": write_result.proposal_id,
                "issue_count": issue_count,
                "critical_count": critical_count,
            }

        logger.info("[KIAEventConsumer] 免疫报告已写入: %s", report_path)
        return {
            "status": "ok",
            "report_path": str(report_path),
            "issue_count": issue_count,
            "critical_count": critical_count,
        }

    # ------------------------------------------------------------------
    # dna.computed
    # ------------------------------------------------------------------

    def on_dna_computed(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """处理 DNA 计算完成事件：保存 DNA 并更新页面 frontmatter。

        注意：这里直接使用事件 payload 中的完整 DNA，不再调用 DNAEngine.compute_dna()，
        避免 compute_dna() 内部再次发布 dna.computed 事件导致无限循环。
        """
        page_path_str = payload.get("page_path", "")
        if not page_path_str:
            return {"status": "skipped", "reason": "no page_path"}

        page_path = Path(page_path_str)

        try:
            from .genos import DNAEngine, KnowledgeDNA

            dna = KnowledgeDNA.from_dict(payload)
            engine = DNAEngine(wiki_base=str(self.wiki_base))
            engine.save_dna(dna)

            # 更新页面 frontmatter
            if page_path.exists():
                try:
                    content = page_path.read_text(encoding="utf-8")
                    fm, body = parse_frontmatter(content)
                    if fm is None:
                        fm = {}
                    fm["dna_hash"] = dna.content_simhash or dna.content_md5
                    fm["dna_domain"] = dna.domain
                    fm["dna_type"] = dna.knowledge_type
                    evidence_refs = [f"dna:{dna.content_simhash or dna.content_md5}"]
                    updated_content = write_frontmatter(fm, body)
                    submit_or_write_markdown_with_decision(
                        decision_policy=KIA_DNA_FRONTMATTER_MARKDOWN_POLICY,
                        decision_facts={
                            "schema_version": "mnemos.kia_dna_frontmatter_facts.v1",
                            "page_path": page_path_str,
                            "dna_hash": dna.content_simhash or dna.content_md5,
                            "domain": dna.domain,
                            "knowledge_type": dna.knowledge_type,
                        },
                        decision_task=f"Project DNA metadata for {page_path.name}",
                        decision_goal="Keep the source page aligned with its durable DNA record.",
                        decision_created_at=datetime.now(timezone.utc).isoformat(),
                        wiki_base=self.wiki_base,
                        target_path=page_path,
                        content=updated_content,
                        source="kia_event_consumer",
                        actor="system",
                        evidence_refs=evidence_refs,
                        proposed_action="update_dna_frontmatter",
                        expected_existing_hash=sha256_text(content),
                        metadata={"domain": dna.domain, "knowledge_type": dna.knowledge_type},
                    )
                except (OSError, IOError) as fm_exc:
                    logger.warning("[KIAEventConsumer] 更新页面 frontmatter 失败: %s", fm_exc)

            logger.info("[KIAEventConsumer] DNA 已保存: %s", page_path)
            return {
                "status": "ok",
                "page_path": page_path_str,
                "dna_hash": dna.content_simhash or dna.content_md5,
            }
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
            logger.warning("[KIAEventConsumer] DNA 处理失败: %s", exc, exc_info=True)
            return {"status": "error", "reason": str(exc)}

    # ------------------------------------------------------------------
    # entropy.suggestions
    # ------------------------------------------------------------------

    @staticmethod
    def _ensure_entropy_schema(conn: sqlite3.Connection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS entropy_suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT,
                trigger TEXT,
                page_a TEXT,
                page_b TEXT,
                similarity REAL,
                merge_strategy TEXT,
                reason TEXT,
                recommended_action TEXT,
                confidence REAL,
                reviewed INTEGER DEFAULT 0,
                source_digest TEXT
            )
            """)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(entropy_suggestions)")}
        if "source_digest" not in columns:
            conn.execute("ALTER TABLE entropy_suggestions ADD COLUMN source_digest TEXT")

    def on_entropy_suggestions(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """处理熵减建议事件：持久化到 SQLite 并生成 Markdown 报告。"""
        candidates = payload.get("candidates", [])
        trigger = payload.get("trigger", "scan")
        estimated_savings = payload.get("estimated_savings", {})
        digest = self._payload_digest(payload)
        created_at = datetime.now().isoformat()[:19]

        db_path = self.wiki_base / ".kg" / "entropy_suggestions.db"
        source_row_ids: list[int] = []
        try:
            with sqlite3.connect(str(db_path), timeout=10) as conn:
                self._ensure_entropy_schema(conn)
                existing = conn.execute(
                    "SELECT id FROM entropy_suggestions WHERE source_digest = ? ORDER BY id",
                    (digest,),
                ).fetchall()
                if existing:
                    source_row_ids = [int(row[0]) for row in existing]
                else:
                    for candidate in candidates:
                        cursor = conn.execute(
                            """
                            INSERT INTO entropy_suggestions
                            (created_at, trigger, page_a, page_b, similarity, merge_strategy,
                             reason, recommended_action, confidence, source_digest)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                created_at,
                                trigger,
                                candidate.get("page_a", ""),
                                candidate.get("page_b", ""),
                                candidate.get("similarity", 0.0),
                                candidate.get("merge_strategy", ""),
                                candidate.get("reason", ""),
                                candidate.get("recommended_action", ""),
                                candidate.get("confidence", 0.0),
                                digest,
                            ),
                        )
                        row_id = cursor.lastrowid
                        if row_id is not None:
                            source_row_ids.append(int(row_id))
                conn.commit()
        except sqlite3.Error as exc:
            logger.warning("[KIAEventConsumer] 熵减建议持久化失败: %s", exc)
            return {"status": "error", "reason": str(exc)}

        if candidates:
            if not source_row_ids:
                return {
                    "status": "skipped",
                    "reason": "no entropy source rows",
                    "candidate_count": len(candidates),
                }
            report_id = f"entropy-{created_at[:10]}-{digest[:10]}"
            report_path = (
                self.entropy_dir / f"entropy-suggestions-{created_at[:10]}-{digest[:10]}.md"
            )
            frontmatter = {
                "mnemos_type": "system_report",
                "report_type": "entropy_suggestions",
                "report_id": report_id,
                "generated_at": created_at,
                "source_event_type": "entropy.suggestions",
                "source_db": str(db_path),
                "source_row_ids": source_row_ids,
                "source_payload_digest": digest,
                "source_count": len(source_row_ids),
                "sources": [
                    f"sqlite:{db_path}#entropy_suggestions/{row_id}" for row_id in source_row_ids
                ],
                "evidence_level": "multiple" if len(source_row_ids) > 1 else "single",
                "knowledge_stage": "P2",
                "status": "active",
                "data_reliability": "db_backed",
                "trigger": trigger,
                "candidate_count": len(candidates),
            }
            frontmatter = compact_entropy_report_frontmatter(frontmatter)
            lines = [
                "# 熵减建议报告",
                "",
                f"- 生成时间: {created_at}",
                f"- 触发方式: {trigger}",
                f"- 候选数: {len(candidates)}",
                f"- 来源数据库: `{db_path}`",
                f"- 来源行: {', '.join(str(row_id) for row_id in source_row_ids)}",
                f"- 预估节省: {estimated_savings}",
                "",
                "## 合并候选",
                "",
            ]
            for candidate in candidates:
                lines.extend(
                    [
                        f"### {candidate.get('page_a', '')} ↔ {candidate.get('page_b', '')}",
                        f"- 相似度: {candidate.get('similarity', 0.0):.3f}",
                        f"- 策略: {candidate.get('merge_strategy', '')}",
                        f"- 原因: {candidate.get('reason', '')}",
                        f"- 建议操作: {candidate.get('recommended_action', '')}",
                        "",
                    ]
                )
            try:
                write_result = self._write_report(report_path, frontmatter, lines)
                if write_result is not None and write_result.intercepted:
                    return {
                        "status": "proposed",
                        "candidate_count": len(candidates),
                        "report_path": str(report_path),
                        "proposal_id": write_result.proposal_id,
                        "source_row_ids": source_row_ids,
                    }
                if write_result is not None:
                    logger.info("[KIAEventConsumer] 熵减建议报告已写入: %s", report_path)
                else:
                    logger.info("[KIAEventConsumer] 熵减建议报告同源已存在: %s", report_path)
                    return {
                        "status": "duplicate",
                        "candidate_count": len(candidates),
                        "report_path": str(report_path),
                        "source_row_ids": source_row_ids,
                    }
            except (OSError, IOError) as exc:
                logger.warning("[KIAEventConsumer] 熵减报告写入失败: %s", exc)

        return {
            "status": "ok",
            "candidate_count": len(candidates),
            "source_row_ids": source_row_ids,
        }
