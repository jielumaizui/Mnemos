#!/usr/bin/env python3

from __future__ import annotations
"""
[DEPRECATED] Auto Distill - 定时蒸馏检查与批量处理脚本 — 已废弃，不再维护

原因：蒸馏队列已统一为 amphora SQLite 队列，HephaestusWorker 自动消费。
该脚本的文件队列逻辑与新系统不兼容。

历史用法（已失效）：
    python3 scripts/auto_distill.py --check          # 检查队列状态
    python3 scripts/auto_distill.py --batch          # 生成批量蒸馏 prompt
    python3 scripts/auto_distill.py --daemon         # 守护模式

替代方案：
    - 使用 mnemos daemon 自动处理蒸馏队列
    - 使用 core/kia/distillation_agent.py --next 手动获取任务
    - 使用 core/hephaestus/wiki_builder.py 手动触发 Wiki 构建
"""

import warnings
warnings.warn(
    "auto_distill.py is deprecated. The distillation queue has been unified to amphora SQLite. "
    "Use mnemos daemon or distillation_agent.py instead.",
    DeprecationWarning,
    stacklevel=2,
)

# 保留原始代码作为参考，但不再推荐运行

import os
import sys
import time
import argparse
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import get_config

# NOTE: distillation_queue module not migrated; using filesystem directly
def list_pending():
    qdir = get_config().claude_data_dir / "distill_queue"
    if not qdir.exists():
        return []
    pending = []
    for f in qdir.glob("*.json"):
        try:
            import json
            data = json.loads(f.read_text(encoding="utf-8"))
            data["session_id"] = f.stem
            pending.append(data)
        except Exception:
            pass
    return pending

def get_next():
    pending = list_pending()
    return pending[0] if pending else None

def mark_done(session_id):
    qdir = get_config().claude_data_dir / "distill_queue"
    f = qdir / f"{session_id}.json"
    if f.exists():
        import shutil
        shutil.move(str(f), str(f.with_suffix(".done")))


QUEUE_DIR = get_config().claude_data_dir / "distill_queue"


def check_queue(threshold: int = 1) -> int:
    """检查队列状态，返回 pending 任务数"""
    pending = list_pending()
    count = len(pending)

    if count == 0:
        print("蒸馏队列: 无待处理任务")
        return 0

    if count >= threshold:
        print(f"蒸馏队列: {count} 个待处理任务（达到阈值 {threshold}）")
        for task in pending[:5]:  # 只显示前 5 个
            meta = task.get("meta", {})
            msg_count = len(task.get("messages", []))
            print(f"  • {task['session_id'][:20]}... | "
                  f"消息: {msg_count:3d} | "
                  f"来源: {meta.get('source', 'unknown'):8s} | "
                  f"创建: {task['created_at'][:19]}")
        if count > 5:
            print(f"  ... 还有 {count - 5} 个任务")
    else:
        print(f"蒸馏队列: {count} 个待处理任务（未达阈值 {threshold}）")

    return count


