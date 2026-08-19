"""
SmartMatcher embedding 语义匹配测试

覆盖：
- embedding client 可用时，同义/近义文本能命中
- embedding 调用失败时回退到 Jaccard
- 高 Jaccard 相似度走快速路径，不调用 embedding
"""

import pytest
import math
from core.kia.aegis import SmartMatcher


class FakeEmbeddingClient:
    """基于词袋生成确定性向量的 fake client"""

    DIM = 1024

    def __init__(self, available=True, raise_on_embed=False):
        self.available = available
        self.raise_on_embed = raise_on_embed

    def health_check(self):
        return {"available": self.available}

    @staticmethod
    def _text_to_words(text: str) -> set:
        """简单分词：字母数字连续段 + 中文字符"""
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
        if self.raise_on_embed:
            raise RuntimeError("embedding service down")
        return [self._vector_for_text(t) for t in texts]

    def embed_single(self, text):
        return self.embed([text])[0]


def test_embedding_semantic_hits_when_jaccard_misses(monkeypatch):
    """
    当本地 Jaccard 未命中时，SmartMatcher 会调用 embedding 语义匹配，
    并将 embedding 命中结果返回。
    """
    client = FakeEmbeddingClient()
    matcher = SmartMatcher(semantic_threshold=0.6, embedding_client=client)

    # Jaccard 不会命中这些文本，但 embedding 会被人为命中
    def _fake_embedding_semantic(text, references):
        return references[0], 0.82

    monkeypatch.setattr(matcher, "_embedding_semantic", _fake_embedding_semantic)

    result = matcher.match_semantic(
        "how to reduce Docker image footprint",
        ["Docker image optimization techniques", "Java servlet container configuration"],
    )

    assert result is not None
    assert "optimization" in result[0].lower()
    assert result[1] == pytest.approx(0.82, abs=0.01)


def test_embedding_failure_falls_back_to_jaccard():
    """embedding 抛异常时应回退到 Jaccard，不崩溃"""
    client = FakeEmbeddingClient(raise_on_embed=True)
    matcher = SmartMatcher(semantic_threshold=0.3, embedding_client=client)

    # 关键词有明显重叠，Jaccard 应能命中
    result = matcher.match_semantic(
        "Python asyncio event loop implementation",
        ["Python asyncio event loop implementation guide", "Java servlet container configuration"],
    )

    assert result is not None
    assert "asyncio" in result[0].lower()


def test_jaccard_shortcut_avoids_embedding_call(monkeypatch):
    """高 Jaccard 命中时不应调用 embedding API"""
    client = FakeEmbeddingClient()
    called = {"n": 0}

    def _counting_embed(texts):
        called["n"] += 1
        return client.embed(texts)

    monkeypatch.setattr(client, "embed", _counting_embed)

    matcher = SmartMatcher(semantic_threshold=0.3, embedding_client=client)

    result = matcher.match_semantic(
        "Python asyncio event loop implementation",
        ["Python asyncio event loop implementation guide"],
    )

    assert result is not None
    assert called["n"] == 0


def test_no_embedding_client_uses_jaccard():
    """无 embedding client 时 SmartMatcher 仍可用"""
    matcher = SmartMatcher(semantic_threshold=0.3, embedding_client=None)

    result = matcher.match_semantic(
        "Python asyncio event loop implementation",
        ["Python asyncio event loop implementation guide"],
    )

    assert result is not None
