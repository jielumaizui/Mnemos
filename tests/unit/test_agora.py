"""
Agora (MCP Server) 单元测试

测试范围：
- Tool 注册与发现（tools/list）
- JSON-RPC 请求路由（initialize、tools/call、notifications、未知方法）
- 响应格式构建（jsonrpc、tool_result、error）
- Wiki 写入（路径安全校验）
- Guard 检查（默认规则回退、session 复用）
- 纯工具函数（路径推断）

未覆盖（需集成测试或大量 mock）：
- wiki_search / context_aware_search（依赖 ContextAwareSearch、WikiReader）
- session_search / knowledge_ingest（依赖 StorageBackend 网络调用）
- preflight_inject（依赖 PreFlightInjector + 复盘文件）
- persona_summary / persona_update（依赖画像数据库）
- health_check（依赖真实模块导入与文件系统）
- document_process / wiki_build（依赖外部文档解析器）
"""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.access_policy import MCP_TOOL_POLICIES
from core.agent_kit.authorization import AgentAuthorizationStore
from integrations.agora import (
    MCPServer,
    MCP_TOOL_EXECUTION_ERROR,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_INVALID_REQUEST,
    JSONRPC_METHOD_NOT_FOUND,
)

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def server(tmp_path):
    """提供已初始化的 MCPServer 实例。"""
    store = AgentAuthorizationStore(tmp_path / "agent_authorization.db")
    credential = store.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities=set(MCP_TOOL_POLICIES.values()),
        allowed_projects={"mnemos", "mnemos personal"},
    )
    return MCPServer(
        launch_credential=credential,
        authorization_store=store,
    )


@pytest.fixture
def patched_config(monkeypatch, tmp_path):
    """将 core.config.get_config 替换为返回隔离临时目录的 stub。"""
    fake_cfg = MagicMock()
    fake_cfg.wiki_dir = tmp_path / "wiki"
    fake_cfg.wiki_dir.mkdir(parents=True, exist_ok=True)
    fake_cfg.database_dir = tmp_path / "db"
    fake_cfg.database_dir.mkdir(parents=True, exist_ok=True)
    fake_cfg.get.side_effect = lambda _key, default=None: default
    monkeypatch.setattr("core.config.get_config", lambda: fake_cfg)
    monkeypatch.setattr("core.app.application_hub.get_config", lambda: fake_cfg)
    try:
        import core.cognitive.trust_scorer as trust_scorer

        monkeypatch.setattr(trust_scorer, "get_config", lambda: fake_cfg)
    except ImportError:
        pass
    return fake_cfg


@pytest.fixture
def mock_home(monkeypatch, tmp_path):
    """Mock Path.home() 返回临时目录，避免测试污染真实 home。"""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# 1. Tool 注册与发现
# ---------------------------------------------------------------------------


def test_server_registers_all_tools(server):
    """MCPServer 初始化时应注册全部 30+ 个 tool。"""
    assert len(server.tools) >= 25
    expected = {
        "wiki_search",
        "wiki_read",
        "wiki_write",
        "memory_write_project",
        "memory_write_framework",
        "memory_write_global",
        "memory_search",
        "session_search",
        "capture_turn",
        "capture_session",
        "end_session",
        "capture_status",
        "knowledge_ingest",
        "knowledge_distill",
        "document_process",
        "wiki_build",
        "preflight_inject",
        "guard_check",
        "persona_summary",
        "persona_behavior_prompt",
        "persona_update",
        "signal_collect",
        "retrospective_list",
        "check_pending_recaps",
        "knowledge_source_list",
        "health_check",
        "self_diagnose",
        "configure_wiki",
        "detect_sources",
        "context_aware_search",
        "intent_route",
        "intent_correct",
        "blindspot_check",
        "predictive_push",
        "push_feedback",
        "freshness_check",
    }
    assert expected.issubset(set(server.tools.keys()))


def test_capture_turn_uses_producer_mode_without_starting_worker(server, monkeypatch):
    """MCP producer 只负责入队，不应启动 CaptureWorkerPool。"""
    calls = []

    class FakeCaptureService:
        def __init__(self, *args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})

        def capture_turn(self, **kwargs):
            return {"status": "queued", "duplicate": False}

    monkeypatch.setattr(
        "core.sync_framework.capture_service.CaptureService",
        FakeCaptureService,
    )

    result = server._tool_capture_turn(
        session_id="sess-producer",
        turn_number=1,
        user_content="hello",
        assistant_content="hi",
    )

    assert result["success"] is True
    assert calls == [{"args": (), "kwargs": {"start_worker": False}}]


def test_tools_list_returns_schema(server):
    """tools/list 应返回带 inputSchema 的工具列表。"""
    resp = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    assert "error" not in resp
    tools = resp["result"]["tools"]
    assert len(tools) >= 25
    names = {t["name"] for t in tools}
    assert "wiki_search" in names
    assert "guard_check" in names
    # 每个 tool 都应包含 inputSchema
    for t in tools:
        assert "inputSchema" in t
        assert "description" in t


def test_tools_list_schema_matches_handlers_for_full_power_capture(server):
    """tools/list 不得漏掉 handler，capture_turn 要暴露满血采集字段。"""
    tools = {tool["name"]: tool for tool in server._list_tools()["tools"]}

    assert set(server.tools).issubset(set(tools))
    assert "session_save" in tools
    capture_props = tools["capture_turn"]["inputSchema"]["properties"]
    for field in (
        "tool_calls",
        "tool_results",
        "reasoning",
        "attachments",
        "raw_event_refs",
        "source_files",
        "completeness",
    ):
        assert field in capture_props
    assert "messages" not in tools["capture_turn"]["inputSchema"].get("required", [])
    document_props = tools["document_process"]["inputSchema"]["properties"]
    assert document_props["mode"]["default"] == "distill"
    assert "write_to_wiki" not in document_props
    assert "save_to_l1" not in document_props
    feedback_schema = tools["push_feedback"]["inputSchema"]
    assert "delivery_event_id" in feedback_schema["properties"]
    assert set(feedback_schema["required"]) == {"delivery_event_id", "topic", "action"}


# ---------------------------------------------------------------------------
# 2. JSON-RPC 请求路由
# ---------------------------------------------------------------------------


