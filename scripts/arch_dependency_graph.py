#!/usr/bin/env python3
"""Static module dependency graph for core/ and integrations/.

AST-only: no imports are executed. Uses networkx for cycle detection when
available, otherwise falls back to an internal Tarjan SCC implementation.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

try:
    import networkx as nx
# pragma: no cover - exercised in fallback tests
except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
    nx = None  # type: ignore


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCAN_DIRS = ["core", "integrations"]
DEFAULT_ROOT_FILES = ["mnemos_cli.py", "mnemos_daemon.py"]
DEFAULT_WAIVER_FILE = PROJECT_ROOT / "scripts" / "arch_dependency_waivers.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "docs" / "core-integrations-dependencies.md"
DEFAULT_FORBIDDEN_SOURCE_PREFIXES = ("core",)
DEFAULT_FORBIDDEN_TARGET_PREFIXES = ("integrations",)
DEFAULT_FORBIDDEN_SOURCE_WAIVERS = ("core.cli", "core.application.facade")
RUNTIME_WAIVER_REQUIRED_FIELDS = ("owner", "target_interface", "resolution")


@dataclass(frozen=True)
class ImportEdge:
    source: str
    target: str
    deferred: bool = False


@dataclass
class RawEdge:
    source: str
    target: str
    names: List[str]
    deferred: bool = False


@dataclass
class DependencyGraph:
    module_edges: List[ImportEdge] = field(default_factory=list)
    module_nodes: Set[str] = field(default_factory=set)


def _is_under_try_import(node: ast.AST, parents: Dict[int, ast.AST]) -> bool:
    """Return True if the import lives inside a try/except block."""
    cur_id = id(node)
    while cur_id in parents:
        parent = parents[cur_id]
        if isinstance(parent, ast.Try):
            return True
        cur_id = id(parent)
    return False


def _module_name_for_path(path: Path, root: Path) -> Optional[str]:
    """Map a .py file under root to its dotted module name."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_relative(
    source_module: str, level: int, module: Optional[str], imported_names: List[str]
) -> List[str]:
    """Resolve a relative import to absolute module names."""
    parts = source_module.split(".")
    if level > len(parts):
        return []
    base = parts[:-level] if level > 0 else parts
    targets: List[str] = []
    if module:
        targets.append(".".join(base + [module]))
    else:
        for name in imported_names:
            targets.append(".".join(base + [name]))
    return targets


