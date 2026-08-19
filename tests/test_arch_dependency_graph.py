"""Tests for scripts/arch_dependency_graph.py."""

import ast
from pathlib import Path

import scripts.arch_dependency_graph as adg


def test_module_name_for_path_simple():
    root = Path("/repo")
    assert adg._module_name_for_path(root / "core" / "config.py", root) == "core.config"


def test_module_name_for_path_init():
    root = Path("/repo")
    assert adg._module_name_for_path(root / "core" / "__init__.py", root) == "core"


def test_module_name_for_path_outside_root():
    root = Path("/repo")
    assert adg._module_name_for_path(Path("/other/file.py"), root) is None


def test_resolve_import_candidates_absolute_import():
    node = ast.parse("import a.b, c").body[0]
    edges = adg._resolve_import_candidates("x", node)
    assert [(e.target, e.deferred) for e in edges] == [("a.b", False), ("c", False)]
    assert all(not e.names for e in edges)


def test_resolve_import_candidates_absolute_from():
    node = ast.parse("from a.b import c").body[0]
    edges = adg._resolve_import_candidates("x", node)
    assert len(edges) == 1
    assert edges[0].target == "a.b"
    assert edges[0].names == ["c"]
    assert edges[0].deferred is True


def test_resolve_import_candidates_relative():
    node = ast.parse("from . import x").body[0]
    edges = adg._resolve_import_candidates("core.app.util", node)
    assert [(e.target, e.names, e.deferred) for e in edges] == [("core.app.x", [], True)]

    node2 = ast.parse("from ..foo import bar").body[0]
    edges2 = adg._resolve_import_candidates("core.app.util", node2)
    assert len(edges2) == 1
    assert edges2[0].target == "core.foo"
    assert edges2[0].names == ["bar"]
    assert edges2[0].deferred is True


def test_build_graph_resolves_from_package_submodule(tmp_path: Path):
    files = {
        "core/pkg/__init__.py": "",
        "core/pkg/sub.py": "",
        "core/consumer.py": "from core.pkg import sub\n",
    }
    _write_files(tmp_path, files)
    graph = adg.build_graph(root=tmp_path, scan_dirs=["core"], root_files=[])
    targets = {e.target for e in graph.module_edges}
    assert "core.pkg.sub" in targets


def test_build_graph_falls_back_to_package_when_name_missing(tmp_path: Path):
    files = {
        "core/pkg/__init__.py": "",
        "core/consumer.py": "from core.pkg import missing\n",
    }
    _write_files(tmp_path, files)
    graph = adg.build_graph(root=tmp_path, scan_dirs=["core"], root_files=[])
    targets = {e.target for e in graph.module_edges}
    assert "core.pkg" in targets
    assert "core.pkg.missing" not in targets


def test_is_under_try_import():
    tree = ast.parse("""
try:
    import x
except ImportError:
    pass
import y
""")
    parents: dict = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent

    try_node = tree.body[0]
    import_x = try_node.body[0]
    import_y = tree.body[1]
    assert adg._is_under_try_import(import_x, parents) is True
    assert adg._is_under_try_import(import_y, parents) is False


def _write_files(base: Path, files: dict) -> None:
    for rel, content in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def test_parse_file_captures_top_level_and_deferred(tmp_path: Path):
    files = {
        "core/a.py": "import core.b\n",
        "core/b.py": "\n",
    }
    _write_files(tmp_path, files)
    edges = adg._parse_file(tmp_path / "core" / "a.py", "core.a")
    assert len(edges) == 1
    assert edges[0].target == "core.b"
    assert edges[0].deferred is False


def test_parse_file_deferred_inside_function(tmp_path: Path):
    files = {
        "core/a.py": "def f():\n    import core.b\n",
        "core/b.py": "",
    }
    _write_files(tmp_path, files)
    edges = adg._parse_file(tmp_path / "core" / "a.py", "core.a")
    assert len(edges) == 1
    assert edges[0].deferred is True


def test_parse_file_ignores_try_imports(tmp_path: Path):
    files = {
        "core/a.py": "try:\n    import core.b\nexcept ImportError:\n    pass\n",
        "core/b.py": "",
    }
    _write_files(tmp_path, files)
    edges = adg._parse_file(tmp_path / "core" / "a.py", "core.a")
    assert edges == []


