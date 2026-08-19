# -*- coding: utf-8 -*-
"""Module toggle and cold-start output contracts for Mnemos.

The registry is intentionally static and dependency-light.  Runtime code can
consult it to explain why a switch is off, when it may be enabled, what it
would produce, and which consumer proves the value of that output.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence


MODULE_TOGGLE_SCHEMA_VERSION = "mnemos.module_toggle.v1"
TOGGLE_OUTPUT_SCHEMA_VERSION = "mnemos.toggle_output.v1"

TOGGLE_STATES = {
    "disabled_default",
    "disabled_cold_start",
    "eligible_to_enable",
    "auto_enabled",
    "manual_enabled",
    "degraded",
    "auto_disabled",
    "registered_but_unwired",
    "stale_removed",
}

TOGGLE_CATEGORIES = {
    "legacy",
    "privacy",
    "cost",
    "cold_start",
    "watcher",
    "daemon",
    "quality",
    "scoring",
    "notification",
}

REQUIRED_TOGGLE_KEYS = {
    "l1_storage.enabled",
    "persona.ab_test_enabled",
    "persona.data_sources.git.enabled",
    "persona.data_sources.wiki.enabled",
    "persona.data_sources.file_system.enabled",
    "cross_agent_share",
    "scoring.clustering.enabled",
    "scoring.training_scheduler.enabled",
    "distill.auto_expression_formatting",
    "dispute_scan.adaptive_learning.enabled",
    "application_signals.auto_notify",
    "watchers.enabled",
    "watchers.agent_paths.enabled",
    "daemon.services.raw_sync",
    "daemon.services.agent_path_watch",
    "daemon.services.link_probe",
    "freshness_refresh.redistill_enabled",
    "intent_router.llm_fallback_enabled",
    "features.enable_link_probe",
    "raw_projection.include_eligible_delete",
}

STALE_TOGGLE_KEYS = {
    "distill.allow_host_agent_delegate",
    "distill.pre_distill_gate.verbose_layers",
    "persona.data_sources.memos.enabled",
}

AUTO_ENABLE_STATES = {"disabled_cold_start", "eligible_to_enable", "auto_enabled"}


def _non_empty(values: Sequence[str]) -> bool:
    return bool(values) and all(isinstance(value, str) and value.strip() for value in values)


def _read_config_key(config: Any, dotted_key: str, default: Any) -> Any:
    if config is None:
        return default
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            return getter(dotted_key, default)
        except TypeError:
            return default
    current: Any = config
    for part in dotted_key.split("."):
        if isinstance(current, Mapping):
            current = current.get(part, default)
        else:
            return default
    return current


@dataclass(frozen=True)
class ToggleOutputContract:
    output_type: str
    storage_ref: str
    schema_ref: str
    consumer_ids: tuple[str, ...]
    quality_gate_id: str
    action_ledger_ref: str
    consumer_effect_metrics: tuple[str, ...]
    rollback_strategy: str
    scorecard_metrics: tuple[str, ...]
    mutual_exclusion: tuple[str, ...] = ()
    high_cost: bool = False
    schema_version: str = TOGGLE_OUTPUT_SCHEMA_VERSION

    def validate(self, *, allow_unwired: bool = False) -> list[str]:
        errors: list[str] = []
        if not self.output_type:
            errors.append("output_type is required")
        if not self.storage_ref:
            errors.append("storage_ref is required")
        if not self.schema_ref:
            errors.append("schema_ref is required")
        if not _non_empty(self.consumer_ids) and not allow_unwired:
            errors.append("consumer_ids must be non-empty")
        if self.quality_gate_id != "activation_quality_decision":
            errors.append("quality_gate_id must be activation_quality_decision")
        if self.action_ledger_ref != "module_toggle":
            errors.append("action_ledger_ref must be module_toggle")
        if not _non_empty(self.consumer_effect_metrics) and not allow_unwired:
            errors.append("consumer_effect_metrics must be non-empty")
        if not self.rollback_strategy:
            errors.append("rollback_strategy is required")
        if not _non_empty(self.scorecard_metrics) and not allow_unwired:
            errors.append("scorecard_metrics must be non-empty")
        return errors

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ModuleToggleDefinition:
    key: str
    owner: str
    category: str
    default_enabled: bool
    default_state: str
    default_reason: str
    activation_policy: tuple[str, ...]
    auto_disable_policy: tuple[str, ...]
    output_contract: ToggleOutputContract
    privacy_policy: str
    manual_override: str
    dependencies: tuple[str, ...] = ()
    auto_enable_allowed: bool = False
    stale: bool = False
    evidence_refs: tuple[str, ...] = ()

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.key:
            errors.append("key is required")
        if not self.owner:
            errors.append(f"{self.key}: owner is required")
        if self.category not in TOGGLE_CATEGORIES:
            errors.append(f"{self.key}: unknown category {self.category}")
        if self.default_state not in TOGGLE_STATES:
            errors.append(f"{self.key}: unknown default_state {self.default_state}")
        if not self.default_reason:
            errors.append(f"{self.key}: default_reason is required")
        if self.key in STALE_TOGGLE_KEYS and not self.stale:
            errors.append(f"{self.key}: stale key must be marked stale")
        if self.stale and self.default_state != "stale_removed":
            errors.append(f"{self.key}: stale key must use stale_removed state")
        if self.auto_enable_allowed and self.default_state not in AUTO_ENABLE_STATES:
            errors.append(f"{self.key}: auto-enabled toggle must start from a cold-start state")
        if self.auto_enable_allowed and not _non_empty(self.activation_policy):
            errors.append(f"{self.key}: activation_policy is required for auto enable")
        if not _non_empty(self.auto_disable_policy):
            errors.append(f"{self.key}: auto_disable_policy is required")
        if not self.manual_override:
            errors.append(f"{self.key}: manual_override is required")
        if not _non_empty(self.evidence_refs):
            errors.append(f"{self.key}: evidence_refs required")
        allow_unwired = self.default_state in {"registered_but_unwired", "stale_removed"}
        errors.extend(f"{self.key}: {error}" for error in self.output_contract.validate(allow_unwired=allow_unwired))
        if self.auto_enable_allowed and not _non_empty(self.output_contract.consumer_ids):
            errors.append(f"{self.key}: auto enable requires at least one consumer")
        return errors

    def current_enabled(self, config: Any | None = None) -> bool:
        value = _read_config_key(config, self.key, self.default_enabled)
        return bool(value)

    def runtime_state(self, config: Any | None = None) -> str:
        if self.stale:
            return "stale_removed"
        enabled = self.current_enabled(config)
        if enabled and self.default_enabled:
            return "manual_enabled"
        if enabled and not self.default_enabled:
            return "manual_enabled"
        return self.default_state

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _output(
    output_type: str,
    storage_ref: str,
    schema_ref: str,
    consumers: Sequence[str],
    effects: Sequence[str],
    rollback: str,
    scorecard: Sequence[str],
    *,
    mutual_exclusion: Sequence[str] = (),
    high_cost: bool = False,
) -> ToggleOutputContract:
    return ToggleOutputContract(
        output_type=output_type,
        storage_ref=storage_ref,
        schema_ref=schema_ref,
        consumer_ids=tuple(consumers),
        quality_gate_id="activation_quality_decision",
        action_ledger_ref="module_toggle",
        consumer_effect_metrics=tuple(effects),
        rollback_strategy=rollback,
        scorecard_metrics=tuple(scorecard),
        mutual_exclusion=tuple(mutual_exclusion),
        high_cost=high_cost,
    )


def _toggle(
    key: str,
    *,
    owner: str,
    category: str,
    default_enabled: bool,
    default_state: str,
    default_reason: str,
    activation_policy: Sequence[str],
    auto_disable_policy: Sequence[str],
    output_contract: ToggleOutputContract,
    privacy_policy: str,
    manual_override: str,
    dependencies: Sequence[str] = (),
    auto_enable_allowed: bool = False,
    stale: bool = False,
    evidence_refs: Sequence[str] = (),
) -> ModuleToggleDefinition:
    return ModuleToggleDefinition(
        key=key,
        owner=owner,
        category=category,
        default_enabled=default_enabled,
        default_state=default_state,
        default_reason=default_reason,
        activation_policy=tuple(activation_policy),
        auto_disable_policy=tuple(auto_disable_policy),
        output_contract=output_contract,
        privacy_policy=privacy_policy,
        manual_override=manual_override,
        dependencies=tuple(dependencies),
        auto_enable_allowed=auto_enable_allowed,
        stale=stale,
        evidence_refs=tuple(evidence_refs),
    )


MODULE_TOGGLE_DEFINITIONS: dict[str, ModuleToggleDefinition] = {
    "l1_storage.enabled": _toggle(
        "l1_storage.enabled",
        owner="core.config",
        category="legacy",
        default_enabled=False,
        default_state="registered_but_unwired",
        default_reason="legacy external L1/Memos storage is no longer the canonical raw layer",
        activation_policy=("manual compatibility only",),
        auto_disable_policy=("disable when raw_event_store and raw_projection are available",),
        output_contract=_output(
            "legacy_l1_storage_event",
            "external_memos_api",
            "legacy.l1_storage.v0",
            (),
            (),
            "leave disabled and route capture through raw_event_store",
            (),
        ),
        privacy_policy="raw_event",
        manual_override="manual legacy deployment override only",
        auto_enable_allowed=False,
        evidence_refs=("core/config.py", "core/sync_framework/README.md"),
    ),
    "persona.ab_test_enabled": _toggle(
        "persona.ab_test_enabled",
        owner="core.persona",
        category="cold_start",
        default_enabled=False,
        default_state="disabled_cold_start",
        default_reason="needs enough persona behavior samples before experiment assignment is useful",
        activation_policy=("persona behavior samples >= 30", "delivery feedback accept rate is measurable"),
        auto_disable_policy=(
            "disable when intervention error rate rises",
            "disable when sample count falls below threshold",
        ),
        output_contract=_output(
            "persona_ab_assignment",
            "persona_metrics.db",
            "mnemos.persona_ab_assignment.v1",
            ("persona_behavior_prompt", "delivery_router"),
            ("profile_prompt_delta", "delivery_accept_rate_delta"),
            "disable runtime experiment assignment and keep baseline prompt",
            ("user_intervention_reduction", "false_positive_rate"),
        ),
        privacy_policy="persona",
        manual_override="user or developer can force experiment mode for evaluation",
        auto_enable_allowed=True,
        evidence_refs=("core/config.py", "core/persona/daimon.py"),
    ),
    "persona.data_sources.git.enabled": _toggle(
        "persona.data_sources.git.enabled",
        owner="core.persona",
        category="privacy",
        default_enabled=False,
        default_state="disabled_cold_start",
        default_reason="git signals can expose project behavior and require explicit readiness",
        activation_policy=(
            "project path consent exists",
            "git repository detected",
            "persona source quality gate passes",
        ),
        auto_disable_policy=("disable on privacy denial", "disable after repeated collector failures"),
        output_contract=_output(
            "persona_git_signal",
            "persona_signals.db",
            "mnemos.persona_signal.v1",
            ("persona_profile_builder", "preflight_inject"),
            ("persona_assertion_delta", "preflight_context_hit_delta"),
            "disable git collector and ignore unconsumed pending signals",
            ("persona_consumption_rate", "user_intervention_reduction"),
        ),
        privacy_policy="persona",
        manual_override="explicit source enablement in user config",
        auto_enable_allowed=True,
        dependencies=("obsidian_vault",),
        evidence_refs=("core/config.py", "core/persona/daimon.py"),
    ),
    "persona.data_sources.wiki.enabled": _toggle(
        "persona.data_sources.wiki.enabled",
        owner="core.persona",
        category="privacy",
        default_enabled=False,
        default_state="disabled_cold_start",
        default_reason="wiki-derived persona signals need source provenance and consumption proof",
        activation_policy=("wiki vault writable", "source refs available", "persona source quality gate passes"),
        auto_disable_policy=("disable when wiki audit fails", "disable when persona signal rejection rate is high"),
        output_contract=_output(
            "persona_wiki_signal",
            "persona_signals.db",
            "mnemos.persona_signal.v1",
            ("persona_profile_builder", "preflight_inject"),
            ("persona_assertion_delta", "preflight_context_hit_delta"),
            "disable wiki source collector and retain previous verified assertions",
            ("persona_consumption_rate", "preflight_hit_rate"),
        ),
        privacy_policy="persona",
        manual_override="explicit source enablement in user config",
        auto_enable_allowed=True,
        dependencies=("obsidian_vault",),
        evidence_refs=("core/config.py", "core/persona/daimon.py"),
    ),
    "persona.data_sources.file_system.enabled": _toggle(
        "persona.data_sources.file_system.enabled",
        owner="core.persona",
        category="privacy",
        default_enabled=False,
        default_state="disabled_cold_start",
        default_reason="file-system signals need explicit path boundaries and privacy review",
        activation_policy=("approved source path exists", "path scope is not home root", "privacy gate passes"),
        auto_disable_policy=("disable when path becomes unavailable", "disable on privacy policy violation"),
        output_contract=_output(
            "persona_file_signal",
            "persona_signals.db",
            "mnemos.persona_signal.v1",
            ("persona_profile_builder", "preflight_inject"),
            ("persona_assertion_delta", "preflight_context_hit_delta"),
            "disable file-system collector and quarantine unverified signals",
            ("persona_consumption_rate", "privacy_block_rate"),
        ),
        privacy_policy="persona",
        manual_override="explicit source enablement with bounded path",
        auto_enable_allowed=True,
        evidence_refs=("core/config.py", "core/persona/daimon.py"),
    ),
    "cross_agent_share": _toggle(
        "cross_agent_share",
        owner="core.access_policy",
        category="privacy",
        default_enabled=False,
        default_state="disabled_default",
        default_reason="cross-agent consumption must be authorized by scope",
        activation_policy=("manual authorization only",),
        auto_disable_policy=("disable when private cross-agent denial is recorded",),
        output_contract=_output(
            "cross_agent_context_ref",
            "access_policy.db",
            "mnemos.cross_agent_ref.v1",
            ("context_aware_search", "preflight_inject"),
            ("authorized_cross_agent_hit_rate",),
            "revoke runtime authorization and keep same-agent access",
            ("privacy_denial_rate", "preflight_hit_rate"),
        ),
        privacy_policy="policy",
        manual_override="explicit per-agent authorization",
        auto_enable_allowed=False,
        evidence_refs=("core/access_policy.py",),
    ),
    "scoring.clustering.enabled": _toggle(
        "scoring.clustering.enabled",
        owner="core.scoring",
        category="scoring",
        default_enabled=False,
        default_state="registered_but_unwired",
        default_reason="clustering has no required production consumer yet",
        activation_policy=("wire a production consumer first",),
        auto_disable_policy=("keep disabled until consumer readiness exists",),
        output_contract=_output(
            "scoring_cluster_candidate",
            "scoring.db",
            "mnemos.scoring_cluster.v1",
            (),
            (),
            "keep disabled; no runtime output is produced",
            (),
        ),
        privacy_policy="policy",
        manual_override="developer-only evaluation mode",
        auto_enable_allowed=False,
        evidence_refs=("core/config.py", "docs/链路13_测试验证层审计报告.md"),
    ),
    "scoring.training_scheduler.enabled": _toggle(
        "scoring.training_scheduler.enabled",
        owner="core.scoring",
        category="scoring",
        default_enabled=False,
        default_state="registered_but_unwired",
        default_reason="training scheduler has no production scorer promotion path yet",
        activation_policy=("wire model promotion and rollback first",),
        auto_disable_policy=("keep disabled until rollback and scorecard metrics exist",),
        output_contract=_output(
            "scoring_training_job",
            "scoring.db",
            "mnemos.scoring_training_job.v1",
            (),
            (),
            "keep disabled; no runtime job is scheduled",
            (),
        ),
        privacy_policy="policy",
        manual_override="developer-only training drill",
        auto_enable_allowed=False,
        evidence_refs=("core/config.py", "docs/链路13_测试验证层审计报告.md"),
    ),
    "distill.auto_expression_formatting": _toggle(
        "distill.auto_expression_formatting",
        owner="core.hephaestus",
        category="quality",
        default_enabled=True,
        default_state="auto_enabled",
        default_reason=(
            "structured Wiki pages record expression_format by default; "
            "body formatting remains guarded by reversible Markdown formatting tests"
        ),
        activation_policy=(
            "wiki quality gate passes",
            "formatting diff is reversible",
            "source refs are retained",
        ),
        auto_disable_policy=("disable when wiki lint or source coverage regresses",),
        output_contract=_output(
            "formatted_wiki_body",
            "mnemos vault markdown",
            "mnemos.wiki_page.v1",
            ("obsidian_ui", "wiki_lint", "context_aware_search"),
            ("wiki_readability_delta", "source_ref_preservation"),
            "restore page snapshot or previous markdown body",
            ("obsidian_experience_score", "source_coverage"),
        ),
        privacy_policy="wiki_page",
        manual_override="explicit formatting enablement for trusted vaults",
        auto_enable_allowed=True,
        evidence_refs=("core/config.py", "core/hephaestus/distillation_engine.py"),
    ),
    "dispute_scan.adaptive_learning.enabled": _toggle(
        "dispute_scan.adaptive_learning.enabled",
        owner="core.app.dispute_scorer",
        category="cold_start",
        default_enabled=False,
        default_state="disabled_cold_start",
        default_reason="weight learning needs enough resolved disputes",
        activation_policy=("resolved dispute samples >= min_samples_before_update", "rollback weights are available"),
        auto_disable_policy=("disable when resolution quality decreases", "disable when sample drift is detected"),
        output_contract=_output(
            "dispute_weight_update",
            "dispute_scorer_state.db",
            "mnemos.dispute_weight_update.v1",
            ("dispute_scorer", "dispute_resolver"),
            ("resolution_gap_delta", "manual_resolution_rate_delta"),
            "restore previous weights",
            ("auto_healing_score", "false_positive_rate"),
        ),
        privacy_policy="policy",
        manual_override="explicit adaptive learning enablement",
        auto_enable_allowed=True,
        evidence_refs=("core/config.py", "core/app/dispute_scorer.py"),
    ),
    "application_signals.auto_notify": _toggle(
        "application_signals.auto_notify",
        owner="core.app.application_signal_service",
        category="notification",
        default_enabled=False,
        default_state="disabled_cold_start",
        default_reason="automatic user notifications need acceptance feedback and cooldown proof",
        activation_policy=(
            "signal accept rate is above threshold",
            "cooldown policy is active",
            "recap feedback exists",
        ),
        auto_disable_policy=(
            "disable when ignore/dismiss rate exceeds budget",
            "disable during quiet delivery profile",
        ),
        output_contract=_output(
            "application_signal_reminder",
            "application_signals.db",
            "mnemos.application_signal.v1",
            ("reminder_engine", "delivery_router"),
            ("reminder_accept_rate", "dismiss_rate_delta"),
            "disable auto-notify and keep explicit signal listing",
            ("user_intervention_reduction", "false_positive_rate"),
        ),
        privacy_policy="reflection",
        manual_override="explicit notification enablement",
        auto_enable_allowed=True,
        evidence_refs=("core/config.py", "core/app/application_signal_service.py"),
    ),
    "watchers.enabled": _toggle(
        "watchers.enabled",
        owner="daemon.watchers",
        category="watcher",
        default_enabled=False,
        default_state="disabled_cold_start",
        default_reason="file watchers should start only after bounded paths and consumers are ready",
        activation_policy=(
            "at least one watcher child is eligible",
            "watched paths are bounded",
            "daemon is healthy",
        ),
        auto_disable_policy=(
            "disable when watcher error budget is exceeded",
            "disable when no child watcher is enabled",
        ),
        output_contract=_output(
            "watcher_change_event",
            "events.db",
            "mnemos.watcher_event.v1",
            ("eventbus", "file_ingestor", "trigger_dispatcher"),
            ("change_event_consumption_rate", "ingest_success_rate"),
            "disable master watcher and keep scheduled scans",
            ("data_pipeline_score", "daemon_error_rate"),
        ),
        privacy_policy="raw_event",
        manual_override="explicit watcher enablement",
        auto_enable_allowed=True,
        evidence_refs=("core/config.py", "mnemos_daemon.py"),
    ),
    "watchers.agent_paths.enabled": _toggle(
        "watchers.agent_paths.enabled",
        owner="daemon.watchers",
        category="watcher",
        default_enabled=False,
        default_state="disabled_cold_start",
        default_reason="agent path watch needs installed agent path registry",
        activation_policy=(
            "agent paths are registered",
            "source parser is available",
            "daemon trigger is healthy",
        ),
        auto_disable_policy=("disable when path disappears", "disable after repeated parse failures"),
        output_contract=_output(
            "agent_path_dirty_signal",
            "events.db",
            "mnemos.agent_path_dirty_signal.v1",
            ("raw_sync", "trigger_dispatcher"),
            ("agent_path_scan_count", "capture_queue_delta"),
            "disable agent path watcher and keep scheduled raw sync",
            ("data_pipeline_score", "user_intervention_reduction"),
        ),
        privacy_policy="raw_event",
        manual_override="explicit agent path watcher enablement",
        auto_enable_allowed=True,
        dependencies=("watchers.enabled",),
        evidence_refs=("core/config.py", "core/sync_framework/agent_path_watcher.py"),
    ),
    "daemon.services.raw_sync": _toggle(
        "daemon.services.raw_sync",
        owner="daemon.raw_sync",
        category="daemon",
        default_enabled=True,
        default_state="auto_enabled",
        default_reason="manifest-owned scheduled capture is the default continuous owner for local AgentSource data",
        activation_policy=(
            "manifest declares an active source",
            "source content access remains user-authorized",
            "capture queue is healthy",
        ),
        auto_disable_policy=(
            "disable only by an explicit operator decision",
            "surface per-source failures in the coverage heartbeat",
        ),
        output_contract=_output(
            "raw_sync_capture_event",
            "raw_events.db",
            "mnemos.raw_event.v1",
            ("raw_event_store", "capture_queue", "raw_projection"),
            ("captured_turn_count", "projection_success_rate"),
            "explicitly disable continuous local capture after reviewing coverage evidence",
            ("data_pipeline_score", "capture_failure_rate"),
        ),
        privacy_policy="raw_event",
        manual_override="explicit operator pause of continuous local capture",
        auto_enable_allowed=True,
        evidence_refs=("core/config.py", "daemon/raw_sync.py"),
    ),
    "daemon.services.agent_path_watch": _toggle(
        "daemon.services.agent_path_watch",
        owner="mnemos_daemon",
        category="daemon",
        default_enabled=False,
        default_state="disabled_cold_start",
        default_reason="agent path watch service requires configured agent transcript paths",
        activation_policy=("watchers.agent_paths.enabled is true", "agent kit detects installed sources"),
        auto_disable_policy=("disable when agent paths disappear", "disable after repeated watcher failures"),
        output_contract=_output(
            "agent_path_watch_result",
            "daemon_heartbeat.json",
            "mnemos.daemon_service_result.v1",
            ("raw_sync", "trigger_dispatcher"),
            ("dirty_source_count", "capture_queue_delta"),
            "disable daemon service and rely on scheduled sync",
            ("data_pipeline_score", "daemon_error_rate"),
        ),
        privacy_policy="raw_event",
        manual_override="explicit daemon service enablement",
        auto_enable_allowed=True,
        dependencies=("watchers.agent_paths.enabled",),
        evidence_refs=("core/config.py", "mnemos_daemon.py"),
    ),
    "daemon.services.link_probe": _toggle(
        "daemon.services.link_probe",
        owner="daemon.link_probe",
        category="daemon",
        default_enabled=False,
        default_state="disabled_cold_start",
        default_reason="link probe is background network work and is off until the feature flag is enabled",
        activation_policy=(
            "features.enable_link_probe is true",
            "network budget allows probes",
            "wiki link queue exists",
        ),
        auto_disable_policy=("disable on network error budget breach", "disable when feature flag is false"),
        output_contract=_output(
            "link_probe_result",
            "link_probe.db",
            "mnemos.link_probe_result.v1",
            ("wiki_lint", "link_probe_status", "obsidian_ui"),
            ("broken_link_count_delta", "probe_success_rate"),
            "disable daemon link probe and leave manual link-probe CLI",
            ("obsidian_experience_score", "daemon_error_rate"),
            high_cost=True,
        ),
        privacy_policy="artifact",
        manual_override="explicit link probe feature and daemon service enablement",
        auto_enable_allowed=True,
        dependencies=("features.enable_link_probe",),
        evidence_refs=("core/config.py", "daemon/link_probe.py"),
    ),
    "freshness_refresh.redistill_enabled": _toggle(
        "freshness_refresh.redistill_enabled",
        owner="core.app.freshness_refresh_worker",
        category="cost",
        default_enabled=False,
        default_state="disabled_cold_start",
        default_reason="redistill is a high-cost rewrite and needs stale-page value proof",
        activation_policy=(
            "stale page queue exists",
            "LLM budget available",
            "page snapshot exists",
            "quality gate passes",
        ),
        auto_disable_policy=(
            "disable when freshness output is rejected",
            "disable when LLM cost budget is exceeded",
        ),
        output_contract=_output(
            "freshness_redistill_body",
            "mnemos vault markdown",
            "mnemos.wiki_page.v1",
            ("wiki_builder", "context_aware_search", "freshness_status"),
            ("freshness_score_delta", "search_hit_delta"),
            "restore page snapshot and mark refresh degraded",
            ("cognitive_assets_score", "cost_benefit_ratio"),
            high_cost=True,
        ),
        privacy_policy="wiki_page",
        manual_override="explicit high-cost refresh enablement",
        auto_enable_allowed=True,
        evidence_refs=("core/config.py", "core/app/freshness_refresh_worker.py"),
    ),
    "intent_router.llm_fallback_enabled": _toggle(
        "intent_router.llm_fallback_enabled",
        owner="core.app.intent_router",
        category="cost",
        default_enabled=False,
        default_state="disabled_cold_start",
        default_reason="LLM fallback adds latency and cost; it needs correction-rate evidence",
        activation_policy=(
            "rule confidence below threshold",
            "intent corrections show local misroutes",
            "LLM budget available",
        ),
        auto_disable_policy=(
            "disable when correction rate does not improve",
            "disable on rate-limit or timeout budget breach",
        ),
        output_contract=_output(
            "llm_route_decision",
            "intent_router.db",
            "mnemos.intent_route_decision.v1",
            ("application_router", "intent_correct", "scorecard"),
            ("route_correction_rate_delta", "fallback_timeout_rate"),
            "disable LLM fallback and return to local rule route decision",
            ("user_intervention_reduction", "cost_benefit_ratio"),
            high_cost=True,
        ),
        privacy_policy="reasoning",
        manual_override="explicit LLM fallback enablement",
        auto_enable_allowed=True,
        evidence_refs=("core/config.py", "core/app/intent_router.py"),
    ),
    "features.enable_link_probe": _toggle(
        "features.enable_link_probe",
        owner="core.hephaestus.link_probe_worker",
        category="cost",
        default_enabled=False,
        default_state="disabled_cold_start",
        default_reason="external link probing is network work and should run only with user value proof",
        activation_policy=(
            "wiki has external links",
            "network budget allows probes",
            "consumer status page is enabled",
        ),
        auto_disable_policy=("disable when network failures exceed budget", "disable when daemon service is disabled"),
        output_contract=_output(
            "link_probe_queue_item",
            "link_probe.db",
            "mnemos.link_probe_queue.v1",
            ("daemon.services.link_probe", "wiki_lint", "link_probe_status"),
            ("probe_queue_consumption_rate", "broken_link_count_delta"),
            "disable probe job enqueueing and keep existing wiki links",
            ("obsidian_experience_score", "cost_benefit_ratio"),
            high_cost=True,
        ),
        privacy_policy="artifact",
        manual_override="explicit feature enablement",
        auto_enable_allowed=True,
        evidence_refs=("core/config.py", "core/hephaestus/link_probe_worker.py"),
    ),
    "raw_projection.include_eligible_delete": _toggle(
        "raw_projection.include_eligible_delete",
        owner="scripts.project_raw_vault",
        category="privacy",
        default_enabled=False,
        default_state="disabled_default",
        default_reason="eligible-delete rows must stay out of user-visible raw projection by default",
        activation_policy=("manual audit/export mode only",),
        auto_disable_policy=(
            "disable after audit/export command completes",
            "disable when retention policy marks rows private",
        ),
        output_contract=_output(
            "raw_projection_deleted_candidate",
            "raw vault projection",
            "mnemos.raw_projection.v1",
            ("retention_audit", "data_ownership_export"),
            ("eligible_delete_visibility_count",),
            "return projection to retained-only rows",
            ("privacy_security_score",),
        ),
        privacy_policy="raw_event",
        manual_override="manual privacy audit/export only",
        auto_enable_allowed=False,
        evidence_refs=("core/config.py", "scripts/project_raw_vault.py"),
    ),
    "distill.allow_host_agent_delegate": _toggle(
        "distill.allow_host_agent_delegate",
        owner="core.config",
        category="legacy",
        default_enabled=False,
        default_state="stale_removed",
        default_reason="removed distill key; host-agent delegation is not a runtime config",
        activation_policy=("migrate/delete stale key",),
        auto_disable_policy=("doctor must report stale key and keep it disabled",),
        output_contract=_output(
            "stale_config_key",
            "config migration report",
            "mnemos.stale_config_key.v1",
            (),
            (),
            "delete stale key or migrate to canonical config",
            (),
        ),
        privacy_policy="policy",
        manual_override="not supported",
        auto_enable_allowed=False,
        stale=True,
        evidence_refs=("core/config.py", "docs/CHANGELOG.md"),
    ),
    "distill.pre_distill_gate.verbose_layers": _toggle(
        "distill.pre_distill_gate.verbose_layers",
        owner="core.config",
        category="legacy",
        default_enabled=False,
        default_state="stale_removed",
        default_reason="removed pre-distill verbose layer key; quality gate uses canonical prompt call logs",
        activation_policy=("migrate/delete stale key",),
        auto_disable_policy=("doctor must report stale key and keep it disabled",),
        output_contract=_output(
            "stale_config_key",
            "config migration report",
            "mnemos.stale_config_key.v1",
            (),
            (),
            "delete stale key; model-call accounting is always enforced by ModelCallLedger",
            (),
        ),
        privacy_policy="policy",
        manual_override="not supported",
        auto_enable_allowed=False,
        stale=True,
        evidence_refs=("core/config.py", "docs/CHANGELOG.md"),
    ),
    "persona.data_sources.memos.enabled": _toggle(
        "persona.data_sources.memos.enabled",
        owner="core.config",
        category="legacy",
        default_enabled=False,
        default_state="stale_removed",
        default_reason="external Memos persona source was retired with local raw_event storage",
        activation_policy=("migrate/delete stale key",),
        auto_disable_policy=("doctor must report stale key and keep it disabled",),
        output_contract=_output(
            "stale_config_key",
            "config migration report",
            "mnemos.stale_config_key.v1",
            (),
            (),
            "delete stale key and use raw_event/persona session sources",
            (),
        ),
        privacy_policy="policy",
        manual_override="not supported",
        auto_enable_allowed=False,
        stale=True,
        evidence_refs=("core/config.py", "docs/CHANGELOG.md"),
    ),
}


def get_toggle_definition(key: str) -> ModuleToggleDefinition | None:
    return MODULE_TOGGLE_DEFINITIONS.get(key)


def build_toggle_matrix(config: Any | None = None) -> dict[str, dict[str, Any]]:
    return {
        key: {
            **definition.as_dict(),
            "current_enabled": definition.current_enabled(config),
            "runtime_state": definition.runtime_state(config),
        }
        for key, definition in sorted(MODULE_TOGGLE_DEFINITIONS.items())
    }


def _validate_registry_paths(root: Any | None, evidence_refs: Sequence[str]) -> list[str]:
    if root is None:
        return []
    from pathlib import Path

    base = Path(root)
    errors: list[str] = []
    for ref in evidence_refs:
        path = str(ref).split(":", 1)[0]
        if path.endswith(".py") or path.endswith(".md") or "/" in path:
            if not (base / path).exists():
                errors.append(f"missing referenced path: {path}")
    return errors


def audit_module_toggle_registry(*, strict: bool = False, root: Any | None = None) -> list[str]:
    errors: list[str] = []
    missing = REQUIRED_TOGGLE_KEYS - set(MODULE_TOGGLE_DEFINITIONS)
    if missing:
        errors.append(f"missing required toggle keys: {sorted(missing)}")
    missing_stale = STALE_TOGGLE_KEYS - set(MODULE_TOGGLE_DEFINITIONS)
    if missing_stale:
        errors.append(f"missing stale toggle keys: {sorted(missing_stale)}")
    for key, definition in MODULE_TOGGLE_DEFINITIONS.items():
        if key != definition.key:
            errors.append(f"{key}: key and definition.key disagree")
        errors.extend(definition.validate())
        if strict:
            errors.extend(
                f"{key}: {error}"
                for error in _validate_registry_paths(root or _repo_root(), definition.evidence_refs)
            )
    return errors


def audit_toggle_auto_disable_policy(*, strict: bool = False) -> list[str]:
    errors: list[str] = []
    for key, definition in MODULE_TOGGLE_DEFINITIONS.items():
        if not _non_empty(definition.auto_disable_policy):
            errors.append(f"{key}: auto_disable_policy required")
        rollback_strategy = definition.output_contract.rollback_strategy.lower()
        if (
            definition.auto_enable_allowed
            and "rollback" not in rollback_strategy
            and "restore" not in rollback_strategy
            and "disable" not in rollback_strategy
        ):
            errors.append(f"{key}: auto-enabled toggle needs rollback/restore/disable strategy")
        if strict and definition.output_contract.high_cost and definition.auto_enable_allowed:
            policy = " ".join(definition.activation_policy).lower()
            if (
                "budget" not in policy
                and "cost" not in policy
                and "network" not in policy
                and "migration" not in policy
            ):
                errors.append(f"{key}: high-cost auto enable must declare budget/cost/network/migration gate")
    return errors


def audit_cold_start_toggle_matrix(*, strict: bool = False) -> list[str]:
    errors: list[str] = []
    for key, definition in MODULE_TOGGLE_DEFINITIONS.items():
        if definition.default_enabled:
            continue
        if definition.default_state not in {
            "disabled_default",
            "disabled_cold_start",
            "registered_but_unwired",
            "stale_removed",
        }:
            errors.append(f"{key}: default-disabled toggle has invalid state {definition.default_state}")
        if not definition.default_reason:
            errors.append(f"{key}: default-disabled toggle needs default_reason")
        if strict and definition.default_state == "disabled_cold_start" and not definition.activation_policy:
            errors.append(f"{key}: cold-start toggle needs activation_policy")
    return errors


def audit_toggle_output_consumers(*, strict: bool = False) -> list[str]:
    errors: list[str] = []
    for key, definition in MODULE_TOGGLE_DEFINITIONS.items():
        allow_unwired = definition.default_state in {"registered_but_unwired", "stale_removed"}
        output_errors = definition.output_contract.validate(allow_unwired=allow_unwired)
        errors.extend(f"{key}: {error}" for error in output_errors)
        if not allow_unwired and not definition.output_contract.consumer_ids:
            errors.append(f"{key}: wired toggle must declare consumers")
        if definition.output_contract.mutual_exclusion and not definition.auto_disable_policy:
            errors.append(f"{key}: mutual exclusion requires auto disable policy")
        if strict and definition.auto_enable_allowed and not definition.output_contract.consumer_effect_metrics:
            errors.append(f"{key}: auto-enabled toggle needs consumer effect metrics")
    return errors


def audit_runtime_producer_consumer_closure(*, strict: bool = False) -> list[str]:
    errors = audit_toggle_output_consumers(strict=strict)
    producers = {
        definition.output_contract.output_type
        for definition in MODULE_TOGGLE_DEFINITIONS.values()
        if definition.default_state not in {"registered_but_unwired", "stale_removed"}
    }
    if strict:
        for key, definition in MODULE_TOGGLE_DEFINITIONS.items():
            if definition.default_state in {"registered_but_unwired", "stale_removed"}:
                continue
            if not definition.output_contract.consumer_ids:
                errors.append(f"{key}: producer has no consumer")
            if definition.output_contract.output_type not in producers:
                errors.append(f"{key}: output_type missing from producer set")
    return errors


def validate_all_module_toggle_contracts(*, strict: bool = False) -> list[str]:
    errors: list[str] = []
    audits = {
        "module_toggle_registry": audit_module_toggle_registry,
        "toggle_auto_disable_policy": audit_toggle_auto_disable_policy,
        "cold_start_toggle_matrix": audit_cold_start_toggle_matrix,
        "toggle_output_consumers": audit_toggle_output_consumers,
        "runtime_producer_consumer_closure": audit_runtime_producer_consumer_closure,
    }
    for name, audit in audits.items():
        errors.extend(f"{name}: {error}" for error in audit(strict=strict))
    return errors


def build_module_toggle_health(config: Any | None = None) -> dict[str, Any]:
    errors = validate_all_module_toggle_contracts(strict=True)
    definitions = list(MODULE_TOGGLE_DEFINITIONS.values())
    auto_enable_candidates = [item.key for item in definitions if item.auto_enable_allowed]
    unwired = [item.key for item in definitions if item.default_state == "registered_but_unwired"]
    stale = [item.key for item in definitions if item.stale]
    return {
        "status": "ok" if not errors else "degraded",
        "schema_versions": {
            "module_toggle": MODULE_TOGGLE_SCHEMA_VERSION,
            "toggle_output": TOGGLE_OUTPUT_SCHEMA_VERSION,
        },
        "counts": {
            "toggles": len(definitions),
            "auto_enable_candidates": len(auto_enable_candidates),
            "registered_but_unwired": len(unwired),
            "stale_removed": len(stale),
            "output_contracts": len(definitions),
        },
        "auto_enable_candidates": auto_enable_candidates,
        "registered_but_unwired": unwired,
        "stale_removed": stale,
        "errors": errors,
    }


def build_module_toggle_report(config: Any | None = None) -> dict[str, Any]:
    health = build_module_toggle_health(config)
    return {
        **health,
        "toggles": build_toggle_matrix(config),
    }


def module_toggle_snapshot() -> dict[str, Any]:
    return {
        "schema_versions": {
            "module_toggle": MODULE_TOGGLE_SCHEMA_VERSION,
            "toggle_output": TOGGLE_OUTPUT_SCHEMA_VERSION,
        },
        "toggles": {key: value.as_dict() for key, value in MODULE_TOGGLE_DEFINITIONS.items()},
    }


def _repo_root() -> Any:
    from pathlib import Path

    return Path(__file__).resolve().parents[1]
