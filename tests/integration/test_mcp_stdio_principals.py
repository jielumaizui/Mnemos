import json
import subprocess
import sys
from pathlib import Path

from core.agent_kit.authorization import AgentAuthorizationStore
from tests.integration.mcp_test_keyring import configure_test_keyring_environment


_STDIO_SERVER = r"""
import os
import sys
from pathlib import Path

from core.access_policy import filter_authorized_items
from core.agent_kit.authorization import AgentAuthorizationStore, MCPLaunchCredentialStore
from integrations.agora import MCPServer

class Facade:
    def wiki_search(self, query, limit=5, *, principal, narrowing):
        items = [
            {
                "page_id": "codex.md",
                "title": "codex",
                "scope": "agent",
                "source_agent": "codex",
                "acl_schema_version": 1,
                "acl_metadata_complete": True,
                "acl_reconciliation_status": "proven",
            },
            {
                "page_id": "claude.md",
                "title": "claude",
                "scope": "agent",
                "source_agent": "claude",
                "acl_schema_version": 1,
                "acl_metadata_complete": True,
                "acl_reconciliation_status": "proven",
            },
        ]
        return filter_authorized_items(items, principal, narrowing)

store = AgentAuthorizationStore(Path(sys.argv[1]), initialize=False)
MCPServer(
    facade=Facade(),
    launch_credential=MCPLaunchCredentialStore().resolve(
        os.environ["MNEMOS_MCP_LAUNCH_CAPABILITY_REF"]
    ),
    authorization_store=store,
).run()
"""


def _run_stdio(tmp_path, db_path, agent, credential):
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "wiki_search",
            "arguments": {"query": "principal boundary"},
        },
    }
    env, _ = configure_test_keyring_environment(
        tmp_path,
        agent=agent,
        credential=credential,
    )
    completed = subprocess.run(
        [sys.executable, "-c", _STDIO_SERVER, str(db_path)],
        input=json.dumps(request) + "\n",
        text=True,
        capture_output=True,
        env=env,
        check=True,
        timeout=10,
    )
    response = json.loads(completed.stdout.strip())
    return json.loads(response["result"]["content"][0]["text"]), completed.stderr


def test_real_stdio_processes_resolve_distinct_server_principals(tmp_path):
    store = AgentAuthorizationStore(tmp_path / "agent_authorization.db")
    codex_credential = store.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities={"memory_read"},
    )
    claude_credential = store.issue_mcp_capability(
        agent="claude",
        host_kind="claude",
        capabilities={"memory_read"},
    )

    codex_payload, codex_stderr = _run_stdio(
        tmp_path / "codex-keyring",
        store.db_path,
        "codex",
        codex_credential,
    )
    claude_payload, claude_stderr = _run_stdio(
        tmp_path / "claude-keyring",
        store.db_path,
        "claude",
        claude_credential,
    )

    assert [item["source_agent"] for item in codex_payload["results"]] == ["codex"]
    assert [item["source_agent"] for item in claude_payload["results"]] == ["claude"]
    assert codex_credential not in codex_stderr
    assert claude_credential not in claude_stderr


def test_real_mnemos_cli_stdio_uses_production_facade_and_authorization(tmp_path):
    store = AgentAuthorizationStore(tmp_path / "agent_authorization.db")
    credential = store.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities={"memory_read"},
    )
    request = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "intent_route",
            "arguments": {"user_input": "修复代码"},
        },
    }
    env, _ = configure_test_keyring_environment(
        tmp_path / "production-keyring",
        agent="codex",
        credential=credential,
    )
    env["MNEMOS_DATABASE_DIR"] = str(tmp_path)

    completed = subprocess.run(
        [sys.executable, "mnemos_cli.py", "mcp", "serve"],
        cwd=str(Path(__file__).resolve().parents[2]),
        input=json.dumps(request, ensure_ascii=False) + "\n",
        text=True,
        capture_output=True,
        env=env,
        check=True,
        timeout=15,
    )

    response = json.loads(completed.stdout.strip())
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["success"] is True
    assert credential not in completed.stdout
    assert credential not in completed.stderr
