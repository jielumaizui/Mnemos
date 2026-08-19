"""黄金路径 e2e 测试：capture -> sync -> distill -> wiki -> search -> preflight.

对应审计项 S44，使用与 docs/demo/run_demo.py 相同的 pipeline，但以断言方式验证
每个环节的产出。
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from docs.demo.run_demo import (  # noqa: E402
    DemoConfig,
    _make_fragment,
    _run_capture_and_distill,
    _run_preflight,
    _run_search,
)


def test_golden_path(tmp_path: Path, monkeypatch) -> None:
    cfg = DemoConfig(tmp_path)
    # 使用 monkeypatch 隔离，避免把 get_config 泄漏到后续测试
    monkeypatch.setattr("core.config.get_config", lambda: cfg)

    # 1. Capture + distill produces a wiki page
    written = _run_capture_and_distill(cfg)
    assert len(written) >= 1, "distillation should write at least one wiki page"
    wiki_page = Path(written[0])
    assert wiki_page.exists(), f"wiki page should exist: {wiki_page}"
    content = wiki_page.read_text(encoding="utf-8")
    assert "asyncio" in content, "wiki page should mention asyncio"
    assert "TimeoutError" in content, "wiki page should mention TimeoutError"

    # 2. RawIndex can find the distilled content
    hits = _run_search(cfg)
    assert len(hits) >= 1, "raw search should find at least one hit for 'asyncio gather'"

    # 3. Preflight injects knowledge when a similar task appears
    checklist_count = _run_preflight(cfg)
    assert checklist_count >= 1, "preflight should inject at least one reminder"


def test_fragment_factory() -> None:
    frag = _make_fragment()
    assert "asyncio" in frag.keywords
    assert "gather" in frag.keywords
