# -*- coding: utf-8 -*-
"""LLM API config resolver tests.

覆盖目标：
- LLMApiConfig 数据类行为（masked_key、configured）
- LLMApiChain 主备链行为（all_configs、describe）
- resolve_llm_api_config 全部分支（env/config/fallback/missing）
- _resolve_api_key 全部分支（env:VAR/直接值/空值）
- resolve_llm_api_chain 全部分支（多节点链/不完整配置/回退）
"""

import time
from unittest.mock import patch

import pytest

from core.llm_config import (
    LLMApiChain,
    LLMApiConfig,
    ProviderRateLimiter,
    _resolve_api_key,
    estimate_cost,
    get_provider_price,
    resolve_embedding_api_config,
    resolve_effective_llm_api_config,
    resolve_llm_api_chain,
    resolve_llm_api_config,
    resolve_reranker_api_config,
)


@pytest.fixture(autouse=True)  # noqa
def clean_llm_env(monkeypatch):
    """每个测试开始时清理 LLM 相关环境变量，避免宿主 shell 环境干扰。"""
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "SILICONFLOW_API_KEY",
        "SILICONFLOW_BASE_URL",
        "SILICONFLOW_MODEL",
        "SILICONFLOW_EMBEDDING_MODEL",
        "SILICONFLOW_RERANKER_MODEL",
        "DMXAPI_API_KEY",
        "DMX_API_KEY",
        "DMXAPI_BASE_URL",
        "DMXAPI_MODEL",
        "MNEMOS_LLM_API_KEY",
        "MNEMOS_LLM_BASE_URL",
        "MNEMOS_LLM_MODEL",
        "MNEMOS_LLM_PROVIDER",
        "MNEMOS_EMBEDDING_API_KEY",
        "MNEMOS_EMBEDDING_BASE_URL",
        "MNEMOS_EMBEDDING_MODEL",
        "MNEMOS_EMBEDDING_PROVIDER",
        "MNEMOS_RERANKER_API_KEY",
        "MNEMOS_RERANKER_BASE_URL",
        "MNEMOS_RERANKER_MODEL",
        "MNEMOS_RERANKER_PROVIDER",
        "MNEMOS_MULTIMODAL_API_KEY",
        "MNEMOS_MULTIMODAL_BASE_URL",
        "MNEMOS_MULTIMODAL_MODEL",
        "MNEMOS_MULTIMODAL_PROVIDER",
        "SILICONFLOW_MULTIMODAL_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


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


# ---------- LLMApiConfig ----------


class TestLLMApiConfig:
    def test_configured_true(self):
        cfg = LLMApiConfig("siliconflow", "key", "https://api.test/v1", "model", "test")
        assert cfg.configured is True

    def test_configured_false(self):
        cfg = LLMApiConfig("siliconflow", "", "https://api.test/v1", "model", "test")
        assert cfg.configured is False

    def test_masked_key_normal(self):
        cfg = LLMApiConfig("sf", "sk" + "-abcdefghijklmnopqrstuvwxyz", "url", "m", "s")
        assert cfg.masked_key() == "sk-a...wxyz"

    def test_masked_key_short(self):
        cfg = LLMApiConfig("sf", "short", "url", "m", "s")
        assert cfg.masked_key() == "***"

    def test_masked_key_empty(self):
        cfg = LLMApiConfig("sf", "", "url", "m", "s")
        assert cfg.masked_key() == ""


# ---------- LLMApiChain ----------


class TestLLMApiChain:
    def test_all_configs_single(self):
        primary = LLMApiConfig("sf", "k1", "url1", "m1", "s1")
        chain = LLMApiChain(primary=primary)
        assert chain.all_configs == [primary]

    def test_all_configs_with_backups(self):
        primary = LLMApiConfig("sf", "k1", "url1", "m1", "s1")
        same = LLMApiConfig("sf", "k2", "url2", "m2", "s2")
        cross = LLMApiConfig("openai", "k3", "url3", "m3", "s3")
        chain = LLMApiChain(primary=primary, same_provider_backup=same, cross_provider_backup=cross)
        assert chain.all_configs == [primary, same, cross]

    def test_all_configs_keeps_additional_backups(self):
        primary = LLMApiConfig("sf", "k1", "url1", "m1", "s1")
        cross = LLMApiConfig("dmxapi", "k2", "url2", "kimi-k2.5-free", "s2")
        extra = LLMApiConfig("dmxapi", "k3", "url3", "MiniMax-M2.7-free", "s3")
        chain = LLMApiChain(
            primary=primary,
            cross_provider_backup=cross,
            additional_backups=(extra,),
        )
        assert chain.all_configs == [primary, cross, extra]
        assert "3. dmxapi / MiniMax-M2.7-free" in chain.describe()

    def test_all_configs_skips_unconfigured_backup(self):
        primary = LLMApiConfig("sf", "k1", "url1", "m1", "s1")
        same = LLMApiConfig("sf", "", "url2", "m2", "s2")  # 未配置
        chain = LLMApiChain(primary=primary, same_provider_backup=same)
        assert chain.all_configs == [primary]

    def test_describe_single(self):
        primary = LLMApiConfig("siliconflow", "k", "url", "deepseek-v3", "s")
        chain = LLMApiChain(primary=primary)
        assert "siliconflow / deepseek-v3" in chain.describe()

    def test_describe_with_backups(self):
        primary = LLMApiConfig("sf", "k1", "url1", "m1", "s1")
        same = LLMApiConfig("sf", "k2", "url2", "m2", "s2")
        cross = LLMApiConfig("openai", "k3", "url3", "m3", "s3")
        chain = LLMApiChain(primary=primary, same_provider_backup=same, cross_provider_backup=cross)
        desc = chain.describe()
        assert "sf / m1" in desc
        assert "sf / m2" in desc
        assert "openai / m3" in desc
        assert "→" in desc


