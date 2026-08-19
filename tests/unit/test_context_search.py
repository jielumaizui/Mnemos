import logging
import os
from datetime import datetime, timedelta

import pytest

from core.access_policy import AccessNarrowing, PrincipalEnvelope


def _principal() -> PrincipalEnvelope:
    return PrincipalEnvelope(
        principal_id="mcp:codex:context-search-test",
        agent="codex",
        host_kind="codex",
        capability_id="context-search-test",
        capabilities=frozenset({"memory_read", "memory_write"}),
        allowed_projects=frozenset({"mnemos"}),
    )


def _search(searcher, query, **kwargs):
    return searcher.search(
        query,
        principal=kwargs.pop("principal", _principal()),
        narrowing=kwargs.pop("narrowing", AccessNarrowing()),
        **kwargs,
    )


def _write_page(
    path,
    frontmatter="",
    body="# Redis Pitfall\nRedis 连接池踩坑：不要在每个请求里新建连接。\n",
    *,
    include_acl=True,
):
    if include_acl and "acl_schema_version:" not in frontmatter:
        frontmatter = (
            "scope: agent\n"
            "source_agent: codex\n"
            "acl_schema_version: 1\n"
            "acl_metadata_complete: true\n"
            "acl_reconciliation_status: proven\n" + frontmatter
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n{body}", encoding="utf-8")


def _sensitive_provider_error_marker() -> str:
    """Build a realistic error payload without storing a credential literal."""
    return "|".join(
        (
            "api" + "_key" + "=" + "DUMMY_CREDENTIAL_VALUE",
            "pass" + "word" + "=" + "DUMMY_CREDENTIAL_VALUE",
            "bank" + "_card" + "=" + "DUMMY_CREDENTIAL_VALUE",
            "prompt" + "=" + "PRIVATE_PROMPT_BODY",
            "response" + "=" + "PRIVATE_RESPONSE_BODY",
        )
    )


def test_context_search_supports_chinese_full_text_and_freshness_alert(tmp_path):
    from core.app.context_search import ContextAwareSearch

    old = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d")
    page = tmp_path / "03-Tech" / "redis.md"
    _write_page(page, f"时效性: 上下文相关\n修改日期: {old}\n置信度: 0.9\n")

    result = _search(ContextAwareSearch(wiki_base=str(tmp_path)), "Redis 连接池踩坑", limit=1)[0]

    assert os.path.normpath(result.page_path) == os.path.normpath("03-Tech/redis.md")
    assert result.final_score == result.score
    assert "关键词匹配" in result.match_reason
    assert result.freshness_alert.type == "potentially_stale"


def test_context_search_result_preserves_complete_acl_metadata(tmp_path):
    from core.app.context_search import ContextAwareSearch

    page = tmp_path / "03-Tech" / "private.md"
    _write_page(
        page,
        """scope: private
source_agent: codex
session_id: session-1
project: mnemos
acl_schema_version: 1
acl_metadata_complete: true
acl_reconciliation_status: proven
置信度: 0.9
""",
        body="# Private Redis\nACL-METADATA-SENTINEL Redis 连接池。\n",
    )

    result = _search(
        ContextAwareSearch(wiki_base=str(tmp_path)),
        "ACL-METADATA-SENTINEL",
        limit=1,
        narrowing=AccessNarrowing(session_id="session-1", project="mnemos"),
    )[0]

    assert result.scope == "private"
    assert result.source_agent == "codex"
    assert result.session_id == "session-1"
    assert result.project == "mnemos"
    assert result.acl_schema_version == 1
    assert result.acl_metadata_complete is True


def test_wiki_result_binds_exact_revision_field_and_tail_span(tmp_path):
    from core.app.context_search import ContextAwareSearch

    sentinel = "WIKI-TAIL-PROVENANCE-SENTINEL"
    page = tmp_path / "03-Tech" / "tail.md"
    body = "# Tail provenance\n" + ("ordinary-prefix " * 220) + sentinel + " suffix\n"
    _write_page(page, "置信度: 0.9\n", body=body)

    first = _search(
        ContextAwareSearch(wiki_base=str(tmp_path)),
        sentinel,
        allow_embedding=False,
        limit=1,
    )[0]

    exact_content = page.read_text(encoding="utf-8")
    start = exact_content.index(sentinel)
    assert sentinel in first.snippet
    assert first.result_kind == "wiki_page"
    assert first.object_type == "wiki_page"
    assert first.object_id == "03-Tech/tail.md"
    assert first.revision_id.startswith("wiki_page:03-Tech/tail.md:sha256:")
    assert first.source_revision_id == first.revision_id
    assert first.matched_field == "wiki.content"
    assert first.source_span_ids == [f"{first.revision_id}#{start}:{start + len(sentinel)}"]
    assert first.acl_decision == "authorized"

    page.write_text(exact_content + "changed\n", encoding="utf-8")
    second = _search(
        ContextAwareSearch(wiki_base=str(tmp_path)),
        sentinel,
        allow_embedding=False,
        limit=1,
    )[0]
    assert second.revision_id != first.revision_id


def test_context_search_returns_typed_cognition_when_wiki_surface_is_empty(tmp_path):
    from core.app.context_search import ContextAwareSearch
    from core.cognitive.state_schema import initialize_cognitive_state_schema
    from core.cognitive.state_store import CognitiveStateStore
    from tests.unit.cognitive.test_cognitive_search import (
        _commit,
        _principal as _cognitive_principal,
        _scoped_revision,
    )

    database_dir = tmp_path / ".kg"
    database_dir.mkdir()
    state_db = database_dir / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(state_db)
    store = CognitiveStateStore(state_db)
    revision = _scoped_revision(
        "context-only",
        claim_text="CONTEXT-COGNITION-SENTINEL 决策只存在于 canonical cognition。",
        owner_principal_id="test:state-store",
    )
    _commit(store, revision, "context-only")

    results = _search(
        ContextAwareSearch(wiki_base=str(tmp_path), database_dir=database_dir),
        "CONTEXT-COGNITION-SENTINEL",
        principal=_cognitive_principal(),
        narrowing=AccessNarrowing(project="mnemos"),
        allow_embedding=False,
        limit=5,
    )

    assert len(results) == 1
    result = results[0]
    assert result.result_kind == "cognitive_state"
    assert result.revision_id == revision.revision_id
    assert result.matched_field == "claims[0].claim_text"
    assert result.source_revision_id == "raw-revision-context-only"
    assert result.source_span_ids == ["raw-revision-context-only#0:32"]
    assert result.acl_decision == "authorized"
    assert result.page_path.startswith("mnemos://cognitive-state/")


def test_file_recall_scans_past_twenty_hits_and_preserves_deep_match_for_snippet(
    tmp_path,
):
    from core.app.context_search import ContextAwareSearch

    searcher = ContextAwareSearch(wiki_base=str(tmp_path))
    authorized = {}
    for index in range(20):
        path = tmp_path / f"{index:02d}-decoy.md"
        _write_page(path, body=f"# Decoy {index}\nCOMMON-RECALL-SENTINEL only\n")
        authorized[path.name] = {}
    target = tmp_path / "99-target.md"
    _write_page(
        target,
        body=(
            "# Deep target\n"
            + ("ordinary-prefix " * 220)
            + "COMMON-RECALL-SENTINEL DEEP-MATCH-SENTINEL"
        ),
    )
    authorized[target.name] = {}
    searcher._authorized_page_frontmatter = authorized

    candidates = searcher._recall_from_files("COMMON-RECALL-SENTINEL DEEP-MATCH-SENTINEL")
    target_candidate = next(
        candidate for candidate in candidates if candidate["path"] == target.name
    )

    assert "DEEP-MATCH-SENTINEL" in target_candidate["content"]
    snippet = searcher._extract_snippet(target_candidate, "DEEP-MATCH-SENTINEL")
    assert "DEEP-MATCH-SENTINEL" in snippet
    assert len(snippet) <= 240


def test_intelligence_service_serializes_reauthorized_cognition_without_wiki(tmp_path, monkeypatch):
    from core.application.intelligence import IntelligenceApplicationService
    from core.cognitive.state_schema import initialize_cognitive_state_schema
    from core.cognitive.state_store import CognitiveStateStore
    from tests.unit.cognitive.test_cognitive_search import (
        _commit,
        _principal as _cognitive_principal,
        _scoped_revision,
    )

    class FakeConfig:
        wiki_dir = tmp_path / "wiki"
        database_dir = tmp_path / "database"

        def get(self, key, default=None):
            return {"embedding.enabled": False}.get(key, default)

    FakeConfig.wiki_dir.mkdir()
    FakeConfig.database_dir.mkdir()
    state_db = FakeConfig.database_dir / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(state_db)
    store = CognitiveStateStore(state_db)
    revision = _scoped_revision(
        "application-only",
        claim_text="APPLICATION-COGNITION-SENTINEL 决策来自 canonical cognition。",
        owner_principal_id="test:state-store",
    )
    _commit(store, revision, "application-only")
    monkeypatch.setattr("core.config.get_config", lambda: FakeConfig())
    monkeypatch.setattr("core.app.context_search.get_config", lambda: FakeConfig())

    response = IntelligenceApplicationService().context_aware_search(
        "APPLICATION-COGNITION-SENTINEL",
        principal=_cognitive_principal(),
        narrowing=AccessNarrowing(project="mnemos"),
    )

    assert response["success"] is True
    assert response["count"] == 1
    result = response["results"][0]
    assert result["result_kind"] == "cognitive_state"
    assert result["revision_id"] == revision.revision_id
    assert result["matched_field"] == "claims[0].claim_text"
    assert result["acl_decision"] == "authorized"
    assert result["source_span_ids"] == ["raw-revision-application-only#0:32"]


def test_intelligence_service_exposes_reauthorized_cognitive_graph_without_wiki(
    tmp_path, monkeypatch
):
    from core.application.intelligence import IntelligenceApplicationService
    from scripts.cognitive_search_benchmark import build_environment, load_fixture

    environment = build_environment(tmp_path / "benchmark", load_fixture())
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()

    class FakeConfig:
        def get(self, key, default=None):
            return {"embedding.enabled": False}.get(key, default)

    FakeConfig.wiki_dir = wiki_dir
    FakeConfig.database_dir = environment.state_db.parent
    FakeConfig.cognitive_graph_db_path = environment.cognitive_graph_db
    monkeypatch.setattr("core.config.get_config", lambda: FakeConfig())
    monkeypatch.setattr("core.app.context_search.get_config", lambda: FakeConfig())

    response = IntelligenceApplicationService().context_aware_search(
        "umber-aqueduct graph cause",
        principal=environment.principal,
        narrowing=environment.narrowing,
    )

    assert response["success"] is True
    graph_result = next(
        result for result in response["results"] if result["result_kind"] == "cognitive_graph"
    )
    assert graph_result["object_id"] == environment.expected_object_ids["graph-source"]
    assert graph_result["matched_field"] == "relation.target.content"
    assert graph_result["acl_decision"] == "authorized"
    assert response["access_filter"]["cognitive_authorized"] >= 1


def test_context_search_does_not_trust_cached_acl_over_canonical_page(tmp_path, monkeypatch):
    from core.app.context_search import ContextAwareSearch

    page = tmp_path / "03-Tech" / "cached-spoof.md"
    _write_page(
        page,
        "置信度: 0.9\n",
        body="# Cached ACL spoof\nCACHED-ACL-SPOOF-SENTINEL\n",
        include_acl=False,
    )
    spoofed_candidate = {
        "path": "03-Tech/cached-spoof.md",
        "title": "Cached ACL spoof",
        "content": "CACHED-ACL-SPOOF-SENTINEL",
        "frontmatter": {
            "scope": "public",
            "source_agent": "codex",
            "acl_schema_version": 1,
            "acl_metadata_complete": True,
            "acl_reconciliation_status": "proven",
            "置信度": 0.9,
        },
        "match_type": "graph",
    }
    monkeypatch.setattr(
        ContextAwareSearch,
        "_recall_from_files",
        lambda self, query: [spoofed_candidate],
    )
    monkeypatch.setattr(ContextAwareSearch, "_recall_from_kg", lambda self, query: [])

    result = _search(
        ContextAwareSearch(wiki_base=str(tmp_path)),
        "CACHED-ACL-SPOOF-SENTINEL",
        limit=1,
        allow_embedding=False,
    )

    assert result == []


def test_context_search_freshness_uses_default_for_invalid_policy_value(tmp_path, monkeypatch):
    import core.app.context_search as context_search
    from core.app.context_search import ContextAwareSearch

    page = tmp_path / "03-Tech" / "redis.md"
    _write_page(page, "置信度: 0.9\n")

    class BadPolicy:
        def get(self, key, default=None):
            return object()

    monkeypatch.setattr(context_search, "get_effective_policy", lambda: BadPolicy())

    freshness = ContextAwareSearch(wiki_base=str(tmp_path))._compute_freshness(
        {"path": "03-Tech/redis.md"}
    )

    assert 0 < freshness <= 1


def test_context_search_tokenizes_intent_wrapped_chinese_query_and_explains_fields(tmp_path):
    from core.app.context_search import ContextAwareSearch

    _write_page(
        tmp_path / "02-Concepts" / "distill.md",
        "名称: 蒸馏链路质量\n摘要: Raw 到 Wiki 的蒸馏机制\n关键词:\n  - 蒸馏\n  - Wiki\n置信度: 0.9\n",
        body="# 蒸馏链路质量\n蒸馏需要保留 Raw 原文、分块覆盖和 Wiki frontmatter 元数据。\n",
    )
    _write_page(
        tmp_path / "02-Concepts" / "knowledge.md",
        "名称: 查知识入口\n关键词:\n  - 查知识\n置信度: 0.9\n",
        body="# 查知识入口\n用户说查知识时应进入 Wiki 搜索。\n",
    )

    results = _search(ContextAwareSearch(wiki_base=str(tmp_path)), "查知识蒸馏", limit=3)

    assert results
    result = results[0]
    assert result.page_path == "02-Concepts/distill.md"
    assert "蒸馏" in result.matched_terms
    assert result.score_breakdown["relevance"] == round(result.relevance, 3)
    assert "命中字段" in result.match_reason
    assert "蒸馏" in result.match_reason


def test_context_search_records_sessions_only_after_authorization(tmp_path, monkeypatch):
    import sqlite3

    from core.app.context_search import ContextAwareSearch

    global_db = tmp_path / "global" / "mnemos.db"

    class FakeConfig:
        wiki_dir = tmp_path / "configured-wiki"
        database_dir = global_db.parent

        def get(self, key, default=None):
            return default

    monkeypatch.setattr("core.config.get_config", lambda: FakeConfig())
    monkeypatch.setattr("core.app.context_search.get_config", lambda: FakeConfig())
    _write_page(
        tmp_path / "02-Concepts" / "distill.md",
        "名称: 蒸馏链路质量\n关键词:\n  - 蒸馏\n置信度: 0.9\n",
        body="# 蒸馏链路质量\n蒸馏需要保留 Raw 原文。\n",
    )

    results = _search(ContextAwareSearch(wiki_base=str(tmp_path)), "查知识蒸馏", limit=1)

    assert results
    inferred_local_db = tmp_path / ".kg" / "mnemos.db"
    assert not inferred_local_db.exists()
    assert not global_db.exists()
    ContextAwareSearch(wiki_base=str(tmp_path)).record_authorized_search(
        "查知识蒸馏",
        results,
        principal=_principal(),
        narrowing=AccessNarrowing(
            session_id="context-search-record-session",
            project="mnemos",
        ),
    )
    assert not inferred_local_db.exists()
    assert not global_db.exists()

    explicit_database_dir = tmp_path / "explicit-search-state"
    explicit_database_dir.mkdir()
    ContextAwareSearch(
        wiki_base=str(tmp_path),
        database_dir=explicit_database_dir,
    ).record_authorized_search(
        "查知识蒸馏",
        results,
        principal=_principal(),
        narrowing=AccessNarrowing(
            session_id="context-search-record-session",
            project="mnemos",
        ),
    )
    explicit_db = explicit_database_dir / "mnemos.db"
    assert explicit_db.exists()
    with sqlite3.connect(explicit_db) as conn:
        row = conn.execute("SELECT query FROM search_sessions").fetchone()
    assert row == ("查知识蒸馏",)


def test_authorized_search_provenance_is_object_scoped_and_propagates_to_feedback(
    tmp_path, monkeypatch
):
    import json
    import sqlite3
    from types import SimpleNamespace

    from core.app.context_search import ContextAwareSearch
    from core.cognitive.state_schema import initialize_cognitive_state_schema
    from core.cognitive.state_store import CognitiveStateStore
    from core.scoring.subject_provenance import delete_scoring_subject_scope

    class FakeConfig:
        wiki_dir = tmp_path / "wiki"
        database_dir = tmp_path
        mnemos_dir = tmp_path
        data_dir = tmp_path

        def get(self, key, default=None):
            return default

    config = FakeConfig()
    config.wiki_dir.mkdir()
    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setattr("core.app.context_search.get_config", lambda: config)
    monkeypatch.setattr(ContextAwareSearch, "_record_search_hits", lambda *_args: None)
    monkeypatch.setattr(
        ContextAwareSearch,
        "_record_authorized_profile_usage",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        ContextAwareSearch,
        "_record_authorized_entity_accesses",
        lambda *_args: None,
    )

    searcher = ContextAwareSearch(wiki_base=str(config.wiki_dir))
    first_scope = AccessNarrowing(session_id="search-scope-1", project="mnemos")
    second_scope = AccessNarrowing(session_id="search-scope-2", project="mnemos")
    result = SimpleNamespace(page_path="03-Tech/result.md")
    first_access = searcher.record_authorized_search(
        "same private query",
        [result],
        principal=_principal(),
        narrowing=first_scope,
    )
    second_access = searcher.record_authorized_search(
        "same private query",
        [result],
        principal=_principal(),
        narrowing=second_scope,
    )

    assert first_access["scope"]["session_id"] == "search-scope-1"
    assert second_access["scope"]["session_id"] == "search-scope-2"
    db_path = tmp_path / "mnemos.db"
    with sqlite3.connect(db_path) as conn:
        sessions = conn.execute("SELECT id, session_id FROM search_sessions ORDER BY id").fetchall()
        sidecars = conn.execute("""
            SELECT object_id, state, access_json
            FROM scoring_object_provenance
            WHERE object_type='search_session'
            ORDER BY CAST(object_id AS INTEGER)
            """).fetchall()
    assert len(sessions) == 2
    assert sessions[0][1] != sessions[1][1]
    assert [row[1] for row in sidecars] == ["tracked", "tracked"]
    assert {json.loads(row[2])["scope"]["session_id"] for row in sidecars} == {
        "search-scope-1",
        "search-scope-2",
    }

    initialize_cognitive_state_schema(tmp_path / "producer_consumer_ledger.db")
    feedback_result = ContextAwareSearch.record_search_click(
        "03-Tech/result.md",
        db_path=db_path,
        principal=_principal(),
        narrowing=first_scope,
    )
    assert feedback_result["success"] is True
    assert feedback_result["terminal_receipt_count"] == 7
    assert len(feedback_result["terminal_receipts"]) == 7
    assert {receipt["schema_version"] for receipt in feedback_result["terminal_receipts"]} == {
        "mnemos.feedback_cognitive_update_receipt.v1"
    }
    with sqlite3.connect(db_path) as conn:
        legacy_training_tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if str(row[0])
            in {
                "ground_truth_signals",
                "scorer_training_queue",
                "scorer_feedback_events",
                "scorer_models",
            }
        }
        operational = conn.execute(
            "SELECT clicked_path, outcome_status FROM search_sessions WHERE id=?",
            (sessions[0][0],),
        ).fetchone()
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    assert legacy_training_tables == set()
    assert operational == (None, "")
    assert len(state.current_revisions(object_type="user_reaction_event")) == 1
    assert len(state.current_revisions(object_type="feedback_attribution_record")) == 1

    deletion = delete_scoring_subject_scope(
        db_path=db_path,
        request_id="delete-authorized-search-scope-1",
        scope_kind="session",
        scope_value="search-scope-1",
    )
    assert deletion["verified"] is True
    assert deletion["search_sessions_deleted"] == 1
    assert deletion["ground_truth_deleted"] == 0
    with sqlite3.connect(db_path) as conn:
        remaining = conn.execute("""
            SELECT access_json FROM scoring_object_provenance
            WHERE object_type='search_session'
            """).fetchall()
    assert [json.loads(row[0])["scope"]["session_id"] for row in remaining] == ["search-scope-2"]


