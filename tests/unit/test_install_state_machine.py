import json
from pathlib import Path

from core.setup.install_lifecycle import (
    REQUIRED_INSTALL_STATUSES,
    InstallLifecycleManager,
    audit_install_upgrade_contract,
    build_install_lifecycle_health,
)
from core.system_contracts import ActionLedger, make_quality_gate_observation


class FakeConfig:
    def __init__(self, root: Path, *, prepared: bool = False):
        self.mnemos_dir = root / ".mnemos"
        self.data_dir = self.mnemos_dir
        self.database_dir = self.mnemos_dir / "databases"
        self.config_path = self.mnemos_dir / "configs" / "main.json"
        self.wiki_dir = root / "MnemosVault"
        self.obsidian_vault_path = root / "RawVault"
        self._data = {
            "vaults": {
                "mnemos": {"path": str(self.wiki_dir), "enabled": True},
                "raw": {"path": str(self.obsidian_vault_path), "enabled": True},
            }
        }
        if prepared:
            self.prepare()

    def prepare(self) -> None:
        self.database_dir.mkdir(parents=True, exist_ok=True)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(self._data), encoding="utf-8")
        self.wiki_dir.mkdir(parents=True, exist_ok=True)
        self.obsidian_vault_path.mkdir(parents=True, exist_ok=True)

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


def test_install_status_machine_contains_required_problem_48_states():
    errors = audit_install_upgrade_contract(strict=True)
    assert errors == []
    assert "uninstalled_preserve_data" in REQUIRED_INSTALL_STATUSES


def test_setup_plan_exposes_machine_readable_repair_actions(tmp_path):
    cfg = FakeConfig(tmp_path, prepared=False)
    state = InstallLifecycleManager(cfg).setup_plan()

    assert state.status == "configuring"
    assert state.intervention_count >= 1
    assert any(step.failure_reason for step in state.steps if step.status == "planned")
    assert "mnemos setup --yes" in state.repair_actions


