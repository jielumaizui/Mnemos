import sqlite3
from pathlib import Path
from types import SimpleNamespace


def _has_table(db_path, table_name):
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
    return row is not None


def test_bootstrap_schema_creates_key_tables(tmp_path, monkeypatch):
    from scripts.auto_setup import generate_config
    from scripts import auto_setup
    from core.db_init import bootstrap_schema
    from core.config import get_config, reload_config, reset_config

    mnemos_dir = tmp_path / ".mnemos"
    mnemos_vault = tmp_path / "vault" / "mnemos"
    raw_vault = tmp_path / "vault" / "raw"
    monkeypatch.setenv("MNEMOS_DIR", str(mnemos_dir))
    monkeypatch.setenv("MNEMOS_LLM_API_KEY", "llm-secret")
    monkeypatch.setenv("MNEMOS_LLM_BASE_URL", "https://llm.example.test/v1")
    monkeypatch.setenv("MNEMOS_LLM_MODEL", "llm-model")
    monkeypatch.setenv("MNEMOS_EMBEDDING_API_KEY", "embed-secret")
    monkeypatch.setenv("MNEMOS_EMBEDDING_BASE_URL", "https://embedding.example.test/v1")
    monkeypatch.setenv("MNEMOS_EMBEDDING_MODEL", "embedding-model")
    monkeypatch.setenv("MNEMOS_RERANKER_API_KEY", "rerank-secret")
    monkeypatch.setenv("MNEMOS_RERANKER_BASE_URL", "https://reranker.example.test/v1")
    monkeypatch.setenv("MNEMOS_RERANKER_MODEL", "reranker-model")
    monkeypatch.setattr(auto_setup, "_smoke_required_model_endpoints", lambda data: (True, {}))
    reset_config()
    generate_config(mnemos_vault, raw_vault, yes_mode=True)
    reset_config()
    reload_config()
    try:
        assert get_config().database_dir == mnemos_dir

        first = bootstrap_schema()
        second = bootstrap_schema()

        assert first["ok"] is True
        assert second["ok"] is True
        db_paths = {
            step["name"]: Path(step["db_path"]) for step in first["steps"] if "db_path" in step
        }
        assert _has_table(db_paths["sync_log_schema"], "sync_log")
        assert _has_table(db_paths["capture_queue"], "capture_events")
        assert _has_table(db_paths["event_bus"], "events")
        assert _has_table(db_paths["amphora"], "distillation_tasks")
        adaptive_db = db_paths["adaptive_scorer"]
        assert _has_table(adaptive_db, "search_sessions")
        assert _has_table(adaptive_db, "scoring_object_provenance")
        assert not _has_table(adaptive_db, "scorer_training_queue")
        assert not _has_table(adaptive_db, "ground_truth_signals")
        assert not _has_table(adaptive_db, "scorer_models")
        assert _has_table(
            db_paths["operational_incidents"],
            "operational_incidents",
        )
    finally:
        # This test reloads the process-global Config with a per-test
        # MNEMOS_DIR.  Drop it before monkeypatch restores the environment so
        # later clients cannot reuse this test's transient database state.
        reset_config()


def test_health_report_exposes_schema_bootstrap_status(tmp_path, monkeypatch):
    from core.ops import health_check

    config = SimpleNamespace(
        data_dir=tmp_path / "data",
        database_dir=tmp_path / "db",
        get=lambda _key, default=None: default,
    )
    config.data_dir.mkdir()
    config.database_dir.mkdir()

    monkeypatch.setattr(health_check, "_check_storage", lambda _config: {"status": "ok"})
    monkeypatch.setattr(health_check, "_check_wiki", lambda _config: {"status": "ok"})
    monkeypatch.setattr(health_check, "_check_agents", lambda: {"status": "ok"})
    monkeypatch.setattr(health_check, "_check_daemon", lambda: {"status": "ok"})
    monkeypatch.setattr(health_check, "_check_event_bus", lambda _config: {"status": "ok"})
    monkeypatch.setattr(health_check, "_check_amphora", lambda: {"status": "ok"})
    monkeypatch.setattr(health_check, "_check_disk", lambda _config: {"status": "ok"})
    monkeypatch.setattr(health_check, "_check_api", lambda _config: {"status": "ok"})
    monkeypatch.setattr(health_check, "_check_heartbeat", lambda _config: {"status": "ok"})

    report = health_check.build_health_report(config)

    schema = report["checks"]["schema"]
    assert schema["status"] == "ok"
    assert schema["database_dir"] == "<PATH>/db"
    assert str(config.database_dir) not in str(schema)
    assert "sync_log_schema" in schema["expected_steps"]
    assert "operational_incidents" in schema["expected_steps"]

    unsafe_report = health_check.build_health_report(config, show_sensitive=True)
    unsafe_schema = unsafe_report["checks"]["schema"]
    assert unsafe_schema["database_dir"] == str(config.database_dir)
