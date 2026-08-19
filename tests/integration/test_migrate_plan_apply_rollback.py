import json
from pathlib import Path

from core.migrations.registry import MigrationLedger, MigrationRegistry


class FakeConfig:
    def __init__(self, root: Path):
        self.mnemos_dir = root
        self.data_dir = root
        self.database_dir = root / "db"
        self.database_dir.mkdir(parents=True)
        self.config_path = root / "configs" / "main.json"
        self.config_path.parent.mkdir(parents=True)
        self._data = {"persona": {"data_sources": {"memos": {"enabled": True}}}}
        self.save()

    def save(self):
        self.config_path.write_text(json.dumps(self._data), encoding="utf-8")

    def to_dict(self):
        return json.loads(json.dumps(self._data))

    def get(self, key, default=None):
        current = self._data
        for part in str(key).split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current


def test_migration_plan_apply_rollback_writes_ledger(tmp_path):
    cfg = FakeConfig(tmp_path)
    registry = MigrationRegistry()
    plan = registry.plan(cfg)
    applied = registry.apply(cfg, "config.stale_keys.v1")
    rolled_back = registry.rollback(cfg, "config.stale_keys.v1")

    ledger = MigrationLedger.from_config(cfg)
    recent = ledger.recent(limit=5)

    assert plan.plan_hash
    assert applied.status == "applied"
    assert rolled_back.status == "rolled_back"
    assert any(row["status"] == "applied" for row in recent)
    assert any(row["status"] == "rolled_back" for row in recent)
