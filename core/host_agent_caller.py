"""
HostAgentCaller — Host Agent 调用器

【E14 全库修复】统一封装各 LLM Agent 的 CLI 调用：
- Claude Code: claude -p
- Kimi: kimi --print
- 通用 OpenAI API 兼容端点

提供重试、超时、错误回退、Token 估算等通用能力。
"""

import os
import re
import subprocess
import time
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class CallResult:
    """调用结果"""
    success: bool
    text: str
    latency_ms: int
    error: str = ""
    tokens_estimated: int = 0


class HostAgentCaller:
    """Host Agent 统一调用器"""

    # 【设计原则】宿主agent优先：只封装 claude/kimi CLI，不直接调用任何 LLM API
    # 如果宿主agent不可用，调用失败并上报，而不是偷偷回退到第三方API
    SUPPORTED_AGENTS = ["claude", "kimi", "generic"]

    def __init__(self, agent_type: str = "claude", timeout: int = 300,
                 max_retries: int = 3, retry_delay: float = 2.0):
        if agent_type not in self.SUPPORTED_AGENTS:
            raise ValueError(f"Unsupported agent: {agent_type}. "
                           f"Supported: {self.SUPPORTED_AGENTS}")

        self.agent_type = agent_type
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay

    def call(self, prompt: str, max_tokens: int = 8000,
             system_prompt: str = None) -> CallResult:
        """
        调用 Agent 执行 prompt

        Args:
            prompt: 用户 prompt
            max_tokens: 最大输出 token 数
            system_prompt: 系统提示（如支持）

        Returns:
            CallResult
        """
        start_time = time.time()

        for attempt in range(self.max_retries):
            try:
                if self.agent_type == "claude":
                    result = self._call_claude(prompt, max_tokens, system_prompt)
                elif self.agent_type == "kimi":
                    result = self._call_kimi(prompt, max_tokens, system_prompt)
                elif self.agent_type == "openai":
                    result = self._call_openai(prompt, max_tokens, system_prompt)
                else:
                    result = self._call_generic(prompt, max_tokens, system_prompt)

                latency_ms = int((time.time() - start_time) * 1000)
                return CallResult(
                    success=True,
                    text=result,
                    latency_ms=latency_ms,
                    tokens_estimated=self._estimate_tokens(prompt + result),
                )

            except subprocess.TimeoutExpired:
                logger.warning(f"[HostAgentCaller] 调用超时 (attempt {attempt + 1}/{self.max_retries})")
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay * (attempt + 1))
                else:
                    latency_ms = int((time.time() - start_time) * 1000)
                    return CallResult(
                        success=False, text="", latency_ms=latency_ms,
                        error=f"Timeout after {self.timeout}s",
                    )

            except Exception as e:
                logger.warning(f"[HostAgentCaller] 调用失败: {e} (attempt {attempt + 1}/{self.max_retries})")
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay * (attempt + 1))
                else:
                    latency_ms = int((time.time() - start_time) * 1000)
                    return CallResult(
                        success=False, text="", latency_ms=latency_ms,
                        error=str(e),
                    )

        # 不应到达这里
        return CallResult(success=False, text="", latency_ms=0, error="Unknown error")

    def _call_claude(self, prompt: str, max_tokens: int,
                     system_prompt: str = None) -> str:
        """调用 Claude Code CLI"""
        cmd = ["claude", "-p"]
        if system_prompt:
            cmd.extend(["--system", system_prompt])
        cmd.append(prompt)

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=self.timeout,
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            raise RuntimeError(f"Claude CLI error: {result.stderr[:500]}")
        return result.stdout.strip()

    def _call_kimi(self, prompt: str, max_tokens: int,
                   system_prompt: str = None) -> str:
        """调用 Kimi CLI"""
        cmd = ["kimi", "--print"]
        if system_prompt:
            cmd.extend(["--system", system_prompt])
        cmd.append(prompt)

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=self.timeout,
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            raise RuntimeError(f"Kimi CLI error: {result.stderr[:500]}")
        return result.stdout.strip()

    # 【已移除】OpenAI API 直接调用
    # 设计原则：所有 LLM 调用必须通过宿主agent CLI（claude -p / kimi --print）
    # 不允许代码中直接嵌入第三方 LLM API 调用，防止：
    # 1. API key 泄露风险
    # 2. 成本失控（无法通过宿主agent的配额/审计追踪）
    # 3. 绕过宿主agent的安全策略（如 thinking 模式、tool 权限等）

    def _call_generic(self, prompt: str, max_tokens: int,
                      system_prompt: str = None) -> str:
        """通用回退：尝试从环境变量读取命令模板"""
        template = os.getenv("HOST_AGENT_CALL_TEMPLATE", "")
        if not template:
            raise RuntimeError("No generic agent template configured. "
                             "Set HOST_AGENT_CALL_TEMPLATE env var.")

        cmd = template.replace("{prompt}", prompt).split()
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=self.timeout,
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            raise RuntimeError(f"Generic agent error: {result.stderr[:500]}")
        return result.stdout.strip()

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """粗略估算 token 数（中文字符 ≈ 1 token，英文词 ≈ 1.3 tokens）"""
        cn_chars = len(re.findall(r'[\u4e00-\u9fa5]', text))
        en_words = len(re.findall(r'[a-zA-Z]+', text))
        others = len(text) - cn_chars - sum(len(w) for w in re.findall(r'[a-zA-Z]+', text))
        return int(cn_chars + en_words * 1.3 + others * 0.25)

    @staticmethod
    def detect_available_agent() -> Optional[str]:
        """检测系统中可用的 Agent"""
        for agent in ["claude", "kimi"]:
            try:
                result = subprocess.run(
                    [agent, "--version"],
                    capture_output=True, timeout=5,
                )
                if result.returncode == 0:
                    return agent
            except Exception:
                continue

        if os.getenv("HOST_AGENT_CALL_TEMPLATE"):
            return "generic"

        return None