def test_initialize_returns_server_info(server):
    """initialize 应返回协议版本、能力与服务器信息。"""
    resp = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        }
    )
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 0
    result = resp["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["serverInfo"]["name"] == "mnemos-mcp-server"
    assert "tools" in result["capabilities"]


def test_invalid_jsonrpc_version_returns_error(server):
    """非 2.0 版本请求应返回 JSONRPC_INVALID_REQUEST 错误。"""
    resp = server.handle_request({"jsonrpc": "1.0", "id": 99, "method": "tools/list", "params": {}})
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 99
    assert resp["error"]["code"] == JSONRPC_INVALID_REQUEST


def test_notification_returns_none(server):
    """notifications/initialized 通知不应返回响应体。"""
    resp = server.handle_request(
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
    )
    assert resp is None


def test_unknown_notification_returns_none(server):
    """未知通知方法不应返回错误响应。"""
    resp = server.handle_request(
        {"jsonrpc": "2.0", "method": "notifications/unknown", "params": {}}
    )
    assert resp is None


def test_unknown_method_returns_error(server):
    """未知方法请求应返回 JSONRPC_METHOD_NOT_FOUND。"""
    resp = server.handle_request({"jsonrpc": "2.0", "id": 7, "method": "foo/bar", "params": {}})
    assert resp["error"]["code"] == JSONRPC_METHOD_NOT_FOUND
    assert "Unknown method" in resp["error"]["message"]


def test_tools_call_rejects_non_object_params(server):
    """tools/call 的 params 必须是 JSON object，否则返回 JSONRPC_INVALID_PARAMS。"""
    resp = server.handle_request(
        {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": []}
    )

    assert resp["error"]["code"] == JSONRPC_INVALID_PARAMS
    assert resp["error"]["message"] == "Invalid params: tools/call params must be an object"


def test_tools_call_rejects_non_object_arguments(server):
    """tools/call 的 arguments 必须是 JSON object，否则返回 JSONRPC_INVALID_PARAMS。"""
    resp = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "intent_route", "arguments": []},
        }
    )

    assert resp["error"]["code"] == JSONRPC_INVALID_PARAMS
    assert resp["error"]["message"] == "Invalid params: tools/call arguments must be an object"


# ---------------------------------------------------------------------------
# 3. Tool 调用与错误处理
# ---------------------------------------------------------------------------


def test_call_tool_success(server):
    """tools/call 成功时应返回 MCP CallToolResult 包装。"""
    resp = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "intent_route",
                "arguments": {"user_input": "搜索知识库"},
            },
        }
    )
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 3
    assert "error" not in resp
    result = resp["result"]
    assert result["content"][0]["type"] == "text"
    payload = json.loads(result["content"][0]["text"])
    assert payload["success"] is True
    assert "intent" in payload


def test_call_unknown_tool_returns_error_result(server):
    """调用未注册 tool 时应在 result 中标记 isError。"""
    resp = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "nonexistent", "arguments": {}},
        }
    )
    assert "error" not in resp  # JSON-RPC 层不报错，业务层标记错误
    assert resp["result"]["isError"] is True
    assert "Unknown tool" in resp["result"]["content"][0]["text"]


def test_call_tool_invalid_params_returns_error(server):
    """Tool 参数不匹配时应返回参数错误（TypeError 捕获）。"""
    resp = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "intent_route", "arguments": {}},  # 缺少必填参数 user_input
        }
    )
    assert "error" not in resp
    assert resp["result"]["isError"] is True
    assert "Invalid parameters" in resp["result"]["content"][0]["text"]


def test_call_tool_execution_error_returns_error(server, monkeypatch):
    """Tool 执行抛异常时应返回封装后的错误结果。"""

    def _boom(**kwargs):
        raise RuntimeError("模拟内部错误")

    monkeypatch.setitem(server.tools, "intent_route", _boom)
    resp = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "intent_route", "arguments": {}},
        }
    )
    assert "error" not in resp
    assert resp["result"]["isError"] is True
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["code"] == MCP_TOOL_EXECUTION_ERROR
    assert "intent_route" in payload["error"]


# ---------------------------------------------------------------------------
# 4. Wiki 写入（路径安全）
# ---------------------------------------------------------------------------


def test_wiki_write_success(patched_config, server):
    """正常写入 Wiki 页面应成功并返回路径与大小。"""
    result = server._tool_wiki_write(
        page_path="concepts/test.md",
        content="# Hello\n\nWorld",
        frontmatter={"title": "Test Page", "tags": ["test"]},
    )
    assert result["success"] is True
    assert result["path"] == "concepts/test.md"
    assert result["size"] > 0
    # 验证文件实际写入
    written = patched_config.wiki_dir / "concepts" / "test.md"
    assert written.exists()
    text = written.read_text(encoding="utf-8")
    assert "title: Test Page" in text
    assert "tags:" in text
    assert "test" in text
    assert "# Hello" in text


def test_wiki_write_path_traversal_blocked(patched_config, server):
    """路径穿越攻击应被阻止，返回安全错误。"""
    result = server._tool_wiki_write(
        page_path="../../../etc/passwd",
        content="evil",
    )
    assert result["success"] is False
    assert "超出 Wiki 目录范围" in result["message"]


def test_wiki_write_auto_frontmatter_timestamp(
    patched_config,
    server,
):
    """未提供 updated_at 时应自动注入当前时间戳。"""
    result = server._tool_wiki_write(
        page_path="auto_fm.md",
        content="body",
        frontmatter={},
    )
    assert result["success"] is True
    written = patched_config.wiki_dir / "auto_fm.md"
    text = written.read_text(encoding="utf-8")
    assert "updated_at:" in text


def test_memory_write_project_adds_scope_frontmatter(
    patched_config,
    server,
):
    result = server._tool_memory_write_project(
        title="Deploy Notes",
        content="Use Obsidian raw vault.",
        project="Mnemos Personal",
    )

    assert result["success"] is True
    assert result["scope"] == "project"
    assert result["project"] == "Mnemos Personal"
    assert result["path"].startswith("scopes/project/mnemos-personal/")
    written = patched_config.wiki_dir / result["path"]
    text = written.read_text(encoding="utf-8")
    assert "scope: project" in text
    assert "project: Mnemos Personal" in text
    assert "scope/project" in text


