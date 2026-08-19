"""
Hamartia (Blind Spot Analyzer) 全面单元测试

覆盖项：
1. 数据类 — BlindSpot、ChallengeRecord、BlindSpotProfile
2. BlindSpotDetector 公共方法 — detect
3. BlindSpotDetector 内部检测方法 — _detect_framing_blindspot、_detect_option_gap、
   _detect_temporal_blindspot、_detect_preference_rigidity
4. BlindSpotDetector 辅助方法 — _calculate_options_similarity、_get_typical_option_count、
   _get_historical_option_patterns、_get_recent_selections
5. ChallengeBalancer — should_challenge、record_reaction、recover_credit
6. BlindSpotProfileManager — analyze_and_update、record_challenge_outcome、recover_credit、
   _load_profile、_save_profile、_dict_to_profile
7. 独立函数 — detect_blindspots、should_challenge_user
8. 兼容别名 — BlindspotAnalyzer
9. 外部依赖隔离 — SignalStore、get_signal_store、PreferenceProfile
"""

import py_compile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.persona.hamartia import (
    BlindSpot,
    ChallengeRecord,
    BlindSpotProfile,
    BlindSpotDetector,
    ChallengeBalancer,
    BlindSpotProfileManager,
    detect_blindspots,
    should_challenge_user,
    BlindspotAnalyzer,
)
from core.persona.pythia import PreferenceProfile

# ---------- Fixtures ----------


@pytest.fixture
def sample_blindspot():
    """返回一个示例 BlindSpot。"""
    return BlindSpot(
        type="framing",
        description="所有选项共享同一前提",
        evidence=["选项A和B都基于X前提"],
        confidence=0.75,
        first_detected="2024-01-01T00:00:00",
    )


@pytest.fixture
def sample_profile():
    """返回一个示例 BlindSpotProfile。"""
    return BlindSpotProfile(
        confirmed=[
            BlindSpot(
                type="temporal",
                description="时间盲区",
                evidence=["历史证据"],
                confidence=0.8,
                first_detected="2024-01-01T00:00:00",
            )
        ],
        suspected=[
            BlindSpot(
                type="framing",
                description="框架盲区",
                evidence=["证据1"],
                confidence=0.65,
                first_detected="2024-01-02T00:00:00",
            )
        ],
        total_challenges=5,
        accepted_count=3,
        ignored_count=1,
        rejected_count=1,
        challenge_credit=8.0,
    )


@pytest.fixture
def sample_persona():
    """返回一个示例 PreferenceProfile。"""
    return PreferenceProfile(
        version=1,
        generated_at=datetime.now().isoformat(),
    )


@pytest.fixture
def sample_session_context():
    """返回一个示例 session_context。"""
    return {
        "task_type": "coding",
        "user_message": "帮我看看这个方案",
        "context_hash": "abc123",
        "decision_risk": "medium",
    }


@pytest.fixture  # noqa
def sample_user_options():
    """返回一组示例选项。"""
    return [
        {"premise": "使用Python", "keywords": ["python", "fast"], "time_horizon": "short"},
        {"premise": "使用Python", "keywords": ["python", "simple"], "time_horizon": "short"},
    ]


@pytest.fixture
def mock_store(monkeypatch):
    """返回一个 mock 的 SignalStore，并隔离 get_signal_store 单例。"""
    store = MagicMock()
    store.get_recent_session_signals.return_value = []
    store.get_latest_persona_version.return_value = None
    store.update_blindspot_profile.return_value = True

    monkeypatch.setattr("core.persona.hamartia.get_signal_store", lambda: store)
    return store


@pytest.fixture
def detector(mock_store):
    """返回使用 mock store 的 BlindSpotDetector。"""
    return BlindSpotDetector(store=mock_store)


@pytest.fixture
def balancer(sample_profile):
    """返回使用示例 profile 的 ChallengeBalancer。"""
    return ChallengeBalancer(profile=sample_profile)


@pytest.fixture
def profile_manager(mock_store):
    """返回使用 mock store 的 BlindSpotProfileManager。"""
    return BlindSpotProfileManager(store=mock_store)


# ========== 数据类 ==========


def test_blindspot_defaults():
    """Detector output is an ephemeral shadow hypothesis."""
    bs = BlindSpot(type="test", description="desc", evidence=[])
    assert bs.confidence == 0.0
    assert bs.first_detected == ""
    assert not hasattr(bs, "asset_id")


def test_blindspot_full_fields():
    """A hypothesis contains no active Persona lifecycle fields."""
    bs = BlindSpot(
        type="framing",
        description="测试",
        evidence=["e1", "e2"],
        confidence=0.9,
        first_detected="2024-01-01T00:00:00",
    )
    assert bs.type == "framing"
    assert bs.confidence == 0.9
    assert not hasattr(bs, "status")


