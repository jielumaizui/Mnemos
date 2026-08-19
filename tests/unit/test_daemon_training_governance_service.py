from __future__ import annotations

import sqlite3
from pathlib import Path

from core.cognitive.state_schema import initialize_cognitive_state_schema
from core.scoring.training_schema import initialize_training_schema
from daemon.training_governance_service import run_service


class _Config:
    def __init__(self, database_dir: Path, enabled: bool = True):
        self.database_dir = database_dir
        self._enabled = enabled

    def get(self, key: str, default=None):
        if key == "daemon.services.training_governance":
            return self._enabled
        if key == "training_governance.reconcile_batch_limit":
            return 10
        return default


def _initialize(root: Path) -> None:
    initialize_cognitive_state_schema(root / "producer_consumer_ledger.db")
    with sqlite3.connect(root / "mnemos.db") as conn:
        initialize_training_schema(conn)


def test_training_governance_service_records_reproducible_empty_denominator(
    tmp_path: Path,
) -> None:
    _initialize(tmp_path)
    errors: list[tuple[str, Exception]] = []

    first = run_service(
        lambda name, exc: errors.append((name, exc)),
        config=_Config(tmp_path),
    )
    replay = run_service(
        lambda name, exc: errors.append((name, exc)),
        config=_Config(tmp_path),
    )

    assert errors == []
    assert first["status"] == replay["status"] == "ok"
    assert first["run_status"] == replay["run_status"] == "insufficient_sample"
    assert first["run_revision_id"] == replay["run_revision_id"]
    with sqlite3.connect(tmp_path / "mnemos.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM governed_training_run_receipts").fetchone() == (
            1,
        )


def test_training_governance_service_respects_disabled_and_absent_state(
    tmp_path: Path,
) -> None:
    assert (
        run_service(
            lambda _name, _exc: None,
            config=_Config(tmp_path, enabled=False),
        )["status"]
        == "skipped"
    )
    assert (
        run_service(
            lambda _name, _exc: None,
            config=_Config(tmp_path),
        )["status"]
        == "not_initialized"
    )
