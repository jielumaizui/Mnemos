"""Maintenance and outbox reconstruction for the cognitive graph store."""

from __future__ import annotations

from contextlib import AbstractContextManager
import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Mapping, Optional

from core.cognitive.access_control import cognitive_access_hash
from core.cognitive.decision_trace import (
    MaterialActionAuthorization,
    MaterialActionRequest,
    authorize_exact_project_contract_action,
)
from core.cognitive.state_contract import sha256_json

from .models import CanonicalNode, CognitiveRelation, SyncOutboxItem

from .store_contracts import (
    PENDING_LIMIT,
    COGNITIVE_CANONICAL_NODE_ACTION,
    COGNITIVE_GRAPH_MAINTENANCE_CONTRACT_ID,
    COGNITIVE_GRAPH_MAINTENANCE_CONTRACT_REVISION,
    COGNITIVE_GRAPH_MAINTENANCE_CONTRACT_TEXT,
    COGNITIVE_GRAPH_MAINTENANCE_PRODUCER_HASH,
    COGNITIVE_RELATION_ACTION,
    COGNITIVE_RELATION_EXECUTOR,
    COGNITIVE_RELATION_OWNER,
    _decision_timestamp,
    _feedback_urn,
    _kg_urn,
    _layer_of_urn,
    _now,
    _obs_urn,
    _parse_graph_access,
    _ref_urn,
    _relation_id,
    _session_urn,
    _strictest_graph_access,
    _wiki_urn,
    cognitive_canonical_node_material_action_binding,
)

logger = logging.getLogger(__name__)


