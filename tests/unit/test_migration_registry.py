import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from core.migrations.model_call_ledger_reconcile import runtime
from core.migrations.registry import (
    ACTION_LEDGER_MIGRATION_ID,
    COGNITIVE_STATE_STORE_MIGRATION_ID,
    DECISION_TRACE_HISTORY_MIGRATION_ID,
    MATERIAL_EFFECT_SCHEMA_MIGRATION_ID,
    MigrationLedger,
    MigrationLedgerRecord,
    MigrationRegistry,
    audit_migration_registry,
    build_migration_health,
)


class FakeConfig:
    def __init__(self, root: Path, data: dict | None = None):
        self.mnemos_dir = root
        self.data_dir = root
        self.database_dir = root / "db"
        self.database_dir.mkdir(parents=True)
        self.config_path = root / "configs" / "main.json"
        self.config_path.parent.mkdir(parents=True)
        self._data = data or {}
        self.save()

    def save(self):
        self.config_path.write_text(json.dumps(self._data), encoding="utf-8")

    def to_dict(self):
        return json.loads(json.dumps(self._data))

    def get(self, key, default=None):
        value = self._data
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value

    def vault_dir(self, name: str) -> Path:
        path = self.mnemos_dir / name
        path.mkdir(exist_ok=True)
        return path


def test_migration_registry_lists_wrapped_scripts_and_config_aliases():
    errors = audit_migration_registry(strict=True)
    assert errors == []
    registry = MigrationRegistry()
    assert "config.stale_keys.v1" in registry.specs
    assert "database.sync_log_schema.v1" in registry.specs
    assert "database.model_call_ledger.v1" in registry.specs
    assert COGNITIVE_STATE_STORE_MIGRATION_ID in registry.specs
    assert ACTION_LEDGER_MIGRATION_ID in registry.specs
    assert DECISION_TRACE_HISTORY_MIGRATION_ID in registry.specs
    assert MATERIAL_EFFECT_SCHEMA_MIGRATION_ID in registry.specs
    assert "vault.layout.v2" in registry.specs
    model_call_spec = registry.specs["database.model_call_ledger.v1"]
    assert model_call_spec.affected_paths[0] == "core/migrations/model_call_ledger_reconcile"
    assert model_call_spec.wrapper_command == (
        "python3",
        "scripts/reconcile_model_call_ledger.py",
        "--json",
    )


def test_dedicated_cognitive_migrations_are_planned_but_never_generically_applied(
    tmp_path,
):
    from core.cognitive.state_schema import initialize_cognitive_state_schema
    from core.ops.action_ledger_schema import initialize_action_ledger_schema

    cfg = FakeConfig(tmp_path)
    registry = MigrationRegistry()
    initial = {item.migration_id: item for item in registry.plan(cfg).items}
    assert initial[COGNITIVE_STATE_STORE_MIGRATION_ID].status == "verified"
    assert initial[ACTION_LEDGER_MIGRATION_ID].status == "verified"
    assert initial[DECISION_TRACE_HISTORY_MIGRATION_ID].status == "verified"
    assert initial[MATERIAL_EFFECT_SCHEMA_MIGRATION_ID].status == "verified"

    initialize_cognitive_state_schema(cfg.database_dir / "producer_consumer_ledger.db")
    action_db = cfg.database_dir / "action_ledger.db"
    initialize_action_ledger_schema(action_db)
    canonical = {item.migration_id: item for item in registry.plan(cfg).items}
    assert canonical[COGNITIVE_STATE_STORE_MIGRATION_ID].status == "verified"
    assert canonical[ACTION_LEDGER_MIGRATION_ID].status == "verified"

    delivery_db = cfg.database_dir / "delivery_events.db"
    with sqlite3.connect(delivery_db) as conn:
        conn.execute("CREATE TABLE delivery_events (event_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO delivery_events VALUES ('uncovered-event')")
    uncovered = {item.migration_id: item for item in registry.plan(cfg).items}
    assert uncovered[DECISION_TRACE_HISTORY_MIGRATION_ID].status == "planned"

    legacy_target = cfg.database_dir / "policy_patches.db"
    with sqlite3.connect(legacy_target) as conn:
        conn.execute("CREATE TABLE policy_patches (patch_id TEXT PRIMARY KEY)")
    target_drift = {item.migration_id: item for item in registry.plan(cfg).items}
    assert target_drift[MATERIAL_EFFECT_SCHEMA_MIGRATION_ID].status == "planned"

    from core.cognitive.material_effect_schema import reconcile_material_effect_schema

    with sqlite3.connect(legacy_target) as conn:
        reconcile_material_effect_schema(conn, apply=True)
    target_canonical = {item.migration_id: item for item in registry.plan(cfg).items}
    assert target_canonical[MATERIAL_EFFECT_SCHEMA_MIGRATION_ID].status == "verified"

    with sqlite3.connect(action_db) as conn:
        conn.execute("DROP TRIGGER action_ledger_no_update")
        conn.execute("DELETE FROM mnemos_schema_registry WHERE component='action_ledger'")
    drifted = {item.migration_id: item for item in registry.plan(cfg).items}
    assert drifted[ACTION_LEDGER_MIGRATION_ID].status == "planned"

    blocked = registry.apply(cfg, ACTION_LEDGER_MIGRATION_ID)
    assert blocked.status == "blocked"
    assert blocked.verification == {"dedicated_reconciliation_required": True}
    rollback = registry.rollback(cfg, ACTION_LEDGER_MIGRATION_ID)
    assert rollback.status == "blocked"
    assert rollback.verification == {"dedicated_backup_restore_required": True}
    assert not (cfg.mnemos_dir / "migrations.db").exists()


