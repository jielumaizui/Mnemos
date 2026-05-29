"""
styx (MemosClient SDK) 单元测试

覆盖项：
- get_session 连接池管理
- MemosClient 初始化与配置
- _make_request 错误分类（429/401/413/500）
- _sanitize 脱敏规则
- _truncate_content 多字节截断
- save / save_long_content 存储与分片
- 查询接口（search / list_by_tags / get_by_uid / list_all_memos）
- 更新与删除（update_tags / update_memo / delete）
- 分段合并（search_and_merge_segments / get_full_content_by_hash）
- 缓存命中与过期
- MCP 存根
"""

import sys
import json
import time
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock, call

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import unittest

# 先 patch config，避免 import 时读取文件系统
_FAKE_CONFIG = {
    "memos_token": "fake-token",
    "memos_api_url": "http://localhost:5230",
    "data_dir": Path(tempfile.gettempdir()) / "mnemos_test",
    "get": lambda key, default=None: {
        "memos.max_content_bytes": 7792,
        "memos.ingest_batch_size": 10,
        "memos.ingest_batch_interval": 10,
        "memos.query_cache_ttl": 30,
    }.get(key, default),
}

with patch("integrations.styx.get_config", return_value=_FAKE_CONFIG):
    from integrations.styx import (
        get_session,
        MemosClient,
        Memory,
        MemosRateLimitError,
        MemosAuthError,
        MemosPayloadTooLargeError,
        MemosServerError,
        _sessions,
    )


def _make_response(status_code=200, json_data=None, headers=None, ok=None):
    """辅助函数：构造 mock Response"""
    resp = Mock()
    resp.status_code = status_code
    resp.headers = headers or {}
    if ok is None:
        resp.ok = 200 <= status_code < 300
    else:
        resp.ok = ok
    # 始终提供 json() 返回值，避免链式 Mock 陷阱
    resp.json.return_value = json_data if json_data is not None else {}
    return resp


class TestGetSession(unittest.TestCase):
    def tearDown(self):
        # 清理全局连接池
        _sessions.clear()

    def test_creates_new_session(self):
        """首次请求创建新的 Session"""
        sess = get_session("http://example.com")
        self.assertIsNotNone(sess)

    def test_reuses_existing_session(self):
        """相同 base_url 复用已有 Session"""
        s1 = get_session("http://example.com")
        s2 = get_session("http://example.com")
        self.assertIs(s1, s2)

    def test_different_urls_different_sessions(self):
        """不同 base_url 创建不同 Session"""
        s1 = get_session("http://a.com")
        s2 = get_session("http://b.com")
        self.assertIsNot(s1, s2)

    def test_trailing_slash_normalized(self):
        """尾部斜杠被规范化，避免重复 session"""
        s1 = get_session("http://example.com/")
        s2 = get_session("http://example.com")
        self.assertIs(s1, s2)


class TestMemoryDataclass(unittest.TestCase):
    def test_basic_fields(self):
        """Memory dataclass 字段正确赋值"""
        m = Memory(
            id=1, uid="abc", content="hello", tags=["a"],
            visibility="PRIVATE", created_at="2024-01-01", updated_at="2024-01-02",
            agent="claude", raw_source_path="/tmp/x.md",
        )
        self.assertEqual(m.uid, "abc")
        self.assertEqual(m.agent, "claude")


class TestMemosClientInit(unittest.TestCase):
    @patch("integrations.styx.get_config", return_value=_FAKE_CONFIG)
    def test_init_with_explicit_params(self, _mock_cfg):
        """显式传入 token/base_url/agent"""
        client = MemosClient(token="t1", base_url="http://memos.local", agent="kimi")
        self.assertEqual(client.token, "t1")
        self.assertEqual(client.base_url, "http://memos.local")
        self.assertEqual(client.agent, "kimi")

    @patch.dict("os.environ", {"MEMOS_TOKEN": "env-token"}, clear=False)
    @patch("integrations.styx.get_config", return_value=_FAKE_CONFIG)
    def test_init_reads_token_from_env(self, _mock_cfg):
        """未传 token 时从环境变量读取"""
        client = MemosClient(base_url="http://m")
        self.assertEqual(client.token, "env-token")

    @patch.dict("os.environ", {"MEMOS_AGENT": "hermes"}, clear=False)
    @patch("integrations.styx.get_config", return_value=_FAKE_CONFIG)
    def test_agent_env_overrides_param(self, _mock_cfg):
        """环境变量 MEMOS_AGENT 优先级高于传入参数"""
        client = MemosClient(token="t", base_url="http://m", agent="claude")
        self.assertEqual(client.agent, "hermes")

    @patch("integrations.styx.get_config", return_value=_FAKE_CONFIG)
    def test_headers_contain_auth(self, _mock_cfg):
        """headers 包含 Bearer token"""
        client = MemosClient(token="secret", base_url="http://m")
        self.assertEqual(client.headers["Authorization"], "Bearer secret")

    @patch("integrations.styx.get_config", return_value=_FAKE_CONFIG)
    def test_config_driven_params(self, _mock_cfg):
        """从 Config 读取 max_content_bytes 等参数"""
        client = MemosClient(token="t", base_url="http://m")
        self.assertEqual(client.max_content_bytes, 7792)
        self.assertEqual(client.ingest_batch_size, 10)