def test_setup_plan_reports_required_model_endpoint_failures(tmp_path, monkeypatch):
    for name in (
        "MNEMOS_LLM_API_KEY",
        "MNEMOS_EMBEDDING_API_KEY",
        "MNEMOS_RERANKER_API_KEY",
        "SILICONFLOW_API_KEY",
        "DMXAPI_API_KEY",
        "DMX_API_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    cfg = FakeConfig(tmp_path, prepared=True)
    state = InstallLifecycleManager(cfg).setup_plan()

    model_step = next(step for step in state.steps if step.step_id == "required_model_endpoints")
    assert model_step.status == "blocked"
    assert model_step.failure_reason == "required_model_endpoints_failed"
    assert state.metadata["required_model_endpoints_failed"] is True
    assert set(state.metadata["required_model_endpoint_errors"]) == {
        "llm",
        "embedding",
        "reranker",
    }


def test_install_lifecycle_health_degrades_partial_required_steps(tmp_path, monkeypatch):
    for name in (
        "MNEMOS_LLM_API_KEY",
        "MNEMOS_EMBEDDING_API_KEY",
        "MNEMOS_RERANKER_API_KEY",
        "SILICONFLOW_API_KEY",
        "DMXAPI_API_KEY",
        "DMX_API_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    cfg = FakeConfig(tmp_path, prepared=True)

    health = build_install_lifecycle_health(cfg)

    assert health["status"] == "degraded"
    assert health["state"]["status"] == "installed_partial"
    assert "install_lifecycle_state: installed_partial" in health["errors"]
    assert any(
        step["step_id"] == "required_model_endpoints"
        for step in health["incomplete_required_steps"]
    )
    assert health["repair_actions"]


def test_install_lifecycle_health_accepts_persisted_ready_setup_state(tmp_path, monkeypatch):
    monkeypatch.setenv("SILICONFLOW_API_KEY", "test-key")
    cfg = FakeConfig(tmp_path, prepared=True)
    manager = InstallLifecycleManager(cfg)
    plan = manager.setup_plan()
    ready = manager._state(
        operation="setup",
        status="installed_ready",
        steps=manager._mark_runtime_steps_ok(plan.steps),
        metadata={"required_model_endpoints_failed": False},
    )
    manager._record_action(action_type="install_setup", target="local", state=ready)

    health = build_install_lifecycle_health(cfg)

    assert health["status"] == "ok"
    assert health["state"]["status"] == "installed_ready"
    assert health["incomplete_required_steps"] == []
    assert health["repair_actions"] == []
    assert health["errors"] == []
    scheduler = next(
        step for step in health["state"]["steps"] if step["step_id"] == "scheduler"
    )
    assert scheduler["status"] == "skipped"


def test_install_lifecycle_health_rechecks_current_blocked_required_steps(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SILICONFLOW_API_KEY", "test-key")
    cfg = FakeConfig(tmp_path, prepared=True)
    manager = InstallLifecycleManager(cfg)
    plan = manager.setup_plan()
    ready = manager._state(
        operation="setup",
        status="installed_ready",
        steps=manager._mark_runtime_steps_ok(plan.steps),
        metadata={"required_model_endpoints_failed": False},
    )
    manager._record_action(action_type="install_setup", target="local", state=ready)
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)

    health = build_install_lifecycle_health(cfg)

    assert health["status"] == "degraded"
    assert health["state"]["status"] == "installed_partial"
    assert any(
        step["step_id"] == "required_model_endpoints"
        for step in health["incomplete_required_steps"]
    )


def test_install_lifecycle_health_finds_ready_setup_beyond_recent_noise(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SILICONFLOW_API_KEY", "test-key")
    cfg = FakeConfig(tmp_path, prepared=True)
    manager = InstallLifecycleManager(cfg)
    plan = manager.setup_plan()
    ready = manager._state(
        operation="setup",
        status="installed_ready",
        steps=manager._mark_runtime_steps_ok(plan.steps),
        metadata={"required_model_endpoints_failed": False},
    )
    manager._record_action(action_type="install_setup", target="local", state=ready)

    ledger = ActionLedger(cfg.database_dir / "action_ledger.db")
    for index in range(60):
        ledger.record_observation(
            make_quality_gate_observation(
                actor="test",
                target=f"claim:{index}",
                evidence_refs=("core/system_contracts.py",),
                details={"index": index},
            )
        )

    health = build_install_lifecycle_health(cfg)

    assert health["status"] == "ok"
    assert health["state"]["metadata"]["persisted_state_source"] == "action_ledger"


def test_upgrade_plan_requires_migration_plan_and_backup_preflight(tmp_path):
    cfg = FakeConfig(tmp_path, prepared=True)
    state = InstallLifecycleManager(cfg).upgrade_plan()

    assert state.status in {"upgrade_available", "installed_ready"}
    assert state.migration_plan_hash
    assert state.backup_ref.startswith("snap-")
    assert state.metadata["preserve_existing"] is True
    assert state.metadata["migration_plan"]["plan_hash"] == state.migration_plan_hash


def test_uninstall_preserves_data_by_default(tmp_path):
    cfg = FakeConfig(tmp_path, prepared=True)
    state = InstallLifecycleManager(cfg).uninstall(dry_run=True)

    assert state.status == "uninstalled_preserve_data"
    assert state.data_policy == "preserve_user_data"
    assert not state.errors


def test_purge_data_is_blocked_by_data_ownership_contract(tmp_path):
    cfg = FakeConfig(tmp_path, prepared=True)
    state = InstallLifecycleManager(cfg).uninstall(
        preserve_data=False,
        purge_data=True,
        confirm=True,
        dry_run=True,
    )

    assert state.status == "uninstall_blocked"
    assert "data_delete_plan" in state.metadata
    assert state.metadata["data_delete_plan"]["requires_confirmation"] is True
    assert state.errors == ("purge_data_requires_freeze_snapshot_and_data_delete_apply",)
