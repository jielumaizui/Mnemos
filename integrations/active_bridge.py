"""Runtime bridge used by generated agent wrappers."""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from integrations.active import write_active_context

logger = logging.getLogger(__name__)


def _publish(event_type: str, agent: str, payload: Dict[str, Any]) -> None:
    try:
        from core.mnemos_bus import Event, get_event_bus

        get_event_bus().publish(Event(event_type=event_type, source=agent, payload=payload))
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        logger.warning("Mnemos event publish failed: %s", exc, exc_info=True)


def _parse_messages(raw: str | None) -> List[Dict[str, Any]]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
    except (
        OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError
    ):
        return []


def _messages_to_turns(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """将标准 messages 列表配对为 turns（user+assistant）。"""
    turns: List[Dict[str, Any]] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "user":
            user_content = content
            assistant_content = ""
            # 尝试与下一个 assistant 配对
            if i + 1 < len(messages) and messages[i + 1].get("role") == "assistant":
                assistant_content = messages[i + 1].get("content", "")
                i += 2
            else:
                i += 1
            turns.append(
                {
                    "turn_number": len(turns),
                    "user_content": user_content,
                    "assistant_content": assistant_content,
                }
            )
        elif role == "assistant":
            # 孤立的 assistant（前面没有 user）
            turns.append(
                {
                    "turn_number": len(turns),
                    "user_content": "",
                    "assistant_content": content,
                }
            )
            i += 1
        else:
            # tool / system / 其他角色作为 raw_event_refs
            turns.append(
                {
                    "turn_number": len(turns),
                    "user_content": "",
                    "assistant_content": "",
                    "raw_event_refs": [{"role": role, "content": content}],
                }
            )
            i += 1
    return turns


def _enqueue_session(
    agent: str, working_dir: str, messages: List[Dict[str, Any]], session_id: str | None = None
) -> str | None:
    """[P0-2] 将 session 通过 CaptureService 写入 L1 storage，再走标准链路。

    Args:
        agent: Agent 名称
        working_dir: 工作目录
        messages: 会话消息列表
        session_id: 可选的外部 session_id（如 Claude Code JSONL stem）。
                    若提供则直接使用，否则生成 agent:hash:ts 格式。
    """
    if not messages:
        return None
    try:
        import hashlib
        from core.sync_framework.capture_service import CaptureService

        wd = working_dir or os.getcwd()
        if session_id:
            sid = session_id
        else:
            dir_hash = hashlib.md5(wd.encode(), usedforsecurity=False).hexdigest()[:8]
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            sid = f"{agent}:{dir_hash}:{ts}"

        capture = CaptureService(start_worker=False)
        turns = _messages_to_turns(messages)

        completeness = {
            "visible_text": "host_provided",
            "loss_reasons": ["host_session_messages_may_be_compressed"],
        }

        # 批量写入整个 session
        capture.capture_session(
            source_agent=agent,
            session_id=sid,
            turns=[
                {
                    **t,
                    "cwd": wd,
                    "metadata": {"capture_source": "active_hook"},
                    "completeness": completeness,
                }
                for t in turns
            ],
        )
        capture.end_session(agent, sid)

        # [P0] 立即 flush，确保数据写入 Obsidian
        # hook 进程结束后队列丢失，不能依赖后台 worker
        worker_pool = getattr(capture, "worker_pool", None)
        if worker_pool is None:
            logger.warning("%s flush_session skipped: worker pool unavailable", agent)
        else:
            try:
                worker_pool.flush_session(agent, sid)
            except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
                logger.warning("%s flush_session failed: %s", agent, exc, exc_info=True)

        # [2026-06] 双写入已移除：amphora 入队已从本函数删除，
        # CaptureWorkerPool.flush_session() 在 L1 写入成功后统一触发蒸馏入队。
        # 这是 "双写入 → 单写入" 过渡的完成：L1 storage 是单一写入源。
        return sid
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        logger.error("%s active bridge enqueue failed: %s", agent, exc, exc_info=True)
        return None


def _normalize_kimi_content(content) -> str:
    """Kimi JSONL 的 content 可能是字符串或 block 列表，统一提取为文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text") or block.get("think") or ""
                if text:
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content)


def _read_kimi_fallback_session() -> Tuple[Optional[str], List[Dict[str, str]]]:
    """Kimi 不提供 SESSION_MESSAGES 环境变量，从最近修改的 context JSONL 回读。

    Returns:
        (session_id, messages)。session_id 取自 JSONL 所在目录名，保持同一 session
        多次 hook 的 session_id 稳定。
    """
    sessions_dir = Path.home() / ".kimi" / "sessions"
    if not sessions_dir.exists():
        return None, []
    # [P006] 同时搜索当前活跃文件 context.jsonl 与已归档的 context_*.jsonl
    candidates = list(sessions_dir.rglob("context_*.jsonl"))
    candidates.extend(sessions_dir.rglob("context.jsonl"))
    if not candidates:
        return None, []
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    session_id = latest.parent.name if latest.parent != sessions_dir else None
    messages: List[Dict[str, str]] = []
    try:
        with open(latest, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                msg = json.loads(line)
                role = msg.get("role", "")
                # 过滤内部系统提示（以 _ 开头的 role）和 tool 结果
                if role.startswith("_") or role in ("tool", "system"):
                    continue
                if role not in ("user", "assistant"):
                    continue
                content = _normalize_kimi_content(msg.get("content", ""))
                messages.append({"role": role, "content": content})
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
        logger.debug("[active_bridge] 读取 Kimi fallback JSONL 失败", exc_info=True)
        return session_id, []
    return session_id, messages


def _event_from_env() -> str:
    keys = (
        "MNEMOS_HOOK_EVENT",
        "KIMI_HOOK_EVENT",
        "CLAUDE_HOOK_EVENT",
        "CODEX_HOOK_EVENT",
        "OPENCODE_HOOK_EVENT",
        "HERMES_HOOK_EVENT",
        "OPENCLAW_HOOK_EVENT",
        "HOOK_EVENT",
        "EVENT",
    )
    for key in keys:
        value = os.environ.get(key)
        if value:
            return value.lower()
    return ""


def main(default_agent: str | None = None) -> None:
    parser = argparse.ArgumentParser(description="Mnemos active bridge")
    parser.add_argument("agent", nargs="?", default=default_agent or "")
    parser.add_argument("--session-start", action="store_true")
    parser.add_argument("--session-end", action="store_true")
    parser.add_argument("--event", default="")
    parser.add_argument("--working-dir", default=os.getcwd())
    parser.add_argument("--user-message", default=os.environ.get("USER_MESSAGE", ""))
    parser.add_argument("--session-messages", default=os.environ.get("SESSION_MESSAGES", ""))
    args = parser.parse_args()

    agent = (
        args.agent or default_agent or os.environ.get("MNEMOS_HOST_AGENT") or "unknown"
    ).lower()
    event = (args.event or _event_from_env()).lower()
    session_start = args.session_start or event in {"sessionstart", "session_start", "start"}
    session_end = args.session_end or event in {"sessionend", "session_end", "end"}

    if session_start:
        path, context = write_active_context(agent, args.working_dir, args.user_message)
        _publish(
            "session.start",
            agent,
            {
                "working_dir": args.working_dir,
                "user_message": args.user_message,
                "active_context_path": str(path),
                "active_context_length": len(context),
            },
        )
        print(context)
        print(f"\n[backend] Active context saved: {path}")
        return

    if session_end:
        messages = _parse_messages(args.session_messages)
        fallback_session_id: Optional[str] = None
        if not messages and agent == "kimi":
            fallback_session_id, messages = _read_kimi_fallback_session()
            if messages:
                logger.info(
                    "[active_bridge] Kimi fallback 读取 %d 条消息 (sid=%s): %s",
                    len(messages),
                    fallback_session_id,
                    args.working_dir,
                )
        sid = _enqueue_session(agent, args.working_dir, messages, session_id=fallback_session_id)
        _publish(
            "session.end",
            agent,
            {
                "working_dir": args.working_dir,
                "session_id": sid,
                "messages": messages,
                "meta": {"source": agent, "working_dir": args.working_dir},
            },
        )
        print("[backend] session.end event published")
        if sid:
            print(f"[backend] Session queued for distillation: {sid}")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
