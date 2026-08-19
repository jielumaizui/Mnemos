"""Tests for distillation response budget audit."""

from scripts import audit_distill_response_budget as audit


def test_distill_response_budget_audit_passes_current_repo():
    assert audit.validate() == []


def test_distill_response_budget_audit_flags_old_default_value(monkeypatch):
    broken_config = {
        "distill": {
            "response_tokens": 4000,
            "response_tokens_default": 6000,
            "response_tokens_medium": 8000,
            "response_tokens_long": 12000,
            "response_tokens_retry_max": 16000,
        }
    }
    monkeypatch.setattr(audit, "DEFAULT_CONFIG", broken_config)

    errors = audit.validate_config_defaults()

    assert any(
        "DEFAULT_CONFIG distill.response_tokens expected 6000, got 4000" in error
        for error in errors
    )


def test_distill_response_budget_audit_flags_engine_fallback_constant(monkeypatch, tmp_path):
    fake_root = tmp_path
    engine = fake_root / "core" / "hephaestus" / "distillation_engine.py"
    engine.parent.mkdir(parents=True)
    engine.write_text("RESPONSE_TOKENS = 4000\n", encoding="utf-8")
    monkeypatch.setattr(audit, "ROOT", fake_root)

    errors = audit.validate_engine_fallback_constant()

    assert errors == ["distillation_engine.RESPONSE_TOKENS expected 6000, got 4000"]
