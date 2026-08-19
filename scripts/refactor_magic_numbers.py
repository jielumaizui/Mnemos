#!/usr/bin/env python3
# mypy: ignore-errors
"""
Refactor P2 magic numbers in core/ into module-level constants.

Usage:
    python3 scripts/refactor_magic_numbers.py --audit-data audit_data.json --dry-run
    python3 scripts/refactor_magic_numbers.py --audit-data audit_data.json --dry-run -v
    python3 scripts/refactor_magic_numbers.py --audit-data audit_data.json --apply --run-tests
"""

from __future__ import annotations

import argparse
import ast
import difflib
import json
import re
import subprocess
import sys
import tokenize
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

PROJECT_DIR = Path(__file__).resolve().parents[1]
CORE_DIR = PROJECT_DIR / "core"
CORE_RELATIVE_PREFIX = f"{CORE_DIR.relative_to(PROJECT_DIR).as_posix()}/"

SKIP_NUMBERS = {"1000", "1024", "2000", "2020", "2021", "2022", "2023", "2024", "2025", "2026"}

NOISE_WORDS = {
    "self",
    "def",
    "return",
    "if",
    "else",
    "elif",
    "for",
    "in",
    "and",
    "or",
    "not",
    "is",
    "None",
    "True",
    "False",
    "with",
    "as",
    "try",
    "except",
    "finally",
    "raise",
    "import",
    "from",
    "class",
    "lambda",
    "pass",
    "continue",
    "break",
    "yield",
    "async",
    "await",
    "global",
    "nonlocal",
    # Type annotations / built-in types
    "int",
    "str",
    "float",
    "bool",
    "list",
    "dict",
    "tuple",
    "set",
    "bytes",
    "object",
    "List",
    "Dict",
    "Tuple",
    "Set",
    "Optional",
    "Any",
    "Union",
    "Callable",
    "Iterator",
    "Iterable",
    "Sequence",
    "Mapping",
    "Type",
    " cast",
    # Generic units that don't add semantic meaning
    "days",
    "day",
    "seconds",
    "second",
    "minutes",
    "minute",
    "hours",
    "hour",
    "weeks",
    "week",
    "months",
    "month",
    "years",
    "year",
    "value",
}


