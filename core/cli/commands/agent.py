"""Agent command for Mnemos CLI."""

import json
import logging
import os
import shlex
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

from core.cli.helpers import _get_config
from core.cli.commands.mcp import (
    _MCP_ONLY_AGENTS,
    _install_mcp_only_agent,
    _mcp_only_agent_status,
)

# Constants extracted from magic numbers
DURATION_BUCKET_WEEK_DAYS = 7

logger = logging.getLogger(__name__)


def add_agent_mcp_grant_parser(agent_sub: Any) -> None:
    """Register the explicit MCP principal-grant CLI contract."""
    parser = agent_sub.add_parser(
        "grant-mcp",
        help="显式配置服务端 MCP principal grant",
    )
    parser.add_argument("agent_name", help="宿主 Agent 名称")
    parser.add_argument(
        "--capability",
        action="append",
        default=[],
        help="授权 policy capability，可重复",
    )
    parser.add_argument(
        "--all-tools",
        action="store_true",
        help="授权当前 51 个 MCP tools 对应的全部 policy capability",
    )
    parser.add_argument(
        "--project",
        action="append",
        default=[],
        help="允许的 project，可重复",
    )
    parser.add_argument(
        "--all-projects",
        action="store_true",
        help="显式允许所有 project（写入 * grant）",
    )
    parser.add_argument(
        "--source-agent",
        action="append",
        default=[],
        help="允许跨 Agent 读取的来源 Agent，可重复",
    )
    parser.add_argument(
        "--revoke",
        action="store_true",
        help="撤销该 Agent 的 MCP principal grant",
    )
    parser.add_argument("--db-path", default="", help="覆盖授权数据库路径")
    parser.add_argument("--json", action="store_true", help="输出 JSON")


def _resolve_target_agents(args: Any, AgentRegistry: Any) -> tuple[List[Any], str]:
    """根据 agent_name 解析要操作的 Agent 列表。

    Returns:
        (agents, target) 元组；agents 可能为空。
    """
    target = getattr(args, "agent_name", "").lower() if hasattr(args, "agent_name") else ""
    if target:
        agent = AgentRegistry.get_adapter(target, include_unavailable=True)
        return ([agent], target) if agent else ([], target)
    return AgentRegistry.discover_all(), ""


def _cmd_agent_list(args: Any) -> None:  # noqa: U100
    """列出所有已发现的 Agent。"""
    from integrations.olympus import AgentRegistry

    print("=" * 60)
    print("AI Agent 状态")
    print("=" * 60)
    agents = AgentRegistry.discover_all()
    if not agents:
        print("未检测到任何 Agent")
        return
    for agent in agents:
        mark = "★" if agent.name == os.environ.get("MNEMOS_HOST_AGENT", "").lower() else " "
        print(f"  [{mark}] {agent.name:12s} 优先级={agent.priority}")
    print()
    print(f"共 {len(agents)} 个 Agent 可用")


def _print_host_agent_status() -> None:
    """打印宿主 Agent 环境变量与最佳可用 Agent。"""
    from integrations.olympus import AgentRegistry

    host = os.environ.get("MNEMOS_HOST_AGENT", "")
    if host:
        print(f"宿主 Agent (MNEMOS_HOST_AGENT): {host}")
    else:
        print("未设置 MNEMOS_HOST_AGENT，将按优先级自动选择")
    best = AgentRegistry.select_best_agent()
    if best:
        print(f"最佳可用 Agent: {best.name}")
    else:
        print("未检测到任何 Agent")


def _storage_status_label(storage: Any) -> str:
    if storage.configured and storage.reachable:
        return "已连接且可连通"
    if storage.configured:
        return "已配置但不可达"
    return "未配置"


def _wiki_status_label(wiki: Any) -> str:
    if wiki.exists and wiki.writable:
        return "就绪"
    if wiki.exists:
        return "存在但不可写"
    return "未就绪"


