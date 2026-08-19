"""Command-line presentation for the Wiki Builder catch-up tool."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import logging
import sys
from typing import Any

from core.cli.periodic import (
    add_periodic_loop_args,
    resolve_max_cycles,
    run_periodic_loop,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WikiBuilderCliDependencies:
    """Operations supplied by the Wiki Builder façade."""

    create_storage_backend: Callable[[], Any]
    get_stats: Callable[[], dict[str, Any]]
    operation_errors: tuple[type[BaseException], ...]
    run_build_cycle: Callable[..., dict[str, Any]]


def main(dependencies: WikiBuilderCliDependencies) -> None:
    """Run the catch-up CLI without adding presentation logic to the builder."""

    parser = argparse.ArgumentParser(description="Wiki Builder - L1 to Wiki Markdown")
    parser.add_argument("--watch", action="store_true", help="守护模式，每5分钟执行")
    parser.add_argument("--dry-run", action="store_true", help="试运行，不写入")
    parser.add_argument("--stats", action="store_true", help="查看统计")
    add_periodic_loop_args(parser, default_interval=300)
    args = parser.parse_args()

    try:
        backend = dependencies.create_storage_backend()
    except dependencies.operation_errors as exc:
        logger.warning("ERROR: 无法创建 StorageBackend: %s", exc)
        sys.exit(1)

    if args.stats:
        stats = dependencies.get_stats()
        logger.info("\n=== Wiki Builder 统计 ===")
        for key, value in stats.items():
            logger.info("  %s: %s", key, value)
        return

    if args.watch:
        logger.info("[WikiBuilder] 守护模式启动 (pipeline=ON)")

        def _cycle() -> None:
            logger.info("\n=== %s ===", datetime.now().isoformat())
            stats = dependencies.run_build_cycle(backend, dry_run=args.dry_run)
            print(
                f"结果: processed={stats['processed']}, "
                f"incomplete={stats['skipped_incomplete']}, "
                f"low_q={stats['skipped_low_quality']}, "
                f"similar={stats['skipped_similar']}, "
                f"distill={stats['skipped_distill']}, "
                f"recirculation={stats['skipped_recirculation']}, "
                f"pipeline={stats['pipeline_used']}, "
                f"rule={stats['rule_used']}, "
                f"failed={stats['failed']}"
            )

        run_periodic_loop(
            _cycle,
            interval=args.interval,
            max_cycles=resolve_max_cycles(once=args.once, max_cycles=args.max_cycles),
            run_seconds=args.run_seconds,
        )
        return

    stats = dependencies.run_build_cycle(backend, dry_run=args.dry_run)
    logger.info("\n=== Wiki 构建完成 ===")
    logger.info("  已处理: %s", stats["processed"])
    logger.warning("  未完成: %s", stats["skipped_incomplete"])
    logger.warning("  质量跳过: %s", stats["skipped_low_quality"])
    logger.warning("  相似跳过: %s", stats["skipped_similar"])
    logger.warning(
        "  回流跳过: %s",
        stats["skipped_distill"] + stats["skipped_recirculation"],
    )
    logger.info("  流水线: %s", stats["pipeline_used"])
    logger.info("  规则级: %s", stats["rule_used"])
    logger.warning("  失败: %s", stats["failed"])


__all__ = ["WikiBuilderCliDependencies", "main"]
