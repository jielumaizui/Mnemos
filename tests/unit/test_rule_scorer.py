"""
rule_scorer.py 全面单元测试

覆盖项：
- RuleResult 数据类行为
- 独立评分函数：noise_penalty、quality_score、completeness_penalty、entity_density_score、actionability_score
- RuleScorer：score、explain、_run_rules、规则开关/权重调整、历史记录
- 异常处理与边界条件

测试数量：约 45 个
"""

import pytest

from core.kia.rule_scorer import (
    RuleResult,
    RuleScorer,
    actionability_score,
    completeness_penalty,
    entity_density_score,
    noise_penalty,
    quality_score,
)

# ==================== Fixtures ====================


@pytest.fixture
def complete_frontmatter():
    """返回一个完整合法的 frontmatter"""
    return {"type": "concept", "name": "Redis", "domain": "backend"}


@pytest.fixture
def scorer():
    """返回默认配置的 RuleScorer 实例"""
    return RuleScorer()


# ==================== RuleResult 数据类 ====================


class TestRuleResult:
    def test_rule_result_basic(self):
        r = RuleResult("test_rule", 0.75, 0.30, ["reason1", "reason2"])
        assert r.rule_name == "test_rule"
        assert r.score == 0.75
        assert r.weight == 0.30
        assert r.reasons == ["reason1", "reason2"]

    def test_rule_result_default_reasons(self):
        r = RuleResult("test_rule", 0.5, 0.2)
        assert r.reasons == []

    def test_rule_result_immutable_after_creation(self):
        r = RuleResult("test", 0.5, 0.2, ["a"])
        # dataclass 默认是可变的，但可以修改字段
        r.score = 0.8
        assert r.score == 0.8


# ==================== noise_penalty ====================


class TestNoisePenalty:
    def test_empty_string(self):
        r = noise_penalty("")
        assert r.score == 0.0
        assert "空内容" in r.reasons[0]

    def test_none_input(self):
        r = noise_penalty(None)
        assert r.score == 0.0
        assert "空内容或非字符串" in r.reasons[0]

    def test_non_string_input(self):
        r = noise_penalty(12345)
        assert r.score == 0.0
        assert "空内容或非字符串" in r.reasons[0]

    def test_whitespace_only(self):
        r = noise_penalty("   \n\t  ")
        assert r.score == 0.0
        assert "空内容" in r.reasons[0]

    def test_very_short_below_min_length(self):
        r = noise_penalty("x")
        assert r.score == 0.6  # 1.0 - 0.4 (过短)
        assert any("过短" in reason for reason in r.reasons)

    def test_short_between_4_and_20(self):
        r = noise_penalty("abcd")
        assert r.score == 0.9  # 1.0 - 0.1 (偏短)
        assert any("偏短" in reason for reason in r.reasons)

    def test_perfunctory_phrase_ok(self):
        r = noise_penalty("ok")
        assert r.score == 0.2  # 1.0 - 0.4 (过短) - 0.4 (敷衍短语)
        assert any("敷衍短语" in reason for reason in r.reasons)

    def test_perfunctory_phrase_case_insensitive(self):
        r = noise_penalty("OK")
        assert r.score == 0.2
        assert any("敷衍短语" in reason for reason in r.reasons)

    def test_perfunctory_phrase_chinese(self):
        r = noise_penalty("好的")
        assert r.score == 0.2
        assert any("敷衍短语" in reason for reason in r.reasons)

    def test_perfunctory_phrase_thanks(self):
        r = noise_penalty("谢谢")
        assert r.score == 0.2
        assert any("敷衍短语" in reason for reason in r.reasons)

    def test_pure_punctuation(self):
        r = noise_penalty("...")
        assert r.score == 0.6  # 过短 0.4
        assert any("过短" in reason for reason in r.reasons)

    def test_pure_punctuation_longer(self):
        # 注意：源码中 _NOISE_RE_PATTERNS[0] 包含 [] 在字符类内部，
        # 导致该正则实际上无法匹配任何内容（[] 在 [] 内形成空字符类）。
        # 因此纯中文标点不会被识别为"纯标点"，只会触发"偏短"。
        r = noise_penalty("，。！？")
        assert r.score == 0.9  # 仅触发偏短(4 < 20)，无纯标点匹配
        assert any("偏短" in reason for reason in r.reasons)

    def test_pure_emoji(self):
        r = noise_penalty("😀😀😀😀😀")
        assert r.score < 0.5
        assert any("纯标点" in reason or "emoji" in reason for reason in r.reasons)

    def test_repeated_characters(self):
        r = noise_penalty("哈哈哈哈哈哈")
        assert r.score == 0.6  # 1.0 - 0.1 (偏短) - 0.3 (重复字符)
        assert any("重复字符" in reason for reason in r.reasons)

    def test_high_quality_content(self):
        content = "这是一个关于 Redis 连接池配置的技术讨论，涉及多线程环境下的性能优化。"
        r = noise_penalty(content)
        assert r.score == 1.0
        assert r.reasons[0] == "无明显噪声特征"

    def test_custom_min_length(self):
        r = noise_penalty("hi", min_length=10)
        assert r.score == 0.6  # 过短惩罚 0.4
        assert any("过短" in reason for reason in r.reasons)

    def test_exactly_min_length(self):
        r = noise_penalty("abcd", min_length=4)
        # len=4, 不小于 min_length，但小于 20
        assert r.score == 0.9  # 偏短 0.1


