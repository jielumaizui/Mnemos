"""Tests for scripts/audit_config_reads.py and T3 config convergence."""

import ast
import textwrap
from pathlib import Path

import pytest

import scripts.audit_config_reads as acr


def _write_files(tmp_path: Path, files: dict) -> None:
    for name, content in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(content), encoding="utf-8")


def test_visitor_collects_os_environ_get():
    source = "x = os.environ.get('MNEMOS_FOO', '1')"
    tree = ast.parse(source)
    visitor = acr._ConfigReadVisitor(source, Path("x.py"), Path("/repo"))
    visitor.visit(tree)
    kinds = {f.kind for f in visitor.findings}
    assert kinds == {"os.environ", "os.environ.get"}


def test_visitor_collects_open_and_path_read_text():
    source = "with open('cfg.yaml') as f: x = Path('f.txt').read_text()"
    tree = ast.parse(source)
    visitor = acr._ConfigReadVisitor(source, Path("x.py"), Path("/repo"))
    visitor.visit(tree)
    kinds = {f.kind for f in visitor.findings}
    assert kinds == {"open", "Path.read_text"}


def test_allowlist_classifies_config_authority():
    rel = "core/config.py"
    snippet = "with open(path) as f: data = json.load(f)"
    match = acr._match_allowlist(rel, snippet, acr.BUILTIN_ALLOWLIST)
    assert match is not None
    assert match[0] == "known_internal"


def test_allowlist_classifies_host_protocol():
    rel = "integrations/apollon.py"
    snippet = "host = os.environ.get('MNEMOS_HOST_AGENT', '')"
    match = acr._match_allowlist(rel, snippet, acr.BUILTIN_ALLOWLIST)
    assert match is not None
    assert match[0] == "host_agent_protocol"


def test_allowlist_classifies_core_obsidian_registry_override():
    rel = "core/vaults/obsidian_registry.py"
    snippet = "override = os.environ.get('MNEMOS_OBSIDIAN_CONFIG_PATH')"
    match = acr._match_allowlist(rel, snippet, acr.BUILTIN_ALLOWLIST)
    assert match is not None
    assert match[0] == "runtime_data_io"


def test_audit_flags_unclassified_config_read(tmp_path: Path):
    files = {
        "core/widget.py": "timeout = os.environ.get('MNEMOS_WIDGET_TIMEOUT', '5')\n",
    }
    _write_files(tmp_path, files)
    result = acr.audit(root=tmp_path, scan_dirs=["core"], root_files=[])
    unclassified = [f for f in result.findings if f.category == "unclassified"]
    assert len(unclassified) == 2  # os.environ + os.environ.get
    assert all("MNEMOS_WIDGET_TIMEOUT" in f.snippet for f in unclassified)


def test_audit_classifies_frontmatter_parsing(tmp_path: Path):
    files = {
        "core/kia/parser.py": "fm = yaml.safe_load(parts[1]) or {}\n",
    }
    _write_files(tmp_path, files)
    result = acr.audit(root=tmp_path, scan_dirs=["core"], root_files=[])
    assert not any(f.category == "unclassified" for f in result.findings)


def test_env_map_contains_converged_flat_env_vars():
    from core.config_registry import CONFIG_REGISTRY

    env_map = CONFIG_REGISTRY.env_overrides
    assert env_map.get("MNEMOS_PREFLIGHT_TIMEOUT_SEC") == "preflight.timeout_sec"
    assert env_map.get("MNEMOS_WIKI_INDEX_CACHE_TTL") == "oracle.index_cache_ttl_seconds"
    assert env_map.get("MNEMOS_OBSIDIAN_CACHE_TTL") == (
        "storage.obsidian.scan_cache_ttl_seconds"
    )
    assert env_map.get("MNEMOS_PUSH_INDEX_CACHE_TTL") == "push.index_cache_ttl_seconds"
    assert env_map.get("MNEMOS_PERSONA_AB_TEST") == "persona.ab_test_enabled"


def test_default_config_contains_converged_keys():
    from core.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["preflight"]["timeout_sec"] == 5
    assert DEFAULT_CONFIG["oracle"]["index_cache_ttl_seconds"] == 60
    assert DEFAULT_CONFIG["push"]["index_cache_ttl_seconds"] == 60
    assert DEFAULT_CONFIG["storage"]["obsidian"]["scan_cache_ttl_seconds"] == 60
    assert DEFAULT_CONFIG["persona"]["ab_test_enabled"] is False
    assert DEFAULT_CONFIG["model_call_ledger"]["daily_cost_cap"] == 50.0


@pytest.mark.parametrize(
    "env_var",
    [
        "MNEMOS_PREFLIGHT_TIMEOUT_SEC",
        "MNEMOS_WIKI_INDEX_CACHE_TTL",
        "MNEMOS_OBSIDIAN_CACHE_TTL",
        "MNEMOS_PUSH_INDEX_CACHE_TTL",
        "MNEMOS_PERSONA_AB_TEST",
    ],
)
def test_converged_env_vars_no_longer_read_directly(env_var: str):
    """Refactored modules must not hard-code the old env var names."""
    target_files = [
        Path("integrations/active.py"),
        Path("integrations/oracle.py"),
        Path("integrations/backends/obsidian_backend.py"),
        Path("core/kia/teiresias.py"),
        Path("core/persona/delphi.py"),
    ]
    for path in target_files:
        source = path.read_text(encoding="utf-8")
        assert env_var not in source, f"{env_var} still referenced in {path}"
