import inspect
import pytest

from core.access_policy import (
    AccessNarrowing,
    MCP_TOOL_POLICIES,
    PrincipalEnvelope,
    authorize_item,
    authorize_tool_call,
    bind_write_acl,
)
from integrations.agora_tools.schema import list_tools
from integrations.agora import MCPServer


def test_tool_authorization_rejects_caller_identity_override():
    principal = PrincipalEnvelope(
        principal_id="mcp:codex:capability",
        agent="codex",
        host_kind="codex",
        capability_id="capability",
        capabilities=frozenset({"memory_read"}),
    )

    decision = authorize_tool_call(
        principal,
        "wiki_search",
        {
            "query": "private notes",
            "agent": "claude",
            "allow_cross_agent": True,
            "authorized_agents": [],
        },
    )

    assert decision.allowed is False
    assert decision.reason == "caller_identity_override_forbidden"
    assert decision.arguments == {}


def test_tool_authorization_rejects_project_outside_server_grant():
    principal = PrincipalEnvelope(
        principal_id="mcp:codex:capability",
        agent="codex",
        host_kind="codex",
        capability_id="capability",
        capabilities=frozenset({"memory_read"}),
        allowed_projects=frozenset({"mnemos"}),
    )

    decision = authorize_tool_call(
        principal,
        "wiki_search",
        {"query": "private notes", "project": "other"},
    )

    assert decision.allowed is False
    assert decision.reason == "principal_project_grant_missing"


def test_tool_authorization_rejects_incomplete_scoped_write_context():
    principal = PrincipalEnvelope(
        principal_id="mcp:codex:capability",
        agent="codex",
        host_kind="codex",
        capability_id="capability",
        capabilities=frozenset({"memory_write"}),
        allowed_projects=frozenset({"mnemos"}),
    )

    private = authorize_tool_call(
        principal,
        "wiki_write",
        {
            "page_path": "00-Inbox/private.md",
            "content": "private",
            "frontmatter": {"scope": "private"},
        },
    )
    project = authorize_tool_call(
        principal,
        "wiki_write",
        {
            "page_path": "00-Inbox/project.md",
            "content": "project",
            "frontmatter": {"scope": "project"},
        },
    )

    assert private.reason == "private_scope_session_required"
    assert project.reason == "project_scope_project_required"


def test_tool_authorization_rejects_nested_acl_identity_override():
    principal = PrincipalEnvelope(
        principal_id="mcp:codex:capability",
        agent="codex",
        host_kind="codex",
        capability_id="capability",
        capabilities=frozenset({"memory_write"}),
    )

    decision = authorize_tool_call(
        principal,
        "wiki_write",
        {
            "page_path": "00-Inbox/test.md",
            "content": "test",
            "frontmatter": {"source_agent": "claude"},
        },
    )

    assert decision.allowed is False
    assert decision.reason == "caller_acl_override_forbidden"


def test_tool_authorization_rejects_ingest_acl_tag_override():
    principal = PrincipalEnvelope(
        principal_id="mcp:codex:capability",
        agent="codex",
        host_kind="codex",
        capability_id="capability",
        capabilities=frozenset({"memory_write"}),
    )

    decision = authorize_tool_call(
        principal,
        "knowledge_ingest",
        {"content": "note", "tags": ["topic=demo", "source=claude"]},
    )

    assert decision.allowed is False
    assert decision.reason == "caller_acl_override_forbidden"


def test_tool_authorization_rejects_write_acl_aliases_and_tags():
    principal = PrincipalEnvelope(
        principal_id="mcp:codex:capability",
        agent="codex",
        host_kind="codex",
        capability_id="capability",
        capabilities=frozenset({"memory_write"}),
    )

    for frontmatter in (
        {"source": "claude"},
        {"source_session": "forged-session"},
        {"tags": ["source=claude"]},
        {"tags": ["scope:global"]},
    ):
        decision = authorize_tool_call(
            principal,
            "wiki_write",
            {
                "page_path": "03-Tech/page.md",
                "content": "body",
                "frontmatter": frontmatter,
            },
        )

        assert decision.allowed is False
        assert decision.reason == "caller_acl_override_forbidden"


