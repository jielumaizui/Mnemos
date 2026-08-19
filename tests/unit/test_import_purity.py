# -*- coding: utf-8 -*-
"""Import-time purity checks for modules with historically eager config access."""

from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace


_MISSING_BINDING = object()


def _capture_parent_bindings(module_names):
    """Snapshot package attributes that import machinery may replace."""

    bindings = []
    seen = set()
    for module_name in module_names:
        parts = module_name.split(".")
        for index in range(1, len(parts)):
            parent_name = ".".join(parts[:index])
            child_name = parts[index]
            key = (parent_name, child_name)
            parent = sys.modules.get(parent_name)
            if parent is None or key in seen:
                continue
            seen.add(key)
            bindings.append(
                (parent, child_name, getattr(parent, child_name, _MISSING_BINDING))
            )
    return bindings


def _restore_parent_bindings(bindings):
    for parent, child_name, value in reversed(bindings):
        if value is _MISSING_BINDING:
            parent.__dict__.pop(child_name, None)
        else:
            setattr(parent, child_name, value)


def test_embedding_cache_import_does_not_read_config(monkeypatch):
    import core.config as config_module

    calls = []

    def fail_get_config():
        calls.append("get_config")
        raise AssertionError("get_config should not run during import")

    monkeypatch.setattr(config_module, "get_config", fail_get_config)
    module_name = "core.embeddings.cache"
    bindings = _capture_parent_bindings((module_name,))
    saved = sys.modules.pop(module_name, None)

    try:
        importlib.import_module(module_name)
    finally:
        sys.modules.pop(module_name, None)
        if saved is not None:
            sys.modules[module_name] = saved
        _restore_parent_bindings(bindings)

    assert calls == []


def test_mnemos_bus_import_does_not_read_config(monkeypatch):
    import core.config as config_module

    calls = []

    def fail_get_config():
        calls.append("get_config")
        raise AssertionError("get_config should not run during import")

    monkeypatch.setattr(config_module, "get_config", fail_get_config)
    module_name = "core.mnemos_bus"
    bindings = _capture_parent_bindings((module_name,))
    saved = sys.modules.pop(module_name, None)

    try:
        importlib.import_module(module_name)
    finally:
        sys.modules.pop(module_name, None)
        if saved is not None:
            sys.modules[module_name] = saved
        _restore_parent_bindings(bindings)

    assert calls == []


def test_mnemos_daemon_import_does_not_read_config(monkeypatch):
    import core.config as config_module

    calls = []

    def fail_get_config():
        calls.append("get_config")
        raise AssertionError("get_config should not run during import")

    monkeypatch.setattr(config_module, "get_config", fail_get_config)
    module_name = "mnemos_daemon"
    bindings = _capture_parent_bindings((module_name,))
    saved = sys.modules.pop(module_name, None)

    try:
        importlib.import_module(module_name)
    finally:
        sys.modules.pop(module_name, None)
        if saved is not None:
            sys.modules[module_name] = saved
        _restore_parent_bindings(bindings)

    assert calls == []


def test_mnemos_cli_import_does_not_load_command_handlers():
    """验证 import mnemos_cli 不触发命令处理器模块的加载。

    隔离策略：
      1. 弹出待测模块（并保存原始对象），
      2. 记录 import 前快照，
      3. 执行断言，
      4. finally 里清掉所有新增模块，再把原始对象还原到 sys.modules，
         保证后续测试拿到的是与本测试前完全相同的 sys.modules 状态。
    """
    _PROBE_MODULES = (
        "mnemos_cli",
        "core.cli.commands",
        "core.cli.commands.doctor",
        "core.cli.commands.distill",
        "core.cli.commands.events",
    )
    # 保存原始对象并弹出
    _bindings = _capture_parent_bindings(_PROBE_MODULES)
    _saved = {m: sys.modules.pop(m) for m in _PROBE_MODULES if m in sys.modules}

    modules_before = set(sys.modules.keys())
    try:
        importlib.import_module("mnemos_cli")

        assert "core.cli.commands.doctor" not in sys.modules
        assert "core.cli.commands.distill" not in sys.modules
        assert "core.cli.commands.events" not in sys.modules
    finally:
        # 清掉所有因本次 import 新进入 sys.modules 的模块
        new_modules = set(sys.modules.keys()) - modules_before
        for m in new_modules:
            sys.modules.pop(m, None)
        # 还原原始对象，保持 sys.modules 与进入本测试前一致
        sys.modules.update(_saved)
        _restore_parent_bindings(_bindings)


def test_application_facade_import_does_not_load_integration_adapters():
    _PROBE_MODULES = (
        "core.application.facade",
        "integrations.oracle",
        "integrations.backends.obsidian_backend",
    )
    _bindings = _capture_parent_bindings(_PROBE_MODULES)
    _saved = {m: sys.modules.pop(m) for m in _PROBE_MODULES if m in sys.modules}

    modules_before = set(sys.modules.keys())
    try:
        importlib.import_module("core.application.facade")

        assert "integrations.oracle" not in sys.modules
        assert "integrations.backends.obsidian_backend" not in sys.modules
    finally:
        new_modules = set(sys.modules.keys()) - modules_before
        for module_name in new_modules:
            sys.modules.pop(module_name, None)
        sys.modules.update(_saved)
        _restore_parent_bindings(_bindings)


def test_embedding_cache_default_path_accepts_injected_config(tmp_path):
    from core.embeddings.cache import EmbeddingCache

    cfg = SimpleNamespace(database_dir=tmp_path / "db")
    cache = EmbeddingCache(config=cfg, model_version="test-model")

    assert cache.db_path == tmp_path / "db" / "embedding_cache.db"
