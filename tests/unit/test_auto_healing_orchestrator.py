import json
import sqlite3
from types import SimpleNamespace

import pytest

from core.ops.auto_healing import (
    AUTO_HEAL_ACTION,
    AUTO_HEAL_EXECUTOR,
    AUTO_HEAL_OWNER,
    AutoHealExecution,
    AutoHealPolicy,
    AutoHealingOrchestrator,
    annotate_checks_with_auto_heal,
    build_health_auto_heal_report,
)
from core.cognitive.state_contract import sha256_json
from core.system_contracts import ActionLedger
from tests.cognitive_decision_fixtures import material_action_resolver
from tests.cognitive_decision_fixtures import material_action_authorization


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


def test_health_findings_get_status_and_user_intervention_budget(tmp_path):
    checks = {
        "api": {
            "status": "degraded",
            "error": "model endpoints missing",
            "repair_actions": ["configure llm/embedding/reranker"],
        },
        "event_bus": {"status": "skipped", "error": "events.db not initialized"},
        "storage": {"status": "ok"},
    }

    report = build_health_auto_heal_report(
        _config(tmp_path, {"auto_heal.user_intervention_budget": 1}),
        checks,
    )

    by_id = {issue["issue_id"]: issue for issue in report["issues"]}
    assert by_id["health:api"]["status"] == "needs_user"
    assert by_id["health:event_bus"]["status"] == "ignored_with_reason"
    assert report["user_intervention_budget"]["used"] == 1
    assert report["user_intervention_budget"]["exceeded"] is False


def test_annotation_adds_auto_heal_state_to_non_ok_checks(tmp_path):
    checks = {
        "api": {"status": "degraded", "repair_actions": ["configure api"]},
        "storage": {"status": "ok"},
    }
    report = build_health_auto_heal_report(_config(tmp_path), checks)

    annotate_checks_with_auto_heal(checks, report)

    assert checks["api"]["auto_heal_state"] == "needs_user"
    assert checks["api"]["auto_heal_issue_id"] == "health:api"
    assert "auto_heal_state" not in checks["storage"]


def test_optional_skipped_checks_do_not_create_auto_heal_issues(tmp_path):
    checks = {
        "multimodal": {
            "status": "skipped",
            "optional": True,
            "repair_actions": ["Set MNEMOS_MULTIMODAL_*"],
        },
    }

    report = build_health_auto_heal_report(_config(tmp_path), checks)

    assert report["status"] == "ok"
    assert report["ok"] is True
    assert report["issues"] == []


def test_profile_usage_lock_timeout_does_not_degrade_auto_heal_report(tmp_path, monkeypatch):
    """画像消费记录是副作用，SQLite 锁超时不应让 auto-healing 报告失败。"""

    class LockedStore:
        def build_user_cognitive_profile_v2(self):
            raise sqlite3.OperationalError("sqlite lock timeout for user_signals.db")

    monkeypatch.setattr("core.persona.psyche.get_signal_store", lambda: LockedStore())

    report = build_health_auto_heal_report(_config(tmp_path), {"storage": {"status": "ok"}})

    assert report["status"] == "ok"
    assert report["ok"] is True