class CognitiveGraphMaintenanceMixin:
    """Recovery, rebuild, and graph-derived relation helpers."""

    if TYPE_CHECKING:
        db_path: Path

        def _conn(self) -> AbstractContextManager[sqlite3.Connection]: ...

        def fetch_outbox(
            self,
            unprocessed_only: bool = True,
            limit: int = 100,
        ) -> List[SyncOutboxItem]: ...

        def add_relations_atomic(
            self,
            relations: Iterable[Dict[str, Any]],
            *,
            material_action_resolver: (
                Callable[[Mapping[str, str]], MaterialActionAuthorization] | None
            ) = None,
        ) -> List[CognitiveRelation]: ...

        def mark_outbox_processed(self, item_id: int) -> bool: ...

        def cleanup_outbox(self, retention_days: int = 30) -> int: ...

        @staticmethod
        def _canonical_id(name: str) -> str: ...

        def add_canonical_node(
            self,
            canonical_name: str,
            canonical_id: Optional[str] = None,
            aliases: Optional[List[str]] = None,
            source_ids: Optional[List[str]] = None,
            embedding: Optional[bytes] = None,
            access_control: Mapping[str, Any] | None = None,
            material_action: MaterialActionAuthorization | None = None,
        ) -> CanonicalNode: ...

    def _authorize_maintenance_relation(
        self,
        binding: Mapping[str, str],
        *,
        source_namespace: str,
        source_facts: Mapping[str, Any],
        evidence_refs: tuple[str, ...],
        created_at: str,
    ) -> MaterialActionAuthorization:
        state_db_path = (self.db_path.parent / "producer_consumer_ledger.db").resolve(strict=False)
        request = MaterialActionRequest(
            owner=COGNITIVE_RELATION_OWNER,
            executor_id=COGNITIVE_RELATION_EXECUTOR,
            action_type=COGNITIVE_RELATION_ACTION,
            target_ref=str(binding["target_ref"]),
            input_hash=str(binding["input_hash"]),
            expected_state_db=str(state_db_path),
        )
        return authorize_exact_project_contract_action(
            expected_request=request,
            state_db_path=state_db_path,
            contract_id=COGNITIVE_GRAPH_MAINTENANCE_CONTRACT_ID,
            contract_revision_id=(COGNITIVE_GRAPH_MAINTENANCE_CONTRACT_REVISION),
            contract_text=COGNITIVE_GRAPH_MAINTENANCE_CONTRACT_TEXT,
            source_namespace=source_namespace,
            source_facts={
                "schema_version": "mnemos.cognitive_graph_maintenance_facts.v1",
                **dict(source_facts),
                "relation_binding": dict(binding),
            },
            decision_checks={
                "maintenance_source_facts_present": bool(source_facts),
                "relation_binding_complete": bool(
                    binding.get("target_ref") and binding.get("input_hash")
                ),
                "maintenance_evidence_present": bool(evidence_refs),
            },
            evidence_refs=evidence_refs,
            task="Rebuild one missing CognitiveGraph relation",
            goal=("Restore only the exact relation derived from durable maintenance input."),
            constraints=(
                "The outbox or canonical-node snapshot and relation binding must remain exact.",
                "Maintenance cannot invent a relation outside the deterministic rebuild rules.",
                "Each relation requires an independent target effect receipt.",
            ),
            created_at=created_at,
            producer="cognitive-graph-maintenance",
            producer_version=COGNITIVE_GRAPH_MAINTENANCE_CONTRACT_REVISION,
            producer_code_hash=COGNITIVE_GRAPH_MAINTENANCE_PRODUCER_HASH,
            evaluator_id="cognitive-graph-maintenance-evaluator",
            approved_candidate_key="rebuild_exact_missing_relation",
            approved_candidate_summary=(
                "Rebuild the exact relation derived by the maintenance rule."
            ),
            rejected_candidate_key="retain_current_graph_state",
            rejected_candidate_summary=(
                "Retain graph state when source evidence or relation binding drifts."
            ),
            approved_reason_code="maintenance_relation_binding_verified",
            rejected_reason_code="maintenance_relation_binding_rejected",
            committed_metric="cognitive_graph_maintenance_relation_committed",
            rejected_metric="unbound_maintenance_relation_count",
        )

    def _authorize_maintenance_node(
        self,
        binding: Mapping[str, str],
        *,
        source_facts: Mapping[str, Any],
        evidence_refs: tuple[str, ...],
        created_at: str,
    ) -> MaterialActionAuthorization:
        state_db_path = (self.db_path.parent / "producer_consumer_ledger.db").resolve(strict=False)
        request = MaterialActionRequest(
            owner=COGNITIVE_RELATION_OWNER,
            executor_id=COGNITIVE_RELATION_EXECUTOR,
            action_type=COGNITIVE_CANONICAL_NODE_ACTION,
            target_ref=str(binding["target_ref"]),
            input_hash=str(binding["input_hash"]),
            expected_state_db=str(state_db_path),
        )
        return authorize_exact_project_contract_action(
            expected_request=request,
            state_db_path=state_db_path,
            contract_id=COGNITIVE_GRAPH_MAINTENANCE_CONTRACT_ID,
            contract_revision_id=(COGNITIVE_GRAPH_MAINTENANCE_CONTRACT_REVISION),
            contract_text=COGNITIVE_GRAPH_MAINTENANCE_CONTRACT_TEXT,
            source_namespace="cognitive-graph-node-rebuild",
            source_facts={
                "schema_version": "mnemos.cognitive_graph_maintenance_facts.v1",
                **dict(source_facts),
                "canonical_node_binding": dict(binding),
            },
            decision_checks={
                "maintenance_source_facts_present": bool(source_facts),
                "canonical_node_binding_complete": bool(
                    binding.get("target_ref") and binding.get("input_hash")
                ),
                "maintenance_evidence_present": bool(evidence_refs),
            },
            evidence_refs=evidence_refs,
            task="Rebuild one missing CognitiveGraph canonical node",
            goal=("Restore only the exact canonical node derived from active relations."),
            constraints=(
                "The relation snapshot and canonical-node binding must remain exact.",
                "Maintenance cannot invent aliases, source URNs, or access scope.",
                "Each canonical node requires an independent target effect receipt.",
            ),
            created_at=created_at,
            producer="cognitive-graph-maintenance",
            producer_version=COGNITIVE_GRAPH_MAINTENANCE_CONTRACT_REVISION,
            producer_code_hash=COGNITIVE_GRAPH_MAINTENANCE_PRODUCER_HASH,
            evaluator_id="cognitive-graph-maintenance-node-evaluator",
            approved_candidate_key="rebuild_exact_missing_canonical_node",
            approved_candidate_summary=(
                "Rebuild the exact canonical node derived by the maintenance rule."
            ),
            rejected_candidate_key="retain_current_canonical_node_state",
            rejected_candidate_summary=(
                "Retain node state when source evidence or binding drifts."
            ),
            approved_reason_code="maintenance_node_binding_verified",
            rejected_reason_code="maintenance_node_binding_rejected",
            committed_metric="cognitive_graph_maintenance_node_committed",
            rejected_metric="unbound_maintenance_node_count",
        )

    def rebuild_missing_relations(self) -> Dict[str, int]:
        """
        兜底重建：扫描 outbox 中未处理的事件，基于 canonical_nodes 重建跨层关系，
        并从已有关系中反推缺失的 canonical 节点。

        返回统计：
        - outbox_processed: 成功重建的 outbox 条目数
        - relations_added: 实际写入的关系数（含 outbox + 跨层 canonical）
        - cross_layer_added: 由 canonical_nodes 生成的跨层关系数
        - canonical_nodes_added: 从关系中反推新增的 canonical 节点数
        - stale_relations: 当前 stale 关系总数
        """
        stats = {
            "outbox_processed": 0,
            "relations_added": 0,
            "cross_layer_added": 0,
            "canonical_nodes_added": 0,
            "stale_relations": self._count_stale(),
        }

        try:
            # 0. P111: 从已有关系中反推缺失的 canonical 节点（回填历史数据）
            try:
                stats["canonical_nodes_added"] = self._derive_canonical_nodes_from_relations()
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                ImportError,
                AttributeError,
                RuntimeError,
                sqlite3.Error,
            ):
                logger.warning("[CognitiveGraphStore] 从关系反推 canonical 节点失败", exc_info=True)

            # 1. 消费未处理的 outbox 条目（事件驱动路径失败时的兜底）
            pending = self.fetch_outbox(unprocessed_only=True, limit=PENDING_LIMIT)
            for item in pending:
                try:
                    rels = self._relations_from_outbox_item(item)
                    if rels:
                        for relation in rels:
                            relation.setdefault("access_control", item.access_control)
                        outbox_facts = {
                            "source_kind": "cognitive_graph_sync_outbox",
                            "outbox_id": int(item.id),
                            "event_type": item.event_type,
                            "payload_hash": sha256_json(item.payload),
                            "access_control_hash": cognitive_access_hash(item.access_control),
                        }
                        self.add_relations_atomic(
                            rels,
                            material_action_resolver=(
                                lambda binding: self._authorize_maintenance_relation(
                                    binding,
                                    source_namespace="cognitive-graph-outbox-rebuild",
                                    source_facts=outbox_facts,
                                    evidence_refs=(
                                        f"cognitive-graph-outbox:{item.id}",
                                        f"outbox-payload:{outbox_facts['payload_hash']}",
                                    ),
                                    created_at=item.created_at or _now(),
                                )
                            ),
                        )
                        stats["relations_added"] += len(rels)
                    if rels is not None:
                        stats["outbox_processed"] += 1
                    self.mark_outbox_processed(item.id)
                except (
                    OSError,
                    ValueError,
                    TypeError,
                    KeyError,
                    ImportError,
                    AttributeError,
                    RuntimeError,
                    sqlite3.Error,
                ):
                    logger.warning("outbox 条目 %s 重建失败", item.id, exc_info=True)

            # 2. 基于 canonical_nodes 重建跨层关系
            cross_rels = self._build_canonical_cross_layer_relations()
            if cross_rels:
                with self._conn() as conn:
                    active_relation_ids = {
                        str(row[0])
                        for row in conn.execute(
                            "SELECT id FROM cognitive_relations WHERE stale = 0"
                        ).fetchall()
                    }
                    cross_created_at = str(
                        conn.execute("SELECT MIN(created_at) FROM canonical_nodes").fetchone()[0]
                        or "1970-01-01T00:00:00+00:00"
                    )
                missing_cross_relations: dict[str, Dict[str, Any]] = {}
                for relation in cross_rels:
                    relation_id = _relation_id(
                        str(relation["source"]),
                        str(relation["target"]),
                        str(relation["relation_type"]),
                    )
                    if relation_id not in active_relation_ids:
                        missing_cross_relations.setdefault(relation_id, relation)
                cross_rels = list(missing_cross_relations.values())
            if cross_rels:
                cross_snapshot_hash = sha256_json(
                    {
                        "schema_version": ("mnemos.cognitive_graph_cross_layer_snapshot.v1"),
                        "relations": cross_rels,
                    }
                )
                self.add_relations_atomic(
                    cross_rels,
                    material_action_resolver=(
                        lambda binding: self._authorize_maintenance_relation(
                            binding,
                            source_namespace="cognitive-graph-cross-layer-rebuild",
                            source_facts={
                                "source_kind": "canonical_node_snapshot",
                                "snapshot_hash": cross_snapshot_hash,
                            },
                            evidence_refs=(f"canonical-node-snapshot:{cross_snapshot_hash}",),
                            created_at=cross_created_at,
                        )
                    ),
                )
                stats["cross_layer_added"] = len(cross_rels)
                stats["relations_added"] += len(cross_rels)

            # 3. 清理已处理 outbox，防止表无限增长
            try:
                self.cleanup_outbox(retention_days=30)
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                ImportError,
                AttributeError,
                RuntimeError,
                sqlite3.Error,
            ):
                logger.warning("[CognitiveGraphStore] outbox 清理失败", exc_info=True)
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
            sqlite3.Error,
        ):
            logger.warning("rebuild_missing_relations 执行失败", exc_info=True)

        return stats

    def _derive_canonical_nodes_from_relations(self) -> int:
        """从 cognitive_relations 的 source/target URN 反推 canonical 节点（P111 回填）。"""
        added = 0
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT id, source, target, created_at, access_control "
                    "FROM cognitive_relations WHERE stale = 0"
                ).fetchall()
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
            sqlite3.Error,
        ):
            logger.warning("[CognitiveGraphStore] 读取 cognitive_relations 失败", exc_info=True)
            return 0

        groups: Dict[str, Dict] = {}
        for row in rows:
            for urn in (row["source"], row["target"]):
                name = self._canonical_name_from_urn(urn)
                if not name:
                    continue
                canonical_id = self._canonical_id(name)
                group = groups.setdefault(
                    canonical_id,
                    {
                        "name": name,
                        "urns": set(),
                        "accesses": [],
                        "relation_ids": set(),
                        "created_at": str(row["created_at"] or _now()),
                    },
                )
                group["urns"].add(urn)
                group["relation_ids"].add(str(row["id"]))
                group["created_at"] = min(
                    str(group["created_at"]),
                    str(row["created_at"] or group["created_at"]),
                )
                group["accesses"].append(
                    _parse_graph_access(
                        row["access_control"],
                        f"relation:{row['id']}",
                    )
                )

        with self._conn() as conn:
            existing_ids = {
                str(row[0])
                for row in conn.execute(
                    "SELECT canonical_id FROM canonical_nodes"
                ).fetchall()
            }

        for group in groups.values():
            try:
                canonical_id = self._canonical_id(group["name"])
                if canonical_id in existing_ids:
                    continue
                access_control = _strictest_graph_access(
                    group["accesses"],
                    object_ref=f"canonical:{canonical_id}",
                )
                binding = cognitive_canonical_node_material_action_binding(
                    canonical_name=group["name"],
                    canonical_id=canonical_id,
                    source_ids=sorted(group["urns"]),
                    access_control=access_control,
                )
                self.add_canonical_node(
                    canonical_name=group["name"],
                    canonical_id=canonical_id,
                    source_ids=sorted(group["urns"]),
                    access_control=access_control,
                    material_action=self._authorize_maintenance_node(
                        binding,
                        source_facts={
                            "source_kind": "active_relation_snapshot",
                            "canonical_name": group["name"],
                            "source_urns": sorted(group["urns"]),
                            "relation_ids": sorted(group["relation_ids"]),
                        },
                        evidence_refs=tuple(
                            f"cognitive-relation:{relation_id}"
                            for relation_id in sorted(group["relation_ids"])
                        ),
                        created_at=_decision_timestamp(str(group["created_at"])),
                    ),
                )
                existing_ids.add(canonical_id)
                added += 1
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                ImportError,
                AttributeError,
                RuntimeError,
                sqlite3.Error,
            ):
                logger.warning(
                    "[CognitiveGraphStore] 写入 canonical 节点 %s 失败",
                    group["name"],
                    exc_info=True,
                )
        return added

    @staticmethod
    def _canonical_name_from_urn(urn: str) -> str:
        """从 URN 推断 canonical 名称。"""
        if urn.startswith("kg://"):
            return urn[5:].strip()
        if urn.startswith("wiki://"):
            p = Path(urn[7:])
            name = p.stem
            name = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", name)
            name = re.sub(r"^\d+-", "", name)
            return name.replace("-", " ").strip()
        if urn.startswith("session://"):
            return f"session:{urn[10:]}"
        if urn.startswith("obs://"):
            return f"obs:{urn[6:]}"
        if urn.startswith("ref://"):
            return f"ref:{urn[6:]}"
        return urn

    def _relations_from_outbox_item(self, item: SyncOutboxItem) -> Optional[List[Dict[str, Any]]]:
        """把一条 outbox 记录翻译成待写入的认知关系列表（None 表示未知事件类型）"""
        event_type = item.event_type
        payload = item.payload or {}

        if event_type == "knowledge_distilled":
            return self._relations_from_knowledge_distilled(payload)
        if event_type == "distill_complete":
            return self._relations_from_distill_complete(payload)
        if event_type == "wiki_page_updated":
            return self._relations_from_wiki_page_updated(payload)
        if event_type == "reflection.completed":
            return self._relations_from_reflection_completed(payload)
        if event_type == "observation.updated":
            return self._relations_from_observation_updated(payload)
        if event_type == "persona.updated":
            return self._relations_from_persona_updated(payload)

        logger.warning("rebuild_missing_relations 遇到未知 outbox 事件类型: %s", event_type)
        return None

    def _relations_from_knowledge_distilled(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        rels: List[Dict[str, Any]] = []
        session_id = payload.get("session_id", "")
        session_urn = _session_urn(session_id) if session_id else ""
        wiki_pages = payload.get("wiki_pages", []) or []
        kg_input = payload.get("kg_input", {}) or {}
        entities = kg_input.get("entities", []) or []
        kg_relations = kg_input.get("relations", []) or []

        if session_urn:
            for page_path in wiki_pages:
                wiki_urn = _wiki_urn(page_path)
                if not wiki_urn:
                    continue
                rels.append(
                    {
                        "source": session_urn,
                        "target": wiki_urn,
                        "relation_type": "derived_from",
                        "strength": 0.9,
                        "confidence": 0.85,
                        "source_layer": "session",
                        "target_layer": "wiki",
                    }
                )
                rels.append(
                    {
                        "source": wiki_urn,
                        "target": session_urn,
                        "relation_type": "derived_from",
                        "strength": 0.9,
                        "confidence": 0.85,
                        "source_layer": "wiki",
                        "target_layer": "session",
                    }
                )
            for entity_name in entities:
                rels.append(
                    {
                        "source": session_urn,
                        "target": _kg_urn(entity_name),
                        "relation_type": "related_to",
                        "strength": 0.7,
                        "confidence": 0.75,
                        "source_layer": "session",
                        "target_layer": "kg",
                    }
                )

        for rel_data in kg_relations:
            if not isinstance(rel_data, dict):
                continue
            src = rel_data.get("source", "")
            tgt = rel_data.get("target", "")
            rtype = rel_data.get("type") or rel_data.get("relation_type") or "related_to"
            confidence = float(rel_data.get("confidence", 0.5))
            strength = float(rel_data.get("strength", confidence))
            rels.append(
                {
                    "source": _kg_urn(src),
                    "target": _kg_urn(tgt),
                    "relation_type": rtype.replace("-", "_").lower(),
                    "strength": strength,
                    "confidence": confidence,
                    "source_layer": "kg",
                    "target_layer": "kg",
                }
            )

        for page_path in wiki_pages:
            wiki_urn = _wiki_urn(page_path)
            if not wiki_urn:
                continue
            for entity_name in entities:
                rels.append(
                    {
                        "source": wiki_urn,
                        "target": _kg_urn(entity_name),
                        "relation_type": "related_to",
                        "strength": 0.65,
                        "confidence": 0.7,
                        "source_layer": "wiki",
                        "target_layer": "kg",
                    }
                )

        return rels

    def _relations_from_distill_complete(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        session_id = payload.get("session_id", "")
        page_path = payload.get("page_path", "")
        if not session_id or not page_path:
            return []
        wiki_urn = _wiki_urn(page_path)
        if not wiki_urn:
            return []
        return [
            {
                "source": _session_urn(session_id),
                "target": wiki_urn,
                "relation_type": "derived_from",
                "strength": 0.95,
                "confidence": 0.9,
                "source_layer": "session",
                "target_layer": "wiki",
            }
        ]

    def _relations_from_wiki_page_updated(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        """wiki_page_updated 事件不再生成自引用边，避免图谱污染和遍历死循环。"""
        # 自引用边（source == target）对认知图无意义，且可能在遍历时造成无限循环。
        # 若未来需要标记“页面存在/更新”，应使用节点元数据而非边。
        return []

    def _relations_from_reflection_completed(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        record_id = payload.get("record_id", "")
        if not record_id:
            return []
        ref_urn = _ref_urn(record_id)
        rels: List[Dict[str, Any]] = [
            {
                "source": ref_urn,
                "target": _feedback_urn(),
                "relation_type": "influenced_by",
                "strength": 0.6,
                "confidence": 0.7,
                "source_layer": "reflection",
                "target_layer": "feedback",
            }
        ]
        summary = payload.get("insight_summary", "")
        if summary:
            rels.append(
                {
                    "source": ref_urn,
                    "target": f"wiki://L4-Reflections/insights/{record_id}.md",
                    "relation_type": "derived_from",
                    "strength": 0.85,
                    "confidence": 0.8,
                    "source_layer": "reflection",
                    "target_layer": "wiki",
                }
            )
        return rels

    def _relations_from_observation_updated(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        rels: List[Dict[str, Any]] = []
        observation_ids = payload.get("observation_ids", []) or []
        wiki_path = payload.get("wiki_path", "")
        wiki_urn = _wiki_urn(wiki_path) if wiki_path else ""
        session_id = payload.get("session_id")
        session_urn = _session_urn(session_id) if session_id else ""
        for obs_id in observation_ids:
            obs_urn = _obs_urn(obs_id)
            if wiki_urn:
                rels.append(
                    {
                        "source": obs_urn,
                        "target": wiki_urn,
                        "relation_type": "observed_in",
                        "strength": 0.8,
                        "confidence": 0.8,
                        "source_layer": "observation",
                        "target_layer": "wiki",
                    }
                )
            if session_urn:
                rels.append(
                    {
                        "source": obs_urn,
                        "target": session_urn,
                        "relation_type": "observed_in",
                        "strength": 0.75,
                        "confidence": 0.75,
                        "source_layer": "observation",
                        "target_layer": "session",
                    }
                )
        return rels

    def _relations_from_persona_updated(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        wiki_path = payload.get("wiki_path", "")
        version = payload.get("version", "latest")
        wiki_urn = _wiki_urn(wiki_path)
        if not wiki_urn:
            return []
        return [
            {
                "source": _feedback_urn(str(version)),
                "target": wiki_urn,
                "relation_type": "derived_from",
                "strength": 0.9,
                "confidence": 0.85,
                "source_layer": "feedback",
                "target_layer": "wiki",
            }
        ]

    # 单个 canonical 节点在每对层之间最多生成的关系数，防止 O(n²) 爆炸
    MAX_CROSS_LAYER_PER_PAIR = 10

    def _build_canonical_cross_layer_relations(self) -> List[Dict[str, Any]]:
        """扫描 canonical_nodes，为跨层的 source_ids 补充 related_to 关系

        限制：每对层之间最多取 Top-K（按 URN 字符串排序后截取），
        避免单个概念在多层有多个来源时产生组合爆炸。
        """
        rels: List[Dict[str, Any]] = []
        with self._conn() as conn:
            rows = conn.execute("SELECT source_ids FROM canonical_nodes").fetchall()
        for row in rows:
            try:
                source_ids = json.loads(row["source_ids"] or "[]")
            except json.JSONDecodeError:
                continue
            ids_by_layer: Dict[str, List[str]] = {}
            for urn in source_ids:
                layer = _layer_of_urn(urn)
                if not layer:
                    continue
                ids_by_layer.setdefault(layer, []).append(urn)
            layers = list(ids_by_layer.keys())
            for i in range(len(layers)):
                for j in range(i + 1, len(layers)):
                    src_list = sorted(ids_by_layer[layers[i]])[: self.MAX_CROSS_LAYER_PER_PAIR]
                    tgt_list = sorted(ids_by_layer[layers[j]])[: self.MAX_CROSS_LAYER_PER_PAIR]
                    for src in src_list:
                        for tgt in tgt_list:
                            rels.append(
                                {
                                    "source": src,
                                    "target": tgt,
                                    "relation_type": "related_to",
                                    "strength": 0.6,
                                    "confidence": 0.6,
                                    "source_layer": layers[i],
                                    "target_layer": layers[j],
                                }
                            )
                            rels.append(
                                {
                                    "source": tgt,
                                    "target": src,
                                    "relation_type": "related_to",
                                    "strength": 0.6,
                                    "confidence": 0.6,
                                    "source_layer": layers[j],
                                    "target_layer": layers[i],
                                }
                            )
        return rels

    def _count_stale(self) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM cognitive_relations WHERE stale = 1"
            ).fetchone()
        return row[0] if row else 0

    def get_stats(self) -> Dict[str, int]:
        with self._conn() as conn:
            rel_total = conn.execute("SELECT COUNT(*) FROM cognitive_relations").fetchone()[0]
            rel_stale = conn.execute(
                "SELECT COUNT(*) FROM cognitive_relations WHERE stale = 1"
            ).fetchone()[0]
            node_count = conn.execute("SELECT COUNT(*) FROM canonical_nodes").fetchone()[0]
            outbox_pending = conn.execute(
                "SELECT COUNT(*) FROM sync_outbox WHERE processed_at IS NULL"
            ).fetchone()[0]
        return {
            "relations": rel_total,
            "relations_stale": rel_stale,
            "canonical_nodes": node_count,
            "outbox_pending": outbox_pending,
        }
