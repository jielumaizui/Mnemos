# -*- coding: utf-8 -*-
"""Agora facade injection tests."""

from __future__ import annotations

import hashlib
import json

import pytest

from core.access_policy import MCP_TOOL_POLICIES
from core.agent_kit.authorization import AgentAuthorizationStore
from integrations.agora import MCPServer


class _SavedResult:
    uid = "uid-1"
    tags = ["human", "mnemos-ingest"]


class _FakeBackend:
    def __init__(self, calls):
        self.calls = calls

    def save(self, content, tags, title):
        self.calls.append(("storage_backend.save", content, tags, title))
        return [_SavedResult()]


class FakeFacade:
    def __init__(self):
        self.calls = []

    def health_check(self):
        self.calls.append("health_check")
        return {"success": True, "source": "fake-health"}

    def agent_runtime_probe(self, source_agent, health_check_ids_hash, sample):
        self.calls.append(
            ("agent_runtime_probe", source_agent, health_check_ids_hash, sample)
        )
        return {"success": True, "runtime_state": "verified"}

    def agent_health_observed(self, source_agent, health_check_ids_hash):
        self.calls.append(
            ("agent_health_observed", source_agent, health_check_ids_hash)
        )
        return {"success": True}

    def self_diagnose(self):
        self.calls.append("self_diagnose")
        return {"success": True, "source": "fake-diagnose"}

    def configure_wiki(self, vault_path: str):
        self.calls.append(("configure_wiki", vault_path))
        return {"success": True, "vault_path": vault_path}

    def detect_sources(self):
        self.calls.append("detect_sources")
        return {"success": True, "sources": {"fake": {"detected": True}}}

    def storage_backend(self):
        self.calls.append("storage_backend")
        return _FakeBackend(self.calls)

    def session_search(
        self,
        query="",
        session_id="",
        uid="",
        limit=10,
        days=None,
        source=None,
        *,
        principal=None,
        narrowing=None,
    ):
        self.calls.append(
            (
                "session_search",
                query,
                session_id,
                uid,
                limit,
                days,
                source,
                principal.agent,
                narrowing.session_id,
                narrowing.project,
            )
        )
        return {"success": True, "results": [{"session_id": session_id or "sess-1"}]}

    def knowledge_ingest(self, content, tags=None, *, principal=None):
        self.calls.append(("knowledge_ingest", content, tags, principal.agent))
        return {"success": True, "uid": "uid-1", "tags": list(tags or []) + ["human"]}

    def wiki_search(self, query: str, limit: int = 5, *, principal=None, narrowing=None):
        self.calls.append(("wiki_search", query, limit, principal.agent))
        return [
            {
                "page_id": "00-Inbox/fake",
                "title": "Fake page",
                "type": "00-Inbox",
                "source": "",
            }
        ], {"allowed": 1}

    def wiki_read(self, page_path: str, *, principal=None, narrowing=None):
        self.calls.append(("wiki_read", page_path, principal.agent))
        return {"success": True, "content": "fake page body", "path": page_path}

    def wiki_write(
        self,
        page_path: str,
        content: str,
        frontmatter=None,
        *,
        principal=None,
        session_id="",
        project="",
    ):
        self.calls.append(
            ("wiki_write", page_path, content, frontmatter, session_id, project)
        )
        return {"success": True, "path": page_path, "size": len(content)}

    def knowledge_source_list(self):
        self.calls.append("knowledge_source_list")
        return {
            "success": True,
            "sources": {"human_written": 1, "distilled": 2},
            "total": 3,
        }

    def session_save(self, session_id, messages, tags=None, source_agent="unknown"):
        self.calls.append(("session_save", session_id, messages, tags, source_agent))
        return {"success": True, "session_id": session_id, "source_agent": source_agent}

    def capture_turn(self, **kwargs):
        self.calls.append(("capture_turn", kwargs))
        return {
            "success": True,
            "status": "queued",
            "session_id": kwargs["session_id"],
            "turn_number": kwargs["turn_number"],
        }

    def capture_session(self, source_agent, session_id, turns):
        self.calls.append(("capture_session", source_agent, session_id, turns))
        return {"success": True, "status": "queued", "queued_count": len(turns)}

    def end_session(self, source_agent, session_id):
        self.calls.append(("end_session", source_agent, session_id))
        return {"success": True, "status": "ended", "session_id": session_id}

    def capture_status(self, source_agent, session_id, turn_number=-1):
        self.calls.append(("capture_status", source_agent, session_id, turn_number))
        return {"success": True, "status": "queued", "session_id": session_id}

    def knowledge_distill(
        self,
        session_id,
        messages,
        write_to_wiki=True,
        *,
        principal=None,
    ):
        self.calls.append(
            ("knowledge_distill", session_id, messages, write_to_wiki, principal.agent)
        )
        return {"success": True, "session_id": session_id, "queued": True}

    def document_process(
        self,
        file_path,
        title="",
        mode="distill",
        *,
        principal=None,
    ):
        self.calls.append(
            (
                "document_process",
                file_path,
                title,
                mode,
                principal.agent,
            )
        )
        return {"success": True, "title": title or "Fake document", "file_path": file_path}

    def wiki_build(self, dry_run=False):
        self.calls.append(("wiki_build", dry_run))
        return {"success": True, "dry_run": dry_run, "result": {"built": 0}}

    def preflight_inject(
        self,
        task_type,
        subtype="",
        context_text="",
        *,
        principal=None,
        narrowing=None,
    ):
        self.calls.append(("preflight_inject", task_type, subtype, context_text))
        return {"success": True, "loaded": True, "source": "fake-preflight"}

    def check_pending_recaps(self, user_context=None, limit=5):
        self.calls.append(("check_pending_recaps", user_context, limit))
        return {"success": True, "pending_count": 0, "items": []}

    def recap_start(
        self,
        task_id="",
        topic="",
        mode="minimal",
        source_agent="",
        owner_agent="",
        source_agents=None,
        session_id="",
        context=None,
        project="",
        task_type="",
        subtype="",
    ):
        self.calls.append(
            (
                "recap_start",
                task_id,
                topic,
                mode,
                source_agent,
                owner_agent,
                source_agents,
                session_id,
                context,
                project,
                task_type,
                subtype,
            )
        )
        return {"success": True, "recap_id": "retro-1", "state": "q1_goal_actual"}

    def recap_submit(self, recap_id, answers, confirm_level="draft", source_agent=""):
        self.calls.append(("recap_submit", recap_id, answers, confirm_level, source_agent))
        return {"success": True, "recap_id": recap_id, "state": "draft_generated"}

    def recap_finalize(
        self,
        recap_id,
        write_policy="save_and_index",
        follow_up_at="",
        confirmed_by_user=True,
        source_agent="",
    ):
        self.calls.append(
            (
                "recap_finalize",
                recap_id,
                write_policy,
                follow_up_at,
                confirmed_by_user,
                source_agent,
            )
        )
        return {"success": True, "page_path": "06-Retrospectives/复盘/fake.md"}

    def recap_skip(
        self,
        recap_id="",
        task_id="",
        skip_reason="",
        user_note="",
        owner_agent="",
        source_agent="",
    ):
        self.calls.append(
            ("recap_skip", recap_id, task_id, skip_reason, user_note, owner_agent, source_agent)
        )
        return {"success": True, "event_id": "skip-1", "skip_status": "deferred"}

    def recap_feedback(
        self,
        recap_id,
        feedback_type,
        comment="",
        source_agent="",
        supersedes_event_id="",
        *,
        principal=None,
        narrowing=None,
    ):
        assert principal is not None
        assert narrowing is not None
        self.calls.append(
            (
                "recap_feedback",
                recap_id,
                feedback_type,
                comment,
                source_agent,
                supersedes_event_id,
            )
        )
        return {"success": True, "event_id": "feedback-1"}

    def recap_status(self, recap_id="", task_id="", source_agent=""):
        self.calls.append(("recap_status", recap_id, task_id, source_agent))
        return {"success": True, "recap_id": recap_id or "retro-1", "state": "q1_goal_actual"}

    def recap_claim_owner(self, recap_id, owner_agent, current_session_id=""):
        self.calls.append(("recap_claim_owner", recap_id, owner_agent, current_session_id))
        return {"success": True, "owner_agent": owner_agent}

    def guard_check(
        self,
        user_message,
        ai_response="",
        task_type="",
        subtype="",
        context=None,
        *,
        principal=None,
        narrowing=None,
    ):
        self.calls.append(
            ("guard_check", user_message, ai_response, task_type, subtype, context)
        )
        return {"success": True, "alert": False, "source": "fake-guard"}

    def retrospective_list(
        self,
        task_type=None,
        limit=10,
        *,
        principal=None,
        narrowing=None,
    ):
        self.calls.append(
            (
                "retrospective_list",
                task_type,
                limit,
                principal.agent,
                narrowing.session_id,
                narrowing.project,
            )
        )
        return {"success": True, "retrospectives": [{"title": "Fake Retro"}]}

    def persona_summary(self, *, principal=None, narrowing=None):
        self.calls.append("persona_summary")
        return {"success": True, "profile": {"energy": {"focus_depth": 0.9}}}

    def persona_behavior_prompt(self, *, principal=None, narrowing=None):
        self.calls.append("persona_behavior_prompt")
        return {"success": True, "behavior_prompts": ["fake prompt"]}

    def persona_behavior_metrics(self, days=30, *, principal=None, narrowing=None):
        self.calls.append(("persona_behavior_metrics", days))
        return {"success": True, "days": days, "total_calls": 1}

    def load_onboarding_prompt(self):
        self.calls.append("load_onboarding_prompt")
        return "fake onboarding"

    def signal_collect(self, sources=None):
        self.calls.append(("signal_collect", sources))
        return {"success": True, "results": {"sources": sources}}

    def persona_update(self, *, principal=None, narrowing=None):
        del principal, narrowing
        self.calls.append("persona_update")
        return {"success": True, "message": "fake update"}

    def context_aware_search(
        self,
        query,
        limit=10,
        working_dir="",
        session_id="",
        project="",
        *,
        principal=None,
    ):
        self.calls.append(
            (
                "context_aware_search",
                query,
                limit,
                working_dir,
                session_id,
                project,
                principal.agent,
            )
        )
        return {"success": True, "query": query, "results": [{"title": "Fake Result"}]}

    def intent_route(self, user_input, working_dir=""):
        self.calls.append(("intent_route", user_input, working_dir))
        return {"success": True, "intent": "knowledge", "confidence": 0.9}

    def intent_correct(self, user_input, original_intent, corrected_intent):
        self.calls.append(("intent_correct", user_input, original_intent, corrected_intent))
        return {"success": True, "corrected_intent": corrected_intent}

    def blindspot_check(
        self,
        query,
        session_id="",
        *,
        principal=None,
        narrowing=None,
    ):
        self.calls.append(
            (
                "blindspot_check",
                query,
                session_id,
                principal.agent,
                narrowing.session_id,
                narrowing.project,
            )
        )
        return {"success": True, "blindspot_found": False}

    def predictive_push(
        self,
        user_input,
        working_dir="",
        *,
        principal=None,
        narrowing=None,
    ):
        self.calls.append(
            (
                "predictive_push",
                user_input,
                working_dir,
                principal.agent,
                narrowing.session_id,
                narrowing.project,
            )
        )
        return {"success": True, "push_available": False}

    def build_cognitive_state(self, context, *, principal=None, narrowing=None):
        self.calls.append(
            ("build_cognitive_state", context, principal.agent, narrowing.session_id, narrowing.project)
        )
        return {"success": True, "schema_version": "mnemos.cognitive_state_read_model.v1"}

    def record_decision(self, trace, *, principal=None, source_authority_catalog=None):
        self.calls.append(
            ("record_decision", trace["source"]["source_revision_id"], principal.agent,
             source_authority_catalog.catalog_hash)
        )
        return {"success": True, "schema_version": "mnemos.decision_receipt.v1"}

    def apply_outcome(self, feedback, *, principal=None, source_authority_catalog=None):
        self.calls.append(
            ("apply_outcome", feedback["source"]["source_revision_id"], principal.agent,
             source_authority_catalog.catalog_hash)
        )
        return {"success": True, "schema_version": "mnemos.outcome_receipt.v1"}

    def record_explicit_profile_evidence(
        self,
        request,
        *,
        principal=None,
        narrowing=None,
        source_authority_catalog=None,
    ):
        self.calls.append(
            (
                "record_explicit_profile_evidence",
                request["source_authority_id"],
                principal.agent,
                narrowing.session_id,
                narrowing.project,
                source_authority_catalog.catalog_hash,
            )
        )
        return {"success": True, "status": "recorded"}

    def record_delivery_display(self, delivery_event_id, rendered_content_hash, *, principal=None):
        self.calls.append(
            ("record_delivery_display", delivery_event_id, rendered_content_hash, principal.agent)
        )
        return {"success": True, "status": "recorded"}

    def push_feedback(
        self,
        topic,
        action,
        delivery_event_id,
        *,
        principal=None,
        narrowing=None,
        supersedes_event_id="",
        correction_target_ref="",
        correction_reason="",
    ):
        self.calls.append(
            (
                "push_feedback",
                topic,
                action,
                delivery_event_id,
                principal.agent,
                narrowing.session_id,
                narrowing.project,
            )
        )
        return {"success": True, "topic": topic.lower(), "action": action}

    def freshness_check(
        self,
        entity_name,
        *,
        principal=None,
        narrowing=None,
    ):
        self.calls.append(
            (
                "freshness_check",
                entity_name,
                principal.agent,
                narrowing.session_id,
                narrowing.project,
            )
        )
        return {"success": True, "entity_name": entity_name, "fresh": True}

    def observation_run(self, full=False, since=""):
        self.calls.append(("observation_run", full, since))
        return {"success": True, "observations": 1, "dimensions": 1}

    def observation_search(
        self,
        dimension="",
        source_type="",
        limit=20,
        *,
        principal=None,
        narrowing=None,
        purpose="observation_read",
    ):
        self.calls.append(
            (
                "observation_search",
                dimension,
                source_type,
                limit,
                principal.agent,
                narrowing.session_id,
                narrowing.project,
                purpose,
            )
        )
        return {"success": True, "count": 1, "observations": [{"id": "obs-1"}]}

    def reflect_on_input(self, text, auto_llm=True, *, principal=None, narrowing=None):
        self.calls.append(
            (
                "reflect_on_input",
                text,
                auto_llm,
                principal.agent if principal else "",
                narrowing.session_id if narrowing else "",
                narrowing.project if narrowing else "",
            )
        )
        return {"success": True, "triggered": True, "insight_summary": "fake insight"}

    def reflect_manually(self, query="", auto_llm=True, *, principal=None, narrowing=None):
        self.calls.append(
            (
                "reflect_manually",
                query,
                auto_llm,
                principal.agent if principal else "",
                narrowing.session_id if narrowing else "",
                narrowing.project if narrowing else "",
            )
        )
        return {"success": True, "triggered": True, "insight_summary": "manual insight"}

    def reflection_feedback(
        self,
        reflection_id,
        feedback_type,
        comment="",
        *,
        principal=None,
        narrowing=None,
        supersedes_event_id="",
        correction_target_ref="",
        correction_reason="",
    ):
        self.calls.append(
            (
                "reflection_feedback",
                reflection_id,
                feedback_type,
                comment,
                principal.agent if principal else "",
                narrowing.session_id if narrowing else "",
                narrowing.project if narrowing else "",
            )
        )
        return {"success": True, "reflection_id": reflection_id, "feedback_type": feedback_type}

    def reflection_pending(self, hours_since=24, limit=20, *, principal=None, narrowing=None):
        self.calls.append(
            (
                "reflection_pending",
                hours_since,
                limit,
                principal.agent if principal else "",
                narrowing.session_id if narrowing else "",
                narrowing.project if narrowing else "",
            )
        )
        return {"success": True, "count": 1, "pending": [{"id": "reflection-1"}]}

    def infer_type_from_path(self, page_path):
        self.calls.append(("infer_type_from_path", page_path))
        return "fake-type"

    def scope_slug(self, value):
        self.calls.append(("scope_slug", value))
        return "fake-slug"

    def scope_page_path(self, scope, title, page_path="", scope_name=""):
        self.calls.append(("scope_page_path", scope, title, page_path, scope_name))
        return "fake/path.md"

    def memory_write_project(
        self,
        title,
        content,
        project="",
        page_path="",
        frontmatter=None,
        *,
        principal=None,
    ):
        self.calls.append(("memory_write_project", title, content, project, page_path, frontmatter))
        return {"success": True, "scope": "project", "project": project or "default"}

    def memory_write_framework(
        self,
        title,
        content,
        framework="",
        page_path="",
        frontmatter=None,
        *,
        principal=None,
    ):
        self.calls.append(
            ("memory_write_framework", title, content, framework, page_path, frontmatter)
        )
        return {"success": True, "scope": "framework", "framework": framework or "general"}

    def memory_write_global(
        self,
        title,
        content,
        page_path="",
        frontmatter=None,
        *,
        principal=None,
    ):
        self.calls.append(("memory_write_global", title, content, page_path, frontmatter))
        return {"success": True, "scope": "global"}

    def memory_search(
        self,
        query,
        scope="all",
        limit=5,
        *,
        principal=None,
        narrowing=None,
    ):
        self.calls.append(
            (
                "memory_search",
                query,
                scope,
                limit,
                principal.agent,
                narrowing.session_id,
                narrowing.project,
            )
        )
        return {"success": True, "scope": scope, "results": [{"title": "Memory"}]}


