from scripts.audit_ops_resilience_matrix import DEFAULT_MATRIX, validate


def test_ops_resilience_matrix_contract_is_current():
    assert validate(DEFAULT_MATRIX) == []
