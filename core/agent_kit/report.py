"""Agent Kit conformance reporting."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set

from core.agent_kit.evidence import agent_install_evidence
from core.agent_kit.protocol import (
    ACTIVE_ENTRYPOINTS,
    TARGET_AGENT_NAMES,
    WORKFLOW_CONTRACTS,
    normalize_agent_name,
    required_cognitive_capabilities,
    required_workflow_tool_names,
)
from core.agent_kit.source_support_manifest import get_agent_source_support_manifest
from core.ops.durable_io import inspect_path_kind

_SAFE_RUNTIME_ERRORS = (
    OSError,
    ValueError,
    TypeError,
    KeyError,
    ImportError,
    AttributeError,
    RuntimeError,
)
_ACTIVE_STATUS_STATE_KEY = "__mnemos_active_status_state__"
_ACTIVE_STATUS_ERROR_KEY = "__mnemos_active_status_error__"


class AgentKitInventoryUnavailableError(RuntimeError):
    """A static Agent Kit registry could not be enumerated safely."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


@dataclass
class AgentKitWorkflowStatus:
    """Runtime status for one required workflow."""

    name: str
    phase: str
    mcp_tool: str
    exposed: bool
    purpose: str


@dataclass
class AgentKitAgentStatus:
    """Conformance status for one target agent."""

    name: str
    active_entrypoint: str
    installed: bool = False
    install_evidence: Optional[str] = None
    active_adapter_registered: bool = False
    active_ready: bool = False
    active_runtime_state: str = "unknown"
    active_runtime_error_code: str = ""
    hooks_installed: bool = False
    mcp_configured: bool = False
    policy_installed: bool = False
    passive_source_registered: bool = False
    passive_source_detected: bool = False
    passive_source_state: str = "unknown"
    passive_source_error_code: str = ""
    path_detected: bool = False
    content_access_authorized: bool = False
    authorization_state: str = "detected"
    data_dir: Optional[str] = None
    required_capabilities: List[str] = field(default_factory=list)
    source_capabilities: Dict[str, Any] = field(default_factory=dict)
    gaps: List[str] = field(default_factory=list)
    full_power_gaps: List[str] = field(default_factory=list)
    runtime_state: str = "unknown"
    runtime_receipt_at: str = ""
    sample_completeness: Dict[str, Any] = field(default_factory=dict)
    health_check_ids_hash: str = ""
    support_manifest_hash: str = ""
    runtime_canary_hash: str = ""
    runtime_canary_verified: bool = False
    source_capture_state: str = "missing"
    source_capture_receipt_at: str = ""
    native_source_snapshot_hash: str = ""
    source_capture_completeness: Dict[str, Any] = field(default_factory=dict)
    discovery_covered: bool = False
    content_parsed: bool = False
    raw_committed: bool = False
    runtime_gaps: List[str] = field(default_factory=list)
    repair_actions: List[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """An agent is usable when either active or passive integration is present."""
        if not self.installed:
            return False
        usable_passive = self.passive_source_registered and self.passive_source_detected
        return self.active_ready or usable_passive

    @property
    def full_power(self) -> bool:
        """True only after static conformance and a current runtime receipt."""
        return (
            self.conformance_ok
            and self.content_access_authorized
            and self.runtime_state == "verified"
            and self.source_capture_state == "verified"
            and self.discovery_covered
            and self.content_parsed
            and self.raw_committed
            and self.runtime_canary_verified
            and not self.runtime_gaps
        )

    @property
    def conformance_ok(self) -> bool:
        """Whether an installed agent satisfies the static integration contract."""
        return self.installed and not self.full_power_gaps

    @property
    def status(self) -> str:
        if not self.installed:
            return "not_installed"
        if self.full_power:
            return "full_power"
        if self.conformance_ok:
            return "runtime_unverified"
        if self.ready:
            return "degraded"
        return "not_ready"

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["verification_layers"] = {
            "installed": self.installed,
            "path_detected": self.path_detected,
            "discovery_covered": self.discovery_covered,
            "content_parsed": self.content_parsed,
            "raw_committed": self.raw_committed,
            "runtime_verified": self.runtime_state == "verified",
            "runtime_canary_verified": self.runtime_canary_verified,
        }
        data["ready"] = self.ready
        data["conformance_ok"] = self.conformance_ok if self.installed else None
        data["conformance_gaps"] = list(self.full_power_gaps)
        data["full_power"] = self.full_power
        data["status"] = self.status
        return data


@dataclass
class IngestionSourceStatus:
    """Raw-ingestion status for a source that is deliberately not a host."""

    name: str
    role: str = "ingestion_only"
    installed: bool = False
    install_evidence: Optional[str] = None
    passive_source_registered: bool = False
    passive_source_detected: bool = False
    passive_source_state: str = "unknown"
    passive_source_error_code: str = ""
    content_access_authorized: bool = False
    authorization_state: str = "detected"
    data_dir: Optional[str] = None
    source_capabilities: Dict[str, Any] = field(default_factory=dict)
    raw_contract: Dict[str, Any] = field(default_factory=dict)
    gaps: List[str] = field(default_factory=list)

    @property
    def full_power(self) -> bool:
        """Ingestion-only sources never participate in the host denominator."""
        return False

    @property
    def raw_contract_ready(self) -> bool:
        if not self.installed:
            return True
        return not self.gaps

    @property
    def status(self) -> str:
        if not self.installed:
            return "not_installed"
        if self.raw_contract_ready:
            return "raw_ready"
        if not self.content_access_authorized:
            return "authorization_required"
        return "raw_degraded"

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["full_power"] = False
        data["raw_contract_ready"] = self.raw_contract_ready
        data["status"] = self.status
        return data


@dataclass
class AgentKitReport:
    """Full Mnemos Agent Kit conformance report."""

    protocol_version: str
    target_agents: List[str]
    workflows: List[AgentKitWorkflowStatus]
    agents: List[AgentKitAgentStatus]
    missing_workflow_tools: List[str]
    workflow_tool_state: str = "available"
    workflow_tool_error_code: str = ""
    active_adapter_registry_state: str = "unknown"
    active_adapter_registry_error_code: str = ""
    ingestion_sources: List[IngestionSourceStatus] = field(default_factory=list)
    support_manifest_hash: str = ""

    @property
    def workflow_contract_ok(self) -> bool:
        return (
            self.workflow_tool_state == "available"
            and not self.missing_workflow_tools
        )

    @property
    def ready_agents(self) -> List[str]:
        return [a.name for a in self.agents if a.ready]

    @property
    def installed_agents(self) -> List[str]:
        return [a.name for a in self.agents if a.installed]

    @property
    def full_power_agents(self) -> List[str]:
        return [a.name for a in self.agents if a.full_power]

    @property
    def conformant_agents(self) -> List[str]:
        return [a.name for a in self.agents if a.conformance_ok]

    @property
    def nonconformant_agents(self) -> List[str]:
        return [a.name for a in self.agents if a.installed and not a.conformance_ok]

    @property
    def degraded_agents(self) -> List[str]:
        return [a.name for a in self.agents if a.installed and not a.full_power]

    @property
    def runtime_unverified_agents(self) -> List[str]:
        verified = set(self.full_power_agents)
        return [name for name in TARGET_AGENT_NAMES if name not in verified]

    @property
    def selected_runtime_unverified_agents(self) -> List[str]:
        return [status.name for status in self.agents if not status.full_power]

    @property
    def missing_target_agents(self) -> List[str]:
        observed = {status.name for status in self.agents}
        return [name for name in TARGET_AGENT_NAMES if name not in observed]

    @property
    def target_agent_coverage_ok(self) -> bool:
        expected = list(TARGET_AGENT_NAMES)
        observed_statuses = [status.name for status in self.agents]
        return (
            len(self.target_agents) == len(expected)
            and len(observed_statuses) == len(expected)
            and set(self.target_agents) == set(expected)
            and set(observed_statuses) == set(expected)
        )

    @property
    def selected_target_coverage_ok(self) -> bool:
        requested = list(self.target_agents)
        observed = [status.name for status in self.agents]
        return (
            len(requested) == len(observed)
            and len(requested) == len(set(requested))
            and set(requested) == set(observed)
        )

    @property
    def conformance_ok(self) -> bool:
        return self.workflow_contract_ok and not self.nonconformant_agents

    @property
    def full_power_ok(self) -> bool:
        return (
            self.workflow_contract_ok
            and self.target_agent_coverage_ok
            and not self.runtime_unverified_agents
        )

    @property
    def runtime_full_power_ok(self) -> bool:
        return self.full_power_ok

    @property
    def selected_target_full_power_ok(self) -> bool:
        return (
            self.workflow_contract_ok
            and self.selected_target_coverage_ok
            and not self.selected_runtime_unverified_agents
        )

    @property
    def ingestion_contract_ok(self) -> bool:
        return all(source.raw_contract_ready for source in self.ingestion_sources)

    @property
    def source_support_ok(self) -> bool:
        return self.conformance_ok and self.ingestion_contract_ok

    def to_dict(self) -> Dict[str, Any]:
        from core.agent_kit.runtime_receipts import runtime_probe_contract

        return {
            "protocol_version": self.protocol_version,
            "workflow_contract_ok": self.workflow_contract_ok,
            "workflow_tool_state": self.workflow_tool_state,
            "workflow_tool_error_code": self.workflow_tool_error_code,
            "active_adapter_registry_state": self.active_adapter_registry_state,
            "active_adapter_registry_error_code": (
                self.active_adapter_registry_error_code
            ),
            "conformance_ok": self.conformance_ok,
            "full_power_ok": self.full_power_ok,
            "runtime_full_power_ok": self.runtime_full_power_ok,
            "source_support_ok": self.source_support_ok,
            "ingestion_contract_ok": self.ingestion_contract_ok,
            "support_manifest_hash": self.support_manifest_hash,
            "target_agents": self.target_agents,
            "installed_agents": self.installed_agents,
            "ready_agents": self.ready_agents,
            "conformant_agents": self.conformant_agents,
            "nonconformant_agents": self.nonconformant_agents,
            "full_power_agents": self.full_power_agents,
            "degraded_agents": self.degraded_agents,
            "runtime_unverified_agents": self.runtime_unverified_agents,
            "selected_runtime_unverified_agents": (
                self.selected_runtime_unverified_agents
            ),
            "missing_target_agents": self.missing_target_agents,
            "target_agent_coverage_ok": self.target_agent_coverage_ok,
            "selected_target_coverage_ok": self.selected_target_coverage_ok,
            "selected_target_full_power_ok": self.selected_target_full_power_ok,
            "missing_workflow_tools": list(self.missing_workflow_tools),
            "workflows": [asdict(w) for w in self.workflows],
            "agents": [a.to_dict() for a in self.agents],
            "ingestion_sources": [source.to_dict() for source in self.ingestion_sources],
            "runtime_probe_contract": runtime_probe_contract(),
        }


def _safe_mcp_tool_names() -> Set[str]:
    try:
        from integrations.agora_tools.schema import list_tools

        payload = list_tools(lambda name: "agent_kit")
        if not isinstance(payload, dict) or not isinstance(payload.get("tools"), list):
            raise AgentKitInventoryUnavailableError(
                "agent_kit_workflow_tool_inventory_unavailable"
            )
        tools = payload["tools"]
        if any(
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not item["name"]
            for item in tools
        ):
            raise AgentKitInventoryUnavailableError(
                "agent_kit_workflow_tool_inventory_unavailable"
            )
        return {str(item["name"]) for item in tools}
    except AgentKitInventoryUnavailableError:
        raise
    except _SAFE_RUNTIME_ERRORS:
        raise AgentKitInventoryUnavailableError(
            "agent_kit_workflow_tool_inventory_unavailable"
        ) from None


def _safe_active_adapter_names() -> Set[str]:
    try:
        from integrations.olympus import AgentRegistry

        AgentRegistry._ensure_adapters_loaded()  # type: ignore[attr-defined]
        adapter_classes = getattr(AgentRegistry, "_adapters", None)
        if not isinstance(adapter_classes, list):
            raise AgentKitInventoryUnavailableError(
                "agent_kit_active_adapter_registry_unavailable"
            )
        names = set()
        for adapter_class in adapter_classes:
            try:
                names.add(normalize_agent_name(adapter_class().name))
            except _SAFE_RUNTIME_ERRORS:
                raise AgentKitInventoryUnavailableError(
                    "agent_kit_active_adapter_registry_unavailable"
                ) from None
        return names
    except AgentKitInventoryUnavailableError:
        raise
    except _SAFE_RUNTIME_ERRORS:
        raise AgentKitInventoryUnavailableError(
            "agent_kit_active_adapter_registry_unavailable"
        ) from None


def _active_status_by_agent(
    load_default_providers: bool,
    *,
    isolated_default_providers: bool = False,
) -> Dict[str, Any]:
    try:
        from core.diagnostics import ConnectionDiagnostics

        statuses = ConnectionDiagnostics.check_agents(
            load_default_providers=load_default_providers,
            isolated_default_providers=isolated_default_providers,
        )
    except _SAFE_RUNTIME_ERRORS:
        return {
            _ACTIVE_STATUS_STATE_KEY: "unavailable",
            _ACTIVE_STATUS_ERROR_KEY: "active_diagnostics_unavailable",
        }

    result: Dict[str, Any] = {
        _ACTIVE_STATUS_STATE_KEY: (
            "available"
            if load_default_providers or isolated_default_providers
            else "unprobed"
        ),
        _ACTIVE_STATUS_ERROR_KEY: "",
    }
    for status in statuses:
        name = normalize_agent_name(getattr(status, "name", ""))
        if name and name not in result:
            result[name] = status
    return result


def _passive_registered_names() -> Set[str]:
    try:
        from core.sync_framework.registry import SourceRegistry

        names = set(SourceRegistry.list_registered())
        names.update(SourceRegistry.list_builtin_agent_names())
        return {normalize_agent_name(n) for n in names}
    except _SAFE_RUNTIME_ERRORS:
        return set()


def _source_fidelity_is_full(value: Any) -> bool:
    return value is True or str(value).lower() == "full"


def _capability_is_available(name: str, value: Any) -> bool:
    if name == "source_fidelity":
        return _source_fidelity_is_full(value)
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {
        "",
        "0",
        "false",
        "none",
        "null",
        "unknown",
        "unavailable",
        "not_available",
    }:
        return False
    return True


def _passive_source_details(agent: str, *, probe_filesystem: bool) -> Dict[str, Any]:
    details: Dict[str, Any] = {
        "registered": False,
        "detected": False,
        "path_detected": False,
        "discovered_session_count": 0,
        "data_dir": None,
        "capabilities": {},
        "state": "unprobed" if not probe_filesystem else "unknown",
        "error_code": "",
    }
    try:
        from core.sync_framework.registry import SourceRegistry

        details["registered"] = agent in set(SourceRegistry.list_builtin_agent_names()) or agent in set(
            SourceRegistry.list_registered()
        )
        source_class = SourceRegistry.get_builtin_source_class(agent)
        if source_class is None:
            details["state"] = "absent"
            return details
        source = source_class()
        details["capabilities"] = source.completeness_capabilities()
        if not probe_filesystem:
            return details
        data_dir = source.data_dir
        if data_dir is None:
            from core.sync_framework.registry import PathDiscover

            data_dir = PathDiscover.find(agent)
        if data_dir is not None:
            details["data_dir"] = str(data_dir)
            details["path_detected"] = inspect_path_kind(data_dir) != "missing"
        details["state"] = (
            "available" if details["path_detected"] else "absent"
        )
        try:
            sessions = source.discover_sessions()
        except _SAFE_RUNTIME_ERRORS as exc:
            details["state"] = "unavailable"
            details["error_code"] = str(
                getattr(exc, "code", "") or "passive_source_discovery_failed"
            )
            return details
        details["discovered_session_count"] = len(sessions)
        # Keep the historical passive-detection signal distinct from both binary
        # installation and path discovery.  The six-layer output exposes the
        # latter two facts separately and never promotes either to coverage.
        installed, evidence = agent_install_evidence(agent)
        details["detected"] = bool(installed or sessions)
        if details["data_dir"] is None and evidence:
            details["data_dir"] = evidence
        return details
    except _SAFE_RUNTIME_ERRORS as exc:
        details["state"] = "unavailable"
        details["error_code"] = str(
            getattr(exc, "code", "") or "passive_source_probe_failed"
        )
        return details


def _authorization_state(agent: str, *, default_state: str) -> tuple[str, bool]:
    try:
        from core.agent_kit.authorization import AgentAuthorizationStore

        store = AgentAuthorizationStore(initialize=False)
        record = store.get_record(agent)
        state = record.state if record is not None else default_state
        return state, AgentAuthorizationStore.content_access_authorized(state)
    except _SAFE_RUNTIME_ERRORS:
        return "unavailable", False


def _runtime_receipt_evaluation(agent: str) -> Dict[str, Any]:
    try:
        from core.agent_kit.runtime_receipts import AgentRuntimeReceiptStore
        from core.config import get_config

        config = get_config()
        max_age_seconds = int(
            config.get("agent_kit.runtime_receipt_max_age_seconds", 86400) or 86400
        )
        return AgentRuntimeReceiptStore(initialize=False).evaluate(
            agent,
            max_age_seconds=max_age_seconds,
        )
    except _SAFE_RUNTIME_ERRORS:
        return {
            "runtime_state": "unavailable",
            "runtime_receipt_at": "",
            "sample_completeness": {},
            "health_check_ids_hash": "",
            "support_manifest_hash": "",
            "error": "runtime capability receipt unavailable",
        }


def _source_capture_receipt_evaluation(agent: str) -> Dict[str, Any]:
    try:
        from core.agent_kit.runtime_receipts import AgentRuntimeReceiptStore
        from core.config import get_config

        config = get_config()
        max_age_seconds = int(
            config.get("agent_kit.runtime_receipt_max_age_seconds", 86400) or 86400
        )
        return AgentRuntimeReceiptStore(initialize=False).evaluate_source_capture(
            agent,
            max_age_seconds=max_age_seconds,
        )
    except _SAFE_RUNTIME_ERRORS:
        return {
            "source_capture_state": "unavailable",
            "source_capture_receipt_at": "",
            "native_source_snapshot_hash": "",
            "capture_completeness": {},
            "error": "source capture receipt unavailable",
        }


def _workflow_statuses(tool_names: Set[str]) -> List[AgentKitWorkflowStatus]:
    statuses: List[AgentKitWorkflowStatus] = []
    for contract in WORKFLOW_CONTRACTS:
        statuses.append(
            AgentKitWorkflowStatus(
                name=contract.name,
                phase=contract.phase,
                mcp_tool=contract.mcp_tool,
                exposed=contract.mcp_tool in tool_names,
                purpose=contract.purpose,
            )
        )
    return statuses


def _agent_status(
    agent: str,
    *,
    active_adapter_names: Set[str],
    active_statuses: Dict[str, Any],
    active_status_state: str,
    active_status_error_code: str,
    active_adapter_registry_state: str,
    active_adapter_registry_error_code: str,
    passive_registered: Set[str],
    probe_filesystem: bool,
) -> AgentKitAgentStatus:
    active_entrypoint = ACTIVE_ENTRYPOINTS.get(agent, "missing")
    runtime_status = active_statuses.get(agent)
    agent_active_status_state = str(
        getattr(runtime_status, "active_runtime_state", "")
        or active_status_state
        or "unknown"
    )
    agent_active_status_error_code = str(
        getattr(runtime_status, "active_runtime_error_code", "")
        or active_status_error_code
        or ""
    )
    if (
        active_entrypoint == "adapter"
        and active_adapter_registry_state == "unavailable"
    ):
        agent_active_status_state = "unavailable"
        agent_active_status_error_code = active_adapter_registry_error_code
    passive_details = _passive_source_details(agent, probe_filesystem=probe_filesystem)
    try:
        installed, evidence = (
            agent_install_evidence(agent) if probe_filesystem else (False, None)
        )
    except _SAFE_RUNTIME_ERRORS as exc:
        installed, evidence = False, None
        passive_details["state"] = "unavailable"
        passive_details["error_code"] = str(
            getattr(exc, "code", "") or "install_evidence_unavailable"
        )
    installed = installed or bool(getattr(runtime_status, "available", False))
    if not installed and bool(getattr(runtime_status, "passive_source_available", False)):
        installed = bool(passive_details.get("detected"))
    data_dir = passive_details.get("data_dir")
    if data_dir is None:
        data_dir = getattr(runtime_status, "data_dir", None)
    active_ready = bool(getattr(runtime_status, "active_ready", False))
    passive_detected = bool(passive_details.get("detected"))
    default_authorization_state = (
        "probe_ok" if installed and (active_ready or passive_detected) else "detected"
    )
    authorization_state, content_access_authorized = _authorization_state(
        agent,
        default_state=default_authorization_state,
    )

    status = AgentKitAgentStatus(
        name=agent,
        active_entrypoint=active_entrypoint,
        installed=installed,
        install_evidence=evidence or getattr(runtime_status, "data_dir", None),
        active_adapter_registered=agent in active_adapter_names,
        active_ready=bool(getattr(runtime_status, "active_ready", False)),
        active_runtime_state=agent_active_status_state,
        active_runtime_error_code=agent_active_status_error_code,
        hooks_installed=(
            bool(getattr(runtime_status, "hooks_installed", False))
            if active_entrypoint == "adapter"
            else False
        ),
        mcp_configured=bool(getattr(runtime_status, "mcp_configured", False)),
        policy_installed=bool(getattr(runtime_status, "policy_installed", False)),
        passive_source_registered=bool(passive_details.get("registered")) or agent in passive_registered,
        passive_source_detected=bool(passive_details.get("detected")),
        passive_source_state=str(passive_details.get("state") or "unknown"),
        passive_source_error_code=str(
            passive_details.get("error_code") or ""
        ),
        path_detected=bool(
            passive_details.get("path_detected", passive_details.get("detected"))
        ),
        content_access_authorized=content_access_authorized,
        authorization_state=authorization_state,
        data_dir=data_dir,
        required_capabilities=list(required_cognitive_capabilities(agent)),
        source_capabilities=dict(passive_details.get("capabilities") or {}),
    )

    if active_entrypoint == "planned":
        status.gaps.append("active entrypoint planned but not implemented")
    elif active_entrypoint == "missing":
        status.gaps.append("active entrypoint not declared")
    elif status.installed:
        if active_entrypoint == "adapter" and not status.active_adapter_registered:
            status.gaps.append("active adapter not registered")
        elif active_entrypoint == "mcp_only" and not status.active_ready:
            status.gaps.append("mcp-only active access not configured")

        if not status.passive_source_registered:
            status.gaps.append("passive source parser not registered")
        if not status.ready:
            status.gaps.append("no usable active or passive integration detected")

    _populate_full_power_status(status)
    _populate_runtime_status(status)
    return status


def _add_repair(status: AgentKitAgentStatus, action: str) -> None:
    if action not in status.repair_actions:
        status.repair_actions.append(action)


def _populate_full_power_status(status: AgentKitAgentStatus) -> None:
    if not status.installed:
        return

    if status.active_entrypoint in {"missing", "planned"}:
        status.full_power_gaps.append("no implemented active entrypoint")
    if status.active_entrypoint == "adapter" and not status.active_adapter_registered:
        status.full_power_gaps.append("adapter entrypoint not registered")
    if status.active_runtime_state == "unavailable":
        status.full_power_gaps.append(
            "active runtime diagnostics unavailable"
            + (
                f" ({status.active_runtime_error_code})"
                if status.active_runtime_error_code
                else ""
            )
        )
    if status.active_entrypoint == "adapter" and not status.hooks_installed:
        status.full_power_gaps.append("lifecycle hooks/wrapper not installed")
        _add_repair(status, f"mnemos doctor repair {status.name}")
    if not status.mcp_configured:
        status.full_power_gaps.append("Mnemos MCP server not configured")
        _add_repair(status, f"mnemos doctor repair {status.name}")
    if not status.policy_installed:
        status.full_power_gaps.append("Mnemos Active Policy not installed")
        _add_repair(status, f"mnemos doctor repair {status.name}")
    if not status.active_ready:
        status.full_power_gaps.append("active Mnemos workflow is not ready")
        _add_repair(status, f"mnemos doctor repair {status.name}")
        _add_repair(status, f"restart {status.name} and rerun mnemos agent kit {status.name}")

    if not status.passive_source_registered:
        status.full_power_gaps.append("passive source parser not registered")
    if status.passive_source_state == "unavailable":
        status.full_power_gaps.append(
            "passive source state unavailable"
            + (
                f" ({status.passive_source_error_code})"
                if status.passive_source_error_code
                else ""
            )
        )
    if not status.path_detected:
        status.full_power_gaps.append("passive source path has not been detected on this machine")
        _add_repair(status, f"start/use {status.name} once, then rerun mnemos agent kit {status.name}")

    fidelity = status.source_capabilities.get("source_fidelity")
    if not _source_fidelity_is_full(fidelity):
        status.full_power_gaps.append(f"passive source fidelity is not full ({fidelity or 'unknown'})")

    for capability in status.required_capabilities:
        if capability == "source_fidelity":
            continue
        value = status.source_capabilities.get(capability)
        if not _capability_is_available(capability, value):
            status.full_power_gaps.append(
                f"required cognitive capability not declared: {capability}"
            )


def _populate_runtime_status(status: AgentKitAgentStatus) -> None:
    if not status.installed:
        status.runtime_state = "not_applicable"
        status.source_capture_state = "not_applicable"
        return
    evaluation = _runtime_receipt_evaluation(status.name)
    status.runtime_state = str(evaluation.get("runtime_state") or "missing")
    status.runtime_receipt_at = str(evaluation.get("runtime_receipt_at") or "")
    status.sample_completeness = dict(evaluation.get("sample_completeness") or {})
    status.health_check_ids_hash = str(evaluation.get("health_check_ids_hash") or "")
    status.support_manifest_hash = str(evaluation.get("support_manifest_hash") or "")
    status.runtime_canary_hash = str(evaluation.get("runtime_canary_hash") or "")
    capture_evaluation = _source_capture_receipt_evaluation(status.name)
    status.source_capture_state = str(
        capture_evaluation.get("source_capture_state") or "missing"
    )
    status.source_capture_receipt_at = str(
        capture_evaluation.get("source_capture_receipt_at") or ""
    )
    status.native_source_snapshot_hash = str(
        capture_evaluation.get("native_source_snapshot_hash") or ""
    )
    status.source_capture_completeness = dict(
        capture_evaluation.get("capture_completeness") or {}
    )
    status.discovery_covered = (
        status.source_capture_completeness.get("discovery_covered") is True
    )
    status.content_parsed = status.source_capture_completeness.get("content_parsed") is True
    status.raw_committed = status.source_capture_completeness.get("raw_committed") is True
    status.runtime_canary_verified = (
        status.source_capture_completeness.get("runtime_canary_verified") is True
        and status.source_capture_completeness.get("runtime_canary_hash")
        == status.runtime_canary_hash
    )
    if not status.conformance_ok:
        status.runtime_gaps.append("static conformance contract failed")
    if not status.content_access_authorized:
        status.runtime_gaps.append(
            f"content access is not authorized ({status.authorization_state})"
        )
    if status.runtime_state != "verified":
        error = str(evaluation.get("error") or status.runtime_state)
        status.runtime_gaps.append(error)
    if status.source_capture_state != "verified":
        error = str(capture_evaluation.get("error") or status.source_capture_state)
        status.runtime_gaps.append(error)
    elif not status.runtime_canary_verified:
        status.runtime_gaps.append(
            "runtime canary is not independently bound to canonical Raw"
        )


def _ingestion_source_status(
    source_name: str,
    *,
    passive_registered: Set[str],
    probe_filesystem: bool,
) -> IngestionSourceStatus:
    """Report raw-ingestion readiness without promoting a source to host status."""
    manifest = get_agent_source_support_manifest()
    spec = manifest.source(source_name)
    passive_details = _passive_source_details(source_name, probe_filesystem=probe_filesystem)
    try:
        installed, evidence = (
            agent_install_evidence(source_name) if probe_filesystem else (False, None)
        )
    except _SAFE_RUNTIME_ERRORS as exc:
        installed, evidence = False, None
        passive_details["state"] = "unavailable"
        passive_details["error_code"] = str(
            getattr(exc, "code", "") or "install_evidence_unavailable"
        )
    installed = installed or bool(passive_details.get("detected"))
    authorization_state, content_access_authorized = _authorization_state(
        source_name,
        default_state="detected",
    )
    status = IngestionSourceStatus(
        name=spec.name,
        role=spec.role,
        installed=installed,
        install_evidence=evidence,
        passive_source_registered=bool(passive_details.get("registered"))
        or source_name in passive_registered,
        passive_source_detected=bool(passive_details.get("detected")),
        passive_source_state=str(passive_details.get("state") or "unknown"),
        passive_source_error_code=str(
            passive_details.get("error_code") or ""
        ),
        content_access_authorized=content_access_authorized,
        authorization_state=authorization_state,
        data_dir=passive_details.get("data_dir"),
        source_capabilities=dict(passive_details.get("capabilities") or {}),
        raw_contract=dict(spec.raw_contract),
    )
    if not status.installed:
        return status
    if status.passive_source_state == "unavailable":
        status.gaps.append(
            "passive source state unavailable"
            + (
                f" ({status.passive_source_error_code})"
                if status.passive_source_error_code
                else ""
            )
        )
    if not status.passive_source_registered:
        status.gaps.append("passive source parser not registered")
    if not status.passive_source_detected:
        status.gaps.append("native source has not been verified on this machine")
    if not status.content_access_authorized:
        status.gaps.append("content access is not authorized")
    expected_fidelity = str(status.raw_contract.get("fidelity") or "")
    actual_fidelity = status.source_capabilities.get("source_fidelity")
    if expected_fidelity and str(actual_fidelity) != expected_fidelity:
        status.gaps.append(
            f"source fidelity does not match raw contract ({actual_fidelity or 'unknown'})"
        )
    return status


def build_agent_kit_report(
    target_agents: Optional[Iterable[str]] = None,
    *,
    probe_filesystem: bool = True,
    load_default_providers: bool = True,
    isolated_default_providers: bool = False,
) -> AgentKitReport:
    """Build a conformance report for the shared Mnemos Agent Kit."""
    manifest = get_agent_source_support_manifest()
    agents = [
        normalize_agent_name(a)
        for a in (target_agents or TARGET_AGENT_NAMES)
        if normalize_agent_name(a)
    ]
    invalid_targets = [agent for agent in agents if agent not in manifest.host_agent_names]
    if invalid_targets:
        raise ValueError(
            "Agent Kit targets must be manifest-declared host agents: "
            + ", ".join(sorted(set(invalid_targets)))
        )
    try:
        tool_names = _safe_mcp_tool_names()
        workflow_tool_state = "available"
        workflow_tool_error_code = ""
    except AgentKitInventoryUnavailableError as exc:
        tool_names = set()
        workflow_tool_state = "unavailable"
        workflow_tool_error_code = exc.code
    missing_tools = [name for name in required_workflow_tool_names() if name not in tool_names]
    workflows = _workflow_statuses(tool_names)
    try:
        active_adapter_names = _safe_active_adapter_names()
        active_adapter_registry_state = "available"
        active_adapter_registry_error_code = ""
    except AgentKitInventoryUnavailableError as exc:
        active_adapter_names = set()
        active_adapter_registry_state = "unavailable"
        active_adapter_registry_error_code = exc.code
    if isolated_default_providers:
        active_statuses = _active_status_by_agent(
            False,
            isolated_default_providers=True,
        )
    else:
        active_statuses = _active_status_by_agent(load_default_providers)
    active_statuses = dict(active_statuses)
    active_status_state = str(
        active_statuses.pop(_ACTIVE_STATUS_STATE_KEY, "unknown") or "unknown"
    )
    active_status_error_code = str(
        active_statuses.pop(_ACTIVE_STATUS_ERROR_KEY, "") or ""
    )
    passive_registered = _passive_registered_names()

    agent_statuses = [
        _agent_status(
            agent,
            active_adapter_names=active_adapter_names,
            active_statuses=active_statuses,
            active_status_state=active_status_state,
            active_status_error_code=active_status_error_code,
            active_adapter_registry_state=active_adapter_registry_state,
            active_adapter_registry_error_code=(
                active_adapter_registry_error_code
            ),
            passive_registered=passive_registered,
            probe_filesystem=probe_filesystem,
        )
        for agent in agents
    ]
    ingestion_statuses = [
        _ingestion_source_status(
            source_name,
            passive_registered=passive_registered,
            probe_filesystem=probe_filesystem,
        )
        for source_name in manifest.ingestion_only_source_names
    ]

    return AgentKitReport(
        protocol_version="agent-kit-v2",
        target_agents=agents,
        workflows=workflows,
        agents=agent_statuses,
        missing_workflow_tools=missing_tools,
        workflow_tool_state=workflow_tool_state,
        workflow_tool_error_code=workflow_tool_error_code,
        active_adapter_registry_state=active_adapter_registry_state,
        active_adapter_registry_error_code=active_adapter_registry_error_code,
        ingestion_sources=ingestion_statuses,
        support_manifest_hash=manifest.manifest_hash,
    )