class RetryableCaller:
    """带缓存和熔断的重试调用器"""

    def __init__(self, caller: HostAgentCaller,
                 cache_ttl_seconds: int = 300,
                 circuit_failure_threshold: int = 5):
        self.caller = caller
        self.cache: Dict[str, Tuple[str, float]] = {}
        self.cache_ttl = cache_ttl_seconds
        self.failure_count = 0
        self.circuit_threshold = circuit_failure_threshold
        self.circuit_open = False

    def call(self, prompt: str, max_tokens: int = 8000,
             system_prompt: str = None, use_cache: bool = True) -> CallResult:
        """带缓存和熔断的调用"""
        cache_key = f"{hash(prompt) & 0xFFFFFFFF}:{max_tokens}"

        # 1. 检查缓存
        if use_cache and cache_key in self.cache:
            cached_text, cached_time = self.cache[cache_key]
            if time.time() - cached_time < self.cache_ttl:
                return CallResult(success=True, text=cached_text,
                                  latency_ms=0, error="")

        # 2. 熔断检查
        if self.circuit_open:
            return CallResult(success=False, text="",
                              latency_ms=0, error="Circuit breaker open")

        # 3. 执行调用
        result = self.caller.call(prompt, max_tokens, system_prompt)

        if result.success:
            self.failure_count = max(0, self.failure_count - 1)
            self.circuit_open = False
            if use_cache:
                self.cache[cache_key] = (result.text, time.time())
        else:
            self.failure_count += 1
            if self.failure_count >= self.circuit_threshold:
                self.circuit_open = True
                logger.error(f"[RetryableCaller] 熔断器打开，连续失败 {self.failure_count} 次")

        return result

    def reset_circuit(self):
        """手动重置熔断器"""
        self.circuit_open = False
        self.failure_count = 0
