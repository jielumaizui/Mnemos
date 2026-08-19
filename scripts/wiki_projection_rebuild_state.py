"""Deterministic state snapshots and artifact reset for Wiki projection rebuilds."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from core.db_utils import render_sql
from core.kia.relation_endpoint_quality import is_derived_kg_scan_path
from core.kia.relation_schema import RelationType, infer_symmetric_type
from core.wiki_navigation import NAV_DIR


VOLATILE_COLUMNS = {
    "created_at",
    "updated_at",
    "generated_at",
    "last_updated",
    "last_accessed",
    "first_seen",
    "last_seen",
    "last_calculated",
}
SNAPSHOT_TABLES = frozenset(
    {
        "entities",
        "entity_aliases",
        "entity_sources",
        "relations",
        "relation_evidence",
        "relation_stats",
        "cognitive_relations",
        "page_metrics",
    }
)


def _connect_read_only(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite source without creating schema or journal sidecars."""

    resolved = db_path.expanduser().resolve(strict=True)
    wal_path = Path(f"{resolved}-wal")
    query = (
        "?mode=ro"
        if wal_path.is_file() and wal_path.stat().st_size
        else "?mode=ro&immutable=1"
    )
    conn = sqlite3.connect(resolved.as_uri() + query, uri=True, timeout=30)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _replace_snapshot_paths(
    value: Any, replacements: tuple[tuple[str, str], ...]
) -> Any:
    if not isinstance(value, str):
        return value
    for source, target in replacements:
        value = value.replace(source, target)
    return value


def _normalize_keyword_order(text: Any) -> Any:
    if not isinstance(text, str) or "共同关键词:" not in text:
        return text

    def replace(match: re.Match[str]) -> str:
        values = sorted(
            item.strip() for item in match.group(1).split(",") if item.strip()
        )
        return "共同关键词: " + ", ".join(values)

    return re.sub(r"共同关键词:\s*([^；\n]+)", replace, text)


