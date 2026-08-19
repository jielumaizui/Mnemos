"""Object-level provenance and deletion for persistent scoring artifacts.

The scoring subsystem has several independently persisted object families:
training samples, ground-truth labels, search sessions, feedback receipts,
models, and Bayesian aggregate state.  This module provides the one typed
sidecar contract for all of them.  It deliberately never derives ownership
from a session-shaped column or a JSON payload: a scoped deletion only follows
validated ACL selectors written with the object itself.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping

from core.db_utils import render_sql
from core.privacy.object_provenance import (
    ObjectProvenance,
    ObjectProvenanceError,
    TRACKED_PROVENANCE_STATE,
    UNATTRIBUTED_PROVENANCE_STATE,
    normalize_scope_selector,
    scope_selector_hash,
)
from core.cognitive.access_control import (
    cognitive_access_hash,
    derive_strictest_cognitive_access,
    validate_cognitive_access_envelope,
)


SCORING_SUBJECT_PROVENANCE_SCHEMA_VERSION = "mnemos.scoring_subject_provenance.v1"
PROVENANCE_TABLE = "scoring_object_provenance"
LINK_TABLE = "scoring_subject_links"
LINEAGE_TABLE = "scoring_object_lineage"
TOMBSTONE_TABLE = "scoring_subject_tombstones"
RECEIPT_TABLE = "scoring_subject_deletion_receipts"
DERIVED_PROVENANCE_STATE = "derived"

# A source identity is intentionally table-local.  No free-text session ID is
# ever used as a fallback selector.
_OBJECT_SPECS: dict[str, tuple[str, str]] = {
    "training_queue": ("scorer_training_queue", "id"),
    "ground_truth": ("ground_truth_signals", "id"),
    "search_session": ("search_sessions", "id"),
    "feedback_event": ("scorer_feedback_events", "feedback_event_id"),
    "model": ("scorer_models", "id"),
    "bayesian_state": ("bayesian_scorer_state", "dimension"),
    "bayesian_feedback": ("bayesian_feedback", "id"),
    "feedback_prompt": ("feedback_prompt_state", "subject"),
}
_DIRECT_OBJECT_TYPES = frozenset(
    {
        "training_queue",
        "ground_truth",
        "search_session",
        "feedback_event",
        "bayesian_feedback",
        "feedback_prompt",
    }
)
_DERIVED_OBJECT_TYPES = frozenset({"model", "bayesian_state"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _receipt_id(request_id: str, scope_kind: str, selector_hash: str) -> str:
    material = "|".join((request_id, scope_kind, selector_hash))
    return "scoring-delete-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


def _object_hash(object_type: str, object_id: str) -> str:
    material = f"{object_type}\x1f{object_id}".encode("utf-8")
    return "sha256:" + hashlib.sha256(material).hexdigest()


def _validate_object_type(object_type: str) -> str:
    normalized = str(object_type or "").strip()
    if normalized not in _OBJECT_SPECS:
        raise ObjectProvenanceError(f"unsupported scoring object type: {object_type}")
    return normalized


def ensure_scoring_subject_provenance_schema(conn: sqlite3.Connection) -> None:
    """Create provenance schema without committing the caller's transaction.

    ``sqlite3.executescript`` performs an implicit commit before executing its
    script.  Writers call this helper after inserting a physical object so an
    implicit commit would make ACL/freeze failures leave an orphaned body.
    Individual DDL statements remain transactional in SQLite.
    """

    statements = (
        f"""CREATE TABLE IF NOT EXISTS {PROVENANCE_TABLE} (
            object_type TEXT NOT NULL,
            object_id TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('tracked', 'derived', 'unattributed')),
            access_json TEXT NOT NULL DEFAULT '',
            access_hash TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            PRIMARY KEY(object_type, object_id)
        )""",
        f"""CREATE TABLE IF NOT EXISTS {LINK_TABLE} (
            object_type TEXT NOT NULL,
            object_id TEXT NOT NULL,
            scope_kind TEXT NOT NULL,
            scope_value_hash TEXT NOT NULL,
            PRIMARY KEY(object_type, object_id, scope_kind, scope_value_hash)
        )""",
        f"""CREATE INDEX IF NOT EXISTS idx_scoring_subject_links_scope
        ON {LINK_TABLE}(scope_kind, scope_value_hash, object_type, object_id)""",
        f"""CREATE TABLE IF NOT EXISTS {LINEAGE_TABLE} (
            object_type TEXT NOT NULL,
            object_id TEXT NOT NULL,
            source_object_type TEXT NOT NULL,
            source_object_id TEXT NOT NULL,
            PRIMARY KEY(object_type, object_id, source_object_type, source_object_id)
        )""",
        f"""CREATE INDEX IF NOT EXISTS idx_scoring_object_lineage_source
        ON {LINEAGE_TABLE}(
            source_object_type, source_object_id, object_type, object_id
        )""",
        f"""CREATE TABLE IF NOT EXISTS {TOMBSTONE_TABLE} (
            object_type TEXT NOT NULL,
            object_id TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            deletion_receipt_id TEXT NOT NULL,
            scope_kind TEXT NOT NULL,
            scope_value_hash TEXT NOT NULL,
            object_hash TEXT NOT NULL,
            tombstoned_at TEXT NOT NULL,
            PRIMARY KEY(object_type, object_id)
        )""",
        f"""CREATE TRIGGER IF NOT EXISTS scoring_subject_tombstone_no_update
        BEFORE UPDATE ON {TOMBSTONE_TABLE} BEGIN
            SELECT RAISE(ABORT, 'scoring subject tombstone is append-only');
        END""",
        f"""CREATE TRIGGER IF NOT EXISTS scoring_subject_tombstone_no_delete
        BEFORE DELETE ON {TOMBSTONE_TABLE} BEGIN
            SELECT RAISE(ABORT, 'scoring subject tombstone is append-only');
        END""",
        f"""CREATE TABLE IF NOT EXISTS {RECEIPT_TABLE} (
            receipt_id TEXT PRIMARY KEY,
            schema_version TEXT NOT NULL,
            request_id TEXT NOT NULL,
            scope_kind TEXT NOT NULL,
            scope_value_hash TEXT NOT NULL,
            target_count INTEGER NOT NULL,
            training_samples_deleted INTEGER NOT NULL,
            ground_truth_deleted INTEGER NOT NULL,
            search_sessions_deleted INTEGER NOT NULL,
            feedback_events_deleted INTEGER NOT NULL,
            models_invalidated INTEGER NOT NULL,
            bayesian_states_invalidated INTEGER NOT NULL,
            bayesian_feedback_deleted INTEGER NOT NULL,
            feedback_prompts_deleted INTEGER NOT NULL,
            after_count INTEGER NOT NULL,
            unresolved_legacy_count INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('flushed', 'applied')),
            created_at TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT '',
            UNIQUE(request_id, scope_kind, scope_value_hash)
        )""",
        f"""CREATE INDEX IF NOT EXISTS idx_scoring_subject_receipts_scope
        ON {RECEIPT_TABLE}(scope_kind, scope_value_hash, status)""",
        f"""CREATE UNIQUE INDEX IF NOT EXISTS idx_scoring_subject_receipts_pending
        ON {RECEIPT_TABLE}(scope_kind, scope_value_hash)
        WHERE status='flushed'""",
    )
    for statement in statements:
        conn.execute(statement)


def scoring_object_is_tombstoned(
    conn: sqlite3.Connection,
    object_type: str,
    object_id: str,
) -> bool:
    """Return whether a persisted scoring object has an immutable tombstone."""

    return conn.execute(
        render_sql(
            "SELECT 1 FROM {table} WHERE object_type=? AND object_id=?",
            identifiers={"table": TOMBSTONE_TABLE},
        ),
        (_validate_object_type(object_type), str(object_id)),
    ).fetchone() is not None


def _write_provenance_row(
    conn: sqlite3.Connection,
    *,
    object_type: str,
    object_id: str,
    state: str,
    access_json: str,
    access_hash: str,
    selector_hashes: Iterable[tuple[str, str]],
) -> None:
    existing = conn.execute(
        render_sql(
            """
        SELECT state, access_json, access_hash FROM {table}
        WHERE object_type=? AND object_id=?
        """,
            identifiers={"table": PROVENANCE_TABLE},
        ),
        (object_type, object_id),
    ).fetchone()
    if existing is not None:
        if tuple(str(value) for value in existing) != (state, access_json, access_hash):
            raise ValueError(
                "immutable scoring provenance conflict for "
                f"{object_type}:{object_id}"
            )
        return
    conn.execute(
        render_sql(
            """
        INSERT INTO {table}(
            object_type, object_id, schema_version, state,
            access_json, access_hash, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
            identifiers={"table": PROVENANCE_TABLE},
        ),
        (
            object_type,
            object_id,
            SCORING_SUBJECT_PROVENANCE_SCHEMA_VERSION,
            state,
            access_json,
            access_hash,
            _now(),
        ),
    )
    links = tuple(sorted(set(selector_hashes)))
    if links:
        conn.executemany(
            render_sql(
                """
            INSERT INTO {table}(
                object_type, object_id, scope_kind, scope_value_hash
            ) VALUES (?, ?, ?, ?)
            """,
                identifiers={"table": LINK_TABLE},
            ),
            ((object_type, object_id, kind, value_hash) for kind, value_hash in links),
        )


