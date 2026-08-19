from pathlib import Path
import sqlite3

from core.agent_kit.authorization import AgentAuthorizationStore


def test_issued_mcp_capability_resolves_server_principal(tmp_path: Path):
    store = AgentAuthorizationStore(tmp_path / "agent_auth.db")

    credential = store.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities={"memory_read"},
        allowed_projects={"mnemos"},
        allowed_source_agents={"claude"},
    )

    principal = store.resolve_mcp_principal(credential)

    assert principal.agent == "codex"
    assert principal.host_kind == "codex"
    assert principal.source == "server"
    assert principal.capabilities == frozenset({"memory_read"})
    assert principal.allowed_projects == frozenset({"mnemos"})
    assert principal.allowed_source_agents == frozenset({"claude"})


def test_revoked_mcp_capability_no_longer_resolves(tmp_path: Path):
    store = AgentAuthorizationStore(tmp_path / "agent_auth.db")
    credential = store.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities={"memory_read"},
    )

    assert store.revoke_mcp_capability(credential) is True
    assert store.resolve_mcp_principal(credential) is None


def test_expired_mcp_capability_no_longer_resolves(tmp_path: Path):
    store = AgentAuthorizationStore(tmp_path / "agent_auth.db")
    credential = store.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities={"memory_read"},
    )
    capability_id = credential.split(".", 1)[0]
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE mcp_launch_capabilities SET expires_at = ? WHERE capability_id = ?",
            ("2000-01-01T00:00:00+00:00", capability_id),
        )

    assert store.resolve_mcp_principal(credential) is None


def test_revoking_principal_grant_revokes_active_launch_capabilities(tmp_path: Path):
    store = AgentAuthorizationStore(tmp_path / "agent_auth.db")
    store.set_mcp_principal_grant("codex", capabilities={"memory_read"})
    credential = store.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities={"memory_read"},
    )

    revoked = store.revoke_mcp_principal_grant("codex")

    assert revoked == 1
    assert store.get_mcp_principal_grant("codex").state == "revoked"
    assert store.resolve_mcp_principal(credential) is None