def _normalize_relation_orientation(item: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize symmetric endpoints without touching surrogate identity."""

    try:
        symmetric = infer_symmetric_type(RelationType(str(item.get("relation_type"))))
    except ValueError:
        symmetric = False
    if symmetric:
        old_source = str(item.get("source", ""))
        old_target = str(item.get("target", ""))
        source, target = sorted((old_source, old_target))
        item["source"], item["target"] = source, target
        if "context" in item:
            context = str(item.get("context", ""))
            item["context"] = context.replace(
                f"{old_source} 与 {old_target}", f"{source} 与 {target}", 1
            )
    return item


def _normalize_snapshot_row(table: str, item: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize set-like relation fields without hiding factual differences."""

    if table == "relation_evidence":
        item["content"] = _normalize_keyword_order(item.get("content"))
    if table != "relations":
        return item
    item["context"] = _normalize_keyword_order(item.get("context"))
    return _normalize_relation_orientation(item)


def _table_snapshot(
    db_path: Path,
    table: str,
    *,
    path_replacements: tuple[tuple[str, str], ...] = (),
) -> dict[str, Any]:
    """Hash a fixed allowlisted projection table excluding volatile timestamps."""

    if table not in SNAPSHOT_TABLES:
        raise ValueError(f"unsupported projection snapshot table: {table}")
    if not db_path.exists():
        return {"table": table, "rows": 0, "sha256": hashlib.sha256(b"").hexdigest()}
    with _connect_read_only(db_path) as conn:
        conn.row_factory = sqlite3.Row
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            return {
                "table": table,
                "rows": 0,
                "sha256": hashlib.sha256(b"").hexdigest(),
            }
        query = f'SELECT * FROM "{table}"'  # nosec B608
        if table == "knowledge_profiles":
            query += " ORDER BY id DESC LIMIT 1"
        rows = conn.execute(query).fetchall()
        evidence_semantic_rows: list[dict[str, Any]] | None = None
        if table == "relation_evidence":
            has_relations = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='relations'"
            ).fetchone()
            if has_relations:
                evidence_semantic_rows = [
                    {
                        "source": source,
                        "target": target,
                        "relation_type": relation_type,
                        "evidence_type": evidence_type,
                        "content": content,
                    }
                    for source, target, relation_type, evidence_type, content in conn.execute(
                        """SELECT relation.source, relation.target,
                                  relation.relation_type, evidence.evidence_type,
                                  evidence.content
                           FROM relation_evidence AS evidence
                           JOIN relations AS relation
                             ON relation.id=evidence.relation_id"""
                    )
                ]
    normalized = []
    semantic_normalized = []
    for row in rows:
        item = {
            key: _replace_snapshot_paths(row[key], path_replacements)
            for key in row.keys()
            if key not in VOLATILE_COLUMNS
            and not (table == "knowledge_profiles" and key == "id")
        }
        normalized_item = _normalize_snapshot_row(table, item)
        normalized.append(normalized_item)
        if evidence_semantic_rows is None:
            semantic_item = dict(normalized_item)
            if table == "relations":
                semantic_item.pop("id", None)
            semantic_normalized.append(semantic_item)
    if evidence_semantic_rows is not None:
        semantic_normalized = [
            _normalize_relation_orientation(
                {
                    **item,
                    "content": _normalize_keyword_order(item.get("content")),
                }
            )
            for item in evidence_semantic_rows
        ]
    normalized.sort(
        key=lambda item: json.dumps(
            item, ensure_ascii=False, sort_keys=True, default=str
        )
    )
    semantic_normalized.sort(
        key=lambda item: json.dumps(
            item, ensure_ascii=False, sort_keys=True, default=str
        )
    )
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    semantic_encoded = json.dumps(
        semantic_normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return {
        "table": table,
        "rows": len(rows),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "semantic_sha256": hashlib.sha256(semantic_encoded).hexdigest(),
    }


def _wiki_mutation_cursor(db_path: Path) -> int:
    """Return the current append-only Wiki mutation sequence without writes."""

    with _connect_read_only(db_path) as conn:
        return int(
            conn.execute(
                "SELECT COALESCE(MAX(sequence_no), 0) FROM wiki_mutations"
            ).fetchone()[0]
        )


def _mutation_prefix_snapshot(
    db_path: Path,
    *,
    through_sequence: int | None = None,
) -> dict[str, Any]:
    """Hash an immutable Wiki-mutation ledger prefix without writable access."""

    if not db_path.is_file():
        raise FileNotFoundError(f"Wiki projection ledger is missing: {db_path}")
    uri = db_path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=30) as conn:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise ValueError(f"Wiki projection ledger integrity failed: {db_path}")
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='wiki_mutations'"
        ).fetchone()
        if table_exists is None:
            raise ValueError(
                f"Wiki projection ledger has no wiki_mutations table: {db_path}"
            )
        columns = [
            str(row[1]) for row in conn.execute("PRAGMA table_info(wiki_mutations)")
        ]
        where = "" if through_sequence is None else " WHERE sequence_no <= ?"
        parameters: tuple[Any, ...] = (
            () if through_sequence is None else (int(through_sequence),)
        )
        query = render_sql(
            "SELECT * FROM wiki_mutations{where} ORDER BY sequence_no",
            fixed_fragments={
                "where": (where, ("", " WHERE sequence_no <= ?")),
            },
        )
        rows = [
            list(row)
            for row in conn.execute(
                query,
                parameters,
            )
        ]
        max_sequence = int(
            conn.execute(
                "SELECT COALESCE(MAX(sequence_no), 0) FROM wiki_mutations"
            ).fetchone()[0]
        )
    encoded = json.dumps(
        {"columns": columns, "rows": rows},
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return {
        "rows": len(rows),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "max_sequence": max_sequence,
    }


def _verified_resume_baseline(
    *,
    backup_dir: Path,
    live_ledger_path: Path,
) -> dict[str, Any]:
    """Prove a rebuild backup is the exact immutable prefix of live state."""

    backup_dir = backup_dir.expanduser().resolve(strict=False)
    if not (backup_dir / "wiki-prestate").is_dir():
        raise FileNotFoundError(
            f"isolated comparator prestate is missing: {backup_dir / 'wiki-prestate'}"
        )
    backup_ledger = backup_dir / "wiki_projection.db"
    backup = _mutation_prefix_snapshot(backup_ledger)
    live = _mutation_prefix_snapshot(live_ledger_path)
    if live["max_sequence"] < backup["max_sequence"]:
        raise ValueError("rebuild resume live ledger is behind the backup baseline")
    live_prefix = _mutation_prefix_snapshot(
        live_ledger_path,
        through_sequence=backup["max_sequence"],
    )
    if (
        live_prefix["rows"] != backup["rows"]
        or live_prefix["sha256"] != backup["sha256"]
    ):
        raise ValueError(
            "rebuild resume rejected because the backup is not an immutable prefix "
            "of the live Wiki mutation ledger"
        )
    return {
        "backup_dir": str(backup_dir),
        "backup_ledger": str(backup_ledger),
        "baseline_sequence": backup["max_sequence"],
        "baseline_rows": backup["rows"],
        "baseline_sha256": backup["sha256"],
        "live_sequence": live["max_sequence"],
        "live_rows": live["rows"],
    }


def _consume_derived_mutations_after(
    db_path: Path,
    *,
    after_sequence: int,
    wiki_dir: Path,
    consume: Any,
) -> int:
    """Drain canonical KG output mutations created inside a controlled cycle."""

    with _connect_read_only(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM wiki_mutations WHERE sequence_no > ? ORDER BY sequence_no",
            (int(after_sequence),),
        ).fetchall()
    for row in rows:
        mutation = dict(row)
        page_path = Path(str(mutation.get("page_path") or ""))
        previous = Path(str(mutation.get("previous_path") or ""))
        if not (
            is_derived_kg_scan_path(page_path, wiki_dir)
            or (
                str(mutation.get("previous_path") or "")
                and is_derived_kg_scan_path(previous, wiki_dir)
            )
        ):
            raise RuntimeError(
                "controlled KG cycle appended a non-derived Wiki mutation: "
                f"sequence={mutation.get('sequence_no')} path={page_path}"
            )
        consume(mutation)
    return len(rows)


def _directory_snapshot(path: Path) -> dict[str, Any]:
    """Hash relative paths and bytes for a deterministic generated directory."""

    rows: list[dict[str, Any]] = []
    if path.is_dir():
        for item in sorted(
            candidate for candidate in path.rglob("*") if candidate.is_file()
        ):
            rows.append(
                {
                    "path": item.relative_to(path).as_posix(),
                    "sha256": hashlib.sha256(item.read_bytes()).hexdigest(),
                    "size_bytes": item.stat().st_size,
                }
            )
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "path": str(path),
        "files": len(rows),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _json_snapshot(
    path: Path,
    *,
    ignored_keys: frozenset[str] = frozenset(),
    path_replacements: tuple[tuple[str, str], ...] = (),
) -> dict[str, Any]:
    """Hash JSON after recursively removing explicitly volatile object keys."""

    if not path.is_file():
        return {"path": str(path), "rows": 0, "sha256": hashlib.sha256(b"").hexdigest()}

    def normalize(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: normalize(item)
                for key, item in sorted(value.items())
                if key not in ignored_keys
            }
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return _replace_snapshot_paths(value, path_replacements)

    normalized = normalize(json.loads(path.read_text(encoding="utf-8")))
    encoded = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "path": str(path),
        "rows": len(normalized) if isinstance(normalized, dict) else 1,
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _reset_projection_artifacts(cfg: Any) -> dict[str, Any]:
    """Delete only rebuildable projection stores after their backup is complete."""

    database_dir = Path(cfg.database_dir)
    bases = (database_dir / "knowledge_graph.db", database_dir / "wiki_metrics.db")
    targets = [
        *bases,
        *(Path(f"{base}{suffix}") for base in bases for suffix in ("-wal", "-shm")),
        database_dir / "embedding_index" / "relation_index.bin",
        database_dir / "embedding_index" / "wiki_index.bin",
        database_dir / "embedding_index" / "wiki_meta.json",
    ]
    removed: list[str] = []
    for path in targets:
        if path.exists():
            path.unlink()
            removed.append(str(path))
    return {
        "removed": removed,
        "preserved": [
            str(database_dir / "wiki_projection.db"),
            str(database_dir / "cognitive_graph.db"),
        ],
    }


def _relation_embedding_rows(
    conn: sqlite3.Connection,
) -> list[tuple[str, int, Any, str]]:
    """Return vectors keyed by the stable relation identity when available."""

    has_relations = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='relations'"
    ).fetchone()
    if has_relations:
        rows = conn.execute(
            """SELECT relation.source, relation.target, relation.relation_type,
                      embedding.relation_id, embedding.embedding,
                      embedding.model_version
               FROM relation_context_embeddings AS embedding
               JOIN relations AS relation ON relation.id=embedding.relation_id"""
        ).fetchall()
        return sorted(
            (
                "\0".join((str(source), str(target), str(relation_type))),
                int(relation_id),
                embedding,
                str(model_version),
            )
            for source, target, relation_type, relation_id, embedding, model_version in rows
        )
    return [
        (f"relation_id:{int(relation_id):020d}", int(relation_id), embedding, str(model))
        for relation_id, embedding, model in conn.execute(
            """SELECT relation_id, embedding, model_version
               FROM relation_context_embeddings ORDER BY relation_id"""
        )
    ]


