from __future__ import annotations

from scripts import plan_cognitive_consolidation


def test_apply_exits_nonzero_until_trusted_page_and_projection_receipts(tmp_path, monkeypatch):
    from tests.unit.test_cognitive_consolidator import _Cfg

    cfg = _Cfg(tmp_path)
    monkeypatch.setattr(plan_cognitive_consolidation, "get_config", lambda: cfg)

    assert plan_cognitive_consolidation.main(["--apply", "--json"]) == 2


def test_dry_run_remains_safe_and_successful(tmp_path, monkeypatch):
    from tests.unit.test_cognitive_consolidator import _Cfg

    cfg = _Cfg(tmp_path)
    monkeypatch.setattr(plan_cognitive_consolidation, "get_config", lambda: cfg)

    assert plan_cognitive_consolidation.main(["--json"]) == 0