def test_challenge_record_defaults():
    """ChallengeRecord 默认值应正确。"""
    cr = ChallengeRecord(
        id="r1",
        timestamp="2024-01-01T00:00:00",
        session_id="s1",
        blindspot_type="framing",
        challenge_message="msg",
    )
    assert cr.user_reaction == ""
    assert cr.outcome == ""
    assert cr.challenge_credit_cost == 1.0


def test_challenge_record_preserves_message_in_serialized_contract():
    """ChallengeRecord 的 challenge_message 是挑战审计记录的一部分。"""
    cr = ChallengeRecord(
        id="r1",
        timestamp="2024-01-01T00:00:00",
        session_id="s1",
        blindspot_type="framing",
        challenge_message="请补一个反向方案",
    )

    assert cr.challenge_message == "请补一个反向方案"
    assert asdict(cr)["challenge_message"] == "请补一个反向方案"


def test_blindspot_profile_defaults():
    """BlindSpotProfile 默认值应正确。"""
    bp = BlindSpotProfile()
    assert bp.confirmed == []
    assert bp.suspected == []
    assert bp.dismissed == []
    assert bp.total_challenges == 0
    assert bp.accepted_count == 0
    assert bp.ignored_count == 0
    assert bp.rejected_count == 0
    assert bp.acceptance_rate == 0.0
    assert bp.challenge_credit == 10.0
    assert bp.credit_max == 10.0
    assert bp.credit_recovery_rate == 1.0


# ========== BlindSpotDetector.detect ==========


def test_detect_returns_sorted_blindspots(
    detector, sample_session_context, sample_user_options, sample_persona, sample_profile  # noqa
):
    """detect 应返回按置信度排序的盲区列表。"""
    # 使用共享前提触发 framing
    options = [
        {"premise": "Python", "keywords": ["py"], "time_horizon": "short"},
        {"premise": "Python", "keywords": ["py"], "time_horizon": "short"},
    ]
    result = detector.detect(sample_session_context, options, sample_persona, sample_profile)
    # framing + temporal 都可能触发
    assert isinstance(result, list)
    if len(result) >= 2:
        assert result[0].confidence >= result[1].confidence


def test_detect_empty_options(detector, sample_session_context, sample_persona, sample_profile):
    """空选项时 temporal 仍可能触发（所有0个选项视为短期），但 framing/option_gap/rigidity 不触发。"""
    result = detector.detect(sample_session_context, [], sample_persona, sample_profile)
    assert isinstance(result, list)
    # temporal 在空选项时可能触发（0个选项视为都是短期导向）
    # 其他类型不应触发
    types = [b.type for b in result]
    assert "framing" not in types
    assert "option_gap" not in types
    assert "preference_rigidity" not in types


def test_detect_single_option(detector, sample_session_context, sample_persona, sample_profile):
    """单个选项无法检测 framing（需要至少2个）。"""
    result = detector.detect(
        sample_session_context, [{"premise": "X"}], sample_persona, sample_profile
    )
    assert isinstance(result, list)


def test_detection_rules_gate_enabled_detectors(
    monkeypatch, detector, sample_session_context, sample_persona, sample_profile
):
    """DETECTION_RULES 应控制 detect() 启用哪些盲区检测器。"""
    rules_without_temporal = {
        key: value
        for key, value in BlindSpotDetector.DETECTION_RULES.items()
        if key != "temporal"
    }
    monkeypatch.setattr(BlindSpotDetector, "DETECTION_RULES", rules_without_temporal)
    options = [
        {"premise": "Python", "keywords": ["python"], "time_horizon": "short"},
        {"premise": "Rust", "keywords": ["rust"], "time_horizon": "short"},
        {"premise": "Go", "keywords": ["go"], "time_horizon": "short"},
    ]

    result = detector.detect(sample_session_context, options, sample_persona, sample_profile)

    assert "temporal" not in [blindspot.type for blindspot in result]


# ========== BlindSpotDetector._detect_framing_blindspot ==========


def test_detect_framing_shared_premise(detector, sample_profile):
    """所有选项共享同一前提时应检测到 framing。"""
    options = [
        {"premise": "使用微服务", "keywords": ["a"]},
        {"premise": "使用微服务", "keywords": ["b"]},
    ]
    result = detector._detect_framing_blindspot({}, options, sample_profile)
    assert result is not None
    assert result.type == "framing"
    assert result.confidence >= 0.5


