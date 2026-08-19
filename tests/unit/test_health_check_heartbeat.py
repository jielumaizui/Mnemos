import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.ops import health_check


def _config(tmp_path, *, wiki_dir=None, values=None):
    values = values or {}
    return SimpleNamespace(
        database_dir=tmp_path,
        wiki_dir=wiki_dir or (tmp_path / "wiki"),
        get=lambda key, default=None: values.get(key, default),
    )


def _sensitive_error_marker() -> str:
    """Construct an adversarial provider payload without a committed secret."""
    return "|".join(
        (
            "api" + "_key" + "=" + "DUMMY_CREDENTIAL_VALUE",
            "pass" + "word" + "=" + "DUMMY_CREDENTIAL_VALUE",
            "bank" + "_card" + "=" + "DUMMY_CREDENTIAL_VALUE",
            "prompt" + "=" + "PRIVATE_PROMPT_BODY",
            "response" + "=" + "PRIVATE_RESPONSE_BODY",
        )
    )


def test_health_public_error_boundaries_redact_exception_text(tmp_path, monkeypatch):
    """A raw provider failure cannot become a health JSON error payload."""
    marker = _sensitive_error_marker()

    def fail(*_args, **_kwargs):
        raise sqlite3.OperationalError(marker)

    safe_check = health_check._safe_check("model_call_ledger", fail)
    assert safe_check == {
        "status": "degraded",
        "error": "model_call_ledger: check_failed",
        "error_category": "check_failed",
    }
    assert marker not in str(safe_check)

    monkeypatch.setattr(health_check, "sqlite_artifact_exists", lambda _path: True)
    from core.kia import amphora

    monkeypatch.setattr(amphora, "list_pending", fail)
    amphora_report = health_check._check_amphora(_config(tmp_path))
    assert amphora_report["error"] == "amphora_check_failed"
    assert amphora_report["error_category"] == "amphora_check_failed"
    assert marker not in str(amphora_report)

    monkeypatch.setattr(health_check, "_connect_read_only", fail)
    processing_report = health_check._distill_processing_freshness(
        tmp_path / "distill_queue.db", 30
    )
    assert processing_report["error"] == "distill_processing_health_read_failed"
    assert processing_report["error_category"] == "distill_processing_health_read_failed"
    assert marker not in str(processing_report)

    action_db = tmp_path / "distill_actions.db"
    action_db.touch()
    actions_report = health_check._check_distill_cognitive_actions(_config(tmp_path))
    assert actions_report["error"] == "distill_cognitive_action_health_read_failed"
    assert actions_report["error_category"] == "distill_cognitive_action_health_read_failed"
    assert marker not in str(actions_report)

    (tmp_path / "daemon_heartbeat.json").write_text(marker, encoding="utf-8")
    heartbeat_report = health_check._check_heartbeat(_config(tmp_path))
    assert heartbeat_report["error"] == "daemon heartbeat unreadable"
    assert heartbeat_report["error_category"] == "daemon_heartbeat_unreadable"
    assert marker not in str(heartbeat_report)


def _write_heartbeat(tmp_path, services, monkeypatch):
    monkeypatch.setattr(
        health_check,
        "_verify_heartbeat_identity",
        lambda _payload, _config, _services: {
            "ok": True,
            "identity_match": True,
            "instance_id": "instance-1",
            "pid": 42,
            "pid_start_time": "start-1",
            "commit": "commit-1",
            "config_hash": "sha256:config",
            "config_fingerprint": "sha256:effective-config",
            "database_identity": "sha256:database",
            "service_manifest_hash": "sha256:services",
        },
    )
    (tmp_path / "daemon_heartbeat.json").write_text(
        json.dumps(
            {
                "timestamp": datetime.now().isoformat(),
                "services": services,
                "service_errors": {},
            }
        ),
        encoding="utf-8",
    )


def test_heartbeat_check_degrades_on_current_service_error(tmp_path, monkeypatch):
    _write_heartbeat(
        tmp_path,
        {
            "raw_projection": {
                "enabled": True,
                "last_ok": False,
                "last_error": "database is locked",
                "last_error_type": "OperationalError",
                "error_count": 4,
                "error_state": "current",
                "error_active": True,
            }
        },
        monkeypatch,
    )

    report = health_check._check_heartbeat(_config(tmp_path))

    assert report["status"] == "degraded"
    assert report["error"] == "daemon services have current errors"
    active = report["active_service_errors"]["raw_projection"]
    assert active["error_category"] == "daemon_service_error"
    assert active["error_state"] == "current"
    assert "last_error" not in active
    assert "last_error_type" not in active
    assert "last_error_context" not in active


def test_heartbeat_check_keeps_historical_service_error_non_blocking(tmp_path, monkeypatch):
    _write_heartbeat(
        tmp_path,
        {
            "raw_projection": {
                "enabled": True,
                "last_ok": True,
                "last_run_at": "2026-01-01T00:00:10",
                "last_error": "database is locked",
                "last_error_type": "OperationalError",
                "error_count": 4,
                "error_state": "historical",
                "error_active": False,
                "last_recovered_at": "2026-01-01T00:00:10",
            }
        },
        monkeypatch,
    )

    report = health_check._check_heartbeat(_config(tmp_path))

    assert report["status"] == "ok"
    assert "error" not in report
    historical = report["historical_service_errors"]["raw_projection"]
    assert historical["error_category"] == "daemon_service_error"
    assert historical["error_state"] == "historical"
    assert "last_error" not in historical
    assert "last_error_type" not in historical
    assert "last_error_context" not in historical


