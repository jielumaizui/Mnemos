from core.system_contracts import CAPABILITY_DEFINITIONS, audit_capability_registry


def test_capability_registry_is_strictly_valid():
    assert audit_capability_registry(strict=True) == []


def test_required_model_capabilities_are_registered():
    for name in ("llm", "embedding", "reranker"):
        capability = CAPABILITY_DEFINITIONS[name]
        assert capability.required is True
        assert capability.config_keys
        assert capability.health_surface.startswith("health.")
