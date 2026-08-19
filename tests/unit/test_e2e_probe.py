"""E2E probe contract tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_probe_capture_flushes_queued_session(monkeypatch):
    """探针 capture 步骤必须同步 flush，不能只入队后假装链路正常。"""
    from scripts import e2e_probe

    calls = []

    class FakeWorkerPool:
        def flush_session(self, source_agent, session_id):
            calls.append((source_agent, session_id))
            return {"flushed": 1, "failed": 0}

    class FakeCaptureService:
        def __init__(self, start_worker=False):
            self.start_worker = start_worker
            self.worker_pool = FakeWorkerPool()

        def capture_session(self, source_agent, session_id, turns):
            return {"status": "queued", "queued_count": len(turns)}

        def end_session(self, source_agent, session_id):
            return {"status": "ok"}

        def close(self):
            pass

    monkeypatch.setattr(
        "core.sync_framework.capture_service.CaptureService",
        FakeCaptureService,
    )

    status, sid = e2e_probe._probe_capture()

    assert status == e2e_probe.STATUS_PASS
    assert calls == [("e2e_probe", sid)]


def test_run_probe_dry_run_does_not_call_write_steps(monkeypatch, tmp_path):
    """dry-run 必须是只读检查，不能调用任何会写入用户记忆库的探针步骤。"""
    from scripts import e2e_probe

    wiki = tmp_path / "wiki"
    db = tmp_path / "db"
    raw = tmp_path / "raw"
    wiki.mkdir()
    db.mkdir()
    raw.mkdir()

    class FakeConfig:
        wiki_dir = wiki
        database_dir = db
        obsidian_vault_path = raw

        def get(self, key, default=None):
            return default

    def forbidden(*args, **kwargs):
        raise AssertionError("dry-run must not call write probe steps")

    monkeypatch.setattr("core.config.get_config", lambda: FakeConfig())
    monkeypatch.setattr(e2e_probe, "_probe_capture", forbidden)
    monkeypatch.setattr(e2e_probe, "_probe_backend", forbidden)
    monkeypatch.setattr(e2e_probe, "_probe_distill", forbidden)
    monkeypatch.setattr(e2e_probe, "_probe_wiki", forbidden)
    monkeypatch.setattr(e2e_probe, "_probe_search", forbidden)
    monkeypatch.setattr(e2e_probe, "_cleanup", forbidden)
    monkeypatch.setattr(e2e_probe, "_probe_mcp", lambda: (e2e_probe.STATUS_PASS, "mcp ok"))

    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    steps = e2e_probe.run_probe(dry_run=True)
    after = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))

    assert set(steps) == {"config", "imports", "databases", "llm_config", "mcp"}
    assert after == before


def test_dry_run_config_redacts_paths_by_default(monkeypatch):
    from scripts import e2e_probe

    home = Path.home()

    class FakeConfig:
        wiki_dir = home / "Documents" / "MnemosVault"
        database_dir = home / ".mnemos" / "db"
        obsidian_vault_path = home / "Documents" / "MnemosRaw"

    monkeypatch.setattr("core.config.get_config", lambda: FakeConfig())
    monkeypatch.setattr(e2e_probe.Path, "exists", lambda _path: True)
    monkeypatch.setattr(e2e_probe.os, "access", lambda _path, _mode: True)

    status, message = e2e_probe._probe_dry_run_config()

    assert status == e2e_probe.STATUS_PASS
    assert str(home) not in message
    assert "<HOME>" in message


def test_dry_run_config_can_show_paths_for_unsafe_debug(monkeypatch):
    from scripts import e2e_probe

    home = Path.home()

    class FakeConfig:
        wiki_dir = home / "Documents" / "MnemosVault"
        database_dir = home / ".mnemos" / "db"
        obsidian_vault_path = home / "Documents" / "MnemosRaw"

    monkeypatch.setattr("core.config.get_config", lambda: FakeConfig())
    monkeypatch.setattr(e2e_probe.Path, "exists", lambda _path: True)
    monkeypatch.setattr(e2e_probe.os, "access", lambda _path, _mode: True)

    status, message = e2e_probe._probe_dry_run_config(show_paths=True)

    assert status == e2e_probe.STATUS_PASS
    assert str(home / "Documents" / "MnemosVault") in message


def test_cleanup_removes_probe_records(monkeypatch, tmp_path):
    """探针清理必须删除写入 L1 storage 的测试记录，避免污染个人长期库。"""
    from scripts import e2e_probe

    sid = "e2e_probe_cleanup_case"

    vault = tmp_path / "vault"
    vault.mkdir()

    class FakeConfig:
        wiki_dir = tmp_path
        database_dir = tmp_path
        obsidian_vault_path = vault
        claude_data_dir = tmp_path / "claude"

    class FakeBackendClient:
        def __init__(self):
            pass

        def search(self, query, limit=10):
            return [
                SimpleNamespace(uid="uid-probe", content=f"E2E探针测试 {sid}"),
                SimpleNamespace(uid="uid-other", content="E2E探针测试 unrelated"),
            ]

    monkeypatch.setattr("core.config.get_config", lambda: FakeConfig)
    monkeypatch.setattr("integrations.backends.ObsidianBackend", FakeBackendClient)

    # 创建模拟文件让 _cleanup 能删除
    (vault / "uid-probe").write_text("probe")
    raw_db = tmp_path / "raw_events.db"
    with sqlite3.connect(raw_db) as conn:
        conn.executescript(
            """
            CREATE TABLE raw_turns (event_id TEXT, source_agent TEXT, session_id TEXT);
            CREATE TABLE raw_access_log (event_id TEXT);
            CREATE TABLE raw_metrics (event_id TEXT);
            CREATE TABLE raw_turn_revisions (revision_id TEXT, logical_event_id TEXT);
            CREATE TABLE raw_provenance_edges (source_revision_id TEXT);
            CREATE TABLE raw_native_contract_observations (logical_event_id TEXT);
            """
        )
        conn.execute(
            "INSERT INTO raw_turns VALUES ('event-probe', 'e2e_probe', ?)",
            (sid,),
        )
        conn.execute("INSERT INTO raw_access_log VALUES ('event-probe')")
        conn.execute("INSERT INTO raw_metrics VALUES ('event-probe')")
        conn.execute("INSERT INTO raw_turn_revisions VALUES ('revision-probe', 'event-probe')")
        conn.execute("INSERT INTO raw_provenance_edges VALUES ('revision-probe')")
        conn.execute("INSERT INTO raw_native_contract_observations VALUES ('event-probe')")

    status, message = e2e_probe._cleanup(sid)

    assert status == e2e_probe.STATUS_PASS
    assert "backend" in message
    assert not (vault / "uid-probe").exists()
    with sqlite3.connect(raw_db) as conn:
        for table in (
            "raw_turns",
            "raw_access_log",
            "raw_metrics",
            "raw_turn_revisions",
            "raw_provenance_edges",
            "raw_native_contract_observations",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_delete_probe_event_rows_rejects_unapproved_table():
    from scripts import e2e_probe

    with sqlite3.connect(":memory:") as conn:
        with pytest.raises(ValueError):
            e2e_probe._delete_probe_event_rows(
                conn,
                "raw_turns; DROP TABLE raw_turns",
                ["raw-1"],
            )


def test_cleanup_removes_probe_records_from_sync_log(monkeypatch, tmp_path):
    """cleanup 应优先按 sync_log 里的 storage_uids 精确删除探针记录。"""
    from scripts import e2e_probe

    sid = "e2e_probe_cleanup_from_sync_log"
    with sqlite3.connect(str(tmp_path / "sync_log.db")) as conn:
        conn.execute("""
            CREATE TABLE sync_log (
                agent_name TEXT,
                session_id TEXT,
                turn_number INTEGER,
                backend_uids TEXT
            )
            """)
        conn.execute(
            "INSERT INTO sync_log VALUES (?, ?, ?, ?)",
            ("e2e_probe", sid, 0, '["uid-a.md", "uid-b.md"]'),
        )

    class FakeConfig:
        wiki_dir = tmp_path
        data_dir = tmp_path
        database_dir = tmp_path
        claude_data_dir = tmp_path / "claude"

    class FakeBackendClient:
        def __init__(self):
            pass

        def search(self, query, limit=10):
            return []

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "uid-a.md").write_text("test-a")
    (vault / "uid-b.md").write_text("test-b")

    FakeConfig.obsidian_vault_path = vault

    monkeypatch.setattr("core.config.get_config", lambda: FakeConfig)
    monkeypatch.setattr("integrations.backends.ObsidianBackend", FakeBackendClient)

    status, message = e2e_probe._cleanup(sid)

    assert status == e2e_probe.STATUS_PASS
    assert "backend=2" in message
    assert not (vault / "uid-a.md").exists()
    assert not (vault / "uid-b.md").exists()


def test_probe_storage_accepts_sync_log_record_before_search(monkeypatch, tmp_path):
    """Canonical raw 模式必须同时有 sync_log 和 raw_events 证据。"""
    from scripts import e2e_probe

    sid = "e2e_probe_sync_log_case"
    db_path = tmp_path / "sync_log.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("""
            CREATE TABLE sync_log (
                agent_name TEXT,
                session_id TEXT,
                turn_number INTEGER,
                status TEXT,
                backend_uids TEXT
            )
            """)
        conn.execute(
            "INSERT INTO sync_log VALUES (?, ?, ?, ?, ?)",
            ("e2e_probe", sid, 0, "new", '["uid-1"]'),
        )
    with sqlite3.connect(str(tmp_path / "raw_events.db")) as conn:
        conn.execute("""
            CREATE TABLE raw_turns (
                event_id TEXT,
                source_agent TEXT,
                session_id TEXT,
                turn_number INTEGER,
                content_hash TEXT
            )
            """)
        conn.execute(
            "INSERT INTO raw_turns VALUES (?, ?, ?, ?, ?)",
            ("raw-1", "e2e_probe", sid, 0, "hash-1"),
        )

    class FakeConfig:
        data_dir = tmp_path
        database_dir = tmp_path
        claude_data_dir = tmp_path / "claude"

        def get(self, key, default=None):
            return {
                "raw_event_store.enabled": True,
                "raw_projection.enabled": True,
            }.get(key, default)

    class SearchShouldNotBeNeeded:
        def __init__(self, *args, **kwargs):
            raise AssertionError("sync_log hit should avoid backend search")

    FakeConfig.obsidian_vault_path = tmp_path / "vault"

    monkeypatch.setattr("core.config.get_config", lambda: FakeConfig)
    monkeypatch.setattr("integrations.backends.ObsidianBackend", SearchShouldNotBeNeeded)

    status, message = e2e_probe._probe_backend(sid)

    assert status == e2e_probe.STATUS_PASS
    assert "sync_log" in message
    assert "raw record id=raw-1" in message


def test_probe_storage_fails_without_raw_record_in_canonical_mode(monkeypatch, tmp_path):
    """sync_log status=new 且 backend_uids=[] 不能在 canonical raw 模式下直接通过。"""
    from scripts import e2e_probe

    sid = "e2e_probe_no_raw_case"
    with sqlite3.connect(str(tmp_path / "sync_log.db")) as conn:
        conn.execute("""
            CREATE TABLE sync_log (
                agent_name TEXT,
                session_id TEXT,
                turn_number INTEGER,
                status TEXT,
                backend_uids TEXT
            )
            """)
        conn.execute(
            "INSERT INTO sync_log VALUES (?, ?, ?, ?, ?)",
            ("e2e_probe", sid, 0, "new", "[]"),
        )

    class FakeConfig:
        database_dir = tmp_path
        obsidian_vault_path = tmp_path / "vault"

        def get(self, key, default=None):
            return {
                "raw_event_store.enabled": True,
                "raw_projection.enabled": True,
            }.get(key, default)

    monkeypatch.setattr("core.config.get_config", lambda: FakeConfig())

    status, message = e2e_probe._probe_backend(sid)

    assert status == e2e_probe.STATUS_FAIL
    assert "raw_events.db 未记录" in message


def test_probe_storage_rejects_skipped_backend_even_with_raw_record(monkeypatch, tmp_path):
    """full/real 探针不能把 skipped_backend 当作成功落地。"""
    from scripts import e2e_probe

    sid = "e2e_probe_skipped_backend_case"
    with sqlite3.connect(str(tmp_path / "sync_log.db")) as conn:
        conn.execute("""
            CREATE TABLE sync_log (
                agent_name TEXT,
                session_id TEXT,
                turn_number INTEGER,
                status TEXT,
                backend_uids TEXT
            )
            """)
        conn.execute(
            "INSERT INTO sync_log VALUES (?, ?, ?, ?, ?)",
            ("e2e_probe", sid, 0, "skipped_backend", '["uid-existing"]'),
        )
    with sqlite3.connect(str(tmp_path / "raw_events.db")) as conn:
        conn.execute("""
            CREATE TABLE raw_turns (
                event_id TEXT,
                source_agent TEXT,
                session_id TEXT,
                turn_number INTEGER,
                content_hash TEXT
            )
            """)
        conn.execute(
            "INSERT INTO raw_turns VALUES (?, ?, ?, ?, ?)",
            ("raw-1", "e2e_probe", sid, 0, "hash-1"),
        )

    class FakeConfig:
        database_dir = tmp_path
        obsidian_vault_path = tmp_path / "vault"

        def get(self, key, default=None):
            return {
                "raw_event_store.enabled": True,
                "raw_projection.enabled": True,
            }.get(key, default)

    monkeypatch.setattr("core.config.get_config", lambda: FakeConfig())

    status, message = e2e_probe._probe_backend(sid)

    assert status == e2e_probe.STATUS_FAIL
    assert "skipped_backend" in message


def test_probe_storage_requires_backend_uid_in_external_backend_mode(monkeypatch, tmp_path):
    """External backend 模式必须能从 backend_uids 反查到实际记录。"""
    from scripts import e2e_probe

    sid = "e2e_probe_external_backend_case"
    with sqlite3.connect(str(tmp_path / "sync_log.db")) as conn:
        conn.execute("""
            CREATE TABLE sync_log (
                agent_name TEXT,
                session_id TEXT,
                turn_number INTEGER,
                status TEXT,
                backend_uids TEXT
            )
            """)
        conn.execute(
            "INSERT INTO sync_log VALUES (?, ?, ?, ?, ?)",
            ("e2e_probe", sid, 0, "new", '["uid-1"]'),
        )

    class FakeConfig:
        database_dir = tmp_path
        obsidian_vault_path = tmp_path / "vault"

        def get(self, key, default=None):
            return {
                "raw_event_store.enabled": False,
                "raw_projection.enabled": False,
            }.get(key, default)

    class FakeBackend:
        def get_by_id(self, uid):
            return SimpleNamespace(
                uid=uid,
                content=f"E2E探针测试 {sid}",
                metadata={"file_path": str(tmp_path / "vault" / uid)},
            )

    monkeypatch.setattr("core.config.get_config", lambda: FakeConfig())
    monkeypatch.setattr(
        "core.sync_framework.storage_backend.create_storage_backend",
        lambda: FakeBackend(),
    )

    status, message = e2e_probe._probe_backend(sid)

    assert status == e2e_probe.STATUS_PASS
    assert "backend uid=uid-1" in message


def test_probe_storage_fails_on_empty_backend_uid_in_external_backend_mode(
    monkeypatch, tmp_path
):
    """External backend 模式不允许 backend_uids=[] 通过。"""
    from scripts import e2e_probe

    sid = "e2e_probe_empty_backend_uid_case"
    with sqlite3.connect(str(tmp_path / "sync_log.db")) as conn:
        conn.execute("""
            CREATE TABLE sync_log (
                agent_name TEXT,
                session_id TEXT,
                turn_number INTEGER,
                status TEXT,
                backend_uids TEXT
            )
            """)
        conn.execute(
            "INSERT INTO sync_log VALUES (?, ?, ?, ?, ?)",
            ("e2e_probe", sid, 0, "new", "[]"),
        )

    class FakeConfig:
        database_dir = tmp_path
        obsidian_vault_path = tmp_path / "vault"

        def get(self, key, default=None):
            return {
                "raw_event_store.enabled": False,
                "raw_projection.enabled": False,
            }.get(key, default)

    monkeypatch.setattr("core.config.get_config", lambda: FakeConfig())

    status, message = e2e_probe._probe_backend(sid)

    assert status == e2e_probe.STATUS_FAIL
    assert "backend_uids 为空" in message


def test_probe_distill_runs_live_engine_when_api_configured(monkeypatch):
    """API 已配置时，E2E 蒸馏步骤必须真实调用 DistillationEngine 写 Wiki。"""
    from scripts import e2e_probe

    calls = []

    class FakeLLMConfig:
        configured = True

    class FakeEngine:
        def process(self, session_id, messages, meta=None):
            calls.append(("process", session_id, messages, meta))
            return SimpleNamespace(judgment="knowledge", fragments=[object()])

        def write_pages(self, result):
            calls.append(("write_pages", result.judgment))
            return ["/tmp/e2e-page.md"]

    monkeypatch.setattr(
        "core.llm_config.resolve_effective_llm_api_config",
        lambda config=None: FakeLLMConfig,
    )
    monkeypatch.setattr("core.hephaestus.distillation_engine.DistillationEngine", FakeEngine)

    status, message = e2e_probe._probe_distill("sid-live")

    assert status == e2e_probe.STATUS_PASS
    assert "生成 Wiki 页面" in message
    assert calls[0][0] == "process"
    assert calls[0][1] == "sid-live"
    assert calls[1] == ("write_pages", "knowledge")


def test_probe_distill_retries_when_first_attempt_writes_no_pages(monkeypatch):
    """真实 E2E 蒸馏第一次无页面时应重试一次，但仍以实际写页为 pass。"""
    from scripts import e2e_probe

    calls = []

    class FakeLLMConfig:
        configured = True

    class FakeEngine:
        def process(self, session_id, messages, meta=None):
            calls.append(("process", session_id, messages, meta))
            if len([c for c in calls if c[0] == "process"]) == 1:
                return SimpleNamespace(judgment="skip", judgment_reason="no fragments")
            return SimpleNamespace(judgment="knowledge", fragments=[object()])

        def write_pages(self, result):
            calls.append(("write_pages", result.judgment))
            if result.judgment == "skip":
                return []
            return ["/tmp/e2e-page.md"]

    monkeypatch.setattr(
        "core.llm_config.resolve_effective_llm_api_config",
        lambda config=None: FakeLLMConfig,
    )
    monkeypatch.setattr("core.hephaestus.distillation_engine.DistillationEngine", FakeEngine)

    status, message = e2e_probe._probe_distill("sid-retry", real_api=True)

    assert status == e2e_probe.STATUS_PASS
    assert "attempt=2" in message
    assert [c[0] for c in calls].count("process") == 2
    assert calls[-1] == ("write_pages", "knowledge")


def test_probe_distill_no_api_skips_before_resolving_llm(monkeypatch):
    """--no-api 必须在任何 LLM 配置解析和 DistillationEngine 调用前跳过。"""
    from scripts import e2e_probe

    monkeypatch.setattr(
        "core.llm_config.resolve_effective_llm_api_config",
        lambda config=None: (_ for _ in ()).throw(AssertionError("LLM must not be resolved")),
    )

    status, message = e2e_probe._probe_distill("sid-no-api", no_api=True)

    assert status == e2e_probe.STATUS_SKIP
    assert "--no-api" in message


def test_probe_distill_skips_when_api_not_configured(monkeypatch):
    """API 未配置时，蒸馏步骤应标记为 skip 而不是失败。"""
    from scripts import e2e_probe

    monkeypatch.setattr(
        "core.llm_config.resolve_effective_llm_api_config",
        lambda config=None: SimpleNamespace(configured=False),
    )

    status, message = e2e_probe._probe_distill("sid-no-api")

    assert status == e2e_probe.STATUS_SKIP
    assert "跳过" in message


def test_probe_distill_fails_when_real_api_required(monkeypatch):
    """--real-api 模式下，API 未配置时应标记为失败。"""
    from scripts import e2e_probe

    monkeypatch.setattr(
        "core.llm_config.resolve_effective_llm_api_config",
        lambda config=None: SimpleNamespace(configured=False),
    )

    status, message = e2e_probe._probe_distill("sid-real", real_api=True)

    assert status == e2e_probe.STATUS_FAIL
    assert "--real-api" in message


def test_probe_wiki_skips_when_distill_skipped(monkeypatch, tmp_path):
    """蒸馏跳过时，Wiki 检查也应标记为 skip。"""
    from scripts import e2e_probe

    class FakeConfig:
        wiki_dir = tmp_path

    monkeypatch.setattr("core.config.get_config", lambda: FakeConfig)

    status, message = e2e_probe._probe_wiki("sid-wiki", distill_status=e2e_probe.STATUS_SKIP)

    assert status == e2e_probe.STATUS_SKIP
    assert "蒸馏已跳过" in message
    assert (tmp_path / "00-Inbox").exists()


def test_probe_wiki_ignores_stale_probe_pages(monkeypatch, tmp_path):
    """Wiki 检查必须命中本次 session_id，不能被历史 e2e_probe 页面误导。"""
    from scripts import e2e_probe

    inbox = tmp_path / "00-Inbox"
    inbox.mkdir()
    (inbox / "stale.md").write_text("old e2e_probe page without this sid", encoding="utf-8")

    class FakeConfig:
        wiki_dir = tmp_path

    monkeypatch.setattr("core.config.get_config", lambda: FakeConfig)

    status, message = e2e_probe._probe_wiki(
        "sid-current",
        distill_status=e2e_probe.STATUS_PASS,
    )

    assert status == e2e_probe.STATUS_FAIL
    assert "未找到探针 Wiki 页面" in message
