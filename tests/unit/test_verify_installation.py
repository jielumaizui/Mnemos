import hashlib
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.setup.vault_layout import init_vaults
from core.telemetry.provider_request import (
    canonical_chat_input,
    canonical_provider_input,
    utf8_token_upper_bound,
)


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


class FakeVaultConfig:
    def __init__(self, mnemos_vault, raw_vault):
        self.mnemos_vault = mnemos_vault
        self.raw_vault = raw_vault

    def vault_dir(self, name):
        return self.mnemos_vault if name == "mnemos" else self.raw_vault


def clear_model_api_env(monkeypatch):
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "SILICONFLOW_API_KEY",
        "SILICONFLOW_BASE_URL",
        "SILICONFLOW_MODEL",
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
    ):
        monkeypatch.delenv(name, raising=False)


def test_check_doctor_uses_runtime_timeout_budget(monkeypatch):
    from scripts import verify_installation

    captured = {}

    class Result:
        stdout = "知识库健康度"
        stderr = ""

    def fake_run(*args, **kwargs):
        captured["timeout"] = kwargs["timeout"]
        return Result()

    monkeypatch.setattr(verify_installation.subprocess, "run", fake_run)

    result = verify_installation.check_doctor()

    assert result["ok"] is True
    assert captured["timeout"] == verify_installation.DOCTOR_TIMEOUT_SECONDS
    assert captured["timeout"] >= 60


def test_check_integration_tests_default_reports_skipped_not_success(capsys):
    from scripts import verify_installation

    result = verify_installation.check_integration_tests(full=False)
    captured = capsys.readouterr()

    assert result["ok"] is False
    assert result["status"] == "skipped"
    assert result["skipped"] is True
    assert result["passed"] == 0
    assert result["failed"] == 0
    assert "跳过集成测试" in captured.out


def test_run_verification_default_is_basic_not_full_ready(monkeypatch):
    from scripts import verify_installation

    monkeypatch.setattr(verify_installation, "check_python_version", lambda: True)
    monkeypatch.setattr(verify_installation, "check_compileall", lambda: True)
    monkeypatch.setattr(verify_installation, "check_cli_help", lambda: True)
    monkeypatch.setattr(verify_installation, "check_daemon_import", lambda: True)
    monkeypatch.setattr(verify_installation, "check_db_writable", lambda: True)
    monkeypatch.setattr(
        verify_installation,
        "check_obsidian_and_vaults",
        lambda: {"ok": True, "warnings": [], "errors": []},
    )
    monkeypatch.setattr(
        verify_installation,
        "check_model_api_config",
        lambda **_kwargs: {"ok": True, "apis": {}, "warnings": [], "errors": []},
    )
    monkeypatch.setattr(
        verify_installation,
        "check_agent_full_power",
        lambda: {"ok": True, "degraded_agents": []},
    )
    monkeypatch.setattr(
        verify_installation,
        "check_doctor",
        lambda: {"ok": True, "warnings": [], "errors": []},
    )
    monkeypatch.setattr(
        verify_installation,
        "check_integration_tests",
        lambda full: {
            "ok": False,
            "status": "skipped",
            "passed": 0,
            "failed": 0,
            "skipped": True,
            "required_for_full_verification": True,
        },
    )

    ok, payload = verify_installation.run_verification(show_sensitive=True)

    assert ok is True
    assert payload["ok"] is True
    assert payload["verification_level"] == "basic"
    assert payload["full_verification_ok"] is False
    assert payload["skipped_checks"] == ["integration_tests"]
    assert payload["results"]["integration_tests"] == "skipped"
    assert payload["integration_tests"]["status"] == "skipped"


def test_path_writable_does_not_reuse_shared_probe_name(tmp_path):
    from scripts import verify_installation

    sentinel = tmp_path / ".mnemos_write_test"
    sentinel.write_text("owned-by-another-process", encoding="utf-8")

    assert verify_installation._path_writable(tmp_path) is True
    assert sentinel.read_text(encoding="utf-8") == "owned-by-another-process"


