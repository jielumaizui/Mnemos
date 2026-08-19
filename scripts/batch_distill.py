#!/usr/bin/env python3
"""批量回追脚本 — 处理未进入 distill_queue 的 L1 sessions。

本脚本为 catch-up 工具，用于补漏因 daemon 未运行等原因未处理的 L1 记录。
正常生产路径应通过 HephaestusWorker 消费 distill_queue，而非直接运行本脚本。
"""

import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent))  # [P2-FIX] Guard sys.path mutation

import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from core.hephaestus.wiki_builder import run_build_cycle  # noqa: E402
from core.sync_framework.storage_backend import create_storage_backend  # noqa: E402


def main() -> None:  # [P2-FIX] Add return type annotation for public function
    """运行 L1 批量回追主流程。"""
    # [P1-FIX] Move monkey-patch into main() to avoid global state mutation at import time
    import core.mnemos_bus as _mnb

    _mnb.EventBus._recover_pending = lambda self: None  # type: ignore[method-assign]

    print("=" * 60)
    print("Batch Distillation (catch-up) — Processing L1 sessions")
    print("Note: Normal flow should use HephaestusWorker + distill_queue")
    print("=" * 60)

    backend = create_storage_backend()
    stats = run_build_cycle(backend, dry_run=False)

    print("\n" + "=" * 60)
    print("Batch Complete")
    print("=" * 60)
    for key, val in stats.items():
        print(f"  {key}: {val}")


if __name__ == "__main__":
    main()
