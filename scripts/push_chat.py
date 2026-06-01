#!/usr/bin/env python3

from __future__ import annotations
"""
即时推送 — 将当前对话记录推送到 Memos 并更新本地索引

用法:
  python3 scripts/push_chat.py

设计: 每次对话结束后手动运行，实现秒级同步。
"""

import os
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from integrations.memos_sync import MemosSyncBridge as MemorySyncBridge
from core.config import get_config


def main():
    import argparse
    parser = argparse.ArgumentParser(description="即时推送对话记录到 Memos")
    parser.add_argument("--agent", default="claude", help="Agent 标识")
    parser.add_argument("--file", help="从文件读取消息（JSON 格式）")
    parser.add_argument("--stdin", action="store_true", help="从 stdin 读取消息")
    args = parser.parse_args()

    bridge = MemorySyncBridge(agent="chat-push")

    messages = []
    if args.stdin:
        import json
        content = sys.stdin.read()
        try:
            messages = json.loads(content)
        except json.JSONDecodeError:
            # 纯文本模式
            messages = [{"role": "user", "content": content, "timestamp": datetime.now().isoformat()}]
    elif args.file:
        import json
        messages = json.loads(Path(args.file).read_text(encoding="utf-8"))
    else:
        # 简单模式：推送一个占位记录，实际内容需要用户提供
        print("用法:")
        print("  echo '{\"role\":\"user\",\"content\":\"hello\"}' | python3 scripts/push_chat.py --stdin")
        print("  python3 scripts/push_chat.py --file messages.json")
        return

    if not messages:
        print("无消息可推送")
        return

    print(f"推送 {len(messages)} 条消息...")
    uid = bridge.export_chat_record(
        agent=args.agent,
        messages=messages,
        metadata={"push_at": datetime.now().isoformat()}
    )
    if uid:
        print(f"推送成功: {uid}")
    else:
        print("推送失败")


if __name__ == "__main__":
    main()