def _embedding_snapshot(db_path: Path) -> dict[str, Any]:
    """Hash relation vectors without materializing the full table in memory."""

    table = "relation_context_embeddings"
    if not db_path.exists():
        return {"table": table, "rows": 0, "sha256": hashlib.sha256(b"").hexdigest()}
    digest = hashlib.sha256()
    semantic_digest = hashlib.sha256()
    structure_digest = hashlib.sha256()
    row_count = 0
    with _connect_read_only(db_path) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            return {"table": table, "rows": 0, "sha256": digest.hexdigest()}
        for business_key, relation_id, embedding, model_version in _relation_embedding_rows(
            conn
        ):
            row_count += 1
            for value in (relation_id, embedding, model_version):
                data = (
                    value
                    if isinstance(value, bytes)
                    else str(value or "").encode("utf-8")
                )
                digest.update(len(data).to_bytes(8, "big"))
                digest.update(data)
            try:
                vector = [float(item) for item in json.loads(embedding)]
                norm = sum(item**2 for item in vector) ** 0.5
                direction = (
                    tuple(round(item / norm, 4) for item in vector)
                    if norm
                    else tuple(0.0 for _item in vector)
                )
                structure = (business_key, model_version, len(vector))
                semantic = (*structure, direction)
                structure_digest.update(repr(structure).encode("utf-8"))
                semantic_digest.update(repr(semantic).encode("utf-8"))
            except (ValueError, TypeError, json.JSONDecodeError):
                semantic_digest.update(f"invalid:{business_key}".encode("utf-8"))
    return {
        "table": table,
        "rows": row_count,
        "sha256": digest.hexdigest(),
        "structure_sha256": structure_digest.hexdigest(),
        "semantic_sha256": semantic_digest.hexdigest(),
    }