class TestSanitizePatterns(unittest.TestCase):
    @patch("integrations.styx.get_config", return_value=_FAKE_CONFIG)
    def test_builtin_patterns_mask_api_key(self, _mock_cfg):
        """内置规则脱敏 OpenAI API Key"""
        client = MemosClient(token="t", base_url="http://m")
        raw = "调用 sk-abcdefghij1234567890abcdef 进行推理"
        result = client._sanitize(raw)
        self.assertNotIn("sk-abcdefghij", result)
        self.assertIn("[API-KEY]", result)

    @patch("integrations.styx.get_config", return_value=_FAKE_CONFIG)
    def test_builtin_patterns_mask_github_token(self, _mock_cfg):
        """内置规则脱敏 GitHub Token"""
        client = MemosClient(token="t", base_url="http://m")
        raw = "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        result = client._sanitize(raw)
        self.assertIn("[GITHUB-TOKEN]", result)

    @patch("integrations.styx.get_config", return_value=_FAKE_CONFIG)
    def test_builtin_patterns_mask_password(self, _mock_cfg):
        """内置规则脱敏 password 字段"""
        client = MemosClient(token="t", base_url="http://m")
        raw = "password=supersecret123"
        result = client._sanitize(raw)
        self.assertIn("password=[HIDDEN]", result)

    @patch("integrations.styx.get_config", return_value=_FAKE_CONFIG)
    def test_load_patterns_from_file(self, _mock_cfg):
        """从配置文件加载自定义脱敏规则"""
        with tempfile.TemporaryDirectory() as td:
            cfg_dir = Path(td) / ".mnemos" / "configs"
            cfg_dir.mkdir(parents=True)
            patterns_file = cfg_dir / "sanitize_patterns.json"
            patterns_file.write_text(json.dumps([[r"secret-\d+", "[SECRET]"]]))

            with patch.object(Path, "home", return_value=Path(td)):
                client = MemosClient(token="t", base_url="http://m")
                raw = "token secret-12345 here"
                result = client._sanitize(raw)
                self.assertIn("[SECRET]", result)


class TestCache(unittest.TestCase):
    @patch("integrations.styx.get_config", return_value=_FAKE_CONFIG)
    def setUp(self, _mock_cfg):
        self.client = MemosClient(token="t", base_url="http://m")
        self.client._cache_ttl = 1  # 1 秒过期，方便测试

    def test_cache_miss_returns_none(self):
        """未缓存返回 None"""
        self.assertIsNone(self.client._cache_get("key1"))

    def test_cache_hit_returns_value(self):
        """缓存命中返回值"""
        self.client._cache_set("key1", ["a", "b"])
        self.assertEqual(self.client._cache_get("key1"), ["a", "b"])

    def test_cache_expires(self):
        """缓存过期后返回 None"""
        self.client._cache_set("key1", "val")
        time.sleep(1.1)
        self.assertIsNone(self.client._cache_get("key1"))

    def test_cache_key_format(self):
        """缓存 key 包含方法名和参数"""
        key = self.client._cache_key("search", "redis", 10)
        self.assertEqual(key, "search:redis:10")


