from __future__ import annotations

import json
import sqlite3
import hashlib
from itertools import permutations
from pathlib import Path
import subprocess
import sys

import pytest

from core.sync_framework.agent_source import SessionInfo, Turn
from core.sync_framework.raw_event_store import RawEventStore
from scripts.audit_agent_source_support_manifest import (
    MANIFEST_RELATIVE_PATH,
    PROJECT_ROOT,
    audit_agent_source_support_manifest,
)
from scripts.backfill_raw_event_store import _backfill_turn


class _Cfg:
    def __init__(self, database_dir: Path):
        self.database_dir = database_dir

    def get(self, _key: str, default=None):
        return default


def _manifest_payload() -> dict:
    return json.loads((PROJECT_ROOT / MANIFEST_RELATIVE_PATH).read_text(encoding="utf-8"))


def _write_manifest(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "agent_source_support_manifest.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_support_manifest_loader_never_follows_a_leaf_symlink(
    tmp_path: Path,
) -> None:
    from core.agent_kit.source_support_manifest import (
        AgentSourceSupportManifestError,
        load_agent_source_support_manifest,
    )

    target = _write_manifest(tmp_path, _manifest_payload())
    alias = tmp_path / "manifest-alias.json"
    alias.symlink_to(target)

    with pytest.raises(
        AgentSourceSupportManifestError,
        match="support manifest is unavailable",
    ):
        load_agent_source_support_manifest(alias)


@pytest.mark.parametrize(
    "module_order",
    tuple(
        permutations(
            (
                "core.agent_kit.source_support_manifest",
                "core.config",
                "core.sync_framework",
            )
        )
    ),
)
def test_config_manifest_and_sync_modules_import_in_any_fresh_process_order(
    module_order: tuple[str, ...],
) -> None:
    import_script = (
        "import importlib\n"
        f"modules = {module_order!r}\n"
        "for module in modules:\n"
        "    importlib.import_module(module)\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", import_script],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_agent_source_support_manifest_audit_is_clean():
    report = audit_agent_source_support_manifest()

    assert report["ok"] is True, report["findings"]
    assert report["source_definition_owner_count"] == 1
    assert report["builtin_source_count"] == 12
    assert report["manifest_source_count"] == 12
    assert len(report["host_agents"]) == 8
    assert len(report["ingestion_only_sources"]) == 4
    assert report["builtin_source_without_manifest"] == 0
    assert report["manifest_without_parser"] == 0
    assert report["installed_source_silently_ignored"] == 0
    assert report["retired_source_callers"] == 0
    assert report["runtime_evidence_collected"] is False
    assert report["support_snapshot_manifest_mismatch"] is None
    assert report["receipt_without_support_manifest_hash"] is None


def test_support_audit_rejects_runtime_receipt_without_raw_canary_binding():
    relative = "core/agent_kit/runtime_receipts.py"
    original = (PROJECT_ROOT / relative).read_text(encoding="utf-8")

    report = audit_agent_source_support_manifest(
        code_overrides={
            relative: original.replace(
                "runtime_canary_raw_revision_ids_hash",
                "removed_raw_binding",
            ).replace(
                "runtime_receipt_id_hash",
                "removed_receipt_binding",
            )
        }
    )

    assert any(
        finding["code"] == "runtime_receipt_contract_missing" for finding in report["findings"]
    )


def test_manifest_mutations_fail_bidirectional_parser_and_role_contract(tmp_path: Path):
    missing = _manifest_payload()
    missing["sources"] = [source for source in missing["sources"] if source["name"] != "aider"]
    missing_report = audit_agent_source_support_manifest(
        manifest_path=_write_manifest(tmp_path, missing)
    )
    assert missing_report["builtin_source_without_manifest"] == 1

    parser_missing = _manifest_payload()
    parser_missing["sources"][0]["parser"]["class"] = "MissingCodexSource"
    parser_report = audit_agent_source_support_manifest(
        manifest_path=_write_manifest(tmp_path, parser_missing)
    )
    assert parser_report["manifest_without_parser"] >= 1

    role_confusion = _manifest_payload()
    role_confusion["sources"][0]["role"] = "ingestion_only"
    role_report = audit_agent_source_support_manifest(
        manifest_path=_write_manifest(tmp_path, role_confusion)
    )
    assert any(finding["code"] == "support_manifest_role" for finding in role_report["findings"])
    assert any(
        finding["code"] == "host_agent_denominator_mismatch" for finding in role_report["findings"]
    )


def test_manifest_rejects_active_source_without_continuous_owner_contract(tmp_path: Path):
    from core.agent_kit.source_support_manifest import (
        AgentSourceSupportManifestError,
        load_agent_source_support_manifest,
    )

    payload = _manifest_payload()
    payload["sources"][0]["continuous"].pop("owner")

    with pytest.raises(AgentSourceSupportManifestError, match="continuous.owner is required"):
        load_agent_source_support_manifest(_write_manifest(tmp_path, payload))

    report = audit_agent_source_support_manifest(manifest_path=_write_manifest(tmp_path, payload))
    assert any(
        finding["code"] == "continuous_capture_owner_invalid" for finding in report["findings"]
    )


def test_claude_recursive_discovery_contract_is_manifest_owned_and_validated(
    tmp_path: Path,
):
    from core.agent_kit.source_support_manifest import (
        AgentSourceSupportManifestError,
        load_agent_source_support_manifest,
    )

    payload = _manifest_payload()
    claude = next(source for source in payload["sources"] if source["name"] == "claude")
    assert claude["native"]["formats"] == ["projects/**/*.jsonl"]
    assert claude["native"]["root_resolver"]["transcript_subdir"] == "projects"

    claude["native"]["formats"] = ["projects/*/*.jsonl"]
    manifest_path = _write_manifest(tmp_path, payload)
    with pytest.raises(
        AgentSourceSupportManifestError,
        match="claude.native.formats must declare recursive projects JSONL",
    ):
        load_agent_source_support_manifest(manifest_path)

    report = audit_agent_source_support_manifest(manifest_path=manifest_path)
    assert any(
        finding["code"] == "claude_recursive_discovery_contract_invalid"
        for finding in report["findings"]
    )


