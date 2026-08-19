# -*- coding: utf-8 -*-
"""
prompt_builder.py 单元测试

覆盖项：
- PromptBuilder.__init__() — 初始化与依赖注入
- PromptBuilder.build() — 完整构建流水线
- TemplateRegistry — 模板选择优先级、回退机制、Schema 渲染
- ContextAssembler — 上下文组装（会话、Wiki 页面、积压项）
- TokenBudgetManager — Token 预算分配与截断策略
- ContentFormatter — 会话格式化、代码清洗、截断
- _render() — 模板变量替换与未替换占位符清理
- build_distill_prompt() — 便捷函数
"""

import json
from pathlib import Path
import re
from unittest.mock import patch

import pytest

from core.hephaestus.prompt_builder import (
    ContentFormatter,
    ContextAssembler,
    DeferredRecord,
    DistillTask,
    PromptBuilder,
    Session,
    TemplateRegistry,
    TokenBudget,
    TokenBudgetManager,
    PromptWikiPage,
    build_distill_prompt,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_template_dir(tmp_path):
    """提供隔离的模板目录，含基础模板与任务模板。"""
    tmpl = tmp_path / "templates"
    tmpl.mkdir()

    # 通用回退模板
    (tmpl / "_base.md").write_text(
        "# {task_type}\n\n"
        "session_type: {session_type}\n"
        "date: {current_date}\n"
        "session_id: {session_id}\n"
        "message_count: {message_count}\n"
        "source: {source}\n\n"
        "## Conversation\n{conversation_text}\n\n"
        "## Target Page\n{target_page_content}\n\n"
        "## Related Wiki\n{related_wiki_pages}\n\n"
        "## Backlog\n{backlog_summary}\n\n"
        "{output_schema}",
        encoding="utf-8",
    )

    # extract 任务基础模板
    extract_dir = tmpl / "extract"
    extract_dir.mkdir()
    (extract_dir / "base.md").write_text(
        "EXTRACT BASE\nTask: {task_type}\nType: {session_type}\n\n{conversation_text}",
        encoding="utf-8",
    )
    (extract_dir / "coding.md").write_text(
        "EXTRACT CODING\nTask: {task_type}\nType: {session_type}\n\n{conversation_text}",
        encoding="utf-8",
    )

    # value_judge 模板（用于 JSON 验证测试）
    vj_dir = tmpl / "value_judge"
    vj_dir.mkdir()
    (vj_dir / "base.md").write_text(
        "VALUE JUDGE\nOutput JSON format.\n\n{conversation_text}",
        encoding="utf-8",
    )

    return tmpl


@pytest.fixture
def tmp_wiki_dir(tmp_path):
    """提供隔离的 Wiki 目录。"""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    return wiki


@pytest.fixture
def char_tokenizer():
    """按字符数计数的 tokenizer，使测试可预测。"""
    class CharTokenizer:
        @staticmethod
        def estimate(text):
            return len(text) if text else 0

        @classmethod
        def truncate_to_tokens(cls, text, max_tokens):
            return text if cls.estimate(text) <= max_tokens else text[:max_tokens]

    return CharTokenizer()


@pytest.fixture
def make_builder(tmp_template_dir, tmp_wiki_dir, char_tokenizer):
    """工厂函数：快速构造 PromptBuilder。"""

    def _make(template_dir=None, wiki_dir=None, tokenizer=None):
        return PromptBuilder(
            template_dir=template_dir or tmp_template_dir,
            wiki_dir=wiki_dir or tmp_wiki_dir,
            tokenizer=tokenizer or char_tokenizer,
        )

    return _make


@pytest.fixture
def simple_session():
    """提供一个单消息会话。"""
    return Session(
        id="sess_001",
        messages=[
            {"role": "user", "content": "Hello, how do I use Python?"},
            {"role": "assistant", "content": "You can start with `print()`."},
        ],
        agent_name="claude",
    )


# ---------------------------------------------------------------------------
# 1. PromptBuilder.__init__()
# ---------------------------------------------------------------------------


def test_prompt_builder_init_uses_provided_dirs(make_builder, tmp_template_dir, tmp_wiki_dir):
    """PromptBuilder 应使用传入的 template_dir 与 wiki_dir。"""
    pb = make_builder()

    assert pb.template_registry.template_dir == tmp_template_dir
    assert pb.context_assembler.wiki_dir == tmp_wiki_dir


def test_prompt_builder_init_uses_custom_tokenizer(make_builder, char_tokenizer):
    """PromptBuilder 应将自定义 tokenizer 注入 TokenBudgetManager。"""
    pb = make_builder(tokenizer=char_tokenizer)

    # callable tokenizer 会被包装为带有 estimate() 方法的对象
    assert hasattr(pb.token_budget.tokenizer, "estimate")
    assert pb.token_budget.tokenizer.estimate("hello") == 5


# ---------------------------------------------------------------------------
# 2. PromptBuilder.build() — 模板选择
# ---------------------------------------------------------------------------


def test_build_selects_specific_session_type_template(make_builder, simple_session):
    """build() 应优先选择 {task_type}/{session_type} 模板。"""
    pb = make_builder()
    task = DistillTask(task_type="extract", session=simple_session, session_type="coding")

    prompt = pb.build(task)

    assert prompt.startswith("EXTRACT CODING")
    assert "Hello, how do I use Python?" in prompt


def test_build_falls_back_to_task_base_template(make_builder, simple_session):
    """当 {task_type}/{session_type} 不存在时，应回退到 {task_type}/base。"""
    pb = make_builder()
    task = DistillTask(task_type="extract", session=simple_session, session_type="marketing")

    prompt = pb.build(task)

    assert prompt.startswith("EXTRACT BASE")


def test_build_falls_back_to_global_base(make_builder, simple_session):
    """当任务模板不存在时，应回退到 _base 模板。"""
    pb = make_builder()
    task = DistillTask(task_type="backlink", session=simple_session, session_type="general")

    prompt = pb.build(task)

    assert prompt.startswith("# backlink")


# ---------------------------------------------------------------------------
# 3. PromptBuilder.build() — 上下文注入
# ---------------------------------------------------------------------------


def test_build_injects_session_context(make_builder, simple_session):
    """build() 应将会话内容注入模板；使用 _base 模板可验证全部字段。"""
    pb = make_builder()
    # 使用 backlink 任务触发 _base 模板，包含所有占位符
    task = DistillTask(task_type="backlink", session=simple_session, session_type="general")

    prompt = pb.build(task)

    assert "sess_001" in prompt
    assert "message_count: 2" in prompt
    assert "claude" in prompt
    assert "Hello, how do I use Python?" in prompt
    assert "You can start with `print()`." in prompt


def test_build_injects_target_wiki_page(make_builder, simple_session):
    """build() 应将目标 Wiki 页面内容注入 target_page_content。"""
    pb = make_builder()
    wiki_page = PromptWikiPage(
        path=Path("test.md"), title="Test", content="Wiki page content here."
    )
    task = DistillTask(
        task_type="incremental",
        session=simple_session,
        target_wiki_page=wiki_page,
    )

    prompt = pb.build(task)

    assert "Wiki page content here." in prompt


def test_build_injects_backlog_items(make_builder, simple_session):
    """build() 应将积压记录注入 backlog_summary。"""
    pb = make_builder()
    backlog = [
        DeferredRecord(session_id="sid_a", agent_name="claude", content="Record one content."),
        DeferredRecord(session_id="sid_b", agent_name="hermes", content="Record two content."),
    ]
    task = DistillTask(task_type="merge", session=simple_session, backlog_items=backlog)

    prompt = pb.build(task)

    assert "Record one content." in prompt
    assert "Record two content." in prompt
    assert "claude" in prompt
    assert "hermes" in prompt
    assert "待合并的 2 条记录" in prompt


def test_build_without_session_injects_defaults(make_builder):
    """无会话时，build() 应注入空默认值。"""
    pb = make_builder()
    task = DistillTask(task_type="value_judge", session=None, session_type="general")

    prompt = pb.build(task)

    # value_judge/base 模板不含所有占位符，验证 prompt 不为空且包含任务类型即可
    assert "value_judge" in prompt.lower() or "VALUE JUDGE" in prompt
    assert prompt.strip() != ""


# ---------------------------------------------------------------------------
# 4. PromptBuilder.build() — Token 截断
# ---------------------------------------------------------------------------


def test_build_truncates_conversation_when_token_budget_exceeded(make_builder):
    """当会话内容超出 Token 预算时，build() 应截断对话内容。"""
    pb = make_builder()
    long_msg = "A" * 5000
    session = Session(id="s1", messages=[{"role": "user", "content": long_msg}])

    # total=200, reserve=20, available=180, content_limit≈99
    budget = TokenBudget(total_limit=200, output_reserve=20)
    task = DistillTask(task_type="extract", session=session, budget_config=budget)

    prompt = pb.build(task)

    assert "截断" in prompt
    assert len(prompt) < len(long_msg) + 200


def test_build_removes_related_context_when_severe_excess(make_builder):
    """Token 严重不足时，build() 应完全移除相关上下文。"""
    pb = make_builder()
    long_msg = "B" * 3000
    session = Session(id="s1", messages=[{"role": "user", "content": long_msg}])

    # total=100, reserve=20, available=80 — 极端紧张
    budget = TokenBudget(total_limit=100, output_reserve=20)
    task = DistillTask(task_type="extract", session=session, budget_config=budget)

    prompt = pb.build(task)

    # 相关上下文应被完全移除
    assert "（暂无相关已有知识）" not in prompt
    # 但对话内容仍应保留（已被截断）
    assert "B" in prompt


# ---------------------------------------------------------------------------
# 5. TemplateRegistry
# ---------------------------------------------------------------------------


def test_template_registry_loads_all_md_files(tmp_path):
    """TemplateRegistry 应递归加载目录下所有 .md 文件。"""
    tmpl = tmp_path / "tmpl"
    tmpl.mkdir()
    (tmpl / "a.md").write_text("A")
    sub = tmpl / "sub"
    sub.mkdir()
    (sub / "b.md").write_text("B")

    reg = TemplateRegistry(tmpl)

    assert "a" in reg._cache
    assert "sub/b" in reg._cache
    assert reg._cache["a"] == "A"
    assert reg._cache["sub/b"] == "B"


def test_template_registry_select_priority(tmp_path):
    """select() 应按 {task}/{session} > {task}/base > _base 优先级选择。"""
    tmpl = tmp_path / "tmpl"
    tmpl.mkdir()
    (tmpl / "_base.md").write_text("BASE")
    task_dir = tmpl / "extract"
    task_dir.mkdir()
    (task_dir / "base.md").write_text("TASK_BASE")
    (task_dir / "coding.md").write_text("TASK_CODING")

    reg = TemplateRegistry(tmpl)

    assert reg.select("extract", "coding") == "TASK_CODING"
    assert reg.select("extract", "marketing") == "TASK_BASE"
    assert reg.select("merge", "general") == "BASE"


def test_template_registry_render_schema(tmp_path):
    """render_schema() 应将 JSON Schema 转为 Markdown 列表。"""
    tmpl = tmp_path / "tmpl"
    tmpl.mkdir()
    schema_dir = tmpl / "_output_schemas"
    schema_dir.mkdir()
    schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Summary text"},
            "score": {"type": "number", "description": "Quality score"},
        },
        "required": ["summary"],
    }
    (schema_dir / "extract.json").write_text(json.dumps(schema))

    reg = TemplateRegistry(tmpl)
    md = reg.render_schema("extract")

    assert "**summary**" in md
    assert "**score**" in md
    assert "(必填)" in md
    assert "Summary text" in md


