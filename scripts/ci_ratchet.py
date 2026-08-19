#!/usr/bin/env python3
"""CI architecture governance ratchet.

Compares the current repository state against a committed baseline and fails
when new architectural debt appears:

- new import cycles
- new forbidden core -> integrations edges
- new direct configuration reads (by category)
- new vulture whitelist entries

Use ``--update`` to refresh the baseline after intentional cleanups.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, cast

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = PROJECT_ROOT / "scripts" / "ci_ratchet_baseline.json"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"


def _load_script_module(name: str):
    """Load a sibling script module by its stem without relying on sys.path."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load script module: {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _rel(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


# ---------------------------------------------------------------------------
# Current-state collectors
# ---------------------------------------------------------------------------


def _collect_arch() -> Dict[str, Any]:
    arch = _load_script_module("arch_dependency_graph")
    graph = arch.build_graph()
    cycles = arch.find_cycles(graph)
    forbidden = arch.find_forbidden_imports(graph)
    return {
        "module_count": len(graph.module_nodes),
        "edge_count": len(graph.module_edges),
        "cycles": [sorted(c) for c in cycles],
        "forbidden_edges": [
            {"source": e.source, "target": e.target, "deferred": e.deferred}
            for e in sorted(forbidden, key=lambda x: (x.source, x.target, x.deferred))
        ],
    }


def _collect_audit() -> Dict[str, Any]:
    audit = _load_script_module("audit_config_reads")
    result = audit.audit()
    counts = audit._category_counts(result.findings)
    unclassified = [
        {
            "file": _rel(f.file),
            "line": f.line,
            "col": f.col + 1,
            "kind": f.kind,
            "snippet": f.snippet,
        }
        for f in sorted(
            (f for f in result.findings if f.category == "unclassified"),
            key=lambda x: (str(x.file), x.line),
        )
    ]
    return {
        "category_counts": dict(sorted(counts.items())),
        "unclassified_findings": unclassified,
    }


def _collect_vulture() -> Dict[str, Any]:
    vulture_audit = _load_script_module("audit_vulture_whitelist")
    entries = vulture_audit.parse_whitelist(vulture_audit.DEFAULT_WHITELIST)
    structured = []
    for entry in entries:
        if "symbol" not in entry or "kind" not in entry or "path" not in entry:
            continue
        structured.append(
            {
                "symbol": entry["symbol"],
                "kind": entry["kind"],
                "path": _rel(Path(entry["path"])),
            }
        )
    return {
        "entry_count": len(structured),
        "entries": sorted(structured, key=lambda x: (x["path"], x["kind"], x["symbol"])),
    }


def collect_vulture_scan(*, min_confidence: int = 80) -> Dict[str, Any]:
    """Run the same live dead-code denominator as CI, without whitelist credit."""
    command = [
        sys.executable,
        "-m",
        "vulture",
        "--min-confidence",
        str(min_confidence),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {
            "status": "scan_unavailable",
            "ok": False,
            "exit_code": -1,
            "finding_count": 0,
            "finding_hashes": [],
        }
    findings = sorted(
        {
            line.strip()
            for line in (completed.stdout + "\n" + completed.stderr).splitlines()
            if line.strip()
        }
    )
    finding_hashes = [
        "sha256:" + hashlib.sha256(line.encode("utf-8")).hexdigest()
        for line in findings
    ]
    ok = completed.returncode == 0 and not findings
    return {
        "status": "zero_dead_code" if ok else "live_dead_code",
        "ok": ok,
        "exit_code": int(completed.returncode),
        "finding_count": len(findings),
        "finding_hashes": finding_hashes,
    }


def compute_current_state() -> Dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "arch_dependency_graph": _collect_arch(),
        "audit_config_reads": _collect_audit(),
        "vulture_whitelist": _collect_vulture(),
    }


# ---------------------------------------------------------------------------
# Baseline I/O
# ---------------------------------------------------------------------------