def test_path_readable_default_does_not_create_missing_directory(tmp_path):
    from scripts import verify_installation

    missing = tmp_path / "missing"

    assert verify_installation._path_accessible(missing, write_probe=False) is False
    assert not missing.exists()


def test_verify_checks_obsidian_and_vaults_without_requiring_app(tmp_path, monkeypatch):
    from scripts.auto_setup import generate_config
    from scripts import auto_setup, verify_installation
    import core.config

    mnemos_dir = tmp_path / ".mnemos"
    mnemos_vault = tmp_path / "vault" / "mnemos"
    raw_vault = tmp_path / "vault" / "raw"
    monkeypatch.setenv("MNEMOS_DIR", str(mnemos_dir))
    monkeypatch.setenv("SILICONFLOW_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(auto_setup, "_smoke_required_model_endpoints", lambda data: (True, {}))
    generate_config(mnemos_vault, raw_vault, yes_mode=True)
    init_vaults(mnemos_vault, raw_vault)
    monkeypatch.setattr(core.config, "get_config", lambda: FakeVaultConfig(mnemos_vault, raw_vault))

    monkeypatch.setattr(
        "scripts.auto_setup.detect_obsidian_app",
        lambda: (False, None, None),
    )

    result = verify_installation.check_obsidian_and_vaults()

    assert result["ok"] is True
    assert result["obsidian"]["installed"] is False
    assert any("https://obsidian.md/download" in warning for warning in result["warnings"])
    assert result["vaults"]["mnemos"]["writable"] is True
    assert result["vaults"]["raw"]["writable"] is True
    assert result["vaults"]["mnemos"]["standard_dirs"]["missing"] == []


def test_verify_checks_mnemos_vault_standard_dirs(tmp_path, monkeypatch):
    from scripts import verify_installation
    import core.config

    mnemos_vault = tmp_path / "vault" / "mnemos"
    raw_vault = tmp_path / "vault" / "raw"
    init_vaults(mnemos_vault, raw_vault)
    (mnemos_vault / "L3-Observations").rmdir()
    monkeypatch.setattr(core.config, "get_config", lambda: FakeVaultConfig(mnemos_vault, raw_vault))

    monkeypatch.setattr(
        "scripts.auto_setup.detect_obsidian_app",
        lambda: (False, None, None),
    )

    result = verify_installation.check_obsidian_and_vaults()

    assert result["ok"] is False
    assert result["vaults"]["mnemos"]["standard_dirs"]["missing"] == ["L3-Observations"]
    assert any("L3-Observations" in error for error in result["errors"])


def test_verify_model_api_config_reports_configured_without_network(capsys, monkeypatch):
    from scripts import verify_installation

    clear_model_api_env(monkeypatch)
    monkeypatch.setenv("VERIFY_LLM_KEY", "llm-key")
    monkeypatch.setenv("VERIFY_EMBEDDING_KEY", "embedding-key")
    monkeypatch.setenv("VERIFY_RERANKER_KEY", "reranker-key")
    cfg = FakeConfig(
        {
            "llm": {
                "provider": "dmxapi",
                "api_key_source": "env:VERIFY_LLM_KEY",
                "base_url": "https://www.dmxapi.cn/v1",
                "model": "kimi-k2.5-free",
            },
            "embedding": {
                "provider": "siliconflow",
                "api_key_source": "env:VERIFY_EMBEDDING_KEY",
                "base_url": "https://api.siliconflow.cn/v1",
                "model": "BAAI/bge-m3",
            },
            "reranker": {
                "provider": "openai-compatible",
                "api_key_source": "env:VERIFY_RERANKER_KEY",
                "base_url": "https://gateway.example.test/v1",
                "model": "custom-reranker",
            },
        }
    )

    result = verify_installation.check_model_api_config(config=cfg, api_smoke=False)
    captured = capsys.readouterr()

    assert result["ok"] is True
    assert result["apis"]["llm"]["status"] == "configured"
    assert result["apis"]["embedding"]["status"] == "configured"
    assert result["apis"]["reranker"]["status"] == "configured"
    assert result["apis"]["llm"]["base_url"] == "https://****/v1"
    assert result["apis"]["llm"]["source"] == "env:****"
    assert result["apis"]["embedding"]["model"] == "BAAI/bge-m3"
    assert "llm-key" not in captured.out
    assert "embedding-key" not in captured.out
    assert "reranker-key" not in captured.out
    assert "www.dmxapi.cn" not in captured.out


def test_verify_model_api_config_unsafe_debug_returns_local_values(capsys, monkeypatch):
    from scripts import verify_installation

    clear_model_api_env(monkeypatch)
    monkeypatch.setenv("VERIFY_LLM_KEY", "llm-key")
    cfg = FakeConfig(
        {
            "llm": {
                "provider": "dmxapi",
                "api_key_source": "env:VERIFY_LLM_KEY",
                "base_url": "https://www.dmxapi.cn/v1",
                "model": "kimi-k2.5-free",
            },
            "embedding": {},
            "reranker": {},
        }
    )

    result = verify_installation.check_model_api_config(
        config=cfg,
        api_smoke=False,
        show_sensitive=True,
    )
    capsys.readouterr()

    assert result["apis"]["llm"]["base_url"] == "https://www.dmxapi.cn/v1"
    assert result["apis"]["llm"]["source"] == "env:VERIFY_LLM_KEY"


def test_model_api_provider_env_status_is_consistent_with_doctor(monkeypatch):
    from core.ops.health_check import _check_api
    from scripts import verify_installation

    clear_model_api_env(monkeypatch)
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sf-key")
    cfg = FakeConfig(
        {
            "llm": {"provider": "siliconflow"},
            "embedding": {"provider": "siliconflow"},
            "reranker": {"provider": "siliconflow"},
        }
    )

    verify_result = verify_installation.check_model_api_config(config=cfg, api_smoke=False)
    doctor_api = _check_api(cfg)

    assert verify_result["ok"] is True
    assert verify_result["warnings"] == [
        "Multimodal API 未配置，已跳过（可选；provider=siliconflow, "
        "model=Qwen/Qwen2.5-VL-72B-Instruct, base_url=https://****/v1, "
        "source=missing）"
    ]
    assert verify_result["apis"]["multimodal"]["optional"] is True
    assert verify_result["apis"]["multimodal"]["status"] == "skipped"
    assert doctor_api["status"] == "ok"
    for key in ("llm", "embedding", "reranker"):
        assert verify_result["apis"][key]["configured"] is True
        assert verify_result["apis"][key]["status"] == "configured"
        assert doctor_api["models"][key]["configured"] is True
        assert doctor_api["models"][key]["status"] == "configured"


def test_verify_model_api_config_loads_default_config_without_network(capsys, monkeypatch):
    from scripts import verify_installation
    import core.config

    clear_model_api_env(monkeypatch)
    monkeypatch.setenv("VERIFY_LLM_KEY", "llm-key")
    monkeypatch.setenv("VERIFY_EMBEDDING_KEY", "embedding-key")
    monkeypatch.setenv("VERIFY_RERANKER_KEY", "reranker-key")
    cfg = FakeConfig(
        {
            "llm": {
                "provider": "openai-compatible",
                "api_key_source": "env:VERIFY_LLM_KEY",
                "base_url": "https://llm.example.test/v1",
                "model": "custom-llm",
            },
            "embedding": {
                "provider": "openai-compatible",
                "api_key_source": "env:VERIFY_EMBEDDING_KEY",
                "base_url": "https://embedding.example.test/v1",
                "model": "custom-embedding",
            },
            "reranker": {
                "provider": "openai-compatible",
                "api_key_source": "env:VERIFY_RERANKER_KEY",
                "base_url": "https://reranker.example.test/v1",
                "model": "custom-reranker",
            },
        }
    )
    monkeypatch.setattr(core.config, "get_config", lambda: cfg)

    result = verify_installation.check_model_api_config(api_smoke=False)
    captured = capsys.readouterr()

    assert result["ok"] is True
    assert result["apis"]["llm"]["status"] == "configured"
    assert result["apis"]["embedding"]["status"] == "configured"
    assert result["apis"]["reranker"]["status"] == "configured"
    assert "配置不完整" not in captured.out


def test_verify_model_api_config_distinguishes_missing_unreachable_available(monkeypatch):
    from scripts import verify_installation

    clear_model_api_env(monkeypatch)
    monkeypatch.setenv("VERIFY_EMBEDDING_KEY", "embedding-key")
    monkeypatch.setenv("VERIFY_RERANKER_KEY", "reranker-key")
    cfg = FakeConfig(
        {
            "llm": {},
            "embedding": {
                "provider": "siliconflow",
                "api_key_source": "env:VERIFY_EMBEDDING_KEY",
                "base_url": "https://api.siliconflow.cn/v1",
                "model": "BAAI/bge-m3",
            },
            "reranker": {
                "provider": "dmxapi",
                "api_key_source": "env:VERIFY_RERANKER_KEY",
                "base_url": "https://www.dmxapi.cn/v1",
                "model": "BAAI/bge-reranker-v2-m3",
            },
        }
    )
    monkeypatch.setattr(
        verify_installation,
        "_smoke_embedding_api",
        lambda api_cfg, *, config=None: (False, "connection refused"),
    )
    monkeypatch.setattr(
        verify_installation,
        "_smoke_reranker_api",
        lambda api_cfg, *, config=None: (True, "ok"),
    )

    result = verify_installation.check_model_api_config(config=cfg, api_smoke=True)

    assert result["ok"] is False
    assert result["apis"]["llm"]["status"] == "not_configured"
    assert result["apis"]["embedding"]["status"] == "unreachable"
    assert result["apis"]["embedding"]["error"] == "connection refused"
    assert result["apis"]["reranker"]["status"] == "available"


def test_api_smoke_report_redacts_provider_exception_text(monkeypatch, capsys):
    from scripts import verify_installation

    clear_model_api_env(monkeypatch)
    monkeypatch.setenv("VERIFY_LLM_KEY", "llm-key")
    cfg = FakeConfig(
        {
            "llm": {
                "provider": "openai-compatible",
                "api_key_source": "env:VERIFY_LLM_KEY",
                "base_url": "https://provider.example.test/v1",
                "model": "test-model",
            },
            "embedding": {},
            "reranker": {},
        }
    )
    marker = "RAW_PROVIDER_EXCEPTION_MARKER_install_report"

    def fail_smoke(_api_cfg, *, config=None):
        raise RuntimeError(marker)

    monkeypatch.setattr(verify_installation, "_smoke_llm_api", fail_smoke)

    result = verify_installation.check_model_api_config(config=cfg, api_smoke=True)
    captured = capsys.readouterr()

    assert result["apis"]["llm"]["status"] == "unreachable"
    assert result["apis"]["llm"]["error"] == "provider_error"
    assert marker not in repr(result)
    assert marker not in captured.out
    assert "provider_error" in captured.out


def test_reranker_smoke_url_accepts_base_or_full_endpoint():
    from scripts.verify_installation import _reranker_smoke_url

    assert _reranker_smoke_url("https://gateway.example/v1") == "https://gateway.example/v1/rerank"
    assert (
        _reranker_smoke_url("https://gateway.example/v1/rerank")
        == "https://gateway.example/v1/rerank"
    )


def test_verify_agent_full_power_rejects_missing_agent_denominator(monkeypatch, capsys):
    from core.agent_kit.report import AgentKitReport
    from scripts import verify_installation
    import core.agent_kit as agent_kit

    monkeypatch.setattr(
        agent_kit,
        "build_agent_kit_report",
        lambda: AgentKitReport(
            protocol_version="agent-kit-v1",
            target_agents=["codex"],
            workflows=[],
            agents=[],
            missing_workflow_tools=[],
        ),
    )

    result = verify_installation.check_agent_full_power()

    assert result["ok"] is False
    assert result["full_power_ok"] is False
    assert result["installed_agents"] == []
    assert "8 Agent 满血分母未闭合" in capsys.readouterr().out


def test_verify_agent_full_power_fails_degraded_installed_agent(monkeypatch):
    from core.agent_kit.report import AgentKitAgentStatus, AgentKitReport
    from scripts import verify_installation
    import core.agent_kit as agent_kit

    agent = AgentKitAgentStatus(
        name="openclaw",
        active_entrypoint="mcp_only",
        installed=True,
        active_ready=True,
        mcp_configured=True,
        policy_installed=True,
        passive_source_registered=True,
        passive_source_detected=True,
        source_capabilities={"visible_text": True, "source_fidelity": "derived"},
        full_power_gaps=["passive source fidelity is not full (derived)"],
        repair_actions=["start/use openclaw once, then rerun mnemos agent kit openclaw"],
    )
    monkeypatch.setattr(
        agent_kit,
        "build_agent_kit_report",
        lambda: AgentKitReport(
            protocol_version="agent-kit-v1",
            target_agents=["openclaw"],
            workflows=[],
            agents=[agent],
            missing_workflow_tools=[],
        ),
    )

    result = verify_installation.check_agent_full_power()

    assert result["ok"] is False
    assert result["degraded_agents"] == ["openclaw"]
    assert any("openclaw 未达到满血接入标准" in error for error in result["errors"])


def test_verify_agent_full_power_rejects_one_verified_host_as_eight(monkeypatch):
    from core.agent_kit.report import AgentKitAgentStatus, AgentKitReport
    from scripts import verify_installation
    import core.agent_kit as agent_kit

    agent = AgentKitAgentStatus(
        name="codex",
        active_entrypoint="mcp_only",
        installed=True,
        active_ready=True,
        mcp_configured=True,
        policy_installed=True,
        passive_source_registered=True,
        passive_source_detected=True,
        content_access_authorized=True,
        runtime_state="verified",
        source_capture_state="verified",
        discovery_covered=True,
        content_parsed=True,
        raw_committed=True,
        runtime_canary_verified=True,
    )
    assert agent.full_power is True
    monkeypatch.setattr(
        agent_kit,
        "build_agent_kit_report",
        lambda: AgentKitReport(
            protocol_version="agent-kit-v2",
            target_agents=["codex"],
            workflows=[],
            agents=[agent],
            missing_workflow_tools=[],
        ),
    )

    result = verify_installation.check_agent_full_power()

    assert result["ok"] is False
    assert result["full_power_ok"] is False
    assert result["full_power_agents"] == ["codex"]
    assert set(result["runtime_unverified_agents"]) - {"codex"}


def test_api_smoke_boundaries_disable_sdk_retries_and_bind_full_request(
    tmp_path, monkeypatch
):
    from scripts import verify_installation

    class RuntimeConfig:
        data_dir = tmp_path
        database_dir = tmp_path

        def get(self, key, default=None):
            if key == "llm.provider_prices":
                return {"test": {"test-model": {"input": 0.1, "output": 0.2}}}
            return default

    api_cfg = SimpleNamespace(
        provider="test",
        api_key="test-key",
        base_url="https://provider.example/v1",
        model="test-model",
        timeout=1,
    )
    config = RuntimeConfig()
    constructor_kwargs = []

    def assert_reservation(operation, provider_input):
        with sqlite3.connect(str(tmp_path / "model_call_ledger.db")) as conn:
            row = conn.execute(
                "SELECT lifecycle_state, request_dispatched, reserved_input_tokens, input_digest "
                "FROM model_call_entries WHERE operation=?",
                (operation,),
            ).fetchone()
        assert row == (
            "reserved",
            1,
            utf8_token_upper_bound(provider_input),
            hashlib.sha256(provider_input.encode("utf-8")).hexdigest(),
        )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            constructor_kwargs.append(kwargs)
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self._chat_create)
            )
            self.embeddings = SimpleNamespace(create=self._embedding_create)

        @staticmethod
        def _chat_create(**kwargs):
            assert_reservation("verify_llm_smoke", canonical_chat_input(kwargs["messages"]))
            return SimpleNamespace(
                id="verify-chat-1",
                usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1),
            )

        @staticmethod
        def _embedding_create(**kwargs):
            assert_reservation("verify_embedding_smoke", canonical_provider_input(kwargs))
            return SimpleNamespace(
                id="verify-embedding-1",
                usage=SimpleNamespace(prompt_tokens=2, total_tokens=2),
            )

    rerank_response = MagicMock()
    rerank_response.headers = {"x-request-id": "verify-rerank-1"}
    rerank_response.json.return_value = {
        "id": "verify-rerank-1",
        "usage": {"total_tokens": 2},
        "results": [],
    }

    def rerank_post(*_args, **kwargs):
        assert_reservation("verify_rerank_smoke", canonical_provider_input(kwargs["json"]))
        return rerank_response

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    with patch("requests.post", side_effect=rerank_post):
        assert verify_installation._smoke_llm_api(api_cfg, config=config) == (True, "ok")
        assert verify_installation._smoke_embedding_api(api_cfg, config=config) == (True, "ok")
        assert verify_installation._smoke_reranker_api(api_cfg, config=config) == (True, "ok")

    assert [kwargs["max_retries"] for kwargs in constructor_kwargs] == [0, 0]


