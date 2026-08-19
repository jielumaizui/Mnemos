"""Incremental page lifecycle operations for the Wiki metrics projection."""

from __future__ import annotations

import logging
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List

from core.frontmatter import fm_get, parse_frontmatter

logger = logging.getLogger(__name__)


def parse_provenance_frontmatter(content: str) -> tuple[int, List[str], int]:
    """Read normalized source count, references, and evidence level."""

    if not content.startswith("---"):
        return 0, [], 1
    parts = content.split("---", 2)
    if len(parts) < 3:
        return 0, [], 1
    fm, _body = parse_frontmatter(content)
    if not isinstance(fm, dict):
        return 0, [], 1
    raw_sources = fm.get("来源", fm.get("sources", []))
    if isinstance(raw_sources, str):
        source_refs = [raw_sources] if raw_sources.strip() else []
    elif isinstance(raw_sources, list):
        source_refs = [str(item) for item in raw_sources if str(item).strip()]
    else:
        source_refs = []
    try:
        source_count = int(fm_get(fm, "source_count", len(source_refs)) or 0)
    except (ValueError, TypeError):
        source_count = len(source_refs)
    evidence_level = {
        "单源": 1,
        "single": 1,
        "多源": 2,
        "multiple": 2,
        "已验证": 3,
        "verified": 3,
    }.get(str(fm_get(fm, "evidence_level", "单源") or "单源"), 1)
    return max(source_count, len(source_refs)), source_refs, evidence_level


def canonical_metric_path(wiki_dir: Path, path: str) -> str:
    """Normalize a metrics key to a Wiki-relative Markdown path when possible."""

    text = str(path).replace("\\", "/")
    try:
        candidate = Path(text).expanduser()
        if candidate.is_absolute():
            try:
                text = str(candidate.relative_to(wiki_dir)).replace("\\", "/")
            except ValueError:
                text = str(candidate).replace("\\", "/")
    except (OSError, ValueError, TypeError):
        logger.debug("Path normalization failed for %s", path, exc_info=True)
    if text and not text.endswith(".md") and not text.endswith(".shadow"):
        text = f"{text}.md"
    return text


def path_candidates(wiki_dir: Path, path: str) -> List[str]:
    """Return compatible absolute and Wiki-relative metrics keys."""

    text = str(path).replace("\\", "/")
    candidates: List[str] = []
    try:
        candidate = Path(text).expanduser()
        if candidate.is_absolute():
            try:
                candidates.append(str(candidate.relative_to(wiki_dir)).replace("\\", "/"))
            except ValueError:
                candidates.append(str(candidate).replace("\\", "/"))
    except (OSError, ValueError, TypeError):
        logger.debug("Path candidate generation failed for %s", path, exc_info=True)
    candidates.append(text)
    if text.endswith(".md"):
        candidates.append(text[:-3])
    elif text:
        candidates.append(f"{text}.md")
    candidates.append(canonical_metric_path(wiki_dir, text))
    return list(dict.fromkeys(candidates))


def refresh_page_file(
    metrics: Any,
    md_file: Path,
    rel_path: str,
    *,
    score_content: Callable[[str], float],
    classify_role: Callable[[str, str], str],
) -> tuple[int, int]:
    """Recompute and upsert one page metrics record."""

    payload = build_refresh_payload(
        metrics,
        md_file,
        rel_path,
        existing_metrics=metrics.get_page(rel_path),
        score_content=score_content,
        classify_role=classify_role,
    )
    inserted, updated = metrics._upsert_scanned_page(rel_path, payload)
    return int(inserted), int(updated)


def build_refresh_payload(
    metrics: Any,
    md_file: Path,
    rel_path: str,
    *,
    existing_metrics: Any,
    score_content: Callable[[str], float],
    classify_role: Callable[[str, str], str],
) -> Dict[str, Any]:
    """Fully validate and compute a page row before beginning a move transaction."""

    content = md_file.read_text(encoding="utf-8", errors="ignore")
    title, status, tags, knowledge_stage = metrics._parse_page_frontmatter(content, rel_path)
    source_count, source_refs, evidence_level = parse_provenance_frontmatter(content)
    quality_score = score_content(content)
    quality_level = metrics._compute_quality_level(quality_score)
    if status == "draft" and quality_score >= 60:
        status = "active"
    if knowledge_stage == "P3":
        knowledge_stage = "P1" if quality_score >= 80 else "P2" if quality_score >= 60 else "P3"
    payload = metrics._build_page_payload(content, md_file.stat(), existing_metrics)
    payload.update(
        {
            "title": title,
            "page_role": classify_role(content, rel_path),
            "status": status,
            "tags": tags,
            "knowledge_stage": knowledge_stage,
            "source_count": source_count,
            "source_refs": source_refs,
            "evidence_level": evidence_level,
            "quality_score": round(quality_score, 1),
            "quality_level": quality_level,
        }
    )
    return dict(payload)


