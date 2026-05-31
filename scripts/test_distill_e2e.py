#!/usr/bin/env python3
"""
端到端蒸馏验证 — 处理少量高评分 session 生成 Wiki 页面
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

# 禁用 EventBus 事件恢复，避免阻塞蒸馏流程
import core.mnemos_bus as _mnb
_mnb.EventBus._recover_pending = lambda self: None

from core.hephaestus.wiki_builder import (
    fetch_l1_sessions, reconstruct_session, score_session,
    _is_session_completed, _is_processed
)
from core.hephaestus.distillation_engine import DistillationEngine
from integrations.styx import MemosClient


def main(limit: int = 3):
    client = MemosClient()
    sessions = fetch_l1_sessions(client)

    # 筛选高评分且未处理的 session
    qualified = []
    for sid, memos in sessions.items():
        if len(memos) > 200:
            continue
        if not _is_session_completed(sid, memos):
            continue
        if _is_processed(sid):
            continue
        messages, meta = reconstruct_session(memos)
        if len(messages) < 4:
            continue
        avg_score, _ = score_session(messages)
        if avg_score >= 40:
            qualified.append((sid, messages, meta, avg_score))

    qualified.sort(key=lambda x: -x[3])
    to_process = qualified[:limit]

    print(f"[E2E] 找到 {len(qualified)} 个合格 session，处理前 {len(to_process)} 个")

    engine = DistillationEngine()
    total_pages = 0

    for sid, messages, meta, score in to_process:
        print(f"\n[E2E] 处理 {sid[:20]}... (score={score:.1f}, msgs={len(messages)})")
        try:
            result = engine.process(sid, messages, meta)
            print(f"  Judgment: {result.judgment}")
            if result.judgment == "knowledge" and result.fragments:
                written = engine.write_pages(result)
                total_pages += len(written)
                print(f"  生成 {len(written)} 个 Wiki 页面:")
                for w in written:
                    print(f"    - {w}")
            elif result.judgment == "skill":
                print(f"  Skill suggestion: {result.skill_suggestion}")
            else:
                print(f"  Skipped: {result.judgment_reason}")
        except Exception as e:
            print(f"  错误: {e}")

    print(f"\n[E2E] 完成，共生成 {total_pages} 个 Wiki 页面")
    print(f"  请查看: ~/Documents/Obsidian Vault/wiki/00-Inbox/")


if __name__ == "__main__":
    from pathlib import Path
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    main(limit)
