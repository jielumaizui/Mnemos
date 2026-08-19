# -*- coding: utf-8 -*-
"""Fragment remediation, validation, and quality-gate helpers."""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable
from typing import Any, Dict, List, Tuple

from core.config import get_config
from core.frontmatter import fm_get
from core.hephaestus.distillation_models import KnowledgeFragment
from core.hephaestus.distillation_self_check import (
    classify_self_check_issue,
    max_self_check_severity,
)

FRAGMENT_BOUNDARY_CHARS = 8000
MIN_CORE_CONTENT_CHARS = 100

logger = logging.getLogger(__name__)


_DOMAIN_KEYWORDS = {
    "backend": [
        "后端",
        "server",
        "api",
        "database",
        "db",
        "redis",
        "python",
        "java",
        "go",
        "node",
        "sql",
        "服务",
        "接口",
        "缓存",
        "并发",
        "flask",
        "django",
        "fastapi",
        "spring",
    ],
    "frontend": [
        "前端",
        "react",
        "vue",
        "html",
        "css",
        "ui",
        "界面",
        "javascript",
        "typescript",
        "angular",
        "webpack",
    ],
    "ai": [
        "模型",
        "llm",
        "gpt",
        "训练",
        "推理",
        "embedding",
        "向量",
        "人工智能",
        "大模型",
        "langchain",
        "rag",
    ],
    "devops": [
        "docker",
        "k8s",
        "kubernetes",
        "部署",
        "ci/cd",
        "运维",
        "监控",
        "linux",
        "nginx",
        "aws",
    ],
    "product": ["产品", "需求", "用户", "功能", "prd"],
    "operation": ["运营", "数据", "指标", "转化", "增长", "留存", "gmv"],
    "management": ["管理", "团队", "流程", "决策", "组织", "绩效"],
    "design": ["设计", "ui", "ux", "视觉", "交互", "figma"],
}


def _infer_domain(text: str) -> str:
    """从文本关键词推断领域，失败时返回通用。"""
    if not text:
        return "通用"
    lower = text.lower()
    for domain, keywords in _DOMAIN_KEYWORDS.items():
        if any(k in lower for k in keywords):
            return domain
    return "通用"


def _first_sentence(text: str, max_len: int = 120) -> str:
    """抽取文本第一句（按常见标点分割），用于自动生成摘要或标题。"""
    if not text:
        return ""
    text = text.strip().replace("\r\n", "\n")
    for delim in ("。", "；", "!", "！", "?", "？", "\n", ". "):
        if delim in text:
            sent = text.split(delim, 1)[0].strip()
            if sent:
                return sent[:max_len]
    return text[:max_len].strip()


def _ensure_frontmatter(fragment: KnowledgeFragment) -> bool:
    """保证 frontmatter 为 dict。"""
    if isinstance(fragment.frontmatter, dict):
        return False
    fragment.frontmatter = {}
    return True


def _remediate_title(fragment: KnowledgeFragment) -> bool:
    """修复标题缺失/过短。"""
    changed = False
    title = (fragment.title or "").strip()
    if not title or title.startswith(("无标题", "未知")):
        content_hint = _first_sentence(fragment.core_content or fragment.background or "", 40)
        fragment.title = (
            f"{fragment.form or '知识'}：{content_hint}" if content_hint else "未命名知识片段"
        )
        changed = True
        title = fragment.title

    if len(title) < 10:
        fm = fragment.frontmatter if isinstance(fragment.frontmatter, dict) else {}
        addon = (fm.get("领域") or "").strip() or (fragment.form or "知识")
        title = f"{title}（{addon}）"
        if len(title) < 10:
            title = f"{title}的完整记录"
        fragment.title = title
        changed = True

    return changed


