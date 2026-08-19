import json
import sqlite3
from pathlib import Path

from core.setup.install_lifecycle import InstallLifecycleManager


class FakeConfig:
    def __init__(self, root: Path):
        self.mnemos_dir = root / ".mnemos"
        self.data_dir = self.mnemos_dir
        self.database_dir = self.mnemos_dir / "databases"
        self.config_path = self.mnemos_dir / "configs" / "main.json"
        self.wiki_dir = root / "MnemosVault"
        self.obsidian_vault_path = root / "RawVault"
        self._data = {
            "persona": {"data_sources": {"memos": {"enabled": True}}},
            "vaults": {
                "mnemos": {"path": str(self.wiki_dir), "enabled": True},
                "raw": {"path": str(self.obsidian_vault_path), "enabled": True},
            },
        }
        self.prepare()

    def prepare(self) -> None:
        self.database_dir.mkdir(parents=True, exist_ok=True)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(self._data), encoding="utf-8")
        self.wiki_dir.mkdir(parents=True, exist_ok=True)
        self.obsidian_vault_path.mkdir(parents=True, exist_ok=True)
        (self.database_dir / "events.db").write_bytes(b"probe")

    def get(self, key, default=None):
        current = self._data
        for part in key.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current

    def to_dict(self):
        return json.loads(json.dumps(self._data))

    def save(self):
        self.config_path.write_text(json.dumps(self._data), encoding="utf-8")

    def vault_dir(self, name: str):
        if name == "mnemos":
            return self.wiki_dir
        if name == "raw":
            return self.obsidian_vault_path
        raise KeyError(name)


def _ledger_counts(db_path: Path) -> dict[str, int]:
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            "SELECT action_type, COUNT(*) FROM action_ledger GROUP BY action_type"
        ).fetchall()
    return {str(action_type): int(count) for action_type, count in rows}


def _observed_action_types(db_path: Path) -> list[str]:
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT action_type
            FROM action_ledger
            WHERE action_type IN (
                'install_setup', 'install_upgrade', 'install_uninstall'
            )
            ORDER BY created_at ASC
            """
        ).fetchall()
    return [str(action_type) for (action_type,) in rows]


def test_setup_upgrade_uninstall_roundtrip_writes_action_ledger(tmp_path):
    cfg = FakeConfig(tmp_path)
    manager = InstallLifecycleManager(cfg)

    setup_state = manager.run_setup(dry_run=False, auto_setup_args=None)
    upgrade_plan = manager.upgrade_plan()
    upgrade_state = manager.upgrade_apply(execute_wrapped=False)
    uninstall_state = manager.uninstall(dry_run=False)

    counts = _ledger_counts(cfg.database_dir / "action_ledger.db")
    assert setup_state.status == "installed_ready"
    assert upgrade_plan.migration_plan_hash
    assert upgrade_state.status in {"installed_ready", "rollback_available"}
    assert upgrade_state.backup_ref.startswith("snap-")
    assert uninstall_state.status == "uninstalled_preserve_data"
    assert counts["install_setup"] == 1
    assert counts["install_upgrade"] == 1
    assert counts["install_uninstall"] == 1
    assert _observed_action_types(cfg.database_dir / "action_ledger.db") == [
        "install_setup",
        "install_upgrade",
        "install_uninstall",
    ]
    assert Path(setup_state.state_path).exists()
