#!/usr/bin/env python3
"""Independently audit continuous capture coverage for all active sources.

This verifier deliberately reads the tracked JSON manifest, effective daemon
configuration, and persisted heartbeat directly.  It does not instantiate a
parser or reuse the daemon's source registry, so an implementation-side list
cannot hide a missing owner or a silent source skip.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from core.ops.durable_io import read_native_bytes

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = PROJECT_ROOT / "core" / "agent_kit" / "agent_source_support_manifest.json"
REPORT_SCHEMA_VERSION = "mnemos.agent_source_coverage_audit.v3"
COVERAGE_SCHEMA_VERSION = "mnemos.agent_source_coverage.v2"
EXPECTED_OWNER = "daemon.raw_sync"
EXPECTED_SERVICE = "raw_sync"
EXPECTED_ACTIVATION_KEY = "daemon.services.raw_sync"
EXPECTED_CURSOR_KIND = "continuous_tail_reconcile_v1"
EXPECTED_ACTIVE_SOURCE_COUNT = 12
EXPECTED_HOST_AGENT_COUNT = 8
EXPECTED_INGESTION_ONLY_SOURCE_COUNT = 4
CAPTURE_CURSOR_HASH_FIELDS = (
    "capture_roster_hash",
    "capture_denominator_session_set_hash",
    "capture_expected_turn_fingerprint_set_hash",
    "capture_receipt_binding_set_hash",
)
CAPTURE_CURSOR_COUNT_FIELDS = (
    "capture_expected_turn_count",
    "capture_receipt_count",
    "capture_exact_receipt_count",
    "capture_pending_turn_count",
    "capture_orphan_receipt_count",
)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _sha256_text(value: object) -> bool:
    text = _text(value)
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _config_value(config: Any, key: str, default: Any = None) -> Any:
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            return default
    value: Any = config
    for part in key.split("."):
        if not isinstance(value, Mapping):
            return default
        value = value.get(part, default)
    return value


def _load_json(path: Path) -> tuple[Mapping[str, Any], str]:
    try:
        payload = json.loads(read_native_bytes(Path(path)).decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {}, exc.__class__.__name__
    if not isinstance(payload, Mapping):
        return {}, "not_object"
    return payload, ""


def _canonical_hash(value: Mapping[str, Any]) -> str:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _finding(code: str, source: str = "", *, detail: str = "") -> dict[str, str]:
    return {
        "code": code,
        "severity": "blocking",
        "source": source,
        "detail": detail,
        "repair_action": "restore an enabled manifest-derived continuous capture owner and fresh source coverage",
    }


def _active_specs(
    manifest: Mapping[str, Any],
) -> tuple[dict[str, Mapping[str, Any]], list[dict[str, str]]]:
    findings: list[dict[str, str]] = []
    specs: dict[str, Mapping[str, Any]] = {}
    sources = manifest.get("sources")
    if not isinstance(sources, list):
        return specs, [_finding("manifest_sources_missing", detail="sources must be a list")]
    for source in sources:
        item = _mapping(source)
        role = item.get("role")
        name = _text(item.get("name"))
        if role not in {"host_agent", "ingestion_only", "retired"}:
            findings.append(_finding("manifest_source_role_invalid", name))
            continue
        if role == "retired":
            continue
        if not name or name in specs:
            findings.append(_finding("manifest_active_source_invalid", name))
            continue
        specs[name] = item
    if not specs:
        findings.append(_finding("manifest_active_source_missing"))
    return specs, findings


def _owner_contract_findings(source_name: str, spec: Mapping[str, Any]) -> list[dict[str, str]]:
    continuous = _mapping(spec.get("continuous"))
    findings: list[dict[str, str]] = []
    if continuous.get("enabled") is not True:
        findings.append(_finding("owner_disabled", source_name))
    if continuous.get("owner") != EXPECTED_OWNER:
        findings.append(_finding("owner_unknown", source_name, detail="manifest owner"))
    if continuous.get("service") != EXPECTED_SERVICE:
        findings.append(_finding("owner_unknown", source_name, detail="manifest service"))
    if continuous.get("activation_key") != EXPECTED_ACTIVATION_KEY:
        findings.append(_finding("owner_unknown", source_name, detail="activation key"))
    poll = continuous.get("poll_interval_seconds")
    sla = continuous.get("max_latency_seconds")
    if (
        not isinstance(poll, int)
        or isinstance(poll, bool)
        or poll <= 0
        or not isinstance(sla, int)
        or isinstance(sla, bool)
        or sla < poll
    ):
        findings.append(_finding("owner_sla_invalid", source_name))
    return findings


def _cursor_contract_findings(source_name: str, entry: Mapping[str, Any]) -> list[dict[str, str]]:
    """Reject bounded historical scans from masquerading as continuous coverage."""
    cursor = _mapping(entry.get("cursor"))
    if cursor.get("kind") != EXPECTED_CURSOR_KIND:
        return [_finding("cursor_contract_invalid", source_name, detail="cursor kind")]
    findings: list[dict[str, str]] = []
    for key in (
        "tail_sessions_per_source",
        "reconciliation_sessions_per_source",
        "turns_per_session",
        "discovered_sessions",
        "reconciliation_selected_sessions",
        "tail_selected_sessions",
        "raw_committed_turns",
        "advanced_sessions",
        "denominator_observed_sessions",
        "denominator_turns",
    ):
        value = cursor.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            findings.append(_finding("cursor_contract_invalid", source_name, detail=key))
    for key in (
        "tail_sessions_per_source",
        "reconciliation_sessions_per_source",
        "turns_per_session",
    ):
        if cursor.get(key) == 0:
            findings.append(_finding("cursor_contract_invalid", source_name, detail=key))
    if not isinstance(cursor.get("denominator_complete"), bool):
        findings.append(
            _finding("cursor_contract_invalid", source_name, detail="denominator_complete")
        )
    completed_at = cursor.get("denominator_completed_at")
    if not isinstance(completed_at, str):
        findings.append(
            _finding("cursor_contract_invalid", source_name, detail="denominator_completed_at")
        )
    if cursor.get("denominator_complete") is True:
        if cursor.get("denominator_observed_sessions") != cursor.get("discovered_sessions"):
            findings.append(
                _finding(
                    "cursor_contract_invalid", source_name, detail="denominator_observed_sessions"
                )
            )
        if not completed_at:
            findings.append(
                _finding("cursor_contract_invalid", source_name, detail="denominator_completed_at")
            )
    if not _text(cursor.get("capture_generation_id")):
        findings.append(
            _finding(
                "cursor_contract_invalid",
                source_name,
                detail="capture_generation_id",
            )
        )
    if not isinstance(cursor.get("capture_generation_eligible"), bool):
        findings.append(
            _finding(
                "cursor_contract_invalid",
                source_name,
                detail="capture_generation_eligible",
            )
        )
    for key in CAPTURE_CURSOR_HASH_FIELDS:
        if not _sha256_text(cursor.get(key)):
            findings.append(_finding("cursor_contract_invalid", source_name, detail=key))
    for key in CAPTURE_CURSOR_COUNT_FIELDS:
        value = cursor.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            findings.append(_finding("cursor_contract_invalid", source_name, detail=key))
    if all(
        isinstance(cursor.get(key), int) and not isinstance(cursor.get(key), bool)
        for key in CAPTURE_CURSOR_COUNT_FIELDS
    ):
        if (
            cursor["capture_exact_receipt_count"] + cursor["capture_pending_turn_count"]
            != cursor["capture_expected_turn_count"]
            or cursor["capture_exact_receipt_count"] > cursor["capture_receipt_count"]
            or cursor["capture_orphan_receipt_count"] > cursor["capture_receipt_count"]
            or cursor["capture_expected_turn_count"] != cursor.get("denominator_turns")
        ):
            findings.append(
                _finding(
                    "cursor_contract_invalid",
                    source_name,
                    detail="capture_set_conservation",
                )
            )
    return findings


def audit_agent_source_coverage(
    *,
    manifest_path: Path = MANIFEST_PATH,
    config: Any,
    heartbeat_path: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a fail-closed source coverage report without mutating any state."""
    findings: list[dict[str, str]] = []
    manifest, manifest_error = _load_json(manifest_path)
    if manifest_error:
        findings.append(_finding("manifest_unreadable", detail=manifest_error))
    active_specs, active_findings = _active_specs(manifest)
    findings.extend(active_findings)

    heartbeat, heartbeat_error = _load_json(heartbeat_path)
    if heartbeat_error:
        findings.append(_finding("heartbeat_unreadable", detail=heartbeat_error))
    services = _mapping(heartbeat.get("services"))
    raw_sync = _mapping(services.get(EXPECTED_SERVICE))
    coverage = _mapping(raw_sync.get("source_coverage"))
    coverage_sources = _mapping(coverage.get("sources"))
    expected_source_names = set(active_specs)
    observed_source_names = {str(source_name) for source_name in coverage_sources}
    for source_name in sorted(expected_source_names ^ observed_source_names):
        findings.append(_finding("coverage_source_set_mismatch", source_name))
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    owner_enabled = bool(_config_value(config, EXPECTED_ACTIVATION_KEY, False))
    if not owner_enabled:
        findings.append(_finding("scheduled_owner_disabled", detail=EXPECTED_ACTIVATION_KEY))
    if coverage.get("schema_version") != COVERAGE_SCHEMA_VERSION:
        findings.append(_finding("coverage_schema_missing"))
    manifest_hash = _canonical_hash(manifest) if manifest else ""
    if coverage.get("support_manifest_hash") != manifest_hash:
        findings.append(_finding("coverage_manifest_hash_mismatch"))
    host_agent_count = sum(1 for spec in active_specs.values() if spec.get("role") == "host_agent")
    ingestion_only_source_count = sum(
        1 for spec in active_specs.values() if spec.get("role") == "ingestion_only"
    )
    for code, actual, expected in (
        ("active_source_count_mismatch", len(active_specs), EXPECTED_ACTIVE_SOURCE_COUNT),
        ("host_agent_count_mismatch", host_agent_count, EXPECTED_HOST_AGENT_COUNT),
        (
            "ingestion_only_source_count_mismatch",
            ingestion_only_source_count,
            EXPECTED_INGESTION_ONLY_SOURCE_COUNT,
        ),
    ):
        if actual != expected:
            findings.append(_finding(code, detail=f"expected={expected},actual={actual}"))

    database_dir_value = getattr(config, "database_dir", heartbeat_path.parent)
    database_dir = Path(database_dir_value)
    configured_raw_path = _config_value(config, "raw_event_store.db_path")
    raw_db_path = (
        Path(configured_raw_path).expanduser()
        if isinstance(configured_raw_path, (str, Path)) and str(configured_raw_path)
        else database_dir / "raw_events.db"
    )
    cursor_db_path = database_dir / "agent_sync_cursors.db"

    source_status: dict[str, dict[str, Any]] = {}
    missing_native_turns: list[str] = []
    owner_unknown: list[str] = []
    silent_skip: list[str] = []
    stale_sources: list[str] = []
    denominator_pending: list[str] = []

    for source_name, spec in sorted(active_specs.items()):
        contract_findings = _owner_contract_findings(source_name, spec)
        findings.extend(contract_findings)
        if contract_findings:
            owner_unknown.append(source_name)
        continuous = _mapping(spec.get("continuous"))
        entry = _mapping(coverage_sources.get(source_name))
        if not entry:
            findings.append(_finding("silent_skip", source_name, detail="coverage entry missing"))
            silent_skip.append(source_name)
            source_status[source_name] = {
                "source_role": _text(spec.get("role")),
                "status": "missing",
                "raw_capture_verified": False,
                "raw_capture_errors": ["source_coverage_missing"],
            }
            continue
        expected_owner = _text(continuous.get("owner"))
        expected_service = _text(continuous.get("service"))
        if entry.get("owner") != expected_owner or entry.get("owner_service") != expected_service:
            findings.append(
                _finding("owner_unknown", source_name, detail="heartbeat owner mismatch")
            )
            owner_unknown.append(source_name)
        findings.extend(_cursor_contract_findings(source_name, entry))
        cursor = _mapping(entry.get("cursor"))
        if cursor.get("denominator_complete") is not True:
            findings.append(_finding("denominator_reconciliation_pending", source_name))
            denominator_pending.append(source_name)
        last_discovery = _parse_timestamp(entry.get("last_discovery_at"))
        max_latency = continuous.get("max_latency_seconds")
        if last_discovery is None:
            findings.append(_finding("silent_skip", source_name, detail="last discovery missing"))
            silent_skip.append(source_name)
        elif isinstance(max_latency, int) and not isinstance(max_latency, bool):
            if (now_utc - last_discovery).total_seconds() > max_latency:
                findings.append(_finding("discovery_stale", source_name))
                stale_sources.append(source_name)
        native_turns = entry.get("native_turns")
        if not isinstance(native_turns, int) or isinstance(native_turns, bool) or native_turns <= 0:
            findings.append(_finding("missing_native_turns", source_name))
            missing_native_turns.append(source_name)
        if not _text(entry.get("last_capture_at")):
            findings.append(_finding("capture_missing", source_name))
        if _text(entry.get("error")):
            findings.append(_finding("source_error", source_name, detail=_text(entry.get("error"))))
        if not _sha256_text(entry.get("native_source_snapshot_hash")):
            findings.append(_finding("native_source_snapshot_missing", source_name))
        try:
            from core.agent_kit.source_capture_verification import verify_source_capture

            capture_evidence = verify_source_capture(
                source_name=source_name,
                coverage=coverage,
                cursor_db_path=cursor_db_path,
                raw_db_path=raw_db_path,
            )
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
        ) as exc:
            capture_evidence = {"ok": False, "errors": [exc.__class__.__name__]}
        raw_capture_errors = capture_evidence.get("errors")
        if not isinstance(raw_capture_errors, list):
            raw_capture_errors = ["capture_evidence_malformed"]
        raw_capture_verified = capture_evidence.get("ok") is True
        if not raw_capture_verified:
            findings.append(
                _finding(
                    "raw_capture_unverified",
                    source_name,
                    detail=",".join(str(error) for error in raw_capture_errors),
                )
            )
        source_status[source_name] = {
            "source_role": _text(spec.get("role")),
            "status": _text(entry.get("status")),
            "gap": _text(entry.get("gap")),
            "last_discovery_at": _text(entry.get("last_discovery_at")),
            "last_capture_at": _text(entry.get("last_capture_at")),
            "native_turns": native_turns if isinstance(native_turns, int) else None,
            "denominator_complete": cursor.get("denominator_complete") is True,
            "denominator_observed_sessions": cursor.get("denominator_observed_sessions"),
            "denominator_turns": cursor.get("denominator_turns"),
            "native_source_snapshot_hash": _text(entry.get("native_source_snapshot_hash")),
            "raw_capture_verified": raw_capture_verified,
            "raw_capture_errors": [str(error) for error in raw_capture_errors],
        }

    # Stable output makes fail-closed facts usable as a release gate input.
    unique_findings = list(
        {(item["code"], item["source"], item["detail"]): item for item in findings}.values()
    )
    unique_findings.sort(key=lambda item: (item["code"], item["source"], item["detail"]))
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "ok": not unique_findings,
        "strict_ok": not unique_findings,
        "certifying": False,
        "release_eligible": False,
        "manifest_path": str(manifest_path),
        "heartbeat_path": str(heartbeat_path),
        "active_source_count": len(active_specs),
        "host_agent_count": host_agent_count,
        "ingestion_only_source_count": ingestion_only_source_count,
        "enabled_owner_count": host_agent_count if owner_enabled else 0,
        "active_enabled_owner_count": len(active_specs) if owner_enabled else 0,
        "missing_native_turns": sorted(set(missing_native_turns)),
        "owner_unknown": sorted(set(owner_unknown)),
        "silent_skip": sorted(set(silent_skip)),
        "stale_sources": sorted(set(stale_sources)),
        "denominator_pending": sorted(set(denominator_pending)),
        "source_status": source_status,
        "findings": unique_findings,
    }


