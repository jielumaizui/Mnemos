import json


def test_auto_setup_writes_runtime_json_config(tmp_path, monkeypatch):
    from core.config import Config
    from scripts.auto_setup import generate_config

    mnemos_dir = tmp_path / ".mnemos"
    wiki_dir = tmp_path / "vault" / "wiki"
    monkeypatch.setenv("MNEMOS_DIR", str(mnemos_dir))

    config_path = generate_config(wiki_dir, "http://localhost:5230", yes_mode=True)
    data = json.loads(config_path.read_text(encoding="utf-8"))

    assert config_path == mnemos_dir / "configs" / "main.json"
    assert data["wiki"]["vault_path"] == str(wiki_dir)
    assert data["memos"]["enabled"] is True
    assert data["memos"]["api_url"] == "http://localhost:5230"
    assert data["daemon"]["services"]["capture_worker"] is True
    assert data["daemon"]["services"]["l1_sync"] is True
    assert data["integrations"]["mcp"]["enabled"] is True
    assert data["distill"]["provider"] == "api"

    config = Config()
    assert config.config_path == config_path
    assert config.wiki_dir == wiki_dir
    assert config.get("daemon.services.l1_sync") is True
    assert config.get("integrations.mcp.enabled") is True


def test_legacy_yaml_migrates_to_json_and_env_still_wins(tmp_path, monkeypatch):
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
    monkeypatch.setenv("MNEMOS_DAEMON__SERVICES__L1_SYNC", "false")

    config = Config()

    assert config.config_path == mnemos_dir / "configs" / "main.json"
    assert config.config_path.exists()
    assert config.wiki_dir.as_posix() == "/legacy/wiki"
    assert config.get("daemon.services.l1_sync") is False

    saved = json.loads(config.config_path.read_text(encoding="utf-8"))
    assert saved["daemon"]["services"]["l1_sync"] is False
