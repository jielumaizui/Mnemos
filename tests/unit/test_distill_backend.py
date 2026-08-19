import pytest


def test_distill_backend_response_preserves_complete_response_evidence():
    from core.hephaestus.distill_response import DistillBackendResponse

    response = DistillBackendResponse.create(
        raw_text='{"judgment":"knowledge"}',
        parsed={"judgment": "knowledge"},
        usage={"prompt_tokens": 12, "completion_tokens": 3, "cost": 0.01},
        provider="test-provider",
        model="test-model",
        request_id="request-123",
        finish_reason="stop",
        parse_path="direct_json",
        attempt_history=(
            {
                "attempt": 0,
                "provider": "test-provider",
                "model": "test-model",
                "status": "success",
            },
        ),
    )

    assert response.parsed == {"judgment": "knowledge"}
    assert response.raw_text == '{"judgment":"knowledge"}'
    assert response.provider == "test-provider"
    assert response.model == "test-model"
    assert response.request_id == "request-123"
    assert response.finish_reason == "stop"
    assert response.parse_path == "direct_json"
    assert response.attempt_history[0]["status"] == "success"
    assert len(response.response_hash) == 64
    assert response.response_hash == DistillBackendResponse.hash_raw_text(response.raw_text)


def test_distill_backend_response_marks_transport_empty_without_inventing_raw_text():
    from core.hephaestus.distill_response import DistillBackendResponse

    response = DistillBackendResponse.transport_empty(
        usage={"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0},
        provider="test-provider",
        model="test-model",
        attempt_history=({"attempt": 0, "status": "transport_empty"},),
    )

    assert response.successful is False
    assert response.raw_text == ""
    assert response.parsed is None
    assert response.parse_path == "transport_empty"
    assert response.to_failure_metadata()["transport_empty"] is True


class _FakeBackend:
    def call(self, prompt, expect_json=True, **kwargs):
        from core.hephaestus.distill_response import DistillBackendResponse

        parsed = {"prompt": prompt, "expect_json": expect_json, "kwargs": kwargs}
        return DistillBackendResponse.create(
            raw_text="fake raw response",
            parsed=parsed,
            usage={"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0},
            provider="test",
            model="fake-model",
            request_id="fake-request",
            finish_reason="stop",
            parse_path="direct_json",
            attempt_history=({"attempt": 0, "status": "success"},),
        )

    def checkpoint_identity(self):
        return {"provider": "test", "model": "fake-model"}


def test_knowledge_extractor_requires_explicit_backend():
    from core.hephaestus.distillation_extractor import KnowledgeExtractor

    with pytest.raises(RuntimeError, match="distill backend is required"):
        KnowledgeExtractor()


def test_value_judge_requires_explicit_backend():
    from core.hephaestus.distillation_value_judge import LLMValueJudge

    with pytest.raises(RuntimeError, match="distill backend is required"):
        LLMValueJudge()


def test_backend_chain_single_node_records_metrics():
    from core.hephaestus.distill_backend import BackendChain

    chain = BackendChain([_FakeBackend()])

    result = chain.call("hello", expect_json=False, response_max_tokens=123)

    assert result.parsed["prompt"] == "hello"
    assert result.parsed["expect_json"] is False
    assert result.parsed["kwargs"]["response_max_tokens"] == 123
    assert result.raw_text == "fake raw response"
    assert len(chain.metrics) == 1
    assert chain.metrics[0].backend == "_FakeBackend"
    assert chain.metrics[0].ok is True


def test_backend_chain_rejects_multiple_nodes():
    from core.hephaestus.distill_backend import BackendChain

    with pytest.raises(ValueError, match="exactly one backend"):
        BackendChain([_FakeBackend(), _FakeBackend()])


def test_backend_chain_exposes_explicit_checkpoint_identity():
    from core.hephaestus.distill_backend import BackendChain

    identity = BackendChain([_FakeBackend()]).checkpoint_identity()

    assert identity == {
        "strategy": "single",
        "backends": [{"provider": "test", "model": "fake-model"}],
    }


def test_llm_backend_uses_typed_evidence_port_instead_of_parsed_only_call():
    from core.hephaestus.distill_backend import LLMBackend
    from core.hephaestus.distill_response import DistillBackendResponse

    class _Caller:
        def call(self, *_args, **_kwargs):
            raise AssertionError("parsed-only compatibility call must not be used")

        def call_with_evidence(self, prompt, expect_json=True, **kwargs):
            return DistillBackendResponse.create(
                raw_text='{"answer":42}',
                parsed={"answer": 42},
                usage={"prompt_tokens": 2, "completion_tokens": 2, "cost": 0.0},
                provider="typed-provider",
                model="typed-model",
                request_id="request-42",
                finish_reason="stop",
                parse_path="direct_json",
                attempt_history=({"attempt": 0, "status": "success"},),
            )

        def checkpoint_identity(self):
            return {"provider": "typed-provider", "model": "typed-model"}

    response = LLMBackend(_Caller()).call("question", expect_json=True)

    assert response.parsed == {"answer": 42}
    assert response.raw_text == '{"answer":42}'
    assert response.provider == "typed-provider"
