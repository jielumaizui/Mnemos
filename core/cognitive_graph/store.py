# -*- coding: utf-8 -*-
"""CognitiveGraphStore — cross-layer cognitive graph persistence."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from unittest.mock import Mock

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognitive.access_control import (
    authorize_cognitive_access,
    cognitive_access_hash,
    cognitive_access_matches_subject,
    validate_cognitive_access_envelope,
)
from core.cognitive.decision_trace import MaterialActionAuthorization
from core.config import get_config
from core.db_utils import render_sql
from core.cognitive.material_effect_ledger import recover_recorded_target_effect  # noqa: F401
from core.cognitive.material_effect_schema import initialize_material_effect_schema

from .models import CanonicalNode, CognitiveRelation, SyncOutboxItem
from .store_contracts import (  # noqa: F401
    PENDING_LIMIT,
    COGNITIVE_GRAPH_READ_PURPOSE,
    COGNITIVE_GRAPH_DELETION_SCHEMA_VERSION,
    COGNITIVE_GRAPH_DELETION_TABLE,
    COGNITIVE_RELATION_ACTION,
    COGNITIVE_RELATION_STALE_ACTION,
    COGNITIVE_RELATION_DELETE_ACTION,
    COGNITIVE_CANONICAL_NODE_ACTION,
    COGNITIVE_RELATION_OWNER,
    COGNITIVE_RELATION_EXECUTOR,
    COGNITIVE_GRAPH_MAINTENANCE_CONTRACT_ID,
    COGNITIVE_GRAPH_MAINTENANCE_CONTRACT_REVISION,
    COGNITIVE_GRAPH_MAINTENANCE_CONTRACT_TEXT,
    COGNITIVE_GRAPH_MAINTENANCE_PRODUCER_HASH,
    CognitiveGraphRelationEffectOracle,
    CognitiveGraphRelationStaleEffectOracle,
    CognitiveGraphRelationDeleteEffectOracle,
    CognitiveGraphCanonicalNodeEffectOracle,
    _relation_id,
    cognitive_relation_material_action_binding,
    cognitive_relation_stale_material_action_binding,
    cognitive_relation_delete_material_action_binding,
    cognitive_canonical_node_material_action_binding,
    _now,
    _graph_deletion_scope_hash,
    _graph_deletion_receipt_id,
    _parse_graph_access,
    _strictest_graph_access,
    _wiki_urn,
)
from .store_maintenance import CognitiveGraphMaintenanceMixin
from .store_mutations import CognitiveGraphMutationMixin

logger = logging.getLogger(__name__)


class CognitiveGraphStore(CognitiveGraphMutationMixin, CognitiveGraphMaintenanceMixin):
    """跨层认知图数据库"""

    SCHEMA = """
        CREATE TABLE IF NOT EXISTS cognitive_relations (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            target TEXT NOT NULL,
            relation_type TEXT NOT NULL,
            strength REAL DEFAULT 0.5,
            confidence REAL DEFAULT 0.5,
            source_layer TEXT,
            target_layer TEXT,
            created_at TEXT,
            updated_at TEXT,
            stale INTEGER DEFAULT 0,
            access_control TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_cog_rel_source
            ON cognitive_relations(source);
        CREATE INDEX IF NOT EXISTS idx_cog_rel_target
            ON cognitive_relations(target);
        CREATE INDEX IF NOT EXISTS idx_cog_rel_type
            ON cognitive_relations(relation_type);
        CREATE INDEX IF NOT EXISTS idx_cog_rel_stale
            ON cognitive_relations(stale);

        CREATE TABLE IF NOT EXISTS canonical_nodes (
            canonical_id TEXT PRIMARY KEY,
            canonical_name TEXT NOT NULL,
            aliases TEXT,                 -- JSON list
            source_ids TEXT,              -- JSON list of URNs
            embedding BLOB,
            created_at TEXT,
            updated_at TEXT,
            access_control TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_canonical_name
            ON canonical_nodes(canonical_name);

        CREATE TABLE IF NOT EXISTS sync_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            payload TEXT NOT NULL,        -- JSON
            created_at TEXT,
            processed_at TEXT,
            access_control TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_outbox_processed
            ON sync_outbox(processed_at);

        CREATE TABLE IF NOT EXISTS cognitive_graph_deletion_receipts (
            receipt_id TEXT PRIMARY KEY,
            schema_version TEXT NOT NULL,
            request_id TEXT NOT NULL,
            scope_kind TEXT NOT NULL,
            scope_value_hash TEXT NOT NULL,
            object_type TEXT NOT NULL,
            object_id TEXT NOT NULL,
            before_acl_hash TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status='applied'),
            created_at TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            UNIQUE(object_type, object_id)
        );
        CREATE INDEX IF NOT EXISTS idx_cognitive_graph_deletion_request
            ON cognitive_graph_deletion_receipts(request_id, status);
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        *,
        ownership_config: Any | None = None,
    ):
        self.db_path = Path(db_path) if db_path else self._default_db_path()
        self._ownership_config = ownership_config or get_config()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _assert_write_not_frozen(
        self,
        access_control: Mapping[str, Any],
    ) -> None:
        from core.privacy.ownership_freeze import cognitive_write_is_frozen

        scope = access_control["scope"]
        if cognitive_write_is_frozen(
            self._ownership_config,
            session_id=str(scope.get("session_id") or ""),
            project=str(scope.get("project") or ""),
            agent=str(access_control["owner"].get("agent") or ""),
        ):
            raise PermissionError(
                "cognitive graph write is blocked by a matching frozen data ownership scope"
            )

    @staticmethod
    def _default_db_path() -> Path:
        cfg = get_config()
        db_path = CognitiveGraphStore._path_from_config_value(
            getattr(cfg, "cognitive_graph_db_path", None)
        )
        if db_path is not None:
            return db_path

        database_dir = CognitiveGraphStore._path_from_config_value(
            getattr(cfg, "database_dir", None)
        )
        if database_dir is not None:
            return database_dir / "cognitive_graph.db"
        raise RuntimeError("CognitiveGraphStore requires cognitive_graph_db_path or database_dir")

    @staticmethod
    def _path_from_config_value(value: Any) -> Optional[Path]:
        if isinstance(value, Mock):
            return None
        if isinstance(value, Path):
            return value.expanduser()
        if isinstance(value, (str, os.PathLike)):
            return Path(value).expanduser()
        return None

    def _init_db(self):
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            initialize_material_effect_schema(conn)
            conn.executescript(self.SCHEMA)
            self._ensure_acl_columns(conn)
            conn.commit()

    @staticmethod
    def _ensure_acl_columns(conn: sqlite3.Connection) -> None:
        """Upgrade historical graph tables without inventing readable ACLs."""

        for table_name in ("cognitive_relations", "canonical_nodes", "sync_outbox"):
            columns = {
                str(row[1])
                for row in conn.execute(
                    render_sql(
                        "PRAGMA table_info({table})",
                        identifiers={"table": table_name},
                    )
                )
            }
            if "access_control" not in columns:
                conn.execute(
                    render_sql(
                        "ALTER TABLE {table} ADD COLUMN " "access_control TEXT NOT NULL DEFAULT ''",
                        identifiers={"table": table_name},
                    )
                )

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row  # noqa
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _object_subject_deleted(
        conn: sqlite3.Connection,
        *,
        object_type: str,
        object_id: str,
    ) -> bool:
        row = conn.execute(
            render_sql(
                """
            SELECT 1 FROM {table}
            WHERE object_type=? AND object_id=? AND status='applied'
            """,
                identifiers={"table": COGNITIVE_GRAPH_DELETION_TABLE},
            ),
            (object_type, object_id),
        ).fetchone()
        return row is not None

    def get_relation(self, rel_id: str) -> Optional[CognitiveRelation]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM cognitive_relations WHERE id = ?", (rel_id,)
            ).fetchone()
        return self._row_to_relation(row) if row else None

    def get_relations(
        self,
        source: Optional[str] = None,
        target: Optional[str] = None,
        relation_type: Optional[str] = None,
        include_stale: bool = False,
        limit: int = 100,
    ) -> List[CognitiveRelation]:
        """按 source/target/relation_type 查询关系"""
        conditions: List[str] = []
        params: List[Any] = []
        if source is not None:
            conditions.append("source = ?")
            params.append(source)
        if target is not None:
            conditions.append("target = ?")
            params.append(target)
        if relation_type is not None:
            conditions.append("relation_type = ?")
            params.append(relation_type)
        if not include_stale:
            conditions.append("stale = 0")

        where = " AND ".join(conditions) if conditions else "1=1"
        query = " ".join(
            [
                "SELECT * FROM cognitive_relations",
                "WHERE",
                where,
                "ORDER BY updated_at DESC",
                "LIMIT ?",
            ]
        )
        params.append(limit)

        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_relation(row) for row in rows]

    @staticmethod
    def _authorize_header(
        raw_access_control: Any,
        *,
        object_ref: str,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
        purpose: str,
    ) -> str:
        """Return an ACL decision using headers only, before body hydration."""

        try:
            access_control = validate_cognitive_access_envelope(
                json.loads(str(raw_access_control or ""))
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            return "acl_unknown"
        return authorize_cognitive_access(
            access_control,
            principal=principal,
            narrowing=narrowing,
            purpose=purpose,
        ).reason

    def authorized_get_relation(
        self,
        rel_id: str,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None = None,
        purpose: str = COGNITIVE_GRAPH_READ_PURPOSE,
    ) -> Tuple[Optional[CognitiveRelation], Dict[str, int]]:
        """Read one relation only after header-level object authorization."""

        if principal is None:
            return None, {"principal_required": 1}
        with self._conn() as conn:
            header = conn.execute(
                "SELECT id, access_control FROM cognitive_relations WHERE id=?",
                (rel_id,),
            ).fetchone()
            if header is None:
                return None, {"not_found": 1}
            reason = self._authorize_header(
                header["access_control"],
                object_ref=f"relation:{rel_id}",
                principal=principal,
                narrowing=narrowing,
                purpose=purpose,
            )
            if reason != "authorized":
                return None, {reason: 1}
            row = conn.execute(
                "SELECT * FROM cognitive_relations WHERE id=?",
                (rel_id,),
            ).fetchone()
        return (self._row_to_relation(row) if row is not None else None), {"authorized": 1}

    def authorized_get_relations(
        self,
        source: Optional[str] = None,
        target: Optional[str] = None,
        relation_type: Optional[str] = None,
        include_stale: bool = False,
        limit: int = 100,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None = None,
        purpose: str = COGNITIVE_GRAPH_READ_PURPOSE,
    ) -> Tuple[List[CognitiveRelation], Dict[str, int]]:
        """Return relation bodies only after per-row ACL header filtering."""

        if principal is None:
            return [], {"principal_required": 1}
        conditions: List[str] = []
        params: List[Any] = []
        if source is not None:
            conditions.append("source = ?")
            params.append(source)
        if target is not None:
            conditions.append("target = ?")
            params.append(target)
        if relation_type is not None:
            conditions.append("relation_type = ?")
            params.append(relation_type)
        if not include_stale:
            conditions.append("stale = 0")
        where = " AND ".join(conditions) if conditions else "1=1"
        normalized_limit = max(0, min(int(limit), 1000))
        with self._conn() as conn:
            headers = conn.execute(
                " ".join(
                    [
                        "SELECT id, access_control FROM cognitive_relations",
                        "WHERE",
                        where,
                        "ORDER BY updated_at DESC",
                    ]
                ),
                params,
            ).fetchall()
            allowed_ids: List[str] = []
            summary: Dict[str, int] = {}
            for header in headers:
                relation_id = str(header["id"])
                reason = self._authorize_header(
                    header["access_control"],
                    object_ref=f"relation:{relation_id}",
                    principal=principal,
                    narrowing=narrowing,
                    purpose=purpose,
                )
                summary[reason] = summary.get(reason, 0) + 1
                if reason == "authorized" and len(allowed_ids) < normalized_limit:
                    allowed_ids.append(relation_id)
            if not allowed_ids:
                return [], summary
            placeholders = ",".join("?" for _ in allowed_ids)
            rows_by_id = {
                str(row["id"]): row
                for row in conn.execute(
                    f"SELECT * FROM cognitive_relations "
                    f"WHERE id IN ({placeholders})",  # nosec B608 - generated placeholders
                    tuple(allowed_ids),
                ).fetchall()
            }
        return (
            [
                self._row_to_relation(rows_by_id[relation_id])
                for relation_id in allowed_ids
                if relation_id in rows_by_id
            ],
            summary,
        )

    def delete_subject_scope(
        self,
        *,
        request_id: str,
        scope_kind: str,
        scope_value: str,
    ) -> Dict[str, Any]:
        """Delete graph bodies matched only by canonical object ACL headers."""

        kind = str(scope_kind or "").strip().lower()
        value = str(scope_value or "").strip()
        supported_scopes = {"all", "agent", "session", "project"}
        if kind not in supported_scopes or (kind == "all" and value != "all"):
            return {
                "status": "unsupported_scope",
                "target_count": 0,
                "verified": False,
                "supported_scopes": sorted(supported_scopes),
            }
        if not str(request_id or "").strip() or not value:
            raise ValueError("cognitive graph subject deletion requires request_id and scope_value")

        normalized_value = value.lower() if kind in {"agent", "project"} else value
        scope_hash = _graph_deletion_scope_hash(kind, normalized_value)
        table_specs = (
            ("relation", "cognitive_relations", "id"),
            ("canonical_node", "canonical_nodes", "canonical_id"),
            ("sync_outbox", "sync_outbox", "id"),
        )
        with self._conn() as conn:
            prior = conn.execute(
                render_sql(
                    """
                SELECT COUNT(*) FROM {table}
                WHERE scope_kind=? AND scope_value_hash=? AND status='applied'
                """,
                    identifiers={"table": COGNITIVE_GRAPH_DELETION_TABLE},
                ),
                (kind, scope_hash),
            ).fetchone()
            selected: list[tuple[str, str, str, str]] = []
            unresolved_legacy_count = 0
            for object_type, table_name, id_column in table_specs:
                headers = conn.execute(
                    render_sql(
                        "SELECT {id_column} AS object_id, access_control " "FROM {table}",
                        identifiers={"id_column": id_column, "table": table_name},
                    )
                ).fetchall()
                for header in headers:
                    raw_access = header["access_control"]
                    try:
                        access = validate_cognitive_access_envelope(
                            json.loads(str(raw_access or ""))
                        )
                        if access["scope"]["resolution"] != "resolved":
                            raise ValueError("cognitive graph ACL scope is unresolved")
                        before_acl_hash = cognitive_access_hash(access)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        unresolved_legacy_count += 1
                        if kind != "all":
                            continue
                        before_acl_hash = (
                            "sha256:"
                            + hashlib.sha256(str(raw_access or "").encode("utf-8")).hexdigest()
                        )
                    if kind == "all" or cognitive_access_matches_subject(
                        access,
                        scope_kind=kind,
                        scope_value=normalized_value,
                    ):
                        selected.append(
                            (object_type, table_name, str(header["object_id"]), before_acl_hash)
                        )

            if not selected:
                prior_count = int(prior[0] or 0) if prior is not None else 0
                return {
                    "status": "existing" if prior_count else "no_targets",
                    "target_count": prior_count,
                    "receipt_count": prior_count,
                    "unresolved_legacy_count": unresolved_legacy_count,
                    "verified": unresolved_legacy_count == 0,
                }

            now = _now()
            id_columns = {object_type: column for object_type, _table, column in table_specs}
            receipt_count = 0
            try:
                for object_type, table_name, object_id, before_acl_hash in selected:
                    conn.execute(
                        render_sql(
                            """
                        INSERT INTO {table} (
                            receipt_id, schema_version, request_id, scope_kind,
                            scope_value_hash, object_type, object_id, before_acl_hash,
                            status, created_at, applied_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'applied', ?, ?)
                        """,
                            identifiers={"table": COGNITIVE_GRAPH_DELETION_TABLE},
                        ),
                        (
                            _graph_deletion_receipt_id(
                                request_id=str(request_id),
                                object_type=object_type,
                                object_id=object_id,
                                scope_hash=scope_hash,
                            ),
                            COGNITIVE_GRAPH_DELETION_SCHEMA_VERSION,
                            str(request_id),
                            kind,
                            scope_hash,
                            object_type,
                            object_id,
                            before_acl_hash,
                            now,
                            now,
                        ),
                    )
                    conn.execute(
                        render_sql(
                            "DELETE FROM {table} WHERE {id_column}=?",
                            identifiers={
                                "table": table_name,
                                "id_column": id_columns[object_type],
                            },
                        ),
                        (object_id,),
                    )
                    receipt_count += 1
                conn.commit()
            except sqlite3.Error:
                conn.rollback()
                return {
                    "status": "blocked",
                    "target_count": 0,
                    "receipt_count": 0,
                    "verified": False,
                    "error": "cognitive_graph_subject_deletion_failed",
                }

            residual_count = 0
            for object_type, table_name, object_id, _before_acl_hash in selected:
                id_column = id_columns[object_type]
                residual_count += int(
                    conn.execute(
                        render_sql(
                            "SELECT COUNT(*) FROM {table} WHERE {id_column}=?",
                            identifiers={
                                "table": table_name,
                                "id_column": id_column,
                            },
                        ),
                        (object_id,),
                    ).fetchone()[0]
                    or 0
                )
            unresolved_after = 0
            for _object_type, table_name, id_column in table_specs:
                for header in conn.execute(
                    render_sql(
                        "SELECT {id_column}, access_control FROM {table}",
                        identifiers={"id_column": id_column, "table": table_name},
                    )
                ).fetchall():
                    try:
                        access = validate_cognitive_access_envelope(
                            json.loads(str(header["access_control"] or ""))
                        )
                        if access["scope"]["resolution"] != "resolved":
                            raise ValueError("cognitive graph ACL scope is unresolved")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        unresolved_after += 1

        return {
            "status": "applied",
            "target_count": len(selected),
            "receipt_count": receipt_count,
            "after_count": residual_count,
            "unresolved_legacy_count": unresolved_after,
            "verified": residual_count == 0 and unresolved_after == 0,
        }

    def reconcile_wiki_page(
        self,
        *,
        previous_path: str,
        page_path: str,
        mutation_type: str,
        material_action_resolver: (
            Callable[[str, Mapping[str, str]], MaterialActionAuthorization] | None
        ) = None,
    ) -> Dict[str, int]:
        """Apply move/delete semantics to relations that reference a Wiki URN."""
        old_urn = _wiki_urn(previous_path or page_path)
        new_urn = _wiki_urn(page_path)
        if mutation_type not in {"move", "delete"} or not old_urn:
            return {"relations_migrated": 0, "relations_staled": 0}
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM cognitive_relations WHERE stale=0 AND (source=? OR target=?)",
                (old_urn, old_urn),
            ).fetchall()
        migrated = 0
        if mutation_type == "move" and new_urn:
            for row in rows:
                source = new_urn if row["source"] == old_urn else row["source"]
                target = new_urn if row["target"] == old_urn else row["target"]
                access_control = _parse_graph_access(
                    row["access_control"],
                    f"relation:{row['id']}",
                )
                binding = cognitive_relation_material_action_binding(
                    source=source,
                    target=target,
                    relation_type=row["relation_type"],
                    strength=row["strength"],
                    confidence=row["confidence"],
                    source_layer=row["source_layer"] or "",
                    target_layer=row["target_layer"] or "",
                    access_control=access_control,
                )
                self.add_relation(
                    source=source,
                    target=target,
                    relation_type=row["relation_type"],
                    strength=row["strength"],
                    confidence=row["confidence"],
                    source_layer=row["source_layer"] or "",
                    target_layer=row["target_layer"] or "",
                    access_control=access_control,
                    material_action=(
                        material_action_resolver(
                            COGNITIVE_RELATION_ACTION,
                            binding,
                        )
                        if material_action_resolver is not None
                        else None
                    ),
                )
                migrated += 1
        staled = 0
        for row in rows:
            binding = cognitive_relation_stale_material_action_binding(row["id"])
            if self.mark_stale(
                row["id"],
                material_action=(
                    material_action_resolver(
                        COGNITIVE_RELATION_STALE_ACTION,
                        binding,
                    )
                    if material_action_resolver is not None
                    else None
                ),
            ):
                staled += 1
        return {"relations_migrated": migrated, "relations_staled": staled}

    @staticmethod
    def _row_to_relation(row: sqlite3.Row) -> CognitiveRelation:
        return CognitiveRelation(
            id=row["id"],
            source=row["source"],
            target=row["target"],
            relation_type=row["relation_type"],
            strength=row["strength"],
            confidence=row["confidence"],
            source_layer=row["source_layer"] or "",
            target_layer=row["target_layer"] or "",
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
            stale=row["stale"],
            access_control=_parse_graph_access(row["access_control"], f"relation:{row['id']}"),
        )

    # ───────────────────────────────
    # 归一化节点
    # ───────────────────────────────

    def get_canonical_node(self, canonical_id: str) -> Optional[CanonicalNode]:
        with self._conn() as conn:
            return self._get_canonical_node_raw(conn, canonical_id)

    def find_canonical_nodes(
        self,
        name: Optional[str] = None,
        alias: Optional[str] = None,
        source_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[CanonicalNode]:
        """按名称、别名或 source_id 查找归一化节点"""
        conditions: List[str] = []
        params: List[Any] = []
        if name is not None:
            conditions.append("canonical_name = ?")
            params.append(name)
        if alias is not None:
            conditions.append("(aliases LIKE ? OR canonical_name = ?)")
            params.append(f'%"{alias}"%')
            params.append(alias)
        if source_id is not None:
            conditions.append("source_ids LIKE ?")
            params.append(f'%"{source_id}"%')

        where = " AND ".join(conditions) if conditions else "1=1"
        query = " ".join(
            [
                "SELECT * FROM canonical_nodes",
                "WHERE",
                where,
                "LIMIT ?",
            ]
        )
        params.append(limit)

        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_canonical_node(row) for row in rows]

    def authorized_find_canonical_nodes(
        self,
        name: Optional[str] = None,
        alias: Optional[str] = None,
        source_id: Optional[str] = None,
        limit: int = 20,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None = None,
        purpose: str = COGNITIVE_GRAPH_READ_PURPOSE,
    ) -> Tuple[List[CanonicalNode], Dict[str, int]]:
        """Search canonical-node ACL headers before reading names or vectors."""

        if principal is None:
            return [], {"principal_required": 1}
        conditions: List[str] = []
        params: List[Any] = []
        if name is not None:
            conditions.append("canonical_name = ?")
            params.append(name)
        if alias is not None:
            conditions.append("(aliases LIKE ? OR canonical_name = ?)")
            params.extend((f'%"{alias}"%', alias))
        if source_id is not None:
            conditions.append("source_ids LIKE ?")
            params.append(f'%"{source_id}"%')
        where = " AND ".join(conditions) if conditions else "1=1"
        normalized_limit = max(0, min(int(limit), 1000))
        with self._conn() as conn:
            headers = conn.execute(
                " ".join(
                    [
                        "SELECT canonical_id, access_control FROM canonical_nodes",
                        "WHERE",
                        where,
                    ]
                ),
                params,
            ).fetchall()
            allowed_ids: List[str] = []
            summary: Dict[str, int] = {}
            for header in headers:
                canonical_id = str(header["canonical_id"])
                reason = self._authorize_header(
                    header["access_control"],
                    object_ref=f"canonical:{canonical_id}",
                    principal=principal,
                    narrowing=narrowing,
                    purpose=purpose,
                )
                summary[reason] = summary.get(reason, 0) + 1
                if reason == "authorized" and len(allowed_ids) < normalized_limit:
                    allowed_ids.append(canonical_id)
            if not allowed_ids:
                return [], summary
            placeholders = ",".join("?" for _ in allowed_ids)
            rows_by_id = {
                str(row["canonical_id"]): row
                for row in conn.execute(
                    f"SELECT * FROM canonical_nodes "
                    f"WHERE canonical_id IN ({placeholders})",  # nosec B608 - generated placeholders
                    tuple(allowed_ids),
                ).fetchall()
            }
        return (
            [
                self._row_to_canonical_node(rows_by_id[canonical_id])
                for canonical_id in allowed_ids
                if canonical_id in rows_by_id
            ],
            summary,
        )

    @staticmethod
    def _canonical_id(name: str) -> str:
        key = name.strip().lower()
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]

    @classmethod
    def _get_canonical_node_raw(
        cls, conn: sqlite3.Connection, canonical_id: str
    ) -> Optional[CanonicalNode]:
        row = conn.execute(
            "SELECT * FROM canonical_nodes WHERE canonical_id = ?",
            (canonical_id,),
        ).fetchone()
        return cls._row_to_canonical_node(row) if row else None

    @staticmethod
    def _row_to_canonical_node(row: sqlite3.Row) -> CanonicalNode:
        def _load_json(col):
            try:
                return json.loads(col or "[]")
            except json.JSONDecodeError:
                return []

        return CanonicalNode(
            canonical_id=row["canonical_id"],
            canonical_name=row["canonical_name"],
            aliases=_load_json(row["aliases"]),
            source_ids=_load_json(row["source_ids"]),
            embedding=row["embedding"],
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
            access_control=_parse_graph_access(
                row["access_control"],
                f"canonical:{row['canonical_id']}",
            ),
        )

    # ───────────────────────────────
    # Outbox
    # ───────────────────────────────

    def add_sync_outbox(
        self,
        event_type: str,
        payload: Dict[str, Any],
        access_control: Mapping[str, Any] | None = None,
    ) -> SyncOutboxItem:
        """向 outbox 写入一条待处理事件"""
        now = _now()
        payload_json = json.dumps(payload, ensure_ascii=False)
        item_ref = (
            f"outbox:{event_type}:{hashlib.sha256(payload_json.encode('utf-8')).hexdigest()[:16]}"
        )
        effective_access = _strictest_graph_access(
            [access_control] if access_control is not None else [],
            object_ref=item_ref,
        )
        self._assert_write_not_frozen(effective_access)
        with self._conn() as conn:
            cursor = conn.execute(
                """INSERT INTO sync_outbox (event_type, payload, created_at, access_control)
                   VALUES (?, ?, ?, ?)""",
                (
                    event_type,
                    payload_json,
                    now,
                    json.dumps(effective_access, ensure_ascii=False, sort_keys=True),
                ),
            )
            conn.commit()
            item_id = cursor.lastrowid
        return self.get_outbox_item(item_id)  # type: ignore[return-value]

    def get_outbox_item(self, item_id: int) -> Optional[SyncOutboxItem]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM sync_outbox WHERE id = ?", (item_id,)).fetchone()
        return self._row_to_outbox_item(row) if row else None

    def fetch_outbox(
        self,
        unprocessed_only: bool = True,
        limit: int = 100,
    ) -> List[SyncOutboxItem]:
        """读取 outbox"""
        if unprocessed_only:
            query = (
                "SELECT * FROM sync_outbox WHERE processed_at IS NULL " "ORDER BY id ASC LIMIT ?"
            )
        else:
            query = "SELECT * FROM sync_outbox ORDER BY id ASC LIMIT ?"
        with self._conn() as conn:
            rows = conn.execute(query, (limit,)).fetchall()
        return [self._row_to_outbox_item(row) for row in rows]

    def mark_outbox_processed(self, item_id: int) -> bool:
        with self._conn() as conn:
            cursor = conn.execute(
                "UPDATE sync_outbox SET processed_at = ? WHERE id = ?",
                (_now(), item_id),
            )
            conn.commit()
        return cursor.rowcount > 0  # type: ignore[no-any-return]

    def cleanup_outbox(self, retention_days: int = 30) -> int:
        """删除已处理超过 retention_days 的 outbox 条目，防止表无限增长。"""
        if retention_days <= 0:
            return 0
        from datetime import datetime, timedelta, timezone

        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()[:19]
        with self._conn() as conn:
            cursor = conn.execute(
                "DELETE FROM sync_outbox WHERE processed_at IS NOT NULL AND processed_at < ?",
                (cutoff,),
            )
            conn.commit()
        removed = cursor.rowcount
        if removed:
            logger.info("[CognitiveGraphStore] 清理 outbox: %d 条已处理记录", removed)
        return removed  # type: ignore[no-any-return]

    @staticmethod
    def _row_to_outbox_item(row: sqlite3.Row) -> SyncOutboxItem:
        return SyncOutboxItem(
            id=row["id"],
            event_type=row["event_type"],
            payload=json.loads(row["payload"]),
            created_at=row["created_at"] or "",
            processed_at=row["processed_at"],
            access_control=_parse_graph_access(row["access_control"], f"outbox:{row['id']}"),
        )

    # ───────────────────────────────
    # 批量 / 兜底
    # ───────────────────────────────


def build_belief_feedback_proposal_owner(database_dir: Path):
    """Return the belief-graph pending-review journal for feedback commands."""

    from core.cognitive.feedback_target_registry import (
        build_registered_feedback_proposal_owner,
    )

    return build_registered_feedback_proposal_owner(
        database_dir,
        "belief_correction_proposal",
    )