def test_crush_multi_root_contract_is_manifest_owned_and_validated(tmp_path: Path):
    from core.agent_kit.source_support_manifest import (
        AgentSourceSupportManifestError,
        load_agent_source_support_manifest,
    )

    payload = _manifest_payload()
    crush = next(source for source in payload["sources"] if source["name"] == "crush")
    assert crush["native"]["formats"] == ["crush.db"]
    assert crush["native"]["root_resolver"]["multi_root"] == {
        "mode": "all_valid",
        "project_ancestor_search": True,
    }

    crush["native"]["root_resolver"]["multi_root"]["mode"] = "first_valid"
    manifest_path = _write_manifest(tmp_path, payload)
    with pytest.raises(
        AgentSourceSupportManifestError,
        match="crush multi-root contract must enumerate all valid roots",
    ):
        load_agent_source_support_manifest(manifest_path)

    report = audit_agent_source_support_manifest(manifest_path=manifest_path)
    assert any(
        finding["code"] == "crush_multi_root_contract_invalid" for finding in report["findings"]
    )


def test_openclaw_multi_format_resolution_is_manifest_owned_and_validated(tmp_path: Path):
    from core.agent_kit.source_support_manifest import (
        AgentSourceSupportManifestError,
        load_agent_source_support_manifest,
    )

    payload = _manifest_payload()
    openclaw = next(source for source in payload["sources"] if source["name"] == "openclaw")
    resolution = openclaw["native"]["format_resolution"]
    assert {item["source_kind"] for item in resolution["variants"]} == {
        "trajectory",
        "normal_jsonl",
        "corpus",
    }
    assert resolution["extension_rule"] == "longest_turn_fingerprint_prefix"
    assert resolution["conflict_rule"] == "opaque_artifact_identity"

    resolution["variants"][1]["priority"] = 0
    with pytest.raises(AgentSourceSupportManifestError, match="priority must be positive"):
        load_agent_source_support_manifest(_write_manifest(tmp_path, payload))

    report = audit_agent_source_support_manifest(manifest_path=_write_manifest(tmp_path, payload))
    assert any(
        finding["code"] == "support_manifest_schema" and "priority" in finding["message"]
        for finding in report["findings"]
    )

    payload = _manifest_payload()
    openclaw = next(source for source in payload["sources"] if source["name"] == "openclaw")
    resolution = openclaw["native"]["format_resolution"]
    resolution["variants"][0]["path_glob"] = "workspace/**/*.txt"
    with pytest.raises(
        AgentSourceSupportManifestError,
        match="openclaw format path contract is invalid",
    ):
        load_agent_source_support_manifest(_write_manifest(tmp_path, payload))
    report = audit_agent_source_support_manifest(manifest_path=_write_manifest(tmp_path, payload))
    assert any(
        "openclaw: invalid format path contract" in finding["message"]
        for finding in report["findings"]
    )

    payload = _manifest_payload()
    openclaw = next(source for source in payload["sources"] if source["name"] == "openclaw")
    resolution = openclaw["native"]["format_resolution"]
    resolution["variants"][2]["priority"] = 400
    with pytest.raises(
        AgentSourceSupportManifestError,
        match="openclaw format priority order is invalid",
    ):
        load_agent_source_support_manifest(_write_manifest(tmp_path, payload))


def test_kimi_artifact_resolution_is_manifest_owned_and_validated(tmp_path: Path):
    from core.agent_kit.source_support_manifest import (
        AgentSourceSupportManifestError,
        load_agent_source_support_manifest,
    )

    payload = _manifest_payload()
    kimi = next(source for source in payload["sources"] if source["name"] == "kimi")
    resolution = kimi["native"]["artifact_resolution"]
    assert {item["source_kind"] for item in resolution["variants"]} == {
        "main_context",
        "subagent_context",
        "main_wire",
        "subagent_wire",
    }
    assert (
        resolution["duplicate_event_rule"]
        == "dedupe_explicit_native_event_id_and_canonical_json_value_only"
    )
    assert resolution["conflict_rule"] == "opaque_artifact_identity"
    assert resolution["identity_contract"] == "kimi-native-artifact-v2"
    assert resolution["state_fingerprint_rule"] == "ordered_artifact_name_and_bytes_sha256"
    assert resolution["decoder_contract"] == "reversible_jsonl_v1"
    assert resolution["decoder_rejection_rule"] == (
        "preserve_invalid_utf8_json_nonobject_duplicate_key_nonfinite_"
        "surrogate_and_excessive_nesting"
    )
    assert resolution["json_value_equality_rule"] == ("number_value_equal_boolean_type_distinct")
    assert resolution["number_decode_rule"] == (
        "decimal_identity_exact_utf8_source_line_and_json_valid_runtime_wrapper"
    )
    assert resolution["migration_rule"] == "fail_closed_on_legacy_raw_overlap"
    assert resolution["legacy_identity_rule"] == ("bind_fixed_point_misclassification_aliases")
    assert resolution["timestamp_rule"] == ("preserve_invalid_numeric_timestamp_as_typed_raw")
    assert resolution["parent_identity_rule"] == "native_parent_plus_canonical_main_artifact_v2"

    resolution["variants"][2]["identity"] = "native_session_id"
    with pytest.raises(AgentSourceSupportManifestError, match="identity is invalid"):
        load_agent_source_support_manifest(_write_manifest(tmp_path, payload))

    report = audit_agent_source_support_manifest(manifest_path=_write_manifest(tmp_path, payload))
    assert any(
        finding["code"] == "support_manifest_schema" and "artifact identity" in finding["message"]
        for finding in report["findings"]
    )