def _format_agent_status(agent: Any) -> str:
    """返回单个 Agent 的状态字符串。"""
    hook_mark = "[hooks]" if agent.hooks_installed else ""
    mcp_mark = "[mcp]" if agent.mcp_configured else ""
    policy_mark = "[policy]" if agent.policy_installed else ""
    active_mark = "[active]" if agent.active_ready else ""
    passive_mark = "[passive]" if agent.passive_source_available else ""
    if agent.available:
        status = "已发现"
        mark = "✓"
    elif agent.passive_source_available:
        status = "被动数据源可用"
        mark = "⚠"
    else:
        status = "未发现"
        mark = "✗"
    data_dir_suffix = f" ({agent.data_dir})" if agent.data_dir else ""
    return (
        f"  {mark} {agent.name}: {status} "
        f"{hook_mark}{mcp_mark}{policy_mark}{active_mark}{passive_mark}{data_dir_suffix}"
    )


def _print_pending_tasks(tasks: List[Any]) -> None:
    """打印待办连接任务。"""
    pending = [t for t in tasks if not t.completed]
    if not pending:
        print("\n✓ 所有核心连接已就绪")
        return
    print("\n待办连接任务:")
    priority_marker = {"high": "🔴", "medium": "🟡", "low": "🟢"}
    for i, t in enumerate(pending, 1):
        marker = priority_marker.get(t.priority, "⚪")
        print(f"  {marker} [{t.priority.upper()}] {t.task}")
        print(f"      → {t.action}")
    print("\n提示: 宿主 Agent 可以通过 MCP 调用 self_diagnose() 获取完整诊断报告")


def _cmd_agent_detect(args: Any) -> None:  # noqa: U100
    """检测宿主 Agent 与核心连接状态。"""
    from core.diagnostics import ConnectionDiagnostics

    _print_host_agent_status()

    print("\n" + "=" * 60)
    print("连接状态检测")
    print("=" * 60)

    storage = ConnectionDiagnostics.check_storage()
    wiki = ConnectionDiagnostics.check_wiki()
    agents = ConnectionDiagnostics.check_agents()

    storage_status = _storage_status_label(storage)
    print(
        f"  {'✓' if (storage.configured and storage.reachable) else '✗'} Raw Vault: {storage_status}"
    )

    wiki_status = _wiki_status_label(wiki)
    print(
        f"  {'✓' if (wiki.exists and wiki.writable) else '✗'} Wiki: {wiki_status} ({wiki.path})"
    )

    for agent in agents:
        print(_format_agent_status(agent))

    tasks = ConnectionDiagnostics.generate_task_list(wiki=wiki, agents=agents, storage=storage)
    _print_pending_tasks(tasks)


def _cmd_agent_install(args: Any) -> bool:
    """为 Agent 安装主动接入组件。"""
    from integrations.olympus import AgentRegistry

    print("=" * 60)
    print("安装 Agent 主动接入")
    print("=" * 60)
    agents, target = _resolve_target_agents(args, AgentRegistry)
    if not agents:
        if target in _MCP_ONLY_AGENTS:
            print(f"\n安装 {target} ...")
            ok = _install_mcp_only_agent(target)
            print(f"  {'✓' if ok else '✗'} MCP-only install")
            return ok
        if target:
            print(f"  ✗ 未找到 Agent 适配器: {target}")
            return False

    ok_all = True
    if not agents and not target:
        print("  未检测到 Adapter Agent，继续安装 MCP-only 主动接入")

    if not target:
        for name in sorted(_MCP_ONLY_AGENTS):
            print(f"\n安装 {name} ...")
            ok = _install_mcp_only_agent(name)
            print(f"  {'✓' if ok else '✗'} MCP-only install")
            ok_all = ok_all and ok
        if not agents:
            return ok_all

    if not agents:
        return False

    for agent in agents:
        print(f"\n安装 {agent.name} ...")
        try:
            hooks_ok = agent.install_hooks()
            mcp_ok = agent.install_mcp_server()
            policy_ok = agent.install_active_policy()
            print(f"  {'✓' if hooks_ok else '✗'} hooks/wrapper")
            print(f"  {'✓' if mcp_ok else '✗'} MCP 主动工具")
            print(f"  {'✓' if policy_ok else '✗'} 主动使用策略")
            print(f"  {'✓' if (hooks_ok and mcp_ok and policy_ok) else '✗'} active ready")
            ok_all = ok_all and hooks_ok and mcp_ok and policy_ok
        except (OSError, ValueError, AttributeError) as e:
            print(f"  ✗ {agent.name}: {e}")
            ok_all = False
    return ok_all


