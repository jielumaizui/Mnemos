"""Unified auto-healing orchestration for operational findings.

This module is intentionally conservative: it builds one machine-readable plan
for scattered repair surfaces and only applies actions that have an explicit
handler, rollback reference, and verification record.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, cast, runtime_checkable

from core.cognitive.decision_trace import (
    MaterialActionAuthorization,
    MaterialActionCoordinator,
    MaterialActionObservation,
    MaterialActionPermit,
    MaterialActionReceipt,
    require_material_action,
    resolve_material_action_recovery_authorization,
)
from core.cognitive.state_contract import sha256_json
from core.cognitive.state_store import CognitiveStateStore
from core.system_contracts import ActionLedger, make_action_record

AUTO_HEAL_SCHEMA_VERSION = "mnemos.auto_heal_orchestrator.v1"
AUTO_HEAL_ACTION = "auto_heal"
AUTO_HEAL_OWNER = "auto_healing"
AUTO_HEAL_EXECUTOR = "auto_healing_orchestrator"
logger = logging.getLogger(__name__)
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
AUTO_HEAL_STATUSES = {
    "auto_fixed",
    "auto_fix_failed",
    "needs_user",
    "ignored_with_reason",
    "blocked",
}
AUTO_HEAL_RISKS = {"low", "medium", "high", "critical"}
NON_ACTIONABLE_STATUSES = {"ok"}
HEALTH_STATUS_TO_RISK = {
    "warning": "medium",
    "skipped": "low",
    "degraded": "high",
    "failed": "critical",
    "error": "critical",
}


def _is_actionable_health_check(check: Mapping[str, Any]) -> bool:
    status = str(check.get("status", "ok"))
    if status in NON_ACTIONABLE_STATUSES:
        return False
    if status == "skipped" and bool(check.get("optional")):
        return False
    return True


@dataclass(frozen=True)
class AutoHealPolicy:
    """Policy for deciding whether a finding can be repaired automatically."""

    risk_level: str = "medium"
    auto_fix_policy: str = "manual"
    rollback_plan: str = "manual rollback or no write has occurred"
    verification_command: str = "python3 mnemos_cli.py health --json"
    user_intervention_required: bool = True


@dataclass(frozen=True)
class AutoHealExecution:
    """Result returned by an explicit auto-heal handler."""

    success: bool
    before_ref: str = ""
    after_ref: str = ""
    before_hash: str = ""
    after_hash: str = ""
    rollback_ref: str = ""
    evidence_refs: tuple[str, ...] = ()
    verification: Mapping[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass
class AutoHealIssue:
    """Machine-readable decision card for one repairable or reportable issue."""

    issue_id: str
    source: str
    subject: str
    issue_type: str
    severity: str
    risk_level: str
    status: str
    status_reason: str
    repair_actions: list[str] = field(default_factory=list)
    rollback_plan: str = ""
    verification_command: str = ""
    user_intervention_required: bool = False
    evidence_refs: list[str] = field(default_factory=list)
    before_ref: str = ""
    after_ref: str = ""
    rollback_ref: str = ""
    action_ledger_ref: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@runtime_checkable
class AutoHealHandler(Protocol):
    """Crash-recoverable auto-heal effect family.

    ``execute`` may mutate only the exact target bound by ``permit``.  ``observe``
    must be read-only and return a result only when that same ``effect_id`` has
    a durable target-local completion marker.  Requiring both methods prevents
    a retry from blindly invoking an arbitrary callable after the target effect
    committed but before the canonical terminal receipt was written.
    """

    def execute(
        self,
        issue: AutoHealIssue,
        permit: MaterialActionPermit,
    ) -> AutoHealExecution:
        """Execute one exact, idempotency-keyed repair."""

        ...

    def observe(
        self,
        issue: AutoHealIssue,
        permit: MaterialActionPermit,
    ) -> AutoHealExecution | None:
        """Read the durable result for this exact effect, without mutation."""

        ...


def _validate_auto_heal_permit(
    permit: MaterialActionPermit,
    binding: Mapping[str, str],
) -> None:
    if not isinstance(permit, MaterialActionPermit):
        raise PermissionError("auto-heal requires a typed material-action permit")
    if (
        permit.owner != AUTO_HEAL_OWNER
        or permit.executor_id != AUTO_HEAL_EXECUTOR
        or permit.action_type != AUTO_HEAL_ACTION
        or permit.target_ref != binding["target_ref"]
        or permit.input_hash != binding["input_hash"]
    ):
        raise PermissionError("auto-heal permit does not match the exact repair")


def _validate_auto_heal_execution(
    execution: AutoHealExecution,
    *,
    permit: MaterialActionPermit,
) -> None:
    if not isinstance(execution, AutoHealExecution):
        raise TypeError("auto-heal handler must return AutoHealExecution")
    if not _SHA256_RE.fullmatch(str(execution.before_hash)):
        raise ValueError("auto-heal execution requires an exact before_hash")
    if not _SHA256_RE.fullmatch(str(execution.after_hash)):
        raise ValueError("auto-heal execution requires an exact after_hash")
    refs = tuple(str(ref).strip() for ref in execution.evidence_refs)
    if not refs or any(not ref for ref in refs):
        raise ValueError("auto-heal execution requires exact target evidence")
    oracle_ref = f"target-oracle:{permit.effect_id}"
    if not any(ref == oracle_ref or ref.startswith(f"{oracle_ref}:") for ref in refs):
        raise ValueError(
            "auto-heal execution requires an effect-bound target oracle"
        )
    if not execution.rollback_ref:
        raise ValueError("auto-heal execution requires an exact rollback_ref")
    if not execution.success and execution.before_hash != execution.after_hash:
        raise ValueError(
            "failed auto-heal may terminate only after proving unchanged state"
        )


@dataclass
class _AutoHealEffectOracle:
    """Adapt a handler's target-local observer to canonical recovery."""

    issue: AutoHealIssue
    handler: AutoHealHandler
    binding: Mapping[str, str]
    last_execution: AutoHealExecution | None = field(init=False, default=None)

    owner = AUTO_HEAL_OWNER
    executor_id = AUTO_HEAL_EXECUTOR
    action_type = AUTO_HEAL_ACTION

    def observe(
        self,
        permit: MaterialActionPermit,
    ) -> MaterialActionObservation | None:
        """Observe and normalize a handler-owned auto-heal target effect."""

        _validate_auto_heal_permit(permit, self.binding)
        execution = self.handler.observe(self.issue, permit)
        if execution is None:
            self.last_execution = None
            return None
        _validate_auto_heal_execution(execution, permit=permit)
        self.last_execution = execution
        refs = list(execution.evidence_refs)
        if execution.success:
            refs.append(f"target-after:{execution.after_hash}")
        else:
            refs.append(f"attempted-effect:{permit.effect_id}")
        return MaterialActionObservation(
            status="committed" if execution.success else "failed_terminal",
            before_hash=execution.before_hash,
            after_hash=execution.after_hash,
            evidence_refs=tuple(dict.fromkeys(refs)),
            reason_code=(
                "" if execution.success else "auto_heal_handler_reported_failure"
            ),
            outcome=(
                "auto-heal target effect committed"
                if execution.success
                else execution.error or "auto-heal effect did not commit"
            ),
            observed_at=datetime.now().astimezone().isoformat(),
        )


