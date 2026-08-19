#!/usr/bin/env python3
"""手动重建 Obsidian Raw Vault 的 RawIndex 索引。

对应审计项 S29：当 vault 文件被外部工具批量修改后，运行此脚本让 RawIndex 与文件系统保持一致。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from core.config import get_config  # noqa: E402
from integrations.backends.obsidian_backend import ObsidianBackend  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild Obsidian RawIndex")
    parser.add_argument(
        "--vault",
        type=Path,
        default=None,
        help="Raw vault path (default: config storage.obsidian.vault_path)",
    )
    args = parser.parse_args(argv)

    vault_path = args.vault or Path(get_config().obsidian_vault_path)
    if not vault_path.exists():
        print(f"Vault path does not exist: {vault_path}", file=sys.stderr)
        return 1

    backend = ObsidianBackend(vault_path=vault_path)
    indexed = backend.rebuild_index()
    print(f"Rebuilt RawIndex for {vault_path}: {indexed} file(s) indexed")
    backend._close_raw_index()
    return 0


if __name__ == "__main__":
    sys.exit(main())