def test_build_graph_filters_external_modules(tmp_path: Path):
    files = {
        "core/a.py": "import yaml\nimport core.b\n",
        "core/b.py": "",
    }
    _write_files(tmp_path, files)
    graph = adg.build_graph(root=tmp_path, scan_dirs=["core"], root_files=[])
    targets = {e.target for e in graph.module_edges}
    assert targets == {"core.b"}
    assert "yaml" not in targets


def test_package_graph():
    graph = adg.DependencyGraph()
    graph.module_edges = [
        adg.ImportEdge("core.a.x", "core.b.y"),
        adg.ImportEdge("core.a.x", "core.a.z"),  # intra-package edge ignored
        adg.ImportEdge("integrations.foo", "core.c"),
    ]
    nodes, edges = adg.package_graph(graph)
    assert nodes == {"core.a", "core.b", "integrations", "core"}
    assert edges == {("core.a", "core.b"), ("integrations", "core")}


def test_find_forbidden_imports_blocks_core_to_integrations_but_allows_cli():
    graph = adg.DependencyGraph()
    graph.module_edges = [
        adg.ImportEdge("core.diagnostics", "integrations.olympus", deferred=True),
        adg.ImportEdge("core.sync_framework.storage_backend", "integrations.backends", deferred=True),
        adg.ImportEdge("core.cli.commands.mcp", "integrations.agora", deferred=True),
        adg.ImportEdge("integrations.agora", "core.diagnostics", deferred=True),
    ]

    forbidden = adg.find_forbidden_imports(graph)

    assert forbidden == [
        adg.ImportEdge("core.diagnostics", "integrations.olympus", deferred=True),
        adg.ImportEdge(
            "core.sync_framework.storage_backend", "integrations.backends", deferred=True
        ),
    ]


def test_core_cli_helpers_has_no_integration_dependency():
    graph = adg.build_graph()

    assert [
        edge
        for edge in graph.module_edges
        if edge.source == "core.cli.helpers" and edge.target.startswith("integrations")
    ] == []


def test_render_mermaid():
    nodes = {"core.a", "core.b"}
    edges = {("core.a", "core.b")}
    text = adg.render_mermaid(nodes, edges)
    assert text.startswith("```mermaid")
    assert text.endswith("```")
    assert 'core_a["core.a"]' in text
    assert "core_a --> core_b" in text


def test_render_runtime_waiver_includes_owner_target_and_resolution():
    text = "\n".join(
        adg._format_waiver(
            ["a", "b"],
            {
                "reason": "runtime",
                "issue": "arch-debt-a-b-port",
                "runtime_only": True,
                "owner": "platform",
                "target_interface": "core.application.ports.ab",
                "resolution": "Move shared contracts to the port.",
            },
        )
    )

    assert "Owner: platform" in text
    assert "Target Interface: core.application.ports.ab" in text
    assert "Resolution: Move shared contracts to the port." in text


def test_find_cycles_detects_simple_cycle():
    graph = adg.DependencyGraph()
    graph.module_edges = [
        adg.ImportEdge("a", "b"),
        adg.ImportEdge("b", "c"),
        adg.ImportEdge("c", "a"),
    ]
    cycles = adg.find_cycles(graph)
    assert ["a", "b", "c"] in cycles


def test_find_cycles_no_cycle():
    graph = adg.DependencyGraph()
    graph.module_edges = [
        adg.ImportEdge("a", "b"),
        adg.ImportEdge("b", "c"),
    ]
    assert adg.find_cycles(graph) == []


def test_classify_cycles():
    cycles = [["a", "b"], ["c", "d"]]
    waivers = [{"cycle": ["b", "a"], "reason": "known", "issue": "x"}]
    non_waived, waived = adg.classify_cycles(cycles, waivers)
    assert non_waived == [["c", "d"]]
    assert len(waived) == 1
    assert waived[0][0] == ["a", "b"]