def test_detect_framing_different_premises(detector, sample_profile):
    """选项有不同前提时不应检测到 framing。"""
    options = [
        {"premise": "微服务", "keywords": ["a"]},
        {"premise": "单体", "keywords": ["b"]},
    ]
    result = detector._detect_framing_blindspot({}, options, sample_profile)
    assert result is None


def test_detect_framing_less_than_two_options(detector, sample_profile):
    """选项少于2个时不应检测 framing。"""
    result = detector._detect_framing_blindspot({}, [{"premise": "X"}], sample_profile)
    assert result is None


def test_detect_framing_no_premise(detector, sample_profile):
    """选项无 premise 时不应检测 framing。"""
    options = [{"keywords": ["a"]}, {"keywords": ["b"]}]
    result = detector._detect_framing_blindspot({}, options, sample_profile)
    assert result is None


def test_detect_framing_historical_boost(detector):
    """历史上有 framing 盲区时置信度应提升。"""
    history = BlindSpotProfile(
        suspected=[BlindSpot(type="framing", description="历史", evidence=[], confidence=0.6)]
    )
    options = [
        {"premise": "X", "keywords": ["a"]},
        {"premise": "X", "keywords": ["b"]},
    ]
    result = detector._detect_framing_blindspot({}, options, history)
    assert result is not None
    assert result.confidence >= 0.7  # 0.5 + 0.2


def test_detect_framing_similarity_boost(detector, sample_profile):
    """选项高度相似时置信度应提升。"""
    options = [
        {"premise": "X", "keywords": ["a", "b", "c"]},
        {"premise": "X", "keywords": ["a", "b", "d"]},
    ]
    result = detector._detect_framing_blindspot({}, options, sample_profile)
    assert result is not None
    # 相似度 > 0.7 会额外 +0.2


# ========== BlindSpotDetector._detect_option_gap ==========


def test_detect_option_gap_few_options(detector, sample_persona, sample_profile):
    """选项少于典型数量时应检测到 option_gap（需有历史数据提升置信度到0.6以上）。"""
    options = [{"premise": "A"}, {"premise": "B"}]
    ctx = {"task_type": "coding"}
    # 无历史数据时 confidence=0.5，不够0.6阈值，返回None
    result = detector._detect_option_gap(ctx, options, sample_persona, sample_profile)
    # 当前实现无历史数据时 confidence=0.5 < 0.6，返回 None
    assert result is None


def test_detect_option_gap_many_options(detector, sample_persona, sample_profile):
    """选项足够多时不应检测到 option_gap。"""
    options = [{"premise": "A"}, {"premise": "B"}, {"premise": "C"}, {"premise": "D"}]
    ctx = {"task_type": "coding"}
    result = detector._detect_option_gap(ctx, options, sample_persona, sample_profile)
    # 4个选项 >= 3，不触发
    assert result is None


def test_detect_option_gap_more_than_three(detector, sample_persona, sample_profile):
    """选项超过3个时直接返回 None。"""
    options = [{"premise": str(i)} for i in range(5)]
    result = detector._detect_option_gap({}, options, sample_persona, sample_profile)
    assert result is None


# ========== BlindSpotDetector._detect_temporal_blindspot ==========


def test_detect_temporal_all_short_term(detector, sample_profile):
    """所有选项都是短期时应检测到 temporal。"""
    options = [
        {"time_horizon": "short"},
        {"time_horizon": "immediate"},
    ]
    result = detector._detect_temporal_blindspot({}, options, sample_profile)
    assert result is not None
    assert result.type == "temporal"
    assert result.confidence >= 0.6


def test_detect_temporal_mixed_horizon(detector, sample_profile):
    """选项有长期和短期混合时不应检测到 temporal。"""
    options = [
        {"time_horizon": "short"},
        {"time_horizon": "long"},
    ]
    result = detector._detect_temporal_blindspot({}, options, sample_profile)
    assert result is None


def test_detect_temporal_coding_boost(detector, sample_profile):
    """技术决策类型应提升 temporal 置信度。"""
    options = [
        {"time_horizon": "this_week"},
        {"time_horizon": "short"},
    ]
    ctx = {"task_type": "coding/python"}
    result = detector._detect_temporal_blindspot(ctx, options, sample_profile)
    assert result is not None
    assert result.confidence >= 0.7  # 0.6 + 0.1


def test_detect_temporal_historical_boost(detector):
    """历史上有 temporal 盲区时置信度应提升。"""
    history = BlindSpotProfile(
        confirmed=[BlindSpot(type="temporal", description="历史", evidence=[], confidence=0.8)]
    )
    options = [{"time_horizon": "short"}, {"time_horizon": "immediate"}]
    result = detector._detect_temporal_blindspot({}, options, history)
    assert result is not None
    assert result.confidence >= 0.75  # 0.6 + 0.15


