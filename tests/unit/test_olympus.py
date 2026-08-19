"""
Olympus 模块单元测试

覆盖项：
- AgentAdapter(ABC) 抽象基类接口契约
- AgentRegistry 注册/获取/列出/发现/优先级/重复注册
- AgentAdapter 可选方法的默认行为
- 环境变量与模块导入隔离

测试策略：
- 创建 FakeAdapter 具体子类来测试抽象基类契约
- monkeypatch 隔离模块导入和环境变量
- tmp_path 做文件系统隔离
- 每个测试独立，避免 _adapters 状态泄漏
"""

import sys
import types
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from integrations.olympus import AgentAdapter, AgentRegistry

# ============================================================================
# FakeAdapter — 用于测试 AgentAdapter 抽象基类的具体子类
# ============================================================================


class FakeAdapter(AgentAdapter):
    """完全实现所有抽象方法的最小适配器。"""

    def __init__(self, name: str = "fake", priority: int = 99, available: bool = True):
        self._name = name
        self._priority = priority
        self._available = available

    @property
    def name(self) -> str:
        return self._name

    @property
    def priority(self) -> int:
        return self._priority

    def is_available(self) -> bool:
        return self._available

    def on_session_start(self, working_dir: str, user_message: str = "") -> Dict[str, Any]:
        return {"session_id": "s1", "signals": []}

    def on_session_end(
        self, working_dir: str, session_messages: List[Dict] = None
    ) -> Dict[str, Any]:
        return {"queued": True, "distill_task_id": "d1"}

    def install_hooks(self) -> bool:
        return True

    def collect_signals(self, days: int = 7) -> List[Dict]:
        return [{"day": days}]

    def inject_knowledge(
        self, task_type: str, subtype: str = "", context_text: str = ""
    ) -> Dict[str, Any]:
        return {"injected": True, "task_type": task_type}


class MinimalAdapter(AgentAdapter):  # noqa
    """仅实现抽象方法，不覆盖任何可选方法。"""

    @property
    def name(self) -> str:
        return "minimal"

    @property
    def priority(self) -> int:
        return 10

    def is_available(self) -> bool:
        return True

    def on_session_start(self, working_dir: str, user_message: str = "") -> Dict[str, Any]:
        return {}

    def on_session_end(
        self, working_dir: str, session_messages: List[Dict] = None
    ) -> Dict[str, Any]:
        return {}

    def install_hooks(self) -> bool:
        return False

    def collect_signals(self, days: int = 7) -> List[Dict]:
        return []

    def inject_knowledge(
        self, task_type: str, subtype: str = "", context_text: str = ""
    ) -> Dict[str, Any]:
        return {}


# ============================================================================
# Fixture
# ============================================================================


@pytest.fixture(autouse=True)  # noqa
def reset_registry():
    """每个测试前清空 AgentRegistry._adapters，防止状态泄漏。"""
    AgentRegistry._adapters.clear()
    yield
    AgentRegistry._adapters.clear()


# ============================================================================
# AgentAdapter 抽象基类测试
# ============================================================================


