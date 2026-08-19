"""Decision inbox CLI."""

from __future__ import annotations

import json
from typing import Any

from core.application.decision_inbox import DecisionInboxService


def _emit(payload: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return
    if isinstance(payload, dict) and "items" in payload:
        print(f"decision inbox: {payload.get('count', 0)} item(s)")
        for item in payload.get("items", []):
            print(
                f"- {item.get('item_id')} [{item.get('severity')}/{item.get('status')}] "
                f"{item.get('title')}"
            )
        return
    print(payload)


def cmd_decision_inbox(args: Any) -> int:
    cmd = getattr(args, "decision_inbox_cmd", "") or "list"
    service = DecisionInboxService()
    as_json = bool(getattr(args, "json", False))
    if cmd == "list":
        result = service.list_items(limit=int(getattr(args, "limit", 50) or 50))
        _emit(result, as_json=as_json)
        return 0
    if cmd == "act":
        result = service.act(
            str(getattr(args, "item_id", "") or ""),
            str(getattr(args, "action", "") or ""),
            reason=str(getattr(args, "reason", "") or ""),
            allow_high_risk=bool(getattr(args, "allow_high_risk", False)),
            snooze_hours=int(getattr(args, "snooze_hours", 24) or 24),
        )
        _emit(result, as_json=as_json)
        return 0 if result.get("success") else 1
    print("用法: mnemos decision-inbox {list|act}")
    return 2
