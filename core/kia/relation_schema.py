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
    """关系类型枚举

    核心类型（蓝图要求 9 种）：CONTAINS, RELATED_TO, CONTRADICTS,
    SUPERCEDES, DERIVES_FROM, PREREQUISITE, CO_OCCURS, SEQUENTIAL, SIMILAR_TO

    扩展类型（向后兼容，共 17+ 种）：涵盖层级、因果、演化、对比、元关系等
    """

    # === 核心类型（蓝图标准 9 种）===
    CONTAINS = "contains"                # 包含（A 包含 B）
    RELATED_TO = "related_to"            # 相关（A 与 B 有关联）
    CONTRADICTS = "contradicts"          # 矛盾（A 和 B 结论冲突）
    SUPERCEDES = "supercedes"            # 取代（A 取代 B）
    DERIVES_FROM = "derives_from"        # 派生（A 由 B 派生）
    PREREQUISITE = "prerequisite"        # 前置（A 是 B 的前置条件）
    CO_OCCURS = "co_occurs"              # 共现（A 与 B 同时出现）
    SEQUENTIAL = "sequential"            # 顺序（A 在 B 之前发生）
    SIMILAR_TO = "similar_to"            # 相似（A 和 B 结构/模式相似）

    # === 扩展类型（向后兼容，保留现有语义）===
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
    ALTERNATIVE_TO = "alternative_to"    # 是...的替代方案（A 和 B 可互换）
    CONTRASTS_WITH = "contrasts_with"    # 与...对比（A 与 B 对比）

    # 动作/语义关系（ConnectWorker 使用）
    USES = "uses"                        # 使用...（A 使用 B）
    IMPLEMENTS = "implements"            # 实现...（A 实现 B）
    EXTENDS = "extends"                  # 扩展...（A 扩展 B）

    # 元关系
    REFERENCES = "references"            # 引用了...（A 提及/参考了 B）
    INSTANCE_OF = "instance_of"          # 是...的实例（A 是 B 的具体案例）


# 蓝图定义的核心关系类型（9 种）
CORE_RELATION_TYPES = frozenset({
    RelationType.CONTAINS,
    RelationType.RELATED_TO,
    RelationType.CONTRADICTS,
    RelationType.SUPERCEDES,
    RelationType.DERIVES_FROM,
    RelationType.PREREQUISITE,
    RelationType.CO_OCCURS,
    RelationType.SEQUENTIAL,
    RelationType.SIMILAR_TO,
})