_METRIC_COLUMNS = (
    "title", "page_role", "knowledge_stage", "evidence_level", "source_count",
    "source_refs", "heat_level", "heat_score", "quality_score", "quality_level",
    "completeness", "freshness_days", "backlink_count", "status", "last_updated",
    "last_accessed", "created_at", "tags",
)


def _upsert_payload_on_conn(
    conn: sqlite3.Connection,
    rel_path: str,
    payload: Dict[str, Any],
    prior_row: sqlite3.Row | None,
) -> tuple[int, int]:
    """Upsert a complete metrics row without committing the caller's transaction."""

    now = datetime.now(timezone.utc).isoformat()
    values: Dict[str, Any] = {
        "title": "", "page_role": "knowledge", "knowledge_stage": "P3",
        "evidence_level": 1, "source_count": 0, "source_refs": [],
        "heat_level": "cold", "heat_score": 0.0, "quality_score": 0.0,
        "quality_level": "acceptable", "completeness": 0.0,
        "freshness_days": 999, "backlink_count": 0, "status": "draft",
        "last_updated": now, "last_accessed": None, "created_at": now, "tags": [],
    }
    if prior_row is not None:
        values.update({column: prior_row[column] for column in _METRIC_COLUMNS})
    values.update(payload)
    for column in ("source_refs", "tags"):
        if isinstance(values[column], list):
            values[column] = json.dumps(values[column], ensure_ascii=False)
    assignments = ", ".join(f"{column}=excluded.{column}" for column in _METRIC_COLUMNS)
    placeholders = ", ".join("?" for _ in range(len(_METRIC_COLUMNS) + 1))
    conn.execute(
        f"INSERT INTO page_metrics (wiki_path, {', '.join(_METRIC_COLUMNS)}) "
        f"VALUES ({placeholders}) ON CONFLICT(wiki_path) DO UPDATE SET {assignments}",  # nosec B608
        (rel_path, *(values[column] for column in _METRIC_COLUMNS)),
    )
    return (0, 1) if prior_row is not None and prior_row["wiki_path"] == rel_path else (1, 0)


def reconcile_page_lifecycle(
    metrics: Any, *, page_path: str, previous_path: str, mutation_type: str
) -> Dict[str, int | str]:
    """Apply one create/update/move/delete to the metrics projection."""

    old_key = canonical_metric_path(metrics.wiki_dir, previous_path or page_path)
    if mutation_type == "delete":
        conn = metrics._get_conn()
        with conn:
            cursor = conn.execute("DELETE FROM page_metrics WHERE wiki_path=?", (old_key,))
        removed = int(cursor.rowcount or 0)
        return {"status": "ok", "inserted": 0, "updated": 0, "deleted": removed}
    path = Path(page_path).expanduser()
    if not path.is_file():
        return {"status": "page_not_found", "inserted": 0, "updated": 0, "deleted": 0}
    try:
        rel_path = str(path.resolve().relative_to(metrics.wiki_dir.resolve()))
    except ValueError:
        return {"status": "invalid_path", "inserted": 0, "updated": 0, "deleted": 0}

    existing = metrics.get_page(old_key if mutation_type == "move" else rel_path)
    payload = build_refresh_payload(
        metrics,
        path,
        rel_path,
        existing_metrics=existing,
        score_content=metrics._score_content,
        classify_role=metrics._classify_page_role,
    )
    conn = metrics._get_conn()
    prior_row = conn.execute(
        "SELECT * FROM page_metrics WHERE wiki_path IN (?, ?) "
        "ORDER BY CASE WHEN wiki_path=? THEN 0 ELSE 1 END LIMIT 1",
        (old_key, rel_path, old_key),
    ).fetchone()
    with conn:
        removed = 0
        if mutation_type == "move" and old_key != rel_path:
            cursor = conn.execute("DELETE FROM page_metrics WHERE wiki_path=?", (old_key,))
            removed = int(cursor.rowcount or 0)
        inserted, updated = _upsert_payload_on_conn(conn, rel_path, payload, prior_row)
    return {"status": "ok", "inserted": inserted, "updated": updated, "deleted": removed}


class WikiMetricsLifecycleMixin:
    """Expose incremental lifecycle operations on the WikiMetrics facade."""

    def _refresh_page_file(self: Any, md_file: Path, rel_path: str) -> tuple[int, int]:
        return refresh_page_file(
            self,
            md_file,
            rel_path,
            score_content=self._score_content,
            classify_role=self._classify_page_role,
        )

    @staticmethod
    def _parse_provenance_frontmatter(content: str) -> tuple[int, List[str], int]:
        return parse_provenance_frontmatter(content)

    def reconcile_page_lifecycle(
        self: Any, *, page_path: str, previous_path: str = "", mutation_type: str = "update"
    ) -> Dict[str, int | str]:
        return reconcile_page_lifecycle(
            self,
            page_path=page_path,
            previous_path=previous_path,
            mutation_type=mutation_type,
        )

    def _canonical_metric_path(self: Any, path: str) -> str:
        return canonical_metric_path(self.wiki_dir, path)

    def _path_candidates(self: Any, path: str) -> List[str]:
        return path_candidates(self.wiki_dir, path)