def test_write_acl_is_bound_to_server_principal():
    principal = PrincipalEnvelope(
        principal_id="mcp:codex:capability",
        agent="codex",
        host_kind="codex",
        capability_id="capability",
        capabilities=frozenset({"memory_write"}),
        allowed_projects=frozenset({"mnemos"}),
    )

    frontmatter = bind_write_acl(
        principal,
        {"title": "Test", "scope": "project", "project": "mnemos"},
    )

    assert frontmatter["source_agent"] == "codex"
    assert frontmatter["scope"] == "project"
    assert frontmatter["project"] == "mnemos"
    assert frontmatter["acl_schema_version"] == 1
    assert frontmatter["acl_metadata_complete"] is True
    assert frontmatter["acl_reconciliation_status"] == "server_principal"


def test_private_write_acl_requires_and_binds_request_session():
    principal = PrincipalEnvelope(
        principal_id="mcp:codex:capability",
        agent="codex",
        host_kind="codex",
        capability_id="capability",
        capabilities=frozenset({"memory_write"}),
    )

    with pytest.raises(ValueError, match="private_scope_session_required"):
        bind_write_acl(principal, {"scope": "private"})

    frontmatter = bind_write_acl(
        principal,
        {"scope": "private"},
        session_id="session-1",
    )
    assert frontmatter["scope"] == "private"
    assert frontmatter["session_id"] == "session-1"


def test_write_acl_rejects_scope_conflicting_with_scoped_path():
    principal = PrincipalEnvelope(
        principal_id="mcp:codex:capability",
        agent="codex",
        host_kind="codex",
        capability_id="capability",
        capabilities=frozenset({"memory_write"}),
        allowed_projects=frozenset({"mnemos"}),
    )

    with pytest.raises(ValueError, match="acl_path_scope_conflict"):
        bind_write_acl(
            principal,
            {"scope": "agent"},
            page_path="scopes/project/mnemos/page.md",
        )

    frontmatter = bind_write_acl(
        principal,
        {},
        page_path="scopes/project/mnemos/page.md",
    )
    assert frontmatter["scope"] == "project"
    assert frontmatter["project"] == "mnemos"


def test_every_public_mcp_tool_has_exactly_one_policy():
    listed = list_tools(lambda _name: "advanced")["tools"]
    public_tool_names = {tool["name"] for tool in listed}

    assert len(public_tool_names) == 57
    assert set(MCP_TOOL_POLICIES) == public_tool_names
    assert MCP_TOOL_POLICIES["persona_record_explicit_evidence"] == "memory_write"


def test_reflection_tools_require_write_capability():
    assert MCP_TOOL_POLICIES["reflect_on_input"] == "memory_write"
    assert MCP_TOOL_POLICIES["reflect_manually"] == "memory_write"
    assert MCP_TOOL_POLICIES["wiki_build"] == "admin_runtime"
    assert MCP_TOOL_POLICIES["document_process"] == "admin_runtime"


def test_server_startup_fails_when_handler_policy_registry_drifts(monkeypatch):
    original = MCPServer._register_tools

    def with_unregistered_handler(self):
        tools = original(self)
        tools["unregistered_tool"] = lambda: {}
        return tools

    monkeypatch.setattr(MCPServer, "_register_tools", with_unregistered_handler)

    with pytest.raises(RuntimeError, match="tool policy registry mismatch"):
        MCPServer()


def test_public_tool_schemas_do_not_expose_identity_or_grant_overrides():
    listed = list_tools(lambda _name: "advanced")["tools"]
    forbidden = {
        "agent",
        "allow_cross_agent",
        "authorized_agents",
        "source_agent",
        "source_agents",
        "owner_agent",
    }

    exposed = {
        tool["name"]: sorted(
            forbidden.intersection(tool["inputSchema"].get("properties", {}))
        )
        for tool in listed
        if forbidden.intersection(tool["inputSchema"].get("properties", {}))
    }

    assert exposed == {}


def test_write_provenance_fields_are_server_derived():
    listed = {
        tool["name"]: tool["inputSchema"].get("properties", {})
        for tool in list_tools(lambda _name: "advanced")["tools"]
    }

    assert "source" not in listed["knowledge_ingest"]
    assert "source_agents" not in listed["recap_start"]
    assert "source" not in inspect.signature(MCPServer._tool_knowledge_ingest).parameters
    assert "source_agents" not in inspect.signature(MCPServer._tool_recap_start).parameters


def test_capture_tool_schemas_bind_source_agent_to_server_principal():
    listed = {tool["name"]: tool for tool in list_tools(lambda _name: "advanced")["tools"]}

    exposed = {
        name: listed[name]["inputSchema"].get("properties", {}).get("source_agent")
        for name in (
            "capture_turn",
            "capture_session",
            "end_session",
            "capture_status",
            "session_save",
        )
        if "source_agent" in listed[name]["inputSchema"].get("properties", {})
    }

    assert exposed == {}


