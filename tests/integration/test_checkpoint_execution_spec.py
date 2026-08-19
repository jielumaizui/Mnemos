import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone

from tests.cognition_episode_fixtures import (
    model_cognition_episode,
    model_exact_evidence,
    resolve_model_evidence,
)


def _spanned_chunk_message(role: str, content: str, turn: int) -> dict[str, object]:
    """Match capture handoff's exact Raw-span shape for engine chunk fixtures."""
    return {
        "role": role,
        "content": content,
        "turn": turn,
        "turn_number": turn,
        "source_span": {
            "revision_id": f"raw-revision-{turn}",
            "logical_event_id": f"logical-event-{turn}",
            "turn_number": turn,
            "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "role": role,
            "span_start": 0,
            "span_end": len(content),
        },
    }


def _fragment():
    from core.hephaestus.distillation_models import KnowledgeFragment

    return KnowledgeFragment(
        form="决策记录",
        title="完整执行规格检查点验证方案",
        frontmatter={"领域": "测试", "摘要": "验证执行规格变化会使检查点失效"},
        background="分块检查点必须绑定完整执行规格。",
        core_content="# 执行规格\n\n模型、提示词、schema 或关键配置变化后不得复用旧结果。" * 4,
        boundaries={"applies": "chunk checkpoint", "not_applies": "raw evidence"},
        anti_patterns=[],
        related_concepts=[],
        claim_ids=["checkpoint-contract"],
    )


def _input_spec(
    *,
    session_id: str = "session",
    text: str = "checkpoint input",
    source_agent: str = "codex",
):
    from core.hephaestus.distill_input_spec import DistillInputSpec

    return DistillInputSpec.build(
        source_agent=source_agent,
        source_session_id=session_id,
        source_event_ids=["raw-1"],
        raw_completeness="full",
        visible_input=text,
        input_mode="chunked",
        source_messages=(
            {
                "role": "user",
                "content": text,
                "source_span": {
                    "revision_id": "raw-1",
                    "content_hash": "sha256:"
                    + hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "span_start": 0,
                    "span_end": len(text),
                    "role": "user",
                },
            },
        ),
    )


def _fragment_payload(fragment):
    return {
        "form": fragment.form,
        "title": fragment.title,
        "frontmatter": dict(fragment.frontmatter),
        "background": fragment.background,
        "core_content": fragment.core_content,
        "boundaries": dict(fragment.boundaries),
        "anti_patterns": list(fragment.anti_patterns),
        "related_concepts": list(fragment.related_concepts),
        "claim_ids": list(fragment.claim_ids),
        "relations": list(fragment.relations),
    }


def _structured_output(input_spec):
    evidence = model_exact_evidence(input_spec)
    return {
        "schema_version": "distill_output_v4",
        "input_spec_hash": input_spec.input_spec_hash,
        "cognition_context_hash": input_spec.cognition_context.context_hash,
        "gate_decision_id": input_spec.gate_decision_id,
        "source_agent": input_spec.source_agent,
        "source_session_id": input_spec.source_session_id,
        "source_event_ids": list(input_spec.source_event_ids),
        "raw_completeness": input_spec.raw_completeness,
        "distill_intent": "create",
        "candidate_summary": "检查点必须只复用同一不可变输入和输出合同。",
        "user_behavior_intent": {
            "content_source": "native_dialogue",
            "user_intent_signal": "seeking_judgment",
            "intent_hypothesis": "seeking_judgment",
            "intent_evidence": [
                {**dict(evidence), "reason": "用户要求验证检查点执行规格。"}
            ],
            "intent_verification_events": [],
            "intent_confidence": 0.8,
            "intent_status": "unverified",
            "behavior_summary": "用户需要验证分块检查点的执行合同。",
        },
        "claims": [
            {
                "claim_id": "checkpoint-contract",
                "claim_text": "已完成检查点必须同时绑定输入规格和输出合同版本。",
                "claim_type": "technical_fact",
                "scope": {"domain": "test", "applies_to": ["checkpoint"]},
                "evidence": [dict(evidence)],
                "relation_to_existing": {"type": "new"},
                "recommended_action": "create_page",
                "confidence": 0.8,
            }
        ],
        "cognition_episode": model_cognition_episode(
            evidence,
            claim_id="checkpoint-contract",
        ),
    }


