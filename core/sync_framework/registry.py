# -*- coding: utf-8 -*-
"""
SourceRegistry — 插件注册表 + AgentLifecycleManager

支持自动发现 + 手动注册。
启动时检查各 Agent 数据目录是否存在，存在的才实例化对应的 Source 类。
AgentLifecycleManager 管理 Agent 的启动发现 + 5 分钟刷新 + 崩溃指数退避重启。

Usage:
    from core.sync_framework.registry import SourceRegistry
    SourceRegistry.register_builtin_agents()
    active = SourceRegistry.auto_discover()
"""

from __future__ import annotations

import json
import importlib
import logging
import os
import threading
import time

from pathlib import Path
from typing import Dict, List, Mapping, Optional, Type, cast

from core.agent_kit.source_support_manifest import (
    AgentSourceSupportSpec,
    AgentSourceSupportManifestError,
    expand_path_templates,
    get_agent_source_support_manifest,
)
from core.ops.durable_io import DurableIOError, inspect_path_kind
from core.ops.durable_io import read_native_bytes
from .agent_source import AgentSource
from core.config import get_config

logger = logging.getLogger(__name__)

_RECOVERABLE_DISCOVERY_ERRORS = (
    OSError,
    ValueError,
    TypeError,
    KeyError,
    ImportError,
    AttributeError,
    RuntimeError,
)


class SourceRegistryUnavailableError(RuntimeError):
    """A declared parser exists but its runtime discovery boundary failed."""

    def __init__(self, source_name: str, operation: str) -> None:
        self.source_name = str(source_name)
        self.operation = str(operation)
        super().__init__(f"source_registry_unavailable:{self.source_name}:{self.operation}")


class PathDiscoveryUnavailableError(RuntimeError):
    """A discovery probe failed, so source absence cannot be certified."""

    def __init__(self, source_name: str, *stages: str) -> None:
        self.source_name = str(source_name)
        self.stages = tuple(
            dict.fromkeys(str(stage) for stage in stages if str(stage))
        )
        suffix = ",".join(self.stages) or "unknown"
        super().__init__(f"path_discovery_unavailable:{self.source_name}:{suffix}")


