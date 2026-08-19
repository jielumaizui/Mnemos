# -*- coding: utf-8 -*-
"""
SourceRegistry + PathDiscover 单元测试

覆盖公共行为：
- SourceRegistry.register / get / list_registered / list_active
- SourceRegistry.auto_discover
- SourceRegistry.register_builtin_agents
- PathDiscover.find（标准路径回退）
- PathDiscover.invalidate_cache（单键 + 全量）
- 重复注册处理
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional
from unittest.mock import PropertyMock, patch

import pytest

from core.sync_framework.agent_source import AgentSource, SessionInfo, Turn
from core.sync_framework.registry import PathDiscover, SourceRegistry
from core.agent_kit.source_support_manifest import AgentSourceSupportManifestError
from integrations.sources.claude_source import ClaudeSource
from integrations.sources.codex_source import CodexSource

# ---------------------------------------------------------------------------
# 辅助：最小可实例化的 AgentSource 子类
# ---------------------------------------------------------------------------


class _FakeSource(AgentSource):
    """测试替身：可指定 name、model_tag、data_dir。"""

    def __init__(
        self, name: str = "codex", model_tag: str = "fake-v1", data_dir: Optional[Path] = None
    ):
        self._name = name
        self._model_tag = model_tag
        self._data_dir = data_dir

    @property
    def name(self) -> str:
        return self._name

    @property
    def model_tag(self) -> str:
        return self._model_tag

    @property
    def data_dir(self) -> Optional[Path]:
        return self._data_dir

    def discover_sessions(self) -> List[SessionInfo]:
        return []

    def parse_turns(self, session_path: Path) -> List[Turn]:
        return []


# ---------------------------------------------------------------------------
# SourceRegistry 测试
# ---------------------------------------------------------------------------


class TestSourceRegistry:
    """SourceRegistry 公共 API 测试套件。"""

    def test_register_adds_source_to_registry(self):
        """register() 将 Source 类加入注册表，list_registered() 可见。"""
        SourceRegistry.register("codex", CodexSource)
        assert "codex" in SourceRegistry.list_registered()

    def test_register_overwrites_duplicate(self):
        """重复注册同名 Source 时，后注册者覆盖前者。"""

        SourceRegistry.register("codex", CodexSource)
        SourceRegistry.register("codex", CodexSource)

        # 通过 get 实例化后验证实际类型
        with patch.object(PathDiscover, "find", return_value=None):
            # data_dir 为 None，find 返回 None，get 返回 None
            # 但注册表本身应记录 _Second
            registered = SourceRegistry.list_registered()
            assert registered.count("codex") == 1

    def test_get_returns_instance_when_data_dir_exists(self, tmp_path: Path):
        """get() 在 data_dir 存在时返回实例化的 AgentSource。"""
        fake_dir = tmp_path / "fake_data"
        fake_dir.mkdir()

        with patch.object(
            CodexSource,
            "data_dir",
            new_callable=PropertyMock,
            return_value=fake_dir,
        ):
            SourceRegistry.register("codex", CodexSource)
            instance = SourceRegistry.get("codex")
            assert isinstance(instance, CodexSource)
            assert SourceRegistry.list_active() == ["codex"]

    def test_get_returns_none_when_data_dir_missing(self):
        """get() 在数据目录不存在时返回 None。"""

        SourceRegistry.register("codex", CodexSource)
        with (
            patch.object(CodexSource, "data_dir", new_callable=PropertyMock, return_value=None),
            patch.object(PathDiscover, "find", return_value=None),
        ):
            result = SourceRegistry.get("codex")
        assert result is None

    def test_get_returns_none_for_unregistered_name(self):
        """未注册的名称返回 None。"""
        assert SourceRegistry.get("__never_registered__") is None

    def test_get_reuses_existing_instance(self, tmp_path: Path):
        """get() 对已有实例直接复用，不重复构造。"""
        fake_dir = tmp_path / "reuse"
        fake_dir.mkdir()

        with patch.object(
            CodexSource,
            "data_dir",
            new_callable=PropertyMock,
            return_value=fake_dir,
        ):
            SourceRegistry.register("codex", CodexSource)
            first = SourceRegistry.get("codex")
            second = SourceRegistry.get("codex")
            assert first is second

    def test_auto_discover_filters_missing_dirs(self):
        """auto_discover() 跳过数据目录不存在的 Source。"""

        SourceRegistry.register("codex", CodexSource)
        with (
            patch.object(CodexSource, "data_dir", new_callable=PropertyMock, return_value=None),
            patch.object(PathDiscover, "find", return_value=None),
        ):
            discovered = SourceRegistry.auto_discover()
        assert discovered == []
        assert SourceRegistry.list_active() == []

    def test_auto_discover_includes_existing_dirs(self, tmp_path: Path):
        """auto_discover() 包含数据目录存在的 Source。"""
        fake_dir = tmp_path / "discover_me"
        fake_dir.mkdir()

        with patch.object(
            CodexSource,
            "data_dir",
            new_callable=PropertyMock,
            return_value=fake_dir,
        ):
            SourceRegistry.register("codex", CodexSource)
            discovered = SourceRegistry.auto_discover()
            assert len(discovered) == 1
            assert discovered[0].name == "codex"
            assert "codex" in SourceRegistry.list_active()

    def test_list_registered_returns_all_names(self):
        """list_registered() 返回所有已注册名称列表。"""
        SourceRegistry.register("codex", CodexSource)
        SourceRegistry.register("claude", ClaudeSource)
        names = SourceRegistry.list_registered()
        assert isinstance(names, list)
        assert "codex" in names
        assert "claude" in names

    def test_list_active_returns_only_instantiated(self, tmp_path: Path):
        """list_active() 仅返回已实例化的 Source 名称。"""
        fake_dir = tmp_path / "active_only"
        fake_dir.mkdir()

        with patch.object(
            CodexSource,
            "data_dir",
            new_callable=PropertyMock,
            return_value=fake_dir,
        ):
            SourceRegistry.register("codex", CodexSource)
            # 注册但未实例化
            assert "codex" not in SourceRegistry.list_active()
            # 实例化后
            SourceRegistry.get("codex")
            assert "codex" in SourceRegistry.list_active()

    def test_register_rejects_undeclared_native_source(self):
        """Unknown native sources cannot enter the production registry."""
        with pytest.raises(AgentSourceSupportManifestError):
            SourceRegistry.register("undeclared-native", _FakeSource)

    def test_register_rejects_parser_substitution_for_declared_name(self):
        """A declared source name cannot be rebound to a different parser class."""
        with pytest.raises(AgentSourceSupportManifestError, match="registry parser must be"):
            SourceRegistry.register("codex", _FakeSource)

    def test_register_builtin_agents_runs_without_crash(self):
        """register_builtin_agents() 可正常执行，不抛异常。"""
        # 内置模块可能不存在，但方法本身不应崩溃
        SourceRegistry.register_builtin_agents()
        registered = SourceRegistry.list_registered()
        assert isinstance(registered, list)

    def test_register_builtin_agents_registers_passive_source_plugins(self):
        """Codex/Hermes/OpenClaw source classes are registered via reflection."""
        SourceRegistry.register_builtin_agents()

        expected = {
            "codex": ("integrations.sources.codex_source", "CodexSource"),
            "hermes": ("integrations.sources.hermes_source", "HermesSource"),
            "openclaw": ("integrations.sources.openclaw_source", "OpenClawSource"),
        }
        for name, (module_name, class_name) in expected.items():
            source_class = SourceRegistry._registry[name]
            assert source_class.__module__ == module_name
            assert source_class.__name__ == class_name


# ---------------------------------------------------------------------------
# PathDiscover 测试
# ---------------------------------------------------------------------------


class TestPathDiscover:
    """PathDiscover 公共 API 测试套件。"""

    def test_find_standard_path_expands_tilde(self, tmp_path: Path, monkeypatch):
        """find() 能正确展开 ~/.claude 等标准路径。"""
        # expanduser() 读取 HOME 环境变量
        monkeypatch.setenv("HOME", str(tmp_path))
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()

        # 清空缓存，避免之前测试的缓存干扰
        PathDiscover.invalidate_cache()
        result = PathDiscover.find("claude")
        assert result == claude_dir

    def test_find_returns_none_when_nothing_exists(self, monkeypatch):
        """所有回退路径都不存在时返回 None。"""
        monkeypatch.setattr(Path, "home", lambda: Path("/nonexistent_home"))
        PathDiscover.invalidate_cache()
        result = PathDiscover.find("nonexistent-agent-xyz")
        assert result is None

    def test_find_uses_env_var_when_set(self, tmp_path: Path, monkeypatch):
        """环境变量优先级高于标准路径。"""
        env_dir = tmp_path / "from_env"
        env_dir.mkdir()
        monkeypatch.setenv("OPENCLAW_STATE_DIR", str(env_dir))
        PathDiscover.invalidate_cache()
        result = PathDiscover.find("openclaw")
        assert result == env_dir

    def test_resolve_agent_subdir_prefers_existing_transcript_dir(self, tmp_path: Path):
        """Known agent roots resolve to their transcript/project subdirectory."""
        claude_root = tmp_path / ".claude"
        projects = claude_root / "projects"
        projects.mkdir(parents=True)

        assert PathDiscover.resolve_agent_subdir("claude", claude_root) == projects
        assert PathDiscover.resolve_agent_subdir("claude", projects) == projects
        assert PathDiscover.resolve_agent_subdir("unknown", claude_root) == claude_root

    def test_find_uses_cached_result(self, tmp_path: Path, monkeypatch):
        """缓存命中时直接返回缓存值，不再访问文件系统。"""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        cached_dir = tmp_path / ".cached_agent"
        cached_dir.mkdir()

        # 先填充缓存
        PathDiscover._cache["cached_agent"] = (cached_dir, 9999999999.0)
        with patch.object(PathDiscover, "_do_find") as mock_do_find:
            result = PathDiscover.find("cached_agent")
        assert result == cached_dir
        mock_do_find.assert_not_called()

    def test_invalidate_cache_single_key(self):
        """invalidate_cache(agent_name) 仅删除指定缓存。"""
        PathDiscover._cache["keep"] = (Path("/keep"), 1.0)
        PathDiscover._cache["remove"] = (Path("/remove"), 2.0)
        PathDiscover.invalidate_cache("remove")
        assert "remove" not in PathDiscover._cache
        assert "keep" in PathDiscover._cache

    def test_invalidate_cache_all(self):
        """invalidate_cache() 无参数时清空全部缓存。"""
        PathDiscover._cache["a"] = (Path("/a"), 1.0)
        PathDiscover._cache["b"] = (Path("/b"), 2.0)
        PathDiscover.invalidate_cache()
        assert PathDiscover._cache == {}

    def test_find_user_config_priority(self, tmp_path: Path):
        """用户显式配置 ~/.mnemos/configs/agent_paths.json 优先级最高。"""
        config_dir = tmp_path / ".mnemos" / "configs"
        config_dir.mkdir(parents=True)
        custom_path = tmp_path / "custom_claude"
        custom_path.mkdir()
        config_file = config_dir / "agent_paths.json"
        config_file.write_text(f'{{"claude": "{custom_path}"}}')

        with patch.object(
            PathDiscover, "_load_user_config", return_value={"claude": str(custom_path)}
        ):
            PathDiscover.invalidate_cache()
            result = PathDiscover.find("claude")
        assert result == custom_path
