#!/usr/bin/env python3
"""Audit production Python code for machine-local or bypassed vault paths."""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGETS = ["core", "integrations", "daemon", "scripts", "mnemos_cli.py", "mnemos_daemon.py"]
EXCLUDED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "htmlcov",
    "node_modules",
}
SKIP_FILES = {"scripts/audit_hardcoded_paths.py"}
DOCUMENTS_SEGMENT = r"Documents"
OBSIDIAN_VAULT_SEGMENT = r"Obsidian Vault"


@dataclass(frozen=True)
class Rule:
    code: str
    pattern: re.Pattern[str]
    message: str


@dataclass(frozen=True)
class AllowlistEntry:
    file: str
    code: re.Pattern[str]
    reason: str


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    rule: str
    text: str
    message: str


RULES = [
    Rule(
        code="machine_absolute_path",
        pattern=re.compile(r"(?<![\w.-])/(?:Users|home)/[A-Za-z0-9._-]+(?:/[^\s'\"),\]]+)?"),
        message="Machine-local absolute paths must not be committed outside fixtures.",
    ),
    Rule(
        code="legacy_obsidian_wiki_default",
        pattern=re.compile(
            rf"(?:{DOCUMENTS_SEGMENT}/{OBSIDIAN_VAULT_SEGMENT}(?:/wiki)?|"
            rf"{DOCUMENTS_SEGMENT}['\"]\s*/\s*['\"]{OBSIDIAN_VAULT_SEGMENT}['\"]"
            r"(?:\s*/\s*['\"]wiki['\"])?|"
            rf"{OBSIDIAN_VAULT_SEGMENT}['\"]\s*/\s*['\"]wiki['\"])"
        ),
        message="Legacy Obsidian wiki defaults must come from get_config().wiki_dir.",
    ),
    Rule(
        code="documents_vault_literal",
        pattern=re.compile(
            r"(?:~[/\\]Documents[/\\](?:raw|mnemos)\b|"
            r"Documents['\"]\s*/\s*['\"](?:raw|mnemos)['\"]|"
            r"Documents[/\\](?:raw|mnemos)\b)"
        ),
        message="Mnemos/raw vault paths must come from Config.vault_dir or an explicit CLI argument.",
    ),
]


ALLOWLIST = [
    AllowlistEntry(
        file="core/config.py",
        code=re.compile(r"Documents['\"]\s*/\s*['\"](?:raw|mnemos)['\"]"),
        reason="Config is the authority for canonical default vault paths.",
    ),
    AllowlistEntry(
        file="core/setup/vault_layout.py",
        code=re.compile(r"Documents['\"]\s*/\s*['\"](?:raw|mnemos)['\"]"),
        reason="Vault layout setup owns first-run default path materialization.",
    ),
    AllowlistEntry(
        file="scripts/auto_setup.py",
        code=re.compile(r"_documents_candidate\(|Obsidian Vault|Obsidian"),
        reason="Setup uses common Obsidian locations only as discovery candidates.",
    ),
]


def iter_python_files(project_root: Path, targets: Sequence[str]) -> Iterable[Path]:
    for target in targets:
        path = project_root / target
        if path.is_file() and path.suffix == ".py":
            yield path
            continue
        if not path.is_dir():
            continue
        for candidate in sorted(path.rglob("*.py")):
            if any(part in EXCLUDED_DIRS for part in candidate.relative_to(project_root).parts):
                continue
            yield candidate


def _is_allowed(rel_path: str, line: str) -> bool:
    for entry in ALLOWLIST:
        if fnmatch.fnmatch(rel_path, entry.file) and entry.code.search(line):
            return True
    return False


def scan_project(project_root: Path, targets: Sequence[str] = DEFAULT_TARGETS) -> list[Finding]:
    findings: list[Finding] = []
    seen: set[Path] = set()
    for path in iter_python_files(project_root, targets):
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        rel_path = path.relative_to(project_root).as_posix()
        if rel_path in SKIP_FILES:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for lineno, line in enumerate(lines, start=1):
            if _is_allowed(rel_path, line):
                continue
            stripped = line.strip()
            for rule in RULES:
                if rule.pattern.search(line):
                    findings.append(
                        Finding(
                            file=rel_path,
                            line=lineno,
                            rule=rule.code,
                            text=stripped,
                            message=rule.message,
                        )
                    )
    return findings


def build_report(findings: Sequence[Finding]) -> dict[str, object]:
    by_rule: dict[str, int] = {}
    for finding in findings:
        by_rule[finding.rule] = by_rule.get(finding.rule, 0) + 1
    return {
        "ok": not findings,
        "finding_count": len(findings),
        "by_rule": by_rule,
        "findings": [asdict(finding) for finding in findings],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when findings exist.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")
    parser.add_argument(
        "--target",
        action="append",
        dest="targets",
        help="Path relative to repo root to scan. May be passed multiple times.",
    )
    args = parser.parse_args(argv)

    targets = args.targets or DEFAULT_TARGETS
    findings = scan_project(PROJECT_ROOT, targets)
    report = build_report(findings)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        if findings:
            print(f"Hardcoded path audit found {len(findings)} issue(s):")
            for finding in findings:
                print(
                    f"- {finding.file}:{finding.line} [{finding.rule}] "
                    f"{finding.text}\n  {finding.message}"
                )
        else:
            print("Hardcoded path audit passed.")
    return 1 if args.strict and findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
