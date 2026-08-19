"""Command-line presentation for the Amphora distillation queue."""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AmphoraCliDependencies:
    """Queue operations supplied by the storage façade at the CLI boundary."""

    cleanup_old: Callable[..., int]
    get_next: Callable[..., dict[str, Any] | None]
    get_task_count: Callable[..., int]
    list_pending: Callable[..., list[dict[str, Any]]]
    mark_done: Callable[..., bool]
    mark_failed: Callable[..., bool]
    mark_intentional_skip: Callable[..., bool]


def main(dependencies: AmphoraCliDependencies) -> None:
    """Render queue operations without adding presentation code to the store."""
    parser = argparse.ArgumentParser(description="Distillation Queue Manager")
    parser.add_argument("--list", action="store_true", help="列出待处理任务")
    parser.add_argument("--next", action="store_true", help="获取下一个任务")
    parser.add_argument("--done", metavar="SESSION_ID", help="标记任务完成")
    parser.add_argument("--fail", metavar="SESSION_ID", help="标记任务失败")
    parser.add_argument("--output", default=None, help="完成时的输出文件路径")
    parser.add_argument("--skip-reason", default=None, help="显式无产物终态原因")
    parser.add_argument("--error", default=None, help="失败时的错误信息")
    parser.add_argument("--cleanup", action="store_true", help="清理旧任务")
    parser.add_argument("--stats", action="store_true", help="队列统计")
    args = parser.parse_args()

    if args.list:
        pending = dependencies.list_pending()
        if pending:
            logger.info("待蒸馏任务: %s", len(pending))
            for task in pending:
                meta_value = task.get("meta")
                meta = meta_value if isinstance(meta_value, dict) else {}
                print(
                    f"  - {task['session_id'][:16]}... | "
                    f"消息: {len(task.get('messages', []))} | "
                    f"来源: {meta.get('source', 'unknown')} | "
                    f"优先级: {task.get('priority', 0)} | "
                    f"创建: {task['created_at'][:19]}"
                )
        else:
            logger.info("无待蒸馏任务")
        return

    if args.next:
        next_task = dependencies.get_next()
        logger.info(json.dumps(next_task or {}, ensure_ascii=False, indent=2))
        return

    if args.done:
        success = (
            dependencies.mark_intentional_skip(args.done, args.skip_reason)
            if args.skip_reason
            else dependencies.mark_done(args.done, args.output)
        )
        logger.info("%s: %s", "已标记完成" if success else "任务不存在", args.done)
        return

    if args.fail:
        success = dependencies.mark_failed(args.fail, args.error or "unknown")
        logger.warning("%s: %s", "已标记失败" if success else "任务不存在", args.fail)
        return

    if args.cleanup:
        archived = dependencies.cleanup_old()
        logger.info("清理完成: 归档 %s 个旧任务", archived)
        return

    if args.stats:
        total = dependencies.get_task_count()
        pending_count = len(
            dependencies.list_pending(include_future_retry=True)
        )
        processing = dependencies.get_task_count("processing")
        committed = dependencies.get_task_count("committed")
        intentional_skip = dependencies.get_task_count(
            "intentional_skip"
        )
        proposal_pending = dependencies.get_task_count("proposal_pending")
        failed = dependencies.get_task_count("failed")
        archived = dependencies.get_task_count("archived")
        print(
            "队列统计: "
            f"总计={total}, 待处理={pending_count}, 处理中={processing}, "
            f"已提交={committed}, 显式跳过={intentional_skip}, "
            f"提案待决={proposal_pending}, 失败={failed}, 归档={archived}"
        )
        return

    parser.print_help()
