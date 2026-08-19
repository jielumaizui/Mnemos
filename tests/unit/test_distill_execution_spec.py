import ast
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest


class _Config:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, key, default=None):
        return self.values.get(key, default)


class _Backend:
    def __init__(self, model="model-a"):
        self.model = model

    def checkpoint_identity(self):
        return {
            "provider": "provider-a",
            "model": self.model,
            "base_url": "https://private.example.invalid/v1",
            "api_key": "must-not-be-persisted",
        }


class _Merger:
    def checkpoint_identity(self):
        return {"strategy": "llm_then_rule", "threshold": 0.4}


def _build(
    values=None,
    *,
    model="model-a",
    prompt="rendered prompt",
    input_spec_hash="sha256:input-a",
    output_admission_contract_version="distill_output_v4",
):
    from core.hephaestus.distill_execution_spec import build_distill_execution_spec

    return build_distill_execution_spec(
        prompt=prompt,
        cfg=_Config(values),
        extractor_backend=_Backend(model),
        merge_component=_Merger(),
        input_contract_version="lossless-visible-v1",
        input_spec_hash=input_spec_hash,
        output_admission_contract_version=output_admission_contract_version,
        prompt_version="prompt-v1",
    )


def test_execution_spec_is_canonical_and_redacts_backend_secrets():
    first = _build()
    second = _build()

    assert first.execution_spec_hash == second.execution_spec_hash
    assert first.schema_version == "mnemos.distill_execution_spec.v2"
    assert first.model_ids == ("provider-a/model-a",)
    assert "must-not-be-persisted" not in first.canonical_json()
    assert "private.example.invalid" not in first.canonical_json()


def test_execution_spec_deep_freezes_nested_config_values():
    source = {"distill.token_budget_total": {"tiers": [16000, 32000]}}
    spec = _build(source)

    assert isinstance(spec.config_values, MappingProxyType)
    nested = spec.config_values["distill.token_budget_total"]
    assert isinstance(nested, MappingProxyType)
    assert nested["tiers"] == (16000, 32000)
    with pytest.raises(TypeError):
        nested["tiers"] = (1,)


@pytest.mark.parametrize(
    "field,value",
    [
        ("prompt_hash", "sha256:changed-prompt"),
        ("output_schema_hash", "sha256:changed-schema"),
        ("extractor_contract_hash", "sha256:changed-extractor"),
        ("backend_hash", "sha256:changed-backend"),
        ("merge_contract_hash", "sha256:changed-merge"),
        ("input_contract_version", "lossless-visible-v2"),
        ("input_spec_hash", "sha256:changed-input"),
        ("output_admission_contract_version", "distill_output_v5"),
        ("prompt_version", "prompt-v2"),
    ],
)
def test_every_execution_contract_field_changes_hash(field, value):
    spec = _build()

    assert replace(spec, **{field: value}).execution_spec_hash != spec.execution_spec_hash


def test_rendered_prompt_and_model_changes_invalidate_spec():
    baseline = _build()

    assert _build(prompt="changed prompt").execution_spec_hash != baseline.execution_spec_hash
    assert _build(model="model-b").execution_spec_hash != baseline.execution_spec_hash


def test_input_spec_and_output_contract_version_change_execution_identity():
    baseline = _build()

    assert (
        _build(input_spec_hash="sha256:another-input").execution_spec_hash
        != baseline.execution_spec_hash
    )
    assert (
        _build(output_admission_contract_version="distill_output_v5").execution_spec_hash
        != baseline.execution_spec_hash
    )


def test_output_schema_content_change_invalidates_spec(monkeypatch):
    from core.hephaestus import distill_execution_spec as mod

    baseline = _build()
    monkeypatch.setattr(mod, "_output_schema_hash", lambda: "sha256:new-schema")

    assert _build().execution_spec_hash != baseline.execution_spec_hash


def test_extractor_contract_change_invalidates_spec(monkeypatch):
    from core.hephaestus import distill_execution_spec as mod

    baseline = _build()
    monkeypatch.setattr(mod, "_extractor_contract_hash", lambda: "sha256:new-code")

    assert _build().execution_spec_hash != baseline.execution_spec_hash


def test_extractor_contract_hash_includes_artifact_identity_code(monkeypatch):
    from core.hephaestus import distill_execution_spec as mod

    seen: list[str] = []

    def fake_file_hash(path):
        seen.append(path.name)
        return f"sha256:{path.name}"

    mod._extractor_contract_hash.cache_clear()
    monkeypatch.setattr(mod, "_file_hash", fake_file_hash)
    try:
        mod._extractor_contract_hash()
    finally:
        mod._extractor_contract_hash.cache_clear()

    assert "artifact_catalog.py" in seen
    assert "artifact_uri.py" in seen


def test_each_registered_output_affecting_config_key_invalidates_spec():
    from core.hephaestus.distill_execution_spec import EXECUTION_CONFIG_KEYS

    baseline = _build()
    for key in EXECUTION_CONFIG_KEYS:
        changed = _build({key: f"changed:{key}"})
        assert changed.execution_spec_hash != baseline.execution_spec_hash, key


def test_unrelated_path_config_does_not_invalidate_spec():
    baseline = _build()
    unrelated = _build({"storage.obsidian.vault_path": "/different/path"})

    assert unrelated.execution_spec_hash == baseline.execution_spec_hash


def test_backend_without_explicit_checkpoint_identity_is_rejected():
    from core.hephaestus.distill_execution_spec import build_distill_execution_spec

    with pytest.raises(TypeError, match="checkpoint_identity"):
        build_distill_execution_spec(
            prompt="prompt",
            cfg=_Config(),
            extractor_backend=object(),
            merge_component=_Merger(),
            input_contract_version="lossless-visible-v1",
            input_spec_hash="sha256:input-a",
            output_admission_contract_version="distill_output_v4",
            prompt_version="prompt-v1",
        )


def test_output_critical_direct_config_reads_are_registered():
    from core.hephaestus.distill_execution_spec import EXECUTION_CONFIG_KEYS

    module_dir = Path(__file__).resolve().parents[2] / "core" / "hephaestus"
    modules = (
        "distillation_engine.py",
        "distillation_extractor.py",
        "distillation_quality.py",
        "distillation_self_check.py",
        "distillation_llm.py",
        "fragment_merger.py",
        "prompt_builder.py",
        "response_budget.py",
        "content_expression.py",
    )
    discovered = set()
    for name in modules:
        tree = ast.parse((module_dir / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                continue
            key = node.args[0].value
            if key.startswith(("distill.", "quality_gate.", "scoring.")):
                discovered.add(key)

    operational_only = {
        "distill.chunk_checkpoint_enabled",
        "distill.chunk_checkpoint_db_path",
    }
    assert discovered - operational_only <= set(EXECUTION_CONFIG_KEYS)
