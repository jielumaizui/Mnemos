#!/usr/bin/env python3
"""Run the deterministic Mnemos golden benchmark."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.benchmarks.golden import (  # noqa: E402
    DEFAULT_BASELINE_PATH,
    DEFAULT_MANIFEST_PATH,
    scorecard_summary,
    run_golden_benchmark,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--mock-llm", action="store_true", help="use deterministic providers")
    parser.add_argument("--output-dir", type=Path, help="write run artifacts to this directory")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE_PATH)
    parser.add_argument(
        "--update-baseline-deny",
        action="store_true",
        help="update committed baseline only when current run has no regression",
    )
    parser.add_argument("--json", action="store_true", help="emit full JSON scorecard")
    args = parser.parse_args(argv)

    try:
        scorecard = run_golden_benchmark(
            manifest_path=args.manifest,
            baseline_path=args.baseline,
            output_dir=args.output_dir,
            strict=args.strict,
            mock_llm=args.mock_llm,
            update_baseline_deny=args.update_baseline_deny,
        )
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"Golden benchmark failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(scorecard, ensure_ascii=False, indent=2))
    else:
        summary = scorecard_summary(scorecard)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if scorecard.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
