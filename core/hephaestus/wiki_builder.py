#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import logging
"""
Wiki Builder - L1 → L2 蒸馏：将 Memos 原始会话转换为 Obsidian Wiki Markdown

流程：
    1. 从 Memos 查询 layer=L1 记录
    2. 按 session= 标签分组，重建完整会话
    3. Session 完成检测（最新 chunk 创建时间 > 5分钟）
    4. 回流防护（skip-distill 标签 + Wiki 引用检测）
    5. 七层蒸馏流水线（噪音→预判→LLM判断→提取→自检→关联→反馈）
    6. 相似度拦截（与已有 Wiki 页面 >85% 相似则跳过）
    7. 生成 wiki 页面
    8. 更新 wiki/index.md 和 wiki/log.md
    9. Git 自动提交（如配置了 git repo）

写模式：
    - create: 新建页面（默认）
    - merge: 合并到已有页面
    - incremental: 增量更新已有页面
"""

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

logger = logging.getLogger(__name__)
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from integrations.styx import MemosClient
from core.config import get_config

from core.kia.assertion_extractor import (
    extract_from_messages, merge_similar_assertions, Assertion, KnowledgeForm,
)
from core.kia.conflict_resolver import detect_conflicts
from core.hephaestus.distillation_engine import (
    DistillationEngine, DistillationResult, KnowledgeFragment,
    generate_wiki_page, build_session_text,
)
from core.hephaestus.evolution_tracker import RecirculationGuard


def _get_wiki_dir() -> Path:
    return get_config().wiki_dir

def _get_inbox_dir() -> Path:
    return _get_wiki_dir() / "00-Inbox"

def _get_wiki_db() -> Path:
    return Path.home() / ".mnemos" / "wiki_state.db"

COMPLETION_TIMEOUT = 300  # 5分钟
SIMILARITY_THRESHOLD = 0.85
MAX_SESSION_CHUNKS = 200


def _ensure_wiki_dirs():
    """确保 Wiki 目录结构存在"""
    for subdir in ["00-Inbox", "01-People", "02-Projects", "03-Tech",
                   "04-Concepts", "05-MOCs", "06-Retrospectives"]:
        (_get_wiki_dir() / subdir).mkdir(parents=True, exist_ok=True)


# ========== SQLite 状态管理 ==========

def _get_conn():
    return sqlite3.connect(str(_get_wiki_db()), timeout=10)


def _is_session_completed(session_id: str, memos: List[Dict]) -> bool:
    """检测 session 是否已完成（最新 chunk 超过 5 分钟）"""
    if not memos:
        return False
    latest_time = None
    for memo in memos:
        create_time = memo.get("createTime", "")
        if create_time:
            try:
                t = datetime.fromisoformat(create_time.replace("Z", "+00:00"))
                if latest_time is None or t > latest_time:
                    latest_time = t
            except Exception:
                logging.getLogger(__name__).warning(f"Caught unexpected error", exc_info=True)
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
                (session_id,),
            )
            return cursor.fetchone() is not None
    except Exception:
        logging.getLogger(__name__).warning(f"Caught unexpected error at wiki_builder.py", exc_info=True)
        return False


