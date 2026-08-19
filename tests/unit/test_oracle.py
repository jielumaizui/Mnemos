"""
WikiReader (Oracle) 单元测试

覆盖项：
- search_all_relevant 相关性排序（P2-18）
- _fallback_body_search 相关性排序
- 标题匹配优先于正文匹配
"""

import pytest
from pathlib import Path


def test_oracle_does_not_expose_stale_page_dto():
    """Oracle 对外契约是 Dict 读取结果，不暴露旧 WikiPage DTO。"""
    import integrations.oracle as oracle

    assert not hasattr(oracle, "OracleWikiPage")


class TestWikiReaderSearchSorting:
    """P2-18: wiki_search fallback 排序测试"""

    @pytest.fixture
    def reader(self, tmp_path):
        """构造一个带 mock index 的 WikiReader。"""
        from integrations.oracle import WikiReader

        reader = WikiReader.__new__(WikiReader)
        reader.wiki_path = tmp_path
        reader.metrics = None
        reader.index = {
            "03-Tech/python-async": {
                "title": "Python Async",
                "type": "03-Tech",
                "entities": ["asyncio", "coroutine"],
                "concepts": ["并发编程"],
                "heat_level": "hot",
                "heat_score": 80.0,
                "verification": "verified",
                "confidence": 0.9,
            },
            "03-Tech/python-basics": {
                "title": "Python 基础教程",
                "type": "03-Tech",
                "entities": ["python", "入门"],
                "concepts": [],
                "heat_level": "cold",
                "heat_score": 5.0,
                "verification": "verified",
                "confidence": 0.8,
            },
            "00-Inbox/untitled": {
                "title": "Untitled Note",
                "type": "00-Inbox",
                "entities": ["python"],
                "concepts": [],
                "heat_level": "cold",
                "heat_score": 0.0,
                "verification": "",
                "confidence": 0.5,
            },
        }
        for page_id, info in reader.index.items():
            page_path = tmp_path / f"{page_id}.md"
            page_path.parent.mkdir(parents=True, exist_ok=True)
            page_path.write_text(
                "---\n"
                "scope: public\n"
                "source_agent: codex\n"
                "acl_schema_version: 1\n"
                "acl_metadata_complete: true\n"
                "acl_reconciliation_status: proven\n"
                "---\n"
                f"{info['title']}\n",
                encoding="utf-8",
            )
            info["path"] = str(page_path)
        return reader

    def test_search_title_exact_match_first(self, reader):
        """标题精确匹配应排在最前（relevance_score 最高）。"""
        results = reader.search_all_relevant("python")
        assert len(results) >= 2
        # 标题含 "python" 的页面 relevance_score 应最高
        top = results[0]
        assert "python" in top["title"].lower() or "python" in top["page_id"].lower()
        assert top["relevance_score"] >= 10

    def test_search_relevance_over_heat(self, reader):
        """相关性高的冷页面应排在相关性低的热页面前。"""
        # python-basics: 标题含 python (score=20), heat=5
        # python-async: 实体含 python (score=15), heat=80
        results = reader.search_all_relevant("python")
        scores = [(r["page_id"], r["relevance_score"], r["heat_score"]) for r in results]
        # 按 relevance_score 降序
        relevance_scores = [s[1] for s in scores]
        assert relevance_scores == sorted(relevance_scores, reverse=True)

    def test_search_inbox_deprioritized(self, reader):
        """00-Inbox 页面即使匹配也应被降权。"""
        results = reader.search_all_relevant("python")
        inbox_items = [r for r in results if r.get("type") == "00-Inbox"]
        non_inbox = [r for r in results if r.get("type") != "00-Inbox"]
        if inbox_items and non_inbox:
            # Inbox 的 relevance_score 应被降权
            assert inbox_items[0]["relevance_score"] < non_inbox[0]["relevance_score"]

    def test_fallback_body_search_sorts_by_relevance(self, tmp_path):
        """_fallback_body_search 按 relevance_score 优先排序。"""
        from integrations.oracle import WikiReader

        reader = WikiReader.__new__(WikiReader)
        reader.wiki_path = tmp_path
        reader.metrics = None
        reader.index = {}

        # 创建测试文件
        (tmp_path / "03-Tech").mkdir(parents=True)
        (tmp_path / "03-Tech" / "hot-topic.md").write_text(
            "---\nname: Hot Topic\nconfidence: 0.9\nstatus: verified\n---\n\nSome content about python."  # noqa: E501
        )
        (tmp_path / "03-Tech" / "cold-topic.md").write_text(
            "---\nname: Cold Topic\nconfidence: 0.9\nstatus: verified\n---\n\nPython python python python python."  # noqa: E501
        )

        results = reader._fallback_body_search("python", limit=10)
        assert len(results) == 2
        # cold-topic 正文匹配次数更多，relevance_score 应更高
        assert results[0]["relevance_score"] >= results[1]["relevance_score"]

    def test_search_no_match_returns_empty(self, reader):
        """无匹配时返回空列表。"""
        results = reader.search_all_relevant("不存在的查询xyz123")
        assert results == []

    def test_search_limit_respected(self, reader):
        """search() 方法遵守 limit 参数。"""
        results = reader.search("python", limit=2)
        assert len(results) <= 2