def _checkpoint_parts(input_spec, fragments=None):
    """Build a valid v4 completed checkpoint and its typed admission proof."""
    from core.hephaestus.chunk_checkpoint import (
        CheckpointAdmission,
        build_checkpoint_output_hash,
    )
    from core.hephaestus.distill_input_spec import OUTPUT_CONTRACT_VERSION
    from core.hephaestus.distillation_contract import (
        canonicalize_extraction_output,
        validate_extraction_output,
    )

    fragments = list(fragments or [_fragment()])
    structured = _structured_output(input_spec)
    payload = {
        "judgment": "knowledge",
        "judgment_reason": "本检查点包含可复用的执行规格证据。",
        "fragments": [_fragment_payload(fragment) for fragment in fragments],
        "structured_output": structured,
    }
    payload = resolve_model_evidence(payload, input_spec)
    structured = payload["structured_output"]
    canonical_output = canonicalize_extraction_output(payload, fragments)
    validation = validate_extraction_output(canonical_output, input_spec)
    assert validation.valid, validation.error_text
    admission = CheckpointAdmission(
        input_spec_hash=input_spec.input_spec_hash,
        output_contract_version=OUTPUT_CONTRACT_VERSION,
        canonical_output_hash=build_checkpoint_output_hash(canonical_output),
        judgment="knowledge",
    )
    chunk_info = {
        "chunk_index": 0,
        "covered_turn_range": "1-1",
        "input_spec_hash": admission.input_spec_hash,
        "output_contract_version": admission.output_contract_version,
        "canonical_output_hash": admission.canonical_output_hash,
        "output_judgment": admission.judgment,
    }
    return fragments, chunk_info, structured, admission, canonical_output


def _extraction_outcome(request, fragment=None):
    """Typed v4 result for the engine-side extractor port."""
    from core.hephaestus.distillation_contract import (
        canonical_extraction_output_hash,
        validate_extraction_output,
    )
    from core.hephaestus.distillation_models import ExtractionOutcome

    fragment = fragment or _fragment()
    structured = _structured_output(request.input_spec)
    payload = {
        "judgment": "knowledge",
        "judgment_reason": "本分块可产生可复用的执行规格知识。",
        "fragments": [_fragment_payload(fragment)],
        "structured_output": structured,
    }
    payload = resolve_model_evidence(payload, request.input_spec)
    structured = payload["structured_output"]
    validation = validate_extraction_output(payload, request.input_spec)
    assert validation.valid, validation.error_text
    return ExtractionOutcome(
        judgment="knowledge",
        fragments=(fragment,),
        structured_output=structured,
        canonical_output=payload,
        admission=validation,
        canonical_output_hash=canonical_extraction_output_hash(
            canonical_output=payload,
        ),
    )


def _spec(input_spec, *, output_contract_version=None):
    from core.hephaestus.distill_execution_spec import DistillExecutionSpec
    from core.hephaestus.distill_input_spec import OUTPUT_CONTRACT_VERSION

    return DistillExecutionSpec(
        input_contract_version="lossless-visible-v1",
        input_spec_hash=input_spec.input_spec_hash,
        output_admission_contract_version=(
            output_contract_version or OUTPUT_CONTRACT_VERSION
        ),
        prompt_version="prompt-v1",
        prompt_hash="sha256:prompt-a",
        output_schema_hash="sha256:schema-a",
        extractor_contract_hash="sha256:extractor-a",
        backend_hash="sha256:backend-a",
        merge_contract_hash="sha256:merge-a",
        model_ids=("provider-a/model-a",),
        config_values={"distill.extract_correction_retries": 1},
    )


def _fingerprint(spec):
    from core.hephaestus.chunk_checkpoint import build_chunk_fingerprint

    return build_chunk_fingerprint(
        [_spanned_chunk_message("user", "checkpoint input", 1)],
        0,
        400,
        5,
        spec.execution_spec_hash,
    )


