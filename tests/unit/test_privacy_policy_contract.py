from core.system_contracts import PRIVACY_POLICIES, audit_privacy_retention_policy


def test_privacy_retention_policy_is_strictly_valid():
    assert audit_privacy_retention_policy(strict=True) == []


def test_secret_policy_never_allows_search_wiki_or_model_context():
    api_key_policy = PRIVACY_POLICIES["api_key"]

    assert api_key_policy.privacy_level == "secret"
    assert api_key_policy.searchable is False
    assert api_key_policy.wiki_allowed is False
    assert api_key_policy.model_consumable is False


def test_local_cognition_uses_narrow_redaction_without_encryption():
    cognition_policy = PRIVACY_POLICIES["cognition_asset"]

    assert cognition_policy.privacy_level == "private"
    assert cognition_policy.encryption_required is False
    assert cognition_policy.searchable is True
    assert cognition_policy.wiki_allowed is True
    assert cognition_policy.redaction_policy == "pii_credentials_only_v1"
