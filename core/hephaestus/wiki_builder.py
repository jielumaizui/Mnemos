#!/usr/bin/env python3
"""
Wiki Builder - L1 → L2 蒸馏：将 Memos 原始会话转换为 Obsidian Wiki Markdown

流程：
    1. 从 Memos 查询 level=L1 记录
    2. 按 session= 标签分组，重建完整会话
    3. Session 完成检测（最新 chunk 创建时间 > 5分钟）
    4. 过滤 skip-distill 标签（回流防护）
    5. 质量评分（低于门槛跳过）
    6. 相似度拦截（与已有 Wiki 页面 >85% 相似则跳过）
    7. 生成 wiki/sources/{session_id}.md
    8. 更新 wiki/index.md 和 wiki/log.md
    9. Git 自动提交（如配置了 git repo）

用法：
    python3 wiki_builder.py              # 执行一轮蒸馏
    python3 wiki_builder.py --watch      # 守护模式，每5分钟执行
    python3 wiki_builder.py --stats      # 查看统计
"""

# Wiki Builder - L1 → L2 蒸馏：将 Memos 原始会话转换为 Obsidian Wiki Markdown
# 原模块: memos-client/wiki_builder.py

import os
import sys
import json
import sqlite3
import hashlib
import argparse
import time
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from integrations.styx import MemosClient
from core.config import get_config
from core.kia.ingest_helpers import score_message_quality

from core.kia.assertion_extractor import (
    extract_from_messages, merge_similar_assertions, Assertion, KnowledgeForm
)
from core.kia.conflict_resolver import detect_conflicts


def _get_wiki_dir() -> Path:
    return get_config().wiki_dir

def _get_inbox_dir() -> Path:
    return _get_wiki_dir() / "00-Inbox"

def _get_wiki_db() -> Path:
    return Path.home() / ".mnemos" / "wiki_state.db"

QUALITY_THRESHOLD = 45.0
COMPLETION_TIMEOUT = 300  # 5分钟
SIMILARITY_THRESHOLD = 0.85
MAX_SESSION_CHUNKS = 200


def _ensure_wiki_dirs():
    """确保 Wiki 目录结构存在"""
    for subdir in ["00-Inbox", "01-People", "02-Projects", "03-Tech", "04-Concepts", "05-MOCs", "06-Retrospectives"]:
        (_get_wiki_dir() / subdir).mkdir(parents=True, exist_ok=True)


# ========== SQLite 状态管理 ==========

def _get_conn():
    return sqlite3.connect(str(_get_wiki_db()), timeout=10)


def _is_session_completed(session_id: str, memos: List[Dict]) -> bool:
    """检测 session 是否已完成（最新 chunk 超过 5 分钟）"""
    if not memos:
        return False

    # 获取最新 chunk 的创建时间
    latest_time = None
    for memo in memos:
        create_time = memo.get("createTime", "")
        if create_time:
            try:
                # 解析 ISO 格式时间
                t = datetime.fromisoformat(create_time.replace("Z", "+00:00"))
                if latest_time is None or t > latest_time:
                    latest_time = t
            except Exception:
                pass

    if not latest_time:
        return False

    now = datetime.now(timezone.utc)
    elapsed = (now - latest_time).total_seconds()
    return elapsed > COMPLETION_TIMEOUT


def _is_processed(session_id: str) -> bool:
    """检查 session 是否已处理过"""
    try:
        with _get_conn() as conn:
            cursor = conn.execute(
                "SELECT 1 FROM processed_sessions WHERE session_id = ?",
                (session_id,)
            )
            return cursor.fetchone() is not None
    except Exception:
        return False


