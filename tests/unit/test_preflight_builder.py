"""Preflight builder 单元测试。

覆盖 Guard checklist 命中回写等生产路径边界情况。
"""

import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from integrations.preflight_builder import (
    _mark_guard_checklist_usage,
    _persona_token_estimate,
    build_lightweight_preflight,
    build_kia_section,
    build_l1_section,
    build_observation_section,
    build_persona_section,
    build_predictive_push_section,
    build_wiki_section,
)


class _FakeChecklistItem:
    def __init__(self, text: str):
        self.item = text


class _FakeAlert:
    def __init__(self, text: str):
        self.checklist_item = _FakeChecklistItem(text)


class _FakeSession:
    def __init__(self, checklist_texts, triggered, silent):
        self.task_type = "coding"
        self.subtype = "debug"
        self.checklist = [_FakeChecklistItem(t) for t in checklist_texts]
        self.triggered_alerts = [_FakeAlert(t) for t in triggered]
        self.silent_records = silent


class _FakeGuard:
    def __init__(self, checklist_texts, triggered, silent):
        self.session = _FakeSession(checklist_texts, triggered, silent)


@pytest.fixture
def fake_injector(monkeypatch, tmp_path):
    """Mock PreFlightInjector so _mark_guard_checklist_usage can be unit-tested."""
    calls = []

    class FakeInjector:
        def __init__(self):
            pass

        def _find_latest_version(self, task_type, subtype):
            return tmp_path / "debug-v1.md"

        def _parse_retrospective(self, path):
            # Source checklist order is intentionally different from session order.
            return {
                "checklist": [
                    {"item": "检查并发"},
                    {"item": "检查边界"},
                ]
            }, ""

        def mark_checklist_used(self, task_type, subtype, item_index, used=True):
            calls.append((task_type, subtype, item_index, used))

    monkeypatch.setattr("core.kia.prophasis.PreFlightInjector", FakeInjector)
    return calls


def test_mark_guard_checklist_usage_uses_source_file_index(fake_injector):
    """命中统计应使用源复盘文件中的 checklist 索引，而不是 session 中的重排索引。"""
    # session order: ["检查边界", "检查并发"]；source order: ["检查并发", "检查边界"]
    guard = _FakeGuard(
        checklist_texts=["检查边界", "检查并发"],
        triggered=["检查并发"],
        silent=[],
    )
    _mark_guard_checklist_usage(guard)

    assert len(fake_injector) == 1
    # "检查并发" is at source index 0, not session index 1
    assert fake_injector[0] == ("coding", "debug", 0, True)


def test_mark_guard_checklist_usage_handles_silent_records(fake_injector):
    """静默记录也应按源文件索引回写命中。"""
    guard = _FakeGuard(
        checklist_texts=["检查边界", "检查并发"],
        triggered=[],
        silent=[{"item": "检查边界"}],
    )
    _mark_guard_checklist_usage(guard)

    assert len(fake_injector) == 1
    assert fake_injector[0] == ("coding", "debug", 1, True)


def test_mark_guard_checklist_usage_ignores_unknown_items(fake_injector):
    """Guard 中不存在于源 checklist 的条目应被忽略，不抛异常。"""
    guard = _FakeGuard(
        checklist_texts=["检查边界", "检查并发"],
        triggered=["不存在的条目"],
        silent=[],
    )
    _mark_guard_checklist_usage(guard)

    assert fake_injector == []


def test_mark_guard_checklist_usage_no_latest_file(fake_injector, monkeypatch):
    """找不到源复盘文件时应静默跳过，不影响主流程。"""

    class NoFileInjector:
        def __init__(self):
            pass

        def _find_latest_version(self, task_type, subtype):
            return None

        def mark_checklist_used(self, task_type, subtype, item_index, used=True):
            fake_injector.append((task_type, subtype, item_index, used))

    monkeypatch.setattr("core.kia.prophasis.PreFlightInjector", NoFileInjector)

    guard = _FakeGuard(
        checklist_texts=["检查边界", "检查并发"],
        triggered=["检查并发"],
        silent=[],
    )
    _mark_guard_checklist_usage(guard)

    assert fake_injector == []


