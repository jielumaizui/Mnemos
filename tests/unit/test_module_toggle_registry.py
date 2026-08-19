from core.module_toggles import (
    MODULE_TOGGLE_DEFINITIONS,
    REQUIRED_TOGGLE_KEYS,
    STALE_TOGGLE_KEYS,
    audit_cold_start_toggle_matrix,
    audit_module_toggle_registry,
    build_module_toggle_health,
    build_toggle_matrix,
)


class MappingConfig:
    def __init__(self, values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


def test_required_and_stale_toggles_are_registered():
    assert REQUIRED_TOGGLE_KEYS <= set(MODULE_TOGGLE_DEFINITIONS)
    assert STALE_TOGGLE_KEYS <= set(MODULE_TOGGLE_DEFINITIONS)
    assert audit_module_toggle_registry(strict=True) == []


def test_default_disabled_toggles_have_governed_states():
    assert audit_cold_start_toggle_matrix(strict=True) == []
    for key, definition in MODULE_TOGGLE_DEFINITIONS.items():
        if definition.default_enabled:
            continue
        assert definition.default_state in {
            "disabled_default",
            "disabled_cold_start",
            "registered_but_unwired",
            "stale_removed",
        }, key
        assert definition.default_reason


def test_stale_toggles_cannot_auto_enable():
    for key in STALE_TOGGLE_KEYS:
        definition = MODULE_TOGGLE_DEFINITIONS[key]
        assert definition.stale is True
        assert definition.auto_enable_allowed is False
        assert definition.default_state == "stale_removed"


def test_build_toggle_matrix_reflects_runtime_config_override():
    matrix = build_toggle_matrix(
        MappingConfig(
            {
                "features.enable_link_probe": True,
                "daemon.services.link_probe": True,
            }
        )
    )

    assert matrix["features.enable_link_probe"]["current_enabled"] is True
    assert matrix["daemon.services.link_probe"]["current_enabled"] is True
    assert matrix["features.enable_link_probe"]["runtime_state"] == "manual_enabled"


def test_module_toggle_health_exposes_counts_and_unwired_items():
    health = build_module_toggle_health()
    assert health["status"] == "ok"
    assert health["counts"]["toggles"] == len(MODULE_TOGGLE_DEFINITIONS)
    assert "scoring.clustering.enabled" in health["registered_but_unwired"]
    assert "persona.data_sources.memos.enabled" in health["stale_removed"]
