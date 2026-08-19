#!/usr/bin/env python3
"""Audit the adaptive policy coverage matrix."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.kia.adaptive_policy_matrix import build_adaptive_policy_report  # noqa: E402

DOC_PATH = PROJECT_ROOT / "docs" / "acceptance" / "adaptive_policy_matrix.json"


def _load_doc_report() -> tuple[dict | None, str | None]:
    if not DOC_PATH.exists():
        return None, f"missing docs artifact: {DOC_PATH.relative_to(PROJECT_ROOT)}"
    try:
        return json.loads(DOC_PATH.read_text(encoding="utf-8")), None
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"invalid docs artifact: {exc}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Output machine-readable report")
    parser.add_argument("--strict", action="store_true", help="Return non-zero on contract errors")
    parser.add_argument(
        "--write-doc",
        action="store_true",
        help="Rewrite docs/acceptance/adaptive_policy_matrix.json from code contracts",
    )
    args = parser.parse_args(argv)

    report = build_adaptive_policy_report()
    doc_error = None
    if args.write_doc:
        DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
        DOC_PATH.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    elif args.strict:
        doc_report, doc_error = _load_doc_report()
        if doc_error is None and doc_report != report:
            doc_error = (
                "stale docs artifact: run "
                "python3 scripts/audit_adaptive_policy_matrix.py --write-doc"
            )

    if args.json:
        output = dict(report)
        if doc_error:
            output["doc_error"] = doc_error
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        status = "ok" if report["ok"] else "failed"
        print(f"adaptive policy coverage: {status}")
        print(f"schema: {report['schema_version']}")
        print(f"rules: {report['rule_count']} coverage_rows: {report['coverage_count']}")
        print(f"domains: {', '.join(report['domains'])}")
        for error in report["errors"]:
            print(f"- {error}")
        if doc_error:
            print(f"- {doc_error}")

    if args.strict and (not report["ok"] or doc_error):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
