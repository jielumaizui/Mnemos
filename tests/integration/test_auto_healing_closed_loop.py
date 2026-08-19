from types import SimpleNamespace

from core.ops.auto_healing import (
    AUTO_HEAL_ACTION,
    AUTO_HEAL_EXECUTOR,
    AUTO_HEAL_OWNER,
    AutoHealExecution,
    AutoHealPolicy,
    AutoHealingOrchestrator,
)
from core.cognitive.state_contract import sha256_json
from core.system_contracts import ActionLedger
from tests.cognitive_decision_fixtures import material_action_resolver


def _config(tmp_path, values=None):
    values = values or {}
    return SimpleNamespace(
        database_dir=tmp_path,
        get=lambda key, default=None: values.get(key, default),
    )


class _ObservedHandler:
    def __init__(self, execute):
        self._execute = execute
        self._results = {}

    def execute(self, issue, permit):
        result = self._execute(issue, permit)
        self._results[permit.effect_id] = result
        return result

    def observe(self, issue, permit):
        return self._results.get(permit.effect_id)


def test_auto_healing_closed_loop_records_fix_budget_and_rollback(tmp_path):
    target = tmp_path / "repair-target.txt"
    target.write_text("before", encoding="utf-8")

    def repair_security(issue, permit):
        before = target.read_text(encoding="utf-8")
        target.write_text("after", encoding="utf-8")
        return AutoHealExecution(
            success=True,
            before_ref=f"file://{target}#content={before}",
            after_ref=f"file://{target}#content=after",
            before_hash=sha256_json({"content": before}),
            after_hash=sha256_json({"content": "after"}),
            rollback_ref=f"file://{target}#content={before}",
            evidence_refs=(
                f"target-oracle:{permit.effect_id}:file:{target}",
            ),
            verification={
                "command": "python3 mnemos_cli.py health --json",
                "result": "passed",
            },
        )

    orchestrator = AutoHealingOrchestrator(
        _config(tmp_path, {"auto_heal.user_intervention_budget": 1}),
        handlers={"health.security": _ObservedHandler(repair_security)},
        policies={
            "security": AutoHealPolicy(
                risk_level="low",
                auto_fix_policy="auto",
                rollback_plan="restore previous content",
                verification_command="python3 mnemos_cli.py health --json",
                user_intervention_required=False,
            )
        },
        material_action_resolver=material_action_resolver(
            tmp_path,
            action_type=AUTO_HEAL_ACTION,
            owner=AUTO_HEAL_OWNER,
            executor=AUTO_HEAL_EXECUTOR,
        ),
    )
    checks = {
        "security": {
            "status": "warning",
            "repair_actions": ["repair local permissions/content marker"],
        },
        "queues": {
            "status": "degraded",
            "repair_actions": ["inspect capture queue backlog"],
        },
    }

    report = orchestrator.build_health_report(checks, apply=True)

    issues = {issue["issue_id"]: issue for issue in report["issues"]}
    assert issues["health:security"]["status"] == "auto_fixed"
    assert issues["health:security"]["user_intervention_required"] is False
    assert issues["health:security"]["rollback_ref"].endswith("#content=before")
    assert issues["health:queues"]["status"] == "needs_user"
    assert report["user_intervention_budget"]["used"] == 1
    assert report["user_intervention_budget"]["exceeded"] is False
    assert target.read_text(encoding="utf-8") == "after"

    ledger_rows = ActionLedger(tmp_path / "action_ledger.db").recent()
    assert len(ledger_rows) == 1
    assert ledger_rows[0]["action_type"] == "auto_heal"
    assert ledger_rows[0]["target"] == "health:security"
    assert ledger_rows[0]["rollback_ref"].endswith("#content=before")