def _authorized_server(facade, tmp_path):
    store = AgentAuthorizationStore(tmp_path / "agent_authorization.db")
    credential = store.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities=set(MCP_TOOL_POLICIES.values()),
        allowed_projects={"mnemos"},
        allowed_source_agents={"hermes"},
    )
    return MCPServer(
        facade=facade,
        launch_credential=credential,
        authorization_store=store,
    )


def test_mcp_server_uses_injected_facade_for_system_tools():
    facade = FakeFacade()
    server = MCPServer(facade=facade)

    health = server._tool_health_check()
    assert health["schema_version"] == "mnemos.mcp_health.v1"
    assert health["status"] == "unknown"
    assert server._tool_self_diagnose()["source"] == "fake-diagnose"
    assert server._tool_configure_wiki("/tmp/mnemos-test")["vault_path"] == "/tmp/mnemos-test"
    assert server._tool_detect_sources()["sources"]["fake"]["detected"] is True
    assert facade.calls == [
        "health_check",
        "self_diagnose",
        ("configure_wiki", "/tmp/mnemos-test"),
        "detect_sources",
    ]


def test_mcp_runtime_probe_binds_receipt_to_authenticated_principal(tmp_path):
    facade = FakeFacade()
    server = _authorized_server(facade, tmp_path)
    sample = {"schema_version": "mnemos.agent_runtime_probe.v1"}

    result = server._tool_agent_runtime_probe("health-hash", sample)

    assert result["runtime_state"] == "verified"
    assert facade.calls == [
        ("agent_runtime_probe", "codex", "health-hash", sample)
    ]


