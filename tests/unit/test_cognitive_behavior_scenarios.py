from scripts.audit_cognitive_behavior_scenarios import DEFAULT_MATRIX, validate


def test_cognitive_behavior_scenario_contract_is_current():
    assert validate(DEFAULT_MATRIX) == []
