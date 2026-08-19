# -*- coding: utf-8 -*-
"""Runtime registry builder for KIA pluggable modules."""

from __future__ import annotations

from typing import Callable, Dict, Any

from core.pluggable import ModuleRegistry, PluggableModule


KIA_MODULE_IDS = ("genos", "eris", "hygieia", "ixion", "stress_test")


def _default_factories(
    wiki_base: str | None = None,
    dry_run: bool = False,
) -> Dict[str, Callable[[], PluggableModule]]:
    return {
        "genos": lambda: _build_genos(wiki_base),
        "eris": lambda: _build_eris(wiki_base),
        "hygieia": lambda: _build_hygieia(wiki_base),
        "ixion": lambda: _build_ixion(wiki_base),
        "stress_test": lambda: _build_stress_test(wiki_base, dry_run),
    }


def _build_genos(wiki_base: str | None = None) -> PluggableModule:
    from core.kia.genos import DNAEngine

    return DNAEngine(wiki_base=wiki_base)


def _build_eris(wiki_base: str | None = None) -> PluggableModule:
    from core.kia.eris import EntropyEngine

    return EntropyEngine(wiki_base=wiki_base)


def _build_hygieia(wiki_base: str | None = None) -> PluggableModule:
    from core.kia.hygieia import KnowledgeImmuneSystem

    return KnowledgeImmuneSystem(wiki_base=wiki_base)


def _build_ixion(wiki_base: str | None = None) -> PluggableModule:
    from core.kia.ixion import CognitiveDecisionFlywheel

    return CognitiveDecisionFlywheel(wiki_base=wiki_base)


def _build_stress_test(wiki_base: str | None = None, dry_run: bool = False) -> PluggableModule:
    from core.kia.stress_test import StressTestEngine

    return StressTestEngine(wiki_base=wiki_base, dry_run=dry_run)


def build_kia_module_registry(
    *,
    wiki_base: str | None = None,
    dry_run: bool = False,
    config: Dict[str, Any] | None = None,
    module_factories: Dict[str, Callable[[], PluggableModule]] | None = None,
) -> ModuleRegistry:
    """Build the runtime registry for KIA pluggable modules."""
    factories = _default_factories(wiki_base=wiki_base, dry_run=dry_run)
    if module_factories:
        factories.update(module_factories)

    registry = ModuleRegistry()
    registry.register("genos", factories["genos"])
    registry.register("eris", factories["eris"], dependencies=["genos"])
    registry.register("hygieia", factories["hygieia"], dependencies=["genos", "eris"])
    registry.register("ixion", factories["ixion"])
    registry.register("stress_test", factories["stress_test"])
    registry.configure(config or {})
    return registry