def _mark_processed(session_id: str, source: str, message_count: int,
                    quality_score: float, wiki_path: str = "", method: str = "rule") -> None:
    """标记 session 已处理"""
    try:
        with _get_conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO processed_sessions
                   (session_id, source, message_count, quality_score,
                    processed_at, distill_method)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, source, message_count, quality_score,
                 datetime.now().isoformat(), method)
            )
            # 同时记录 wiki 页面
            if wiki_path:
                conn.execute(
                    """INSERT OR REPLACE INTO wiki_pages
                       (page_id, file_path, type, source_session, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (session_id, wiki_path, "source", session_id,
                     datetime.now().isoformat(), datetime.now().isoformat())
                )
            conn.commit()
    except Exception as e:
        print(f"  [WikiBuilder] 标记处理状态失败: {e}")


def _log(session_id: str, action: str, detail: str = "") -> None:
    """记录处理日志到 log.md"""
    log_path = _get_wiki_dir() / "log.md"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"- [{timestamp}] `{session_id[:8]}` {action}: {detail}\n"

    try:
        if log_path.exists():
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line)
        else:
            with open(log_path, "w", encoding="utf-8") as f:
                f.write("# Wiki Build Log\n\n")
                f.write(line)
    except Exception:
        pass


# ========== Memos 查询与会话重建 ==========

def fetch_l1_sessions(client: MemosClient) -> Dict[str, List[Dict]]:
    """从 Memos 获取所有 level=L1 的记录，按 session= 标签分组"""
    print("[WikiBuilder] 查询 Memos 中 L1 记录...")
    try:
        all_memos = client.list_all_memos()
    except Exception as e:
        print(f"[WikiBuilder] 查询失败: {e}")
        return {}

    sessions: Dict[str, List[Dict]] = {}
    skipped = 0

    for memo in all_memos:
        tags = memo.get("tags", [])
        tag_set = set(tags)

        if "level=L1" not in tag_set:
            skipped += 1
            continue

        session_id = ""
        for tag in tags:
            if tag.startswith("session="):
                session_id = tag.split("=", 1)[1]
                break

        if not session_id:
            continue

        sessions.setdefault(session_id, []).append(memo)

    print(f"[WikiBuilder] 总计记录: {len(all_memos)}, 跳过非L1: {skipped}, "
          f"待处理 session 数: {len(sessions)}")
    return sessions


def _try_parse_json(content: str) -> Optional[Dict]:
    """尝试解析可能被截断的 JSON，Memos 截断在 ~499 字符"""
    content = content.strip()
    if not content.startswith("{"):
        return None

    # 1. 标准解析
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # 2. 正则提取 _meta.segment 用于排序
    segment = None
    seg_match = re.search(r'"segment"\s*:\s*"([^"]+)"', content)
    if seg_match:
        segment = seg_match.group(1)

    # 3. 尝试正则提取 messages 数组（完整片段可能包含）
    try:
        match = re.search(r'"messages"\s*:\s*(\[[\s\S]*?\])(?:\s*,\s*"|\s*\})', content)
        if match:
            msgs = json.loads(match.group(1))
            return {"_meta": {"segment": segment or "1/1"}, "messages": msgs}
    except Exception:
        pass

    # 4. Fallback：逐条正则提取 role/content 对（处理严重截断）
    messages = []
    # 找所有 role 字段位置
    for m in re.finditer(r'"role"\s*:\s*"([^"]*)"', content):
        role = m.group(1)
        pos = m.end()
        # 从 role 之后找最近的 content 字段
        content_match = re.search(r'"content"\s*:\s*"([^"]*)"', content[pos:pos+300])
        if content_match:
            messages.append({
                "role": role,
                "content": content_match.group(1)
            })

    if messages:
        return {"_meta": {"segment": segment or "1/1"}, "messages": messages}

    return None


def _clean_message_content(content: str) -> str:
    """清理消息内容：移除 thinking 块、代码块、shell 命令等非断言内容"""
    if not content:
        return ""

    # 1. 移除 [thinking]...[/thinking] 或 [thinking]... 块
    content = re.sub(r'\[thinking\].*?(?:\[/thinking\]|$)', '', content, flags=re.DOTALL)

    # 2. 移除代码块 ```...```
    content = re.sub(r'```.*?```', '', content, flags=re.DOTALL)

    # 3. 移除单行 shell 命令（curl, chmod, wget, npm, pip 等）
    content = re.sub(r'^(curl|chmod|wget|npm|pip|pip3|docker|git|mkdir|cd|ls|cat|rm|mv|cp)\s+.+$', '', content, flags=re.MULTILINE)

    # 4. 移除纯数字/编号行（如 "5." 后面没有实质内容）
    content = re.sub(r'^\s*\d+\.\s*$', '', content, flags=re.MULTILINE)

    # 5. 清理多余空行
    content = re.sub(r'\n{3,}', '\n\n', content)

    return content.strip()


def reconstruct_session(memos: List[Dict]) -> Tuple[List[Dict], Dict]:
    """从一个 session 的所有 chunk 中重建完整消息列表"""
    all_messages = []
    meta = {
        "source": "", "model": "", "cwd": "", "session_id": "",
        "total_chunks": len(memos), "has_skip_distill": False,
    }

    def _sort_key(m):
        content = m.get("content", "")
        data = _try_parse_json(content)
        if data:
            seg = data.get("_meta", {}).get("segment", "1/1")
            return int(seg.split("/")[0])
        return 0

    sorted_memos = sorted(memos, key=_sort_key)

    for memo in sorted_memos:
        tags = memo.get("tags", [])
        for tag in tags:
            if tag.startswith("source=") and not meta["source"]:
                meta["source"] = tag.split("=", 1)[1]
            elif tag.startswith("model=") and not meta["model"]:
                meta["model"] = tag.split("=", 1)[1]
            elif tag.startswith("cwd=") and not meta["cwd"]:
                meta["cwd"] = tag.split("=", 1)[1]
            elif tag.startswith("session=") and not meta["session_id"]:
                meta["session_id"] = tag.split("=", 1)[1]

            # 检测回流防护标记
            if tag == "skip-distill=true":
                meta["has_skip_distill"] = True

        content = memo.get("content", "")
        data = _try_parse_json(content)
        if data:
            msgs = data.get("messages", [])
            if isinstance(msgs, list):
                for msg in msgs:
                    if isinstance(msg, dict) and "content" in msg:
                        msg["content"] = _clean_message_content(msg["content"])
                all_messages.extend(msgs)
        else:
            cleaned = _clean_message_content(content)
            if cleaned:
                all_messages.append({
                    "role": "system",
                    "content": cleaned[:500],
                    "timestamp": memo.get("createTime", ""),
                })

    return all_messages, meta


# ========== 质量评估 ==========

def score_session(messages: List[Dict]) -> Tuple[float, Dict]:
    """对整个会话进行质量评分"""
    if not messages:
        return 0.0, {"total_messages": 0, "valid_messages": 0}

    scores = []
    valid_count = 0
    for msg in messages:
        content = msg.get("content", "")
        if not content:
            continue
        result = score_message_quality(content)
        scores.append(result.get("total_score", 0))
        if result.get("total_score", 0) >= QUALITY_THRESHOLD:
            valid_count += 1

    if not scores:
        return 0.0, {"total_messages": len(messages), "valid_messages": 0}

    avg_score = sum(scores) / len(scores)
    return avg_score, {
        "total_messages": len(messages),
        "valid_messages": valid_count,
        "avg_score": round(avg_score, 1),
        "min_score": round(min(scores), 1),
        "max_score": round(max(scores), 1),
    }


# ========== 相似度检测 ==========

def _compute_similarity(text1: str, text2: str) -> float:
    """计算两段文本的相似度"""
    return SequenceMatcher(None, text1, text2).ratio()


def _find_similar_source(content: str, threshold: float = SIMILARITY_THRESHOLD) -> Optional[str]:
    """查找与已有 source 文件相似度超过门槛的页面"""
    inbox_dir = _get_wiki_dir() / "00-Inbox"
    if not inbox_dir.exists():
        return None

    for md_file in inbox_dir.glob("*.md"):
        try:
            existing = md_file.read_text(encoding="utf-8")
            similarity = _compute_similarity(content, existing)
            if similarity > threshold:
                return f"{md_file.name} ({similarity:.1%})"
        except Exception:
            pass

    return None


# ========== Markdown 生成 ==========

def _infer_temporal_scope(assertions: List[Assertion]) -> str:
    """
    推断知识的时效类型

    规则（按优先级）：
    - 包含版本号 → version-bound
    - 包含 "永远"、"绝对"、"本质" → permanent
    - 包含 "目前"、"当前"、"现阶段" → contextual
    - 包含 "通常"、"一般"、"大多数情况下" → stable
    - 默认 → contextual
    """
    all_claims = " ".join(a.claim for a in assertions)

    # version-bound 信号
    if re.search(r'v\d+\.?\d*|version\s+\d+|版本\s*\d', all_claims, re.I):
        return "version-bound"

    # permanent 信号
    permanent_signals = ['永远', '绝对', '本质', '根本', ' invariably ', 'always', 'fundamental']
    if any(s in all_claims for s in permanent_signals):
        return "permanent"

    # contextual 信号
    contextual_signals = ['目前', '当前', '现阶段', '这次', '当下', '现在', 'currently', 'at present']
    if any(s in all_claims for s in contextual_signals):
        return "contextual"

    # stable 信号
    stable_signals = ['通常', '一般', '大多数情况下', '普遍', 'normally', 'generally', 'usually']
    if any(s in all_claims for s in stable_signals):
        return "stable"

    return "contextual"


def _extract_one_liner(assertions: List[Assertion]) -> str:
    """从断言中提取一句话结论"""
    if not assertions:
        return ""
    # 优先使用置信度最高的断言
    best = max(assertions, key=lambda a: a.confidence)
    return best.claim


def _generate_form_page(session_id: str, form: KnowledgeForm,
                        assertions: List[Assertion], meta: Dict) -> str:
    """为特定知识形态生成 Markdown 页面"""
    lines = []
    source = meta.get("source", "unknown")
    one_liner = _extract_one_liner(assertions)

    # 推断时效类型
    temporal_scope = _infer_temporal_scope(assertions)

    # Frontmatter
    lines.append("---")
    lines.append(f'type: {form.value}')
    lines.append(f'source_session: {session_id}')
    lines.append(f'source_agent: {source}')
    lines.append(f'status: active')
    lines.append(f'stage: captured')
    lines.append(f'evidence: single-source')
    lines.append(f'temporal: {temporal_scope}')
    lines.append(f'level: L3')
    lines.append(f'created: {datetime.now().strftime("%Y-%m-%d")}')
    lines.append("---")
    lines.append("")

    # 一句话结论
    lines.append(f"# {one_liner[:80]}{'...' if len(one_liner) > 80 else ''}")
    lines.append("")

    # 背景/上下文
    lines.append("## 背景")
    lines.append("")
    # 提取断言的上下文作为背景
    contexts = []
    for a in assertions:
        if a.context and a.context not in contexts:
            contexts.append(a.context)
    for ctx in contexts[:3]:
        # 过滤回流标记
        if "<wiki-context" in ctx:
            continue
        # 只保留前 200 字
        lines.append(ctx[:200] + ("..." if len(ctx) > 200 else ""))
        lines.append("")

    # 核心断言
    lines.append("## 核心内容")
    lines.append("")
    for i, a in enumerate(assertions, 1):
        prefix = "⚠️ " if a.is_negated else ""
        lines.append(f"{prefix}{i}. {a.claim}")
        if a.boundary_hint:
            lines.append(f"   > 边界: {a.boundary_hint}")
    lines.append("")

    # 边界与反例
    boundaries = [a.boundary_hint for a in assertions if a.boundary_hint]
    if boundaries:
        lines.append("## 边界与反例")
        lines.append("")
        for b in boundaries:
            lines.append(f"- {b}")
        lines.append("")

    # 演化历史
    lines.append("## 演化历史")
    lines.append("")
    lines.append("- v1: 初始记录")
    lines.append("")

    # 关联
    # 从断言中提取关键词作为潜在关联
    all_claims = " ".join(a.claim for a in assertions)
    keywords = re.findall(r'[a-zA-Z]{3,}', all_claims)
    unique_kw = list(set(k.lower() for k in keywords if len(k) > 3))[:8]
    if unique_kw:
        lines.append("## 潜在关联")
        lines.append("")
        for kw in unique_kw:
            lines.append(f"- [[{kw}]]")
        lines.append("")

    return "\n".join(lines)


def generate_wiki_pages(session_id: str, messages: List[Dict], meta: Dict,
                        quality_score: float, score_detail: Dict) -> List[Tuple[str, str]]:
    """
    生成 Wiki 页面列表（六种知识形态）

    Returns:
        [(page_id, md_content), ...]
    """
    # 1. 提取断言
    assertions = extract_from_messages(messages, session_id)

    # 2. 合并相似断言
    assertions = merge_similar_assertions(assertions, similarity_threshold=0.85)

    # 3. 过滤低置信度断言
    assertions = [a for a in assertions if a.confidence >= 0.4]

    if not assertions:
        return []

    # 4. 按形态分组
    form_groups: Dict[KnowledgeForm, List[Assertion]] = {}
    for a in assertions:
        form_groups.setdefault(a.form, []).append(a)

    # 5. 为每个形态生成页面（至少2个断言才生成）
    pages = []
    for form, form_assertions in form_groups.items():
        if len(form_assertions) < 1:
            continue

        page_id = f"{session_id[:8]}_{form.value}"
        md_content = _generate_form_page(session_id, form, form_assertions, meta)
        pages.append((page_id, md_content))

    # 6. 如果只有一个形态且只有一个断言，也生成（避免空session）
    if not pages and assertions:
        a = assertions[0]
        page_id = f"{session_id[:8]}_{a.form.value}"
        md_content = _generate_form_page(session_id, a.form, [a], meta)
        pages.append((page_id, md_content))

    return pages


# 保留旧的 generate_source_md 作为兼容入口（但不再使用）
def generate_source_md(session_id: str, messages: List[Dict], meta: Dict,
                       quality_score: float, score_detail: Dict) -> str:
    """[兼容入口] 生成 Source 页面 Markdown - 已废弃，请使用 generate_wiki_pages"""
    pages = generate_wiki_pages(session_id, messages, meta, quality_score, score_detail)
    if pages:
        return pages[0][1]
    return ""


def update_index_md():
    """更新 wiki/index.md"""
    index_path = _get_wiki_dir() / "index.md"
    inbox_dir = _get_wiki_dir() / "00-Inbox"

    lines = ["# Wiki Index", ""]

    # Sources
    if inbox_dir.exists():
        md_files = sorted(inbox_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        lines.append(f"## Inbox ({len(md_files)} sessions)")
        lines.append("")
        for md_file in md_files[:50]:  # 最近 50 个
            try:
                content = md_file.read_text(encoding="utf-8")
                # 提取 frontmatter 中的 source_agent
                agent = "unknown"
                m = re.search(r'^source_agent:\s*(.+)$', content, re.MULTILINE)
                if m:
                    agent = m.group(1).strip()
                quality = "?"
                m = re.search(r'^quality_score:\s*([\d.]+)$', content, re.MULTILINE)
                if m:
                    quality = m.group(1).strip()

                name = md_file.stem[:16]
                lines.append(f"- [[{name}]] ({agent}, Q:{quality})")
            except Exception:
                pass
        lines.append("")

    # Stats
    try:
        with _get_conn() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM processed_sessions")
            total = cursor.fetchone()[0]
            cursor = conn.execute("SELECT AVG(quality_score) FROM processed_sessions")
            avg_q = cursor.fetchone()[0] or 0
            lines.append(f"## Stats")
            lines.append(f"- Total sessions: {total}")
            lines.append(f"- Avg quality: {avg_q:.1f}")
            lines.append(f"- Last update: {datetime.now().isoformat()}")
    except Exception:
        pass

    index_path.write_text("\n".join(lines), encoding="utf-8")


# ========== Git 自动提交 ==========

def _git_auto_commit():
    """自动提交 Wiki 变更到 git"""
    try:
        if not (_get_wiki_dir() / ".git").exists():
            return  # 未初始化 git

        # 检查是否有变更
        result = subprocess.run(
            ["git", "diff", "--quiet"],
            cwd=_get_wiki_dir(),
            capture_output=True
        )
        if result.returncode == 0:
            return  # 无变更

        subprocess.run(
            ["git", "add", "."],
            cwd=_get_wiki_dir(),
            capture_output=True
        )
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        subprocess.run(
            ["git", "commit", "-m", f"wiki auto-build {timestamp}"],
            cwd=_get_wiki_dir(),
            capture_output=True
        )
        print(f"[WikiBuilder] Git committed at {timestamp}")
    except Exception:
        pass


# ========== 主流程 ==========

def run_build_cycle(client: MemosClient, dry_run: bool = False) -> Dict:
    """执行一轮 Wiki 构建"""
    _ensure_wiki_dirs()
    sessions = fetch_l1_sessions(client)

    stats = {"processed": 0, "skipped_low_quality": 0, "skipped_incomplete": 0,
             "skipped_similar": 0, "skipped_distill": 0, "failed": 0}

    for session_id, memos in sessions.items():
        if len(memos) > MAX_SESSION_CHUNKS:
            print(f"  [WikiBuilder] {session_id[:8]}: chunk 过多 ({len(memos)}), 跳过")
            _log(session_id, "skip", f"too_many_chunks:{len(memos)}")
            continue

        # Session 完成检测
        if not _is_session_completed(session_id, memos):
            stats["skipped_incomplete"] += 1
            continue

        # 跳过已处理
        if _is_processed(session_id):
            continue

        print(f"[WikiBuilder] 处理 session {session_id[:8]} ({len(memos)} chunks)...")

        # 重建会话
        messages, meta = reconstruct_session(memos)
        if not messages:
            print(f"  [WikiBuilder] 无有效消息，跳过")
            continue

        # 回流防护：跳过带有 skip-distill 标记的 session
        if meta.get("has_skip_distill"):
            print(f"  [WikiBuilder] 检测到 skip-distill 标记，跳过（回流防护）")
            _mark_processed(session_id, meta.get("source", "unknown"),
                           len(messages), 0, method="skipped_distill")
            _log(session_id, "skip", "skip-distill")
            stats["skipped_distill"] += 1
            continue

        # 质量评分
        avg_score, score_detail = score_session(messages)
        print(f"  [WikiBuilder] 质量评分: {avg_score:.1f} "
              f"(messages={score_detail['total_messages']}, "
              f"valid={score_detail['valid_messages']})")

        if avg_score < QUALITY_THRESHOLD:
            print(f"  [WikiBuilder] 质量分低于门槛 ({QUALITY_THRESHOLD})，跳过")
            _mark_processed(session_id, meta.get("source", "unknown"),
                           len(messages), avg_score, method="skipped_low_quality")
            _log(session_id, "skip_low_quality", f"score:{avg_score:.1f}")
            stats["skipped_low_quality"] += 1
            continue

        # 生成 Wiki 页面（六种知识形态）
        pages = generate_wiki_pages(session_id, messages, meta, avg_score, score_detail)
        if not pages:
            print(f"  [WikiBuilder] 未提取到有效断言，跳过")
            continue

        if dry_run:
            total_chars = sum(len(c) for _, c in pages)
            print(f"  [WikiBuilder] [DRY RUN] 将生成 {len(pages)} 个页面 ({total_chars} chars)")
            stats["processed"] += len(pages)
            continue

        # 写入多个页面文件
        created_pages = 0
        for page_id, md_content in pages:
            # 相似度拦截
            similar = _find_similar_source(md_content)
            if similar:
                print(f"  [WikiBuilder] 页面 {page_id} 与已有页面相似度过高: {similar}，跳过")
                _log(session_id, "skip_similar", f"{page_id}: {similar}")
                stats["skipped_similar"] += 1
                continue

            source_path = _get_wiki_dir() / "00-Inbox" / f"{page_id}.md"
            try:
                source_path.write_text(md_content, encoding="utf-8")
                _mark_processed(session_id, meta.get("source", "unknown"),
                               len(messages), avg_score, str(source_path))
                _log(session_id, "created", f"00-Inbox/{page_id}.md, Q:{avg_score:.1f}")
                print(f"  [WikiBuilder] 页面已创建: 00-Inbox/{page_id}.md")
                created_pages += 1
            except Exception as e:
                print(f"  [WikiBuilder] 写入失败: {e}")
                _log(session_id, "error", f"{page_id}: {e}")
                stats["failed"] += 1

        stats["processed"] += created_pages

    # 更新索引
    if stats["processed"] > 0:
        update_index_md()
        _git_auto_commit()

    return stats


def get_stats() -> Dict:
    """获取处理统计"""
    try:
        with _get_conn() as conn:
            cursor = conn.execute(
                "SELECT COUNT(*), AVG(quality_score) FROM processed_sessions"
            )
            total, avg_score = cursor.fetchone()

            cursor = conn.execute(
                "SELECT COUNT(*) FROM wiki_pages WHERE type = 'source'"
            )
            source_count = cursor.fetchone()[0]

            return {
                "total_processed": total or 0,
                "avg_quality_score": round(avg_score or 0, 1),
                "source_pages": source_count or 0,
                "wiki_dir": str(_get_wiki_dir()),
            }
    except Exception as e:
        return {"error": str(e)}


# ========== CLI ==========

def main():
    global QUALITY_THRESHOLD
    parser = argparse.ArgumentParser(description="Wiki Builder - L1 to Wiki Markdown")
    parser.add_argument("--watch", action="store_true", help="守护模式，每5分钟执行")
    parser.add_argument("--dry-run", action="store_true", help="试运行，不写入")
    parser.add_argument("--stats", action="store_true", help="查看统计")
    parser.add_argument("--threshold", type=float, default=QUALITY_THRESHOLD,
                        help=f"质量分门槛 (默认 {QUALITY_THRESHOLD})")
    args = parser.parse_args()

    QUALITY_THRESHOLD = args.threshold

    token = os.getenv("MEMOS_TOKEN")
    if not token:
        print("ERROR: MEMOS_TOKEN 环境变量未设置")
        sys.exit(1)

    client = MemosClient(token=token, agent="wiki-builder")

    if args.stats:
        stats = get_stats()
        print("\n=== Wiki Builder 统计 ===")
        for k, v in stats.items():
            print(f"  {k}: {v}")
        return

    if args.watch:
        print(f"[WikiBuilder] 守护模式启动，质量门槛: {QUALITY_THRESHOLD}")
        while True:
            print(f"\n=== {datetime.now().isoformat()} ===")
            stats = run_build_cycle(client, dry_run=args.dry_run)
            print(f"结果: processed={stats['processed']}, "
                  f"incomplete={stats['skipped_incomplete']}, "
                  f"low_q={stats['skipped_low_quality']}, "
                  f"similar={stats['skipped_similar']}, "
                  f"distill={stats['skipped_distill']}, "
                  f"failed={stats['failed']}")
            time.sleep(300)
    else:
        stats = run_build_cycle(client, dry_run=args.dry_run)
        print(f"\n=== Wiki 构建完成 ===")
        print(f"  已处理: {stats['processed']}")
        print(f"  未完成: {stats['skipped_incomplete']}")
        print(f"  质量跳过: {stats['skipped_low_quality']}")
        print(f"  相似跳过: {stats['skipped_similar']}")
        print(f"  回流跳过: {stats['skipped_distill']}")
        print(f"  失败: {stats['failed']}")


if __name__ == "__main__":
    main()
