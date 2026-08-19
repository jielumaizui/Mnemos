"""Read-only Agent Kit projection for the machine health report."""

from __future__ import annotations

from typing import Any


def build_agent_health() -> dict[str, Any]:
    from core.agent_kit import build_agent_kit_report

    try:
        report = build_agent_kit_report(
            probe_filesystem=True,
            load_default_providers=False,
            isolated_default_providers=True,
        )
    except TypeError as exc:
        if "unexpected keyword" not in str(exc):
            raise
        report = build_agent_kit_report()
    agents = [
        {
            "name": agent.name,
            "installed": agent.installed,
            "status": agent.status,
            "conformance_ok": agent.conformance_ok if agent.installed else None,
            "conformance_gaps": list(agent.full_power_gaps),
            "full_power": agent.full_power,
            "ready": agent.ready,
            "data_dir": agent.data_dir,
            "install_evidence": agent.install_evidence,
            "hooks_installed": agent.hooks_installed,
            "mcp_configured": agent.mcp_configured,
            "policy_installed": agent.policy_installed,
            "active_ready": agent.active_ready,
            "passive_source_registered": agent.passive_source_registered,
            "passive_source_detected": agent.passive_source_detected,
            "content_access_authorized": agent.content_access_authorized,
            "authorization_state": agent.authorization_state,
            "runtime_state": agent.runtime_state,
            "runtime_receipt_at": agent.runtime_receipt_at,
            "sample_completeness": dict(agent.sample_completeness),
            "health_check_ids_hash": agent.health_check_ids_hash,
            "runtime_canary_hash": agent.runtime_canary_hash,
            "runtime_canary_verified": agent.runtime_canary_verified,
            "source_capture_state": agent.source_capture_state,
            "source_capture_receipt_at": agent.source_capture_receipt_at,
            "native_source_snapshot_hash": agent.native_source_snapshot_hash,
            "source_capture_completeness": dict(agent.source_capture_completeness),
            "runtime_gaps": list(agent.runtime_gaps),
            "full_power_gaps": list(agent.full_power_gaps),
            "repair_actions": list(agent.repair_actions),
        }
        for agent in report.agents
    ]
    return {
        "status": "ok" if report.full_power_ok else "degraded",
        "count": len(agents),
        "installed_count": len(report.installed_agents),
        "conformant_count": len(report.conformant_agents),
        "full_power_count": len(report.full_power_agents),
        "conformance_ok": report.conformance_ok,
        "runtime_full_power_ok": report.runtime_full_power_ok,
        "nonconformant_agents": report.nonconformant_agents,
        "runtime_unverified_agents": report.runtime_unverified_agents,
        "degraded_agents": report.degraded_agents,
        "workflow_contract_ok": report.workflow_contract_ok,
        "agents": agents,
    }
