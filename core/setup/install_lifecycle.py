# -*- coding: utf-8 -*-
"""Product-level setup, upgrade, and uninstall lifecycle for Mnemos."""

from __future__ import annotations

import json
import importlib
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from core.cognitive.state_contract import sha256_json
from core.ops.action_ledger import authorize_primary_action_ledger_record

INSTALL_LIFECYCLE_SCHEMA_VERSION = "mnemos.install_lifecycle.v1"
_INSTALL_LEDGER_CONTRACT = (
    "Append an install lifecycle result only when its operation, canonical "
    "state snapshot, target, and rollback evidence satisfy the lifecycle contract."
)
_INSTALL_LEDGER_PRODUCER_HASH = sha256_json(
    {
        "module": "core.setup.install_lifecycle",
        "contract": "mnemos.install_lifecycle_action_ledger.v1",
    }
)
_INSTALL_ACTION_OPERATIONS = {
    "install_setup": "setup",
    "install_upgrade": "upgrade_apply",
    "install_uninstall": "uninstall",
    "install_repair_all": "repair_all",
}

INSTALL_STATUSES = {
    "not_installed",
    "configuring",
    "installed_partial",
    "installed_ready",
    "upgrade_available",
    "upgrading",
    "upgrade_failed",
    "rollback_available",
    "uninstalled_preserve_data",
    "uninstall_blocked",
    "uninstalled_purged",
}

STEP_STATUSES = {"ok", "planned", "skipped", "blocked", "failed"}

REQUIRED_INSTALL_STATUSES = {
    "not_installed",
    "configuring",
    "installed_partial",
    "installed_ready",
    "upgrade_available",
    "upgrading",
    "upgrade_failed",
    "rollback_available",
    "uninstalled_preserve_data",
}

RECOMMENDED_ENTRYPOINTS = (
    "mnemos setup",
    "mnemos upgrade plan",
    "mnemos uninstall --preserve-data",
    "mnemos doctor repair-all",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(value: Any) -> Path | None:
    if value in (None, ""):
        return None
    try:
        return Path(value)
    except TypeError:
        return None


def _config_path(config: Any) -> Path:
    return _path(getattr(config, "config_path", None)) or (
        _mnemos_dir(config) / "configs" / "main.json"
    )


def _mnemos_dir(config: Any) -> Path:
    return (
        _path(getattr(config, "mnemos_dir", None))
        or _path(getattr(config, "data_dir", None))
        or (Path.home() / ".mnemos")
    )


def _database_dir(config: Any) -> Path:
    return _path(getattr(config, "database_dir", None)) or _mnemos_dir(config)


def _vault_dir(config: Any, name: str) -> Path | None:
    method = getattr(config, "vault_dir", None)
    if callable(method):
        try:
            return Path(method(name))
        except (KeyError, TypeError, ValueError):
            return None
    if name == "mnemos":
        return _path(getattr(config, "wiki_dir", None))
    if name == "raw":
        return _path(getattr(config, "obsidian_vault_path", None))
    return None


def _status_for_exists(path: Path | None, *, repair_action: str) -> tuple[str, str, str]:
    if path is None:
        return "blocked", "path_not_configured", repair_action
    if path.exists():
        return "ok", "", ""
    return "planned", "path_missing", repair_action


def _step_from_mapping(raw: Mapping[str, Any]) -> InstallStep:
    evidence_refs_raw = raw.get("evidence_refs", ())
    evidence_refs: tuple[str, ...]
    if isinstance(evidence_refs_raw, str):
        evidence_refs = (evidence_refs_raw,)
    else:
        try:
            evidence_refs = tuple(str(item) for item in evidence_refs_raw)
        except TypeError:
            evidence_refs = ()
    return InstallStep(
        step_id=str(raw.get("step_id", "") or ""),
        title=str(raw.get("title", "") or ""),
        status=str(raw.get("status", "") or ""),
        command=str(raw.get("command", "") or ""),
        repair_action=str(raw.get("repair_action", "") or ""),
        failure_reason=str(raw.get("failure_reason", "") or ""),
        required=bool(raw.get("required", True)),
        evidence_refs=evidence_refs,
    )


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, str):
        return (value,)
    try:
        return tuple(str(item) for item in value)
    except TypeError:
        return ()


