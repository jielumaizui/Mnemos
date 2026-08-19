"""Search command for Mnemos CLI."""

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _mock_safe(value: Any, default: Any = "") -> Any:
    if value is None:
        return default
    if value.__class__.__module__.startswith("unittest.mock"):
        return default
    return value


def _result_attr(result: Any, name: str, default: Any = "") -> Any:
    return _mock_safe(getattr(result, name, default), default)


def _float_attr(result: Any, name: str, default: float = 0.0) -> float:
    value = _result_attr(result, name, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _jsonable(value: Any) -> Any:
    value = _mock_safe(value, None)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _search_result_to_dict(result: Any) -> dict[str, Any]:
    score_breakdown = _result_attr(result, "score_breakdown", {}) or {}
    matched_terms = _result_attr(result, "matched_terms", []) or []
    return {
        "title": str(_result_attr(result, "title", "")),
        "path": str(_result_attr(result, "page_path", "")),
        "score": _float_attr(result, "score"),
        "snippet": str(_result_attr(result, "snippet", "")),
        "reason": str(_result_attr(result, "match_reason", "")),
        "source": str(_result_attr(result, "source", "")),
        "verification": str(_result_attr(result, "verification", "")),
        "match_source": str(_result_attr(result, "match_source", "")),
        "matched_terms": [str(term) for term in matched_terms],
        "score_breakdown": _jsonable(score_breakdown) or {},
        "scores": {
            "page_embedding": _float_attr(result, "page_embedding_score"),
            "relation": _float_attr(result, "relation_score"),
            "keyword": _float_attr(result, "keyword_score"),
            "relevance": _float_attr(result, "relevance"),
            "confidence": _float_attr(result, "confidence"),
            "continuity": _float_attr(result, "continuity"),
            "freshness": _float_attr(result, "freshness"),
            "persona": _float_attr(result, "persona_score"),
            "context_boost": _float_attr(result, "context_boost", 1.0),
            "final": _float_attr(result, "final_score"),
        },
        "heat": {
            "level": str(_result_attr(result, "heat_level", "cold")),
            "score": _float_attr(result, "heat_score"),
            "last_accessed": str(_result_attr(result, "last_accessed", "")),
        },
        "freshness_alert": _jsonable(_result_attr(result, "freshness_alert", None)),
        "last_modified": str(_result_attr(result, "last_modified", "")),
    }


def _print_search_json(query: str, results: list[Any]) -> None:
    payload = {
        "query": query,
        "count": len(results),
        "results": [_search_result_to_dict(result) for result in results],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _run_context_search(args):
    """Execute the context search and return (query, results)."""
    from core.app.context_search import ContextAwareSearch

    search = ContextAwareSearch()
    results = search.search(args.query, limit=args.limit or 10)
    return args.query, results


def _format_search_badges(result: Any) -> str:
    """Build the parenthetical badge string for a search result."""
    badges = []
    if getattr(result, "verification", ""):
        badges.append(result.verification)
    if getattr(result, "source", ""):
        badges.append(f"来源:{result.source}")
    return f" ({', '.join(badges)})" if badges else ""


def _format_score_detail(result: Any) -> str:
    """Format the embedding/relation/keyword score detail fragment."""
    if (
        getattr(result, "page_embedding_score", 0.0) > 0
        or getattr(result, "relation_score", 0.0) > 0
    ):
        return (
            f" [p={result.page_embedding_score:.2f} r={result.relation_score:.2f} "
            f"k={result.keyword_score:.2f}]"
        )
    return ""


def _format_breakdown_detail(result: Any) -> str:
    """Format the score breakdown detail fragment if present."""
    breakdown = getattr(result, "score_breakdown", {}) or {}
    if not breakdown:
        return ""
    detail = " ".join(
        f"{key}={breakdown[key]}"
        for key in ("relevance", "confidence", "freshness", "keyword")
        if key in breakdown
    )
    return detail


def _print_search_result(result: Any, index: int) -> None:
    """Print a single human-readable search result."""
    badge_str = _format_search_badges(result)
    score_detail = _format_score_detail(result)
    print(f"  {index}. [{result.score:.2f}]{score_detail} {result.title}{badge_str}")
    if result.snippet:
        snippet = result.snippet[:80].replace("\n", " ")
        print(f"     {snippet}...")
    if getattr(result, "match_reason", ""):
        print(f"     原因: {result.match_reason}")
    matched_terms = getattr(result, "matched_terms", []) or []
    if matched_terms:
        print(f"     命中词: {', '.join(str(t) for t in matched_terms[:8])}")
    breakdown_detail = _format_breakdown_detail(result)
    if breakdown_detail:
        print(f"     分数解释: {breakdown_detail}")
    print(f"     路径: {result.page_path}")


def _print_search_results(query: str, results: list[Any]) -> None:
    """Print human-readable search results."""
    if not results:
        print(f"未找到与 '{query}' 相关的知识")
        return

    print(f"搜索结果 ({len(results)} 条):")
    for i, result in enumerate(results, 1):
        _print_search_result(result, i)


def _handle_search_error(args: Any, exc: Exception) -> None:
    """Print a search error in the requested output format."""
    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "query": getattr(args, "query", ""),
                    "count": 0,
                    "results": [],
                    "error": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    print(f"搜索失败: {exc}")


def cmd_search(args):
    """上下文感知搜索"""
    try:
        query, results = _run_context_search(args)
        if getattr(args, "json", False):
            _print_search_json(query, results)
            return
        _print_search_results(query, results)
    except (ImportError, AttributeError, OSError) as e:
        _handle_search_error(args, e)
