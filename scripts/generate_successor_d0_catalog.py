#!/usr/bin/env python3
"""Preview or explicitly write the Mnemos cognitive-successor D0 catalog."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.successor_d0_catalog import (
    ARTIFACT_ORDER,
    CatalogInputError,
    CatalogRequest,
    SuccessorD0Catalog,
    sha256_bytes,
)

DEFAULT_DESIGN_PATH = Path(
    "docs/superpowers/specs/" "2026-08-01-cognitive-successor-capability-atomicity-design.md"
)
DEFAULT_PHASE_CONTRACT_PATH = (
    Path.home() / "Desktop" / "Mnemos-Phase0-7全局工程修复合同-2026-07-24.md"
)
DEFAULT_OUTPUT_DIR = Path("docs/acceptance/cognitive_successor_d0")
REPORT_SCHEMA = "mnemos.cognitive_successor_d0.generator_report.v1"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a fail-closed D0 catalog from an explicit immutable legacy "
            "commit. Preview is the default; files are written only with --write."
        )
    )
    parser.add_argument(
        "--legacy-commit",
        required=True,
        help=(
            "complete lowercase 40- or 64-hex Git commit object ID to archive "
            "and inventory; refs such as HEAD, branches, and tags are rejected"
        ),
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=ROOT,
        help=f"legacy Git repository root (default: {ROOT})",
    )
    parser.add_argument(
        "--design-path",
        type=Path,
        default=DEFAULT_DESIGN_PATH,
        help=(
            "exact current successor design file; relative paths are resolved "
            "against --repo-root"
        ),
    )
    parser.add_argument(
        "--phase-contract-path",
        type=Path,
        default=DEFAULT_PHASE_CONTRACT_PATH,
        help=(
            "exact current Phase 0-7 contract; relative paths are resolved " "against --repo-root"
        ),
    )
    parser.add_argument(
        "--config-snapshot",
        type=Path,
        help=(
            "optional opaque exact config snapshot; relative paths are resolved "
            "against --repo-root and no default production config is read"
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=(
            "bundle destination used only with --write; relative paths are resolved "
            "against --repo-root"
        ),
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help=(
            "publish each canonical JSONL file atomically and manifest.json last; "
            "verification rejects interrupted mixed generations"
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit a JSON report")
    return parser


def _resolve_output(repo_root: Path, output_dir: Path) -> Path:
    expanded = output_dir.expanduser()
    if expanded.is_absolute():
        return expanded
    return repo_root / expanded


def _report(
    *,
    bundle: Any,
    output_dir: Path,
    write_requested: bool,
    written_paths: Sequence[Path],
) -> dict[str, Any]:
    manifest = bundle.manifest
    findings = manifest.get("findings", [])
    return {
        "schema_version": REPORT_SCHEMA,
        "ok": not bundle.blocked,
        "mode": "write" if write_requested else "preview",
        "bundle_status": manifest.get("bundle_status"),
        "release_eligible": False,
        "denominator_frozen": False,
        "denominator_approved": False,
        "output_dir": str(output_dir),
        "written_paths": [str(path) for path in written_paths],
        "artifact_order": list(ARTIFACT_ORDER),
        "artifacts": manifest.get("artifacts"),
        "manifest_sha256": sha256_bytes(bundle.artifacts["manifest.json"]),
        "legacy_snapshot": manifest.get("legacy_snapshot"),
        "config_snapshot": manifest.get("config_snapshot"),
        "source_bindings": manifest.get("source_bindings"),
        "generator_identity": manifest.get("generator_identity"),
        "inventory_metrics": manifest.get("inventory_metrics"),
        "closure": manifest.get("closure"),
        "finding_counts": {
            "blocking": sum(
                isinstance(item, dict) and item.get("severity") == "BLOCKING" for item in findings
            ),
            "warning": sum(
                isinstance(item, dict) and item.get("severity") == "WARNING" for item in findings
            ),
        },
        "findings": findings,
    }


def _print_human(report: dict[str, Any]) -> None:
    artifacts = report.get("artifacts") or []
    counts = ", ".join(
        f"{item.get('artifact_id')}={item.get('record_count')}"
        for item in artifacts
        if isinstance(item, dict)
    )
    print(f"{report['bundle_status']} {report['mode']}; " f"manifest={report['manifest_sha256']}")
    print(f"artifacts: {counts}")
    print(
        "closure: verification_pending=true, denominator_frozen=false, "
        "denominator_approved=false, release_eligible=false"
    )
    print(f"output: {report['output_dir']}")
    finding_counts = report["finding_counts"]
    print(
        f"findings: blocking={finding_counts['blocking']} " f"warning={finding_counts['warning']}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = args.repo_root.expanduser().resolve()
    output_dir = _resolve_output(repo_root, args.output_dir)
    request = CatalogRequest(
        repo_root=repo_root,
        legacy_commit=args.legacy_commit,
        design_path=args.design_path,
        phase_contract_path=args.phase_contract_path,
        config_snapshot=args.config_snapshot,
    )
    generator = SuccessorD0Catalog()
    try:
        bundle = generator.generate(request)
        written_paths = generator.write(bundle, output_dir) if args.write else ()
    except CatalogInputError as exc:
        report = {
            "schema_version": REPORT_SCHEMA,
            "ok": False,
            "mode": "write" if args.write else "preview",
            "bundle_status": "INPUT_ERROR",
            "release_eligible": False,
            "error": str(exc),
        }
        if args.json:
            print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
        else:
            print(f"INPUT_ERROR: {exc}", file=sys.stderr)
        return 2
    report = _report(
        bundle=bundle,
        output_dir=output_dir,
        write_requested=args.write,
        written_paths=written_paths,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
    else:
        _print_human(report)
    return 1 if bundle.blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
