"""
Evidence Graph — 洞察血缘图谱

目标：
- 每条 Insight 都能被追溯到：Observation → Knowledge → Memory
- 用户问"为什么这么认为"时，系统能给出可点击的证据链
- Evidence Graph 是信任基础设施，不是新功能

数据模型：
- EvidenceNode: Memory / Knowledge / Observation / Mirror / Insight / Reflection /
  CognitiveShift / UserFeedback
- EvidenceEdge: DERIVED_FROM / SUPPORTS / CONTRADICTS / USED_IN / OBSERVED_IN /
  GENERATED_FROM / FEEDBACK_ON

存储：
- 独立 SQLite: ~/.mnemos/evidence_graph.db
- 复用 KG 的存储思路，但独立库避免污染知识图谱
"""

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from core.privacy.content_redaction import redact_persistence_value

logger = logging.getLogger(__name__)


class EvidenceNodeType(str, Enum):
    """证据节点类型"""

    MEMORY = "memory"  # L1 原始记忆
    KNOWLEDGE = "knowledge"  # L2 蒸馏知识（Wiki 页面）
    OBSERVATION = "observation"  # L3 系统观察
    MIRROR = "mirror"  # Mirror 证据链
    INSIGHT = "insight"  # 生成的洞察
    REFLECTION = "reflection"  # 一次完整 Reflection 记录
    COGNITIVE_SHIFT = "cognitive_shift"  # 认知变迁
    USER_FEEDBACK = "user_feedback"  # 用户反馈
    RAW_REVISION_SPAN = "raw_revision_span"
    EPISODE = "episode"
    CLAIM = "claim"
    BELIEF = "belief"
    DECISION = "decision"
    PREDICTION = "prediction"
    ACTION = "action"
    OUTCOME = "outcome"


class EvidenceRelationType(str, Enum):
    """证据边类型"""

    DERIVED_FROM = "derived_from"  # A 派生自 B
    SUPPORTS = "supports"  # A 支持 B
    CONTRADICTS = "contradicts"  # A 反驳 B
    USED_IN = "used_in"  # A 被用于生成 B
    OBSERVED_IN = "observed_in"  # A 在 B 中被观察到
    GENERATED_FROM = "generated_from"  # A 由 B 生成
    FEEDBACK_ON = "feedback_on"  # A 是对 B 的反馈
    CONTAINS = "contains"
    BASED_ON = "based_on"
    PREDICTED_FROM = "predicted_from"
    IMPLEMENTS = "implements"
    MEASURES = "measures"


@dataclass
class EvidenceNode:
    """证据节点"""

    id: str
    node_type: EvidenceNodeType
    title: str = ""
    source_path: str = ""  # 原始文件路径或 source_id
    content: str = ""  # 摘要或描述
    metadata: Dict = field(default_factory=dict)
    access_control: Dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "node_type": self.node_type.value,
            "title": self.title,
            "source_path": self.source_path,
            "content": self.content,
            "metadata": self.metadata,
            "access_control": self.access_control,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class EvidenceEdge:
    """证据关系边"""

    source_id: str
    target_id: str
    relation_type: EvidenceRelationType
    confidence: float = 1.0
    evidence: List[str] = field(default_factory=list)
    access_control: Dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    id: Optional[int] = None

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "relation_type": self.relation_type.value,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "access_control": self.access_control,
            "created_at": self.created_at.isoformat(),
        }


