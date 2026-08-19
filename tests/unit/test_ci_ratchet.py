"""Tests for scripts/ci_ratchet.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

# ci_ratchet.py lives in scripts/; add it to the import path for these tests.
_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import ci_ratchet  # noqa: E402


def test_check_passes_against_committed_baseline():
    """The current repo state must match the committed baseline."""
    baseline = ci_ratchet.load_baseline()
    current = ci_ratchet.compute_current_state()
    ok, regressions = ci_ratchet.check(baseline, current)
    assert ok, "\n".join(regressions)


def test_check_fails_on_new_cycle(tmp_path):
    """A cycle not in the baseline is reported as regression."""
    baseline = ci_ratchet.load_baseline()
    current = ci_ratchet.compute_current_state()
    current["arch_dependency_graph"]["cycles"].append(["core.fake.a", "core.fake.b"])
    ok, regressions = ci_ratchet.check(baseline, current)
    assert not ok
    assert any("New import cycles" in r for r in regressions)


def test_check_fails_on_new_forbidden_edge(tmp_path):
    """A forbidden edge not in the baseline is reported as regression."""
    baseline = ci_ratchet.load_baseline()
    current = ci_ratchet.compute_current_state()
    current["arch_dependency_graph"]["forbidden_edges"].append(
        {"source": "core.fake", "target": "integrations.fake", "deferred": False}
    )
    ok, regressions = ci_ratchet.check(baseline, current)
    assert not ok
    assert any("New forbidden import edges" in r for r in regressions)


def test_check_fails_on_increased_config_read_count(tmp_path):
    """An increased category count is reported as regression."""
    current = ci_ratchet.compute_current_state()
    baseline = json.loads(json.dumps(current))
    counts = baseline["audit_config_reads"]["category_counts"]
    category = next(category for category, count in counts.items() if count > 0)
    counts[category] = counts[category] - 1  # baseline lower than current
    ok, regressions = ci_ratchet.check(baseline, current)
    assert not ok
    assert any("New direct config reads" in r for r in regressions)
    assert any(category in r for r in regressions)


def test_check_fails_on_new_whitelist_entry(tmp_path):
    """A whitelist entry not in the baseline is reported as regression."""
    baseline = ci_ratchet.load_baseline()
    current = ci_ratchet.compute_current_state()
    current["vulture_whitelist"]["entries"].append(
        {"symbol": "_.totally_fake_symbol", "kind": "method", "path": "core/fake.py"}
    )
    ok, regressions = ci_ratchet.check(baseline, current)
    assert not ok
    assert any("New vulture whitelist entries" in r for r in regressions)


def test_check_allows_improvements(tmp_path):
    """Decreased counts or removed cycles do not fail the ratchet."""
    baseline = ci_ratchet.load_baseline()
    # Pretend baseline had one extra cycle and one extra whitelist entry.
    baseline["arch_dependency_graph"]["cycles"].append(["core.fake.a"])
    baseline["vulture_whitelist"]["entries"].append(
        {"symbol": "_.fake_removed", "kind": "method", "path": "core/fake.py"}
    )
    current = ci_ratchet.compute_current_state()
    ok, regressions = ci_ratchet.check(baseline, current)
    assert ok, "\n".join(regressions)


def test_update_writes_baseline(tmp_path):
    """--update writes a readable baseline file."""
    baseline_path = tmp_path / "baseline.json"
    assert not baseline_path.exists()
    code = ci_ratchet.main(["--update", "--baseline", str(baseline_path)])
    assert code == 0
    assert baseline_path.exists()
    data = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert "arch_dependency_graph" in data
    assert "audit_config_reads" in data
    assert "vulture_whitelist" in data


def test_closure_rejects_stale_vulture_baseline():
    current = ci_ratchet.compute_current_state()
    baseline = json.loads(json.dumps(current))
    baseline["vulture_whitelist"] = {
        "entry_count": 1,
        "entries": [
            {"symbol": "old", "kind": "function", "path": "core/old.py"}
        ],
    }

    report = ci_ratchet.build_closure_report(
        baseline,
        current,
        vulture_scan={
            "status": "zero_dead_code",
            "ok": True,
            "exit_code": 0,
            "finding_count": 0,
            "finding_hashes": [],
        },
    )

    assert not report["ok"]
    assert any("not tightly equal" in item for item in report["closure_errors"])


def test_closure_passes_only_with_exact_zero_vulture_baseline():
    current = ci_ratchet.compute_current_state()
    current["vulture_whitelist"] = {"entry_count": 0, "entries": []}
    baseline = json.loads(json.dumps(current))

    report = ci_ratchet.build_closure_report(
        baseline,
        current,
        vulture_scan={
            "status": "zero_dead_code",
            "ok": True,
            "exit_code": 0,
            "finding_count": 0,
            "finding_hashes": [],
        },
    )

    assert report["ok"]
    assert report["closure"] == {
        "status": "zero_debt",
        "closure_target": 0,
        "current_count": 0,
        "baseline_count": 0,
        "release_eligible": True,
    }


def test_closure_rejects_live_vulture_findings_even_with_zero_whitelist():
    current = ci_ratchet.compute_current_state()
    current["vulture_whitelist"] = {"entry_count": 0, "entries": []}
    baseline = json.loads(json.dumps(current))

    report = ci_ratchet.build_closure_report(
        baseline,
        current,
        vulture_scan={
            "status": "live_dead_code",
            "ok": False,
            "exit_code": 1,
            "finding_count": 1,
            "finding_hashes": ["sha256:" + ("d" * 64)],
        },
    )

    assert report["ok"] is False
    assert report["vulture_scan"]["finding_count"] == 1
    assert any(
        "live dead-code findings" in error
        for error in report["closure_errors"]
    )


def test_update_refuses_nonzero_vulture_baseline(tmp_path, monkeypatch):
    state = {
        "generated_at": "test",
        "arch_dependency_graph": {"cycles": [], "forbidden_edges": []},
        "audit_config_reads": {"category_counts": {}, "unclassified_findings": []},
        "vulture_whitelist": {
            "entry_count": 1,
            "entries": [{"symbol": "old", "kind": "function", "path": "core/old.py"}],
        },
    }
    monkeypatch.setattr(ci_ratchet, "compute_current_state", lambda: state)

    assert ci_ratchet.main(["--update", "--baseline", str(tmp_path / "baseline.json")]) == 1


def test_update_refuses_ratchet_regression_without_explicit_review(tmp_path, monkeypatch):
    baseline = {
        "generated_at": "old",
        "arch_dependency_graph": {"cycles": [], "forbidden_edges": []},
        "audit_config_reads": {"category_counts": {"runtime_data_io": 1}},
        "vulture_whitelist": {"entry_count": 0, "entries": []},
    }
    current = json.loads(json.dumps(baseline))
    current["audit_config_reads"]["category_counts"]["runtime_data_io"] = 2
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    monkeypatch.setattr(ci_ratchet, "compute_current_state", lambda: current)

    assert ci_ratchet.main(["--update", "--baseline", str(baseline_path)]) == 1
    assert json.loads(baseline_path.read_text(encoding="utf-8")) == baseline