def load_baseline(path: Path = BASELINE_PATH) -> Dict[str, Any]:
    if not path.exists():
        return {}
    return cast(Dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def save_baseline(state: Dict[str, Any], path: Path = BASELINE_PATH) -> None:
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Comparison helpers
# ---------------------------------------------------------------------------


def _cycle_key(cycle: List[str]) -> Tuple[str, ...]:
    return tuple(sorted(cycle))


def _forbidden_key(edge: Dict[str, Any]) -> Tuple[str, str, bool]:
    return (edge["source"], edge["target"], edge.get("deferred", False))


def _vulture_key(entry: Dict[str, Any]) -> Tuple[str, str, str]:
    return (entry["path"], entry["kind"], entry["symbol"])


def _compare_cycles(baseline: Dict[str, Any], current: Dict[str, Any]) -> List[str]:
    base_cycles = {_cycle_key(c) for c in baseline.get("cycles", [])}
    curr_cycles = {_cycle_key(c) for c in current.get("cycles", [])}
    return [" -> ".join(c) for c in sorted(curr_cycles - base_cycles)]


def _compare_forbidden_edges(
    baseline: Dict[str, Any], current: Dict[str, Any]
) -> List[str]:
    base_edges = {_forbidden_key(e) for e in baseline.get("forbidden_edges", [])}
    curr_edges = {_forbidden_key(e) for e in current.get("forbidden_edges", [])}
    return [
        f"{e[0]} -> {e[1]} (deferred={e[2]})"
        for e in sorted(curr_edges - base_edges)
    ]


def _compare_audit_counts(
    baseline: Dict[str, Any], current: Dict[str, Any]
) -> List[str]:
    base_counts = baseline.get("category_counts", {})
    curr_counts = current.get("category_counts", {})
    regressions: List[str] = []
    for category, count in sorted(curr_counts.items()):
        base = base_counts.get(category, 0)
        if count > base:
            regressions.append(
                f"{category}: {base} -> {count} (+{count - base})"
            )
    return regressions


def _compare_vulture_entries(
    baseline: Dict[str, Any], current: Dict[str, Any]
) -> List[str]:
    base_entries = {_vulture_key(e) for e in baseline.get("entries", [])}
    curr_entries = {_vulture_key(e) for e in current.get("entries", [])}
    return [
        f"{e[0]}:{e[1]} {e[2]}"
        for e in sorted(curr_entries - base_entries)
    ]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def check(baseline: Dict[str, Any], current: Dict[str, Any]) -> Tuple[bool, List[str]]:
    regressions: List[str] = []

    arch_base = baseline.get("arch_dependency_graph", {})
    arch_curr = current.get("arch_dependency_graph", {})
    new_cycles = _compare_cycles(arch_base, arch_curr)
    new_edges = _compare_forbidden_edges(arch_base, arch_curr)
    if new_cycles:
        regressions.append("New import cycles:")
        regressions.extend(f"  - {c}" for c in new_cycles)
    if new_edges:
        regressions.append("New forbidden import edges:")
        regressions.extend(f"  - {e}" for e in new_edges)

    audit_base = baseline.get("audit_config_reads", {})
    audit_curr = current.get("audit_config_reads", {})
    audit_regressions = _compare_audit_counts(audit_base, audit_curr)
    if audit_regressions:
        regressions.append("New direct config reads:")
        regressions.extend(f"  - {r}" for r in audit_regressions)

    vulture_base = baseline.get("vulture_whitelist", {})
    vulture_curr = current.get("vulture_whitelist", {})
    new_whitelist = _compare_vulture_entries(vulture_base, vulture_curr)
    if new_whitelist:
        regressions.append("New vulture whitelist entries:")
        regressions.extend(f"  - {e}" for e in new_whitelist)

    return not regressions, regressions


def build_closure_report(
    baseline: Dict[str, Any],
    current: Dict[str, Any],
    *,
    vulture_scan: Dict[str, Any],
) -> Dict[str, Any]:
    ratchet_ok, regressions = check(baseline, current)
    baseline_vulture = baseline.get("vulture_whitelist", {})
    current_vulture = current.get("vulture_whitelist", {})
    baseline_entries = baseline_vulture.get("entries", [])
    current_entries = current_vulture.get("entries", [])
    baseline_count = int(baseline_vulture.get("entry_count", len(baseline_entries)))
    current_count = int(current_vulture.get("entry_count", len(current_entries)))
    closure_errors: List[str] = []
    if baseline_count != len(baseline_entries):
        closure_errors.append(
            "Vulture baseline entry_count does not match its exact entries denominator."
        )
    if current_count != len(current_entries):
        closure_errors.append(
            "Vulture current entry_count does not match its exact entries denominator."
        )
    if baseline_count != current_count or baseline_entries != current_entries:
        closure_errors.append(
            "Vulture baseline is not tightly equal to the current whitelist; improvements must be locked."
        )
    if current_count:
        closure_errors.append(
            f"Vulture closure requires zero whitelist entries; current_count={current_count}."
        )
    finding_hashes = vulture_scan.get("finding_hashes")
    finding_count = vulture_scan.get("finding_count")
    if (
        vulture_scan.get("ok") is not True
        or vulture_scan.get("status") != "zero_dead_code"
        or vulture_scan.get("exit_code") != 0
        or not isinstance(finding_hashes, list)
        or not isinstance(finding_count, int)
        or finding_count != len(finding_hashes)
        or any(
            not isinstance(value, str) or not value.startswith("sha256:")
            for value in finding_hashes
        )
    ):
        closure_errors.append(
            "Vulture scan reported "
            f"{finding_count if isinstance(finding_count, int) else 'unknown'} "
            "live dead-code findings or an invalid scan result."
        )
    ok = ratchet_ok and not closure_errors
    return {
        "schema_version": "mnemos.ci_ratchet_closure.v2",
        "ok": ok,
        "ratchet_status": "passed" if ratchet_ok else "failed",
        "closure": {
            "status": "zero_debt" if ok else "failed",
            "closure_target": 0,
            "current_count": current_count,
            "baseline_count": baseline_count,
            "release_eligible": ok,
        },
        "vulture_scan": dict(vulture_scan),
        "regressions": regressions,
        "closure_errors": closure_errors,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        default=True,
        help="Compare current state to the baseline (default).",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Recompute and overwrite the committed baseline.",
    )
    parser.add_argument(
        "--accept-risk-changes",
        action="store_true",
        help="Explicitly approve architectural/config ratchet regressions during --update.",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=BASELINE_PATH,
        help=f"Baseline JSON path (default: {BASELINE_PATH}).",
    )
    parser.add_argument("--closure", action="store_true", help="Require a tight zero vulture baseline.")
    parser.add_argument("--strict", action="store_true", help="Require release-eligible closure semantics.")
    parser.add_argument("--json", action="store_true", help="Print a machine-readable report.")
    args = parser.parse_args(argv)

    current = compute_current_state()

    if args.update:
        vulture_scan = collect_vulture_scan()
        if vulture_scan.get("ok") is not True:
            print(
                "Refusing to baseline while the live Vulture scan is not zero "
                f"(finding_count={vulture_scan.get('finding_count')})."
            )
            return 1
        vulture_count = int(current.get("vulture_whitelist", {}).get("entry_count", 0))
        if vulture_count:
            print(
                "Refusing to baseline non-zero vulture debt; remove whitelist entries first "
                f"(current_count={vulture_count})."
            )
            return 1
        previous = load_baseline(args.baseline)
        if previous:
            current_ok, regressions = check(previous, current)
            if not current_ok and not args.accept_risk_changes:
                print("Refusing to absorb CI ratchet regressions without --accept-risk-changes:")
                for regression in regressions:
                    print(regression)
                return 1
        save_baseline(current, args.baseline)
        print(f"Baseline updated: {args.baseline}")
        return 0

    baseline = load_baseline(args.baseline)
    if not baseline:
        print(f"No baseline found at {args.baseline}; run with --update to create one.")
        return 1

    if args.closure:
        report = build_closure_report(
            baseline,
            current,
            vulture_scan=collect_vulture_scan(),
        )
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        elif report["ok"]:
            print(
                "CI ratchet closure passed: vulture current=0, baseline=0, "
                "release_eligible=true."
            )
        else:
            print("CI ratchet closure failed:")
            for line in [*report["regressions"], *report["closure_errors"]]:
                print(f"  - {line}")
        return 0 if report["ok"] else 1

    ok, regressions = check(baseline, current)
    if ok:
        print("CI ratchet check passed. No new architectural debt detected.")
        return 0

    print("CI ratchet check failed. New architectural debt detected:")
    for line in regressions:
        print(line)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