def test_template_registry_renders_extract_skip_and_non_skip_conditions():
    """extract schema 的组合/条件约束必须进入 LLM 可见的 Prompt。"""
    repository_root = Path(__file__).resolve().parents[2]
    registry = TemplateRegistry(repository_root / "prompts" / "distill")

    rendered = registry.render_schema("extract")

    assert "**分支：SkipOutput**" in rendered
    assert "**分支：KnowledgeOrSkillOutput**" in rendered
    assert "**fragments** (`array`; 最多项数：0)" in rendered
    assert "**fragments** (`array`; 最少项数：1)" in rendered
    assert "**当 `distill_intent` 固定为 `skip` 时**" in rendered
    assert "**满足时必填字段**：`skip_reason`、`no_value_evidence`、`claims`" in rendered
    assert "**claims** (`array`; 最多项数：0)" in rendered
    assert (
        "**否则（非 skip）必填字段**："
        "`user_behavior_intent`、`cognition_episode`、`claims`"
    ) in rendered
    assert "**claims** (`array`; 最少项数：1)" in rendered
    assert "**当 `content_source` 固定为 `external_file` 时**" not in rendered
    assert "**intent_hypothesis** (`string`; 固定为 `curate_or_decision_material`)" not in rendered
    assert "`relation_to_existing.type` 为以下之一：`contradicts`、`supersedes`" in rendered
    assert "**recommended_action** (`string`; 固定为 `route_to_dispute`)" in rendered
    assert "`recommended_action` 不得为 `skip`" in rendered
    assert "匹配模式：`^artifact-ref:[0-9a-f]{32}$`" in rendered
    assert "匹配模式：`^source-authority:[0-9a-f]{32}$`" in rendered
    assert "artifact_uri" not in rendered


