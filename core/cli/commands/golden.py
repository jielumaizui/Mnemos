"""Golden evaluation commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def cmd_golden(args: Any) -> int:
    if getattr(args, "golden_cmd", "") != "eval":
        print("可用子命令: eval")
        return 1

    from core.agent_kit.shadow_eval import AgentShadowConfigStore, run_agent_shadow_eval

    db_path = Path(args.db_path).expanduser() if getattr(args, "db_path", "") else None
    output_dir = Path(args.output_dir).expanduser() if getattr(args, "output_dir", "") else None
    result = run_agent_shadow_eval(
        config_store=AgentShadowConfigStore(db_path),
        confirm_send_content=bool(getattr(args, "confirm_send_content", False)),
        output_dir=output_dir,
    )
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        metrics = result.get("metrics", {})
        print(f"Agent shadow eval: {result.get('status')}")
        print(f"agent: {result.get('agent') or 'none'}")
        print(f"schema_success_rate: {metrics.get('schema_success_rate')}")
        print(f"fallback_rate: {metrics.get('fallback_rate')}")
        print(f"quality_delta: {metrics.get('quality_delta')}")
    return 0 if result.get("ok") else 1
