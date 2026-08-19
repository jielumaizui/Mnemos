# -*- coding: utf-8 -*-
from __future__ import annotations

"""
Knowledge Graph Manager - 知识图谱管理器

基于 SQLite 存储 Wiki 页面之间的语义关系，支持：
- CRUD 操作（自动维护对称关系的双向一致性）
- 自动关系发现（关键词重叠、链接解析、反模式关联）
- 路径查找（A 到 B 的知识路径）
- 冲突检测（矛盾关系环）
- 导出 Obsidian 格式（Mermaid / Dataview）

设计原则：
- 与蒸馏流程解耦，后置增强
- 支持增量更新，新页面入库时自动发现关系
- 关系带置信度，低置信度关系可人工审核
"""

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Tuple,
    TYPE_CHECKING,
)

from core.cognitive.decision_trace import (
    MaterialActionAuthorization,
    MaterialActionCoordinator,
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
from core.config import get_config
from core.db_utils import sqlite_conn
from core.utils import LazyPath

from .relation_schema import (
    Relation,
    RelationType,
    RELATION_META,
)
from .relation_identity import relation_target_ref
from .knowledge_graph_discovery import (  # noqa: F401
    KnowledgeGraphDiscoveryMixin,
    KnowledgePath,
    PathNode,
)
from .knowledge_graph_projection import KnowledgeGraphProjectionMixin
from .knowledge_graph_search import (  # noqa: F401
    HIDDEN_RELATION_STRATEGY_ERRORS,
    KnowledgeGraphSearchMixin,
)

if TYPE_CHECKING:
    from core.embeddings.relation_manager import RelationEmbeddingManager

logger = logging.getLogger(__name__)

# 模块级路径常量：首次访问时才解析
DB_PATH = LazyPath("database_dir", "knowledge_graph.db")
WIKI_DIR = LazyPath("wiki_dir")
KG_GRAPH_RELATION_ACTION = "upsert_relation"
KG_GRAPH_RELATION_OWNER = "knowledge_graph"
KG_GRAPH_RELATION_EXECUTOR = "knowledge_graph"
KNOWLEDGE_GRAPH_DECISION_CONTRACT_ID = "project-contract:knowledge-graph-relation-upserts"
KNOWLEDGE_GRAPH_DECISION_CONTRACT_REVISION = "mnemos.knowledge_graph_relation_upserts.v2"
KNOWLEDGE_GRAPH_DECISION_CONTRACT_TEXT = (
    "KnowledgeGraph may upsert only the exact relation that passed the current "
    "endpoint, type, confidence, source, symmetry, context, and evidence "
    "validation path."
)
KNOWLEDGE_GRAPH_DECISION_PRODUCER_HASH = sha256_json(
    {
        "module": "core.kia.knowledge_graph",
        "producer": "KnowledgeGraph.add_relation",
        "version": KNOWLEDGE_GRAPH_DECISION_CONTRACT_REVISION,
    }
)


class KnowledgeGraphRelationEffectOracle(SqliteTargetEffectOracle):
    """Observe one committed KnowledgeGraph relation upsert."""

    owner = KG_GRAPH_RELATION_OWNER
    executor_id = KG_GRAPH_RELATION_EXECUTOR
    action_type = KG_GRAPH_RELATION_ACTION


def knowledge_graph_relation_target_ref(relation: Relation) -> str:
    """Return the opaque stable identity of one logical relation row."""

    return relation_target_ref(relation)


def knowledge_graph_relation_material_action_metadata(
    relation: Relation,
) -> dict[str, Any]:
    """Return the canonical visible input bound to one relation effect."""

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
        "reverse_type": relation.reverse_type if relation.is_symmetric else "",
        "strength": relation.strength,
        "symmetric": relation.is_symmetric,
    }


def knowledge_graph_relation_material_action_binding(
    relation: Relation,
) -> dict[str, str]:
    """Bind one KnowledgeGraph relation to its full semantic input."""

    from core.trust.formal_cognitive_mutation import (
        formal_cognitive_mutation_input_hash,
    )

    # Authorization identities are immutable ledger keys, not display text.
    # Hash the canonical row identity so a page title containing credentials or
    # PII can never become an unredactable target_ref.  The visible endpoints
    # remain bound below as redaction-aware input metadata.
    target_ref = knowledge_graph_relation_target_ref(relation)
    metadata = knowledge_graph_relation_material_action_metadata(relation)
    return {
        "target_ref": target_ref,
        "input_hash": formal_cognitive_mutation_input_hash(
            asset_kind="kg_relation",
            action=KG_GRAPH_RELATION_ACTION,
            target_ref=target_ref,
            actor=relation.source_method or "system",
            reason="knowledge_graph.add_relation",
            metadata=metadata,
        ),
    }


