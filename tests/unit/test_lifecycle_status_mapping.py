from core.system_contracts import (
    FAILURE_CLASSES,
    LIFECYCLE_MAPPINGS,
    LIFECYCLE_STATUSES,
    audit_lifecycle_status_contract,
)


def test_lifecycle_status_contract_is_strictly_valid():
    assert audit_lifecycle_status_contract(strict=True) == []


def test_daemon_database_lock_maps_to_failure_class():
    mapping = LIFECYCLE_MAPPINGS["daemon_service"]

    assert mapping.failure_classes["OperationalError"] == "database_lock"
    assert "database_lock" in FAILURE_CLASSES
    assert set(mapping.local_statuses.values()) <= LIFECYCLE_STATUSES
