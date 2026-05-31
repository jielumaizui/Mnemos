#!/usr/bin/env python3
"""持续批量蒸馏 — 自动处理所有待处理 sessions"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

import core.mnemos_bus as _mnb
_mnb.EventBus._recover_pending = lambda self: None

from core.hephaestus.wiki_builder import (
    fetch_l1_sessions, reconstruct_session, _is_session_completed,
    _is_processed, _mark_processed
)
from core.hephaestus.distillation_engine import DistillationEngine, generate_wiki_page
from integrations.styx import MemosClient


def main():
    client = MemosClient()
    engine = DistillationEngine()
    inbox = Path('/Users/zhuwei/Documents/Obsidian Vault/wiki/00-Inbox')
    inbox.mkdir(parents=True, exist_ok=True)

    total_ok = 0
    total_skip = 0
    total_fail = 0

    while True:
        sessions = fetch_l1_sessions(client)
        pending = [
            (sid, memos) for sid, memos in sessions.items()
            if _is_session_completed(sid, memos) and not _is_processed(sid)
        ]

        if not pending:
            print("All sessions processed!", flush=True)
            break

        print(f"\n=== Batch start: {len(pending)} pending ===", flush=True)

        for sid, memos in pending[:10]:  # 每批 10 个
            try:
                messages, meta = reconstruct_session(memos)
                if len(messages) < 5:
                    _mark_processed(sid, meta.get('source', 'unknown'), len(messages), 0, 'skipped_low_quality')
                    total_skip += 1
                    continue

                result = engine.process(sid, messages, meta)
                if result.judgment != 'knowledge' or not result.fragments:
                    _mark_processed(sid, meta.get('source', 'unknown'), len(messages), 0, 'skipped_by_pipeline')
                    total_skip += 1
                    continue

                for i, frag in enumerate(result.fragments):
                    md = generate_wiki_page(frag, sid, source=meta.get('source', 'unknown'))
                    (inbox / f'{sid[:8]}_{frag.form}_{i+1}.md').write_text(md, encoding='utf-8')

                _mark_processed(sid, meta.get('source', 'unknown'), len(messages), 0, 'pipeline')
                total_ok += 1
                print(f"OK: {sid[:8]} -> {len(result.fragments)} pages", flush=True)

            except Exception as e:
                total_fail += 1
                print(f"FAIL: {sid[:8]}: {e}", flush=True)

        print(f"Running total: OK={total_ok}, SKIP={total_skip}, FAIL={total_fail}", flush=True)


if __name__ == "__main__":
    main()
