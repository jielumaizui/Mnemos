from __future__ import annotations

import argparse
import json
from pathlib import Path

import mnemos_cli
from core.ops.config_audit import build_config_audit_report


class FakeConfig:
    def __init__(self, root: Path) -> None:
        self.database_dir = root / "db"
        self.data_dir = root / "data"
        self.config_path = root / "configs" / "main.json"
        self.database_dir.mkdir(parents=True)
        self.data_dir.mkdir()
        self.config_path.parent.mkdir()
        self.config_path.write_text("{}", encoding="utf-8")
        self._vaults = {
            "mnemos": root / "mnemos-vault",
            "raw": root / "raw-vault",
        }
        for path in self._vaults.values():
            path.mkdir()
        self._data = {
            "llm": {
                "provider": "siliconflow",
                "base_url": "https://api.siliconflow.cn/v1",
                "model": "deepseek-ai/DeepSeek-V4-Flash",
                "api_key": "",
                "api_key_source": "env:MNEMOS_LLM_API_KEY",
                "chain": [],
            },
            "embedding": {
                "enabled": True,
                "provider": "siliconflow",
                "base_url": "https://api.siliconflow.cn/v1",
                "model": "BAAI/bge-m3",
                "embedding_model": "BAAI/bge-m3",
                "api_key": "",
                "api_key_source": "env:MNEMOS_EMBEDDING_API_KEY",
            },
            "reranker": {
                "enabled": True,
                "provider": "siliconflow",
                "base_url": "https://api.siliconflow.cn/v1",
                "model": "BAAI/bge-reranker-v2-m3",
                "api_key": "",
                "api_key_source": "env:MNEMOS_RERANKER_API_KEY",
            },
            "storage": {
                "retention_days": {
                    "observations": 30,
                    "reflections": 30,
                    "distillation_chunks": 30,
                },
                "disk_budget": {
                    "sqlite_wal_file_max_mb": 512,
                    "sqlite_wal_total_max_mb": 1024,
                    "temp_total_max_mb": 2048,
                    "temp_stale_minutes": 60,
                    "snapshot_total_max_mb": 20480,
                    "snapshot_growth_max_mb_per_day": 8192,
                    "raw_events_max_mb": 4096,
                    "raw_events_growth_max_mb_per_day": 2048,
                    "growth_sample_min_seconds": 300,
                },
            },
            "daemon": {
                "services": {
                    "capture_worker": True,
                    "raw_projection": True,
                    "distill_and_merge": True,
                    "heartbeat": True,
                    "eventbus": True,
                }
            },
            "vaults": {
                "mnemos": {"path": str(self._vaults["mnemos"])},
                "raw": {"path": str(self._vaults["raw"])},
            },
            "l1_storage": {"enabled": False, "token": "", "api_url": ""},
        }

    def get(self, key: str, default=None):
        value = self._data
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value

    def persisted_source_data(self):
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def vault_dir(self, name: str) -> Path:
        return self._vaults[name]

    @property
    def l1_storage_enabled(self) -> bool:
        return bool(self._data["l1_storage"]["enabled"])

    @property
    def l1_storage_token(self) -> str:
        return str(self._data["l1_storage"]["token"])

    @property
    def l1_storage_api_url(self) -> str:
        return str(self._data["l1_storage"]["api_url"])


def _patch_security(monkeypatch):
    monkeypatch.setattr(
        "scripts.health_check.check_security",
        lambda config=None: {
            "permission_violations": [],
            "repair_actions": [],
            "plaintext_api_key_risks": [],
            "secret_inventory": {
                "schema_version": "mnemos.secret_inventory.v1",
                "findings": [],
                "plaintext_count": 0,
                "reference_count": 0,
                "error": None,
            },
            "legacy_key_rows": {"enc_rows": 0, "plaintext_rows": 0, "keyref_rows": 0},
            "keyring": {
                "schema_version": "mnemos.keyring_doctor.v1",
                "status": "accepted",
                "summary": "keyring unavailable; env fallback accepted",
                "risk_level": "safe_but_not_best",
                "safe_but_not_best": True,
                "requires_user_choice": False,
                "env_fallback_accepted": True,
                "secret_reference_counts": {
                    "env": 3,
                    "keyring": 0,
                    "keyref": 0,
                    "plaintext": 0,
                    "unknown": 0,
                },
                "secret_inventory_plaintext_count": 0,
                "keyring": {
                    "available": False,
                    "backend": None,
                    "error": "not installed",
                },
                "repair_actions": ["install keyring"],
            },
        },
    )


def _set_model_env(monkeypatch):
    monkeypatch.setenv("MNEMOS_LLM_API_KEY", "test-llm-key")
    monkeypatch.setenv("MNEMOS_EMBEDDING_API_KEY", "test-embedding-key")
    monkeypatch.setenv("MNEMOS_RERANKER_API_KEY", "test-reranker-key")


