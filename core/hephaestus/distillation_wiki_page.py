# -*- coding: utf-8 -*-
"""Wiki page rendering helpers for the distillation pipeline."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from core.cognition_episode_contract import COGNITION_EPISODE_FIELDS
from core.frontmatter import fm_get, to_chinese_frontmatter
from core.hephaestus.distillation_page_identity import distillation_fragment_hash
from core.hephaestus.distillation_models import KnowledgeFragment
from core.hephaestus.distillation_self_check import max_self_check_severity
from core.knowledge_form import (
    FORM_ALIASES,
    knowledge_form_entity_type,
    normalize_knowledge_form,
)


def _yaml_safe(value):
    """对 frontmatter 字符串值进行 YAML 安全转义。"""
    if not isinstance(value, str):
        return str(value)
    import yaml

    dumped = yaml.safe_dump(value, allow_unicode=True, width=float("inf")).strip()
    if dumped.endswith("..."):
        dumped = dumped[:-3].rstrip()
    return dumped


LEGACY_FORM_TO_ENTITY_TYPE = {
    "pattern": "concept",
    "snippet": "technology",
    "reference": "technology",
    "todo": "project",
    "data-insight": "dataset",
}

FORM_TO_ENTITY_TYPE = {
    **{alias: knowledge_form_entity_type(alias) for alias in FORM_ALIASES},
    **LEGACY_FORM_TO_ENTITY_TYPE,
}


def _map_form_to_type(form: str) -> str:
    """将知识形态映射为蓝图标准的实体类型"""
    canonical_type = knowledge_form_entity_type(form)
    if canonical_type:
        return canonical_type
    return LEGACY_FORM_TO_ENTITY_TYPE.get(str(form or "").strip().lower(), "concept")


def _usage_hints_for_fragment(fragment: KnowledgeFragment) -> List[str]:
    """生成 Obsidian 首屏可读的使用提示。"""
    form = normalize_knowledge_form(fragment.form)
    if form == "problem-solution":
        hints = [
            "遇到同类问题时，先对照适用场景确认是否命中，再按详细内容里的根因、验证步骤和修复动作执行。",
            "执行前先看“不适用于”和“反例 / 坑”，避免把一次性经验误用成通用规则。",
        ]
    elif form == "decision":
        hints = [
            "做同类取舍时，优先看结论、适用场景和不适用边界，再把当前约束与原始决策约束逐项对齐。",
            "如果约束已经变化，把它作为历史决策参考，而不是直接复用结论。",
        ]
    elif form == "heuristic":
        hints = [
            "把它当作经验规则使用：先检查适用边界，再用反例验证当前场景是否会失效。",
            "适合在方案评审、排查和复盘时作为快速检查项。",
        ]
    elif form == "anti-pattern":
        hints = [
            "看到类似信号时优先暂停当前做法，回到反例、边界和修复动作逐项检查。",
            "适合放进 guard、checklist 或复盘问题里，作为提前拦截项。",
        ]
    elif form == "methodology":
        hints = [
            "按步骤执行，并在每一步记录输入、判断依据和输出结果，方便之后复盘和改进。",
            "先看适用场景；如果场景不匹配，只复用方法中的局部检查点。",
        ]
    elif form == "insight":
        hints = [
            "把它作为解释模型或判断视角使用，不要直接当成操作步骤。",
            "用于生成方案前的假设检查、复盘时的原因分析，或关联其他知识时的背景线索。",
        ]
    else:
        hints = [
            "先读结论和适用场景，再决定它是可直接执行、可作为检查项，还是只作为背景参考。",
            "使用前确认来源覆盖和置信度；如果来源不完整，先回到原会话或补充验证。",
        ]

    if fragment.self_check_issues:
        hints.append("这条知识仍有待验证项，使用前需要先补证据或做小范围验证。")
    return hints


def _source_quality_notes(
    fragment: KnowledgeFragment,
    session_coverage: str,
    distill_input_mode: str,
    covered_turn_range: str,
    truncated: bool,
) -> List[str]:
    """生成面向用户的来源质量提示。"""
    notes: List[str] = []
    evidence_level = fm_get(fragment.frontmatter, "evidence_level", "")
    confidence = fm_get(fragment.frontmatter, "confidence", "")
    if evidence_level:
        notes.append(f"证据级别: {evidence_level}")
    if confidence != "":
        notes.append(f"置信度: {confidence}")

    coverage = session_coverage or (fragment.frontmatter or {}).get("session_coverage", "")
    if coverage:
        notes.append(f"会话覆盖: {coverage}")
    if covered_turn_range:
        notes.append(f"覆盖轮次: {covered_turn_range}")
    if distill_input_mode:
        notes.append(f"蒸馏输入: {distill_input_mode}")
    notes.append(f"是否截断: {'是' if truncated else '否'}")

    if truncated:
        notes.append("使用前建议回查来源追踪中的原始会话或 artifact，确认关键上下文没有丢失。")
    return notes


def _related_concepts_for_page(
    fragment: KnowledgeFragment,
    is_stop_phrase: Callable[[str], bool] | None,
    wiki_dir: Path | None,
    wiki_dir_getter: Callable[[], Path] | None,
) -> List[str]:
    all_related = []
    seen = set()
    for concept in list(fragment.related_concepts) + list(fragment.cross_agent_links):
        if not concept or concept in seen:
            continue
        seen.add(concept)
        if len(concept) < 2 or concept.startswith("待"):
            continue
        if is_stop_phrase and is_stop_phrase(concept):
            continue
        if "/" in concept or "\\" in concept:
            base_dir = wiki_dir
            if base_dir is None and wiki_dir_getter:
                base_dir = wiki_dir_getter()
            if base_dir is None:
                continue
            target_path = base_dir / f"{concept}.md"
            if not target_path.exists():
                continue
        all_related.append(concept)
    return all_related


def _resolve_coverage(distill_input_mode: str, truncated: bool) -> str:
    """根据蒸馏输入模式与截断标志确定覆盖度标记。"""
    if distill_input_mode == "chunked":
        return "full_chunked"
    if truncated:
        return "partial"
    return "full"


def _coverage_label(coverage: str) -> str:
    """将覆盖度标记转为可读中文。"""
    if coverage == "full":
        return "完整"
    if coverage == "partial":
        return "部分"
    if coverage == "none":
        return "无"
    return coverage or "未知"


def _as_string_list(value: Any) -> List[str]:
    """Normalize trace-id style values to a compact string list."""
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None and str(item)]


def _structured_trace_fields(structured_output: Dict[str, Any] | None) -> Dict[str, Any]:
    """Extract user-visible raw trace fields from distill_output_v4."""
    if not isinstance(structured_output, dict):
        return {}

    fields: Dict[str, Any] = {}
    source_event_ids = _as_string_list(structured_output.get("source_event_ids"))
    if source_event_ids:
        fields["source_event_ids"] = source_event_ids

    for key in ("raw_completeness", "gate_decision_id", "distill_intent"):
        value = structured_output.get(key)
        if value not in (None, ""):
            fields[key] = value

    behavior = structured_output.get("user_behavior_intent")
    if isinstance(behavior, dict):
        behavior_summary = behavior.get("behavior_summary")
        if behavior_summary not in (None, ""):
            fields["behavior_intent_summary"] = str(behavior_summary)
        for source_key, target_key in (
            ("content_source", "behavior_content_source"),
            ("user_intent_signal", "user_intent_signal"),
            ("intent_hypothesis", "intent_hypothesis"),
            ("intent_confidence", "intent_confidence"),
            ("intent_status", "intent_status"),
        ):
            value = behavior.get(source_key)
            if value not in (None, ""):
                fields[target_key] = value
        intent_evidence = behavior.get("intent_evidence")
        if isinstance(intent_evidence, list) and intent_evidence:
            fields["intent_evidence"] = intent_evidence
        verification_events = behavior.get("intent_verification_events")
        if isinstance(verification_events, list):
            fields["intent_verification_events"] = verification_events

    evidence_refs: List[Dict[str, Any]] = []
    cognitive_actions: List[str] = []
    cognitive_action_refs: List[Dict[str, Any]] = []
    source_authorities: List[str] = []
    unauthorized_claim_count = 0
    claims = structured_output.get("claims")
    if isinstance(claims, list):
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            claim_authorized = False
            claim_id = claim.get("claim_id")
            claim_actions = _as_string_list(claim.get("cognitive_actions"))
            if claim_actions:
                cognitive_actions.extend(claim_actions)
                action_ref: Dict[str, Any] = {"actions": claim_actions}
                if claim_id:
                    action_ref["claim_id"] = str(claim_id)
                claim_type = claim.get("claim_type")
                if claim_type:
                    action_ref["claim_type"] = str(claim_type)
                cognitive_action_refs.append(action_ref)
            for evidence in claim.get("evidence") or []:
                if not isinstance(evidence, dict):
                    continue
                source_event_id = evidence.get("source_event_id")
                if not source_event_id:
                    continue
                ref: Dict[str, Any] = {
                    "source_event_id": str(source_event_id),
                }
                if claim_id:
                    ref["claim_id"] = str(claim_id)
                quote = evidence.get("quote")
                if quote:
                    ref["quote"] = str(quote)
                for source_key, target_key in (
                    ("source_authority_id", "source_authority_id"),
                    ("source_authority", "source_authority"),
                    ("authority_purpose", "authority_purpose"),
                    ("artifact_uri", "artifact_uri"),
                    ("artifact_type", "artifact_type"),
                    ("artifact_summary", "artifact_summary"),
                    ("artifact_sha256", "artifact_sha256"),
                    ("artifact_mime_type", "artifact_mime_type"),
                ):
                    value = evidence.get(source_key)
                    if value:
                        ref[target_key] = str(value)
                authority = evidence.get("source_authority")
                if authority:
                    source_authorities.append(str(authority))
                if evidence.get("authority_allows_cognitive_update") is True:
                    claim_authorized = True
                evidence_refs.append(ref)
            if not claim_authorized:
                unauthorized_claim_count += 1
    if evidence_refs:
        fields["evidence_refs"] = evidence_refs
    if cognitive_actions:
        fields["cognitive_actions"] = _dedup_strings(cognitive_actions)
        fields["cognitive_action_refs"] = cognitive_action_refs
    elif isinstance(claims, list) and claims:
        fields["cognitive_action_status"] = "ordinary_knowledge"
    if source_authorities:
        fields["source_authorities"] = _dedup_strings(source_authorities)
        fields["cognitive_authority_status"] = (
            "authorized" if unauthorized_claim_count == 0 else "pending_hypothesis"
        )
        fields["unauthorized_claim_count"] = unauthorized_claim_count
    return fields


def _dedup_strings(values: List[str]) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _format_trace_ids(ids: List[str], limit: int = 8) -> str:
    visible = ids[:limit]
    suffix = f" 等共 {len(ids)} 个" if len(ids) > limit else ""
    return ", ".join(f"`{item}`" for item in visible) + suffix


def _prepare_frontmatter(
    fragment: KnowledgeFragment,
    session_id: str,
    source: str,
    session_coverage: str,
    distill_input_mode: str,
    distill_prompt_version: str,
    covered_turn_range: str,
    truncated: bool,
    structured_output: Dict[str, Any] | None,
    input_revision: str,
    fragment_hash: str,
) -> Dict[str, Any]:
    """构造 frontmatter 默认值与清洗后的用户字段。"""
    entity_type = fm_get(fragment.frontmatter, "type", "")
    if (
        not entity_type
        or normalize_knowledge_form(entity_type)
        or str(entity_type).strip().lower() in LEGACY_FORM_TO_ENTITY_TYPE
    ):
        entity_type = _map_form_to_type(fragment.form)

    summary = fm_get(fragment.frontmatter, "summary", "")
    if not summary:
        parts = [p for p in (fragment.title, fragment.background) if p]
        summary = " — ".join(parts)[:150] if parts else (fragment.title or "")[:150]

    cleaned_fm = dict(fragment.frontmatter or {})
    for _k in ("类型", "type"):
        val = cleaned_fm.get(_k)
        if normalize_knowledge_form(val) or str(val or "").strip().lower() in (
            LEGACY_FORM_TO_ENTITY_TYPE
        ):
            cleaned_fm.pop(_k, None)

    coverage = _resolve_coverage(distill_input_mode, truncated)
    defaults = {
        "type": entity_type,
        "form": fragment.form,
        "name": fragment.title,
        "domain": (fragment.frontmatter or {}).get("领域", "未分类"),
        "summary": summary,
        "status": "草稿",
        "knowledge_stage": "原始",
        "source_count": 1,
        "evidence_level": (fragment.frontmatter or {}).get("证据级别", "single"),
        "confidence": (fragment.frontmatter or {}).get("置信度", 0.5),
        "temporal_scope": (fragment.frontmatter or {}).get("时效性", "contextual"),
        "created_at": datetime.now().strftime("%Y-%m-%d"),
        "source": source or "unknown",
        "source_session": session_id,
        "input_revision": input_revision,
        "fragment_hash": fragment_hash or distillation_fragment_hash(fragment),
        "distilled_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "distill_input_mode": distill_input_mode or "unknown",
        "distill_prompt_version": distill_prompt_version or "",
        "source_coverage": session_coverage or "unknown",
        "covered_turn_range": covered_turn_range or "",
        "truncated": truncated,
        "coverage": coverage,
    }
    defaults.update(_structured_trace_fields(structured_output))
    return {"cleaned_fm": cleaned_fm, "defaults": defaults, "coverage": coverage}


def _render_frontmatter(
    fragment: KnowledgeFragment,
    frontmatter: Dict[str, Any],
) -> List[str]:
    """把 frontmatter 字典渲染为 YAML 行列表。"""
    fm = to_chinese_frontmatter(frontmatter["cleaned_fm"], frontmatter["defaults"])
    lines = ["---"]
    ordered_keys = [
        "类型",
        "知识形态",
        "名称",
        "领域",
        "摘要",
        "状态",
        "知识阶段",
        "来源数量",
        "证据级别",
        "置信度",
        "时效性",
        "创建日期",
        "来源",
        "来源会话",
        "访问范围",
        "来源Agent",
        "会话ID",
        "项目",
        "ACL版本",
        "ACL元数据完整",
        "ACL校准状态",
        "来源事件ID",
        "证据引用",
        "Raw完整度",
        "门禁决策ID",
        "蒸馏意图",
        "行为意图摘要",
        "行为内容来源",
        "用户意图信号",
        "意图假设",
        "意图证据",
        "意图验证事件",
        "意图置信度",
        "意图状态",
        "书籍元数据",
        "数据洞察",
        "策略要点",
        "报告要点",
        "表格证据",
        "蒸馏时间",
        "关键词",
        "触发器",
        "别名",
        "版本标记",
        "决策摘要",
        "合并来源",
        "跨Agent关联",
        "蒸馏输入模式",
        "蒸馏提示版本",
        "来源覆盖度",
        "覆盖轮次范围",
        "是否截断",
        "覆盖度",
        "提取方式",
        "验证状态",
        "验证等级",
        "质量门禁状态",
        "质量门禁分",
        "质量门禁阈值",
        "质量门禁原因",
        "质量门禁账本ID",
        "认知价值门禁状态",
        "认知价值分",
        "认知价值阈值",
        "认知价值原因",
        "认知贡献类型",
        "认知消费者",
        "认知动作",
        "认知动作状态",
        "来源权限",
        "认知权限状态",
        "未授权认知声明数",
        "认知动作引用",
        "认知事件修订ID",
        "Wiki路由状态",
        "Wiki路由原因",
        "Wiki路由目标",
        "表达格式",
        "领域评分",
    ]
    for key in ordered_keys:
        if key not in fm:
            continue
        value = fm[key]
        if isinstance(value, (list, dict)):
            lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
        else:
            lines.append(f"{key}: {_yaml_safe(value)}")

    extract_method = (fragment.frontmatter or {}).get("提取方式", "")
    if extract_method:
        lines.append(f"提取方式: {_yaml_safe(extract_method)}")

    if not fragment.self_check_passed:
        severity = getattr(fragment, "self_check_severity", "") or max_self_check_severity(
            getattr(fragment, "self_check_issues", []) or []
        )
        if severity == "ok" and fragment.self_check_issues:
            severity = max_self_check_severity(fragment.self_check_issues)
        if "验证状态" not in fm:
            lines.append("验证状态: pending-verification")
        if "验证等级" not in fm:
            lines.append(f"验证等级: {_yaml_safe(severity or 'warning')}")

    if fragment.relations:
        lines.append(f"关联: {json.dumps(fragment.relations, ensure_ascii=False)}")

    lines.append("---")
    return lines


def _extract_conclusion(fragment: KnowledgeFragment, has_deep_structure: bool) -> str:
    """从 background 或 core_content 提取结论摘要。"""
    if fragment.background and not fragment.background.strip().startswith("#"):
        return fragment.background.strip()
    if fragment.core_content and not has_deep_structure:
        first_para = fragment.core_content.strip().split("\n")[0].strip()
        if len(first_para) > 10:
            return first_para.split("。")[0].strip() + "。"
    return ""


_COMMAND_PREFIXES = (
    "curl ",
    "git ",
    "docker ",
    "pip ",
    "npm ",
    "cd ",
    "mkdir ",
    "rm ",
    "mv ",
    "cp ",
    "unset ",
    "export ",
    "source ",
    "conda ",
    "brew ",
)


def _extract_procedures(core_content: str | None) -> List[str]:
    """从 core_content 中提取可执行命令/代码块作为操作步骤。"""
    procedures: List[str] = []
    if not core_content:
        return procedures
    code_blocks = re.findall(
        r"```(?:\w+)?\n(.*?)```",
        core_content,
        re.DOTALL,
    )
    for block in code_blocks:
        stripped = block.strip()
        if any(cmd in stripped for cmd in _COMMAND_PREFIXES):
            procedures.append("```bash\n" + stripped + "\n```")
    for line in core_content.split("\n"):
        line = line.strip()
        if line.startswith(_COMMAND_PREFIXES):
            procedures.append(f"- `{line}`")
    return procedures


def _render_page_header(
    fragment: KnowledgeFragment,
    source: str,
    confidence: Any,
    coverage: str,
    truncated: bool,
) -> List[str]:
    """渲染标题与首屏元信息块。"""
    body = [f"# {fragment.title}", ""]
    coverage_label = _coverage_label(coverage)
    verification = "已验证" if fragment.self_check_passed else "待验证"
    meta_parts = [
        f"**来源**：{source or 'unknown'}",
        f"**置信度**：{confidence}",
        f"**覆盖度**：{coverage_label}",
        f"**状态**：{verification}",
    ]
    if truncated:
        meta_parts.append("⚠️ **截断**")
    body.append("> " + " | ".join(meta_parts))
    body.append("")

    if truncated:
        body.append(
            "> ⚠️ **此页面内容可能不完整**：原始会话超长，部分上下文未进入蒸馏。"
            "关键决策建议回查原始会话确认。"
        )
        body.append("")
    return body


def _render_conclusion_section(fragment: KnowledgeFragment, has_deep_structure: bool) -> List[str]:
    """渲染结论摘要。"""
    conclusion = _extract_conclusion(fragment, has_deep_structure)
    if conclusion and len(conclusion) > 10:
        return ["## 结论", "", conclusion, ""]
    return []


def _render_usage_section(fragment: KnowledgeFragment) -> List[str]:
    """渲染使用提示。"""
    usage_hints = _usage_hints_for_fragment(fragment)
    if not usage_hints:
        return []
    lines = ["## 怎么用", ""]
    for hint in usage_hints:
        lines.append(f"- {hint}")
    lines.append("")
    return lines


def _render_applies_section(fragment: KnowledgeFragment, has_deep_structure: bool) -> List[str]:
    """渲染适用场景。"""
    applies = (fragment.boundaries or {}).get("applies", "")
    if applies:
        return ["## 适用场景", "", applies, ""]
    if not has_deep_structure and fragment.form in (
        "decision-log",
        "decision",
        "问题-解决",
        "problem-solution",
        "heuristic",
        "经验法则",
        "methodology",
        "方法论",
    ):
        return ["## 适用场景", "", "*待补充：该知识在什么场景下适用*", ""]
    return []


def _render_procedures_section(core_content: str | None) -> List[str]:
    """渲染操作步骤 / 使用方法。"""
    procedures = _extract_procedures(core_content)
    if not procedures:
        return []
    lines = ["## 操作步骤 / 使用方法", ""]
    lines.extend(procedures)
    lines.append("")
    return lines


def _render_core_content_section(
    fragment: KnowledgeFragment, has_deep_structure: bool
) -> List[str]:
    """渲染核心内容。"""
    if not fragment.core_content:
        return []
    if has_deep_structure:
        return [fragment.core_content, ""]
    return ["## 详细内容", "", fragment.core_content, ""]


def _render_anti_patterns_section(fragment: KnowledgeFragment) -> List[str]:
    """渲染反例 / 坑。"""
    if not fragment.anti_patterns:
        return []
    lines = ["## 反例 / 坑", ""]
    for ap in fragment.anti_patterns:
        lines.append(f"- {ap}")
    lines.append("")
    return lines


def _render_not_applies_section(fragment: KnowledgeFragment) -> List[str]:
    """渲染不适用于边界。"""
    not_applies = (fragment.boundaries or {}).get("not_applies", "")
    if not not_applies:
        return []
    return ["### 不适用于", "", f"- {not_applies}", ""]


def _render_self_check_issues_section(fragment: KnowledgeFragment) -> List[str]:
    """渲染待验证项。"""
    if not fragment.self_check_issues:
        return []
    lines = ["### 待验证项", ""]
    for issue in fragment.self_check_issues:
        lines.append(f"- ⚠️ {issue}")
    lines.append("")
    return lines


def _render_evolution_history_section() -> List[str]:
    """渲染演化历史。"""
    return [
        "## 演化历史",
        "",
        f"- v1: 初始记录（{datetime.now().strftime('%Y-%m-%d')}）",
        "",
    ]


def _render_related_section(
    fragment: KnowledgeFragment,
    is_stop_phrase: Callable[[str], bool] | None,
    wiki_dir: Path | None,
    wiki_dir_getter: Callable[[], Path] | None,
) -> List[str]:
    """渲染关联知识与结构化关系说明。"""
    lines: List[str] = []
    all_related = _related_concepts_for_page(fragment, is_stop_phrase, wiki_dir, wiki_dir_getter)
    if all_related:
        lines.extend(["## 关联知识", ""])
        for concept in all_related:
            lines.append(f"- [[{concept}]]")
        lines.append("")

    if fragment.relations:
        lines.extend(["### 关联说明", ""])
        for rel in fragment.relations:
            target = rel.get("target", "")
            rel_type = rel.get("type", "related_to")
            context = rel.get("context", "")
            lines.append(f"- **{target}**（`{rel_type}`）: {context}")
        lines.append("")
    return lines


def _render_ai_expansion_section(fragment: KnowledgeFragment) -> List[str]:
    """渲染 AI 关联扩充。"""
    if not fragment.ai_expansion:
        return []
    return [
        "## AI 关联扩充",
        "",
        "> ⚠️ **此区域内容由 AI 根据原始文档生成，属于关联性补充和建议，"
        "可能与作者原意存在偏差。请结合原始内容独立判断。**",
        "",
        fragment.ai_expansion,
        "",
    ]


def _render_quality_notes_section(
    fragment: KnowledgeFragment,
    session_coverage: str,
    distill_input_mode: str,
    covered_turn_range: str,
    truncated: bool,
) -> List[str]:
    """渲染可信度提示。"""
    quality_notes = _source_quality_notes(
        fragment,
        session_coverage,
        distill_input_mode,
        covered_turn_range,
        truncated,
    )
    if not quality_notes:
        return []
    lines = ["## 可信度提示", ""]
    for note in quality_notes:
        lines.append(f"- {note}")
    lines.append("")
    return lines


def _inline_cognition_text(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        rendered = str(value or "")
    return rendered.replace("\r\n", "<br>").replace("\n", "<br>")


def _render_source_span(evidence: Any) -> str:
    if not isinstance(evidence, dict):
        return "`unknown` / `unknown`"
    source_revision = str(evidence.get("source_event_id") or "unknown")
    span_start = evidence.get("authority_span_start")
    span_end = evidence.get("authority_span_end")
    span = (
        f"{span_start}:{span_end}" if span_start is not None and span_end is not None else "unknown"
    )
    revision_hash = str(evidence.get("authority_source_revision_sha256") or "")
    rendered = f"`{source_revision}` / `{span}`"
    if revision_hash:
        rendered += f" / `{revision_hash}`"
    return rendered


def _render_cognition_episode_section(
    structured_output: Dict[str, Any] | None,
) -> List[str]:
    """Render the complete admitted claim catalog and 19-field episode projection."""

    if not isinstance(structured_output, dict):
        return []
    claims = structured_output.get("claims")
    episode = structured_output.get("cognition_episode")
    has_claims = isinstance(claims, list) and bool(claims)
    has_episode = isinstance(episode, dict)
    if not has_claims and not has_episode:
        return []

    lines = [
        "## 认知事件投影",
        "",
        "> 本节是可读投影；canonical cognition revision 仍由 CognitiveStateStore 持有。",
        "",
    ]
    if isinstance(claims, list) and claims:
        lines.extend(["### 声明目录", ""])
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            claim_id = str(claim.get("claim_id") or "unknown-claim")
            lines.extend(
                [
                    f"#### `{claim_id}`",
                    "",
                    f"- 声明: {_inline_cognition_text(claim.get('claim_text'))}",
                    f"- 类型: `{_inline_cognition_text(claim.get('claim_type'))}`",
                ]
            )
            scope = claim.get("scope")
            if isinstance(scope, dict):
                lines.append(f"- Scope domain: `{_inline_cognition_text(scope.get('domain'))}`")
                for field_name, label in (
                    ("applies_to", "适用于"),
                    ("not_applies_to", "不适用于"),
                ):
                    values = scope.get(field_name)
                    if isinstance(values, list):
                        rendered = "、".join(_inline_cognition_text(value) for value in values)
                        lines.append(f"  - {label}: {rendered or '无'}")
            relation = claim.get("relation_to_existing")
            if isinstance(relation, dict):
                lines.append(
                    "- 与既有知识关系: " f"`{_inline_cognition_text(relation.get('type'))}`"
                )
                targets = relation.get("target_pages")
                if isinstance(targets, list):
                    rendered_targets = "、".join(_inline_cognition_text(value) for value in targets)
                    lines.append(f"  - 目标页面: {rendered_targets or '无'}")
                for field_name, label in (
                    ("delta_text", "差异"),
                    ("reason", "理由"),
                ):
                    value = relation.get(field_name)
                    if value not in (None, ""):
                        lines.append(f"  - {label}: {_inline_cognition_text(value)}")
            lines.append(f"- 建议动作: `{_inline_cognition_text(claim.get('recommended_action'))}`")
            actions = claim.get("cognitive_actions")
            if isinstance(actions, list):
                lines.append(
                    "- 认知动作: "
                    + ("、".join(f"`{_inline_cognition_text(value)}`" for value in actions) or "无")
                )
            lines.append(f"- 置信度: `{_inline_cognition_text(claim.get('confidence'))}`")
            evidence_refs = claim.get("evidence")
            if isinstance(evidence_refs, list):
                for evidence in evidence_refs:
                    lines.append(f"- 来源修订/Span: {_render_source_span(evidence)}")
            lines.append("")

    if isinstance(episode, dict):
        lines.extend(["### 19 字段认知链路", ""])
        for field_name in COGNITION_EPISODE_FIELDS:
            lines.extend([f"#### `{field_name}`", ""])
            entries = episode.get(field_name)
            if not isinstance(entries, list) or not entries:
                lines.extend(["- `missing`: 未提供 typed entry。", ""])
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    lines.append("- `invalid`: entry 不是对象。")
                    continue
                status = str(entry.get("status") or "unknown")
                content = entry.get("value") if status == "known" else entry.get("reason")
                lines.append(f"- `{status}`: {_inline_cognition_text(content)}")
                entry_claim_ids = entry.get("claim_ids")
                if isinstance(entry_claim_ids, list) and entry_claim_ids:
                    lines.append(
                        "  - 声明: "
                        + "、".join(
                            f"`{_inline_cognition_text(value)}`" for value in entry_claim_ids
                        )
                    )
                evidence_refs = entry.get("evidence_refs")
                if isinstance(evidence_refs, list):
                    for evidence in evidence_refs:
                        lines.append(f"  - 来源修订/Span: {_render_source_span(evidence)}")
            lines.append("")
    return lines


def _render_source_tracking_section(
    fragment: KnowledgeFragment,
    session_id: str,
    source: str,
    session_coverage: str,
    structured_output: Dict[str, Any] | None,
) -> List[str]:
    """渲染来源追踪。"""
    body = ["## 来源追踪", ""]
    body.append(f"- 来源会话: `{session_id}`")
    if source:
        body.append(f"- 来源 Agent: {source}")
    coverage = session_coverage or (fragment.frontmatter or {}).get("session_coverage", "")
    if coverage:
        body.append(f"- 会话覆盖: {coverage}")
    trace = _structured_trace_fields(structured_output)
    source_event_ids = trace.get("source_event_ids")
    if isinstance(source_event_ids, list) and source_event_ids:
        body.append(f"- Raw 事件: {_format_trace_ids(source_event_ids)}")
    raw_completeness = trace.get("raw_completeness")
    if raw_completeness:
        body.append(f"- Raw 完整度: {raw_completeness}")
    gate_decision_id = trace.get("gate_decision_id")
    if gate_decision_id:
        body.append(f"- 门禁决策: `{gate_decision_id}`")
    behavior_summary = trace.get("behavior_intent_summary")
    if behavior_summary:
        body.append(f"- 用户引入原因: {behavior_summary}")
    intent_hypothesis = trace.get("intent_hypothesis")
    intent_status = trace.get("intent_status")
    intent_confidence = trace.get("intent_confidence")
    if intent_hypothesis:
        status_part = f" / {intent_status}" if intent_status else ""
        confidence_part = (
            f" / 置信度 {intent_confidence}" if intent_confidence not in (None, "") else ""
        )
        body.append(f"- 用户意图: {intent_hypothesis}{status_part}{confidence_part}")
    intent_evidence = trace.get("intent_evidence")
    if isinstance(intent_evidence, list) and intent_evidence:
        body.append("- 意图证据:")
        for ref in intent_evidence[:3]:
            if not isinstance(ref, dict):
                continue
            source_event_id = ref.get("source_event_id")
            quote = ref.get("quote")
            reason = ref.get("reason")
            line = f"  - `{source_event_id}`"
            if quote:
                line += f": {quote}"
            if reason:
                line += f" ({reason})"
            body.append(line)
    verification_events = trace.get("intent_verification_events")
    if isinstance(verification_events, list) and verification_events:
        body.append("- 意图验证/修正:")
        for event in verification_events[:3]:
            if not isinstance(event, dict):
                continue
            source_event_id = event.get("source_event_id")
            status = event.get("status")
            quote = event.get("quote")
            line = f"  - `{source_event_id}` / {status or 'unknown'}"
            if quote:
                line += f": {quote}"
            body.append(line)
    evidence_refs = trace.get("evidence_refs")
    if isinstance(evidence_refs, list) and evidence_refs:
        body.append("- 证据引用:")
        for ref in evidence_refs[:5]:
            if not isinstance(ref, dict):
                continue
            source_event_id = ref.get("source_event_id")
            claim_id = ref.get("claim_id")
            quote = ref.get("quote")
            artifact_uri = ref.get("artifact_uri")
            artifact_summary = ref.get("artifact_summary") or ref.get("artifact_type")
            line = f"  - `{source_event_id}`"
            if claim_id:
                line += f" / {claim_id}"
            if quote:
                line += f": {quote}"
            if artifact_uri:
                label = str(artifact_summary or "artifact").replace("[", "(").replace("]", ")")
                line += f" | artifact: [{label}]({artifact_uri})"
            body.append(line)
        if len(evidence_refs) > 5:
            body.append(f"  - 另有 {len(evidence_refs) - 5} 条证据引用")
    original_source = (fragment.frontmatter or {}).get(
        "来源", (fragment.frontmatter or {}).get("source", "")
    )
    if original_source and original_source != source:
        body.append(f"- 原始来源: {original_source}")
    body.append(f"- 蒸馏时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return body


def _render_body(
    fragment: KnowledgeFragment,
    session_id: str,
    source: str,
    session_coverage: str,
    distill_input_mode: str,
    covered_turn_range: str,
    truncated: bool,
    coverage: str,
    confidence: Any,
    structured_output: Dict[str, Any] | None,
    *,
    is_stop_phrase: Callable[[str], bool] | None,
    wiki_dir: Path | None,
    wiki_dir_getter: Callable[[], Path] | None,
) -> List[str]:
    """渲染 wiki 页面正文。"""
    body: List[str] = []
    body.extend(_render_page_header(fragment, source, confidence, coverage, truncated))

    has_deep_structure = bool(
        fragment.core_content and fragment.core_content.strip().startswith("#")
    )

    body.extend(_render_conclusion_section(fragment, has_deep_structure))
    body.extend(_render_cognition_episode_section(structured_output))
    body.extend(_render_usage_section(fragment))
    body.extend(_render_applies_section(fragment, has_deep_structure))
    body.extend(_render_procedures_section(fragment.core_content))
    body.extend(_render_core_content_section(fragment, has_deep_structure))
    body.extend(_render_anti_patterns_section(fragment))
    body.extend(_render_not_applies_section(fragment))
    body.extend(_render_self_check_issues_section(fragment))
    body.extend(_render_evolution_history_section())
    body.extend(_render_related_section(fragment, is_stop_phrase, wiki_dir, wiki_dir_getter))
    body.extend(_render_ai_expansion_section(fragment))
    body.extend(
        _render_quality_notes_section(
            fragment,
            session_coverage,
            distill_input_mode,
            covered_turn_range,
            truncated,
        )
    )
    body.extend(
        _render_source_tracking_section(
            fragment,
            session_id,
            source,
            session_coverage,
            structured_output,
        )
    )
    return body


def generate_wiki_page(
    fragment: KnowledgeFragment,
    session_id: str,
    source: str = "",
    session_coverage: str = "",
    distill_input_mode: str = "",
    distill_prompt_version: str = "",
    covered_turn_range: str = "",
    truncated: bool = False,
    structured_output: Dict[str, Any] | None = None,
    input_revision: str = "",
    fragment_hash: str = "",
    *,
    is_stop_phrase: Callable[[str], bool] | None = None,
    wiki_dir: Path | None = None,
    wiki_dir_getter: Callable[[], Path] | None = None,
) -> str:
    """生成 wiki 页面 Markdown — 对齐蓝图 32 字段规范"""
    frontmatter = _prepare_frontmatter(
        fragment,
        session_id,
        source,
        session_coverage,
        distill_input_mode,
        distill_prompt_version,
        covered_turn_range,
        truncated,
        structured_output,
        input_revision,
        fragment_hash,
    )
    fm = to_chinese_frontmatter(frontmatter["cleaned_fm"], frontmatter["defaults"])
    lines = _render_frontmatter(fragment, frontmatter)
    body = _render_body(
        fragment,
        session_id,
        source,
        session_coverage,
        distill_input_mode,
        covered_turn_range,
        truncated,
        frontmatter["coverage"],
        fm.get("置信度", 0.5),
        structured_output,
        is_stop_phrase=is_stop_phrase,
        wiki_dir=wiki_dir,
        wiki_dir_getter=wiki_dir_getter,
    )
    return "\n".join(lines + [""] + body)
