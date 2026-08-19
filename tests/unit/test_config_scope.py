from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier


def test_config_scope_is_nested_and_restores_global_config(monkeypatch):
    import core.config as config_module
    from core.ops.config_scope import use_config

    global_config = object()
    outer = object()
    inner = object()
    monkeypatch.setattr(config_module, "_config", global_config)

    assert config_module.get_config() is global_config
    with use_config(outer):
        assert config_module.get_config() is outer
        with use_config(inner):
            assert config_module.get_config() is inner
        assert config_module.get_config() is outer
    assert config_module.get_config() is global_config


def test_config_scope_isolated_between_threads(monkeypatch):
    import core.config as config_module
    from core.ops.config_scope import use_config

    global_config = object()
    first = object()
    second = object()
    barrier = Barrier(2)
    monkeypatch.setattr(config_module, "_config", global_config)

    def resolve(scoped):
        with use_config(scoped):
            barrier.wait(timeout=5)
            return config_module.get_config()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(resolve, (first, second)))

    assert results == [first, second]
    assert config_module.get_config() is global_config
