from __future__ import annotations

from pathlib import Path

from core.llm_config import resolve_multimodal_api_config
from core.ops import health_check
from scripts import auto_setup as aus


class FakeConfig:
    def __init__(self, data: dict | None = None) -> None:
        self._data = data or {}

    def get(self, key: str, default=None):
        value = self._data
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value


def _clear_multimodal_env(monkeypatch) -> None:
    for name in (
        "MNEMOS_MULTIMODAL_API_KEY",
        "MNEMOS_MULTIMODAL_PROVIDER",
        "MNEMOS_MULTIMODAL_BASE_URL",
        "MNEMOS_MULTIMODAL_MODEL",
        "SILICONFLOW_API_KEY",
        "SILICONFLOW_BASE_URL",
        "SILICONFLOW_MULTIMODAL_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_resolve_multimodal_missing_is_optional(monkeypatch) -> None:
    _clear_multimodal_env(monkeypatch)

    cfg = resolve_multimodal_api_config(FakeConfig({}))

    assert cfg.kind == "multimodal"
    assert cfg.configured is False
    assert cfg.source == "missing"
    assert cfg.model


def test_resolve_multimodal_from_dedicated_env(monkeypatch) -> None:
    _clear_multimodal_env(monkeypatch)
    monkeypatch.setenv("MNEMOS_MULTIMODAL_API_KEY", "vision-key")
    monkeypatch.setenv("MNEMOS_MULTIMODAL_BASE_URL", "https://vision.example.test/v1/")
    monkeypatch.setenv("MNEMOS_MULTIMODAL_MODEL", "vision-model")

    cfg = resolve_multimodal_api_config(FakeConfig({}))

    assert cfg.configured is True
    assert cfg.api_key == "vision-key"
    assert cfg.base_url == "https://vision.example.test/v1"
    assert cfg.model == "vision-model"
    assert cfg.source == "env:MNEMOS_MULTIMODAL_API_KEY"


def test_resolve_multimodal_config_requires_explicit_enable(monkeypatch) -> None:
    _clear_multimodal_env(monkeypatch)
    monkeypatch.setenv("VISION_KEY", "cfg-vision-key")
    data = {
        "multimodal": {
            "enabled": True,
            "provider": "openai-compatible",
            "api_key_source": "env:VISION_KEY",
            "base_url": "https://vision.example.test/v1",
            "model": "vision-model",
        }
    }

    cfg = resolve_multimodal_api_config(FakeConfig(data))

    assert cfg.configured is True
    assert cfg.source == "env:VISION_KEY"
    assert cfg.provider == "openai-compatible"


def test_health_multimodal_skips_when_unconfigured(monkeypatch) -> None:
    _clear_multimodal_env(monkeypatch)

    report = health_check._check_multimodal_model(FakeConfig({}))

    assert report["status"] == "skipped"
    assert report["optional"] is True
    assert report["endpoint_status"] == "skipped"
    assert "MNEMOS_MULTIMODAL_API_KEY" in " ".join(report["repair_actions"])


def test_health_multimodal_reports_configured(monkeypatch) -> None:
    _clear_multimodal_env(monkeypatch)
    monkeypatch.setenv("MNEMOS_MULTIMODAL_API_KEY", "vision-key")
    monkeypatch.setenv("MNEMOS_MULTIMODAL_BASE_URL", "https://vision.example.test/v1")
    monkeypatch.setenv("MNEMOS_MULTIMODAL_MODEL", "vision-model")

    report = health_check._check_multimodal_model(FakeConfig({}))

    assert report["status"] == "ok"
    assert report["optional"] is True
    assert report["endpoint_status"] == "configured"
    assert report["configured"] is True


def test_auto_setup_multimodal_yes_mode_skips_without_env(monkeypatch) -> None:
    _clear_multimodal_env(monkeypatch)
    data: dict = {}

    aus._setup_optional_multimodal(data, yes_mode=True)

    assert data["multimodal"]["enabled"] is False
    assert data["multimodal"]["api_key_env"] == "MNEMOS_MULTIMODAL_API_KEY"


def test_auto_setup_multimodal_uses_dedicated_env(monkeypatch) -> None:
    _clear_multimodal_env(monkeypatch)
    monkeypatch.setenv("MNEMOS_MULTIMODAL_API_KEY", "vision-key")
    monkeypatch.setenv("MNEMOS_MULTIMODAL_BASE_URL", "https://vision.example.test/v1")
    monkeypatch.setenv("MNEMOS_MULTIMODAL_MODEL", "vision-model")
    data: dict = {}

    aus._setup_optional_multimodal(data, yes_mode=True)

    assert data["multimodal"]["enabled"] is True
    assert data["multimodal"]["api_key_source"] == "env:MNEMOS_MULTIMODAL_API_KEY"
    assert data["multimodal"]["base_url"] == "https://vision.example.test/v1"
    assert data["multimodal"]["model"] == "vision-model"


def test_generate_config_does_not_require_optional_multimodal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _clear_multimodal_env(monkeypatch)
    config_path = tmp_path / ".mnemos" / "configs" / "main.json"
    monkeypatch.setattr(aus, "_runtime_config_path", lambda: config_path)
    monkeypatch.setattr(aus, "_mnemos_dir", lambda: tmp_path / ".mnemos")
    monkeypatch.setattr(aus, "_smoke_required_model_endpoints", lambda data: (True, {}))

    path = aus.generate_config(tmp_path / "mnemos-vault", tmp_path / "raw-vault", yes_mode=True)

    assert path == config_path
    data = path.read_text(encoding="utf-8")
    assert '"multimodal"' in data
    assert '"enabled": false' in data