class SourceRegistry:
    """Agent Source 插件注册表"""

    _registry: Dict[str, Type[AgentSource]] = {}
    _instances: Dict[str, AgentSource] = {}

    @classmethod
    def register(cls, name: str, source_class: Type[AgentSource]) -> None:
        """Register one active manifest-declared native source class."""
        spec = cls._require_manifest_source_class(name, source_class)
        cls._registry[spec.name] = source_class

    @classmethod
    def _require_manifest_source_class(
        cls,
        name: str,
        source_class: Type[AgentSource],
    ) -> AgentSourceSupportSpec:
        """Require the exact parser class declared by the support manifest."""
        manifest = get_agent_source_support_manifest()
        spec = manifest.require_active_source(name)
        try:
            module = importlib.import_module(spec.parser_module)
            expected_class = getattr(module, spec.parser_class)
        except (ImportError, AttributeError) as exc:
            raise AgentSourceSupportManifestError(
                f"{spec.name}: declared parser cannot be imported"
            ) from exc
        if source_class is not expected_class:
            raise AgentSourceSupportManifestError(
                f"{spec.name}: registry parser must be "
                f"{spec.parser_module}.{spec.parser_class}"
            )
        return spec

    @classmethod
    def _validate_source_instance(
        cls,
        name: str,
        source_class: Type[AgentSource],
        source: AgentSource,
    ) -> str:
        """Reject substituted classes or instances that report another source."""
        spec = cls._require_manifest_source_class(name, source_class)
        if type(source) is not source_class:
            raise AgentSourceSupportManifestError(
                f"{spec.name}: instantiated parser class does not match the registry"
            )
        manifest = get_agent_source_support_manifest()
        reported_name = manifest.normalize_name(source.name)
        if reported_name != spec.name:
            raise AgentSourceSupportManifestError(
                f"registry source {spec.name} instantiated parser reporting {source.name!r}"
            )
        return spec.name

    @classmethod
    def auto_discover(cls) -> List[AgentSource]:
        """
        自动发现：检查各 Agent 数据目录是否存在，
        存在的才实例化对应的 Source 类。
        """
        discovered: List[AgentSource] = []
        for name, source_class in cls._registry.items():
            canonical_name = cls._require_manifest_source_class(name, source_class).name
            # 复用已有实例
            if canonical_name in cls._instances:
                source = cls._instances[canonical_name]
                cls._validate_source_instance(canonical_name, source_class, source)
                discovered.append(source)
                continue

            try:
                source = source_class()
                cls._validate_source_instance(canonical_name, source_class, source)
                data_dir = source.data_dir or PathDiscover.find(canonical_name)
                if data_dir and PathDiscover._path_exists(
                    canonical_name,
                    data_dir,
                    "registered_source_root",
                ):
                    cls._instances[canonical_name] = source
                    discovered.append(source)
                    logger.info("[SourceRegistry] source_available=%s", canonical_name)
            except AgentSourceSupportManifestError:
                raise
            except _RECOVERABLE_DISCOVERY_ERRORS as exc:
                raise SourceRegistryUnavailableError(
                    canonical_name,
                    "auto_discover",
                ) from exc
        return discovered

    @classmethod
    def get(cls, name: str) -> Optional[AgentSource]:
        """获取已注册的 AgentSource 实例"""
        canonical_name = get_agent_source_support_manifest().normalize_name(name)
        source_class = cls._registry.get(canonical_name)
        if canonical_name in cls._instances:
            if source_class is None:
                return None
            source = cls._instances[canonical_name]
            cls._validate_source_instance(canonical_name, source_class, source)
            return source
        if source_class:
            cls._require_manifest_source_class(canonical_name, source_class)
            try:
                source = source_class()
                cls._validate_source_instance(canonical_name, source_class, source)
                data_dir = source.data_dir or PathDiscover.find(canonical_name)
                if data_dir and PathDiscover._path_exists(
                    canonical_name,
                    data_dir,
                    "registered_source_root",
                ):
                    cls._instances[canonical_name] = source
                    return source
            except AgentSourceSupportManifestError:
                raise
            except _RECOVERABLE_DISCOVERY_ERRORS as exc:
                raise SourceRegistryUnavailableError(
                    canonical_name,
                    "get",
                ) from exc
        return None

    @classmethod
    def list_registered(cls) -> List[str]:
        """列出所有已注册的 Agent 名称"""
        return list(cls._registry.keys())

    @classmethod
    def list_active(cls) -> List[str]:
        """列出所有活跃的 Agent 名称"""
        return list(cls._instances.keys())

    @classmethod
    def list_sources(cls) -> List[AgentSource]:
        """发现所有可用的 Agent Source（注册内置 Agent 后自动发现）。"""
        cls.register_builtin_agents()
        return cls.auto_discover()

    @classmethod
    def builtin_agent_specs(cls) -> tuple[tuple[str, str, str], ...]:
        """Return active parser specs generated by the support manifest."""
        return get_agent_source_support_manifest().builtin_registry_specs()

    @classmethod
    def list_builtin_agent_names(cls) -> List[str]:
        """Return built-in AgentSource names without mutating the registry."""
        return [name for name, _, _ in cls.builtin_agent_specs()]

    @classmethod
    def get_builtin_source_class(cls, name: str) -> Optional[Type[AgentSource]]:
        """Return a built-in AgentSource class without registering it globally."""
        target = name.lower()
        for agent_name, module_name, class_name in cls.builtin_agent_specs():
            if agent_name != target:
                continue
            module = importlib.import_module(module_name)
            return cast(Type[AgentSource], getattr(module, class_name))
        return None

    @classmethod
    def reset(cls) -> None:
        """清空已发现 source 实例，保留注册表。用于 daemon 重启或测试隔离。"""
        cls._instances.clear()

    @classmethod
    def register_builtin_agents(cls) -> None:
        """注册所有内置 Agent"""
        from core.import_guard import assert_allowed_module

        for name, module_path, class_name in cls.builtin_agent_specs():
            if name in cls._registry:
                cls._require_manifest_source_class(name, cls._registry[name])
                continue
            try:
                assert_allowed_module(module_path)
                import importlib

                module = importlib.import_module(module_path)
                source_class = getattr(module, class_name)
                cls.register(name, source_class)
            except (ImportError, AttributeError, ValueError) as exc:
                raise AgentSourceSupportManifestError(
                    f"{name}: builtin parser registration failed"
                ) from exc