def _load_read_only_config(config_path: Path | None) -> tuple[Any | None, Path | None, str]:
    sys.path.insert(0, str(PROJECT_ROOT))
    from core.config import Config

    try:
        config = Config(config_path=config_path, provision=False)
    except (  # Report config failure as audit evidence, never provision it.
        OSError,
        ValueError,
        TypeError,
        KeyError,
        ImportError,
        AttributeError,
        RuntimeError,
    ) as exc:
        return None, config_path, exc.__class__.__name__
    return config, config.config_path, ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--heartbeat", type=Path)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    config, config_path, config_error = _load_read_only_config(args.config)
    if config is None:
        report = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "ok": False,
            "strict_ok": False,
            "certifying": False,
            "release_eligible": False,
            "manifest_path": str(args.manifest),
            "heartbeat_path": str(args.heartbeat or ""),
            "active_source_count": 0,
            "host_agent_count": 0,
            "ingestion_only_source_count": 0,
            "enabled_owner_count": 0,
            "active_enabled_owner_count": 0,
            "missing_native_turns": [],
            "owner_unknown": [],
            "silent_skip": [],
            "stale_sources": [],
            "denominator_pending": [],
            "source_status": {},
            "findings": [_finding("config_unreadable", detail=config_error)],
        }
    else:
        heartbeat_path = args.heartbeat or Path(config.database_dir) / "daemon_heartbeat.json"
        report = audit_agent_source_coverage(
            manifest_path=args.manifest,
            config=config,
            heartbeat_path=heartbeat_path,
        )
        report["config_path"] = str(config_path or "")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        raw_findings = report.get("findings", [])
        finding_count = len(raw_findings) if isinstance(raw_findings, list) else 0
        print(
            f"Agent source coverage: {'OK' if report['ok'] else 'BLOCKED'} "
            f"({report['active_source_count']} active sources, {finding_count} findings)"
        )
    return 0 if not args.strict or report["strict_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
