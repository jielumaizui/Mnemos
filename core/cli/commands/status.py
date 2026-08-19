"""Status command for Mnemos CLI."""

import logging
import sqlite3
import time
from pathlib import Path

from core.cli.helpers import (
    _get_cognitive_graph_stats,
    _print_runtime_health,
    _print_today_summary,
)
from core.cli.commands.sync import _get_backfill_status

# Constants extracted from magic numbers
STATS_DAYS = 30

logger = logging.getLogger(__name__)


def _print_status_header(config) -> None:
    """打印状态头部与核心开关。"""
    print("Mnemos 状态")
    print("=" * 40)
    print(f"配置文件:      {config.config_path}")
    print(f"认知 Vault:    {config.vault_dir('mnemos')}")
    print(f"Raw Vault:     {config.vault_dir('raw')}")
    print("存储后端:      Obsidian/raw vault")
    print(f"跨层认知图:    {'✓' if config.cognitive_graph_enabled else '✗'}")
    print(f"画像系统:      {'✓' if config.persona_enabled else '✗'}")
    print(f"Claude Code:   {'✓' if config.claude_code_enabled else '✗'}")
    print(f"MCP 配置:      {'✓' if config.mcp_enabled else '✗'}")


def _connect_read_only(path: Path):
    return sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro",
        uri=True,
        timeout=5,
    )


def _print_mcp_server_status(config) -> None:
    """打印静态 MCP 配置状态，不实例化服务或存储。"""
    try:
        from integrations.agora_tools.schema import list_tools

        schemas = list_tools(lambda _name: "core")
        enabled = bool(config.mcp_enabled)
        print(
            f"MCP Server:    {'✓' if enabled else '✗'} 已声明 "
            f"({len(schemas.get('tools', []))} tools)"
        )
    except (ImportError, AttributeError, OSError) as e:
        print(f"MCP Server:    ✗ schema 不可读 ({e})")


def _print_daemon_services(config) -> None:
    """打印 daemon 服务开关状态。"""
    services = config.get("daemon.services")
    if services:
        enabled = [k for k, v in services.items() if v]
        disabled = [k for k, v in services.items() if not v]
        print(f"daemon 服务:   开 {len(enabled)} / 关 {len(disabled)}")


def _print_wiki_stats(config) -> None:
    """打印 Wiki 与 KG 基本统计。"""
    wiki_dir = config.vault_dir("mnemos")
    if wiki_dir.exists():
        md_count = len(list(wiki_dir.rglob("*.md")))
        print(f"Wiki 页面数:   {md_count}")
    print(f"KG 数据库:     {config.database_dir / 'knowledge_graph.db'}")
    print(f"向量索引目录:  {config.database_dir / 'embedding_index'}")

    cg_stats = _get_cognitive_graph_stats(config)
    if cg_stats:
        print(f"认知图关系:    {cg_stats.get('relations', 0)}")
        print(f"认知图节点:    {cg_stats.get('canonical_nodes', 0)}")


def _print_persona_signal_stats(config) -> None:
    """打印最近 30 天信号统计。"""
    try:
        db_path = Path(config.database_dir) / "user_signals.db"
        if not db_path.is_file():
            print("画像数据库:    未初始化")
            return
        table_names = (
            "session_signals",
            "knowledge_signals",
            "git_signals",
            "file_system_signals",
            "note_signals",
            "wechat_signals",
        )
        total = 0
        with _connect_read_only(db_path) as conn:
            existing = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            for table in table_names:
                if table not in existing:
                    continue
                row = conn.execute(
                    f"SELECT COUNT(*) FROM {table} "  # nosec B608 - fixed allowlist
                    "WHERE timestamp >= date('now', ?)",
                    (f"-{STATS_DAYS} days",),
                ).fetchone()
                total += int(row[0]) if row else 0
        print(f"最近30天信号: {total}")
    except (
        ImportError,
        AttributeError,
        OSError,
        TypeError,
        ValueError,
        RuntimeError,
        sqlite3.Error,
    ):
        logger.debug("状态页画像统计失败", exc_info=True)
        print("画像数据库:    未初始化")