def test_memory_write_framework_and_global_paths(
    patched_config,
    server,
):
    framework = server._tool_memory_write_framework(
        title="React Query Rules",
        content="Cache invalidation notes.",
        framework="React Query",
    )
    global_result = server._tool_memory_write_global(
        title="Writing Principle",
        content="Prefer concrete examples.",
    )

    assert framework["success"] is True
    assert framework["path"].startswith("scopes/framework/react-query/")
    assert global_result["success"] is True
    assert global_result["path"].startswith("scopes/global/")


# ---------------------------------------------------------------------------
# 5. Guard 检查（默认规则回退 + session 复用）
# ---------------------------------------------------------------------------


def test_guard_check_no_knowledge_uses_default_rules(server, monkeypatch):
    """当 PreFlightInjector 无返回时，guard_check 应回退到默认高风险规则。"""
    # mock PreFlightInjector 返回空知识
    monkeypatch.setattr(
        "core.kia.prophasis.PreFlightInjector.inject",
        lambda self, *a, **k: None,
    )
    # mock InProcessGuard 不触发警报
    monkeypatch.setattr(
        "core.kia.aegis.InProcessGuard.check",
        lambda self, *a, **k: None,
    )
    result = server._tool_guard_check(
        user_message="正常消息",
        task_type="coding",
    )
    assert result["success"] is True
    assert result["alert"] is False
    assert result["message"] == "无风险触发"


def test_guard_check_lock_timeout_returns_success(server, monkeypatch, patched_config):
    """PreFlightInjector 构造期锁超时时，MCP guard_check 应降级而不是工具失败。"""

    class LockedPreFlightInjector:
        def __init__(self):
            raise sqlite3.OperationalError(
                "sqlite lock timeout for user_signals.db"
            )

    monkeypatch.setattr("core.kia.prophasis.PreFlightInjector", LockedPreFlightInjector)

    result = server._tool_guard_check(
        user_message="检查风险",
        task_type="coding",
    )

    assert result["success"] is True
    assert result["alert"] is False
    assert result["message"] == "无风险触发"


def test_guard_check_reuses_guard_session(server, monkeypatch):
    """相同 checklist 的 guard 应在多次调用间复用实例。"""
    from core.application.kia import KiaApplicationService

    call_count = 0

    class FakeGuard:
        def __init__(self, knowledge):
            self.knowledge = knowledge

        def check(self, user_message, ai_response, context=None):
            nonlocal call_count
            call_count += 1
            return None

    monkeypatch.setattr("core.kia.aegis.InProcessGuard", FakeGuard)
    monkeypatch.setattr(KiaApplicationService, "_active_policy_patches", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        "core.kia.prophasis.PreFlightInjector.inject",
        lambda self, *a, **k: None,
    )

    server._tool_guard_check(user_message="msg1", task_type="coding")
    server._tool_guard_check(user_message="msg2", task_type="coding")
    # 两次调用应复用同一个 guard 实例（只构造一次）
    assert len(server._guard_sessions) == 1


# ---------------------------------------------------------------------------
# 6. 纯工具函数
# ---------------------------------------------------------------------------


def test_infer_type_from_path_with_category(server):
    """路径含目录时应返回第一级目录名。"""
    assert server._infer_type_from_path("concepts/agora.md") == "concepts"
    assert server._infer_type_from_path("01-Projects/mcp.md") == "01-Projects"


def test_infer_type_from_path_without_category(server):
    """无目录路径应回退为 00-Inbox。"""
    assert server._infer_type_from_path("note.md") == "00-Inbox"
    assert server._infer_type_from_path("hello") == "00-Inbox"


# ---------------------------------------------------------------------------
# 7. 响应构建工具
# ---------------------------------------------------------------------------


def test_make_jsonrpc_response_structure(server):
    """成功响应应包含 jsonrpc、id、result 三要素。"""
    resp = server._make_jsonrpc_response(42, {"foo": "bar"})
    assert resp == {"jsonrpc": "2.0", "id": 42, "result": {"foo": "bar"}}


def test_make_jsonrpc_error_structure(server):
    """错误响应应包含 jsonrpc、id、error（code + message）。"""
    resp = server._make_jsonrpc_error(42, -32600, "Bad Request", data="extra")
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 42
    assert resp["error"]["code"] == -32600
    assert resp["error"]["message"] == "Bad Request"
    assert resp["error"]["data"] == "extra"


def test_make_tool_result_with_dict(server):
    """字典结果应被序列化为 JSON 文本放入 content。"""
    result = server._make_tool_result({"success": True, "count": 3})
    assert result["content"][0]["type"] == "text"
    payload = json.loads(result["content"][0]["text"])
    assert payload["success"] is True
    assert payload["count"] == 3


def test_make_tool_result_with_string(server):
    """字符串结果应直接作为文本返回。"""
    result = server._make_tool_result("plain text")
    assert result["content"][0]["text"] == "plain text"


def test_make_tool_result_error_flag(server):
    """is_error=True 时应标记 isError 字段。"""
    result = server._make_tool_result("oops", is_error=True)
    assert result["isError"] is True


def test_retrospective_list_returns_structured_fields(server, patched_config):
    """retrospective_list 应返回 path/title 以及 task_type/subtype/version 结构化字段。"""
    wiki_dir = patched_config.wiki_dir
    retro_dir = wiki_dir / "06-Retrospectives" / "coding"
    retro_dir.mkdir(parents=True, exist_ok=True)
    (retro_dir / "debug-v1.md").write_text(
        "---\n"
        "title: Debug Retro\n"
        "applies_when:\n  task_type: [coding]\n"
        "scope: agent\n"
        "source_agent: codex\n"
        "acl_schema_version: 1\n"
        "acl_metadata_complete: true\n"
        "acl_reconciliation_status: proven\n"
        "---\nbody",
        encoding="utf-8",
    )
    (retro_dir / "private-claude-v1.md").write_text(
        "---\n"
        "title: PRIVATE-CLAUDE-RETRO\n"
        "scope: agent\n"
        "source_agent: claude\n"
        "acl_schema_version: 1\n"
        "acl_metadata_complete: true\n"
        "acl_reconciliation_status: proven\n"
        "---\nPRIVATE-RETRO-BODY\n",
        encoding="utf-8",
    )

    result = server._tool_retrospective_list(limit=10)
    assert result["success"] is True
    assert len(result["retrospectives"]) == 1
    item = result["retrospectives"][0]
    assert item["path"] == "coding/debug-v1.md"
    assert item["title"] == "Debug Retro"
    assert item["task_type"] == "coding"
    assert item["subtype"] == "debug"
    assert item["version"] == 1
    assert result["access_filter"]["cross_agent_requires_authorization"] == 1
    assert "PRIVATE-CLAUDE-RETRO" not in json.dumps(result)


