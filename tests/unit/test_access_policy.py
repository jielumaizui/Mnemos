from core.access_policy import AccessContext, can_read_item, filter_readable_items


def test_private_cross_agent_is_denied():
    item = {
        "page_id": "raw/codex/private.md",
        "source": "codex",
        "tags": ["scope=private", "session=s1"],
    }

    decision = can_read_item(item, AccessContext(agent="hermes", session_id="s1"))

    assert decision.allowed is False
    assert decision.reason == "private_cross_agent_denied"


def test_private_requires_agent_identity():
    item = {
        "page_id": "raw/codex/private.md",
        "source": "codex",
        "tags": ["scope=private", "session=s1"],
    }

    decision = can_read_item(item, AccessContext())

    assert decision.allowed is False
    assert decision.reason == "missing_agent_identity"


def test_private_same_agent_requires_session_match_when_tagged():
    item = {
        "page_id": "raw/codex/private.md",
        "source": "codex",
        "tags": ["scope=private", "session=s1"],
    }

    allowed = can_read_item(item, AccessContext(agent="codex", session_id="s1"))
    denied = can_read_item(item, AccessContext(agent="codex", session_id="s2"))

    assert allowed.allowed is True
    assert denied.allowed is False
    assert denied.reason == "private_session_mismatch"


def test_private_same_agent_requires_session_context_when_tagged():
    item = {
        "page_id": "raw/codex/private.md",
        "source": "codex",
        "tags": ["scope=private", "session=s1"],
    }

    decision = can_read_item(item, AccessContext(agent="codex"))

    assert decision.allowed is False
    assert decision.reason == "private_session_requires_context"


def test_cross_agent_public_requires_explicit_authorization():
    item = {"page_id": "notes/public.md", "source": "codex", "tags": ["scope=public"]}

    denied = can_read_item(item, AccessContext(agent="hermes"))
    allowed = can_read_item(
        item,
        AccessContext(
            agent="hermes",
            allow_cross_agent=True,
            authorized_agents=frozenset({"codex"}),
        ),
    )

    assert denied.allowed is False
    assert denied.reason == "cross_agent_requires_authorization"
    assert allowed.allowed is True
    assert allowed.reason == "cross_agent_authorized"


def test_global_scope_with_agent_provenance_still_requires_cross_agent_grant():
    item = {
        "page_id": "scopes/global/rule.md",
        "scope": "global",
        "source_agent": "codex",
    }

    denied = can_read_item(item, AccessContext(agent="claude"))
    allowed = can_read_item(
        item,
        AccessContext(
            agent="claude",
            allow_cross_agent=True,
            authorized_agents=frozenset({"codex"}),
        ),
    )

    assert denied.reason == "cross_agent_requires_authorization"
    assert allowed.allowed is True


def test_missing_agent_only_reads_unscoped_public_or_global_items():
    items = [
        {"page_id": "public.md", "tags": ["scope=public"]},
        {"page_id": "scopes/global/rule.md"},
        {"page_id": "source-public.md", "source": "codex", "tags": ["scope=public"]},
        {"page_id": "private.md", "tags": ["scope=private"]},
        {"page_id": "project.md", "tags": ["scope=project", "project=mnemos"]},
        {"page_id": "framework.md", "tags": ["scope=framework"]},
    ]

    readable, summary = filter_readable_items(items, AccessContext())

    assert [item["page_id"] for item in readable] == ["public.md", "scopes/global/rule.md"]
    assert summary["allowed"] == 2
    assert summary["missing_agent_identity"] == 4


def test_project_scope_requires_project_context():
    item = {"page_id": "scopes/project/mnemos/foo.md", "tags": ["scope=project"]}

    missing_project = can_read_item(item, AccessContext(agent="codex"))
    allowed = can_read_item(item, AccessContext(agent="codex", project="mnemos"))

    assert missing_project.allowed is False
    assert missing_project.reason == "project_scope_requires_context"
    assert allowed.allowed is True


def test_filter_readable_items_returns_reason_summary():
    items = [
        {"page_id": "a.md", "source": "codex", "tags": ["scope=public"]},
        {"page_id": "b.md", "source": "hermes", "tags": ["scope=public"]},
    ]

    readable, summary = filter_readable_items(items, AccessContext(agent="hermes"))

    assert [item["page_id"] for item in readable] == ["b.md"]
    assert summary["cross_agent_requires_authorization"] == 1
    assert summary["allowed"] == 1
