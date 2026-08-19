from scripts.audit_adaptive_data_flows import DEFAULT_MATRIX, validate


def test_adaptive_data_flow_contract_is_current():
    assert validate(DEFAULT_MATRIX) == []


def test_adaptive_data_flow_contract_declares_runtime_audit_surface():
    import json

    matrix = json.loads(DEFAULT_MATRIX.read_text(encoding="utf-8"))
    runtime_audit = matrix["runtime_audit"]

    assert runtime_audit["schema_version"] == "mnemos.runtime_producer_consumer.v2"
    assert runtime_audit["data_event_schema"] == "mnemos.cognitive_data_event.v1"
    assert runtime_audit["data_interface_registry_schema"] == "mnemos.data_interface_registry.v1"
    assert runtime_audit["ledger"] == "producer_consumer_ledger.db"
    assert (
        runtime_audit["strict_gate"]
        == "python3 scripts/audit_runtime_producer_consumer_closure.py --strict"
    )
    assert (
        runtime_audit["data_interface_strict_gate"]
        == "python3 scripts/audit_data_interface_registry.py --strict"
    )
    assert runtime_audit["health_check"] == "checks.runtime_producer_consumer"
    assert "producer_consumer.orphan_outputs" in runtime_audit["scorecard_metrics"]
    assert "producer_consumer.no_source_consumers" in runtime_audit["scorecard_metrics"]
    assert "producer_consumer.item_mismatches" in runtime_audit["scorecard_metrics"]
    assert "cognitive_data.events" in runtime_audit["scorecard_metrics"]
    assert "duplicates.reconciled" in runtime_audit["scorecard_metrics"]
    assert len(runtime_audit["flow_contracts"]) == len(matrix["flows"])
