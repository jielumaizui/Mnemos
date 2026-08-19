from core.module_toggles import (
    MODULE_TOGGLE_DEFINITIONS,
    audit_runtime_producer_consumer_closure,
    audit_toggle_output_consumers,
)


def test_toggle_output_contracts_validate_strictly():
    assert audit_toggle_output_consumers(strict=True) == []
    assert audit_runtime_producer_consumer_closure(strict=True) == []


def test_auto_enabled_toggles_have_consumers_effect_metrics_and_rollback():
    auto_enabled = [
        definition
        for definition in MODULE_TOGGLE_DEFINITIONS.values()
        if definition.auto_enable_allowed
    ]
    assert auto_enabled
    for definition in auto_enabled:
        contract = definition.output_contract
        assert contract.consumer_ids, definition.key
        assert contract.consumer_effect_metrics, definition.key
        assert contract.scorecard_metrics, definition.key
        assert any(
            word in contract.rollback_strategy.lower()
            for word in ("disable", "restore", "rollback")
        ), definition.key


def test_unwired_toggles_are_not_auto_enabled():
    unwired = [
        definition
        for definition in MODULE_TOGGLE_DEFINITIONS.values()
        if definition.default_state == "registered_but_unwired"
    ]
    assert {item.key for item in unwired} >= {
        "scoring.clustering.enabled",
        "scoring.training_scheduler.enabled",
    }
    assert all(not definition.auto_enable_allowed for definition in unwired)