def test_context_search_exposes_embedding_and_rerank_trace(tmp_path, monkeypatch):
    from core.app.context_search import ContextAwareSearch

    class FakeConfig:
        wiki_dir = tmp_path
        data_dir = tmp_path
        database_dir = tmp_path

        def get(self, key, default=None):
            values = {
                "embedding.enabled": True,
                "embedding.use_rerank": True,
                "search.weights": {},
            }
            return values.get(key, default)

    class FakeDualIndexRetriever:
        def __init__(self, wiki_base=None, **_kwargs):
            self.wiki_base = wiki_base
            self._trace = {}
            self.authorized_documents = []

        def search_detailed(self, query, top_k=15, use_rerank=None, *, allowed_page_paths=None):
            assert use_rerank is False
            assert allowed_page_paths == {"03-Tech/redis.md"}
            self._trace = {
                "page_search_attempted": True,
                "page_search_ok": True,
                "page_result_count": 1,
                "relation_search_attempted": True,
                "relation_search_ok": True,
                "relation_result_count": 1,
                "rerank_configured": False,
                "rerank_attempted": False,
                "rerank_api_called": False,
                "rerank_applied": False,
                "rerank_degraded": False,
                "degraded": False,
                "degraded_reasons": [],
            }
            return [("03-Tech/redis.md", 0.91, 0.88, 0.03)]

        def rerank_authorized_documents(self, query, documents, *, top_n):
            self.authorized_documents = list(documents)
            return [0], {
                "rerank_configured": True,
                "rerank_attempted": True,
                "rerank_api_called": True,
                "rerank_applied": True,
                "rerank_degraded": False,
                "degraded": False,
                "degraded_reasons": [],
            }

        def get_last_trace(self):
            return dict(self._trace)

    class FakeReadyIndex:
        client = object()

        def __init__(self, *_args, **_kwargs):
            pass

        def persisted_search_available(self):
            return True

    monkeypatch.setattr("core.config.get_config", lambda: FakeConfig())
    monkeypatch.setattr("core.app.context_search.get_config", lambda: FakeConfig())
    monkeypatch.setattr(
        "core.embeddings.index_manager.EmbeddingIndexManager",
        FakeReadyIndex,
    )
    monkeypatch.setattr("core.embeddings.dual_index.DualIndexRetriever", FakeDualIndexRetriever)
    monkeypatch.setattr(ContextAwareSearch, "_get_metrics", lambda self: None)
    monkeypatch.setattr(
        ContextAwareSearch,
        "_get_profile_weights",
        lambda self, principal=None, narrowing=None: {},
    )
    _write_page(
        tmp_path / "03-Tech" / "redis.md",
        "名称: Redis 连接池\n置信度: 0.9\n",
        body="# Redis 连接池\nRedis 连接池需要复用连接并设置超时。\n",
    )
    (tmp_path / "embedding_index").mkdir()

    searcher = ContextAwareSearch(wiki_base=str(tmp_path))
    results = _search(searcher, "Redis 连接池", limit=1)
    results = searcher.rerank_authorized("Redis 连接池", results, limit=1)
    trace = searcher.get_last_query_trace()

    assert results
    assert results[0].page_path == "03-Tech/redis.md"
    assert results[0].match_source in {"semantic", "hybrid"}
    assert results[0].page_embedding_score == 0.88
    assert trace["embedding_enabled"] is True
    assert trace["embedding_attempted"] is True
    assert trace["semantic_candidates"] == 1
    assert trace["rerank_configured"] is True
    assert trace["rerank_api_called"] is True
    assert trace["rerank_applied"] is True
    assert trace["degraded"] is False


