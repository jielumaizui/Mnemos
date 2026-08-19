#!/usr/bin/env python3
"""User-value centered E2E probe for the Mnemos wow path.

The regular e2e probe proves low-level connectivity. This probe proves the
first-user value loop: configuration readiness, trusted document ingestion,
distillation, Obsidian routing, recall/preflight consumption, consumer ledger
evidence, and auto-heal dry-run planning.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib
import json
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__" and str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

WOW_SCHEMA_VERSION = "mnemos.e2e_wow_probe.v1"
STATUS_PASS = "pass"
STATUS_SKIP = "skip"
STATUS_FAIL = "fail"
DOCUMENT_SESSION_ID = "wow-path-trusted-document"
DOCUMENT_TITLE = "可信文档哇塞链路：用户决策素材直达认知系统"
DOCUMENT_QUERY = "可信文档 哇塞链路 决策素材"


@dataclass
class WowConfig:
    """Isolated config used by the wow probe."""

    root: Path

    def __post_init__(self) -> None:
        self.mnemos_dir = self.root / ".mnemos"
        self.data_dir = self.root / "data"
        self.database_dir = self.data_dir
        self.wiki_dir = self.root / "wiki"
        self.raw_dir = self.root / "raw"
        self.obsidian_vault_path = self.raw_dir
        self.claude_data_dir = self.root / "claude"
        self._data: dict[str, Any] = {
            "document_process.max_file_size_mb": 100,
            "embedding.enabled": False,
            "distill.action_router.enabled": True,
            "distill.auto_expression_formatting": False,
            "auto_heal.enabled": True,
            "auto_heal.user_intervention_budget": 3,
            "auto_heal.record_action_ledger": False,
            "trusted_push.mode": "off",
            "trusted_push.db_path": str(self.database_dir / "trusted_push.db"),
            "llm.provider": "mock",
            "llm.model": "mock-wow-llm",
            "embedding.provider": "mock",
            "embedding.model": "mock-wow-embedding",
            "reranker.provider": "mock",
            "reranker.model": "mock-wow-reranker",
        }

    def ensure_dirs(self) -> None:
        for path in (
            self.mnemos_dir,
            self.data_dir,
            self.wiki_dir,
            self.raw_dir,
            self.claude_data_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)


@contextmanager
def _patched_config(cfg: WowConfig) -> Iterator[None]:
    import core.config as config_mod
    import core.kia.policy as policy_mod
    import core.trust.config as trust_config_mod
    from core.hephaestus import distillation_engine as engine_mod

    original_config_get = config_mod.get_config
    original_engine_get = engine_mod.get_config
    original_policy_get = policy_mod.get_config
    original_trust_get = trust_config_mod.get_config
    original_policy_instance = getattr(policy_mod, "_policy_instance", None)
    config_mod.reset_config()
    config_mod.get_config = lambda: cfg  # type: ignore[assignment, return-value]
    engine_mod.get_config = lambda: cfg  # type: ignore[assignment, return-value]
    policy_mod.get_config = lambda: cfg  # type: ignore[assignment, return-value]
    trust_config_mod.get_config = lambda: cfg  # type: ignore[assignment, return-value]
    policy_mod._policy_instance = None  # type: ignore[attr-defined]
    try:
        yield
    finally:
        policy_mod._policy_instance = original_policy_instance  # type: ignore[attr-defined]
        trust_config_mod.get_config = original_trust_get  # type: ignore[assignment]
        policy_mod.get_config = original_policy_get  # type: ignore[assignment]
        engine_mod.get_config = original_engine_get  # type: ignore[assignment]
        config_mod.get_config = original_config_get  # type: ignore[assignment]
        config_mod.reset_config()


@contextmanager
def _wow_material_action_scope(cfg: WowConfig, mode: str) -> Iterator[None]:
    """Seal real pre-action decisions for this isolated user-value probe."""

    from core.cognitive.decision_trace import (
        DecisionCandidateEvaluation,
        DecisionRejectionEvaluation,
        MaterialActionRequest,
        ProjectContractDecisionContext,
        ProjectContractDecisionEvaluation,
        ProjectContractMaterialActionResolver,
        material_action_resolution_scope,
    )
    from core.cognitive.state_contract import sha256_json
    from core.trust.vault_mutation_service import (
        TRUSTED_MARKDOWN_ACTION_TYPE,
        TRUSTED_MARKDOWN_EXECUTOR,
        TRUSTED_MARKDOWN_OWNER,
    )

    contract_id = "project-contract:e2e-wow-isolated-material-effects"
    contract_revision = "mnemos.e2e_wow_material_effects.v1"
    contract_text = (
        "The explicit wow probe may write only its configured isolated root "
        "and must seal every material effect before execution."
    )
    source_hash = sha256_json(
        {
            "mode": mode,
            "root": str(cfg.root.resolve()),
            "schema_version": WOW_SCHEMA_VERSION,
        }
    )
    source_id = "wow-probe-invocation:" + source_hash.split(":", 1)[1][:32]
    source_facts_hash = sha256_json(
        {
            "schema_version": "mnemos.e2e_wow_evaluation_facts.v1",
            "mode": mode,
            "root": str(cfg.root.resolve()),
            "database_dir": str(cfg.database_dir.resolve()),
            "wiki_dir": str(cfg.wiki_dir.resolve()),
            "source_hash": source_hash,
        }
    )

    def evaluate_request(
        request: MaterialActionRequest,
    ) -> ProjectContractDecisionEvaluation:
        """Admit only the probe's declared hermetic material-action families."""

        request_hash = sha256_json(
            {
                "owner": request.owner,
                "executor_id": request.executor_id,
                "action_type": request.action_type,
                "target_ref": request.target_ref,
                "input_hash": request.input_hash,
            }
        )
        request_ref = f"request-binding:{request_hash}"
        facts_ref = f"source-facts:{source_facts_hash}"
        allowed_families = {
            ("knowledge_delivery", "knowledge_delivery_router", "outward_delivery"),
            (
                TRUSTED_MARKDOWN_OWNER,
                TRUSTED_MARKDOWN_EXECUTOR,
                TRUSTED_MARKDOWN_ACTION_TYPE,
            ),
            ("knowledge_vault", "knowledge_vault_writer", "knowledge_vault_write"),
            ("policy_patch", "policy_patch_store", "policy_patch_propose"),
            ("policy_patch", "policy_patch_store", "policy_patch_feedback"),
            ("policy_patch", "policy_patch_store", "policy_patch_reconcile"),
            ("knowledge_graph", "knowledge_graph", "upsert_relation"),
            ("knowledge_graph", "relation_manager", "upsert_relation"),
            ("cognitive_graph", "cognitive_graph_store", "upsert_relation"),
            ("chronos", "knowledge_scheduler", "create_scheduled_task"),
            ("chronos", "knowledge_scheduler", "execute_scheduled_step"),
            ("auto_healing", "auto_healing_orchestrator", "auto_heal"),
            ("action_ledger", "action_ledger", "auto_heal"),
        }
        family = (request.owner, request.executor_id, request.action_type)
        absolute_paths = [
            part
            for part in request.target_ref.replace("->", ":").split(":")
            if part.startswith("/")
        ]
        paths_inside = all(
            Path(value).expanduser().resolve(strict=False).is_relative_to(
                cfg.root.resolve()
            )
            for value in absolute_paths
        )
        approved = (
            family in allowed_families
            and paths_inside
            and request.input_hash.startswith("sha256:")
        )
        approved_key = "execute_sandbox_bound_wow_effect"
        rejected_key = "reject_effect_outside_wow_sandbox"
        common_refs = (request_ref, facts_ref, source_id, f"wow-mode:{mode}")
        return ProjectContractDecisionEvaluation(
            request_binding_hash=request_hash,
            source_facts_hash=source_facts_hash,
            candidates=(
                DecisionCandidateEvaluation(
                    key=approved_key,
                    summary="Execute the exact effect inside the configured wow sandbox.",
                    supporting_evidence=common_refs if approved else (),
                    opposing_evidence=() if approved else common_refs,
                    satisfies_value_keys=("safety", "project_contract"),
                ),
                DecisionCandidateEvaluation(
                    key=rejected_key,
                    summary="Reject any effect outside the configured wow sandbox.",
                    supporting_evidence=common_refs if not approved else (),
                    opposing_evidence=() if not approved else common_refs,
                    satisfies_value_keys=("safety",),
                ),
            ),
            selection_key=approved_key if approved else rejected_key,
            rejections=(
                DecisionRejectionEvaluation(
                    candidate_key=rejected_key if approved else approved_key,
                    reason_code=(
                        "wow_sandbox_binding_verified"
                        if approved
                        else "wow_sandbox_binding_rejected"
                    ),
                    evidence_refs=common_refs,
                ),
            ),
            expected_outcomes=(
                {
                    "metric": (
                        "sandbox_material_effect_receipt"
                        if approved
                        else "outside_sandbox_effect_count"
                    ),
                    "operator": "equals",
                    "value": 1 if approved else 0,
                },
            ),
            approval_decision="approved" if approved else "rejected",
            approval_evidence_ref=facts_ref,
        )

    resolver = ProjectContractMaterialActionResolver(
        ProjectContractDecisionContext(
            state_db_path=cfg.database_dir / "producer_consumer_ledger.db",
            contract_id=contract_id,
            contract_revision_id=contract_revision,
            contract_text=contract_text,
            contract_evidence_ref=f"{contract_id}#{contract_revision}",
            source_id=source_id,
            source_revision_id=f"{source_id}:{mode}",
            source_content_hash=source_hash,
            source_uri=f"e2e-wow://{mode}/{source_hash.split(':', 1)[1][:16]}",
            evidence_refs=(source_id, f"wow-mode:{mode}"),
            task="Execute the isolated Mnemos wow-path probe",
            goal=(
                "Prove the first-user value loop inside the caller-supplied "
                "sandbox without touching production state."
            ),
            constraints=(
                f"All durable targets must remain under {cfg.root.resolve()}.",
                "Every material effect requires an exact pre-action permit.",
            ),
            created_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            scope_prefix=f"wow-probe:{mode}",
            producer="e2e-wow-probe",
            producer_version=contract_revision,
            producer_code_hash=sha256_json(
                {
                    "module": "scripts.e2e_wow_probe",
                    "schema_version": WOW_SCHEMA_VERSION,
                    "contract": contract_revision,
                }
            ),
            evaluator_id="wow-sandbox-material-evaluator",
            evaluator=evaluate_request,
        )
    )
    with material_action_resolution_scope(resolver):
        yield