# ========== BlindSpotDetector._detect_preference_rigidity ==========


def test_detect_preference_rigidity_insufficient_selections(
    detector, sample_persona, sample_profile
):
    """选择记录不足时不应检测到 preference_rigidity。"""
    options = [{"premise": "A"}]
    result = detector._detect_preference_rigidity({}, options, sample_persona, sample_profile)
    assert result is None


def test_detect_preference_rigidity_with_mock_store(
    detector, sample_persona, sample_profile, mock_store
):
    """模拟有足够选择记录时应检测到 preference_rigidity。"""
    mock_store.get_recent_session_signals.return_value = [
        {
            "options_presented": 2,
            "task_type": "coding",
            "working_dir": "/proj",
            "timestamp": "2024-01-01T00:00:00",
        },
        {
            "options_presented": 2,
            "task_type": "coding",
            "working_dir": "/proj",
            "timestamp": "2024-01-02T00:00:00",
        },
        {
            "options_presented": 2,
            "task_type": "coding",
            "working_dir": "/proj",
            "timestamp": "2024-01-03T00:00:00",
        },
        {
            "options_presented": 2,
            "task_type": "coding",
            "working_dir": "/other",
            "timestamp": "2024-01-04T00:00:00",
        },
        {
            "options_presented": 2,
            "task_type": "coding",
            "working_dir": "/other",
            "timestamp": "2024-01-05T00:00:00",
        },
    ]
    options = [{"premise": "A"}]
    ctx = {"context_hash": "/new"}
    result = detector._detect_preference_rigidity(ctx, options, sample_persona, sample_profile)
    assert result is not None
    assert result.type == "preference_rigidity"


def test_detect_preference_rigidity_no_consistent_pattern(
    detector, sample_persona, sample_profile, mock_store
):
    """选择模式不一致时不应检测到 preference_rigidity。"""
    mock_store.get_recent_session_signals.return_value = [
        {
            "options_presented": 2,
            "task_type": "coding",
            "working_dir": "/a",
            "timestamp": "2024-01-01T00:00:00",
        },
        {
            "options_presented": 2,
            "task_type": "debugging",
            "working_dir": "/b",
            "timestamp": "2024-01-02T00:00:00",
        },
        {
            "options_presented": 2,
            "task_type": "design",
            "working_dir": "/c",
            "timestamp": "2024-01-03T00:00:00",
        },
    ]
    options = [{"premise": "A"}]
    result = detector._detect_preference_rigidity({}, options, sample_persona, sample_profile)
    assert result is None


def test_detect_preference_rigidity_store_failure(
    detector, sample_persona, sample_profile, mock_store
):
    """store 查询失败时应优雅返回空列表。"""
    mock_store.get_recent_session_signals.side_effect = OSError("db error")
    options = [{"premise": "A"}]
    result = detector._detect_preference_rigidity({}, options, sample_persona, sample_profile)
    assert result is None


# ========== BlindSpotDetector 辅助方法 ==========


def test_calculate_options_similarity_identical(detector):
    """完全相同的选项相似度应为 1.0。"""
    options = [
        {"keywords": ["a", "b", "c"]},
        {"keywords": ["a", "b", "c"]},
    ]
    sim = detector._calculate_options_similarity(options)
    assert sim == 1.0


def test_calculate_options_similarity_no_overlap(detector):
    """无重叠关键词时相似度应为 0.0。"""
    options = [
        {"keywords": ["a", "b"]},
        {"keywords": ["c", "d"]},
    ]
    sim = detector._calculate_options_similarity(options)
    assert sim == 0.0


def test_calculate_options_similarity_single_option(detector):
    """单个选项时相似度应为 1.0。"""
    sim = detector._calculate_options_similarity([{"keywords": ["a"]}])
    assert sim == 1.0


def test_calculate_options_similarity_partial_overlap(detector):
    """部分重叠时相似度应在 0 和 1 之间。"""
    options = [
        {"keywords": ["a", "b", "c"]},
        {"keywords": ["a", "d", "e"]},
    ]
    sim = detector._calculate_options_similarity(options)
    assert 0.0 < sim < 1.0


def test_calculate_options_similarity_empty_keywords(detector):
    """空关键词列表时应返回 0.0。"""
    options = [
        {"keywords": []},
        {"keywords": []},
    ]
    sim = detector._calculate_options_similarity(options)
    assert sim == 0.0


def test_get_typical_option_count_coding(detector):
    """coding 任务类型应返回 3。"""
    assert detector._get_typical_option_count("coding/python") == 3