def test_checkpoint_reuses_only_exact_execution_spec(tmp_path):
    from core.hephaestus.chunk_checkpoint import (
        CheckpointAdmissionRequest,
        ChunkCheckpointStore,
    )

    store = ChunkCheckpointStore(tmp_path / "chunks.db")
    input_spec = _input_spec()
    spec = _spec(input_spec)
    chunk_hash = _fingerprint(spec)
    fragments, chunk_info, structured, admission, canonical_output = _checkpoint_parts(input_spec)
    store.save_completed(
        "session", 0, chunk_hash, spec, fragments, chunk_info, structured, admission,
        canonical_output=canonical_output,
        input_spec=input_spec,
    )

    hit = store.lookup_completed(
        "session",
        0,
        chunk_hash,
        spec,
        CheckpointAdmissionRequest.for_input_spec(input_spec),
    )
    assert hit.cache_hit is True
    assert hit.miss_reason == ""
    assert hit.execution_spec_hash == spec.execution_spec_hash
    assert hit.admission == admission
    assert hit.chunk_info["input_spec_hash"] == input_spec.input_spec_hash

    changed = replace(spec, prompt_hash="sha256:prompt-b")
    miss = store.lookup_completed("session", 0, _fingerprint(changed), changed)
    assert miss.cache_hit is False
    assert miss.miss_reason == "execution_spec_changed"
    assert "prompt_hash" in miss.spec_diff_fields


def test_failed_new_spec_preserves_previous_completed_generation(tmp_path):
    from core.hephaestus.chunk_checkpoint import (
        CheckpointAdmissionRequest,
        ChunkCheckpointStore,
    )

    store = ChunkCheckpointStore(tmp_path / "chunks.db")
    input_spec = _input_spec()
    old_spec = _spec(input_spec)
    old_hash = _fingerprint(old_spec)
    fragments, chunk_info, structured, admission, canonical_output = _checkpoint_parts(input_spec)
    store.save_completed(
        "session", 0, old_hash, old_spec, fragments, chunk_info, structured, admission,
        canonical_output=canonical_output,
        input_spec=input_spec,
    )

    new_spec = replace(old_spec, backend_hash="sha256:backend-b")
    new_hash = _fingerprint(new_spec)
    store.mark_failed("session", 0, new_hash, new_spec, "temporary provider failure")

    assert store.lookup_completed(
        "session",
        0,
        old_hash,
        old_spec,
        CheckpointAdmissionRequest.for_input_spec(input_spec),
    ).cache_hit is True
    failed = store.lookup_completed("session", 0, new_hash, new_spec)
    assert failed.cache_hit is False
    assert failed.miss_reason == "checkpoint_not_completed"
    with sqlite3.connect(str(tmp_path / "chunks.db")) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM distill_chunk_results WHERE session_id = 'session'"
        ).fetchone()[0] == 2


