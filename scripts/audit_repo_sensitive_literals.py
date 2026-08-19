#!/usr/bin/env python3
"""Audit repository text for sensitive-looking literals."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]

_SK_PREFIX = "sk" + "-"
_ANTHROPIC_PREFIX = _SK_PREFIX + "ant" + "-"
_GOOGLE_PREFIX = "AI" + "za"
_GITHUB_PREFIXES = ("ghp" + "_", "gho" + "_", "ghu" + "_", "ghs" + "_", "ghr" + "_")
_GITHUB_PAT_PREFIX = "github" + "_pat" + "_"
_SLACK_PREFIX = "xox"
_HUGGINGFACE_PREFIX = "hf" + "_"

TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "raw_anthropic_key",
        re.compile(rf"\b{re.escape(_ANTHROPIC_PREFIX)}[A-Za-z0-9_-]{{16,}}\b"),
    ),
    (
        "raw_openai_key",
        re.compile(
            rf"\b{re.escape(_SK_PREFIX)}(?!ant-)[A-Za-z0-9_-]{{16,}}\b"
        ),
    ),
    (
        "raw_google_key",
        re.compile(rf"\b{re.escape(_GOOGLE_PREFIX)}[0-9A-Za-z_-]{{16,}}\b"),
    ),
    (
        "raw_github_token",
        re.compile(
            rf"\b(?:{'|'.join(re.escape(prefix) for prefix in _GITHUB_PREFIXES)})"
            r"[0-9A-Za-z_]{16,}\b"
        ),
    ),
    (
        "raw_github_pat",
        re.compile(rf"\b{re.escape(_GITHUB_PAT_PREFIX)}[0-9A-Za-z_]{{16,}}\b"),
    ),
    (
        "raw_slack_token",
        re.compile(rf"\b{re.escape(_SLACK_PREFIX)}[baprs]-[0-9A-Za-z-]{{8,}}\b"),
    ),
    (
        "raw_huggingface_token",
        re.compile(rf"\b{re.escape(_HUGGINGFACE_PREFIX)}[0-9A-Za-z]{{16,}}\b"),
    ),
)
LOCAL_HOME_RE = re.compile(
    r"(?<![\w.-])(?:"
    r"/(?:Users|home)/(?:[A-Za-z0-9._-]+|<[^/>\s]+>)"
    r"|[A-Za-z]:\\+Users\\+(?:[A-Za-z0-9._-]+|<[^\\>\s]+>)"
    r")(?:[^\s`'\"),\]]*)?"
)
CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?P<field>\b(?:api[_ -]?key|token|secret|password|credential|bearer)\b)"
    r"\s*(?<![=!<>])(?:[:=]|=>)(?![=])\s*"
    r"(?P<value>\"[^\"]*\"|'[^']*'|`[^`]*`|[^\s,;}]+)",
    re.IGNORECASE,
)
ENV_ASSIGNMENT_RE = re.compile(
    r"(?P<field>\b[A-Z][A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)[A-Z0-9_]*\b)"
    r"\s*(?<![=!<>])=(?![=])\s*"
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s,;}]+)"
)

PLACEHOLDER_MARKERS = (
    "...",
    "****",
    "<",
    ">",
    "dummy",
    "example",
    "fake",
    "mock",
    "placeholder",
    "redacted",
    "sentinel",
    "test",
    "env:",
    "keyring:",
    "keyref:",
    "your_",
    "your-",
    "$",
)
NON_SECRET_FIELD_MARKERS = (
    "max_tokens",
    "token_budget",
    "tokenizer",
    "completion_tokens",
    "prompt_tokens",
    "response_tokens",
    "per_message_token",
    "source_event_id",
)
DEFAULT_EXCLUDES = (
    ".git/",
    ".mypy_cache/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".venv/",
    "__pycache__/",
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    text: str
    message: str


RULE_MESSAGES = {
    "credential_field_value": (
        "Credential-like assignments in tracked files must use placeholders, "
        "references, or harmless sentinel values."
    ),
    "local_home_path": "Tracked files must not contain machine-local home paths.",
}


def _relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _message(rule: str) -> str:
    return RULE_MESSAGES.get(rule, "Provider-shaped key literal must be constructed or redacted.")


def _make_finding(path: Path, line: int, rule: str, text: str) -> Finding:
    return Finding(
        path=_relative(path),
        line=line,
        rule=rule,
        text=text.strip(),
        message=_message(rule),
    )


def _repo_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    paths = {
        REPO_ROOT / raw.decode("utf-8")
        for raw in result.stdout.split(b"\0")
        if raw
    }
    return sorted(paths)


def _is_excluded(path: Path, excludes: Sequence[str]) -> bool:
    rel = _relative(path)
    return any(rel == exclude.rstrip("/") or rel.startswith(exclude) for exclude in excludes)


def _read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def _normalize_value(value: str) -> str:
    return value.strip().strip("\"'`").strip()


def _is_placeholder_value(value: str) -> bool:
    normalized = _normalize_value(value)
    if normalized in {"", "''", '""', "``"}:
        return True
    lowered = normalized.lower()
    if any(marker in lowered for marker in PLACEHOLDER_MARKERS):
        return True
    if lowered in {"key", "good", "bad"}:
        return True
    if lowered.endswith(("-key", "-test")):
        return True
    return bool(re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", normalized))


def _is_static_literal_value(value: str) -> bool:
    stripped = value.strip()
    normalized = _normalize_value(stripped)
    if stripped.startswith(("\"", "'", "`")):
        return True
    if normalized in {
        "str",
        "bytes",
        "int",
        "float",
        "bool",
        "None",
        "True",
        "False",
        "Optional[str]",
    }:
        return False
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", normalized):
        if "_" not in normalized and len(normalized) >= 6 and re.search(r"\d", normalized):
            return True
        return False
    if any(marker in stripped for marker in ("(", ")", "[", "]", "{", "}", ".")):
        return False
    return True


def _is_non_secret_field(field: str) -> bool:
    lowered = field.lower()
    return any(marker in lowered for marker in NON_SECRET_FIELD_MARKERS)


def audit_file(path: Path) -> list[Finding]:
    text = _read_text(path)
    if text is None:
        return []
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        token_hit = False
        for rule, pattern in TOKEN_PATTERNS:
            if pattern.search(line):
                findings.append(_make_finding(path, lineno, rule, line))
                token_hit = True
        if LOCAL_HOME_RE.search(line):
            findings.append(_make_finding(path, lineno, "local_home_path", line))
        if token_hit:
            continue
        if stripped.startswith(("#", "def ", "async def ")) or "getpass.getpass" in line:
            continue
        for pattern in (CREDENTIAL_ASSIGNMENT_RE, ENV_ASSIGNMENT_RE):
            for match in pattern.finditer(line):
                field = match.group("field")
                value = match.group("value")
                if (
                    _is_non_secret_field(field)
                    or not _is_static_literal_value(value)
                    or _is_placeholder_value(value)
                ):
                    continue
                findings.append(
                    _make_finding(path, lineno, "credential_field_value", line)
                )
    return findings


def audit_paths(paths: Iterable[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for path in paths:
        findings.extend(audit_file(path))
    return findings


def iter_audit_files(excludes: Sequence[str] = DEFAULT_EXCLUDES) -> Iterator[Path]:
    for path in _repo_files():
        if not _is_excluded(path, excludes):
            yield path


def build_payload(paths: Sequence[Path], findings: Sequence[Finding]) -> dict[str, object]:
    by_rule: dict[str, int] = {}
    for finding in findings:
        by_rule[finding.rule] = by_rule.get(finding.rule, 0) + 1
    return {
        "ok": not findings,
        "scanned_files": len(paths),
        "by_rule": by_rule,
        "rules": sorted(set(RULE_MESSAGES) | {rule for rule, _ in TOKEN_PATTERNS}),
        "findings": [asdict(finding) for finding in findings],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--strict", action="store_true", help="Exit non-zero on findings.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args(argv)

    paths = list(args.paths) if args.paths else list(iter_audit_files())
    findings = audit_paths(paths)
    payload = build_payload(paths, findings)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif findings:
        print("Repo sensitive literal audit found issue(s):")
        for finding in findings:
            print(
                f"- {finding.path}:{finding.line} [{finding.rule}] "
                f"{finding.text}\n  {finding.message}"
            )
    else:
        print("Repo sensitive literal audit passed.")
    return 1 if args.strict and findings else 0


if __name__ == "__main__":
    sys.exit(main())