def _step(step_id: str, title: str, status: str, message: str, **extra: Any) -> dict[str, Any]:
    payload = {
        "id": step_id,
        "title": title,
        "status": status,
        "message": message,
    }
    payload.update(extra)
    return payload


def _ok_status(steps: Sequence[Mapping[str, Any]]) -> bool:
    return not any(step.get("status") == STATUS_FAIL for step in steps)


def _write_user_document(cfg: WowConfig) -> Path:
    input_dir = cfg.root / "user-documents"
    input_dir.mkdir(parents=True, exist_ok=True)
    path = input_dir / "wow-decision-material.html"
    path.write_text(
        """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>可信文档哇塞链路：用户决策素材直达认知系统</title>
</head>
<body>
  <h1>可信文档哇塞链路：用户决策素材直达认知系统</h1>
  <p>用户主动喂给 Mnemos 的材料不应只变成文件备份。</p>
  <p>它必须经过 100MB 统一可信文档 gate、隐私预扫描、蒸馏、Obsidian 路由，并在后续搜索和 preflight 中被消费。</p>
  <h2>判断标准</h2>
  <ul>
    <li>用户不需要理解 L1/L2/L3 内部层级。</li>
    <li>可信文档默认进入 distill 模式。</li>
    <li>蒸馏页面必须保留行为意图、证据和消费者记录。</li>
    <li>搜索和 preflight 能召回这条经验，减少重复解释和重复决策成本。</li>
  </ul>
</body>
</html>
""",
        encoding="utf-8",
    )
    return path


