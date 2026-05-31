#!/usr/bin/env python3
"""为所有 Inbox 页面建立跨 Agent 关联"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

from core.kia.cross_agent_linker import CrossAgentLinker
from core.config import get_config


def main():
    config = get_config()
    wiki_root = config.wiki_dir
    inbox = wiki_root / "00-Inbox"

    if not inbox.exists():
        print("Inbox not found")
        return

    linker = CrossAgentLinker(wiki_root=wiki_root)

    pages = list(inbox.glob("*.md"))
    print(f"Found {len(pages)} pages in Inbox")

    total_links = 0
    for page in pages:
        try:
            actions = linker.link_after_distill(page)
            if actions:
                total_links += len(actions)
                print(f"  {page.name}: {len(actions)} links")
        except Exception as e:
            print(f"  {page.name}: ERROR {e}")

    print(f"\nTotal link actions: {total_links}")


if __name__ == "__main__":
    main()
