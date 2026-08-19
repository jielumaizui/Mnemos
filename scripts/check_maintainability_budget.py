#!/usr/bin/env python3
"""Ratchet gate for large files and broad ``except Exception`` usage."""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUDGET_FILE = PROJECT_ROOT / "scripts" / "maintainability_budget.json"
SCAN_PATHS = (
    "core",
    "integrations",
    "daemon",
    "scripts",
    "mnemos_cli.py",
    "mnemos_daemon.py",
)
SKIP_DIRS = {".git", ".venv", ".audit_venv", "__pycache__", ".pytest_cache", "build", "dist"}
SCHEMA_VERSION = "mnemos.maintainability_budget.v2"
REPORT_SCHEMA_VERSION = "mnemos.maintainability_closure.v1"
DEFAULT_ACCEPTANCE_DAYS = 90
DEFAULT_MAX_NEW_FILE_LINES = 1500
DEFAULT_MAX_NEW_BROAD_EXCEPTIONS = 0
DEFAULT_REQUIRED_CLASSIFIED_BROAD_EXCEPTION_PATHS = (
    "core/cli/doctor_helpers.py",
    "core/setup/install_lifecycle.py",
    "core/sync_framework/capture_worker.py",
    "core/sync_framework/sync_engine.py",
    "daemon/raw_sync.py",
    "scripts/distill_all.py",
    "scripts/health_check.py",
)
LOG_RE = re.compile(
    r"\b(logger|log|logging)\.(debug|info|warning|warn|error|exception|critical)\(",
)
DEBT_RE = re.compile(r"#.*?(DEBT\(S8\)|TODO|FIXME|NOTE|boundary|adapter)", re.IGNORECASE)


@dataclass(frozen=True)
class BroadCatch:
    path: str
    line: int
    kind: str
    classified: bool
    classification: str
    fingerprint: str


@dataclass(frozen=True)
class FileMetric:
    path: str
    lines: int
    broad_exceptions: int
    catches: tuple[BroadCatch, ...]
    parse_error: str = ""


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def iter_python_files(root: Path, scan_paths: Iterable[str] = SCAN_PATHS) -> Iterable[Path]:
    for rel in scan_paths:
        path = root / rel
        if path.is_file():
            yield path
            continue
        for fpath in sorted(path.rglob("*.py")):
            if any(part in SKIP_DIRS for part in fpath.parts):
                continue
            yield fpath


def _is_broad_exception(node: ast.ExceptHandler) -> bool:
    exc_type = node.type
    if exc_type is None:
        return False
    if isinstance(exc_type, ast.Name):
        return exc_type.id == "Exception"
    if isinstance(exc_type, ast.Tuple):
        return any(isinstance(elt, ast.Name) and elt.id == "Exception" for elt in exc_type.elts)
    return False


def _handler_lines(lines: list[str], node: ast.ExceptHandler) -> list[str]:
    start = max(0, node.lineno - 1)
    end = getattr(node, "end_lineno", node.lineno) or node.lineno
    return lines[start:end]


def _has_reraise(node: ast.ExceptHandler) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Raise):
            return True
    return False


def _classify_handler(lines: list[str], node: ast.ExceptHandler) -> tuple[bool, str]:
    block = _handler_lines(lines, node)
    text = "\n".join(block)
    if DEBT_RE.search(text):
        return True, "annotated_boundary"
    if LOG_RE.search(text):
        return True, "logged_boundary"
    if _has_reraise(node):
        return True, "reraised"
    return False, "unclassified"


def scan_file(path: Path, root: Path) -> FileMetric:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        return FileMetric(_rel(path, root), 0, 0, (), f"UnicodeError: {exc}")
    lines = text.splitlines()
    catches: list[BroadCatch] = []
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return FileMetric(
            _rel(path, root),
            len(lines),
            0,
            (),
            f"SyntaxError:{exc.lineno}:{exc.offset}: {exc.msg}",
        )
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or not _is_broad_exception(node):
            continue
        classified, classification = _classify_handler(lines, node)
        fingerprint = hashlib.sha256(
            ast.dump(node, include_attributes=False).encode("utf-8")
        ).hexdigest()
        catches.append(
            BroadCatch(
                path=_rel(path, root),
                line=node.lineno,
                kind="except Exception",
                classified=classified,
                classification=classification,
                fingerprint=fingerprint,
            )
        )
    return FileMetric(_rel(path, root), len(lines), len(catches), tuple(catches))


