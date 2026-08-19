"""Independent append-only stores for active user cognitive assets.

Knowledge coverage gaps, user cognitive blindspots, and interaction
preferences intentionally do not share a database or identity namespace.  The
knowledge-coverage schema is owned by ``core.app.blindspot_asset_schema``;
this module owns the other two stores and validates each one independently.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.cognitive.user_model_assets import (
    CognitiveAuthorityEvidence,
    InteractionPreference,
    UserCognitiveBlindspot,
    cognitive_evidence_payloads,
)
from core.evidence.source_authority import SourceAuthorityCatalog

REGISTRY_TABLE = "mnemos_schema_registry"
REGISTRY_DDL = f"""
CREATE TABLE IF NOT EXISTS {REGISTRY_TABLE} (
    component TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    ddl_hash TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""


class UserModelAssetStoreError(RuntimeError):
    """A typed user-model asset store is absent, drifted, or misused."""


@dataclass(frozen=True)
class AssetStoreSpec:
    asset_type: str
    asset_prefix: str
    schema_version: str
    schema_component: str
    revision_table: str
    head_table: str
    allowed_statuses: tuple[str, ...]
    initial_status: str
    transitions: Mapping[str, tuple[str, ...]]

    @property
    def revision_ddl(self) -> str:
        status_sql = ", ".join(repr(value) for value in self.allowed_statuses)
        return f"""
CREATE TABLE {self.revision_table} (
    revision_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL,
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    status TEXT NOT NULL CHECK (status IN ({status_sql})),
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    principal_id TEXT NOT NULL DEFAULT '',
    evidence_refs_json TEXT NOT NULL,
    authority_evidence_json TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    invalidation_condition TEXT NOT NULL,
    supersedes_revision_id TEXT,
    consumers_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(asset_id, revision_number),
    FOREIGN KEY (supersedes_revision_id) REFERENCES {self.revision_table}(revision_id)
)
"""

    @property
    def head_ddl(self) -> str:
        return f"""
CREATE TABLE {self.head_table} (
    asset_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL UNIQUE,
    FOREIGN KEY (revision_id) REFERENCES {self.revision_table}(revision_id)
)
"""

    @property
    def index_ddl(self) -> tuple[str, ...]:
        return (
            f"CREATE INDEX idx_{self.asset_type}_scope ON "
            f"{self.revision_table}(scope_type, scope_id, purpose)",
            f"CREATE INDEX idx_{self.asset_type}_status ON "
            f"{self.revision_table}(status, expires_at)",
        )

    @property
    def trigger_ddl(self) -> tuple[str, ...]:
        return (
            f"""CREATE TRIGGER trg_{self.revision_table}_immutable_update
                BEFORE UPDATE ON {self.revision_table}
                BEGIN SELECT RAISE(ABORT, '{self.asset_type} revisions are immutable'); END""",
            f"""CREATE TRIGGER trg_{self.revision_table}_immutable_delete
                BEFORE DELETE ON {self.revision_table}
                BEGIN SELECT RAISE(ABORT, '{self.asset_type} revisions are immutable'); END""",
        )

    @property
    def ddl_hash(self) -> str:
        payload = "\n".join(
            (
                self.revision_ddl.strip(),
                self.head_ddl.strip(),
                *(value.strip() for value in self.index_ddl),
                *(value.strip() for value in self.trigger_ddl),
            )
        )
        return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


USER_COGNITIVE_BLINDSPOT_SPEC = AssetStoreSpec(
    asset_type="user_cognitive_blindspot",
    asset_prefix="ucb_",
    schema_version="mnemos.user_cognitive_blindspot_store.v1",
    schema_component="cognitive.user_cognitive_blindspot",
    revision_table="user_cognitive_blindspot_revisions",
    head_table="user_cognitive_blindspot_heads",
    allowed_statuses=("suspected", "confirmed", "dismissed", "expired"),
    initial_status="suspected",
    transitions={
        "suspected": ("confirmed", "dismissed", "expired"),
        "confirmed": ("dismissed", "expired"),
        "dismissed": (),
        "expired": (),
    },
)

INTERACTION_PREFERENCE_SPEC = AssetStoreSpec(
    asset_type="interaction_preference",
    asset_prefix="ipr_",
    schema_version="mnemos.interaction_preference_store.v1",
    schema_component="cognitive.interaction_preference",
    revision_table="interaction_preference_revisions",
    head_table="interaction_preference_heads",
    allowed_statuses=("active", "invalidated", "expired"),
    initial_status="active",
    transitions={"active": ("invalidated", "expired"), "invalidated": (), "expired": ()},
)


@dataclass(frozen=True)
class AssetStoreState:
    asset_type: str
    status: str
    row_count: int
    current_count: int
    ddl_hash: str
    canonical_ddl_hash: str
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.status == "ready" and not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset_type": self.asset_type,
            "status": self.status,
            "row_count": self.row_count,
            "current_count": self.current_count,
            "ddl_hash": self.ddl_hash,
            "canonical_ddl_hash": self.canonical_ddl_hash,
            "errors": list(self.errors),
            "ok": self.ok,
        }


