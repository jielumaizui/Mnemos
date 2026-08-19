# -*- coding: utf-8 -*-
"""Relation discovery, traversal, and export operations for KnowledgeGraph."""

from __future__ import annotations

import re
import sqlite3
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from core.kia.relation_endpoint_quality import is_derived_kg_scan_path

from .relation_schema import (
    Relation,
    RelationEvidence,
    RelationType,
    suggest_relation_type,
)


@dataclass
class PathNode:
    """路径节点"""

    page: str
    relation_type: str
    strength: float


@dataclass
class KnowledgePath:
    """知识路径"""

    nodes: List[PathNode]
    total_strength: float = 0.0
    length: int = 0


class KnowledgeGraphDiscoveryMixin:
    """Automatic relation discovery and graph-analysis surface."""

    # Structural contract supplied by the ``KnowledgeGraph`` facade and its
    # projection mixin.  Annotation-only members avoid a second runtime owner.
    wiki_base: Path
    MAX_CANDIDATE_PAGES: int
    _conn: Callable[[], AbstractContextManager[sqlite3.Connection]]
    _extract_frontmatter: Callable[[str], Dict[str, Any]]
    _extract_title: Callable[[str], str]
    _extract_all_keywords: Callable[[Dict[str, Any]], List[str]]
    _extract_wiki_links: Callable[[str], List[str]]
    _extract_anti_patterns_from_body: Callable[[str], List[str]]
    _extract_hash_prefix: Callable[[str], Optional[str]]
    prepare_relation_candidates: Callable[[List[Path]], Dict[Path, Dict[str, Any]]]
    _build_relation_context: Callable[[Relation], str]
    add_relation: Callable[[Relation], bool]
    get_relations: Callable[..., List[Relation]]
    get_incoming_relations: Callable[..., List[Relation]]

    _CONFIDENCE_CEILING: Dict[str, float] = {
        "same_directory": 0.45,
        "hash_prefix_series": 0.55,
        "keyword_overlap": 0.65,
        "link_parse": 0.8,
        "anti_pattern_match": 0.75,
        "body_anti_pattern": 0.65,
        "title_containment": 0.7,
        "domain_containment": 0.7,
        "keyword_relation": 0.5,
    }

    def _candidate_existing_pages(self) -> List[Path]:
        """获取候选现有页面列表，按修改时间由新到旧排序并限制数量。"""
        pages = []
        for p in self.wiki_base.rglob("*.md"):
            if is_derived_kg_scan_path(p, self.wiki_base):
                continue
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            pages.append((mtime, p))
        # 最近修改的页面优先，更有可能与新页面相关
        pages.sort(key=lambda x: x[0], reverse=True)
        return [p for _, p in pages[: self.MAX_CANDIDATE_PAGES]]

    def _rel_path(self, path: Path) -> str:
        """将路径转为相对于 wiki_base 的字符串；无法相对时返回原路径。"""
        if str(path).startswith(str(self.wiki_base)):
            return str(path.relative_to(self.wiki_base))
        return str(path)

    def _prepare_new_page(self, new_page_path: Path, new_content: str) -> Dict[str, Any]:
        """解析新页面内容，提取 frontmatter、标题、关键词、链接与正文。"""
        new_meta = self._extract_frontmatter(new_content)
        new_title = self._extract_title(new_content) or new_page_path.stem
        new_keywords = self._extract_all_keywords(new_meta)
        new_links = self._extract_wiki_links(new_content)
        body_text = new_content.split("---", 2)[-1] if "---" in new_content else new_content
        return {
            "meta": new_meta,
            "title": new_title,
            "keywords": new_keywords,
            "links": new_links,
            "body_text": body_text,
        }

    def _relations_from_links(self, new_links: List[str], rel_source: str) -> List[Relation]:
        """解析 [[链接]] → REFERENCES 关系。"""
        relations: List[Relation] = []
        for link_target in new_links:
            clean_target = link_target.split("/")[-1] if "/" in link_target else link_target
            relations.append(
                Relation(
                    source=rel_source,
                    target=clean_target,
                    relation_type=RelationType.REFERENCES,
                    strength=0.85,
                    confidence=0.9,
                    source_method="link_parse",
                    evidence=[
                        RelationEvidence(
                            evidence_type="wiki_link",
                            content=f"页面中显式链接到 [[{link_target}]]",
                        )
                    ],
                )
            )
        return relations

    @staticmethod
    def _relation_from_keyword_overlap(
        new_keywords: List[str],
        existing_keywords: List[str],
        rel_source: str,
        rel_target: str,
    ) -> Optional[Relation]:
        """关键词高度重叠时生成 SIMILAR_TO 关系。"""
        overlap = set(new_keywords) & set(existing_keywords)
        if not overlap:
            return None
        overlap_ratio = len(overlap) / max(len(new_keywords), len(existing_keywords), 1)
        if overlap_ratio < 0.7:
            return None
        return Relation(
            source=rel_source,
            target=rel_target,
            relation_type=RelationType.SIMILAR_TO,
            strength=min(overlap_ratio + 0.3, 0.9),
            confidence=overlap_ratio,
            source_method="keyword_overlap",
            evidence=[
                RelationEvidence(
                    evidence_type="keyword_overlap",
                    content=f"共同关键词: {', '.join(list(overlap)[:5])}",
                )
            ],
        )

    @staticmethod
    def _relations_from_frontmatter_anti_patterns(
        new_anti: List[str],
        existing_title: str,
        rel_source: str,
        rel_target: str,
    ) -> List[Relation]:
        """frontmatter 显式反模式与现有标题匹配 → CONTRADICTS。"""
        relations: List[Relation] = []
        existing_title_parts = existing_title.lower().split()
        for anti in new_anti:
            anti_lower = anti.lower()
            if any(part in anti_lower for part in existing_title_parts if len(part) > 2):
                relations.append(
                    Relation(
                        source=rel_source,
                        target=rel_target,
                        relation_type=RelationType.CONTRADICTS,
                        strength=0.75,
                        confidence=0.85,
                        source_method="anti_pattern_match",
                        evidence=[
                            RelationEvidence(
                                evidence_type="anti_pattern_quote",
                                content=f"反模式提及: {anti[:100]}",
                            )
                        ],
                    )
                )
        return relations

    def _relations_from_body_anti_patterns(
        self,
        body_text: str,
        existing_title_parts: List[str],
        existing_keywords: List[str],
        rel_source: str,
        rel_target: str,
    ) -> List[Relation]:
        """正文反模式句式提取 → CONTRADICTS（带数量与长度限制）。"""
        MAX_BODY_ANTI_PATTERN_PER_PAGE = 5
        MIN_MATCH_LEN = 4
        relations: List[Relation] = []
        anti_patterns = self._extract_anti_patterns_from_body(body_text)
        for anti in anti_patterns:
            if len(relations) >= MAX_BODY_ANTI_PATTERN_PER_PAGE:
                break
            anti_lower = anti.lower()
            matched = False
            for part in existing_title_parts:
                if len(part) >= MIN_MATCH_LEN and part in anti_lower:
                    matched = True
                    break
            if not matched:
                for kw in existing_keywords:
                    if len(kw) >= MIN_MATCH_LEN and kw.lower() in anti_lower:
                        matched = True
                        break
            if matched:
                relations.append(
                    Relation(
                        source=rel_source,
                        target=rel_target,
                        relation_type=RelationType.CONTRADICTS,
                        strength=0.65,
                        confidence=0.75,
                        source_method="body_anti_pattern",
                        evidence=[
                            RelationEvidence(
                                evidence_type="anti_pattern_quote",
                                content=f"正文反模式: {anti[:100]}",
                            )
                        ],
                    )
                )
        return relations

    @staticmethod
    def _relations_from_title_containment(
        new_title: str,
        existing_title: str,
        rel_source: str,
        rel_target: str,
    ) -> List[Relation]:
        """标题包含关系 → SPECIALIZES / GENERALIZES。"""
        relations: List[Relation] = []
        et = existing_title.lower()
        nt = new_title.lower()
        if et in nt and len(existing_title) > 5:
            ratio = len(existing_title) / max(len(new_title), 1)
            if ratio >= 0.4:
                relations.append(
                    Relation(
                        source=rel_source,
                        target=rel_target,
                        relation_type=RelationType.SPECIALIZES,
                        strength=0.7,
                        confidence=0.75,
                        source_method="title_containment",
                        evidence=[
                            RelationEvidence(
                                evidence_type="title_match",
                                content=f"标题包含: '{existing_title}' in '{new_title}'",
                            )
                        ],
                    )
                )
        elif nt in et and len(new_title) > 5:
            ratio = len(new_title) / max(len(existing_title), 1)
            if ratio >= 0.4:
                relations.append(
                    Relation(
                        source=rel_source,
                        target=rel_target,
                        relation_type=RelationType.GENERALIZES,
                        strength=0.7,
                        confidence=0.75,
                        source_method="title_containment",
                        evidence=[
                            RelationEvidence(
                                evidence_type="title_match",
                                content=f"标题被包含: '{new_title}' in '{existing_title}'",
                            )
                        ],
                    )
                )
        return relations

    @staticmethod
    def _relations_from_domain_containment(
        new_meta: Dict[str, Any],
        existing_meta: Dict[str, Any],
        rel_source: str,
        rel_target: str,
    ) -> List[Relation]:
        """领域层级包含 → SPECIALIZES / GENERALIZES。"""
        relations: List[Relation] = []
        new_domain = new_meta.get("领域", "")
        existing_domain = existing_meta.get("领域", "")
        if (
            not new_domain
            or not existing_domain
            or not isinstance(new_domain, str)
            or not isinstance(existing_domain, str)
        ):
            return relations

        new_parts = [p.strip() for p in new_domain.split("/")]
        existing_parts = [p.strip() for p in existing_domain.split("/")]
        if (
            len(new_parts) > len(existing_parts)
            and existing_parts == new_parts[: len(existing_parts)]
        ):
            relations.append(
                Relation(
                    source=rel_source,
                    target=rel_target,
                    relation_type=RelationType.SPECIALIZES,
                    strength=0.6,
                    confidence=0.7,
                    source_method="domain_containment",
                    evidence=[
                        RelationEvidence(
                            evidence_type="domain_match",
                            content=f"领域包含: '{new_domain}' ⊃ '{existing_domain}'",
                        )
                    ],
                )
            )
        elif len(existing_parts) > len(new_parts) and new_parts == existing_parts[: len(new_parts)]:
            relations.append(
                Relation(
                    source=rel_source,
                    target=rel_target,
                    relation_type=RelationType.GENERALIZES,
                    strength=0.6,
                    confidence=0.7,
                    source_method="domain_containment",
                    evidence=[
                        RelationEvidence(
                            evidence_type="domain_match",
                            content=f"领域包含: '{existing_domain}' ⊃ '{new_domain}'",
                        )
                    ],
                )
            )
        return relations

    def _relation_from_hash_prefix(
        self,
        new_page_path: Path,
        existing_path: Path,
        rel_source: str,
        rel_target: str,
    ) -> Optional[Relation]:
        """相同哈希前缀 → 主题系列 PART_OF。"""
        new_prefix = self._extract_hash_prefix(new_page_path.stem)
        existing_prefix = self._extract_hash_prefix(existing_path.stem)
        if (
            new_prefix
            and existing_prefix
            and new_prefix == existing_prefix
            and new_page_path.stem != existing_path.stem
        ):
            return Relation(
                source=rel_source,
                target=rel_target,
                relation_type=RelationType.PART_OF,
                strength=0.75,
                confidence=0.8,
                source_method="hash_prefix_series",
                evidence=[
                    RelationEvidence(
                        evidence_type="series_match",
                        content=f"同一主题系列: {new_prefix}",
                    )
                ],
            )
        return None

    def _relation_from_directory(self, new_page_path: Path, rel_source: str) -> Optional[Relation]:
        """同目录结构 → PART_OF（页面 → 目录）。"""
        if new_page_path.parent == self.wiki_base:
            return None
        dir_name = new_page_path.parent.name
        dir_rel_path = str(new_page_path.parent.relative_to(self.wiki_base))
        return Relation(
            source=rel_source,
            target=dir_rel_path,
            relation_type=RelationType.PART_OF,
            strength=0.6,
            confidence=0.75,
            source_method="same_directory",
            evidence=[
                RelationEvidence(
                    evidence_type="directory_proximity",
                    content=f"所在目录: {dir_name}",
                )
            ],
        )

    @staticmethod
    def _deduplicate_and_limit_relations(
        discovered: List[Relation],
        max_relations: int,
        max_keyword_relations: int,
    ) -> List[Relation]:
        """按 source+target+type 去重，限制 keyword_relation 数量与总数量。"""
        seen: Set[Tuple[str, str, str]] = set()
        unique = []
        for rel in discovered:
            key = (rel.source, rel.target, rel.relation_type.value)
            if key not in seen:
                seen.add(key)
                unique.append(rel)

        keyword_count = sum(1 for r in unique if r.source_method == "keyword_relation")
        if keyword_count > max_keyword_relations:
            kept = []
            keyword_kept = 0
            for rel in unique:
                if rel.source_method == "keyword_relation":
                    if keyword_kept < max_keyword_relations:
                        kept.append(rel)
                        keyword_kept += 1
                else:
                    kept.append(rel)
            unique = kept

        if len(unique) > max_relations:
            unique = sorted(unique, key=lambda r: r.confidence, reverse=True)[:max_relations]
        return unique

    def _compare_existing_page(
        self,
        existing_path: Path,
        new_page_path: Path,
        ctx: Dict[str, Any],
        rel_source: str,
        candidate: Dict[str, Any] | None = None,
    ) -> List[Relation]:
        """将新页面与一个现有页面比对，返回发现的关系。"""
        if candidate is None:
            candidate = self.prepare_relation_candidates([existing_path]).get(existing_path)
        if candidate is None:
            return []
        existing_meta = candidate["meta"]
        existing_title = candidate["title"]
        existing_keywords = candidate["keywords"]
        rel_target = candidate["rel_target"]
        new_title = ctx["title"]
        new_meta = ctx["meta"]
        new_keywords = ctx["keywords"]
        body_text = ctx["body_text"]

        relations: List[Relation] = []
        rel = self._relation_from_keyword_overlap(
            new_keywords, existing_keywords, rel_source, rel_target
        )
        if rel is not None:
            relations.append(rel)

        relations.extend(
            self._relations_from_frontmatter_anti_patterns(
                new_meta.get("反模式", []) or [], existing_title, rel_source, rel_target
            )
        )

        relations.extend(
            self._relations_from_body_anti_patterns(
                body_text,
                existing_title.lower().split(),
                existing_keywords,
                rel_source,
                rel_target,
            )
        )

        relations.extend(
            self._relations_from_title_containment(
                new_title, existing_title, rel_source, rel_target
            )
        )

        relations.extend(
            self._relations_from_domain_containment(new_meta, existing_meta, rel_source, rel_target)
        )

        rel = self._relation_from_hash_prefix(new_page_path, existing_path, rel_source, rel_target)
        if rel is not None:
            relations.append(rel)

        return relations

    def discover_relations(
        self,
        new_page_path: Path,
        existing_pages: List[Path] | None = None,
        new_content: str | None = None,
        candidate_cache: Dict[Path, Dict[str, Any]] | None = None,
    ) -> List[Relation]:
        """
        自动发现新页面与现有页面的关系

        Args:
            new_page_path: 新页面路径
            existing_pages: 现有页面列表（可选）；建议事件处理入口一次性传入，避免重复 I/O
            new_content: 可选，已读取的新页面内容（避免重复 I/O）

        发现策略：
        1. 解析 [[链接]] → REFERENCES 关系
        2. frontmatter 关键词重叠 → SIMILAR_TO / PART_OF
        3. 反模式文本匹配 → CONTRADICTS
        4. 标题关键词匹配 → BUILDS_ON / SPECIALIZES
        5. 正文关系关键词兜底 → 各类关系（保守，仅当目标页面标题同句出现）
        """
        MAX_RELATIONS_PER_PAGE = 10
        MAX_KEYWORD_RELATION_PER_PAGE = 5

        if not new_page_path.exists():
            return []

        if new_content is None:
            new_content = new_page_path.read_text(encoding="utf-8")
        ctx = self._prepare_new_page(new_page_path, new_content)

        if existing_pages is None:
            existing_pages = self._candidate_existing_pages()

        rel_source = self._rel_path(new_page_path)

        discovered: List[Relation] = []
        discovered.extend(self._relations_from_links(ctx["links"], rel_source))

        for existing_path in existing_pages:
            if existing_path == new_page_path:
                continue
            discovered.extend(
                self._compare_existing_page(
                    existing_path,
                    new_page_path,
                    ctx,
                    rel_source,
                    (candidate_cache or {}).get(existing_path),
                )
            )

        keyword_rels = self._discover_relations_from_text(
            rel_source,
            ctx["body_text"],
            existing_pages,
            candidate_cache=candidate_cache,
        )
        discovered.extend(keyword_rels)

        dir_rel = self._relation_from_directory(new_page_path, rel_source)
        if dir_rel is not None:
            discovered.append(dir_rel)

        return self._deduplicate_and_limit_relations(
            discovered,
            MAX_RELATIONS_PER_PAGE,
            MAX_KEYWORD_RELATION_PER_PAGE,
        )

    def _discover_relations_from_text(
        self,
        rel_source: str,
        body_text: str,
        existing_pages: List[Path],
        *,
        candidate_cache: Dict[Path, Dict[str, Any]] | None = None,
    ) -> List[Relation]:
        """
        基于正文关系关键词做保守兜底发现。

        策略：
        1. 把正文拆成句子；
        2. 对每个句子提取中文/英文候选词；
        3. 用 suggest_relation_type 判断是否存在关系关键词；
        4. 只有在同一句子中出现现有页面标题/名称时，才建立关系；
        5. 置信度上限 0.5，strength 按匹配分打折，避免噪声污染图谱。

        这是 discover_relations 的最后一道防线，只在结构化规则未命中时工作。
        """
        discovered: List[Relation] = []
        if not body_text or not existing_pages:
            return discovered

        # 预计算候选页面（排除源页面本身）
        candidates: List[Tuple[str, str]] = []
        for existing_path in existing_pages:
            if existing_path.name == rel_source:
                continue
            candidate = (candidate_cache or {}).get(existing_path)
            if candidate is None:
                candidate = self.prepare_relation_candidates([existing_path]).get(existing_path)
            if candidate is None:
                continue
            candidates.append((str(candidate["rel_target"]), str(candidate["title"])))

        if not candidates:
            return discovered

        sentences = re.split(r"[。！？\n!?]+", body_text)
        seen: Set[Tuple[str, str, str]] = set()

        for raw_sentence in sentences:
            sentence = raw_sentence.strip()
            if len(sentence) < 4:
                continue

            # 提取候选词：中文2字以上 或 英文2字母以上
            terms = re.findall(r"[一-鿿]{2,}|[a-zA-Z_]{2,}", sentence)
            if not terms:
                continue

            try:
                suggestions = suggest_relation_type(terms)
            # DEBT(S8): 容错跳过，避免单条记录中断批量处理
            except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError):
                continue
            if not suggestions:
                continue

            sentence_lower = sentence.lower()
            for rel_type, score in suggestions:
                if score < 0.6:
                    continue

                for rel_target, title in candidates:
                    title_lower = title.lower()
                    if len(title_lower) <= 2:
                        continue
                    if title_lower not in sentence_lower:
                        continue

                    key = (rel_source, rel_target, rel_type.value)
                    if key in seen:
                        continue
                    seen.add(key)

                    discovered.append(
                        Relation(
                            source=rel_source,
                            target=rel_target,
                            relation_type=rel_type,
                            strength=round(score * 0.6, 2),
                            confidence=score,
                            source_method="keyword_relation",
                            evidence=[
                                RelationEvidence(
                                    evidence_type="keyword_match",
                                    content=f"正文关系关键词匹配，目标 '{title}' 出现在同句: {sentence[:120]}",
                                )
                            ],
                        )
                    )

        return discovered

    def prepare_discovered_relations(
        self,
        relations: List[Relation],
        min_confidence: float = 0.5,
    ) -> List[Relation]:
        """Freeze eligible relation payloads before their formal effects run."""

        prepared: List[Relation] = []
        for rel in relations:
            ceiling = self._CONFIDENCE_CEILING.get(rel.source_method, 1.0)
            rel.confidence = min(rel.confidence, ceiling)
            if rel.confidence >= min_confidence:
                if not rel.context or not rel.context.strip():
                    rel.context = self._build_relation_context(rel)
                prepared.append(rel)
        return prepared

    def apply_discovered(self, relations: List[Relation], min_confidence: float = 0.5) -> int:
        """将发现的关系写入数据库（过滤低置信度，应用 source_method 上限）"""
        count = 0
        for rel in self.prepare_discovered_relations(relations, min_confidence):
            if self.add_relation(rel):
                count += 1
        return count

    def find_path(
        self, from_page: str, to_page: str, max_depth: int = 4, min_strength: float = 0.3
    ) -> Optional[KnowledgePath]:
        """
        查找从 A 到 B 的知识路径（BFS + 加权）

        Returns:
            最短且强度最高的路径，或 None
        """
        if from_page == to_page:
            return KnowledgePath(nodes=[], total_strength=1.0, length=0)

        # BFS，优先队列按累计强度排序
        from heapq import heappush, heappop

        visited = set()
        queue: List[Tuple[float, str, List[PathNode]]] = [
            (-1.0, from_page, [])
        ]  # (-strength, current_page, path)

        while queue:
            neg_strength, current, path = heappop(queue)
            strength = -neg_strength

            if current in visited:
                continue
            visited.add(current)

            if len(path) >= max_depth:
                continue

            # 获取当前页面的出边
            rels = self.get_relations(current, min_confidence=min_strength)
            for rel in rels:
                if rel.strength < min_strength:
                    continue

                new_path = path + [
                    PathNode(
                        page=rel.target,
                        relation_type=rel.relation_type.value,
                        strength=rel.strength,
                    )
                ]

                if rel.target == to_page:
                    return KnowledgePath(
                        nodes=new_path,
                        total_strength=strength * rel.strength,
                        length=len(new_path),
                    )

                if rel.target not in visited:
                    heappush(queue, (-strength * rel.strength, rel.target, new_path))

        return None

    def get_related_cluster(self, page: str, depth: int = 2, min_strength: float = 0.3) -> Set[str]:
        """获取页面的关联簇（N 度邻居）"""
        cluster = {page}
        current_layer = {page}

        for _ in range(depth):
            next_layer = set()
            for node in current_layer:
                rels = self.get_relations(node, min_confidence=min_strength)
                for rel in rels:
                    if rel.strength >= min_strength:
                        next_layer.add(rel.target)
                # 入边也考虑
                incoming = self.get_incoming_relations(node, min_confidence=min_strength)
                for rel in incoming:
                    if rel.strength >= min_strength:
                        next_layer.add(rel.source)
            cluster.update(next_layer)
            current_layer = next_layer - cluster
            if not current_layer:
                break

        return cluster

    def detect_conflicts(
        self,
        conflict_resolver: Any | None = None,
    ) -> List[Tuple[Relation, Relation, str]]:
        """
        检测知识冲突

        返回 [(关系1, 关系2, 冲突描述), ...]

        冲突类型：
        1. 直接矛盾：A contradicts B 且 B contradicts A（正常）
        2. 逻辑矛盾：A builds_on B 且 A contradicts B
        3. 替代矛盾：A replaces B 且 B replaces A（循环替代）
        4. 演化矛盾：A evolved_from B 且 B evolved_from A
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT source, target, relation_type, strength, confidence, source_method FROM relations"  # noqa: E501
            ).fetchall()

        relations = []
        for row in rows:
            try:
                relation_type = RelationType(row["relation_type"])
            except ValueError:
                continue
            relations.append(
                Relation(
                    source=row["source"],
                    target=row["target"],
                    relation_type=relation_type,
                    strength=row["strength"],
                    confidence=row["confidence"],
                    source_method=row["source_method"],
                )
            )

        if conflict_resolver is None:
            from core.kia.conflict_resolver import ConflictResolver

            conflict_resolver = ConflictResolver()

        return conflict_resolver.detect_relation_conflicts(relations)  # type: ignore[return-value]

    def get_contradiction_pairs(self, page: str) -> List[Relation]:
        """获取与某页面存在矛盾关系的所有页面"""
        contradictions = []

        # 出边 contradicts
        out_rels = self.get_relations(page, relation_type=RelationType.CONTRADICTS)
        contradictions.extend(out_rels)

        # 入边 contradicts（对称关系自动维护，但保险起见也查）
        in_rels = self.get_incoming_relations(page)
        for rel in in_rels:
            if rel.relation_type == RelationType.CONTRADICTS:
                contradictions.append(rel)

        return contradictions

    def export_mermaid(self, page: str, depth: int = 1, min_strength: float = 0.3) -> str:
        """
        导出 Mermaid 图（用于嵌入 Obsidian）

        Returns:
            Mermaid flowchart 语法字符串
        """
        cluster = self.get_related_cluster(page, depth=depth, min_strength=min_strength)

        lines = ["```mermaid", "flowchart TD"]
        node_ids = {}

        for i, node in enumerate(cluster):
            node_id = f"N{i}"
            node_ids[node] = node_id
            # 简化显示：只显示文件名
            label = Path(node).stem if "/" in node else node
            lines.append(f'    {node_id}["{label}"]')

        # 收集关系边
        edges = set()
        for node in cluster:
            rels = self.get_relations(node, min_confidence=min_strength)
            for rel in rels:
                if rel.target in cluster and rel.strength >= min_strength:
                    edge_key = (
                        node_ids.get(node),
                        node_ids.get(rel.target),
                        rel.relation_type.value,
                    )
                    if edge_key not in edges:
                        edges.add(edge_key)
                        lines.append(
                            f"    {edge_key[0]} -->|{rel.relation_type.value}({rel.strength:.1f})| {edge_key[1]}"  # noqa: E501
                        )

        lines.append("```")
        return "\n".join(lines)

    def export_dataview_query(self, page: str) -> str:
        """
        导出 Dataview 查询（Obsidian 插件）

        生成可在 Obsidian Dataview 中运行的查询语句
        """
        return f"""```dataview
    TABLE relation_type, strength, confidence
    FROM ""
    WHERE file.path = "{page}"
    ```

    > Dataview 目前不支持直接查询外部关系数据库。
    > 建议将关键关系同步到 frontmatter 的 `relations` 字段后使用。
    """

    def export_frontmatter_relations(self, page: str) -> List[Dict]:
        """
        导出适合写入 frontmatter 的关系列表

        Returns:
            [{target, type, strength}, ...]
        """
        rels = self.get_relations(page)
        return [
            {
                "target": Path(rel.target).stem if "/" in rel.target else rel.target,
                "type": rel.relation_type.value,
                "strength": round(rel.strength, 2),
            }
            for rel in rels
        ]

    def get_stats(self) -> Dict:
        """获取图谱统计"""
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
            type_counts = conn.execute(
                "SELECT relation_type, COUNT(*) FROM relations GROUP BY relation_type"
            ).fetchall()
            avg_confidence = (
                conn.execute("SELECT AVG(confidence) FROM relations").fetchone()[0] or 0
            )
            avg_strength = conn.execute("SELECT AVG(strength) FROM relations").fetchone()[0] or 0

        return {
            "total_relations": total,
            "type_distribution": {row[0]: row[1] for row in type_counts},
            "avg_confidence": round(avg_confidence, 3),
            "avg_strength": round(avg_strength, 3),
        }

    def get_hub_pages(self, top_n: int = 10) -> List[Tuple[str, int]]:
        """获取枢纽页面（连接数最多的页面）"""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT source, COUNT(*) as out_count
                FROM relations
                GROUP BY source
                ORDER BY out_count DESC
                LIMIT ?
            """,
                (top_n,),
            ).fetchall()
        return [(row[0], row[1]) for row in rows]
