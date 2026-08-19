from __future__ import annotations

import json
from pathlib import Path

from scripts import audit_runtime_cognitive_interfaces as audit


INTERFACE_ID = "capture_service_turn"


def _write_sources(
    root: Path,
    *,
    producer_source: str = "def expected():\n    record_cognitive_data_event(event)\n",
    consumer_source: str = "def consume():\n    record_cognitive_data_consumed(event_id)\n",
) -> None:
    (root / "producer.py").write_text(producer_source, encoding="utf-8")
    (root / "consumer.py").write_text(consumer_source, encoding="utf-8")
    (root / "gate.py").write_text("def run_contract_gate():\n    return None\n", encoding="utf-8")


def _write_manifest(
    path: Path,
    *,
    interface_id: str = INTERFACE_ID,
    runtime_required: bool = True,
    evidence_mode: str = "runtime_receipt",
    producer_symbol: str = "expected",
    eligible_event_denominator: dict[str, object] | None = None,
) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": audit.MANIFEST_SCHEMA_VERSION,
                "required_release_gate": {
                    "gate_id": "cognitive_production_effectiveness",
                    "runner_path": "gate.py",
                    "runner_symbol": "run_contract_gate",
                    "activation_root": "COG-042",
                    "phase_0_status": "contract_locked_deferred",
                },
                "interfaces": [
                    {
                        "interface_id": interface_id,
                        "runtime_required": runtime_required,
                        "evidence_mode": evidence_mode,
                        "producer_anchors": [
                            {
                                "path": "producer.py",
                                "symbol": producer_symbol,
                                "call": "record_cognitive_data_event",
                            }
                        ],
                        "consumer_anchors": [
                            {
                                "path": "consumer.py",
                                "symbol": "consume",
                                "call": "record_cognitive_data_consumed",
                            }
                        ],
                        "eligible_event_denominator": eligible_event_denominator
                        or {
                            "owner": "canonical_raw",
                            "positive_event_required": True,
                            "unknown_allowed": False,
                        },
                        "target_effect_oracle": {
                            "owner": "target_store",
                            "contract": "before_after_hash_and_reciprocal_receipt",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _registry_payload(*interface_ids: str) -> dict[str, object]:
    return {
        "interfaces": [{"interface_id": interface_id} for interface_id in interface_ids],
        "runtime_instrumented_interface_ids": list(interface_ids),
    }


def _valid_effect_evidence(_: object) -> dict[str, object]:
    return {
        "eligible_event_count": 1,
        "produced_event_count": 1,
        "consumed_event_count": 1,
        "effect_count": 1,
        "unknown_eligible_count": 0,
        "target_before_hash": "before",
        "target_after_hash": "after",
        "reciprocal_receipt": "target-owner:1",
    }


def _single_interface_baseline(monkeypatch) -> None:
    monkeypatch.setattr(audit, "BASELINE_REQUIRED_INTERFACE_IDS", frozenset({INTERFACE_ID}))


def test_runtime_declaration_without_real_scoped_callsite_is_rejected(tmp_path: Path, monkeypatch) -> None:
    _single_interface_baseline(monkeypatch)
    _write_sources(
        tmp_path,
        producer_source="def expected():\n    return None\n",
        consumer_source="def consume():\n    return None\n",
    )
    manifest = _write_manifest(tmp_path / "manifest.json")

    report = audit.audit_runtime_cognitive_interfaces(
        manifest_path=manifest,
        repo_root=tmp_path,
        registry_payload=_registry_payload(INTERFACE_ID),
        effect_evidence_reader=_valid_effect_evidence,
    )

    result = report["interfaces"][0]
    assert report["ok"] is False
    assert result["declared_runtime_receipt"] is True
    assert result["producer_anchors_ok"] is False
    assert result["consumer_anchors_ok"] is False
    assert "producer_anchor_missing" in result["failures"]
    assert "consumer_anchor_missing" in result["failures"]


def test_same_named_call_in_an_unrelated_symbol_is_rejected(tmp_path: Path, monkeypatch) -> None:
    _single_interface_baseline(monkeypatch)
    _write_sources(
        tmp_path,
        producer_source=(
            "def expected():\n"
            "    return None\n\n"
            "def unrelated():\n"
            "    record_cognitive_data_event(event)\n"
        ),
    )
    manifest = _write_manifest(tmp_path / "manifest.json")

    report = audit.audit_runtime_cognitive_interfaces(
        manifest_path=manifest,
        repo_root=tmp_path,
        registry_payload=_registry_payload(INTERFACE_ID),
        effect_evidence_reader=_valid_effect_evidence,
    )

    anchor = report["interfaces"][0]["producer_anchors"][0]
    assert anchor["error"] == "scoped callsite missing"
    assert "producer_anchor_missing" in report["interfaces"][0]["failures"]


def test_static_calls_are_not_substitutes_for_independent_effect_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _single_interface_baseline(monkeypatch)
    _write_sources(tmp_path)
    manifest = _write_manifest(tmp_path / "manifest.json")

    report = audit.audit_runtime_cognitive_interfaces(
        manifest_path=manifest,
        repo_root=tmp_path,
        registry_payload=_registry_payload(INTERFACE_ID),
    )

    result = report["interfaces"][0]
    assert report["ok"] is False
    assert result["producer_anchors_ok"] is True
    assert result["consumer_anchors_ok"] is True
    assert "independent_effect_evidence_missing" in result["failures"]


def test_manifest_cannot_silently_drop_a_baseline_interface(tmp_path: Path, monkeypatch) -> None:
    _write_sources(tmp_path)
    manifest = _write_manifest(tmp_path / "manifest.json", interface_id=INTERFACE_ID)
    monkeypatch.setattr(
        audit,
        "BASELINE_REQUIRED_INTERFACE_IDS",
        frozenset({INTERFACE_ID, "sync_engine_turn"}),
    )

    report = audit.audit_runtime_cognitive_interfaces(
        manifest_path=manifest,
        repo_root=tmp_path,
        registry_payload=_registry_payload(INTERFACE_ID),
        effect_evidence_reader=_valid_effect_evidence,
    )

    assert report["ok"] is False
    assert report["manifest_errors"] == ["missing baseline interface ids: sync_engine_turn"]


def test_registry_and_declared_runtime_ids_missing_from_manifest_block(tmp_path: Path, monkeypatch) -> None:
    _single_interface_baseline(monkeypatch)
    _write_sources(tmp_path)
    manifest = _write_manifest(tmp_path / "manifest.json")

    report = audit.audit_runtime_cognitive_interfaces(
        manifest_path=manifest,
        repo_root=tmp_path,
        registry_payload=_registry_payload(INTERFACE_ID, "untracked_interface"),
        effect_evidence_reader=_valid_effect_evidence,
    )

    assert "registry_interface_missing_manifest:untracked_interface" in report["contract_failures"]
    assert "declared_runtime_interface_missing_manifest:untracked_interface" in report["contract_failures"]


def test_baseline_interface_cannot_be_made_optional(tmp_path: Path, monkeypatch) -> None:
    _single_interface_baseline(monkeypatch)
    _write_sources(tmp_path)
    manifest = _write_manifest(
        tmp_path / "manifest.json",
        runtime_required=False,
        evidence_mode="contract_only",
    )

    report = audit.audit_runtime_cognitive_interfaces(
        manifest_path=manifest,
        repo_root=tmp_path,
        registry_payload=_registry_payload(INTERFACE_ID),
        effect_evidence_reader=_valid_effect_evidence,
    )

    failures = report["interfaces"][0]["failures"]
    assert "baseline_interface_made_optional" in failures
    assert "baseline_interface_not_runtime_receipt" in failures


def test_eligible_positive_without_event_is_rejected(tmp_path: Path, monkeypatch) -> None:
    _single_interface_baseline(monkeypatch)
    _write_sources(tmp_path)
    manifest = _write_manifest(tmp_path / "manifest.json")

    report = audit.audit_runtime_cognitive_interfaces(
        manifest_path=manifest,
        repo_root=tmp_path,
        registry_payload=_registry_payload(INTERFACE_ID),
        effect_evidence_reader=lambda _: {
            **_valid_effect_evidence({}),
            "produced_event_count": 0,
            "consumed_event_count": 0,
            "effect_count": 0,
        },
    )

    assert "eligible_event_without_produced_event" in report["interfaces"][0]["failures"]


def test_receipt_without_target_change_is_rejected(tmp_path: Path, monkeypatch) -> None:
    _single_interface_baseline(monkeypatch)
    _write_sources(tmp_path)
    manifest = _write_manifest(tmp_path / "manifest.json")

    report = audit.audit_runtime_cognitive_interfaces(
        manifest_path=manifest,
        repo_root=tmp_path,
        registry_payload=_registry_payload(INTERFACE_ID),
        effect_evidence_reader=lambda _: {
            **_valid_effect_evidence({}),
            "target_after_hash": "before",
        },
    )

    assert "target_effect_unchanged" in report["interfaces"][0]["failures"]


def test_contract_gate_runner_removal_is_rejected(tmp_path: Path, monkeypatch) -> None:
    _single_interface_baseline(monkeypatch)
    _write_sources(tmp_path)
    manifest = _write_manifest(tmp_path / "manifest.json")
    (tmp_path / "gate.py").write_text("def removed_gate():\n    return None\n", encoding="utf-8")

    report = audit.audit_runtime_cognitive_interfaces(
        manifest_path=manifest,
        repo_root=tmp_path,
        registry_payload=_registry_payload(INTERFACE_ID),
        effect_evidence_reader=_valid_effect_evidence,
    )

    assert "required_release_gate_runner_symbol_missing" in report["contract_failures"]
