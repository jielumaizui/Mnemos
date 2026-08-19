"""Append-only dispositions for legacy assets that cannot receive a proven ACL.

The ledger never grants read access.  It binds an exact current source row or
Wiki file hash to a reviewed fail-closed exclusion so audits can distinguish a
known historical disposition from an unresolved ACL gap.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Iterator, Mapping, Sequence

from core.cognitive.state_contract import canonical_json, sha256_json
from core.frontmatter import normalize_frontmatter, read_frontmatter_only
from core.utils import EXCLUDED_DIRS

SEARCH_EXCLUSION_SCHEMA_VERSION = "mnemos.cognitive_search_exclusions.v1"
SEARCH_EXCLUSION_RULE_VERSION = "historical-acl-unavailable-v1"
SEARCH_EXCLUSION_REASON = "historical_acl_unavailable"
SEARCH_EXCLUSION_APPROVAL = "user_confirmed_phase4_repair:2026-07-20"
_REGISTRY_COMPONENT = "cognitive_search_exclusions"

_DDL = """
CREATE TABLE cognitive_search_exclusion_registry (
    component TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    ddl_hash TEXT NOT NULL
);

CREATE TABLE cognitive_search_exclusions (
    exclusion_id TEXT PRIMARY KEY,
    channel TEXT NOT NULL CHECK(channel IN (
        'wiki_page', 'cognitive_graph', 'evidence_graph'
    )),
    source_locator_hash TEXT NOT NULL,
    source_table TEXT NOT NULL,
    source_key_json TEXT NOT NULL CHECK(json_valid(source_key_json)),
    source_key_hash TEXT NOT NULL,
    source_row_hash TEXT NOT NULL,
    reason_code TEXT NOT NULL CHECK(reason_code='historical_acl_unavailable'),
    rule_version TEXT NOT NULL,
    approval_basis TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(
        channel, source_locator_hash, source_table,
        source_key_hash, source_row_hash
    )
);

CREATE INDEX idx_cognitive_search_exclusions_lookup
ON cognitive_search_exclusions(
    channel, source_locator_hash, source_table,
    source_key_hash, source_row_hash
);

CREATE TRIGGER cognitive_search_exclusions_no_update
BEFORE UPDATE ON cognitive_search_exclusions BEGIN
    SELECT RAISE(ABORT, 'cognitive_search_exclusions is append-only');
END;

CREATE TRIGGER cognitive_search_exclusions_no_delete
BEFORE DELETE ON cognitive_search_exclusions BEGIN
    SELECT RAISE(ABORT, 'cognitive_search_exclusions is append-only');
