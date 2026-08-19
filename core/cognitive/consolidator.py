# -*- coding: utf-8 -*-
"""Cognitive consolidation planning for raw retention and distilled methods."""

from __future__ import annotations

import json
import sqlite3
from hashlib import sha256
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from core.config import get_config
from core.cognitive.trust_scorer import (
    KnowledgeTrustOptions,
    KnowledgeTrustScorer,
    TrustDecision,
)
from core.frontmatter import fm_get, parse_frontmatter
from core.sync_framework.native_raw_contract_ledger import NativeRawContractLedger
from core.sync_framework.raw_event_store import RawEventStore
from core.sync_framework.raw_subject_deletion import subject_deletion_visibility_predicate


SCHEMA_VERSION = "mnemos.cognitive_consolidation.v1"
AbstractionCallback = Callable[[Mapping[str, Any]], Mapping[str, Any] | str]


def _current_projection_consistency_query() -> str:
    """Return the fixed current-Raw query used by projection consistency checks."""
    return (
        """
        SELECT COALESCE(t.current_revision_id, t.event_id),
               COALESCE(m.retention_state, 'active')
        FROM raw_turns t
        LEFT JOIN raw_metrics m ON m.event_id = t.event_id
        WHERE 1=1
        """
        + NativeRawContractLedger.current_event_visibility_predicate("t.event_id")
        + subject_deletion_visibility_predicate("t.event_id")
    )


@dataclass(frozen=True)
class CognitiveConsolidationOptions:
    """Options for one consolidation planning/execution run."""

    database_dir: Path
    wiki_dir: Path
    raw_vault_dir: Path
    db_path: Path
    method_pages_dir: Path
    candidate_limit: int = 50
    raw_purge_limit: int = 10
    min_key_details: int = 1
    max_key_details: int = 2

    @classmethod
    def from_config(cls, cfg: Any) -> "CognitiveConsolidationOptions":
        database_dir = Path(getattr(cfg, "database_dir", "") or Path.home() / ".mnemos")
        wiki_dir = Path(getattr(cfg, "wiki_dir", "") or ".")
        raw_vault_dir = _configured_raw_vault_dir(cfg)
        configured_db = _cfg_get(cfg, "cognitive_consolidation.db_path", None)
        db_path = Path(configured_db).expanduser() if configured_db else database_dir / "cognitive_consolidation.db"
        method_pages_dir = Path(
            _cfg_get(cfg, "cognitive_consolidation.method_pages_dir", "04-Concepts/方法论")
        )
        return cls(
            database_dir=database_dir.expanduser(),
            wiki_dir=wiki_dir.expanduser(),
            raw_vault_dir=raw_vault_dir.expanduser(),
            db_path=db_path.expanduser(),
            method_pages_dir=method_pages_dir,
            candidate_limit=int(_cfg_get(cfg, "cognitive_consolidation.candidate_limit", 50) or 50),
            raw_purge_limit=int(_cfg_get(cfg, "cognitive_consolidation.raw_purge_limit", 10) or 10),
            min_key_details=int(_cfg_get(cfg, "cognitive_consolidation.min_key_details", 1) or 1),
            max_key_details=int(_cfg_get(cfg, "cognitive_consolidation.max_key_details", 2) or 2),
        )