def _build_extra_sections(fragment: KnowledgeFragment) -> List[str]:
    """从片段已有上下文组装扩展正文段落。"""
    sections: List[str] = []
    background = (fragment.background or "").strip()
    if background:
        sections.append(f"## 背景\n\n{background}")
    if isinstance(fragment.boundaries, dict) and fragment.boundaries:
        boundary_lines = [
            f"- {key}: {value}" for key, value in fragment.boundaries.items() if str(value).strip()
        ]
        if boundary_lines:
            sections.append("## 适用边界\n\n" + "\n".join(boundary_lines))
    if fragment.anti_patterns:
        sections.append(
            "## 反模式/注意事项\n\n"
            + "\n".join(f"- {item}" for item in fragment.anti_patterns if str(item).strip())
        )
    if fragment.related_concepts:
        sections.append(
            "## 相关概念\n\n"
            + "\n".join(f"- {item}" for item in fragment.related_concepts if str(item).strip())
        )
    if fragment.keywords:
        sections.append(
            "## 关键词\n\n"
            + "\n".join(f"- {item}" for item in fragment.keywords if str(item).strip())
        )
    return sections


def _remediate_core_content(fragment: KnowledgeFragment) -> bool:
    """核心内容过短时，用已有上下文扩展正文。"""
    content = (fragment.core_content or "").strip()
    if len(content) >= MIN_CORE_CONTENT_CHARS:
        return False

    extra_sections = _build_extra_sections(fragment)
    if not extra_sections:
        return False

    base = content
    if base and not re.search(r"^#{1,3}\s", base, re.MULTILINE) and "```" not in base:
        base = f"## 核心内容\n\n{base}"
    fragment.core_content = "\n\n".join(part for part in [base, *extra_sections] if part)
    return True


def _ensure_structure(fragment: KnowledgeFragment) -> bool:
    """缺少 Markdown 结构标记时 prepend 二级标题。"""
    content = (fragment.core_content or "").strip()
    has_structure = bool(re.search(r"^#{1,3}\s", content, re.MULTILINE) or "```" in content)
    if has_structure:
        return False
    fragment.core_content = f"## 核心内容\n\n{content}"
    return True


def _remediate_summary(fragment: KnowledgeFragment) -> bool:
    """frontmatter 摘要缺失/过短时从正文第一句生成。"""
    fm = fragment.frontmatter
    summary = fm.get("摘要", "")
    if summary and isinstance(summary, str) and len(summary.strip()) >= 5:
        return False

    base = (fragment.core_content or fragment.background or fragment.title or "").strip()
    sent = _first_sentence(base, 150)
    if len(sent) < 5:
        sent = f"{fragment.title}：{sent}"
    fm["摘要"] = sent
    return True


def _remediate_domain(fragment: KnowledgeFragment) -> bool:
    """frontmatter 领域缺失/过短时从关键词推断。"""
    fm = fragment.frontmatter
    domain = fm.get("领域", "")
    if domain and isinstance(domain, str) and len(domain.strip()) >= 2:
        return False
    fm["领域"] = _infer_domain(f"{fragment.title} {fragment.core_content}")
    return True


def _ensure_boundary(fragment: KnowledgeFragment, config_getter: Callable[[], Any]) -> bool:
    """超长内容且无边界时补充兜底边界说明。"""
    cfg = config_getter()
    fragment_boundary_chars = int(
        cfg.get("distill.fragment_boundary_chars", FRAGMENT_BOUNDARY_CHARS)
        or FRAGMENT_BOUNDARY_CHARS
    )
    content = (fragment.core_content or "").strip()
    if len(content) <= fragment_boundary_chars or fragment.boundaries:
        return False
    fragment.boundaries = {"applies": "详见核心内容", "not_applies": "未在对话中验证的场景"}
    return True


def _auto_remediate_fragment(
    fragment: KnowledgeFragment,
    config_getter: Callable[[], Any] = get_config,
) -> bool:
    """对常见硬校验失败进行确定性自动修复，从根因上提升片段质量。

    修复策略：
    1. 标题缺失/过短：从形态 + 内容生成，或补充领域/形态后缀。
    2. 核心内容过短：把 background、boundaries、anti_patterns、related_concepts
       组装成结构化正文；补足后仍不足硬标准则保持失败。
    3. 缺少结构化标记：prepend Markdown 标题。
    4. frontmatter 摘要缺失/过短：从正文第一句生成。
    5. frontmatter 领域缺失：从关键词推断。
    6. 超长内容无边界：补充兜底边界说明。

    Returns:
        是否发生了变更。
    """
    if fragment is None:
        return False

    changed = _ensure_frontmatter(fragment)
    changed = _remediate_title(fragment) or changed
    changed = _remediate_core_content(fragment) or changed
    changed = _ensure_structure(fragment) or changed
    changed = _remediate_summary(fragment) or changed
    changed = _remediate_domain(fragment) or changed
    changed = _ensure_boundary(fragment, config_getter) or changed
    return changed


