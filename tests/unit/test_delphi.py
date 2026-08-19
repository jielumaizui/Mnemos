"""
Delphi (PersonaStore + KnowledgeAligner) 单元测试

覆盖公共 API：
2. KnowledgeAligner — 知识-画像匹配度计算
3. PersonaStore — 画像存储、加载、版本、反写
4. 便捷函数 — save_persona_to_wiki、align_wiki_with_persona、get_persona_store
5. 行为提示词 — _ensure_ab_test_group、_load_base_behavior_prompt、get_behavior_prompt
"""

import pytest
from pathlib import Path
from types import SimpleNamespace

from core.persona.pythia import (
    PreferenceProfile,
    EnergyProfile,
    CognitiveProfile,
    ValueProfile,
)
from core.cognitive.user_model_assets import AssetScope, UserCognitiveBlindspot
from core.persona.hamartia import BlindSpotProfile
from core.persona.psyche import SignalStore
from tests.persona_decision_fixtures import (
    authorized_persona_store,
    save_persona_version_authorized,
)

# ---------- Fixtures ----------


@pytest.fixture
def sample_profile():
    """返回一个可用于测试的 PreferenceProfile。"""
    return PreferenceProfile(
        version=1,
        generated_at="2024-01-01T00:00:00",
        period_start="2024-01-01",
        period_end="2024-01-31",
        energy=EnergyProfile(
            focus_depth=0.8,
            startup_difficulty=0.7,
            endurance_mode=0.5,
            switching_flexibility=0.3,
            recovery_cycle=0.5,
            confidence=0.6,
            insufficient_dimensions=["recovery_cycle"],
        ),
        cognitive=CognitiveProfile(
            abstraction=0.7,
            system_view=0.6,
            skepticism=0.4,
            creativity=0.5,
            deduction=0.5,
            confidence=0.5,
            insufficient_dimensions=["creativity", "deduction"],
        ),
        value=ValueProfile(
            correctness_vs_efficiency=0.7,
            depth_vs_breadth=0.3,
            perfection_vs_completion=0.6,
            innovation_vs_safety=0.4,
            autonomy_vs_collaboration=0.5,
            action_vs_analysis=0.5,
            confidence=0.5,
            insufficient_dimensions=["autonomy_vs_collaboration", "action_vs_analysis"],
        ),
        signal_count=42,
    )


@pytest.fixture
def sample_blindspot():
    """返回一个可用于测试的 BlindSpotProfile。"""
    return BlindSpotProfile(
        confirmed=[
            UserCognitiveBlindspot.create(
                blindspot_type="framing",
                description="框架盲区测试",
                evidence_refs=("source-authority:e1", "source-authority:e2"),
                user_goal_ref="goal:test-roundtrip",
                impact="May exclude another frame.",
                scope=AssetScope(
                    scope_type="session",
                    scope_id="delphi-roundtrip",
                    purpose="decision_support",
                ),
                confidence=0.8,
                expires_at="2099-01-01T00:00:00+00:00",
                invalidation_condition="A later exact decision has independent frames.",
                first_detected="2024-01-01T00:00:00",
            )
        ],
        suspected=[
            UserCognitiveBlindspot.create(
                blindspot_type="option_gap",
                description="选项盲区测试",
                evidence_refs=("source-authority:e3",),
                user_goal_ref="goal:test-roundtrip",
                impact="May exclude another option.",
                scope=AssetScope(
                    scope_type="session",
                    scope_id="delphi-roundtrip",
                    purpose="decision_support",
                ),
                confidence=0.5,
                expires_at="2099-01-01T00:00:00+00:00",
                invalidation_condition="A later exact decision includes the missing option.",
                first_detected="2024-01-02T00:00:00",
            )
        ],
        total_challenges=5,
        accepted_count=2,
        ignored_count=1,
        rejected_count=2,
        acceptance_rate=0.4,
        challenge_credit=8.0,
    )


@pytest.fixture
def mock_signal_store(tmp_path):
    """返回使用临时数据库的 SignalStore。"""
    db = tmp_path / "test_signals.db"
    store = SignalStore(initialize_schema=True, db_path=db)
    yield store
    store.close()


@pytest.fixture
def persona_store(tmp_path, mock_signal_store):
    """返回使用临时 wiki 目录和数据库的 PersonaStore。"""
    wiki_dir = tmp_path / "wiki"
    store = authorized_persona_store(
        wiki_dir=wiki_dir,
        signal_store=mock_signal_store,
    )
    return store


@pytest.fixture(autouse=True)  # noqa
def reset_delphi_singletons(monkeypatch):
    """每个测试前后重置 Delphi 单例和全局状态。"""
    import core.persona.delphi as _delphi

    monkeypatch.setattr(_delphi, "_persona_store_instance", None)
    monkeypatch.setattr(_delphi, "_ab_test_persona_driven", None)
    yield


# ========== KnowledgeAligner ==========


def test_knowledge_aligner_estimate_user_level(sample_profile):
    """_estimate_user_level 应基于 focus_depth 和 abstraction 映射到 1-9。"""
    from core.persona.delphi import KnowledgeAligner

    aligner = KnowledgeAligner(sample_profile)
    level = aligner._estimate_user_level()
    # (0.8 + 0.7) / 2 = 0.75 -> int(1 + 0.75 * 8) = 7
    assert level == 7


def test_knowledge_aligner_calc_preference_match_decision_feasibility(
    sample_profile,
):
    """decision 类型 + feasibility_first 偏好应得高分。"""
    from core.persona.delphi import KnowledgeAligner

    aligner = KnowledgeAligner(sample_profile)
    score = aligner._calc_preference_match("decision", {})
    # correctness_vs_efficiency=0.7 > 0.6 -> feasibility_first
    # TYPE_PREFERENCE_MATRIX["decision"]["feasibility_first"] = 1.0
    assert score == 1.0