# 关系元数据：反向关系、对称性、传递性、描述
RELATION_META: Dict[RelationType, Dict] = {
    # --- 核心类型 ---
    RelationType.CONTAINS: {
        "reverse": "is_contained_by",
        "symmetric": False,
        "transitive": True,
        "description": "A 包含 B（组成部分或子集）",
        "example": "「OAuth2 流程」contains「JWT 签名验证」",
    },
    RelationType.RELATED_TO: {
        "reverse": "related_to",
        "symmetric": True,
        "transitive": False,
        "description": "A 与 B 存在一般性关联",
        "example": "「DDD」related_to「微服务」",
    },
    RelationType.CONTRADICTS: {
        "reverse": "is_contradicted_by",
        "symmetric": True,
        "transitive": False,
        "description": "A 和 B 的结论/建议互相冲突",
        "example": "「微服务拆分要细」contradicts「服务不宜过多」",
    },
    RelationType.SUPERCEDES: {
        "reverse": "is_superceded_by",
        "symmetric": False,
        "transitive": False,
        "description": "A 取代 B，B 已过时",
        "example": "「pytest」supercedes「unittest」",
    },
    RelationType.DERIVES_FROM: {
        "reverse": "is_derived_by",
        "symmetric": False,
        "transitive": True,
        "description": "A 由 B 派生或扩展而来",
        "example": "「v2 复盘」derives_from「v1 复盘」",
    },
    RelationType.PREREQUISITE: {
        "reverse": "is_prerequisite_for",
        "symmetric": False,
        "transitive": True,
        "description": "学习/理解 A 之前需要先掌握 B",
        "example": "「HTTP 协议」prerequisite「RESTful API 设计」",
    },
    RelationType.CO_OCCURS: {
        "reverse": "co_occurs",
        "symmetric": True,
        "transitive": False,
        "description": "A 与 B 在上下文中同时出现",
        "example": "「Kubernetes」co_occurs「Docker」",
    },
    RelationType.SEQUENTIAL: {
        "reverse": "is_preceded_by",
        "symmetric": False,
        "transitive": True,
        "description": "A 在 B 之前发生或执行",
        "example": "「需求分析」sequential「系统设计」",
    },
    RelationType.SIMILAR_TO: {
        "reverse": "similar_to",
        "symmetric": True,
        "transitive": False,
        "description": "A 和 B 结构、模式或适用场景相似",
        "example": "「策略模式」similar_to「状态模式」",
    },
    # --- 扩展类型（向后兼容）---
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
    """知识关系（对齐蓝图 §3.2）"""
    source: str                       # 源实体
    target: str                       # 目标实体
    relation_type: RelationType
    strength: float = 0.5             # 综合强度（向后兼容字段）
    base_strength: float = 0.5        # 基础强度（提取时计算）
    dynamic_strength: float = 0.5     # 动态强度（基于使用频率）
    confidence: float = 0.5           # 初始可信度
    confidence_history: List[float] = None  # 可信度历史（贝叶斯更新记录）
    evidence: List[RelationEvidence] = None   # 支持证据列表
    source_method: str = "auto"       # auto / manual / dark_knowledge
    context: str = ""                 # 关联上下文（ADR-019：语义桥接文本）
    created_at: str = ""
    updated_at: str = ""
    last_validated: str = ""          # 上次验证时间
    status: str = "active"            # active / suspect / deprecated

    def __post_init__(self):
        if self.evidence is None:
            self.evidence = []
        if self.confidence_history is None:
            self.confidence_history = []
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()[:19]
        if not self.updated_at:
            self.updated_at = self.created_at
        # 如果 base/dynamic 被显式设置，重新计算 strength
        if self.base_strength != 0.5 or self.dynamic_strength != 0.5:
            self.strength = round(self.base_strength * self.dynamic_strength, 3)

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

    def update_confidence(self, new_score: float) -> None:
        """贝叶斯更新置信度，追加到历史"""
        self.confidence_history.append(round(self.confidence, 4))
        # 简单 EWMA 更新
        alpha = 0.3
        self.confidence = round(alpha * new_score + (1 - alpha) * self.confidence, 4)
        self.updated_at = datetime.now(timezone.utc).isoformat()[:19]

    def to_dict(self) -> Dict:
        """序列化为字典"""
        return {
            "source": self.source,
            "target": self.target,
            "relation_type": self.relation_type.value,
            "strength": round(self.strength, 2),
            "base_strength": round(self.base_strength, 2),
            "dynamic_strength": round(self.dynamic_strength, 2),
            "confidence": round(self.confidence, 2),
            "confidence_history": self.confidence_history,
            "source_method": self.source_method,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_validated": self.last_validated,
            "status": self.status,
            "evidence": [
                {"type": e.evidence_type, "content": e.content}
                for e in (self.evidence or [])
            ],
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "Relation":
        """从字典反序列化（向后兼容：旧数据中的 strength 映射为 base_strength）"""
        evidence = [
            RelationEvidence(
                evidence_type=e.get("type", e.get("evidence_type", "")),
                content=e.get("content", ""),
            )
            for e in data.get("evidence", [])
        ]
        # 向后兼容：旧数据可能只有 strength 字段
        base_strength = data.get("base_strength")
        dynamic_strength = data.get("dynamic_strength")
        old_strength = data.get("strength", 0.5)
        if base_strength is None:
            base_strength = old_strength
            dynamic_strength = dynamic_strength or 1.0
        return cls(
            source=data["source"],
            target=data["target"],
            relation_type=RelationType(data["relation_type"]),
            strength=old_strength,
            base_strength=base_strength,
            dynamic_strength=dynamic_strength or 0.5,
            confidence=data.get("confidence", 0.5),
            confidence_history=data.get("confidence_history", []),
            source_method=data.get("source_method", "auto"),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            last_validated=data.get("last_validated", ""),
            status=data.get("status", "active"),
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
        # --- 核心类型关键词 ---
        "包含": [(RelationType.CONTAINS, 0.9)],
        "涵盖": [(RelationType.CONTAINS, 0.8)],
        "相关": [(RelationType.RELATED_TO, 0.8)],
        "关联": [(RelationType.RELATED_TO, 0.8)],
        "矛盾": [(RelationType.CONTRADICTS, 0.9)],
        "冲突": [(RelationType.CONTRADICTS, 0.8)],
        "相反": [(RelationType.CONTRADICTS, 0.7)],
        "取代": [(RelationType.SUPERCEDES, 0.9)],
        "废弃": [(RelationType.SUPERCEDES, 0.9)],
        "过时": [(RelationType.SUPERCEDES, 0.8)],
        "派生": [(RelationType.DERIVES_FROM, 0.9)],
        "基于": [(RelationType.DERIVES_FROM, 0.8), (RelationType.BUILDS_ON, 0.7)],
        "来源": [(RelationType.DERIVES_FROM, 0.7)],
        "前置": [(RelationType.PREREQUISITE, 0.9)],
        "先决": [(RelationType.PREREQUISITE, 0.9)],
        "先学": [(RelationType.PREREQUISITE, 0.8)],
        "共现": [(RelationType.CO_OCCURS, 0.9)],
        "同时": [(RelationType.CO_OCCURS, 0.7)],
        "顺序": [(RelationType.SEQUENTIAL, 0.8)],
        "先后": [(RelationType.SEQUENTIAL, 0.7)],
        "类似": [(RelationType.SIMILAR_TO, 0.9)],
        "像": [(RelationType.SIMILAR_TO, 0.7)],
        "类比": [(RelationType.SIMILAR_TO, 0.8)],
        # --- 扩展类型关键词（向后兼容）---
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
        "导致": [(RelationType.CAUSES, 0.9)],
        "引发": [(RelationType.CAUSES, 0.8)],
        "原因": [(RelationType.CAUSES, 0.7)],
        "依赖": [(RelationType.DEPENDS_ON, 0.9)],
        "需要": [(RelationType.DEPENDS_ON, 0.7), (RelationType.PREREQUISITE_FOR, 0.6)],
        "前提": [(RelationType.PREREQUISITE_FOR, 0.9)],
        "解决": [(RelationType.SOLVES, 0.9)],
        "方案": [(RelationType.SOLVES, 0.7)],
        "修复": [(RelationType.SOLVES, 0.7)],
        "替代": [(RelationType.REPLACES, 0.9), (RelationType.ALTERNATIVE_TO, 0.7)],
        "取代老": [(RelationType.REPLACES, 0.9)],
        "升级": [(RelationType.REPLACES, 0.7), (RelationType.EVOLVED_FROM, 0.6)],
        "演化": [(RelationType.EVOLVED_FROM, 0.9)],
        "迭代": [(RelationType.EVOLVED_FROM, 0.8)],
        "或": [(RelationType.ALTERNATIVE_TO, 0.6)],
        "可选": [(RelationType.ALTERNATIVE_TO, 0.7)],
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
