from datetime import datetime, timedelta, timezone

from core.app.application_signal_detectors import (
    AvoidanceSignalDetector,
    CrossAgentDivergenceDetector,
    FreshnessSignalChecker,
)


def test_avoidance_detector_flags_repeated_unclicked_topic():
    history = [
        {
            "query": "Rust架构优化 方法",
            "results_shown": ["Rust架构优化", "Python migration"],
            "clicked_results": [],
        },
        {
            "query": "Rust架构优化 工具",
            "results_shown": ["Rust架构优化", "CLI"],
            "clicked_results": [],
        },
        {
            "query": "Rust架构优化 方案",
            "results_shown": ["Rust架构优化", "Storage"],
            "clicked_results": [],
        },
        {
            "query": "Python testing",
            "results_shown": ["Python testing"],
            "clicked_results": ["Python testing"],
        },
    ]

    signals = AvoidanceSignalDetector(min_occurrences=3, significance_threshold=0.5).detect(history)

    assert signals
    first = signals[0].as_dict()
    assert first["kind"] == "avoidance"
    assert first["cooldown_days"] == 14
    assert any("topic_click_rate" in item for item in first["evidence"])


def test_cross_agent_divergence_detector_flags_low_similarity_outputs():
    outputs = [
        {
            "agent_id": "codex",
            "topic": "deploy",
            "output": "Use Obsidian raw vault deployment with configurable API keys.",
            "confidence": 0.9,
        },
        {
            "agent_id": "hermes",
            "topic": "deploy",
            "output": "Use an external SQL service and fixed local credentials.",
            "confidence": 0.3,
        },
    ]

    signals = CrossAgentDivergenceDetector(min_score=0.2).detect(outputs)

    assert len(signals) == 1
    signal = signals[0].as_dict()
    assert signal["kind"] == "cross_agent_divergence"
    assert signal["topic"] == "deploy"
    assert any("agents=codex,hermes" in item for item in signal["evidence"])


def test_freshness_checker_flags_old_or_missing_timestamps():
    old_date = (datetime.now(timezone.utc) - timedelta(days=180)).isoformat()
    checker = FreshnessSignalChecker(half_life_days=30, stale_threshold=0.25)

    stale = checker.check({"title": "Obsidian plugin API", "last_modified": old_date})
    missing = checker.check({"title": "No date"})
    fresh = checker.check(
        {
            "title": "Fresh",
            "last_modified": datetime.now(timezone.utc).isoformat(),
        }
    )

    assert stale is not None
    assert stale.kind == "freshness"
    assert stale.cooldown_days == 30
    assert missing is not None
    assert missing.evidence == ["missing_last_modified"]
    assert fresh is None