def scan_repo(root: Path) -> dict[str, FileMetric]:
    return {metric.path: metric for metric in (scan_file(path, root) for path in iter_python_files(root))}


def _default_owner(path: str) -> str:
    if path.startswith("core/kia/"):
        return "kia"
    if path.startswith("core/hephaestus/"):
        return "hephaestus"
    if path.startswith("core/scoring/"):
        return "scoring"
    if path.startswith("daemon/") or path == "mnemos_daemon.py":
        return "daemon"
    if path.startswith("integrations/"):
        return "integrations"
    if path.startswith("scripts/"):
        return "ops"
    return "core"


def _acceptance_metadata(
    *,
    path: str,
    owner: str,
    kind: str,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    old = previous or {}
    default_remove_when = (
        "Split the file below the default line limit and remove this acceptance."
        if kind == "large_file"
        else "Replace broad catches with specific exceptions or proven boundary handling."
    )
    return {
        "owner": str(old.get("owner") or owner),
        "expires_at": str(
            old.get("expires_at")
            or (date.today() + timedelta(days=DEFAULT_ACCEPTANCE_DAYS)).isoformat()
        ),
        "telemetry": str(
            old.get("telemetry")
            or "python3 scripts/check_maintainability_budget.py --closure --json"
        ),
        "remove_when": str(old.get("remove_when") or default_remove_when),
        "acceptance_id": str(old.get("acceptance_id") or f"maintainability:{kind}:{path}"),
    }


def build_budget(
    metrics: dict[str, FileMetric],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    previous = previous or {}
    previous_large = previous.get("large_files", {})
    previous_broad = previous.get("broad_exceptions", {})
    large_files: dict[str, dict[str, Any]] = {}
    broad_files: dict[str, dict[str, Any]] = {}
    for path, metric in sorted(metrics.items()):
        owner = _default_owner(path)
        if metric.lines > DEFAULT_MAX_NEW_FILE_LINES:
            large_files[path] = {
                "max_lines": metric.lines,
                **_acceptance_metadata(
                    path=path,
                    owner=owner,
                    kind="large_file",
                    previous=previous_large.get(path),
                ),
                "split_plan": (
                    "Do not add responsibilities here; move new behavior behind "
                    "a focused module before lowering this budget."
                ),
            }
        if metric.broad_exceptions:
            broad_files[path] = {
                "max_count": metric.broad_exceptions,
                "accepted_catches": sorted(catch.fingerprint for catch in metric.catches),
                **_acceptance_metadata(
                    path=path,
                    owner=owner,
                    kind="broad_exception",
                    previous=previous_broad.get(path),
                ),
                "reason": (
                    "Time-bounded residual boundary debt. Exact catch fingerprints "
                    "prevent same-count replacement; unclassified catches remain "
                    "visible until narrowed or proven before expiry."
                ),
                "strategy": (
                    "Ratchet down by replacing broad catches with specific exception "
                    "types when editing this file."
                ),
            }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "max_new_file_lines": DEFAULT_MAX_NEW_FILE_LINES,
        "max_new_broad_exceptions_per_file": DEFAULT_MAX_NEW_BROAD_EXCEPTIONS,
        "max_unclassified_broad_exceptions": sum(
            1 for metric in metrics.values() for catch in metric.catches if not catch.classified
        ),
        "require_per_catch_classification": False,
        "require_classified_broad_exception_paths": list(
            DEFAULT_REQUIRED_CLASSIFIED_BROAD_EXCEPTION_PATHS
        ),
        "scan_paths": list(SCAN_PATHS),
        "large_files": large_files,
        "broad_exceptions": broad_files,
    }


def load_budget(path: Path) -> dict[str, Any]:
    try:
        raw_data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"Missing maintainability budget file: {path}") from None
    if not isinstance(raw_data, dict):
        raise SystemExit(f"Invalid maintainability budget file: {path}")
    data = cast(dict[str, Any], raw_data)
    if data.get("schema_version") != SCHEMA_VERSION:
        raise SystemExit(f"Unsupported maintainability budget schema: {data.get('schema_version')}")
    return data