def test_knowledge_aligner_calc_preference_match_risk_averse():
    """pitfall 类型 + 纯 risk_averse 偏好应得高分。"""
    from core.persona.delphi import KnowledgeAligner

    # 仅设置 risk_averse（innovation_vs_safety < 0.4），
    # correctness_vs_efficiency 保持中性（0.5）避免同时触发 feasibility_first
    profile = PreferenceProfile(
        value=ValueProfile(
            correctness_vs_efficiency=0.5,
            innovation_vs_safety=0.3,
        ),
    )
    aligner = KnowledgeAligner(profile)
    score = aligner._calc_preference_match("pitfall", {})
    # 仅 risk_averse 一个偏好，pitfall 对其评分 1.0
    assert score == 1.0


def test_knowledge_aligner_calc_preference_match_unknown_type():
    """未知页面类型应返回默认 0.5。"""
    from core.persona.delphi import KnowledgeAligner

    profile = PreferenceProfile(
        value=ValueProfile(correctness_vs_efficiency=0.5, innovation_vs_safety=0.5),
    )
    aligner = KnowledgeAligner(profile)
    score = aligner._calc_preference_match("unknown_type", {})
    assert score == 0.5


def test_knowledge_aligner_calc_preference_match_neutral_prefs():
    """中性偏好（无明确倾向）应返回默认 0.5。"""
    from core.persona.delphi import KnowledgeAligner

    profile = PreferenceProfile(
        value=ValueProfile(
            correctness_vs_efficiency=0.5,
            innovation_vs_safety=0.5,
        ),
    )
    aligner = KnowledgeAligner(profile)
    score = aligner._calc_preference_match("decision", {})
    assert score == 0.5


def test_knowledge_aligner_dynamic_preference_match_with_tags():
    """页面显式标签匹配用户偏好时应比不匹配时分数更高。"""
    from core.persona.delphi import KnowledgeAligner

    profile = PreferenceProfile(
        value=ValueProfile(
            correctness_vs_efficiency=0.7,  # feasibility_first
            innovation_vs_safety=0.3,  # risk_averse
        ),
    )
    aligner = KnowledgeAligner(profile)

    # 匹配的页面标签应比不匹配的分数高
    matching = aligner._calc_preference_match("decision", {"tags": ["risk_averse"]})
    mismatching = aligner._calc_preference_match("decision", {"tags": ["cost_first"]})
    assert 0.0 <= matching <= 1.0
    assert 0.0 <= mismatching <= 1.0
    assert matching > mismatching


def test_knowledge_aligner_dynamic_preference_match_clipped():
    """动态匹配分数不应超过 1.0，静态已为满分时仍保持满分。"""
    from core.persona.delphi import KnowledgeAligner

    profile = PreferenceProfile(
        value=ValueProfile(
            correctness_vs_efficiency=0.7,
            innovation_vs_safety=0.3,
        ),
    )
    aligner = KnowledgeAligner(profile)
    # pitfall + risk_averse 静态已是 1.0，加入显式标签后仍应为 1.0
    score = aligner._calc_preference_match(
        "pitfall", {"tags": ["risk_averse", "feasibility_first"]}
    )
    assert score == 1.0


def test_knowledge_aligner_calc_capability_match_sweet_spot(sample_profile):
    """能力与知识等级匹配时应返回 1.0（学习区）。"""
    from core.persona.delphi import KnowledgeAligner

    aligner = KnowledgeAligner(sample_profile)
    # user_level = 7, gap = 0 -> sweet spot
    score = aligner._calc_capability_match({"level": "L7"})
    assert score == 1.0


def test_knowledge_aligner_calc_capability_match_boredom(sample_profile):
    """知识太简单时应返回低分（无聊区）。"""
    from core.persona.delphi import KnowledgeAligner

    aligner = KnowledgeAligner(sample_profile)
    # user_level = 7, gap = -5 -> boredom
    score = aligner._calc_capability_match({"level": "L1"})
    assert score == 0.3


def test_knowledge_aligner_calc_capability_match_stretch(sample_profile):
    """知识略难时应返回中高分（拉伸区）。"""
    from core.persona.delphi import KnowledgeAligner

    aligner = KnowledgeAligner(sample_profile)
    # user_level = 7, gap = 1 -> stretch zone
    score = aligner._calc_capability_match({"level": "L8"})
    assert score == 0.7


def test_knowledge_aligner_calc_capability_match_panic(sample_profile):
    """知识太难时应返回低分（恐慌区）。"""
    from core.persona.delphi import KnowledgeAligner

    aligner = KnowledgeAligner(sample_profile)
    # user_level = 7, gap = 5 -> panic
    score = aligner._calc_capability_match({"level": "L12"})
    assert score == 0.1


def test_knowledge_aligner_calc_capability_match_invalid_level(sample_profile):
    """无效 level 应回退到默认 L2，与用户 level=7 形成 boredom 区。"""
    from core.persona.delphi import KnowledgeAligner

    aligner = KnowledgeAligner(sample_profile)
    score = aligner._calc_capability_match({"level": "invalid"})
    # invalid -> level_num=2, user_level=7, gap=-5 -> boredom zone = 0.3
    assert score == 0.3


def test_knowledge_aligner_calc_context_match_with_tags():
    """任务类型与页面标签匹配时应提高分数。"""
    from core.persona.delphi import KnowledgeAligner

    profile = PreferenceProfile()
    aligner = KnowledgeAligner(profile)
    wiki_page = {
        "path": "coding/python.md",
        "frontmatter": {"tags": ["python", "coding"]},
    }
    ctx = {"task_type": "coding/python", "working_dir": "/proj", "recent_queries": []}
    score = aligner._calc_context_match(wiki_page, ctx)
    assert score > 0.5