def to_snake_upper(name: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return s.upper().replace("-", "_").replace(".", "_")


def clean_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_")
    if not name:
        return "CONST"
    if name[0].isdigit():
        name = "_" + name
    return name


def unit_suffix(value: int, context: str, node_stack: list[ast.AST]) -> str:
    ctx = context.lower()
    seconds_keys = ["second", "seconds", "timeout", "cooldown", "refresh", "ttl"]
    minutes_keys = ["minute", "minutes"]
    hours_keys = ["hour", "hours"]
    days_keys = ["day", "days", "timedelta", "datetime"]
    weeks_keys = ["week", "weeks"]
    time_keys = (
        seconds_keys
        + minutes_keys
        + hours_keys
        + days_keys
        + weeks_keys
        + [
            "ttl",
            "half_life",
            "half-life",
            "age",
            "fresh",
            "remind",
            "reminder",
            "schedule",
            "cron",
            "interval",
            "period",
            "duration",
            "ago",
        ]
    )
    token_keys = ["token", "tokens", "max_token"]
    limit_keys = ["limit", "top", "max_count", "count"]

    if any(k in ctx for k in time_keys):
        if value in (600, 3600, 86400, 604800, 1440) or any(k in ctx for k in seconds_keys):
            return "_SECONDS"
        if any(k in ctx for k in minutes_keys):
            return "_MINUTES"
        if any(k in ctx for k in hours_keys):
            return "_HOURS"
        if value in (7, 30, 90, 180, 365) and not is_in_slice(node_stack):
            return "_DAYS"
        return ""
    if any(k in ctx for k in token_keys) and value >= 1000:
        return "_TOKENS"
    if any(k in ctx for k in limit_keys) and value >= 1000:
        return "_LIMIT"
    return ""


def fallback_name(value: int) -> str:
    mapping = {
        7: "_DAYS_IN_WEEK",
        30: "_DAYS_IN_MONTH",
        90: "_NINETY_DAYS",
        180: "_ONE_HUNDRED_EIGHTY_DAYS",
        365: "_DAYS_IN_YEAR",
        600: "_SECONDS_IN_TEN_MINUTES",
        3600: "_SECONDS_IN_HOUR",
        86400: "_SECONDS_IN_DAY",
        604800: "_SECONDS_IN_WEEK",
    }
    if value in mapping:
        return mapping[value]
    return f"_VALUE_{value}"


@dataclass
class Replacement:
    line: int
    start_col: int
    end_col: int
    old: str
    new: str


@dataclass
class Constant:
    name: str
    value: int
    value_str: str
    reason: str


@dataclass
class FilePlan:
    constants: list[Constant] = field(default_factory=list)
    replacements: list[Replacement] = field(default_factory=list)


def load_occurrences(audit_path: Path) -> set[tuple[str, int, str]]:
    data = json.loads(audit_path.read_text(encoding="utf-8"))
    occurrences: set[tuple[str, int, str]] = set()
    for item in data.get("files", []):
        for fn in item.get("functions", []):
            file_path = fn.get("file")
            if not file_path or not file_path.startswith(CORE_RELATIVE_PREFIX):
                continue
            for ln, val in fn.get("hardcoded_numbers", []):
                if str(val) in SKIP_NUMBERS:
                    continue
                occurrences.add((file_path, int(ln), str(val)))
    return occurrences


def find_number_tokens(src: str) -> list[tuple[int, int, int, str]]:
    tokens: list[tuple[int, int, int, str]] = []
    try:
        for tok in tokenize.generate_tokens(iter(src.splitlines(keepends=True)).__next__):
            if tok.type == tokenize.NUMBER:
                tokens.append((tok.start[0], tok.start[1], tok.end[1], tok.string))
    except tokenize.TokenError:
        pass
    return tokens


def get_stack_for_position(tree: ast.AST, line: int, col: int) -> list[ast.AST]:
    """Return the AST node stack from root to the innermost node containing (line, col)."""
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


def enclosing(node_stack: list[ast.AST], *types) -> Optional[ast.AST]:
    for node in reversed(node_stack):
        if isinstance(node, types):
            return node
    return None


def enclosing_assignment(node_stack: list[ast.AST]) -> Optional[ast.Assign | ast.AnnAssign]:
    # Prefer the innermost assignment whose RHS contains the literal.
    for node in reversed(node_stack):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            return node
    return None


def assignment_target_name(node: ast.Assign | ast.AnnAssign) -> Optional[str]:
    if isinstance(node, ast.AnnAssign):
        return _target_name(node.target)
    for target in node.targets:
        name = _target_name(target)
        if name:
            return name
    return None


def _target_name(target: ast.AST) -> Optional[str]:
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    if isinstance(target, ast.Subscript):
        return _target_name(target.value)
    return None


def kwarg_name_from_line(line_text: str, value: int) -> Optional[str]:
    # Match keyword arg / parameter with optional type annotation.
    pattern = re.compile(
        r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*(?::\s*[A-Za-z_][A-Za-z0-9_\[\]|,\s]*)?\s*=\s*"
        + re.escape(str(value))
        + r"\b"
    )
    m = pattern.search(line_text)
    return m.group(1) if m else None


def nearby_word(line_text: str) -> Optional[str]:
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", line_text)
    candidates = [w for w in words if w not in NOISE_WORDS and not w.startswith(("__", "_"))]
    if candidates:
        # Prefer the last meaningful word on the line, it usually describes the value.
        return candidates[-1]
    return None


DURATION_BUCKET_VALUES = {7, 30, 90, 180, 365}
DURATION_BUCKET_NAMES = {
    7: "DURATION_BUCKET_WEEK_DAYS",
    30: "DURATION_BUCKET_MONTH_DAYS",
    90: "DURATION_BUCKET_QUARTER_DAYS",
    180: "DURATION_BUCKET_HALF_YEAR_DAYS",
    365: "DURATION_BUCKET_YEAR_DAYS",
}


def is_in_slice(node_stack: list[ast.AST]) -> bool:
    for node in node_stack:
        if isinstance(node, ast.Slice):
            return True
    return False


def is_duration_bucket_context(
    line_text: str, func_name: Optional[str], node_stack: list[ast.AST]
) -> bool:
    if is_in_slice(node_stack):
        return False
    ctx = (line_text + " " + (func_name or "")).lower()
    # Only treat as a duration bucket when the context is explicitly about
    # categorizing durations / freshness / recency. TTL/timeout/cooldown are
    # not buckets.
    duration_keys = [
        "humanize",
        "duration",
        "days_ago",
        "fresh",
        "stale",
        "expired",
        "recency",
        "recent",
        "bucket",
        "period",
        "half_life",
        "half-life",
        "cutoff",
        "retention",
        "age",
    ]
    return any(k in ctx for k in duration_keys)


def _name_from_duration_bucket(
    value: int,
    node_stack: list[ast.AST],
    cls: Optional[ast.ClassDef],
    func_name: Optional[str],
    line_text: str,
) -> Optional[str]:
    """Return a duration-bucket constant name if the context matches."""
    if value not in DURATION_BUCKET_VALUES:
        return None
    if not is_duration_bucket_context(line_text, func_name, node_stack):
        return None
    bucket_name = DURATION_BUCKET_NAMES[value]
    if cls:
        cls_prefix = to_snake_upper(cls.name)
        return f"{cls_prefix}_{bucket_name}"
    return bucket_name


def _name_from_assignment_target(
    assignment: ast.Assign | ast.AnnAssign,
    suffix: str,
    cls: Optional[ast.ClassDef],
    func: Optional[ast.FunctionDef | ast.AsyncFunctionDef],
) -> Optional[str]:
    """Derive a name from the assignment target, preserving unit suffix rules."""
    target = assignment_target_name(assignment)
    if not target:
        return None
    target_upper = to_snake_upper(target)
    # Drop redundant suffix if target already carries a unit.
    if target_upper.endswith(
        ("_DAYS", "_SECONDS", "_MINUTES", "_HOURS", "_LIMIT", "_TOKENS", "_TTL", "_SEC")
    ):
        name = target_upper
    else:
        name = target_upper + suffix
    # If this is a class-level constant assignment, prefix with class name for clarity.
    if cls and func is None:
        cls_prefix = to_snake_upper(cls.name)
        if not name.startswith(cls_prefix):
            name = f"{cls_prefix}_{name}"
    return name


_KWARG_SKIP_WORDS = {"days", "seconds", "minutes", "hours", "weeks", "value"}


def _name_from_kwarg(line_text: str, value: int, suffix: str) -> Optional[str]:
    """Derive a name from a keyword argument / parameter name on the line."""
    kw = kwarg_name_from_line(line_text, value)
    if not kw or kw.lower() in _KWARG_SKIP_WORDS:
        return None
    kw_upper = to_snake_upper(kw)
    if kw_upper.endswith(
        ("_DAYS", "_SECONDS", "_MINUTES", "_HOURS", "_LIMIT", "_TOKENS", "_TTL", "_SEC")
    ):
        return kw_upper
    return kw_upper + suffix


def _name_from_context(
    line_text: str,
    suffix: str,
    cls: Optional[ast.ClassDef],
    func: Optional[ast.FunctionDef | ast.AsyncFunctionDef],
) -> Optional[str]:
    """Derive a name from function/class scope plus the nearest meaningful word."""
    parts = []
    if cls:
        parts.append(to_snake_upper(cls.name))
    if func:
        parts.append(to_snake_upper(func.name))
    word = nearby_word(line_text)
    word_upper = to_snake_upper(word) if word else ""
    # Avoid duplicating the class/function name as the nearby word.
    if word_upper and word_upper not in parts:
        parts.append(word_upper)
    if parts:
        return "_".join(parts) + suffix
    return None


def _fallback_or_context_name(
    value: int,
    suffix: str,
    cls: Optional[ast.ClassDef],
    func: Optional[ast.FunctionDef | ast.AsyncFunctionDef],
) -> str:
    """Use class/function context with suffix, or fall back to a generic name."""
    parts = []
    if cls:
        parts.append(to_snake_upper(cls.name))
    if func:
        parts.append(to_snake_upper(func.name))
    if parts:
        return "_".join(parts) + suffix
    return fallback_name(value)


def derive_semantic_name(
    value: int,
    node_stack: list[ast.AST],
    assignment: Optional[ast.Assign | ast.AnnAssign],
    line_text: str,
) -> str:
    """Return a business-meaningful constant name without collision resolution."""
    suffix = unit_suffix(value, line_text, node_stack)
    func = enclosing(node_stack, ast.FunctionDef, ast.AsyncFunctionDef)
    cls = enclosing(node_stack, ast.ClassDef)
    func_name = func.name if func else None

    name: Optional[str] = None

    # Special case: standard duration buckets in duration-related contexts.
    name = _name_from_duration_bucket(value, node_stack, cls, func_name, line_text)

    # 1. Assignment target
    if name is None and assignment:
        name = _name_from_assignment_target(assignment, suffix, cls, func)

    # 2. Kwarg / parameter name
    if name is None:
        name = _name_from_kwarg(line_text, value, suffix)

    # 3. Function/class context + nearby word
    if name is None:
        name = _name_from_context(line_text, suffix, cls, func)

    # 4. Function/class context only, or generic fallback
    if name is None or name.strip("_") == suffix.strip("_"):
        name = _fallback_or_context_name(value, suffix, cls, func)

    return clean_name(name)


def resolve_collision(name: str, existing_names: set[str]) -> str:
    if name not in existing_names:
        return name
    for i in range(2, 1000):
        candidate = f"{name}_{i}"
        if candidate not in existing_names:
            return candidate
    return f"{name}_UNIQUE"


def is_in_string_or_comment(src: str, line: int, start_col: int, end_col: int) -> bool:
    lines = src.splitlines(keepends=True)
    if line < 1 or line > len(lines):
        return False
    text = lines[line - 1]
    hash_pos = text.find("#")
    if hash_pos != -1 and start_col >= hash_pos:
        return True

    try:
        for tok in tokenize.generate_tokens(iter(lines).__next__):
            if tok.type == tokenize.STRING and tok.start[0] <= line <= tok.end[0]:
                if tok.start[0] == tok.end[0]:
                    if tok.start[1] <= start_col and end_col <= tok.end[1]:
                        return True
                elif tok.start[0] == line and start_col >= tok.start[1]:
                    return True
                elif tok.end[0] == line and end_col <= tok.end[1]:
                    return True
                elif tok.start[0] < line < tok.end[0]:
                    return True
    except tokenize.TokenError:
        pass
    return False


def is_literal_type_arg(node_stack: list[ast.AST]) -> bool:
    for node in node_stack:
        if isinstance(node, ast.Subscript):
            value = node.value
            if isinstance(value, ast.Attribute) and value.attr == "Literal":
                return True
            if isinstance(value, ast.Name) and value.id == "Literal":
                return True
    return False


def is_reexport_line(file_path: Path, line_text: str) -> bool:
    if file_path.name != "__init__.py":
        return False
    stripped = line_text.strip()
    return stripped.startswith("__all__") or ("import" in stripped and "as" in stripped)


def collect_existing_names(tree: ast.AST) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
    return names


def plan_file(file_path: Path, occurrences: set[tuple[int, str]]) -> FilePlan:
    plan = FilePlan()
    src = file_path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        print(f"[WARN] Syntax error in {file_path}: {e}", file=sys.stderr)
        return plan

    existing_names = collect_existing_names(tree)
    lines = src.splitlines(keepends=True)
    tokens = find_number_tokens(src)

    line_values: dict[int, set[str]] = defaultdict(set)
    for ln, val in occurrences:
        line_values[ln].add(val)

    # Map (name, value) -> Constant for reuse when identical context/name is encountered.
    name_value_to_constant: dict[tuple[str, int], Constant] = {}

    for tok_line, start_col, end_col, val_str in tokens:
        if val_str not in line_values.get(tok_line, set()):
            continue
        value = int(val_str)

        if is_in_string_or_comment(src, tok_line, start_col, end_col):
            continue

        line_text = lines[tok_line - 1].rstrip("\n")

        if is_reexport_line(file_path, line_text):
            continue

        stack = get_stack_for_position(tree, tok_line, start_col)
        assignment = enclosing_assignment(stack)

        if is_literal_type_arg(stack):
            continue

        semantic_name = derive_semantic_name(value, stack, assignment, line_text)

        # Reuse an existing constant if the same name/value pair already exists.
        key = (semantic_name, value)
        if key in name_value_to_constant:
            const = name_value_to_constant[key]
            const_name = const.name
        else:
            # Only resolve collisions when the semantic name is used by a DIFFERENT value.
            const_name = resolve_collision(semantic_name, existing_names)
            const = Constant(
                name=const_name, value=value, value_str=val_str, reason=line_text.strip()
            )
            name_value_to_constant[key] = const
            plan.constants.append(const)
            existing_names.add(const_name)

        plan.replacements.append(
            Replacement(
                line=tok_line,
                start_col=start_col,
                end_col=end_col,
                old=val_str,
                new=const.name,
            )
        )

    return plan


def find_top_level_insertion_line(tree: ast.AST) -> int:
    """Return the line number after which module-level constants should be inserted."""
    last_import = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last_import = max(last_import, getattr(node, "end_lineno", node.lineno))

    if last_import:
        return last_import

    # No imports; insert after module docstring if present.
    if (
        tree.body
        and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
    ):
        return tree.body[0].end_lineno

    return 0


def insert_constants(src: str, constants: list[Constant], tree: ast.AST) -> tuple[str, int]:
    if not constants:
        return src, 0

    lines = src.splitlines(keepends=True)
    insert_after = find_top_level_insertion_line(tree)

    const_lines = ["\n", "# Constants extracted from magic numbers\n"]
    for const in constants:
        const_lines.append(f"{const.name} = {const.value}\n")

    new_lines = lines[:insert_after] + const_lines + lines[insert_after:]
    return "".join(new_lines), len(const_lines)


def apply_replacements(
    src: str, replacements: list[Replacement], inserted_lines: int, insertion_line: int
) -> str:
    if not replacements:
        return src

    lines = src.splitlines(keepends=True)
    by_line: dict[int, list[Replacement]] = defaultdict(list)
    for r in replacements:
        # Adjust line numbers for lines after constant insertion.
        adjusted_line = r.line + (inserted_lines if r.line > insertion_line else 0)
        by_line[adjusted_line].append(r)

    for line_idx, reps in by_line.items():
        reps.sort(key=lambda r: r.start_col, reverse=True)
        line = lines[line_idx - 1]
        for r in reps:
            line = line[: r.start_col] + r.new + line[r.end_col :]
        lines[line_idx - 1] = line

    return "".join(lines)


def refactor_file(
    file_path: Path, occurrences: set[tuple[int, str]], dry_run: bool, verbose: bool = False
) -> tuple[int, int]:
    plan = plan_file(file_path, occurrences)
    if not plan.replacements:
        return 0, 0

    src = file_path.read_text(encoding="utf-8")
    tree = ast.parse(src)

    new_src, inserted_count = insert_constants(src, plan.constants, tree)
    insertion_line = find_top_level_insertion_line(tree)
    new_src = apply_replacements(new_src, plan.replacements, inserted_count, insertion_line)

    # Validate that the refactored file is syntactically valid.
    try:
        ast.parse(new_src)
    except SyntaxError as e:
        print(f"[WARN] Refactoring produced syntax error in {file_path}: {e}", file=sys.stderr)
        return 0, 0

    if verbose:
        print(f"\n--- {file_path.relative_to(PROJECT_DIR)} ---")
        diff = difflib.unified_diff(
            src.splitlines(keepends=True),
            new_src.splitlines(keepends=True),
            fromfile=str(file_path),
            tofile=str(file_path),
        )
        print("".join(diff))

    if not dry_run:
        file_path.write_text(new_src, encoding="utf-8")

    return len(plan.constants), len(plan.replacements)


def run_py_compile(paths: list[Path]) -> bool:
    ok = True
    for p in paths:
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", str(p)], capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"[ERROR] py_compile failed for {p}:\n{result.stderr}", file=sys.stderr)
            ok = False
    return ok


def run_tests() -> int:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/unit", "tests/integration", "-q", "--tb=short"],
        cwd=PROJECT_DIR,
    )
    return result.returncode


