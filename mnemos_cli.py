#!/usr/bin/env python3
"""Mnemos - 命令行入口"""
import importlib
import logging
import json
import sys
from core.cli.commands.agent import add_agent_mcp_grant_parser
from core.cli.commands.migrate import add_model_call_ledger_migration_args
from core.cli.parser_help import build_docstring as _build_docstring
from core.cli.helpers import (  # noqa: F401
    BYTES_PER_KB,
    _check_vault_health,
    _daemon_processes,
    _format_bytes,
    _get_cognitive_graph_stats,
    _get_sqlite_conn,
    _print_config_contract,
    _print_runtime_health,
    _print_today_summary,
    _print_vault_status,
    _sqlite_group_counts,
)
from core.config import get_config  # noqa: F401
from core.db_utils import sqlite_conn  # noqa: F401

logger = logging.getLogger(__name__)
_LAZY_EXPORTS = {
    "cmd_init": ("core.cli.commands.init", "cmd_init"),
    "cmd_doctor": ("core.cli.commands.doctor", "cmd_doctor"),
    "cmd_status": ("core.cli.commands.status", "cmd_status"),
    "cmd_config": ("core.cli.commands.config", "cmd_config"),
    "cmd_secrets": ("core.cli.commands.secrets", "cmd_secrets"),
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
    "cmd_metrics_assess": ("core.cli.commands.metrics", "cmd_metrics_assess"),
    "cmd_perf": ("core.cli.commands.perf", "cmd_perf"),
    "cmd_raw_index": ("core.cli.commands.raw_index", "cmd_raw_index"),
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
    "cmd_cognitive_graph": (
        "core.cli.commands.cognitive_graph",
        "cmd_cognitive_graph",
    ),
    "cmd_link_probe": ("core.cli.commands.link_probe", "cmd_link_probe"),
    "cmd_proposal": ("core.cli.commands.proposal", "cmd_proposal"),
    "cmd_decision_inbox": ("core.cli.commands.decision_inbox", "cmd_decision_inbox"),
    "cmd_kg_doctor": ("core.cli.commands.kg", "cmd_kg_doctor"),
    "cmd_kg_rebuild_entities": ("core.cli.commands.kg", "cmd_kg_rebuild_entities"),
    "cmd_kg_consistency": ("core.cli.commands.kg", "cmd_kg_consistency"),
    "cmd_kg_normalize_endpoints": (
        "core.cli.commands.kg",
        "cmd_kg_normalize_endpoints",
    ),
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
    "cmd_db_maintenance": ("core.cli.commands.db", "cmd_db_maintenance"),
    "cmd_persona": ("core.cli.commands.persona", "cmd_persona"),
    "cmd_freshness": ("core.cli.commands.freshness", "cmd_freshness"),
    "cmd_entropy": ("core.cli.commands.entropy", "cmd_entropy"),
    "cmd_immune": ("core.cli.commands.immune", "cmd_immune"),
    "cmd_reminder": ("core.cli.commands.reminder", "cmd_reminder"),
    "cmd_recap": ("core.cli.commands.recap", "cmd_recap"),
    "cmd_signals": ("core.cli.commands.signals", "cmd_signals"),
    "cmd_verify": ("core.cli.commands.verify", "cmd_verify"),
    "cmd_migrate": ("core.cli.commands.migrate", "cmd_migrate"),
    "cmd_backup": ("core.cli.commands.backup", "cmd_backup"),
    "cmd_restore": ("core.cli.commands.backup", "cmd_restore"),
    "cmd_data": ("core.cli.commands.data", "cmd_data"),
    "cmd_setup": ("core.cli.commands.setup", "cmd_setup"),
    "cmd_upgrade": ("core.cli.commands.setup", "cmd_upgrade"),
    "cmd_uninstall": ("core.cli.commands.setup", "cmd_uninstall"),
    "cmd_repair_all": ("core.cli.commands.setup", "cmd_repair_all"),
}


def _load_export(name):
    from core.import_guard import assert_allowed_module

    module_name, attr_name = _LAZY_EXPORTS[name]
    assert_allowed_module(module_name)
    module = importlib.import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def _resolve_export(name):
    if name in globals():
        return globals()[name]
    return _load_export(name)


def _call_command(name, args):
    return _resolve_export(name)(args)


def __getattr__(name):
    if name in _LAZY_EXPORTS:
        return _load_export(name)
    raise AttributeError(name)