def test_extract_template_requires_complete_skip_instead_of_bare_empty_fragments():
    """低价值输入必须走完整 skip 分支，而不是只输出空 fragments。"""
    repository_root = Path(__file__).resolve().parents[2]
    template = (
        repository_root / "prompts" / "distill" / "extract" / "base.md"
    ).read_text(encoding="utf-8")

    assert "不得只返回空的 `fragments` 数组" in template
    assert "完整的严格 skip 输出" in template
    assert "至少一条 `no_value_evidence`" in template
    assert "`claims=[]` 与 `fragments=[]`" in template
    assert '"source_event_id": "session:{session_id}"' not in template
    assert "从 source_event_ids 中选择一个 id" in template
    assert '"judgment": "knowledge" | "skill" | "skip"' not in template
    assert '"distill_intent": "create|update|merge|dispute|reinforce|skip"' not in template


def test_template_registry_render_schema_missing_returns_empty(tmp_path):
    """Schema 文件不存在时，render_schema() 应返回空字符串。"""
    tmpl = tmp_path / "tmpl"
    tmpl.mkdir()
    reg = TemplateRegistry(tmpl)

    assert reg.render_schema("nonexistent") == ""


def test_skill_proposal_prompt_examples_obey_the_strict_output_schema():
    """Prompt examples must not teach the model to emit schema-invalid fallbacks."""

    repo_root = Path(__file__).resolve().parents[2]
    prompt = (repo_root / "prompts/distill/skill_suggestion/base.md").read_text(
        encoding="utf-8"
    )
    schema = json.loads(
        (repo_root / "prompts/distill/_output_schemas/skill_suggestion.json").read_text(
            encoding="utf-8"
        )
    )
    examples = [
        json.loads(block)
        for block in re.findall(r"```json\n(.*?)\n```", prompt, flags=re.DOTALL)
    ]

    assert len(examples) == 1
    example = examples[0]
    assert set(example) == set(schema["required"])
    assert example["asset_schema"] == schema["properties"]["asset_schema"]["const"]
    assert example["asset_type"] in schema["properties"]["asset_type"]["enum"]
    assert example["skill_name"].strip()
    assert example["skill_purpose"].strip()
    for field in ("evidence_refs", "applicability", "failure_modes", "verification_recipe"):
        assert isinstance(example[field], list)
    assert isinstance(example["automation_derivative_allowed"], bool)


