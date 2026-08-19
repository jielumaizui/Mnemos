from scripts.audit_function_matrix import DEFAULT_MATRIX, _entry_exists, validate


def test_function_matrix_contract_is_current():
    assert validate(DEFAULT_MATRIX) == []


def test_cli_entrypoint_validation_accepts_documented_options():
    cli_commands = {"reminder", "reminder resolve"}

    assert _entry_exists("cli:reminder resolve --issue", cli_commands, set())
