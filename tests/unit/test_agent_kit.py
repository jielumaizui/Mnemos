from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest


def test_agent_kit_protocol_targets_all_current_agents():
    from core.agent_kit.protocol import (
        TARGET_AGENT_NAMES,
        required_cognitive_capabilities,
        required_workflow_tool_names,
    )

    assert TARGET_AGENT_NAMES == (
        "codex",
        "claude",
        "hermes",
        "opencode",
        "openclaw",
        "crush",
        "kiro",
        "kimi",
    )
    assert set(required_workflow_tool_names()) == {
        "preflight_inject",
        "context_aware_search",
        "build_cognitive_state",
        "record_decision",
        "apply_outcome",
        "guard_check",
        "capture_turn",
        "predictive_push",
        "delivery_display_ack",
        "push_feedback",
        "check_pending_recaps",
        "health_check",
        "agent_runtime_probe",
    }
    assert required_cognitive_capabilities("kiro") == (
        "visible_text",
        "tool_calls",
        "tool_results",
        "source_fidelity",
        "reasoning",
        "attachments",
    )


def test_agent_kit_report_is_json_serializable_without_filesystem_probe():
    from core.agent_kit.report import build_agent_kit_report

    report = build_agent_kit_report(
        ["claude-code", "kiro"],
        probe_filesystem=False,
        load_default_providers=False,
    )
    data = report.to_dict()

    assert data["protocol_version"] == "agent-kit-v2"
    assert data["target_agents"] == ["claude", "kiro"]
    assert data["workflow_contract_ok"] is True
    assert data["runtime_probe_contract"]["tool"] == "agent_runtime_probe"
    assert {a["name"] for a in data["agents"]} == {"claude", "kiro"}
    for agent in data["agents"]:
        assert agent["content_access_authorized"] is False
        assert agent["authorization_state"] == "detected"
        caps = agent["source_capabilities"]
        assert "memory_scope" in caps
        assert "host_memory_effect" in caps
        assert "transcript_kind" in caps
        assert "compression" in caps
        assert "dedupe_strategy" in caps
    json.dumps(data, ensure_ascii=False)


def test_agent_kit_marks_kiro_as_mcp_only_with_passive_source(monkeypatch):
    from core.agent_kit.report import build_agent_kit_report

    monkeypatch.setattr("core.agent_kit.report._active_status_by_agent", lambda _load: {})
    report = build_agent_kit_report(
        ["kiro"],
        probe_filesystem=False,
        load_default_providers=False,
    )
    agent = report.agents[0]

    assert agent.name == "kiro"
    assert agent.active_entrypoint == "mcp_only"
    assert agent.passive_source_registered is True
    assert agent.passive_source_detected is False
    assert agent.ready is False
    assert agent.status == "not_installed"
    assert agent.gaps == []


def test_agent_kit_marks_kiro_active_ready_when_mcp_only_status_exists(monkeypatch):
    from core.diagnostics import AgentStatus
    from core.agent_kit.report import build_agent_kit_report

    monkeypatch.setattr(
        "core.agent_kit.report.agent_install_evidence",
        lambda name: (name == "kiro", "/fake/kiro"),
    )
    monkeypatch.setattr(
        "core.agent_kit.report._active_status_by_agent",
        lambda _load_default_providers: {
            "kiro": AgentStatus(
                name="kiro",
                hooks_installed=True,
                mcp_configured=True,
                policy_installed=True,
                active_ready=True,
                available=True,
            )
        },
    )
    monkeypatch.setattr(
        "core.agent_kit.report._passive_source_details",
        lambda agent, **kw: {
            "registered": True,
            "detected": True,
            "data_dir": "/fake/kiro/sessions/cli",
            "capabilities": {
                "visible_text": True,
                "tool_calls": True,
                "tool_results": True,
                "reasoning": True,
                "attachments": "available",
                "source_fidelity": True,
            },
        },
    )

    report = build_agent_kit_report(
        ["kiro"],
        probe_filesystem=True,
        load_default_providers=False,
    )
    agent = report.agents[0]

    assert agent.active_entrypoint == "mcp_only"
    assert agent.mcp_configured is True
    assert agent.policy_installed is True
    assert agent.active_ready is True
    assert agent.ready is True
    assert agent.conformance_ok is True
    assert agent.full_power is False
    assert agent.authorization_state == "probe_ok"
    assert agent.content_access_authorized is False
    assert agent.gaps == []
    assert agent.full_power_gaps == []
    assert report.conformance_ok is True
    assert report.full_power_ok is False


