"""
Knowledge Entropy Engine - 知识熵减引擎

自动检测并建议合并知识库中的冗余/相似内容：
1. 完全重复（相似度 > 0.95）→ 建议删除其中一个
2. 高度相似（0.80-0.95）→ 建议合并为一个，保留更完整版本
3. 部分重叠（0.60-0.80）→ 建议建立关系，不合并
4. 互补（0.40-0.60）→ 建议互相引用，形成知识网络

设计原则：
- 利用 DNA 指纹计算相似度，不重复建索引
- 只生成建议，不自动执行合并（避免误删）
- 合并策略基于内容完整性、时效性、置信度综合判断
- 输出结构化报告，支持批量处理
"""
# Eris — 纷争女神 — 熵引擎，知识混乱度与新鲜度计算
# 原模块: entropy_engine.py



import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from core.config import get_config


@dataclass
class MergeCandidate:
    """合并候选"""
    page_a: str
    page_b: str
    similarity: float
    merge_strategy: str      # delete_duplicate / merge_into_one / link_related / cross_reference
    reason: str
    recommended_action: str
    keep_page: str = ""      # 建议保留的页面（合并/删除时）
    confidence: float = 0.0


@dataclass
class EntropyReport:
    """熵减报告"""
    total_pairs_scanned: int = 0
    candidates: List[MergeCandidate] = field(default_factory=list)
    estimated_savings: Dict[str, int] = field(default_factory=dict)  # 预估节省

    @property
    def duplicate_count(self) -> int:
        return sum(1 for c in self.candidates if c.merge_strategy == "delete_duplicate")

    @property
    def mergeable_count(self) -> int:
        return sum(1 for c in self.candidates if c.merge_strategy == "merge_into_one")

    @property
    def linkable_count(self) -> int:
        return sum(1 for c in self.candidates if c.merge_strategy == "link_related")


