# -*- coding: utf-8 -*-
"""
SiliconFlowRateLimiter 单元测试
"""

import time

import pytest

from core.embeddings.rate_limiter import SiliconFlowRateLimiter


class TestSiliconFlowRateLimiter:
    def test_acquire_zero_when_empty(self):
        limiter = SiliconFlowRateLimiter(rpm=10, tpm=1000)
        assert limiter.acquire(estimated_tokens=100) == 0.0

    def test_acquire_blocks_after_rpm_exceeded(self):
        limiter = SiliconFlowRateLimiter(rpm=2, tpm=10000, window_sec=1.0)
        limiter.record()
        limiter.record()
        wait = limiter.acquire(estimated_tokens=1)
        assert wait > 0.0  # 超过 RPM，需要等待

    def test_acquire_blocks_after_tpm_exceeded(self):
        limiter = SiliconFlowRateLimiter(rpm=100, tpm=100, window_sec=1.0)
        limiter.record(actual_tokens=80)
        wait = limiter.acquire(estimated_tokens=30)
        assert wait > 0.0  # 超过 TPM，需要等待

    def test_window_slides(self):
        limiter = SiliconFlowRateLimiter(rpm=2, tpm=1000, window_sec=0.1)
        limiter.record()
        limiter.record()
        # 等待窗口滑动
        time.sleep(0.15)
        wait = limiter.acquire(estimated_tokens=1)
        assert wait == 0.0  # 窗口已滑动，可以执行

    def test_wait_and_record(self):
        limiter = SiliconFlowRateLimiter(rpm=1000, tpm=1000000)
        start = time.time()
        limiter.wait_and_record(estimated_tokens=10, actual_tokens=10)
        elapsed = time.time() - start
        assert elapsed < 0.1  # 不应等待

    def test_status(self):
        limiter = SiliconFlowRateLimiter(rpm=10, tpm=1000)
        limiter.record(actual_tokens=50)
        limiter.record(actual_tokens=30)
        status = limiter.get_status()
        assert status["requests_in_window"] == 2
        assert status["tokens_in_window"] == 80
        assert status["rpm_limit"] == 10
        assert status["tpm_limit"] == 1000