def test_independent_verifier_rejects_protocol_and_registry_definition_mutations():
    protocol_relative = "core/agent_kit/protocol.py"
    protocol_text = (PROJECT_ROOT / protocol_relative).read_text(encoding="utf-8")
    expected_protocol = "TARGET_AGENT_NAMES = _MANIFEST.host_agent_names"
    assert expected_protocol in protocol_text
    protocol_report = audit_agent_source_support_manifest(
        code_overrides={
            protocol_relative: protocol_text.replace(
                expected_protocol,
                "TARGET_AGENT_NAMES = _MANIFEST.host_agent_names[:-1]",
            )
        }
    )
    assert any(
        finding["code"] == "protocol_host_list_not_manifest_derived"
        for finding in protocol_report["findings"]
    )

    registry_relative = "core/sync_framework/registry.py"
    registry_text = (PROJECT_ROOT / registry_relative).read_text(encoding="utf-8")
    expected_registry = "return get_agent_source_support_manifest().builtin_registry_specs()"
    assert expected_registry in registry_text
    registry_report = audit_agent_source_support_manifest(
        code_overrides={registry_relative: registry_text.replace(expected_registry, "return ()")}
    )
    assert any(
        finding["code"] == "registry_manifest_derivation_missing"
        for finding in registry_report["findings"]
    )

    parser_identity_guard = "source_class is not expected_class"
    assert parser_identity_guard in registry_text
    parser_identity_report = audit_agent_source_support_manifest(
        code_overrides={
            registry_relative: registry_text.replace(
                parser_identity_guard,
                "source_class is expected_class",
            )
        }
    )
    assert any(
        finding["code"] == "registry_parser_identity_guard_missing"
        for finding in parser_identity_report["findings"]
    )

    daemon_relative = "mnemos_daemon.py"
    daemon_text = (PROJECT_ROOT / daemon_relative).read_text(encoding="utf-8")
    trigger_guard = 'source_spec.continuous["trigger"]'
    assert trigger_guard in daemon_text
    trigger_report = audit_agent_source_support_manifest(
        code_overrides={
            daemon_relative: daemon_text.replace(trigger_guard, 'source.trigger_strategy["type"]')
        }
    )
    assert any(
        finding["code"] == "daemon_trigger_contract_not_manifest_bound"
        for finding in trigger_report["findings"]
    )

    ledger_relative = "core/sync_framework/native_raw_contract_ledger.py"
    ledger_text = (PROJECT_ROOT / ledger_relative).read_text(encoding="utf-8")
    assert "_sync_effective_metrics" in ledger_text
    ledger_report = audit_agent_source_support_manifest(
        code_overrides={
            ledger_relative: ledger_text.replace(
                "_sync_effective_metrics",
                "_metrics_sync_removed",
            )
        }
    )
    assert any(
        finding["code"] == "native_raw_contract_guard_missing"
        for finding in ledger_report["findings"]
    )


def test_native_source_snapshot_rejects_forged_source_capability_and_stale_manifest(tmp_path: Path):
    from core.agent_kit.source_support_manifest import (
        NativeSourceSnapshot,
        build_native_source_snapshot,
        get_agent_source_support_manifest,
        load_agent_source_support_manifest,
        validate_native_source_snapshot,
    )

    manifest = get_agent_source_support_manifest()
    with pytest.raises(TypeError):
        manifest.source("aider").payload["capability"]["source_fidelity"] = "forged"
    snapshot = build_native_source_snapshot(
        "aider",
        resolved_roots=[tmp_path / "project"],
        cursor={"kind": "backfill", "since_hours": 0},
        native_denominator={"sessions": 1, "turns": 1},
    )
    assert validate_native_source_snapshot(snapshot) == []

    forged_source = snapshot.to_dict()
    forged_source["source_name"] = "cursor"
    assert "snapshot_parser_mismatch" in validate_native_source_snapshot(
        NativeSourceSnapshot.from_dict(forged_source)
    )

    forged_capability = snapshot.to_dict()
    forged_capability["capability_contract_hash"] = "forged"
    assert "snapshot_capability_mismatch" in validate_native_source_snapshot(
        NativeSourceSnapshot.from_dict(forged_capability)
    )

    stale = snapshot.to_dict()
    stale["support_manifest_hash"] = "old-manifest"
    assert "support_manifest_hash_mismatch" in validate_native_source_snapshot(
        NativeSourceSnapshot.from_dict(stale)
    )

    retired_payload = _manifest_payload()
    retired_payload["sources"][-1]["role"] = "retired"
    retired_manifest = load_agent_source_support_manifest(
        _write_manifest(tmp_path, retired_payload)
    )
    assert "windsurf" not in retired_manifest.active_source_names

    changed_payload = _manifest_payload()
    changed_payload["sources"][0]["retention"]["policy"] = "changed_retention_policy"
    changed_manifest = load_agent_source_support_manifest(
        _write_manifest(tmp_path, changed_payload)
    )
    assert "support_manifest_hash_mismatch" in validate_native_source_snapshot(
        snapshot,
        manifest=changed_manifest,
    )


