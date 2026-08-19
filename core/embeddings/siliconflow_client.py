# -*- coding: utf-8 -*-
"""
硅基流动 Embedding 客户端

兼容 OpenAI API 格式，支持：
- Embedding: BAAI/bge-m3
- Rerank: BAAI/bge-reranker-v2-m3（如需）

配置来源（优先级从高到低）：
1. 环境变量 MNEMOS_EMBEDDING_* / MNEMOS_RERANKER_* / SILICONFLOW_*
2. ~/.mnemos/configs/main.json → embedding.* / reranker.*
3. 默认值
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
import uuid
from functools import lru_cache
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from core.telemetry.prompt_call_log import (
    ModelCallLedger,
    ModelCallLedgerError,
    ModelCallReservation,
    current_model_call_run,
    metered_provider_usage,
)
from core.telemetry.provider_request import (
    ProviderRequestError,
    canonical_provider_input,
    new_non_redirecting_openai_client,
    safe_provider_error_category,
    utf8_token_upper_bound,
)

# Constants extracted from magic numbers
RESP_SECONDS = 30

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
        rerank_api_key: Optional[str] = None,
        rerank_base_url: Optional[str] = None,
        cache=None,
        limiter=None,
        ledger: ModelCallLedger | None = None,
        model_call_run_id: str = "",
        config: Any | None = None,
    ):
        embedding_cfg = self._resolve_embedding_config(config)
        reranker_cfg = self._resolve_reranker_config(config)

        self.api_key = api_key or embedding_cfg.api_key
        self.base_url = base_url or embedding_cfg.base_url or DEFAULT_BASE_URL
        self.embedding_model = embedding_model or embedding_cfg.model or DEFAULT_EMBEDDING_MODEL
        self.embedding_provider = str(getattr(embedding_cfg, "provider", "") or "siliconflow")
        self.rerank_api_key = rerank_api_key or reranker_cfg.api_key or api_key or ""
        self.rerank_base_url = rerank_base_url or reranker_cfg.base_url or base_url or DEFAULT_BASE_URL
        self.rerank_model = rerank_model or reranker_cfg.model or DEFAULT_RERANK_MODEL
        self.rerank_provider = str(getattr(reranker_cfg, "provider", "") or "siliconflow")
        self._client = None
        self._client_transport = None

        # 缓存与限流（可选注入）
        self._cache = cache
        self._limiter = limiter
        self._model_call_ledger = ledger
        self._model_call_run_id = str(model_call_run_id or "")
        self._model_call_subject_scopes: tuple[tuple[str, str], ...] = ()
        self._config = config

    def bind_model_call_run(
        self,
        ledger: ModelCallLedger,
        run_id: str,
        *,
        subject_scopes: Iterable[tuple[str, str]] | None = None,
    ) -> None:
        """Bind an embedding/rerank client to a caller-owned shared run."""
        self._model_call_ledger = ledger
        self._model_call_run_id = ledger.start_run(run_id)
        if subject_scopes is not None:
            self._model_call_subject_scopes = tuple(subject_scopes)

    @staticmethod
    def _root_subject_scope(
        subject_scopes: Iterable[tuple[str, str]] | None,
        *,
        fallback_source: str,
    ) -> tuple[str, str]:
        normalized = tuple(subject_scopes or ())
        if normalized:
            return normalized[0]
        return "source", fallback_source

    def _ledger_for_call(
        self,
        operation: str,
        *,
        subject_scopes: Iterable[tuple[str, str]] | None = None,
    ) -> tuple[ModelCallLedger, str]:
        # A caller-owned binding (for example distillation) always wins.  A
        # process-cached embedding client must never otherwise retain one
        # standalone run forever: unrelated search/scoring requests need
        # separate attribution and cannot share a spent budget.
        if self._model_call_ledger is not None and self._model_call_run_id:
            return self._model_call_ledger, self._model_call_run_id
        scoped = current_model_call_run()
        if scoped is not None:
            return scoped
        ledger = self._model_call_ledger or ModelCallLedger.for_config(self._config)
        self._model_call_ledger = ledger
        fallback_source = (
            "siliconflow_embedding_client"
            if operation == "embedding"
            else "siliconflow_reranker_client"
        )
        return ledger, ledger.start_run(
            f"{operation}:{uuid.uuid4().hex}",
            subject_scope=self._root_subject_scope(
                subject_scopes or self._model_call_subject_scopes,
                fallback_source=fallback_source,
            ),
        )

    @staticmethod
    def _resolve_embedding_config(config: Any | None = None):
        """从统一解析器读取 embedding 配置。"""
        try:
            from core.llm_config import resolve_embedding_api_config

            return resolve_embedding_api_config(config)
        except ImportError:
            logger.debug("[siliconflow_client] embedding config resolver unavailable")
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
            logger.debug("[siliconflow_client] embedding config resolution failed")

        class _Fallback:
            api_key = os.environ.get("SILICONFLOW_API_KEY") or ""
            base_url = os.environ.get("SILICONFLOW_BASE_URL") or DEFAULT_BASE_URL
            model = DEFAULT_EMBEDDING_MODEL

        return _Fallback()

    @staticmethod
    def _resolve_reranker_config(config: Any | None = None):
        """从统一解析器读取 reranker 配置。"""
        try:
            from core.llm_config import resolve_reranker_api_config

            return resolve_reranker_api_config(config)
        except ImportError:
            logger.debug("[siliconflow_client] reranker config resolver unavailable")
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
            logger.debug("[siliconflow_client] reranker config resolution failed")

        class _Fallback:
            api_key = os.environ.get("SILICONFLOW_API_KEY") or ""
            base_url = os.environ.get("SILICONFLOW_BASE_URL") or DEFAULT_BASE_URL
            model = DEFAULT_RERANK_MODEL

        return _Fallback()

    def _get_client(self):
        """懒加载 OpenAI 客户端"""
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError:
                raise RuntimeError("openai package not installed. " "Run: pip install openai")
            if not self.api_key:
                raise RuntimeError(
                    "SiliconFlow API key not configured. "
                    "Set MNEMOS_EMBEDDING_API_KEY/SILICONFLOW_API_KEY env var "
                    "or embedding.api_key in config."
                )
            self._client, self._client_transport = new_non_redirecting_openai_client(
                OpenAI,
                api_key=self.api_key,
                base_url=self.base_url,
            )
        return self._client

    def close(self) -> None:
        """Close the SDK client and the no-redirect transport it owns."""
        client, transport = self._client, self._client_transport
        self._client = None
        self._client_transport = None
        close_client = getattr(client, "close", None)
        if callable(close_client):
            close_client()
        close_transport = getattr(transport, "close", None)
        if callable(close_transport):
            close_transport()

    def _filter_valid_texts(
        self, texts: List[str]
    ) -> tuple[List[int], List[str], List[Optional[List[float]]]]:
        """过滤空字符串并保留原始位置；空输入返回 None 占位。"""
        placeholder_result: List[Optional[List[float]]] = [None for _ in texts]
        valid_indices: List[int] = []
        valid_texts: List[str] = []
        for i, t in enumerate(texts):
            if t and str(t).strip():
                valid_indices.append(i)
                valid_texts.append(str(t))
        return valid_indices, valid_texts, placeholder_result

    def _resolve_cached_embeddings(
        self,
        valid_texts: List[str],
        valid_indices: List[int],
        model_name: str,
        result: List[Optional[List[float]]],
    ) -> tuple[List[str], List[int]]:
        """查询缓存，命中则回填 result；返回未缓存的 (texts, indices)。"""
        if not self._cache:
            return valid_texts, valid_indices

        uncached_texts: List[str] = []
        uncached_indices: List[int] = []
        cached_results, _missing = self._cache.get_batch(valid_texts, model_name)
        for local_idx, global_idx in enumerate(valid_indices):
            cached = cached_results[local_idx]
            if cached is not None:
                result[global_idx] = cached
            else:
                uncached_texts.append(valid_texts[local_idx])
                uncached_indices.append(global_idx)
        return uncached_texts, uncached_indices

    def _iter_embedding_batches_within_tpm(
        self,
        texts: List[str],
        indices: List[int],
    ) -> Iterator[tuple[List[str], List[int]]]:
        """Yield embedding batches that fit the limiter's TPM budget."""
        tpm_limit = getattr(self._limiter, "tpm", None)
        if not self._limiter or not isinstance(tpm_limit, int) or tpm_limit <= 0:
            yield texts, indices
            return

        batch_texts: List[str] = []
        batch_indices: List[int] = []
        batch_tokens = 0
        for text, index in zip(texts, indices):
            # The limiter and the durable reservation must share a conservative
            # input bound.  Character count underestimates UTF-8 CJK input.
            text_tokens = len(str(text).encode("utf-8", "surrogatepass"))
            if text_tokens > tpm_limit:
                raise ValueError(
                    "single embedding input exceeds tpm limit: "
                    f"{text_tokens} > {tpm_limit}"
                )

            if batch_texts and batch_tokens + text_tokens > tpm_limit:
                yield batch_texts, batch_indices
                batch_texts = []
                batch_indices = []
                batch_tokens = 0

            batch_texts.append(text)
            batch_indices.append(index)
            batch_tokens += text_tokens

        if batch_texts:
            yield batch_texts, batch_indices

    def _call_embedding_api(
        self,
        texts: List[str],
        model_name: str,
        *,
        subject_scopes: Iterable[tuple[str, str]] | None = None,
    ) -> tuple[List[List[float]], int]:
        """调用 embedding API 并返回 (embeddings, total_tokens)。"""
        request_payload = {
            "model": model_name,
            "input": texts,
            "encoding_format": "float",
        }
        provider_input = canonical_provider_input(request_payload)
        # The durable ledger reserves the complete canonical provider request
        # below.  The provider TPM limiter, however, is charged against the
        # embedding content token usage reported by the provider rather than
        # JSON field names/framing; keeping that estimate content-scoped also
        # lets the batch splitter make forward progress at small TPM limits.
        rate_estimated_tokens = max(
            1,
            sum(len(str(text).encode("utf-8", "surrogatepass")) for text in texts),
        )
        ledger_input_tokens = utf8_token_upper_bound(provider_input)
        if self._limiter:
            wait = self._limiter.acquire(estimated_tokens=rate_estimated_tokens)
            if wait > 0:
                logger.debug("[Embedding] 限流等待 %.2fs", wait)
                time.sleep(wait)

        client = self._get_client()
        provider_error_type: type[Exception]
        try:
            from openai import OpenAIError
            provider_error_type = OpenAIError
        except ImportError:
            # A test double may supply the client without the optional SDK.
            # It cannot raise an SDK provider error in that configuration.
            provider_error_type = RuntimeError
        reservation: ModelCallReservation | None = None
        entry_subject_scopes = tuple(subject_scopes or self._model_call_subject_scopes)
        try:
            ledger, run_id = self._ledger_for_call(
                "embedding",
                subject_scopes=entry_subject_scopes or None,
            )
            reservation = ledger.reserve(
                run_id=run_id,
                operation="embedding",
                provider=self.embedding_provider,
                model=model_name,
                input_text=provider_input,
                input_tokens=ledger_input_tokens,
                output_tokens=0,
                cache_status="miss",
                subject_scopes=entry_subject_scopes or None,
            )
            reservation.mark_dispatched()
            started = time.perf_counter()
            resp = client.embeddings.create(**request_payload)
            embeddings = [item.embedding for item in resp.data]
            usage = getattr(resp, "usage", None)
            total_tokens = getattr(usage, "total_tokens", 0) or 0
            request_id = str(getattr(resp, "id", "") or "")
            provider_usage = metered_provider_usage(
                usage,
                request_id=request_id,
                output_required=False,
            )
            if provider_usage is None:
                reservation.preserve_incurred(error_code="embedding_provider_usage_missing")
            else:
                reservation.settle(
                    usage=provider_usage,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            return embeddings, total_tokens
        except ModelCallLedgerError:
            # Budget, attribution, and schema/reconciliation failures are
            # local control-plane failures.  They must stay typed and
            # fail-closed instead of being misreported as a provider error.
            if reservation is not None:
                if reservation.dispatched:
                    reservation.preserve_incurred(
                        error_code="embedding_ledger_error_after_dispatch"
                    )
                else:
                    reservation.release(
                        error_code="embedding_ledger_error_before_dispatch"
                    )
            raise
        except (
            provider_error_type,
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
        ) as e:
            if reservation is not None:
                if reservation.dispatched:
                    reservation.preserve_incurred(error_code="embedding_provider_exception")
                else:
                    reservation.release(error_code="embedding_pre_dispatch_exception")
            error_category = safe_provider_error_category(e)
            logger.warning(
                "[Embedding] API call failed: category=%s",
                error_category,
            )
            # Do not re-raise a provider exception: SDK errors frequently
            # include the submitted embedding input or response body.
            raise ProviderRequestError(error_category) from None

    def _persist_embedding_results(
        self,
        texts: List[str],
        embeddings: List[List[float]],
        model_name: str,
        total_tokens: int,
    ) -> None:
        """写入缓存并记录限流 tokens（actual 不可用则使用 estimated）。"""
        rate_estimated_tokens = max(
            1,
            sum(len(str(text).encode("utf-8", "surrogatepass")) for text in texts),
        )
        if self._cache:
            try:
                self._cache.set_batch(texts, embeddings, model_name)
            except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
                logger.warning("[Embedding] 缓存写入失败: category=cache_write_failed")

        if self._limiter:
            self._limiter.record(actual_tokens=total_tokens or rate_estimated_tokens)

    @staticmethod
    def _per_text_subject_scopes(
        texts: Sequence[str],
        subject_scopes: Sequence[Iterable[tuple[str, str]]] | None,
    ) -> tuple[tuple[tuple[str, str], ...], ...]:
        """Validate the immutable subject set corresponding to each input text."""
        if subject_scopes is None:
            return tuple(() for _ in texts)
        if len(subject_scopes) != len(texts):
            raise ValueError("embedding subject scopes must align one-to-one with input texts")
        return tuple(tuple(scopes) for scopes in subject_scopes)

    def embed(
        self,
        texts: List[str],
        model: Optional[str] = None,
        *,
        subject_scopes: Sequence[Iterable[tuple[str, str]]] | None = None,
    ) -> List[List[float]]:
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

        per_text_subject_scopes = self._per_text_subject_scopes(texts, subject_scopes)

        valid_indices, valid_texts, result = self._filter_valid_texts(texts)
        if not valid_texts:
            # [P1-30] 空文本不返回零向量，避免下游 cosine 除零
            return result  # type: ignore[return-value]

        model_name = model or self.embedding_model
        uncached_texts, uncached_indices = self._resolve_cached_embeddings(
            valid_texts, valid_indices, model_name, result
        )
        if not uncached_texts:
            return result  # type: ignore[return-value]

        for batch_texts, batch_indices in self._iter_embedding_batches_within_tpm(
            uncached_texts, uncached_indices
        ):
            batch_subject_scopes = tuple(
                sorted(
                    {
                        scope
                        for index in batch_indices
                        for scope in per_text_subject_scopes[index]
                    }
                )
            )
            embeddings, total_tokens = self._call_embedding_api(
                batch_texts,
                model_name,
                subject_scopes=batch_subject_scopes or None,
            )
            for global_idx, emb in zip(batch_indices, embeddings):
                result[global_idx] = emb

            self._persist_embedding_results(batch_texts, embeddings, model_name, total_tokens)
        return result  # type: ignore[return-value]

    def embed_single(
        self,
        text: str,
        model: Optional[str] = None,
        *,
        subject_scope: tuple[str, str] | None = None,
    ) -> List[float]:
        """单文本 embedding 便捷方法"""
        if text is None or str(text).strip() == "":
            return []
        results = self.embed(
            [text],
            model=model,
            subject_scopes=[(subject_scope,)] if subject_scope is not None else None,
        )
        return results[0] if results else []

    def rerank(
        self,
        query: str,
        documents: List[str],
        model: Optional[str] = None,
        top_n: Optional[int] = None,
        *,
        subject_scopes: Iterable[tuple[str, str]] | None = None,
    ) -> List[Tuple[int, float]]:
        """
        重排序（带限流）

        Returns:
            [(原始索引, 重排分数), ...] 按分数降序
        """
        if not documents or not query:
            return []

        try:
            import requests
        except ImportError:
            raise RuntimeError("requests package required for rerank")

        url = f"{self.rerank_base_url}/rerank"
        headers = {
            "Authorization": f"Bearer {self.rerank_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model or self.rerank_model,
            "query": query,
            "documents": documents,
            "top_n": top_n or len(documents),
            "return_documents": False,
        }
        provider_input = canonical_provider_input(payload)
        rate_estimated_tokens = max(
            1,
            len(str(query).encode("utf-8", "surrogatepass"))
            + sum(len(str(document).encode("utf-8", "surrogatepass")) for document in documents),
        )
        ledger_input_tokens = utf8_token_upper_bound(provider_input)
        if self._limiter:
            wait = self._limiter.acquire(estimated_tokens=rate_estimated_tokens)
            if wait > 0:
                logger.debug("[Rerank] 限流等待 %.2fs", wait)
                time.sleep(wait)

        reservation: ModelCallReservation | None = None
        entry_subject_scopes = tuple(subject_scopes or self._model_call_subject_scopes)
        try:
            ledger, run_id = self._ledger_for_call(
                "rerank",
                subject_scopes=entry_subject_scopes or None,
            )
            reservation = ledger.reserve(
                run_id=run_id,
                operation="rerank",
                provider=self.rerank_provider,
                model=str(payload["model"]),
                input_text=provider_input,
                input_tokens=ledger_input_tokens,
                output_tokens=0,
                cache_status="miss",
                subject_scopes=entry_subject_scopes or None,
            )
            reservation.mark_dispatched()
            started = time.perf_counter()
            resp = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=RESP_SECONDS,
                allow_redirects=False,
            )  # type: ignore[arg-type]
            status_code = getattr(resp, "status_code", None)
            if isinstance(status_code, int) and 300 <= status_code < 400:
                raise requests.HTTPError("rerank provider redirect response rejected")
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            total_tokens = data.get("usage", {}).get("total_tokens", 0)
            usage = data.get("usage")
            request_id = str(
                data.get("request_id")
                or data.get("id")
                or resp.headers.get("x-request-id", "")
                or resp.headers.get("request-id", "")
                or ""
            )
            provider_usage = metered_provider_usage(
                usage,
                request_id=request_id,
                output_required=False,
            )
            if provider_usage is None:
                reservation.preserve_incurred(error_code="rerank_provider_usage_missing")
            else:
                reservation.settle(
                    usage=provider_usage,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            if self._limiter:
                self._limiter.record(actual_tokens=total_tokens or rate_estimated_tokens)
            parsed = []
            for r in results:
                score = r.get("score", r.get("relevance_score", r.get("similarity", 0.0)))
                parsed.append((r["index"], float(score)))
            return parsed
        except ModelCallLedgerError:
            # See the embedding boundary above: a ledger invariant is not a
            # remote provider failure and must retain its typed fail-closed
            # signal for reconciliation/budget handling.
            if reservation is not None:
                if reservation.dispatched:
                    reservation.preserve_incurred(
                        error_code="rerank_ledger_error_after_dispatch"
                    )
                else:
                    reservation.release(
                        error_code="rerank_ledger_error_before_dispatch"
                    )
            raise
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError) as e:
            if reservation is not None:
                if reservation.dispatched:
                    reservation.preserve_incurred(error_code="rerank_provider_exception")
                else:
                    reservation.release(error_code="rerank_pre_dispatch_exception")
            error_category = safe_provider_error_category(e)
            logger.warning(
                "[Rerank] API call failed: category=%s",
                error_category,
            )
            raise ProviderRequestError(error_category) from None

    def health_check(self) -> Dict[str, Any]:
        """Read configuration/cache readiness without invoking a provider.

        Health/status commands are contractually read-only.  A real embedding
        smoke call belongs to an explicit verification workflow where it has a
        durable ledger run and an operator's intent, never in this probe.
        """
        configured = bool(
            str(self.api_key or "").strip()
            and str(self.base_url or "").strip()
            and str(self.embedding_model or "").strip()
        )
        return {
            "available": configured,
            "check_mode": "read_only_config",
            "network_checked": False,
            "model": self.embedding_model,
            "base_url": self.base_url,
            "cache_configured": self._cache is not None,
            "error": "embedding API key not configured" if not configured else "",
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
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
        logger.debug("[Embedding] 客户端初始化失败: category=client_initialization_failed")
        return None


def embedding_available() -> bool:
    """检查 embedding 是否可用"""
    client = get_embedding_client()
    if client is None:
        return False
    try:
        hc = client.health_check()
        return hc.get("available", False)  # type: ignore[no-any-return]
    # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
    except (
        OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError
    ):
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