def test_knowledge_aligner_calc_context_match_no_context():
    """无 session_context 时应返回默认 0.5。"""
    from core.persona.delphi import KnowledgeAligner

    profile = PreferenceProfile()
    aligner = KnowledgeAligner(profile)
    wiki_page = {"path": "test.md", "frontmatter": {"tags": []}}
    score = aligner._calc_context_match(wiki_page, {})
    assert score == 0.5


def test_knowledge_aligner_calc_context_match_capped_at_1():
    """情境匹配分数不应超过 1.0。"""
    from core.persona.delphi import KnowledgeAligner

    profile = PreferenceProfile()
    aligner = KnowledgeAligner(profile)
    wiki_page = {
        "path": "coding/python/tutorial.md",
        "frontmatter": {"tags": ["python", "coding", "tutorial"]},
    }
    ctx = {
        "task_type": "coding/python/tutorial",
        "working_dir": "/proj/coding/python",
        "recent_queries": ["python", "tutorial"],
    }
    score = aligner._calc_context_match(wiki_page, ctx)
    assert score == 1.0


def test_knowledge_aligner_alignment_weights():
    """_get_alignment_weights 应返回固定权重。"""
    from core.persona.delphi import KnowledgeAligner

    profile = PreferenceProfile()
    aligner = KnowledgeAligner(profile)
    weights = aligner._get_alignment_weights()
    assert weights == {"preference": 0.3, "capability": 0.4, "context": 0.3}


def test_knowledge_aligner_calculate_alignment_full(sample_profile):
    """calculate_alignment 应返回包含所有维度的字典。"""
    from core.persona.delphi import KnowledgeAligner

    aligner = KnowledgeAligner(sample_profile)
    wiki_page = {
        "path": "test.md",
        "frontmatter": {"type": "decision", "level": "L7"},
        "content_snippet": "test",
    }
    ctx = {"task_type": "coding", "working_dir": "/proj", "recent_queries": []}
    result = aligner.calculate_alignment(wiki_page, ctx)

    assert set(result.keys()) == {
        "preference_match",
        "capability_match",
        "context_match",
        "total",
    }
    assert 0.0 <= result["preference_match"] <= 1.0
    assert 0.0 <= result["capability_match"] <= 1.0
    assert 0.0 <= result["context_match"] <= 1.0
    assert 0.0 <= result["total"] <= 1.0


def test_knowledge_aligner_calculate_alignment_without_context(sample_profile):
    """不提供 session_context 时 context_match 应保持默认 0.5。"""
    from core.persona.delphi import KnowledgeAligner

    aligner = KnowledgeAligner(sample_profile)
    wiki_page = {
        "path": "test.md",
        "frontmatter": {"type": "snippet", "level": "L7"},
        "content_snippet": "test",
    }
    result = aligner.calculate_alignment(wiki_page)
    assert result["context_match"] == 0.5


# ========== PersonaStore ==========


def test_persona_store_init_creates_directories(persona_store):
    """初始化应自动创建 wiki 子目录。"""
    assert persona_store.persona_page.parent.exists()
    assert persona_store.history_dir.exists()


def test_persona_store_save_and_load_roundtrip(persona_store, sample_profile):
    """save_persona + load_persona 应能完整往返。"""
    persona_store.save_persona(sample_profile)

    loaded, _ = persona_store.load_persona()
    assert loaded is not None
    assert loaded.version == sample_profile.version
    assert loaded.energy.focus_depth == sample_profile.energy.focus_depth
    assert loaded.cognitive.abstraction == sample_profile.cognitive.abstraction
    assert loaded.value.correctness_vs_efficiency == sample_profile.value.correctness_vs_efficiency
    assert loaded.signal_count == sample_profile.signal_count


@pytest.mark.no_canonical_material_actions
def test_persona_store_seals_its_own_exact_material_decisions(
    tmp_path,
    mock_signal_store,
    sample_profile,
    monkeypatch,
):
    import sqlite3

    from core.persona.delphi import PersonaStore

    values = {
        "trusted_push.mode": "off",
        "trusted_push.db_path": str(tmp_path / "trusted_push.db"),
    }
    config = SimpleNamespace(
        database_dir=tmp_path,
        get=lambda key, default=None: values.get(key, default),
    )
    monkeypatch.setattr("core.trust.config.get_config", lambda: config)
    store = PersonaStore(
        wiki_dir=tmp_path / "wiki",
        signal_store=mock_signal_store,
    )

    store.save_persona(sample_profile)

    assert store.persona_page.is_file()
    with sqlite3.connect(tmp_path / "producer_consumer_ledger.db") as conn:
        receipts = conn.execute("SELECT status FROM cognitive_state_effect_receipts").fetchall()
    assert receipts == [("committed",)]