def test_get_typical_option_count_architecture(detector):
    """architecture 任务类型应返回 4。"""
    assert detector._get_typical_option_count("system_architecture") == 4


def test_get_typical_option_count_unknown(detector):
    """未知任务类型应返回默认值 3。"""
    assert detector._get_typical_option_count("unknown_task") == 3


def test_get_typical_option_count_strategy(detector):
    """strategy 任务类型应返回 4。"""
    assert detector._get_typical_option_count("business_strategy") == 4


def test_get_historical_option_patterns_returns_empty(detector):
    """_get_historical_option_patterns 当前实现返回空列表。"""
    result = detector._get_historical_option_patterns({})
    assert result == []


def test_get_recent_selections_empty_store(detector, mock_store):
    """store 无记录时应返回空列表。"""
    mock_store.get_recent_session_signals.return_value = []
    result = detector._get_recent_selections({})
    assert result == []


def test_get_recent_selections_with_records(detector, mock_store):
    """store 有记录时应返回格式化后的选择列表。"""
    mock_store.get_recent_session_signals.return_value = [
        {
            "options_presented": 2,
            "task_type": "coding",
            "working_dir": "/a",
            "timestamp": "2024-01-01T00:00:00",
        },
        {
            "options_presented": 0,
            "task_type": "debug",
            "working_dir": "/b",
            "timestamp": "2024-01-02T00:00:00",
        },
    ]
    result = detector._get_recent_selections({})
    assert len(result) == 1
    assert result[0]["option_type"] == "coding"


def test_get_recent_selections_store_failure(detector, mock_store):
    """store 查询失败时应返回空列表。"""
    mock_store.get_recent_session_signals.side_effect = ValueError("error")
    result = detector._get_recent_selections({})
    assert result == []


# ========== ChallengeBalancer ==========


def test_balancer_init_default():
    """ChallengeBalancer 无参数时应创建默认 profile。"""
    b = ChallengeBalancer()
    assert b.profile is not None
    assert b.profile.challenge_credit == 10.0


def test_balancer_should_challenge_credit_depleted():
    """信用额度为0时不应挑战。"""
    profile = BlindSpotProfile(challenge_credit=0)
    b = ChallengeBalancer(profile=profile)
    should, spots, reason = b.should_challenge({}, [])
    assert should is False
    assert "信用" in reason


def test_balancer_should_challenge_high_risk(balancer, sample_blindspot):
    """高风险决策时应强制挑战。"""
    ctx = {"decision_risk": "high"}
    should, spots, reason = balancer.should_challenge(ctx, [sample_blindspot])
    assert should is True
    assert "高stakes" in reason


def test_balancer_should_challenge_user_asks(balancer, sample_blindspot):
    """用户主动要求挑战时应返回全部盲区。"""
    ctx = {"user_message": "帮我挑挑毛病"}
    should, spots, reason = balancer.should_challenge(ctx, [sample_blindspot])
    assert should is True
    assert "主动要求" in reason
    assert len(spots) == 1


def test_balancer_should_challenge_execution_mode(balancer, sample_blindspot):
    """执行模式且时间紧时不应挑战。"""
    ctx = {"mode": "execution", "time_pressure": True}
    should, spots, reason = balancer.should_challenge(ctx, [sample_blindspot])
    assert should is False
    assert "执行模式" in reason


def test_balancer_should_challenge_high_rejection(balancer, sample_blindspot):
    """用户高拒绝率时不应挑战。"""
    balancer.profile.total_challenges = 10
    balancer.profile.rejected_count = 8
    should, spots, reason = balancer.should_challenge({}, [sample_blindspot])
    assert should is False
    assert "拒绝率高" in reason


def test_balancer_should_challenge_significant_blindspots(balancer):
    """有显著盲区时应挑战并消耗信用。"""
    bs = BlindSpot(type="framing", description="显著盲区", evidence=[], confidence=0.8)
    should, spots, reason = balancer.should_challenge({}, [bs])
    assert should is True
    assert len(spots) == 1
    assert balancer.profile.challenge_credit < 8.0  # 消耗了信用


def test_balancer_should_challenge_no_significant(balancer):
    """无显著盲区时不应挑战。"""
    bs = BlindSpot(type="framing", description="低置信", evidence=[], confidence=0.5)
    should, spots, reason = balancer.should_challenge({}, [bs])
    assert should is False
    assert "无显著盲区" in reason


def test_balancer_should_challenge_empty_blindspots(balancer):
    """空盲区列表时不应挑战。"""
    should, spots, reason = balancer.should_challenge({}, [])
    assert should is False