def _relation_embedding_semantic_comparison(
    expected_db: Path,
    actual_db: Path,
    *,
    cosine_threshold: float = 0.99,
) -> dict[str, Any]:
    """Compare matched relation-vector directions with an explicit tolerance."""

    def rows(conn: sqlite3.Connection) -> Any:
        return iter(_relation_embedding_rows(conn))

    matched = missing = orphan = incompatible = below_threshold = invalid = 0
    minimum_cosine = 1.0
    with _connect_read_only(expected_db) as expected_conn, _connect_read_only(
        actual_db
    ) as actual_conn:
        expected_iter = rows(expected_conn)
        actual_iter = rows(actual_conn)
        expected = next(expected_iter, None)
        actual = next(actual_iter, None)
        while expected is not None or actual is not None:
            if actual is None or (
                expected is not None and str(expected[0]) < str(actual[0])
            ):
                missing += 1
                expected = next(expected_iter, None)
                continue
            if expected is None or str(actual[0]) < str(expected[0]):
                orphan += 1
                actual = next(actual_iter, None)
                continue
            matched += 1
            try:
                expected_vector = [float(item) for item in json.loads(expected[2])]
                actual_vector = [float(item) for item in json.loads(actual[2])]
                if str(expected[3]) != str(actual[3]) or len(expected_vector) != len(
                    actual_vector
                ):
                    incompatible += 1
                else:
                    expected_norm = math.sqrt(
                        sum(item * item for item in expected_vector)
                    )
                    actual_norm = math.sqrt(sum(item * item for item in actual_vector))
                    if not expected_norm or not actual_norm:
                        cosine = 1.0 if expected_norm == actual_norm else 0.0
                    else:
                        cosine = sum(
                            left * right
                            for left, right in zip(expected_vector, actual_vector)
                        ) / (expected_norm * actual_norm)
                    minimum_cosine = min(minimum_cosine, cosine)
                    if cosine < cosine_threshold:
                        below_threshold += 1
            except (TypeError, ValueError, json.JSONDecodeError):
                invalid += 1
            expected = next(expected_iter, None)
            actual = next(actual_iter, None)
    return {
        "schema_version": "mnemos.relation_embedding_semantic_comparison.v1",
        "cosine_threshold": cosine_threshold,
        "matched": matched,
        "missing": missing,
        "orphan": orphan,
        "incompatible": incompatible,
        "invalid": invalid,
        "below_threshold": below_threshold,
        "minimum_cosine": round(minimum_cosine, 9) if matched else None,
        "equal": not any((missing, orphan, incompatible, invalid, below_threshold)),
    }


