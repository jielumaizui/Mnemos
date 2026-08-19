#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Plan Mnemos cognitive consolidation without deleting details by default."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.cognitive.consolidator import (  # noqa: E402
    CognitiveConsolidationOptions,
    CognitiveConsolidator,
    dumps_report,
    format_consolidation_plan,
)
from core.config import get_config  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="冻结候选计划；不会写 Wiki、coverage 或删除 Raw",
    )
    parser.add_argument(
        "--reconcile-run",
        default=None,
        help="核验一个已冻结计划的可信页面提交和投影 receipts",
    )
    parser.add_argument(
        "--trusted-proposal-id",
        default="",
        help="与 --reconcile-run 一起提供的已提交 trusted-push proposal ID",
    )
    parser.add_argument(
        "--submit-run",
        default=None,
        help="将冻结页面的精确字节提交到 trusted-push（只创建 proposal）",
    )
    parser.add_argument(
        "--record-run",
        action="store_true",
        help="dry-run 时也初始化 cognitive_consolidation.db 并记录本次计划结果",
    )
    parser.add_argument(
        "--purge-raw",
        action="store_true",
        help="在 method page 合格且 --apply 时小批量物理清理 raw",
    )
    parser.add_argument("--method-page", default=None, help="压缩产物方法论页路径")
    parser.add_argument(
        "--generate-method",
        action="store_true",
        help="请求生成方法论页；CLI 默认无 LLM callback，仅报告缺失",
    )
    parser.add_argument("--method-output", default=None, help="生成方法论页的目标路径")
    parser.add_argument("--candidate-limit", type=int, default=None, help="候选样本上限")
    parser.add_argument("--raw-purge-limit", type=int, default=None, help="raw purge 上限")
    parser.add_argument(
        "--refresh-survival",
        action="store_true",
        help="计划前刷新 raw survival scores",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = get_config()
    options = CognitiveConsolidationOptions.from_config(cfg)
    consolidator = CognitiveConsolidator(options=options, config=cfg)
    try:
        if args.submit_run:
            report = consolidator.submit_frozen_page(args.submit_run)
            report["recorded_run"] = False
        elif args.reconcile_run:
            report = consolidator.reconcile_coverage(
                args.reconcile_run,
                trusted_proposal_id=args.trusted_proposal_id,
            )
            report["recorded_run"] = False
        else:
            report = consolidator.plan(
                apply=bool(args.apply),
                purge_raw=bool(args.purge_raw),
                method_page=args.method_page,
                generate_method=bool(args.generate_method),
                method_output=args.method_output,
                candidate_limit=args.candidate_limit,
                raw_purge_limit=args.raw_purge_limit,
                refresh_survival=bool(args.refresh_survival),
            )
            report["recorded_run"] = bool(args.apply or args.record_run)
            if args.record_run and not args.apply:
                consolidator.record_run(report)
    finally:
        consolidator.close()
    if args.json:
        print(dumps_report(report))
    else:
        print(format_consolidation_plan(report))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
