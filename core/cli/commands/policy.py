# -*- coding: utf-8 -*-
"""
mnemos policy — 有效策略 shadow 管理

- list: 列出待人工裁决的策略 shadow
- commit <experiment_id>: 强制提交指定 shadow 到全局 Config
- rollback <experiment_id>: 强制回滚指定 shadow
"""

from __future__ import annotations

from typing import Any

from core.kia.policy import get_effective_policy


def _format_shadow(config_key: str, meta: dict[str, Any]) -> str:
    experiment_id = meta.get("experiment_id", "")
    old_value = meta.get("old_value", "")
    new_value = meta.get("new_value", "")
    metric_before = meta.get("metric_before", "")
    applied_at = meta.get("applied_at", "")
    suffix = f" metric_before={metric_before}" if metric_before not in ("", None) else ""
    if applied_at:
        suffix = f"{suffix} applied_at={applied_at}"
    return f"{experiment_id} {config_key}: {old_value} -> {new_value}{suffix}"


def cmd_policy(args) -> int:
    """有效策略 CLI 入口。"""
    policy = get_effective_policy()
    cmd = getattr(args, "policy_cmd", None)

    if cmd == "list":
        shadows = policy.list_shadows()
        if not shadows:
            print("暂无待裁决策略 shadow")
            return 0
        print(f"待裁决策略 shadow: {len(shadows)}")
        for config_key, meta in sorted(shadows.items()):
            print(f"  - {_format_shadow(config_key, meta)}")
        return 0

    if cmd == "commit":
        experiment_id = getattr(args, "experiment_id", "")
        if not experiment_id:
            print("错误: 缺少 experiment_id")
            return 1
        if policy.force_commit(experiment_id):
            print(f"已提交策略 shadow: {experiment_id}")
            return 0
        print(f"未找到可提交的策略 shadow: {experiment_id}")
        return 1

    if cmd == "rollback":
        experiment_id = getattr(args, "experiment_id", "")
        if not experiment_id:
            print("错误: 缺少 experiment_id")
            return 1
        if policy.force_rollback(experiment_id):
            print(f"已回滚策略 shadow: {experiment_id}")
            return 0
        print(f"未找到可回滚的策略 shadow: {experiment_id}")
        return 1

    print("未知子命令")
    return 1