# ---------------------------------------------------------------------------
# 6. ContentFormatter
# ---------------------------------------------------------------------------


def test_content_formatter_formats_messages():
    """format_session() 应按角色格式化每条消息。"""
    session = Session(
        id="s1",
        messages=[
            {"role": "user", "content": "Question?"},
            {"role": "assistant", "content": "Answer."},
        ],
    )
    cf = ContentFormatter()

    result = cf.format_session(session)

    assert "### Message 1 (user)" in result
    assert "### Message 2 (assistant)" in result
    assert "Question?" in result
    assert "Answer." in result


def test_content_formatter_removes_code_blocks_by_default():
    """默认情况下，format_session() 应移除代码块。"""
    session = Session(
        id="s1",
        messages=[{"role": "assistant", "content": "Code:\n```python\nprint(1)\n```\nDone"}],
    )
    cf = ContentFormatter()

    result = cf.format_session(session)

    assert "```python" not in result
    assert "print(1)" not in result
    assert "Code:" in result
    assert "Done" in result


def test_content_formatter_keeps_code_when_requested():
    """keep_code=True 时，format_session() 应保留代码块。"""
    session = Session(
        id="s1",
        messages=[{"role": "assistant", "content": "Code:\n```python\nprint(1)\n```\nDone"}],
    )
    cf = ContentFormatter()

    result = cf.format_session(session, keep_code=True)

    assert "```python" in result
    assert "print(1)" in result