def _validate_fragment(
    fragment: KnowledgeFragment,
    config_getter: Callable[[], Any] = get_config,
) -> List[str]:
    """单片段硬校验，返回该片段未通过的校验项列表。"""
    failures = []
    title = (fragment.title or "").strip()

    # 1. 标题质量
    if not title or title.startswith(("无标题", "未知")):
        failures.append(f"标题缺失或无效: '{title}'")
    elif len(title) < 10:
        failures.append(f"标题过短 ({len(title)} 字符): '{title}'")

    # 2. 核心内容质量：prompt/schema/runtime 统一执行同一硬标准。
    cfg = config_getter()
    fragment_boundary_chars = int(
        cfg.get("distill.fragment_boundary_chars", FRAGMENT_BOUNDARY_CHARS)
        or FRAGMENT_BOUNDARY_CHARS
    )
    content = (fragment.core_content or "").strip()
    if len(content) < MIN_CORE_CONTENT_CHARS:
        failures.append(
            f"核心内容过短 ({len(content)} 字符)，必须至少 {MIN_CORE_CONTENT_CHARS} 字符"
        )
    if len(content) > fragment_boundary_chars and not fragment.boundaries:
        failures.append(f"内容超长 ({len(content)} 字符) 且缺少边界定义")

    # 3. frontmatter 关键字段
    fm = fragment.frontmatter or {}
    summary = fm.get("摘要", "")
    if not summary or (isinstance(summary, str) and len(summary.strip()) < 5):
        failures.append("frontmatter 摘要缺失或无效")
    domain = fm.get("领域", "")
    if not domain or (isinstance(domain, str) and len(domain.strip()) < 2):
        failures.append("frontmatter 领域缺失或无效")

    # 4. 结构化证明
    has_structure = bool(re.search(r"^#{1,3}\s", content, re.MULTILINE) or "```" in content)
    if not has_structure:
        failures.append("缺少结构化标记（无标题层级或代码块）")

    return failures


def _strict_validate_fragments(
    fragments: List[KnowledgeFragment],
    config_getter: Callable[[], Any] = get_config,
) -> Tuple[bool, List[str]]:
    """硬校验：LLM 输出不符合 schema 就整个 session 标记为失败，不入库。"""
    failures = []
    for i, frag in enumerate(fragments):
        prefix = f"片段[{i}]"
        for err in _validate_fragment(frag, config_getter=config_getter):
            failures.append(f"{prefix} {err}")
    return len(failures) == 0, failures


def _fatal_self_check_errors(fragments: List[KnowledgeFragment]) -> List[str]:
    failures: List[str] = []
    for i, frag in enumerate(fragments):
        issues = list(getattr(frag, "self_check_issues", []) or [])
        severity = getattr(frag, "self_check_severity", "") or max_self_check_severity(issues)
        if severity == "ok" and issues:
            severity = max_self_check_severity(issues)
        if severity != "fatal":
            continue
        fatal_issues = [issue for issue in issues if classify_self_check_issue(issue) == "fatal"]
        if not fatal_issues:
            fatal_issues = ["自检 fatal 失败"]
        failures.extend(f"片段[{i}] 自检fatal: {issue}" for issue in fatal_issues)
    return failures


def _fragment_uncertainty(fragment: KnowledgeFragment) -> float:
    raw_confidence = fm_get(fragment.frontmatter, "confidence", "")
    try:
        confidence = float(raw_confidence)
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))
    return 1.0 - confidence