def test_continuous_snapshot_requires_exact_capture_sets_and_changes_with_content(
    tmp_path: Path,
) -> None:
    from core.agent_kit.source_support_manifest import (
        AgentSourceSupportManifestError,
        build_native_source_snapshot,
        native_source_snapshot_hash,
    )

    with pytest.raises(
        AgentSourceSupportManifestError,
        match="snapshot_capture_cursor_incomplete",
    ):
        build_native_source_snapshot(
            "codex",
            resolved_roots=[tmp_path / "codex"],
            cursor={
                "kind": "continuous_tail_reconcile_v1",
                "denominator_complete": True,
            },
            native_denominator={"sessions": 1, "turns": 1},
        )

    cursor = {
        "kind": "continuous_tail_reconcile_v1",
        "denominator_complete": True,
        "denominator_observed_sessions": 1,
        "discovered_sessions": 1,
        "denominator_turns": 1,
        "capture_generation_id": "capture-gen-test",
        "capture_roster_hash": "a" * 64,
        "capture_generation_eligible": True,
        "capture_expected_turn_count": 1,
        "capture_receipt_count": 1,
        "capture_exact_receipt_count": 1,
        "capture_pending_turn_count": 0,
        "capture_orphan_receipt_count": 0,
        "capture_denominator_session_set_hash": "b" * 64,
        "capture_expected_turn_fingerprint_set_hash": "c" * 64,
        "capture_receipt_binding_set_hash": "d" * 64,
    }
    before = build_native_source_snapshot(
        "codex",
        resolved_roots=[tmp_path / "codex"],
        cursor=cursor,
        native_denominator={"sessions": 1, "turns": 1},
    )
    changed_cursor = dict(cursor)
    changed_cursor["capture_expected_turn_fingerprint_set_hash"] = "e" * 64
    after = build_native_source_snapshot(
        "codex",
        resolved_roots=[tmp_path / "codex"],
        cursor=changed_cursor,
        native_denominator={"sessions": 1, "turns": 1},
    )

    assert native_source_snapshot_hash(before) != native_source_snapshot_hash(after)


