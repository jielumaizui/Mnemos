from pathlib import Path

from core.privacy.data_ownership import DataOwnershipManager


class FakeConfig:
    def __init__(self, root: Path):
        self.mnemos_dir = root
        self.data_dir = root
        self.database_dir = root / "db"
        self.database_dir.mkdir(parents=True)
        self._vaults = {"mnemos": root / "mnemos_vault", "raw": root / "raw_vault"}
        for vault in self._vaults.values():
            vault.mkdir()

    def vault_dir(self, name: str) -> Path:
        return self._vaults[name]


def test_data_export_dry_run_returns_secret_redacted_manifest(tmp_path):
    cfg = FakeConfig(tmp_path)
    manifest = DataOwnershipManager(cfg).export("all", dry_run=True)

    assert manifest.dry_run is True
    assert manifest.output_path == ""
    assert manifest.redaction_policy == "secret_redacted_summary"
    assert manifest.validate() == []
