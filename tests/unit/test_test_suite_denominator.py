from scripts.run_tests import LAYERS, audit_layer_coverage


def test_release_layers_cover_every_pytest_file_exactly_once():
    report = audit_layer_coverage()

    assert report["ok"] is True, report
    assert report["discovered_count"] == report["assigned_count"]
    assert report["missing"] == []
    assert report["extra"] == []
    assert report["overlaps"] == {}


def test_root_level_contract_tests_are_owned_by_quick():
    assert "tests/test_delayed_imports.py" in LAYERS["quick"]
    assert "tests/test_event_bus_map.py" in LAYERS["quick"]
    assert "tests/static" in LAYERS["quick"]