def _config_step(mode: str, cfg: WowConfig) -> dict[str, Any]:
    if mode != "real_api":
        return _step(
            "config",
            "三项必填模型配置",
            STATUS_PASS,
            "mock 模式下 LLM、embedding、reranker 由本地 provider 替身满足",
            required_configured=3,
            required_total=3,
            providers={"llm": "mock", "embedding": "mock", "reranker": "mock"},
        )

    from core.llm_config import (
        resolve_embedding_api_config,
        resolve_llm_api_chain,
        resolve_reranker_api_config,
    )

    llm = resolve_llm_api_chain(cfg).primary.active()
    embedding = resolve_embedding_api_config(cfg)
    reranker = resolve_reranker_api_config(cfg)
    models: dict[str, Any] = {"llm": llm, "embedding": embedding, "reranker": reranker}
    configured = {name: bool(model.configured) for name, model in models.items()}
    count = sum(1 for ok in configured.values() if ok)
    status = STATUS_PASS if count == 3 else STATUS_FAIL
    return _step(
        "config",
        "三项必填模型配置",
        status,
        f"required configured {count}/3",
        required_configured=count,
        required_total=3,
        providers={name: getattr(model, "provider", "") for name, model in models.items()},
        configured=configured,
    )


def _multimodal_step(mode: str, cfg: WowConfig) -> dict[str, Any]:
    if mode != "real_api":
        return _step(
            "multimodal",
            "可选多模态配置",
            STATUS_SKIP,
            "mock 模式未配置多模态；可选项按可恢复跳过处理",
            optional=True,
        )
    from core.llm_config import resolve_multimodal_api_config

    multimodal = resolve_multimodal_api_config(cfg)
    if multimodal.configured:
        return _step(
            "multimodal",
            "可选多模态配置",
            STATUS_PASS,
            f"multimodal configured: {multimodal.provider}/{multimodal.model}",
            optional=True,
        )
    return _step(
        "multimodal",
        "可选多模态配置",
        STATUS_SKIP,
        "multimodal 未配置；这是可选能力，不阻断文档哇塞链路",
        optional=True,
    )


def _document_import_step(cfg: WowConfig, document_path: Path) -> dict[str, Any]:
    from core.application.document_import_service import DocumentImportService

    service = DocumentImportService(config=cfg)
    result = service.import_document(
        document_path,
        mode="distill",
        title=DOCUMENT_TITLE,
        dry_run=True,
    )
    status = STATUS_PASS if result.get("success") else STATUS_FAIL
    return _step(
        "document_import",
        "可信文档 gate 与默认 distill 入口",
        status,
        str(result.get("message", "")),
        mode=result.get("mode"),
        source_hash=result.get("source_hash"),
        content_size=result.get("content_size"),
        max_file_size_mb=result.get("max_file_size_mb"),
        max_file_size_config_key=result.get("max_file_size_config_key"),
        privacy_scan=result.get("privacy_scan"),
        quality_decision=result.get("quality_decision"),
        action_ledger_ref=result.get("action_ledger_ref", ""),
    )


