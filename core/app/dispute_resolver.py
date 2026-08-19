# -*- coding: utf-8 -*-
"""
DisputeResolver — 争议仲裁界面

知识图谱检测到 suspect 冲突关系时，生成 Markdown 仲裁页面。
高强度冲突在每日报告升级，未解决 7 天后在下周报告置顶。
"""

from __future__ import annotations

import logging
import re
import hashlib
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core import config as _config_module
from core.cognitive.decision_trace import (
    MaterialActionRequest,
    authorize_exact_project_contract_action,
    find_pending_material_action_authorization,
)
from core.cognitive.state_contract import sha256_json
from core.frontmatter import fm_get, parse_frontmatter
from core.kia.conflict_resolver import ConflictResolver
from core.kia.knowledge_graph import KnowledgeGraph
from core.trust.models import sha256_text
from core.trust.vault_mutation_service import (
    TRUSTED_MARKDOWN_ACTION_TYPE,
    TRUSTED_MARKDOWN_EXECUTOR,
    TRUSTED_MARKDOWN_OWNER,
    TrustedVaultMutationService,
    commit_trusted_markdown,
    trusted_markdown_material_action_binding,
)
from core.wiki_metrics import WikiMetrics
from core.app.dispute_scorer import DisputeScorer, RelationFeatures, _DIMENSIONS


DISPUTE_OPERATION_ERRORS = (
    ImportError,
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    KeyError,
    sqlite3.Error,
)

# Constants extracted from magic numbers
FILENAME = 30
DISPUTE_RESOLVER_GET_UNRESOLVED_DISPUTES_DAYS_OLD_DAYS = 7
DISPUTE_DECISION_CONTRACT_ID = "project-contract:dispute-markdown-mutations"
DISPUTE_DECISION_CONTRACT_REVISION = "mnemos.dispute_markdown_mutations.v1"
DISPUTE_DECISION_CONTRACT_TEXT = (
    "The dispute resolver may create or revise only exact Wiki pages derived "
    "from a current dispute, resolution, or dispute-context rollback."
)
DISPUTE_DECISION_PRODUCER_HASH = sha256_json(
    {
        "module": "core.app.dispute_resolver",
        "producer": "DisputeResolver._write_or_propose",
        "version": DISPUTE_DECISION_CONTRACT_REVISION,
    }
)
DISPUTE_MARKDOWN_ACTIONS = frozenset(
    {
        "append_dispute_boundary",
        "create_dispute_page",
        "resolve_dispute_page",
        "rollback_dispute_context",
        "record_dispute_context_rollback",
        "add_dispute_context",
    }
)

logger = logging.getLogger(__name__)


@dataclass
class DisputeAssertion:
    """争议断言"""

    page_path: str
    title: str
    content: str
    reference_count: int
    relation_context: str = ""
    relation_evidence: List[str] = None  # type: ignore[assignment]
    source_method: str = ""
    confidence: float = 0.0
    strength: float = 0.0

    def __post_init__(self):
        if self.relation_evidence is None:
            self.relation_evidence = []


@dataclass
class DisputePage:
    """争议页面"""

    topic: str
    new_assertion: DisputeAssertion
    existing_assertions: List[DisputeAssertion]
    conflict_strength: float
    is_core_knowledge: bool
    page_path: str = ""

    @property
    def severity(self) -> str:
        if self.conflict_strength > 0.9 and self.is_core_knowledge:
            return "extreme"
        elif self.conflict_strength > 0.7 and self.is_core_knowledge:
            return "high"
        else:
            return "medium"