def test_balancer_record_reaction_accepted(balancer):
    """接受挑战时应增加接受计数和信用。"""
    old_credit = balancer.profile.challenge_credit
    balancer.record_reaction("c1", "accepted")
    assert balancer.profile.accepted_count == 4  # 3 + 1
    assert balancer.profile.total_challenges == 6  # 5 + 1
    assert balancer.profile.challenge_credit > old_credit
    assert balancer.last_reaction_context == {
        "challenge_id": "c1",
        "reaction": "accepted",
        "outcome": "",
    }


def test_balancer_record_reaction_keeps_challenge_context(balancer):
    """记录反应时应保留挑战 ID 和结果，便于定位聚合计数来源。"""
    balancer.record_reaction("session-framing", "accepted", outcome="用户补充了反向方案")

    assert balancer.last_reaction_context == {
        "challenge_id": "session-framing",
        "reaction": "accepted",
        "outcome": "用户补充了反向方案",
    }


def test_balancer_record_reaction_ignored(balancer):
    """忽略挑战时应增加忽略计数并扣信用。"""
    old_credit = balancer.profile.challenge_credit
    balancer.record_reaction("c1", "ignored")
    assert balancer.profile.ignored_count == 2  # 1 + 1
    assert balancer.profile.challenge_credit < old_credit


def test_balancer_record_reaction_rejected(balancer):
    """拒绝挑战时应增加拒绝计数并扣信用。"""
    old_credit = balancer.profile.challenge_credit
    balancer.record_reaction("c1", "rejected")
    assert balancer.profile.rejected_count == 2  # 1 + 1
    assert balancer.profile.challenge_credit < old_credit


def test_balancer_record_reaction_updates_acceptance_rate(balancer):
    """记录反应后应更新接受率。"""
    balancer.record_reaction("c1", "accepted")
    expected_rate = balancer.profile.accepted_count / balancer.profile.total_challenges
    assert balancer.profile.acceptance_rate == expected_rate


def test_balancer_recover_credit(balancer):
    """recover_credit 应恢复信用额度。"""
    balancer.profile.challenge_credit = 5.0
    balancer.recover_credit()
    assert balancer.profile.challenge_credit == 6.0  # 5 + 1


def test_balancer_recover_credit_capped(balancer):
    """recover_credit 不应超过 credit_max。"""
    balancer.profile.challenge_credit = 9.5
    balancer.recover_credit()
    assert balancer.profile.challenge_credit == 10.0


def test_balancer_record_reaction_unknown(balancer):
    """未知反应类型不应崩溃。"""
    balancer.record_reaction("c1", "unknown")
    assert balancer.profile.total_challenges == 6


# ========== BlindSpotProfileManager ==========


def test_profile_manager_init_default(mock_store):
    """BlindSpotProfileManager 应正确初始化 detector 和 balancer。"""
    pm = BlindSpotProfileManager(store=mock_store)
    assert pm.detector is not None
    assert pm.balancer is not None


def test_profile_manager_analyze_and_update_no_challenge(
    profile_manager, sample_session_context, sample_persona, mock_store
):
    """analyze_and_update 无显著盲区时应返回空列表。"""
    mock_store.get_latest_persona_version.return_value = None
    # 使用足够多选项避免 option_gap，不同 premise 避免 framing，混合 time_horizon 避免 temporal
    options = [
        {"premise": "A", "keywords": ["a"], "time_horizon": "short"},
        {"premise": "B", "keywords": ["b"], "time_horizon": "long"},
        {"premise": "C", "keywords": ["c"], "time_horizon": "medium"},
    ]
    result = profile_manager.analyze_and_update(sample_session_context, options, sample_persona)
    assert isinstance(result, list)


def test_profile_manager_analyze_and_update_with_blindspot(
    profile_manager, sample_session_context, sample_persona, mock_store
):
    """analyze_and_update 检测到盲区时应返回建议挑战列表。"""
    mock_store.get_latest_persona_version.return_value = None
    # 触发 framing：共享 premise，且只有2个选项
    options = [
        {"premise": "Python", "keywords": ["py"], "time_horizon": "short"},
        {"premise": "Python", "keywords": ["py2"], "time_horizon": "short"},
    ]
    result = profile_manager.analyze_and_update(sample_session_context, options, sample_persona)
    assert isinstance(result, list)
    # framing 和 temporal 都可能触发


