"""Golden/characterization tests for scripts/refactor_magic_numbers.py.

These tests lock the exact naming rules before any cyclomatic-complexity
refactoring of derive_semantic_name().  The AST fixtures are intentionally
small and constructed from source snippets so the node_stack and assignment
context match real usage.
"""

from __future__ import annotations

import ast
import json

import pytest

from scripts.refactor_magic_numbers import (
    CORE_RELATIVE_PREFIX,
    DURATION_BUCKET_NAMES,
    clean_name,
    derive_semantic_name,
    fallback_name,
    is_duration_bucket_context,
    is_in_slice,
    kwarg_name_from_line,
    load_occurrences,
    nearby_word,
    unit_suffix,
)


def _stack_for(source: str, value: int) -> tuple[list[ast.AST], ast.Assign | ast.AnnAssign | None, str]:
    """Parse *source*, find the token for *value*, and return its AST stack.

    The returned tuple is (node_stack, assignment_or_None, line_text).
    """
    tree = ast.parse(source)
    lines = source.splitlines()
    value_str = str(value)
    for lineno, line in enumerate(lines, start=1):
        if value_str not in line:
            continue
        col = line.index(value_str)
        stack = _get_stack(tree, lineno, col)
        assignment = _find_assignment(stack)
        return stack, assignment, line.rstrip("\n")
    raise ValueError(f"value {value} not found in source")


def _get_stack(tree: ast.AST, line: int, col: int) -> list[ast.AST]:
    """Minimal port of get_stack_for_position for test construction."""
    stack = [tree]
    changed = True
    while changed:
        changed = False
        for child in ast.iter_child_nodes(stack[-1]):
            start = getattr(child, "lineno", None)
            end = getattr(child, "end_lineno", None)
            if start is None or end is None:
                continue
            start_col = getattr(child, "col_offset", 0)
            end_col = getattr(child, "end_col_offset", 0)
            contains = False
            if start < line < end:
                contains = True
            elif start == line == end and start_col <= col < end_col:
                contains = True
            elif start == line and end > line:
                contains = col >= start_col
            elif end == line and start < line:
                contains = col < end_col
            if contains:
                stack.append(child)
                changed = True
                break
    return stack


def _find_assignment(stack: list[ast.AST]) -> ast.Assign | ast.AnnAssign | None:
    for node in reversed(stack):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            return node
    return None


# ── unit-level golden tests ──


