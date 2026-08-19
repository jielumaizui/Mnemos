from __future__ import annotations

import json
from pathlib import Path

import pytest


def _flatten(value, prefix: str = "") -> dict[str, object]:
    result: dict[str, object] = {}
    if isinstance(value, dict):
        if prefix and not value:
            return {prefix: {}}
        for key, child in value.items():
            child_key = f"{prefix}.{key}" if prefix else str(key)
            result.update(_flatten(child, child_key))
        return result
    result[prefix] = value
    return result


def test_registry_is_the_complete_typed_view_of_default_config():
    from core.config import DEFAULT_CONFIG, PERFORMANCE_TIERS
    from core.config_registry import CONFIG_REGISTRY

    defaults = _flatten(DEFAULT_CONFIG)
    assert CONFIG_REGISTRY.schema_version == "mnemos.config_registry.v1"
    assert CONFIG_REGISTRY.keys_present_in_tree(DEFAULT_CONFIG) == set(defaults) | {
        "llm.providers"
    }
    assert CONFIG_REGISTRY.keys() == set(defaults) | {"llm.providers"}
    assert CONFIG_REGISTRY.key_count == len(defaults) + 1

    for key, default in defaults.items():
        spec = CONFIG_REGISTRY.require(key)
        assert spec.default == default
        assert spec.example_documented is True
        assert spec.tested is True
        assert spec.documented is True

    for tier, overrides in PERFORMANCE_TIERS.items():
        errors = CONFIG_REGISTRY.validate_override_tree(
            overrides,
            source=f"performance_tier:{tier}",
        )
        assert errors == []


def test_registry_owns_env_alias_removed_and_runtime_only_lifecycle():
    from core.config_registry import CONFIG_REGISTRY, UnknownConfigKeyError

    assert CONFIG_REGISTRY.env_overrides["MNEMOS_PREFLIGHT_TIMEOUT_SEC"] == (
        "preflight.timeout_sec"
    )
    assert CONFIG_REGISTRY.env_overrides["MNEMOS_RETENTION_DAYS_DISTILLATION_CHUNKS"] == (
        "storage.retention_days.distillation_chunks"
    )
    assert CONFIG_REGISTRY.aliases["daemon.services.l1_sync"] == (
        "daemon.services.raw_sync"
    )
    assert "daemon.services.l1_sync" not in CONFIG_REGISTRY.keys()
    assert "privacy.encryption.databases" in CONFIG_REGISTRY.removed_keys
    assert "storage.retention_days.prompt_calls" in CONFIG_REGISTRY.removed_keys
    assert "quality_gate.prompt_call_log" in CONFIG_REGISTRY.removed_keys
    with pytest.raises(UnknownConfigKeyError):
        CONFIG_REGISTRY.require("daemon.services.l1_sync")


def test_registry_rejects_unknown_removed_and_wrong_type_values():
    from core.config_registry import CONFIG_REGISTRY

    assert [e.code for e in CONFIG_REGISTRY.validate_flat_values({"not.real": 1})] == [
        "unknown_key"
    ]
    assert [
        e.code
        for e in CONFIG_REGISTRY.validate_flat_values(
            {"privacy.encryption.databases": True}
        )
    ] == ["removed_key"]
    assert [
        e.code
        for e in CONFIG_REGISTRY.validate_flat_values(
            {"distill.token_budget_total": "not-an-int"}
        )
    ] == ["invalid_type"]


def test_config_rejects_unknown_and_removed_file_keys(tmp_path: Path, monkeypatch):
    from core.config import Config
    from core.config_registry import ConfigValidationError

    monkeypatch.setenv("MNEMOS_DIR", str(tmp_path / "home"))
    for payload in (
        {"not": {"real": 1}},
        {"daemon": {"services": {"l1_sync": True}}},
        {"privacy": {"encryption": {"databases": True}}},
        {"distill": {"token_budget_total": "not-an-int"}},
        {"performance_tier": "turbo-unknown"},
    ):
        config_path = tmp_path / f"config-{len(list(tmp_path.glob('config-*')))}.json"
        config_path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ConfigValidationError):
            Config(config_path=config_path)


def test_config_does_not_silently_discard_removed_model_call_keys(tmp_path: Path, monkeypatch):
    from core.config import Config
    from core.config_registry import ConfigValidationError

    monkeypatch.setenv("MNEMOS_DIR", str(tmp_path / "home"))
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"storage": {"retention_days": {"prompt_calls": 7}}}),
        encoding="utf-8",
    )

    with pytest.raises(ConfigValidationError) as exc_info:
        Config(config_path=config_path)

    assert any(issue.key == "storage.retention_days.prompt_calls" for issue in exc_info.value.errors)


def test_config_rejects_non_object_json_root(tmp_path: Path, monkeypatch):
    from core.config import Config
    from core.config_registry import ConfigValidationError

    monkeypatch.setenv("MNEMOS_DIR", str(tmp_path / "home"))
    config_path = tmp_path / "config.json"
    config_path.write_text("[]", encoding="utf-8")

    with pytest.raises(ConfigValidationError):
        Config(config_path=config_path)