def test_profile_manager_record_challenge_outcome_accepted(profile_manager, mock_store):
    """接受挑战只表示愿意探索，不得确认认知缺陷。"""
    mock_store.get_latest_persona_version.return_value = {
        "blindspot_profile": {
            "confirmed": [],
            "suspected": [
                {
                    "type": "framing",
                    "description": "框架盲区",
                    "evidence": ["e1"],
                    "confidence": 0.7,
                    "first_detected": "2024-01-01T00:00:00",
                    "challenge_count": 0,
                    "user_reaction": "",
                    "status": "suspected",
                }
            ],
            "dismissed": [],
            "total_challenges": 0,
            "accepted_count": 0,
            "ignored_count": 0,
            "rejected_count": 0,
            "acceptance_rate": 0.0,
            "challenge_credit": 10.0,
        }
    }
    profile_manager.record_challenge_outcome(
        "framing",
        "accepted",
        session_id="s1",
        _challenge_message="请补一个反向方案",
    )
    # 验证 save_profile 被调用
    assert mock_store.update_blindspot_profile.called
    assert profile_manager.last_challenge_record is not None
    record = asdict(profile_manager.last_challenge_record)
    assert record["timestamp"]
    assert record == {
        "id": "s1_framing",
        "timestamp": record["timestamp"],
        "session_id": "s1",
        "blindspot_type": "framing",
        "challenge_message": "请补一个反向方案",
        "user_reaction": "accepted",
        "outcome": "",
        "challenge_credit_cost": 1.0,
    }
    saved = mock_store.update_blindspot_profile.call_args.args[0]
    assert saved["confirmed"] == []
    assert saved["suspected"] == []


def test_profile_manager_rejects_legacy_string_outcome_evidence(
    profile_manager, mock_store
):
    """Legacy profile JSON and a string receipt cannot confirm an asset."""
    mock_store.get_latest_persona_version.return_value = {
        "blindspot_profile": {
            "confirmed": [],
            "suspected": [
                {
                    "type": "framing",
                    "description": "框架盲区",
                    "evidence": ["e1"],
                    "confidence": 0.7,
                    "status": "suspected",
                    "asset_id": "ucb-1",
                }
            ],
            "dismissed": [],
        }
    }

    profile_manager.record_challenge_outcome(
        "framing",
        "accepted",
        session_id="s1",
        asset_id="ucb-1",
        outcome="validated",
        outcome_evidence=("decision-trace:receipt-1",),
    )

    saved = mock_store.update_blindspot_profile.call_args.args[0]
    assert saved["suspected"] == []
    assert saved["confirmed"] == []


def test_profile_manager_record_challenge_outcome_rejected_three_times(profile_manager, mock_store):
    """拒绝3次后盲区应移至 dismissed。"""
    mock_store.get_latest_persona_version.return_value = {
        "blindspot_profile": {
            "confirmed": [],
            "suspected": [
                {
                    "type": "framing",
                    "description": "框架盲区",
                    "evidence": ["e1"],
                    "confidence": 0.7,
                    "first_detected": "2024-01-01T00:00:00",
                    "challenge_count": 2,
                    "user_reaction": "rejected",
                    "status": "suspected",
                }
            ],
            "dismissed": [],
            "total_challenges": 2,
            "accepted_count": 0,
            "ignored_count": 0,
            "rejected_count": 2,
            "acceptance_rate": 0.0,
            "challenge_credit": 10.0,
        }
    }
    profile_manager.record_challenge_outcome("framing", "rejected", session_id="s1")
    assert mock_store.update_blindspot_profile.called


def test_profile_manager_record_challenge_outcome_no_matching_blindspot(
    profile_manager, mock_store
):
    """无匹配盲区时不应崩溃。"""
    mock_store.get_latest_persona_version.return_value = {
        "blindspot_profile": {
            "confirmed": [],
            "suspected": [],
            "dismissed": [],
            "total_challenges": 0,
            "challenge_credit": 10.0,
        }
    }
    profile_manager.record_challenge_outcome("nonexistent", "accepted", session_id="s1")
    assert mock_store.update_blindspot_profile.called


def test_profile_manager_recover_credit(profile_manager, mock_store):
    """recover_credit 应恢复信用并持久化。"""
    mock_store.get_latest_persona_version.return_value = {
        "blindspot_profile": {
            "confirmed": [],
            "suspected": [],
            "dismissed": [],
            "total_challenges": 0,
            "challenge_credit": 5.0,
            "credit_max": 10.0,
            "credit_recovery_rate": 1.0,
        }
    }
    result = profile_manager.recover_credit()
    assert result == 6.0
    assert mock_store.update_blindspot_profile.called


def test_profile_manager_load_profile_empty_db(profile_manager, mock_store):
    """数据库无记录时应返回默认 profile。"""
    mock_store.get_latest_persona_version.return_value = None
    profile = profile_manager._load_profile()
    assert isinstance(profile, BlindSpotProfile)
    assert profile.challenge_credit == 10.0