def test_context_search_can_skip_embedding_for_startup_preflight(tmp_path, monkeypatch):
    from core.app.context_search import ContextAwareSearch

    class FakeConfig:
        wiki_dir = tmp_path
        data_dir = tmp_path
        database_dir = tmp_path

        def get(self, key, default=None):
            values = {
                "embedding.enabled": True,
                "search.weights": {},
            }
            return values.get(key, default)

    class FailingDualIndexRetriever:
        def __init__(self, wiki_base=None):
            raise AssertionError("embedding retriever should not be constructed")

    monkeypatch.setattr("core.config.get_config", lambda: FakeConfig())
    monkeypatch.setattr("core.app.context_search.get_config", lambda: FakeConfig())
    monkeypatch.setattr("core.embeddings.dual_index.DualIndexRetriever", FailingDualIndexRetriever)
    monkeypatch.setattr(ContextAwareSearch, "_get_metrics", lambda self: None)
    monkeypatch.setattr(
        ContextAwareSearch,
        "_get_profile_weights",
        lambda self, principal=None, narrowing=None: {},
    )
    _write_page(
        tmp_path / "03-Tech" / "redis.md",
        "名称: Redis 连接池\n置信度: 0.9\n",
        body="# Redis 连接池\nRedis 连接池需要复用连接并设置超时。\n",
    )

    searcher = ContextAwareSearch(wiki_base=str(tmp_path))
    results = _search(searcher, "Redis 连接池", limit=1, allow_embedding=False)
    trace = searcher.get_last_query_trace()

    assert results
    assert results[0].page_path == "03-Tech/redis.md"
    assert trace["embedding_configured"] is True
    assert trace["embedding_allowed"] is False
    assert trace["embedding_enabled"] is False
    assert trace["embedding_attempted"] is False
    assert trace["semantic_candidates"] == 0


