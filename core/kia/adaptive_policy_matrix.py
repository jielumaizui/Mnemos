"""Adaptive policy coverage contract.

This module is the single source for built-in adaptive rules and the coverage
matrix that explains how each rule is produced, consumed, and rolled back.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

SCHEMA_VERSION = "mnemos.adaptive_policy_coverage.v1"


@dataclass(frozen=True)
class AdaptivePolicyCoverage:
    id: str
    domain: str
    config_key: str
    metric: str
    input_signal: str
    producer: str
    consumer: str
    effective_read_entry: str
    rollback_metric: str
    acceptance_metric: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


DEFAULT_ADAPTIVE_POLICY_RULES: list[dict[str, Any]] = [
    {
        "config_key": "distill.trigger_threshold",
        "metric": "distill.false_positive_rate",
        "threshold_high": 0.3,
        "threshold_low": 0.05,
        "adjust_up": 0.05,
        "adjust_down": -0.05,
        "min_value": 0.1,
        "max_value": 0.8,
    },
    {
        "config_key": "distill.min_session_fragment_pass_ratio",
        "metric": "distill.fragment_reject_rate",
        "threshold_high": 0.35,
        "threshold_low": 0.05,
        "adjust_up": -0.05,
        "adjust_down": 0.05,
        "min_value": 0.2,
        "max_value": 0.9,
    },
    {
        "config_key": "quality_gate.base_threshold",
        "metric": "quality_gate.rejection_rate",
        "threshold_high": 0.4,
        "threshold_low": 0.05,
        "adjust_up": -0.03,
        "adjust_down": 0.03,
        "min_value": 0.35,
        "max_value": 0.85,
    },
    {
        "config_key": "quality_gate.review_margin",
        "metric": "quality_gate.review_rate",
        "threshold_high": 0.35,
        "threshold_low": 0.05,
        "adjust_up": -0.02,
        "adjust_down": 0.02,
        "min_value": 0.05,
        "max_value": 0.35,
    },
    {
        "config_key": "scoring.min_samples_per_dimension",
        "metric": "scoring.feedback_rate",
        "threshold_high": 0.8,
        "threshold_low": 0.2,
        "adjust_up": 5,
        "adjust_down": -5,
        "min_value": 5,
        "max_value": 100,
    },
    {
        "config_key": "app.push_max_items",
        "metric": "app.push_ignore_rate",
        "threshold_high": 0.5,
        "threshold_low": 0.1,
        "adjust_up": -1,
        "adjust_down": 1,
        "min_value": 1,
        "max_value": 10,
    },
    {
        "config_key": "knowledge_graph.freshness_decay_half_life_days",
        "metric": "knowledge_graph.stale_page_rate",
        "threshold_high": 0.4,
        "threshold_low": 0.1,
        "adjust_up": -7,
        "adjust_down": 7,
        "min_value": 7,
        "max_value": 90,
    },
    {
        "config_key": "raw_event_store.retention_days",
        "metric": "raw.partial_rate",
        "threshold_high": 0.25,
        "threshold_low": 0.02,
        "adjust_up": 7,
        "adjust_down": -7,
        "min_value": 7,
        "max_value": 180,
    },
    {
        "config_key": "document_process.max_file_size_mb",
        "metric": "document_process.rejection_rate",
        "threshold_high": 0.25,
        "threshold_low": 0.02,
        "adjust_up": 20,
        "adjust_down": -10,
        "min_value": 10,
        "max_value": 300,
    },
    {
        "config_key": "intent_router.llm_fallback_threshold",
        "metric": "intent.low_confidence_route_rate",
        "threshold_high": 0.3,
        "threshold_low": 0.05,
        "adjust_up": -0.03,
        "adjust_down": 0.03,
        "min_value": 0.45,
        "max_value": 0.9,
    },
    {
        "config_key": "trust.min_delivery_score",
        "metric": "delivery.dismiss_rate",
        "threshold_high": 0.3,
        "threshold_low": 0.05,
        "adjust_up": 0.03,
        "adjust_down": -0.03,
        "min_value": 0.35,
        "max_value": 0.85,
    },
]


ADAPTIVE_POLICY_COVERAGE: tuple[AdaptivePolicyCoverage, ...] = (
    AdaptivePolicyCoverage(
        id="distill_trigger_threshold",
        domain="distill",
        config_key="distill.trigger_threshold",
        metric="distill.false_positive_rate",
        input_signal="distillation rejection and user correction outcomes",
        producer="daemon.adaptive_service / explicit AdaptiveConfig.record_usage",
        consumer="core/scoring/scorers/distill_scorer_v2.py",
        effective_read_entry="EffectivePolicy.get('distill.trigger_threshold')",
        rollback_metric="distill.false_positive_rate",
        acceptance_metric="lower false-positive rate without distill backlog growth",
    ),
    AdaptivePolicyCoverage(
        id="distill_fragment_pass_ratio",
        domain="distill",
        config_key="distill.min_session_fragment_pass_ratio",
        metric="distill.fragment_reject_rate",
        input_signal="fragment pass/reject ratios from distillation runs",
        producer="daemon.adaptive_service / explicit AdaptiveConfig.record_usage",
        consumer="core/hephaestus/distillation_engine.py",
        effective_read_entry=(
            "get_shadowed_value('distill.min_session_fragment_pass_ratio') "
            "-> EffectivePolicy.get when active shadow exists"
        ),
        rollback_metric="distill.fragment_reject_rate",
        acceptance_metric="fragment reject rate decreases without accepting empty fragments",
    ),
    AdaptivePolicyCoverage(
        id="quality_gate_base_threshold",
        domain="quality_gate",
        config_key="quality_gate.base_threshold",
        metric="quality_gate.rejection_rate",
        input_signal="quality gate reject ratio",
        producer="daemon.adaptive_service / distill action ledger",
        consumer="core/hephaestus/distillation_quality.py",
        effective_read_entry=(
            "get_shadowed_value('quality_gate.base_threshold') "
            "-> EffectivePolicy.get when active shadow exists"
        ),
        rollback_metric="quality_gate.rejection_rate",
        acceptance_metric="reject/review ratio stabilizes while wiki quality budget holds",
    ),
    AdaptivePolicyCoverage(
        id="quality_gate_review_margin",
        domain="quality_gate",
        config_key="quality_gate.review_margin",
        metric="quality_gate.review_rate",
        input_signal="quality gate review ratio",
        producer="daemon.adaptive_service / distill action ledger",
        consumer="core/hephaestus/distillation_quality.py",
        effective_read_entry=(
            "get_shadowed_value('quality_gate.review_margin') "
            "-> EffectivePolicy.get when active shadow exists"
        ),
        rollback_metric="quality_gate.review_rate",
        acceptance_metric="manual review load decreases without reject-rate spike",
    ),
    AdaptivePolicyCoverage(
        id="scoring_min_samples",
        domain="scoring",
        config_key="scoring.min_samples_per_dimension",
        metric="scoring.feedback_rate",
        input_signal="ground truth and search feedback samples",
        producer="daemon.adaptive_service / AdaptiveScorerV2",
        consumer="core/scoring/adaptive_scorer_v2.py",
        effective_read_entry="EffectivePolicy.get('scoring.min_samples_per_dimension')",
        rollback_metric="scoring.feedback_rate",
        acceptance_metric="sufficient samples before model changes are consumed",
    ),
    AdaptivePolicyCoverage(
        id="delivery_push_max_items",
        domain="delivery",
        config_key="app.push_max_items",
        metric="app.push_ignore_rate",
        input_signal="push ignore/dismiss feedback",
        producer="daemon.adaptive_service / delivery_events",
        consumer="core/cognitive/delivery_router.py and core/kia/dialog_reminder.py",
        effective_read_entry=(
            "get_shadowed_value('app.push_max_items') "
            "-> EffectivePolicy.get when active shadow exists"
        ),
        rollback_metric="app.push_ignore_rate",
        acceptance_metric="ignore rate falls without starving accepted pushes",
    ),
    AdaptivePolicyCoverage(
        id="search_freshness_half_life",
        domain="search",
        config_key="knowledge_graph.freshness_decay_half_life_days",
        metric="knowledge_graph.stale_page_rate",
        input_signal="freshness alerts and stale page outcomes",
        producer="daemon.adaptive_service / wiki_state.db",
        consumer="core/app/context_search.py and core/kia/proteus.py",
        effective_read_entry="EffectivePolicy.get('knowledge_graph.freshness_decay_half_life_days')",
        rollback_metric="knowledge_graph.stale_page_rate",
        acceptance_metric="stale-page search exposure decreases without freshness noise",
    ),
    AdaptivePolicyCoverage(
        id="raw_retention_days",
        domain="raw",
        config_key="raw_event_store.retention_days",
        metric="raw.partial_rate",
        input_signal="raw completeness status distribution",
        producer="daemon.adaptive_service / raw_events.db",
        consumer="core/sync_framework/raw_event_store.py",
        effective_read_entry=(
            "get_shadowed_value('raw_event_store.retention_days') "
            "-> EffectivePolicy.get when active shadow exists"
        ),
        rollback_metric="raw.partial_rate",
        acceptance_metric="partial raw ratio decreases before purge pressure increases",
    ),
    AdaptivePolicyCoverage(
        id="document_max_file_size",
        domain="document_process",
        config_key="document_process.max_file_size_mb",
        metric="document_process.rejection_rate",
        input_signal="document ingestion rejection/review outcomes",
        producer="daemon.adaptive_service / rejected_documents",
        consumer=(
            "core/document_import.py validate_trusted_user_document; "
            "core/application/document_import_service.py; "
            "core/sync_framework/file_ingestor.py; "
            "core/kia/knowledge_inbox.py"
        ),
        effective_read_entry=(
            "document_max_file_size_mb() -> "
            "get_shadowed_value('document_process.max_file_size_mb') when active shadow exists"
        ),
        rollback_metric="document_process.rejection_rate",
        acceptance_metric="file-size rejections fall without parse failures increasing",
    ),
    AdaptivePolicyCoverage(
        id="intent_llm_fallback_threshold",
        domain="intent",
        config_key="intent_router.llm_fallback_threshold",
        metric="intent.low_confidence_route_rate",
        input_signal="intent route corrections and low-confidence classifications",
        producer="intent_route / explicit AdaptiveConfig.record_usage",
        consumer="core/app/intent_router.py",
        effective_read_entry=(
            "get_shadowed_value('intent_router.llm_fallback_threshold') "
            "-> EffectivePolicy.get when active shadow exists"
        ),
        rollback_metric="intent.low_confidence_route_rate",
        acceptance_metric="correction rate falls without extra fallback latency",
    ),
    AdaptivePolicyCoverage(
        id="trust_min_delivery_score",
        domain="cognitive_decision",
        config_key="trust.min_delivery_score",
        metric="delivery.dismiss_rate",
        input_signal="delivery dismiss/ignore outcomes",
        producer="daemon.adaptive_service / delivery_events",
        consumer="core/cognitive/trust_scorer.py",
        effective_read_entry=(
            "get_shadowed_value('trust.min_delivery_score') "
            "-> EffectivePolicy.get when active shadow exists"
        ),
        rollback_metric="delivery.dismiss_rate",
        acceptance_metric="dismiss rate falls without hiding useful knowledge pushes",
    ),
)


REQUIRED_ADAPTIVE_DOMAINS = {
    "distill",
    "quality_gate",
    "scoring",
    "delivery",
    "search",
    "raw",
    "document_process",
    "intent",
    "cognitive_decision",
}


def coverage_rows() -> list[dict[str, str]]:
    return [item.to_dict() for item in ADAPTIVE_POLICY_COVERAGE]


def coverage_by_rule() -> dict[tuple[str, str], AdaptivePolicyCoverage]:
    return {(item.config_key, item.metric): item for item in ADAPTIVE_POLICY_COVERAGE}


def audit_adaptive_policy_coverage(*, strict: bool = True) -> list[str]:
    errors: list[str] = []
    coverage_pairs = set(coverage_by_rule())
    rule_pairs = {(str(rule["config_key"]), str(rule["metric"])) for rule in DEFAULT_ADAPTIVE_POLICY_RULES}

    missing_coverage = sorted(rule_pairs - coverage_pairs)
    for config_key, metric in missing_coverage:
        errors.append(f"rule missing coverage row: {config_key} / {metric}")

    orphan_coverage = sorted(coverage_pairs - rule_pairs)
    for config_key, metric in orphan_coverage:
        errors.append(f"coverage row missing runtime rule: {config_key} / {metric}")

    for row in ADAPTIVE_POLICY_COVERAGE:
        for field_name, value in row.to_dict().items():
            if not str(value).strip():
                errors.append(f"{row.id} has empty field: {field_name}")
        if strict and not (
            "EffectivePolicy.get(" in row.effective_read_entry
            or "get_shadowed_value(" in row.effective_read_entry
        ):
            errors.append(f"{row.id} must document an EffectivePolicy read entry")

    domains = {row.domain for row in ADAPTIVE_POLICY_COVERAGE}
    for domain in sorted(REQUIRED_ADAPTIVE_DOMAINS - domains):
        errors.append(f"missing adaptive coverage domain: {domain}")

    return errors


def build_adaptive_policy_report() -> dict[str, Any]:
    errors = audit_adaptive_policy_coverage(strict=True)
    domains = sorted({row.domain for row in ADAPTIVE_POLICY_COVERAGE})
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": not errors,
        "coverage_count": len(ADAPTIVE_POLICY_COVERAGE),
        "rule_count": len(DEFAULT_ADAPTIVE_POLICY_RULES),
        "domains": domains,
        "errors": errors,
        "coverage": coverage_rows(),
    }
