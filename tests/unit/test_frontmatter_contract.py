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


def test_generate_wiki_page_binds_canonical_cognition_episode_revision():
    from core.hephaestus.distillation_engine import KnowledgeFragment, generate_wiki_page

    revision_id = "cogrev-1234567890abcdef"
    fragment = KnowledgeFragment(
        form="决策记录",
        title="认知事件修订绑定测试页面",
        frontmatter={
            "领域": "测试",
            "摘要": "验证 Wiki 只投影 canonical cognition episode revision id。",
            "cognition_episode_revision_id": revision_id,
        },
        background="Wiki 不是认知状态的第二份 canonical owner。",
        core_content=(
            "## 认知事件投影\n\n"
            "页面只保存 committed revision id，完整语义保留在 CognitiveStateStore。"
        ),
        boundaries={"applies": "conversation distillation"},
        anti_patterns=[],
        related_concepts=[],
    )

    content = generate_wiki_page(fragment, "sess-cognition-episode")

    assert f"认知事件修订ID: {revision_id}" in content
    assert "cognition_episode_revision_id:" not in content


def test_generate_wiki_page_renders_complete_readable_cognition_projection():
    from core.cognition_episode_contract import COGNITION_EPISODE_FIELDS
    from core.hephaestus.distillation_engine import KnowledgeFragment, generate_wiki_page
    from tests.unit.cognitive.test_cognition_episode_contract import (
        _input_spec,
        _resolve,
        _root,
    )

    spec = _input_spec()
    structured_output = _resolve(_root(spec, include_episode=True), spec)["structured_output"]
    fragment = KnowledgeFragment(
        form="决策记录",
        title="Redis 连接池认知事件可读投影",
        frontmatter={"领域": "backend", "摘要": "认知事件完整可读投影。"},
        background="Wiki 是可读投影，canonical owner 仍是 CognitiveStateStore。",
        core_content="## 处置记录\n\n提高连接上限并增加超时监控。",
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
    )

    content = generate_wiki_page(
        fragment,
        "session-1",
        source="codex",
        structured_output=structured_output,
    )

    assert "## 认知事件投影" in content
    assert "### 声明目录" in content
    assert "连接上限过低且缺少超时监控会导致连接池耗尽。" in content
    assert "- 类型: `technical_fact`" in content
    assert "- Scope domain: `backend`" in content
    assert "- 与既有知识关系: `new`" in content
    assert "- 建议动作: `create_page`" in content
    assert "- 置信度: `0.9`" in content
    assert "- 来源修订/Span: `rawrev-1` / `0:" in content
    for field_name in COGNITION_EPISODE_FIELDS:
        assert f"#### `{field_name}`" in content
    assert "- `known`: 当前 Redis 连接池频繁耗尽。" in content
    assert "- `unknown`: 输入没有提供 assumptions 的可靠证据。" in content


def test_generate_wiki_page_includes_usage_and_source_quality_sections():
    from core.hephaestus.distillation_engine import KnowledgeFragment, generate_wiki_page

    fragment = KnowledgeFragment(
        form="problem-solution",
        title="Kimi 长对话完整采集修复",
        frontmatter={
            "evidence_level": "single",
            "confidence": 0.82,
        },
        background="长对话进入 L1 storage 后不能只保留摘要，蒸馏必须能回读完整 artifact。",
        core_content="根因是 L1 projection 指向 artifact，但 wiki builder 没有回读 artifact。",
        boundaries={
            "applies": "适用于 CaptureService 因 payload 过大写入 artifact 的对话。",
            "not_applies": "不适用于 artifact 文件本身已经丢失的历史数据。",
        },
        anti_patterns=["只看 L1 storage 摘要就开始蒸馏。"],
        related_concepts=[],
        self_check_passed=False,
        self_check_issues=["需要用真实 Kimi 长对话再做端到端回放。"],
    )

    content = generate_wiki_page(
        fragment,
        "sess-kimi",
        source="kimi",
        session_coverage="full",
        distill_input_mode="artifact",
        covered_turn_range="1-8",
        truncated=True,
    )

    assert "## 怎么用" in content
    assert "遇到同类问题时" in content
    assert "这条知识仍有待验证项" in content
    assert "## 可信度提示" in content
    assert "- 证据级别: single" in content
    assert "- 置信度: 0.82" in content
    assert "- 会话覆盖: full" in content
    assert "- 蒸馏输入: artifact" in content
    assert "- 是否截断: 是" in content