def _relation_embedding_coverage(db_path: Path, index_path: Path) -> dict[str, Any]:
    """Verify every KG relation has one embedding row and a persisted ANN index."""

    if not db_path.is_file():
        return {
            "relations": 0,
            "embeddings": 0,
            "missing_embeddings": 0,
            "orphan_embeddings": 0,
            "index_exists": False,
            "ok": False,
        }
    with _connect_read_only(db_path) as conn:
        relation_count = int(conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0])
        embedding_count = int(
            conn.execute("SELECT COUNT(*) FROM relation_context_embeddings").fetchone()[0]
        )
        missing = int(
            conn.execute(
                """SELECT COUNT(*) FROM relations AS relation
                   LEFT JOIN relation_context_embeddings AS embedding
                     ON embedding.relation_id=relation.id
                   WHERE embedding.relation_id IS NULL"""
            ).fetchone()[0]
        )
        orphan = int(
            conn.execute(
                """SELECT COUNT(*) FROM relation_context_embeddings AS embedding
                   LEFT JOIN relations AS relation ON relation.id=embedding.relation_id
                   WHERE relation.id IS NULL"""
            ).fetchone()[0]
        )
    index_exists = index_path.is_file() if embedding_count else True
    return {
        "relations": relation_count,
        "embeddings": embedding_count,
        "missing_embeddings": missing,
        "orphan_embeddings": orphan,
        "index_exists": index_exists,
        "ok": not missing
        and not orphan
        and relation_count == embedding_count
        and index_exists,
    }


def _file_snapshot(path: Path) -> dict[str, Any]:
    """Stream-hash a binary projection artifact."""

    digest = hashlib.sha256()
    size = 0
    if path.is_file():
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
    return {"path": str(path), "size_bytes": size, "sha256": digest.hexdigest()}


