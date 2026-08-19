"""
Tests for core.app.question_answer_search

Covers: QuestionAnswerSearch init, search, answer,
        extract_question_type, _score_paragraph, _normalize_question,
        _extract_solution_blocks, _split_into_paragraphs, _results_to_context.
"""

from unittest.mock import Mock, patch

import pytest

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.app.question_answer_search import QuestionAnswerSearch
from core.app.context_search import SearchResult


def _principal() -> PrincipalEnvelope:
    return PrincipalEnvelope(
        principal_id="mcp:codex:qa-test",
        agent="codex",
        host_kind="codex",
        capability_id="qa-test",
        capabilities=frozenset({"memory_read"}),
    )


def _access_kwargs():
    return {"principal": _principal(), "narrowing": AccessNarrowing()}


class TestQuestionAnswerSearchInit:
    def test_init_default_uses_config_wiki_dir(self, tmp_path):
        cfg = Mock(wiki_dir=tmp_path)
        with patch("core.app.question_answer_search.ContextAwareSearch") as mock_cs:
            with patch("core.config.get_config", return_value=cfg):
                mock_cs.return_value = Mock()
                engine = QuestionAnswerSearch()
        assert engine.wiki_dir == tmp_path
        mock_cs.assert_called_once_with(wiki_base=str(tmp_path))

    def test_init_explicit_wiki(self, tmp_path):
        with patch("core.app.question_answer_search.ContextAwareSearch") as mock_cs:
            mock_cs.return_value = Mock()
            engine = QuestionAnswerSearch(wiki_dir=tmp_path)
            assert engine.wiki_dir == tmp_path


class TestExtractQuestionType:
    @pytest.fixture
    def engine(self, tmp_path):
        with patch("core.app.question_answer_search.ContextAwareSearch") as mock_cs:
            mock_cs.return_value = Mock()
            return QuestionAnswerSearch(wiki_dir=tmp_path)

    def test_definition(self, engine):
        assert engine.extract_question_type("什么是Python") == "definition"
        assert engine.extract_question_type("what is docker") == "definition"

    def test_causation(self, engine):
        assert engine.extract_question_type("为什么报错") == "causation"
        assert engine.extract_question_type("why failed") == "causation"

    def test_procedure(self, engine):
        assert engine.extract_question_type("如何安装") == "procedure"
        assert engine.extract_question_type("how to deploy") == "procedure"

    def test_entity(self, engine):
        assert engine.extract_question_type("谁是作者") == "entity"
        assert engine.extract_question_type("who wrote this") == "entity"

    def test_temporal(self, engine):
        assert engine.extract_question_type("什么时候发布") == "temporal"
        assert engine.extract_question_type("when released") == "temporal"

    def test_comparison(self, engine):
        assert engine.extract_question_type("A vs B") == "comparison"
        assert engine.extract_question_type("有什么区别") == "comparison"

    def test_general(self, engine):
        assert engine.extract_question_type("random query") == "general"


class TestNormalizeQuestion:
    @pytest.fixture
    def engine(self, tmp_path):
        with patch("core.app.question_answer_search.ContextAwareSearch") as mock_cs:
            mock_cs.return_value = Mock()
            return QuestionAnswerSearch(wiki_dir=tmp_path)

    def test_remove_stop_words(self, engine):
        result = engine._normalize_question("如何安装Python")
        assert "如何" not in result
        # "安装" 不是停用词，保留
        assert "安装" in result
        assert "python" in result.lower()

    def test_preserve_entities(self, engine):
        result = engine._normalize_question("什么是Kubernetes集群")
        assert "kubernetes" in result.lower()
        assert "集群" in result


class TestScoreParagraph:
    @pytest.fixture
    def engine(self, tmp_path):
        with patch("core.app.question_answer_search.ContextAwareSearch") as mock_cs:
            mock_cs.return_value = Mock()
            return QuestionAnswerSearch(wiki_dir=tmp_path)

    def test_definition_bonus(self, engine):
        para = {"text": "Python 是一种编程语言"}
        score = engine._score_paragraph(para, "什么是Python", "definition")
        assert score > 0.2

    def test_causation_bonus(self, engine):
        para = {"text": "因为配置错误导致服务响应变慢"}
        score = engine._score_paragraph(para, "为什么响应变慢", "causation")
        assert score > 0.2

    def test_procedure_bonus(self, engine):
        # 使用足够长、带标点、且含关键词的段落，避免 heading_penalty 和 length_factor 压分
        para = {"text": "部署服务的步骤如下：首先安装依赖，然后运行启动脚本。"}
        score = engine._score_paragraph(para, "如何部署服务", "procedure")
        assert score > 0.2

    def test_comparison_bonus(self, engine):
        para = {"text": "两者的架构选型不同"}
        score = engine._score_paragraph(para, "Kubernetes与Docker的架构", "comparison")
        assert score > 0.2

    def test_low_score_irrelevant(self, engine):
        para = {"text": "这是一段完全不相关的内容"}
        score = engine._score_paragraph(para, "xyzabc123", "general")
        assert score < 0.2

    def test_heading_penalty(self, engine):
        para = {"text": "## 结论"}
        score = engine._score_paragraph(para, "什么是结论", "definition")
        assert score < 0.3

    def test_code_bonus(self, engine):
        para = {"text": "使用 `docker run` 命令启动容器"}
        score = engine._score_paragraph(para, "如何使用docker", "procedure")
        assert score > 0.0


