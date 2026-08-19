from core.persona.contextual_strategy import (
    BehaviorSkillReporter,
    ContextualPersonaBuffer,
    PersonaStrategyBuilder,
    detect_persona_context,
)


def _home_path(relative: str) -> str:
    return "/" + f"Users/me/{relative}"


def test_contextual_persona_keeps_working_dirs_separate():
    buffer = ContextualPersonaBuffer()

    work = buffer.add_signal({"working_dir": _home_path("work/client/src"), "value": "work"})
    personal = buffer.add_signal(
        {"working_dir": _home_path("personal/side-project"), "value": "personal"}
    )

    assert work.scope == "work"
    assert personal.scope == "personal"
    assert buffer.get_signals("work")[0]["value"] == "work"
    assert buffer.get_signals("personal")[0]["value"] == "personal"


def test_detect_persona_context_uses_session_tags():
    context = detect_persona_context(working_dir="/tmp/unknown", session_tags=["reading"])

    assert context.scope == "study"


def test_persona_strategy_builder_respects_token_limit():
    builder = PersonaStrategyBuilder(max_strategies=5, token_limit=18)
    result = builder.build(
        preference_profile={
            "startup_difficulty": 0.9,
            "abstraction": 0.9,
            "skepticism": 0.9,
            "system_view": 0.9,
            "correctness_vs_efficiency": 0.9,
        },
        blindspot_profile={"option_gap": 0.9},
    )

    assert result["enabled"] is True
    assert result["tokens_estimate"] <= 18
    assert len(result["strategies"]) >= 1


def test_behavior_skill_reporter_is_report_only():
    actions = [
        {"action": "search"},
        {"action": "read"},
        {"action": "search"},
        {"action": "read"},
        {"action": "search"},
        {"action": "read"},
    ]

    report = BehaviorSkillReporter(min_support=0.3).suggest(actions, current_skills=[])

    assert report["report_only"] is True
    assert report["suggestions"]
    assert report["suggestions"][0]["action"] == "create"
