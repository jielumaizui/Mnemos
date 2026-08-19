"""Mnemos CLI command handlers.

导入语义：本 __init__ 不再在模块级 eager-import 所有命令子模块。
只有当调用方访问某个名称（如 cmd_doctor、cmd_distill 等）时，
才通过 __getattr__ 惰性加载对应子模块并缓存到 globals()。

__all__ 仍保持完整，供 IDE、静态分析和 ``from ... import *`` 使用。
"""

from __future__ import annotations

import importlib
from typing import Any

# 每个导出名 -> (子模块路径, 属性名)
_LAZY: dict[str, tuple[str, str]] = {
    "cmd_init": ("core.cli.commands.init", "cmd_init"),
    "cmd_doctor": ("core.cli.commands.doctor", "cmd_doctor"),
    "cmd_status": ("core.cli.commands.status", "cmd_status"),
    "cmd_config": ("core.cli.commands.config", "cmd_config"),
    "cmd_daemon": ("core.cli.commands.daemon", "cmd_daemon"),
    "cmd_scheduler": ("core.cli.commands.scheduler", "cmd_scheduler"),
    "cmd_calibrate": ("core.cli.commands.calibrate", "cmd_calibrate"),
    "cmd_mcp_serve": ("core.cli.commands.mcp", "cmd_mcp_serve"),
    "_install_mcp_only_agent": ("core.cli.commands.mcp", "_install_mcp_only_agent"),
    "_mcp_only_agent_status": ("core.cli.commands.mcp", "_mcp_only_agent_status"),
    "_MCP_ONLY_AGENTS": ("core.cli.commands.mcp", "_MCP_ONLY_AGENTS"),
    "cmd_agent": ("core.cli.commands.agent", "cmd_agent"),
    "cmd_golden": ("core.cli.commands.golden", "cmd_golden"),
    "cmd_scorer": ("core.cli.commands.scorer", "cmd_scorer"),
    "cmd_ingest": ("core.cli.commands.ingest", "cmd_ingest"),
    "cmd_sync": ("core.cli.commands.sync", "cmd_sync"),
    "_compress_ranges": ("core.cli.commands.sync", "_compress_ranges"),
    "_get_backfill_status": ("core.cli.commands.sync", "_get_backfill_status"),
    "_write_backfill_status": ("core.cli.commands.sync", "_write_backfill_status"),
    "_cmd_sync_backfill": ("core.cli.commands.sync", "_cmd_sync_backfill"),
    "_cmd_sync_audit": ("core.cli.commands.sync", "_cmd_sync_audit"),
    "cmd_build_relation_index": (
        "core.cli.commands.build_relation_index",
        "cmd_build_relation_index",
    ),
    "cmd_search": ("core.cli.commands.search", "cmd_search"),
    "cmd_push": ("core.cli.commands.push", "cmd_push"),
    "cmd_metrics_scan": ("core.cli.commands.metrics", "cmd_metrics_scan"),
    "cmd_perf": ("core.cli.commands.perf", "cmd_perf"),
    "cmd_wiki": ("core.cli.commands.wiki", "cmd_wiki"),
    "cmd_report": ("core.cli.commands.report", "cmd_report"),
    "cmd_distill": ("core.cli.commands.distill", "cmd_distill"),
    "_cmd_distill_audit": ("core.cli.commands.distill", "_cmd_distill_audit"),
    "_cmd_distill_backfill_metadata": (
        "core.cli.commands.distill",
        "_cmd_distill_backfill_metadata",
    ),
    "cmd_events": ("core.cli.commands.events", "cmd_events"),
    "cmd_observe": ("core.cli.commands.observe", "cmd_observe"),
    "cmd_reflect": ("core.cli.commands.reflect", "cmd_reflect"),
    "cmd_feedback": ("core.cli.commands.feedback", "cmd_feedback"),
    "cmd_vaults": ("core.cli.commands.vaults", "cmd_vaults"),
    "cmd_cognitive_graph": ("core.cli.commands.cognitive_graph", "cmd_cognitive_graph"),
    "cmd_link_probe": ("core.cli.commands.link_probe", "cmd_link_probe"),
    "cmd_kg_doctor": ("core.cli.commands.kg", "cmd_kg_doctor"),
    "cmd_kg_rebuild_entities": ("core.cli.commands.kg", "cmd_kg_rebuild_entities"),
    "cmd_kg_build_graph": ("core.cli.commands.kg", "cmd_kg_build_graph"),
    "cmd_kg_export_dataview": ("core.cli.commands.kg", "cmd_kg_export_dataview"),
    "cmd_genos": ("core.cli.commands.genos", "cmd_genos"),
    "cmd_capsule": ("core.cli.commands.capsule", "cmd_capsule"),
    "cmd_policy": ("core.cli.commands.policy", "cmd_policy"),
    "cmd_version": ("core.cli.commands.version", "cmd_version"),
    "cmd_shadow": ("core.cli.commands.shadow", "cmd_shadow"),
    "cmd_stress": ("core.cli.commands.stress", "cmd_stress"),
    "cmd_dispute": ("core.cli.commands.dispute", "cmd_dispute"),
    "cmd_blindspot": ("core.cli.commands.blindspot", "cmd_blindspot"),
    "cmd_persona": ("core.cli.commands.persona", "cmd_persona"),
    "cmd_freshness": ("core.cli.commands.freshness", "cmd_freshness"),
    "cmd_entropy": ("core.cli.commands.entropy", "cmd_entropy"),
    "cmd_immune": ("core.cli.commands.immune", "cmd_immune"),
    "cmd_reminder": ("core.cli.commands.reminder", "cmd_reminder"),
    "cmd_signals": ("core.cli.commands.signals", "cmd_signals"),
    "cmd_verify": ("core.cli.commands.verify", "cmd_verify"),
}

__all__ = list(_LAZY.keys())


def __getattr__(name: str) -> Any:  # noqa: ANN001
    from core.import_guard import assert_allowed_module

    if name in _LAZY:
        module_path, attr_name = _LAZY[name]
        assert_allowed_module(module_path)
        module = importlib.import_module(module_path)
        value = getattr(module, attr_name)
        # 缓存到本模块 globals，避免重复触发 __getattr__
        globals()[name] = value
        return value
    # 支持子模块名访问（如 "status"、"doctor"），供 pytest monkeypatch
    # 字符串路径解析使用：setattr("core.cli.commands.status.foo", ...)
    submodule_path = f"core.cli.commands.{name}"
    try:
        assert_allowed_module(submodule_path)
        submod = importlib.import_module(submodule_path)
        globals()[name] = submod
        return submod
    except ImportError:
        pass
    raise AttributeError(f"module 'core.cli.commands' has no attribute {name!r}")
