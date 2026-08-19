# -*- coding: utf-8 -*-
"""KG endpoint semantic normalization and path migration helpers."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.db_utils import sqlite_conn
from core.kia.entity_manager import EntityManager
from core.kia.kg_consistency import audit_kg_consistency
from core.kia.relation_endpoint_quality import prunable_relation_endpoint_reason

SCHEMA_VERSION = "mnemos.kg_endpoint_normalization.v1"

DEFAULT_WIKI_TOP_LEVELS = frozenset(
    {
        "00-Inbox",
        "01-People",
        "02-Projects",
        "03-Tech",
        "04-Concepts",
        "05-MOCs",
    }
)

INVALID_ENDPOINTS = frozenset({"", "---"})


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'virtual table') AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _backup_database(db_path: Path, backup_root: Path | None = None) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    root = backup_root or db_path.parent / "backups" / "kg-endpoints"
    root.mkdir(parents=True, exist_ok=True)
    backup_path = root / f"{db_path.stem}-{timestamp}.db"
    with sqlite3.connect(str(db_path), timeout=30) as source:
        with sqlite3.connect(str(backup_path), timeout=30) as target:
            source.backup(target)
    return backup_path


def _endpoint_exists_in_wiki(endpoint: str, wiki_base: Path | None) -> bool:
    if not endpoint or wiki_base is None:
        return False
    candidates = [wiki_base / endpoint]
    if not endpoint.endswith(".md"):
        candidates.append(wiki_base / f"{endpoint}.md")
    return any(candidate.exists() for candidate in candidates)


def _is_indexable_wiki_page(path: Path, wiki_base: Path) -> bool:
    rel = path.relative_to(wiki_base)
    if any(part.startswith(".") for part in rel.parts):
        return False
    if not rel.parts:
        return False
    top = rel.parts[0]
    return top in DEFAULT_WIKI_TOP_LEVELS or len(rel.parts) == 1


def _wiki_stem_index(wiki_base: Path | None) -> dict[str, set[str]]:
    if wiki_base is None or not wiki_base.exists():
        return {}
    index: dict[str, set[str]] = {}
    for page in wiki_base.rglob("*.md"):
        try:
            if not _is_indexable_wiki_page(page, wiki_base):
                continue
            rel = str(page.relative_to(wiki_base))
        except ValueError:
            continue
        for key in {page.name, page.stem}:
            if key:
                index.setdefault(key, set()).add(rel)
    return index


def _endpoint_lookup_variants(endpoint: str) -> set[str]:
    stripped_endpoint = endpoint.strip()
    path = Path(stripped_endpoint)
    stem = path.stem if stripped_endpoint.endswith(".md") or "/" in stripped_endpoint else stripped_endpoint
    variants = {path.name, stem}
    stripped = re.sub(r"^[0-9a-f]{8}_", "", stem)
    stripped = re.sub(r"^session__", "", stripped)
    variants.add(stripped)
    return {variant for variant in variants if variant}


def _unique_wiki_candidate(endpoint: str, stem_index: dict[str, set[str]]) -> str | None:
    matches: set[str] = set()
    for variant in _endpoint_lookup_variants(endpoint):
        matches.update(stem_index.get(variant, set()))
    if len(matches) == 1:
        return next(iter(matches))
    return None


def _is_clean_concept_endpoint(endpoint: str, *, min_refs: int, ref_count: int) -> bool:
    text = endpoint.strip()
    if ref_count < min_refs:
        return False
    if text in INVALID_ENDPOINTS:
        return False
    if any(char in text for char in ("/", "\\", "\n", "\r")):
        return False
    if text.startswith(".") or text.endswith(".md"):
        return False
    if re.match(r"^[0-9a-f]{8}_", text) or text.startswith("session__"):
        return False
    return EntityManager._is_valid_entity_name(text)


def _endpoint_ref_counts(conn: sqlite3.Connection) -> tuple[dict[str, int], dict[str, set[str]]]:
    refs: dict[str, int] = {}
    methods: dict[str, set[str]] = {}
    for column in ("source", "target"):
        for row in conn.execute(
            f"""SELECT {column}, COUNT(*) AS count, GROUP_CONCAT(DISTINCT source_method)
                FROM relations
                WHERE {column} IS NOT NULL AND TRIM({column}) <> ''
                GROUP BY {column}"""  # nosec B608: fixed column names
        ):
            endpoint = str(row[0])
            refs[endpoint] = refs.get(endpoint, 0) + int(row[1])
            method_set = methods.setdefault(endpoint, set())
            for method in str(row[2] or "").split(","):
                if method:
                    method_set.add(method)
    return refs, methods


def _missing_endpoints(
    conn: sqlite3.Connection,
    *,
    wiki_base: Path | None,
) -> tuple[list[str], dict[str, int], dict[str, set[str]]]:
    if not (_table_exists(conn, "relations") and _table_exists(conn, "entities")):
        return [], {}, {}
    refs, methods = _endpoint_ref_counts(conn)
    entity_rows = conn.execute("SELECT name, uid FROM entities").fetchall()
    entity_names = {str(row[0]) for row in entity_rows if row[0]}
    entity_uids = {str(row[1]) for row in entity_rows if row[1]}
    missing = [
        endpoint
        for endpoint in sorted(refs)
        if endpoint not in entity_names
        and endpoint not in entity_uids
        and not _endpoint_exists_in_wiki(endpoint, wiki_base)
    ]
    return missing, refs, methods


def _sample(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    return items[: max(0, limit)]


def _classify_endpoints(
    conn: sqlite3.Connection,
    *,
    wiki_base: Path | None,
    sample_limit: int,
    min_concept_refs: int,
) -> dict[str, Any]:
    missing, refs, methods = _missing_endpoints(conn, wiki_base=wiki_base)
    stem_index = _wiki_stem_index(wiki_base)
    path_migrations: list[dict[str, Any]] = []
    concept_entities: list[dict[str, Any]] = []
    invalid_endpoints: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    noise: list[dict[str, Any]] = []

    for endpoint in missing:
        ref_count = refs.get(endpoint, 0)
        method_list = sorted(methods.get(endpoint, set()))
        prune_reason = prunable_relation_endpoint_reason(endpoint)
        if prune_reason:
            invalid_endpoints.append(
                {
                    "endpoint": endpoint,
                    "relation_refs": ref_count,
                    "methods": method_list,
                    "reason": prune_reason,
                }
            )
            continue

        target = _unique_wiki_candidate(endpoint, stem_index)
        if target is not None:
            path_migrations.append(
                {
                    "endpoint": endpoint,
                    "target": target,
                    "relation_refs": ref_count,
                    "methods": method_list,
                }
            )
            continue

        if _is_clean_concept_endpoint(
            endpoint,
            min_refs=min_concept_refs,
            ref_count=ref_count,
        ):
            concept_entities.append(
                {
                    "endpoint": endpoint,
                    "relation_refs": ref_count,
                    "methods": method_list,
                }
            )
            continue

        bucket = noise if endpoint in INVALID_ENDPOINTS else unresolved
        bucket.append(
            {
                "endpoint": endpoint,
                "relation_refs": ref_count,
                "methods": method_list,
            }
        )

    return {
        "missing_endpoint_count": len(missing),
        "path_migrations": path_migrations,
        "concept_entities": concept_entities,
        "invalid_endpoints": invalid_endpoints,
        "unresolved": unresolved,
        "noise": noise,
        "counts": {
            "path_migrations": len(path_migrations),
            "concept_entities": len(concept_entities),
            "invalid_endpoints": len(invalid_endpoints),
            "unresolved": len(unresolved),
            "noise": len(noise),
        },
        "samples": {
            "path_migrations": _sample(path_migrations, sample_limit),
            "concept_entities": _sample(concept_entities, sample_limit),
            "invalid_endpoints": _sample(invalid_endpoints, sample_limit),
            "unresolved": _sample(unresolved, sample_limit),
            "noise": _sample(noise, sample_limit),
        },
    }


def _relation_conflict_exists(
    conn: sqlite3.Connection,
    *,
    relation_id: int,
    source: str,
    target: str,
    relation_type: str,
) -> bool:
    row = conn.execute(
        """SELECT id FROM relations
           WHERE source=? AND target=? AND relation_type=? AND id<>?
           LIMIT 1""",
        (source, target, relation_type, relation_id),
    ).fetchone()
    return row is not None


def _rewrite_relation_fts(conn: sqlite3.Connection, relation_id: int) -> None:
    row = conn.execute(
        """SELECT source, target, relation_type, context
           FROM relations
           WHERE id=?""",
        (relation_id,),
    ).fetchone()
    if row is None or not _table_exists(conn, "relations_fts"):
        return
    content = f"{row[0]} {row[1]} {row[2]} {row[3] or ''}"
    conn.execute("DELETE FROM relations_fts WHERE rowid=?", (relation_id,))
    conn.execute("INSERT INTO relations_fts(rowid, content) VALUES (?, ?)", (relation_id, content))


def _apply_path_migrations(
    conn: sqlite3.Connection,
    migrations: list[dict[str, Any]],
) -> dict[str, Any]:
    mapping = {str(item["endpoint"]): str(item["target"]) for item in migrations}
    updated = 0
    fts_updated = 0
    skipped_conflicts: list[dict[str, Any]] = []
    skipped_self_relations: list[dict[str, Any]] = []
    if not mapping:
        return {
            "relations_updated": updated,
            "fts_updated": fts_updated,
            "skipped_conflicts": skipped_conflicts,
            "skipped_self_relations": skipped_self_relations,
        }

    candidate_rows = conn.execute("SELECT id, source, target, relation_type FROM relations").fetchall()
    rows = [row for row in candidate_rows if str(row[1]) in mapping or str(row[2]) in mapping]
    now = datetime.now(timezone.utc).isoformat()[:19]
    for row in rows:
        relation_id = int(row[0])
        source = str(row[1])
        target = str(row[2])
        relation_type = str(row[3])
        new_source = mapping.get(source, source)
        new_target = mapping.get(target, target)
        if new_source == source and new_target == target:
            continue
        if new_source == new_target:
            skipped_self_relations.append(
                {
                    "relation_id": relation_id,
                    "source": source,
                    "target": target,
                    "mapped_to": new_source,
                }
            )
            continue
        if _relation_conflict_exists(
            conn,
            relation_id=relation_id,
            source=new_source,
            target=new_target,
            relation_type=relation_type,
        ):
            skipped_conflicts.append(
                {
                    "relation_id": relation_id,
                    "source": source,
                    "target": target,
                    "mapped_source": new_source,
                    "mapped_target": new_target,
                    "relation_type": relation_type,
                }
            )
            continue
        conn.execute(
            "UPDATE relations SET source=?, target=?, updated_at=? WHERE id=?",
            (new_source, new_target, now, relation_id),
        )
        _rewrite_relation_fts(conn, relation_id)
        updated += 1
        fts_updated += 1

    return {
        "relations_updated": updated,
        "fts_updated": fts_updated,
        "skipped_conflicts": skipped_conflicts,
        "skipped_self_relations": skipped_self_relations,
    }


def _entity_uid(conn: sqlite3.Connection, name: str) -> str:
    base = EntityManager._slugify(name)
    row = conn.execute("SELECT name FROM entities WHERE uid=?", (base,)).fetchone()
    if row is None or str(row[0]) == name:
        return base
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    return f"{base[:55].rstrip('-')}-{digest}"


def _apply_concept_entities(
    conn: sqlite3.Connection,
    concepts: list[dict[str, Any]],
) -> dict[str, Any]:
    inserted = 0
    skipped_existing = 0
    now = datetime.now(timezone.utc).isoformat()
    for item in concepts:
        name = str(item["endpoint"])
        exists = conn.execute(
            "SELECT 1 FROM entities WHERE name=? OR uid=? LIMIT 1",
            (name, name),
        ).fetchone()
        if exists is not None:
            skipped_existing += 1
            continue
        uid = _entity_uid(conn, name)
        source_count = max(1, int(item.get("relation_refs") or 1))
        conn.execute(
            """INSERT INTO entities
               (uid, name, entity_type, source_page, quality_score, confidence,
                temporal_scope, version_info, status, visit_count, tags,
                first_seen, last_updated, source_count)
               VALUES (?, ?, 'concept', '', 0.4, 0.45, 'stable', NULL, 'active', 0, ?, ?, ?, ?)""",
            (
                uid,
                name,
                json.dumps(["kg_endpoint_auto", "semantic_normalization"], ensure_ascii=False),
                now,
                now,
                source_count,
            ),
        )
        inserted += 1
    return {"entities_inserted": inserted, "skipped_existing": skipped_existing}


def _invalid_relation_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT id, source, target, relation_type, source_method FROM relations ORDER BY id"
    ).fetchall():
        source_reason = prunable_relation_endpoint_reason(row[1])
        target_reason = prunable_relation_endpoint_reason(row[2])
        if source_reason or target_reason:
            rows.append(
                {
                    "relation_id": int(row[0]),
                    "source": str(row[1]),
                    "target": str(row[2]),
                    "relation_type": str(row[3]),
                    "source_method": str(row[4] or ""),
                    "source_reason": source_reason,
                    "target_reason": target_reason,
                }
            )
    return rows


def _prune_invalid_relations(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = _invalid_relation_rows(conn)
    relation_ids = [int(row["relation_id"]) for row in rows]
    if not relation_ids:
        return {
            "relations_deleted": 0,
            "fts_deleted": 0,
            "evidence_deleted": 0,
            "embeddings_deleted": 0,
        }

    placeholders = ",".join("?" for _ in relation_ids)
    evidence_deleted = 0
    fts_deleted = 0
    embeddings_deleted = 0
    if _table_exists(conn, "relation_evidence"):
        cur = conn.execute(
            f"DELETE FROM relation_evidence WHERE relation_id IN ({placeholders})",  # nosec B608
            relation_ids,
        )
        evidence_deleted = int(cur.rowcount or 0)
    if _table_exists(conn, "relations_fts"):
        cur = conn.execute(
            f"DELETE FROM relations_fts WHERE rowid IN ({placeholders})",  # nosec B608
            relation_ids,
        )
        fts_deleted = int(cur.rowcount or 0)
    if _table_exists(conn, "relation_context_embeddings"):
        cur = conn.execute(
            f"DELETE FROM relation_context_embeddings WHERE relation_id IN ({placeholders})",  # nosec B608
            relation_ids,
        )
        embeddings_deleted = int(cur.rowcount or 0)
    cur = conn.execute(
        f"DELETE FROM relations WHERE id IN ({placeholders})",  # nosec B608
        relation_ids,
    )
    return {
        "relations_deleted": int(cur.rowcount or 0),
        "fts_deleted": fts_deleted,
        "evidence_deleted": evidence_deleted,
        "embeddings_deleted": embeddings_deleted,
    }


def normalize_kg_endpoints(
    db_path: str | Path,
    *,
    wiki_base: str | Path | None = None,
    apply: bool = False,
    create_backup: bool = False,
    backup_root: str | Path | None = None,
    sample_limit: int = 10,
    min_concept_refs: int = 2,
    prune_invalid: bool = False,
) -> dict[str, Any]:
    """Classify and optionally repair semantic KG endpoint gaps.

    The apply path is deliberately conservative:
    - migrate an endpoint to a wiki page only when the page match is unique;
    - materialize an entity only for clean concept-like endpoints with enough
      relation references;
    - leave noise, stale system projection paths, ambiguous mappings, and
      relation uniqueness conflicts untouched for manual review.
    """
    path = Path(db_path).expanduser()
    wiki_path = Path(wiki_base).expanduser() if wiki_base else None
    backup_path = ""
    with sqlite_conn(str(path), timeout=30) as conn:
        before = audit_kg_consistency(path, wiki_base=wiki_path, sample_limit=sample_limit)
        classification = _classify_endpoints(
            conn,
            wiki_base=wiki_path,
            sample_limit=sample_limit,
            min_concept_refs=min_concept_refs,
        )
        invalid_relation_count = len(_invalid_relation_rows(conn))

    applied: dict[str, Any] = {
        "relations_updated": 0,
        "fts_updated": 0,
        "entities_inserted": 0,
        "invalid_relations_deleted": 0,
        "invalid_fts_deleted": 0,
        "invalid_evidence_deleted": 0,
        "invalid_embeddings_deleted": 0,
        "skipped_existing_entities": 0,
        "skipped_conflicts": [],
        "skipped_self_relations": [],
    }

    if apply:
        if create_backup:
            backup_path = str(
                _backup_database(
                    path,
                    Path(backup_root).expanduser() if backup_root else None,
                )
            )
        with sqlite_conn(str(path), timeout=30) as conn:
            path_result = _apply_path_migrations(
                conn,
                classification["path_migrations"],
            )
            concept_result = _apply_concept_entities(
                conn,
                classification["concept_entities"],
            )
            prune_result = (
                _prune_invalid_relations(conn)
                if prune_invalid
                else {
                    "relations_deleted": 0,
                    "fts_deleted": 0,
                    "evidence_deleted": 0,
                    "embeddings_deleted": 0,
                }
            )
            conn.commit()
        applied.update(path_result)
        applied["entities_inserted"] = concept_result["entities_inserted"]
        applied["skipped_existing_entities"] = concept_result["skipped_existing"]
        applied["invalid_relations_deleted"] = prune_result["relations_deleted"]
        applied["invalid_fts_deleted"] = prune_result["fts_deleted"]
        applied["invalid_evidence_deleted"] = prune_result["evidence_deleted"]
        applied["invalid_embeddings_deleted"] = prune_result["embeddings_deleted"]

    after = audit_kg_consistency(path, wiki_base=wiki_path, sample_limit=sample_limit)
    status = "ok"
    if after.get("status") not in {"ok", "missing"}:
        status = "degraded"

    return {
        "schema_version": SCHEMA_VERSION,
        "dry_run": not apply,
        "db_path": str(path),
        "wiki_base": str(wiki_path) if wiki_path else "",
        "backup_path": backup_path,
        "min_concept_refs": min_concept_refs,
        "prune_invalid": prune_invalid,
        "before": before,
        "after": after if apply else before,
        "classification": classification,
        "would_apply": (
            {
                "path_migrations": len(classification["path_migrations"]),
                "concept_entities": len(classification["concept_entities"]),
                **(
                    {"invalid_relations_deleted": invalid_relation_count}
                    if prune_invalid
                    else {}
                ),
            }
            if not apply
            else {}
        ),
        "applied": applied if apply else {},
        "status": status,
    }


def emit_report(payload: dict[str, Any], *, json_output: bool = False) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2))
