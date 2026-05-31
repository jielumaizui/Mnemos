#!/usr/bin/env python3
"""批量重新蒸馏 — 断点续传，每次处理一批 sessions"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# 禁用 EventBus 恢复以避免启动延迟
import core.mnemos_bus as _mnb
_mnb.EventBus._recover_pending = lambda self: None

from core.hephaestus.wiki_builder import run_build_cycle
from integrations.styx import MemosClient


def main(max_sessions: int = 20):
    print("=" * 60)
    print("Batch Redistillation — L1 sessions → Wiki pages")
    print("=" * 60)

    client = MemosClient()

    # 获取当前未处理数量
    from core.hephaestus.wiki_builder import fetch_l1_sessions, _is_processed, _is_session_completed
    sessions = fetch_l1_sessions(client)
    pending = [
        sid for sid, memos in sessions.items()
        if _is_session_completed(sid, memos) and not _is_processed(sid)
    ]
    total_pending = len(pending)
    print(f"Pending sessions: {total_pending}")

    if not pending:
        print("All sessions processed!")
        return

    # 处理一批
    stats = run_build_cycle(client, dry_run=False, use_pipeline=True)

    print("\n" + "=" * 60)
    print("Batch Complete")
    print("=" * 60)
    for key, val in stats.items():
        print(f"  {key}: {val}")

    # 剩余数量
    sessions = fetch_l1_sessions(client)
    remaining = len([
        sid for sid, memos in sessions.items()
        if _is_session_completed(sid, memos) and not _is_processed(sid)
    ])
    print(f"\nRemaining sessions: {remaining}/{total_pending}")


if __name__ == "__main__":
    main()
