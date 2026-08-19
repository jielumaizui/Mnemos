"""Independent AST discovery for retired COG-048 training surfaces."""

from __future__ import annotations

import ast
from pathlib import Path
import re
from typing import Any

from core.utils import read_text_value


RETIRED_CALLS = frozenset(
    {
        "enqueue_training_sample",
        "insert_ground_truth",
        "process_training_queue",
        "save_model",
        "load_model",
        "refresh_bayesian_priors_from_ground_truth",
        "update_from_ground_truth",
        "batch_update",
        "set_neg_likelihood",
        "restore_state",
        "reset_dimension",
        "get_weight_history",
        "refresh_shared_weights",
    }
)

LEGACY_TABLES = frozenset(
    {
        "ground_truth_signals",
        "scorer_training_queue",
        "scorer_feedback_events",
        "scorer_models",
        "bayesian_scorer_state",
        "bayesian_feedback",
        "rule_outcomes",
        "optimize_log",
        "weight_history",
        "rule_weights",
        "layer5_dimension_weights",
    }
)

HISTORICAL_SQL_OWNER_FUNCTIONS = frozenset(
    {
        ("core/scoring/subject_provenance.py", "_delete_object"),
        ("core/cognitive/training_history_migration.py", "_inventory_source"),
    }
)

RETIRED_DEFINITION_OWNERS = frozenset(
    {
        "core/scoring/adaptive_scorer_v2.py",
        "core/scoring/bayesian_scorer.py",
        "core/kia/rule_scorer.py",
    }
)

# These names intentionally remain callable so stale callers fail closed with a
# stable error instead of silently reading or mutating pre-COG-048 state. They
# are active safety boundaries, not compatibility implementations or zombie
# allowances, and their exact negative behavior is part of the static contract.
PERMANENT_FAIL_CLOSED_BOUNDARIES = {
    (
        "core/scoring/adaptive_scorer_v2.py",
        "AdaptiveScorerV2",
        "load_model",
    ): ("_reject_retired_training", "load_model"),
    (
        "core/scoring/adaptive_scorer_v2.py",
        "AdaptiveScorerV2",
        "insert_ground_truth",
    ): ("_reject_retired_training", "insert_ground_truth"),
}
FAIL_CLOSED_HELPER_OWNER = "core/scoring/adaptive_scorer_v2.py"
FAIL_CLOSED_HELPER_NAME = "_reject_retired_training"
FAIL_CLOSED_ERROR_CONSTANT = "LEGACY_TRAINING_ERROR"
FAIL_CLOSED_ERROR_VALUE = "training_admission_receipt_required"


def audit_retired_training_surfaces(repo_root: Path) -> dict[str, Any]:
    """Return production calls and SQL reads/writes outside exact owners."""

    root = Path(repo_root).resolve()
    files = [
        *sorted((root / "core").rglob("*.py")),
        *sorted((root / "daemon").rglob("*.py")),
        root / "mnemos_daemon.py",
    ]
    call_sites: list[str] = []
    sql_sites: list[str] = []
    parse_errors: list[str] = []
    for path in files:
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(read_text_value(path), filename=str(path))
        except (OSError, SyntaxError, UnicodeError) as exc:
            parse_errors.append(f"{relative}:{type(exc).__name__}")
            continue
        parents = _parent_map(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                callee = _call_name(node.func)
                if callee in RETIRED_CALLS and not _is_retired_definition_internal(
                    relative,
                    node,
                    parents,
                ):
                    call_sites.append(f"{relative}:{node.lineno}:{callee}")
                if (
                    callee in {"execute", "executemany", "executescript"}
                    and node.args
                    and not _is_exact_historical_sql_owner(relative, node, parents)
                ):
                    resolved = _resolve_executed_sql(node, tree, parents)
                    if resolved is not None:
                        _append_retired_sql_sites(
                            sql_sites,
                            relative=relative,
                            lineno=node.lineno,
                            sql=resolved,
                        )
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and not _is_exact_historical_sql_owner(relative, node, parents)
            ):
                _append_retired_sql_sites(
                    sql_sites,
                    relative=relative,
                    lineno=node.lineno,
                    sql=node.value,
                )
    return {
        "legacy_call_sites": sorted(set(call_sites)),
        "legacy_sql_sites": sorted(set(sql_sites)),
        "parse_errors": sorted(set(parse_errors)),
        "fail_closed_boundary_gaps": _audit_fail_closed_boundaries(root),
    }


