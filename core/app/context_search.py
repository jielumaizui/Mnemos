# -*- coding: utf-8 -*-
"""
ContextAwareSearch — 上下文感知搜索

知识图谱召回 + 画像加权评分。
4 维加权：confidence×0.4 + relevance×0.3 + continuity×0.2 + freshness×0.1
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import log1p
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """搜索结果"""
    page_path: str
    title: str
    snippet: str
    score: float
    relevance: float = 0.0
    confidence: float = 0.0
    continuity: float = 0.0
    freshness: float = 0.0


class ContextAwareSearch:
    """上下文感知搜索"""

    MAX_RESULTS = 10
    FRESHNESS_HALF_LIFE_DAYS = 30

    def __init__(self, wiki_base: Optional[str] = None):
        if wiki_base:
            self.wiki_base = Path(wiki_base).expanduser()
        else:
            from core.config import get_config
            self.wiki_base = get_config().wiki_dir

    def search(self, query: str, context: Optional[Dict] = None,
               limit: int = None) -> List[SearchResult]:
        """
        上下文感知搜索。

        Args:
            query: 搜索查询
            context: 上下文（working_dir, active_entities, recent_pages 等）
            limit: 最大结果数

        Returns:
            排序后的搜索结果列表
        """
        context = context or {}
        limit = limit or self.MAX_RESULTS

        # 1. 知识图谱召回
        candidates = self._recall_from_kg(query)
        if not candidates:
            # 回退到文件系统搜索
            candidates = self._recall_from_files(query)

        if not candidates:
            return []

        # 2. 画像加权评分
        profile = self._get_profile_weights()
        results = []
        for candidate in candidates:
            relevance = self._compute_relevance(query, candidate)
            confidence = self._compute_confidence(candidate)
            continuity = self._compute_continuity(candidate, context)
            freshness = self._compute_freshness(candidate)

            # 加权总分
            score = (
                confidence * 0.4 * profile.get("confidence_boost", 1.0)
                + relevance * 0.3 * profile.get("domain_boost", 1.0)
                + continuity * 0.2
                + freshness * 0.1 * profile.get("temporal_boost", 1.0)
            )
            score = min(score, 1.0)

            results.append(SearchResult(
                page_path=candidate.get("path", ""),
                title=candidate.get("title", ""),
                snippet=self._extract_snippet(candidate, query),
                score=score,
                relevance=relevance,
                confidence=confidence,
                continuity=continuity,
                freshness=freshness,
            ))

        # 3. 排序并截取
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:limit]

    def _recall_from_kg(self, query: str) -> List[Dict]:
        """从知识图谱召回候选页面"""
        try:
            from core.kia.knowledge_graph import KnowledgeGraph
            kg = KnowledgeGraph(wiki_base=str(self.wiki_base))
            results = kg.search(query, limit=20)
            return [
                {"path": r.get("page_path", ""), "title": r.get("title", ""),
                 "content": r.get("content", ""), "entity": r.get("entity_name", "")}
                for r in results
            ]
        except Exception as e:
            logger.debug(f"KG 召回失败: {e}")
            return []

    def _recall_from_files(self, query: str) -> List[Dict]:
        """从文件系统召回（回退方案）"""
        candidates = []
        keywords = query.lower().split()

        for md_file in self.wiki_base.rglob("*.md"):
            try:
                content = md_file.read_text(encoding="utf-8", errors="ignore")
                content_lower = content.lower()
                if any(kw in content_lower for kw in keywords):
                    title = md_file.stem
                    rel_path = str(md_file.relative_to(self.wiki_base))
                    candidates.append({
                        "path": rel_path,
                        "title": title,
                        "content": content[:2000],
                    })
                    if len(candidates) >= 20:
                        break
            except Exception:
                continue

        return candidates

    def _compute_relevance(self, query: str, candidate: Dict) -> float:
        """计算查询与候选内容的相关性"""
        keywords = query.lower().split()
        content = candidate.get("content", "").lower()
        title = candidate.get("title", "").lower()

        title_matches = sum(1 for kw in keywords if kw in title)
        content_matches = sum(1 for kw in keywords if kw in content)

        if not keywords:
            return 0.0

        # 标题匹配权重更高
        raw = (title_matches * 2 + content_matches) / (len(keywords) * 3)
        return min(raw, 1.0)

    def _compute_confidence(self, candidate: Dict) -> float:
        """计算候选页面的置信度"""
        entity = candidate.get("entity", "")
        if not entity:
            return 0.5

        try:
            from core.kia.entity_manager import EntityManager
            em = EntityManager()
            e = em.get_entity(entity)
            if e:
                return e.confidence
        except Exception:
            pass

        return 0.5

    def _compute_continuity(self, candidate: Dict, context: Dict) -> float:
        """计算浏览连续性 — 与当前上下文的关联程度"""
        if not context:
            return 0.3

        score = 0.0
        candidate_path = candidate.get("path", "")

        # 检查是否与最近访问的页面有链接关系
        recent_pages = context.get("recent_pages", [])
        for rp in recent_pages:
            if candidate_path in str(rp) or str(rp) in candidate_path:
                score += 0.3
                break

        # 检查是否与活跃实体匹配
        active_entities = context.get("active_entities", [])
        content = candidate.get("content", "").lower()
        for entity in active_entities:
            if entity.lower() in content:
                score += 0.2
                break

        # 工作目录相关
        working_dir = context.get("working_dir", "")
        if working_dir and working_dir.lower() in content:
            score += 0.2

        return min(score, 1.0)

    def _compute_freshness(self, candidate: Dict) -> float:
        """计算内容新鲜度 — 基于半衰期衰减"""
        path = candidate.get("path", "")
        if not path:
            return 0.5

        try:
            md_file = self.wiki_base / path
            if md_file.exists():
                mtime = datetime.fromtimestamp(md_file.stat().st_mtime)
                age_days = (datetime.now() - mtime).days
                # 半衰期衰减
                freshness = 0.5 ** (age_days / self.FRESHNESS_HALF_LIFE_DAYS)
                return freshness
        except Exception:
            pass

        return 0.5

    def _get_profile_weights(self) -> Dict:
        """获取画像加权系数"""
        try:
            from core.persona.daimon import SignalCollector
            from core.persona.psyche import get_signal_store
            store = get_signal_store()
            stats = store.get_signal_stats(days=30)
            total = sum(v for v in stats.values() if v > 0)

            if total < 10:
                return {}  # 信号不足，不偏权

            # 简单画像加权（信号充足时激活）
            return {
                "domain_boost": 1.0,
                "confidence_boost": 1.0,
                "temporal_boost": 1.15,  # 时间模式加权
            }
        except Exception:
            return {}

    def _extract_snippet(self, candidate: Dict, query: str) -> str:
        """提取搜索片段"""
        content = candidate.get("content", "")
        keywords = query.lower().split()
        if not content:
            return ""

        # 找到第一个包含关键词的段落
        for paragraph in content.split("\n\n"):
            if any(kw in paragraph.lower() for kw in keywords):
                snippet = paragraph.strip()[:200]
                if len(paragraph) > 200:
                    snippet += "..."
                return snippet

        # 回退：取前 200 字符
        return content[:200].strip() + ("..." if len(content) > 200 else "")