def test_memory_and_session_handlers_do_not_accept_caller_identity_overrides():
    forbidden = {"agent", "allow_cross_agent", "authorized_agents"}

    exposed = {
        name: sorted(forbidden.intersection(inspect.signature(handler).parameters))
        for name, handler in {
            "memory_search": MCPServer._tool_memory_search,
            "session_search": MCPServer._tool_session_search,
        }.items()
        if forbidden.intersection(inspect.signature(handler).parameters)
    }

    assert exposed == {}


def test_server_principal_without_cross_agent_grant_denies_other_agent_item():
    principal = PrincipalEnvelope(
        principal_id="mcp:codex:capability",
        agent="codex",
        host_kind="codex",
        capability_id="capability",
        capabilities=frozenset({"memory_read"}),
    )

    decision = authorize_item(
        principal,
        {
            "scope": "public",
            "source_agent": "claude",
            "acl_schema_version": 1,
            "acl_metadata_complete": True,
            "acl_reconciliation_status": "proven",
        },
        AccessNarrowing(),
    )

    assert decision.allowed is False
    assert decision.reason == "cross_agent_requires_authorization"


def test_server_principal_denies_item_without_complete_acl_metadata():
    principal = PrincipalEnvelope(
        principal_id="mcp:codex:capability",
        agent="codex",
        host_kind="codex",
        capability_id="capability",
        capabilities=frozenset({"memory_read"}),
    )

    decision = authorize_item(
        principal,
        {"page_id": "notes/legacy-page"},
        AccessNarrowing(),
    )

    assert decision.allowed is False
    assert decision.reason == "acl_metadata_missing"


def test_complete_flag_without_acl_schema_or_provenance_fails_closed():
    principal = PrincipalEnvelope(
        principal_id="mcp:codex:capability",
        agent="codex",
        host_kind="codex",
        capability_id="capability",
        capabilities=frozenset({"memory_read"}),
    )

    missing_schema = authorize_item(
        principal,
        {"scope": "agent", "source_agent": "codex", "acl_metadata_complete": True},
        AccessNarrowing(),
    )
    missing_provenance = authorize_item(
        principal,
        {
            "scope": "agent",
            "acl_schema_version": 1,
            "acl_metadata_complete": True,
            "acl_reconciliation_status": "proven",
        },
        AccessNarrowing(),
    )

    assert missing_schema.reason == "acl_schema_unsupported"
    assert missing_provenance.reason == "acl_provenance_missing"


def test_conflicting_acl_fields_fail_closed():
    principal = PrincipalEnvelope(
        principal_id="mcp:codex:capability",
        agent="codex",
        host_kind="codex",
        capability_id="capability",
        capabilities=frozenset({"memory_read"}),
    )

    decision = authorize_item(
        principal,
        {
            "scope": "agent",
            "source_agent": "codex",
            "acl_schema_version": 1,
            "acl_metadata_complete": True,
            "acl_reconciliation_status": "proven",
            "metadata": {"scope": "private", "source_agent": "codex"},
        },
        AccessNarrowing(),
    )

    assert decision.reason == "acl_metadata_conflict"


def test_conflicting_source_alias_fails_closed():
    principal = PrincipalEnvelope(
        principal_id="mcp:codex:capability",
        agent="codex",
        host_kind="codex",
        capability_id="capability",
        capabilities=frozenset({"memory_read"}),
    )

    decision = authorize_item(
        principal,
        {
            "scope": "agent",
            "source_agent": "codex",
            "source": "claude",
            "acl_schema_version": 1,
            "acl_metadata_complete": True,
            "acl_reconciliation_status": "proven",
        },
        AccessNarrowing(),
    )

    assert decision.reason == "acl_metadata_conflict"


def test_server_principal_denies_reconciled_unknown_item():
    principal = PrincipalEnvelope(
        principal_id="mcp:codex:capability",
        agent="codex",
        host_kind="codex",
        capability_id="capability",
        capabilities=frozenset({"memory_read"}),
    )

    decision = authorize_item(
        principal,
        {
            "scope": "restricted",
            "acl_schema_version": 1,
            "acl_metadata_complete": True,
            "acl_reconciliation_status": "restricted_unknown",
        },
        AccessNarrowing(),
    )

    assert decision.allowed is False
    assert decision.reason == "acl_reconciliation_required"
