def test_generate_wiki_page_outputs_chinese_frontmatter_from_english_keys():
    from core.hephaestus.distillation_engine import KnowledgeFragment, generate_wiki_page

    fragment = KnowledgeFragment(
        form="pitfall",
        title="Redis 连接池耗尽的排查与修复",
        frontmatter={
            "type": "technology",
            "name": "Redis 连接池耗尽的排查与修复",
            "domain": "backend",
            "summary": "Redis 连接池耗尽问题的原因、修复方式和适用边界。",
            "status": "草稿",
            "knowledge_stage": "原始",
            "source_count": 1,
            "evidence_level": "单源",
            "confidence": 0.86,
        },
        background="高并发任务中 Redis 连接池偶发耗尽。",
        core_content="原因是连接池上限过低且缺少超时监控。",
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
    )

    content = generate_wiki_page(fragment, "sess-001")
    head = content.split("---", 2)[1]

    assert "类型: technology" in head
    assert "名称: Redis 连接池耗尽的排查与修复" in head
    assert "领域: backend" in head
    assert "摘要: Redis 连接池耗尽问题的原因、修复方式和适用边界。" in head
    assert "状态: 草稿" in head
    assert "知识阶段: 原始" in head
    assert "来源数量: 1" in head
    assert "证据级别: 单源" in head
    assert "type:" not in head
    assert "knowledge_stage:" not in head


def test_rule_scorer_accepts_chinese_frontmatter_aliases():
    from core.kia.rule_scorer import completeness_penalty

    result = completeness_penalty(
        {"类型": "technology", "名称": "Redis 连接池", "领域": "backend"},
        "Redis 连接池耗尽问题的原因、修复方式和适用边界。" * 2,
    )

    assert result.score > 0.8


def test_wiki_lint_accepts_chinese_contract_fields():
    from scripts.wiki_lint import check_missing_meta

    page = {
        "frontmatter": {
            "状态": "草稿",
            "来源数量": 1,
            "知识阶段": "原始",
            "证据级别": "单源",
        }
    }

    assert check_missing_meta(page) == []


def test_frontmatter_field_update_preserves_chinese_display_contract(tmp_path):
    from core.hephaestus.distillation_engine import DistillationEngine

    page = tmp_path / "page.md"
    page.write_text(
        "---\n"
        "类型: technology\n"
        "名称: Redis 连接池\n"
        "knowledge_stage: raw\n"
        "---\n"
        "# Redis 连接池\n",
        encoding="utf-8",
    )

    DistillationEngine._update_frontmatter_field(
        page,
        "cross_agent_refs",
        [{"page": "Other", "reason": "same topic"}],
    )

    head = page.read_text(encoding="utf-8").split("---", 2)[1]
    assert "类型: technology" in head
    assert "名称: Redis 连接池" in head
    assert "知识阶段: raw" in head
    assert "跨Agent关联:" in head
    assert "knowledge_stage:" not in head
    assert "cross_agent_refs:" not in head