def test_independent_auditor_rejects_invalid_continuous_capture_cursor(
    tmp_path: Path,
) -> None:
    """The external auditor must revalidate the capture cursor itself."""

    from core.agent_kit.source_support_manifest import (
        build_native_source_snapshot,
        build_source_support_runtime_report,
        get_agent_source_support_manifest,
    )

    cursor = {
        "kind": "continuous_tail_reconcile_v1",
        "denominator_complete": True,
        "denominator_observed_sessions": 1,
        "discovered_sessions": 1,
        "denominator_turns": 1,
        "capture_generation_id": "capture-gen-audit",
        "capture_roster_hash": "a" * 64,
        "capture_generation_eligible": True,
        "capture_expected_turn_count": 1,
        "capture_receipt_count": 1,
        "capture_exact_receipt_count": 1,
        "capture_pending_turn_count": 0,
        "capture_orphan_receipt_count": 0,
        "capture_denominator_session_set_hash": "b" * 64,
        "capture_expected_turn_fingerprint_set_hash": "c" * 64,
        "capture_receipt_binding_set_hash": "d" * 64,
    }
    manifest = get_agent_source_support_manifest()
    snapshot = build_native_source_snapshot(
        "codex",
        resolved_roots=[tmp_path / "codex"],
        cursor=cursor,
        native_denominator={"sessions": 1, "turns": 1},
    ).to_dict()
    evidence = build_source_support_runtime_report(
        {
            "source_snapshots": {"codex": snapshot},
            "errors": 0,
            "unmanifested_sources": [],
        },
        producer="daemon.raw_sync",
        manifest=manifest,
    )
    cases = (
        (
            lambda value: value.pop("capture_generation_id"),
            "snapshot_capture_cursor_incomplete",
        ),
        (
            lambda value: value.__setitem__(
                "capture_roster_hash",
                "not-a-sha256",
            ),
            "snapshot_capture_cursor_malformed",
        ),
        (
            lambda value: value.__setitem__(
                "capture_pending_turn_count",
                1,
            ),
            "snapshot_capture_cursor_inconsistent",
        ),
    )
    for mutate, expected_error in cases:
        mutated = dict(evidence)
        mutated["source_snapshots"] = {"codex": dict(snapshot)}
        mutated_cursor = dict(snapshot["cursor"])
        mutated["source_snapshots"]["codex"]["cursor"] = mutated_cursor
        mutate(mutated_cursor)
        mutated.pop("report_hash", None)
        rendered = json.dumps(
            mutated,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        mutated["report_hash"] = hashlib.sha256(
            rendered.encode("utf-8")
        ).hexdigest()

        report = audit_agent_source_support_manifest(runtime_evidence=mutated)

        assert report["support_snapshot_manifest_mismatch"] == 1
        assert report["ok"] is False
        assert any(
            expected_error in finding["message"]
            for finding in report["findings"]
        )


def test_independent_auditor_validates_observed_runtime_evidence(tmp_path: Path):
    from core.agent_kit.source_support_manifest import (
        AgentSourceSupportManifestError,
        build_native_source_snapshot,
        build_source_support_runtime_report,
        get_agent_source_support_manifest,
    )

    def _rechecksum(payload: dict) -> dict:
        result = dict(payload)
        result.pop("report_hash", None)
        rendered = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        result["report_hash"] = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        return result

    manifest = get_agent_source_support_manifest()
    snapshot = build_native_source_snapshot(
        "aider",
        resolved_roots=[tmp_path / "aider"],
        cursor={"kind": "backfill"},
        native_denominator={"sessions": 1, "turns": 2},
    ).to_dict()
    evidence = build_source_support_runtime_report(
        {
            "source_snapshots": {"aider": snapshot},
            "errors": 0,
            "unmanifested_sources": [],
        },
        producer="daemon.raw_sync",
        manifest=manifest,
    )
    evidence_path = tmp_path / "runtime-evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    clean = audit_agent_source_support_manifest(
        runtime_evidence=evidence_path,
        require_runtime_evidence=True,
    )
    assert clean["ok"] is True, clean["findings"]
    assert clean["support_snapshot_manifest_mismatch"] == 0
    assert clean["runtime_evidence_state"] == "structural_observation"
    assert clean["runtime_evidence_certifying"] is False
    assert clean["runtime_full_power_ok"] is None
    assert clean["receipt_without_support_manifest_hash"] is None

    forged_evidence = dict(evidence)
    forged_evidence["source_snapshots"] = {"aider": dict(snapshot)}
    forged_evidence["source_snapshots"]["aider"]["support_manifest_hash"] = "stale-manifest"
    forged_evidence = _rechecksum(forged_evidence)
    bad_snapshot = audit_agent_source_support_manifest(
        runtime_evidence=forged_evidence,
    )
    assert bad_snapshot["support_snapshot_manifest_mismatch"] == 1
    assert bad_snapshot["ok"] is False

    wrong_schema_evidence = dict(evidence)
    wrong_schema_evidence["source_snapshots"] = {"aider": dict(snapshot)}
    wrong_schema_evidence["source_snapshots"]["aider"]["schema_version"] = "forged.v0"
    wrong_schema_report = audit_agent_source_support_manifest(
        runtime_evidence=_rechecksum(wrong_schema_evidence),
        require_runtime_evidence=True,
    )
    assert wrong_schema_report["support_snapshot_manifest_mismatch"] == 1
    assert wrong_schema_report["ok"] is False
    assert any(
        "snapshot_schema_version_mismatch" in finding["message"]
        for finding in wrong_schema_report["findings"]
    )

    with pytest.raises(
        AgentSourceSupportManifestError,
        match="cannot carry runtime receipts or attestations",
    ):
        build_source_support_runtime_report(
            {
                "source_snapshots": {"aider": snapshot},
                "errors": 0,
                "unmanifested_sources": [],
                "runtime_receipts": [
                    {"agent": name, "support_manifest_hash": manifest.manifest_hash}
                    for name in manifest.host_agent_names
                ],
            },
            producer="daemon.raw_sync",
            manifest=manifest,
        )
    with pytest.raises(
        AgentSourceSupportManifestError,
        match="cannot carry runtime receipts or attestations",
    ):
        build_source_support_runtime_report(
            {
                "source_snapshots": {"aider": snapshot},
                "errors": 0,
                "unmanifested_sources": [],
                "certifying": True,
                "runtime_full_power_ok": True,
            },
            producer="daemon.raw_sync",
            manifest=manifest,
        )
    forged_full_power = _rechecksum(
        {
            **evidence,
            "runtime_receipts": [
                {"agent": name, "support_manifest_hash": manifest.manifest_hash}
                for name in manifest.host_agent_names
            ],
        }
    )
    forged_full_power_report = audit_agent_source_support_manifest(
        runtime_evidence=forged_full_power,
    )
    assert forged_full_power_report["certifying"] is False
    assert any(
        finding["code"] == "runtime_report_contains_forbidden_attestation"
        for finding in forged_full_power_report["findings"]
    )
    assert forged_full_power_report["ok"] is False

    forged_certifying = audit_agent_source_support_manifest(
        runtime_evidence=_rechecksum({**evidence, "release_eligible": True}),
    )
    assert forged_certifying["certifying"] is False
    assert any(
        finding["code"] == "runtime_report_contains_forbidden_attestation"
        for finding in forged_certifying["findings"]
    )

    unmanifested_report = audit_agent_source_support_manifest(
        runtime_evidence=_rechecksum({**evidence, "unmanifested_sources": ["rogue"]}),
    )
    assert unmanifested_report["ok"] is False
    assert any(
        finding["code"] == "runtime_report_unmanifested_sources_present"
        for finding in unmanifested_report["findings"]
    )

    failed_report = audit_agent_source_support_manifest(
        runtime_evidence=_rechecksum({**evidence, "errors": 1}),
    )
    assert failed_report["ok"] is False
    assert any(
        finding["code"] == "runtime_report_errors_present" for finding in failed_report["findings"]
    )

    absent = audit_agent_source_support_manifest(require_runtime_evidence=True)
    assert any(finding["code"] == "runtime_evidence_missing" for finding in absent["findings"])