def generate_batch_prompt(threshold: int = 1) -> str:
    """生成批量蒸馏 prompt（供 Claude Code Agent 使用）"""
    pending = list_pending()
    count = len(pending)

    if count == 0:
        print("无待蒸馏任务")
        return ""

    if count < threshold:
        print(f"待蒸馏任务 {count} 个，未达阈值 {threshold}")
        return ""

    lines = [
        "# 批量知识蒸馏任务",
        "",
        f"> 待处理 Session: {count} 个",
        f"> 生成时间: {datetime.now().isoformat()[:19]}",
        "",
        "## 执行步骤",
        "",
        "1. 读取每个 session 的 JSON 数据（在 ~/.claude/distill_queue/ 中）",
        "2. 分析对话内容，提取有价值的知识单元",
        "3. 生成 Markdown + YAML frontmatter",
        "4. 写入 wiki/00-Inbox/ 或对应分类目录",
        "5. 标记任务完成",
        "",
        "## 快速处理命令",
        "",
        "```bash",
        "# 获取下一个任务",
        "python3 -m core.kia.distillation_agent --next",
        "",
        "# 列出所有待处理任务",
        "python3 -m core.kia.distillation_queue --list",
        "",
        "# 标记任务完成",
        "python3 -m core.kia.distillation_agent --done {session_id}",
        "```",
        "",
        "## 待处理 Session 列表",
        "",
    ]

    # 按类型分组统计
    type_counts = {}
    for task in pending:
        from core.kia.distillation_agent import _detect_session_type
        stype = _detect_session_type(task.get("messages", []))
        type_counts[stype] = type_counts.get(stype, 0) + 1

    lines.append(f"- 类型分布: {type_counts}")
    lines.append("")

    for i, task in enumerate(pending, 1):
        meta = task.get("meta", {})
        msg_count = len(task.get("messages", []))
        from core.kia.distillation_agent import _detect_session_type
        stype = _detect_session_type(task.get("messages", []))
        lines.append(f"### {i}. {task['session_id'][:24]}...")
        lines.append(f"- 消息数: {msg_count}")
        lines.append(f"- 类型: **{stype}**")
        lines.append(f"- 来源: {meta.get('source', 'unknown')}")
        lines.append(f"- 工作目录: {meta.get('working_dir', 'N/A')}")
        lines.append(f"- 创建时间: {task['created_at'][:19]}")
        lines.append("")

    lines.extend([
        "## 质量要求",
        "",
        "- 只提取真正有价值的知识（排除闲聊、过渡语句）",
        "- 每个知识单元必须可独立理解",
        "- 置信度 < 0.6 的内容不要写入",
        "- 如果 session 中没有值得提取的知识，直接标记完成即可",
        "",
    ])

    prompt_text = "\n".join(lines)

    # 保存 prompt 到文件
    prompt_path = QUEUE_DIR / "batch_prompt.md"
    prompt_path.write_text(prompt_text, encoding="utf-8")

    print(f"批量蒸馏 prompt 已生成: {prompt_path}")
    return prompt_text


def run_daemon(threshold: int = 3, interval_minutes: int = 30):
    """守护模式：定期检查队列并生成 prompt"""
    print(f"守护模式启动 (检查间隔: {interval_minutes} 分钟, 阈值: {threshold})")

    while True:
        count = check_queue(threshold)
        if count >= threshold:
            generate_batch_prompt(threshold)
            print("已生成批量蒸馏 prompt，请调用 Claude Code Agent 处理")
        print(f"下次检查: {interval_minutes} 分钟后...")
        print()
        time.sleep(interval_minutes * 60)


def main():
    parser = argparse.ArgumentParser(description="Auto Distill - 定时蒸馏检查")
    parser.add_argument("--check", action="store_true",
                        help="检查队列状态")
    parser.add_argument("--batch", action="store_true",
                        help="生成批量蒸馏 prompt")
    parser.add_argument("--daemon", action="store_true",
                        help="守护模式（每30分钟检查）")
    parser.add_argument("--threshold", type=int, default=3,
                        help="触发批量蒸馏的阈值（默认: 3）")
    parser.add_argument("--interval", type=int, default=30,
                        help="守护模式检查间隔（分钟，默认: 30）")
    parser.add_argument("--notify", action="store_true",
                        help="达到阈值时发送通知（macOS）")

    args = parser.parse_args()

    if args.daemon:
        run_daemon(args.threshold, args.interval)
        return

    if args.batch:
        generate_batch_prompt(args.threshold)
        return

    if args.check:
        count = check_queue(args.threshold)

        # macOS 通知
        if args.notify and count >= args.threshold and sys.platform == "darwin":
            try:
                import subprocess
                subprocess.run([
                    "osascript", "-e",
                    f'display notification "{count} 个 session 待蒸馏" '
                    f'with title "Memos-Wiki" sound name "default"'
                ], capture_output=True, timeout=5)
            except Exception:
                pass
        return

    # 默认：检查 + 如果达到阈值则生成 prompt
    count = check_queue(args.threshold)
    if count >= args.threshold:
        generate_batch_prompt(args.threshold)
    else:
        print("提示: 使用 --batch 强制生成 prompt，或使用 --daemon 启动守护模式")


if __name__ == "__main__":
    main()
