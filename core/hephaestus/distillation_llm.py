# -*- coding: utf-8 -*-
"""LLM API caller for the distillation pipeline."""

from __future__ import annotations

import concurrent.futures
import contextlib
import logging
import queue
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, cast

from core.hephaestus.distillation_errors import DistillationAPIError
from core.hephaestus.distillation_json import JsonExtractionResult, extract_json_with_metadata
from core.hephaestus.distillation_metrics import record_json_parse_event
from core.hephaestus.distill_response import DistillBackendResponse
from core.hephaestus.tokenizer import estimate_tokens
from core.llm_config import (
    LLMApiChain,
    LLMApiConfig,
    ProviderRateLimiter,
    estimate_cost,
    get_provider_price,
    resolve_llm_api_chain,
)
from core.telemetry.prompt_call_log import (
    ModelCallBudgetExceeded,
    ModelCallLedger,
    ModelCallReservation,
    ModelCallSubjectFrozen,
    MeteredProviderUsage,
    metered_provider_usage,
)
from core.telemetry.model_call_ledger.normalization import MeteredProviderUsageReceipt
from core.telemetry.provider_request import (
    canonical_chat_input,
    non_redirecting_openai_client,
    safe_provider_error_category,
    utf8_token_upper_bound,
)

logger = logging.getLogger(__name__)

HTTP_API_HOST_AGENT_CALLER_TIMEOUT_SECONDS = 120
RESPONSE_TOKENS = 6000


