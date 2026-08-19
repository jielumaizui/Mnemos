#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verify roadmap step 1 acceptance contracts and sample matrix."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.agent_kit.acceptance_contracts import (
    build_agent_acceptance_samples,
    load_acceptance_manifest,
    validate_acceptance_manifest,
    validate_builtin_agent_capabilities,
    validate_contracts,
)


DEFAULT_MANIFEST = Path("tests/fixtures/agent_acceptance_samples/manifest.json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="Acceptance sample manifest to validate.",
    )
    parser.add_argument(
        "--emit-template",
        action="store_true",
        help="Print the generated manifest template and exit.",
    )
    args = parser.parse_args(argv)

    if args.emit_template:
        import json

        print(json.dumps(build_agent_acceptance_samples(), ensure_ascii=False, indent=2))
        return 0

    errors = []
    errors.extend(validate_contracts())
    if args.manifest.exists():
        manifest = load_acceptance_manifest(args.manifest)
        errors.extend(validate_acceptance_manifest(manifest))
    else:
        errors.append(f"manifest does not exist: {args.manifest}")
    errors.extend(validate_builtin_agent_capabilities())

    if errors:
        print("Acceptance contract verification failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    print(f"Acceptance contracts OK: {args.manifest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
