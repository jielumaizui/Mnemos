from __future__ import annotations


def test_health_report_exposes_canonical_check_ids_and_hash(monkeypatch, tmp_path):
    from core.ops import health_check
    from core.ops.health_contract import (
        CANONICAL_HEALTH_CHECK_IDS,
        CANONICAL_HEALTH_CHECK_IDS_HASH,
    )
    from tests.unit.test_health_check_heartbeat import _config

    monkeypatch.setattr(health_check, "_safe_check", lambda _name, _func: {"status": "ok"})
    report = health_check.build_health_report(_config(tmp_path))

    assert tuple(report["health_check_ids"]) == CANONICAL_HEALTH_CHECK_IDS
    assert report["health_check_ids_hash"] == CANONICAL_HEALTH_CHECK_IDS_HASH
    assert tuple(report["checks"]) == CANONICAL_HEALTH_CHECK_IDS
    assert "agent" in report["strict_checks"]


def test_mcp_facade_health_uses_same_canonical_builder(monkeypatch):
    from core.application.facade import DefaultMnemosServiceFacade

    canonical = {
        "ok": False,
        "usable": False,
        "status": "degraded",
        "health_check_ids": ["storage", "wiki"],
        "health_check_ids_hash": "same-hash",
        "checks": {"storage": {"status": "ok"}, "wiki": {"status": "degraded"}},
    }
    monkeypatch.setattr(
        "core.ops.health_check.build_health_report_quiet",
        lambda: canonical,
    )

    result = DefaultMnemosServiceFacade().health_check()

    assert result is canonical
    assert result["health_check_ids_hash"] == "same-hash"


def test_agent_health_check_exposes_conformance_authorization_and_runtime_receipt(monkeypatch):
    from core.agent_kit.report import AgentKitAgentStatus, AgentKitReport
    from core.ops import health_check

    agent = AgentKitAgentStatus(
        name="codex",
        active_entrypoint="mcp_only",
        installed=True,
        active_ready=True,
        mcp_configured=True,
        policy_installed=True,
        passive_source_registered=True,
        passive_source_detected=True,
        content_access_authorized=False,
        authorization_state="probe_ok",
        runtime_state="missing",
        runtime_gaps=["runtime capability receipt missing"],
    )
    report = AgentKitReport(
        protocol_version="agent-kit-v2",
        target_agents=["codex"],
        workflows=[],
        agents=[agent],
        missing_workflow_tools=[],
    )
    monkeypatch.setattr(
        "core.agent_kit.build_agent_kit_report",
        lambda: report,
    )

    result = health_check._check_agents()

    assert result["status"] == "degraded"
    assert result["conformance_ok"] is True
    assert result["runtime_full_power_ok"] is False
    assert result["agents"][0]["conformance_ok"] is True
    assert result["agents"][0]["authorization_state"] == "probe_ok"
    assert result["agents"][0]["runtime_state"] == "missing"
    assert result["agents"][0]["runtime_receipt_at"] == ""
    assert result["agents"][0]["sample_completeness"] == {}
    assert result["agents"][0]["health_check_ids_hash"] == ""
    assert result["agents"][0]["runtime_canary_hash"] == ""
    assert result["agents"][0]["runtime_canary_verified"] is False
    assert result["agents"][0]["source_capture_state"] == "missing"


def test_agent_health_uses_read_only_static_probe_without_loading_providers(monkeypatch):
    from core.agent_kit.report import AgentKitReport
    from core.ops.health_agent import build_agent_health

    captured = {}

    def fake_report(**kwargs):
        captured.update(kwargs)
        return AgentKitReport(
            protocol_version="agent-kit-v2",
            target_agents=[],
            workflows=[],
            agents=[],
            missing_workflow_tools=[],
        )

    monkeypatch.setattr("core.agent_kit.build_agent_kit_report", fake_report)

    build_agent_health()

    assert captured == {
        "probe_filesystem": True,
        "load_default_providers": False,
        "isolated_default_providers": True,
    }