class HttpApiHostAgentCaller:
    """LLM 调用器 — 主备 API 切换，零降级。

    支持两种调用策略：
    - sequential：按 chain 顺序逐个尝试，保证确定性路由。
    - priority_race：优先使用免费/低成本模型；低成本模型全部被限流时，
      阻塞等待低成本配额的同时并行调用高成本模型，取先返回的成功结果。
    """

    MAX_RETRIES = 2
    TIMEOUT = HTTP_API_HOST_AGENT_CALLER_TIMEOUT_SECONDS  # 长 prompt 蒸馏需要更长时间

    def __init__(
        self,
        timeout: int | None = None,
        api_chain: LLMApiChain | None = None,
        force_provider: str | None = None,
        config_getter: Callable[[], Any] | None = None,
        wiki_db_getter: Callable[[], Path] | None = None,
    ):
        from core.config import get_config

        self._get_config = config_getter or get_config
        self._get_wiki_db = wiki_db_getter or (
            lambda: self._get_config().database_dir / "wiki_state.db"
        )
        if timeout is not None:
            self._timeout = timeout
        else:
            try:
                cfg_timeout = self._get_config().get("llm.timeout")
                self._timeout = int(cfg_timeout) if cfg_timeout else self.TIMEOUT
            except (
                OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, sqlite3.Error,
                subprocess.SubprocessError
            ):
                self._timeout = self.TIMEOUT

        self._api_chain = api_chain or resolve_llm_api_chain()
        self._force_provider = self._normalize_force_provider(force_provider)
        self._rate_limiter = ProviderRateLimiter()

        # 调用策略
        self._routing_strategy = "sequential"
        try:
            strategy = self._get_config().get("llm.routing_strategy", "sequential")
            if strategy in ("sequential", "priority_race"):
                self._routing_strategy = strategy
        except (
            OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, sqlite3.Error,
            subprocess.SubprocessError
        ):
            pass

        self._race_timeout = self._timeout
        try:
            race_timeout = self._get_config().get("llm.race_timeout")
            if race_timeout:
                self._race_timeout = int(race_timeout)
        except (
            OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, sqlite3.Error,
            subprocess.SubprocessError
        ):
            pass

        # 确保 race_timeout 不小于 chain 中任意节点的 timeout，
        # 否则免费模型 80s timeout 还没完成，race 就先放弃了。
        max_chain_timeout = self._timeout
        for cfg in self._api_chain.all_configs:
            if cfg.timeout and cfg.timeout > max_chain_timeout:
                max_chain_timeout = cfg.timeout
        self._race_timeout = max(self._race_timeout, max_chain_timeout)

        # 免费层模型轮询索引
        self._free_index = 0
        self._free_index_lock = threading.Lock()

        # 会话级成本累加器
        self._session_cost_acc = 0.0
        self._session_cost_budget: Optional[float] = None
        self._last_usage: Dict[str, Any] = {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
        self._cost_lock = threading.Lock()
        self._model_call_ledger: ModelCallLedger | None = None
        self._model_call_run_id = ""
        self._model_call_subject_scopes: tuple[tuple[str, str], ...] = ()
        self._model_call_context = threading.local()

    def checkpoint_identity(self) -> Dict[str, Any]:
        """Return the credential-free model/routing identity used for generation."""
        return {
            "caller": f"{type(self).__module__}.{type(self).__qualname__}",
            "api_chain": [
                {
                    "provider": str(cfg.provider or ""),
                    "model": str(cfg.model or ""),
                    "base_url": str(cfg.base_url or ""),
                    "timeout": cfg.timeout,
                    "cost_level": str(cfg.cost_level or ""),
                }
                for cfg in self._api_chain.all_configs
            ],
            "force_provider": self._force_provider,
            "routing_strategy": self._routing_strategy,
            "timeout": self._timeout,
            "race_timeout": self._race_timeout,
        }

    @staticmethod
    def _normalize_force_provider(force_provider: str | None) -> Optional[str]:
        """Return a concrete provider filter, or None for auto/API-chain routing."""
        if force_provider is None:
            return None
        provider = str(force_provider).strip().lower()
        if not provider or provider in {"auto", "api"}:
            return None
        return provider

    def _candidate_configs(self) -> List[LLMApiConfig]:
        """Return API configs allowed by the optional forced provider filter."""
        configs = self._api_chain.all_configs
        if self._force_provider is None:
            return configs
        return [cfg for cfg in configs if cfg.provider.lower() == self._force_provider]

    def reset_session_cost_budget(
        self,
        budget: Optional[float] = None,
        *,
        run_id: str | None = None,
        ledger: ModelCallLedger | None = None,
        subject_scopes: Iterable[tuple[str, str]] | None = None,
    ):
        """重置会话级成本累加器与预算。"""
        self._session_cost_acc = 0.0
        self._session_cost_budget = float(budget) if budget is not None else None
        self._model_call_subject_scopes = tuple(subject_scopes or ())
        caller_owned_ledger = ledger is not None
        selected_ledger = ledger or ModelCallLedger.for_config(self._get_config())
        selected_run_id = run_id or f"distill:{uuid.uuid4().hex}"
        if not caller_owned_ledger or run_id is None:
            selected_ledger.start_run(
                selected_run_id,
                cost_budget=self._session_cost_budget,
                subject_scope=("source", "standalone_distillation_caller"),
            )
        self.bind_model_call_run(
            selected_ledger,
            selected_run_id,
            subject_scopes=self._model_call_subject_scopes,
        )

    def bind_model_call_run(
        self,
        ledger: ModelCallLedger,
        run_id: str,
        *,
        subject_scopes: Iterable[tuple[str, str]] | None = None,
    ) -> None:
        """Bind this provider boundary to the shared durable distillation run."""
        self._model_call_ledger = ledger
        self._model_call_run_id = ledger.start_run(run_id)
        if subject_scopes is not None:
            self._model_call_subject_scopes = tuple(subject_scopes)

    @contextlib.contextmanager
    def model_call_context(self, operation: str):
        """Label a provider request without relying on prompt inspection."""
        previous = getattr(self._model_call_context, "operation", "")
        self._model_call_context.operation = str(operation or "llm")
        try:
            yield
        finally:
            self._model_call_context.operation = previous

    def _model_call_operation(self) -> str:
        return str(getattr(self._model_call_context, "operation", "") or "distill")

    def _ledger_for_call(self) -> tuple[ModelCallLedger, str]:
        ledger = self._model_call_ledger
        if ledger is None:
            ledger = ModelCallLedger.for_config(self._get_config())
            run_id = ledger.start_run(
                f"standalone-distill:{uuid.uuid4().hex}",
                subject_scope=("source", "standalone_distillation_caller"),
            )
            self._model_call_ledger = ledger
            self._model_call_run_id = run_id
        if not self._model_call_run_id:
            self._model_call_run_id = ledger.start_run(
                f"standalone-distill:{uuid.uuid4().hex}",
                subject_scope=("source", "standalone_distillation_caller"),
            )
        return ledger, self._model_call_run_id

    @property
    def session_cost(self) -> float:
        return self._session_cost_acc

    @property
    def budget_exceeded(self) -> bool:
        if self._session_cost_budget is None:
            return False
        return self._session_cost_acc >= self._session_cost_budget

    @property
    def last_usage(self) -> Dict[str, Any]:
        return self._last_usage

    def call(
        self,
        prompt: str,
        expect_json: bool = True,
        max_retries: int | None = None,
        response_max_tokens: int | None = None,
        response_retry_max_tokens: int | None = None,
    ) -> Any:
        """Compatibility projection returning only the parsed provider payload."""

        response = self.call_with_evidence(
            prompt,
            expect_json=expect_json,
            max_retries=max_retries,
            response_max_tokens=response_max_tokens,
            response_retry_max_tokens=response_retry_max_tokens,
        )
        return response.parsed

    def call_with_evidence(
        self,
        prompt: str,
        expect_json: bool = True,
        max_retries: int | None = None,
        response_max_tokens: int | None = None,
        response_retry_max_tokens: int | None = None,
    ) -> DistillBackendResponse:
        """Call the model and preserve raw, routing, usage and parse evidence."""
        if self.budget_exceeded:
            raise DistillationAPIError(
                f"会话 LLM 成本已超出预算 (current={self._session_cost_acc:.6f}, "
                f"budget={self._session_cost_budget:.6f})"
            )

        retries = max_retries if max_retries is not None else self.MAX_RETRIES
        # Routing/rate admission follows the shared tokenizer estimate for the
        # user-visible prompt.  The concrete provider boundary separately
        # reserves the full canonical chat request before dispatch.
        estimated_tokens = estimate_tokens(prompt)

        configs = self._candidate_configs()
        if not configs:
            if self._force_provider is not None:
                raise DistillationAPIError(
                    f"force_provider='{self._force_provider}' 未匹配任何可用 LLM API 配置",
                    self._api_chain.describe(),
                )
            raise DistillationAPIError("没有可用的 LLM API 配置", self._api_chain.describe())

        if self._routing_strategy == "priority_race":
            if response_max_tokens is None and response_retry_max_tokens is None:
                result, usage = self._call_priority_race(
                    configs, prompt, expect_json, retries, estimated_tokens
                )
            else:
                result, usage = self._call_priority_race(
                    configs,
                    prompt,
                    expect_json,
                    retries,
                    estimated_tokens,
                    response_max_tokens,
                    response_retry_max_tokens,
                )
        else:
            if response_max_tokens is None and response_retry_max_tokens is None:
                result, usage = self._call_sequential(
                    configs, prompt, expect_json, retries, estimated_tokens
                )
            else:
                result, usage = self._call_sequential(
                    configs,
                    prompt,
                    expect_json,
                    retries,
                    estimated_tokens,
                    response_max_tokens,
                    response_retry_max_tokens,
                )

        if result is None or not result.successful:
            with self._cost_lock:
                self._last_usage = usage
            self._log_call(prompt, "", False, "", "", 0, usage=usage)
            raise DistillationAPIError(
                "所有 LLM API 均不可用",
                self._api_chain.describe(),
                response_evidence=result,
            )

        with self._cost_lock:
            self._last_usage = usage
            self._session_cost_acc += usage.get("cost", 0.0)

        return result

    def _call_sequential(
        self,
        configs: List[LLMApiConfig],
        prompt: str,
        expect_json: bool,
        retries: int,
        estimated_tokens: int,
        response_max_tokens: int | None = None,
        response_retry_max_tokens: int | None = None,
    ) -> Tuple[DistillBackendResponse, Dict[str, Any]]:
        """顺序 failover：按 chain 顺序逐个尝试。"""
        last_usage: Dict[str, Any] = {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
        prior_attempts: List[Dict[str, Any]] = []
        last_result: DistillBackendResponse | None = None
        for cfg in configs:
            self._rate_limiter.acquire(cfg.provider, cfg.model, estimated_tokens)
            result, usage = self._call_one_config(
                cfg,
                prompt,
                expect_json,
                retries,
                response_max_tokens=response_max_tokens,
                response_retry_max_tokens=response_retry_max_tokens,
            )
            last_usage = usage
            if result is not None and result.successful:
                return result.with_prior_attempts(prior_attempts), usage
            if result is not None:
                last_result = result
                prior_attempts.extend(dict(attempt) for attempt in result.attempt_history)

        if last_result is not None:
            earlier_count = len(prior_attempts) - len(last_result.attempt_history)
            return last_result.with_prior_attempts(prior_attempts[:earlier_count]), last_usage
        return DistillBackendResponse.transport_empty(
            usage=last_usage,
            attempt_history=tuple(prior_attempts),
        ), last_usage

    def _call_priority_race(
        self,
        configs: List[LLMApiConfig],
        prompt: str,
        expect_json: bool,
        retries: int,
        estimated_tokens: int,
        response_max_tokens: int | None = None,
        response_retry_max_tokens: int | None = None,
    ) -> Tuple[DistillBackendResponse, Dict[str, Any]]:
        """优先级并行竞争策略。

        1. 不忙时：只在免费/低成本层内按轮询顺序尝试，不调用付费模型。
        2. 忙碌时：低成本层全部被限流或失败后，启动 free + paid 的并行竞争，
           取先返回的成功结果。
        """
        free_configs, paid_configs = self._group_by_cost(configs)
        prior_attempts: List[Dict[str, Any]] = []

        # 1. 不忙时：优先尝试免费层（轮询）
        if free_configs:
            with self._free_index_lock:
                idx = self._free_index % len(free_configs)
                self._free_index += 1
            ordered_free = free_configs[idx:] + free_configs[:idx]

            for cfg in ordered_free:
                if self._rate_limiter.can_acquire(cfg.provider, cfg.model, estimated_tokens):
                    result, usage = self._call_one_config(
                        cfg,
                        prompt,
                        expect_json,
                        retries,
                        response_max_tokens=response_max_tokens,
                        response_retry_max_tokens=response_retry_max_tokens,
                    )
                    if result is not None and result.successful:
                        return result.with_prior_attempts(prior_attempts), usage
                    if result is not None:
                        prior_attempts.extend(
                            dict(attempt) for attempt in result.attempt_history
                        )

        # 2. 忙碌时：并行竞争（等待低成本 + 立即调用高成本）
        candidates = free_configs + paid_configs
        result, usage = self._race_configs(
            candidates,
            prompt,
            expect_json,
            retries,
            estimated_tokens,
            response_max_tokens,
            response_retry_max_tokens,
        )
        if result is not None and result.successful:
            return result.with_prior_attempts(prior_attempts), usage

        if result is not None:
            return result.with_prior_attempts(prior_attempts), usage
        empty_usage = {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
        return DistillBackendResponse.transport_empty(
            usage=empty_usage,
            attempt_history=tuple(prior_attempts)
            + ({"status": "priority_race_exhausted"},),
        ), empty_usage

    def _group_by_cost(
        self, configs: List[LLMApiConfig]
    ) -> Tuple[List[LLMApiConfig], List[LLMApiConfig]]:
        """按成本将配置分为免费层与付费层。"""
        free: List[LLMApiConfig] = []
        paid: List[LLMApiConfig] = []
        for cfg in configs:
            if cfg.cost_level == "free":
                free.append(cfg)
            elif cfg.cost_level == "paid":
                paid.append(cfg)
            else:
                # 未显式指定时根据价格表推断
                price = get_provider_price(cfg.provider, cfg.model, self._get_config())
                if price.get("input", 0.0) == 0.0 and price.get("output", 0.0) == 0.0:
                    free.append(cfg)
                else:
                    paid.append(cfg)
        return free, paid

    def _race_configs(
        self,
        candidates: List[LLMApiConfig],
        prompt: str,
        expect_json: bool,
        retries: int,
        estimated_tokens: int,
        response_max_tokens: int | None = None,
        response_retry_max_tokens: int | None = None,
    ) -> Tuple[DistillBackendResponse, Dict[str, Any]]:
        """并行调用候选配置，返回第一个成功结果。

        使用 wait 循环：成功时立即返回；所有 worker 都完成且未成功时立即返回 None。
        避免在全部失败时仍等待整个 race_timeout。
        """
        if not candidates:
            empty_usage = {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
            return DistillBackendResponse.transport_empty(
                usage=empty_usage,
                attempt_history=({"status": "no_race_candidates"},),
            ), empty_usage

        result_queue: queue.Queue = queue.Queue()
        cancel_event = threading.Event()
        deadline = time.monotonic() + self._race_timeout

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(len(candidates), 3)
        ) as executor:
            futures = [
                executor.submit(
                    self._race_worker,
                    cfg,
                    prompt,
                    expect_json,
                    retries,
                    estimated_tokens,
                    result_queue,
                    cancel_event,
                    response_max_tokens,
                    response_retry_max_tokens,
                )
                for cfg in candidates
            ]

            remaining = set(futures)
            status, result, usage = "error", None, {"cost": 0.0}
            failures: List[DistillBackendResponse] = []

            while remaining and time.monotonic() < deadline:
                # 非阻塞检查是否有成功结果
                try:
                    status, result, usage, _ = result_queue.get(timeout=0.05)
                    if status == "success":
                        break
                    if isinstance(result, DistillBackendResponse):
                        failures.append(result)
                except queue.Empty:
                    pass

                # 等待至少一个 future 完成
                done, remaining = concurrent.futures.wait(
                    remaining, timeout=max(0.0, deadline - time.monotonic()), return_when="FIRST_COMPLETED"
                )
                # 清理已完成的 future（捕获异常避免日志噪音）
                for fut in done:
                    if not fut.cancelled():
                        fut.exception()

                # 如果所有 future 都已完成，立即退出
                if not remaining:
                    # 再检查一次 queue，可能有成功结果在 wait 期间到达
                    while True:
                        try:
                            queued_status, queued_result, queued_usage, _ = result_queue.get(
                                block=False
                            )
                        except queue.Empty:
                            break
                        if queued_status == "success":
                            status, result, usage = (
                                queued_status,
                                queued_result,
                                queued_usage,
                            )
                            break
                        if isinstance(queued_result, DistillBackendResponse):
                            failures.append(queued_result)
                    break

            cancel_event.set()
            # 给其他线程一点清理时间
            concurrent.futures.wait(futures, timeout=0.2)

        if status == "success" and result is not None:
            return result, usage
        if failures:
            selected = next(
                (item for item in reversed(failures) if item.raw_text),
                failures[-1],
            )
            all_attempts = [
                attempt
                for failure in failures
                for attempt in failure.attempt_history
            ]
            combined = DistillBackendResponse.create(
                raw_text=selected.raw_text,
                parsed=None,
                usage=selected.usage,
                provider=selected.provider,
                model=selected.model,
                request_id=selected.request_id,
                finish_reason=selected.finish_reason,
                parse_path=selected.parse_path,
                attempt_history=all_attempts,
            )
            return combined, dict(selected.usage)
        empty_usage = {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
        return DistillBackendResponse.transport_empty(
            usage=empty_usage,
            attempt_history=({"status": "priority_race_exhausted"},),
        ), empty_usage

    def _race_worker(
        self,
        cfg: LLMApiConfig,
        prompt: str,
        expect_json: bool,
        retries: int,
        estimated_tokens: int,
        result_queue: queue.Queue,
        cancel_event: threading.Event,
        response_max_tokens: int | None = None,
        response_retry_max_tokens: int | None = None,
    ):
        """并行竞争工作线程。"""
        try:
            self._rate_limiter.acquire(cfg.provider, cfg.model, estimated_tokens)

            if cancel_event.is_set():
                return

            result, usage = self._call_one_config(
                cfg,
                prompt,
                expect_json,
                retries,
                response_max_tokens=response_max_tokens,
                response_retry_max_tokens=response_retry_max_tokens,
            )
            if cancel_event.is_set():
                return

            status = "success" if result.successful else "failure"
            result_queue.put((status, result, usage, cfg))
            if result.successful:
                cancel_event.set()
        except (
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
            KeyError,
            sqlite3.Error,
            subprocess.SubprocessError,
        ) as e:
            if not cancel_event.is_set():
                logger.warning(
                    "[Distillation] race worker %s/%s failed: category=%s",
                    cfg.provider,
                    cfg.model,
                    safe_provider_error_category(e),
                )

    def _call_one_config(
        self,
        cfg: LLMApiConfig,
        prompt: str,
        expect_json: bool,
        retries: int,
        response_max_tokens: int | None = None,
        response_retry_max_tokens: int | None = None,
    ) -> Tuple[DistillBackendResponse, Dict[str, Any]]:
        """Call one chain node and retain evidence for every bounded attempt."""
        active_response_max_tokens = response_max_tokens
        last_usage: Dict[str, Any] = {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
        attempt_history: List[Dict[str, Any]] = []
        last_raw = ""
        last_provider = str(cfg.provider or "")
        last_model = str(cfg.model or "")
        last_request_id = ""
        last_finish_reason = ""
        last_parse_path = "transport_empty"
        for attempt in range(retries + 1):
            active_cfg = cfg.active()
            last_provider = str(active_cfg.provider or last_provider)
            last_model = str(active_cfg.model or last_model)
            if not active_cfg.configured:
                attempt_history.append(
                    {
                        "attempt": attempt,
                        "provider": last_provider,
                        "model": last_model,
                        "status": "not_configured",
                        "raw_length": 0,
                    }
                )
                return DistillBackendResponse.transport_empty(
                    usage=last_usage,
                    provider=last_provider,
                    model=last_model,
                    attempt_history=attempt_history,
                ), last_usage

            effective_timeout = active_cfg.timeout or self._timeout
            start = time.perf_counter()
            try:
                previous_retry_attempt = getattr(self._model_call_context, "retry_attempt", 0)
                self._model_call_context.retry_attempt = attempt
                try:
                    if active_response_max_tokens is None:
                        raw, usage = self._try_api_config(prompt, effective_timeout, active_cfg)
                    else:
                        raw, usage = self._try_api_config(
                            prompt,
                            effective_timeout,
                            active_cfg,
                            max_tokens=active_response_max_tokens,
                        )
                finally:
                    self._model_call_context.retry_attempt = previous_retry_attempt
                duration_ms = int((time.perf_counter() - start) * 1000)
                self._settle_provider_usage(usage, duration_ms)
                usage = dict(usage)
                last_usage = usage
                last_request_id = str(usage.get("request_id") or "")
                last_finish_reason = str(usage.get("finish_reason") or "")
                length_truncated = usage.get("finish_reason") == "length"
                retry_length_truncation = False
                if (
                    length_truncated
                    and response_retry_max_tokens is not None
                    and (
                        active_response_max_tokens is None
                        or response_retry_max_tokens > active_response_max_tokens
                    )
                ):
                    active_response_max_tokens = response_retry_max_tokens
                    retry_length_truncation = attempt < retries
                if not raw:
                    attempt_history.append(
                        {
                            "attempt": attempt,
                            "provider": last_provider,
                            "model": last_model,
                            "status": "transport_empty",
                            "duration_ms": duration_ms,
                            "request_id": last_request_id,
                            "finish_reason": last_finish_reason,
                            "raw_length": 0,
                        }
                    )
                    self._log_call(prompt, "", False, active_cfg.provider, active_cfg.model, duration_ms)
                    continue
                last_raw = str(raw)
                if retry_length_truncation:
                    last_parse_path = "length_truncated"
                    attempt_history.append(
                        {
                            "attempt": attempt,
                            "provider": last_provider,
                            "model": last_model,
                            "status": "length_retry",
                            "duration_ms": duration_ms,
                            "request_id": last_request_id,
                            "finish_reason": last_finish_reason,
                            "raw_length": len(last_raw),
                            "response_hash": DistillBackendResponse.hash_raw_text(last_raw),
                        }
                    )
                    self._log_call(
                        prompt,
                        last_raw,
                        False,
                        active_cfg.provider,
                        active_cfg.model,
                        duration_ms,
                        usage=usage,
                    )
                    continue

                if expect_json:
                    extraction = extract_json_with_metadata(last_raw)
                    event_id = self._record_json_parse_metric(
                        extraction,
                        provider=active_cfg.provider,
                        model=active_cfg.model,
                    )
                    usage["json_parse"] = extraction.as_dict()
                    if event_id:
                        usage["json_parse_event_id"] = event_id
                    last_usage = usage
                    last_parse_path = extraction.path
                    if extraction.success:
                        attempt_history.append(
                            {
                                "attempt": attempt,
                                "provider": last_provider,
                                "model": last_model,
                                "status": "success",
                                "duration_ms": duration_ms,
                                "request_id": last_request_id,
                                "finish_reason": last_finish_reason,
                                "parse_path": extraction.path,
                                "raw_length": len(last_raw),
                                "response_hash": DistillBackendResponse.hash_raw_text(last_raw),
                            }
                        )
                        self._log_call(
                            prompt,
                            last_raw,
                            True,
                            active_cfg.provider,
                            active_cfg.model,
                            duration_ms,
                            usage=usage,
                        )
                        cfg.report_success(active_cfg)
                        return DistillBackendResponse.create(
                            raw_text=last_raw,
                            parsed=extraction.data,
                            usage=usage,
                            provider=last_provider,
                            model=last_model,
                            request_id=last_request_id,
                            finish_reason=last_finish_reason,
                            parse_path=extraction.path,
                            attempt_history=attempt_history,
                        ), usage
                    attempt_history.append(
                        {
                            "attempt": attempt,
                            "provider": last_provider,
                            "model": last_model,
                            "status": "parse_failed",
                            "duration_ms": duration_ms,
                            "request_id": last_request_id,
                            "finish_reason": last_finish_reason,
                            "parse_path": extraction.path,
                            "raw_length": len(last_raw),
                            "response_hash": DistillBackendResponse.hash_raw_text(last_raw),
                        }
                    )
                    self._log_call(
                        prompt,
                        last_raw,
                        False,
                        active_cfg.provider,
                        active_cfg.model,
                        duration_ms,
                        usage=usage,
                    )
                    if attempt < retries:
                        continue
                else:
                    last_parse_path = "raw_text"
                    attempt_history.append(
                        {
                            "attempt": attempt,
                            "provider": last_provider,
                            "model": last_model,
                            "status": "success",
                            "duration_ms": duration_ms,
                            "request_id": last_request_id,
                            "finish_reason": last_finish_reason,
                            "parse_path": last_parse_path,
                            "raw_length": len(last_raw),
                            "response_hash": DistillBackendResponse.hash_raw_text(last_raw),
                        }
                    )
                    self._log_call(
                        prompt,
                        last_raw,
                        True,
                        active_cfg.provider,
                        active_cfg.model,
                        duration_ms,
                        usage=usage,
                    )
                    cfg.report_success(active_cfg)
                    return DistillBackendResponse.create(
                        raw_text=last_raw,
                        parsed={"raw": last_raw},
                        usage=usage,
                        provider=last_provider,
                        model=last_model,
                        request_id=last_request_id,
                        finish_reason=last_finish_reason,
                        parse_path=last_parse_path,
                        attempt_history=attempt_history,
                    ), usage
            except subprocess.TimeoutExpired:
                duration_ms = int((time.perf_counter() - start) * 1000)
                attempt_history.append(
                    {
                        "attempt": attempt,
                        "provider": last_provider,
                        "model": last_model,
                        "status": "provider_timeout",
                        "duration_ms": duration_ms,
                        "raw_length": 0,
                    }
                )
                logger.warning(
                    "[%s/%s] provider call failed (attempt %s): category=provider_timeout",
                    active_cfg.provider,
                    active_cfg.model,
                    attempt + 1,
                )
                self._log_call(prompt, "", False, active_cfg.provider, active_cfg.model, duration_ms)
                cfg.report_failure(active_cfg, "provider_timeout")
            except DistillationAPIError:
                # 成本预算超支等控制类异常不再继续重试，直接向上抛
                raise
            except (
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
                KeyError,
                IndexError,
                sqlite3.Error,
                subprocess.SubprocessError,
            ) as e:
                duration_ms = int((time.perf_counter() - start) * 1000)
                error_category = safe_provider_error_category(e)
                attempt_history.append(
                    {
                        "attempt": attempt,
                        "provider": last_provider,
                        "model": last_model,
                        "status": "provider_error",
                        "error_category": error_category,
                        "duration_ms": duration_ms,
                        "raw_length": 0,
                    }
                )
                logger.warning(
                    "[%s/%s] provider call failed (attempt %s): category=%s",
                    active_cfg.provider,
                    active_cfg.model,
                    attempt + 1,
                    error_category,
                )
                self._log_call(prompt, "", False, active_cfg.provider, active_cfg.model, duration_ms)
                cfg.report_failure(active_cfg, error_category)

        return DistillBackendResponse.create(
            raw_text=last_raw,
            parsed=None,
            usage=last_usage,
            provider=last_provider,
            model=last_model,
            request_id=last_request_id,
            finish_reason=last_finish_reason,
            parse_path=last_parse_path,
            attempt_history=attempt_history,
        ), last_usage

    def _try_api_config(
        self,
        prompt: str,
        timeout: int,
        cfg: LLMApiConfig,
        max_tokens: int | None = None,
    ) -> Tuple[Optional[str], Dict[str, Any]]:
        """使用指定的活跃 LLMApiConfig 调用 OpenAI 兼容 API。

        ``cfg`` is expected to be the active key returned by ``node_cfg.active()``.

        Returns:
            (raw_response, usage_dict)
        """
        empty_usage = {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
        if not cfg.configured:
            return None, empty_usage
        try:
            import openai
        except ImportError:
            logger.warning("[Distillation] openai SDK 未安装，无法调用 LLM")
            return None, empty_usage
        openai_error_type = getattr(openai, "OpenAIError", RuntimeError)

        client_kwargs = {"api_key": cfg.api_key}
        if cfg.base_url:
            client_kwargs["base_url"] = cfg.base_url
        messages = [{"role": "user", "content": prompt}]
        provider_input = canonical_chat_input(messages)

        reservation: ModelCallReservation | None = None
        try:
            if max_tokens is None:
                max_tokens = int(
                    self._get_config().get("distill.response_tokens", RESPONSE_TOKENS)
                    or RESPONSE_TOKENS
                )
            else:
                max_tokens = int(max_tokens)
            ledger, run_id = self._ledger_for_call()
            reservation = ledger.reserve(
                run_id=run_id,
                operation=self._model_call_operation(),
                provider=cfg.provider,
                model=cfg.model,
                input_text=provider_input,
                input_tokens=utf8_token_upper_bound(provider_input),
                output_tokens=max_tokens,
                cache_status="miss",
                retry_attempt=max(
                    0,
                    int(getattr(self._model_call_context, "retry_attempt", 0) or 0),
                ),
                subject_scopes=self._model_call_subject_scopes or None,
            )
            reservation.mark_dispatched()
            # 流式输出：避免非流式调用在模型尚未生成完整响应时被 timeout 整体取消。
            # 服务端会逐 token 推送，timeout 作用于整个 HTTP 连接而非单段生成时间。
            content_parts: List[str] = []
            finish_reason: Optional[str] = None
            provider_usage: Any = None
            request_id = ""
            with non_redirecting_openai_client(
                openai.OpenAI, **client_kwargs
            ) as client:  # type: ignore[arg-type]
                response = client.chat.completions.create(
                    model=cfg.model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=0.2,
                    timeout=timeout,
                    stream=True,
                    stream_options={"include_usage": True},
                )
                # The streaming response owns a live HTTP connection.  It
                # must be fully consumed before this helper closes the SDK
                # client and its no-redirect transport.
                for chunk in response:
                    request_id = str(getattr(chunk, "id", "") or request_id)
                    if getattr(chunk, "usage", None) is not None:
                        provider_usage = chunk.usage
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    delta = getattr(choice, "delta", None)
                    if delta and getattr(delta, "content", None):
                        content_parts.append(delta.content)
                    fr = getattr(choice, "finish_reason", None)
                    if fr:
                        finish_reason = fr

            content = "".join(content_parts).strip() if content_parts else None
            if finish_reason == "length":
                logger.warning(
                    "[Distillation] LLM response truncated by max_tokens (%s/%s, max_tokens=%s)",
                    cfg.provider,
                    cfg.model,
                    max_tokens,
                )
            prompt_tokens = int(getattr(provider_usage, "prompt_tokens", 0) or 0)
            completion_tokens = int(getattr(provider_usage, "completion_tokens", 0) or 0)
            if provider_usage is None:
                prompt_tokens = estimate_tokens(prompt)
                completion_tokens = estimate_tokens(content) if content else 0
            cost = estimate_cost(cfg.provider, cfg.model, prompt_tokens, completion_tokens)
            provider_usage_receipt = metered_provider_usage(
                provider_usage,
                request_id=request_id,
                output_required=True,
            )
            return content, {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost": cost,
                "finish_reason": finish_reason,
                "request_id": request_id,
                "response_max_tokens": max_tokens,
                "ledger_entry_id": reservation.entry_id,
                "_ledger_reservation": reservation,
                "_ledger_metered_provider_usage": provider_usage_receipt,
            }
        except ModelCallBudgetExceeded:
            if reservation is not None:
                if reservation.dispatched:
                    reservation.preserve_incurred(error_code="model_call_budget_after_dispatch")
                else:
                    reservation.release(error_code="model_call_budget_before_dispatch")
            # Ledger exception text is not a public error contract.  Keep the
            # caller-visible failure stable and prevent future ledger changes
            # from exposing a request scope or provider-controlled detail.
            raise DistillationAPIError("模型调用预算已耗尽") from None
        except ModelCallSubjectFrozen as exc:
            if reservation is not None:
                if reservation.dispatched:
                    reservation.preserve_incurred(error_code="model_call_subject_frozen_after_dispatch")
                else:
                    reservation.release(error_code="model_call_subject_frozen_before_dispatch")
            raise DistillationAPIError("模型调用主体已冻结，禁止发送给模型提供方") from exc
        except (
            openai_error_type,
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            IndexError,
            sqlite3.Error,
            subprocess.SubprocessError,
        ) as exc:
            if reservation is not None:
                if reservation.dispatched:
                    reservation.preserve_incurred(error_code="provider_exception_after_dispatch")
                else:
                    reservation.release(error_code="provider_exception_before_dispatch")
            logger.warning(
                "[Distillation] LLM API request failed (%s %s): category=%s",
                cfg.provider,
                cfg.model,
                safe_provider_error_category(exc),
            )
            return None, empty_usage

    @staticmethod
    def _settle_provider_usage(usage: Dict[str, Any], latency_ms: int) -> None:
        """Settle exactly once after the outer boundary has measured latency."""
        reservation = usage.pop("_ledger_reservation", None)
        if reservation is None:
            return
        if not isinstance(reservation, ModelCallReservation):
            raise TypeError("ledger reservation has an invalid type")
        provider_usage = usage.pop("_ledger_metered_provider_usage", None)
        if provider_usage is None:
            reservation.preserve_incurred(error_code="provider_usage_missing")
            return
        if not isinstance(provider_usage, MeteredProviderUsage):
            raise TypeError("ledger provider usage receipt has an invalid type")
        reservation.settle(
            usage=cast(MeteredProviderUsageReceipt, provider_usage),
            latency_ms=max(0, int(latency_ms)),
        )

    def _record_json_parse_metric(
        self,
        result: JsonExtractionResult,
        *,
        provider: str,
        model: str,
    ) -> str:
        try:
            cfg = self._get_config()
            return record_json_parse_event(
                cfg.database_dir,
                result,
                provider=provider,
                model=model,
            )
        except (sqlite3.Error, OSError, ValueError, TypeError, AttributeError):
            logger.debug("[Distillation] JSON parse metric write skipped: category=metric_write_failed")
        return ""

    def _log_call(
        self,
        prompt: str,
        response: str,
        success: bool,
        provider: str,
        model: str,
        duration_ms: int = 0,
        usage: Optional[Dict[str, Any]] = None,
    ):
        # The provider boundary has already reserved and settled the durable
        # ledger entry.  Keep this compatibility hook intentionally write-free:
        # a second post-hoc table would reintroduce split accounting.
        del prompt, response, success, provider, model, duration_ms, usage
