#!/usr/bin/env python3
"""Inspect or explicitly migrate the canonical cognitive state ledger."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
from typing import Any, Mapping
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cognitive.state_schema import reconcile_cognitive_state_schema  # noqa: E402
from core.cognitive.state_contract import canonical_json, sha256_json  # noqa: E402
from core.cognitive.state_store import CognitiveStateStore  # noqa: E402
from core.migrations.model_call_ledger_reconcile.runtime import (  # noqa: E402
    runtime_writers_are_inactive,
)
from core.ops.offline_migration_lock import offline_migration_lock  # noqa: E402
from core.ops.durable_io import (  # noqa: E402
    DurableIOError,
    fsync_directory,
    fsync_regular_file,
    inspect_path_kind,
    normalize_private_sqlite_copy,
    owned_sqlite_connection_pair,
    physical_scope_signature,
    private_sqlite_sidecars,
    regular_file_sha256,
    validate_private_sqlite_copy,
)
from core.ops.durable_io import read_native_bytes  # noqa: E402
from core.ops.readiness_query_budget import connect_readonly_sqlite  # noqa: E402
from core.ops.runtime_execution_identity import (  # noqa: E402
    runtime_execution_identity,
)


_RETIREMENT_RULES: dict[str, dict[str, str]] = {
    "database.calibration_provenance.v1": {
        "object_type": "calibration_record",
        "reason_code": "retired_legacy_system_identity_collision",
        "verified_count_field": "retired_collision_count",
    },
    "database.demo_fixture_leak.v1": {
        "object_type": "cognition_episode",
        "reason_code": "synthetic_fixture_source_not_in_canonical_raw",
        "verified_count_field": "retired_episode_count",
    },
}


def _sha256(path: Path) -> str:
    return "sha256:" + regular_file_sha256(path)


def _canonical_sqlite_path(path: Path) -> Path:
    candidate = Path(path).expanduser()
    try:
        return candidate.parent.resolve(strict=True) / candidate.name
    except OSError:
        raise RuntimeError("migration_database_parent_unavailable") from None


def _backup_database(source: Path, backup_dir: Path) -> dict[str, str]:
    _ensure_private_backup_dir(backup_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = backup_dir / f"producer-consumer-before-cognitive-state-{stamp}.db"
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        with owned_sqlite_connection_pair(
            lambda: connect_readonly_sqlite(source),
            lambda: sqlite3.connect(target),
        ) as (src, dst):
            src.backup(dst)
            integrity = str(dst.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                raise RuntimeError(f"backup integrity check failed: {integrity}")
        normalize_private_sqlite_copy(target)
        os.chmod(target, 0o600)
        fsync_regular_file(target)
        fsync_directory(backup_dir)
    except BaseException:
        for candidate in (*private_sqlite_sidecars(target), target):
            candidate.unlink(missing_ok=True)
        raise
    return {
        "path": str(target),
        "sha256": _sha256(target),
        "integrity_check": "ok",
    }


def _sqlite_logical_hash(path: Path, *, immutable: bool = False) -> str:
    digest = hashlib.sha256()
    with connect_readonly_sqlite(path, immutable=immutable) as conn:
        conn.execute("BEGIN")
        for statement in conn.iterdump():
            digest.update(statement.encode("utf-8"))
            digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _ensure_private_backup_dir(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary_created = False
    try:
        with open(temporary, "x", encoding="utf-8") as handle:
            temporary_created = True
            json.dump(dict(payload), handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if temporary_created:
            temporary.unlink(missing_ok=True)


def _receipt_path(backup_dir: Path, plan_hash: str) -> Path:
    suffix = plan_hash.removeprefix("sha256:")
    return backup_dir / f"cognitive-state-migration.{suffix}.json"


def _migration_physical_signature(
    db_path: Path,
    backup_dir: Path,
    receipt_path: Path,
) -> dict[str, object]:
    return physical_scope_signature(
        (
            db_path,
            db_path.with_name(f"{db_path.name}-wal"),
            db_path.with_name(f"{db_path.name}-shm"),
            receipt_path,
        ),
        inventory_directory=backup_dir,
    )


def _ensure_private_backup_dir(path: Path) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise RuntimeError("migration_backup_directory_unsafe")
    resolved = candidate.resolve(strict=False)
    try:
        resolved.mkdir(mode=0o700, parents=True, exist_ok=True)
        if resolved.is_symlink():
            raise RuntimeError("migration_backup_directory_unsafe")
        metadata = resolved.stat()
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            os.chmod(resolved, 0o700)
            metadata = resolved.stat()
    except OSError:
        raise RuntimeError("migration_backup_directory_unsafe") from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise RuntimeError("migration_backup_directory_unsafe")
    return resolved


def _private_backup_file_ok(path: Path, backup_dir: Path) -> bool:
    try:
        metadata = path.lstat()
        return bool(
            not path.is_symlink()
            and stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid == os.getuid()
            and stat.S_IMODE(metadata.st_mode) == 0o600
            and path.parent == backup_dir.resolve()
            and stat.S_IMODE(backup_dir.stat().st_mode) == 0o700
        )
    except OSError:
        return False


def _reviewed_plan_hash(plan: Mapping[str, Any]) -> str:
    material = dict(plan)
    material.pop("plan_hash", None)
    material.pop("integrity_check", None)
    return str(sha256_json(material))


def _verified_plan_backup(
    *,
    backup: Mapping[str, Any],
    backup_dir: Path,
    expected_logical_hash: str,
) -> Path:
    source = Path(str(backup.get("path") or "")).expanduser()
    if source.is_symlink():
        raise RuntimeError("migration_receipt_backup_invalid")
    try:
        resolved = source.resolve(strict=True)
    except OSError:
        raise RuntimeError("migration_receipt_backup_invalid") from None
    if (
        resolved.parent != backup_dir.resolve()
        or not resolved.name.startswith("producer-consumer-before-cognitive-state-")
        or not _private_backup_file_ok(resolved, backup_dir)
        or str(backup.get("sha256") or "") != _sha256(resolved)
        or str(backup.get("integrity_check") or "") != "ok"
        or _sqlite_logical_hash(resolved, immutable=True) != expected_logical_hash
    ):
        raise RuntimeError("migration_receipt_backup_invalid")
    try:
        validate_private_sqlite_copy(resolved)
    except DurableIOError:
        raise RuntimeError("migration_receipt_backup_invalid") from None
    return resolved


def _restore_backup(backup: Mapping[str, str], target: Path) -> None:
    source = Path(str(backup.get("path") or ""))
    if (
        not source.is_file()
        or str(backup.get("sha256") or "") != _sha256(source)
        or str(backup.get("integrity_check") or "") != "ok"
    ):
        raise RuntimeError("migration_backup_invalid")
    try:
        validate_private_sqlite_copy(source)
    except DurableIOError:
        raise RuntimeError("migration_backup_invalid") from None
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.restore")
    temporary_created = False
    try:
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        temporary_created = True
        os.close(descriptor)
        with owned_sqlite_connection_pair(
            lambda: connect_readonly_sqlite(source, immutable=True),
            lambda: sqlite3.connect(temporary),
        ) as (src, dst):
            src.backup(dst)
            if str(dst.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
                raise RuntimeError("migration_backup_invalid")
        normalize_private_sqlite_copy(temporary)
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        for sidecar in private_sqlite_sidecars(target):
            sidecar.unlink(missing_ok=True)
        fsync_regular_file(target)
        fsync_directory(target.parent)
    finally:
        if temporary_created:
            for candidate in (*private_sqlite_sidecars(temporary), temporary):
                candidate.unlink(missing_ok=True)


def _json_object(raw: object, *, field: str) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field}_invalid") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{field}_invalid")
    return value


def _json_list(raw: object, *, field: str) -> list[Any]:
    try:
        value = json.loads(str(raw or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field}_invalid") from exc
    if not isinstance(value, list):
        raise ValueError(f"{field}_invalid")
    return value


def _verified_retirement_proof(
    *,
    db_path: Path,
    evidence_path: Path,
) -> dict[str, Any]:
    """Bind a historical head preimage to its verified retirement migration."""

    resolved_evidence = _canonical_sqlite_path(evidence_path)
    evidence_sha = _sha256(resolved_evidence)
    migrations_path = db_path.parent / "migrations.db"
    if not migrations_path.is_file():
        raise ValueError("retirement_migration_ledger_missing")
    with connect_readonly_sqlite(migrations_path) as conn:
        conn.row_factory = sqlite3.Row
        applying_rows = conn.execute(
            """
            SELECT ledger_id, migration_id, plan_hash, backup_ref
            FROM migration_ledger
            WHERE status='applying'
            ORDER BY created_at, ledger_id
            """
        ).fetchall()
        matches: list[dict[str, Any]] = []
        for applying in applying_rows:
            migration_id = str(applying["migration_id"])
            rule = _RETIREMENT_RULES.get(migration_id)
            if rule is None:
                continue
            receipts = _json_list(
                applying["backup_ref"],
                field="retirement_backup_ref",
            )
            matching_receipt = None
            for raw_receipt in receipts:
                if not isinstance(raw_receipt, Mapping):
                    continue
                receipt_path = Path(str(raw_receipt.get("path") or "")).expanduser()
                try:
                    same_path = receipt_path.resolve(strict=True) == resolved_evidence
                except OSError:
                    same_path = False
                if (
                    same_path
                    and str(raw_receipt.get("sha256") or "") == evidence_sha
                    and str(raw_receipt.get("integrity_check") or "") == "ok"
                    and Path(str(raw_receipt.get("source") or "")).expanduser().resolve(
                        strict=False
                    )
                    == db_path.expanduser().resolve(strict=True)
                ):
                    matching_receipt = dict(raw_receipt)
                    break
            if matching_receipt is None:
                continue
            verified_rows = conn.execute(
                """
                SELECT ledger_id, verification_json
                FROM migration_ledger
                WHERE migration_id=? AND plan_hash=? AND status='verified'
                ORDER BY created_at DESC, ledger_id DESC
                """,
                (migration_id, str(applying["plan_hash"])),
            ).fetchall()
            for verified in verified_rows:
                verification = _json_object(
                    verified["verification_json"],
                    field="retirement_verification",
                )
                retired_count = verification.get(rule["verified_count_field"])
                if (
                    isinstance(retired_count, int)
                    and not isinstance(retired_count, bool)
                    and retired_count > 0
                ):
                    matches.append(
                        {
                            "migration_id": migration_id,
                            "plan_hash": str(applying["plan_hash"]),
                            "applying_ledger_id": str(applying["ledger_id"]),
                            "verified_ledger_id": str(verified["ledger_id"]),
                            "retired_count": retired_count,
                            "evidence_sha256": evidence_sha,
                            **rule,
                        }
                    )
                    break
    if len(matches) != 1:
        raise ValueError("retirement_evidence_not_uniquely_verified")
    return matches[0]


def _retirement_candidates(
    conn: sqlite3.Connection,
    *,
    db_path: Path,
    evidence_path: Path,
    proof: Mapping[str, Any],
) -> dict[str, Any]:
    """Find only exact immutable revisions retired by the proved migration."""

    object_type = str(proof["object_type"])
    conn.row_factory = sqlite3.Row
    current_heads = {
        str(row["object_id"]): str(row["revision_id"])
        for row in conn.execute(
            "SELECT object_id, revision_id FROM cognitive_state_heads WHERE object_type=?",
            (object_type,),
        ).fetchall()
    }
    quarantine_keys = {
        str(row[0])
        for row in conn.execute(
            """
            SELECT source_key FROM cognitive_state_migration_quarantine
            WHERE source_table='cognitive_state_revisions'
            """
        ).fetchall()
    }
    with connect_readonly_sqlite(evidence_path) as evidence:
        evidence.row_factory = sqlite3.Row
        if str(evidence.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
            raise ValueError("retirement_evidence_integrity_failed")
        evidence_rows = evidence.execute(
            """
            SELECT h.object_id, h.revision_id
            FROM cognitive_state_heads AS h
            WHERE h.object_type=?
            ORDER BY h.object_id, h.revision_id
            """,
            (object_type,),
        ).fetchall()

    observation_ids: set[str] = set()
    observations_path = db_path.parent / "observations.db"
    if object_type == "calibration_record" and observations_path.is_file():
        with connect_readonly_sqlite(observations_path) as observations:
            observation_ids = {
                str(row[0])
                for row in observations.execute("SELECT id FROM observations").fetchall()
            }

    candidates: list[dict[str, str]] = []
    blocked: list[dict[str, str]] = []
    for evidence_row in evidence_rows:
        object_id = str(evidence_row["object_id"])
        revision_id = str(evidence_row["revision_id"])
        if object_id in current_heads or revision_id in quarantine_keys:
            continue
        current = conn.execute(
            "SELECT * FROM cognitive_state_revisions WHERE revision_id=?",
            (revision_id,),
        ).fetchone()
        if current is None:
            continue
        with connect_readonly_sqlite(evidence_path) as evidence:
            evidence.row_factory = sqlite3.Row
            evidence_row = evidence.execute(
                "SELECT * FROM cognitive_state_revisions WHERE revision_id=?",
                (revision_id,),
            ).fetchone()
        if evidence_row is None:
            blocked.append(
                {
                    "revision_id": revision_id,
                    "reason": "evidence_revision_missing",
                }
            )
            continue
        if dict(current) != dict(evidence_row):
            blocked.append(
                {
                    "revision_id": revision_id,
                    "reason": "immutable_revision_drift",
                }
            )
            continue
        if str(current["admission_state"]) != "active":
            continue
        if object_type == "calibration_record" and object_id in observation_ids:
            blocked.append(
                {
                    "revision_id": revision_id,
                    "reason": "calibration_observation_still_present",
                }
            )
            continue
        candidates.append(
            {
                "object_type": object_type,
                "object_id": object_id,
                "revision_id": revision_id,
                "payload_hash": str(current["payload_hash"]),
            }
        )
    if len(candidates) > int(proof["retired_count"]):
        blocked.append(
            {
                "revision_id": "",
                "reason": "candidate_count_exceeds_verified_retirement_count",
            }
        )
    return {
        "ok": not blocked,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "blocked": blocked,
        "inserted_count": 0,
        "proof": dict(proof),
    }


def _insert_retirement_sidecars(
    conn: sqlite3.Connection,
    report: dict[str, Any],
) -> int:
    if not report.get("ok"):
        raise ValueError("retirement_reconciliation_blocked")
    proof = report["proof"]
    inserted = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for candidate in report["candidates"]:
            payload = {
                "schema_version": "mnemos.cognitive_state_retirement_reconciliation.v1",
                **candidate,
                "reason_code": str(proof["reason_code"]),
                "migration_id": str(proof["migration_id"]),
                "migration_plan_hash": str(proof["plan_hash"]),
                "migration_applying_ledger_id": str(proof["applying_ledger_id"]),
                "migration_verified_ledger_id": str(proof["verified_ledger_id"]),
                "retirement_evidence_sha256": str(proof["evidence_sha256"]),
            }
            payload_hash = sha256_json(payload)
            quarantine_id = "cogquarantine-" + payload_hash.split(":", 1)[1][:32]
            conn.execute(
                """
                INSERT INTO cognitive_state_migration_quarantine (
                    quarantine_id, source_table, source_key, reason_code,
                    field_manifest, payload_json, payload_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    quarantine_id,
                    "cognitive_state_revisions",
                    str(candidate["revision_id"]),
                    str(proof["reason_code"]),
                    canonical_json(sorted(payload)),
                    canonical_json(payload),
                    payload_hash,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            inserted += 1
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return inserted


def _preview_plan(
    *,
    db_path: Path,
    backup_dir: Path | None,
    retirement_evidence_db: Path | None,
    writers_inactive: bool,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any]]:
    with connect_readonly_sqlite(db_path) as conn:
        report = reconcile_cognitive_state_schema(conn, apply=False)
        retirement_report = None
        if retirement_evidence_db is not None:
            proof = _verified_retirement_proof(
                db_path=db_path,
                evidence_path=retirement_evidence_db,
            )
            retirement_report = _retirement_candidates(
                conn,
                db_path=db_path,
                evidence_path=retirement_evidence_db,
                proof=proof,
            )
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    dependency_hashes = _cognitive_migration_dependency_hashes()
    retirement_binding = None
    if retirement_evidence_db is not None:
        resolved = _canonical_sqlite_path(retirement_evidence_db)
        retirement_binding = {
            "path": str(resolved),
            "sha256": _sha256(resolved),
        }
    plan_material = {
        "schema_version": "mnemos.cognitive_state_migration_plan.v1",
        "db_path": str(_canonical_sqlite_path(db_path)),
        "database_logical_hash": _sqlite_logical_hash(db_path),
        "backup_dir": (
            str(backup_dir.expanduser().resolve(strict=False))
            if backup_dir is not None
            else ""
        ),
        "schema_action": str(report.get("action") or ""),
        "schema_before": report.get("before"),
        "retirement_evidence": retirement_binding,
        "retirement_candidate_count": int(
            (retirement_report or {}).get("candidate_count") or 0
        ),
        "retirement_candidate_revision_hashes": sorted(
            str(candidate.get("payload_hash") or "")
            for candidate in (retirement_report or {}).get("candidates", [])
        ),
        "dependency_hashes": dependency_hashes,
        "runtime_execution_identity": runtime_execution_identity(),
        "writer_lock_state": (
            "writers_inactive" if writers_inactive else "active_or_unverified"
        ),
        "apply_eligible": bool(writers_inactive),
    }
    return (
        report,
        retirement_report,
        {
            **plan_material,
            "integrity_check": integrity,
            "plan_hash": sha256_json(plan_material),
        },
    )


