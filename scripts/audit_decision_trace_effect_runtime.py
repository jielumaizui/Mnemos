"""Runtime and persisted-state evidence checks for the COG-036 audit."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping, TypeGuard

from core.cognitive.access_control import cognitive_access_hash
from core.cognitive.decision_snapshot_access import (
    DECISION_SNAPSHOT_OUTPUT_PURPOSE,
    DECISION_SNAPSHOT_SOURCE_PURPOSE_CONTRACT_HASH,
    DECISION_SNAPSHOT_SOURCE_PURPOSE_SCHEMA_VERSION,
    DECISION_SNAPSHOT_SOURCE_PURPOSES,
)
from core.cognitive.decision_trace_migration import (
    HistoricalObject,
    SourceDomain,
    build_decision_trace_inventory,
    default_source_domains,
)
from core.cognitive.material_effect_schema import (
    ROW_SCHEMA_VERSION as TARGET_EFFECT_LEDGER_SCHEMA,
    inspect_material_effect_schema,
)
from core.cognitive.state_contract import (
    COGNITIVE_OBJECT_SCHEMA_VERSIONS,
    FIXED_VALUE_PRECEDENCE,
    VALUE_PRECEDENCE_CONTRACT,
    sha256_json,
)
from core.cognitive.state_schema import (
    DECISION_TRACE_ENFORCEMENT_COMPONENT,
    DECISION_TRACE_ENFORCEMENT_HASH,
    DECISION_TRACE_ENFORCEMENT_VERSION,
    REGISTRY_TABLE,
    STATE_SCHEMA_VERSION,
)
from core.db_utils import render_sql
from core.trust.models import sha256_json as trust_sha256_json
from scripts.audit_decision_trace_effect_contracts import (
    PROHIBITED_REASONING_FIELDS,
    TARGET_EFFECT_LEDGER_FAMILIES,
    ZERO_METRICS,
)


def _audit_external_domains(
    *,
    database_dir: Path,
    state_db: Path,
    source_domains: Iterable[SourceDomain] | None = None,
) -> dict[str, Any]:
    domains = tuple(source_domains or default_source_domains(database_dir=database_dir))
    existing = [domain for domain in domains if domain.path.is_file()]
    if not existing:
        return {
            "status": "not_initialized",
            "total_objects": 0,
            "historical_quarantine_count": 0,
            "runtime_provenance_count": 0,
            "uncovered_count": 0,
            "uncovered_samples": [],
            "failures": [],
        }
    if len(existing) != len(domains):
        missing = [str(domain.path) for domain in domains if not domain.path.is_file()]
        return {
            "status": "partial",
            "total_objects": 0,
            "historical_quarantine_count": 0,
            "runtime_provenance_count": 0,
            "uncovered_count": 1,
            "uncovered_samples": missing[:20],
            "failures": ["external material-action denominator is partial: " + ",".join(missing)],
        }
    try:
        inventory = build_decision_trace_inventory(domains)
    except (FileNotFoundError, RuntimeError, sqlite3.Error) as exc:
        return {
            "status": "invalid",
            "total_objects": 0,
            "historical_quarantine_count": 0,
            "runtime_provenance_count": 0,
            "uncovered_count": 1,
            "uncovered_samples": [],
            "failures": [f"external material-action inventory failed: {exc}"],
        }
    if not state_db.is_file():
        total = len(inventory.objects)
        return {
            "status": "state_store_missing",
            "total_objects": total,
            "historical_quarantine_count": 0,
            "runtime_provenance_count": 0,
            "uncovered_count": total,
            "uncovered_samples": [
                f"{row.domain}:{row.source_primary_key_value}" for row in inventory.objects[:20]
            ],
            "failures": [f"{total} external material objects lack a canonical state store"],
        }

    failures: list[str] = []
    historical_count = 0
    runtime_count = 0
    uncovered: list[str] = []
    with sqlite3.connect(
        f"file:{state_db}?mode=ro",
        uri=True,
        timeout=30,
    ) as conn:
        conn.row_factory = sqlite3.Row
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        required_tables = {
            "cognitive_state_migration_quarantine",
            "cognitive_state_revisions",
            "cognitive_state_outbox",
            "cognitive_state_effect_receipts",
        }
        if not required_tables.issubset(tables):
            missing = sorted(required_tables - tables)
            return {
                "status": "state_store_legacy",
                "total_objects": len(inventory.objects),
                "historical_quarantine_count": 0,
                "runtime_provenance_count": 0,
                "uncovered_count": len(inventory.objects),
                "uncovered_samples": missing,
                "failures": [
                    "external denominator cannot resolve canonical tables: " + ",".join(missing)
                ],
            }
        quarantine = {
            (str(row["source_table"]), str(row["source_key"])): dict(row)
            for row in conn.execute("""
                SELECT source_table, source_key, reason_code, payload_json,
                       payload_hash
                FROM cognitive_state_migration_quarantine
                WHERE reason_code='historical_incomplete'
                """).fetchall()
        }
        for historical in inventory.objects:
            runtime_status = _runtime_object_status(conn, historical)
            if runtime_status == "verified_runtime":
                runtime_count += 1
                continue
            quarantine_row = quarantine.get(
                (
                    f"{historical.domain}.{historical.source_table}",
                    historical.source_primary_key_value,
                )
            )
            if _exact_quarantine_match(historical, quarantine_row):
                historical_count += 1
                continue
            if len(uncovered) < 20:
                uncovered.append(
                    f"{historical.domain}:"
                    f"{historical.source_primary_key_value}:"
                    f"{runtime_status}"
                )
    uncovered_count = len(inventory.objects) - historical_count - runtime_count
    if uncovered_count:
        failures.append(
            f"external material objects without exact decision provenance: {uncovered_count}"
        )
    return {
        "status": "available",
        "inventory_hash": inventory.inventory_hash,
        "object_manifest_hash": inventory.object_manifest_hash,
        "total_objects": len(inventory.objects),
        "historical_quarantine_count": historical_count,
        "runtime_provenance_count": runtime_count,
        "uncovered_count": uncovered_count,
        "uncovered_samples": uncovered,
        "failures": failures,
    }


def _exact_quarantine_match(
    historical: HistoricalObject,
    row: Mapping[str, Any] | None,
) -> bool:
    if row is None or str(row.get("reason_code") or "") != "historical_incomplete":
        return False
    try:
        payload = json.loads(str(row["payload_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(payload, Mapping):
        return False
    link_status = str(payload.get("canonical_link_status") or "")
    if link_status not in {
        "not_declared",
        "declared_unresolvable",
        "verified_existing",
    }:
        return False
    expected = historical.quarantine_payload(canonical_link_status=link_status)
    return dict(payload) == expected and str(row.get("payload_hash") or "") == str(
        sha256_json(expected)
    )


def _runtime_object_status(
    conn: sqlite3.Connection,
    historical: HistoricalObject,
) -> str:
    material = historical.runtime_material_action
    if not material:
        return "runtime_provenance_missing"
    command = conn.execute(
        """
        SELECT command_id, revision_id, event_id, consumer_id, command_type,
               payload_json, payload_hash, created_at
        FROM cognitive_state_outbox WHERE command_id=?
        """,
        (material["command_id"],),
    ).fetchone()
    if command is None or str(command["command_type"]) != "execute_material_action":
        return "runtime_command_unresolvable"
    decision = conn.execute(
        """
        SELECT payload_json, payload_hash, created_at
        FROM cognitive_state_revisions
        WHERE revision_id=? AND object_type='decision_trace'
        """,
        (material["decision_revision_id"],),
    ).fetchone()
    if decision is None:
        return "runtime_decision_unresolvable"
    try:
        command_payload = json.loads(str(command["payload_json"]))
        decision_payload = json.loads(str(decision["payload_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return "runtime_payload_invalid"
    if (
        sha256_json(command_payload) != str(command["payload_hash"])
        or sha256_json(decision_payload) != str(decision["payload_hash"])
        or str(command["revision_id"]) != material["decision_revision_id"]
        or material["decision_revision_id"]
        != str(command_payload.get("decision_revision_id") or "")
        or material["action_id"] != str(command_payload.get("action_id") or "")
        or material["effect_id"] != str(command_payload.get("effect_id") or "")
        or material["action_type"] != str(command_payload.get("action_type") or "")
        or material["owner"] != str(command_payload.get("owner") or "")
        or material["executor_id"] != str(command_payload.get("executor") or "")
        or material["target_ref"] != str(command_payload.get("target_ref") or "")
        or material["input_hash"] != str(command_payload.get("input_hash") or "")
        or _precedes(
            historical.source_created_at,
            str(decision["created_at"]),
        )
    ):
        return "runtime_command_binding_mismatch"
    specs = _action_specs(decision_payload)
    spec = next(
        (value for value in specs if value.get("action_id") == material["action_id"]),
        None,
    )
    if spec is None or not _command_matches_spec(
        {
            "payload": command_payload,
            "revision_id": str(command["revision_id"]),
        },
        spec,
    ):
        return "runtime_action_binding_mismatch"
    receipts = conn.execute(
        """
        SELECT status, target_effect_id, before_hash, after_hash,
               evidence_refs, created_at
        FROM cognitive_state_effect_receipts WHERE command_id=?
        """,
        (material["command_id"],),
    ).fetchall()
    if len(receipts) != 1:
        return "runtime_terminal_unresolvable"
    receipt = receipts[0]
    try:
        receipt_refs = {str(value) for value in json.loads(str(receipt["evidence_refs"]))}
    except (TypeError, ValueError, json.JSONDecodeError):
        return "runtime_receipt_invalid"
    required_refs = {
        f"material-command:{material['command_id']}",
        f"decision-revision:{material['decision_revision_id']}",
        f"material-effect:{material['effect_id']}",
    }
    if (
        str(receipt["status"]) != "committed"
        or str(receipt["target_effect_id"]) != material["effect_id"]
        or not str(receipt["before_hash"])
        or not str(receipt["after_hash"])
        or not required_refs.issubset(receipt_refs)
    ):
        return "runtime_receipt_binding_mismatch"
    source = historical.source_binding
    source_key = historical.source_primary_key_value
    if historical.domain == "action_ledger":
        if (
            source_key != material["action_id"]
            or source.get("action_type") != material["action_type"]
            or source.get("target") != material["target_ref"]
            or source.get("quality_decision_id") != material["decision_revision_id"]
            or not any(
                ref.startswith(f"target-oracle:action-ledger:{source_key}:") for ref in receipt_refs
            )
        ):
            return "runtime_source_object_mismatch"
    elif historical.domain == "delivery_events":
        expected_target = (
            f"delivery:{source.get('channel', '')}:"
            f"{source.get('target') or source.get('subject', '')}"
        )
        if (
            source.get("decision") != "deliver"
            or expected_target != material["target_ref"]
            or f"delivery-event:{source_key}" not in receipt_refs
            or not any(
                ref.startswith(f"target-oracle:delivery-event:{source_key}:")
                for ref in receipt_refs
            )
        ):
            return "runtime_source_object_mismatch"
    elif historical.domain == "formal_cognitive_mutations":
        expected_event_id = (
            "fcm_"
            + trust_sha256_json(
                {
                    "command_id": material["command_id"],
                    "asset_kind": source.get("asset_kind", ""),
                    "action": source.get("action", ""),
                    "target_ref": source.get("target_ref", ""),
                }
            )[:32]
        )
        if (
            source_key != expected_event_id
            or source.get("action") != material["action_type"]
            or source.get("target_ref") != material["target_ref"]
            or source.get("decision") != material["decision_revision_id"]
            or not required_refs.issubset(set(historical.provenance_refs))
        ):
            return "runtime_source_object_mismatch"
    else:
        return "runtime_unknown_domain"
    return "verified_runtime"


def _audit_target_effect_journals(
    *,
    state_db: Path,
    journal_paths: Iterable[Path],
) -> dict[str, Any]:
    """Compare canonical receipts with each sink-local SQLite effect journal."""

    if not state_db.is_file():
        return {
            "status": "not_initialized",
            "journal_databases": [],
            "expected_commands": 0,
            "journal_rows": 0,
            "failures": [],
        }
    failures: list[str] = []
    commands: dict[str, dict[str, Any]] = {}
    receipts: dict[str, list[dict[str, Any]]] = {}
    try:
        with sqlite3.connect(
            f"file:{state_db.resolve(strict=True)}?mode=ro",
            uri=True,
            timeout=30,
        ) as conn:
            conn.row_factory = sqlite3.Row
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            required = {
                "cognitive_state_outbox",
                "cognitive_state_effect_receipts",
                "cognitive_data_consumptions",
            }
            if not required.issubset(tables):
                return {
                    "status": "legacy",
                    "journal_databases": [],
                    "expected_commands": 0,
                    "journal_rows": 0,
                    "failures": [],
                }
            command_rows = conn.execute("""
                SELECT command_id, revision_id, payload_json
                FROM cognitive_state_outbox
                WHERE command_type='execute_material_action'
                """).fetchall()
            for row in command_rows:
                payload = _json_object(
                    row["payload_json"],
                    failures,
                    f"target-command:{row['command_id']}",
                )
                commands[str(row["command_id"])] = {
                    "revision_id": str(row["revision_id"]),
                    "payload": payload,
                }
            receipt_rows = conn.execute("""
                SELECT r.command_id, r.receipt_id, r.status,
                       r.target_effect_id, r.before_hash, r.after_hash,
                       r.evidence_refs, r.created_at,
                       c.metadata AS consumption_metadata
                FROM cognitive_state_effect_receipts AS r
                LEFT JOIN cognitive_data_consumptions AS c
                  ON c.consumption_id=r.consumption_id
                """).fetchall()
            for row in receipt_rows:
                receipt = dict(row)
                receipt["evidence_refs"] = _json_array(
                    row["evidence_refs"],
                    failures,
                    f"target-receipt:{row['receipt_id']}",
                )
                receipt["consumption_metadata"] = _json_object(
                    row["consumption_metadata"],
                    failures,
                    f"target-receipt-metadata:{row['receipt_id']}",
                )
                receipts.setdefault(str(row["command_id"]), []).append(receipt)
    except sqlite3.Error as exc:
        return {
            "status": "invalid",
            "journal_databases": [],
            "expected_commands": 0,
            "journal_rows": 0,
            "failures": [f"target effect canonical store query failed: {exc}"],
        }

    canonical_path = state_db.resolve(strict=True)
    journal_rows: dict[str, list[dict[str, Any]]] = {}
    journal_databases: list[str] = []
    for raw_path in sorted(
        {Path(value).expanduser().resolve(strict=False) for value in journal_paths}
    ):
        if not raw_path.is_file():
            continue
        try:
            path = raw_path.resolve(strict=True)
        except OSError as exc:
            failures.append(f"target effect database path unavailable: {raw_path}: {exc}")
            continue
        if path == canonical_path or not path.is_file():
            continue
        try:
            with sqlite3.connect(
                f"file:{path}?mode=ro",
                uri=True,
                timeout=30,
            ) as conn:
                conn.row_factory = sqlite3.Row
                table = conn.execute("""SELECT 1 FROM sqlite_master
                       WHERE type='table' AND name='material_target_effects'""").fetchone()
                if table is None:
                    continue
                schema_state = inspect_material_effect_schema(conn)
                if not schema_state.ok:
                    failures.append(
                        "target effect schema is not canonical: "
                        f"{path}: {schema_state.classification}"
                    )
                integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
                if integrity != "ok":
                    failures.append(f"target effect database integrity_check={integrity}: {path}")
                rows = conn.execute("SELECT * FROM material_target_effects").fetchall()
        except sqlite3.Error as exc:
            failures.append(f"target effect journal query failed: {path}: {exc}")
            continue
        journal_databases.append(str(path))
        for row in rows:
            payload = dict(row)
            payload["database_path"] = str(path)
            journal_rows.setdefault(str(row["command_id"]), []).append(payload)

    expected_commands = {
        command_id
        for command_id, command in commands.items()
        if (
            str(command["payload"].get("owner") or ""),
            str(command["payload"].get("executor") or ""),
            str(command["payload"].get("action_type") or ""),
        )
        in TARGET_EFFECT_LEDGER_FAMILIES
    }
    for command_id in sorted(expected_commands | set(journal_rows)):
        rows = journal_rows.get(command_id, [])
        if len(rows) != 1:
            failures.append(
                "target effect journal cardinality mismatch: "
                f"{command_id}: expected=1 actual={len(rows)}"
            )
            continue
        row = rows[0]
        command = commands.get(command_id)
        if command is None:
            failures.append(f"orphan target effect journal command: {command_id}")
            continue
        payload = command["payload"]
        expected_binding = {
            "command_id": command_id,
            "effect_id": str(payload.get("effect_id") or ""),
            "decision_revision_id": str(command["revision_id"]),
            "action_id": str(payload.get("action_id") or ""),
            "owner": str(payload.get("owner") or ""),
            "executor_id": str(payload.get("executor") or ""),
            "action_type": str(payload.get("action_type") or ""),
            "target_ref": str(payload.get("target_ref") or ""),
            "input_hash": str(payload.get("input_hash") or ""),
            "schema_version": TARGET_EFFECT_LEDGER_SCHEMA,
        }
        if any(str(row.get(key) or "") != value for key, value in expected_binding.items()):
            failures.append(f"target effect journal permit mismatch: {command_id}")
            continue
        command_receipts = receipts.get(command_id, [])
        if len(command_receipts) != 1:
            failures.append(
                "target effect receipt cardinality mismatch: "
                f"{command_id}: expected=1 actual={len(command_receipts)}"
            )
            continue
        receipt = command_receipts[0]
        try:
            journal_refs = json.loads(str(row.get("evidence_refs_json") or "[]"))
        except json.JSONDecodeError:
            journal_refs = None
        metadata = receipt.get("consumption_metadata")
        if not isinstance(journal_refs, list) or not journal_refs:
            failures.append(f"target effect journal evidence invalid: {command_id}")
            continue
        if not isinstance(metadata, Mapping):
            failures.append(f"target effect receipt metadata invalid: {command_id}")
            continue
        receipt_refs = {str(value) for value in receipt.get("evidence_refs") or ()}
        exact_match = (
            str(row.get("status") or "") == str(receipt.get("status") or "")
            and str(row.get("effect_id") or "") == str(receipt.get("target_effect_id") or "")
            and str(row.get("before_hash") or "") == str(receipt.get("before_hash") or "")
            and str(row.get("after_hash") or "") == str(receipt.get("after_hash") or "")
            and str(row.get("reason_code") or "") == str(metadata.get("terminal_reason_code") or "")
            and bool(row.get("retry_exhausted")) == (metadata.get("retry_exhausted") is True)
            and str(row.get("observed_at") or "") == str(receipt.get("created_at") or "")
            and {str(value) for value in journal_refs}.issubset(receipt_refs)
        )
        if not exact_match:
            failures.append(f"target effect journal does not match terminal receipt: {command_id}")
    return {
        "status": "available",
        "journal_databases": journal_databases,
        "expected_commands": len(expected_commands),
        "journal_rows": sum(len(rows) for rows in journal_rows.values()),
        "failures": failures,
    }


def _audit_live_store(path: Path) -> dict[str, Any]:
    metrics = {name: 0 for name in ZERO_METRICS}
    if not path.is_file():
        return {
            "status": "not_initialized",
            "path": str(path),
            "metrics": metrics,
            "failures": [],
            "counts": {},
        }

    failures: list[str] = []
    counts: Counter[str] = Counter()
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        return {
            "status": "unavailable",
            "path": str(path),
            "metrics": metrics,
            "failures": [f"state store unavailable: {exc}"],
            "counts": {},
        }
    try:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            failures.append(f"state store integrity_check={integrity}")
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        required = {
            "cognitive_state_revisions",
            "cognitive_state_heads",
            "cognitive_state_outbox",
            "cognitive_state_effect_receipts",
            "cognitive_data_events",
            "cognitive_data_consumptions",
            "cognitive_state_migration_quarantine",
            REGISTRY_TABLE,
        }
        missing = sorted(required - tables)
        if missing:
            return {
                "status": "legacy",
                "path": str(path),
                "integrity_check": integrity,
                "metrics": metrics,
                "failures": [f"canonical decision tables missing: {','.join(missing)}"],
                "counts": {},
                "activation_marker": False,
            }

        registry = {
            str(row["component"]): (str(row["schema_version"]), str(row["ddl_hash"]))
            for row in conn.execute(
                render_sql(
                    "SELECT component, schema_version, ddl_hash FROM {table}",
                    identifiers={"table": REGISTRY_TABLE},
                )
            ).fetchall()
        }
        schema_entry = registry.get("cognitive_state_store")
        if schema_entry is None or schema_entry[0] != STATE_SCHEMA_VERSION:
            failures.append("canonical cognitive state schema v2 is not registered")
        marker = registry.get(DECISION_TRACE_ENFORCEMENT_COMPONENT)
        activation_marker = marker == (
            DECISION_TRACE_ENFORCEMENT_VERSION,
            DECISION_TRACE_ENFORCEMENT_HASH,
        )
        if not activation_marker:
            failures.append("strict decision-trace activation marker is missing or invalid")

        revision_rows = conn.execute("""
            SELECT revision_id, object_type, object_id, scope_type, scope_id,
                   payload_json, payload_hash, admission_state, created_at
            FROM cognitive_state_revisions
            """).fetchall()
        revisions: dict[str, dict[str, Any]] = {}
        for row in revision_rows:
            revision_id = str(row["revision_id"])
            payload = _json_object(row["payload_json"], failures, f"revision:{revision_id}")
            revisions[revision_id] = {
                "revision_id": revision_id,
                "object_type": str(row["object_type"]),
                "object_id": str(row["object_id"]),
                "scope_type": str(row["scope_type"]),
                "scope_id": str(row["scope_id"]),
                "payload": payload,
                "payload_hash": str(row["payload_hash"]),
                "admission_state": str(row["admission_state"]),
                "created_at": str(row["created_at"]),
            }
        decisions = {
            key: value
            for key, value in revisions.items()
            if value["object_type"] == "decision_trace"
        }
        counts["decision_revisions"] = len(decisions)

        commands: dict[str, dict[str, Any]] = {}
        commands_by_revision: dict[str, list[dict[str, Any]]] = {}
        command_rows = conn.execute("""
            SELECT command_id, revision_id, event_id, consumer_id, command_type,
                   payload_json, payload_hash, created_at
            FROM cognitive_state_outbox
            WHERE command_type='execute_material_action'
            """).fetchall()
        for row in command_rows:
            command_id = str(row["command_id"])
            command: dict[str, Any] = {
                "command_id": command_id,
                "revision_id": str(row["revision_id"]),
                "event_id": str(row["event_id"]),
                "consumer_id": str(row["consumer_id"]),
                "payload": _json_object(row["payload_json"], failures, f"command:{command_id}"),
                "payload_hash": str(row["payload_hash"]),
                "created_at": str(row["created_at"]),
            }
            commands[command_id] = command
            commands_by_revision.setdefault(command["revision_id"], []).append(command)
        counts["material_commands"] = len(commands)

        receipts_by_command: dict[str, list[dict[str, Any]]] = {}
        receipt_rows = conn.execute("""
            SELECT r.receipt_id, r.command_id, r.revision_id, r.event_id,
                   r.consumer_id, r.consumption_id, r.status,
                   r.target_effect_id, r.before_hash, r.after_hash,
                   r.evidence_refs, r.created_at,
                   c.metadata AS consumption_metadata
            FROM cognitive_state_effect_receipts AS r
            LEFT JOIN cognitive_data_consumptions AS c
              ON c.consumption_id=r.consumption_id
            """).fetchall()
        for row in receipt_rows:
            receipt = dict(row)
            receipt["evidence_refs"] = _json_array(
                row["evidence_refs"], failures, f"receipt:{row['receipt_id']}"
            )
            receipt["consumption_metadata"] = _json_object(
                row["consumption_metadata"],
                failures,
                f"receipt-metadata:{row['receipt_id']}",
            )
            receipts_by_command.setdefault(str(row["command_id"]), []).append(receipt)
        counts["material_terminal_receipts"] = sum(
            len(values)
            for command_id, values in receipts_by_command.items()
            if command_id in commands
        )

        for command in commands.values():
            decision = decisions.get(command["revision_id"])
            payload = command["payload"]
            if decision is None:
                metrics["action_without_decision"] += 1
                continue
            if sha256_json(payload) != command["payload_hash"]:
                failures.append(f"material command payload hash mismatch: {command['command_id']}")
            specs = _action_specs(decision["payload"])
            spec = next(
                (value for value in specs if value.get("action_id") == payload.get("action_id")),
                None,
            )
            if spec is None or not _command_matches_spec(command, spec):
                failures.append(
                    f"material command/action binding mismatch: {command['command_id']}"
                )
            if _precedes(command["created_at"], decision["created_at"]):
                failures.append(f"material command precedes decision: {command['command_id']}")

            receipts = receipts_by_command.get(command["command_id"], [])
            if len(receipts) > 1:
                failures.append(f"multiple terminal receipts: {command['command_id']}")
            for receipt in receipts:
                if not _receipt_matches(command, receipt):
                    failures.append(f"material receipt binding mismatch: {command['command_id']}")
                if _precedes(str(receipt["created_at"]), decision["created_at"]):
                    failures.append(f"material effect precedes decision: {command['command_id']}")
                if _precedes(str(receipt["created_at"]), command["created_at"]):
                    failures.append(f"material effect precedes command: {command['command_id']}")

        for decision_id, decision in decisions.items():
            payload = decision["payload"]
            if sha256_json(payload) != decision["payload_hash"]:
                failures.append(f"DecisionTrace payload hash mismatch: {decision_id}")
            if _contains_prohibited_reasoning(payload):
                failures.append(
                    f"DecisionTrace contains prohibited private reasoning: {decision_id}"
                )
            state = str(payload.get("decision_state") or "")
            specs = _action_specs(payload)
            decision_commands = commands_by_revision.get(decision_id, [])
            if state == "approved":
                terminal_action_ids = {
                    str(command["payload"].get("action_id") or "")
                    for command in decision_commands
                    if len(receipts_by_command.get(command["command_id"], ())) == 1
                    and _receipt_matches(
                        command,
                        receipts_by_command[command["command_id"]][0],
                    )
                }
                metrics["decision_without_action_terminal"] += sum(
                    1
                    for spec in specs
                    if str(spec.get("action_id") or "") not in terminal_action_ids
                )
            elif state == "rejected":
                if specs or decision_commands:
                    failures.append(f"rejected decision emitted material action: {decision_id}")
            else:
                failures.append(f"unsupported decision state: {decision_id}")

            _audit_dead_letter_supersessions(
                decision,
                decisions=decisions,
                commands=commands,
                receipts_by_command=receipts_by_command,
                failures=failures,
            )

            value_revision_id = str(payload.get("value_context_revision_id") or "")
            if not value_revision_id:
                metrics["decision_without_value_context"] += 1
            value_revision = revisions.get(value_revision_id)
            if value_revision_id and (
                value_revision is None or value_revision["object_type"] != "value_context"
            ):
                metrics["value_context_revision_missing"] += 1
            elif value_revision is not None:
                _audit_value_context(decision, value_revision, metrics, failures)

            snapshot_revision_id = str(payload.get("snapshot_revision_id") or "")
            snapshot = revisions.get(snapshot_revision_id)
            if snapshot is None or snapshot["object_type"] != "cognitive_state_snapshot":
                metrics["decision_snapshot_unresolvable"] += 1
            else:
                _audit_snapshot(decision, snapshot, revisions, metrics, failures)

        active_historical = conn.execute("""
            SELECT COUNT(*)
            FROM cognitive_state_heads h
            JOIN cognitive_state_revisions r ON r.revision_id=h.revision_id
            WHERE r.admission_state!='active'
            """).fetchone()[0]
        if int(active_historical):
            failures.append(
                f"historical-incomplete objects entered active heads: {active_historical}"
            )
        counts["historical_incomplete"] = int(
            conn.execute(
                "SELECT COUNT(*) FROM cognitive_state_migration_quarantine WHERE reason_code='historical_incomplete'"
            ).fetchone()[0]
        )
        return {
            "status": "available",
            "path": str(path),
            "integrity_check": integrity,
            "activation_marker": activation_marker,
            "metrics": metrics,
            "failures": failures,
            "counts": dict(counts),
        }
    except sqlite3.Error as exc:
        failures.append(f"state store audit query failed: {exc}")
        return {
            "status": "invalid",
            "path": str(path),
            "metrics": metrics,
            "failures": failures,
            "counts": dict(counts),
        }
    finally:
        conn.close()


def _audit_value_context(
    decision: Mapping[str, Any],
    value_revision: Mapping[str, Any],
    metrics: dict[str, int],
    failures: list[str],
) -> None:
    payload = value_revision["payload"]
    decision_payload = decision["payload"]
    if sha256_json(payload) != value_revision["payload_hash"]:
        failures.append(f"ValueContext payload hash mismatch: {value_revision['revision_id']}")
    if (
        payload.get("precedence_contract") != VALUE_PRECEDENCE_CONTRACT
        or tuple(payload.get("precedence") or ()) != FIXED_VALUE_PRECEDENCE
        or decision_payload.get("value_context_hash") != value_revision["payload_hash"]
    ):
        failures.append(f"ValueContext contract mismatch: {decision['revision_id']}")
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        metrics["value_ref_missing"] += 1
        return
    item_refs = {str(item.get("item_ref") or "") for item in items if isinstance(item, dict)}
    selected_key = str((decision_payload.get("selection") or {}).get("candidate_key") or "")
    selected = next(
        (
            row
            for row in decision_payload.get("candidates") or ()
            if isinstance(row, dict) and str(row.get("key") or "") == selected_key
        ),
        None,
    )
    if selected is None:
        failures.append(f"selected candidate is unavailable: {decision['revision_id']}")
        return
    refs = set(str(value) for value in selected.get("violated_value_refs") or ())
    refs.update(str(value) for value in selected.get("satisfies_value_refs") or ())
    metrics["value_ref_missing"] += len({value for value in refs if value not in item_refs})
    if selected.get("violated_value_refs"):
        failures.append(f"hard constraint override: {decision['revision_id']}")


def _audit_snapshot(
    decision: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    revisions: Mapping[str, Mapping[str, Any]],
    metrics: dict[str, int],
    failures: list[str],
) -> None:
    payload = snapshot["payload"]
    if sha256_json(payload) != snapshot["payload_hash"]:
        failures.append(f"snapshot payload hash mismatch: {snapshot['revision_id']}")
    without_self_hash = dict(payload)
    stored_hash = str(without_self_hash.pop("snapshot_hash", ""))
    if not stored_hash or sha256_json(without_self_hash) != stored_hash:
        metrics["snapshot_hash_mismatch"] += 1
    decision_payload = decision["payload"]
    if (
        decision_payload.get("snapshot_revision_id") != snapshot["revision_id"]
        or decision_payload.get("snapshot_id") != snapshot["object_id"]
        or decision_payload.get("snapshot_hash") != stored_hash
    ):
        metrics["decision_snapshot_unresolvable"] += 1
    consumed = payload.get("consumed_state") or []
    if not isinstance(consumed, list):
        metrics["value_ref_missing"] += 1
        return
    if not _snapshot_source_purpose_contract_matches(
        payload,
        consumed,
        revisions,
    ):
        metrics["decision_snapshot_source_purpose_contract_gap"] += 1
        failures.append(f"snapshot source-purpose contract mismatch: {snapshot['revision_id']}")
    for entry in consumed:
        if not isinstance(entry, dict):
            metrics["value_ref_missing"] += 1
            continue
        revision_id = str(entry.get("revision_id") or "")
        revision = revisions.get(revision_id)
        if revision is None or entry.get("payload_hash") != revision["payload_hash"]:
            metrics["value_ref_missing"] += 1
    for head in payload.get("head_preconditions") or ():
        if not isinstance(head, dict) or str(head.get("revision_id") or "") not in revisions:
            failures.append(f"unresolved head-precondition drift: {decision['revision_id']}")
            break


def _snapshot_source_purpose_contract_matches(
    payload: Mapping[str, Any],
    consumed: list[Any],
    revisions: Mapping[str, Mapping[str, Any]],
) -> bool:
    schema_version = str(payload.get("schema_version") or "")
    if schema_version == "mnemos.cognitive_state_snapshot.v1":
        return True
    if schema_version != COGNITIVE_OBJECT_SCHEMA_VERSIONS["cognitive_state_snapshot"]:
        return False

    completeness = payload.get("source_completeness")
    if not isinstance(completeness, Mapping):
        return False
    if completeness.get("contract") != {
        "schema_version": DECISION_SNAPSHOT_SOURCE_PURPOSE_SCHEMA_VERSION,
        "contract_hash": DECISION_SNAPSHOT_SOURCE_PURPOSE_CONTRACT_HASH,
        "output_purpose": DECISION_SNAPSHOT_OUTPUT_PURPOSE,
    }:
        return False
    summaries = completeness.get("by_object_type")
    if not isinstance(summaries, Mapping) or set(summaries) != set(
        DECISION_SNAPSHOT_SOURCE_PURPOSES
    ):
        return False

    aggregate_candidates = 0
    aggregate_authorized = 0
    aggregate_denials: Counter[str] = Counter()
    authorized_by_type: Counter[str] = Counter()
    for entry in consumed:
        if not isinstance(entry, Mapping):
            return False
        revision = revisions.get(str(entry.get("revision_id") or ""))
        if revision is None:
            return False
        object_type = str(revision["object_type"])
        expected_purpose = DECISION_SNAPSHOT_SOURCE_PURPOSES.get(object_type)
        if (
            expected_purpose is None
            or entry.get("source_read_purpose") != expected_purpose
            or entry.get("source_purpose_contract_hash")
            != DECISION_SNAPSHOT_SOURCE_PURPOSE_CONTRACT_HASH
        ):
            return False
        source_payload = revision.get("payload")
        if not isinstance(source_payload, Mapping) or not isinstance(
            source_payload.get("access_control"), Mapping
        ):
            return False
        if entry.get("access_control_hash") != cognitive_access_hash(
            source_payload["access_control"]
        ):
            return False
        authorized_by_type[object_type] += 1

    for object_type, expected_purpose in DECISION_SNAPSHOT_SOURCE_PURPOSES.items():
        summary = summaries.get(object_type)
        if not isinstance(summary, Mapping) or summary.get("purpose") != expected_purpose:
            return False
        candidate_count = summary.get("candidate_count")
        authorized_count = summary.get("authorized_count")
        denied_by_reason = summary.get("denied_by_reason")
        if not _is_nonnegative_int(candidate_count):
            return False
        if not _is_nonnegative_int(authorized_count):
            return False
        if authorized_count > candidate_count or not isinstance(denied_by_reason, Mapping):
            return False
        typed_denials: dict[str, int] = {}
        for reason, count in denied_by_reason.items():
            if not _is_nonnegative_int(count):
                return False
            typed_denials[str(reason)] = count
        if candidate_count - authorized_count != sum(typed_denials.values()):
            return False
        if authorized_by_type[object_type] != authorized_count:
            return False
        aggregate_candidates += candidate_count
        aggregate_authorized += authorized_count
        aggregate_denials.update(typed_denials)

    return (
        completeness.get("candidate_count") == aggregate_candidates
        and completeness.get("authorized_count") == aggregate_authorized
        and completeness.get("denied_by_reason") == dict(sorted(aggregate_denials.items()))
    )


def _is_nonnegative_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _command_matches_spec(command: Mapping[str, Any], spec: Mapping[str, Any]) -> bool:
    payload = command["payload"]
    return (
        all(
            payload.get(key) == spec.get(key)
            for key in (
                "action_id",
                "effect_id",
                "action_type",
                "owner",
                "executor",
                "target_ref",
                "target_hash",
                "input_hash",
            )
        )
        and payload.get("decision_revision_id") == command["revision_id"]
    )


def _receipt_matches(command: Mapping[str, Any], receipt: Mapping[str, Any]) -> bool:
    payload = command["payload"]
    refs = set(str(value) for value in receipt.get("evidence_refs") or ())
    metadata = receipt.get("consumption_metadata")
    if not isinstance(metadata, Mapping):
        return False
    status = str(receipt.get("status") or "")
    reason_code = str(metadata.get("terminal_reason_code") or "")
    retry_exhausted = metadata.get("retry_exhausted") is True
    effect_id = str(payload.get("effect_id") or "")
    command_id = str(command.get("command_id") or "")
    revision_id = str(command.get("revision_id") or "")
    before_hash = str(receipt.get("before_hash") or "")
    after_hash = str(receipt.get("after_hash") or "")
    required = {
        f"material-command:{command_id}",
        f"decision-revision:{revision_id}",
        f"material-effect:{effect_id}",
    }
    binding_matches = (
        receipt.get("revision_id") == revision_id
        and receipt.get("event_id") == command["event_id"]
        and receipt.get("consumer_id") == command["consumer_id"]
        and receipt.get("target_effect_id") == effect_id
        and _canonical_hash(before_hash)
        and _canonical_hash(after_hash)
        and required.issubset(refs)
    )
    if not binding_matches:
        return False

    target_oracle = any(ref.startswith("target-oracle:") for ref in refs)
    target_journal = any(ref.startswith("target-journal:") for ref in refs)
    rollback = any(ref.startswith("rollback:") for ref in refs)
    if status == "committed":
        return (
            not reason_code
            and not retry_exhausted
            and f"target-after:{after_hash}" in refs
            and (target_oracle or target_journal)
        )
    if status in {"failed_terminal", "dead_letter"}:
        if (
            not reason_code
            or f"attempted-effect:{effect_id}" not in refs
            or (before_hash != after_hash and not rollback)
            or not (target_oracle or rollback)
        ):
            return False
        if status == "dead_letter":
            return retry_exhausted and f"retry-budget-exhausted:{command_id}" in refs
        return not retry_exhausted
    if status not in {"rejected", "revoked", "intentional_skip"}:
        return False
    if (
        not reason_code
        or retry_exhausted
        or before_hash != after_hash
        or f"no-effect-oracle:{effect_id}:{before_hash}" not in refs
    ):
        return False
    return status != "intentional_skip" or f"approved-skip:{revision_id}" in refs


def _canonical_hash(value: str) -> bool:
    if not value.startswith("sha256:") or len(value) != 71:
        return False
    return all(character in "0123456789abcdef" for character in value[7:])


def _action_specs(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("action_specs")
    if not isinstance(raw, list):
        return []
    return [dict(value) for value in raw if isinstance(value, dict)]


def _same_material_action_binding(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> bool:
    return all(
        str(left.get(key) or "") == str(right.get(key) or "")
        for key in ("owner", "executor", "action_type", "target_ref", "input_hash")
    )


def _audit_dead_letter_supersessions(
    decision: Mapping[str, Any],
    *,
    decisions: Mapping[str, Mapping[str, Any]],
    commands: Mapping[str, Mapping[str, Any]],
    receipts_by_command: Mapping[str, list[dict[str, Any]]],
    failures: list[str],
) -> None:
    """Independently prove every exact dead-letter retry generation is linked."""

    decision_id = str(decision["revision_id"])
    payload = decision["payload"]
    raw_declared = payload.get("supersedes_decision_revision_ids")
    if not isinstance(raw_declared, list) or any(
        not isinstance(value, str) or not value.strip() for value in raw_declared or ()
    ):
        failures.append(f"DecisionTrace supersession list is malformed: {decision_id}")
        declared: list[str] = []
    else:
        declared = [str(value) for value in raw_declared]
        if declared != sorted(set(declared)):
            failures.append(
                f"DecisionTrace supersession list is not sorted and unique: {decision_id}"
            )

    current_specs = _action_specs(payload)
    expected: set[str] = set()
    for current_spec in current_specs:
        exact_terminals: list[tuple[str, str, Mapping[str, Any], Mapping[str, Any]]] = []
        for command in commands.values():
            prior_decision_id = str(command.get("revision_id") or "")
            if prior_decision_id == decision_id:
                continue
            prior_decision = decisions.get(prior_decision_id)
            if (
                prior_decision is None
                or (
                    prior_decision.get("scope_type"),
                    prior_decision.get("scope_id"),
                )
                != (decision.get("scope_type"), decision.get("scope_id"))
                or _precedes(
                    str(decision.get("created_at") or ""),
                    str(prior_decision.get("created_at") or ""),
                )
                or not _same_material_action_binding(
                    command.get("payload") or {},
                    current_spec,
                )
            ):
                continue
            for receipt in receipts_by_command.get(
                str(command.get("command_id") or ""),
                (),
            ):
                exact_terminals.append(
                    (
                        str(receipt.get("created_at") or ""),
                        str(receipt.get("receipt_id") or ""),
                        command,
                        receipt,
                    )
                )
        if exact_terminals:
            _created_at, _receipt_id, latest_command, latest_receipt = max(
                exact_terminals,
                key=lambda value: (value[0], value[1]),
            )
            if str(latest_receipt.get("status") or "") == "dead_letter":
                expected.add(str(latest_command.get("revision_id") or ""))

    expected_list = sorted(expected)
    if declared != expected_list:
        failures.append(
            "DecisionTrace dead-letter supersession mismatch: "
            f"{decision_id}: declared={declared!r} expected={expected_list!r}"
        )

    for prior_decision_id in declared:
        prior_decision = decisions.get(prior_decision_id)
        if (
            prior_decision is None
            or (
                prior_decision.get("scope_type"),
                prior_decision.get("scope_id"),
            )
            != (decision.get("scope_type"), decision.get("scope_id"))
            or _precedes(
                str(decision.get("created_at") or ""),
                str(prior_decision.get("created_at") or ""),
            )
        ):
            failures.append(f"DecisionTrace supersedes invalid or later decision: {decision_id}")
            continue
        linked = any(
            str(command.get("revision_id") or "") == prior_decision_id
            and any(
                str(receipt.get("status") or "") == "dead_letter"
                for receipt in receipts_by_command.get(
                    str(command.get("command_id") or ""),
                    (),
                )
            )
            and any(
                _same_material_action_binding(
                    command.get("payload") or {},
                    current_spec,
                )
                for current_spec in current_specs
            )
            for command in commands.values()
        )
        if not linked:
            failures.append(f"DecisionTrace supersession lacks exact dead letter: {decision_id}")


def _json_object(raw: Any, failures: list[str], label: str) -> dict[str, Any]:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        failures.append(f"invalid JSON object: {label}")
        return {}
    if not isinstance(value, dict):
        failures.append(f"invalid JSON object: {label}")
        return {}
    return value


def _json_array(raw: Any, failures: list[str], label: str) -> list[Any]:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        failures.append(f"invalid JSON array: {label}")
        return []
    if not isinstance(value, list):
        failures.append(f"invalid JSON array: {label}")
        return []
    return value


def _contains_prohibited_reasoning(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key).lower() in PROHIBITED_REASONING_FIELDS or _contains_prohibited_reasoning(child)
            for key, child in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_prohibited_reasoning(child) for child in value)
    return False


def _precedes(left: str, right: str) -> bool:
    try:
        return datetime.fromisoformat(left.replace("Z", "+00:00")) < datetime.fromisoformat(
            right.replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return True
