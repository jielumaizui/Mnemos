"""Tests for config example coverage verification."""

import json
from pathlib import Path

import yaml

from scripts import verify_config_examples as verifier


def test_model_call_ledger_daily_cap_examples_match_canonical_default():
    from core.config import DEFAULT_CONFIG

    repository_root = Path(__file__).resolve().parents[2]
    json_example = json.loads(
        (repository_root / "config" / "config.example.json").read_text(encoding="utf-8")
    )
    yaml_example = yaml.safe_load(
        (repository_root / "config" / "config.example.yaml").read_text(encoding="utf-8")
    )

    expected = DEFAULT_CONFIG["model_call_ledger"]["daily_cost_cap"]
    assert expected == 50.0
    assert json_example["model_call_ledger"]["daily_cost_cap"] == expected
    assert yaml_example["model_call_ledger"]["daily_cost_cap"] == expected


def test_verify_config_examples_default_keeps_95_percent_threshold(
    monkeypatch, capsys
):
    config = {f"k{i}": i for i in range(20)}
    env_vars = [f"MNEMOS_TEST_{i}" for i in range(20)]

    monkeypatch.setattr(verifier, "DEFAULT_CONFIG", config)
    monkeypatch.setattr(
        verifier,
        "_load_json_example",
        lambda: dict(list(config.items())[:19]),
    )
    monkeypatch.setattr(
        verifier,
        "_load_yaml_example",
        lambda: dict(list(config.items())[:19]),
    )
    monkeypatch.setattr(verifier, "_extract_env_map_vars", lambda: env_vars)
    monkeypatch.setattr(verifier, "public_env_vars", lambda: [])
    monkeypatch.setattr(verifier, "_env_example_vars", lambda: set(env_vars[:19]))

    assert verifier.main([]) == 0

    output = capsys.readouterr().out
    assert "config.example.json: 19/20 (95%)" in output
    assert ".env.example: 19/20 env vars (95%)" in output
    assert "OK: config examples meet coverage thresholds." in output


def test_verify_config_examples_strict_fails_below_full_coverage(
    monkeypatch, capsys
):
    monkeypatch.setattr(verifier, "DEFAULT_CONFIG", {"a": 1, "b": 2})
    monkeypatch.setattr(verifier, "_load_json_example", lambda: {"a": 1})
    monkeypatch.setattr(verifier, "_load_yaml_example", lambda: {"a": 1, "b": 2})
    monkeypatch.setattr(
        verifier,
        "_extract_env_map_vars",
        lambda: ["MNEMOS_TEST_A", "MNEMOS_TEST_B"],
    )
    monkeypatch.setattr(verifier, "public_env_vars", lambda: [])
    monkeypatch.setattr(verifier, "_env_example_vars", lambda: {"MNEMOS_TEST_A"})

    assert verifier.main(["--strict"]) == 1

    output = capsys.readouterr().out
    assert "config.example.json: 1/2 (50%)" in output
    assert ".env.example: 1/2 env vars (50%)" in output
    assert "ERROR: config.example.json coverage < 100%" in output
    assert "ERROR: .env.example coverage < 100%" in output


def test_nested_leaf_is_part_of_strict_coverage(monkeypatch, capsys):
    monkeypatch.setattr(
        verifier,
        "DEFAULT_CONFIG",
        {"daemon": {"services": {"a": True, "b": True}}},
    )
    monkeypatch.setattr(
        verifier,
        "_load_json_example",
        lambda: {"daemon": {"services": {"a": True}}},
    )
    monkeypatch.setattr(
        verifier,
        "_load_yaml_example",
        lambda: {"daemon": {"services": {"a": True, "b": True}}},
    )
    monkeypatch.setattr(verifier, "_extract_env_map_vars", lambda: [])
    monkeypatch.setattr(verifier, "public_env_vars", lambda: [])
    monkeypatch.setattr(verifier, "_env_example_vars", lambda: set())

    assert verifier.main(["--strict"]) == 1
    assert "config.example.json: 1/2 (50%)" in capsys.readouterr().out