def _mock_fragment() -> Any:
    from core.hephaestus.distillation_engine import KnowledgeFragment

    return KnowledgeFragment(
        # Keep the demo fixture on the same public extraction vocabulary as
        # the canonical v3 JSON Schema.  The previous English alias was only
        # accepted by the old list-return fake and would mask schema drift.
        form="方法论",
        title=DOCUMENT_TITLE,
        frontmatter={
            "领域": "mnemos",
            "摘要": "可信文档哇塞链路证明用户主动喂文档后能自动形成可召回的认知资产。",
            "置信度": 0.93,
            "时效性": "stable",
            "scope": "project",
            "source_agent": "trusted_user_document",
            "session_id": DOCUMENT_SESSION_ID,
            "project": "mnemos",
            "acl_schema_version": 1,
            "acl_metadata_complete": True,
            "acl_reconciliation_status": "server_principal",
            "source_event_ids": ["trusted_user_document:wow-path"],
            "evidence_refs": ["scripts/e2e_wow_probe.py --mock-llm"],
            "triggers": ["下次验证可信文档导入或新用户首次配置时复用"],
        },
        background="用户第一次把决策材料交给 Mnemos 时，需要看到从输入到召回的完整价值闭环。",
        core_content=(
            "## 哇塞链路验收\n\n"
            "决定把可信文档导入验收做成一条用户价值链路，而不是只验证内部模块可导入。"
            "可信文档必须经过 `document_process.max_file_size_mb=100` 的统一 gate，"
            "再进入蒸馏、Obsidian 路由、搜索召回和 preflight 消费；guard 和 scorecard "
            "也能把这条链路当作 full-score 证据。\n\n"
            "## 用户价值\n\n"
            "用户不需要理解 L1/L2/L3 内部层级，也不需要手动复制结论。"
            "后续相似任务中，Mnemos 应主动召回这条决策素材，减少重复解释和重复决策成本。\n\n"
            "## 下次触发\n\n"
            "如果用户再次要求验证可信文档、首次配置、哇塞链路、Obsidian 路由或 preflight，"
            "复用本方法：先跑 `python3 scripts/e2e_wow_probe.py --mock-llm`，再检查报告中的 "
            "Wiki 页面、search 命中、preflight 提醒和 consumer ledger。验证证据："
            "`python3 -m pytest tests/e2e/test_wow_path.py`。\n"
        ),
        boundaries={
            "applies": "用户主动喂给 Mnemos 的可信文档、决策素材、复盘材料",
            "not_applies": "系统临时目录文件、raw vault 自身文件、超过大小上限的文件",
        },
        anti_patterns=["只保存文档不蒸馏", "只生成 Wiki 页面但没有消费者"],
        related_concepts=["trusted_user_document", "Obsidian 路由", "context search", "preflight"],
        keywords=["可信文档", "哇塞链路", "决策素材", "preflight"],
        claim_ids=["claim-wow-path-trusted-document"],
    )


def _fragment_payload(fragment: Any) -> dict[str, Any]:
    """Return the model-side fragment shape admitted by the v4 union."""
    return {
        "form": fragment.form,
        "title": fragment.title,
        "frontmatter": dict(fragment.frontmatter),
        "background": fragment.background,
        "core_content": fragment.core_content,
        "boundaries": dict(fragment.boundaries),
        "anti_patterns": list(fragment.anti_patterns),
        "related_concepts": list(fragment.related_concepts),
        "relations": list(fragment.relations),
        "claim_ids": list(fragment.claim_ids),
    }


def _structured_output(input_spec: Any) -> dict[str, Any]:
    """Build output by echoing the request-owned immutable input contract."""
    from core.cognition_episode_contract import COGNITION_EPISODE_FIELDS

    authority = next(
        entry
        for entry in input_spec.source_authority_catalog.entries
        if entry.span_status == "exact"
    )
    evidence = {
        "source_event_id": authority.source_event_id,
        "source_authority_id": authority.source_authority_id,
        "quote": authority._verifiable_text,
    }
    cognition_episode = {
        field: [
            {
                "status": "known",
                "value": f"可信文档哇塞链路的 {field} 认知",
                "evidence_refs": [dict(evidence)],
                "claim_ids": ["claim-wow-path-trusted-document"],
            }
            if field in {"situation", "facts", "scope"}
            else {
                "status": "unknown",
                "reason": f"该验收材料没有提供 {field} 的可靠证据。",
                "evidence_refs": [],
                "claim_ids": [],
            }
        ]
        for field in COGNITION_EPISODE_FIELDS
    }
    return {
        "schema_version": "distill_output_v4",
        **input_spec.prompt_contract(),
        "distill_intent": "create",
        "candidate_summary": "可信文档哇塞链路验收",
        "user_behavior_intent": {
            "content_source": "external_file",
            "user_intent_signal": "curate_or_decision_material",
            "intent_hypothesis": "curate_or_decision_material",
            "intent_evidence": [
                {
                    **dict(evidence),
                    "reason": "用户提供外部决策素材，希望系统沉淀并在后续任务消费。",
                }
            ],
            "intent_verification_events": [
                {
                    "event_id": "wow-path-context-search",
                    **dict(evidence),
                    "status": "verified",
                }
            ],
            "intent_confidence": 0.82,
            "intent_status": "verified",
            "behavior_summary": "用户需要把外部决策材料转成后续可召回的认知资产。",
        },
        "claims": [
            {
                "claim_id": "claim-wow-path-trusted-document",
                "claim_text": (
                    "可信用户文档的黄金路径必须覆盖大小 gate、蒸馏、路由、召回、"
                    "preflight 消费和 auto-heal dry-run。"
                ),
                "claim_type": "procedure",
                "scope": {"domain": "mnemos"},
                "evidence": [dict(evidence)],
                "relation_to_existing": {
                    "type": "new",
                    "target_pages": [],
                    "delta_text": "",
                    "reason": "临时测试 vault 中没有同等页面。",
                },
                "recommended_action": "create_page",
                "cognitive_actions": ["create_observation", "propose_methodology"],
                "confidence": 0.93,
            }
        ],
        "cognition_episode": cognition_episode,
    }


