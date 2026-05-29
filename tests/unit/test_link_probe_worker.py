"""
link_probe_worker 单元测试

覆盖项：
- enqueue / enqueue_from_content 入队逻辑
- _is_external_url 内外部地址过滤
- probe_single 单链接探测（reachable / broken / timeout / retryable）
- probe_batch 批量探测与统计
- 指数退避重试（429/503）
- get_broken_links_for_page / get_pending_count / get_stats
- run_once 单次运行
- update_wiki_frontmatter frontmatter 反写
"""

import sys
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import unittest

with patch("core.hephaestus.link_probe_worker.get_config") as _mock_cfg:
    _mock_cfg.return_value.data_dir = Path(tempfile.gettempdir())
    from core.hephaestus.link_probe_worker import LinkProbeWorker, _is_external_url


class TestIsExternalUrl(unittest.TestCase):
    def test_http_is_external(self):
        self.assertTrue(_is_external_url("http://example.com/page"))

    def test_https_is_external(self):
        self.assertTrue(_is_external_url("https://redis.io/docs"))

    def test_localhost_is_internal(self):
        self.assertFalse(_is_external_url("http://localhost:8080"))

    def test_127_is_internal(self):
        self.assertFalse(_is_external_url("http://127.0.0.1/api"))

    def test_private_ip_is_internal(self):
        self.assertFalse(_is_external_url("http://192.168.1.1/home"))
        self.assertFalse(_is_external_url("http://10.0.0.1/x"))

    def test_file_scheme_is_internal(self):
        self.assertFalse(_is_external_url("file:///etc/passwd"))

    def test_malformed_returns_false(self):
        self.assertFalse(_is_external_url("not-a-url"))


