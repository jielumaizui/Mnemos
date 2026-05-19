from __future__ import annotations

#!/usr/bin/env python3
"""
Memos 统计提醒脚本
每周六下午运行，统计 Memos 记录数量
发送 macOS 通知
"""

import os
import sys
import subprocess
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from integrations.styx import MemosClient
from core.config import get_config


def send_macos_notification(title: str, message: str):
    """发送 macOS 通知（仅 macOS）"""
    if sys.platform != "darwin":
        print(f"[{title}] {message}")
        return
    try:
        script = f'display notification "{message}" with title "{title}" sound name "default"'
        subprocess.run(["osascript", "-e", script], check=False)
    except Exception as e:
        print(f"通知发送失败: {e}")


def main():
    """主函数"""
    token = get_config().memos_token
    if not token:
        raise ValueError("MEMOS_TOKEN 环境变量未设置")
    client = MemosClient(token=token, agent="stats-reminder")

    # 获取所有记录
    memories = client.list_all_memos(max_records=1000)

    # 统计
    source_count = {}
    for mem in memories:
        source = "unknown"
        for tag in mem.tags:
            if tag.startswith("source="):
                source = tag.replace("source=", "")
                break
        source_count[source] = source_count.get(source, 0) + 1

    # 生成提醒消息
    total = len(memories)

    title = "Memos 统计提醒"
    message = f"共有 {total} 条记录"

    # 发送通知
    send_macos_notification(title, message)

    # 在 Memos 保存报告
    report = f"""# Memos 统计报告

**时间**: {datetime.now().strftime('%Y-%m-%d %H:%M')}
**总记录数**: {total} 条

## 按来源分布

"""
    for source, count in source_count.items():
        report += f"- {source}: {count} 条\n"

    report += """
## 说明

Memos 记录永久保留，作为 AI 上下文回忆的素材库。
"""

    client.save(
        content=report,
        tags=["stats-reminder", "type:report", f"week:{datetime.now().isocalendar()[1]}"]
    )

    print(f"统计提醒: {total} 条记录")
    print(f"   来源分布: {source_count}")


if __name__ == "__main__":
    main()