def test_mcp_health_records_authenticated_roundtrip_for_followup_probe(tmp_path):
    facade = FakeFacade()
    facade.health_check = lambda: {
        "status": "ok",
        "health_check_ids_hash": "canonical-hash",
    }
    server = _authorized_server(facade, tmp_path)

    result = server._tool_health_check()

    assert result["health_check_ids_hash"] == "canonical-hash"
    assert facade.calls == [
        ("agent_health_observed", "codex", "canonical-hash")
    ]


def test_mcp_health_is_bounded_even_when_diagnostic_report_is_large(tmp_path):
    facade = FakeFacade()
    facade.health_check = lambda: {
        "ok": False,
        "usable": False,
        "strict_ok": False,
        "status": "degraded",
        "health_check_ids": ["storage", "agent"],
        "health_check_ids_hash": "canonical-hash",
        "checks": {
            "storage": {"status": "ok", "large_inventory": "x" * 100_000},
            "agent": {"status": "degraded", "nested": {"x": "y"}},
        },
        "errors": ["x" * 100_000],
        "auto_healing": {"evidence": ["x" * 100_000]},
        "strict_failures": ["agent"],
        "degraded_checks": ["agent"],
    }
    server = _authorized_server(facade, tmp_path)

    result = server._tool_health_check()

    assert result["checks"] == {"storage": "ok", "agent": "degraded"}
    assert result["health_check_ids_hash"] == "canonical-hash"
    assert result["strict_failures"] == ["agent"]
    assert "errors" not in result
    assert len(json.dumps(result, ensure_ascii=False)) < 2_000