def test_heartbeat_public_projection_never_exports_provider_failure_content(
    tmp_path, monkeypatch
):
    """The default and local-debug health views share a content-free service projection."""
    marker = _sensitive_error_marker()
    _write_heartbeat(
        tmp_path,
        {
            "raw_projection": {
                "enabled": True,
                "last_ok": False,
                "last_error": marker,
                "last_error_type": "CallerControlledProviderFailure",
                "last_error_context": marker,
                "error_count": 1,
                "error_active": True,
                "source_coverage": {"error": marker},
                "metrics": {"provider_response": marker},
            }
        },
        monkeypatch,
    )

    heartbeat = health_check._check_heartbeat(_config(tmp_path))
    public_service = heartbeat["services"]["raw_projection"]
    assert marker not in json.dumps(heartbeat, ensure_ascii=False)
    assert set(public_service) <= {
        "enabled",
        "ok",
        "last_ok",
        "last_run_at",
        "error_count",
        "error_state",
        "error_active",
        "last_error_at",
        "last_recovered_at",
        "error_category",
    }
    assert public_service["error_category"] == "daemon_service_error"

    _patch_report_checks(monkeypatch, _check_heartbeat=lambda _config: dict(heartbeat))
    default_report = health_check.build_health_report(_config(tmp_path))
    debug_report = health_check.build_health_report(_config(tmp_path), show_sensitive=True)
    assert marker not in json.dumps(default_report, ensure_ascii=False)
    assert marker not in json.dumps(debug_report, ensure_ascii=False)


def test_heartbeat_check_infers_legacy_historical_service_error(tmp_path, monkeypatch):
    _write_heartbeat(
        tmp_path,
        {
            "raw_projection": {
                "enabled": True,
                "last_ok": True,
                "last_run_at": "2026-01-01T00:00:10",
                "last_error": "database is locked",
                "last_error_type": "OperationalError",
                "last_error_at": "2026-01-01T00:00:00",
                "error_count": 4,
            }
        },
        monkeypatch,
    )

    report = health_check._check_heartbeat(_config(tmp_path))

    assert report["status"] == "ok"
    assert "raw_projection" not in report["active_service_errors"]
    assert "raw_projection" in report["historical_service_errors"]


def test_heartbeat_check_rejects_timestamp_only_legacy_payload(tmp_path):
    (tmp_path / "daemon_heartbeat.json").write_text(
        json.dumps(
            {
                "timestamp": datetime.now().isoformat(),
                "services": {},
                "service_errors": {},
            }
        ),
        encoding="utf-8",
    )

    report = health_check._check_heartbeat(_config(tmp_path))

    assert report["status"] == "degraded"
    assert report["identity_match"] is False
    assert report["identity_reason"] == "unsupported_heartbeat_schema"


def test_heartbeat_invalid_timestamp_never_echoes_untrusted_value(tmp_path):
    marker = _sensitive_error_marker()
    (tmp_path / "daemon_heartbeat.json").write_text(
        json.dumps({"timestamp": marker, "services": {}}, ensure_ascii=False),
        encoding="utf-8",
    )

    report = health_check._check_heartbeat(_config(tmp_path))

    assert report["status"] == "degraded"
    assert report["error_category"] == "daemon_heartbeat_timestamp_invalid"
    assert report["timestamp_present"] is True
    assert "raw_timestamp" not in report
    assert marker not in json.dumps(report, ensure_ascii=False)


