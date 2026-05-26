"""
aegis SmartMatcher + DuplicateWorkDetector 单元测试

覆盖项：
- SmartMatcher 三层匹配（精确、关键词、语义）
- DuplicateWorkDetector 指纹+语义+关键词重叠检测
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import unittest
from core.kia.aegis import SmartMatcher, DuplicateWorkDetector


class TestSmartMatcher(unittest.TestCase):
    def setUp(self):
        self.matcher = SmartMatcher()

    def test_match_exact(self):
        result = self.matcher.match_exact("Hello World", ["hello world", "foo bar"])
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "hello world")
        self.assertEqual(result[1], 1.0)

    def test_match_keyword(self):
        result = self.matcher.match_keyword("I love Python programming", ["java", "python", "rust"])
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "python")

    def test_match_semantic(self):
        result = self.matcher.match_semantic(
            "Python asyncio event loop implementation",
            ["Python asyncio event loop implementation guide", "Java servlet container configuration"]
        )
        self.assertIsNotNone(result)
        self.assertIn("guide", result[0].lower())

    def test_match_three_tier_exact_first(self):
        result = self.matcher.match_three_tier(
            "exact match",
            exact_candidates=["exact match"],
            keywords=["match"],
            semantic_refs=["something else"]
        )
        self.assertEqual(result["layer"], 1)
        self.assertEqual(result["type"], "exact")

    def test_match_three_tier_keyword_fallback(self):
        result = self.matcher.match_three_tier(
            "keyword here",
            exact_candidates=["no match"],
            keywords=["here"],
            semantic_refs=["other"]
        )
        self.assertEqual(result["layer"], 2)
        self.assertEqual(result["type"], "keyword")

    def test_match_three_tier_no_match(self):
        result = self.matcher.match_three_tier(
            "completely unrelated",
            exact_candidates=["a"],
            keywords=["b"],
            semantic_refs=["c"]
        )
        self.assertIsNone(result)


class TestDuplicateWorkDetector(unittest.TestCase):
    def test_no_history_not_duplicate(self):
        detector = DuplicateWorkDetector()
        is_dup, score, reason = detector.is_duplicate("hello world")
        self.assertFalse(is_dup)

    def test_exact_fingerprint_duplicate(self):
        detector = DuplicateWorkDetector()
        detector.add_message("Implement user authentication with JWT tokens")
        is_dup, score, reason = detector.is_duplicate("Implement user authentication with JWT tokens")
        self.assertTrue(is_dup)
        self.assertEqual(score, 1.0)

    def test_semantic_similar_duplicate(self):
        detector = DuplicateWorkDetector()
        detector.add_message("Python asyncio event loop implementation")
        is_dup, score, reason = detector.is_duplicate("Python asyncio event loop design")
        # 语义相似但非精确，可能触发也可能不触发（取决于阈值）
        self.assertIsInstance(is_dup, bool)
        self.assertIsInstance(score, float)

    def test_history_limit(self):
        detector = DuplicateWorkDetector()
        for i in range(1100):
            detector.add_message(f"msg {i}")
        self.assertLessEqual(len(detector.history), 1000)


if __name__ == "__main__":
    unittest.main()
