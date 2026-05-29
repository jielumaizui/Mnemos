"""
Frontmatter field contract helpers.

Obsidian-facing Markdown uses Chinese field names for readability, while
Python/SQLite/event payloads keep English canonical keys internally.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable


CANONICAL_TO_DISPLAY: Dict[str, str] = {
    "type": "类型",
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
    "source_session": "来源会话",
    "source_agent": "来源Agent",
    "broken_links": "失效链接",
}

DISPLAY_ALIASES: Dict[str, Iterable[str]] = {
    "type": ("类型", "类别"),
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
    "source_session": ("来源会话", "source_session"),
    "source_agent": ("来源Agent", "source_agent"),
    "broken_links": ("失效链接", "broken_links"),
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


def normalize_frontmatter(frontmatter: Dict[str, Any] | None) -> Dict[str, Any]:
    """Normalize mixed Chinese/English frontmatter to English canonical keys."""
    normalized: Dict[str, Any] = {}
    if not isinstance(frontmatter, dict):
        return normalized
    for key, value in frontmatter.items():
        normalized[canonical_key(str(key))] = value
    return normalized


def fm_get(frontmatter: Dict[str, Any] | None, key: str, default: Any = None) -> Any:
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
    canonical = normalize_frontmatter(frontmatter or {})
    if defaults:
        merged = dict(defaults)
        merged.update(canonical)
        canonical = merged

    result: Dict[str, Any] = {}
    for key, value in canonical.items():
        if value is None or value == "":
            continue
        result[CANONICAL_TO_DISPLAY.get(key, key)] = value
    return result
