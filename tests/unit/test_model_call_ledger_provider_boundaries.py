"""Runtime proof that direct provider boundaries use the durable model-call ledger.

These tests deliberately inspect the SQLite row from inside the mocked provider
call.  That makes the ordering contractual: a provider cannot be reached until
its reservation has been committed and marked dispatched, and a successful
response must leave one settled entry with a provider usage receipt.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.llm_config import LLMApiChain, LLMApiConfig
from core.telemetry.provider_request import (
    ProviderRequestError,
    canonical_chat_input,
    canonical_provider_input,
    utf8_token_upper_bound,
)


class RuntimeConfig:
    def __init__(self, root: Path) -> None:
        self.data_dir = root
        self.database_dir = root
        self.wiki_dir = root / "wiki"
        self._data = {
            "llm": {
                "provider_prices": {
                    "test": {"test-model": {"input": 0.1, "output": 0.2}},
                    "siliconflow": {
                        "embed-model": {"input": 0.1, "output": 0.2},
                        "rerank-model": {"input": 0.1, "output": 0.2},
                    },
                }
            },
            "model_call_ledger": {"daily_cost_cap": 10.0},
        }

    def get(self, key: str, default=None):
        value = self._data
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value


def _sensitive_provider_error_marker() -> str:
    """Build an adversarial payload without committing a credential literal."""
    return "|".join(
        (
            "api" + "_key" + "=" + "DUMMY_CREDENTIAL_VALUE",
            "pass" + "word" + "=" + "DUMMY_CREDENTIAL_VALUE",
            "bank" + "_card" + "=" + "DUMMY_CREDENTIAL_VALUE",
            "private" + "_prompt" + "=" + "PRIVATE_PROMPT_BODY",
            "private" + "_response" + "=" + "PRIVATE_RESPONSE_BODY",
        )
    )


def _chain() -> LLMApiChain:
    return LLMApiChain(
        primary=LLMApiConfig(
            provider="test",
            api_key="test-key",
            base_url="https://provider.example/v1",
            model="test-model",
            source="test",
        )
    )


def _entry(config: RuntimeConfig, operation: str) -> dict[str, object]:
    with sqlite3.connect(str(config.database_dir / "model_call_ledger.db")) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT operation, lifecycle_state, request_dispatched, provider_usage_id, "
            "request_id, actual_input_tokens, actual_output_tokens "
            "FROM model_call_entries WHERE operation=?",
            (operation,),
        ).fetchone()
    assert row is not None
    return dict(row)


def _reserved_snapshot(config: RuntimeConfig, operation: str) -> tuple[str, int]:
    with sqlite3.connect(str(config.database_dir / "model_call_ledger.db")) as conn:
        row = conn.execute(
            "SELECT lifecycle_state, request_dispatched FROM model_call_entries WHERE operation=?",
            (operation,),
        ).fetchone()
    assert row is not None
    return str(row[0]), int(row[1])


def _reservation_input_snapshot(
    config: RuntimeConfig, operation: str
) -> tuple[str, int, int, str]:
    with sqlite3.connect(str(config.database_dir / "model_call_ledger.db")) as conn:
        row = conn.execute(
            "SELECT lifecycle_state, request_dispatched, reserved_input_tokens, input_digest "
            "FROM model_call_entries WHERE operation=?",
            (operation,),
        ).fetchone()
    assert row is not None
    return str(row[0]), int(row[1]), int(row[2]), str(row[3])


def _terminal_snapshot(
    config: RuntimeConfig, operation: str
) -> tuple[str, int, str]:
    with sqlite3.connect(str(config.database_dir / "model_call_ledger.db")) as conn:
        row = conn.execute(
            "SELECT lifecycle_state, request_dispatched, error_code "
            "FROM model_call_entries WHERE operation=?",
            (operation,),
        ).fetchone()
    assert row is not None
    return str(row[0]), int(row[1]), str(row[2])


def test_distillation_provider_is_reserved_before_dispatch_and_settled(tmp_path, monkeypatch):
    from core.hephaestus.distillation_llm import HttpApiHostAgentCaller

    config = RuntimeConfig(tmp_path)
    observed: list[tuple[str, int]] = []
    constructor_kwargs: list[dict[str, object]] = []
    canonical_inputs: list[tuple[str, int, int, str]] = []

    def create(**kwargs):
        observed.append(_reserved_snapshot(config, "distill"))
        provider_input = canonical_chat_input(kwargs["messages"])
        canonical_inputs.append(_reservation_input_snapshot(config, "distill"))
        assert canonical_inputs[-1] == (
            "reserved",
            1,
            utf8_token_upper_bound(provider_input),
            hashlib.sha256(provider_input.encode("utf-8")).hexdigest(),
        )
        return iter(
            [
                SimpleNamespace(
                    id="request-distill-1",
                    usage=None,
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content="provider response"),
                            finish_reason="stop",
                        )
                    ],
                ),
                SimpleNamespace(
                    id="request-distill-1",
                    usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3),
                    choices=[],
                ),
            ]
        )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            constructor_kwargs.append(kwargs)
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    caller = HttpApiHostAgentCaller(api_chain=_chain(), config_getter=lambda: config)

    assert caller.call("secret 蒸馏提示", expect_json=False, max_retries=0) == {
        "raw": "provider response"
    }
    assert observed == [("reserved", 1)]
    assert constructor_kwargs[0]["max_retries"] == 0
    assert constructor_kwargs[0]["http_client"].follow_redirects is False
    entry = _entry(config, "distill")
    assert {key: value for key, value in entry.items() if key != "request_id"} == {
        "operation": "distill",
        "lifecycle_state": "settled",
        "request_dispatched": 1,
        "provider_usage_id": "",
        "actual_input_tokens": 7,
        "actual_output_tokens": 3,
    }
    assert entry["request_id"].startswith("request:v2:sha256:")
    assert "request-distill-1" not in entry["request_id"]
    assert b"secret \xe8\x92\xb8\xe9\xa6\x8f\xe6\x8f\x90\xe7\xa4\xba" not in (
        config.database_dir / "model_call_ledger.db"
    ).read_bytes()


def test_distillation_stream_is_consumed_before_owned_transport_closes(tmp_path, monkeypatch):
    """A real OpenAI stream remains readable until its final usage chunk."""
    pytest.importorskip("openai")
    from core.hephaestus.distillation_llm import HttpApiHostAgentCaller

    config = RuntimeConfig(tmp_path)
    received_paths: list[str] = []
    broken_pipe = False

    class StreamingProvider(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib handler contract.
            nonlocal broken_pipe
            received_paths.append(self.path)
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            # `create(stream=True)` returns after headers.  Waiting here makes
            # a client which closes before iteration deterministically fail.
            time.sleep(0.05)
            events = (
                {
                    "id": "chatcmpl-local-stream",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "test-model",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "streamed "},
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "chatcmpl-local-stream",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "test-model",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "response"},
                            "finish_reason": "stop",
                        }
                    ],
                },
                {
                    "id": "chatcmpl-local-stream",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "test-model",
                    "choices": [],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 3},
                },
            )
            try:
                for event in events:
                    self.wfile.write(
                        ("data: " + json.dumps(event) + "\n\n").encode("utf-8")
                    )
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except BrokenPipeError:
                broken_pipe = True

        def log_message(self, _format, *_args):
            return

    server = HTTPServer(("127.0.0.1", 0), StreamingProvider)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    chain = LLMApiChain(
        primary=LLMApiConfig(
            provider="test",
            api_key="test-key",
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            model="test-model",
            source="test",
        )
    )
    caller = HttpApiHostAgentCaller(api_chain=chain, config_getter=lambda: config)
    try:
        assert caller.call("local streaming request", expect_json=False, max_retries=0) == {
            "raw": "streamed response"
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert received_paths == ["/v1/chat/completions"]
    assert broken_pipe is False
    entry = _entry(config, "distill")
    assert {key: value for key, value in entry.items() if key != "request_id"} == {
        "operation": "distill",
        "lifecycle_state": "settled",
        "request_dispatched": 1,
        "provider_usage_id": "",
        "actual_input_tokens": 7,
        "actual_output_tokens": 3,
    }
    assert entry["request_id"].startswith("request:v2:sha256:")


def test_fragment_merger_provider_is_reserved_before_dispatch_and_settled(tmp_path, monkeypatch):
    from core.hephaestus.fragment_merger import FragmentMerger

    config = RuntimeConfig(tmp_path)
    observed: list[tuple[str, int]] = []
    constructor_kwargs: list[dict[str, object]] = []

    def create(**kwargs):
        observed.append(_reserved_snapshot(config, "distill_merge"))
        provider_input = canonical_chat_input(kwargs["messages"])
        assert _reservation_input_snapshot(config, "distill_merge") == (
            "reserved",
            1,
            utf8_token_upper_bound(provider_input),
            hashlib.sha256(provider_input.encode("utf-8")).hexdigest(),
        )
        return SimpleNamespace(
            id="request-merge-1",
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=5),
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="merged response"), finish_reason="stop"
                )
            ],
        )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            constructor_kwargs.append(kwargs)
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))

    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    merger = FragmentMerger(api_chain=_chain(), enable_llm=True, max_retries=0)

    assert merger._call_llm("secret 合并提示", fragment_count=2) == "merged response"
    assert observed == [("reserved", 1)]
    assert constructor_kwargs[0]["max_retries"] == 0
    entry = _entry(config, "distill_merge")
    assert {key: value for key, value in entry.items() if key != "request_id"} == {
        "operation": "distill_merge",
        "lifecycle_state": "settled",
        "request_dispatched": 1,
        "provider_usage_id": "",
        "actual_input_tokens": 11,
        "actual_output_tokens": 5,
    }
    assert entry["request_id"].startswith("request:v2:sha256:")
    assert "request-merge-1" not in entry["request_id"]
    assert b"secret \xe5\x90\x88\xe5\xb9\xb6\xe6\x8f\x90\xe7\xa4\xba" not in (
        config.database_dir / "model_call_ledger.db"
    ).read_bytes()


def test_openai_provider_error_and_empty_choices_preserve_dispatched_entries(
    tmp_path, monkeypatch, caplog
):
    """SDK errors and post-dispatch empty choices cannot leave a reservation open."""
    from core.hephaestus.distillation_llm import HttpApiHostAgentCaller
    from core.hephaestus.fragment_merger import FragmentMerger

    config = RuntimeConfig(tmp_path)
    constructor_kwargs: list[dict[str, object]] = []

    class ProviderError(Exception):
        pass

    class ErrorOpenAI:
        def __init__(self, **kwargs):
            constructor_kwargs.append(kwargs)
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self._create)
            )

        @staticmethod
        def _create(**_kwargs):
            raise ProviderError("RAW_PROVIDER_EXCEPTION_MARKER_after_dispatch")

    monkeypatch.setitem(
        sys.modules,
        "openai",
        SimpleNamespace(OpenAI=ErrorOpenAI, OpenAIError=ProviderError),
    )
    caller = HttpApiHostAgentCaller(api_chain=_chain(), config_getter=lambda: config)
    caplog.set_level(logging.DEBUG)

    raw, usage = caller._try_api_config("secret 失败提示", 30, _chain().primary)

    assert raw is None
    assert usage == {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
    assert constructor_kwargs[0]["max_retries"] == 0
    assert _terminal_snapshot(config, "distill") == (
        "incurred_unknown",
        1,
        "provider_exception_after_dispatch",
    )
    assert "RAW_PROVIDER_EXCEPTION_MARKER_after_dispatch" not in caplog.text
    assert "category=provider_error" in caplog.text

    class EmptyChoicesOpenAI:
        def __init__(self, **kwargs):
            constructor_kwargs.append(kwargs)
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self._create)
            )

        @staticmethod
        def _create(**_kwargs):
            return SimpleNamespace(
                id="request-empty-choices",
                usage=SimpleNamespace(prompt_tokens=4, completion_tokens=1),
                choices=[],
            )

    monkeypatch.setitem(
        sys.modules,
        "openai",
        SimpleNamespace(OpenAI=EmptyChoicesOpenAI, OpenAIError=ProviderError),
    )
    monkeypatch.setattr("core.config.get_config", lambda: config)
    merger = FragmentMerger(api_chain=_chain(), enable_llm=True, max_retries=0)

    assert merger._call_llm("secret 空选择", fragment_count=1) is None
    assert constructor_kwargs[-1]["max_retries"] == 0
    assert _terminal_snapshot(config, "distill_merge") == (
        "incurred_unknown",
        1,
        "merge_provider_exception",
    )


def test_embedding_and_rerank_boundaries_are_reserved_and_settled(tmp_path):
    from core.embeddings.siliconflow_client import SiliconFlowEmbeddingClient

    config = RuntimeConfig(tmp_path)
    client = SiliconFlowEmbeddingClient(
        api_key="embedding-key",
        base_url="https://embedding.example/v1",
        embedding_model="embed-model",
        rerank_api_key="rerank-key",
        rerank_base_url="https://rerank.example/v1",
        rerank_model="rerank-model",
        config=config,
    )
    observed: list[tuple[str, tuple[str, int]]] = []

    def embedding_create(**kwargs):
        observed.append(("embedding", _reserved_snapshot(config, "embedding")))
        provider_input = canonical_provider_input(kwargs)
        assert _reservation_input_snapshot(config, "embedding") == (
            "reserved",
            1,
            utf8_token_upper_bound(provider_input),
            hashlib.sha256(provider_input.encode("utf-8")).hexdigest(),
        )
        return SimpleNamespace(
            id="request-embedding-1",
            usage=SimpleNamespace(prompt_tokens=4, total_tokens=4),
            data=[SimpleNamespace(embedding=[0.1, 0.2])],
        )

    fake_client = SimpleNamespace(embeddings=SimpleNamespace(create=embedding_create))
    response = MagicMock()
    response.headers = {"x-request-id": "request-rerank-1"}
    response.json.return_value = {
        "id": "request-rerank-1",
        "usage": {"total_tokens": 6},
        "results": [{"index": 0, "relevance_score": 0.8}],
    }

    def rerank_post(*_args, **kwargs):
        observed.append(("rerank", _reserved_snapshot(config, "rerank")))
        provider_input = canonical_provider_input(kwargs["json"])
        assert _reservation_input_snapshot(config, "rerank") == (
            "reserved",
            1,
            utf8_token_upper_bound(provider_input),
            hashlib.sha256(provider_input.encode("utf-8")).hexdigest(),
        )
        return response

    with patch.object(client, "_get_client", return_value=fake_client), patch(
        "requests.post", side_effect=rerank_post
    ):
        assert client.embed(["secret 嵌入文本"]) == [[0.1, 0.2]]
        assert client.rerank("secret 查询", ["secret 文档"], top_n=1) == [(0, 0.8)]

    assert observed == [("embedding", ("reserved", 1)), ("rerank", ("reserved", 1))]
    embedding_entry = _entry(config, "embedding")
    assert {key: value for key, value in embedding_entry.items() if key != "request_id"} == {
        "operation": "embedding",
        "lifecycle_state": "settled",
        "request_dispatched": 1,
        "provider_usage_id": "",
        "actual_input_tokens": 4,
        "actual_output_tokens": 0,
    }
    assert embedding_entry["request_id"].startswith("request:v2:sha256:")
    assert "request-embedding-1" not in embedding_entry["request_id"]
    rerank_entry = _entry(config, "rerank")
    assert {key: value for key, value in rerank_entry.items() if key != "request_id"} == {
        "operation": "rerank",
        "lifecycle_state": "settled",
        "request_dispatched": 1,
        "provider_usage_id": "",
        "actual_input_tokens": 6,
        "actual_output_tokens": 0,
    }
    assert rerank_entry["request_id"].startswith("request:v2:sha256:")
    assert "request-rerank-1" not in rerank_entry["request_id"]
    ledger_bytes = (config.database_dir / "model_call_ledger.db").read_bytes()
    assert b"secret \xe5\xb5\x8c\xe5\x85\xa5\xe6\x96\x87\xe6\x9c\xac" not in ledger_bytes
    assert b"secret \xe6\x9f\xa5\xe8\xaf\xa2" not in ledger_bytes
    assert b"secret \xe6\x96\x87\xe6\xa1\xa3" not in ledger_bytes


def test_embedding_sdk_disables_retries_and_provider_errors_are_terminal(
    tmp_path, monkeypatch, caplog
):
    from core.embeddings.siliconflow_client import SiliconFlowEmbeddingClient

    config = RuntimeConfig(tmp_path)
    constructor_kwargs: list[dict[str, object]] = []
    sensitive_marker = _sensitive_provider_error_marker()

    class ProviderError(Exception):
        pass

    class FakeOpenAI:
        def __init__(self, **kwargs):
            constructor_kwargs.append(kwargs)
            self.embeddings = SimpleNamespace(create=self._create)

        @staticmethod
        def _create(**_kwargs):
            raise ProviderError(sensitive_marker)

    monkeypatch.setitem(
        sys.modules,
        "openai",
        SimpleNamespace(OpenAI=FakeOpenAI, OpenAIError=ProviderError),
    )
    client = SiliconFlowEmbeddingClient(
        api_key="embedding-key",
        base_url="https://embedding.example/v1",
        embedding_model="embed-model",
        config=config,
    )

    caplog.set_level(logging.WARNING)
    with pytest.raises(ProviderRequestError) as exc_info:
        client.embed(["secret 嵌入失败"])

    assert exc_info.value.category == "provider_error"
    assert sensitive_marker not in str(exc_info.value)
    assert sensitive_marker not in caplog.text
    assert "category=provider_error" in caplog.text

    assert len(constructor_kwargs) == 1
    constructor = constructor_kwargs[0]
    assert {
        key: value for key, value in constructor.items() if key != "http_client"
    } == {
        "api_key": "embedding-key",
        "base_url": "https://embedding.example/v1",
        "max_retries": 0,
    }
    assert constructor["http_client"].follow_redirects is False
    assert _terminal_snapshot(config, "embedding") == (
        "incurred_unknown",
        1,
        "embedding_provider_exception",
    )
    assert sensitive_marker.encode("utf-8") not in (
        config.database_dir / "model_call_ledger.db"
    ).read_bytes()


def test_embedding_ledger_error_after_dispatch_stays_typed_and_terminal(tmp_path, monkeypatch):
    """A local ledger invariant after dispatch cannot leak into provider handling."""
    from core.embeddings.siliconflow_client import SiliconFlowEmbeddingClient
    from core.telemetry.prompt_call_log import (
        ModelCallLedgerInvariantError,
        ModelCallReservation,
    )

    config = RuntimeConfig(tmp_path)
    client = SiliconFlowEmbeddingClient(
        api_key="embedding-key",
        base_url="https://embedding.example/v1",
        embedding_model="embed-model",
        config=config,
    )

    def fail_settle(self, *, usage, latency_ms=0):
        del self, usage, latency_ms
        raise ModelCallLedgerInvariantError("test settlement invariant")

    monkeypatch.setattr(ModelCallReservation, "settle", fail_settle)
    provider = SimpleNamespace(
        embeddings=SimpleNamespace(
            create=lambda **_kwargs: SimpleNamespace(
                id="embedding-request",
                usage=SimpleNamespace(prompt_tokens=4, total_tokens=4),
                data=[SimpleNamespace(embedding=[0.1, 0.2])],
            )
        )
    )

    with patch.object(client, "_get_client", return_value=provider):
        with pytest.raises(ModelCallLedgerInvariantError, match="test settlement invariant"):
            client.embed(["visible embedding input"])

    assert _terminal_snapshot(config, "embedding") == (
        "incurred_unknown",
        1,
        "embedding_ledger_error_after_dispatch",
    )


def test_openai_boundary_transport_disables_redirects_and_closes_owned_resources():
    """One reservation must not permit a 307/308 replay by the SDK transport."""
    from core.telemetry.provider_request import non_redirecting_openai_client

    captured: dict[str, object] = {}

    class FakeClient:
        closed = False

        def close(self) -> None:
            self.closed = True

    fake_client = FakeClient()

    def factory(**kwargs):
        captured.update(kwargs)
        return fake_client

    with non_redirecting_openai_client(factory, api_key="test-key") as client:
        assert client is fake_client
        assert captured["max_retries"] == 0
        assert captured["http_client"].follow_redirects is False
        assert captured["http_client"].is_closed is False

    assert fake_client.closed is True
    assert captured["http_client"].is_closed is True


@pytest.mark.parametrize(
    ("relative_path", "helper_name", "minimum_calls"),
    (
        ("core/app/freshness_refresh_worker.py", "non_redirecting_openai_client", 1),
        ("core/app/intent_router.py", "non_redirecting_openai_client", 1),
        ("core/hephaestus/distillation_llm.py", "non_redirecting_openai_client", 1),
        ("core/hephaestus/fragment_merger.py", "non_redirecting_openai_client", 1),
        ("core/reflection/insight_generator.py", "non_redirecting_openai_client", 1),
        ("scripts/verify_installation.py", "non_redirecting_openai_client", 2),
        ("core/embeddings/siliconflow_client.py", "new_non_redirecting_openai_client", 1),
    ),
)
def test_sdk_provider_boundaries_use_owned_non_redirecting_transports(
    relative_path: str, helper_name: str, minimum_calls: int
):
    source_path = Path(__file__).parents[2] / relative_path
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    helper_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == helper_name
    ]

    assert len(helper_calls) >= minimum_calls


@pytest.mark.parametrize(
    "relative_path",
    (
        "scripts/verify_installation.py",
        "core/kia/knowledge_inbox.py",
        "core/embeddings/siliconflow_client.py",
    ),
)
def test_direct_provider_posts_explicitly_reject_redirects(relative_path: str):
    """Raw HTTP boundaries must not replay a billable POST after 307/308."""
    source_path = Path(__file__).parents[2] / relative_path
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    post_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "post"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "requests"
    ]

    assert post_calls, f"expected a direct requests.post provider boundary in {relative_path}"
    for call in post_calls:
        allow_redirects = next(
            (keyword.value for keyword in call.keywords if keyword.arg == "allow_redirects"), None
        )
        assert isinstance(allow_redirects, ast.Constant)
        assert allow_redirects.value is False


def test_rerank_request_error_preserves_dispatched_reservation(tmp_path, caplog):
    from core.embeddings.siliconflow_client import SiliconFlowEmbeddingClient

    config = RuntimeConfig(tmp_path)
    sensitive_marker = _sensitive_provider_error_marker()
    client = SiliconFlowEmbeddingClient(
        api_key="embedding-key",
        base_url="https://embedding.example/v1",
        embedding_model="embed-model",
        rerank_api_key="rerank-key",
        rerank_base_url="https://rerank.example/v1",
        rerank_model="rerank-model",
        config=config,
    )

    caplog.set_level(logging.WARNING)
    with patch("requests.post", side_effect=RuntimeError(sensitive_marker)):
        with pytest.raises(ProviderRequestError) as exc_info:
            client.rerank("secret 查询", ["secret 文档"])

    assert exc_info.value.category == "provider_error"
    assert sensitive_marker not in str(exc_info.value)
    assert sensitive_marker not in caplog.text
    assert "category=provider_error" in caplog.text

    assert _terminal_snapshot(config, "rerank") == (
        "incurred_unknown",
        1,
        "rerank_provider_exception",
    )
    assert sensitive_marker.encode("utf-8") not in (
        config.database_dir / "model_call_ledger.db"
    ).read_bytes()


def test_rerank_ledger_error_after_dispatch_stays_typed_and_terminal(tmp_path, monkeypatch):
    """Rerank retains ledger-control-plane failures after a real dispatch."""
    from core.embeddings.siliconflow_client import SiliconFlowEmbeddingClient
    from core.telemetry.prompt_call_log import (
        ModelCallLedgerInvariantError,
        ModelCallReservation,
    )

    config = RuntimeConfig(tmp_path)
    client = SiliconFlowEmbeddingClient(
        api_key="embedding-key",
        base_url="https://embedding.example/v1",
        embedding_model="embed-model",
        rerank_api_key="rerank-key",
        rerank_base_url="https://rerank.example/v1",
        rerank_model="rerank-model",
        config=config,
    )

    def fail_settle(self, *, usage, latency_ms=0):
        del self, usage, latency_ms
        raise ModelCallLedgerInvariantError("test settlement invariant")

    monkeypatch.setattr(ModelCallReservation, "settle", fail_settle)
    response = MagicMock()
    response.headers = {"x-request-id": "rerank-request"}
    response.json.return_value = {
        "id": "rerank-request",
        "usage": {"total_tokens": 4},
        "results": [{"index": 0, "relevance_score": 0.8}],
    }

    with patch("requests.post", return_value=response):
        with pytest.raises(ModelCallLedgerInvariantError, match="test settlement invariant"):
            client.rerank("visible query", ["visible document"])

    assert _terminal_snapshot(config, "rerank") == (
        "incurred_unknown",
        1,
        "rerank_ledger_error_after_dispatch",
    )


def test_rerank_redirect_cannot_turn_one_reservation_into_two_posts(tmp_path, monkeypatch):
    """A 307 must remain terminal at the first provider POST boundary."""
    from core.embeddings.siliconflow_client import SiliconFlowEmbeddingClient

    config = RuntimeConfig(tmp_path)
    received_paths: list[str] = []

    class RedirectingProvider(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib handler contract.
            received_paths.append(self.path)
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.path == "/rerank":
                self.send_response(307)
                self.send_header("Location", "/redirect-target")
                self.end_headers()
                return
            payload = b'{"id":"redirected","usage":{"total_tokens":1},"results":[]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format, *_args):
            return

    server = HTTPServer(("127.0.0.1", 0), RedirectingProvider)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    client = SiliconFlowEmbeddingClient(
        api_key="embedding-key",
        base_url="https://embedding.example/v1",
        embedding_model="embed-model",
        rerank_api_key="rerank-key",
        rerank_base_url=f"http://127.0.0.1:{server.server_port}",
        rerank_model="rerank-model",
        config=config,
    )
    try:
        with pytest.raises(ProviderRequestError) as exc_info:
            client.rerank("redirect query", ["redirect document"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert received_paths == ["/rerank"]
    assert exc_info.value.category == "provider_error"
    assert _terminal_snapshot(config, "rerank") == (
        "incurred_unknown",
        1,
        "rerank_provider_exception",
    )


def test_cached_embedding_client_creates_a_distinct_run_per_unbound_request(tmp_path):
    from core.embeddings.siliconflow_client import SiliconFlowEmbeddingClient

    config = RuntimeConfig(tmp_path)
    client = SiliconFlowEmbeddingClient(config=config)

    ledger_a, run_a = client._ledger_for_call("embedding")  # noqa: SLF001
    ledger_b, run_b = client._ledger_for_call("embedding")  # noqa: SLF001

    assert ledger_a is ledger_b
    assert run_a != run_b


def test_distillation_run_id_hashes_raw_context_and_keeps_subject_attribution_private(tmp_path):
    from core.hephaestus.backend_bundle import DistillBackendBundle

    config = RuntimeConfig(tmp_path)
    raw_session_context = "session-private-value:revision-private-value"

    class FakeCaller:
        def _get_config(self):
            return config

        def reset_session_cost_budget(self, *_args, **_kwargs):
            return None

    class FakeBackend:
        def __init__(self, caller):
            self.caller = caller

    caller = FakeCaller()
    bundle = DistillBackendBundle(
        judge=FakeBackend(caller),
        extractor=FakeBackend(caller),
        skill=FakeBackend(caller),
    )

    bundle.reset_session_cost_budget(
        1.0,
        run_context=raw_session_context,
        subject_scope=("session", "session-private-value"),
    )

    assert raw_session_context not in bundle.model_call_run_id
    ledger_path = config.database_dir / "model_call_ledger.db"
    assert b"session-private-value" not in ledger_path.read_bytes()
    with sqlite3.connect(str(ledger_path)) as conn:
        run_id = conn.execute("SELECT run_id FROM model_call_runs").fetchone()[0]
        scope = conn.execute(
            "SELECT scope_kind, subject_hash FROM model_call_run_subjects WHERE run_id=?",
            (run_id,),
        ).fetchone()
    assert run_id == bundle.model_call_run_id
    assert scope[0] == "session"
    assert scope[1] != "session-private-value"
