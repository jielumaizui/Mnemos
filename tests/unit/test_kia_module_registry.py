# -*- coding: utf-8 -*-
"""KIA module registry builder tests."""

from __future__ import annotations

from core.pluggable import PluggableModule


class FakeModule(PluggableModule):
    def __init__(self, module_id: str, events: list[str]):
        self.module_id = module_id
        self.events = events

    def enable(self) -> None:
        self.events.append(f"enable:{self.module_id}")

    def disable(self) -> None:
        self.events.append(f"disable:{self.module_id}")

    def configure(self, cfg):
        self.events.append(f"configure:{self.module_id}:{cfg.get('marker', '')}")

    def handle_event(self, event_type, data):
        self.events.append(f"event:{self.module_id}:{event_type}")


def test_kia_module_registry_registers_expected_modules():
    from core.kia.module_registry import KIA_MODULE_IDS, build_kia_module_registry

    registry = build_kia_module_registry(wiki_base="/tmp/wiki", dry_run=True)

    assert tuple(registry.status()) == KIA_MODULE_IDS


def test_kia_module_registry_starts_fake_modules_with_dependencies():
    from core.kia.module_registry import build_kia_module_registry

    events: list[str] = []
    factories = {
        module_id: (lambda module_id=module_id: FakeModule(module_id, events))
        for module_id in ("genos", "eris", "hygieia", "ixion", "stress_test")
    }
    registry = build_kia_module_registry(
        wiki_base="/tmp/wiki",
        dry_run=True,
        module_factories=factories,
        config={"modules": {"eris": {"marker": "entropy"}}},
    )

    status = registry.start_enabled()

    assert status["genos"]["state"] == "running"
    assert status["eris"]["state"] == "running"
    assert events.index("enable:genos") < events.index("enable:eris")
    assert "configure:eris:entropy" in events


def test_kia_module_registry_honors_disabled_config():
    from core.kia.module_registry import build_kia_module_registry

    events: list[str] = []
    factories = {
        module_id: (lambda module_id=module_id: FakeModule(module_id, events))
        for module_id in ("genos", "eris", "hygieia", "ixion", "stress_test")
    }
    registry = build_kia_module_registry(
        wiki_base="/tmp/wiki",
        module_factories=factories,
        config={"modules": {"genos": {"enabled": False}}},
    )

    status = registry.start_enabled()

    assert status["genos"]["state"] == "disabled"
    assert status["eris"]["state"] == "blocked"
    assert status["hygieia"]["state"] == "blocked"


def test_kia_module_registry_can_subscribe_to_event_bus():
    from core.kia.module_registry import build_kia_module_registry

    events: list[str] = []
    subscriptions = []
    factories = {
        module_id: (lambda module_id=module_id: FakeModule(module_id, events))
        for module_id in ("genos", "eris", "hygieia", "ixion", "stress_test")
    }
    registry = build_kia_module_registry(
        wiki_base="/tmp/wiki",
        dry_run=True,
        module_factories=factories,
    )
    registry.start_module("genos")

    class FakeBus:
        def subscribe(self, event_type, handler):
            subscriptions.append((event_type, handler))

    handler = registry.subscribe_to_event_bus(FakeBus())
    handler(type("Event", (), {"event_type": "knowledge.ingested", "payload": {}})())

    assert subscriptions == [("*", handler)]
    assert "event:genos:knowledge.ingested" in events
