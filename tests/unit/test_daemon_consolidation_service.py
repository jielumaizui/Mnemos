from __future__ import annotations

from pathlib import Path

from daemon.consolidation_service import run_service


class _Cfg:
    def __init__(self, root: Path, *, enabled: bool = True):
        self.database_dir = root / "db"
        self.wiki_dir = root / "wiki"
        self.raw_vault_dir = root / "raw"
        self.database_dir.mkdir()
        self.wiki_dir.mkdir()
        self.raw_vault_dir.mkdir()
        self.enabled = enabled

    def get(self, key: str, default=None):
        values = {
            "daemon.services.cognitive_consolidation": self.enabled,
            "raw_event_store.db_path": str(self.database_dir / "raw_events.db"),
            "cognitive_consolidation.db_path": str(self.database_dir / "cognitive_consolidation.db"),
            "cognitive_consolidation.raw_vault_dir": str(self.raw_vault_dir),
        }
        return values.get(key, default)


def test_consolidation_daemon_is_read_only_and_requires_initialized_raw(tmp_path):
    cfg = _Cfg(tmp_path)
    errors = []

    result = run_service(lambda _name, error: errors.append(error), config=cfg)

    assert result == {"status": "not_initialized", "reason": "raw_events_db_absent", "planned": 0}
    assert not errors
    assert not (cfg.database_dir / "cognitive_consolidation.db").exists()


def test_consolidation_daemon_respects_disable_flag(tmp_path):
    cfg = _Cfg(tmp_path, enabled=False)

    assert run_service(lambda *_args: None, config=cfg) == {
        "status": "skipped",
        "reason": "daemon_service_disabled",
        "planned": 0,
    }


def test_consolidation_daemon_plans_initialized_raw_without_new_ledger(tmp_path):
    from core.sync_framework.raw_event_store import RawEventStore

    cfg = _Cfg(tmp_path)
    store = RawEventStore(config=cfg)
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="consolidation-daemon",
            turn_number=1,
            user_content="candidate",
            assistant_content="response",
        )
        result = run_service(lambda *_args: None, config=cfg)
    finally:
        store.close()

    assert result["status"] == "planned"
    assert result["raw_purge_allowed"] is False
    assert result["coverage_written"] == 0
    assert not (cfg.database_dir / "cognitive_consolidation.db").exists()