class TestSplitIntoParagraphs:
    @pytest.fixture
    def engine(self, tmp_path):
        with patch("core.app.question_answer_search.ContextAwareSearch") as mock_cs:
            mock_cs.return_value = Mock()
            return QuestionAnswerSearch(wiki_dir=tmp_path)

    def test_basic_split(self, engine):
        context = "### Title\n> 来源: page.md\n\nParagraph one.\n\nParagraph two."
        paras = engine._split_into_paragraphs(context)
        assert len(paras) >= 1
        assert paras[0]["source"] == "page.md"

    def test_skip_short_lines(self, engine):
        context = "### Title\n> 来源: page.md\n\nab\n\nLong enough paragraph here."
        paras = engine._split_into_paragraphs(context)
        assert all(len(p["text"]) > 8 for p in paras)

    def test_empty_context(self, engine):
        assert engine._split_into_paragraphs("") == []


class TestExtractAnswerSnippets:
    @pytest.fixture
    def engine(self, tmp_path):
        with patch("core.app.question_answer_search.ContextAwareSearch") as mock_cs:
            mock_cs.return_value = Mock()
            return QuestionAnswerSearch(wiki_dir=tmp_path)

    def test_extract_relevant(self, engine):
        context = "### Page\n> 来源: test.md\n\nPython is a programming language.\n\nDocker is a container platform."  # noqa: E501
        snippets = engine._extract_answer_snippets(context, "什么是Python", "definition")
        assert len(snippets) > 0
        assert any("python" in s["text"].lower() for s in snippets)

    def test_no_relevant_content(self, engine):
        context = "### Page\n> 来源: test.md\n\nSome unrelated text here."
        snippets = engine._extract_answer_snippets(context, "xyzabc123", "general")
        assert snippets == []

    def test_sorted_by_score(self, engine):
        context = (
            "### Page\n> 来源: test.md\n\nPython is great.\n\nPython is a programming language."
        )
        snippets = engine._extract_answer_snippets(context, "什么是Python", "definition")
        if len(snippets) >= 2:
            assert snippets[0]["score"] >= snippets[1]["score"]


class TestSearchResults:
    @pytest.fixture
    def engine(self, tmp_path):
        with patch("core.app.question_answer_search.ContextAwareSearch") as mock_cs:
            mock_cs.return_value = Mock()
            return QuestionAnswerSearch(wiki_dir=tmp_path)

    def test_with_search_method(self, engine):
        mock_result = SearchResult(
            page_path="test.md",
            title="Test",
            snippet="content",
            score=0.9,
        )
        engine.retriever = Mock()
        engine.retriever.search.return_value = [mock_result]
        results = engine._search_results("query", top_k=5, **_access_kwargs())
        assert len(results) == 1
        assert results[0].title == "Test"

    def test_with_assemble_fallback(self, engine):
        engine.retriever = Mock()
        del engine.retriever.search  # 没有 search 方法
        engine.retriever.assemble.return_value = "assembled context"
        results = engine._search_results("query", top_k=5, **_access_kwargs())
        assert results == []

    def test_empty_assemble(self, engine):
        engine.retriever = Mock()
        del engine.retriever.search
        engine.retriever.assemble.return_value = ""
        results = engine._search_results("query", top_k=5, **_access_kwargs())
        assert results == []


class TestResultsToContext:
    @pytest.fixture
    def engine(self, tmp_path):
        with patch("core.app.question_answer_search.ContextAwareSearch") as mock_cs:
            mock_cs.return_value = Mock()
            return QuestionAnswerSearch(wiki_dir=tmp_path)

    def test_results_to_context(self, engine):
        results = [
            SearchResult(
                page_path="test.md",
                title="Test Page",
                snippet="Snippet content",
                score=0.9,
            ),
        ]
        context = engine._results_to_context(results, **_access_kwargs())
        assert "Test Page" in context
        assert "test.md" in context
        assert "Snippet content" in context

    def test_results_to_context_reads_file(self, engine, tmp_path):
        page = tmp_path / "real.md"
        page.write_text("---\ntitle: Real\n---\n# Real\nBody content here.", encoding="utf-8")
        results = [
            SearchResult(
                page_path="real.md",
                title="Real",
                snippet="---\ntitle: Real\n---\nshort",  # 会被 frontmatter 截断
                score=0.9,
            ),
        ]
        engine.retriever.read_authorized_page.return_value = page.read_text(encoding="utf-8")
        context = engine._results_to_context(results, **_access_kwargs())
        assert "Body content here" in context


