#!/usr/bin/env python3
"""持续批量蒸馏 — 自动处理所有待处理 sessions

用法：
    # 默认：扫描 Memos 处理所有待处理 sessions
    python scripts/distill_all.py

    # 处理单个本地文件（直出管道，不走 Memos）
    python scripts/distill_all.py --file /path/to/book.pdf

    # 处理整个目录（直出管道，不走 Memos）
    python scripts/distill_all.py --dir /path/to/documents/
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

import core.mnemos_bus as _mnb
_mnb.EventBus._recover_pending = lambda self: None

from core.hephaestus.wiki_builder import (
    fetch_l1_sessions, reconstruct_session, _is_session_completed,
    _is_processed, _mark_processed
)
from core.hephaestus.distillation_engine import (
    DistillationEngine, generate_wiki_page
)
from core.hephaestus.document_pipeline import process_doc_session
from core.hephaestus.document_processor import DocumentProcessor
from integrations.styx import MemosClient


SUPPORTED_EXTENSIONS = {'.pdf', '.pptx', '.ppt', '.xlsx', '.xls', '.docx', '.doc', '.epub', '.html', '.htm'}


def process_memos_sessions(engine: DistillationEngine, inbox: Path):
    """扫描 Memos 处理待处理 sessions（保留原有逻辑）"""
    client = MemosClient()
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

                # Doc sessions (external documents) — 直接解析生成 wiki，不走 LLM
                if sid.startswith('doc-'):
                    pages = process_doc_session(sid, messages, meta, inbox)
                    if pages > 0:
                        _mark_processed(sid, meta.get('source', 'unknown'), len(messages), 0, 'pipeline')
                        total_ok += 1
                        print(f"OK: {sid[:8]} -> {pages} pages (doc)", flush=True)
                    else:
                        _mark_processed(sid, meta.get('source', 'unknown'), len(messages), 0, 'skipped_by_pipeline')
                        total_skip += 1
                    continue

                # Regular chat sessions — 使用 LLM 蒸馏
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

    return total_ok, total_skip, total_fail


def process_local_file(file_path: Path, force_provider: str = None) -> int:
    """处理单个本地文件（直出管道，不走 Memos）"""
    if not file_path.exists():
        print(f"File not found: {file_path}", flush=True)
        return 0

    if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        print(f"Unsupported file type: {file_path.suffix}", flush=True)
        return 0

    processor = DocumentProcessor()
    provider_label = f" [{force_provider}]" if force_provider else ""
    print(f"Processing{provider_label}: {file_path.name} ...", flush=True)
    result = processor.process_and_distill(file_path, force_provider=force_provider)
    print(f"Done: {result} fragments generated", flush=True)
    return result


def process_local_dir(dir_path: Path, force_provider: str = None) -> int:
    """处理目录中的所有支持文件（直出管道，不走 Memos）"""
    if not dir_path.exists() or not dir_path.is_dir():
        print(f"Directory not found: {dir_path}", flush=True)
        return 0

    files = [f for f in dir_path.iterdir() if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS]
    if not files:
        print(f"No supported files found in {dir_path}", flush=True)
        return 0

    print(f"Found {len(files)} files to process", flush=True)
    total = 0
    for f in sorted(files):
        total += process_local_file(f, force_provider=force_provider)
    print(f"\nTotal: {total} fragments from {len(files)} files", flush=True)
    return total


def main():
    parser = argparse.ArgumentParser(description='批量蒸馏工具')
    parser.add_argument('--file', type=Path, help='处理单个本地文件（直出管道）')
    parser.add_argument('--dir', type=Path, help='处理目录中的所有文件（直出管道）')
    parser.add_argument('--provider', choices=['auto', 'api', 'cli'], default='auto',
                        help='LLM 提供商: auto=自动选择(默认), api=强制API, cli=强制本地CLI')
    args = parser.parse_args()

    from core.config import get_config
    inbox = get_config().wiki_dir / "00-Inbox"
    inbox.mkdir(parents=True, exist_ok=True)

    force_provider = None if args.provider == 'auto' else args.provider

    if args.file:
        # 单文件直出
        process_local_file(args.file, force_provider=force_provider)
    elif args.dir:
        # 目录直出
        process_local_dir(args.dir, force_provider=force_provider)
    else:
        # 默认：扫描 Memos
        engine = DistillationEngine()
        process_memos_sessions(engine, inbox)


if __name__ == "__main__":
    main()