def _semantic_projection_hash(tables: dict[str, Any]) -> str:
    """Hash logical projection state while retaining raw artifact hashes as evidence."""

    logical: dict[str, Any] = {}
    nondeterministic_binary = {"relation_hnsw", "wiki_search_hnsw"}
    for name, snapshot in tables.items():
        if name == "relation_embeddings":
            logical[name] = {
                "rows": snapshot["rows"],
                "structure_sha256": snapshot["structure_sha256"],
            }
        elif name in nondeterministic_binary:
            logical[name] = {"present": int(snapshot.get("size_bytes", 0)) > 0}
        elif "semantic_sha256" in snapshot:
            logical[name] = {
                "rows": snapshot.get("rows", 0),
                "semantic_sha256": snapshot["semantic_sha256"],
            }
        else:
            logical[name] = {
                key: value for key, value in snapshot.items() if key != "path"
            }
    encoded = json.dumps(
        logical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _projection_state(cfg: Any) -> dict[str, Any]:
    """Return a deterministic snapshot of every required projection endpoint."""

    database_dir = Path(cfg.database_dir)
    wiki_dir = Path(cfg.wiki_dir)
    index_dir = database_dir / "embedding_index"
    replacements = (
        (str(wiki_dir.resolve(strict=False)), "$WIKI"),
        (str(database_dir.resolve(strict=False)), "$DATABASE"),
        *tuple(getattr(cfg, "snapshot_path_replacements", ())),
    )
    tables = {
        "kg_entities": _table_snapshot(
            database_dir / "knowledge_graph.db",
            "entities",
            path_replacements=replacements,
        ),
        "kg_entity_aliases": _table_snapshot(
            database_dir / "knowledge_graph.db",
            "entity_aliases",
            path_replacements=replacements,
        ),
        "kg_entity_sources": _table_snapshot(
            database_dir / "knowledge_graph.db",
            "entity_sources",
            path_replacements=replacements,
        ),
        "kg_relations": _table_snapshot(
            database_dir / "knowledge_graph.db",
            "relations",
            path_replacements=replacements,
        ),
        "kg_relation_evidence": _table_snapshot(
            database_dir / "knowledge_graph.db",
            "relation_evidence",
            path_replacements=replacements,
        ),
        "kg_relation_stats": _table_snapshot(
            database_dir / "knowledge_graph.db",
            "relation_stats",
            path_replacements=replacements,
        ),
        "relation_embeddings": _embedding_snapshot(database_dir / "knowledge_graph.db"),
        "relation_hnsw": _file_snapshot(index_dir / "relation_index.bin"),
        "relation_embedding_coverage": _relation_embedding_coverage(
            database_dir / "knowledge_graph.db", index_dir / "relation_index.bin"
        ),
        "wiki_search_meta": _json_snapshot(
            index_dir / "wiki_meta.json",
            ignored_keys=frozenset({"mtime"}),
            path_replacements=replacements,
        ),
        "wiki_search_hnsw": _file_snapshot(index_dir / "wiki_index.bin"),
        "cognitive_relations": _table_snapshot(
            database_dir / "cognitive_graph.db",
            "cognitive_relations",
            path_replacements=replacements,
        ),
        "wiki_metrics": _table_snapshot(
            database_dir / "wiki_metrics.db",
            "page_metrics",
            path_replacements=replacements,
        ),
        "moc_navigation": _directory_snapshot(wiki_dir / NAV_DIR),
    }
    hash_tables = {
        name: {key: value for key, value in snapshot.items() if key != "path"}
        for name, snapshot in tables.items()
    }
    payload = json.dumps(
        hash_tables, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return {
        "schema_version": "mnemos.wiki_projection_snapshot.v1",
        "tables": tables,
        "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "semantic_sha256": _semantic_projection_hash(tables),
    }


def _materialize_incremental_mutation(
    mutation: dict[str, Any],
    *,
    target_path: Path,
    previous_path: Path,
    preserve_managed_target: bool = False,
) -> None:
    """Apply one lifecycle step to an isolated Wiki prestate."""

    mutation_type = str(mutation["mutation_type"])
    has_previous = bool(str(mutation.get("previous_path", "")))
    if preserve_managed_target:
        if has_previous and previous_path.is_file() and previous_path != target_path:
            previous_path.unlink()
        return
    if mutation_type == "delete":
        if has_previous and previous_path.is_file() and previous_path != target_path:
            previous_path.unlink()
        if target_path.is_file():
            target_path.unlink()
        return
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if (
        mutation_type == "move"
        and has_previous
        and previous_path.is_file()
        and previous_path != target_path
    ):
        previous_path.replace(target_path)
    source_path = Path(str(mutation["page_path"]))
    if source_path.is_file() and source_path.resolve() != target_path.resolve():
        shutil.copy2(source_path, target_path)


__all__ = [
    "_directory_snapshot",
    "_embedding_snapshot",
    "_consume_derived_mutations_after",
    "_materialize_incremental_mutation",
    "_mutation_prefix_snapshot",
    "_projection_state",
    "_relation_embedding_coverage",
    "_relation_embedding_semantic_comparison",
    "_reset_projection_artifacts",
    "_semantic_projection_hash",
    "_table_snapshot",
    "_wiki_mutation_cursor",
    "_verified_resume_baseline",
]
