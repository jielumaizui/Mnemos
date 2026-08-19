"""Read-only cognitive readiness audit for Mnemos runtime data."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, cast

from core.db_utils import sqlite_artifact_exists
from core.db_utils import validate_sql_identifier
from core.ops.cognitive_readiness_lineage import (
    build_learning_signal_metrics,
    delivery_outcome_metric,
    observation_lineage_metric,
)
from core.ops.readiness_query_budget import connect_readonly_sqlite
from core.wiki_page_roles import source_exempt_reason

SCHEMA_VERSION = "mnemos.cognitive_readiness.v2"
DEFAULT_FRESHNESS_WINDOW_SECONDS = 30 * 86400
EMPTY_JSON_VALUES = ("", "[]", "{}", "null", "None")
SOURCE_EXEMPT_SAMPLE_LIMIT = 10
READINESS_SECTIONS = ("source", "evidence", "consumer", "behavior")
READINESS_WHERE_CLAUSES = frozenset(
    {
        "COALESCE(source_count, 0) = 0",
        "status = 'resolved' AND choice IS NOT NULL AND choice != ''",
        "resolved_choice IS NOT NULL AND TRIM(resolved_choice) != ''",
        "clicked_path IS NOT NULL AND TRIM(clicked_path) != ''",
        "opened_path IS NOT NULL AND TRIM(opened_path) != ''",
        "ignored_at IS NOT NULL AND TRIM(ignored_at) != ''",
        "outcome_status = 'no_result'",
    }
)

READINESS_POLICIES: dict[str, dict[str, Any]] = {
    "raw_events_missing": {
        "section": "source",
        "owner": "core.sync_framework",
        "failure_class": "source_unavailable",
        "lifecycle_status": "blocked",
        "repair_action": "run capture ingestion and verify raw_events.db/raw_turns",
        "budget": 0,
    },
    "page_metrics_missing": {
        "section": "source",
        "owner": "core.wiki_metrics",
        "failure_class": "metrics_unavailable",
        "lifecycle_status": "blocked",
        "repair_action": "rebuild wiki metrics before scoring cognitive readiness",
        "budget": 0,
    },
    "page_source_gap": {
        "section": "source",
        "owner": "hephaestus.wiki_builder",
        "failure_class": "source_refs_missing",
        "lifecycle_status": "degraded",
        "repair_action": "mnemos distill evidence-backfill --apply",
        "metric_path": ("page_metrics", "source_count_zero"),
        "budget": 0,
    },
    "page_source_refs_gap": {
        "section": "evidence",
        "owner": "core.ops.evidence_backfill",
        "failure_class": "evidence_refs_empty",
        "lifecycle_status": "degraded",
        "repair_action": "backfill non-empty source_refs for pages with positive source_count",
        "metric_path": ("page_metrics", "source_count_positive_empty_refs"),
        "budget": 0,
    },
    "wiki_source_ref_gap": {
        "section": "evidence",
        "owner": "hephaestus.wiki_builder",
        "failure_class": "wiki_page_without_source",
        "lifecycle_status": "degraded",
        "repair_action": "rebuild wiki pages with frontmatter/source_refs coverage",
        "metric_path": ("wiki_pages", "missing_source_refs"),
        "budget": 0,
    },
    "delivery_events_missing": {
        "section": "consumer",
        "owner": "core.cognitive.delivery_router",
        "failure_class": "consumer_ledger_missing",
        "lifecycle_status": "blocked",
        "repair_action": "run delivery router migration and verify delivery_events.db",
        "budget": 0,
    },
    "cognitive_outcomes_missing": {
        "section": "consumer",
        "owner": "core.cognitive.delivery_router",
        "failure_class": "outcome_ledger_missing",
        "lifecycle_status": "blocked",
        "repair_action": "run outcome recorder migration and verify cognitive_outcomes table",
        "budget": 0,
    },
    "required_evidence_unavailable": {
        "section": "consumer",
        "owner": "core.ops.cognitive_readiness",
        "failure_class": "required_evidence_unavailable",
        "lifecycle_status": "blocked",
        "repair_action": "initialize the canonical observation, policy, and consolidation ledgers",
        "metric_path": ("learning_signal", "required_tables_missing_count"),
        "budget": 0,
    },
    "required_evidence_empty": {
        "section": "source",
        "owner": "core.ops.cognitive_readiness",
        "failure_class": "required_evidence_unobserved",
        "lifecycle_status": "degraded",
        "repair_action": "produce real source, delivery, outcome, and lineage evidence before scoring",
        "metric_path": ("learning_signal", "required_evidence_empty_count"),
        "budget": 0,
    },
    "search_outcome_schema_gap": {
        "section": "behavior",
        "owner": "core.scoring.adaptive_scorer_v2",
        "failure_class": "behavior_schema_incomplete",
        "lifecycle_status": "degraded",
        "repair_action": "call AdaptiveScorerV2.ensure_tables() to add search outcome fields",
        "budget": 0,
    },
    "search_behavior_gap": {
        "section": "behavior",
        "owner": "core.app.context_search",
        "failure_class": "behavior_outcome_missing",
        "lifecycle_status": "degraded",
        "repair_action": "record search click/open/ignore/no_result outcomes",
        "metric_path": ("search_sessions", "unclosed"),
        "budget": 0,
    },
    "search_click_gap": {
        "section": "behavior",
        "owner": "core.app.context_search",
        "failure_class": "positive_feedback_missing",
        "lifecycle_status": "degraded",
        "repair_action": "record search click/open when user opens a result",
        "metric_path": ("search_sessions", "clicked_gap"),
        "budget": 0,
    },
    "dialog_reminder_backlog": {
        "section": "behavior",
        "owner": "core.kia.dialog_reminder",
        "failure_class": "reminder_backlog",
        "lifecycle_status": "degraded",
        "repair_action": "resolve, ignore, defer, or expire pending dialog reminders",
        "metric_path": ("dialog_reminders", "pending"),
        "budget": 0,
    },
    "dialog_reminder_resolution_gap": {
        "section": "behavior",
        "owner": "core.kia.dialog_reminder",
        "failure_class": "reminder_outcome_missing",
        "lifecycle_status": "degraded",
        "repair_action": "record resolved/ignored/deferred reminder outcomes",
        "metric_path": ("dialog_reminders", "resolution_gap"),
        "budget": 0,
    },
    "learning_observation_output_gap": {
        "section": "consumer",
        "owner": "core.cognitive.observation_engine",
        "failure_class": "learning_signal_unconverted",
        "lifecycle_status": "degraded",
        "repair_action": "run observation engine and inspect zero-output reason before marking raw as processed",
        "metric_path": ("learning_signal", "observation_output_gap"),
        "budget": 0,
    },
    "learning_observation_all_skipped": {
        "section": "consumer",
        "owner": "core.cognitive.observation_engine",
        "failure_class": "eligible_raw_all_intentionally_skipped",
        "lifecycle_status": "degraded",
        "repair_action": (
            "audit no-observation reasons; at least one visible eligible Raw "
            "requires a real Observation effect"
        ),
        "metric_path": ("learning_signal", "all_visible_raw_skipped"),
        "budget": 0,
    },
    "learning_policy_patch_gap": {
        "section": "consumer",
        "owner": "core.cognitive.policy_patch",
        "failure_class": "policy_candidate_unrecorded",
        "lifecycle_status": "degraded",
        "repair_action": "route reflection/recap lessons through PolicyPatchStore or record explicit no-patch feedback",
        "metric_path": ("learning_signal", "policy_patch_gap"),
        "budget": 0,
    },
    "learning_consolidation_run_gap": {
        "section": "consumer",
        "owner": "core.cognitive.consolidator",
        "failure_class": "consolidation_run_missing",
        "lifecycle_status": "degraded",
        "repair_action": "apply a trusted cognitive consolidation with per-candidate coverage",
        "metric_path": ("learning_signal", "consolidation_run_gap"),
        "budget": 0,
    },
    "delivery_feedback_lineage_gap": {
        "section": "behavior",
        "owner": "core.cognitive.delivery_router",
        "failure_class": "delivery_effect_unproven",
        "lifecycle_status": "degraded",
        "repair_action": "record explicit feedback or an exact reciprocal delivery outcome link",
        "metric_path": ("learning_signal", "delivery_feedback_lineage_gap"),
        "budget": 0,
    },
    "observation_lineage_gap": {
        "section": "consumer",
        "owner": "core.cognitive.observation_engine",
        "failure_class": "raw_observation_lineage_missing",
        "lifecycle_status": "degraded",
        "repair_action": "persist exact raw event or current revision IDs on observations",
        "metric_path": ("learning_signal", "observation_lineage_gap"),
        "budget": 0,
    },
    "policy_driver_lineage_gap": {
        "section": "consumer",
        "owner": "core.cognitive.policy_patch",
        "failure_class": "policy_driver_effect_unproven",
        "lifecycle_status": "degraded",
        "repair_action": "link each reflection or recap driver to a patch or explicit no_patch outcome",
        "metric_path": ("learning_signal", "policy_driver_lineage_gap"),
        "budget": 0,
    },
    "consolidation_coverage_gap": {
        "section": "consumer",
        "owner": "core.cognitive.consolidator",
        "failure_class": "consolidation_candidate_unapplied",
        "lifecycle_status": "degraded",
        "repair_action": "apply a trusted consolidation and write per-candidate coverage rows",
        "metric_path": ("learning_signal", "consolidation_coverage_gap"),
        "budget": 0,
    },
    "learning_evidence_stale": {
        "section": "consumer",
        "owner": "core.ops.cognitive_readiness",
        "failure_class": "learning_evidence_stale",
        "lifecycle_status": "degraded",
        "repair_action": "produce fresh effect evidence inside the configured readiness window",
        "metric_path": ("learning_signal", "stale_lineage_count"),
        "budget": 0,
    },
    "learning_lineage_unobserved": {
        "section": "consumer",
        "owner": "core.ops.cognitive_readiness",
        "failure_class": "learning_lineage_unobserved",
        "lifecycle_status": "degraded",
        "repair_action": "produce at least one real denominator event for every required lineage",
        "metric_path": ("learning_signal", "unobserved_lineage_count"),
        "budget": 0,
    },
}


def build_cognitive_readiness_report(
    config: Any,
    *,
    strict: bool = False,
    enforce_budget: bool = False,
) -> dict[str, Any]:
    """Build a read-only report from configured Mnemos databases and vault paths."""
    database_dir = Path(getattr(config, "database_dir", Path.home() / ".mnemos"))
    wiki_dir = _configured_vault_path(config, attr="wiki_dir", vault_name="mnemos")
    raw_vault_dir = _configured_vault_path(config, attr="obsidian_vault_path", vault_name="raw")
    raw_events_db = _configured_db_path(
        config, "raw_event_store.db_path", database_dir / "raw_events.db"
    )
    delivery_db = _configured_db_path(
        config, "delivery.db_path", database_dir / "delivery_events.db"
    )
    observations_db = database_dir / "observations.db"
    policy_db = _configured_db_path(
        config, "policy_patch.db_path", database_dir / "policy_patches.db"
    )
    consolidation_db = _configured_db_path(
        config,
        "cognitive_consolidation.db_path",
        database_dir / "cognitive_consolidation.db",
    )
    freshness_window_seconds = max(
        1,
        _config_int(
            config,
            "cognitive_readiness.freshness_window_seconds",
            DEFAULT_FRESHNESS_WINDOW_SECONDS,
        ),
    )
    now = datetime.now(timezone.utc)
    delivery_lineage = delivery_outcome_metric(
        delivery_db,
        freshness_window_seconds=freshness_window_seconds,
        now=now,
    )

    wiki_metrics_db = database_dir / "wiki_metrics.db"
    page_roles = _page_metric_roles(wiki_metrics_db, wiki_dir)
    page_metrics = _page_metrics(wiki_metrics_db, wiki_dir)
    source_paths = _page_metric_source_paths(wiki_metrics_db, wiki_dir)
    metrics = {
        "page_metrics": page_metrics,
        "wiki_pages": _wiki_pages(wiki_dir, source_paths, page_roles),
        "raw_turns": _raw_turns(raw_events_db),
        "raw_retention": _raw_retention(raw_events_db),
        "distill_queue": _status_table(
            database_dir / "distill_queue.db",
            table="distillation_tasks",
            status_column="status",
        ),
        "knowledge_graph": _table_counts(
            database_dir / "knowledge_graph.db",
            ("entities", "relations", "relation_evidence"),
        ),
        "cognitive_graph": _table_counts(
            database_dir / "cognitive_graph.db",
            ("canonical_nodes", "cognitive_relations"),
        ),
        "evidence_graph": _table_counts(
            database_dir / "evidence_graph.db",
            ("evidence_nodes", "evidence_edges"),
        ),
        "dialog_reminders": _dialog_reminders(database_dir / "dialog_reminder.db"),
        "recap_tasks": _status_table(
            database_dir / "recap_tasks.db",
            table="recap_tasks",
            status_column="status",
        ),
        "search_sessions": _search_sessions(database_dir / "mnemos.db"),
        "delivery_events": delivery_lineage["delivery_table"],
        "cognitive_outcomes": delivery_lineage["outcome_table"],
        "delivery_outcome_lineage": delivery_lineage,
        "raw_vault": _vault_count(raw_vault_dir),
    }
    metrics["learning_signal"] = build_learning_signal_metrics(
        database_dir,
        metrics,
        raw_events_db=raw_events_db,
        observations_db=observations_db,
        policy_db=policy_db,
        consolidation_db=consolidation_db,
        freshness_window_seconds=freshness_window_seconds,
        now=now,
    )

    findings = _findings(metrics)
    state_machine = _state_machine()
    budget = _budget(metrics, findings)
    readiness = _readiness_sections(metrics, findings, budget)
    scorecard = _scorecard(readiness, budget, metrics)
    blocking_failures = [
        item
        for item in budget["failures"]
        if item.get("lifecycle_status") == "blocked"
    ]
    ok = not budget["failures"] if strict or enforce_budget else not blocking_failures
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": ok,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": {
            "strict": bool(strict),
            "budget": bool(enforce_budget),
            "side_effects": "none",
            "freshness_window_seconds": freshness_window_seconds,
        },
        "paths": {
            "database_dir": str(database_dir),
            "wiki_dir": str(wiki_dir),
            "raw_vault_dir": str(raw_vault_dir),
        },
        "metrics": metrics,
        "findings": findings,
        "state_machine": state_machine,
        "budget": budget,
        "readiness": readiness,
        "scorecard": scorecard,
    }


def format_cognitive_readiness_text(report: dict[str, Any]) -> str:
    """Format a concise human-readable report."""
    metrics = report["metrics"]
    lines = [
        "Mnemos cognitive readiness",
        f"schema: {report['schema_version']}",
        f"database_dir: {report['paths']['database_dir']}",
        f"wiki_dir: {report['paths']['wiki_dir']}",
        "",
        "Key metrics:",
        _metric_line(
            "page_metrics source_count=0",
            metrics["page_metrics"].get("source_count_zero"),
            metrics["page_metrics"].get("total"),
        ),
        _metric_line(
            "wiki pages missing source refs",
            metrics["wiki_pages"].get("missing_source_refs"),
            metrics["wiki_pages"].get("total"),
        ),
        _metric_line(
            "search sessions clicked",
            metrics["search_sessions"].get("clicked"),
            metrics["search_sessions"].get("total"),
        ),
        _metric_line(
            "dialog reminders resolved",
            metrics["dialog_reminders"].get("resolved"),
            metrics["dialog_reminders"].get("total"),
        ),
        _metric_line(
            "search sessions closed",
            metrics["search_sessions"].get("behavior_outcomes"),
            metrics["search_sessions"].get("total"),
        ),
        _metric_line(
            "learning observations",
            metrics["learning_signal"].get("observation_count"),
            metrics["learning_signal"].get("raw_signal_count"),
        ),
        _metric_line(
            "learning policy patches",
            metrics["learning_signal"].get("policy_patch_count"),
            metrics["learning_signal"].get("policy_driver_count"),
        ),
        _metric_line(
            "cognitive consolidation runs",
            metrics["learning_signal"].get("consolidation_run_count"),
            metrics["learning_signal"].get("method_candidate_count"),
        ),
        "",
        "Readiness:",
        *(
            f"- {name}: {section['status']} "
            f"({len(section['gaps'])} gaps)"
            for name, section in report.get("readiness", {}).items()
        ),
        "",
        f"Budget: {'ok' if report.get('budget', {}).get('ok') else 'fail'}",
        f"Score: {report.get('scorecard', {}).get('score', 0)}/100",
        "",
        "Findings:",
    ]
    findings = report.get("findings", [])
    if not findings:
        lines.append("- none")
    else:
        lines.extend(f"- {item['severity']}: {item['code']} - {item['message']}" for item in findings)
    return "\n".join(lines)


def _metric_line(label: str, value: Any, total: Any) -> str:
    if value is None or total is None:
        return f"- {label}: unavailable"
    return f"- {label}: {value}/{total}"


def _findings(metrics: dict[str, Any]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    if not metrics["raw_turns"].get("exists"):
        findings.append(_finding("warning", "raw_events_missing", "raw_events.db/raw_turns unavailable"))
    if not metrics["page_metrics"].get("exists"):
        findings.append(
            _finding("warning", "page_metrics_missing", "wiki_metrics.db/page_metrics unavailable")
        )
    if metrics["page_metrics"].get("source_count_zero", 0) > 0:
        findings.append(
            _finding("warning", "page_source_gap", "some page_metrics rows have source_count=0")
        )
    if metrics["page_metrics"].get("source_count_positive_empty_refs", 0) > 0:
        findings.append(
            _finding(
                "warning",
                "page_source_refs_gap",
                "some pages have source_count>0 but empty source_refs",
            )
        )
    if metrics["wiki_pages"].get("missing_source_refs", 0) > 0:
        findings.append(
            _finding("warning", "wiki_source_ref_gap", "some wiki pages lack source refs")
        )
    if not metrics["delivery_events"].get("exists"):
        findings.append(
            _finding("warning", "delivery_events_missing", "delivery_events table unavailable")
        )
    if not metrics["cognitive_outcomes"].get("exists"):
        findings.append(
            _finding("warning", "cognitive_outcomes_missing", "cognitive_outcomes table unavailable")
        )
    learning_signal = metrics.get("learning_signal", {})
    if learning_signal.get("required_tables_missing_count", 0) > 0:
        findings.append(
            _finding(
                "error",
                "required_evidence_unavailable",
                "one or more required learning lineage tables are unavailable or invalid",
            )
        )
    if learning_signal.get("required_evidence_empty_count", 0) > 0:
        findings.append(
            _finding(
                "warning",
                "required_evidence_empty",
                "required readiness schemas are initialized but contain no real evidence",
            )
        )
    if metrics["search_sessions"].get("exists"):
        if not metrics["search_sessions"].get("supports_outcomes", False):
            findings.append(
                _finding(
                    "warning",
                    "search_outcome_schema_gap",
                    "search_sessions table lacks open/ignore/outcome fields",
                )
            )
        if metrics["search_sessions"].get("unclosed", 0) > 0:
            findings.append(
                _finding(
                    "warning",
                    "search_behavior_gap",
                    "some search sessions lack click/open/ignore/no_result outcomes",
                )
            )
        if metrics["search_sessions"].get("clicked_gap", 0) > 0:
            findings.append(
                _finding(
                    "info",
                    "search_click_gap",
                    "search sessions exist but no click/open feedback was recorded",
                )
            )
    if metrics["dialog_reminders"].get("pending", 0) > 0:
        findings.append(
            _finding("info", "dialog_reminder_backlog", "dialog reminders still have pending rows")
        )
    if metrics["dialog_reminders"].get("resolution_gap", 0) > 0:
        findings.append(
            _finding(
                "warning",
                "dialog_reminder_resolution_gap",
                "dialog reminders exist but no resolved/ignored/deferred outcome was recorded",
            )
        )
    if learning_signal.get("observation_output_gap", 0) > 0:
        findings.append(
            _finding(
                "warning",
                "learning_observation_output_gap",
                "raw signals exist but no observations were produced",
            )
        )
    if learning_signal.get("all_visible_raw_skipped", 0) > 0:
        findings.append(
            _finding(
                "warning",
                "learning_observation_all_skipped",
                "all visible eligible Raw revisions ended without an Observation effect",
            )
        )
    if learning_signal.get("policy_patch_gap", 0) > 0:
        findings.append(
            _finding(
                "warning",
                "learning_policy_patch_gap",
                "reflection/feedback drivers exist but no policy patch or no-patch evidence was recorded",
            )
        )
    if learning_signal.get("consolidation_run_gap", 0) > 0:
        findings.append(
            _finding(
                "warning",
                "learning_consolidation_run_gap",
                "raw signals exist but cognitive_consolidation has no recorded run",
            )
        )
    for code, key, message in (
        (
            "delivery_feedback_lineage_gap",
            "delivery_feedback_lineage_gap",
            "delivery decisions lack explicit feedback or exact reciprocal outcome links",
        ),
        (
            "observation_lineage_gap",
            "observation_lineage_gap",
            "raw turns lack exact observation lineage coverage",
        ),
        (
            "policy_driver_lineage_gap",
            "policy_driver_lineage_gap",
            "reflection or recap drivers lack patch/no_patch lineage coverage",
        ),
        (
            "consolidation_coverage_gap",
            "consolidation_coverage_gap",
            "consolidation candidates lack applied per-candidate coverage",
        ),
        (
            "learning_evidence_stale",
            "stale_lineage_count",
            "one or more cognitive effect lineages are stale",
        ),
        (
            "learning_lineage_unobserved",
            "unobserved_lineage_count",
            "one or more required cognitive lineages have a zero denominator",
        ),
    ):
        if learning_signal.get(key, 0) > 0:
            findings.append(_finding("warning", code, message))
    return findings


def _finding(severity: str, code: str, message: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message}


def record_cognitive_readiness_gaps(report: dict[str, Any], config: Any) -> str | None:
    """Record current readiness gaps in the ActionLedger when explicitly requested."""
    findings = list(report.get("findings") or [])
    failures = list(report.get("budget", {}).get("failures") or [])
    if not findings and not failures:
        return None
    from core.system_contracts import (
        ActionLedger,
        make_cognitive_readiness_observation,
    )

    evidence_refs = ["core/ops/cognitive_readiness.py"]
    evidence_refs.extend(f"finding:{item.get('code')}" for item in findings if item.get("code"))
    ledger = ActionLedger.from_config(config, initialize=True)
    action_id = ledger.record_observation(
        make_cognitive_readiness_observation(
            actor="cognitive_readiness_audit",
            target=f"{SCHEMA_VERSION}:{report.get('generated_at', '')}",
            evidence_refs=evidence_refs,
            result_status="needs_user" if failures else "degraded",
            details={
                "schema_version": SCHEMA_VERSION,
                "budget_ok": bool(report.get("budget", {}).get("ok")),
                "failure_codes": [item.get("code") for item in failures],
                "finding_codes": [item.get("code") for item in findings],
                "score": report.get("scorecard", {}).get("score"),
            },
        )
    )
    report["action_ledger"] = {
        "recorded": True,
        "action_id": action_id,
        "action_type": "cognitive_readiness_gap",
    }
    return action_id


def _state_machine() -> dict[str, dict[str, Any]]:
    return {
        code: {
            "section": policy["section"],
            "owner": policy["owner"],
            "failure_class": policy["failure_class"],
            "lifecycle_status": policy["lifecycle_status"],
            "repair_action": policy["repair_action"],
            "budget": policy["budget"],
        }
        for code, policy in READINESS_POLICIES.items()
    }


def _budget(metrics: dict[str, Any], findings: list[dict[str, str]]) -> dict[str, Any]:
    finding_codes = {item["code"] for item in findings}
    items: list[dict[str, Any]] = []
    for code, policy in READINESS_POLICIES.items():
        if "metric_path" in policy:
            observed = _metric_at(metrics, tuple(policy["metric_path"]))
        else:
            observed = 1 if code in finding_codes else 0
        limit = int(policy["budget"])
        ok = int(observed or 0) <= limit
        items.append(
            {
                "code": code,
                "section": policy["section"],
                "owner": policy["owner"],
                "observed": int(observed or 0),
                "budget": limit,
                "ok": ok,
                "failure_class": policy["failure_class"],
                "lifecycle_status": policy["lifecycle_status"],
                "repair_action": policy["repair_action"],
            }
        )
    failures = [item for item in items if not item["ok"]]
    return {
        "ok": not failures,
        "items": items,
        "failures": failures,
        "failure_count": len(failures),
    }


def _metric_at(metrics: dict[str, Any], path: tuple[str, str]) -> int:
    section, key = path
    value = metrics.get(section, {}).get(key, 0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _readiness_sections(
    metrics: dict[str, Any],
    findings: list[dict[str, str]],
    budget: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    findings_by_section: dict[str, list[dict[str, str]]] = {
        name: [] for name in READINESS_SECTIONS
    }
    for finding in findings:
        policy = READINESS_POLICIES.get(finding["code"], {})
        section = policy.get("section", "evidence")
        findings_by_section.setdefault(section, []).append(finding)
    failures_by_section: dict[str, list[dict[str, Any]]] = {
        name: [] for name in READINESS_SECTIONS
    }
    for item in budget.get("failures", []):
        failures_by_section.setdefault(item["section"], []).append(item)

    result: dict[str, dict[str, Any]] = {}
    for section in READINESS_SECTIONS:
        failures = failures_by_section.get(section, [])
        if any(item["lifecycle_status"] == "blocked" for item in failures):
            status = "blocked"
        elif failures or findings_by_section.get(section):
            status = "degraded"
        else:
            status = "ok"
        result[section] = {
            "status": status,
            "gaps": findings_by_section.get(section, []),
            "budget_failures": failures,
            "metrics": _section_metrics(section, metrics),
        }
    return result


def _section_metrics(section: str, metrics: dict[str, Any]) -> dict[str, Any]:
    if section == "source":
        return {
            "raw_turns": metrics["raw_turns"],
            "page_metrics": metrics["page_metrics"],
            "raw_retention": metrics["raw_retention"],
        }
    if section == "evidence":
        return {
            "wiki_pages": metrics["wiki_pages"],
            "knowledge_graph": metrics["knowledge_graph"],
            "evidence_graph": metrics["evidence_graph"],
        }
    if section == "consumer":
        return {
            "delivery_events": metrics["delivery_events"],
            "cognitive_outcomes": metrics["cognitive_outcomes"],
            "recap_tasks": metrics["recap_tasks"],
            "learning_signal": metrics["learning_signal"],
        }
    return {
        "search_sessions": metrics["search_sessions"],
        "dialog_reminders": metrics["dialog_reminders"],
    }


def _scorecard(
    readiness: dict[str, Any],
    budget: dict[str, Any],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    failure_count = int(budget.get("failure_count", 0))
    blocked_sections = [
        name for name, section in readiness.items() if section.get("status") == "blocked"
    ]
    degraded_sections = [
        name for name, section in readiness.items() if section.get("status") == "degraded"
    ]
    deduction = min(100, failure_count * 8 + len(blocked_sections) * 10)
    return {
        "dimension": "cognitive_assets",
        "score_name": "cognitive_maturity_readiness",
        "score": max(0, 100 - deduction),
        "max_score": 100,
        "budget_ok": bool(budget.get("ok")),
        "blocked_sections": blocked_sections,
        "degraded_sections": degraded_sections,
        "blocking_findings": [
            item["code"]
            for item in budget.get("failures", [])
            if item.get("lifecycle_status") == "blocked"
        ],
        "runtime_metrics": {
            "cognitive_readiness.budget_ok": bool(budget.get("ok")),
            "cognitive_readiness.failure_count": failure_count,
            "cognitive_readiness.sections.blocked": len(blocked_sections),
            "cognitive_readiness.sections.degraded": len(degraded_sections),
            "learning_signal.observation_output_gap": int(
                metrics.get("learning_signal", {}).get("observation_output_gap", 0) or 0
            ),
            "learning_signal.all_visible_raw_skipped": int(
                metrics.get("learning_signal", {}).get("all_visible_raw_skipped", 0) or 0
            ),
            "learning_signal.policy_patch_gap": int(
                metrics.get("learning_signal", {}).get("policy_patch_gap", 0) or 0
            ),
            "learning_signal.consolidation_run_gap": int(
                metrics.get("learning_signal", {}).get("consolidation_run_gap", 0) or 0
            ),
            "learning_signal.delivery_feedback_lineage_gap": int(
                metrics.get("learning_signal", {}).get("delivery_feedback_lineage_gap", 0) or 0
            ),
            "learning_signal.observation_lineage_gap": int(
                metrics.get("learning_signal", {}).get("observation_lineage_gap", 0) or 0
            ),
            "learning_signal.policy_driver_lineage_gap": int(
                metrics.get("learning_signal", {}).get("policy_driver_lineage_gap", 0) or 0
            ),
            "learning_signal.consolidation_coverage_gap": int(
                metrics.get("learning_signal", {}).get("consolidation_coverage_gap", 0) or 0
            ),
            "learning_signal.stale_lineage_count": int(
                metrics.get("learning_signal", {}).get("stale_lineage_count", 0) or 0
            ),
            "learning_signal.unobserved_lineage_count": int(
                metrics.get("learning_signal", {}).get("unobserved_lineage_count", 0) or 0
            ),
        },
    }


def build_learning_signal_report(config: Any) -> dict[str, Any]:
    """Return the learning-signal slice used by health and audits."""
    return cast(
        dict[str, Any],
        build_cognitive_readiness_report(config)["metrics"]["learning_signal"],
    )


def _page_metrics(db_path: Path, wiki_dir: Path) -> dict[str, Any]:
    base = _base_metric(db_path, "page_metrics")
    if not base["exists"]:
        return base
    wiki_paths = _wiki_markdown_paths(wiki_dir)
    metric_rows: dict[str, dict[str, Any]] = {}
    stale_metric_rows = 0
    with _connect_ro(db_path) as conn:
        columns = _columns(conn, "page_metrics")
        metric_rows_total = _count(conn, "page_metrics")
        if "wiki_path" in columns:
            selected = [
                "wiki_path",
                "source_count" if "source_count" in columns else "0 AS source_count",
                "source_refs" if "source_refs" in columns else "'' AS source_refs",
                "page_role" if "page_role" in columns else "'knowledge' AS page_role",
            ]
            rows = conn.execute(
                f"SELECT {', '.join(selected)} FROM page_metrics"  # nosec B608
            ).fetchall()
            for row in rows:
                normalized = _normalize_wiki_path(str(row["wiki_path"] or ""), wiki_dir)
                if not normalized or normalized not in wiki_paths:
                    stale_metric_rows += 1
                    continue
                metric_rows[normalized] = {
                    "source_count": int(row["source_count"] or 0),
                    "source_refs": str(row["source_refs"] or ""),
                    "page_role": str(row["page_role"] or "knowledge"),
                }
    source_required_total = 0
    source_exempt_total = 0
    source_exempt_reasons: Counter[str] = Counter()
    source_exempt_samples: list[dict[str, str]] = []
    source_zero = 0
    source_refs_nonempty = 0
    source_count_positive_empty_refs = 0
    for rel_path in sorted(wiki_paths):
        row = metric_rows.get(rel_path, {})
        exempt_reason = source_exempt_reason(rel_path, str(row.get("page_role") or ""))
        if exempt_reason:
            source_exempt_total += 1
            source_exempt_reasons[exempt_reason] += 1
            if len(source_exempt_samples) < SOURCE_EXEMPT_SAMPLE_LIMIT:
                source_exempt_samples.append({"wiki_path": rel_path, "reason": exempt_reason})
            continue
        source_required_total += 1
        source_count = int(row.get("source_count") or 0)
        source_refs = str(row.get("source_refs") or "")
        if source_count <= 0:
            source_zero += 1
        elif _source_refs_nonempty_value(source_refs):
            source_refs_nonempty += 1
        else:
            source_count_positive_empty_refs += 1
    with_source = max(0, source_required_total - source_zero)
    base.update(
        {
            "total": source_required_total,
            "wiki_page_total": len(wiki_paths),
            "metric_rows_total": metric_rows_total,
            "stale_metric_rows": stale_metric_rows,
            "source_required_total": source_required_total,
            "source_exempt_total": source_exempt_total,
            "source_exempt_reasons": dict(source_exempt_reasons),
            "source_exempt_samples": source_exempt_samples,
            "source_count_zero": source_zero,
            "with_source_count": with_source,
            "source_refs_nonempty": source_refs_nonempty,
            "source_count_positive_empty_refs": source_count_positive_empty_refs,
            "source_count_zero_ratio": _ratio(source_zero, source_required_total),
        }
    )
    return base


def _wiki_pages(
    wiki_dir: Path,
    source_paths: set[str],
    page_roles: dict[str, str],
) -> dict[str, Any]:
    if not wiki_dir.exists():
        return {
            "exists": False,
            "path": str(wiki_dir),
            "total": 0,
            "with_source_refs": 0,
            "missing_source_refs": 0,
            "sample_missing_source_refs": [],
        }
    total = 0
    source_required_total = 0
    source_exempt_total = 0
    source_exempt_reasons: Counter[str] = Counter()
    source_exempt_samples: list[dict[str, str]] = []
    with_source_refs = 0
    samples: list[str] = []
    for md_file in sorted(wiki_dir.rglob("*.md")):
        rel = md_file.relative_to(wiki_dir)
        if any(part.startswith(".") or part == "99-Reports" for part in rel.parts):
            continue
        rel_path = str(rel)
        total += 1
        exempt_reason = source_exempt_reason(rel_path, page_roles.get(rel_path, ""))
        if exempt_reason:
            source_exempt_total += 1
            source_exempt_reasons[exempt_reason] += 1
            if len(source_exempt_samples) < SOURCE_EXEMPT_SAMPLE_LIMIT:
                source_exempt_samples.append({"wiki_path": rel_path, "reason": exempt_reason})
            continue
        source_required_total += 1
        if rel_path in source_paths:
            with_source_refs += 1
        elif len(samples) < 10:
            samples.append(rel_path)
    missing = source_required_total - with_source_refs
    return {
        "exists": True,
        "path": str(wiki_dir),
        "total": total,
        "source_required_total": source_required_total,
        "source_exempt_total": source_exempt_total,
        "source_exempt_reasons": dict(source_exempt_reasons),
        "source_exempt_samples": source_exempt_samples,
        "with_source_refs": with_source_refs,
        "missing_source_refs": missing,
        "missing_source_refs_ratio": _ratio(missing, source_required_total),
        "sample_missing_source_refs": samples,
    }


def _wiki_markdown_paths(wiki_dir: Path) -> set[str]:
    if not wiki_dir.exists():
        return set()
    paths: set[str] = set()
    for md_file in wiki_dir.rglob("*.md"):
        try:
            rel = md_file.relative_to(wiki_dir)
        except ValueError:
            continue
        if any(part.startswith(".") or part == "99-Reports" for part in rel.parts):
            continue
        paths.add(str(rel))
    return paths


def _page_metric_roles(db_path: Path, wiki_dir: Path) -> dict[str, str]:
    base = _base_metric(db_path, "page_metrics")
    if not base["exists"]:
        return {}
    with _connect_ro(db_path) as conn:
        columns = _columns(conn, "page_metrics")
        if "wiki_path" not in columns or "page_role" not in columns:
            return {}
        rows = conn.execute("SELECT wiki_path, page_role FROM page_metrics").fetchall()
    roles: dict[str, str] = {}
    for row in rows:
        normalized = _normalize_wiki_path(str(row["wiki_path"] or ""), wiki_dir)
        role = str(row["page_role"] or "")
        if normalized and role:
            roles[normalized] = role
    return roles


def _source_refs_nonempty_value(value: Any) -> bool:
    if value is None:
        return False
    text = str(value).strip()
    return bool(text and text not in EMPTY_JSON_VALUES)


def _raw_turns(db_path: Path) -> dict[str, Any]:
    base = _base_metric(db_path, "raw_turns")
    if not base["exists"]:
        return base
    with _connect_ro(db_path) as conn:
        columns = _columns(conn, "raw_turns")
        total = _count(conn, "raw_turns")
        base.update(
            {
                "total": total,
                "status_counts": _group_counts(conn, "raw_turns", "completeness_status"),
                "with_tool_calls": _json_nonempty_count(conn, "raw_turns", "tool_calls_json", columns),
                "with_tool_results": _json_nonempty_count(
                    conn, "raw_turns", "tool_results_json", columns
                ),
                "with_attachments": _json_nonempty_count(
                    conn, "raw_turns", "attachments_json", columns
                ),
                "with_source_files": _json_nonempty_count(
                    conn, "raw_turns", "source_files_json", columns
                ),
            }
        )
    return base


def _raw_retention(db_path: Path) -> dict[str, Any]:
    base = _base_metric(db_path, "raw_metrics")
    if not base["exists"]:
        return base
    with _connect_ro(db_path) as conn:
        total = _count(conn, "raw_metrics")
        counts = _group_counts(conn, "raw_metrics", "retention_state")
        base.update(
            {
                "total": total,
                "status_counts": counts,
                "active": counts.get("active", 0),
                "eligible_delete": counts.get("eligible_delete", 0),
            }
        )
    return base


def _dialog_reminders(db_path: Path) -> dict[str, Any]:
    base = _base_metric(db_path, "dialog_reminders")
    if not base["exists"]:
        return base
    with _connect_ro(db_path) as conn:
        total = _count(conn, "dialog_reminders")
        counts = _group_counts(conn, "dialog_reminders", "status")
        resolved_with_choice = 0
        if "resolved_choice" in _columns(conn, "dialog_reminders"):
            resolved_with_choice = _count_where(
                conn,
                "dialog_reminders",
                "resolved_choice IS NOT NULL AND TRIM(resolved_choice) != ''",
            )
        base.update(
            {
                "total": total,
                "status_counts": counts,
                "pending": counts.get("pending", 0),
                "resolved": counts.get("resolved", 0),
                "ignored": counts.get("ignored", 0),
                "deferred": counts.get("deferred", 0),
                "expired": counts.get("expired", 0),
                "resolved_with_choice": resolved_with_choice,
                "closed": (
                    counts.get("resolved", 0)
                    + counts.get("ignored", 0)
                    + counts.get("deferred", 0)
                    + counts.get("expired", 0)
                ),
            }
        )
        base["resolution_gap"] = 1 if total > 0 and base["closed"] == 0 else 0
        base["closed_ratio"] = _ratio(base["closed"], total)
    return base


def _search_sessions(db_path: Path) -> dict[str, Any]:
    base = _base_metric(db_path, "search_sessions")
    if not base["exists"]:
        return base
    with _connect_ro(db_path) as conn:
        columns = _columns(conn, "search_sessions")
        total = _count(conn, "search_sessions")
        clicked = 0
        if "clicked_path" in columns:
            clicked = _count_where(
                conn,
                "search_sessions",
                "clicked_path IS NOT NULL AND TRIM(clicked_path) != ''",
            )
        opened = (
            _count_where(
                conn,
                "search_sessions",
                "opened_path IS NOT NULL AND TRIM(opened_path) != ''",
            )
            if "opened_path" in columns
            else 0
        )
        ignored = (
            _count_where(
                conn,
                "search_sessions",
                "ignored_at IS NOT NULL AND TRIM(ignored_at) != ''",
            )
            if "ignored_at" in columns
            else 0
        )
        no_result = (
            _count_where(conn, "search_sessions", "outcome_status = 'no_result'")
            if "outcome_status" in columns
            else 0
        )
        behavior_outcomes = _search_behavior_outcome_count(conn, columns)
        supports_outcomes = {"opened_path", "ignored_at", "outcome_status", "outcome_at"} <= columns
        clicked_or_opened = max(clicked, opened)
        base.update(
            {
                "total": total,
                "clicked": clicked,
                "opened": opened,
                "ignored": ignored,
                "no_result": no_result,
                "behavior_outcomes": behavior_outcomes,
                "unclosed": max(0, total - behavior_outcomes),
                "clicked_gap": 1 if total > 0 and clicked_or_opened == 0 else 0,
                "clicked_ratio": _ratio(clicked_or_opened, total),
                "behavior_outcome_ratio": _ratio(behavior_outcomes, total),
                "supports_outcomes": supports_outcomes,
                "outcome_status_counts": (
                    _group_counts(conn, "search_sessions", "outcome_status")
                    if "outcome_status" in columns
                    else {}
                ),
            }
        )
    return base


def _search_behavior_outcome_count(conn: sqlite3.Connection, columns: set[str]) -> int:
    if {"clicked_path", "opened_path", "ignored_at", "outcome_status"} <= columns:
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM search_sessions
            WHERE (clicked_path IS NOT NULL AND TRIM(clicked_path) != '')
               OR (opened_path IS NOT NULL AND TRIM(opened_path) != '')
               OR (ignored_at IS NOT NULL AND TRIM(ignored_at) != '')
               OR outcome_status IN ('click', 'ignore', 'no_result')
            """
        ).fetchone()
        return int(row[0]) if row else 0
    if "clicked_path" not in columns:
        return 0
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM search_sessions
        WHERE clicked_path IS NOT NULL AND TRIM(clicked_path) != ''
        """
    ).fetchone()
    return int(row[0]) if row else 0


def _status_table(db_path: Path, *, table: str, status_column: str) -> dict[str, Any]:
    base = _base_metric(db_path, table)
    if not base["exists"]:
        return base
    with _connect_ro(db_path) as conn:
        total = _count(conn, table)
        counts = _group_counts(conn, table, status_column)
        base.update({"total": total, "status_counts": counts})
    return base


def _table_counts(db_path: Path, tables: Iterable[str]) -> dict[str, Any]:
    if not sqlite_artifact_exists(db_path):
        return {"exists": False, "path": str(db_path), "tables": {}, "schema_valid": False}
    counts: dict[str, Any] = {}
    try:
        with _connect_ro(db_path) as conn:
            for table in tables:
                counts[table] = _count(conn, table) if _table_exists(conn, table) else None
    except (OSError, sqlite3.Error) as exc:
        return {
            "exists": False,
            "path": str(db_path),
            "tables": {},
            "schema_valid": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "exists": True,
        "path": str(db_path),
        "tables": counts,
        "schema_valid": all(value is not None for value in counts.values()),
    }


def _vault_count(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "path": str(path), "markdown_files": 0}
    count = sum(1 for item in path.rglob("*.md") if item.is_file())
    return {"exists": True, "path": str(path), "markdown_files": count}


def _base_metric(db_path: Path, table: str) -> dict[str, Any]:
    if not sqlite_artifact_exists(db_path):
        return {"exists": False, "path": str(db_path), "table": table}
    try:
        with _connect_ro(db_path) as conn:
            exists = _table_exists(conn, table)
    except (OSError, sqlite3.Error) as exc:
        return {
            "exists": False,
            "path": str(db_path),
            "table": table,
            "error": str(exc),
        }
    return {"exists": exists, "path": str(db_path), "table": table}


def _page_metric_source_paths(db_path: Path, wiki_dir: Path) -> set[str]:
    base = _base_metric(db_path, "page_metrics")
    if not base["exists"]:
        return set()
    with _connect_ro(db_path) as conn:
        columns = _columns(conn, "page_metrics")
        if "source_count" not in columns:
            return set()
        if "source_refs" in columns:
            placeholders = ", ".join("?" for _ in EMPTY_JSON_VALUES)
            rows = conn.execute(
                f"""
                SELECT wiki_path
                FROM page_metrics
                WHERE COALESCE(source_count, 0) > 0
                  AND source_refs IS NOT NULL
                  AND TRIM(source_refs) NOT IN ({placeholders})
                """,  # nosec B608
                EMPTY_JSON_VALUES,
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT wiki_path
                FROM page_metrics
                WHERE COALESCE(source_count, 0) > 0
                """
            ).fetchall()
    return {
        normalized
        for row in rows
        if row[0] and (normalized := _normalize_wiki_path(str(row[0]), wiki_dir))
    }


