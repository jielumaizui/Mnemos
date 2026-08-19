import json
import sqlite3
from types import SimpleNamespace

from core.agent_kit.authorization import (
    AgentAuthorizationStore,
    InMemoryMCPLaunchCredentialStore,
)
from integrations.agora import MCPServer, build_mcp_server_from_environment


def _tool_payload(response):
    result = response["result"]
    return result, json.loads(result["content"][0]["text"])


def test_tool_call_without_server_principal_is_denied():
    server = MCPServer()

    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "intent_route",
                "arguments": {},
            },
        }
    )

    result, payload = _tool_payload(response)
    assert result["isError"] is True
    assert payload == {
        "success": False,
        "code": "principal_required",
        "tool": "intent_route",
    }


def test_unavailable_capability_store_fails_closed_without_server_crash(tmp_path):
    broken_db = tmp_path / "agent_auth.db"
    broken_db.write_bytes(b"")
    store = AgentAuthorizationStore(broken_db, initialize=False)

    server = MCPServer(
        launch_credential="missing.secret",
        authorization_store=store,
    )
    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 19,
            "method": "tools/call",
            "params": {"name": "health_check", "arguments": {}},
        }
    )

    result, payload = _tool_payload(response)
    assert result["isError"] is True
    assert payload["code"] == "principal_required"


def test_valid_launch_capability_authorizes_matching_tool(tmp_path):
    store = AgentAuthorizationStore(tmp_path / "agent_auth.db")
    credential = store.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities={"memory_read"},
    )
    server = MCPServer(
        launch_credential=credential,
        authorization_store=store,
    )

    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "intent_route",
                "arguments": {"user_input": "修复代码"},
            },
        }
    )

    result, payload = _tool_payload(response)
    assert "isError" not in result
    assert payload["success"] is True
    assert payload["intent"] == "task"


def test_running_server_denies_calls_after_launch_capability_revocation(tmp_path):
    calls = []

    class Facade:
        def health_check(self):
            calls.append("health_check")
            return {"success": True}

    store = AgentAuthorizationStore(tmp_path / "agent_auth.db")
    credential = store.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities={"public_metadata"},
    )
    server = MCPServer(
        facade=Facade(),
        launch_credential=credential,
        authorization_store=store,
    )
    request = {
        "jsonrpc": "2.0",
        "id": 20,
        "method": "tools/call",
        "params": {"name": "health_check", "arguments": {}},
    }

    first = server.handle_request(request)
    assert "isError" not in first["result"]
    assert store.revoke_mcp_capability(credential) is True
    second = server.handle_request(request)
    result, payload = _tool_payload(second)

    assert result["isError"] is True
    assert payload["code"] == "principal_revoked_or_expired"
    assert calls == ["health_check"]


def test_legacy_plaintext_environment_launch_credential_is_ignored(
    tmp_path,
    monkeypatch,
):
    store = AgentAuthorizationStore(tmp_path / "agent_auth.db")
    credential = store.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities={"memory_read"},
    )
    monkeypatch.setenv("MNEMOS_MCP_LAUNCH_CAPABILITY", credential)
    monkeypatch.setattr(
        "core.config.get_config",
        lambda: SimpleNamespace(
            get_runtime_environment=lambda key, default="": (
                credential
                if key == "MNEMOS_MCP_LAUNCH_CAPABILITY"
                else default
            )
        ),
    )

    server = build_mcp_server_from_environment(authorization_store=store)
    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "intent_route",
                "arguments": {"user_input": "修复代码"},
            },
        }
    )

    result, payload = _tool_payload(response)
    assert result["isError"] is True
    assert payload["code"] == "principal_required"


