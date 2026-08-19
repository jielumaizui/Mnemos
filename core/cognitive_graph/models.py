# -*- coding: utf-8 -*-
"""
Cognitive Graph 数据模型

跨层认知关系的基本数据结构，强调 URN 寻址和可追溯性。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()[:19]


@dataclass
class CognitiveRelation:
    """跨层认知关系"""

    id: str
    source: str
    target: str
    relation_type: str
    strength: float = 0.5
    confidence: float = 0.5
    source_layer: str = ""
    target_layer: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    stale: int = 0
    access_control: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "target": self.target,
            "relation_type": self.relation_type,
            "strength": self.strength,
            "confidence": self.confidence,
            "source_layer": self.source_layer,
            "target_layer": self.target_layer,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "stale": self.stale,
            "access_control": self.access_control,
        }


@dataclass
class CanonicalNode:
    """归一化节点：把不同层的同名/同义实体合并为一个 canonical 节点"""

    canonical_id: str
    canonical_name: str
    aliases: List[str] = field(default_factory=list)
    source_ids: List[str] = field(default_factory=list)
    embedding: Optional[bytes] = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    access_control: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "canonical_id": self.canonical_id,
            "canonical_name": self.canonical_name,
            "aliases": self.aliases,
            "source_ids": self.source_ids,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "access_control": self.access_control,
        }


@dataclass
class SyncOutboxItem:
    """同步 outbox 记录"""

    id: int
    event_type: str
    payload: Dict[str, Any]
    created_at: str
    processed_at: Optional[str] = None
    access_control: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "event_type": self.event_type,
            "payload": self.payload,
            "created_at": self.created_at,
            "processed_at": self.processed_at,
            "access_control": self.access_control,
        }
