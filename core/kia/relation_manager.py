# -*- coding: utf-8 -*-
"""
RelationManager — 关系管理器

职责：
- 从蒸馏输出提取关系
- 发现隐式关系（关键词重叠 + 共现）
- 贝叶斯置信度更新
- 新增关系类型：CO_OCCURS, SEQUENTIAL, SIMILAR_TO
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Set

from core.config import get_config
from core.cognitive.decision_trace import (
    MaterialActionAuthorization,
    MaterialActionCoordinator,
    MaterialActionPermit,
    MaterialActionRequest,
    authorize_exact_project_contract_action,
    find_pending_material_action_authorization,
    require_material_action,
    resolve_material_action_recovery_authorization,
)
from core.cognitive.state_contract import sha256_json
from core.cognitive.material_effect_ledger import (
    SqliteTargetEffectOracle,
    record_target_effect,
    recorded_target_effect_command_ids,
    recover_pending_target_effects,
    recover_recorded_target_effect,
)
from core.cognitive.material_effect_schema import (
    initialize_material_effect_schema,
)
from core.cognitive.state_store import CognitiveStateStore
from core.db_utils import sqlite_conn
from core.kia.relation_endpoint_quality import (
    is_derived_kg_scan_path,
    relation_endpoint_rejection_reason,
)
from core.kia.relation_evidence_schema import (
    initialize_relation_evidence_schema,
    validate_existing_relation_evidence_schema,
)
from core.kia.relation_identity import relation_target_ref
from core.kia.relation_writer import upsert_relation_row
from .relation_schema import Relation, RelationType, RelationEvidence

logger = logging.getLogger(__name__)

KG_RELATION_ACTION = "upsert_relation"
KG_RELATION_OWNER = "knowledge_graph"
KG_RELATION_EXECUTOR = "relation_manager"
RELATION_MANAGER_DECISION_CONTRACT_ID = (
    "project-contract:relation-manager-upserts"
)
RELATION_MANAGER_DECISION_CONTRACT_REVISION = (
    "mnemos.relation_manager_upserts.v2"
)
RELATION_MANAGER_DECISION_CONTRACT_TEXT = (
    "RelationManager may upsert only an exact relation that passed the current "
    "endpoint, type, confidence, source, and evidence validation path."
)
RELATION_MANAGER_DECISION_PRODUCER_HASH = sha256_json(
    {
        "module": "core.kia.relation_manager",
        "producer": "RelationManager",
        "version": RELATION_MANAGER_DECISION_CONTRACT_REVISION,
    }
)


class RelationManagerEffectOracle(SqliteTargetEffectOracle):
    """Observe one committed RelationManager relation mutation."""

    owner = KG_RELATION_OWNER
    executor_id = KG_RELATION_EXECUTOR
    action_type = KG_RELATION_ACTION


def _get_db_path() -> Path:
    # 统一存储到 knowledge_graph.db，与 EntityManager / KnowledgeGraph 共享
    from core.config import get_config

    return Path(get_config().database_dir) / "knowledge_graph.db"


@dataclass
class RelationSuggestion:
    """关系建议（待确认）"""

    source: str
    target: str
    relation_type: str
    confidence: float
    reason: str = ""
    evidence_type: str = "auto_discover"


@dataclass
class _ImplicitRelationIndex:
    """单次隐式关系发现的 Wiki 只读索引。"""

    wiki_dir: Path
    page_text: Dict[Path, str]
    page_lower: Dict[Path, str]
    page_links: Dict[Path, Set[str]]
    all_entities: List[str]
    entity_pages: Dict[str, List[Path]]
    entity_keywords: Dict[str, Set[str]]


class RelationManager:
    """关系管理器"""

    def __init__(
        self,
        db_path: str | None = None,
        *,
        initialize: bool = True,
        material_action_resolver: Callable[
            [Mapping[str, str]], MaterialActionAuthorization
        ]
        | None = None,
    ):
        self._db_path = Path(db_path) if db_path else _get_db_path()
        self._material_action_resolver = material_action_resolver
        if initialize:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_db()

    @staticmethod
    def _relation_target_ref(relation: Relation) -> str:
        return relation_target_ref(relation)

    @staticmethod
    def _relation_material_action_metadata(relation: Relation) -> dict[str, Any]:
        """Return the canonical visible input for a manager-owned relation effect."""

        return {
            "source": relation.source,
            "target": relation.target,
            "relation_type": relation.relation_type.value,
            "confidence": relation.confidence,
            "context": relation.context,
            "evidence": [
                {
                    "evidence_type": evidence.evidence_type,
                    "content": evidence.content,
                }
                for evidence in (relation.evidence or [])
            ],
            "strength": relation.strength,
        }

    @classmethod
    def relation_material_action_binding(
        cls,
        relation: Relation,
        *,
        reason: str,
    ) -> dict[str, str]:
        """Bind one relation mutation to its exact content and reason."""

        from core.trust.formal_cognitive_mutation import (
            formal_cognitive_mutation_input_hash,
        )

        target_ref = cls._relation_target_ref(relation)
        metadata = cls._relation_material_action_metadata(relation)
        return {
            "target_ref": target_ref,
            "input_hash": formal_cognitive_mutation_input_hash(
                asset_kind="kg_relation",
                action=KG_RELATION_ACTION,
                target_ref=target_ref,
                actor=relation.source_method or "system",
                reason=reason,
                metadata=metadata,
            ),
        }

    def _resolve_material_action(
        self,
        binding: Mapping[str, str],
        command_ids: Mapping[str, str] | None,
        *,
        relation: Relation,
        reason: str,
    ) -> MaterialActionAuthorization:
        if self._material_action_resolver is not None:
            return self._material_action_resolver(binding)
        if isinstance(command_ids, Mapping):
            command_id = str(command_ids.get(binding["target_ref"]) or "").strip()
            if not command_id:
                raise PermissionError(
                    "knowledge graph relation lacks its exact material command"
                )
            return MaterialActionCoordinator(
                CognitiveStateStore(self._db_path.parent)
            ).bind_for_recovery(
                command_id,
                executor_id=KG_RELATION_EXECUTOR,
            )
        if reason not in {
            "relation_manager.add_from_distill",
            "relation_manager.apply_implicit_relations",
            "relation_manager.update_confidence",
        }:
            raise ValueError(f"unsupported relation material reason: {reason}")
        state_db_path = (
            self._db_path.parent / "producer_consumer_ledger.db"
        ).resolve(strict=False)
        request = MaterialActionRequest(
            owner=KG_RELATION_OWNER,
            executor_id=KG_RELATION_EXECUTOR,
            action_type=KG_RELATION_ACTION,
            target_ref=str(binding["target_ref"]),
            input_hash=str(binding["input_hash"]),
            expected_state_db=str(state_db_path),
        )
        pending = find_pending_material_action_authorization(
            state_db_path=state_db_path,
            owner=request.owner,
            executor_id=request.executor_id,
            action_type=request.action_type,
            target_ref=request.target_ref,
            input_hash=request.input_hash,
        )
        if pending is not None:
            return pending
        decision_created_at = datetime.now(timezone.utc).isoformat()
        return authorize_exact_project_contract_action(
            expected_request=request,
            state_db_path=state_db_path,
            contract_id=RELATION_MANAGER_DECISION_CONTRACT_ID,
            contract_revision_id=RELATION_MANAGER_DECISION_CONTRACT_REVISION,
            contract_text=RELATION_MANAGER_DECISION_CONTRACT_TEXT,
            source_namespace="relation-manager-upsert",
            source_facts={
                "schema_version": "mnemos.relation_manager_upsert_facts.v2",
                "decision_created_at": decision_created_at,
                "reason": reason,
                "relation": self._relation_effect_payload(relation),
                "database_path": str(self._db_path.resolve(strict=False)),
            },
            decision_checks={
                "supported_relation_reason": reason
                in {
                    "relation_manager.add_from_distill",
                    "relation_manager.apply_implicit_relations",
                    "relation_manager.update_confidence",
                },
                "relation_endpoints_valid": not self._invalid_relation_endpoint(
                    relation
                ),
            },
            evidence_refs=tuple(
                dict.fromkeys(
                    (
                        f"relation-source:{relation.source_method or 'system'}",
                        f"relation-context:{sha256_json(relation.context)}",
                        *(
                            f"{item.evidence_type}:{item.content}"
                            for item in (relation.evidence or [])
                            if item.content
                        ),
                    )
                )
            ),
            task=f"Upsert relation {binding['target_ref']}",
            goal="Persist only the exact relation accepted by RelationManager validation.",
            constraints=(
                "Both endpoints and the relation type must pass canonical validation.",
                "Confidence, strength, source method, context, and evidence cannot drift.",
            ),
            created_at=decision_created_at,
            producer="relation-manager",
            producer_version=RELATION_MANAGER_DECISION_CONTRACT_REVISION,
            producer_code_hash=RELATION_MANAGER_DECISION_PRODUCER_HASH,
            evaluator_id="relation-manager-upsert-evaluator",
            approved_candidate_key="upsert_exact_validated_relation",
            approved_candidate_summary=(
                "Upsert the exact relation that passed RelationManager validation."
            ),
            rejected_candidate_key="reject_invalid_or_drifted_relation",
            rejected_candidate_summary=(
                "Reject a relation with invalid endpoints, type, evidence, or drift."
            ),
            approved_reason_code="relation_manager_binding_verified",
            rejected_reason_code="relation_manager_binding_rejected",
            committed_metric="relation_manager_upsert_receipt",
            rejected_metric="invalid_relation_upsert_count",
        )

    @staticmethod
    def _relation_effect_hash(
        conn: sqlite3.Connection,
        relation: Relation,
    ) -> str:
        row = conn.execute(
            """SELECT * FROM relations
               WHERE source=? AND target=? AND relation_type=?""",
            (
                relation.source,
                relation.target,
                relation.relation_type.value,
            ),
        ).fetchone()
        evidence: list[dict[str, object]] = []
        if row is not None:
            columns = [str(item[0]) for item in conn.execute(
                "SELECT name FROM pragma_table_info('relations') ORDER BY cid"
            ).fetchall()]
            row_payload = dict(zip(columns, tuple(row)))
            evidence_columns = [str(item[0]) for item in conn.execute(
                "SELECT name FROM pragma_table_info('relation_evidence') ORDER BY cid"
            ).fetchall()]
            evidence = [
                dict(zip(evidence_columns, tuple(item)))
                for item in conn.execute(
                    """SELECT * FROM relation_evidence
                       WHERE relation_id=? ORDER BY id""",
                    (int(row_payload["id"]),),
                ).fetchall()
            ]
        else:
            row_payload = None
        return sha256_json({"relation": row_payload, "evidence": evidence})

    @staticmethod
    def _relation_effect_payload(relation: Relation) -> dict[str, Any]:
        return {
            "source": relation.source,
            "target": relation.target,
            "relation_type": relation.relation_type.value,
            "strength": float(relation.strength),
            "confidence": float(relation.confidence),
            "source_method": str(relation.source_method),
            "context": str(relation.context),
            "evidence": [
                {
                    "evidence_type": evidence.evidence_type,
                    "content": evidence.content,
                    "created_at": evidence.created_at,
                }
                for evidence in (relation.evidence or [])
            ],
        }

    def _relation_target_effect(
        self,
        command_id: str,
        *,
        schema_version: str,
    ) -> dict[str, Any]:
        with sqlite3.connect(
            f"file:{self._db_path.resolve(strict=True)}?mode=ro",
            uri=True,
        ) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM material_target_effects WHERE command_id=?",
                (str(command_id),),
            ).fetchone()
        if row is None:
            raise RuntimeError("relation target journal is missing")
        try:
            outcome = json.loads(str(row["outcome"] or ""))
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "relation target journal outcome is invalid"
            ) from exc
        if (
            not isinstance(outcome, dict)
            or outcome.get("schema_version") != schema_version
        ):
            raise RuntimeError(
                "relation target journal outcome is unsupported"
            )
        return {
            "before_hash": str(row["before_hash"]),
            "after_hash": str(row["after_hash"]),
            "outcome": outcome,
        }

    @staticmethod
    def _relation_from_effect_outcome(
        outcome: Mapping[str, Any],
    ) -> Relation:
        payload = outcome.get("relation")
        if not isinstance(payload, Mapping):
            raise RuntimeError("relation feedback outcome lacks relation payload")
        evidence_payload = payload.get("evidence", ())
        if not isinstance(evidence_payload, list):
            raise RuntimeError("relation feedback outcome evidence is invalid")
        evidence = []
        for item in evidence_payload:
            if not isinstance(item, Mapping):
                raise RuntimeError("relation feedback outcome evidence is invalid")
            evidence.append(
                RelationEvidence(
                    evidence_type=str(item.get("evidence_type") or ""),
                    content=str(item.get("content") or ""),
                    created_at=str(item.get("created_at") or ""),
                )
            )
        try:
            return Relation(
                source=str(payload["source"]),
                target=str(payload["target"]),
                relation_type=RelationType(str(payload["relation_type"])),
                strength=float(payload["strength"]),
                confidence=float(payload["confidence"]),
                source_method=str(payload["source_method"]),
                context=str(payload["context"]),
                evidence=evidence,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "relation feedback outcome relation is invalid"
            ) from exc

    def _init_db(self):
        """确保 relations 表存在（RelationManager 独立使用时）"""
        with sqlite_conn(str(self._db_path), timeout=5) as conn:
            validate_existing_relation_evidence_schema(conn)
            initialize_material_effect_schema(conn)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS relations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    target TEXT NOT NULL,
                    relation_type TEXT NOT NULL,
                    strength REAL DEFAULT 0.5,
                    confidence REAL DEFAULT 0.5,
                    source_method TEXT DEFAULT 'auto',
                    context TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(source, target, relation_type)
                )
            """)
            # Explicit bootstrap ensures the current relation context column.
            existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(relations)")}
            if "context" not in existing_cols:
                conn.execute("ALTER TABLE relations ADD COLUMN context TEXT DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rel_source ON relations(source)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rel_target ON relations(target)")

            initialize_relation_evidence_schema(conn)
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS relations_fts USING fts5(
                    content,
                    content_rowid=rowid
                )
            """)
            conn.commit()

    def add_from_distill(self, kg_input: Dict) -> List[Relation]:
        """从蒸馏输出提取关系并持久化到数据库

        kg_input 格式：
        {
            "entities": ["entity1", "entity2"],
            "relations": [
                {"source": "A", "target": "B", "type": "builds_on", "confidence": 0.8}
            ]
        }
        """
        relations = self.plan_distill_relations(kg_input)
        try:
            self._commit_relation_actions(
                relations,
                reason="relation_manager.add_from_distill",
                command_ids=kg_input.get("material_action_commands"),
            )
        except sqlite3.Error as e:
            logger.warning("RelationManager 持久化失败: %s", e, exc_info=True)
        return relations

    def plan_distill_relations(self, kg_input: Mapping[str, Any]) -> List[Relation]:
        """Derive the exact distillation relation objects without writing them."""

        relations: List[Relation] = []
        for rel_data in kg_input.get("relations", []):
            rel_type_str = rel_data.get("type", "references")
            try:
                rel_type = RelationType(rel_type_str)
            except ValueError:
                logger.warning("跳过未知蒸馏关系类型: %s", rel_type_str)
                continue
            relation = Relation(
                source=rel_data["source"],
                target=rel_data["target"],
                relation_type=rel_type,
                strength=rel_data.get("strength", 0.5),
                confidence=rel_data.get("confidence", 0.5),
                source_method="distill",
                evidence=[
                    RelationEvidence(
                        evidence_type="distill_extraction",
                        content=rel_data.get("reason", ""),
                    )
                ],
            )
            if not self._invalid_relation_endpoint(relation):
                relations.append(relation)
        return relations

    def apply_planned_relations(
        self,
        relations: List[Relation],
        *,
        reason: str,
    ) -> int:
        """Commit a relation set already frozen into upstream decision facts."""

        return self._commit_relation_actions(
            relations,
            reason=reason,
            command_ids=None,
        )

    # 单个实体隐式关系发现上限，防止 O(N²) 膨胀
    MAX_IMPLICIT_SUGGESTIONS_PER_ENTITY = 5
    ENTITY_MATCHER_CHUNK_SIZE = 500

    def discover_implicit_relations(
        self,
        entity_name: str,
        wiki_dir: Path | None = None,
        *,
        index: _ImplicitRelationIndex | None = None,
    ) -> List[RelationSuggestion]:
        """发现隐式关系

        策略：
        1. 关键词重叠 → SIMILAR_TO
        2. 共现关系 → CO_OCCURS
        3. 顺序关系 → SEQUENTIAL（A 出现在 B 之前）
        """
        if not entity_name:
            return []

        if not wiki_dir:
            wiki_dir = get_config().wiki_dir
        wiki_dir = Path(wiki_dir)

        if index is None:
            all_entities = self._get_all_entity_names()
            index = self._build_implicit_relation_index(wiki_dir, all_entities)

        return self._discover_implicit_relations_from_index(entity_name, index)

    def discover_implicit_relations_batch(
        self,
        entity_names: List[str],
        wiki_dir: Path | None = None,
    ) -> Dict[str, List[RelationSuggestion]]:
        """批量发现隐式关系。

        单次构建 Wiki 索引并在多个实体间复用，避免 N 个实体触发 N 次全库 Markdown
        扫描。索引只在本次调用内有效，因此新写入的 Markdown 会在下一次批处理被看到。
        """
        if not wiki_dir:
            wiki_dir = get_config().wiki_dir
        wiki_dir = Path(wiki_dir)

        unique_names: List[str] = []
        seen = set()
        for name in entity_names:
            if not name:
                continue
            normalized = str(name).strip()
            if not normalized:
                continue
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            unique_names.append(normalized)

        if not unique_names:
            return {}

        all_entities = list(dict.fromkeys([*self._get_all_entity_names(), *unique_names]))
        index = self._build_implicit_relation_index(wiki_dir, all_entities)
        return {
            name: self._discover_implicit_relations_from_index(name, index)
            for name in unique_names
        }

    def _discover_implicit_relations_from_index(
        self,
        entity_name: str,
        index: _ImplicitRelationIndex,
    ) -> List[RelationSuggestion]:
        suggestions = []  # type: ignore[var-annotated]

        # 搜索提及该实体的页面
        entity_pages = self._find_pages_mentioning_in_index(entity_name, index)
        if not entity_pages:
            return suggestions

        # 与其他实体的共现分析
        co_occurring = self._analyze_co_occurrence_in_index(entity_name, entity_pages, index)
        for other_entity, count in co_occurring.items():
            if other_entity == entity_name:
                continue
            if count >= 2:
                suggestions.append(
                    RelationSuggestion(
                        source=entity_name,
                        target=other_entity,
                        relation_type="co_occurs",
                        confidence=min(0.6, count * 0.15),
                        reason=f"在 {count} 个页面中同时出现",
                        evidence_type="co_occurrence",
                    )
                )

        # 关键词重叠分析
        entity_kw = self._extract_entity_keywords_in_index(entity_name, entity_pages, index)
        for other in index.all_entities:
            if other == entity_name:
                continue
            other_kw = self._extract_entity_keywords_in_index(
                other,
                self._find_pages_mentioning_in_index(other, index),
                index,
            )
            if entity_kw and other_kw:
                jaccard = (
                    len(entity_kw & other_kw) / len(entity_kw | other_kw)
                    if entity_kw | other_kw
                    else 0
                )
                if jaccard >= 0.3:
                    suggestions.append(
                        RelationSuggestion(
                            source=entity_name,
                            target=other,
                            relation_type="similar_to",
                            confidence=jaccard,
                            reason=f"关键词重叠度 {jaccard:.0%}",
                            evidence_type="keyword_overlap",
                        )
                    )

        # 按置信度排序并截断，防止单个实体产生过多建议
        suggestions = sorted(suggestions, key=lambda s: s.confidence, reverse=True)
        if len(suggestions) > self.MAX_IMPLICIT_SUGGESTIONS_PER_ENTITY:
            suggestions = suggestions[: self.MAX_IMPLICIT_SUGGESTIONS_PER_ENTITY]

        return suggestions

    def apply_implicit_relations(
        self,
        suggestions: List[RelationSuggestion],
        min_confidence: float = 0.5,
        *,
        material_action_commands: Mapping[str, str] | None = None,
    ) -> int:
        """将隐式关系建议持久化到数据库，返回成功写入的数量。"""
        if not suggestions:
            return 0
        relations = self.plan_implicit_relations(
            suggestions,
            min_confidence=min_confidence,
        )
        try:
            applied = self._commit_relation_actions(
                relations,
                reason="relation_manager.apply_implicit_relations",
                command_ids=material_action_commands,
            )
        except sqlite3.Error as e:
            logger.warning("持久化隐式关系失败: %s", e, exc_info=True)
            return 0
        return applied

    def plan_implicit_relations(
        self,
        suggestions: List[RelationSuggestion],
        *,
        min_confidence: float = 0.5,
    ) -> List[Relation]:
        """Derive eligible implicit relations without executing their effects."""

        relations: List[Relation] = []
        for suggestion in suggestions:
            if suggestion.confidence < min_confidence:
                continue
            try:
                rel_type = RelationType(suggestion.relation_type)
            except ValueError:
                logger.warning(
                    "跳过未知隐式关系类型: %s",
                    suggestion.relation_type,
                )
                continue
            relation = Relation(
                source=suggestion.source,
                target=suggestion.target,
                relation_type=rel_type,
                strength=suggestion.confidence,
                confidence=suggestion.confidence,
                source_method="implicit_discover",
                context=suggestion.reason,
            )
            if suggestion.reason:
                relation.evidence = [
                    RelationEvidence(
                        evidence_type=(
                            suggestion.evidence_type or "auto_discover"
                        ),
                        content=suggestion.reason,
                    )
                ]
            if not self._invalid_relation_endpoint(relation):
                relations.append(relation)
        return relations

    def update_confidence(
        self,
        source: str,
        target: str,
        relation_type: str,
        feedback: float,
        *,
        material_action: MaterialActionAuthorization | None = None,
    ) -> None:
        """贝叶斯置信度更新

        feedback: 0-1，1 表示关系确认，0 表示关系否定
        """
        with sqlite_conn(str(self._db_path), timeout=5) as conn:
            row = conn.execute(
                """SELECT * FROM relations
                   WHERE source=? AND target=? AND relation_type=?""",
                (source, target, relation_type),
            ).fetchone()
            if not row:
                return
            relation = Relation(
                source=source,
                target=target,
                relation_type=RelationType(relation_type),
                strength=float(row[4]),
                confidence=float(row[5]),
                source_method=str(row[6] or "feedback"),
                context=str(row[7] or ""),
            )
            old_conf = relation.confidence
            alpha = 0.2
            new_conf = alpha * feedback + (1 - alpha) * old_conf
            updated_relation = Relation(
                source=source,
                target=target,
                relation_type=relation.relation_type,
                strength=relation.strength,
                confidence=new_conf,
                source_method="suspect" if new_conf < 0.2 else relation.source_method,
                context=relation.context,
            )
            reason = "relation_manager.update_confidence"
            binding = self.relation_material_action_binding(
                updated_relation,
                reason=reason,
            )
            recover_pending_target_effects(
                state_db_path=self._db_path.parent / "producer_consumer_ledger.db",
                oracle=RelationManagerEffectOracle(self._db_path),
                target_ref=binding["target_ref"],
            )
            authorization = material_action or self._resolve_material_action(
                binding,
                None,
                relation=updated_relation,
                reason=reason,
            )
            recovery_input_hash = (
                authorization.permit.input_hash
                if isinstance(material_action, MaterialActionAuthorization)
                else binding["input_hash"]
            )
            authorization, _ = resolve_material_action_recovery_authorization(
                authorization,
                owner=KG_RELATION_OWNER,
                executor_id=KG_RELATION_EXECUTOR,
                action_type=KG_RELATION_ACTION,
                target_ref=binding["target_ref"],
                input_hash=recovery_input_hash,
                expected_state_db=(
                    self._db_path.parent / "producer_consumer_ledger.db"
                ),
            )
            if recover_recorded_target_effect(
                authorization,
                RelationManagerEffectOracle(self._db_path),
            ):
                replay = self._relation_target_effect(
                    authorization.permit.command_id,
                    schema_version="mnemos.relation_feedback_effect.v1",
                )
                request = replay["outcome"].get("request", {})
                expected_request = {
                    "source": source,
                    "target": target,
                    "relation_type": relation_type,
                    "feedback": float(feedback),
                }
                if request != expected_request:
                    raise PermissionError(
                        "terminal relation feedback command belongs to another request"
                    )
                replay_relation = self._relation_from_effect_outcome(
                    replay["outcome"]
                )
                self._close_relation_action(
                    relation=replay_relation,
                    reason=str(replay["outcome"]["reason"]),
                    relation_id=int(replay["outcome"]["relation_id"]),
                    authorization=authorization,
                    permit=authorization.permit,
                    before_hash=str(replay["before_hash"]),
                    after_hash=str(replay["after_hash"]),
                )
                return
            permit = require_material_action(
                authorization,
                owner=KG_RELATION_OWNER,
                executor_id=KG_RELATION_EXECUTOR,
                action_type=KG_RELATION_ACTION,
                target_ref=binding["target_ref"],
                input_hash=binding["input_hash"],
                expected_state_db=self._db_path.parent / "producer_consumer_ledger.db",
            )
            before_hash = self._relation_effect_hash(conn, relation)
            conn.execute(
                """UPDATE relations
                   SET confidence=?, source_method=?, updated_at=?
                   WHERE source=? AND target=? AND relation_type=?""",
                (
                    new_conf,
                    updated_relation.source_method,
                    datetime.now(timezone.utc).isoformat(),
                    source,
                    target,
                    relation_type,
                ),
            )
            after_hash = self._relation_effect_hash(conn, updated_relation)
            record_target_effect(
                conn,
                permit,
                status="committed",
                before_hash=before_hash,
                after_hash=after_hash,
                evidence_refs=(
                    f"target-after:{after_hash}",
                    f"target-journal:knowledge-graph:{int(row[0])}:{after_hash}",
                ),
                outcome=json.dumps(
                    {
                        "schema_version": "mnemos.relation_feedback_effect.v1",
                        "request": {
                            "source": source,
                            "target": target,
                            "relation_type": relation_type,
                            "feedback": float(feedback),
                        },
                        "reason": reason,
                        "relation_id": int(row[0]),
                        "relation": self._relation_effect_payload(
                            updated_relation
                        ),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                observed_at=datetime.now(timezone.utc).isoformat(),
            )
            conn.commit()
        self._close_relation_action(
            relation=updated_relation,
            reason=reason,
            relation_id=int(row[0]),
            authorization=authorization,
            permit=permit,
            before_hash=before_hash,
            after_hash=after_hash,
        )

    # ---- 内部方法 ----

    @staticmethod
    def _invalid_relation_endpoint(relation: Relation) -> bool:
        source_reason = relation_endpoint_rejection_reason(relation.source)
        target_reason = relation_endpoint_rejection_reason(relation.target)
        if not source_reason and not target_reason:
            return False
        logger.warning(
            "跳过非法关系 endpoint: source=%s target=%s",
            source_reason or "ok",
            target_reason or "ok",
        )
        return True

    def _commit_relation_actions(
        self,
        relations: List[Relation],
        *,
        reason: str,
        command_ids: Mapping[str, str] | None,
    ) -> int:
        recover_pending_target_effects(
            state_db_path=self._db_path.parent / "producer_consumer_ledger.db",
            oracle=RelationManagerEffectOracle(self._db_path),
        )
        if not relations:
            return 0
        with sqlite_conn(str(self._db_path), timeout=5) as conn:
            plans = self._authorized_relation_plans(
                conn,
                relations,
                reason=reason,
                command_ids=command_ids,
            )
            effects: list[
                tuple[
                    int,
                    Relation,
                    MaterialActionAuthorization,
                    MaterialActionPermit,
                    str,
                    str,
                    bool,
                ]
            ] = []
            for relation, authorization, permit, before_hash in plans:
                require_material_action(
                    authorization,
                    owner=KG_RELATION_OWNER,
                    executor_id=KG_RELATION_EXECUTOR,
                    action_type=KG_RELATION_ACTION,
                    target_ref=permit.target_ref,
                    input_hash=permit.input_hash,
                    expected_state_db=(
                        self._db_path.parent / "producer_consumer_ledger.db"
                    ),
                )
                rel_id, _, changed = upsert_relation_row(
                    conn,
                    relation,
                    source=relation.source,
                    target=relation.target,
                    insert_evidence=True,
                )
                after_hash = self._relation_effect_hash(conn, relation)
                observed_at = datetime.now(timezone.utc).isoformat()
                record_target_effect(
                    conn,
                    permit,
                    status="committed",
                    before_hash=before_hash,
                    after_hash=after_hash,
                    evidence_refs=(
                        f"target-after:{after_hash}",
                        f"target-journal:knowledge-graph:{rel_id}:{after_hash}",
                    ),
                    outcome=json.dumps(
                        {
                            "schema_version": "mnemos.relation_upsert_effect.v1",
                            "reason": reason,
                            "relation_id": rel_id,
                            "relation": self._relation_effect_payload(relation),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    observed_at=observed_at,
                )
                effects.append(
                    (
                        rel_id,
                        relation,
                        authorization,
                        permit,
                        before_hash,
                        after_hash,
                        changed,
                    )
                )
            conn.commit()
        for (
            rel_id,
            relation,
            authorization,
            permit,
            before_hash,
            after_hash,
            _changed,
        ) in effects:
            self._close_relation_action(
                relation=relation,
                reason=reason,
                relation_id=rel_id,
                authorization=authorization,
                permit=permit,
                before_hash=before_hash,
                after_hash=after_hash,
            )
        return sum(1 for effect in effects if effect[-1])

    def _authorized_relation_plans(
        self,
        conn: sqlite3.Connection,
        relations: List[Relation],
        *,
        reason: str,
        command_ids: Mapping[str, str] | None,
    ) -> list[
        tuple[
            Relation,
            MaterialActionAuthorization,
            MaterialActionPermit,
            str,
        ]
    ]:
        """Return only plans whose exact effects have canonical permits."""

        plans: list[
            tuple[
                Relation,
                MaterialActionAuthorization,
                MaterialActionPermit,
                str,
            ]
        ] = []
        for relation in relations:
            binding = self.relation_material_action_binding(
                relation,
                reason=reason,
            )
            oracle = RelationManagerEffectOracle(self._db_path)
            if (
                command_ids is None
                and self._material_action_resolver is None
            ):
                current_hash = self._relation_effect_hash(conn, relation)
                for command_id in recorded_target_effect_command_ids(
                    oracle,
                    target_ref=binding["target_ref"],
                    input_hash=binding["input_hash"],
                ):
                    replay_authorization = MaterialActionCoordinator(
                        CognitiveStateStore(
                            self._db_path.parent
                            / "producer_consumer_ledger.db"
                        )
                    ).bind_for_recovery(
                        command_id,
                        executor_id=KG_RELATION_EXECUTOR,
                    )
                    replay_authorization, _ = (
                        resolve_material_action_recovery_authorization(
                            replay_authorization,
                            owner=KG_RELATION_OWNER,
                            executor_id=KG_RELATION_EXECUTOR,
                            action_type=KG_RELATION_ACTION,
                            target_ref=binding["target_ref"],
                            input_hash=binding["input_hash"],
                            expected_state_db=(
                                self._db_path.parent
                                / "producer_consumer_ledger.db"
                            ),
                        )
                    )
                    if not recover_recorded_target_effect(
                        replay_authorization,
                        oracle,
                    ):
                        continue
                    replay = self._relation_target_effect(
                        command_id,
                        schema_version="mnemos.relation_upsert_effect.v1",
                    )
                    if (
                        replay["outcome"].get("reason") != reason
                        or replay["outcome"].get("relation")
                        != self._relation_effect_payload(relation)
                    ):
                        raise PermissionError(
                            "recorded relation command conflicts with its exact binding"
                        )
                    if str(replay["after_hash"]) != current_hash:
                        continue
                    replay_relation = self._relation_from_effect_outcome(
                        replay["outcome"]
                    )
                    self._close_relation_action(
                        relation=replay_relation,
                        reason=reason,
                        relation_id=int(replay["outcome"]["relation_id"]),
                        authorization=replay_authorization,
                        permit=replay_authorization.permit,
                        before_hash=str(replay["before_hash"]),
                        after_hash=str(replay["after_hash"]),
                    )
                    break
                else:
                    replay_authorization = None
                if replay_authorization is not None:
                    continue
            authorization = self._resolve_material_action(
                binding,
                command_ids,
                relation=relation,
                reason=reason,
            )
            authorization, _ = resolve_material_action_recovery_authorization(
                authorization,
                owner=KG_RELATION_OWNER,
                executor_id=KG_RELATION_EXECUTOR,
                action_type=KG_RELATION_ACTION,
                target_ref=binding["target_ref"],
                input_hash=binding["input_hash"],
                expected_state_db=(
                    self._db_path.parent / "producer_consumer_ledger.db"
                ),
            )
            if recover_recorded_target_effect(
                authorization,
                oracle,
            ):
                replay = self._relation_target_effect(
                    authorization.permit.command_id,
                    schema_version="mnemos.relation_upsert_effect.v1",
                )
                if (
                    replay["outcome"].get("reason") != reason
                    or replay["outcome"].get("relation")
                    != self._relation_effect_payload(relation)
                ):
                    raise PermissionError(
                        "terminal relation command belongs to another exact relation"
                    )
                replay_relation = self._relation_from_effect_outcome(
                    replay["outcome"]
                )
                self._close_relation_action(
                    relation=replay_relation,
                    reason=str(replay["outcome"]["reason"]),
                    relation_id=int(replay["outcome"]["relation_id"]),
                    authorization=authorization,
                    permit=authorization.permit,
                    before_hash=str(replay["before_hash"]),
                    after_hash=str(replay["after_hash"]),
                )
                continue
            permit = require_material_action(
                authorization,
                owner=KG_RELATION_OWNER,
                executor_id=KG_RELATION_EXECUTOR,
                action_type=KG_RELATION_ACTION,
                target_ref=binding["target_ref"],
                input_hash=binding["input_hash"],
                expected_state_db=(
                    self._db_path.parent / "producer_consumer_ledger.db"
                ),
            )
            plans.append(
                (
                    relation,
                    authorization,
                    permit,
                    self._relation_effect_hash(conn, relation),
                )
            )
        return plans

    def _close_relation_action(
        self,
        *,
        relation: Relation,
        reason: str,
        relation_id: int,
        authorization: MaterialActionAuthorization,
        permit: MaterialActionPermit,
        before_hash: str,
        after_hash: str,
    ) -> None:
        from core.trust.formal_cognitive_mutation import FormalCognitiveMutationJournal
        reciprocal_refs = [
            f"material-command:{permit.command_id}",
            f"decision-revision:{permit.decision_revision_id}",
            f"material-effect:{permit.effect_id}",
            *[
                f"{ev.evidence_type}:{ev.content}"
                for ev in (relation.evidence or [])
                if ev.content
            ],
        ]
        if not recover_recorded_target_effect(
            authorization,
            RelationManagerEffectOracle(self._db_path),
        ):
            raise RuntimeError("relation manager effect journal was not recoverable")
        FormalCognitiveMutationJournal.for_database(self._db_path).record(
            asset_kind="kg_relation",
            action=KG_RELATION_ACTION,
            target_ref=self._relation_target_ref(relation),
            actor=relation.source_method or "system",
            decision=permit.decision_revision_id,
            reason=reason,
            evidence_refs=reciprocal_refs,
            metadata=self._relation_material_action_metadata(relation),
            material_action=authorization,
        )

    def _build_implicit_relation_index(
        self,
        wiki_dir: Path,
        all_entities: List[str],
    ) -> _ImplicitRelationIndex:
        """构建单次批处理可复用的 Markdown 索引。"""
        wiki_dir = Path(wiki_dir)
        page_text: Dict[Path, str] = {}
        page_lower: Dict[Path, str] = {}
        page_links: Dict[Path, Set[str]] = {}
        for subdir in ["00-Inbox", "03-Tech", "04-Concepts"]:
            md_dir = wiki_dir / subdir
            if not md_dir.exists():
                continue
            for md_file in md_dir.glob("*.md"):
                if is_derived_kg_scan_path(md_file, wiki_dir):
                    continue
                try:
                    content = md_file.read_text(encoding="utf-8")
                except (OSError, IOError):
                    logging.getLogger(__name__).warning(
                        "Caught unexpected error at relation_manager.py", exc_info=True
                    )
                    continue
                page_text[md_file] = content
                page_lower[md_file] = content.lower()
                page_links[md_file] = {
                    link.strip() for link in re.findall(r"\[\[([^\]|]+)", content) if link.strip()
                }

        return _ImplicitRelationIndex(
            wiki_dir=wiki_dir,
            page_text=page_text,
            page_lower=page_lower,
            page_links=page_links,
            all_entities=all_entities,
            entity_pages=self._precompute_entity_pages(page_lower, all_entities),
            entity_keywords={},
        )

    def _precompute_entity_pages(
        self,
        page_lower: Dict[Path, str],
        all_entities: List[str],
    ) -> Dict[str, List[Path]]:
        """一次性预计算 entity->pages，避免查找阶段逐实体扫描所有页面。"""
        entity_keys = []
        seen = set()
        for entity in all_entities:
            key = str(entity).strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            entity_keys.append(key)

        entity_pages: Dict[str, List[Path]] = {key: [] for key in entity_keys}
        if not entity_keys or not page_lower:
            return entity_pages

        # 使用 lookahead 保留旧 substring 语义，同时支持 Redis / Redis cache 这类重叠命中。
        for start in range(0, len(entity_keys), self.ENTITY_MATCHER_CHUNK_SIZE):
            chunk = entity_keys[start : start + self.ENTITY_MATCHER_CHUNK_SIZE]
            pattern = re.compile(
                "(?=(" + "|".join(re.escape(key) for key in sorted(chunk, key=len, reverse=True)) + "))"
            )
            for page, content_lower in page_lower.items():
                seen_on_page = set()
                for match in pattern.finditer(content_lower):
                    key = match.group(1)
                    if key in seen_on_page:
                        continue
                    seen_on_page.add(key)
                    entity_pages[key].append(page)
        return entity_pages

    def _find_pages_mentioning_in_index(
        self,
        entity: str,
        index: _ImplicitRelationIndex,
    ) -> List[Path]:
        cache_key = entity.lower()
        if cache_key not in index.entity_pages:
            index.entity_pages[cache_key] = [
                page
                for page, content_lower in index.page_lower.items()
                if cache_key in content_lower
            ]
        return index.entity_pages[cache_key]

    def _analyze_co_occurrence_in_index(
        self,
        entity: str,
        pages: List[Path],
        index: _ImplicitRelationIndex,
    ) -> Dict[str, int]:
        co_occurrence: Dict[str, int] = {}
        entity_lower = entity.lower()
        for page in pages:
            for link in index.page_links.get(page, set()):
                if link.lower() != entity_lower:
                    co_occurrence[link] = co_occurrence.get(link, 0) + 1
        return co_occurrence

    def _extract_entity_keywords_in_index(
        self,
        entity: str,
        pages: List[Path],
        index: _ImplicitRelationIndex,
    ) -> Set[str]:
        cache_key = entity.lower()
        if cache_key in index.entity_keywords:
            return index.entity_keywords[cache_key]

        keywords = set()  # type: ignore[var-annotated]
        for page in pages[:5]:
            try:
                content = index.page_text.get(page, "")[:2000]
                fm = self._parse_frontmatter(content)
                kw = fm.get("关键词", {})
                if isinstance(kw, dict):
                    for layer_words in kw.values():
                        if isinstance(layer_words, list):
                            keywords.update(w.lower() for w in layer_words if isinstance(w, str))
                elif isinstance(kw, list):
                    keywords.update(w.lower() for w in kw if isinstance(w, str))
            except (
                ValueError,
                TypeError,
                KeyError,
                ImportError,
                AttributeError,
                RuntimeError,
                sqlite3.Error,
            ):
                logging.getLogger(__name__).warning(
                    "Caught unexpected error at relation_manager.py", exc_info=True
                )
                continue
        index.entity_keywords[cache_key] = keywords
        return keywords

    def _get_all_entity_names(self) -> List[str]:
        """获取所有实体名称"""
        if not self._db_path.is_file():
            return []
        try:
            with sqlite3.connect(
                f"file:{self._db_path.resolve(strict=True)}?mode=ro",
                uri=True,
            ) as conn:
                table = conn.execute(
                    """SELECT 1 FROM sqlite_master
                       WHERE type='table' AND name='entities'"""
                ).fetchone()
                if table is None:
                    return []
                rows = conn.execute(
                    "SELECT name FROM entities WHERE status='active' ORDER BY name"
                ).fetchall()
            return [str(row[0]) for row in rows if str(row[0] or "")]
        except sqlite3.Error:
            logging.getLogger(__name__).warning(
                "Unable to read relation-planning entities",
                exc_info=True,
            )
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
                        logger.warning(
                            "[relation_manager] json.JSONDecodeError suppressed", exc_info=True
                        )
                fm[key] = val
        return fm