def _audit_fail_closed_boundaries(root: Path) -> list[str]:
    """Require exact, side-effect-free negative behavior at public boundaries."""

    trees: dict[str, ast.Module | None] = {}
    gaps: list[str] = []
    helper_path = root / FAIL_CLOSED_HELPER_OWNER
    try:
        helper_tree = ast.parse(
            read_text_value(helper_path),
            filename=str(helper_path),
        )
    except (OSError, SyntaxError, UnicodeError):
        helper_tree = None
    if helper_tree is None or not _has_exact_fail_closed_helper(helper_tree):
        gaps.append(
            f"{FAIL_CLOSED_HELPER_OWNER}:{FAIL_CLOSED_HELPER_NAME}:" "negative_contract_mismatch"
        )
    trees[FAIL_CLOSED_HELPER_OWNER] = helper_tree

    for boundary, expected in sorted(PERMANENT_FAIL_CLOSED_BOUNDARIES.items()):
        relative, class_name, method_name = boundary
        expected_callee, expected_operation = expected
        if relative not in trees:
            path = root / relative
            try:
                trees[relative] = ast.parse(read_text_value(path), filename=str(path))
            except (OSError, SyntaxError, UnicodeError):
                trees[relative] = None
        tree = trees[relative]
        label = f"{relative}:{class_name}.{method_name}"
        if tree is None:
            gaps.append(label + ":missing_or_unparseable_owner")
            continue
        classes = [
            node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
        ]
        methods = (
            []
            if len(classes) != 1
            else [
                node
                for node in classes[0].body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == method_name
            ]
        )
        if len(methods) != 1 or not _is_exact_fail_closed_method(
            methods[0],
            expected_callee=expected_callee,
            expected_operation=expected_operation,
        ):
            gaps.append(label + ":negative_contract_mismatch")
    return gaps


def _has_exact_fail_closed_helper(tree: ast.Module) -> bool:
    constants = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == FAIL_CLOSED_ERROR_CONSTANT
    ]
    helpers = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == FAIL_CLOSED_HELPER_NAME
    ]
    if (
        len(constants) != 1
        or not isinstance(constants[0].value, ast.Constant)
        or constants[0].value.value != FAIL_CLOSED_ERROR_VALUE
        or len(helpers) != 1
    ):
        return False
    helper = helpers[0]
    if (
        len(helper.args.args) != 1
        or helper.args.args[0].arg != "operation"
        or helper.args.vararg is not None
        or helper.args.kwarg is not None
        or helper.args.kwonlyargs
        or helper.args.defaults
        or helper.args.kw_defaults
        or len(helper.body) != 1
        or not isinstance(helper.body[0], ast.Raise)
    ):
        return False
    error = helper.body[0].exc
    if (
        not isinstance(error, ast.Call)
        or _call_name(error.func) != "PermissionError"
        or len(error.args) != 1
        or error.keywords
        or not isinstance(error.args[0], ast.JoinedStr)
    ):
        return False
    values = error.args[0].values
    return bool(
        len(values) == 3
        and isinstance(values[0], ast.FormattedValue)
        and isinstance(values[0].value, ast.Name)
        and values[0].value.id == FAIL_CLOSED_ERROR_CONSTANT
        and isinstance(values[1], ast.Constant)
        and values[1].value == ":"
        and isinstance(values[2], ast.FormattedValue)
        and isinstance(values[2].value, ast.Name)
        and values[2].value.id == "operation"
    )