def test_schema_hidden_argument_is_denied_before_handler_side_effect(tmp_path):
    calls = []

    class Facade:
        def freshness_check(self, entity_name, auto_refresh=False):
            calls.append((entity_name, auto_refresh))
            return {"success": True}

    store = AgentAuthorizationStore(tmp_path / "agent_auth.db")
    credential = store.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities={"memory_read"},
    )
    server = MCPServer(
        facade=Facade(),
        launch_credential=credential,
        authorization_store=store,
    )

    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 31,
            "method": "tools/call",
            "params": {
                "name": "freshness_check",
                "arguments": {"entity_name": "Python", "auto_refresh": True},
            },
        }
    )

    result, payload = _tool_payload(response)
    assert result["isError"] is True
    assert payload["code"] == "unknown_arguments"
    assert payload["arguments"] == ["auto_refresh"]
    assert calls == []


def test_environment_launch_reference_builds_authenticated_server(
    tmp_path,
    monkeypatch,
):
    store = AgentAuthorizationStore(tmp_path / "agent_auth.db")
    credential = store.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities={"memory_read"},
    )
    secret_store = InMemoryMCPLaunchCredentialStore()
    reference = secret_store.store("codex", credential)
    monkeypatch.delenv("MNEMOS_MCP_LAUNCH_CAPABILITY", raising=False)
    monkeypatch.setenv("MNEMOS_MCP_LAUNCH_CAPABILITY_REF", reference)
    monkeypatch.setattr(
        "core.config.get_config",
        lambda: SimpleNamespace(get_runtime_environment=lambda _key, default="": default),
    )

    server = build_mcp_server_from_environment(
        authorization_store=store,
        credential_store=secret_store,
    )
    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 30,
            "method": "tools/call",
            "params": {
                "name": "intent_route",
                "arguments": {"user_input": "修复代码"},
            },
        }
    )

    result, payload = _tool_payload(response)
    assert "isError" not in result
    assert payload["success"] is True


def test_config_runtime_reference_remains_supported_for_embedded_server(
    tmp_path,
    monkeypatch,
):
    store = AgentAuthorizationStore(tmp_path / "agent_auth.db")
    credential = store.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities={"memory_read"},
    )
    secret_store = InMemoryMCPLaunchCredentialStore()
    reference = secret_store.store("codex", credential)
    monkeypatch.delenv("MNEMOS_MCP_LAUNCH_CAPABILITY_REF", raising=False)
    monkeypatch.setattr(
        "core.config.get_config",
        lambda: SimpleNamespace(
            get_runtime_environment=lambda key, default="": (
                reference
                if key == "MNEMOS_MCP_LAUNCH_CAPABILITY_REF"
                else default
            )
        ),
    )

    server = build_mcp_server_from_environment(
        authorization_store=store,
        credential_store=secret_store,
    )
    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 31,
            "method": "tools/call",
            "params": {
                "name": "intent_route",
                "arguments": {"user_input": "修复代码"},
            },
        }
    )

    result, payload = _tool_payload(response)
    assert "isError" not in result
    assert payload["success"] is True


def test_ordinary_mcp_call_does_not_forge_runtime_probe_receipt(tmp_path, monkeypatch):
    class Facade:
        def intent_route(self, user_input, working_dir=None):
            return {"success": True, "intent": user_input}

    receipt_store_constructed = []

    class UnexpectedReceiptStore:
        def __init__(self):
            receipt_store_constructed.append(True)

    monkeypatch.setattr(
        "core.agent_kit.runtime_receipts.AgentRuntimeReceiptStore",
        UnexpectedReceiptStore,
    )
    store = AgentAuthorizationStore(tmp_path / "agent_auth.db")
    credential = store.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities={"memory_read"},
    )
    server = MCPServer(facade=Facade(), launch_credential=credential, authorization_store=store)

    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 32,
            "method": "tools/call",
            "params": {
                "name": "intent_route",
                "arguments": {"user_input": "修复代码"},
            },
        }
    )

    result, payload = _tool_payload(response)
    assert "isError" not in result
    assert payload["success"] is True
    assert receipt_store_constructed == []


