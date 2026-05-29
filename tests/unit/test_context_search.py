import os
from datetime import datetime, timedelta


def _write_page(path, frontmatter="", body="# Redis Pitfall\nRedis 连接池踩坑：不要在每个请求里新建连接。\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n{body}", encoding="utf-8")


def test_context_search_supports_chinese_full_text_and_freshness_alert(tmp_path):
    from core.app.context_search import ContextAwareSearch

    old = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d")
    page = tmp_path / "03-Tech" / "redis.md"
    _write_page(page, f"时效性: 上下文相关\n修改日期: {old}\n置信度: 0.9\n")

    result = ContextAwareSearch(wiki_base=str(tmp_path)).search("Redis 连接池踩坑", limit=1)[0]

    assert os.path.normpath(result.page_path) == os.path.normpath("03-Tech/redis.md")
    assert result.final_score == result.score
    assert "关键词匹配" in result.match_reason
    assert result.freshness_alert.type == "potentially_stale"


def test_context_search_excludes_archive_dirs(tmp_path):
    from core.app.context_search import ContextAwareSearch

    _write_page(tmp_path / "99-Archive" / "old.md", body="Redis 连接池踩坑")

    assert ContextAwareSearch(wiki_base=str(tmp_path)).search("Redis 连接池踩坑") == []


def test_question_answer_search_reuses_context_aware_search(tmp_path):
    from core.app.question_answer_search import QuestionAnswerSearch

    _write_page(tmp_path / "03-Tech" / "redis.md", body="# Redis\n步骤：首先复用连接池，然后设置超时。")

    answer = QuestionAnswerSearch(wiki_dir=tmp_path).answer("如何处理 Redis 连接池？")

    assert answer is not None
    assert answer["question_type"] == "procedure"
    assert "连接池" in answer["answer"]


def test_question_answer_markdown_formats_results(tmp_path):
    from core.app.question_answer_search import QuestionAnswerSearch

    _write_page(tmp_path / "03-Tech" / "redis.md", body="# Redis\nRedis 是一个内存数据库。")

    markdown = QuestionAnswerSearch(wiki_dir=tmp_path).answer_markdown("什么是 Redis？")

    assert markdown.startswith("根据你的知识库")
    assert "Redis" in markdown
