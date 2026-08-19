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
        self.config_path.write_text(json.dumps({"healthy": True}), encoding="utf-8")
        (self.database_dir / "action_ledger.db").write_text("ledger", encoding="utf-8")
        self._vaults = {"mnemos": root / "mnemos_vault", "raw": root / "raw_vault"}
        for vault in self._vaults.values():
            vault.mkdir()
            (vault / "page.md").write_text("present", encoding="utf-8")

    def vault_dir(self, name: str) -> Path:
        return self._vaults[name]


def test_disaster_recovery_drill_restores_missing_files(tmp_path):
    cfg = FakeConfig(tmp_path)
    manager = MnemosSnapshotManager(cfg)
    manifest = manager.create(reason="drill", scopes=("config", "action_ledger", "mnemos_vault"))

    cfg.config_path.unlink()
    (cfg.vault_dir("mnemos") / "page.md").unlink()

    restored = manager.restore_apply(manifest.snapshot_id)
    verified = manager.restore_verify(manifest.snapshot_id)

    assert restored.status == "verified"
    assert verified.status == "verified"
    assert cfg.config_path.exists()
    assert (cfg.vault_dir("mnemos") / "page.md").exists()
