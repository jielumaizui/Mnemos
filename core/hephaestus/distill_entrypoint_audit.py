"""Static ownership audit for the production distillation entrypoint.

The daemon must have one active queue consumer, and that consumer must route
through the typed backend and canonical extraction contract.  This audit is
deliberately structural: a second output-directory collector or a parsed-only
backend call is a release-blocking ownership violation even when tests happen
to pass.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
import stat
from typing import Iterable

from core.ops.durable_io import read_native_bytes


BANNED_WORKER_METHODS = frozenset(
    {
        "collect_completed",
        "_process_completed_file",
        "_parse_distill_output",
        "_validate_distill_output",
        "_move_to_failed",
        "_move_to_inbox",
        "_try_parse_structured_output",
        "_submit_or_write_inbox",
    }
)
BANNED_RUNTIME_NAMES = frozenset(
    {
        "max_collect_per_cycle",
        "submit_or_write_markdown",
    }
)


@dataclass(frozen=True)
class DistillEntrypointAudit:
    """Machine-readable result for the one-owner production invariant."""

    active_owner_count: int
    active_owner_paths: tuple[str, ...]
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "active_owner_count": self.active_owner_count,
            "active_owner_paths": list(self.active_owner_paths),
            "errors": list(self.errors),
        }


def _parse(path: Path) -> ast.Module:
    return ast.parse(read_native_bytes(path).decode("utf-8"), filename=str(path))


def _class(tree: ast.Module, name: str) -> ast.ClassDef | None:
    return next(
        (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name),
        None,
    )


def _method(class_node: ast.ClassDef | None, name: str) -> ast.FunctionDef | None:
    if class_node is None:
        return None
    return next(
        (
            node
            for node in class_node.body
            if isinstance(node, ast.FunctionDef) and node.name == name
        ),
        None,
    )


def _attribute_count(node: ast.AST | None, attribute: str) -> int:
    if node is None:
        return 0
    return sum(
        1
        for child in ast.walk(node)
        if isinstance(child, ast.Attribute) and child.attr == attribute
    )


def _name_call_count(node: ast.AST | None, name: str) -> int:
    if node is None:
        return 0
    return sum(
        1
        for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == name
    )


def _active_python_paths(repo_root: Path) -> Iterable[Path]:
    daemon_root = repo_root / "daemon"
    try:
        daemon_root_is_directory = stat.S_ISDIR(daemon_root.lstat().st_mode)
    except OSError:
        daemon_root_is_directory = False
    if daemon_root_is_directory:
        yield from sorted(daemon_root.rglob("*.py"))
    daemon_entry = repo_root / "mnemos_daemon.py"
    try:
        daemon_entry_is_file = stat.S_ISREG(daemon_entry.lstat().st_mode)
    except OSError:
        daemon_entry_is_file = False
    if daemon_entry_is_file:
        yield daemon_entry


def _active_owner_paths(repo_root: Path, errors: list[str]) -> tuple[str, ...]:
    owners: list[str] = []
    for path in _active_python_paths(repo_root):
        try:
            tree = _parse(path)
        except (OSError, UnicodeError, SyntaxError) as exc:
            errors.append(f"cannot parse active runtime {path.relative_to(repo_root)}: {exc}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr == "process_all":
                owners.append(f"{path.relative_to(repo_root)}:{node.lineno}")
    return tuple(owners)


def audit_distill_entrypoint(repo_root: Path) -> DistillEntrypointAudit:
    """Return release-blocking errors for ownership or protocol drift."""

    root = repo_root.resolve()
    errors: list[str] = []
    owners = _active_owner_paths(root, errors)
    if len(owners) != 1:
        errors.append(
            "active distillation queue owner count must be exactly 1 "
            f"(found {len(owners)})"
        )

    worker_path = root / "core" / "hephaestus_worker.py"
    backend_path = root / "core" / "hephaestus" / "distill_backend.py"
    extractor_path = root / "core" / "hephaestus" / "distillation_extractor.py"
    try:
        worker_tree = _parse(worker_path)
        worker = _class(worker_tree, "HephaestusWorker")
        defined = {
            node.name
            for node in (worker.body if worker is not None else ())
            if isinstance(node, ast.FunctionDef)
        }
        if worker is None:
            errors.append("HephaestusWorker class is missing")
        for name in sorted(BANNED_WORKER_METHODS & defined):
            errors.append(f"legacy external-output worker method is forbidden: {name}")
        worker_source = read_native_bytes(worker_path).decode("utf-8")
        for name in sorted(BANNED_RUNTIME_NAMES):
            if name in worker_source:
                errors.append(f"legacy external-output runtime symbol is forbidden: {name}")

        process_one = _method(worker, "process_one_task")
        sync_owner = _method(worker, "_sync_distill_and_complete")
        future_owner = _method(worker, "_submit_distillation_future")
        engine_owner = _method(worker, "_run_distillation_engine")
        if _attribute_count(process_one, "_sync_distill_and_complete") != 1:
            errors.append("process_one_task must route exactly once to the synchronous owner")
        if _attribute_count(sync_owner, "_submit_distillation_future") != 1:
            errors.append(
                "synchronous owner must route exactly once to the future owner"
            )
        if _attribute_count(future_owner, "_run_distillation_engine") != 1:
            errors.append(
                "future owner must route exactly once to the engine owner"
            )
        if _attribute_count(worker, "_run_distillation_engine") != 1:
            errors.append(
                "engine owner must be reachable through exactly one worker call site"
            )
        if _attribute_count(sync_owner, "write_pages_with_receipt") != 1:
            errors.append("synchronous owner must use exactly one receipt-governed write call")
        if _name_call_count(engine_owner, "DistillationEngine") != 1:
            errors.append("engine owner must construct exactly one DistillationEngine")
        if _attribute_count(engine_owner, "process") != 1:
            errors.append("engine owner must call DistillationEngine.process exactly once")
    except (OSError, UnicodeError, SyntaxError) as exc:
        errors.append(f"cannot audit HephaestusWorker: {exc}")

    try:
        backend_tree = _parse(backend_path)
        backend_call = _method(_class(backend_tree, "LLMBackend"), "call")
        if _attribute_count(backend_call, "call_with_evidence") != 1:
            errors.append("LLMBackend.call must use the typed call_with_evidence port exactly once")
        if _attribute_count(backend_call, "_caller") != 1:
            errors.append("LLMBackend.call must have one explicit caller boundary")
    except (OSError, UnicodeError, SyntaxError) as exc:
        errors.append(f"cannot audit distill backend: {exc}")

    try:
        extractor_tree = _parse(extractor_path)
        assess = _method(_class(extractor_tree, "KnowledgeExtractor"), "_assess_output")
        if _name_call_count(assess, "validate_extraction_output") != 1:
            errors.append("extractor assessment must call the canonical contract exactly once")
    except (OSError, UnicodeError, SyntaxError) as exc:
        errors.append(f"cannot audit knowledge extractor: {exc}")

    return DistillEntrypointAudit(
        active_owner_count=len(owners),
        active_owner_paths=owners,
        errors=tuple(errors),
    )
