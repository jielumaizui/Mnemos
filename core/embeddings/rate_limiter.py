# -*- coding: utf-8 -*-
"""
硅基流动 API 限流器

免费额度限制：
    - RPM (Requests Per Minute): 2,000
    - TPM (Tokens Per Minute): 500,000

实现：滑动窗口计数器（比令牌桶更简单，足够用于 Embedding/Rerank 场景）
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

# Constants extracted from magic numbers
TPM = 500000

logger = logging.getLogger(__name__)


class SiliconFlowRateLimiter:
    """
    双层限流：RPM + TPM，滑动窗口实现。

    Args:
        rpm: 每分钟最大请求数
        tpm: 每分钟最大 token 数
        window_sec: 滑动窗口长度（默认 60 秒）
    """

    def __init__(self, rpm: int = 2000, tpm: int = TPM, window_sec: float = 60.0):
        self.rpm = rpm
        self.tpm = tpm
        self.window_sec = window_sec

        # 请求时间戳队列
        self._request_times: deque[float] = deque()
        # token 消耗队列: (timestamp, tokens)
        self._token_records: deque[tuple[float, int]] = deque()

        self._lock = threading.Lock()

    def _validate_estimated_tokens(self, estimated_tokens: int) -> None:
        if estimated_tokens > self.tpm:
            raise ValueError(
                f"estimated_tokens exceeds tpm limit: {estimated_tokens} > {self.tpm}"
            )

    def _prune(self) -> None:
        """清理窗口外的记录"""
        now = time.time()
        cutoff = now - self.window_sec

        while self._request_times and self._request_times[0] < cutoff:
            self._request_times.popleft()

        while self._token_records and self._token_records[0][0] < cutoff:
            self._token_records.popleft()

    def acquire(self, estimated_tokens: int = 1000) -> float:
        """
        请求许可，返回需要等待的秒数（0 表示立即可执行）。

        acquire 是线程安全查询；调用方不需要也不应该访问私有锁。
        请求完成后调用 record() 记录实际消耗，record() 自身也是线程安全的。

        Args:
            estimated_tokens: 预估本次请求消耗的 tokens

        Returns:
            wait_time: 需要等待的秒数
        """
        self._validate_estimated_tokens(estimated_tokens)
        with self._lock:
            self._prune()
            now = time.time()
            wait_time = 0.0

            # RPM 检查
            if len(self._request_times) >= self.rpm:
                oldest = self._request_times[0]
                wait_time = max(wait_time, (oldest + self.window_sec) - now)

            # TPM 检查
            current_tpm = sum(tokens for _, tokens in self._token_records)
            if current_tpm + estimated_tokens > self.tpm:
                # 需要等待直到 oldest token 记录过期
                if self._token_records:
                    oldest_ts = self._token_records[0][0]
                    wait_time = max(wait_time, (oldest_ts + self.window_sec) - now)

            if wait_time > 0:
                logger.debug(
                    "[RateLimiter] 需要等待 %.2fs (requests=%s/%s, tokens=%s/%s)",
                    wait_time,
                    len(self._request_times),
                    self.rpm,
                    current_tpm,
                    self.tpm,
                )

            return max(wait_time, 0.0)

    def record(self, actual_tokens: int = 0) -> None:
        """记录本次请求的实际消耗。"""
        with self._lock:
            self._record_unlocked(actual_tokens)

    def _record_unlocked(self, actual_tokens: int = 0) -> None:
        """记录消耗；仅用于内部已持锁路径。"""
        now = time.time()
        self._request_times.append(now)
        self._token_records.append((now, actual_tokens))

    def wait_and_record(
        self,
        estimated_tokens: int = 1000,
        actual_tokens: int = 0,
        max_wait_seconds: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """等待许可并记录（原子化操作，防止并发超发）

        [P1-13] 使用锁保护 acquire + sleep + record 全过程。
        sleep 期间释放锁以避免阻塞其他线程的检查操作，
        但在 sleep 结束后重新获取锁并立即 record，最小化竞态窗口。
        """
        self._validate_estimated_tokens(estimated_tokens)
        deadline = (
            None if max_wait_seconds is None else time.monotonic() + max_wait_seconds
        )
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise TimeoutError("rate limiter wait cancelled")

            with self._lock:
                wait = self._prune_and_compute_wait(estimated_tokens)
                if wait <= 0:
                    # 无需等待，立即记录
                    self._record_unlocked(actual_tokens)
                    return

            sleep_for = min(wait, 1.0)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or wait > remaining:
                    raise TimeoutError(
                        "rate limiter wait exceeded max_wait_seconds"
                    )
                sleep_for = min(sleep_for, remaining)

            # [P1-13] 释放锁后 sleep，不阻塞其他线程的 acquire 查询
            time.sleep(sleep_for)

    def _prune_and_compute_wait(self, estimated_tokens: int = 1000) -> float:
        """清理过期记录并计算需要等待的时间；仅用于内部已持锁路径。"""
        self._prune()
        now = time.time()
        wait_time = 0.0

        # RPM 检查
        if len(self._request_times) >= self.rpm:
            oldest = self._request_times[0]
            wait_time = max(wait_time, (oldest + self.window_sec) - now)

        # TPM 检查
        current_tpm = sum(tokens for _, tokens in self._token_records)
        if current_tpm + estimated_tokens > self.tpm:
            if self._token_records:
                oldest_ts = self._token_records[0][0]
                wait_time = max(wait_time, (oldest_ts + self.window_sec) - now)
            else:
                wait_time = max(wait_time, 1.0)

        if wait_time > 0:
            logger.debug(
                "[RateLimiter] 需要等待 %.2fs (requests=%s/%s, tokens=%s/%s)",
                wait_time,
                len(self._request_times),
                self.rpm,
                current_tpm,
                self.tpm,
            )

        return max(wait_time, 0.0)

    def get_status(self) -> dict:
        """返回当前限流器状态"""
        with self._lock:
            self._prune()
            return {
                "requests_in_window": len(self._request_times),
                "rpm_limit": self.rpm,
                "tokens_in_window": sum(t for _, t in self._token_records),
                "tpm_limit": self.tpm,
                "window_sec": self.window_sec,
            }
