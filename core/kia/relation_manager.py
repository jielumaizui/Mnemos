# -*- coding: utf-8 -*-
"""
RelationManager — 关系管理器

职责：
- 从蒸馏输出提取关系
- 从暗知识共现提取关系
- 发现隐式关系（关键词重叠 + 暗知识共现）
- 贝叶斯置信度更新
- 新增关系类型：CO_OCCURS, SEQUENTIAL, SIMILAR_TO
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from core.config import get_config
from .relation_schema import Relation, RelationType, RelationEvidence, suggest_relation_type



def _get_db_path() -> Path:
    return Path.home() / ".mnemos" / "wiki_state.db"


@dataclass
class RelationSuggestion:
    """关系建议（待确认）"""
    source: str
    target: str
    relation_type: str
    confidence: float
    reason: str = ""
    evidence_type: str = "auto_discover"


class RelationManager:
    """关系管理器"""

    def __init__(self, db_path: str = None):
        self._db_path = Path(db_path) if db_path else _get_db_path()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

    def add_from_distill(self, kg_input: Dict) -> List[Relation]:
        """从蒸馏输出提取关系

        kg_input 格式：
        {
            "entities": ["entity1", "entity2"],
            "relations": [
                {"source": "A", "target": "B", "type": "builds_on", "confidence": 0.8}
            ]
        }
        """
        relations = []
        for rel_data in kg_input.get("relations", []):
            rel_type_str = rel_data.get("type", "references")
            try:
                rel_type = RelationType(rel_type_str)
            except ValueError:
                rel_type = RelationType.REFERENCES

            relation = Relation(
                source=rel_data["source"],
                target=rel_data["target"],
                relation_type=rel_type,
                strength=rel_data.get("strength", 0.5),
                confidence=rel_data.get("confidence", 0.5),
                source_method="distill",
                evidence=[RelationEvidence(
                    evidence_type="distill_extraction",
                    content=rel_data.get("reason", ""),
                )],
            )
            relations.append(relation)
        return relations

    def add_from_dark_knowledge(self, association: Dict) -> Relation:
        """从暗知识共现提取关系

        association 格式：
        {
            "entity_a": "asyncio",
            "entity_b": "uvloop",
            "co_occurrence_count": 5,
            "context": "性能优化讨论中同时出现"
        }
        """
        co_count = association.get("co_occurrence_count", 1)
        confidence = min(0.7, co_count * 0.15)  # 共现越多置信度越高，但上限0.7

        return Relation(
            source=association["entity_a"],
            target=association["entity_b"],
            relation_type=RelationType.SIMILAR_TO,
            strength=min(0.8, co_count * 0.1),
            confidence=confidence * 0.7,  # 暗知识置信度打折
            source_method="dark_knowledge",
            evidence=[RelationEvidence(
                evidence_type="co_occurrence",
                content=association.get("context", f"共现 {co_count} 次"),
            )],
        )

    def discover_implicit_relations(self, entity_name: str,
                                     wiki_dir: Path = None) -> List[RelationSuggestion]:
        """发现隐式关系

        策略：
        1. 关键词重叠 → SIMILAR_TO
        2. 共现关系 → CO_OCCURS
        3. 顺序关系 → SEQUENTIAL（A 出现在 B 之前）
        """
        suggestions = []

        if not wiki_dir:
            wiki_dir = get_config().wiki_dir

        # 搜索提及该实体的页面
        entity_pages = self._find_pages_mentioning(entity_name, wiki_dir)
        if not entity_pages:
            return suggestions

        # 与其他实体的共现分析
        co_occurring = self._analyze_co_occurrence(entity_name, entity_pages)
        for other_entity, count in co_occurring.items():
            if other_entity == entity_name:
                continue
            if count >= 2:
                suggestions.append(RelationSuggestion(
                    source=entity_name,
                    target=other_entity,
                    relation_type="co_occurs",
                    confidence=min(0.6, count * 0.15),
                    reason=f"在 {count} 个页面中同时出现",
                    evidence_type="co_occurrence",
                ))

        # 关键词重叠分析
        entity_kw = self._extract_entity_keywords(entity_name, entity_pages)
        all_entities = self._get_all_entity_names()
        for other in all_entities:
            if other == entity_name:
                continue
            other_kw = self._extract_entity_keywords(other, self._find_pages_mentioning(other, wiki_dir))
            if entity_kw and other_kw:
                jaccard = len(entity_kw & other_kw) / len(entity_kw | other_kw) if entity_kw | other_kw else 0
                if jaccard >= 0.3:
                    suggestions.append(RelationSuggestion(
                        source=entity_name,
                        target=other,
                        relation_type="similar_to",
                        confidence=jaccard,
                        reason=f"关键词重叠度 {jaccard:.0%}",
                        evidence_type="keyword_overlap",
                    ))

        return suggestions

    def update_confidence(self, source: str, target: str,
                          relation_type: str, feedback: float) -> None:
        """贝叶斯置信度更新

        feedback: 0-1，1 表示关系确认，0 表示关系否定
        """
        try:
            with sqlite3.connect(str(self._db_path), timeout=5) as conn:
                cursor = conn.execute(
                    "SELECT confidence FROM relations "
                    "WHERE source=? AND target=? AND relation_type=?",
                    (source, target, relation_type),
                )
                row = cursor.fetchone()
                if not row:
                    return

                old_conf = row[0]
                # EWMA 更新
                alpha = 0.2
                new_conf = alpha * feedback + (1 - alpha) * old_conf

                conn.execute(
                    "UPDATE relations SET confidence=?, updated_at=? "
                    "WHERE source=? AND target=? AND relation_type=?",
                    (new_conf, datetime.now().isoformat()[:19],
                     source, target, relation_type),
                )

                # 置信度低于 0.2 的关系标记为 suspect
                if new_conf < 0.2:
                    conn.execute(
                        "UPDATE relations SET source_method='suspect' "
                        "WHERE source=? AND target=? AND relation_type=?",
                        (source, target, relation_type),
                    )
                conn.commit()
        except Exception as e:
            logger.warning(f"更新关系置信度失败: {e}")

    # ---- 内部方法 ----

    def _find_pages_mentioning(self, entity: str, wiki_dir: Path) -> List[Path]:
        """搜索提及某实体的 Wiki 页面"""
        pages = []
        entity_lower = entity.lower()
        for subdir in ["00-Inbox", "03-Tech", "04-Concepts"]:
            md_dir = wiki_dir / subdir
            if not md_dir.exists():
                continue
            for md_file in md_dir.glob("*.md"):
                try:
                    content = md_file.read_text(encoding="utf-8").lower()
                    if entity_lower in content:
                        pages.append(md_file)
                except Exception:
                    logging.getLogger(__name__).warning(f"Caught unexpected error at relation_manager.py", exc_info=True)
                    continue
        return pages

    def _analyze_co_occurrence(self, entity: str, pages: List[Path]) -> Dict[str, int]:
        """分析实体共现"""
        co_occurrence: Dict[str, int] = {}
        entity_lower = entity.lower()

        for page in pages:
            try:
                content = page.read_text(encoding="utf-8")
            except Exception:
                logging.getLogger(__name__).warning(f"Caught unexpected error at relation_manager.py", exc_info=True)
                continue

            # 提取 [[链接]] 中的实体
            links = set(re.findall(r'\[\[([^\]|]+)', content))
            for link in links:
                link = link.strip()
                if link.lower() != entity_lower:
                    co_occurrence[link] = co_occurrence.get(link, 0) + 1

        return co_occurrence

    def _extract_entity_keywords(self, entity: str, pages: List[Path]) -> Set[str]:
        """提取实体的关键词集合"""
        keywords = set()
        for page in pages[:5]:  # 最多5个页面
            try:
                content = page.read_text(encoding="utf-8")[:2000]
                fm = self._parse_frontmatter(content)
                kw = fm.get("关键词", {})
                if isinstance(kw, dict):
                    for layer_words in kw.values():
                        if isinstance(layer_words, list):
                            keywords.update(w.lower() for w in layer_words if isinstance(w, str))
            except Exception:
                logging.getLogger(__name__).warning(f"Caught unexpected error at relation_manager.py", exc_info=True)
                continue
        return keywords

    def _get_all_entity_names(self) -> List[str]:
        """获取所有实体名称"""
        try:
            from .entity_manager import EntityManager
            em = EntityManager()
            entities = em.get_all_entities()
            return [e.name for e in entities]
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at relation_manager.py", exc_info=True)
            return []

    @staticmethod
    def _parse_frontmatter(content: str) -> Dict:
        if not content.startswith("---"):
            return {}
        end = content.find("---", 3)
        if end == -1:
            return {}
        fm = {}
        for line in content[3:end].strip().split("\n"):
            if ":" in line:
                key, _, val = line.partition(":")
                key = key.strip()
                val = val.strip()
                if val.startswith("["):
                    try:
                        val = json.loads(val)
                    except json.JSONDecodeError:
                        pass
                fm[key] = val
        return fm


import re
logger = logging.getLogger(__name__)