class _PersonaCfg:
    def __init__(self, *, enabled=True, strategy_enabled=True, token_limit=120):
        self.enabled = enabled
        self.strategy_enabled = strategy_enabled
        self.token_limit = token_limit

    def get(self, key, default=None):
        values = {
            "persona.enabled": self.enabled,
            "persona.strategy_injection_enabled": self.strategy_enabled,
            "persona.strategy_token_limit": self.token_limit,
        }
        return values.get(key, default)


class _PersonaStore:
    def __init__(self, profile):
        self.profile = profile

    def get_latest_persona_version(self):
        return self.profile


def _patch_persona(monkeypatch, cfg):
    profile = {
        "energy_profile": {"startup_difficulty": 0.9},
        "cognitive_profile": {"abstraction": 0.9, "system_view": 0.9},
        "value_profile": {"correctness_vs_efficiency": 0.9},
        "blindspot_profile": {"option_gap": 0.9},
    }
    monkeypatch.setattr("integrations.preflight_builder.get_config", lambda: cfg)
    monkeypatch.setattr("core.persona.delphi.get_behavior_prompt", lambda agent: "BASE PERSONA")
    monkeypatch.setattr(
        "core.persona.psyche.get_signal_store",
        lambda: _PersonaStore(profile),
    )
    monkeypatch.setattr(
        "integrations.preflight_builder._load_contextual_persona_profiles",
        lambda _principal, _narrowing: (
            {
                "startup_difficulty": 0.9,
                "abstraction": 0.9,
                "system_view": 0.9,
                "correctness_vs_efficiency": 0.9,
            },
            {"option_gap": 0.9},
        ),
    )
    monkeypatch.setattr(
        "integrations.preflight_builder.resolve_preflight_principal",
        lambda agent: PrincipalEnvelope(
            principal_id=f"mcp:{agent}:persona-test",
            agent=agent,
            host_kind=agent,
            capability_id="persona-test",
            capabilities=frozenset({"memory_read"}),
        ),
    )


def _home_path(relative: str) -> str:
    return "/" + f"Users/me/{relative}"


def test_build_persona_section_disables_contextual_strategy(monkeypatch):
    _patch_persona(monkeypatch, _PersonaCfg(strategy_enabled=False))

    result = build_persona_section("codex", working_dir=_home_path("Projects/client"))

    assert result == "BASE PERSONA"
    assert "Contextual Persona Strategy" not in result


def test_build_persona_section_varies_by_working_context(monkeypatch):
    _patch_persona(monkeypatch, _PersonaCfg(token_limit=160))

    work = build_persona_section("codex", working_dir=_home_path("Projects/client/src"))
    personal = build_persona_section(
        "codex",
        working_dir=_home_path("personal/side-project"),
        session_tags=["personal"],
    )

    assert "BASE PERSONA" in work
    assert "Contextual Persona Strategy" in work
    assert "- scope: work" in work
    assert "工作上下文" in work
    assert "- scope: personal" in personal
    assert "个人上下文" in personal
    assert work != personal


def test_build_persona_section_respects_strategy_token_limit(monkeypatch):
    limit = 28
    _patch_persona(monkeypatch, _PersonaCfg(token_limit=limit))

    result = build_persona_section("codex", working_dir=_home_path("study/course"))
    contextual = "[Contextual" + result.split("[Contextual", 1)[1]

    assert _persona_token_estimate(contextual) <= limit
    assert "- scope: study" in contextual


def test_observation_and_push_require_principal_before_constructing_readers(monkeypatch):
    monkeypatch.setattr(
        "integrations.preflight_builder.resolve_preflight_principal",
        lambda _agent: None,
    )

    def forbidden_index(*_args, **_kwargs):
        raise AssertionError("unauthenticated preflight must not read Observation")

    def forbidden_push(*_args, **_kwargs):
        raise AssertionError("unauthenticated preflight must not read push candidates")

    monkeypatch.setattr("core.cognitive.observation_store.ObservationIndex", forbidden_index)
    monkeypatch.setattr("core.kia.teiresias.PredictivePushEngine", forbidden_push)

    assert build_observation_section(agent="codex") == ""
    assert build_predictive_push_section("how do I fix this?", agent="codex") == ""


