#!/usr/bin/env python3
"""1) 为所有 Inbox 页面补全 frontmatter 来源字段  2) 建立跨 Agent 关联"""
import sys, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

from integrations.styx import MemosClient
from core.kia.cross_agent_linker import CrossAgentLinker
from core.config import get_config


def update_page_source(page_path: Path, source: str) -> bool:
    """更新页面 frontmatter 中的来源字段"""
    content = page_path.read_text(encoding="utf-8")
    if "来源:" in content or "source:" in content:
        return False  # 已有来源

    # 在 frontmatter 末尾添加来源
    lines = content.split("\n")
    fm_end = 0
    in_fm = False
    for i, line in enumerate(lines):
        if line.strip() == "---":
            if in_fm:
                fm_end = i
                break
            in_fm = True

    if fm_end > 0:
        lines.insert(fm_end, f"来源: {source}")
        page_path.write_text("\n".join(lines), encoding="utf-8")
        return True
    return False


def main():
    client = MemosClient()
    config = get_config()
    wiki_root = config.wiki_dir
    inbox = wiki_root / "00-Inbox"

    pages = list(inbox.glob("*.md"))
    print(f"Found {len(pages)} pages")

    # Step 1: 补全来源字段
    source_fixed = 0
    for page in pages:
        # 从文件名提取 session_id 前缀
        sid_prefix = page.stem.split("_")[0]
        # 查询 Memos 找匹配的 session
        try:
            memories = client.list_by_tags([f"session={sid_prefix}"], limit=1)
            if memories:
                source = "unknown"
                for tag in memories[0].tags:
                    if tag.startswith("source="):
                        source = tag.split("=", 1)[1]
                        break
                if update_page_source(page, source):
                    source_fixed += 1
                    print(f"  Updated source: {page.name} -> {source}")
        except Exception as e:
            print(f"  Error querying {sid_prefix}: {e}")

    print(f"\nSource fixed: {source_fixed}")

    # Step 2: 建立跨 Agent 关联
    linker = CrossAgentLinker(wiki_root=wiki_root)
    total_links = 0
    for page in pages:
        try:
            actions = linker.link_after_distill(page)
            if actions:
                total_links += len(actions)
                print(f"  Linked: {page.name} -> {len(actions)} actions")
        except Exception as e:
            print(f"  Link error {page.name}: {e}")

    print(f"\nTotal link actions: {total_links}")


if __name__ == "__main__":
    main()
