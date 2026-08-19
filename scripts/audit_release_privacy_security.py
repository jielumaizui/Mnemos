#!/usr/bin/env python3
"""Aggregate release-level privacy and security gates."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.privacy.redaction import redact_text  # noqa: E402

SCHEMA_VERSION = "mnemos.release_privacy_security.v1"

Runner = Callable[..., subprocess.CompletedProcess[str]]

URL_RE = re.compile(r"https?://[^\s`'\"<>)\]}]+")
LOCAL_HOME_RE = re.compile(
    r"(?<![\w.-])(?:"
    r"/(?:Users|home)/(?:[A-Za-z0-9._-]+|<[^/>\s]+>)"
    r"|[A-Za-z]:\\+Users\\+(?:[A-Za-z0-9._-]+|<[^\\>\s]+>)"
    r")(?:[^\s`'\"),\]]*)?"
)
KEY_SOURCE_RE = re.compile(r"\b(?:env|keyring|keyref):[A-Za-z0-9_.:/@-]+\b")
TOKEN_PATTERNS = (
    ("raw_openai_key", re.compile(r"\bsk-(?!ant-)[A-Za-z0-9_-]{16,}\b")),
    ("raw_anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b")),
    ("raw_google_key", re.compile(r"\bAIza[0-9A-Za-z_-]{16,}\b")),
    ("raw_github_token", re.compile(r"\bgh[opusr]_[0-9A-Za-z_]{16,}\b")),
    ("raw_github_pat", re.compile(r"\bgithub_pat_[0-9A-Za-z_]{16,}\b")),
    ("raw_slack_token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{8,}\b")),
    ("raw_huggingface_token", re.compile(r"\bhf_[0-9A-Za-z]{16,}\b")),
)
ALLOWED_URL_HOSTS = {"localhost", "127.0.0.1", "::1"}
ALLOWED_URL_SUFFIXES = (
    ".example",
    ".example.com",
    ".example.net",
    ".example.org",
    ".example.invalid",
    ".invalid",
)


def _display_command(command: Sequence[str]) -> list[str]:
    return [redact_text(part) for part in command]


@dataclass
class CommandCheck:
    id: str
    command: list[str]
    returncode: int | None
    status: str
    ok: bool
    error: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class Finding:
    source: str
    code: str
    message: str
    repair_action: str
    evidence: dict[str, Any] = field(default_factory=dict)


def _python_cmd() -> str:
    for candidate in (
        ROOT / ".venv" / "bin" / "python",
        ROOT / ".venv" / "Scripts" / "python.exe",
    ):
        if candidate.exists():
            return str(candidate)
    return sys.executable


def _loads_json_output(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for match in re.finditer(r"{", raw):
            try:
                data, _ = decoder.raw_decode(raw[match.start() :])
                return data if isinstance(data, dict) else {}
            except json.JSONDecodeError:
                continue
    return {}


def _run(
    command: Sequence[str],
    *,
    runner: Runner,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return runner(
        list(command),
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _run_json_check(
    check_id: str,
    command: Sequence[str],
    *,
    runner: Runner,
    timeout: int = 300,
    allow_nonzero_json: bool = False,
) -> tuple[CommandCheck, dict[str, Any], str]:
    try:
        proc = _run(command, runner=runner, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        check = CommandCheck(
            id=check_id,
            command=_display_command(command),
            returncode=None,
            status="failed",
            ok=False,
            error=f"timed out after {timeout}s",
        )
        return check, {}, str(exc)
    except OSError as exc:
        check = CommandCheck(
            id=check_id,
            command=_display_command(command),
            returncode=None,
            status="failed",
            ok=False,
            error=str(exc),
        )
        return check, {}, ""

    payload = _loads_json_output(proc.stdout)
    ok = bool(payload) and (allow_nonzero_json or proc.returncode == 0)
    check = CommandCheck(
        id=check_id,
        command=_display_command(command),
        returncode=proc.returncode,
        status="passed" if ok else "failed",
        ok=ok,
        error="" if ok else (proc.stderr or "missing JSON payload").strip(),
    )
    return check, payload, proc.stdout


def _run_command_check(
    check_id: str,
    command: Sequence[str],
    *,
    runner: Runner,
    timeout: int = 300,
) -> CommandCheck:
    try:
        proc = _run(command, runner=runner, timeout=timeout)
    except subprocess.TimeoutExpired:
        return CommandCheck(
            id=check_id,
            command=_display_command(command),
            returncode=None,
            status="failed",
            ok=False,
            error=f"timed out after {timeout}s",
        )
    except OSError as exc:
        return CommandCheck(
            id=check_id,
            command=_display_command(command),
            returncode=None,
            status="failed",
            ok=False,
            error=str(exc),
        )
    return CommandCheck(
        id=check_id,
        command=_display_command(command),
        returncode=proc.returncode,
        status="passed" if proc.returncode == 0 else "failed",
        ok=proc.returncode == 0,
        error="" if proc.returncode == 0 else redact_text((proc.stderr or proc.stdout).strip())[:500],
    )


def _run_text_check(
    check_id: str,
    command: Sequence[str],
    *,
    runner: Runner,
    timeout: int = 300,
) -> tuple[CommandCheck, str]:
    try:
        proc = _run(command, runner=runner, timeout=timeout)
    except subprocess.TimeoutExpired:
        return (
            CommandCheck(
                id=check_id,
                command=_display_command(command),
                returncode=None,
                status="failed",
                ok=False,
                error=f"timed out after {timeout}s",
            ),
            "",
        )
    except OSError as exc:
        return (
            CommandCheck(
                id=check_id,
                command=_display_command(command),
                returncode=None,
                status="failed",
                ok=False,
                error=str(exc),
            ),
            "",
        )

    diagnostic_text = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
    return (
        CommandCheck(
            id=check_id,
            command=_display_command(command),
            returncode=proc.returncode,
            status="passed" if proc.returncode == 0 else "failed",
            ok=proc.returncode == 0,
            error="" if proc.returncode == 0 else redact_text(diagnostic_text.strip())[:500],
        ),
        diagnostic_text,
    )


def _finding(
    source: str,
    code: str,
    message: str,
    repair_action: str,
    **evidence: Any,
) -> Finding:
    return Finding(source, code, message, repair_action, evidence)


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _iter_items(config_report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    items = config_report.get("items", [])
    if not isinstance(items, list):
        return {}
    return {
        str(item.get("item_id")): item
        for item in items
        if isinstance(item, Mapping) and item.get("item_id")
    }


def _evaluate_config_report(config_report: Mapping[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    required_failed = config_report.get("required_failed") or []
    if config_report.get("ok") is not True or required_failed:
        findings.append(
            _finding(
                "doctor_config",
                "config_doctor_required_failed",
                "Strict config doctor is not release-clean.",
                "Run python3 mnemos_cli.py doctor config --strict --json and fix required_failed items.",
                required_failed=required_failed,
            )
        )

    items = _iter_items(config_report)
    secret_item = items.get("security.secret_inventory")
    secret_evidence = (
        secret_item.get("evidence", {}) if isinstance(secret_item, Mapping) else {}
    )
    plaintext = _as_int(secret_evidence.get("plaintext_count"))
    if plaintext:
        findings.append(
            _finding(
                "doctor_config",
                "plaintext_secret_inventory",
                "Plaintext secret-like config values block release security.",
                "Replace secret-like config values with env:, keyring:, or keyref: references.",
                plaintext_count=plaintext,
            )
        )

    return findings


def _evaluate_security_audit_report(
    report: Mapping[str, Any],
    *,
    returncode: int | None,
) -> list[Finding]:
    from scripts.security_audit import validate_security_report

    errors = validate_security_report(dict(report), returncode=returncode)
    summary = report.get("summary")
    findings = report.get("findings")
    finding_items = findings if isinstance(findings, list) else []
    if not isinstance(summary, Mapping) or not isinstance(findings, list):
        blocking_count = -1
        warning_count = -1
    else:
        blocking_count = sum(
            1
            for finding in finding_items
            if isinstance(finding, Mapping) and finding.get("severity") == "blocking"
        )
        warning_count = sum(
            1
            for finding in finding_items
            if isinstance(finding, Mapping) and finding.get("severity") == "warning"
        )
    if errors:
        return [
            _finding(
                "security_audit.strict",
                "security_report_invariant_failed",
                "Security audit report fields contradict its blocking findings or exit code.",
                "Run python3 scripts/security_audit.py --strict --json and repair the report invariant.",
                invariant_errors=errors,
                blocking_count=blocking_count,
                warning_count=warning_count,
                returncode=returncode,
            )
        ]
    if blocking_count:
        blocking_codes = sorted(
            str(finding.get("code", "unknown"))
            for finding in finding_items
            if isinstance(finding, Mapping) and finding.get("severity") == "blocking"
        )
        return [
            _finding(
                "security_audit.strict",
                "security_audit_blocking_findings",
                f"Security audit reported {blocking_count} blocking finding(s).",
                "Repair every blocking security finding and rerun the strict JSON audit.",
                blocking_codes=blocking_codes,
            )
        ]
    return []


def _security_audit_warning_findings(report: Mapping[str, Any]) -> list[Finding]:
    findings = report.get("findings")
    if not isinstance(findings, list):
        return []
    warning_codes = sorted(
        str(finding.get("code", "unknown"))
        for finding in findings
        if isinstance(finding, Mapping) and finding.get("severity") == "warning"
    )
    if not warning_codes:
        return []
    return [
        _finding(
            "security_audit.strict",
            "security_audit_warning_findings",
            f"Security audit reported {len(warning_codes)} warning finding(s).",
            "Review security warnings before release and promote any active risk to blocking.",
            warning_codes=warning_codes,
        )
    ]


def _evaluate_health_report(health_report: Mapping[str, Any]) -> tuple[list[Finding], list[Finding]]:
    blocking: list[Finding] = []
    warnings: list[Finding] = []
    checks = health_report.get("checks", {})
    if not isinstance(checks, Mapping):
        blocking.append(
            _finding(
                "health",
                "missing_checks",
                "Health JSON did not contain a checks object.",
                "Run python3 mnemos_cli.py health --json and repair the health report output.",
            )
        )
        return blocking, warnings

    security = checks.get("security", {})
    if not isinstance(security, Mapping):
        blocking.append(
            _finding(
                "health.security",
                "missing_security_check",
                "Health JSON did not contain checks.security.",
                "Restore scripts.health_check.check_security integration.",
            )
        )
    else:
        blocking.extend(_evaluate_health_security(security))
        if security.get("keyring_safe_but_not_best") or security.get("safe_but_not_best"):
            warnings.append(
                _finding(
                    "health.security",
                    "keyring_safe_but_not_best",
                    "Secret references are safe but not using the best keyring path.",
                    "Run python3 mnemos_cli.py secrets doctor --json and migrate to keyring/keyref when practical.",
                    keyring_status=security.get("keyring_status"),
                )
            )

    return blocking, warnings


def _evaluate_health_security(security: Mapping[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    status = str(security.get("status", "unknown"))
    if status in {"degraded", "failed", "error"}:
        findings.append(
            _finding(
                "health.security",
                "security_check_not_ok",
                f"Health security status is {status}.",
                "Run python3 mnemos_cli.py health --json and fix checks.security repair_actions.",
                status=status,
            )
        )
    if security.get("permission_violations"):
        findings.append(
            _finding(
                "health.security",
                "sensitive_permission_violation",
                "Sensitive runtime files have overly broad permissions.",
                "Apply chmod repair actions from checks.security.repair_actions.",
                violations=security.get("permission_violations"),
            )
        )
    secret_inventory = security.get("secret_inventory", {})
    if isinstance(secret_inventory, Mapping):
        plaintext = _as_int(secret_inventory.get("plaintext_count"))
        if plaintext:
            findings.append(
                _finding(
                    "health.security",
                    "plaintext_secret_inventory",
                    "Plaintext secret-like config values block release security.",
                    "Replace secret-like config values with env:, keyring:, or keyref: references.",
                    plaintext_count=plaintext,
                )
            )
    if security.get("plaintext_api_key_risks"):
        findings.append(
            _finding(
                "health.security",
                "plaintext_api_key_risk",
                "Plaintext API key patterns were found in runtime config.",
                "Move API keys to env:, keyring:, or keyref: references.",
                risks=security.get("plaintext_api_key_risks"),
            )
        )
    if security.get("pickle_findings") or security.get("weak_hash_findings"):
        findings.append(
            _finding(
                "health.security",
                "unsafe_code_pattern",
                "Pickle or weak hash findings were reported by security health.",
                "Remove unsafe serialization or require usedforsecurity=False for non-security hashes.",
                pickle_findings=security.get("pickle_findings"),
                weak_hash_findings=security.get("weak_hash_findings"),
            )
        )
    return findings


def _is_allowed_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host or host in ALLOWED_URL_HOSTS:
        return True
    if host in {"****"}:
        return True
    return any(host.endswith(suffix) for suffix in ALLOWED_URL_SUFFIXES)


def _diagnostic_redaction_findings(outputs: Mapping[str, str]) -> list[Finding]:
    findings: list[Finding] = []
    for source, text in outputs.items():
        for match in URL_RE.finditer(text):
            url = match.group(0).rstrip(".,;:")
            if _is_allowed_url(url):
                continue
            findings.append(
                _finding(
                    source,
                    "diagnostic_real_api_url",
                    "Diagnostic output contains a real URL.",
                    "Route diagnostic payloads through core.privacy.redaction.redact_sensitive_data().",
                    match=redact_text(url),
                )
            )
            break
        path_match = LOCAL_HOME_RE.search(text)
        if path_match:
            findings.append(
                _finding(
                    source,
                    "diagnostic_local_path",
                    "Diagnostic output contains a machine-local home path.",
                    "Redact local paths to <HOME> or <REPO> before printing shareable diagnostics.",
                    match=redact_text(path_match.group(0)),
                )
            )
        key_source_match = KEY_SOURCE_RE.search(text)
        if key_source_match and "****" not in key_source_match.group(0):
            findings.append(
                _finding(
                    source,
                    "diagnostic_unredacted_key_source",
                    "Diagnostic output contains an unredacted env/keyring/keyref identifier.",
                    "Redact key sources with core.privacy.redaction.redact_key_source().",
                    match=redact_text(key_source_match.group(0)),
                )
            )
        for rule, pattern in TOKEN_PATTERNS:
            token_match = pattern.search(text)
            if token_match:
                findings.append(
                    _finding(
                        source,
                        f"diagnostic_{rule}",
                        "Diagnostic output contains a provider-shaped token.",
                        "Use placeholders or runtime-constructed sentinel values in diagnostics.",
                        match=f"<{rule}>",
                    )
                )
                break
    return findings


def _payload(
    *,
    strict: bool,
    checks: Sequence[CommandCheck],
    blocking_findings: Sequence[Finding],
    warning_findings: Sequence[Finding],
) -> dict[str, Any]:
    repair_actions = sorted(
        {
            finding.repair_action
            for finding in [*blocking_findings, *warning_findings]
            if finding.repair_action
        }
    )
    ok = not blocking_findings and all(check.ok for check in checks)
    return {
        "schema_version": SCHEMA_VERSION,
        "release_privacy_security": {
            "ok": ok,
            "strict": strict,
            "blocking_count": len(blocking_findings),
            "warning_count": len(warning_findings),
        },
        "checks": [asdict(check) for check in checks],
        "blocking_findings": [asdict(finding) for finding in blocking_findings],
        "warning_findings": [asdict(finding) for finding in warning_findings],
        "repair_actions": repair_actions,
    }


def run_audit(*, strict: bool, runner: Runner = subprocess.run) -> dict[str, Any]:
    python = _python_cmd()
    checks: list[CommandCheck] = []
    blocking: list[Finding] = []
    warnings: list[Finding] = []
    diagnostic_outputs: dict[str, str] = {}

    security_check, security_report, _security_stdout = _run_json_check(
        "security_audit.strict",
        [python, "scripts/security_audit.py", "--strict", "--json"],
        runner=runner,
        timeout=360,
        allow_nonzero_json=True,
    )
    security_findings = _evaluate_security_audit_report(
        security_report,
        returncode=security_check.returncode,
    ) if security_report else []
    if security_findings:
        security_check.ok = False
        security_check.status = "failed"
        security_check.evidence = {
            "schema_version": security_report.get("schema_version"),
            "blocking_count": security_report.get("summary", {}).get("blocking_count"),
            "warning_count": security_report.get("summary", {}).get("warning_count"),
        }
    checks.append(security_check)
    if security_report:
        blocking.extend(security_findings)
        warnings.extend(_security_audit_warning_findings(security_report))
    elif not security_check.ok:
        blocking.append(
            _finding(
                "security_audit.strict",
                "security_audit_failed",
                "Code/dependency security audit failed.",
                "Run python3 scripts/security_audit.py --strict and fix reported "
                "Bandit, dependency, or health security failures.",
                returncode=security_check.returncode,
                error=security_check.error,
            )
        )

    config_check, config_report, config_stdout = _run_json_check(
        "doctor_config.strict",
        [python, "mnemos_cli.py", "doctor", "config", "--strict", "--json"],
        runner=runner,
        timeout=180,
    )
    checks.append(config_check)
    diagnostic_outputs["doctor_config.strict"] = config_stdout
    if not config_check.ok:
        blocking.append(
            _finding(
                "doctor_config.strict",
                "config_doctor_command_failed",
                "Strict config doctor did not return a usable JSON report.",
                "Run python3 mnemos_cli.py doctor config --strict --json and fix command errors.",
                returncode=config_check.returncode,
                error=config_check.error,
            )
        )
    else:
        blocking.extend(_evaluate_config_report(config_report))

    health_check, health_report, health_stdout = _run_json_check(
        "health.security_privacy",
        [python, "mnemos_cli.py", "health", "--json"],
        runner=runner,
        timeout=180,
        allow_nonzero_json=True,
    )
    checks.append(health_check)
    diagnostic_outputs["health.security_privacy"] = health_stdout
    if not health_check.ok:
        blocking.append(
            _finding(
                "health.security_privacy",
                "health_command_failed",
                "Health command did not return a usable JSON report.",
                "Run python3 mnemos_cli.py health --json and fix command errors.",
                returncode=health_check.returncode,
                error=health_check.error,
            )
        )
    else:
        health_blocking, health_warnings = _evaluate_health_report(health_report)
        blocking.extend(health_blocking)
        warnings.extend(health_warnings)

    diagnostic_commands = (
        (
            "distill.status_redaction",
            [python, "mnemos_cli.py", "distill", "status"],
            "Run python3 mnemos_cli.py distill status and fix unredacted diagnostics.",
        ),
        (
            "e2e_probe.dry_run_redaction",
            [python, "scripts/e2e_probe.py", "--dry-run", "--no-api"],
            "Run python3 scripts/e2e_probe.py --dry-run --no-api and fix unredacted diagnostics.",
        ),
    )
    for check_id, command, repair_action in diagnostic_commands:
        text_check, diagnostic_text = _run_text_check(
            check_id,
            command,
            runner=runner,
            timeout=180,
        )
        checks.append(text_check)
        diagnostic_outputs[check_id] = diagnostic_text
        if not text_check.ok:
            blocking.append(
                _finding(
                    check_id,
                    "diagnostic_command_failed",
                    "Shareable diagnostic command did not complete successfully.",
                    repair_action,
                    returncode=text_check.returncode,
                    error=text_check.error,
                )
            )

    docs_check, docs_report, _ = _run_json_check(
        "docs_sensitive.strict",
        [python, "scripts/audit_docs_sensitive_info.py", "--strict", "--json"],
        runner=runner,
        timeout=180,
    )
    checks.append(docs_check)
    if not docs_check.ok or docs_report.get("ok") is not True:
        blocking.append(
            _finding(
                "docs_sensitive.strict",
                "docs_sensitive_findings",
                "Public Markdown docs contain sensitive information or unsafe examples.",
                "Run python3 scripts/audit_docs_sensitive_info.py --strict --json and redact findings.",
                finding_count=len(docs_report.get("findings", []) or []),
            )
        )

    repo_check, repo_report, _ = _run_json_check(
        "repo_sensitive_literals.strict",
        [python, "scripts/audit_repo_sensitive_literals.py", "--strict", "--json"],
        runner=runner,
        timeout=180,
    )
    checks.append(repo_check)
    if not repo_check.ok or repo_report.get("ok") is not True:
        blocking.append(
            _finding(
                "repo_sensitive_literals.strict",
                "repo_sensitive_literal_findings",
                "Repository text contains provider-shaped tokens, local paths, or credential literals.",
                "Run python3 scripts/audit_repo_sensitive_literals.py --strict --json and remove findings.",
                finding_count=len(repo_report.get("findings", []) or []),
            )
        )

    redaction_findings = _diagnostic_redaction_findings(diagnostic_outputs)
    blocking.extend(redaction_findings)

    return _payload(
        strict=strict,
        checks=checks,
        blocking_findings=blocking,
        warning_findings=warnings,
    )


def _format_text(payload: Mapping[str, Any]) -> str:
    summary = payload["release_privacy_security"]
    lines = [
        "Mnemos Release Privacy/Security Audit",
        "=" * 44,
        f"schema: {payload['schema_version']}",
        f"strict: {summary['strict']}",
        f"ok: {summary['ok']}",
        f"blocking_findings: {summary['blocking_count']}",
        f"warning_findings: {summary['warning_count']}",
        "checks:",
    ]
    for check in payload.get("checks", []):
        lines.append(
            f"  - {check['id']}: {check['status']} rc={check['returncode']}"
        )
    if payload.get("blocking_findings"):
        lines.append("blocking_findings:")
        for finding in payload["blocking_findings"]:
            lines.append(f"  - {finding['source']} [{finding['code']}]: {finding['message']}")
    if payload.get("warning_findings"):
        lines.append("warning_findings:")
        for finding in payload["warning_findings"]:
            lines.append(f"  - {finding['source']} [{finding['code']}]: {finding['message']}")
    if payload.get("repair_actions"):
        lines.append("repair_actions:")
        lines.extend(f"  - {action}" for action in payload["repair_actions"])
    return "\n".join(lines) + "\n"


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="Use release-blocking semantics.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, *, runner: Runner = subprocess.run) -> int:
    args = _parse_args(argv)
    payload = run_audit(strict=bool(args.strict), runner=runner)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(_format_text(payload), end="")
    return 0 if payload["release_privacy_security"]["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
