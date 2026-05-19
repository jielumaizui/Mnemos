# -*- coding: utf-8 -*-
"""
Auxiliary Client - 多 Provider 降级客户端

降级链路：
Claude (Anthropic) → OpenAI → SiliconFlow → Local

特性：
- 统一接口（不同 provider 差异透明化）
- 请求级自动降级（一个失败自动切换下一个）
- 与 CredentialPool 集成
- 响应标准化
- Anthropic/OpenAI SDK 懒加载
- 网络重试机制（3次，指数退避）

用法:
    from core.auxiliary_client import AuxiliaryClient

    client = AuxiliaryClient()

    # 自动选择可用 provider
    response = client.chat(
        messages=[{"role": "user", "content": "Hello"}],
    )

    # 指定 provider
    response = client.chat(
        messages=[...],
        provider="openai",
    )
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Any
from enum import Enum

from core.credential_pool import (
    CredentialPool, Provider, get_default_pool,
)


# ==================== 1. 数据模型 ====================

@dataclass
class ChatResponse:
    """标准化响应"""
    content: str
    provider: str
    model: str
    usage: Optional[Dict[str, int]] = None
    latency_ms: float = 0.0
    raw_response: Optional[Any] = None


@dataclass
class ChatRequest:
    """标准化请求"""
    messages: List[Dict[str, str]]
    model: Optional[str] = None
    max_tokens: int = 1000
    temperature: float = 0.7
    system: Optional[str] = None


# ==================== 2. 网络重试 ====================

def _retry_call(fn, max_retries: int = 3, base_delay: float = 1.0):
    """
    带指数退避的重试包装器

    Args:
        fn: 无参可调用对象
        max_retries: 最大重试次数
        base_delay: 基础延迟（秒），每次翻倍
    """
    last_exc = None
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                time.sleep(delay)
    raise last_exc


# ==================== 3. Provider 适配器 ====================

class ProviderAdapter:
    """Provider 适配器基类"""

    def chat(self, request: ChatRequest, api_key: str,
             api_base: Optional[str] = None) -> ChatResponse:
        raise NotImplementedError


class AnthropicAdapter(ProviderAdapter):
    """Anthropic Claude 适配器"""

    # 默认模型（可被 credential_pool 中的配置覆盖）
    DEFAULT_MODEL = "claude-sonnet-4-6"

    def chat(self, request: ChatRequest, api_key: str,
             api_base: Optional[str] = None) -> ChatResponse:
        import anthropic

        kwargs = {"api_key": api_key}
        if api_base:
            kwargs["base_url"] = api_base

        client = anthropic.Anthropic(**kwargs)
        messages = request.messages.copy()
        call_kwargs = {
            "model": request.model or self.DEFAULT_MODEL,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": messages,
        }
        if request.system:
            call_kwargs["system"] = request.system

        start = time.time()
        resp = _retry_call(lambda: client.messages.create(**call_kwargs))
        latency = (time.time() - start) * 1000

        return ChatResponse(
            content=resp.content[0].text,
            provider="anthropic",
            model=resp.model,
            usage={
                "input_tokens": resp.usage.input_tokens,
                "output_tokens": resp.usage.output_tokens,
            },
            latency_ms=latency,
            raw_response=resp,
        )


class OpenAIAdapter(ProviderAdapter):
    """OpenAI 适配器"""

    DEFAULT_MODEL = "gpt-4"

    def chat(self, request: ChatRequest, api_key: str,
             api_base: Optional[str] = None) -> ChatResponse:
        try:
            from openai import OpenAI
        except ImportError:
            raise RuntimeError("openai package not installed")

        kwargs = {"api_key": api_key}
        if api_base:
            kwargs["base_url"] = api_base

        client = OpenAI(**kwargs)
        messages = request.messages.copy()
        if request.system:
            messages.insert(0, {"role": "system", "content": request.system})

        start = time.time()
        resp = _retry_call(lambda: client.chat.completions.create(
            model=request.model or self.DEFAULT_MODEL,
            messages=messages,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
        ))
        latency = (time.time() - start) * 1000

        return ChatResponse(
            content=resp.choices[0].message.content,
            provider="openai",
            model=resp.model,
            usage={
                "input_tokens": resp.usage.prompt_tokens,
                "output_tokens": resp.usage.completion_tokens,
            },
            latency_ms=latency,
            raw_response=resp,
        )


class SiliconFlowAdapter(ProviderAdapter):
    """SiliconFlow 适配器（OpenAI 兼容 API）"""

    DEFAULT_MODEL = "deepseek-ai/DeepSeek-V2.5"

    def chat(self, request: ChatRequest, api_key: str,
             api_base: Optional[str] = None) -> ChatResponse:
        try:
            from openai import OpenAI
        except ImportError:
            raise RuntimeError("openai package not installed")

        client = OpenAI(
            api_key=api_key,
            base_url=api_base or "https://api.siliconflow.cn/v1",
        )

        messages = request.messages.copy()
        if request.system:
            messages.insert(0, {"role": "system", "content": request.system})

        start = time.time()
        resp = _retry_call(lambda: client.chat.completions.create(
            model=request.model or self.DEFAULT_MODEL,
            messages=messages,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
        ))
        latency = (time.time() - start) * 1000

        return ChatResponse(
            content=resp.choices[0].message.content,
            provider="siliconflow",
            model=resp.model,
            usage={
                "input_tokens": resp.usage.prompt_tokens,
                "output_tokens": resp.usage.completion_tokens,
            },
            latency_ms=latency,
            raw_response=resp,
        )


# ==================== 4. AuxiliaryClient ====================

class AuxiliaryClient:
    """
    多 Provider 降级客户端

    降级链路（可配置）：
    anthropic → openai → siliconflow
    """

    # 默认降级链路
    DEFAULT_CHAIN = [Provider.ANTHROPIC, Provider.OPENAI, Provider.SILICONFLOW]

    # Provider → 适配器映射
    ADAPTERS = {
        Provider.ANTHROPIC: AnthropicAdapter(),
        Provider.OPENAI: OpenAIAdapter(),
        Provider.SILICONFLOW: SiliconFlowAdapter(),
    }

    # 默认模型映射（可被 credential 中 model 字段覆盖）
    DEFAULT_MODELS = {
        Provider.ANTHROPIC: "claude-sonnet-4-6",
        Provider.OPENAI: "gpt-4",
        Provider.SILICONFLOW: "deepseek-ai/DeepSeek-V2.5",
    }

    def __init__(self, credential_pool: Optional[CredentialPool] = None,
                 fallback_chain: Optional[List[Provider]] = None):
        self.pool = credential_pool or get_default_pool()
        self.fallback_chain = fallback_chain or list(self.DEFAULT_CHAIN)
        self._last_provider: Optional[str] = None
        self._last_error: Optional[str] = None

    def chat(self,
             messages: List[Dict[str, str]],
             model: Optional[str] = None,
             max_tokens: int = 1000,
             temperature: float = 0.7,
             system: Optional[str] = None,
             provider: Optional[str] = None) -> ChatResponse:
        """
        发送聊天请求，自动降级

        Args:
            messages: 消息列表
            model: 模型名称（None 则使用 provider 默认或 credential_pool 配置）
            max_tokens: 最大输出 token
            temperature: 温度
            system: 系统提示
            provider: 指定 provider（None 则自动选择）

        Returns:
            ChatResponse

        Raises:
            RuntimeError: 所有 provider 都失败
        """
        request = ChatRequest(
            messages=messages,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
        )

        # 确定尝试顺序
        if provider:
            try:
                p = Provider(provider)
                chain = [p]
            except ValueError:
                raise ValueError(f"Unknown provider: {provider}")
        else:
            chain = self.fallback_chain

        # 尝试每个 provider
        errors = []
        for p in chain:
            adapter = self.ADAPTERS.get(p)
            if not adapter:
                errors.append(f"{p.value}: no adapter")
                continue

            # 从 pool 获取可用 key
            cred = self.pool.get_key(p)
            if not cred:
                errors.append(f"{p.value}: no available key")
                continue

            try:
                # 使用优先级：用户指定 > credential 中的 model > provider 默认
                if not request.model:
                    request.model = cred.model or self.DEFAULT_MODELS.get(p)

                response = adapter.chat(
                    request=request,
                    api_key=cred.api_key,
                    api_base=cred.api_base,
                )

                # 标记成功
                self.pool.mark_success(cred.id)
                self._last_provider = p.value
                self._last_error = None
                return response

            except Exception as e:
                error_msg = f"{p.value}: {type(e).__name__}: {str(e)[:100]}"
                errors.append(error_msg)
                self._last_error = error_msg

                # 标记失败
                self.pool.mark_failure(cred.id, error=str(e))

                # 继续下一个 provider
                continue

        # 全部失败
        raise RuntimeError(
            f"All providers failed. Errors: {'; '.join(errors)}"
        )

    def quick_chat(self, prompt: str,
                   system: Optional[str] = None,
                   max_tokens: int = 500,
                   temperature: float = 0.7) -> str:
        """
        快速单轮对话

        Returns:
            响应文本（字符串）
        """
        messages = [{"role": "user", "content": prompt}]
        resp = self.chat(messages=messages, system=system, max_tokens=max_tokens, temperature=temperature)
        return resp.content

    def get_last_provider(self) -> Optional[str]:
        """获取上次成功使用的 provider"""
        return self._last_provider

    def get_last_error(self) -> Optional[str]:
        """获取上次错误信息"""
        return self._last_error

    def get_available_providers(self) -> List[str]:
        """获取当前可用的 provider 列表"""
        available = []
        for p in self.fallback_chain:
            cred = self.pool.get_key(p)
            if cred:
                available.append(p.value)
        return available


# ==================== 便捷函数 ====================

_default_client: Optional[AuxiliaryClient] = None
_client_lock = __import__("threading").Lock()


def get_default_client() -> AuxiliaryClient:
    """获取全局默认 AuxiliaryClient 实例"""
    global _default_client
    if _default_client is None:
        with _client_lock:
            if _default_client is None:
                _default_client = AuxiliaryClient()
    return _default_client


# ==================== CLI ====================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Auxiliary Client CLI")
    parser.add_argument("prompt", nargs="?", help="聊天提示")
    parser.add_argument("--system", help="系统提示")
    parser.add_argument("--provider", help="指定 provider")
    parser.add_argument("--model", help="指定模型")
    parser.add_argument("--max-tokens", type=int, default=500)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--list-providers", action="store_true",
                        help="列出可用 provider")
    args = parser.parse_args()

    client = get_default_client()

    if args.list_providers:
        available = client.get_available_providers()
        chain = [p.value for p in client.fallback_chain]
        print("降级链路:", " -> ".join(chain))
        print("可用 provider:", ", ".join(available))
        return

    if not args.prompt:
        parser.print_help()
        return

    try:
        resp = client.chat(
            messages=[{"role": "user", "content": args.prompt}],
            model=args.model,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            system=args.system,
            provider=args.provider,
        )
        print(f"[{resp.provider}/{resp.model}] 延迟: {resp.latency_ms:.0f}ms")
        print(f"用量: {resp.usage}")
        print("-" * 40)
        print(resp.content)
    except RuntimeError as e:
        print(f"错误: {e}")


if __name__ == "__main__":
    main()
