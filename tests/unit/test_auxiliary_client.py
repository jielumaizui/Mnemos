"""
auxiliary_client 单元测试

核心验证点（E14 宿主agent优先原则）：
- chat() 默认（provider=None）走 HostAgentAdapter，不走任何 API
- 显式指定 provider 时才走 API 链路
- embed()/rerank() 允许直接调用 API（两个例外场景）
- quick_chat() 继承默认行为
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import unittest
from unittest.mock import patch, MagicMock

from core.auxiliary_client import (
    AuxiliaryClient, ChatRequest, HostAgentAdapter,
    AnthropicAdapter, OpenAIAdapter, SiliconFlowAdapter,
)


class TestDefaultHostAgent(unittest.TestCase):
    """验证默认行为：provider=None 时走宿主agent"""

    @patch("core.host_agent_caller.HostAgentCaller.detect_available_agent")
    @patch("core.host_agent_caller.HostAgentCaller.call")
    def test_chat_default_uses_host_agent(self, mock_call, mock_detect):
        """chat() 默认 provider=None → 调用 HostAgentAdapter"""
        mock_detect.return_value = "generic"
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.text = "Hello from host agent"
        mock_result.tokens_estimated = 10
        mock_call.return_value = mock_result

        client = AuxiliaryClient()
        resp = client.chat(messages=[{"role": "user", "content": "hi"}])

        self.assertTrue(resp.content.startswith("Hello from host agent"))
        self.assertTrue(resp.provider.startswith("host_agent"))
        mock_detect.assert_called_once()
        mock_call.assert_called_once()

    @patch("core.host_agent_caller.HostAgentCaller.detect_available_agent")
    def test_chat_default_raises_when_no_host_agent(self, mock_detect):
        """宿主agent不可用时抛出 RuntimeError，不走 API"""
        mock_detect.return_value = None

        client = AuxiliaryClient()
        with self.assertRaises(RuntimeError) as ctx:
            client.chat(messages=[{"role": "user", "content": "hi"}])

        self.assertIn("Host agent", str(ctx.exception))

    @patch("core.host_agent_caller.HostAgentCaller.detect_available_agent")
    @patch("core.host_agent_caller.HostAgentCaller.call")
    def test_quick_chat_inherits_default(self, mock_call, mock_detect):
        """quick_chat() 也默认走宿主agent"""
        mock_detect.return_value = "generic"
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.text = "Quick response"
        mock_result.tokens_estimated = 5
        mock_call.return_value = mock_result

        client = AuxiliaryClient()
        text = client.quick_chat("hello")

        self.assertEqual(text, "Quick response")
        mock_call.assert_called_once()


class TestExplicitProvider(unittest.TestCase):
    """验证显式指定 provider 时走 API"""

    @patch("core.auxiliary_client.OpenAIAdapter.chat")
    @patch("core.credential_pool.CredentialPool.get_key")
    def test_chat_with_explicit_provider(self, mock_get_key, mock_adapter_chat):
        """显式 provider="openai" → 走 OpenAIAdapter"""
        mock_cred = MagicMock()
        mock_cred.api_key = "test-key"
        mock_cred.api_base = None
        mock_cred.model = None
        mock_cred.id = "cred-1"
        mock_get_key.return_value = mock_cred

        mock_resp = MagicMock()
        mock_resp.content = "API response"
        mock_resp.provider = "openai"
        mock_resp.model = "gpt-4"
        mock_resp.usage = {"input_tokens": 10, "output_tokens": 5}
        mock_resp.latency_ms = 100.0
        mock_resp.raw_response = None
        mock_adapter_chat.return_value = mock_resp

        client = AuxiliaryClient()
        resp = client.chat(
            messages=[{"role": "user", "content": "hi"}],
            provider="openai",
        )

        self.assertEqual(resp.provider, "openai")
        mock_adapter_chat.assert_called_once()


class TestHostAgentAdapter(unittest.TestCase):
    """测试 HostAgentAdapter 自身"""

    @patch("core.host_agent_caller.HostAgentCaller.detect_available_agent")
    @patch("core.host_agent_caller.HostAgentCaller.call")
    def test_messages_to_prompt_format(self, mock_call, mock_detect):
        """验证消息列表正确转换为 prompt"""
        mock_detect.return_value = "claude"
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.text = "ok"
        mock_result.tokens_estimated = 1
        mock_call.return_value = mock_result

        adapter = HostAgentAdapter()
        request = ChatRequest(
            messages=[
                {"role": "user", "content": "Question?"},
                {"role": "assistant", "content": "Answer."},
            ],
            system="You are helpful",
        )
        adapter.chat(request)

        call_kwargs = mock_call.call_args.kwargs
        prompt = call_kwargs["prompt"]
        self.assertIn("[System]", prompt)
        self.assertIn("You are helpful", prompt)
        self.assertIn("[User]", prompt)
        self.assertIn("Question?", prompt)
        self.assertIn("[Assistant]", prompt)
        self.assertIn("Answer.", prompt)

    def test_messages_to_prompt_no_system(self):
        """无 system prompt 时不包含 [System]"""
        adapter = HostAgentAdapter()
        request = ChatRequest(
            messages=[{"role": "user", "content": "hi"}],
        )
        prompt = adapter._messages_to_prompt(request)
        self.assertNotIn("[System]", prompt)
        self.assertIn("[User]", prompt)


class TestEmbedRerankAllowed(unittest.TestCase):
    """验证 embed/rerank 允许 API（两个例外）"""

    @patch("core.credential_pool.CredentialPool.get_key")
    def test_embed_uses_api(self, mock_get_key):
        """embed() 直接调用 OpenAI API（允许）"""
        mock_cred = MagicMock()
        mock_cred.api_key = "test-key"
        mock_cred.api_base = None
        mock_cred.id = "cred-1"
        mock_get_key.return_value = mock_cred

        # mock 整个 openai 模块（环境中可能未安装）
        fake_openai = MagicMock()
        mock_client = MagicMock()
        mock_emb = MagicMock()
        mock_emb.embedding = [0.1, 0.2, 0.3]
        mock_resp = MagicMock()
        mock_resp.data = [mock_emb]
        mock_client.embeddings.create.return_value = mock_resp
        fake_openai.OpenAI.return_value = mock_client

        with patch.dict("sys.modules", {"openai": fake_openai}):
            client = AuxiliaryClient()
            result = client.embed(["hello"])

            self.assertEqual(len(result), 1)
            self.assertEqual(len(result[0]), 3)
            mock_client.embeddings.create.assert_called_once()

    @patch("core.credential_pool.CredentialPool.get_key")
    def test_rerank_uses_api(self, mock_get_key):
        """rerank() 直接调用 API（允许）"""
        mock_cred = MagicMock()
        mock_cred.api_key = "test-key"
        mock_cred.api_base = None
        mock_cred.id = "cred-1"
        mock_get_key.return_value = mock_cred

        fake_openai = MagicMock()
        mock_client = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = "0. doc A - 0.9\n1. doc B - 0.7"
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_client.chat.completions.create.return_value = mock_resp
        fake_openai.OpenAI.return_value = mock_client

        with patch.dict("sys.modules", {"openai": fake_openai}):
            client = AuxiliaryClient()
            result = client.rerank("query", ["doc A", "doc B"])

            self.assertIsInstance(result, list)
            mock_client.chat.completions.create.assert_called_once()


class TestAdapterRegistry(unittest.TestCase):
    """验证适配器注册"""

    def test_adapters_registered(self):
        """所有 provider 都有对应的适配器"""
        from core.credential_pool import Provider
        client = AuxiliaryClient()
        self.assertIn(Provider.ANTHROPIC, client.ADAPTERS)
        self.assertIn(Provider.OPENAI, client.ADAPTERS)
        self.assertIn(Provider.SILICONFLOW, client.ADAPTERS)
        self.assertIsInstance(client.ADAPTERS[Provider.ANTHROPIC], AnthropicAdapter)
        self.assertIsInstance(client.ADAPTERS[Provider.OPENAI], OpenAIAdapter)
        self.assertIsInstance(client.ADAPTERS[Provider.SILICONFLOW], SiliconFlowAdapter)


if __name__ == "__main__":
    unittest.main()