def test_recap_object_operations_require_the_server_principal_owner(
    tmp_path,
    monkeypatch,
):
    config = SimpleNamespace(
        database_dir=tmp_path / "db",
        wiki_dir=tmp_path / "wiki",
        get=lambda _key, default=None: default,
    )
    config.database_dir.mkdir(parents=True)
    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setattr(
        "core.app.retrospective_session_manager.get_config",
        lambda: config,
    )
    monkeypatch.setattr("core.app.forced_retrospective.get_config", lambda: config)
    store = AgentAuthorizationStore(tmp_path / "agent_auth.db")
    codex_credential = store.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities={"memory_read", "feedback_write"},
    )
    claude_credential = store.issue_mcp_capability(
        agent="claude",
        host_kind="claude",
        capabilities={"memory_read", "feedback_write"},
    )
    codex = MCPServer(
        launch_credential=codex_credential,
        authorization_store=store,
    )
    claude = MCPServer(
        launch_credential=claude_credential,
        authorization_store=store,
    )

    started_response = codex.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 40,
            "method": "tools/call",
            "params": {
                "name": "recap_start",
                "arguments": {"topic": "principal-owned recap"},
            },
        }
    )
    _, started = _tool_payload(started_response)
    recap_id = started["recap_id"]

    denied_payloads = []
    for request_id, name, arguments in (
        (41, "recap_status", {"recap_id": recap_id}),
        (42, "recap_feedback", {"recap_id": recap_id, "feedback_type": "useful"}),
        (43, "recap_finalize", {"recap_id": recap_id}),
    ):
        response = claude.handle_request(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        _, payload = _tool_payload(response)
        denied_payloads.append(payload)

    assert all(payload["success"] is False for payload in denied_payloads)
    assert all(payload["error"] == "owner_conflict" for payload in denied_payloads)
    assert all(payload["owner_agent"] == "codex" for payload in denied_payloads)
    with sqlite3.connect(config.database_dir / "recap_tasks.db") as conn:
        feedback_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recap_feedback_events'"
        ).fetchone()
    assert feedback_table is None


def test_authenticated_context_search_uses_server_principal(
    tmp_path,
    monkeypatch,
):
    wiki_dir = tmp_path / "wiki"
    page = wiki_dir / "03-Tech" / "private.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        """---
scope: private
source_agent: codex
session_id: session-1
project: mnemos
acl_schema_version: 1
acl_metadata_complete: true
acl_reconciliation_status: proven
置信度: 0.9
---
MCP-SERVER-PRINCIPAL-SENTINEL
""",
        encoding="utf-8",
    )
    config = SimpleNamespace(
        wiki_dir=wiki_dir,
        data_dir=tmp_path,
        database_dir=tmp_path / "db",
        get=lambda _key, default=None: default,
    )
    # Import and patch the lazily reached KG module explicitly so pytest does
    # not leave a module-level binding to this test's temporary config after
    # the core.config monkeypatch is restored.
    from core.kia import knowledge_graph as knowledge_graph_module

    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setattr("core.app.context_search.get_config", lambda: config)
    monkeypatch.setattr(knowledge_graph_module, "get_config", lambda: config)
    store = AgentAuthorizationStore(tmp_path / "agent_auth.db")
    credential = store.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities={"memory_read"},
        allowed_projects={"mnemos"},
    )
    server = MCPServer(
        launch_credential=credential,
        authorization_store=store,
    )

    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "context_aware_search",
                "arguments": {
                    "query": "MCP-SERVER-PRINCIPAL-SENTINEL",
                    "session_id": "session-1",
                    "project": "mnemos",
                },
            },
        }
    )

    result, payload = _tool_payload(response)
    assert "isError" not in result
    assert payload["success"] is True
    assert payload["count"] == 1
    assert payload["results"][0]["source_agent"] == "codex"


def test_authenticated_session_search_filters_with_server_principal(
    tmp_path,
    monkeypatch,
):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "codex.md").write_text(
        """---
session_id: session-1
date: 2026-07-10
source: codex
source_agent: codex
project: mnemos
scope: private
acl_schema_version: 1
acl_metadata_complete: true
acl_reconciliation_status: provenance_write
time: 10:00
---
SESSIONAUTH same agent evidence
""",
        encoding="utf-8",
    )
    (raw_dir / "claude.md").write_text(
        """---