def test_heartbeat_identity_failure_does_not_echo_untrusted_service_key(tmp_path):
    marker = _sensitive_error_marker()
    (tmp_path / "daemon_heartbeat.json").write_text(
        json.dumps(
            {
                "timestamp": datetime.now().isoformat(),
                "services": {marker: {"last_error": marker}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = health_check._check_heartbeat(_config(tmp_path))

    assert report["status"] == "degraded"
    assert report["identity_reason"] == "unsupported_heartbeat_schema"
    assert report["services"] == {}
    assert report["unrecognized_service_count"] == 1
    assert marker not in json.dumps(report, ensure_ascii=False)


def test_heartbeat_check_rejects_empty_service_manifest(tmp_path):
    (tmp_path / "daemon_heartbeat.json").write_text(
        json.dumps(
            {
                "schema_version": "mnemos.daemon_heartbeat.v3",
                "timestamp": datetime.now().isoformat(),
                "instance_identity": {"instance_id": "instance-1"},
                "services": {},
                "service_errors": {},
            }
        ),
        encoding="utf-8",
    )

    report = health_check._check_heartbeat(_config(tmp_path))

    assert report["status"] == "degraded"
    assert report["identity_reason"] == "heartbeat_service_set_mismatch"
    assert report["identity"]["expected_service_count"] > 0


def test_heartbeat_check_surfaces_build_context_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(
        health_check,
        "_verify_heartbeat_identity",
        lambda _payload, _config, _services: {
            "ok": False,
            "reason": "build_fingerprint_mismatch",
            "identity_match": True,
        },
    )
    (tmp_path / "daemon_heartbeat.json").write_text(
        json.dumps(
            {
                "schema_version": "mnemos.daemon_heartbeat.v3",
                "timestamp": datetime.now().isoformat(),
                "instance_identity": {"instance_id": "instance-1"},
                "services": {"heartbeat": {"enabled": True}},
                "service_errors": {},
            }
        ),
        encoding="utf-8",
    )

    report = health_check._check_heartbeat(_config(tmp_path))

    assert report["status"] == "degraded"
    assert report["identity_match"] is True
    assert report["identity_reason"] == "build_fingerprint_mismatch"


def test_heartbeat_identity_must_match_pid_record(monkeypatch, tmp_path):
    from daemon import instance_identity, intervals, process_control

    services = {
        name: {"enabled": True} for name in intervals.build_default_intervals(capture_tick=300)
    }
    heartbeat_identity = {
        "schema_version": instance_identity.SCHEMA_VERSION,
        "instance_id": "heartbeat-instance",
        "pid": 42,
    }
    pid_identity = {**heartbeat_identity, "instance_id": "pid-instance"}
    monkeypatch.setattr(
        process_control,
        "read_pid_record",
        lambda *_args, **_kwargs: pid_identity,
    )

    result = health_check._verify_heartbeat_identity(
        {
            "schema_version": instance_identity.HEARTBEAT_SCHEMA_VERSION,
            "instance_identity": heartbeat_identity,
        },
        _config(tmp_path),
        services,
    )

    assert result["ok"] is False
    assert result["reason"] == "heartbeat_pid_identity_mismatch"
    assert "instance_id" in result["mismatched_fields"]


def test_heartbeat_identity_must_match_effective_config_fingerprint(
    monkeypatch, tmp_path
):
    from daemon import instance_identity, intervals, process_control

    services = {
        name: {"enabled": True}
        for name in intervals.build_default_intervals(capture_tick=300)
    }
    heartbeat_identity = {
        "schema_version": instance_identity.SCHEMA_VERSION,
        "instance_id": "instance-1",
        "pid": 42,
        "config_fingerprint": "sha256:heartbeat-effective-config",
    }
    pid_identity = {
        **heartbeat_identity,
        "config_fingerprint": "sha256:pid-effective-config",
    }
    monkeypatch.setattr(
        process_control,
        "read_pid_record",
        lambda *_args, **_kwargs: pid_identity,
    )
    monkeypatch.setattr(
        instance_identity,
        "verify_instance_record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("mismatch must fail before live verification")
        ),
    )

    result = health_check._verify_heartbeat_identity(
        {
            "schema_version": instance_identity.HEARTBEAT_SCHEMA_VERSION,
            "instance_identity": heartbeat_identity,
        },
        _config(tmp_path),
        services,
    )

    assert result["ok"] is False
    assert result["reason"] == "heartbeat_pid_identity_mismatch"
    assert result["mismatched_fields"] == ["config_fingerprint"]


def test_heartbeat_identity_surfaces_effective_config_fingerprint(
    monkeypatch, tmp_path
):
    from daemon import instance_identity, intervals, process_control

    services = {
        name: {"enabled": True}
        for name in intervals.build_default_intervals(capture_tick=300)
    }
    identity = {
        "schema_version": instance_identity.SCHEMA_VERSION,
        "instance_id": "instance-1",
        "pid": 42,
        "pid_start_time": "start-1",
        "boot_id": "boot-1",
        "executable": "python",
        "command_line_hash": "sha256:command",
        "commit": "commit-1",
        "build_fingerprint": "sha256:build",
        "config_hash": "sha256:file-config",
        "config_fingerprint": "sha256:effective-config",
        "database_identity": "sha256:database",
        "service_manifest": sorted(services),
        "service_manifest_hash": "sha256:services",
        "python": "python",
    }
    monkeypatch.setattr(
        process_control,
        "read_pid_record",
        lambda *_args, **_kwargs: identity,
    )
    verification = SimpleNamespace(
        ok=True,
        details={
            "current_commit": "commit-1",
            "commit_match": True,
            "build_compatible": True,
        },
        to_dict=lambda: {
            "ok": True,
            "identity_match": True,
            "instance_id": "instance-1",
            "pid": 42,
        },
    )
    monkeypatch.setattr(
        instance_identity,
        "verify_instance_record",
        lambda *_args, **_kwargs: verification,
    )

    result = health_check._verify_heartbeat_identity(
        {
            "schema_version": instance_identity.HEARTBEAT_SCHEMA_VERSION,
            "instance_identity": identity,
        },
        _config(tmp_path),
        services,
    )

    assert result["ok"] is True
    assert result["config_fingerprint"] == "sha256:effective-config"


def test_amphora_check_degrades_when_failed_tasks_exist(monkeypatch):
    from core.kia import amphora

    monkeypatch.setattr(amphora, "list_pending", lambda include_future_retry=False: [])
    monkeypatch.setattr(amphora, "list_processing", lambda: [])
    monkeypatch.setattr(
        amphora,
        "get_task_count",
        lambda status: {"done": 5, "failed": 1, "archived": 0}.get(status, 0),
    )

    report = health_check._check_amphora()

    assert report["status"] == "degraded"
    assert report["failed"] == 1
    assert report["failed_task_budget"] == 0
    assert report["error"] == "amphora has failed distillation tasks"


def test_amphora_check_names_receipt_reconciliation_command(monkeypatch):
    from core.kia import amphora

    monkeypatch.setattr(amphora, "list_pending", lambda include_future_retry=False: [])
    monkeypatch.setattr(amphora, "list_processing", lambda: [])
    monkeypatch.setattr(
        amphora,
        "get_task_count",
        lambda status: {"done": 0, "reconciliation_required": 2}.get(status, 0),
    )

    report = health_check._check_amphora()

    assert report["status"] == "degraded"
    assert report["reconciliation_required"] == 2
    assert "reconcile_pipeline_receipts.py --apply" in report["repair_action"]


def test_amphora_check_degrades_when_processing_task_is_stale(monkeypatch):
    from core.kia import amphora

    stale_at = (datetime.now() - timedelta(minutes=31)).isoformat()
    monkeypatch.setattr(amphora, "list_pending", lambda include_future_retry=False: [])
    monkeypatch.setattr(
        amphora,
        "list_processing",
        lambda: [
            {
                "task_id": "task-1",
                "session_id": "sess-1",
                "created_at": stale_at,
                "started_at": stale_at,
                "updated_at": stale_at,
                "progress_step": "extracting",
                "progress_detail": "正在提炼知识...",
                "error": None,
            }
        ],
    )
    monkeypatch.setattr(
        amphora,
        "get_task_count",
        lambda status: {"done": 5, "failed": 0, "archived": 0}.get(status, 0),
    )

    report = health_check._check_amphora()

    assert report["status"] == "degraded"
    assert report["processing"] == 1
    assert report["stale_processing"] == 1
    assert report["stale_processing_budget"] == 0
    assert report["stale_processing_tasks"][0]["task_id"] == "task-1"
    assert report["error"] == "amphora has stale processing distillation tasks"


def test_stale_processing_health_projection_omits_raw_task_failure_content():
    marker = _sensitive_error_marker()
    stale_at = (datetime.now() - timedelta(minutes=31)).isoformat()

    projected = health_check._stale_processing_tasks(
        [
            {
                "task_id": "task-1",
                "session_id": "session-1",
                "created_at": stale_at,
                "started_at": stale_at,
                "updated_at": stale_at,
                "progress_step": "extracting",
                "progress_detail": marker,
                "error": marker,
            }
        ],
        timeout_minutes=30,
    )

    assert len(projected) == 1
    assert marker not in json.dumps(projected, ensure_ascii=False)
    assert projected[0]["error_category"] == "distill_task_processing_error"
    assert "progress_detail" not in projected[0]
    assert "error" not in projected[0]


def test_amphora_check_uses_latest_processing_activity_timestamp(monkeypatch):
    from core.kia import amphora

    stale_at = (datetime.now() - timedelta(minutes=31)).isoformat()
    fresh_at = datetime.now().isoformat()
    monkeypatch.setattr(amphora, "list_pending", lambda include_future_retry=False: [])
    monkeypatch.setattr(
        amphora,
        "list_processing",
        lambda: [
            {
                "task_id": "task-1",
                "session_id": "sess-1",
                "created_at": stale_at,
                "started_at": fresh_at,
                "updated_at": stale_at,
                "progress_step": "extracting",
                "progress_detail": "daemon picked up task",
                "error": None,
            }
        ],
    )
    monkeypatch.setattr(
        amphora,
        "get_task_count",
        lambda status: {"done": 5, "failed": 0, "archived": 0}.get(status, 0),
    )

    report = health_check._check_amphora()

    assert report["status"] == "ok"
    assert report["processing"] == 1
    assert report["stale_processing"] == 0


def test_queue_backlog_check_degrades_when_budgets_are_exceeded(tmp_path):
    with sqlite3.connect(str(tmp_path / "distill_queue.db")) as conn:
        conn.execute("CREATE TABLE distillation_tasks (status TEXT)")
        conn.execute("INSERT INTO distillation_tasks(status) VALUES ('failed')")
    with sqlite3.connect(str(tmp_path / "recap_tasks.db")) as conn:
        conn.execute("CREATE TABLE recap_tasks (status TEXT, severity TEXT)")
        conn.execute("INSERT INTO recap_tasks(status, severity) VALUES ('pending', 'high')")
    with sqlite3.connect(str(tmp_path / "dialog_reminder.db")) as conn:
        conn.execute("CREATE TABLE dialog_reminders (status TEXT)")
        conn.executemany(
            "INSERT INTO dialog_reminders(status) VALUES (?)",
            [("pending",)] * 501,
        )

    report = health_check._check_queue_backlog(_config(tmp_path))

    assert report["status"] == "degraded"
    assert report["error"] == "queue backlog exceeds health budgets"
    assert report["distill"]["failed"] == 1
    assert report["recap"]["high_or_critical_pending"] == 1
    assert report["dialog_reminder"]["pending"] == 501
    assert set(report["over_budget"]) == {
        "distill_failed",
        "recap_high_pending",
        "dialog_pending",
        "dialog_active",
    }


def test_queue_backlog_check_degrades_when_distill_processing_is_stale(tmp_path):
    stale_at = (datetime.now() - timedelta(minutes=31)).isoformat()
    with sqlite3.connect(str(tmp_path / "distill_queue.db")) as conn:
        conn.execute("""
            CREATE TABLE distillation_tasks (
                task_id TEXT,
                session_id TEXT,
                status TEXT,
                created_at TEXT,
                started_at TEXT,
                updated_at TEXT,
                progress_step TEXT,
                progress_detail TEXT,
                error TEXT
            )
            """)
        conn.execute(
            """
            INSERT INTO distillation_tasks(
                task_id, session_id, status, created_at, started_at, updated_at,
                progress_step, progress_detail, error
            )
            VALUES (?, ?, 'processing', ?, ?, ?, 'extracting', 'stuck', NULL)
            """,
            ("task-1", "sess-1", stale_at, stale_at, stale_at),
        )

    report = health_check._check_queue_backlog(_config(tmp_path))

    assert report["status"] == "degraded"
    assert "distill_processing_stale" in report["over_budget"]
    freshness = report["distill"]["processing_freshness"]
    assert freshness["processing"] == 1
    assert freshness["stale_processing"] == 1
    assert freshness["stale_processing_tasks"][0]["task_id"] == "task-1"


def test_distill_json_quality_health_reports_metrics(tmp_path):
    from core.hephaestus.distillation_json import extract_json_with_metadata
    from core.hephaestus.distillation_metrics import record_json_parse_event

    record_json_parse_event(tmp_path, extract_json_with_metadata('{"ok": true}'))
    record_json_parse_event(tmp_path, extract_json_with_metadata("not json"))

    report = health_check._check_distill_json_quality(_config(tmp_path))

    assert report["schema_version"] == "mnemos.distill_json_quality.v1"
    assert report["total_events"] == 2
    assert report["failed"] == 1
    assert report["rates"]["final_failure_rate"] == 0.5


def test_distill_cognitive_actions_health_reports_counts(tmp_path):
    db_path = tmp_path / "distill_actions.db"
    artifact_dir = tmp_path / "distill_cognitive_actions" / "2026-07-04"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "dca_1.json").write_text("{}", encoding="utf-8")
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("""
            CREATE TABLE cognitive_action_log (
                cognitive_action TEXT NOT NULL,
                status TEXT NOT NULL
            )
            """)
        conn.executemany(
            "INSERT INTO cognitive_action_log(cognitive_action, status) VALUES (?, ?)",
            [
                ("create_observation", "queued"),
                ("create_observation", "queued"),
                ("propose_methodology", "done"),
            ],
        )

    report = health_check._check_distill_cognitive_actions(_config(tmp_path))

    assert report["schema_version"] == "mnemos.distill_cognitive_actions_health.v1"
    assert report["status"] == "warning"
    assert report["table_exists"] is True
    assert report["total_actions"] == 3
    assert report["counts"] == {"create_observation": 2, "propose_methodology": 1}
    assert report["status_counts"] == {"done": 1, "queued": 2}
    assert report["queued_over_budget"] is True
    assert report["artifact_count"] == 1


def test_distill_cognitive_actions_health_uses_registered_queued_budget(tmp_path):
    db_path = tmp_path / "distill_actions.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "CREATE TABLE cognitive_action_log (cognitive_action TEXT NOT NULL, status TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO cognitive_action_log(cognitive_action, status) VALUES (?, ?)",
            [("create_observation", "queued"), ("create_observation", "queued")],
        )

    report = health_check._check_distill_cognitive_actions(
        _config(tmp_path, values={"distill.cognitive_actions.queued_budget": 2})
    )

    assert report["queued_budget"] == 2
    assert report["queued_over_budget"] is False
    assert report["status"] == "ok"


def test_wiki_route_health_degrades_when_budgets_are_exceeded(tmp_path):
    wiki = tmp_path / "wiki"
    inbox = wiki / "00-Inbox"
    inbox.mkdir(parents=True)
    (inbox / "redis.md").write_text(
        "---\n类型: tech\n名称: Redis\n领域: redis\n摘要: 可自动路由\n---\n# Redis\n",
        encoding="utf-8",
    )
    cfg = _config(
        tmp_path,
        wiki_dir=wiki,
        values={"health.wiki_route_budgets.inbox_ready_to_classify": 0},
    )

    report = health_check._check_wiki_route(cfg)

    assert report["schema_version"] == "mnemos.wiki_route_health.v1"
    assert report["status"] == "degraded"
    assert report["error"] == "wiki route budgets exceeded"
    assert report["counts"]["inbox_ready_to_classify"] == 1
    assert report["over_budget"] == ["inbox_ready_to_classify"]


def test_wiki_route_health_degrades_when_wiki_dir_is_missing(tmp_path):
    cfg = SimpleNamespace(
        database_dir=tmp_path,
        get=lambda key, default=None: default,
    )

    report = health_check._check_wiki_route(cfg)

    assert report["status"] == "degraded"
    assert report["error"] == "wiki route vault path is not configured"


def _patch_report_checks(monkeypatch, **overrides):
    ok_check = {"status": "ok"}
    checks = {
        "_check_storage": lambda _config: dict(ok_check),
        "_check_wiki": lambda _config: dict(ok_check),
        "_check_agents": lambda: dict(ok_check),
        "_check_daemon": lambda: dict(ok_check),
        "_check_event_bus": lambda _config: dict(ok_check),
        "_check_schema": lambda _config: dict(ok_check),
        "_check_amphora": lambda: dict(ok_check),
        "_check_queue_backlog": lambda _config: dict(ok_check),
        "_check_disk": lambda _config: dict(ok_check),
        "_check_api": lambda _config: dict(ok_check),
        "_check_multimodal_model": lambda _config: dict(ok_check),
        "_check_heartbeat": lambda _config: dict(ok_check),
        "_check_system_contracts": lambda: dict(ok_check),
        "_check_module_toggles": lambda _config: dict(ok_check),
        "_check_runtime_producer_consumer": lambda _config: dict(ok_check),
        "_check_migrations": lambda _config: dict(ok_check),
        "_check_backup": lambda _config: dict(ok_check),
        "_check_data_ownership": lambda _config: dict(ok_check),
        "_check_model_call_ledger": lambda _config: dict(ok_check),
        "_check_golden_benchmark": lambda: dict(ok_check),
        "_check_distill_json_quality": lambda _config: dict(ok_check),
        "_check_distill_cognitive_actions": lambda _config: dict(ok_check),
        "_check_wiki_route": lambda _config: dict(ok_check),
        "_check_wiki_projection": lambda _config: dict(ok_check),
        "_check_install_lifecycle": lambda _config: dict(ok_check),
        "_check_sqlite_disk_budget": lambda _config: dict(ok_check),
        "_check_adaptive_policy": lambda _config: dict(ok_check),
        "_check_cognitive_readiness": lambda _config: dict(ok_check),
        "_check_cognitive_learning": lambda _config: dict(ok_check),
        "_check_security": lambda _config: dict(ok_check),
    }
    checks.update(overrides)
    for name, func in checks.items():
        monkeypatch.setattr(health_check, name, func)


def test_build_health_report_treats_amphora_as_strict_check(tmp_path, monkeypatch):
    _patch_report_checks(
        monkeypatch,
        _check_amphora=lambda: {
            "status": "degraded",
            "failed": 1,
            "error": "amphora has failed distillation tasks",
        },
    )

    report = health_check.build_health_report(_config(tmp_path))

    assert report["status"] == "degraded"
    assert report["ok"] is False
    assert report["usable"] is False
    assert report["strict_ok"] is False
    assert "amphora" in report["strict_failures"]
    assert "amphora: amphora has failed distillation tasks" in report["errors"]


def test_build_health_report_treats_wiki_route_as_strict_check(tmp_path, monkeypatch):
    _patch_report_checks(
        monkeypatch,
        _check_wiki_route=lambda _config: {
            "status": "degraded",
            "error": "wiki route budgets exceeded",
        },
    )

    report = health_check.build_health_report(_config(tmp_path))

    assert report["status"] == "degraded"
    assert report["ok"] is False
    assert report["strict_ok"] is False
    assert "wiki_route" in report["strict_failures"]
    assert "wiki_route: wiki route budgets exceeded" in report["errors"]


def test_build_health_report_treats_queue_backlog_as_strict_check(tmp_path, monkeypatch):
    _patch_report_checks(
        monkeypatch,
        _check_queue_backlog=lambda _config: {
            "status": "degraded",
            "error": "queue backlog exceeds health budgets",
        },
    )

    report = health_check.build_health_report(_config(tmp_path))

    assert report["status"] == "degraded"
    assert report["usable"] is False
    assert report["strict_ok"] is False
    assert "queues" in report["strict_failures"]
    assert "queues: queue backlog exceeds health budgets" in report["errors"]


def test_build_health_report_treats_sqlite_disk_budget_as_strict_check(tmp_path, monkeypatch):
    _patch_report_checks(
        monkeypatch,
        _check_sqlite_disk_budget=lambda _config: {
            "status": "degraded",
            "error": "sqlite disk budget exceeded",
        },
    )

    report = health_check.build_health_report(_config(tmp_path))

    assert report["status"] == "degraded"
    assert report["usable"] is False
    assert report["strict_ok"] is False
    assert "sqlite_disk_budget" in report["strict_failures"]
    assert "sqlite_disk_budget: sqlite disk budget exceeded" in report["errors"]


def test_build_health_report_treats_model_call_ledger_as_strict_check(tmp_path, monkeypatch):
    _patch_report_checks(
        monkeypatch,
        _check_model_call_ledger=lambda _config: {
            "status": "degraded",
            "error": "legacy_prompt_storage_not_reconciled",
        },
    )

    report = health_check.build_health_report(_config(tmp_path))

    assert report["status"] == "degraded"
    assert report["strict_ok"] is False
    assert "model_call_ledger" in report["strict_failures"]


def test_build_health_report_treats_install_lifecycle_as_strict_check(tmp_path, monkeypatch):
    _patch_report_checks(
        monkeypatch,
        _check_install_lifecycle=lambda _config: {
            "status": "degraded",
            "state": {"status": "installed_partial"},
            "error": "install_lifecycle_state: installed_partial",
        },
    )

    report = health_check.build_health_report(_config(tmp_path))

    assert report["status"] == "degraded"
    assert report["usable"] is False
    assert report["strict_ok"] is False
    assert "install_lifecycle" in report["strict_failures"]
    assert "install_lifecycle: install_lifecycle_state: installed_partial" in report["errors"]


def test_build_health_report_treats_cognitive_readiness_as_strict_check(tmp_path, monkeypatch):
    _patch_report_checks(
        monkeypatch,
        _check_cognitive_readiness=lambda _config: {
            "status": "degraded",
            "budget_ok": False,
            "failure_count": 2,
            "score": 84,
            "error": "cognitive readiness budget failed",
        },
    )

    report = health_check.build_health_report(_config(tmp_path))

    assert report["status"] == "degraded"
    assert report["ok"] is False
    assert report["usable"] is False
    assert report["strict_ok"] is False
    assert "cognitive_readiness" in report["strict_failures"]
    assert "cognitive_readiness: cognitive readiness budget failed" in report["errors"]
    assert report["checks"]["cognitive_readiness"]["failure_count"] == 2


def test_build_health_report_treats_runtime_producer_consumer_as_strict_check(
    tmp_path, monkeypatch
):
    _patch_report_checks(
        monkeypatch,
        _check_runtime_producer_consumer=lambda _config: {
            "status": "degraded",
            "error": "runtime producer/consumer budgets exceeded",
        },
    )

    report = health_check.build_health_report(_config(tmp_path))

    assert report["status"] == "degraded"
    assert report["usable"] is False
    assert report["strict_ok"] is False
    assert "runtime_producer_consumer" in report["strict_failures"]
    assert (
        "runtime_producer_consumer: runtime producer/consumer budgets exceeded" in report["errors"]
    )


def test_build_health_report_treats_agent_runtime_degradation_as_strict(tmp_path, monkeypatch):
    _patch_report_checks(
        monkeypatch,
        _check_agents=lambda: {"status": "degraded", "error": "agent not full power"},
    )

    report = health_check.build_health_report(_config(tmp_path))

    assert report["status"] == "degraded"
    assert report["ok"] is False
    assert report["usable"] is False
    assert report["strict_ok"] is False
    assert "agent" in report["strict_failures"]
    assert "agent: agent not full power" in report["errors"]


def test_build_health_report_ignores_optional_skipped_multimodal(tmp_path, monkeypatch):
    _patch_report_checks(
        monkeypatch,
        _check_multimodal_model=lambda _config: {
            "status": "skipped",
            "optional": True,
            "endpoint_status": "skipped",
            "repair_actions": ["Set MNEMOS_MULTIMODAL_*"],
        },
    )

    report = health_check.build_health_report(_config(tmp_path))

    assert report["status"] == "ok"
    assert report["ok"] is True
    assert report["usable"] is True
    assert report["strict_ok"] is True
    assert report["warning_checks"] == []
    assert report["checks"]["multimodal"]["status"] == "skipped"
    assert report["checks"]["multimodal"]["endpoint_status"] == "skipped"
    assert report["checks"]["auto_healing"]["status"] == "ok"


def test_build_health_report_surfaces_security_warning_as_non_strict(tmp_path, monkeypatch):
    _patch_report_checks(
        monkeypatch,
        _check_security=lambda _config: {
            "status": "warning",
            "keyring_available": False,
            "warnings": ["keyring unavailable; env fallback accepted"],
            "repair_actions": ["install keyring or use api_key_env"],
        },
    )

    report = health_check.build_health_report(_config(tmp_path))

    assert report["status"] == "warning"
    assert report["usable"] is True
    assert report["strict_ok"] is True
    assert "security" in report["warning_checks"]
    assert "security: keyring unavailable; env fallback accepted" in report["warnings"]
    assert report["checks"]["security"]["keyring_available"] is False


def test_build_health_report_surfaces_cognitive_learning_as_non_strict_warning(
    tmp_path,
    monkeypatch,
):
    _patch_report_checks(
        monkeypatch,
        _check_cognitive_learning=lambda _config: {
            "status": "warning",
            "gap_names": ["consolidation_run_gap"],
        },
    )

    report = health_check.build_health_report(_config(tmp_path))

    assert report["status"] == "warning"
    assert report["usable"] is True
    assert report["strict_ok"] is True
    assert "cognitive_learning" in report["warning_checks"]
    assert "cognitive_learning" not in report["strict_failures"]


def _successful_readiness_report(*, raw_signal_count=0):
    return {
        "schema_version": "mnemos.cognitive_readiness.v2",
        "ok": True,
        "budget": {"ok": True, "failure_count": 0, "failures": []},
        "scorecard": {"score": 100, "max_score": 100, "blocking_findings": []},
        "readiness": {},
        "metrics": {
            "learning_signal": {
                "schema_version": "mnemos.learning_signal.v2",
                "raw_signal_count": raw_signal_count,
            }
        },
        "mode": {"strict": True, "budget": True, "side_effects": "none"},
        "generated_at": "2026-07-15T00:00:00+00:00",
    }


def _patch_health_checks_with_real_readiness(monkeypatch):
    readiness_check = health_check._check_cognitive_readiness
    learning_check = health_check._check_cognitive_learning
    _patch_report_checks(
        monkeypatch,
        _check_cognitive_readiness=readiness_check,
        _check_cognitive_learning=learning_check,
    )


def test_health_reuses_one_bounded_cognitive_readiness_report(tmp_path, monkeypatch):
    from core.ops import cognitive_readiness

    _patch_health_checks_with_real_readiness(monkeypatch)
    calls = []

    def build_report(_config, **kwargs):
        calls.append(kwargs)
        return _successful_readiness_report(raw_signal_count=7)

    monkeypatch.setattr(cognitive_readiness, "build_cognitive_readiness_report", build_report)

    report = health_check.build_health_report(_config(tmp_path))

    assert calls == [{"strict": True, "enforce_budget": True}]
    assert report["checks"]["cognitive_readiness"]["status"] == "ok"
    assert report["checks"]["cognitive_learning"]["raw_signal_count"] == 7


def test_health_readiness_timeout_is_typed_strict_failure_and_readonly(tmp_path, monkeypatch):
    from core.ops import cognitive_readiness
    from core.ops import cognitive_readiness_lineage

    _patch_health_checks_with_real_readiness(monkeypatch)
    db_path = tmp_path / "readiness-probe.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE probe (value INTEGER)")
        conn.execute("INSERT INTO probe VALUES (1)")
    before = db_path.read_bytes()

    def build_report(_config, **_kwargs):
        # This uses the lineage module's normal read-only connection path,
        # rather than calling the budget helper directly.
        with cognitive_readiness_lineage._connect_ro(db_path) as conn:
            conn.execute(
                """
                WITH RECURSIVE series(value) AS (
                    SELECT 1
                    UNION ALL
                    SELECT value + 1 FROM series WHERE value < 100000000
                )
                SELECT SUM(value) FROM series
                """
            ).fetchone()
        return _successful_readiness_report()

    monkeypatch.setattr(cognitive_readiness, "build_cognitive_readiness_report", build_report)
    monkeypatch.setattr(
        health_check,
        "HEALTH_COGNITIVE_READINESS_QUERY_TIMEOUT_SECONDS",
        0.02,
    )

    report = health_check.build_health_report(_config(tmp_path))

    readiness = report["checks"]["cognitive_readiness"]
    assert readiness["status"] == "degraded"
    assert readiness["error"] == "readiness_query_timeout"
    assert readiness["error_category"] == "readiness_query_timeout"
    assert (
        report["checks"]["cognitive_learning"]["error_category"]
        == "readiness_query_timeout"
    )
    assert report["strict_ok"] is False
    assert "cognitive_readiness" in report["strict_failures"]
    assert "interrupted" not in json.dumps(readiness)
    assert db_path.read_bytes() == before
    assert not (tmp_path / "readiness-probe.db-wal").exists()
    assert not (tmp_path / "readiness-probe.db-shm").exists()
    assert not (tmp_path / "readiness-probe.db-journal").exists()


def test_health_readiness_busy_database_is_typed_strict_failure(tmp_path, monkeypatch):
    from core.ops import cognitive_readiness
    from core.ops.readiness_query_budget import connect_readonly_sqlite

    _patch_health_checks_with_real_readiness(monkeypatch)
    db_path = tmp_path / "readiness-locked.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE probe (value INTEGER)")
        conn.execute("INSERT INTO probe VALUES (1)")
    before = db_path.read_bytes()
    lock_connection = sqlite3.connect(db_path, timeout=0)
    lock_connection.execute("BEGIN EXCLUSIVE")

    def build_report(_config, **_kwargs):
        with connect_readonly_sqlite(db_path) as conn:
            conn.execute("SELECT COUNT(*) FROM probe").fetchone()
        return _successful_readiness_report()

    monkeypatch.setattr(cognitive_readiness, "build_cognitive_readiness_report", build_report)
    monkeypatch.setattr(
        health_check,
        "HEALTH_COGNITIVE_READINESS_QUERY_TIMEOUT_SECONDS",
        0.2,
    )
    try:
        report = health_check.build_health_report(_config(tmp_path))
    finally:
        lock_connection.rollback()
        lock_connection.close()

    readiness = report["checks"]["cognitive_readiness"]
    assert readiness["status"] == "degraded"
    assert readiness["error"] == "readiness_db_busy"
    assert readiness["error_category"] == "readiness_db_busy"
    assert report["checks"]["cognitive_learning"]["error_category"] == "readiness_db_busy"
    assert report["strict_ok"] is False
    assert "cognitive_readiness" in report["strict_failures"]
    assert db_path.read_bytes() == before


