# -*- coding: utf-8 -*-
"""Tests for manifest-owned continuous AgentSource coverage state."""

from __future__ import annotations

import json
import stat
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.agent_kit.source_support_manifest import get_agent_source_support_manifest
from daemon import heartbeat
from daemon.agent_source_coverage import (
    SOURCE_COVERAGE_SCHEMA_VERSION,
    SourceCoverageStateError,
    coverage_state_path,
    initialize_source_coverage,
    load_source_coverage_state,
    record_source_observation,
    source_coverage_for_heartbeat,
    write_source_coverage_state,
)
from scripts.audit_agent_source_coverage import audit_agent_source_coverage


class _Config:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def get(self, key, default=None):
        if key == "daemon.services.raw_sync":
            return self.enabled
        return default


def _captured_coverage(observed_at: str) -> dict:
    manifest = get_agent_source_support_manifest()
    coverage = initialize_source_coverage(manifest, observed_at=observed_at)
    for source_name in manifest.active_source_names:
        record_source_observation(
            coverage,
            source_name,
            observed_at=observed_at,
            cursor={
                "kind": "continuous_tail_reconcile_v1",
                "tail_sessions_per_source": 10,
                "reconciliation_sessions_per_source": 10,
                "turns_per_session": 100,
                "discovered_sessions": 1,
                "reconciliation_selected_sessions": 1,
                "tail_selected_sessions": 1,
                "raw_committed_turns": 2,
                "advanced_sessions": 1,
                "denominator_complete": True,
                "denominator_observed_sessions": 1,
                "denominator_turns": 2,
                "denominator_completed_at": observed_at,
                "capture_generation_id": "capture-gen-test",
                "capture_roster_hash": "b" * 64,
                "capture_generation_eligible": True,
                "capture_expected_turn_count": 2,
                "capture_receipt_count": 2,
                "capture_exact_receipt_count": 2,
                "capture_pending_turn_count": 0,
                "capture_orphan_receipt_count": 0,
                "capture_denominator_session_set_hash": "c" * 64,
                "capture_expected_turn_fingerprint_set_hash": "d" * 64,
                "capture_receipt_binding_set_hash": "e" * 64,
                "source_path": "/must-not-persist",
            },
            native_sessions=1,
            native_turns=2,
            captured_sessions=1,
            native_source_snapshot_hash="a" * 64,
        )
    return cast(dict[str, Any], coverage)


def test_source_coverage_persists_only_safe_cursor_data(tmp_path):
    observed_at = "2026-07-12T12:00:00+00:00"
    coverage = _captured_coverage(observed_at)
    codex = coverage["sources"]["codex"]

    assert codex["status"] == "captured"
    assert codex["last_capture_at"] == observed_at
    assert codex["cursor"] == {
        "kind": "continuous_tail_reconcile_v1",
        "tail_sessions_per_source": 10,
        "reconciliation_sessions_per_source": 10,
        "turns_per_session": 100,
        "discovered_sessions": 1,
        "reconciliation_selected_sessions": 1,
        "tail_selected_sessions": 1,
        "raw_committed_turns": 2,
        "advanced_sessions": 1,
        "denominator_complete": True,
        "denominator_observed_sessions": 1,
        "denominator_turns": 2,
        "denominator_completed_at": observed_at,
        "capture_generation_id": "capture-gen-test",
        "capture_roster_hash": "b" * 64,
        "capture_generation_eligible": True,
        "capture_expected_turn_count": 2,
        "capture_receipt_count": 2,
        "capture_exact_receipt_count": 2,
        "capture_pending_turn_count": 0,
        "capture_orphan_receipt_count": 0,
        "capture_denominator_session_set_hash": "c" * 64,
        "capture_expected_turn_fingerprint_set_hash": "d" * 64,
        "capture_receipt_binding_set_hash": "e" * 64,
    }
    assert "source_path" not in json.dumps(coverage)

    path = coverage_state_path(tmp_path)
    write_source_coverage_state(path, coverage)

    assert load_source_coverage_state(path) == coverage
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_existing_invalid_coverage_never_becomes_empty_state(tmp_path):
    invalid_payloads = (
        ("not-json", "source_coverage_state_malformed"),
        ("[]", "source_coverage_state_not_object"),
        (
            '{"schema_version": "wrong", "sources": {}}',
            "source_coverage_schema_unsupported",
        ),
        (
            json.dumps(
                {
                    "schema_version": SOURCE_COVERAGE_SCHEMA_VERSION,
                    "sources": [],
                }
            ),
            "source_coverage_sources_invalid",
        ),
    )
    for index, (payload, code) in enumerate(invalid_payloads):
        path = tmp_path / f"coverage-{index}.json"
        path.write_text(payload, encoding="utf-8")
        try:
            load_source_coverage_state(path)
        except SourceCoverageStateError as exc:
            assert exc.code == code
        else:
            raise AssertionError(f"{code} was folded into an empty coverage state")