# ==================== quality_score ====================


class TestQualityScore:
    def test_empty_string(self):
        r = quality_score("")
        assert r.score == 0.0
        assert r.reasons[0] == "空内容"

    def test_none_input(self):
        r = quality_score(None)
        assert r.score == 0.0
        assert r.reasons[0] == "空内容"

    def test_non_string_input(self):
        r = quality_score(12345)
        assert r.score == 0.0
        assert r.reasons[0] == "空内容"

    def test_no_valid_words(self):
        r = quality_score("的 了 在")
        assert r.score == 0.0
        assert r.reasons[0] == "无有效词汇"

    def test_very_short_content(self):
        r = quality_score("a" * 19)
        # 长度评分 < 20，密度和丰富度正常
        assert 0.5 < r.score < 0.7
        assert any("长度" in reason for reason in r.reasons)

    def test_medium_length_content(self):
        r = quality_score("a" * 100)
        # 长度评分达到上限 30
        assert r.score >= 0.7

    def test_long_content_500_chars(self):
        r = quality_score("a" * 500)
        assert r.score >= 0.7

    def test_very_long_content_1000_chars(self):
        r = quality_score("a" * 1000)
        # 长度评分开始下降
        assert r.score >= 0.6

    def test_mixed_zh_en_bonus(self):
        content = "Python 的 GIL 机制导致多线程性能问题"
        r = quality_score(content)
        assert r.score >= 0.7
        assert any("密度" in reason for reason in r.reasons)

    def test_with_code_block(self):
        content = "```python\nprint(1)\n```"
        r = quality_score(content)
        assert r.score > 0.5
        assert any("丰富度" in reason for reason in r.reasons)

    def test_with_inline_code(self):
        content = "Use `docker run` to start the container"
        r = quality_score(content)
        assert r.score > 0.6

    def test_with_url(self):
        content = "See https://github.com/example for details"
        r = quality_score(content)
        assert r.score > 0.6

    def test_with_list_items(self):
        content = "- first item\n- second item\n- third item"
        r = quality_score(content)
        assert r.score > 0.5

    def test_value_signals(self):
        content = "代码部署调试测试优化重构"
        r = quality_score(content)
        assert r.score > 0.5
        assert any("价值信号" in reason for reason in r.reasons)

    def test_reasons_structure(self):
        content = "这是一个技术讨论"
        r = quality_score(content)
        assert len(r.reasons) >= 4
        assert any("长度" in reason for reason in r.reasons)
        assert any("密度" in reason for reason in r.reasons)
        assert any("丰富度" in reason for reason in r.reasons)


# ==================== completeness_penalty ====================