def _apply_expression_formatting(fragment: KnowledgeFragment, cfg) -> None:
    from core.hephaestus.content_expression import (
        ContentExpressionFormatter,
        maybe_format_expression,
    )

    original = fragment.core_content or ""
    suggestion = ContentExpressionFormatter().detect_form(original)
    formatted = maybe_format_expression(original, cfg)
    fragment.frontmatter["expression_format"] = suggestion.form.value
    if formatted != original:
        fragment.core_content = formatted


def _evaluate_quality_gate(fragment: KnowledgeFragment, cfg):
    from core.hephaestus.cognitive_value_gate import CognitiveValueGate
    from core.hephaestus.quality_gate import QualityGate
    from core.kia.policy import get_shadowed_value

    if not bool(cfg.get("quality_gate.enabled", True)):
        return None

    gate = QualityGate(
        base_threshold=float(
            get_shadowed_value(
                "quality_gate.base_threshold",
                cfg.get("quality_gate.base_threshold", 0.55) or 0.55,
            )
        ),
        review_margin=float(
            get_shadowed_value(
                "quality_gate.review_margin",
                cfg.get("quality_gate.review_margin", 0.15) or 0.15,
            )
        ),
    )
    content = "\n\n".join(
        part
        for part in [fragment.title, fragment.background, fragment.core_content]
        if part and str(part).strip()
    )
    decision = gate.evaluate(content, uncertainty=_fragment_uncertainty(fragment))
    fragment.frontmatter["quality_gate_disposition"] = decision.disposition
    fragment.frontmatter["quality_gate_score"] = round(decision.score, 3)
    fragment.frontmatter["quality_gate_threshold"] = round(decision.threshold, 3)
    fragment.frontmatter["quality_gate_reason"] = decision.reason

    final_decision: Any = decision
    if (
        bool(cfg.get("quality_gate.cognitive_value.enabled", True))
        and decision.disposition != "reject"
    ):
        cognitive_gate = CognitiveValueGate(
            base_threshold=float(
                cfg.get("quality_gate.cognitive_value.base_threshold", 0.55) or 0.55
            ),
            review_margin=float(
                cfg.get("quality_gate.cognitive_value.review_margin", 0.15) or 0.15
            ),
        )
        cognitive_decision = cognitive_gate.evaluate(
            content,
            frontmatter=fragment.frontmatter,
        )
        fragment.frontmatter["cognitive_value_disposition"] = cognitive_decision.disposition
        fragment.frontmatter["cognitive_value_score"] = round(cognitive_decision.score, 3)
        fragment.frontmatter["cognitive_value_threshold"] = round(cognitive_decision.threshold, 3)
        fragment.frontmatter["cognitive_value_reason"] = cognitive_decision.reason
        fragment.frontmatter["cognitive_contribution_types"] = list(
            cognitive_decision.contribution_types
        )
        fragment.frontmatter["cognitive_consumers"] = list(cognitive_decision.consumers)

        if cognitive_decision.disposition == "reject":
            final_decision = cognitive_decision
        elif decision.disposition == "accept" and cognitive_decision.disposition == "review":
            final_decision = cognitive_decision

    if final_decision.disposition == "review":
        issue = (
            f"质量门禁建议人工复核: score={final_decision.score:.3f}, "
            f"threshold={final_decision.threshold:.3f}, reason={final_decision.reason}"
        )
        if issue not in fragment.self_check_issues:
            fragment.self_check_issues.append(issue)
        fragment.self_check_passed = False
        if fragment.self_check_severity != "fatal":
            fragment.self_check_severity = "warning"
        fragment.frontmatter["verification"] = "pending-verification"
        fragment.frontmatter["verification_severity"] = fragment.self_check_severity
    return final_decision