def test_legacy_checkpoint_schema_is_migrated_but_never_reused(tmp_path):
    from core.hephaestus.chunk_checkpoint import (
        CheckpointAdmissionRequest,
        ChunkCheckpointStore,
    )

    db_path = tmp_path / "chunks.db"
    input_spec = _input_spec(session_id="legacy-session")
    spec = _spec(input_spec)
    chunk_hash = _fingerprint(spec)
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE distill_chunk_results (
                session_id TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                chunk_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                fragment_json TEXT NOT NULL DEFAULT '[]',
                chunk_info_json TEXT NOT NULL DEFAULT '{}',
                structured_output_json TEXT,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (session_id, chunk_index)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO distill_chunk_results VALUES (?, ?, ?, 'completed', ?, '{}', NULL, '', ?, ?)
            """,
            (
                "legacy-session",
                0,
                chunk_hash,
                json.dumps([], ensure_ascii=False),
                now,
                now,
            ),
        )

    store = ChunkCheckpointStore(db_path)
    miss = store.lookup_completed(
        "legacy-session",
        0,
        chunk_hash,
        spec,
        CheckpointAdmissionRequest.for_input_spec(input_spec),
    )

    assert miss.cache_hit is False
    assert miss.miss_reason == "legacy_execution_spec_missing"
    with sqlite3.connect(str(db_path)) as conn:
        pk = [
            row[1]
            for row in conn.execute("PRAGMA table_info(distill_chunk_results)")
            if row[5] > 0
        ]
        assert pk == ["session_id", "chunk_index", "chunk_hash"]


def test_corrupt_execution_spec_metadata_is_never_reused(tmp_path):
    from core.hephaestus.chunk_checkpoint import (
        CheckpointAdmissionRequest,
        ChunkCheckpointStore,
    )

    store = ChunkCheckpointStore(tmp_path / "chunks.db")
    input_spec = _input_spec()
    spec = _spec(input_spec)
    chunk_hash = _fingerprint(spec)
    fragments, chunk_info, structured, admission, canonical_output = _checkpoint_parts(input_spec)
    store.save_completed(
        "session", 0, chunk_hash, spec, fragments, chunk_info, structured, admission,
        canonical_output=canonical_output,
        input_spec=input_spec,
    )
    with sqlite3.connect(str(tmp_path / "chunks.db")) as conn:
        conn.execute(
            "UPDATE distill_chunk_results SET execution_spec_json = 'not-json'"
        )

    miss = store.lookup_completed(
        "session",
        0,
        chunk_hash,
        spec,
        CheckpointAdmissionRequest.for_input_spec(input_spec),
    )
    assert miss.cache_hit is False
    assert miss.miss_reason == "corrupt_execution_spec"


def test_completed_checkpoint_without_admission_metadata_is_never_reused(tmp_path):
    """A prior completed row without COG-011 proof is legacy, never a hit."""
    from core.hephaestus.chunk_checkpoint import (
        CheckpointAdmissionRequest,
        ChunkCheckpointStore,
    )

    store = ChunkCheckpointStore(tmp_path / "chunks.db")
    input_spec = _input_spec()
    spec = _spec(input_spec)
    chunk_hash = _fingerprint(spec)
    fragments, chunk_info, structured, admission, canonical_output = _checkpoint_parts(input_spec)
    store.save_completed(
        "session", 0, chunk_hash, spec, fragments, chunk_info, structured, admission,
        canonical_output=canonical_output,
        input_spec=input_spec,
    )
    # Simulate a v2-era completed row: the extraction result exists, but it
    # does not say which immutable input/output contract admitted it.
    chunk_info.pop("input_spec_hash")
    chunk_info.pop("output_contract_version")
    chunk_info.pop("canonical_output_hash")
    chunk_info.pop("output_judgment")
    with sqlite3.connect(str(tmp_path / "chunks.db")) as conn:
        conn.execute(
            "UPDATE distill_chunk_results SET chunk_info_json = ?",
            (json.dumps(chunk_info, ensure_ascii=False),),
        )

    miss = store.lookup_completed(
        "session",
        0,
        chunk_hash,
        spec,
        CheckpointAdmissionRequest.for_input_spec(input_spec),
    )

    assert miss.cache_hit is False
    assert miss.miss_reason == "legacy_output_admission_missing"


def test_execution_identity_includes_input_spec_and_output_contract(tmp_path):
    """Neither provenance input nor output-union version can share a cache key."""
    from core.hephaestus.chunk_checkpoint import ChunkCheckpointStore

    store = ChunkCheckpointStore(tmp_path / "chunks.db")
    first_input = _input_spec(text="first visible input")
    first_spec = _spec(first_input)
    first_hash = _fingerprint(first_spec)
    fragments, chunk_info, structured, admission, canonical_output = _checkpoint_parts(first_input)
    store.save_completed(
        "session", 0, first_hash, first_spec, fragments, chunk_info, structured, admission,
        canonical_output=canonical_output,
        input_spec=first_input,
    )

    changed_input = _input_spec(text="changed visible input")
    changed_input_spec = _spec(changed_input)
    assert changed_input_spec.execution_spec_hash != first_spec.execution_spec_hash
    input_miss = store.lookup_completed(
        "session", 0, _fingerprint(changed_input_spec), changed_input_spec
    )
    assert input_miss.cache_hit is False
    assert input_miss.miss_reason == "execution_spec_changed"
    assert "input_spec_hash" in input_miss.spec_diff_fields

    changed_output_contract = _spec(
        first_input, output_contract_version="distill_output_v999"
    )
    assert changed_output_contract.execution_spec_hash != first_spec.execution_spec_hash
    output_miss = store.lookup_completed(
        "session",
        0,
        _fingerprint(changed_output_contract),
        changed_output_contract,
    )
    assert output_miss.cache_hit is False
    assert output_miss.miss_reason == "execution_spec_changed"
    assert "output_admission_contract_version" in output_miss.spec_diff_fields


class _EngineConfig:
    def __init__(self, root, values=None):
        self.database_dir = root / "db"
        self.wiki_dir = root / "wiki"
        self._values = {
            "distill.chunk_checkpoint_enabled": True,
            "distill.chunk_checkpoint_db_path": str(self.database_dir / "chunks.db"),
            **(values or {}),
        }

    def get(self, key, default=None):
        return self._values.get(key, default)


class _EngineBackend:
    def __init__(self, model):
        self.model = model

    def checkpoint_identity(self):
        return {"provider": "test", "model": self.model}

    def call(self, prompt, expect_json=True, **kwargs):
        raise AssertionError("engine checkpoint test must not call the backend directly")


class _EngineExtractor:
    def __init__(self, model, template="template-a"):
        self.backend = _EngineBackend(model)
        self.template = template
        self.calls = []

    def prepare_prompt(self, request):
        from core.hephaestus.distill_input_spec import PreparedExtractionPrompt

        return PreparedExtractionPrompt.build(
            f"{self.template}|{request.input_spec.source_session_id}|"
            f"{request.analysis_type}|{request.session_text}",
            request,
        )

    def extract(self, request, *, prepared=None):
        assert prepared is not None
        prepared.assert_matches(request)
        self.calls.append((request, prepared))
        return _extraction_outcome(request)


class _EngineMerger:
    def checkpoint_identity(self):
        return {"strategy": "test-stable"}


def _engine(root, cfg, *, model="model-a", template="template-a"):
    from core.hephaestus.distillation_engine import DistillationEngine

    engine = DistillationEngine(
        wiki_base=str(root / "wiki"),
        backend_factory=lambda: _EngineBackend(model),
        receipt_config=cfg,
    )
    extractor = _EngineExtractor(model, template)
    engine._extractor = extractor
    engine._fragment_merger = _EngineMerger()
    chunk = [_spanned_chunk_message("user", "engine checkpoint input", 1)]
    engine._chunk_messages = lambda *args, **kwargs: [chunk]
    return engine, extractor, chunk


def _run_chunked(engine, cfg, chunk):
    from core.hephaestus.distillation_engine import DistillationResult

    result = DistillationResult(session_id="engine-restart-session")
    return engine._extract_chunked(
        result,
        chunk,
        {"cfg": cfg, "chunk_size": 400},
    )


def test_engine_restart_reuses_exact_spec_and_invalidates_model_prompt_and_config(tmp_path):
    baseline_cfg = _EngineConfig(tmp_path, {"distill.extract_correction_retries": 1})
    first, first_extractor, chunk = _engine(tmp_path, baseline_cfg)
    _, first_infos = _run_chunked(first, baseline_cfg, chunk)
    assert len(first_extractor.calls) == 1
    assert first_infos[0]["cache_hit"] is False
    assert first_infos[0]["miss_reason"] == "checkpoint_not_found"

    restarted, restarted_extractor, chunk = _engine(tmp_path, baseline_cfg)
    _, restarted_infos = _run_chunked(restarted, baseline_cfg, chunk)
    assert restarted_extractor.calls == []
    assert restarted_infos[0]["cache_hit"] is True
    assert restarted_infos[0]["checkpoint_reused"] is True

    model_changed, model_extractor, chunk = _engine(tmp_path, baseline_cfg, model="model-b")
    _, model_infos = _run_chunked(model_changed, baseline_cfg, chunk)
    assert len(model_extractor.calls) == 1
    assert model_infos[0]["miss_reason"] == "execution_spec_changed"
    assert {"backend_hash", "model_ids"}.issubset(model_infos[0]["spec_diff_fields"])

    prompt_changed, prompt_extractor, chunk = _engine(
        tmp_path, baseline_cfg, model="model-b", template="template-b"
    )
    _, prompt_infos = _run_chunked(prompt_changed, baseline_cfg, chunk)
    assert len(prompt_extractor.calls) == 1
    assert prompt_infos[0]["miss_reason"] == "execution_spec_changed"
    assert "prompt_hash" in prompt_infos[0]["spec_diff_fields"]

    changed_cfg = _EngineConfig(tmp_path, {"distill.extract_correction_retries": 2})
    config_changed, config_extractor, chunk = _engine(
        tmp_path, changed_cfg, model="model-b", template="template-b"
    )
    _, config_infos = _run_chunked(config_changed, changed_cfg, chunk)
    assert len(config_extractor.calls) == 1
    assert config_infos[0]["miss_reason"] == "execution_spec_changed"
    assert (
        "config_values.distill.extract_correction_retries"
        in config_infos[0]["spec_diff_fields"]
    )
