# -*- coding: utf-8 -*-
"""LLM API config resolver tests."""

from core.llm_config import resolve_llm_api_config


class FakeConfig:
    def __init__(self, data):
        self.data = data

    def get(self, key, default=None):
        value = self.data
        for part in key.split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return default
        return value


def test_resolve_siliconflow_env(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sf-test")

    cfg = resolve_llm_api_config(FakeConfig({}))

    assert cfg.configured is True
    assert cfg.provider == "siliconflow"
    assert cfg.api_key == "sf-test"
    assert "siliconflow" in cfg.base_url
    assert cfg.source == "env:SILICONFLOW_API_KEY"


def test_resolve_top_level_llm_config(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    cfg = resolve_llm_api_config(FakeConfig({
        "llm": {
            "provider": "siliconflow",
            "api_key": "cfg-key",
            "base_url": "https://api.siliconflow.cn/v1",
            "model": "deepseek-ai/DeepSeek-V3",
        }
    }))

    assert cfg.configured is True
    assert cfg.api_key == "cfg-key"
    assert cfg.source == "config:llm.api_key"


def test_resolve_provider_config(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    cfg = resolve_llm_api_config(FakeConfig({
        "llm": {
            "provider": "openai",
            "providers": {
                "openai": {
                    "api_key": "openai-key",
                    "base_url": "https://example.test/v1",
                    "model": "test-model",
                }
            },
        }
    }))

    assert cfg.configured is True
    assert cfg.provider == "openai"
    assert cfg.base_url == "https://example.test/v1"
    assert cfg.model == "test-model"
    assert cfg.source == "config:llm.providers.openai.api_key"


def test_embedding_key_fallback(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    cfg = resolve_llm_api_config(FakeConfig({
        "embedding": {
            "provider": "siliconflow",
            "api_key": "embed-key",
            "base_url": "https://api.siliconflow.cn/v1",
        }
    }))

    assert cfg.configured is True
    assert cfg.provider == "siliconflow"
    assert cfg.source == "config:embedding.api_key"

