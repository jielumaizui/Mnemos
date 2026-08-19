# -*- coding: utf-8 -*-
"""ModuleRegistry tests."""

from __future__ import annotations

from types import SimpleNamespace

from core.pluggable import ModuleRegistry, PluggableModule


class FakeModule(PluggableModule):
    def __init__(self, module_id: str, events: list[str], fail_enable: bool = False):
        self.module_id = module_id
        self.events = events
        self.fail_enable = fail_enable
        self.config = {}
        self.enabled = False

    def enable(self) -> None:
        self.events.append(f"enable:{self.module_id}")
        if self.fail_enable:
            raise RuntimeError(f"boom:{self.module_id}")
        self.enabled = True

    def disable(self) -> None:
        self.events.append(f"disable:{self.module_id}")
        self.enabled = False

    def configure(self, cfg):
        self.config = dict(cfg)
        self.events.append(f"configure:{self.module_id}:{cfg.get('value', '')}")

    def handle_event(self, event_type, data):
        self.events.append(f"event:{self.module_id}:{event_type}")

    def health_check(self):
        return {"healthy": self.enabled, "module_id": self.module_id}


def test_registry_starts_enabled_modules_in_dependency_order():
    events: list[str] = []
    registry = ModuleRegistry()
    registry.register("core", lambda: FakeModule("core", events))
    registry.register("dependent", lambda: FakeModule("dependent", events), dependencies=["core"])
    registry.configure({"modules": {"dependent": {"value": "v"}}})

    status = registry.start_enabled()

    assert status["core"]["state"] == "running"
    assert status["dependent"]["state"] == "running"
    assert events == [
        "configure:core:",
        "enable:core",
        "configure:dependent:v",
        "enable:dependent",
    ]
    assert registry.health_check()["dependent"]["details"]["healthy"] is True


def test_registry_skips_disabled_dependencies_and_dependents():
    events: list[str] = []
    registry = ModuleRegistry()
    registry.register("core", lambda: FakeModule("core", events))
    registry.register("dependent", lambda: FakeModule("dependent", events), dependencies=["core"])
    registry.configure({"modules": {"core": {"enabled": False}}})

    status = registry.start_enabled()

    assert status["core"]["state"] == "disabled"
    assert status["dependent"]["state"] == "blocked"
    assert status["dependent"]["reason"] == "dependency core is disabled"
    assert events == []


def test_registry_isolates_start_failures_and_stops_reverse_order():
    events: list[str] = []
    registry = ModuleRegistry()
    registry.register("bad", lambda: FakeModule("bad", events, fail_enable=True))
    registry.register("ok", lambda: FakeModule("ok", events))

    status = registry.start_enabled()
    stop_status = registry.stop_all()

    assert status["bad"]["state"] == "failed"
    assert "boom:bad" in status["bad"]["error"]
    assert status["ok"]["state"] == "running"
    assert stop_status["ok"]["state"] == "stopped"
    assert events == [
        "configure:bad:",
        "enable:bad",
        "configure:ok:",
        "enable:ok",
        "disable:ok",
    ]


def test_registry_can_start_one_module_with_dependencies():
    events: list[str] = []
    registry = ModuleRegistry()
    registry.register("core", lambda: FakeModule("core", events))
    registry.register("dependent", lambda: FakeModule("dependent", events), dependencies=["core"])

    status = registry.start_module("dependent")

    assert status["dependent"]["state"] == "running"
    assert registry.get_instance("core") is not None
    assert registry.get_instance("dependent") is not None
    assert events == [
        "configure:core:",
        "enable:core",
        "configure:dependent:",
        "enable:dependent",
    ]


def test_registry_start_one_blocks_when_dependency_fails():
    events: list[str] = []
    registry = ModuleRegistry()
    registry.register("bad", lambda: FakeModule("bad", events, fail_enable=True))
    registry.register("dependent", lambda: FakeModule("dependent", events), dependencies=["bad"])

    status = registry.start_module("dependent")

    assert status["bad"]["state"] == "failed"
    assert status["dependent"]["state"] == "blocked"
    assert registry.get_instance("dependent") is None


def test_registry_event_bus_bridge_dispatches_to_running_modules():
    events: list[str] = []
    subscriptions = []
    registry = ModuleRegistry()
    registry.register("core", lambda: FakeModule("core", events))
    registry.start_enabled()

    class FakeBus:
        def subscribe(self, event_type, handler):
            subscriptions.append((event_type, handler))

    handler = registry.subscribe_to_event_bus(FakeBus())
    result = handler(
        SimpleNamespace(event_type="knowledge.ingested", payload={"page_path": "p.md"})
    )

    assert subscriptions == [("*", handler)]
    assert result == {"core": {"state": "handled"}}
    assert "event:core:knowledge.ingested" in events
