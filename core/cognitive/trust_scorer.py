# -*- coding: utf-8 -*-
"""Shared trust decisions for consolidation, merge, guard, and delivery."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from core.config import get_config
from core.kia.policy import get_shadowed_value


SCHEMA_VERSION = "mnemos.trust_decisions.v1"
NEGATIVE_SIGNAL_TYPES = {
    "no_click",
    "ignore",
    "dismiss",
    "contradicted",
    "outdated",
    "harmful",
}


@dataclass(frozen=True)
class KnowledgeTrustOptions:
    """Runtime options for trust scoring."""

    database_dir: Path
    db_path: Path
    base_trust_score: float = 0.6
    evidence_ref_bonus: float = 0.08
    min_merge_score: float = 0.72
    min_delivery_score: float = 0.55
    min_delivery_task_fit: float = 0.45
    min_guard_score: float = 0.75
    min_guard_task_fit: float = 0.7
    ignore_penalty: float = 0.12
    dismiss_penalty: float = 0.22
    no_click_penalty: float = 0.08
    contradicted_penalty: float = 0.35
    harmful_penalty: float = 1.0
    harmful_cooldown_days: int = 30

    @classmethod
    def from_config(
        cls,
        cfg: Any | None = None,
        *,
        database_dir: Path | None = None,
    ) -> "KnowledgeTrustOptions":
        cfg = cfg or get_config()
        base_dir = Path(database_dir or getattr(cfg, "database_dir", "") or Path.home() / ".mnemos")
        configured_db = _cfg_get(cfg, "trust.db_path", None)
        db_path = Path(configured_db).expanduser() if configured_db else base_dir / "trust_decisions.db"
        return cls(
            database_dir=base_dir.expanduser(),
            db_path=db_path.expanduser(),
            base_trust_score=float(_cfg_get(cfg, "trust.base_trust_score", 0.6) or 0.6),
            evidence_ref_bonus=float(_cfg_get(cfg, "trust.evidence_ref_bonus", 0.08) or 0.08),
            min_merge_score=float(_cfg_get(cfg, "trust.min_merge_score", 0.72) or 0.72),
            min_delivery_score=float(
                get_shadowed_value(
                    "trust.min_delivery_score",
                    _cfg_get(cfg, "trust.min_delivery_score", 0.55) or 0.55,
                )
            ),
            min_delivery_task_fit=float(
                _cfg_get(cfg, "trust.min_delivery_task_fit", 0.45) or 0.45
            ),
            min_guard_score=float(_cfg_get(cfg, "trust.min_guard_score", 0.75) or 0.75),
            min_guard_task_fit=float(_cfg_get(cfg, "trust.min_guard_task_fit", 0.7) or 0.7),
            ignore_penalty=float(_cfg_get(cfg, "trust.ignore_penalty", 0.12) or 0.12),
            dismiss_penalty=float(_cfg_get(cfg, "trust.dismiss_penalty", 0.22) or 0.22),
            no_click_penalty=float(_cfg_get(cfg, "trust.no_click_penalty", 0.08) or 0.08),
            contradicted_penalty=float(
                _cfg_get(cfg, "trust.contradicted_penalty", 0.35) or 0.35
            ),
            harmful_penalty=float(_cfg_get(cfg, "trust.harmful_penalty", 1.0) or 1.0),
            harmful_cooldown_days=int(
                _cfg_get(cfg, "trust.harmful_cooldown_days", 30) or 30
            ),
        )


@dataclass(frozen=True)
class TrustDecision:
    """One auditable decision produced by KnowledgeTrustScorer."""

    decision_id: str
    source: str
    subject: str
    action: str
    decision: str
    reason: str
    trust_score: float
    task_fit_score: float
    interruption_cost: float
    outcome_score: float
    active_risk: bool = False
    evidence_refs: list[str] = field(default_factory=list)
    scope_type: str = "global"
    scope_value: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class KnowledgeTrustScorer:
    """Central trust scorer and ledger for knowledge actions."""

    def __init__(
        self,
        options: KnowledgeTrustOptions | None = None,
        *,
        config: Any | None = None,
        database_dir: Path | None = None,
        ensure_db: bool = True,
    ):
        self.options = options or KnowledgeTrustOptions.from_config(
            config,
            database_dir=database_dir,
        )
        if ensure_db:
            self._ensure_schema()

    def decide(
        self,
        *,
        source: str,
        subject: str,
        action: str,
        evidence_refs: list[str] | None = None,
        task_fit_score: float = 0.5,
        interruption_cost: float = 0.0,
        active_risk: bool = False,
        scope_type: str = "global",
        scope_value: str = "",
        metadata: Mapping[str, Any] | None = None,
        persist: bool = True,
    ) -> TrustDecision:
        evidence = _dedup([str(ref) for ref in (evidence_refs or []) if str(ref)])
        task_fit = _clamp(task_fit_score)
        interruption = _clamp(interruption_cost)
        negatives = self.negative_evidence_for(
            subject=subject,
            scope_type=scope_type,
            scope_value=scope_value,
        )
        trust_score = self._trust_score(evidence, negatives)
        adjusted_task_fit = self._task_fit_after_negatives(task_fit, negatives)
        outcome_score = self._outcome_score(negatives)
        decision, reason = self._route_decision(
            action=action,
            trust_score=trust_score,
            task_fit_score=adjusted_task_fit,
            interruption_cost=interruption,
            active_risk=active_risk,
            evidence_refs=evidence,
            negatives=negatives,
        )
        decision_id = _decision_id(source, subject, action)
        decision_obj = TrustDecision(
            decision_id=decision_id,
            source=str(source or ""),
            subject=_norm_subject(subject),
            action=str(action or ""),
            decision=decision,
            reason=reason,
            trust_score=trust_score,
            task_fit_score=adjusted_task_fit,
            interruption_cost=interruption,
            outcome_score=outcome_score,
            active_risk=bool(active_risk),
            evidence_refs=evidence,
            scope_type=_norm_scope_type(scope_type),
            scope_value=_norm_scope_value(scope_value),
            metadata=dict(metadata or {}),
        )
        if persist:
            self._log_decision(decision_obj)
        return decision_obj

    def record_negative_evidence(
        self,
        *,
        source: str,
        subject: str,
        signal_type: str,
        scope_type: str = "global",
        scope_value: str = "",
        severity: float = 1.0,
        source_event_id: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_signal = str(signal_type or "").strip().lower()
        if normalized_signal not in NEGATIVE_SIGNAL_TYPES:
            raise ValueError(f"invalid negative evidence type: {signal_type}")
        evidence_id = _negative_id(
            source,
            subject,
            normalized_signal,
            source_event_id=source_event_id,
        )
        cooldown_until = ""
        if normalized_signal == "harmful":
            cooldown_until = (
                datetime.now(timezone.utc) + timedelta(days=self.options.harmful_cooldown_days)
            ).isoformat(timespec="seconds")
        row = {
            "evidence_id": evidence_id,
            "created_at": _now(),
            "source": str(source or ""),
            "subject": _norm_subject(subject),
            "scope_type": _norm_scope_type(scope_type),
            "scope_value": _norm_scope_value(scope_value),
            "signal_type": normalized_signal,
            "severity": _clamp(severity),
            "cooldown_until": cooldown_until,
            "metadata": dict(metadata or {}),
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO negative_evidence (
                    evidence_id, created_at, source, subject, scope_type, scope_value,
                    signal_type, severity, cooldown_until, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["evidence_id"],
                    row["created_at"],
                    row["source"],
                    row["subject"],
                    row["scope_type"],
                    row["scope_value"],
                    row["signal_type"],
                    row["severity"],
                    row["cooldown_until"],
                    _json_dumps(row["metadata"]),
                ),
            )
            conn.commit()
            stored = conn.execute(
                "SELECT * FROM negative_evidence WHERE evidence_id=?",
                (evidence_id,),
            ).fetchone()
        return _row_to_dict(stored) if stored else row

    def negative_evidence_for(
        self,
        *,
        subject: str,
        scope_type: str = "global",
        scope_value: str = "",
    ) -> list[dict[str, Any]]:
        if not self.options.db_path.exists():
            return []
        now = _now()
        normalized_subject = _norm_subject(subject)
        normalized_scope = _norm_scope_type(scope_type)
        normalized_value = _norm_scope_value(scope_value)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM negative_evidence
                WHERE subject = ?
                  AND (
                    scope_type = 'global'
                    OR (scope_type = ? AND scope_value = ?)
                  )
                  AND (cooldown_until = '' OR cooldown_until >= ?)
                ORDER BY created_at DESC, evidence_id DESC
                """,
                (normalized_subject, normalized_scope, normalized_value, now),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def get_decision(self, decision_id: str) -> dict[str, Any] | None:
        if not self.options.db_path.exists():
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM trust_decisions WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
        return _row_to_dict(row) if row else None

    def _route_decision(
        self,
        *,
        action: str,
        trust_score: float,
        task_fit_score: float,
        interruption_cost: float,
        active_risk: bool,
        evidence_refs: list[str],
        negatives: list[dict[str, Any]],
    ) -> tuple[str, str]:
        normalized_action = str(action or "").strip().lower()
        if any(item.get("signal_type") == "harmful" for item in negatives):
            return "block", "harmful_negative_evidence"

        if normalized_action == "guard_block":
            if not evidence_refs:
                return "observe", "missing_evidence_refs"
            if not active_risk:
                return "observe", "missing_active_risk"
            if task_fit_score < self.options.min_guard_task_fit:
                return "observe", "low_task_fit"
            if trust_score < self.options.min_guard_score:
                return "observe", "low_trust_score"
            return "block", "guard_requirements_met"

        if normalized_action in {"merge", "merge_into_page", "update", "update_page"}:
            if not evidence_refs:
                return "shadow", "missing_evidence_refs"
            if trust_score < self.options.min_merge_score:
                return "shadow", "low_trust_score"
            return "apply", "merge_requirements_met"

        if normalized_action in {"extract", "create_page"}:
            if not evidence_refs:
                return "review", "missing_evidence_refs"
            if task_fit_score < self.options.min_delivery_task_fit:
                return "review", "low_task_fit"
            if trust_score < self.options.min_delivery_score:
                return "review", "low_trust_score"
            return "accept", "extraction_requirements_met"

        if normalized_action in {
            "delivery",
            "predictive_push",
            "preflight_inject",
            "guard_check",
            "check_pending_recaps",
            "dialog_reminder",
        }:
            if task_fit_score < self.options.min_delivery_task_fit:
                return "suppress", "low_task_fit"
            if trust_score < self.options.min_delivery_score:
                return "suppress", "low_trust_score"
            if interruption_cost > task_fit_score:
                return "suppress", "interruption_cost_too_high"
            return "deliver", "delivery_requirements_met"

        return "record", "record_only"

    def _trust_score(self, evidence_refs: list[str], negatives: list[dict[str, Any]]) -> float:
        score = self.options.base_trust_score
        score += min(3, len(evidence_refs)) * self.options.evidence_ref_bonus
        for item in negatives:
            signal = item.get("signal_type")
            severity = float(item.get("severity") or 1.0)
            if signal == "contradicted":
                score -= self.options.contradicted_penalty * severity
            elif signal == "harmful":
                score -= self.options.harmful_penalty * severity
        return _clamp(score)

    def _task_fit_after_negatives(
        self,
        task_fit: float,
        negatives: list[dict[str, Any]],
    ) -> float:
        adjusted = task_fit
        for item in negatives:
            signal = item.get("signal_type")
            severity = float(item.get("severity") or 1.0)
            if signal == "ignore":
                adjusted -= self.options.ignore_penalty * severity
            elif signal == "dismiss":
                adjusted -= self.options.dismiss_penalty * severity
            elif signal == "no_click":
                adjusted -= self.options.no_click_penalty * severity
        return _clamp(adjusted)

    def _outcome_score(self, negatives: list[dict[str, Any]]) -> float:
        penalty = 0.0
        for item in negatives:
            severity = float(item.get("severity") or 1.0)
            if item.get("signal_type") in {"ignore", "dismiss", "no_click"}:
                penalty += 0.1 * severity
            elif item.get("signal_type") == "contradicted":
                penalty += 0.35 * severity
            elif item.get("signal_type") == "harmful":
                penalty += 1.0 * severity
        return _clamp(1.0 - penalty)

    def _log_decision(self, decision: TrustDecision) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO trust_decisions (
                    decision_id, created_at, source, subject, scope_type, scope_value,
                    action, trust_score, task_fit_score, interruption_cost, outcome_score,
                    decision, reason, evidence_refs_json, active_risk, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.decision_id,
                    _now(),
                    decision.source,
                    decision.subject,
                    decision.scope_type,
                    decision.scope_value,
                    decision.action,
                    decision.trust_score,
                    decision.task_fit_score,
                    decision.interruption_cost,
                    decision.outcome_score,
                    decision.decision,
                    decision.reason,
                    _json_dumps(decision.evidence_refs),
                    int(decision.active_risk),
                    _json_dumps(decision.metadata),
                ),
            )
            conn.commit()

    def _ensure_schema(self) -> None:
        self.options.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trust_decisions (
                    decision_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    subject TEXT NOT NULL DEFAULT '',
                    scope_type TEXT NOT NULL DEFAULT 'global',
                    scope_value TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL DEFAULT '',
                    trust_score REAL NOT NULL DEFAULT 0,
                    task_fit_score REAL NOT NULL DEFAULT 0,
                    interruption_cost REAL NOT NULL DEFAULT 0,
                    outcome_score REAL NOT NULL DEFAULT 0,
                    decision TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
                    active_risk INTEGER NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS negative_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    subject TEXT NOT NULL DEFAULT '',
                    scope_type TEXT NOT NULL DEFAULT 'global',
                    scope_value TEXT NOT NULL DEFAULT '',
                    signal_type TEXT NOT NULL,
                    severity REAL NOT NULL DEFAULT 1,
                    cooldown_until TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_trust_decisions_subject
                ON trust_decisions(subject, created_at)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_negative_evidence_scope
                ON negative_evidence(subject, scope_type, scope_value, signal_type)
                """
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.options.db_path)
        conn.row_factory = sqlite3.Row
        return conn


def _cfg_get(cfg: Any, key: str, default: Any) -> Any:
    try:
        return cfg.get(key, default)
    except (AttributeError, TypeError):
        return default


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key in ("metadata_json", "evidence_refs_json"):
        if key not in data:
            continue
        out_key = key.removesuffix("_json")
        try:
            data[out_key] = json.loads(data.pop(key) or "null")
        except json.JSONDecodeError:
            data[out_key] = None
    return data


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clamp(value: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = 0.0
    return max(0.0, min(1.0, numeric))


def _norm_subject(value: str) -> str:
    return str(value or "").strip().lower()


def _norm_scope_type(value: str) -> str:
    return str(value or "global").strip().lower() or "global"


def _norm_scope_value(value: str) -> str:
    return str(value or "").strip().lower()


def _dedup(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _decision_id(source: str, subject: str, action: str) -> str:
    raw = f"{source}|{_norm_subject(subject)}|{action}|{_now()}|{uuid4().hex}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"trust-{digest}"


def _negative_id(
    source: str,
    subject: str,
    signal_type: str,
    *,
    source_event_id: str = "",
) -> str:
    identity = source_event_id or f"{_now()}|{uuid4().hex}"
    raw = f"{source}|{_norm_subject(subject)}|{signal_type}|{identity}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"neg-{digest}"
