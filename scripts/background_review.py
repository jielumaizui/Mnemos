from __future__ import annotations

#!/usr/bin/env python3
"""
Background Review - 知识自动审查脚本

触发方式：
  手动: python3 scripts/background_review.py
  定时: cron（建议每日一次）

功能：
  1. 读取 review_queue（由 ingest_engine 或 ai_memory_sync 写入）
  2. 生成审查报告（human-readable）
  3. 可选：调用 LLM 生成精炼摘要
  4. 标记已处理条目

安全设计：
  - 只读取，不修改原始 Memos 记录
  - 审查报告供人工确认后执行
"""

import json
import sys
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional
from core.config import get_config

sys.path.insert(0, str(Path(__file__).parent.parent))


REVIEW_QUEUE = get_config().data_dir / "review_queue.jsonl"
REVIEW_LOG_DIR = get_config().data_dir / "logs/review"


def load_pending_entries() -> List[Dict]:
    """读取 review_queue 中 pending 的条目"""
    if not REVIEW_QUEUE.exists():
        return []

    entries = []
    try:
        with open(REVIEW_QUEUE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("status") == "pending":
                        entries.append(entry)
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        print(f"[Review] 读取队列失败: {e}")
        return []

    return entries


def generate_review_report(entries: List[Dict]) -> str:
    """生成审查报告"""
    lines = [
        f"# Background Review Report",
        f"",
        f"生成时间: {datetime.now().isoformat()}",
        f"待审查条目: {len(entries)}",
        f"",
        f"---",
        f"",
    ]

    for i, entry in enumerate(entries, 1):
        entities = entry.get("entities", [])
        concepts = entry.get("concepts", [])
        category = entry.get("category", "unknown")
        summary = entry.get("summary", "")
        content_preview = entry.get("content_preview", "")

        lines.append(f"## {i}. {entry.get('l1_uid', 'unknown')[:16]}...")
        lines.append(f"")
        lines.append(f"- **分类**: {category}")
        lines.append(f"- **实体**: {', '.join(entities[:5]) if entities else 'N/A'}")
        lines.append(f"- **概念**: {', '.join(concepts[:5]) if concepts else 'N/A'}")
        lines.append(f"- **摘要**: {summary or 'N/A'}")
        lines.append(f"")
        lines.append(f"**内容预览**:")
        lines.append(f"```")
        lines.append(content_preview[:500])
        lines.append(f"```")
        lines.append(f"")

        # 审查建议（规则-based，零 LLM 成本）
        suggestions = []
        if not entities and not concepts:
            suggestions.append("无实体/概念提取，可能为低价值内容")
        if len(content_preview) < 100:
            suggestions.append("内容过短，建议确认是否需要精炼")
        if category == "unknown":
            suggestions.append("分类未知，建议人工确认")
        if not summary:
            suggestions.append("缺少摘要，建议补充")

        if suggestions:
            lines.append(f"**审查建议**:")
            for s in suggestions:
                lines.append(f"- {s}")
        else:
            lines.append(f"**审查建议**: 通过")

        lines.append(f"")
        lines.append(f"---")
        lines.append(f"")

    return "\n".join(lines)


def mark_entries_processed(entries: List[Dict]) -> None:
    """标记条目为已处理"""
    if not REVIEW_QUEUE.exists() or not entries:
        return

    l1_uids = {e.get("l1_uid") for e in entries}
    updated_lines = []

    try:
        with open(REVIEW_QUEUE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("l1_uid") in l1_uids and entry.get("status") == "pending":
                        entry["status"] = "reviewed"
                        entry["reviewed_at"] = datetime.now().isoformat()
                    updated_lines.append(json.dumps(entry, ensure_ascii=False))
                except json.JSONDecodeError:
                    updated_lines.append(line)

        with open(REVIEW_QUEUE, "w", encoding="utf-8") as f:
            for line in updated_lines:
                f.write(line + "\n")
    except Exception as e:
        print(f"[Review] 标记处理状态失败: {e}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Background Review - 知识自动审查")
    parser.add_argument("--dry-run", action="store_true", help="只生成报告，不标记已处理")
    parser.add_argument("--output", help="报告输出路径（默认自动生成）")
    args = parser.parse_args()

    print("[Review] 读取 review queue...")
    entries = load_pending_entries()
    print(f"[Review] 找到 {len(entries)} 条待审查记录")

    if not entries:
        print("[Review] 没有待审查记录，退出")
        return

    # 生成报告
    report = generate_review_report(entries)

    # 保存报告
    REVIEW_LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = Path(args.output) if args.output else REVIEW_LOG_DIR / f"review-{timestamp}.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"[Review] 报告已保存: {report_path}")

    # 标记已处理
    if not args.dry_run:
        mark_entries_processed(entries)
        print(f"[Review] 已标记 {len(entries)} 条记录为已审查")
    else:
        print("[Review] Dry-run 模式，未标记处理状态")


if __name__ == "__main__":
    main()
