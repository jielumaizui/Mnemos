from __future__ import annotations

import os
import json
import time
from types import SimpleNamespace

from core.ops.sqlite_disk_budget import (
    BYTES_PER_MIB,
    build_sqlite_disk_budget_report,
    repair_sqlite_disk_budget,
)


def _config(tmp_path, *, values=None):
    values = values or {}
    defaults = {
        "storage.disk_budget.sqlite_wal_file_max_mb": 1,
        "storage.disk_budget.sqlite_wal_total_max_mb": 1,
        "storage.disk_budget.temp_total_max_mb": 1,
        "storage.disk_budget.temp_stale_minutes": 1,
        "storage.disk_budget.snapshot_total_max_mb": 1,
        "storage.disk_budget.snapshot_growth_max_mb_per_day": 1,
        "storage.disk_budget.raw_projection_backup_total_max_mb": 1,
        "storage.disk_budget.raw_events_max_mb": 1,
        "storage.disk_budget.raw_events_growth_max_mb_per_day": 1,
        "storage.disk_budget.growth_sample_min_seconds": 1,
    }
    defaults.update(values)
    return SimpleNamespace(
        database_dir=tmp_path / "db",
        mnemos_dir=tmp_path / "mnemos",
        get=lambda key, default=None: defaults.get(key, default),
    )


def test_sqlite_disk_budget_reports_auto_and_manual_handling(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    cfg.database_dir.mkdir(parents=True)
    (cfg.mnemos_dir / "backups" / "snapshots" / "snap-a").mkdir(parents=True)

    (cfg.database_dir / "events.db-wal").write_bytes(b"w" * (2 * BYTES_PER_MIB))
    (cfg.database_dir / "raw_events.db").write_bytes(b"r" * (2 * BYTES_PER_MIB))
    (cfg.mnemos_dir / "backups" / "snapshots" / "snap-a" / "raw_events.db").write_bytes(
        b"s" * (2 * BYTES_PER_MIB)
    )

    temp_dir = tmp_path / "tmp"
    temp_dir.mkdir()
    temp_file = temp_dir / "mnemos_large.tmp"
    temp_file.write_bytes(b"t" * (2 * BYTES_PER_MIB))
    old = time.time() - 120
    os.utime(temp_file, (old, old))
    monkeypatch.setattr("core.ops.sqlite_disk_budget.tempfile.gettempdir", lambda: str(temp_dir))

    report = build_sqlite_disk_budget_report(cfg, update_state=False)
    findings = {finding["metric"]: finding for finding in report["findings"]}

    assert report["status"] == "degraded"
    assert findings["sqlite_wal_file"]["handling"] == "auto_heal_safe"
    assert findings["temp_total"]["handling"] == "auto_heal_safe"
    assert findings["snapshot_total"]["handling"] == "manual_required"
    assert findings["raw_events_size"]["handling"] == "manual_required"
    assert report["auto_heal_available"] >= 2
    assert report["manual_required"] >= 2


def test_sqlite_disk_budget_reports_growth_rate_from_state(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    cfg.database_dir.mkdir(parents=True)
    (cfg.database_dir / "raw_events.db").write_bytes(b"r" * (2 * BYTES_PER_MIB))
    empty_temp = tmp_path / "tmp"
    empty_temp.mkdir()
    monkeypatch.setattr("core.ops.sqlite_disk_budget.tempfile.gettempdir", lambda: str(empty_temp))
    state_path = cfg.database_dir / "sqlite_disk_budget_state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "mnemos.sqlite_disk_budget.v1",
                "raw_events": {"size_bytes": 0, "sampled_at": time.time() - 2},
            }
        ),
        encoding="utf-8",
    )

    report = build_sqlite_disk_budget_report(cfg, update_state=False)
    metrics = {finding["metric"] for finding in report["findings"]}

    assert "raw_events_growth_per_day" in metrics


def test_sqlite_disk_budget_keeps_projection_backup_retention_manual(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    cfg.database_dir.mkdir(parents=True)
    backup = cfg.mnemos_dir / "backups" / "raw-vault-projection-legacy"
    backup.mkdir(parents=True)
    (backup / "legacy-raw.md").write_bytes(b"r" * (2 * BYTES_PER_MIB))
    empty_temp = tmp_path / "tmp"
    empty_temp.mkdir()
    monkeypatch.setattr("core.ops.sqlite_disk_budget.tempfile.gettempdir", lambda: str(empty_temp))

    report = build_sqlite_disk_budget_report(cfg, update_state=False)
    findings = {finding["metric"]: finding for finding in report["findings"]}

    assert report["raw_projection_backups"]["directory_count"] == 1
    assert findings["raw_projection_backup_total"]["handling"] == "manual_required"
    assert "audit_raw_projection_backups.py" in findings["raw_projection_backup_total"]["user_action"]


def test_repair_sqlite_disk_budget_deletes_only_stale_temp(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    cfg.database_dir.mkdir(parents=True)
    temp_dir = tmp_path / "tmp"
    temp_dir.mkdir()
    stale = temp_dir / "mnemos_stale.tmp"
    fresh = temp_dir / "mnemos_fresh.tmp"
    stale.write_text("stale", encoding="utf-8")
    fresh.write_text("fresh", encoding="utf-8")
    old = time.time() - 120
    os.utime(stale, (old, old))
    monkeypatch.setattr("core.ops.sqlite_disk_budget.tempfile.gettempdir", lambda: str(temp_dir))

    dry_run = repair_sqlite_disk_budget(cfg, apply=False, repair_wal=False, repair_temp=True)
    assert stale.exists()
    assert fresh.exists()
    assert dry_run["actions"][0]["status"] == "planned"

    applied = repair_sqlite_disk_budget(cfg, apply=True, repair_wal=False, repair_temp=True)
    assert applied["ok"] is True
    assert not stale.exists()
    assert fresh.exists()