def test_lightweight_preflight_without_principal_returns_only_public_tooling(monkeypatch):
    monkeypatch.setattr(
        "integrations.preflight_builder.resolve_preflight_principal",
        lambda _agent: None,
    )

    result = build_lightweight_preflight("codex", "/tmp/project", "continue repair")

    assert "## Active Tooling" in result
    assert "## User-Visible Behavior" in result
    assert "check_pending_recaps" in result
    assert "KIA Checklist" not in result
    assert "Wiki" not in result


# ---------------------------------------------------------------------------
# Wiki section
# ---------------------------------------------------------------------------


class TestBuildWikiSection:
    """Preflight Wiki rendering must use the principal-bound facade seam."""

    @pytest.fixture
    def fake_facade(self, monkeypatch):
        class FakeFacade:
            search_results: List[Dict] = []
            bodies: Dict[str, str] = {}
            search_calls: List[tuple] = []
            read_calls: List[tuple] = []

            def __init__(self, _logger=None):
                pass

            def wiki_search(self, query, limit=5, *, principal, narrowing):
                self.search_calls.append((query, limit, principal, narrowing))
                return self.search_results[:limit], {"authorized": len(self.search_results)}

            def wiki_read(self, page_id, *, principal, narrowing):
                self.read_calls.append((page_id, principal, narrowing))
                content = self.bodies.get(page_id, "")
                return {"success": bool(content), "content": content, "path": page_id}

        monkeypatch.setattr("core.application.facade.DefaultMnemosServiceFacade", FakeFacade)
        return FakeFacade

    @staticmethod
    def _principal() -> PrincipalEnvelope:
        return PrincipalEnvelope(
            principal_id="mcp:codex:preflight-test",
            agent="codex",
            host_kind="codex",
            capability_id="preflight-test",
            capabilities=frozenset({"memory_read"}),
        )

    def _make_page(self, title, heat_level, body, score=1.0):
        return {
            "page_id": f"03-Tech/{title.lower().replace(' ', '-')}",
            "title": title,
            "heat_level": heat_level,
            "heat_score": score,
            "relevance_score": score,
        }

    @staticmethod
    def _markdown(body: str) -> str:
        return f"---\ntitle: test\n---\n{body}"

    def test_deep_wiki_xml_wrapper_and_heat_order(self, fake_facade):
        fake_facade.search_results = [
            self._make_page("Hot Page", "hot", "hot body"),
            self._make_page("Warm Page", "warm", "warm summary"),
            self._make_page("Cold Page", "cold", "cold note"),
        ]
        fake_facade.bodies = {
            page["page_id"]: self._markdown(body)
            for page, body in zip(
                fake_facade.search_results,
                ("hot body", "warm summary", "cold note"),
            )
        }
        result = build_wiki_section(
            "python async",
            mode="deep",
            agent="codex",
            principal=self._principal(),
            narrowing=AccessNarrowing(),
        )
        assert fake_facade.search_calls
        assert result.startswith('<wiki-context source="knowledge-query">')
        assert result.endswith("</wiki-context>")
        hot_pos = result.find("### [hot]")
        warm_pos = result.find("### [warm]")
        cold_pos = result.find("### [cold]")
        assert 0 < hot_pos < warm_pos < cold_pos

    def test_deep_wiki_logs_pages_through_knowledge_usage_helper(
        self, fake_facade, monkeypatch
    ):
        fake_facade.search_results = [
            self._make_page("Hot Page", "hot", "hot body"),
            self._make_page("Warm Page", "warm", "warm summary"),
        ]
        fake_facade.bodies = {
            "03-Tech/hot-page": self._markdown("hot body"),
            "03-Tech/warm-page": self._markdown("warm summary"),
        }
        calls = []

        def fake_import(module_path, symbol_name):
            assert module_path == "core.kia.ariadne"
            assert symbol_name == "log_knowledge_usage"

            def fake_log(page_path, event_type="query", context=""):
                calls.append((page_path, event_type, context))
                return True

            return fake_log

        monkeypatch.setattr("integrations.preflight_builder._import_optional_class", fake_import)

        build_wiki_section(
            "python async",
            mode="deep",
            agent="codex",
            principal=self._principal(),
        )

        assert calls == [
            ("03-Tech/hot-page", "query", "python async"),
            ("03-Tech/warm-page", "query", "python async"),
        ]

    def test_deep_wiki_body_is_fetched_after_authorized_search(self, fake_facade):
        page = self._make_page("Precedence", "hot", "full content")
        fake_facade.search_results = [page]
        fake_facade.bodies = {page["page_id"]: self._markdown("full content")}
        result = build_wiki_section(
            "q", mode="deep", agent="codex", principal=self._principal()
        )
        assert "full content" in result
        assert [call[0] for call in fake_facade.read_calls] == [page["page_id"]]

    def test_deep_wiki_not_found(self, fake_facade):
        assert build_wiki_section(
            "missing", mode="deep", agent="codex", principal=self._principal()
        ) == "\n（Wiki中未找到相关知识）\n"

    def test_light_wiki_outputs_snippets(self, fake_facade):
        fake_facade.search_results = [
            {"page_id": "03-Tech/a", "title": "Alpha", "relevance_score": 8.5},
            {"page_id": "03-Tech/b", "title": "Beta", "score": 3.0},
        ]
        fake_facade.bodies = {
            "03-Tech/a": self._markdown("alpha summary long enough"),
            "03-Tech/b": self._markdown("beta content line\nsecond line"),
        }
        result = build_wiki_section(
            "query", mode="light", agent="codex", principal=self._principal()
        )
        assert result.startswith("## Related Wiki Knowledge")
        assert "Alpha (03-Tech/a, score=8.50):" in result
        assert "alpha summary" in result
        assert "Beta (03-Tech/b, score=3.00):" in result
        # newlines in snippet are collapsed to spaces and trimmed
        assert "beta content line second line" in result

    def test_light_wiki_empty_returns_empty(self, fake_facade):
        assert build_wiki_section(
            "query", mode="light", agent="codex", principal=self._principal()
        ) == ""

    def test_missing_principal_never_constructs_facade(self, monkeypatch):
        def forbidden_facade(*_args, **_kwargs):
            raise AssertionError("unauthenticated preflight must not read Wiki")

        monkeypatch.setattr(
            "core.application.facade.DefaultMnemosServiceFacade", forbidden_facade
        )
        assert build_wiki_section("query", mode="light", agent="codex") == ""

    def test_empty_query_returns_empty(self, fake_facade):
        assert build_wiki_section("", mode="deep", principal=self._principal()) == ""
        assert build_wiki_section("", mode="light", principal=self._principal()) == ""


