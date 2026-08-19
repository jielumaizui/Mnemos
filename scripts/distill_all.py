#!/usr/bin/env python3
"""持续批量蒸馏 — 自动处理所有待处理 sessions

用法：
    # 默认：扫描 L1 storage 处理所有待处理 sessions
    python3 scripts/distill_all.py

    # 处理单个本地文件（直出管道，不走 backend）
    python3 scripts/distill_all.py --file /path/to/book.pdf

    # 处理整个目录（直出管道，不走 backend）
    python3 scripts/distill_all.py --dir /path/to/documents/
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse  # noqa: E402
from typing import Optional
import logging  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from core.hephaestus.wiki_builder import (  # noqa: E402
    fetch_l1_sessions,
    reconstruct_session,
    _is_session_completed,
    _is_processed,
    _mark_processed,
)
from core.hephaestus.distillation_engine import DistillationEngine, generate_wiki_page  # noqa: E402
from core.hephaestus.distillation_errors import DistillationAPIError  # noqa: E402
from core.hephaestus.document_pipeline import DocumentDistillationPipeline  # noqa: E402
from core.hephaestus.document_processor import DocumentProcessor  # noqa: E402
from core.sync_framework.storage_backend import create_storage_backend  # noqa: E402

DISTILL_ALL_RECOVERABLE_ERRORS = (
    OSError,
    ValueError,
    TypeError,
    KeyError,
    ImportError,
    AttributeError,
    RuntimeError,
    DistillationAPIError,
)

SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".pptx",
    ".ppt",
    ".xlsx",
    ".xls",
    ".docx",
    ".doc",
    ".epub",
    ".html",
    ".htm",
}


def _sanitize_filename_part(s: str) -> str:
    """[P1-FIX] Sanitize a string for use in a filename."""
    import re

    return re.sub(r"[^\w\-]", "_", s)[:32]


def process_backend_sessions(engine: DistillationEngine, inbox: Path):
    """扫描 L1 storage 处理待处理 sessions（保留原有逻辑）"""
    backend = create_storage_backend()
    total_ok = 0
    total_skip = 0
    total_fail = 0

    # [P1-FIX] Fetch sessions once before loop to avoid O(n²) API hammering
    sessions = fetch_l1_sessions(backend)
    pending = [
        (sid, session_data)
        for sid, session_data in sessions.items()
        if _is_session_completed(sid, session_data) and not _is_processed(sid)
    ]

    while pending:
        batch = pending[:10]  # 每批 10 个
        pending = pending[10:]

        print(f"\n=== Batch start: {len(batch)} pending ===", flush=True)

        for sid, session_data in batch:
            try:
                messages, meta = reconstruct_session(session_data)
                source = meta.get("source", "unknown")
                msg_count = len(messages)

                # Doc sessions (external documents) — 直接解析生成 wiki，不走 LLM
                if sid.startswith("doc-"):
                    document_pipeline = DocumentDistillationPipeline()
                    document_result = document_pipeline.process(sid, messages, meta)
                    if document_result.judgment == "skip" or not document_result.fragments:
                        pages = 0
                    else:
                        document_pipeline.inbox_dir = inbox
                        written_paths = document_pipeline.write_to_wiki(
                            document_result,
                            source=meta.get("source", "unknown"),
                        )
                        if not written_paths:
                            raise RuntimeError(
                                "document pipeline produced fragments without committed Wiki pages"
                            )
                        pages = len(written_paths)
                    if pages > 0:
                        total_ok += 1
                        print(f"OK: {sid[:8]} -> {pages} pages (doc)", flush=True)
                    else:
                        total_skip += 1
                    # [P1-FIX] Mark processed only after all side effects succeed
                    _mark_processed(
                        sid,
                        source,
                        msg_count,
                        0,
                        "pipeline" if pages > 0 else "skipped_by_pipeline",
                    )
                    continue

                # Regular chat sessions — 使用 LLM 蒸馏
                if len(messages) < 5:
                    total_skip += 1
                    _mark_processed(sid, source, msg_count, 0, "skipped_low_quality")
                    continue

                result = engine.process(sid, messages, meta)
                if result.judgment != "knowledge" or not result.fragments:
                    total_skip += 1
                    _mark_processed(sid, source, msg_count, 0, "skipped_by_pipeline")
                    continue

                safe_sid = _sanitize_filename_part(sid)
                for i, frag in enumerate(result.fragments):
                    md = generate_wiki_page(frag, sid, source=source)
                    # [P1-FIX] Use sanitized sid in filename to avoid path injection
                    (inbox / f"{safe_sid}_{frag.form}_{i+1}.md").write_text(md, encoding="utf-8")

                # [P1-FIX] Mark processed only after successful write
                _mark_processed(sid, source, msg_count, 0, "pipeline")
                total_ok += 1
                print(f"OK: {sid[:8]} -> {len(result.fragments)} pages", flush=True)

            except DISTILL_ALL_RECOVERABLE_ERRORS as e:
                total_fail += 1
                print(f"FAIL: {sid[:8]}: {e}", flush=True)

        print(f"Running total: OK={total_ok}, SKIP={total_skip}, FAIL={total_fail}", flush=True)

    print("All sessions processed!", flush=True)
    return total_ok, total_skip, total_fail


def process_local_file(file_path: Path, force_provider: Optional[str] = None) -> int:
    """处理单个本地文件（直出管道，不走 backend）"""
    if not file_path.exists():
        print(f"File not found: {file_path}", flush=True)
        return 0

    if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        print(f"Unsupported file type: {file_path.suffix}", flush=True)
        return 0

    processor = DocumentProcessor()
    provider_label = f" [{force_provider}]" if force_provider else ""
    print(f"Processing{provider_label}: {file_path.name} ...", flush=True)
    result = processor.process_and_distill(file_path, force_provider=force_provider or "")
    print(f"Done: {result} fragments generated", flush=True)
    if isinstance(result, dict):
        return int(result.get("fragment_count", 0) or 0)
    return int(result)


def process_local_dir(dir_path: Path, force_provider: Optional[str] = None) -> int:
    """处理目录中的所有支持文件（直出管道，不走 backend）"""
    if not dir_path.exists() or not dir_path.is_dir():
        print(f"Directory not found: {dir_path}", flush=True)
        return 0

    files = [
        f for f in dir_path.iterdir() if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
    ]
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
    parser = argparse.ArgumentParser(description="批量蒸馏工具")
    parser.add_argument("--file", type=Path, help="处理单个本地文件（直出管道）")
    parser.add_argument("--dir", type=Path, help="处理目录中的所有文件（直出管道）")
    parser.add_argument(
        "--provider",
        default="auto",
        help="LLM 提供商: auto=自动选择(默认), api=默认 API chain，或填写具体 provider 名如 dmxapi/siliconflow/openai",
    )
    args = parser.parse_args()

    # [P0-FIX] 从配置读取 inbox 路径，避免硬编码导致数据写入错误目录
    from core.config import get_config  # noqa: E402

    inbox = get_config().wiki_dir / "00-Inbox"
    inbox.mkdir(parents=True, exist_ok=True)

    force_provider = None if args.provider == "auto" else args.provider

    # [P1-FIX] Move monkey-patch into main() to avoid global state mutation at import time
    import core.mnemos_bus as _mnb

    _mnb.EventBus._recover_pending = lambda self: None

    if args.file:
        # 单文件直出
        process_local_file(args.file, force_provider=force_provider)
    elif args.dir:
        # 目录直出
        process_local_dir(args.dir, force_provider=force_provider)
    else:
        # 默认：扫描 L1 storage
        engine = DistillationEngine()
        process_backend_sessions(engine, inbox)


if __name__ == "__main__":
    main()
