#!/usr/bin/env python3
"""Explicitly provision and migrate the runtime producer/consumer ledger."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.ops.producer_consumer_ledger import DEFAULT_MATRIX  # noqa: E402
from core.ops.runtime_flow_health import (  # noqa: E402
    bootstrap_runtime_producer_consumer_ledger,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    from core.config import get_config

    result = bootstrap_runtime_producer_consumer_ledger(
        get_config(),
        matrix_path=args.matrix,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            "Runtime producer/consumer ledger ready: "
            f"{result['registered_flows']} flows, "
            f"{result['migrated_legacy_receipts']} legacy receipts migrated, "
            f"{result['replayed_outbox_events']} outbox events replayed"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
