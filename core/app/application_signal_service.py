"""Application signal detection service with persistence and optional reminders."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

from core.app.application_signal_detectors import (
    AppSignal,
    AvoidanceSignalDetector,
    CrossAgentDivergenceDetector,
    FreshnessSignalChecker,
)
from core.config import get_config
from core.db_utils import delete_older_than
from core.frontmatter import parse_frontmatter
from core.utils import atomic_write_text


SCHEMA = """
CREATE TABLE IF NOT EXISTS application_signals (
    signal_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    topic TEXT NOT NULL,
    confidence REAL NOT NULL,
    severity TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    suggested_action TEXT,
    cooldown_days INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    notify_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_application_signals_kind ON application_signals(kind);
CREATE INDEX IF NOT EXISTS idx_application_signals_status ON application_signals(status);
CREATE INDEX IF NOT EXISTS idx_application_signals_last_seen ON application_signals(last_seen_at);
"""


class ApplicationSignalService:
    """Run application-level detectors and persist visible, explainable results."""

    def __init__(self, config=None, db_path: Path | None = None, reminder_queue=None):
        self.config = config or get_config()
        self.db_path = Path(db_path or (self.config.database_dir / "application_signals.db"))
        self.report_path = self.config.database_dir / "application_signals_report.md"
        self.reminder_queue = reminder_queue
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.commit()

    def run(
        self,
        *,
        avoidance_history: Iterable[Dict[str, Any]] | None = None,
        divergence_outputs: Iterable[Dict[str, Any]] | None = None,
        freshness_pages: Iterable[Dict[str, Any]] | None = None,
    ) -> Dict[str, Any]:
        if not self.config.get("application_signals.enabled", True):
            return {"enabled": False, "detected": 0, "persisted": 0, "cooldown_skipped": 0}

        signals = self.detect(
            avoidance_history=avoidance_history,
            divergence_outputs=divergence_outputs,
            freshness_pages=freshness_pages,
        )
        persisted, skipped = self.persist(signals)
        enqueued = 0
        if self.config.get("application_signals.auto_notify", False):
            enqueued = self.enqueue_reminders(persisted)
        self.write_report()
        return {
            "enabled": True,
            "detected": len(signals),
            "persisted": len(persisted),
            "cooldown_skipped": skipped,
            "reminders_enqueued": enqueued,
            "db_path": str(self.db_path),
            "report_path": str(self.report_path),
        }

    def detect(
        self,
        *,
        avoidance_history: Iterable[Dict[str, Any]] | None = None,
        divergence_outputs: Iterable[Dict[str, Any]] | None = None,
        freshness_pages: Iterable[Dict[str, Any]] | None = None,
    ) -> List[AppSignal]:
        signals: List[AppSignal] = []
        if self.config.get("application_signals.avoidance.enabled", True):
            signals.extend(AvoidanceSignalDetector().detect(avoidance_history or []))
        if self.config.get("application_signals.cross_agent_divergence.enabled", True):
            signals.extend(CrossAgentDivergenceDetector().detect(divergence_outputs or []))
        if self.config.get("application_signals.freshness.enabled", True):
            checker = FreshnessSignalChecker()
            pages = list(freshness_pages) if freshness_pages is not None else self._wiki_pages()
            for page in pages:
                signal = checker.check(page)
                if signal is not None:
                    signals.append(signal)
        return signals

    def persist(self, signals: Iterable[AppSignal]) -> tuple[List[AppSignal], int]:
        persisted: List[AppSignal] = []
        cooldown_skipped = 0
        now = datetime.now(timezone.utc)
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.row_factory = sqlite3.Row  # noqa
            for signal in signals:
                signal_id = self._signal_id(signal)
                row = conn.execute(
                    "SELECT last_seen_at, cooldown_days FROM application_signals WHERE signal_id=?",
                    (signal_id,),
                ).fetchone()
                if row and self._within_cooldown(
                    row["last_seen_at"], int(row["cooldown_days"] or signal.cooldown_days), now
                ):
                    cooldown_skipped += 1
                    continue
                payload = signal.as_dict()
                conn.execute(
                    """
                    INSERT INTO application_signals (
                        signal_id, kind, topic, confidence, severity, evidence_json,
                        suggested_action, cooldown_days, status, created_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                    ON CONFLICT(signal_id) DO UPDATE SET
                        confidence=excluded.confidence,
                        severity=excluded.severity,
                        evidence_json=excluded.evidence_json,
                        suggested_action=excluded.suggested_action,
                        cooldown_days=excluded.cooldown_days,
                        status='active',
                        last_seen_at=excluded.last_seen_at
                    """,
                    (
                        signal_id,
                        signal.kind,
                        signal.topic,
                        signal.confidence,
                        signal.severity,
                        json.dumps(payload["evidence"], ensure_ascii=False),
                        signal.suggested_action,
                        signal.cooldown_days,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                persisted.append(signal)
            conn.commit()
        return persisted, cooldown_skipped

    def enqueue_reminders(self, signals: Iterable[AppSignal]) -> int:
        if self.reminder_queue is None:
            from core.kia.dialog_reminder import DialogReminderQueue

            self.reminder_queue = DialogReminderQueue()
        count = 0
        for signal in signals:
            self.reminder_queue.enqueue(
                issue_id=f"app_signal:{signal.kind}:{self._signal_id(signal)[:12]}",
                page_path=f"app-signals/{signal.kind}/{signal.topic}",
                severity=signal.severity,
                content=self._reminder_content(signal),
                choices=["知道了", "稍后处理", "忽略此类"],
            )
            count += 1
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            for signal in signals:
                conn.execute(
                    "UPDATE application_signals SET notify_count = notify_count + 1 WHERE signal_id=?",
                    (self._signal_id(signal),),
                )
            conn.commit()
        return count

    def write_report(self, limit: int = 20) -> None:
        rows = self.list_signals(limit=limit)
        lines = ["# Application Signals", "", f"updated_at: {datetime.now().isoformat()}", ""]
        if not rows:
            lines.append("_No active application signals._")
        for row in rows:
            evidence = json.loads(row["evidence_json"] or "[]")
            lines.extend(
                [
                    f"## {row['kind']} / {row['topic']}",
                    "",
                    f"- severity: {row['severity']}",
                    f"- confidence: {row['confidence']:.3f}",
                    f"- suggested_action: {row['suggested_action'] or ''}",
                    f"- evidence: {', '.join(str(item) for item in evidence)}",
                    "",
                ]
            )
        atomic_write_text(self.report_path, "\n".join(lines), encoding="utf-8")

    def cleanup_older_than(self, days: int, dry_run: bool = False) -> int:
        """清理/统计 created_at 早于保留期限的应用层信号。"""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            return delete_older_than(conn, "application_signals", "created_at", days, dry_run=dry_run)

    def list_signals(self, limit: int = 20) -> List[sqlite3.Row]:
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.row_factory = sqlite3.Row  # noqa
            return list(
                conn.execute(
                    """
                    SELECT * FROM application_signals
                    WHERE status='active'
                    ORDER BY severity DESC, confidence DESC, last_seen_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            )

    def _wiki_pages(self, limit: int = 200) -> List[Dict[str, Any]]:
        wiki_dir = self.config.wiki_dir
        if not wiki_dir.exists():
            return []
        pages: List[Dict[str, Any]] = []
        for md_file in wiki_dir.rglob("*.md"):
            if len(pages) >= limit:
                break
            try:
                text = md_file.read_text(encoding="utf-8", errors="ignore")
                fm, _ = parse_frontmatter(text)
                updated = (fm or {}).get("更新日期") or (fm or {}).get("updated_at")
                pages.append(
                    {
                        "title": (fm or {}).get("名称") or md_file.stem,
                        "path": str(md_file.relative_to(wiki_dir)),
                        "updated_at": updated,
                        "last_modified": datetime.fromtimestamp(
                            md_file.stat().st_mtime, timezone.utc
                        ).isoformat(),
                    }
                )
            except OSError:
                continue
        return pages

    @staticmethod
    def _signal_id(signal: AppSignal) -> str:
        raw = f"{signal.kind}:{signal.topic}".encode("utf-8")
        return hashlib.sha1(raw, usedforsecurity=False).hexdigest()

    @staticmethod
    def _within_cooldown(last_seen_at: str, cooldown_days: int, now: datetime) -> bool:
        try:
            last = datetime.fromisoformat(last_seen_at)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return False
        return now - last < timedelta(days=max(0, cooldown_days))

    @staticmethod
    def _reminder_content(signal: AppSignal) -> str:
        evidence = "\n".join(f"- {item}" for item in signal.evidence)
        return (
            f"应用层信号提醒：{signal.kind} / {signal.topic}\n\n"
            f"置信度：{signal.confidence:.2f}\n\n"
            f"建议动作：{signal.suggested_action}\n\n"
            f"证据：\n{evidence}"
        )