def test_mcp_only_agent_does_not_report_hooks_installed(monkeypatch):
    from core.diagnostics import AgentStatus
    from core.agent_kit.report import build_agent_kit_report

    monkeypatch.setattr(
        "core.agent_kit.report.agent_install_evidence",
        lambda name: (name == "kiro", "/fake/kiro"),
    )
    monkeypatch.setattr(
        "core.agent_kit.report._active_status_by_agent",
        lambda _load_default_providers: {
            "kiro": AgentStatus(
                name="kiro",
                hooks_installed=True,
                mcp_configured=True,
                policy_installed=True,
                active_ready=True,
                available=True,
            )
        },
    )
    monkeypatch.setattr(
        "core.agent_kit.report._passive_source_details",
        lambda agent, **kw: {
            "registered": True,
            "detected": True,
            "data_dir": "/fake/kiro",
            "capabilities": {
                "visible_text": True,
                "tool_calls": True,
                "tool_results": True,
                "reasoning": True,
                "attachments": True,
                "source_fidelity": True,
            },
        },
    )

    report = build_agent_kit_report(
        ["kiro"], probe_filesystem=True, load_default_providers=False
    )

    assert report.agents[0].active_entrypoint == "mcp_only"
    assert report.agents[0].hooks_installed is False


def test_passive_detection_requires_install_or_real_session(monkeypatch, tmp_path):
    from core.agent_kit import report as report_module

    class EmptySource:
        data_dir = tmp_path

        def completeness_capabilities(self):
            return {"source_fidelity": True}

        def discover_sessions(self):
            return []

    monkeypatch.setattr(
        "core.sync_framework.registry.SourceRegistry.get_builtin_source_class",
        lambda _agent: EmptySource,
    )
    monkeypatch.setattr(report_module, "agent_install_evidence", lambda _agent: (False, None))

    details = report_module._passive_source_details("codex", probe_filesystem=True)

    assert details["data_dir"] == str(tmp_path)
    assert details["detected"] is False


def test_passive_detection_accepts_read_only_session_discovery(monkeypatch, tmp_path):
    from core.agent_kit import report as report_module

    class SessionSource:
        data_dir = tmp_path

        def completeness_capabilities(self):
            return {"source_fidelity": True}

        def discover_sessions(self):
            return [object()]

    monkeypatch.setattr(
        "core.sync_framework.registry.SourceRegistry.get_builtin_source_class",
        lambda _agent: SessionSource,
    )
    monkeypatch.setattr(report_module, "agent_install_evidence", lambda _agent: (False, None))

    details = report_module._passive_source_details("codex", probe_filesystem=True)

    assert details["detected"] is True


def test_mcp_only_status_provider_reports_kiro_active(monkeypatch, tmp_path: Path):
    from core.agent_kit.authorization import (
        AgentAuthorizationStore,
        InMemoryMCPLaunchCredentialStore,
    )
    from integrations import active
    from integrations.diagnostics_provider import McpOnlyAgentStatusProvider

    monkeypatch.setenv("HOME", str(tmp_path))
    launch_secrets = InMemoryMCPLaunchCredentialStore()
    authorization_store = AgentAuthorizationStore(tmp_path / "agent_authorization.db")
    monkeypatch.setattr(
        active,
        "MCPLaunchCredentialStore",
        lambda: launch_secrets,
    )
    monkeypatch.setattr(
        active,
        "AgentAuthorizationStore",
        lambda *args, **kwargs: authorization_store,
    )
    monkeypatch.setattr(
        "integrations.diagnostics_provider.agent_install_evidence",
        lambda name: (name == "kiro", "/fake/kiro-cli" if name == "kiro" else None),
    )
    kiro_dir = tmp_path / ".kiro"
    active.upsert_kiro_mcp_server(kiro_dir / "settings" / "mcp.json")
    active.upsert_marked_block(
        kiro_dir / "MNEMOS_ACTIVE.md",
        active.active_policy_text("kiro"),
    )

    statuses = McpOnlyAgentStatusProvider().list_agent_statuses()
    kiro = next(status for status in statuses if status.name == "kiro")

    assert kiro.mcp_configured is True
    assert kiro.policy_installed is True
    assert kiro.active_ready is True
    assert kiro.available is True