def _state_from_mapping(
    raw: Mapping[str, Any],
    *,
    default_state_path: str,
    default_action_ledger_ref: str,
) -> InstallLifecycleState | None:
    raw_steps = raw.get("steps", ())
    if not isinstance(raw_steps, Sequence) or isinstance(raw_steps, (str, bytes)):
        return None
    try:
        steps = tuple(_step_from_mapping(step) for step in raw_steps if isinstance(step, Mapping))
        state = InstallLifecycleState(
            operation=str(raw.get("operation", "") or ""),
            status=str(raw.get("status", "") or ""),
            generated_at=str(raw.get("generated_at", "") or ""),
            steps=steps,
            state_path=str(raw.get("state_path", "") or default_state_path),
            action_ledger_ref=str(raw.get("action_ledger_ref", "") or default_action_ledger_ref),
            migration_plan_hash=str(raw.get("migration_plan_hash", "") or ""),
            backup_ref=str(raw.get("backup_ref", "") or ""),
            data_policy=str(raw.get("data_policy", "") or "preserve_user_data"),
            intervention_count=int(raw.get("intervention_count", 0) or 0),
            repair_actions=_string_tuple(raw.get("repair_actions", ())),
            errors=_string_tuple(raw.get("errors", ())),
            metadata=dict(raw.get("metadata", {}) or {}),
            schema_version=str(raw.get("schema_version", "") or INSTALL_LIFECYCLE_SCHEMA_VERSION),
        )
    except (TypeError, ValueError):
        return None
    if state.validate():
        return None
    return state