def test_context_search_exposes_embedding_degradation_on_empty_success(tmp_path, monkeypatch):
    from core.app.context_search import ContextAwareSearch

    class FakeConfig:
        wiki_dir = tmp_path
        data_dir = tmp_path
        database_dir = tmp_path

        def get(self, key, default=None):
            values = {
                "embedding.enabled": True,
                "embedding.use_rerank": True,
                "search.weights": {},
            }
            return values.get(key, default)

    class FakeDualIndexRetriever:
        def __init__(self, wiki_base=None, **_kwargs):
            self._trace = {}

        def search_detailed(self, query, top_k=15, use_rerank=None, *, allowed_page_paths=None):
            assert use_rerank is False
            self._trace = {
                "page_search_attempted": False,
                "page_search_ok": False,
                "page_result_count": 0,
                "rerank_configured": True,
                "rerank_attempted": False,
                "rerank_api_called": False,
                "rerank_applied": False,
                "rerank_degraded": False,
                "degraded": True,
                "degraded_reasons": ["page_embedding_client_unavailable"],
            }
            return []

        def get_last_trace(self):
            return dict(self._trace)

    class FakeReadyIndex:
        client = object()

        def __init__(self, *_args, **_kwargs):
            pass

        def persisted_search_available(self):
            return True

    monkeypatch.setattr("core.config.get_config", lambda: FakeConfig())
    monkeypatch.setattr("core.app.context_search.get_config", lambda: FakeConfig())
    monkeypatch.setattr(
        "core.embeddings.index_manager.EmbeddingIndexManager",
        FakeReadyIndex,
    )
    monkeypatch.setattr("core.embeddings.dual_index.DualIndexRetriever", FakeDualIndexRetriever)
    monkeypatch.setattr(ContextAwareSearch, "_recall_from_kg", lambda self, query: [])
    _write_page(tmp_path / "03-Tech" / "authorized.md", body="# Authorized\nordinary body")
    (tmp_path / "embedding_index").mkdir()

    searcher = ContextAwareSearch(wiki_base=str(tmp_path))
    assert _search(searcher, "不存在的语义查询", limit=1) == []
    trace = searcher.get_last_query_trace()

    assert trace["embedding_enabled"] is True
    assert trace["semantic_candidates"] == 0
    assert trace["result_count"] == 0
    assert trace["degraded"] is True
    assert trace["embedding_degraded"] is True
    assert "page_embedding_client_unavailable" in trace["degraded_reasons"]