session_id: session-1
date: 2026-07-10
source: claude
source_agent: claude
project: mnemos
scope: private
acl_schema_version: 1
acl_metadata_complete: true
acl_reconciliation_status: provenance_write
time: 10:01
---
SESSIONAUTH cross agent evidence
""",
        encoding="utf-8",
    )

    class Config:
        obsidian_vault_path = raw_dir
        wiki_dir = tmp_path / "wiki"
        database_dir = tmp_path / "db"

        @staticmethod
        def get(key, default=None):
            values = {
                "raw_event_store.enabled": True,
                "raw_event_store.db_path": str(tmp_path / "db" / "raw_events.db"),
            }
            return values.get(key, default)

    monkeypatch.setattr("core.config.get_config", lambda: Config())
    from core.app.raw_search import RawIndex
    from core.sync_framework.raw_event_store import RawEventStore

    canonical_store = RawEventStore(config=Config())
    canonical_store.upsert_turn(
        source_agent="codex",
        session_id="session-1",
        turn_number=0,
        user_content="SESSIONAUTH same agent evidence",
        assistant_content="same agent answer",
    )
    canonical_store.upsert_turn(
        source_agent="claude",
        session_id="session-1",
        turn_number=1,
        user_content="SESSIONAUTH cross agent evidence",
        assistant_content="cross agent answer",
    )
    canonical_store.close()

    index = RawIndex(
        raw_dir=raw_dir,
        db_path=Config.database_dir / "raw_index.db",
        config=Config(),
    )
    index.sync_index()
    index.close()
    store = AgentAuthorizationStore(tmp_path / "agent_auth.db")
    credential = store.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities={"memory_read"},
        allowed_projects={"mnemos"},
    )
    server = MCPServer(
        launch_credential=credential,
        authorization_store=store,
    )

    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "session_search",
                "arguments": {
                    "query": "SESSIONAUTH",
                    "session_id": "session-1",
                    "project": "mnemos",
                },
            },
        }
    )

    result, payload = _tool_payload(response)
    assert "isError" not in result
    assert payload["success"] is True
    assert payload["count"] == 1
    assert payload["results"][0]["source_agent"] == "codex"
    assert payload["access_filter"]["private_cross_agent_denied"] == 1


def test_preflight_and_guard_do_not_use_unscoped_retrospective_injector(
    tmp_path,
    monkeypatch,
):
    wiki_dir = tmp_path / "wiki"
    page = wiki_dir / "06-Retrospectives" / "coding" / "private-v1.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        """---
type: retrospective
scope: private
source_agent: claude
session_id: session-1
acl_schema_version: 1
acl_metadata_complete: true
acl_reconciliation_status: proven
---
UNAUTHORIZED-PREFLIGHT-SENTINEL
""",
        encoding="utf-8",
    )

    config = SimpleNamespace(
        database_dir=tmp_path / "db",
        wiki_dir=wiki_dir,
        get=lambda _key, default=None: default,
    )

    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setattr("core.app.context_search.get_config", lambda: config)

    def fail_unscoped_injector(*_args, **_kwargs):
        raise AssertionError("MCP preflight/guard must not instantiate unscoped injector")

    monkeypatch.setattr(
        "core.kia.prophasis.PreFlightInjector.__init__",
        fail_unscoped_injector,
    )
    store = AgentAuthorizationStore(tmp_path / "agent_auth.db")
    credential = store.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities={"memory_read"},
    )
    server = MCPServer(
        launch_credential=credential,
        authorization_store=store,
    )

    preflight = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "preflight_inject",
                "arguments": {"task_type": "coding"},
            },
        }
    )
    guard = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "guard_check",
                "arguments": {"user_message": "review code", "task_type": "coding"},
            },
        }
    )
    _, preflight_payload = _tool_payload(preflight)
    _, guard_payload = _tool_payload(guard)

    assert preflight_payload["success"] is True
    assert "UNAUTHORIZED-PREFLIGHT-SENTINEL" not in json.dumps(preflight_payload)
    assert guard_payload["success"] is True
    assert "UNAUTHORIZED-PREFLIGHT-SENTINEL" not in json.dumps(guard_payload)
