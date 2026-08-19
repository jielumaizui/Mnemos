"""Canonical registry for Mnemos configuration keys and lifecycle metadata."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = "mnemos.config_registry.v1"


class ConfigRegistryError(ValueError):
    """Base error for canonical configuration contract violations."""


class UnknownConfigKeyError(ConfigRegistryError, KeyError):
    """Raised when a caller attempts to read an unregistered key."""


class ConfigValidationError(ConfigRegistryError):
    """Raised when a config source violates the registry contract."""

    def __init__(self, errors: Iterable["ConfigValidationIssue"]):
        self.errors = tuple(errors)
        detail = "; ".join(f"{item.code}:{item.key}" for item in self.errors)
        super().__init__(f"configuration validation failed: {detail}")


@dataclass(frozen=True)
class ConfigValidationIssue:
    code: str
    key: str
    source: str
    expected_type: str = ""
    actual_type: str = ""


@dataclass(frozen=True)
class ConfigKeySpec:
    key: str
    default: Any
    value_types: tuple[type, ...]
    secret: bool
    open_mapping: bool
    example_documented: bool = True
    tested: bool = True
    documented: bool = True

    @property
    def value_type_name(self) -> str:
        return "|".join(item.__name__ for item in self.value_types)

    def accepts(self, value: Any) -> bool:
        if bool in self.value_types and isinstance(value, bool):
            return True
        if isinstance(value, bool) and bool not in self.value_types:
            return False
        if float in self.value_types and isinstance(value, int):
            return True
        return isinstance(value, self.value_types)


CONFIG_KEY_ALIASES: dict[str, str] = {
    "daemon.services.l1_sync": "daemon.services.raw_sync",
    "daemon.services.distill_merge": "daemon.services.distill_and_merge",
    "daemon.services.event_bus": "daemon.services.eventbus",
    "sync.l1_scan_max_sessions_per_source": "sync.raw_sync_sessions_per_source",
    "sync.l1_scan_max_turns_per_session": "sync.raw_sync_turns_per_session",
    "distill.auto_enabled": "distill.auto",
    "embedding.rerank_model": "reranker.model",
    "skill.cleanup_days": "skill.cognitive_decision_flywheel.cleanup_days",
    "skill.grace_period_days": "skill.cognitive_decision_flywheel.grace_period_days",
    "skill.min_age_days": "skill.cognitive_decision_flywheel.min_age_days",
    "skill.min_usage_count": "skill.cognitive_decision_flywheel.min_usage_count",
    "skill.time_window_days": "skill.cognitive_decision_flywheel.time_window_days",
    "skill.wiki_jaccard_threshold": "skill.cognitive_decision_flywheel.wiki_jaccard_threshold",
}

REMOVED_CONFIG_KEYS: dict[str, str] = {
    "memos": "legacy external Memos integration config was removed",
    "persona.data_sources.memos.enabled": "external Memos persona source was removed",
    "distill.allow_host_agent_delegate": "host-agent delegation is no longer a runtime switch",
    "distill.pre_distill_gate.verbose_layers": "pre-distill verbose layer switch was removed",
    "privacy.encryption": "whole-file SQLite encryption config was removed",
    "privacy.encryption.enabled": "whole-file SQLite encryption config was removed",
    "privacy.encryption.key_source": "whole-file SQLite encryption config was removed",
    "privacy.encryption.databases": "whole-file SQLite encryption config was removed",
    "privacy.encryption.accept_plaintext_risk": "whole-file SQLite encryption config was removed",
    "daemon.initial_delays.distill_merge": "daemon initial-delay config was removed",
    "daemon.initial_delays.event_bus": "daemon initial-delay config was removed",
    "sync.l1_scan_max_sessions_per_session": "replaced by durable source reconciliation",
    "sync.l1_scan_max_sources_per_cycle": "all active sources are discovered every cycle",
    "sync.l1_scan_poll_interval_seconds": "daemon raw_sync interval owns scheduling",
    "sync.l1_scan_recent_hours": "time-window completion semantics were removed",
    "capture.duplicate_ttl_days": (
        "capture idempotency is permanent by canonical Raw revision; queue payload retention is separate"
    ),
    "distill.skill_suggestion_max_chars": (
        "COG-013 removed the anonymous truncated suggestion input; proposals derive from the full asset"
    ),
    "distill.max_collect_per_cycle": (
        "COG-028 removed the external distill_output collector; the synchronous worker is the sole owner"
    ),
    "storage.retention_days.prompt_calls": (
        "replaced by storage.retention_days.model_call_ledger; billable model calls have one canonical ledger"
    ),
    "quality_gate.prompt_call_log": (
        "post-hoc prompt logging was removed; ModelCallLedger never persists prompt or response bodies"
    ),
    "watchers.raw_vault.enabled": (
        "Raw Markdown is a projection and cannot be an automatic ingestion source"
    ),
    "watchers.raw_vault.poll_interval_seconds": (
        "the retired Raw projection watcher no longer owns a polling interval"
    ),
    "watchers.raw_vault.watch_dir": (
        "the retired Raw projection watcher no longer owns a watched directory"
    ),
    "daemon.services.raw_vault_watch": (
        "canonical Raw capture and projection replaced the Raw vault watcher service"
    ),
    "daemon.services.persona_extensions": (
        "COG-032 removed the empty-context persona extension service; "
        "the canonical persona_challenge service requires a real decision context"
    ),
}

# These keys existed in historical main.json files but have no current reader.
# Keeping the lifecycle list here makes removal auditable and prevents a stale
# value from silently becoming a new runtime contract later.
_UNREAD_LEGACY_CONFIG_KEYS = {
    "app.dispute_escalation_intensity",
    "app.dispute_stale_days",
    "app.freshness_alert_days",
    "app.push_cooldown_minutes",
    "app.push_penalty_multipliers",
    "app.search_max_results",
    "cognitive_graph.entity_link_threshold",
    "cognitive_graph.reconcile_interval_seconds",
    "daemon.initial_delays.capture_worker",
    "daemon.initial_delays.dispute_scan",
    "daemon.initial_delays.distill_and_merge",
    "daemon.initial_delays.eventbus",
    "daemon.initial_delays.heartbeat",
    "daemon.initial_delays.inbox_scanner",
    "daemon.initial_delays.l1_sync",
    "daemon.initial_delays.persona_analyzer",
    "daemon.initial_delays.reminder_scan",
    "daemon.initial_delays.signal_collector",
    "daemon.services.claude_live_sync",
    "daemon.startup_stagger_seconds",
    "distill.aggregate_threshold",
    "distill.deferred_max_days",
    "distill.pre_distill_gate.enabled",
    "distill.provider",
    "distill.similarity_dedup_threshold",
    "distill.single_threshold",
    "distill.strategy",
    "embedding.index_interval_hours",
    "embedding.similarity_threshold",
    "embedding.ttl_days",
    "event_bus.poll_interval_seconds",
    "knowledge_graph.entity_quality_threshold",
    "knowledge_graph.freshness_deprecated_threshold",
    "knowledge_graph.relation_confidence_strong",
    "knowledge_graph.relation_confidence_weak",
    "knowledge_graph.vector_dim",
    "knowledge_graph.vector_index_init_capacity",
    "observation.max_per_cycle",
    "ops.backup_retention_days",
    "ops.default_timeout",
    "ops.health_check_interval",
    "ops.heartbeat_interval_seconds",
    "ops.inbox_scan_interval_seconds",
    "ops.persona_analysis_interval_seconds",
    "ops.persona_full_analysis_interval_seconds",
    "ops.save_interval_sec",
    "persona.data_sources.memos",
    "persona.max_signals_per_agent_per_cycle",
    "persona.signal_collector_agent_pause_seconds",
    "persona_engine.blind_spot_min_queries",
    "persona_engine.interest_decay_half_life_days",
    "persona_engine.preference_likelihood.ignore",
    "persona_engine.preference_likelihood.save",
    "persona_engine.preference_likelihood.search",
    "persona_engine.preference_likelihood.share",
    "raw_event_store.purge_backup_days",
    "reminder.banner_scan_max_pages",
    "scheduler.beat_seconds",
    "scoring.feedback_fatigue_ignore_cooldown_hours",
    "scoring.feedback_fatigue_max_daily",
    "scoring.feedback_fatigue_min_interval_minutes",
    "scoring.model_version_keep",
    "scoring.retrain_buffer",
    "scoring.retrain_interval_seconds",
    "scoring.threshold",
    "storage.obsidian.chatlog_subdir",
    "sync.backfill_fill_gaps",
    "sync.backfill_skip_stale_hours",
    "sync.debounce_stable_reads",
    "sync.interval_seconds",
    "sync.l1_scan_max_file_bytes",
    "sync.noise_threshold",
    "sync.polling_interval_openclaw",
    "system.archive_days",
    "system.feedback_min_interval",
    "system.index_rebuild_interval",
    "system.retention_days",
    "system.trigger_buffer_size",
    "wiki.subdirs",
}
REMOVED_CONFIG_KEYS.update(
    {key: "legacy config key has no current runtime reader" for key in _UNREAD_LEGACY_CONFIG_KEYS}
)

# Values of ``None`` are process-only variables resolved outside the persisted
# config tree. They remain registered here so env ownership is still explicit.
ENV_OVERRIDES: dict[str, str | None] = {
    "MNEMOS_WIKI_DIR": "wiki.vault_path",
    "WIKI_DIR": "wiki.vault_path",
    "MNEMOS_DIR": None,
    "MNEMOS_DATABASE_DIR": None,
    "MNEMOS_PREFLIGHT_TIMEOUT_SEC": "preflight.timeout_sec",
    "MNEMOS_MCP_LAUNCH_CAPABILITY_REF": None,
    "MNEMOS_WIKI_INDEX_CACHE_TTL": "oracle.index_cache_ttl_seconds",
    "MNEMOS_OBSIDIAN_CACHE_TTL": "storage.obsidian.scan_cache_ttl_seconds",
    "MNEMOS_OBSIDIAN_CACHE_MAX_ENTRIES": "storage.obsidian.scan_cache_max_entries",
    "MNEMOS_RETENTION_DAYS_OBSERVATIONS": "storage.retention_days.observations",
    "MNEMOS_RETENTION_DAYS_REFLECTIONS": "storage.retention_days.reflections",
    "MNEMOS_RETENTION_DAYS_USER_SIGNALS": "storage.retention_days.user_signals",
    "MNEMOS_RETENTION_DAYS_APPLICATION_SIGNALS": "storage.retention_days.application_signals",
    "MNEMOS_RETENTION_DAYS_KNOWLEDGE_GRAPH": "storage.retention_days.knowledge_graph",
    "MNEMOS_RETENTION_DAYS_WIKI_METRICS_QUERY_LOG": "storage.retention_days.wiki_metrics_query_log",
    "MNEMOS_RETENTION_DAYS_MNEMOS_SEARCH_SESSIONS": "storage.retention_days.mnemos_search_sessions",
    "MNEMOS_RETENTION_DAYS_LINK_PROBE_QUEUE": "storage.retention_days.link_probe_queue",
    "MNEMOS_RETENTION_DAYS_MODEL_CALL_LEDGER": "storage.retention_days.model_call_ledger",
    "MNEMOS_RETENTION_DAYS_DISTILLATION_CHUNKS": "storage.retention_days.distillation_chunks",
    "MNEMOS_PUSH_INDEX_CACHE_TTL": "push.index_cache_ttl_seconds",
    "MNEMOS_PERSONA_AB_TEST": "persona.ab_test_enabled",
    "MNEMOS_PERFORMANCE_TIER": "performance_tier",
    "L1_STORAGE_API_URL": "l1_storage.api_url",
    "L1_STORAGE_TOKEN": "l1_storage.token",
    "CLAUDE_SETTINGS_JSON": "integrations.claude_code.settings_json_path",
}


def _is_secret_key(key: str) -> bool:
    tail = key.rsplit(".", 1)[-1].lower()
    return tail in {"api_key", "token", "secret", "password", "credential", "bearer"}


def _value_types(default: Any) -> tuple[type, ...]:
    if default is None:
        return (type(None), str)
    if isinstance(default, float):
        return (float,)
    return (type(default),)


class ConfigRegistry:
    """Typed, versioned owner of canonical keys, env mappings, and aliases."""

    schema_version = SCHEMA_VERSION

    def __init__(self) -> None:
        self._specs: dict[str, ConfigKeySpec] = {}
        self._defaults: Mapping[str, Any] = {}
        self._bound = False
        self._performance_tiers: dict[str, Mapping[str, Any]] = {}
        self.aliases = dict(CONFIG_KEY_ALIASES)
        self.removed_keys = dict(REMOVED_CONFIG_KEYS)
        self.env_overrides = {
            name: target for name, target in ENV_OVERRIDES.items() if target is not None
        }
        self.env_targets = dict(ENV_OVERRIDES)

    @staticmethod
    def flatten_tree(value: Any, prefix: str = "") -> dict[str, Any]:
        result: dict[str, Any] = {}
        if isinstance(value, Mapping):
            if prefix and not value:
                result[prefix] = {}
                return result
            for key, child in value.items():
                child_key = f"{prefix}.{key}" if prefix else str(key)
                result.update(ConfigRegistry.flatten_tree(child, child_key))
            return result
        result[prefix] = value
        return result

    def bind_defaults(
        self,
        defaults: Mapping[str, Any],
        performance_tiers: Mapping[str, Mapping[str, Any]],
    ) -> None:
        flattened = self.flatten_tree(defaults)
        specs = {
            key: ConfigKeySpec(
                key=key,
                default=value,
                value_types=_value_types(value),
                secret=_is_secret_key(key),
                open_mapping=isinstance(value, Mapping) and not value,
            )
            for key, value in flattened.items()
        }
        # Provider names are user-extensible. Known built-ins retain typed
        # leaf specs while this branch permits additional provider mappings.
        providers = defaults.get("llm", {}).get("providers", {})
        specs["llm.providers"] = ConfigKeySpec(
            key="llm.providers",
            default=dict(providers),
            value_types=(dict,),
            secret=False,
            open_mapping=True,
        )
        self._specs = specs
        self._defaults = defaults
        self._performance_tiers = dict(performance_tiers)
        self._bound = True
        errors: list[ConfigValidationIssue] = []
        for tier, overrides in performance_tiers.items():
            errors.extend(self.validate_override_tree(overrides, source=f"performance_tier:{tier}"))
        if errors:
            raise ConfigValidationError(errors)
        for env_name, target in self.env_overrides.items():
            if target not in self._specs:
                raise ConfigValidationError(
                    [ConfigValidationIssue("unknown_env_target", target, f"env:{env_name}")]
                )
        for alias, canonical in self.aliases.items():
            if canonical not in self._specs:
                raise ConfigValidationError(
                    [ConfigValidationIssue("unknown_alias_target", alias, "registry")]
                )

    @property
    def key_count(self) -> int:
        return len(self._specs)

    def keys(self) -> set[str]:
        return set(self._specs)

    def keys_present_in_tree(self, tree: Mapping[str, Any]) -> set[str]:
        """Return canonical entries represented by a config/document tree."""
        keys = set(self.flatten_tree(tree))
        for key, spec in self._specs.items():
            if spec.open_mapping and self._get_dotted(tree, key)[0]:
                keys.add(key)
        return keys

    def canonical_key(self, key: str) -> str:
        return self.aliases.get(key, key)

    @staticmethod
    def _get_dotted(tree: Mapping[str, Any], key: str) -> tuple[bool, Any]:
        current: Any = tree
        for part in key.split("."):
            if not isinstance(current, Mapping) or part not in current:
                return False, None
            current = current[part]
        return True, current

    @staticmethod
    def _set_dotted(tree: dict[str, Any], key: str, value: Any) -> None:
        current = tree
        parts = key.split(".")
        for part in parts[:-1]:
            child = current.get(part)
            if not isinstance(child, dict):
                child = {}
                current[part] = child
            current = child
        current[parts[-1]] = value

    @staticmethod
    def _delete_dotted(tree: dict[str, Any], key: str) -> bool:
        current: Any = tree
        parents: list[tuple[dict[str, Any], str]] = []
        parts = key.split(".")
        for part in parts[:-1]:
            if not isinstance(current, dict) or not isinstance(current.get(part), dict):
                return False
            parents.append((current, part))
            current = current[part]
        if not isinstance(current, dict) or parts[-1] not in current:
            return False
        del current[parts[-1]]
        for parent, part in reversed(parents):
            child = parent.get(part)
            if isinstance(child, dict) and not child:
                del parent[part]
            else:
                break
        return True

    def migrate_aliases(
        self, tree: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, str], tuple[str, ...]]:
        """Rewrite persisted aliases once; runtime reads never accept them.

        Canonical values win when both names are present.  The returned
        metadata distinguishes migrated aliases from discarded conflicts.
        """
        result = deepcopy(dict(tree))
        migrated: dict[str, str] = {}
        conflicts: list[str] = []
        for alias, canonical in sorted(self.aliases.items()):
            alias_exists, alias_value = self._get_dotted(result, alias)
            if not alias_exists:
                continue
            canonical_exists, _ = self._get_dotted(result, canonical)
            if canonical_exists:
                conflicts.append(alias)
            else:
                self._set_dotted(result, canonical, alias_value)
                migrated[alias] = canonical
            self._delete_dotted(result, alias)
        return result, migrated, tuple(conflicts)

    def _open_mapping_spec(self, key: str) -> ConfigKeySpec | None:
        parts = key.split(".")
        for end in range(len(parts) - 1, 0, -1):
            spec = self._specs.get(".".join(parts[:end]))
            if spec is not None and spec.open_mapping:
                return spec
        return None

    def _branch_spec(self, key: str) -> ConfigKeySpec | None:
        prefix = key + "."
        if not any(item.startswith(prefix) for item in self._specs):
            return None
        current: Any = self._defaults
        for part in key.split("."):
            if not isinstance(current, Mapping) or part not in current:
                return None
            current = current[part]
        if not isinstance(current, Mapping):
            return None
        return ConfigKeySpec(
            key=key,
            default=dict(current),
            value_types=(dict,),
            secret=False,
            open_mapping=False,
        )

    def require(self, key: str) -> ConfigKeySpec:
        if key in self.removed_keys:
            raise UnknownConfigKeyError(f"removed config key: {key}")
        if key in self.aliases:
            raise UnknownConfigKeyError(f"deprecated config alias: {key}; use {self.aliases[key]}")
        canonical = key
        spec = (
            self._specs.get(canonical)
            or self._open_mapping_spec(canonical)
            or self._branch_spec(canonical)
        )
        if spec is None:
            raise UnknownConfigKeyError(f"unknown config key: {key}")
        return spec

    def _flatten_override(self, value: Any, prefix: str = "") -> dict[str, Any]:
        if prefix in self.removed_keys or prefix in self.aliases:
            return {prefix: value}
        if prefix:
            spec = self._specs.get(prefix)
            if (
                spec is not None
                and spec.open_mapping
                and (not isinstance(value, Mapping) or not value)
            ):
                return {prefix: value}
        if isinstance(value, Mapping):
            if prefix and not value:
                return {prefix: {}}
            result: dict[str, Any] = {}
            for key, child in value.items():
                child_key = f"{prefix}.{key}" if prefix else str(key)
                result.update(self._flatten_override(child, child_key))
            return result
        return {prefix: value}

    def flatten_override(self, value: Mapping[str, Any]) -> dict[str, Any]:
        """Flatten an override using open-mapping boundaries from the registry."""
        return self._flatten_override(value)

    def validate_flat_values(
        self,
        values: Mapping[str, Any],
        *,
        source: str = "runtime",
    ) -> list[ConfigValidationIssue]:
        issues: list[ConfigValidationIssue] = []
        for key, value in values.items():
            if key in self.removed_keys:
                issues.append(ConfigValidationIssue("removed_key", key, source))
                continue
            if key in self.aliases:
                issues.append(ConfigValidationIssue("alias_key", key, source))
                continue
            try:
                spec = self.require(key)
            except UnknownConfigKeyError:
                issues.append(ConfigValidationIssue("unknown_key", key, source))
                continue
            if spec.open_mapping and key != spec.key:
                continue
            if not spec.accepts(value):
                issues.append(
                    ConfigValidationIssue(
                        "invalid_type",
                        key,
                        source,
                        expected_type=spec.value_type_name,
                        actual_type=type(value).__name__,
                    )
                )
        return issues

    def coerce_runtime_value(self, key: str, value: Any, *, source: str) -> Any:
        """Apply the registry's only lossless runtime coercion, then validate."""
        spec = self.require(key)
        if (
            spec.value_types == (int,)
            and isinstance(value, float)
            and not isinstance(value, bool)
            and value.is_integer()
        ):
            value = int(value)
        if not spec.accepts(value):
            raise ConfigValidationError(
                [
                    ConfigValidationIssue(
                        "invalid_type",
                        key,
                        source,
                        expected_type=spec.value_type_name,
                        actual_type=type(value).__name__,
                    )
                ]
            )
        return value

    def validate_override_tree(
        self,
        tree: Mapping[str, Any],
        *,
        source: str,
    ) -> list[ConfigValidationIssue]:
        return self.validate_flat_values(self._flatten_override(tree), source=source)

    def assert_valid_override_tree(self, tree: Mapping[str, Any], *, source: str) -> None:
        errors = self.validate_override_tree(tree, source=source)
        if errors:
            raise ConfigValidationError(errors)

    def fingerprint(self, data: Mapping[str, Any]) -> str:
        canonical = self._flatten_override(data)
        raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, default=str)
        return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def report(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "key_count": self.key_count,
            "keys": [
                {
                    "key": spec.key,
                    "type": spec.value_type_name,
                    "secret": spec.secret,
                    "open_mapping": spec.open_mapping,
                    "example_documented": spec.example_documented,
                    "tested": spec.tested,
                    "documented": spec.documented,
                }
                for spec in sorted(self._specs.values(), key=lambda item: item.key)
            ],
            "env_overrides": dict(sorted(self.env_targets.items())),
            "aliases": dict(sorted(self.aliases.items())),
            "removed_keys": dict(sorted(self.removed_keys.items())),
        }


CONFIG_REGISTRY = ConfigRegistry()
