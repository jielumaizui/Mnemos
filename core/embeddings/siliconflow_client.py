# -*- coding: utf-8 -*-
"""
硅基流动 Embedding 客户端

兼容 OpenAI API 格式，支持：
- Embedding: BAAI/bge-m3
- Rerank: BAAI/bge-reranker-v2-m3（如需）

配置来源（优先级从高到低）：
1. 环境变量 SILICONFLOW_API_KEY / SILICONFLOW_BASE_URL
2. ~/.mnemos/configs/main.json → embedding.api_key / embedding.base_url
3. 默认值
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_MODEL = "BAAI/bge-m3"
DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"


class SiliconFlowEmbeddingClient:
    """硅基流动 Embedding 客户端（OpenAI 兼容格式）

    新增能力：
    - Embedding 缓存（SQLite 持久化）
    - RPM/TPM 限流（滑动窗口）
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        embedding_model: Optional[str] = None,
        rerank_model: Optional[str] = None,
        cache=None,
        limiter=None,
    ):
        self.api_key = api_key or self._resolve_api_key()
        self.base_url = base_url or self._resolve_base_url()
        self.embedding_model = embedding_model or DEFAULT_EMBEDDING_MODEL
        self.rerank_model = rerank_model or DEFAULT_RERANK_MODEL
        self._client = None

        # 缓存与限流（可选注入）
        self._cache = cache
        self._limiter = limiter

    @staticmethod
    def _resolve_api_key() -> Optional[str]:
        """从环境变量或配置文件解析 API Key"""
        # 1. 环境变量
        for env in ("SILICONFLOW_API_KEY", "OPENAI_API_KEY"):
            val = os.environ.get(env)
            if val:
                return val
        # 2. 配置文件
        try:
            from core.config import get_config
            cfg = get_config()
            key = cfg.get("embedding.api_key", "")
            if key:
                return key
            # 回退到 llm.providers.siliconflow.api_key
            providers = cfg.get("llm.providers", {})
            if "siliconflow" in providers:
                return providers["siliconflow"].get("api_key", "")
        except Exception:
            pass
        return None

    @staticmethod
    def _resolve_base_url() -> str:
        """从环境变量或配置文件解析 Base URL"""
        for env in ("SILICONFLOW_BASE_URL", "OPENAI_BASE_URL"):
            val = os.environ.get(env)
            if val:
                return val
        try:
            from core.config import get_config
            cfg = get_config()
            url = cfg.get("embedding.base_url", "")
            if url:
                return url
        except Exception:
            pass
        return DEFAULT_BASE_URL

    def _get_client(self):
        """懒加载 OpenAI 客户端"""
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError:
                raise RuntimeError(
                    "openai package not installed. "
                    "Run: pip install openai"
                )
            if not self.api_key:
                raise RuntimeError(
                    "SiliconFlow API key not configured. "
                    "Set SILICONFLOW_API_KEY env var or embedding.api_key in config."
                )
            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
            )
        return self._client

    def embed(self, texts: List[str], model: Optional[str] = None) -> List[List[float]]:
        """
        批量获取文本 embedding（带缓存 + 限流）

        Args:
            texts: 文本列表（自动过滤空字符串）
            model: 覆盖默认模型

        Returns:
            向量列表，与输入顺序一致
        """
        if not texts:
            return []

        # 过滤空字符串但保留位置（用零向量占位）
        valid_indices = []
        valid_texts = []
        for i, t in enumerate(texts):
            if t and str(t).strip():
                valid_indices.append(i)
                valid_texts.append(str(t))

        if not valid_texts:
            return [[0.0] * 1024 for _ in texts]  # bge-m3 是 1024 维

        model_name = model or self.embedding_model
        dim = 1024  # bge-m3 默认维度
        result = [[0.0] * dim for _ in texts]

        # --- 缓存检查 ---
        uncached_texts = []
        uncached_indices = []
        if self._cache:
            cached_results, missing = self._cache.get_batch(valid_texts, model_name)
            for local_idx, global_idx in enumerate(valid_indices):
                if cached_results[local_idx] is not None:
                    result[global_idx] = cached_results[local_idx]
                    dim = len(cached_results[local_idx])
                else:
                    uncached_texts.append(valid_texts[local_idx])
                    uncached_indices.append(global_idx)
        else:
            uncached_texts = valid_texts
            uncached_indices = valid_indices

        if not uncached_texts:
            return result

        # --- 限流等待 ---
        estimated_tokens = sum(len(t) for t in uncached_texts)
        if self._limiter:
            wait = self._limiter.acquire(estimated_tokens=estimated_tokens)
            if wait > 0:
                logger.debug(f"[Embedding] 限流等待 {wait:.2f}s")
                time.sleep(wait)

        # --- API 调用 ---
        client = self._get_client()
        try:
            resp = client.embeddings.create(
                model=model_name,
                input=uncached_texts,
                encoding_format="float",
            )
            valid_embeddings = [item.embedding for item in resp.data]
            total_tokens = getattr(getattr(resp, "usage", None), "total_tokens", 0) or 0
        except Exception as e:
            logger.warning(f"[Embedding] API 调用失败: {e}")
            raise

        # --- 回填结果 + 写入缓存 ---
        for global_idx, emb in zip(uncached_indices, valid_embeddings):
            result[global_idx] = emb
            dim = len(emb)

        if self._cache:
            try:
                self._cache.set_batch(uncached_texts, valid_embeddings, model_name)
            except Exception as e:
                logger.warning(f"[Embedding] 缓存写入失败: {e}")

        if self._limiter:
            self._limiter.record(actual_tokens=total_tokens or estimated_tokens)

        return result

    def embed_single(self, text: str, model: Optional[str] = None) -> List[float]:
        """单文本 embedding 便捷方法"""
        results = self.embed([text], model=model)
        return results[0] if results else []

    def rerank(
        self,
        query: str,
        documents: List[str],
        model: Optional[str] = None,
        top_n: Optional[int] = None,
    ) -> List[Tuple[int, float]]:
        """
        重排序（带限流）

        Returns:
            [(原始索引, 重排分数), ...] 按分数降序
        """
        if not documents or not query:
            return []

        # --- 限流等待 ---
        estimated_tokens = len(query) + sum(len(d) for d in documents)
        if self._limiter:
            wait = self._limiter.acquire(estimated_tokens=estimated_tokens)
            if wait > 0:
                logger.debug(f"[Rerank] 限流等待 {wait:.2f}s")
                time.sleep(wait)

        try:
            import requests
        except ImportError:
            raise RuntimeError("requests package required for rerank")

        url = f"{self.base_url}/rerank"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model or self.rerank_model,
            "query": query,
            "documents": documents,
            "top_n": top_n or len(documents),
            "return_documents": False,
        }

        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            total_tokens = data.get("usage", {}).get("total_tokens", 0)
            if self._limiter:
                self._limiter.record(actual_tokens=total_tokens or estimated_tokens)
            parsed = []
            for r in results:
                score = r.get("score", r.get("relevance_score", r.get("similarity", 0.0)))
                parsed.append((r["index"], float(score)))
            return parsed
        except Exception as e:
            logger.warning(f"[Rerank] API 调用失败: {e}")
            raise

    def health_check(self) -> Dict[str, any]:
        """快速健康检查：尝试嵌入一个短文本"""
        try:
            start = time.time()
            vec = self.embed_single("test")
            latency_ms = (time.time() - start) * 1000
            return {
                "available": True,
                "latency_ms": round(latency_ms, 1),
                "dimension": len(vec),
                "model": self.embedding_model,
                "base_url": self.base_url,
            }
        except Exception as e:
            return {
                "available": False,
                "error": str(e),
                "model": self.embedding_model,
                "base_url": self.base_url,
            }


# ---- 模块级便捷函数 ----

@lru_cache(maxsize=1)
def get_embedding_client() -> Optional[SiliconFlowEmbeddingClient]:
    """获取全局 Embedding 客户端（单例，懒加载，带缓存 + 限流）"""
    try:
        from .cache import EmbeddingCache
        from .rate_limiter import SiliconFlowRateLimiter
        cache = EmbeddingCache()
        limiter = SiliconFlowRateLimiter()
        return SiliconFlowEmbeddingClient(cache=cache, limiter=limiter)
    except Exception as e:
        logger.debug(f"[Embedding] 客户端初始化失败: {e}")
        return None


def embedding_available() -> bool:
    """检查 embedding 是否可用"""
    client = get_embedding_client()
    if client is None:
        return False
    try:
        hc = client.health_check()
        return hc.get("available", False)
    except Exception:
        return False


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """计算两个向量的余弦相似度"""
    import math
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def text_hash(text: str) -> str:
    """计算文本的短哈希，用于缓存键"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