def test_mcp_server_uses_injected_facade_for_wiki_and_storage_tools(tmp_path):
    facade = FakeFacade()
    server = _authorized_server(facade, tmp_path)

    search = server._tool_wiki_search("facade query", limit=2)
    ingest = server._tool_knowledge_ingest("remember this")

    assert search["success"] is True
    assert search["results"][0]["title"] == "Fake page"
    assert ingest["success"] is True
    assert ingest["uid"] == "uid-1"
    read = server._tool_wiki_read("00-Inbox/fake.md")
    write = server._tool_wiki_write(
        "00-Inbox/new.md",
        "new body",
        frontmatter={"title": "New"},
        session_id="session-1",
        project="mnemos",
    )
    sources = server._tool_knowledge_source_list()

    assert read["content"] == "fake page body"
    assert write["size"] == len("new body")
    assert sources["total"] == 3
    assert facade.calls == [
        ("wiki_search", "facade query", 2, "codex"),
        ("knowledge_ingest", "remember this", None, "codex"),
        ("wiki_read", "00-Inbox/fake.md", "codex"),
        (
            "wiki_write",
            "00-Inbox/new.md",
            "new body",
            {"title": "New"},
            "session-1",
            "mnemos",
        ),
        "knowledge_source_list",
    ]


def test_mcp_server_uses_injected_facade_for_session_search(monkeypatch, tmp_path):
    def fail_raw_index(*args, **kwargs):
        raise AssertionError("session_search should use the injected facade")

    monkeypatch.setattr("core.app.raw_search.RawIndex.__init__", fail_raw_index)
    facade = FakeFacade()
    server = _authorized_server(facade, tmp_path)

    result = server._tool_session_search(
        query="hello",
        session_id="sess-1",
        uid="uid-1",
        limit=3,
        days=7,
        source="codex",
        project="mnemos",
    )

    assert result["results"][0]["session_id"] == "sess-1"
    assert facade.calls == [
        (
            "session_search",
            "hello",
            "sess-1",
            "uid-1",
            3,
            7,
            "codex",
            "codex",
            "sess-1",
            "mnemos",
        )
    ]