def _cmd_agent_repair(args: Any) -> bool:
    """修复静态接入缺口；运行验收必须由宿主完成授权安全探针。"""
    from core.agent_kit import build_agent_kit_report, normalize_agent_name

    target = normalize_agent_name(getattr(args, "agent_name", ""))
    targets = [target] if target else None

    print("=" * 60)
    print("Agent 主动接入修复")
    print("=" * 60)

    before = build_agent_kit_report(targets)
    repair_names = (
        ([target] if target in before.nonconformant_agents else [])
        if target
        else list(before.nonconformant_agents)
    )

    if not repair_names:
        if (
            before.selected_target_full_power_ok
            if target
            else before.full_power_ok
        ):
            print("✓ Agent Kit 已满血，无需修复")
            if before.full_power_agents:
                print("满血 Agent: " + ", ".join(before.full_power_agents))
            return True
        runtime_unverified = (
            before.selected_runtime_unverified_agents
            if target
            else before.runtime_unverified_agents
        )
        if before.conformance_ok and runtime_unverified:
            print("✓ 静态接入已合规，无需重装")
            print(
                "✗ 运行能力尚未验收；请先完成用户授权，再由宿主依次调用 "
                "health_check 与 agent_runtime_probe，并在完整 raw_sync 分母后运行 "
                "scripts/attest_agent_source_capture.py --agent <agent> --apply"
            )
            return False
        print("✗ Agent Kit workflow 契约缺失，当前 repair 无法自动补齐 MCP 工具注册")
        if before.missing_workflow_tools:
            print("缺失 MCP 工具: " + ", ".join(before.missing_workflow_tools))
        return False

    ok_all = True
    for name in repair_names:
        print(f"\n--- 修复 {name} ---")
        ok_all = _cmd_agent_install(SimpleNamespace(agent_name=name)) and ok_all

    after = build_agent_kit_report(targets)
    print("\n" + "=" * 60)
    print("修复后 Agent Kit 满血验收")
    print("=" * 60)
    print("修复后满血 Agent: " + (", ".join(after.full_power_agents) or "无"))
    print("修复后静态合规 Agent: " + (", ".join(after.conformant_agents) or "无"))
    if after.degraded_agents:
        print("仍未满血 Agent: " + ", ".join(after.degraded_agents))

    repaired = set(repair_names)
    for agent in after.agents:
        if agent.name not in repaired or agent.full_power:
            continue
        for gap in agent.full_power_gaps:
            print(f"  {agent.name} gap: {gap}")
        for gap in agent.runtime_gaps:
            print(f"  {agent.name} runtime: {gap}")
        for action in agent.repair_actions:
            print(f"  {agent.name} next: {action}")

    return ok_all and (
        after.selected_target_full_power_ok
        if target
        else after.full_power_ok
    )


def _diagnose_mcp_only(target: str) -> bool:
    """对仅支持 MCP 的 Agent 输出简化诊断。"""
    status = _mcp_only_agent_status(target)
    if status is None:
        print(f"✗ 无法获取 {target} 状态")
        return False
    mcp_ok = status["mcp"]
    policy_ok = status["policy"]
    mark = "✓" if (mcp_ok and policy_ok) else "⚠"
    print(
        f"  {mark} {target}: mcp{'✓' if mcp_ok else '✗'}, policy{'✓' if policy_ok else '✗'} [mcp-only]"
    )
    return True


def _check_agent_availability(agent: Any) -> None:
    try:
        avail = agent.is_available()
        print(f"  {'✓' if avail else '✗'} 可用性: {'可用' if avail else '不可用'}")
    except (OSError, ValueError, AttributeError) as e:
        print(f"  ✗ 可用性检查失败: {e}")