class TestEnqueue(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.worker = LinkProbeWorker(db_path=str(Path(self.tmpdir.name) / "links.db"))

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_enqueue_external_url(self):
        """外部 URL 成功入队"""
        ok = self.worker.enqueue("https://example.com", "wiki/Tech.md")
        self.assertTrue(ok)
        self.assertEqual(self.worker.get_pending_count(), 1)

    def test_skip_internal_url(self):
        """内部地址不入队"""
        ok = self.worker.enqueue("http://localhost:8080", "wiki/Tech.md")
        self.assertFalse(ok)
        self.assertEqual(self.worker.get_pending_count(), 0)

    def test_enqueue_from_content(self):
        """从内容中提取多个 URL"""
        content = "See https://a.com and https://b.com/page"
        count = self.worker.enqueue_from_content(content, "wiki/Tech.md")
        self.assertEqual(count, 2)
        self.assertEqual(self.worker.get_pending_count(), 2)

    def test_enqueue_deduplicated(self):
        """相同 URL + 相同页面去重"""
        self.worker.enqueue("https://example.com", "wiki/Tech.md")
        self.worker.enqueue("https://example.com", "wiki/Tech.md")
        self.assertEqual(self.worker.get_pending_count(), 1)

    def test_enqueue_same_url_different_page(self):
        """相同 URL 不同页面可以重复入队"""
        self.worker.enqueue("https://example.com", "wiki/A.md")
        self.worker.enqueue("https://example.com", "wiki/B.md")
        self.assertEqual(self.worker.get_pending_count(), 2)


class TestProbeSingle(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.worker = LinkProbeWorker(db_path=str(Path(self.tmpdir.name) / "links.db"))

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_reachable_url(self):
        """可访问链接返回 reachable"""
        mock_resp = Mock()
        mock_resp.status_code = 200
        with patch.object(self.worker._session, "head", return_value=mock_resp):
            status, http_status, err = self.worker.probe_single("https://example.com")
        self.assertEqual(status, "reachable")
        self.assertEqual(http_status, 200)
        self.assertIsNone(err)

    def test_405_fallback_to_get(self):
        """405 时回退到 GET"""
        head_resp = Mock()
        head_resp.status_code = 405
        get_resp = Mock()
        get_resp.status_code = 200
        with patch.object(self.worker._session, "head", return_value=head_resp):
            with patch.object(self.worker._session, "get", return_value=get_resp) as mock_get:
                status, http_status, err = self.worker.probe_single("https://example.com")
        self.assertEqual(status, "reachable")
        mock_get.assert_called_once()

    def test_broken_404(self):
        """404 返回 broken"""
        mock_resp = Mock()
        mock_resp.status_code = 404
        with patch.object(self.worker._session, "head", return_value=mock_resp):
            status, http_status, err = self.worker.probe_single("https://example.com/missing")
        self.assertEqual(status, "broken")
        self.assertEqual(http_status, 404)

    def test_retryable_429(self):
        """429 返回 retryable"""
        mock_resp = Mock()
        mock_resp.status_code = 429
        with patch.object(self.worker._session, "head", return_value=mock_resp):
            status, http_status, err = self.worker.probe_single("https://example.com")
        self.assertEqual(status, "retryable")
        self.assertEqual(http_status, 429)

    def test_timeout(self):
        """超时返回 timeout"""
        import requests
        with patch.object(self.worker._session, "head", side_effect=requests.Timeout):
            status, http_status, err = self.worker.probe_single("https://slow.com")
        self.assertEqual(status, "timeout")
        self.assertIsNone(http_status)
        self.assertIn("超时", err)

    def test_connection_error(self):
        """连接失败返回 broken"""
        import requests
        with patch.object(self.worker._session, "head", side_effect=requests.ConnectionError("refused")):
            status, http_status, err = self.worker.probe_single("https://down.com")
        self.assertEqual(status, "broken")


class TestProbeBatch(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.worker = LinkProbeWorker(db_path=str(Path(self.tmpdir.name) / "links.db"))

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_batch_probes_and_updates_db(self):
        """批量探测并更新数据库状态"""
        self.worker.enqueue("https://a.com", "wiki/A.md")
        self.worker.enqueue("https://b.com", "wiki/B.md")

        responses = [
            Mock(status_code=200),
            Mock(status_code=404),
        ]
        with patch.object(self.worker._session, "head", side_effect=responses):
            stats = self.worker.probe_batch(batch_size=10)

        self.assertEqual(stats["probed"], 2)
        self.assertEqual(stats["reachable"], 1)
        self.assertEqual(stats["broken"], 1)
        # DB 状态已更新
        db_stats = self.worker.get_stats()
        self.assertEqual(db_stats.get("reachable", 0), 1)
        self.assertEqual(db_stats.get("broken", 0), 1)

    def test_retryable_increments_retry_count(self):
        """retryable 状态增加 retry_count，达到 max_retries 后跳过"""
        self.worker.enqueue("https://rate-limited.com", "wiki/X.md")

        # 连续探测 3 次（max_retries=3），前 3 次都被选中
        for i in range(3):
            with patch.object(self.worker._session, "head", return_value=Mock(status_code=503)):
                stats = self.worker.probe_batch(batch_size=10)
            self.assertEqual(stats["probed"], 1, f"第 {i+1} 次应被探测")
            self.assertEqual(stats["retryable"], 1)

        # 第 4 次：retry_count >= max_retries，被跳过
        with patch.object(self.worker._session, "head", return_value=Mock(status_code=503)):
            stats = self.worker.probe_batch(batch_size=10)
        self.assertEqual(stats["probed"], 0)

    def test_respects_batch_size(self):
        """batch_size 限制探测数量"""
        for i in range(10):
            self.worker.enqueue(f"https://site{i}.com", "wiki/X.md")

        with patch.object(self.worker._session, "head", return_value=Mock(status_code=200)):
            stats = self.worker.probe_batch(batch_size=3)

        self.assertEqual(stats["probed"], 3)
        self.assertEqual(self.worker.get_pending_count(), 7)


class TestQueryInterface(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.worker = LinkProbeWorker(db_path=str(Path(self.tmpdir.name) / "links.db"))

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_get_broken_links_for_page(self):
        """按页面查询失效链接"""
        self.worker.enqueue("https://broken.com", "wiki/Tech.md")
        # 手动模拟探测结果（直接改 DB）
        import sqlite3
        with sqlite3.connect(str(self.worker.db_path)) as conn:
            conn.execute(
                "UPDATE link_probe_queue SET status='broken', http_status=404, probe_error='Not Found', last_probed='2024-01-01T00:00:00' WHERE url=?",
                ("https://broken.com",),
            )
            conn.commit()

        broken = self.worker.get_broken_links_for_page("wiki/Tech.md")
        self.assertEqual(len(broken), 1)
        self.assertEqual(broken[0]["url"], "https://broken.com")
        self.assertEqual(broken[0]["http_status"], 404)

    def test_get_stats(self):
        """统计各状态数量"""
        self.worker.enqueue("https://a.com", "wiki/A.md")
        self.worker.enqueue("https://b.com", "wiki/B.md")
        stats = self.worker.get_stats()
        self.assertEqual(stats.get("pending", 0), 2)


class TestRunOnce(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.worker = LinkProbeWorker(db_path=str(Path(self.tmpdir.name) / "links.db"))

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_run_once_no_pending(self):
        """无待探测链接时返回 note"""
        result = self.worker.run_once()
        self.assertEqual(result["probed"], 0)
        self.assertEqual(result["note"], "no_pending")

    def test_run_once_probes(self):
        """有待探测链接时执行批次"""
        self.worker.enqueue("https://example.com", "wiki/X.md")
        with patch.object(self.worker._session, "head", return_value=Mock(status_code=200)):
            result = self.worker.run_once()
        self.assertEqual(result["probed"], 1)


class TestUpdateWikiFrontmatter(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.worker = LinkProbeWorker(db_path=str(Path(self.tmpdir.name) / "links.db"))

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_updates_frontmatter_with_broken_links(self):
        """将失效链接写入 wiki 页面 frontmatter"""
        wiki_file = Path(self.tmpdir.name) / "Redis.md"
        wiki_file.write_text("""---
title: Redis
tags: ["redis"]
---

# Redis

See https://redis.io/docs
""", encoding="utf-8")

        # 手动插入 broken 记录
        import sqlite3
        with sqlite3.connect(str(self.worker.db_path)) as conn:
            conn.execute("""
                INSERT INTO link_probe_queue (url, page_path, status, http_status, probe_error, last_probed)
                VALUES (?, ?, 'broken', 404, 'Not Found', '2024-01-01T00:00:00')
            """, ("https://redis.io/docs", str(wiki_file)))
            conn.commit()

        ok = self.worker.update_wiki_frontmatter(str(wiki_file))
        self.assertTrue(ok)
        updated = wiki_file.read_text(encoding="utf-8")
        self.assertIn("失效链接", updated)
        self.assertNotIn("broken_links", updated)
        self.assertIn("https://redis.io/docs", updated)

    def test_noop_when_no_broken_links(self):
        """无失效链接时不修改文件"""
        wiki_file = Path(self.tmpdir.name) / "Clean.md"
        wiki_file.write_text("# Clean\n", encoding="utf-8")
        ok = self.worker.update_wiki_frontmatter(str(wiki_file))
        self.assertFalse(ok)

    def test_noop_when_file_missing(self):
        """文件不存在返回 False"""
        ok = self.worker.update_wiki_frontmatter("/nonexistent/page.md")
        self.assertFalse(ok)


class TestDistillSelfCheckIntegration(unittest.TestCase):
    """DistillSelfCheck 与 LinkProbeWorker 的衔接测试"""

    def test_self_check_enqueues_url_when_probe_worker_given(self):
        """传入 LinkProbeWorker 时，URL 会被 enqueue"""
        from core.hephaestus.distillation_engine import DistillSelfCheck, KnowledgeFragment

        tmpdir = tempfile.TemporaryDirectory()
        worker = LinkProbeWorker(db_path=str(Path(tmpdir.name) / "links.db"))
        checker = DistillSelfCheck(link_probe_worker=worker)

        frag = KnowledgeFragment(
            form="decision",
            title="Test",
            frontmatter={},
            background="",
            core_content="参考 https://redis.io/docs/latest/ 进行配置",
            boundaries={},
            anti_patterns=[],
            related_concepts=[],
        )
        passed, issues = checker.check([frag], [])
        self.assertEqual(worker.get_pending_count(), 1)
        tmpdir.cleanup()

    def test_self_check_does_not_enqueue_without_probe_worker(self):
        """不传入 LinkProbeWorker 时，URL 不入队"""
        from core.hephaestus.distillation_engine import DistillSelfCheck, KnowledgeFragment

        tmpdir = tempfile.TemporaryDirectory()
        worker = LinkProbeWorker(db_path=str(Path(tmpdir.name) / "links.db"))
        checker = DistillSelfCheck()  # 不传入 link_probe_worker

        frag = KnowledgeFragment(
            form="decision",
            title="Test",
            frontmatter={},
            background="",
            core_content="参考 https://redis.io/docs/latest/ 进行配置",
            boundaries={},
            anti_patterns=[],
            related_concepts=[],
        )
        passed, issues = checker.check([frag], [])
        self.assertEqual(worker.get_pending_count(), 0)
        tmpdir.cleanup()


if __name__ == "__main__":
    unittest.main()
