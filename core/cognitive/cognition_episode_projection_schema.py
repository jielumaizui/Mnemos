"""Canonical schema authority for cognition-episode graph projections.

Normal graph constructors may create their long-standing base tables, but they
must never upgrade an existing database into the cognition-episode projection
contract.  Historical databases are upgraded only through the explicit,
backup-protected reconciliation path.  A genuinely absent database may use the
same initializer as part of first-run bootstrap.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any, Mapping

SCHEMA_VERSION = "mnemos.cognition_episode_projection_schema.v1"

_EVIDENCE_EFFECT_COLUMNS = {
    "effect_id",
    "revision_id",
    "manifest_hash",
    "before_hash",
    "after_hash",
    "node_count",
    "edge_count",
    "omission_count",
    "access_control_hash",
    "created_at",
}
_EVIDENCE_OMISSION_COLUMNS = {
    "omission_id",
    "revision_id",
    "field_name",
    "entry_index",
    "disposition",
    "reason_code",
    "payload_hash",
    "created_at",
}
_COGNITIVE_EFFECT_COLUMNS = {
    "effect_id",
    "revision_id",
    "manifest_hash",
    "before_hash",
    "after_hash",
    "relation_count",
    "access_control_hash",
    "created_at",
}
_WIKI_EFFECT_COLUMNS = {
    "effect_id",
    "revision_id",
    "manifest_hash",
    "before_hash",
    "after_hash",
    "page_count",
    "projection_json",
    "created_at",
}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}  # nosec B608


def _has_unique_columns(
    conn: sqlite3.Connection,
    table: str,
    expected: tuple[str, ...],
) -> bool:
    for index in conn.execute(f"PRAGMA index_list({table})"):  # nosec B608
        if not int(index[2]):
            continue
        columns = tuple(
            str(row[2]) for row in conn.execute(f"PRAGMA index_info({index[1]})")  # nosec B608
        )
        if columns == expected:
            return True
    return False


def _evidence_gaps(conn: sqlite3.Connection) -> list[str]:
    tables = _tables(conn)
    gaps: list[str] = []
    if "evidence_nodes" not in tables:
        gaps.append("missing_base_table:evidence_nodes")
    elif "access_control" not in _columns(conn, "evidence_nodes"):
        gaps.append("missing_acl_column:evidence_nodes.access_control")
    if "evidence_edges" not in tables:
        gaps.append("missing_base_table:evidence_edges")
    elif "access_control" not in _columns(conn, "evidence_edges"):
        gaps.append("missing_acl_column:evidence_edges.access_control")

    effect_table = "cognition_episode_projection_effects"
    if effect_table not in tables:
        gaps.append(f"missing_projection_table:{effect_table}")
    else:
        missing = _EVIDENCE_EFFECT_COLUMNS - _columns(conn, effect_table)
        gaps.extend(f"missing_projection_column:{effect_table}.{name}" for name in sorted(missing))
        if not _has_unique_columns(conn, effect_table, ("revision_id",)):
            gaps.append(f"missing_unique_contract:{effect_table}.revision_id")

    omission_table = "cognition_episode_projection_omissions"
    if omission_table not in tables:
        gaps.append(f"missing_projection_table:{omission_table}")
    else:
        missing = _EVIDENCE_OMISSION_COLUMNS - _columns(conn, omission_table)
        gaps.extend(
            f"missing_projection_column:{omission_table}.{name}" for name in sorted(missing)
        )
        if not _has_unique_columns(
            conn,
            omission_table,
            ("revision_id", "field_name", "entry_index"),
        ):
            gaps.append(
                "missing_unique_contract:"
                "cognition_episode_projection_omissions.revision_id_field_name_entry_index"
            )
    return gaps


def _cognitive_graph_gaps(conn: sqlite3.Connection) -> list[str]:
    tables = _tables(conn)
    gaps: list[str] = []
    if "cognitive_relations" not in tables:
        gaps.append("missing_base_table:cognitive_relations")
    effect_table = "cognition_episode_projection_effects"
    if effect_table not in tables:
        gaps.append(f"missing_projection_table:{effect_table}")
    else:
        missing = _COGNITIVE_EFFECT_COLUMNS - _columns(conn, effect_table)
        gaps.extend(f"missing_projection_column:{effect_table}.{name}" for name in sorted(missing))
        if not _has_unique_columns(conn, effect_table, ("revision_id",)):
            gaps.append(f"missing_unique_contract:{effect_table}.revision_id")
    return gaps


def _wiki_projection_gaps(conn: sqlite3.Connection) -> list[str]:
    tables = _tables(conn)
    gaps: list[str] = []
    for table in ("wiki_pages", "wiki_mutations"):
        if table not in tables:
            gaps.append(f"missing_base_table:{table}")
    effect_table = "cognition_episode_projection_effects"
    if effect_table not in tables:
        gaps.append(f"missing_projection_table:{effect_table}")
    else:
        missing = _WIKI_EFFECT_COLUMNS - _columns(conn, effect_table)
        gaps.extend(f"missing_projection_column:{effect_table}.{name}" for name in sorted(missing))
        if not _has_unique_columns(conn, effect_table, ("revision_id",)):
            gaps.append(f"missing_unique_contract:{effect_table}.revision_id")
    return gaps


def _inspect(path: Path, inspector) -> dict[str, Any]:
    path = Path(path).expanduser().resolve(strict=False)
    if not path.is_file():
        return {"path": str(path), "initialized": False, "gaps": ["database_missing"]}
    try:
        with sqlite3.connect(f"file:{path.resolve(strict=True)}?mode=ro", uri=True) as conn:
            conn.execute("PRAGMA query_only=ON")
            gaps = inspector(conn)
    except (OSError, sqlite3.Error) as exc:
        gaps = [f"schema_read_error:{type(exc).__name__}:{exc}"]
    return {"path": str(path), "initialized": not gaps, "gaps": gaps}


def inspect_cognition_episode_projection_schema(
    *,
    evidence_db_path: Path,
    cognitive_graph_db_path: Path,
    wiki_projection_db_path: Path,
) -> dict[str, Any]:
    evidence = _inspect(Path(evidence_db_path), _evidence_gaps)
    cognitive_graph = _inspect(Path(cognitive_graph_db_path), _cognitive_graph_gaps)
    wiki = _inspect(Path(wiki_projection_db_path), _wiki_projection_gaps)
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": not evidence["gaps"] and not cognitive_graph["gaps"] and not wiki["gaps"],
        "evidence_graph": evidence,
        "cognitive_graph": cognitive_graph,
        "wiki": wiki,
    }


def validate_cognition_episode_projection_schema(
    *,
    evidence_db_path: Path,
    cognitive_graph_db_path: Path,
    wiki_projection_db_path: Path,
) -> None:
    report = inspect_cognition_episode_projection_schema(
        evidence_db_path=evidence_db_path,
        cognitive_graph_db_path=cognitive_graph_db_path,
        wiki_projection_db_path=wiki_projection_db_path,
    )
    if report["ok"]:
        return
    gaps = [
        *(f"evidence_graph:{gap}" for gap in report["evidence_graph"]["gaps"]),
        *(f"cognitive_graph:{gap}" for gap in report["cognitive_graph"]["gaps"]),
        *(f"wiki:{gap}" for gap in report["wiki"]["gaps"]),
    ]
    raise RuntimeError(
        "cognition episode projection schema requires explicit reconciliation: " + ", ".join(gaps)
    )


def initialize_evidence_projection_schema(db_path: Path) -> None:
    """Explicitly migrate an initialized EvidenceGraph database in one transaction."""

    path = Path(db_path).expanduser().resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path), timeout=60) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            tables = _tables(conn)
            if not {"evidence_nodes", "evidence_edges"} <= tables:
                raise RuntimeError("EvidenceGraph base schema must be initialized first")
            if "access_control" not in _columns(conn, "evidence_nodes"):
                conn.execute(
                    "ALTER TABLE evidence_nodes "
                    "ADD COLUMN access_control TEXT NOT NULL DEFAULT ''"
                )
            if "access_control" not in _columns(conn, "evidence_edges"):
                conn.execute(
                    "ALTER TABLE evidence_edges "
                    "ADD COLUMN access_control TEXT NOT NULL DEFAULT ''"
                )
            conn.execute("""CREATE TABLE IF NOT EXISTS cognition_episode_projection_effects (
                       effect_id TEXT PRIMARY KEY,
                       revision_id TEXT NOT NULL UNIQUE,
                       manifest_hash TEXT NOT NULL,
                       before_hash TEXT NOT NULL,
                       after_hash TEXT NOT NULL,
                       node_count INTEGER NOT NULL,
                       edge_count INTEGER NOT NULL,
                       omission_count INTEGER NOT NULL,
                       access_control_hash TEXT NOT NULL,
                       created_at TEXT NOT NULL
                   )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS cognition_episode_projection_omissions (
                       omission_id TEXT PRIMARY KEY,
                       revision_id TEXT NOT NULL,
                       field_name TEXT NOT NULL,
                       entry_index INTEGER NOT NULL,
                       disposition TEXT NOT NULL CHECK(disposition='omitted'),
                       reason_code TEXT NOT NULL,
                       payload_hash TEXT NOT NULL,
                       created_at TEXT NOT NULL,
                       UNIQUE(revision_id, field_name, entry_index)
                   )""")
            conn.execute("""CREATE INDEX IF NOT EXISTS idx_episode_projection_omission_revision
                   ON cognition_episode_projection_omissions(revision_id)""")
            gaps = _evidence_gaps(conn)
            if gaps:
                raise RuntimeError("invalid evidence projection schema: " + ", ".join(gaps))
            conn.commit()
        except (RuntimeError, sqlite3.Error):
            conn.rollback()
            raise


def initialize_cognitive_graph_projection_schema(db_path: Path) -> None:
    """Explicitly install the target-effect table after base graph initialization."""

    path = Path(db_path).expanduser().resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path), timeout=60) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if "cognitive_relations" not in _tables(conn):
                raise RuntimeError("CognitiveGraph base schema must be initialized first")
            conn.execute("""CREATE TABLE IF NOT EXISTS cognition_episode_projection_effects (
                       effect_id TEXT PRIMARY KEY,
                       revision_id TEXT NOT NULL UNIQUE,
                       manifest_hash TEXT NOT NULL,
                       before_hash TEXT NOT NULL,
                       after_hash TEXT NOT NULL,
                       relation_count INTEGER NOT NULL,
                       access_control_hash TEXT NOT NULL,
                       created_at TEXT NOT NULL
                   )""")
            gaps = _cognitive_graph_gaps(conn)
            if gaps:
                raise RuntimeError("invalid cognitive graph projection schema: " + ", ".join(gaps))
            conn.commit()
        except (RuntimeError, sqlite3.Error):
            conn.rollback()
            raise


def initialize_wiki_projection_schema(db_path: Path) -> None:
    """Explicitly install the episode effect journal in the Wiki lifecycle DB."""

    path = Path(db_path).expanduser().resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path), timeout=60) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if not {"wiki_pages", "wiki_mutations"} <= _tables(conn):
                raise RuntimeError("Wiki projection base schema must be initialized first")
            conn.execute("""CREATE TABLE IF NOT EXISTS cognition_episode_projection_effects (
                       effect_id TEXT PRIMARY KEY,
                       revision_id TEXT NOT NULL UNIQUE,
                       manifest_hash TEXT NOT NULL,
                       before_hash TEXT NOT NULL,
                       after_hash TEXT NOT NULL,
                       page_count INTEGER NOT NULL,
                       projection_json TEXT NOT NULL,
                       created_at TEXT NOT NULL
                   )""")
            gaps = _wiki_projection_gaps(conn)
            if gaps:
                raise RuntimeError("invalid Wiki projection schema: " + ", ".join(gaps))
            conn.commit()
        except (RuntimeError, sqlite3.Error):
            conn.rollback()
            raise


def initialize_cognition_episode_projection_schema(
    *,
    evidence_db_path: Path,
    cognitive_graph_db_path: Path,
    wiki_projection_db_path: Path,
) -> None:
    initialize_evidence_projection_schema(evidence_db_path)
    initialize_cognitive_graph_projection_schema(cognitive_graph_db_path)
    initialize_wiki_projection_schema(wiki_projection_db_path)
    validate_cognition_episode_projection_schema(
        evidence_db_path=evidence_db_path,
        cognitive_graph_db_path=cognitive_graph_db_path,
        wiki_projection_db_path=wiki_projection_db_path,
    )


def initialize_fresh_projection_targets(
    *,
    evidence_db_path: Path,
    cognitive_graph_db_path: Path,
    wiki_projection_db_path: Path,
    target_was_absent: Mapping[str, bool],
) -> None:
    """First-run bootstrap only; existing targets remain reconciliation-owned."""

    if target_was_absent.get("evidence_graph"):
        initialize_evidence_projection_schema(evidence_db_path)
    if target_was_absent.get("cognitive_graph"):
        initialize_cognitive_graph_projection_schema(cognitive_graph_db_path)
    if target_was_absent.get("wiki"):
        initialize_wiki_projection_schema(wiki_projection_db_path)