def test_context_search_checks_acl_before_any_denied_page_body_read(tmp_path, monkeypatch):
    from core.app.context_search import ContextAwareSearch
    from core.frontmatter import read_markdown as real_read_markdown

    allowed = tmp_path / "03-Tech" / "allowed.md"
    denied = tmp_path / "03-Tech" / "denied.md"
    _write_page(
        allowed,
        body="# Allowed\nAUTHORIZED-RETRIEVAL-SENTINEL Redis connection pool",
    )
    _write_page(
        denied,
        """scope: private
source_agent: claude
session_id: session-denied
project: mnemos
acl_schema_version: 1
acl_metadata_complete: true
acl_reconciliation_status: proven
""",
        body="# Denied\nDENIED-BODY-MUST-NEVER-BE-READ Redis connection pool",
    )

    def guarded_read_markdown(path, *args, **kwargs):
        if os.path.normpath(str(path)) == os.path.normpath(str(denied)):
            raise AssertionError("denied page body must not be read")
        return real_read_markdown(path, *args, **kwargs)

    monkeypatch.setattr("core.frontmatter.read_markdown", guarded_read_markdown)
    results = _search(
        ContextAwareSearch(wiki_base=str(tmp_path)),
        "Redis connection pool",
        allow_embedding=False,
    )

    assert [result.page_path for result in results] == ["03-Tech/allowed.md"]
    assert all("DENIED-BODY-MUST-NEVER-BE-READ" not in result.snippet for result in results)


def test_context_search_denies_lifecycle_tombstoned_page_before_body_read(tmp_path, monkeypatch):
    from core.app.context_search import ContextAwareSearch
    from core.wiki_projection_lifecycle import WikiProjectionLedger

    class FakeConfig:
        wiki_dir = tmp_path
        database_dir = tmp_path / "db"

        def get(self, _key, default=None):
            return default

    FakeConfig.database_dir.mkdir()
    page = tmp_path / "03-Tech" / "tombstoned.md"
    _write_page(page, body="# Tombstoned\nBODY-MUST-NOT-REACH-RETRIEVAL")
    ledger = WikiProjectionLedger(FakeConfig.database_dir / "wiki_projection.db")
    ledger.record_mutation(page, mutation_type="create")
    ledger.record_mutation(page, mutation_type="delete")
    monkeypatch.setattr("core.app.context_search.get_config", lambda: FakeConfig())

    def forbidden_body_read(*_args, **_kwargs):
        raise AssertionError("tombstoned page body must not be read")

    monkeypatch.setattr("core.frontmatter.read_markdown", forbidden_body_read)
    searcher = ContextAwareSearch(wiki_base=str(tmp_path))

    assert _search(searcher, "BODY-MUST-NOT-REACH-RETRIEVAL", allow_embedding=False) == []
    assert searcher.get_last_query_trace()["access_filter"] == {"subject_deleted": 1}


