"""Tests for keyring/env fallback diagnostics."""

from __future__ import annotations

import sys
from types import SimpleNamespace

from core.ops.keyring_doctor import build_keyring_doctor_report, probe_keyring


class _Config:
    def __init__(self, accepted: bool = False):
        self._data = {"llm": {"api_key_source": "env:MNEMOS_LLM_API_KEY"}}
        self._accepted = accepted

    def get(self, key, default=None):
        if key == "security.accept_env_secret_fallback":
            return self._accepted
        return default


def _inventory():
    return {
        "findings": [{"status": "reference", "source": "env"}],
        "plaintext_count": 0,
        "reference_count": 1,
        "error": None,
    }


def test_probe_keyring_treats_fail_backend_as_unavailable(monkeypatch):
    class Keyring:
        pass

    Keyring.__module__ = "keyring.backends.fail"
    monkeypatch.setitem(
        sys.modules,
        "keyring",
        SimpleNamespace(get_keyring=lambda: Keyring()),
    )

    result = probe_keyring()

    assert result["available"] is False
    assert result["backend"] == "keyring.backends.fail.Keyring"
    assert "not usable" in result["error"]


def test_keyring_report_marks_unaccepted_env_fallback_safe_but_not_best():
    report = build_keyring_doctor_report(
        _Config(accepted=False),
        keyring_info={"available": False, "backend": None, "error": "missing"},
        secret_inventory=_inventory(),
    )

    assert report["status"] == "warning"
    assert report["ok"] is False
    assert report["risk_level"] == "safe_but_not_best"
    assert report["requires_user_choice"] is True
    assert report["secret_inventory_plaintext_count"] == 0


def test_keyring_report_accepts_env_fallback_when_plaintext_free():
    report = build_keyring_doctor_report(
        _Config(accepted=True),
        keyring_info={"available": False, "backend": None, "error": "missing"},
        secret_inventory=_inventory(),
    )

    assert report["status"] == "accepted"
    assert report["ok"] is True
    assert report["env_fallback_accepted"] is True
    assert report["safe_but_not_best"] is True