def auto_heal_material_action_binding(issue: AutoHealIssue) -> dict[str, str]:
    """Bind one health repair to the immutable issue state seen by its handler."""

    payload = {
        "schema_version": "mnemos.auto_heal_input.v1",
        "issue_id": str(issue.issue_id),
        "source": str(issue.source),
        "subject": str(issue.subject),
        "issue_type": str(issue.issue_type),
        "severity": str(issue.severity),
        "risk_level": str(issue.risk_level),
        "status_reason": str(issue.status_reason),
        "repair_actions": list(issue.repair_actions),
        "rollback_plan": str(issue.rollback_plan),
        "verification_command": str(issue.verification_command),
        "evidence_refs": list(issue.evidence_refs),
    }
    return {
        "target_ref": str(issue.issue_id),
        "input_hash": sha256_json(payload),
    }


DEFAULT_HEALTH_POLICIES: dict[str, AutoHealPolicy] = {
    "queues": AutoHealPolicy(
        risk_level="medium",
        auto_fix_policy="manual",
        rollback_plan="queue action can be retried or reverted by status-specific CLI",
        verification_command="python3 mnemos_cli.py health --json",
    ),
    "amphora": AutoHealPolicy(
        risk_level="medium",
        auto_fix_policy="manual",
        rollback_plan="failed task archive/retry state remains auditable in Amphora",
        verification_command="python3 mnemos_cli.py health --json",
    ),
    "heartbeat": AutoHealPolicy(
        risk_level="medium",
        auto_fix_policy="manual",
        rollback_plan="restart daemon or disable the failing service explicitly",
        verification_command="python3 mnemos_cli.py health --json",
    ),
    "wiki_route": AutoHealPolicy(
        risk_level="medium",
        auto_fix_policy="manual",
        rollback_plan="Wiki routing must be checked through Charon/content audit",
        verification_command="python3 mnemos_cli.py health --json",
    ),
    "sqlite_disk_budget": AutoHealPolicy(
        risk_level="high",
        auto_fix_policy="manual",
        rollback_plan=(
            "safe repair only checkpoints WAL and deletes stale Mnemos temp files; "
            "snapshots/raw_events require manual retention decisions"
        ),
        verification_command="python3 mnemos_cli.py health --json",
    ),
    "security": AutoHealPolicy(
        risk_level="low",
        auto_fix_policy="manual",
        rollback_plan="chmod changes can be reverted to the previous mode if needed",
        verification_command="python3 mnemos_cli.py health --json",
    ),
}


