"""
ConnectWorker — 连接 Worker

【E14 全库修复】E13 连接 Worker 完整实现。
负责从内容中提取实体、构建关系，并写入知识图谱。
"""

from typing import List, Dict, Optional, Set, Tuple
from pathlib import Path
import re

from core.kia.ingest_helpers import (
    extract_entities_fallback,
    extract_concepts_fallback,
    extract_entity_description,
)
from core.kia.knowledge_graph import KnowledgeGraph
from core.kia.relation_manager import RelationManager, Relation, RelationType, RelationEvidence


class ConnectWorker:
    """知识连接 Worker：实体提取 + 关系构建 + 知识图谱写入"""

    # 关系类型映射：根据文本模式推断关系类型
    _RELATION_PATTERNS = [
        (re.compile(r'(?:使用|基于|通过|采用|调用|引入|集成|安装|用到)\s+'), RelationType.USES),
        (re.compile(r'(?:实现|完成|达成|做到|解决)\s+'), RelationType.IMPLEMENTS),
        (re.compile(r'(?:扩展|继承|派生|衍生|发展)\s+'), RelationType.EXTENDS),
        (re.compile(r'(?:依赖|依靠|需要|要求| prerequisite)\s+'), RelationType.DEPENDS_ON),
        (re.compile(r'(?:对比|比较|versus|vs)\s+'), RelationType.CONTRASTS_WITH),
        (re.compile(r'(?:相似|类似|same as|like)\s+'), RelationType.SIMILAR_TO),
        (re.compile(r'(?:构建|建立|创建|搭建|组成)\s+'), RelationType.BUILDS_ON),
    ]

    def __init__(self, knowledge_graph: KnowledgeGraph = None):
        self.kg = knowledge_graph or KnowledgeGraph()
        self.processed_count = 0
        self._relation_manager = RelationManager()

    def extract_and_connect(self, content: str, source_page: str = "") -> Dict:
        """
        从内容中提取实体并建立关系

        Args:
            content: 文本内容
            source_page: 来源页面标识

        Returns:
            {"entities": [...], "relations": [...], "concepts": [...], "source": str}
        """
        if not content or not isinstance(content, str):
            return {"entities": [], "relations": [], "concepts": [], "source": source_page}

        # 1. 实体提取
        entities = extract_entities_fallback(content)

        # 2. 概念提取
        concepts = extract_concepts_fallback(content)

        # 3. 构建共现关系（同一内容中共同出现的实体）
        relations = self._build_co_occurrence_relations(entities, content, source_page)

        # 4. 基于语义模式推断关系
        inferred_relations = self._infer_relations_from_patterns(entities, content, source_page)
        relations.extend(inferred_relations)

        # 5. 写入知识图谱
        self._write_to_knowledge_graph(entities, concepts, relations, source_page)

        self.processed_count += 1

        return {
            "entities": entities,
            "concepts": concepts,
            "relations": [self._relation_to_dict(r) for r in relations],
            "source": source_page,
        }

    def batch_connect(self, contents: List[Tuple[str, str]]) -> List[Dict]:
        """批量处理多个内容"""
        return [self.extract_and_connect(c, s) for c, s in contents]

    def _build_co_occurrence_relations(self, entities: List[str], content: str,
                                       source_page: str) -> List[Relation]:
        """构建实体间的共现关系"""
        relations = []
        if len(entities) < 2:
            return relations

        # 统计共现：两个实体在同一句子中出现
        sentences = re.split(r'[。！？\n;；]', content)
        co_occurrence_counts: Dict[Tuple[str, str], int] = {}

        for sent in sentences:
            sent_entities = [e for e in entities if e in sent]
            for i, e1 in enumerate(sent_entities):
                for e2 in sent_entities[i + 1:]:
                    pair = tuple(sorted([e1, e2]))
                    co_occurrence_counts[pair] = co_occurrence_counts.get(pair, 0) + 1

        # 生成关系（共现次数越多，strength 越高）
        for (e1, e2), count in co_occurrence_counts.items():
            confidence = min(0.7, count * 0.15)
            strength = min(0.8, count * 0.1)
            relations.append(Relation(
                source=e1,
                target=e2,
                relation_type=RelationType.SIMILAR_TO,
                strength=strength,
                confidence=confidence,
                source_method="co_occurrence",
                evidence=[RelationEvidence(
                    evidence_type="co_occurrence",
                    content=f"在 '{source_page}' 中共现 {count} 次",
                )],
            ))

        return relations

    def _infer_relations_from_patterns(self, entities: List[str], content: str,
                                       source_page: str) -> List[Relation]:
        """基于语义模式推断关系类型"""
        relations = []
        sentences = re.split(r'[。！？\n;；]', content)

        for sent in sentences:
            sent_lower = sent.lower()
            for pattern, rel_type in self._RELATION_PATTERNS:
                if pattern.search(sent):
                    # 找到句子中的实体
                    sent_entities = [e for e in entities if e.lower() in sent_lower]
                    if len(sent_entities) >= 2:
                        # 取前两个实体作为 source 和 target
                        relations.append(Relation(
                            source=sent_entities[0],
                            target=sent_entities[1],
                            relation_type=rel_type,
                            strength=0.5,
                            confidence=0.4,
                            source_method="pattern_inference",
                            evidence=[RelationEvidence(
                                evidence_type="pattern_match",
                                content=sent[:200],
                            )],
                        ))

        return relations

    def _write_to_knowledge_graph(self, entities: List[str], concepts: List[str],
                                  relations: List[Relation], source_page: str):
        """将提取的内容写入知识图谱"""
        try:
            # 写入关系
            for relation in relations:
                self.kg.add_relation(relation)

            # 通过 relation_manager 批量处理
            kg_input = {
                "entities": entities + concepts,
                "relations": [self._relation_to_dict(r) for r in relations],
            }
            self._relation_manager.add_from_distill(kg_input)

        except Exception:
            # 写入失败不影响提取结果
            pass

    @staticmethod
    def _relation_to_dict(relation: Relation) -> Dict:
        """将 Relation 对象转为字典"""
        return {
            "source": relation.source,
            "target": relation.target,
            "type": relation.relation_type.value,
            "strength": relation.strength,
            "confidence": relation.confidence,
            "reason": relation.evidence[0].content if relation.evidence else "",
        }
