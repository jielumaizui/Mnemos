import json
import subprocess
import sys
from pathlib import Path

from core.agent_kit.authorization import AgentAuthorizationStore
from tests.integration.mcp_test_keyring import configure_test_keyring_environment


def test_mcp_stdio_tools_call_returns_call_tool_result(tmp_path):
    store = AgentAuthorizationStore(tmp_path / "agent_authorization.db")
    credential = store.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities={"memory_read"},
    )
    env, _ = configure_test_keyring_environment(
        tmp_path,
        agent="codex",
        credential=credential,
    )
    env["MNEMOS_DATABASE_DIR"] = str(tmp_path)
    proc = subprocess.Popen(
        [sys.executable, "mnemos_cli.py", "mcp", "serve"],
        cwd=Path(__file__).resolve().parents[2],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "guard_check",
                    "arguments": {
                        "user_message": "检查风险",
                        "task_type": "coding",
                        "context": {},
                    },
                },
            },
        ]
        responses = []
        for request in requests:
            proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            responses.append(json.loads(proc.stdout.readline()))

        assert responses[0]["result"]["serverInfo"]["name"] == "mnemos-mcp-server"
        call_result = responses[1]["result"]
        assert "error" not in responses[1]
        assert call_result["content"][0]["type"] == "text"
        payload = json.loads(call_result["content"][0]["text"])
        assert payload["success"] is True
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
