"""
core/diagnostics.py — Mnemos 连接诊断引擎

提供统一的连接状态检测和任务清单生成，被 MCP 工具和 CLI 共享使用。
避免 agora.py 和 mnemos_cli.py 中的重复诊断逻辑。
"""

import os
import importlib
import logging
import uuid
from typing import Dict, List, Optional, Any, Protocol
from dataclasses import dataclass

from core.ops.durable_io import inspect_path_kind

logger = logging.getLogger(__name__)


@dataclass
class WikiStatus:
    path: str = ""
    exists: bool = False
    writable: bool = False


@dataclass
class StorageStatus:
    backend: str = "obsidian"
    configured: bool = False
    reachable: Optional[bool] = None
    error: Optional[str] = None
    path: Optional[str] = None  # Obsidian vault path


@dataclass
class AgentStatus:
    name: str = ""
    available: bool = False
    data_dir: Optional[str] = None
    hooks_installed: bool = False
    mcp_configured: bool = False
    policy_installed: bool = False
    active_ready: bool = False
    active_runtime_state: str = "unknown"
    active_runtime_error_code: str = ""
    passive_source_available: bool = False
    passive_source_state: str = "unknown"
    passive_source_error_code: str = ""


@dataclass
class ConnectionTask:
    priority: str = ""  # "high" | "medium" | "low"
    task: str = ""
    action: str = ""
    completed: bool = False


class AgentStatusProvider(Protocol):
    """Adapter-side provider for active agent diagnostics."""

    def list_agent_statuses(self) -> List[AgentStatus]:
        ...


_AGENT_STATUS_PROVIDERS: List[AgentStatusProvider] = []
_AGENT_STATUS_PROVIDER_KEYS: set[str] = set()
_DEFAULT_AGENT_STATUS_PROVIDER_MODULES = ("integrations.diagnostics_provider",)