class TestWikiReaderReadByDepth:
    """Phase 4 Wave 6: _read_by_depth 行为刻画测试。"""

    @pytest.fixture
    def page(self, tmp_path):
        """在临时 wiki 目录中创建一页内容并返回元信息。"""
        subdir = tmp_path / "03-Tech"
        subdir.mkdir(parents=True)
        file_path = subdir / "test-page.md"
        long_body = (
            "第一段关于 Python 异步编程的详细介绍。\n\n"
            "第二段关于并发模型的补充说明，包含更多技术细节。\n\n"
            "第三段关于性能优化的实践经验，持续展开以超过 100 字的摘要阈值。\n\n"
            "第四段关于测试与调试的具体建议，确保正文足够长以覆盖 paragraph 深度。"
        )
        file_path.write_text(
            "---\n"
            "name: 测试页面\n"
            "tags: [python, async]\n"
            "entities: [asyncio, python]\n"
            "concepts: [并发, 异步]\n"
            "source: test-source\n"
            "status: verified\n"
            "confidence: 0.85\n"
            "---\n\n"
            f"{long_body}\n",
            encoding="utf-8",
        )
        return {
            "page_id": "03-Tech/test-page",
            "file_path": file_path,
            "body": long_body,
        }

    @pytest.fixture
    def reader(self, page):
        """构造 WikiReader，index 只包含单页，metrics 置空。"""
        from integrations.oracle import WikiReader

        reader = WikiReader.__new__(WikiReader)
        reader.wiki_path = page["file_path"].parent.parent
        reader.metrics = None
        reader._index_cache_ttl = 60
        reader.index = {
            page["page_id"]: {
                "type": "03-Tech",
                "title": "测试页面",
                "entities": ["asyncio", "python"],
                "concepts": ["并发", "异步"],
                "path": page["file_path"],
                "heat_level": "cold",
                "heat_score": 5.0,
                "verification": "verified",
                "confidence": 0.85,
            }
        }
        return reader

    @pytest.fixture
    def reader_with_related(self, page, tmp_path):
        """构造包含关联页面的 WikiReader。"""
        from integrations.oracle import WikiReader

        subdir = tmp_path / "03-Tech"
        related_path = subdir / "related-page.md"
        related_path.write_text("---\nname: 关联页面\n---\n\nbody", encoding="utf-8")

        reader = WikiReader.__new__(WikiReader)
        reader.wiki_path = tmp_path
        reader.metrics = None
        reader._index_cache_ttl = 60
        reader.index = {
            page["page_id"]: {
                "type": "03-Tech",
                "title": "测试页面",
                "entities": ["asyncio"],
                "concepts": ["并发"],
                "path": page["file_path"],
                "heat_level": "hot",
                "heat_score": 80.0,
                "verification": "verified",
                "confidence": 0.85,
            },
            "03-Tech/related-page": {
                "type": "03-Tech",
                "title": "关联页面",
                "entities": ["asyncio"],
                "concepts": [],
                "path": related_path,
                "heat_level": "cold",
                "heat_score": 0.0,
                "verification": "",
                "confidence": 0.5,
            },
        }
        return reader

    def test_read_metadata_depth(self, reader, page):
        """metadata 深度返回元数据字段，不含 content/summary。"""
        result = reader._read_by_depth(page["page_id"], "metadata")
        assert result is not None
        assert result["title"] == "测试页面"
        assert result["tags"] == ["python", "async"]
        assert result["entities"] == ["asyncio", "python"]
        assert result["concepts"] == ["并发", "异步"]
        # 无 metrics 时 _record_heat 回退使用 index 条目，其无 "level" 键，
        # 因此 reported_heat_level 会保持为传入的 heat_level。
        assert result["heat_level"] == "metadata"
        assert result["heat_score"] == 5.0
        assert result["verification"] == "verified"
        assert result["confidence"] == 0.85
        assert result["source"] == "test-source"
        assert result["depth"] == "metadata_only"
        assert result["note"] == "沉睡知识，低活跃度，可唤醒"
        assert "content" not in result
        assert "summary" not in result
        assert "related" not in result

    def test_read_summary_depth(self, reader, page):
        """summary 深度返回截断摘要。"""
        result = reader._read_by_depth(page["page_id"], "cold")
        assert result is not None
        assert result["depth"] == "summary_100"
        assert result["summary"] == page["body"][:100] + "..."
        assert result["title"] == "测试页面"
        assert result["heat_level"] == "cold"
        assert "content" not in result
        assert "related" not in result

    def test_read_summary_without_truncation(self, reader, page, tmp_path):
        """正文不足阈值时不追加省略号。"""
        short_path = tmp_path / "03-Tech" / "short.md"
        short_path.parent.mkdir(parents=True, exist_ok=True)
        short_path.write_text("---\nname: 短页面\n---\n\n短正文。", encoding="utf-8")
        reader.index["03-Tech/short"] = {
            "type": "03-Tech",
            "title": "短页面",
            "entities": [],
            "concepts": [],
            "path": short_path,
            "heat_level": "cold",
            "heat_score": 0.0,
            "verification": "",
            "confidence": 0.5,
        }
        result = reader._read_by_depth("03-Tech/short", "cold")
        assert result["summary"] == "短正文。"
        assert not result["summary"].endswith("...")

    def test_read_paragraph_depth(self, reader, page):
        """paragraph 深度返回 500 字内容，默认不含 related。"""
        result = reader._read_by_depth(page["page_id"], "warm")
        assert result is not None
        assert result["depth"] == "paragraph_500"
        assert result["content"] == page["body"][:500]
        assert result["entities"] == ["asyncio", "python"]
        assert result["concepts"] == ["并发", "异步"]
        # 无 metrics 时 reported_heat_level 回退为传入的 heat_level。
        assert result["heat_level"] == "warm"
        assert "related" not in result

    def test_read_paragraph_with_related(self, reader_with_related, page):
        """当 READ_DEPTH 配置 related=True 时 paragraph 也会附加 related。"""
        reader_with_related.READ_DEPTH["warm"]["related"] = True
        try:
            result = reader_with_related._read_by_depth(page["page_id"], "warm")
            assert result is not None
            assert "related" in result
            assert len(result["related"]) <= 3
        finally:
            reader_with_related.READ_DEPTH["warm"]["related"] = False

    def test_read_full_depth(self, reader_with_related, page):
        """full 深度返回全文和 related。"""
        result = reader_with_related._read_by_depth(page["page_id"], "hot")
        assert result is not None
        assert result["depth"] == "full"
        assert result["content"] == page["body"]
        assert "related" in result
        assert len(result["related"]) <= 5
        assert result["related"][0]["type"] in ("entity_link", "concept_link")

    def test_get_knowledge_accepts_include_related_compat_flag(self, reader_with_related):
        """兼容 preflight 调用方传入的 _include_related 关键字参数。"""
        result = reader_with_related.get_knowledge("asyncio", _include_related=False)

        assert result["found"] is True
        assert result["query"] == "asyncio"
        assert result["total_pages"] >= 1
        assert result["context"]

    def test_read_full_plus_depth(self, reader_with_related, page):
        """full_plus 深度返回全文、related、deep_traced 和 note。"""
        # 原始 READ_DEPTH 中没有 full_plus，临时注入以刻画该分支。
        reader_with_related.READ_DEPTH["core"] = {
            "type": "full_plus",
            "chars": -1,
            "related": True,
            "deep": True,
            "desc": "核心知识，深度追踪",
        }
        try:
            result = reader_with_related._read_by_depth(page["page_id"], "core")
            assert result is not None
            assert result["depth"] == "full_plus"
            assert result["content"] == page["body"]
            assert result["deep_traced"] is True
            assert result["note"] == "核心知识，深度追踪"
            assert "related" in result
        finally:
            del reader_with_related.READ_DEPTH["core"]

    def test_read_unknown_heat_level_defaults_to_summary(self, reader, page):
        """未知 heat_level 回退到 cold/summary。"""
        result = reader._read_by_depth(page["page_id"], "nonexistent")
        assert result is not None
        assert result["depth"] == "summary_100"

    def test_read_missing_page_returns_none(self, reader):
        """page_id 不存在时返回 None。"""
        assert reader._read_by_depth("not/exist", "cold") is None

    def test_heat_info_update_changes_reported_level(self, page):
        """metrics 返回新的热力值时，index 与结果应同步更新。"""
        from integrations.oracle import WikiReader

        class FakeMetrics:
            def update_heat(self, page_id, access_type):
                pass

            def get_page(self, page_id):
                class FakePage:
                    heat_level = "hot"
                    heat_score = 99.0
                return FakePage()

        reader = WikiReader.__new__(WikiReader)
        reader.wiki_path = page["file_path"].parent.parent
        reader.metrics = FakeMetrics()
        reader._index_cache_ttl = 60
        reader.index = {
            page["page_id"]: {
                "type": "03-Tech",
                "title": "测试页面",
                "entities": [],
                "concepts": [],
                "path": page["file_path"],
                "heat_level": "cold",
                "heat_score": 5.0,
                "verification": "",
                "confidence": 0.5,
            }
        }

        result = reader._read_by_depth(page["page_id"], "cold")
        assert result["heat_level"] == "hot"
        assert result["heat_score"] == 99.0
        assert reader.index[page["page_id"]]["heat_level"] == "hot"
        assert reader.index[page["page_id"]]["heat_score"] == 99.0

    def test_read_by_depth_skips_unreadable_file(self, reader, page):
        """文件不可读时返回 None，不抛异常。"""
        reader.index[page["page_id"]]["path"] = Path("/nonexistent/path.md")
        assert reader._read_by_depth(page["page_id"], "cold") is None