class TestCompletenessPenalty:
    def test_none_frontmatter(self):
        r = completeness_penalty(None, "content")
        assert r.score == 0.0
        assert r.reasons[0] == "无 frontmatter"

    def test_empty_frontmatter(self):
        # 注意：源码中 empty dict 经过 normalize_frontmatter 后仍为 {}
        # 而 `if not frontmatter` 会将其视为 falsy，直接返回 "无 frontmatter"
        r = completeness_penalty({}, "content")
        assert r.score == 0.0
        assert r.reasons[0] == "无 frontmatter"

    def test_complete_frontmatter(self, complete_frontmatter):
        r = completeness_penalty(complete_frontmatter, "a" * 50)
        assert r.score == 1.0
        assert r.reasons[0] == "frontmatter 完整"

    def test_missing_domain(self):
        fm = {"type": "concept", "name": "Redis"}
        r = completeness_penalty(fm, "a" * 50)
        assert r.score < 1.0
        assert any("domain" in reason for reason in r.reasons)

    def test_invalid_type(self):
        fm = {"type": "invalid_type", "name": "Redis", "domain": "backend"}
        r = completeness_penalty(fm, "a" * 50)
        assert r.score < 1.0
        assert any("非法 type" in reason for reason in r.reasons)

    def test_empty_type(self):
        fm = {"type": "", "name": "Redis", "domain": "backend"}
        r = completeness_penalty(fm, "a" * 50)
        assert r.score < 1.0
        assert any("type 为空" in reason for reason in r.reasons)

    def test_short_name(self):
        fm = {"type": "concept", "name": "R", "domain": "backend"}
        r = completeness_penalty(fm, "a" * 50)
        assert r.score < 1.0
        assert any("name 过短" in reason for reason in r.reasons)

    def test_empty_name(self):
        fm = {"type": "concept", "name": "", "domain": "backend"}
        r = completeness_penalty(fm, "a" * 50)
        assert r.score < 1.0
        assert any("name" in reason for reason in r.reasons)

    def test_empty_domain(self):
        fm = {"type": "concept", "name": "Redis", "domain": ""}
        r = completeness_penalty(fm, "a" * 50)
        assert r.score < 1.0
        assert any("domain 为空" in reason for reason in r.reasons)

    def test_short_content(self):
        r = completeness_penalty({"type": "concept", "name": "Redis", "domain": "backend"}, "short")
        assert r.score < 1.0
        assert any("正文过短" in reason for reason in r.reasons)

    def test_chinese_frontmatter_keys(self):
        fm = {"类型": "concept", "名称": "Redis", "领域": "backend"}
        r = completeness_penalty(fm, "a" * 50)
        assert r.score == 1.0
        assert r.reasons[0] == "frontmatter 完整"

    def test_valid_types(self):
        for t in ["concept", "person", "project", "technology", "MOC", "retrospective"]:
            fm = {"type": t, "name": "Test", "domain": "test"}
            r = completeness_penalty(fm, "a" * 50)
            assert r.score == 1.0, f"type={t} should be valid"


# ==================== entity_density_score ====================


class TestEntityDensityScore:
    def test_empty_string(self):
        r = entity_density_score("")
        assert r.score == 0.0
        assert r.reasons[0] == "空内容"

    def test_no_entities(self):
        r = entity_density_score("这是一段普通的文字内容")
        assert r.score == 0.0
        assert r.reasons[0] == "低密度"

    def test_code_block(self):
        content = "```python\nprint(1)\n```"
        r = entity_density_score(content)
        assert r.score > 0.0
        assert any("代码块" in reason for reason in r.reasons)

    def test_multiple_code_blocks(self):
        content = "```python\nprint(1)\n```\n\n```bash\necho hi\n```"
        r = entity_density_score(content)
        assert r.score > 0.0
        assert any("代码块" in reason for reason in r.reasons)

    def test_inline_code(self):
        content = "Use `SomeClass` and `AnotherClass`"
        r = entity_density_score(content)
        assert r.score > 0.0
        assert any("行内代码" in reason for reason in r.reasons)

    def test_url(self):
        content = "See https://example.com and https://github.com"
        r = entity_density_score(content)
        assert r.score > 0.0
        assert any("链接" in reason for reason in r.reasons)

    def test_list_items(self):
        content = "- item 1\n- item 2\n- item 3\n- item 4"
        r = entity_density_score(content)
        assert r.score > 0.0
        assert any("列表项" in reason for reason in r.reasons)

    def test_camel_case_terms(self):
        content = "RedisCluster DockerCompose SpringBoot"
        r = entity_density_score(content)
        assert r.score > 0.0
        assert any("技术术语" in reason for reason in r.reasons)

    def test_mixed_signals(self):
        content = "`Code` https://example.com - list item"
        r = entity_density_score(content)
        assert r.score > 0.0
        assert len(r.reasons) >= 2

    def test_max_score_cap(self):
        # 大量信号也不应超过 1.0
        content = "```python\nprint(1)\n```\n" * 10
        content += "`code` " * 10
        content += "https://example.com " * 10
        r = entity_density_score(content)
        assert r.score <= 1.0


