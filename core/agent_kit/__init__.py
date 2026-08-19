"""Mnemos Agent Kit.

Agent-agnostic protocol and conformance helpers for every host agent that
connects to Mnemos.
"""

from core.agent_kit.protocol import (
    CONTEXT_SHARE_AGENT_NAMES,
    TARGET_AGENT_NAMES,
    WORKFLOW_CONTRACTS,
    WorkflowContract,
    normalize_agent_name,
)
from core.agent_kit.agent_backend import AgentBackendConfig, AgentBackendResult, CLIAgentBackend
from core.agent_kit.prompt_sanitizer import (
    PromptSanitizer,
    PromptSanitizerAuditStore,
    PromptSanitizerFinding,
    PromptSanitizerResult,
)
from core.agent_kit.report import build_agent_kit_report
from core.agent_kit.shadow_eval import (
    AgentShadowConfig,
    AgentShadowConfigStore,
    run_agent_shadow_eval,
)

__all__ = [
    "AgentBackendConfig",
    "AgentBackendResult",
    "AgentShadowConfig",
    "AgentShadowConfigStore",
    "CLIAgentBackend",
    "CONTEXT_SHARE_AGENT_NAMES",
    "PromptSanitizer",
    "PromptSanitizerAuditStore",
    "PromptSanitizerFinding",
    "PromptSanitizerResult",
    "TARGET_AGENT_NAMES",
    "WORKFLOW_CONTRACTS",
    "WorkflowContract",
    "build_agent_kit_report",
    "normalize_agent_name",
    "run_agent_shadow_eval",
]
