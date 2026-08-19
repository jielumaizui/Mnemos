import json
from pathlib import Path
from types import SimpleNamespace

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.agent_kit.authorization import AgentAuthorizationStore
from core.application.intelligence import IntelligenceApplicationService
from integrations.agora import MCPServer


class _IsolatedConfig:
    def __init__(self, root: Path):
        self.wiki_dir = root / "wiki"
        self.database_dir = root / "db"
        self.data_dir = root / "data"

    def get(self, _key, default=None):
        return default


def test_denied_context_search_has_zero_persistent_side_effects(
    tmp_path,
    monkeypatch,
):
    config = _IsolatedConfig(tmp_path)
    page = config.wiki_dir / "03-Tech" / "private.md"
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
置信度: 0.9
---
# Private Search
AUTH-BEFORE-SIDE-EFFECT-SENTINEL
""",
        encoding="utf-8",
    )
    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setattr("core.app.context_search.get_config", lambda: config)
    principal = PrincipalEnvelope(
        principal_id="mcp:codex:capability",
        agent="codex",
        host_kind="codex",
        capability_id="capability",
        capabilities=frozenset({"memory_read"}),
        allowed_projects=frozenset({"mnemos"}),
    )

    result = IntelligenceApplicationService().context_aware_search(
        "AUTH-BEFORE-SIDE-EFFECT-SENTINEL",
        principal=principal,
        narrowing=AccessNarrowing(project="mnemos", session_id="session-1"),
    )

    assert result["success"] is True
    assert result["results"] == []
    assert result["access_filter"]["private_cross_agent_denied"] == 1
    assert sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*.db")) == []


def test_denied_wiki_search_has_zero_persistent_side_effects(
    tmp_path,
    monkeypatch,
):
    config = _IsolatedConfig(tmp_path)
    page = config.wiki_dir / "03-Tech" / "private.md"
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
置信度: 0.9
---
# Private Wiki Search
DENIED-WIKI-SEARCH-SENTINEL
""",
        encoding="utf-8",
    )
    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setattr("core.app.context_search.get_config", lambda: config)
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
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "wiki_search",
                "arguments": {
                    "query": "DENIED-WIKI-SEARCH-SENTINEL",
                    "session_id": "session-1",
                    "project": "mnemos",
                },
            },
        }
    )
    result = response["result"]
    payload = json.loads(result["content"][0]["text"])

    assert "isError" not in result
    assert payload["results"] == []
    assert payload["access_filter"]["private_cross_agent_denied"] == 1
    assert list(config.database_dir.rglob("*.db")) == []
    assert list((config.wiki_dir / ".kg").rglob("*.db")) == []


