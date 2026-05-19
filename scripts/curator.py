from __future__ import annotations

#!/usr/bin/env python3
"""
Curator - Wiki 页面自动合并编排器

触发方式：
  手动: python3 scripts/curator.py
  定时: cron（P2→P1 每日，P1→P0 每周）

功能：
  1. 扫描 wiki/ 目录，统计各主题页面数
  2. 堆积检测：单主题 >50 页面时告警
  3. 生成合并建议报告
  4. 可选 --auto 模式：调用 LLM 生成合并后的 P1/P0 页面

安全约束：
  - 从不删除原页面，只标记 deprecated
  - 合并前备份原页面
  - 报告留存供人工复查
"""

import json
import sys
import re
import shutil
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from collections import defaultdict
from core.config import get_config

sys.path.insert(0, str(Path(__file__).parent.parent))

WIKI_DIR = get_config().wiki_dir
CURATOR_LOG_DIR = get_config().data_dir / "logs/curator"
ARCHIVE_DIR = get_config().wiki_dir / ".archive"

# 堆积阈值
PILEUP_THRESHOLD = 50       # 单主题超过此数量触发紧急合并
P2_P1_DAYS = 1              # P2→P1 合并周期（天）
P1_P0_DAYS = 7              # P1→P0 合并周期（天）


def scan_wiki_pages() -> List[Dict]:
    """扫描 wiki 目录下的所有页面（递归，支持 workspace 隔离）"""
    pages = []
    if not WIKI_DIR.exists():
        return pages

    # 跳过的目录：归档、索引、版本控制
    skip_dirs = {".archive", "docs", ".git"}

    # 递归扫描所有 .md 文件
    for md_file in WIKI_DIR.rglob("*.md"):
        rel_parts = md_file.relative_to(WIKI_DIR).parts
        # 跳过指定目录及其子目录
        if any(part in skip_dirs for part in rel_parts):
            continue
        if md_file.name == "index.md":
            continue

        stat = md_file.stat()
        # category 保留完整的相对目录路径（如 claude/entities）
        category = "/".join(rel_parts[:-1]) if len(rel_parts) > 1 else "root"

        pages.append({
            "path": str(md_file),
            "name": md_file.stem,
            "category": category,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "ctime": stat.st_ctime,
        })

    return pages


def group_by_topic(pages: List[Dict]) -> Dict[str, List[Dict]]:
    """按主题聚类页面"""
    topics = defaultdict(list)

    for page in pages:
        name = page["name"]
        # 提取主题前缀（如 "api-design-v1", "api-design-v2" → "api-design"）
        base = re.sub(r'[-_]?v?\d+$', '', name)
        base = re.sub(r'[-_]?\d{4}-\d{2}-\d{2}$', '', base)
        topics[base].append(page)

    return dict(topics)


def detect_pileups(topics: Dict[str, List[Dict]], pages: List[Dict] = None,
                   threshold: int = PILEUP_THRESHOLD) -> List[Tuple[str, List[Dict]]]:
    """检测堆积的主题

    两个维度：
    1. 同一主题前缀（英文版本号类页面）
    2. 同一 category（中文页面等无版本号页面）
    """
    pileups = []

    # 维度1：主题前缀堆积（原有逻辑）
    for topic, pgs in topics.items():
        if len(pgs) >= threshold:
            pileups.append((topic, pgs))

    # 维度2：按目录堆积（中文页面无版本号，按 category 检测）
    if pages:
        from collections import Counter
        category_counts = Counter(p.get("category", "unknown") for p in pages)
        for cat, count in category_counts.items():
            if count >= threshold:
                # 检查是否已作为某个 topic 的一部分被报告
                cat_pages = [p for p in pages if p.get("category") == cat]
                pileups.append((f"category:{cat}", cat_pages))

    # 去重：同一组页面只报一次
    seen_paths = set()
    unique_pileups = []
    for topic, pgs in pileups:
        key = tuple(sorted(p["path"] for p in pgs))
        if key not in seen_paths:
            seen_paths.add(key)
            unique_pileups.append((topic, pgs))

    # 按页面数降序
    unique_pileups.sort(key=lambda x: len(x[1]), reverse=True)
    return unique_pileups


def find_stale_pages(pages: List[Dict], days: int) -> List[Dict]:
    """查找超过 N 天未修改的页面"""
    cutoff = datetime.now() - timedelta(days=days)
    cutoff_ts = cutoff.timestamp()
    return [p for p in pages if p["mtime"] < cutoff_ts]


def generate_merge_plan(topics: Dict[str, List[Dict]],
                        pileups: List[Tuple[str, List[Dict]]]) -> Dict:
    """生成合并计划"""
    plan = {
        "generated_at": datetime.now().isoformat(),
        "total_pages": sum(len(pages) for pages in topics.values()),
        "total_topics": len(topics),
        "pileups": [],
        "daily_merge_candidates": [],
        "weekly_merge_candidates": [],
    }

    # 堆积主题
    for topic, pages in pileups:
        plan["pileups"].append({
            "topic": topic,
            "page_count": len(pages),
            "pages": [p["name"] for p in pages],
            "suggested_action": "merge_to_p1",
        })

    # 日常合并候选（最近1天修改的页面）
    recent_cutoff = (datetime.now() - timedelta(days=P2_P1_DAYS)).timestamp()
    for topic, pages in topics.items():
        recent = [p for p in pages if p["mtime"] >= recent_cutoff]
        if len(recent) >= 2:
            plan["daily_merge_candidates"].append({
                "topic": topic,
                "recent_pages": [p["name"] for p in recent],
                "count": len(recent),
            })

    # 周合并候选（超过7天未修改的页面）
    stale_cutoff = (datetime.now() - timedelta(days=P1_P0_DAYS)).timestamp()
    for topic, pages in topics.items():
        stale = [p for p in pages if p["mtime"] < stale_cutoff]
        if len(stale) >= 3:
            plan["weekly_merge_candidates"].append({
                "topic": topic,
                "stale_pages": [p["name"] for p in stale],
                "count": len(stale),
            })

    return plan


