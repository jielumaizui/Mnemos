import sqlite3
from pathlib import Path

from core.privacy.data_ownership import (
    DATA_DOMAINS,
    DataOwnershipManager,
    audit_data_ownership_contract,
)


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


def test_data_ownership_contract_audit_passes():
    assert audit_data_ownership_contract(strict=True) == []


def test_data_inventory_covers_all_required_domains(tmp_path):
    cfg = FakeConfig(tmp_path)
    inventory = DataOwnershipManager(cfg).inventory()
    domains = {item["domain"] for item in inventory["domains"]}

    assert inventory["status"] == "ok"
    assert DATA_DOMAINS <= domains


def test_data_inventory_blocks_retired_prompt_storage_inside_canonical_ledger(tmp_path):
    cfg = FakeConfig(tmp_path)
    ledger_path = cfg.database_dir / "model_call_ledger.db"
    with sqlite3.connect(ledger_path) as conn:
        conn.execute("CREATE TABLE prompt_calls (id INTEGER PRIMARY KEY, prompt_summary TEXT)")
        conn.execute("INSERT INTO prompt_calls(prompt_summary) VALUES ('private marker')")
        conn.execute("CREATE TABLE prompt_call_stats (name TEXT PRIMARY KEY, value REAL)")

    inventory = DataOwnershipManager(cfg).inventory()
    ledger = next(item for item in inventory["domains"] if item["domain"] == "model_call_ledger")

    assert inventory["status"] == "degraded"
    assert inventory["counts"]["blocked_records"] == 2
    assert ledger["estimated_records"] == 2
    assert "model_call_ledger_retired_prompt_storage" in inventory["errors"]
    assert "private marker" not in str(inventory)


def test_data_inventory_blocks_an_empty_canonical_retired_record_table(tmp_path):
    cfg = FakeConfig(tmp_path)
    ledger_path = cfg.database_dir / "model_call_ledger.db"
    with sqlite3.connect(ledger_path) as conn:
        conn.execute("CREATE TABLE prompt_call_log (id INTEGER PRIMARY KEY)")

    inventory = DataOwnershipManager(cfg).inventory()

    assert inventory["status"] == "degraded"
    assert inventory["counts"]["blocked_records"] == 1
    assert "model_call_ledger_retired_prompt_storage" in inventory["errors"]