def test_mcp_only_status_provider_does_not_promote_adapter_host_to_mcp_only():
    from integrations.diagnostics_provider import McpOnlyAgentStatusProvider

    statuses = McpOnlyAgentStatusProvider().list_agent_statuses()

    assert "crush" not in {status.name for status in statuses}


def test_agent_kit_does_not_treat_precreated_mcp_config_as_installed(monkeypatch):
    from core.diagnostics import AgentStatus
    from core.agent_kit.protocol import TARGET_AGENT_NAMES
    from core.agent_kit.report import build_agent_kit_report

    monkeypatch.setattr(
        "core.agent_kit.report.agent_install_evidence",
        lambda name: (False, None),
    )
    monkeypatch.setattr(
        "core.agent_kit.report._active_status_by_agent",
        lambda _load_default_providers: {
            "kiro": AgentStatus(
                name="kiro",
                hooks_installed=True,
                mcp_configured=True,
                policy_installed=True,
                active_ready=True,
                available=False,
            )
        },
    )
    monkeypatch.setattr(
        "core.agent_kit.report._passive_source_details",
        lambda agent, **kw: {
            "registered": True,
            "detected": False,
            "data_dir": None,
            "capabilities": {"visible_text": True, "source_fidelity": True},
        },
    )

    report = build_agent_kit_report(["kiro"], probe_filesystem=True, load_default_providers=False)
    agent = report.agents[0]

    assert agent.installed is False
    assert agent.ready is False
    assert agent.full_power is False
    assert agent.status == "not_installed"
    assert report.degraded_agents == []
    assert report.full_power_ok is False
    assert report.target_agent_coverage_ok is False
    assert report.runtime_unverified_agents == list(TARGET_AGENT_NAMES)


def test_agent_install_evidence_ignores_crush_mcp_config_only(monkeypatch, tmp_path: Path):
    from core.agent_kit.evidence import agent_install_evidence

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("core.agent_kit.evidence._agent_home", lambda: tmp_path)
    monkeypatch.setattr("core.agent_kit.evidence.shutil.which", lambda name: None)
    config_path = tmp_path / ".config" / "crush" / "crush.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{}", encoding="utf-8")

    assert agent_install_evidence("crush") == (False, None)

    db_path = tmp_path / ".crush" / "crush.db"
    db_path.parent.mkdir(parents=True)
    db_path.write_text("", encoding="utf-8")

    assert agent_install_evidence("crush") == (True, str(db_path))


def test_agent_install_evidence_detects_kimi_code_home(monkeypatch, tmp_path: Path):
    from core.agent_kit.evidence import agent_install_evidence

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("core.agent_kit.evidence.shutil.which", lambda name: None)
    kimi_code = tmp_path / ".kimi-code"
    kimi_code.mkdir()

    assert agent_install_evidence("kimi") == (True, str(kimi_code))


def test_agent_install_evidence_does_not_treat_uninspectable_path_as_absent(
    monkeypatch,
    tmp_path: Path,
):
    from core.agent_kit.evidence import agent_install_evidence
    from core.ops.durable_io import DurableIOError

    target = tmp_path / ".codex"
    original_stat = Path.stat

    def denied(path: Path, *args: object, **kwargs: object):
        if path == target:
            raise PermissionError("sentinel")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr("core.agent_kit.evidence._agent_home", lambda: tmp_path)
    monkeypatch.setattr("core.agent_kit.evidence.shutil.which", lambda _name: None)
    monkeypatch.setattr(
        "core.agent_kit.evidence.expand_path_templates",
        lambda *_args, **_kwargs: [target],
    )
    monkeypatch.setattr(Path, "stat", denied)

    with pytest.raises(DurableIOError, match="durable_path_inspection_failed"):
        agent_install_evidence("codex")


