import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from tests.cognition_episode_fixtures import (
    model_cognition_episode,
    model_exact_evidence,
    resolve_model_evidence,
)


def _fragment(title: str = "可续跑分块蒸馏检查点"):
    from core.hephaestus.distillation_engine import KnowledgeFragment

    return KnowledgeFragment(
        form="决策记录",
        title=title,
        frontmatter={"领域": "测试", "摘要": "验证分块蒸馏检查点的鲁棒性"},
        background="分块蒸馏成功 chunk 会写入本地检查点。",
        core_content=(
            "# 检查点\n\n成功 chunk 应能在后续重试中复用，坏行应被忽略。"
            "检查点还必须同时保存不可变输入规格、输出合同版本和完整输出哈希。" * 3
        ),
        boundaries={"applies": "chunked distillation", "not_applies": "standard"},
        anti_patterns=[],
        related_concepts=[],
        claim_ids=["checkpoint-unit-contract"],
        relations=[],
        keywords=["distill", "checkpoint"],
    )


def _input_spec(
    *,
    session_id="sess",
    text="checkpoint test input",
    artifact_refs=(),
):
    from core.hephaestus.distill_input_spec import DistillInputSpec

    return DistillInputSpec.build(
        source_agent="codex",
        source_session_id=session_id,
        source_event_ids=["raw-1"],
        raw_completeness="full",
        visible_input=text,
        input_mode="chunked",
        artifact_refs=artifact_refs,
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


def _attachment_ref(path, *, turn=1):
    import hashlib

    from core.evidence.artifact_uri import build_artifact_uri

    return {
        "uri": build_artifact_uri("codex", "sess", turn, "attachment", 0),
        "artifact_type": "attachment",
        "summary": "checkpoint attachment",
        "source_event_id": "raw-1",
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "path": str(path),
        "mime_type": "text/plain",
    }


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


def _checkpoint_parts(input_spec, fragments=None):
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
    evidence = model_exact_evidence(input_spec)
    structured = {
        "schema_version": "distill_output_v4",
        "input_spec_hash": input_spec.input_spec_hash,
        "cognition_context_hash": input_spec.cognition_context.context_hash,
        "gate_decision_id": input_spec.gate_decision_id,
        "source_agent": input_spec.source_agent,
        "source_session_id": input_spec.source_session_id,
        "source_event_ids": list(input_spec.source_event_ids),
        "raw_completeness": input_spec.raw_completeness,
        "distill_intent": "create",
        "candidate_summary": "检查点持久化必须保留输入和输出合同身份。",
        "user_behavior_intent": {
            "content_source": "native_dialogue",
            "user_intent_signal": "seeking_judgment",
            "intent_hypothesis": "seeking_judgment",
            "intent_evidence": [
                {**dict(evidence), "reason": "用户要求验证检查点持久化合同。"}
            ],
            "intent_verification_events": [],
            "intent_confidence": 0.8,
            "intent_status": "unverified",
            "behavior_summary": "用户需要验证检查点的可恢复性。",
        },
        "claims": [
            {
                "claim_id": "checkpoint-unit-contract",
                "claim_text": "检查点命中必须验证不可变输入和输出合同。",
                "claim_type": "technical_fact",
                "scope": {"domain": "test"},
                "evidence": [dict(evidence)],
                "relation_to_existing": {"type": "new"},
                "recommended_action": "create_page",
                "confidence": 0.8,
            }
        ],
        "cognition_episode": model_cognition_episode(
            evidence,
            claim_id="checkpoint-unit-contract",
        ),
    }
    payload = {
        "judgment": "knowledge",
        "judgment_reason": "检查点包含可复用的验证结果。",
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
        "covered_turn_range": "1-2",
        "input_spec_hash": admission.input_spec_hash,
        "output_contract_version": admission.output_contract_version,
        "canonical_output_hash": admission.canonical_output_hash,
        "output_judgment": admission.judgment,
    }
    return fragments, chunk_info, structured, admission, canonical_output


def _skip_checkpoint_parts(input_spec):
    """Build a legal empty checkpoint branch without treating it as malformed."""
    from core.hephaestus.chunk_checkpoint import (
        CheckpointAdmission,
        build_checkpoint_output_hash,
    )
    from core.hephaestus.distill_input_spec import OUTPUT_CONTRACT_VERSION
    from core.hephaestus.distillation_contract import validate_extraction_output

    structured = {
        "schema_version": OUTPUT_CONTRACT_VERSION,
        **input_spec.prompt_contract(),
        "distill_intent": "skip",
        "candidate_summary": "检查点 fixture 没有可沉淀的长期知识。",
        "skip_reason": "该输入只包含检查点基础设施验证，没有用户知识结论。",
        "no_value_evidence": [
            {
                "source_event_id": input_spec.source_event_ids[0],
                "reason": "输入只验证缓存分支，不提供可复用的用户结论。",
            }
        ],
        "claims": [],
    }
    canonical_output = {
        "judgment": "skip",
        "judgment_reason": "这是一个合法的无知识 checkpoint 分支。",
        "fragments": [],
        "structured_output": structured,
    }
    validation = validate_extraction_output(canonical_output, input_spec)
    assert validation.valid and validation.is_skip, validation.error_text
    admission = CheckpointAdmission(
        input_spec_hash=input_spec.input_spec_hash,
        output_contract_version=OUTPUT_CONTRACT_VERSION,
        canonical_output_hash=build_checkpoint_output_hash(canonical_output),
        judgment="skip",
    )
    chunk_info = {
        "chunk_index": 0,
        "covered_turn_range": "1-1",
        "input_spec_hash": admission.input_spec_hash,
        "output_contract_version": admission.output_contract_version,
        "canonical_output_hash": admission.canonical_output_hash,
        "output_judgment": admission.judgment,
    }
    return chunk_info, structured, admission, canonical_output


def _spec(input_spec):
    from core.hephaestus.distill_execution_spec import DistillExecutionSpec
    from core.hephaestus.distill_input_spec import OUTPUT_CONTRACT_VERSION

    return DistillExecutionSpec(
        input_contract_version="lossless-visible-v1",
        input_spec_hash=input_spec.input_spec_hash,
        output_admission_contract_version=OUTPUT_CONTRACT_VERSION,
        prompt_version="prompt-v1",
        prompt_hash="sha256:prompt",
        output_schema_hash="sha256:schema",
        extractor_contract_hash="sha256:extractor",
        backend_hash="sha256:backend",
        merge_contract_hash="sha256:merge",
        model_ids=("provider/model",),
        config_values={},
    )


def test_corrupt_completed_checkpoint_is_ignored(tmp_path):
    from core.hephaestus.chunk_checkpoint import (
        CheckpointAdmissionRequest,
        ChunkCheckpointStore,
    )

    db_path = tmp_path / "chunks.db"
    store = ChunkCheckpointStore(db_path)
    input_spec = _input_spec()
    spec = _spec(input_spec)
    fragments, chunk_info, structured, admission, canonical_output = _checkpoint_parts(input_spec)
    store.save_completed(
        "sess",
        0,
        "hash",
        spec,
        fragments,
        chunk_info,
        structured,
        admission,
        canonical_output=canonical_output,
        input_spec=input_spec,
    )

    admission_request = CheckpointAdmissionRequest.for_input_spec(input_spec)
    assert store.load_completed("sess", 0, "hash", spec, admission_request) is not None

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            UPDATE distill_chunk_results
            SET fragment_json = ?, chunk_info_json = ?
            WHERE session_id = ? AND chunk_index = ?
            """,
            ('"not-a-list"', '"not-a-dict"', "sess", 0),
        )
        conn.commit()

    lookup = store.lookup_completed("sess", 0, "hash", spec, admission_request)
    assert lookup.cache_hit is False
    assert lookup.miss_reason == "corrupt_checkpoint_payload"
    assert store.load_completed("sess", 0, "hash", spec, admission_request) is None


def test_checkpoint_hits_when_same_artifact_moves_to_another_local_path(tmp_path):
    from core.hephaestus.chunk_checkpoint import (
        CheckpointAdmissionRequest,
        ChunkCheckpointStore,
    )

    first = tmp_path / "first" / "evidence.txt"
    second = tmp_path / "second" / "renamed.txt"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("stable evidence", encoding="utf-8")
    second.write_text("stable evidence", encoding="utf-8")
    saved_input = _input_spec(artifact_refs=(_attachment_ref(first, turn=1),))
    replay_input = _input_spec(artifact_refs=(_attachment_ref(second, turn=9),))

    assert saved_input.input_spec_hash == replay_input.input_spec_hash
    store = ChunkCheckpointStore(tmp_path / "chunks.db")
    execution_spec = _spec(saved_input)
    fragments, chunk_info, structured, admission, canonical_output = _checkpoint_parts(
        saved_input
    )
    store.save_completed(
        "sess",
        0,
        "hash",
        execution_spec,
        fragments,
        chunk_info,
        structured,
        admission,
        canonical_output=canonical_output,
        input_spec=saved_input,
    )

    lookup = store.lookup_completed(
        "sess",
        0,
        "hash",
        _spec(replay_input),
        CheckpointAdmissionRequest.for_input_spec(replay_input),
    )

    assert lookup.cache_hit is True
    assert lookup.canonical_output == canonical_output


def test_completed_checkpoint_missing_typed_admission_is_a_legacy_miss(tmp_path):
    from core.hephaestus.chunk_checkpoint import (
        CheckpointAdmissionRequest,
        ChunkCheckpointStore,
    )

    store = ChunkCheckpointStore(tmp_path / "chunks.db")
    input_spec = _input_spec()
    spec = _spec(input_spec)
    fragments, chunk_info, structured, admission, canonical_output = _checkpoint_parts(input_spec)
    store.save_completed(
        "sess", 0, "hash", spec, fragments, chunk_info, structured, admission,
        canonical_output=canonical_output,
        input_spec=input_spec,
    )

    # This is a completed legacy row, not a legal cache hit: it lacks the
    # immutable input hash, v4 output version, verdict and output hash proof.
    with sqlite3.connect(str(tmp_path / "chunks.db")) as conn:
        conn.execute(
            "UPDATE distill_chunk_results SET chunk_info_json = ?",
            (json.dumps({"chunk_index": 0, "covered_turn_range": "1-2"}),),
        )

    lookup = store.lookup_completed(
        "sess",
        0,
        "hash",
        spec,
        CheckpointAdmissionRequest.for_input_spec(input_spec),
    )
    assert lookup.cache_hit is False
    assert lookup.miss_reason == "legacy_output_admission_missing"


def test_checkpoint_store_rejects_invalid_root_on_write_and_reuse(tmp_path):
    """Neither a forged admission nor a stored invalid union may become a hit."""
    from core.hephaestus.chunk_checkpoint import (
        CheckpointAdmission,
        CheckpointAdmissionRequest,
        ChunkCheckpointStore,
        build_checkpoint_output_hash,
    )

    db_path = tmp_path / "chunks.db"
    store = ChunkCheckpointStore(db_path)
    input_spec = _input_spec()
    spec = _spec(input_spec)
    fragments, chunk_info, structured, admission, canonical_output = _checkpoint_parts(input_spec)

    invalid_output = json.loads(json.dumps(canonical_output))
    invalid_output["structured_output"]["claims"] = []
    invalid_admission = CheckpointAdmission(
        input_spec_hash=input_spec.input_spec_hash,
        output_contract_version=admission.output_contract_version,
        canonical_output_hash=build_checkpoint_output_hash(invalid_output),
        judgment="knowledge",
    )
    invalid_info = dict(chunk_info)
    invalid_info["canonical_output_hash"] = invalid_admission.canonical_output_hash
    store.save_completed(
        "invalid",
        0,
        "invalid-hash",
        spec,
        fragments,
        invalid_info,
        structured,
        invalid_admission,
        canonical_output=invalid_output,
        input_spec=input_spec,
    )
    invalid_lookup = store.lookup_completed(
        "invalid",
        0,
        "invalid-hash",
        spec,
        CheckpointAdmissionRequest.for_input_spec(input_spec),
    )
    assert invalid_lookup.cache_hit is False
    assert invalid_lookup.miss_reason == "checkpoint_not_found"

    store.save_completed(
        "valid",
        0,
        "valid-hash",
        spec,
        fragments,
        chunk_info,
        structured,
        admission,
        canonical_output=canonical_output,
        input_spec=input_spec,
    )
    with sqlite3.connect(str(db_path)) as conn:
        wrapped = json.loads(
            conn.execute(
                "SELECT structured_output_json FROM distill_chunk_results WHERE session_id = ?",
                ("valid",),
            ).fetchone()[0]
        )
        stored_info = json.loads(
            conn.execute(
                "SELECT chunk_info_json FROM distill_chunk_results WHERE session_id = ?",
                ("valid",),
            ).fetchone()[0]
        )
        wrapped["canonical_output"]["structured_output"]["claims"] = []
        stored_info["canonical_output_hash"] = build_checkpoint_output_hash(
            wrapped["canonical_output"]
        )
        conn.execute(
            "UPDATE distill_chunk_results SET structured_output_json = ?, chunk_info_json = ? "
            "WHERE session_id = ?",
            (json.dumps(wrapped), json.dumps(stored_info), "valid"),
        )
        conn.commit()

    lookup = store.lookup_completed(
        "valid",
        0,
        "valid-hash",
        spec,
        CheckpointAdmissionRequest.for_input_spec(input_spec),
    )
    assert lookup.cache_hit is False
    assert lookup.miss_reason == "checkpoint_output_contract_invalid"


def test_checkpoint_store_reuses_a_legal_skip(tmp_path):
    from core.hephaestus.chunk_checkpoint import (
        CheckpointAdmissionRequest,
        ChunkCheckpointStore,
    )

    store = ChunkCheckpointStore(tmp_path / "chunks.db")
    input_spec = _input_spec()
    spec = _spec(input_spec)
    chunk_info, structured, admission, canonical_output = _skip_checkpoint_parts(input_spec)
    store.save_completed(
        "skip",
        0,
        "skip-hash",
        spec,
        [],
        chunk_info,
        structured,
        admission,
        canonical_output=canonical_output,
        input_spec=input_spec,
    )

    lookup = store.lookup_completed(
        "skip",
        0,
        "skip-hash",
        spec,
        CheckpointAdmissionRequest.for_input_spec(input_spec),
    )
    assert lookup.cache_hit is True
    assert lookup.fragments == ()
    assert lookup.admission is not None
    assert lookup.admission.judgment == "skip"


def test_cleanup_older_than_deletes_stale_checkpoints(tmp_path):
    from core.hephaestus.chunk_checkpoint import (
        CheckpointAdmissionRequest,
        ChunkCheckpointStore,
    )

    db_path = tmp_path / "chunks.db"
    store = ChunkCheckpointStore(db_path)
    old_input = _input_spec(session_id="old", text="old checkpoint input")
    old_spec = _spec(old_input)
    old_parts = _checkpoint_parts(old_input, [_fragment("旧检查点可恢复性验证方案")])
    old_fragments, old_info, old_structured, old_admission, old_output = old_parts
    store.save_completed(
        "old", 0, "hash-old", old_spec, old_fragments, old_info, old_structured,
        old_admission, canonical_output=old_output, input_spec=old_input,
    )
    recent_input = _input_spec(session_id="recent", text="recent checkpoint input")
    recent_spec = _spec(recent_input)
    recent_parts = _checkpoint_parts(recent_input, [_fragment("新检查点可恢复性验证方案")])
    recent_fragments, recent_info, recent_structured, recent_admission, recent_output = recent_parts
    store.save_completed(
        "recent", 0, "hash-new", recent_spec, recent_fragments, recent_info,
        recent_structured, recent_admission, canonical_output=recent_output,
        input_spec=recent_input,
    )
    store.mark_failed(
        "old-failed", 0, "hash-failed", old_spec, "temporary failure"
    )
    store.mark_failed(
        "old-processing",
        0,
        "hash-processing",
        old_spec,
        "future non-terminal state",
    )

    old_time = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            UPDATE distill_chunk_results
            SET updated_at = ?
            WHERE session_id = ?
            """,
            (old_time, "old"),
        )
        conn.execute(
            """
            UPDATE distill_chunk_results
            SET status = 'processing', updated_at = ?
            WHERE session_id = ?
            """,
            (old_time, "old-processing"),
        )
        conn.execute(
            """
            UPDATE distill_chunk_results
            SET updated_at = ?
            WHERE session_id = ?
            """,
            (old_time, "old-failed"),
        )
        conn.commit()

    assert store.cleanup_older_than(30, dry_run=True) == 2
    assert store.load_completed(
        "old",
        0,
        "hash-old",
        old_spec,
        CheckpointAdmissionRequest.for_input_spec(old_input),
    ) is not None

    assert store.cleanup_older_than(30) == 2
    assert store.load_completed(
        "old",
        0,
        "hash-old",
        old_spec,
        CheckpointAdmissionRequest.for_input_spec(old_input),
    ) is None
    assert store.load_completed(
        "recent",
        0,
        "hash-new",
        recent_spec,
        CheckpointAdmissionRequest.for_input_spec(recent_input),
    ) is not None
    with sqlite3.connect(str(db_path)) as conn:
        assert (
            conn.execute(
                "SELECT status FROM distill_chunk_results WHERE session_id = ?",
                ("old-processing",),
            ).fetchone()[0]
            == "processing"
        )