# ---------------------------------------------------------------------------
# L1 section
# ---------------------------------------------------------------------------


class TestBuildL1Section:
    """Phase 4 Wave 6: build_l1_section 行为刻画测试。"""

    @staticmethod
    def _principal(
        agent: str = "claude", *, allowed_source_agents: frozenset[str] = frozenset()
    ) -> PrincipalEnvelope:
        return PrincipalEnvelope(
            principal_id=f"mcp:{agent}:preflight-test",
            agent=agent,
            host_kind=agent,
            capability_id="preflight-test",
            capabilities=frozenset({"memory_read"}),
            allowed_source_agents=allowed_source_agents,
        )

    @pytest.fixture
    def l1_setup(self, monkeypatch):
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(days=1)).isoformat()
        old = (now - timedelta(days=10)).isoformat()

        class Mem:
            def __init__(self, content, created_at, tags):
                self.content = content
                self.created_at = created_at
                self.tags = tags

        class FakeFacade:
            def __init__(self, _logger=None):
                self.my: List[Mem] = []
                self.cross: Dict[str, List[Mem]] = {}
                self.related: List[Mem] = []

            @staticmethod
            def _serialize(memory: Mem, default_source: str = "") -> Dict[str, str]:
                values = {
                    tag.split("=", 1)[0]: tag.split("=", 1)[1]
                    for tag in memory.tags
                    if "=" in tag
                }
                return {
                    "snippet": memory.content,
                    "created_at": memory.created_at,
                    "source_agent": values.get("source", default_source),
                    "session_id": values.get("session", "session-test"),
                }

            def session_search(
                self,
                query="",
                source=None,
                days=None,
                limit=10,
                *,
                principal,
                narrowing,
            ):
                if query:
                    memories = self.related
                elif source == principal.agent:
                    memories = self.my
                else:
                    memories = self.cross.get(source or "", [])
                return {
                    "success": True,
                    "results": [
                        self._serialize(memory, source or "")
                        for memory in memories[:limit]
                    ],
                }

        facade = FakeFacade()
        fake_config = type(
            "C",
            (),
            {"cross_agent_share": False, "data_dir": Path("/tmp")},
        )()
        monkeypatch.setattr(
            "core.application.facade.DefaultMnemosServiceFacade", lambda _logger=None: facade
        )
        monkeypatch.setattr("integrations.preflight_builder.get_config", lambda: fake_config)
        return facade, Mem, recent, old

    def test_my_memories_within_7_days_and_session_tag(self, l1_setup):
        facade, Mem, recent, _ = l1_setup
        facade.my = [Mem("hello world", recent, ["source=claude", "session=abc123"])]
        result = build_l1_section(
            _home_path("project"), "claude", principal=self._principal()
        )
        assert "最近会话上下文" in result
        assert "Session `abc123`" in result
        assert "hello world" in result

    def test_old_memories_excluded_by_7_day_cutoff(self, l1_setup):
        facade, Mem, _, old = l1_setup
        facade.my = [Mem("old memory", old, ["source=claude", "session=x"])]
        result = build_l1_section(
            _home_path("project"), "claude", principal=self._principal()
        )
        assert result == "\n（暂无相关上下文）\n"

    def test_authorize_cross_explicit(self, l1_setup):
        facade, Mem, recent, _ = l1_setup
        facade.cross["hermes"] = [Mem("hermes memory", recent, ["source=hermes"])]
        result = build_l1_section(
            _home_path("project"),
            "claude",
            authorize_cross=["hermes"],
            principal=self._principal(allowed_source_agents=frozenset({"hermes"})),
        )
        assert "hermes 框架共享记忆" in result
        assert "hermes memory" in result

    def test_authorize_cross_none_uses_config_true(self, l1_setup, monkeypatch):
        facade, Mem, recent, _ = l1_setup
        fake_config = type(
            "C",
            (),
            {"cross_agent_share": True, "data_dir": Path("/tmp")},
        )()
        monkeypatch.setattr("integrations.preflight_builder.get_config", lambda: fake_config)
        facade.cross["hermes"] = [Mem("shared memory", recent, ["source=hermes"])]
        result = build_l1_section(
            _home_path("project"),
            "claude",
            principal=self._principal(allowed_source_agents=frozenset({"hermes"})),
        )
        assert "hermes 框架共享记忆" in result
        assert "shared memory" in result

    def test_authorize_cross_none_uses_config_false(self, l1_setup):
        facade, Mem, recent, _ = l1_setup
        facade.cross["hermes"] = [Mem("shared memory", recent, ["source=hermes"])]
        result = build_l1_section(
            _home_path("project"), "claude", principal=self._principal()
        )
        assert "框架共享记忆" not in result
        assert result == "\n（暂无相关上下文）\n"

    def test_related_memories_with_source_tag(self, l1_setup):
        facade, Mem, recent, _ = l1_setup
        facade.related = [Mem("related memory", recent, ["source=opencode", "session=y"])]
        result = build_l1_section(
            _home_path("project"), "claude", principal=self._principal()
        )
        assert "相关记忆" in result
        assert "[opencode]" in result
        assert "related memory" in result

    def test_empty_fallback(self, l1_setup):
        result = build_l1_section(
            _home_path("project"), "claude", principal=self._principal()
        )
        assert result == "\n（暂无相关上下文）\n"

    def test_l1_requires_principal_before_constructing_facade(self, monkeypatch):
        def forbidden_facade(_logger=None):
            raise AssertionError("unauthenticated preflight must not read canonical Raw")

        monkeypatch.setattr(
            "core.application.facade.DefaultMnemosServiceFacade", forbidden_facade
        )
        assert build_l1_section(_home_path("project"), "claude") == ""