def test_mcp_server_uses_injected_facade_for_capture_tools(monkeypatch, tmp_path):
    class NoRealCaptureService:
        def __init__(self, *args, **kwargs):
            raise AssertionError("Capture tools should use the injected facade")

    monkeypatch.setattr(
        "core.sync_framework.capture_service.CaptureService",
        NoRealCaptureService,
    )
    facade = FakeFacade()
    server = _authorized_server(facade, tmp_path)
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    turns = [{"turn_number": 0, "user_content": "hello", "assistant_content": "hi"}]

    save = server._tool_session_save(
        "sess-1",
        messages,
        tags=["manual"],
    )
    turn = server._tool_capture_turn(
        session_id="sess-1",
        turn_number=1,
        user_content="hello",
        assistant_content="hi",
    )
    session = server._tool_capture_session("sess-1", turns)
    ended = server._tool_end_session("sess-1")
    status = server._tool_capture_status("sess-1", turn_number=1)

    assert save["success"] is True
    assert turn["status"] == "queued"
    assert session["queued_count"] == 1
    assert ended["status"] == "ended"
    assert status["success"] is True
    assert facade.calls == [
        ("session_save", "sess-1", messages, ["manual"], "codex"),
        (
            "capture_turn",
            {
                "source_agent": "codex",
                "session_id": "sess-1",
                "turn_id": "",
                "turn_number": 1,
                "user_content": "hello",
                "assistant_content": "hi",
                "timestamp": "",
                "model": "",
                "cwd": "",
                "metadata": None,
                "tool_calls": None,
                "tool_results": None,
                "reasoning": "",
                "attachments": None,
                "raw_event_refs": None,
                "source_files": None,
                "completeness": None,
            },
        ),
        ("capture_session", "codex", "sess-1", turns),
        ("end_session", "codex", "sess-1"),
        ("capture_status", "codex", "sess-1", 1),
    ]


def test_mcp_server_uses_injected_facade_for_distillation_tools(monkeypatch, tmp_path):
    def fail_enqueue(*args, **kwargs):
        raise AssertionError("knowledge_distill should use the injected facade")

    def fail_backend(*args, **kwargs):
        raise AssertionError("wiki_build should use the injected facade")

    monkeypatch.setattr("integrations.active_bridge._enqueue_session", fail_enqueue)
    monkeypatch.setattr("core.sync_framework.storage_backend.create_storage_backend", fail_backend)
    facade = FakeFacade()
    server = _authorized_server(facade, tmp_path)
    messages = [{"role": "user", "content": "remember this"}]

    distill = server._tool_knowledge_distill("sess-distill", messages, write_to_wiki=False)
    document = server._tool_document_process(
        "/tmp/not-needed-by-facade.pdf",
        title="Facade Doc",
        mode="distill",
    )
    build = server._tool_wiki_build(dry_run=True)

    assert distill["queued"] is True
    assert document["title"] == "Facade Doc"
    assert build["dry_run"] is True
    assert facade.calls == [
        ("knowledge_distill", "sess-distill", messages, False, "codex"),
        (
            "document_process",
            "/tmp/not-needed-by-facade.pdf",
            "Facade Doc",
            "distill",
            "codex",
        ),
        ("wiki_build", True),
    ]


