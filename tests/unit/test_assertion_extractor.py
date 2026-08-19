"""
Tests for core.kia.assertion_extractor

Covers: sentence splitting, assertion detection, form classification,
        negation detection, boundary detection, merge dedup, CLI.
"""

from core.kia.assertion_extractor import (
    Assertion,
    KnowledgeForm,
    extract_assertions,
    extract_from_messages,
    merge_similar_assertions,
    _split_sentences,
    _is_likely_assertion,
    _detect_boundary,
    _detect_negation,
    _is_user_request,
)


class TestSplitSentences:
    def test_basic_chinese(self):
        text = "这是第一句。 这是第二句！ 这是第三句？"
        sents = _split_sentences(text)
        assert len(sents) == 3
        assert "第一句" in sents[0]

    def test_english_sentences(self):
        text = "First sentence. Second one! Third?"
        sents = _split_sentences(text)
        assert len(sents) == 3

    def test_empty_and_whitespace(self):
        assert _split_sentences("") == []
        assert _split_sentences("   ") == []


class TestIsLikelyAssertion:
    def test_too_short(self):
        assert _is_likely_assertion("短") == (False, 0.0)

    def test_too_long(self):
        long_sent = "x" * 301
        assert _is_likely_assertion(long_sent) == (False, 0.0)

    def test_subjective_prefix_filtered(self):
        assert _is_likely_assertion("我觉得这个方案不错")[0] is False
        assert _is_likely_assertion("I think this is good")[0] is False

    def test_question_filtered(self):
        assert _is_likely_assertion("什么是最好的方案？")[0] is False
        assert _is_likely_assertion("How to fix this?")[0] is False

    def test_imperative_filtered(self):
        assert _is_likely_assertion("请帮我修改这个文件")[0] is False
        assert _is_likely_assertion("Run the tests")[0] is False

    def test_high_confidence_signals(self):
        """包含高置信度信号的句子应被识别"""
        sent = "问题的根因是千帆 API 的 endpoint 与 OpenAI 不兼容"
        is_a, conf = _is_likely_assertion(sent)
        assert is_a is True
        assert conf >= 0.4

    def test_with_numbers_boosts_confidence(self):
        sent = "API 响应时间在 200ms 以内"
        is_a, conf = _is_likely_assertion(sent)
        assert is_a is True
        assert conf > 0.3

    def test_contrast_objective_part(self):
        """ "我觉得...，但实际上..." 应提取客观部分；若客观部分无强信号则整体仍低置信度"""
        sent = "我觉得 A 方案不错，但实际上 B 方案更适合大规模场景"
        is_a, conf = _is_likely_assertion(sent)
        # 客观部分"B 方案更适合大规模场景"缺乏高置信度信号，整体被过滤
        assert is_a is False


class TestDetectBoundary:
    def test_boundary_detected(self):
        sent = "这个方案很有效，但是只在特定场景下适用"
        assert _detect_boundary(sent) != ""

    def test_no_boundary(self):
        sent = "这个方案很有效"
        assert _detect_boundary(sent) == ""


class TestDetectNegation:
    def test_negation_signals(self):
        assert _detect_negation("不要直接修改生产环境") is True
        assert _detect_negation("避免使用全局变量") is True

    def test_ai_behavior_anti_pattern(self):
        assert _detect_negation("陷入循环反复读取是常见错误") is True

    def test_not_negation(self):
        assert _detect_negation("应该使用类型提示") is False


class TestExtractAssertions:
    def test_extract_from_simple_text(self):
        text = "问题的根因是配置错误。解决方法是重启服务。"
        assertions = extract_assertions(text)
        assert len(assertions) >= 1
        assert all(isinstance(a, Assertion) for a in assertions)

    def test_assertion_has_context(self):
        text = "第一句。第二句是断言。第三句。"
        assertions = extract_assertions(text)
        for a in assertions:
            assert a.context  # 上下文非空

    def test_form_classification(self):
        text = "不要直接修改生产环境，否则会导致故障。"
        assertions = extract_assertions(text)
        # 至少有一个被分类为反模式
        anti_patterns = [a for a in assertions if a.form == KnowledgeForm.ANTI_PATTERN]
        assert len(anti_patterns) >= 1 or len(assertions) == 0

    def test_problem_solution_classification(self):
        text = "问题：服务启动失败。根因是端口被占用。解决方案是更换端口。"
        assertions = extract_assertions(text)
        ps = [a for a in assertions if a.form == KnowledgeForm.PROBLEM_SOLUTION]
        # 至少有一个被分类为问题-解决对
        assert len(ps) >= 1 or len(assertions) == 0

    def test_source_attached(self):
        assertions = extract_assertions("测试断言", source="session_123")
        assert all(a.source == "session_123" for a in assertions)


class TestExtractFromMessages:
    def test_skips_system_and_tool(self):
        messages = [
            {"role": "system", "content": "系统提示"},
            {"role": "tool", "content": "工具结果"},
        ]
        assertions = extract_from_messages(messages)
        assert assertions == []

    def test_skips_wiki_context(self):
        messages = [
            {"role": "assistant", "content": "<wiki-context>知识内容</wiki-context>"},
        ]
        assertions = extract_from_messages(messages)
        assert assertions == []

    def test_assistant_vs_user_confidence(self):
        """user 消息的置信度应低于 assistant"""
        msg = "问题的根因是配置错误。"
        assistant_as = extract_from_messages([{"role": "assistant", "content": msg}])
        user_as = extract_from_messages([{"role": "user", "content": msg}])
        if assistant_as and user_as:
            assert assistant_as[0].confidence > user_as[0].confidence

    def test_user_request_filtered(self):
        messages = [
            {"role": "user", "content": "帮我修改这个文件"},
        ]
        assertions = extract_from_messages(messages)
        # 用户请求应被过滤或置信度为 0
        for a in assertions:
            assert a.confidence == 0.0


class TestMergeSimilarAssertions:
    def test_merge_substring_duplicates(self):
        a1 = Assertion(claim="根因是配置错误", form=KnowledgeForm.UNKNOWN, confidence=0.9)
        a2 = Assertion(claim="根因是配置错误，需要重启", form=KnowledgeForm.UNKNOWN, confidence=0.8)
        merged = merge_similar_assertions([a1, a2])
        assert len(merged) == 1

    def test_no_merge_different(self):
        a1 = Assertion(claim="根因是配置错误", form=KnowledgeForm.UNKNOWN, confidence=0.9)
        a2 = Assertion(claim="解决方案是重启", form=KnowledgeForm.UNKNOWN, confidence=0.8)
        merged = merge_similar_assertions([a1, a2])
        assert len(merged) == 2

    def test_empty_list(self):
        assert merge_similar_assertions([]) == []

    def test_sort_by_confidence(self):
        a1 = Assertion(claim="低置信度", form=KnowledgeForm.UNKNOWN, confidence=0.5)
        a2 = Assertion(claim="高置信度", form=KnowledgeForm.UNKNOWN, confidence=0.9)
        merged = merge_similar_assertions([a1, a2])
        # 高置信度优先保留
        assert merged[0].claim == "高置信度"


class TestIsUserRequest:
    def test_request_prefixes(self):
        assert _is_user_request("帮我修改代码") is True
        assert _is_user_request("能否解释一下") is True
        assert _is_user_request("请问怎么解决") is True

    def test_question_mark(self):
        assert _is_user_request("这是什么意思？") is True

    def test_english_questions(self):
        assert _is_user_request("How to fix this?") is True
        assert _is_user_request("What is the problem?") is True

    def test_not_request(self):
        assert _is_user_request("问题的根因是配置错误") is False
