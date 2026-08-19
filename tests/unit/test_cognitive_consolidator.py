# -*- coding: utf-8 -*-
from __future__ import annotations

import sqlite3
from pathlib import Path

from core.cognitive.consolidator import (
    CognitiveConsolidationOptions,
    CognitiveConsolidator,
)
from core.cognitive.trust_scorer import TrustDecision
from core.sync_framework.raw_event_store import RawEventStore
from core.trust.models import CandidateBundle, JournalEventInput, sha256_text
from core.trust.proposal_queue import ProposalQueue
from core.trust.write_journal import WriteJournal
from core.wiki_projection_lifecycle import DEFAULT_REQUIRED_CONSUMERS, WikiProjectionLedger


class _Cfg:
    def __init__(self, root: Path):
        self.database_dir = root / "db"
        self.wiki_dir = root / "wiki"
        self.raw_vault_dir = root / "raw"
        self.database_dir.mkdir()
        self.wiki_dir.mkdir()
        self.raw_vault_dir.mkdir()

    def get(self, key, default=None):
        values = {
            "raw_event_store.db_path": str(self.database_dir / "raw_events.db"),
            "cognitive_consolidation.raw_vault_dir": str(self.raw_vault_dir),
            "cognitive_consolidation.db_path": str(self.database_dir / "cognitive_consolidation.db"),
            "cognitive_consolidation.method_pages_dir": "04-Concepts/方法论",
            "cognitive_consolidation.candidate_limit": 10,
            "cognitive_consolidation.raw_purge_limit": 1,
            "cognitive_consolidation.min_key_details": 1,
            "cognitive_consolidation.max_key_details": 2,
        }
        return values.get(key, default)


class _VaultCfg:
    def __init__(self, root: Path):
        self.database_dir = root / "db"
        self.wiki_dir = root / "wiki"
        self.raw_dir = root / "configured-raw"
        self.database_dir.mkdir()
        self.wiki_dir.mkdir()
        self.raw_dir.mkdir()

    def get(self, key, default=None):
        values = {
            "cognitive_consolidation.db_path": str(self.database_dir / "cognitive_consolidation.db"),
            "cognitive_consolidation.method_pages_dir": "04-Concepts/方法论",
        }
        return values.get(key, default)

    def vault_dir(self, name: str) -> Path:
        if name != "raw":
            raise KeyError(name)
        return self.raw_dir


def _store_with_turns(cfg: _Cfg) -> RawEventStore:
    store = RawEventStore(config=cfg)
    for idx in range(3):
        store.upsert_turn(
            source_agent="codex",
            session_id="sess-consolidate",
            turn_number=idx,
            user_content=f"user {idx}",
            assistant_content=f"assistant {idx}",
            timestamp="2026-01-01T00:00:00",
            completeness={"status": "complete"},
        )
    revision_ids = [
        store.find_event_id(
            source_agent="codex",
            session_id="sess-consolidate",
            turn_number=idx,
        )
        for idx in range(3)
    ]
    event_ids = [
        store.get_turn(revision_id)["logical_event_id"]
        for revision_id in revision_ids
    ]
    conn = sqlite3.connect(store.db_path)
    conn.execute(
        "UPDATE raw_metrics SET retention_state='eligible_delete', survival_score=1 WHERE event_id = ?",
        (event_ids[0],),
    )
    conn.execute(
        "UPDATE raw_metrics SET retention_state='eligible_delete', survival_score=2 WHERE event_id = ?",
        (event_ids[1],),
    )
    conn.commit()
    conn.close()
    return store


def _valid_method_page(wiki_dir: Path, evidence_refs: list[str] | None = None) -> Path:
    path = wiki_dir / "06-Retrospectives" / "method.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\n"
        "名称: 处理重复踩坑的方法论\n"
        "证据引用:\n"
        + "".join(f"- {ref}\n" for ref in (evidence_refs or ["raw-1: 用户重复踩坑"]))
        +
        "key_details:\n"
        "- 保留第一个关键细节\n"
        "- 保留第二个关键细节\n"
        "---\n"
        "# 方法论\n\n"
        "遇到同类问题时先查 action log，再执行最小验证。\n\n"
        "## 不适用条件\n\n"
        "- 没有证据引用时不适用。\n",
        encoding="utf-8",
    )
    return path


class _EventBus:
    def publish(self, event):
        return f"trace-{event.trace_id}"