def test_config_audit_strict_passes_with_reference_secrets(tmp_path, monkeypatch):
    _patch_security(monkeypatch)
    _set_model_env(monkeypatch)
    cfg = FakeConfig(tmp_path)

    report = build_config_audit_report(cfg, strict=True)

    assert report["schema_version"] == "mnemos.config_audit.v1"
    assert report["ok"] is True
    assert report["required_failed"] == []
    assert "test-llm-key" not in json.dumps(report)
    model_items = {item["item_id"]: item for item in report["items"]}
    assert model_items["model.llm"]["status"] == "configured"
    assert model_items["model.embedding"]["evidence"]["base_url"] == "https://****/v1"
    assert model_items["model.embedding"]["evidence"]["key_source"] == "env:****"
    assert model_items["security.keyring"]["status"] == "accepted"
    assert model_items["security.keyring"]["evidence"]["risk_level"] == "safe_but_not_best"
    assert "api.siliconflow.cn" not in json.dumps(report)
    assert str(tmp_path) not in json.dumps(report)
    assert model_items["storage.disk_budget"]["status"] == "ok"


def test_config_audit_unsafe_debug_can_return_local_values(tmp_path, monkeypatch):
    _patch_security(monkeypatch)
    _set_model_env(monkeypatch)
    cfg = FakeConfig(tmp_path)

    report = build_config_audit_report(cfg, strict=True, show_sensitive=True)
    model_items = {item["item_id"]: item for item in report["items"]}

    assert model_items["model.llm"]["evidence"]["base_url"] == "https://api.siliconflow.cn/v1"
    assert model_items["model.llm"]["evidence"]["key_source"] == "env:MNEMOS_LLM_API_KEY"
    assert model_items["path.database_dir"]["evidence"]["path"] == str(cfg.database_dir)


def test_config_audit_requires_disk_budget_config(tmp_path, monkeypatch):
    _patch_security(monkeypatch)
    _set_model_env(monkeypatch)
    cfg = FakeConfig(tmp_path)
    cfg._data["storage"].pop("disk_budget")

    report = build_config_audit_report(cfg, strict=True)
    items = {item["item_id"]: item for item in report["items"]}

    assert report["ok"] is False
    assert "storage.disk_budget" in report["required_failed"]
    assert items["storage.disk_budget"]["evidence"]["missing"]


def test_config_audit_flags_plaintext_without_leaking_value(tmp_path, monkeypatch):
    _patch_security(monkeypatch)
    _set_model_env(monkeypatch)
    cfg = FakeConfig(tmp_path)
    secret_value = "sk" + "-secret-value-that-must-not-leak"
    cfg._data["llm"]["api_key"] = secret_value
    cfg._data["external_service"] = {
        "bearer": "Bearer secret-bearer-that-must-not-leak",
        "token_budget_total": 16000,
    }

    report = build_config_audit_report(cfg, strict=True)
    payload = json.dumps(report)

    assert report["ok"] is False
    assert any(item.startswith("secret.llm.api_key") for item in report["required_failed"])
    assert any(item.startswith("secret.external_service.bearer") for item in report["required_failed"])
    assert secret_value not in payload
    assert "secret-bearer-that-must-not-leak" not in payload
    assert "secret.external_service.token_budget_total" not in payload
    assert "value_length" in payload


def test_config_audit_reports_stale_config_migration(tmp_path, monkeypatch):
    _patch_security(monkeypatch)
    _set_model_env(monkeypatch)
    cfg = FakeConfig(tmp_path)
    cfg._data["memos"] = {"enabled": True, "token": "legacy-token"}
    cfg.config_path.write_text(json.dumps(cfg._data), encoding="utf-8")

    report = build_config_audit_report(cfg, strict=True)
    items = {item["item_id"]: item for item in report["items"]}

    assert "legacy.config_stale_keys" in report["required_failed"]
    assert items["legacy.config_stale_keys"]["status"] == "legacy-risk"
    assert items["legacy.config_stale_keys"]["evidence"]["stale_keys"] == ["memos"]
    assert "legacy-token" not in json.dumps(report)


def test_doctor_config_parser_accepts_strict_json():
    parser = mnemos_cli.build_parser()

    args = parser.parse_args(["doctor", "config", "--strict", "--json"])

    assert args.command == "doctor"
    assert args.doctor_action == "config"
    assert args.strict is True
    assert args.json is True


def test_cmd_doctor_config_writes_redacted_artifact(tmp_path, monkeypatch, capsys):
    _patch_security(monkeypatch)
    _set_model_env(monkeypatch)
    cfg = FakeConfig(tmp_path)
    monkeypatch.setattr("core.cli.commands.doctor._get_config", lambda: cfg)

    result = mnemos_cli.cmd_doctor(
        argparse.Namespace(
            doctor_action="config",
            strict=True,
            json=True,
            e2e=False,
            verbose=False,
            dry_run=False,
            cognitive_readiness=False,
        )
    )

    out = capsys.readouterr().out
    payload = json.loads(out)
    artifact = cfg.database_dir / "config_audit.json"
    assert result is True
    assert artifact.exists()
    assert artifact.stat().st_mode & 0o777 == 0o600
    assert payload["schema_version"] == "mnemos.config_audit.v1"
    assert "test-llm-key" not in out
    assert "api.siliconflow.cn" not in out
    assert str(tmp_path) not in out
    assert payload["artifact_path"] != str(artifact)
    assert "test-llm-key" not in artifact.read_text(encoding="utf-8")
    assert "api.siliconflow.cn" not in artifact.read_text(encoding="utf-8")
    assert str(tmp_path) not in artifact.read_text(encoding="utf-8")
