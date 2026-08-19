#!/usr/bin/env python3
"""Mnemos 惊艳场景 Demo：从一次踩坑到一周后 preflight 提醒。

故事：
1. 用户第一次被 asyncio.gather 的 TimeoutError 坑到，和 Claude 讨论出解决方案。
2. Mnemos 采集这段对话，蒸馏成 Wiki 页面。
3. 一周后用户面临类似任务（"我要用 asyncio.gather 批量调用 API"）。
4. Mnemos preflight 注入之前的经验教训，避免重蹈覆辙。

运行：
    python docs/demo/run_demo.py
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.hephaestus.distillation_engine import (  # noqa: E402
    DistillationEngine,
    KnowledgeFragment,
    ValuePrejudgment,
)


class DemoConfig:
    """隔离的临时配置，避免污染 ~/.mnemos。"""

    def __init__(self, tmpdir: Path):
        self.mnemos_dir = tmpdir / ".mnemos"
        self.data_dir = tmpdir / "data"
        self.database_dir = self.data_dir
        self.wiki_dir = tmpdir / "wiki"
        self.raw_dir = tmpdir / "raw"
        self.obsidian_vault_path = self.raw_dir
        self.claude_data_dir = tmpdir / "claude"
        self._data: dict = {}

    def get(self, key: str, default=None):
        # Demo 中关闭真实 LLM/embedding，避免需要 API key
        if key.startswith("embedding."):
            return False
        return self._data.get(key, default)


def _patch_config(cfg: DemoConfig) -> None:
    import core.config as _config_mod

    _config_mod.reset_config()
    _config_mod.get_config = lambda: cfg  # type: ignore[assignment]


def _make_fragment() -> KnowledgeFragment:
    return KnowledgeFragment(
        # The demo must exercise the same public v4 schema as production;
        # the old English form alias was only tolerated by the legacy fake.
        form="问题-解决",
        title="asyncio.gather 并发请求 TimeoutError 定位与兜底",
        frontmatter={
            "领域": "backend",
            "置信度": 0.9,
            "时效性": "contextual",
            "摘要": (
                "asyncio.gather 默认在任一任务异常时取消其他任务，且裸超时难以定位。"
                "应使用 return_exceptions=True 并为每个任务单独包装 asyncio.wait_for + 日志标记。"
            ),
        },
        background="高并发异步请求场景下，asyncio.gather 的默认行为会导致一个任务失败拖垮整批，并且超时后无法快速定位问题来源。",
        core_content=(
            "## 问题现象\n\n"
            "await asyncio.gather(*tasks) 时，任意任务抛异常会立刻取消其余任务；"
            "若使用整体 timeout，异常堆栈难以对应到具体请求。\n\n"
            "## 解决方案\n\n"
            "1. 每个任务单独 `asyncio.wait_for(task, timeout=...)`，捕获 `asyncio.TimeoutError`。\n"
            "2. `asyncio.gather(..., return_exceptions=True)` 返回结果或异常列表，避免整批失败。\n"
            "3. 给任务命名并在日志中记录 `task_name` 与耗时，便于排错。"
        ),
        boundaries={"applies": "asyncio 并发请求", "not_applies": "同步阻塞 IO"},
        anti_patterns=["裸 gather 不加超时", "整体 timeout 不标记任务"],
        related_concepts=["asyncio", "并发", "超时", "可观测性"],
        claim_ids=["claim-asyncio-gather-timeout"],
        keywords=["asyncio", "gather", "TimeoutError", "并发请求"],
    )


def _fragment_payload(fragment: KnowledgeFragment) -> dict[str, Any]:
    """Return the model-side fragment shape checked by the v4 union."""
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


def _model_evidence(input_spec: Any, quote: str) -> dict[str, str]:
    for entry in input_spec.source_authority_catalog.entries:
        if entry.span_status == "exact" and entry.matches_quote(quote):
            return {
                "source_event_id": entry.source_event_id,
                "source_authority_id": entry.source_authority_id,
                "quote": quote,
            }
    raise RuntimeError("demo evidence quote is not bound to an exact Raw span")


def _make_cognition_episode(
    evidence: dict[str, str],
    *,
    claim_id: str,
) -> dict[str, list[dict[str, Any]]]:
    from core.cognition_episode_contract import COGNITION_EPISODE_FIELDS

    known_values = {
        "situation": "用户正在排查 asyncio.gather 并发请求的 TimeoutError。",
        "facts": "用户确认将采用 return_exceptions=True 和单任务超时包装。",
        "scope": "该结论适用于当前 asyncio 并发请求改造。",
    }
    return {
        field: [
            {
                "status": "known",
                "value": known_values[field],
                "evidence_refs": [dict(evidence)],
                "claim_ids": [claim_id],
            }
            if field in known_values
            else {
                "status": "unknown",
                "reason": f"输入没有提供 {field} 的可靠证据。",
                "evidence_refs": [],
                "claim_ids": [],
            }
        ]
        for field in COGNITION_EPISODE_FIELDS
    }


def _make_structured_output(input_spec: Any) -> dict[str, Any]:
    """Echo the immutable request identity instead of hard-coding source data."""
    claim_id = "claim-asyncio-gather-timeout"
    evidence = _model_evidence(
        input_spec,
        "明白了，我这就去把现有代码改成 return_exceptions=True + 单独超时包装。",
    )
    return {
        "schema_version": "distill_output_v4",
        **input_spec.prompt_contract(),
        "distill_intent": "create",
        "candidate_summary": "asyncio.gather 超时定位和兜底经验",
        "user_behavior_intent": {
            "content_source": "native_dialogue",
            "user_intent_signal": "seeking_judgment",
            "intent_hypothesis": "seeking_judgment",
            "intent_evidence": [
                {
                    **evidence,
                    "reason": "用户明确确认采用并发超时修复方案。",
                }
            ],
            "intent_verification_events": [],
            "intent_confidence": 0.75,
            "intent_status": "unverified",
            "behavior_summary": "用户需要沉淀 asyncio 并发超时排查方法。",
        },
        "claims": [
            {
                "claim_id": claim_id,
                "claim_text": (
                    "当前 asyncio.gather 并发请求将采用单任务超时包装，"
                    "并配合 return_exceptions=True 避免一个失败拖垮整批。"
                ),
                "claim_type": "procedure",
                "scope": {"domain": "backend"},
                "evidence": [dict(evidence)],
                "relation_to_existing": {
                    "type": "new",
                    "target_pages": [],
                    "delta_text": "",
                    "reason": "demo vault 中没有同等页面。",
                },
                "recommended_action": "create_page",
                "cognitive_actions": ["create_observation", "propose_methodology"],
                "confidence": 0.95,
            }
        ],
        "cognition_episode": _make_cognition_episode(
            evidence,
            claim_id=claim_id,
        ),
    }


def _make_extraction_outcome(request: Any) -> Any:
    """Build an admitted typed extraction outcome for the offline demo."""
    from core.hephaestus.distillation_contract import (
        canonical_extraction_output_hash,
        validate_extraction_output,
    )
    from core.hephaestus.distillation_models import ExtractionOutcome
    from core.evidence.source_authority import (
        resolve_model_source_authority_selections,
    )

    fragment = _make_fragment()
    structured = _make_structured_output(request.input_spec)
    payload = {
        "judgment": "knowledge",
        "judgment_reason": "异步超时排查过程可复用于后续并发调用任务。",
        "fragments": [_fragment_payload(fragment)],
        "structured_output": structured,
    }
    authority_resolution = resolve_model_source_authority_selections(
        payload,
        request.input_spec.source_authority_catalog,
    )
    if authority_resolution.issues:
        raise RuntimeError(
            "demo fixture source authority resolution failed: "
            + "; ".join(issue.message for issue in authority_resolution.issues)
        )
    payload = authority_resolution.payload
    admission = validate_extraction_output(payload, request.input_spec)
    if not admission.valid:
        raise RuntimeError(f"demo fixture violates v4 extraction contract: {admission.error_text}")
    return ExtractionOutcome(
        judgment="knowledge",
        fragments=(fragment,),
        structured_output=payload["structured_output"],
        canonical_output=payload,
        admission=admission,
        canonical_output_hash=canonical_extraction_output_hash(
            canonical_output=payload,
        ),
    )


class _DemoExtractor:
    """Typed extraction port for the deterministic demo fixture."""

    def prepare_prompt(self, request: Any) -> Any:
        from core.hephaestus.distill_input_spec import PreparedExtractionPrompt

        return PreparedExtractionPrompt.build(
            "mnemos offline demo extractor v1",
            request,
        )

    def extract(self, request: Any, *, prepared: Any = None) -> Any:
        if prepared is not None:
            prepared.assert_matches(request)
        return _make_extraction_outcome(request)


def _run_capture_and_distill(cfg: DemoConfig) -> list[str]:
    from integrations.sources.claude_source import ClaudeSource
    from core.sync_framework.agent_source import SessionInfo
    from core.sync_framework.raw_event_store import RawEventStore
    from integrations.backends.obsidian_backend import ObsidianBackend
    from core.sync_framework.sync_engine import SyncEngine
    from scripts.project_raw_vault import (
        _fetch_refs,
        build_projection_chunks,
        write_projection,
    )

    fixture = Path(__file__).parent / "fixtures" / "claude_asyncio_gather.jsonl"
    source = ClaudeSource()
    turns = source.parse_turns(fixture)

    backend = ObsidianBackend(vault_path=cfg.raw_dir)
    engine = SyncEngine(
        backend=backend,
        db_path=cfg.database_dir / "sync_log.db",
        config=cfg,
    )
    session = SessionInfo(
        session_id="demo-asyncio-gather",
        source_path=fixture,
        working_dir=str(cfg.raw_dir),
    )
    engine.sync_session(source, session, incremental=False)
    engine.close()

    raw_store = RawEventStore(db_path=cfg.database_dir / "raw_events.db", config=cfg)
    refs = []
    try:
        refs = sorted(
            _fetch_refs(raw_store),
            key=lambda value: (value.turn_number, value.event_id),
        )
        chunks = build_projection_chunks(
            refs,
            chunk_turns=5,
            max_chunks=None,
        )
        write_projection(
            cfg.raw_dir,
            raw_store,
            chunks,
            db_path=cfg.database_dir / "raw_events.db",
            max_turn_chars=0,
        )
    finally:
        raw_store.close()

    from core.cognitive.state_schema import initialize_cognitive_state_schema

    initialize_cognitive_state_schema(
        cfg.database_dir / "producer_consumer_ledger.db"
    )

    if len(refs) != len(turns):
        raise RuntimeError("demo Raw revisions must map one-to-one to parsed turns")
    messages = []
    raw_event_refs = []
    for turn, ref in zip(turns, refs):
        for role, content in (
            ("user", turn.user_content),
            ("assistant", turn.assistant_content),
        ):
            if not content:
                continue
            content_hash = "sha256:" + hashlib.sha256(
                content.encode("utf-8")
            ).hexdigest()
            source_span = {
                "revision_id": ref.event_id,
                "content_hash": content_hash,
                "span_start": 0,
                "span_end": len(content),
                "role": role,
            }
            messages.append(
                {"role": role, "content": content, "source_span": source_span}
            )
            raw_event_refs.append(dict(source_span))

    de = DistillationEngine(
        wiki_base=str(cfg.wiki_dir),
        receipt_config=cfg,
    )
    de._noise_filter = SimpleNamespace(
        filter=lambda messages: (
            messages,
            {"total": len(messages), "noise": 0, "kept": len(messages)},
        )
    )
    de._value_prejudgment = SimpleNamespace(
        judge=lambda messages: (ValuePrejudgment.CERTAINLY_YES, 0.95)
    )
    de._llm_judge = SimpleNamespace(
        judge=lambda session_text, session_id: ("knowledge", "mock", 0.95)
    )
    de._extractor = _DemoExtractor()
    de._self_check = SimpleNamespace(check=lambda fragments, messages: (True, []))
    de._cross_linker = SimpleNamespace(link=lambda fragments: fragments)
    de._feedback_loop = SimpleNamespace(evaluate=lambda result: [])
    de._kia_linker = False

    result = de.process(
        "demo-asyncio-gather",
        messages,
        meta={"source": "claude", "raw_event_refs": raw_event_refs},
    )
    written = de.write_pages(result)
    return written


def _run_search(cfg: DemoConfig) -> list[dict]:
    from core.app.raw_search import RawIndex

    idx = RawIndex(
        raw_dir=cfg.raw_dir,
        db_path=cfg.database_dir / "raw_index.db",
        config=cfg,
    )
    idx.sync_index()
    return idx.search("asyncio gather", limit=5)


def _run_preflight(cfg: DemoConfig) -> int:
    from core.kia.prophasis import PreFlightInjector
    from core.kia.kairos import TimeWindow, TimeWindowType

    injector = PreFlightInjector(wiki_base=str(cfg.wiki_dir))
    tw = TimeWindow(window=TimeWindowType.IMMEDIATE, days_until=0)
    loaded = injector.inject(
        task_type="coding",
        subtype="debug",
        time_window=tw,
        context_text="我要用 asyncio.gather 批量并发调用外部 API",
    )
    return len(loaded.checklist) if loaded else 0


def main() -> int:
    print("=== Mnemos Demo: asyncio.gather pitfall → preflight reminder ===\n")

    with tempfile.TemporaryDirectory() as tmp:
        cfg = DemoConfig(Path(tmp))
        _patch_config(cfg)

        print("[1/4] 采集并同步 Claude 对话到 Raw Vault ...")
        written_pages = _run_capture_and_distill(cfg)
        print(f"       写入 Wiki 页面: {written_pages}")

        print("\n[2/4] 索引 Raw Vault ...")
        hits = _run_search(cfg)
        print(f"       搜索 'asyncio.gather' 命中 {len(hits)} 条")

        print("\n[3/4] 模拟一周后遇到相似任务 ...")
        checklist_count = _run_preflight(cfg)
        print(f"       Preflight 注入 {checklist_count} 条提醒")

        print("\n[4/4] 结果摘要")
        wiki_page = written_pages[0] if written_pages else None
        print(f"       生成的 Wiki 页面: {wiki_page}")
        if wiki_page and Path(wiki_page).exists():
            content = Path(wiki_page).read_text(encoding="utf-8")
            print("\n--- Wiki 页面预览 ---")
            print("\n".join(content.splitlines()[:20]))
            print("---")

        print("\n✅ Demo 完成：过去的踩坑经验已被预注入到当前任务上下文。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