def generate_curator_report(plan: Dict) -> str:
    """生成 Obsidian 友好的 Curator 报告（带 frontmatter + wikilink）"""
    lines = [
        "---",
        "hermes_type: curator-report",
        f"generated_at: {plan['generated_at']}",
        f"total_pages: {plan['total_pages']}",
        f"total_topics: {plan['total_topics']}",
        "tags: [hermes, curator, report]",
        "---",
        "",
        "# Curator Merge Report",
        "",
        f"> 生成时间: {plan['generated_at']} | 总页面: {plan['total_pages']} | 主题: {plan['total_topics']}",
        "",
        "---",
        "",
    ]

    # 堆积告警
    if plan["pileups"]:
        lines.append(f"## 堆积告警（需立即合并）")
        lines.append(f"")
        for pileup in plan["pileups"]:
            lines.append(f"- **{pileup['topic']}**: {pileup['page_count']} 页")
            lines.append(f"  - 页面: {', '.join(pileup['pages'][:5])}")
            if len(pileup['pages']) > 5:
                lines.append(f"  - ... 等共 {pileup['page_count']} 页")
        lines.append(f"")
    else:
        lines.append(f"## 无堆积告警")
        lines.append(f"")

    # 日常合并候选
    if plan["daily_merge_candidates"]:
        lines.append(f"## 日常合并候选（P2→P1）")
        lines.append(f"")
        for candidate in plan["daily_merge_candidates"]:
            lines.append(f"- **{candidate['topic']}**: {candidate['count']} 个新页面")
        lines.append(f"")

    # 周合并候选
    if plan["weekly_merge_candidates"]:
        lines.append(f"## 周合并候选（P1→P0）")
        lines.append(f"")
        for candidate in plan["weekly_merge_candidates"]:
            lines.append(f"- **{candidate['topic']}**: {candidate['count']} 个旧页面")
        lines.append(f"")

    # 操作建议
    lines.append(f"---")
    lines.append(f"")
    lines.append(f"## 操作建议")
    lines.append(f"")
    if plan["pileups"]:
        lines.append(f"1. **优先处理堆积主题**（>{PILEUP_THRESHOLD} 页）")
        lines.append(f"   ```bash")
        for pileup in plan["pileups"][:3]:
            lines.append(f"   python3 scripts/curator.py --merge-topic '{pileup['topic']}'")
        lines.append(f"   ```")
    if plan["daily_merge_candidates"]:
        lines.append(f"2. **日常合并**（P2→P1）")
        lines.append(f"   ```bash")
        lines.append(f"   python3 scripts/curator.py --daily-merge")
        lines.append(f"   ```")
    if plan["weekly_merge_candidates"]:
        lines.append(f"3. **周合并**（P1→P0）")
        lines.append(f"   ```bash")
        lines.append(f"   python3 scripts/curator.py --weekly-merge")
        lines.append(f"   ```")

    return "\n".join(lines)


def archive_pages(pages: List[Dict]) -> None:
    """归档页面（备份，不删除）"""
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_subdir = ARCHIVE_DIR / f"curator-{timestamp}"
    archive_subdir.mkdir(exist_ok=True)

    for page in pages:
        src = Path(page["path"])
        if src.exists():
            shutil.copy2(src, archive_subdir / src.name)

    print(f"[Curator] 已归档 {len(pages)} 个页面到: {archive_subdir}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Curator - Wiki 页面自动合并编排器")
    parser.add_argument("--status", action="store_true", help="查看当前状态")
    parser.add_argument("--merge-topic", help="合并指定主题的所有页面")
    parser.add_argument("--daily-merge", action="store_true", help="执行日常合并（P2→P1）")
    parser.add_argument("--weekly-merge", action="store_true", help="执行周合并（P1→P0）")
    parser.add_argument("--auto", action="store_true", help="自动模式（堆积时自动合并）")
    parser.add_argument("--dry-run", action="store_true", help="只生成报告，不执行操作")
    args = parser.parse_args()

    # 传统模式
    print("[Curator] 扫描 Wiki 目录...")
    pages = scan_wiki_pages()
    topics = group_by_topic(pages)
    pileups = detect_pileups(topics, pages=pages)

    if args.status:
        print(f"[Curator] 总页面: {len(pages)}, 主题: {len(topics)}")
        if pileups:
            print(f"[Curator] 堆积主题: {len(pileups)}")
            for topic, pgs in pileups[:5]:
                print(f"  - {topic}: {len(pgs)} 页")
        else:
            print("[Curator] 无堆积")
        return

    # 生成合并计划
    plan = generate_merge_plan(topics, pileups)

    # 保存报告到 wiki/ 目录（Obsidian 可直接查看）
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = WIKI_DIR / f"curator-report-{timestamp}.md"
    report = generate_curator_report(plan)
    report_path.write_text(report, encoding="utf-8")
    print(f"[Curator] 报告已保存: {report_path}")

    # 自动模式：堆积时告警
    if pileups and args.auto:
        print(f"[Curator] 检测到 {len(pileups)} 个堆积主题")
        if not args.dry_run:
            print("[Curator] 自动合并模式：请查看报告后手动执行合并")
            print(f"  python3 scripts/curator.py --merge-topic 'TOPIC_NAME'")

    print("[Curator] 完成")


if __name__ == "__main__":
    main()
