# -*- coding: utf-8 -*-
"""
Auxiliary Client - 统一 LLM 客户端

【设计原则】宿主agent优先：
- chat/completion 默认通过宿主agent CLI（claude -p / kimi --print）
- embedding / reranking 允许直接调用 API（OpenAI/SiliconFlow）
- 只有显式指定 provider 时才走 API 链路

降级链路（仅当显式指定 provider 时生效）：
anthropic → openai → siliconflow

特性：
- 统一接口（不同 provider 差异透明化）
- 与 CredentialPool 集成
- 响应标准化
- 网络重试机制（3次，指数退避）

用法:
    from core.auxiliary_client import AuxiliaryClient

    client = AuxiliaryClient()

    # 【默认】通过宿主agent调用（claude -p / kimi --print）
    response = client.chat(
        messages=[{"role": "user", "content": "Hello"}],
    )

    # 【显式】指定 provider 走 API
    response = client.chat(
        messages=[...],
        provider="openai",
    )

    # embedding（允许API）
    vectors = client.embed(["text1", "text2"])

    # reranking（允许API）
    ranked = client.rerank("query", ["doc1", "doc2"])
"""
from __future__ import annotations
import logging
logger = logging.getLogger(__name__)

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


class HostAgentAdapter(ProviderAdapter):
    """宿主agent适配器（claude -p / kimi --print）

    【设计原则】所有 chat/completion 默认通过宿主agent CLI，
    不直接调用任何 LLM API。
    """

    def chat(self, request: ChatRequest, api_key: str = None,
             api_base: Optional[str] = None) -> ChatResponse:
        from core.host_agent_caller import HostAgentCaller

        # 检测可用的宿主agent
        agent_type = HostAgentCaller.detect_available_agent()
        if not agent_type:
            raise RuntimeError(
                "No host agent available. Install Claude Code (npm install -g @anthropic-ai/claude-code) "
                "or Kimi CLI."
            )

        caller = HostAgentCaller(agent_type=agent_type, timeout=300)

        # 将 messages 转换为 prompt
        prompt = self._messages_to_prompt(request)

        start = time.time()
        result = caller.call(
            prompt=prompt,
            max_tokens=request.max_tokens,
            system_prompt=request.system,
        )
        latency = (time.time() - start) * 1000

        if not result.success:
            raise RuntimeError(f"Host agent ({agent_type}) failed: {result.error}")

        return ChatResponse(
            content=result.text,
            provider=f"host_agent/{agent_type}",
            model=agent_type,
            usage={
                "input_tokens": result.tokens_estimated,
                "output_tokens": 0,
            },
            latency_ms=latency,
            raw_response=None,
        )

    @staticmethod
    def _messages_to_prompt(request: ChatRequest) -> str:
        """将消息列表转换为单条 prompt"""
        parts = []
        if request.system:
            parts.append(f"[System]\n{request.system}")
        for msg in request.messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            parts.append(f"[{role.capitalize()}]\n{content}")
        return "\n\n".join(parts)