# ---------------------------------------------------------------------------
# 6. Predictive Push / Push Feedback / Intent Correct
# ---------------------------------------------------------------------------


def test_tool_predictive_push_delegates_to_reminder_engine(
    server,
    monkeypatch,
    patched_config,
):
    """predictive_push 应直接调用 ReminderEngine.contextual_reminders()。"""
    from core.kia.reminder_engine import Reminder

    fake_reminder = Reminder(
        reminder_type="contextual",
        page_path="03-Tech/redis.md",
        title="Redis 连接池",
        message="推荐查看：Redis 连接池",
        reason="用户提到 Redis 报错",
        confidence=0.85,
        priority="high",
    )
    page = patched_config.wiki_dir / "03-Tech" / "redis.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\nscope: agent\nsource_agent: codex\nacl_schema_version: 1\n"
        "acl_metadata_complete: true\nacl_reconciliation_status: proven\n---\nbody\n",
        encoding="utf-8",
    )

    class FakeReminderEngine:
        def contextual_reminders(
            self,
            user_input,
            recent_context=None,
            candidate_filter=None,
            candidate_path_filter=None,
        ):
            assert recent_context == "/project"
            assert candidate_path_filter(fake_reminder.page_path) is True
            assert candidate_filter(fake_reminder) is True
            return [fake_reminder]

    class FakeDeliveryDecision:
        event_id = "delivery-1"
        decision = "deliver"
        trust_decision_id = "trust-1"
        metadata = {
            "trust_decision": {
                "decision_id": "trust-1",
                "decision": "deliver",
                "reason": "delivery_requirements_met",
            }
        }

        def to_dict(self):
            return {
                "event_id": self.event_id,
                "decision": self.decision,
                "trust_decision_id": self.trust_decision_id,
                "metadata": self.metadata,
                "reason": "delivery_requirements_met",
            }

    class FakeDeliveryRouter:
        def route_candidate(self, **kwargs):
            assert kwargs["channel"] == "predictive_push"
            assert kwargs["task_key"] == "/project"
            assert kwargs["cooldown_key"] == "redis 连接池"
            assert kwargs["evidence_refs"] == ["03-Tech/redis.md"]
            assert kwargs["principal"] == server._server_principal()
            source_access = kwargs["source_access_control"]
            assert source_access["owner"]["agent"] == "codex"
            assert source_access["scope"]["scope_type"] == "topic"
            assert source_access["scope"]["scope_id"] == "redis 连接池"
            assert source_access["source_acl_lineage"][0].startswith("sha256:")
            return FakeDeliveryDecision()

    monkeypatch.setattr("core.kia.reminder_engine.ReminderEngine", FakeReminderEngine)
    monkeypatch.setattr(
        "core.cognitive.delivery_router.KnowledgeDeliveryRouter",
        lambda: FakeDeliveryRouter(),
    )

    result = server._tool_predictive_push("Redis 报错怎么解决", working_dir="/project")

    assert result["success"] is True
    assert result["push_available"] is True
    assert result["count"] == 1
    push = result["pushes"][0]
    assert push["topic"] == "redis 连接池"
    assert push["title"] == "Redis 连接池"
    assert push["page_path"] == "03-Tech/redis.md"
    assert push["confidence"] == 0.85
    assert push["trust_decision_id"] == "trust-1"
    assert push["trust_decision"]["decision"] == "deliver"


def test_tool_predictive_push_respects_trust_gate_suppression(
    server,
    monkeypatch,
    patched_config,
):
    from core.kia.reminder_engine import Reminder

    fake_reminder = Reminder(
        reminder_type="contextual",
        page_path="03-Tech/redis.md",
        title="Redis 连接池",
        message="推荐查看：Redis 连接池",
        reason="用户提到 Redis 报错",
        confidence=0.85,
        priority="high",
    )
    page = patched_config.wiki_dir / "03-Tech" / "redis.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\nscope: agent\nsource_agent: codex\nacl_schema_version: 1\n"
        "acl_metadata_complete: true\nacl_reconciliation_status: proven\n---\nbody\n",
        encoding="utf-8",
    )

    class FakeReminderEngine:
        def contextual_reminders(
            self,
            user_input,
            recent_context=None,
            candidate_filter=None,
            candidate_path_filter=None,
        ):
            assert candidate_path_filter(fake_reminder.page_path) is True
            assert candidate_filter(fake_reminder) is True
            return [fake_reminder]

    class FakeDeliveryDecision:
        event_id = "delivery-2"
        decision = "suppress"
        trust_decision_id = "trust-2"
        metadata = {
            "trust_decision": {
                "decision_id": "trust-2",
                "decision": "suppress",
                "reason": "low_task_fit",
            }
        }

        def to_dict(self):
            return {
                "event_id": self.event_id,
                "decision": self.decision,
                "trust_decision_id": self.trust_decision_id,
                "metadata": self.metadata,
                "reason": "low_task_fit",
            }

    class FakeDeliveryRouter:
        def route_candidate(self, **kwargs):
            return FakeDeliveryDecision()

    monkeypatch.setattr("core.kia.reminder_engine.ReminderEngine", FakeReminderEngine)
    monkeypatch.setattr(
        "core.cognitive.delivery_router.KnowledgeDeliveryRouter",
        lambda: FakeDeliveryRouter(),
    )

    result = server._tool_predictive_push("Redis 报错怎么解决")

    assert result["success"] is True
    assert result["push_available"] is False
    assert result["suppressed_count"] == 1
    assert result["trust_decisions"][0]["decision_id"] == "trust-2"