def _check_agent_hooks(agent: Any) -> None:
    try:
        hooks_ok = agent.is_hooks_installed()
        print(f"  {'✓' if hooks_ok else '✗'} Hooks: {'已安装' if hooks_ok else '未安装'}")
    except (OSError, ValueError, AttributeError) as e:
        print(f"  ✗ Hooks 检查失败: {e}")


def _check_agent_mcp(agent: Any) -> None:
    try:
        mcp_ok = agent.is_mcp_configured()
        print(f"  {'✓' if mcp_ok else '✗'} MCP 主动工具: {'已配置' if mcp_ok else '未配置'}")
    except (OSError, ValueError, AttributeError) as e:
        print(f"  ✗ MCP 检查失败: {e}")


def _check_agent_policy(agent: Any) -> None:
    try:
        policy_ok = agent.is_active_policy_installed()
        print(
            f"  {'✓' if policy_ok else '✗'} 主动使用策略: {'已安装' if policy_ok else '未安装'}"
        )
    except (OSError, ValueError, AttributeError) as e:
        print(f"  ✗ 主动使用策略检查失败: {e}")


def _check_agent_active_connection(agent: Any) -> None:
    try:
        active_ok = agent.is_active_connection_installed()
        print(f"  {'✓' if active_ok else '✗'} 主动接入: {'就绪' if active_ok else '未就绪'}")
    except (OSError, ValueError, AttributeError) as e:
        print(f"  ✗ 主动接入检查失败: {e}")


def _check_event_dir_writable(event_dir: Any) -> None:
    try:
        event_dir.mkdir(parents=True, exist_ok=True)
        test_file = event_dir / ".write_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
        print(f"  ✓ 事件目录可读写: {event_dir}")
    except (OSError, ValueError) as e:
        print(f"  ✗ 事件目录不可写: {e}")


def _check_worker_queue() -> None:
    from core.hephaestus_worker import HephaestusWorker

    try:
        worker = HephaestusWorker()
        stats = worker.get_stats()
        print(f"  ✓ 蒸馏队列: {stats['pending']} 待处理")
    except (OSError, ValueError, AttributeError) as e:
        print(f"  ✗ 蒸馏队列检查失败: {e}")


def _check_agent_signals(agent: Any) -> None:
    try:
        signals = agent.collect_signals(days=DURATION_BUCKET_WEEK_DAYS)
        print(f"  ✓ 信号采集: 最近7天 {len(signals)} 条")
    except (OSError, ValueError, AttributeError) as e:
        print(f"  ✗ 信号采集失败: {e}")


def _diagnose_single_agent(agent: Any, event_dir: Any) -> None:
    """输出单个 Agent 的完整诊断。"""
    print(f"\n--- {agent.name} ---")
    _check_agent_availability(agent)
    _check_agent_hooks(agent)
    _check_agent_mcp(agent)
    _check_agent_policy(agent)
    _check_agent_active_connection(agent)
    _check_event_dir_writable(event_dir)
    _check_worker_queue()
    _check_agent_signals(agent)


def _cmd_agent_doctor(args: Any) -> bool:
    """对目标 Agent 做完整诊断。"""
    from integrations.olympus import AgentRegistry

    print("=" * 60)
    print("Agent 诊断")
    print("=" * 60)
    agents, target = _resolve_target_agents(args, AgentRegistry)
    if not agents:
        if target in _MCP_ONLY_AGENTS:
            return _diagnose_mcp_only(target)
        print("✗ 未注册任何 Agent 适配器")
        return False

    checked = 0
    event_dir = _get_config().database_dir / "events"
    for agent in agents:
        if target and agent.name != target:
            continue
        checked += 1
        _diagnose_single_agent(agent, event_dir)

    if target and checked == 0:
        print(f"✗ 未找到 Agent: {target}")
        return False
    print(f"\n{'=' * 60}\n诊断完成: {checked} 个 Agent")
    return True