def test_registry_module_main_projects_model_ledger_records_before_json_output(
    tmp_path, monkeypatch, capsys
):
    """The module entry point cannot bypass the public record renderer."""
    import core.config as config_module
    import core.migrations.registry as registry_module

    cfg = FakeConfig(tmp_path / "private-runtime")
    private_value = "caller" + "-private-value"
    record = MigrationLedgerRecord(
        ledger_id="migration-record",
        migration_id="database.model_call_ledger.v1",
        status="noop",
        plan_hash="sha256:" + "1" * 64,
        from_version="v1",
        to_version="v2",
        backup_ref=str(cfg.mnemos_dir / "backups" / "private-backup"),
        rollback_ref=str(cfg.mnemos_dir / "backups" / "private-recovery.json"),
        actor="test",
        error=private_value,
        verification={
            "execution_plan_hash": "sha256:" + "2" * 64,
            "reviewed_plan_hash": private_value,
        },
    )

    class FakeRegistry:
        def apply(self, *_args, **_kwargs):
            return record

    monkeypatch.setattr(config_module, "Config", lambda **_kwargs: cfg)
    monkeypatch.setattr(registry_module, "MigrationRegistry", FakeRegistry)

    assert (
        registry_module.main(
            [
                "apply",
                "database.model_call_ledger.v1",
                "--execute-wrapped",
                "--expected-plan-hash",
                private_value,
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert private_value not in json.dumps(payload, ensure_ascii=False)
    assert str(cfg.mnemos_dir) not in json.dumps(payload, ensure_ascii=False)
    assert payload["backup_ref"].startswith("<MNEMOS_DIR>/backups/")
    assert payload["rollback_ref"].startswith("<MNEMOS_DIR>/backups/")
    assert payload["verification"]["reviewed_plan_hash_present"] is True
    assert payload["verification"]["reviewed_plan_hash_matches"] is False
    assert payload["error"] == "model_call_ledger_record_error"


def test_migration_status_projects_historical_model_ledger_verification(tmp_path):
    """Status must redact a legacy record before its JSON leaves the registry."""
    cfg = FakeConfig(tmp_path / "private-runtime")
    private_value = "historic" + "-caller-private-value"
    MigrationLedger.from_config(cfg).record(
        MigrationLedgerRecord(
            ledger_id="historical-record",
            migration_id="database.model_call_ledger.v1",
            status="failed",
            plan_hash="sha256:" + "1" * 64,
            from_version="v1",
            to_version="v2",
            backup_ref=str(cfg.mnemos_dir / "backups" / "private-backup"),
            rollback_ref=str(cfg.mnemos_dir / "backups" / "private-recovery.json"),
            actor=private_value,
            error=private_value,
            verification={
                "execution_plan_hash": "sha256:" + "2" * 64,
                "expected_plan_hash": private_value,
                "reviewed_plan_hash": private_value,
                "prior_backend_error_text": private_value,
                "nested": {"caller_response": private_value},
            },
        )
    )

    status = MigrationRegistry().status(cfg, read_only=True)
    rendered = next(
        row
        for row in status["recent_ledger"]
        if row["migration_id"] == "database.model_call_ledger.v1"
    )

    assert private_value not in json.dumps(status, ensure_ascii=False)
    assert str(cfg.mnemos_dir) not in json.dumps(status, ensure_ascii=False)
    assert rendered["backup_ref"] == "protected_model_call_ledger_backup"
    assert rendered["rollback_ref"] == "sealed_or_manual_recovery_manifest"
    assert rendered["verification"]["reviewed_plan_hash_present"] is True
    assert rendered["verification"]["reviewed_plan_hash_matches"] is False
    assert "prior_backend_error_text" not in rendered["verification"]
    assert "nested" not in rendered["verification"]
    assert rendered["actor"] == "migration_operator"
    assert rendered["error"] == "model_call_ledger_record_error"


def test_migration_plan_reports_stale_config_keys(tmp_path):
    cfg = FakeConfig(
        tmp_path,
        {
            "memos": {"enabled": True, "token": "legacy-token"},
            "daemon": {"services": {"l1_sync": True}},
            "persona": {"data_sources": {"memos": {"enabled": True}}},
        },
    )
    plan = MigrationRegistry().plan(cfg)
    item = next(item for item in plan.items if item.migration_id == "config.stale_keys.v1")
    assert item.status == "planned"
    assert "memos" in item.stale_keys
    assert "daemon.services.l1_sync" in item.stale_keys
    assert "persona.data_sources.memos.enabled" in item.stale_keys
    assert plan.plan_hash


def test_migration_health_exposes_registry_counts(tmp_path):
    cfg = FakeConfig(tmp_path, {})
    health = build_migration_health(cfg)
    assert health["status"] == "ok"
    assert health["counts"]["registered"] >= 3
    assert health["ledger_path"] == "<MNEMOS_DIR>/migrations.db"


def _migrate_cli_args(command: str) -> SimpleNamespace:
    return SimpleNamespace(
        migrate_cmd=command,
        json=True,
        migration_id="",
        execute_wrapped=False,
        discard_unattributable_legacy=False,
        discard_unrecoverable_run_tombstone_history=False,
    )


def test_migrate_plan_status_and_verify_do_not_provision_runtime_state(tmp_path, monkeypatch, capsys):
    from core.cli.commands.migrate import cmd_migrate

    mnemos_dir = tmp_path / "empty-mnemos"
    monkeypatch.setenv("MNEMOS_DIR", str(mnemos_dir))

    for command in ("plan", "status", "verify"):
        assert cmd_migrate(_migrate_cli_args(command)) == 0
        assert json.loads(capsys.readouterr().out)
        assert not mnemos_dir.exists()


def test_migrate_diagnostics_inspect_legacy_config_without_rewriting_it(
    tmp_path, monkeypatch, capsys
):
    from core.cli.commands.migrate import cmd_migrate

    mnemos_dir = tmp_path / "legacy-mnemos"
    mnemos_dir.mkdir()
    legacy_config = mnemos_dir / "config.yaml"
    legacy_bytes = b"performance_tier: default\nmemos:\n  enabled: true\n"
    legacy_config.write_bytes(legacy_bytes)
    monkeypatch.setenv("MNEMOS_DIR", str(mnemos_dir))

    for command in ("plan", "status", "verify"):
        assert cmd_migrate(_migrate_cli_args(command)) == 0
        assert json.loads(capsys.readouterr().out)
        assert legacy_config.read_bytes() == legacy_bytes
        assert not (mnemos_dir / "configs" / "main.json").exists()
        assert not (mnemos_dir / "migrations.db").exists()
        assert not (mnemos_dir / "logs").exists()


def test_model_call_ledger_migration_uses_registry_ledger_and_verified_backup(tmp_path, monkeypatch):
    from core.telemetry.prompt_call_log import ModelCallLedger

    cfg = FakeConfig(tmp_path)
    # Exercise the full canonical-plus-retired-source reconciliation path.
    ModelCallLedger.for_config(cfg)
    source = cfg.database_dir / "wiki_state.db"
    with __import__("sqlite3").connect(str(source)) as conn:
        conn.execute(
            "CREATE TABLE prompt_calls (operation TEXT, provider TEXT, model TEXT, "
            "prompt_hash TEXT, input_tokens INTEGER, output_tokens INTEGER, created_at TEXT, session_id TEXT)"
        )
        conn.execute(
            "INSERT INTO prompt_calls VALUES ('distill', 'test', 'test-model', ?, 1, 1, ?, ?)",
            ("a" * 64, "2026-07-14T00:00:00+00:00", "registry-session"),
        )
    monkeypatch.setattr(runtime, "runtime_writers_are_inactive", lambda _directory: True)

    registry = MigrationRegistry()
    blocked = registry.apply(cfg, "database.model_call_ledger.v1")
    assert blocked.status == "blocked"

    reviewed_plan = registry.plan(cfg)
    item = next(
        item
        for item in reviewed_plan.items
        if item.migration_id == "database.model_call_ledger.v1"
    )
    assert item.execution_plan_hash

    record = registry.apply(
        cfg,
        "database.model_call_ledger.v1",
        execute_wrapped=True,
        expected_plan_hash=item.execution_plan_hash,
        discard_unattributable_legacy=True,
    )
    assert record.status == "applied"
    assert record.backup_ref
    assert Path(record.backup_ref).is_dir()
    assert record.verification["reconciliation"]["status"] == "applied"
    assert record.verification["reviewed_plan_hash"] == item.execution_plan_hash
    assert record.verification["recovery_manifest_sha256"]


def test_model_call_ledger_migration_records_failed_commit_with_backup_ref(
    tmp_path, monkeypatch
):
    """A final ledger write failure must retain the protected recovery root."""
    from core.telemetry.prompt_call_log import ModelCallLedger

    cfg = FakeConfig(tmp_path)
    ModelCallLedger.for_config(cfg)
    source = cfg.database_dir / "wiki_state.db"
    with sqlite3.connect(str(source)) as conn:
        conn.execute(
            "CREATE TABLE prompt_calls (operation TEXT, provider TEXT, model TEXT, "
            "prompt_hash TEXT, input_tokens INTEGER, output_tokens INTEGER, "
            "created_at TEXT, session_id TEXT)"
        )
        conn.execute(
            "INSERT INTO prompt_calls VALUES ('distill', 'test', 'test-model', ?, 1, 1, ?, ?)",
            ("a" * 64, "2026-07-14T00:00:00+00:00", "registry-session"),
        )
    monkeypatch.setattr(runtime, "runtime_writers_are_inactive", lambda _directory: True)

    original_record = MigrationLedger.record

    def fail_completed_record(self, record):
        if record.status == "applied":
            raise sqlite3.OperationalError("simulated final ledger failure")
        return original_record(self, record)

    monkeypatch.setattr(MigrationLedger, "record", fail_completed_record)
    registry = MigrationRegistry()
    item = next(
        item
        for item in registry.plan(cfg).items
        if item.migration_id == "database.model_call_ledger.v1"
    )

    record = registry.apply(
        cfg,
        "database.model_call_ledger.v1",
        execute_wrapped=True,
        expected_plan_hash=item.execution_plan_hash,
        discard_unattributable_legacy=True,
    )

    assert record.status == "failed"
    assert Path(record.backup_ref).is_dir()
    assert record.verification["unsafe_mutation"]["failure_stage"] == "migration_ledger_commit"
    assert record.rollback_ref
