# -*- coding: utf-8 -*-
"""
阶段三测试 — 跨 Agent 关联闭环整合

覆盖：
  1. generate_wiki_page 包含 summary frontmatter
  2. DistillationEngine.write_pages 调用新 CrossAgentLinker
  3. DistillationEngine 发射 distill_complete 事件
  4. _update_frontmatter_field 辅助方法
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml


# ==================== 1. generate_wiki_page summary frontmatter ====================

class TestGenerateWikiPage:
    def test_includes_summary_from_title(self):
        from core.hephaestus.distillation_engine import (
            generate_wiki_page,
            KnowledgeFragment,
        )

        frag = KnowledgeFragment(
            form="decision",
            title="Redis Cluster 选举机制深度解析",
            frontmatter={},
            background="",
            core_content="",
            boundaries={},
            anti_patterns=[],
            related_concepts=[],
        )
        page = generate_wiki_page(frag, "sess-123")
        assert "摘要: Redis Cluster 选举机制深度解析" in page

    def test_includes_summary_from_frontmatter(self):
        from core.hephaestus.distillation_engine import (
            generate_wiki_page,
            KnowledgeFragment,
        )

        frag = KnowledgeFragment(
            form="decision",
            title="x",
            frontmatter={"summary": "自定义摘要"},
            background="",
            core_content="",
            boundaries={},
            anti_patterns=[],
            related_concepts=[],
        )
        page = generate_wiki_page(frag, "sess-123")
        assert "摘要: 自定义摘要" in page

    def test_summary_truncated_to_150_chars(self):
        from core.hephaestus.distillation_engine import (
            generate_wiki_page,
            KnowledgeFragment,
        )

        long_title = "A" * 200
        frag = KnowledgeFragment(
            form="decision",
            title=long_title,
            frontmatter={},
            background="",
            core_content="",
            boundaries={},
            anti_patterns=[],
            related_concepts=[],
        )
        page = generate_wiki_page(frag, "sess-123")
        # summary 应为前150字符（fallback 截断策略）
        assert "摘要: " + "A" * 150 in page
        assert "摘要: " + "A" * 151 not in page


# ==================== 2. write_pages calls new linker ====================

class TestWritePagesIntegration:
    @pytest.fixture(autouse=True)
    def _patch_scorers_and_bus(self, monkeypatch):
        """禁用耗时的 scorer 初始化和事件总线，避免测试超时。"""
        from core.hephaestus.distillation_engine import ValuePrejudgment
        monkeypatch.setattr(ValuePrejudgment, "_get_scorer", lambda self: None)
        monkeypatch.setattr(ValuePrejudgment, "_get_scorer_v2", lambda self: None)
        # mock mnemos_bus.publish_event 避免导入耗时
        monkeypatch.setattr("core.mnemos_bus.publish_event", lambda *a, **k: None)

    def test_write_pages_calls_link_after_distill(self, monkeypatch, tmp_path):
        from core.hephaestus.distillation_engine import (
            DistillationEngine,
            DistillationResult,
            KnowledgeFragment,
            ValuePrejudgment,
        )

        engine = DistillationEngine(wiki_base=str(tmp_path))

        # Mock 新 linker
        mock_linker = MagicMock()
        mock_linker.link_after_distill.return_value = [
            MagicMock(
                from_page=tmp_path / "00-Inbox" / "test.md",
                to_page=Path("/wiki/other.md"),
                reason="similar topic",
                similarity=0.82,
            ),
        ]
        monkeypatch.setattr(engine, "_kia_linker", mock_linker)

        result = DistillationResult(
            session_id="sess-abc",
            prejudgment=ValuePrejudgment.CERTAINLY_YES,
            judgment="knowledge",
            fragments=[
                KnowledgeFragment(
                    form="decision",
                    title="Test",
                    frontmatter={},
                    background="bg",
                    core_content="core",
                    boundaries={},
                    anti_patterns=[],
                    related_concepts=[],
                ),
            ],
        )

        written = engine.write_pages(result)

        assert len(written) == 1
        assert mock_linker.link_after_distill.call_count == 1
        # 验证 frontmatter 被更新
        page_path = Path(written[0])
        text = page_path.read_text(encoding="utf-8")
        assert "跨Agent关联" in text

    def test_write_pages_skips_linker_when_no_fragments(self, monkeypatch, tmp_path):
        from core.hephaestus.distillation_engine import (
            DistillationEngine,
            DistillationResult,
            ValuePrejudgment,
        )
        monkeypatch.setattr(ValuePrejudgment, "_get_scorer", lambda self: None)
        monkeypatch.setattr(ValuePrejudgment, "_get_scorer_v2", lambda self: None)
        monkeypatch.setattr("core.mnemos_bus.publish_event", lambda *a, **k: None)

        engine = DistillationEngine(wiki_base=str(tmp_path))
        mock_linker = MagicMock()
        monkeypatch.setattr(engine, "_kia_linker", mock_linker)

        result = DistillationResult(
            session_id="sess-empty",
            judgment="skip",
            fragments=[],
        )
        written = engine.write_pages(result)
        assert written == []
        mock_linker.link_after_distill.assert_not_called()

    def test_kia_linker_lazily_loaded(self, monkeypatch, tmp_path):
        from core.hephaestus.distillation_engine import DistillationEngine, ValuePrejudgment
        monkeypatch.setattr(ValuePrejudgment, "_get_scorer", lambda self: None)
        monkeypatch.setattr(ValuePrejudgment, "_get_scorer_v2", lambda self: None)
        monkeypatch.setattr("core.mnemos_bus.publish_event", lambda *a, **k: None)

        engine = DistillationEngine(wiki_base=str(tmp_path))
        assert engine._kia_linker is None  # 未初始化

        # 直接 patch _get_kia_linker 的返回值
        monkeypatch.setattr(
            engine, "_get_kia_linker", lambda: MagicMock(
                link_after_distill=lambda p: [],
            ),
        )
        linker = engine._get_kia_linker()
        assert linker is not None

    def test_kia_linker_failure_is_non_blocking(self, monkeypatch, tmp_path):
        from core.hephaestus.distillation_engine import (
            DistillationEngine,
            DistillationResult,
            KnowledgeFragment,
            ValuePrejudgment,
        )
        monkeypatch.setattr(ValuePrejudgment, "_get_scorer", lambda self: None)
        monkeypatch.setattr(ValuePrejudgment, "_get_scorer_v2", lambda self: None)
        monkeypatch.setattr("core.mnemos_bus.publish_event", lambda *a, **k: None)

        engine = DistillationEngine(wiki_base=str(tmp_path))

        mock_linker = MagicMock()
        mock_linker.link_after_distill.side_effect = RuntimeError("vector index down")
        monkeypatch.setattr(engine, "_kia_linker", mock_linker)

        result = DistillationResult(
            session_id="sess-err",
            prejudgment=ValuePrejudgment.CERTAINLY_YES,
            judgment="knowledge",
            fragments=[
                KnowledgeFragment(
                    form="decision",
                    title="Test",
                    frontmatter={},
                    background="bg",
                    core_content="core",
                    boundaries={},
                    anti_patterns=[],
                    related_concepts=[],
                ),
            ],
        )

        written = engine.write_pages(result)
        assert len(written) == 1  # 写文件成功
        # linker 失败不应阻塞


# ==================== 3. distill_complete event ====================

class TestDistillCompleteEvent:
    @pytest.fixture(autouse=True)
    def _patch_scorers(self, monkeypatch):
        from core.hephaestus.distillation_engine import ValuePrejudgment
        monkeypatch.setattr(ValuePrejudgment, "_get_scorer", lambda self: None)
        monkeypatch.setattr(ValuePrejudgment, "_get_scorer_v2", lambda self: None)

    def test_distill_complete_event_emitted(self, monkeypatch, tmp_path):
        from core.hephaestus.distillation_engine import (
            DistillationEngine,
            DistillationResult,
            KnowledgeFragment,
            ValuePrejudgment,
        )

        engine = DistillationEngine(wiki_base=str(tmp_path))
        monkeypatch.setattr(engine, "_kia_linker", False)  # 禁用 linker

        events = []

        def mock_publish(event_type, source, data):
            events.append((event_type, source, data))

        monkeypatch.setattr(
            "core.mnemos_bus.publish_event",
            mock_publish,
        )

        result = DistillationResult(
            session_id="sess-event",
            prejudgment=ValuePrejudgment.CERTAINLY_YES,
            judgment="knowledge",
            fragments=[
                KnowledgeFragment(
                    form="pattern",
                    title="EventTest",
                    frontmatter={},
                    background="bg",
                    core_content="core",
                    boundaries={},
                    anti_patterns=[],
                    related_concepts=[],
                ),
            ],
        )

        engine.write_pages(result)

        assert any(e[0] == "distill_complete" for e in events)
        dc_event = next(e for e in events if e[0] == "distill_complete")
        assert dc_event[2]["session_id"] == "sess-event"
        assert dc_event[2]["title"] == "EventTest"
        assert "page_path" in dc_event[2]


# ==================== 4. _update_frontmatter_field ====================

class TestUpdateFrontmatterField:
    def test_updates_existing_frontmatter(self, tmp_path):
        from core.hephaestus.distillation_engine import DistillationEngine

        md = tmp_path / "test.md"
        md.write_text("""---
type: decision
source_agent: claude
---

# Title

Body content.
""", encoding="utf-8")

        DistillationEngine._update_frontmatter_field(
            md, "cross_agent_refs", [{"page": "other.md", "similarity": 0.8}],
        )

        text = md.read_text(encoding="utf-8")
        assert "跨Agent关联:" in text
        assert "other.md" in text
        assert "Body content." in text  # body 保留

    def test_noop_when_no_frontmatter(self, tmp_path):
        from core.hephaestus.distillation_engine import DistillationEngine

        md = tmp_path / "no_fm.md"
        md.write_text("# No Frontmatter\n\nBody", encoding="utf-8")

        DistillationEngine._update_frontmatter_field(
            md, "key", "value",
        )

        text = md.read_text(encoding="utf-8")
        assert text == "# No Frontmatter\n\nBody"