def _cmd_agent_kit(args: Any) -> bool:
    """检查 Mnemos Agent Kit 统一协议和目标 Agent 接入状态。"""
    from core.agent_kit import build_agent_kit_report, normalize_agent_name

    target = normalize_agent_name(getattr(args, "agent_name", ""))
    targets = [target] if target else None
    no_probe = getattr(args, "no_probe", False)
    report = build_agent_kit_report(
        targets,
        probe_filesystem=not no_probe,
        load_default_providers=not no_probe,
    )
    selected_ok = (
        report.selected_target_full_power_ok
        if target
        else report.full_power_ok
    )
    if getattr(args, "json", False):
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return selected_ok

    print("=" * 60)
    print("Mnemos Agent Kit")
    print("=" * 60)
    print(f"协议版本: {report.protocol_version}")
    print(f"Workflow 契约: {'OK' if report.workflow_contract_ok else '缺失'}")
    print(f"静态合规: {'OK' if report.conformance_ok else '存在缺口'}")
    print(f"运行满血验收: {'OK' if selected_ok else '未通过'}")
    if report.missing_workflow_tools:
        print("缺失 MCP 工具: " + ", ".join(report.missing_workflow_tools))

    print("\n统一 workflow:")
    for workflow in report.workflows:
        mark = "✓" if workflow.exposed else "✗"
        print(f"  {mark} {workflow.name:14s} -> {workflow.mcp_tool} [{workflow.phase}]")

    print("\n目标 Agent:")
    for agent in report.agents:
        if agent.full_power:
            mark = "✓"
        elif agent.installed:
            mark = "⚠"
        else:
            mark = "☐"
        passive = "passive✓" if agent.passive_source_registered else "passive✗"
        detected = "detected✓" if agent.passive_source_detected else "detected✗"
        active = "active✓" if agent.active_ready else "active✗"
        adapter = "adapter✓" if agent.active_adapter_registered else "adapter✗"
        print(
            f"  {mark} {agent.name:10s} status={agent.status:13s} entry={agent.active_entrypoint:8s} "
            f"{adapter} {active} {passive} {detected}"
        )
        if not agent.installed:
            continue
        if agent.install_evidence:
            print(f"      install_evidence: {agent.install_evidence}")
        if agent.data_dir:
            print(f"      data_dir: {agent.data_dir}")
        if agent.source_capabilities:
            fidelity = agent.source_capabilities.get("source_fidelity", "unknown")
            tools = "tools✓" if agent.source_capabilities.get("tool_results") else "tools✗"
            reasoning = agent.source_capabilities.get("reasoning", False)
            print(f"      capture: fidelity={fidelity}, {tools}, reasoning={reasoning}")
        for gap in agent.gaps:
            print(f"      gap: {gap}")
        for gap in agent.full_power_gaps:
            print(f"      conformance_gap: {gap}")
        print(
            f"      runtime: state={agent.runtime_state}, "
            f"authorization={agent.authorization_state}, "
            f"receipt_at={agent.runtime_receipt_at or 'missing'}"
        )
        for gap in agent.runtime_gaps:
            print(f"      runtime_gap: {gap}")
        for action in agent.repair_actions:
            print(f"      repair: {action}")

    print(f"\n可用 Agent: {', '.join(report.ready_agents) if report.ready_agents else '无'}")
    print(
        "运行满血 Agent: "
        + (", ".join(report.full_power_agents) if report.full_power_agents else "无")
    )
    print(
        "静态合规 Agent: "
        + (", ".join(report.conformant_agents) if report.conformant_agents else "无")
    )
    if report.degraded_agents:
        print("运行未验收 Agent: " + ", ".join(report.degraded_agents))
    return selected_ok


def _shadow_store_from_args(args: Any):
    from core.agent_kit.shadow_eval import AgentShadowConfigStore

    db_path = Path(args.db_path).expanduser() if getattr(args, "db_path", "") else None
    return AgentShadowConfigStore(db_path)