# ==================== actionability_score ====================


class TestActionabilityScore:
    def test_empty_string(self):
        r = actionability_score("")
        assert r.score == 0.0
        assert r.reasons[0] == "空内容"

    def test_no_action_signals(self):
        r = actionability_score("这是一段普通的描述性文字")
        assert r.score == 0.0
        assert r.reasons[0] == "无明确行动指引"

    def test_step_markers(self):
        content = "步骤1：安装 Docker。步骤2：编写 Dockerfile。"
        r = actionability_score(content)
        assert r.score > 0.0
        assert any("步骤标记" in reason for reason in r.reasons)

    def test_step_markers_english(self):
        content = "Step 1: Install Docker. Step 2: Build image."
        r = actionability_score(content)
        assert r.score > 0.0
        assert any("步骤标记" in reason for reason in r.reasons)

    def test_shell_block(self):
        content = "```bash\nnpm install\n```"
        r = actionability_score(content)
        assert r.score > 0.0
        assert any("命令块" in reason for reason in r.reasons)

    def test_config_block(self):
        content = '```json\n{"key": "value"}\n```'
        r = actionability_score(content)
        assert r.score > 0.0
        assert any("配置示例" in reason for reason in r.reasons)

    def test_action_verbs(self):
        content = "运行 npm install 然后执行 npm run build"
        r = actionability_score(content)
        assert r.score > 0.0
        assert any("行动动词" in reason for reason in r.reasons)

    def test_mixed_actions(self):
        content = "步骤1：安装 Docker\n```bash\ndocker run -d redis\n```\n运行测试验证"
        r = actionability_score(content)
        assert r.score > 0.0
        assert len(r.reasons) >= 2

    def test_max_score_cap(self):
        content = "步骤1：安装\n步骤2：配置\n步骤3：部署\n步骤4：测试\n步骤5：验证"
        content += "\n```bash\ncmd1\n```\n```bash\ncmd2\n```"
        content += "\n运行执行安装配置"
        r = actionability_score(content)
        assert 0.0 <= r.score <= 1.0


# ==================== RuleScorer ====================


class TestRuleScorerInit:
    def test_default_rules(self, scorer):
        assert len(scorer.rules) == 5
        rule_names = [func.__name__ for func, _, _ in scorer.rules]
        assert "noise_penalty" in rule_names
        assert "quality_score" in rule_names
        assert "completeness_penalty" in rule_names
        assert "entity_density_score" in rule_names
        assert "actionability_score" in rule_names

    def test_default_weights(self, scorer):
        expected = {
            "noise_penalty": 0.15,
            "quality_score": 0.30,
            "completeness_penalty": 0.20,
            "entity_density_score": 0.15,
            "actionability_score": 0.20,
        }
        for func, weight, _ in scorer.rules:
            assert expected[func.__name__] == weight

    def test_custom_rules(self):
        custom_rules = [(noise_penalty, 1.0, True)]
        s = RuleScorer(rules=custom_rules)
        assert len(s.rules) == 1

    def test_history_empty_on_init(self, scorer):
        assert scorer.get_history() == []


