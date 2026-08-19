# -*- coding: utf-8 -*-
"""Diagnostics providers owned by integration adapters."""

from __future__ import annotations

import logging
from typing import List

from core.agent_kit.evidence import agent_install_evidence
from core.diagnostics import AgentStatus, AgentStatusProvider, register_agent_status_provider

logger = logging.getLogger(__name__)


class OlympusAgentStatusProvider:
    """Report active-connection status for Olympus agent adapters."""

    def list_agent_statuses(self) -> List[AgentStatus]:
        from integrations.olympus import AgentRegistry

        try:
            adapters = AgentRegistry.discover_all()
        except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
            logger.warning("Agent 发现失败: %s", exc, exc_info=True)
            adapters = []

        results: List[AgentStatus] = []
        for adapter in adapters:
            try:
                data_dir = adapter.get_data_dir()
                results.append(
                    AgentStatus(
                        name=adapter.name,
                        available=True,
                        data_dir=str(data_dir) if data_dir else None,
                        hooks_installed=adapter.is_hooks_installed(),
                        mcp_configured=adapter.is_mcp_configured(),
                        policy_installed=adapter.is_active_policy_installed(),
                        active_ready=adapter.is_active_connection_installed(),
                        passive_source_available=False,
                    )
                )
            except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
                logger.debug("检查 Agent %s 状态失败: %s", adapter.name, exc)
                results.append(
                    AgentStatus(
                        name=adapter.name,
                        available=False,
                        passive_source_available=False,
                    )
                )
        return results


class McpOnlyAgentStatusProvider:
    """Report active-connection status for agents configured through MCP only."""

    def list_agent_statuses(self) -> List[AgentStatus]:
        from core.agent_kit.source_support_manifest import get_agent_source_support_manifest
        from core.cli.commands.mcp import _mcp_only_agent_status

        results: List[AgentStatus] = []
        for name in get_agent_source_support_manifest().mcp_only_host_names:
            try:
                status = _mcp_only_agent_status(name) or {}
                mcp_ok = bool(status.get("mcp"))
                policy_ok = bool(status.get("policy"))
                active_ready = mcp_ok and policy_ok
                installed, evidence = agent_install_evidence(name)
                results.append(
                    AgentStatus(
                        name=name,
                        available=installed,
                        data_dir=evidence,
                        hooks_installed=True,
                        mcp_configured=mcp_ok,
                        policy_installed=policy_ok,
                        active_ready=active_ready,
                        passive_source_available=False,
                    )
                )
            except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
                logger.debug("检查 MCP-only Agent %s 状态失败: %s", name, exc)
                results.append(AgentStatus(name=name, available=False))
        return results


def default_diagnostics_providers() -> list[AgentStatusProvider]:
    """Build provider instances without registering them in process-global state."""
    return [OlympusAgentStatusProvider(), McpOnlyAgentStatusProvider()]


def register_diagnostics_providers() -> None:
    """Register default providers for callers that explicitly opt into globals."""
    for provider, key in zip(
        default_diagnostics_providers(),
        ("integrations.olympus", "integrations.mcp_only"),
        strict=True,
    ):
        register_agent_status_provider(provider, key=key)