# ---------------------------------------------------------------------------
# KIA section
# ---------------------------------------------------------------------------


class TestBuildKiaSection:
    """Phase 4 Wave 6: build_kia_section 行为刻画测试。"""

    @pytest.fixture
    def kia_mocks(self, monkeypatch):
        class FakeTimeWindow:
            def __init__(self, **kwargs):
                self.window = kwargs.get("window")
                self.days_until = kwargs.get("days_until", 0)
                self.due_date = kwargs.get("due_date")
                self.is_periodic = False
                self.period = None

        class FakeTimeWindowType:
            IMMEDIATE = "immediate"
            MEDIUM = "medium"

        class FakeTimeParser:
            def parse(self, text, task_type=None):
                return FakeTimeWindow(window=FakeTimeWindowType.IMMEDIATE, days_until=0)

            def should_load_now(self, tw, task_type=None):
                return True

            def get_reminder_days_before(self, tw):
                return 1

        class FakeChecklistItem:
            def __init__(self, item, severity="medium"):
                self.item = item
                self.severity = severity

        class FakeKnowledge:
            def __init__(self, checklist=None):
                self.task_type = "coding"
                self.version = "v1"
                self.is_compact = False
                self.total_items = 0
                self.lessons_summary = ""
                self.checklist = checklist or []

        class FakeAlert:
            def __init__(self, level, item, suggestion=""):
                self.level = level
                self.checklist_item = item
                self.suggestion = suggestion

        class FakeGuard:
            alert = None
            silent: List[Any] = []

            def __init__(self, knowledge):
                self.knowledge = knowledge
                self.session = type(
                    "S",
                    (),
                    {
                        "task_type": "coding",
                        "subtype": "debug",
                        "checklist": knowledge.checklist,
                        "triggered_alerts": [self.alert] if self.alert else [],
                        "silent_records": self.silent,
                    },
                )()

            def check(self, text, empty):
                return self.alert

            def check_silent(self, text, empty):
                return self.silent

        class FakeInjector:
            knowledge = None

            def __init__(self):
                pass

            def inject(self, *args, **kwargs):
                return self.knowledge

            def format_for_context(self, knowledge):
                return f"FORMATTED:{knowledge.version}"

        class FakeClassifier:
            confidence = 0.9
            task_type = "coding"
            subtype = "debug"
            suggested_confirmation = "silent"

            def classify(self, messages):
                return self

            def get_task_type_label(self, task_type, subtype):
                return f"{task_type}/{subtype}"

        class FakeScheduler:
            def schedule(self, *args, **kwargs):
                return "task-123"

        class FakeGuardLevel:
            INTERRUPT = type("I", (), {"value": "interrupt"})()
            HINT = type("H", (), {"value": "hint"})()
            SILENT = type("S", (), {"value": "silent"})()

        monkeypatch.setattr("core.kia.kairos.TimeParser", FakeTimeParser)
        monkeypatch.setattr("core.kia.kairos.TimeWindow", FakeTimeWindow)
        monkeypatch.setattr("core.kia.kairos.TimeWindowType", FakeTimeWindowType)
        monkeypatch.setattr("core.kia.prophasis.PreFlightInjector", FakeInjector)
        monkeypatch.setattr("core.kia.dike.TaskClassifier", FakeClassifier)
        monkeypatch.setattr("core.kia.aegis.InProcessGuard", FakeGuard)
        monkeypatch.setattr("core.kia.aegis.GuardLevel", FakeGuardLevel)
        monkeypatch.setattr("core.kia.chronos.KnowledgeScheduler", FakeScheduler)
        monkeypatch.setattr(
            "integrations.preflight_builder._save_guard_state",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            "integrations.preflight_builder._mark_guard_checklist_usage",
            lambda *args, **kwargs: None,
        )

        return {
            "TimeWindow": FakeTimeWindow,
            "TimeWindowType": FakeTimeWindowType,
            "TimeParser": FakeTimeParser,
            "Knowledge": FakeKnowledge,
            "ChecklistItem": FakeChecklistItem,
            "Alert": FakeAlert,
            "Guard": FakeGuard,
            "Injector": FakeInjector,
            "Classifier": FakeClassifier,
            "Scheduler": FakeScheduler,
            "GuardLevel": FakeGuardLevel,
        }

    def test_light_mode_returns_formatted_knowledge(self, kia_mocks):
        kia_mocks["Injector"].knowledge = kia_mocks["Knowledge"](
            checklist=[kia_mocks["ChecklistItem"]("check 1")]
        )
        result = build_kia_section("do something", task_type="coding", mode="light")
        assert "## KIA Checklist" in result
        assert "check 1" in result

    def test_light_mode_no_knowledge_returns_empty(self, kia_mocks):
        kia_mocks["Injector"].knowledge = None
        assert build_kia_section("do something", task_type="coding", mode="light") == ""

    def test_full_mode_low_confidence_returns_empty(self, kia_mocks):
        kia_mocks["Classifier"].confidence = 0.5
        assert build_kia_section("do something", mode="full") == ""

    def test_full_mode_low_confidence_prints_confirmation_request(
        self, kia_mocks, capsys
    ):
        kia_mocks["Classifier"].confidence = 0.5
        kia_mocks["Classifier"].suggested_confirmation = "ask"

        assert build_kia_section("do something", mode="full") == ""

        captured = capsys.readouterr()
        assert "任务分类需要确认" in captured.out
        assert "coding/debug" in captured.out

    def test_full_mode_interrupt_short_circuit(self, kia_mocks):
        item = kia_mocks["ChecklistItem"]("critical check")
        kia_mocks["Injector"].knowledge = kia_mocks["Knowledge"](checklist=[item])
        kia_mocks["Guard"].alert = kia_mocks["Alert"](
            kia_mocks["GuardLevel"].INTERRUPT,
            item,
            suggestion="stop now",
        )
        try:
            result = build_kia_section("do something", mode="full")
            assert "FORMATTED:v1" in result
            assert "[Guard Alert]" in result
            assert "critical check" in result
            assert "stop now" in result
        finally:
            kia_mocks["Guard"].alert = None

    def test_full_mode_static_guard_rules(self, kia_mocks):
        item = kia_mocks["ChecklistItem"]("high severity item", severity="high")
        kia_mocks["Injector"].knowledge = kia_mocks["Knowledge"](checklist=[item])
        result = build_kia_section("do something", mode="full")
        assert "[Guard Rules]" in result
        assert "high severity item" in result
        assert result.startswith("FORMATTED:v1")
        assert result.endswith("\n")

    def test_full_mode_uses_should_load_knowledge_helper(self, kia_mocks, monkeypatch):
        calls = []

        def fake_should_load_knowledge(content, task_type=None):
            calls.append((content, task_type))
            return True, kia_mocks["TimeWindow"](
                window=kia_mocks["TimeWindowType"].IMMEDIATE,
                days_until=0,
            )

        monkeypatch.setattr("core.kia.kairos.should_load_knowledge", fake_should_load_knowledge)

        item = kia_mocks["ChecklistItem"]("high severity item", severity="high")
        kia_mocks["Injector"].knowledge = kia_mocks["Knowledge"](checklist=[item])
        result = build_kia_section("do something", mode="full")

        assert calls == [("do something", "coding")]
        assert result.startswith("FORMATTED:v1")

    def test_full_mode_schedules_deferred_task_with_reminder_lead_time(
        self, kia_mocks, monkeypatch, capsys
    ):
        def fake_should_load_knowledge(content, task_type=None):
            return False, kia_mocks["TimeWindow"](
                window=kia_mocks["TimeWindowType"].MEDIUM,
                days_until=14,
                due_date=datetime(2026, 6, 10, 9, 0, tzinfo=timezone.utc),
            )

        monkeypatch.setattr("core.kia.kairos.should_load_knowledge", fake_should_load_knowledge)

        result = build_kia_section("do it later", mode="full")

        assert result == ""
        captured = capsys.readouterr()
        assert "任务已记入调度器: task-123" in captured.out
        assert "提前 1 天提醒" in captured.out

    def test_full_mode_no_static_rules_for_lower_severity(self, kia_mocks):
        item = kia_mocks["ChecklistItem"]("medium severity item", severity="medium")
        kia_mocks["Injector"].knowledge = kia_mocks["Knowledge"](checklist=[item])
        result = build_kia_section("do something", mode="full")
        assert "[Guard Rules]" not in result
        assert result == "FORMATTED:v1\n"

    def test_full_mode_silent_records_no_interrupt(self, kia_mocks):
        item = kia_mocks["ChecklistItem"]("silent item")
        kia_mocks["Injector"].knowledge = kia_mocks["Knowledge"](checklist=[item])
        kia_mocks["Guard"].silent = [{"item": "silent record"}]
        try:
            result = build_kia_section("do something", mode="full")
            assert result == "FORMATTED:v1\n"
            assert "[Guard Alert]" not in result
        finally:
            kia_mocks["Guard"].silent = []

    def test_empty_query_returns_empty(self, kia_mocks):
        assert build_kia_section("", mode="light") == ""
        assert build_kia_section("", mode="full") == ""
