"""Tests for core.cli.commands.search."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from core.cli.commands.search import (
    _format_breakdown_detail,
    _format_score_detail,
    _format_search_badges,
    _handle_search_error,
    _print_search_json,
    _print_search_result,
    _print_search_results,
    _run_context_search,
    _search_result_to_dict,
    cmd_search,
)


def _make_result(**overrides):
    defaults = {
        "title": "Note",
        "page_path": "/path/to/note.md",
        "score": 0.85,
        "snippet": "A short snippet for testing.",
        "match_reason": "keyword match",
        "source": "obsidian",
        "verification": "verified",
        "match_source": "fts",
        "matched_terms": ["ai", "memory"],
        "score_breakdown": {
            "relevance": 0.8,
            "confidence": 0.7,
            "freshness": 0.6,
            "keyword": 0.9,
        },
        "page_embedding_score": 0.12,
        "relation_score": 0.34,
        "keyword_score": 0.56,
        "relevance": 0.78,
        "confidence": 0.9,
        "continuity": 0.11,
        "freshness": 0.22,
        "persona_score": 0.33,
        "context_boost": 1.5,
        "final_score": 0.88,
        "heat_level": "warm",
        "heat_score": 0.5,
        "last_accessed": "2026-06-25T10:00:00",
        "freshness_alert": None,
        "last_modified": "2026-06-24T12:00:00",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_search_result_to_dict_full():
    result = _make_result()
    d = _search_result_to_dict(result)

    assert d["title"] == "Note"
    assert d["path"] == "/path/to/note.md"
    assert d["score"] == 0.85
    assert d["snippet"] == "A short snippet for testing."
    assert d["reason"] == "keyword match"
    assert d["source"] == "obsidian"
    assert d["verification"] == "verified"
    assert d["match_source"] == "fts"
    assert d["matched_terms"] == ["ai", "memory"]
    assert d["score_breakdown"]["relevance"] == 0.8
    assert d["scores"]["page_embedding"] == 0.12
    assert d["scores"]["context_boost"] == 1.5
    assert d["heat"]["level"] == "warm"
    assert d["heat"]["last_accessed"] == "2026-06-25T10:00:00"
    assert d["freshness_alert"] is None
    assert d["last_modified"] == "2026-06-24T12:00:00"


def test_search_result_to_dict_defaults():
    result = SimpleNamespace(title="T", page_path="p.md", score=0.5)
    d = _search_result_to_dict(result)

    assert d["title"] == "T"
    assert d["path"] == "p.md"
    assert d["score"] == 0.5
    assert d["snippet"] == ""
    assert d["matched_terms"] == []
    assert d["score_breakdown"] == {}
    assert d["scores"]["context_boost"] == 1.0
    assert d["heat"]["level"] == "cold"


def test_print_search_json(capsys):
    result = _make_result(title="JSON Note", page_path="json.md")
    _print_search_json("test query", [result])

    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["query"] == "test query"
    assert parsed["count"] == 1
    assert parsed["results"][0]["title"] == "JSON Note"
    assert parsed["results"][0]["path"] == "json.md"


def test_format_search_badges():
    assert _format_search_badges(_make_result()) == " (verified, 来源:obsidian)"
    assert _format_search_badges(_make_result(verification="", source="")) == ""
    assert _format_search_badges(_make_result(verification="", source="src")) == " (来源:src)"


def test_format_score_detail():
    assert _format_score_detail(_make_result()) == " [p=0.12 r=0.34 k=0.56]"
    assert _format_score_detail(_make_result(page_embedding_score=0.0, relation_score=0.0)) == ""


def test_format_breakdown_detail():
    assert _format_breakdown_detail(_make_result()) == "relevance=0.8 confidence=0.7 freshness=0.6 keyword=0.9"
    assert _format_breakdown_detail(_make_result(score_breakdown={})) == ""
    assert (
        _format_breakdown_detail(_make_result(score_breakdown={"relevance": 0.1, "unknown": 0.2}))
        == "relevance=0.1"
    )


def test_print_search_result_full(capsys):
    _print_search_result(_make_result(title="Full Note"), 1)
    captured = capsys.readouterr()
    lines = captured.out.split("\n")
    assert lines[0] == "  1. [0.85] [p=0.12 r=0.34 k=0.56] Full Note (verified, 来源:obsidian)"
    assert lines[1] == "     A short snippet for testing...."
    assert lines[2] == "     原因: keyword match"
    assert lines[3] == "     命中词: ai, memory"
    assert lines[4] == "     分数解释: relevance=0.8 confidence=0.7 freshness=0.6 keyword=0.9"
    assert lines[5] == "     路径: /path/to/note.md"


def test_print_search_result_minimal(capsys):
    result = SimpleNamespace(
        title="Minimal",
        page_path="min.md",
        score=0.1,
        snippet="",
        matched_terms=[],
        score_breakdown={},
    )
    _print_search_result(result, 2)
    captured = capsys.readouterr()
    assert captured.out == "  2. [0.10] Minimal\n     路径: min.md\n"


def test_print_search_results_empty(capsys):
    _print_search_results("empty query", [])
    captured = capsys.readouterr()
    assert captured.out == "未找到与 'empty query' 相关的知识\n"


def test_print_search_results_non_empty(capsys):
    result = SimpleNamespace(
        title="Only",
        page_path="only.md",
        score=0.99,
        snippet="",
        matched_terms=[],
        score_breakdown={},
    )
    _print_search_results("q", [result])
    captured = capsys.readouterr()
    assert captured.out.startswith("搜索结果 (1 条):\n")
    assert "  1. [0.99] Only\n     路径: only.md\n" in captured.out


def test_run_context_search():
    args = SimpleNamespace(query="hello", limit=5)
    mock_results = [SimpleNamespace(title="R")]

    with patch("core.app.context_search.ContextAwareSearch") as MockSearch:
        MockSearch.return_value.search.return_value = mock_results
        query, results = _run_context_search(args)

    assert query == "hello"
    assert results == mock_results
    MockSearch.return_value.search.assert_called_once_with("hello", limit=5)


def test_run_context_search_default_limit():
    args = SimpleNamespace(query="hello", limit=None)

    with patch("core.app.context_search.ContextAwareSearch") as MockSearch:
        MockSearch.return_value.search.return_value = []
        _run_context_search(args)

    MockSearch.return_value.search.assert_called_once_with("hello", limit=10)


def test_handle_search_error_text(capsys):
    args = SimpleNamespace(query="q", json=False)
    _handle_search_error(args, ValueError("boom"))
    captured = capsys.readouterr()
    assert captured.out == "搜索失败: boom\n"


def test_handle_search_error_json(capsys):
    args = SimpleNamespace(query="q", json=True)
    _handle_search_error(args, ValueError("boom"))
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed == {
        "query": "q",
        "count": 0,
        "results": [],
        "error": "boom",
    }


def test_cmd_search_json(capsys):
    args = SimpleNamespace(query="hello", limit=3, json=True)
    result = _make_result(title="Cmd Note")

    with patch("core.app.context_search.ContextAwareSearch") as MockSearch:
        MockSearch.return_value.search.return_value = [result]
        cmd_search(args)

    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["query"] == "hello"
    assert parsed["count"] == 1
    assert parsed["results"][0]["title"] == "Cmd Note"


def test_cmd_search_human(capsys):
    args = SimpleNamespace(query="hello", limit=3, json=False)
    result = SimpleNamespace(
        title="Human",
        page_path="h.md",
        score=0.5,
        snippet="",
        matched_terms=[],
        score_breakdown={},
    )

    with patch("core.app.context_search.ContextAwareSearch") as MockSearch:
        MockSearch.return_value.search.return_value = [result]
        cmd_search(args)

    captured = capsys.readouterr()
    assert captured.out == "搜索结果 (1 条):\n  1. [0.50] Human\n     路径: h.md\n"


def test_cmd_search_no_results(capsys):
    args = SimpleNamespace(query="missing", limit=3, json=False)

    with patch("core.app.context_search.ContextAwareSearch") as MockSearch:
        MockSearch.return_value.search.return_value = []
        cmd_search(args)

    captured = capsys.readouterr()
    assert captured.out == "未找到与 'missing' 相关的知识\n"


def test_cmd_search_error_json(capsys):
    args = SimpleNamespace(query="bad", limit=3, json=True)

    with patch("core.app.context_search.ContextAwareSearch") as MockSearch:
        MockSearch.side_effect = ImportError("no module")
        cmd_search(args)

    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["query"] == "bad"
    assert parsed["count"] == 0
    assert parsed["results"] == []
    assert parsed["error"] == "no module"


def test_cmd_search_error_human(capsys):
    args = SimpleNamespace(query="bad", limit=3, json=False)

    with patch("core.app.context_search.ContextAwareSearch") as MockSearch:
        MockSearch.side_effect = ImportError("no module")
        cmd_search(args)

    captured = capsys.readouterr()
    assert captured.out == "搜索失败: no module\n"
