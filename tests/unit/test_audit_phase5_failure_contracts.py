from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "audit_phase5_failure_contracts.py"


def _module():
    spec = importlib.util.spec_from_file_location(
        "audit_phase5_failure_contracts",
        SCRIPT,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dynamic_legacy_entrypoint_and_old_config_are_detected(tmp_path):
    root = tmp_path / "repo"
    (root / "core").mkdir(parents=True)
    (root / "integrations").mkdir()
    (root / "daemon").mkdir()
    (root / "core/runtime.py").write_text(
        'writer = getattr(store, "record_profile_signal")\n' "writer(signal)\n",
        encoding="utf-8",
    )
    (root / "integrations/apollon.py").write_text(
        "def _run_persona_cycle():\n"
        '    writer = getattr(adapter, "save_persona")\n'
        "    return writer(profile)\n",
        encoding="utf-8",
    )
    (root / "daemon/service.py").write_text(
        'KEY = "daemon.services.persona_extensions"\n',
        encoding="utf-8",
    )

    residuals = _module()._retired_runtime_residuals(root)

    assert any("call:record_profile_signal" in item for item in residuals)
    assert any("dynamic:save_persona" in item for item in residuals)
    assert any("legacy_config:daemon.services.persona_extensions" in item for item in residuals)


def test_candidate_phase5_contract_snapshot_has_no_runtime_residuals():
    module = _module()
    root = Path(__file__).resolve().parents[2]

    report = module.audit_phase5_failure_contracts(
        root,
        evidence_path=root / "docs/acceptance/phase5_baseline_failure_evidence.json",
        skip_baseline_evidence=True,
    )

    assert report["wrong_legacy_behavior_expected_by_runtime_test"] == 0
    assert report["old_production_caller_residual"] == 0
    assert report["static_green_production_red"] == 0
    assert all(report["checks"].values())
    assert all(report["false_green_checks"].values())
