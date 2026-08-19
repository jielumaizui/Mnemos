"""CLI commands for application-level signals."""

from __future__ import annotations

import json

from core.app.application_signal_service import ApplicationSignalService


def cmd_signals(args) -> int:
    sub = getattr(args, "signals_cmd", "list")
    if sub in (None, "list"):
        return _cmd_signals_list(args)
    print("用法: mnemos signals list [--limit N] [--json]")
    return 1


def _cmd_signals_list(args) -> int:
    limit = int(getattr(args, "limit", 20) or 20)
    service = ApplicationSignalService()
    rows = service.list_signals(limit=limit)
    payload = []
    for row in rows:
        evidence = json.loads(row["evidence_json"] or "[]")
        payload.append(
            {
                "kind": row["kind"],
                "topic": row["topic"],
                "severity": row["severity"],
                "confidence": row["confidence"],
                "suggested_action": row["suggested_action"],
                "evidence": evidence,
                "last_seen_at": row["last_seen_at"],
                "notify_count": row["notify_count"],
            }
        )

    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"Application Signals: {len(payload)}")
    for item in payload:
        print(
            f"- [{item['severity']}] {item['kind']} / {item['topic']} "
            f"(confidence={item['confidence']:.2f})"
        )
        if item["suggested_action"]:
            print(f"  why: {item['suggested_action']}")
        if item["evidence"]:
            print(f"  evidence: {', '.join(str(e) for e in item['evidence'])}")
    return 0