def _config_int(config: Any, key: str, default: int) -> int:
    try:
        raw = config.get(key, default)
        return default if raw is None else int(raw)
    except (AttributeError, TypeError, ValueError):
        return default


def _config_bool(config: Any, key: str, default: bool) -> bool:
    try:
        raw = config.get(key, default)
    except AttributeError:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


def _problem_text(check: Mapping[str, Any]) -> str:
    error = check.get("error")
    if error:
        return str(error)
    warnings = check.get("warnings")
    if isinstance(warnings, list) and warnings:
        return "; ".join(str(item) for item in warnings[:2])
    return str(check.get("status", "unknown"))


class AutoHealingOrchestrator:
    """Plan and apply explicitly safe auto-healing actions."""

    def __init__(
        self,
        config: Any,
        *,
        handlers: Mapping[str, AutoHealHandler] | None = None,
        policies: Mapping[str, AutoHealPolicy] | None = None,
        material_action_resolver: Callable[
            [Mapping[str, str]], MaterialActionAuthorization
        ]
        | None = None,
    ) -> None:
        self.config = config
        self.handlers = dict(handlers or {})
        self.policies = {**DEFAULT_HEALTH_POLICIES, **dict(policies or {})}
        self._material_action_resolver = material_action_resolver

    def _resolve_material_action(
        self,
        binding: Mapping[str, str],
        command_ids: Mapping[str, str] | None,
    ) -> MaterialActionAuthorization:
        if self._material_action_resolver is not None:
            return self._material_action_resolver(binding)
        if isinstance(command_ids, Mapping):
            command_id = str(command_ids.get(binding["target_ref"]) or "").strip()
            if not command_id:
                raise PermissionError("auto-heal action lacks its exact material command")
            return MaterialActionCoordinator(
                CognitiveStateStore(self.config)
            ).bind_for_recovery(
                command_id,
                executor_id=AUTO_HEAL_EXECUTOR,
            )
        authorization, _ = resolve_material_action_recovery_authorization(
            None,
            owner=AUTO_HEAL_OWNER,
            executor_id=AUTO_HEAL_EXECUTOR,
            action_type=AUTO_HEAL_ACTION,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
            expected_state_db=Path(self.config.database_dir)
            / "producer_consumer_ledger.db",
        )
        return authorization

    def build_health_report(
        self,
        checks: Mapping[str, Mapping[str, Any]],
        *,
        apply: bool = False,
        material_action_commands: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if not _config_bool(self.config, "auto_heal.enabled", True):
            return {
                "status": "skipped",
                "ok": True,
                "schema_version": AUTO_HEAL_SCHEMA_VERSION,
                "mode": "disabled",
                "issues": [],
                "counts": {},
                "user_intervention_budget": self._user_intervention_budget([]),
                "action_log_contract": "ActionLedger(action_type=auto_heal)",
                "reason": "auto_heal.enabled is false",
            }
        issues = [
            self._issue_from_health_check(
                name,
                check,
                apply=apply,
                material_action_commands=material_action_commands,
            )
            for name, check in checks.items()
            if name != "auto_healing"
            and _is_actionable_health_check(check)
        ]
        counts = Counter(issue.status for issue in issues)
        user_budget = self._user_intervention_budget(issues)
        status = "ok"
        if issues:
            status = "warning"
        if user_budget["exceeded"] or counts.get("auto_fix_failed") or counts.get("blocked"):
            status = "degraded"
        return {
            "status": status,
            "ok": status == "ok",
            "schema_version": AUTO_HEAL_SCHEMA_VERSION,
            "mode": "apply" if apply else "dry_run",
            "issues": [issue.as_dict() for issue in issues],
            "counts": dict(counts),
            "user_intervention_budget": user_budget,
            "action_log_contract": "ActionLedger(action_type=auto_heal)",
        }

    def _issue_from_health_check(
        self,
        name: str,
        check: Mapping[str, Any],
        *,
        apply: bool,
        material_action_commands: Mapping[str, str] | None,
    ) -> AutoHealIssue:
        status = str(check.get("status", "warning"))
        policy = self.policies.get(
            name,
            AutoHealPolicy(risk_level=HEALTH_STATUS_TO_RISK.get(status, "medium")),
        )
        verification_command = (
            policy.verification_command
            or str(
                getattr(self.config, "get", lambda _key, default=None: default)(
                    "auto_heal.default_verification_command",
                    "python3 mnemos_cli.py health --json",
                )
            )
        )
        repair_actions = [
            str(action)
            for action in check.get("repair_actions", [])
            if str(action).strip()
        ]
        issue = AutoHealIssue(
            issue_id=f"health:{name}",
            source="health",
            subject=name,
            issue_type=name,
            severity=status,
            risk_level=policy.risk_level,
            status="needs_user",
            status_reason=_problem_text(check),
            repair_actions=repair_actions,
            rollback_plan=policy.rollback_plan,
            verification_command=verification_command,
            user_intervention_required=policy.user_intervention_required,
            evidence_refs=["core/ops/health_check.py"],
        )
        if status == "skipped":
            issue.status = "ignored_with_reason"
            issue.user_intervention_required = False
            issue.status_reason = f"check skipped: {issue.status_reason}"
            return issue

        handler_key = f"health.{name}"
        handler = self.handlers.get(handler_key)
        if policy.auto_fix_policy == "auto" and handler:
            return (
                self._apply_handler(
                    issue,
                    handler,
                    material_action_commands=material_action_commands,
                )
                if apply
                else issue
            )

        if not repair_actions and policy.auto_fix_policy != "auto":
            issue.status = "blocked"
            issue.user_intervention_required = True
            issue.status_reason = f"no repair action advertised: {issue.status_reason}"
        return issue

    def _apply_handler(
        self,
        issue: AutoHealIssue,
        handler: AutoHealHandler,
        *,
        material_action_commands: Mapping[str, str] | None,
    ) -> AutoHealIssue:
        if not _config_bool(self.config, "auto_heal.record_action_ledger", True):
            raise PermissionError(
                "material auto-heal requires the canonical ActionLedger projection"
            )
        if not isinstance(handler, AutoHealHandler):
            raise TypeError(
                "auto-heal apply requires execute() plus a read-only observe() oracle"
            )
        binding = auto_heal_material_action_binding(issue)
        authorization = self._resolve_material_action(
            binding,
            material_action_commands,
        )
        permit = authorization.permit
        _validate_auto_heal_permit(permit, binding)
        oracle = _AutoHealEffectOracle(issue, handler, binding)
        receipt = authorization.recover(oracle)
        execution = oracle.last_execution

        if receipt is not None and execution is None:
            # ``recover`` returns an existing receipt without consulting the
            # target.  Re-observe it so the ActionLedger projection is rebuilt
            # from target truth after a crash between those two writes.
            observation = oracle.observe(permit)
            if observation is None:
                raise RuntimeError(
                    "terminal auto-heal receipt lacks its exact target observation"
                )
            execution = oracle.last_execution

        if receipt is None:
            permit = require_material_action(
                authorization,
                owner=AUTO_HEAL_OWNER,
                executor_id=AUTO_HEAL_EXECUTOR,
                action_type=AUTO_HEAL_ACTION,
                target_ref=binding["target_ref"],
                input_hash=binding["input_hash"],
                expected_state_db=Path(self.config.database_dir)
                / "producer_consumer_ledger.db",
            )
            reported: AutoHealExecution | None = None
            execution_error: Exception | None = None
            try:
                reported = handler.execute(issue, permit)
                _validate_auto_heal_execution(reported, permit=permit)
            except (
                OSError,
                ValueError,
                TypeError,
                RuntimeError,
                AttributeError,
            ) as exc:
                execution_error = exc

            # Always observe after the attempt.  If the process died after the
            # target commit, the same path is used on restart and execute() is
            # never invoked again.
            receipt = authorization.recover(oracle)
            execution = oracle.last_execution
            if receipt is None or execution is None:
                if execution_error is None:
                    raise RuntimeError(
                        "auto-heal handler returned without a durable target observation"
                    )
                issue.status = "auto_fix_failed"
                issue.error = str(execution_error)
                issue.user_intervention_required = True
                return issue
            if reported is not None and execution_error is None and reported != execution:
                raise RuntimeError(
                    "auto-heal target observation disagrees with handler result"
                )

        if execution is None or receipt is None:
            raise RuntimeError("auto-heal recovery did not produce an exact result")
        self._validate_recovered_execution(receipt, execution)

        issue.before_ref = execution.before_ref
        issue.after_ref = execution.after_ref
        issue.rollback_ref = execution.rollback_ref
        issue.error = execution.error
        issue.status = "auto_fixed" if execution.success else "auto_fix_failed"
        issue.user_intervention_required = not execution.success
        issue.action_ledger_ref = self._record_action(
            issue,
            execution,
            authorization=authorization,
            permit=permit,
            receipt=receipt,
            input_hash=binding["input_hash"],
        )
        if not execution.success and execution.error:
            issue.status_reason = execution.error
        return issue

    @staticmethod
    def _validate_recovered_execution(
        receipt: MaterialActionReceipt,
        execution: AutoHealExecution,
    ) -> None:
        expected_status = "committed" if execution.success else "failed_terminal"
        if (
            receipt.status != expected_status
            or receipt.before_hash != execution.before_hash
            or receipt.after_hash != execution.after_hash
        ):
            raise RuntimeError(
                "auto-heal canonical receipt disagrees with target observation"
            )

    def _record_action(
        self,
        issue: AutoHealIssue,
        execution: AutoHealExecution,
        *,
        authorization: MaterialActionAuthorization,
        permit: MaterialActionPermit,
        receipt: MaterialActionReceipt,
        input_hash: str,
    ) -> str:
        verification = dict(execution.verification or {})
        if "command" not in verification:
            verification["command"] = issue.verification_command
        verification["material_input_hash"] = input_hash
        status = "verified" if execution.success else "failed_terminal"
        canonical_refs = (
            f"material-command:{permit.command_id}",
            f"decision-revision:{permit.decision_revision_id}",
            f"material-effect:{permit.effect_id}",
        )
        record = replace(
            make_action_record(
                action_id=permit.action_id,
                actor=AUTO_HEAL_EXECUTOR,
                action_type=AUTO_HEAL_ACTION,
                target=issue.issue_id,
                evidence_refs=tuple(
                    dict.fromkeys(
                        (
                            *issue.evidence_refs,
                            *canonical_refs,
                            *execution.evidence_refs,
                        )
                    )
                ),
                before_ref=execution.before_ref,
                after_ref=execution.after_ref,
                quality_decision_id=permit.decision_revision_id,
                rollback_ref=execution.rollback_ref,
                verification=verification,
                status=status,
            ),
            created_at=receipt.created_at,
        )
        return cast(
            str,
            ActionLedger.from_config(self.config, initialize=True).record(
                record,
                material_action=authorization,
            ),
        )

    def _user_intervention_budget(self, issues: list[AutoHealIssue]) -> dict[str, Any]:
        max_items = _config_int(self.config, "auto_heal.user_intervention_budget", 5)
        needs_user = [issue for issue in issues if issue.user_intervention_required]
        return {
            "max": max_items,
            "used": len(needs_user),
            "remaining": max(0, max_items - len(needs_user)),
            "exceeded": len(needs_user) > max_items,
            "reasons": [
                {
                    "issue_id": issue.issue_id,
                    "risk_level": issue.risk_level,
                    "reason": issue.status_reason,
                    "default_action": issue.repair_actions[0] if issue.repair_actions else "",
                }
                for issue in needs_user
            ],
        }


def build_health_auto_heal_report(
    config: Any,
    checks: Mapping[str, Mapping[str, Any]],
    *,
    apply: bool = False,
    record_profile_usage: bool = False,
    material_action_commands: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build the auto-healing decision layer for an existing health report."""

    report = AutoHealingOrchestrator(config).build_health_report(
        checks,
        apply=apply,
        material_action_commands=material_action_commands,
    )
    if record_profile_usage:
        _record_profile_usage_for_auto_heal(report)
    return report


def _record_profile_usage_for_auto_heal(report: Mapping[str, Any]) -> None:
    """Skip profile claim reads from an unauthenticated health worker."""

    logger.debug(
        "auto-heal profile usage skipped: principal and scope are required"
    )


def annotate_checks_with_auto_heal(
    checks: dict[str, dict[str, Any]],
    auto_heal_report: Mapping[str, Any],
) -> None:
    """Attach one auto-heal state card to each non-ok health check."""

    for issue in auto_heal_report.get("issues", []):
        if not isinstance(issue, Mapping):
            continue
        if issue.get("source") != "health":
            continue
        subject = str(issue.get("subject", ""))
        check = checks.get(subject)
        if check is None:
            continue
        check["auto_heal_state"] = issue.get("status", "")
        check["auto_heal_issue_id"] = issue.get("issue_id", "")
        check["auto_heal_risk_level"] = issue.get("risk_level", "")
        check["user_intervention_required"] = bool(
            issue.get("user_intervention_required", False)
        )


def format_auto_heal_report(report: Mapping[str, Any]) -> str:
    """Render a compact human-readable repair plan."""

    budget = report.get("user_intervention_budget", {})
    lines = [
        "Mnemos Auto-Healing",
        "=" * 40,
        f"status: {report.get('status', 'unknown')}",
        f"mode: {report.get('mode', 'dry_run')}",
        (
            "user_intervention_budget: "
            f"{budget.get('used', 0)}/{budget.get('max', 0)}"
        ),
    ]
    for issue in report.get("issues", []):
        if not isinstance(issue, Mapping):
            continue
        action = ""
        actions = issue.get("repair_actions") or []
        if actions:
            action = f" action={actions[0]}"
        lines.append(
            f"- {issue.get('issue_id')}: {issue.get('status')} "
            f"risk={issue.get('risk_level')}{action}"
        )
    return "\n".join(lines)