def test_content_formatter_truncates_long_content(monkeypatch, char_tokenizer):
    """format_session() 应在内容超过 token budget 时截断。"""
    session = Session(
        id="s1",
        messages=[{"role": "user", "content": "A" * 200}],
    )
    cf = ContentFormatter()
    monkeypatch.setattr("core.hephaestus.prompt_builder.get_tokenizer", lambda: char_tokenizer)

    result = cf.format_session(session, max_tokens=80)

    assert "截断" in result
    assert len(result) < len("A" * 200) + 50


def test_content_formatter_skips_empty_after_cleaning():
    """清洗后内容为空的消息应被跳过。"""
    session = Session(
        id="s1",
        messages=[
            {"role": "user", "content": "[thinking]secret[/thinking]"},
            {"role": "assistant", "content": "Real content."},
        ],
    )
    cf = ContentFormatter()

    result = cf.format_session(session)

    assert "secret" not in result
    assert "Real content." in result
    assert "Message 1" not in result  # 被清洗为空，跳过
    assert "Message 2" in result


# ---------------------------------------------------------------------------
# 7. TokenBudgetManager
# ---------------------------------------------------------------------------


def test_token_budget_manager_no_truncation_when_under_budget(char_tokenizer):
    """总 Token 未超预算时，apply() 应原样返回。"""
    tbm = TokenBudgetManager(tokenizer=char_tokenizer)
    budget = TokenBudget(total_limit=1000, output_reserve=200)
    ctx = {
        "conversation_text": "short",
        "related_wiki_pages": "none",
        "other": "x",
    }

    result = tbm.apply(ctx, budget)

    assert result["conversation_text"] == "short"
    assert result["related_wiki_pages"] == "none"


def test_token_budget_manager_truncates_related_context_first(char_tokenizer):
    """超预算时，apply() 应优先截断 related_wiki_pages。"""
    tbm = TokenBudgetManager(tokenizer=char_tokenizer)
    budget = TokenBudget(total_limit=300, output_reserve=50)
    # available=250, context_limit=62, content_limit=137
    ctx = {
        "conversation_text": "C" * 150,
        "related_wiki_pages": "W" * 100 + "### Page1\ncontent\n### Page2\ncontent",
        "other": "O" * 20,
    }

    result = tbm.apply(ctx, budget)

    # related_wiki_pages 应被截断或移除
    assert len(result["related_wiki_pages"]) < len(ctx["related_wiki_pages"])
    # conversation_text 应尽可能保留
    assert "C" in result["conversation_text"]


def test_token_budget_manager_removes_related_context_when_still_excess(char_tokenizer):
    """截断相关上下文后仍超预算，apply() 应完全移除 related_wiki_pages。"""
    tbm = TokenBudgetManager(tokenizer=char_tokenizer)
    budget = TokenBudget(total_limit=100, output_reserve=20)
    ctx = {
        "conversation_text": "X" * 200,
        "related_wiki_pages": "Y" * 100,
        "other": "Z" * 10,
    }

    result = tbm.apply(ctx, budget)

    assert result["related_wiki_pages"] == ""