def _mock_extraction_outcome(request: Any) -> Any:
    """Return an admitted typed fake, never a list plus side-channel state."""
    from core.hephaestus.distillation_contract import (
        canonical_extraction_output_hash,
        validate_extraction_output,
    )
    from core.hephaestus.distillation_models import ExtractionOutcome
    from core.evidence.source_authority import resolve_model_source_authority_selections

    fragment = _mock_fragment()
    structured = _structured_output(request.input_spec)
    payload = {
        "judgment": "knowledge",
        "judgment_reason": "可信文档包含可复用的用户决策和验收方法。",
        "fragments": [_fragment_payload(fragment)],
        "structured_output": structured,
    }
    resolution = resolve_model_source_authority_selections(
        payload,
        request.input_spec.source_authority_catalog,
    )
    if resolution.issues:
        raise RuntimeError(f"wow probe source resolution failed: {resolution.issues}")
    payload = resolution.payload
    structured = payload["structured_output"]
    admission = validate_extraction_output(payload, request.input_spec)
    if not admission.valid:
        raise RuntimeError(f"wow probe fixture violates v4 extraction contract: {admission.error_text}")
    return ExtractionOutcome(
        judgment="knowledge",
        fragments=(fragment,),
        structured_output=structured,
        canonical_output=payload,
        admission=admission,
        canonical_output_hash=canonical_extraction_output_hash(
            canonical_output=payload,
        ),
    )


class _MockWowExtractor:
    """Typed COG-011 test port used by the offline wow probe."""

    @staticmethod
    def prepare_prompt(request: Any) -> Any:
        from core.hephaestus.distill_input_spec import PreparedExtractionPrompt

        return PreparedExtractionPrompt.build("offline wow probe prompt", request)

    def extract(self, request: Any, *, prepared: Any = None) -> Any:
        if prepared is not None:
            prepared.assert_matches(request)
        return _mock_extraction_outcome(request)