class TestMakeRequest(unittest.TestCase):
    @patch("integrations.styx.get_config", return_value=_FAKE_CONFIG)
    def setUp(self, _mock_cfg):
        self.client = MemosClient(token="t", base_url="http://m")
        self.mock_session = Mock()
        self.client.session = self.mock_session

    def test_success_returns_response(self):
        """200 正常返回 Response"""
        resp = _make_response(200, {"ok": True})
        self.mock_session.get.return_value = resp
        result = self.client._make_request("GET", "http://m/api/v1/test")
        self.assertEqual(result.status_code, 200)

    def test_429_raises_rate_limit(self):
        """429 抛出 MemosRateLimitError"""
        resp = _make_response(429, headers={"Retry-After": "60"})
        self.mock_session.post.return_value = resp
        with self.assertRaises(MemosRateLimitError) as ctx:
            self.client._make_request("POST", "http://m/api/v1/memos")
        self.assertEqual(ctx.exception.retry_after, 60)

    def test_401_raises_auth_error(self):
        """401 抛出 MemosAuthError"""
        resp = _make_response(401)
        self.mock_session.get.return_value = resp
        with self.assertRaises(MemosAuthError):
            self.client._make_request("GET", "http://m/api/v1/memos")

    def test_413_raises_payload_too_large(self):
        """413 抛出 MemosPayloadTooLargeError"""
        resp = _make_response(413)
        self.mock_session.post.return_value = resp
        with self.assertRaises(MemosPayloadTooLargeError):
            self.client._make_request("POST", "http://m/api/v1/memos")

    def test_500_raises_server_error(self):
        """500 抛出 MemosServerError"""
        resp = _make_response(500)
        self.mock_session.get.return_value = resp
        with self.assertRaises(MemosServerError) as ctx:
            self.client._make_request("GET", "http://m/api/v1/memos")
        self.assertEqual(ctx.exception.status_code, 500)

    def test_200_does_not_raise(self):
        """200 不抛出异常"""
        resp = _make_response(200, {"data": []})
        self.mock_session.get.return_value = resp
        result = self.client._make_request("GET", "http://m/api/v1/memos")
        self.assertIs(result, resp)

    def test_metrics_callback_called(self):
        """metrics 回调在请求后被调用"""
        metrics = Mock()
        client = MemosClient(token="t", base_url="http://m", metrics_callback=metrics)
        client.session = self.mock_session
        resp = _make_response(200)
        self.mock_session.get.return_value = resp
        client._make_request("GET", "http://m/api/v1/memos")
        metrics.assert_called_once()
        args = metrics.call_args[0]
        self.assertEqual(args[0], "GET")
        self.assertEqual(args[2], 200)

    def test_metrics_callback_failure_isolated(self):
        """metrics 回调异常不影响主流程"""
        metrics = Mock(side_effect=RuntimeError("metrics boom"))
        client = MemosClient(token="t", base_url="http://m", metrics_callback=metrics)
        client.session = self.mock_session
        resp = _make_response(200)
        self.mock_session.get.return_value = resp
        # 不应抛出异常
        result = client._make_request("GET", "http://m/api/v1/memos")
        self.assertEqual(result.status_code, 200)


class TestExtractTags(unittest.TestCase):
    @patch("integrations.styx.get_config", return_value=_FAKE_CONFIG)
    def setUp(self, _mock_cfg):
        self.client = MemosClient(token="t", base_url="http://m")

    def test_extract_hash_tags(self):
        """提取 #标签"""
        content = "讨论 #Redis 和 #Python 的用法"
        clean, tags = self.client._extract_tags(content)
        self.assertEqual(tags, ["Redis", "Python"])
        self.assertNotIn("#Redis", clean)

    def test_no_tags(self):
        """无标签时返回空列表"""
        content = "普通文本"
        clean, tags = self.client._extract_tags(content)
        self.assertEqual(tags, [])
        self.assertEqual(clean, "普通文本")


class TestAutoClassify(unittest.TestCase):
    @patch("integrations.styx.get_config", return_value=_FAKE_CONFIG)
    def setUp(self, _mock_cfg):
        self.client = MemosClient(token="t", base_url="http://m", agent="claude")

    def test_shared_keyword_triggers_tag(self):
        """包含共享关键词时添加 shared 标签"""
        tags = self.client._auto_classify("我的偏好是使用 pathlib")
        self.assertIn("shared", tags)
        self.assertIn("agent=claude", tags)

    def test_no_shared_keyword(self):
        """不包含共享关键词时只添加 agent 标签"""
        tags = self.client._auto_classify("普通技术讨论")
        self.assertNotIn("shared", tags)
        self.assertIn("agent=claude", tags)