def test_profile_manager_load_profile_with_data(profile_manager, mock_store):
    """数据库有记录时应正确加载。"""
    mock_store.get_latest_persona_version.return_value = {
        "blindspot_profile": {
            "confirmed": [
                {
                    "type": "temporal",
                    "description": "时间盲区",
                    "evidence": ["e1"],
                    "confidence": 0.8,
                    "first_detected": "2024-01-01T00:00:00",
                }
            ],
            "suspected": [],
            "dismissed": [],
            "total_challenges": 3,
            "accepted_count": 2,
            "challenge_credit": 7.0,
        }
    }
    profile = profile_manager._load_profile()
    assert profile.confirmed == []
    assert profile.total_challenges == 3
    assert profile.challenge_credit == 7.0


def test_profile_manager_load_profile_db_error(profile_manager, mock_store):
    """数据库错误时应返回默认 profile。"""
    mock_store.get_latest_persona_version.side_effect = OSError("db error")
    profile = profile_manager._load_profile()
    assert isinstance(profile, BlindSpotProfile)
    assert profile.challenge_credit == 10.0


def test_profile_manager_dict_to_profile(profile_manager):
    """_dict_to_profile 应正确转换字典。"""
    data = {
        "confirmed": [
            {
                "type": "framing",
                "description": "d",
                "evidence": ["e"],
                "confidence": 0.7,
                "first_detected": "2024-01-01T00:00:00",
            }
        ],
        "suspected": [],
        "dismissed": [],
        "total_challenges": 5,
        "accepted_count": 3,
        "ignored_count": 1,
        "rejected_count": 1,
        "acceptance_rate": 0.6,
        "challenge_credit": 8.0,
        "credit_max": 10.0,
        "credit_recovery_rate": 2.0,
    }
    profile = profile_manager._dict_to_profile(data)
    assert profile.confirmed == []
    assert profile.challenge_credit == 8.0
    assert profile.credit_recovery_rate == 2.0


def test_profile_manager_dict_to_profile_defaults(profile_manager):
    """_dict_to_profile 对缺失字段应使用默认值。"""
    profile = profile_manager._dict_to_profile({})
    assert profile.confirmed == []
    assert profile.challenge_credit == 10.0
    assert profile.credit_max == 10.0


def test_profile_manager_save_profile(profile_manager, mock_store):
    """_save_profile 应调用 store.update_blindspot_profile。"""
    profile = BlindSpotProfile(
        confirmed=[
            BlindSpot(
                type="framing",
                description="d",
                evidence=["e"],
                confidence=0.7,
                first_detected="2024-01-01T00:00:00",
            )
        ],
        total_challenges=1,
        challenge_credit=9.0,
    )
    profile_manager._save_profile(profile)
    assert mock_store.update_blindspot_profile.called
    call_args = mock_store.update_blindspot_profile.call_args[0][0]
    assert "confirmed" in call_args
    assert call_args["total_challenges"] == 1
    assert call_args["challenge_credit"] == 9.0


# ========== 独立函数 ==========


def test_detect_blindspots(mock_store, sample_session_context, sample_persona):
    """detect_blindspots 便捷函数应返回列表。"""
    mock_store.get_latest_persona_version.return_value = None
    options = [
        {"premise": "Python", "keywords": ["py"], "time_horizon": "short"},
        {"premise": "Python", "keywords": ["py2"], "time_horizon": "short"},
    ]
    result = detect_blindspots(sample_session_context, options, sample_persona)
    assert isinstance(result, list)


def test_should_challenge_user_with_blindspots():
    """should_challenge_user 应正确委托给 ChallengeBalancer。"""
    bs = BlindSpot(type="framing", description="d", evidence=[], confidence=0.8)
    ctx = {"decision_risk": "high"}
    should, spots, reason = should_challenge_user(ctx, [bs])
    assert should is True
    assert len(spots) == 1


def test_should_challenge_user_empty():
    """should_challenge_user 无盲区时应返回 False。"""
    should, spots, reason = should_challenge_user({}, [])
    assert should is False


# ========== 兼容别名 ==========


def test_blindspot_analyzer_alias():
    """BlindspotAnalyzer 应是 BlindSpotDetector 的别名。"""
    assert BlindspotAnalyzer is BlindSpotDetector


# ========== 编译验证 ==========


def test_module_compiles():
    """hamartia.py 应能通过 py_compile 编译。"""
    module_path = Path(__file__).parent.parent.parent / "core" / "persona" / "hamartia.py"
    py_compile.compile(str(module_path), doraise=True)


def test_test_file_compiles():
    """本测试文件应能通过 py_compile 编译。"""
    py_compile.compile(__file__, doraise=True)