class TestAgentAdapterInterface:
    """测试抽象基类的接口契约与默认实现。"""

    def test_cannot_instantiate_abc_directly(self):
        """AgentAdapter 是抽象基类，不能直接实例化。"""
        with pytest.raises(TypeError):
            AgentAdapter()

    def test_fake_adapter_can_instantiate(self):
        """FakeAdapter 实现所有抽象方法，应可正常实例化。"""
        adapter = FakeAdapter()
        assert adapter.name == "fake"
        assert adapter.priority == 99

    def test_name_property_is_abstract(self):
        """未实现 name 的子类不能实例化。"""

        class BadAdapter(AgentAdapter):
            @property
            def priority(self) -> int:
                return 1

            def is_available(self) -> bool:
                return True

            def on_session_start(self, working_dir: str, user_message: str = "") -> Dict[str, Any]:
                return {}

            def on_session_end(
                self, working_dir: str, session_messages: List[Dict] = None
            ) -> Dict[str, Any]:
                return {}

            def install_hooks(self) -> bool:
                return False

            def collect_signals(self, days: int = 7) -> List[Dict]:
                return []

            def inject_knowledge(
                self, task_type: str, subtype: str = "", context_text: str = ""
            ) -> Dict[str, Any]:
                return {}

        with pytest.raises(TypeError):
            BadAdapter()

    def test_priority_property_is_abstract(self):
        """未实现 priority 的子类不能实例化。"""

        class BadAdapter(AgentAdapter):
            @property
            def name(self) -> str:
                return "bad"

            def is_available(self) -> bool:
                return True

            def on_session_start(self, working_dir: str, user_message: str = "") -> Dict[str, Any]:
                return {}

            def on_session_end(
                self, working_dir: str, session_messages: List[Dict] = None
            ) -> Dict[str, Any]:
                return {}

            def install_hooks(self) -> bool:
                return False

            def collect_signals(self, days: int = 7) -> List[Dict]:
                return []

            def inject_knowledge(
                self, task_type: str, subtype: str = "", context_text: str = ""
            ) -> Dict[str, Any]:
                return {}

        with pytest.raises(TypeError):
            BadAdapter()

    def test_all_abstract_methods_must_be_implemented(self):
        """只实现部分抽象方法仍不能实例化。"""

        class PartialAdapter(AgentAdapter):
            @property
            def name(self) -> str:
                return "partial"

            @property
            def priority(self) -> int:
                return 1

            def is_available(self) -> bool:
                return True

        with pytest.raises(TypeError):
            PartialAdapter()

    def test_get_config_path_default(self):
        """默认 get_config_path 返回 None。"""
        adapter = FakeAdapter()
        assert adapter.get_config_path() is None

    def test_get_data_dir_default(self):
        """默认 get_data_dir 返回 None。"""
        adapter = FakeAdapter()
        assert adapter.get_data_dir() is None

    def test_is_hooks_installed_default(self):
        """默认 is_hooks_installed 返回 False。"""
        adapter = FakeAdapter()
        assert adapter.is_hooks_installed() is False

    def test_install_mcp_server_default(self):
        """默认 install_mcp_server 返回 False。"""
        adapter = FakeAdapter()
        assert adapter.install_mcp_server() is False

    def test_is_mcp_configured_default(self):
        """默认 is_mcp_configured 返回 False。"""
        adapter = FakeAdapter()
        assert adapter.is_mcp_configured() is False

    def test_install_active_policy_import_error(self, monkeypatch):
        """当 integrations.active 不存在时，install_active_policy 返回 False 且不抛异常。"""
        adapter = FakeAdapter()
        # 确保 import 会失败
        monkeypatch.setitem(sys.modules, "integrations.active", None)
        # 通过删除模块让 import 触发 ImportError
        # 但 None 在 sys.modules 中已经会让 from ... import 失败
        result = adapter.install_active_policy()
        assert result is False

    def test_is_active_policy_installed_import_error(self, monkeypatch):
        """当 integrations.active 不存在时，is_active_policy_installed 返回 False。"""
        adapter = FakeAdapter()
        monkeypatch.setitem(sys.modules, "integrations.active", None)
        result = adapter.is_active_policy_installed()
        assert result is False

    def test_is_active_connection_installed_all_false(self):
        """当 hooks、mcp、policy 都未安装时，is_active_connection_installed 返回 False。"""
        adapter = FakeAdapter()
        assert adapter.is_active_connection_installed() is False

    def test_is_active_connection_installed_all_true(self, monkeypatch):
        """当 hooks、mcp、policy 都安装时，is_active_connection_installed 返回 True。"""
        adapter = FakeAdapter()
        monkeypatch.setattr(adapter, "is_hooks_installed", lambda: True)
        monkeypatch.setattr(adapter, "is_mcp_configured", lambda: True)
        monkeypatch.setattr(adapter, "is_active_policy_installed", lambda: True)
        assert adapter.is_active_connection_installed() is True

    def test_is_active_connection_installed_partial(self, monkeypatch):
        """仅部分就绪时返回 False。"""
        adapter = FakeAdapter()
        monkeypatch.setattr(adapter, "is_hooks_installed", lambda: True)
        monkeypatch.setattr(adapter, "is_mcp_configured", lambda: True)
        monkeypatch.setattr(adapter, "is_active_policy_installed", lambda: False)
        assert adapter.is_active_connection_installed() is False

    def test_on_session_start_returns_dict(self):
        """on_session_start 应返回字典。"""
        adapter = FakeAdapter()
        result = adapter.on_session_start("/tmp", "hello")
        assert isinstance(result, dict)
        assert result["session_id"] == "s1"

    def test_on_session_end_returns_dict(self):
        """on_session_end 应返回字典。"""
        adapter = FakeAdapter()
        result = adapter.on_session_end("/tmp", [{"role": "user", "content": "hi"}])
        assert isinstance(result, dict)
        assert result["queued"] is True

    def test_collect_signals_accepts_days_param(self):
        """collect_signals 应接受 days 参数并返回列表。"""
        adapter = FakeAdapter()
        result = adapter.collect_signals(days=3)
        assert isinstance(result, list)
        assert result[0]["day"] == 3

    def test_inject_knowledge_accepts_params(self):
        """inject_knowledge 应接受所有参数并返回字典。"""
        adapter = FakeAdapter()
        result = adapter.inject_knowledge("coding", subtype="debug", context_text="ctx")
        assert isinstance(result, dict)
        assert result["task_type"] == "coding"

    def test_subclass_can_override_optional_methods(self, tmp_path):
        """子类可以覆盖可选方法。"""

        class CustomAdapter(FakeAdapter):
            def get_config_path(self) -> Optional[Path]:
                return Path("/custom/config.json")

            def is_hooks_installed(self) -> bool:
                return True

        adapter = CustomAdapter()
        assert adapter.get_config_path() == Path("/custom/config.json")
        assert adapter.is_hooks_installed() is True