def test_legacy_raw_projection_truncation_is_discarded_not_reenabled(tmp_path: Path, monkeypatch):
    from core.config import Config

    monkeypatch.setenv("MNEMOS_DIR", str(tmp_path / "home"))
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"raw_projection": {"max_turn_chars": 12000}}),
        encoding="utf-8",
    )

    cfg = Config(config_path=config_path, provision=False)

    assert cfg.get("raw_projection.max_turn_chars") == 0
    assert "raw_projection.max_turn_chars" in cfg._ignored_obsolete_keys  # noqa: SLF001


def test_legacy_skill_suggestion_truncation_is_discarded_not_reenabled(
    tmp_path: Path, monkeypatch
):
    """COG-013 removes the terminal suggestion truncation knob compatibly."""
    from core.config import Config
    from core.config_registry import CONFIG_REGISTRY

    monkeypatch.setenv("MNEMOS_DIR", str(tmp_path / "home"))
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"distill": {"skill_suggestion_max_chars": 3000}}),
        encoding="utf-8",
    )

    cfg = Config(config_path=config_path, provision=False)

    assert "distill.skill_suggestion_max_chars" in cfg._ignored_obsolete_keys  # noqa: SLF001
    assert "skill_suggestion_max_chars" not in cfg._data["distill"]  # noqa: SLF001
    assert "distill.skill_suggestion_max_chars" in CONFIG_REGISTRY.removed_keys


def test_legacy_external_collector_budget_is_discarded_not_reenabled(
    tmp_path: Path, monkeypatch
):
    """COG-028 removes the second distillation entrypoint and its budget."""
    from core.config import Config
    from core.config_registry import CONFIG_REGISTRY

    monkeypatch.setenv("MNEMOS_DIR", str(tmp_path / "home"))
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"distill": {"max_collect_per_cycle": 10}}),
        encoding="utf-8",
    )

    cfg = Config(config_path=config_path, provision=False)

    assert "distill.max_collect_per_cycle" in cfg._ignored_obsolete_keys  # noqa: SLF001
    assert "max_collect_per_cycle" not in cfg._data["distill"]  # noqa: SLF001
    assert "distill.max_collect_per_cycle" in CONFIG_REGISTRY.removed_keys


def test_config_get_cannot_restore_caller_selected_unknown_fallback(
    tmp_path: Path,
    monkeypatch,
):
    from core.config import Config
    from core.config_registry import UnknownConfigKeyError

    monkeypatch.setenv("MNEMOS_DIR", str(tmp_path / "home"))
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    cfg = Config(config_path=config_path)

    assert cfg.get("distill.token_budget_total", 999999) == 16000
    with pytest.raises(UnknownConfigKeyError):
        cfg.get("not.real", "caller-fallback")
    with pytest.raises(UnknownConfigKeyError):
        cfg.get("daemon.services.l1_sync")
    cfg.set("app.push_max_items", 8.0)
    assert cfg.get("app.push_max_items") == 8
    assert type(cfg.get("app.push_max_items")) is int


def test_effective_source_and_fingerprint_are_stable_and_value_safe(
    tmp_path: Path,
    monkeypatch,
):
    from core.config import Config

    monkeypatch.setenv("MNEMOS_DIR", str(tmp_path / "home"))
    monkeypatch.setenv("MNEMOS_DISTILL__TOKEN_BUDGET_TOTAL", "32000")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"distill": {"extract_correction_retries": 2}}),
        encoding="utf-8",
    )
    first = Config(config_path=config_path)
    second = Config(config_path=config_path)

    assert first.explain("distill.token_budget_total") == {
        "key": "distill.token_budget_total",
        "effective_source": "env:MNEMOS_DISTILL__TOKEN_BUDGET_TOTAL",
        "value_type": "int",
        "secret": False,
    }
    assert first.explain("distill.extract_correction_retries")["effective_source"] == (
        f"file:{config_path}"
    )
    assert first.explain("vaults.raw.path")["effective_source"] == "auto:platform"
    assert first.config_fingerprint == second.config_fingerprint
    assert first.config_fingerprint.startswith("sha256:")
    assert "32000" not in first.config_fingerprint


def test_generic_env_override_rejects_unknown_registry_key(tmp_path: Path, monkeypatch):
    from core.config import Config
    from core.config_registry import ConfigValidationError

    monkeypatch.setenv("MNEMOS_DIR", str(tmp_path / "home"))
    monkeypatch.setenv("MNEMOS_NOT_REAL__KEY", "1")
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    with pytest.raises(ConfigValidationError):
        Config(config_path=config_path)


def test_non_provisioning_config_load_is_read_only(tmp_path: Path, monkeypatch):
    from core.config import Config

    mnemos_dir = tmp_path / "absent-home"
    monkeypatch.setenv("MNEMOS_DIR", str(mnemos_dir))
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")

    cfg = Config(config_path=config_path, provision=False)

    assert cfg.config_fingerprint.startswith("sha256:")
    assert not mnemos_dir.exists()
    with pytest.raises(RuntimeError, match="cannot write"):
        cfg.save()
