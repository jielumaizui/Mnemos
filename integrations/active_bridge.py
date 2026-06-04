"""Runtime bridge used by generated agent wrappers."""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List

from integrations.active import write_active_context


logger = logging.getLogger(__name__)


def _publish(event_type: str, agent: str, payload: Dict[str, Any]) -> None:
    try:
        from core.mnemos_bus import EventBus

        EventBus().publish(event_type, agent, payload)
    except Exception as exc:
        logger.warning("Mnemos event publish failed: %s", exc)


def _parse_messages(raw: str | None) -> List[Dict[str, Any]]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except Exception:
        return []


def _enqueue_session(agent: str, working_dir: str, messages: List[Dict[str, Any]]) -> str | None:
    if not messages:
        return None
    try:
        import hashlib
        from core.kia.amphora import enqueue

        wd = working_dir or os.getcwd()
        dir_hash = hashlib.md5(wd.encode()).hexdigest()[:8]
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        sid = f"{agent}:{dir_hash}:{ts}"
        enqueue(
            session_id=sid,
            messages=messages,
            meta={"source": agent, "working_dir": wd},
        )
        return sid
    except Exception as exc:
        logger.warning("%s distillation enqueue failed: %s", agent, exc)
        return None


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

    agent = (args.agent or default_agent or os.environ.get("MNEMOS_HOST_AGENT") or "unknown").lower()
    event = (args.event or _event_from_env()).lower()
    session_start = args.session_start or event in {"sessionstart", "session_start", "start"}
    session_end = args.session_end or event in {"sessionend", "session_end", "end"}

    if session_start:
        path, context = write_active_context(agent, args.working_dir, args.user_message)
        _publish("session.start", agent, {
            "working_dir": args.working_dir,
            "user_message": args.user_message,
            "active_context_path": str(path),
            "active_context_length": len(context),
        })
        print(context)
        print(f"\n[Mnemos] Active context saved: {path}")
        return

    if session_end:
        messages = _parse_messages(args.session_messages)
        sid = _enqueue_session(agent, args.working_dir, messages)
        _publish("session.end", agent, {
            "working_dir": args.working_dir,
            "session_id": sid,
            "messages": messages,
            "meta": {"source": agent, "working_dir": args.working_dir},
        })
        print("[Mnemos] session.end event published")
        if sid:
            print(f"[Mnemos] Session queued for distillation: {sid}")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
