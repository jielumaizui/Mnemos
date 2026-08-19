import json
from pathlib import Path

from core.migrations.registry import MigrationRegistry


class FakeConfig:
    def __init__(self, root: Path):
        self.mnemos_dir = root
        self.data_dir = root
        self.database_dir = root / "db"
        self.database_dir.mkdir(parents=True)
        self.config_path = root / "configs" / "main.json"
        self.config_path.parent.mkdir(parents=True)
        self._data = {
            "memos": {"enabled": True, "token": "legacy-token"},
            "daemon": {"services": {"l1_sync": True, "raw_sync": False}},
            "distill": {"allow_host_agent_delegate": True},
            "app": {"push_max_items": 9.0},
            "scoring": {"min_samples_per_dimension": 5.0},
            "skill": {"min_usage_count": 3},
        }
        self.save()

    def save(self):
        self.config_path.write_text(json.dumps(self._data), encoding="utf-8")

    def get(self, key, default=None):
        current = self._data
        for part in key.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current

    def to_dict(self):
        return json.loads(json.dumps(self._data))


def test_stale_config_migration_apply_removes_legacy_keys(tmp_path):
    cfg = FakeConfig(tmp_path)
    record = MigrationRegistry().apply(cfg, "config.stale_keys.v1")

    assert record.status == "applied"
    assert "memos" not in cfg._data
    assert "daemon" in cfg._data
    assert "l1_sync" not in cfg._data["daemon"]["services"]
    assert "distill" not in cfg._data
    assert cfg._data["app"]["push_max_items"] == 9
    assert type(cfg._data["app"]["push_max_items"]) is int
    assert cfg._data["scoring"]["min_samples_per_dimension"] == 5
    assert record.verification["type_coercions"] == {
        "app.push_max_items": 9,
        "scoring.min_samples_per_dimension": 5,
    }
    assert cfg._data["skill"]["cognitive_decision_flywheel"]["min_usage_count"] == 3
    assert record.verification["alias_migrations"] == {
        "skill.min_usage_count": "skill.cognitive_decision_flywheel.min_usage_count"
    }
    assert record.verification["alias_conflicts"] == ["daemon.services.l1_sync"]
    assert record.rollback_ref


def test_stale_config_migration_rollback_restores_backup(tmp_path):
    cfg = FakeConfig(tmp_path)
    registry = MigrationRegistry()
    registry.apply(cfg, "config.stale_keys.v1")
    rollback = registry.rollback(cfg, "config.stale_keys.v1")

    assert rollback.status == "rolled_back"
    assert cfg._data["memos"]["token"] == "legacy-token"
    assert cfg._data["daemon"]["services"]["l1_sync"] is True
    assert cfg._data["distill"]["allow_host_agent_delegate"] is True


def test_stale_config_migration_moves_alias_when_canonical_value_is_absent(tmp_path):
    cfg = FakeConfig(tmp_path)
    del cfg._data["daemon"]["services"]["raw_sync"]
    cfg.save()

    record = MigrationRegistry().apply(cfg, "config.stale_keys.v1")

    assert cfg._data["daemon"]["services"] == {"raw_sync": True}
    assert record.verification["alias_migrations"] == {
        "daemon.services.l1_sync": "daemon.services.raw_sync",
        "skill.min_usage_count": "skill.cognitive_decision_flywheel.min_usage_count",
    }
    assert record.verification["alias_conflicts"] == []


def test_stale_config_migration_preserves_l1_batch_budgets_as_continuous_budgets(tmp_path):
    cfg = FakeConfig(tmp_path)
    cfg._data = {
        "sync": {
            "l1_scan_max_sessions_per_source": 20,
            "l1_scan_max_turns_per_session": 50,
            "l1_scan_max_sources_per_cycle": 3,
            "l1_scan_poll_interval_seconds": 60,
            "l1_scan_recent_hours": 24.0,
        }
    }
    cfg.save()

    record = MigrationRegistry().apply(cfg, "config.stale_keys.v1")

    assert cfg._data == {
        "sync": {
            "raw_sync_sessions_per_source": 20,
            "raw_sync_turns_per_session": 50,
        }
    }
    assert record.verification["alias_migrations"] == {
        "sync.l1_scan_max_sessions_per_source": "sync.raw_sync_sessions_per_source",
        "sync.l1_scan_max_turns_per_session": "sync.raw_sync_turns_per_session",
    }