class CognitiveConsolidator:
    """Plan cognitive compression without deleting details unless coverage exists."""

    def __init__(
        self,
        options: CognitiveConsolidationOptions | None = None,
        *,
        config: Any | None = None,
        raw_store: RawEventStore | None = None,
        abstraction_callback: AbstractionCallback | None = None,
        trust_scorer: KnowledgeTrustScorer | None = None,
    ):
        cfg = config or get_config()
        self._config = cfg
        self.options = options or CognitiveConsolidationOptions.from_config(cfg)
        self.raw_store = raw_store or RawEventStore(config=cfg)
        self._owns_raw_store = raw_store is None
        self._abstraction_callback = abstraction_callback
        self._trust_scorer = trust_scorer

    def close(self) -> None:
        if self._owns_raw_store:
            self.raw_store.close()

    def plan(
        self,
        *,
        apply: bool = False,
        purge_raw: bool = False,
        method_page: str | Path | None = None,
        generate_method: bool = False,
        method_output: str | Path | None = None,
        candidate_limit: int | None = None,
        raw_purge_limit: int | None = None,
        refresh_survival: bool = False,
    ) -> dict[str, Any]:
        """Build a safe plan; consolidation itself never deletes Raw or writes Wiki."""
        limit = max(0, int(candidate_limit if candidate_limit is not None else self.options.candidate_limit))
        purge_limit = max(
            0,
            int(raw_purge_limit if raw_purge_limit is not None else self.options.raw_purge_limit),
        )
        run_id = _run_id()
        survival = (
            self.raw_store.refresh_survival_scores(limit=limit or None)
            if refresh_survival
            else {"refreshed": False}
        )
        raw_candidates = self._raw_candidates(limit)
        projection = self._raw_projection_consistency()
        wiki_candidates = self._wiki_candidates(limit)
        kg_summary = self._kg_summary()
        abstraction = {
            "attempted": False,
            "generated": False,
            "reason": "",
        }
        method: dict[str, Any] | None = None
        if generate_method and method_page is None:
            abstraction = self._generate_method_page_candidate(
                raw_candidates,
                wiki_candidates,
                kg_summary,
                method_output=method_output,
                apply=apply,
            )
            generated_method = abstraction.get("method_page")
            if isinstance(generated_method, dict):
                if abstraction.get("written"):
                    method = self._validate_method_page(str(generated_method.get("path", "")))
                else:
                    method = dict(generated_method)
        if method is None:
            method = self._validate_method_page(method_page)
        method = self._gate_method_page(method, apply=apply)

        # COG-031: Raw deletion is a separately authorized DataOwnership workflow.
        # A consolidation plan may name retention candidates but must never purge them.
        purge_allowed = False
        purge_result = {
            "purged": 0,
            "raw_turns_deleted": 0,
            "raw_metrics_deleted": 0,
            "raw_access_logs_deleted": 0,
            "blocked_reason": "",
        }
        if purge_raw:
            purge_result["blocked_reason"] = "raw_purge_requires_data_ownership_workflow"

        # The old --apply path wrote a page directly and then asserted coverage
        # without a trusted projection receipt.  It is deliberately fail-closed
        # until a committed trusted-page handoff supplies exact source hashes.
        coverage_rows: list[dict[str, Any]] = []
        apply_blocked_reason = (
            "trusted_page_commit_and_projection_receipts_required" if apply else ""
        )
        disposition_state = (
            "awaiting_trusted_page_commit"
            if method["valid"]
            else "blocked_invalid_method_page"
        )
        candidate_dispositions = [
            {
                "source_event_id": str(item["event_id"]),
                "source_revision_id": str(item["revision_id"]),
                "source_content_hash": str(item["content_hash"]),
                "exact_source_ref": str(item["exact_source_ref"]),
                "state": disposition_state,
            }
            for item in raw_candidates
        ]

        report = {
            "schema_version": SCHEMA_VERSION,
            "ok": not bool(apply),
            "run_id": run_id,
            "applied": False,
            "requested_apply": bool(apply),
            "blocked_reason": apply_blocked_reason,
            "generated_at": _now(),
            "method_page": method,
            "abstraction": abstraction,
            "raw": {
                "candidate_count": len(raw_candidates),
                "candidates": raw_candidates,
                "purge_requested": bool(purge_raw),
                "purge_allowed": purge_allowed,
                "purge_result": purge_result,
                "survival_refresh": survival,
                "projection_consistency": projection,
            },
            "wiki": {
                "candidate_count": len(wiki_candidates),
                "candidates": wiki_candidates,
                "physical_delete_allowed": False,
            },
            "kg": {
                **kg_summary,
                "physical_delete_allowed": False,
            },
            "coverage": {
                "written": len(coverage_rows),
                "covered_by": method.get("relative_path", ""),
                "candidate_dispositions": candidate_dispositions,
            },
        }
        if apply:
            self._log_run(report)
        return report

    def record_run(self, report: Mapping[str, Any]) -> None:
        """Persist a dry-run or apply report as a consolidation run ledger entry."""
        self._log_run(report)

    def reconcile_coverage(
        self,
        run_id: str,
        *,
        trusted_proposal_id: str = "",
        event_bus: Any | None = None,
    ) -> dict[str, Any]:
        """Close one frozen plan only after a trusted page and all projections.

        This method deliberately has no Raw mutation.  A plan can become covered
        only when every frozen ``raw-revision:<revision>:<hash>`` is present in
        the committed page and the exact lifecycle mutation has terminal
        receipts from every required Wiki consumer.
        """
        self._ensure_schema()
        if trusted_proposal_id:
            self._bind_trusted_proposal(run_id, trusted_proposal_id)
        else:
            trusted_proposal_id = self._bound_trusted_proposal_id(run_id)
        report = self._frozen_report(run_id)
        if report is None:
            return {"ok": False, "run_id": run_id, "reason": "frozen_plan_not_found"}
        candidates = list(report.get("coverage", {}).get("candidate_dispositions", []))
        method = dict(report.get("method_page") or {})
        checks = {
            "candidate_claim_not_represented": 0,
            "coverage_without_exact_source_hash": 0,
            "wiki_write_without_projection_receipts": 0,
            "purge_before_coverage_commit": 0,
        }
        if bool(report.get("raw", {}).get("purge_allowed")) or int(
            report.get("raw", {}).get("purge_result", {}).get("purged", 0)
        ):
            checks["purge_before_coverage_commit"] = 1
        path = Path(str(method.get("path") or ""))
        if not path.is_file():
            return self._reconcile_result(run_id, checks, candidates, reason="method_page_missing")
        text = path.read_text(encoding="utf-8", errors="strict")
        actual_hash = sha256(text.encode("utf-8")).hexdigest()
        if actual_hash != str(method.get("content_sha256") or ""):
            return self._reconcile_result(run_id, checks, candidates, reason="method_page_hash_drift")
        validated = self._validate_method_text(text, path)
        exact_refs = {str(ref) for ref in validated.get("evidence_refs", [])}
        represented = {
            str(item.get("exact_source_ref") or "")
            for item in candidates
            if str(item.get("exact_source_ref") or "") in exact_refs
        }
        checks["candidate_claim_not_represented"] = len(candidates) - len(represented)
        checks["coverage_without_exact_source_hash"] = sum(
            not self._candidate_is_still_exact(item) for item in candidates
        )
        if checks["candidate_claim_not_represented"] or checks["coverage_without_exact_source_hash"]:
            return self._reconcile_result(
                run_id, checks, candidates, reason="exact_source_representation_required"
            )
        if not self._trusted_page_commit_matches(
            proposal_id=str(trusted_proposal_id or method.get("trusted_proposal_id") or ""),
            path=path,
            content_hash=actual_hash,
            evidence_refs=exact_refs,
        ):
            return self._reconcile_result(
                run_id, checks, candidates, reason="trusted_page_commit_required"
            )

        from core.wiki_projection_lifecycle import (
            DEFAULT_REQUIRED_CONSUMERS,
            WikiProjectionLedger,
            resolve_wiki_projection_db_path,
        )
        from core.wiki_projection_publisher import publish_wiki_mutation

        ledger = WikiProjectionLedger(resolve_wiki_projection_db_path(self._config))
        receipt = ledger.record_mutation(path, mutation_type="create")
        published = publish_wiki_mutation(
            receipt, ledger=ledger, source="cognitive_consolidation", event_bus=event_bus
        )
        gaps = ledger.required_consumer_gaps(receipt.mutation_id, DEFAULT_REQUIRED_CONSUMERS)
        checks["wiki_write_without_projection_receipts"] = len(gaps)
        if gaps or checks["purge_before_coverage_commit"]:
            return self._reconcile_result(
                run_id,
                checks,
                candidates,
                reason="projection_receipts_required",
                mutation_id=receipt.mutation_id,
                event_trace_id=str(published.get("event_trace_id") or ""),
                projection_gaps=gaps,
            )
        rows = self._write_verified_coverage(
            run_id=run_id,
            candidates=candidates,
            method_path=str(method.get("relative_path") or path),
            method_hash=actual_hash,
            mutation_id=receipt.mutation_id,
        )
        return self._reconcile_result(
            run_id,
            checks,
            candidates,
            reason="covered",
            mutation_id=receipt.mutation_id,
            event_trace_id=str(published.get("event_trace_id") or ""),
            coverage_rows=rows,
        )

    def reconcile_bound_runs(self, *, event_bus: Any | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Retry only operator-bound trusted plans; this never creates a plan."""
        if not self.options.db_path.is_file():
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT run_id FROM consolidation_trusted_pages ORDER BY bound_at LIMIT ?",
                (max(0, int(limit)),),
            ).fetchall()
        return [self.reconcile_coverage(str(row[0]), event_bus=event_bus) for row in rows]

    def submit_frozen_page(self, run_id: str) -> dict[str, Any]:
        """Submit, but never write, a frozen method page through trusted push."""
        self._ensure_schema()
        report = self._frozen_report(run_id)
        if report is None:
            return {"ok": False, "run_id": run_id, "reason": "frozen_plan_not_found"}
        method = dict(report.get("method_page") or {})
        path = Path(str(method.get("path") or ""))
        if not path.is_file():
            return {"ok": False, "run_id": run_id, "reason": "method_page_missing"}
        content = path.read_text(encoding="utf-8", errors="strict")
        content_hash = sha256(content.encode("utf-8")).hexdigest()
        if content_hash != str(method.get("content_sha256") or ""):
            return {"ok": False, "run_id": run_id, "reason": "method_page_hash_drift"}
        candidates = list(report.get("coverage", {}).get("candidate_dispositions", []))
        refs = [str(item.get("exact_source_ref") or "") for item in candidates]
        validated = self._validate_method_text(content, path)
        if not validated.get("valid") or not set(refs).issubset(
            {str(ref) for ref in validated.get("evidence_refs", [])}
        ):
            return {"ok": False, "run_id": run_id, "reason": "exact_source_representation_required"}
        from core.application.trusted_write_bridge import submit_application_wiki_write

        result = submit_application_wiki_write(
            wiki_dir=self.options.wiki_dir,
            target=path,
            content=content,
            page_path=str(method.get("relative_path") or path.relative_to(self.options.wiki_dir)),
            frontmatter_keys=(parse_frontmatter(content)[0] or {}).keys(),
            operation_created_at=_now(),
            session_id=str(run_id),
            project="mnemos",
            evidence_refs=refs,
            config=self._config,
        )
        result_dict = result.to_dict()
        if result.intercepted and result.proposal_id:
            self._bind_trusted_proposal(run_id, result.proposal_id)
            return {
                "ok": True,
                "run_id": run_id,
                "status": "proposed",
                "proposal_id": result.proposal_id,
                "trusted_push": result_dict,
                "raw_purge_allowed": False,
            }
        return {
            "ok": False,
            "run_id": run_id,
            "reason": "trusted_push_enforce_required",
            "trusted_push": result_dict,
            "raw_purge_allowed": False,
        }

    def _raw_candidates(self, limit: int) -> list[dict[str, Any]]:
        db_path = Path(self.raw_store.db_path)
        if not db_path.exists():
            return []
        query = """
            SELECT COALESCE(t.current_revision_id, t.event_id), t.event_id,
                   t.content_hash, t.full_content_hash,
                   t.source_agent, t.session_id, t.turn_number,
                   m.survival_score, m.retention_state, m.updated_at
            FROM raw_metrics m
            JOIN raw_turns t ON t.event_id = m.event_id
            WHERE m.retention_state = 'eligible_delete'
        """
        query += NativeRawContractLedger.current_event_visibility_predicate("m.event_id")
        query += " ORDER BY m.updated_at ASC, m.survival_score ASC"
        params: list[Any] = []
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "revision_id": row[0],
                "event_id": row[1],
                "content_hash": row[2],
                "full_content_hash": row[3],
                "source_agent": row[4],
                "session_id": row[5],
                "turn_number": row[6],
                "survival_score": row[7],
                "retention_state": row[8],
                "updated_at": row[9],
                "exact_source_ref": _exact_source_ref(row[0], row[2]),
            }
            for row in rows
        ]

    def _raw_projection_consistency(self) -> dict[str, Any]:
        db_path = Path(self.raw_store.db_path)
        if not db_path.exists():
            return {"raw_events": 0, "projected_event_ids": 0, "missing_active": 0, "eligible_projected": 0}
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(_current_projection_consistency_query()).fetchall()
        active = {str(row[0]) for row in rows if row[1] != "eligible_delete"}
        eligible = {str(row[0]) for row in rows if row[1] == "eligible_delete"}
        projected = self._projected_raw_event_ids()
        return {
            "raw_events": len(rows),
            "projected_event_ids": len(projected),
            "missing_active": len(active - projected) if projected else len(active),
            "eligible_projected": len(eligible & projected),
        }

    def _projected_raw_event_ids(self) -> set[str]:
        root = self.options.raw_vault_dir
        if not root.exists():
            return set()
        event_ids: set[str] = set()
        for md_file in root.rglob("*.md"):
            try:
                fm, body = parse_frontmatter(md_file.read_text(encoding="utf-8", errors="ignore"))
            except (OSError, UnicodeError, ValueError):
                continue
            for item in _as_list(fm_get(fm, "event_ids", [])):
                if item:
                    event_ids.add(str(item))
            for line in body.splitlines():
                marker = "- event_id: `"
                if line.startswith(marker) and line.endswith("`"):
                    event_ids.add(line[len(marker) : -1])
        return event_ids

    def _wiki_candidates(self, limit: int) -> list[dict[str, Any]]:
        root = self.options.wiki_dir
        if not root.exists():
            return []
        candidates: list[dict[str, Any]] = []
        for md_file in sorted(root.rglob("*.md")):
            rel = md_file.relative_to(root)
            if any(part.startswith(".") or part == "99-Archive" for part in rel.parts):
                continue
            try:
                text = md_file.read_text(encoding="utf-8", errors="ignore")
            except (OSError, UnicodeError):
                continue
            fm, body = parse_frontmatter(text)
            evidence_refs = _as_list(fm_get(fm, "evidence_refs", []))
            if len(body) < 4000 and evidence_refs:
                continue
            candidates.append(
                {
                    "path": str(rel),
                    "reason": "large_or_missing_evidence_refs",
                    "evidence_ref_count": len(evidence_refs),
                    "bytes": len(text.encode("utf-8")),
                    "physical_delete_allowed": False,
                }
            )
            if limit and len(candidates) >= limit:
                break
        return candidates

    def _kg_summary(self) -> dict[str, Any]:
        db_path = self.options.database_dir / "knowledge_graph.db"
        if not db_path.exists():
            return {"exists": False, "relation_candidates": 0}
        with sqlite3.connect(db_path) as conn:
            try:
                rows = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM relations
                    WHERE COALESCE(status, '') IN ('deprecated', 'stale')
                    """
                ).fetchone()
            except sqlite3.Error:
                return {"exists": True, "relation_candidates": 0}
        return {"exists": True, "relation_candidates": int(rows[0] if rows else 0)}

    def _validate_method_page(self, method_page: str | Path | None) -> dict[str, Any]:
        if not method_page:
            return {"provided": False, "valid": False, "reason": "method_page_required"}
        path = Path(method_page).expanduser()
        if not path.is_absolute():
            path = self.options.wiki_dir / path
        if not path.exists():
            return {"provided": True, "valid": False, "path": str(path), "reason": "missing"}
        text = path.read_text(encoding="utf-8", errors="ignore")
        return self._validate_method_text(text, path)

    def _validate_method_text(self, text: str, path: Path | None = None) -> dict[str, Any]:
        fm, body = parse_frontmatter(text)
        evidence_refs = _as_list(fm_get(fm, "evidence_refs", []))
        key_details = _extract_key_details(fm, body)
        has_not_applicable = bool(
            _as_list(fm_get(fm, "not_applies_to", []))
            or "不适用" in body
            or "not applicable" in body.lower()
        )
        reasons = []
        if not evidence_refs:
            reasons.append("missing_evidence_refs")
        if not self.options.min_key_details <= len(key_details) <= self.options.max_key_details:
            reasons.append(
                f"key_details_must_be_{self.options.min_key_details}_to_"
                f"{self.options.max_key_details}"
            )
        if not has_not_applicable:
            reasons.append("missing_not_applicable_conditions")
        path_text = str(path) if path else ""
        if path and _is_relative_to(path, self.options.wiki_dir):
            rel = str(path.relative_to(self.options.wiki_dir))
        else:
            rel = path_text
        return {
            "provided": True,
            "valid": not reasons,
            "path": path_text,
            "relative_path": rel,
            "evidence_refs": evidence_refs,
            "evidence_ref_count": len(evidence_refs),
            "key_details": key_details,
            "has_not_applicable": has_not_applicable,
            "content_sha256": sha256(text.encode("utf-8")).hexdigest(),
            "trusted_proposal_id": str(fm_get(fm, "trusted_proposal_id", "") or ""),
            "reason": ",".join(reasons) if reasons else "ok",
        }

    def _gate_method_page(self, method: Mapping[str, Any], *, apply: bool) -> dict[str, Any]:
        gated = dict(method)
        if not gated.get("valid"):
            return gated
        decision = self._score_method_page(gated, persist=apply)
        gated["trust_decision"] = decision.to_dict()
        gated["trust_decision_id"] = decision.decision_id
        if decision.decision != "accept":
            gated["valid"] = False
            reason = str(gated.get("reason") or "ok")
            gated["reason"] = f"{reason},trust_gate:{decision.reason}"
        return gated

    def _score_method_page(self, method: Mapping[str, Any], *, persist: bool) -> TrustDecision:
        scorer = self._trust_scorer
        if scorer is None:
            scorer = KnowledgeTrustScorer(
                options=KnowledgeTrustOptions.from_config(
                    self._config,
                    database_dir=self.options.database_dir,
                ),
                ensure_db=persist,
            )
            if persist:
                self._trust_scorer = scorer
        key_details = _as_list(method.get("key_details"))
        task_fit_score = 0.9 if key_details and method.get("has_not_applicable") else 0.5
        return scorer.decide(
            source="cognitive_consolidator",
            subject=str(method.get("relative_path") or method.get("path") or "method_page"),
            action="extract",
            evidence_refs=_as_list(method.get("evidence_refs")),
            task_fit_score=task_fit_score,
            interruption_cost=0.0,
            active_risk=False,
            scope_type="method_page",
            scope_value=str(method.get("relative_path") or method.get("path") or ""),
            metadata={
                "schema_version": SCHEMA_VERSION,
                "key_detail_count": len(key_details),
                "has_not_applicable": bool(method.get("has_not_applicable")),
            },
            persist=persist,
        )

    def _generate_method_page_candidate(
        self,
        raw_candidates: list[dict[str, Any]],
        wiki_candidates: list[dict[str, Any]],
        kg_summary: Mapping[str, Any],
        *,
        method_output: str | Path | None,
        apply: bool,
    ) -> dict[str, Any]:
        # This legacy callback receives only planning metadata, not the complete
        # role-local Raw spans and ACL catalog required for a trusted abstraction.
        # Do not let it manufacture a page from a partial view.
        return {
            "attempted": False,
            "generated": False,
            "written": False,
            "reason": "trusted_consolidation_worker_required",
        }

    def _log_run(self, report: Mapping[str, Any]) -> None:
        self._ensure_schema()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO consolidation_runs (
                    run_id, created_at, applied, purge_requested, purge_allowed,
                    method_page, method_valid, raw_candidate_count, purged_count,
                    report_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report["run_id"],
                    report["generated_at"],
                    int(bool(report["applied"])),
                    int(bool(report["raw"]["purge_requested"])),
                    int(bool(report["raw"]["purge_allowed"])),
                    str(report["method_page"].get("relative_path", "")),
                    int(bool(report["method_page"].get("valid"))),
                    int(report["raw"]["candidate_count"]),
                    int(report["raw"]["purge_result"].get("purged", 0)),
                    json.dumps(report, ensure_ascii=False, sort_keys=True, default=str),
                ),
            )
            conn.commit()

    def _frozen_report(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT report_json FROM consolidation_runs WHERE run_id=?", (str(run_id),)
            ).fetchone()
        if row is None:
            return None
        try:
            report = json.loads(str(row[0]))
        except (TypeError, ValueError):
            return None
        return report if isinstance(report, dict) else None

    def _bind_trusted_proposal(self, run_id: str, proposal_id: str) -> None:
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT trusted_proposal_id FROM consolidation_trusted_pages WHERE run_id=?",
                (str(run_id),),
            ).fetchone()
            if existing is not None and str(existing[0]) != str(proposal_id):
                raise ValueError("frozen consolidation run already has a different trusted proposal")
            conn.execute(
                """
                INSERT INTO consolidation_trusted_pages (run_id, trusted_proposal_id, bound_at)
                VALUES (?, ?, ?)
                ON CONFLICT(run_id) DO NOTHING
                """,
                (str(run_id), str(proposal_id), _now()),
            )
            conn.commit()

    def _bound_trusted_proposal_id(self, run_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT trusted_proposal_id FROM consolidation_trusted_pages WHERE run_id=?",
                (str(run_id),),
            ).fetchone()
        return str(row[0]) if row is not None else ""

    def _candidate_is_still_exact(self, candidate: Mapping[str, Any]) -> bool:
        event_id = str(candidate.get("source_event_id") or "")
        revision_id = str(candidate.get("source_revision_id") or "")
        content_hash = str(candidate.get("source_content_hash") or "")
        if not event_id or not revision_id or not content_hash:
            return False
        db_path = Path(self.raw_store.db_path)
        if not db_path.is_file():
            return False
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                """
                SELECT COALESCE(current_revision_id, event_id), content_hash
                FROM raw_turns WHERE event_id=?
                """,
                (event_id,),
            ).fetchone()
        return bool(row and str(row[0]) == revision_id and str(row[1]) == content_hash)

    def _trusted_page_commit_matches(
        self,
        *,
        proposal_id: str,
        path: Path,
        content_hash: str,
        evidence_refs: set[str],
    ) -> bool:
        """Prove that the page bytes came from one committed trusted proposal."""
        if not proposal_id:
            return False
        try:
            from core.trust.config import load_trusted_push_config
            from core.trust.proposal_queue import ProposalQueue
            from core.trust.write_journal import WriteJournal

            trusted = load_trusted_push_config(self._config, wiki_base=self.options.wiki_dir)
            proposal = ProposalQueue(
                trusted.db_path, wiki_base=self.options.wiki_dir, config=trusted
            ).get(proposal_id)
            if proposal.status != "committed":
                return False
            target = Path(str(proposal.candidate.target_path or "")).expanduser()
            if target.resolve(strict=False) != path.resolve(strict=False):
                return False
            payload = proposal.candidate.payload
            if sha256(str(payload.get("content") or "").encode("utf-8")).hexdigest() != content_hash:
                return False
            if not evidence_refs.issubset({str(ref) for ref in proposal.candidate.evidence_refs}):
                return False
            events = WriteJournal(trusted.db_path, config=trusted).events_for_proposal(proposal_id)
            return any(
                event.get("event_type") == "commit"
                and str(event.get("target_uri") or "") == str(path)
                and str(event.get("content_hash") or "") == content_hash
                for event in events
            )
        except (OSError, ValueError, KeyError, LookupError, sqlite3.Error):
            return False

    def _write_verified_coverage(
        self,
        *,
        run_id: str,
        candidates: list[Mapping[str, Any]],
        method_path: str,
        method_hash: str,
        mutation_id: str,
    ) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        with self._connect() as conn:
            for candidate in candidates:
                row = {
                    "run_id": str(run_id),
                    "source_event_id": str(candidate["source_event_id"]),
                    "source_revision_id": str(candidate["source_revision_id"]),
                    "source_content_hash": str(candidate["source_content_hash"]),
                    "exact_source_ref": str(candidate["exact_source_ref"]),
                    "covered_by": method_path,
                    "method_content_hash": method_hash,
                    "mutation_id": mutation_id,
                }
                result = conn.execute(
                    """
                    INSERT INTO consolidation_coverage_receipts (
                        run_id, source_event_id, source_revision_id, source_content_hash,
                        exact_source_ref, covered_by, method_content_hash, mutation_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, exact_source_ref) DO NOTHING
                    """,
                    (*row.values(), _now()),
                )
                if result.rowcount:
                    rows.append(row)
            conn.execute(
                "UPDATE consolidation_runs SET applied=1 WHERE run_id=?",
                (str(run_id),),
            )
            conn.commit()
        return rows

    @staticmethod
    def _reconcile_result(
        run_id: str,
        checks: Mapping[str, int],
        candidates: list[Mapping[str, Any]],
        *,
        reason: str,
        mutation_id: str = "",
        event_trace_id: str = "",
        projection_gaps: list[str] | None = None,
        coverage_rows: list[Mapping[str, str]] | None = None,
    ) -> dict[str, Any]:
        covered = reason == "covered"
        dispositions = [
            {
                **dict(item),
                "state": "covered" if covered else "awaiting_projection_receipts",
            }
            for item in candidates
        ]
        return {
            "ok": covered,
            "run_id": run_id,
            "reason": reason,
            "mutation_id": mutation_id,
            "event_trace_id": event_trace_id,
            "projection_gaps": list(projection_gaps or []),
            "coverage": {"written": len(coverage_rows or []), "candidate_dispositions": dispositions},
            "checks": dict(checks),
            "raw_purge_allowed": False,
        }

    def _ensure_schema(self) -> None:
        self.options.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS consolidation_runs (
                    run_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    applied INTEGER NOT NULL DEFAULT 0,
                    purge_requested INTEGER NOT NULL DEFAULT 0,
                    purge_allowed INTEGER NOT NULL DEFAULT 0,
                    method_page TEXT NOT NULL DEFAULT '',
                    method_valid INTEGER NOT NULL DEFAULT 0,
                    raw_candidate_count INTEGER NOT NULL DEFAULT 0,
                    purged_count INTEGER NOT NULL DEFAULT 0,
                    report_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS consolidation_coverage (
                    run_id TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    covered_by TEXT NOT NULL,
                    coverage_type TEXT NOT NULL,
                    detail_weight TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, source_event_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS consolidation_coverage_receipts (
                    run_id TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    source_revision_id TEXT NOT NULL,
                    source_content_hash TEXT NOT NULL,
                    exact_source_ref TEXT NOT NULL,
                    covered_by TEXT NOT NULL,
                    method_content_hash TEXT NOT NULL,
                    mutation_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, exact_source_ref)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS consolidation_trusted_pages (
                    run_id TEXT PRIMARY KEY,
                    trusted_proposal_id TEXT NOT NULL,
                    bound_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.options.db_path)


def format_consolidation_plan(report: Mapping[str, Any]) -> str:
    """Return a compact human-readable consolidation plan."""
    raw = report["raw"]
    method = report["method_page"]
    lines = [
        "Mnemos cognitive consolidation",
        f"schema: {report['schema_version']}",
        f"mode: {'apply' if report['applied'] else 'dry-run'}",
        "",
        "Method page:",
        f"- valid: {method.get('valid')}",
        f"- reason: {method.get('reason')}",
        "",
        "Raw:",
        f"- candidates: {raw['candidate_count']}",
        f"- purge_requested: {raw['purge_requested']}",
        f"- purge_allowed: {raw['purge_allowed']}",
        f"- purged: {raw['purge_result'].get('purged', 0)}",
        "",
        "Wiki/KG:",
        f"- wiki_candidates: {report['wiki']['candidate_count']} (physical delete disabled)",
        f"- kg_relation_candidates: {report['kg'].get('relation_candidates', 0)} (physical delete disabled)",
    ]
    return "\n".join(lines)


def dumps_report(report: Mapping[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str)


def _extract_key_details(fm: Mapping[str, Any] | None, body: str) -> list[str]:
    for key in ("key_details", "关键细节"):
        values = _as_list((fm or {}).get(key))
        if values:
            return [str(item) for item in values if str(item).strip()]
    details: list[str] = []
    capture = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("##") and ("关键细节" in stripped or "Key Details" in stripped):
            capture = True
            continue
        if capture and stripped.startswith("##"):
            break
        if capture and stripped.startswith("- "):
            details.append(stripped[2:].strip())
    return details


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _exact_source_ref(revision_id: Any, content_hash: Any) -> str:
    """Canonical evidence token required to represent one Raw candidate."""

    revision = str(revision_id or "").strip()
    digest = str(content_hash or "").strip()
    return f"raw-revision:{revision}:{digest}" if revision and digest else ""


def _cfg_get(cfg: Any, key: str, default: Any) -> Any:
    try:
        return cfg.get(key, default)
    except (AttributeError, TypeError):
        return default


def _configured_raw_vault_dir(cfg: Any) -> Path:
    explicit = _cfg_get(cfg, "cognitive_consolidation.raw_vault_dir", None)
    if explicit:
        return Path(explicit)

    direct = getattr(cfg, "raw_vault_dir", None)
    if direct:
        return Path(direct)

    try:
        return Path(cfg.vault_dir("raw"))
    except (AttributeError, KeyError, TypeError):
        pass

    obsidian_path = getattr(cfg, "obsidian_vault_path", None)
    if obsidian_path:
        return Path(obsidian_path)

    return Path(get_config().vault_dir("raw"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run_id() -> str:
    return "cc_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