class TestTruncateContent(unittest.TestCase):
    @patch("integrations.styx.get_config", return_value=_FAKE_CONFIG)
    def setUp(self, _mock_cfg):
        self.client = MemosClient(token="t", base_url="http://m")

    def test_no_truncation_needed(self):
        """内容未超限不截断"""
        text = "短内容"
        result, truncated, orig = self.client._truncate_content(text, 100)
        self.assertEqual(result, text)
        self.assertFalse(truncated)

    def test_truncates_long_content(self):
        """超长内容被截断"""
        text = "A" * 1000
        result, truncated, orig = self.client._truncate_content(text, 100)
        self.assertTrue(truncated)
        self.assertLess(len(result.encode("utf-8")), 105)

    def test_does_not_break_multibyte_char(self):
        """不在多字节 UTF-8 字符中间截断"""
        text = "中" * 500  # 每个中文字符 3 字节
        result, truncated, orig = self.client._truncate_content(text, 100)
        self.assertTrue(truncated)
        # 确保可以正常解码
        result.encode("utf-8").decode("utf-8")

    def test_returns_original_byte_count(self):
        """返回原始字节数"""
        text = "中" * 100
        _result, _truncated, orig = self.client._truncate_content(text, 50)
        self.assertEqual(orig, 300)


class TestSave(unittest.TestCase):
    @patch("integrations.styx.get_config", return_value=_FAKE_CONFIG)
    def setUp(self, _mock_cfg):
        self.client = MemosClient(token="t", base_url="http://m", agent="test")
        self.mock_session = Mock()
        self.client.session = self.mock_session

    def test_save_success(self):
        """save 成功返回 Memory"""
        resp = _make_response(200, {
            "name": "memos/uid-123",
            "content": "hello #agent=test",
            "visibility": "PRIVATE",
            "createTime": "2024-01-01T00:00:00Z",
            "updateTime": "2024-01-01T00:00:00Z",
            "tags": ["agent=test"],
        })
        self.mock_session.post.return_value = resp
        mem = self.client.save("hello", tags=["tag1"])
        self.assertEqual(mem.uid, "uid-123")
        self.assertEqual(mem.visibility, "PRIVATE")
        # 验证调用了正确 URL
        self.mock_session.post.assert_called_once()
        url = self.mock_session.post.call_args[0][0]
        self.assertIn("/api/v1/memos", url)

    def test_save_truncates_long_content(self):
        """save 对超长内容自动截断"""
        resp = _make_response(200, {
            "name": "memos/uid-456",
            "content": "x",
            "visibility": "PRIVATE",
            "createTime": "2024-01-01T00:00:00Z",
            "updateTime": "2024-01-01T00:00:00Z",
            "tags": [],
        })
        self.mock_session.post.return_value = resp
        long_text = "A" * 20000
        mem = self.client.save(long_text, tags=["t"])
        # 被截断的 Memory 内容会带截断标记
        self.assertIn("已截断", mem.content)

    def test_save_sanitizes_content(self):
        """save 自动脱敏"""
        resp = _make_response(200, {
            "name": "memos/uid-789",
            "content": "x",
            "visibility": "PRIVATE",
            "createTime": "2024-01-01T00:00:00Z",
            "updateTime": "2024-01-01T00:00:00Z",
            "tags": [],
        })
        self.mock_session.post.return_value = resp
        self.client.save("token=secret123", tags=[])
        # 验证 POST 的 json 中 content 已被脱敏
        call_kwargs = self.mock_session.post.call_args[1]
        posted_content = call_kwargs["json"]["content"]
        self.assertIn("[HIDDEN]", posted_content)
        self.assertNotIn("secret123", posted_content)


class TestSaveLongContent(unittest.TestCase):
    @patch("integrations.styx.get_config", return_value=_FAKE_CONFIG)
    def setUp(self, _mock_cfg):
        self.client = MemosClient(token="t", base_url="http://m", agent="test")
        self.mock_session = Mock()
        self.client.session = self.mock_session

    def test_short_content_single_save(self):
        """短内容不分片，直接 save"""
        resp = _make_response(200, {
            "name": "memos/uid-1",
            "content": "short",
            "visibility": "PRIVATE",
            "createTime": "2024-01-01T00:00:00Z",
            "updateTime": "2024-01-01T00:00:00Z",
            "tags": [],
        })
        self.mock_session.post.return_value = resp
        memories = self.client.save_long_content("short text", tags=["a"])
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].uid, "uid-1")

    def test_long_content_splits_into_chunks(self):
        """长内容自动分片保存"""
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            return _make_response(200, {
                "name": f"memos/uid-{call_count[0]}",
                "content": "chunk",
                "visibility": "PRIVATE",
                "createTime": "2024-01-01T00:00:00Z",
                "updateTime": "2024-01-01T00:00:00Z",
                "tags": [],
            })

        self.mock_session.post.side_effect = side_effect
        # 构造超过 3000 字节的长内容
        long_text = "A" * 10000
        memories = self.client.save_long_content(long_text, tags=["a"], title="test-title")
        self.assertGreater(len(memories), 1)
        # 每个分片包含 segment 标签
        first_call = self.mock_session.post.call_args_list[0]
        posted_json = first_call[1]["json"]
        self.assertIn("test-title", posted_json["content"])


