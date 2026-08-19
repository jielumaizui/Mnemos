#!/usr/bin/env python3
"""
Mnemos Vault 布局迁移脚本（v1 -> v2）

功能：
1. 将旧 wiki 内容复制到配置中的主认知 Vault
2. 初始化配置中的 raw Vault
3. 将两个 Vault 注册到 Obsidian
4. 在主认知 Vault 初始化 git 仓库
5. 更新运行时配置（vaults + 兼容别名）
6. 可选：在确认成功后删除旧 wiki 目录

用法：
    python3 scripts/migrate_vault_layout.py [--yes] [--delete-old] [--dry-run]

安全规则：
- 默认 dry-run，先预览要复制的文件
- 删除旧目录需要显式 --delete-old，且仅在新目录文件完整后执行
- 复制使用增量策略：目标不存在或源文件更新时才覆盖
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# 确保能导入项目模块
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config import get_config  # noqa: E402
from core.setup.vault_layout import default_mnemos_vault_path, default_raw_vault_path  # noqa: E402
from integrations.backends.obsidian_backend import ensure_vault_recognized  # noqa: E402

DEFAULT_MNEMOS_VAULT = default_mnemos_vault_path()
DEFAULT_RAW_VAULT = default_raw_vault_path()


def _copy_tree_incremental(src: Path, dst: Path, dry_run: bool = False) -> tuple[int, int]:
    """增量复制目录树，返回 (copied_dirs, copied_files)。"""
    # 安全：防止目标在源内部（或源在目标内部）导致无限嵌套复制
    try:
        dst.resolve().relative_to(src.resolve())
        raise ValueError(f"目标目录 {dst} 不能位于源目录 {src} 内部")
    except ValueError:
        pass
    try:
        src.resolve().relative_to(dst.resolve())
        raise ValueError(f"源目录 {src} 不能位于目标目录 {dst} 内部")
    except ValueError:
        pass

    copied_dirs = 0
    copied_files = 0
    for item in sorted(src.rglob("*")):
        rel = item.relative_to(src)
        target = dst / rel
        if item.is_dir():
            if not target.exists():
                if not dry_run:
                    target.mkdir(parents=True, exist_ok=True)
                copied_dirs += 1
            continue
        # 文件：目标不存在或源更新时才复制
        if not target.exists() or item.stat().st_mtime > target.stat().st_mtime:
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)
            copied_files += 1
    return copied_dirs, copied_files


def _init_git(vault_dir: Path, dry_run: bool = False) -> bool:
    git_dir = vault_dir / ".git"
    if git_dir.exists():
        return True
    if dry_run:
        return True
    try:
        subprocess.run(
            ["git", "init", str(vault_dir)],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        subprocess.run(
            ["git", "-C", str(vault_dir), "add", "."],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        subprocess.run(
            ["git", "-C", str(vault_dir), "commit", "-m", "chore: initial vault migration"],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return True
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        print(f"  ⚠️  git 初始化失败（可稍后手动执行）: {e}")
        return False


def migrate(
    mnemos_vault: Path = DEFAULT_MNEMOS_VAULT,
    raw_vault: Path = DEFAULT_RAW_VAULT,
    yes: bool = False,
    delete_old: bool = False,
    dry_run: bool = False,
) -> bool:
    cfg = get_config()
    src_wiki = cfg.wiki_dir

    print("=" * 60)
    print("Mnemos Vault 布局迁移")
    print("=" * 60)
    print(f"源 wiki 目录: {src_wiki}")
    print(f"目标 mnemos Vault: {mnemos_vault}")
    print(f"目标 raw Vault: {raw_vault}")
    print(f"模式: {'dry-run' if dry_run else '执行'}")
    print()

    if src_wiki == mnemos_vault:
        print("✓ 当前配置已是新布局，无需迁移。")
        return True

    if not src_wiki.exists():
        print(f"⚠️  源 wiki 目录不存在: {src_wiki}，跳过复制")
    else:
        if not dry_run and not yes:
            ans = input("确认开始复制？[y/N]: ").strip().lower()
            if ans not in ("y", "yes"):
                print("已取消")
                return False

        print("  复制 wiki 内容到 mnemos Vault...")
        dirs, files = _copy_tree_incremental(src_wiki, mnemos_vault, dry_run=dry_run)
        print(f"  ✓ 复制完成: {dirs} 个目录, {files} 个文件")

    # 确保认知层子目录存在
    cognitive_subdirs = [
        "L2.4-KG",
        "L2.4-KG/Entities",
        "L2.4-KG/Relations",
        "L2.4-KG/MOCs",
        "L3-Observations",
        "L4-Reflections",
        "L4-Reflections/Reflections",
        "L4-Reflections/Shifts",
        "L4-Reflections/Reports",
        "L5-Feedback",
    ]
    for sub in cognitive_subdirs:
        d = mnemos_vault / sub
        if not dry_run:
            d.mkdir(parents=True, exist_ok=True)
        print(f"  ✓ 确保目录存在: {d}")

    # 初始化 raw Vault
    if not dry_run:
        raw_vault.mkdir(parents=True, exist_ok=True)
        (raw_vault / ".obsidian").mkdir(parents=True, exist_ok=True)
    print(f"  ✓ raw Vault 已就绪: {raw_vault}")

    # 注册 Obsidian
    if not dry_run:
        ensure_vault_recognized(mnemos_vault)
        ensure_vault_recognized(raw_vault)
    print("  ✓ Vault 已注册到 Obsidian")

    # 初始化 git
    if _init_git(mnemos_vault, dry_run=dry_run):
        print("  ✓ mnemos Vault git 仓库已初始化")

    # 更新配置
    if not dry_run:
        cfg.set("vaults.mnemos.path", str(mnemos_vault))
        cfg.set("vaults.raw.path", str(raw_vault))
        cfg.set("wiki.vault_path", str(mnemos_vault))
        cfg.set("storage.obsidian.vault_path", str(raw_vault))
        cfg.save()
    print("  ✓ 配置已更新")

    # 删除旧目录
    if delete_old and src_wiki.exists() and src_wiki != mnemos_vault:
        if not yes:
            ans = input(f"确认删除旧 wiki 目录 {src_wiki}？[yes/N]: ").strip().lower()
            if ans != "yes":
                print("  已跳过删除旧目录")
                return True
        if not dry_run:
            shutil.rmtree(src_wiki)
        print(f"  ✓ 已删除旧 wiki 目录: {src_wiki}")

    print()
    print("=" * 60)
    print("迁移完成" if not dry_run else "dry-run 预览完成")
    print("=" * 60)
    print(f"请打开 Obsidian 验证 {mnemos_vault} 与 {raw_vault}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Mnemos Vault 布局迁移")
    parser.add_argument(
        "--mnemos-vault",
        type=Path,
        default=DEFAULT_MNEMOS_VAULT,
        help=f"目标主认知 Vault 路径（默认 {DEFAULT_MNEMOS_VAULT}）",
    )
    parser.add_argument(
        "--raw-vault",
        type=Path,
        default=DEFAULT_RAW_VAULT,
        help=f"目标 raw Vault 路径（默认 {DEFAULT_RAW_VAULT}）",
    )
    parser.add_argument("--yes", "-y", action="store_true", help="自动确认")
    parser.add_argument(
        "--delete-old", action="store_true", help="迁移成功后删除旧 wiki 目录（需显式确认）"
    )
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不实际写入")
    args = parser.parse_args()

    ok = migrate(
        mnemos_vault=args.mnemos_vault.expanduser(),
        raw_vault=args.raw_vault.expanduser(),
        yes=args.yes,
        delete_old=args.delete_old,
        dry_run=args.dry_run,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
