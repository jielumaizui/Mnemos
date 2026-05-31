#!/usr/bin/env python3
"""批量蒸馏脚本 - 处理未处理的 L1 sessions"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# 禁用 EventBus 恢复以避免启动延迟
import core.mnemos_bus as _mnb
_mnb.EventBus._recover_pending = lambda self: None

from core.hephaestus.wiki_builder import run_build_cycle
from integrations.styx import MemosClient

def main():
    print("=" * 60)
    print("Batch Distillation - Processing L1 sessions")
    print("=" * 60)
    
    client = MemosClient()
    stats = run_build_cycle(client, dry_run=False, use_pipeline=True)
    
    print("\n" + "=" * 60)
    print("Batch Complete")
    print("=" * 60)
    for key, val in stats.items():
        print(f"  {key}: {val}")

if __name__ == "__main__":
    main()