# ---------------------------------------------------------------------------
# 8. _render() — 模板变量替换
# ---------------------------------------------------------------------------


def test_render_replaces_placeholders(make_builder):
    """_render() 应将 {variable} 替换为对应值。"""
    pb = make_builder()
    template = "Task: {task_type}, Type: {session_type}, ID: {session_id}"
    context = {"task_type": "extract", "session_type": "coding", "session_id": "s1"}

    result = pb._render(template, context)

    assert result == "Task: extract, Type: coding, ID: s1"


def test_render_removes_unmatched_placeholders(make_builder):
    """_render() 应清理未提供值的占位符。"""
    pb = make_builder()
    template = "Known: {known}, Unknown: {unknown_var}"
    context = {"known": "value"}

    result = pb._render(template, context)

    assert "Known: value" in result
    assert "{unknown_var}" not in result
    assert "Unknown:" in result


# ---------------------------------------------------------------------------
# 9. build_distill_prompt() — 便捷函数
# ---------------------------------------------------------------------------


def test_build_distill_prompt_convenience_function():
    """build_distill_prompt() 应正确构造会话并返回 Prompt。"""
    messages = [
        {"role": "user", "content": "How do I debug Python?"},
        {"role": "assistant", "content": "Use pdb."},
    ]

    with patch("core.hephaestus.prompt_builder.PromptBuilder.build") as mock_build:
        mock_build.return_value = "MOCKED PROMPT"
        prompt = build_distill_prompt(
            "sid_123", messages, task_type="extract", session_type="coding"
        )

    assert prompt == "MOCKED PROMPT"
    mock_build.assert_called_once()
    call_task = mock_build.call_args[0][0]
    assert call_task.task_type == "extract"
    assert call_task.session_type == "coding"
    assert call_task.session.id == "sid_123"
    assert len(call_task.session.messages) == 2


# ---------------------------------------------------------------------------
# 10. TokenBudget 属性
# ---------------------------------------------------------------------------


def test_token_budget_properties():
    """TokenBudget 各属性应按比例正确计算。"""
    budget = TokenBudget(total_limit=16000, output_reserve=2000)

    assert budget.available_for_input == 14000
    assert budget.system_limit == 1400  # 10%
    assert budget.context_limit == 3500  # 25%
    assert budget.content_limit == 7700  # 55%


# ---------------------------------------------------------------------------
# 11. PromptWikiPage.read_content()
# ---------------------------------------------------------------------------


def test_wiki_page_read_content_from_field():
    """PromptWikiPage 应优先返回已设置的 content 字段。"""
    page = PromptWikiPage(path=Path("/fake/path.md"), title="T", content="inline content")

    assert page.read_content() == "inline content"


def test_wiki_page_read_content_from_file(tmp_path):
    """PromptWikiPage 应在 content 为空时从文件读取。"""
    md_file = tmp_path / "test.md"
    md_file.write_text("file content", encoding="utf-8")
    page = PromptWikiPage(path=md_file, title="T", content="")

    assert page.read_content() == "file content"


def test_distill_prompt_behavior_intent_never_uses_llm_fallback(tmp_path, monkeypatch):
    from core.app.intent_router import IntentRouter

    monkeypatch.setattr(
        "core.app.intent_router.get_config",
        lambda: {"intent_router.llm_fallback_enabled": True},
    )
    monkeypatch.setattr(
        IntentRouter,
        "_llm_classify",
        lambda self, user_input, candidates: pytest.fail(
            "checkpoint prompt preparation must remain local and deterministic"
        ),
    )
    assembler = ContextAssembler(tmp_path)
    monkeypatch.setattr(assembler, "_build_cognitive_profile_context", lambda: "none")
    task = DistillTask(
        task_type="extract",
        session=Session(
            id="deterministic-prompt",
            messages=[{"role": "user", "content": "glorp zeta flux"}],
            agent_name="",
        ),
        preformatted=True,
    )

    context = assembler.assemble(task)

    assert "intent: chat" in context["behavior_intent_context"]