class PathDiscover:
    """跨平台 Agent 数据目录发现（5 层回退 + 缓存 + 启发式搜索）"""

    _cache: Dict[str, tuple[Optional[Path], float]] = {}
    _cache_ttl = 60  # 秒

    @classmethod
    def _normalized_name(cls, agent_name: str) -> str:
        return get_agent_source_support_manifest().normalize_name(agent_name)

    @classmethod
    def _root_resolver(cls, agent_name: str) -> dict:
        spec = get_agent_source_support_manifest().source(agent_name)
        return dict(spec.root_resolver)

    @classmethod
    def _path_kind(cls, agent_name: str, path: Path, stage: str) -> str:
        try:
            return inspect_path_kind(path)
        except DurableIOError:
            raise PathDiscoveryUnavailableError(agent_name, stage) from None

    @classmethod
    def _path_exists(cls, agent_name: str, path: Path, stage: str) -> bool:
        return cls._path_kind(agent_name, path, stage) != "missing"

    @classmethod
    def _paths_from_value(cls, value: str, mode: str, agent_name: str) -> list[Path]:
        raw = str(value or "").strip()
        if not raw:
            return []
        if mode == "path_list":
            separator = "," if "," in raw else os.pathsep
            return [Path(item.strip()).expanduser() for item in raw.split(separator) if item.strip()]
        path = Path(raw).expanduser()
        if mode == "parent_if_file":
            return (
                [path.parent]
                if cls._path_kind(agent_name, path, "configured_path") == "file"
                else [path]
            )
        if mode.startswith("append:"):
            return [path / mode.partition(":")[2]]
        if mode == "openclaw_profile":
            return [Path.home() / f".openclaw-{raw}"]
        return [path]

    @classmethod
    def resolve_agent_subdir(cls, agent_name: str, base_path: Path) -> Path:
        """Return the observed session/project subdir for a discovered agent root.

        ``find()`` intentionally returns the agent root for compatibility. Watchers
        and status surfaces can use this helper to prefer the high-churn transcript
        directory when it exists.
        """
        base = Path(base_path).expanduser()
        try:
            resolver = cls._root_resolver(agent_name)
        except AgentSourceSupportManifestError:
            return base
        subdir = str(resolver.get("transcript_subdir") or "")
        if not subdir:
            return base

        subdir_parts = Path(subdir).parts
        if subdir_parts and tuple(base.parts[-len(subdir_parts) :]) == subdir_parts:
            return base

        candidate = base.joinpath(*subdir_parts)
        return (
            candidate
            if cls._path_exists(agent_name, candidate, "transcript_subdir")
            else base
        )

    @classmethod
    def find(cls, agent_name: str) -> Optional[Path]:
        """发现 Agent 数据目录（5 层回退）"""
        normalized_name = cls._normalized_name(agent_name)
        # 缓存命中检查
        cached = cls._cache.get(normalized_name)
        if cached:
            path, ts = cached
            if time.time() - ts < cls._cache_ttl:
                return path

        result = cls._do_find(normalized_name)
        cls._cache[normalized_name] = (result, time.time())
        return result

    @classmethod
    def _do_find(cls, agent_name: str) -> Optional[Path]:
        """实际发现逻辑"""
        try:
            resolver = cls._root_resolver(agent_name)
        except AgentSourceSupportManifestError:
            return None
        failed_stages: list[str] = []

        # 1. 用户显式配置
        config = cls._load_user_config(agent_name)
        if agent_name in config:
            path = Path(config[agent_name]).expanduser()
            if cls._path_exists(agent_name, path, "user_config_path"):
                return path

        # 2. 环境变量（声明来自 support manifest）
        for environment in resolver.get("environment", []):
            if not isinstance(environment, Mapping):
                continue
            env_var = str(environment.get("name") or "")
            mode = str(environment.get("mode") or "path")
            val = os.environ.get(env_var)
            if not val:
                continue
            for path in cls._paths_from_value(val, mode, agent_name):
                try:
                    if cls._path_exists(agent_name, path, "environment_path"):
                        return path
                except PathDiscoveryUnavailableError:
                    failed_stages.append("environment_path")

        # 3. 进程探测（增强为所有 Agent）
        try:
            result = cls._discover_from_process(agent_name)
            if result:
                return result
        except _RECOVERABLE_DISCOVERY_ERRORS:
            failed_stages.append("process_probe")
            logger.warning(
                "[PathDiscover] source=%s stage=process_probe unavailable",
                agent_name,
            )

        # 4. 文件系统启发式搜索（新增）
        try:
            result = cls._heuristic_search(agent_name)
            if result:
                return result
        except _RECOVERABLE_DISCOVERY_ERRORS:
            failed_stages.append("heuristic_probe")
            logger.warning(
                "[PathDiscover] source=%s stage=heuristic_probe unavailable",
                agent_name,
            )

        # 5. 标准路径（声明来自 support manifest）
        for path in expand_path_templates(resolver.get("standard_paths", [])):
            try:
                if cls._path_exists(agent_name, path, "standard_path"):
                    return path
            except PathDiscoveryUnavailableError:
                failed_stages.append("standard_path")

        if failed_stages:
            raise PathDiscoveryUnavailableError(agent_name, *failed_stages)
        return None

    @classmethod
    def invalidate_cache(cls, agent_name: str | None = None):
        """使缓存失效。agent_name=None 时清空全部缓存。"""
        if agent_name is None:
            cls._cache.clear()
        else:
            cls._cache.pop(agent_name, None)

    @classmethod
    def _discover_from_process(cls, agent_name: str) -> Optional[Path]:
        """通过 psutil 进程探测发现数据目录（支持所有 Agent）"""
        try:
            import psutil
        except ImportError:
            return None

        try:
            process_args = cls._root_resolver(agent_name).get("process_args", [])
        except AgentSourceSupportManifestError:
            process_args = []
        arg_mapping = {
            str(item.get("flag")): str(item.get("mode") or "path")
            for item in process_args
            if isinstance(item, Mapping) and item.get("flag")
        }

        inspection_failed = False
        for proc in psutil.process_iter(["name", "cmdline"]):
            name = proc.info.get("name", "") or ""
            if agent_name.lower() not in name.lower():
                continue

            cmdline = proc.info.get("cmdline") or []
            # 1. 解析已知 CLI 参数
            for i, arg in enumerate(cmdline):
                if arg in arg_mapping and i + 1 < len(cmdline):
                    val = cmdline[i + 1]
                    for candidate in cls._paths_from_value(val, arg_mapping[arg], agent_name):
                        if cls._path_exists(
                            agent_name,
                            candidate,
                            "process_candidate",
                        ):
                            return candidate

            # 2. 从进程 exe 路径推导（通用策略）
            try:
                exe = proc.exe()
                if exe:
                    # 例如 /usr/local/bin/claude → 查找 ~/.claude
                    home = Path.home()
                    dot_dir = home / f".{agent_name.lower()}"
                    if cls._path_exists(agent_name, dot_dir, "process_home"):
                        return dot_dir
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                # 进程在迭代过程中已退出，忽略并继续下一个
                continue
            except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
                inspection_failed = True

        if inspection_failed:
            raise PathDiscoveryUnavailableError(agent_name, "process_inspection")
        return None

    @classmethod
    def _heuristic_search(cls, agent_name: str) -> Optional[Path]:
        """文件系统启发式搜索：在常见父目录中搜索已知配置文件"""
        try:
            pattern = cls._root_resolver(agent_name).get("heuristic")
        except AgentSourceSupportManifestError:
            pattern = None
        if not isinstance(pattern, Mapping) or not pattern.get("filenames"):
            return None

        # 常见父目录（按优先级）
        parents = [
            Path.home(),
            Path.home() / ".config",
            Path.home() / "Library" / "Application Support",
        ]

        filenames = pattern["filenames"]  # type: ignore[index]
        marker = pattern.get("content_marker")  # type: ignore[attr-defined]

        inspection_failed = False
        for parent in parents:
            try:
                if not cls._path_exists(agent_name, parent, "heuristic_parent"):
                    continue
            except PathDiscoveryUnavailableError:
                inspection_failed = True
                continue
            try:
                for entry in os.scandir(parent):
                    if not entry.is_dir():
                        continue
                    # 目录名匹配 agent_name（如 .claude, kimi）
                    if agent_name.lower() not in entry.name.lower():
                        continue
                    for fn in filenames:
                        candidate = Path(entry.path) / fn
                        if cls._path_exists(
                            agent_name,
                            candidate,
                            "heuristic_candidate",
                        ):
                            if marker:
                                try:
                                    content = read_native_bytes(candidate).decode("utf-8")
                                    if marker in content:
                                        return Path(entry.path)
                                except (OSError, UnicodeError):
                                    inspection_failed = True
                                    continue
                            else:
                                return Path(entry.path)
            except (OSError, PathDiscoveryUnavailableError):
                inspection_failed = True
                continue
        if inspection_failed:
            raise PathDiscoveryUnavailableError(agent_name, "heuristic_inspection")
        return None

    @classmethod
    def _load_user_config(cls, agent_name: str) -> Dict[str, str]:
        """加载用户显式配置"""
        config_file = get_config().data_dir / "configs" / "agent_paths.json"
        try:
            config_file.stat()
        except FileNotFoundError:
            return {}
        except OSError:
            raise PathDiscoveryUnavailableError(
                agent_name,
                "user_config",
            ) from None
        try:
            payload = json.loads(read_native_bytes(config_file).decode("utf-8"))
            if not isinstance(payload, dict) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in payload.items()
            ):
                raise ValueError("agent path configuration must be a string mapping")
            return cast(Dict[str, str], payload)
        except (OSError, ValueError, TypeError):
            raise PathDiscoveryUnavailableError(
                agent_name,
                "user_config",
            ) from None