def test_runtime_receipt_binds_manifest_hash_and_rejects_stale_receipt(
    tmp_path: Path,
    monkeypatch,
):
    import core.agent_kit.source_support_manifest as source_support_manifest_module

    from core.agent_kit.authorization import AgentAuthorizationStore
    from core.agent_kit.runtime_receipts import AgentRuntimeReceiptStore, runtime_probe_contract
    from core.agent_kit.source_support_manifest import (
        get_agent_source_support_manifest,
        load_agent_source_support_manifest,
    )
    from core.ops.health_contract import CANONICAL_HEALTH_CHECK_IDS_HASH

    db_path = tmp_path / "agent_authorization.db"
    AgentAuthorizationStore(db_path).set_state("codex", "user_authorized")
    store = AgentRuntimeReceiptStore(db_path)
    store.record_health_check("codex", CANONICAL_HEALTH_CHECK_IDS_HASH)
    result = store.record_probe(
        "codex",
        health_check_ids_hash=CANONICAL_HEALTH_CHECK_IDS_HASH,
        sample=runtime_probe_contract()["sample"],
    )
    assert result["runtime_state"] == "verified"
    assert result["support_manifest_hash"] == get_agent_source_support_manifest().manifest_hash

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT sample_completeness_json FROM agent_runtime_receipts WHERE agent='codex'"
        ).fetchone()
        assert row is not None
        payload = json.loads(row[0])
        payload["support_manifest_hash"] = "stale-manifest"
        conn.execute(
            "UPDATE agent_runtime_receipts SET sample_completeness_json=? WHERE agent='codex'",
            (json.dumps(payload, sort_keys=True),),
        )
    stale = store.evaluate("codex")
    assert stale["runtime_state"] == "support_manifest_hash_mismatch"

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT sample_completeness_json FROM agent_runtime_receipts WHERE agent='codex'"
        ).fetchone()
        assert row is not None
        payload = json.loads(row[0])
        payload["support_manifest_hash"] = get_agent_source_support_manifest().manifest_hash
        conn.execute(
            "UPDATE agent_runtime_receipts SET sample_completeness_json=? WHERE agent='codex'",
            (json.dumps(payload, sort_keys=True),),
        )

    updated_payload = _manifest_payload()
    updated_payload["sources"][0]["retention"]["policy"] = "new_policy"
    updated_manifest = load_agent_source_support_manifest(
        _write_manifest(tmp_path, updated_payload)
    )
    monkeypatch.setattr(
        source_support_manifest_module,
        "load_agent_source_support_manifest",
        lambda: updated_manifest,
    )
    changed_contract = store.evaluate("codex")
    assert changed_contract["runtime_state"] == "support_manifest_hash_mismatch"


def test_manifest_getter_reloads_changed_contract_without_cache(monkeypatch, tmp_path: Path):
    import core.agent_kit.source_support_manifest as source_support_manifest_module

    first = source_support_manifest_module.load_agent_source_support_manifest()
    changed_payload = _manifest_payload()
    changed_payload["sources"][0]["retention"]["policy"] = "cache-reload-proof"
    second = source_support_manifest_module.load_agent_source_support_manifest(
        _write_manifest(tmp_path, changed_payload)
    )
    current = [first]
    monkeypatch.setattr(
        source_support_manifest_module,
        "load_agent_source_support_manifest",
        lambda: current[0],
    )

    assert source_support_manifest_module.get_agent_source_support_manifest() is first
    current[0] = second
    assert source_support_manifest_module.get_agent_source_support_manifest() is second


def test_agent_kit_surfaces_installed_unauthorized_ingestion_only_source(monkeypatch):
    from core.agent_kit import report as report_module

    monkeypatch.setattr(
        report_module,
        "agent_install_evidence",
        lambda name: (name == "aider", "/fixture/aider" if name == "aider" else None),
    )
    monkeypatch.setattr(report_module, "_safe_active_adapter_names", lambda: set())
    monkeypatch.setattr(report_module, "_active_status_by_agent", lambda _load: {})
    monkeypatch.setattr(
        report_module,
        "_passive_source_details",
        lambda _agent, **_kwargs: {
            "registered": True,
            "detected": True,
            "data_dir": "/fixture/aider",
            "capabilities": {"source_fidelity": "full"},
        },
    )

    report = report_module.build_agent_kit_report(
        probe_filesystem=True,
        load_default_providers=False,
    ).to_dict()
    aider = next(source for source in report["ingestion_sources"] if source["name"] == "aider")
    assert aider["installed"] is True
    assert aider["full_power"] is False
    assert aider["authorization_state"] == "detected"
    assert "content access is not authorized" in aider["gaps"]


def test_ingestion_only_source_cannot_be_promoted_to_host_active_policy(monkeypatch):
    from integrations import active

    monkeypatch.setattr(
        active,
        "write_active_policy_file",
        lambda: pytest.fail("ingestion-only policy install must not write shared policy state"),
    )

    assert active.install_agent_policy("aider") is False
    assert active.is_agent_policy_installed("aider") is False


@pytest.mark.parametrize(
    ("source_name", "module_name", "class_name", "file_name", "content"),
    [
        (
            "aider",
            "integrations.sources.aider_source",
            "AiderSource",
            ".aider.chat.history.md",
            "#### /message\nuser canary\n\n#### assistant\nassistant canary\n",
        ),
        (
            "gemini",
            "integrations.sources.gemini_cli_source",
            "GeminiCliSource",
            "session.jsonl",
            '{"role":"user","content":"user canary"}\n{"role":"model","content":"assistant canary"}\n',
        ),
        (
            "cursor",
            "integrations.sources.cursor_source",
            "CursorSource",
            "chat_history.json",
            (
                '[{"role":"user","content":"user canary"},'
                '{"role":"assistant","content":"assistant canary"}]'
            ),
        ),
        (
            "windsurf",
            "integrations.sources.windsurf_source",
            "WindsurfSource",
            "history.json",
            (
                '[{"role":"user","content":"user canary"},'
                '{"role":"assistant","content":"assistant canary"}]'
            ),
        ),
    ],
)
def test_ingestion_only_native_to_raw_canary_preserves_manifest_contract(
    tmp_path: Path,
    source_name: str,
    module_name: str,
    class_name: str,
    file_name: str,
    content: str,
):
    module = __import__(module_name, fromlist=[class_name])
    source = getattr(module, class_name)()
    source_path = tmp_path / file_name
    source_path.write_text(content, encoding="utf-8")
    turns = source.parse_turns(source_path)
    assert turns, source_name
    session = SessionInfo(session_id=f"{source_name}-canary", source_path=source_path)
    store = RawEventStore(db_path=tmp_path / f"{source_name}.db", config=_Cfg(tmp_path))
    try:
        event_id = _backfill_turn(store, source, session, turns[0])
        row = store.get_turn(event_id)
    finally:
        store.close()

    assert row is not None
    assert row["metadata"]["support_role"] == "ingestion_only"
    assert row["metadata"]["support_manifest_hash"]
    assert row["metadata"]["support_raw_contract_hash"]
    assert row["metadata"]["support_native_to_raw"] == "lossless_visible_v1"
    assert row["metadata"]["support_acl_policy"] == "inherit_source_scope"
    assert row["metadata"]["support_retention_policy"] == "raw_event_store_default"
    assert row["metadata"]["support_native_capture"] is True
    assert row["metadata"]["source_fidelity"] in {"full", "experimental"}
    assert row["completeness"]["visible_text"] == "full"
    assert row["completeness"]["truncated"] is False
    assert "user canary" in row["user_content"]
    assert "assistant canary" in row["assistant_content"]