def _mark_processed(session_id: str, source: str, message_count: int,
                    quality_score: float, wiki_path: str = "",
                    method: str = "pipeline") -> None:
    """标记 session 已处理"""
    try:
        with _get_conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO processed_sessions
                   (session_id, source, message_count, quality_score,
                    processed_at, distill_method)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, source, message_count, quality_score,
                 datetime.now().isoformat(), method),
            )
            if wiki_path:
                conn.execute(
                    """INSERT OR REPLACE INTO wiki_pages
                       (page_id, file_path, type, source_session, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (session_id, wiki_path, "source", session_id,
                     datetime.now().isoformat(), datetime.now().isoformat()),
                )
            conn.commit()
    except Exception as e:
        logger.warning(f"  [WikiBuilder] 标记处理状态失败: {e}")


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
        logging.getLogger(__name__).warning(f"Caught unexpected error at wiki_builder.py", exc_info=True)
        pass


# ========== Memos 查询与会话重建 ==========

def fetch_l1_sessions(client: MemosClient) -> Dict[str, List[Dict]]:
    """从 Memos 获取所有 layer=L1 的记录，按 session= 标签分组"""
    logger.info("[WikiBuilder] 查询 Memos 中 L1 记录...")
    try:
        all_memos = client.list_all_memos()
    except Exception as e:
        logger.warning(f"[WikiBuilder] 查询失败: {e}")
        return {}

    sessions: Dict[str, List[Dict]] = {}
    skipped = 0

    for memo in all_memos:
        tags = memo.get("tags", [])
        tag_set = set(tags)
        if "layer=L1" not in tag_set:
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
    """尝试解析可能被截断的 JSON（save_session_full 格式兼容）"""
    content = content.strip()
    if not content.startswith("{"):
        return None
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    seg_match = re.search(r'"segment"\s*:\s*"([^"]+)"', content)
    segment = seg_match.group(1) if seg_match else None
    try:
        match = re.search(r'"messages"\s*:\s*(\[[\s\S]*?\])(?:\s*,\s*"|\s*\})', content)
        if match:
            msgs = json.loads(match.group(1))
            return {"_meta": {"segment": segment or "1/1"}, "messages": msgs}
    except Exception:
        logging.getLogger(__name__).warning(f"Caught unexpected error", exc_info=True)
        pass
    messages = []
    for m in re.finditer(r'"role"\s*:\s*"([^"]*)"', content):
        role = m.group(1)
        pos = m.end()
        content_match = re.search(r'"content"\s*:\s*"([^"]*)"', content[pos:pos+300])
        if content_match:
            messages.append({"role": role, "content": content_match.group(1)})
    if messages:
        return {"_meta": {"segment": segment or "1/1"}, "messages": messages}
    return None


def _parse_markdown_turns(content: str) -> Optional[List[Dict]]:
    """从 sync_engine 生成的 Markdown 内容中提取消息列表。

    支持两种格式：
    1. 标准格式（build_turn_markdown 生成）：
        ## Turn N
        **User** (model):\n\ncontent\n\n**Assistant**:\n\ncontent\n\n---\n
    2. 简化格式（旧数据/短内容）：
        **User** (model):\n\ncontent\n\n**Assistant**:\n\ncontent\n\n---\n
    3. 分片格式（save_long_content 生成）：
        [N/M] «title»\n\n...（上述格式之一）

    Returns:
        List[{"role": "user"|"assistant", "content": str}] 或 None
    """
    if not content or not content.strip():
        return None

    # 移除分片前缀 [N/M] «title»
    content = re.sub(r'^\[\d+/\d+\] «[^»]+»\s*\n\n', '', content, count=1)

    # 如果内容以 { 开头，说明是 JSON，不应走 Markdown 解析
    if content.strip().startswith("{"):
        return None

    messages = []

    # 模式1：带 ## Turn N 标题的格式
    # 按 "## Turn" 分割，但保留第一个块（可能在标题前）
    turn_blocks = re.split(r'\n## Turn\s+\d+\s*\n', content)
    if len(turn_blocks) > 1:
        # 有明确的 Turn 标题
        for block in turn_blocks[1:]:  # 跳过第一个空块或前缀
            block_msgs = _extract_messages_from_block(block)
            if block_msgs:
                messages.extend(block_msgs)
        if messages:
            return messages

    # 模式2：无 Turn 标题，整个内容就是一个 turn
    # 尝试从整个内容提取 User/Assistant 对
    block_msgs = _extract_messages_from_block(content)
    if block_msgs:
        return block_msgs

    return None


def _extract_messages_from_block(block: str) -> List[Dict]:
    """从单个 Markdown 块中提取 User/Assistant 消息对。"""
    messages = []
    block = block.strip()
    if not block:
        return messages

    # 匹配 **User** (model):\n\ncontent\n\n**Assistant**:\n\ncontent
    # 使用非贪婪匹配，但支持多行内容
    # 分隔符可以是 **Assistant**: 或 **User** 或 --- 或文件末尾
    user_pattern = r'\*\*User\*\*\s*(?:\([^)]+\))?\s*:\s*\n\n(.*?)(?=\n\n\*\*Assistant\*\*|\n\n---|\n\n\*\*User\*\*|\Z)'
    assistant_pattern = r'\*\*Assistant\*\*\s*(?:\([^)]+\))?\s*:\s*\n\n(.*?)(?=\n\n\*\*User\*\*|\n\n---|\n\n\*\*Assistant\*\*|\Z)'

    # 找到所有 User 和 Assistant 的位置
    user_matches = list(re.finditer(user_pattern, block, re.DOTALL))
    assistant_matches = list(re.finditer(assistant_pattern, block, re.DOTALL))

    # 按在文本中出现的顺序交错合并
    all_matches = []
    for m in user_matches:
        all_matches.append((m.start(), "user", m.group(1).strip()))
    for m in assistant_matches:
        all_matches.append((m.start(), "assistant", m.group(1).strip()))

    all_matches.sort(key=lambda x: x[0])

    for _, role, text in all_matches:
        if text:
            messages.append({"role": role, "content": text})

    return messages


def _clean_message_content(content: str) -> str:
    """清理消息内容"""
    if not content:
        return ""
    content = re.sub(r'\[thinking\].*?(?:\[/thinking\]|$)', '', content, flags=re.DOTALL)
    content = re.sub(r'```.*?```', '', content, flags=re.DOTALL)
    content = re.sub(
        r'^(?!.*[\u4e00-\u9fff])\s*(curl|chmod|wget|npm|pip|pip3|docker|git|mkdir|cd|ls|cat|rm|mv|cp)\b.+$',
        '', content, flags=re.MULTILINE,
    )
    content = re.sub(r'^\s*\d+\.\s*$', '', content, flags=re.MULTILINE)
    content = re.sub(r'\n{3,}', '\n\n', content)
    return content.strip()


def _mask_wiki_generated_blocks(content: str) -> str:
    """屏蔽 Wiki 生成的注入块，避免回流

    检测并替换以下标记块：
    - <wiki-context>...</wiki-context>
    - <!-- wiki-injected -->...<!-- /wiki-injected -->
    - <!-- auto-maintained -->...<!-- /auto-maintained -->
    """
    content = re.sub(
        r'<wiki-context>.*?</wiki-context>',
        '[wiki-context-blocked]', content, flags=re.DOTALL,
    )
    content = re.sub(
        r'<!-- wiki-injected -->.*?<!-- /wiki-injected -->',
        '[wiki-injected-blocked]', content, flags=re.DOTALL,
    )
    content = re.sub(
        r'<!-- auto-maintained -->.*?<!-- /auto-maintained -->',
        '[auto-maintained-blocked]', content, flags=re.DOTALL,
    )
    return content


def reconstruct_session(memos: List[Dict]) -> Tuple[List[Dict], Dict]:
    """从一个 session 的所有 chunk 中重建完整消息列表"""
    all_messages = []
    meta = {
        "source": "", "model": "", "cwd": "", "session_id": "",
        "total_chunks": len(memos), "has_skip_distill": False,
    }

    def _sort_key(m):
        content = m.get("content", "")
        tags = m.get("tags", [])

        # 1. JSON _meta.segment（save_session_full 格式，保留兼容）
        data = _try_parse_json(content)
        if data and "_meta" in data:
            seg = data["_meta"].get("segment", "1/1")
            try:
                return int(seg.split("/")[0])
            except ValueError:
                pass

        # 2. 标签中的 segment=N/M（统一后的 save_long_content 格式）
        for tag in tags:
            if tag.startswith("segment="):
                try:
                    return int(tag.split("=")[1].split("/")[0])
                except (ValueError, IndexError):
                    pass

        # 3. 标签中的 turn=N
        for tag in tags:
            if tag.startswith("turn="):
                try:
                    return int(tag.split("=")[1])
                except ValueError:
                    pass

        # 4. Markdown 内容中的 ## Turn N
        turn_match = re.search(r'^## Turn\s+(\d+)', content, re.MULTILINE)
        if turn_match:
            return int(turn_match.group(1))

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
            if tag == "skip-distill=true":
                meta["has_skip_distill"] = True

        content = memo.get("content", "")
        data = _try_parse_json(content)
        if data:
            # JSON 路径（save_session_full 格式，保留兼容）
            msgs = data.get("messages", [])
            if isinstance(msgs, list):
                for msg in msgs:
                    if isinstance(msg, dict) and "content" in msg:
                        msg["content"] = _clean_message_content(msg["content"])
                        # 回流防护：屏蔽 wiki 生成块
                        msg["content"] = _mask_wiki_generated_blocks(msg["content"])
                all_messages.extend(msgs)
        else:
            # Markdown 路径（sync_engine 格式，新增）
            md_msgs = _parse_markdown_turns(content)
            if md_msgs:
                for msg in md_msgs:
                    if isinstance(msg, dict) and "content" in msg:
                        msg["content"] = _clean_message_content(msg["content"])
                        msg["content"] = _mask_wiki_generated_blocks(msg["content"])
                all_messages.extend(md_msgs)
            else:
                # Fallback：纯文本，当作 system 消息处理
                cleaned = _clean_message_content(content)
                cleaned = _mask_wiki_generated_blocks(cleaned)
                if cleaned:
                    all_messages.append({
                        "role": "system",
                        "content": cleaned[:500],
                        "timestamp": memo.get("createTime", ""),
                    })

    return all_messages, meta


# ========== 相似度检测 ==========

def _compute_similarity(text1: str, text2: str) -> float:
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
            logging.getLogger(__name__).warning(f"Caught unexpected error", exc_info=True)
            pass
    return None


# ========== Markdown 生成（断言级，保留兼容） ==========

def _infer_temporal_scope(assertions: List[Assertion]) -> str:
    """推断知识的时效类型"""
    all_claims = " ".join(a.claim for a in assertions)
    if re.search(r'v\d+\.?\d*|version\s+\d+|版本\s*\d', all_claims, re.I):
        return "version-bound"
    permanent_signals = ['永远', '绝对', '本质', '根本', 'always', 'fundamental']
    if any(s in all_claims for s in permanent_signals):
        return "permanent"
    contextual_signals = ['目前', '当前', '现阶段', '这次', '当下', '现在', 'currently']
    if any(s in all_claims for s in contextual_signals):
        return "contextual"
    stable_signals = ['通常', '一般', '大多数情况下', '普遍', 'normally', 'generally']
    if any(s in all_claims for s in stable_signals):
        return "stable"
    return "contextual"


def _extract_one_liner(assertions: List[Assertion]) -> str:
    if not assertions:
        return ""
    best = max(assertions, key=lambda a: a.confidence)
    return best.claim


def _generate_form_page(session_id: str, form: KnowledgeForm,
                        assertions: List[Assertion], meta: Dict) -> str:
    """为特定知识形态生成 Markdown 页面（断言级生成，兼容旧路径）"""
    lines = []
    source = meta.get("source", "unknown")
    one_liner = _extract_one_liner(assertions)
    temporal_scope = _infer_temporal_scope(assertions)

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

    lines.append(f"# {one_liner[:80]}{'...' if len(one_liner) > 80 else ''}")
    lines.append("")

    lines.append("## 背景")
    lines.append("")
    contexts = []
    for a in assertions:
        if a.context and a.context not in contexts:
            contexts.append(a.context)
    for ctx in contexts[:3]:
        if "<wiki-context" in ctx:
            continue
        lines.append(ctx[:200] + ("..." if len(ctx) > 200 else ""))
        lines.append("")

    lines.append("## 核心内容")
    lines.append("")
    for i, a in enumerate(assertions, 1):
        prefix = "⚠️ " if a.is_negated else ""
        lines.append(f"{prefix}{i}. {a.claim}")
        if a.boundary_hint:
            lines.append(f"   > 边界: {a.boundary_hint}")
    lines.append("")

    boundaries = [a.boundary_hint for a in assertions if a.boundary_hint]
    if boundaries:
        lines.append("## 边界与反例")
        lines.append("")
        for b in boundaries:
            lines.append(f"- {b}")
        lines.append("")

    lines.append("## 演化历史")
    lines.append("")
    lines.append("- v1: 初始记录")
    lines.append("")

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
    """生成 Wiki 页面列表（六种知识形态）— 兼容旧路径"""
    assertions = extract_from_messages(messages, session_id)
    assertions = merge_similar_assertions(assertions)
    assertions = [a for a in assertions if a.confidence >= 0.4]
    if not assertions:
        return []

    form_groups: Dict[KnowledgeForm, List[Assertion]] = {}
    for a in assertions:
        form_groups.setdefault(a.form, []).append(a)

    pages = []
    for form, form_assertions in form_groups.items():
        if len(form_assertions) < 1:
            continue
        page_id = f"{session_id[:8]}_{form.value}"
        md_content = _generate_form_page(session_id, form, form_assertions, meta)
        pages.append((page_id, md_content))

    if not pages and assertions:
        a = assertions[0]
        page_id = f"{session_id[:8]}_{a.form.value}"
        md_content = _generate_form_page(session_id, a.form, [a], meta)
        pages.append((page_id, md_content))

    return pages


def generate_source_md(session_id: str, messages: List[Dict], meta: Dict,
                       quality_score: float, score_detail: Dict) -> str:
    """[兼容入口] 生成 Source 页面 Markdown"""
    pages = generate_wiki_pages(session_id, messages, meta, quality_score, score_detail)
    if pages:
        return pages[0][1]
    return ""


# ========== 质量评估 ==========

def score_session(messages: List[Dict]) -> Tuple[float, Dict]:
    """对整个会话进行质量评分（使用 DistillScorer 如果可用）"""
    if not messages:
        return 0.0, {"total_messages": 0, "valid_messages": 0}

    try:
        from core.scoring.scorers.distill_scorer import DistillScorer
        scorer = DistillScorer()
        session_text = build_session_text(messages)
        cards = scorer.score(session_text)
        distill_card = next((c for c in cards if c.dimension == "distill_score"), None)
        if distill_card:
            avg_score = distill_card.value * 100  # 转为 0-100 范围兼容旧逻辑
            return avg_score, {
                "total_messages": len(messages),
                "valid_messages": len(messages),
                "avg_score": round(avg_score, 1),
                "scorer": "distill_scorer",
            }
    except Exception:
        logging.getLogger(__name__).warning(f"Caught unexpected error at wiki_builder.py", exc_info=True)
        pass

    # 降级到 RuleScorer
    from core.kia.ingest_helpers import score_message_quality
    scores = []
    valid_count = 0
    for msg in messages:
        content = msg.get("content", "")
        if not content:
            continue
        result = score_message_quality(content)
        scores.append(result.get("total_score", 0))
        if result.get("total_score", 0) >= 45:
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
        "scorer": "rule_scorer_fallback",
    }


# ========== 主流程 ==========

def run_build_cycle(client: MemosClient, dry_run: bool = False,
                    use_pipeline: bool = True) -> Dict:
    """执行一轮 Wiki 构建

    Args:
        client: MemosClient 实例
        dry_run: 试运行模式
        use_pipeline: 是否使用七层蒸馏流水线（默认 True）
    """
    _ensure_wiki_dirs()
    sessions = fetch_l1_sessions(client)

    stats = {
        "processed": 0, "skipped_low_quality": 0, "skipped_incomplete": 0,
        "skipped_similar": 0, "skipped_distill": 0, "skipped_recirculation": 0,
        "failed": 0, "pipeline_used": 0, "rule_used": 0,
    }

    # 回流防护
    recirculation_guard = RecirculationGuard()

    # 蒸馏引擎（如果使用流水线）
    engine = DistillationEngine() if use_pipeline else None

    for session_id, memos in sessions.items():
        if len(memos) > MAX_SESSION_CHUNKS:
            _log(session_id, "skip", f"too_many_chunks:{len(memos)}")
            continue

        if not _is_session_completed(session_id, memos):
            stats["skipped_incomplete"] += 1
            continue

        if _is_processed(session_id):
            continue

        # 重建会话
        messages, meta = reconstruct_session(memos)
        if not messages:
            continue

        # 回流防护：skip-distill 标签
        if meta.get("has_skip_distill"):
            _mark_processed(session_id, meta.get("source", "unknown"),
                           len(messages), 0, method="skipped_distill")
            _log(session_id, "skip", "skip-distill")
            stats["skipped_distill"] += 1
            continue

        # 回流防护：内容级检测
        has_recirculation, recirc_detail = recirculation_guard.check_session(messages)
        if has_recirculation:
            _mark_processed(session_id, meta.get("source", "unknown"),
                           len(messages), 0, method="recirculation_blocked")
            _log(session_id, "skip_recirculation", recirc_detail)
            stats["skipped_recirculation"] += 1
            continue

        # 质量评分
        avg_score, score_detail = score_session(messages)

        # 决策：是否蒸馏
        should_distill = avg_score >= 40  # 降门槛，让流水线内部做更精细的判断

        if not should_distill:
            _mark_processed(session_id, meta.get("source", "unknown"),
                           len(messages), avg_score, method="skipped_low_quality")
            _log(session_id, "skip_low_quality", f"score:{avg_score:.1f}")
            stats["skipped_low_quality"] += 1
            continue

        if dry_run:
            stats["processed"] += 1
            continue

        # ===== 蒸馏 =====
        created_pages = 0

        if use_pipeline and engine:
            # 七层蒸馏流水线
            try:
                result = engine.process(session_id, messages, meta)

                if result.judgment == "knowledge" and result.fragments:
                    written = engine.write_pages(result)
                    for path_str in written:
                        _mark_processed(session_id, meta.get("source", "unknown"),
                                       len(messages), avg_score, path_str,
                                       method="pipeline")
                        created_pages += 1
                    stats["pipeline_used"] += 1

                    # 记录流水线层结果
                    layer_summary = ", ".join(
                        f"L{r.layer}({r.name}:{'pass' if r.passed else 'fail'})"
                        for r in result.layer_results
                    )
                    _log(session_id, "pipeline", layer_summary)
                elif result.judgment == "skill":
                    _mark_processed(session_id, meta.get("source", "unknown"),
                                   len(messages), avg_score, method="skill_suggestion")
                    _log(session_id, "skill", result.skill_suggestion)
                    stats["processed"] += 1
                else:
                    _mark_processed(session_id, meta.get("source", "unknown"),
                                   len(messages), avg_score, method="skipped_by_pipeline")
                    _log(session_id, "skip_pipeline", result.judgment_reason)
                    stats["skipped_low_quality"] += 1
            except Exception as e:
                logger.warning(f"  [WikiBuilder] 流水线处理失败: {e}")
                _log(session_id, "error", str(e))
                stats["failed"] += 1
        else:
            # 降级到规则级断言提取
            pages = generate_wiki_pages(session_id, messages, meta, avg_score, score_detail)
            if not pages:
                continue

            for page_id, md_content in pages:
                similar = _find_similar_source(md_content)
                if similar:
                    _log(session_id, "skip_similar", f"{page_id}: {similar}")
                    stats["skipped_similar"] += 1
                    continue

                source_path = _get_wiki_dir() / "00-Inbox" / f"{page_id}.md"
                try:
                    source_path.write_text(md_content, encoding="utf-8")
                    _mark_processed(session_id, meta.get("source", "unknown"),
                                   len(messages), avg_score, str(source_path),
                                   method="rule")
                    _log(session_id, "created", f"00-Inbox/{page_id}.md, Q:{avg_score:.1f}")
                    created_pages += 1
                except Exception as e:
                    _log(session_id, "error", f"{page_id}: {e}")
                    stats["failed"] += 1
            stats["rule_used"] += 1

        stats["processed"] += created_pages

    if stats["processed"] > 0:
        update_index_md()
        _git_auto_commit()

    return stats


def update_index_md():
    """更新 wiki/index.md"""
    index_path = _get_wiki_dir() / "index.md"
    inbox_dir = _get_wiki_dir() / "00-Inbox"

    lines = ["# Wiki Index", ""]

    if inbox_dir.exists():
        md_files = sorted(inbox_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        lines.append(f"## Inbox ({len(md_files)} sessions)")
        lines.append("")
        for md_file in md_files[:50]:
            try:
                content = md_file.read_text(encoding="utf-8")
                agent = "unknown"
                m = re.search(r'^source_agent:\s*(.+)$', content, re.MULTILINE)
                if m:
                    agent = m.group(1).strip()
                name = md_file.stem[:16]
                lines.append(f"- [[{name}]] ({agent})")
            except Exception:
                logging.getLogger(__name__).warning(f"Caught unexpected error", exc_info=True)
                pass
        lines.append("")

    try:
        with _get_conn() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM processed_sessions")
            total = cursor.fetchone()[0]
            lines.append("## Stats")
            lines.append(f"- Total sessions: {total}")
            lines.append(f"- Last update: {datetime.now().isoformat()}")
    except Exception:
        logging.getLogger(__name__).warning(f"Caught unexpected error", exc_info=True)
        pass
    index_path.write_text("\n".join(lines), encoding="utf-8")


def _git_auto_commit():
    """自动提交 Wiki 变更到 git"""
    try:
        wiki_dir = _get_wiki_dir()
        if not (wiki_dir / ".git").exists():
            return
        result = subprocess.run(
            ["git", "diff", "--quiet"], cwd=wiki_dir, capture_output=True,
        )
        if result.returncode == 0:
            return
        subprocess.run(["git", "add", "."], cwd=wiki_dir, capture_output=True)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        subprocess.run(
            ["git", "commit", "-m", f"wiki auto-build {timestamp}"],
            cwd=wiki_dir, capture_output=True,
        )
    except Exception:
        logging.getLogger(__name__).warning(f"Caught unexpected error", exc_info=True)
        pass
def get_stats() -> Dict:
    """获取处理统计"""
    try:
        with _get_conn() as conn:
            cursor = conn.execute(
                "SELECT COUNT(*), AVG(quality_score) FROM processed_sessions"
            )
            total, avg_score = cursor.fetchone()
            cursor = conn.execute("SELECT COUNT(*) FROM wiki_pages WHERE type = 'source'")
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
    parser = argparse.ArgumentParser(description="Wiki Builder - L1 to Wiki Markdown")
    parser.add_argument("--watch", action="store_true", help="守护模式，每5分钟执行")
    parser.add_argument("--dry-run", action="store_true", help="试运行，不写入")
    parser.add_argument("--stats", action="store_true", help="查看统计")
    parser.add_argument("--no-pipeline", action="store_true", help="禁用七层流水线，使用规则级蒸馏")
    args = parser.parse_args()

    token = os.getenv("MEMOS_TOKEN")
    if not token:
        logger.warning("ERROR: MEMOS_TOKEN 环境变量未设置")
        sys.exit(1)

    client = MemosClient(token=token, agent="wiki-builder")

    if args.stats:
        stats = get_stats()
        logger.info("\n=== Wiki Builder 统计 ===")
        for k, v in stats.items():
            logger.info(f"  {k}: {v}")
        return

    use_pipeline = not args.no_pipeline

    if args.watch:
        logger.info(f"[WikiBuilder] 守护模式启动 (pipeline={'ON' if use_pipeline else 'OFF'})")
        while True:
            logger.info(f"\n=== {datetime.now().isoformat()} ===")
            stats = run_build_cycle(client, dry_run=args.dry_run, use_pipeline=use_pipeline)
            print(f"结果: processed={stats['processed']}, "
                  f"incomplete={stats['skipped_incomplete']}, "
                  f"low_q={stats['skipped_low_quality']}, "
                  f"similar={stats['skipped_similar']}, "
                  f"distill={stats['skipped_distill']}, "
                  f"recirculation={stats['skipped_recirculation']}, "
                  f"pipeline={stats['pipeline_used']}, "
                  f"rule={stats['rule_used']}, "
                  f"failed={stats['failed']}")
            time.sleep(300)
    else:
        stats = run_build_cycle(client, dry_run=args.dry_run, use_pipeline=use_pipeline)
        logger.info(f"\n=== Wiki 构建完成 ===")
        logger.info(f"  已处理: {stats['processed']}")
        logger.warning(f"  未完成: {stats['skipped_incomplete']}")
        logger.warning(f"  质量跳过: {stats['skipped_low_quality']}")
        logger.warning(f"  相似跳过: {stats['skipped_similar']}")
        logger.warning(f"  回流跳过: {stats['skipped_distill'] + stats['skipped_recirculation']}")
        logger.info(f"  流水线: {stats['pipeline_used']}")
        logger.info(f"  规则级: {stats['rule_used']}")
        logger.warning(f"  失败: {stats['failed']}")


if __name__ == "__main__":
    main()