def test_mcp_server_uses_injected_facade_for_kia_tools(tmp_path):
    facade = FakeFacade()
    server = _authorized_server(facade, tmp_path)
    context = {"current_file": "core/example.py"}
    answers = {
        "goal_actual": "目标：A\n实际：B",
        "cause_lesson": "执行漏了",
        "next_handling": "下次先验证",
    }

    preflight = server._tool_preflight_inject("coding", subtype="refactor", context_text="ctx")
    recaps = server._tool_check_pending_recaps(user_context=context, limit=2)
    start = server._tool_recap_start(
        task_id="task-1",
        context=context,
        task_type="coding",
    )
    submit = server._tool_recap_submit("retro-1", answers)
    finalize = server._tool_recap_finalize("retro-1", follow_up_at="2026-07-10T09:00:00")
    skip = server._tool_recap_skip(
        recap_id="retro-1",
        skip_reason="no_time",
    )
    feedback = server._tool_recap_feedback("retro-1", "useful", comment="ok")
    status = server._tool_recap_status(recap_id="retro-1")
    owner = server._tool_recap_claim_owner("retro-1", "session-1")
    guard = server._tool_guard_check(
        user_message="继续修复",
        ai_response="",
        task_type="coding",
        subtype="refactor",
        context=context,
    )
    retros = server._tool_retrospective_list(
        task_type="coding",
        limit=3,
        session_id="session-1",
        project="mnemos",
    )

    assert preflight["source"] == "fake-preflight"
    assert recaps["pending_count"] == 0
    assert start["recap_id"] == "retro-1"
    assert submit["state"] == "draft_generated"
    assert finalize["page_path"].startswith("06-Retrospectives/")
    assert skip["skip_status"] == "deferred"
    assert feedback["event_id"] == "feedback-1"
    assert status["state"] == "q1_goal_actual"
    assert owner["owner_agent"] == "codex"
    assert guard["source"] == "fake-guard"
    assert retros["retrospectives"][0]["title"] == "Fake Retro"
    assert facade.calls == [
        ("preflight_inject", "coding", "refactor", "ctx"),
        ("check_pending_recaps", context, 2),
        (
            "recap_start",
            "task-1",
            "",
            "minimal",
            "codex",
            "codex",
            ["codex"],
            "",
            context,
            "",
            "coding",
            "",
        ),
        ("recap_submit", "retro-1", answers, "draft", "codex"),
        (
            "recap_finalize",
            "retro-1",
            "save_and_index",
            "2026-07-10T09:00:00",
            True,
            "codex",
        ),
        ("recap_skip", "retro-1", "", "no_time", "", "codex", "codex"),
        ("recap_feedback", "retro-1", "useful", "ok", "codex", ""),
        ("recap_status", "retro-1", "", "codex"),
        ("recap_claim_owner", "retro-1", "codex", "session-1"),
        ("guard_check", "继续修复", "", "coding", "refactor", context),
        ("retrospective_list", "coding", 3, "codex", "session-1", "mnemos"),
    ]


def test_mcp_server_uses_injected_facade_for_persona_tools(tmp_path):
    facade = FakeFacade()
    authorization_store = AgentAuthorizationStore(tmp_path / "persona-tools-auth.db")
    credential = authorization_store.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities=set(MCP_TOOL_POLICIES.values()),
        allowed_projects={"mnemos"},
    )
    server = MCPServer(
        facade=facade,
        launch_credential=credential,
        authorization_store=authorization_store,
    )

    summary = server._tool_persona_summary()
    prompt = server._tool_persona_behavior_prompt()
    metrics = server._tool_persona_behavior_metrics(days=7)
    onboarding = server._load_onboarding_prompt()
    signals = server._tool_signal_collect(sources=["codex"])
    update = server._tool_persona_update()

    assert summary["profile"]["energy"]["focus_depth"] == 0.9
    assert prompt["behavior_prompts"] == ["fake prompt"]
    assert metrics["days"] == 7
    assert onboarding == "fake onboarding"
    assert signals["results"]["sources"] == ["codex"]
    assert update["message"] == "fake update"
    assert facade.calls == [
        "persona_summary",
        "persona_behavior_prompt",
        ("persona_behavior_metrics", 7),
        "load_onboarding_prompt",
        ("signal_collect", ["codex"]),
        "persona_update",
    ]


