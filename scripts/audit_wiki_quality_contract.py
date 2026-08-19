#!/usr/bin/env python3
"""Audit the stable Wiki quality report contract."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.system_contracts import ACTION_TYPES, LIFECYCLE_MAPPINGS, WIKI_QUALITY_SCHEMA_VERSION
from scripts import wiki_lint


REQUIRED_ISSUE_TYPES = {"missing_meta", "orphan", "broken_link", "stub"}


def audit_wiki_quality_contract(*, strict: bool = False) -> list[str]:
    errors: list[str] = []
    sample_results = [
        {
            "page": "broken.md",
            "severity": "error",
            "issues": [{"type": "broken_link", "msg": "坏链接: [[missing]]"}],
        },
        {
            "page": "meta.md",
            "severity": "warning",
            "issues": [{"type": "missing_meta", "msg": "缺少 status"}],
        },
        {
            "page": "stub.md",
            "severity": "warning",
            "issues": [{"type": "stub", "msg": "内容过短"}],
        },
        {
            "page": "orphan.md",
            "severity": "warning",
            "issues": [{"type": "orphan", "msg": "无入链"}],
        },
    ]
    report = wiki_lint.build_quality_report(sample_results, vault_dir=ROOT / "wiki")
    if report.get("schema_version") != WIKI_QUALITY_SCHEMA_VERSION:
        errors.append("wiki quality schema version mismatch")
    if "wiki_quality_fix" not in ACTION_TYPES:
        errors.append("wiki_quality_fix action type is not registered")

    state_machine = report.get("state_machine") or {}
    missing_issue_types = REQUIRED_ISSUE_TYPES - set(state_machine)
    if missing_issue_types:
        errors.append(f"missing issue state mappings: {sorted(missing_issue_types)}")

    lifecycle_mapping = LIFECYCLE_MAPPINGS.get("wiki_quality")
    if not lifecycle_mapping:
        errors.append("wiki_quality lifecycle mapping is missing")
    else:
        unknown_local = {
            state["local_status"]
            for state in state_machine.values()
            if state["local_status"] not in lifecycle_mapping.local_statuses
        }
        if unknown_local:
            errors.append(f"unknown local statuses: {sorted(unknown_local)}")

    budget_lines = report.get("budgets", {}).get("lines", [])
    budget_by_type = {line["issue_type"]: line for line in budget_lines}
    for issue_type in REQUIRED_ISSUE_TYPES:
        line = budget_by_type.get(issue_type)
        if not line:
            errors.append(f"{issue_type}: missing budget line")
            continue
        if not line.get("owner") or not line.get("strategy"):
            errors.append(f"{issue_type}: owner and strategy required")
    if "broken_link" not in report.get("manual_review", {}):
        errors.append("broken_link must produce manual review queue")
    if report["state_machine"]["missing_meta"]["auto_fixable"] is not True:
        errors.append("missing_meta must remain auto-fixable")
    if strict and report["scorecard"]["dimension"] != "obsidian_experience":
        errors.append("wiki quality must map to obsidian_experience scorecard")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    errors = audit_wiki_quality_contract(strict=args.strict)
    payload = {
        "schema_version": WIKI_QUALITY_SCHEMA_VERSION,
        "ok": not errors,
        "errors": errors,
        "required_issue_types": sorted(REQUIRED_ISSUE_TYPES),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif errors:
        print("Wiki quality contract audit failed:")
        for error in errors:
            print(f"- {error}")
    else:
        print("Wiki quality contract audit passed")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
