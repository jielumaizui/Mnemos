"""Bounded CLI presentation for Agent Native-to-Raw reconciliation."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from core.ops.durable_io import DurableIOError
from core.sync_framework.native_artifact_inventory import (
    DEFAULT_SNAPSHOT_PARSE_INFRASTRUCTURE_ATTEMPTS,
)
from scripts.agent_source_raw_recovery_contract import (
    AgentSourceRawReconciliationError,
    SCHEMA_VERSION,
)
from scripts.agent_source_raw_recovery_support import _canonical_hash


@dataclass(frozen=True)
class CliDependencies:
    """Call-local seams resolved by the reconciler facade."""

    config_factory: Callable[..., Any]
    load_active_sources: Callable[[], list[Any]]
    with_current_codex_cutoff: Callable[[list[Any]], list[Any]]
    reconcile: Callable[..., Mapping[str, Any]]
    write_new_receipt: Callable[[Path, Mapping[str, Any]], None]
    file_sha256: Callable[[Path], str]


def default_raw_db_path(config: Any) -> Path:
    configured = config.get("raw_event_store.db_path")
    return Path(configured or (Path(config.database_dir) / "raw_events.db")).expanduser()


def bounded_cli_error_details(
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep typed content-free failures without unbounded terminal output."""
    details = result.get("error_details")
    if not isinstance(details, Mapping):
        return {}

    def bounded_failure(value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            return {}
        bounded: dict[str, Any] = {}
        text_contracts = {
            "source_name": r"[a-z][a-z0-9_-]{0,63}",
            "error_code": r"[a-z][a-z0-9_]{2,127}",
            "reason_code": r"[a-z][a-z0-9_]{2,127}",
            "exception_type": r"[A-Za-z_][A-Za-z0-9_.]{0,127}",
            "session_id_hash": r"sha256:[0-9a-f]{64}",
            "sqlite_errorname": r"SQLITE_[A-Z0-9_]{1,96}",
        }
        for key, pattern in text_contracts.items():
            candidate = value.get(key)
            if isinstance(candidate, str) and re.fullmatch(pattern, candidate):
                bounded[key] = candidate
        attempt_count = value.get("attempt_count")
        if (
            isinstance(attempt_count, int)
            and not isinstance(attempt_count, bool)
            and 1 <= attempt_count <= DEFAULT_SNAPSHOT_PARSE_INFRASTRUCTURE_ATTEMPTS
        ):
            bounded["attempt_count"] = attempt_count
        signal_value = value.get("signal")
        if (
            isinstance(signal_value, int)
            and not isinstance(signal_value, bool)
            and 1 <= signal_value <= 127
        ):
            bounded["signal"] = signal_value
        failure_class = value.get("failure_class")
        if failure_class in {
            "os_nontransient",
            "os_transient",
            "sqlite_nontransient",
            "sqlite_transient",
            "storage_untyped",
        }:
            bounded["failure_class"] = failure_class
        for key in ("os_errno", "sqlite_errorcode"):
            candidate = value.get(key)
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                bounded[key] = candidate
        return bounded

    bounded = bounded_failure(details)
    failures = details.get("failures")
    if isinstance(failures, list):
        selected = [item for item in (bounded_failure(value) for value in failures[:16]) if item]
        bounded["failure_count"] = len(failures)
        bounded["failures"] = selected
        bounded["failures_truncated"] = len(failures) > 16
    source_failure_count = details.get("source_failure_count")
    if (
        isinstance(source_failure_count, int)
        and not isinstance(source_failure_count, bool)
        and 0 <= source_failure_count <= 1024
    ):
        bounded["source_failure_count"] = source_failure_count
    return bounded


def bounded_cli_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    """Return a stable content-free summary with the exact plan hash."""
    inventory_value = result.get("native_artifact_inventory")
    inventory = inventory_value if isinstance(inventory_value, Mapping) else {}
    snapshot_value = result.get("native_artifact_snapshot")
    snapshot = snapshot_value if isinstance(snapshot_value, Mapping) else {}
    identity_value = result.get("session_identity_reconciliation")
    identity = identity_value if isinstance(identity_value, Mapping) else {}
    projection_value = result.get("current_projection_reconciliation")
    projection = projection_value if isinstance(projection_value, Mapping) else {}
    post_gap_value = result.get("post_apply_gap")
    post_gap = post_gap_value if isinstance(post_gap_value, Mapping) else {}
    conservation_value = result.get("raw_conservation")
    conservation = conservation_value if isinstance(conservation_value, Mapping) else {}
    projection_keys = (
        "ok",
        "invalid_count",
        "restore_revision_count",
        "append_revision_count",
        "blocked_count",
        "repaired_count",
        "invalid_after_count",
    )
    identity_keys = (
        "ok",
        "ambiguous_count",
        "unresolved_count",
        "candidate_count",
        "reconciliation_count",
    )
    summary = {
        "schema_version": "mnemos.agent_source_raw_cli_summary.v2",
        "result_schema_version": str(result.get("schema_version") or ""),
        "mode": str(result.get("mode") or ""),
        "ok": bool(result.get("ok")),
        "error_code": str(result.get("error_code") or ""),
        "plan_hash": str(result.get("plan_hash") or ""),
        "apply_eligible": result.get("apply_eligible") is True,
        "current_state_ok": result.get("current_state_ok") is True,
        "writer_lock_state": str(result.get("writer_lock_state") or ""),
        "active_sources": [str(value) for value in list(result.get("active_sources") or [])[:32]],
        "native_artifact_inventory": {
            "inventory_hash": str(inventory.get("inventory_hash") or ""),
            "source_count": int(inventory.get("source_count") or 0),
            "artifact_count": int(inventory.get("artifact_count") or 0),
        },
        "native_artifact_snapshot": {
            key: snapshot.get(key)
            for key in (
                "inventory_hash",
                "snapshot_artifact_count",
                "snapshot_logical_bytes",
                "stabilization_attempts",
            )
            if key in snapshot
        },
        "session_identity_reconciliation": {
            key: identity.get(key) for key in identity_keys if key in identity
        },
        "current_projection_reconciliation": {
            key: projection.get(key) for key in projection_keys if key in projection
        },
        "raw_conservation": {
            key: conservation.get(key)
            for key in (
                "logical_event_count",
                "revision_count",
                "current_revision_count",
                "native_contract_observation_count",
            )
            if key in conservation
        },
        "required_gap": int(result.get("required_gap") or post_gap.get("required_gap") or 0),
        "full_result_sha256": _canonical_hash(dict(result)),
    }
    error_details = bounded_cli_error_details(result)
    if error_details:
        summary["error_details"] = error_details
    return summary


def main(
    argv: list[str] | None,
    *,
    dependencies: CliDependencies,
) -> int:
    """Execute the CLI through call-local facade dependencies."""
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild a frozen 12-source Native-to-Raw generation " "without semantic writes."
        )
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--db-path", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--expected-plan-hash", default="")
    parser.add_argument(
        "--confirm-read-native-history",
        action="store_true",
    )
    parser.add_argument(
        "--defer-current-codex-thread",
        action="store_true",
    )
    parser.add_argument("--cycles", type=int, default=2)
    parser.add_argument("--batch-sessions", type=int, default=100)
    parser.add_argument("--batch-turns", type=int, default=100)
    parser.add_argument("--resume", action="store_true")
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument("--json", action="store_true")
    output_group.add_argument("--summary-json", action="store_true")
    parser.add_argument(
        "--output-json",
        type=Path,
        help=(
            "write the full result to a new file under a new directory or an "
            "existing owner-only 0700 directory; broader existing directories "
            "are rejected and never chmodded"
        ),
    )
    args = parser.parse_args(argv)
    try:
        config = dependencies.config_factory(
            config_path=args.config,
            provision=False,
        )
        raw_db_path = args.db_path or default_raw_db_path(config)
        if not args.confirm_read_native_history:
            raise AgentSourceRawReconciliationError("native_history_read_confirmation_required")
        if args.apply and args.backup_dir is None:
            raise AgentSourceRawReconciliationError("backup_directory_required")
        if args.apply and not args.expected_plan_hash:
            raise AgentSourceRawReconciliationError("expected_plan_hash_required")
        backup_dir = args.backup_dir or (Path(config.database_dir) / "backups")
        sources = list(dependencies.load_active_sources())
        if args.defer_current_codex_thread:
            sources = dependencies.with_current_codex_cutoff(sources)
        result = dict(
            dependencies.reconcile(
                config=config,
                raw_db_path=raw_db_path,
                backup_dir=backup_dir,
                sources=sources,
                apply=bool(args.apply),
                cycles=int(args.cycles),
                batch_sessions=int(args.batch_sessions),
                batch_turns=int(args.batch_turns),
                reset_derived_state=not bool(args.resume),
                expected_plan_hash=str(args.expected_plan_hash),
            )
        )
    except AgentSourceRawReconciliationError as exc:
        result = {
            "schema_version": SCHEMA_VERSION,
            "mode": "apply" if args.apply else "dry_run",
            "ok": False,
            "error_code": exc.code,
        }
        if exc.details:
            result["error_details"] = exc.details
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        ImportError,
        AttributeError,
        RuntimeError,
    ):
        result = {
            "schema_version": SCHEMA_VERSION,
            "mode": "apply" if args.apply else "dry_run",
            "ok": False,
            "error_code": "reconciliation_failed",
        }
    output_evidence: dict[str, Any] = {}
    output_failed = False
    if args.output_json is not None:
        try:
            output_path = Path(args.output_json).expanduser().resolve(strict=False)
            dependencies.write_new_receipt(output_path, result)
            output_evidence = {
                "status": "written",
                "path": str(output_path),
                "sha256": (f"sha256:{dependencies.file_sha256(output_path)}"),
            }
        except (
            AgentSourceRawReconciliationError,
            DurableIOError,
            OSError,
        ):
            output_failed = True
            output_evidence = {
                "status": "failed",
                "path": str(Path(args.output_json).expanduser()),
                "error_code": "output_json_write_failed",
            }
    rendered_result = bounded_cli_summary(result) if args.summary_json else dict(result)
    if output_evidence:
        rendered_result["output_json"] = output_evidence
    rendered = json.dumps(
        rendered_result,
        ensure_ascii=False,
        sort_keys=True,
    )
    print(
        rendered
        if args.json or args.summary_json
        else json.dumps(
            rendered_result,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if result.get("ok") and not output_failed else 1