class TestExtractSolutionBlocks:
    @pytest.fixture
    def engine(self, tmp_path):
        with patch("core.app.question_answer_search.ContextAwareSearch") as mock_cs:
            mock_cs.return_value = Mock()
            return QuestionAnswerSearch(wiki_dir=tmp_path)

    def test_extract_from_heading(self, engine, tmp_path):
        page = tmp_path / "solution.md"
        page.write_text(
            "---\n---\n## 解决方案\n\n步骤一：执行命令\n步骤二：验证结果\n",
            encoding="utf-8",
        )
        engine.retriever.read_authorized_page.return_value = page.read_text(encoding="utf-8")
        blocks = engine._extract_solution_blocks("solution.md", "如何修复", **_access_kwargs())
        assert len(blocks) > 0

    def test_extract_commands(self, engine, tmp_path):
        page = tmp_path / "cmd.md"
        page.write_text(
            "---\n---\n## 操作\n\n`docker run -d nginx`\n`git pull`\n",
            encoding="utf-8",
        )
        engine.retriever.read_authorized_page.return_value = page.read_text(encoding="utf-8")
        blocks = engine._extract_solution_blocks("cmd.md", "如何操作", **_access_kwargs())
        assert any("docker" in b or "git" in b for b in blocks)

    def test_nonexistent_source(self, engine):
        engine.retriever.read_authorized_page.return_value = None
        blocks = engine._extract_solution_blocks("nonexistent.md", "query", **_access_kwargs())
        assert blocks == []

    def test_unknown_source(self, engine):
        blocks = engine._extract_solution_blocks("unknown", "query", **_access_kwargs())
        assert blocks == []


class TestSearch:
    @pytest.fixture
    def engine(self, tmp_path):
        with patch("core.app.question_answer_search.ContextAwareSearch") as mock_cs:
            mock_cs.return_value = Mock()
            return QuestionAnswerSearch(wiki_dir=tmp_path)

    def test_search_returns_results(self, engine):
        mock_result = SearchResult(
            page_path="test.md",
            title="Python",
            snippet="Python is a language.\nIt is popular.",
            score=0.95,
        )
        engine.retriever = Mock()
        engine.retriever.search.return_value = [mock_result]

        results = engine.search("什么是Python", **_access_kwargs())
        assert len(results) > 0
        assert "answer_snippet" in results[0]
        assert "confidence" in results[0]
        assert results[0]["question_type"] == "definition"

    def test_search_empty_results(self, engine):
        engine.retriever = Mock()
        engine.retriever.search.return_value = []
        results = engine.search("xyzabc123", **_access_kwargs())
        assert results == []

    def test_search_fallback(self, engine):
        mock_result = SearchResult(
            page_path="test.md",
            title="Python Programming",
            snippet="short",
            score=0.6,
        )
        engine.retriever = Mock()
        engine.retriever.search.return_value = [mock_result]
        results = engine.search("Python", **_access_kwargs())
        assert len(results) > 0
        assert "answer_snippet" in results[0]

    def test_search_top_k_limit(self, engine):
        mock_results = [
            SearchResult(
                page_path=f"p{i}.md",
                title=f"Page {i}",
                snippet=f"content {i}",
                score=0.9 - i * 0.05,
            )
            for i in range(10)
        ]
        engine.retriever = Mock()
        engine.retriever.search.return_value = mock_results
        results = engine.search("query", top_k=3, **_access_kwargs())
        assert len(results) <= 3


class TestAnswer:
    @pytest.fixture
    def engine(self, tmp_path):
        with patch("core.app.question_answer_search.ContextAwareSearch") as mock_cs:
            mock_cs.return_value = Mock()
            return QuestionAnswerSearch(wiki_dir=tmp_path)

    def test_answer_returns_dict(self, engine):
        mock_result = SearchResult(
            page_path="test.md",
            title="测试",
            snippet="测试是一种验证软件行为的方法。\nMore lines.",
            score=0.9,
        )
        engine.retriever = Mock()
        engine.retriever.search.return_value = [mock_result]

        result = engine.answer("什么是测试", **_access_kwargs())
        assert result is not None
        assert "answer" in result
        assert "confidence" in result
        assert "source" in result

    def test_answer_no_results(self, engine):
        engine.retriever = Mock()
        engine.retriever.search.return_value = []
        result = engine.answer("xyzabc123", **_access_kwargs())
        assert result is None

    def test_answer_procedure_with_blocks(self, engine, tmp_path):
        page = tmp_path / "proc.md"
        page.write_text(
            "---\n---\n## 解决方案\n\n步骤一：执行 A\n步骤二：执行 B\n",
            encoding="utf-8",
        )
        mock_result = SearchResult(
            page_path="proc.md",
            title="操作步骤",
            snippet="操作步骤如下。",
            score=0.9,
        )
        engine.retriever = Mock()
        engine.retriever.search.return_value = [mock_result]

        engine.retriever.read_authorized_page.return_value = page.read_text(encoding="utf-8")
        result = engine.answer("如何操作", **_access_kwargs())
        assert result is not None
        assert "answer" in result
        assert result["question_type"] == "procedure"


class TestAnswerMarkdown:
    @pytest.fixture
    def engine(self, tmp_path):
        with patch("core.app.question_answer_search.ContextAwareSearch") as mock_cs:
            mock_cs.return_value = Mock()
            return QuestionAnswerSearch(wiki_dir=tmp_path)
