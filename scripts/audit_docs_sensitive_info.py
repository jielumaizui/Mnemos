#!/usr/bin/env python3
"""Audit public Markdown docs for sensitive values and unsafe examples."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence
from urllib.parse import urlparse

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


def discover_reviewed_repo_markdown() -> list[Path]:
    return _discover_reviewed_markdown(REPO_ROOT)


def discover_desktop_system_map() -> list[Path]:
    path = Path.home() / "Desktop" / DESKTOP_SYSTEM_MAP_DIRNAME
    return [path] if path.is_dir() else []


def default_paths() -> list[Path]:
    return discover_reviewed_repo_markdown() + discover_desktop_system_map()


CREDENTIAL_FIELDS = (
    "api_key",
    "api-key",
    "api key",
    "token",
    "secret",
    "password",
    "credential",
    "bearer",
    "key_source",
    "api_key_source",
    "api-key-source",
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
PII_KEYWORDS_CN = (
    "姓名",
    "年龄",
    "住址",
    "地址",
    "电话",
    "手机号",
    "身份证",
    "公司",
    "单位",
    "岗位",
    "部门",
    "工作",
)
PII_KEYWORDS_EN = (
    "name",
    "age",
    "address",
    "phone",
    "email",
    "company",
    "employer",
    "job",
    "workplace",
    "department",
)
PLACEHOLDER_MARKERS = (
    "...",
    "****",
    "<",
    ">",
    "your_",
    "your-",
    "example",
    "placeholder",
    "redacted",
    "env:",
    "keyring:",
    "keyref:",
    "$",
)
ALLOWED_URL_HOSTS = {
    "localhost",
    "127.0.0.1",
    "0.0.0.0",  # nosec B104 - documentation placeholder host, not socket binding.
    "::1",
}
ALLOWED_URL_SUFFIXES = (
    ".example",
    ".example.com",
    ".example.net",
    ".example.org",
    ".example.invalid",
    ".invalid",
)
API_URL_CONTEXT_RE = re.compile(
    r"\b(?:api(?:[_ -]?url|[_ -]?base|[_ -]?host)?|base_url|endpoint|接口地址|API\s*地址|模型端点)\b",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://[^\s`'\"<>)\]}]+")
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
CN_PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
CN_ID_RE = re.compile(r"(?<!\d)\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)")
LOCAL_HOME_RE = re.compile(
    r"(?<![\w.-])(?:"
    r"/(?:Users|home)/(?:[A-Za-z0-9._-]+|<[^/>\s]+>)"
    r"|[A-Za-z]:\\+Users\\+(?:[A-Za-z0-9._-]+|<[^\\>\s]+>)"
    r")(?:[^\s`'\"),\]]*)?"
)
_SK_PREFIX = "sk" + "-"
_ANTHROPIC_PREFIX = _SK_PREFIX + "ant" + "-"
_GOOGLE_PREFIX = "AI" + "za"
_GITHUB_PREFIXES = ("ghp" + "_", "gho" + "_", "ghu" + "_", "ghs" + "_", "ghr" + "_")
_GITHUB_PAT_PREFIX = "github" + "_pat" + "_"
_SLACK_PREFIX = "xox"
_HUGGINGFACE_PREFIX = "hf" + "_"
TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("raw_anthropic_key", re.compile(rf"\b{re.escape(_ANTHROPIC_PREFIX)}[A-Za-z0-9_-]{{16,}}\b")),
    ("raw_openai_key", re.compile(rf"\b{re.escape(_SK_PREFIX)}(?!ant-)[A-Za-z0-9_-]{{16,}}\b")),
    ("raw_google_key", re.compile(rf"\b{re.escape(_GOOGLE_PREFIX)}[0-9A-Za-z_-]{{16,}}\b")),
    (
        "raw_github_token",
        re.compile(
            rf"\b(?:{'|'.join(re.escape(prefix) for prefix in _GITHUB_PREFIXES)})"
            r"[0-9A-Za-z_]{16,}\b"
        ),
    ),
    ("raw_github_pat", re.compile(rf"\b{re.escape(_GITHUB_PAT_PREFIX)}[0-9A-Za-z_]{{16,}}\b")),
    ("raw_slack_token", re.compile(rf"\b{re.escape(_SLACK_PREFIX)}[baprs]-[0-9A-Za-z-]{{8,}}\b")),
    ("raw_huggingface_token", re.compile(rf"\b{re.escape(_HUGGINGFACE_PREFIX)}[0-9A-Za-z]{{16,}}\b")),
    (
        "jwt_like",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    ),
)
CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?P<field>\b(?:api[_ -]?key|token|secret|password|credential|bearer|key[_ -]?source|api[_ -]?key[_ -]?source)\b)"
    r"\s*(?:[:=]|=>)\s*"
    r"(?P<value>\"[^\"]*\"|'[^']*'|`[^`]*`|[^\s,;}]+)",
    re.IGNORECASE,
)
ENV_ASSIGNMENT_RE = re.compile(
    r"(?P<field>\b[A-Z][A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|KEY_SOURCE)[A-Z0-9_]*\b)"
    r"\s*=\s*"
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s,;}]+)"
)
BEARER_RE = re.compile(r"\bBearer\s+(?P<value>[A-Za-z0-9._~+/=-]{8,})\b", re.IGNORECASE)
PII_CN_ASSIGNMENT_RE = re.compile(
    rf"(?P<field>{'|'.join(map(re.escape, PII_KEYWORDS_CN))})\s*[：:=]\s*(?P<value>\S+)"
)
PII_EN_ASSIGNMENT_RE = re.compile(
    rf"\b(?P<field>{'|'.join(PII_KEYWORDS_EN)})\b\s*[:=]\s*(?P<value>\S+)",
    re.IGNORECASE,
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
        "Credential-like field examples must use empty values or "
        "env:/keyring:/keyref:/**** placeholders."
    ),
    "email_address": "Public docs must not contain real personal or company email addresses.",
    "local_home_path": "Public docs must not contain machine-local absolute paths.",
    "pii_assignment": "Personal or workplace data examples must be generalized or removed.",
    "phone_cn": "Public docs must not contain real mainland China phone numbers.",
    "id_cn": "Public docs must not contain real mainland China ID numbers.",
    "real_api_url": "API endpoint examples must use a placeholder host such as https://api.example.invalid/v1.",
}


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


def _message(rule: str) -> str:
    return RULE_MESSAGES.get(rule, "Sensitive value pattern must be redacted.")


def _make_finding(path: Path, line: int, rule: str, text: str) -> Finding:
    return Finding(
        path=_relative(path),
        line=line,
        rule=rule,
        text=text.strip(),
        message=_message(rule),
    )


def _strip_inline_code_spans(line: str) -> str:
    return re.sub(r"`[^`]*`", " ", line)


def _normalize_value(value: str) -> str:
    return value.strip().strip("\"'`").strip()


def _is_placeholder_value(value: str) -> bool:
    normalized = _normalize_value(value)
    if normalized in {"", "''", '""', "``"}:
        return True
    if normalized.startswith("某"):
        return True
    lowered = normalized.lower()
    if any(marker in lowered for marker in PLACEHOLDER_MARKERS):
        return True
    return bool(re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", normalized))


def _is_code_or_config_item(line: str) -> bool:
    stripped = line.lstrip()
    return stripped.startswith(("- ", "* ")) and bool(
        re.match(r"^[-*]\s+[A-Za-z0-9_.-]+\s*:", stripped)
    )


def _is_non_secret_field(field: str) -> bool:
    lowered = field.lower()
    return any(marker in lowered for marker in NON_SECRET_FIELD_MARKERS)


def _is_allowed_email(email: str) -> bool:
    domain = email.rsplit("@", 1)[-1].lower()
    return (
        domain in {"example.com", "example.net", "example.org", "example.invalid"}
        or domain.endswith(".example")
        or domain.endswith(".invalid")
    )


def _is_allowed_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host in ALLOWED_URL_HOSTS:
        return True
    return any(host.endswith(suffix) for suffix in ALLOWED_URL_SUFFIXES)


def _is_api_url_context(line: str, url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    return (
        bool(API_URL_CONTEXT_RE.search(line))
        or host.startswith("api.")
        or ".api." in host
        or path in {"/v1", "/v1/"}
        or path.endswith("/v1")
        or path.endswith("/rerank")
    )


def _audit_urls(path: Path, lineno: int, line: str) -> list[Finding]:
    findings: list[Finding] = []
    for match in URL_RE.finditer(line):
        url = match.group(0).rstrip(".,;:")
        if _is_allowed_url(url):
            continue
        if _is_api_url_context(line, url):
            findings.append(_make_finding(path, lineno, "real_api_url", line))
            break
    return findings


def _audit_credentials(path: Path, lineno: int, line: str) -> list[Finding]:
    findings: list[Finding] = []
    for pattern in (CREDENTIAL_ASSIGNMENT_RE, ENV_ASSIGNMENT_RE):
        for match in pattern.finditer(line):
            field = match.group("field")
            value = match.group("value")
            if _is_non_secret_field(field) or _is_placeholder_value(value):
                continue
            findings.append(_make_finding(path, lineno, "credential_field_value", line))
            return findings
    bearer = BEARER_RE.search(line)
    if bearer and not _is_placeholder_value(bearer.group("value")):
        findings.append(_make_finding(path, lineno, "credential_field_value", line))
    return findings


def _audit_pii_assignments(path: Path, lineno: int, line: str) -> list[Finding]:
    findings: list[Finding] = []
    text = _strip_inline_code_spans(line)
    for pattern in (PII_CN_ASSIGNMENT_RE, PII_EN_ASSIGNMENT_RE):
        for match in pattern.finditer(text):
            value = match.group("value")
            if _is_placeholder_value(value):
                continue
            findings.append(_make_finding(path, lineno, "pii_assignment", line))
            return findings
    return findings


def audit_file(path: Path) -> list[Finding]:
    findings: list[Finding] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for lineno, line in enumerate(text.splitlines(), start=1):
        token_hit = False
        for rule, pattern in TOKEN_PATTERNS:
            if pattern.search(line):
                findings.append(_make_finding(path, lineno, rule, line))
                token_hit = True
        email_hit = False
        for email in EMAIL_RE.findall(line):
            if not _is_allowed_email(email):
                findings.append(_make_finding(path, lineno, "email_address", line))
                email_hit = True
                break
        phone_or_id_hit = False
        if CN_PHONE_RE.search(line):
            findings.append(_make_finding(path, lineno, "phone_cn", line))
            phone_or_id_hit = True
        if CN_ID_RE.search(line):
            findings.append(_make_finding(path, lineno, "id_cn", line))
            phone_or_id_hit = True
        if LOCAL_HOME_RE.search(line):
            findings.append(_make_finding(path, lineno, "local_home_path", line))
        findings.extend(_audit_urls(path, lineno, line))
        if not token_hit:
            findings.extend(_audit_credentials(path, lineno, line))
        if not email_hit and not phone_or_id_hit and not _is_code_or_config_item(line):
            findings.extend(_audit_pii_assignments(path, lineno, line))
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
        "rules": sorted(set(RULE_MESSAGES) | {rule for rule, _ in TOKEN_PATTERNS}),
        "credential_fields": CREDENTIAL_FIELDS,
        "pii_keywords_cn": PII_KEYWORDS_CN,
        "pii_keywords_en": PII_KEYWORDS_EN,
        "findings": [asdict(finding) for finding in findings],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--strict", action="store_true", help="Exit non-zero on findings.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args(argv)

    paths = list(args.paths) if args.paths else default_paths()
    findings = audit_paths(paths)
    payload = build_payload(paths, findings)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif findings:
        print("Docs sensitive info audit found issue(s):")
        for finding in findings:
            print(
                f"- {finding.path}:{finding.line} [{finding.rule}] "
                f"{finding.text}\n  {finding.message}"
            )
    else:
        print("Docs sensitive info audit passed.")
    return 1 if args.strict and findings else 0


if __name__ == "__main__":
    sys.exit(main())
