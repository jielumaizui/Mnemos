"""Projection, batching, and lifecycle behavior mixed into KnowledgeGraph."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Dict, List, Tuple, cast

from .relation_schema import RELATION_META, Relation, RelationEvidence, RelationType

logger = logging.getLogger(__name__)


class KnowledgeGraphProjectionMixin:
    """Keep projection concerns out of the core graph query facade."""

    _deferred_relation_embeddings: Dict[int, Tuple[str, bool]] | None

    @staticmethod
    def _ensure_embedding_outbox(conn: Any) -> None:
        conn.execute("""CREATE TABLE IF NOT EXISTS kg_embedding_outbox (
                   relation_id INTEGER PRIMARY KEY,
                   operation TEXT NOT NULL CHECK(operation IN ('upsert', 'delete')),
                   context TEXT NOT NULL DEFAULT '',
                   hnsw_id INTEGER,
                   attempts INTEGER NOT NULL DEFAULT 0,
                   last_error TEXT NOT NULL DEFAULT '',
                   updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
               )""")

    @classmethod
    def _queue_embedding_operation(
        cls,
        conn: Any,
        relation_id: int,
        operation: str,
        *,
        context: str = "",
        hnsw_id: int | None = None,
    ) -> None:
        cls._ensure_embedding_outbox(conn)
        conn.execute(
            """INSERT INTO kg_embedding_outbox
                   (relation_id, operation, context, hnsw_id, updated_at)
               VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(relation_id) DO UPDATE SET
                   operation=excluded.operation,
                   context=excluded.context,
                   hnsw_id=COALESCE(excluded.hnsw_id, kg_embedding_outbox.hnsw_id),
                   last_error='',
                   updated_at=CURRENT_TIMESTAMP""",
            (relation_id, operation, context, hnsw_id),
        )

    def repair_relation_embedding_outbox(
        self: Any,
        *,
        flush_each: bool = True,
    ) -> Dict[str, int]:
        """Replay durable vector changes and clear only flushed operations."""

        with self._conn() as conn:
            self._ensure_embedding_outbox(conn)
            rows = conn.execute("SELECT * FROM kg_embedding_outbox ORDER BY relation_id").fetchall()
        repaired = failed = 0
        attempted: list[tuple[int, bool, str]] = []
        defer_flush = getattr(self._rel_emb_mgr, "defer_automatic_flush", None)
        flush_scope = defer_flush() if not flush_each and callable(defer_flush) else nullcontext()
        with flush_scope:
            for row in rows:
                relation_id = int(row["relation_id"])
                try:
                    if row["operation"] == "delete":
                        ok = self._rel_emb_mgr.remove_relation_projection(
                            relation_id,
                            hnsw_id=(int(row["hnsw_id"]) if row["hnsw_id"] is not None else None),
                        )
                    else:
                        ok = self._rel_emb_mgr.add_relation_context(
                            relation_id, str(row["context"]), force=True
                        )
                    if flush_each:
                        ok = bool(ok and self._rel_emb_mgr.flush())
                except (
                    OSError,
                    ValueError,
                    TypeError,
                    KeyError,
                    AttributeError,
                    ImportError,
                    RuntimeError,
                    sqlite3.Error,
                ) as exc:
                    ok = False
                    error = str(exc)
                else:
                    error = "embedding operation returned false" if not ok else ""
                if not flush_each:
                    attempted.append((relation_id, ok, error))
                    continue
                with self._conn() as conn:
                    if ok:
                        conn.execute(
                            "DELETE FROM kg_embedding_outbox WHERE relation_id=?",
                            (relation_id,),
                        )
                        repaired += 1
                    else:
                        conn.execute(
                            """UPDATE kg_embedding_outbox
                               SET attempts=attempts+1, last_error=?,
                                   updated_at=CURRENT_TIMESTAMP
                               WHERE relation_id=?""",
                            (error, relation_id),
                        )
                        failed += 1
                    conn.commit()
        if not flush_each and attempted:
            flush_ok = bool(self._rel_emb_mgr.flush())
            with self._conn() as conn:
                for relation_id, ok, error in attempted:
                    if ok and flush_ok:
                        conn.execute(
                            "DELETE FROM kg_embedding_outbox WHERE relation_id=?",
                            (relation_id,),
                        )
                        repaired += 1
                    else:
                        failure = error or "embedding batch flush returned false"
                        conn.execute(
                            """UPDATE kg_embedding_outbox
                               SET attempts=attempts+1, last_error=?,
                                   updated_at=CURRENT_TIMESTAMP
                               WHERE relation_id=?""",
                            (failure, relation_id),
                        )
                        failed += 1
                conn.commit()
        return {"pending": len(rows), "repaired": repaired, "failed": failed}

    def repair_relation_embedding_orphans(self: Any) -> Dict[str, int]:
        """Convert preexisting SQLite orphans into replayable delete operations."""

        self._rel_emb_mgr
        with self._conn() as conn:
            self._ensure_embedding_outbox(conn)
            rows = conn.execute("""SELECT embedding.relation_id, embedding.id
                   FROM relation_context_embeddings AS embedding
                   LEFT JOIN relations AS relation ON relation.id=embedding.relation_id
                   WHERE relation.id IS NULL""").fetchall()
            for row in rows:
                self._queue_embedding_operation(
                    conn,
                    int(row["relation_id"]),
                    "delete",
                    hnsw_id=int(row["id"]),
                )
            conn.commit()
        result = self.repair_relation_embedding_outbox()
        return {"orphans": len(rows), **result}

    def _schedule_relation_embedding(
        self: Any, rel_id: int, context: str, *, replace: bool
    ) -> bool:
        if self._deferred_relation_embeddings is not None:
            self._deferred_relation_embeddings[rel_id] = (context, replace)
            return True
        if replace:
            self._rel_emb_mgr.remove_relation_context(rel_id)
        return bool(self._sync_relation_embedding(rel_id, context))

    def _schedule_missing_relation_embeddings(
        self: Any,
        relation: Relation,
    ) -> int:
        """Repair the target-commit-before-vector crash window on replay."""

        endpoints = [(relation.source, relation.target)]
        if relation.is_symmetric and relation.source != relation.target:
            endpoints.append((relation.target, relation.source))
        with self._conn() as conn:
            has_embeddings = conn.execute("""SELECT 1 FROM sqlite_master
                   WHERE type='table' AND name='relation_context_embeddings'""").fetchone()
            if has_embeddings is None:
                return 0
            missing_ids: list[int] = []
            for source, target in endpoints:
                row = conn.execute(
                    """SELECT relation.id
                       FROM relations AS relation
                       LEFT JOIN relation_context_embeddings AS embedding
                         ON embedding.relation_id=relation.id
                       WHERE relation.source=? AND relation.target=?
                         AND relation.relation_type=?
                         AND embedding.relation_id IS NULL""",
                    (source, target, relation.relation_type.value),
                ).fetchone()
                if row is not None:
                    missing_ids.append(int(row[0]))
        for relation_id in missing_ids:
            self._schedule_relation_embedding(
                relation_id,
                relation.context,
                replace=False,
            )
        return len(missing_ids)

    @contextmanager
    def defer_relation_embeddings(self: Any):
        """Collect changed relation embeddings and flush them in one batch."""

        if self._deferred_relation_embeddings is not None:
            raise RuntimeError("relation embedding batch is already active")
        # A clean projection rebuild starts with no knowledge_graph.db.  Make
        # the RelationEmbeddingManager, its canonical schema owner, establish
        # the durable relation-context table before replaying relations.  The
        # replay may otherwise see no pending vector work and leave the
        # required projection endpoint absent.
        _ = self._rel_emb_mgr
        pending: Dict[int, Tuple[str, bool]] = {}
        stats = {"total": 0, "added": 0, "skipped": 0, "failed": 0}
        self._deferred_relation_embeddings = pending
        try:
            yield stats
        finally:
            self._deferred_relation_embeddings = None
            outbox = self.repair_relation_embedding_outbox(flush_each=False)
            if int(outbox.get("failed", 0)):
                raise RuntimeError(
                    "relation embedding delete batch failed: "
                    f"{outbox['failed']}/{outbox['pending']}"
                )
            if pending:
                result = self._rel_emb_mgr.add_relation_contexts(
                    {relation_id: item[0] for relation_id, item in pending.items()},
                    replace_ids={relation_id for relation_id, item in pending.items() if item[1]},
                )
                stats.update(result)
                if int(result.get("failed", 0)):
                    raise RuntimeError(
                        "relation embedding batch failed: " f"{result['failed']}/{result['total']}"
                    )
            if int(outbox.get("pending", 0)):
                from core.embeddings.relation_manager import HNSWLIB_AVAILABLE

                if HNSWLIB_AVAILABLE and not self._rel_emb_mgr.rebuild_persistent_index():
                    raise RuntimeError(
                        "relation embedding ANN compaction failed after durable changes"
                    )

    def _rows_to_relations(self: Any, rows: List[Any]) -> List[Relation]:
        """Convert joined relation/evidence rows into unique relation objects."""

        relations_map: Dict[tuple[str, str, str], Relation] = {}
        for row in rows:
            rel_key = (
                str(row["source"]),
                str(row["target"]),
                str(row["relation_type"]),
            )
            if rel_key not in relations_map:
                relations_map[rel_key] = Relation(
                    source=row["source"],
                    target=row["target"],
                    relation_type=RelationType(row["relation_type"]),
                    strength=row["strength"],
                    confidence=row["confidence"],
                    source_method=row["source_method"],
                    context=row["context"] or "",
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    evidence=[],
                )
            if row["evidence_type"]:
                relation = relations_map[rel_key]
                if relation.evidence is None:
                    relation.evidence = []
                relation.evidence.append(
                    RelationEvidence(
                        evidence_type=row["evidence_type"], content=row["content"] or ""
                    )
                )
        return list(relations_map.values())

    def list_relations_for_projection(self: Any) -> List[Relation]:
        """Return the complete relation/evidence view used by read-only projections."""

        query = """
            SELECT r.*, e.evidence_type, e.content
            FROM relations r
            LEFT JOIN relation_evidence e ON r.id = e.relation_id
            ORDER BY r.confidence DESC,
                     r.strength DESC,
                     r.source COLLATE BINARY ASC,
                     r.relation_type COLLATE BINARY ASC,
                     r.target COLLATE BINARY ASC,
                     COALESCE(r.source_method, '') COLLATE BINARY ASC,
                     COALESCE(r.context, '') COLLATE BINARY ASC,
                     r.id ASC,
                     e.evidence_type COLLATE BINARY ASC,
                     COALESCE(e.content, '') COLLATE BINARY ASC
        """
        with self._conn() as conn:
            rows = conn.execute(query).fetchall()
        return cast(List[Relation], self._rows_to_relations(rows))

    def prepare_relation_candidates(
        self: Any, existing_pages: List[Path]
    ) -> Dict[Path, Dict[str, Any]]:
        """Parse candidate pages once for a multi-page reconciliation batch."""

        prepared: Dict[Path, Dict[str, Any]] = {}
        for existing_path in existing_pages:
            try:
                content = existing_path.read_text(encoding="utf-8")
                meta = self._extract_frontmatter(content)
                prepared[existing_path] = {
                    "meta": meta,
                    "title": self._extract_title(content) or existing_path.stem,
                    "keywords": self._extract_all_keywords(meta),
                    "rel_target": self._rel_path(existing_path),
                }
            except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError):
                logger.warning(
                    "KG relation candidate parse failed: %s", existing_path, exc_info=True
                )
        return prepared

    def reconcile_page_lifecycle(
        self: Any,
        *,
        previous_path: Path,
        page_path: Path,
        mutation_type: str,
        replacement_relations: List[Relation] | None = None,
    ) -> Dict[str, int | str]:
        """Keep entity and relation endpoints symmetric with Wiki move/delete."""

        pending = (
            {"pending": 0, "repaired": 0, "failed": 0}
            if self._deferred_relation_embeddings is not None
            else self.repair_relation_embedding_outbox()
        )
        if pending["failed"]:
            raise RuntimeError(f"KG lifecycle embedding outbox repair failed: {pending['failed']}")
        if mutation_type not in {"create", "update", "move", "delete"}:
            return {"mutation_type": mutation_type, "entities_updated": 0, "relations_updated": 0}
        old_absolute = str(previous_path.expanduser().resolve(strict=False))
        new_absolute = str(page_path.expanduser().resolve(strict=False))
        old_endpoint = self._rel_path(Path(old_absolute))
        new_endpoint = self._rel_path(Path(new_absolute))
        relations_updated = relations_deleted = entities_updated = 0
        with self._conn() as conn:
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            self._ensure_embedding_outbox(conn)
            if "entities" in tables:
                entities_updated = self._reconcile_entity_sources(
                    conn,
                    tables=tables,
                    mutation_type=mutation_type,
                    old_absolute=old_absolute,
                    old_endpoint=old_endpoint,
                    new_absolute=new_absolute,
                    new_endpoint=new_endpoint,
                )
            replacing_page = mutation_type in {"create", "update"}
            rows = conn.execute(
                (
                    "SELECT * FROM relations WHERE source=?"
                    if replacing_page
                    else "SELECT * FROM relations WHERE source=? OR target=?"
                ),
                (old_endpoint,) if replacing_page else (old_endpoint, old_endpoint),
            ).fetchall()
            if replacing_page:
                retained_keys: set[tuple[str, str, str]] = set()
                for relation in replacement_relations or []:
                    relation_type = relation.relation_type.value
                    retained_keys.add((relation.source, relation.target, relation_type))
                    if relation.is_symmetric and relation.source != relation.target:
                        retained_keys.add((relation.target, relation.source, relation_type))
                owned_rows = {
                    int(row["id"]): row
                    for row in rows
                    if (
                        str(row["source"]),
                        str(row["target"]),
                        str(row["relation_type"]),
                    )
                    not in retained_keys
                }
                for row in tuple(owned_rows.values()):
                    try:
                        relation_type = RelationType(str(row["relation_type"]))
                    except ValueError as exc:
                        raise RuntimeError(
                            "KG update encountered an unknown relation type"
                        ) from exc
                    if not RELATION_META.get(relation_type, {}).get("symmetric"):
                        continue
                    reverse_rows = conn.execute(
                        """SELECT * FROM relations
                           WHERE source=? AND target=? AND relation_type=?""",
                        (row["target"], old_endpoint, row["relation_type"]),
                    ).fetchall()
                    owned_rows.update({int(reverse["id"]): reverse for reverse in reverse_rows})
                rows = list(owned_rows.values())
            for row in rows:
                relation_id = int(row["id"])
                if mutation_type == "delete" or replacing_page:
                    if self._deferred_relation_embeddings is not None:
                        self._deferred_relation_embeddings.pop(relation_id, None)
                    embedding = (
                        conn.execute(
                            "SELECT id FROM relation_context_embeddings WHERE relation_id=?",
                            (relation_id,),
                        ).fetchone()
                        if "relation_context_embeddings" in tables
                        else None
                    )
                    self._queue_embedding_operation(
                        conn,
                        relation_id,
                        "delete",
                        hnsw_id=int(embedding["id"]) if embedding is not None else None,
                    )
                    self._delete_relation_projection_rows(conn, tables, relation_id)
                    relations_deleted += 1
                    continue
                new_source = new_endpoint if row["source"] == old_endpoint else row["source"]
                new_target = new_endpoint if row["target"] == old_endpoint else row["target"]
                duplicate = conn.execute(
                    """SELECT id FROM relations
                       WHERE source=? AND target=? AND relation_type=? AND id<>?""",
                    (new_source, new_target, row["relation_type"], relation_id),
                ).fetchone()
                if duplicate:
                    duplicate_id = int(duplicate["id"])
                    if "relation_evidence" in tables:
                        conn.execute(
                            "UPDATE relation_evidence SET relation_id=? WHERE relation_id=?",
                            (duplicate_id, relation_id),
                        )
                    embedding = (
                        conn.execute(
                            "SELECT id FROM relation_context_embeddings WHERE relation_id=?",
                            (relation_id,),
                        ).fetchone()
                        if "relation_context_embeddings" in tables
                        else None
                    )
                    self._queue_embedding_operation(
                        conn,
                        relation_id,
                        "delete",
                        hnsw_id=int(embedding["id"]) if embedding is not None else None,
                    )
                    self._delete_relation_projection_rows(conn, tables, relation_id)
                    relations_deleted += 1
                    continue
                context = str(row["context"] or "").replace(old_endpoint, new_endpoint)
                conn.execute(
                    """UPDATE relations
                       SET source=?, target=?, context=?, updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (new_source, new_target, context, relation_id),
                )
                if "relations_fts" in tables:
                    conn.execute("DELETE FROM relations_fts WHERE rowid=?", (relation_id,))
                    conn.execute(
                        "INSERT INTO relations_fts(rowid, content) VALUES (?, ?)",
                        (relation_id, context),
                    )
                if "relation_context_embeddings" in tables:
                    conn.execute(
                        "DELETE FROM relation_context_embeddings WHERE relation_id=?",
                        (relation_id,),
                    )
                self._queue_embedding_operation(conn, relation_id, "upsert", context=context)
                relations_updated += 1
            conn.commit()
        embedding_result = (
            {
                "pending": relations_deleted,
                "repaired": 0,
                "failed": 0,
            }
            if self._deferred_relation_embeddings is not None
            else self.repair_relation_embedding_outbox()
        )
        if embedding_result["failed"]:
            raise RuntimeError(
                "KG lifecycle embedding reconciliation failed: " f"{embedding_result['failed']}"
            )
        return {
            "mutation_type": mutation_type,
            "entities_updated": entities_updated,
            "relations_updated": relations_updated,
            "relations_deleted": relations_deleted,
            "projection_errors": 0,
        }

    @staticmethod
    def _delete_relation_projection_rows(conn: Any, tables: set[str], relation_id: int) -> None:
        if "relation_evidence" in tables:
            conn.execute("DELETE FROM relation_evidence WHERE relation_id=?", (relation_id,))
        if "relations_fts" in tables:
            conn.execute("DELETE FROM relations_fts WHERE rowid=?", (relation_id,))
        if "relation_context_embeddings" in tables:
            conn.execute(
                "DELETE FROM relation_context_embeddings WHERE relation_id=?", (relation_id,)
            )
        conn.execute("DELETE FROM relations WHERE id=?", (relation_id,))

    @staticmethod
    def _reconcile_entity_sources(
        conn: Any,
        *,
        tables: set[str],
        mutation_type: str,
        old_absolute: str,
        old_endpoint: str,
        new_absolute: str,
        new_endpoint: str,
    ) -> int:
        if "entity_sources" not in tables:
            if mutation_type == "move":
                cursor = conn.execute(
                    """UPDATE entities SET source_page=?, last_updated=CURRENT_TIMESTAMP
                       WHERE source_page IN (?, ?)""",
                    (new_absolute, old_absolute, old_endpoint),
                )
            else:
                cursor = conn.execute(
                    """UPDATE entities SET source_page='', status='source_missing',
                           source_count=MAX(source_count - 1, 0), last_updated=CURRENT_TIMESTAMP
                       WHERE source_page IN (?, ?)""",
                    (old_absolute, old_endpoint),
                )
            return int(cursor.rowcount or 0)
        rows = conn.execute(
            """SELECT entity_uid, source_page, first_seen, last_seen
               FROM entity_sources WHERE source_page IN (?, ?)""",
            (old_absolute, old_endpoint),
        ).fetchall()
        affected = {str(row["entity_uid"]) for row in rows}
        if mutation_type == "move":
            for row in rows:
                replacement = new_absolute if row["source_page"] == old_absolute else new_endpoint
                conn.execute(
                    """INSERT INTO entity_sources(entity_uid, source_page, first_seen, last_seen)
                       VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                       ON CONFLICT(entity_uid, source_page) DO UPDATE SET last_seen=excluded.last_seen""",
                    (row["entity_uid"], replacement, row["first_seen"]),
                )
        conn.execute(
            "DELETE FROM entity_sources WHERE source_page IN (?, ?)",
            (old_absolute, old_endpoint),
        )
        for uid in affected:
            remaining = [
                str(row[0])
                for row in conn.execute(
                    """SELECT source_page FROM entity_sources
                       WHERE entity_uid=? ORDER BY first_seen, source_page""",
                    (uid,),
                ).fetchall()
            ]
            entity = conn.execute(
                "SELECT source_page, status FROM entities WHERE uid=?", (uid,)
            ).fetchone()
            if entity is None:
                continue
            primary = str(entity["source_page"] or "")
            if primary in {old_absolute, old_endpoint}:
                primary = (
                    new_absolute
                    if mutation_type == "move" and primary == old_absolute
                    else (
                        new_endpoint
                        if mutation_type == "move"
                        else remaining[0] if remaining else ""
                    )
                )
            status = "source_missing" if not remaining else str(entity["status"] or "active")
            conn.execute(
                """UPDATE entities SET source_page=?, source_count=?, status=?,
                       last_updated=CURRENT_TIMESTAMP WHERE uid=?""",
                (primary, len(remaining), status, uid),
            )
        return len(affected)

    def normalize_entity_primary_sources(self: Any) -> int:
        """Derive each primary source deterministically from its canonical source set."""

        changed = 0
        with self._conn() as conn:
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if not {"entities", "entity_sources"} <= tables:
                return 0
            rows = conn.execute("""SELECT entity.uid, entity.source_page,
                          entity.source_count AS current_source_count,
                          entity.status, MIN(source.source_page) AS primary_source,
                          COUNT(source.source_page) AS derived_source_count
                   FROM entities AS entity
                   LEFT JOIN entity_sources AS source ON source.entity_uid=entity.uid
                   GROUP BY entity.uid""").fetchall()
            for row in rows:
                primary = str(row["primary_source"] or "")
                source_count = int(row["derived_source_count"] or 0)
                current_status = str(row["status"] or "active")
                status = (
                    "source_missing"
                    if not source_count
                    else "active" if current_status == "source_missing" else current_status
                )
                if (
                    str(row["source_page"] or "") == primary
                    and int(row["current_source_count"] or 0) == source_count
                    and current_status == status
                ):
                    continue
                conn.execute(
                    """UPDATE entities SET source_page=?, source_count=?, status=?,
                              last_updated=CURRENT_TIMESTAMP WHERE uid=?""",
                    (primary, source_count, status, row["uid"]),
                )
                changed += 1
            conn.commit()
        return changed