def test_save_persona_enforce_publishes_deterministic_derived_projection(
    monkeypatch,
    tmp_path,
    persona_store,
    sample_profile,
):
    from core.trust.formal_cognitive_mutation import FormalCognitiveMutationJournal
    from core.trust.proposal_queue import ProposalQueue

    db_dir = tmp_path / "db"
    db_dir.mkdir()
    trusted_db = db_dir / "trusted_push.db"
    fake_config = SimpleNamespace(
        wiki_dir=persona_store.wiki_dir,
        database_dir=db_dir,
        get=lambda key, default=None: {
            "trusted_push.mode": "enforce",
            "trusted_push.db_path": str(trusted_db),
        }.get(key, default),
    )
    monkeypatch.setattr("core.trust.config.get_config", lambda: fake_config)

    persona_store.save_persona(sample_profile)

    assert persona_store.persona_page.exists()
    proposals = ProposalQueue(trusted_db, wiki_base=persona_store.wiki_dir).list()
    assert proposals == []
    binding = persona_store.projection_lifecycle.binding_for_path(persona_store.persona_page)
    assert binding is not None
    assert binding["page_role"] == "formal_derived:persona"
    assert binding["status"] == "published"
    assert (
        FormalCognitiveMutationJournal(trusted_db).list_events(
            asset_kind="persona_profile",
        )
        == []
    )
    events = FormalCognitiveMutationJournal.for_database(
        persona_store.signal_store.db_path
    ).list_events(asset_kind="persona_profile")
    assert len(events) == 1
    assert events[0]["target_ref"].startswith("persona-version:1:")
    assert events[0]["decision"].startswith("cogrev-")
    assert events[0]["actor"] == "system"


def test_persona_store_save_creates_backup(persona_store, sample_profile):
    """重复保存应创建历史版本备份。"""
    persona_store.save_persona(sample_profile)

    profile_v2 = PreferenceProfile(
        version=2,
        generated_at="2024-02-01T00:00:00",
        period_start="2024-02-01",
        period_end="2024-02-28",
        energy=EnergyProfile(focus_depth=0.9, confidence=0.7),
        cognitive=CognitiveProfile(abstraction=0.8, confidence=0.6),
        value=ValueProfile(correctness_vs_efficiency=0.8, confidence=0.6),
        signal_count=100,
    )
    persona_store.save_persona(profile_v2)

    backup = persona_store.history_dir / "user-persona-v1.md"
    assert backup.exists()
    content = backup.read_text(encoding="utf-8")
    assert "version: 1" in content


def test_persona_store_load_uses_canonical_db_not_wiki_projection(persona_store, sample_profile):
    """Wiki is derived; the latest canonical Persona row remains authoritative."""
    persona_store.save_persona(sample_profile)

    # 同时写入数据库一个不同版本
    save_persona_version_authorized(
        persona_store.signal_store,
        version=99,
        period_start="2099-01-01",
        period_end="2099-01-31",
        energy={"focus_depth": 0.1},
        cognitive={"abstraction": 0.1},
        value={"correctness_vs_efficiency": 0.1},
        blindspot={},
        signal_count=1,
    )

    loaded, _ = persona_store.load_persona()
    assert loaded.version == 99
    assert loaded.energy.focus_depth == 0.1


def test_persona_store_load_fallback_to_db(persona_store, sample_profile):
    """wiki 文件不存在时应回退到数据库。"""
    # 直接保存到数据库，不创建 wiki 文件
    save_persona_version_authorized(
        persona_store.signal_store,
        version=5,
        period_start="2024-01-01",
        period_end="2024-01-31",
        energy={
            "focus_depth": 0.6,
            "startup_difficulty": 0.5,
            "endurance_mode": 0.5,
            "switching_flexibility": 0.5,
            "recovery_cycle": 0.5,
            "confidence": 0.4,
            "insufficient_dimensions": [],
        },
        cognitive={
            "abstraction": 0.6,
            "system_view": 0.5,
            "skepticism": 0.5,
            "creativity": 0.5,
            "deduction": 0.5,
            "confidence": 0.4,
            "insufficient_dimensions": [],
        },
        value={
            "correctness_vs_efficiency": 0.6,
            "depth_vs_breadth": 0.5,
            "perfection_vs_completion": 0.5,
            "innovation_vs_safety": 0.5,
            "autonomy_vs_collaboration": 0.5,
            "action_vs_analysis": 0.5,
            "confidence": 0.4,
            "insufficient_dimensions": [],
        },
        blindspot={},
        signal_count=20,
    )

    loaded, _ = persona_store.load_persona()
    assert loaded.version == 5
    assert loaded.energy.focus_depth == 0.6


def test_persona_store_load_default_when_empty(persona_store):
    """wiki 和数据库都为空时应返回默认冷启动模板。"""
    loaded, bs = persona_store.load_persona()
    assert loaded is not None
    assert loaded.version == 0
    assert loaded.energy.focus_depth == 0.5
    assert loaded.energy.confidence == 0.0
    assert bs is None


def test_persona_store_load_recent_personas(persona_store):
    """load_recent_personas 应返回最近保存的画像版本，并过滤冷启动模板。"""
    for v in [1, 2]:
        save_persona_version_authorized(
            persona_store.signal_store,
            version=v,
            period_start="2024-01-01",
            period_end="2024-01-31",
            energy={
                "focus_depth": 0.5 + v * 0.1,
                "startup_difficulty": 0.5,
                "endurance_mode": 0.5,
                "switching_flexibility": 0.5,
                "recovery_cycle": 0.5,
                "confidence": 0.4,
                "insufficient_dimensions": [],
            },
            cognitive={
                "abstraction": 0.5,
                "system_view": 0.5,
                "skepticism": 0.5,
                "creativity": 0.5,
                "deduction": 0.5,
                "confidence": 0.4,
                "insufficient_dimensions": [],
            },
            value={
                "correctness_vs_efficiency": 0.5,
                "depth_vs_breadth": 0.5,
                "perfection_vs_completion": 0.5,
                "innovation_vs_safety": 0.5,
                "autonomy_vs_collaboration": 0.5,
                "action_vs_analysis": 0.5,
                "confidence": 0.4,
                "insufficient_dimensions": [],
            },
            blindspot={},
            signal_count=v * 10,
        )

    recent = persona_store.load_recent_personas(limit=2)
    assert len(recent) == 2
    assert [p.version for p in recent] == [2, 1]
    assert recent[0].energy.focus_depth == 0.7