def _resolve_import_candidates(source_module: str, node: ast.AST) -> List[RawEdge]:
    """Return candidate dependency edges for an Import/ImportFrom node."""
    if isinstance(node, ast.Import):
        return [RawEdge(source_module, alias.name, [], False) for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        module = node.module
        level = node.level or 0
        names = [alias.name for alias in node.names]
        if level > 0:
            targets = _resolve_relative(source_module, level, module, names)
            return [RawEdge(source_module, t, names if module else [], True) for t in targets]
        if module:
            return [RawEdge(source_module, module, names, True)]
    return []


def _parse_file(path: Path, module_name: str) -> List[RawEdge]:
    """Parse a single Python file and return raw candidate dependency edges."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
    except (
        OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError
    ):
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    parents: Dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent

    edges: List[RawEdge] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if _is_under_try_import(node, parents):
            continue
        # Deferred if not at module top level (i.e., nested in function/class).
        deferred = not isinstance(parents.get(id(node)), ast.Module)
        for raw in _resolve_import_candidates(module_name, node):
            raw.deferred = deferred
            edges.append(raw)
    return edges


def _collect_files(root: Path, scan_dirs: List[str], root_files: List[str]) -> Dict[str, Path]:
    """Collect module_name -> path for all files we care about."""
    modules: Dict[str, Path] = {}
    for dir_name in scan_dirs:
        base = root / dir_name
        if not base.is_dir():
            continue
        for py_file in base.rglob("*.py"):
            mod = _module_name_for_path(py_file, root)
            if mod:
                modules[mod] = py_file
    for file_name in root_files:
        py_file = root / file_name
        if py_file.is_file():
            mod = py_file.stem
            modules[mod] = py_file
    return modules


def build_graph(
    root: Path = PROJECT_ROOT,
    scan_dirs: Optional[List[str]] = None,
    root_files: Optional[List[str]] = None,
) -> DependencyGraph:
    """Build a static dependency graph for the requested source tree."""
    scan_dirs = scan_dirs or DEFAULT_SCAN_DIRS
    root_files = root_files or DEFAULT_ROOT_FILES
    modules = _collect_files(root, scan_dirs, root_files)
    graph = DependencyGraph(module_nodes=set(modules))
    for module_name, path in modules.items():
        for raw in _parse_file(path, module_name):
            if not raw.names:
                resolved = {raw.target}
            else:
                resolved = set()
                for name in raw.names:
                    submodule = f"{raw.target}.{name}"
                    resolved.add(submodule if submodule in modules else raw.target)
            for target in resolved:
                if target != module_name and target in modules:
                    graph.module_edges.append(
                        ImportEdge(source=module_name, target=target, deferred=raw.deferred)
                    )
                    graph.module_nodes.add(target)
    return graph


def _package_name(module_name: str) -> str:
    parts = module_name.split(".")
    return parts[0] if len(parts) <= 1 else ".".join(parts[:-1])


def package_graph(graph: DependencyGraph) -> Tuple[Set[str], Set[Tuple[str, str]]]:
    """Aggregate module edges to package-level nodes/edges."""
    nodes: Set[str] = set()
    edges: Set[Tuple[str, str]] = set()
    for edge in graph.module_edges:
        src_pkg = _package_name(edge.source)
        tgt_pkg = _package_name(edge.target)
        nodes.add(src_pkg)
        nodes.add(tgt_pkg)
        if src_pkg != tgt_pkg:
            edges.add((src_pkg, tgt_pkg))
    return nodes, edges


def _module_matches_prefix(module_name: str, prefix: str) -> bool:
    return module_name == prefix or module_name.startswith(prefix + ".")


def find_forbidden_imports(
    graph: DependencyGraph,
    *,
    source_prefixes: Tuple[str, ...] = DEFAULT_FORBIDDEN_SOURCE_PREFIXES,
    target_prefixes: Tuple[str, ...] = DEFAULT_FORBIDDEN_TARGET_PREFIXES,
    allowed_source_prefixes: Tuple[str, ...] = DEFAULT_FORBIDDEN_SOURCE_WAIVERS,
) -> List[ImportEdge]:
    """Return layer-violating imports such as core domain modules importing adapters."""
    forbidden: List[ImportEdge] = []
    for edge in graph.module_edges:
        if not any(_module_matches_prefix(edge.source, prefix) for prefix in source_prefixes):
            continue
        if any(_module_matches_prefix(edge.source, prefix) for prefix in allowed_source_prefixes):
            continue
        if any(_module_matches_prefix(edge.target, prefix) for prefix in target_prefixes):
            forbidden.append(edge)
    return sorted(forbidden, key=lambda e: (e.source, e.target, e.deferred))


def _sanitize_mermaid_id(name: str) -> str:
    """Turn a dotted package/module name into a safe Mermaid node id."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


def render_mermaid(nodes: Set[str], edges: Set[Tuple[str, str]]) -> str:
    """Render a Mermaid directed graph."""
    lines = ["```mermaid", "graph TD;"]
    for node in sorted(nodes):
        node_id = _sanitize_mermaid_id(node)
        lines.append(f'    {node_id}["{node}"]')
    for src, tgt in sorted(edges):
        lines.append(f"    {_sanitize_mermaid_id(src)} --> {_sanitize_mermaid_id(tgt)}")
    lines.append("```")
    return "\n".join(lines)


def find_cycles(graph: DependencyGraph) -> List[List[str]]:
    """Return strongly-connected components of size > 1 as cycles."""
    if nx is None:
        cycles = _find_cycles_tarjan(graph)
    else:
        dg = nx.DiGraph()
        for edge in graph.module_edges:
            dg.add_edge(edge.source, edge.target)
        cycles = []
        for scc in nx.strongly_connected_components(dg):
            if len(scc) > 1:
                cycles.append(sorted(scc))
    # Self-loops are also cycles.
    for src, tgt in {(e.source, e.target) for e in graph.module_edges}:
        if src == tgt:
            cycles.append([src])
    # Deduplicate and deterministic order.
    seen: Set[Tuple[str, ...]] = set()
    unique: List[List[str]] = []
    for c in sorted(cycles, key=lambda x: (len(x), x)):
        key = tuple(c)
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique


def _find_cycles_tarjan(graph: DependencyGraph) -> List[List[str]]:
    """Find strongly-connected components without optional dependencies."""
    adjacency: Dict[str, Set[str]] = {node: set() for node in graph.module_nodes}
    for edge in graph.module_edges:
        adjacency.setdefault(edge.source, set()).add(edge.target)
        adjacency.setdefault(edge.target, set())

    index = 0
    stack: List[str] = []
    on_stack: Set[str] = set()
    indices: Dict[str, int] = {}
    lowlinks: Dict[str, int] = {}
    components: List[List[str]] = []

    def strongconnect(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)

        for target in adjacency.get(node, set()):
            if target not in indices:
                strongconnect(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[target])

        if lowlinks[node] == indices[node]:
            component: List[str] = []
            while stack:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            if len(component) > 1:
                components.append(sorted(component))

    for node in sorted(adjacency):
        if node not in indices:
            strongconnect(node)

    return components


def load_waivers(path: Path) -> List[Dict]:
    """Load waiver definitions from JSON."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
    except (
        OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError
    ):
        return []


def _normalize_cycle(cycle: Iterable[str]) -> Tuple[str, ...]:
    return tuple(sorted(set(cycle)))


def classify_cycles(
    cycles: List[List[str]], waivers: List[Dict]
) -> Tuple[List[List[str]], List[Tuple[List[str], Dict]]]:
    """Split cycles into non-waived and waived."""
    waived_sets = {_normalize_cycle(w.get("cycle", [])): w for w in waivers}
    non_waived: List[List[str]] = []
    waived: List[Tuple[List[str], Dict]] = []
    for cycle in cycles:
        key = _normalize_cycle(cycle)
        if key in waived_sets:
            waived.append((cycle, waived_sets[key]))
        else:
            non_waived.append(cycle)
    return non_waived, waived


def _merge_runtime_waivers(
    classified_waived: List[Tuple[List[str], Dict]], all_waivers: List[Dict]
) -> List[Tuple[List[str], Dict]]:
    """Append runtime-only waivers that were not matched by static cycle detection."""
    seen = {_normalize_cycle(c) for c, _ in classified_waived}
    merged = list(classified_waived)
    for w in all_waivers:
        if w.get("runtime_only"):
            key = _normalize_cycle(w.get("cycle", []))
            if key not in seen:
                seen.add(key)
                merged.append((list(key), w))
    return merged


def _format_waiver(cyc: List[str], w: Dict) -> List[str]:
    lines = [
        f"- `{' → '.join(cyc + [cyc[0]])}`",
        f"  - Reason: {w.get('reason', 'n/a')}",
        f"  - Issue: {w.get('issue', 'n/a')}",
    ]
    if w.get("runtime_only"):
        lines.extend(
            [
                f"  - Owner: {w.get('owner', 'n/a')}",
                f"  - Target Interface: {w.get('target_interface', 'n/a')}",
                f"  - Resolution: {w.get('resolution', 'n/a')}",
            ]
        )
    return lines


def runtime_waiver_metadata_gaps(waivers: List[Dict]) -> List[str]:
    """Return runtime-only waivers that lack explicit ownership or target seams."""
    gaps: List[str] = []
    for waiver in waivers:
        if not waiver.get("runtime_only"):
            continue
        cycle = " → ".join(_normalize_cycle(waiver.get("cycle", [])))
        missing = [
            field for field in RUNTIME_WAIVER_REQUIRED_FIELDS if not waiver.get(field)
        ]
        issue = str(waiver.get("issue", ""))
        if issue in {"T7", "TODOS-2", "T7/TODOS-2"}:
            missing.append("specific_issue")
        if missing:
            gaps.append(f"{cycle}: missing {', '.join(missing)}")
    return gaps


def render_markdown(
    graph: DependencyGraph,
    waived: List[Tuple[List[str], Dict]],
    non_waived: List[List[str]],
) -> str:
    """Render the full dependency documentation."""
    pkg_nodes, pkg_edges = package_graph(graph)
    runtime_waived = [(c, w) for c, w in waived if w.get("runtime_only")]
    static_waived = [(c, w) for c, w in waived if not w.get("runtime_only")]
    lines: List[str] = []
    lines.append("# core/ & integrations/ Dependency Graph\n")
    lines.append("_Generated by `scripts/arch_dependency_graph.py`._\n")
    lines.append("## Package-level Graph\n")
    lines.append(render_mermaid(pkg_nodes, pkg_edges))
    lines.append("")
    lines.append("## Module-level Cycles\n")
    if non_waived:
        lines.append("The following cycles are **not waived** and will fail `--check`:\n")
        for cyc in non_waived:
            lines.append(f"- `{' → '.join(cyc + [cyc[0]])}`")
    else:
        lines.append("No un-waived module-level cycles detected.\n")
    if static_waived:
        lines.append("\n### Waived Cycles\n")
        for cyc, w in static_waived:
            lines.extend(_format_waiver(cyc, w))
    if runtime_waived:
        lines.append(
            "\n### Runtime-only Known Cycles\n\n"
            "These cycles are not detected by static import analysis "
            "(they arise from deferred/attribute access imports) but are recorded here "
            "so they remain visible and are not accidentally hidden by future waivers.\n"
        )
        for cyc, w in runtime_waived:
            lines.extend(_format_waiver(cyc, w))
    lines.append("")
    return "\n".join(lines) + "\n"


def write_output(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def check_output(graph: DependencyGraph, output_path: Path, waivers: List[Dict]) -> int:
    """Regenerate output and compare to on-disk file; also check for new cycles."""
    cycles = find_cycles(graph)
    non_waived, waived = classify_cycles(cycles, waivers)
    waived = _merge_runtime_waivers(waived, waivers)
    forbidden_imports = find_forbidden_imports(graph)
    runtime_gaps = runtime_waiver_metadata_gaps(waivers)
    fresh = render_markdown(graph, waived, non_waived)
    existing = ""
    if output_path.exists():
        existing = output_path.read_text(encoding="utf-8")
    if fresh != existing:
        print(f"Dependency doc is out of sync: {output_path}", file=sys.stderr)
        return 1
    if forbidden_imports:
        print("Forbidden core -> integrations imports detected:", file=sys.stderr)
        for edge in forbidden_imports:
            marker = " [deferred]" if edge.deferred else ""
            print(f"  {edge.source} -> {edge.target}{marker}", file=sys.stderr)
        return 1
    if non_waived:
        print("Un-waived cycles detected:", file=sys.stderr)
        for cyc in non_waived:
            print(f"  {' → '.join(cyc + [cyc[0]])}", file=sys.stderr)
        return 1
    if runtime_gaps:
        print("Runtime-only waiver metadata gaps detected:", file=sys.stderr)
        for gap in runtime_gaps:
            print(f"  {gap}", file=sys.stderr)
        return 1
    print("Dependency graph check passed.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a static dependency graph for core/ and integrations/."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the generated dependency doc is up-to-date and no new cycles exist.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output markdown path (default: {DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "--waiver",
        type=Path,
        default=DEFAULT_WAIVER_FILE,
        help=f"Waiver JSON path (default: {DEFAULT_WAIVER_FILE}).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print parsed edges and detected cycles.",
    )
    args = parser.parse_args(argv)

    graph = build_graph()
    waivers = load_waivers(args.waiver)

    if args.verbose:
        print(f"Modules: {len(graph.module_nodes)}")
        print(f"Edges: {len(graph.module_edges)}")
        for edge in sorted(graph.module_edges, key=lambda e: (e.source, e.target)):
            marker = " [deferred]" if edge.deferred else ""
            print(f"  {edge.source} -> {edge.target}{marker}")
        for cyc in find_cycles(graph):
            print(f"  cycle: {' -> '.join(cyc)}")

    if args.check:
        return check_output(graph, args.output, waivers)

    cycles = find_cycles(graph)
    non_waived, waived = classify_cycles(cycles, waivers)
    waived = _merge_runtime_waivers(waived, waivers)
    content = render_markdown(graph, waived, non_waived)
    write_output(args.output, content)
    print(f"Wrote dependency graph to {args.output}")
    if non_waived:
        print("Warning: un-waived cycles detected:", file=sys.stderr)
        for cyc in non_waived:
            print(f"  {' → '.join(cyc + [cyc[0]])}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
