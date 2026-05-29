# -*- coding: utf-8 -*-
"""
从 Memos 拉取历史 session，过滤后执行蒸馏 → Wiki

过滤规则：
1. 排除 AI 自己的分析输出（数据报告、配置检查等）
2. 排除代码块、shell 命令、工具调用
3. 排除太短的对话（< 50 字符）

可被 daemon 直接调用 main()，也可通过 CLI 运行。
"""

from __future__ import annotations
import logging
logger = logging.getLogger(__name__)

import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional

from core.orchestrator import Orchestrator
from core.config import get_config
from integrations.styx import MemosClient


# ==================== 过滤模式 ====================

SKIP_PATTERNS = [
    r"^让我(试|看|检查|搜索|读|写|改|运行|测试)",
    r"^现在(修改|测试|运行|尝试|检查)",
    r"^我来(测试|尝试|运行|检查)",
    r"^帮我(生成|处理|写|做|找|查)",
    r"^能否(帮我|帮我做|帮我写)",
    r"^```",
    r"^(curl|chmod|npm|pip|git|docker|kubectl|uvicorn)\s",
    r"^\[thinking\]",
    r"^<!DOCTYPE",
    r"^<html",
    r"^<task-notification",
    r"^(The file|file state|Exit code|Error:|Traceback)",
    r"^(INFO:|DEBUG:|WARNING:)",
    r'^\{"ok":\s*(true|false)',
    r"^<system-reminder",
    r"^\[system-reminder",
    r"^Called the ",
    r"^Result of calling ",
    r"^\{'memos':",
    r"^采样 \d+ 条",
    r"^扫描 \d+ 条",
    r"^检查 \d+ 条",
    r"^拉取 \d+ 条",
    r"^Session:",
    r"^选中 session:",
    r"^JSON parse error",
    r"^=== .* ===",
    r"^\s*\d+\.",  # 编号列表（通常是 AI 输出）
]


# ==================== 核心逻辑 ====================

def fetch_memos() -> List[Dict]:
    """从 Memos API 拉取所有数据（通过 MemosClient）"""
    config = get_config()
    client = MemosClient(
        token=config.memos_token,
        base_url=config.memos_api_url,
    )
    all_memos = client.list_all_memos()
    return all_memos


def extract_session_data(memos_content: str) -> Optional[Dict]:
    """从 clean-refined 内容中提取 session JSON"""
    parts = memos_content.split("---", 1)
    if len(parts) < 2:
        return None

    decoder = json.JSONDecoder()
    try:
        session_data, _ = decoder.raw_decode(parts[1].strip())
        return session_data
    except (json.JSONDecodeError, ValueError):
        return None


def should_skip_message(text: str) -> bool:
    """判断消息是否应该被过滤"""
    text = text.strip()
    if len(text) < 50:
        return True
    for pattern in SKIP_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def collect_sessions(memos_list: List[Dict], exclude_session_id: str = "") -> Dict[str, List[Dict]]:
    """按 session 聚合消息，返回 {session_id: [messages]}

    Args:
        memos_list: Memos 记录列表
        exclude_session_id: 排除的 session ID（避免自循环蒸馏）
    """
    sessions: Dict[str, List[Dict]] = {}

    for m in memos_list:
        tags = m.get("tags", [])
        if isinstance(tags, str):
            # 有些 API 返回逗号分隔字符串
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        if "type=clean-refined" not in tags:
            continue

        content = m.get("content", "")
        session_data = extract_session_data(content)
        if not session_data:
            continue

        sid = session_data.get("session_id", "")
        if not sid or sid == exclude_session_id:
            continue

        if sid not in sessions:
            sessions[sid] = []

        for msg in session_data.get("messages", []):
            text = msg.get("content", "").strip()
            if should_skip_message(text):
                continue
            sessions[sid].append({
                "role": msg.get("role", "user"),
                "content": text,
                "timestamp": msg.get("timestamp", ""),
            })

    # 按时间排序每个 session 的消息
    for sid in sessions:
        sessions[sid].sort(key=lambda x: x.get("timestamp", ""))

    # 过滤掉消息太少的 session
    sessions = {sid: msgs for sid, msgs in sessions.items() if len(msgs) >= 3}

    return sessions