# ========== 数据库 Schema ==========

DB_SCHEMA = """
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
);

CREATE INDEX IF NOT EXISTS idx_rel_source ON relations(source);
CREATE INDEX IF NOT EXISTS idx_rel_target ON relations(target);
CREATE INDEX IF NOT EXISTS idx_rel_type ON relations(relation_type);
CREATE INDEX IF NOT EXISTS idx_rel_confidence ON relations(confidence);

CREATE TABLE IF NOT EXISTS relation_stats (
    node TEXT PRIMARY KEY,
    in_degree INTEGER DEFAULT 0,
    out_degree INTEGER DEFAULT 0,
    hub_score REAL DEFAULT 0.0,
    last_calculated TEXT
);

-- FTS5 全文索引（加速关系搜索）
CREATE VIRTUAL TABLE IF NOT EXISTS relations_fts USING fts5(
    content,
    content_rowid=rowid
);
"""


class KnowledgeGraph(
    KnowledgeGraphSearchMixin,
    KnowledgeGraphDiscoveryMixin,
    KnowledgeGraphProjectionMixin,
):
    """知识图谱门面 — 委托给 EntityManager + RelationManager

    保留原有 CRUD + 发现 + 路径 + 导出接口，
    新增：entity_manager / relation_manager 子系统。
    """

    def __init__(
        self,
        db_path: str | None = None,
        wiki_base: str | None = None,
        embedding_index_dir: str | None = None,
        initialize: bool = True,
        read_only: bool = False,
        embedding_client: Any | None = None,
        config: object | None = None,
        material_action_resolver: (
            Callable[[Mapping[str, str]], MaterialActionAuthorization] | None
        ) = None,
    ):
        cfg = config or get_config()
        configured_wiki_dir = getattr(cfg, "wiki_dir", None)
        if configured_wiki_dir is None:
            raise TypeError("KnowledgeGraph config must provide wiki_dir")
        configured_wiki_path = Path(configured_wiki_dir).expanduser()
        self._runtime_config = cfg
        self._embedding_client = embedding_client
        self._material_action_resolver = material_action_resolver
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else configured_wiki_path
        custom_wiki = bool(wiki_base) and self.wiki_base.resolve(strict=False) != Path(
            configured_wiki_path
        ).expanduser().resolve(strict=False)
        local_state = self.wiki_base / ".kg"
        self.db_path = (
            Path(db_path)
            if db_path
            else (local_state / "knowledge_graph.db" if custom_wiki else Path(DB_PATH))
        )
        self.embedding_index_dir: Path | None = (
            Path(embedding_index_dir).expanduser()
            if embedding_index_dir
            else local_state / "embedding_index" if custom_wiki else None
        )
        self.read_only = read_only
        if initialize:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_db()
        self._entity_manager = None
        self._relation_manager = None
        self._deferred_relation_embeddings: Dict[int, Tuple[str, bool]] | None = None

    @property
    def entity_manager(self):
        if self._entity_manager is None:
            from .entity_manager import EntityManager

            self._entity_manager = EntityManager(
                db_path=self.db_path,
                initialize=not self.read_only,
                read_only=self.read_only,
            )
        return self._entity_manager

    @property
    def projection_ledger_dir(self) -> Path:
        """State directory for projection-consumption receipts."""

        return self.db_path.parent

    @property
    def relation_manager(self):
        if self._relation_manager is None:
            from .relation_manager import RelationManager

            self._relation_manager = RelationManager(str(self.db_path))
        return self._relation_manager

    @property
    def _rel_emb_mgr(self) -> "RelationEmbeddingManager":
        """RelationEmbeddingManager（延迟初始化）"""
        if not hasattr(self, "_rel_emb_mgr_instance"):
            from core.embeddings.relation_manager import RelationEmbeddingManager

            self._rel_emb_mgr_instance = RelationEmbeddingManager(
                db_path=self.db_path,
                index_dir=self.embedding_index_dir,
                client=self._embedding_client,
                config=self._runtime_config,
            )
        return self._rel_emb_mgr_instance

    def close(self) -> None:
        """Flush and release lazily-created projection resources."""

        manager = getattr(self, "_rel_emb_mgr_instance", None)
        if manager is not None:
            manager.close()

    def export_to_vault(self, vault_dir: str | None = None) -> Dict[str, int]:
        """Project the graph into the target Obsidian vault."""

        from .kg_exporter import KGExporter

        target = vault_dir or str(self.wiki_base)
        return KGExporter(target, kg=self).export_to_vault()

    def _init_db(self):
        """初始化数据库"""
        from .relation_evidence_schema import (
            initialize_relation_evidence_schema,
            validate_existing_relation_evidence_schema,
        )

        with sqlite_conn(str(self.db_path), timeout=10) as conn:
            validate_existing_relation_evidence_schema(conn)
            initialize_material_effect_schema(conn)
            conn.executescript(DB_SCHEMA)
            initialize_relation_evidence_schema(conn)
            conn.commit()

    @contextmanager
    def _conn(self):
        """获取数据库连接"""
        database = f"file:{self.db_path}?mode=ro" if self.read_only else str(self.db_path)
        with sqlite_conn(
            database,
            timeout=10,
            uri=self.read_only,
            wal=not self.read_only,
        ) as conn:
            conn.row_factory = sqlite3.Row  # noqa
            yield conn

    # ========== CRUD ==========

    def _build_relation_context(self, relation: Relation) -> str:
        """为关系生成富文本上下文，用于 embedding"""
        parts = []
        rel_desc = RELATION_META.get(relation.relation_type, {}).get(
            "description", relation.relation_type.value
        )
        # 用自然语言描述关系，避免模板句
        parts.append(f"{relation.source} 与 {relation.target}: {rel_desc}")

        # 融入证据片段
        if relation.evidence:
            for ev in relation.evidence[:2]:
                if ev.content:
                    snippet = ev.content[:200].replace("\n", " ")
                    parts.append(f"证据: {snippet}")

        # 融入置信度信息（低置信度关系会自然被区分）
        if relation.confidence > 0:
            parts.append(f"置信度 {relation.confidence:.0%}")

        return "；".join(parts)

    def _sync_relation_embedding(self, rel_id: int, context: str) -> bool:
        """将关系上下文同步到向量索引（软失败：不阻断主流程）"""
        # RelationEmbeddingManager owns expected provider/storage degradation.
        # Unknown implementation faults must remain visible to the caller.
        return self._rel_emb_mgr.add_relation_context(rel_id, context)  # type: ignore[no-any-return]  # noqa: E501

    @staticmethod
    def _relation_rows_payload(
        conn: sqlite3.Connection,
        relation: Relation,
    ) -> dict[str, Any]:
        endpoints = [(relation.source, relation.target)]
        if relation.is_symmetric and relation.source != relation.target:
            endpoints.append((relation.target, relation.source))
        rows: list[dict[str, Any]] = []
        for source, target in endpoints:
            row = conn.execute(
                """SELECT * FROM relations
                   WHERE source=? AND target=? AND relation_type=?""",
                (source, target, relation.relation_type.value),
            ).fetchone()
            if row is None:
                rows.append({"source": source, "target": target, "relation": None, "evidence": []})
                continue
            relation_payload = dict(row)
            evidence = [
                dict(item)
                for item in conn.execute(
                    """SELECT * FROM relation_evidence
                       WHERE relation_id=? ORDER BY id""",
                    (int(relation_payload["id"]),),
                ).fetchall()
            ]
            rows.append(
                {
                    "source": source,
                    "target": target,
                    "relation": relation_payload,
                    "evidence": evidence,
                }
            )
        return {"relations": rows}

    @classmethod
    def _relation_effect_hash(
        cls,
        conn: sqlite3.Connection,
        relation: Relation,
    ) -> str:
        return sha256_json(cls._relation_rows_payload(conn, relation))

    @staticmethod
    def _relation_is_exact_noop(
        conn: sqlite3.Connection,
        relation: Relation,
    ) -> bool:
        forward = conn.execute(
            """SELECT id, strength, confidence, source_method, context
               FROM relations WHERE source=? AND target=? AND relation_type=?""",
            (
                relation.source,
                relation.target,
                relation.relation_type.value,
            ),
        ).fetchone()
        if forward is None:
            return False
        desired_evidence = [
            (str(item.evidence_type), str(item.content)) for item in (relation.evidence or [])
        ]
        current_evidence = [
            (str(item[0]), str(item[1]))
            for item in conn.execute(
                """SELECT evidence_type, content FROM relation_evidence
                   WHERE relation_id=? ORDER BY id""",
                (int(forward[0]),),
            ).fetchall()
        ]
        if not (
            float(forward[1]) == float(relation.strength)
            and float(forward[2]) == float(relation.confidence)
            and str(forward[3] or "") == str(relation.source_method or "")
            and str(forward[4] or "") == str(relation.context or "")
            and current_evidence == desired_evidence
        ):
            return False
        if not relation.is_symmetric or relation.source == relation.target:
            return True
        return (
            conn.execute(
                """SELECT 1 FROM relations
                   WHERE source=? AND target=? AND relation_type=?""",
                (
                    relation.target,
                    relation.source,
                    relation.relation_type.value,
                ),
            ).fetchone()
            is not None
        )

    def add_relation(
        self,
        relation: Relation,
        *,
        material_action: MaterialActionAuthorization | None = None,
    ) -> bool:
        """
        添加关系

        自动处理对称关系：如果关系是对称的，同时添加反向关系。
        同时自动生成富文本 context 并同步到 relation embedding 索引。
        """
        from core.kia.relation_endpoint_quality import relation_endpoint_rejection_reason

        source_reason = relation_endpoint_rejection_reason(relation.source)
        target_reason = relation_endpoint_rejection_reason(relation.target)
        if source_reason or target_reason:
            logger.warning(
                "[KG] 拒绝非法关系端点 source=%r(%s) target=%r(%s)",
                relation.source,
                source_reason or "ok",
                relation.target,
                target_reason or "ok",
            )
            return False

        # 自动生成 context（若为空）
        if not relation.context or not relation.context.strip():
            relation.context = self._build_relation_context(relation)

        binding = knowledge_graph_relation_material_action_binding(relation)
        oracle = KnowledgeGraphRelationEffectOracle(self.db_path)
        recorded_commands = recorded_target_effect_command_ids(
            oracle,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
        )
        recover_pending_target_effects(
            state_db_path=self.db_path.parent / "producer_consumer_ledger.db",
            oracle=oracle,
            target_ref=binding["target_ref"],
        )
        with self._conn() as conn:
            exact_noop = self._relation_is_exact_noop(conn, relation)

        def project_relation(
            authorization: MaterialActionAuthorization,
        ) -> None:
            """Publish the relation commit receipt after target verification."""

            with self._conn() as conn:
                effect = conn.execute(
                    "SELECT outcome FROM material_target_effects WHERE command_id=?",
                    (authorization.permit.command_id,),
                ).fetchone()
            if effect is None:
                raise RuntimeError("KG relation target journal is missing")
            try:
                outcome = json.loads(str(effect[0] or ""))
            except json.JSONDecodeError as exc:
                raise RuntimeError("KG relation target outcome is invalid") from exc
            if (
                not isinstance(outcome, Mapping)
                or outcome.get("schema_version") != "mnemos.knowledge_graph_relation_effect.v1"
            ):
                raise RuntimeError("KG relation target outcome is unsupported")
            from core.ops.cognitive_pipeline_receipts import record_kg_relation_commit

            record_kg_relation_commit(
                self.db_path,
                relation,
                int(outcome["relation_id"]),
                (
                    int(outcome["reverse_relation_id"])
                    if outcome.get("reverse_relation_id") is not None
                    else None
                ),
                material_action=authorization,
                mutation_metadata=(
                    knowledge_graph_relation_material_action_metadata(relation)
                ),
            )

        if exact_noop:
            replay_authorizations: list[MaterialActionAuthorization] = []
            if material_action is not None:
                replay_authorizations.append(material_action)
            else:
                coordinator = MaterialActionCoordinator(
                    CognitiveStateStore(self.db_path.parent / "producer_consumer_ledger.db")
                )
                replay_authorizations.extend(
                    coordinator.bind_for_recovery(
                        command_id,
                        executor_id=KG_GRAPH_RELATION_EXECUTOR,
                    )
                    for command_id in recorded_commands
                )
            for replay_authorization in replay_authorizations:
                replay_authorization, _ = resolve_material_action_recovery_authorization(
                    replay_authorization,
                    owner=KG_GRAPH_RELATION_OWNER,
                    executor_id=KG_GRAPH_RELATION_EXECUTOR,
                    action_type=KG_GRAPH_RELATION_ACTION,
                    target_ref=binding["target_ref"],
                    input_hash=binding["input_hash"],
                    expected_state_db=(self.db_path.parent / "producer_consumer_ledger.db"),
                )
                if not recover_recorded_target_effect(
                    replay_authorization,
                    oracle,
                ):
                    raise RuntimeError("exact-noop KG command has no committed target effect")
                project_relation(replay_authorization)
            self._schedule_missing_relation_embeddings(relation)
            return True

        if material_action is None and self._material_action_resolver is not None:
            material_action = self._material_action_resolver(binding)
        state_db_path = (self.db_path.parent / "producer_consumer_ledger.db").resolve(strict=False)
        if material_action is None:
            material_action = find_pending_material_action_authorization(
                state_db_path=state_db_path,
                owner=KG_GRAPH_RELATION_OWNER,
                executor_id=KG_GRAPH_RELATION_EXECUTOR,
                action_type=KG_GRAPH_RELATION_ACTION,
                target_ref=binding["target_ref"],
                input_hash=binding["input_hash"],
            )
        if material_action is None:
            decision_created_at = datetime.now(timezone.utc).isoformat()
            request = MaterialActionRequest(
                owner=KG_GRAPH_RELATION_OWNER,
                executor_id=KG_GRAPH_RELATION_EXECUTOR,
                action_type=KG_GRAPH_RELATION_ACTION,
                target_ref=binding["target_ref"],
                input_hash=binding["input_hash"],
                expected_state_db=str(state_db_path),
            )
            material_action = authorize_exact_project_contract_action(
                expected_request=request,
                state_db_path=state_db_path,
                contract_id=KNOWLEDGE_GRAPH_DECISION_CONTRACT_ID,
                contract_revision_id=(KNOWLEDGE_GRAPH_DECISION_CONTRACT_REVISION),
                contract_text=KNOWLEDGE_GRAPH_DECISION_CONTRACT_TEXT,
                source_namespace="knowledge-graph-relation-upsert",
                source_facts={
                    "schema_version": ("mnemos.knowledge_graph_relation_upsert_facts.v1"),
                    "decision_created_at": decision_created_at,
                    "database_path": str(self.db_path.resolve(strict=False)),
                    "relation": {
                        "source": relation.source,
                        "target": relation.target,
                        "relation_type": relation.relation_type.value,
                        "strength": relation.strength,
                        "confidence": relation.confidence,
                        "source_method": relation.source_method,
                        "context": relation.context,
                        "symmetric": relation.is_symmetric,
                        "reverse_type": (relation.reverse_type if relation.is_symmetric else ""),
                        "evidence": [
                            {
                                "evidence_type": item.evidence_type,
                                "content": item.content,
                            }
                            for item in (relation.evidence or [])
                        ],
                    },
                },
                decision_checks={
                    "relation_endpoints_valid": (
                        not relation_endpoint_rejection_reason(relation.source)
                        and not relation_endpoint_rejection_reason(relation.target)
                    ),
                    "relation_type_registered": isinstance(
                        relation.relation_type,
                        RelationType,
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
                goal=("Persist only the exact relation accepted by KnowledgeGraph " "validation."),
                constraints=(
                    "Both endpoints and the relation type must pass canonical validation.",
                    "Confidence, strength, source, symmetry, context, and evidence cannot drift.",
                ),
                created_at=decision_created_at,
                producer="knowledge-graph-add-relation",
                producer_version=(KNOWLEDGE_GRAPH_DECISION_CONTRACT_REVISION),
                producer_code_hash=KNOWLEDGE_GRAPH_DECISION_PRODUCER_HASH,
                evaluator_id="knowledge-graph-relation-upsert-evaluator",
                approved_candidate_key="upsert_exact_validated_relation",
                approved_candidate_summary=(
                    "Upsert the exact relation accepted by KnowledgeGraph."
                ),
                rejected_candidate_key="reject_invalid_or_drifted_relation",
                rejected_candidate_summary=(
                    "Reject a relation that is invalid or differs from the bound input."
                ),
                approved_reason_code="knowledge_graph_relation_verified",
                rejected_reason_code="knowledge_graph_relation_rejected",
                committed_metric="knowledge_graph_relation_committed",
                rejected_metric="knowledge_graph_relation_rejected",
            )
        material_action, permit = resolve_material_action_recovery_authorization(
            material_action,
            owner=KG_GRAPH_RELATION_OWNER,
            executor_id=KG_GRAPH_RELATION_EXECUTOR,
            action_type=KG_GRAPH_RELATION_ACTION,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
            expected_state_db=self.db_path.parent / "producer_consumer_ledger.db",
        )
        if recover_recorded_target_effect(
            material_action,
            KnowledgeGraphRelationEffectOracle(self.db_path),
        ):
            project_relation(material_action)
            return True

        permit = require_material_action(
            material_action,
            owner=KG_GRAPH_RELATION_OWNER,
            executor_id=KG_GRAPH_RELATION_EXECUTOR,
            action_type=KG_GRAPH_RELATION_ACTION,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
            expected_state_db=self.db_path.parent / "producer_consumer_ledger.db",
        )

        try:
            from core.kia.relation_writer import upsert_relation_row

            with self._conn() as conn:
                before_hash = self._relation_effect_hash(conn, relation)
                if self._relation_is_exact_noop(conn, relation):
                    after_hash = before_hash
                    rel_id = int(
                        conn.execute(
                            """SELECT id FROM relations
                               WHERE source=? AND target=? AND relation_type=?""",
                            (
                                relation.source,
                                relation.target,
                                relation.relation_type.value,
                            ),
                        ).fetchone()[0]
                    )
                    reverse_rel_id = None
                    rel_existed = True
                    rel_changed = False
                    reverse_existed = False
                    reverse_changed = False
                else:
                    rel_id, rel_existed, rel_changed = upsert_relation_row(
                        conn,
                        relation,
                        source=relation.source,
                        target=relation.target,
                        insert_evidence=True,
                    )

                    # 对称关系：自动添加反向
                    reverse_rel_id = None
                    reverse_existed = False
                    reverse_changed = False
                    if relation.is_symmetric and relation.source != relation.target:
                        reverse_rel_id, reverse_existed, reverse_changed = upsert_relation_row(
                            conn,
                            relation,
                            source=relation.target,
                            target=relation.source,
                            insert_evidence=False,
                        )

                    after_hash = self._relation_effect_hash(conn, relation)
                observed_at = (
                    relation.updated_at + "+00:00"
                    if "+" not in relation.updated_at and not relation.updated_at.endswith("Z")
                    else relation.updated_at
                )
                record_target_effect(
                    conn,
                    permit,
                    status="committed",
                    before_hash=before_hash,
                    after_hash=after_hash,
                    evidence_refs=(
                        f"target-after:{after_hash}",
                        f"target-journal:knowledge-graph:{rel_id}:{after_hash}",
                        f"reverse-relation:{reverse_rel_id or 0}",
                    ),
                    outcome=json.dumps(
                        {
                            "schema_version": ("mnemos.knowledge_graph_relation_effect.v1"),
                            "relation_id": rel_id,
                            "reverse_relation_id": reverse_rel_id,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    observed_at=observed_at,
                )
                conn.commit()

            if not recover_recorded_target_effect(
                material_action,
                KnowledgeGraphRelationEffectOracle(self.db_path),
            ):
                raise RuntimeError("knowledge graph effect journal was not recoverable")

            project_relation(material_action)

            # 事务外同步 embedding（软失败，不阻塞主流程）
            if rel_changed:
                self._schedule_relation_embedding(
                    rel_id,
                    relation.context,  # type: ignore[arg-type]
                    replace=rel_existed,
                )
            if reverse_rel_id and reverse_changed:
                self._schedule_relation_embedding(
                    reverse_rel_id,
                    relation.context,
                    replace=reverse_existed,
                )

            return True
        except (sqlite3.Error, ValueError):
            return False

    def remove_relation(
        self, source: str, target: str, relation_type: RelationType | None = None
    ) -> bool:
        """删除关系（同步清理 FTS5 索引）"""
        try:
            with self._conn() as conn:
                if relation_type:
                    # 先查 rowid 再删，确保 FTS5 同步
                    rows = conn.execute(
                        "SELECT id FROM relations WHERE source=? AND target=? AND relation_type=?",
                        (source, target, relation_type.value),
                    ).fetchall()
                    for row in rows:
                        conn.execute("DELETE FROM relations_fts WHERE rowid=?", (row[0],))
                        conn.execute("DELETE FROM relation_evidence WHERE relation_id=?", (row[0],))
                        try:
                            self._rel_emb_mgr.remove_relation_context(row[0])
                        except (
                            OSError,
                            ValueError,
                            TypeError,
                            KeyError,
                            AttributeError,
                            ImportError,
                        ):
                            logging.getLogger(__name__).warning("Unexpected error", exc_info=True)

                    conn.execute(
                        "DELETE FROM relations WHERE source=? AND target=? AND relation_type=?",
                        (source, target, relation_type.value),
                    )
                    # 对称关系同时删反向
                    meta = RELATION_META.get(relation_type, {})
                    if meta.get("symmetric"):
                        rows = conn.execute(
                            "SELECT id FROM relations WHERE source=? AND target=? AND relation_type=?",  # noqa: E501
                            (target, source, relation_type.value),
                        ).fetchall()
                        for row in rows:
                            conn.execute("DELETE FROM relations_fts WHERE rowid=?", (row[0],))
                            conn.execute(
                                "DELETE FROM relation_evidence WHERE relation_id=?", (row[0],)
                            )
                            try:
                                self._rel_emb_mgr.remove_relation_context(row[0])
                            except (
                                OSError,
                                ValueError,
                                TypeError,
                                KeyError,
                                AttributeError,
                                ImportError,
                            ):
                                logging.getLogger(__name__).warning(
                                    "Unexpected error", exc_info=True
                                )
                        conn.execute(
                            "DELETE FROM relations WHERE source=? AND target=? AND relation_type=?",
                            (target, source, relation_type.value),
                        )
                else:
                    rows = conn.execute(
                        "SELECT id FROM relations WHERE source=? AND target=?", (source, target)
                    ).fetchall()
                    for row in rows:
                        conn.execute("DELETE FROM relations_fts WHERE rowid=?", (row[0],))
                        conn.execute("DELETE FROM relation_evidence WHERE relation_id=?", (row[0],))
                        # [P1-5] 同步清理关系向量索引
                        try:
                            self._rel_emb_mgr.remove_relation_context(row[0])
                        except (
                            OSError,
                            ValueError,
                            TypeError,
                            KeyError,
                            AttributeError,
                            ImportError,
                        ):
                            logging.getLogger(__name__).warning("Unexpected error", exc_info=True)
                    conn.execute(
                        "DELETE FROM relations WHERE source=? AND target=?", (source, target)
                    )
                conn.commit()
                return True
        except sqlite3.Error:
            return False

    def rebuild_relation_index(self, batch_size: int = 50) -> dict:
        """
        批量重建所有关系的 embedding 索引。

        对数据库中所有 relation：
        1. 若 context 为空或模板句，自动生成富文本 context 并更新 DB
        2. 调用 RelationEmbeddingManager.add_relation_context(force=True) 重建 embedding

        Returns:
            {"total": N, "updated": M, "failed": K, "skipped": S}
        """
        stats = {"total": 0, "updated": 0, "failed": 0, "skipped": 0}
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT id, source, target, relation_type, strength, confidence, source_method, context "  # noqa: E501
                    "FROM relations ORDER BY id"
                ).fetchall()
        except (sqlite3.Error, OSError) as e:
            logger.warning("[KG] 读取关系失败: %s", e)
            return stats

        stats["total"] = len(rows)
        for row in rows:
            rel_id, source, target, rel_type_str, strength, confidence, source_method, context = row
            try:
                rel_type = RelationType(rel_type_str)
            except ValueError:
                stats["skipped"] += 1
                continue

            relation = Relation(
                source=source,
                target=target,
                relation_type=rel_type,
                strength=strength or 0.5,
                confidence=confidence or 0.5,
                source_method=source_method or "auto",
                context=context or "",
            )

            # 自动生成/更新 context（若为空或为旧模板句）
            needs_update = False
            if not relation.context or not relation.context.strip():
                relation.context = self._build_relation_context(relation)
                needs_update = True
            elif "关联类型为" in relation.context or relation.context.startswith("页面"):
                # 旧模板句检测，替换为富文本
                relation.context = self._build_relation_context(relation)
                needs_update = True

            if needs_update:
                try:
                    with self._conn() as conn:
                        conn.execute(
                            "UPDATE relations SET context=? WHERE id=?", (relation.context, rel_id)
                        )
                        conn.commit()
                except (sqlite3.Error, OSError) as e:
                    logger.debug("[KG] 更新 context 失败 rel_id=%s: %s", rel_id, e, exc_info=True)

            # 强制重建 embedding
            if self._sync_relation_embedding(rel_id, relation.context):
                stats["updated"] += 1
            else:
                stats["failed"] += 1

        if stats["failed"] == 0 and not self._rel_emb_mgr.flush():
            stats["failed"] += 1
        return stats

    def audit_relation_embedding_projection(self) -> dict:
        """Delegate relation-vector durability to its canonical owner."""

        return self._rel_emb_mgr.audit_projection()

    def get_relations(
        self, page: str, relation_type: RelationType | None = None, min_confidence: float = 0.0
    ) -> List[Relation]:
        """获取某页面的出边关系"""
        query = """SELECT r.*, e.evidence_type, e.content
                   FROM relations r
                   LEFT JOIN relation_evidence e ON r.id = e.relation_id
                   WHERE r.source = ? AND r.confidence >= ?"""
        params = [page, min_confidence]

        if relation_type:
            query += " AND r.relation_type = ?"
            params.append(relation_type.value)

        query += """ ORDER BY r.strength DESC,
                                 r.confidence DESC,
                                 r.target COLLATE BINARY ASC,
                                 r.relation_type COLLATE BINARY ASC,
                                 COALESCE(r.source_method, '') COLLATE BINARY ASC,
                                 COALESCE(r.context, '') COLLATE BINARY ASC,
                                 r.id ASC,
                                 e.evidence_type COLLATE BINARY ASC,
                                 COALESCE(e.content, '') COLLATE BINARY ASC,
                                 e.id ASC"""

        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()

        return self._rows_to_relations(rows)

    def get_incoming_relations(self, page: str, min_confidence: float = 0.0) -> List[Relation]:
        """获取指向某页面的入边关系"""
        query = """SELECT r.*, e.evidence_type, e.content
                   FROM relations r
                   LEFT JOIN relation_evidence e ON r.id = e.relation_id
                   WHERE r.target = ? AND r.confidence >= ?
                   ORDER BY r.strength DESC,
                            r.confidence DESC,
                            r.source COLLATE BINARY ASC,
                            r.relation_type COLLATE BINARY ASC,
                            COALESCE(r.source_method, '') COLLATE BINARY ASC,
                            COALESCE(r.context, '') COLLATE BINARY ASC,
                            r.id ASC,
                            e.evidence_type COLLATE BINARY ASC,
                            COALESCE(e.content, '') COLLATE BINARY ASC,
                            e.id ASC"""

        with self._conn() as conn:
            rows = conn.execute(query, (page, min_confidence)).fetchall()

        return self._rows_to_relations(rows)

    def get_all_relations(
        self, page: str, min_confidence: float = 0.0
    ) -> Tuple[List[Relation], List[Relation]]:
        """获取页面的所有关系（出边 + 入边）"""
        return (
            self.get_relations(page, min_confidence=min_confidence),
            self.get_incoming_relations(page, min_confidence=min_confidence),
        )


# ========== 便捷函数 ==========


def build_graph_for_wiki(wiki_base: str | None = None) -> KnowledgeGraph:
    """为整个 Wiki 构建知识图谱（全量扫描）"""
    kg = KnowledgeGraph(wiki_base=wiki_base)
    wiki_path = kg.wiki_base / "00-Inbox"

    if not wiki_path.exists():
        return kg

    all_pages = sorted(wiki_path.glob("*.md"))

    for page in all_pages:
        relations = kg.discover_relations(page, all_pages)
        kg.apply_discovered(relations)

    return kg