def _is_exact_fail_closed_method(
    method: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    expected_callee: str,
    expected_operation: str,
) -> bool:
    statements = list(method.body)
    if (
        statements
        and isinstance(statements[0], ast.Expr)
        and isinstance(statements[0].value, ast.Constant)
        and isinstance(statements[0].value.value, str)
    ):
        statements.pop(0)
    terminal_calls: list[ast.Call] = []
    for statement in statements:
        if isinstance(statement, ast.Delete):
            continue
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            terminal_calls.append(statement.value)
            continue
        return False
    return bool(
        len(terminal_calls) == 1
        and _call_name(terminal_calls[0].func) == expected_callee
        and len(terminal_calls[0].args) == 1
        and not terminal_calls[0].keywords
        and isinstance(terminal_calls[0].args[0], ast.Constant)
        and terminal_calls[0].args[0].value == expected_operation
    )


def _append_retired_sql_sites(
    sites: list[str],
    *,
    relative: str,
    lineno: int,
    sql: str,
) -> None:
    normalized = " ".join(str(sql).lower().split())
    if not any(
        keyword in normalized
        for keyword in (
            "select ",
            "insert ",
            "update ",
            "delete ",
            "create table",
            "alter table",
        )
    ):
        return
    for table in LEGACY_TABLES:
        if re.search(
            rf"(?<![a-z0-9_]){re.escape(table)}(?![a-z0-9_])",
            normalized,
        ):
            sites.append(f"{relative}:{lineno}:{table}")


def _resolve_executed_sql(
    call: ast.Call,
    tree: ast.AST,
    parents: dict[ast.AST, ast.AST],
) -> str | None:
    assignments: dict[str, ast.AST] = {}
    containing_function: ast.AST | None = call
    while containing_function is not None and not isinstance(
        containing_function,
        (ast.FunctionDef, ast.AsyncFunctionDef),
    ):
        containing_function = parents.get(containing_function)
    scopes = [tree]
    if containing_function is not None:
        scopes.append(containing_function)
    for scope in scopes:
        for node in ast.walk(scope):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.lineno >= call.lineno:
                continue
            value = node.value
            targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
            for target in targets:
                if isinstance(target, ast.Name) and value is not None:
                    assignments[target.id] = value
    return _resolve_string_expression(call.args[0], assignments, frozenset())


def _resolve_string_expression(
    expression: ast.AST,
    assignments: dict[str, ast.AST],
    seen: frozenset[str],
) -> str | None:
    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        return expression.value
    if isinstance(expression, ast.Name):
        if expression.id in seen or expression.id not in assignments:
            return None
        return _resolve_string_expression(
            assignments[expression.id],
            assignments,
            seen | {expression.id},
        )
    if isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Add):
        left = _resolve_string_expression(expression.left, assignments, seen)
        right = _resolve_string_expression(expression.right, assignments, seen)
        return None if left is None or right is None else left + right
    if isinstance(expression, ast.JoinedStr):
        parts: list[str] = []
        for value in expression.values:
            target = value.value if isinstance(value, ast.FormattedValue) else value
            resolved = _resolve_string_expression(target, assignments, seen)
            if resolved is None:
                return None
            parts.append(resolved)
        return "".join(parts)
    return None


def _is_exact_historical_sql_owner(
    relative: str,
    node: ast.AST,
    parents: dict[ast.AST, ast.AST],
) -> bool:
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return (relative, current.name) in HISTORICAL_SQL_OWNER_FUNCTIONS
        current = parents.get(current)
    return False


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_retired_definition_internal(
    relative: str,
    node: ast.AST,
    parents: dict[ast.AST, ast.AST],
) -> bool:
    if relative not in RETIRED_DEFINITION_OWNERS:
        return False
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name in RETIRED_CALLS
        current = parents.get(current)
    return False
