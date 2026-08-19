"""Object-level provenance inventory for legacy belief-like artifacts.

The reconciler never parses prose into a belief.  It binds a legacy object's
stable source identity, field denominator, and exact content hash, then may
append that envelope to the existing cognitive-state migration quarantine.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping, Sequence

from core.cognitive.state_contract import canonical_json, sha256_json
from core.cognitive.state_schema import (
    _quarantine,
    validate_cognitive_state_schema,
)
from core.db_utils import render_sql
from core.privacy.content_redaction import redact_persistence_value

CANDIDATE_CONTRACT_VERSION = "mnemos.belief_migration_candidate.v1"
RECONCILIATION_REPORT_VERSION = "mnemos.belief_revision_candidate_reconciliation.v1"


@dataclass(frozen=True)
class BeliefMigrationCandidate:
    domain: str
    source_table: str
    source_key: str
    source_identifier: str
    source_content_hash: str
    payload: Mapping[str, Any]


class BeliefCandidateReconciler:
    """Inventory and optionally quarantine exact pre-canonical envelopes."""

    def __init__(
        self,
        *,
        state_db: Path | str,
        wiki_roots: Sequence[Path | str] = (),
        cognitive_graph_dbs: Sequence[Path | str] = (),
        reflection_dbs: Sequence[Path | str] = (),
        profile_dbs: Sequence[Path | str] = (),
    ) -> None:
        self.state_db = Path(state_db)
        self.wiki_roots = tuple(Path(value) for value in wiki_roots)
        self.cognitive_graph_dbs = tuple(Path(value) for value in cognitive_graph_dbs)
        self.reflection_dbs = tuple(Path(value) for value in reflection_dbs)
        self.profile_dbs = tuple(Path(value) for value in profile_dbs)

    def inventory(self) -> tuple[BeliefMigrationCandidate, ...]:
        candidates: list[BeliefMigrationCandidate] = []
        for root in self.wiki_roots:
            candidates.extend(_wiki_candidates(root))
        for path in self.cognitive_graph_dbs:
            candidates.extend(
                _sqlite_candidates(
                    path,
                    domain="cognitive_graph",
                    table_filter=lambda name: name == "cognitive_relations",
                )
            )
        for path in self.reflection_dbs:
            candidates.extend(
                _sqlite_candidates(
                    path,
                    domain="reflection",
                    table_filter=lambda name: "reflection" in name.lower(),
                )
            )
        for path in self.profile_dbs:
            candidates.extend(
                _sqlite_candidates(
                    path,
                    domain="profile_assertion",
                    table_filter=lambda name: (
                        "assertion" in name.lower() or "cognitive_profile" in name.lower()
                    ),
                )
            )
        return tuple(
            sorted(
                candidates,
                key=lambda value: (value.domain, value.source_table, value.source_key),
            )
        )

    def reconcile(
        self,
        *,
        apply: bool = False,
        backup_dir: Path | str | None = None,
        confirm_daemon_stopped: bool = False,
        expected_inventory_hash: str | None = None,
    ) -> dict[str, Any]:
        candidates = self.inventory()
        domain_counts = dict(sorted(Counter(value.domain for value in candidates).items()))
        inventory_hash = sha256_json(
            [
                {
                    "domain": value.domain,
                    "source_table": value.source_table,
                    "source_key": value.source_key,
                    "source_identifier": value.source_identifier,
                    "source_content_hash": value.source_content_hash,
                    "payload": value.payload,
                }
                for value in candidates
            ]
        )
        report: dict[str, Any] = {
            "schema_version": RECONCILIATION_REPORT_VERSION,
            "mode": "apply" if apply else "dry_run",
            "candidate_count": len(candidates),
            "domain_counts": domain_counts,
            "inventory_hash": inventory_hash,
            "inserted_count": 0,
            "existing_count": 0,
            "active_head_delta": 0,
            "active_revision_delta": 0,
            "backup": {},
            "state_integrity_check": "not_run",
        }
        if not apply:
            return report
        if not confirm_daemon_stopped:
            raise ValueError("apply requires explicit confirmation of a stopped daemon")
        if backup_dir is None:
            raise ValueError("apply requires an explicit backup directory")
        reviewed_hash = str(expected_inventory_hash or "").strip()
        if not reviewed_hash:
            raise ValueError("apply requires the reviewed dry-run inventory hash")
        if reviewed_hash != inventory_hash:
            raise ValueError(
                "inventory changed since dry-run; review the new inventory before apply"
            )
        if not self.state_db.is_file():
            raise FileNotFoundError(f"cognitive state database not found: {self.state_db}")

        backup = _backup_sqlite(self.state_db, Path(backup_dir))
        report["backup"] = backup
        conn = sqlite3.connect(self.state_db)
        try:
            validate_cognitive_state_schema(conn)
            before_heads = _count(conn, "cognitive_state_heads")
            before_revisions = _count(conn, "cognitive_state_revisions")
            conn.execute("BEGIN IMMEDIATE")
            for candidate in candidates:
                status, _ = _append_unverified_candidate(
                    conn,
                    source_table=candidate.source_table,
                    source_key=candidate.source_key,
                    payload=candidate.payload,
                )
                report[f"{status}_count"] += 1
            after_heads = _count(conn, "cognitive_state_heads")
            after_revisions = _count(conn, "cognitive_state_revisions")
            report["active_head_delta"] = after_heads - before_heads
            report["active_revision_delta"] = after_revisions - before_revisions
            if report["active_head_delta"] or report["active_revision_delta"]:
                raise RuntimeError("belief migration attempted to create active cognition")
            integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                raise RuntimeError(f"state integrity check failed: {integrity}")
            report["state_integrity_check"] = integrity
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        return report


def _append_unverified_candidate(
    conn: sqlite3.Connection,
    *,
    source_table: str,
    source_key: str,
    payload: Mapping[str, Any],
) -> tuple[str, str]:
    normalized_table = str(source_table or "").strip()
    normalized_key = str(source_key or "").strip()
    if not normalized_table or not normalized_key:
        raise ValueError("migration candidate source identity is required")
    if payload.get("classification") != "unverified_candidate":
        raise ValueError("migration candidate must remain unverified")
    if payload.get("active_schema_upgrade") is not False:
        raise ValueError("migration candidate cannot request an active schema upgrade")
    forbidden = {
        "belief_id",
        "claim_id",
        "stance",
        "confidence",
        "supersedes_revision_id",
        "correction_of_revision_id",
    }
    if forbidden.intersection(payload):
        raise ValueError("migration candidate contains inferred belief semantics")
    redacted = redact_persistence_value(dict(payload))
    if not isinstance(redacted.value, Mapping):
        raise RuntimeError("migration quarantine payload is invalid")
    expected_json = canonical_json(redacted.value)
    expected_hash = sha256_json(redacted.value)
    reason_code = "unverified_belief_candidate"
    existing = conn.execute(
        """
        SELECT quarantine_id, payload_json, payload_hash
        FROM cognitive_state_migration_quarantine
        WHERE source_table=? AND source_key=? AND reason_code=?
        """,
        (normalized_table, normalized_key, reason_code),
    ).fetchone()
    if existing is not None:
        if str(existing[1]) != expected_json or str(existing[2]) != expected_hash:
            raise RuntimeError("immutable quarantine conflict for changed source")
        return "existing", str(existing[0])
    quarantine_id = _quarantine(
        conn,
        source_table=normalized_table,
        source_key=normalized_key,
        reason_code=reason_code,
        payload=payload,
    )
    return "inserted", quarantine_id


def _wiki_candidates(root: Path) -> list[BeliefMigrationCandidate]:
    if not root.is_dir():
        return []
    locator_hash = _locator_hash(root)
    candidates: list[BeliefMigrationCandidate] = []
    for path in sorted(root.rglob("*.md")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        content_hash = _file_hash(path)
        source_identifier = f"wiki://{locator_hash}/{relative}"
        candidates.append(
            _candidate(
                domain="wiki",
                locator_hash=locator_hash,
                source_table_name="markdown_page",
                source_identity={"relative_path": relative},
                source_identifier=source_identifier,
                source_content_hash=content_hash,
                field_manifest=("content", "relative_path"),
            )
        )
    return candidates


def _sqlite_candidates(
    path: Path,
    *,
    domain: str,
    table_filter: Callable[[str], bool],
) -> list[BeliefMigrationCandidate]:
    if not path.is_file():
        return []
    locator_hash = _locator_hash(path)
    uri = f"file:{path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    candidates: list[BeliefMigrationCandidate] = []
    try:
        tables = tuple(
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            if table_filter(str(row[0]))
        )
        for table in tables:
            columns = conn.execute(
                render_sql(
                    "PRAGMA table_info({table})",
                    identifiers={"table": table},
                )
            ).fetchall()
            field_manifest = tuple(sorted(str(row[1]) for row in columns))
            primary_keys = tuple(
                str(row[1])
                for row in sorted(columns, key=lambda value: int(value[5]))
                if int(row[5]) > 0
            )
            if primary_keys:
                rows = conn.execute(
                    render_sql(
                        "SELECT * FROM {table}",
                        identifiers={"table": table},
                    )
                ).fetchall()
            else:
                rows = conn.execute(
                    render_sql(
                        "SELECT rowid AS __mnemos_rowid__, * FROM {table}",
                        identifiers={"table": table},
                    )
                ).fetchall()
            for row in rows:
                values = {key: _json_safe(row[key]) for key in row.keys()}
                source_identity = (
                    {key: values[key] for key in primary_keys}
                    if primary_keys
                    else {"rowid": values["__mnemos_rowid__"]}
                )
                content_hash = sha256_json(values)
                identity_hash = sha256_json(source_identity).split(":", 1)[1]
                source_identifier = f"{domain}://{locator_hash}/{table}/{identity_hash}"
                candidates.append(
                    _candidate(
                        domain=domain,
                        locator_hash=locator_hash,
                        source_table_name=table,
                        source_identity=source_identity,
                        source_identifier=source_identifier,
                        source_content_hash=content_hash,
                        field_manifest=field_manifest,
                    )
                )
    finally:
        conn.close()
    return candidates


def _candidate(
    *,
    domain: str,
    locator_hash: str,
    source_table_name: str,
    source_identity: Mapping[str, Any],
    source_identifier: str,
    source_content_hash: str,
    field_manifest: Sequence[str],
) -> BeliefMigrationCandidate:
    identity_hash = sha256_json(source_identity).split(":", 1)[1]
    source_table = f"legacy_belief_candidate:{domain}:{locator_hash}"
    source_key = f"{source_table_name}:{identity_hash}"
    payload = {
        "schema_version": CANDIDATE_CONTRACT_VERSION,
        "classification": "unverified_candidate",
        "active_schema_upgrade": False,
        "legacy_domain": domain,
        "source_identifier": source_identifier,
        "source_content_hash": source_content_hash,
        "source_locator_hash": locator_hash,
        "source_table_name": source_table_name,
        "source_identity_hash": "sha256:" + identity_hash,
        "source_field_manifest": sorted(str(value) for value in field_manifest),
    }
    return BeliefMigrationCandidate(
        domain=domain,
        source_table=source_table,
        source_key=source_key,
        source_identifier=source_identifier,
        source_content_hash=source_content_hash,
        payload=payload,
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return {
            "blob_hash": "sha256:" + hashlib.sha256(value).hexdigest(),
            "byte_length": len(value),
        }
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _locator_hash(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:24]


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _backup_sqlite(source: Path, backup_dir: Path) -> dict[str, str]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    source_hash = _file_hash(source)
    destination = backup_dir / (
        f"{source.stem}.belief-candidates.{source_hash.split(':', 1)[1][:16]}.db"
    )
    if not destination.exists():
        source_conn = sqlite3.connect(source)
        destination_conn = sqlite3.connect(destination)
        try:
            source_conn.backup(destination_conn)
        finally:
            destination_conn.close()
            source_conn.close()
    with sqlite3.connect(destination) as conn:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    if integrity != "ok":
        raise RuntimeError(f"backup integrity check failed: {integrity}")
    return {
        "path": str(destination),
        "source_hash": source_hash,
        "backup_hash": _file_hash(destination),
        "integrity_check": integrity,
    }


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(
        conn.execute(
            render_sql(
                "SELECT COUNT(*) FROM {table}",
                identifiers={"table": table},
            )
        ).fetchone()[0]
    )


__all__ = [
    "BeliefCandidateReconciler",
    "BeliefMigrationCandidate",
    "CANDIDATE_CONTRACT_VERSION",
    "RECONCILIATION_REPORT_VERSION",
]