def distill_session(session_id: str, messages: List[Dict], wiki_base: str) -> Optional[object]:
    """对单个 session 执行蒸馏（同源复用：入队，由 HephaestusWorker 异步委托）"""
    from core.kia.amphora import enqueue

    enqueue(
        session_id=session_id,
        messages=messages,
        meta={"source": "memos", "working_dir": wiki_base}
    )
    # 异步入队，不阻塞等待。daemon 的 HephaestusWorker 会处理。
    return None


def main(limit: int = 3, exclude_session_id: str = "") -> Dict:
    """
    Memos → Wiki 蒸馏流水线入口

    可被 daemon 直接调用::

        from core.run_distill_from_memos import main
        result = main(limit=5)

    Args:
        limit: 最多处理的 session 数量
        exclude_session_id: 排除的 session ID（避免自循环蒸馏当前会话）

    Returns:
        汇总统计字典
    """
    config = get_config()
    wiki_base = str(config.wiki_dir)

    # 从环境变量读取排除的 session（用于避免自循环）
    if not exclude_session_id:
        exclude_session_id = os.environ.get("MNEMOS_EXCLUDE_SESSION", "")

    logger.info("=" * 60)
    logger.info("Memos → Wiki 蒸馏流水线")
    logger.info(f"Wiki: {wiki_base}")
    logger.info(f"启动时间: {datetime.now(timezone.utc).isoformat()}")
    logger.info("=" * 60)

    # 1. 拉取数据
    logger.info("\n[1/4] 拉取 Memos 数据...")
    memos = fetch_memos()
    logger.info(f"  共 {len(memos)} 条原始数据")

    # 2. 按 session 聚合过滤
    logger.info("\n[2/4] 按 session 聚合过滤...")
    sessions = collect_sessions(memos, exclude_session_id=exclude_session_id)
    logger.info(f"  共 {len(sessions)} 个历史 session（过滤后消息 >= 3 条）")

    # 按消息数排序
    sorted_sessions = sorted(sessions.items(), key=lambda x: -len(x[1]))
    for sid, msgs in sorted_sessions[:10]:
        logger.info(f"    {sid[:8]}...: {len(msgs)} 条消息")

    # 3. 取前 N 个 session 入蒸馏队列（同源复用：委托给宿主 Agent）
    target_sessions = sorted_sessions[:limit]

    logger.info(f"\n[3/4] 入队 {len(target_sessions)} 个 session 到蒸馏队列...")
    enqueued = 0

    for i, (sid, msgs) in enumerate(target_sessions, 1):
        logger.info(f"\n  Session {i}/{len(target_sessions)}: {sid[:8]}... ({len(msgs)} 条消息)")
        try:
            distill_session(sid, msgs, wiki_base)
            enqueued += 1
            logger.info("  ✓ 已入队")
        except Exception as e:
            logger.warning(f"  ✗ 入队失败: {e}")

    # 尝试立即触发委托（不阻塞等待）
    if enqueued > 0:
        try:
            from core.hephaestus_worker import HephaestusWorker
            worker = HephaestusWorker()
            delegated = worker.process_all()
            logger.info(f"\n  已委托 {delegated} 个任务给宿主 Agent")
            logger.info("  提示: daemon 会在 Agent 完成后自动收集结果到 Inbox")
        except Exception as e:
            logger.warning(f"  委托触发失败: {e}")

    # 4. 跑主控循环
    logger.info("\n[4/4] 执行主控循环...")
    orch = Orchestrator(wiki_base=wiki_base, dry_run=False, limit=50, verbose=False)

    results: Dict[str, Dict] = {}
    results["dna"] = orch.run_dna()
    results["graph"] = orch.run_graph()
    results["immune"] = orch.run_immune()
    results["stress"] = orch.run_stress()

    summary = {
        "sessions_processed": len(target_sessions),
        "enqueued": enqueued,
        "dna_computed": results["dna"].get("computed", 0),
        "immune_score": results["immune"].get("health_score", 0),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }

    logger.info("\n" + "=" * 60)
    logger.info("蒸馏完成")
    logger.info(f"  Session 处理: {summary['sessions_processed']}")
    logger.info(f"  已入队: {summary['enqueued']} 个")
    logger.info(f"  DNA: {summary['dna_computed']} 个")
    logger.info(f"  免疫: {summary['immune_score']} 分")
    logger.info("=" * 60)

    return summary


# ==================== CLI ====================

if __name__ == "__main__":
    _limit = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    main(limit=_limit)