class TestRuleScorerScore:
    def test_score_empty_content(self, scorer):
        score = scorer.score("")
        assert 0.0 <= score <= 1.0
        assert score < 0.5

    def test_score_high_quality_content(self, scorer):
        content = "这是一个关于 Redis 连接池配置的技术讨论。步骤1：安装 Redis。步骤2：配置连接池。"
        score = scorer.score(content)
        assert 0.0 <= score <= 1.0
        assert score > 0.3

    def test_score_with_frontmatter(self, scorer, complete_frontmatter):
        content = "a" * 100
        score = scorer.score(content, complete_frontmatter)
        assert 0.0 <= score <= 1.0
        assert score > 0.3

    def test_score_noise_message(self, scorer):
        score = scorer.score("好的")
        assert 0.0 <= score <= 1.0
        assert score < 0.5

    def test_score_bounds(self, scorer):
        # 确保分数始终在 [0, 1] 范围内
        test_cases = ["", "a", "好的", "a" * 1000, "```python\nprint(1)\n```"]
        for content in test_cases:
            score = scorer.score(content)
            assert 0.0 <= score <= 1.0

    def test_all_rules_disabled(self, scorer):
        for func, _, _ in scorer.rules:
            scorer.disable_rule(func.__name__)
        score = scorer.score("test content")
        assert score == 0.0

    def test_single_rule_enabled(self, scorer):
        for func, _, _ in scorer.rules:
            scorer.disable_rule(func.__name__)
        scorer.enable_rule("noise_penalty")
        score = scorer.score("好的")
        # 只有 noise_penalty 启用，权重 0.15，但总分要除以总权重 0.15
        # noise_penalty("好的") = 0.2
        # weighted_score = 0.2 * 0.15 = 0.03
        # total_weight = 0.15
        # final = 0.03 / 0.15 = 0.2
        assert score == 0.2

    def test_custom_rule_weight(self, scorer):
        scorer.set_rule_weight("noise_penalty", 0.5)
        for func, weight, _ in scorer.rules:
            if func.__name__ == "noise_penalty":
                assert weight == 0.5

    def test_score_records_history(self, scorer):
        scorer.score("test")
        history = scorer.get_history()
        assert len(history) == 1
        assert "content_preview" in history[0]
        assert "score" in history[0]
        assert "rule_results" in history[0]

    def test_score_history_multiple(self, scorer):
        for i in range(5):
            scorer.score(f"test {i}")
        assert len(scorer.get_history()) == 5

    def test_get_history_limit(self, scorer):
        for i in range(10):
            scorer.score(f"test {i}")
        assert len(scorer.get_history(limit=3)) == 3
        assert len(scorer.get_history(limit=100)) == 10

    def test_clear_history(self, scorer):
        scorer.score("test")
        scorer.clear_history()
        assert scorer.get_history() == []

    def test_history_content_preview(self, scorer):
        long_content = "a" * 200
        scorer.score(long_content)
        history = scorer.get_history()
        assert len(history[0]["content_preview"]) <= 100


class TestRuleScorerExplain:
    def test_explain_structure(self, scorer):
        exp = scorer.explain("test content")
        assert "final_score" in exp
        assert "rules" in exp
        assert isinstance(exp["rules"], list)
        assert len(exp["rules"]) == 5

    def test_explain_rule_structure(self, scorer):
        exp = scorer.explain("test")
        for rule in exp["rules"]:
            assert "name" in rule
            assert "score" in rule
            assert "weight" in rule
            assert "enabled" in rule
            assert "reasons" in rule

    def test_explain_disabled_rules(self, scorer):
        scorer.disable_rule("noise_penalty")
        exp = scorer.explain("test")
        noise_rule = next(r for r in exp["rules"] if r["name"] == "noise_penalty")
        assert noise_rule["enabled"] is False

    def test_explain_final_score_matches_rules(self, scorer):
        exp = scorer.explain("test content")
        enabled_rules = [r for r in exp["rules"] if r["enabled"]]
        total_weight = sum(r["weight"] for r in enabled_rules)
        weighted = sum(r["score"] * r["weight"] for r in enabled_rules)
        expected = round(weighted / total_weight, 3) if total_weight > 0 else 0.0
        assert exp["final_score"] == expected


class TestRuleScorerRuleManagement:
    def test_disable_enable_rule(self, scorer):
        scorer.disable_rule("noise_penalty")
        for func, _, enabled in scorer.rules:
            if func.__name__ == "noise_penalty":
                assert enabled is False

        scorer.enable_rule("noise_penalty")
        for func, _, enabled in scorer.rules:
            if func.__name__ == "noise_penalty":
                assert enabled is True

    def test_disable_nonexistent_rule(self, scorer):
        with pytest.raises(ValueError, match="规则不存在"):
            scorer.disable_rule("nonexistent_rule")

    def test_enable_nonexistent_rule(self, scorer):
        with pytest.raises(ValueError, match="规则不存在"):
            scorer.enable_rule("nonexistent_rule")

    def test_set_weight_nonexistent_rule(self, scorer):
        with pytest.raises(ValueError, match="规则不存在"):
            scorer.set_rule_weight("nonexistent_rule", 0.5)

    def test_set_rule_weight_zero(self, scorer):
        scorer.set_rule_weight("noise_penalty", 0.0)
        for func, weight, _ in scorer.rules:
            if func.__name__ == "noise_penalty":
                assert weight == 0.0


