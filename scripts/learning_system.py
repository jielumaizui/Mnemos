#!/usr/bin/env python3

from __future__ import annotations
"""
四分离学习系统（OpenClaw P7）

维护 ~/.claude/learnings/ 下的三个文件：
- CHANGELOG.md: 系统变更记录（Git commit 自动提取）
- ERRORS.md: 错误模式库（触发式）
- LEARNINGS.md: 经验总结（触发式 + 用户确认）

用法:
  python3 scripts/learning_system.py --changelog           # 从 git log 更新 CHANGELOG
  python3 scripts/learning_system.py --add-error          # 交互式添加错误条目
  python3 scripts/learning_system.py --add-learning       # 交互式添加学习条目
  python3 scripts/learning_system.py --detect-decision "文本"  # 检测决策语言
"""

import os
import sys
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict
from core.config import get_config

sys.path.insert(0, str(Path(__file__).parent.parent))

LEARNINGS_DIR = get_config().data_dir / "learnings"
CHANGELOG_PATH = LEARNINGS_DIR / "CHANGELOG.md"
ERRORS_PATH = LEARNINGS_DIR / "ERRORS.md"
LEARNINGS_PATH = LEARNINGS_DIR / "LEARNINGS.md"

# 决策语言检测关键词（用于 AI 对话中触发 LEARNING 草稿）
DECISION_KEYWORDS = [
    "确定", "就这样", "按这个来", "决定", "选定", "选定", "采用",
    "就用", "选", "定", "确认", "拍板", "定了", "就用这个",
]


def ensure_files():
    """确保目录和文件存在"""
    LEARNINGS_DIR.mkdir(parents=True, exist_ok=True)
    for p in [CHANGELOG_PATH, ERRORS_PATH, LEARNINGS_PATH]:
        if not p.exists():
            p.write_text(f"# {p.stem.upper()}\n\n", encoding="utf-8")


def detect_decision_language(text: str) -> tuple[bool, str]:
    """检测文本中是否包含决策语言

    Returns:
        (是否检测到, 匹配到的关键词)
    """
    for kw in DECISION_KEYWORDS:
        if kw in text:
            return True, kw
    return False, ""


