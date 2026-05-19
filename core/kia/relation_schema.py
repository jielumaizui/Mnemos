# -*- coding: utf-8 -*-
from __future__ import annotations

"""
知识关系语义化 Schema

定义 Wiki 页面之间的丰富语义关系类型，支持有向/无向、带强度/置信度的边。

设计原则：
- 关系类型覆盖常见知识关联模式（因果、依赖、演化、矛盾等）
- 每个关系类型定义反向关系，自动维护双向一致性
- 支持关系证据追踪（为什么认为两个页面有关系）
- 与 Obsidian 兼容：可导出为 frontmatter / Mermaid / Dataview 查询
"""

from enum import Enum
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone


class RelationType(str, Enum):
    """关系类型枚举"""

    # 层级关系
    BUILDS_ON = "builds_on"              # 建立在...之上（A 深化/扩展了 B）
    SPECIALIZES = "specializes"          # 是...的特化（A 是 B 的具体场景）
    GENERALIZES = "generalizes"          # 是...的泛化（A 是 B 的抽象概括）
    PART_OF = "part_of"                  # 是...的一部分（A 属于 B 的组成）
    HAS_PART = "has_part"                # 包含...部分（A 包含 B）

    # 因果/依赖关系
    CAUSES = "causes"                    # 导致...（A 引发 B）
    DEPENDS_ON = "depends_on"            # 依赖于...（A 需要 B 作为前提）
    PREREQUISITE_FOR = "prerequisite_for"  # 是...的前置条件（A 是 B 的前置知识）
    SOLVES = "solves"                    # 解决了...（A 是 B 的解决方案）

    # 演化关系
    REPLACES = "replaces"                # 替代了...（新版 A 替代旧版 B）
    EVOLVED_FROM = "evolved_from"        # 从...演化而来（A 由 B 演化）
    SUPERCEDED_BY = "superceded_by"      # 被...取代（A 被 B 取代）

    # 对比关系
    CONTRADICTS = "contradicts"          # 与...矛盾（A 和 B 结论冲突）
    ALTERNATIVE_TO = "alternative_to"    # 是...的替代方案（A 和 B 可互换）
    SIMILAR_TO = "similar_to"            # 类似于...（A 和 B 结构/模式相似）

    # 元关系
    REFERENCES = "references"            # 引用了...（A 提及/参考了 B）
    INSTANCE_OF = "instance_of"          # 是...的实例（A 是 B 的具体案例）


# 关系元数据：反向关系、对称性、传递性、描述
RELATION_META: Dict[RelationType, Dict] = {
    RelationType.BUILDS_ON: {
        "reverse": "is_built_by",
        "symmetric": False,
        "transitive": True,
        "description": "A 在 B 的基础上深化、扩展或补充",
        "example": "「asyncio 最佳实践」builds_on「Python 并发基础」",
    },
    RelationType.SPECIALIZES: {
        "reverse": "is_generalized_by",
        "symmetric": False,
        "transitive": True,
        "description": "A 是 B 在特定场景/条件下的具体化",
        "example": "「高并发下的缓存策略」specializes「缓存设计原则」",
    },
    RelationType.GENERALIZES: {
        "reverse": "is_specialized_by",
        "symmetric": False,
        "transitive": True,
        "description": "A 是 B 的抽象概括，适用范围更广",
        "example": "「CAP 定理」generalizes「分布式数据库选型」",
    },
    RelationType.PART_OF: {
        "reverse": "has_part",
        "symmetric": False,
        "transitive": True,
        "description": "A 是 B 的组成部分",
        "example": "「JWT 签名验证」part_of「OAuth2 流程」",
    },
    RelationType.HAS_PART: {
        "reverse": "part_of",
        "symmetric": False,
        "transitive": True,
        "description": "A 包含 B 作为组成部分",
        "example": "「OAuth2 流程」has_part「JWT 签名验证」",
    },
    RelationType.CAUSES: {
        "reverse": "caused_by",
        "symmetric": False,
        "transitive": False,
        "description": "A 直接导致 B 发生",
        "example": "「连接池耗尽」causes「API 超时」",
    },
    RelationType.DEPENDS_ON: {
        "reverse": "is_dependency_of",
        "symmetric": False,
        "transitive": True,
        "description": "A 的正常工作依赖 B",
        "example": "「微服务网关」depends_on「服务发现」",
    },
    RelationType.PREREQUISITE_FOR: {
        "reverse": "requires",
        "symmetric": False,
        "transitive": True,
        "description": "学习/理解 A 之前需要先掌握 B",
        "example": "「HTTP 协议」prerequisite_for「RESTful API 设计」",
    },
    RelationType.SOLVES: {
        "reverse": "is_solved_by",
        "symmetric": False,
        "transitive": False,
        "description": "A 是 B 问题的解决方案",
        "example": "「熔断器模式」solves「级联故障」",
    },
    RelationType.REPLACES: {
        "reverse": "is_replaced_by",
        "symmetric": False,
        "transitive": False,
        "description": "A 是 B 的替代品，推荐用 A 取代 B",
        "example": "「pytest」replaces「unittest」",
    },
    RelationType.EVOLVED_FROM: {
        "reverse": "evolved_into",
        "symmetric": False,
        "transitive": True,
        "description": "A 由 B 演化/迭代而来",
        "example": "「v2 复盘」evolved_from「v1 复盘」",
    },
    RelationType.SUPERCEDED_BY: {
        "reverse": "supercedes",
        "symmetric": False,
        "transitive": False,
        "description": "A 已被 B 取代，B 是更优方案",
        "example": "「Python 2」superceded_by「Python 3」",
    },
    RelationType.CONTRADICTS: {
        "reverse": "is_contradicted_by",
        "symmetric": True,
        "transitive": False,
        "description": "A 和 B 的结论/建议互相冲突",
        "example": "「微服务拆分要细」contradicts「服务不宜过多」",
    },
    RelationType.ALTERNATIVE_TO: {
        "reverse": "alternative_to",
        "symmetric": True,
        "transitive": False,
        "description": "A 和 B 是可互换的替代方案",
        "example": "「Redis」alternative_to「Memcached」",
    },
    RelationType.SIMILAR_TO: {
        "reverse": "similar_to",
        "symmetric": True,
        "transitive": False,
        "description": "A 和 B 结构、模式或适用场景相似",
        "example": "「策略模式」similar_to「状态模式」",
    },
    RelationType.REFERENCES: {
        "reverse": "is_referenced_by",
        "symmetric": False,
        "transitive": False,
        "description": "A 明确提及或参考了 B",
        "example": "「DDD 实践」references「限界上下文」",
    },
    RelationType.INSTANCE_OF: {
        "reverse": "has_instance",
        "symmetric": False,
        "transitive": False,
        "description": "A 是 B 的一个具体实例/案例",
        "example": "「淘宝双十一」instance_of「秒杀架构」",
    },
}