def _committed_trusted_proposal(cfg: _Cfg, path: Path, evidence_refs: list[str]) -> str:
    content = path.read_text(encoding="utf-8")
    candidate = CandidateBundle.from_payload(
        source="cognitive_consolidation",
        source_agent="mnemos",
        target_kind="markdown",
        target_path=str(path),
        payload={"content": content},
        evidence_refs=evidence_refs,
        proposed_actions=["update_markdown"],
    )
    db_path = cfg.database_dir / "trusted_push.db"
    queue = ProposalQueue(db_path, wiki_base=cfg.wiki_dir)
    proposal = queue.submit_candidate(candidate)
    queue.update_status(proposal.proposal_id, "committed")
    WriteJournal(db_path).append_event(
        JournalEventInput(
            proposal_id=proposal.proposal_id,
            event_type="commit",
            target_uri=str(path),
            content_hash=sha256_text(content),
        )
    )
    return proposal.proposal_id


def test_options_use_configured_raw_vault_when_no_cognitive_override(tmp_path):
    cfg = _VaultCfg(tmp_path)

    options = CognitiveConsolidationOptions.from_config(cfg)

    assert options.raw_vault_dir == cfg.raw_dir


def test_plan_blocks_raw_purge_without_valid_method_page(tmp_path):
    cfg = _Cfg(tmp_path)
    store = _store_with_turns(cfg)
    options = CognitiveConsolidationOptions.from_config(cfg)
    consolidator = CognitiveConsolidator(options=options, config=cfg, raw_store=store)

    report = consolidator.plan(apply=True, purge_raw=True, candidate_limit=10)

    assert report["raw"]["candidate_count"] == 2
    assert report["raw"]["purge_allowed"] is False
    assert report["raw"]["purge_result"]["blocked_reason"] == "raw_purge_requires_data_ownership_workflow"
    assert report["ok"] is False
    first_event_id = store.find_event_id(
        source_agent="codex",
        session_id="sess-consolidate",
        turn_number=0,
    )
    assert first_event_id is not None
    assert store.get_turn(first_event_id) is not None
    with sqlite3.connect(options.db_path) as conn:
        row = conn.execute("SELECT method_valid, purged_count FROM consolidation_runs").fetchone()
    assert row == (0, 0)
    store.close()


def test_record_run_initializes_db_for_dry_run(tmp_path):
    cfg = _Cfg(tmp_path)
    store = _store_with_turns(cfg)
    options = CognitiveConsolidationOptions.from_config(cfg)
    consolidator = CognitiveConsolidator(options=options, config=cfg, raw_store=store)

    report = consolidator.plan(apply=False, candidate_limit=10)

    assert not options.db_path.exists()
    consolidator.record_run(report)
    with sqlite3.connect(options.db_path) as conn:
        row = conn.execute(
            "SELECT applied, raw_candidate_count, purged_count FROM consolidation_runs"
        ).fetchone()
    assert row == (0, 2, 0)
    store.close()


def test_valid_method_page_never_purges_or_claims_coverage_without_receipts(tmp_path):
    cfg = _Cfg(tmp_path)
    store = _store_with_turns(cfg)
    method = _valid_method_page(cfg.wiki_dir)
    options = CognitiveConsolidationOptions.from_config(cfg)
    consolidator = CognitiveConsolidator(options=options, config=cfg, raw_store=store)

    report = consolidator.plan(
        apply=True,
        purge_raw=True,
        method_page=method,
        candidate_limit=10,
        raw_purge_limit=1,
    )

    assert report["method_page"]["valid"] is True
    assert report["raw"]["purge_allowed"] is False
    assert report["raw"]["purge_result"]["purged"] == 0
    assert report["raw"]["purge_result"]["blocked_reason"] == "raw_purge_requires_data_ownership_workflow"
    assert report["coverage"]["written"] == 0
    assert report["ok"] is False
    assert {item["state"] for item in report["coverage"]["candidate_dispositions"]} == {
        "awaiting_trusted_page_commit"
    }
    assert all(item["exact_source_ref"].startswith("raw-revision:") for item in report["coverage"]["candidate_dispositions"])
    with sqlite3.connect(options.db_path) as conn:
        coverage = conn.execute("SELECT COUNT(*) FROM consolidation_coverage").fetchone()[0]
        run = conn.execute("SELECT method_valid, purged_count FROM consolidation_runs").fetchone()
    assert coverage == 0
    assert run == (1, 0)
    assert report["method_page"]["trust_decision_id"]
    with sqlite3.connect(cfg.database_dir / "trust_decisions.db") as conn:
        trust_row = conn.execute(
            "SELECT source, action, decision FROM trust_decisions WHERE decision_id = ?",
            (report["method_page"]["trust_decision_id"],),
        ).fetchone()
    assert trust_row == ("cognitive_consolidator", "extract", "accept")
    active_event_id = store.find_event_id(
        source_agent="codex",
        session_id="sess-consolidate",
        turn_number=2,
    )
    assert active_event_id is not None
    assert store.get_turn(active_event_id) is not None
    first_revision_id = store.find_event_id(
        source_agent="codex",
        session_id="sess-consolidate",
        turn_number=0,
    )
    assert first_revision_id is not None
    assert store.get_turn(first_revision_id) is not None
    store.close()


