import json

from core.agent_kit.authorization import AgentAuthorizationStore
from integrations.agora import MCPServer


def test_tools_call_returns_mcp_call_tool_result_content(tmp_path):
    store = AgentAuthorizationStore(tmp_path / "agent_authorization.db")
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
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "guard_check",
                "arguments": {
                    "user_message": "检查一下风险",
                    "task_type": "coding",
                    "context": {},
                },
            },
        }
    )

    assert response["jsonrpc"] == "2.0"
    assert response["id"] == 1
    assert "error" not in response
    result = response["result"]
    assert list(result.keys()) == ["content"]
    assert result["content"][0]["type"] == "text"
    payload = json.loads(result["content"][0]["text"])
    assert payload["success"] is True


def test_tools_call_unknown_tool_returns_mcp_tool_error_result():
    server = MCPServer()

    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "missing_tool", "arguments": {}},
        }
    )

    assert "error" not in response
    assert response["result"]["isError"] is True
    assert response["result"]["content"][0]["type"] == "text"
    assert "Unknown tool" in response["result"]["content"][0]["text"]