def _normalized_sql(value: str) -> str:
    return " ".join(str(value or "").strip().rstrip(";").split()).casefold()


def _object_sql(conn: sqlite3.Connection, object_type: str, name: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type=? AND name=?", (object_type, name)
    ).fetchone()
    return str(row[0] or "") if row else ""


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(_object_sql(conn, "table", name))


def inspect_asset_store(conn: sqlite3.Connection, spec: AssetStoreSpec) -> AssetStoreState:
    if not _table_exists(conn, spec.revision_table):
        return AssetStoreState(
            asset_type=spec.asset_type,
            status="uninitialized",
            row_count=0,
            current_count=0,
            ddl_hash="",
            canonical_ddl_hash=spec.ddl_hash,
            errors=(),
        )
    errors: list[str] = []
    if not _table_exists(conn, spec.head_table):
        errors.append(f"missing canonical table: {spec.head_table}")
    if _normalized_sql(_object_sql(conn, "table", spec.revision_table)) != _normalized_sql(
        spec.revision_ddl
    ):
        errors.append(f"canonical table DDL mismatch: {spec.revision_table}")
    if _table_exists(conn, spec.head_table) and _normalized_sql(
        _object_sql(conn, "table", spec.head_table)
    ) != _normalized_sql(spec.head_ddl):
        errors.append(f"canonical table DDL mismatch: {spec.head_table}")
    for statement in spec.index_ddl:
        name = statement.split()[2]
        if _normalized_sql(_object_sql(conn, "index", name)) != _normalized_sql(statement):
            errors.append(f"canonical index missing or drifted: {name}")
    for statement in spec.trigger_ddl:
        name = statement.split()[2]
        if _normalized_sql(_object_sql(conn, "trigger", name)) != _normalized_sql(statement):
            errors.append(f"canonical trigger missing or drifted: {name}")
    registry_hash = ""
    if _table_exists(conn, REGISTRY_TABLE):
        row = conn.execute(
            f"SELECT schema_version, ddl_hash FROM {REGISTRY_TABLE} WHERE component=?",  # nosec B608
            (spec.schema_component,),
        ).fetchone()
        if row:
            registry_hash = str(row[1])
            if str(row[0]) != spec.schema_version or registry_hash != spec.ddl_hash:
                errors.append("asset store registry version/hash mismatch")
        else:
            errors.append("asset store registry entry missing")
    else:
        errors.append("asset store registry table missing")
    row_count = int(conn.execute(f"SELECT COUNT(*) FROM {spec.revision_table}").fetchone()[0])
    current_count = (
        int(conn.execute(f"SELECT COUNT(*) FROM {spec.head_table}").fetchone()[0])
        if _table_exists(conn, spec.head_table)
        else 0
    )
    if not errors:
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            errors.append("asset store foreign-key violations")
        invalid_json = int(conn.execute(f"""SELECT COUNT(*) FROM {spec.revision_table}
                    WHERE NOT json_valid(evidence_refs_json)
                       OR NOT json_valid(authority_evidence_json)
                       OR NOT json_valid(consumers_json)
                       OR NOT json_valid(payload_json)""").fetchone()[0])  # nosec B608
        if invalid_json:
            errors.append(f"invalid asset JSON fields: {invalid_json}")
        noncurrent = int(conn.execute(f"""SELECT COUNT(*) FROM {spec.head_table} h
                    JOIN {spec.revision_table} r ON r.revision_id=h.revision_id
                    JOIN (
                        SELECT asset_id, MAX(revision_number) AS max_revision
                        FROM {spec.revision_table} GROUP BY asset_id
                    ) latest ON latest.asset_id=h.asset_id
                    WHERE r.asset_id != h.asset_id
                       OR r.revision_number != latest.max_revision""").fetchone()[0])  # nosec B608
        if noncurrent:
            errors.append(f"non-current asset heads: {noncurrent}")
    return AssetStoreState(
        asset_type=spec.asset_type,
        status="ready" if not errors else "reconciliation_required",
        row_count=row_count,
        current_count=current_count,
        ddl_hash=registry_hash,
        canonical_ddl_hash=spec.ddl_hash,
        errors=tuple(errors),
    )