def test_agent_kit_marks_installed_agent_degraded_when_source_is_derived(monkeypatch):
    from core.diagnostics import AgentStatus
    from core.agent_kit.report import build_agent_kit_report

    monkeypatch.setattr(
        "core.agent_kit.report.agent_install_evidence",
        lambda name: (name == "openclaw", "/fake/openclaw"),
    )
    monkeypatch.setattr(
        "core.agent_kit.report._active_status_by_agent",
        lambda _load_default_providers: {
            "openclaw": AgentStatus(
                name="openclaw",
                hooks_installed=True,
                mcp_configured=True,
                policy_installed=True,
                active_ready=True,
                available=True,
            )
        },
    )
    monkeypatch.setattr(
        "core.agent_kit.report._passive_source_details",
        lambda agent, **kw: {
            "registered": True,
            "detected": True,
            "data_dir": "/fake/openclaw",
            "capabilities": {
                "visible_text": True,
                "tool_calls": False,
                "tool_results": False,
                "reasoning": False,
                "source_fidelity": "derived",
            },
        },
    )

    report = build_agent_kit_report(
        ["openclaw"], probe_filesystem=True, load_default_providers=False
    )
    agent = report.agents[0]

    assert agent.installed is True
    assert agent.ready is True
    assert agent.full_power is False
    assert agent.status == "degraded"
    assert report.full_power_ok is False
    assert report.degraded_agents == ["openclaw"]
    assert any("source fidelity" in gap for gap in agent.full_power_gaps)


def test_agent_kit_marks_tool_capable_agent_degraded_without_tool_evidence(monkeypatch):
    from core.diagnostics import AgentStatus
    from core.agent_kit.report import build_agent_kit_report

    monkeypatch.setattr(
        "core.agent_kit.report.agent_install_evidence",
        lambda name: (name == "hermes", "/fake/hermes"),
    )
    monkeypatch.setattr(
        "core.agent_kit.report._active_status_by_agent",
        lambda _load_default_providers: {
            "hermes": AgentStatus(
                name="hermes",
                hooks_installed=True,
                mcp_configured=True,
                policy_installed=True,
                active_ready=True,
                available=True,
            )
        },
    )
    monkeypatch.setattr(
        "core.agent_kit.report._passive_source_details",
        lambda agent, **kw: {
            "registered": True,
            "detected": True,
            "data_dir": "/fake/hermes",
            "capabilities": {
                "visible_text": True,
                "tool_calls": False,
                "tool_results": False,
                "reasoning": True,
                "source_fidelity": True,
            },
        },
    )

    report = build_agent_kit_report(
        ["hermes"], probe_filesystem=True, load_default_providers=False
    )
    agent = report.agents[0]

    assert agent.status == "degraded"
    assert "tool_calls" in "\n".join(agent.full_power_gaps)
    assert "tool_results" in "\n".join(agent.full_power_gaps)


def test_agent_kit_cli_json(monkeypatch, capsys):
    import mnemos_cli

    monkeypatch.setattr(
        "core.agent_kit.report._safe_active_adapter_names",
        lambda: set(),
    )
    args = argparse.Namespace(
        agent_cmd="kit",
        agent_name="kiro",
        json=True,
        no_probe=True,
    )

    mnemos_cli.cmd_agent(args)
    output = json.loads(capsys.readouterr().out)

    assert output["target_agents"] == ["kiro"]
    assert output["agents"][0]["active_entrypoint"] == "mcp_only"


def test_path_discover_has_kiro_slot(monkeypatch, tmp_path: Path):
    from core.sync_framework.registry import PathDiscover

    monkeypatch.setenv("HOME", str(tmp_path))
    kiro_dir = tmp_path / ".kiro"
    kiro_dir.mkdir()
    PathDiscover.invalidate_cache()

    assert PathDiscover.find("kiro") == kiro_dir


def test_agent_kit_report_does_not_register_builtin_sources():
    from core.agent_kit.report import build_agent_kit_report
    from core.sync_framework.registry import SourceRegistry

    before = set(SourceRegistry.list_registered())
    build_agent_kit_report(
        ["codex", "kiro"],
        probe_filesystem=False,
        load_default_providers=False,
    )
    after = set(SourceRegistry.list_registered())

    assert after == before