def test_native_raw_contract_rejects_invalid_fidelity_before_upsert(tmp_path: Path):
    from core.agent_kit.source_support_manifest import AgentSourceSupportManifestError
    from core.sync_framework.source_support import build_native_raw_metadata

    class _NativeCodex:
        name = "codex"

        def completeness_capabilities(self):
            return {"source_fidelity": "full"}

    session = SessionInfo(session_id="native-contract", source_path=tmp_path / "session.jsonl")
    turn = Turn(
        turn_number=0,
        user_content="user",
        assistant_content="assistant",
    )
    metadata = build_native_raw_metadata(_NativeCodex(), session, turn)
    metadata["source_fidelity"] = "invented"
    store = RawEventStore(db_path=tmp_path / "raw_events.db", config=_Cfg(tmp_path))
    try:
        with pytest.raises(
            AgentSourceSupportManifestError, match="observed source fidelity is invalid"
        ):
            store.upsert_turn(
                source_agent="codex",
                session_id=session.session_id,
                turn_number=turn.turn_number,
                user_content=turn.user_content,
                assistant_content=turn.assistant_content,
                metadata=metadata,
                completeness=dict(turn.completeness),
            )
    finally:
        store.close()


def test_degraded_native_raw_is_retained_with_nonconforming_contract_evidence(tmp_path: Path):
    from core.sync_framework.source_support import build_native_raw_metadata

    class _NativeCodex:
        name = "codex"

        def completeness_capabilities(self):
            return {"source_fidelity": "full"}

    session = SessionInfo(session_id="partial-contract", source_path=tmp_path / "session.jsonl")
    turn = Turn(
        turn_number=0,
        user_content="retained user bytes",
        assistant_content="retained assistant bytes",
        completeness={
            "visible_text": "full",
            "truncated": True,
            "loss_reasons": ["native_stream_ended"],
        },
    )
    store = RawEventStore(db_path=tmp_path / "raw_events.db", config=_Cfg(tmp_path))
    try:
        revision_id = store.upsert_turn(
            source_agent="codex",
            session_id=session.session_id,
            turn_number=turn.turn_number,
            user_content=turn.user_content,
            assistant_content=turn.assistant_content,
            metadata=build_native_raw_metadata(_NativeCodex(), session, turn),
            completeness=dict(turn.completeness),
        )
        row = store.get_turn(revision_id)
    finally:
        store.close()

    assert row is not None
    assert row["user_content"] == "retained user bytes"
    assert row["assistant_content"] == "retained assistant bytes"
    assert row["completeness_status"] == "partial"
    assert row["metadata"]["support_raw_contract_state"] == "nonconforming"
    assert "raw_content_truncated" in row["metadata"]["support_raw_contract_errors"]
    assert "raw_content_loss_reason_present" in row["metadata"]["support_raw_contract_errors"]


def test_unknown_native_fidelity_is_nonconforming_and_never_complete(tmp_path: Path):
    from core.sync_framework.source_support import build_native_raw_metadata

    class _UnknownNativeCodex:
        name = "codex"

        def completeness_capabilities(self):
            return {}

    session = SessionInfo(session_id="unknown-fidelity", source_path=tmp_path / "session.jsonl")
    turn = Turn(turn_number=0, user_content="user bytes", assistant_content="assistant bytes")
    store = RawEventStore(db_path=tmp_path / "raw_events.db", config=_Cfg(tmp_path))
    try:
        revision_id = store.upsert_turn(
            source_agent="codex",
            session_id=session.session_id,
            turn_number=turn.turn_number,
            user_content=turn.user_content,
            assistant_content=turn.assistant_content,
            metadata=build_native_raw_metadata(_UnknownNativeCodex(), session, turn),
            completeness={"visible_text": "full", "truncated": False},
        )
        row = store.get_turn(revision_id)
    finally:
        store.close()

    assert row is not None
    assert row["metadata"]["source_fidelity"] == "unknown"
    assert row["metadata"]["support_raw_contract_state"] == "nonconforming"
    assert "source_fidelity_contract_mismatch" in row["metadata"]["support_raw_contract_errors"]
    assert row["completeness_status"] == "partial"