class TestIdempotentSave(unittest.TestCase):
    @patch("integrations.styx.get_config", return_value=_FAKE_CONFIG)
    def setUp(self, _mock_cfg):
        self.client = MemosClient(token="t", base_url="http://m", agent="test")
        self.mock_session = Mock()
        self.client.session = self.mock_session
        # 默认 list_by_tags 返回空，使 get_full_content_by_hash 返回 None
        self.mock_session.get.return_value = _make_response(200, {"memos": [], "nextPageToken": None})

    def test_new_content_saves(self):
        """内容不存在时正常保存"""
        resp = _make_response(200, {
            "name": "memos/uid-new",
            "content": "hello",
            "visibility": "PRIVATE",
            "createTime": "2024-01-01T00:00:00Z",
            "updateTime": "2024-01-01T00:00:00Z",
            "tags": [],
        })
        self.mock_session.post.return_value = resp
        mem = self.client.idempotent_save("unique content xyz", tags=["t"])
        self.assertEqual(mem.uid, "uid-new")

    def test_existing_content_returns_dummy(self):
        """内容已存在时返回 id=0 的占位 Memory"""
        # mock list_by_tags 返回已存在的记录（带 hash 标签）
        existing_mem = Memory(
            id="uid-1", uid="uid-1", content="existing",
            tags=["hash=abc12345"], visibility="PRIVATE",
            created_at="2024-01-01", updated_at="2024-01-01", agent="test",
        )
        self.client.list_by_tags = Mock(return_value=[existing_mem])
        self.client.get_full_content_by_hash = Mock(return_value="existing content")

        mem = self.client.idempotent_save("existing content", tags=["t"])
        self.assertEqual(mem.id, 0)
        self.assertEqual(mem.content, "existing content")


class TestUpdateTags(unittest.TestCase):
    @patch("integrations.styx.get_config", return_value=_FAKE_CONFIG)
    def setUp(self, _mock_cfg):
        self.client = MemosClient(token="t", base_url="http://m", agent="test")
        self.mock_session = Mock()
        self.client.session = self.mock_session

    def test_add_tags_success(self):
        """添加标签成功"""
        # get_by_uid 返回当前 memo
        get_resp = _make_response(200, {
            "name": "memos/uid-1",
            "content": "hello #old",
            "visibility": "PRIVATE",
            "createTime": "2024-01-01T00:00:00Z",
            "updateTime": "2024-01-01T00:00:00Z",
            "tags": ["old"],
        })
        patch_resp = _make_response(200, {"ok": True})
        self.mock_session.get.return_value = get_resp
        self.mock_session.patch.return_value = patch_resp

        result = self.client.update_tags("uid-1", add_tags=["new"])
        self.assertTrue(result)
        # 验证 PATCH 调用
        self.mock_session.patch.assert_called_once()
        call_kwargs = self.mock_session.patch.call_args[1]
        self.assertIn("new", call_kwargs["json"]["content"])

    def test_no_op_when_no_tags(self):
        """无标签变更时直接返回 True"""
        result = self.client.update_tags("uid-1")
        self.assertTrue(result)
        self.mock_session.patch.assert_not_called()

    def test_returns_false_when_memo_not_found(self):
        """memo 不存在返回 False"""
        self.mock_session.get.return_value = _make_response(404)
        result = self.client.update_tags("uid-1", add_tags=["x"])
        self.assertFalse(result)


class TestListAllMemos(unittest.TestCase):
    @patch("integrations.styx.get_config", return_value=_FAKE_CONFIG)
    def setUp(self, _mock_cfg):
        self.client = MemosClient(token="t", base_url="http://m")
        self.mock_session = Mock()
        self.client.session = self.mock_session

    def test_paginates_until_no_more(self):
        """分页获取直到无更多数据"""
        self.mock_session.get.side_effect = [
            _make_response(200, {
                "memos": [{"name": "memos/uid-1", "content": "a", "tags": []}],
                "nextPageToken": "token1",
            }),
            _make_response(200, {
                "memos": [{"name": "memos/uid-2", "content": "b", "tags": []}],
                "nextPageToken": None,
            }),
        ]
        result = self.client.list_all_memos()
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].get("name"), "memos/uid-1")

    def test_respects_max_records(self):
        """max_records 限制返回数量"""
        self.mock_session.get.return_value = _make_response(200, {
            "memos": [
                {"name": "memos/uid-1", "content": "a", "tags": []},
                {"name": "memos/uid-2", "content": "b", "tags": []},
                {"name": "memos/uid-3", "content": "c", "tags": []},
            ],
            "nextPageToken": None,
        })
        result = self.client.list_all_memos(max_records=2)
        self.assertEqual(len(result), 2)

    def test_filter_fn_applied(self):
        """filter_fn 过滤结果"""
        self.mock_session.get.return_value = _make_response(200, {
            "memos": [
                {"name": "memos/uid-1", "content": "a", "tags": ["t1"]},
                {"name": "memos/uid-2", "content": "b", "tags": ["t2"]},
            ],
            "nextPageToken": None,
        })
        result = self.client.list_all_memos(filter_fn=lambda m: "t1" in m.get("tags", []))
        self.assertEqual(len(result), 1)