def test_load_occurrences_uses_core_relative_prefix(tmp_path) -> None:
    audit_path = tmp_path / "audit_data.json"
    audit_path.write_text(
        json.dumps(
            {
                "files": [
                    {
                        "functions": [
                            {
                                "file": f"{CORE_RELATIVE_PREFIX}sample.py",
                                "hardcoded_numbers": [[10, 42], [11, 1000]],
                            },
                            {
                                "file": "scripts/sample.py",
                                "hardcoded_numbers": [[20, 7]],
                            },
                        ]
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    assert CORE_RELATIVE_PREFIX == "core/"
    assert load_occurrences(audit_path) == {("core/sample.py", 10, "42")}


@pytest.mark.parametrize(
    ("value", "context", "expected"),
    [
        # 'timeout'/'refresh'/'cooldown' are in seconds_keys and win before minutes.
        (30, "default_timeout = 30", "_SECONDS"),
        (30, "refresh_every = 30", "_SECONDS"),
        (30, "cooldown_minutes = 30", "_SECONDS"),
        (30, "age_hours = 30", "_HOURS"),
        # days context with a bucket value and not in a slice -> _DAYS.
        (30, "stale_days = 30", "_DAYS"),
        (30, "last_week = 30", "_DAYS"),
        (30, "slice_it[:30]", ""),
        (1000, "max_tokens = 1000", "_TOKENS"),
        (999, "max_tokens = 999", ""),
        (2000, "top_count = 2000", "_LIMIT"),
        (500, "top_count = 500", ""),
    ],
)
def test_unit_suffix(value: int, context: str, expected: str) -> None:
    stack, _, line_text = _stack_for(context, value)
    assert unit_suffix(value, line_text, stack) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (7, "_DAYS_IN_WEEK"),
        (30, "_DAYS_IN_MONTH"),
        (90, "_NINETY_DAYS"),
        (180, "_ONE_HUNDRED_EIGHTY_DAYS"),
        (365, "_DAYS_IN_YEAR"),
        (600, "_SECONDS_IN_TEN_MINUTES"),
        (3600, "_SECONDS_IN_HOUR"),
        (86400, "_SECONDS_IN_DAY"),
        (604800, "_SECONDS_IN_WEEK"),
        (42, "_VALUE_42"),
    ],
)
def test_fallback_name(value: int, expected: str) -> None:
    assert fallback_name(value) == expected


@pytest.mark.parametrize(
    ("line_text", "expected"),
    [
        ("timeout = 30", "timeout"),
        ("def f(retry_count: int = 5):", "retry_count"),
        ("x = 1  # no names", "x"),
    ],
)
def test_kwarg_name_from_line(line_text: str, expected: str | None) -> None:
    value = 30 if "30" in line_text else (5 if "5" in line_text else 1)
    assert kwarg_name_from_line(line_text, value) == expected


@pytest.mark.parametrize(
    ("line_text", "expected"),
    [
        # nearby_word takes the last identifier that is not a noise word.
        ("default_timeout_seconds = 30", "default_timeout_seconds"),
        ("humanize_duration(value)", "humanize_duration"),
        ("if x == 1:", "x"),
    ],
)
def test_nearby_word(line_text: str, expected: str | None) -> None:
    assert nearby_word(line_text) == expected


@pytest.mark.parametrize(
    ("value", "context", "expected"),
    [
        (7, "bucket = humanize(days_ago=7)", True),
        (30, "def f(x):\n    return recency > 30", True),
        (30, "timeout = 30", False),
        (30, "items[:30]", False),
    ],
)
def test_is_duration_bucket_context(value: int, context: str, expected: bool) -> None:
    stack, _, line_text = _stack_for(context, value)
    func = None
    for node in reversed(stack):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func = node
            break
    assert is_duration_bucket_context(line_text, func.name if func else None, stack) == expected


@pytest.mark.parametrize(
    ("source", "value", "expected"),
    [
        ("x = items[1:30]", 30, True),
        ("x = items[30]", 30, False),
    ],
)
def test_is_in_slice(source: str, value: int, expected: bool) -> None:
    stack, _, _ = _stack_for(source, value)
    assert is_in_slice(stack) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", "CONST"),
        ("123abc", "_123abc"),
        ("foo-bar", "foo_bar"),
        ("a@b#c", "a_b_c"),
    ],
)
def test_clean_name(raw: str, expected: str) -> None:
    assert clean_name(raw) == expected


# ── derive_semantic_name golden tests ──


def _derive(source: str, value: int) -> str:
    stack, assignment, line_text = _stack_for(source, value)
    return derive_semantic_name(value, stack, assignment, line_text)


@pytest.mark.parametrize(
    ("source", "value", "expected"),
    [
        # Duration bucket special cases
        (
            "def categorize(d):\n    return humanize(days_ago=7)",
            7,
            "DURATION_BUCKET_WEEK_DAYS",
        ),
        (
            "class Freshness:\n    def bucket(self):\n        return humanize(days_ago=30)",
            30,
            "FRESHNESS_DURATION_BUCKET_MONTH_DAYS",
        ),
        (
            "def cutoff():\n    return duration_bucket(365)",
            365,
            "DURATION_BUCKET_YEAR_DAYS",
        ),
        # Assignment target rules
        ("timeout_seconds = 30", 30, "TIMEOUT_SECONDS"),
        ("max_token_limit = 2000", 2000, "MAX_TOKEN_LIMIT"),
        ("retry_ttl = 600", 600, "RETRY_TTL"),
        (
            "class Worker:\n    COOLDOWN_SEC = 300",
            300,
            "WORKER_COOLDOWN_SEC",
        ),
        # stale_days is both an assignment target and a duration bucket context:
        # the bucket special case runs first and wins.
        ("stale_days = 90", 90, "DURATION_BUCKET_QUARTER_DAYS"),
        # Kwarg rules
        ("def f(window_size=30):\n    pass", 30, "WINDOW_SIZE"),
        ("func(timeout=600)", 600, "TIMEOUT_SECONDS"),
        # 'days' is in the kwarg skip set, so nearby_word ('func') is used.
        ("func(days=7)", 7, "FUNC_DAYS"),
        # Function/class context + nearby word
        (
            "class Config:\n    def load(self):\n        x = default_timeout = 30",
            30,
            "X_SECONDS",
        ),
        (
            "def refresh_items():\n    interval = 120",
            120,
            "INTERVAL",
        ),
        # Context-only fallback (no nearby meaningful word)
        (
            "class Scorer:\n    def compute(self):\n        return 42",
            42,
            "SCORER_COMPUTE",
        ),
        # Plain assignment fallback
        ("x = 42", 42, "X"),
    ],
)
def test_derive_semantic_name(source: str, value: int, expected: str) -> None:
    assert _derive(source, value) == expected


def test_duration_bucket_names_mapping_unchanged() -> None:
    """Lock the exact public bucket-name mapping."""
    assert DURATION_BUCKET_NAMES == {
        7: "DURATION_BUCKET_WEEK_DAYS",
        30: "DURATION_BUCKET_MONTH_DAYS",
        90: "DURATION_BUCKET_QUARTER_DAYS",
        180: "DURATION_BUCKET_HALF_YEAR_DAYS",
        365: "DURATION_BUCKET_YEAR_DAYS",
    }