def test_tool_predictive_push_no_reminders(server, monkeypatch, patched_config):
    """ReminderEngine 无提醒时返回 push_available=False。"""

    class FakeReminderEngine:
        def contextual_reminders(
            self,
            user_input,
            recent_context=None,
            candidate_filter=None,
            candidate_path_filter=None,
        ):
            return []

    monkeypatch.setattr("core.kia.reminder_engine.ReminderEngine", FakeReminderEngine)

    result = server._tool_predictive_push("随便聊聊")
    assert result["success"] is True
    assert result["push_available"] is False
    assert "message" in result


def _create_feedback_delivery(
    server,
    patched_config,
    topic,
    *,
    principal_id="",
    project="",
    session_id="",
    cooldown_key="",
):
    import hashlib

    from core.cognitive.access_control import make_cognitive_access_envelope
    from core.cognitive.delivery_router import DeliveryBudgetPolicy, KnowledgeDeliveryRouter

    principal = server._server_principal()
    if principal_id:
        principal = replace(principal, principal_id=principal_id)
    router = KnowledgeDeliveryRouter(
        db_path=patched_config.database_dir / "delivery_events.db",
        database_dir=patched_config.database_dir,
        policy=DeliveryBudgetPolicy(
            daily_total=100,
            per_task_total=100,
            per_task_hint=100,
            per_task_warn=100,
            same_topic_cooldown_hours=0,
        ),
    )
    normalized_topic = topic.lower()
    source_access = make_cognitive_access_envelope(
        owner_principal_id=principal.principal_id,
        owner_agent=principal.agent,
        scope_type="topic",
        scope_id=normalized_topic,
        session_id=session_id or f"predictive-test:{normalized_topic}",
        project=project,
        purposes=(
            "cognitive_state_read",
            "cognitive_state_write",
            "prediction_read",
        ),
        consent_provenance_refs=(f"wiki:{normalized_topic}",),
        sensitivity="sensitive",
        retention_policy="prediction_source",
        source_acl_lineage=(
            "sha256:"
            + hashlib.sha256(normalized_topic.encode()).hexdigest(),
        ),
    )
    route_kwargs = {
        "source": "predictive_push",
        "subject": normalized_topic,
        "channel": "predictive_push",
        "evidence_refs": [f"03-Tech/{topic}.md"],
        "task_fit_score": 0.9,
        "cooldown_key": cooldown_key or topic.lower(),
        "metadata": {
            "principal_id": principal.principal_id,
            "principal_agent": principal.agent,
            "project": project,
            "session_id": session_id,
        },
        "source_access_control": source_access,
        "principal": principal,
    }
    decision = router.route_candidate(**route_kwargs)
    assert decision.decision == "deliver"
    router.record_presentation(
        decision.event_id,
        host_agent=principal.agent,
        rendered_content_hash="sha256:"
        + hashlib.sha256(f"agora-feedback-display:{decision.event_id}".encode()).hexdigest(),
    )
    return decision


def test_tool_push_feedback_records_canonical_reactions_without_direct_updates(
    server, patched_config, monkeypatch
):
    outcome_calls = []
    trust_calls = []

    class ForbiddenOutcomeRecorder:
        def record_outcome(self, **kwargs):
            outcome_calls.append(kwargs)
            raise AssertionError("reaction must not enter OutcomeRecorder")

    class ForbiddenTrustScorer:
        def record_negative_evidence(self, **kwargs):
            trust_calls.append(kwargs)
            raise AssertionError("reaction must not directly update trust")

    decisions = [
        (_create_feedback_delivery(server, patched_config, "IgnoreTopic"), "ignore"),
        (_create_feedback_delivery(server, patched_config, "AcceptTopic"), "accept"),
        (_create_feedback_delivery(server, patched_config, "DismissTopic"), "dismiss"),
    ]
    monkeypatch.setattr(
        "core.app.outcome_recorder.OutcomeRecorder",
        lambda: ForbiddenOutcomeRecorder(),
    )
    monkeypatch.setattr(
        "core.cognitive.delivery_router.KnowledgeTrustScorer",
        lambda *args, **kwargs: ForbiddenTrustScorer(),
    )

    results = [
        server._tool_push_feedback(decision.subject, action, decision.event_id)
        for decision, action in decisions
    ]

    assert {result["action"] for result in results} == {"ignore", "accept", "dismiss"}
    assert {result["disposition"] for result in results} == {"record_only"}
    assert all(result["required_receipts_complete"] for result in results)
    assert not outcome_calls
    assert not trust_calls
    from core.cognitive.state_store import CognitiveStateStore

    state = CognitiveStateStore(patched_config.database_dir / "producer_consumer_ledger.db")
    assert len(state.current_revisions(object_type="user_reaction_event")) == 3
    assert len(state.current_revisions(object_type="outcome_measurement")) == 0


def test_tool_push_feedback_binds_exact_event_and_principal(server, patched_config):
    decision = _create_feedback_delivery(server, patched_config, "ExactIdentityTopic")

    result = server._tool_push_feedback(
        "ExactIdentityTopic",
        "dismiss",
        decision.event_id,
    )

    assert result["success"] is True
    assert result["status"] == "complete"
    assert result["delivery_event_id"] == decision.event_id
    assert result["feedback_event_id"].startswith("feedback-event-")
    assert result["required_receipts_complete"] is True
    assert result["terminal_status"] == "complete"
    assert result["principal"]["principal_id"] == server._server_principal().principal_id
    assert result["effect_delta"] == {
        "direct_domain_updates": 0,
        "proposal_commands": 0,
    }
    with sqlite3.connect(patched_config.database_dir / "delivery_events.db") as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(delivery_events)")}
    assert {"feedback", "feedback_at", "outcome_id"}.isdisjoint(columns)


def test_tool_push_feedback_rejects_wrong_principal_before_side_effects(
    server, patched_config
):
    decision = _create_feedback_delivery(
        server,
        patched_config,
        "WrongPrincipalTopic",
        principal_id="another-principal",
    )

    result = server._tool_push_feedback(
        "WrongPrincipalTopic",
        "ignore",
        decision.event_id,
    )

    assert result == {
        "success": False,
        "reason": "delivery_event_principal_mismatch",
    }
    from core.cognitive.state_store import CognitiveStateStore

    state = CognitiveStateStore(patched_config.database_dir / "producer_consumer_ledger.db")
    assert not state.current_revisions(object_type="user_reaction_event")


