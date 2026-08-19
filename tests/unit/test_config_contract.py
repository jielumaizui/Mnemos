import json

import pytest

from core.config_registry import UnknownConfigKeyError


def test_mcp_launch_environment_is_process_only_and_never_persisted(
    tmp_path,
    monkeypatch,
):
    from core.config import Config

    mnemos_dir = tmp_path / ".mnemos"
    monkeypatch.setenv("MNEMOS_DIR", str(mnemos_dir))
    monkeypatch.setenv(
        "MNEMOS_MCP_LAUNCH_CAPABILITY_REF",
        "keyring:mnemos.mcp.launch/codex/capability-id",
    )

    config = Config()
    assert config.get_runtime_environment(
        "MNEMOS_MCP_LAUNCH_CAPABILITY_REF"
    ).startswith("keyring:")

    config.save()
    persisted = config.config_path.read_text(encoding="utf-8")
    assert "MNEMOS_MCP_LAUNCH_CAPABILITY" not in persisted
    assert "launch_capability" not in persisted


def test_auto_setup_writes_runtime_json_config(tmp_path, monkeypatch):
    from core.config import Config
    from scripts.auto_setup import generate_config
    from scripts import auto_setup

    mnemos_dir = tmp_path / ".mnemos"
    mnemos_vault = tmp_path / "vault" / "mnemos"
    raw_vault = tmp_path / "vault" / "raw"
    monkeypatch.setenv("MNEMOS_DIR", str(mnemos_dir))
    monkeypatch.setenv("MNEMOS_LLM_API_KEY", "llm-secret")
    monkeypatch.setenv("MNEMOS_LLM_BASE_URL", "https://llm.example.test/v1")
    monkeypatch.setenv("MNEMOS_LLM_MODEL", "llm-model")
    monkeypatch.setenv("MNEMOS_EMBEDDING_API_KEY", "embed-secret")
    monkeypatch.setenv("MNEMOS_EMBEDDING_BASE_URL", "https://embedding.example.test/v1")
    monkeypatch.setenv("MNEMOS_EMBEDDING_MODEL", "embedding-model")
    monkeypatch.setenv("MNEMOS_RERANKER_API_KEY", "rerank-secret")
    monkeypatch.setenv("MNEMOS_RERANKER_BASE_URL", "https://reranker.example.test/v1")
    monkeypatch.setenv("MNEMOS_RERANKER_MODEL", "reranker-model")
    monkeypatch.setattr(auto_setup, "_smoke_required_model_endpoints", lambda data: (True, {}))

    config_path = generate_config(mnemos_vault, raw_vault, yes_mode=True)
    data = json.loads(config_path.read_text(encoding="utf-8"))

    assert config_path == mnemos_dir / "configs" / "main.json"
    assert data["vaults"]["mnemos"]["path"] == str(mnemos_vault)
    assert data["vaults"]["raw"]["path"] == str(raw_vault)
    assert data["wiki"]["vault_path"] == str(mnemos_vault)
    assert data["storage"]["obsidian"]["vault_path"] == str(raw_vault)
    assert data["l1_storage"]["enabled"] is False
    assert data["l1_storage"]["api_url"] == ""
    assert data["daemon"]["services"]["capture_worker"] is True
    assert data["daemon"]["services"]["raw_projection"] is True
    assert data["daemon"]["services"]["raw_sync"] is True
    assert "l1_sync" not in data["daemon"]["services"]
    assert data["integrations"]["mcp"]["enabled"] is True
    assert data["llm"]["api_key"] == ""
    assert data["llm"]["api_key_env"] == "MNEMOS_LLM_API_KEY"
    assert data["llm"]["api_key_source"] == "env:MNEMOS_LLM_API_KEY"
    assert data["llm"]["provider"] == "openai-compatible"
    assert data["llm"]["base_url"] == "https://llm.example.test/v1"
    assert data["llm"]["model"] == "llm-model"
    assert data["llm"]["chain"][0]["provider"] == "openai-compatible"
    assert data["llm"]["chain"][0]["api_key_source"] == "env:MNEMOS_LLM_API_KEY"
    assert data["llm"]["providers"]["dmxapi"]["api_key"] == ""
    assert data["llm"]["providers"]["dmxapi"]["api_key_env"] == "DMXAPI_API_KEY"
    assert data["llm"]["providers"]["siliconflow"]["api_key"] == ""
    assert data["llm"]["providers"]["siliconflow"]["api_key_env"] == "SILICONFLOW_API_KEY"
    assert data["embedding"]["api_key"] == ""
    assert data["embedding"]["api_key_env"] == "MNEMOS_EMBEDDING_API_KEY"
    assert data["embedding"]["api_key_source"] == "env:MNEMOS_EMBEDDING_API_KEY"
    assert data["embedding"]["base_url"] == "https://embedding.example.test/v1"
    assert data["embedding"]["model"] == "embedding-model"
    assert data["reranker"]["api_key"] == ""
    assert data["reranker"]["api_key_env"] == "MNEMOS_RERANKER_API_KEY"
    assert data["reranker"]["api_key_source"] == "env:MNEMOS_RERANKER_API_KEY"
    assert data["reranker"]["base_url"] == "https://reranker.example.test/v1"
    assert data["reranker"]["model"] == "reranker-model"
    assert data["raw_projection"]["enabled"] is True
    assert data["raw_projection"]["chunk_turns"] == 5
    assert data["raw_projection"]["max_files"] == 0
    assert data["raw_projection"]["max_turn_chars"] == 0
    text = config_path.read_text(encoding="utf-8")
    assert "llm-secret" not in text
    assert "embed-secret" not in text
    assert "rerank-secret" not in text
    # provider / allow_host_agent_delegate 已移除，不再写入运行配置
    assert "provider" not in data.get("distill", {})
    assert "allow_host_agent_delegate" not in data.get("distill", {})

    config = Config()
    assert config.config_path == config_path
    assert config.wiki_dir == mnemos_vault
    assert config.vault_dir("raw") == raw_vault
    assert config.get("daemon.services.raw_sync") is True
    with pytest.raises(UnknownConfigKeyError):
        config.get("daemon.services.l1_sync")
    assert config.get("integrations.mcp.enabled") is True
    assert config.get("distill.max_tasks_per_cycle") > 0
    assert config.get("observation.interval_seconds") > 0
    assert config.get("feedback.pending_hours") > 0


def test_legacy_yaml_migrates_alias_to_canonical_json_and_canonical_env_wins(
    tmp_path, monkeypatch
):
    from core.config import Config

    mnemos_dir = tmp_path / ".mnemos"
    mnemos_dir.mkdir()
    legacy = mnemos_dir / "config.yaml"
    legacy.write_text(
        """
wiki:
  vault_path: /legacy/wiki
daemon:
  services:
    l1_sync: true
""",
        encoding="utf-8",
    )

    monkeypatch.setenv("MNEMOS_DIR", str(mnemos_dir))
    monkeypatch.setenv("MNEMOS_DAEMON__SERVICES__RAW_SYNC", "false")

    config = Config()

    assert config.config_path == mnemos_dir / "configs" / "main.json"
    assert config.config_path.exists()
    assert config.wiki_dir.as_posix() == "/legacy/wiki"
    assert config.get("daemon.services.raw_sync") is False
    with pytest.raises(UnknownConfigKeyError):
        config.get("daemon.services.l1_sync")

    saved = json.loads(config.config_path.read_text(encoding="utf-8"))
    # Process env wins at runtime but is never persisted into the migrated file.
    assert saved["daemon"]["services"]["raw_sync"] is True
    assert "l1_sync" not in saved["daemon"]["services"]
