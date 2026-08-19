"""
Frontmatter field contract helpers.

Obsidian-facing Markdown uses Chinese field names for readability, while
Python/SQLite/event payloads keep English canonical keys internally.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

import yaml

CANONICAL_TO_DISPLAY: Dict[str, str] = {
    "type": "类型",
    "form": "知识形态",
    "name": "名称",
    "domain": "领域",
    "summary": "摘要",
    "keywords": "关键词",
    "triggers": "触发器",
    "aliases": "别名",
    "status": "状态",
    "knowledge_stage": "知识阶段",
    "heat_score": "热度分",
    "heat_level": "热度等级",
    "session_count": "会话数量",
    "search_hits": "搜索命中",
    "ref_count": "引用数量",
    "source_count": "来源数量",
    "quality_score": "质量分",
    "freshness_days": "新鲜度天数",
    "last_accessed": "最后访问",
    "completeness": "完整度",
    "stats_updated": "统计更新时间",
    "temporal_scope": "时效性",
    "created_at": "创建日期",
    "version_tag": "版本标记",
    "confidence": "置信度",
    "evidence_level": "证据级别",
    "decision": "决策摘要",
    "merged_from": "合并来源",
    "cross_agent_refs": "跨Agent关联",
    "source": "来源",
    "source_session": "来源会话",
    "scope": "访问范围",
    "source_agent": "来源Agent",
    "session_id": "会话ID",
    "project": "项目",
    "acl_schema_version": "ACL版本",
    "acl_metadata_complete": "ACL元数据完整",
    "acl_reconciliation_status": "ACL校准状态",
    "source_event_ids": "来源事件ID",
    "evidence_refs": "证据引用",
    "raw_completeness": "Raw完整度",
    "gate_decision_id": "门禁决策ID",
    "distill_intent": "蒸馏意图",
    "behavior_intent_summary": "行为意图摘要",
    "behavior_content_source": "行为内容来源",
    "user_intent_signal": "用户意图信号",
    "intent_hypothesis": "意图假设",
    "intent_evidence": "意图证据",
    "intent_verification_events": "意图验证事件",
    "intent_confidence": "意图置信度",
    "intent_status": "意图状态",
    "book_meta": "书籍元数据",
    "data_insights": "数据洞察",
    "strategy_items": "策略要点",
    "report_items": "报告要点",
    "table_artifacts": "表格证据",
    "distilled_at": "蒸馏时间",
    "broken_links": "失效链接",
    "distill_input_mode": "蒸馏输入模式",
    "distill_prompt_version": "蒸馏提示版本",
    "source_coverage": "来源覆盖度",
    "covered_turn_range": "覆盖轮次范围",
    "truncated": "是否截断",
    "coverage": "覆盖度",
    "extract_method": "提取方式",
    "verification": "验证状态",
    "verification_severity": "验证等级",
    "quality_gate_disposition": "质量门禁状态",
    "quality_gate_score": "质量门禁分",
    "quality_gate_threshold": "质量门禁阈值",
    "quality_gate_reason": "质量门禁原因",
    "quality_gate_action_ledger_ref": "质量门禁账本ID",
    "cognitive_value_disposition": "认知价值门禁状态",
    "cognitive_value_score": "认知价值分",
    "cognitive_value_threshold": "认知价值阈值",
    "cognitive_value_reason": "认知价值原因",
    "cognitive_contribution_types": "认知贡献类型",
    "cognitive_consumers": "认知消费者",
    "cognitive_actions": "认知动作",
    "cognitive_action_status": "认知动作状态",
    "source_authorities": "来源权限",
    "cognitive_authority_status": "认知权限状态",
    "unauthorized_claim_count": "未授权认知声明数",
    "cognitive_action_refs": "认知动作引用",
    "cognition_episode_revision_id": "认知事件修订ID",
    "wiki_route_status": "Wiki路由状态",
    "wiki_route_reason": "Wiki路由原因",
    "wiki_route_target": "Wiki路由目标",
    "expression_format": "表达格式",
    "domain_scores": "领域评分",
    "reinforcement_count": "强化次数",
    "reinforced_at": "强化时间",
    "reinforcement_source_event_ids": "强化来源事件ID",
    "mnemos_quality_score": "质量评分",
    "mnemos_heat_score": "热度评分",
    "mnemos_usage_count": "使用次数",
    "mnemos_last_scored": "最后评分日期",
    "knowledge_stage_metric": "知识阶段指标",
    "status_metric": "状态指标",
}

DISPLAY_ALIASES: Dict[str, Iterable[str]] = {
    "type": ("类型", "类别"),
    "form": ("知识形态", "形态", "form"),
    "name": ("名称", "标题", "实体名"),
    "domain": ("领域",),
    "summary": ("摘要",),
    "keywords": ("关键词",),
    "triggers": ("触发器", "触发场景"),
    "aliases": ("别名",),
    "status": ("状态",),
    "knowledge_stage": ("知识阶段", "成熟度"),
    "heat_score": ("热度分",),
    "heat_level": ("热度等级",),
    "session_count": ("会话数量", "session_count"),
    "search_hits": ("搜索命中",),
    "ref_count": ("引用数量",),
    "source_count": ("来源数量", "source_count"),
    "quality_score": ("质量分",),
    "freshness_days": ("新鲜度天数",),
    "last_accessed": ("最后访问",),
    "completeness": ("完整度",),
    "stats_updated": ("统计更新时间", "stats_updated"),
    "temporal_scope": ("时效性",),
    "created_at": ("创建日期",),
    "version_tag": ("版本标记",),
    "confidence": ("置信度",),
    "evidence_level": ("证据级别",),
    "decision": ("决策摘要",),
    "merged_from": ("合并来源",),
    "cross_agent_refs": ("跨Agent关联", "cross_agent_refs"),
    "source": ("来源", "source"),
    "scope": ("访问范围", "scope"),
    "source_agent": ("来源Agent", "source_agent"),
    "session_id": ("会话ID", "session_id"),
    "project": ("项目", "project"),
    "acl_schema_version": ("ACL版本", "acl_schema_version"),
    "acl_metadata_complete": ("ACL元数据完整", "acl_metadata_complete"),
    "acl_reconciliation_status": ("ACL校准状态", "acl_reconciliation_status"),
    "source_event_ids": ("来源事件ID", "source_event_ids"),
    "evidence_refs": ("证据引用", "evidence_refs"),
    "raw_completeness": ("Raw完整度", "raw_completeness"),
    "gate_decision_id": ("门禁决策ID", "gate_decision_id"),
    "distill_intent": ("蒸馏意图", "distill_intent"),
    "behavior_intent_summary": ("行为意图摘要", "behavior_intent_summary"),
    "behavior_content_source": ("行为内容来源", "behavior_content_source"),
    "user_intent_signal": ("用户意图信号", "user_intent_signal"),
    "intent_hypothesis": ("意图假设", "intent_hypothesis"),
    "intent_evidence": ("意图证据", "intent_evidence"),
    "intent_verification_events": ("意图验证事件", "intent_verification_events"),
    "intent_confidence": ("意图置信度", "intent_confidence"),
    "intent_status": ("意图状态", "intent_status"),
    "book_meta": ("书籍元数据", "book_meta"),
    "data_insights": ("数据洞察", "data_insights"),
    "strategy_items": ("策略要点", "strategy_items"),
    "report_items": ("报告要点", "report_items"),
    "table_artifacts": ("表格证据", "table_artifacts"),
    "broken_links": ("失效链接", "broken_links"),
    "distill_input_mode": ("蒸馏输入模式",),
    "distill_prompt_version": ("蒸馏提示版本",),
    "source_coverage": ("来源覆盖度",),
    "covered_turn_range": ("覆盖轮次范围",),
    "truncated": ("是否截断",),
    "extract_method": ("提取方式",),
    "verification": ("验证状态", "verification"),
    "verification_severity": ("验证等级", "verification_severity"),
    "quality_gate_disposition": ("质量门禁状态", "quality_gate_disposition"),
    "quality_gate_score": ("质量门禁分", "quality_gate_score"),
    "quality_gate_threshold": ("质量门禁阈值", "quality_gate_threshold"),
    "quality_gate_reason": ("质量门禁原因", "quality_gate_reason"),
    "quality_gate_action_ledger_ref": (
        "质量门禁账本ID",
        "quality_gate_action_ledger_ref",
    ),
    "cognitive_value_disposition": ("认知价值门禁状态", "cognitive_value_disposition"),
    "cognitive_value_score": ("认知价值分", "cognitive_value_score"),
    "cognitive_value_threshold": ("认知价值阈值", "cognitive_value_threshold"),
    "cognitive_value_reason": ("认知价值原因", "cognitive_value_reason"),
    "cognitive_contribution_types": ("认知贡献类型", "cognitive_contribution_types"),
    "cognitive_consumers": ("认知消费者", "cognitive_consumers"),
    "cognitive_actions": ("认知动作", "cognitive_actions"),
    "cognitive_action_status": ("认知动作状态", "cognitive_action_status"),
    "source_authorities": ("来源权限", "source_authorities"),
    "cognitive_authority_status": ("认知权限状态", "cognitive_authority_status"),
    "unauthorized_claim_count": ("未授权认知声明数", "unauthorized_claim_count"),
    "cognitive_action_refs": ("认知动作引用", "cognitive_action_refs"),
    "cognition_episode_revision_id": (
        "认知事件修订ID",
        "cognition_episode_revision_id",
    ),
    "wiki_route_status": ("Wiki路由状态", "wiki_route_status"),
    "wiki_route_reason": ("Wiki路由原因", "wiki_route_reason"),
    "wiki_route_target": ("Wiki路由目标", "wiki_route_target"),
    "expression_format": ("表达格式", "expression_format"),
    "domain_scores": ("领域评分", "domain_scores"),
    "reinforcement_count": ("强化次数", "reinforcement_count"),
    "reinforced_at": ("强化时间", "reinforced_at"),
    "reinforcement_source_event_ids": ("强化来源事件ID", "reinforcement_source_event_ids"),
    "mnemos_quality_score": ("质量评分",),
    "mnemos_heat_score": ("热度评分",),
    "mnemos_usage_count": ("使用次数",),
    "mnemos_last_scored": ("最后评分日期",),
}

DISPLAY_TO_CANONICAL: Dict[str, str] = {}
for canonical, display in CANONICAL_TO_DISPLAY.items():
    DISPLAY_TO_CANONICAL[canonical] = canonical
    DISPLAY_TO_CANONICAL[display] = canonical
for canonical, aliases in DISPLAY_ALIASES.items():
    for alias in aliases:
        DISPLAY_TO_CANONICAL[alias] = canonical


def canonical_key(key: str) -> str:
    """Return the English canonical key for a display or canonical key."""
    return DISPLAY_TO_CANONICAL.get(key, key)


_KNOWN_CANONICAL_KEYS = frozenset(CANONICAL_TO_DISPLAY.keys())


def normalize_frontmatter(
    frontmatter: Dict[str, Any] | None, strict: bool = False
) -> Dict[str, Any]:
    """Normalize mixed Chinese/English frontmatter to English canonical keys.

    Args:
        frontmatter: Raw frontmatter dict.
        strict: If True, drop keys not in the known canonical whitelist.
    """
    normalized: Dict[str, Any] = {}
    if not isinstance(frontmatter, dict):
        return normalized
    for key, value in frontmatter.items():
        ckey = canonical_key(str(key))
        if strict and ckey not in _KNOWN_CANONICAL_KEYS:
            continue
        normalized[ckey] = value
    return normalized


def fm_get(frontmatter: Dict[str, Any] | None, key: str, default: Any | None = None) -> Any:
    """Read a canonical field from mixed Chinese/English frontmatter."""
    if not isinstance(frontmatter, dict):
        return default
    if key in frontmatter:
        return frontmatter[key]
    display_key = CANONICAL_TO_DISPLAY.get(key)
    if display_key in frontmatter:
        return frontmatter[display_key]
    for alias in DISPLAY_ALIASES.get(key, ()):
        if alias in frontmatter:
            return frontmatter[alias]
    return default


def to_chinese_frontmatter(
    frontmatter: Dict[str, Any] | None,
    defaults: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Convert canonical/mixed frontmatter to Chinese display keys."""
    canonical = normalize_frontmatter(frontmatter or {}, strict=True)
    if defaults:
        merged = dict(defaults)
        merged.update(canonical)
        canonical = merged

    result: Dict[str, Any] = {}
    for key, value in canonical.items():
        if value is None or value == "":
            continue
        display_key = CANONICAL_TO_DISPLAY.get(key)
        if display_key is None:
            continue
        result[display_key] = value
    return result


