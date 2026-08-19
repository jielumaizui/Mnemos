"""
DuplicateWorkDetector embedding 语义重复检测测试
"""

import math
import pytest
from core.kia.aegis import DuplicateWorkDetector


class FakeEmbeddingClient:
    """基于词袋生成确定性向量的 fake client"""

    DIM = 1024

    @staticmethod
    def _text_to_words(text: str) -> set:
        import re

        words = set(re.findall(r"[a-zA-Z0-9]+", text.lower()))
        words.update(re.findall(r"[\u4e00-\u9fa5]", text))
        return words

    def _vector_for_text(self, text: str) -> list:
        vec = [0.0] * self.DIM
        for w in self._text_to_words(text):
            idx = hash(w) % self.DIM
            vec[idx] += 1.0
        norm = math.sqrt(sum(x * x for x in vec))
        if norm == 0:
            return vec
        return [x / norm for x in vec]

    def embed(self, texts):
        return [self._vector_for_text(t) for t in texts]

    def embed_single(self, text):
        return self.embed([text])[0]


def test_duplicate_detector_embedding_catches_paraphrase(monkeypatch):
    """embedding 可用时，DuplicateWorkDetector 能识别同义重复"""
    client = FakeEmbeddingClient()
    detector = DuplicateWorkDetector(embedding_client=client)

    # Jaccard 快速路径会命中这个高重叠文本，因此构造一个 Jaccard 不命中的同义句
    detector.add_message("deploy the service to production environment")

    # 直接 patch SmartMatcher 的 _embedding_semantic，确保走的是 embedding 路径
    def _fake_semantic(text, references):
        return references[0], 0.78

    monkeypatch.setattr(detector.matcher, "_embedding_semantic", _fake_semantic)

    is_dup, score, reason = detector.is_duplicate("push the application to the live environment")

    assert is_dup is True
    assert score == pytest.approx(0.78, abs=0.01)


def test_duplicate_detector_no_embedding_uses_jaccard():
    """无 embedding client 时仍可用 Jaccard 检测"""
    detector = DuplicateWorkDetector(embedding_client=None)
    detector.add_message("Implement user authentication with JWT tokens")

    is_dup, score, reason = detector.is_duplicate("Implement user authentication with JWT tokens")

    assert is_dup is True
    assert score == 1.0