class EntropyEngine:
    """知识熵减引擎"""

    # 相似度阈值
    DUPLICATE_THRESHOLD = 0.95
    MERGE_THRESHOLD = 0.80
    LINK_THRESHOLD = 0.60

    def __init__(self, wiki_base: str = None):
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else (
            get_config().wiki_dir
        )
        self.inbox = self.wiki_base / "00-Inbox"

        # 懒加载 DNA 引擎
        self._dna_engine = None

    @property
    def dna_engine(self):
        if self._dna_engine is None:
            try:
                from .genos import DNAEngine
                self._dna_engine = DNAEngine(wiki_base=str(self.wiki_base))
            except ImportError:
                self._dna_engine = None
        return self._dna_engine

    def scan(self, sample_size: int = None) -> EntropyReport:
        """
        扫描知识库，找出合并候选

        Args:
            sample_size: 限制扫描的页面对数（用于测试）

        Returns:
            EntropyReport
        """
        if not self.inbox.exists():
            return EntropyReport()

        # 1. 为所有页面计算 DNA
        pages = list(self.inbox.glob("*.md"))
        dnas = []
        for page in pages:
            if self.dna_engine:
                dna = self.dna_engine.compute_dna(page)
                if dna:
                    self.dna_engine.save_dna(dna)
                    dnas.append(dna)

        if len(dnas) < 2:
            return EntropyReport()

        # 2. 两两比较（优化：只比较同领域/同类型的页面）
        candidates = []
        compared = set()

        for i, dna_a in enumerate(dnas):
            for j, dna_b in enumerate(dnas[i + 1:], i + 1):
                if sample_size and len(compared) >= sample_size:
                    break

                pair_key = tuple(sorted([dna_a.page_path, dna_b.page_path]))
                if pair_key in compared:
                    continue
                compared.add(pair_key)

                # 快速过滤：不同领域+不同类型 跳过
                if not self._should_compare(dna_a, dna_b):
                    continue

                # 计算相似度
                result = self.dna_engine.compare(dna_a, dna_b)

                if result.overall_score >= self.LINK_THRESHOLD:
                    candidate = self._generate_candidate(
                        dna_a, dna_b, result
                    )
                    if candidate:
                        candidates.append(candidate)

            if sample_size and len(compared) >= sample_size:
                break

        # 3. 按相似度排序
        candidates.sort(key=lambda x: x.similarity, reverse=True)

        # 4. 去重（避免 A-B 和 B-A 同时出现）
        seen_pairs = set()
        unique = []
        for c in candidates:
            pair = tuple(sorted([c.page_a, c.page_b]))
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                unique.append(c)

        return EntropyReport(
            total_pairs_scanned=len(compared),
            candidates=unique,
        )

    def _should_compare(self, dna_a, dna_b) -> bool:
        """判断是否值得比较两个 DNA"""
        # 提取领域和类型
        domain_a = dna_a.semantic_signature.split(":")[0] if ":" in dna_a.semantic_signature else ""
        domain_b = dna_b.semantic_signature.split(":")[0] if ":" in dna_b.semantic_signature else ""

        type_a = dna_a.semantic_signature.split(":")[1] if ":" in dna_a.semantic_signature else ""
        type_b = dna_b.semantic_signature.split(":")[1] if ":" in dna_b.semantic_signature else ""

        # 同领域 或 同类型 才比较
        if domain_a and domain_b and domain_a == domain_b:
            return True
        if type_a and type_b and type_a == type_b:
            return True

        # 如果有工具重叠，也比较
        tool_overlap = dna_a.tool_entities & dna_b.tool_entities
        if tool_overlap:
            return True

        return False

    def _generate_candidate(self, dna_a, dna_b, similarity_result) -> Optional[MergeCandidate]:
        """基于相似度结果生成合并候选"""
        score = similarity_result.overall_score

        if score >= self.DUPLICATE_THRESHOLD:
            return self._suggest_delete_duplicate(dna_a, dna_b, score)
        elif score >= self.MERGE_THRESHOLD:
            return self._suggest_merge(dna_a, dna_b, score)
        elif score >= self.LINK_THRESHOLD:
            return self._suggest_link(dna_a, dna_b, score)

        return None

    def _suggest_delete_duplicate(self, dna_a, dna_b, score) -> MergeCandidate:
        """建议删除重复"""
        # 选择保留更完整的页面
        keep = self._choose_better_page(dna_a, dna_b)
        discard = dna_b.page_path if keep == dna_a.page_path else dna_a.page_path

        return MergeCandidate(
            page_a=dna_a.page_path,
            page_b=dna_b.page_path,
            similarity=round(score, 3),
            merge_strategy="delete_duplicate",
            reason=f"相似度 {score:.0%}，内容高度重复",
            recommended_action=f"删除 '{Path(discard).name}'，保留 '{Path(keep).name}'（内容更完整）",
            keep_page=keep,
            confidence=score,
        )

    def _suggest_merge(self, dna_a, dna_b, score) -> MergeCandidate:
        """建议合并"""
        keep = self._choose_better_page(dna_a, dna_b)
        merge_from = dna_b.page_path if keep == dna_a.page_path else dna_a.page_path

        # 分析互补内容
        complement = self._analyze_complement(dna_a, dna_b)

        reason = f"相似度 {score:.0%}，主题高度重叠"
        if complement:
            reason += f"，互补内容: {complement}"

        return MergeCandidate(
            page_a=dna_a.page_path,
            page_b=dna_b.page_path,
            similarity=round(score, 3),
            merge_strategy="merge_into_one",
            reason=reason,
            recommended_action=f"将 '{Path(merge_from).name}' 的内容合并到 '{Path(keep).name}'，然后删除前者",
            keep_page=keep,
            confidence=score,
        )

    def _suggest_link(self, dna_a, dna_b, score) -> MergeCandidate:
        """建议建立关系"""
        # 判断关系类型
        relation_type = self._infer_relation_type(dna_a, dna_b)

        return MergeCandidate(
            page_a=dna_a.page_path,
            page_b=dna_b.page_path,
            similarity=round(score, 3),
            merge_strategy="link_related",
            reason=f"相似度 {score:.0%}，主题相关但各有侧重",
            recommended_action=f"在两者之间建立 '{relation_type}' 关系，不合并",
            confidence=score,
        )

    def _choose_better_page(self, dna_a, dna_b) -> str:
        """选择更优质的页面保留"""
        scores = {}
        for dna in [dna_a, dna_b]:
            score = 0
            # 置信度高加分
            score += dna.confidence * 2
            # 关键词多加分
            score += len(dna.keyword_set) * 0.1
            # 标题模式清晰加分
            if dna.title_pattern != "statement":
                score += 0.3
            scores[dna.page_path] = score

        return max(scores, key=scores.get)

    def _analyze_complement(self, dna_a, dna_b) -> str:
        """分析两个页面的互补内容"""
        a_unique = dna_a.keyword_set - dna_b.keyword_set
        b_unique = dna_b.keyword_set - dna_a.keyword_set

        parts = []
        if a_unique:
            parts.append(f"A 独有: {', '.join(list(a_unique)[:3])}")
        if b_unique:
            parts.append(f"B 独有: {', '.join(list(b_unique)[:3])}")

        return "; ".join(parts)

    def _infer_relation_type(self, dna_a, dna_b) -> str:
        """推断两个页面之间的关系类型"""
        # 简单的启发式判断
        title_a = Path(dna_a.page_path).stem.lower()
        title_b = Path(dna_b.page_path).stem.lower()

        # 标题包含关系
        if title_a in title_b or title_b in title_a:
            return "specializes/generalizes"

        # 工具实体重叠
        tool_overlap = dna_a.tool_entities & dna_b.tool_entities
        if tool_overlap:
            return "similar_to"

        # 场景重叠
        scenario_overlap = dna_a.scenario_tags & dna_b.scenario_tags
        if scenario_overlap:
            return "related_to"

        return "references"

    # ========== 报告生成 ==========

    def generate_report(self, report: EntropyReport) -> str:
        """生成 Markdown 格式的熵减报告"""
        lines = [
            "# 知识熵减报告",
            f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            f"扫描对数: {report.total_pairs_scanned}",
            f"发现候选: {len(report.candidates)}",
            f"- 完全重复: {report.duplicate_count}",
            f"- 可合并: {report.mergeable_count}",
            f"- 建议关联: {report.linkable_count}",
            "",
        ]

        if not report.candidates:
            lines.append("✅ 知识库健康，未发现冗余内容\n")
            return "\n".join(lines)

        # 按策略分组
        strategy_groups = {
            "delete_duplicate": ([], "🔴 完全重复（建议删除）"),
            "merge_into_one": ([], "🟠 高度相似（建议合并）"),
            "link_related": ([], "🟡 部分重叠（建议关联）"),
        }

        for candidate in report.candidates:
            if candidate.merge_strategy in strategy_groups:
                strategy_groups[candidate.merge_strategy][0].append(candidate)

        for (candidates, title) in strategy_groups.values():
            if not candidates:
                continue
            lines.extend([f"## {title}", ""])
            for i, c in enumerate(candidates[:10], 1):
                name_a = Path(c.page_a).name
                name_b = Path(c.page_b).name
                lines.append(f"{i}. **{name_a}** ↔ **{name_b}** (相似度 {c.similarity:.0%})")
                lines.append(f"   - 理由: {c.reason}")
                lines.append(f"   - 建议: {c.recommended_action}")
                lines.append("")

        return "\n".join(lines)

    def auto_fix(self, report: EntropyReport,
                 apply_duplicates: bool = False,
                 apply_links: bool = True) -> List[str]:
        """
        自动执行低风险的熵减操作

        Args:
            apply_duplicates: 是否自动删除完全重复（默认否，安全考虑）
            apply_links: 是否自动建立关系（默认是）

        Returns:
            操作日志
        """
        logs = []

        for candidate in report.candidates:
            if candidate.merge_strategy == "delete_duplicate" and apply_duplicates:
                # 删除重复页面
                discard = candidate.page_b if candidate.keep_page == candidate.page_a else candidate.page_a
                try:
                    Path(discard).unlink()
                    logs.append(f"已删除重复页面: {Path(discard).name}")
                except Exception as e:
                    logs.append(f"删除失败 {Path(discard).name}: {e}")

            elif candidate.merge_strategy == "link_related" and apply_links:
                # 自动建立关系
                try:
                    from .knowledge_graph import KnowledgeGraph, Relation, RelationType
                    kg = KnowledgeGraph(wiki_base=str(self.wiki_base))
                    rel_type = self._map_to_relation_type(candidate)
                    rel = Relation(
                        source=candidate.page_a,
                        target=candidate.page_b,
                        relation_type=rel_type,
                        strength=candidate.similarity,
                        confidence=candidate.confidence,
                        source_method="entropy_engine",
                    )
                    if kg.add_relation(rel):
                        logs.append(f"已建立关系: {Path(candidate.page_a).name} {rel_type.value} {Path(candidate.page_b).name}")
                except Exception as e:
                    logs.append(f"建立关系失败: {e}")

        return logs

    def _map_to_relation_type(self, candidate: MergeCandidate):
        """将策略映射到关系类型"""
        try:
            from .relation_schema import RelationType
        except ImportError:
            return None

        if "specializes" in candidate.reason:
            return RelationType.SPECIALIZES
        elif "similar" in candidate.reason:
            return RelationType.SIMILAR_TO
        else:
            return RelationType.REFERENCES


# ========== 便捷函数 ==========

def run_entropy_scan(wiki_base: str = None) -> EntropyReport:
    """便捷函数：运行熵减扫描"""
    engine = EntropyEngine(wiki_base=wiki_base)
    return engine.scan()


def run_and_report(wiki_base: str = None) -> str:
    """便捷函数：运行扫描并生成报告"""
    engine = EntropyEngine(wiki_base=wiki_base)
    report = engine.scan()
    return engine.generate_report(report)