def _mock_distill_step(cfg: WowConfig) -> tuple[dict[str, Any], str]:
    from core.cognitive.state_schema import initialize_cognitive_state_schema
    from core.cognitive.state_store import CognitiveStateStore
    from core.hephaestus.distillation_engine import DistillationEngine, ValuePrejudgment
    from core.sync_framework.raw_event_store import RawEventStore

    initialize_cognitive_state_schema(
        cfg.database_dir / "producer_consumer_ledger.db"
    )
    engine = DistillationEngine(
        wiki_base=str(cfg.wiki_dir),
        receipt_config=cfg,
    )
    setattr(
        engine,
        "_noise_filter",
        SimpleNamespace(
            filter=lambda messages: (
                messages,
                {"total": len(messages), "noise": 0, "kept": len(messages)},
            )
        ),
    )
    setattr(
        engine,
        "_value_prejudgment",
        SimpleNamespace(judge=lambda messages: (ValuePrejudgment.CERTAINLY_YES, 0.95)),
    )
    setattr(
        engine,
        "_llm_judge",
        SimpleNamespace(
            judge=lambda session_text, session_id: ("knowledge", "mock-wow", 0.95)
        ),
    )
    setattr(engine, "_extractor", _MockWowExtractor())
    setattr(engine, "_self_check", SimpleNamespace(check=lambda fragments, messages: (True, [])))
    setattr(engine, "_cross_linker", SimpleNamespace(link=lambda fragments: fragments))
    setattr(engine, "_feedback_loop", SimpleNamespace(evaluate=lambda result: []))
    setattr(engine, "_kia_linker", False)

    message_specs = (
        (
            "user",
            "请把这份可信文档沉淀为后续可召回的 Mnemos 哇塞链路验收知识。"
            "用户主动喂给 Mnemos 的材料不应只变成文件备份；"
            "可信文档默认进入 distill 模式。",
        ),
        ("assistant", "会保留行为意图、证据、路由和消费者记录。"),
    )
    messages = []
    raw_event_refs = []
    raw_store = RawEventStore(config=cfg)
    try:
        for turn, (role, content) in enumerate(message_specs, start=1):
            revision_id = raw_store.upsert_turn(
                source_agent="trusted_user_document",
                session_id=DOCUMENT_SESSION_ID,
                turn_number=turn,
                user_content=content if role == "user" else "",
                assistant_content=content if role == "assistant" else "",
                metadata={"native_event_id": f"wow:{turn}:{role}"},
            )
            raw_turn = raw_store.get_turn(revision_id)
            if raw_turn is None:
                raise RuntimeError(f"wow probe Raw revision missing: {revision_id}")
            source_span = {
                "revision_id": revision_id,
                "logical_event_id": str(raw_turn["logical_event_id"]),
                "turn_number": turn,
                "content_hash": str(raw_turn["content_hash"]),
                "role": role,
                "span_start": 0,
                "span_end": len(content),
            }
            messages.append(
                {
                    "role": role,
                    "content": content,
                    "turn": turn,
                    "turn_number": turn,
                    "source_span": source_span,
                }
            )
            raw_event_refs.append(dict(source_span))
    finally:
        raw_store.close()
    result = engine.process(
        DOCUMENT_SESSION_ID,
        messages,
        meta={
            "source": "trusted_user_document",
            "content_source": "external_file",
            "raw_event_refs": raw_event_refs,
        },
    )
    written = engine.write_pages(result)
    wiki_page = written[0] if written else ""
    episode_receipt = result.cognition_episode_receipt
    episode_revision_id = str(result.cognition_episode_revision_id or "")
    episode_revision = (
        CognitiveStateStore(cfg).revision(episode_revision_id)
        if episode_revision_id
        else None
    )
    outbox_ids = list(getattr(episode_receipt, "outbox_ids", ()) or ())
    page_binds_episode = bool(
        wiki_page
        and episode_revision_id
        and episode_revision_id
        in Path(wiki_page).read_text(encoding="utf-8")
    )
    status = (
        STATUS_PASS
        if wiki_page
        and episode_revision is not None
        and episode_revision.object_type == "cognition_episode"
        and getattr(episode_receipt, "status", "") in {"committed", "existing"}
        and len(outbox_ids) == 3
        and page_binds_episode
        else STATUS_FAIL
    )
    structured = result.structured_output or {}
    behavior = structured.get("user_behavior_intent") or {}
    return (
        _step(
            "distill",
            "Mock LLM 蒸馏、认知事件与行为意图",
            status,
            f"wiki pages={len(written)}, cognition outbox={len(outbox_ids)}",
            wiki_paths=written,
            cognition_episode_revision_id=episode_revision_id,
            cognition_episode_status=getattr(episode_receipt, "status", ""),
            cognition_episode_outbox_count=len(outbox_ids),
            cognition_episode_object_type=(
                episode_revision.object_type if episode_revision is not None else ""
            ),
            wiki_binds_cognition_episode=page_binds_episode,
            content_source=behavior.get("content_source"),
            intent_hypothesis=behavior.get("intent_hypothesis"),
            intent_status=behavior.get("intent_status"),
            evidence_count=len(behavior.get("intent_evidence") or []),
        ),
        wiki_page,
    )


def _real_distill_step(cfg: WowConfig, document_path: Path) -> tuple[dict[str, Any], str]:
    from core.application.document_import_service import DocumentImportService

    service = DocumentImportService(config=cfg)
    result = service.import_document(
        document_path,
        mode="distill",
        title=DOCUMENT_TITLE,
        dry_run=False,
    )
    wiki_paths = [str(path) for path in result.get("wiki_paths", [])]
    status = STATUS_PASS if result.get("success") and wiki_paths else STATUS_FAIL
    return (
        _step(
            "distill",
            "真实 API 蒸馏与行为意图",
            status,
            str(result.get("message", "")),
            wiki_paths=wiki_paths,
            provider=result.get("provider", ""),
            model=result.get("model", ""),
            quality_decision=result.get("quality_decision", ""),
            routing_result=result.get("routing_result", {}),
        ),
        wiki_paths[0] if wiki_paths else "",
    )


def _wiki_route_step(wiki_page: str) -> dict[str, Any]:
    if not wiki_page:
        return _step("wiki_route", "Obsidian 自动路由", STATUS_FAIL, "未生成 Wiki 页面")
    path = Path(wiki_page)
    route_status = "needs_review" if "00-Inbox" in path.parts else "routed"
    return _step(
        "wiki_route",
        "Obsidian 自动路由",
        STATUS_PASS,
        f"{route_status}: {path.relative_to(path.parents[1]) if len(path.parents) > 1 else path.name}",
        route_status=route_status,
        wiki_page=str(path),
    )