def _record_quality_gate_action(
    fragment: KnowledgeFragment,
    cfg: Any,
    *,
    session_id: str | None,
    fragment_index: int,
    decision: Any,
) -> None:
    """Persist the final write-gate decision when the runtime has a ledger path."""
    database_dir = getattr(cfg, "database_dir", None)
    if not database_dir or decision is None:
        return

    try:
        from core.system_contracts import (
            ActionLedger,
            make_quality_gate_observation,
        )

        status_by_disposition = {
            "accept": "verified",
            "review": "needs_user",
            "reject": "failed_terminal",
        }
        status = status_by_disposition.get(decision.disposition, "degraded")
        evidence_refs = ["core/hephaestus/quality_gate.py"]
        if "cognitive_value_disposition" in fragment.frontmatter:
            evidence_refs.append("core/hephaestus/cognitive_value_gate.py")
        action_id = ActionLedger.from_config(
            cfg,
            initialize=True,
        ).record_observation(
            make_quality_gate_observation(
                actor="core.hephaestus.distillation_engine",
                target=f"distill:{session_id or 'unknown'}:fragment:{fragment_index}",
                evidence_refs=tuple(evidence_refs),
                result_status=status,
                decision_id=str(fragment.frontmatter.get("gate_decision_id", "")),
                details={
                    "session_id": session_id or "",
                    "fragment_index": fragment_index,
                    "title": fragment.title,
                    "final_disposition": decision.disposition,
                    "final_score": round(float(decision.score), 3),
                    "final_threshold": round(float(decision.threshold), 3),
                    "final_reason": decision.reason,
                    "quality_gate_disposition": fragment.frontmatter.get(
                        "quality_gate_disposition"
                    ),
                    "cognitive_value_disposition": fragment.frontmatter.get(
                        "cognitive_value_disposition"
                    ),
                    "cognitive_contribution_types": fragment.frontmatter.get(
                        "cognitive_contribution_types", []
                    ),
                    "cognitive_consumers": fragment.frontmatter.get("cognitive_consumers", []),
                },
            )
        )
        fragment.frontmatter["quality_gate_action_ledger_ref"] = action_id
    except (OSError, TypeError, ValueError, sqlite3.Error) as exc:  # pragma: no cover
        logger.warning("[Distillation] 写入质量门禁 ActionLedger 失败: %s", exc)


def _collect_fragment_errors(
    fragments: List[KnowledgeFragment],
    cfg: Any,
    session_id: str | None = None,
) -> Tuple[set, List[str]]:
    """返回片段硬校验失败索引集合 + 所有错误信息。"""
    frag_errors = [(i, _validate_fragment(frag)) for i, frag in enumerate(fragments)]
    all_errors = [f"片段[{i}] {err}" for i, errs in frag_errors for err in errs]
    failed_indices = {i for i, errs in frag_errors if errs}

    for i, frag in enumerate(fragments):
        decision = _evaluate_quality_gate(frag, cfg)
        _record_quality_gate_action(
            frag,
            cfg,
            session_id=session_id,
            fragment_index=i,
            decision=decision,
        )
        if decision is not None and decision.disposition == "reject":
            failed_indices.add(i)
            all_errors.append(
                f"片段[{i}] 质量门禁拒绝: score={decision.score:.3f}, "
                f"threshold={decision.threshold:.3f}, reason={decision.reason}"
            )
    return failed_indices, all_errors


def _apply_domain_scores(fragment: KnowledgeFragment, cfg) -> None:
    if not bool(cfg.get("scoring.domain_scorers_enabled", True)):
        return
    try:
        from core.scoring.scorers import dimension_catalog, score_domain

        content = "\n\n".join(
            part
            for part in [fragment.title, fragment.background, fragment.core_content]
            if part and str(part).strip()
        )
        metadata = {
            "frontmatter": fragment.frontmatter or {},
            "domain": (fragment.frontmatter or {}).get("领域", ""),
        }
        scored: Dict[str, Any] = {}
        for domain in sorted(dimension_catalog()):
            card = score_domain(domain, content, metadata)
            scored[domain] = {
                "scores": {k: round(v, 3) for k, v in card.scores.items()},
                "confidences": {k: round(v, 3) for k, v in card.confidences.items()},
                "model_version": card.model_version,
            }
        if scored:
            fragment.frontmatter["domain_scores"] = scored
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
        logger.debug("[Distillation] domain scorers failed", exc_info=True)
