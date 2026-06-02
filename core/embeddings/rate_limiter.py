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
from typing import Optional

logger = logging.getLogger(__name__)


class SiliconFlowRateLimiter:
    """
    双层限流：RPM + TPM，滑动窗口实现。

    Args:
        rpm: 每分钟最大请求数
        tpm: 每分钟最大 token 数
        window_sec: 滑动窗口长度（默认 60 秒）
    """

    def __init__(self, rpm: int = 2000, tpm: int = 500000, window_sec: float = 60.0):
        self.rpm = rpm
        self.tpm = tpm
        self.window_sec = window_sec

        # 请求时间戳队列
        self._request_times: deque[float] = deque()
        # token 消耗队列: (timestamp, tokens)
        self._token_records: deque[tuple[float, int]] = deque()

        self._lock = threading.Lock()

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

        Args:
            estimated_tokens: 预估本次请求消耗的 tokens

        Returns:
            wait_time: 需要等待的秒数
        """
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
                else:
                    # 极端情况：窗口内 token 已超但队列为空（不应发生）
                    wait_time = max(wait_time, 1.0)

            if wait_time > 0:
                logger.debug(
                    f"[RateLimiter] 需要等待 {wait_time:.2f}s "
                    f"(requests={len(self._request_times)}/{self.rpm}, "
                    f"tokens={current_tpm}/{self.tpm})"
                )

            return max(wait_time, 0.0)

    def record(self, actual_tokens: int = 0) -> None:
        """记录本次请求的实际消耗"""
        with self._lock:
            now = time.time()
            self._request_times.append(now)
            self._token_records.append((now, actual_tokens))

    def wait_and_record(self, estimated_tokens: int = 1000, actual_tokens: int = 0) -> None:
        """等待许可并记录（便捷方法）"""
        wait = self.acquire(estimated_tokens)
        if wait > 0:
            time.sleep(wait)
        self.record(actual_tokens)

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
