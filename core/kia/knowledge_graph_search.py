"""Hidden-relation suggestions and allowlisted Wiki search for KnowledgeGraph."""

from __future__ import annotations

import logging
import os
from pathlib import Path
import random
import re
import sqlite3
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Set, Tuple

from core.db_utils import sqlite_conn

from .relation_manager import RelationSuggestion

logger = logging.getLogger(__name__)

HIDDEN_RELATION_STRATEGY_ERRORS = (
    ImportError,
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    KeyError,
    sqlite3.Error,
)


class KnowledgeGraphSearchMixin:
    """Own hidden-relation suggestions and authorization-bounded search."""

    if TYPE_CHECKING:
        wiki_base: Path
        embedding_index_dir: Optional[Path]
        _embedding_client: Any
        _runtime_config: Any

        def _conn(self) -> AbstractContextManager[sqlite3.Connection]: ...

    # ========== 自动关系发现 ==========

    # 关系发现时默认最多考虑的现有页面数，避免全 wiki O(n²) 扫描
    MAX_CANDIDATE_PAGES = 500

    # ========== 隐藏关系建议（暗知识 + 量子纠缠合并入口） ==========

    def suggest_hidden_relations(
        self,
        max_depth: int = 2,
        min_strength: float = 0.4,
        max_start_nodes: int = 50,
        max_dna_pairs: int = 100,
    ) -> List[RelationSuggestion]:
        """发现隐藏关系建议（只读，不写入数据库）。

        合并原暗知识隐性关联与量子纠缠四种发现策略：
        1. 间接路径关联（A→B→C）
        2. 语义深层关联（DNA 相似但关键词重叠低）
        3. 跨域共振（不同领域但共享概念/工具）
        4. 互补纠缠（低相似 + 高互补）
        同时吸收同 session 查询共现作为输入之一。
        """
        direct_pairs = self._load_direct_pairs()
        suggestions: List[RelationSuggestion] = []

        try:
            suggestions.extend(
                self._suggest_indirect_paths(direct_pairs, max_depth, min_strength, max_start_nodes)
            )
        except HIDDEN_RELATION_STRATEGY_ERRORS as e:
            logger.debug("隐藏关系-间接路径策略失败: %s", e)

        try:
            suggestions.extend(self._suggest_cross_domain(direct_pairs, min_strength))
        except HIDDEN_RELATION_STRATEGY_ERRORS as e:
            logger.debug("隐藏关系-跨域共振策略失败: %s", e)

        try:
            suggestions.extend(
                self._suggest_semantic_deep(direct_pairs, min_strength, max_dna_pairs)
            )
        except HIDDEN_RELATION_STRATEGY_ERRORS as e:
            logger.debug("隐藏关系-语义深层策略失败: %s", e)

        try:
            suggestions.extend(self._suggest_complementary(min_strength, max_dna_pairs))
        except HIDDEN_RELATION_STRATEGY_ERRORS as e:
            logger.debug("隐藏关系-互补纠缠策略失败: %s", e)

        try:
            suggestions.extend(self._suggest_trail_cooccurrence())
        except HIDDEN_RELATION_STRATEGY_ERRORS as e:
            logger.debug("隐藏关系-trail 共现策略失败: %s", e)

        return self._deduplicate_suggestions(suggestions)

    def _load_direct_pairs(self) -> Set[frozenset]:
        """加载已有直接关系的端点集合（无向）。"""
        pairs: Set[frozenset] = set()
        try:
            with self._conn() as conn:
                rows = conn.execute("SELECT source, target FROM relations").fetchall()
            for row in rows:
                pairs.add(frozenset([row["source"], row["target"]]))
        except sqlite3.Error as e:
            logger.debug("加载直接关系失败: %s", e)
        return pairs

    def _suggest_indirect_paths(
        self,
        direct_pairs: Set[frozenset],
        max_depth: int,
        min_strength: float,
        max_start_nodes: int,
    ) -> List[RelationSuggestion]:
        """基于关系表 BFS 发现间接路径 A→B→C。"""
        edges = []
        try:
            with self._conn() as conn:
                rows = conn.execute("SELECT source, target, strength FROM relations").fetchall()
            edges = [dict(row) for row in rows]
        except sqlite3.Error:
            return []

        graph: Dict[str, List[Tuple[str, float]]] = {}
        for edge in edges:
            strength = edge.get("strength") or 0.0
            if strength < min_strength:
                continue
            src, tgt = edge["source"], edge["target"]
            graph.setdefault(src, []).append((tgt, strength))
            graph.setdefault(tgt, []).append((src, strength))

        start_nodes = list(graph.keys())
        if len(start_nodes) > max_start_nodes:
            random.shuffle(start_nodes)
            start_nodes = start_nodes[:max_start_nodes]

        suggestions = []
        seen: Set[Tuple[frozenset, Tuple[str, ...]]] = set()

        for start in start_nodes:
            queue = [(start, [start], 1.0)]
            while queue:
                node, path, strength = queue.pop(0)
                if len(path) > max_depth:
                    continue
                for neighbor, weight in graph.get(node, []):
                    if neighbor in path:
                        continue
                    new_path = path + [neighbor]
                    new_strength = strength * weight
                    if len(new_path) >= 3:
                        endpoints = frozenset([new_path[0], new_path[-1]])
                        if endpoints in direct_pairs:
                            continue
                        intermediates = tuple(sorted(new_path[1:-1]))
                        key = (endpoints, intermediates)
                        if key in seen:
                            continue
                        seen.add(key)
                        if new_strength >= min_strength:
                            a, b = sorted([new_path[0], new_path[-1]])
                            suggestions.append(
                                RelationSuggestion(
                                    source=a,
                                    target=b,
                                    relation_type="related_to",
                                    confidence=round(new_strength, 4),
                                    reason=f"通过 {' → '.join(new_path)} 间接关联",
                                    evidence_type="indirect_path",
                                )
                            )
                    queue.append((neighbor, new_path, new_strength))

        return suggestions

    def _load_domain_map(self) -> Dict[str, Dict]:
        """加载 Wiki 页面的领域、核心概念、工具实体。"""
        domain_map: Dict[str, Dict] = {}
        if not self.wiki_base.exists():
            return domain_map

        for md in self.wiki_base.rglob("*.md"):
            rel = str(md.relative_to(self.wiki_base))
            try:
                text = md.read_text(encoding="utf-8", errors="ignore")
                fm = self._extract_frontmatter(text)
                if not fm:
                    continue
                domain = fm.get("领域") or fm.get("domain")
                keywords = fm.get("关键词", {})
                concepts, tools = set(), set()
                if isinstance(keywords, dict):
                    concepts = set(keywords.get("核心概念", []))
                    tools = set(keywords.get("工具实体", []))
                domain_map[rel] = {
                    "domain": domain,
                    "concepts": {c.lower() for c in concepts if isinstance(c, str)},
                    "tools": {t.lower() for t in tools if isinstance(t, str)},
                }
            # DEBT(S8): 容错跳过，避免单条记录中断批量处理
            except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError):
                continue
        return domain_map

    def _suggest_cross_domain(
        self,
        direct_pairs: Set[frozenset],
        min_strength: float,
    ) -> List[RelationSuggestion]:
        """发现跨域关系：不同领域页面间的现有关系，或无直接关系但共享概念/工具。"""
        domain_map = self._load_domain_map()
        if not domain_map:
            return []

        suggestions = self._suggest_cross_domain_existing(domain_map, min_strength, direct_pairs)
        concept_pages, tool_pages = self._build_domain_entity_maps(domain_map)
        suggestions.extend(
            self._suggest_cross_domain_shared(domain_map, direct_pairs, "核心概念", concept_pages)
        )
        suggestions.extend(
            self._suggest_cross_domain_shared(domain_map, direct_pairs, "工具实体", tool_pages)
        )
        return suggestions

    def _build_domain_entity_maps(
        self, domain_map: Dict[str, Dict]
    ) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
        """按核心概念/工具实体聚合页面，用于跨域共享检测。"""
        concept_pages: Dict[str, List[str]] = {}
        tool_pages: Dict[str, List[str]] = {}
        for page, info in domain_map.items():
            for c in info.get("concepts", set()):
                concept_pages.setdefault(c, []).append(page)
            for t in info.get("tools", set()):
                tool_pages.setdefault(t, []).append(page)
        return concept_pages, tool_pages

    def _suggest_cross_domain_existing(
        self,
        domain_map: Dict[str, Dict],
        min_strength: float,
        direct_pairs: Set[frozenset],
    ) -> List[RelationSuggestion]:
        """从已有关系中提取跨域边。"""
        suggestions = []
        try:
            with self._conn() as conn:
                rows = conn.execute("SELECT source, target, strength FROM relations").fetchall()
            for row in rows:
                src_info = domain_map.get(row["source"])
                tgt_info = domain_map.get(row["target"])
                if not src_info or not tgt_info:
                    continue
                if not (
                    src_info["domain"]
                    and tgt_info["domain"]
                    and src_info["domain"] != tgt_info["domain"]
                ):
                    continue
                a, b = sorted([row["source"], row["target"]])
                strength = row["strength"] or 0.5
                if strength >= min_strength:
                    suggestions.append(
                        RelationSuggestion(
                            source=a,
                            target=b,
                            relation_type="related_to",
                            confidence=round(strength, 4),
                            reason=f"跨域关联：{src_info['domain']} → {tgt_info['domain']}",
                            evidence_type="cross_domain",
                        )
                    )
        except sqlite3.Error:
            logger.warning("[knowledge_graph] sqlite3.Error suppressed", exc_info=True)
        return suggestions

    def _suggest_cross_domain_shared(
        self,
        domain_map: Dict[str, Dict],
        direct_pairs: Set[frozenset],
        label: str,
        entity_map: Dict[str, List[str]],
    ) -> List[RelationSuggestion]:
        """发现共享同一实体但属于不同领域的页面。"""
        suggestions = []
        added = set()
        for _, pages in entity_map.items():
            if len(pages) < 2:
                continue
            for i in range(len(pages)):
                for j in range(i + 1, len(pages)):
                    a, b = sorted([pages[i], pages[j]])
                    key = (a, b)
                    if key in added or frozenset([a, b]) in direct_pairs:
                        continue
                    info_a, info_b = domain_map[a], domain_map[b]
                    if not info_a["domain"] or not info_b["domain"]:
                        continue
                    if info_a["domain"] == info_b["domain"]:
                        continue
                    added.add(key)
                    suggestions.append(
                        RelationSuggestion(
                            source=a,
                            target=b,
                            relation_type="related_to",
                            confidence=0.55,
                            reason=f"跨域共享{label}",
                            evidence_type="cross_domain",
                        )
                    )
        return suggestions

    def _load_dna_index(self) -> Dict[str, Any]:
        """加载 DNA 库索引，返回 {page_path: KnowledgeDNA}。"""
        try:
            from core.kia.genos import DNAEngine

            engine = DNAEngine(wiki_base=str(self.wiki_base))
            if not engine.db_path.exists():
                return {}
            with sqlite_conn(str(engine.db_path), timeout=10) as conn:
                rows = conn.execute("SELECT page_path FROM knowledge_dna").fetchall()
            dnas = {}
            for (page_path,) in rows:
                dna = engine.load_dna(page_path)
                if dna:
                    dnas[page_path] = dna
            return dnas
        except HIDDEN_RELATION_STRATEGY_ERRORS as e:
            logger.debug("加载 DNA 索引失败: %s", e)
            return {}

    def _suggest_semantic_deep(
        self,
        direct_pairs: Set[frozenset],
        min_strength: float,
        max_dna_pairs: int,
    ) -> List[RelationSuggestion]:
        """语义深层关联：DNA 相似度高但关键词重叠低。"""
        dnas = self._load_dna_index()
        if len(dnas) < 2:
            return []

        try:
            from core.kia.genos import DNAEngine

            engine = DNAEngine(wiki_base=str(self.wiki_base))
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            ImportError,
        ):  # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
            return []

        pages = list(dnas.keys())
        pairs = set()  # type: ignore[var-annotated]
        attempts = 0
        while len(pairs) < max_dna_pairs and attempts < max_dna_pairs * 10:
            attempts += 1
            a, b = random.sample(pages, 2)  # nosec B311: non-crypto sampling
            pair = tuple(sorted([a, b]))
            pairs.add(pair)

        suggestions = []
        for a, b in pairs:
            if frozenset([a, b]) in direct_pairs:
                continue
            dna_a, dna_b = dnas[a], dnas[b]
            try:
                result = engine.compare(dna_a, dna_b)
            # DEBT(S8): 容错跳过，避免单条记录中断批量处理
            except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError):
                continue
            keyword_overlap = self._jaccard(dna_a.keyword_set, dna_b.keyword_set)
            if result.overall_score >= 0.55 and keyword_overlap < 0.3:
                confidence = max(min_strength, result.overall_score)
                suggestions.append(
                    RelationSuggestion(
                        source=a,
                        target=b,
                        relation_type="similar_to",
                        confidence=round(confidence, 4),
                        reason=f"DNA 语义相似 {result.overall_score:.2f}，关键词重叠低",
                        evidence_type="dna_similarity",
                    )
                )

        return suggestions

    def _suggest_complementary(
        self,
        min_strength: float,
        max_dna_pairs: int,
    ) -> List[RelationSuggestion]:
        """互补纠缠：低 DNA 相似 + 高关键词/工具互补。"""
        dnas = self._load_dna_index()
        if len(dnas) < 2:
            return []

        try:
            from core.kia.genos import DNAEngine

            engine = DNAEngine(wiki_base=str(self.wiki_base))
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            ImportError,
        ):  # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
            return []

        pages = list(dnas.keys())
        pairs = set()  # type: ignore[var-annotated]
        attempts = 0
        while len(pairs) < max_dna_pairs and attempts < max_dna_pairs * 10:
            attempts += 1
            a, b = random.sample(pages, 2)  # nosec B311: non-crypto sampling
            pair = tuple(sorted([a, b]))
            pairs.add(pair)

        suggestions = []
        for a, b in pairs:
            dna_a, dna_b = dnas[a], dnas[b]
            try:
                result = engine.compare(dna_a, dna_b)
            # DEBT(S8): 容错跳过，避免单条记录中断批量处理
            except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError):
                continue
            if result.overall_score >= 0.4:
                continue

            keyword_comp = self._complement_score(dna_a.keyword_set, dna_b.keyword_set)
            tool_comp = self._complement_score(dna_a.tool_entities, dna_b.tool_entities)
            title_diff = 0.0
            if (
                dna_a.title_pattern
                and dna_b.title_pattern
                and dna_a.title_pattern != dna_b.title_pattern
            ):
                title_diff = 1.0

            comp_score = keyword_comp * 0.5 + tool_comp * 0.3 + title_diff * 0.2
            if comp_score >= 0.6:
                confidence = max(min_strength, comp_score)
                suggestions.append(
                    RelationSuggestion(
                        source=a,
                        target=b,
                        relation_type="related_to",
                        confidence=round(confidence, 4),
                        reason="知识互补：关键词/工具互补度高但语义相似度低",
                        evidence_type="complementary",
                    )
                )

        return suggestions

    def _suggest_trail_cooccurrence(
        self,
        min_confidence: float = 0.5,
        min_frequency: int = 2,
    ) -> List[RelationSuggestion]:
        """从 KnowledgeTrail 的同 session 查询共现中发现隐性关联。"""
        try:
            from core.kia.ariadne import KnowledgeTrail

            trail = KnowledgeTrail(wiki_base=str(self.wiki_base))
            if not trail.db_path.exists():
                return []
            with trail._conn() as conn:
                rows = conn.execute("""SELECT session_id, page_path
                       FROM trail_events
                       WHERE event_type = 'query'
                         AND session_id IS NOT NULL AND session_id != ''
                       ORDER BY timestamp""").fetchall()
        except HIDDEN_RELATION_STRATEGY_ERRORS as e:
            logger.debug("读取 trail 共现失败: %s", e)
            return []

        sessions: Dict[str, List[str]] = {}
        page_counts: Dict[str, int] = {}
        for row in rows:
            sessions.setdefault(row["session_id"], []).append(row["page_path"])
            page_counts[row["page_path"]] = page_counts.get(row["page_path"], 0) + 1

        pair_counts: Dict[Tuple[str, str], int] = {}
        for pages in sessions.values():
            unique = []
            seen = set()
            for p in pages:
                if p not in seen:
                    seen.add(p)
                    unique.append(p)
            for i in range(len(unique) - 1):
                a, b = sorted([unique[i], unique[i + 1]])
                pair_counts[(a, b)] = pair_counts.get((a, b), 0) + 1

        suggestions = []
        for (a, b), co_count in pair_counts.items():
            if co_count < min_frequency:
                continue
            count_a = page_counts.get(a, 0)
            count_b = page_counts.get(b, 0)
            union = count_a + count_b - co_count
            confidence = co_count / max(union, 1)
            if confidence < min_confidence:
                continue
            suggestions.append(
                RelationSuggestion(
                    source=a,
                    target=b,
                    relation_type="co_occurs",
                    confidence=round(confidence, 4),
                    reason=f"同 session 查询中共现 {co_count} 次",
                    evidence_type="trail_cooccurrence",
                )
            )

        return suggestions

    @staticmethod
    def _jaccard(set_a: Set[str], set_b: Set[str]) -> float:
        if not set_a and not set_b:
            return 1.0
        return len(set_a & set_b) / len(set_a | set_b)

    @staticmethod
    def _complement_score(set_a: Set[str], set_b: Set[str]) -> float:
        if not set_a and not set_b:
            return 0.0
        diff = (set_a - set_b) | (set_b - set_a)
        union = set_a | set_b
        return len(diff) / len(union)

    @staticmethod
    def _deduplicate_suggestions(
        suggestions: List[RelationSuggestion],
    ) -> List[RelationSuggestion]:
        best: Dict[Tuple[str, str, str], RelationSuggestion] = {}
        for s in suggestions:
            key = (s.source, s.target, s.relation_type)
            if key not in best or s.confidence > best[key].confidence:
                best[key] = s
        return sorted(best.values(), key=lambda x: x.confidence, reverse=True)

    # ========== 搜索召回（供 ContextAwareSearch 使用）==========

    # 与 core/app/context_search.py 对齐的排除目录
    EXCLUDED_DIRS = {".git", ".obsidian", ".kg", "__pycache__"}

    def _normalize_search_query(self, query: str) -> List[str]:
        """将查询拆分为小写关键词列表。"""
        keywords = [kw.strip().lower() for kw in query.split() if len(kw.strip()) > 1]
        if not keywords:
            keywords = [query.lower().strip()]
        return keywords

    def _search_semantic(
        self,
        query: str,
        limit: int,
        results_map: Dict[str, Dict],
        allowed_page_paths: set[str],
    ) -> None:
        """语义召回（embedding 优先）。"""
        try:
            from core.config import get_config

            cfg = get_config()
            if not cfg.get("embedding.enabled", True):
                return
            if self.embedding_index_dir is None or not self.embedding_index_dir.is_dir():
                return

            from core.embeddings import EmbeddingIndexManager

            idx = EmbeddingIndexManager(
                wiki_base=self.wiki_base,
                index_dir=self.embedding_index_dir,
                client=self._embedding_client,
                config=self._runtime_config,
            )
            semantic_results = idx.search(
                query,
                top_k=limit,
                allowed_page_paths=allowed_page_paths,
            )
            for rel_path, sim in semantic_results:
                if not isinstance(rel_path, (str, os.PathLike)):
                    continue
                try:
                    page_path = (self.wiki_base / rel_path).resolve()
                    if self.wiki_base not in page_path.parents and page_path != self.wiki_base:
                        continue
                    canonical_rel_path = page_path.relative_to(self.wiki_base).as_posix()
                except (ValueError, OSError):
                    continue
                if canonical_rel_path not in allowed_page_paths:
                    continue
                if not page_path.exists():
                    continue
                try:
                    content = page_path.read_text(encoding="utf-8", errors="ignore")
                except (OSError, IOError):
                    logger.warning("[knowledge_graph] (OSError, IOError) suppressed", exc_info=True)
                    continue
                title = page_path.stem
                results_map[canonical_rel_path] = {
                    "page_path": canonical_rel_path,
                    "title": title,
                    "content": content,
                    "entity_name": title,
                    "_score": sim * 2,  # 语义结果加权
                    "_semantic": True,
                }
        except (OSError, IOError):
            logger.warning("[knowledge_graph] (OSError, IOError) suppressed", exc_info=True)

    def _search_relations(
        self,
        keywords: List[str],
        results_map: Dict[str, Dict],
        allowed_page_paths: set[str],
    ) -> None:
        """从关系数据库召回（优先 FTS5，回退 LIKE）。"""
        try:
            with self._conn() as conn:
                fts_query = " OR ".join(keywords)
                try:
                    rows = conn.execute(
                        """SELECT r.source, r.target, r.relation_type, r.strength, r.confidence
                           FROM relations_fts fts
                           JOIN relations r ON fts.rowid = r.id
                           WHERE fts.content MATCH ?""",
                        (fts_query,),
                    ).fetchall()
                except sqlite3.Error:
                    rows = []

                if not rows:
                    conditions = []
                    params = []
                    for kw in keywords:
                        like = f"%{kw}%"
                        conditions.extend(
                            [
                                "source LIKE ?",
                                "target LIKE ?",
                                "relation_type LIKE ?",
                                "context LIKE ?",
                            ]
                        )
                        params.extend([like, like, like, like])

                    where = " OR ".join(conditions)
                    rows = conn.execute(
                        f"""SELECT source, target, relation_type, strength, confidence
                            FROM relations WHERE {where}""",  # nosec B608
                        params,
                    ).fetchall()

                for row in rows:
                    for col in ("source", "target"):
                        page_path = row[col]
                        rel_path = self._to_rel_path(page_path)
                        if rel_path not in allowed_page_paths:
                            continue
                        score = row["strength"] * row["confidence"]
                        if rel_path in results_map:
                            results_map[rel_path]["_score"] += score
                        else:
                            title = Path(page_path).stem
                            results_map[rel_path] = {
                                "page_path": rel_path,
                                "title": title,
                                "content": "",
                                "entity_name": title,
                                "_score": score,
                            }
        except sqlite3.Error as e:
            logger.warning("KG 数据库搜索失败: %s", e)

    def _is_excluded_wiki_path(self, rel_parts: Tuple[str, ...]) -> bool:
        """判断相对路径是否命中排除目录。"""
        return any(part in self.EXCLUDED_DIRS or part.startswith(".") for part in rel_parts)

    @staticmethod
    def _merge_wiki_file_result(
        rel_path: str,
        content: str,
        title: str,
        fm: Dict,
        match_count: int,
        results_map: Dict[str, Dict],
    ) -> None:
        """将单个 Wiki 文件的匹配结果合并到结果映射中。"""
        if rel_path in results_map:
            results_map[rel_path]["title"] = title
            results_map[rel_path]["content"] = content
            results_map[rel_path]["entity_name"] = title
            results_map[rel_path]["frontmatter"] = fm
            results_map[rel_path]["_score"] += match_count * 0.1
        else:
            results_map[rel_path] = {
                "page_path": rel_path,
                "title": title,
                "content": content,
                "entity_name": title,
                "frontmatter": fm,
                "_score": match_count * 0.1,
            }

    def _search_wiki_files(
        self,
        keywords: List[str],
        results_map: Dict[str, Dict],
        allowed_page_paths: set[str],
    ) -> None:
        """从 Wiki 文件召回（补充内容和标题）。"""
        if not self.wiki_base.exists():
            return

        for rel_path in sorted(allowed_page_paths):
            try:
                md_file = (self.wiki_base / rel_path).resolve()
                if self.wiki_base not in md_file.parents or not md_file.is_file():
                    continue
                rel_parts = md_file.relative_to(self.wiki_base).parts
                if self._is_excluded_wiki_path(rel_parts):
                    continue

                content = md_file.read_text(encoding="utf-8", errors="ignore")
                content_lower = content.lower()

                match_count = sum(1 for kw in keywords if kw in content_lower)
                if match_count == 0:
                    continue

                rel_path = md_file.relative_to(self.wiki_base).as_posix()
                title = self._extract_title(content) or md_file.stem
                fm = self._extract_frontmatter(content)
                self._merge_wiki_file_result(rel_path, content, title, fm, match_count, results_map)
            except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError):
                logging.getLogger(__name__).warning(
                    "Caught unexpected error at knowledge_graph.py", exc_info=True
                )
                continue

    def _finalize_search_results(self, results_map: Dict[str, Dict], limit: int) -> List[Dict]:
        """按分数排序、移除内部字段并截取。"""
        sorted_results = sorted(results_map.values(), key=lambda r: r["_score"], reverse=True)
        for r in sorted_results:
            del r["_score"]
        return sorted_results[:limit]

    def search(
        self,
        query: str,
        limit: int = 20,
        *,
        allowed_page_paths: Iterable[str] | None = None,
        allow_semantic: bool = True,
    ) -> List[Dict]:
        """
        知识图谱关键词召回（语义搜索增强）。

        在关系数据库和 Wiki 页面中搜索与查询相关的页面。
        若 embedding 开启，优先尝试语义召回，再补充关键词召回。

        ``allowed_page_paths`` must be an allowlist built by the unique
        authorization seam before this method is called.  A missing allowlist
        fails closed so callers cannot accidentally turn graph recall into a
        body-read bypass.

        Returns:
            [{"page_path": str, "title": str, "content": str, "entity_name": str}, ...]
        """
        allowed_paths = self._normalize_allowed_page_paths(allowed_page_paths)
        if allowed_paths is None:
            return []
        results_map: Dict[str, Dict] = {}

        if allow_semantic:
            self._search_semantic(query, limit, results_map, allowed_paths)

        keywords = self._normalize_search_query(query)
        self._search_relations(keywords, results_map, allowed_paths)
        self._search_wiki_files(keywords, results_map, allowed_paths)

        return self._finalize_search_results(results_map, limit)

    def _normalize_allowed_page_paths(
        self,
        allowed_page_paths: Iterable[str] | None,
    ) -> set[str] | None:
        """Normalize caller-provided allowlist without consulting page bodies."""
        if allowed_page_paths is None:
            return None
        normalized: set[str] = set()
        wiki_root = self.wiki_base.resolve(strict=False)
        for raw_path in allowed_page_paths:
            candidate = Path(str(raw_path or ""))
            if not str(candidate):
                continue
            try:
                resolved = (
                    candidate.resolve(strict=False)
                    if candidate.is_absolute()
                    else (wiki_root / candidate).resolve(strict=False)
                )
                relative_path = resolved.relative_to(wiki_root).as_posix()
            except (OSError, ValueError):
                continue
            if Path(relative_path).suffix.lower() != ".md":
                continue
            normalized.add(relative_path)
        return normalized

    def _to_rel_path(self, page_path: str) -> str:
        """将绝对路径转换为相对于 wiki_base 的路径。"""
        try:
            p = Path(page_path)
            if (p.is_absolute() and self.wiki_base in p.parents) or p == self.wiki_base:
                return str(p.relative_to(self.wiki_base))
        except ValueError:
            logger.warning("[knowledge_graph] ValueError suppressed", exc_info=True)
        return page_path

    # ========== 辅助方法 ==========

    @staticmethod
    def _extract_frontmatter(content: str) -> Dict:
        """提取 frontmatter"""
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    import yaml

                    return yaml.safe_load(parts[1]) or {}
                except ImportError:
                    logging.getLogger(__name__).warning("Caught unexpected error", exc_info=True)
        return {}

    @staticmethod
    def _extract_title(content: str) -> str:
        """提取标题"""
        match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        return match.group(1).strip() if match else ""

    @staticmethod
    def _extract_wiki_links(content: str) -> List[str]:
        """提取 [[链接]]"""
        links = re.findall(r"\[\[([^\]]+)\]\]", content)
        # 处理别名：[[目标|显示名]]
        return [link.split("|")[0].strip() for link in links]

    # 通用关键词黑名单（会导致过度连接的宽泛标签）
    _GENERIC_KEYWORDS: frozenset = frozenset(
        {
            "technology",
            "技术",
            "tech",
            "concept",
            "概念",
            "未分类",
            "wiki",
            "obsidian",
            "methodology",
            "方法论",
        }
    )

    @staticmethod
    def _extract_all_keywords(frontmatter: Dict) -> List[str]:
        """提取所有关键词，自动过滤通用宽泛标签"""
        keywords = []

        # 分层关键词
        kw_dict = frontmatter.get("关键词", {})
        if isinstance(kw_dict, dict):
            for layer_words in kw_dict.values():
                if isinstance(layer_words, list):
                    keywords.extend(layer_words)

        # 其他可能的关键词字段
        for field in ["领域", "类型", "版本标记"]:
            val = frontmatter.get(field)
            if val and isinstance(val, str):
                keywords.append(val)

        # 触发场景
        scenes = frontmatter.get("触发场景", [])
        if isinstance(scenes, list):
            keywords.extend(scenes)

        # 过滤通用词 + 去重
        filtered = []
        seen = set()
        for k in keywords:
            if not isinstance(k, str):
                continue
            kl = k.lower().strip()
            if kl and kl not in seen and kl not in KnowledgeGraphSearchMixin._GENERIC_KEYWORDS:
                seen.add(kl)
                filtered.append(kl)
        return filtered

    @staticmethod
    def _extract_anti_patterns_from_body(body_text: str) -> List[str]:
        """从正文提取反模式/警示句式（不依赖 frontmatter）"""
        # 先清洗：移除 markdown 链接和 HTML 注释
        clean = re.sub(r"\[\[[^\]]+\]\]", "", body_text)
        clean = re.sub(r"<!--.*?-->", "", clean, flags=re.DOTALL)
        patterns = []
        # 匹配常见反模式句式（允许换行或中文标点结尾）
        anti_regex = re.compile(
            r"(?:不应该|避免|禁止|不要|切勿|千万别|错误的|反模式|误区|陷阱|坑点)"
            r"[^。\n]{3,80}[。\n]",
            re.UNICODE,
        )
        for m in anti_regex.finditer(clean):
            sentence = m.group(0).strip()
            if len(sentence) < 10 or len(sentence) > 120:
                continue
            # 严格过滤：排除含路径/残留标记/纯列表项的内容
            if "/" in sentence or "]]" in sentence or "-->" in sentence or "###" in sentence:
                continue
            # 排除包含文件名片段（如 反模式_1）的内容
            if re.search(r"[a-f0-9]{8}_", sentence) or re.search(r"_[0-9]+", sentence):
                continue
            # 排除编号列表项（含 1) 2. 等）或 markdown 标题
            if re.search(r"\d+[\.\)]", sentence) or sentence.startswith("#"):
                continue
            # 排除分号分隔的列表（通常含多个分号或过多逗号）
            if sentence.count("；") >= 1 or sentence.count("，") >= 3:
                continue
            patterns.append(sentence)
        return patterns[:5]  # 每页最多取 5 条

    @staticmethod
    def _extract_hash_prefix(stem: str) -> Optional[str]:
        """从文件名提取哈希前缀（如 74f412fa_方法论_3 → 74f412fa）"""
        # 匹配 8位十六进制 或 日期格式前缀
        m = re.match(r"^([0-9a-f]{8})_", stem)
        if m:
            return m.group(1)
        m = re.match(r"^(\d{2}-\d{2}-\d{2})_", stem)
        if m:
            return m.group(1)
        return None