def _recall_step(cfg: WowConfig, wiki_page: str) -> tuple[dict[str, Any], int, int]:
    if not wiki_page:
        return _step("recall", "搜索与 preflight 召回", STATUS_FAIL, "缺少 Wiki 页面"), 0, 0
    from core.app.context_search import ContextAwareSearch
    from core.access_policy import AccessNarrowing, PrincipalEnvelope
    from core.kia.kairos import TimeWindow, TimeWindowType
    from core.kia.prophasis import PreFlightInjector

    search = ContextAwareSearch(wiki_base=str(cfg.wiki_dir))
    principal = PrincipalEnvelope(
        principal_id="e2e-wow:trusted-user-document",
        agent="trusted_user_document",
        host_kind="test",
        capability_id="e2e-wow-recall",
        capabilities=frozenset({"memory_read"}),
        allowed_projects=frozenset({"mnemos"}),
    )
    hits = search.search(
        DOCUMENT_QUERY,
        limit=5,
        principal=principal,
        narrowing=AccessNarrowing(project="mnemos"),
    )
    injector = PreFlightInjector(wiki_base=str(cfg.wiki_dir))
    loaded = injector.inject(
        task_type="coding",
        subtype="wow_path",
        time_window=TimeWindow(window=TimeWindowType.IMMEDIATE, days_until=0),
        context_text="我要验证可信文档哇塞链路和后续 preflight 召回",
    )
    reminder_count = len(loaded.checklist) if loaded else 0
    status = STATUS_PASS if hits and reminder_count else STATUS_FAIL
    return (
        _step(
            "recall",
            "搜索与 preflight 召回",
            status,
            f"context_search={len(hits)}, preflight={reminder_count}",
            search_hits=len(hits),
            preflight_reminders=reminder_count,
            top_hit=getattr(hits[0], "page_path", "") if hits else "",
        ),
        len(hits),
        reminder_count,
    )


def _consumer_ledger_step(cfg: WowConfig, source_hash: str, search_hits: int, reminders: int) -> dict[str, Any]:
    from core.ops.producer_consumer_ledger import ProducerConsumerLedger

    flow_id = "wow_path_trusted_document_to_recall"
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_flow(
        flow_id=flow_id,
        data_type="trusted user document wow-path knowledge",
        producer_refs=["scripts/e2e_wow_probe.py:document_import"],
        consumer_refs=[
            "core/app/context_search.py:ContextAwareSearch",
            "core/kia/prophasis.py:PreFlightInjector",
        ],
        pending_budget=0,
        dead_letter_budget=0,
    )
    item_id = source_hash or DOCUMENT_SESSION_ID
    ledger.record_produced(
        flow_id,
        source="trusted_user_document",
        item_id=item_id,
        metadata={"mode": "wow_probe"},
    )
    if search_hits and reminders:
        for consumer in (
            "core/app/context_search.py:ContextAwareSearch",
            "core/kia/prophasis.py:PreFlightInjector",
        ):
            ledger.record_consumed(
                flow_id,
                source=consumer,
                item_id=item_id,
                metadata={"hits": search_hits, "reminders": reminders},
            )
    snapshot = ledger.snapshot()
    flow = snapshot.get("flows", {}).get(flow_id, {})
    status = STATUS_PASS if flow.get("status") == "ok" else STATUS_FAIL
    return _step(
        "consumer_ledger",
        "消费者运行时对账",
        status,
        f"producer={flow.get('produced_count')}, consumed={flow.get('consumed_count')}",
        flow_id=flow_id,
        produced_count=flow.get("produced_count"),
        consumed_count=flow.get("consumed_count"),
        pending_count=flow.get("pending_count"),
        orphan_item_count=flow.get("orphan_item_count"),
        no_source_item_count=flow.get("no_source_item_count"),
        db_path=str(ledger.db_path),
    )


def _auto_heal_step(cfg: WowConfig) -> dict[str, Any]:
    from core.ops.auto_healing import build_health_auto_heal_report

    checks = {
        "multimodal": {
            "status": "skipped",
            "error": "optional multimodal not configured",
            "repair_actions": ["Set MNEMOS_MULTIMODAL_API_KEY if image ingestion is required."],
        },
    }
    report = build_health_auto_heal_report(cfg, checks, apply=False)
    budget = report.get("user_intervention_budget", {})
    user_count = int(budget.get("used", 0) or 0)
    blocking = [
        issue
        for issue in report.get("issues", [])
        if issue.get("status") not in {"ignored_with_reason"}
        and issue.get("risk_level") in {"high", "critical"}
    ]
    status = STATUS_PASS if not blocking else STATUS_FAIL
    return _step(
        "auto_heal",
        "自愈 dry-run 计划",
        status,
        f"mode={report.get('mode')}, user_interventions={user_count}",
        mode=report.get("mode"),
        user_interventions=user_count,
        issues=report.get("issues", []),
    )


def _dry_run_report(cfg: WowConfig) -> dict[str, Any]:
    steps = [
        _config_step("mock_llm", cfg),
        _multimodal_step("mock_llm", cfg),
        _dry_run_imports_step(),
        _step(
            "dry_run",
            "只读 wow path 合同检查",
            STATUS_PASS,
            "dry-run 只检查可导入模块、配置合同和执行计划，不写临时 vault",
            writes=[],
            planned_steps=[
                "trusted document gate",
                "mock distill",
                "wiki route",
                "context search",
                "preflight",
                "auto-heal dry-run",
            ],
        ),
    ]
    return _build_report("dry_run", cfg, steps, wiki_page="", search_hits=0, reminders=0)


def _dry_run_imports_step() -> dict[str, Any]:
    modules = [
        "core.application.document_import_service",
        "core.hephaestus.distillation_engine",
        "core.app.context_search",
        "core.kia.prophasis",
        "core.ops.auto_healing",
        "core.ops.producer_consumer_ledger",
    ]
    failures = []
    for module in modules:
        try:
            importlib.import_module(module)
        except (ImportError, OSError, RuntimeError, AttributeError) as exc:
            failures.append(f"{module}: {exc}")
    if failures:
        return _step(
            "imports",
            "wow path 关键模块导入",
            STATUS_FAIL,
            "; ".join(failures),
            checked=len(modules),
        )
    return _step(
        "imports",
        "wow path 关键模块导入",
        STATUS_PASS,
        f"关键模块可导入 ({len(modules)} 个)",
        checked=len(modules),
    )