def test_mcp_server_uses_injected_facade_for_intelligence_tools(monkeypatch, tmp_path):
    def fail_search(*args, **kwargs):
        raise AssertionError("intelligence tools should use the injected facade")

    monkeypatch.setattr("core.app.context_search.ContextAwareSearch.search", fail_search)
    facade = FakeFacade()
    server = _authorized_server(facade, tmp_path)

    search = server._tool_context_aware_search(
        "query",
        limit=2,
        working_dir="/tmp/project",
        session_id="sess",
        project="mnemos",
    )
    route = server._tool_intent_route("查资料", working_dir="/tmp/project")
    correction = server._tool_intent_correct("查资料", "chat", "knowledge")
    blindspot = server._tool_blindspot_check("missing thing", session_id="sess")
    push = server._tool_predictive_push(
        "redis error",
        working_dir="/tmp/project",
        session_id="sess",
        project="mnemos",
    )
    feedback = server._tool_push_feedback(
        "Docker",
        "accept",
        "delivery-1",
        session_id="sess",
        project="mnemos",
    )
    freshness = server._tool_freshness_check(
        "Python",
        session_id="sess",
        project="mnemos",
    )

    assert search["results"][0]["title"] == "Fake Result"
    assert route["intent"] == "knowledge"
    assert correction["corrected_intent"] == "knowledge"
    assert blindspot["blindspot_found"] is False
    assert push["push_available"] is False
    assert feedback["action"] == "accept"
    assert freshness["fresh"] is True
    assert facade.calls == [
        (
            "context_aware_search",
            "query",
            2,
            "/tmp/project",
            "sess",
            "mnemos",
            "codex",
        ),
        ("intent_route", "查资料", "/tmp/project"),
        ("intent_correct", "查资料", "chat", "knowledge"),
        ("blindspot_check", "missing thing", "sess", "codex", "sess", ""),
        (
            "predictive_push",
            "redis error",
            "/tmp/project",
            "codex",
            "sess",
            "mnemos",
        ),
        ("push_feedback", "Docker", "accept", "delivery-1", "codex", "sess", "mnemos"),
        ("freshness_check", "Python", "codex", "sess", "mnemos"),
    ]


def test_mcp_server_uses_one_facade_contract_for_cognitive_loop_tools(tmp_path):
    facade = FakeFacade()
    server = _authorized_server(facade, tmp_path)
    content = "record this exact decision"
    source_message = {
        "role": "user",
        "content": content,
        "source_span": {
            "revision_id": "raw-revision-1",
            "role": "user",
            "span_start": 0,
            "span_end": len(content),
            "content_hash": "sha256:" + hashlib.sha256(content.encode()).hexdigest(),
        },
    }
    trace = {"source": {"source_revision_id": "raw-revision-1"}}

    state = server._tool_build_cognitive_state({"scope_type": "project"}, "sess", "mnemos")
    decision = server._tool_record_decision(trace, [source_message])
    outcome = server._tool_apply_outcome({"source": {"source_revision_id": "raw-revision-1"}}, [source_message])
    display = server._tool_delivery_display_ack("delivery-1", "sha256:" + "a" * 64)

    assert state["schema_version"] == "mnemos.cognitive_state_read_model.v1"
    assert decision["schema_version"] == "mnemos.decision_receipt.v1"
    assert outcome["schema_version"] == "mnemos.outcome_receipt.v1"
    assert display["status"] == "recorded"
    assert facade.calls[0] == ("build_cognitive_state", {"scope_type": "project"}, "codex", "sess", "mnemos")
    assert facade.calls[1][0:3] == ("record_decision", "raw-revision-1", "codex")
    assert facade.calls[2][0:3] == ("apply_outcome", "raw-revision-1", "codex")
    assert facade.calls[3] == ("record_delivery_display", "delivery-1", "sha256:" + "a" * 64, "codex")


def test_persona_evidence_tool_binds_exact_source_catalog_to_facade(tmp_path):
    facade = FakeFacade()
    server = _authorized_server(facade, tmp_path)
    content = "每次修复后都要先运行相关测试，再记录深审证据。"
    source_message = {
        "role": "user",
        "content": content,
        "source_span": {
            "revision_id": "raw-revision-persona-evidence",
            "role": "user",
            "span_start": 0,
            "span_end": len(content),
            "content_hash": "sha256:" + hashlib.sha256(content.encode()).hexdigest(),
        },
    }
    catalog = server._cognitive_source_authority_catalog(
        {"source": {"source_revision_id": "raw-revision-persona-evidence"}},
        [source_message],
    )
    request = {
        "source": {"source_revision_id": "raw-revision-persona-evidence"},
        "source_authority_id": catalog.entries[0].source_authority_id,
        "signal_type": "explicit_preference",
        "dimension": "interaction_contract",
        "quote": content,
        "session_id": "sess",
        "project": "mnemos",
    }

    result = server._tool_persona_record_explicit_evidence(request, [source_message])

    assert result == {"success": True, "status": "recorded"}
    assert facade.calls == [
        (
            "record_explicit_profile_evidence",
            catalog.entries[0].source_authority_id,
            "codex",
            "sess",
            "mnemos",
            catalog.catalog_hash,
        )
    ]


def test_all_eight_host_principals_reach_the_same_cognitive_contract(tmp_path):
    from core.agent_kit.protocol import TARGET_AGENT_NAMES

    for agent in TARGET_AGENT_NAMES:
        facade = FakeFacade()
        store = AgentAuthorizationStore(tmp_path / f"{agent}.db")
        credential = store.issue_mcp_capability(
            agent=agent,
            host_kind=agent,
            capabilities=set(MCP_TOOL_POLICIES.values()),
            allowed_projects={"mnemos"},
        )
        server = MCPServer(
            facade=facade,
            launch_credential=credential,
            authorization_store=store,
        )

        state = server._tool_build_cognitive_state({}, "", "mnemos")
        display = server._tool_delivery_display_ack("delivery-1", "sha256:" + "a" * 64)

        assert state["success"] is True, agent
        assert display["success"] is True, agent
        assert facade.calls == [
            ("build_cognitive_state", {}, agent, "", "mnemos"),
            ("record_delivery_display", "delivery-1", "sha256:" + "a" * 64, agent),
        ], agent


