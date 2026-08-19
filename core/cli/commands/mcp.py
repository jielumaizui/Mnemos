"""MCP command helpers for Mnemos CLI."""

import logging
import sys
from typing import Dict, Optional

from core.agent_kit.source_support_manifest import get_agent_source_support_manifest

logger = logging.getLogger(__name__)


def cmd_mcp_serve(args):
    """启动 MCP 服务器"""
    # MCP stdio 协议要求 stdout 只能输出 JSON，所有日志/提示走 stderr
    print("启动 MCP 服务器 (stdin/stdout 模式)...", file=sys.stderr)
    print("按 Ctrl+C 停止", file=sys.stderr)

    try:
        from integrations.agora import run_mcp_server

        run_mcp_server()
    except ImportError:
        print("MCP 服务集成未安装或未就绪，请先安装 mnemos[mcp] 依赖。", file=sys.stderr)
        sys.exit(1)


def _install_mcp_only_agent(agent_name: str) -> bool:
    """为无 Adapter 的 Agent 安装 MCP server 与 Active Policy。"""
    from pathlib import Path
    from core.agent_kit.source_support_manifest import AgentSourceSupportManifestError
    from core.agent_kit.source_support_manifest import get_agent_source_support_manifest
    from integrations import active

    try:
        spec = get_agent_source_support_manifest().require_host_agent(agent_name)
    except AgentSourceSupportManifestError:
        return False
    if spec.active_entrypoint != "mcp_only":
        return False
    agent = spec.name
    try:
        if agent == "codex":
            mcp_ok = active.upsert_codex_mcp_server(Path.home() / ".codex" / "config.toml")
            policy_ok = active.install_agent_policy("codex")
        elif agent == "hermes":
            mcp_ok = active.upsert_yaml_mcp_server(
                Path.home() / ".hermes" / "config.yaml",
                top_key="mcp_servers",
                agent="hermes",
            )
            policy_ok = active.install_agent_policy("hermes")
        elif agent == "kiro":
            mcp_ok = active.upsert_kiro_mcp_server(active.kiro_mcp_config_path())
            policy_ok = active.install_agent_policy("kiro")
        elif agent == "openclaw":
            mcp_ok = active.upsert_openclaw_mcp_server(Path.home() / ".openclaw" / "openclaw.json")
            policy_ok = active.install_agent_policy("openclaw")
        elif agent == "opencode":
            cfg_path = Path.home() / ".config" / "opencode" / "opencode.json"
            mcp_ok = active.upsert_opencode_config(cfg_path, include_mcp=True, include_policy=True)
            policy_ok = active.opencode_policy_configured(cfg_path)
        else:
            return False

        print(f"  {'✓' if mcp_ok else '✗'} MCP 主动工具")
        print(f"  {'✓' if policy_ok else '✗'} 主动使用策略")
        return mcp_ok and policy_ok
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError) as e:
        print(f"  ✗ {agent_name}: {e}")
        return False


def _mcp_only_agent_status(agent_name: str) -> Optional[Dict[str, bool]]:
    """返回无 Adapter Agent 的 MCP / Policy 安装状态。"""
    from pathlib import Path
    from core.agent_kit.source_support_manifest import AgentSourceSupportManifestError
    from core.agent_kit.source_support_manifest import get_agent_source_support_manifest
    from integrations import active

    try:
        spec = get_agent_source_support_manifest().require_host_agent(agent_name)
    except AgentSourceSupportManifestError:
        return None
    if spec.active_entrypoint != "mcp_only":
        return None
    agent = spec.name
    try:
        if agent == "codex":
            return {
                "mcp": active.codex_mcp_configured(Path.home() / ".codex" / "config.toml"),
                "policy": active.marked_block_installed(Path.home() / ".codex" / "AGENTS.md"),
            }
        if agent == "hermes":
            return {
                "mcp": active.yaml_mcp_configured(
                    Path.home() / ".hermes" / "config.yaml", top_key="mcp_servers"
                ),
                "policy": active.marked_block_installed(
                    Path.home() / ".hermes" / "MNEMOS_ACTIVE.md"
                ),
            }
        if agent == "kiro":
            return {
                "mcp": active.kiro_mcp_configured(active.kiro_mcp_config_path()),
                "policy": active.marked_block_installed(
                    Path.home() / ".kiro" / "MNEMOS_ACTIVE.md"
                ),
            }
        if agent == "openclaw":
            return {
                "mcp": active.openclaw_mcp_configured(Path.home() / ".openclaw" / "openclaw.json"),
                "policy": active.marked_block_installed(
                    Path.home() / ".openclaw" / "MNEMOS_ACTIVE.md"
                ),
            }
        if agent == "opencode":
            cfg_path = Path.home() / ".config" / "opencode" / "opencode.json"
            return {
                "mcp": active.opencode_mcp_configured(cfg_path),
                "policy": active.opencode_policy_configured(cfg_path),
            }
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
        logger.debug("MCP-only agent %s 状态检查失败", agent_name, exc_info=True)
    return None


_MCP_ONLY_AGENTS = frozenset(get_agent_source_support_manifest().mcp_only_host_names)
