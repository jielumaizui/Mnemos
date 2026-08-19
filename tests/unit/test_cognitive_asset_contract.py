from core.system_contracts import (
    CognitiveAsset,
    audit_cognitive_asset_schema,
    validate_all_system_contracts,
)


def test_cognitive_asset_registry_is_strictly_valid():
    assert audit_cognitive_asset_schema(strict=True) == []


def test_cognitive_asset_requires_source_evidence_and_consumers():
    asset = CognitiveAsset(
        asset_id="asset-1",
        asset_type="wiki_page",
        source_refs=("raw://turn/1",),
        evidence_refs=("artifact://conversation/1",),
        confidence=0.9,
        privacy_level="wiki_page",
        status="produced",
        consumers=("context_aware_search",),
        revision_policy="source_refs required",
    )

    assert asset.validate() == []


def test_all_system_contracts_are_valid():
    assert validate_all_system_contracts(strict=True) == []