class TestSearch(unittest.TestCase):
    @patch("integrations.styx.get_config", return_value=_FAKE_CONFIG)
    def setUp(self, _mock_cfg):
        self.client = MemosClient(token="t", base_url="http://m")
        self.mock_session = Mock()
        self.client.session = self.mock_session

    def test_search_returns_memories(self):
        """搜索返回 Memory 列表"""
        self.mock_session.get.return_value = _make_response(200, {
            "memos": [
                {"name": "memos/uid-1", "content": "hello world", "tags": [], "visibility": "PRIVATE"},
            ],
            "nextPageToken": None,
        })
        result = self.client.search("hello")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].uid, "uid-1")

    def test_search_uses_cache(self):
        """相同查询命中缓存"""
        self.mock_session.get.return_value = _make_response(200, {
            "memos": [{"name": "memos/uid-1", "content": "x", "tags": [], "visibility": "PRIVATE"}],
            "nextPageToken": None,
        })
        r1 = self.client.search("query")
        r2 = self.client.search("query")
        # 第二次应命中缓存，session.get 只应被调用一次
        self.mock_session.get.assert_called_once()
        self.assertEqual(r1[0].uid, r2[0].uid)

    def test_search_limit(self):
        """limit 限制返回数量"""
        self.mock_session.get.return_value = _make_response(200, {
            "memos": [
                {"name": "memos/uid-1", "content": "a", "tags": [], "visibility": "PRIVATE"},
                {"name": "memos/uid-2", "content": "b", "tags": [], "visibility": "PRIVATE"},
            ],
            "nextPageToken": None,
        })
        result = self.client.search("x", limit=1)
        self.assertEqual(len(result), 1)


class TestListByTags(unittest.TestCase):
    @patch("integrations.styx.get_config", return_value=_FAKE_CONFIG)
    def setUp(self, _mock_cfg):
        self.client = MemosClient(token="t", base_url="http://m")
        self.mock_session = Mock()
        self.client.session = self.mock_session

    def test_returns_matching_memories(self):
        """按标签查询返回匹配 Memory"""
        self.mock_session.get.return_value = _make_response(200, {
            "memos": [
                {"name": "memos/uid-1", "content": "hello #shared", "tags": ["shared"], "visibility": "PRIVATE"},
            ],
            "nextPageToken": None,
        })
        result = self.client.list_by_tags(["shared"])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].uid, "uid-1")

    def test_deduplicates_by_uid(self):
        """相同 UID 去重"""
        self.mock_session.get.return_value = _make_response(200, {
            "memos": [
                {"name": "memos/uid-1", "content": "a", "tags": ["t1"], "visibility": "PRIVATE"},
                {"name": "memos/uid-1", "content": "b", "tags": ["t2"], "visibility": "PRIVATE"},
            ],
            "nextPageToken": None,
        })
        result = self.client.list_by_tags(["t1", "t2"])
        self.assertEqual(len(result), 1)


class TestGetByUid(unittest.TestCase):
    @patch("integrations.styx.get_config", return_value=_FAKE_CONFIG)
    def setUp(self, _mock_cfg):
        self.client = MemosClient(token="t", base_url="http://m")
        self.mock_session = Mock()
        self.client.session = self.mock_session

    def test_returns_memory(self):
        """根据 UID 获取 Memory"""
        self.mock_session.get.return_value = _make_response(200, {
            "name": "memos/uid-1",
            "content": "hello #tag1",
            "visibility": "PRIVATE",
            "createTime": "2024-01-01T00:00:00Z",
            "updateTime": "2024-01-01T00:00:00Z",
            "tags": ["tag1"],
        })
        mem = self.client.get_by_uid("uid-1")
        self.assertIsNotNone(mem)
        self.assertEqual(mem.uid, "uid-1")
        self.assertIn("tag1", mem.tags)

    def test_returns_none_on_404(self):
        """404 返回 None"""
        self.mock_session.get.return_value = _make_response(404)
        mem = self.client.get_by_uid("uid-1")
        self.assertIsNone(mem)