def test_later_degraded_native_observation_is_append_only_and_noncertifying(tmp_path: Path):
    from core.sync_framework.native_raw_contract_ledger import NativeRawContractLedger
    from core.sync_framework.source_support import build_native_raw_metadata

    class _NativeCodex:
        name = "codex"

        def completeness_capabilities(self):
            return {"source_fidelity": "full"}

    session = SessionInfo(session_id="late-degradation", source_path=tmp_path / "session.jsonl")
    full_turn = Turn(turn_number=0, user_content="full user", assistant_content="full assistant")
    degraded_turn = Turn(
        turn_number=0,
        user_content="partial user",
        assistant_content="partial assistant",
        completeness={
            "visible_text": "full",
            "truncated": True,
            "loss_reasons": ["native_stream_ended"],
        },
    )
    store = RawEventStore(db_path=tmp_path / "raw_events.db", config=_Cfg(tmp_path))
    try:
        current_revision_id = store.upsert_turn(
            source_agent="codex",
            session_id=session.session_id,
            turn_number=0,
            user_content=full_turn.user_content,
            assistant_content=full_turn.assistant_content,
            metadata=build_native_raw_metadata(_NativeCodex(), session, full_turn),
            completeness={"visible_text": "full", "truncated": False},
        )
        complete_metrics = store.get_metrics(current_revision_id)
        returned_revision_id = store.upsert_turn(
            source_agent="codex",
            session_id=session.session_id,
            turn_number=0,
            user_content=degraded_turn.user_content,
            assistant_content=degraded_turn.assistant_content,
            metadata=build_native_raw_metadata(_NativeCodex(), session, degraded_turn),
            completeness=dict(degraded_turn.completeness),
        )
        degraded_metrics = store.get_metrics(current_revision_id)
        conn = store._pool.get_conn()  # noqa: SLF001
        logical_event_id = conn.execute(
            "SELECT event_id FROM raw_turns WHERE source_agent='codex' AND session_id=?",
            (session.session_id,),
        ).fetchone()[0]
        # Simulate a delayed writer whose earlier clock timestamp reaches SQLite last.
        conn.execute(
            "UPDATE raw_native_contract_observations SET observed_at='2000-01-01T00:00:00' "
            "WHERE logical_event_id=? AND contract_state='nonconforming'",
            (logical_event_id,),
        )
        NativeRawContractLedger().refresh_effective_state(
            conn,
            logical_event_id=logical_event_id,
            observed_at="2026-07-12T00:00:00",
        )
        conn.commit()
        current = store.get_turn(current_revision_id)
        degraded_revision = store.get_turn(returned_revision_id)
        revisions = store.list_revisions(
            source_agent="codex",
            session_id=session.session_id,
            turn_number=0,
        )
        observations = store.list_native_contract_observations(
            source_agent="codex",
            session_id=session.session_id,
            turn_number=0,
        )
        logical_row = (
            store._pool.get_conn()
            .execute(  # noqa: SLF001
                "SELECT completeness_status, metadata_json FROM raw_turns "
                "WHERE source_agent='codex' AND session_id=? AND turn_number=0",
                (session.session_id,),
            )
            .fetchone()
        )
        restored_revision_id = store.upsert_turn(
            source_agent="codex",
            session_id=session.session_id,
            turn_number=0,
            user_content=full_turn.user_content,
            assistant_content=full_turn.assistant_content,
            metadata=build_native_raw_metadata(_NativeCodex(), session, full_turn),
            completeness={"visible_text": "full", "truncated": False},
        )
        restored = store.get_turn(restored_revision_id)
        restored_metrics = store.get_metrics(restored_revision_id)
    finally:
        store.close()

    assert returned_revision_id != current_revision_id
    assert degraded_revision is not None
    assert degraded_revision["user_content"] == "partial user"
    assert degraded_revision["supersedes_revision_id"] == current_revision_id
    assert current is not None
    assert current["user_content"] == "full user"
    assert current["assistant_content"] == "full assistant"
    assert current["completeness_status"] == "partial"
    assert current["metadata"]["support_raw_contract_state"] == "nonconforming"
    assert current["metadata"]["support_current_revision_raw_contract_state"] == "conformant"
    assert current["metadata"]["support_native_contract_certifying"] is False
    assert current["native_contract_observation"]["contract_state"] == "nonconforming"
    assert complete_metrics is not None
    assert complete_metrics["confidence"] == 1.0
    assert complete_metrics["survival_score"] == 1.0
    assert degraded_metrics is not None
    assert degraded_metrics["confidence"] == 0.4
    assert degraded_metrics["survival_score"] == 0.4
    assert logical_row is not None
    assert logical_row[0] == "partial"
    assert json.loads(logical_row[1])["support_raw_contract_state"] == "nonconforming"
    assert len(revisions) == 2
    assert [item["contract_state"] for item in observations] == [
        "conformant",
        "nonconforming",
    ]
    assert restored is not None
    assert restored["completeness_status"] == "complete"
    assert restored["metadata"]["support_native_contract_certifying"] is True
    assert restored_metrics is not None
    assert restored_metrics["confidence"] == 1.0
    assert restored_metrics["survival_score"] == 1.0


def test_nonconforming_native_raw_cannot_claim_complete_status(tmp_path: Path):
    from core.sync_framework.source_support import build_native_raw_metadata

    class _NativeCodex:
        name = "codex"

        def completeness_capabilities(self):
            return {"source_fidelity": "full"}

    session = SessionInfo(session_id="acl-contract", source_path=tmp_path / "session.jsonl")
    turn = Turn(turn_number=0, user_content="user bytes", assistant_content="assistant bytes")
    metadata = build_native_raw_metadata(_NativeCodex(), session, turn)
    metadata.pop("canonical_session_id")
    metadata.pop("source_session_id")
    store = RawEventStore(db_path=tmp_path / "raw_events.db", config=_Cfg(tmp_path))
    try:
        revision_id = store.upsert_turn(
            source_agent="codex",
            session_id=session.session_id,
            turn_number=turn.turn_number,
            user_content=turn.user_content,
            assistant_content=turn.assistant_content,
            metadata=metadata,
            completeness={"visible_text": "full", "truncated": False},
        )
        row = store.get_turn(revision_id)
    finally:
        store.close()

    assert row is not None
    assert row["completeness_status"] == "partial"
    assert row["metadata"]["support_raw_contract_state"] == "nonconforming"
    assert "acl_canonical_session_missing" in row["metadata"]["support_raw_contract_errors"]
    assert "acl_source_session_missing" in row["metadata"]["support_raw_contract_errors"]
