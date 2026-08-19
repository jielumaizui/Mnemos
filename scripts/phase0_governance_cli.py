"""Command-line presentation for Phase 0/1 governance projections."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
import json
from pathlib import Path

from core.ops.exclusive_file_lock import exclusive_file_lock
from scripts.phase0_governance_constants import (
    FINDING_OWNERS,
    ROOT_ORDER,
    SCHEMA_VERSION,
)


@dataclass(frozen=True)
class GovernanceCliDependencies:
    """Generator operations supplied at the command seam."""

    governance_refresh_lock_path: Path
    validate_assets: Callable[..., list[str]]
    write_assets_transactionally: Callable[..., list[str]]


def main(dependencies: GovernanceCliDependencies) -> int:
    """Validate or transactionally publish the governance projection."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--desktop-mode",
        choices=("required", "skip"),
        default="required",
        help="require local Desktop contracts or validate the portable repo-only projection",
    )
    args = parser.parse_args()
    try:
        with exclusive_file_lock(
            dependencies.governance_refresh_lock_path,
            unavailable_message="phase1_governance_refresh_already_running",
        ):
            errors = (
                dependencies.write_assets_transactionally(
                    desktop_mode=args.desktop_mode,
                )
                if args.write
                else dependencies.validate_assets(desktop_mode=args.desktop_mode)
            )
    except (OSError, RuntimeError, ValueError) as exc:
        errors = [str(exc)]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "ok": not errors,
        "errors": errors,
        "root_count": len(ROOT_ORDER),
        "finding_count": len(FINDING_OWNERS),
        "interface_count": 13,
        "release_eligible": False,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            f"Phase 0 governance: ok={payload['ok']} "
            f"roots={payload['root_count']} findings={payload['finding_count']} "
            f"interfaces={payload['interface_count']}"
        )
        for error in errors:
            print(f"- {error}")
    return 0 if not errors else 1


__all__ = ["GovernanceCliDependencies", "main"]