def test_authoritative_readonly_sqlite_rejects_leaf_symlink(
    tmp_path: Path,
) -> None:
    from core.ops.durable_io import DurableIOError
    from core.ops.readiness_query_budget import connect_readonly_sqlite

    database = tmp_path / "authoritative.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE probe (value INTEGER)")
    link = tmp_path / "authoritative-link.db"
    link.symlink_to(database)

    with pytest.raises(DurableIOError, match="readonly_sqlite_path_not_regular"):
        connect_readonly_sqlite(link)


def test_authoritative_readonly_sqlite_rejects_path_replacement_during_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.ops import readiness_query_budget
    from core.ops.durable_io import DurableIOError

    database = tmp_path / "authoritative.db"
    replacement = tmp_path / "replacement.db"
    for path, value in ((database, 1), (replacement, 2)):
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE probe (value INTEGER)")
            connection.execute("INSERT INTO probe VALUES (?)", (value,))
    original_connect = readiness_query_budget.sqlite3.connect

    def replace_then_connect(*args, **kwargs):
        database.unlink()
        replacement.replace(database)
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(
        readiness_query_budget.sqlite3,
        "connect",
        replace_then_connect,
    )

    with pytest.raises(DurableIOError, match="readonly_sqlite_identity_changed"):
        readiness_query_budget.connect_readonly_sqlite(database)