END;
"""


def _ddl_hash() -> str:
    return "sha256:" + hashlib.sha256(" ".join(_DDL.split()).encode("utf-8")).hexdigest()


SEARCH_EXCLUSION_DDL_HASH = _ddl_hash()


@dataclass(frozen=True)
class SearchExclusionCandidate:
    channel: str
    source_locator_hash: str
    source_table: str
    source_key_json: str
    source_key_hash: str
    source_row_hash: str

    @property
    def identity(self) -> tuple[str, str, str, str, str]:
        return (
            self.channel,
            self.source_locator_hash,
            self.source_table,
            self.source_key_hash,
            self.source_row_hash,
        )

    @property
    def identity_key(self) -> bytes:
        return search_exclusion_identity_key(self.identity)

    def manifest(self) -> dict[str, str]:
        return {
            "channel": self.channel,
            "source_locator_hash": self.source_locator_hash,
            "source_table": self.source_table,
            "source_key_hash": self.source_key_hash,
            "source_row_hash": self.source_row_hash,
        }


_TABLE_SPECS = {
    "cognitive_graph": (
        ("cognitive_relations", "id", "stale = 0"),
        ("canonical_nodes", "canonical_id", "1 = 1"),
    ),
    "evidence_graph": (
        ("evidence_nodes", "id", "1 = 1"),
        ("evidence_edges", "id", "1 = 1"),
    ),
}


def search_exclusion_identity_key(identity: Sequence[str]) -> bytes:
    """Return a compact exact lookup key for one five-field source identity."""

    if len(identity) != 5:
        raise ValueError("cognitive search exclusion identity must have five fields")
    encoded = canonical_json([str(value) for value in identity]).encode("utf-8")
    return hashlib.sha256(encoded).digest()


def initialize_search_exclusion_ledger(connection: sqlite3.Connection) -> None:
    """Initialize a fresh ledger; existing or partial schemas fail closed."""

    existing = connection.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger', 'index') "
        "AND name LIKE 'cognitive_search_exclusion%'"
    ).fetchall()
    if existing:
        raise RuntimeError("cognitive search exclusion ledger is not fresh")
    connection.executescript(_DDL)
    connection.execute(
        "INSERT INTO cognitive_search_exclusion_registry(component, schema_version, ddl_hash) "
        "VALUES (?, ?, ?)",
        (_REGISTRY_COMPONENT, SEARCH_EXCLUSION_SCHEMA_VERSION, SEARCH_EXCLUSION_DDL_HASH),
    )


def validate_search_exclusion_ledger(connection: sqlite3.Connection) -> dict[str, Any]:
    """Validate registry plus exact table/index/trigger ownership."""

    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    indexes = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    triggers = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
    }
    required_tables = {
        "cognitive_search_exclusion_registry",
        "cognitive_search_exclusions",
    }
    required_indexes = {"idx_cognitive_search_exclusions_lookup"}
    required_triggers = {
        "cognitive_search_exclusions_no_update",
        "cognitive_search_exclusions_no_delete",
    }
    registry = None
    registry_row_count = 0
    if required_tables <= tables:
        registry_row_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM cognitive_search_exclusion_registry"
            ).fetchone()[0]
        )
        registry = connection.execute(
            "SELECT schema_version, ddl_hash FROM cognitive_search_exclusion_registry "
            "WHERE component=?",
            (_REGISTRY_COMPONENT,),
        ).fetchone()
    registry_ok = bool(
        registry_row_count == 1
        and registry is not None
        and tuple(str(value) for value in registry)
        == (SEARCH_EXCLUSION_SCHEMA_VERSION, SEARCH_EXCLUSION_DDL_HASH)
    )
    columns: tuple[str, ...] = ()
    if "cognitive_search_exclusions" in tables:
        columns = tuple(
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(cognitive_search_exclusions)"
            ).fetchall()
        )
    expected_columns = (
        "exclusion_id",
        "channel",
        "source_locator_hash",
        "source_table",
        "source_key_json",
        "source_key_hash",
        "source_row_hash",
        "reason_code",
        "rule_version",
        "approval_basis",
        "created_at",
    )
    schema_signature_ok = _owned_schema_signature(connection) == _expected_schema_signature()
    semantic_mismatch_count = 0
    row_count = 0
    if "cognitive_search_exclusions" in tables:
        exclusion_rows = connection.execute("""
            SELECT exclusion_id, channel, source_locator_hash, source_table,
                   source_key_json, source_key_hash, source_row_hash,
                   reason_code, rule_version, approval_basis, created_at
            FROM cognitive_search_exclusions
            """)
        for row in exclusion_rows:
            row_count += 1
            if not _valid_exclusion_row(tuple(str(value) for value in row)):
                semantic_mismatch_count += 1
    ok = bool(
        required_tables <= tables
        and required_indexes <= indexes
        and required_triggers <= triggers
        and columns == expected_columns
        and registry_ok
        and schema_signature_ok
        and semantic_mismatch_count == 0
    )
    return {
        "schema_version": SEARCH_EXCLUSION_SCHEMA_VERSION,
        "registry_ok": registry_ok,
        "registry_row_count": registry_row_count,
        "tables_ok": required_tables <= tables,
        "indexes_ok": required_indexes <= indexes,
        "triggers_ok": required_triggers <= triggers,
        "columns_ok": columns == expected_columns,
        "schema_signature_ok": schema_signature_ok,
        "semantic_mismatch_count": semantic_mismatch_count,
        "row_count": row_count,
        "ok": ok,
    }


def _owned_schema_signature(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        """
        SELECT type, name, tbl_name, COALESCE(sql, '')
        FROM sqlite_master
        WHERE name IN (?, ?)
           OR tbl_name IN (?, ?)
        ORDER BY type, name, tbl_name
        """,
        (
            "cognitive_search_exclusion_registry",
            "cognitive_search_exclusions",
            "cognitive_search_exclusion_registry",
            "cognitive_search_exclusions",
        ),
    ).fetchall()
    normalized = [
        [str(kind), str(name), str(table), " ".join(str(sql).split())]
        for kind, name, table, sql in rows
    ]
    return sha256_json(normalized)


@lru_cache(maxsize=1)
def _expected_schema_signature() -> str:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(_DDL)
        return _owned_schema_signature(connection)
    finally:
        connection.close()


def _valid_exclusion_row(row: tuple[str, ...]) -> bool:
    (
        exclusion_id,
        channel,
        source_locator_hash,
        source_table,
        source_key_json,
        source_key_hash,
        source_row_hash,
        reason_code,
        rule_version,
        approval_basis,
        created_at,
    ) = row
    if not all(
        _is_sha256(value) for value in (source_locator_hash, source_key_hash, source_row_hash)
    ):
        return False
    expected_key = {
        ("wiki_page", "markdown_page"): "relative_path",
        ("cognitive_graph", "cognitive_relations"): "id",
        ("cognitive_graph", "canonical_nodes"): "canonical_id",
        ("evidence_graph", "evidence_nodes"): "id",
        ("evidence_graph", "evidence_edges"): "id",
    }.get((channel, source_table))
    if expected_key is None:
        return False
    try:
        decoded_key = json.loads(source_key_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if (
        not isinstance(decoded_key, Mapping)
        or set(decoded_key) != {expected_key}
        or canonical_json(decoded_key) != source_key_json
        or sha256_json(decoded_key) != source_key_hash
    ):
        return False
    if expected_key == "relative_path":
        relative_path = Path(str(decoded_key[expected_key]))
        if relative_path.is_absolute() or ".." in relative_path.parts:
            return False
    identity = {
        "channel": channel,
        "source_locator_hash": source_locator_hash,
        "source_table": source_table,
        "source_key_hash": source_key_hash,
        "source_row_hash": source_row_hash,
        "reason_code": reason_code,
        "rule_version": rule_version,
        "approval_basis": approval_basis,
    }
    if (
        reason_code != SEARCH_EXCLUSION_REASON
        or rule_version != SEARCH_EXCLUSION_RULE_VERSION
        or approval_basis != SEARCH_EXCLUSION_APPROVAL
        or sha256_json(identity) != exclusion_id
    ):
        return False
    try:
        timestamp = datetime.fromisoformat(created_at)
    except ValueError:
        return False
    return timestamp.tzinfo is not None


def _is_sha256(value: str) -> bool:
    if not value.startswith("sha256:") or len(value) != 71:
        return False
    return all(character in "0123456789abcdef" for character in value[7:])


def iter_wiki_exclusion_candidates(wiki_dir: Path) -> Iterator[SearchExclusionCandidate]:
    """Yield only explicit complete ``restricted_unknown`` Markdown assets."""

    root = wiki_dir.expanduser().resolve(strict=False)
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(root)
        if path.is_symlink() or any(
            part in EXCLUDED_DIRS or part.startswith(".") for part in relative.parts
        ):
            continue
        try:
            frontmatter = normalize_frontmatter(read_frontmatter_only(path, errors="strict"))
        except (OSError, UnicodeError, ValueError):
            continue
        candidate = build_wiki_exclusion_candidate(
            wiki_dir=root,
            relative_path=relative,
            frontmatter=frontmatter,
            source_row_hash=_file_hash(path),
        )
        if candidate is not None:
            yield candidate


def build_wiki_exclusion_candidate(
    *,
    wiki_dir: Path,
    relative_path: Path,
    frontmatter: Mapping[str, Any],
    source_row_hash: str,
) -> SearchExclusionCandidate | None:
    """Build the exact exclusion identity for a complete fail-closed Wiki page."""

    if not (
        str(frontmatter.get("scope") or "").strip().lower() == "restricted"
        and str(frontmatter.get("acl_reconciliation_status") or "").strip().lower()
        == "restricted_unknown"
        and frontmatter.get("acl_schema_version") == 1
        and frontmatter.get("acl_metadata_complete") is True
    ):
        return None
    source_key = {"relative_path": relative_path.as_posix()}
    return SearchExclusionCandidate(
        channel="wiki_page",
        source_locator_hash=_locator_hash(wiki_dir.expanduser().resolve(strict=False)),
        source_table="markdown_page",
        source_key_json=canonical_json(source_key),
        source_key_hash=sha256_json(source_key),
        source_row_hash=source_row_hash,
    )


def iter_sqlite_exclusion_candidates(
    db_path: Path,
    *,
    channel: str,
) -> Iterator[SearchExclusionCandidate]:
    """Yield historical untyped rows with exactly empty ACLs, never malformed ACLs."""

    if channel not in _TABLE_SPECS:
        raise ValueError("unsupported cognitive search exclusion channel")
    path = db_path.expanduser().resolve(strict=False)
    if not path.is_file():
        return
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    try:
        for table, id_column, where in _TABLE_SPECS[channel]:
            rows = connection.execute(
                f"SELECT * FROM {table} WHERE {where} ORDER BY {id_column}"  # nosec B608 - fixed identifiers
            )
            for row in rows:
                candidate = build_sqlite_exclusion_candidate(
                    db_path=path,
                    channel=channel,
                    table=table,
                    id_column=id_column,
                    row=row,
                )
                if candidate is not None:
                    yield candidate
    finally:
        connection.close()


def build_sqlite_exclusion_candidate(
    *,
    db_path: Path,
    channel: str,
    table: str,
    id_column: str,
    row: Mapping[str, Any],
) -> SearchExclusionCandidate | None:
    """Build an exact exclusion identity only for a row with a blank ACL."""

    if channel not in _TABLE_SPECS:
        raise ValueError("unsupported cognitive search exclusion channel")
    table_specs = {(name, identifier) for name, identifier, _where in _TABLE_SPECS[channel]}
    if (table, id_column) not in table_specs:
        raise ValueError("unsupported cognitive search exclusion table")
    if str(row["access_control"] or "").strip():
        return None
    source_key = {id_column: _json_safe(row[id_column])}
    values = {str(key): _json_safe(row[key]) for key in row.keys()}
    return SearchExclusionCandidate(
        channel=channel,
        source_locator_hash=_locator_hash(db_path.expanduser().resolve(strict=False)),
        source_table=table,
        source_key_json=canonical_json(source_key),
        source_key_hash=sha256_json(source_key),
        source_row_hash=sha256_json(values),
    )


def iter_search_exclusion_candidates(
    *,
    targets: Sequence[str],
    wiki_dir: Path,
    cognitive_graph_db: Path,
    evidence_graph_db: Path,
) -> Iterator[SearchExclusionCandidate]:
    selected = tuple(dict.fromkeys(str(target) for target in targets))
    if not selected or set(selected) - {"wiki", "cognitive_graph", "evidence_graph"}:
        raise ValueError("search exclusion targets are invalid")
    if "wiki" in selected:
        yield from iter_wiki_exclusion_candidates(wiki_dir)
    if "cognitive_graph" in selected:
        yield from iter_sqlite_exclusion_candidates(
            cognitive_graph_db,
            channel="cognitive_graph",
        )
    if "evidence_graph" in selected:
        yield from iter_sqlite_exclusion_candidates(
            evidence_graph_db,
            channel="evidence_graph",
        )


def inventory_search_exclusions(candidates: Iterable[SearchExclusionCandidate]) -> dict[str, Any]:
    digest = hashlib.sha256()
    counts: Counter[str] = Counter()
    table_counts: Counter[str] = Counter()
    total = 0
    for candidate in candidates:
        encoded = canonical_json(candidate.manifest()).encode("utf-8")
        digest.update(encoded)
        digest.update(b"\n")
        total += 1
        counts[candidate.channel] += 1
        table_counts[f"{candidate.channel}:{candidate.source_table}"] += 1
    object_manifest_hash = "sha256:" + digest.hexdigest()
    inventory_hash = sha256_json(
        {
            "schema_version": SEARCH_EXCLUSION_SCHEMA_VERSION,
            "rule_version": SEARCH_EXCLUSION_RULE_VERSION,
            "reason_code": SEARCH_EXCLUSION_REASON,
            "approval_basis": SEARCH_EXCLUSION_APPROVAL,
            "candidate_count": total,
            "channel_counts": dict(sorted(counts.items())),
            "table_counts": dict(sorted(table_counts.items())),
            "object_manifest_hash": object_manifest_hash,
        }
    )
    return {
        "schema_version": SEARCH_EXCLUSION_SCHEMA_VERSION,
        "candidate_count": total,
        "channel_counts": dict(sorted(counts.items())),
        "table_counts": dict(sorted(table_counts.items())),
        "object_manifest_hash": object_manifest_hash,
        "inventory_hash": inventory_hash,
    }


def load_search_exclusion_keys(
    ledger_db: Path,
) -> tuple[set[bytes], dict[str, Any]]:
    if not ledger_db.is_file():
        return set(), {
            "schema_version": SEARCH_EXCLUSION_SCHEMA_VERSION,
            "schema_present": False,
            "row_count": 0,
            "ok": False,
        }
    connection = sqlite3.connect(
        f"{ledger_db.expanduser().resolve().as_uri()}?mode=ro",
        uri=True,
        timeout=10,
    )
    try:
        validation = validate_search_exclusion_ledger(connection)
        if not validation["ok"]:
            return set(), validation
        keys = {
            search_exclusion_identity_key(tuple(str(value) for value in row))
            for row in connection.execute("""
                SELECT channel, source_locator_hash, source_table,
                       source_key_hash, source_row_hash
                FROM cognitive_search_exclusions
                """).fetchall()
        }
        return keys, validation
    finally:
        connection.close()


def search_exclusion_coverage(
    candidates: Iterable[SearchExclusionCandidate],
    *,
    exclusion_keys: set[bytes],
) -> dict[str, Any]:
    table_counts: dict[str, dict[str, int]] = {}
    covered_identities: set[bytes] = set()
    candidate_count = 0
    covered_count = 0
    for candidate in candidates:
        candidate_count += 1
        key = f"{candidate.channel}:{candidate.source_table}"
        row = table_counts.setdefault(key, {"candidate_count": 0, "covered_count": 0})
        row["candidate_count"] += 1
        if candidate.identity_key in exclusion_keys:
            covered_count += 1
            row["covered_count"] += 1
            covered_identities.add(candidate.identity_key)
    for row in table_counts.values():
        row["uncovered_count"] = row["candidate_count"] - row["covered_count"]
    return {
        "candidate_count": candidate_count,
        "covered_count": covered_count,
        "uncovered_count": candidate_count - covered_count,
        "table_counts": dict(sorted(table_counts.items())),
        "covered_identities": covered_identities,
    }


def insert_search_exclusion(
    connection: sqlite3.Connection,
    candidate: SearchExclusionCandidate,
    *,
    created_at: str | None = None,
) -> str:
    identity = {
        **candidate.manifest(),
        "reason_code": SEARCH_EXCLUSION_REASON,
        "rule_version": SEARCH_EXCLUSION_RULE_VERSION,
        "approval_basis": SEARCH_EXCLUSION_APPROVAL,
    }
    exclusion_id = sha256_json(identity)
    existing = connection.execute(
        """
        SELECT exclusion_id, source_key_json, reason_code, rule_version, approval_basis
        FROM cognitive_search_exclusions
        WHERE channel=? AND source_locator_hash=? AND source_table=?
          AND source_key_hash=? AND source_row_hash=?
        """,
        candidate.identity,
    ).fetchone()
    if existing is not None:
        if tuple(str(value) for value in existing) != (
            exclusion_id,
            candidate.source_key_json,
            SEARCH_EXCLUSION_REASON,
            SEARCH_EXCLUSION_RULE_VERSION,
            SEARCH_EXCLUSION_APPROVAL,
        ):
            raise RuntimeError("immutable cognitive search exclusion conflict")
        return "existing"
    connection.execute(
        """
        INSERT INTO cognitive_search_exclusions(
            exclusion_id, channel, source_locator_hash, source_table,
            source_key_json, source_key_hash, source_row_hash,
            reason_code, rule_version, approval_basis, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            exclusion_id,
            candidate.channel,
            candidate.source_locator_hash,
            candidate.source_table,
            candidate.source_key_json,
            candidate.source_key_hash,
            candidate.source_row_hash,
            SEARCH_EXCLUSION_REASON,
            SEARCH_EXCLUSION_RULE_VERSION,
            SEARCH_EXCLUSION_APPROVAL,
            created_at or datetime.now(timezone.utc).isoformat(),
        ),
    )
    return "inserted"


def _locator_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return {
            "blob_hash": "sha256:" + hashlib.sha256(value).hexdigest(),
            "byte_length": len(value),
        }
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


__all__ = [
    "SEARCH_EXCLUSION_APPROVAL",
    "SEARCH_EXCLUSION_DDL_HASH",
    "SEARCH_EXCLUSION_REASON",
    "SEARCH_EXCLUSION_RULE_VERSION",
    "SEARCH_EXCLUSION_SCHEMA_VERSION",
    "SearchExclusionCandidate",
    "build_sqlite_exclusion_candidate",
    "build_wiki_exclusion_candidate",
    "initialize_search_exclusion_ledger",
    "insert_search_exclusion",
    "inventory_search_exclusions",
    "iter_search_exclusion_candidates",
    "iter_sqlite_exclusion_candidates",
    "iter_wiki_exclusion_candidates",
    "load_search_exclusion_keys",
    "search_exclusion_coverage",
    "search_exclusion_identity_key",
    "validate_search_exclusion_ledger",
]
