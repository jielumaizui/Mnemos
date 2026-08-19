#!/usr/bin/env python3
"""Static orphan-module audit for Mnemos.

Scans core/, integrations/, daemon/, scripts/ and marks modules as:
- integrated: imported statically or referenced dynamically by entry points/registry
- entry: CLI commands, scripts, mnemos_cli.py / mnemos_daemon.py
- test_only: only imported by tests
- dead: no production or test references
- island: connected only to other non-entry modules

Prints a Markdown report to stdout by default. Use ``--output`` to write a
file, and add ``--apply`` when writing inside the repository.
"""
from __future__ import annotations

import argparse
import ast
import importlib
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PKG_DIRS = ["core", "integrations", "daemon", "scripts"]
ENTRY_FILES = ["mnemos_cli.py", "mnemos_daemon.py"]

# Modules that are intentionally not statically imported but are active entrypoints
# (module CLIs, eventbus subscribers, dynamic adapters, etc.).
DOCUMENTED_ENTRYPOINTS = {
    "core.cognitive.cli",
    "core.cognitive.observation_index",
    "core.cognitive_graph.updater",
    "integrations.kimi_adapter",
}
DEFAULT_REPORT_PATH = Path("docs/orphan-modules-report.md")


class ModuleIndex:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.modules: Dict[str, Path] = {}
        self._build_index()

    def _build_index(self) -> None:
        for d in PKG_DIRS:
            for path in (self.root / d).rglob("*.py"):
                if "__pycache__" in path.parts:
                    continue
                name = self._module_name(path)
                if name:
                    self.modules[name] = path

    def _module_name(self, path: Path) -> Optional[str]:
        rel = path.resolve().relative_to(self.root)
        parts = rel.parts
        if parts[0] not in PKG_DIRS:
            return None
        if path.name == "__init__.py":
            return ".".join(parts[:-1])
        package = ".".join(parts[:-1])
        return f"{package}.{path.stem}" if package else path.stem

    def is_module(self, name: str) -> bool:
        return name in self.modules


def is_test_file(path: Path) -> bool:
    return (
        "tests" in path.parts
        or path.name.startswith("test_")
        or path.name.endswith("_test.py")
    )


