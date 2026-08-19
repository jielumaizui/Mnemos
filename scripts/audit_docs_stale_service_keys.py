#!/usr/bin/env python3
"""Fail when public docs present retired daemon service keys as live config."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATHS = (REPO_ROOT / "README.md", REPO_ROOT / "README-en.md", REPO_ROOT / "docs")
RETIRED_DAEMON_SERVICE_KEYS = ("l1_sync", "distill_merge", "event_bus")

DOTTED_SERVICE_KEY = re.compile(
    r"\bdaemon\.services\.(?P<key>l1_sync|distill_merge|event_bus)\b"
)
MAPPING_SERVICE_KEY = re.compile(
    r"^\s*[\"']?(?P<key>l1_sync|distill_merge|event_bus)[\"']?\s*[:=]"
)
FENCE = re.compile(r"^\s*```")
MIGRATION_CONTEXT = re.compile(
    r"legacy|stale|migration|migrate|compat|historical|old|"
    r"旧|迁移|兼容|历史|已移除|清理",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    key: str
    context: str
    text: str


def iter_markdown_files(paths: Iterable[Path]) -> Iterator[Path]:
    for path in paths:
        if path.is_file() and path.suffix == ".md":
            yield path
        elif path.is_dir():
            yield from sorted(path.rglob("*.md"))


def _relative(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _iter_fenced_blocks(text: str) -> Iterator[tuple[int, list[str]]]:
    in_fence = False
    start_line = 0
    lines: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if FENCE.match(line):
            if in_fence:
                yield start_line, lines
                lines = []
                in_fence = False
            else:
                start_line = lineno + 1
                in_fence = True
            continue
        if in_fence:
            lines.append(line)


def _inside_daemon_services(lines: Sequence[str], index: int) -> bool:
    context = "\n".join(lines[max(0, index - 12): index + 1])
    has_daemon = re.search(r"[\"']?daemon[\"']?\s*[:\[]", context)
    has_services = re.search(r"[\"']?services[\"']?\s*[:\]]", context)
    return bool(has_daemon and has_services) or "[daemon.services]" in context


def _find_in_code_block(path: Path, start_line: int, lines: Sequence[str]) -> list[Finding]:
    findings: list[Finding] = []
    for index, line in enumerate(lines):
        lineno = start_line + index
        dotted = DOTTED_SERVICE_KEY.search(line)
        if dotted:
            findings.append(
                Finding(
                    path=_relative(path),
                    line=lineno,
                    key=dotted.group("key"),
                    context="code",
                    text=line.strip(),
                )
            )
            continue

        mapped = MAPPING_SERVICE_KEY.search(line)
        if mapped and _inside_daemon_services(lines, index):
            findings.append(
                Finding(
                    path=_relative(path),
                    line=lineno,
                    key=mapped.group("key"),
                    context="daemon.services block",
                    text=line.strip(),
                )
            )
    return findings


def _find_in_prose(path: Path, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        match = DOTTED_SERVICE_KEY.search(line)
        if match and not MIGRATION_CONTEXT.search(line):
            findings.append(
                Finding(
                    path=_relative(path),
                    line=lineno,
                    key=match.group("key"),
                    context="prose",
                    text=line.strip(),
                )
            )
    return findings


def audit_file(path: Path) -> list[Finding]:
    text = path.read_text(encoding="utf-8")
    findings = _find_in_prose(path, text)
    for start_line, lines in _iter_fenced_blocks(text):
        findings.extend(_find_in_code_block(path, start_line, lines))
    return findings


def audit_paths(paths: Iterable[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for path in iter_markdown_files(paths):
        findings.extend(audit_file(path))
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, default=list(DEFAULT_PATHS))
    parser.add_argument("--json", action="store_true", help="Emit machine-readable output.")
    args = parser.parse_args(argv)

    files = list(iter_markdown_files(args.paths))
    findings = audit_paths(files)
    payload = {
        "ok": not findings,
        "scanned_files": len(files),
        "retired_keys": list(RETIRED_DAEMON_SERVICE_KEYS),
        "findings": [asdict(finding) for finding in findings],
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif findings:
        print("FAIL: retired daemon service keys found in live documentation config")
        for finding in findings:
            print(
                f"  {finding.path}:{finding.line} "
                f"{finding.context} {finding.key}: {finding.text}"
            )
    else:
        print("OK: no retired daemon service keys in live documentation config")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
