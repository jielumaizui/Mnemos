from core.privacy.content_redaction import (
    REDACTION_POLICY,
    redact_persistence_value,
)
import pytest


def test_persistence_redaction_masks_only_pii_cards_and_credentials():
    provider_value = "sk-" + "example-123456789012"
    source = {
        "content": (
            "Keep this verification recipe and code block.\n"
            f"api_key={provider_value} password=hunter-example-1234\n"
            "email=private.person@example.com phone=13800138000\n"
            "身份证号: 11010519491231002X\n"
            "银行卡号: 4111 1111 1111 1111"
        ),
        "nested": {"password": "another-private-password"},
    }

    redacted = redact_persistence_value(source)
    rendered = str(redacted.value)

    assert redacted.policy == REDACTION_POLICY
    assert redacted.total >= 6
    assert "Keep this verification recipe and code block." in rendered
    assert "1111 1111 1111" not in rendered
    for sensitive_value in (
        provider_value,
        "hunter-example-1234",
        "private.person@example.com",
        "13800138000",
        "11010519491231002X",
        "4111 1111 1111 1111",
        "another-private-password",
    ):
        assert sensitive_value not in rendered


def test_persistence_redaction_preserves_non_sensitive_numbers_and_provenance():
    source = {
        "source_agent": "codex",
        "session_id": "session-2026-07-15-001",
        "name": "Redis connection pool",
        "address": "0x7ffee3ab",
        "content": "Python 3.12, timeout=300, sha256:123456789012 and port 5432.",
    }

    redacted = redact_persistence_value(source)

    assert redacted.value == source
    assert redacted.counts == ()


def test_bank_card_redaction_does_not_corrupt_opaque_revision_hashes():
    source = {
        "revision_id": "rawrev-abc4111111111111111def",
        "content": (
            "standalone card 4111111111111111 and ID 11010519491231002X "
            "must still be private"
        ),
        "opaque_id": "rawrev-abc11010519491231002Xdef",
    }

    redacted = redact_persistence_value(source)

    assert redacted.value["revision_id"] == source["revision_id"]
    assert redacted.value["opaque_id"] == source["opaque_id"]
    assert "4111111111111111" not in redacted.value["content"]
    assert redacted.value["content"] == (
        "standalone card [REDACTED:BANK_CARD] and ID [REDACTED:ID] "
        "must still be private"
    )


def test_bank_card_redaction_preserves_compact_timestamp_that_passes_luhn():
    source = {
        "stored_in": "/tmp/failed-session-1-20260715-231648.json",
        "content": "standalone card 4111111111111111 must still be private",
    }

    redacted = redact_persistence_value(source)

    assert redacted.value["stored_in"] == source["stored_in"]
    assert redacted.value["content"] == (
        "standalone card [REDACTED:BANK_CARD] must still be private"
    )
    assert redacted.counts == (("bank_card", 1),)


def test_opaque_target_ref_preserves_non_secret_identity_words():
    source = {"target_ref": "security-page->invalid-api-key:references"}

    redacted = redact_persistence_value(source)

    assert redacted.value == source
    assert redacted.counts == ()


def test_opaque_target_ref_rejects_real_credential_bytes_instead_of_rewriting():
    source = {
        "target_ref": (
            "relation:api_key=DUMMY_CREDENTIAL_VALUE_FOR_REDACTION_TEST"
        )
    }

    with pytest.raises(ValueError, match="opaque identity contains sensitive content"):
        redact_persistence_value(source)


def test_generated_compact_timestamp_is_not_misclassified_as_bank_card():
    source = {
        "target_ref": (
            "kg-page:复盘提醒-system-recap-20260703011227-存在-1-个显著偏差"
        )
    }

    redacted = redact_persistence_value(source)

    assert redacted.value == source
    assert redacted.counts == ()
