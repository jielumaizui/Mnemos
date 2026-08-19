from core.kia.assertion_extractor import KnowledgeForm, extract_assertions
from core.agent_kit.authorization import AgentAuthorizationStore
from core.kia.kairos import TimeWindow, TimeWindowType
from core.kia.prophasis import BEHAVIOR_CONSTRAINTS, PreFlightInjector
from integrations.agora import MCPServer


def _authorized_server(tmp_path):
    store = AgentAuthorizationStore(tmp_path / "agent_authorization.db")
    credential = store.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities={"memory_read"},
    )
    return MCPServer(
        launch_credential=credential,
        authorization_store=store,
    )


def test_preflight_always_injects_behavior_constraints(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()

    injector = PreFlightInjector(wiki_base=str(wiki))
    knowledge = injector.inject(
        "coding",
        "",
        TimeWindow(window=TimeWindowType.IMMEDIATE, days_until=0),
        "查一下之前的 ci，没通过，需要修复",
    )

    assert knowledge is not None
    assert [item.item for item in knowledge.checklist[: len(BEHAVIOR_CONSTRAINTS)]] == [
        item.item for item in BEHAVIOR_CONSTRAINTS
    ]


def test_preflight_keeps_behavior_constraints_before_compact_truncation(tmp_path):
    wiki = tmp_path / "wiki"
    concepts = wiki / "04-Concepts"
    concepts.mkdir(parents=True)

    for index in range(12):
        (concepts / f"rule-{index}.md").write_text(
            "---\n"
            "type: anti-pattern\n"
            "task_type: coding\n"
            f"name: Rule {index}\n"
            "keywords:\n"
            f"  - keyword-{index}\n"
            "---\n\n"
            f"# Rule {index}\n",
            encoding="utf-8",
        )

    injector = PreFlightInjector(wiki_base=str(wiki))
    knowledge = injector.inject(
        "coding",
        "",
        TimeWindow(window=TimeWindowType.IMMEDIATE, days_until=0),
        "修复 CI",
    )

    assert knowledge is not None
    assert knowledge.is_compact
    assert [item.item for item in knowledge.checklist[: len(BEHAVIOR_CONSTRAINTS)]] == [
        item.item for item in BEHAVIOR_CONSTRAINTS
    ]


def test_mcp_guard_check_preserves_loop_state_across_calls(monkeypatch, tmp_path):
    from core.application.kia import KiaApplicationService

    monkeypatch.setattr(KiaApplicationService, "_active_policy_patches", lambda *args, **kwargs: [])
    server = _authorized_server(tmp_path)

    responses = [
        server._tool_guard_check(
            user_message=f"第 {index} 轮观察",
            ai_response="我需要分析这个问题。",
            task_type="coding",
        )
        for index in range(2)
    ]

    assert responses[-1]["alert"] is True
    assert responses[-1]["level"] == "hint"
    assert responses[-1]["checklist_item"] == "思考循环检测：连续多轮纯分析无行动"
    assert responses[-1]["threshold_source"] == "config"
    assert responses[-1]["threshold_value"] == 2
    assert responses[-1]["current_count"] == 2


def test_mcp_guard_detects_repeated_file_reads_from_context(monkeypatch, tmp_path):
    from core.application.kia import KiaApplicationService

    monkeypatch.setattr(KiaApplicationService, "_active_policy_patches", lambda *args, **kwargs: [])
    server = _authorized_server(tmp_path)
    context = {
        "tool_calls": [
            {
                "name": "ReadFile",
                "input": {"path": "core/kia/aegis.py"},
            }
        ]
    }

    responses = [
        server._tool_guard_check(
            user_message=f"继续定位第 {index} 步",
            ai_response="",
            task_type="coding",
            context=context,
        )
        for index in range(2)
    ]

    assert responses[-1]["alert"] is True
    assert responses[-1]["level"] == "hint"
    assert responses[-1]["checklist_item"] == "思考循环检测：同一文件/工具被重复读取"
    assert responses[-1]["threshold_source"] == "config"
    assert responses[-1]["threshold_value"] == 2
    assert responses[-1]["current_count"] == 2


def test_mcp_guard_schema_exposes_context_for_process_signals():
    guard_tool = next(
        tool for tool in MCPServer()._list_tools()["tools"] if tool["name"] == "guard_check"
    )

    assert "context" in guard_tool["inputSchema"]["properties"]


def test_mcp_guard_fallback_constructs_valid_loaded_knowledge(monkeypatch, tmp_path):
    from core.kia.prophasis import PreFlightInjector

    monkeypatch.setattr(PreFlightInjector, "inject", lambda *args, **kwargs: None)

    result = _authorized_server(tmp_path)._tool_guard_check(
        user_message="准备提交未测试代码",
        ai_response="我会直接提交，未测试。",
        task_type="unknown",
    )

    assert result["success"] is True
    assert result["alert"] is True


def test_ai_behavior_loop_assertion_is_anti_pattern():
    assertions = extract_assertions(
        "陷入循环的根因在于同一文件反复分析达到阈值但没有行动修复。"
        "用户要求修复但 AI 还在分析。",
        source="loop-context",
    )

    assert assertions
    assert assertions[0].form == KnowledgeForm.ANTI_PATTERN
    assert assertions[0].is_negated is True
