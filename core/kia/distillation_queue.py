"""
Distillation Queue - 子 Agent 蒸馏任务队列管理

职责：
- 接收待蒸馏的 session 数据（JSON）
- 管理队列状态（pending / processing / done / failed）
- 提供 CLI 接口供 Agent 查询和处理

用法：
    python3 core/distillation_queue.py --list          # 列出待处理任务
    python3 core/distillation_queue.py --next          # 获取下一个任务并标记为 processing
    python3 core/distillation_queue.py --done {id}     # 标记任务完成
    python3 core/distillation_queue.py --fail {id}     # 标记任务失败
"""

import os
import sys
import json
import hashlib
import argparse
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional
import logging

logger = logging.getLogger(__name__)

def _queue_dir() -> Path:
    """获取 distill_queue 目录（可配置，默认跟随 Claude Code 数据目录）"""
    from core.config import get_config
    return get_config().claude_data_dir / "distill_queue"


def _ensure_dir():
    _queue_dir().mkdir(parents=True, exist_ok=True)


def _task_path(session_id: str) -> Path:
    """获取任务文件路径"""
    safe_id = hashlib.md5(session_id.encode()).hexdigest()[:12]
    return _queue_dir() / f"{safe_id}.json"


def enqueue(session_id: str, messages: List[Dict], meta: Dict = None) -> Path:
    """
    将 session 数据加入蒸馏队列

    Args:
        session_id: session 唯一标识
        messages: 消息列表 [{"role": "user", "content": "..."}, ...]
        meta: 元数据 {"source": "claude", "working_dir": "...", ...}

    Returns:
        任务文件路径
    """
    _ensure_dir()
    task_path = _task_path(session_id)

    # 幂等性检查
    if task_path.exists():
        return task_path

    task = {
        "session_id": session_id,
        "status": "pending",
        "messages": messages,
        "meta": meta or {},
        "created_at": datetime.now().isoformat(),
        "started_at": None,
        "completed_at": None,
        "output_path": None,
        "error": None,
    }

    task_path.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    return task_path


def list_pending() -> List[Dict]:
    """列出所有 pending 状态的任务"""
    _ensure_dir()
    tasks = []
    for task_file in _queue_dir().glob("*.json"):
        try:
            task = json.loads(task_file.read_text(encoding="utf-8"))
            if task.get("status") == "pending":
                tasks.append(task)
        except Exception as e:
            logger.warning(f"忽略异常: {e}")
    # 按创建时间排序
    tasks.sort(key=lambda t: t.get("created_at", ""))
    return tasks


def get_next() -> Optional[Dict]:
    """获取下一个 pending 任务并标记为 processing"""
    pending = list_pending()
    if not pending:
        return None

    task = pending[0]
    task_path = _task_path(task["session_id"])

    try:
        task["status"] = "processing"
        task["started_at"] = datetime.now().isoformat()
        task_path.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
        return task
    except Exception:
        return None


def mark_done(session_id: str, output_path: str = None):
    """标记任务完成"""
    task_path = _task_path(session_id)
    if not task_path.exists():
        return False

    try:
        task = json.loads(task_path.read_text(encoding="utf-8"))
        task["status"] = "done"
        task["completed_at"] = datetime.now().isoformat()
        if output_path:
            task["output_path"] = output_path
        task_path.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def mark_failed(session_id: str, error: str):
    """标记任务失败"""
    task_path = _task_path(session_id)
    if not task_path.exists():
        return False

    try:
        task = json.loads(task_path.read_text(encoding="utf-8"))
        task["status"] = "failed"
        task["error"] = error
        task["completed_at"] = datetime.now().isoformat()
        task_path.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def cleanup_old(days: int = 7):
    """清理 N 天前的已完成任务"""
    _ensure_dir()
    cutoff = datetime.now().timestamp() - days * 86400
    removed = 0
    for task_file in _queue_dir().glob("*.json"):
        try:
            if task_file.stat().st_mtime < cutoff:
                task = json.loads(task_file.read_text(encoding="utf-8"))
                if task.get("status") in ("done", "failed"):
                    task_file.unlink()
                    removed += 1
        except Exception as e:
            logger.warning(f"忽略异常: {e}")
    return removed


def main():
    parser = argparse.ArgumentParser(description="Distillation Queue Manager")
    parser.add_argument("--list", action="store_true", help="列出待处理任务")
    parser.add_argument("--next", action="store_true", help="获取下一个任务")
    parser.add_argument("--done", metavar="SESSION_ID", help="标记任务完成")
    parser.add_argument("--fail", metavar="SESSION_ID", help="标记任务失败")
    parser.add_argument("--output", default=None, help="完成时的输出文件路径")
    parser.add_argument("--error", default=None, help="失败时的错误信息")
    parser.add_argument("--cleanup", action="store_true", help="清理旧任务")
    args = parser.parse_args()

    if args.list:
        pending = list_pending()
        if pending:
            print(f"待蒸馏任务: {len(pending)}")
            for task in pending:
                meta = task.get("meta", {})
                msg_count = len(task.get("messages", []))
                print(f"  - {task['session_id'][:16]}... | "
                      f"消息: {msg_count} | "
                      f"来源: {meta.get('source', 'unknown')} | "
                      f"创建: {task['created_at'][:19]}")
        else:
            print("无待蒸馏任务")
        return

    if args.next:
        task = get_next()
        if task:
            print(json.dumps(task, ensure_ascii=False, indent=2))
        else:
            print("{}")
        return

    if args.done:
        success = mark_done(args.done, args.output)
        print(f"{'已标记完成' if success else '任务不存在'}: {args.done}")
        return

    if args.fail:
        success = mark_failed(args.fail, args.error or "unknown")
        print(f"{'已标记失败' if success else '任务不存在'}: {args.fail}")
        return

    if args.cleanup:
        removed = cleanup_old()
        print(f"清理完成: 移除 {removed} 个旧任务")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