class EvidenceGraph:
    """证据图谱 — Insight 血缘的持久化存储"""

    def __init__(self, db_path: Optional[str] = None):
        if db_path:
            self.db_path = Path(db_path)
        else:
            self.db_path = Path.home() / ".mnemos" / "evidence_graph.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        """初始化证据图谱表结构"""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS evidence_nodes (
                    id TEXT PRIMARY KEY,
                    node_type TEXT NOT NULL,
                    title TEXT DEFAULT '',
                    source_path TEXT DEFAULT '',
                    content TEXT DEFAULT '',
                    metadata TEXT DEFAULT '{}',
                    access_control TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS evidence_edges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    relation_type TEXT NOT NULL,
                    confidence REAL DEFAULT 1.0,
                    evidence TEXT DEFAULT '[]',
                    access_control TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(source_id, target_id, relation_type)
                );

                CREATE INDEX IF NOT EXISTS idx_ev_edge_source
                ON evidence_edges(source_id);

                CREATE INDEX IF NOT EXISTS idx_ev_edge_target
                ON evidence_edges(target_id);

                CREATE INDEX IF NOT EXISTS idx_ev_edge_relation
                ON evidence_edges(relation_type);

                CREATE INDEX IF NOT EXISTS idx_ev_node_type
                ON evidence_nodes(node_type);
            """)
            conn.commit()

    # ───────────────────────────────
    # 基础 CRUD
    # ───────────────────────────────

    def ensure_node(self, node: EvidenceNode) -> bool:
        """确保节点存在（存在则更新 title/content/metadata）"""
        redacted = redact_persistence_value(
            {
                "title": node.title,
                "source_path": node.source_path,
                "content": node.content,
                "metadata": node.metadata,
            }
        ).value
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            existing = conn.execute(
                "SELECT 1 FROM evidence_nodes WHERE id = ?",
                (node.id,),
            ).fetchone()

            if existing:
                conn.execute(
                    """UPDATE evidence_nodes SET
                        node_type = ?, title = ?, source_path = ?,
                        content = ?, metadata = ?, access_control = ?
                       WHERE id = ?""",
                    (
                        node.node_type.value,
                        redacted["title"],
                        redacted["source_path"],
                        redacted["content"],
                        json.dumps(redacted["metadata"], ensure_ascii=False),
                        json.dumps(node.access_control, ensure_ascii=False, sort_keys=True),
                        node.id,
                    ),
                )
            else:
                conn.execute(
                    """INSERT INTO evidence_nodes
                        (id, node_type, title, source_path, content, metadata,
                         access_control, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        node.id,
                        node.node_type.value,
                        redacted["title"],
                        redacted["source_path"],
                        redacted["content"],
                        json.dumps(redacted["metadata"], ensure_ascii=False),
                        json.dumps(node.access_control, ensure_ascii=False, sort_keys=True),
                        node.created_at.isoformat(),
                    ),
                )
            conn.commit()
        return True

    def add_edge(self, edge: EvidenceEdge) -> bool:
        """添加关系边（存在则更新 confidence/evidence）"""
        redacted_evidence = redact_persistence_value(edge.evidence).value
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            existing = conn.execute(
                """SELECT id FROM evidence_edges
                   WHERE source_id = ? AND target_id = ? AND relation_type = ?""",
                (edge.source_id, edge.target_id, edge.relation_type.value),
            ).fetchone()

            if existing:
                conn.execute(
                    """UPDATE evidence_edges SET
                        confidence = ?, evidence = ?, access_control = ?, created_at = ?
                       WHERE id = ?""",
                    (
                        edge.confidence,
                        json.dumps(redacted_evidence, ensure_ascii=False),
                        json.dumps(edge.access_control, ensure_ascii=False, sort_keys=True),
                        edge.created_at.isoformat(),
                        existing[0],
                    ),
                )
            else:
                conn.execute(
                    """INSERT INTO evidence_edges
                        (source_id, target_id, relation_type, confidence, evidence,
                         access_control, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        edge.source_id,
                        edge.target_id,
                        edge.relation_type.value,
                        edge.confidence,
                        json.dumps(redacted_evidence, ensure_ascii=False),
                        json.dumps(edge.access_control, ensure_ascii=False, sort_keys=True),
                        edge.created_at.isoformat(),
                    ),
                )
            conn.commit()
        return True

    def get_node(self, node_id: str) -> Optional[EvidenceNode]:
        """按 ID 获取节点"""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.row_factory = sqlite3.Row  # noqa
            row = conn.execute(
                "SELECT * FROM evidence_nodes WHERE id = ?",
                (node_id,),
            ).fetchone()
        return self._row_to_node(row) if row else None

    def get_edges(
        self,
        source_id: Optional[str] = None,
        target_id: Optional[str] = None,
        relation_type: Optional[EvidenceRelationType] = None,
    ) -> List[EvidenceEdge]:
        """查询边"""
        conditions = []
        params = []
        if source_id:
            conditions.append("source_id = ?")
            params.append(source_id)
        if target_id:
            conditions.append("target_id = ?")
            params.append(target_id)
        if relation_type:
            conditions.append("relation_type = ?")
            params.append(relation_type.value)

        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
        sql = f"SELECT * FROM evidence_edges {where_clause} ORDER BY created_at DESC"  # nosec B608

        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.row_factory = sqlite3.Row  # noqa
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_edge(row) for row in rows]

    def project_cognition_episode(
        self,
        *,
        effect_id: str,
        revision_id: str,
        manifest_hash: str,
        nodes: Sequence[Mapping[str, Any]],
        edges: Sequence[Mapping[str, Any]],
        omissions: Sequence[Mapping[str, Any]],
        access_control: Mapping[str, Any],
        created_at: str,
    ) -> Dict[str, Any]:
        """Atomically project one committed episode and its omission denominator.

        ``nodes`` and ``edges`` are deterministic derivatives of an immutable
        cognition revision.  The target-local effect row is committed in the
        same transaction, so a state-outbox replay can independently recover a
        missing cross-database receipt without re-running a model.
        """

        # Delay the upper-layer contract import until this lower-level module
        # has finished loading; ``core.cognitive`` exports engines that refer
        # back to EvidenceGraph.
        from core.cognitive.access_control import (
            cognitive_access_hash,
            validate_cognitive_access_envelope,
        )
        from core.cognitive.state_contract import sha256_json

        validate_cognitive_access_envelope(access_control)
        normalized_effect = str(effect_id or "").strip()
        normalized_revision = str(revision_id or "").strip()
        if not normalized_effect or not normalized_revision:
            raise ValueError("cognition episode projection identity is required")
        expected_manifest_hash = sha256_json(
            {
                "revision_id": normalized_revision,
                "nodes": [dict(value) for value in nodes],
                "edges": [dict(value) for value in edges],
                "omissions": [dict(value) for value in omissions],
                "access_control_hash": cognitive_access_hash(access_control),
            }
        )
        if str(manifest_hash) != expected_manifest_hash:
            raise ValueError("cognition episode evidence manifest hash mismatch")
        before_hash = sha256_json(
            {"revision_id": normalized_revision, "projection_state": "unprojected"}
        )
        after_hash = expected_manifest_hash
        acl_json = json.dumps(dict(access_control), ensure_ascii=False, sort_keys=True)
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            existing = conn.execute(
                "SELECT * FROM cognition_episode_projection_effects WHERE effect_id=?",
                (normalized_effect,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["revision_id"]) != normalized_revision
                    or str(existing["manifest_hash"]) != expected_manifest_hash
                    or str(existing["before_hash"]) != before_hash
                    or str(existing["after_hash"]) != after_hash
                    or int(existing["node_count"]) != len(nodes)
                    or int(existing["edge_count"]) != len(edges)
                    or int(existing["omission_count"]) != len(omissions)
                    or str(existing["access_control_hash"]) != cognitive_access_hash(access_control)
                ):
                    raise RuntimeError("cognition episode evidence effect identity conflict")
                self._verify_episode_projection_rows(
                    conn,
                    revision_id=normalized_revision,
                    nodes=nodes,
                    edges=edges,
                    omissions=omissions,
                )
                return dict(existing)

            conn.execute("BEGIN IMMEDIATE")
            try:
                for raw_node in nodes:
                    node = dict(raw_node)
                    node_id = str(node["id"])
                    node_type = EvidenceNodeType(str(node["node_type"]))
                    redacted = redact_persistence_value(
                        {
                            "title": str(node.get("title") or ""),
                            "source_path": str(node.get("source_path") or ""),
                            "content": str(node.get("content") or ""),
                            "metadata": dict(node.get("metadata") or {}),
                        }
                    ).value
                    node_values = (
                        node_type.value,
                        redacted["title"],
                        redacted["source_path"],
                        redacted["content"],
                        json.dumps(redacted["metadata"], ensure_ascii=False, sort_keys=True),
                        acl_json,
                    )
                    existing_node = conn.execute(
                        """SELECT node_type, title, source_path, content, metadata,
                                  access_control
                           FROM evidence_nodes WHERE id=?""",
                        (node_id,),
                    ).fetchone()
                    if existing_node is not None:
                        if tuple(existing_node) != node_values:
                            raise RuntimeError("cognition episode evidence node identity conflict")
                    else:
                        conn.execute(
                            """INSERT INTO evidence_nodes
                               (id, node_type, title, source_path, content, metadata,
                                access_control, created_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            (node_id, *node_values, created_at),
                        )
                for raw_edge in edges:
                    edge = dict(raw_edge)
                    relation_type = EvidenceRelationType(str(edge["relation_type"]))
                    evidence = redact_persistence_value(list(edge.get("evidence") or ())).value
                    edge_key = (
                        str(edge["source_id"]),
                        str(edge["target_id"]),
                        relation_type.value,
                    )
                    edge_values = (
                        float(edge.get("confidence", 1.0)),
                        json.dumps(evidence, ensure_ascii=False),
                        acl_json,
                    )
                    existing_edge = conn.execute(
                        """SELECT confidence, evidence, access_control
                           FROM evidence_edges
                           WHERE source_id=? AND target_id=? AND relation_type=?""",
                        edge_key,
                    ).fetchone()
                    if existing_edge is not None:
                        if tuple(existing_edge) != edge_values:
                            raise RuntimeError("cognition episode evidence edge identity conflict")
                    else:
                        conn.execute(
                            """INSERT INTO evidence_edges
                               (source_id, target_id, relation_type, confidence, evidence,
                                access_control, created_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?)""",
                            (*edge_key, *edge_values, created_at),
                        )
                for raw_omission in omissions:
                    omission = dict(raw_omission)
                    omission_id = str(omission["omission_id"])
                    omission_values = (
                        normalized_revision,
                        str(omission["field_name"]),
                        int(omission["entry_index"]),
                        "omitted",
                        str(omission["reason_code"]),
                        str(omission["payload_hash"]),
                    )
                    existing_omission = conn.execute(
                        """SELECT omission_id, revision_id, field_name, entry_index,
                                  disposition, reason_code, payload_hash
                           FROM cognition_episode_projection_omissions
                           WHERE omission_id=? OR
                                 (revision_id=? AND field_name=? AND entry_index=?)""",
                        (
                            omission_id,
                            normalized_revision,
                            str(omission["field_name"]),
                            int(omission["entry_index"]),
                        ),
                    ).fetchone()
                    if existing_omission is not None:
                        if tuple(existing_omission) != (omission_id, *omission_values):
                            raise RuntimeError(
                                "cognition episode evidence omission identity conflict"
                            )
                    else:
                        conn.execute(
                            """INSERT INTO cognition_episode_projection_omissions
                               (omission_id, revision_id, field_name, entry_index,
                                disposition, reason_code, payload_hash, created_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            (omission_id, *omission_values, created_at),
                        )
                conn.execute(
                    """INSERT INTO cognition_episode_projection_effects
                       (effect_id, revision_id, manifest_hash, before_hash, after_hash,
                        node_count, edge_count, omission_count, access_control_hash,
                        created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        normalized_effect,
                        normalized_revision,
                        expected_manifest_hash,
                        before_hash,
                        after_hash,
                        len(nodes),
                        len(edges),
                        len(omissions),
                        cognitive_access_hash(access_control),
                        created_at,
                    ),
                )
                self._verify_episode_projection_rows(
                    conn,
                    revision_id=normalized_revision,
                    nodes=nodes,
                    edges=edges,
                    omissions=omissions,
                )
                conn.commit()
            except (KeyError, TypeError, ValueError, RuntimeError, sqlite3.Error):
                conn.rollback()
                raise
            row = conn.execute(
                "SELECT * FROM cognition_episode_projection_effects WHERE effect_id=?",
                (normalized_effect,),
            ).fetchone()
        assert row is not None
        return dict(row)

    @staticmethod
    def _verify_episode_projection_rows(
        conn: sqlite3.Connection,
        *,
        revision_id: str,
        nodes: Sequence[Mapping[str, Any]],
        edges: Sequence[Mapping[str, Any]],
        omissions: Sequence[Mapping[str, Any]],
    ) -> None:
        missing_nodes = [
            str(node["id"])
            for node in nodes
            if conn.execute(
                "SELECT 1 FROM evidence_nodes WHERE id=? AND node_type=?",
                (str(node["id"]), str(node["node_type"])),
            ).fetchone()
            is None
        ]
        missing_edges = [
            (str(edge["source_id"]), str(edge["target_id"]), str(edge["relation_type"]))
            for edge in edges
            if conn.execute(
                """SELECT 1 FROM evidence_edges
                   WHERE source_id=? AND target_id=? AND relation_type=?""",
                (
                    str(edge["source_id"]),
                    str(edge["target_id"]),
                    str(edge["relation_type"]),
                ),
            ).fetchone()
            is None
        ]
        omission_count = int(
            conn.execute(
                """SELECT COUNT(*) FROM cognition_episode_projection_omissions
                   WHERE revision_id=? AND disposition='omitted'""",
                (revision_id,),
            ).fetchone()[0]
        )
        if missing_nodes or missing_edges or omission_count != len(omissions):
            raise RuntimeError("cognition episode evidence target verification failed")

    # ───────────────────────────────
    # 高层语义接口
    # ───────────────────────────────

    def add_observation_sources(
        self,
        observation_id: str,
        source_items: List,
        observation_summary: str = "",
    ) -> List[EvidenceEdge]:
        """
        把 Observation 追溯到原始来源（Memory / Knowledge）

        Args:
            observation_id: Observation.id
            source_items: SourceItem 列表
            observation_summary: Observation 摘要
        """
        if not source_items:
            return []

        self.ensure_node(
            EvidenceNode(
                id=observation_id,
                node_type=EvidenceNodeType.OBSERVATION,
                title=f"Observation {observation_id}",
                content=observation_summary,
            )
        )

        edges = []
        for item in source_items:
            # 区分 Memory 与 Knowledge
            if item.source_type == "wiki":
                node_type = EvidenceNodeType.KNOWLEDGE
                title = f"Wiki: {item.file_path}"
            elif item.source_type == "raw":
                node_type = EvidenceNodeType.MEMORY
                title = f"Memory: {item.file_path}"
            else:
                node_type = EvidenceNodeType.MEMORY
                title = f"Source: {item.file_path}"

            source_node_id = self._source_node_id(item)
            self.ensure_node(
                EvidenceNode(
                    id=source_node_id,
                    node_type=node_type,
                    title=title,
                    source_path=item.file_path,
                    content=item.content[:300] if hasattr(item, "content") and item.content else "",
                    metadata={
                        "source_type": item.source_type,
                        "timestamp": (
                            item.timestamp.isoformat()
                            if hasattr(item, "timestamp") and item.timestamp
                            else None
                        ),
                    },
                )
            )

            edge = EvidenceEdge(
                source_id=observation_id,
                target_id=source_node_id,
                relation_type=EvidenceRelationType.OBSERVED_IN,
                confidence=1.0,
                evidence=[f"Observation 派生自 {item.source_type}: {item.file_path}"],
            )
            self.add_edge(edge)
            edges.append(edge)

        return edges

    def add_mirror_observations(
        self,
        mirror_id: str,
        observation_ids: List[str],
        mirror_summary: str = "",
        mirror_metadata: Optional[Dict] = None,
        evidence_by_observation: Optional[Dict[str, str]] = None,
    ) -> List[EvidenceEdge]:
        """Mirror 使用了哪些 Observation"""
        existing = self.get_node(mirror_id)
        metadata = (
            mirror_metadata
            if mirror_metadata is not None
            else (existing.metadata if existing else {})
        )
        content = mirror_summary or (existing.content if existing else "")
        self.ensure_node(
            EvidenceNode(
                id=mirror_id,
                node_type=EvidenceNodeType.MIRROR,
                title=f"Mirror {mirror_id}",
                content=content,
                metadata=metadata,
            )
        )

        edges = []
        evidence_by_observation = evidence_by_observation or {}
        for obs_id in observation_ids:
            edge = EvidenceEdge(
                source_id=mirror_id,
                target_id=obs_id,
                relation_type=EvidenceRelationType.USED_IN,
                confidence=1.0,
                evidence=[
                    evidence_by_observation.get(
                        obs_id,
                        "Mirror 构建时引用该 Observation",
                    )
                ],
            )
            self.add_edge(edge)
            edges.append(edge)
        return edges

    def add_insight_derivation(
        self,
        insight_id: str,
        mirror_id: str,
        observation_ids: List[str],
        knowledge_refs: Optional[List[str]] = None,
        reflection_id: Optional[str] = None,
        insight_summary: str = "",
        insight_metadata: Optional[Dict] = None,
        include_mirror_observation_edges: bool = True,
    ) -> Dict[str, List[EvidenceEdge]]:
        """
        记录 Insight 的完整血缘

        Returns:
            Dict[edge_type, edges]
        """
        existing = self.get_node(insight_id)
        metadata = (
            insight_metadata
            if insight_metadata is not None
            else (existing.metadata if existing else {})
        )
        content = insight_summary or (existing.content if existing else "")
        self.ensure_node(
            EvidenceNode(
                id=insight_id,
                node_type=EvidenceNodeType.INSIGHT,
                title=f"Insight {insight_id}",
                content=content,
                metadata=metadata,
            )
        )

        result: Dict[str, List[EvidenceEdge]] = {
            "insight_to_mirror": [],
            "insight_to_observations": [],
            "mirror_to_observations": [],
            "insight_to_knowledge": [],
            "reflection_to_insight": [],
        }

        # Insight → Mirror
        result["insight_to_mirror"].append(
            self._add_edge(
                insight_id,
                mirror_id,
                EvidenceRelationType.DERIVED_FROM,
                evidence=["Insight 基于 Mirror 证据链生成"],
            )
        )

        # Insight → Observations
        for obs_id in observation_ids:
            result["insight_to_observations"].append(
                self._add_edge(
                    insight_id,
                    obs_id,
                    EvidenceRelationType.DERIVED_FROM,
                    evidence=["Insight 直接派生自该 Observation"],
                )
            )

        # Mirror → Observations
        if include_mirror_observation_edges:
            result["mirror_to_observations"].extend(
                self.add_mirror_observations(
                    mirror_id,
                    observation_ids,
                )
            )

        # Insight → Knowledge references
        if knowledge_refs:
            for ref_path in knowledge_refs:
                ref_node_id = self._knowledge_node_id(ref_path)
                self.ensure_node(
                    EvidenceNode(
                        id=ref_node_id,
                        node_type=EvidenceNodeType.KNOWLEDGE,
                        title=f"Wiki: {ref_path}",
                        source_path=ref_path,
                    )
                )
                result["insight_to_knowledge"].append(
                    self._add_edge(
                        insight_id,
                        ref_node_id,
                        EvidenceRelationType.SUPPORTS,
                        evidence=["Insight 引用该 Wiki 知识作为背景"],
                    )
                )

        # Reflection → Insight
        if reflection_id:
            if not self.get_node(reflection_id):
                self.ensure_node(
                    EvidenceNode(
                        id=reflection_id,
                        node_type=EvidenceNodeType.REFLECTION,
                        title=f"Reflection {reflection_id}",
                    )
                )
            result["reflection_to_insight"].append(
                self._add_edge(
                    insight_id,
                    reflection_id,
                    EvidenceRelationType.GENERATED_FROM,
                    evidence=["Insight 由该 Reflection 过程生成"],
                )
            )
            result["reflection_to_insight"].append(
                self._add_edge(
                    reflection_id,
                    mirror_id,
                    EvidenceRelationType.USED_IN,
                    evidence=["Reflection 过程使用该 Mirror"],
                )
            )

        return result

    def add_reflection_record(
        self,
        record,
        insight_id: Optional[str] = None,
        mirror_id: Optional[str] = None,
    ) -> Dict[str, List[EvidenceEdge]]:
        """
        将 ReflectionRecord 写入 Evidence Graph

        Args:
            record: ReflectionRecord 实例
            insight_id: 可选的 Insight 节点 ID（默认使用 record.id:insight）
            mirror_id: 可选的 Mirror 节点 ID（默认使用 record.id:mirror）
        """
        from core.reflection.models import ReflectionRecord

        if not isinstance(record, ReflectionRecord):
            raise TypeError("record must be a ReflectionRecord")

        reflection_node_id = record.id
        self.ensure_node(
            EvidenceNode(
                id=reflection_node_id,
                node_type=EvidenceNodeType.REFLECTION,
                title=f"Reflection {record.id}",
                content=record.trigger_event,
                metadata={
                    "trigger": record.trigger.value,
                    "user_query": record.user_query,
                    "dimensions": record.mirror_dimensions,
                },
            )
        )

        # Mirror → Observations
        mirror_node_id = mirror_id or f"{record.id}:mirror"
        observation_ids = [s.observation_id for s in record.mirror_snapshots]
        evidence_by_observation = {}
        for snap in record.mirror_snapshots:
            self.ensure_node(
                EvidenceNode(
                    id=snap.observation_id,
                    node_type=EvidenceNodeType.OBSERVATION,
                    title=f"Observation {snap.observation_id}",
                    content=snap.value_summary,
                    metadata={
                        "dimension": snap.dimension,
                        "confidence": snap.confidence,
                    },
                )
            )
            evidence_by_observation[snap.observation_id] = (
                snap.evidence_summary or "Observation 被 Mirror 引用"
            )

        self.add_mirror_observations(
            mirror_node_id,
            observation_ids,
            mirror_metadata={"dimensions": record.mirror_dimensions},
            evidence_by_observation=evidence_by_observation,
        )

        # Insight node
        insight_node_id = insight_id or f"{record.id}:insight"
        if record.insight:
            self.add_insight_derivation(
                insight_node_id,
                mirror_node_id,
                observation_ids,
                reflection_id=reflection_node_id,
                insight_summary=record.insight.summary,
                insight_metadata={
                    "key_points": record.insight.key_points,
                    "dimensions": record.insight.dimensions_involved,
                },
                include_mirror_observation_edges=False,
            )
        else:
            # Without an insight, add_insight_derivation() is not invoked, so the
            # reflection still needs its mirror lineage edge.
            self.add_edge(
                EvidenceEdge(
                    source_id=reflection_node_id,
                    target_id=mirror_node_id,
                    relation_type=EvidenceRelationType.USED_IN,
                    evidence=["Reflection 使用此 Mirror"],
                )
            )

        return {
            "reflection_id": reflection_node_id,  # type: ignore[dict-item]
            "mirror_id": mirror_node_id,  # type: ignore[dict-item]
            "insight_id": insight_node_id if record.insight else None,  # type: ignore[dict-item]
            "observation_ids": observation_ids,  # type: ignore[dict-item]
        }

    # ───────────────────────────────
    # 血缘查询
    # ───────────────────────────────

    def get_lineage(
        self,
        node_id: str,
        direction: str = "both",
        depth: int = 5,
    ) -> Dict:
        """
        获取节点的血缘网络

        Args:
            node_id: 起始节点 ID
            direction: "upstream" | "downstream" | "both"
            depth: 最大遍历深度
        """
        nodes: Dict[str, EvidenceNode] = {}
        edges: List[EvidenceEdge] = []
        visited_edges = set()  # type: ignore[var-annotated]

        start_node = self.get_node(node_id)
        if not start_node:
            return {"nodes": {}, "edges": []}

        nodes[node_id] = start_node
        self._traverse(node_id, direction, depth, nodes, edges, visited_edges, current_depth=0)

        return {
            "nodes": {nid: n.to_dict() for nid, n in nodes.items()},
            "edges": [e.to_dict() for e in edges],
        }

    def _traverse(
        self,
        node_id: str,
        direction: str,
        max_depth: int,
        nodes: Dict[str, EvidenceNode],
        edges: List[EvidenceEdge],
        visited_edges: set,
        current_depth: int,
    ):
        if current_depth >= max_depth:
            return

        if direction in ("upstream", "both"):
            # Canonical direction is derived → evidence, so upstream follows
            # the current derived node's outgoing evidence edges.
            for edge in self.get_edges(source_id=node_id):
                key = (edge.source_id, edge.target_id, edge.relation_type.value)
                if key in visited_edges:
                    continue
                visited_edges.add(key)
                edges.append(edge)
                if edge.target_id not in nodes:
                    nodes[edge.target_id] = self.get_node(
                        edge.target_id
                    )  # type: ignore[assignment]
                self._traverse(
                    edge.target_id,
                    direction,
                    max_depth,
                    nodes,
                    edges,
                    visited_edges,
                    current_depth + 1,
                )

        if direction in ("downstream", "both"):
            # Downstream asks which derived nodes depend on this evidence.
            for edge in self.get_edges(target_id=node_id):
                key = (edge.source_id, edge.target_id, edge.relation_type.value)
                if key in visited_edges:
                    continue
                visited_edges.add(key)
                edges.append(edge)
                if edge.source_id not in nodes:
                    nodes[edge.source_id] = self.get_node(
                        edge.source_id
                    )  # type: ignore[assignment]
                self._traverse(
                    edge.source_id,
                    direction,
                    max_depth,
                    nodes,
                    edges,
                    visited_edges,
                    current_depth + 1,
                )

    def get_evidence_chain_for_insight(
        self,
        insight_id: str,
    ) -> List[Tuple[EvidenceNode, EvidenceRelationType, EvidenceNode]]:
        """
        获取某个 Insight 的直接证据链

        Returns:
            [(source_node, relation, target_node), ...]
        """
        chain = []
        edges = self.get_edges(source_id=insight_id)
        for edge in edges:
            target = self.get_node(edge.target_id)
            source = self.get_node(edge.source_id)
            if source and target:
                chain.append((source, edge.relation_type, target))
        return chain

    def explain_why(
        self,
        insight_id: str,
    ) -> Dict:
        """
        回答"为什么这么认为"

        返回结构化证据说明，包含直接证据链、Observation 和原始 Memory/Knowledge。
        """
        lineage = self.get_lineage(insight_id, direction="upstream", depth=10)
        direct_chain = self.get_evidence_chain_for_insight(insight_id)

        nodes = dict(lineage["nodes"])
        edge_keys = {
            (edge["source_id"], edge["target_id"], edge["relation_type"])
            for edge in lineage["edges"]
        }
        direct_evidence_chain = []
        for source, relation, target in direct_chain:
            nodes[source.id] = source.to_dict()
            nodes[target.id] = target.to_dict()
            edge_keys.add((source.id, target.id, relation.value))
            direct_evidence_chain.append(
                {
                    "source_id": source.id,
                    "source_type": source.node_type.value,
                    "relation": relation.value,
                    "target_id": target.id,
                    "target_type": target.node_type.value,
                    "target_title": target.title,
                    "target_source_path": target.source_path,
                    "target_content": target.content,
                }
            )

        observations = [
            n for n in nodes.values() if n["node_type"] == EvidenceNodeType.OBSERVATION.value
        ]
        memories = [n for n in nodes.values() if n["node_type"] == EvidenceNodeType.MEMORY.value]
        knowledges = [
            n for n in nodes.values() if n["node_type"] == EvidenceNodeType.KNOWLEDGE.value
        ]

        return {
            "insight_id": insight_id,
            "direct_evidence_chain": direct_evidence_chain,
            "observations": observations,
            "memories": memories,
            "knowledges": knowledges,
            "edge_count": len(edge_keys),
            "node_count": len(nodes),
        }

    def get_stats(self) -> Dict:
        """获取图谱统计"""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            total_nodes = conn.execute("SELECT COUNT(*) FROM evidence_nodes").fetchone()[0]
            total_edges = conn.execute("SELECT COUNT(*) FROM evidence_edges").fetchone()[0]
            by_type = conn.execute(
                "SELECT node_type, COUNT(*) FROM evidence_nodes GROUP BY node_type"
            ).fetchall()
            by_relation = conn.execute(
                "SELECT relation_type, COUNT(*) FROM evidence_edges GROUP BY relation_type"
            ).fetchall()

        return {
            "total_nodes": total_nodes,
            "total_edges": total_edges,
            "by_type": {t: c for t, c in by_type},
            "by_relation": {r: c for r, c in by_relation},
        }

    # ───────────────────────────────
    # 工具方法
    # ───────────────────────────────

    def _add_edge(
        self,
        source_id: str,
        target_id: str,
        relation_type: EvidenceRelationType,
        confidence: float = 1.0,
        evidence: Optional[List[str]] = None,
    ) -> EvidenceEdge:
        edge = EvidenceEdge(
            source_id=source_id,
            target_id=target_id,
            relation_type=relation_type,
            confidence=confidence,
            evidence=evidence or [],
        )
        self.add_edge(edge)
        return edge

    @staticmethod
    def _source_node_id(item) -> str:
        """根据 SourceItem 生成稳定节点 ID"""
        path = item.file_path
        if path.startswith("/"):
            return f"source:{item.source_type}:{path}"
        return f"source:{item.source_type}:{path}"

    @staticmethod
    def _knowledge_node_id(ref_path: str) -> str:
        """根据 Wiki 路径生成稳定节点 ID"""
        return f"knowledge:{ref_path}"

    def _row_to_node(self, row: sqlite3.Row) -> EvidenceNode:
        return EvidenceNode(
            id=row["id"],
            node_type=EvidenceNodeType(row["node_type"]),
            title=row["title"] or "",
            source_path=row["source_path"] or "",
            content=row["content"] or "",
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
            access_control=(json.loads(row["access_control"]) if row["access_control"] else {}),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def _row_to_edge(self, row: sqlite3.Row) -> EvidenceEdge:
        return EvidenceEdge(
            id=row["id"],
            source_id=row["source_id"],
            target_id=row["target_id"],
            relation_type=EvidenceRelationType(row["relation_type"]),
            confidence=row["confidence"] or 1.0,
            evidence=json.loads(row["evidence"]) if row["evidence"] else [],
            access_control=(json.loads(row["access_control"]) if row["access_control"] else {}),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