def test_llm_api_smoke_provider_error_preserves_dispatched_reservation(tmp_path, monkeypatch):
    from scripts import verify_installation

    class RuntimeConfig:
        data_dir = tmp_path
        database_dir = tmp_path

        def get(self, key, default=None):
            if key == "llm.provider_prices":
                return {"test": {"test-model": {"input": 0.1, "output": 0.2}}}
            return default

    class ProviderError(Exception):
        pass

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self._create)
            )

        @staticmethod
        def _create(**_kwargs):
            raise ProviderError("smoke provider failed")

    api_cfg = SimpleNamespace(
        provider="test",
        api_key="test-key",
        base_url="https://provider.example/v1",
        model="test-model",
        timeout=1,
    )
    monkeypatch.setitem(
        sys.modules,
        "openai",
        SimpleNamespace(OpenAI=FakeOpenAI, OpenAIError=ProviderError),
    )

    with pytest.raises(ProviderError, match="smoke provider failed"):
        verify_installation._smoke_llm_api(api_cfg, config=RuntimeConfig())

    with sqlite3.connect(str(tmp_path / "model_call_ledger.db")) as conn:
        row = conn.execute(
            "SELECT lifecycle_state, request_dispatched, error_code "
            "FROM model_call_entries WHERE operation='verify_llm_smoke'"
        ).fetchone()
    assert row == ("incurred_unknown", 1, "verify_llm_smoke_exception")