def _normalize_wiki_path(raw_path: str, wiki_dir: Path) -> str:
    cleaned = raw_path.strip()
    if not cleaned:
        return ""
    candidate = Path(cleaned)
    if candidate.is_absolute():
        try:
            candidate = candidate.resolve().relative_to(wiki_dir.resolve())
        except ValueError:
            return cleaned.replace("\\", "/")
    return str(candidate).replace("\\", "/")


def _configured_vault_path(config: Any, *, attr: str, vault_name: str) -> Path:
    direct = getattr(config, attr, None)
    if direct:
        return Path(direct).expanduser()
    try:
        return Path(config.vault_dir(vault_name)).expanduser()
    except (AttributeError, KeyError, TypeError):
        return Path(".")


def _configured_db_path(config: Any, key: str, default: Path) -> Path:
    try:
        value = config.get(key, None)
    except (AttributeError, TypeError):
        value = None
    return Path(value).expanduser() if value else default


def _config_int(config: Any, key: str, default: int) -> int:
    try:
        return int(config.get(key, default) or default)
    except (AttributeError, TypeError, ValueError):
        return default


def _delivery_outcome_metric(
    db_path: Path,
    *,
    freshness_window_seconds: int = DEFAULT_FRESHNESS_WINDOW_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compatibility test seam for the versioned delivery lineage metric."""
    return delivery_outcome_metric(
        db_path,
        freshness_window_seconds=freshness_window_seconds,
        now=now or datetime.now(timezone.utc),
    )


def _observation_lineage_metric(
    raw_db_path: Path,
    observation_db_path: Path,
    *,
    freshness_window_seconds: int = DEFAULT_FRESHNESS_WINDOW_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compatibility test seam for the versioned observation lineage metric."""
    return observation_lineage_metric(
        raw_db_path,
        observation_db_path,
        freshness_window_seconds=freshness_window_seconds,
        now=now or datetime.now(timezone.utc),
    )


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = connect_readonly_sqlite(db_path, timeout_seconds=5)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    table = validate_sql_identifier(table)
    query = f"PRAGMA table_info({table})"  # nosec B608
    return {str(row[1]) for row in conn.execute(query).fetchall()}


def _count(conn: sqlite3.Connection, table: str) -> int:
    table = validate_sql_identifier(table)
    query = f"SELECT COUNT(*) FROM {table}"  # nosec B608
    row = conn.execute(query).fetchone()
    return int(row[0]) if row else 0


def _count_where(conn: sqlite3.Connection, table: str, where: str) -> int:
    table = validate_sql_identifier(table)
    if where not in READINESS_WHERE_CLAUSES:
        raise ValueError(f"Unsupported cognitive readiness WHERE clause: {where!r}")
    query = f"SELECT COUNT(*) FROM {table} WHERE {where}"  # nosec B608
    row = conn.execute(query).fetchone()
    return int(row[0]) if row else 0


def _group_counts(conn: sqlite3.Connection, table: str, column: str) -> dict[str, int]:
    table = validate_sql_identifier(table)
    column = validate_sql_identifier(column)
    if column not in _columns(conn, table):
        return {}
    query = f"SELECT {column}, COUNT(*) FROM {table} GROUP BY {column}"  # nosec B608
    rows = conn.execute(query).fetchall()
    return {str(row[0] or ""): int(row[1]) for row in rows}


def _json_nonempty_count(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    columns: set[str],
) -> int:
    table = validate_sql_identifier(table)
    column = validate_sql_identifier(column)
    if column not in columns:
        return 0
    placeholders = ", ".join("?" for _ in EMPTY_JSON_VALUES)
    query = f"""
        SELECT COUNT(*)
        FROM {table}
        WHERE {column} IS NOT NULL
          AND TRIM({column}) NOT IN ({placeholders})
        """  # nosec B608
    row = conn.execute(
        query,
        EMPTY_JSON_VALUES,
    ).fetchone()
    return int(row[0]) if row else 0


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


def dumps_report(report: dict[str, Any]) -> str:
    """Serialize report using the repository's JSON style."""
    return json.dumps(report, ensure_ascii=False, indent=2)
