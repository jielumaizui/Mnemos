#!/usr/bin/env python3
"""Audit Markdown docs for stale commands, paths, and retired terms."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Iterator, Sequence

try:
    from scripts.audit_document_asset_manifest import (
        discover_reviewed_markdown as _discover_reviewed_markdown,
    )
except ModuleNotFoundError:  # direct `python3 scripts/...` execution
    from audit_document_asset_manifest import (  # type: ignore[no-redef]
        discover_reviewed_markdown as _discover_reviewed_markdown,
    )

REPO_ROOT = Path(__file__).resolve().parents[1]
DESKTOP_SYSTEM_MAP_DIRNAME = "mnemos系统图谱"
CONFIG_EXAMPLE_PATH = REPO_ROOT / "config" / "config.example.json"
SHELL_FENCE_LANGS = {"bash", "sh", "shell", "console", "zsh"}
REPO_PATH_ROOTS = {
    ".github",
    "AGENTS.md",
    "CLAUDE.md",
    "README-en.md",
    "README.md",
    "SECURITY.md",
    "config",
    "core",
    "daemon",
    "docs",
    "integrations",
    "mnemos_cli.py",
    "mnemos_daemon.py",
    "prompts",
    "run_tests.py",
    "scripts",
    "setup.bat",
    "setup.py",
    "setup.sh",
    "tests",
    "verify_installation.py",
}
CONFIG_SET_RE = re.compile(r"\bmnemos\s+config\s+set\s+([A-Za-z0-9_.-]+)")
DYNAMIC_RULE_MESSAGES = {
    "doc_command_missing_path": "Shell command references a repo path that does not exist.",
    "config_key_not_in_example": "mnemos config set example key must exist in config/config.example.json.",
}


@dataclass(frozen=True)
class Rule:
    code: str
    pattern: re.Pattern[str]
    message: str


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    text: str
    message: str


RULES = (
    Rule(
        code="machine_local_path",
        pattern=re.compile(
            r"(?<![\w.-])(?:"
            r"/(?:Users|home)/(?:[A-Za-z0-9._-]+|<[^/>\s]+>)"
            r"|[A-Za-z]:\\+Users\\+(?:[A-Za-z0-9._-]+|<[^\\>\s]+>)"
            r")(?:[^\s`'\"),\]]*)?"
        ),
        message="Markdown docs must not contain machine-local absolute paths.",
    ),
    Rule(
        code="bare_python_scripts",
        pattern=re.compile(r"(?<![\w./-])python\s+scripts/"),
        message="Use python3 or .venv/bin/python for script examples.",
    ),
    Rule(
        code="bare_python_module",
        pattern=re.compile(r"(?<![\w./-])python\s+-m\s+"),
        message="Use python3 -m or .venv/bin/python -m in docs.",
    ),
    Rule(
        code="bare_python_inline",
        pattern=re.compile(r"(?<![\w./-])python\s+-c\s+"),
        message="Use python3 -c or .venv/bin/python -c in docs.",
    ),
    Rule(
        code="legacy_memos_name",
        pattern=re.compile(r"\bMemos\b"),
        message="Docs should describe current Mnemos storage without old system names.",
    ),
    Rule(
        code="legacy_daemon_service_key",
        pattern=re.compile(
            r"\b(?:"
            r"daemon\.(?:services|initial_delays)\.(?:l1_sync|distill_merge|event_bus)"
            r"|persona\.data_sources\.memos\.enabled"
            r"|memos\.token"
            r")\b"
        ),
        message="Retired daemon service keys must not appear in public docs.",
    ),
    Rule(
        code="legacy_removed_module",
        pattern=re.compile(r"\b(?:dark_knowledge|quantum_entanglement)\b"),
        message="Removed module names must not appear in public docs.",
    ),
    Rule(
        code="legacy_removed_mode",
        pattern=re.compile(r"--mode\s+(?:dark|entangle)\b"),
        message="Removed CLI mode names must not appear in public docs.",
    ),
)


def _make_finding(path: Path, line: int, rule: str, text: str) -> Finding:
    message = DYNAMIC_RULE_MESSAGES.get(rule)
    if message is None:
        message = next(item.message for item in RULES if item.code == rule)
    return Finding(
        path=_relative(path),
        line=line,
        rule=rule,
        text=text.strip(),
        message=message,
    )


def discover_desktop_system_map() -> tuple[Path, ...]:
    path = Path.home() / "Desktop" / DESKTOP_SYSTEM_MAP_DIRNAME
    if path.exists():
        return (path,)
    return ()


def discover_reviewed_repo_markdown() -> list[Path]:
    return _discover_reviewed_markdown(REPO_ROOT)


def default_paths(include_desktop_system_map: bool = True) -> list[Path]:
    paths = discover_reviewed_repo_markdown()
    if include_desktop_system_map:
        paths.extend(discover_desktop_system_map())
    return paths


def resolve_scan_paths(
    positional_paths: Sequence[Path],
    option_paths: Sequence[Path] | None,
    external_paths: Sequence[Path],
    *,
    include_desktop_system_map: bool = True,
) -> list[Path]:
    if option_paths:
        paths = list(option_paths)
    elif positional_paths:
        paths = list(positional_paths)
    else:
        paths = default_paths(include_desktop_system_map=include_desktop_system_map)
    paths.extend(external_paths)
    return paths


def iter_markdown_files(paths: Iterable[Path]) -> Iterator[Path]:
    for path in paths:
        if path.is_file() and path.suffix == ".md":
            yield path
        elif path.is_dir():
            yield from sorted(path.rglob("*.md"))


def _relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _iter_shell_fence_lines(text: str) -> Iterator[tuple[int, str]]:
    in_fence = False
    shell_fence = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        fence = re.match(r"^\s*```([A-Za-z0-9_-]*)", line)
        if fence:
            if in_fence:
                in_fence = False
                shell_fence = False
            else:
                in_fence = True
                shell_fence = fence.group(1).lower() in SHELL_FENCE_LANGS
            continue
        if in_fence and shell_fence:
            yield lineno, line


def _split_shell_line(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return []
    if stripped.startswith("$ "):
        stripped = stripped[2:].strip()
    try:
        return shlex.split(stripped, comments=True, posix=True)
    except ValueError:
        return []


def _clean_candidate_token(token: str) -> str | None:
    token = token.strip().strip("'\"")
    token = token.rstrip("\\")
    token = token.rstrip(".,;:")
    if token.startswith("./"):
        token = token[2:]
    if (
        not token
        or token in {".", ".."}
        or token.startswith("-")
        or token.startswith(("~", "$", "<", ">", "{", "|"))
        or "://" in token
        or "*" in token
        or "..." in token
        or token.startswith(("/tmp/", "/path/to/"))  # nosec B108 - docs placeholder filter, not temp file use.
    ):
        return None
    if "=" in token and "/" not in token and not token.endswith((".py", ".md", ".json", ".yaml")):
        return None
    return token.rstrip("/")


def _is_repo_path_token(token: str) -> bool:
    if token in REPO_PATH_ROOTS:
        return True
    first_segment = token.split("/", 1)[0]
    if first_segment in REPO_PATH_ROOTS:
        return True
    return token.endswith((".py", ".md", ".json", ".yaml", ".yml", ".sh", ".bat"))


def _repo_path_exists(token: str) -> bool:
    return (REPO_ROOT / token).exists()


def _audit_shell_fence_commands(path: Path, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for lineno, line in _iter_shell_fence_lines(text):
        for raw_token in _split_shell_line(line):
            token = _clean_candidate_token(raw_token)
            if token is None or not _is_repo_path_token(token):
                continue
            if not _repo_path_exists(token):
                findings.append(
                    _make_finding(
                        path,
                        lineno,
                        "doc_command_missing_path",
                        f"{line.strip()}  [missing: {token}]",
                    )
                )
    return findings


def _flatten_config_keys(value: object, prefix: str = "") -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            current = f"{prefix}.{key}" if prefix else str(key)
            keys.add(current)
            keys.update(_flatten_config_keys(child, current))
    return keys


@lru_cache(maxsize=1)
def _example_config_keys() -> set[str]:
    data = json.loads(CONFIG_EXAMPLE_PATH.read_text(encoding="utf-8"))
    return _flatten_config_keys(data)


def _audit_config_set_examples(path: Path, text: str) -> list[Finding]:
    findings: list[Finding] = []
    config_keys = _example_config_keys()
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in CONFIG_SET_RE.finditer(line):
            key = match.group(1)
            if key not in config_keys:
                findings.append(
                    _make_finding(path, lineno, "config_key_not_in_example", line)
                )
    return findings


def audit_file(path: Path) -> list[Finding]:
    findings: list[Finding] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for lineno, line in enumerate(text.splitlines(), start=1):
        for rule in RULES:
            if rule.pattern.search(line):
                findings.append(_make_finding(path, lineno, rule.code, line))
    findings.extend(_audit_shell_fence_commands(path, text))
    findings.extend(_audit_config_set_examples(path, text))
    return findings


def audit_paths(paths: Iterable[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for path in iter_markdown_files(paths):
        findings.extend(audit_file(path))
    return findings


def build_payload(paths: Iterable[Path], findings: Sequence[Finding]) -> dict[str, object]:
    files = list(iter_markdown_files(paths))
    by_rule: dict[str, int] = {}
    for finding in findings:
        by_rule[finding.rule] = by_rule.get(finding.rule, 0) + 1
    return {
        "ok": not findings,
        "scanned_files": len(files),
        "by_rule": by_rule,
        "rules": [rule.code for rule in RULES] + sorted(DYNAMIC_RULE_MESSAGES),
        "findings": [asdict(finding) for finding in findings],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("positional_paths", nargs="*", type=Path)
    parser.add_argument(
        "--paths",
        nargs="+",
        type=Path,
        help="Explicit docs to scan; overrides default and positional paths.",
    )
    parser.add_argument(
        "--external-path",
        action="append",
        default=[],
        type=Path,
        help="Additional external docs path to scan read-only.",
    )
    parser.add_argument(
        "--no-desktop-system-map",
        action="store_true",
        help="Do not auto-discover ~/Desktop/mnemos系统图谱 when using defaults.",
    )
    parser.add_argument("--strict", action="store_true", help="Exit non-zero on findings.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args(argv)

    paths = resolve_scan_paths(
        args.positional_paths,
        args.paths,
        args.external_path,
        include_desktop_system_map=not args.no_desktop_system_map,
    )
    findings = audit_paths(paths)
    payload = build_payload(paths, findings)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif findings:
        print("Docs freshness audit found issue(s):")
        for finding in findings:
            print(
                f"- {finding.path}:{finding.line} [{finding.rule}] "
                f"{finding.text}\n  {finding.message}"
            )
    else:
        print("Docs freshness audit passed.")
    return 1 if args.strict and findings else 0


if __name__ == "__main__":
    sys.exit(main())