def assert_scoring_write_not_frozen(
    subject_provenance: Mapping[str, Any] | None,
) -> None:
    """Block a scoring transaction covered by a durable ownership freeze."""

    if subject_provenance is None:
        return
    access_control = validate_cognitive_access_envelope(subject_provenance)
    from core.config import get_config
    from core.privacy.ownership_freeze import assert_cognitive_write_not_frozen

    assert_cognitive_write_not_frozen(
        get_config(),
        access_control,
        domain="scoring",
    )


def record_scoring_subject_provenance(
    conn: sqlite3.Connection,
    *,
    object_type: str,
    object_id: str,
    subject_provenance: Mapping[str, Any] | None,
) -> None:
    """Persist a direct object's validated ACL sidecar in its write transaction."""

    ensure_scoring_subject_provenance_schema(conn)
    kind = _validate_object_type(object_type)
    if kind not in _DIRECT_OBJECT_TYPES:
        raise ObjectProvenanceError(
            f"{kind} is derived; use record_scoring_derived_object instead"
        )
    object_id = str(object_id)
    if scoring_object_is_tombstoned(conn, kind, object_id):
        raise PermissionError(f"scoring object {kind}:{object_id} is tombstoned")
    if subject_provenance is None:
        _write_provenance_row(
            conn,
            object_type=kind,
            object_id=object_id,
            state=UNATTRIBUTED_PROVENANCE_STATE,
            access_json="",
            access_hash="",
            selector_hashes=(),
        )
        return
    provenance = ObjectProvenance.from_access_control(subject_provenance)
    assert_scoring_write_not_frozen(provenance.access_control)
    _write_provenance_row(
        conn,
        object_type=kind,
        object_id=object_id,
        state=TRACKED_PROVENANCE_STATE,
        access_json=provenance.access_json,
        access_hash=provenance.access_hash,
        selector_hashes=provenance.selector_hashes,
    )


