"""Shared Mnemos Agent Kit protocol.

This module is intentionally small and dependency-free. It defines the common
agent names and the workflow contract that every host agent should reach through
its local adapter, MCP config, or active policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Tuple

from core.agent_kit.source_support_manifest import get_agent_source_support_manifest


_MANIFEST = get_agent_source_support_manifest()

TARGET_AGENT_NAMES = _MANIFEST.host_agent_names

CONTEXT_SHARE_AGENT_NAMES: Tuple[str, ...] = TARGET_AGENT_NAMES

AGENT_ALIASES: Mapping[str, str] = _MANIFEST.aliases

ACTIVE_ENTRYPOINTS: Mapping[str, str] = _MANIFEST.active_entrypoints

# Mnemos is a cognitive system, so "full power" means the agent can feed the
# cognitive loop with evidence, not just connect to an MCP server.  Tool calls
# and tool results are required for every supported coding agent; reasoning and
# attachments are required only where the current local/official surfaces expose
# them as durable evidence.
CORE_COGNITIVE_CAPABILITIES = _MANIFEST.host_core_cognitive_capabilities

AGENT_COGNITIVE_CAPABILITIES: Mapping[str, Tuple[str, ...]] = (
    _MANIFEST.agent_cognitive_capabilities
)


@dataclass(frozen=True)
class WorkflowContract:
    """One required Mnemos workflow exposed to host agents."""

    name: str
    phase: str
    mcp_tool: str
    required: bool
    purpose: str


WORKFLOW_CONTRACTS: Tuple[WorkflowContract, ...] = (
    WorkflowContract(
        name="preflight",
        phase="start",
        mcp_tool="preflight_inject",
        required=True,
        purpose="Inject task-scoped retrospective and wiki context before planning.",
    ),
    WorkflowContract(
        name="context_search",
        phase="running",
        mcp_tool="context_aware_search",
        required=True,
        purpose="Retrieve durable knowledge with agent/project/session access controls.",
    ),
    WorkflowContract(
        name="cognitive_state",
        phase="running",
        mcp_tool="build_cognitive_state",
        required=True,
        purpose="Read the canonical, ACL-filtered cognitive state without writing it.",
    ),
    WorkflowContract(
        name="decision_record",
        phase="decision",
        mcp_tool="record_decision",
        required=True,
        purpose="Seal a material decision only with exact source-authority evidence.",
    ),
    WorkflowContract(
        name="guard",
        phase="running",
        mcp_tool="guard_check",
        required=True,
        purpose="Detect repeated work, unsafe behavior, and known operational risks.",
    ),
    WorkflowContract(
        name="capture_turn",
        phase="final",
        mcp_tool="capture_turn",
        required=True,
        purpose="Persist a lightweight turn record into the Mnemos capture queue.",
    ),
    WorkflowContract(
        name="predictive_delivery",
        phase="delivery",
        mcp_tool="predictive_push",
        required=True,
        purpose="Request a governed predictive-delivery decision from the sole router.",
    ),
    WorkflowContract(
        name="display_ack",
        phase="delivery",
        mcp_tool="delivery_display_ack",
        required=True,
        purpose="Record a host-bound presentation receipt only after rendering a delivered item.",
    ),
    WorkflowContract(
        name="outcome",
        phase="outcome",
        mcp_tool="apply_outcome",
        required=True,
        purpose="Admit an independently evidenced outcome for a canonical prediction.",
    ),
    WorkflowContract(
        name="correction",
        phase="outcome",
        mcp_tool="push_feedback",
        required=True,
        purpose="Record a typed reaction or correction against a displayed predictive delivery.",
    ),
    WorkflowContract(
        name="recap_check",
        phase="start_or_final",
        mcp_tool="check_pending_recaps",
        required=True,
        purpose="Surface urgent retrospective work and follow-up reminders.",
    ),
    WorkflowContract(
        name="health_check",
        phase="diagnostic",
        mcp_tool="health_check",
        required=True,
        purpose="Verify the local Mnemos runtime and storage backend are reachable.",
    ),
    WorkflowContract(
        name="runtime_probe",
        phase="diagnostic",
        mcp_tool="agent_runtime_probe",
        required=True,
        purpose=(
            "Record an authorized synthetic-safe MCP roundtrip and completeness receipt."
        ),
    ),
)


def normalize_agent_name(name: str) -> str:
    """Normalize public aliases to Mnemos source_agent names."""
    normalized = name.strip().lower().replace(" ", "-")
    return AGENT_ALIASES.get(normalized, normalized)


def required_workflow_tool_names() -> Tuple[str, ...]:
    """Return MCP tool names that must exist for the shared workflow contract."""
    return tuple(w.mcp_tool for w in WORKFLOW_CONTRACTS if w.required)


def required_cognitive_capabilities(agent: str) -> Tuple[str, ...]:
    """Return passive evidence capabilities required for full-power status."""
    normalized = normalize_agent_name(agent)
    return CORE_COGNITIVE_CAPABILITIES + AGENT_COGNITIVE_CAPABILITIES.get(
        normalized, ()
    )