def _cmd_agent_shadow(args: Any) -> bool:
    from core.agent_kit.shadow_eval import command_from_string

    store = _shadow_store_from_args(args)
    action = getattr(args, "shadow_cmd", "")
    if action == "status":
        payload = store.get().to_dict()
        if getattr(args, "json", False):
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            state = "enabled" if payload["enabled"] else "disabled"
            print(f"AgentBackend shadow: {state}")
            if payload["agent"]:
                print(f"agent: {payload['agent']}")
                print(f"directory: {payload['directory']}")
                print(f"command: {shlex.join(payload['command'])}")
        return True
    if action == "enable":
        command = command_from_string(args.agent_command)
        allowed_dirs = [Path(path).expanduser() for path in getattr(args, "allowed_dir", [])]
        config = store.enable(
            agent=args.agent_name,
            command=command,
            timeout_seconds=args.timeout,
            directory=args.directory,
            allowed_dirs=allowed_dirs,
        )
        if getattr(args, "json", False):
            print(json.dumps(config.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(f"AgentBackend shadow enabled for {config.agent}")
        return True
    if action == "disable":
        config = store.disable()
        if getattr(args, "json", False):
            print(json.dumps(config.to_dict(), ensure_ascii=False, indent=2))
        else:
            print("AgentBackend shadow disabled")
        return True
    print("可用子命令: status, enable, disable")
    return False


def _cmd_agent_mcp_grant(args: Any) -> bool:
    """Create or revoke an explicit server-side MCP principal grant."""
    from core.access_policy import MCP_TOOL_POLICIES
    from core.agent_kit.authorization import AgentAuthorizationStore

    agent = str(getattr(args, "agent_name", "") or "").strip().lower()
    if not agent:
        raise ValueError("agent_name is required")
    db_path = str(getattr(args, "db_path", "") or "").strip()
    store = AgentAuthorizationStore(Path(db_path).expanduser() if db_path else None)
    supported = set(MCP_TOOL_POLICIES.values())
    requested = {
        str(value).strip()
        for value in (getattr(args, "capability", []) or [])
        if str(value).strip()
    }
    unknown = requested - supported
    if unknown:
        raise ValueError(f"unsupported MCP capabilities: {sorted(unknown)}")
    if getattr(args, "all_tools", False):
        requested = supported
    if not requested and not getattr(args, "revoke", False):
        requested = {"public_metadata"}
    projects = {
        str(value).strip()
        for value in (getattr(args, "project", []) or [])
        if str(value).strip()
    }
    if getattr(args, "all_projects", False):
        projects.add("*")
    sources = {
        str(value).strip().lower()
        for value in (getattr(args, "source_agent", []) or [])
        if str(value).strip()
    }
    revoke = bool(getattr(args, "revoke", False))
    grant = store.set_mcp_principal_grant(
        agent,
        capabilities=requested,
        allowed_projects=projects,
        allowed_source_agents=sources,
        state="revoked" if revoke else "active",
    )
    revoked_launches = store.revoke_mcp_principal_grant(agent) if revoke else 0
    payload = {
        "success": True,
        "agent": grant.agent,
        "state": grant.state,
        "capabilities": sorted(grant.capabilities),
        "allowed_projects": sorted(grant.allowed_projects),
        "allowed_source_agents": sorted(grant.allowed_source_agents),
        "revoked_launches": revoked_launches,
        "next_action": (
            f"mnemos agent grant-mcp {grant.agent} --all-tools"
            if revoke
            else f"mnemos agent install {grant.agent}"
        ),
    }
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"MCP principal grant {grant.state}: {grant.agent}")
        print(f"Next: {payload['next_action']}")
    return True


def cmd_agent(args):
    """AI Agent 管理"""
    handlers = {
        "list": _cmd_agent_list,
        "detect": _cmd_agent_detect,
        "install": _cmd_agent_install,
        "repair": _cmd_agent_repair,
        "doctor": _cmd_agent_doctor,
        "kit": _cmd_agent_kit,
        "scan": _cmd_agent_kit,
        "grant-mcp": _cmd_agent_mcp_grant,
        "shadow": _cmd_agent_shadow,
    }
    handler = handlers.get(args.agent_cmd)
    if handler is None:
        print(
            "可用子命令: list, detect, install, repair, doctor, kit, scan, "
            "grant-mcp, shadow"
        )
        return None
    return handler(args)
