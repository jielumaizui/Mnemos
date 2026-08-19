"""Tests for scripts/check_maintainability_budget.py."""

from __future__ import annotations

from datetime import date, timedelta
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import check_maintainability_budget as budget_gate  # noqa: E402


def _write(path: Path, lines: int, body: str = "") -> None:
    filler = "\n".join(f"# filler {i}" for i in range(max(0, lines - 1)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((filler + "\n" + body).strip() + "\n", encoding="utf-8")


def _budget_for(metrics):
    budget = budget_gate.build_budget(metrics)
    budget["generated_at"] = "test"
    return budget


def test_fails_on_new_large_file(tmp_path):
    root = tmp_path
    _write(root / "core" / "large.py", 1502)
    metrics = budget_gate.scan_repo(root)
    budget = _budget_for({})

    ok, report = budget_gate.check_budget(metrics, budget)

    assert not ok
    assert report["failures"][0]["type"] == "new_large_file"


def test_fails_on_broad_exception_growth(tmp_path):
    root = tmp_path
    _write(
        root / "core" / "service.py",
        1,
        "try:\n    run()\nexcept Exception as exc:\n    logger.warning('failed: %s', exc)\n",
    )
    metrics = budget_gate.scan_repo(root)
    budget = _budget_for(metrics)
    budget["broad_exceptions"]["core/service.py"]["max_count"] = 0

    ok, report = budget_gate.check_budget(metrics, budget)

    assert not ok
    assert report["failures"][0]["type"] == "broad_exception_growth"


def test_reports_reduced_budget_without_failing(tmp_path):
    root = tmp_path
    _write(
        root / "core" / "service.py",
        1,
        "try:\n    run()\nexcept Exception as exc:\n    logger.warning('failed: %s', exc)\n",
    )
    metrics = budget_gate.scan_repo(root)
    budget = _budget_for(metrics)
    budget["large_files"]["core/service.py"] = {
        "max_lines": 10,
        "owner": "core",
        "split_plan": "test",
    }
    budget["broad_exceptions"]["core/service.py"]["max_count"] = 2

    ok, report = budget_gate.check_budget(metrics, budget)

    assert ok
    assert any(item["type"] == "broad_exception_reduced" for item in report["improvements"])


def test_unclassified_broad_exception_is_visible_but_baselined(tmp_path):
    root = tmp_path
    _write(root / "core" / "service.py", 1, "try:\n    run()\nexcept Exception:\n    return None\n")
    metrics = budget_gate.scan_repo(root)
    budget = _budget_for(metrics)

    ok, report = budget_gate.check_budget(metrics, budget)

    assert ok
    assert report["summary"]["unclassified_broad_exceptions"] == 1

    budget["require_per_catch_classification"] = True
    ok, strict_report = budget_gate.check_budget(metrics, budget)
    assert not ok
    assert strict_report["failures"][0]["type"] == "unclassified_broad_exception"


def test_unclassified_broad_exception_growth_fails(tmp_path):
    root = tmp_path
    _write(root / "core" / "service.py", 1, "try:\n    run()\nexcept Exception:\n    return None\n")
    metrics = budget_gate.scan_repo(root)
    budget = _budget_for(metrics)
    budget["max_unclassified_broad_exceptions"] = 0

    ok, report = budget_gate.check_budget(metrics, budget)

    assert not ok
    assert report["failures"][0]["type"] == "unclassified_broad_exception_growth"


def test_required_path_unclassified_broad_exception_fails(tmp_path):
    root = tmp_path
    _write(root / "core" / "service.py", 1, "try:\n    run()\nexcept Exception:\n    return None\n")
    metrics = budget_gate.scan_repo(root)
    budget = _budget_for(metrics)
    budget["require_classified_broad_exception_paths"] = ["core/service.py"]

    ok, report = budget_gate.check_budget(metrics, budget)

    assert not ok
    assert report["failures"][0]["type"] == "unclassified_broad_exception_in_required_path"


def test_closure_accepts_tight_time_bounded_debt_but_blocks_release(tmp_path):
    _write(
        tmp_path / "core" / "service.py",
        1,
        "try:\n    run()\nexcept Exception as exc:\n    logger.warning('failed: %s', exc)\n",
    )
    metrics = budget_gate.scan_repo(tmp_path)
    budget = _budget_for(metrics)

    ok, report = budget_gate.check_budget(metrics, budget, closure=True)

    assert ok
    assert report["closure"] == {
        "requested": True,
        "status": "accepted_debt",
        "closure_target": 0,
        "current_count": 1,
        "accepted_count": 1,
        "unaccepted_count": 0,
        "release_eligible": False,
    }


def test_closure_rejects_expired_acceptance(tmp_path):
    _write(
        tmp_path / "core" / "service.py",
        1,
        "try:\n    run()\nexcept Exception as exc:\n    logger.warning('failed: %s', exc)\n",
    )
    metrics = budget_gate.scan_repo(tmp_path)
    budget = _budget_for(metrics)
    budget["broad_exceptions"]["core/service.py"]["expires_at"] = (
        date.today() - timedelta(days=1)
    ).isoformat()

    ok, report = budget_gate.check_budget(metrics, budget, closure=True)

    assert not ok
    assert any(item["type"] == "expired_risk_acceptance" for item in report["failures"])


def test_closure_rejects_loose_improvement_baseline(tmp_path):
    _write(
        tmp_path / "core" / "service.py",
        1,
        "try:\n    run()\nexcept Exception as exc:\n    logger.warning('failed: %s', exc)\n",
    )
    metrics = budget_gate.scan_repo(tmp_path)
    budget = _budget_for(metrics)
    budget["broad_exceptions"]["core/service.py"]["max_count"] = 2

    ok, report = budget_gate.check_budget(metrics, budget, closure=True)

    assert not ok
    assert any(item["type"] == "closure_baseline_not_tight" for item in report["failures"])


def test_same_count_replacement_cannot_reuse_old_broad_exception_acceptance(tmp_path):
    source = tmp_path / "core" / "service.py"
    _write(
        source,
        1,
        "try:\n    run()\nexcept Exception as exc:\n    logger.warning('first: %s', exc)\n",
    )
    original = budget_gate.scan_repo(tmp_path)
    budget = _budget_for(original)
    _write(
        source,
        1,
        "try:\n    other()\nexcept Exception as exc:\n    logger.warning('replacement: %s', exc)\n",
    )

    ok, report = budget_gate.check_budget(budget_gate.scan_repo(tmp_path), budget)

    assert not ok
    assert any(item["type"] == "broad_exception_identity_changed" for item in report["failures"])


def test_source_parse_error_fails_closed(tmp_path):
    _write(tmp_path / "core" / "broken.py", 1, "def broken(:\n    pass\n")
    metrics = budget_gate.scan_repo(tmp_path)
    budget = _budget_for(metrics)

    ok, report = budget_gate.check_budget(metrics, budget)

    assert not ok
    assert report["failures"][0]["type"] == "source_parse_error"


def test_budget_update_requires_explicit_review_for_changed_catch_identity(tmp_path):
    source = tmp_path / "core" / "service.py"
    _write(
        source,
        1,
        "try:\n    run()\nexcept Exception as exc:\n    logger.warning('first: %s', exc)\n",
    )
    original = budget_gate.scan_repo(tmp_path)
    budget = _budget_for(original)
    _write(
        source,
        1,
        "try:\n    other()\nexcept Exception as exc:\n    logger.warning('replacement: %s', exc)\n",
    )

    changes = budget_gate.risk_acceptance_changes(budget_gate.scan_repo(tmp_path), budget)

    assert changes == ["changed broad-exception identities: core/service.py"]


def test_budget_update_allows_removing_registered_broad_exception(tmp_path):
    source = tmp_path / "core" / "service.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "try:\n    first()\nexcept Exception as exc:\n    logger.warning('first: %s', exc)\n"
        "try:\n    second()\nexcept Exception as exc:\n    logger.warning('second: %s', exc)\n",
        encoding="utf-8",
    )
    budget = _budget_for(budget_gate.scan_repo(tmp_path))
    source.write_text(
        "try:\n    first()\nexcept Exception as exc:\n    logger.warning('first: %s', exc)\n",
        encoding="utf-8",
    )

    changes = budget_gate.risk_acceptance_changes(budget_gate.scan_repo(tmp_path), budget)

    assert changes == []


def test_budget_update_requires_explicit_review_for_large_file_growth(tmp_path):
    source = tmp_path / "core" / "large.py"
    _write(source, 1502)
    original = budget_gate.scan_repo(tmp_path)
    budget = _budget_for(original)
    _write(source, 1503)

    changes = budget_gate.risk_acceptance_changes(budget_gate.scan_repo(tmp_path), budget)

    assert changes == ["increased large-file acceptance: core/large.py"]