def test_method_page_trust_gate_uses_configured_trust_db_path(tmp_path):
    class _TrustCfg(_Cfg):
        def get(self, key, default=None):
            if key == "trust.db_path":
                return str(self.database_dir / "custom-trust.db")
            return super().get(key, default)

    cfg = _TrustCfg(tmp_path)
    store = _store_with_turns(cfg)
    method = _valid_method_page(cfg.wiki_dir)
    consolidator = CognitiveConsolidator(
        options=CognitiveConsolidationOptions.from_config(cfg),
        config=cfg,
        raw_store=store,
    )

    report = consolidator.plan(apply=True, method_page=method)

    assert report["method_page"]["valid"] is True
    assert (cfg.database_dir / "custom-trust.db").exists()
    assert not (cfg.database_dir / "trust_decisions.db").exists()
    store.close()


def test_method_page_trust_gate_blocks_raw_purge(tmp_path):
    cfg = _Cfg(tmp_path)
    store = _store_with_turns(cfg)
    method = _valid_method_page(cfg.wiki_dir)

    class _ReviewScorer:
        def decide(self, **kwargs):
            return TrustDecision(
                decision_id="trust-review",
                source=kwargs["source"],
                subject=kwargs["subject"],
                action=kwargs["action"],
                decision="review",
                reason="low_trust_score",
                trust_score=0.2,
                task_fit_score=kwargs["task_fit_score"],
                interruption_cost=kwargs["interruption_cost"],
                outcome_score=1.0,
                evidence_refs=list(kwargs.get("evidence_refs") or []),
            )

    consolidator = CognitiveConsolidator(
        options=CognitiveConsolidationOptions.from_config(cfg),
        config=cfg,
        raw_store=store,
        trust_scorer=_ReviewScorer(),
    )

    report = consolidator.plan(apply=True, purge_raw=True, method_page=method)

    assert report["method_page"]["valid"] is False
    assert "trust_gate:low_trust_score" in report["method_page"]["reason"]
    assert report["raw"]["purge_allowed"] is False
    assert report["raw"]["purge_result"]["blocked_reason"] == "raw_purge_requires_data_ownership_workflow"
    store.close()


def test_method_page_requires_evidence_key_details_and_not_applicable(tmp_path):
    cfg = _Cfg(tmp_path)
    store = _store_with_turns(cfg)
    invalid = cfg.wiki_dir / "method.md"
    invalid.write_text("# 只有方法论，没有证据和边界\n", encoding="utf-8")
    consolidator = CognitiveConsolidator(
        options=CognitiveConsolidationOptions.from_config(cfg),
        config=cfg,
        raw_store=store,
    )

    report = consolidator.plan(apply=False, method_page=invalid)

    assert report["method_page"]["valid"] is False
    assert "missing_evidence_refs" in report["method_page"]["reason"]
    assert "key_details_must_be_1_to_2" in report["method_page"]["reason"]
    assert "missing_not_applicable_conditions" in report["method_page"]["reason"]
    store.close()


def test_dry_run_does_not_create_consolidation_database(tmp_path):
    cfg = _Cfg(tmp_path)
    store = _store_with_turns(cfg)
    options = CognitiveConsolidationOptions.from_config(cfg)
    consolidator = CognitiveConsolidator(options=options, config=cfg, raw_store=store)

    report = consolidator.plan(apply=False, candidate_limit=1)

    assert report["applied"] is False
    assert not options.db_path.exists()
    store.close()


def test_dry_run_method_page_trust_gate_does_not_write_ledgers(tmp_path):
    cfg = _Cfg(tmp_path)
    store = _store_with_turns(cfg)
    method = _valid_method_page(cfg.wiki_dir)
    options = CognitiveConsolidationOptions.from_config(cfg)
    consolidator = CognitiveConsolidator(options=options, config=cfg, raw_store=store)

    report = consolidator.plan(apply=False, purge_raw=True, method_page=method)

    assert report["method_page"]["valid"] is True
    assert report["method_page"]["trust_decision"]["decision"] == "accept"
    assert report["raw"]["purge_allowed"] is False
    assert not options.db_path.exists()
    assert not (cfg.database_dir / "trust_decisions.db").exists()
    store.close()


