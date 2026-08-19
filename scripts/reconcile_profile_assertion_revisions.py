#!/usr/bin/env python3
"""Reconcile legacy persona assertion projections into an immutable history.

Dry-run is read-only.  ``--apply`` requires an explicit backup directory and
uses SQLite's backup API before it creates the revision ledger or anchors any
existing projection rows.  It never fabricates a revision for a row it cannot
decode.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.migrations.model_call_ledger_reconcile.runtime import (  # noqa: E402
    runtime_writers_are_inactive,
)
from core.ops.offline_migration_lock import offline_migration_lock  # noqa: E402
from core.persona.cognitive_profile import (  # noqa: E402
    PROFILE_SCHEMA_SQL,
    clamp_confidence,
    ensure_cognitive_profile_access_schema,
    inspect_cognitive_profile_runtime_schema,
    parse_json_list,
    register_cognitive_profile_runtime_schema,
    validate_cognitive_profile_runtime_schema,
)
from core.persona.profile_assertion_schema import (  # noqa: E402
    PROFILE_ASSERTION_PROJECTION_SQL,
    PROFILE_ASSERTION_SCHEMA_SQL,
    inspect_profile_assertion_schema,
    profile_assertion_projection_is_canonical,
    register_profile_assertion_schema,
    validate_profile_assertion_schema,
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _content_hash(row: sqlite3.Row) -> str:
    payload = {
        "dimension": row["dimension"],
        "claim": row["claim"],
        "supporting_signals": parse_json_list(row["supporting_signals"]),
        "contradicting_signals": parse_json_list(row["contradicting_signals"]),
        "confidence": clamp_confidence(row["confidence"]),
        "privacy_level": row["privacy_level"],
        "revision_policy": row["revision_policy"],
        "status": row["status"],
        "access_control": row["access_control"],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _execute_script_in_transaction(
    conn: sqlite3.Connection,
    script: str,
    *,
    failpoint: Callable[[str], None] | None = None,
    stage_prefix: str = "schema",
) -> None:
    """Execute canonical DDL without sqlite3.executescript's implicit commit."""

    statement = ""
    statement_number = 0
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            if statement.strip():
                conn.execute(statement)
                statement_number += 1
                if failpoint is not None:
                    failpoint(f"{stage_prefix}_statement:{statement_number}")
            statement = ""
    if statement.strip():
        raise ValueError("incomplete canonical profile assertion schema")


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _logical_database_hash(conn: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for line in conn.iterdump():
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def _integrity_state(conn: sqlite3.Connection) -> tuple[bool, list[str]]:
    integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
    try:
        foreign_keys = [
            "|".join(str(item) for item in row)
            for row in conn.execute("PRAGMA foreign_key_check").fetchall()
        ]
    except sqlite3.OperationalError as exc:
        foreign_keys = [f"operational_error:{exc}"]
    return integrity == ["ok"], foreign_keys


def _reserve_unique_sqlite_path(
    root: Path,
    *,
    stem: str,
    generation: str | None = None,
) -> tuple[Path, str]:
    resolved_generation = str(generation or uuid4().hex)
    target = root / f"{stem}.{resolved_generation}.sqlite"
    try:
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError("backup generation collision") from exc
    os.close(fd)
    return target, resolved_generation


def _backup_database(source_path: Path, backup_path: Path) -> None:
    with sqlite3.connect(source_path) as source, sqlite3.connect(backup_path) as backup:
        source.backup(backup)


def _restore_drill(backup_path: Path, backup_root: Path) -> bool:
    drill_path, _generation = _reserve_unique_sqlite_path(
        backup_root,
        stem=".profile-v2-restore-drill",
    )
    try:
        with sqlite3.connect(backup_path) as backup, sqlite3.connect(drill_path) as restored:
            backup.backup(restored)
        with sqlite3.connect(backup_path) as backup, sqlite3.connect(drill_path) as restored:
            backup_ok, backup_fk = _integrity_state(backup)
            restored_ok, restored_fk = _integrity_state(restored)
            return bool(
                backup_ok
                and restored_ok
                and backup_fk == restored_fk
                and _logical_database_hash(backup) == _logical_database_hash(restored)
            )
    finally:
        drill_path.unlink(missing_ok=True)


def inspect(db_path: Path) -> dict[str, Any]:
    resolved = db_path.expanduser().resolve(strict=False)
    payload: dict[str, Any] = {
        "schema_version": "mnemos.profile_assertion_revisions.reconcile.v1",
        "db_path": str(resolved),
        "read_only": True,
        "ok": False,
        "errors": [],
    }
    if not resolved.is_file():
        payload["errors"].append("persona_signal_store_uninitialized")
        return payload
    # A plain read-only URI includes the current WAL state; immutable reads do
    # not, and could undercount projections awaiting reconciliation.
    with sqlite3.connect(f"file:{resolved}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        if not _table_exists(conn, "profile_assertions"):
            payload["errors"].append("profile_assertions_missing")
            return payload
        current_count = int(conn.execute("SELECT COUNT(*) FROM profile_assertions").fetchone()[0])
        revision_exists = _table_exists(conn, "profile_assertion_revisions")
        revision_count = (
            int(conn.execute("SELECT COUNT(*) FROM profile_assertion_revisions").fetchone()[0])
            if revision_exists
            else 0
        )
        missing_history = current_count
        if revision_exists:
            missing_history = int(conn.execute("""
                    SELECT COUNT(*) FROM profile_assertions AS current
                    WHERE NOT EXISTS (
                        SELECT 1 FROM profile_assertion_revisions AS revision
                        WHERE revision.assertion_id=current.assertion_id
                    )
                    """).fetchone()[0])
        state = inspect_profile_assertion_schema(conn)
        runtime_state = inspect_cognitive_profile_runtime_schema(conn)
        head_gap = current_count
        projection_head_mismatch = current_count
        if revision_exists and _table_exists(conn, "profile_assertion_heads"):
            head_gap = int(conn.execute("""
                    SELECT COUNT(*)
                    FROM profile_assertions AS current
                    WHERE NOT EXISTS (
                        SELECT 1 FROM profile_assertion_heads AS head
                        WHERE head.assertion_id=current.assertion_id
                    )
                    """).fetchone()[0])
            if "current_revision_id" in {
                str(row[1]) for row in conn.execute("PRAGMA table_info(profile_assertions)")
            }:
                projection_head_mismatch = int(conn.execute("""
                        SELECT COUNT(*)
                        FROM profile_assertions AS current
                        LEFT JOIN profile_assertion_heads AS head
                          ON head.assertion_id=current.assertion_id
                        WHERE head.revision_id IS NULL
                           OR current.current_revision_id IS NOT head.revision_id
                        """).fetchone()[0])
        payload.update(
            {
                "current_projection_count": current_count,
                "revision_table_exists": revision_exists,
                "revision_count": revision_count,
                "missing_history_count": missing_history,
                "needs_schema_install": not revision_exists,
                "head_gap_count": head_gap,
                "projection_head_mismatch": projection_head_mismatch,
                "schema_errors": list(state.errors),
                "runtime_schema_errors": list(runtime_state["errors"]),
                "partial_profile_schema_migration": int(not runtime_state["ok"]),
                "source_logical_hash": _logical_database_hash(conn),
            }
        )
        integrity_rows = [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
        try:
            foreign_key_rows = [
                "|".join(str(item) for item in row)
                for row in conn.execute("PRAGMA foreign_key_check").fetchall()
            ]
        except sqlite3.OperationalError as exc:
            foreign_key_rows = [f"operational_error:{exc}"]
        payload["source_integrity_ok"] = integrity_rows == ["ok"]
        payload["source_foreign_key_errors"] = foreign_key_rows
    plan_binding = {
        "schema_version": payload["schema_version"],
        "db_path": payload["db_path"],
        "source_logical_hash": payload["source_logical_hash"],
        "current_projection_count": payload["current_projection_count"],
        "revision_count": payload["revision_count"],
        "missing_history_count": payload["missing_history_count"],
        "head_gap_count": payload["head_gap_count"],
        "projection_head_mismatch": payload["projection_head_mismatch"],
        "schema_errors": payload["schema_errors"],
        "runtime_schema_errors": payload["runtime_schema_errors"],
    }
    payload["plan_hash"] = _canonical_hash(plan_binding)
    payload["ok"] = (
        bool(payload["revision_table_exists"])
        and not payload["schema_errors"]
        and not payload["runtime_schema_errors"]
        and payload["missing_history_count"] == 0
        and payload["head_gap_count"] == 0
        and payload["projection_head_mismatch"] == 0
        and payload["source_integrity_ok"]
        and not payload["source_foreign_key_errors"]
    )
    return payload


def _rebuild_profile_assertion_projection(
    conn: sqlite3.Connection,
    *,
    failpoint: Callable[[str], None] | None,
) -> None:
    legacy_table = "profile_assertions_profile_v2_legacy"
    if _table_exists(conn, legacy_table):
        raise RuntimeError("legacy profile assertion projection staging table already exists")
    original_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(profile_assertions)")
    }
    canonical_columns = (
        "assertion_id",
        "current_revision_id",
        "dimension",
        "claim",
        "supporting_signals",
        "contradicting_signals",
        "confidence",
        "privacy_level",
        "last_verified_at",
        "revision_policy",
        "status",
        "access_control",
        "updated_at",
    )
    copied_columns = [column for column in canonical_columns if column in original_columns]
    conn.execute(f"ALTER TABLE profile_assertions RENAME TO {legacy_table}")  # nosec B608
    if failpoint is not None:
        failpoint("projection_rebuild_rename_statement:1")
    for index, index_name in enumerate(
        ("idx_profile_assertions_dimension", "idx_profile_assertions_status"),
        start=1,
    ):
        conn.execute(f"DROP INDEX IF EXISTS {index_name}")  # nosec B608
        if failpoint is not None:
            failpoint(f"projection_rebuild_drop_index_statement:{index}")
    _execute_script_in_transaction(
        conn,
        PROFILE_ASSERTION_PROJECTION_SQL,
        failpoint=failpoint,
        stage_prefix="projection_rebuild_schema",
    )
    columns_sql = ",".join(copied_columns)
    conn.execute(
        f"INSERT INTO profile_assertions({columns_sql}) "  # nosec B608
        f"SELECT {columns_sql} FROM {legacy_table}"  # nosec B608
    )
    if failpoint is not None:
        failpoint("projection_rebuild_copy_statement:1")
    conn.execute(f"DROP TABLE {legacy_table}")  # nosec B608
    if failpoint is not None:
        failpoint("projection_rebuild_drop_legacy_statement:1")


def _reconcile_once(
    conn: sqlite3.Connection,
    *,
    failpoint: Callable[[str], None] | None = None,
) -> int:
    _execute_script_in_transaction(
        conn,
        PROFILE_ASSERTION_SCHEMA_SQL,
        failpoint=failpoint,
        stage_prefix="assertion_schema",
    )
    if profile_assertion_projection_is_canonical(conn):
        _execute_script_in_transaction(
            conn,
            PROFILE_ASSERTION_PROJECTION_SQL,
            failpoint=failpoint,
            stage_prefix="projection_schema",
        )
    else:
        _rebuild_profile_assertion_projection(
            conn,
            failpoint=failpoint,
        )
    ensure_cognitive_profile_access_schema(
        conn,
        statement_callback=failpoint,
    )
    _execute_script_in_transaction(
        conn,
        PROFILE_SCHEMA_SQL,
        failpoint=failpoint,
        stage_prefix="profile_schema",
    )
    if failpoint is not None:
        failpoint("after_schema_install")
    rows = conn.execute("""
        SELECT assertion_id, dimension, claim, supporting_signals,
               contradicting_signals, confidence, privacy_level,
               last_verified_at, revision_policy, status, access_control
        FROM profile_assertions ORDER BY assertion_id
        """).fetchall()
    inserted = 0
    for row in rows:
        assertion_id = str(row["assertion_id"])
        content_hash = _content_hash(row)
        exists = conn.execute(
            "SELECT revision_id FROM profile_assertion_revisions "
            "WHERE assertion_id=? AND content_hash=?",
            (assertion_id, content_hash),
        ).fetchone()
        if exists is None:
            latest = conn.execute(
                "SELECT revision_id, revision_number FROM profile_assertion_revisions "
                "WHERE assertion_id=? ORDER BY revision_number DESC LIMIT 1",
                (assertion_id,),
            ).fetchone()
            number = int(latest["revision_number"] or 0) + 1 if latest else 1
            prior = str(latest["revision_id"]) if latest else None
            revision_id = f"par_{assertion_id}_{number}_{content_hash[:12]}"
            conn.execute(
                """
                INSERT INTO profile_assertion_revisions (
                    revision_id, assertion_id, revision_number, content_hash,
                    supersedes_revision_id, dimension, claim, supporting_signals,
                    contradicting_signals, confidence, privacy_level,
                    last_verified_at, revision_policy, status, access_control
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id,
                    assertion_id,
                    number,
                    content_hash,
                    prior,
                    row["dimension"],
                    row["claim"],
                    row["supporting_signals"],
                    row["contradicting_signals"],
                    row["confidence"],
                    row["privacy_level"],
                    row["last_verified_at"],
                    row["revision_policy"],
                    row["status"],
                    row["access_control"],
                ),
            )
            if failpoint is not None:
                failpoint(f"revision_insert:{assertion_id}")
            inserted += 1
        else:
            revision_id = str(exists["revision_id"])
        conn.execute(
            """
            INSERT INTO profile_assertion_heads(assertion_id, revision_id, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(assertion_id) DO UPDATE SET
                revision_id=excluded.revision_id,
                updated_at=CURRENT_TIMESTAMP
            WHERE revision_id IS NOT excluded.revision_id
            """,
            (assertion_id, revision_id),
        )
        if failpoint is not None:
            failpoint(f"head_upsert:{assertion_id}")
        conn.execute(
            "UPDATE profile_assertions SET current_revision_id=? "
            "WHERE assertion_id=? AND current_revision_id IS NOT ?",
            (revision_id, assertion_id, revision_id),
        )
        if failpoint is not None:
            failpoint(f"projection_bind:{assertion_id}")
    if failpoint is not None:
        failpoint("after_revision_anchor")
    register_profile_assertion_schema(conn)
    if failpoint is not None:
        failpoint("assertion_registry_statement:1")
    register_cognitive_profile_runtime_schema(conn)
    if failpoint is not None:
        failpoint("profile_runtime_registry_statement:1")
    validate_profile_assertion_schema(conn)
    validate_cognitive_profile_runtime_schema(conn)
    if failpoint is not None:
        failpoint("before_commit")
    return inserted


def apply(
    db_path: Path,
    backup_dir: Path,
    *,
    expected_plan_hash: str = "",
    daemon_check: Callable[[Path], bool] = runtime_writers_are_inactive,
    failpoint: Callable[[str], None] | None = None,
    backup_generation: str | None = None,
) -> dict[str, Any]:
    if not expected_plan_hash:
        raise ValueError("apply requires an exact expected plan hash")
    resolved = db_path.expanduser().resolve(strict=False)
    backup_root = backup_dir.expanduser().resolve(strict=False)
    with offline_migration_lock(resolved.parent, daemon_check=daemon_check):
        plan = inspect(resolved)
        if plan.get("errors"):
            return plan
        if plan.get("plan_hash") != expected_plan_hash:
            raise ValueError("expected plan hash does not match locked source state")
        if not plan.get("source_integrity_ok"):
            raise RuntimeError("source database integrity check failed")
        unexpected_fk_errors = [
            error
            for error in plan.get("source_foreign_key_errors") or ()
            if not (
                not plan.get("revision_table_exists") and "profile_assertion_revisions" in error
            )
        ]
        if unexpected_fk_errors:
            raise RuntimeError("source database foreign key check failed")

        backup_root.mkdir(parents=True, exist_ok=True)
        backup_path, generation = _reserve_unique_sqlite_path(
            backup_root,
            stem=f"{resolved.name}.before-profile-v2",
            generation=backup_generation,
        )
        _backup_database(resolved, backup_path)
        with sqlite3.connect(backup_path) as backup:
            backup_integrity_ok, backup_fk_errors = _integrity_state(backup)
            backup_logical_hash = _logical_database_hash(backup)
        if not backup_integrity_ok:
            raise RuntimeError("backup integrity or foreign key check failed")
        if backup_logical_hash != plan["source_logical_hash"]:
            raise RuntimeError("backup does not match reviewed source state")
        restore_drill_ok = _restore_drill(backup_path, backup_root)
        if not restore_drill_ok and not plan.get("source_foreign_key_errors"):
            raise RuntimeError("backup restore drill failed")

        with sqlite3.connect(resolved) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            try:
                inserted = _reconcile_once(conn, failpoint=failpoint)
                before_second_apply = conn.total_changes
                second_inserted = _reconcile_once(conn)
                second_apply_changed_rows = conn.total_changes - before_second_apply
                if second_inserted or second_apply_changed_rows:
                    raise RuntimeError("profile migration second apply was not a no-op")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

        result = inspect(resolved)
        result.update(
            {
                "read_only": False,
                "reviewed_plan_hash": expected_plan_hash,
                "backup_path": str(backup_path),
                "backup_generation": generation,
                "backup_integrity_ok": backup_integrity_ok,
                "backup_foreign_key_errors": backup_fk_errors,
                "restore_drill_ok": restore_drill_ok,
                "inserted_revision_count": inserted,
                "second_apply_changed_rows": second_apply_changed_rows,
            }
        )
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", default="")
    parser.add_argument("--expected-plan-hash", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.db_path:
        db_path = Path(args.db_path)
    else:
        from core.config import get_config

        db_path = Path(get_config().database_dir) / "user_signals.db"
    if args.apply:
        if not args.backup_dir:
            parser.error("--apply requires --backup-dir")
        if not args.expected_plan_hash:
            parser.error("--apply requires --expected-plan-hash")
        payload = apply(
            db_path,
            Path(args.backup_dir),
            expected_plan_hash=args.expected_plan_hash,
        )
    else:
        payload = inspect(db_path)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(payload)
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