def test_tool_push_feedback_duplicate_submission_has_one_canonical_effect_set(
    server, patched_config
):
    decision = _create_feedback_delivery(server, patched_config, "DuplicateFeedbackTopic")

    first = server._tool_push_feedback(
        "DuplicateFeedbackTopic", "ignore", decision.event_id
    )
    repeated = server._tool_push_feedback(
        "DuplicateFeedbackTopic", "ignore", decision.event_id
    )

    assert first["success"] is True
    assert repeated["success"] is True
    assert repeated["feedback_event_id"] == first["feedback_event_id"]
    assert repeated["reaction_revision_id"] == first["reaction_revision_id"]
    assert repeated["attribution_revision_id"] == first["attribution_revision_id"]
    from core.cognitive.state_store import CognitiveStateStore

    state = CognitiveStateStore(patched_config.database_dir / "producer_consumer_ledger.db")
    assert len(state.current_revisions(object_type="user_reaction_event")) == 1
    receipts = state.effect_receipts_for_revision(first["attribution_revision_id"])
    assert len(receipts) == 7


def test_tool_push_feedback_invalid_action(server):
    result = server._tool_push_feedback("Docker", "unknown", "delivery-missing")
    assert result["success"] is False
    assert "action" in result["error"]


def test_tool_quality_correction_requires_and_records_exact_latest_lineage(
    server, patched_config
):
    decision = _create_feedback_delivery(server, patched_config, "QualityFeedback")
    first = server._tool_push_feedback(
        "QualityFeedback",
        "dismiss",
        decision.event_id,
    )

    rejected = server._tool_push_feedback(
        "QualityFeedback",
        "outdated",
        decision.event_id,
    )
    corrected = server._tool_push_feedback(
        "QualityFeedback",
        "outdated",
        decision.event_id,
        supersedes_event_id=first["feedback_event_id"],
        correction_target_ref="delivery:" + decision.event_id + "#claim-1",
        correction_reason="The delivered version is stale.",
    )

    assert rejected == {
        "success": False,
        "reason": "correction_requires_latest_event_target_and_reason",
    }
    assert corrected["success"] is True
    assert corrected["status"] == "complete"
    assert corrected["disposition"] == "proposal_eligible"
    assert corrected["pending_command_ids"] == []
    from core.cognitive.state_store import CognitiveStateStore

    state = CognitiveStateStore(patched_config.database_dir / "producer_consumer_ledger.db")
    reaction = state.revision(corrected["reaction_revision_id"])
    assert reaction is not None
    assert reaction.correction_of_revision_id == first["reaction_revision_id"]


def test_tool_intent_correct_records_correction(tmp_path, monkeypatch):
    """intent_correct 应写入纠正库并影响后续 intent_route。"""
    from integrations.agora import MCPServer

    fake_cfg = MagicMock()
    fake_cfg.wiki_dir = tmp_path / "wiki"
    fake_cfg.wiki_dir.mkdir(parents=True, exist_ok=True)
    fake_cfg.database_dir = tmp_path / "db"
    fake_cfg.database_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("core.app.intent_router.get_config", lambda: fake_cfg)
    monkeypatch.setattr("core.config.get_config", lambda: fake_cfg)

    server = MCPServer()
    route1 = server._tool_intent_route("随便聊聊")
    assert route1["intent"] == "chat"

    result = server._tool_intent_correct("随便聊聊", "chat", "knowledge")
    assert result["success"] is True

    route2 = server._tool_intent_route("随便聊聊")
    assert route2["intent"] == "knowledge"
    assert route2["route_tools"] == ["context_aware_search", "wiki_search"]


def test_tool_intent_route_returns_route_and_fallback_tools(tmp_path, monkeypatch):
    """intent_route 应返回第 4 步需要的数据源、首选工具和 fallback。"""
    from integrations.agora import MCPServer

    fake_cfg = MagicMock()
    fake_cfg.wiki_dir = tmp_path / "wiki"
    fake_cfg.wiki_dir.mkdir(parents=True, exist_ok=True)
    fake_cfg.database_dir = tmp_path / "db"
    fake_cfg.database_dir.mkdir(parents=True, exist_ok=True)
    fake_cfg.get.side_effect = lambda key, default=None: {
        "intent_router.llm_fallback_enabled": False,
    }.get(key, default)
    monkeypatch.setattr("core.app.intent_router.get_config", lambda: fake_cfg)
    monkeypatch.setattr("core.config.get_config", lambda: fake_cfg)

    server = MCPServer()
    result = server._tool_intent_route("上次我们怎么解决这个问题")

    assert result["intent"] == "mixed_recall"
    assert result["data_source"] == "raw+wiki"
    assert result["route_tools"] == ["session_search", "context_aware_search"]
    assert result["fallback_tools"] == ["wiki_search"]
    assert result["explanation"]


def test_tool_intent_correct_invalid_original_intent(server):
    """intent_correct 非法 original_intent 应返回错误。"""
    result = server._tool_intent_correct("x", "invalid", "knowledge")
    assert result["success"] is False
    assert "original_intent" in result["error"]


def test_tool_intent_correct_invalid_intent(server):
    """intent_correct 非法 corrected_intent 应返回错误。"""
    result = server._tool_intent_correct("x", "chat", "invalid")
    assert result["success"] is False
    assert "corrected_intent" in result["error"]


