#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P2-3 历史截断数据标记脚本

扫描 Memos 中所有包含截断标记的记录，在本地数据库创建追踪表，
并可选择通过 Memos API 给记录打上 raw_incomplete=true 标签。

截断标记来源（历史版本遗留）：
- [...内容截断...]
- [... {N} 字符已截断 ...]
- ...(truncated)
- ...(session truncated)
- [...内容过长，已截断...]
- ... (truncated, total {N} chars)
- ... (内容截断)
- ... (共 {N} 字符，已截断)
"""

import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# 历史截断标记（不同模块/版本遗留）
TRUNCATION_MARKERS = [
    r"内容过长已截断",           # [⚠️ 内容过长已截断：N 字节 → M 字节]
    r"文件内容已截断",
    r"\.\.\.\(truncated\)",
    r"\.\.\.\(session truncated\)",
    r"\[\.\.\.内容过长，已截断\.\.\.\]",
    r"\.\.\.\s*\(truncated,\s*total\s*\d+\s*chars\)",
    r"\.\.\.\s*\(内容截断\)",
    r"\.\.\.\s*\(共\s*\d+\s*字符，已截断\)",
    r"chars truncated",            # English truncated markers
]
TRUNCATION_RE = re.compile("|".join(TRUNCATION_MARKERS), re.IGNORECASE)


def _init_table(db_path: Path):
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS truncated_memos (
                memos_uid TEXT PRIMARY KEY,
                marker_snippet TEXT,
                discovered_at TEXT,
                tagged INTEGER DEFAULT 0
            )
        """)
        conn.commit()


def scan_and_record(dry_run: bool = False, tag_memos: bool = False):
    from core.config import get_config
    from integrations.styx import MemosClient

    cfg = get_config()
    client = MemosClient(token=cfg.memos_token, base_url=cfg.memos_api_url)
    db_path = cfg.data_dir / "mnemos.db"

    if not dry_run:
        _init_table(db_path)

    all_memos = client.list_all_memos()
    truncated = []
    for m in all_memos:
        content = m.get("content", "")
        if TRUNCATION_RE.search(content):
            uid = m.get("name", "").replace("memos/", "")
            truncated.append({
                "uid": uid,
                "snippet": content[:200].replace("\n", " "),
            })

    print(f"扫描完成: {len(all_memos)} 条 Memos，发现 {len(truncated)} 条截断记录")

    if dry_run:
        for t in truncated:
            print(f"  [DRY-RUN] {t['uid'][:24]}... | {t['snippet'][:80]}...")
        return

    # 写入本地数据库
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        existing = {
            r[0] for r in conn.execute("SELECT memos_uid FROM truncated_memos").fetchall()
        }
        new_count = 0
        for t in truncated:
            if t["uid"] not in existing:
                conn.execute(
                    "INSERT INTO truncated_memos (memos_uid, marker_snippet, discovered_at) VALUES (?, ?, ?)",
                    (t["uid"], t["snippet"], datetime.now().isoformat()),
                )
                new_count += 1
        conn.commit()
        print(f"本地数据库: 新增 {new_count} 条，已有 {len(existing)} 条")

    # 可选：通过 Memos API 打标签
    if tag_memos:
        tagged = 0
        for t in truncated:
            try:
                # 读取现有内容，追加标签
                memo = client.get_by_uid(t["uid"])
                if memo and "raw_incomplete=true" not in str(memo.tags):
                    new_tags = list(memo.tags) + ["raw_incomplete=true"]
                    # MemosClient 可能没有 update 方法，用底层 API
                    client._make_request(
                        "PATCH",
                        f"{client.base_url}/api/v1/memos/{t['uid']}",
                        json={"tags": new_tags},
                    )
                    tagged += 1
            except Exception as e:
                print(f"  标签失败 {t['uid']}: {e}")
        print(f"Memos 标签: 已标记 {tagged} 条")

    # 更新 tagged 状态
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        for t in truncated:
            conn.execute(
                "UPDATE truncated_memos SET tagged = 1 WHERE memos_uid = ?",
                (t["uid"],),
            )
        conn.commit()


def get_truncated_count() -> int:
    """供 doctor 调用"""
    from core.config import get_config
    db_path = get_config().data_dir / "mnemos.db"
    if not db_path.exists():
        return 0
    try:
        with sqlite3.connect(str(db_path), timeout=5) as conn:
            row = conn.execute("SELECT COUNT(*) FROM truncated_memos").fetchone()
            return row[0] if row else 0
    except Exception:
        return 0


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    tag = "--tag" in sys.argv
    scan_and_record(dry_run=dry, tag_memos=tag)
