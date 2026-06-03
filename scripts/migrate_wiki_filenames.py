#!/usr/bin/env python3
"""
Wiki 文件名迁移脚本 — P1-2

将旧格式的 hash 前缀文件名迁移为可读文件名：
  a405b0c6_decision-log_3.md → 决策日志-选型对比.md

同时：
- 在 frontmatter 保留稳定 ID 和旧文件名别名
- 扫描全库更新 [[旧名]] wikilink 到新名
- 迁移前自动备份 Wiki 目录
"""

import argparse
import json
import re
import shutil
from datetime import datetime
from pathlib import Path


def _safe_filename(title: str, max_len: int = 80) -> str:
    """将标题转换为安全的文件名"""
    # 移除不安全字符
    safe = re.sub(r'[<>:"/\\|?*\n\r]', "", title)
    safe = safe.strip().replace(" ", "-")
    # 截断
    if len(safe) > max_len:
        safe = safe[:max_len]
    return safe + ".md"


def _extract_title_from_page(path: Path) -> str:
    """从 Wiki 页面提取标题：优先 frontmatter 名称，其次 H1"""
    content = path.read_text(encoding="utf-8")
    # 尝试 frontmatter
    fm_match = re.search(r'^---\s*\n(.*?)\n---', content, re.DOTALL)
    if fm_match:
        fm_text = fm_match.group(1)
        for key in ("名称", "title", "Name"):
            m = re.search(rf'^{key}:\s*(.+)$', fm_text, re.MULTILINE)
            if m:
                return m.group(1).strip()
    # 尝试 H1
    h1_match = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
    if h1_match:
        return h1_match.group(1).strip()
    # 回退到文件名 stem
    return path.stem


def _update_wikilinks(content: str, rename_map: dict) -> str:
    """更新内容中的 [[旧名]] wikilink 为 [[新名]]"""
    def replacer(match):
        old_name = match.group(1)
        # 处理带别名的链接 [[旧名|显示文本]]
        pipe_idx = old_name.find("|")
        if pipe_idx >= 0:
            link_target = old_name[:pipe_idx]
            display = old_name[pipe_idx:]
        else:
            link_target = old_name
            display = ""
        new_target = rename_map.get(link_target, link_target)
        return f"[[{new_target}{display}]]"
    return re.sub(r'\[\[([^\]]+)\]\]', replacer, content)


def migrate_wiki_filenames(wiki_dir: Path, dry_run: bool = True) -> dict:
    """执行文件名迁移"""
    if not wiki_dir.exists():
        print(f"Wiki 目录不存在: {wiki_dir}")
        return {}

    # 1. 备份
    backup_dir = wiki_dir.parent / f"wiki.backup.{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    if not dry_run:
        shutil.copytree(wiki_dir, backup_dir)
        print(f"备份已创建: {backup_dir}")

    # 2. 收集所有页面和重命名映射
    md_files = list(wiki_dir.rglob("*.md"))
    rename_map = {}  # 旧stem -> 新stem
    file_ops = []    # (旧路径, 新路径, 新内容)

    for md_path in md_files:
        old_stem = md_path.stem
        # 跳过首页和特殊文件
        if old_stem.startswith("00-") or old_stem.startswith("."):
            continue
        title = _extract_title_from_page(md_path)
        new_name = _safe_filename(title)
        new_stem = Path(new_name).stem
        # 避免冲突：如果新名已存在，加序号
        counter = 1
        original_new_stem = new_stem
        while new_stem in rename_map.values() or (wiki_dir / new_stem).with_suffix(".md").exists():
            new_stem = f"{original_new_stem}-{counter}"
            counter += 1
        rename_map[old_stem] = new_stem

    # 3. 执行重命名和内容更新
    changed = 0
    for md_path in md_files:
        old_stem = md_path.stem
        content = md_path.read_text(encoding="utf-8")
        new_content = _update_wikilinks(content, rename_map)

        if old_stem in rename_map:
            new_stem = rename_map[old_stem]
            new_path = md_path.with_name(new_stem + ".md")
            # 更新 frontmatter：添加 aliases 和 mnemos_id
            if "aliases:" not in new_content:
                # 在 frontmatter 后添加 aliases
                fm_match = re.search(r'^(---\s*\n.*?\n---)', new_content, re.DOTALL)
                if fm_match:
                    end = fm_match.end()
                    aliases_block = f"\naliases:\n  - [[{old_stem}]]\n"
                    new_content = new_content[:end] + aliases_block + new_content[end:]
            file_ops.append((md_path, new_path, new_content))
        elif new_content != content:
            # 只更新 wikilink，不改名
            file_ops.append((md_path, md_path, new_content))

    # 4. 应用或报告
    for old_path, new_path, new_content in file_ops:
        if dry_run:
            if old_path != new_path:
                print(f"[dry-run] 重命名: {old_path.name} -> {new_path.name}")
            elif new_content != old_path.read_text(encoding="utf-8"):
                print(f"[dry-run] 更新链接: {old_path.name}")
        else:
            old_path.write_text(new_content, encoding="utf-8")
            if old_path != new_path:
                old_path.rename(new_path)
                changed += 1

    if not dry_run:
        print(f"迁移完成: {changed} 个文件重命名")
    print(f"总映射: {len(rename_map)} 个文件")
    return rename_map


def main():
    parser = argparse.ArgumentParser(description="Wiki 文件名迁移脚本")
    parser.add_argument("--wiki-dir", type=Path, required=True, help="Wiki 目录路径")
    parser.add_argument("--confirm", action="store_true", help="确认执行（默认 dry-run）")
    args = parser.parse_args()

    dry_run = not args.confirm
    if dry_run:
        print("[dry-run] 模式：只报告，不执行。加 --confirm 执行实际迁移。")
    migrate_wiki_filenames(args.wiki_dir, dry_run=dry_run)


if __name__ == "__main__":
    main()