def test_health_readiness_deadline_reaches_canonical_raw_read_only_connection(tmp_path):
    from core.ops.readiness_query_budget import (
        READINESS_QUERY_TIMEOUT,
        health_readiness_query_budget,
        readiness_query_failure_code,
    )
    from core.sync_framework.raw_event_reader import _read_only_raw_connection

    db_path = tmp_path / "raw-readiness-probe.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE probe (value INTEGER)")
        conn.execute("INSERT INTO probe VALUES (1)")
    before = db_path.read_bytes()

    with health_readiness_query_budget(0.02):
        try:
            with _read_only_raw_connection(db_path) as conn:
                conn.execute(
                    """
                    WITH RECURSIVE series(value) AS (
                        SELECT 1
                        UNION ALL
                        SELECT value + 1 FROM series WHERE value < 100000000
                    )
                    SELECT SUM(value) FROM series
                    """
                ).fetchone()
        except sqlite3.OperationalError:
            pass
        assert readiness_query_failure_code() == READINESS_QUERY_TIMEOUT
    assert db_path.read_bytes() == before
    assert not (tmp_path / "raw-readiness-probe.db-wal").exists()
    assert not (tmp_path / "raw-readiness-probe.db-shm").exists()


def test_canonical_raw_reader_does_not_label_uninspectable_database_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.sync_framework.raw_event_reader import (
        CanonicalRawReadError,
        _read_only_raw_connection,
    )

    db_path = tmp_path / "raw_events.db"
    original_stat = Path.stat

    def denied(path: Path, *args: object, **kwargs: object):
        if path == db_path:
            raise PermissionError("sentinel")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)

    with pytest.raises(CanonicalRawReadError, match="database is unavailable"):
        _read_only_raw_connection(db_path)
