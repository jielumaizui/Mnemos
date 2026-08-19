from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from core.benchmarks.golden import SCORECARD_FILENAME, run_golden_benchmark


def test_golden_benchmark_pipeline_writes_reproducible_scorecard(tmp_path: Path) -> None:
    scorecard = run_golden_benchmark(output_dir=tmp_path, strict=True, mock_llm=True)

    assert scorecard["ok"] is True
    assert scorecard["sample_count"] == 5
    assert scorecard["scores"]["total_score"] == 100
    assert scorecard["scores"]["cognitive_maturity_score"] == 100
    assert scorecard["scores"]["consumer_closure_score"] == 100
    assert scorecard["trend_comparison"]["regressions"] == []

    written = tmp_path / SCORECARD_FILENAME
    assert written.exists()
    persisted = json.loads(written.read_text(encoding="utf-8"))
    assert persisted["scores"] == scorecard["scores"]


def test_golden_benchmark_records_output_and_consumption_actions(tmp_path: Path) -> None:
    scorecard = run_golden_benchmark(output_dir=tmp_path, strict=True, mock_llm=True)

    wiki_pages = list((tmp_path / "wiki").rglob("*.md"))
    assert wiki_pages
    assert (tmp_path / "persona_delta.json").exists()

    action_ledger = tmp_path / str(scorecard["action_ledger"])
    with sqlite3.connect(str(action_ledger)) as conn:
        rows = conn.execute(
            "SELECT action_type, COUNT(*) FROM action_ledger GROUP BY action_type"
        ).fetchall()
        observation_rows = conn.execute(
            "SELECT verification_json FROM action_ledger "
            "WHERE action_type='golden_benchmark_observation'"
        ).fetchall()
    counts = {action_type: count for action_type, count in rows}
    observed_types = [
        json.loads(str(verification_json))["benchmark_stage"]
        for (verification_json,) in observation_rows
    ]
    assert counts["quality_gate"] == 5
    assert counts["benchmark_consumer_verify"] >= 5
    assert observed_types.count("distill_write") >= 3
    assert "persona_update" in observed_types


def test_golden_benchmark_rejects_real_provider_mode(tmp_path: Path) -> None:
    try:
        run_golden_benchmark(output_dir=tmp_path, mock_llm=False)
    except ValueError as exc:
        assert "--mock-llm" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("real provider mode should be rejected")