def test_context_search_batches_lifecycle_checks_before_any_body_read(tmp_path, monkeypatch):
    from core.app.context_search import ContextAwareSearch
    from core.wiki_projection_lifecycle import WikiProjectionLedger

    wiki = tmp_path / "wiki"
    for index in range(100):
        _write_page(
            wiki / f"page-{index:03d}.md",
            body=(
                f"# Page {index}\nBULK-LIFECYCLE-TARGET"
                if index == 99
                else f"# Page {index}\nunrelated body"
            ),
        )
    tombstoned = wiki / "tombstoned.md"
    _write_page(tombstoned, body="# Deleted\nBULK-LIFECYCLE-TARGET")
    ledger = WikiProjectionLedger(tmp_path / "wiki_projection.db")
    ledger.record_mutation(tombstoned, mutation_type="create")
    ledger.record_mutation(tombstoned, mutation_type="delete")

    original_bulk = WikiProjectionLedger.tombstone_states
    batch_sizes = []

    def observed_bulk(db_path, page_paths):
        materialized = tuple(page_paths)
        batch_sizes.append(len(materialized))
        return original_bulk(db_path, materialized)

    def forbidden_scalar(*_args, **_kwargs):
        raise AssertionError("context search must not issue one lifecycle query per page")

    monkeypatch.setattr(
        WikiProjectionLedger,
        "tombstone_states",
        staticmethod(observed_bulk),
    )
    monkeypatch.setattr(
        WikiProjectionLedger,
        "tombstone_state",
        staticmethod(forbidden_scalar),
    )
    searcher = ContextAwareSearch(
        wiki_base=str(wiki),
        wiki_projection_db=ledger.db_path,
    )

    results = _search(
        searcher,
        "BULK-LIFECYCLE-TARGET",
        allow_embedding=False,
        limit=5,
    )

    assert [result.page_path for result in results] == ["page-099.md"]
    assert batch_sizes == [101, 1]


def test_context_search_legacy_kg_recall_is_read_only_and_never_duplicates_semantic(
    tmp_path,
    monkeypatch,
):
    from core.app.context_search import ContextAwareSearch

    wiki = tmp_path / "wiki"
    _write_page(wiki / "allowed.md", body="# Allowed\nLOCAL-KG-READ-ONLY")
    graph_db = tmp_path / "knowledge_graph.db"
    graph_db.touch()
    observed = {}

    class FakeKnowledgeGraph:
        def __init__(self, **kwargs):
            observed["initialize"] = kwargs.get("initialize")
            observed["read_only"] = kwargs.get("read_only")

        def search(self, query, limit, *, allowed_page_paths, allow_semantic):
            observed["query"] = query
            observed["allowed"] = set(allowed_page_paths)
            observed["allow_semantic"] = allow_semantic
            return []

    monkeypatch.setattr(
        "core.kia.knowledge_graph.KnowledgeGraph",
        FakeKnowledgeGraph,
    )
    searcher = ContextAwareSearch(
        wiki_base=str(wiki),
        knowledge_graph_db=graph_db,
    )

    _search(
        searcher,
        "LOCAL-KG-READ-ONLY",
        allow_embedding=False,
        limit=5,
    )

    assert observed == {
        "initialize": False,
        "read_only": True,
        "query": "LOCAL-KG-READ-ONLY",
        "allowed": {"allowed.md"},
        "allow_semantic": False,
    }


def test_bulk_lifecycle_snapshot_distinguishes_active_deleted_and_invalid(tmp_path):
    from core.wiki_projection_lifecycle import WikiProjectionLedger

    active = tmp_path / "active.md"
    deleted = tmp_path / "deleted.md"
    absent = tmp_path / "absent.md"
    _write_page(active)
    _write_page(deleted)
    ledger = WikiProjectionLedger(tmp_path / "wiki_projection.db")
    ledger.record_mutation(active, mutation_type="create")
    ledger.record_mutation(deleted, mutation_type="create")
    ledger.record_mutation(deleted, mutation_type="delete")

    states = WikiProjectionLedger.tombstone_states(
        ledger.db_path,
        (active, deleted, absent),
    )

    assert states[str(active.resolve())] is False
    assert states[str(deleted.resolve())] is True
    assert states[str(absent.resolve())] is False

    invalid_db = tmp_path / "invalid.db"
    import sqlite3

    with sqlite3.connect(invalid_db) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")
    invalid = WikiProjectionLedger.tombstone_states(invalid_db, (active,))
    assert invalid[str(active.resolve())] is None


def test_context_search_uses_custom_wiki_lifecycle_ledger_before_body_read(tmp_path, monkeypatch):
    """A custom project Wiki must not consult the global projection ledger."""

    from core.app.context_search import ContextAwareSearch
    from core.wiki_projection_lifecycle import WikiProjectionLedger

    class FakeConfig:
        wiki_dir = tmp_path / "default-wiki"
        database_dir = tmp_path / "default-db"

        def get(self, _key, default=None):
            return default

    FakeConfig.wiki_dir.mkdir()
    FakeConfig.database_dir.mkdir()
    custom_wiki = tmp_path / "custom-wiki"
    page = custom_wiki / "03-Tech" / "tombstoned.md"
    _write_page(page, body="# Tombstoned\nCUSTOM-WIKI-BODY-MUST-NOT-REACH-RETRIEVAL")
    custom_ledger = WikiProjectionLedger(custom_wiki / ".kg" / "wiki_projection.db")
    custom_ledger.record_mutation(page, mutation_type="create")
    custom_ledger.record_mutation(page, mutation_type="delete")
    monkeypatch.setattr("core.app.context_search.get_config", lambda: FakeConfig())

    def forbidden_body_read(*_args, **_kwargs):
        raise AssertionError("custom lifecycle tombstone must block body read")

    monkeypatch.setattr("core.frontmatter.read_markdown", forbidden_body_read)
    searcher = ContextAwareSearch(
        wiki_base=str(custom_wiki),
        wiki_projection_db=custom_wiki / ".kg" / "wiki_projection.db",
    )

    assert (
        _search(
            searcher,
            "CUSTOM-WIKI-BODY-MUST-NOT-REACH-RETRIEVAL",
            allow_embedding=False,
        )
        == []
    )
    assert searcher.get_last_query_trace()["access_filter"] == {"subject_deleted": 1}


