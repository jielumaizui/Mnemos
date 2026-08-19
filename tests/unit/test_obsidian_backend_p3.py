"""Tests for ObsidianBackend sensitive content sanitization."""

import pytest

from integrations.backends.obsidian_backend import _sanitize_content


@pytest.mark.parametrize(
    "raw,expected_substring",
    [
        ("key: " + "sk" + "-abcdefghijklmnopqrstuvwxyz123456", "[REDACTED_API_KEY]"),
        ("to" + "ken: " + "memos_pat_abc123def456ghi789", "[REDACTED_TOKEN]"),
        ("Authorization: Bearer abcdef1234567890", "Bearer [REDACTED_TOKEN]"),
        (
            '"ANTHROPIC_AUTH_TOKEN": "' + "sk" + '-secret-123"',
            '"ANTHROPIC_AUTH_TOKEN": "[REDACTED]"',
        ),
        (
            "OPENAI_API_" + 'KEY="' + "sk" + '-12345678901234567890"',
            'OPENAI_API_KEY="[REDACTED]"',
        ),
    ],
)
def test_sanitize_content_redacts_secrets(raw, expected_substring):
    sanitized = _sanitize_content(raw)
    assert expected_substring in sanitized
    assert "sk-" not in sanitized
    assert "memos_pat_" not in sanitized


def test_sanitize_content_preserves_normal_text():
    normal = "This is a normal conversation about sk-learn and tokenization."
    assert _sanitize_content(normal) == normal
