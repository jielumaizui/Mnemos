from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile

import pytest

from scripts.audit_cognitive_search import (
    audit_wiki_acl,
    run_audit,
    runtime_channel_population_report,
    strict_failures,
)
from core.cognitive.search_exclusion_ledger import (
    initialize_search_exclusion_ledger,
    insert_search_exclusion,
    iter_search_exclusion_candidates,
)
from scripts.cognitive_search_benchmark import (
    DEFAULT_FIXTURE,
    build_environment,
    fixture_contract_hash,
    load_fixture,
)

ROOT = Path(__file__).resolve().parents[2]


def _write_page(path, frontmatter: str, body: str = "body") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n{body}\n", encoding="utf-8")


def test_cognitive_search_benchmark_meets_frozen_holdout_contract() -> None:
    report = run_audit()
    benchmark = report["benchmark"]

    assert report["schema_version"] == "mnemos.cognitive_search_gate.v2"
    assert report["mode"] == "hermetic"
    assert benchmark["query_count"] == 36
    assert benchmark["holdout_query_count"] == 28
    assert benchmark["negative_query_count"] == 7
    assert benchmark["critical_recall_at_5"] == 1.0
    assert benchmark["recall_at_10"] >= 0.95
    assert benchmark["mrr"] >= 0.90
    assert benchmark["unauthorized_hit_count"] == 0
    assert benchmark["field_trace_gap"] == 0
    assert benchmark["source_trace_gap"] == 0
    assert benchmark["current_revision_gap"] == 0
    assert benchmark["superseded_belief_hit_count"] == 0
    assert benchmark["query_order_invariant"] is True
    assert report["answer_leakage"]["answer_leakage_count"] == 0
    assert report["runtime_channel_population"]["minimum_population_ready"] is True
    assert strict_failures(report) == []
    assert report["ok"] is True


def test_runtime_population_report_refuses_empty_graph_and_episode_claims() -> None:
    population = runtime_channel_population_report(
        {"active_page_count": 12},
        {
            "state": {"current_cognition_episode_count": 0},
            "cognitive_graph": {
                "tables": {
                    "cognitive_relations": {"valid_acl_count": 0},
                    "canonical_nodes": {"valid_acl_count": 0},
                }
            },
            "evidence_graph": {
                "tables": {
                    "evidence_nodes": {"valid_acl_count": 0},
                    "evidence_edges": {"valid_acl_count": 0},
                }
            },
        },
    )

    assert population["minimum_population_ready"] is False
    assert population["missing_channels"] == [
        "cognitive_graph",
        "cognitive_state",
        "evidence_graph",
    ]


def test_benchmark_fixture_hash_rejects_silent_answer_drift(tmp_path) -> None:
    payload = json.loads(DEFAULT_FIXTURE.read_text(encoding="utf-8"))
    payload["cases"][0]["query"] = "tampered benchmark answer"
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="fixture hash mismatch"):
        load_fixture(tampered)


def test_benchmark_fixture_external_pin_rejects_resigned_answer_drift(tmp_path) -> None:
    payload = json.loads(DEFAULT_FIXTURE.read_text(encoding="utf-8"))
    payload["cases"][0]["query"] = "tampered and internally resigned benchmark answer"
    payload["fixture_contract_sha256"] = fixture_contract_hash(payload)
    tampered = tmp_path / "resigned-tampered.json"
    tampered.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="violates frozen external pin"):
        load_fixture(tampered)


def test_wiki_acl_audit_counts_fail_closed_quarantine_as_unresolved(tmp_path) -> None:
    _write_page(
        tmp_path / "active.md",
        """scope: agent
source_agent: codex
acl_schema_version: 1
acl_metadata_complete: true
acl_reconciliation_status: proven
""",
    )
    _write_page(
        tmp_path / "quarantined.md",
        """scope: restricted
acl_schema_version: 1
acl_metadata_complete: true
acl_reconciliation_status: restricted_unknown
""",
    )

    report = audit_wiki_acl(tmp_path)

    assert report["active_page_count"] == 1
    assert report["quarantined_restricted_unknown_count"] == 1
    assert report["acl_metadata_missing"] == 0
    assert report["acl_reconciliation_required"] == 1
    assert report["acl_unknown"] == 1


def test_wiki_acl_audit_reports_missing_metadata(tmp_path) -> None:
    _write_page(tmp_path / "missing.md", "title: Missing ACL\n")

    report = audit_wiki_acl(tmp_path)

    assert report["acl_metadata_missing"] == 1


def test_production_mode_refuses_fixture_quarantine_and_unknown_store_acl() -> None:
    fixture = load_fixture()
    with tempfile.TemporaryDirectory() as temp_dir:
        environment = build_environment(Path(temp_dir) / "live", fixture)
        report = run_audit(
            production=True,
            wiki_dir=environment.wiki_dir,
            state_db=environment.state_db,
            cognitive_graph_db=environment.cognitive_graph_db,
            evidence_graph_db=environment.evidence_graph_db,
        )

    assert report["mode"] == "production"
    assert report["ok"] is False
    assert "restricted_unknown_quarantine" in report["failures"]
    assert "evidence_graph_inventory" in report["failures"]


def test_production_mode_accepts_only_exact_fail_closed_exclusion_receipts() -> None:
    fixture = load_fixture()
    with tempfile.TemporaryDirectory() as temp_dir:
        environment = build_environment(Path(temp_dir) / "live", fixture)
        exclusion_db = Path(temp_dir) / "cognitive_search_exclusions.db"
        candidates = iter_search_exclusion_candidates(
            targets=("wiki", "cognitive_graph", "evidence_graph"),
            wiki_dir=environment.wiki_dir,
            cognitive_graph_db=environment.cognitive_graph_db,
            evidence_graph_db=environment.evidence_graph_db,
        )
        with sqlite3.connect(exclusion_db) as connection:
            initialize_search_exclusion_ledger(connection)
            for candidate in candidates:
                insert_search_exclusion(connection, candidate)
            connection.commit()
        report = run_audit(
            production=True,
            wiki_dir=environment.wiki_dir,
            state_db=environment.state_db,
            cognitive_graph_db=environment.cognitive_graph_db,
            evidence_graph_db=environment.evidence_graph_db,
            exclusion_db=exclusion_db,
        )

    assert report["wiki_acl"]["restricted_unknown_page_count"] > 0
    assert report["wiki_acl"]["historical_excluded_count"] > 0
    assert report["wiki_acl"]["acl_unknown"] == 0
    assert report["store_inventory"]["cognitive_graph"]["acl_unknown_count"] == 0
    assert report["store_inventory"]["evidence_graph"]["acl_unknown_count"] == 0
    assert report["ok"] is True


def test_cognitive_search_audit_is_in_every_required_gate_denominator() -> None:
    from scripts import run_full_score_gates, run_local_gates

    expected = [
        "python",
        "scripts/audit_cognitive_search.py",
        "--strict",
        "--json",
    ]
    local_commands = {name: command for name, command in run_local_gates.GATES}
    assert local_commands["cognitive search contract"] == expected

    full_commands = {
        gate.gate_id: list(gate.command) for gate in run_full_score_gates.contract_gates()
    }
    assert full_commands["contracts.cognitive_search"] == expected
    for path in (ROOT / ".pre-commit-config.yaml", ROOT / ".github/workflows/ci.yml"):
        assert "scripts/audit_cognitive_search.py --strict --json" in path.read_text(
            encoding="utf-8"
        )