def test_persona_store_generate_and_parse_roundtrip(
    persona_store, sample_profile, sample_blindspot
):
    """_generate_persona_page + _parse_persona_page 应能完整往返。"""
    content = persona_store._generate_persona_page(sample_profile, sample_blindspot)
    assert "type: user-persona" in content
    assert "version: 1" in content
    assert "盲区画像" in content
    assert "已确认的盲区" in content
    assert "挑战统计" in content
    assert "source_count: 42" in content
    assert "signal_store:" in content

    parsed, _ = persona_store._parse_persona_page(content)
    assert parsed.version == 1
    assert parsed.energy.focus_depth == sample_profile.energy.focus_depth
    assert parsed.cognitive.abstraction == sample_profile.cognitive.abstraction


def test_persona_store_parse_invalid_content(persona_store):
    """_parse_persona_page 对无效内容应返回 (None, None)。"""
    result = persona_store._parse_persona_page("no frontmatter here")
    assert result == (None, None)


def test_persona_store_parse_malformed_frontmatter(persona_store):
    """只有 --- 没有有效 YAML 时应回退到默认值。"""
    content = "---\n---\n\n# Hello"
    parsed, _ = persona_store._parse_persona_page(content)
    assert parsed is not None
    assert parsed.version == 0
    assert parsed.energy.focus_depth == 0.5


def test_persona_store_blindspot_to_dict(persona_store, sample_blindspot):
    """_blindspot_to_dict 应正确转换所有字段。"""
    d = persona_store._blindspot_to_dict(sample_blindspot)
    assert d["total_challenges"] == 5
    assert d["accepted_count"] == 2
    assert d["challenge_credit"] == 8.0
    assert len(d["confirmed"]) == 1
    assert d["confirmed"][0]["type"] == "framing"


def test_persona_store_blindspot_to_dict_none(persona_store):
    """_blindspot_to_dict 对 None 应返回空字典。"""
    assert persona_store._blindspot_to_dict(None) == {}


def test_persona_store_backup_current_version(persona_store, sample_profile):
    """_backup_current_version 应将当前版本复制到 history 目录。"""
    persona_store.save_persona(sample_profile)
    persona_store._backup_current_version()

    backup = persona_store.history_dir / "user-persona-v1.md"
    assert backup.exists()


def test_persona_store_backup_when_no_file_exists(persona_store):
    """_backup_current_version 在文件不存在时应静默返回。"""
    # 不应抛出异常
    persona_store._backup_current_version()


def test_persona_store_extract_frontmatter_valid(persona_store):
    """_extract_frontmatter 应正确提取 YAML frontmatter。"""
    content = "---\ntype: test\nlevel: L3\n---\n\n# Hello"
    fm = persona_store._extract_frontmatter(content)
    assert fm == {"type": "test", "level": "L3"}


def test_persona_store_extract_frontmatter_no_fm(persona_store):
    """无 frontmatter 时应返回 None。"""
    content = "# No frontmatter\ncontent"
    assert persona_store._extract_frontmatter(content) is None


def test_persona_store_extract_frontmatter_body_with_separators(persona_store):
    """正文中的 --- 不应被误切分。"""
    content = "---\ntype: test\n---\n\n# Hello\n\n---\nseparator\n---"
    fm = persona_store._extract_frontmatter(content)
    assert fm == {"type": "test"}


def test_persona_store_update_persona_frontmatter_new(persona_store):
    """首次更新应写入 persona_current，不创建 history。"""
    content = "---\ntype: test\n---\n\n# Hello"
    fm = persona_store._extract_frontmatter(content)
    alignment = {
        "preference_match": 0.8,
        "capability_match": 0.7,
        "context_match": 0.6,
        "total": 0.71,
    }
    new_content = persona_store._update_persona_frontmatter(content, fm, alignment, 1)
    assert "persona_current:" in new_content
    assert "persona_history:" not in new_content
    assert "total_alignment: 0.71" in new_content


def test_persona_store_update_persona_frontmatter_with_history(persona_store):
    """已有 persona_current 时应将其移到 history 并标记 superseded。"""
    content = "---\ntype: test\npersona_current:\n  version: 1\n---\n\n# Hello"
    fm = persona_store._extract_frontmatter(content)
    alignment = {
        "preference_match": 0.8,
        "capability_match": 0.7,
        "context_match": 0.6,
        "total": 0.71,
    }
    new_content = persona_store._update_persona_frontmatter(content, fm, alignment, 2)
    assert "persona_history:" in new_content
    assert "status: superseded" in new_content
    assert "superseded_by: 2" in new_content


def test_persona_store_update_persona_frontmatter_no_frontmatter(persona_store):
    """无 frontmatter 的输入应原样返回。"""
    content = "# No frontmatter"
    alignment = {
        "preference_match": 0.5,
        "capability_match": 0.5,
        "context_match": 0.5,
        "total": 0.5,
    }
    result = persona_store._update_persona_frontmatter(content, {}, alignment, 1)
    assert result == content


def test_persona_store_align_all_wiki_pages_updates_frontmatter(persona_store, sample_profile):
    """align_all_wiki_pages 应计算匹配度并更新 frontmatter。"""
    test_page = persona_store.wiki_dir / "test.md"
    test_page.parent.mkdir(parents=True, exist_ok=True)
    test_page.write_text(
        "---\ntype: snippet\nlevel: L3\n---\n\n# Test\ncontent",
        encoding="utf-8",
    )

    stats = persona_store.align_all_wiki_pages(sample_profile)
    assert stats["scanned"] >= 1
    assert stats["updated"] >= 1

    updated = test_page.read_text(encoding="utf-8")
    assert "persona_current:" in updated