class DisputeResolver:
    """争议仲裁器"""

    def __init__(
        self,
        wiki_base: Optional[str] = None,
        db_path: Optional[str] = None,
        conflict_resolver: Optional[ConflictResolver] = None,
    ):
        if wiki_base:
            self.wiki_base = Path(wiki_base).expanduser()
        else:
            self.wiki_base = _config_module.get_config().wiki_dir
        self.db_path = db_path
        self._scorer = None
        self._conflict_resolver = conflict_resolver or ConflictResolver()

    def _get_scorer(self) -> DisputeScorer:
        if self._scorer is None:
            self._scorer = DisputeScorer(wiki_dir=self.wiki_base)  # type: ignore[assignment]
        return self._scorer  # type: ignore[return-value]

    def scan(self, max_disputes: Optional[int] = None) -> Dict[str, Any]:
        """
        自动扫描知识冲突并裁决。

        流程：
        1. 从 KnowledgeGraph 检测关系级冲突
        2. 对冲突双方计算综合评分
        3. 根据分差决定：自动裁决 / 合并 / 生成争议页
        """
        result = {
            "scanned_relations": 0,
            "conflicts_found": 0,
            "auto_resolved": 0,
            "merged": 0,
            "disputes_created": 0,
            "skipped": 0,
        }

        cfg = _config_module.get_config()
        ds_cfg = cfg.get("dispute_scan", {}) or {}
        if not ds_cfg.get("enabled", True):
            return result

        try:
            kg_kwargs: Dict[str, Any] = {"wiki_base": str(self.wiki_base)}
            if self.db_path:
                kg_kwargs["db_path"] = str(self.db_path)
            kg = KnowledgeGraph(**kg_kwargs)
            scorer = self._get_scorer()

            conflicts = kg.detect_conflicts(conflict_resolver=self._conflict_resolver)
            result["conflicts_found"] = len(conflicts)
            max_pairs = max(0, int(ds_cfg.get("max_pages_per_scan", len(conflicts))))
            conflicts_to_scan = conflicts[:max_pairs] if max_pairs else []
            result["scanned_relations"] = len(conflicts_to_scan) * 2
            if len(conflicts_to_scan) < len(conflicts):
                result["skipped"] += len(conflicts) - len(conflicts_to_scan)

            max_daily = (
                max_disputes
                if max_disputes is not None
                else int(ds_cfg.get("max_daily_disputes", 10))
            )
            created_today = self._count_today_disputes()

            for rel_a, rel_b, reason in conflicts_to_scan:
                if rel_a.status == "deprecated" or rel_b.status == "deprecated":
                    continue

                features_a = scorer.extract_features(rel_a)
                features_b = scorer.extract_features(rel_b)
                conflict_strength = self._estimate_conflict_strength(
                    rel_a, rel_b, features_a, features_b
                )

                pair_key = self._relation_pair_key(rel_a, rel_b)
                action, context = scorer.decide(
                    features_a, features_b, conflict_strength, pair_key=pair_key
                )

                if action == "skip":
                    result["skipped"] += 1
                    continue

                if action == "auto_resolve":
                    self._apply_auto_resolution(kg, rel_a, rel_b, context, scorer)
                    result["auto_resolved"] += 1
                    continue

                if action == "merge":
                    self._apply_merge(rel_a, rel_b, context)
                    result["merged"] += 1
                    continue

                # create_dispute
                if created_today >= max_daily:
                    result["skipped"] += 1
                    continue
                if self._dispute_exists(pair_key):
                    continue

                new_assertion = self._relation_to_assertion(rel_a)
                existing_assertions = [self._relation_to_assertion(rel_b)]
                self.create_dispute_page(
                    new_assertion=new_assertion,
                    conflicts=existing_assertions,
                    conflict_strength=conflict_strength,
                    is_core_knowledge=context.get("is_core", False),
                    pair_key=pair_key,
                    features_a=features_a,
                    features_b=features_b,
                )
                created_today += 1
                result["disputes_created"] += 1

        except DISPUTE_OPERATION_ERRORS as e:
            logger.warning("争议扫描失败: %s", e, exc_info=True)

        return result

    def _relation_pair_key(self, rel_a, rel_b) -> str:
        parts = sorted(
            [
                f"{rel_a.source}|{rel_a.target}|{rel_a.relation_type.value}",
                f"{rel_b.source}|{rel_b.target}|{rel_b.relation_type.value}",
            ]
        )
        return "#".join(parts)

    def _estimate_conflict_strength(
        self, rel_a, rel_b, features_a: RelationFeatures, features_b: RelationFeatures
    ) -> float:
        """综合关系自身强度与特征差异估算冲突强度"""
        base = max(getattr(rel_a, "strength", 0.5) or 0.5, getattr(rel_b, "strength", 0.5) or 0.5)
        confidence_diff = abs(features_a.confidence - features_b.confidence)
        freshness_diff = abs(features_a.freshness - features_b.freshness)
        return min(1.0, base * 0.5 + confidence_diff * 0.25 + freshness_diff * 0.25)

    def _relation_to_assertion(self, rel) -> DisputeAssertion:
        page_path = rel.source
        full_path = self.wiki_base / page_path
        if not full_path.exists():
            full_path = self.wiki_base / (page_path + ".md")
        content = ""
        if full_path.exists():
            try:
                content = full_path.read_text(encoding="utf-8", errors="ignore")
            except (OSError, UnicodeError) as e:
                logger.debug("读取争议页面内容失败 %s: %s", full_path, e)

        pm = WikiMetrics(wiki_dir=str(self.wiki_base)).get_page(page_path)
        reference_count = (pm.source_count if pm else 0) + (pm.backlink_count if pm else 0)
        return DisputeAssertion(
            page_path=page_path,
            title=Path(page_path).stem,
            content=content,
            reference_count=max(0, reference_count),
            relation_context=getattr(rel, "context", "") or "",
            relation_evidence=list(getattr(rel, "evidence", []) or []),
            source_method=getattr(rel, "source_method", "") or "",
            confidence=float(getattr(rel, "confidence", 0.0) or 0.0),
            strength=float(getattr(rel, "strength", 0.0) or 0.0),
        )

    def _count_today_disputes(self) -> int:
        dispute_dir = self.wiki_base / "08-Disputes"
        if not dispute_dir.exists():
            return 0
        today = datetime.now().strftime("%Y-%m-%d")
        return len(list(dispute_dir.glob(f"{today}-*.md")))

    def _dispute_exists(self, pair_key: str) -> bool:
        dispute_dir = self.wiki_base / "08-Disputes"
        if not dispute_dir.exists():
            return False
        for md_file in dispute_dir.glob("*.md"):
            try:
                text = md_file.read_text(encoding="utf-8")
                if f"dispute_pair: {pair_key}" in text:
                    return True
            # DEBT(S8): 容错跳过，避免单条记录中断批量处理
            except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
                continue
        return False

    def _apply_auto_resolution(
        self, kg, rel_a, rel_b, context: Dict[str, Any], scorer: DisputeScorer
    ) -> None:
        """自动裁决：deprecate 低分方，并在关系上下文里写清原因"""
        winner = context.get("winner", "a")
        _ = rel_a if winner == "a" else rel_b
        loser_rel = rel_b if winner == "a" else rel_a

        note = (
            f"[auto-resolved by dispute scan] 综合分 {context.get('score_' + winner, 0):.3f} "
            f"vs {context.get('score_' + ('b' if winner == 'a' else 'a'), 0):.3f}"
        )
        self._deprecate_relation(kg, loser_rel, note)
        # 记录反馈， winner 为胜方代码 "a" 或 "b"
        scorer.record_outcome(
            pair_key=self._relation_pair_key(rel_a, rel_b),
            features_a=scorer.extract_features(rel_a),
            features_b=scorer.extract_features(rel_b),
            system_decision="auto_resolve",
            actual_winner=winner,
        )

    def _apply_merge(self, rel_a, rel_b, context: Dict[str, Any]) -> None:
        """合并边界：在双方页面末尾添加争议引用说明"""
        note = (
            "[auto-merge by dispute scan] 该页面与 "
            f"[[{rel_b.source if rel_a.source != rel_b.source else rel_b.target}]] "
            "存在潜在冲突，待进一步验证。"
        )
        self._append_page_note(rel_a.source, note)
        self._append_page_note(rel_b.source, note)

    def _append_page_note(self, page_path: str, note: str) -> None:
        full_path = self.wiki_base / page_path
        if not full_path.exists():
            full_path = self.wiki_base / (page_path + ".md")
        if not full_path.exists():
            return
        try:
            content = full_path.read_text(encoding="utf-8")
            marker = "<!-- dispute-boundary -->"
            if marker in content:
                return
            self._write_or_propose(
                full_path,
                content + f"\n\n{marker}\n> {note}\n",
                proposed_action="append_dispute_boundary",
                evidence_refs=[page_path],
                expected_existing_hash=sha256_text(content),
            )
        except DISPUTE_OPERATION_ERRORS as e:
            logger.warning("添加争议边界说明失败 %s: %s", full_path, e)

    def _resolve_wiki_path(self, page_path: str | Path) -> Path:
        path = Path(page_path).expanduser()
        if path.is_absolute():
            return path
        return self.wiki_base / path

    def _deprecate_relation(self, kg, rel, reason: str) -> None:
        """将关系标记为 deprecated 并降低置信度/强度"""
        try:
            with kg._conn() as conn:
                new_context = (rel.context or "").rstrip() + " " + reason
                conn.execute(
                    """UPDATE relations
                       SET status='deprecated', confidence=0, strength=0, context=?
                       WHERE source=? AND target=? AND relation_type=?""",
                    (new_context, rel.source, rel.target, rel.relation_type.value),
                )
                conn.commit()
        except (OSError, RuntimeError, ValueError, TypeError, sqlite3.Error) as e:
            logger.warning("deprecate 关系失败 %s -> %s: %s", rel.source, rel.target, e)

    def create_dispute_page(
        self,
        new_assertion: DisputeAssertion,
        conflicts: List[DisputeAssertion],
        conflict_strength: float,
        is_core_knowledge: bool = False,
        pair_key: str = "",
        features_a: Optional[RelationFeatures] = None,
        features_b: Optional[RelationFeatures] = None,
    ) -> DisputePage:
        """
        创建争议仲裁页面。

        不弹窗不中断，只生成 Markdown 页面。
        """
        topic = new_assertion.title
        dispute = DisputePage(
            topic=topic,
            new_assertion=new_assertion,
            existing_assertions=conflicts,
            conflict_strength=conflict_strength,
            is_core_knowledge=is_core_knowledge,
        )

        # 生成 Markdown
        content = self._render_dispute_page(dispute, features_a=features_a, features_b=features_b)

        # 写入 Wiki
        date_str = datetime.now().strftime("%Y-%m-%d")
        dispute_dir = self.wiki_base / "08-Disputes"
        dispute_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{date_str}-{topic[:FILENAME].replace('/', '-').replace(' ', '_')}.md"
        page_path = dispute_dir / filename
        frontmatter = self._build_dispute_frontmatter(
            dispute, date_str, pair_key, features_a, features_b
        )
        self._write_or_propose(
            page_path,
            frontmatter + content,
            proposed_action="create_dispute_page",
            evidence_refs=new_assertion.relation_evidence or [new_assertion.page_path],
            metadata={"pair_key": pair_key, "conflict_strength": conflict_strength},
        )

        dispute.page_path = str(page_path.relative_to(self.wiki_base))
        logger.info("争议页面已创建: %s", page_path)

        return dispute

    def _build_dispute_frontmatter(
        self,
        dispute: DisputePage,
        date_str: str,
        pair_key: str,
        features_a: Optional[RelationFeatures] = None,
        features_b: Optional[RelationFeatures] = None,
    ) -> str:
        import json

        lines = [
            "---",
            "type: dispute",
            "status: unresolved",
            f"topic: {dispute.topic}",
            f"conflict_strength: {dispute.conflict_strength:.3f}",
            f"is_core_knowledge: {dispute.is_core_knowledge}",
            f"dispute_pair: {pair_key}",
            f"created: {date_str}",
        ]
        if features_a and features_b:
            lines.append(f"features_a: {json.dumps(features_a.to_dict(), ensure_ascii=False)}")
            lines.append(f"features_b: {json.dumps(features_b.to_dict(), ensure_ascii=False)}")
        lines.extend(["---", ""])
        return "\n".join(lines)

    def get_unresolved_disputes(self) -> List[Dict]:
        """获取未解决的争议列表"""
        disputes = []  # type: ignore[var-annotated]
        dispute_dir = self.wiki_base / "08-Disputes"
        if not dispute_dir.exists():
            return disputes

        for md_file in dispute_dir.glob("*.md"):
            try:
                content = md_file.read_text(encoding="utf-8")
                # 未解决的争议页面包含未勾选的 checkbox
                if "- [ ] " in content:
                    days_old = (
                        datetime.now() - datetime.fromtimestamp(md_file.stat().st_mtime)
                    ).days
                    disputes.append(
                        {
                            "path": str(md_file.relative_to(self.wiki_base)),
                            "title": md_file.stem,
                            "days_old": days_old,
                            "needs_escalation": days_old
                            >= DISPUTE_RESOLVER_GET_UNRESOLVED_DISPUTES_DAYS_OLD_DAYS,
                        }
                    )
            except (OSError, ValueError):
                logging.getLogger(__name__).warning(
                    "Caught unexpected error at dispute_resolver.py", exc_info=True
                )
                continue

        return sorted(disputes, key=lambda d: d["days_old"], reverse=True)

    def resolve_dispute(self, page_path: str, resolution: str, context: str = "") -> None:
        """
        解决争议。

        Args:
            page_path: 争议页面路径
            resolution: adopt_new / keep_old / keep_both / need_more_info
            context: 附加上下文（keep_both 时必填）
        """
        full_path = self.wiki_base / page_path
        if not full_path.exists():
            return

        content = full_path.read_text(encoding="utf-8")
        expected_existing_hash = sha256_text(content)

        # 更新 checkbox
        content = content.replace("- [ ] ", "- [x] ")

        # 添加解决方案
        resolution_labels = {
            "adopt_new": "采纳新断言",
            "keep_old": "保留旧断言",
            "keep_both": "保留双方（添加上下文）",
            "need_more_info": "需要更多信息",
        }
        content += f"\n\n---\n**解决方案**: {resolution_labels.get(resolution, resolution)}"
        if context:
            content += f"\n**上下文**: {context}"
        content += f"\n**解决时间**: {datetime.now().strftime('%Y-%m-%d %H:%M')}"

        self._write_or_propose(
            full_path,
            content,
            proposed_action="resolve_dispute_page",
            evidence_refs=[page_path],
            metadata={"resolution": resolution},
            expected_existing_hash=expected_existing_hash,
        )

        # 根据解决方案更新知识图谱
        if resolution == "adopt_new":
            self._apply_resolution_to_relations(page_path, winner="a")
        elif resolution == "keep_old":
            self._apply_resolution_to_relations(page_path, winner="b")
        elif resolution == "keep_both" and context:
            self._add_context_to_both(page_path, context)

        # 记录用户反馈，供自适应权重学习使用
        self._record_resolution_feedback(page_path, resolution)

    def _record_resolution_feedback(self, page_path: str, resolution: str) -> None:
        """读取争议页 frontmatter，将用户裁决反馈写入自适应学习器。"""
        full_path = self.wiki_base / page_path
        try:
            content = full_path.read_text(encoding="utf-8")
            frontmatter, _ = parse_frontmatter(content)
            if not frontmatter:
                return

            pair_key = fm_get(frontmatter, "dispute_pair", "")
            if not pair_key:
                return

            features_a = fm_get(frontmatter, "features_a")
            features_b = fm_get(frontmatter, "features_b")
            if not features_a or not features_b:
                # 旧页面没有存特征，尝试从 KG 重新构造
                try:
                    rel_a, rel_b = self._load_pair_relations(pair_key)
                    if rel_a is None or rel_b is None:
                        return
                    scorer = self._get_scorer()
                    features_a = scorer.extract_features(rel_a).to_dict()
                    features_b = scorer.extract_features(rel_b).to_dict()
                # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
                except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
                    return

            winner_map = {
                "adopt_new": "a",
                "keep_old": "b",
                "keep_both": "both",
                "need_more_info": "none",
            }
            actual_winner = winner_map.get(resolution, "none")

            scorer = self._get_scorer()
            scorer.record_outcome(
                pair_key=pair_key,
                features_a=RelationFeatures.from_dict(features_a),
                features_b=RelationFeatures.from_dict(features_b),
                system_decision="create_dispute",
                actual_winner=actual_winner,
                user_overridden=True,
            )
        except DISPUTE_OPERATION_ERRORS as e:
            logger.debug("记录争议反馈失败 %s: %s", page_path, e)

    def _load_pair_relations(self, pair_key: str):
        """从 pair_key 解析并加载对应的两条关系"""
        part_a, part_b = pair_key.split("#", 1)
        source_a, target_a, type_a = part_a.split("|", 2)
        source_b, target_b, type_b = part_b.split("|", 2)

        kg = KnowledgeGraph(
            wiki_base=str(self.wiki_base),
            db_path=str(self.db_path) if self.db_path else None,
        )
        rel_a = self._find_relation(kg, source_a, target_a, type_a)
        rel_b = self._find_relation(kg, source_b, target_b, type_b)
        return rel_a, rel_b

    def _find_relation(self, kg, source: str, target: str, rel_type: str):
        from core.kia.relation_schema import RelationType

        try:
            rt = RelationType(rel_type)
        except ValueError:
            logger.debug("解析关系类型失败: %s", rel_type)
            return None
        for rel in kg.get_relations(source, relation_type=rt):
            if rel.target == target:
                return rel
        logger.debug("未找到关系: %s -> %s (%s)", source, target, rel_type)
        return None

    def _render_dispute_page(
        self,
        dispute: DisputePage,
        features_a: Optional[RelationFeatures] = None,
        features_b: Optional[RelationFeatures] = None,
    ) -> str:
        """渲染争议页面 Markdown"""
        lines = [
            f"# 争议仲裁：{dispute.topic}",
            "",
            f"> 冲突强度：{dispute.conflict_strength:.2f} | "
            f"严重级别：{dispute.severity} | "
            f"核心知识：{'是' if dispute.is_core_knowledge else '否'}",
            "",
            "## 新断言",
            "",
            f"**来源**: [{dispute.new_assertion.title}]({dispute.new_assertion.page_path})",
            f"**引用数**: {dispute.new_assertion.reference_count}",
            "",
            f"> {dispute.new_assertion.content[:300]}",
            "",
            "## 现有断言",
            "",
        ]

        for i, assertion in enumerate(dispute.existing_assertions, 1):
            lines.append(f"### 断言 {i}")
            lines.append("")
            lines.append(f"**来源**: [{assertion.title}]({assertion.page_path})")
            lines.append(f"**引用数**: {assertion.reference_count}")
            lines.append("")
            lines.append(f"> {assertion.content[:300]}")
            lines.append("")

        lines.extend(
            [
                "## 证据上下文",
                "",
            ]
        )

        def _render_evidence(assertion: DisputeAssertion, label: str):
            evidence_list = assertion.relation_evidence
            evidence_block = ""
            if evidence_list:

                def _fmt_evidence(item):
                    if hasattr(item, "content"):
                        return (
                            f"{item.evidence_type}: {item.content}"
                            if hasattr(item, "evidence_type")
                            else str(item.content)
                        )
                    return str(item)

                evidence_block = "\n".join(f"  - {_fmt_evidence(item)}" for item in evidence_list)
            else:
                evidence_block = "  - （无显式证据）"
            return [
                f"### {label}证据",
                "",
                f"- 来源方法：{assertion.source_method or '未知'}",
                f"- 置信度：{assertion.confidence:.3f}",
                f"- 强度：{assertion.strength:.3f}",
                f"- 关系上下文：{assertion.relation_context or '（空）'}",
                "- 证据列表：",
                evidence_block,
                "",
            ]

        lines.extend(_render_evidence(dispute.new_assertion, "新断言"))
        for i, assertion in enumerate(dispute.existing_assertions, 1):
            lines.extend(_render_evidence(assertion, f"现有断言 {i}"))

        if features_a is not None and features_b is not None:
            lines.extend(self._render_score_breakdown(features_a, features_b))

        lines.extend(
            [
                "## 解决方案",
                "",
                "- [ ] **采纳新断言** — 旧断言标记为 deprecated",
                "- [ ] **保留旧断言** — 忽略新断言",
                "- [ ] **保留双方** — 添加上下文说明各自的适用范围",
                "- [ ] **需要更多信息** — 暂不决定，等待更多证据",
                "",
                "## 影响评估",
                "",
            ]
        )

        total_refs = dispute.new_assertion.reference_count + sum(
            a.reference_count for a in dispute.existing_assertions
        )
        lines.append(f"受影响页面总数：{total_refs}")

        return "\n".join(lines)

    def _render_score_breakdown(
        self,
        features_a: RelationFeatures,
        features_b: RelationFeatures,
    ) -> List[str]:
        """渲染评分明细表格与可视化说明"""
        scorer = self._get_scorer()
        weights = scorer.current_weights()
        score_a = scorer.composite_score(features_a)
        score_b = scorer.composite_score(features_b)
        gap = abs(score_a - score_b)

        lines = [
            "",
            "## 评分明细",
            "",
            "| 维度 | 权重 | 新断言 | 现有断言 | 加权差 |",
            "|------|------|--------|----------|--------|",
        ]

        dimension_labels = {
            "confidence": "置信度",
            "freshness": "新鲜度",
            "citation": "引用数",
            "quality": "质量",
            "source": "来源可信度",
            "core": "核心度",
        }

        total_diff = 0.0
        for dim in _DIMENSIONS:
            w = weights.get(dim, 0.0)
            va = getattr(features_a, dim, 0.0)
            vb = getattr(features_b, dim, 0.0)
            diff = w * (va - vb)
            total_diff += diff
            sign = "+" if diff >= 0 else ""
            label = dimension_labels.get(dim, dim)
            lines.append(f"| {label} ({dim}) | {w:.3f} | {va:.3f} | {vb:.3f} | {sign}{diff:+.3f} |")

        sign = "+" if total_diff >= 0 else ""
        lines.append(
            f"| **综合分** | - | **{score_a:.3f}** | **{score_b:.3f}** | **{sign}{total_diff:+.3f}** |"
        )

        lines.extend(
            [
                "",
                f"- 综合分差距：{gap:.3f}",
                f"- 当前阈值：自动裁决≥{scorer.auto_gap:.2f} / 合并边界≥{scorer.merge_gap:.2f}",
            ]
        )

        if gap >= scorer.auto_gap:
            suggested = "auto_resolve"
        elif gap >= scorer.merge_gap:
            suggested = "merge"
        else:
            suggested = "create_dispute"
        lines.append(f"- 建议动作：{suggested}")
        lines.append("")

        return lines

    def _apply_resolution_to_relations(self, dispute_page_path: str, winner: str = "a") -> None:
        """根据裁决结果把败方关系标记为 deprecated。

        从争议页 frontmatter 读取 dispute_pair，定位两条关系，
        把败方（winner 对方）的 confidence/strength 置 0，并在 context
        中追加 deprecation 标记。
        """
        try:
            full_path = self.wiki_base / dispute_page_path
            if not full_path.exists():
                return

            content = full_path.read_text(encoding="utf-8")
            frontmatter, _ = parse_frontmatter(content)
            pair_key = fm_get(frontmatter, "dispute_pair", "") if frontmatter else ""
            if not pair_key:
                logger.debug("争议页缺少 dispute_pair，跳过 KG 更新: %s", dispute_page_path)
                return

            rel_a, rel_b = self._load_pair_relations(pair_key)
            if rel_a is None or rel_b is None:
                logger.debug("无法从 dispute_pair 加载关系: %s", pair_key)
                return

            loser_rel = rel_b if winner == "a" else rel_a
            note = f" [deprecated by dispute resolution, winner={winner}]"
            new_context = (loser_rel.context or "").rstrip() + note

            from core.kia.knowledge_graph import KnowledgeGraph

            kg = KnowledgeGraph(
                wiki_base=str(self.wiki_base),
                db_path=str(self.db_path) if self.db_path else None,
            )
            with kg._conn() as conn:
                conn.execute(
                    """UPDATE relations
                       SET confidence=0, strength=0, context=?
                       WHERE source=? AND target=? AND relation_type=?""",
                    (
                        new_context,
                        loser_rel.source,
                        loser_rel.target,
                        loser_rel.relation_type.value,
                    ),
                )
                conn.commit()

            logger.info(
                "争议败方关系已标记 deprecated: %s -> %s (winner=%s)",
                loser_rel.source,
                loser_rel.target,
                winner,
            )
        except DISPUTE_OPERATION_ERRORS as e:
            logger.warning("应用裁决到 KG 失败: %s", e, exc_info=True)

    def rollback_resolution_context(self, dispute_page_path: str) -> int:
        """移除 resolve keep_both 同步到原始页面的争议上下文块。"""
        try:
            dispute_path = self._resolve_wiki_path(dispute_page_path)
            if not dispute_path.exists():
                return 0

            marker_id = self._context_marker_id(dispute_path)
            content = dispute_path.read_text(encoding="utf-8")
            page_paths = self._extract_dispute_original_paths(content)
            pattern = self._context_block_pattern(marker_id)
            updated = 0

            for original_path in page_paths:
                try:
                    original_content = original_path.read_text(encoding="utf-8")
                    new_content, count = pattern.subn("\n", original_content)
                    if count:
                        self._write_or_propose(
                            original_path,
                            new_content.rstrip() + "\n",
                            proposed_action="rollback_dispute_context",
                            evidence_refs=[str(dispute_path)],
                            expected_existing_hash=sha256_text(original_content),
                        )
                        updated += 1
                except DISPUTE_OPERATION_ERRORS as e:
                    logger.warning("Failed to rollback dispute context in %s: %s", original_path, e)

            if updated:
                rollback_note = (
                    "\n\n---\n"
                    f"**争议上下文回滚**: 已从 {updated} 个原始页面移除同步上下文\n"
                    f"**回滚时间**: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                )
                self._write_or_propose(
                    dispute_path,
                    content.rstrip() + rollback_note,
                    proposed_action="record_dispute_context_rollback",
                    evidence_refs=[str(dispute_path)],
                    expected_existing_hash=sha256_text(content),
                )

            return updated
        except DISPUTE_OPERATION_ERRORS as e:
            logger.warning("Failed to rollback dispute context: %s", e, exc_info=True)
            return 0

    def _add_context_to_both(self, dispute_page_path: str, context: str) -> int:
        """给双方原始页面添加上下文，引用争议页面"""
        try:
            dispute_path = self._resolve_wiki_path(dispute_page_path)
            if not dispute_path.exists():
                return 0

            content = dispute_path.read_text(encoding="utf-8")
            page_paths = self._extract_dispute_original_paths(content)
            marker_id = self._context_marker_id(dispute_path)
            start_marker = f"<!-- mnemos-dispute-context:start {marker_id} -->"
            end_marker = f"<!-- mnemos-dispute-context:end {marker_id} -->"
            updated = 0

            # 在每个原始页面末尾添加争议引用
            for original_path in page_paths:
                try:
                    original_content = original_path.read_text(encoding="utf-8")
                    if start_marker in original_content:
                        continue
                    rel_link = self._relative_link(original_path.parent, dispute_path)
                    note = (
                        f"\n\n{start_marker}\n"
                        f"> [!note] 争议仲裁补充\n"
                        f"> 争议页：[{dispute_path.stem}]({rel_link})\n"
                        f"> 裁决上下文：{context}\n"
                        f"{end_marker}\n"
                    )
                    self._write_or_propose(
                        original_path,
                        original_content.rstrip() + note,
                        proposed_action="add_dispute_context",
                        evidence_refs=[str(dispute_path)],
                        expected_existing_hash=sha256_text(original_content),
                    )
                    updated += 1
                    logger.debug("Added dispute link to %s", original_path)
                except DISPUTE_OPERATION_ERRORS as e:
                    logger.warning("Failed to add dispute context to %s: %s", original_path, e)
            return updated
        except DISPUTE_OPERATION_ERRORS as e:
            logger.warning("Failed to add context to both sides: %s", e, exc_info=True)
            return 0

    def _write_or_propose(
        self,
        path: Path,
        content: str,
        *,
        proposed_action: str,
        evidence_refs: List[str],
        metadata: Optional[Dict[str, Any]] = None,
        expected_existing_hash: str | None = None,
    ) -> bool:
        if proposed_action not in DISPUTE_MARKDOWN_ACTIONS:
            raise ValueError(
                f"unsupported dispute Markdown action: {proposed_action}"
            )
        normalized_evidence_refs = [
            self._normalize_dispute_evidence_ref(ref) for ref in evidence_refs
        ]
        if not normalized_evidence_refs or any(
            not ref for ref in normalized_evidence_refs
        ):
            raise ValueError("dispute Markdown action requires exact evidence refs")
        service = TrustedVaultMutationService(wiki_base=self.wiki_base)
        binding = trusted_markdown_material_action_binding(
            target_path=path,
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
                contract_id=DISPUTE_DECISION_CONTRACT_ID,
                contract_revision_id=DISPUTE_DECISION_CONTRACT_REVISION,
                contract_text=DISPUTE_DECISION_CONTRACT_TEXT,
                source_namespace="dispute-markdown-action",
                source_facts={
                    "schema_version": "mnemos.dispute_markdown_action_facts.v1",
                    "decision_created_at": decision_created_at,
                    "proposed_action": proposed_action,
                    "target_path": str(path.expanduser().resolve(strict=False)),
                    "content_hash": sha256_text(content),
                    "expected_existing_hash": str(expected_existing_hash or ""),
                    "evidence_refs": normalized_evidence_refs,
                    "metadata": dict(metadata or {}),
                },
                decision_checks={
                    "registered_dispute_action": (
                        proposed_action in DISPUTE_MARKDOWN_ACTIONS
                    ),
                    "evidence_refs_resolved": bool(normalized_evidence_refs)
                    and all(normalized_evidence_refs),
                },
                evidence_refs=tuple(
                    dict.fromkeys(
                        (
                            f"dispute-action:{proposed_action}",
                            *normalized_evidence_refs,
                        )
                    )
                ),
                task=f"Apply dispute Wiki action {proposed_action}",
                goal=(
                    "Mutate only the exact Wiki page derived from the current dispute."
                ),
                constraints=(
                    "The action must be a registered dispute mutation.",
                    "The target, content, before hash, and evidence cannot drift.",
                ),
                created_at=decision_created_at,
                producer="dispute-resolver",
                producer_version=DISPUTE_DECISION_CONTRACT_REVISION,
                producer_code_hash=DISPUTE_DECISION_PRODUCER_HASH,
                evaluator_id="dispute-markdown-action-evaluator",
                approved_candidate_key=(
                    "apply_exact_evidence_bound_dispute_mutation"
                ),
                approved_candidate_summary=(
                    "Apply the exact registered dispute mutation bound to its evidence."
                ),
                rejected_candidate_key="reject_unbound_dispute_mutation",
                rejected_candidate_summary=(
                    "Reject an unregistered or drifted dispute page mutation."
                ),
                approved_reason_code="dispute_mutation_binding_verified",
                rejected_reason_code="dispute_mutation_binding_rejected",
                committed_metric="dispute_markdown_mutation_receipt",
                rejected_metric="unbound_dispute_markdown_mutation_count",
            )
        trusted = service.submit_markdown(
            target_path=path,
            content=content,
            source="dispute_resolver",
            actor="system",
            evidence_refs=normalized_evidence_refs,
            proposed_action=proposed_action,
            expected_existing_hash=expected_existing_hash,
            metadata=metadata or {},
            material_action=material_action,
        )
        if trusted.intercepted:
            return True
        commit_trusted_markdown(
            trusted,
            target_path=path,
            content=content,
            material_action=material_action,
        )
        return True

    @staticmethod
    def _normalize_dispute_evidence_ref(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        evidence_type = str(getattr(value, "evidence_type", "") or "").strip()
        content = str(getattr(value, "content", "") or "").strip()
        created_at = str(getattr(value, "created_at", "") or "").strip()
        if evidence_type and content:
            digest = sha256_text(
                f"{evidence_type}\n{content}\n{created_at}"
            )
            return f"relation-evidence:{evidence_type}:{digest}"
        return ""

    def _extract_dispute_original_paths(self, dispute_content: str) -> set[Path]:
        """从争议页正文中的 Markdown 链接提取 wiki 内原始页面路径。"""
        refs = re.findall(r"\[([^\]]+)\]\(([^)]+)\)", dispute_content)
        page_paths: set[Path] = set()
        for _, path in refs:
            if path.startswith(("http://", "https://", "#")):
                continue
            abs_path = self._resolve_wiki_path(path)
            if abs_path.exists() and abs_path.suffix == ".md":
                page_paths.add(abs_path)
        return page_paths

    def _context_marker_id(self, dispute_path: Path) -> str:
        try:
            rel = dispute_path.relative_to(self.wiki_base)
        except ValueError:
            rel = dispute_path
        digest = hashlib.sha1(rel.as_posix().encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
        return f"dispute-{digest}"

    def _context_block_pattern(self, marker_id: str) -> re.Pattern[str]:
        return re.compile(
            rf"\n*<!-- mnemos-dispute-context:start {re.escape(marker_id)} -->.*?"
            rf"<!-- mnemos-dispute-context:end {re.escape(marker_id)} -->\n*",
            re.DOTALL,
        )

    def _relative_link(self, from_dir: Path, target: Path) -> str:
        return os.path.relpath(target, start=from_dir).replace(os.sep, "/")