class AgentLifecycleManager:
    """
    Agent 生命周期管理器。

    职责：
    - 启动时发现所有活跃 Agent
    - 5 分钟刷新检查（新 Agent 上线 / 离线 Agent 恢复）
    - 崩溃指数退避重启
    - 离线 Agent 不销毁触发器，保持等待恢复
    """

    def __init__(self, refresh_interval: int = 300):
        self._refresh_interval = refresh_interval
        self._active_agents: Dict[str, AgentSource] = {}
        self._error_counts: Dict[str, int] = {}
        self._running = False
        self._refresh_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start(self):
        """启动生命周期管理"""
        if self._running:
            logger.warning("[LifecycleManager] 已启动，跳过重复调用")
            return
        self._running = True
        self._refresh_agents()
        self._refresh_thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self._refresh_thread.start()
        logger.info("[LifecycleManager] 启动，监控 %s 个 Agent", len(self._active_agents))

    def stop(self):
        """停止生命周期管理"""
        self._running = False
        if self._refresh_thread:
            self._refresh_thread.join(timeout=5)

    def discover_agents(self):
        """手动触发一次 Agent 发现（兼容 daemon 旧调用）"""
        self._refresh_agents()

    def get_active_agents(self) -> Dict[str, AgentSource]:
        """获取当前活跃的 Agent"""
        with self._lock:
            return dict(self._active_agents)

    def report_error(self, agent_name: str):
        """报告 Agent 错误"""
        with self._lock:
            self._error_counts[agent_name] = self._error_counts.get(agent_name, 0) + 1

    def report_success(self, agent_name: str):
        """报告 Agent 成功"""
        with self._lock:
            self._error_counts[agent_name] = 0

    def _refresh_agents(self):
        """刷新活跃 Agent 列表"""
        SourceRegistry.register_builtin_agents()
        active = SourceRegistry.auto_discover()
        with self._lock:
            new_names = {a.name for a in active}
            old_names = set(self._active_agents.keys())
            for agent in active:
                self._active_agents[agent.name] = agent
            # 离线 Agent 保留（不删除），等待恢复
            for name in old_names - new_names:
                logger.info("[LifecycleManager] Agent %s 离线，保留等待恢复", name)

    def _refresh_loop(self):
        """定时刷新循环"""
        while self._running:
            # 分段 sleep
            end = time.monotonic() + self._refresh_interval
            while self._running and time.monotonic() < end:
                time.sleep(min(5, end - time.monotonic()))

            if not self._running:
                break

            try:
                self._refresh_agents()
            except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError) as e:
                logger.error("[LifecycleManager] 刷新失败: %s", e, exc_info=True)