def test_context_search_without_server_principal_never_reads_page_body(tmp_path, monkeypatch):
    from core.app.context_search import ContextAwareSearch

    page = tmp_path / "03-Tech" / "page.md"
    _write_page(page, body="# Page\nBODY-MUST-NOT-BE-READ")

    def forbidden_body_read(*_args, **_kwargs):
        raise AssertionError("body must not be read without a principal")

    monkeypatch.setattr("core.frontmatter.read_markdown", forbidden_body_read)
    searcher = ContextAwareSearch(wiki_base=str(tmp_path))

    assert searcher.search("BODY-MUST-NOT-BE-READ", allow_embedding=False) == []
    assert searcher.get_last_query_trace()["access_filter"] == {"principal_required": 1}


def test_context_search_semantic_exception_redacts_public_trace_and_log(
    tmp_path, monkeypatch, caplog
):
    """Provider exceptions must not escape into query-trace or diagnostic logs."""
    from core.app.context_search import ContextAwareSearch

    sensitive_marker = _sensitive_provider_error_marker()

    class FakeConfig:
        wiki_dir = tmp_path
        data_dir = tmp_path
        database_dir = tmp_path

        def get(self, key, default=None):
            return {"embedding.enabled": True}.get(key, default)

    class FailingDualIndexRetriever:
        def __init__(self, wiki_base=None, **_kwargs):
            self.wiki_base = wiki_base

        def search_detailed(self, *_args, **_kwargs):
            raise RuntimeError(sensitive_marker)

    class FakeReadyIndex:
        client = object()

        def __init__(self, *_args, **_kwargs):
            pass

        def persisted_search_available(self):
            return True

    monkeypatch.setattr("core.config.get_config", lambda: FakeConfig())
    monkeypatch.setattr("core.app.context_search.get_config", lambda: FakeConfig())
    monkeypatch.setattr(
        "core.embeddings.index_manager.EmbeddingIndexManager",
        FakeReadyIndex,
    )
    monkeypatch.setattr("core.embeddings.dual_index.DualIndexRetriever", FailingDualIndexRetriever)
    caplog.set_level(logging.DEBUG)
    (tmp_path / "embedding_index").mkdir()

    searcher = ContextAwareSearch(wiki_base=str(tmp_path))
    searcher._authorized_page_frontmatter = {}
    assert searcher._recall_from_embedding("private query") == []

    trace = searcher.get_last_query_trace()
    assert trace["degraded_reasons"] == ["semantic_recall_exception:provider_error"]
    assert sensitive_marker not in str(trace)
    assert sensitive_marker not in caplog.text
    assert "category=provider_error" in caplog.text
    assert sensitive_marker.encode("utf-8") not in (tmp_path / "model_call_ledger.db").read_bytes()


def test_context_search_no_embedding_client_creates_no_model_call_run(tmp_path, monkeypatch):
    from core.app.context_search import ContextAwareSearch
    import core.telemetry.prompt_call_log as prompt_call_log

    class FakeConfig:
        wiki_dir = tmp_path
        database_dir = tmp_path

        def get(self, key, default=None):
            return {"embedding.enabled": True}.get(key, default)

    class NoClientIndex:
        def __init__(self, *_args, **_kwargs):
            self.client = None

    def forbidden_model_call_run(*_args, **_kwargs):
        raise AssertionError("no provider means no model-call run")

    index_dir = tmp_path / "embedding_index"
    index_dir.mkdir()
    monkeypatch.setattr("core.app.context_search.get_config", lambda: FakeConfig())
    monkeypatch.setattr(
        "core.embeddings.index_manager.EmbeddingIndexManager",
        NoClientIndex,
    )
    monkeypatch.setattr(
        prompt_call_log,
        "model_call_run_scope",
        forbidden_model_call_run,
    )
    searcher = ContextAwareSearch(
        wiki_base=str(tmp_path),
        database_dir=tmp_path,
        embedding_index_dir=index_dir,
    )
    searcher._authorized_page_frontmatter = {}
    searcher.last_query_trace = searcher._new_query_trace("private query", 10, True)

    assert searcher._recall_from_embedding("private query") == []
    assert searcher.get_last_query_trace()["degraded_reasons"] == [
        "page_embedding_client_unavailable"
    ]
    assert not (tmp_path / "model_call_ledger.db").exists()


def test_context_search_records_heat_only_after_authorization(tmp_path):
    from core.app.context_search import ContextAwareSearch

    page = tmp_path / "03-Tech" / "redis.md"
    _write_page(page, "名称: Redis 连接池\n置信度: 0.9\n")

    searcher = ContextAwareSearch(wiki_base=str(tmp_path), database_dir=tmp_path / ".kg")
    metrics = searcher._get_metrics()
    metrics.upsert_page("03-Tech/redis.md", heat_score=5, heat_level="warm")

    result = _search(searcher, "Redis 连接池踩坑", limit=1)[0]
    assert result.page_path == "03-Tech/redis.md"
    assert result.heat_level == "warm"

    searcher.record_authorized_search(
        "Redis 连接池踩坑",
        [result],
        principal=_principal(),
        narrowing=AccessNarrowing(
            session_id="context-search-heat-session",
            project="mnemos",
        ),
    )
    stored = metrics.get_page("03-Tech/redis")

    assert result.heat_level == "hot"
    assert result.heat_score == 8
    assert result.last_accessed
    assert stored.heat_score == 8
    assert stored.last_accessed == result.last_accessed