@dataclass(frozen=True)
class InstallStep:
    step_id: str
    title: str
    status: str
    command: str
    repair_action: str
    failure_reason: str = ""
    required: bool = True
    evidence_refs: tuple[str, ...] = ()

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.step_id:
            errors.append("step_id is required")
        if self.status not in STEP_STATUSES:
            errors.append(f"{self.step_id}: unknown step status {self.status}")
        if not self.command:
            errors.append(f"{self.step_id}: command is required")
        if self.status in {"blocked", "failed"} and not self.failure_reason:
            errors.append(f"{self.step_id}: failure_reason required")
        if self.status in {"planned", "blocked", "failed"} and not self.repair_action:
            errors.append(f"{self.step_id}: repair_action required")
        return errors

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class InstallLifecycleState:
    operation: str
    status: str
    generated_at: str
    steps: tuple[InstallStep, ...]
    state_path: str
    action_ledger_ref: str
    migration_plan_hash: str = ""
    backup_ref: str = ""
    data_policy: str = "preserve_user_data"
    intervention_count: int = 0
    repair_actions: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = INSTALL_LIFECYCLE_SCHEMA_VERSION

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.status not in INSTALL_STATUSES:
            errors.append(f"unknown install status: {self.status}")
        if not self.operation:
            errors.append("operation is required")
        if not self.state_path:
            errors.append("state_path is required")
        if not self.action_ledger_ref:
            errors.append("action_ledger_ref is required")
        for step in self.steps:
            errors.extend(step.validate())
        if self.status in {"upgrade_failed", "uninstall_blocked"} and not self.errors:
            errors.append(f"{self.status} requires errors")
        return errors

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class InstallLifecycleManager:
    """Single product journey for setup, upgrade, uninstall, and repair."""

    def __init__(self, config: Any):
        self.config = config
        self.state_path = _database_dir(config) / "install_state.json"
        self.action_ledger_path = _database_dir(config) / "action_ledger.db"

    def _state(
        self,
        *,
        operation: str,
        status: str,
        steps: Sequence[InstallStep],
        migration_plan_hash: str = "",
        backup_ref: str = "",
        data_policy: str = "preserve_user_data",
        errors: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> InstallLifecycleState:
        repair_actions_list: list[str] = []
        seen_actions: set[str] = set()
        for step in steps:
            if not step.repair_action or step.status not in {"planned", "blocked", "failed"}:
                continue
            if step.repair_action in seen_actions:
                continue
            repair_actions_list.append(step.repair_action)
            seen_actions.add(step.repair_action)
        repair_actions = tuple(repair_actions_list)
        intervention_count = len(repair_actions)
        return InstallLifecycleState(
            operation=operation,
            status=status,
            generated_at=_now_iso(),
            steps=tuple(steps),
            state_path=str(self.state_path),
            action_ledger_ref=str(self.action_ledger_path),
            migration_plan_hash=migration_plan_hash,
            backup_ref=backup_ref,
            data_policy=data_policy,
            intervention_count=intervention_count,
            repair_actions=repair_actions,
            errors=tuple(errors),
            metadata=dict(metadata or {}),
        )

    def _write_state(self, state: InstallLifecycleState) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(state.as_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _read_ready_setup_state_from_ledger(self) -> InstallLifecycleState | None:
        if not self.action_ledger_path.exists():
            return None
        try:
            with sqlite3.connect(str(self.action_ledger_path), timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT verification_json
                    FROM action_ledger
                    WHERE action_type = ? AND status = ?
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    ("install_setup", "verified"),
                ).fetchall()
        except (OSError, sqlite3.Error):
            return None
        for row in rows:
            try:
                verification = json.loads(str(row["verification_json"] or "{}"))
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(verification, Mapping):
                continue
            state = _state_from_mapping(
                verification,
                default_state_path=str(self.state_path),
                default_action_ledger_ref=str(self.action_ledger_path),
            )
            if state is None:
                continue
            if state.operation == "setup" and state.status == "installed_ready":
                return state
        return None

    def _record_action(
        self,
        *,
        action_type: str,
        target: str,
        state: InstallLifecycleState,
        status: str = "verified",
        evidence_refs: Iterable[str] = (),
        rollback_ref: str = "",
    ) -> str:
        system_contracts = importlib.import_module("core.system_contracts")

        refs = tuple(evidence_refs) or (
            "core/setup/install_lifecycle.py",
            state.state_path,
        )
        record = system_contracts.make_action_record(
            actor="mnemos_install_lifecycle",
            action_type=action_type,
            target=target,
            evidence_refs=refs,
            status=status,
            verification=state.as_dict(),
            rollback_ref=rollback_ref,
        )
        expected_operation = _INSTALL_ACTION_OPERATIONS.get(action_type, "")
        material_action = authorize_primary_action_ledger_record(
            record,
            state_db_path=_database_dir(self.config)
            / "producer_consumer_ledger.db",
            contract_id="project-contract:install-lifecycle-action-ledger",
            contract_revision_id="mnemos.install_lifecycle_action_ledger.v1",
            contract_text=_INSTALL_LEDGER_CONTRACT,
            source_namespace="install-lifecycle-action-ledger",
            source_facts={
                "operation": state.operation,
                "state_status": state.status,
                "state_hash": sha256_json(state.as_dict()),
                "action_status": status,
                "backup_ref": state.backup_ref,
                "error_count": len(state.errors),
            },
            decision_checks={
                "action_is_registered": bool(expected_operation),
                "operation_matches_action": state.operation == expected_operation,
                "target_is_local_install": target == "local",
                "upgrade_has_rollback": (
                    action_type != "install_upgrade" or bool(rollback_ref)
                ),
            },
            evidence_refs=tuple(str(value) for value in refs),
            task=f"Append {action_type} lifecycle result to ActionLedger",
            goal="Preserve the exact finalized install lifecycle state.",
            constraints=(
                "Do not relabel one lifecycle operation as another.",
                "The authorization governs only this ActionLedger append.",
            ),
            producer="install-lifecycle-manager",
            producer_version="mnemos.install_lifecycle_action_ledger.v1",
            producer_code_hash=_INSTALL_LEDGER_PRODUCER_HASH,
            evaluator_id="install-lifecycle-action-ledger-evaluator",
            approved_candidate_key="append_bound_install_lifecycle_result",
            approved_candidate_summary=(
                "Append the exact lifecycle result bound to its canonical state."
            ),
            rejected_candidate_key="omit_mismatched_install_lifecycle_result",
            rejected_candidate_summary=(
                "Do not append a result with a mismatched operation or rollback."
            ),
            approved_reason_code="install_lifecycle_binding_verified",
            rejected_reason_code="install_lifecycle_binding_rejected",
            committed_metric="install_lifecycle_action_ledger_receipt",
            rejected_metric="mismatched_install_lifecycle_ledger_count",
        )
        from core.ops.runtime_flow_telemetry import (
            record_runtime_produced,
            runtime_item_id,
        )

        lifecycle_flow_item_id = runtime_item_id(
            "install-lifecycle", action_type, target, state.generated_at
        )
        record_runtime_produced(
            "install_lifecycle_to_scorecard",
            source="core/setup/install_lifecycle.py",
            item_id=lifecycle_flow_item_id,
            intended_consumers=["core/system_contracts.py:ActionLedger"],
            metadata={"transition": "install_lifecycle_state_finalized", "status": status},
            config_or_path=_database_dir(self.config),
        )
        ledger_id = system_contracts.ActionLedger.from_config(
            self.config,
            initialize=True,
        ).record(record, material_action=material_action)
        from core.ops.runtime_flow_telemetry import record_runtime_consumed

        record_runtime_consumed(
            "install_lifecycle_to_scorecard",
            source="core/system_contracts.py:ActionLedger",
            item_id=lifecycle_flow_item_id,
            metadata={"transition": "install_action_ledger_recorded"},
            config_or_path=_database_dir(self.config),
        )
        return str(ledger_id)

    def _setup_steps(self) -> tuple[InstallStep, ...]:
        steps, _metadata = self._setup_contract()
        return steps

    def _required_model_endpoint_step(self) -> tuple[InstallStep, dict[str, Any]]:
        metadata: dict[str, Any] = {
            "required_model_endpoints_failed": False,
            "required_model_endpoint_errors": {},
        }
        try:
            from scripts.auto_setup import (
                _MODEL_ENDPOINT_SPECS,
                _model_cfg_ready,
                _resolve_required_model_configs,
            )

            if callable(getattr(self.config, "to_dict", None)):
                data = self.config.to_dict()
            else:
                data = getattr(self.config, "_data", {})
            configs = _resolve_required_model_configs(data)
            errors: dict[str, str] = {}
            for kind in ("llm", "embedding", "reranker"):
                cfg = configs[kind]
                if not _model_cfg_ready(cfg):
                    errors[kind] = "missing model id, API URL, or API key"
            if errors:
                metadata.update(
                    {
                        "required_model_endpoints_failed": True,
                        "required_model_endpoint_errors": errors,
                    }
                )
                labels = ", ".join(_MODEL_ENDPOINT_SPECS[kind]["label"] for kind in errors)
                return (
                    InstallStep(
                        "required_model_endpoints",
                        "Required model endpoints",
                        "blocked",
                        "mnemos setup --yes --max-smoke-attempts 3",
                        "Configure LLM, Embedding, and Reranker endpoints, then rerun setup",
                        "required_model_endpoints_failed",
                        evidence_refs=("scripts/auto_setup.py", "scripts/verify_installation.py"),
                    ),
                    {**metadata, "required_model_endpoint_labels": labels},
                )
            return (
                InstallStep(
                    "required_model_endpoints",
                    "Required model endpoints",
                    "planned",
                    "python3 scripts/verify_installation.py --json",
                    "Run setup or verify_installation to smoke test model endpoints",
                    "required_model_endpoint_smoke_not_run_in_plan",
                    evidence_refs=("scripts/auto_setup.py", "scripts/verify_installation.py"),
                ),
                metadata,
            )
        except (ImportError, KeyError, TypeError, AttributeError, ValueError) as exc:
            metadata.update({"required_model_endpoint_probe_error": str(exc)})
            return (
                InstallStep(
                    "required_model_endpoints",
                    "Required model endpoints",
                    "planned",
                    "python3 scripts/verify_installation.py --json",
                    "Run setup or verify_installation to smoke test model endpoints",
                    "required_model_endpoint_probe_unavailable",
                    evidence_refs=("scripts/auto_setup.py", "scripts/verify_installation.py"),
                ),
                metadata,
            )

    def _setup_contract(self) -> tuple[tuple[InstallStep, ...], dict[str, Any]]:
        config_path = _config_path(self.config)
        mnemos_vault = _vault_dir(self.config, "mnemos")
        raw_vault = _vault_dir(self.config, "raw")
        model_step, metadata = self._required_model_endpoint_step()
        config_status, config_reason, config_repair = _status_for_exists(
            config_path,
            repair_action="mnemos setup --yes",
        )
        mnemos_status, mnemos_reason, mnemos_repair = _status_for_exists(
            mnemos_vault,
            repair_action="mnemos setup --yes",
        )
        raw_status, raw_reason, raw_repair = _status_for_exists(
            raw_vault,
            repair_action="mnemos setup --yes",
        )
        vault_status = "ok" if mnemos_status == raw_status == "ok" else "planned"
        vault_reason = ";".join(reason for reason in (mnemos_reason, raw_reason) if reason)
        if "blocked" in {mnemos_status, raw_status}:
            vault_status = "blocked"
        steps = (
            InstallStep(
                "python_runtime",
                "Python runtime",
                "ok" if sys.version_info >= (3, 10) else "blocked",
                "python3 --version",
                "Install Python >= 3.10",
                "" if sys.version_info >= (3, 10) else "python_version_too_old",
                evidence_refs=("pyproject.toml",),
            ),
            InstallStep(
                "config_home",
                "Configuration home",
                config_status,
                "mnemos setup --dry-run --json",
                config_repair,
                config_reason,
                evidence_refs=(str(config_path),),
            ),
            model_step,
            InstallStep(
                "vault_layout",
                "Mnemos and raw vault layout",
                vault_status,
                "mnemos setup --dry-run --json",
                mnemos_repair or raw_repair,
                vault_reason,
                evidence_refs=tuple(
                    str(path) for path in (mnemos_vault, raw_vault) if path is not None
                ),
            ),
            InstallStep(
                "capability_discovery",
                "Capability discovery",
                "planned",
                "mnemos doctor modules --json",
                "mnemos doctor repair-all --json",
                "capability_probe_not_run_in_plan",
                evidence_refs=("core/module_toggles.py",),
            ),
            InstallStep(
                "agent_policy",
                "Agent policy installation",
                "planned",
                "mnemos agent install",
                "mnemos doctor repair-all --json",
                "agent_targets_require_runtime_probe",
                evidence_refs=("core/agent_kit",),
            ),
            InstallStep(
                "scheduler",
                "Scheduler registration",
                "planned",
                "mnemos scheduler status",
                "Use mnemos setup --yes or the platform-specific scheduler install command",
                "scheduler_requires_platform_probe",
                required=False,
                evidence_refs=("core/cli/commands/scheduler.py",),
            ),
            InstallStep(
                "verify_installation",
                "Deployment verification",
                "planned",
                "python3 scripts/verify_installation.py --json",
                "mnemos doctor repair-all --json",
                "verification_not_run_in_plan",
                evidence_refs=("scripts/verify_installation.py",),
            ),
            InstallStep(
                "health",
                "Machine-readable health",
                "planned",
                "mnemos health --json",
                "mnemos doctor repair-all --json",
                "health_not_run_in_plan",
                evidence_refs=("core/ops/health_check.py",),
            ),
        )
        return steps, metadata

    def setup_plan(self) -> InstallLifecycleState:
        steps, metadata = self._setup_contract()
        blocked = any(step.status == "blocked" and step.required for step in steps)
        required_planned = any(step.status == "planned" and step.required for step in steps)
        existing_ready = all(
            step.status == "ok"
            for step in steps
            if step.required and step.step_id in {"python_runtime", "config_home", "vault_layout"}
        )
        if not existing_ready:
            status = "configuring"
        elif blocked or required_planned:
            status = "installed_partial"
        else:
            status = "installed_ready"
        return self._state(operation="setup_plan", status=status, steps=steps, metadata=metadata)

    def health_state(self) -> InstallLifecycleState:
        """Return current install health using real setup completion evidence.

        The setup contract intentionally contains runtime probes as planned
        steps before setup runs. Health must not treat those static planned
        steps as current failures once a successful setup action has verified
        successfully in the ActionLedger, but it still re-checks current
        blocking prerequisites such as config, vaults, and required model
        endpoint configuration.
        """

        plan = self.setup_plan()
        persisted = self._read_ready_setup_state_from_ledger()
        if persisted is None or persisted.status != "installed_ready":
            return plan

        persisted_steps = {step.step_id: step for step in persisted.steps}
        merged_steps: list[InstallStep] = []
        for step in plan.steps:
            persisted_step = persisted_steps.get(step.step_id)
            if (
                step.required
                and step.status == "planned"
                and persisted_step is not None
                and persisted_step.required
                and persisted_step.status == "ok"
            ):
                merged_steps.append(
                    InstallStep(
                        step.step_id,
                        step.title,
                        "ok",
                        step.command,
                        "",
                        "",
                        step.required,
                        step.evidence_refs,
                    )
                )
                continue
            if (
                not step.required
                and step.status in {"planned", "blocked", "failed"}
                and persisted_step is not None
                and not persisted_step.required
                and persisted_step.status in {"ok", "skipped"}
            ):
                merged_steps.append(
                    InstallStep(
                        step.step_id,
                        step.title,
                        "skipped",
                        step.command,
                        "",
                        "",
                        step.required,
                        step.evidence_refs,
                    )
                )
                continue
            merged_steps.append(step)

        incomplete_required = any(
            step.required and step.status in {"planned", "blocked", "failed"}
            for step in merged_steps
        )
        metadata = dict(plan.metadata)
        metadata.update(
            {
                "persisted_state_status": persisted.status,
                "persisted_state_generated_at": persisted.generated_at,
                "persisted_state_operation": persisted.operation,
                "persisted_state_source": "action_ledger",
            }
        )
        return self._state(
            operation="setup_health",
            status="installed_partial" if incomplete_required else "installed_ready",
            steps=merged_steps,
            metadata=metadata,
        )

    def run_setup(
        self, *, dry_run: bool, auto_setup_args: Any | None = None
    ) -> InstallLifecycleState:
        if dry_run:
            return self.setup_plan()

        steps, metadata = self._setup_contract()
        configuring = self._state(
            operation="setup",
            status="configuring",
            steps=steps,
            metadata=metadata,
        )
        self._write_state(configuring)
        try:
            from scripts.auto_setup import _run_setup

            if auto_setup_args is not None:
                _run_setup(auto_setup_args)
            final_metadata = dict(metadata)
            final_metadata.update(
                {
                    "required_model_endpoints_failed": False,
                    "required_model_endpoint_errors": {},
                }
            )
            final = self._state(
                operation="setup",
                status="installed_ready",
                steps=self._mark_runtime_steps_ok(steps),
                metadata=final_metadata,
            )
            self._write_state(final)
            self._record_action(action_type="install_setup", target="local", state=final)
            return final
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
        ) as exc:  # pragma: no cover - defensive CLI wrapper
            failed_metadata = dict(metadata)
            failed_metadata.update(self._required_model_exception_metadata(exc))
            failed_steps = steps
            if failed_metadata.get("required_model_endpoints_failed"):
                failed_steps = self._mark_required_model_step_failed(
                    failed_steps,
                    failure_reason="required_model_endpoints_failed",
                )
            failed = self._state(
                operation="setup",
                status="installed_partial",
                steps=failed_steps,
                errors=(str(exc),),
                metadata=failed_metadata,
            )
            self._write_state(failed)
            self._record_action(
                action_type="install_setup",
                target="local",
                state=failed,
                status="failed_recoverable",
            )
            return failed

    @staticmethod
    def _required_model_exception_metadata(exc: Exception) -> dict[str, Any]:
        if getattr(exc, "failure_code", "") != "required_model_endpoints_failed":
            return {}
        to_metadata = getattr(exc, "to_metadata", None)
        if callable(to_metadata):
            return dict(to_metadata())
        return {"required_model_endpoints_failed": True}

    @staticmethod
    def _mark_required_model_step_failed(
        steps: Sequence[InstallStep], *, failure_reason: str
    ) -> tuple[InstallStep, ...]:
        marked: list[InstallStep] = []
        for step in steps:
            if step.step_id != "required_model_endpoints":
                marked.append(step)
                continue
            marked.append(
                InstallStep(
                    step.step_id,
                    step.title,
                    "failed",
                    step.command,
                    "Configure LLM, Embedding, and Reranker endpoints, then rerun setup",
                    failure_reason,
                    step.required,
                    step.evidence_refs,
                )
            )
        return tuple(marked)

    @staticmethod
    def _mark_runtime_steps_ok(steps: Sequence[InstallStep]) -> tuple[InstallStep, ...]:
        marked: list[InstallStep] = []
        for step in steps:
            status = "ok" if step.required else step.status
            if not step.required and status in {"planned", "blocked", "failed"}:
                status = "skipped"
            marked.append(
                InstallStep(
                    step.step_id,
                    step.title,
                    status,
                    step.command,
                    "",
                    "",
                    step.required,
                    step.evidence_refs,
                )
            )
        return tuple(marked)

    def upgrade_plan(self) -> InstallLifecycleState:
        from core.backup.snapshot_manager import MnemosSnapshotManager
        from core.migrations.registry import MigrationRegistry

        migration_plan = MigrationRegistry().plan(self.config)
        backup = MnemosSnapshotManager(self.config).create(
            reason="upgrade_preflight",
            trigger_action="upgrade.plan",
            dry_run=True,
        )
        planned = [item for item in migration_plan.items if item.status == "planned"]
        status = "upgrade_available" if planned else "installed_ready"
        steps = (
            InstallStep(
                "migration_plan",
                "Migration plan",
                "ok",
                "mnemos migrate plan --json",
                "",
                "",
                evidence_refs=("core/migrations/registry.py",),
            ),
            InstallStep(
                "backup_preflight",
                "Backup preflight",
                "planned",
                "mnemos backup create --dry-run --json --trigger-action upgrade.plan",
                "mnemos backup create --reason upgrade --trigger-action upgrade.apply --json",
                "backup_create_not_run_in_plan",
                evidence_refs=("core/backup/snapshot_manager.py",),
            ),
            InstallStep(
                "upgrade_apply",
                "Upgrade apply",
                "planned" if planned else "skipped",
                "mnemos upgrade apply --json",
                "Review blocked migrations, then rerun with --execute-wrapped if intended",
                "migration_apply_not_run_in_plan" if planned else "",
                evidence_refs=("core/migrations/registry.py",),
            ),
            InstallStep(
                "post_upgrade_verify",
                "Post-upgrade verification",
                "planned",
                "python3 scripts/verify_installation.py --json",
                "mnemos doctor repair-all --json",
                "verification_not_run_in_plan",
                evidence_refs=("scripts/verify_installation.py",),
            ),
        )
        backup_summary = {
            "snapshot_id": backup.snapshot_id,
            "created_at": backup.created_at,
            "reason": backup.reason,
            "trigger_action": backup.trigger_action,
            "scopes": list(backup.scopes),
            "file_entry_count": len(backup.file_entries),
            "database_entry_count": len(backup.database_entries),
            "dry_run": backup.dry_run,
            "schema_version": backup.schema_version,
        }
        return self._state(
            operation="upgrade_plan",
            status=status,
            steps=steps,
            migration_plan_hash=migration_plan.plan_hash,
            backup_ref=backup.snapshot_id,
            metadata={
                "migration_plan": migration_plan.as_dict(),
                "backup_preflight": backup_summary,
                "preserve_existing": True,
            },
        )

    def upgrade_apply(self, *, execute_wrapped: bool = False) -> InstallLifecycleState:
        from core.backup.snapshot_manager import MnemosSnapshotManager
        from core.migrations.registry import MigrationRegistry

        registry = MigrationRegistry()
        plan = registry.plan(self.config)
        snapshot = MnemosSnapshotManager(self.config).create(
            reason="upgrade",
            trigger_action="upgrade.apply",
            dry_run=False,
        )
        results: list[dict[str, Any]] = []
        errors: list[str] = []
        for item in plan.items:
            if item.status == "verified":
                continue
            if item.wrapper_command and not execute_wrapped:
                reason = f"{item.migration_id} requires --execute-wrapped"
                results.append(
                    {"migration_id": item.migration_id, "status": "blocked", "error": reason}
                )
                errors.append(reason)
                continue
            record = registry.apply(
                self.config,
                item.migration_id,
                execute_wrapped=execute_wrapped,
            )
            results.append(record.as_dict())
            if record.status in {"failed", "blocked"}:
                errors.append(f"{record.migration_id}: {record.error or record.status}")
        status = "installed_ready" if not errors else "rollback_available"
        action_status = "verified" if not errors else "needs_user"
        steps = (
            InstallStep(
                "backup_create",
                "Backup create",
                "ok",
                "mnemos backup create --reason upgrade --trigger-action upgrade.apply --json",
                "",
                "",
                evidence_refs=(snapshot.snapshot_id,),
            ),
            InstallStep(
                "migration_apply",
                "Migration apply",
                "ok" if not errors else "blocked",
                "mnemos migrate apply <migration_id> --json",
                "Review migration ledger and rerun with --execute-wrapped if intended",
                "; ".join(errors),
                evidence_refs=("core/migrations/registry.py",),
            ),
            InstallStep(
                "post_upgrade_verify",
                "Post-upgrade verification",
                "planned",
                "python3 scripts/verify_installation.py --json",
                "mnemos doctor repair-all --json",
                "verification_not_run_after_apply",
                evidence_refs=("scripts/verify_installation.py",),
            ),
        )
        state = self._state(
            operation="upgrade_apply",
            status=status,
            steps=steps,
            migration_plan_hash=plan.plan_hash,
            backup_ref=snapshot.snapshot_id,
            errors=errors,
            metadata={"migration_results": results, "snapshot": snapshot.as_dict()},
        )
        self._write_state(state)
        self._record_action(
            action_type="install_upgrade",
            target="local",
            state=state,
            status=action_status,
            rollback_ref=snapshot.snapshot_id,
        )
        return state

    def uninstall(
        self,
        *,
        preserve_data: bool = True,
        purge_data: bool = False,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> InstallLifecycleState:
        from core.privacy.data_ownership import DataOwnershipManager

        steps: tuple[InstallStep, ...]
        if purge_data:
            delete_plan = DataOwnershipManager(self.config).delete("all", dry_run=True, apply=False)
            error = ""
            status = "uninstalled_purged"
            step_status = "planned" if dry_run else "blocked"
            if not confirm:
                status = "uninstall_blocked"
                error = "purge_data_requires_confirm"
            else:
                status = "uninstall_blocked"
                error = "purge_data_requires_freeze_snapshot_and_data_delete_apply"
            steps = (
                InstallStep(
                    "data_delete_contract",
                    "Data ownership delete contract",
                    step_status,
                    "mnemos data delete --scope all --dry-run --json",
                    (
                        "mnemos data freeze --scope all --json && "
                        "mnemos backup create --reason uninstall --json && "
                        "mnemos data delete --scope all --apply --confirm --snapshot-ref <snapshot>"
                    ),
                    error,
                    evidence_refs=("core/privacy/data_ownership.py",),
                ),
            )
            state = self._state(
                operation="uninstall",
                status=status,
                steps=steps,
                data_policy="purge_requires_data_ownership_contract",
                errors=(error,) if error else (),
                metadata={"data_delete_plan": delete_plan.as_dict()},
            )
        else:
            steps = (
                InstallStep(
                    "preserve_user_data",
                    "Preserve user data",
                    "ok",
                    "mnemos uninstall --preserve-data --json",
                    "",
                    "",
                    evidence_refs=("core/privacy/data_ownership.py",),
                ),
                InstallStep(
                    "runtime_disable",
                    "Disable runtime hooks manually if needed",
                    "planned",
                    "mnemos scheduler uninstall-windows",
                    "Use platform scheduler uninstall or remove launchd/cron entry",
                    "runtime_disable_requires_platform_context",
                    required=False,
                    evidence_refs=("core/cli/commands/scheduler.py",),
                ),
            )
            state = self._state(
                operation="uninstall",
                status="uninstalled_preserve_data",
                steps=steps,
                data_policy="preserve_user_data",
            )
        if not dry_run:
            self._write_state(state)
            self._record_action(
                action_type="install_uninstall",
                target="local",
                state=state,
                status="verified" if not state.errors else "needs_user",
            )
        return state

    def repair_all(self, *, dry_run: bool = False) -> InstallLifecycleState:
        plan = self.setup_plan()
        expanded_steps: list[InstallStep] = []
        for step in plan.steps:
            repair_action = step.repair_action
            if repair_action == "mnemos doctor repair-all --json":
                repair_action = "mnemos setup --yes"
            expanded_steps.append(
                InstallStep(
                    step.step_id,
                    step.title,
                    step.status,
                    step.command,
                    repair_action,
                    step.failure_reason,
                    step.required,
                    step.evidence_refs,
                )
            )
        recommended_actions = tuple(
            dict.fromkeys(
                step.repair_action
                for step in expanded_steps
                if step.repair_action and step.status in {"planned", "blocked", "failed"}
            )
        )
        steps = (
            InstallStep(
                "doctor_repair_all",
                "Repair all install surfaces",
                "ok",
                "mnemos doctor repair-all --json",
                "",
                "",
                evidence_refs=("mnemos_cli.py", "core/cli/commands/doctor.py"),
            ),
            *expanded_steps,
        )
        state = self._state(
            operation="repair_all",
            status="installed_partial" if plan.repair_actions else "installed_ready",
            steps=steps,
            metadata={"recommended_repair_actions": recommended_actions},
        )
        if not dry_run:
            self._write_state(state)
            self._record_action(
                action_type="install_repair_all",
                target="local",
                state=state,
                status="needs_user" if plan.repair_actions else "verified",
            )
        return state


def audit_install_upgrade_contract(*, strict: bool = False, root: Path | None = None) -> list[str]:
    root = root or Path(__file__).resolve().parents[2]
    errors: list[str] = []
    missing_statuses = REQUIRED_INSTALL_STATUSES - INSTALL_STATUSES
    if missing_statuses:
        errors.append(f"missing install statuses: {sorted(missing_statuses)}")
    system_contracts = importlib.import_module("core.system_contracts")

    required_actions = {
        "install_setup",
        "install_upgrade",
        "install_uninstall",
        "install_repair_all",
    }
    missing_actions = required_actions - system_contracts.ACTION_TYPES
    if missing_actions:
        errors.append(f"missing install action types: {sorted(missing_actions)}")
    if "deployment_migration" not in system_contracts.SCORECARD_DIMENSIONS:
        errors.append("deployment_migration scorecard dimension is missing")
    if strict:
        required_paths = (
            "core/setup/install_lifecycle.py",
            "core/cli/commands/setup.py",
            "scripts/audit_install_upgrade_contract.py",
            "scripts/e2e_install_probe.py",
            "scripts/e2e_upgrade_probe.py",
            "scripts/auto_setup.py",
            "scripts/verify_installation.py",
        )
        for rel in required_paths:
            if not (root / rel).exists():
                errors.append(f"missing required path: {rel}")
        try:
            import mnemos_cli

            parser = mnemos_cli.build_parser()
            choices = parser._subparsers._group_actions[0].choices
            for command in ("setup", "upgrade", "uninstall", "doctor"):
                if command not in choices:
                    errors.append(f"missing CLI command: mnemos {command}")
            doctor_parser = choices.get("doctor")
            if doctor_parser is not None:
                actions = [
                    action
                    for action in doctor_parser._actions
                    if getattr(action, "dest", "") == "doctor_action"
                ]
                doctor_choices = set(actions[0].choices or ()) if actions else set()
                if "repair-all" not in doctor_choices:
                    errors.append("doctor repair-all action is missing")
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
        ) as exc:  # pragma: no cover - strict import guard
            errors.append(f"CLI contract import failed: {exc}")
    return errors


def build_install_lifecycle_health(config: Any | None = None) -> dict[str, Any]:
    if config is None:
        from core.config import get_config

        config = get_config()
    manager = InstallLifecycleManager(config)
    errors = audit_install_upgrade_contract(strict=True)
    state = manager.health_state()
    errors.extend(state.validate())
    incomplete_required_steps: list[dict[str, Any]] = []
    incomplete_required_step_ids: list[str] = []
    for step in state.steps:
        if not step.required or step.status == "ok":
            continue
        incomplete_required_step_ids.append(step.step_id)
        incomplete_required_steps.append(
            {
                "step_id": step.step_id,
                "title": step.title,
                "status": step.status,
                "failure_reason": step.failure_reason,
                "repair_action": step.repair_action,
                "evidence_refs": list(step.evidence_refs),
            }
        )
    if state.status != "installed_ready":
        errors.append(f"install_lifecycle_state: {state.status}")
    if incomplete_required_steps:
        errors.append("incomplete_required_steps: " + ", ".join(incomplete_required_step_ids))
    return {
        "schema_version": INSTALL_LIFECYCLE_SCHEMA_VERSION,
        "status": "ok" if not errors else "degraded",
        "state": state.as_dict(),
        "incomplete_required_steps": incomplete_required_steps,
        "recommended_entrypoints": list(RECOMMENDED_ENTRYPOINTS),
        "repair_actions": list(state.repair_actions),
        "state_path": str(manager.state_path),
        "action_ledger_ref": str(manager.action_ledger_path),
        "errors": errors,
        "error": "; ".join(errors) if errors else "",
    }
