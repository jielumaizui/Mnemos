"""Backfill page-level evidence refs for distilled Wiki pages."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from core.config import get_config
from core.cognitive.state_contract import sha256_json
from core.db_utils import validate_sql_identifier
from core.frontmatter import fm_get, parse_frontmatter, write_frontmatter
from core.trust.formal_markdown import (
    TrustedMarkdownDecisionPolicy,
    submit_or_write_markdown_with_decision,
)
from core.trust.models import sha256_text
from core.wiki_metrics import WikiMetrics, compute_evidence_level

SCHEMA_VERSION = "mnemos.evidence_backfill.v1"
EMPTY_REF_VALUES = ("", "[]", "{}", "null", "None")
EVIDENCE_BACKFILL_MARKDOWN_POLICY = TrustedMarkdownDecisionPolicy(
    contract_id="project-contract:evidence-backfill-frontmatter",
    contract_revision_id="mnemos.evidence_backfill_frontmatter.v1",
    contract_text=(
        "EvidenceBackfill may update only the exact page whose reviewed source "
        "references and evidence counts match the current backfill plan."
    ),
    source_namespace="evidence-backfill-frontmatter",
    producer="evidence-backfill",
    producer_code_hash=sha256_json(
        {
            "module": "core.ops.evidence_backfill",
            "producer": "_apply_frontmatter_change",
            "version": "mnemos.evidence_backfill_frontmatter.v1",
        }
    ),
    evaluator_id="evidence-backfill-frontmatter-evaluator",
    constraints=(
        "Source refs, counts, evidence level, page preimage, and target must remain exact.",
        "No unresolved source may be fabricated or promoted by the backfill.",
    ),
    approved_candidate_key="apply_exact_evidence_backfill",
    approved_candidate_summary="Apply the exact reviewed evidence frontmatter delta.",
    rejected_candidate_key="retain_existing_evidence_frontmatter",
    rejected_candidate_summary="Retain the page if source evidence or bytes drift.",
    approved_reason_code="evidence_backfill_binding_verified",
    rejected_reason_code="evidence_backfill_binding_rejected",
    committed_metric="evidence_backfill_markdown_committed",
    rejected_metric="unbound_evidence_backfill_count",
)

EVIDENCE_BACKFILL_REPORT_MARKDOWN_POLICY = TrustedMarkdownDecisionPolicy(
    contract_id="project-contract:evidence-backfill-report",
    contract_revision_id="mnemos.evidence_backfill_report.v1",
    contract_text=(
        "EvidenceBackfill may publish only the exact readiness report rendered from "
        "the completed backfill denominator, changes, errors, and unresolved samples."
    ),
    source_namespace="evidence-backfill-report",
    producer="evidence-backfill",
    producer_code_hash=sha256_json(
        {
            "module": "core.ops.evidence_backfill",
            "producer": "_write_obsidian_report",
            "version": "mnemos.evidence_backfill_report.v1",
        }
    ),
    evaluator_id="evidence-backfill-report-evaluator",
    constraints=(
        "Report mode, denominator, changes, unresolved samples, target, and bytes remain exact.",
        "The report may not claim an applied change absent from the backfill result.",
    ),
    approved_candidate_key="publish_exact_evidence_backfill_report",
    approved_candidate_summary="Publish the exact evidence-backfill readiness report.",
    rejected_candidate_key="retain_evidence_report_state",
    rejected_candidate_summary="Retain report state when backfill facts drift.",
    approved_reason_code="evidence_backfill_report_binding_verified",
    rejected_reason_code="evidence_backfill_report_binding_rejected",
    committed_metric="evidence_backfill_report_committed",
    rejected_metric="unbound_evidence_backfill_report_count",
)


@dataclass(frozen=True)
class EvidenceBackfillOptions:
    apply: bool = False
    limit: int | None = None
    max_refs_per_page: int = 20
    frontmatter_ref_limit: int = 10
    unresolved_sample_limit: int = 50
    change_sample_limit: int = 100
    include_relation_evidence: bool = True
    relation_evidence_types: tuple[str, ...] = ("anti_pattern_quote", "distill_extraction")
    write_frontmatter: bool = True
    write_report: bool = True
    report_dir: str = "99-Reports/认知数据就绪度"


@dataclass
class PageState:
    wiki_path: str
    source_count: int = 0
    source_refs: list[str] | None = None
    evidence_level: int = 1

    @property
    def refs(self) -> list[str]:
        return list(self.source_refs or [])


def run_evidence_backfill(
    config: Any | None = None,
    *,
    apply: bool = False,
    limit: int | None = None,
    max_refs_per_page: int | None = None,
    frontmatter_ref_limit: int | None = None,
    unresolved_sample_limit: int | None = None,
    change_sample_limit: int | None = None,
    include_relation_evidence: bool | None = None,
    relation_evidence_types: list[str] | tuple[str, ...] | None = None,
    write_frontmatter: bool | None = None,
    write_report: bool | None = None,
    report_dir: str | None = None,
) -> dict[str, Any]:
    """Build and optionally apply a page-level evidence backfill plan."""
    runtime = config or get_config()
    options = _resolve_options(
        runtime,
        apply=apply,
        limit=limit,
        max_refs_per_page=max_refs_per_page,
        frontmatter_ref_limit=frontmatter_ref_limit,
        unresolved_sample_limit=unresolved_sample_limit,
        change_sample_limit=change_sample_limit,
        include_relation_evidence=include_relation_evidence,
        relation_evidence_types=relation_evidence_types,
        write_frontmatter=write_frontmatter,
        write_report=write_report,
        report_dir=report_dir,
    )
    database_dir = Path(getattr(runtime, "database_dir", Path.home() / ".mnemos"))
    wiki_dir = _configured_wiki_dir(runtime)
    wiki_paths = _wiki_markdown_paths(wiki_dir)
    page_states = _load_page_states(database_dir / "wiki_metrics.db", wiki_dir, wiki_paths)
    refs_by_page: dict[str, set[str]] = {}
    source_stats = {
        "document_wiki_link": _source_stat(),
        "distillation_tasks": _source_stat(),
        "relation_evidence": _source_stat(),
        "frontmatter": _source_stat(),
    }
    diagnostics = {
        "distill_missing_raw_event_refs": {
            "count": 0,
            "samples": [],
        }
    }

    _collect_document_links(
        database_dir / "knowledge_graph.db",
        wiki_dir,
        wiki_paths,
        refs_by_page,
        source_stats["document_wiki_link"],
    )
    _collect_distill_tasks(
        database_dir / "distill_queue.db",
        wiki_dir,
        wiki_paths,
        refs_by_page,
        source_stats["distillation_tasks"],
        diagnostics["distill_missing_raw_event_refs"],
        options.unresolved_sample_limit,
    )
    if options.include_relation_evidence:
        _collect_relation_evidence(
            database_dir / "knowledge_graph.db",
            wiki_dir,
            wiki_paths,
            refs_by_page,
            source_stats["relation_evidence"],
            set(options.relation_evidence_types),
        )
    _collect_frontmatter_provenance(
        wiki_dir,
        wiki_paths,
        refs_by_page,
        source_stats["frontmatter"],
    )

    changes = _build_changes(page_states, refs_by_page, options)
    unresolved = _unresolved_pages(page_states, refs_by_page, options.unresolved_sample_limit)
    errors: list[dict[str, str]] = []
    if options.apply:
        errors = _apply_changes(database_dir, wiki_dir, changes, options)
    reported_changes = _reported_changes(changes, options.change_sample_limit)
    report = {
        "schema_version": SCHEMA_VERSION,
        "ok": not errors,
        "applied": options.apply,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "paths": {
            "database_dir": str(database_dir),
            "wiki_dir": str(wiki_dir),
        },
        "config": {
            "limit": options.limit,
            "max_refs_per_page": options.max_refs_per_page,
            "frontmatter_ref_limit": options.frontmatter_ref_limit,
            "unresolved_sample_limit": options.unresolved_sample_limit,
            "change_sample_limit": options.change_sample_limit,
            "include_relation_evidence": options.include_relation_evidence,
            "relation_evidence_types": list(options.relation_evidence_types),
            "write_frontmatter": options.write_frontmatter,
            "write_report": options.write_report,
            "report_dir": options.report_dir,
        },
        "sources": source_stats,
        "diagnostics": diagnostics,
        "scanned_pages": len(page_states),
        "candidate_pages": len(refs_by_page),
        "changed_pages": len(changes),
        "reported_changes": len(reported_changes),
        "errors": errors,
        "unresolved": unresolved,
        "changes": reported_changes,
        "report_path": "",
    }
    if options.apply and options.write_report:
        report_path, proposal_id = _write_obsidian_report(wiki_dir, report, options)
        report["report_path"] = str(report_path) if report_path is not None else ""
        if proposal_id:
            report["report_proposal_id"] = proposal_id
    return report


def format_evidence_backfill_text(report: dict[str, Any]) -> str:
    action = "applied" if report["applied"] else "dry-run"
    lines = [
        "Mnemos evidence backfill",
        f"schema: {report['schema_version']}",
        f"mode: {action}",
        f"database_dir: {report['paths']['database_dir']}",
        f"wiki_dir: {report['paths']['wiki_dir']}",
        "",
        "Summary:",
        f"- scanned_pages: {report['scanned_pages']}",
        f"- candidate_pages: {report['candidate_pages']}",
        f"- changed_pages: {report['changed_pages']}",
        f"- unresolved_source_count_zero: {report['unresolved']['source_count_zero']}",
        "",
        "Sources:",
    ]
    for name, stats in report["sources"].items():
        lines.append(f"- {name}: rows={stats['rows']} refs={stats['refs']}")
    if report.get("errors"):
        lines.append("")
        lines.append("Errors:")
        for error in report["errors"]:
            lines.append(f"- {error['wiki_path']}: {error['error']}")
    return "\n".join(lines)


def dumps_report(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2)


def _resolve_options(
    config: Any,
    *,
    apply: bool,
    limit: int | None,
    max_refs_per_page: int | None,
    frontmatter_ref_limit: int | None,
    unresolved_sample_limit: int | None,
    change_sample_limit: int | None,
    include_relation_evidence: bool | None,
    relation_evidence_types: list[str] | tuple[str, ...] | None,
    write_frontmatter: bool | None,
    write_report: bool | None,
    report_dir: str | None,
) -> EvidenceBackfillOptions:
    resolved_limit = _int_option(config, "evidence_backfill.default_limit", 0)
    return EvidenceBackfillOptions(
        apply=apply,
        limit=limit if limit is not None else (resolved_limit or None),
        max_refs_per_page=max_refs_per_page
        if max_refs_per_page is not None
        else _int_option(config, "evidence_backfill.max_refs_per_page", 20),
        frontmatter_ref_limit=frontmatter_ref_limit
        if frontmatter_ref_limit is not None
        else _int_option(config, "evidence_backfill.frontmatter_ref_limit", 10),
        unresolved_sample_limit=unresolved_sample_limit
        if unresolved_sample_limit is not None
        else _int_option(config, "evidence_backfill.unresolved_sample_limit", 50),
        change_sample_limit=change_sample_limit
        if change_sample_limit is not None
        else _int_option(config, "evidence_backfill.change_sample_limit", 100),
        include_relation_evidence=include_relation_evidence
        if include_relation_evidence is not None
        else _bool_option(config, "evidence_backfill.include_relation_evidence", True),
        relation_evidence_types=tuple(
            relation_evidence_types
            if relation_evidence_types is not None
            else _list_option(
                config,
                "evidence_backfill.relation_evidence_types",
                ["anti_pattern_quote", "distill_extraction"],
            )
        ),
        write_frontmatter=write_frontmatter
        if write_frontmatter is not None
        else _bool_option(config, "evidence_backfill.write_frontmatter", True),
        write_report=write_report
        if write_report is not None
        else _bool_option(config, "evidence_backfill.write_report", True),
        report_dir=report_dir
        if report_dir is not None
        else str(_raw_option(config, "evidence_backfill.report_dir", "99-Reports/认知数据就绪度")),
    )


def _int_option(config: Any, key: str, default: int) -> int:
    getter = getattr(config, "get", None)
    value = getter(key, default) if callable(getter) else default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bool_option(config: Any, key: str, default: bool) -> bool:
    getter = getattr(config, "get", None)
    value = getter(key, default) if callable(getter) else default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _raw_option(config: Any, key: str, default: Any) -> Any:
    getter = getattr(config, "get", None)
    return getter(key, default) if callable(getter) else default


def _list_option(config: Any, key: str, default: list[str]) -> list[str]:
    getter = getattr(config, "get", None)
    value = getter(key, default) if callable(getter) else default
    if isinstance(value, list):
        return [str(item) for item in value if str(item or "").strip()]
    if isinstance(value, tuple):
        return [str(item) for item in value if str(item or "").strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return default


def _source_stat() -> dict[str, int | bool | str]:
    return {"exists": False, "rows": 0, "refs": 0, "path": ""}


def _load_page_states(
    db_path: Path,
    wiki_dir: Path,
    wiki_paths: set[str],
) -> dict[str, PageState]:
    states = {
        path: PageState(wiki_path=path)
        for path in sorted(_wiki_markdown_paths(wiki_dir))
    }
    if not db_path.exists():
        return states
    try:
        with _connect_ro(db_path) as conn:
            if not _table_exists(conn, "page_metrics"):
                return states
            rows = conn.execute(
                """
                SELECT wiki_path, source_count, source_refs, evidence_level
                FROM page_metrics
                """
            ).fetchall()
    except (OSError, sqlite3.Error):
        return states
    for row in rows:
        rel_path = _normalize_wiki_path(str(row["wiki_path"] or ""), wiki_dir)
        if not rel_path or rel_path not in wiki_paths:
            continue
        states[rel_path] = PageState(
            wiki_path=rel_path,
            source_count=int(row["source_count"] or 0),
            source_refs=_json_list(row["source_refs"]),
            evidence_level=int(row["evidence_level"] or 1),
        )
    return states


def _wiki_markdown_paths(wiki_dir: Path) -> set[str]:
    if not wiki_dir.exists():
        return set()
    paths: set[str] = set()
    for md_file in wiki_dir.rglob("*.md"):
        try:
            rel = md_file.relative_to(wiki_dir)
        except ValueError:
            continue
        if _ignore_wiki_path(rel):
            continue
        paths.add(str(rel).replace("\\", "/"))
    return paths


def _ignore_wiki_path(rel_path: Path) -> bool:
    return any(part.startswith(".") or part == "99-Reports" for part in rel_path.parts)


def _collect_document_links(
    db_path: Path,
    wiki_dir: Path,
    wiki_paths: set[str],
    refs_by_page: dict[str, set[str]],
    stats: dict[str, int | bool | str],
) -> None:
    stats["path"] = str(db_path)
    if not db_path.exists():
        return
    try:
        with _connect_ro(db_path) as conn:
            if not _table_exists(conn, "document_wiki_link"):
                return
            stats["exists"] = True
            rows = conn.execute(
                """
                SELECT session_id, source, wiki_page_path
                FROM document_wiki_link
                """
            ).fetchall()
    except (OSError, sqlite3.Error):
        return
    for row in rows:
        stats["rows"] = int(stats["rows"]) + 1
        page = _match_page(row["wiki_page_path"], wiki_dir, wiki_paths)
        if not page:
            continue
        refs = []
        if row["session_id"]:
            refs.append(f"document_wiki_link:session:{row['session_id']}")
        if row["source"]:
            refs.append(f"document_wiki_link:source:{row['source']}")
        _add_refs(refs_by_page, page, refs, stats)


def _collect_distill_tasks(
    db_path: Path,
    wiki_dir: Path,
    wiki_paths: set[str],
    refs_by_page: dict[str, set[str]],
    stats: dict[str, int | bool | str],
    missing_raw_refs: dict[str, Any],
    sample_limit: int,
) -> None:
    stats["path"] = str(db_path)
    if not db_path.exists():
        return
    try:
        with _connect_ro(db_path) as conn:
            if not _table_exists(conn, "distillation_tasks"):
                return
            stats["exists"] = True
            columns = _columns(conn, "distillation_tasks")
            selected = [
                "task_id",
                "session_id",
                "output_path" if "output_path" in columns else "'' AS output_path",
                "meta" if "meta" in columns else "'' AS meta",
            ]
            rows = conn.execute(
                f"SELECT {', '.join(selected)} FROM distillation_tasks"  # nosec B608
            ).fetchall()
    except (OSError, sqlite3.Error):
        return
    for row in rows:
        stats["rows"] = int(stats["rows"]) + 1
        page = _match_page(row["output_path"], wiki_dir, wiki_paths)
        if not page:
            continue
        refs = [f"distill_task:{row['task_id']}:session:{row['session_id']}"]
        meta = _loads_json_dict(row["meta"])
        raw_refs = _extract_raw_event_refs(meta)
        if not raw_refs:
            _record_missing_raw_event_ref(missing_raw_refs, row, page, sample_limit)
        refs.extend(f"raw_event:{item}" for item in raw_refs)
        _add_refs(refs_by_page, page, refs, stats)


def _collect_relation_evidence(
    db_path: Path,
    wiki_dir: Path,
    wiki_paths: set[str],
    refs_by_page: dict[str, set[str]],
    stats: dict[str, int | bool | str],
    allowed_types: set[str],
) -> None:
    stats["path"] = str(db_path)
    if not db_path.exists():
        return
    try:
        with _connect_ro(db_path) as conn:
            if not _table_exists(conn, "relations") or not _table_exists(
                conn, "relation_evidence"
            ):
                return
            stats["exists"] = True
            rows = conn.execute(
                """
                SELECT e.relation_id, e.evidence_type, r.source, r.target
                FROM relation_evidence e
                JOIN relations r ON r.id = e.relation_id
                """
            ).fetchall()
    except (OSError, sqlite3.Error):
        return
    for row in rows:
        stats["rows"] = int(stats["rows"]) + 1
        if allowed_types and row["evidence_type"] not in allowed_types:
            continue
        ref = f"kg_relation:{row['relation_id']}:{row['evidence_type']}"
        matched_pages = {
            page
            for value in (row["source"], row["target"])
            if (page := _match_page(value, wiki_dir, wiki_paths))
        }
        for page in matched_pages:
            _add_refs(refs_by_page, page, [ref], stats)


def _collect_frontmatter_provenance(
    wiki_dir: Path,
    wiki_paths: set[str],
    refs_by_page: dict[str, set[str]],
    stats: dict[str, int | bool | str],
) -> None:
    """Collect explicit frontmatter provenance already stored on Wiki pages."""
    stats["path"] = str(wiki_dir)
    if not wiki_dir.exists():
        return
    stats["exists"] = True
    for rel_path in sorted(wiki_paths):
        stats["rows"] = int(stats["rows"]) + 1
        page_path = wiki_dir / rel_path
        try:
            frontmatter, _body = parse_frontmatter(_read_page_text(page_path, errors="ignore"))
        except (OSError, ValueError, TypeError):
            continue
        refs = _frontmatter_source_refs(frontmatter)
        if refs:
            _add_refs(refs_by_page, rel_path, refs, stats)


def _frontmatter_source_refs(frontmatter: dict[str, Any] | None) -> list[str]:
    if not isinstance(frontmatter, dict):
        return []
    refs: list[str] = []
    refs.extend(
        _normalize_frontmatter_refs(
            "raw_event",
            _frontmatter_values(fm_get(frontmatter, "source_event_ids")),
        )
    )
    refs.extend(
        _normalize_frontmatter_refs(
            "evidence",
            _frontmatter_values(fm_get(frontmatter, "evidence_refs")),
        )
    )

    session_values = _frontmatter_values(fm_get(frontmatter, "source_session"))
    session_values.extend(_frontmatter_values(fm_get(frontmatter, "source_session_id")))
    session_values.extend(_frontmatter_values(fm_get(frontmatter, "source_sessions")))
    refs.extend(_normalize_frontmatter_refs("source_session", session_values))

    source_values = _frontmatter_values(fm_get(frontmatter, "source"))
    source_values.extend(_frontmatter_values(fm_get(frontmatter, "source_agent")))
    has_origin_context = bool(
        refs
        or fm_get(frontmatter, "distilled_at")
        or fm_get(frontmatter, "source_coverage")
    )
    if has_origin_context:
        refs.extend(_normalize_frontmatter_refs("source_agent", source_values))
    return _dedupe_refs(refs)


def _frontmatter_values(value: Any) -> list[str]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item or "").strip()]
    if isinstance(value, tuple):
        return [str(item).strip() for item in value if str(item or "").strip()]
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _normalize_frontmatter_refs(kind: str, values: Iterable[str]) -> list[str]:
    refs: list[str] = []
    for value in values:
        clean = str(value or "").strip()
        if not clean:
            continue
        if kind == "raw_event":
            refs.append(clean if clean.startswith("raw_event:") else f"raw_event:{clean}")
        elif kind == "evidence":
            refs.append(clean if ":" in clean else f"frontmatter:evidence:{clean}")
        elif kind == "source_session":
            refs.append(f"frontmatter:source_session:{clean}")
        elif kind == "source_agent":
            refs.append(f"frontmatter:source_agent:{clean}")
    return refs


def _dedupe_refs(refs: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for ref in refs:
        clean = str(ref or "").strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        deduped.append(clean)
    return deduped


def _add_refs(
    refs_by_page: dict[str, set[str]],
    page: str,
    refs: Iterable[str],
    stats: dict[str, int | bool | str],
) -> None:
    bucket = refs_by_page.setdefault(page, set())
    for ref in refs:
        clean = str(ref or "").strip()
        if not clean or clean in bucket:
            continue
        bucket.add(clean)
        stats["refs"] = int(stats["refs"]) + 1


def _record_missing_raw_event_ref(
    missing_raw_refs: dict[str, Any],
    row: sqlite3.Row,
    page: str,
    sample_limit: int,
) -> None:
    missing_raw_refs["count"] = int(missing_raw_refs["count"]) + 1
    samples = missing_raw_refs.setdefault("samples", [])
    if sample_limit != 0 and len(samples) >= max(0, sample_limit):
        return
    samples.append(
        {
            "wiki_path": page,
            "task_id": str(row["task_id"]),
            "session_id": str(row["session_id"]),
            "reason": "distill_task_missing_raw_event_refs",
        }
    )


def _build_changes(
    page_states: dict[str, PageState],
    refs_by_page: dict[str, set[str]],
    options: EvidenceBackfillOptions,
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for wiki_path in sorted(refs_by_page):
        state = page_states.get(wiki_path, PageState(wiki_path=wiki_path))
        merged = _merge_refs(state.refs, sorted(refs_by_page[wiki_path]), options.max_refs_per_page)
        if merged == state.refs:
            continue
        after_count = max(len(merged), state.source_count)
        changes.append(
            {
                "wiki_path": wiki_path,
                "before_source_count": state.source_count,
                "after_source_count": after_count,
                "before_evidence_level": state.evidence_level,
                "after_evidence_level": compute_evidence_level(after_count),
                "before_ref_count": len(state.refs),
                "after_ref_count": len(merged),
                "added_refs": [ref for ref in merged if ref not in set(state.refs)],
                "source_refs": merged,
            }
        )
        if options.limit is not None and len(changes) >= options.limit:
            break
    return changes


def _reported_changes(
    changes: list[dict[str, Any]],
    sample_limit: int,
) -> list[dict[str, Any]]:
    if sample_limit == 0:
        return changes
    return changes[: max(0, sample_limit)]


def _merge_refs(existing: list[str], additions: list[str], max_refs: int) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for ref in existing:
        clean = str(ref or "").strip()
        if clean and clean not in seen:
            seen.add(clean)
            merged.append(clean)
    for ref in additions:
        clean = str(ref or "").strip()
        if not clean or clean in seen:
            continue
        if max_refs > 0 and len(merged) >= max_refs:
            break
        seen.add(clean)
        merged.append(clean)
    return merged


def _unresolved_pages(
    page_states: dict[str, PageState],
    refs_by_page: dict[str, set[str]],
    sample_limit: int,
) -> dict[str, Any]:
    unresolved = [
        {
            "wiki_path": state.wiki_path,
            "reason": "no_document_link_relation_evidence_or_distill_output",
            "mark": "evidence_gap",
        }
        for state in page_states.values()
        if state.source_count <= 0 and not refs_by_page.get(state.wiki_path)
    ]
    unresolved.sort(key=lambda item: item["wiki_path"])
    sample_count = len(unresolved) if sample_limit == 0 else max(0, sample_limit)
    return {
        "source_count_zero": len(unresolved),
        "samples": unresolved[:sample_count],
    }


def _apply_changes(
    database_dir: Path,
    wiki_dir: Path,
    changes: list[dict[str, Any]],
    options: EvidenceBackfillOptions,
) -> list[dict[str, str]]:
    metrics = WikiMetrics(
        db_path=str(database_dir / "wiki_metrics.db"),
        wiki_dir=str(wiki_dir),
    )
    errors: list[dict[str, str]] = []
    for change in changes:
        wiki_path = str(change["wiki_path"])
        try:
            metrics.upsert_page(
                wiki_path,
                source_count=change["after_source_count"],
                source_refs=change["source_refs"],
                evidence_level=change["after_evidence_level"],
                _preserve_last_updated=True,
            )
            if options.write_frontmatter:
                _write_page_frontmatter(wiki_dir, change, options.frontmatter_ref_limit)
        except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
            errors.append({"wiki_path": wiki_path, "error": str(exc)})
    return errors


def _write_obsidian_report(
    wiki_dir: Path,
    report: dict[str, Any],
    options: EvidenceBackfillOptions,
) -> tuple[Path | None, str]:
    generated_at = str(report["generated_at"])
    stamp = generated_at.replace(":", "").replace("+", "Z").split(".")[0]
    report_dir = wiki_dir / options.report_dir
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"认知数据就绪度-evidence-backfill-{stamp}.md"
    lines = [
        "# 认知数据就绪度 EvidenceBackfill",
        "",
        f"- schema: `{report['schema_version']}`",
        f"- mode: `{'applied' if report['applied'] else 'dry-run'}`",
        f"- scanned_pages: {report['scanned_pages']}",
        f"- candidate_pages: {report['candidate_pages']}",
        f"- changed_pages: {report['changed_pages']}",
        f"- unresolved_source_count_zero: {report['unresolved']['source_count_zero']}",
        f"- relation_evidence_types: {', '.join(report['config']['relation_evidence_types'])}",
        "",
        "## Sources",
        "",
    ]
    for name, stats in report["sources"].items():
        lines.append(f"- `{name}`: rows={stats['rows']} refs={stats['refs']}")
    lines.extend(["", "## Applied Changes", ""])
    if report["changes"]:
        for change in report["changes"]:
            lines.append(
                f"- `{change['wiki_path']}`: "
                f"{change['before_source_count']} -> {change['after_source_count']}, "
                f"refs {change['before_ref_count']} -> {change['after_ref_count']}"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Evidence Gap Samples", ""])
    samples = report["unresolved"]["samples"]
    if samples:
        for item in samples:
            lines.append(f"- `{item['wiki_path']}`: `{item['mark']}` ({item['reason']})")
    else:
        lines.append("- none")
    lines.extend(["", "## Distill Pages Missing Raw Event Refs", ""])
    missing_raw = report["diagnostics"]["distill_missing_raw_event_refs"]
    if missing_raw["samples"]:
        for item in missing_raw["samples"]:
            lines.append(
                f"- `{item['wiki_path']}`: task `{item['task_id']}`, "
                f"session `{item['session_id']}` ({item['reason']})"
            )
    else:
        lines.append("- none")
    frontmatter = {
        "mnemos_type": "system_report",
        "report_type": "cognitive_data_readiness",
        "schema_version": report["schema_version"],
        "generated_at": generated_at,
        "applied": report["applied"],
        "changed_pages": report["changed_pages"],
        "unresolved_source_count_zero": report["unresolved"]["source_count_zero"],
        "evidence_gap_marking": "report_level",
    }
    rendered_content = write_frontmatter(frontmatter, "\n".join(lines) + "\n")
    result = submit_or_write_markdown_with_decision(
        decision_policy=EVIDENCE_BACKFILL_REPORT_MARKDOWN_POLICY,
        decision_facts={
            "schema_version": "mnemos.evidence_backfill_report_facts.v1",
            "report_schema": report["schema_version"],
            "applied": report["applied"],
            "scanned_pages": report["scanned_pages"],
            "candidate_pages": report["candidate_pages"],
            "changed_pages": report["changed_pages"],
            "errors": list(report["errors"]),
            "unresolved": dict(report["unresolved"]),
        },
        decision_task=f"Write evidence backfill report {path.name}",
        decision_goal="Publish the exact completed evidence-readiness result.",
        decision_created_at=generated_at,
        wiki_base=wiki_dir,
        target_path=path,
        content=rendered_content,
        source="evidence_backfill",
        actor="cli",
        evidence_refs=[f"evidence-backfill:{generated_at}"],
        proposed_action="write_evidence_backfill_report",
        metadata={"changed_pages": report["changed_pages"]},
    )
    if result.intercepted:
        return None, result.proposal_id
    return path, ""


def _write_page_frontmatter(
    wiki_dir: Path,
    change: dict[str, Any],
    frontmatter_ref_limit: int,
) -> None:
    page_path = wiki_dir / str(change["wiki_path"])
    if not page_path.exists():
        return
    content = _read_page_text(page_path)
    fm, body = parse_frontmatter(content)
    fm = dict(fm or {})
    refs = list(change["source_refs"])
    if frontmatter_ref_limit > 0:
        refs = refs[:frontmatter_ref_limit]
    fm["来源数量"] = change["after_source_count"]
    fm["证据级别"] = change["after_evidence_level"]
    fm["证据引用"] = refs
    fm["来源覆盖度"] = "backfilled"
    fm["统计更新时间"] = datetime.now(timezone.utc).isoformat()
    submit_or_write_markdown_with_decision(
        decision_policy=EVIDENCE_BACKFILL_MARKDOWN_POLICY,
        decision_facts={
            "schema_version": "mnemos.evidence_backfill_change_facts.v1",
            "change": dict(change),
            "frontmatter_ref_limit": frontmatter_ref_limit,
        },
        decision_task=f"Backfill evidence for {change['wiki_path']}",
        decision_goal="Apply the exact evidence metadata derived from canonical sources.",
        decision_created_at=datetime.now(timezone.utc).isoformat(),
        wiki_base=wiki_dir,
        target_path=page_path,
        content=write_frontmatter(fm, body),
        source="evidence_backfill",
        actor="cli",
        evidence_refs=[f"wiki_page:{change['wiki_path']}"] + refs[:5],
        proposed_action="backfill_evidence_frontmatter",
        expected_existing_hash=sha256_text(content),
        metadata={"source_count": change["after_source_count"]},
    )


def _read_page_text(page_path: Path, errors: str = "strict") -> str:
    return page_path.read_text(encoding="utf-8", errors=errors)


def _match_page(raw_path: Any, wiki_dir: Path, wiki_paths: set[str]) -> str:
    for candidate in _page_candidates(raw_path, wiki_dir):
        if candidate in wiki_paths:
            return candidate
    return ""


def _page_candidates(raw_path: Any, wiki_dir: Path) -> list[str]:
    normalized = _normalize_wiki_path(str(raw_path or ""), wiki_dir)
    if not normalized:
        return []
    candidates = [normalized]
    if not normalized.endswith(".md"):
        candidates.append(f"{normalized}.md")
    if normalized.endswith(".md"):
        candidates.append(normalized[:-3])
    seen: set[str] = set()
    result: list[str] = []
    for item in candidates:
        clean = item.replace("\\", "/")
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result


def _normalize_wiki_path(raw_path: str, wiki_dir: Path) -> str:
    cleaned = raw_path.strip()
    if not cleaned:
        return ""
    path = Path(cleaned)
    if path.is_absolute():
        try:
            return str(path.resolve().relative_to(wiki_dir.resolve())).replace("\\", "/")
        except ValueError:
            return str(path).replace("\\", "/")
    return cleaned.replace("\\", "/")


def _configured_wiki_dir(runtime: Any) -> Path:
    direct = getattr(runtime, "wiki_dir", None)
    if direct:
        return Path(direct).expanduser()
    try:
        return Path(runtime.vault_dir("mnemos")).expanduser()
    except (AttributeError, KeyError, TypeError):
        return Path(get_config().wiki_dir).expanduser()


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item or "").strip()]
    if value is None or str(value).strip() in EMPTY_REF_VALUES:
        return []
    try:
        parsed = json.loads(str(value))
    except (json.JSONDecodeError, TypeError, ValueError):
        return [str(value)]
    if isinstance(parsed, list):
        return [str(item) for item in parsed if str(item or "").strip()]
    if parsed in (None, "", {}, []):
        return []
    return [str(parsed)]


def _loads_json_dict(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _extract_raw_event_refs(payload: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for key in ("raw_event_refs", "raw_event_ids", "source_event_ids"):
        value = payload.get(key)
        if isinstance(value, list):
            refs.extend(str(item) for item in value if str(item or "").strip())
        elif isinstance(value, str) and value.strip():
            refs.append(value.strip())
    seen: set[str] = set()
    result: list[str] = []
    for item in refs:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
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