def test_persona_store_align_all_dry_run(persona_store, sample_profile):
    """dry_run=True 时不应修改文件。"""
    test_page = persona_store.wiki_dir / "dry_run.md"
    test_page.parent.mkdir(parents=True, exist_ok=True)
    original = "---\ntype: snippet\nlevel: L3\n---\n\n# Test"
    test_page.write_text(original, encoding="utf-8")

    stats = persona_store.align_all_wiki_pages(sample_profile, dry_run=True)
    assert stats["scanned"] >= 1

    after = test_page.read_text(encoding="utf-8")
    assert after == original


def test_persona_store_align_skips_no_frontmatter(persona_store, sample_profile):
    """无 frontmatter 的页面应被计入 skipped。"""
    test_page = persona_store.wiki_dir / "no_fm.md"
    test_page.parent.mkdir(parents=True, exist_ok=True)
    test_page.write_text("# No frontmatter\ncontent", encoding="utf-8")

    stats = persona_store.align_all_wiki_pages(sample_profile)
    assert stats["skipped"] >= 1


def test_persona_store_align_skips_user_persona(persona_store, sample_profile):
    """user-persona.md 本身应被跳过。"""
    persona_store.persona_page.parent.mkdir(parents=True, exist_ok=True)
    persona_store.persona_page.write_text(
        "---\ntype: user-persona\n---\n\n# Persona", encoding="utf-8"
    )

    stats = persona_store.align_all_wiki_pages(sample_profile)
    # user-persona.md 不计入 scanned
    scanned_names = [
        p.name for p in persona_store.wiki_dir.rglob("*.md") if p.name != "user-persona.md"
    ]
    assert stats["scanned"] == len(scanned_names)


def test_persona_store_alignment_never_rewrites_derived_projection(
    persona_store,
    sample_profile,
):
    derived = persona_store.wiki_dir / "L3-Observations" / "attention.md"
    derived.parent.mkdir(parents=True, exist_ok=True)
    original = (
        "---\n"
        "page_role: formal_derived:observation\n"
        "canonical_revision: canonical:attention\n"
        "---\n"
        "# Attention\n"
    )
    derived.write_text(original, encoding="utf-8")

    stats = persona_store.align_all_wiki_pages(sample_profile)

    assert stats["skipped"] >= 1
    assert derived.read_text(encoding="utf-8") == original


def test_persona_store_align_with_session_context(persona_store, sample_profile):
    """提供 session_context 时应计算 context_match。"""
    test_page = persona_store.wiki_dir / "ctx.md"
    test_page.parent.mkdir(parents=True, exist_ok=True)
    test_page.write_text(
        "---\ntype: snippet\nlevel: L3\ntags:\n  - python\n---\n\n# Test",
        encoding="utf-8",
    )

    ctx = {
        "task_type": "coding/python",
        "working_dir": "/proj/python",
        "recent_queries": ["python"],
    }
    stats = persona_store.align_all_wiki_pages(sample_profile, session_context=ctx)
    assert stats["updated"] >= 1

    updated = test_page.read_text(encoding="utf-8")
    assert "context_alignment:" in updated


def test_persona_store_align_no_wiki_dir(mock_signal_store, sample_profile, tmp_path):
    """wiki_dir 不存在时应返回零统计。"""
    from core.persona.delphi import PersonaStore

    store = PersonaStore(wiki_dir=tmp_path / "nonexistent", signal_store=mock_signal_store)
    stats = store.align_all_wiki_pages(sample_profile)
    assert stats == {"scanned": 0, "updated": 0, "skipped": 0}


# ========== 便捷函数 ==========


def test_save_persona_to_wiki_rejects_unguarded_direct_write(monkeypatch, tmp_path, sample_profile):
    """The convenience wrapper cannot bypass the canonical application command."""
    from core.persona.delphi import save_persona_to_wiki

    wiki_dir = tmp_path / "wiki"
    monkeypatch.setattr("core.persona.delphi.WIKI_DIR", wiki_dir)
    signal_store = SignalStore(initialize_schema=True, db_path=tmp_path / "signals.db")
    monkeypatch.setattr(
        "core.persona.delphi.PersonaStore",
        lambda: authorized_persona_store(
            wiki_dir=wiki_dir,
            signal_store=signal_store,
        ),
    )

    with pytest.raises(RuntimeError, match="PersonaApplicationService"):
        save_persona_to_wiki(sample_profile)

    persona_page = wiki_dir / "L5-Feedback" / "user-persona.md"
    assert not persona_page.exists()


def test_align_wiki_with_persona(
    monkeypatch,
    tmp_path,
    sample_profile,
    mock_signal_store,
):
    """align_wiki_with_persona 应返回统计字典。"""
    from core.persona.delphi import align_wiki_with_persona

    wiki_dir = tmp_path / "wiki"
    monkeypatch.setattr("core.persona.delphi.WIKI_DIR", wiki_dir)
    monkeypatch.setattr(
        "core.persona.delphi.get_signal_store",
        lambda: mock_signal_store,
    )

    stats = align_wiki_with_persona(sample_profile, dry_run=True)
    assert "scanned" in stats
    assert "updated" in stats
    assert "skipped" in stats


def test_get_persona_store_singleton(monkeypatch, mock_signal_store):
    """get_persona_store 应返回单例。"""
    from core.persona.delphi import get_persona_store

    monkeypatch.setattr(
        "core.persona.delphi.get_signal_store",
        lambda: mock_signal_store,
    )
    store1 = get_persona_store()
    store2 = get_persona_store()
    assert store1 is store2


