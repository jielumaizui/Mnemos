# -*- coding: utf-8 -*-
"""确认本轮审计删除的死代码/僵尸模块已彻底移除。"""

import pytest


class TestDeadCodeGone:
    @pytest.mark.parametrize(
        "module",
        [
            "core.scoring.clustering_engine",
            "core.hephaestus.deferred_distill",
            "core.hephaestus.incremental_distiller",
        ],
    )
    def test_dead_module_cannot_be_imported(self, module):
        with pytest.raises(ImportError):
            __import__(module)

    def test_evolution_signal_integration_removed(self):
        from core.hephaestus import evolution_tracker

        assert not hasattr(evolution_tracker, "EvolutionSignalIntegration")

    def test_hephaestus_package_no_dead_exports(self):
        import core.hephaestus as heph

        assert not hasattr(heph, "IncrementalDistiller")
        assert not hasattr(heph, "DeferredDistillationQueue")
        assert not hasattr(heph, "WikiIncrementalDistiller")
        assert not hasattr(heph, "FragmentationDetector")
        assert not hasattr(heph, "CrossPageDistiller")
        assert not hasattr(heph, "EvolutionSignalIntegration")

    def test_dead_config_keys_removed(self):
        """确认死配置键已从 DEFAULT_CONFIG 中移除。"""
        from core.config import DEFAULT_CONFIG

        scoring = DEFAULT_CONFIG.get("scoring", {})
        distill = DEFAULT_CONFIG.get("distill", {})
        persona = DEFAULT_CONFIG.get("persona_engine", {})
        assert "retrain_buffer" not in scoring
        assert "retrain_interval_seconds" not in scoring
        assert "single_threshold" not in distill
        assert "aggregate_threshold" not in distill
        assert "deferred_max_days" not in distill
        assert "similarity_dedup_threshold" not in distill
        assert "interest_decay_half_life_days" not in persona
