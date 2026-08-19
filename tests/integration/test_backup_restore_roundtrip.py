import json
from pathlib import Path

from core.backup.snapshot_manager import MnemosSnapshotManager


class FakeConfig:
    def __init__(self, root: Path):
        self.mnemos_dir = root
        self.data_dir = root
        self.database_dir = root / "db"
        self.database_dir.mkdir(parents=True)
        self.config_path = root / "configs" / "main.json"
        self.config_path.parent.mkdir(parents=True)
        self.config_path.write_text(json.dumps({"version": 1}), encoding="utf-8")
        self._vaults = {"mnemos": root / "mnemos_vault", "raw": root / "raw_vault"}
        for vault in self._vaults.values():
            vault.mkdir()
        (self._vaults["mnemos"] / "page.md").write_text("before", encoding="utf-8")

    def vault_dir(self, name: str) -> Path:
        return self._vaults[name]


def test_backup_restore_roundtrip_for_config_and_wiki(tmp_path):
    cfg = FakeConfig(tmp_path)
    manager = MnemosSnapshotManager(cfg)
    manifest = manager.create(reason="roundtrip", scopes=("config", "mnemos_vault"))

    cfg.config_path.write_text(json.dumps({"version": 2}), encoding="utf-8")
    (cfg.vault_dir("mnemos") / "page.md").write_text("after", encoding="utf-8")

    plan = manager.restore_plan(manifest.snapshot_id)
    restored = manager.restore_apply(manifest.snapshot_id, allow_conflicts=True)
    verified = manager.restore_verify(manifest.snapshot_id)

    assert plan.status == "blocked"
    assert restored.status == "verified"
    assert verified.status == "verified"
    assert json.loads(cfg.config_path.read_text(encoding="utf-8"))["version"] == 1
    assert (cfg.vault_dir("mnemos") / "page.md").read_text(encoding="utf-8") == "before"