def get_scoring_object_access_control(
    conn: sqlite3.Connection,
    *,
    object_type: str,
    object_id: str,
) -> dict[str, Any] | None:
    """Return one validated ACL header without reading its scoring payload.

    Missing, unattributed, malformed, or unknown-schema sidecars are all
    denied.  Callers use this seam before hydrating query/features/body fields
    and then apply the normal cognitive authorization policy.
    """

    kind = _validate_object_type(object_type)
    try:
        row = conn.execute(
            render_sql(
                """
            SELECT state, access_json
            FROM {table}
            WHERE object_type=? AND object_id=?
            """,
                identifiers={"table": PROVENANCE_TABLE},
            ),
            (kind, str(object_id)),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None or str(row[0]) not in {
        TRACKED_PROVENANCE_STATE,
        DERIVED_PROVENANCE_STATE,
    }:
        return None
    try:
        return validate_cognitive_access_envelope(json.loads(str(row[1] or "")))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _aggregate_authorization_boundary(access_control: Mapping[str, Any]) -> str:
    """Return fields that decide whether two sources may share one aggregate.

    Consent evidence and source hashes remain exact in the source-object
    sidecars and lineage table.  They may grow without changing who can read
    the aggregate.  Any field that can broaden or narrow authorization must
    remain identical for an in-place aggregate update.
    """

    access = validate_cognitive_access_envelope(access_control)
    boundary = {
        "owner": access["owner"],
        "scope": access["scope"],
        "purposes": access["purposes"],
        "consent_status": access["consent"]["status"],
        "sensitivity": access["sensitivity"],
        "retention_policy": access["retention_policy"],
        "redaction_policy": access["redaction_policy"],
        "visibility": access["visibility"],
        "declassification": access["declassification"],
    }
    return json.dumps(
        boundary,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def record_scoring_derived_object(
    conn: sqlite3.Connection,
    *,
    object_type: str,
    object_id: str,
    source_refs: Iterable[tuple[str, str]] | None,
) -> None:
    """Record a derived object with the union of every exact source selector.

    A derived object is only marked ``derived`` when *all* sources already have
    tracked/derived provenance and each exposes an exact selector.  Otherwise
    it remains explicitly unattributed; callers cannot upgrade it later by
    supplying a more convenient ACL.  ``None`` means an operational rewrite
    with no new data source: preserve an existing sidecar, or initialize a new
    object as explicitly unattributed.
    """

    ensure_scoring_subject_provenance_schema(conn)
    kind = _validate_object_type(object_type)
    if kind not in _DERIVED_OBJECT_TYPES:
        raise ObjectProvenanceError(f"{kind} is not a derived scoring object")
    object_id = str(object_id)
    if scoring_object_is_tombstoned(conn, kind, object_id):
        raise PermissionError(f"scoring object {kind}:{object_id} is tombstoned")
    existing = conn.execute(
        render_sql(
            "SELECT state, access_json FROM {table} "
            "WHERE object_type=? AND object_id=?",
            identifiers={"table": PROVENANCE_TABLE},
        ),
        (kind, object_id),
    ).fetchone()
    if source_refs is None:
        if existing is None:
            _write_provenance_row(
                conn,
                object_type=kind,
                object_id=object_id,
                state=UNATTRIBUTED_PROVENANCE_STATE,
                access_json="",
                access_hash="",
                selector_hashes=(),
            )
        return
    sources = tuple(
        sorted(
            {
                (_validate_object_type(source_type), str(source_id))
                for source_type, source_id in source_refs
                if str(source_id)
            }
        )
    )
    source_states: list[str] = []
    source_access_controls: list[dict[str, Any]] = []
    selector_hashes: set[tuple[str, str]] = set()
    for source_type, source_id in sources:
        row = conn.execute(
            render_sql(
                """
            SELECT state, access_json FROM {table}
            WHERE object_type=? AND object_id=?
            """,
                identifiers={"table": PROVENANCE_TABLE},
            ),
            (source_type, source_id),
        ).fetchone()
        if row is None:
            source_states.append(UNATTRIBUTED_PROVENANCE_STATE)
            continue
        source_states.append(str(row[0]))
        try:
            source_access_controls.append(
                validate_cognitive_access_envelope(json.loads(str(row[1] or "")))
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            source_access_controls.append({})
        selector_hashes.update(
            (str(link[0]), str(link[1]))
            for link in conn.execute(
                render_sql(
                    """
                SELECT scope_kind, scope_value_hash FROM {table}
                WHERE object_type=? AND object_id=?
                """,
                    identifiers={"table": LINK_TABLE},
                ),
                (source_type, source_id),
            ).fetchall()
        )
    derived_is_tracked = (
        bool(sources)
        and bool(selector_hashes)
        and len(source_access_controls) == len(sources)
        and all(source_access_controls)
        and all(
            state in {TRACKED_PROVENANCE_STATE, DERIVED_PROVENANCE_STATE}
            for state in source_states
        )
    )
    desired_state = (
        DERIVED_PROVENANCE_STATE if derived_is_tracked else UNATTRIBUTED_PROVENANCE_STATE
    )
    derived_access_json = ""
    derived_access_hash = ""
    for source_access_control in source_access_controls:
        if source_access_control:
            assert_scoring_write_not_frozen(source_access_control)
    if derived_is_tracked:
        allowed_purposes = set(source_access_controls[0]["purposes"])
        for source_access in source_access_controls[1:]:
            allowed_purposes.intersection_update(source_access["purposes"])
        if not allowed_purposes:
            derived_is_tracked = False
            desired_state = UNATTRIBUTED_PROVENANCE_STATE
        else:
            first_access = source_access_controls[0]
            derived_access = derive_strictest_cognitive_access(
                source_access_controls,
                owner_principal_id=str(first_access["owner"]["principal_id"]),
                owner_agent=str(first_access["owner"]["agent"]),
                scope_type=kind,
                scope_id=object_id,
                purposes=tuple(sorted(allowed_purposes)),
                retention_policy="scoring_derived_retention",
            )
            derived_access_json = json.dumps(
                derived_access,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            derived_access_hash = cognitive_access_hash(derived_access)
    if (
        existing is not None
        and str(existing[0]) == DERIVED_PROVENANCE_STATE
        and desired_state == DERIVED_PROVENANCE_STATE
    ):
        try:
            existing_access = validate_cognitive_access_envelope(
                json.loads(str(existing[1] or ""))
            )
            candidate_access = validate_cognitive_access_envelope(
                json.loads(derived_access_json)
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ObjectProvenanceError(
                f"derived scoring aggregate ACL conflict for {kind}:{object_id}"
            ) from exc
        if _aggregate_authorization_boundary(
            existing_access
        ) != _aggregate_authorization_boundary(candidate_access):
            raise ObjectProvenanceError(
                f"derived scoring aggregate ACL conflict for {kind}:{object_id}"
            )
    if existing is None:
        _write_provenance_row(
            conn,
            object_type=kind,
            object_id=object_id,
            state=desired_state,
            access_json=derived_access_json,
            access_hash=derived_access_hash,
            selector_hashes=selector_hashes if derived_is_tracked else (),
        )
    elif str(existing[0]) not in {desired_state, UNATTRIBUTED_PROVENANCE_STATE}:
        raise ValueError(f"immutable scoring provenance conflict for {kind}:{object_id}")

    if sources:
        conn.executemany(
            render_sql(
                """
            INSERT OR IGNORE INTO {table}(
                object_type, object_id, source_object_type, source_object_id
            ) VALUES (?, ?, ?, ?)
            """,
                identifiers={"table": LINEAGE_TABLE},
            ),
            ((kind, object_id, source_type, source_id) for source_type, source_id in sources),
        )
    # Existing derived rows may receive more source objects as an aggregate is
    # incrementally updated.  Their inherited selector set only grows; it can
    # never be narrowed by a later caller.
    if existing is not None and str(existing[0]) == DERIVED_PROVENANCE_STATE and selector_hashes:
        conn.executemany(
            render_sql(
                """
            INSERT OR IGNORE INTO {table}(
                object_type, object_id, scope_kind, scope_value_hash
            ) VALUES (?, ?, ?, ?)
            """,
                identifiers={"table": LINK_TABLE},
            ),
            ((kind, object_id, link_kind, value_hash) for link_kind, value_hash in selector_hashes),
        )


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def _physical_objects(
    conn: sqlite3.Connection, *, tables: set[str]
) -> set[tuple[str, str]]:
    objects: set[tuple[str, str]] = set()
    for object_type, (table, id_column) in _OBJECT_SPECS.items():
        if table not in tables:
            continue
        rows = conn.execute(
            render_sql(
                "SELECT {id_column} FROM {table}",
                identifiers={"id_column": id_column, "table": table},
            )
        ).fetchall()
        objects.update((object_type, str(row[0])) for row in rows)
    return objects


def _active_objects_for_scope(
    conn: sqlite3.Connection,
    *,
    tables: set[str],
    scope_kind: str,
    selector_hash: str,
) -> list[tuple[str, str]]:
    physical = _physical_objects(conn, tables=tables)
    if scope_kind == "all":
        candidates = physical
    else:
        rows = conn.execute(
            render_sql(
                """
            SELECT provenance.object_type, provenance.object_id
            FROM {provenance_table} AS provenance
            JOIN {link_table} AS link
              ON link.object_type=provenance.object_type
             AND link.object_id=provenance.object_id
            WHERE provenance.state IN (?, ?)
              AND link.scope_kind=?
              AND link.scope_value_hash=?
            """,
                identifiers={
                    "provenance_table": PROVENANCE_TABLE,
                    "link_table": LINK_TABLE,
                },
            ),
            (TRACKED_PROVENANCE_STATE, DERIVED_PROVENANCE_STATE, scope_kind, selector_hash),
        ).fetchall()
        candidates = {(str(row[0]), str(row[1])) for row in rows} & physical
    active: list[tuple[str, str]] = []
    for object_type, object_id in candidates:
        if not scoring_object_is_tombstoned(conn, object_type, object_id):
            active.append((object_type, object_id))
    return sorted(active)


def _unresolved_historical_count(
    conn: sqlite3.Connection,
    *,
    tables: set[str],
    scope_kind: str,
) -> int:
    if scope_kind == "all":
        return 0
    count = 0
    for object_type, object_id in _physical_objects(conn, tables=tables):
        if scoring_object_is_tombstoned(conn, object_type, object_id):
            continue
        row = conn.execute(
            render_sql(
                """
            SELECT state FROM {table}
            WHERE object_type=? AND object_id=?
            """,
                identifiers={"table": PROVENANCE_TABLE},
            ),
            (object_type, object_id),
        ).fetchone()
        if row is None or str(row[0]) == UNATTRIBUTED_PROVENANCE_STATE:
            count += 1
    return count


def _delete_object(
    conn: sqlite3.Connection,
    *,
    object_type: str,
    object_id: str,
    tables: set[str],
) -> dict[str, int]:
    """Delete one physical object and return body-free count deltas."""

    table, id_column = _OBJECT_SPECS[object_type]
    deltas = {
        "training_samples_deleted": 0,
        "ground_truth_deleted": 0,
        "search_sessions_deleted": 0,
        "feedback_events_deleted": 0,
        "models_invalidated": 0,
        "bayesian_states_invalidated": 0,
        "bayesian_feedback_deleted": 0,
        "feedback_prompts_deleted": 0,
    }
    if table not in tables:
        return deltas
    deleted = int(
        conn.execute(
            render_sql(
                "DELETE FROM {table} WHERE {id_column}=?",
                identifiers={"table": table, "id_column": id_column},
            ),
            (object_id,),
        ).rowcount
        or 0
    )
    deleted = max(0, deleted)
    key_by_type = {
        "training_queue": "training_samples_deleted",
        "ground_truth": "ground_truth_deleted",
        "search_session": "search_sessions_deleted",
        "feedback_event": "feedback_events_deleted",
        "model": "models_invalidated",
        "bayesian_state": "bayesian_states_invalidated",
        "bayesian_feedback": "bayesian_feedback_deleted",
        "feedback_prompt": "feedback_prompts_deleted",
    }
    deltas[key_by_type[object_type]] += deleted
    # Bayesian state is an aggregate, not a removable per-subject contribution.
    # Once one exact source is deleted, discard its same-dimension feedback log
    # too rather than retaining an unverifiable aggregate contribution.
    if object_type == "bayesian_state" and "bayesian_feedback" in tables:
        feedback_deleted = int(
            conn.execute(
                "DELETE FROM bayesian_feedback WHERE dimension=?", (object_id,)
            ).rowcount
            or 0
        )
        deltas["bayesian_feedback_deleted"] += max(0, feedback_deleted)
    return deltas


def _result(
    *,
    status: str,
    target_count: int,
    after_count: int,
    unresolved_legacy_count: int,
    values: Mapping[str, int],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": status,
        "target_count": target_count,
        "receipt_count": 1,
        "after_count": after_count,
        "unresolved_legacy_count": unresolved_legacy_count,
        "verified": (
            status in {"applied", "existing"}
            and after_count == 0
            and unresolved_legacy_count == 0
        ),
    }
    result.update({key: int(value) for key, value in values.items()})
    return result


def _blocked(error: str) -> dict[str, Any]:
    return {
        "status": "blocked",
        "target_count": 0,
        "receipt_count": 0,
        "verified": False,
        "error": error,
    }


_COUNT_KEYS = (
    "training_samples_deleted",
    "ground_truth_deleted",
    "search_sessions_deleted",
    "feedback_events_deleted",
    "models_invalidated",
    "bayesian_states_invalidated",
    "bayesian_feedback_deleted",
    "feedback_prompts_deleted",
)


def _receipt_values(row: sqlite3.Row) -> dict[str, int | str]:
    values: dict[str, int | str] = {
        "receipt_id": str(row["receipt_id"]),
        "status": str(row["status"]),
        "target_count": int(row["target_count"] or 0),
        "unresolved_legacy_count": int(row["unresolved_legacy_count"] or 0),
    }
    values.update({key: int(row[key] or 0) for key in _COUNT_KEYS})
    return values


def _result_from_receipt(
    *, status: str, after_count: int, values: Mapping[str, int | str]
) -> dict[str, Any]:
    return _result(
        status=status,
        target_count=int(values["target_count"]),
        after_count=after_count,
        unresolved_legacy_count=int(values["unresolved_legacy_count"]),
        values={key: int(values[key]) for key in _COUNT_KEYS},
    )


def delete_scoring_subject_scope(
    *,
    db_path: Path | str,
    request_id: str,
    scope_kind: str,
    scope_value: str,
) -> dict[str, Any]:
    """Delete exact scoring objects and invalidate their persisted derivatives.

    Missing lineage is never guessed.  A scoped operation will return an
    unverified receipt when unrelated live scoring objects remain unattributed,
    even though the exact linked objects have already been deleted.
    """

    try:
        kind, value = normalize_scope_selector(scope_kind, scope_value)
    except ObjectProvenanceError:
        return {
            "status": "unsupported_scope",
            "target_count": 0,
            "receipt_count": 0,
            "verified": False,
        }
    database = Path(db_path).expanduser()
    if not database.is_file():
        return {
            "status": "not_initialized",
            "target_count": 0,
            "receipt_count": 0,
            "unresolved_legacy_count": 0,
            "verified": True,
        }
    if not str(request_id or "").strip():
        raise ValueError("scoring deletion requires request_id")

    selector_hash = scope_selector_hash(kind, value)
    receipt_id = _receipt_id(str(request_id), kind, selector_hash)
    values: dict[str, int | str]
    targets: list[tuple[str, str]]
    try:
        with sqlite3.connect(str(database), timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            tables = _table_names(conn)
            known_tables = {table for table, _ in _OBJECT_SPECS.values()} & tables
            if not known_tables:
                return {
                    "status": "not_initialized",
                    "target_count": 0,
                    "receipt_count": 0,
                    "unresolved_legacy_count": 0,
                    "verified": True,
                }
            ensure_scoring_subject_provenance_schema(conn)
            conn.commit()
            fields = (
                "receipt_id",
                "status",
                "target_count",
                "unresolved_legacy_count",
                *_COUNT_KEYS,
            )
            existing = conn.execute(
                render_sql(
                    "SELECT {fields} FROM {receipt_table} "
                    "WHERE request_id=? AND scope_kind=? AND scope_value_hash=?",
                    identifiers={"receipt_table": RECEIPT_TABLE},
                    identifier_lists={"fields": fields},
                ),
                (request_id, kind, selector_hash),
            ).fetchone()
            pending = conn.execute(
                render_sql(
                    "SELECT {fields} FROM {receipt_table} "
                    "WHERE scope_kind=? AND scope_value_hash=? AND status='flushed' "
                    "ORDER BY created_at ASC LIMIT 1",
                    identifiers={"receipt_table": RECEIPT_TABLE},
                    identifier_lists={"fields": fields},
                ),
                (kind, selector_hash),
            ).fetchone()
            selected = pending or existing
            if selected is not None:
                values = _receipt_values(selected)
                receipt_id = str(values["receipt_id"])
                if values["status"] == "applied":
                    return _result_from_receipt(status="existing", after_count=0, values=values)
                # A crash may happen after the durable tombstone/``flushed``
                # receipt commit and before the physical-after oracle.  The
                # resume path must rehydrate the immutable target set rather
                # than treating an empty in-memory list as proof of deletion.
                targets = [
                    (str(row["object_type"]), str(row["object_id"]))
                    for row in conn.execute(
                        render_sql(
                            """
                        SELECT object_type, object_id
                        FROM {table}
                        WHERE deletion_receipt_id=?
                        ORDER BY object_type, object_id
                        """,
                            identifiers={"table": TOMBSTONE_TABLE},
                        ),
                        (receipt_id,),
                    ).fetchall()
                ]
                if len(targets) != int(values["target_count"]):
                    return _blocked("scoring_subject_tombstone_receipt_mismatch")
            else:
                secure_delete = conn.execute("PRAGMA secure_delete=ON").fetchone()
                if not secure_delete or int(secure_delete[0] or 0) < 1:
                    return _blocked("scoring_provenance_secure_delete_unavailable")
                conn.execute("BEGIN IMMEDIATE")
                targets = _active_objects_for_scope(
                    conn,
                    tables=tables,
                    scope_kind=kind,
                    selector_hash=selector_hash,
                )
                count_values = {key: 0 for key in _COUNT_KEYS}
                for object_type, object_id in targets:
                    conn.execute(
                        render_sql(
                            """
                        INSERT INTO {table}(
                            object_type, object_id, schema_version, deletion_receipt_id,
                            scope_kind, scope_value_hash, object_hash, tombstoned_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                            identifiers={"table": TOMBSTONE_TABLE},
                        ),
                        (
                            object_type,
                            object_id,
                            SCORING_SUBJECT_PROVENANCE_SCHEMA_VERSION,
                            receipt_id,
                            kind,
                            selector_hash,
                            _object_hash(object_type, object_id),
                            _now(),
                        ),
                    )
                    deltas = _delete_object(
                        conn,
                        object_type=object_type,
                        object_id=object_id,
                        tables=tables,
                    )
                    for key, count in deltas.items():
                        count_values[key] += count
                if targets:
                    conn.executemany(
                        render_sql(
                            "DELETE FROM {table} WHERE object_type=? AND object_id=?",
                            identifiers={"table": LINK_TABLE},
                        ),
                        targets,
                    )
                    conn.executemany(
                        render_sql(
                            "DELETE FROM {table} WHERE object_type=? AND object_id=?",
                            identifiers={"table": PROVENANCE_TABLE},
                        ),
                        targets,
                    )
                    for object_type, object_id in targets:
                        conn.execute(
                            render_sql(
                                """
                            DELETE FROM {table}
                            WHERE (object_type=? AND object_id=?)
                               OR (source_object_type=? AND source_object_id=?)
                            """,
                                identifiers={"table": LINEAGE_TABLE},
                            ),
                            (object_type, object_id, object_type, object_id),
                        )
                unresolved = _unresolved_historical_count(
                    conn, tables=tables, scope_kind=kind
                )
                conn.execute(
                    render_sql(
                        """
                    INSERT INTO {table}(
                        receipt_id, schema_version, request_id, scope_kind, scope_value_hash,
                        target_count, training_samples_deleted, ground_truth_deleted,
                        search_sessions_deleted, feedback_events_deleted, models_invalidated,
                        bayesian_states_invalidated, bayesian_feedback_deleted,
                        feedback_prompts_deleted, after_count, unresolved_legacy_count,
                        status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'flushed', ?)
                    """,
                        identifiers={"table": RECEIPT_TABLE},
                    ),
                    (
                        receipt_id,
                        SCORING_SUBJECT_PROVENANCE_SCHEMA_VERSION,
                        request_id,
                        kind,
                        selector_hash,
                        len(targets),
                        *(count_values[key] for key in _COUNT_KEYS),
                        unresolved,
                        _now(),
                    ),
                )
                conn.commit()
                values = {
                    "receipt_id": receipt_id,
                    "status": "flushed",
                    "target_count": len(targets),
                    "unresolved_legacy_count": unresolved,
                    **count_values,
                }
    except (sqlite3.Error, OSError, ValueError):
        return _blocked("scoring_subject_deletion_failed")

    try:
        with sqlite3.connect(str(database), timeout=10) as conn:
            tables = _table_names(conn)
            checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is not None and int(checkpoint[0] or 0) != 0:
                return _result_from_receipt(
                    status="pending_checkpoint", after_count=0, values=values
                )
            remaining = _physical_objects(conn, tables=tables) & set(targets)
            after_count = len(remaining)
            if after_count:
                return _blocked("scoring_subject_after_oracle_nonzero")
            conn.execute(
                render_sql(
                    """
                UPDATE {table}
                SET status='applied', after_count=0, applied_at=?
                WHERE receipt_id=? AND status='flushed'
                """,
                    identifiers={"table": RECEIPT_TABLE},
                ),
                (_now(), receipt_id),
            )
            conn.commit()
    except (sqlite3.Error, OSError, ValueError):
        return _result_from_receipt(status="pending_checkpoint", after_count=0, values=values)

    return _result_from_receipt(status="applied", after_count=0, values=values)