def test_generate_method_fails_closed_without_trusted_consolidation_worker(tmp_path):
    cfg = _Cfg(tmp_path)
    store = _store_with_turns(cfg)
    options = CognitiveConsolidationOptions.from_config(cfg)
    calls = []

    def abstraction_callback(payload):
        calls.append(payload)
        raise AssertionError("legacy abstraction callback must not receive partial Raw")

    consolidator = CognitiveConsolidator(
        options=options,
        config=cfg,
        raw_store=store,
        abstraction_callback=abstraction_callback,
    )

    report = consolidator.plan(
        apply=True,
        generate_method=True,
        method_output="04-Concepts/方法论/generated.md",
        candidate_limit=10,
    )

    assert report["abstraction"]["attempted"] is False
    assert report["abstraction"]["generated"] is False
    assert report["abstraction"]["written"] is False
    assert report["abstraction"]["reason"] == "trusted_consolidation_worker_required"
    assert not calls
    assert report["method_page"]["valid"] is False
    assert report["method_page"]["reason"] == "method_page_required"
    assert not (cfg.wiki_dir / "04-Concepts" / "方法论" / "generated.md").exists()
    assert report["coverage"]["written"] == 0
    store.close()


def test_coverage_requires_each_frozen_exact_source_and_all_projection_receipts(tmp_path):
    cfg = _Cfg(tmp_path)
    store = _store_with_turns(cfg)
    options = CognitiveConsolidationOptions.from_config(cfg)
    planner = CognitiveConsolidator(options=options, config=cfg, raw_store=store)
    preview = planner.plan(apply=False, candidate_limit=10)
    refs = [item["exact_source_ref"] for item in preview["raw"]["candidates"]]
    method = _valid_method_page(cfg.wiki_dir, refs)
    frozen = planner.plan(apply=True, method_page=method, candidate_limit=10)
    proposal_id = _committed_trusted_proposal(cfg, method, refs)

    first = planner.reconcile_coverage(
        frozen["run_id"], trusted_proposal_id=proposal_id, event_bus=_EventBus()
    )

    assert first["ok"] is False
    assert first["reason"] == "projection_receipts_required"
    assert first["checks"]["candidate_claim_not_represented"] == 0
    assert first["checks"]["coverage_without_exact_source_hash"] == 0
    assert first["checks"]["wiki_write_without_projection_receipts"] == len(
        DEFAULT_REQUIRED_CONSUMERS
    )
    assert first["coverage"]["written"] == 0
    ledger = WikiProjectionLedger(cfg.database_dir / "wiki_projection.db")
    for consumer in DEFAULT_REQUIRED_CONSUMERS:
        ledger.record_projection_receipt(
            mutation_id=first["mutation_id"], consumer=consumer, outcome="ack"
        )

    completed = planner.reconcile_coverage(
        frozen["run_id"], trusted_proposal_id=proposal_id, event_bus=_EventBus()
    )

    assert completed["ok"] is True
    assert completed["reason"] == "covered"
    assert completed["coverage"]["written"] == 2
    assert completed["checks"] == {
        "candidate_claim_not_represented": 0,
        "coverage_without_exact_source_hash": 0,
        "wiki_write_without_projection_receipts": 0,
        "purge_before_coverage_commit": 0,
    }
    with sqlite3.connect(options.db_path) as conn:
        rows = conn.execute(
            "SELECT exact_source_ref, source_content_hash, mutation_id "
            "FROM consolidation_coverage_receipts ORDER BY exact_source_ref"
        ).fetchall()
    assert [row[0] for row in rows] == sorted(refs)
    assert all(row[1] for row in rows)
    assert {row[2] for row in rows} == {completed["mutation_id"]}
    restarted = CognitiveConsolidator(options=options, config=cfg, raw_store=store)
    replay = restarted.reconcile_coverage(frozen["run_id"], event_bus=_EventBus())
    assert replay["ok"] is True
    assert replay["coverage"]["written"] == 0
    assert store.get_turn(preview["raw"]["candidates"][0]["revision_id"]) is not None
    restarted.close()
    store.close()