def _cognitive_migration_dependency_hashes() -> dict[str, str]:
    dependency_paths = (
        Path(__file__).resolve(),
        *sorted((ROOT / "core").rglob("*.py")),
    )
    return {
        str(path.relative_to(ROOT)): _sha256(path)
        for path in dependency_paths
    }


def _recover_or_verify_receipt(
    *,
    receipt_path: Path,
    db_path: Path,
    backup_dir: Path,
    expected_plan_hash: str,
) -> dict[str, Any] | None:
    try:
        receipt_kind = inspect_path_kind(receipt_path)
    except DurableIOError:
        raise RuntimeError("migration_receipt_unreadable") from None
    if receipt_kind == "missing":
        return None
    if receipt_kind != "file":
        raise RuntimeError("migration_receipt_permissions_invalid")
    if not _private_backup_file_ok(receipt_path, backup_dir):
        raise RuntimeError("migration_receipt_permissions_invalid")
    physical_before = _migration_physical_signature(
        db_path,
        backup_dir,
        receipt_path,
    )
    try:
        receipt = json.loads(read_native_bytes(receipt_path).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise RuntimeError("migration_receipt_unreadable") from None
    if (
        receipt.get("schema_version") != "mnemos.cognitive_state_migration_receipt.v1"
        or receipt.get("plan_hash") != expected_plan_hash
        or receipt.get("db_path") != str(_canonical_sqlite_path(db_path))
        or receipt.get("backup_dir") != str(backup_dir.resolve())
    ):
        raise RuntimeError("migration_receipt_binding_mismatch")
    status = str(receipt.get("status") or "")
    reviewed_plan = receipt.get("reviewed_plan")
    if (
        not isinstance(reviewed_plan, Mapping)
        or _reviewed_plan_hash(reviewed_plan) != expected_plan_hash
        or reviewed_plan.get("plan_hash") != expected_plan_hash
        or reviewed_plan.get("db_path") != str(_canonical_sqlite_path(db_path))
        or reviewed_plan.get("backup_dir") != str(backup_dir.resolve())
        or reviewed_plan.get("database_logical_hash")
        != receipt.get("before_logical_hash")
    ):
        raise RuntimeError("migration_receipt_binding_mismatch")
    if reviewed_plan.get("dependency_hashes") != (
        _cognitive_migration_dependency_hashes()
    ):
        raise RuntimeError("migration_receipt_code_drift")
    if (
        reviewed_plan.get("runtime_execution_identity")
        != runtime_execution_identity()
    ):
        raise RuntimeError("migration_receipt_runtime_drift")
    if status == "prepared":
        backup = receipt.get("backup")
        if not isinstance(backup, Mapping):
            raise RuntimeError("migration_receipt_backup_invalid")
        _verified_plan_backup(
            backup=backup,
            backup_dir=backup_dir,
            expected_logical_hash=str(reviewed_plan["database_logical_hash"]),
        )
        _restore_backup(backup, db_path)
        if _sqlite_logical_hash(db_path) != receipt.get("before_logical_hash"):
            raise RuntimeError("migration_rollback_state_mismatch")
        _atomic_write_json(
            receipt_path,
            {
                **receipt,
                "status": "recovered_rollback",
                "rollback_ok": True,
                "recovered_after_process_interruption": True,
            },
        )
        return None
    if status == "recovered_rollback":
        return None
    if status != "completed":
        raise RuntimeError("migration_receipt_binding_mismatch")
    if _sqlite_logical_hash(db_path) != receipt.get("after_logical_hash"):
        raise RuntimeError("migration_receipt_post_state_drift")
    backup = receipt.get("backup")
    if backup is not None:
        if not isinstance(backup, Mapping):
            raise RuntimeError("migration_receipt_backup_invalid")
        _verified_plan_backup(
            backup=backup,
            backup_dir=backup_dir,
            expected_logical_hash=str(reviewed_plan["database_logical_hash"]),
        )
    with connect_readonly_sqlite(db_path) as conn:
        current = reconcile_cognitive_state_schema(conn, apply=False)
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    if not current["before"].get("ok") or integrity != "ok":
        raise RuntimeError("migration_receipt_post_state_drift")
    physical_after = _migration_physical_signature(
        db_path,
        backup_dir,
        receipt_path,
    )
    if physical_after != physical_before:
        raise RuntimeError("migration_second_apply_physical_drift")
    return {
        "ok": True,
        "db_path": str(db_path),
        "backup": backup,
        "integrity_check": integrity,
        **current,
        "applied": False,
        "action": "same_plan_second_apply",
        "reviewed_plan_hash": expected_plan_hash,
        "physical_delta": 0,
        "physical_pre_signature": physical_before,
        "physical_post_signature": physical_after,
        "semantic_delta": 0,
        "receipt_path": str(receipt_path),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--expected-plan-hash", default="")
    parser.add_argument("--retirement-evidence-db", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.db_path is None:
        from core.config import get_config

        db_path = Path(get_config().database_dir) / "producer_consumer_ledger.db"
    else:
        db_path = args.db_path
    if not db_path.is_file():
        payload = {"ok": False, "error": "cognitive state database is not initialized"}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2
    if args.apply and args.backup_dir is None:
        print(
            json.dumps(
                {"ok": False, "error": "--apply requires --backup-dir"},
                ensure_ascii=False,
            )
        )
        return 2
    writers_inactive = bool(runtime_writers_are_inactive(db_path.parent))
    if args.apply and not writers_inactive:
        print(json.dumps({"ok": False, "error": "daemon_not_inactive"}))
        return 2
    if args.apply and not args.expected_plan_hash:
        print(json.dumps({"ok": False, "error": "expected_plan_hash_required"}))
        return 2
    if args.apply and not re.fullmatch(
        r"sha256:[0-9a-f]{64}",
        args.expected_plan_hash,
    ):
        print(json.dumps({"ok": False, "error": "expected_plan_hash_invalid"}))
        return 2

    backup: dict[str, str] | None = None
    try:
        report, retirement_report, plan = _preview_plan(
            db_path=db_path,
            backup_dir=args.backup_dir,
            retirement_evidence_db=args.retirement_evidence_db,
            writers_inactive=writers_inactive,
        )
        if not args.apply:
            before = report["before"]
            payload = {
                "ok": bool(
                    plan["integrity_check"] == "ok"
                    and (
                        before.get("ok")
                        or before.get("classification")
                        in {
                            "legacy_runtime_v1_or_v2",
                            "canonical_v2_feedback_attribution_upgrade_required",
                            "canonical_v3_training_governance_upgrade_required",
                            "canonical_v4_stage_receipt_upgrade_required",
                            "absent",
                        }
                    )
                    and (
                        retirement_report is None
                        or retirement_report.get("ok")
                    )
                ),
                "db_path": str(db_path),
                "backup": None,
                "integrity_check": plan["integrity_check"],
                "retirement_reconciliation": retirement_report,
                "state_integrity": None,
                **report,
                "applied": False,
                "plan": plan,
                "plan_hash": plan["plan_hash"],
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0 if payload.get("ok") else 1

        resolved_backup_dir = args.backup_dir.expanduser().resolve(strict=False)
        with offline_migration_lock(
            db_path.parent,
            daemon_check=lambda _database_dir: runtime_writers_are_inactive(
                db_path.parent
            ),
        ):
            _ensure_private_backup_dir(resolved_backup_dir)
            receipt_path = _receipt_path(
                resolved_backup_dir,
                args.expected_plan_hash,
            )
            repeated = _recover_or_verify_receipt(
                receipt_path=receipt_path,
                db_path=db_path,
                backup_dir=resolved_backup_dir,
                expected_plan_hash=args.expected_plan_hash,
            )
            if repeated is not None:
                print(json.dumps(repeated, ensure_ascii=False, indent=2))
                return 0
            report, retirement_report, plan = _preview_plan(
                db_path=db_path,
                backup_dir=resolved_backup_dir,
                retirement_evidence_db=args.retirement_evidence_db,
                writers_inactive=True,
            )
            if plan["plan_hash"] != args.expected_plan_hash:
                raise RuntimeError("expected_plan_hash_mismatch")
            retirement_candidates = int(
                (retirement_report or {}).get("candidate_count") or 0
            )
            write_required = (
                str(report.get("action") or "") != "already_canonical"
                or retirement_candidates > 0
            )
            before_logical_hash = str(plan["database_logical_hash"])
            if not write_required:
                _atomic_write_json(
                    receipt_path,
                    {
                        "schema_version": "mnemos.cognitive_state_migration_receipt.v1",
                        "status": "completed",
                        "reviewed_plan": plan,
                        "plan_hash": args.expected_plan_hash,
                        "db_path": str(_canonical_sqlite_path(db_path)),
                        "backup_dir": str(resolved_backup_dir),
                        "backup": None,
                        "before_logical_hash": before_logical_hash,
                        "after_logical_hash": before_logical_hash,
                        "physical_delta": 0,
                        "semantic_delta": 0,
                    },
                )
                payload = {
                    "ok": True,
                    "db_path": str(db_path),
                    "backup": None,
                    "integrity_check": plan["integrity_check"],
                    "retirement_reconciliation": retirement_report,
                    "state_integrity": None,
                    **report,
                    "applied": False,
                    "action": "already_canonical",
                    "reviewed_plan_hash": args.expected_plan_hash,
                    "physical_delta": 0,
                    "semantic_delta": 0,
                    "receipt_path": str(receipt_path),
                }
                print(json.dumps(payload, ensure_ascii=False, indent=2))
                return 0

            backup = _backup_database(db_path, resolved_backup_dir)
            _atomic_write_json(
                receipt_path,
                {
                    "schema_version": "mnemos.cognitive_state_migration_receipt.v1",
                    "status": "prepared",
                    "reviewed_plan": plan,
                    "plan_hash": args.expected_plan_hash,
                    "db_path": str(_canonical_sqlite_path(db_path)),
                    "backup_dir": str(resolved_backup_dir),
                    "backup": backup,
                    "before_logical_hash": before_logical_hash,
                },
            )
            try:
                with sqlite3.connect(db_path) as conn:
                    report = reconcile_cognitive_state_schema(conn, apply=True)
                    if args.retirement_evidence_db is not None:
                        proof = _verified_retirement_proof(
                            db_path=db_path,
                            evidence_path=args.retirement_evidence_db,
                        )
                        retirement_report = _retirement_candidates(
                            conn,
                            db_path=db_path,
                            evidence_path=args.retirement_evidence_db,
                            proof=proof,
                        )
                        conn.commit()
                        retirement_report["inserted_count"] = (
                            _insert_retirement_sidecars(
                                conn,
                                retirement_report,
                            )
                        )
                    integrity = str(
                        conn.execute("PRAGMA integrity_check").fetchone()[0]
                    )
                state_integrity = (
                    CognitiveStateStore(db_path).integrity_report()
                    if args.retirement_evidence_db is not None
                    else None
                )
                if (
                    integrity != "ok"
                    or not report["after"].get("ok")
                    or (
                        state_integrity is not None
                        and state_integrity.get("current_state_hash_mismatch") != 0
                    )
                ):
                    raise RuntimeError("migration_postcondition_failed")
                after_logical_hash = _sqlite_logical_hash(db_path)
                retirement_inserted = int(
                    (retirement_report or {}).get("inserted_count") or 0
                )
                resolved_action = (
                    "retirement_sidecars_inserted"
                    if retirement_inserted
                    else str(report.get("action") or "")
                )
                _atomic_write_json(
                    receipt_path,
                    {
                        "schema_version": "mnemos.cognitive_state_migration_receipt.v1",
                        "status": "completed",
                        "reviewed_plan": plan,
                        "plan_hash": args.expected_plan_hash,
                        "db_path": str(_canonical_sqlite_path(db_path)),
                        "backup_dir": str(resolved_backup_dir),
                        "backup": backup,
                        "before_logical_hash": before_logical_hash,
                        "after_logical_hash": after_logical_hash,
                        "physical_delta": 1,
                        "semantic_delta": 1,
                        "integrity_check": integrity,
                    },
                )
            except BaseException:
                _restore_backup(backup, db_path)
                _atomic_write_json(
                    receipt_path,
                    {
                        "schema_version": "mnemos.cognitive_state_migration_receipt.v1",
                        "status": "recovered_rollback",
                        "reviewed_plan": plan,
                        "plan_hash": args.expected_plan_hash,
                        "db_path": str(_canonical_sqlite_path(db_path)),
                        "backup_dir": str(resolved_backup_dir),
                        "backup": backup,
                        "before_logical_hash": before_logical_hash,
                        "after_logical_hash": before_logical_hash,
                        "rollback_ok": True,
                    },
                )
                raise
        payload = {
            "ok": True,
            "db_path": str(db_path),
            "backup": backup,
            "integrity_check": integrity,
            "retirement_reconciliation": retirement_report,
            "state_integrity": state_integrity,
            **report,
            "applied": True,
            "action": resolved_action,
            "reviewed_plan_hash": args.expected_plan_hash,
            "physical_delta": 1,
            "semantic_delta": 1,
            "receipt_path": str(receipt_path),
        }
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        payload = {
            "ok": False,
            "db_path": str(db_path),
            "backup": backup,
            "error": str(exc),
        }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