def _print_resource_budget_status() -> None:
    """打印资源预算状态。"""
    try:
        from core.resource_budget import get_budget

        budget = get_budget()
        rstatus = budget.status()
        state_map = {
            "normal": "正常",
            "slowed": "降速",
            "throttled": "节流",
            "battery": "电池供电",
        }
        state_label = state_map.get(rstatus["state"], rstatus["state"])
        print(
            f"资源状态:      {state_label} (CPU {rstatus['cpu']}, 内存 {rstatus['memory']}, 温度 {rstatus['thermal']})"  # noqa: E501
        )
    except (ImportError, AttributeError, OSError):
        logger.debug("状态页资源预算显示失败", exc_info=True)


def _print_distillation_pause_status() -> None:
    """打印蒸馏暂停状态与恢复诊断。"""
    try:
        from core.hephaestus.distillation_pause import get_pause_status

        status = get_pause_status()
    except (ImportError, AttributeError, OSError, ValueError):
        logger.debug("状态页蒸馏暂停状态显示失败", exc_info=True)
        return

    paused = bool(status.get("paused"))
    print(f"蒸馏暂停:      {'是' if paused else '否'}")
    if not paused:
        return

    details = [
        ("原因", status.get("reason")),
        ("暂停时间", status.get("paused_at")),
        ("恢复时间", status.get("resume_at")),
        ("API 链", status.get("api_chain_desc")),
        ("最后错误", status.get("last_error")),
    ]
    for label, value in details:
        if value:
            print(f"  {label}: {value}")


def _print_adaptive_config_metrics(config) -> None:
    """打印自适应配置最近指标。"""
    try:
        from core.kia.adaptive_config import AdaptiveConfig
        from core.kia.policy import EffectivePolicy

        db_path = Path(config.database_dir) / "adaptive_config.db"
        adaptive = AdaptiveConfig(
            db_path=db_path,
            policy=EffectivePolicy(
                db_path=db_path,
                config=config,
                initialize=False,
            ),
            initialize=False,
        )
        summary = adaptive.get_metrics_summary()
    except (
        ImportError,
        AttributeError,
        OSError,
        TypeError,
        ValueError,
        RuntimeError,
        sqlite3.Error,
    ):
        logger.debug("状态页自适应配置指标显示失败", exc_info=True)
        return

    policy_summary = {}
    get_policy_summary = getattr(adaptive, "get_policy_summary", None)
    if callable(get_policy_summary):
        try:
            policy_summary = get_policy_summary()
        except (OSError, TypeError, ValueError, RuntimeError, sqlite3.Error):
            logger.debug("状态页自适应策略覆盖显示失败", exc_info=True)

    if policy_summary:
        print(
            "自适应策略覆盖: "
            f"{policy_summary.get('coverage_count', 0)} "
            f"rules={policy_summary.get('rule_count', 0)} "
            f"domains={len(policy_summary.get('domains', []))} "
            f"active_shadow={policy_summary.get('active_shadow_count', 0)} "
            f"overdue={policy_summary.get('overdue_shadow_count', 0)}"
        )
        if policy_summary.get("coverage_errors"):
            print(f"  coverage_errors={len(policy_summary.get('coverage_errors', []))}")
        for shadow in policy_summary.get("active_shadows", [])[:5]:
            print(
                "  shadow "
                f"{shadow.get('experiment_id')}: {shadow.get('config_key')} "
                f"{shadow.get('old_value')} -> {shadow.get('new_value')} "
                f"metric_before={shadow.get('metric_before')} "
                f"age_hours={shadow.get('age_hours')}"
            )

    if not summary:
        return

    print("自适应配置指标:")
    for feature in sorted(summary):
        metrics = summary.get(feature, {})
        if not isinstance(metrics, dict):
            continue
        for metric_name in sorted(metrics):
            metric = metrics.get(metric_name, {})
            if not isinstance(metric, dict):
                continue
            print(
                f"  {feature}.{metric_name}: "
                f"ewma={metric.get('ewma', '-')}; "
                f"trend={metric.get('trend', '-')}; "
                f"last={metric.get('last_value', '-')}; "
                f"samples={metric.get('sample_count', 0)}"
            )