def test_generate_wiki_page_renders_artifact_evidence_as_summary_link():
    from core.hephaestus.distillation_engine import KnowledgeFragment, generate_wiki_page

    fragment = KnowledgeFragment(
        form="problem-solution",
        title="pytest 失败报告引用",
        frontmatter={"evidence_level": "single", "confidence": 0.9},
        background="测试失败需要保留报告引用，但不能把完整终端输出塞进正文。",
        core_content="修复前先打开 artifact 里的失败摘要和原始报告。",
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
    )
    structured_output = {
        "source_event_ids": ["raw-1"],
        "raw_completeness": "full",
        "gate_decision_id": "gate-1",
        "distill_intent": "create",
        "user_behavior_intent": {
            "content_source": "native_dialogue",
            "user_intent_signal": "seeking_judgment",
            "intent_hypothesis": "seeking_judgment",
            "intent_evidence": [
                {
                    "source_event_id": "raw-1",
                    "quote": "pytest 显示 test_api 失败",
                    "reason": "用户需要定位测试失败原因。",
                }
            ],
            "intent_verification_events": [],
            "intent_confidence": 0.74,
            "intent_status": "unverified",
            "behavior_summary": "用户需要把测试失败报告转成可追踪修复素材。",
        },
        "claims": [
            {
                "claim_id": "claim-1",
                "evidence": [
                    {
                        "source_event_id": "raw-1",
                        "quote": "pytest 显示 test_api 失败",
                        "artifact_uri": "mnemos-artifact://codex/sess-1/turn/3/test_report/0",
                        "artifact_type": "test_report",
                        "artifact_summary": "pytest test_api failure",
                    }
                ],
            }
        ],
    }

    content = generate_wiki_page(
        fragment,
        "sess-1",
        source="codex",
        structured_output=structured_output,
    )

    assert (
        "[pytest test_api failure](mnemos-artifact://codex/sess-1/turn/3/test_report/0)" in content
    )
    assert "artifact_uri" in content
    assert "行为意图摘要:" in content
    assert "- 用户引入原因: 用户需要把测试失败报告转成可追踪修复素材。" in content


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


def test_frontmatter_field_update_preserves_chinese_display_contract(
    tmp_path,
):
    from core.hephaestus.distillation_engine import DistillationEngine

    page = tmp_path / "page.md"
    page.write_text(
        "---\n"
        "类型: technology\n"
        "名称: Redis 连接池\n"
        "knowledge_stage: raw\n"
        "验证状态: pending-verification\n"
        "验证等级: warning\n"
        "质量门禁状态: review\n"
        '领域评分: {"kg": {"scores": {"entity_quality": 0.5}}}\n'
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
    assert "验证状态: pending-verification" in head
    assert "验证等级: warning" in head
    assert "质量门禁状态: review" in head
    assert "领域评分:" in head
    assert "名称: Redis 连接池" in head
    assert "知识阶段: raw" in head
    assert "跨Agent关联:" in head
    assert "knowledge_stage:" not in head
    assert "cross_agent_refs:" not in head


def test_wiki_reader_depth_and_chinese_title_contract(tmp_path):
    from integrations.oracle import WikiReader

    page_dir = tmp_path / "04-Concepts"
    page_dir.mkdir(parents=True)
    page = page_dir / "machine_name.md"
    page.write_text(
        "---\n"
        "名称: 用户可读标题\n"
        "摘要: 这是一条摘要\n"
        "关键词:\n"
        "- 同步\n"
        "置信度: 0.8\n"
        "---\n"
        "# 用户可读标题\n\n"
        "正文内容足够长，用于验证 summary 和 full 两种读取模式。\n",
        encoding="utf-8",
    )

    reader = WikiReader(wiki_path=str(tmp_path))
    metadata = reader.read_page("04-Concepts/machine_name.md", depth="metadata")
    summary = reader.read_page("04-Concepts/machine_name.md", depth="summary")
    full = reader.read_page("04-Concepts/machine_name.md", depth="full")

    assert metadata["title"] == "用户可读标题"
    assert metadata["depth"] == "metadata_only"
    assert summary["title"] == "用户可读标题"
    assert "summary" in summary
    assert full["title"] == "用户可读标题"
    assert full["depth"] == "full"
    assert "正文内容足够长" in full["content"]


def test_wiki_reader_uses_local_metrics_for_explicit_wiki_path(tmp_path):
    from core.wiki_metrics import WikiMetrics
    from integrations.oracle import WikiReader

    page_dir = tmp_path / "04-Concepts"
    page_dir.mkdir(parents=True)
    page = page_dir / "machine_name.md"
    page.write_text(
        "---\n名称: 用户可读标题\n置信度: 0.8\n---\n# 用户可读标题\n正文内容。\n",
        encoding="utf-8",
    )
    metrics = WikiMetrics(db_path=str(tmp_path / ".kg" / "wiki_metrics.db"), wiki_dir=str(tmp_path))
    metrics.upsert_page("04-Concepts/machine_name.md", heat_score=6, heat_level="warm")

    reader = WikiReader(wiki_path=str(tmp_path))

    assert reader.index["04-Concepts/machine_name"]["heat_score"] == 6
    reader.read_page("04-Concepts/machine_name.md", depth="metadata")
    assert metrics.get_page("04-Concepts/machine_name").heat_score == 7