def test_stale_config_migration_removes_lossy_raw_projection_profile(tmp_path):
    cfg = FakeConfig(tmp_path)
    cfg._data = {
        "capture": {"duplicate_ttl_days": 90},
        "raw_projection": {"max_turn_chars": 12000},
    }
    cfg.save()

    record = MigrationRegistry().apply(cfg, "config.stale_keys.v1")

    assert cfg._data == {"raw_projection": {"max_turn_chars": 0}}
    assert record.verification["removed_stale_keys"] == ["capture.duplicate_ttl_days"]
    assert record.verification["value_normalizations"] == {
        "raw_projection.max_turn_chars": {"from": 12000, "to": 0}
    }


def test_stale_config_migration_removes_external_collector_budget(tmp_path):
    cfg = FakeConfig(tmp_path)
    cfg._data = {"distill": {"max_collect_per_cycle": 10}}
    cfg.save()

    record = MigrationRegistry().apply(cfg, "config.stale_keys.v1")

    assert cfg._data == {}
    assert record.verification["removed_stale_keys"] == ["distill.max_collect_per_cycle"]
    assert record.rollback_ref


def test_stale_config_migration_removes_retired_raw_vault_watcher(tmp_path):
    cfg = FakeConfig(tmp_path)
    cfg._data = {
        "watchers": {
            "enabled": False,
            "raw_vault": {
                "enabled": False,
                "poll_interval_seconds": 30,
                "watch_dir": "/tmp/raw",
            },
        },
        "daemon": {"services": {"raw_vault_watch": False, "raw_sync": True}},
    }
    cfg.save()

    record = MigrationRegistry().apply(cfg, "config.stale_keys.v1")

    assert cfg._data == {
        "watchers": {"enabled": False},
        "daemon": {"services": {"raw_sync": True}},
    }
    assert record.verification["removed_stale_keys"] == [
        "daemon.services.raw_vault_watch",
        "watchers.raw_vault.enabled",
        "watchers.raw_vault.poll_interval_seconds",
        "watchers.raw_vault.watch_dir",
    ]


def test_runtime_config_ignores_retired_empty_context_persona_service(tmp_path, monkeypatch):
    from core.config import Config

    mnemos_dir = tmp_path / ".mnemos"
    config_path = mnemos_dir / "configs" / "main.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps({"daemon": {"services": {"persona_extensions": True}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("MNEMOS_DIR", str(mnemos_dir))

    cfg = Config()

    assert cfg.get("daemon.services.persona_challenge") is True
    assert "daemon.services.persona_extensions" in cfg._ignored_obsolete_keys
    plan = MigrationRegistry().plan(cfg).items[0]
    assert "daemon.services.persona_extensions" in plan.stale_keys


def test_real_config_migration_rewrites_only_persisted_source_document(tmp_path, monkeypatch):
    from core.config import Config

    mnemos_dir = tmp_path / ".mnemos"
    config_path = mnemos_dir / "configs" / "main.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "daemon": {"services": {"l1_sync": False}},
                "app": {"push_max_items": 9.0},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MNEMOS_DIR", str(mnemos_dir))
    monkeypatch.setenv("MNEMOS_DISTILL__TOKEN_BUDGET_TOTAL", "32000")
    cfg = Config(strict=False)

    record = MigrationRegistry().apply(cfg, "config.stale_keys.v1")
    persisted = json.loads(config_path.read_text(encoding="utf-8"))

    assert record.status == "applied"
    assert persisted == {
        "daemon": {"services": {"raw_sync": False}},
        "app": {"push_max_items": 9},
    }
    assert cfg.get("distill.token_budget_total") == 32000


def test_real_config_migration_detects_lossy_profile_hidden_by_runtime_sanitizer(
    tmp_path, monkeypatch
):
    from core.config import Config

    mnemos_dir = tmp_path / ".mnemos"
    config_path = mnemos_dir / "configs" / "main.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps({"raw_projection": {"max_turn_chars": 12000}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("MNEMOS_DIR", str(mnemos_dir))
    cfg = Config(strict=False)

    plan_item = MigrationRegistry().plan(cfg).items[0]

    assert plan_item.status == "planned"
    assert plan_item.value_normalizations == {
        "raw_projection.max_turn_chars": {"from": 12000, "to": 0}
    }

    record = MigrationRegistry().apply(cfg, "config.stale_keys.v1")
    persisted = json.loads(config_path.read_text(encoding="utf-8"))

    assert record.status == "applied"
    assert persisted == {"raw_projection": {"max_turn_chars": 0}}