@dataclass
class RelationEvidence:
    """关系证据"""
    evidence_type: str           # quote / similarity / keyword_overlap / llm_inference / user_annotation
    content: str                 # 证据内容（引用文本、相似度值、关键词列表等）
    created_at: str = ""


@dataclass
class Relation:
    """知识关系"""
    source: str                  # 源页面 ID（文件路径或页面标识）
    target: str                  # 目标页面 ID
    relation_type: RelationType
    strength: float = 0.5        # 关系强度 0.0-1.0
    confidence: float = 0.5      # 置信度 0.0-1.0（关系是否真实存在的确定程度）
    source_method: str = "auto"  # auto / llm / similarity / keyword / manual
    created_at: str = ""
    updated_at: str = ""
    evidence: List[RelationEvidence] = None

    def __post_init__(self):
        if self.evidence is None:
            self.evidence = []
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()[:19]
        if not self.updated_at:
            self.updated_at = self.created_at

    @property
    def is_symmetric(self) -> bool:
        """是否对称关系"""
        return RELATION_META.get(self.relation_type, {}).get("symmetric", False)

    @property
    def reverse_type(self) -> str:
        """反向关系类型名称"""
        return RELATION_META.get(self.relation_type, {}).get("reverse", "related_to")

    @property
    def is_transitive(self) -> bool:
        """是否传递关系"""
        return RELATION_META.get(self.relation_type, {}).get("transitive", False)

    def to_dict(self) -> Dict:
        """序列化为字典"""
        return {
            "source": self.source,
            "target": self.target,
            "relation_type": self.relation_type.value,
            "strength": round(self.strength, 2),
            "confidence": round(self.confidence, 2),
            "source_method": self.source_method,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "evidence": [
                {"type": e.evidence_type, "content": e.content}
                for e in (self.evidence or [])
            ],
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "Relation":
        """从字典反序列化"""
        evidence = [
            RelationEvidence(
                evidence_type=e.get("type", e.get("evidence_type", "")),
                content=e.get("content", ""),
            )
            for e in data.get("evidence", [])
        ]
        return cls(
            source=data["source"],
            target=data["target"],
            relation_type=RelationType(data["relation_type"]),
            strength=data.get("strength", 0.5),
            confidence=data.get("confidence", 0.5),
            source_method=data.get("source_method", "auto"),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            evidence=evidence,
        )


# ========== 便捷函数 ==========

def get_relation_description(relation_type: RelationType) -> str:
    """获取关系类型描述"""
    return RELATION_META.get(relation_type, {}).get("description", "")


def get_relation_example(relation_type: RelationType) -> str:
    """获取关系类型示例"""
    return RELATION_META.get(relation_type, {}).get("example", "")


def infer_symmetric_type(relation_type: RelationType) -> bool:
    """推断关系是否对称"""
    return RELATION_META.get(relation_type, {}).get("symmetric", False)


def get_all_relation_types() -> List[Tuple[str, str, str]]:
    """
    获取所有关系类型的信息列表

    Returns:
        [(类型值, 描述, 示例), ...]
    """
    return [
        (
            rt.value,
            RELATION_META.get(rt, {}).get("description", ""),
            RELATION_META.get(rt, {}).get("example", ""),
        )
        for rt in RelationType
    ]


def suggest_relation_type(keywords: List[str]) -> List[Tuple[RelationType, float]]:
    """
    基于关键词猜测可能的关系类型

    返回 [(关系类型, 匹配分数), ...]，按分数降序
    """
    keyword_map = {
        "基于": [(RelationType.BUILDS_ON, 0.9)],
        "建立": [(RelationType.BUILDS_ON, 0.8)],
        "扩展": [(RelationType.BUILDS_ON, 0.8)],
        "深化": [(RelationType.BUILDS_ON, 0.7)],
        "特化": [(RelationType.SPECIALIZES, 0.9)],
        "具体": [(RelationType.SPECIALIZES, 0.7)],
        "场景": [(RelationType.SPECIALIZES, 0.6)],
        "抽象": [(RelationType.GENERALIZES, 0.8)],
        "概括": [(RelationType.GENERALIZES, 0.8)],
        "总结": [(RelationType.GENERALIZES, 0.6)],
        "部分": [(RelationType.PART_OF, 0.8)],
        "组成": [(RelationType.PART_OF, 0.7), (RelationType.HAS_PART, 0.7)],
        "包含": [(RelationType.HAS_PART, 0.8)],
        "导致": [(RelationType.CAUSES, 0.9)],
        "引发": [(RelationType.CAUSES, 0.8)],
        "原因": [(RelationType.CAUSES, 0.7)],
        "依赖": [(RelationType.DEPENDS_ON, 0.9)],
        "需要": [(RelationType.DEPENDS_ON, 0.7), (RelationType.PREREQUISITE_FOR, 0.6)],
        "前提": [(RelationType.PREREQUISITE_FOR, 0.9)],
        "先学": [(RelationType.PREREQUISITE_FOR, 0.8)],
        "解决": [(RelationType.SOLVES, 0.9)],
        "方案": [(RelationType.SOLVES, 0.7)],
        "修复": [(RelationType.SOLVES, 0.7)],
        "替代": [(RelationType.REPLACES, 0.9), (RelationType.ALTERNATIVE_TO, 0.7)],
        "取代": [(RelationType.REPLACES, 0.9), (RelationType.SUPERCEDED_BY, 0.8)],
        "升级": [(RelationType.REPLACES, 0.7), (RelationType.EVOLVED_FROM, 0.6)],
        "演化": [(RelationType.EVOLVED_FROM, 0.9)],
        "迭代": [(RelationType.EVOLVED_FROM, 0.8)],
        "过时": [(RelationType.SUPERCEDED_BY, 0.8)],
        "废弃": [(RelationType.SUPERCEDED_BY, 0.9)],
        "矛盾": [(RelationType.CONTRADICTS, 0.9)],
        "冲突": [(RelationType.CONTRADICTS, 0.8)],
        "相反": [(RelationType.CONTRADICTS, 0.7)],
        "或": [(RelationType.ALTERNATIVE_TO, 0.6)],
        "可选": [(RelationType.ALTERNATIVE_TO, 0.7)],
        "类似": [(RelationType.SIMILAR_TO, 0.9)],
        "像": [(RelationType.SIMILAR_TO, 0.7)],
        "类比": [(RelationType.SIMILAR_TO, 0.8)],
        "参考": [(RelationType.REFERENCES, 0.8)],
        "引用": [(RelationType.REFERENCES, 0.8)],
        "提及": [(RelationType.REFERENCES, 0.6)],
        "案例": [(RelationType.INSTANCE_OF, 0.9)],
        "实例": [(RelationType.INSTANCE_OF, 0.9)],
        "例子": [(RelationType.INSTANCE_OF, 0.7)],
    }

    scores: Dict[RelationType, float] = {}
    for kw in keywords:
        for matched_kw, candidates in keyword_map.items():
            if matched_kw in kw or kw in matched_kw:
                for rel_type, score in candidates:
                    scores[rel_type] = max(scores.get(rel_type, 0), score)

    return sorted(scores.items(), key=lambda x: x[1], reverse=True)
