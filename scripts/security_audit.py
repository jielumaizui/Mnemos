#!/usr/bin/env python3
"""安全审计脚本（S47）。

运行 bandit（high 级别）与 pip-audit（任意已知依赖漏洞），并调用
scripts/health_check.py::check_security() 做本地安全状态总览。
默认优先使用仓库 .venv 中的 Python；传入 --no-venv-autodetect 或
--strict-env 时才强制使用当前解释器。

退出码：
  0  无 high bandit 问题、无已知依赖漏洞
  1  发现 high bandit 问题、strict medium 回归、已知依赖漏洞或工具缺失
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

PROJECT_ROOT = Path(__file__).parent.parent
DEV_DEPENDENCY_REPAIR_COMMAND = "uv pip install -r requirements-dev.txt"
BANDIT_ARGS = [
    "-r",
    "core",
    "integrations",
    "daemon",
    "scripts",
    "mnemos_cli.py",
    "mnemos_daemon.py",
    "-ll",
    "-ii",
    "-f",
    "json",
]
SECURITY_AUDIT_SCHEMA_VERSION = "mnemos.security_audit.v2"


@dataclass(frozen=True)
class SecurityFinding:
    source: str
    code: str
    severity: str
    message: str
    repair_action: str


def _finding(
    code: str,
    message: str,
    repair_action: str,
    *,
    severity: str = "blocking",
) -> SecurityFinding:
    return SecurityFinding(
        source="health_security",
        code=code,
        severity=severity,
        message=message,
        repair_action=repair_action,
    )


def _python_cmd(autodetect_venv: bool = True) -> str:
    if autodetect_venv:
        for candidate in (
            PROJECT_ROOT / ".venv" / "bin" / "python",
            PROJECT_ROOT / ".venv" / "Scripts" / "python.exe",
        ):
            if candidate.exists():
                return str(candidate)
    return sys.executable


def _python_environment(python_cmd: str) -> str:
    try:
        selected_path = Path(python_cmd).expanduser()
        if not selected_path.is_absolute():
            selected_path = PROJECT_ROOT / selected_path
        selected = selected_path.absolute()
        repo_venv = (PROJECT_ROOT / ".venv").absolute()
        if repo_venv in selected.parents:
            return "repo_venv"
        if selected_path.resolve() == Path(sys.executable).resolve():
            return "current_python"
    except OSError:
        pass
    return "external_python"


def _module_available(python_cmd: str, module_name: str) -> bool:
    probe = (
        "import importlib.util, sys; "
        "sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 1)"
    )
    try:
        proc = subprocess.run(
            [python_cmd, "-c", probe, module_name],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return getattr(proc, "returncode", 0) == 0


def _missing_dependency_message(tool_name: str, module_name: str, python_cmd: str) -> str:
    return (
        f"{tool_name} dependency missing for Python {python_cmd} "
        f"(module {module_name}). Install dev dependencies with: "
        f"{DEV_DEPENDENCY_REPAIR_COMMAND}; or run: "
        f"{python_cmd} -m pip install -r requirements-dev.txt"
    )


def _loads_json_output(raw: str) -> Dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(raw[start : end + 1])
                return data if isinstance(data, dict) else {}
            except json.JSONDecodeError:
                return {}
        return {}


def _run_bandit(python_cmd: Optional[str] = None, *, fail_on_medium: bool = False) -> Dict[str, Any]:
    python_cmd = python_cmd or _python_cmd()
    result: Dict[str, Any] = {
        "ok": True,
        "high": 0,
        "medium": 0,
        "low": 0,
        "errors": [],
        "python": python_cmd,
        "environment": _python_environment(python_cmd),
        "dependency_missing": False,
    }
    if not _module_available(python_cmd, "bandit"):
        result["ok"] = False
        result["dependency_missing"] = True
        result["errors"].append(_missing_dependency_message("bandit", "bandit", python_cmd))
        return result

    try:
        proc = subprocess.run(
            [python_cmd, "-m", "bandit", *BANDIT_ARGS],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except FileNotFoundError:
        result["ok"] = False
        result["errors"].append("bandit not installed")
        return result
    except subprocess.TimeoutExpired:
        result["ok"] = False
        result["errors"].append("bandit timed out")
        return result

    data = _loads_json_output(proc.stdout)

    if getattr(proc, "returncode", 0) != 0 and not data.get("results"):
        result["ok"] = False
        result["errors"].append((proc.stderr or "bandit failed").strip())
        return result

    for issue in data.get("results", []):
        severity = issue.get("issue_severity", "LOW").lower()
        if severity == "high":
            result["high"] += 1
        elif severity == "medium":
            result["medium"] += 1
        else:
            result["low"] += 1
        if severity == "high":
            result["errors"].append(
                f"bandit {issue.get('test_id')} {issue.get('issue_text')} "
                f"at {issue.get('filename')}:{issue.get('line_number')}"
            )

    if result["high"] > 0:
        result["ok"] = False
    if fail_on_medium and result["medium"] > 0:
        result["ok"] = False
        result["errors"].append(f"bandit strict medium issues: {result['medium']}")
    return result


def _run_pip_audit(python_cmd: Optional[str] = None) -> Dict[str, Any]:
    python_cmd = python_cmd or _python_cmd()
    result: Dict[str, Any] = {
        "ok": True,
        "vulnerabilities": [],
        "errors": [],
        "python": python_cmd,
        "environment": _python_environment(python_cmd),
        "dependency_missing": False,
    }
    if not _module_available(python_cmd, "pip_audit"):
        result["ok"] = False
        result["dependency_missing"] = True
        result["errors"].append(_missing_dependency_message("pip-audit", "pip_audit", python_cmd))
        return result

    try:
        proc = subprocess.run(
            [python_cmd, "-m", "pip_audit", "--desc", "--format=json", "--local"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except FileNotFoundError:
        result["ok"] = False
        result["errors"].append("pip-audit not installed")
        return result
    except subprocess.TimeoutExpired:
        result["ok"] = False
        result["errors"].append("pip-audit timed out")
        return result

    data = _loads_json_output(proc.stdout)

    if getattr(proc, "returncode", 0) != 0 and not data:
        result["ok"] = False
        result["errors"].append((proc.stderr or "pip-audit failed").strip())
        return result

    for dep, dep_info in _iter_pip_audit_dependencies(data):
        installed_version = str(dep_info.get("version", "unknown"))
        for vuln in dep_info.get("vulns", []):
            fix_versions = vuln.get("fix_versions", [])
            vuln_id = vuln.get("id") or vuln.get("vulnerability_id") or "unknown"
            entry = {
                "package": dep,
                "installed_version": installed_version,
                "vulnerability_id": vuln_id,
                "description": vuln.get("description", "")[:200],
                "fix_versions": fix_versions,
                "aliases": vuln.get("aliases", []),
            }
            result["vulnerabilities"].append(entry)
            fix_label = ",".join(fix_versions) if fix_versions else "none"
            result["errors"].append(
                f"pip-audit {dep} {installed_version} {vuln_id} fix={fix_label}"
            )

    if result["vulnerabilities"]:
        result["ok"] = False
    return result


def _iter_pip_audit_dependencies(data: Any) -> Iterable[Tuple[str, Dict[str, Any]]]:
    if not isinstance(data, dict):
        return

    dependencies = data.get("dependencies")
    if isinstance(dependencies, list):
        for dep_info in dependencies:
            if not isinstance(dep_info, dict):
                continue
            name = str(dep_info.get("name") or "unknown")
            yield name, dep_info
        return

    for name, dep_info in data.items():
        if not isinstance(dep_info, dict):
            continue
        dep_info.setdefault("name", name)
        yield str(name), dep_info


def _health_security_findings(sec: Dict[str, Any]) -> list[SecurityFinding]:
    findings: list[SecurityFinding] = []
    health_status = str(sec.get("status", "unknown"))
    if health_status == "warning":
        findings.append(
            _finding(
                "health_security_warning",
                "health security reported warning status",
                "Review the health security warning before release.",
                severity="warning",
            )
        )
    elif health_status != "ok":
        findings.append(
            _finding(
                "health_security_status",
                f"health security status is {health_status}",
                "Repair the health security check until status=ok or an explicit warning.",
            )
        )
    if sec.get("pickle_findings") or sec.get("weak_hash_findings"):
        findings.append(
            _finding(
                "unsafe_serialization_or_hash",
                "health check security found pickle or weak hash",
                "Remove unsafe pickle loading or weak cryptographic hashes.",
            )
        )
    if sec.get("legacy_key_rows", {}).get("enc_rows", 0) > 0:
        findings.append(
            _finding(
                "legacy_encrypted_credentials",
                "legacy XOR-encrypted credential rows found",
                "Migrate legacy credential rows to key references and remove the old rows.",
            )
        )
    if sec.get("plaintext_api_key_risks"):
        findings.append(
            _finding(
                "plaintext_api_key_pattern",
                "plaintext API key patterns found in config",
                "Replace plaintext API keys with env:, keyring:, or keyref: references.",
            )
        )
    plaintext_secret_count = int(
        sec.get("secret_inventory", {}).get("plaintext_count", 0) or 0
    )
    if plaintext_secret_count:
        findings.append(
            _finding(
                "plaintext_secret_inventory",
                f"plaintext secret-like config values found: {plaintext_secret_count}",
                "Replace plaintext secret-like values with approved secret references.",
            )
        )
    return findings


def _finalize_health_security(
    *,
    python_cmd: str,
    health_status: str,
    findings: list[SecurityFinding],
) -> Dict[str, Any]:
    blocking = [finding for finding in findings if finding.severity == "blocking"]
    warnings = [finding for finding in findings if finding.severity == "warning"]
    return {
        "ok": not blocking,
        "status": "failed" if blocking else ("warning" if warnings else "ok"),
        "health_status": health_status,
        "blocking_count": len(blocking),
        "warning_count": len(warnings),
        "errors": [finding.message for finding in blocking],
        "findings": [asdict(finding) for finding in findings],
        "python": python_cmd,
        "environment": _python_environment(python_cmd),
    }


def _run_health_security(python_cmd: Optional[str] = None) -> Dict[str, Any]:
    python_cmd = python_cmd or _python_cmd()
    try:
        proc = subprocess.run(
            [
                python_cmd,
                "-c",
                (
                    "import json, sys; "
                    f"sys.path.insert(0, {str(PROJECT_ROOT)!r}); "
                    "from scripts.health_check import check_security; "
                    "print(json.dumps(check_security(), default=str))"
                ),
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if getattr(proc, "returncode", 0) != 0:
            message = (proc.stderr or "health check security failed").strip()
            return _finalize_health_security(
                python_cmd=python_cmd,
                health_status="error",
                findings=[
                    _finding(
                        "health_security_command_failed",
                        message,
                        "Run the health security check directly and repair the command failure.",
                    )
                ],
            )
        sec = _loads_json_output(proc.stdout)
        health_status = str(sec.get("status", "unknown"))
        return _finalize_health_security(
            python_cmd=python_cmd,
            health_status=health_status,
            findings=_health_security_findings(sec),
        )
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError) as exc:
        return _finalize_health_security(
            python_cmd=python_cmd,
            health_status="error",
            findings=[
                _finding(
                    "health_security_exception",
                    f"health check security failed: {exc}",
                    "Repair the health security exception before release.",
                )
            ],
        )


def _component_findings(source: str, result: Dict[str, Any]) -> list[SecurityFinding]:
    structured = result.get("findings")
    if isinstance(structured, list):
        findings: list[SecurityFinding] = []
        for item in structured:
            if not isinstance(item, dict):
                continue
            findings.append(
                SecurityFinding(
                    source=str(item.get("source", source)),
                    code=str(item.get("code", f"{source}_finding")),
                    severity=str(item.get("severity", "blocking")),
                    message=str(item.get("message", "security finding")),
                    repair_action=str(item.get("repair_action", "Repair the security finding.")),
                )
            )
        if findings:
            return findings
    errors = [str(error) for error in result.get("errors", []) if str(error)]
    if not errors and result.get("ok") is not True:
        errors = [f"{source} reported ok=false without a finding"]
    return [
        SecurityFinding(
            source=source,
            code=f"{source}_blocking_error",
            severity="blocking",
            message=message,
            repair_action=f"Run the {source} check directly and repair the blocking error.",
        )
        for message in errors
    ]


def build_security_report(
    bandit_result: Dict[str, Any],
    pip_audit_result: Dict[str, Any],
    health_result: Dict[str, Any],
) -> Dict[str, Any]:
    findings = [
        *_component_findings("bandit", bandit_result),
        *_component_findings("pip_audit", pip_audit_result),
        *_component_findings("health_security", health_result),
    ]
    blocking = [finding for finding in findings if finding.severity == "blocking"]
    warnings = [finding for finding in findings if finding.severity == "warning"]
    report = {
        "schema_version": SECURITY_AUDIT_SCHEMA_VERSION,
        "summary": {
            "ok": not blocking,
            "status": "failed" if blocking else ("warning" if warnings else "ok"),
            "blocking_count": len(blocking),
            "warning_count": len(warnings),
        },
        "findings": [asdict(finding) for finding in findings],
        "checks": {
            "bandit": bandit_result,
            "pip_audit": pip_audit_result,
            "health_security": health_result,
        },
    }
    validation_errors = validate_security_report(report)
    if validation_errors:
        raise AssertionError(f"invalid security report: {validation_errors}")
    return report


def validate_security_report(
    report: Dict[str, Any],
    *,
    returncode: int | None = None,
) -> list[str]:
    errors: list[str] = []
    if report.get("schema_version") != SECURITY_AUDIT_SCHEMA_VERSION:
        errors.append("schema_version")
    summary = report.get("summary")
    findings = report.get("findings")
    checks = report.get("checks")
    if not isinstance(summary, dict) or not isinstance(findings, list) or not isinstance(checks, dict):
        return errors + ["structure"]
    valid_findings = []
    for finding in findings:
        if (
            not isinstance(finding, dict)
            or finding.get("severity") not in {"blocking", "warning"}
            or not all(
                isinstance(finding.get(field), str) and finding.get(field)
                for field in ("source", "code", "message", "repair_action")
            )
        ):
            errors.append("finding_structure")
            continue
        valid_findings.append(finding)
    blocking_count = sum(
        1 for finding in valid_findings if finding["severity"] == "blocking"
    )
    warning_count = sum(
        1 for finding in valid_findings if finding["severity"] == "warning"
    )
    if summary.get("blocking_count") != blocking_count:
        errors.append("blocking_count")
    if summary.get("warning_count") != warning_count:
        errors.append("warning_count")
    if (summary.get("ok") is True) != (blocking_count == 0):
        errors.append("ok_invariant")
    expected_status = "failed" if blocking_count else ("warning" if warning_count else "ok")
    if summary.get("status") != expected_status:
        errors.append("status_invariant")
    if returncode is not None:
        expected_returncode = 0 if blocking_count == 0 else 1
        if returncode != expected_returncode:
            errors.append("returncode_invariant")
    return errors


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Mnemos local security audit.")
    parser.add_argument(
        "--no-venv-autodetect",
        "--strict-env",
        dest="no_venv_autodetect",
        action="store_true",
        help="Use the current Python interpreter instead of auto-detecting repo .venv.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail when Bandit reports any medium severity issue.",
    )
    parser.add_argument("--json", action="store_true", help="Emit mnemos.security_audit.v2 JSON.")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    python_cmd = _python_cmd(autodetect_venv=not args.no_venv_autodetect)
    bandit_result = _run_bandit(python_cmd, fail_on_medium=args.strict)
    pip_audit_result = _run_pip_audit(python_cmd)
    health_result = _run_health_security(python_cmd)
    report = build_security_report(bandit_result, pip_audit_result, health_result)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["summary"]["ok"] else 1

    print("=== Security Audit (S47) ===")
    print(
        "tool python: "
        f"{python_cmd} ({_python_environment(python_cmd)}, "
        f"venv_autodetect={'off' if args.no_venv_autodetect else 'on'})"
    )

    strict_label = " strict=on" if args.strict else ""
    print(
        f"bandit: high={bandit_result['high']} medium={bandit_result['medium']} "
        f"low={bandit_result['low']}{strict_label}"
    )
    for err in bandit_result["errors"]:
        print(f"  BANDIT: {err}")

    print(f"pip-audit: vulnerabilities={len(pip_audit_result['vulnerabilities'])}")
    for err in pip_audit_result["errors"]:
        print(f"  PIP-AUDIT: {err}")

    print(
        f"health security: status={health_result['status']} "
        f"health_status={health_result['health_status']} "
        f"blocking={health_result['blocking_count']} warnings={health_result['warning_count']}"
    )
    for err in health_result["errors"]:
        print(f"  HEALTH: {err}")

    if report["summary"]["ok"]:
        print("\nOK: no blocking bandit issues or dependency vulnerabilities detected")
        return 0

    print("\nFAIL: blocking security issues detected")
    return 1


if __name__ == "__main__":
    sys.exit(main())
