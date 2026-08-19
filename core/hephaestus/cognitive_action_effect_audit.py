"""Read-only independent audit for distill cognitive-action effects."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from core.hephaestus.cognitive_action_state_reconcile_contracts import (
    RECONCILIATION_BATCH_TABLE,
    RECONCILIATION_TABLE,
    reconciliation_schema_is_valid,
)
from core.hephaestus.cognitive_action_state_reconcile_planner import (
    build_reconciliation_candidate,
    validate_existing_reconciliation,
)
from core.hephaestus.cognitive_action_targets import (
    TARGET_RECEIPT_SCHEMA_VERSION,
    CognitiveActionTargetError,
    target_state_hash_for_contract,
    validated_cognitive_action_artifact,
)
from core.hephaestus.distill_action_store import (
    DistillActionSchemaError,
    SCHEMA_VERSION,
    schema_hash,
    validate_schema_structure,
)
from core.db_utils import render_sql


GAP_FIELDS = (
    "applied_without_effect",
    "effect_without_action",
    "effect_without_consumption",
    "consumption_without_effect",
    "command_parent_not_applied",
    "parent_applied_intent_without_command",
    "nonterminal_commands",
    "dead_commands",
    "invalid_artifacts",
    "invalid_effect_hashes",
    "missing_target_receipts",
    "target_receipt_mismatches",
    "target_state_missing",
    "target_state_hash_mismatches",
    "invalid_target_state_reconciliations",
    "orphan_target_state_reconciliations",
    "target_state_reconciliation_schema_gaps",
)


def audit_cognitive_action_effects(db_path: Path) -> dict[str, Any]:
    """Audit action/effect/receipt closure without creating or changing a DB."""
    path = Path(db_path).expanduser()
    report: dict[str, Any] = {
        "schema_version": "mnemos.cognitive_action_effect_audit.v2",
        "db_path": str(path),
        "db_exists": path.is_file(),
        "schema_state": "missing_database",
        "integrity_check": "missing",
        "counts": {
            "parent_actions": 0,
            "intents": 0,
            "commands": 0,
            "applied_commands": 0,
            "effects": 0,
            "consumptions": 0,
            "target_state_reconciliations": 0,
            "validated_target_state_reconciliations": 0,
        },
        "gaps": {field: 0 for field in GAP_FIELDS},
        "lineage_gap_count": 0,
        "findings": [],
        "ok": True,
    }
    if not path.is_file():
        return report
    try:
        with _connect_read_only(path) as conn:
            report["integrity_check"] = str(
                conn.execute("PRAGMA integrity_check").fetchone()[0]
            )
            tables = _tables(conn)
            state = _schema_state(conn, tables)
            report["schema_state"] = state
            if state == "legacy_v1":
                commands = _count(conn, "cognitive_action_log")
                applied = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM cognitive_action_log WHERE status='applied'"
                    ).fetchone()[0]
                )
                report["counts"].update(
                    {
                        "parent_actions": _count_if_exists(conn, tables, "distill_action_log"),
                        "commands": commands,
                        "applied_commands": applied,
                        "consumptions": _count_if_exists(
                            conn, tables, "cognitive_action_consumptions"
                        ),
                    }
                )
                report["gaps"]["applied_without_effect"] = applied
                report["findings"].append(
                    {
                        "code": "legacy_self_signed_action_schema",
                        "count": commands,
                        "repair_action": "run reconcile_cognitive_action_effects.py",
                    }
                )
                return _finalize(report)
            if state == "uninitialized":
                return report
            if state != "current_v2":
                report["findings"].append(
                    {
                        "code": "distill_action_schema_drift",
                        "count": 1,
                        "repair_action": "run explicit COG-014 reconciliation",
                    }
                )
                return _finalize(report)
            _audit_current(conn, report)
    except sqlite3.Error:
        report["schema_state"] = "read_error"
        report["findings"].append(
            {
                "code": "cognitive_action_audit_read_failed",
                "count": 1,
                "repair_action": "verify SQLite integrity and restore from backup",
            }
        )
    return _finalize(report)


def _audit_current(conn: sqlite3.Connection, report: dict[str, Any]) -> None:
    counts = report["counts"]
    gaps = report["gaps"]
    counts.update(
        {
            "parent_actions": _count(conn, "distill_action_log"),
            "intents": _count(conn, "cognitive_action_intents"),
            "commands": _count(conn, "cognitive_action_log"),
            "applied_commands": int(
                conn.execute(
                    "SELECT COUNT(*) FROM cognitive_action_log WHERE status='applied'"
                ).fetchone()[0]
            ),
            "effects": _count(conn, "cognitive_action_effects"),
            "consumptions": _count(conn, "cognitive_action_consumptions"),
        }
    )
    gaps["applied_without_effect"] = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM cognitive_action_log AS command
            LEFT JOIN cognitive_action_effects AS effect
              ON effect.cognitive_action_id=command.cognitive_action_id
            WHERE command.status='applied' AND effect.effect_id IS NULL
            """
        ).fetchone()[0]
    )
    gaps["effect_without_action"] = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM cognitive_action_effects AS effect
            LEFT JOIN cognitive_action_log AS command
              ON command.cognitive_action_id=effect.cognitive_action_id
            WHERE command.cognitive_action_id IS NULL OR command.status<>'applied'
            """
        ).fetchone()[0]
    )
    gaps["effect_without_consumption"] = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM cognitive_action_effects AS effect
            LEFT JOIN cognitive_action_consumptions AS consumption
              ON consumption.effect_id=effect.effect_id
            WHERE consumption.consumption_id IS NULL OR consumption.status<>'applied'
            """
        ).fetchone()[0]
    )
    gaps["consumption_without_effect"] = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM cognitive_action_consumptions AS consumption
            LEFT JOIN cognitive_action_effects AS effect
              ON effect.effect_id=consumption.effect_id
            WHERE effect.effect_id IS NULL
            """
        ).fetchone()[0]
    )
    gaps["command_parent_not_applied"] = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM cognitive_action_log AS command
            LEFT JOIN distill_action_log AS parent
              ON parent.action_id=command.distill_action_id
            WHERE parent.action_id IS NULL OR parent.result_status<>'applied'
            """
        ).fetchone()[0]
    )
    gaps["parent_applied_intent_without_command"] = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM cognitive_action_intents AS intent
            LEFT JOIN cognitive_action_log AS command
              ON command.cognitive_action_id=intent.cognitive_action_id
            WHERE intent.parent_status='applied'
              AND intent.disposition='command_created'
              AND command.cognitive_action_id IS NULL
            """
        ).fetchone()[0]
    )
    gaps["nonterminal_commands"] = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM cognitive_action_log
            WHERE status IN ('queued', 'processing', 'retry')
            """
        ).fetchone()[0]
    )
    gaps["dead_commands"] = int(
        conn.execute(
            "SELECT COUNT(*) FROM cognitive_action_log WHERE status='dead'"
        ).fetchone()[0]
    )

    commands = {
        str(row["cognitive_action_id"]): dict(row)
        for row in conn.execute("SELECT * FROM cognitive_action_log")
    }
    for row in commands.values():
        if not _artifact_valid(row):
            gaps["invalid_artifacts"] += 1

    effects = [dict(row) for row in conn.execute("SELECT * FROM cognitive_action_effects")]
    for effect in effects:
        command = commands.get(str(effect["cognitive_action_id"]))
        if not all(
            str(effect.get(field) or "")
            for field in (
                "effect_id",
                "cognitive_action_id",
                "target",
                "target_object_id",
                "before_hash",
                "after_hash",
                "expected_delta_hash",
                "reciprocal_receipt",
                "receipt_db_path",
                "committed_at",
            )
        ) or effect["before_hash"] == effect["after_hash"]:
            gaps["invalid_effect_hashes"] += 1
            continue
        receipt = _read_target_receipt(effect)
        if receipt is None:
            gaps["missing_target_receipts"] += 1
            continue
        if command is None or not _receipt_matches(effect, command, receipt):
            gaps["target_receipt_mismatches"] += 1
            continue
        state = _read_target_state(effect, receipt)
        if state is None:
            gaps["target_state_missing"] += 1
            continue
        try:
            contract = _target_state_contract(receipt)
            recorded_hash = target_state_hash_for_contract(
                str(effect["target"]),
                state,
                contract,
            )
        except (CognitiveActionTargetError, ValueError):
            gaps["target_state_hash_mismatches"] += 1
            continue
        if recorded_hash == str(effect["after_hash"]):
            if _target_reconciliation_exists(
                Path(str(effect["receipt_db_path"])),
                str(effect["effect_id"]),
            ):
                gaps["invalid_target_state_reconciliations"] += 1
            continue
        try:
            candidate = build_reconciliation_candidate(
                command=command,
                effect=effect,
                receipt=receipt,
                state=state,
                recorded_contract=contract,
            )
        except (CognitiveActionTargetError, ValueError):
            gaps["target_state_hash_mismatches"] += 1
            continue
        path = Path(str(effect["receipt_db_path"]))
        try:
            with _connect_read_only(path) as target_connection:
                reconciled = validate_existing_reconciliation(
                    target_connection,
                    candidate=candidate,
                )
        except sqlite3.Error:
            reconciled = False
        if reconciled:
            counts["validated_target_state_reconciliations"] += 1
        else:
            gaps["target_state_hash_mismatches"] += 1
            if _target_reconciliation_exists(path, str(effect["effect_id"])):
                gaps["invalid_target_state_reconciliations"] += 1
    _audit_reconciliation_denominator(effects, report)