# ============================================================================
# AgentRegistry 测试
# ============================================================================


class TestAgentRegistry:
    """测试 AgentRegistry 的注册、发现、获取、排序等行为。"""

    def test_register_adds_class(self):
        """register 应将适配器类加入 _adapters。"""
        AgentRegistry.register(FakeAdapter)
        assert FakeAdapter in AgentRegistry._adapters

    def test_register_returns_class(self):
        """register 应返回被注册的类本身（支持装饰器语法）。"""
        result = AgentRegistry.register(FakeAdapter)
        assert result is FakeAdapter

    def test_register_duplicate_allowed(self):
        """重复注册同一类不会报错，_adapters 会出现重复。"""
        AgentRegistry.register(FakeAdapter)
        AgentRegistry.register(FakeAdapter)
        assert AgentRegistry._adapters.count(FakeAdapter) == 2

    def test_discover_all_returns_available_instances(self):
        """discover_all 应返回可用适配器实例列表，按优先级排序。"""
        AgentRegistry.register(FakeAdapter)
        monkeypatch_local = pytest.MonkeyPatch()
        # 阻止 _ensure_adapters_loaded 做实际 import
        monkeypatch_local.setattr(
            AgentRegistry, "_ensure_adapters_loaded", classmethod(lambda cls: None)
        )
        result = AgentRegistry.discover_all()
        assert len(result) == 1
        assert isinstance(result[0], FakeAdapter)
        assert result[0].name == "fake"
        monkeypatch_local.undo()

    def test_discover_all_skips_unavailable(self):
        """discover_all 应跳过 is_available=False 的适配器。"""

        class UnavailableAdapter(FakeAdapter):
            def __init__(self):
                super().__init__(name="unavailable", available=False)

        AgentRegistry.register(UnavailableAdapter)
        monkeypatch_local = pytest.MonkeyPatch()
        monkeypatch_local.setattr(
            AgentRegistry, "_ensure_adapters_loaded", classmethod(lambda cls: None)
        )
        result = AgentRegistry.discover_all()
        assert len(result) == 0
        monkeypatch_local.undo()

    def test_discover_all_sorts_by_priority(self):
        """discover_all 应按 priority 升序排序。"""

        class HighPriority(FakeAdapter):
            def __init__(self):
                super().__init__(name="high", priority=1)

        class LowPriority(FakeAdapter):
            def __init__(self):
                super().__init__(name="low", priority=10)

        AgentRegistry.register(LowPriority)
        AgentRegistry.register(HighPriority)
        monkeypatch_local = pytest.MonkeyPatch()
        monkeypatch_local.setattr(
            AgentRegistry, "_ensure_adapters_loaded", classmethod(lambda cls: None)
        )
        result = AgentRegistry.discover_all()
        assert len(result) == 2
        assert result[0].name == "high"
        assert result[1].name == "low"
        monkeypatch_local.undo()

    def test_discover_all_ignores_exceptions(self):
        """实例化抛出异常时不应中断整个发现流程。"""

        class BrokenAdapter(AgentAdapter):
            @property
            def name(self) -> str:
                return "broken"

            @property
            def priority(self) -> int:
                return 1

            def __init__(self):
                raise RuntimeError("boom")

            def is_available(self) -> bool:
                return True

            def on_session_start(self, working_dir: str, user_message: str = "") -> Dict[str, Any]:
                return {}

            def on_session_end(
                self, working_dir: str, session_messages: List[Dict] = None
            ) -> Dict[str, Any]:
                return {}

            def install_hooks(self) -> bool:
                return False

            def collect_signals(self, days: int = 7) -> List[Dict]:
                return []

            def inject_knowledge(
                self, task_type: str, subtype: str = "", context_text: str = ""
            ) -> Dict[str, Any]:
                return {}

        AgentRegistry.register(BrokenAdapter)
        AgentRegistry.register(FakeAdapter)
        monkeypatch_local = pytest.MonkeyPatch()
        monkeypatch_local.setattr(
            AgentRegistry, "_ensure_adapters_loaded", classmethod(lambda cls: None)
        )
        result = AgentRegistry.discover_all()
        assert len(result) == 1
        assert result[0].name == "fake"
        monkeypatch_local.undo()

    def test_get_adapter_by_name(self):
        """get_adapter 应通过 name 返回对应实例。"""
        AgentRegistry.register(FakeAdapter)
        monkeypatch_local = pytest.MonkeyPatch()
        monkeypatch_local.setattr(
            AgentRegistry, "_ensure_adapters_loaded", classmethod(lambda cls: None)
        )
        result = AgentRegistry.get_adapter("fake")
        assert result is not None
        assert result.name == "fake"
        monkeypatch_local.undo()

    def test_get_adapter_case_insensitive(self):
        """get_adapter 应大小写不敏感匹配 name。"""
        AgentRegistry.register(FakeAdapter)
        monkeypatch_local = pytest.MonkeyPatch()
        monkeypatch_local.setattr(
            AgentRegistry, "_ensure_adapters_loaded", classmethod(lambda cls: None)
        )
        result = AgentRegistry.get_adapter("FAKE")
        assert result is not None
        assert result.name == "fake"
        monkeypatch_local.undo()

    def test_get_adapter_not_found(self):
        """get_adapter 在找不到时返回 None。"""
        monkeypatch_local = pytest.MonkeyPatch()
        monkeypatch_local.setattr(
            AgentRegistry, "_ensure_adapters_loaded", classmethod(lambda cls: None)
        )
        result = AgentRegistry.get_adapter("nonexistent")
        assert result is None
        monkeypatch_local.undo()

    def test_get_adapter_skips_unavailable_by_default(self):
        """默认情况下 get_adapter 跳过不可用的适配器。"""

        class UnavailableAdapter(FakeAdapter):
            def __init__(self):
                super().__init__(name="unavailable", available=False)

        AgentRegistry.register(UnavailableAdapter)
        monkeypatch_local = pytest.MonkeyPatch()
        monkeypatch_local.setattr(
            AgentRegistry, "_ensure_adapters_loaded", classmethod(lambda cls: None)
        )
        result = AgentRegistry.get_adapter("unavailable")
        assert result is None
        monkeypatch_local.undo()

    def test_get_adapter_include_unavailable(self):
        """include_unavailable=True 时返回不可用适配器。"""

        class UnavailableAdapter(FakeAdapter):
            def __init__(self):
                super().__init__(name="unavailable", available=False)

        AgentRegistry.register(UnavailableAdapter)
        monkeypatch_local = pytest.MonkeyPatch()
        monkeypatch_local.setattr(
            AgentRegistry, "_ensure_adapters_loaded", classmethod(lambda cls: None)
        )
        result = AgentRegistry.get_adapter("unavailable", include_unavailable=True)
        assert result is not None
        assert result.name == "unavailable"
        monkeypatch_local.undo()

    def test_get_adapter_ignores_exceptions(self):
        """get_adapter 在实例化异常时应继续扫描并返回 None。"""

        class BrokenAdapter(AgentAdapter):
            @property
            def name(self) -> str:
                return "broken"

            @property
            def priority(self) -> int:
                return 1

            def __init__(self):
                raise RuntimeError("boom")

            def is_available(self) -> bool:
                return True

            def on_session_start(self, working_dir: str, user_message: str = "") -> Dict[str, Any]:
                return {}

            def on_session_end(
                self, working_dir: str, session_messages: List[Dict] = None
            ) -> Dict[str, Any]:
                return {}

            def install_hooks(self) -> bool:
                return False

            def collect_signals(self, days: int = 7) -> List[Dict]:
                return []

            def inject_knowledge(
                self, task_type: str, subtype: str = "", context_text: str = ""
            ) -> Dict[str, Any]:
                return {}

        AgentRegistry.register(BrokenAdapter)
        monkeypatch_local = pytest.MonkeyPatch()
        monkeypatch_local.setattr(
            AgentRegistry, "_ensure_adapters_loaded", classmethod(lambda cls: None)
        )
        result = AgentRegistry.get_adapter("broken")
        assert result is None
        monkeypatch_local.undo()

    def test_ensure_adapters_loaded_imports_modules(self, monkeypatch):
        """_ensure_adapters_loaded 应尝试导入所有适配器模块。"""
        imported = []

        def fake_import(name, *args):
            imported.append(name)
            # 模拟模块不存在时的 ImportError
            if "nonexistent" in name:
                raise ImportError(f"No module named {name}")
            # 返回一个假模块，由 monkeypatch 自动清理
            mod = types.ModuleType(name)
            monkeypatch.setitem(sys.modules, name, mod)
            return mod

        monkeypatch.setattr("builtins.__import__", fake_import)
        AgentRegistry._ensure_adapters_loaded()
        expected = [
            "integrations.apollon",
            "integrations.kimi_adapter",
            "integrations.crush_adapter",
        ]
        for mod in expected:
            assert mod in imported, f"{mod} 未被导入"

    def test_ensure_adapters_loaded_graceful_on_import_error(self, monkeypatch):
        """模块导入失败时不应抛异常。"""

        def fake_import(name, *args):
            raise ImportError(f"No module named {name}")

        monkeypatch.setattr("builtins.__import__", fake_import)
        # 不应抛异常
        AgentRegistry._ensure_adapters_loaded()

    def test_crush_adapter_prefers_project_local_db(self, tmp_path, monkeypatch):
        from integrations.crush_adapter import CrushAdapter

        project = tmp_path / "project"
        db_dir = project / ".crush"
        db_dir.mkdir(parents=True)
        (db_dir / "crush.db").write_text("", encoding="utf-8")
        monkeypatch.chdir(project)
        monkeypatch.delenv("CRUSH_HOME", raising=False)
        monkeypatch.delenv("CRUSH_DATA_DIR", raising=False)

        assert CrushAdapter().get_data_dir() == db_dir

    def test_crush_adapter_uses_env_data_dir(self, tmp_path, monkeypatch):
        from integrations.crush_adapter import CrushAdapter

        env_dir = tmp_path / "crush-env"
        env_dir.mkdir()
        (env_dir / "crush.db").write_text("", encoding="utf-8")
        monkeypatch.setenv("CRUSH_DATA_DIR", str(env_dir))
        monkeypatch.delenv("CRUSH_HOME", raising=False)

        assert CrushAdapter().get_data_dir() == env_dir

    def test_kimi_adapter_prefers_kimi_code_home(self, tmp_path, monkeypatch):
        from integrations.kimi_adapter import KimiAdapter

        kimi_code = tmp_path / ".kimi-code"
        kimi_code.mkdir()
        monkeypatch.setenv("KIMI_CODE_HOME", str(kimi_code))
        monkeypatch.delenv("KIMI_HOME", raising=False)

        assert KimiAdapter().get_data_dir() == kimi_code

    def test_kimi_adapter_collects_official_wire_session(self, tmp_path, monkeypatch):
        from integrations.kimi_adapter import KimiAdapter

        kimi_code = tmp_path / ".kimi-code"
        wire = (
            kimi_code
            / "sessions"
            / "work-key"
            / "session-1"
            / "agents"
            / "main"
            / "wire.jsonl"
        )
        wire.parent.mkdir(parents=True)
        wire.write_text(
            json.dumps(
                {
                    "message": {
                        "type": "TurnBegin",
                        "payload": {"user_input": [{"type": "text", "text": "hello"}]},
                    }
                }
            )
            + "\n"
            + json.dumps(
                {
                    "message": {
                        "type": "ContentPart",
                        "payload": {"type": "text", "text": "world"},
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("KIMI_CODE_HOME", str(kimi_code))
        monkeypatch.delenv("KIMI_HOME", raising=False)

        signals = KimiAdapter().collect_signals(days=3650)

        assert signals[0]["session_id"].startswith("session-1::main_wire::")
        assert signals[0]["native_session_id"] == "session-1"
        assert signals[0]["source_kind"] == "main_wire"
        assert signals[0]["source_artifact_id"].startswith("kimi-artifact-")
        assert signals[0]["parent_session_id"] == "session-1"
        assert signals[0]["canonical_parent_session_id"].startswith(
            "session-1::main_context::"
        )
        assert signals[0]["parent_source_artifact_id"].startswith("kimi-artifact-")
        assert signals[0]["identity_contract_version"] == "kimi-native-artifact-v2"
        assert signals[0]["messages"] == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ]

    def test_get_host_agent_with_env(self, monkeypatch):
        """MNEMOS_HOST_AGENT 设置时返回对应适配器。"""
        monkeypatch.setenv("MNEMOS_HOST_AGENT", "fake")
        AgentRegistry.register(FakeAdapter)
        monkeypatch_local = pytest.MonkeyPatch()
        monkeypatch_local.setattr(
            AgentRegistry, "_ensure_adapters_loaded", classmethod(lambda cls: None)
        )
        result = AgentRegistry.get_host_agent()
        assert result is not None
        assert result.name == "fake"
        monkeypatch_local.undo()

    def test_get_host_agent_no_env(self, monkeypatch):
        """MNEMOS_HOST_AGENT 未设置时返回 None。"""
        monkeypatch.delenv("MNEMOS_HOST_AGENT", raising=False)
        monkeypatch_local = pytest.MonkeyPatch()
        monkeypatch_local.setattr(
            AgentRegistry, "_ensure_adapters_loaded", classmethod(lambda cls: None)
        )
        result = AgentRegistry.get_host_agent()
        assert result is None
        monkeypatch_local.undo()

    def test_get_host_agent_env_not_found(self, monkeypatch):
        """MNEMOS_HOST_AGENT 指向不存在的适配器时返回 None。"""
        monkeypatch.setenv("MNEMOS_HOST_AGENT", "ghost")
        monkeypatch_local = pytest.MonkeyPatch()
        monkeypatch_local.setattr(
            AgentRegistry, "_ensure_adapters_loaded", classmethod(lambda cls: None)
        )
        result = AgentRegistry.get_host_agent()
        assert result is None
        monkeypatch_local.undo()

    def test_select_best_agent_prefers_host(self, monkeypatch):
        """select_best_agent 优先返回宿主 Agent。"""
        monkeypatch.setenv("MNEMOS_HOST_AGENT", "fake")
        AgentRegistry.register(FakeAdapter)
        monkeypatch_local = pytest.MonkeyPatch()
        monkeypatch_local.setattr(
            AgentRegistry, "_ensure_adapters_loaded", classmethod(lambda cls: None)
        )
        result = AgentRegistry.select_best_agent()
        assert result is not None
        assert result.name == "fake"
        monkeypatch_local.undo()

    def test_select_best_agent_fallback_to_priority(self, monkeypatch):
        """无宿主时按优先级返回第一个可用适配器。"""
        monkeypatch.delenv("MNEMOS_HOST_AGENT", raising=False)

        class HighPriority(FakeAdapter):
            def __init__(self):
                super().__init__(name="high", priority=1)

        class LowPriority(FakeAdapter):
            def __init__(self):
                super().__init__(name="low", priority=10)

        AgentRegistry.register(LowPriority)
        AgentRegistry.register(HighPriority)
        monkeypatch_local = pytest.MonkeyPatch()
        monkeypatch_local.setattr(
            AgentRegistry, "_ensure_adapters_loaded", classmethod(lambda cls: None)
        )
        result = AgentRegistry.select_best_agent()
        assert result is not None
        assert result.name == "high"
        monkeypatch_local.undo()

    def test_select_best_agent_no_adapters(self, monkeypatch):
        """无任何可用适配器时返回 None。"""
        monkeypatch.delenv("MNEMOS_HOST_AGENT", raising=False)
        monkeypatch_local = pytest.MonkeyPatch()
        monkeypatch_local.setattr(
            AgentRegistry, "_ensure_adapters_loaded", classmethod(lambda cls: None)
        )
        result = AgentRegistry.select_best_agent()
        assert result is None
        monkeypatch_local.undo()

    def test_registry_state_isolated_per_test(self):
        """reset_registry fixture 应确保每个测试的注册表是干净的。"""
        assert AgentRegistry._adapters == []