def test_embedding_and_rerank_smoke_errors_preserve_dispatched_reservations(
    tmp_path, monkeypatch
):
    from scripts import verify_installation

    class RuntimeConfig:
        data_dir = tmp_path
        database_dir = tmp_path

        def get(self, key, default=None):
            if key == "llm.provider_prices":
                return {"test": {"test-model": {"input": 0.1, "output": 0.2}}}
            return default

    class ProviderError(Exception):
        pass

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.embeddings = SimpleNamespace(create=self._create)

        @staticmethod
        def _create(**_kwargs):
            raise ProviderError("embedding smoke provider failed")

    api_cfg = SimpleNamespace(
        provider="test",
        api_key="test-key",
        base_url="https://provider.example/v1",
        model="test-model",
        timeout=1,
    )
    config = RuntimeConfig()
    monkeypatch.setitem(
        sys.modules,
        "openai",
        SimpleNamespace(OpenAI=FakeOpenAI, OpenAIError=ProviderError),
    )

    with pytest.raises(ProviderError, match="embedding smoke provider failed"):
        verify_installation._smoke_embedding_api(api_cfg, config=config)
    with patch("requests.post", side_effect=RuntimeError("rerank smoke request failed")):
        with pytest.raises(RuntimeError, match="rerank smoke request failed"):
            verify_installation._smoke_reranker_api(api_cfg, config=config)

    with sqlite3.connect(str(tmp_path / "model_call_ledger.db")) as conn:
        rows = conn.execute(
            "SELECT operation, lifecycle_state, request_dispatched, error_code "
            "FROM model_call_entries ORDER BY operation"
        ).fetchall()
    assert rows == [
        (
            "verify_embedding_smoke",
            "incurred_unknown",
            1,
            "verify_embedding_smoke_exception",
        ),
        (
            "verify_rerank_smoke",
            "incurred_unknown",
            1,
            "verify_rerank_smoke_exception",
        ),
    ]