# ========== 行为提示词 ==========


def test_ensure_ab_test_group_deterministic():
    """_ensure_ab_test_group 在同一进程中应返回固定值。"""
    from core.persona.delphi import _ensure_ab_test_group
    import core.persona.delphi as _delphi

    _delphi._ab_test_persona_driven = None
    result1 = _ensure_ab_test_group()
    result2 = _ensure_ab_test_group()
    assert result1 == result2
    assert isinstance(result1, bool)


def test_load_base_behavior_prompt_ab_test_control(monkeypatch, patched_get_config, fake_config):
    """A/B 测试对照组应返回空策略提示。"""
    from core.persona.delphi import _load_base_behavior_prompt
    import core.persona.delphi as _delphi

    monkeypatch.setattr(_delphi, "get_config", lambda: fake_config)
    fake_config._values["persona.ab_test_enabled"] = True
    _delphi._ab_test_persona_driven = False

    result = _load_base_behavior_prompt()
    assert "A/B 对照组" in result


def test_load_base_behavior_prompt_ab_test_disabled(
    monkeypatch,
    patched_get_config,
    fake_config,
    mock_signal_store,
):
    """未启用 A/B 测试时应返回正常策略。"""
    from core.persona.delphi import _load_base_behavior_prompt
    import core.persona.delphi as _delphi

    monkeypatch.setattr(_delphi, "get_config", lambda: fake_config)
    monkeypatch.setattr(_delphi, "get_signal_store", lambda: mock_signal_store)
    fake_config._values["persona.ab_test_enabled"] = False
    _delphi._ab_test_persona_driven = None

    result = _load_base_behavior_prompt()
    # 默认冷启动画像会生成一些策略
    assert isinstance(result, str)


def test_load_base_behavior_prompt_with_insufficient_dimensions(
    monkeypatch, tmp_path, sample_profile
):
    """标记为 insufficient 的维度不应生成策略。"""
    from core.persona.delphi import _load_base_behavior_prompt

    # 创建 wiki 文件，包含特定画像
    wiki_dir = tmp_path / "wiki"
    signal_store = SignalStore(initialize_schema=True, db_path=tmp_path / "signals.db")
    store = authorized_persona_store(
        wiki_dir=wiki_dir,
        signal_store=signal_store,
    )
    store.save_persona(sample_profile)

    monkeypatch.setattr("core.persona.delphi.WIKI_DIR", wiki_dir)
    monkeypatch.setattr(
        "core.persona.delphi.get_signal_store",
        lambda: signal_store,
    )

    result = _load_base_behavior_prompt()
    # recovery_cycle 在 insufficient_dimensions 中，不应出现
    assert "恢复周期" not in result
    # creativity 在 insufficient_dimensions 中，不应出现
    assert "创造" not in result


def test_get_behavior_prompt_returns_empty_for_no_profile(monkeypatch):
    """无画像时 get_behavior_prompt 应返回空字符串。"""
    from core.persona.delphi import get_behavior_prompt

    monkeypatch.setattr("core.persona.delphi._load_base_behavior_prompt", lambda: "")
    result = get_behavior_prompt("claude")
    assert result == ""


def test_get_behavior_prompt_with_agent_note(monkeypatch):
    """get_behavior_prompt 应附加 Agent 特定注释。"""
    from core.persona.delphi import get_behavior_prompt

    base = "\n[Persona-Driven Behavior]\n- 测试策略"
    monkeypatch.setattr("core.persona.delphi._load_base_behavior_prompt", lambda: base)

    result = get_behavior_prompt("claude")
    assert "[Agent Note]" in result
    assert "claude" in result.lower() or "Claude" in result


def test_get_behavior_prompt_unknown_agent(monkeypatch):
    """未知 Agent 不应附加特定注释。"""
    from core.persona.delphi import get_behavior_prompt

    base = "\n[Persona-Driven Behavior]\n- 测试策略"
    monkeypatch.setattr("core.persona.delphi._load_base_behavior_prompt", lambda: base)

    result = get_behavior_prompt("unknown_agent")
    assert "[Agent Note]" not in result


def test_get_behavior_prompt_all_known_agents(monkeypatch):
    """所有已知 Agent 都应有特定注释。"""
    from core.persona.delphi import get_behavior_prompt

    base = "\n[Persona-Driven Behavior]\n- 测试策略"
    monkeypatch.setattr("core.persona.delphi._load_base_behavior_prompt", lambda: base)

    agents = ["claude", "hermes", "openclaw", "opencode", "codex"]
    for agent in agents:
        result = get_behavior_prompt(agent)
        assert "[Agent Note]" in result, f"Agent {agent} missing note"


def test_get_behavior_prompt_tracks_usage(monkeypatch, tmp_path):
    """get_behavior_prompt 应记录行为提示使用。"""
    from core.persona.delphi import get_behavior_prompt
    from core.persona.behavior_tracker import BehaviorPromptTracker

    base = "\n[Persona-Driven Behavior]\n- 用户专注深度高：提供结构化回复"
    monkeypatch.setattr("core.persona.delphi._load_base_behavior_prompt", lambda: base)

    db_path = tmp_path / "behavior_signals.db"
    tracker = BehaviorPromptTracker(db_path=db_path)
    monkeypatch.setattr("core.persona.behavior_tracker.BehaviorPromptTracker", lambda: tracker)

    result = get_behavior_prompt("claude")
    assert "专注深度高" in result

    metrics = tracker.get_metrics(days=1)
    assert metrics["total_calls"] == 1
    assert metrics["by_agent"].get("claude") == 1
    assert metrics["by_source"].get("preflight") == 1
    assert "focus_depth_high" in metrics["by_strategy"]


