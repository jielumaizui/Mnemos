"""Tests for core.reflection.insight_generator."""

from datetime import datetime
import hashlib
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.reflection.experience_matcher import ExperienceMatch
from core.reflection.insight_generator import InsightGenerator
from core.reflection.mirror_engine import MirrorResult
from core.reflection.models import MirrorSnapshot
from core.reflection.time_awareness import TemporalContext
from core.telemetry.provider_request import canonical_chat_input, utf8_token_upper_bound


def _make_mirror(snapshots):
    return MirrorResult(
        snapshots=snapshots,
        dimensions_involved=list({s.dimension for s in snapshots}),
        total_observations_scanned=len(snapshots),
        total_weighted_score=0.0,
        temporal_note="常规时间",
    )


def test_calculate_confidence_empty_snapshots():
    generator = InsightGenerator(use_llm=False)
    mirror = _make_mirror([])
    assert generator._calculate_confidence(mirror) == 0.0


def test_calculate_confidence_with_full_evidence():
    generator = InsightGenerator(use_llm=False)
    snapshots = [
        MirrorSnapshot(
            observation_id=f"obs-{i}",
            dimension="attention",
            value_summary=f"value {i}",
            evidence_summary="evidence",
            confidence=0.9,
            recency_weight=0.9,
        )
        for i in range(5)
    ]
    mirror = _make_mirror(snapshots)
    confidence = generator._calculate_confidence(mirror)
    # count_score = 1.0, recency_score = 0.9, conf_score = 0.9
    # overall = 1.0*0.3 + 0.9*0.4 + 0.9*0.3 = 0.93
    assert confidence == pytest.approx(0.93)


def test_calculate_confidence_with_few_observations():
    generator = InsightGenerator(use_llm=False)
    snapshots = [
        MirrorSnapshot(
            observation_id="obs-1",
            dimension="attention",
            value_summary="value",
            evidence_summary="evidence",
            confidence=0.8,
            recency_weight=0.8,
        ),
    ]
    mirror = _make_mirror(snapshots)
    confidence = generator._calculate_confidence(mirror)
    # count_score = 0.2, recency_score = 0.8, conf_score = 0.8
    # overall = 0.2*0.3 + 0.8*0.4 + 0.8*0.3 = 0.62
    assert confidence == pytest.approx(0.62)


def test_generate_use_llm_false_does_not_call_llm():
    generator = InsightGenerator(use_llm=False)
    snapshots = [
        MirrorSnapshot(
            observation_id="obs-1",
            dimension="attention",
            value_summary="关注编码",
            evidence_summary="典型情境",
            confidence=0.8,
            recency_weight=0.9,
        ),
    ]
    mirror = _make_mirror(snapshots)
    generator._call_llm = MagicMock(return_value=None)

    result = generator.generate(mirror=mirror, user_query="分析最近状态")
    assert result.llm_called is False
    assert result.llm_error == ""
    generator._call_llm.assert_not_called()


def test_generate_returns_result_with_dimensions_and_prompt():
    generator = InsightGenerator(use_llm=False)
    snapshots = [
        MirrorSnapshot(
            observation_id="obs-1",
            dimension="attention",
            value_summary="关注编码",
            evidence_summary="典型情境",
            confidence=0.8,
            recency_weight=0.9,
        ),
    ]
    mirror = _make_mirror(snapshots)
    temporal = TemporalContext(
        now=datetime(2026, 6, 13, 10, 0, 0),
        now_str="2026-06-13 10:00",
        rhythm="normal",
        rhythm_description="常规时间",
    )

    result = generator.generate(
        mirror=mirror,
        temporal=temporal,
        user_query="我要重构项目",
        calibration_hints="多关注 attention",
    )

    assert result.dimensions_involved == ["attention"]
    assert result.confidence == pytest.approx(0.66)
    assert "我要重构项目" in result.prompt_used
    assert "多关注 attention" in result.prompt_used
    assert "Mirror（证据链）" in result.prompt_used
    assert "时间上下文" in result.prompt_used