def _print_sync_audit(config) -> None:
    """打印最近 24 小时扫描审计。"""
    try:
        db_path = Path(config.database_dir) / "sync_log.db"
        if not db_path.is_file():
            return
        cutoff = time.time() - 24 * 60 * 60
        with _connect_read_only(db_path) as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sync_audit'"
            ).fetchone()
            if not table:
                return
            rows = conn.execute(
                "SELECT source, SUM(skipped_missing), SUM(skipped_large), "
                "SUM(skipped_stale), SUM(skipped_unchanged), "
                "SUM(skipped_over_limit), SUM(selected), SUM(synced_turns) "
                "FROM sync_audit WHERE created_at >= ? GROUP BY source",
                (cutoff,),
            ).fetchall()
        audit = {
            str(row[0]): {
                "skipped_missing": int(row[1] or 0),
                "skipped_large": int(row[2] or 0),
                "skipped_stale": int(row[3] or 0),
                "skipped_unchanged": int(row[4] or 0),
                "skipped_over_limit": int(row[5] or 0),
                "selected": int(row[6] or 0),
                "synced_turns": int(row[7] or 0),
            }
            for row in rows
        }
        if not audit:
            return
        print("最近 24 小时扫描审计:")
        for source, stats in audit.items():
            total_skipped = sum(v for k, v in stats.items() if k.startswith("skipped_"))
            skipped_detail = ", ".join(
                f"{k[8:]}={v}" for k, v in stats.items() if k.startswith("skipped_") and v
            )
            detail_str = f" ({skipped_detail})" if skipped_detail else ""
            print(
                f"  {source}: selected={stats.get('selected', 0)}, synced_turns={stats.get('synced_turns', 0)}, skipped={total_skipped}{detail_str}"  # noqa: E501
            )
        print()
    except (OSError, TypeError, ValueError, RuntimeError, sqlite3.Error):
        logger.debug("状态页扫描审计显示失败", exc_info=True)


def _print_backfill_progress(config) -> None:
    """打印历史回填进度。"""
    try:
        bf_status = _get_backfill_status(config)
        if bf_status:
            bf_state = bf_status.get("status", "unknown")
            state_label = {
                "running": "运行中",
                "done": "已完成",
                "partial": "范围受限（未声明完成）",
                "dry_run": "演练完成（未写入）",
                "failed": "失败",
            }.get(bf_state, bf_state)
            print(f"历史回填: {state_label}")
            if bf_status.get("stats"):
                s = bf_status["stats"]
                print(
                    f"  agents={s.get('agents', '?')}, sessions={s.get('sessions', '?')}, turns={s.get('turns', '?')}"  # noqa: E501
                )
            print()
        else:
            print("历史回填: 未运行（建议首次部署后执行 `mnemos sync backfill --since 0`）")
            print()
    except (OSError, ValueError):
        logger.debug("状态页回填进度显示失败", exc_info=True)


def cmd_status(args):
    """查看系统状态"""
    from core.config import Config
    from core.ops.config_scope import use_config

    config = Config(provision=False)

    with use_config(config):
        _print_status_header(config)
        _print_mcp_server_status(config)
        _print_daemon_services(config)
        print()

        # [C] 今日动态摘要 — 用户可见状态面板
        _print_today_summary(config)
        print()

        _print_wiki_stats(config)
        _print_persona_signal_stats(config)
        print()

        _print_resource_budget_status()
        _print_distillation_pause_status()
        _print_adaptive_config_metrics(config)
        _print_sync_audit(config)
        _print_backfill_progress(config)

        _print_runtime_health(config)