def test_context_aware_search_tool_returns_score_breakdown(server, monkeypatch):
    class FakeSearchResult:
        page_path = "02-Concepts/distill.md"
        title = "蒸馏链路质量"
        snippet = "蒸馏需要保留 Raw 原文。"
        score = 0.91
        heat_level = "warm"
        heat_score = 6.0
        last_accessed = ""
        match_source = "keyword"
        match_reason = "关键词命中:蒸馏；命中字段:标题/frontmatter/正文"
        score_breakdown = {"relevance": 0.9, "confidence": 0.8, "keyword": 0.75}
        matched_terms = ["蒸馏"]
        scope = "public"
        source_agent = "codex"
        session_id = ""
        project = ""
        tags = []
        acl_schema_version = 1
        acl_metadata_complete = True
        acl_reconciliation_status = "proven"

    class FakeContextAwareSearch:
        def search(self, query, context=None, limit=10, *, principal, narrowing):
            return [FakeSearchResult()]

        def get_last_query_trace(self):
            return {
                "embedding_enabled": True,
                "embedding_attempted": True,
                "rerank_api_called": True,
                "degraded": False,
                "degraded_reasons": [],
            }

        def record_authorized_search(
            self,
            query,
            results,
            *,
            principal=None,
            narrowing=None,
        ):
            del query, results, principal, narrowing
            return None

    monkeypatch.setattr("core.app.context_search.ContextAwareSearch", FakeContextAwareSearch)
    monkeypatch.setattr(
        "core.application.intelligence.IntelligenceApplicationService._authorized_wiki_surface",
        lambda *_args, **_kwargs: (True, {}),
    )

    result = server._tool_context_aware_search("查知识蒸馏")

    first = result["results"][0]
    assert first["score_breakdown"]["relevance"] == 0.9
    assert first["matched_terms"] == ["蒸馏"]
    assert result["query_trace"]["embedding_attempted"] is True
    assert result["query_trace"]["rerank_api_called"] is True
    assert result["degraded"] is False
    assert result["degraded_reasons"] == []


def test_context_aware_search_rejects_caller_identity_override(server):
    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 91,
            "method": "tools/call",
            "params": {
                "name": "context_aware_search",
                "arguments": {"query": "codex", "agent": "hermes"},
            },
        }
    )
    payload = json.loads(response["result"]["content"][0]["text"])

    assert response["result"]["isError"] is True
    assert payload["code"] == "caller_identity_override_forbidden"


def test_server_principal_does_not_follow_mnemos_agent_env(server, monkeypatch):
    monkeypatch.setenv("MNEMOS_AGENT", "hermes")

    assert server._principal.agent == "codex"
    assert server._principal.source == "server"


def test_context_aware_search_denies_result_with_missing_acl_metadata(server, monkeypatch):
    class FakeSearchResult:
        page_path = "02-Concepts/codex-only.md"
        title = "Codex only"
        snippet = "source scoped result"
        score = 0.8
        heat_level = "cold"
        heat_score = 0.0
        last_accessed = ""
        match_source = "keyword"
        match_reason = "keyword"
        score_breakdown = {}
        matched_terms = []
        source_agent = "codex"
        acl_metadata_complete = False

    class FakeContextAwareSearch:
        def search(self, query, context=None, limit=10, *, principal, narrowing):
            return [FakeSearchResult()]

    monkeypatch.setattr("core.app.context_search.ContextAwareSearch", FakeContextAwareSearch)
    monkeypatch.setattr(
        "core.application.intelligence.IntelligenceApplicationService._authorized_wiki_surface",
        lambda *_args, **_kwargs: (True, {}),
    )

    result = server._tool_context_aware_search("codex")

    assert result["results"] == []
    assert result["access_filter"]["acl_metadata_missing"] == 1


# ---------------------------------------------------------------------------
# 8. Reflection 工具
# ---------------------------------------------------------------------------


def _make_fake_reflection_result(summary="", key_points=None, llm_called=False, llm_error=""):
    from core.reflection.models import InsightSnapshot, ReflectionRecord, ReflectionTrigger
    from datetime import datetime

    insight = MagicMock()
    insight.summary = summary
    insight.key_points = key_points or []
    insight.confidence = 0.8
    insight.prompt_used = "fake prompt"
    insight.llm_called = llm_called
    insight.llm_error = llm_error
    insight.to_snapshot.return_value = InsightSnapshot(
        summary=summary, key_points=key_points or [], dimensions_involved=["decision"]
    )

    record = ReflectionRecord(
        id="ref-123",
        trigger=ReflectionTrigger.MANUAL,
        trigger_event="t",
        user_query="q",
        created_at=datetime.now(),
    )

    result = MagicMock()
    result.triggered = True
    result.insight = insight
    result.record = record
    result.feedback_messages = []
    return result


def _patch_reflection_signal_store(monkeypatch):
    fake_store = object()
    monkeypatch.setattr("core.persona.psyche.get_signal_store", lambda: fake_store)
    return fake_store


def test_reflect_on_input_auto_llm_true_returns_insight(server, monkeypatch):
    """auto_llm=True 时返回 LLM 生成的洞察摘要与 key_points。"""
    _patch_reflection_signal_store(monkeypatch)

    fake_result = _make_fake_reflection_result(
        summary="你近期频繁启动新项目",
        key_points=["关键发现1"],
        llm_called=True,
    )

    class FakeEngine:
        def __init__(self, use_llm=True, **kwargs):
            self.use_llm = use_llm

        def reflect_on_user_input(self, text, **_kwargs):
            return fake_result

        def reflect_manually(self, query, **_kwargs):
            return fake_result

    monkeypatch.setattr("core.reflection.reflection_engine.ReflectionEngine", FakeEngine)

    result = server._tool_reflect_on_input(
        "我要启动新项目", auto_llm=True, session_id="session-1", project="mnemos"
    )

    assert result["triggered"] is True
    assert result["insight_summary"] == "你近期频繁启动新项目"
    assert result["key_points"] == ["关键发现1"]
    assert result["llm_called"] is True
    assert result["llm_error"] == ""


def test_reflect_on_input_auto_llm_false_returns_prompt_only(server, monkeypatch):
    """auto_llm=False 时只返回 prompt，不调用 LLM。"""
    _patch_reflection_signal_store(monkeypatch)

    fake_result = _make_fake_reflection_result(
        summary="",
        key_points=[],
        llm_called=False,
    )

    engine_calls = []

    class FakeEngine:
        def __init__(self, use_llm=True, **kwargs):
            self.use_llm = use_llm
            engine_calls.append(use_llm)

        def reflect_on_user_input(self, text, **_kwargs):
            return fake_result

        def reflect_manually(self, query, **_kwargs):
            return fake_result

    monkeypatch.setattr("core.reflection.reflection_engine.ReflectionEngine", FakeEngine)

    result = server._tool_reflect_on_input(
        "我要启动新项目", auto_llm=False, session_id="session-1", project="mnemos"
    )

    assert result["triggered"] is True
    assert result["insight_summary"] == ""
    assert result["key_points"] == []
    assert result["llm_called"] is False
    assert result["prompt_used"] == "fake prompt"
    assert engine_calls == [False]