def test_get_ab_test_group_label(monkeypatch, patched_get_config, fake_config):
    """_get_ab_test_group_label 返回正确的 A/B 分组标签。"""
    from core.persona.delphi import _get_ab_test_group_label
    import core.persona.delphi as _delphi

    monkeypatch.setattr(_delphi, "get_config", lambda: fake_config)
    fake_config._values["persona.ab_test_enabled"] = False
    _delphi._ab_test_persona_driven = None
    assert _get_ab_test_group_label() is None

    fake_config._values["persona.ab_test_enabled"] = True
    _delphi._ab_test_persona_driven = True
    assert _get_ab_test_group_label() == "treatment"

    _delphi._ab_test_persona_driven = False
    assert _get_ab_test_group_label() == "control"


# ========== 依赖隔离（config patch） ==========


def test_persona_store_uses_explicit_wiki_dir(
    patched_get_config,
    fake_config,
    mock_signal_store,
):
    """PersonaStore 应使用显式传入的 wiki_dir。"""
    from core.persona.delphi import (
        PERSONA_HISTORY_DIR,
        PERSONA_PAGE_PATH,
        PersonaStore,
    )

    store = PersonaStore(
        wiki_dir=fake_config.wiki_dir,
        signal_store=mock_signal_store,
    )
    assert str(store.wiki_dir) == str(fake_config.wiki_dir)
    assert store.persona_page == fake_config.wiki_dir / "L5-Feedback" / "user-persona.md"
    assert store.history_dir == fake_config.wiki_dir / "L5-Feedback" / "user-persona-history"
    assert Path(PERSONA_PAGE_PATH) == store.persona_page
    assert Path(PERSONA_HISTORY_DIR) == store.history_dir


def test_persona_canonical_replay_reader_is_database_only_and_read_only(
    persona_store,
    mock_signal_store,
    sample_profile,
):
    from core.persona.delphi import PersonaStore

    persona_store.save_persona(sample_profile)
    before = mock_signal_store.db_path.read_bytes()

    profile, blindspot = PersonaStore.load_canonical_persona_read_only(mock_signal_store.db_path)

    assert profile is not None
    assert profile.version == sample_profile.version
    assert blindspot is None
    assert mock_signal_store.db_path.read_bytes() == before


def test_persona_replay_rejects_conflicting_reused_version(
    mock_signal_store,
):
    from core.persona.delphi import PersonaStore

    common = {
        "version": 1,
        "period_start": "2026-07-01",
        "period_end": "2026-07-31",
        "cognitive": {"abstraction": 0.5},
        "value": {"correctness_vs_efficiency": 0.5},
        "blindspot": {},
        "signal_count": 10,
    }
    save_persona_version_authorized(
        mock_signal_store,
        **common,
        energy={"focus_depth": 0.2},
        generated_at="2026-07-20T00:00:00+00:00",
    )
    with pytest.raises(ValueError, match="already belongs to a different Persona"):
        save_persona_version_authorized(
            mock_signal_store,
            **common,
            energy={"focus_depth": 0.9},
            generated_at="2026-07-21T00:00:00+00:00",
        )

    versions = PersonaStore.load_canonical_persona_versions_read_only(mock_signal_store.db_path)

    assert len(versions) == 1
    assert versions[0][0].generated_at == "2026-07-20T00:00:00+00:00"
    assert versions[0][0].energy.focus_depth == 0.2


def test_persona_full_replay_matches_incremental_and_removes_stale_history(
    tmp_path,
    persona_store,
    mock_signal_store,
    sample_profile,
):
    from core.persona.delphi import PersonaStore
    from core.wiki_derived_projection import DerivedProjectionLifecycle
    from core.wiki_projection_lifecycle import WikiProjectionLedger

    persona_store.save_persona(sample_profile)
    profile_v2 = PreferenceProfile(
        version=2,
        generated_at="2024-02-01T00:00:00",
        period_start="2024-02-01",
        period_end="2024-02-28",
        energy=EnergyProfile(focus_depth=0.9, confidence=0.7),
        cognitive=CognitiveProfile(abstraction=0.8, confidence=0.6),
        value=ValueProfile(correctness_vs_efficiency=0.8, confidence=0.6),
        signal_count=100,
    )
    persona_store.save_persona(profile_v2)
    before = mock_signal_store.db_path.read_bytes()

    replay_root = tmp_path / "replayed-wiki"
    lifecycle = DerivedProjectionLifecycle(
        replay_root,
        ledger=WikiProjectionLedger(tmp_path / "replay-projection.db"),
        event_bus=SimpleNamespace(
            publish=lambda event: str(event.trace_id),
        ),
    )
    replay = PersonaStore.for_projection_replay(
        wiki_dir=replay_root,
        canonical_db_path=mock_signal_store.db_path,
        projection_lifecycle=lifecycle,
    )
    stale = replay.history_dir / "user-persona-v999.md"
    stale.write_text("stale", encoding="utf-8")
    independent = replay.history_dir / "manual-notes.md"
    independent.write_text("manual", encoding="utf-8")
    versions = PersonaStore.load_canonical_persona_versions_read_only(mock_signal_store.db_path)

    stats = replay.project_all_personas(versions)

    assert stats == {"current": 1, "history": 1}
    assert not stale.exists()
    assert independent.read_text(encoding="utf-8") == "manual"
    assert replay.persona_page.read_bytes() == persona_store.persona_page.read_bytes()
    assert (replay.history_dir / "user-persona-v1.md").read_bytes() == (
        persona_store.history_dir / "user-persona-v1.md"
    ).read_bytes()
    assert mock_signal_store.db_path.read_bytes() == before