def risk_acceptance_changes(
    metrics: dict[str, FileMetric],
    previous: dict[str, Any],
) -> list[str]:
    changes: list[str] = []
    previous_large = previous.get("large_files", {})
    previous_broad = previous.get("broad_exceptions", {})
    for path, metric in sorted(metrics.items()):
        if metric.lines > DEFAULT_MAX_NEW_FILE_LINES:
            old_large = previous_large.get(path)
            if old_large is None:
                changes.append(f"new large-file acceptance: {path}")
            elif metric.lines > int(old_large.get("max_lines", 0)):
                changes.append(f"increased large-file acceptance: {path}")
        if not metric.broad_exceptions:
            continue
        old = previous_broad.get(path)
        current_fingerprints = sorted(catch.fingerprint for catch in metric.catches)
        if old is None:
            changes.append(f"new broad-exception acceptance: {path}")
        else:
            accepted_catches = old.get("accepted_catches")
            accepted_set = set(accepted_catches) if isinstance(accepted_catches, list) else set()
            if not set(current_fingerprints).issubset(accepted_set):
                changes.append(f"changed broad-exception identities: {path}")
    return changes


def _acceptance_errors(
    entry: dict[str, Any],
    *,
    path: str,
    kind: str,
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for field in ("acceptance_id", "owner", "expires_at", "telemetry", "remove_when"):
        if not str(entry.get(field, "")).strip():
            failures.append(
                {
                    "type": "missing_risk_acceptance_metadata",
                    "path": path,
                    "kind": kind,
                    "field": field,
                }
            )
    expires_at = str(entry.get("expires_at", ""))
    if expires_at:
        try:
            expiry = date.fromisoformat(expires_at)
        except ValueError:
            failures.append(
                {
                    "type": "invalid_risk_acceptance_expiry",
                    "path": path,
                    "kind": kind,
                    "expires_at": expires_at,
                }
            )
        else:
            if expiry < date.today():
                failures.append(
                    {
                        "type": "expired_risk_acceptance",
                        "path": path,
                        "kind": kind,
                        "expires_at": expires_at,
                    }
                )
    return failures


def check_budget(
    metrics: dict[str, FileMetric],
    budget: dict[str, Any],
    *,
    closure: bool = False,
) -> tuple[bool, dict[str, Any]]:
    max_new_lines = int(budget.get("max_new_file_lines", DEFAULT_MAX_NEW_FILE_LINES))
    max_new_broad = int(
        budget.get("max_new_broad_exceptions_per_file", DEFAULT_MAX_NEW_BROAD_EXCEPTIONS)
    )
    large_budget = budget.get("large_files", {})
    broad_budget = budget.get("broad_exceptions", {})
    require_per_catch = bool(budget.get("require_per_catch_classification", False))
    max_unclassified_raw = budget.get("max_unclassified_broad_exceptions")
    max_unclassified = int(max_unclassified_raw) if max_unclassified_raw is not None else None
    required_classified_paths = tuple(
        str(path)
        for path in budget.get("require_classified_broad_exception_paths", ())
        if str(path).strip()
    )
    failures: list[dict[str, Any]] = []
    improvements: list[dict[str, Any]] = []
    unclassified: list[dict[str, Any]] = []
    required_path_unclassified: list[dict[str, Any]] = []
    accepted_count = 0
    unaccepted_count = 0

    for metric in metrics.values():
        if metric.parse_error:
            failures.append(
                {
                    "type": "source_parse_error",
                    "path": metric.path,
                    "error": metric.parse_error,
                }
            )

    for path, metric in sorted(metrics.items()):
        large_entry = large_budget.get(path)
        if metric.lines > max_new_lines:
            if large_entry is None:
                failures.append(
                    {
                        "type": "new_large_file",
                        "path": path,
                        "lines": metric.lines,
                        "limit": max_new_lines,
                    }
                )
            elif metric.lines > int(large_entry.get("max_lines", 0)):
                failures.append(
                    {
                        "type": "large_file_growth",
                        "path": path,
                        "lines": metric.lines,
                        "limit": int(large_entry.get("max_lines", 0)),
                    }
                )
            elif metric.lines < int(large_entry.get("max_lines", 0)):
                improvements.append(
                    {
                        "type": "large_file_reduced",
                        "path": path,
                        "lines": metric.lines,
                        "previous": int(large_entry.get("max_lines", 0)),
                    }
                )
            if closure and large_entry is not None:
                acceptance_failures = _acceptance_errors(
                    large_entry,
                    path=path,
                    kind="large_file",
                )
                failures.extend(acceptance_failures)
                if acceptance_failures:
                    unaccepted_count += 1
                else:
                    accepted_count += 1
                if metric.lines != int(large_entry.get("max_lines", 0)):
                    failures.append(
                        {
                            "type": "closure_baseline_not_tight",
                            "path": path,
                            "kind": "large_file",
                            "current": metric.lines,
                            "baseline": int(large_entry.get("max_lines", 0)),
                        }
                    )

        broad_entry = broad_budget.get(path)
        if metric.broad_exceptions:
            if broad_entry is None and metric.broad_exceptions > max_new_broad:
                failures.append(
                    {
                        "type": "new_broad_exception_file",
                        "path": path,
                        "count": metric.broad_exceptions,
                        "limit": max_new_broad,
                    }
                )
            elif broad_entry is not None:
                for required in ("owner", "reason", "strategy"):
                    if not str(broad_entry.get(required, "")).strip():
                        failures.append(
                            {
                                "type": "missing_broad_exception_budget_metadata",
                                "path": path,
                                "field": required,
                            }
                        )
                limit = int(broad_entry.get("max_count", 0))
                accepted_catches = broad_entry.get("accepted_catches")
                current_fingerprints = sorted(catch.fingerprint for catch in metric.catches)
                if not isinstance(accepted_catches, list):
                    failures.append(
                        {
                            "type": "missing_exact_broad_exception_registry",
                            "path": path,
                        }
                    )
                elif accepted_catches != current_fingerprints:
                    failures.append(
                        {
                            "type": "broad_exception_identity_changed",
                            "path": path,
                            "current_count": len(current_fingerprints),
                            "registered_count": len(accepted_catches),
                        }
                    )
                if metric.broad_exceptions > limit:
                    failures.append(
                        {
                            "type": "broad_exception_growth",
                            "path": path,
                            "count": metric.broad_exceptions,
                            "limit": limit,
                        }
                    )
                elif metric.broad_exceptions < limit:
                    improvements.append(
                        {
                            "type": "broad_exception_reduced",
                            "path": path,
                            "count": metric.broad_exceptions,
                            "previous": limit,
                        }
                    )
                if closure:
                    acceptance_failures = _acceptance_errors(
                        broad_entry,
                        path=path,
                        kind="broad_exception",
                    )
                    failures.extend(acceptance_failures)
                    if acceptance_failures:
                        unaccepted_count += metric.broad_exceptions
                    else:
                        accepted_count += metric.broad_exceptions
                    if metric.broad_exceptions != limit:
                        failures.append(
                            {
                                "type": "closure_baseline_not_tight",
                                "path": path,
                                "kind": "broad_exception",
                                "current": metric.broad_exceptions,
                                "baseline": limit,
                            }
                        )

        for catch in metric.catches:
            if not catch.classified:
                item = {
                    "type": "unclassified_broad_exception",
                    "path": catch.path,
                    "line": catch.line,
                }
                unclassified.append(item)
                if require_per_catch:
                    failures.append(item)
                if any(fnmatch.fnmatchcase(catch.path, pattern) for pattern in required_classified_paths):
                    required_item = {
                        "type": "unclassified_broad_exception_in_required_path",
                        "path": catch.path,
                        "line": catch.line,
                    }
                    required_path_unclassified.append(required_item)
                    failures.append(required_item)

    if max_unclassified is not None and len(unclassified) > max_unclassified:
        failures.append(
            {
                "type": "unclassified_broad_exception_growth",
                "count": len(unclassified),
                "limit": max_unclassified,
            }
        )
    if closure and max_unclassified != len(unclassified):
        failures.append(
            {
                "type": "closure_baseline_not_tight",
                "kind": "unclassified_broad_exception",
                "current": len(unclassified),
                "baseline": max_unclassified,
            }
        )

    for path, entry in sorted(large_budget.items()):
        large_metric = metrics.get(path)
        if large_metric is None:
            improvements.append({"type": "large_file_removed", "path": path})
            continue
        if large_metric.lines <= max_new_lines:
            improvements.append(
                {
                    "type": "large_file_under_default_limit",
                    "path": path,
                    "lines": large_metric.lines,
                    "limit": max_new_lines,
                }
            )
    for path, entry in sorted(broad_budget.items()):
        broad_metric = metrics.get(path)
        count = broad_metric.broad_exceptions if broad_metric else 0
        if count == 0:
            improvements.append({"type": "broad_exception_removed", "path": path})

    current_count = sum(1 for metric in metrics.values() if metric.lines > max_new_lines) + sum(
        metric.broad_exceptions for metric in metrics.values()
    )
    closure_status = "not_requested"
    if closure:
        if current_count == 0 and not failures:
            closure_status = "zero_debt"
        elif unaccepted_count == 0 and not failures:
            closure_status = "accepted_debt"
        else:
            closure_status = "failed"

    closure_failure_types = {
        "missing_risk_acceptance_metadata",
        "invalid_risk_acceptance_expiry",
        "expired_risk_acceptance",
        "closure_baseline_not_tight",
    }
    ratchet_failures = [
        failure for failure in failures if failure.get("type") not in closure_failure_types
    ]
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "ok": not failures,
        "ratchet_status": "passed" if not ratchet_failures else "failed",
        "closure": {
            "requested": closure,
            "status": closure_status,
            "closure_target": 0,
            "current_count": current_count,
            "accepted_count": accepted_count,
            "unaccepted_count": unaccepted_count,
            "release_eligible": closure and current_count == 0 and not failures,
        },
        "summary": {
            "files_scanned": len(metrics),
            "large_files": sum(1 for metric in metrics.values() if metric.lines > max_new_lines),
            "broad_exception_files": sum(1 for metric in metrics.values() if metric.broad_exceptions),
            "broad_exceptions": sum(metric.broad_exceptions for metric in metrics.values()),
            "unclassified_broad_exceptions": len(unclassified),
            "required_path_unclassified_broad_exceptions": len(required_path_unclassified),
            "failure_count": len(failures),
            "improvement_count": len(improvements),
        },
        "failures": failures,
        "improvements": improvements,
        "unclassified_broad_exceptions": unclassified[:200],
        "required_path_unclassified_broad_exceptions": required_path_unclassified,
    }
    return not failures, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check maintainability budget ratchets.")
    parser.add_argument("--budget-file", default=str(BUDGET_FILE))
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument("--json", action="store_true", help="Print machine-readable report.")
    parser.add_argument("--closure", action="store_true", help="Validate closure metadata and tight baselines.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="With --closure, require zero residual debt for release eligibility.",
    )
    parser.add_argument("--update", action="store_true", help="Rewrite the budget with current counts.")
    parser.add_argument(
        "--accept-risk-changes",
        action="store_true",
        help="Explicitly approve new/changed time-bounded risk identities during --update.",
    )
    args = parser.parse_args(argv)

    root = Path(args.repo_root).resolve()
    budget_path = Path(args.budget_file)
    metrics = scan_repo(root)

    if args.update:
        previous = (
            cast(dict[str, Any], json.loads(budget_path.read_text(encoding="utf-8")))
            if budget_path.exists()
            else None
        )
        changes = risk_acceptance_changes(metrics, previous or {})
        if changes and previous is not None and not args.accept_risk_changes:
            print("Refusing to create or replace risk acceptances without --accept-risk-changes:")
            for change in changes:
                print(f"  - {change}")
            return 1
        budget = build_budget(metrics, previous=previous)
        budget_path.write_text(json.dumps(budget, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Updated maintainability budget: {budget_path}")
        return 0

    budget = load_budget(budget_path)
    ok, report = check_budget(metrics, budget, closure=args.closure)
    if args.strict and args.closure and not report["closure"]["release_eligible"]:
        ok = False
        report["ok"] = False
        report["failures"].append(
            {
                "type": "release_requires_zero_residual_debt",
                "current_count": report["closure"]["current_count"],
            }
        )
        report["summary"]["failure_count"] = len(report["failures"])
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        summary = report["summary"]
        print(
            "Maintainability budget: "
            f"large_files={summary['large_files']}, "
            f"broad_exception_files={summary['broad_exception_files']}, "
            f"broad_exceptions={summary['broad_exceptions']}, "
            f"unclassified_broad_exceptions={summary['unclassified_broad_exceptions']}, "
            "required_path_unclassified_broad_exceptions="
            f"{summary['required_path_unclassified_broad_exceptions']}, "
            f"failures={summary['failure_count']}, "
            f"improvements={summary['improvement_count']}"
        )
        if args.closure:
            closure_report = report["closure"]
            print(
                "Maintainability closure: "
                f"status={closure_report['status']}, "
                f"current={closure_report['current_count']}, "
                f"accepted={closure_report['accepted_count']}, "
                f"unaccepted={closure_report['unaccepted_count']}, "
                f"release_eligible={closure_report['release_eligible']}"
            )
        if report["failures"]:
            print("Failures:")
            for failure in report["failures"][:50]:
                print(f"  - {failure}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
