from types import SimpleNamespace

from core.access_policy import MCP_TOOL_POLICIES
from core.agent_kit.authorization import AgentAuthorizationStore
from core.cli.commands.agent import _cmd_agent_mcp_grant


def test_agent_mcp_grant_command_persists_explicit_server_grant(tmp_path, capsys):
    db_path = tmp_path / "agent_authorization.db"
    args = SimpleNamespace(
        agent_name="codex",
        db_path=str(db_path),
        capability=[],
        all_tools=True,
        project=[],
        all_projects=True,
        source_agent=["claude", "kimi"],
        revoke=False,
        json=True,
    )

    assert _cmd_agent_mcp_grant(args) is True

    grant = AgentAuthorizationStore(db_path).get_mcp_principal_grant("codex")
    assert grant.state == "active"
    assert grant.capabilities == frozenset(MCP_TOOL_POLICIES.values())
    assert grant.allowed_projects == frozenset({"*"})
    assert grant.allowed_source_agents == frozenset({"claude", "kimi"})
    output = capsys.readouterr().out
    assert "secret" not in output.lower()
    assert "credential" not in output.lower()


def test_agent_mcp_grant_command_rejects_unknown_capability(tmp_path):
    args = SimpleNamespace(
        agent_name="codex",
        db_path=str(tmp_path / "agent_authorization.db"),
        capability=["unregistered_policy"],
        all_tools=False,
        project=[],
        all_projects=False,
        source_agent=[],
        revoke=False,
        json=True,
    )

    try:
        _cmd_agent_mcp_grant(args)
    except ValueError as exc:
        assert "unsupported MCP capabilities" in str(exc)
    else:
        raise AssertionError("unknown capability must fail closed")


def test_agent_mcp_grant_revoke_invalidates_active_launches(tmp_path, capsys):
    db_path = tmp_path / "agent_authorization.db"
    store = AgentAuthorizationStore(db_path)
    store.set_mcp_principal_grant("codex", capabilities={"memory_read"})
    credential = store.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities={"memory_read"},
    )
    args = SimpleNamespace(
        agent_name="codex",
        db_path=str(db_path),
        capability=[],
        all_tools=False,
        project=[],
        all_projects=False,
        source_agent=[],
        revoke=True,
        json=True,
    )

    assert _cmd_agent_mcp_grant(args) is True

    assert store.resolve_mcp_principal(credential) is None
    grant = store.get_mcp_principal_grant("codex")
    assert grant.state == "revoked"
    payload = capsys.readouterr().out
    assert '"revoked_launches": 1' in payload
    assert credential not in payload


def test_active_grant_update_immediately_invalidates_stale_launch(tmp_path):
    db_path = tmp_path / "agent_authorization.db"
    store = AgentAuthorizationStore(db_path)
    store.set_mcp_principal_grant(
        "codex",
        capabilities=set(MCP_TOOL_POLICIES.values()),
    )
    credential = store.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities=set(MCP_TOOL_POLICIES.values()),
    )

    store.set_mcp_principal_grant(
        "codex",
        capabilities={"public_metadata"},
    )

    assert store.resolve_mcp_principal(credential) is None
    assert store.get_mcp_principal_grant("codex").capabilities == frozenset(
        {"public_metadata"}
    )