def test_generate_low_confidence_flag():
    generator = InsightGenerator(use_llm=False)
    snapshots = [
        MirrorSnapshot(
            observation_id="obs-1",
            dimension="attention",
            value_summary="关注编码",
            evidence_summary="典型情境",
            confidence=0.5,
            recency_weight=0.5,
        ),
    ]
    mirror = _make_mirror(snapshots)
    result = generator.generate(mirror=mirror, min_confidence=0.8)
    assert result.confidence < 0.8
    assert "低于校准阈值" in result.calibration_note


def test_generate_no_low_confidence_flag_when_above():
    generator = InsightGenerator(use_llm=False)
    snapshots = [
        MirrorSnapshot(
            observation_id="obs-1",
            dimension="attention",
            value_summary="关注编码",
            evidence_summary="典型情境",
            confidence=0.9,
            recency_weight=0.9,
        ),
    ]
    mirror = _make_mirror(snapshots)
    result = generator.generate(mirror=mirror, min_confidence=0.3)
    assert not hasattr(result, "calibration_note") or not result.calibration_note


def test_build_prompt_includes_experiences():
    generator = InsightGenerator(use_llm=False)
    snapshots = [
        MirrorSnapshot(
            observation_id="obs-1",
            dimension="attention",
            value_summary="关注编码",
            evidence_summary="典型情境",
            confidence=0.8,
            recency_weight=0.9,
        ),
    ]
    mirror = _make_mirror(snapshots)
    experiences = [
        ExperienceMatch(
            source_type="reflection",
            source_id="r-old",
            title="旧 Reflection",
            summary="曾经重构过项目",
            score=0.85,
        ),
    ]
    prompt = generator._build_prompt(
        mirror=mirror,
        temporal=None,
        user_query="重构",
        experiences=experiences,
    )
    assert "历史相似事件" in prompt
    assert "旧 Reflection" in prompt
    assert "0.85" in prompt
    assert "曾经重构过项目" in prompt


def test_build_prompt_without_temporal_or_experiences():
    generator = InsightGenerator(use_llm=False)
    snapshots = [
        MirrorSnapshot(
            observation_id="obs-1",
            dimension="decisions",
            value_summary="决策快",
            evidence_summary="",
            confidence=0.8,
            recency_weight=0.8,
        ),
    ]
    mirror = _make_mirror(snapshots)
    prompt = generator._build_prompt(mirror=mirror, temporal=None, user_query="")
    assert "Mirror（证据链）" in prompt
    assert "分析要求" in prompt
    assert "输出格式" in prompt


def test_generate_parses_llm_response():
    generator = InsightGenerator(use_llm=True)
    snapshots = [
        MirrorSnapshot(
            observation_id="obs-1",
            dimension="attention",
            value_summary="关注编码",
            evidence_summary="典型情境",
            confidence=0.8,
            recency_weight=0.9,
        ),
    ]
    mirror = _make_mirror(snapshots)

    raw_response = (
        "### 一句话摘要\n"
        "你近期在编码任务上保持高度专注。\n"
        "\n"
        "### 关键发现\n"
        "1. **注意力集中**：证据显示你在编码时进入心流状态\n"
        "2. **时间模式**：上午的效率明显高于下午\n"
        "\n"
        "### 不确定性标注\n"
        "样本量较小，结论需谨慎参考。\n"
    )
    generator._call_llm = lambda prompt: raw_response

    result = generator.generate(mirror=mirror, user_query="分析最近状态")
    assert result.summary == "你近期在编码任务上保持高度专注。"
    assert len(result.key_points) == 2
    assert "注意力集中" in result.key_points[0]
    assert "时间模式" in result.key_points[1]
    assert result.llm_called is True
    assert result.llm_error == ""


def test_generate_falls_back_when_llm_unavailable():
    generator = InsightGenerator(use_llm=True)
    snapshots = [
        MirrorSnapshot(
            observation_id="obs-1",
            dimension="attention",
            value_summary="关注编码",
            evidence_summary="典型情境",
            confidence=0.8,
            recency_weight=0.9,
        ),
    ]
    mirror = _make_mirror(snapshots)
    generator._call_llm = lambda prompt: None

    result = generator.generate(mirror=mirror, user_query="分析最近状态")
    assert result.summary == ""
    assert result.key_points == []
    assert "分析最近状态" in result.prompt_used
    assert result.llm_called is True
    assert "LLM 调用未返回有效内容" in result.llm_error