def test_reflect_on_input_llm_failure_returns_error(server, monkeypatch):
    """LLM 调用失败时返回 llm_called=true 与 llm_error。"""
    _patch_reflection_signal_store(monkeypatch)

    fake_result = _make_fake_reflection_result(
        summary="",
        key_points=[],
        llm_called=True,
        llm_error="LLM 调用未返回有效内容",
    )

    class FakeEngine:
        def __init__(self, use_llm=True, **kwargs):
            self.use_llm = use_llm

        def reflect_on_user_input(self, text, **_kwargs):
            return fake_result

        def reflect_manually(self, query, **_kwargs):
            return fake_result

    monkeypatch.setattr("core.reflection.reflection_engine.ReflectionEngine", FakeEngine)

    result = server._tool_reflect_on_input(
        "我要启动新项目", auto_llm=True, session_id="session-1", project="mnemos"
    )

    assert result["llm_called"] is True
    assert "LLM 调用未返回有效内容" in result["llm_error"]


def test_reflect_manually_auto_llm_false(server, monkeypatch):
    """reflect_manually 也支持 auto_llm=False。"""
    _patch_reflection_signal_store(monkeypatch)

    fake_result = _make_fake_reflection_result(
        summary="",
        key_points=[],
        llm_called=False,
    )

    engine_calls = []

    class FakeEngine:
        def __init__(self, use_llm=True, **kwargs):
            self.use_llm = use_llm
            engine_calls.append(use_llm)

        def reflect_manually(self, query, **_kwargs):
            return fake_result

    monkeypatch.setattr("core.reflection.reflection_engine.ReflectionEngine", FakeEngine)

    result = server._tool_reflect_manually(
        "分析最近状态", auto_llm=False, session_id="session-1", project="mnemos"
    )

    assert result["llm_called"] is False
    assert engine_calls == [False]


def test_reflect_on_input_disables_unscoped_legacy_consumers(server, monkeypatch):
    """MCP 反射入口不能启用绕过 ACL/删除回执的旧消费者或 Wiki exporter。"""

    fake_result = _make_fake_reflection_result(summary="s", key_points=["k"], llm_called=True)
    captured = {}
    _patch_reflection_signal_store(monkeypatch)

    class FakeEngine:
        def __init__(self, use_llm=True, **kwargs):
            self.use_llm = use_llm
            captured["kwargs"] = kwargs

        def reflect_on_user_input(self, text, **_kwargs):
            return fake_result

    monkeypatch.setattr("core.reflection.reflection_engine.ReflectionEngine", FakeEngine)

    server._tool_reflect_on_input(
        "测试", auto_llm=True, session_id="session-1", project="mnemos"
    )
    assert captured["kwargs"].get("register_default_consumers") is False
    assert captured["kwargs"].get("export_to_wiki") is False


def test_reflect_on_input_persona_lock_timeout_degrades(server, monkeypatch):
    """画像库锁超时时，reflection MCP 入口应继续返回结果。"""

    fake_result = _make_fake_reflection_result(summary="s", key_points=["k"], llm_called=True)
    captured = {}

    class FakeEngine:
        def __init__(self, use_llm=True, **kwargs):
            self.use_llm = use_llm
            captured["kwargs"] = kwargs

        def reflect_on_user_input(self, text, **_kwargs):
            return fake_result

    def locked_signal_store():
        raise sqlite3.OperationalError(
            "sqlite lock timeout for user_signals.db"
        )

    monkeypatch.setattr("core.reflection.reflection_engine.ReflectionEngine", FakeEngine)
    monkeypatch.setattr("core.persona.psyche.get_signal_store", locked_signal_store)

    result = server._tool_reflect_on_input(
        "测试", auto_llm=True, session_id="session-1", project="mnemos"
    )

    assert result["success"] is True
    assert captured["kwargs"].get("register_default_consumers") is False


# ---------------------------------------------------------------------------
# 9. Persona 行为指标工具
# ---------------------------------------------------------------------------


def test_persona_behavior_metrics_returns_only_authorized_profile_usage(server, monkeypatch):
    """persona_behavior_metrics 不读取未迁移的 legacy tracker。"""

    monkeypatch.setattr(
        "core.persona.psyche.get_signal_store",
        lambda: MagicMock(
            get_authorized_profile_usage_metrics=lambda **_kwargs: {
                "schema_version": "mnemos.profile_usage.v1",
                "total_usages": 1,
            }
        ),
    )

    result = server._tool_persona_behavior_metrics(days=7)
    assert result["success"] is True
    assert result["tracking_status"] == "legacy_tracker_acl_unavailable"
    assert result["profile_usage"]["total_usages"] == 1


def test_persona_behavior_metrics_profile_usage_lock_fails_closed(
    server,
    monkeypatch,
    tmp_path,
):
    """profile_usage 读取锁超时时，不能回退读取未授权 legacy 指标。"""

    def locked_signal_store():
        raise sqlite3.OperationalError(
            "sqlite lock timeout for user_signals.db"
        )

    monkeypatch.setattr("core.persona.psyche.get_signal_store", locked_signal_store)

    result = server._tool_persona_behavior_metrics(days=7)

    assert result["success"] is False
    assert "lock timeout" in result["error"]


def test_persona_behavior_metrics_never_opens_legacy_tracker(server, monkeypatch):
    """legacy BehaviorPromptTracker 不再是画像读取回退。"""

    class BadTracker:
        def get_metrics(self, days):
            raise RuntimeError("db locked")

    monkeypatch.setattr("core.persona.behavior_tracker.BehaviorPromptTracker", BadTracker)
    monkeypatch.setattr(
        "core.persona.psyche.get_signal_store",
        lambda: MagicMock(
            get_authorized_profile_usage_metrics=lambda **_kwargs: {
                "schema_version": "mnemos.profile_usage.v1",
                "total_usages": 0,
            }
        ),
    )

    result = server._tool_persona_behavior_metrics(days=7)
    assert result["success"] is True
    assert result["profile_usage"]["total_usages"] == 0
