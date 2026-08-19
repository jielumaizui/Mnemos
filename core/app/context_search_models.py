# -*- coding: utf-8 -*-
"""Typed search-result contract shared by context-search consumers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SearchResult:
    """搜索结果"""

    page_path: str
    title: str
    snippet: str
    score: float
    relevance: float = 0.0
    confidence: float = 0.0
    continuity: float = 0.0
    freshness: float = 0.0
    persona_score: float = 0.0
    context_boost: float = 1.0
    final_score: float = 0.0
    match_reason: str = ""
    freshness_alert: Optional[Any] = None
    verification: str = ""
    source: str = ""
    last_modified: str = ""
    match_source: str = ""  # keyword / semantic / relation / graph / hybrid / fallback
    page_embedding_score: float = 0.0
    relation_score: float = 0.0
    keyword_score: float = 0.0
    score_breakdown: Dict[str, float] = field(default_factory=dict)
    matched_terms: List[str] = field(default_factory=list)
    heat_level: str = "cold"
    heat_score: float = 0.0
    last_accessed: str = ""
    scope: str = ""
    source_agent: str = ""
    session_id: str = ""
    project: str = ""
    tags: List[str] = field(default_factory=list)
    acl_schema_version: int = 0
    acl_metadata_complete: bool = False
    acl_reconciliation_status: str = ""
    entity: str = ""
    result_kind: str = "wiki_page"
    object_type: str = ""
    object_id: str = ""
    revision_id: str = ""
    matched_field: str = ""
    source_revision_id: str = ""
    source_span_ids: List[str] = field(default_factory=list)
    acl_decision: str = ""
    supersedes_revision_id: str = ""
    is_current: bool = True

    @property
    def page_title(self) -> str:
        return self.title

    @property
    def excerpt(self) -> str:
        return self.snippet