def test_one_arbitrary_evidence_ref_cannot_cover_two_frozen_candidates(tmp_path):
    cfg = _Cfg(tmp_path)
    store = _store_with_turns(cfg)
    options = CognitiveConsolidationOptions.from_config(cfg)
    planner = CognitiveConsolidator(options=options, config=cfg, raw_store=store)
    preview = planner.plan(apply=False, candidate_limit=10)
    first_ref = preview["raw"]["candidates"][0]["exact_source_ref"]
    method = _valid_method_page(cfg.wiki_dir, [first_ref])
    frozen = planner.plan(apply=True, method_page=method, candidate_limit=10)

    result = planner.reconcile_coverage(frozen["run_id"], event_bus=_EventBus())

    assert result["ok"] is False
    assert result["reason"] == "exact_source_representation_required"
    assert result["checks"]["candidate_claim_not_represented"] == 1
    assert result["coverage"]["written"] == 0
    store.close()


def test_rejected_or_uncommitted_trusted_page_never_records_coverage(tmp_path):
    cfg = _Cfg(tmp_path)
    store = _store_with_turns(cfg)
    options = CognitiveConsolidationOptions.from_config(cfg)
    planner = CognitiveConsolidator(options=options, config=cfg, raw_store=store)
    preview = planner.plan(apply=False, candidate_limit=10)
    refs = [item["exact_source_ref"] for item in preview["raw"]["candidates"]]
    method = _valid_method_page(cfg.wiki_dir, refs)
    frozen = planner.plan(apply=True, method_page=method, candidate_limit=10)
    candidate = CandidateBundle.from_payload(
        source="cognitive_consolidation",
        source_agent="mnemos",
        target_kind="markdown",
        target_path=str(method),
        payload={"content": method.read_text(encoding="utf-8")},
        evidence_refs=refs,
        proposed_actions=["update_markdown"],
    )
    proposal = ProposalQueue(cfg.database_dir / "trusted_push.db", wiki_base=cfg.wiki_dir).submit_candidate(candidate)

    rejected = planner.reconcile_coverage(
        frozen["run_id"], trusted_proposal_id=proposal.proposal_id, event_bus=_EventBus()
    )

    assert rejected["ok"] is False
    assert rejected["reason"] == "trusted_page_commit_required"
    assert rejected["coverage"]["written"] == 0
    assert store.get_turn(preview["raw"]["candidates"][0]["revision_id"]) is not None
    store.close()


def test_submit_frozen_page_creates_trusted_proposal_without_writing_raw(tmp_path):
    class _EnforcedCfg(_Cfg):
        def get(self, key, default=None):
            if key == "trusted_push.mode":
                return "enforce"
            return super().get(key, default)

    cfg = _EnforcedCfg(tmp_path)
    store = _store_with_turns(cfg)
    options = CognitiveConsolidationOptions.from_config(cfg)
    planner = CognitiveConsolidator(options=options, config=cfg, raw_store=store)
    preview = planner.plan(apply=False, candidate_limit=10)
    refs = [item["exact_source_ref"] for item in preview["raw"]["candidates"]]
    method = _valid_method_page(cfg.wiki_dir, refs)
    frozen = planner.plan(apply=True, method_page=method, candidate_limit=10)

    result = planner.submit_frozen_page(frozen["run_id"])

    assert result["ok"] is True, result
    assert result["status"] == "proposed"
    assert result["raw_purge_allowed"] is False
    proposal = ProposalQueue(cfg.database_dir / "trusted_push.db", wiki_base=cfg.wiki_dir).get(
        result["proposal_id"]
    )
    assert set(refs).issubset(set(proposal.candidate.evidence_refs))
    assert store.get_turn(preview["raw"]["candidates"][0]["revision_id"]) is not None
    store.close()


def test_raw_projection_consistency_reports_missing_and_eligible_projection(tmp_path):
    cfg = _Cfg(tmp_path)
    store = _store_with_turns(cfg)
    eligible_event_id = store.find_event_id(
        source_agent="codex",
        session_id="sess-consolidate",
        turn_number=0,
    )
    active_event_id = store.find_event_id(
        source_agent="codex",
        session_id="sess-consolidate",
        turn_number=2,
    )
    projection = cfg.raw_vault_dir / "codex.md"
    projection.write_text(
        "---\n"
        "event_ids:\n"
        f"- {eligible_event_id}\n"
        f"- {active_event_id}\n"
        "---\n"
        "# raw projection\n",
        encoding="utf-8",
    )
    consolidator = CognitiveConsolidator(
        options=CognitiveConsolidationOptions.from_config(cfg),
        config=cfg,
        raw_store=store,
    )

    report = consolidator.plan(apply=False, candidate_limit=10)

    consistency = report["raw"]["projection_consistency"]
    assert consistency["raw_events"] == 3
    assert consistency["projected_event_ids"] == 2
    assert consistency["missing_active"] == 0
    assert consistency["eligible_projected"] == 1
    store.close()
