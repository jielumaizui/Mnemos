#!/usr/bin/env python3
"""Static audit for direct configuration reads outside ``core.config.get_config()``.

Scans ``core/``, ``integrations/``, and the top-level entry files for direct
``os.environ`` / ``os.getenv`` / ``open()`` / ``Path.read_text()`` reads and
classifies them.  By default any unclassified read is treated as a configuration
authority violation and causes a non-zero exit.

Classification is driven by a built-in allow-list plus an optional waiver JSON
file.  This lets host-agent protocol env vars, platform discovery, external
credentials, and runtime data IO remain explicit without being flagged as
config debt.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCAN_DIRS = ["core", "integrations"]
DEFAULT_ROOT_FILES = ["mnemos_cli.py", "mnemos_daemon.py"]
DEFAULT_WAIVER_FILE = PROJECT_ROOT / "scripts" / "config_read_waivers.json"


# Built-in allow-list.  Entries are matched in order; the first match wins.
# ``file`` is a fnmatch glob relative to the repo root.
# ``code`` is a regex searched against the source line text.
BUILTIN_ALLOWLIST: List[Dict[str, str]] = [
    {
        "file": "core/runtime_environment.py",
        "code": r"os\.environ",
        "category": "known_internal",
        "reason": "Single process-environment authority",
    },
    {
        "file": "core/ops/hermetic_run.py",
        "code": r"os\.environ|HOME|MNEMOS_RUN_|MNEMOS_DIR|XDG_",
        "category": "runtime_data_io",
        "reason": "Run-scoped environment ownership and formal-state evidence",
    },
    {
        "file": "core/benchmarks/golden.py",
        "code": r"MNEMOS_RUN_ARTIFACTS_DIR",
        "category": "runtime_data_io",
        "reason": "Run-owned benchmark artifact destination",
    },
    {
        "file": "integrations/sources/opencode_source.py",
        "code": r"OPENCODE_DB_PATH",
        "category": "platform_discovery",
        "reason": "Explicit external agent database discovery",
    },
    # ------------------------------------------------------------------
    # core/config.py is the configuration authority itself.
    # ------------------------------------------------------------------
    {
        "file": "core/config.py",
        "code": r"os\.getenv|os\.environ|open\(|\.read_text\(|json\.load|yaml\.safe_load",
        "category": "known_internal",
        "reason": "Config authority loads/overrides its own sources",
    },
    {
        "file": "core/config_persistence.py",
        "code": r"yaml\.safe_load",
        "category": "known_internal",
        "reason": "Config persistence migration authority loads legacy YAML sources",
    },
    # ------------------------------------------------------------------
    # Host-agent protocol environment variables
    # ------------------------------------------------------------------
    {
        "file": "core/cli/commands/*.py",
        "code": (
            r"MNEMOS_HOST_AGENT|MNEMOS_HOOK_EVENT|CLAUDE_HOOK_EVENT|"
            r"KIMI_HOOK_EVENT|USER_MESSAGE|SESSION_MESSAGES"
        ),
        "category": "host_agent_protocol",
        "reason": "Host-agent protocol inputs, not Mnemos configuration",
    },
    {
        "file": "core/cli/doctor_helpers.py",
        "code": r"env_name in os\.environ|MNEMOS_HOST_AGENT|MNEMOS_HOOK_EVENT",
        "category": "host_agent_protocol",
        "reason": "Config-source introspection for doctor / host protocol",
    },
    {
        "file": "core/diagnostics.py",
        "code": r"MNEMOS_HOST_AGENT|MNEMOS_HOOK_EVENT",
        "category": "host_agent_protocol",
        "reason": "Host-agent protocol inputs, not Mnemos configuration",
    },
    {
        "file": "integrations/*",
        "code": (
            r"MNEMOS_HOST_AGENT|MNEMOS_HOOK_EVENT|CLAUDE_HOOK_EVENT|"
            r"KIMI_HOOK_EVENT|USER_MESSAGE|SESSION_MESSAGES"
        ),
        "category": "host_agent_protocol",
        "reason": "Host-agent protocol inputs, not Mnemos configuration",
    },
    {
        "file": "mnemos_cli.py",
        "code": r"MNEMOS_HOST_AGENT|MNEMOS_HOOK_EVENT",
        "category": "host_agent_protocol",
        "reason": "Host-agent protocol inputs, not Mnemos configuration",
    },
    {
        "file": "mnemos_daemon.py",
        "code": r"MNEMOS_HOST_AGENT|MNEMOS_HOOK_EVENT",
        "category": "host_agent_protocol",
        "reason": "Host-agent protocol inputs, not Mnemos configuration",
    },
    # ------------------------------------------------------------------
    # Platform / installation discovery
    # ------------------------------------------------------------------
    {
        "file": "core/*",
        "code": r"HOME|USER|APPDATA|XDG_|Path\.home\(\)|sys\.platform|expanduser",
        "category": "platform_discovery",
        "reason": "Platform-specific default path discovery",
    },
    {
        "file": "core/**/*",
        "code": r"HOME|USER|APPDATA|XDG_|Path\.home\(\)|sys\.platform|expanduser",
        "category": "platform_discovery",
        "reason": "Platform-specific default path discovery",
    },
    {
        "file": "integrations/*",
        "code": r"HOME|USER|APPDATA|XDG_|Path\.home\(\)|sys\.platform|expanduser",
        "category": "platform_discovery",
        "reason": "Platform-specific default path discovery",
    },
    {
        "file": "integrations/**/*",
        "code": r"HOME|USER|APPDATA|XDG_|Path\.home\(\)|sys\.platform|expanduser",
        "category": "platform_discovery",
        "reason": "Platform-specific default path discovery",
    },
    {
        "file": "mnemos_daemon.py",
        "code": r"APPDATA|Path\.home\(\)|sys\.platform|expanduser",
        "category": "platform_discovery",
        "reason": "Platform-specific default path discovery",
    },
    {
        "file": "mnemos_cli.py",
        "code": r"APPDATA|Path\.home\(\)|sys\.platform|expanduser",
        "category": "platform_discovery",
        "reason": "Platform-specific default path discovery",
    },
    {
        "file": "core/sync_framework/registry.py",
        "code": r"os\.environ\.get\(env_var\)|env_var in cfg",
        "category": "platform_discovery",
        "reason": "External agent installation discovery via env vars",
    },
    {
        "file": "core/app/obsidian_opener.py",
        "code": r"SystemRoot",
        "category": "platform_discovery",
        "reason": "Windows shell fallback path discovery",
    },
    {
        "file": "integrations/sources/*.py",
        "code": r"_HOME|AIDER_PROJECT_ROOTS|os\.getenv",
        "category": "platform_discovery",
        "reason": "External agent installation discovery via env vars",
    },
    {
        "file": "integrations/*_adapter.py",
        "code": r"_HOME|_DATA_DIR|os\.getenv",
        "category": "platform_discovery",
        "reason": "External agent active-adapter installation discovery via env vars",
    },
    # ------------------------------------------------------------------
    # External credentials / provider API env vars (not Mnemos config)
    # ------------------------------------------------------------------
    {
        "file": "core/embeddings/siliconflow_client.py",
        "code": r"os\.getenv|os\.environ\.get",
        "category": "external_credentials",
        "reason": "External embedding provider credential lookup",
    },
    {
        "file": "core/llm_config.py",
        "code": r"os\.environ|os\.getenv|OPENAI_|SILICONFLOW_|ANTHROPIC_|API_KEY|BASE_URL|MODEL",
        "category": "external_credentials",
        "reason": "External LLM provider credential / model lookup",
    },
    {
        "file": "mnemos_daemon.py",
        "code": r"API_KEY|BASE_URL|MODEL|os\.environ\.get\(name",
        "category": "external_credentials",
        "reason": "Credential env forwarding for Windows daemon relaunch",
    },
    # ------------------------------------------------------------------
    # Runtime data / document IO and external agent configs
    # ------------------------------------------------------------------
    {
        "file": "core/app/freshness_alert.py",
        "code": r"yaml\.safe_load",
        "category": "runtime_data_io",
        "reason": "Wiki frontmatter / document parsing",
    },
    {
        "file": "core/application/*.py",
        "code": r"yaml\.safe_load",
        "category": "runtime_data_io",
        "reason": "Wiki frontmatter / document parsing",
    },
    {
        "file": "core/cli/commands/doctor.py",
        "code": r"yaml\.safe_load",
        "category": "runtime_data_io",
        "reason": "Runtime config-file frontmatter parsing for doctor",
    },
    {
        "file": "core/cognitive/sources.py",
        "code": r"yaml\.safe_load",
        "category": "runtime_data_io",
        "reason": "Wiki frontmatter / document parsing",
    },
    {
        "file": "core/frontmatter.py",
        "code": r"yaml\.safe_load",
        "category": "runtime_data_io",
        "reason": "Wiki frontmatter / document parsing",
    },
    {
        "file": "core/hephaestus/document_processor.py",
        "code": r"open\(|\.read_text\(|\.read_bytes\(|json\.load",
        "category": "runtime_data_io",
        "reason": "Document ingestion IO",
    },
    {
        "file": "core/hephaestus/prompt_builder.py",
        "code": r"yaml\.safe_load",
        "category": "runtime_data_io",
        "reason": "Wiki frontmatter / document parsing",
    },
    {
        "file": "core/hephaestus/wiki_builder.py",
        "code": r"open\(|\.read_text\(|\.read_bytes\(|json\.load|yaml\.safe_load",
        "category": "runtime_data_io",
        "reason": "Wiki build / log IO",
    },
    {
        "file": "core/kia/assertion_extractor.py",
        "code": r"open\(|\.read_text\(|\.read_bytes\(|json\.load",
        "category": "runtime_data_io",
        "reason": "CLI file IO",
    },
    {
        "file": "core/kia/*.py",
        "code": r"yaml\.safe_load",
        "category": "runtime_data_io",
        "reason": "Wiki frontmatter / document parsing",
    },
    {
        "file": "core/persona/*.py",
        "code": r"yaml\.safe_load",
        "category": "runtime_data_io",
        "reason": "Wiki frontmatter / document parsing",
    },
    {
        "file": "core/reflection/consumers.py",
        "code": r"open\(|\.read_text\(|\.read_bytes\(|json\.load",
        "category": "runtime_data_io",
        "reason": "Reflection log IO",
    },
    {
        "file": "core/scoring/adaptive_scorer_v2.py",
        "code": r"open\(|yaml\.safe_load",
        "category": "runtime_data_io",
        "reason": "Scorer cache / frontmatter IO",
    },
    {
        "file": "core/sync_framework/file_ingestor.py",
        "code": r"open\(|\.read_text\(|\.read_bytes\(|json\.load",
        "category": "runtime_data_io",
        "reason": "Raw file ingestion IO",
    },
    {
        "file": "core/sync_framework/registry.py",
        "code": r"open\(|\.read_text\(|\.read_bytes\(|json\.load",
        "category": "runtime_data_io",
        "reason": "Agent config file IO",
    },
    {
        "file": "core/sync_framework/storage_backend.py",
        "code": r"MNEMOS_STORAGE_BACKEND_PLUGINS|os\.environ",
        "category": "runtime_data_io",
        "reason": "Storage backend plugin discovery override",
    },
    {
        "file": "core/vaults/obsidian_registry.py",
        "code": (
            r"MNEMOS_OBSIDIAN_CONFIG_PATH|os\.environ|os\.getenv|"
            r"read_text\(|write_text\("
        ),
        "category": "runtime_data_io",
        "reason": "Obsidian vault registry IO and test-isolation env override",
    },
    {
        "file": "core/sync_framework/sync_engine.py",
        "code": r"open\(|\.read_text\(|\.read_bytes\(|json\.load",
        "category": "runtime_data_io",
        "reason": "Sync patterns file IO",
    },
    {
        "file": "core/wiki_metrics.py",
        "code": r"yaml\.safe_load",
        "category": "runtime_data_io",
        "reason": "Wiki frontmatter / document parsing",
    },
    {
        "file": "integrations/active_bridge.py",
        "code": r"open\(|json\.loads|os\.environ|os\.getenv",
        "category": "runtime_data_io",
        "reason": "Session JSONL ingestion and host-agent protocol",
    },
    {
        "file": "integrations/active.py",
        "code": (
            r"\.read_text\(|json\.loads|yaml\.safe_load|settings\.json|"
            r"opencode\.json|\.aider/|\.gemini/|\.config/opencode|claude_data_dir"
        ),
        "category": "runtime_data_io",
        "reason": "External agent config / runtime policy files",
    },
    {
        "file": "integrations/mcp_config_security.py",
        "code": r"\.read_text\(",
        "category": "runtime_data_io",
        "reason": "Atomic external MCP host config rotation and rollback",
    },
    {
        "file": "integrations/agora.py",
        "code": r"open\(|read_text\(|yaml\.safe_load|json\.loads|expanduser|prompts/|\.md",
        "category": "runtime_data_io",
        "reason": "MCP runtime document/protocol IO and bundled prompts",
    },
    {
        "file": "integrations/apollon.py",
        "code": r"open\(|read_text\(|read_bytes\(|json\.load|json\.loads",
        "category": "runtime_data_io",
        "reason": "Claude Code settings / session / guard-state IO",
    },
    {
        "file": "integrations/backends/obsidian_backend.py",
        "code": (
            r"open\(|read_text\(|read_bytes\(|json\.loads|"
            r"MNEMOS_OBSIDIAN_CONFIG_PATH|os\.environ|os\.getenv"
        ),
        "category": "runtime_data_io",
        "reason": "Obsidian vault document / index IO and test-isolation env override",
    },
    {
        "file": "integrations/kimi_adapter.py",
        "code": r"open\(|\.read_text\(|\.read_bytes\(|json\.load",
        "category": "runtime_data_io",
        "reason": "Session file ingestion IO",
    },
    {
        "file": "integrations/oracle.py",
        "code": r"yaml\.safe_load|read_text\(|\.md",
        "category": "runtime_data_io",
        "reason": "Wiki frontmatter / document parsing",
    },
    {
        "file": "integrations/sources/*.py",
        "code": r"open\(|read_text\(|read_bytes\(|json\.load|json\.loads",
        "category": "runtime_data_io",
        "reason": "External agent session / corpus ingestion IO",
    },
    {
        "file": "mnemos_daemon.py",
        "code": (
            r"open\(|os\.open|/proc/|os\.devnull|read_text\(|SystemRoot|"
            r"read_bytes\(|json\.load|yaml\.safe_load"
        ),
        "category": "runtime_data_io",
        "reason": "Daemon runtime lock/proc/IO operations",
    },
    {
        "file": "mnemos_cli.py",
        "code": r"open\(|read_text\(|json\.load|yaml\.safe_load",
        "category": "runtime_data_io",
        "reason": "CLI runtime IO",
    },
    {
        "file": "core/*",
        "code": (
            r"json\.load\(f\)|yaml\.safe_load\(f\)|read_text\(encoding=|"
            r"read_bytes\(|open\(agents\.json"
        ),
        "category": "runtime_data_io",
        "reason": "Runtime data / agent config IO",
    },
    # ------------------------------------------------------------------
    # Allowed env overrides already centralised in core.config
    # ------------------------------------------------------------------
    {
        "file": "core/config.py",
        "code": (
            r"L1_STORAGE_API_URL|L1_STORAGE_TOKEN|SILICONFLOW_API_KEY|"
            r"OPENAI_API_KEY|CLAUDE_SETTINGS_JSON|MNEMOS_WIKI_DIR|WIKI_DIR"
        ),
        "category": "known_internal",
        "reason": "Centralised env override mappings",
    },
    {
        "file": "core/migrations/registry.py",
        "code": r"LEGACY_ENV_ALIASES|os\.getenv",
        "category": "known_internal",
        "reason": "Migration registry reports legacy env aliases for upgrade planning",
    },
]


@dataclass
class Finding:
    file: Path
    line: int
    col: int
    snippet: str
    kind: str  # e.g. 'os.environ', 'open', 'Path.read_text'
    category: str = "unclassified"
    reason: str = ""


@dataclass
class ConfigReadAudit:
    findings: List[Finding] = field(default_factory=list)


def _collect_files(root: Path, scan_dirs: List[str], root_files: List[str]) -> List[Path]:
    files: Set[Path] = set()
    for dir_name in scan_dirs:
        base = root / dir_name
        if not base.is_dir():
            continue
        for py_file in base.rglob("*.py"):
            files.add(py_file.resolve())
    for file_name in root_files:
        py_file = root / file_name
        if py_file.is_file():
            files.add(py_file.resolve())
    return sorted(files)


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _match_allowlist(
    rel_path: str, snippet: str, allowlist: Iterable[Dict[str, str]]
) -> Optional[Tuple[str, str]]:
    for entry in allowlist:
        if not fnmatch.fnmatch(rel_path, entry["file"]):
            continue
        if re.search(entry["code"], snippet):
            return entry["category"], entry["reason"]
    return None


def _detect_os_environ_get(func: ast.AST) -> str | None:
    if isinstance(func, ast.Attribute) and func.attr == "get":
        if isinstance(func.value, ast.Attribute) and func.value.attr == "environ":
            if isinstance(func.value.value, ast.Name) and func.value.value.id == "os":
                return "os.environ.get"
    return None


def _detect_os_getenv(func: ast.AST) -> str | None:
    if isinstance(func, ast.Attribute) and func.attr == "getenv":
        if isinstance(func.value, ast.Name) and func.value.id == "os":
            return "os.getenv"
    return None


def _detect_open(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name) and func.id == "open":
        return "open"
    return None


def _detect_path_read(func: ast.AST) -> str | None:
    if isinstance(func, ast.Attribute) and func.attr in ("read_text", "read_bytes"):
        return f"Path.{func.attr}"
    return None


def _detect_library_load(func: ast.AST) -> str | None:
    if isinstance(func, ast.Attribute) and func.attr in ("load", "safe_load"):
        if isinstance(func.value, ast.Name) and func.value.id in ("json", "yaml", "tomllib"):
            return f"{func.value.id}.{func.attr}"
    return None


class _ConfigReadVisitor(ast.NodeVisitor):
    """AST visitor that collects direct configuration reads."""

    def __init__(self, source: str, path: Path, root: Path) -> None:
        self.source = source
        self.path = path
        self.root = root
        self.findings: List[Finding] = []
        self._lines: Optional[List[str]] = None

    def _line(self, lineno: int) -> str:
        if self._lines is None:
            self._lines = self.source.splitlines()
        if 1 <= lineno <= len(self._lines):
            return self._lines[lineno - 1]
        return ""

    def _add(self, node: ast.AST, kind: str) -> None:
        lineno = getattr(node, "lineno", 1) or 1
        col = getattr(node, "col_offset", 0) or 0
        snippet = self._line(lineno).strip()
        self.findings.append(Finding(self.path, lineno, col, snippet, kind))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # os.environ (including os.environ.get handled via Call below)
        if isinstance(node.value, ast.Name) and node.value.id == "os":
            if node.attr in ("environ",):
                self._add(node, "os.environ")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        detectors = (
            _detect_os_environ_get,
            _detect_os_getenv,
            _detect_open,
            _detect_path_read,
            _detect_library_load,
        )
        for detect in detectors:
            kind = detect(node.func)
            if kind:
                self._add(node, kind)
                break
        self.generic_visit(node)


def _load_waivers(path: Path) -> List[Dict[str, str]]:
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


def _merge_allowlist(
    builtin: List[Dict[str, str]], waivers: List[Dict[str, str]]
) -> List[Dict[str, str]]:
    # Waivers prepend so they can override builtin entries.
    merged = list(waivers)
    merged.extend(builtin)
    return merged


def audit(
    root: Path = PROJECT_ROOT,
    scan_dirs: Optional[List[str]] = None,
    root_files: Optional[List[str]] = None,
    waivers: Optional[List[Dict[str, str]]] = None,
) -> ConfigReadAudit:
    scan_dirs = scan_dirs or DEFAULT_SCAN_DIRS
    root_files = root_files or DEFAULT_ROOT_FILES
    allowlist = _merge_allowlist(BUILTIN_ALLOWLIST, waivers or [])

    audit_result = ConfigReadAudit()
    for path in _collect_files(root, scan_dirs, root_files):
        rel = _rel(path, root)
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        visitor = _ConfigReadVisitor(source, path, root)
        visitor.visit(tree)
        for finding in visitor.findings:
            match = _match_allowlist(rel, finding.snippet, allowlist)
            if match:
                finding.category, finding.reason = match
            audit_result.findings.append(finding)
    return audit_result


def _category_counts(findings: List[Finding]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for f in findings:
        counts[f.category] = counts.get(f.category, 0) + 1
    return counts


def render_text(audit_result: ConfigReadAudit) -> str:
    lines: List[str] = []
    lines.append("Direct config read audit")
    lines.append("")
    counts = _category_counts(audit_result.findings)
    for category in sorted(counts):
        lines.append(f"  {category}: {counts[category]}")
    lines.append("")

    unclassified = [f for f in audit_result.findings if f.category == "unclassified"]
    if unclassified:
        lines.append("Unclassified direct reads (treat as config-authority violations):")
        for f in sorted(unclassified, key=lambda x: (str(x.file), x.line)):
            rel = _rel(f.file, PROJECT_ROOT)
            lines.append(f"  {rel}:{f.line}:{f.col + 1}  {f.kind}: {f.snippet}")
    else:
        lines.append("No unclassified direct configuration reads found.")
    return "\n".join(lines) + "\n"


def render_markdown(audit_result: ConfigReadAudit) -> str:
    lines: List[str] = []
    lines.append("# Direct Configuration Read Audit\n")
    lines.append("_Generated by `scripts/audit_config_reads.py`._\n")
    lines.append("## Summary\n")
    counts = _category_counts(audit_result.findings)
    for category in sorted(counts):
        lines.append(f"- **{category}**: {counts[category]}")
    lines.append("")

    unclassified = [f for f in audit_result.findings if f.category == "unclassified"]
    if unclassified:
        lines.append("## Unclassified Reads (config-authority violations)\n")
        for f in sorted(unclassified, key=lambda x: (str(x.file), x.line)):
            rel = _rel(f.file, PROJECT_ROOT)
            lines.append(f"- `{rel}:{f.line}` `{f.kind}` — `{f.snippet}`")
    else:
        lines.append("No unclassified direct configuration reads found.\n")
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit direct configuration reads in core/ and integrations/."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if any unclassified direct config reads are found.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write Markdown report to this path (default: print text to stdout).",
    )
    parser.add_argument(
        "--waiver",
        type=Path,
        default=DEFAULT_WAIVER_FILE,
        help=f"Waiver JSON path (default: {DEFAULT_WAIVER_FILE}).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output findings as JSON instead of text.",
    )
    args = parser.parse_args(argv)

    waivers = _load_waivers(args.waiver)
    result = audit(waivers=waivers)

    if args.json:
        payload = [
            {
                "file": _rel(f.file, PROJECT_ROOT),
                "line": f.line,
                "col": f.col + 1,
                "kind": f.kind,
                "category": f.category,
                "reason": f.reason,
                "snippet": f.snippet,
            }
            for f in result.findings
        ]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    elif args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(render_markdown(result), encoding="utf-8")
        print(f"Wrote config-read audit to {args.output}")
    else:
        print(render_text(result), end="")

    unclassified = [f for f in result.findings if f.category == "unclassified"]
    if args.check and unclassified:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
