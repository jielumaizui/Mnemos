import json
import logging
from pathlib import Path
from types import SimpleNamespace

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.application.facade import DefaultMnemosServiceFacade


def test_cross_agent_private_page_is_denied_before_content_read(
    tmp_path,
    monkeypatch,
):
    wiki_dir = tmp_path / "wiki"
    page = wiki_dir / "03-Tech" / "private.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        """---
scope: private
source_agent: claude
session_id: session-1
project: mnemos
acl_schema_version: 1
acl_metadata_complete: true
acl_reconciliation_status: proven
---
PRIVATE-CONTENT-SENTINEL
""",
        encoding="utf-8",
    )
    config = SimpleNamespace(
        wiki_dir=wiki_dir,
        database_dir=tmp_path / "db",
        get=lambda _key, default=None: default,
    )
    monkeypatch.setattr("core.config.get_config", lambda: config)
    original_read_text = Path.read_text

    def guarded_read_text(path, *args, **kwargs):
        if Path(path).resolve() == page.resolve():
            raise AssertionError("denied page body must not be read")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    principal = PrincipalEnvelope(
        principal_id="mcp:codex:capability",
        agent="codex",
        host_kind="codex",
        capability_id="capability",
        capabilities=frozenset({"memory_read"}),
        allowed_projects=frozenset({"mnemos"}),
    )
    facade = DefaultMnemosServiceFacade(logging.getLogger(__name__))

    result = facade.wiki_read(
        "03-Tech/private.md",
        principal=principal,
        narrowing=AccessNarrowing(project="mnemos", session_id="session-1"),
    )

    assert result == {
        "success": False,
        "code": "access_denied",
        "reason": "private_cross_agent_denied",
        "path": "03-Tech/private.md",
    }
    assert "PRIVATE-CONTENT-SENTINEL" not in json.dumps(result)


def test_same_agent_private_page_remains_readable(tmp_path, monkeypatch):
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
---
AUTHORIZED-CONTENT-SENTINEL
""",
        encoding="utf-8",
    )
    config = SimpleNamespace(
        wiki_dir=wiki_dir,
        database_dir=tmp_path / "db",
        get=lambda _key, default=None: default,
    )
    monkeypatch.setattr("core.config.get_config", lambda: config)
    principal = PrincipalEnvelope(
        principal_id="mcp:codex:capability",
        agent="codex",
        host_kind="codex",
        capability_id="capability",
        capabilities=frozenset({"memory_read"}),
        allowed_projects=frozenset({"mnemos"}),
    )
    facade = DefaultMnemosServiceFacade(logging.getLogger(__name__))

    result = facade.wiki_read(
        "03-Tech/private.md",
        principal=principal,
        narrowing=AccessNarrowing(project="mnemos", session_id="session-1"),
    )

    assert result["success"] is True
    assert "AUTHORIZED-CONTENT-SENTINEL" in json.dumps(result["content"])


def test_cross_agent_existing_page_is_denied_before_write(tmp_path, monkeypatch):
    wiki_dir = tmp_path / "wiki"
    page = wiki_dir / "03-Tech" / "private.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        """---
scope: private
source_agent: claude
session_id: session-1
project: mnemos
acl_schema_version: 1
acl_metadata_complete: true
acl_reconciliation_status: proven
---
ORIGINAL-CROSS-AGENT-CONTENT
""",
        encoding="utf-8",
    )
    config = SimpleNamespace(
        wiki_dir=wiki_dir,
        database_dir=tmp_path / "db",
        get=lambda _key, default=None: default,
    )
    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setattr(
        "core.application.trusted_write_bridge.write_application_wiki_page",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("denied overwrite must not reach trusted writer")
        ),
    )
    principal = PrincipalEnvelope(
        principal_id="mcp:codex:capability",
        agent="codex",
        host_kind="codex",
        capability_id="capability",
        capabilities=frozenset({"memory_write"}),
        allowed_projects=frozenset({"mnemos"}),
    )

    result = DefaultMnemosServiceFacade(logging.getLogger(__name__)).wiki_write(
        "03-Tech/private.md",
        "ATTEMPTED-OVERWRITE",
        {"scope": "private"},
        principal=principal,
        session_id="session-1",
        project="mnemos",
    )

    assert result == {
        "success": False,
        "code": "cross_agent_write_forbidden",
        "path": "03-Tech/private.md",
    }
    assert "ORIGINAL-CROSS-AGENT-CONTENT" in page.read_text(encoding="utf-8")
    assert "ATTEMPTED-OVERWRITE" not in page.read_text(encoding="utf-8")


def test_cross_agent_read_grant_does_not_authorize_existing_page_write(
    tmp_path,
    monkeypatch,
):
    wiki_dir = tmp_path / "wiki"
    page = wiki_dir / "03-Tech" / "shared.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        """---
scope: agent
source_agent: claude
acl_schema_version: 1
acl_metadata_complete: true
acl_reconciliation_status: server_principal
---
ORIGINAL-SHARED-CONTENT
""",
        encoding="utf-8",
    )
    config = SimpleNamespace(
        wiki_dir=wiki_dir,
        database_dir=tmp_path / "db",
        get=lambda _key, default=None: default,
    )
    monkeypatch.setattr("core.config.get_config", lambda: config)
    principal = PrincipalEnvelope(
        principal_id="mcp:codex:capability",
        agent="codex",
        host_kind="codex",
        capability_id="capability",
        capabilities=frozenset({"memory_write"}),
        allowed_source_agents=frozenset({"claude"}),
    )

    result = DefaultMnemosServiceFacade(logging.getLogger(__name__)).wiki_write(
        "03-Tech/shared.md",
        "ATTEMPTED-SHARED-OVERWRITE",
        {"scope": "agent"},
        principal=principal,
    )

    assert result["code"] == "cross_agent_write_forbidden"
    assert "ORIGINAL-SHARED-CONTENT" in page.read_text(encoding="utf-8")
