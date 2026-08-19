from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "audit_persona_profile_contract.py"


def _module():
    spec = importlib.util.spec_from_file_location("persona_profile_contract", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_seeded_persona_profile_audit_is_explicitly_non_certifying(capsys):
    module = _module()
    assert module.main(["--strict", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["audit_scope"] == "isolated_seeded_structural_contract"
    assert payload["seeded_by_audit"] is True
    assert payload["certifying"] is False
    assert payload["authorized_consumers"] == [
        "context_search",
        "persona_behavior_prompt",
        "preflight_builder",
    ]
    assert "distillation_prompt" in payload["unscoped_consumers_disabled"]
    assert payload["phase5_consumer_metrics"] == {
        "declared_consumer_without_runtime_route": 0,
        "disabled_consumer_counted_effective": 0,
        "usage_recorded_before_final_render": 0,
    }
    assert payload["usage_call_without_resolved_principal_scope"] == 0


def test_profile_usage_render_binding_rejects_receipt_before_final_output(
    monkeypatch, tmp_path: Path
):
    module = _module()
    binding = tmp_path / "effect.py"
    search = tmp_path / "core/app/context_search.py"
    binding.write_text('"baseline_ranking"\n"persona_enabled_ranking"\n', encoding="utf-8")
    search.parent.mkdir(parents=True)
    search.write_text(
        "self._record_authorized_profile_usage(\nselected = results[:limit]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "FINAL_RENDER_BINDINGS", {"effect.py": ()})

    assert module._profile_usage_render_binding_gaps() == 1