def test_cognitive_decision_contract_rejects_detached_source_messages(tmp_path):
    facade = FakeFacade()
    server = _authorized_server(facade, tmp_path)

    result = server._tool_record_decision(
        {"source": {"source_revision_id": "raw-revision-1"}},
        [{"role": "user", "content": "detached source"}],
    )

    assert result == {
        "success": False,
        "schema_version": "mnemos.cognitive_operation_failure.v1",
        "status": "rejected",
        "error_code": "source_authority_invalid",
        "message": "cognitive contract requires exact role-local Raw source spans",
    }


def test_mcp_server_uses_injected_facade_for_observation_tools(monkeypatch, tmp_path):
    def fail_observation_engine(*args, **kwargs):
        raise AssertionError("observation tools should use the injected facade")

    monkeypatch.setattr(
        "core.cognitive.observation_engine.ObservationEngine.__init__",
        fail_observation_engine,
    )
    facade = FakeFacade()
    server = _authorized_server(facade, tmp_path)

    run = server._tool_observation_run(full=True, since="2026-06-24T00:00:00")
    search = server._tool_observation_search(
        dimension="theme",
        source_type="conversation",
        limit=5,
    )

    assert run["observations"] == 1
    assert search["observations"][0]["id"] == "obs-1"
    assert facade.calls == [
        ("observation_run", True, "2026-06-24T00:00:00"),
        (
            "observation_search",
            "theme",
            "conversation",
            5,
            "codex",
            "",
            "",
            "observation_read",
        ),
    ]


def test_mcp_server_uses_injected_facade_for_reflection_tools(monkeypatch, tmp_path):
    def fail_reflection_engine(*args, **kwargs):
        raise AssertionError("reflection tools should use the injected facade")

    monkeypatch.setattr(
        "core.reflection.reflection_engine.ReflectionEngine.__init__",
        fail_reflection_engine,
    )
    facade = FakeFacade()
    server = _authorized_server(facade, tmp_path)

    on_input = server._tool_reflect_on_input(
        "start project", auto_llm=False, session_id="sess", project="mnemos"
    )
    manual = server._tool_reflect_manually(
        "manual query", auto_llm=True, session_id="sess", project="mnemos"
    )
    feedback = server._tool_reflection_feedback(
        "reflection-1",
        "accepted",
        comment="good",
        session_id="sess",
        project="mnemos",
    )
    pending = server._tool_reflection_pending(
        hours_since=12,
        limit=3,
        session_id="sess",
        project="mnemos",
    )

    assert on_input["insight_summary"] == "fake insight"
    assert manual["insight_summary"] == "manual insight"
    assert feedback["reflection_id"] == "reflection-1"
    assert pending["pending"][0]["id"] == "reflection-1"
    assert facade.calls == [
        ("reflect_on_input", "start project", False, "codex", "sess", "mnemos"),
        ("reflect_manually", "manual query", True, "codex", "sess", "mnemos"),
        ("reflection_feedback", "reflection-1", "accepted", "good", "codex", "sess", "mnemos"),
        ("reflection_pending", 12, 3, "codex", "sess", "mnemos"),
    ]


def test_mcp_server_uses_injected_facade_for_memory_tools(tmp_path):
    facade = FakeFacade()
    server = _authorized_server(facade, tmp_path)

    inferred = server._infer_type_from_path("concepts/agora.md")
    slug = server._scope_slug("Hello World")
    path = server._scope_page_path("project", "Title", scope_name="Mnemos")
    project = server._tool_memory_write_project(
        "Project Note",
        "content",
        project="mnemos",
        frontmatter={"tags": ["existing"]},
    )
    framework = server._tool_memory_write_framework(
        "Framework Note",
        "content",
        framework="testing",
    )
    global_result = server._tool_memory_write_global("Global Note", "content")
    search = server._tool_memory_search(
        "query",
        scope="project",
        limit=2,
        session_id="sess",
        project="mnemos",
    )

    assert inferred == "fake-type"
    assert slug == "fake-slug"
    assert path == "fake/path.md"
    assert project["scope"] == "project"
    assert framework["scope"] == "framework"
    assert global_result["scope"] == "global"
    assert search["results"][0]["title"] == "Memory"
    assert facade.calls == [
        ("infer_type_from_path", "concepts/agora.md"),
        ("scope_slug", "Hello World"),
        ("scope_page_path", "project", "Title", "", "Mnemos"),
        (
            "memory_write_project",
            "Project Note",
            "content",
            "mnemos",
            "",
            {"tags": ["existing"]},
        ),
        ("memory_write_framework", "Framework Note", "content", "testing", "", None),
        ("memory_write_global", "Global Note", "content", "", None),
        (
            "memory_search",
            "query",
            "project",
            2,
            "codex",
            "sess",
            "mnemos",
        ),
    ]