# ---------- resolve_llm_api_config ----------


class TestResolveLLMApiConfig:
    def test_resolve_siliconflow_env(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        monkeypatch.setenv("SILICONFLOW_API_KEY", "sf-test")

        cfg = resolve_llm_api_config(FakeConfig({}))

        assert cfg.configured is True
        assert cfg.provider == "siliconflow"
        assert cfg.api_key == "sf-test"
        assert "siliconflow" in cfg.base_url
        assert cfg.source == "env:SILICONFLOW_API_KEY"

    def test_resolve_openai_env(self, monkeypatch):
        """OPENAI_API_KEY 环境变量优先路径"""
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "oa-test")

        cfg = resolve_llm_api_config(FakeConfig({}))

        assert cfg.configured is True
        assert cfg.provider == "openai"
        assert cfg.api_key == "oa-test"
        assert cfg.source == "env:OPENAI_API_KEY"

    def test_resolve_dmxapi_env(self, monkeypatch):
        monkeypatch.setenv("DMXAPI_API_KEY", "dmx-test")

        cfg = resolve_llm_api_config(FakeConfig({}))

        assert cfg.configured is True
        assert cfg.provider == "dmxapi"
        assert cfg.api_key == "dmx-test"
        assert cfg.base_url == "https://www.dmxapi.cn/v1"
        assert cfg.source == "env:DMXAPI_API_KEY"

    def test_resolve_mnemos_llm_env_overrides_provider_base_model(self, monkeypatch):
        monkeypatch.setenv("MNEMOS_LLM_PROVIDER", "dmxapi")
        monkeypatch.setenv("MNEMOS_LLM_API_KEY", "mnemos-llm")
        monkeypatch.setenv("MNEMOS_LLM_BASE_URL", "https://gateway.example.test/v1/")
        monkeypatch.setenv("MNEMOS_LLM_MODEL", "custom-free-model")

        cfg = resolve_llm_api_config(FakeConfig({}))

        assert cfg.configured is True
        assert cfg.provider == "dmxapi"
        assert cfg.api_key == "mnemos-llm"
        assert cfg.base_url == "https://gateway.example.test/v1"
        assert cfg.model == "custom-free-model"
        assert cfg.source == "env:MNEMOS_LLM_API_KEY"

    def test_resolve_env_base_openai(self, monkeypatch):
        """OPENAI_BASE_URL 存在且不含 siliconflow → openai provider"""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_BASE_URL", "https://custom.openai.com/v1")
        monkeypatch.setenv("OPENAI_API_KEY", "custom-key")

        cfg = resolve_llm_api_config(FakeConfig({}))

        assert cfg.provider == "openai"
        assert cfg.base_url == "https://custom.openai.com/v1"
        assert cfg.source == "env:OPENAI_API_KEY"

    def test_resolve_env_base_siliconflow(self, monkeypatch):
        """OPENAI_BASE_URL 含 siliconflow → siliconflow provider"""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_BASE_URL", "https://api.siliconflow.cn/v1")
        monkeypatch.setenv("SILICONFLOW_API_KEY", "sf-key")

        cfg = resolve_llm_api_config(FakeConfig({}))

        assert cfg.provider == "siliconflow"
        assert cfg.source == "env:SILICONFLOW_API_KEY"

    def test_resolve_top_level_llm_config(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        monkeypatch.setenv("CFG_LLM_KEY", "cfg-key")

        cfg = resolve_llm_api_config(
            FakeConfig(
                {
                    "llm": {
                        "provider": "siliconflow",
                        "api_key_source": "env:CFG_LLM_KEY",
                        "base_url": "https://api.siliconflow.cn/v1",
                        "model": "deepseek-ai/DeepSeek-V3",
                    }
                }
            )
        )

        assert cfg.configured is True
        assert cfg.api_key == "cfg-key"
        assert cfg.source == "env:CFG_LLM_KEY"

    def test_top_level_llm_config_overrides_provider_defaults_and_env(self, monkeypatch):
        """初始用户填写的顶层 LLM 配置应覆盖全局 provider/env 默认。"""
        monkeypatch.setenv("SILICONFLOW_API_KEY", "global-sf-key")
        monkeypatch.setenv("PROVIDER_SF_KEY", "provider-sf-key")
        monkeypatch.setenv("USER_LLM_KEY", "user-llm-key")

        cfg = resolve_llm_api_config(
            FakeConfig(
                {
                    "llm": {
                        "provider": "siliconflow",
                        "api_key_source": "env:USER_LLM_KEY",
                        "base_url": "https://user-llm.example.test/v1",
                        "model": "user-chat-model",
                        "providers": {
                            "siliconflow": {
                                "api_key_source": "env:PROVIDER_SF_KEY",
                                "base_url": "https://provider.example.test/v1",
                                "model": "provider-model",
                            }
                        },
                    }
                }
            )
        )

        assert cfg.configured is True
        assert cfg.api_key == "user-llm-key"
        assert cfg.base_url == "https://user-llm.example.test/v1"
        assert cfg.model == "user-chat-model"
        assert cfg.source == "env:USER_LLM_KEY"

    def test_resolve_provider_config(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        monkeypatch.setenv("PROVIDER_OPENAI_KEY", "openai-key")

        cfg = resolve_llm_api_config(
            FakeConfig(
                {
                    "llm": {
                        "provider": "openai",
                        "providers": {
                            "openai": {
                                "api_key_source": "env:PROVIDER_OPENAI_KEY",
                                "base_url": "https://example.test/v1",
                                "model": "test-model",
                            }
                        },
                    }
                }
            )
        )

        assert cfg.configured is True
        assert cfg.provider == "openai"
        assert cfg.base_url == "https://example.test/v1"
        assert cfg.model == "test-model"
        assert cfg.source == "env:PROVIDER_OPENAI_KEY"

    def test_resolve_config_none_auto_load(self, monkeypatch):
        """config=None 时自动从 get_config() 加载"""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        monkeypatch.setenv("AUTO_LLM_KEY", "auto-key")

        fake_cfg = FakeConfig({"llm": {"api_key_source": "env:AUTO_LLM_KEY"}})
        with patch("core.config.get_config", return_value=fake_cfg):
            cfg = resolve_llm_api_config(None)

        assert cfg.configured is True
        assert cfg.api_key == "auto-key"
        assert cfg.source == "env:AUTO_LLM_KEY"

    def test_resolve_no_config_returns_missing(self, monkeypatch):
        """无任何配置时返回 missing，provider 默认 siliconflow"""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

        cfg = resolve_llm_api_config(FakeConfig({}))

        assert cfg.configured is False
        assert cfg.source == "missing"
        assert cfg.provider == "siliconflow"

    def test_embedding_key_does_not_fallback_to_llm(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

        cfg = resolve_llm_api_config(
            FakeConfig(
                {
                    "embedding": {
                        "provider": "siliconflow",
                        "api_key": "embed-key",
                        "base_url": "https://api.siliconflow.cn/v1",
                    }
                }
            )
        )

        assert cfg.configured is False
        assert cfg.provider == "siliconflow"
        assert cfg.source == "missing"

    def test_embedding_any_provider_does_not_fallback_to_llm(self, monkeypatch):
        """embedding provider 任意 provider 都不能隐式成为 LLM provider。"""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

        cfg = resolve_llm_api_config(
            FakeConfig(
                {
                    "embedding": {
                        "provider": "openai",
                        "api_key": "embed-key",
                    }
                }
            )
        )

        assert cfg.configured is False
        assert cfg.provider == "siliconflow"
        assert cfg.source == "missing"


# ---------- resolve_embedding_api_config / resolve_reranker_api_config ----------


class TestResolveVectorModelApiConfig:
    @pytest.mark.parametrize(
        "provider,base_url,llm_model,embedding_model,reranker_model",
        [
            (
                "siliconflow",
                "https://api.siliconflow.cn/v1",
                "deepseek-ai/DeepSeek-V4-Flash",
                "BAAI/bge-m3",
                "BAAI/bge-reranker-v2-m3",
            ),
            (
                "dmxapi",
                "https://www.dmxapi.cn/v1",
                "kimi-k2.5-free",
                "BAAI/bge-m3",
                "BAAI/bge-reranker-v2-m3",
            ),
            (
                "openai-compatible",
                "https://gateway.example.test/v1",
                "custom-chat-model",
                "custom-embedding-model",
                "custom-reranker-model",
            ),
        ],
    )
    def test_provider_matrix_resolves_three_model_kinds(
        self, provider, base_url, llm_model, embedding_model, reranker_model, monkeypatch
    ):
        """用户替换 provider/base_url/model 后，三类模型配置应独立生效。"""
        monkeypatch.setenv("MATRIX_LLM_KEY", "llm-key")
        monkeypatch.setenv("MATRIX_EMBEDDING_KEY", "embedding-key")
        monkeypatch.setenv("MATRIX_RERANKER_KEY", "reranker-key")
        cfg = FakeConfig(
            {
                "llm": {
                    "provider": provider,
                    "api_key_source": "env:MATRIX_LLM_KEY",
                    "base_url": base_url + "/",
                    "model": llm_model,
                },
                "embedding": {
                    "provider": provider,
                    "api_key_source": "env:MATRIX_EMBEDDING_KEY",
                    "base_url": base_url + "/",
                    "model": embedding_model,
                },
                "reranker": {
                    "provider": provider,
                    "api_key_source": "env:MATRIX_RERANKER_KEY",
                    "base_url": base_url + "/",
                    "model": reranker_model,
                },
            }
        )

        llm_cfg = resolve_llm_api_config(cfg)
        embedding_cfg = resolve_embedding_api_config(cfg)
        reranker_cfg = resolve_reranker_api_config(cfg)

        assert llm_cfg.configured is True
        assert llm_cfg.provider == provider
        assert llm_cfg.api_key == "llm-key"
        assert llm_cfg.base_url == base_url
        assert llm_cfg.model == llm_model

        assert embedding_cfg.configured is True
        assert embedding_cfg.kind == "embedding"
        assert embedding_cfg.provider == provider
        assert embedding_cfg.api_key == "embedding-key"
        assert embedding_cfg.base_url == base_url
        assert embedding_cfg.model == embedding_model

        assert reranker_cfg.configured is True
        assert reranker_cfg.kind == "reranker"
        assert reranker_cfg.provider == provider
        assert reranker_cfg.api_key == "reranker-key"
        assert reranker_cfg.base_url == base_url
        assert reranker_cfg.model == reranker_model

    def test_embedding_env_is_independent_from_llm(self, monkeypatch):
        monkeypatch.setenv("MNEMOS_EMBEDDING_API_KEY", "embed-env")
        monkeypatch.setenv("MNEMOS_EMBEDDING_BASE_URL", "https://embed.example.test/v1/")
        monkeypatch.setenv("MNEMOS_EMBEDDING_MODEL", "custom-embedding")

        embedding_cfg = resolve_embedding_api_config(FakeConfig({}))
        llm_cfg = resolve_llm_api_config(FakeConfig({}))

        assert embedding_cfg.configured is True
        assert embedding_cfg.kind == "embedding"
        assert embedding_cfg.provider == "siliconflow"
        assert embedding_cfg.api_key == "embed-env"
        assert embedding_cfg.base_url == "https://embed.example.test/v1"
        assert embedding_cfg.model == "custom-embedding"
        assert embedding_cfg.source == "env:MNEMOS_EMBEDDING_API_KEY"
        assert llm_cfg.configured is False

    def test_embedding_config_supports_api_key_env(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_KEY", "embed-key")

        cfg = resolve_embedding_api_config(
            FakeConfig(
                {
                    "embedding": {
                        "provider": "siliconflow",
                        "api_key_env": "EMBEDDING_KEY",
                        "base_url": "https://api.siliconflow.cn/v1/",
                        "model": "BAAI/custom-embed",
                    }
                }
            )
        )

        assert cfg.configured is True
        assert cfg.api_key == "embed-key"
        assert cfg.base_url == "https://api.siliconflow.cn/v1"
        assert cfg.model == "BAAI/custom-embed"
        assert cfg.source == "env:EMBEDDING_KEY"

    def test_reranker_config_is_independent_from_embedding(self, monkeypatch):
        monkeypatch.setenv("RERANKER_KEY", "rerank-key")

        reranker_cfg = resolve_reranker_api_config(
            FakeConfig(
                {
                    "reranker": {
                        "provider": "siliconflow",
                        "api_key_env": "RERANKER_KEY",
                        "base_url": "https://rerank.example.test/v1",
                        "model": "BAAI/custom-rerank",
                    }
                }
            )
        )
        embedding_cfg = resolve_embedding_api_config(FakeConfig({}))

        assert reranker_cfg.configured is True
        assert reranker_cfg.kind == "reranker"
        assert reranker_cfg.api_key == "rerank-key"
        assert reranker_cfg.base_url == "https://rerank.example.test/v1"
        assert reranker_cfg.model == "BAAI/custom-rerank"
        assert reranker_cfg.source == "env:RERANKER_KEY"
        assert embedding_cfg.configured is False

    def test_vector_model_config_overrides_global_siliconflow_env(self, monkeypatch):
        """Embedding/Reranker 用户配置应覆盖 SiliconFlow 全局 key/model。"""
        monkeypatch.setenv("SILICONFLOW_API_KEY", "global-sf-key")
        monkeypatch.setenv("SILICONFLOW_EMBEDDING_MODEL", "global-embedding")
        monkeypatch.setenv("SILICONFLOW_RERANKER_MODEL", "global-reranker")
        monkeypatch.setenv("USER_EMBEDDING_KEY", "user-embedding-key")
        monkeypatch.setenv("USER_RERANKER_KEY", "user-reranker-key")

        cfg = FakeConfig(
            {
                "embedding": {
                    "provider": "openai-compatible",
                    "api_key_source": "env:USER_EMBEDDING_KEY",
                    "base_url": "https://embedding.example.test/v1",
                    "model": "user-embedding-model",
                },
                "reranker": {
                    "provider": "openai-compatible",
                    "api_key_source": "env:USER_RERANKER_KEY",
                    "base_url": "https://reranker.example.test/v1",
                    "model": "user-reranker-model",
                },
            }
        )

        embedding_cfg = resolve_embedding_api_config(cfg)
        reranker_cfg = resolve_reranker_api_config(cfg)

        assert embedding_cfg.api_key == "user-embedding-key"
        assert embedding_cfg.provider == "openai-compatible"
        assert embedding_cfg.model == "user-embedding-model"
        assert reranker_cfg.api_key == "user-reranker-key"
        assert reranker_cfg.provider == "openai-compatible"
        assert reranker_cfg.model == "user-reranker-model"


# ---------- _resolve_api_key ----------


class TestResolveApiKey:
    def test_env_prefix(self, monkeypatch):
        monkeypatch.setenv("MY_API_KEY", "secret123")
        key, source = _resolve_api_key("", "env:MY_API_KEY")
        assert key == "secret123"
        assert source == "env:MY_API_KEY"

    def test_env_prefix_missing(self, monkeypatch):
        monkeypatch.delenv("MISSING_KEY", raising=False)
        key, source = _resolve_api_key("", "env:MISSING_KEY")
        assert key == ""
        assert source == "env:MISSING_KEY"

    def test_direct_source_value_is_rejected(self):
        key, source = _resolve_api_key("", "direct-key-value")
        assert key == ""
        assert source == "invalid:plaintext_api_key_source"

    def test_fallback_to_api_key_field_is_rejected(self):
        key, source = _resolve_api_key("field-key", "")
        assert key == ""
        assert source == "invalid:plaintext_api_key"

    def test_both_empty(self):
        key, source = _resolve_api_key("", "")
        assert key == ""
        assert source == "missing"


# ---------- resolve_llm_api_chain ----------


class TestResolveLLMApiChain:
    def test_chain_from_config(self, monkeypatch):
        """llm.chain 多节点配置解析"""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
        monkeypatch.setenv("CHAIN_PRIMARY_KEY", "primary-key")
        monkeypatch.setenv("CHAIN_SAME_KEY", "same-key")
        monkeypatch.setenv("CHAIN_CROSS_KEY", "cross-key")

        fake_cfg = FakeConfig(
            {
                "llm": {
                    "chain": [
                        {
                            "provider": "siliconflow",
                            "base_url": "https://api.siliconflow.cn/v1",
                            "model": "deepseek-v3",
                            "api_key_source": "env:CHAIN_PRIMARY_KEY",
                        },
                        {
                            "provider": "siliconflow",
                            "base_url": "https://backup.siliconflow.cn/v1",
                            "model": "deepseek-v3-backup",
                            "api_key_source": "env:CHAIN_SAME_KEY",
                        },
                        {
                            "provider": "openai",
                            "base_url": "https://api.openai.com/v1",
                            "model": "gpt-4o",
                            "api_key_source": "env:CHAIN_CROSS_KEY",
                        },
                    ]
                }
            }
        )

        chain = resolve_llm_api_chain(fake_cfg)

        assert chain.primary.provider == "siliconflow"
        assert chain.primary.api_key == "primary-key"
        assert chain.same_provider_backup is not None
        assert chain.same_provider_backup.provider == "siliconflow"
        assert chain.same_provider_backup.api_key == "same-key"
        assert chain.cross_provider_backup is not None
        assert chain.cross_provider_backup.provider == "openai"
        assert chain.cross_provider_backup.api_key == "cross-key"

    def test_default_config_prefers_deepseek_then_free_backups(self, monkeypatch):
        """仓库默认 LLM 链应先用 DeepSeek V4 Flash，再顺序兜底免费模型。"""
        from core.config import DEFAULT_CONFIG

        monkeypatch.setenv("SILICONFLOW_API_KEY", "sf-key")
        monkeypatch.setenv("DMXAPI_API_KEY", "dmx-key")

        chain = resolve_llm_api_chain(FakeConfig(DEFAULT_CONFIG))

        assert [cfg.provider for cfg in chain.all_configs] == [
            "siliconflow",
            "dmxapi",
            "dmxapi",
        ]
        assert [cfg.model for cfg in chain.all_configs] == [
            "deepseek-ai/DeepSeek-V4-Flash",
            "kimi-k2.5-free",
            "MiniMax-M2.7-free",
        ]
        assert chain.primary.cost_level == "paid"
        assert chain.cross_provider_backup is not None
        assert chain.additional_backups

    def test_top_level_config_overrides_default_global_chain(self, monkeypatch):
        """默认全局 chain 不应遮蔽安装阶段用户填写的顶层 LLM 信息。"""
        monkeypatch.setenv("SILICONFLOW_API_KEY", "global-sf-key")
        monkeypatch.setenv("DMXAPI_API_KEY", "global-dmx-key")
        monkeypatch.setenv("USER_LLM_KEY", "user-llm-key")

        fake_cfg = FakeConfig(
            {
                "llm": {
                    "provider": "openai-compatible",
                    "base_url": "https://user-llm.example.test/v1",
                    "model": "user-chat-model",
                    "api_key_source": "env:USER_LLM_KEY",
                    "chain": [
                        {
                            "provider": "siliconflow",
                            "base_url": "https://api.siliconflow.cn/v1",
                            "model": "deepseek-ai/DeepSeek-V4-Flash",
                            "api_key_source": "env:SILICONFLOW_API_KEY",
                        },
                        {
                            "provider": "dmxapi",
                            "base_url": "https://www.dmxapi.cn/v1",
                            "model": "kimi-k2.5-free",
                            "api_key_source": "env:DMXAPI_API_KEY",
                        },
                        {
                            "provider": "dmxapi",
                            "base_url": "https://www.dmxapi.cn/v1",
                            "model": "MiniMax-M2.7-free",
                            "api_key_source": "env:DMXAPI_API_KEY",
                        },
                    ],
                }
            }
        )

        chain = resolve_llm_api_chain(fake_cfg)

        assert chain.primary.provider == "openai-compatible"
        assert chain.primary.api_key == "user-llm-key"
        assert chain.primary.base_url == "https://user-llm.example.test/v1"
        assert chain.primary.model == "user-chat-model"
        assert chain.same_provider_backup is None
        assert chain.cross_provider_backup is None

    def test_chain_skips_incomplete_nodes(self, monkeypatch, caplog):
        """不完整的 chain 节点应被跳过"""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
        monkeypatch.setenv("CHAIN_PRIMARY_KEY", "primary-key")

        fake_cfg = FakeConfig(
            {
                "llm": {
                    "chain": [
                        {
                            "provider": "siliconflow",
                            "base_url": "https://api.siliconflow.cn/v1",
                            "model": "deepseek-v3",
                            "api_key_source": "env:CHAIN_PRIMARY_KEY",
                        },
                        {
                            "provider": "",
                            "base_url": "",
                            "model": "",
                            "api_key": "bad-key",
                        },
                    ]
                }
            }
        )

        chain = resolve_llm_api_chain(fake_cfg)

        assert chain.primary.api_key == "primary-key"
        assert chain.same_provider_backup is None
        assert chain.cross_provider_backup is None
        assert "跳过不完整配置" in caplog.text

    def test_chain_empty_fallback_to_single(self, monkeypatch):
        """llm.chain 为空时回退到 resolve_llm_api_config"""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        monkeypatch.setenv("SINGLE_CHAIN_KEY", "single-key")

        fake_cfg = FakeConfig(
            {
                "llm": {
                    "api_key_source": "env:SINGLE_CHAIN_KEY",
                    "provider": "siliconflow",
                },
                "llm.chain": [],
            }
        )

        chain = resolve_llm_api_chain(fake_cfg)

        assert chain.primary.api_key == "single-key"
        assert chain.same_provider_backup is None
        assert chain.cross_provider_backup is None

    def test_chain_env_source(self, monkeypatch):
        """chain 节点支持 api_key_source 指向环境变量"""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
        monkeypatch.setenv("CHAIN_API_KEY", "from-env")

        fake_cfg = FakeConfig(
            {
                "llm": {
                    "chain": [
                        {
                            "provider": "siliconflow",
                            "base_url": "https://api.siliconflow.cn/v1",
                            "model": "deepseek-v3",
                            "api_key": "",
                            "api_key_source": "env:CHAIN_API_KEY",
                        },
                    ]
                }
            }
        )

        chain = resolve_llm_api_chain(fake_cfg)

        assert chain.primary.api_key == "from-env"
        assert chain.primary.source == "env:CHAIN_API_KEY"

    def test_chain_skips_unconfigured_nodes(self, monkeypatch):
        """chain 节点存在但 API key 未配置时，使用后续可用节点。"""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
        monkeypatch.setenv("CHAIN_READY_KEY", "ready-key")

        fake_cfg = FakeConfig(
            {
                "llm": {
                    "chain": [
                        {
                            "provider": "dmxapi",
                            "base_url": "https://www.dmxapi.cn/v1",
                            "model": "kimi-k2.5-free",
                            "api_key_source": "env:MISSING_DMX_KEY",
                        },
                        {
                            "provider": "siliconflow",
                            "base_url": "https://api.siliconflow.cn/v1",
                            "model": "deepseek-v3",
                            "api_key_source": "env:CHAIN_READY_KEY",
                        },
                    ]
                }
            }
        )

        chain = resolve_llm_api_chain(fake_cfg)

        assert chain.primary.provider == "siliconflow"
        assert chain.primary.api_key == "ready-key"

    def test_unconfigured_chain_falls_back_to_provider_env(self, monkeypatch):
        """默认 chain 未配置时，不应遮蔽运行时支持的 provider 环境变量。"""
        monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
        monkeypatch.delenv("DMXAPI_API_KEY", raising=False)
        monkeypatch.delenv("DMX_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

        fake_cfg = FakeConfig(
            {
                "llm": {
                    "chain": [
                        {
                            "provider": "dmxapi",
                            "base_url": "https://www.dmxapi.cn/v1",
                            "model": "kimi-k2.5-free",
                            "api_key_source": "env:MISSING_DMX_KEY",
                        }
                    ]
                }
            }
        )

        chain = resolve_llm_api_chain(fake_cfg)

        assert chain.primary.provider == "openai"
        assert chain.primary.api_key == "openai-key"
        assert chain.primary.source == "env:OPENAI_API_KEY"

    def test_chain_config_none_auto_load(self, monkeypatch):
        """config=None 时自动加载 get_config()"""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
        monkeypatch.setenv("AUTO_CHAIN_KEY", "auto-chain-key")

        fake_cfg = FakeConfig(
            {
                "llm": {
                    "api_key_source": "env:AUTO_CHAIN_KEY",
                    "provider": "siliconflow",
                }
            }
        )
        with patch("core.config.get_config", return_value=fake_cfg):
            chain = resolve_llm_api_chain(None)

        assert chain.primary.api_key == "auto-chain-key"

    def test_effective_config_uses_runtime_chain_primary(self, monkeypatch):
        """状态/验证入口应与真实运行链路使用同一个 LLM 主节点。"""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
        monkeypatch.setenv("CHAIN_PRIMARY_KEY", "chain-key")
        monkeypatch.setenv("TOP_LEVEL_KEY", "legacy-key")

        fake_cfg = FakeConfig(
            {
                "llm": {
                    "provider": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-4o-mini",
                    "api_key_source": "env:TOP_LEVEL_KEY",
                    "chain": [
                        {
                            "provider": "siliconflow",
                            "base_url": "https://api.siliconflow.cn/v1",
                            "model": "deepseek-ai/DeepSeek-V4-Flash",
                            "api_key_source": "env:CHAIN_PRIMARY_KEY",
                        }
                    ],
                }
            }
        )

        cfg = resolve_effective_llm_api_config(fake_cfg)

        assert cfg.provider == "siliconflow"
        assert cfg.model == "deepseek-ai/DeepSeek-V4-Flash"
        assert cfg.api_key == "chain-key"


# ---------- ProviderRateLimiter ----------


class TestProviderRateLimiter:
    def test_unknown_provider_has_no_limit(self):
        rl = ProviderRateLimiter(FakeConfig({}))
        start = time.monotonic()
        for _ in range(10):
            rl.acquire("unknown")
        assert time.monotonic() - start < 0.1

    def test_dmxapi_default_five_per_minute(self):
        rl = ProviderRateLimiter(FakeConfig({}))
        start = time.monotonic()
        for _ in range(5):
            rl.acquire("dmxapi")
        assert time.monotonic() - start < 0.1

        with patch("core.llm_config.time.sleep") as mock_sleep:
            rl.acquire("dmxapi")
            mock_sleep.assert_called_once()
            assert mock_sleep.call_args[0][0] > 50

    def test_config_rpm_and_tpm_limits(self):
        rl = ProviderRateLimiter(
            FakeConfig(
                {
                    "llm": {
                        "rate_limits": {
                            "siliconflow": {"rpm": 2, "tpm": 100},
                        },
                    },
                }
            )
        )
        # 两次小请求不触发限流
        rl.acquire("siliconflow", estimated_tokens=30)
        rl.acquire("siliconflow", estimated_tokens=30)

        # 第三次 30 tokens 会触发 TPM 限制（当前窗口 60 + 30 > 100）
        with patch("core.llm_config.time.sleep") as mock_sleep:
            rl.acquire("siliconflow", estimated_tokens=30)
            mock_sleep.assert_called_once()

    def test_rpm_limit_blocks_after_threshold(self):
        rl = ProviderRateLimiter(
            FakeConfig(
                {
                    "llm": {
                        "rate_limits": {
                            "siliconflow": {"rpm": 1},
                        },
                    },
                }
            )
        )
        rl.acquire("siliconflow")
        with patch("core.llm_config.time.sleep") as mock_sleep:
            rl.acquire("siliconflow")
            mock_sleep.assert_called_once()

    def test_per_model_rpm_limits_are_independent(self):
        # 显式放大 provider 级限制，确保只测试 per-model 限制
        rl = ProviderRateLimiter(
            FakeConfig(
                {
                    "llm": {
                        "rate_limits": {
                            "dmxapi": {
                                "rpm": 100,
                                "models": {
                                    "kimi-k2.5-free": {"rpm": 2},
                                    "MiniMax-M2.7-free": {"rpm": 2},
                                },
                            },
                        },
                    },
                }
            )
        )
        # kimi 达到 2 RPM
        rl.acquire("dmxapi", "kimi-k2.5-free")
        rl.acquire("dmxapi", "kimi-k2.5-free")

        # MiniMax 仍可用（per-model 独立）
        rl.acquire("dmxapi", "MiniMax-M2.7-free")
        rl.acquire("dmxapi", "MiniMax-M2.7-free")

        # kimi 再次调用应被限流
        with patch("core.llm_config.time.sleep") as mock_sleep:
            rl.acquire("dmxapi", "kimi-k2.5-free")
            mock_sleep.assert_called_once()

        # MiniMax 也被限流
        with patch("core.llm_config.time.sleep") as mock_sleep:
            rl.acquire("dmxapi", "MiniMax-M2.7-free")
            mock_sleep.assert_called_once()

    def test_can_acquire_reflects_current_window(self):
        rl = ProviderRateLimiter(
            FakeConfig(
                {
                    "llm": {
                        "rate_limits": {
                            "dmxapi": {
                                "models": {"kimi-k2.5-free": {"rpm": 2}},
                            },
                        },
                    },
                }
            )
        )
        assert rl.can_acquire("dmxapi", "kimi-k2.5-free") is True
        rl.acquire("dmxapi", "kimi-k2.5-free")
        assert rl.can_acquire("dmxapi", "kimi-k2.5-free") is True
        rl.acquire("dmxapi", "kimi-k2.5-free")
        assert rl.can_acquire("dmxapi", "kimi-k2.5-free") is False

    def test_per_model_limit_with_provider_default(self):
        """per-model 限制与 provider 默认限制同时生效。"""
        # dmxapi provider 默认 5 RPM，kimi per-model 限制 2 RPM
        # 先通过配置设置 per-model
        rl2 = ProviderRateLimiter(
            FakeConfig(
                {
                    "llm": {
                        "rate_limits": {
                            "dmxapi": {
                                "models": {"kimi-k2.5-free": {"rpm": 2}},
                            },
                        },
                    },
                }
            )
        )
        rl2.acquire("dmxapi", "kimi-k2.5-free")
        rl2.acquire("dmxapi", "kimi-k2.5-free")
        with patch("core.llm_config.time.sleep") as mock_sleep:
            rl2.acquire("dmxapi", "kimi-k2.5-free")
            mock_sleep.assert_called_once()


class TestProviderPrice:
    def test_get_provider_price_case_insensitive_model_name(self):
        """模型名大小写不敏感：DEFAULT_PROVIDER_PRICES 中大写 key 也能被小写 model 命中。"""
        assert get_provider_price("siliconflow", "deepseek-ai/DeepSeek-V4-Flash") == {
            "input": 0.0005,
            "output": 0.001,
        }
        assert get_provider_price("siliconflow", "deepseek-ai/deepseek-v4-flash") == {
            "input": 0.0005,
            "output": 0.001,
        }
        assert get_provider_price("dmxapi", "MiniMax-M2.7-free") == {"input": 0.0, "output": 0.0}
        assert get_provider_price("dmxapi", "minimax-m2.7-free") == {"input": 0.0, "output": 0.0}

    def test_user_provider_prices_case_insensitive(self):
        """用户自定义价格表中的模型名也大小写不敏感。"""
        config = FakeConfig(
            {
                "llm": {
                    "provider_prices": {
                        "siliconflow": {
                            "DeepSeek-V4-Flash": {"input": 0.001, "output": 0.002},
                        },
                    },
                },
            }
        )
        assert get_provider_price("siliconflow", "deepseek-v4-flash", config) == {
            "input": 0.001,
            "output": 0.002,
        }

    def test_estimate_cost_with_case_insensitive_price(self):
        """成本估算应正确命中大小写混合的价格配置。"""
        cost = estimate_cost("siliconflow", "deepseek-ai/DeepSeek-V4-Flash", 2000, 1000)
        # input 0.0005/1K + output 0.001/1K
        assert cost == 2000 / 1000 * 0.0005 + 1000 / 1000 * 0.001


class TestMultiKeyPool:
    def test_top_level_api_key_sources_builds_pool(self, monkeypatch):
        monkeypatch.setenv("SF_KEY_A", "sf-a")
        monkeypatch.setenv("SF_KEY_B", "sf-b")

        cfg = resolve_llm_api_config(
            FakeConfig(
                {
                    "llm": {
                        "provider": "siliconflow",
                        "api_key_sources": ["env:SF_KEY_A", "env:SF_KEY_B"],
                    }
                }
            )
        )

        assert cfg.configured is True
        assert cfg.pool is not None
        active = cfg.active()
        assert active.api_key in {"sf-a", "sf-b"}
        assert active.provider == "siliconflow"

    def test_provider_level_api_key_sources_builds_pool(self, monkeypatch):
        monkeypatch.setenv("OA_KEY_A", "oa-a")
        monkeypatch.setenv("OA_KEY_B", "oa-b")

        cfg = resolve_llm_api_config(
            FakeConfig(
                {
                    "llm": {
                        "provider": "openai",
                        "providers": {
                            "openai": {
                                "api_key_sources": ["env:OA_KEY_A", "env:OA_KEY_B"],
                            }
                        },
                    }
                }
            )
        )

        assert cfg.configured is True
        assert cfg.pool is not None
        assert cfg.active().api_key in {"oa-a", "oa-b"}

    def test_chain_node_api_key_sources_builds_pool(self, monkeypatch):
        monkeypatch.setenv("DMX_KEY_A", "dmx-a")
        monkeypatch.setenv("DMX_KEY_B", "dmx-b")

        chain = resolve_llm_api_chain(
            FakeConfig(
                {
                    "llm": {
                        "chain": [
                            {
                                "provider": "dmxapi",
                                "base_url": "https://www.dmxapi.cn/v1",
                                "model": "kimi-k2.5-free",
                                "api_key_sources": ["env:DMX_KEY_A", "env:DMX_KEY_B"],
                            }
                        ]
                    }
                }
            )
        )

        assert chain.primary.configured is True
        assert chain.primary.pool is not None
        assert chain.primary.active().api_key in {"dmx-a", "dmx-b"}

    def test_api_key_sources_missing_env_results_unconfigured(self):
        cfg = resolve_llm_api_config(
            FakeConfig(
                {
                    "llm": {
                        "provider": "siliconflow",
                        "api_key_sources": ["env:MISSING_KEY_A", "env:MISSING_KEY_B"],
                    }
                }
            )
        )

        assert cfg.configured is False
        assert cfg.pool is None