def _artifact_valid(row: Mapping[str, Any]) -> bool:
    try:
        validated_cognitive_action_artifact(row)
    except CognitiveActionTargetError:
        return False
    return True


def _target_state_contract(receipt: Mapping[str, Any]) -> str:
    try:
        detail = json.loads(str(receipt.get("detail") or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError("target receipt detail is invalid JSON") from exc
    if not isinstance(detail, Mapping):
        raise ValueError("target receipt detail is not an object")
    return str(detail.get("target_state_hash_contract") or "")


def _target_reconciliation_exists(path: Path, effect_id: str) -> bool:
    try:
        with _connect_read_only(path) as connection:
            table = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type='table'
                  AND name='cognitive_action_target_state_reconciliations'
                """
            ).fetchone()
            if table is None:
                return False
            return bool(
                connection.execute(
                    """
                    SELECT 1 FROM cognitive_action_target_state_reconciliations
                    WHERE effect_id=?
                    """,
                    (effect_id,),
                ).fetchone()
            )
    except sqlite3.Error:
        return False


def _audit_reconciliation_denominator(
    effects: list[dict[str, Any]],
    report: dict[str, Any],
) -> None:
    expected_by_path: dict[Path, set[str]] = {}
    for effect in effects:
        path = Path(str(effect.get("receipt_db_path") or ""))
        if path.is_file():
            expected_by_path.setdefault(path.resolve(), set()).add(str(effect["effect_id"]))
    counts = report["counts"]
    gaps = report["gaps"]
    for path, expected_effect_ids in expected_by_path.items():
        try:
            with _connect_read_only(path) as connection:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                present = {
                    RECONCILIATION_BATCH_TABLE,
                    RECONCILIATION_TABLE,
                } & tables
                if not present:
                    continue
                if not reconciliation_schema_is_valid(connection):
                    gaps["target_state_reconciliation_schema_gaps"] += 1
                    continue
                rows = connection.execute(
                    f"SELECT effect_id FROM {RECONCILIATION_TABLE}"  # nosec B608
                ).fetchall()
                reconciled_effect_ids = {str(row[0]) for row in rows}
                counts["target_state_reconciliations"] += len(rows)
                gaps["orphan_target_state_reconciliations"] += len(
                    reconciled_effect_ids - expected_effect_ids
                )
                orphan_batches = int(
                    connection.execute(
                        f"""
                        SELECT COUNT(*)
                        FROM {RECONCILIATION_BATCH_TABLE} AS batch
                        LEFT JOIN {RECONCILIATION_TABLE} AS item
                          ON item.batch_id=batch.batch_id
                        WHERE item.reconciliation_id IS NULL
                        """  # nosec B608
                    ).fetchone()[0]
                )
                gaps["orphan_target_state_reconciliations"] += orphan_batches
        except sqlite3.Error:
            gaps["target_state_reconciliation_schema_gaps"] += 1


def _read_target_receipt(effect: Mapping[str, Any]) -> dict[str, Any] | None:
    path = Path(str(effect.get("receipt_db_path") or ""))
    if not path.is_file():
        return None
    try:
        with _connect_read_only(path) as conn:
            row = conn.execute(
                "SELECT * FROM cognitive_action_target_receipts WHERE effect_id=?",
                (str(effect["effect_id"]),),
            ).fetchone()
    except sqlite3.Error:
        return None
    return dict(row) if row else None


def _receipt_matches(
    effect: Mapping[str, Any],
    command: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> bool:
    expected = {
        "effect_id": effect["effect_id"],
        "cognitive_action_id": effect["cognitive_action_id"],
        "action": command["cognitive_action"],
        "target": effect["target"],
        "target_object_id": effect["target_object_id"],
        "before_hash": effect["before_hash"],
        "after_hash": effect["after_hash"],
        "expected_delta_hash": effect["expected_delta_hash"],
        "artifact_hash": command["artifact_hash"],
        "committed_at": effect["committed_at"],
        "schema_version": TARGET_RECEIPT_SCHEMA_VERSION,
    }
    if any(str(receipt.get(key) or "") != str(value) for key, value in expected.items()):
        return False
    path = Path(str(effect["receipt_db_path"]))
    expected_ref = f"{path.name}:cognitive_action_target_receipts:{effect['effect_id']}"
    return str(effect["reciprocal_receipt"]) == expected_ref


def _read_target_state(
    effect: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> dict[str, Any] | None:
    path = Path(str(effect["receipt_db_path"]))
    try:
        detail = json.loads(str(receipt.get("detail") or "{}"))
    except json.JSONDecodeError:
        return None
    locator = detail.get("target_locator") if isinstance(detail, Mapping) else None
    if not isinstance(locator, Mapping):
        return None
    try:
        with _connect_read_only(path) as conn:
            target = str(effect["target"])
            object_id = str(effect["target_object_id"])
            if target == "observation_store":
                row = conn.execute(
                    "SELECT * FROM observations WHERE id=?", (object_id,)
                ).fetchone()
                return dict(row) if row else None
            if target == "reflection_store":
                row = conn.execute(
                    "SELECT * FROM reflection_records WHERE id=?", (object_id,)
                ).fetchone()
                return dict(row) if row else None
            if target == "policy_patch_store":
                row = conn.execute(
                    "SELECT * FROM policy_patches WHERE patch_id=?", (object_id,)
                ).fetchone()
                return dict(row) if row else None
            if target == "knowledge_graph":
                row = conn.execute(
                    """
                    SELECT * FROM relations
                    WHERE source=? AND target=? AND relation_type=?
                    """,
                    (
                        str(locator.get("source") or ""),
                        str(locator.get("target") or ""),
                        str(locator.get("relation_type") or ""),
                    ),
                ).fetchone()
                if row is None:
                    return None
                evidence = conn.execute(
                    """
                    SELECT evidence_type, content FROM relation_evidence
                    WHERE relation_id=? ORDER BY id
                    """,
                    (int(row["id"]),),
                ).fetchall()
                payload = dict(row)
                payload["evidence"] = [dict(item) for item in evidence]
                return payload
    except sqlite3.Error:
        return None
    return None


def _schema_state(conn: sqlite3.Connection, tables: set[str]) -> str:
    if "distill_action_schema_registry" in tables:
        try:
            row = conn.execute(
                """
                SELECT schema_version, schema_hash FROM distill_action_schema_registry
                WHERE schema_name='distill_actions'
                """
            ).fetchone()
        except sqlite3.Error:
            return "registry_drift"
        if row and str(row[0]) == SCHEMA_VERSION and str(row[1]) == schema_hash():
            try:
                validate_schema_structure(conn)
            except DistillActionSchemaError:
                return "physical_schema_drift"
            return "current_v2"
        return "registry_drift"
    if "cognitive_action_log" not in tables:
        return "uninitialized"
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(cognitive_action_log)")
    }
    return "legacy_v1" if "artifact_hash" not in columns else "unknown"


def _finalize(report: dict[str, Any]) -> dict[str, Any]:
    gap_count = sum(int(value) for value in report["gaps"].values())
    report["lineage_gap_count"] = gap_count
    if report["integrity_check"] not in {"missing", "ok"}:
        report["findings"].append(
            {
                "code": "distill_action_integrity_failed",
                "count": 1,
                "repair_action": "restore the verified COG-014 backup",
            }
        )
    for code, count in report["gaps"].items():
        if count:
            report["findings"].append(
                {
                    "code": code,
                    "count": int(count),
                    "repair_action": "reconcile and replay through the real target service",
                }
            )
    report["ok"] = bool(
        report["schema_state"] in {"missing_database", "uninitialized", "current_v2"}
        and report["integrity_check"] in {"missing", "ok"}
        and gap_count == 0
        and not any(item["code"].endswith("drift") for item in report["findings"])
    )
    return report


def _connect_read_only(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        if str(row[0]) != "sqlite_sequence"
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


def _count_if_exists(conn: sqlite3.Connection, tables: set[str], table: str) -> int:
    return _count(conn, table) if table in tables else 0