def run_audit(audit_tool_path: Path, audit_data_path: Path) -> dict:
    result = subprocess.run(
        [sys.executable, str(audit_tool_path)],
        cwd=PROJECT_DIR,
        capture_output=True,
        text=True,
    )
    print(result.stdout)
    if result.returncode != 0:
        print(f"[ERROR] audit tool failed:\n{result.stderr}", file=sys.stderr)
    data = json.loads(audit_data_path.read_text(encoding="utf-8"))
    total = 0
    for item in data.get("files", []):
        for fn in item.get("functions", []):
            if fn.get("file", "").startswith(CORE_RELATIVE_PREFIX):
                total += len(fn.get("hardcoded_numbers", []))
    return {"total_core_magic_numbers": total}


def main():
    parser = argparse.ArgumentParser(
        description=f"Refactor magic numbers in {CORE_RELATIVE_PREFIX}"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show planned changes without writing files."
    )
    parser.add_argument("--apply", action="store_true", help="Apply changes to files.")
    parser.add_argument("--run-tests", action="store_true", help="Run tests after applying.")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print diffs in dry-run mode.")
    parser.add_argument(
        "--audit-data",
        type=Path,
        required=True,
        help="Path to audit_data.json generated by the magic-number audit.",
    )
    parser.add_argument(
        "--audit-tool",
        type=Path,
        help="Optional audit tool to run after --apply.",
    )
    args = parser.parse_args()

    if not args.dry_run and not args.apply:
        parser.print_help()
        return

    audit_data_path = args.audit_data.expanduser()
    if not audit_data_path.exists():
        parser.error(f"--audit-data does not exist: {audit_data_path}")

    occurrences_set = load_occurrences(audit_data_path)
    by_file: dict[str, set[tuple[int, str]]] = defaultdict(set)
    for file_path, ln, val in occurrences_set:
        by_file[file_path].add((ln, val))

    changed_files = []
    total_constants = 0
    total_replacements = 0

    for rel_path in sorted(by_file):
        file_path = PROJECT_DIR / rel_path
        if not file_path.exists():
            print(f"[WARN] File not found: {file_path}", file=sys.stderr)
            continue
        const_count, rep_count = refactor_file(
            file_path, by_file[rel_path], dry_run=args.dry_run, verbose=args.verbose
        )
        if rep_count:
            changed_files.append(file_path)
            total_constants += const_count
            total_replacements += rep_count
            print(
                f"{'[DRY-RUN] ' if args.dry_run else ''}{rel_path}: {const_count} constants, {rep_count} replacements"  # noqa: E501
            )

    print(
        f"\nSummary: {len(changed_files)} files, {total_constants} constants, {total_replacements} literal occurrences"  # noqa: E501
    )

    if args.apply:
        print("\nRunning py_compile on changed files...")
        if not run_py_compile(changed_files):
            print("[FAIL] py_compile failed; aborting tests.")
            return

        if args.run_tests:
            print("\nRunning test suite...")
            rc = run_tests()
            if rc != 0:
                print(f"[FAIL] Tests exited with code {rc}")
                return

        if args.audit_tool:
            audit_tool_path = args.audit_tool.expanduser()
            if not audit_tool_path.exists():
                print(f"[WARN] audit tool not found: {audit_tool_path}", file=sys.stderr)
                return
            print("\nRegenerating audit report...")
            audit_result = run_audit(audit_tool_path, audit_data_path)
            print(f"Remaining core magic numbers: {audit_result['total_core_magic_numbers']}")
        else:
            print("\nSkipped audit regeneration; pass --audit-tool to rerun the audit.")


if __name__ == "__main__":
    main()