def test_low_risk_explicit_handler_records_action_ledger(tmp_path):
    def fix_permissions(issue, permit):
        return AutoHealExecution(
            success=True,
            before_ref="mode://0o755",
            after_ref="mode://0o700",
            before_hash=sha256_json({"mode": "0o755"}),
            after_hash=sha256_json({"mode": "0o700"}),
            rollback_ref="mode://restore-0o755",
            evidence_refs=(f"target-oracle:{permit.effect_id}:file-mode:0o700",),
            verification={"command": "python3 mnemos_cli.py health --json"},
        )

    orchestrator = AutoHealingOrchestrator(
        _config(tmp_path),
        handlers={"health.security": _ObservedHandler(fix_permissions)},
        policies={
            "security": AutoHealPolicy(
                risk_level="low",
                auto_fix_policy="auto",
                rollback_plan="restore previous file mode",
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

    report = orchestrator.build_health_report(
        {"security": {"status": "warning", "repair_actions": ["chmod 700 ~/.mnemos"]}},
        apply=True,
    )

    issue = report["issues"][0]
    assert issue["status"] == "auto_fixed"
    assert issue["user_intervention_required"] is False
    assert issue["action_ledger_ref"].startswith("material-action-")
    row = ActionLedger(tmp_path / "action_ledger.db").recent()[0]
    assert row["action_type"] == "auto_heal"
    assert row["target"] == "health:security"
    assert row["rollback_ref"] == "mode://restore-0o755"
    assert row["quality_decision_id"]
    assert row["verification"]["material_input_hash"].startswith("sha256:")


@pytest.mark.no_canonical_material_actions
def test_auto_heal_fails_closed_before_handler_without_authorization(tmp_path):
    invoked = False

    def fix_permissions(issue, permit):
        nonlocal invoked
        invoked = True
        return AutoHealExecution(success=True)

    orchestrator = AutoHealingOrchestrator(
        _config(tmp_path),
        handlers={"health.security": _ObservedHandler(fix_permissions)},
        policies={
            "security": AutoHealPolicy(
                risk_level="low",
                auto_fix_policy="auto",
                rollback_plan="restore previous mode",
                user_intervention_required=False,
            )
        },
    )

    with pytest.raises(
        PermissionError,
        match="canonical material-action authorization is required",
    ):
        orchestrator.build_health_report(
            {"security": {"status": "warning", "repair_actions": ["chmod"]}},
            apply=True,
        )

    assert invoked is False


def test_auto_heal_failed_terminal_requires_proven_unchanged_state(tmp_path):
    state_hash = sha256_json({"mode": "0o755"})

    def failed_permissions_fix(issue, permit):
        return AutoHealExecution(
            success=False,
            before_ref="mode://0o755",
            after_ref="mode://0o755",
            before_hash=state_hash,
            after_hash=state_hash,
            rollback_ref="mode://restore-0o755",
            evidence_refs=(f"target-oracle:{permit.effect_id}:file-mode:0o755",),
            verification={"result": "unchanged"},
            error="permission denied before chmod",
        )

    orchestrator = AutoHealingOrchestrator(
        _config(tmp_path),
        handlers={
            "health.security": _ObservedHandler(failed_permissions_fix)
        },
        policies={
            "security": AutoHealPolicy(
                risk_level="low",
                auto_fix_policy="auto",
                rollback_plan="restore previous mode",
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

    report = orchestrator.build_health_report(
        {"security": {"status": "warning", "repair_actions": ["chmod"]}},
        apply=True,
    )

    issue = report["issues"][0]
    assert issue["status"] == "auto_fix_failed"
    row = ActionLedger(tmp_path / "action_ledger.db").recent()[0]
    assert row["status"] == "failed_terminal"


def test_auto_heal_recovers_target_commit_without_reinvoking_handler(tmp_path):
    target_db = tmp_path / "auto_heal_target.db"
    with sqlite3.connect(str(target_db)) as conn:
        conn.execute("CREATE TABLE target_state(id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO target_state VALUES (1, 'before')")
        conn.execute(
            """
            CREATE TABLE target_effects(
                effect_id TEXT PRIMARY KEY,
                before_value TEXT NOT NULL,
                after_value TEXT NOT NULL
            )
            """
        )

    class DurableHandler:
        def __init__(self):
            self.execute_calls = 0

        def execute(self, issue, permit):
            self.execute_calls += 1
            with sqlite3.connect(str(target_db)) as conn:
                before = conn.execute(
                    "SELECT value FROM target_state WHERE id=1"
                ).fetchone()[0]
                conn.execute("UPDATE target_state SET value='after' WHERE id=1")
                conn.execute(
                    "INSERT INTO target_effects VALUES (?, ?, 'after')",
                    (permit.effect_id, before),
                )
            raise OSError("injected crash after target transaction")

        def observe(self, issue, permit):
            with sqlite3.connect(str(target_db)) as conn:
                row = conn.execute(
                    """
                    SELECT e.before_value, e.after_value, s.value
                    FROM target_effects e CROSS JOIN target_state s
                    WHERE e.effect_id=? AND s.id=1
                    """,
                    (permit.effect_id,),
                ).fetchone()
            if row is None:
                return None
            before, after, current = row
            if after != current:
                raise RuntimeError("auto-heal target marker disagrees with target")
            return AutoHealExecution(
                success=True,
                before_ref=f"sqlite://{target_db}#value={before}",
                after_ref=f"sqlite://{target_db}#value={after}",
                before_hash=sha256_json({"value": before}),
                after_hash=sha256_json({"value": after}),
                rollback_ref=f"sqlite://{target_db}#restore={before}",
                evidence_refs=(
                    f"target-oracle:{permit.effect_id}:sqlite:{target_db}",
                ),
                verification={"target_value": current},
            )

    handler = DurableHandler()
    authorization = None

    def resolve(binding):
        nonlocal authorization
        if authorization is None:
            authorization = material_action_authorization(
                tmp_path,
                action_type=AUTO_HEAL_ACTION,
                owner=AUTO_HEAL_OWNER,
                executor=AUTO_HEAL_EXECUTOR,
                target_ref=binding["target_ref"],
                input_hash=binding["input_hash"],
                nonce="auto-heal-crash-recovery",
            )
        return authorization

    orchestrator = AutoHealingOrchestrator(
        _config(tmp_path),
        handlers={"health.security": handler},
        policies={
            "security": AutoHealPolicy(
                risk_level="low",
                auto_fix_policy="auto",
                rollback_plan="restore target value",
                user_intervention_required=False,
            )
        },
        material_action_resolver=resolve,
    )
    checks = {
        "security": {"status": "warning", "repair_actions": ["repair target"]}
    }

    first = orchestrator.build_health_report(checks, apply=True)
    second = orchestrator.build_health_report(checks, apply=True)

    assert first["issues"][0]["status"] == "auto_fixed"
    assert second["issues"][0]["status"] == "auto_fixed"
    assert handler.execute_calls == 1
    assert len(ActionLedger(tmp_path / "action_ledger.db").recent()) == 1


def test_doctor_repair_dry_run_outputs_unified_plan(tmp_path, monkeypatch, capsys):
    from core.cli.commands import doctor

    cfg = _config(tmp_path)
    monkeypatch.setattr(doctor, "_get_config", lambda: cfg)
    monkeypatch.setattr(
        "core.ops.health_check.build_health_report_quiet",
        lambda _cfg: {
            "checks": {
                "api": {
                    "status": "degraded",
                    "error": "missing api config",
                    "repair_actions": ["configure providers"],
                }
            }
        },
    )
    args = SimpleNamespace(
        doctor_action="repair",
        agent_name="",
        dry_run=True,
        json=True,
    )

    assert doctor.cmd_doctor(args) is True
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == "mnemos.auto_heal_orchestrator.v1"
    assert payload["issues"][0]["issue_id"] == "health:api"