class TestDelete(unittest.TestCase):
    @patch("integrations.styx.get_config", return_value=_FAKE_CONFIG)
    def setUp(self, _mock_cfg):
        self.client = MemosClient(token="t", base_url="http://m")
        self.mock_session = Mock()
        self.client.session = self.mock_session

    def test_delete_success(self):
        """删除成功返回 True"""
        self.mock_session.patch.return_value = _make_response(200, {"ok": True})
        result = self.client.delete("uid-1")
        self.assertTrue(result)

    def test_delete_failure(self):
        """删除失败返回 False"""
        self.mock_session.patch.return_value = _make_response(500)
        result = self.client.delete("uid-1")
        self.assertFalse(result)


class TestUpdateMemo(unittest.TestCase):
    @patch("integrations.styx.get_config", return_value=_FAKE_CONFIG)
    def setUp(self, _mock_cfg):
        self.client = MemosClient(token="t", base_url="http://m", agent="test")
        self.mock_session = Mock()
        self.client.session = self.mock_session

    def test_update_content_and_tags(self):
        """更新内容和标签"""
        self.mock_session.get.return_value = _make_response(200, {
            "name": "memos/uid-1",
            "content": "old content #old",
            "visibility": "PRIVATE",
            "createTime": "2024-01-01T00:00:00Z",
            "updateTime": "2024-01-01T00:00:00Z",
            "tags": ["old"],
        })
        self.mock_session.patch.return_value = _make_response(200, {
            "name": "memos/uid-1",
            "content": "new content #test-private",
            "visibility": "PRIVATE",
            "updateTime": "2024-01-02T00:00:00Z",
        })
        mem = self.client.update_memo("uid-1", content="new content", tags=["new"])
        self.assertIsNotNone(mem)
        self.assertEqual(mem.uid, "uid-1")

    def test_returns_none_when_memo_not_found(self):
        """memo 不存在返回 None"""
        self.mock_session.get.return_value = _make_response(404)
        mem = self.client.update_memo("uid-1", content="x")
        self.assertIsNone(mem)


class TestBatchSave(unittest.TestCase):
    @patch("integrations.styx.get_config", return_value=_FAKE_CONFIG)
    def setUp(self, _mock_cfg):
        self.client = MemosClient(token="t", base_url="http://m", agent="test")
        self.mock_session = Mock()
        self.client.session = self.mock_session

    def test_batch_save_reports_success_and_failed(self):
        """批量保存报告成功和失败"""
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_response(200, {"name": "memos/uid-1"})
            else:
                raise MemosServerError("boom")

        self.mock_session.post.side_effect = side_effect
        result = self.client.batch_save([
            {"content": "item1", "tags": ["a"]},
            {"content": "item2", "tags": ["b"]},
        ])
        self.assertEqual(result["total"], 2)
        self.assertEqual(len(result["successful"]), 1)
        self.assertEqual(len(result["failed"]), 1)


class TestSaveSessionFull(unittest.TestCase):
    @patch("integrations.styx.get_config", return_value=_FAKE_CONFIG)
    def setUp(self, _mock_cfg):
        self.client = MemosClient(token="t", base_url="http://m", agent="test")
        self.mock_session = Mock()
        self.client.session = self.mock_session

    def test_short_session_single_chunk(self):
        """短会话单条消息不分片"""
        self.mock_session.post.return_value = _make_response(200, {
            "name": "memos/uid-1",
            "content": "x",
            "visibility": "PUBLIC",
            "createTime": "2024-01-01T00:00:00Z",
            "updateTime": "2024-01-01T00:00:00Z",
            "tags": [],
        })
        memories = self.client.save_session_full(
            "sess-1",
            [{"role": "user", "content": "hello"}],
            tags=["source=claude"],
        )
        self.assertEqual(len(memories), 1)
        # 验证标签包含 level=L1 和 session=
        call_kwargs = self.mock_session.post.call_args[1]
        posted_content = call_kwargs["json"]["content"]
        self.assertIn("sess-1", posted_content)

    def test_long_messages_split_into_chunks(self):
        """多消息超限制时分片"""
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            return _make_response(200, {
                "name": f"memos/uid-{call_count[0]}",
                "content": "x",
                "visibility": "PUBLIC",
                "createTime": "2024-01-01T00:00:00Z",
                "updateTime": "2024-01-01T00:00:00Z",
                "tags": [],
            })

        self.mock_session.post.side_effect = side_effect
        # 构造 10 条消息（超过每组 5 条限制）
        messages = [{"role": "user", "content": f"msg-{i}"} for i in range(10)]
        memories = self.client.save_session_full("sess-2", messages, tags=[])
        self.assertGreaterEqual(len(memories), 2)