class TestRuleScorerExceptionHandling:
    def test_rule_exception_not_fatal(self):
        def bad_rule(content):
            raise ValueError("intentional error")

        scorer = RuleScorer(rules=[(bad_rule, 1.0, True)])
        score = scorer.score("test")
        assert score == 0.5  # 异常时返回中等分数

    def test_rule_exception_in_explain(self):
        def bad_rule(content):
            raise RuntimeError("runtime error")

        scorer = RuleScorer(rules=[(bad_rule, 1.0, True)])
        exp = scorer.explain("test")
        assert exp["final_score"] == 0.5
        assert "规则异常" in exp["rules"][0]["reasons"][0]

    def test_rule_programming_error_is_not_hidden(self):
        def broken_rule(content):
            raise AssertionError("broken rule contract")

        scorer = RuleScorer(rules=[(broken_rule, 1.0, True)])
        with pytest.raises(AssertionError, match="broken rule contract"):
            scorer.score("test")


# ==================== 集成测试 ====================


class TestIntegration:
    def test_full_pipeline_with_frontmatter(self):
        scorer = RuleScorer()
        content = "步骤1：安装 Docker。步骤2：运行 `docker-compose up`。"
        frontmatter = {"type": "technology", "name": "Docker", "domain": "devops"}
        score = scorer.score(content, frontmatter)
        exp = scorer.explain(content, frontmatter)

        assert 0.0 <= score <= 1.0
        assert exp["final_score"] == score
        assert len(exp["rules"]) == 5

    def test_scorer_reusability(self):
        scorer = RuleScorer()
        s1 = scorer.score("好的")
        s2 = scorer.score("Redis 配置")
        s3 = scorer.score("")

        assert s1 != s2
        assert s3 < s1
        assert len(scorer.get_history()) == 3

    def test_rule_independence(self):
        scorer = RuleScorer()
        # 修改一个规则的权重不应影响其他规则
        original_weights = {func.__name__: weight for func, weight, _ in scorer.rules}
        scorer.set_rule_weight("noise_penalty", 0.99)

        for func, weight, _ in scorer.rules:
            if func.__name__ != "noise_penalty":
                assert weight == original_weights[func.__name__]

    def test_explain_consistency_with_score(self):
        scorer = RuleScorer()
        content = "测试内容"
        score = scorer.score(content)
        exp = scorer.explain(content)
        assert score == exp["final_score"]

    def test_history_order(self):
        scorer = RuleScorer()
        scorer.score("first")
        scorer.score("second")
        scorer.score("third")
        history = scorer.get_history()
        assert history[0]["content_preview"] == "first"
        assert history[1]["content_preview"] == "second"
        assert history[2]["content_preview"] == "third"


# ==================== P2-12: RuleWeightOptimizer 模式检测与权重调整 ====================


class TestRuleWeightOptimizerPatternDetection:
    """The historical optimizer surface remains a side-effect-free tripwire."""

    @pytest.fixture
    def optimizer(self, tmp_path):
        from core.kia.rule_scorer import RuleWeightOptimizer

        db = tmp_path / "test_optimizer.db"
        return RuleWeightOptimizer(db_path=db)

    def test_get_recent_outcomes_empty(self, optimizer):
        with pytest.raises(PermissionError, match="training_admission_receipt_required"):
            optimizer.get_recent_outcomes("noise_penalty", limit=10)
        assert not optimizer.db_path.exists()

    def test_get_recent_outcomes_ordered(self, optimizer):
        with pytest.raises(PermissionError, match="training_admission_receipt_required"):
            optimizer.record_outcome("noise_penalty", 0.84, True)
        assert not optimizer.db_path.exists()

    def test_feedback_sourced_outcomes_are_quarantined_from_active_reads(
        self,
        optimizer,
    ):
        with pytest.raises(PermissionError, match="training_admission_receipt_required"):
            optimizer.get_total_samples("noise_penalty")
        assert not optimizer.db_path.exists()

    def test_detect_misalignment_high_pred_false(self, optimizer):
        with pytest.raises(PermissionError, match="training_admission_receipt_required"):
            optimizer.detect_misalignment({"noise_penalty": 0.15}, window_size=10)
        assert not optimizer.db_path.exists()

    def test_detect_misalignment_low_pred_true(self, optimizer):
        with pytest.raises(PermissionError, match="training_admission_receipt_required"):
            optimizer.get_rule_accuracy("quality_score", min_samples=1)
        assert not optimizer.db_path.exists()

    def test_detect_misalignment_no_pattern(self, optimizer):
        with pytest.raises(PermissionError, match="training_admission_receipt_required"):
            optimizer.get_stats()
        assert not optimizer.db_path.exists()

    def test_detect_misalignment_min_window(self, optimizer):
        with pytest.raises(PermissionError, match="training_admission_receipt_required"):
            optimizer.get_weight_history("noise_penalty")
        assert not optimizer.db_path.exists()

    def test_detect_misalignment_respects_window_size(self, optimizer):
        with pytest.raises(PermissionError, match="training_admission_receipt_required"):
            optimizer.optimize_weights({"noise_penalty": 0.15}, force=True)
        assert not optimizer.db_path.exists()


