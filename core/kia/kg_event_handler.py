# -*- coding: utf-8 -*-
"""
KGEventHandler — 知识图谱事件处理器

订阅 knowledge_distilled 事件，蒸馏完成后实时更新实体和关系。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from core.config import get_config

logger = logging.getLogger(__name__)


class KGEventHandler:
    """知识图谱事件处理器

    事件驱动：订阅蒸馏完成事件，自动更新知识图谱。
    """

    def __init__(self):
        self._entity_manager = None
        self._relation_manager = None

    def _get_entity_manager(self):
        if self._entity_manager is None:
            from .entity_manager import EntityManager
            self._entity_manager = EntityManager()
        return self._entity_manager

    def _get_relation_manager(self):
        if self._relation_manager is None:
            from .relation_manager import RelationManager
            self._relation_manager = RelationManager()
        return self._relation_manager

    def on_distilled(self, event: Dict) -> Dict:
        """蒸馏完成事件处理

        Args:
            event: {
                "type": "knowledge_distilled",
                "session_id": "...",
                "fragments": [...],
                "wiki_pages": ["path1", "path2"],
                "meta": {...}
            }

        Returns:
            处理结果 {entities_created, relations_created, ...}
        """
        result = {
            "entities_created": 0,
            "entities_updated": 0,
            "relations_discovered": 0,
            "relations_added": 0,
        }

        wiki_pages = event.get("wiki_pages", [])
        if not wiki_pages:
            return result

        em = self._get_entity_manager()
        rm = self._get_relation_manager()

        # 1. 从每个 Wiki 页面提取实体
        all_entities = []
        for page_path in wiki_pages:
            page = Path(page_path)
            if not page.exists():
                continue
            entities = em.ingest_from_wiki(page)
            all_entities.extend(entities)

        result["entities_created"] = sum(
            1 for e in all_entities if e.source_count == 1
        )
        result["entities_updated"] = len(all_entities) - result["entities_created"]

        # 2. 从蒸馏输出提取关系
        kg_input = event.get("kg_input", {})
        if kg_input:
            relations = rm.add_from_distill(kg_input)
            result["relations_discovered"] = len(relations)

            # 将关系写入知识图谱
            from .knowledge_graph import KnowledgeGraph
            kg = KnowledgeGraph()
            added = kg.apply_discovered(relations, min_confidence=0.7)
            result["relations_added"] = added

        # 3. 自动发现 Wiki 页面之间的关系
        from .knowledge_graph import KnowledgeGraph
        kg = KnowledgeGraph()
        for page_path in wiki_pages:
            page = Path(page_path)
            if not page.exists():
                continue
            discovered = kg.discover_relations(page)
            added = kg.apply_discovered(discovered, min_confidence=0.7)
            result["relations_added"] += added

        logger.info(
            f"[KGEventHandler] 蒸馏事件处理完成: "
            f"entities={result['entities_created']}/{result['entities_updated']}, "
            f"relations={result['relations_added']}"
        )

        return result

    def on_page_updated(self, event: Dict) -> Dict:
        """页面更新事件处理

        Args:
            event: {
                "type": "wiki_page_updated",
                "page_path": "...",
                "update_type": "append|replace|merge",
            }
        """
        page_path = event.get("page_path", "")
        if not page_path:
            return {"status": "skipped"}

        page = Path(page_path)
        if not page.exists():
            return {"status": "page_not_found"}

        # 重新提取实体（更新质量分）
        em = self._get_entity_manager()
        entities = em.ingest_from_wiki(page)

        # 重新发现关系
        from .knowledge_graph import KnowledgeGraph
        kg = KnowledgeGraph()
        discovered = kg.discover_relations(page)
        added = kg.apply_discovered(discovered, min_confidence=0.4)

        return {
            "status": "ok",
            "entities_updated": len(entities),
            "relations_added": added,
        }

    def on_entity_accessed(self, entity_name: str) -> None:
        """实体被访问事件（用于时间衰减计算）"""
        em = self._get_entity_manager()
        entity = em.resolve_alias(entity_name)
        if entity:
            # 小幅增加置信度
            em.update_quality(entity.uid, 0.7, 0.75)