def build_parser():
    """Build the top-level argparse parser and all subparsers."""
    import argparse

    def positive_int(value: str) -> int:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("must be an integer >= 1") from exc
        if parsed < 1:
            raise argparse.ArgumentTypeError("must be an integer >= 1")
        return parsed

    parser = argparse.ArgumentParser(
        description="Mnemos - 命令行入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # init
    init_parser = subparsers.add_parser("init", help="交互式配置向导")
    init_parser.add_argument("--yes", "-y", action="store_true", help="非交互模式")
    init_parser.add_argument("--skip-hooks", action="store_true", help="跳过安装 Agent 主动接入")
    init_parser.add_argument("--skip-verify", action="store_true", help="跳过部署后验证")
    init_parser.add_argument("--preserve-config", action="store_true", help="保留现有配置并补齐必要字段")
    init_parser.add_argument(
        "--max-smoke-attempts",
        type=positive_int,
        default=3,
        help="必填模型端点 smoke 最大尝试次数（默认 3）",
    )

    # setup / upgrade / uninstall
    setup_parser = subparsers.add_parser("setup", help="产品级安装、配置、验证入口")
    setup_parser.add_argument("--dry-run", action="store_true", help="只输出安装计划，不写状态")
    setup_parser.add_argument("--yes", "-y", action="store_true", help="非交互模式")
    setup_parser.add_argument("--skip-backend", action="store_true", help="跳过 Raw Vault 后端确认")
    setup_parser.add_argument("--skip-daemon", action="store_true", help="跳过启动守护进程")
    setup_parser.add_argument("--skip-scheduler", action="store_true", help="跳过配置系统定时任务")
    setup_parser.add_argument("--skip-hooks", action="store_true", help="跳过安装 Agent 主动接入")
    setup_parser.add_argument("--skip-verify", action="store_true", help="跳过部署后验证")
    setup_parser.add_argument("--skip-backfill", action="store_true", help="跳过历史数据回填")
    setup_parser.add_argument("--skip-e2e", action="store_true", help="跳过 E2E 全链路探针")
    setup_parser.add_argument("--preserve-config", action="store_true", help="保留现有配置并补齐必要字段")
    setup_parser.add_argument(
        "--max-smoke-attempts",
        type=positive_int,
        default=3,
        help="必填模型端点 smoke 最大尝试次数（默认 3）",
    )
    setup_parser.add_argument("--venv-reexec", action="store_true", help=argparse.SUPPRESS)
    setup_parser.add_argument("--json", action="store_true", help="输出 JSON")

    upgrade_parser = subparsers.add_parser("upgrade", help="产品级升级规划与执行")
    upgrade_sub = upgrade_parser.add_subparsers(dest="upgrade_cmd")
    upgrade_plan = upgrade_sub.add_parser("plan", help="生成升级计划")
    upgrade_plan.add_argument("--json", action="store_true", help="输出 JSON")
    upgrade_apply = upgrade_sub.add_parser("apply", help="创建备份并执行升级")
    upgrade_apply.add_argument("--execute-wrapped", action="store_true", help="允许执行包装的旧迁移脚本")
    upgrade_apply.add_argument("--json", action="store_true", help="输出 JSON")

    uninstall_parser = subparsers.add_parser("uninstall", help="卸载或生成数据保留/删除计划")
    uninstall_parser.add_argument("--preserve-data", action="store_true", help="保留用户数据（默认）")
    uninstall_parser.add_argument("--purge-data", action="store_true", help="请求删除数据，需二次确认")
    uninstall_parser.add_argument("--confirm", action="store_true", help="确认高风险卸载动作")
    uninstall_parser.add_argument("--dry-run", action="store_true", help="只输出计划，不写状态")
    uninstall_parser.add_argument("--json", action="store_true", help="输出 JSON")

    # doctor
    doctor_parser = subparsers.add_parser("doctor", help="系统诊断")
    doctor_parser.add_argument(
        "doctor_action",
        nargs="?",
        choices=["repair", "repair-all", "modules", "config"],
        default=None,
        help="可选动作：repair 修复 Agent；repair-all 修复安装旅程；modules 查看模块契约；config 审计全局配置",
    )
    doctor_parser.add_argument(
        "agent_name",
        nargs="?",
        default="",
        help="doctor repair 时指定 Agent 名称（可选，如 claude/kimi）",
    )
    doctor_parser.add_argument("--e2e", action="store_true", help="运行 E2E 全链路探针")
    doctor_parser.add_argument("--verbose", action="store_true", help="显示配置来源等诊断细节")
    doctor_parser.add_argument("--strict", action="store_true", help="doctor config 时按发布部署口径执行严格配置审计")
    doctor_parser.add_argument(
        "--unsafe-debug",
        action="store_true",
        help="输出未脱敏的本机路径和端点，仅限本机排障",
    )
    doctor_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="repair 无 agent 时输出统一自愈计划；repair-all 时只输出修复计划",
    )
    doctor_parser.add_argument("--json", action="store_true", help="输出机器可读健康报告")
    doctor_parser.add_argument(
        "--cognitive-readiness",
        action="store_true",
        help="输出认知系统就绪度只读审计",
    )

    # health
    health_parser = subparsers.add_parser("health", help="机器可读健康检查")
    health_parser.add_argument("--json", action="store_true", help="输出 JSON")
    health_parser.add_argument(
        "--unsafe-debug",
        action="store_true",
        help="输出未脱敏的本机路径和端点，仅限本机排障",
    )

    # status
    subparsers.add_parser("status", help="查看系统状态")

    # migrate
    migrate_parser = subparsers.add_parser("migrate", help="版本迁移与升级治理")
    migrate_sub = migrate_parser.add_subparsers(dest="migrate_cmd")
    migrate_status = migrate_sub.add_parser("status", help="查看迁移状态")
    migrate_status.add_argument("--json", action="store_true", help="输出 JSON")
    migrate_plan = migrate_sub.add_parser("plan", help="预览迁移计划")
    migrate_plan.add_argument("--json", action="store_true", help="输出 JSON")
    migrate_apply = migrate_sub.add_parser("apply", help="执行指定迁移")
    migrate_apply.add_argument("migration_id", help="迁移 ID")
    migrate_apply.add_argument("--json", action="store_true", help="输出 JSON")
    migrate_rollback = migrate_sub.add_parser("rollback", help="回滚指定迁移")
    migrate_rollback.add_argument("migration_id", help="迁移 ID")
    migrate_rollback.add_argument("--json", action="store_true", help="输出 JSON")
    add_model_call_ledger_migration_args(migrate_apply, migrate_rollback)

    # backup / restore
    backup_parser = subparsers.add_parser("backup", help="全局快照备份")
    backup_sub = backup_parser.add_subparsers(dest="backup_cmd")
    backup_create = backup_sub.add_parser("create", help="创建或预览全局快照")
    backup_create.add_argument("--reason", default="manual", help="创建原因")
    backup_create.add_argument("--trigger-action", default="manual", help="触发动作")
    backup_create.add_argument("--dry-run", action="store_true", help="只预览 manifest")
    backup_create.add_argument("--json", action="store_true", help="输出 JSON")
    backup_list = backup_sub.add_parser("list", help="列出已有快照")
    backup_list.add_argument("--json", action="store_true", help="输出 JSON")

    restore_parser = subparsers.add_parser("restore", help="全局快照恢复")
    restore_sub = restore_parser.add_subparsers(dest="restore_cmd")
    restore_plan = restore_sub.add_parser("plan", help="预览恢复计划")
    restore_plan.add_argument("snapshot_id", nargs="?", default="latest", help="快照 ID")
    restore_plan.add_argument("--json", action="store_true", help="输出 JSON")
    restore_apply = restore_sub.add_parser("apply", help="执行恢复")
    restore_apply.add_argument("snapshot_id", nargs="?", default="latest", help="快照 ID")
    restore_apply.add_argument("--allow-conflicts", action="store_true", help="允许覆盖冲突文件")
    restore_apply.add_argument("--json", action="store_true", help="输出 JSON")
    restore_verify = restore_sub.add_parser("verify", help="校验恢复结果")
    restore_verify.add_argument("snapshot_id", nargs="?", default="latest", help="快照 ID")
    restore_verify.add_argument("--json", action="store_true", help="输出 JSON")

    # data ownership
    data_parser = subparsers.add_parser("data", help="数据所有权、导出、冻结和删除")
    data_sub = data_parser.add_subparsers(dest="data_cmd")
    data_inventory = data_sub.add_parser("inventory", help="列出 Mnemos 数据资产清单")
    data_inventory.add_argument("--json", action="store_true", help="输出 JSON")
    data_export = data_sub.add_parser("export", help="导出或预览数据包")
    data_export.add_argument("--scope", default="all", help="范围，如 all 或 session:test")
    data_export.add_argument("--dry-run", action="store_true", help="只预览，不写包")
    data_export.add_argument("--json", action="store_true", help="输出 JSON")
    data_freeze = data_sub.add_parser("freeze", help="冻结指定范围数据")
    data_freeze.add_argument("--scope", required=True, help="范围，如 session:test")
    data_freeze.add_argument("--reason", default="user_request", help="冻结原因")
    data_freeze.add_argument("--json", action="store_true", help="输出 JSON")
    data_snapshot = data_sub.add_parser("snapshot", help="创建绑定删除范围的保留快照")
    data_snapshot.add_argument("--scope", required=True, help="范围，如 session:test")
    data_snapshot.add_argument(
        "--retention-days", type=int, default=30, help="保留天数（至少 1 天）"
    )
    data_snapshot.add_argument("--json", action="store_true", help="输出 JSON")
    data_delete = data_sub.add_parser("delete", help="删除 dry-run 或执行证明")
    data_delete.add_argument("--scope", required=True, help="范围，如 session:test")
    data_delete.add_argument("--dry-run", action="store_true", help="只生成删除计划")
    data_delete.add_argument("--apply", action="store_true", help="执行删除证明流程")
    data_delete.add_argument("--confirm", action="store_true", help="确认执行")
    data_delete.add_argument("--snapshot-ref", default="", help="删除前快照引用")
    data_delete.add_argument("--json", action="store_true", help="输出 JSON")

    # push
    push_parser = subparsers.add_parser("push", help="预测知识推送")
    push_sub = push_parser.add_subparsers(dest="push_cmd")
    push_check_parser = push_sub.add_parser("check", help="检查当前输入是否应推送知识")
    push_check_parser.add_argument("message", help="用户输入文本")
    push_check_parser.add_argument("--task", default="", help="当前任务上下文")
    push_check_parser.add_argument("--session-id", default="", help="会话 ID")
    push_check_parser.add_argument("--wiki-base", default=None, help="Wiki 根目录")
    push_check_parser.add_argument("--json", action="store_true", help="输出 JSON")
    push_stats_parser = push_sub.add_parser("stats", help="查看预测推送历史统计")
    push_stats_parser.add_argument("--days", type=int, default=30, help="统计最近多少天")
    push_stats_parser.add_argument("--wiki-base", default=None, help="Wiki 根目录")
    push_stats_parser.add_argument("--json", action="store_true", help="输出 JSON")

    # metrics
    metrics_parser = subparsers.add_parser("metrics", help="Wiki 页面度量管理")
    metrics_sub = metrics_parser.add_subparsers(dest="metrics_cmd")
    metrics_sub.add_parser("scan", help="全量扫描 Wiki 页面 metrics")
    metrics_assess = metrics_sub.add_parser("assess", help="快速评估单个 Wiki 页面")
    metrics_assess.add_argument("page", help="Wiki 页面路径；相对路径按当前 Wiki 根目录解析")
    metrics_assess.add_argument("--source-count", type=int, default=1, help="来源数量")

    # perf
    subparsers.add_parser("perf", help="查看后台性能、队列和数据体积")

    # raw-index
    raw_index_parser = subparsers.add_parser("raw-index", help="RawIndex 维护")
    raw_index_sub = raw_index_parser.add_subparsers(dest="raw_index_cmd")
    raw_index_status = raw_index_sub.add_parser("status", help="只读查看 raw_index.db 状态")
    raw_index_status.add_argument("--raw-dir", help="覆盖 raw vault 目录")
    raw_index_status.add_argument("--db-path", help="覆盖 raw_index.db 路径")
    raw_index_status.add_argument("--json", action="store_true", help="输出 JSON")
    raw_index_rebuild = raw_index_sub.add_parser(
        "rebuild", help="重建 raw_index.db；默认 dry-run"
    )
    raw_index_rebuild.add_argument("--raw-dir", help="覆盖 raw vault 目录")
    raw_index_rebuild.add_argument("--db-path", help="覆盖 raw_index.db 路径")
    raw_index_rebuild.add_argument("--incremental", action="store_true", help="只增量更新")
    raw_index_rebuild.add_argument("--apply", action="store_true", help="实际执行重建")
    raw_index_rebuild.add_argument("--json", action="store_true", help="输出 JSON")

    # config
    config_parser = subparsers.add_parser("config", help="查看/编辑配置")
    config_parser.add_argument("--set", help="设置配置项 (如 wiki.vault_path=~/wiki)")

    # secrets
    secrets_parser = subparsers.add_parser("secrets", help="Secret/keyring 诊断")
    secrets_sub = secrets_parser.add_subparsers(dest="secrets_cmd")
    secrets_doctor = secrets_sub.add_parser("doctor", help="诊断 keyring 与 env fallback")
    secrets_doctor.add_argument("--json", action="store_true", help="输出 JSON")
    secrets_doctor.add_argument(
        "--accept-env-fallback",
        action="store_true",
        help="明确接受 env: secret references 作为 keyring 不可用时的降级",
    )

    # agent
    agent_parser = subparsers.add_parser("agent", help="AI Agent 管理")
    agent_sub = agent_parser.add_subparsers(dest="agent_cmd")
    agent_sub.add_parser("list", help="列出本地可用的 AI Agent")
    install_parser = agent_sub.add_parser("install", help="为所有可用 Agent 安装主动接入")
    install_parser.add_argument(
        "agent_name",
        nargs="?",
        default="",
        help="指定 Agent 名称（可选，如 claude/hermes/openclaw/opencode/codex/crush/kimi）",
    )
    repair_parser = agent_sub.add_parser("repair", help="一键修复 Agent 主动接入并重跑满血验收")
    repair_parser.add_argument(
        "agent_name",
        nargs="?",
        default="",
        help="指定 Agent 名称（可选，如 claude/hermes/openclaw/opencode/codex/crush/kimi）",
    )
    agent_sub.add_parser("detect", help="检测宿主 Agent（MNEMOS_HOST_AGENT）")
    agent_doctor_parser = agent_sub.add_parser("doctor", help="诊断 Agent 状态")
    agent_doctor_parser.add_argument(
        "agent_name",
        nargs="?",
        default="",
        help="指定 Agent 名称（可选，如 claude/hermes/openclaw/opencode/codex/crush/kimi）",
    )
    agent_kit_parser = agent_sub.add_parser("kit", help="检查 Mnemos Agent Kit 统一协议")
    agent_kit_parser.add_argument(
        "agent_name",
        nargs="?",
        default="",
        help="指定 Agent 名称（可选，如 openclaw/hermes/claude-code/opencode/codex/crush/kiro/kimi）",
    )
    agent_kit_parser.add_argument("--json", action="store_true", help="输出机器可读报告")
    agent_kit_parser.add_argument(
        "--no-probe",
        action="store_true",
        help="不探测本机文件系统，只检查协议和注册表",
    )
    agent_scan_parser = agent_sub.add_parser("scan", help="只读发现本地 Agent 能力状态")
    agent_scan_parser.add_argument(
        "agent_name",
        nargs="?",
        default="",
        help="指定 Agent 名称（可选，如 openclaw/hermes/claude-code/opencode/codex/crush/kiro/kimi）",
    )
    agent_scan_parser.add_argument("--json", action="store_true", help="输出机器可读报告")
    agent_scan_parser.add_argument(
        "--no-probe",
        action="store_true",
        help="不探测本机文件系统，只检查协议和注册表",
    )
    add_agent_mcp_grant_parser(agent_sub)
    agent_shadow_parser = agent_sub.add_parser("shadow", help="管理单 AgentBackend shadow 灰度")
    agent_shadow_parser.add_argument(
        "--db-path",
        default="",
        help="覆盖 agent_authorization.db 路径",
    )
    agent_shadow_sub = agent_shadow_parser.add_subparsers(dest="shadow_cmd")
    agent_shadow_status = agent_shadow_sub.add_parser("status", help="查看 AgentBackend shadow 状态")
    agent_shadow_status.add_argument("--json", action="store_true", help="输出 JSON")
    agent_shadow_enable = agent_shadow_sub.add_parser("enable", help="启用单 agent shadow")
    agent_shadow_enable.add_argument("agent_name", help="agent 名称，如 codex/claude/kimi")
    agent_shadow_enable.add_argument(
        "--command",
        dest="agent_command",
        required=True,
        help="非交互 CLI 命令",
    )
    agent_shadow_enable.add_argument("--timeout", type=float, default=30.0, help="超时秒数")
    agent_shadow_enable.add_argument(
        "--directory",
        default="*",
        help="授权目录 scope，默认 *",
    )
    agent_shadow_enable.add_argument(
        "--allowed-dir",
        action="append",
        default=[],
        help="允许传入 AgentBackend 的脱敏/只读目录，可重复",
    )
    agent_shadow_enable.add_argument("--json", action="store_true", help="输出 JSON")
    agent_shadow_disable = agent_shadow_sub.add_parser("disable", help="关闭 AgentBackend shadow")
    agent_shadow_disable.add_argument("--json", action="store_true", help="输出 JSON")

    # golden agent shadow eval
    golden_parser = subparsers.add_parser("golden", help="Golden 评估")
    golden_sub = golden_parser.add_subparsers(dest="golden_cmd")
    golden_eval = golden_sub.add_parser("eval", help="运行 AgentBackend shadow golden eval")
    golden_eval.add_argument("--db-path", default="", help="覆盖 agent_authorization.db 路径")
    golden_eval.add_argument("--output-dir", default="", help="写入评估产物目录")
    golden_eval.add_argument(
        "--confirm-send-content",
        action="store_true",
        help="显式确认把脱敏 fixture 内容发送给本地 shadow agent",
    )
    golden_eval.add_argument("--json", action="store_true", help="输出 JSON")

    # trusted push proposal queue
    proposal_parser = subparsers.add_parser("proposal", help="可信推送 Proposal 管理")
    proposal_parser.add_argument("--db-path", default="", help="覆盖 trusted_push.db 路径")
    proposal_parser.add_argument("--wiki-base", default="", help="覆盖 Wiki 根目录")
    proposal_sub = proposal_parser.add_subparsers(dest="proposal_cmd")
    proposal_list = proposal_sub.add_parser("list", help="列出 Proposal")
    proposal_list.add_argument("--status", default="", help="按状态过滤")
    proposal_list.add_argument("--limit", type=int, default=50, help="最多返回数量")
    proposal_list.add_argument("--json", action="store_true", help="输出 JSON")
    proposal_show = proposal_sub.add_parser("show", help="查看单个 Proposal")
    proposal_show.add_argument("proposal_id")
    proposal_show.add_argument("--json", action="store_true", help="输出 JSON")
    proposal_approve = proposal_sub.add_parser("approve", help="批准并写入 Proposal")
    proposal_approve.add_argument("proposal_id")
    proposal_approve.add_argument("--yes", action="store_true", help="跳过确认提示")
    proposal_approve.add_argument(
        "--allow-high-risk", action="store_true", help="允许高风险 Proposal 写入"
    )
    proposal_approve.add_argument("--json", action="store_true", help="输出 JSON")
    proposal_reject = proposal_sub.add_parser("reject", help="拒绝 Proposal")
    proposal_reject.add_argument("proposal_id")
    proposal_reject.add_argument("--reason", default="", help="拒绝理由")
    proposal_reject.add_argument("--json", action="store_true", help="输出 JSON")
    proposal_edit = proposal_sub.add_parser("edit", help="编辑 Proposal 后重新过 Gate")
    proposal_edit.add_argument("proposal_id")
    proposal_edit.add_argument("--content", default=None, help="直接提供新 Markdown 内容")
    proposal_edit.add_argument("--content-file", default="", help="从系统临时文件外的文件读取内容")
    proposal_edit.add_argument("--editor", default="", help="无显式内容时使用的编辑器命令")
    proposal_edit.add_argument("--json", action="store_true", help="输出 JSON")
    proposal_recover = proposal_sub.add_parser("recover", help="恢复未闭环 Journal prepare")
    proposal_recover.add_argument(
        "--apply",
        action="store_true",
        help="实际写入恢复事件；默认只输出 dry-run 计划",
    )
    proposal_recover.add_argument(
        "--dry-run",
        action="store_true",
        help="只输出恢复计划（默认行为）",
    )
    proposal_recover.add_argument("--json", action="store_true", help="输出 JSON")
    proposal_audit = proposal_sub.add_parser("audit", help="审计 Journal hash chain 和未闭环写入")
    proposal_audit.add_argument("--json", action="store_true", help="输出 JSON")
    proposal_push = proposal_sub.add_parser("push", help="输出对话内结构化决策卡")
    proposal_push.add_argument("--limit", type=int, default=5, help="最多返回卡片数量")
    proposal_push.add_argument("--json", action="store_true", help="输出 JSON")
    proposal_decide = proposal_sub.add_parser("decide", help="执行对话卡片内联决策")
    proposal_decide.add_argument("proposal_id")
    proposal_decide.add_argument(
        "action",
        choices=["approve", "reject", "snooze", "edit"],
        help="执行 approve/reject/snooze/edit",
    )
    proposal_decide.add_argument("--yes", action="store_true", help="approve 时跳过确认提示")
    proposal_decide.add_argument(
        "--allow-high-risk", action="store_true", help="允许高风险 Proposal 写入"
    )
    proposal_decide.add_argument("--reason", default="", help="reject/snooze/edit 原因")
    proposal_decide.add_argument("--snooze-hours", type=int, default=24, help="snooze 小时数")
    proposal_decide.add_argument("--content", default=None, help="edit 时直接提供新 Markdown 内容")
    proposal_decide.add_argument("--content-file", default="", help="edit 时从文件读取内容")
    proposal_decide.add_argument("--editor", default="", help="edit 无显式内容时使用的编辑器命令")
    proposal_decide.add_argument("--json", action="store_true", help="输出 JSON")

    decision_inbox_parser = subparsers.add_parser("decision-inbox", help="统一决策收件箱")
    decision_inbox_sub = decision_inbox_parser.add_subparsers(dest="decision_inbox_cmd")
    decision_inbox_list = decision_inbox_sub.add_parser("list", help="列出待决策事项")
    decision_inbox_list.add_argument("--limit", type=int, default=50, help="最多返回数量")
    decision_inbox_list.add_argument("--json", action="store_true", help="输出 JSON")
    decision_inbox_act = decision_inbox_sub.add_parser("act", help="处理一个决策事项")
    decision_inbox_act.add_argument("item_id", help="形如 proposal:<id> / recap:<id>")
    decision_inbox_act.add_argument("action", help="approve/reject/resolve/dismiss/feedback/process")
    decision_inbox_act.add_argument("--reason", default="", help="处理原因")
    decision_inbox_act.add_argument("--allow-high-risk", action="store_true", help="允许高风险 proposal")
    decision_inbox_act.add_argument("--snooze-hours", type=int, default=24, help="proposal snooze 小时数")
    decision_inbox_act.add_argument("--json", action="store_true", help="输出 JSON")

    # daemon
    daemon_parser = subparsers.add_parser("daemon", help="后台守护进程")
    daemon_sub = daemon_parser.add_subparsers(dest="daemon_cmd")
    daemon_sub.add_parser("start", help="启动守护进程")
    daemon_sub.add_parser("stop", help="停止守护进程")
    daemon_sub.add_parser("status", help="查看守护进程状态")
    daemon_sub.add_parser("run", help="前台运行守护进程（调试）")

    # scheduler
    scheduler_parser = subparsers.add_parser("scheduler", help="定时任务管理")
    scheduler_sub = scheduler_parser.add_subparsers(dest="scheduler_cmd")
    scheduler_sub.add_parser("install-windows", help="注册 Windows 开机启动任务")
    scheduler_sub.add_parser("uninstall-windows", help="注销 Windows 开机启动任务")
    scheduler_sub.add_parser("status", help="查看 KIA 调度步骤状态")
    scheduler_sub.add_parser("list", help="列出 KIA 调度步骤")
    scheduler_reminders = scheduler_sub.add_parser("reminders", help="列出到期知识调度提醒")
    scheduler_reminders.add_argument("--json", action="store_true", help="输出 JSON")
    scheduler_schedule = scheduler_sub.add_parser("schedule", help="添加知识调度任务")
    scheduler_schedule.add_argument("task_type", help="任务类型，例如 review")
    scheduler_schedule.add_argument("subtype", help="任务子类型，例如 wiki")
    scheduler_schedule.add_argument("due_date", help="到期时间，ISO 格式，例如 2026-07-10T09:30:00")
    scheduler_schedule.add_argument("--context", default="", help="任务上下文")
    scheduler_schedule.add_argument("--periodic", action="store_true", help="创建周期任务")
    scheduler_schedule.add_argument("--period", default=None, help="周期: daily/weekly/biweekly/monthly/quarterly/yearly")
    scheduler_schedule.add_argument("--priority", type=int, default=0, help="提醒优先级")
    scheduler_tick = scheduler_sub.add_parser("tick", help="执行一次 KIA 调度 tick")
    scheduler_tick.add_argument(
        "--dry-run", action="store_true", help="只预览将执行的步骤，不实际运行"
    )

    # calibrate
    subparsers.add_parser("calibrate", help="画像校准与挑战反馈")

    # mcp serve
    mcp_parser = subparsers.add_parser("mcp", help="MCP 协议")
    mcp_sub = mcp_parser.add_subparsers(dest="mcp_cmd")
    mcp_sub.add_parser("serve", help="启动 MCP 服务器")

    # scorer
    scorer_parser = subparsers.add_parser("scorer", help="评分层管理")
    scorer_sub = scorer_parser.add_subparsers(dest="scorer_cmd")
    scorer_sub.add_parser("status", help="查看评分器和调度步骤状态")
    scorer_sub.add_parser("retrain", help="触发模型重训练")
    scorer_sub.add_parser("rollback", help="回滚到上一版本模型")

    # sync
    sync_parser = subparsers.add_parser("sync", help="同步层管理")
    sync_sub = sync_parser.add_subparsers(dest="sync_cmd")
    sync_sub.add_parser("status", help="查看同步状态")
    sync_sub.add_parser("retry-failed", help="重试失败的同步任务")
    backfill_parser = sync_sub.add_parser(
        "backfill", help="历史回填：全量/大批量扫描 Agent 历史会话"
    )
    backfill_parser.add_argument("--source", help="指定 Agent 源（如 claude/kimi/codex/all）")
    backfill_parser.add_argument("--since", type=float, default=0, help="时间范围（小时，0=全部）")
    backfill_parser.add_argument(
        "--max-turns", type=int, default=0, help="每 session 最大 turn 数（0=无限制）"
    )
    backfill_parser.add_argument(
        "--max-sessions", type=int, default=0, help="每 source 最大 session 数（0=无限制）"
    )
    backfill_parser.add_argument("--dry-run", action="store_true", help="只统计，不入队")
    audit_parser = sync_sub.add_parser("audit", help="同步完整性审计：报告缺洞、截断、覆盖率")
    audit_parser.add_argument(
        "--source", default="all", help="指定 Agent 源（如 claude/kimi/codex/all）"
    )

    # build-relation-index
    subparsers.add_parser("build-relation-index", help="重建关联上下文向量索引")

    # ingest
    ingest_parser = subparsers.add_parser("ingest", help="摄入本地文件/目录到 L1 并触发蒸馏")
    ingest_parser.add_argument("path", help="文件或目录路径")
    ingest_parser.add_argument("--agent-name", default="file", help="来源 Agent 名")
    ingest_parser.add_argument(
        "--recursive", action="store_true", default=True, help="递归摄入目录"
    )
    ingest_parser.add_argument("--no-recursive", action="store_true", help="不递归摄入目录")

    # import
    import_parser = subparsers.add_parser("import", help="导入用户指定文档到 Mnemos")
    import_parser.add_argument("path", help="文件路径")
    import_parser.add_argument(
        "--mode",
        choices=["parse", "capture", "distill", "watch"],
        default="distill",
        help="导入模式：parse 仅预览，capture 写 L1 并入队，distill 直达质量门蒸馏，watch 仅预检守护监听",
    )
    import_parser.add_argument(
        "--agent-name",
        default="trusted_user_document",
        help="来源 Agent 名",
    )
    import_parser.add_argument("--dry-run", action="store_true", help="只做路径、大小和隐私预检")
    import_parser.add_argument("--json", action="store_true", help="输出 JSON")

    # search
    search_parser = subparsers.add_parser("search", help="上下文感知搜索")
    search_parser.add_argument("query", help="搜索查询")
    search_parser.add_argument("--limit", type=int, default=10, help="最大结果数")
    search_parser.add_argument("--json", action="store_true", help="以 JSON 输出搜索结果")

    # wiki
    wiki_parser = subparsers.add_parser("wiki", help="Wiki 知识库操作")
    wiki_sub = wiki_parser.add_subparsers(dest="wiki_cmd")
    wiki_read_parser = wiki_sub.add_parser("read", help="读取指定 Wiki 页面")
    wiki_read_parser.add_argument("page_path", help="Wiki 页面路径（如 03-Tech/codex-cli.md）")
    wiki_read_parser.add_argument(
        "--depth", choices=["metadata", "summary", "full"], default="full", help="读取深度"
    )
    wiki_rebuild_parser = wiki_sub.add_parser("rebuild", help="选择性重跑 Wiki 页面")
    wiki_rebuild_parser.add_argument(
        "--selective",
        action="store_true",
        required=True,
        help="只重跑低可读性、未手工编辑、有 L1 对应的页面（必选标记）",
    )
    wiki_rebuild_parser.add_argument(
        "--dry-run", action="store_true", help="只生成报告，不实际写入"
    )
    wiki_rebuild_parser.add_argument(
        "--min-readability", type=float, default=60.0, help="可读性评分门槛 (0-100, 默认 60)"
    )
    wiki_rebuild_parser.add_argument(
        "--include-edited", action="store_true", help="包含已被用户手工编辑的页面"
    )
    wiki_rebuild_parser.add_argument(
        "--backup-dir",
        type=str,
        default="",
        help="备份目录 (默认: wiki/.rebuild-backup/YYYYMMDD-HHMMSS)",
    )
    # report
    report_parser = subparsers.add_parser("report", help="报告生成")
    report_sub = report_parser.add_subparsers(dest="report_cmd")
    report_sub.add_parser("generate", help="生成周报")
    # distill
    distill_parser = subparsers.add_parser("distill", help="蒸馏层管理")
    distill_sub = distill_parser.add_subparsers(dest="distill_cmd")
    distill_status_parser = distill_sub.add_parser("status", help="查看蒸馏队列状态")
    distill_status_parser.add_argument(
        "--unsafe-debug", "--show-paths", dest="unsafe_debug", action="store_true",
        help="输出未脱敏的本机路径，仅限本机排障",
    )
    distill_drain_parser = distill_sub.add_parser("drain", help="手动处理蒸馏队列积压")
    distill_drain_parser.add_argument("--limit", type=int, default=5, help="本次最多处理任务数")
    distill_drain_parser.add_argument(
        "--dry-run", action="store_true", help="只显示将处理的任务数，不实际调用 LLM"
    )
    distill_retry_parser = distill_sub.add_parser("retry-failed", help="重试 failed 蒸馏任务")
    distill_retry_parser.add_argument("--task-id", default="", help="指定 task_id 或 session_id")
    distill_retry_parser.add_argument("--all", action="store_true", help="重试全部 failed 任务")
    distill_retry_parser.add_argument("--limit", type=int, default=None, help="最多处理数量")
    distill_retry_parser.add_argument("--reason", default="", help="操作原因")
    distill_retry_parser.add_argument("--json", action="store_true", help="输出 JSON")
    distill_archive_parser = distill_sub.add_parser(
        "archive-failed", help="归档已确认不可重试的 failed 蒸馏任务"
    )
    distill_archive_parser.add_argument("--task-id", default="", help="指定 task_id 或 session_id")
    distill_archive_parser.add_argument("--all", action="store_true", help="归档全部 failed 任务")
    distill_archive_parser.add_argument("--limit", type=int, default=None, help="最多处理数量")
    distill_archive_parser.add_argument("--reason", default="", help="操作原因")
    distill_archive_parser.add_argument("--json", action="store_true", help="输出 JSON")
    distill_reset_parser = distill_sub.add_parser("reset-timeouts", help="重置超时 processing")
    distill_reset_parser.add_argument("--minutes", type=int, default=30, help="超时分钟数")
    distill_reset_parser.add_argument("--json", action="store_true", help="输出 JSON")
    distill_sub.add_parser(
        "audit", help="蒸馏完整性审计：报告截断、缺失 prompt_version、source_coverage"
    )
    distill_backfill_parser = distill_sub.add_parser(
        "backfill-metadata",
        help="回填历史蒸馏页缺失的 prompt version、source coverage、input mode",
    )
    distill_backfill_parser.add_argument(
        "--dry-run", action="store_true", help="只统计将要回填的页面"
    )
    distill_backfill_parser.add_argument(
        "--limit", type=int, default=None, help="最多处理的缺字段页面数"
    )
    evidence_backfill_parser = distill_sub.add_parser(
        "evidence-backfill",
        help="从 provenance 表回填 Wiki 页面 source_refs；默认 dry-run",
    )
    evidence_backfill_parser.add_argument(
        "--apply", action="store_true", help="实际写入 page_metrics 和 frontmatter"
    )
    evidence_backfill_parser.add_argument("--json", action="store_true", help="输出 JSON")
    evidence_backfill_parser.add_argument(
        "--limit", type=int, default=None, help="最多处理的 changed pages"
    )
    evidence_backfill_parser.add_argument(
        "--max-refs-per-page", type=int, default=None, help="临时覆盖单页 refs 上限"
    )
    evidence_backfill_parser.add_argument(
        "--frontmatter-ref-limit", type=int, default=None, help="临时覆盖 frontmatter refs 数"
    )
    evidence_backfill_parser.add_argument(
        "--unresolved-sample-limit", type=int, default=None, help="临时覆盖未解析样本数"
    )
    evidence_backfill_parser.add_argument(
        "--change-sample-limit", type=int, default=None, help="临时覆盖 JSON changes 样本数"
    )
    evidence_backfill_parser.add_argument(
        "--relation-evidence-type",
        action="append",
        dest="relation_evidence_types",
        default=None,
        help="允许参与 source_refs 的 relation_evidence.evidence_type，可重复",
    )
    evidence_backfill_parser.add_argument(
        "--skip-relation-evidence", action="store_true", help="不使用 KG relation_evidence"
    )
    evidence_backfill_parser.add_argument(
        "--no-frontmatter", action="store_true", help="只写 page_metrics，不写 Markdown"
    )
    evidence_backfill_parser.add_argument(
        "--no-report", action="store_true", help="不写 99-Reports/认知数据就绪度 报告"
    )
    evidence_backfill_parser.add_argument(
        "--report-dir", default=None, help="临时覆盖 Wiki 内报告目录"
    )
    distill_actions_parser = distill_sub.add_parser(
        "actions",
        help="查看或处理 distill action router 日志",
    )
    distill_actions_parser.add_argument("--json", action="store_true", help="输出 JSON")
    distill_actions_parser.add_argument(
        "--session-id", default=None, help="按 source_session_id/session_id 查询 action"
    )
    distill_actions_parser.add_argument(
        "--action-id", default=None, help="查询单个 action 及 knowledge action 明细"
    )
    distill_actions_parser.add_argument(
        "--limit", type=int, default=20, help="最近 action 查询数量，默认 20"
    )
    distill_actions_parser.add_argument(
        "--process-queued", action="store_true", help="处理 queued cognitive actions"
    )
    distill_actions_parser.add_argument(
        "--process-limit", type=int, default=100, help="最多处理 queued cognitive actions 数量"
    )

    # events
    events_parser = subparsers.add_parser("events", help="事件总线管理")
    events_sub = events_parser.add_subparsers(dest="events_cmd")
    cleanup_parser = events_sub.add_parser("cleanup", help="清理旧事件和死信（默认 dry-run）")
    cleanup_parser.add_argument("--confirm", action="store_true", help="确认执行删除和 VACUUM")
    events_sub.add_parser("archive-orphans", help="归档无消费者的历史 pending 事件")
    replay_parser = events_sub.add_parser("replay", help="重放事件总线死信")
    replay_parser.add_argument("--trace-id", default="", help="重放指定 trace_id 的死信")
    replay_parser.add_argument(
        "--no-consumer",
        action="store_true",
        help="重放已有消费者的 no_consumer 死信（未注册消费者的类型不会重放）",
    )
    replay_parser.add_argument(
        "--event-type",
        dest="event_types",
        action="append",
        default=[],
        help="限定 no_consumer 重放的事件类型，可重复传入",
    )
    replay_parser.add_argument("--limit", type=int, default=100, help="最多重放死信数量")
    events_sub.add_parser("stats", help="查看事件总线统计")

    # observe (L3)
    observe_parser = subparsers.add_parser("observe", help="Observation 观察层管理")
    observe_sub = observe_parser.add_subparsers(dest="observe_cmd")
    observe_run_parser = observe_sub.add_parser("run", help="运行 Observation Engine")
    observe_run_parser.add_argument("--full", action="store_true", help="全量提取")
    observe_run_parser.add_argument("--since", help="增量模式 ISO 时间戳")
    observe_search_parser = observe_sub.add_parser("search", help="搜索 Observation Index")
    observe_search_parser.add_argument("--dimension", default="", help="按维度筛选")
    observe_search_parser.add_argument("--source-type", default="", help="按来源类型筛选")
    observe_search_parser.add_argument("--limit", type=int, default=20, help="返回数量上限")
    observe_sub.add_parser("stats", help="查看 Observation 统计")

    # reflect (L4)
    reflect_parser = subparsers.add_parser("reflect", help="Reflection 反射层管理")
    reflect_sub = reflect_parser.add_subparsers(dest="reflect_cmd")
    reflect_on_parser = reflect_sub.add_parser("on", help="对文本触发 Reflection")
    reflect_on_parser.add_argument("text", help="用户输入文本")
    reflect_on_parser.add_argument(
        "--auto-llm",
        dest="auto_llm",
        action="store_true",
        default=True,
        help="由 Mnemos 自动调用 LLM 生成洞察（默认启用）",
    )
    reflect_on_parser.add_argument(
        "--no-auto-llm",
        dest="auto_llm",
        action="store_false",
        help="只生成 prompt，由宿主 Agent 自行调用 LLM",
    )
    reflect_manual_parser = reflect_sub.add_parser("manual", help="手动触发通用 Reflection")
    reflect_manual_parser.add_argument("query", nargs="?", default="", help="查询语句")
    reflect_manual_parser.add_argument(
        "--auto-llm",
        dest="auto_llm",
        action="store_true",
        default=True,
        help="由 Mnemos 自动调用 LLM 生成洞察（默认启用）",
    )
    reflect_manual_parser.add_argument(
        "--no-auto-llm",
        dest="auto_llm",
        action="store_false",
        help="只生成 prompt，由宿主 Agent 自行调用 LLM",
    )
    reflect_pending_parser = reflect_sub.add_parser("pending", help="查看待反馈 Reflection")
    reflect_pending_parser.add_argument(
        "--hours-since", type=float, default=24, help="时间范围（小时）"
    )
    reflect_pending_parser.add_argument("--limit", type=int, default=20, help="返回数量上限")
    reflect_feedback_parser = reflect_sub.add_parser("feedback", help="提交 Reflection 反馈")
    reflect_feedback_parser.add_argument("reflection_id", help="Reflection 记录 ID")
    reflect_feedback_parser.add_argument(
        "feedback_type",
        choices=["accurate", "inaccurate", "insightful", "irrelevant"],
        help="反馈类型",
    )
    reflect_feedback_parser.add_argument("--comment", default="", help="可选评论")

    # feedback (L5)
    feedback_parser = subparsers.add_parser("feedback", help="Feedback 反馈层管理")
    feedback_sub = feedback_parser.add_subparsers(dest="feedback_cmd")
    feedback_stats_parser = feedback_sub.add_parser("stats", help="反馈统计")
    feedback_stats_parser.add_argument("--days", type=int, default=30, help="统计天数")

    # vaults
    vaults_parser = subparsers.add_parser("vaults", help="Vault 管理")
    vaults_sub = vaults_parser.add_subparsers(dest="vaults_cmd")
    vaults_sub.add_parser("status", help="查看 vault 状态")
    vaults_audit_parser = vaults_sub.add_parser(
        "audit-placement", help="审计 Vault 文件摆放与同名冲突"
    )
    vaults_audit_parser.add_argument("--json", action="store_true", help="输出 JSON")
    vaults_content_audit_parser = vaults_sub.add_parser(
        "audit-content", help="审计 Vault 展示、分类与结构化输出问题"
    )
    vaults_content_audit_parser.add_argument("--json", action="store_true", help="输出 JSON")
    link_scope_choices = (
        "all", "kg", "shadow", "observation", "reflection", "persona",
        "reminder", "dispute", "reports",
    )
    vaults_link_audit_parser = vaults_sub.add_parser(
        "audit-links", help="审计 Vault 内部 wikilink 断链"
    )
    vaults_repair_parser = vaults_sub.add_parser(
        "repair-placement", help="归档内容完全相同的同名 Vault 文件（默认 dry-run）"
    )
    vaults_repair_parser.add_argument("--apply", action="store_true", help="实际移动到归档")
    vaults_repair_parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="允许在 dirty vault 下执行 apply",
    )
    vaults_repair_parser.add_argument("--json", action="store_true", help="输出 JSON")
    vaults_repair_parser.add_argument("--limit", type=int, default=None, help="最多处理数量")
    vaults_link_repair_parser = vaults_sub.add_parser(
        "repair-links", help="修复 Vault 内部 wikilink；默认 dry-run"
    )
    for link_parser in (vaults_link_audit_parser, vaults_link_repair_parser):
        link_parser.add_argument("--json", action="store_true", help="输出 JSON")
        link_parser.add_argument("--vault", help="覆盖默认 Mnemos vault 路径")
        link_parser.add_argument("--limit", type=int, default=20, help="样本数量")
        link_parser.add_argument("--scope", choices=link_scope_choices, default="all")
    vaults_link_repair_parser.add_argument("--strip-broken", action="store_true")
    vaults_link_repair_parser.add_argument("--apply", action="store_true", help="实际写入")
    vaults_link_repair_parser.add_argument("--allow-dirty", action="store_true")
    vaults_sync_parser = vaults_sub.add_parser(
        "sync", help="手动全量重建认知 Vault 投影；默认 dry-run"
    )
    vaults_sync_parser.add_argument("--apply", action="store_true", help="实际写入投影")
    vaults_sync_parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="允许在 dirty vault 下执行 apply",
    )
    vaults_sync_parser.add_argument("--dry-run", action="store_true", help="只预览，不写入")
    vaults_sync_parser.add_argument("--no-commit", action="store_true", help="不创建 git 快照")

    # link-probe
    link_probe_parser = subparsers.add_parser("link-probe", help="外部链接可达性探测")
    link_probe_sub = link_probe_parser.add_subparsers(dest="link_probe_cmd")
    link_probe_run_parser = link_probe_sub.add_parser("run", help="批量探测 pending 链接")
    link_probe_run_parser.add_argument("--batch-size", type=int, default=50, help="每批探测数量")
    link_probe_sub.add_parser("status", help="查看链接探测队列状态")

    # cognitive-graph
    cg_parser = subparsers.add_parser("cognitive-graph", help="跨层认知图管理")
    cg_sub = cg_parser.add_subparsers(dest="cg_cmd")
    cg_sub.add_parser("stats", help="查看认知图统计")
    cg_sub.add_parser("reconcile", help="触发 reconciliation")
    cg_ingest_parser = cg_sub.add_parser("ingest", help="手动摄入一个事件")
    cg_ingest_parser.add_argument("--event-type", default="distill_complete", help="事件类型")
    cg_ingest_parser.add_argument("--session-id", default="", help="session_id")
    cg_ingest_parser.add_argument("--page-path", default="", help="wiki 页面路径")

    # kg
    kg_parser = subparsers.add_parser("kg", help="知识图谱运维")
    kg_sub = kg_parser.add_subparsers(dest="kg_cmd")
    kg_sub.add_parser("doctor", help="诊断 KG/CG 健康状态")
    kg_sub.add_parser("rebuild-entities", help="扫描全 Wiki 重建 entities 表")
    kg_consistency_parser = kg_sub.add_parser(
        "consistency", help="审计或修复 knowledge_graph.db 硬一致性"
    )
    kg_consistency_parser.add_argument("--apply", action="store_true", help="实际执行修复")
    kg_consistency_parser.add_argument("--json", action="store_true", help="输出 JSON")
    kg_consistency_parser.add_argument(
        "--no-backup", action="store_true", help="apply 时跳过定向 KG 备份"
    )
    kg_normalize_parser = kg_sub.add_parser(
        "normalize-endpoints", help="审计或修复 KG endpoint 语义归一化/路径迁移"
    )
    kg_normalize_parser.add_argument("--apply", action="store_true", help="实际执行修复")
    kg_normalize_parser.add_argument("--json", action="store_true", help="输出 JSON")
    kg_normalize_parser.add_argument(
        "--no-backup", action="store_true", help="apply 时跳过定向 KG 备份"
    )
    kg_normalize_parser.add_argument(
        "--min-concept-refs",
        type=int,
        default=2,
        help="概念实体物化所需最小关系引用数",
    )
    kg_normalize_parser.add_argument(
        "--prune-invalid",
        action="store_true",
        help="删除明确非法 endpoint 对应的关系行（需配合 --apply 才会落库）",
    )
    kg_build_graph_parser = kg_sub.add_parser(
        "build-graph", help="扫描 00-Inbox 重建 Wiki 关系图"
    )
    kg_build_graph_parser.add_argument("--wiki-base", default=None, help="Wiki 根目录")
    kg_export_dataview_parser = kg_sub.add_parser(
        "export-dataview", help="导出 Obsidian Dataview 查询块"
    )
    kg_export_dataview_parser.add_argument("page", help="wiki 页面路径")

    # genos
    genos_parser = subparsers.add_parser("genos", help="知识 DNA 运维")
    genos_sub = genos_parser.add_subparsers(dest="genos_cmd")
    genos_compute_parser = genos_sub.add_parser("compute", help="计算并保存页面 DNA")
    genos_compute_parser.add_argument("page", help="wiki 页面路径")
    genos_compute_parser.add_argument("--wiki-base", default=None, help="Wiki 根目录")
    genos_compute_parser.add_argument("--json", action="store_true", help="输出 JSON")
    genos_duplicate_parser = genos_sub.add_parser("duplicate", help="检查页面 DNA 重复")
    genos_duplicate_parser.add_argument("page", help="wiki 页面路径")
    genos_duplicate_parser.add_argument("--wiki-base", default=None, help="Wiki 根目录")
    genos_duplicate_parser.add_argument("--json", action="store_true", help="输出 JSON")

    # capsule
    capsule_parser = subparsers.add_parser("capsule", help="时间胶囊管理")
    capsule_sub = capsule_parser.add_subparsers(dest="capsule_cmd")
    capsule_list_parser = capsule_sub.add_parser("list", help="列出所有胶囊")
    capsule_list_parser.add_argument("--page", default=None, help="按页面路径过滤")
    capsule_list_parser.add_argument("--status", default=None, help="按状态过滤")
    capsule_due_parser = capsule_sub.add_parser("due", help="列出即将到期提醒")
    capsule_due_parser.add_argument("--days", type=int, default=7, help="提前天数")
    capsule_sub.add_parser("overdue", help="列出已逾期提醒")
    capsule_set_parser = capsule_sub.add_parser("set", help="为页面设置人工提醒")
    capsule_set_parser.add_argument("page_path", help="页面路径")
    capsule_set_parser.add_argument("--days", type=int, default=90, help="多少天后提醒")
    capsule_complete_parser = capsule_sub.add_parser("complete", help="标记胶囊完成")
    capsule_complete_parser.add_argument("capsule_id", type=int, help="胶囊 ID")
    capsule_dismiss_parser = capsule_sub.add_parser("dismiss", help="忽略胶囊提醒")
    capsule_dismiss_parser.add_argument("capsule_id", type=int, help="胶囊 ID")
    capsule_snooze_parser = capsule_sub.add_parser("snooze", help="推迟胶囊")
    capsule_snooze_parser.add_argument("capsule_id", type=int, help="胶囊 ID")
    capsule_snooze_parser.add_argument("--days", type=int, default=7, help="推迟天数")
    capsule_sub.add_parser("report", help="生成提醒报告")

    # policy
    policy_parser = subparsers.add_parser("policy", help="有效策略管理")
    policy_sub = policy_parser.add_subparsers(dest="policy_cmd")
    policy_sub.add_parser("list", help="列出待裁决策略 shadow")
    policy_commit_parser = policy_sub.add_parser("commit", help="强制提交策略 shadow")
    policy_commit_parser.add_argument("experiment_id", help="实验 ID")
    policy_rollback_parser = policy_sub.add_parser("rollback", help="强制回滚策略 shadow")
    policy_rollback_parser.add_argument("experiment_id", help="实验 ID")

    # version
    version_parser = subparsers.add_parser("version", help="版本时间旅行")
    version_sub = version_parser.add_subparsers(dest="version_cmd")
    version_list_parser = version_sub.add_parser("list", help="列出页面版本")
    version_list_parser.add_argument("page_path", help="wiki 页面路径（相对或绝对）")
    version_diff_parser = version_sub.add_parser("diff", help="对比版本")
    version_diff_parser.add_argument("page_path", help="wiki 页面路径")
    version_diff_parser.add_argument("--from", dest="from_id", default=None, help="起始版本 ID")
    version_diff_parser.add_argument("--to", dest="to_id", default=None, help="目标版本 ID")
    version_restore_parser = version_sub.add_parser("restore", help="回滚到指定版本")
    version_restore_parser.add_argument("page_path", help="wiki 页面路径")
    version_restore_parser.add_argument("snapshot_id", help="目标版本 ID")
    version_restore_parser.add_argument(
        "--no-backup", action="store_true", help="恢复前不备份当前版本"
    )
    version_create_parser = version_sub.add_parser("create", help="手动创建快照")
    version_create_parser.add_argument("page_path", help="wiki 页面路径")
    version_create_parser.add_argument("--summary", default="", help="变更摘要")
    version_sub.add_parser("scan-all", help="扫描全库为变更页面创建快照")

    # shadow
    shadow_parser = subparsers.add_parser("shadow", help="影子页面管理")
    shadow_sub = shadow_parser.add_subparsers(dest="shadow_cmd")
    shadow_sync_parser = shadow_sub.add_parser("sync", help="批量生成/更新影子页面")
    shadow_sync_parser.add_argument("--page", default="*.md", help="页面匹配模式")
    shadow_premise_parser = shadow_sub.add_parser("premise", help="批量验证页面前提条件")
    shadow_premise_parser.add_argument("--page", default="*.md", help="页面匹配模式")
    shadow_sub.add_parser("status", help="查看影子页面统计")

    # stress
    stress_parser = subparsers.add_parser("stress", help="知识压力测试")
    stress_sub = stress_parser.add_subparsers(dest="stress_cmd")
    stress_run_parser = stress_sub.add_parser("run", help="运行压力测试")
    stress_run_parser.add_argument("--page", default=None, help="只测试指定页面")
    stress_run_parser.add_argument("--limit", type=int, default=None, help="最多测试页面数")
    stress_run_parser.add_argument("--dry-run", action="store_true", help="不写入数据库")
    stress_sub.add_parser("status", help="查看压力测试统计")

    # dispute
    dispute_parser = subparsers.add_parser("dispute", help="争议仲裁管理")
    dispute_sub = dispute_parser.add_subparsers(dest="dispute_cmd")
    dispute_scan_parser = dispute_sub.add_parser("scan", help="手动触发争议扫描")
    dispute_scan_parser.add_argument(
        "--max-disputes", type=int, default=None, help="本次最多创建争议数"
    )
    dispute_list_parser = dispute_sub.add_parser("list", help="列出争议页面")
    dispute_list_parser.add_argument(
        "--unresolved-only", action="store_true", help="只列出未解决争议"
    )
    dispute_resolve_parser = dispute_sub.add_parser("resolve", help="解决指定争议")
    dispute_resolve_parser.add_argument("page_path", help="争议页面路径（相对 wiki 或绝对路径）")
    dispute_resolve_parser.add_argument(
        "--resolution",
        required=True,
        choices=["adopt_new", "keep_old", "keep_both", "need_more_info"],
        help="裁决结果",
    )
    dispute_resolve_parser.add_argument("--context", default="", help="附加说明上下文")
    dispute_rollback_parser = dispute_sub.add_parser(
        "rollback-context", help="回滚同步到原始页面的争议上下文"
    )
    dispute_rollback_parser.add_argument(
        "page_path", help="争议页面路径（相对 wiki 或绝对路径）"
    )
    dispute_sub.add_parser("stats", help="查看争议统计")

    dispute_weights_parser = dispute_sub.add_parser("weights", help="查看/调整争议仲裁权重")
    dispute_weights_parser.add_argument(
        "--set",
        action="append",
        dest="set_weights",
        metavar="DIM=VALUE",
        help="设置某一维度权重，可多次使用（如 --set confidence=0.3）",
    )
    dispute_weights_parser.add_argument(
        "--reset", action="store_true", help="清除 state 权重，回退到 config/默认值"
    )
    dispute_weights_parser.add_argument(
        "--learn", action="store_true", help="手动触发一次自适应权重学习"
    )

    dispute_show_parser = dispute_sub.add_parser("show", help="查看指定争议页的评分详情")
    dispute_show_parser.add_argument("page_path", help="争议页面路径（相对 wiki 或绝对路径）")

    # blindspot
    blindspot_parser = subparsers.add_parser("blindspot", help="盲区管理")
    blindspot_sub = blindspot_parser.add_subparsers(dest="blindspot_cmd")
    blindspot_list_parser = blindspot_sub.add_parser("list", help="列出盲区记录")
    blindspot_list_parser.add_argument(
        "--status", default="", help="按状态过滤 (detected/reminded/investigating/resolved/ignored)"
    )
    blindspot_sub.add_parser("status", help="查看盲区统计")
    blindspot_ignore_parser = blindspot_sub.add_parser("ignore", help="忽略指定盲区")
    blindspot_ignore_parser.add_argument("topic", help="盲区 topic")
    blindspot_ignore_parser.add_argument("--asset-id", default="", help="精确 typed asset ID")
    blindspot_resolve_parser = blindspot_sub.add_parser(
        "resolve", help="拒绝手工关闭；仅保留为 fail-closed 兼容入口"
    )
    blindspot_resolve_parser.add_argument("topic", help="盲区 topic")
    blindspot_resolve_parser.add_argument("--asset-id", default="", help="精确 typed asset ID")
    blindspot_resolve_parser.add_argument("--page", default="", help="关闭它的 wiki 页面路径")
    blindspot_resolve_parser.add_argument(
        "--resolution-receipt",
        default="",
        help="已弃用；字符串 receipt 不构成独立覆盖证据",
    )
    blindspot_cleanup_parser = blindspot_sub.add_parser("cleanup", help="清理已解决的旧盲区记录")
    blindspot_cleanup_parser.add_argument(
        "--days", type=int, default=15, help="清理多少天前已解决的记录"
    )

    # persona
    persona_parser = subparsers.add_parser("persona", help="画像系统管理")
    persona_sub = persona_parser.add_subparsers(dest="persona_cmd")
    persona_metrics_parser = persona_sub.add_parser(
        "behavior-metrics", help="查看画像行为提示使用指标"
    )
    persona_metrics_parser.add_argument("--days", type=int, default=30, help="统计最近多少天")
    persona_daily_parser = persona_sub.add_parser("daily-summary", help="查看指定日期的画像信号摘要")
    persona_daily_parser.add_argument("date", nargs="?", default="", help="日期 YYYY-MM-DD，默认今天")
    persona_daily_parser.add_argument("--json", action="store_true", help="输出 JSON")
    persona_projects_parser = persona_sub.add_parser("projects", help="列出最近有画像信号的项目")
    persona_projects_parser.add_argument("--days", type=int, default=30, help="统计最近多少天")
    persona_projects_parser.add_argument("--json", action="store_true", help="输出 JSON")
    persona_project_parser = persona_sub.add_parser(
        "project-signals", help="查看指定项目隔离后的画像信号"
    )
    persona_project_parser.add_argument("project_dir", help="项目目录路径")
    persona_project_parser.add_argument("--days", type=int, default=30, help="统计最近多少天")
    persona_project_parser.add_argument("--json", action="store_true", help="输出 JSON")
    persona_recent_parser = persona_sub.add_parser(
        "recent-signals", help="查看最近 notes/wechat 原始画像信号"
    )
    persona_recent_parser.add_argument(
        "--source", choices=["all", "notes", "wechat"], default="all", help="信号来源"
    )
    persona_recent_parser.add_argument("--days", type=int, default=30, help="统计最近多少天")
    persona_recent_parser.add_argument("--json", action="store_true", help="输出 JSON")

    # freshness
    freshness_parser = subparsers.add_parser("freshness", help="知识新鲜度管理")
    freshness_sub = freshness_parser.add_subparsers(dest="freshness_cmd")
    freshness_list_parser = freshness_sub.add_parser("list", help="列出页面新鲜度状态")
    freshness_list_parser.add_argument(
        "--status", choices=["stale", "fresh", "all"], default="all", help="过滤状态"
    )
    freshness_refresh_parser = freshness_sub.add_parser("refresh", help="刷新指定页面")
    freshness_refresh_parser.add_argument("page_path", help="Wiki 页面路径（相对或绝对）")
    freshness_refresh_all_parser = freshness_sub.add_parser("refresh-all", help="批量刷新过期页面")
    freshness_refresh_all_parser.add_argument("--limit", type=int, default=10, help="最多刷新数量")

    # entropy
    entropy_parser = subparsers.add_parser("entropy", help="知识熵减管理")
    entropy_sub = entropy_parser.add_subparsers(dest="entropy_cmd")
    entropy_scan_parser = entropy_sub.add_parser("scan", help="运行熵减扫描")
    entropy_scan_parser.add_argument("--limit", type=int, default=None, help="限制扫描对数")
    entropy_scan_parser.add_argument(
        "--write-report", action="store_true", help="写入报告到 99-Reports"
    )
    entropy_autofix_parser = entropy_sub.add_parser("auto-fix", help="自动处理熵减建议")
    entropy_autofix_parser.add_argument("--apply-links", action="store_true", help="建立 KG 关系")

    # immune
    immune_parser = subparsers.add_parser("immune", help="知识免疫扫描管理")
    immune_sub = immune_parser.add_subparsers(dest="immune_cmd")
    immune_scan_parser = immune_sub.add_parser("scan", help="运行知识免疫扫描")
    immune_scan_parser.add_argument(
        "--write-report",
        action="store_true",
        help="写入 Markdown 报告到 Wiki 99-Reports",
    )

    # reminder
    reminder_parser = subparsers.add_parser("reminder", help="对话提醒队列管理")
    reminder_sub = reminder_parser.add_subparsers(dest="reminder_cmd")
    reminder_status = reminder_sub.add_parser("status", help="查看提醒队列状态")
    reminder_status.add_argument("--json", action="store_true", help="输出 JSON")
    reminder_list_parser = reminder_sub.add_parser("list", help="列出提醒")
    reminder_list_parser.add_argument(
        "--status",
        choices=[
            "pending",
            "pushed",
            "resolved",
            "deferred",
            "ignored",
            "dismissed",
            "expired",
            "all",
        ],
        default="all",
        help="状态过滤",
    )
    reminder_push_parser = reminder_sub.add_parser("push", help="手动触发兜底推送")
    reminder_push_parser.add_argument("--max", type=int, default=None, help="最大推送数")
    reminder_resolve_parser = reminder_sub.add_parser("resolve", help="关闭指定提醒")
    reminder_resolve_parser.add_argument("reminder_id", nargs="?", help="提醒 ID")
    reminder_resolve_parser.add_argument("--issue", help="按 issue_id 关闭未关闭提醒")
    reminder_resolve_parser.add_argument("--choice", default="已处理", help="用户选择")
    reminder_dismiss_parser = reminder_sub.add_parser("dismiss", help="忽略指定提醒")
    reminder_dismiss_parser.add_argument("reminder_id", nargs="?", help="提醒 ID")
    reminder_dismiss_parser.add_argument("--issue", help="按 issue_id 忽略未关闭提醒")
    reminder_dismiss_parser.add_argument("--reason", default="dismissed", help="忽略原因")
    reminder_expire_parser = reminder_sub.add_parser("expire-stale", help="过期旧 pending/deferred 提醒")
    reminder_expire_parser.add_argument("--days", type=int, default=30, help="超过多少天即过期")
    reminder_expire_parser.add_argument("--limit", type=int, default=None, help="最多处理数量")
    reminder_expire_parser.add_argument("--severity", default="", help="按 severity 过滤")
    reminder_expire_parser.add_argument("--json", action="store_true", help="输出 JSON")

    # recap
    recap_parser = subparsers.add_parser("recap", help="复盘任务队列管理")
    recap_sub = recap_parser.add_subparsers(dest="recap_cmd")
    recap_list = recap_sub.add_parser("list", help="列出复盘任务")
    recap_list.add_argument(
        "--status",
        choices=["pending", "resolved", "dismissed", "ignored", "reminded", "cancelled", "all"],
        default="pending",
        help="状态过滤",
    )
    recap_list.add_argument("--severity", default="", help="按 severity 过滤")
    recap_list.add_argument("--source", default="", help="按 source 过滤")
    recap_list.add_argument("--limit", type=int, default=50, help="最多显示数量")
    recap_list.add_argument("--json", action="store_true", help="输出 JSON")
    for subcmd, help_text in (
        ("resolve", "标记复盘任务已解决"),
        ("dismiss", "忽略/关闭复盘任务"),
    ):
        recap_action = recap_sub.add_parser(subcmd, help=help_text)
        recap_action.add_argument("task_id", nargs="?", help="复盘任务 ID")
        recap_action.add_argument("--all", action="store_true", help="批量处理 pending 任务")
        recap_action.add_argument("--severity", default="", help="批量处理指定 severity")
        recap_action.add_argument("--source", default="system", help="批量处理指定 source")
        recap_action.add_argument("--limit", type=int, default=None, help="最多处理数量")
        recap_action.add_argument("--reason", default="", help="操作原因")
        recap_action.add_argument("--actor", default="cli", help="操作者")
        recap_action.add_argument("--json", action="store_true", help="输出 JSON")

    # application signals
    signals_parser = subparsers.add_parser("signals", help="应用层信号管理")
    signals_sub = signals_parser.add_subparsers(dest="signals_cmd")
    signals_list_parser = signals_sub.add_parser("list", help="列出应用层信号及触发原因")
    signals_list_parser.add_argument("--limit", type=int, default=20, help="最多显示数量")
    signals_list_parser.add_argument("--json", action="store_true", help="输出 JSON")

    # verify
    verify_parser = subparsers.add_parser("verify", help="受控求证队列")
    verify_sub = verify_parser.add_subparsers(dest="verify_cmd")
    verify_plan_parser = verify_sub.add_parser("plan", help="只读规划 verification queue")
    verify_plan_parser.add_argument("--limit", type=int, default=None, help="最多返回候选数量")
    verify_plan_parser.add_argument("--json", action="store_true", help="输出 JSON")
    verify_run_parser = verify_sub.add_parser("run", help="运行 verification queue；默认 dry-run")
    verify_run_parser.add_argument("--apply", action="store_true", help="写入 verification DB 和报告")
    verify_run_parser.add_argument("--limit", type=int, default=None, help="最多处理候选数量")
    verify_run_parser.add_argument("--json", action="store_true", help="输出 JSON")

    # db maintenance
    db_parser = subparsers.add_parser("db", help="数据库维护")
    db_sub = db_parser.add_subparsers(dest="db_cmd")
    db_maintenance_parser = db_sub.add_parser("maintenance", help="运行数据库存留清理与维护")
    db_maintenance_parser.add_argument(
        "--dry-run", action="store_true", help="只预览将清理的行数，不实际删除"
    )

    parser.description = _build_docstring(parser)
    return parser


