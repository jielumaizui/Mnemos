#!/usr/bin/env python3
"""Check that legacy/compatibility code is explicitly accounted for.

The gate scans function/class names, docstrings, and comments for legacy or
compatibility markers. Every finding must be listed in
scripts/zombie_code_baseline.json with owner/reason/callers/remove_when/expires_at
and executable telemetry metadata.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CheckableNode = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
DEFAULT_SCAN_PATHS = (
    "core",
    "integrations",
    "daemon",
    "scripts",
    "mnemos_cli.py",
    "mnemos_daemon.py",
)
DEFAULT_BASELINE = PROJECT_ROOT / "scripts" / "zombie_code_baseline.json"
SCHEMA_VERSION = "mnemos.zombie_code_baseline.v2"
REPORT_SCHEMA_VERSION = "mnemos.zombie_code_closure.v1"
SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}
MARKERS = (
    "legacy",
    "向后兼容",
    "兼容旧",
    "旧入口",
    "旧链路",
    "旧格式",
    "已废弃",
    "保留兼容",
)
DEFAULT_EXPIRY_DAYS = 90


@dataclass(frozen=True)
class ZombieFinding:
    """One function/class level compatibility candidate."""

    path: str
    qualified_name: str
    kind: str
    line: int
    markers: Tuple[str, ...]

    @property
    def key(self) -> Tuple[str, str, str]:
        return (self.path, self.qualified_name, self.kind)

    def to_baseline_entry(self, metadata: Mapping[str, Any]) -> Dict[str, Any]:
        entry = {
            "path": self.path,
            "qualified_name": self.qualified_name,
            "kind": self.kind,
            "line": self.line,
            "markers": list(self.markers),
        }
        entry.update(metadata)
        return entry


def _iter_python_files(
    paths: Iterable[Path],
    project_root: Path = PROJECT_ROOT,
) -> Iterable[Path]:
    seen = set()
    self_path = Path(__file__).resolve()
    for raw_path in paths:
        path = raw_path if raw_path.is_absolute() else project_root / raw_path
        if path.is_file():
            candidates = [path]
        elif path.is_dir():
            candidates = sorted(path.rglob("*.py"))
        else:
            continue

        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved == self_path:
                continue
            if any(part in SKIP_DIRS for part in resolved.parts):
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            yield resolved


def _relative_path(path: Path, project_root: Path) -> str:
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return path.name


def _marker_in(text: str) -> Tuple[str, ...]:
    lowered = text.casefold()
    return tuple(marker for marker in MARKERS if marker.casefold() in lowered)


def _comment_fragments(lines: Sequence[str], start: int, end: int) -> List[str]:
    comments: List[str] = []
    for line in lines[start - 1:end]:
        stripped = line.strip()
        if stripped.startswith("#"):
            comments.append(stripped)
        elif "#" in line:
            comments.append(line.split("#", 1)[1].strip())
    return comments


class _ZombieVisitor(ast.NodeVisitor):
    def __init__(self, path: Path, project_root: Path, lines: Sequence[str]):
        self.rel_path = _relative_path(path, project_root)
        self.lines = lines
        self.scope: List[str] = []
        self.findings: List[ZombieFinding] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self._check_node(node, kind="class", include_comments=False)
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._check_node(node, kind="function", include_comments=True)
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._check_node(node, kind="async_function", include_comments=True)
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def _check_node(
        self,
        node: CheckableNode,
        *,
        kind: str,
        include_comments: bool,
    ) -> None:
        name = getattr(node, "name")
        pieces = [name]
        docstring = ast.get_docstring(node, clean=False)
        if docstring:
            pieces.append(docstring)
        if include_comments:
            end_lineno = node.end_lineno or node.lineno
            pieces.extend(_comment_fragments(self.lines, node.lineno, end_lineno))

        markers = _marker_in("\n".join(pieces))
        if not markers:
            return

        qualified_name = ".".join([*self.scope, name])
        self.findings.append(
            ZombieFinding(
                path=self.rel_path,
                qualified_name=qualified_name,
                kind=kind,
                line=node.lineno,
                markers=markers,
            )
        )


def scan_file(path: Path, project_root: Path = PROJECT_ROOT) -> List[ZombieFinding]:
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    lines = text.splitlines()
    visitor = _ZombieVisitor(path, project_root, lines)
    visitor.visit(tree)
    return visitor.findings


def scan_project(
    paths: Iterable[Path] | None = None,
    project_root: Path = PROJECT_ROOT,
) -> List[ZombieFinding]:
    scan_paths = paths or (Path(p) for p in DEFAULT_SCAN_PATHS)
    findings: List[ZombieFinding] = []
    for path in _iter_python_files(scan_paths, project_root):
        findings.extend(scan_file(path, project_root))
    return sorted(findings, key=lambda item: item.key)


def _load_baseline(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Baseline must be a JSON object: {path}")
    return data


def _baseline_index(
    baseline: Mapping[str, Any],
) -> Tuple[Dict[Tuple[str, str, str], Mapping[str, Any]], List[str]]:
    errors: List[str] = []
    index: Dict[Tuple[str, str, str], Mapping[str, Any]] = {}
    if baseline.get("schema_version") != SCHEMA_VERSION:
        errors.append(
            "Baseline schema_version must be "
            f"{SCHEMA_VERSION!r}, got {baseline.get('schema_version')!r}."
        )

    entries = baseline.get("entries")
    if not isinstance(entries, list):
        return index, [*errors, "Baseline entries must be a list."]

    for offset, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            errors.append(f"Baseline entry #{offset} must be an object.")
            continue
        key = (
            str(entry.get("path", "")),
            str(entry.get("qualified_name", "")),
            str(entry.get("kind", "")),
        )
        if not all(key):
            errors.append(f"Baseline entry #{offset} is missing path/name/kind.")
            continue
        if key in index:
            errors.append(f"Duplicate baseline entry: {key!r}.")
        index[key] = entry
        errors.extend(_policy_metadata_errors(entry, key))
    return index, errors


def _policy_metadata_errors(
    entry: Mapping[str, Any],
    key: Tuple[str, str, str],
) -> List[str]:
    errors: List[str] = []
    for field in ("owner", "reason", "remove_when", "telemetry"):
        value = entry.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"Baseline entry {key!r} needs non-empty {field}.")

    expires_at = entry.get("expires_at")
    if not isinstance(expires_at, str) or not expires_at.strip():
        errors.append(f"Baseline entry {key!r} needs non-empty expires_at.")
    else:
        try:
            expiry = date.fromisoformat(expires_at)
        except ValueError:
            errors.append(
                f"Baseline entry {key!r} has invalid expires_at date {expires_at!r}."
            )
        else:
            if expiry < date.today():
                errors.append(
                    f"Baseline entry {key!r} expired on {expires_at}; resolve or renew owner plan."
                )

    callers = entry.get("callers")
    if not isinstance(callers, list) or not callers:
        errors.append(f"Baseline entry {key!r} needs non-empty callers list.")
    elif not all(isinstance(caller, str) and caller.strip() for caller in callers):
        errors.append(f"Baseline entry {key!r} has invalid callers list.")
    return errors


def _default_owner(path: str) -> str:
    if path.startswith("core/kia/"):
        return "kia"
    if path.startswith("core/hephaestus"):
        return "hephaestus"
    if path.startswith("core/sync_framework/"):
        return "sync"
    if path.startswith("core/scoring/"):
        return "scoring"
    if path.startswith("daemon/") or path == "mnemos_daemon.py":
        return "daemon"
    if path.startswith("integrations/"):
        return "integrations"
    if path.startswith("scripts/"):
        return "ops"
    return "core"


def _default_expires_at() -> str:
    return (date.today() + timedelta(days=DEFAULT_EXPIRY_DAYS)).isoformat()


def check_baseline(
    findings: Sequence[ZombieFinding],
    baseline: Mapping[str, Any],
) -> List[str]:
    finding_index = {finding.key: finding for finding in findings}
    baseline_index, errors = _baseline_index(baseline)

    for finding in findings:
        if finding.key not in baseline_index:
            errors.append(
                "Undocumented zombie-code candidate: "
                f"{finding.path}:{finding.line} {finding.qualified_name} "
                f"({finding.kind}, markers={','.join(finding.markers)})."
            )

    for key in baseline_index:
        if key not in finding_index:
            errors.append(f"Stale zombie-code baseline entry: {key!r}.")

    return errors


def _default_policy_metadata(finding: ZombieFinding) -> Dict[str, Any]:
    return {
        "owner": _default_owner(finding.path),
        "reason": (
            "Compatibility behavior is intentionally retained while current "
            "callers or persisted data migrate."
        ),
        "callers": [
            "documented compatibility path",
        ],
        "remove_when": (
            "Remove after compatibility callers and persisted legacy data are "
            "retired."
        ),
        "expires_at": _default_expires_at(),
        "telemetry": "python3 scripts/check_zombie_code_policy.py --closure --json",
    }


def write_baseline(findings: Sequence[ZombieFinding], path: Path) -> None:
    existing: Dict[Tuple[str, str, str], Mapping[str, Any]] = {}
    if path.exists():
        baseline = _load_baseline(path)
        existing, _ = _baseline_index(baseline)

    entries = []
    for finding in findings:
        old_entry = existing.get(finding.key, {})
        defaults = _default_policy_metadata(finding)
        metadata = {
            field: old_entry.get(field) or defaults[field]
            for field in (
                "owner",
                "reason",
                "callers",
                "remove_when",
                "expires_at",
                "telemetry",
            )
        }
        if _policy_metadata_errors(metadata, finding.key):
            metadata = defaults
        entries.append(finding.to_baseline_entry(metadata))

    data = {
        "schema_version": SCHEMA_VERSION,
        "description": (
            "Function/class level legacy or compatibility candidates allowed "
            "by the No Zombie Code Policy gate."
        ),
        "entries": entries,
    }
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _print_errors(errors: Sequence[str]) -> None:
    print(f"FAIL: found {len(errors)} No Zombie Code Policy problem(s).")
    for error in errors:
        print(f"  - {error}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check No Zombie Code Policy")
    parser.add_argument(
        "paths",
        nargs="*",
        help="Files or directories to scan. Defaults to project source paths.",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE,
        help="Path to zombie-code baseline JSON.",
    )
    parser.add_argument(
        "--update",
        "--write-baseline",
        action="store_true",
        help="Write/update the baseline from current findings.",
    )
    parser.add_argument(
        "--accept-new-risk",
        action="store_true",
        help="Explicitly approve new time-bounded compatibility candidates during --update.",
    )
    parser.add_argument("--closure", action="store_true", help="Report residual accepted debt.")
    parser.add_argument("--json", action="store_true", help="Print a machine-readable report.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="With --closure, require zero residual candidates for release eligibility.",
    )
    args = parser.parse_args(argv)

    paths = [Path(path) for path in args.paths] if args.paths else None
    findings = scan_project(paths)

    if args.update:
        existing_keys: set[Tuple[str, str, str]] = set()
        if args.baseline.exists():
            existing_keys = set(_baseline_index(_load_baseline(args.baseline))[0])
        new_findings = [finding for finding in findings if finding.key not in existing_keys]
        if new_findings and not args.accept_new_risk:
            print("Refusing to create new zombie-code acceptances without --accept-new-risk:")
            for finding in new_findings:
                print(f"  - {finding.path}:{finding.line} {finding.qualified_name}")
            return 1
        write_baseline(findings, args.baseline)
        print(f"Wrote {len(findings)} zombie-code baseline entries.")
        return 0

    if not args.baseline.exists():
        print(f"FAIL: missing zombie-code baseline: {args.baseline}")
        return 1

    errors = check_baseline(findings, _load_baseline(args.baseline))
    ratchet_ok = not errors
    release_eligible = not findings and ratchet_ok
    closure_status = "not_requested"
    if args.closure:
        closure_status = (
            "zero_debt"
            if release_eligible
            else "accepted_debt" if ratchet_ok else "failed"
        )
    if args.strict and args.closure and findings:
        errors.append(
            f"Release requires zero zombie-code candidates; current_count={len(findings)}."
        )
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "ok": not errors,
        "ratchet_status": "passed" if ratchet_ok else "failed",
        "closure": {
            "requested": args.closure,
            "status": closure_status,
            "closure_target": 0,
            "current_count": len(findings),
            "accepted_count": len(findings) if ratchet_ok else 0,
            "unaccepted_count": 0 if ratchet_ok else len(findings),
            "release_eligible": release_eligible,
        },
        "errors": errors,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif errors:
        _print_errors(errors)
    else:
        print(
            f"OK: {len(findings)} zombie-code candidate(s) documented with "
            "owner/expiry/telemetry plans."
        )
        if args.closure:
            print(
                "Zombie closure: "
                f"status={closure_status}, current={len(findings)}, "
                f"accepted={len(findings)}, release_eligible={release_eligible}"
            )
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