def test_insight_provider_reserves_before_dispatch_and_settles(tmp_path: Path, monkeypatch):
    from core.llm_config import LLMApiChain, LLMApiConfig

    class RuntimeConfig:
        data_dir = tmp_path
        database_dir = tmp_path

        def get(self, key, default=None):
            if key == "llm.provider_prices":
                return {"test": {"test-model": {"input": 0.1, "output": 0.2}}}
            return default

    config = RuntimeConfig()
    chain = LLMApiChain(
        primary=LLMApiConfig(
            provider="test",
            api_key="test-key",
            base_url="https://provider.example/v1",
            model="test-model",
            source="test",
        )
    )
    snapshots = []
    constructor_kwargs = []

    def create(**kwargs):
        with sqlite3.connect(str(tmp_path / "model_call_ledger.db")) as conn:
            snapshots.append(
                conn.execute(
                    "SELECT lifecycle_state, request_dispatched FROM model_call_entries "
                    "WHERE operation='reflection_insight'"
                ).fetchone()
            )
            provider_input = canonical_chat_input(kwargs["messages"])
            reservation_input = conn.execute(
                "SELECT reserved_input_tokens, input_digest FROM model_call_entries "
                "WHERE operation='reflection_insight'"
            ).fetchone()
        assert reservation_input == (
            utf8_token_upper_bound(provider_input),
            hashlib.sha256(provider_input.encode("utf-8")).hexdigest(),
        )
        return SimpleNamespace(
            id="request-reflection-1",
            usage=SimpleNamespace(prompt_tokens=8, completion_tokens=3),
            choices=[SimpleNamespace(message=SimpleNamespace(content="insight response"))],
        )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            constructor_kwargs.append(kwargs)
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))

    monkeypatch.setattr("core.reflection.insight_generator.get_config", lambda: config)
    monkeypatch.setattr("core.reflection.insight_generator.resolve_llm_api_chain", lambda _config: chain)
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))

    assert InsightGenerator()._call_llm("secret 洞察提示") == "insight response"
    assert snapshots == [("reserved", 1)]
    assert constructor_kwargs[0]["max_retries"] == 0
    with sqlite3.connect(str(tmp_path / "model_call_ledger.db")) as conn:
        row = conn.execute(
            "SELECT lifecycle_state, provider_usage_id, actual_input_tokens, actual_output_tokens "
            "FROM model_call_entries WHERE operation='reflection_insight'"
        ).fetchone()
    assert row == ("settled", "", 8, 3)
    assert b"secret \xe6\xb4\x9e\xe5\xaf\x9f\xe6\x8f\x90\xe7\xa4\xba" not in (
        tmp_path / "model_call_ledger.db"
    ).read_bytes()


def test_insight_openai_error_preserves_dispatched_reservation(tmp_path: Path, monkeypatch):
    from core.llm_config import LLMApiChain, LLMApiConfig

    class RuntimeConfig:
        data_dir = tmp_path
        database_dir = tmp_path

        def get(self, key, default=None):
            if key == "llm.provider_prices":
                return {"test": {"test-model": {"input": 0.1, "output": 0.2}}}
            return default

    class ProviderError(Exception):
        pass

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self._create)
            )

        @staticmethod
        def _create(**_kwargs):
            raise ProviderError("insight provider failed")

    chain = LLMApiChain(
        primary=LLMApiConfig(
            provider="test",
            api_key="test-key",
            base_url="https://provider.example/v1",
            model="test-model",
            source="test",
        )
    )
    monkeypatch.setattr(
        "core.reflection.insight_generator.get_config", lambda: RuntimeConfig()
    )
    monkeypatch.setattr(
        "core.reflection.insight_generator.resolve_llm_api_chain", lambda _config: chain
    )
    monkeypatch.setitem(
        sys.modules,
        "openai",
        SimpleNamespace(OpenAI=FakeOpenAI, OpenAIError=ProviderError),
    )

    assert InsightGenerator()._call_llm("secret 洞察失败") is None
    with sqlite3.connect(str(tmp_path / "model_call_ledger.db")) as conn:
        row = conn.execute(
            "SELECT lifecycle_state, request_dispatched, error_code "
            "FROM model_call_entries WHERE operation='reflection_insight'"
        ).fetchone()
    assert row == ("incurred_unknown", 1, "reflection_provider_exception")