def test_runtime_waiver_metadata_gaps_require_owner_target_and_specific_issue():
    waivers = [
        {
            "cycle": ["a", "b"],
            "reason": "runtime only",
            "issue": "T7",
            "runtime_only": True,
        },
        {
            "cycle": ["c", "d"],
            "reason": "runtime only",
            "issue": "arch-debt-c-d-port",
            "runtime_only": True,
            "owner": "architecture",
            "target_interface": "core.application.ports.example",
            "resolution": "Move shared contracts to the port.",
        },
    ]

    gaps = adg.runtime_waiver_metadata_gaps(waivers)

    assert gaps == [
        "a → b: missing owner, target_interface, resolution, specific_issue"
    ]


def test_load_waivers_missing_file(tmp_path: Path):
    assert adg.load_waivers(tmp_path / "missing.json") == []


def test_load_waivers_malformed_file(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text("not json", encoding="utf-8")
    assert adg.load_waivers(path) == []


def test_check_output_passes_when_synced(tmp_path: Path):
    graph = adg.DependencyGraph()
    graph.module_edges = [adg.ImportEdge("a", "b")]
    output = tmp_path / "deps.md"
    content = adg.render_markdown(graph, [], [])
    output.write_text(content, encoding="utf-8")
    assert adg.check_output(graph, output, []) == 0


def test_check_output_fails_on_new_cycle(tmp_path: Path):
    graph = adg.DependencyGraph()
    graph.module_edges = [
        adg.ImportEdge("a", "b"),
        adg.ImportEdge("b", "a"),
    ]
    output = tmp_path / "deps.md"
    # Pre-write a stale doc that has no cycles.
    stale = adg.render_markdown(adg.DependencyGraph(), [], [])
    output.write_text(stale, encoding="utf-8")
    assert adg.check_output(graph, output, []) == 1


def test_check_output_fails_on_forbidden_core_import(tmp_path: Path):
    graph = adg.DependencyGraph()
    graph.module_edges = [
        adg.ImportEdge("core.diagnostics", "integrations.olympus", deferred=True)
    ]
    output = tmp_path / "deps.md"
    output.write_text(adg.render_markdown(graph, [], []), encoding="utf-8")

    assert adg.check_output(graph, output, []) == 1


def test_render_markdown_sections():
    graph = adg.DependencyGraph()
    graph.module_edges = [adg.ImportEdge("core.a", "core.b")]
    content = adg.render_markdown(graph, [], [["core.a", "core.b"]])
    assert "# core/ & integrations/ Dependency Graph" in content
    assert "## Package-level Graph" in content
    assert "## Module-level Cycles" in content
    assert "core.a → core.b → core.a" in content


def test_render_markdown_runtime_only_section():
    graph = adg.DependencyGraph()
    waivers = [
        (["a", "b"], {"reason": "static", "issue": "x"}),
        (
            ["c", "d"],
            {
                "reason": "runtime",
                "issue": "arch-debt-c-d-port",
                "runtime_only": True,
                "owner": "architecture",
                "target_interface": "core.application.ports.cd",
                "resolution": "Move shared contracts to the port.",
            },
        ),
    ]
    content = adg.render_markdown(graph, waivers, [])
    assert "### Waived Cycles" in content
    assert "### Runtime-only Known Cycles" in content
    assert "a → b → a" in content
    assert "c → d → c" in content


def test_build_graph_on_repo_has_no_unwaived_cycles():
    """Integration smoke test against the real repository."""
    graph = adg.build_graph()
    assert len(graph.module_nodes) > 0
    waivers = adg.load_waivers(adg.DEFAULT_WAIVER_FILE)
    cycles = adg.find_cycles(graph)
    non_waived, _ = adg.classify_cycles(cycles, waivers)
    assert non_waived == []


def test_decision_trace_depends_on_persona_challenge_contract_not_consumer():
    graph = adg.build_graph()
    targets = {
        edge.target
        for edge in graph.module_edges
        if edge.source == "core.cognitive.decision_trace_store"
    }

    assert "core.cognitive.persona_challenge_contract" in targets
    assert "core.persona.challenge_queue" not in targets


def test_repo_dependency_doc_is_synced():
    """Real dependency doc must match scripts/arch_dependency_graph.py output."""
    graph = adg.build_graph()
    waivers = adg.load_waivers(adg.DEFAULT_WAIVER_FILE)
    assert adg.check_output(graph, adg.DEFAULT_OUTPUT, waivers) == 0