def test_external_rerank_receives_only_acl_authorized_content(tmp_path, monkeypatch):
    class Config(_IsolatedConfig):
        def get(self, key, default=None):
            return {
                "embedding.enabled": True,
                "embedding.use_rerank": True,
                "search.weights": {},
            }.get(key, default)

    config = Config(tmp_path)
    config.database_dir.mkdir(parents=True)
    (config.database_dir / "embedding_index").mkdir()
    allowed = config.wiki_dir / "03-Tech" / "allowed.md"
    denied = config.wiki_dir / "03-Tech" / "denied.md"
    allowed.parent.mkdir(parents=True)
    allowed.write_text(
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
# Allowed
RERANK-AUTHORIZED-CONTENT
""",
        encoding="utf-8",
    )
    denied.write_text(
        """---
scope: private
source_agent: claude
session_id: session-1
project: mnemos
acl_schema_version: 1
acl_metadata_complete: true
acl_reconciliation_status: proven
置信度: 0.9
---
# Denied
RERANK-DENIED-SECRET-SENTINEL
""",
        encoding="utf-8",
    )
    external_documents = []

    class FakeEmbeddingIndexManager:
        def __init__(self, wiki_base=None, index_dir=None):
            del wiki_base, index_dir
            self.client = object()

        @staticmethod
        def persisted_search_available():
            return True

    class FakeDualIndexRetriever:
        def __init__(
            self,
            page_index=None,
            relation_manager=None,
            wiki_base=None,
        ):
            del page_index, relation_manager, wiki_base
            self._trace = {}

        def search_detailed(self, query, top_k=15, use_rerank=None, *, allowed_page_paths=None):
            assert use_rerank is False
            assert allowed_page_paths == {"03-Tech/allowed.md"}
            self._trace = {
                "page_search_attempted": True,
                "page_search_ok": True,
                "page_result_count": 2,
                "rerank_configured": False,
                "rerank_attempted": False,
                "rerank_api_called": False,
                "rerank_applied": False,
                "rerank_degraded": False,
                "degraded": False,
                "degraded_reasons": [],
            }
            return [
                ("03-Tech/denied.md", 0.99, 0.99, 0.0),
                ("03-Tech/allowed.md", 0.98, 0.98, 0.0),
            ]

        def get_last_trace(self):
            return dict(self._trace)

        def rerank_authorized_documents(self, query, documents, *, top_n):
            external_documents.extend(documents)
            return list(range(len(documents))), {
                "rerank_configured": True,
                "rerank_attempted": True,
                "rerank_api_called": True,
                "rerank_applied": True,
                "rerank_degraded": False,
                "degraded": False,
                "degraded_reasons": [],
            }

    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setattr("core.app.context_search.get_config", lambda: config)
    monkeypatch.setattr(
        "core.embeddings.dual_index.DualIndexRetriever",
        FakeDualIndexRetriever,
    )
    monkeypatch.setattr(
        "core.embeddings.index_manager.EmbeddingIndexManager",
        FakeEmbeddingIndexManager,
    )
    principal = PrincipalEnvelope(
        principal_id="mcp:codex:capability",
        agent="codex",
        host_kind="codex",
        capability_id="capability",
        capabilities=frozenset({"memory_read"}),
        allowed_projects=frozenset({"mnemos"}),
    )

    result = IntelligenceApplicationService().context_aware_search(
        "RERANK",
        principal=principal,
        narrowing=AccessNarrowing(project="mnemos", session_id="session-1"),
    )

    assert [item["source_agent"] for item in result["results"]] == ["codex"]
    assert len(external_documents) == 1
    assert all("RERANK-DENIED-SECRET-SENTINEL" not in text for text in external_documents)


def test_denied_session_search_does_not_mutate_raw_index(
    tmp_path,
    monkeypatch,
):
    from core.app.raw_search import RawIndex

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "claude.md").write_text(
        """---
source: claude
session_id: session-1
date: 2026-07-10
scope: private
acl_schema_version: 1
acl_metadata_complete: true
acl_reconciliation_status: provenance_write
---
RAWAUTHSIDEEFFECTSENTINEL
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

    config = Config()
    monkeypatch.setattr("core.config.get_config", lambda: config)
    from core.sync_framework.raw_event_store import RawEventStore

    canonical_store = RawEventStore(config=config)
    canonical_store.upsert_turn(
        source_agent="claude",
        session_id="session-1",
        turn_number=0,
        user_content="RAWAUTHSIDEEFFECTSENTINEL",
        assistant_content="cross-agent private evidence",
    )
    canonical_store.close()
    db_path = config.database_dir / "raw_index.db"
    with RawIndex(raw_dir=raw_dir, db_path=db_path, config=config) as index:
        index.sync_index(force_full=True)
    before = {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in config.database_dir.glob("*.db*")
    }

    def fail_sync(*_args, **_kwargs):
        raise AssertionError("session search must not synchronize the index")

    monkeypatch.setattr(RawIndex, "sync_index", fail_sync)
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
                "name": "session_search",
                "arguments": {"query": "RAWAUTHSIDEEFFECTSENTINEL"},
            },
        }
    )
    payload = json.loads(response["result"]["content"][0]["text"])
    after = {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in config.database_dir.glob("*.db*")
    }

    assert payload["results"] == []
    assert payload["access_filter"]["private_cross_agent_denied"] == 1
    assert after == before


def test_denied_predictive_push_has_no_cooldown_or_delivery_side_effect(
    tmp_path,
    monkeypatch,
):
    config = _IsolatedConfig(tmp_path)
    page = config.wiki_dir / "03-Tech" / "private-push.md"
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
PRIVATE-PUSH-CONTENT
""",
        encoding="utf-8",
    )
    decision = SimpleNamespace(
        should_push=True,
        reason="match",
        matches=[
            SimpleNamespace(
                page_path="03-Tech/private-push.md",
                page_title="Private push",
                relevant_excerpt="PRIVATE-PUSH-CONTENT",
                match_reason="match",
                match_score=0.9,
                push_priority="high",
            )
        ],
    )

    def decide_push(_input, current_task="", *, candidate_path_filter=None):
        assert candidate_path_filter is not None
        return decision

    push_engine = SimpleNamespace(decide_push=decide_push)
    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setattr(
        "core.kia.reminder_engine.ReminderEngine._get_push_engine",
        lambda self: push_engine,
    )
    monkeypatch.setattr(
        "core.kia.reminder_engine.ReminderEngine._record_shown",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("denied reminder must not be recorded")
        ),
    )
    principal = PrincipalEnvelope(
        principal_id="mcp:codex:capability",
        agent="codex",
        host_kind="codex",
        capability_id="capability",
        capabilities=frozenset({"memory_read"}),
        allowed_projects=frozenset({"mnemos"}),
    )

    result = IntelligenceApplicationService().predictive_push(
        "private push",
        principal=principal,
        narrowing=AccessNarrowing(project="mnemos", session_id="session-1"),
    )

    assert result["push_available"] is False
    assert result["access_filter"] == {"private_cross_agent_denied": 1}
    assert not (config.database_dir / "reminder_cooldown.db").exists()
    assert not (config.wiki_dir / ".kg" / "push.db").exists()
