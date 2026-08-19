"""
Minimal benchmark smoke tests for hot import paths.

This module intentionally avoids heavy fixtures or real I/O.  It only:
- imports the hot modules listed in tests/benchmark/__init__.py
- exercises trivial, deterministic operations on them
- asserts generous wall-time ceilings so the suite stays green on slow CI runners.

If pytest-benchmark is installed, an extra set of benchmark-enabled tests is
registered automatically.  We do not add pytest-benchmark as a dependency.
"""

from __future__ import annotations

import time
from typing import Any, Callable

# Hot-path imports: keep them at module level so import time is measured too.
from core.sync_framework import sync_engine
from core.kia import knowledge_graph
from core.app import context_search

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _timed(call: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[Any, float]:
    """Run ``call(*args, **kwargs)`` and return (result, elapsed_seconds)."""
    start = time.perf_counter()
    result = call(*args, **kwargs)
    elapsed = time.perf_counter() - start
    return result, elapsed


# ---------------------------------------------------------------------------
# Plain smoke tests with generous time ceilings
# ---------------------------------------------------------------------------


class TestSyncEngineSmoke:
    """Smoke benchmarks for core.sync_framework.sync_engine."""

    def test_sync_engine_symbols_imported(self):
        assert hasattr(sync_engine, "SyncEngine")
        assert hasattr(sync_engine, "sanitize_content")
        assert callable(sync_engine.sanitize_content)

    def test_sanitize_content_performance(self):
        text = "token" + "=abc123 password" + "=supersecret secret" + "=my-value"
        result, elapsed = _timed(sync_engine.sanitize_content, text)
        # Deterministic correctness check.
        assert "supersecret" not in result
        assert "my-value" not in result
        assert elapsed < 1.0, f"sanitize_content took {elapsed:.3f}s"


class TestKnowledgeGraphSmoke:
    """Smoke benchmarks for core.kia.knowledge_graph."""

    def test_knowledge_graph_symbols_imported(self):
        assert hasattr(knowledge_graph, "KnowledgeGraph")

    def test_knowledge_graph_schema_defined(self):
        # The schema string is created at import time; just assert it exists.
        assert hasattr(knowledge_graph, "DB_SCHEMA")
        assert "CREATE TABLE" in knowledge_graph.DB_SCHEMA


class TestContextSearchSmoke:
    """Smoke benchmarks for core.app.context_search."""

    def test_context_search_symbols_imported(self):
        assert hasattr(context_search, "ContextAwareSearch")
        assert hasattr(context_search, "SearchResult")

    def test_search_result_dataclass_performance(self):
        result, elapsed = _timed(
            context_search.SearchResult,
            page_path="test.md",
            title="Test",
            snippet="hello world",
            score=0.5,
        )
        assert result.page_path == "test.md"
        assert result.title == "Test"
        assert elapsed < 0.5, f"SearchResult creation took {elapsed:.3f}s"


# ---------------------------------------------------------------------------
# Optional pytest-benchmark harness (only registered when the plugin is present)
# ---------------------------------------------------------------------------

try:
    import pytest_benchmark  # noqa: F401

    _BENCHMARK_AVAILABLE = True
except ImportError:  # pragma: no cover
    _BENCHMARK_AVAILABLE = False


if _BENCHMARK_AVAILABLE:

    class TestSyncEngineBenchmark:
        def test_sanitize_content_benchmark(self, benchmark):
            text = "token" + "=abc123 password" + "=supersecret secret" + "=my-value"
            result = benchmark(sync_engine.sanitize_content, text)
            assert "supersecret" not in result

    class TestContextSearchBenchmark:
        def test_search_result_creation_benchmark(self, benchmark):
            result = benchmark(
                context_search.SearchResult,
                page_path="test.md",
                title="Test",
                snippet="hello world",
                score=0.5,
            )
            assert result.page_path == "test.md"