def test_missing_coverage_is_the_only_empty_state(tmp_path):
    assert load_source_coverage_state(tmp_path / "missing.json") == {}


def test_coverage_symlink_is_unavailable_not_missing(tmp_path):
    target = tmp_path / "missing-target.json"
    link = tmp_path / "coverage.json"
    link.symlink_to(target)

    try:
        load_source_coverage_state(link)
    except SourceCoverageStateError as exc:
        assert exc.code == "source_coverage_state_symlink"
    else:
        raise AssertionError("coverage symlink was folded into a missing state")


def test_uninspectable_coverage_is_unavailable_not_missing(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "coverage.json"
    original_stat = Path.stat

    def guarded_stat(path: Path, *args: object, **kwargs: object):
        if path == target:
            raise PermissionError("coverage metadata unavailable")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", guarded_stat)

    with pytest.raises(
        SourceCoverageStateError,
        match="source_coverage_state_unreadable",
    ):
        load_source_coverage_state(target)


@pytest.mark.parametrize(
    ("coverage", "code"),
    [
        ({}, "source_coverage_heartbeat_schema_unsupported"),
        (
            {
                "schema_version": SOURCE_COVERAGE_SCHEMA_VERSION,
                "sources": [],
            },
            "source_coverage_heartbeat_sources_invalid",
        ),
        (
            {
                "schema_version": SOURCE_COVERAGE_SCHEMA_VERSION,
                "sources": {"codex": "corrupt"},
            },
            "source_coverage_heartbeat_entry_invalid",
        ),
        (
            {
                "schema_version": SOURCE_COVERAGE_SCHEMA_VERSION,
                "sources": {"codex": {"cursor": "corrupt"}},
            },
            "source_coverage_heartbeat_cursor_invalid",
        ),
        (
            {
                "schema_version": SOURCE_COVERAGE_SCHEMA_VERSION,
                "sources": {"codex": {"native_sessions": -1}},
            },
            "source_coverage_heartbeat_count_invalid",
        ),
        (
            {
                "schema_version": SOURCE_COVERAGE_SCHEMA_VERSION,
                "sources": {"codex": {"status": 1}},
            },
            "source_coverage_heartbeat_text_invalid",
        ),
    ],
)
def test_invalid_heartbeat_coverage_never_becomes_empty(coverage, code):
    with pytest.raises(SourceCoverageStateError, match=code):
        source_coverage_for_heartbeat(coverage)


def test_heartbeat_rejects_partial_or_foreign_source_denominator():
    observed_at = "2026-07-12T12:00:00+00:00"
    partial = _captured_coverage(observed_at)
    partial["sources"].pop("codex")
    with pytest.raises(
        SourceCoverageStateError,
        match="source_coverage_heartbeat_denominator_incomplete",
    ):
        source_coverage_for_heartbeat(partial)

    foreign = _captured_coverage(observed_at)
    foreign["support_manifest_hash"] = "0" * 64
    with pytest.raises(
        SourceCoverageStateError,
        match="source_coverage_heartbeat_manifest_mismatch",
    ):
        source_coverage_for_heartbeat(foreign)


def test_heartbeat_projects_per_source_capture_state_without_raw_paths():
    observed_at = "2026-07-12T12:00:00+00:00"
    coverage = _captured_coverage(observed_at)
    snapshot = heartbeat.build_heartbeat_snapshot(
        instance_identity={"instance_id": "test"},
        intervals={"raw_sync": 600},
        service_results={
            "raw_sync": {
                "at": observed_at,
                "ok": True,
                "result": {"enabled": True, "synced": 8, "source_coverage": coverage},
            }
        },
        service_error_state={},
        cfg=_Config(True),
        service_enabled=lambda _cfg, _name: True,
    )

    projected = snapshot["services"]["raw_sync"]["source_coverage"]
    assert projected == source_coverage_for_heartbeat(coverage)
    assert projected["sources"]["codex"]["last_capture_at"] == observed_at
    assert "source_path" not in json.dumps(projected)


def test_heartbeat_restores_persisted_coverage_before_next_scheduled_scan():
    coverage = _captured_coverage("2026-07-12T12:00:00+00:00")
    snapshot = heartbeat.build_heartbeat_snapshot(
        instance_identity={"instance_id": "test"},
        intervals={"raw_sync": 600},
        service_results={},
        service_error_state={},
        cfg=_Config(True),
        service_enabled=lambda _cfg, _name: True,
        persisted_source_coverage=coverage,
    )

    assert (
        snapshot["services"]["raw_sync"]["source_coverage"]["sources"]["codex"]["status"]
        == "captured"
    )


def test_independent_coverage_auditor_rejects_heartbeat_without_raw_evidence(tmp_path):
    observed = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    coverage = _captured_coverage(observed.isoformat())
    heartbeat_path = tmp_path / "daemon_heartbeat.json"
    heartbeat_path.write_text(
        json.dumps(
            {
                "services": {
                    "raw_sync": {
                        "enabled": True,
                        "source_coverage": source_coverage_for_heartbeat(coverage),
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    report = audit_agent_source_coverage(
        config=_Config(True),
        heartbeat_path=heartbeat_path,
        now=observed,
    )

    assert report["ok"] is False
    assert report["active_source_count"] == 12
    assert report["host_agent_count"] == 8
    assert report["ingestion_only_source_count"] == 4
    assert report["enabled_owner_count"] == 8
    assert report["active_enabled_owner_count"] == 12
    assert report["source_status"]["codex"]["source_role"] == "host_agent"
    assert report["source_status"]["gemini"]["source_role"] == "ingestion_only"
    assert all(
        report["source_status"][source_name]["raw_capture_verified"] is False
        for source_name in get_agent_source_support_manifest().active_source_names
    )
    assert {
        item["source"] for item in report["findings"] if item["code"] == "raw_capture_unverified"
    } == set(get_agent_source_support_manifest().active_source_names)
    assert report["missing_native_turns"] == []
    assert report["owner_unknown"] == []
    assert report["silent_skip"] == []

    disabled = audit_agent_source_coverage(
        config=_Config(False),
        heartbeat_path=heartbeat_path,
        now=observed,
    )
    assert disabled["ok"] is False
    assert any(item["code"] == "scheduled_owner_disabled" for item in disabled["findings"])

    coverage["sources"]["codex"]["cursor"]["kind"] = "daemon_l1"
    invalid_heartbeat = heartbeat_path.with_name("invalid_heartbeat.json")
    invalid_heartbeat.write_text(
        json.dumps(
            {
                "services": {
                    "raw_sync": {
                        "enabled": True,
                        "source_coverage": source_coverage_for_heartbeat(coverage),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    invalid = audit_agent_source_coverage(
        config=_Config(True),
        heartbeat_path=invalid_heartbeat,
        now=observed,
    )
    assert any(item["code"] == "cursor_contract_invalid" for item in invalid["findings"])


def test_independent_coverage_auditor_rejects_legacy_or_unbound_capture_sets(
    tmp_path: Path,
) -> None:
    observed = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    coverage = _captured_coverage(observed.isoformat())
    projected = source_coverage_for_heartbeat(coverage)
    del projected["sources"]["codex"]["cursor"]["capture_expected_turn_fingerprint_set_hash"]
    projected["schema_version"] = "mnemos.agent_source_coverage.v1"
    heartbeat_path = tmp_path / "daemon_heartbeat.json"
    heartbeat_path.write_text(
        json.dumps(
            {
                "services": {
                    "raw_sync": {
                        "enabled": True,
                        "source_coverage": projected,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    report = audit_agent_source_coverage(
        config=_Config(True),
        heartbeat_path=heartbeat_path,
        now=observed,
    )

    assert report["ok"] is False
    assert any(item["code"] == "coverage_schema_missing" for item in report["findings"])
    assert any(
        item["code"] == "cursor_contract_invalid"
        and item["source"] == "codex"
        and item["detail"] == "capture_expected_turn_fingerprint_set_hash"
        for item in report["findings"]
    )


def test_independent_coverage_auditor_rejects_rogue_heartbeat_source(tmp_path):
    observed = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    coverage = _captured_coverage(observed.isoformat())
    projected = source_coverage_for_heartbeat(coverage)
    projected["sources"]["rogue"] = dict(projected["sources"]["codex"])
    heartbeat_path = tmp_path / "daemon_heartbeat.json"
    heartbeat_path.write_text(
        json.dumps(
            {
                "services": {
                    "raw_sync": {
                        "enabled": True,
                        "source_coverage": projected,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    report = audit_agent_source_coverage(
        config=_Config(True),
        heartbeat_path=heartbeat_path,
        now=observed,
    )

    assert any(
        item["code"] == "coverage_source_set_mismatch" and item["source"] == "rogue"
        for item in report["findings"]
    )


def test_independent_coverage_auditor_rejects_silent_ingestion_only_omission(tmp_path):
    observed = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    coverage = _captured_coverage(observed.isoformat())
    projected = source_coverage_for_heartbeat(coverage)
    del projected["sources"]["gemini"]
    heartbeat_path = tmp_path / "daemon_heartbeat.json"
    heartbeat_path.write_text(
        json.dumps(
            {
                "services": {
                    "raw_sync": {
                        "enabled": True,
                        "source_coverage": projected,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    report = audit_agent_source_coverage(
        config=_Config(True),
        heartbeat_path=heartbeat_path,
        now=observed,
    )

    assert report["active_source_count"] == 12
    assert report["host_agent_count"] == 8
    assert report["ingestion_only_source_count"] == 4
    assert report["ok"] is False
    assert "gemini" in report["silent_skip"]


def test_independent_coverage_auditor_rejects_unknown_manifest_role(tmp_path):
    canonical_manifest = (
        Path(__file__).resolve().parents[2]
        / "core"
        / "agent_kit"
        / "agent_source_support_manifest.json"
    )
    manifest = json.loads(canonical_manifest.read_text(encoding="utf-8"))
    next(item for item in manifest["sources"] if item["name"] == "gemini")["role"] = "unknown"
    manifest_path = tmp_path / "agent_source_support_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    heartbeat_path = tmp_path / "daemon_heartbeat.json"
    heartbeat_path.write_text("{}", encoding="utf-8")

    report = audit_agent_source_coverage(
        manifest_path=manifest_path,
        config=_Config(True),
        heartbeat_path=heartbeat_path,
    )

    assert any(
        item["code"] == "manifest_source_role_invalid" and item["source"] == "gemini"
        for item in report["findings"]
    )


def test_independent_coverage_auditor_rejects_shrunk_manifest_denominator(tmp_path):
    canonical_manifest = (
        Path(__file__).resolve().parents[2]
        / "core"
        / "agent_kit"
        / "agent_source_support_manifest.json"
    )
    manifest = json.loads(canonical_manifest.read_text(encoding="utf-8"))
    manifest["sources"] = [item for item in manifest["sources"] if item["name"] != "gemini"]
    manifest_path = tmp_path / "agent_source_support_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    heartbeat_path = tmp_path / "daemon_heartbeat.json"
    heartbeat_path.write_text("{}", encoding="utf-8")

    report = audit_agent_source_coverage(
        manifest_path=manifest_path,
        config=_Config(True),
        heartbeat_path=heartbeat_path,
    )

    assert any(item["code"] == "active_source_count_mismatch" for item in report["findings"])


def test_daemon_service_persists_coverage_before_structural_report(monkeypatch, tmp_path):
    import mnemos_daemon as daemon

    coverage = _captured_coverage("2026-07-12T12:00:00+00:00")
    fake_config = SimpleNamespace(database_dir=tmp_path)
    observed: dict[str, object] = {}

    def fake_run_service(_log_error, **kwargs):
        observed["previous"] = kwargs["previous_source_coverage"]
        kwargs["coverage_state_sink"](coverage)
        return {"source_coverage": coverage, "report_hash": "structural"}

    monkeypatch.setattr("core.config.get_config", lambda: fake_config)
    monkeypatch.setattr(daemon._raw_sync, "run_service", fake_run_service)

    result = daemon.service_raw_sync()

    assert observed["previous"] == {}
    assert result["report_hash"] == "structural"
    assert load_source_coverage_state(coverage_state_path(tmp_path)) == coverage


def test_latest_phase1_revalidation_owns_current_schema_inventory_generation() -> None:
    from scripts import generate_phase0_governance_contracts as governance

    phase1 = json.loads(governance.PHASE1_LEDGER_PATH.read_text(encoding="utf-8"))
    record = phase1[governance.PHASE1_REVALIDATION_SEQUENCE[-1][1]]
    schema_path = governance.ACCEPTANCE / "schema_owner_manifest.json"

    assert record["root_id"] == governance.PHASE1_REVALIDATION_SEQUENCE[-1][0]
    assert governance.hashlib.sha256(schema_path.read_bytes()).hexdigest() == (
        record["governance_revalidation"]["schema_inventory"]["sha256"]
    )
    boundary = record["closure_boundary"]
    assert boundary["code_contract_verified"] is True
    assert boundary["next_root_started"] is False
    assert boundary["production_effect"] == "not verified"
    assert isinstance(boundary["production_mutation"], str)
    assert boundary["production_mutation"]
    assert boundary["live_snapshot_raw_rebuilt"] is False
    assert boundary["post_apply_zero_gap_verified"] is False
    assert boundary["readiness_certified"] is False
    assert boundary["release_eligible"] is False
    assert boundary["root_closed"] is False
    # Per-root closure boundary schemas renamed this key from
    # ``native_history_read`` (COG-045/COG-001) to
    # ``current_native_history_read`` (COG-002 onward); the latest record must
    # expose exactly one of them and it must remain fail-closed.
    native_history_keys = [
        key for key in ("current_native_history_read", "native_history_read") if key in boundary
    ]
    assert len(native_history_keys) == 1
    if boundary[native_history_keys[0]] is True:
        assert boundary["live_cursor_schema_migrated"] is True
        assert boundary["production_mutation"].startswith("authorized;")
    else:
        assert boundary["production_mutation"] == ("not authorized and not performed")
    assert boundary["readiness_certified"] is False
    assert boundary["release_eligible"] is False
    assert boundary["root_closed"] is False


def test_governance_rejects_cog045_schema_inventory_overclaim(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from scripts import generate_phase0_governance_contracts as governance

    phase1 = json.loads(governance.PHASE1_LEDGER_PATH.read_text(encoding="utf-8"))
    phase1["phase1_cog045_contract_revalidation_20260725"]["closure_boundary"]["root_closed"] = True
    phase1_path = tmp_path / "overclaim-cog045-ledger.json"
    phase1_path.write_text(json.dumps(phase1), encoding="utf-8")
    monkeypatch.setattr(governance, "PHASE1_LEDGER_PATH", phase1_path)

    assert "COG-045 contract revalidation closure boundary overclaim" in (
        governance.validate_assets()
    )
