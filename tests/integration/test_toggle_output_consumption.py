from core.module_toggles import MODULE_TOGGLE_DEFINITIONS, audit_runtime_producer_consumer_closure


def test_cold_start_outputs_are_bound_to_downstream_consumers():
    assert audit_runtime_producer_consumer_closure(strict=True) == []

    for definition in MODULE_TOGGLE_DEFINITIONS.values():
        if definition.default_state in {"registered_but_unwired", "stale_removed"}:
            continue
        contract = definition.output_contract
        assert contract.consumer_ids, definition.key
        assert contract.consumer_effect_metrics, definition.key
        assert contract.action_ledger_ref == "module_toggle"
