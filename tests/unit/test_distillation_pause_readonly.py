from __future__ import annotations

import sqlite3

import pytest


def test_pause_status_does_not_create_missing_database(monkeypatch, tmp_path):
    from core.hephaestus import distillation_pause

    db_path = tmp_path / "missing" / "distillation_state.db"
    monkeypatch.setattr(distillation_pause, "_get_pause_db", lambda: db_path)

    assert distillation_pause.get_pause_status() == {"paused": False}
    assert not db_path.exists()
    assert not db_path.parent.exists()


def test_pause_status_does_not_create_missing_table(monkeypatch, tmp_path):
    from core.hephaestus import distillation_pause

    db_path = tmp_path / "distillation_state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    before = db_path.read_bytes()
    monkeypatch.setattr(distillation_pause, "_get_pause_db", lambda: db_path)

    assert distillation_pause.get_pause_status() == {"paused": False}
    assert db_path.read_bytes() == before


def test_pause_write_uses_hermetic_database_when_config_is_stale(monkeypatch, tmp_path):
    from core.hephaestus import distillation_pause

    run_root = tmp_path / "run"
    run_database = run_root / "home" / ".mnemos"
    stale_database = tmp_path / "outside" / ".mnemos"
    config = type("StaleConfig", (), {"database_dir": stale_database})()
    monkeypatch.setenv("MNEMOS_RUN_ROOT", str(run_root))
    monkeypatch.setenv("MNEMOS_DATABASE_DIR", str(run_database))
    monkeypatch.setattr(distillation_pause, "get_config", lambda: config)

    assert distillation_pause._get_pause_db() == run_database / "distillation_state.db"
    distillation_pause.resume_distillation()

    assert (run_database / "distillation_state.db").is_file()
    assert not stale_database.exists()


def test_pause_write_refuses_hermetic_database_outside_run_root(monkeypatch, tmp_path):
    from core.hephaestus import distillation_pause

    run_root = tmp_path / "run"
    outside_database = tmp_path / "outside" / ".mnemos"
    config = type("HermeticConfig", (), {"database_dir": run_root / "database"})()
    monkeypatch.setenv("MNEMOS_RUN_ROOT", str(run_root))
    monkeypatch.setenv("MNEMOS_DATABASE_DIR", str(outside_database))
    monkeypatch.setattr(distillation_pause, "get_config", lambda: config)

    with pytest.raises(ValueError, match="escapes MNEMOS_RUN_ROOT"):
        distillation_pause.resume_distillation()

    assert not outside_database.exists()
