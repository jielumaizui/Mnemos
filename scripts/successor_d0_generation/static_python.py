"""Private implementation module for successor_d0_generation.static_python."""

from __future__ import annotations

from typing import Any

from typing import Sequence

import ast


def _literal_assignment(
    tree: ast.AST,
    name: str,
    *,
    class_name: str | None = None,
    function_name: str | None = None,
) -> Any:
    scope: ast.AST = tree
    if class_name:
        scope = next(
            (
                node
                for node in getattr(tree, "body", [])
                if isinstance(node, ast.ClassDef) and node.name == class_name
            ),
            tree,
        )
    if function_name:
        scope = next(
            (
                node
                for node in getattr(scope, "body", [])
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == function_name
            ),
            scope,
        )
    for node in ast.walk(scope):
        value: ast.AST | None = None
        targets: Sequence[ast.AST] = ()
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = (node.target,)
            value = node.value
        if value is not None and any(
            isinstance(target, ast.Name) and target.id == name for target in targets
        ):
            try:
                return ast.literal_eval(value)
            except (TypeError, ValueError):
                return None
    return None


def _safe_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    try:
        return ast.unparse(node.args)
    except (AttributeError, ValueError):
        return node.name
