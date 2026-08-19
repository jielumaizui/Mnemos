# -*- coding: utf-8 -*-
"""Relation endpoint quality gates.

These checks are intentionally conservative.  They block values that cannot be
valid KG endpoints in any supported namespace, while leaving ambiguous legacy
paths for the endpoint normalizer to migrate instead of deleting them.
"""

from __future__ import annotations

import re
from pathlib import Path

INVALID_ENDPOINT_LITERALS = frozenset({"", "---"})
_PRUNABLE_INTERNAL_PREFIXES = (
    "07-Shadow/",
    "L2.4-KG/Relations/",
)
_BLOCKED_INTERNAL_PREFIXES = (
    "07-Shadow/",
    "L2.4-KG/Relations/",
)
_ATTACHMENT_SUFFIXES = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".pdf",
)
_CHINESE_FRAGMENT_STARTS = (
    "在",
    "被",
    "通过",
    "以及",
    "其他",
    "决定",
    "如果",
    "已部署",
    "该系统",
    "对系统",
    "对比其他",
    "每一个",
)
_CHINESE_FRAGMENT_ENDS = ("的", "了", "有", "于", "与", "在", "中")
_CHINESE_FRAGMENT_MIDDLE = ("与", "在")
_ZH_TECH_INDICATORS = (
    "数据库",
    "服务器",
    "客户端",
    "中间件",
    "微服务",
    "程序",
    "框架",
    "服务",
    "系统",
    "平台",
    "引擎",
    "模型",
    "算法",
    "接口",
    "协议",
    "组件",
    "模块",
)
_ZH_TECH_INDICATOR_PATTERN = "|".join(
    re.escape(indicator) for indicator in sorted(_ZH_TECH_INDICATORS, key=len, reverse=True)
)
_ZH_TECH_LABEL_PATTERN = re.compile(
    r"(?:技术|工具|框架|系统|平台|服务|组件|模块|协议|接口|数据库)"
    r"[\s:：]+([\w\-一-鿿]{2,20})"
)
_ZH_TECH_QUOTED_PATTERN = re.compile(
    rf"[「『\"'`]([\w\-一-鿿]{{2,20}}(?:{_ZH_TECH_INDICATOR_PATTERN}))[」』\"'`]"
)
_DERIVED_SCAN_DIRS = {
    "07-Shadow",
    "99-Archive",
    "99-Reports",
}


def _endpoint_text(endpoint: object) -> str:
    return str(endpoint or "").strip()


def _is_short_chinese_fragment(text: str) -> bool:
    has_chinese = any("\u4e00" <= ch <= "\u9fff" for ch in text)
    has_ascii_or_digit = any(ch.isascii() and ch.isalnum() for ch in text)
    if not has_chinese or has_ascii_or_digit or len(text) > 12:
        return False
    if text.endswith(_CHINESE_FRAGMENT_ENDS):
        return True
    if text.startswith(_CHINESE_FRAGMENT_STARTS):
        return True
    return any(marker in text[1:-1] for marker in _CHINESE_FRAGMENT_MIDDLE)


def prunable_relation_endpoint_reason(endpoint: object) -> str:
    """Return a deletion-safe reason for endpoints that should never be kept."""
    text = _endpoint_text(endpoint)
    if text in INVALID_ENDPOINT_LITERALS:
        return "empty_or_marker"
    if "\n" in text or "\r" in text:
        return "multiline_fragment"
    if text.startswith(_PRUNABLE_INTERNAL_PREFIXES):
        return "stale_internal_projection"
    if text.lower().endswith(_ATTACHMENT_SUFFIXES):
        return "attachment_file"
    if "/" not in text and "\\" not in text and not text.endswith(".md"):
        if _is_short_chinese_fragment(text):
            return "invalid_entity_fragment"
    return ""


def relation_endpoint_rejection_reason(endpoint: object) -> str:
    """Return why an endpoint must be rejected at write time, or an empty string."""
    text = _endpoint_text(endpoint)
    reason = prunable_relation_endpoint_reason(text)
    if reason:
        return reason
    if text.startswith(_BLOCKED_INTERNAL_PREFIXES):
        return "internal_kg_projection"
    if not any(ch.isalpha() or "\u4e00" <= ch <= "\u9fff" for ch in text):
        return "no_word_character"
    return ""


def is_valid_relation_endpoint(endpoint: object) -> bool:
    return relation_endpoint_rejection_reason(endpoint) == ""


def extract_labeled_chinese_tech_terms(text: str) -> set[str]:
    """Extract explicit Chinese technology names without arbitrary sentence slicing."""
    terms: set[str] = set()
    for pattern in (_ZH_TECH_LABEL_PATTERN, _ZH_TECH_QUOTED_PATTERN):
        for match in pattern.finditer(text):
            terms.add(match.group(1).strip())
    return terms


def is_derived_kg_scan_path(path: Path, wiki_base: Path | None = None) -> bool:
    """Return whether a Wiki page is a derived artifact, not a KG source page."""
    page = Path(path)
    try:
        parts = page.relative_to(wiki_base).parts if wiki_base else page.parts
    except ValueError:
        parts = page.parts

    if any(part.startswith(".") for part in parts):
        return True
    if any(part in _DERIVED_SCAN_DIRS for part in parts):
        return True
    if parts and parts[0] == "L2.4-KG":
        return True
    if len(parts) >= 2 and parts[:2] == ("05-MOCs", "Mnemos-Navigation"):
        return True
    if (
        len(parts) >= 3
        and parts[0] == "06-Retrospectives"
        and parts[1] == "entropy"
        and parts[-1].startswith("entropy-suggestions")
    ):
        return True
    return False