def parse_imports(path: Path) -> List[Tuple[str, str]]:
    """Return list of (imported_module_or_none, kind) for a file.

    kind is 'static' for regular imports or 'dynamic' for string literals that
    look like module paths. None means a from-import of an attribute.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(text)
    except SyntaxError:
        return []

    results: List[Tuple[Any, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                results.append((alias.name, "static"))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                module = node.module or ""
                results.append(((node.level, module), "static"))
            elif node.module:
                results.append((node.module, "static"))
                # submodule imports: from a.b import c -> a.b.c
                for alias in node.names:
                    results.append((f"{node.module}.{alias.name}", "static_submodule"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            results.append((node.value, "dynamic_string"))
    return results


def resolve_relative(index: ModuleIndex, current: str, level: int, module: str) -> str:
    parts = current.split(".")
    path = index.modules.get(current)
    is_package_init = path is not None and path.name == "__init__.py"
    # __init__.py: from . import m -> current.m
    # module file: from . import m -> parent.m
    keep = len(parts) - level + (1 if is_package_init else 0)
    keep = max(keep, 0)
    base = ".".join(parts[:keep])
    return f"{base}.{module}" if base else module


def _import_targets(index: ModuleIndex, raw, importer_name: str) -> Set[str]:
    targets: Set[str] = set()
    if isinstance(raw, tuple):
        level, module = raw
        resolved = resolve_relative(index, importer_name, level, module)
        targets.add(resolved)
        segs = resolved.split(".")
        for i in range(1, len(segs)):
            targets.add(".".join(segs[:i]))
    elif isinstance(raw, str) and raw:
        segs = raw.split(".")
        # Include all existing package prefixes (e.g. from a.b import c loads a and a.b)
        for i in range(len(segs), 0, -1):
            prefix = ".".join(segs[:i])
            if index.is_module(prefix):
                targets.add(prefix)
    return {t for t in targets if index.is_module(t)}


def collect_static_edges(index: ModuleIndex) -> Dict[str, Set[str]]:
    """Map module -> set of modules it imports."""
    edges: Dict[str, Set[str]] = defaultdict(set)
    for name, path in index.modules.items():
        for raw, _kind in parse_imports(path):
            for t in _import_targets(index, raw, name):
                if t != name:
                    edges[name].add(t)
    return edges


def collect_root_importers(index: ModuleIndex) -> Dict[str, Set[str]]:
    """Map module -> set of root entry files that import it."""
    importers: Dict[str, Set[str]] = defaultdict(set)
    for filename in ENTRY_FILES:
        path = index.root / filename
        if not path.exists():
            continue
        for raw, _kind in parse_imports(path):
            for t in _import_targets(index, raw, filename):
                importers[t].add(filename)
    return importers


def collect_dynamic_refs(index: ModuleIndex) -> Dict[str, Set[str]]:
    """Find modules whose dotted names appear as string literals anywhere."""
    refs: Dict[str, Set[str]] = defaultdict(set)
    module_pattern = re.compile(
        r"\b(" + "|".join(re.escape(m) for m in index.modules) + r")\b"
    )
    # Scan likely entry/registry files plus all files for completeness
    scan_roots = [index.root / d for d in PKG_DIRS] + [
        index.root / f for f in ENTRY_FILES
    ]
    seen: Set[Path] = set()
    for root in scan_roots:
        for path in root.rglob("*.py") if root.is_dir() else [root]:
            if path in seen or "__pycache__" in path.parts:
                continue
            seen.add(path)
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for match in module_pattern.finditer(text):
                refs[match.group(1)].add(str(path.relative_to(index.root)))
    return refs


def collect_test_importers(index: ModuleIndex) -> Dict[str, Set[str]]:
    """Map module -> set of test files that import it."""
    importers: Dict[str, Set[str]] = defaultdict(set)
    tests_dir = index.root / "tests"
    if not tests_dir.exists():
        return importers
    for path in tests_dir.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        label = str(path.relative_to(index.root))
        for raw, _kind in parse_imports(path):
            for t in _import_targets(index, raw, label):
                importers[t].add(label)
    return importers


def find_islands(edges: Dict[str, Set[str]], entries: Set[str]) -> List[Set[str]]:
    """Return weakly connected components that contain no entry point."""
    all_nodes = set(edges) | {t for ts in edges.values() for t in ts}
    visited: Set[str] = set()
    islands: List[Set[str]] = []

    def neighbors(node: str) -> Set[str]:
        return edges.get(node, set()) | {n for n, ts in edges.items() if node in ts}

    for node in all_nodes:
        if node in visited:
            continue
        component: Set[str] = set()
        stack = [node]
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            component.add(cur)
            stack.extend(neighbors(cur) - visited)
        if not (component & entries):
            islands.append(component)
    return islands


def build_report(root: Path) -> str:
    index = ModuleIndex(root)
    edges = collect_static_edges(index)
    dynamic_refs = collect_dynamic_refs(index)
    root_importers = collect_root_importers(index)
    test_importers = collect_test_importers(index)

    # entry points
    entry_modules: Set[str] = set()
    for name, path in index.modules.items():
        if path.name == "__init__.py" and path.parent.name in PKG_DIRS:
            # package root is entry-ish
            pass
        if str(path.resolve()) in {str(root / f) for f in ENTRY_FILES}:
            entry_modules.add(name)
        if path.parent.name == "scripts":
            entry_modules.add(name)
        if "core/cli/commands" in str(path):
            entry_modules.add(name)
        if name in {"core.cognitive.cli", "core.cognitive.observation_index"}:
            entry_modules.add(name)

    # modules explicitly documented as active entrypoints
    documented: Set[str] = set(DOCUMENTED_ENTRYPOINTS)

    integrated: Set[str] = set()
    dead: Set[str] = set()
    test_only: Set[str] = set()
    entry_only: Set[str] = set()
    pending: Set[str] = set()

    for name in sorted(index.modules):
        path = index.modules[name]
        static_importers = {n for n, ts in edges.items() if name in ts and not is_test_file(index.modules[n])}
        root_import = bool(root_importers.get(name))
        dynamic = bool(dynamic_refs.get(name))
        test = bool(test_importers.get(name))
        is_entry = name in entry_modules
        is_documented = name in documented

        has_prod_static = (
            static_importers
            and not all(is_test_file(index.modules[n]) for n in static_importers)
        )
        if is_documented or root_import or has_prod_static:
            integrated.add(name)
        elif dynamic and not is_entry:
            integrated.add(name)
        elif is_entry:
            if static_importers or root_import or dynamic or test:
                integrated.add(name)
            else:
                entry_only.add(name)
        elif test:
            test_only.add(name)
        elif static_importers and all(is_test_file(index.modules[n]) for n in static_importers):
            test_only.add(name)
        else:
            pending.add(name)

    # mark pending with no dynamic refs as dead; otherwise integrate
    for name in list(pending):
        if dynamic_refs.get(name) or root_importers.get(name):
            integrated.add(name)
        else:
            dead.add(name)

    # package __init__ modules are pulled in whenever any submodule is imported
    for name in sorted(index.modules):
        if name in integrated:
            continue
        if index.modules[name].name != "__init__.py":
            continue
        for sub in index.modules:
            if sub != name and sub.startswith(name + ".") and sub in integrated:
                integrated.add(name)
                dead.discard(name)
                test_only.discard(name)
                entry_only.discard(name)
                break

    islands = find_islands(edges, entry_modules | integrated)

    lines: List[str] = [
        "# Orphan / Unconnected Module Audit",
        "",
        "Generated by `scripts/audit_orphan_modules.py` on `<repo>`.",
        "",
        "## Summary",
        "",
        f"- Total modules scanned: {len(index.modules)}",
        f"- Integrated (static/dynamic import): {len(integrated)}",
        f"- Legitimate entry points with no other reference: {len(entry_only)}",
        f"- Test-only modules: {len(test_only)}",
        f"- Dead modules (no references): {len(dead)}",
        f"- Disconnected islands (components without entry/integrated node): {len(islands)}",
        "",
        "## Recommended first-pass deletions",
        "",
        "These modules have no static imports, no dynamic string references, and no tests.",
        "",
    ]
    for name in sorted(dead):
        relative_path = index.modules[name].resolve().relative_to(index.root)
        lines.append(f"- `{name}` → `{relative_path}`")
    lines.append("")

    lines.append("## Test-only modules (review for archival)")
    lines.append("")
    for name in sorted(test_only):
        tests = ", ".join(f"`{Path(t).name}`" for t in sorted(test_importers.get(name, set())))
        lines.append(f"- `{name}` — tests: {tests}")
    lines.append("")

    lines.append("## Entry-only modules (expected to have no callers)")
    lines.append("")
    for name in sorted(entry_only):
        lines.append(f"- `{name}`")
    lines.append("")

    lines.append("## Documented dynamic entrypoints")
    lines.append("")
    for name in sorted(documented):
        lines.append(f"- `{name}`")
    lines.append("")

    lines.append("## Disconnected islands")
    lines.append("")
    for idx, island in enumerate(sorted(islands, key=lambda s: -len(s)), 1):
        lines.append(f"### Island {idx} ({len(island)} modules)")
        for name in sorted(island):
            lines.append(f"- `{name}`")
        lines.append("")

    lines.append("## Detail by module")
    lines.append("")
    lines.append("| module | status | static importers | dynamic refs | test importers |")
    lines.append("|--------|--------|------------------|--------------|----------------|")
    for name in sorted(index.modules):
        status = (
            "integrated" if name in integrated
            else "entry_only" if name in entry_only
            else "test_only" if name in test_only
            else "dead"
        )
        static_count = len({n for n, ts in edges.items() if name in ts and not is_test_file(index.modules[n])})
        if root_importers.get(name):
            static_count += len(root_importers[name])
        dynamic_count = len(dynamic_refs.get(name, set()))
        test_count = len(test_importers.get(name, set()))
        lines.append(f"| `{name}` | {status} | {static_count} | {dynamic_count} | {test_count} |")

    return "\n".join(lines)


def _resolve_output_path(root: Path, output: str | None) -> Path | None:
    if not output:
        return None
    path = Path(output).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _is_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _check_report(path: Path, report: str) -> int:
    if not path.exists():
        print(f"Report missing: {path}", file=sys.stderr)
        return 1
    existing = path.read_text(encoding="utf-8")
    if existing != report:
        print(f"Report out of date: {path}", file=sys.stderr)
        return 1
    print(f"Report up to date: {path}")
    return 0


def _action_ledger_path(path_arg: str | None) -> Path:
    if path_arg:
        return Path(path_arg).expanduser().resolve()
    config_module = importlib.import_module("core.config")
    get_config = getattr(config_module, "get_config")
    return Path(get_config().database_dir) / "action_ledger.db"


def _record_repo_report_action(root: Path, output: Path, ledger_path: Path) -> str:
    contracts = importlib.import_module("core.system_contracts")
    action_ledger_cls = getattr(contracts, "ActionLedger")
    make_quality_gate_observation = getattr(
        contracts,
        "make_quality_gate_observation",
    )

    rel_output = output.relative_to(root).as_posix()
    ledger = action_ledger_cls(ledger_path, initialize=True)
    return str(
        ledger.record_observation(
            make_quality_gate_observation(
                actor="scripts.audit_orphan_modules",
                target=f"repo_report:{rel_output}",
                evidence_refs=("scripts/audit_orphan_modules.py", rel_output),
                result_status="produced",
                details={
                    "command": "python3 scripts/audit_orphan_modules.py --output "
                    f"{rel_output} --apply",
                    "mode": "explicit_repo_write",
                },
            )
        )
    )


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit orphan/unconnected modules")
    parser.add_argument("--root", default=".", help="project root")
    parser.add_argument(
        "--output",
        default=None,
        help="write report to this path; default prints to stdout without modifying the repo",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "compare the generated report with --output, or docs/orphan-modules-report.md "
            "when --output is omitted; never writes files"
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="required when --output points inside the repository; records an ActionLedger row",
    )
    parser.add_argument(
        "--action-ledger",
        default=None,
        help="override ActionLedger DB path for explicit repo writes",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    report = build_report(root)
    output = _resolve_output_path(root, args.output)

    if args.check:
        check_path = output or (root / DEFAULT_REPORT_PATH).resolve()
        return _check_report(check_path, report)

    if output is None:
        print(report)
        return 0

    repo_write = _is_inside(output, root)
    if repo_write and not args.apply:
        print(
            "Refusing to write inside the repository without --apply. "
            "Use --check for CI or write to /tmp with --output.",
            file=sys.stderr,
        )
        return 2

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")
    if repo_write:
        action_id = _record_repo_report_action(root, output, _action_ledger_path(args.action_ledger))
        print(f"Report written to {output}; action_ledger_ref={action_id}")
    else:
        print(f"Report written to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
