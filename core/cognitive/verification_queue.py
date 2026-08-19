"""Controlled verification queue for evidence-backed cognitive proposals."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, cast

from core.utils import atomic_write_text

SCHEMA_VERSION = "mnemos.verification_report.v1"

logger = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS verification_queue (
    task_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    confidence REAL NOT NULL,
    evidence_json TEXT NOT NULL,
    commands_json TEXT NOT NULL,
    proposal_json TEXT NOT NULL,
    report_id TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_verification_queue_status
ON verification_queue(status);

CREATE INDEX IF NOT EXISTS idx_verification_queue_source
ON verification_queue(source_type, source_id);

CREATE TABLE IF NOT EXISTS verification_runs (
    report_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    task_count INTEGER NOT NULL,
    mode TEXT NOT NULL,
    report_path TEXT NOT NULL,
    report_json TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class VerificationTask:
    task_id: str
    source_type: str
    source_id: str
    subject: str
    severity: str
    confidence: float
    conclusion: str
    suggested_action: str
    evidence_refs: list[str] = field(default_factory=list)
    verification_commands: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"

    @property
    def has_controlled_evidence(self) -> bool:
        return bool(self.evidence_refs or self.verification_commands)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "subject": self.subject,
            "severity": self.severity,
            "confidence": round(float(self.confidence), 4),
            "conclusion": self.conclusion,
            "suggested_action": self.suggested_action,
            "evidence_refs": list(self.evidence_refs),
            "verification_commands": list(self.verification_commands),
            "metadata": dict(self.metadata),
            "status": self.status,
            "has_controlled_evidence": self.has_controlled_evidence,
        }


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task_id(source_type: str, source_id: str) -> str:
    raw = f"{source_type}:{source_id}".encode("utf-8")
    digest = hashlib.sha1(raw, usedforsecurity=False).hexdigest()[:16]
    return f"verify-{digest}"


def _to_float(value: Any, default: float = 0.5) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _severity_rank(severity: str) -> int:
    return {
        "critical": 0,
        "extreme": 0,
        "high": 1,
        "medium": 2,
        "low": 3,
    }.get(str(severity or "").lower(), 4)


class VerificationQueue:
    """Plan and persist controlled verification proposals.

    The queue never executes verification commands and never edits code or Wiki body
    content. Applying a run only writes the verification DB and report file.
    """

    def __init__(
        self,
        *,
        config: Any | None = None,
        db_path: Path | None = None,
        report_path: Path | None = None,
        wiki_base: Path | None = None,
    ) -> None:
        if config is None:
            from core.config import get_config

            config = get_config()
        self.config = config
        database_dir = Path(getattr(config, "database_dir", Path.home() / ".mnemos"))
        self.db_path = Path(
            db_path
            or self._cfg_path(
                "verification_queue.db_path",
                database_dir / "verification_queue.db",
            )
        )
        self.report_path = Path(
            report_path
            or self._cfg_path(
                "verification_queue.report_path",
                database_dir / "verification_report.md",
            )
        )
        wiki_root = wiki_base if wiki_base is not None else getattr(config, "wiki_dir", None)
        self.wiki_base = Path(str(wiki_root or Path.cwd())).expanduser()

    def plan(
        self,
        *,
        limit: int | None = None,
        source_rows: Mapping[str, list[Mapping[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        candidates = self._collect(source_rows=source_rows)
        candidates.sort(
            key=lambda item: (
                _severity_rank(item.severity),
                -float(item.confidence),
                item.source_type,
                item.subject,
            )
        )
        configured_limit = self._cfg_int("verification_queue.max_candidates", 50)
        final_limit = max(0, int(limit if limit is not None else configured_limit))
        if final_limit:
            candidates = candidates[:final_limit]

        counts: dict[str, int] = {}
        for task in candidates:
            counts[task.source_type] = counts.get(task.source_type, 0) + 1

        tasks = [task.to_dict() for task in candidates]
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _utc_now(),
            "mode": "plan",
            "status": "ok",
            "task_count": len(tasks),
            "counts": counts,
            "conclusions_have_evidence": all(task["has_controlled_evidence"] for task in tasks),
            "writes": {
                "verification_db": False,
                "report": False,
                "wiki_body": False,
                "code": False,
            },
            "tasks": tasks,
        }

    def run(
        self,
        *,
        apply: bool = False,
        limit: int | None = None,
        source_rows: Mapping[str, list[Mapping[str, Any]]] | None = None,
        background: bool = False,
        budget: Any | None = None,
    ) -> dict[str, Any]:
        if not self._cfg_bool("verification_queue.enabled", True):
            return {
                "schema_version": SCHEMA_VERSION,
                "generated_at": _utc_now(),
                "status": "disabled",
                "reason": "verification_queue_disabled",
                "task_count": 0,
                "tasks": [],
                "writes": {
                    "verification_db": False,
                    "report": False,
                    "wiki_body": False,
                    "code": False,
                },
            }
        if background and self._cfg_bool("verification_queue.respect_resource_budget", True):
            deferred = self._resource_deferral(budget=budget)
            if deferred is not None:
                return deferred

        report = self.plan(limit=limit, source_rows=source_rows)
        report["mode"] = "apply" if apply else "dry_run"
        report["writes"] = {
            "verification_db": bool(apply),
            "report": bool(apply),
            "wiki_body": False,
            "code": False,
        }
        if not apply:
            return report

        self._init_db()
        report_id = self._report_id(report)
        tasks = list(report.get("tasks", []))
        self._write_queue(report_id, tasks)
        report_path = self._write_report(report_id, report)
        report["report_id"] = report_id
        report["db_path"] = str(self.db_path)
        report["report_path"] = str(report_path)
        self._record_run(report_id, report)
        return report

    def _collect(
        self,
        *,
        source_rows: Mapping[str, list[Mapping[str, Any]]] | None = None,
    ) -> list[VerificationTask]:
        rows = source_rows or {
            "disputes": self._collect_disputes(),
            "blindspots": self._collect_blindspots(),
            "freshness": self._collect_freshness_alerts(),
        }
        tasks: list[VerificationTask] = []
        tasks.extend(self._tasks_from_disputes(rows.get("disputes", [])))
        tasks.extend(self._tasks_from_blindspots(rows.get("blindspots", [])))
        tasks.extend(self._tasks_from_freshness(rows.get("freshness", [])))

        deduped: dict[str, VerificationTask] = {}
        for task in tasks:
            if task.has_controlled_evidence:
                deduped.setdefault(task.task_id, task)
        return list(deduped.values())

    def _collect_disputes(self) -> list[Mapping[str, Any]]:
        try:
            from core.app.dispute_resolver import DisputeResolver

            resolver = DisputeResolver(wiki_base=str(self.wiki_base))
            rows = list(resolver.get_unresolved_disputes())[: self._cfg_int("verification_queue.max_disputes", 10)]
            return cast(list[Mapping[str, Any]], rows)
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
            logger.debug("[verification_queue] dispute collection failed", exc_info=True)
            return []

    def _collect_blindspots(self) -> list[Mapping[str, Any]]:
        db_path = Path(
            self._cfg_path(
                "verification_queue.blindspots_db_path",
                Path(getattr(self.config, "database_dir", self.db_path.parent)) / "blindspots.db",
            )
        )
        if not db_path.exists():
            return []
        try:
            with sqlite3.connect(str(db_path), timeout=10) as conn:
                conn.row_factory = sqlite3.Row  # noqa
                rows = conn.execute(
                    """
                    SELECT topic, description, confidence, status, detected_at, resolved_by_page
                    FROM blindspots
                    WHERE status IN ('detected', 'reminded', 'investigating')
                    ORDER BY confidence DESC, detected_at DESC
                    LIMIT ?
                    """,
                    (self._cfg_int("verification_queue.max_blindspots", 10),),
                ).fetchall()
                mapped_rows = [dict(row) for row in rows]
                return cast(list[Mapping[str, Any]], mapped_rows)
        except sqlite3.Error:
            logger.debug("[verification_queue] blindspot collection failed", exc_info=True)
            return []

    def _collect_freshness_alerts(self) -> list[Mapping[str, Any]]:
        try:
            from core.app.freshness_refresh_worker import FreshnessRefreshWorker

            worker = FreshnessRefreshWorker(wiki_base=str(self.wiki_base))
            rows = list(worker.list_pages(status_filter="stale"))[
                : self._cfg_int("verification_queue.max_freshness_alerts", 10)
            ]
            return cast(list[Mapping[str, Any]], rows)
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
            logger.debug("[verification_queue] freshness collection failed", exc_info=True)
            return []

    def _tasks_from_disputes(
        self, rows: list[Mapping[str, Any]]
    ) -> list[VerificationTask]:
        tasks: list[VerificationTask] = []
        for row in rows:
            path = str(row.get("path") or row.get("page_path") or "")
            if not path:
                continue
            days_old = row.get("days_old", 0)
            severity = "high" if row.get("needs_escalation") else "medium"
            evidence = [f"wiki:{path}", f"days_old={days_old}"]
            tasks.append(
                VerificationTask(
                    task_id=_task_id("dispute", path),
                    source_type="dispute",
                    source_id=path,
                    subject=str(row.get("title") or Path(path).stem),
                    severity=severity,
                    confidence=0.85 if row.get("needs_escalation") else 0.7,
                    conclusion="unresolved_dispute_needs_verification",
                    suggested_action="先查看争议评分和证据上下文，再由用户选择解决方案。",
                    evidence_refs=evidence,
                    verification_commands=[
                        f"python3 mnemos_cli.py dispute show {json.dumps(path, ensure_ascii=False)}",
                        f"python3 mnemos_cli.py wiki read {json.dumps(path, ensure_ascii=False)} --depth summary",
                    ],
                    metadata={"needs_escalation": bool(row.get("needs_escalation"))},
                )
            )
        return tasks

    def _tasks_from_blindspots(
        self, rows: list[Mapping[str, Any]]
    ) -> list[VerificationTask]:
        tasks: list[VerificationTask] = []
        for row in rows:
            topic = str(row.get("topic") or "")
            if not topic:
                continue
            description = str(row.get("description") or "")
            confidence = _to_float(row.get("confidence"), 0.5)
            severity = "high" if confidence >= 0.75 else "medium"
            evidence = [
                f"blindspots.db:{topic}",
                f"status={row.get('status') or 'detected'}",
                f"confidence={confidence:.3f}",
            ]
            if description:
                evidence.append(f"description={description[:160]}")
            tasks.append(
                VerificationTask(
                    task_id=_task_id("blindspot", topic),
                    source_type="blindspot",
                    source_id=topic,
                    subject=topic,
                    severity=severity,
                    confidence=confidence,
                    conclusion="blindspot_requires_source_research",
                    suggested_action="先做受控搜索或读取现有 Wiki，再决定是否记录新知识。",
                    evidence_refs=evidence,
                    verification_commands=[
                        f"python3 mnemos_cli.py search {json.dumps(topic, ensure_ascii=False)} --limit 5",
                        "python3 mnemos_cli.py blindspot resolve "
                        f"{json.dumps(topic, ensure_ascii=False)} --page <verified-wiki-page>",
                    ],
                    metadata={"detected_at": row.get("detected_at") or ""},
                )
            )
        return tasks

    def _tasks_from_freshness(
        self, rows: list[Mapping[str, Any]]
    ) -> list[VerificationTask]:
        tasks: list[VerificationTask] = []
        for row in rows:
            path = str(row.get("path") or row.get("page_path") or "")
            if not path:
                continue
            severity = str(row.get("severity") or "medium").lower()
            message = str(row.get("message") or row.get("reason") or "")
            evidence = [f"wiki:{path}", f"freshness_status={row.get('status') or 'stale'}"]
            if message:
                evidence.append(f"message={message[:180]}")
            tasks.append(
                VerificationTask(
                    task_id=_task_id("freshness", path),
                    source_type="freshness",
                    source_id=path,
                    subject=Path(path).stem,
                    severity=severity,
                    confidence=0.75 if severity in {"critical", "high"} else 0.65,
                    conclusion="stale_knowledge_needs_verification_before_refresh",
                    suggested_action="先读取页面和来源，再决定是否刷新正文或只标注待确认。",
                    evidence_refs=evidence,
                    verification_commands=[
                        "python3 mnemos_cli.py freshness list --status stale",
                        f"python3 mnemos_cli.py wiki read {json.dumps(path, ensure_ascii=False)} --depth metadata",
                    ],
                    metadata={"alert_type": row.get("type") or ""},
                )
            )
        return tasks

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.commit()

    def _write_queue(self, report_id: str, tasks: list[Mapping[str, Any]]) -> None:
        now = _utc_now()
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            for task in tasks:
                conn.execute(
                    """
                    INSERT INTO verification_queue (
                        task_id, created_at, updated_at, source_type, source_id,
                        subject, severity, status, confidence, evidence_json,
                        commands_json, proposal_json, report_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_id) DO UPDATE SET
                        updated_at=excluded.updated_at,
                        severity=excluded.severity,
                        status=excluded.status,
                        confidence=excluded.confidence,
                        evidence_json=excluded.evidence_json,
                        commands_json=excluded.commands_json,
                        proposal_json=excluded.proposal_json,
                        report_id=excluded.report_id
                    """,
                    (
                        task["task_id"],
                        now,
                        now,
                        task["source_type"],
                        task["source_id"],
                        task["subject"],
                        task["severity"],
                        task["status"],
                        float(task["confidence"]),
                        _json_dumps(task["evidence_refs"]),
                        _json_dumps(task["verification_commands"]),
                        _json_dumps(task),
                        report_id,
                    ),
                )
            conn.commit()

    def _write_report(self, report_id: str, report: Mapping[str, Any]) -> Path:
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        text = format_verification_report_text(report, report_id=report_id)
        atomic_write_text(self.report_path, text, encoding="utf-8")
        return self.report_path

    def _record_run(self, report_id: str, report: Mapping[str, Any]) -> None:
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO verification_runs (
                    report_id, created_at, task_count, mode, report_path, report_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    str(report.get("generated_at") or _utc_now()),
                    int(report.get("task_count") or 0),
                    str(report.get("mode") or ""),
                    str(self.report_path),
                    _json_dumps(report),
                ),
            )
            conn.commit()

    def _resource_deferral(self, *, budget: Any | None = None) -> dict[str, Any] | None:
        try:
            if budget is None:
                from core.resource_budget import get_budget

                budget = get_budget()
            if budget.can_run("verification_queue"):
                return None
            delay = budget.throttle_delay("verification_queue")
            status = budget.status()
            return {
                "schema_version": SCHEMA_VERSION,
                "generated_at": _utc_now(),
                "status": "deferred",
                "reason": "resource_budget",
                "resource_state": status.get("state", "unknown"),
                "resource_status": status,
                "retry_after_seconds": max(1, int(delay or 60)),
                "task_count": 0,
                "tasks": [],
                "writes": {
                    "verification_db": False,
                    "report": False,
                    "wiki_body": False,
                    "code": False,
                },
            }
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
            logger.debug("[verification_queue] resource budget check failed", exc_info=True)
            return None

    def _report_id(self, report: Mapping[str, Any]) -> str:
        raw = _json_dumps(
            {
                "generated_at": report.get("generated_at"),
                "task_ids": [task.get("task_id") for task in report.get("tasks", [])],
            }
        ).encode("utf-8")
        return "verification-" + hashlib.sha1(raw, usedforsecurity=False).hexdigest()[:16]

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError, AttributeError):
            return default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        try:
            return bool(self.config.get(key, default))
        except AttributeError:
            return default

    def _cfg_path(self, key: str, default: Path) -> Path:
        try:
            value = self.config.get(key, None)
        except AttributeError:
            value = None
        return Path(value).expanduser() if value else Path(default)


def format_verification_report_text(report: Mapping[str, Any], report_id: str = "") -> str:
    lines = [
        "# Mnemos Verification Report",
        "",
        f"- schema_version: {report.get('schema_version', SCHEMA_VERSION)}",
        f"- report_id: {report_id or report.get('report_id', '')}",
        f"- generated_at: {report.get('generated_at', '')}",
        f"- mode: {report.get('mode', '')}",
        f"- status: {report.get('status', '')}",
        f"- task_count: {report.get('task_count', 0)}",
        f"- conclusions_have_evidence: {report.get('conclusions_have_evidence', False)}",
        "- writes:",
    ]
    writes = dict(report.get("writes") or {})
    for key in ("verification_db", "report", "wiki_body", "code"):
        lines.append(f"  - {key}: {bool(writes.get(key))}")
    lines.append("")
    tasks = list(report.get("tasks") or [])
    if not tasks:
        lines.append("_No verification tasks._")
        return "\n".join(lines)

    for task in tasks:
        lines.extend(
            [
                f"## {task.get('source_type')} / {task.get('subject')}",
                "",
                f"- task_id: {task.get('task_id')}",
                f"- severity: {task.get('severity')}",
                f"- confidence: {task.get('confidence')}",
                f"- conclusion: {task.get('conclusion')}",
                f"- suggested_action: {task.get('suggested_action')}",
                "- evidence_refs:",
            ]
        )
        for evidence in task.get("evidence_refs") or []:
            lines.append(f"  - {evidence}")
        lines.append("- verification_commands:")
        for command in task.get("verification_commands") or []:
            lines.append(f"  - `{command}`")
        lines.append("")
    return "\n".join(lines)


def run_verification_queue(
    *,
    apply: bool = False,
    background: bool = False,
    limit: int | None = None,
    config: Any | None = None,
) -> dict[str, Any]:
    return VerificationQueue(config=config).run(
        apply=apply,
        background=background,
        limit=limit,
    )