class AnthropicAdapter(ProviderAdapter):
    """Anthropic Claude API 适配器（仅当显式指定 provider 时使用）"""

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

    # 默认降级链路（仅当显式指定 provider 时使用）
    API_CHAIN = [Provider.ANTHROPIC, Provider.OPENAI, Provider.SILICONFLOW]

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
        self.fallback_chain = fallback_chain or list(self.API_CHAIN)
        self._last_provider: Optional[str] = None
        self._last_error: Optional[str] = None
        self._host_agent_adapter = HostAgentAdapter()

    def chat(self,
             messages: List[Dict[str, str]],
             model: Optional[str] = None,
             max_tokens: int = 1000,
             temperature: float = 0.7,
             system: Optional[str] = None,
             provider: Optional[str] = None) -> ChatResponse:
        """
        发送聊天请求

        【设计原则】
        - provider=None（默认）→ 通过宿主agent CLI（claude -p / kimi --print）
        - provider="anthropic/openai/siliconflow" → 显式走 API

        Args:
            messages: 消息列表
            model: 模型名称
            max_tokens: 最大输出 token
            temperature: 温度
            system: 系统提示
            provider: 指定 provider（None 则默认宿主agent）

        Returns:
            ChatResponse

        Raises:
            RuntimeError: 宿主agent不可用 或 所有 API provider 都失败
        """
        request = ChatRequest(
            messages=messages,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
        )

        # ============================================================
        # 默认路径：宿主agent CLI（claude -p / kimi --print）
        # ============================================================
        if provider is None:
            try:
                response = self._host_agent_adapter.chat(request)
                self._last_provider = response.provider
                self._last_error = None
                return response
            except RuntimeError:
                # 宿主agent不可用，检查是否允许降级到API
                if os.getenv("MNEMOS_ALLOW_API_FALLBACK"):
                    pass  # 继续下面的API降级逻辑
                else:
                    raise RuntimeError(
                        "Host agent not available (claude/kimi CLI not found). "
                        "Install a host agent, or set MNEMOS_ALLOW_API_FALLBACK=1 to use API fallback."
                    )

        # ============================================================
        # 显式指定 provider 或 允许降级时：走 API 链路
        # ============================================================
        if provider:
            try:
                p = Provider(provider)
                chain = [p]
            except ValueError:
                raise ValueError(f"Unknown provider: {provider}")
        else:
            chain = self.fallback_chain

        errors = []
        for p in chain:
            adapter = self.ADAPTERS.get(p)
            if not adapter:
                errors.append(f"{p.value}: no adapter")
                continue

            cred = self.pool.get_key(p)
            if not cred:
                errors.append(f"{p.value}: no available key")
                continue

            try:
                if not request.model:
                    request.model = cred.model or self.DEFAULT_MODELS.get(p)

                response = adapter.chat(
                    request=request,
                    api_key=cred.api_key,
                    api_base=cred.api_base,
                )

                self.pool.mark_success(cred.id)
                self._last_provider = p.value
                self._last_error = None
                return response

            except Exception as e:
                error_msg = f"{p.value}: {type(e).__name__}: {str(e)[:100]}"
                errors.append(error_msg)
                self._last_error = error_msg
                self.pool.mark_failure(cred.id, error=str(e))
                continue

        raise RuntimeError(
            f"All providers failed. Errors: {'; '.join(errors)}"
        )

    def embed(self, texts: List[str], model: str = None) -> List[List[float]]:
        """
        文本嵌入（允许直接调用 API）

        【例外】embedding 是允许直接调用 API 的两个场景之一。
        默认使用 OpenAI 的 text-embedding-3-small，可通过环境变量覆盖。

        Args:
            texts: 文本列表
            model: 嵌入模型

        Returns:
            向量列表
        """
        provider = Provider.OPENAI
        cred = self.pool.get_key(provider)
        if not cred:
            raise RuntimeError("No OpenAI API key available for embedding")

        try:
            from openai import OpenAI
        except ImportError:
            raise RuntimeError("openai package not installed")

        client = OpenAI(api_key=cred.api_key, base_url=cred.api_base)
        model_name = model or "text-embedding-3-small"

        # 批量处理（OpenAI 限制每批最多 2048 条）
        all_embeddings = []
        batch_size = 100
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            resp = client.embeddings.create(input=batch, model=model_name)
            for item in resp.data:
                all_embeddings.append(item.embedding)

        self.pool.mark_success(cred.id)
        return all_embeddings

    def rerank(self, query: str, documents: List[str],
               model: str = None, top_n: int = None) -> List[Dict]:
        """
        重排序（允许直接调用 API）

        【例外】reranking 是允许直接调用 API 的两个场景之一。
        使用 SiliconFlow 的 BGE-Reranker 或 Cohere 的 rerank API。

        Args:
            query: 查询文本
            documents: 文档列表
            model: 重排模型
            top_n: 返回前 N 个结果

        Returns:
            [{"index": int, "text": str, "score": float}, ...]
        """
        provider = Provider.SILICONFLOW
        cred = self.pool.get_key(provider)
        if not cred:
            raise RuntimeError("No SiliconFlow API key available for reranking")

        try:
            from openai import OpenAI
        except ImportError:
            raise RuntimeError("openai package not installed")

        client = OpenAI(api_key=cred.api_key, base_url=cred.api_base or "https://api.siliconflow.cn/v1")
        model_name = model or "BAAI/bge-reranker-v2-m3"

        # 构造 rerank 请求（SiliconFlow 兼容格式）
        resp = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": "Rerank the following documents based on relevance to the query."},
                {"role": "user", "content": f"Query: {query}\n\nDocuments:\n" + "\n".join(f"{i}. {d}" for i, d in enumerate(documents))},
            ],
            max_tokens=1000,
        )

        # 解析响应（简化版：按行提取索引和分数）
        content = resp.choices[0].message.content
        results = []
        for line in content.split("\n"):
            match = __import__('re').match(r'\s*(\d+)[:.\)]\s*(.+?)\s*[-—:]\s*([\d.]+)', line)
            if match:
                idx = int(match.group(1))
                if 0 <= idx < len(documents):
                    results.append({
                        "index": idx,
                        "text": documents[idx],
                        "score": float(match.group(3)),
                    })

        # 按分数排序
        results.sort(key=lambda x: x["score"], reverse=True)
        if top_n:
            results = results[:top_n]

        self.pool.mark_success(cred.id)
        return results

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
        logger.info("降级链路:", " -> ".join(chain))
        logger.info("可用 provider:", ", ".join(available))
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
        logger.warning(f"错误: {e}")


if __name__ == "__main__":
    main()