def test_record_search_click_records_only_canonical_weak_reaction(tmp_path):
    import json
    import sqlite3
    from datetime import datetime

    from core.app.context_search import ContextAwareSearch
    from core.cognitive.access_control import make_cognitive_access_envelope
    from core.cognitive.state_schema import initialize_cognitive_state_schema
    from core.cognitive.state_store import CognitiveStateStore
    from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2
    from core.scoring.subject_provenance import record_scoring_subject_provenance

    search_db = tmp_path / "mnemos.db"
    AdaptiveScorerV2.ensure_tables(str(search_db))
    with sqlite3.connect(search_db) as conn:
        cursor = conn.execute(
            """
            INSERT INTO search_sessions
                (session_id, query, result_paths, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                "search-test",
                "蒸馏",
                json.dumps(["02-Concepts/distill.md"]),
                datetime.now().isoformat(),
            ),
        )
        access_control = make_cognitive_access_envelope(
            owner_principal_id=_principal().principal_id,
            owner_agent="codex",
            scope_type="session",
            scope_id="search-test-scope",
            session_id="search-test-scope",
            project="mnemos",
            purposes=(
                "cognitive_state_read",
                "cognitive_state_write",
                "reflection_experience_read",
                "reflection_read",
                "score_training",
                "search_feedback",
            ),
            consent_provenance_refs=("search-test-consent",),
            sensitivity="sensitive",
            retention_policy="search-test",
            source_acl_lineage=("sha256:" + "a" * 64,),
            visibility="private",
        )
        record_scoring_subject_provenance(
            conn,
            object_type="search_session",
            object_id=str(cursor.lastrowid),
            subject_provenance=access_control,
        )
        conn.commit()

    initialize_cognitive_state_schema(tmp_path / "producer_consumer_ledger.db")

    feedback_result = ContextAwareSearch.record_search_click(
        "02-Concepts/distill.md",
        db_path=search_db,
        principal=_principal(),
        narrowing=AccessNarrowing(
            session_id="search-test-scope",
            project="mnemos",
        ),
    )
    assert feedback_result["success"] is True
    assert feedback_result["terminal_receipt_count"] == 7

    with sqlite3.connect(search_db) as conn:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert "ground_truth_signals" not in tables
    assert "scorer_training_queue" not in tables
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    reaction = state.current_revisions(object_type="user_reaction_event")
    assert len(reaction) == 1
    assert reaction[0].payload["interaction"] == {
        "kind": "clicked",
        "observed_facts": [{"name": "clicked", "value": True}],
    }
    assert not (tmp_path / "reflections.db").exists()
    assert not (tmp_path / "rule_weight_optimizer.db").exists()


@pytest.mark.parametrize(
    ("interaction", "result_path", "expected_kind"),
    (
        ("open", "02-Concepts/distill.md", "opened"),
        ("click", "02-Concepts/distill.md", "clicked"),
        ("ignore", "", "ignore"),
        ("no_click", "", "no_click"),
        ("silence", "", "silence_window_closed"),
    ),
)
def test_context_search_feedback_entrypoint_covers_fixed_interaction_taxonomy(
    tmp_path,
    interaction,
    result_path,
    expected_kind,
):
    from core.cognitive.access_control import make_cognitive_access_envelope
    from core.cognitive.feedback_entrypoints import record_context_search_feedback
    from core.cognitive.state_schema import initialize_cognitive_state_schema
    from core.cognitive.state_store import CognitiveStateStore

    access_control = make_cognitive_access_envelope(
        owner_principal_id=_principal().principal_id,
        owner_agent="codex",
        scope_type="session",
        scope_id="search-taxonomy-scope",
        session_id="search-taxonomy-scope",
        project="mnemos",
        purposes=("cognitive_state_read", "cognitive_state_write", "search_feedback"),
        consent_provenance_refs=("search-taxonomy-consent",),
        sensitivity="sensitive",
        retention_policy="search-test",
        source_acl_lineage=("sha256:" + "b" * 64,),
        visibility="private",
    )
    initialize_cognitive_state_schema(tmp_path / "producer_consumer_ledger.db")

    result = record_context_search_feedback(
        database_dir=tmp_path,
        search_object_id=1,
        search_session_id="search-taxonomy-session",
        query="蒸馏",
        result_paths_json='["02-Concepts/distill.md"]',
        interaction=interaction,
        result_path=result_path,
        access_control=access_control,
        principal=_principal(),
    )

    assert result["success"] is True
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    reaction = state.current_revisions(object_type="user_reaction_event")
    assert len(reaction) == 1
    assert reaction[0].payload["interaction"]["kind"] == expected_kind
    assert reaction[0].payload["authority_class"] == (
        "explicit_user" if expected_kind == "ignore" else "tool_observation"
    )
    assert result["disposition"] == "record_only"


def test_context_search_excludes_hidden_dirs(tmp_path):
    from core.app.context_search import ContextAwareSearch

    # .git 等隐藏目录应被排除
    _write_page(tmp_path / ".git" / "old.md", body="Redis 连接池踩坑")

    assert _search(ContextAwareSearch(wiki_base=str(tmp_path)), "Redis 连接池踩坑") == []


def test_question_answer_search_reuses_context_aware_search(tmp_path):
    from core.app.question_answer_search import QuestionAnswerSearch

    _write_page(
        tmp_path / "03-Tech" / "redis.md", body="# Redis\n步骤：首先复用连接池，然后设置超时。"
    )

    answer = QuestionAnswerSearch(wiki_dir=tmp_path).answer(
        "如何处理 Redis 连接池？",
        principal=_principal(),
        narrowing=AccessNarrowing(),
    )

    assert answer is not None
    assert answer["question_type"] == "procedure"
    assert "连接池" in answer["answer"]


def test_compute_persona_score_malformed_value_returns_zero(tmp_path):
    """Untrusted frontmatter persona_alignment cannot affect ranking."""
    from core.app.context_search import ContextAwareSearch

    searcher = ContextAwareSearch(wiki_base=str(tmp_path))
    candidate = {"frontmatter": {"persona_alignment": {"total": "not-a-number"}}}
    assert searcher._compute_persona_score(candidate, {}) == 0.0


def test_compute_persona_score_requires_real_assertion_candidate_match(tmp_path):
    from core.app.context_search import ContextAwareSearch

    searcher = ContextAwareSearch(wiki_base=str(tmp_path))
    profile = {
        "persona_assertions": [
            {
                "assertion_id": "pa_fix_test_doc",
                "claim": "用户希望每个问题按修复、测试、文档同步、本地提交的闭环处理。",
                "confidence": 0.9,
            }
        ]
    }
    related = {
        "title": "问题清单闭环修复",
        "content": "修复后执行测试，再同步文档并完成本地提交。",
        "frontmatter": {"persona_alignment": {"total": 0}},
    }
    unrelated = {
        "title": "Redis 连接池",
        "content": "使用连接池并设置超时。",
        "frontmatter": {"persona_alignment": {"total": 1}},
    }

    assert searcher._compute_persona_score(related, profile) > 0.0
    assert searcher._compute_persona_score(unrelated, profile) == 0.0


def test_get_metrics_oserror_returns_none(tmp_path, monkeypatch):
    """WikiMetrics 初始化失败时应返回 None 而不抛异常"""
    from core.app.context_search import ContextAwareSearch

    def raise_oserror(*args, **kwargs):
        raise OSError("fake db error")

    monkeypatch.setattr("core.wiki_metrics.WikiMetrics", raise_oserror)
    searcher = ContextAwareSearch(wiki_base=str(tmp_path))
    assert searcher._get_metrics() is None
