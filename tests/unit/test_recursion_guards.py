"""Tests for cycle guards in recursive helpers."""


def test_config_deep_merge_handles_cyclic_override(monkeypatch, tmp_path):
    """Config._deep_merge 遇到循环引用时不应栈溢出。"""
    monkeypatch.setenv("MNEMOS_DIR", str(tmp_path))
    from core.config import Config

    cfg = Config()
    base = {}
    cyclic = {}
    cyclic["self"] = cyclic

    cfg._deep_merge(base, cyclic)

    assert base["self"] is cyclic


def test_auto_setup_deep_merge_handles_cyclic_override():
    """scripts.auto_setup._deep_merge 遇到循环引用时不应栈溢出。"""
    from scripts.auto_setup import _deep_merge

    base = {}
    cyclic = {}
    cyclic["self"] = cyclic

    _deep_merge(base, cyclic)

    assert base["self"] is cyclic


def test_redact_secrets_handles_cyclic_value():
    """scripts._config_example_data.redact_secrets 遇到循环引用时应返回占位符。"""
    from scripts._config_example_data import redact_secrets

    cyclic = {}
    cyclic["self"] = cyclic

    result = redact_secrets(cyclic)

    assert result["self"] == "<cyclic-reference>"


def test_redact_secrets_still_redacts_secret_keys():
    """redact_secrets 在增加环检测后仍应脱敏 secret 键。"""
    from scripts._config_example_data import redact_secrets

    result = redact_secrets({"api_key": "secret", "token": "t", "name": "ok"})

    assert result["api_key"] == ""
    assert result["token"] == ""
    assert result["name"] == "ok"