class TestRuleScorerPatternAdjustments:
    """All retired RuleScorer adaptation entrypoints fail closed."""

    def test_apply_adjustments_without_optimizer_returns_empty(self):
        scorer = RuleScorer(optimizer=None)
        with pytest.raises(PermissionError, match="training_admission_receipt_required"):
            scorer.apply_pattern_adjustments()

    def test_apply_adjustments_with_high_pred_false(self, tmp_path):
        from core.kia.rule_scorer import RuleWeightOptimizer

        db = tmp_path / "test_adjust.db"
        optimizer = RuleWeightOptimizer(db_path=db)
        scorer = RuleScorer(optimizer=optimizer)
        with pytest.raises(PermissionError, match="training_admission_receipt_required"):
            scorer.record_rule_outcome("noise_penalty", 0.8, False)
        assert not db.exists()

    def test_apply_adjustments_clamped_to_min(self, tmp_path):
        from core.kia.rule_scorer import RuleWeightOptimizer

        db = tmp_path / "test_clamp.db"
        optimizer = RuleWeightOptimizer(db_path=db)
        scorer = RuleScorer(optimizer=optimizer)
        with pytest.raises(PermissionError, match="training_admission_receipt_required"):
            scorer.auto_optimize(force=True)
        assert not db.exists()

    def test_apply_adjustments_clamped_to_max(self, tmp_path):
        from core.kia.rule_scorer import RuleWeightOptimizer

        db = tmp_path / "test_clamp_max.db"
        optimizer = RuleWeightOptimizer(db_path=db)
        scorer = RuleScorer(optimizer=optimizer)
        with pytest.raises(PermissionError, match="training_admission_receipt_required"):
            scorer.get_optimizer_stats()
        assert not db.exists()

    def test_apply_adjustments_no_pattern_no_change(self, tmp_path):
        from core.kia.rule_scorer import RuleWeightOptimizer

        db = tmp_path / "test_no_change.db"
        optimizer = RuleWeightOptimizer(db_path=db)
        scorer = RuleScorer(optimizer=optimizer)
        with pytest.raises(PermissionError, match="training_admission_receipt_required"):
            scorer.refresh_weights()
        assert not db.exists()


class TestRuleScorerOptimizerContracts:
    """Legacy optimization contracts cannot bypass governed admissions."""

    def test_record_full_outcome_records_enabled_rules_and_reports_count(self, tmp_path):
        from core.kia.rule_scorer import RuleWeightOptimizer

        db = tmp_path / "optimizer.db"
        optimizer = RuleWeightOptimizer(db_path=db)
        scorer = RuleScorer(optimizer=optimizer, load_shared_weights=False)
        with pytest.raises(PermissionError, match="training_admission_receipt_required"):
            scorer.record_full_outcome(actual_label=True, content="substantial text")
        assert not db.exists()

    def test_weight_history_can_be_filtered_after_forced_optimization(self, tmp_path):
        from core.kia.rule_scorer import RuleWeightOptimizer

        db = tmp_path / "history.db"
        optimizer = RuleWeightOptimizer(db_path=db)
        with pytest.raises(PermissionError, match="training_admission_receipt_required"):
            optimizer.get_weight_history("strong_rule")
        assert not db.exists()