class AgentDiagnosticsUnavailableError(RuntimeError):
    """The active diagnostics provider boundary could not be evaluated."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


def register_agent_status_provider(
    provider: AgentStatusProvider,
    *,
    key: str | None = None,
) -> None:
    """Register an adapter-owned provider without importing adapters from core."""
    if key:
        if key in _AGENT_STATUS_PROVIDER_KEYS:
            return
        _AGENT_STATUS_PROVIDER_KEYS.add(key)
    _AGENT_STATUS_PROVIDERS.append(provider)


def clear_agent_status_providers() -> None:
    """Clear registered providers; intended for tests and controlled reconfiguration."""
    _AGENT_STATUS_PROVIDERS.clear()
    _AGENT_STATUS_PROVIDER_KEYS.clear()


def _load_default_agent_status_providers() -> None:
    """Load optional provider modules that self-register via the public registry."""
    from core.import_guard import assert_allowed_module

    for module_name in _DEFAULT_AGENT_STATUS_PROVIDER_MODULES:
        try:
            assert_allowed_module(module_name)
            module = importlib.import_module(module_name)
            register = getattr(module, "register_diagnostics_providers", None)
            if callable(register):
                register()
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
            logger.debug("加载 Agent 诊断 provider 失败: %s", module_name, exc_info=True)
            raise AgentDiagnosticsUnavailableError(
                "active_diagnostics_provider_load_failed"
            ) from None


def _isolated_default_agent_status_providers() -> List[AgentStatusProvider]:
    """Return default providers without touching the process-global registry."""
    try:
        from core.import_guard import assert_allowed_module

        assert_allowed_module("integrations.diagnostics_provider")
        from integrations.diagnostics_provider import default_diagnostics_providers

        return list(default_diagnostics_providers())
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
        logger.debug("构建隔离 Agent 诊断 provider 失败", exc_info=True)
        raise AgentDiagnosticsUnavailableError(
            "active_diagnostics_provider_load_failed"
        ) from None


class ConnectionDiagnostics:
    """Mnemos 连接诊断引擎

    统一检测 Storage、Wiki、Agent 的连接状态，生成任务清单。
    被 MCP self_diagnose / detect_sources 和 CLI cmd_agent detect 共享。
    """

    @classmethod
    def check_storage(cls, config=None, *, probe_writable: bool = False) -> StorageStatus:
        """检查当前配置的存储后端状态（默认只读）。

        ``probe_writable`` 只供显式诊断使用；探针采用唯一文件名和
        ``O_EXCL``，不会复用或删除用户同名文件。
        """
        if config is None:
            from core.config import get_config

            config = get_config()

        backend = config.storage_backend
        status = StorageStatus(backend=backend)

        if backend == "obsidian":
            vault_path = config.obsidian_vault_path
            status.path = str(vault_path)
            # configured = True when user explicitly set storage.obsidian.vault_path
            # or configured vaults.raw.path (auto-resolved path)
            raw_cfg = getattr(config, "_data", {}) if config else {}
            explicit = raw_cfg.get("storage", {}).get("obsidian", {}).get("vault_path")
            raw_vault = raw_cfg.get("vaults", {}).get("raw", {}).get("path")
            status.configured = bool(explicit) or bool(raw_vault)
            try:
                status.reachable = bool(
                    vault_path.exists()
                    and vault_path.is_dir()
                    and os.access(vault_path, os.R_OK)
                )
                if status.reachable and probe_writable:
                    cls._probe_directory_writable(vault_path)
            except (OSError, RuntimeError, ValueError, TypeError) as e:
                status.reachable = False
                status.error = str(e)
        else:
            status.error = f"未知存储后端: {backend}"
            status.reachable = False

        return status

    @staticmethod
    def _probe_directory_writable(path) -> None:
        probe = path / f".mnemos_write_test.{uuid.uuid4().hex}"
        descriptor = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, b"ok")
        finally:
            os.close(descriptor)
            probe.unlink(missing_ok=True)

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
    def check_agents(
        cls,
        *,
        load_default_providers: bool = True,
        isolated_default_providers: bool = False,
    ) -> List[AgentStatus]:
        """检查所有已发现 Agent 的状态（主动接入 + 被动数据源）。

        ``load_default_providers=False`` is used by conformance probes and tests
        that must not mutate global adapter/source registries.

        ``isolated_default_providers=True`` supplies fresh read-only default
        providers without registering them, so health can report the same
        active configuration as Agent Kit without mutating global registries.
        """
        results: List[AgentStatus] = []
        providers = list(_AGENT_STATUS_PROVIDERS)
        active_diagnostics_error_code = ""
        if isolated_default_providers:
            if load_default_providers:
                raise ValueError(
                    "isolated_default_providers requires load_default_providers=False"
                )
            try:
                providers.extend(_isolated_default_agent_status_providers())
            except AgentDiagnosticsUnavailableError as exc:
                active_diagnostics_error_code = exc.code
        if load_default_providers:
            try:
                _load_default_agent_status_providers()
            except AgentDiagnosticsUnavailableError as exc:
                active_diagnostics_error_code = exc.code
            providers = list(_AGENT_STATUS_PROVIDERS)

        # 1. 检测 adapter 侧注册的主动接入状态
        for provider in providers:
            try:
                statuses = provider.list_agent_statuses()
                if statuses:
                    results.extend(statuses)
            except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError) as e:
                logger.warning("Agent status provider failed: %s", e, exc_info=True)
                active_diagnostics_error_code = "active_diagnostics_provider_probe_failed"

        if active_diagnostics_error_code:
            from core.agent_kit.source_support_manifest import (
                get_agent_source_support_manifest,
            )

            for name in get_agent_source_support_manifest().host_agent_names:
                existing = next((item for item in results if item.name == name), None)
                if existing is None:
                    existing = AgentStatus(name=name)
                    results.append(existing)
                existing.active_runtime_state = "unavailable"
                existing.active_runtime_error_code = active_diagnostics_error_code

        if not load_default_providers:
            return results

        # 2. 检测 sync_framework 被动数据源
        try:
            # type: ignore[attr-defined]
            from core.sync_framework.registry import (  # type: ignore[attr-defined]
                SourceRegistry,
                SourceRegistryUnavailableError,
            )

            # 只在注册表为空时触发一次内置注册，避免诊断检查产生副作用
            if not SourceRegistry.list_registered():
                SourceRegistry.register_builtin_agents()
            for name in SourceRegistry.list_registered():
                try:
                    source = SourceRegistry.get(name)
                    if source is None:
                        has_passive = False
                        passive_state = "absent"
                        error_code = ""
                    else:
                        data_dir = source.data_dir
                        has_passive = (
                            data_dir is not None
                            and inspect_path_kind(data_dir) != "missing"
                        )
                        passive_state = "available" if has_passive else "absent"
                        error_code = ""
                except SourceRegistryUnavailableError as exc:
                    has_passive = False
                    passive_state = "unavailable"
                    error_code = str(exc)
                except (
                    OSError,
                    ValueError,
                    TypeError,
                    KeyError,
                    ImportError,
                    AttributeError,
                    RuntimeError,
                ):
                    has_passive = False
                    passive_state = "unavailable"
                    error_code = "passive_source_probe_failed"

                existing = next((r for r in results if r.name == name), None)
                if existing:
                    if has_passive and data_dir is not None and not existing.data_dir:
                        existing.data_dir = str(data_dir)
                    existing.passive_source_available = has_passive
                    existing.passive_source_state = passive_state
                    existing.passive_source_error_code = error_code
                else:
                    results.append(
                        AgentStatus(
                            name=name,
                            available=False,
                            data_dir=str(data_dir) if has_passive and data_dir else None,
                            passive_source_available=has_passive,
                            passive_source_state=passive_state,
                            passive_source_error_code=error_code,
                        )
                    )
        except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError) as e:
            logger.debug("被动数据源检测失败: %s", e)
            try:
                from core.agent_kit.source_support_manifest import (
                    get_agent_source_support_manifest,
                )

                names = get_agent_source_support_manifest().active_source_names
            except (
                ImportError,
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
                KeyError,
            ):
                names = ()
            for name in names:
                existing = next((item for item in results if item.name == name), None)
                if existing is None:
                    existing = AgentStatus(name=name)
                    results.append(existing)
                existing.passive_source_available = False
                existing.passive_source_state = "unavailable"
                existing.passive_source_error_code = "source_registry_probe_failed"

        return results

    @classmethod
    def generate_task_list(
        cls,
        wiki: WikiStatus | None = None,
        agents: List[AgentStatus] | None = None,
        storage: StorageStatus | None = None,
    ) -> List[ConnectionTask]:
        """基于检测结果生成优先级排序的连接任务清单"""
        tasks = []

        if wiki is None:
            wiki = cls.check_wiki()
        if agents is None:
            agents = cls.check_agents()
        if storage is None:
            storage = cls.check_storage()

        # High priority: Storage backend (Obsidian)
        if not storage.reachable:
            tasks.append(
                ConnectionTask(
                    priority="high",
                    task="确认 Obsidian Vault",
                    action=f"检查 Obsidian Vault 路径是否可写: {storage.path or '未配置'}",
                    completed=False,
                )
            )
        else:
            tasks.append(
                ConnectionTask(
                    priority="high",
                    task="确认 Obsidian Vault",
                    action=f"已就绪: {storage.path}",
                    completed=True,
                )
            )

        # High priority: Wiki
        if not wiki.exists:
            tasks.append(
                ConnectionTask(
                    priority="high",
                    task="确认 Wiki 路径",
                    action="询问用户 Obsidian Vault 路径，调用 configure_wiki(vault_path=...)",
                    completed=False,
                )
            )
        elif not wiki.writable:
            tasks.append(
                ConnectionTask(
                    priority="high",
                    task="确认 Wiki 路径",
                    action=f"路径存在但不可写: {wiki.path}",
                    completed=False,
                )
            )
        else:
            tasks.append(
                ConnectionTask(
                    priority="high",
                    task="确认 Wiki 路径",
                    action=f"已就绪: {wiki.path}",
                    completed=True,
                )
            )

        # Medium priority: Agent active integration
        detected_agents = [a for a in agents if a.available or a.passive_source_available]
        unavailable_agents = [
            a for a in agents if a.passive_source_state == "unavailable"
        ]
        for agent in unavailable_agents:
            tasks.append(
                ConnectionTask(
                    priority="medium",
                    task=f"恢复 {agent.name} 被动数据源诊断",
                    action=(
                        "数据源状态不可读，先修复诊断边界: "
                        f"{agent.passive_source_error_code or 'passive_source_probe_failed'}"
                    ),
                    completed=False,
                )
            )
        if not detected_agents and not unavailable_agents:
            tasks.append(
                ConnectionTask(
                    priority="medium",
                    task="安装 Agent 主动接入",
                    action="未检测到任何支持的 Agent，请确保 Claude Code / Kimi 等已安装",
                    completed=False,
                )
            )
        else:
            for agent in detected_agents:
                if not agent.available and agent.passive_source_available:
                    # 纯被动数据源（无 adapter），不生成主动接入任务
                    tasks.append(
                        ConnectionTask(
                            priority="low",
                            task=f"发现 {agent.name} 被动数据源",
                            action="被动数据源可用，无主动接入适配器",
                            completed=True,
                        )
                    )
                    continue
                # 有 adapter 的 agent，检查主动接入状态
                if not agent.hooks_installed:
                    tasks.append(
                        ConnectionTask(
                            priority="medium",
                            task=f"安装 {agent.name} hooks",
                            action=f"调用 mnemos doctor repair {agent.name}",
                            completed=False,
                        )
                    )
                if not agent.mcp_configured:
                    tasks.append(
                        ConnectionTask(
                            priority="medium",
                            task=f"配置 {agent.name} Mnemos MCP",
                            action=f"调用 mnemos doctor repair {agent.name} 写入 preflight/guard/wiki_search 工具",  # noqa: E501
                            completed=False,
                        )
                    )
                if not agent.policy_installed:
                    tasks.append(
                        ConnectionTask(
                            priority="medium",
                            task=f"安装 {agent.name} 主动使用策略",
                            action=f"调用 mnemos doctor repair {agent.name} 写入 Mnemos Active Policy",
                            completed=False,
                        )
                    )

        # Low priority: 额外 Agent 数据源（仅检测，不一定需要 hooks）
        from core.agent_kit.source_support_manifest import get_agent_source_support_manifest
        for name in get_agent_source_support_manifest().ingestion_only_source_names:
            observed = next((agent for agent in agents if agent.name == name), None)
            if observed is not None and observed.passive_source_state == "available":
                tasks.append(
                    ConnectionTask(
                        priority="low",
                        task=f"发现 {name} 数据源",
                        action=(
                            f"路径: {observed.data_dir}"
                            if observed.data_dir
                            else "被动数据源可用"
                        ),
                        completed=True,
                    )
                )

        # Sort: high -> medium -> low, incomplete first within same priority
        priority_order = {"high": 0, "medium": 1, "low": 2}
        tasks.sort(key=lambda t: (priority_order.get(t.priority, 99), t.completed))

        return tasks

    @classmethod
    def full_report(cls) -> Dict[str, Any]:
        """返回完整诊断报告（供 self_diagnose 使用）"""
        wiki = cls.check_wiki()
        agents = cls.check_agents()
        storage = cls.check_storage()
        tasks = cls.generate_task_list(wiki, agents, storage)

        missing = []
        if not storage.reachable:
            missing.append(f"storage: Obsidian Vault 不可写 ({storage.path})")
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
                "mcp_configured": a.mcp_configured,
                "policy_installed": a.policy_installed,
                "active_ready": a.active_ready,
                "passive_source_available": a.passive_source_available,
                "passive_source_state": a.passive_source_state,
                "passive_source_error_code": a.passive_source_error_code,
            }

        # 补充 PathDiscover 发现的额外 Agent（可能没有 adapter）
        from core.agent_kit.source_support_manifest import get_agent_source_support_manifest
        for name in get_agent_source_support_manifest().active_source_names:
            if name not in agents_dict:
                spec = get_agent_source_support_manifest().source(name)
                agents_dict[name] = {
                    "data_dir_found": False,
                    "data_dir_path": None,
                    "source_role": spec.role,
                    "hooks_installed": False,  # 无 adapter 无法验证 hooks
                    "mcp_configured": False,
                    "policy_installed": False,
                    "active_ready": False,
                    "passive_source_available": False,
                    "passive_source_state": "unavailable",
                    "passive_source_error_code": "source_status_missing",
                }

        return {
            "connections": {
                "storage": {
                    "backend": storage.backend,
                    "configured": storage.configured,
                    "reachable": storage.reachable,
                    "path": storage.path,
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
        wiki = cls.check_wiki()
        agents = cls.check_agents()
        storage = cls.check_storage()

        total_agents = len(agents)
        hooked_agents = sum(1 for a in agents if a.hooks_installed)
        mcp_agents = sum(1 for a in agents if a.mcp_configured)
        policy_agents = sum(1 for a in agents if a.policy_installed)
        active_agents = sum(1 for a in agents if a.active_ready)
        passive_agents = sum(1 for a in agents if a.passive_source_available)

        storage_ready = storage.reachable

        return {
            "ready": storage_ready and wiki.exists and wiki.writable,
            "has_agents": active_agents > 0 or passive_agents > 0,
            "storage": {
                "backend": storage.backend,
                "configured": storage.configured,
                "reachable": storage.reachable,
            },
            "wiki": {"exists": wiki.exists, "writable": wiki.writable},
            "agents": {
                "total": total_agents,
                "hooked": hooked_agents,
                "mcp": mcp_agents,
                "policy": policy_agents,
                "active": active_agents,
                "passive": passive_agents,
                "names": [a.name for a in agents if a.available or a.passive_source_available],
            },
        }