def read_asset_store_state(path: Path, spec: AssetStoreSpec) -> AssetStoreState:
    db_path = Path(path).expanduser()
    if not db_path.exists():
        return AssetStoreState(
            asset_type=spec.asset_type,
            status="uninitialized",
            row_count=0,
            current_count=0,
            ddl_hash="",
            canonical_ddl_hash=spec.ddl_hash,
            errors=(),
        )
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        return inspect_asset_store(conn, spec)


def initialize_asset_store(path: Path, spec: AssetStoreSpec) -> None:
    db_path = Path(path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        state = inspect_asset_store(conn, spec)
        if state.ok:
            return
        if state.status != "uninitialized":
            raise UserModelAssetStoreError(
                f"{spec.asset_type} store requires explicit reconciliation"
            )
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(spec.revision_ddl)
            conn.execute(spec.head_ddl)
            for statement in spec.index_ddl:
                conn.execute(statement)
            for statement in spec.trigger_ddl:
                conn.execute(statement)
            conn.execute(REGISTRY_DDL)
            conn.execute(
                f"INSERT INTO {REGISTRY_TABLE}(component, schema_version, ddl_hash, applied_at) "  # nosec B608
                "VALUES (?, ?, ?, ?)",
                (
                    spec.schema_component,
                    spec.schema_version,
                    spec.ddl_hash,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


class _RevisionAssetStore:
    def __init__(self, path: Path, spec: AssetStoreSpec):
        self.path = Path(path).expanduser()
        self.spec = spec

    def schema_status(self) -> dict[str, Any]:
        return read_asset_store_state(self.path, self.spec).as_dict()

    def _connect(self, *, write: bool) -> sqlite3.Connection:
        if write:
            if not self.path.exists():
                raise UserModelAssetStoreError(
                    f"{self.spec.asset_type} store requires explicit reconciliation"
                )
            conn = sqlite3.connect(self.path, timeout=10)
            conn.execute("PRAGMA foreign_keys=ON")
        else:
            if not self.path.exists():
                raise UserModelAssetStoreError(f"{self.spec.asset_type} store is uninitialized")
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        state = inspect_asset_store(conn, self.spec)
        if not state.ok:
            conn.close()
            raise UserModelAssetStoreError(
                f"{self.spec.asset_type} store requires explicit reconciliation"
            )
        return conn

    def list_current(self, *, status: str = "") -> list[dict[str, Any]]:
        with self._connect(write=False) as conn:
            where = "WHERE r.status=?" if status else ""
            parameters: tuple[str, ...] = (status,) if status else ()
            rows = conn.execute(
                f"""SELECT r.* FROM {self.spec.head_table} h
                    JOIN {self.spec.revision_table} r ON r.revision_id=h.revision_id
                    {where} ORDER BY r.asset_id""",  # nosec B608
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def current(self, asset_id: str) -> dict[str, Any] | None:
        with self._connect(write=False) as conn:
            row = conn.execute(
                f"""SELECT r.* FROM {self.spec.head_table} h
                    JOIN {self.spec.revision_table} r ON r.revision_id=h.revision_id
                    WHERE h.asset_id=?""",  # nosec B608
                (asset_id,),
            ).fetchone()
        return dict(row) if row else None

    def append_initial(
        self,
        payload: Mapping[str, Any],
        *,
        evidence_refs: Iterable[str],
        authority_evidence: Iterable[Mapping[str, Any]],
        scope_type: str,
        scope_id: str,
        purpose: str,
        principal_id: str,
        expires_at: str,
        invalidation_condition: str,
        consumers: Iterable[str],
    ) -> bool:
        asset_id = str(payload.get("asset_id") or "")
        revision_id = str(payload.get("revision_id") or "")
        status = str(payload.get("status") or self.spec.initial_status)
        if (
            not asset_id.startswith(self.spec.asset_prefix)
            or revision_id != f"{asset_id}:r1"
            or status != self.spec.initial_status
            or not expires_at
            or not invalidation_condition
        ):
            raise UserModelAssetStoreError(f"invalid initial {self.spec.asset_type} contract")
        with self._connect(write=True) as conn:
            existing = conn.execute(
                f"SELECT revision_id FROM {self.spec.head_table} WHERE asset_id=?",  # nosec B608
                (asset_id,),
            ).fetchone()
            if existing:
                return False
            now = datetime.now(timezone.utc).isoformat()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"""INSERT INTO {self.spec.revision_table} (
                    revision_id, asset_id, revision_number, status,
                    scope_type, scope_id, purpose, principal_id,
                    evidence_refs_json, authority_evidence_json,
                    expires_at, invalidation_condition, supersedes_revision_id,
                    consumers_json, payload_json, created_at
                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)""",  # nosec B608
                (
                    revision_id,
                    asset_id,
                    status,
                    scope_type,
                    scope_id,
                    purpose,
                    principal_id,
                    json.dumps(list(evidence_refs), ensure_ascii=False),
                    json.dumps(list(authority_evidence), ensure_ascii=False, sort_keys=True),
                    expires_at,
                    invalidation_condition,
                    json.dumps(list(consumers), ensure_ascii=False),
                    json.dumps(dict(payload), ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            conn.execute(
                f"INSERT INTO {self.spec.head_table}(asset_id, revision_id) VALUES (?, ?)",  # nosec B608
                (asset_id, revision_id),
            )
            conn.commit()
        return True

    def transition(
        self,
        asset_id: str,
        *,
        expected_revision_id: str,
        next_status: str,
        evidence_refs: Iterable[str],
        authority_evidence: Iterable[Mapping[str, Any]],
        payload_updates: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._connect(write=True) as conn:
            current = conn.execute(
                f"""SELECT r.* FROM {self.spec.head_table} h
                    JOIN {self.spec.revision_table} r ON r.revision_id=h.revision_id
                    WHERE h.asset_id=?""",  # nosec B608
                (asset_id,),
            ).fetchone()
            if current is None or str(current["revision_id"]) != expected_revision_id:
                raise UserModelAssetStoreError("stale or unknown asset revision")
            current_status = str(current["status"])
            if next_status not in self.spec.transitions.get(current_status, ()):
                raise UserModelAssetStoreError(
                    f"invalid {self.spec.asset_type} transition: {current_status}->{next_status}"
                )
            revision_number = int(current["revision_number"]) + 1
            revision_id = f"{asset_id}:r{revision_number}"
            payload: dict[str, Any] = dict(json.loads(str(current["payload_json"])))
            payload.update(
                {
                    "revision_id": revision_id,
                    "status": next_status,
                    "supersedes_revision_id": expected_revision_id,
                }
            )
            payload.update(dict(payload_updates or {}))
            merged_evidence = tuple(
                dict.fromkeys((*json.loads(str(current["evidence_refs_json"])), *evidence_refs))
            )
            merged_authority = [
                *json.loads(str(current["authority_evidence_json"])),
                *authority_evidence,
            ]
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"""INSERT INTO {self.spec.revision_table} (
                    revision_id, asset_id, revision_number, status,
                    scope_type, scope_id, purpose, principal_id,
                    evidence_refs_json, authority_evidence_json,
                    expires_at, invalidation_condition, supersedes_revision_id,
                    consumers_json, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",  # nosec B608
                (
                    revision_id,
                    asset_id,
                    revision_number,
                    next_status,
                    current["scope_type"],
                    current["scope_id"],
                    current["purpose"],
                    current["principal_id"],
                    json.dumps(list(merged_evidence), ensure_ascii=False),
                    json.dumps(merged_authority, ensure_ascii=False, sort_keys=True),
                    current["expires_at"],
                    current["invalidation_condition"],
                    expected_revision_id,
                    current["consumers_json"],
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            updated = conn.execute(
                f"UPDATE {self.spec.head_table} SET revision_id=? "  # nosec B608
                "WHERE asset_id=? AND revision_id=?",
                (revision_id, asset_id, expected_revision_id),
            )
            if updated.rowcount != 1:
                conn.rollback()
                raise UserModelAssetStoreError("asset head changed during transition")
            conn.commit()
            return payload


class UserCognitiveBlindspotStore(_RevisionAssetStore):
    def __init__(self, path: Path):
        super().__init__(path, USER_COGNITIVE_BLINDSPOT_SPEC)

    def persist(
        self,
        blindspot: UserCognitiveBlindspot,
        *,
        evidence: Iterable[CognitiveAuthorityEvidence],
        catalog: SourceAuthorityCatalog,
    ) -> bool:
        authority_payloads = cognitive_evidence_payloads(evidence, catalog)
        required_admission_context = {
            "decision_id",
            "decision_trace_revision_id",
            "decision_trace_hash",
            "session_id",
            "project_id",
            "persona_revision_id",
        }
        if (
            not blindspot.admission_command_id
            or not blindspot.admission_command_hash
            or not blindspot.admission_idempotency_key
            or required_admission_context - set(blindspot.decision_context)
            or any(
                not str(blindspot.decision_context.get(field_name) or "").strip()
                for field_name in required_admission_context
            )
        ):
            raise UserModelAssetStoreError(
                "canonical blindspot persistence requires an admission command"
            )
        evidence_refs = tuple(item["evidence_ref"] for item in authority_payloads)
        blindspot.authority_evidence_refs = evidence_refs
        return self.append_initial(
            asdict(blindspot),
            evidence_refs=(*blindspot.evidence_refs, *evidence_refs),
            authority_evidence=authority_payloads,
            scope_type=blindspot.scope_type,
            scope_id=blindspot.scope_id,
            purpose=blindspot.purpose,
            principal_id=blindspot.principal_id,
            expires_at=blindspot.expires_at,
            invalidation_condition=blindspot.invalidation_condition,
            consumers=blindspot.consumers,
        )

    def current_blindspot(self, asset_id: str) -> UserCognitiveBlindspot | None:
        row = self.current(asset_id)
        if row is None:
            return None
        return UserCognitiveBlindspot(**json.loads(str(row["payload_json"])))

    def transition_blindspot(
        self,
        asset_id: str,
        *,
        expected_revision_id: str,
        next_status: str,
        evidence: Iterable[CognitiveAuthorityEvidence],
        catalog: SourceAuthorityCatalog,
        payload_updates: Mapping[str, Any] | None = None,
    ) -> UserCognitiveBlindspot:
        authority_payloads = cognitive_evidence_payloads(evidence, catalog)
        payload = self.transition(
            asset_id,
            expected_revision_id=expected_revision_id,
            next_status=next_status,
            evidence_refs=tuple(item["evidence_ref"] for item in authority_payloads),
            authority_evidence=authority_payloads,
            payload_updates=payload_updates,
        )
        return UserCognitiveBlindspot(**payload)

    def current_blindspots(self) -> list[UserCognitiveBlindspot]:
        return [
            UserCognitiveBlindspot(**json.loads(str(row["payload_json"])))
            for row in self.list_current()
        ]


class InteractionPreferenceStore(_RevisionAssetStore):
    def __init__(self, path: Path):
        super().__init__(path, INTERACTION_PREFERENCE_SPEC)

    def persist(
        self,
        preference: InteractionPreference,
        *,
        evidence: Iterable[CognitiveAuthorityEvidence],
        catalog: SourceAuthorityCatalog,
    ) -> bool:
        authority_payloads = cognitive_evidence_payloads(evidence, catalog)
        payload = asdict(preference)
        payload["scope"] = asdict(preference.scope)
        payload["authority_evidence_refs"] = tuple(
            item["evidence_ref"] for item in authority_payloads
        )
        return self.append_initial(
            payload,
            evidence_refs=(*preference.evidence_refs, *payload["authority_evidence_refs"]),
            authority_evidence=authority_payloads,
            scope_type=preference.scope.scope_type,
            scope_id=preference.scope.scope_id,
            purpose=preference.scope.purpose,
            principal_id=preference.scope.principal_id,
            expires_at=preference.expires_at,
            invalidation_condition=preference.invalidation_condition,
            consumers=preference.consumers,
        )

    @staticmethod
    def _preference_from_payload(payload: Mapping[str, Any]) -> InteractionPreference:
        from core.cognitive.user_model_assets import AssetScope

        values = dict(payload)
        values.pop("asset_type", None)
        values["scope"] = AssetScope(**dict(values["scope"]))
        return InteractionPreference(**values)

    def transition_preference(
        self,
        asset_id: str,
        *,
        expected_revision_id: str,
        next_status: str,
        evidence: Iterable[CognitiveAuthorityEvidence],
        catalog: SourceAuthorityCatalog,
    ) -> InteractionPreference:
        authority_payloads = cognitive_evidence_payloads(evidence, catalog)
        payload = self.transition(
            asset_id,
            expected_revision_id=expected_revision_id,
            next_status=next_status,
            evidence_refs=tuple(item["evidence_ref"] for item in authority_payloads),
            authority_evidence=authority_payloads,
            payload_updates={
                "invalidation_evidence_refs": tuple(
                    item["evidence_ref"] for item in authority_payloads
                )
            },
        )
        return self._preference_from_payload(payload)

    def current_preferences(self) -> list[InteractionPreference]:
        return [
            self._preference_from_payload(json.loads(str(row["payload_json"])))
            for row in self.list_current()
        ]


__all__ = [
    "AssetStoreSpec",
    "AssetStoreState",
    "INTERACTION_PREFERENCE_SPEC",
    "InteractionPreferenceStore",
    "USER_COGNITIVE_BLINDSPOT_SPEC",
    "UserCognitiveBlindspotStore",
    "UserModelAssetStoreError",
    "initialize_asset_store",
    "inspect_asset_store",
    "read_asset_store_state",
]