__doc__ = _build_docstring(build_parser())


def _configure_environment() -> None:
    """设置敏感 umask 与 Windows 控制台编码。"""
    from core.utils import set_sensitive_umask

    set_sensitive_umask()

    if sys.platform == "win32":
        try:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            if hasattr(sys.stderr, "reconfigure"):
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def _get_subparser(parser, name: str):
    """获取顶层命令对应的子解析器，用于 print_help。"""
    if parser._subparsers:
        actions = parser._subparsers._group_actions
        if actions and hasattr(actions[0], "choices"):
            return actions[0].choices.get(name)
    return None


def _handle_health(args) -> None:
    """health 命令：输出报告并决定退出码。"""
    from core.ops.health_check import (  # noqa: PLC0415
        build_health_report,
        build_health_report_quiet,
        print_human_report,
    )

    if getattr(args, "json", False):
        report = build_health_report_quiet(
            show_sensitive=bool(getattr(args, "unsafe_debug", False))
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        report = build_health_report(
            show_sensitive=bool(getattr(args, "unsafe_debug", False))
        )
        print_human_report(report)
    sys.exit(0 if report.get("usable", report["ok"]) else 1)


# (command_name, exit_mode)
# exit_mode: None -> ignore return; "bool" -> sys.exit(0/1); "code" -> sys.exit(return)
_COMMAND_ROUTES = {
    "init": ("cmd_init", None),
    "status": ("cmd_status", None),
    "config": ("cmd_config", None),
    "secrets": ("cmd_secrets", "code"),
    "agent": ("cmd_agent", None),
    "golden": ("cmd_golden", "code"),
    "daemon": ("cmd_daemon", None),
    "scheduler": ("cmd_scheduler", "code"),
    "calibrate": ("cmd_calibrate", None),
    "scorer": ("cmd_scorer", None),
    "sync": ("cmd_sync", None),
    "push": ("cmd_push", "code"),
    "search": ("cmd_search", None),
    "build-relation-index": ("cmd_build_relation_index", None),
    "wiki": ("cmd_wiki", None),
    "report": ("cmd_report", None),
    "distill": ("cmd_distill", None),
    "proposal": ("cmd_proposal", "code"),
    "decision-inbox": ("cmd_decision_inbox", "code"),
    "events": ("cmd_events", None),
    "perf": ("cmd_perf", None),
    "observe": ("cmd_observe", None),
    "reflect": ("cmd_reflect", None),
    "feedback": ("cmd_feedback", None),
    "vaults": ("cmd_vaults", "code"),
    "cognitive-graph": ("cmd_cognitive_graph", None),
    # mcp serve 需要同时满足命令和子命令，单独处理
    "doctor": ("cmd_doctor", "bool"),
    "ingest": ("cmd_ingest", "code"),
    "import": ("cmd_ingest", "code"),
    "link-probe": ("cmd_link_probe", "code"),
    "setup": ("cmd_setup", "code"),
    "upgrade": ("cmd_upgrade", "code"),
    "uninstall": ("cmd_uninstall", "code"),
}


# 需要子命令存在且子命令决定具体 handler 的命令
_SUBCOMMAND_ROUTES = {
    "kg": {
        "doctor": ("cmd_kg_doctor", "code"),
        "rebuild-entities": ("cmd_kg_rebuild_entities", "code"),
        "consistency": ("cmd_kg_consistency", "code"),
        "normalize-endpoints": ("cmd_kg_normalize_endpoints", "code"),
        "build-graph": ("cmd_kg_build_graph", "code"),
        "export-dataview": ("cmd_kg_export_dataview", "code"),
    },
    "capsule": ("cmd_capsule", None),
    "policy": ("cmd_policy", "code"),
    "version": ("cmd_version", None),
    "genos": ("cmd_genos", "code"),
    "shadow": ("cmd_shadow", None),
    "stress": ("cmd_stress", None),
    "dispute": ("cmd_dispute", "code"),
    "blindspot": ("cmd_blindspot", "code"),
    "persona": ("cmd_persona", "code"),
    "freshness": ("cmd_freshness", "code"),
    "entropy": ("cmd_entropy", "code"),
    "immune": ("cmd_immune", "code"),
    "reminder": ("cmd_reminder", "code"),
    "recap": ("cmd_recap", "code"),
    "signals": ("cmd_signals", "code"),
    "verify": ("cmd_verify", "code"),
    "raw-index": ("cmd_raw_index", "code"),
    "migrate": ("cmd_migrate", "code"),
    "backup": ("cmd_backup", "code"),
    "restore": ("cmd_restore", "code"),
    "data": ("cmd_data", "code"),
    "db": {"maintenance": ("cmd_db_maintenance", "code")},
}


def _dispatch_route(cmd_name: str, exit_mode: str | None, args) -> None:
    """执行简单命令并根据 exit_mode 处理退出码。"""
    result = _call_command(cmd_name, args)
    if exit_mode == "bool":
        sys.exit(0 if result else 1)
    elif exit_mode == "code":
        sys.exit(result)


def _dispatch_subcommand(command: str, args, parser) -> None:
    """处理需要子命令的命令。"""
    spec = _SUBCOMMAND_ROUTES[command]
    if isinstance(spec, dict):
        subcmd = getattr(args, f"{command.replace('-', '_')}_cmd", "")
        route = spec.get(subcmd)
        if route is None:
            (_get_subparser(parser, command) or parser).print_help()
            return
        cmd_name, exit_mode = route
    else:
        subcmd_attr = f"{command.replace('-', '_')}_cmd"
        if not getattr(args, subcmd_attr, ""):
            if command in {"freshness", "entropy", "immune", "reminder", "signals"}:
                parser.print_help()
            else:
                (_get_subparser(parser, command) or parser).print_help()
            return
        cmd_name, exit_mode = spec
    _dispatch_route(cmd_name, exit_mode, args)


def main():
    _configure_environment()
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "health":
        _handle_health(args)
        return

    if args.command == "mcp" and getattr(args, "mcp_cmd", "") == "serve":
        _call_command("cmd_mcp_serve", args)
        return

    if args.command == "metrics" and getattr(args, "metrics_cmd", "") == "scan":
        _call_command("cmd_metrics_scan", args)
        return

    if args.command == "metrics" and getattr(args, "metrics_cmd", "") == "assess":
        _call_command("cmd_metrics_assess", args)
        return

    route = _COMMAND_ROUTES.get(args.command)
    if route:
        cmd_name, exit_mode = route
        _dispatch_route(cmd_name, exit_mode, args)
        return

    if args.command in _SUBCOMMAND_ROUTES:
        _dispatch_subcommand(args.command, args, parser)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