def _build_report(
    mode: str,
    cfg: WowConfig,
    steps: list[dict[str, Any]],
    *,
    wiki_page: str,
    search_hits: int,
    reminders: int,
) -> dict[str, Any]:
    hard_failures = [step for step in steps if step["status"] == STATUS_FAIL]
    user_interventions = sum(int(step.get("user_interventions", 0) or 0) for step in steps)
    if hard_failures:
        user_interventions += len(hard_failures)
    visible_report = (
        "可信文档已形成 Wiki 知识页，并被搜索/preflight 消费。"
        if wiki_page
        else "dry-run 已验证哇塞链路执行计划；未写入临时 Wiki 或数据库。"
    )
    return {
        "schema_version": WOW_SCHEMA_VERSION,
        "ok": _ok_status(steps),
        "mode": mode,
        "root": str(cfg.root),
        "user_intervention_count": user_interventions,
        "steps": steps,
        "final_value": {
            "wiki_page": wiki_page,
            "search_hits": search_hits,
            "preflight_reminders": reminders,
            "user_visible_report": visible_report,
        },
        "artifacts": {
            "wiki_page": wiki_page,
            "database_dir": str(cfg.database_dir),
            "wiki_dir": str(cfg.wiki_dir),
        },
    }


def run_wow_probe(
    *,
    mode: str,
    root: Path | None = None,
    emit: bool = True,
) -> dict[str, Any]:
    if mode not in {"dry_run", "mock_llm", "real_api"}:
        raise ValueError(f"unsupported wow probe mode: {mode}")
    cfg = WowConfig((root or Path(tempfile.mkdtemp(prefix="mnemos-wow-probe-"))).resolve())
    if mode == "dry_run":
        report = _dry_run_report(cfg)
        if emit:
            _print_report(report)
        return report

    cfg.ensure_dirs()
    with _patched_config(cfg), _wow_material_action_scope(cfg, mode):
        document_path = _write_user_document(cfg)
        steps: list[dict[str, Any]] = [
            _config_step(mode, cfg),
            _multimodal_step(mode, cfg),
        ]
        doc_step = _document_import_step(cfg, document_path)
        steps.append(doc_step)
        source_hash = str(doc_step.get("source_hash") or "")

        if any(step["status"] == STATUS_FAIL for step in steps):
            report = _build_report(mode, cfg, steps, wiki_page="", search_hits=0, reminders=0)
            if emit:
                _print_report(report)
            return report

        if mode == "real_api":
            distill_step, wiki_page = _real_distill_step(cfg, document_path)
        else:
            distill_step, wiki_page = _mock_distill_step(cfg)
        steps.append(distill_step)
        steps.append(_wiki_route_step(wiki_page))
        recall_step, search_hits, reminders = _recall_step(cfg, wiki_page)
        steps.append(recall_step)
        if source_hash:
            steps.append(_consumer_ledger_step(cfg, source_hash, search_hits, reminders))
        steps.append(_auto_heal_step(cfg))
        report = _build_report(
            mode,
            cfg,
            steps,
            wiki_page=wiki_page,
            search_hits=search_hits,
            reminders=reminders,
        )
    if emit:
        _print_report(report)
    return report


def _status_symbol(status: str) -> str:
    if status == STATUS_PASS:
        return "[PASS]"
    if status == STATUS_SKIP:
        return "[SKIP]"
    return "[FAIL]"


def _print_report(report: Mapping[str, Any]) -> None:
    print("Mnemos Wow Path E2E Probe")
    print("=" * 60)
    print(f"schema: {report.get('schema_version')}")
    print(f"mode: {report.get('mode')}")
    print(f"ok: {report.get('ok')}")
    print(f"user_intervention_count: {report.get('user_intervention_count')}")
    print("")
    for step in report.get("steps", []):
        if not isinstance(step, Mapping):
            continue
        print(f"{_status_symbol(str(step.get('status')))} {step.get('title')}: {step.get('message')}")
    final = report.get("final_value", {})
    print("")
    print("Final Value")
    print("- wiki_page:", final.get("wiki_page", ""))
    print("- search_hits:", final.get("search_hits", 0))
    print("- preflight_reminders:", final.get("preflight_reminders", 0))
    print("- report:", final.get("user_visible_report", ""))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mnemos wow path E2E probe")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="只检查合同和执行计划，不写入")
    mode.add_argument("--mock-llm", action="store_true", help="使用临时目录和 mock LLM 跑完整哇塞链路")
    mode.add_argument("--real-api", action="store_true", help="使用真实 API 跑完整哇塞链路")
    parser.add_argument("--root", type=Path, default=None, help="隔离运行根目录；默认创建临时目录")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    mode = "dry_run" if args.dry_run else "real_api" if args.real_api else "mock_llm"
    report = run_wow_probe(mode=mode, root=args.root, emit=not args.json)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
