#!/usr/bin/env python3
"""Static audit for delayed (function-local) imports.

Many modules in Mnemos use function-local imports to break circular
dependencies.  This is technical debt: it hides the real dependency graph,
makes code harder to statically analyse, and can mask import-order bugs.

This script scans selected Python files, finds every ``import`` statement that
occurs inside a function or method body (as opposed to module top-level), and
reports any import that is not covered by an explicit waiver.

The intended workflow is:
1. Establish a baseline waiver file listing every existing delayed import and
   the reason it is currently required.
2. Run this audit in CI / health stack.
3. New delayed imports fail the audit until they are either removed or added
   to the waiver with an explicit justification.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCAN_FILES = [
    "core/kia/chronos.py",
    "core/kia/chronos_builtin_steps.py",
]
DEFAULT_WAIVER_FILE = PROJECT_ROOT / "scripts" / "delayed_import_waivers.json"


@dataclass(frozen=True)
class DelayedImport:
    """A single delayed import occurrence."""

    file: str  # repo-relative path
    function: str  # qualified function/method name
    module: str  # imported module name
    line: int
    col: int


def _repo_relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _qualified_name(function_node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Return the qualified function name using enclosing class/function names."""
    parts: List[str] = [function_node.name]
    node: Optional[ast.AST] = function_node
    while node is not None:
        node = getattr(node, "_parent", None)
        if isinstance(node, ast.ClassDef):
            parts.append(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Nested functions: include parent function name too.
            parts.append(node.name)
    return ".".join(reversed(parts))


class _DelayedImportVisitor(ast.NodeVisitor):
    """Collect all import statements that live inside function/method bodies."""

    def __init__(self, file_path: Path, root: Path) -> None:
        self.file_path = file_path
        self.root = root
        self.findings: List[DelayedImport] = []
        self._function_stack: List[ast.FunctionDef | ast.AsyncFunctionDef] = []

    def _record(self, node: ast.Import | ast.ImportFrom) -> None:
        if not self._function_stack:
            return
        modules: Set[str] = set()
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        else:
            if node.module is None:
                return
            modules.add(node.module)
        function = _qualified_name(self._function_stack[-1])
        for module in sorted(modules):
            self.findings.append(
                DelayedImport(
                    file=_repo_relative(self.file_path, self.root),
                    function=function,
                    module=module,
                    line=node.lineno,
                    col=node.col_offset,
                )
            )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._function_stack.append(node)
        self.generic_visit(node)
        self._function_stack.pop()

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        self._record(node)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        self._record(node)
        self.generic_visit(node)


def _attach_parents(tree: ast.AST) -> None:
    """Set ``_parent`` references so we can compute qualified names."""
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            setattr(child, "_parent", node)


def find_delayed_imports(paths: Iterable[Path], root: Path) -> List[DelayedImport]:
    """Scan files and return all delayed imports, sorted and deduplicated."""
    all_findings: List[DelayedImport] = []
    for path in paths:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        _attach_parents(tree)
        visitor = _DelayedImportVisitor(path, root)
        visitor.visit(tree)
        all_findings.extend(visitor.findings)
    # Deduplicate by file/function/module; keep first line occurrence.
    seen: Set[Tuple[str, str, str]] = set()
    unique: List[DelayedImport] = []
    for finding in sorted(all_findings, key=lambda f: (f.file, f.function, f.module, f.line)):
        key = (finding.file, finding.function, finding.module)
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    return unique


@dataclass(frozen=True)
class Waiver:
    file: str
    function: str
    module: str
    reason: str

    def matches(self, finding: DelayedImport) -> bool:
        return (
            fnmatch.fnmatch(finding.file, self.file)
            and fnmatch.fnmatch(finding.function, self.function)
            and fnmatch.fnmatch(finding.module, self.module)
        )

    @classmethod
    def from_dict(cls, data: Dict[str, str]) -> "Waiver":
        reason = data.get("reason", "").strip()
        if not reason:
            raise ValueError(
                "delayed import waiver requires a non-empty reason: "
                f"{data.get('file', '<missing>')} "
                f"{data.get('function', '<missing>')} "
                f"{data.get('module', '<missing>')}"
            )
        return cls(
            file=data["file"],
            function=data["function"],
            module=data["module"],
            reason=reason,
        )


def load_waivers(path: Path) -> List[Waiver]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [Waiver.from_dict(item) for item in data]


def audit(
    scan_paths: Iterable[Path],
    root: Path,
    waivers: Sequence[Waiver],
) -> Tuple[List[DelayedImport], List[DelayedImport]]:
    """Return (waived, unwaived) findings."""
    findings = find_delayed_imports(scan_paths, root)
    waived: List[DelayedImport] = []
    unwaived: List[DelayedImport] = []
    for finding in findings:
        if any(w.matches(finding) for w in waivers):
            waived.append(finding)
        else:
            unwaived.append(finding)
    return waived, unwaived


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit function-local (delayed) imports and enforce a waiver baseline."
    )
    parser.add_argument(
        "--scan-files",
        nargs="+",
        default=None,
        help="Python files to scan (repo-relative). Defaults to the Chronos runtime modules.",
    )
    parser.add_argument(
        "--waiver-file",
        type=Path,
        default=DEFAULT_WAIVER_FILE,
        help=f"Path to waiver JSON. Default: {DEFAULT_WAIVER_FILE}",
    )
    parser.add_argument(
        "--generate-waivers",
        action="store_true",
        help="Print a JSON waiver snippet for current findings instead of auditing.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=PROJECT_ROOT,
        help="Project root used for repo-relative paths.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    root = args.root.resolve()
    scan_files = args.scan_files or DEFAULT_SCAN_FILES
    scan_paths = [(root / f).resolve() for f in scan_files]
    missing = [str(p) for p in scan_paths if not p.exists()]
    if missing:
        print(f"Missing scan files: {missing}", file=sys.stderr)
        return 2

    waivers = load_waivers(args.waiver_file)
    waived, unwaived = audit(scan_paths, root, waivers)

    if args.generate_waivers:
        output = [
            {
                "file": f.file,
                "function": f.function,
                "module": f.module,
                "reason": "TODO: document why this delayed import is required",
            }
            for f in sorted(unwaived + waived, key=lambda x: (x.file, x.function, x.module))
        ]
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0

    print(f"Delayed import audit: {len(waived)} waived, {len(unwaived)} unwaived")
    for finding in unwaived:
        print(
            f"{finding.file}:{finding.line}:{finding.col} "
            f"{finding.function}() imports {finding.module}"
        )
    return 0 if not unwaived else 1


if __name__ == "__main__":
    sys.exit(main())
