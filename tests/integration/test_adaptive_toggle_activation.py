from core.module_toggles import MODULE_TOGGLE_DEFINITIONS, build_toggle_matrix


class MappingConfig:
    def __init__(self, values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


def test_adaptive_toggle_activation_uses_decision_before_runtime_enable():
    definition = MODULE_TOGGLE_DEFINITIONS["intent_router.llm_fallback_enabled"]

    assert definition.default_state == "disabled_cold_start"
    assert definition.auto_enable_allowed is True
    assert definition.output_contract.quality_gate_id == "activation_quality_decision"
    assert definition.output_contract.action_ledger_ref == "module_toggle"

    matrix = build_toggle_matrix(MappingConfig({"intent_router.llm_fallback_enabled": True}))
    runtime = matrix["intent_router.llm_fallback_enabled"]
    assert runtime["current_enabled"] is True
    assert runtime["runtime_state"] == "manual_enabled"
    assert runtime["output_contract"]["consumer_ids"] == (
        "application_router",
        "intent_correct",
        "scorecard",
    )


def test_auto_enable_candidates_have_activation_and_disable_policies():
    candidates = [
        definition
        for definition in MODULE_TOGGLE_DEFINITIONS.values()
        if definition.auto_enable_allowed
    ]
    assert candidates
    for definition in candidates:
        assert definition.activation_policy, definition.key
        assert definition.auto_disable_policy, definition.key
        assert definition.output_contract.consumer_effect_metrics, definition.key