def to_chinese_frontmatter_preserving_unknown(
    frontmatter: Dict[str, Any] | None,
    defaults: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Convert known fields while retaining producer-specific metadata.

    Use this for read-modify-write operations on an existing page. The strict
    ``to_chinese_frontmatter`` function remains appropriate when constructing a
    new page from a validated schema, but it must not erase provenance fields
    owned by another producer.
    """

    raw = frontmatter if isinstance(frontmatter, dict) else {}
    result = {
        key: value
        for key, value in raw.items()
        if canonical_key(str(key)) not in _KNOWN_CANONICAL_KEYS
        and value is not None
        and value != ""
    }
    result.update(to_chinese_frontmatter(raw, defaults))
    return result


# ========== YAML 解析 / 写入 ==========


def read_markdown(path: Path, *, errors: str = "strict") -> str:
    """Read a Markdown document through the canonical frontmatter IO seam."""
    return Path(path).read_text(encoding="utf-8", errors=errors)


def read_frontmatter_document(
    path: Path,
    *,
    errors: str = "strict",
) -> Tuple[Optional[Dict[str, Any]], str, str]:
    """Read and parse one Markdown document, returning metadata, body, and text."""
    content = read_markdown(path, errors=errors)
    frontmatter, body = parse_frontmatter(content)
    return frontmatter, body, content


def read_strict_frontmatter_document(
    path: Path,
    *,
    errors: str = "strict",
) -> Tuple[Dict[str, Any], str, str]:
    """Read one Markdown file and reject missing, malformed, or non-map YAML."""
    content = read_markdown(path, errors=errors)
    if not content.startswith("---"):
        raise ValueError("frontmatter opening delimiter missing")
    end = content.find("---", 3)
    if end == -1:
        raise ValueError("frontmatter closing delimiter missing")
    try:
        loaded = yaml.safe_load(content[3:end].strip())
    except yaml.YAMLError as exc:
        raise ValueError("frontmatter YAML is invalid") from exc
    if not isinstance(loaded, dict):
        raise ValueError("frontmatter must be a mapping")
    return loaded, content[end + 3 :].lstrip("\n"), content


def read_frontmatter_only(
    path: Path,
    *,
    errors: str = "strict",
    max_bytes: int = 65536,
) -> Dict[str, Any]:
    """Read only the leading YAML metadata block, never the Markdown body."""
    lines: list[str] = []
    total = 0
    with Path(path).open("r", encoding="utf-8", errors=errors) as handle:
        if handle.readline().strip() != "---":
            raise ValueError("frontmatter opening delimiter missing")
        for line in handle:
            if line.strip() == "---":
                try:
                    loaded = yaml.safe_load("".join(lines))
                except yaml.YAMLError as exc:
                    raise ValueError("frontmatter YAML is invalid") from exc
                if not isinstance(loaded, dict):
                    raise ValueError("frontmatter must be a mapping")
                return loaded
            total += len(line.encode("utf-8"))
            if total > max_bytes:
                raise ValueError("frontmatter exceeds size limit")
            lines.append(line)
    raise ValueError("frontmatter closing delimiter missing")


def parse_frontmatter(content: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """Parse YAML frontmatter from markdown content.

    Returns:
        (frontmatter_dict, body) — frontmatter_dict is None if no frontmatter found.
    """
    if not content.startswith("---"):
        return None, content

    # Find the closing --- (skip the opening one)
    end = content.find("---", 3)
    if end == -1:
        return None, content

    fm_text = content[3:end].strip()
    body = content[end + 3 :].lstrip("\n")

    if not fm_text:
        return {}, body

    try:
        fm = yaml.safe_load(fm_text)
        if isinstance(fm, dict):
            return fm, body
        return {}, body
    except yaml.YAMLError:
        return {}, body


def write_frontmatter(fm: Dict[str, Any], body: str) -> str:
    """Serialize frontmatter dict + body back to markdown.

    Args:
        fm: Frontmatter dictionary (can contain Chinese or English keys).
        body: Markdown body content.

    Returns:
        Full markdown string with YAML frontmatter.
    """
    if not fm:
        return body

    fm_text = yaml.safe_dump(
        fm,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()

    return f"---\n{fm_text}\n---\n\n{body}"