def extract_recent_git_commits(days: int = 7, max_count: int = 20) -> list[Dict]:
    """从 git log 提取最近 commit"""
    since = (datetime.now() - __import__('datetime').timedelta(days=days)).strftime("%Y-%m-%d")
    cmd = [
        "git", "log",
        f"--since={since}",
        f"--max-count={max_count}",
        "--format=%H|%ci|%s",
        "--no-merges"
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            return []
        commits = []
        for line in proc.stdout.strip().split("\n"):
            if "|" not in line:
                continue
            parts = line.split("|", 2)
            if len(parts) >= 3:
                commits.append({
                    "hash": parts[0][:8],
                    "date": parts[1][:10],
                    "message": parts[2].strip()
                })
        return commits
    except Exception:
        return []


def update_changelog():
    """从 git log 更新 CHANGELOG.md"""
    ensure_files()
    commits = extract_recent_git_commits(days=30, max_count=50)
    if not commits:
        print("[Learning] 最近 30 天无新 commit")
        return

    content = CHANGELOG_PATH.read_text(encoding="utf-8")

    new_entries = []
    for c in commits:
        # 避免重复：检查 hash 是否已存在
        if c["hash"] in content:
            continue
        entry = f"""## {c['date']} | {c['message']}
- **类型**: auto
- **commit**: `{c['hash']}`
- **变更**: {c['message']}

"""
        new_entries.append(entry)

    if not new_entries:
        print("[Learning] CHANGELOG 已是最新")
        return

    # 插入到文件头部（## 格式 之后）
    # 找到第一个 ## 开头的行
    lines = content.splitlines()
    insert_idx = len(lines)
    for i, line in enumerate(lines):
        if line.startswith("## "):
            insert_idx = i
            break

    new_lines = lines[:insert_idx] + [""] + [e.rstrip() for e in new_entries] + lines[insert_idx:]
    CHANGELOG_PATH.write_text("\n".join(new_lines), encoding="utf-8")
    print(f"[Learning] CHANGELOG 已更新: +{len(new_entries)} 条")


def add_error_entry(scene: str = "", symptom: str = "", root_cause: str = "",
                    fix: str = "", prevention: str = "", related_file: str = ""):
    """添加错误条目到 ERRORS.md"""
    ensure_files()
    today = datetime.now().strftime("%Y-%m-%d")

    # 生成关键词（从症状中提取前几个词）
    keywords = " ".join(symptom.split()[:3]) if symptom else "unknown"
    entry = f"""## {today} | {keywords}
- **场景**: {scene}
- **症状**: {symptom}
- **根因**: {root_cause}
- **修复**: {fix}
- **预防**: {prevention}
- **相关文件**: `{related_file}`

"""

    content = ERRORS_PATH.read_text(encoding="utf-8")
    # 插入到格式模板之后（找到 "## 格式模板" 后面的第一个空行）
    lines = content.splitlines()
    insert_idx = len(lines)
    for i, line in enumerate(lines):
        if line.startswith("## 格式模板"):
            # 找到模板结束后的空行
            for j in range(i + 1, len(lines)):
                if lines[j].strip() == "" and j + 1 < len(lines) and lines[j + 1].startswith("## "):
                    insert_idx = j + 1
                    break
            break

    new_lines = lines[:insert_idx] + [entry.rstrip(), ""] + lines[insert_idx:]
    ERRORS_PATH.write_text("\n".join(new_lines), encoding="utf-8")
    print(f"[Learning] ERRORS 已添加: {keywords}")


def add_learning_entry(question: str = "", options: str = "", decision: str = "",
                       reason: str = "", expected: str = "", verification: str = ""):
    """添加学习条目到 LEARNINGS.md"""
    ensure_files()
    today = datetime.now().strftime("%Y-%m-%d")
    keywords = question.split("?")[0][:30] if question else "unknown"

    entry = f"""## {today} | {keywords}
- **问题**: {question}
- **选项**: {options}
- **决策**: {decision}
- **原因**: {reason}
- **预期结果**: {expected}
- **后续验证**: {verification}

"""

    content = LEARNINGS_PATH.read_text(encoding="utf-8")
    lines = content.splitlines()
    insert_idx = len(lines)
    for i, line in enumerate(lines):
        if line.startswith("## 格式模板"):
            for j in range(i + 1, len(lines)):
                if lines[j].strip() == "" and j + 1 < len(lines) and lines[j + 1].startswith("## "):
                    insert_idx = j + 1
                    break
            break

    new_lines = lines[:insert_idx] + [entry.rstrip(), ""] + lines[insert_idx:]
    LEARNINGS_PATH.write_text("\n".join(new_lines), encoding="utf-8")
    print(f"[Learning] LEARNINGS 已添加: {keywords}")


def interactive_add_error():
    """交互式添加错误条目"""
    print("=== 添加错误条目 ===")
    scene = input("场景: ").strip()
    symptom = input("症状: ").strip()
    root_cause = input("根因: ").strip()
    fix = input("修复: ").strip()
    prevention = input("预防: ").strip()
    related = input("相关文件: ").strip()
    add_error_entry(scene, symptom, root_cause, fix, prevention, related)


def interactive_add_learning():
    """交互式添加学习条目"""
    print("=== 添加学习条目 ===")
    question = input("问题: ").strip()
    options = input("选项: ").strip()
    decision = input("决策: ").strip()
    reason = input("原因: ").strip()
    expected = input("预期结果: ").strip()
    verification = input("后续验证: ").strip()
    add_learning_entry(question, options, decision, reason, expected, verification)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Learning System (OpenClaw P7)")
    parser.add_argument("--changelog", action="store_true", help="从 git log 更新 CHANGELOG")
    parser.add_argument("--add-error", action="store_true", help="交互式添加错误条目")
    parser.add_argument("--add-learning", action="store_true", help="交互式添加学习条目")
    parser.add_argument("--detect-decision", help="检测文本中的决策语言")
    args = parser.parse_args()

    if args.changelog:
        update_changelog()
    elif args.add_error:
        interactive_add_error()
    elif args.add_learning:
        interactive_add_learning()
    elif args.detect_decision:
        detected, kw = detect_decision_language(args.detect_decision)
        if detected:
            print(f"[Learning] 检测到决策语言: '{kw}'")
            print("建议：整理决策上下文 → 生成 LEARNINGS.md 草稿 → 询问用户确认")
        else:
            print("[Learning] 未检测到决策语言")
    else:
        # 默认：更新 changelog + 显示统计
        ensure_files()
        print(f"[Learning] 学习系统状态:")
        for p, label in [(CHANGELOG_PATH, "CHANGELOG"), (ERRORS_PATH, "ERRORS"), (LEARNINGS_PATH, "LEARNINGS")]:
            lines = p.read_text(encoding="utf-8").splitlines()
            count = sum(1 for l in lines if l.startswith("## ") and not l.startswith("## 格式"))
            print(f"  {label}: {count} 条")


if __name__ == "__main__":
    main()
