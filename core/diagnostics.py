"""
core/diagnostics.py — Mnemos 连接诊断引擎

提供统一的连接状态检测和任务清单生成，被 MCP 工具和 CLI 共享使用。
避免 agora.py 和 mnemos_cli.py 中的重复诊断逻辑。
"""

import os
import logging
from typing import Dict, List, Optional, Any
from pathlib import Path
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class MemosStatus:
    enabled: bool = False
    configured: bool = False
    api_url: Optional[str] = None
    reachable: Optional[bool] = None
    error: Optional[str] = None


@dataclass
class WikiStatus:
    path: str = ""
    exists: bool = False
    writable: bool = False


@dataclass
class AgentStatus:
    name: str = ""
    available: bool = False
    data_dir: Optional[str] = None
    hooks_installed: bool = False


@dataclass
class ConnectionTask:
    priority: str = ""  # "high" | "medium" | "low"
    task: str = ""
    action: str = ""
    completed: bool = False


class ConnectionDiagnostics:
    """Mnemos 连接诊断引擎

    统一检测 Memos、Wiki、Agent 的连接状态，生成任务清单。
    被 MCP self_diagnose / detect_sources 和 CLI cmd_agent detect 共享。
    """

    @classmethod
    def check_memos(cls, config=None) -> MemosStatus:
        """检查 Memos 连接状态"""
        if config is None:
            from core.config import get_config
            config = get_config()

        status = MemosStatus(
            enabled=config.memos_enabled,
            configured=bool(config.memos_token and config.memos_api_url),
            api_url=config.memos_api_url if config.memos_enabled else None,
        )

        if status.configured:
            try:
                from integrations.styx import MemosClient
                client = MemosClient(
                    token=config.memos_token,
                    base_url=config.memos_api_url,
                )
                # 使用 list_all_memos 做健康探测（兼容 REST 和 Connect API）
                client.list_all_memos(max_records=1)
                status.reachable = True
            except Exception as e:
                status.reachable = False
                status.error = str(e)

        return status

    @classmethod
    def check_wiki(cls, config=None) -> WikiStatus:
        """检查 Wiki/Obsidian 路径状态"""
        if config is None:
            from core.config import get_config
            config = get_config()

        wiki_dir = config.wiki_dir
        exists = wiki_dir.exists()
        return WikiStatus(
            path=str(wiki_dir),
            exists=exists,
            writable=exists and os.access(wiki_dir, os.W_OK),
        )

    @classmethod
    def check_agents(cls) -> List[AgentStatus]:
        """检查所有已发现 Agent 的状态（可用性 + hooks 安装状态）"""
        from integrations.olympus import AgentRegistry

        results = []
        try:
            adapters = AgentRegistry.discover_all()
        except Exception as e:
            logger.warning(f"Agent 发现失败: {e}")
            return results

        for adapter in adapters:
            try:
                data_dir = adapter.get_data_dir()
                results.append(AgentStatus(
                    name=adapter.name,
                    available=True,
                    data_dir=str(data_dir) if data_dir else None,
                    hooks_installed=adapter.is_hooks_installed(),
                ))
            except Exception as e:
                logger.debug(f"检查 Agent {adapter.name} 状态失败: {e}")
                results.append(AgentStatus(name=adapter.name, available=False))

        return results

    @classmethod
    def generate_task_list(
        cls,
        memos: MemosStatus = None,
        wiki: WikiStatus = None,
        agents: List[AgentStatus] = None,
    ) -> List[ConnectionTask]:
        """基于检测结果生成优先级排序的连接任务清单"""
        tasks = []

        if memos is None:
            memos = cls.check_memos()
        if wiki is None:
            wiki = cls.check_wiki()
        if agents is None:
            agents = cls.check_agents()

        # High priority: Memos
        if not memos.configured:
            tasks.append(ConnectionTask(
                priority="high",
                task="连接 Memos",
                action="询问用户 Memos 实例地址和 API Token，调用 configure_memos(api_url=..., token=...)",
                completed=False,
            ))
        else:
            tasks.append(ConnectionTask(
                priority="high",
                task="连接 Memos",
                action="已配置" + (" 且可连通" if memos.reachable else " 但连通性异常"),
                completed=True,
            ))

        # High priority: Wiki
        if not wiki.exists:
            tasks.append(ConnectionTask(
                priority="high",
                task="确认 Wiki 路径",
                action="询问用户 Obsidian Vault 路径，调用 configure_wiki(vault_path=...)",
                completed=False,
            ))
        elif not wiki.writable:
            tasks.append(ConnectionTask(
                priority="high",
                task="确认 Wiki 路径",
                action=f"路径存在但不可写: {wiki.path}",
                completed=False,
            ))
        else:
            tasks.append(ConnectionTask(
                priority="high",
                task="确认 Wiki 路径",
                action=f"已就绪: {wiki.path}",
                completed=True,
            ))

        # Medium priority: Agent hooks
        available_agents = [a for a in agents if a.available]
        if not available_agents:
            tasks.append(ConnectionTask(
                priority="medium",
                task="安装 Agent hooks",
                action="未检测到任何支持的 Agent，请确保 Claude Code / Kimi 等已安装",
                completed=False,
            ))
        else:
            for agent in available_agents:
                if not agent.hooks_installed:
                    tasks.append(ConnectionTask(
                        priority="medium",
                        task=f"安装 {agent.name} hooks",
                        action=f"调用 mnemos agent install {agent.name} 或 MCP install_hooks",
                        completed=False,
                    ))

        # Low priority: 额外 Agent 数据源（仅检测，不一定需要 hooks）
        from core.sync_framework.registry import PathDiscover
        extra_agents = ["aider", "gemini", "cursor", "windsurf"]
        for name in extra_agents:
            data_dir = PathDiscover.find(name)
            if data_dir:
                tasks.append(ConnectionTask(
                    priority="low",
                    task=f"发现 {name} 数据源",
                    action=f"路径: {data_dir}",
                    completed=True,
                ))

        # Sort: high -> medium -> low, incomplete first within same priority
        priority_order = {"high": 0, "medium": 1, "low": 2}
        tasks.sort(key=lambda t: (priority_order.get(t.priority, 99), t.completed))

        return tasks

    @classmethod
    def full_report(cls) -> Dict[str, Any]:
        """返回完整诊断报告（供 self_diagnose 使用）"""
        memos = cls.check_memos()
        wiki = cls.check_wiki()
        agents = cls.check_agents()
        tasks = cls.generate_task_list(memos, wiki, agents)

        missing = []
        if not memos.configured:
            missing.append("memos: 需要配置 api_url + token")
        if not wiki.exists:
            missing.append("wiki: 目录不存在")
        if not wiki.writable:
            missing.append("wiki: 目录不可写")

        agents_dict = {}
        for a in agents:
            agents_dict[a.name] = {
                "data_dir_found": a.available,
                "data_dir_path": a.data_dir,
                "hooks_installed": a.hooks_installed,
            }

        # 补充 PathDiscover 发现的额外 Agent（可能没有 adapter）
        from core.sync_framework.registry import PathDiscover
        for name in ["claude", "kimi", "hermes", "codex", "openclaw", "aider", "gemini", "cursor", "windsurf"]:
            if name not in agents_dict:
                data_dir = PathDiscover.find(name)
                agents_dict[name] = {
                    "data_dir_found": data_dir is not None,
                    "data_dir_path": str(data_dir) if data_dir else None,
                    "hooks_installed": False,  # 无 adapter 无法验证 hooks
                }

        return {
            "connections": {
                "memos": {
                    "enabled": memos.enabled,
                    "configured": memos.configured,
                    "api_url": memos.api_url,
                    "reachable": memos.reachable,
                },
                "wiki": {
                    "path": wiki.path,
                    "exists": wiki.exists,
                    "writable": wiki.writable,
                },
            },
            "agents": agents_dict,
            "missing": missing,
            "tasks": [
                {
                    "priority": t.priority,
                    "task": t.task,
                    "action": t.action,
                    "completed": t.completed,
                }
                for t in tasks
            ],
            "host_agent": os.environ.get("MNEMOS_HOST_AGENT", "unknown"),
            "mnemos_version": "2.0.0",
        }

    @classmethod
    def quick_status(cls) -> Dict[str, Any]:
        """返回简洁的连接状态摘要（供快速检查使用）"""
        memos = cls.check_memos()
        wiki = cls.check_wiki()
        agents = cls.check_agents()

        total_agents = len(agents)
        hooked_agents = sum(1 for a in agents if a.hooks_installed)

        return {
            "ready": memos.configured and wiki.exists and wiki.writable and hooked_agents > 0,
            "memos": {"configured": memos.configured, "reachable": memos.reachable},
            "wiki": {"exists": wiki.exists, "writable": wiki.writable},
            "agents": {
                "total": total_agents,
                "hooked": hooked_agents,
                "names": [a.name for a in agents if a.available],
            },
        }
