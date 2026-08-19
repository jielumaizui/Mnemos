"""Verification queue CLI commands."""

from __future__ import annotations

import json
from typing import Any

from core.cognitive.verification_queue import (
    VerificationQueue,
    format_verification_report_text,
)


def cmd_verify(args: Any) -> int:
    cmd = getattr(args, "verify_cmd", None)
    queue = VerificationQueue()
    limit = getattr(args, "limit", None)

    if cmd == "plan":
        report = queue.plan(limit=limit)
        _print_report(report, json_output=bool(getattr(args, "json", False)))
        return 0

    if cmd == "run":
        report = queue.run(
            apply=bool(getattr(args, "apply", False)),
            limit=limit,
            background=False,
        )
        _print_report(report, json_output=bool(getattr(args, "json", False)))
        return 0 if report.get("status") in {"ok", "deferred"} else 1

    print("用法: mnemos verify {plan|run}")
    return 1


def _print_report(report: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    print(format_verification_report_text(report, report_id=str(report.get("report_id") or "")))