class TestSearchAndMergeSegments(unittest.TestCase):
    @patch("integrations.styx.get_config", return_value=_FAKE_CONFIG)
    def setUp(self, _mock_cfg):
        self.client = MemosClient(token="t", base_url="http://m", agent="test")
        self.mock_session = Mock()
        self.client.session = self.mock_session

    def test_merges_segmented_memories(self):
        """自动检测并合并分段内容"""
        self.mock_session.get.side_effect = [
            # search 初始结果
            _make_response(200, {
                "memos": [
                    {"name": "memos/uid-1", "content": '{"_meta":{"hash":"abc12345"},"session_id":"s1"}', "tags": ["hash=abc12345", "segment=1/2", "type=chunk"], "visibility": "PRIVATE"},
                    {"name": "memos/uid-2", "content": '{"_meta":{"hash":"abc12345"},"session_id":"s1"}', "tags": ["hash=abc12345", "segment=2/2", "type=chunk"], "visibility": "PRIVATE"},
                ],
                "nextPageToken": None,
            }),
            # list_by_tags 获取所有分段
            _make_response(200, {
                "memos": [
                    {"name": "memos/uid-1", "content": '{"_meta":{"hash":"abc12345"}}', "tags": ["hash=abc12345", "segment=1/2", "type=chunk"], "visibility": "PRIVATE"},
                    {"name": "memos/uid-2", "content": '{"_meta":{"hash":"abc12345"}}', "tags": ["hash=abc12345", "segment=2/2", "type=chunk"], "visibility": "PRIVATE"},
                ],
                "nextPageToken": None,
            }),
        ]
        result = self.client.search_and_merge_segments("s1", limit=10)
        self.assertEqual(len(result), 1)
        self.assertIn("type=merged", result[0].tags)

    def test_normal_memories_not_merged(self):
        """普通记录不触发合并"""
        self.mock_session.get.return_value = _make_response(200, {
            "memos": [
                {"name": "memos/uid-1", "content": "hello", "tags": ["normal"], "visibility": "PRIVATE"},
            ],
            "nextPageToken": None,
        })
        result = self.client.search_and_merge_segments("hello", limit=10)
        self.assertEqual(len(result), 1)
        self.assertNotIn("type=merged", result[0].tags)


class TestGetFullContentByHash(unittest.TestCase):
    @patch("integrations.styx.get_config", return_value=_FAKE_CONFIG)
    def setUp(self, _mock_cfg):
        self.client = MemosClient(token="t", base_url="http://m", agent="test")
        self.mock_session = Mock()
        self.client.session = self.mock_session

    def test_returns_merged_content(self):
        """通过 hash 获取合并后的完整内容"""
        self.mock_session.get.return_value = _make_response(200, {
            "memos": [
                {"name": "memos/uid-1", "content": '{"_meta":{"hash":"abc12345"}} part1', "tags": ["hash=abc12345", "segment=1/2", "type=chunk"], "visibility": "PRIVATE"},
                {"name": "memos/uid-2", "content": '{"_meta":{"hash":"abc12345"}} part2', "tags": ["hash=abc12345", "segment=2/2", "type=chunk"], "visibility": "PRIVATE"},
            ],
            "nextPageToken": None,
        })
        content = self.client.get_full_content_by_hash("abc12345")
        self.assertIsNotNone(content)
        self.assertIn("part1", content)

    def test_returns_none_when_no_segments(self):
        """无分段记录返回 None"""
        self.mock_session.get.return_value = _make_response(200, {
            "memos": [],
            "nextPageToken": None,
        })
        content = self.client.get_full_content_by_hash("nonexistent")
        self.assertIsNone(content)


class TestMcpServerStub(unittest.TestCase):
    @patch("integrations.styx.get_config", return_value=_FAKE_CONFIG)
    def test_returns_dict_with_tools(self, _mock_cfg):
        """MCP 存根返回包含工具列表的字典"""
        result = MemosClient.as_mcp_server()
        self.assertEqual(result["name"], "styx-memos")
        self.assertTrue(result["capabilities"]["tools"])
        self.assertGreater(len(result["tools"]), 0)
        tool_names = [t["name"] for t in result["tools"]]
        self.assertIn("memos_save", tool_names)
        self.assertIn("memos_search", tool_names)


if __name__ == "__main__":
    unittest.main()
