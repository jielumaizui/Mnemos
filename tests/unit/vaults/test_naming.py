from __future__ import annotations


def test_source_prefixed_stem_detection_covers_known_generated_prefixes():
    from core.vaults.naming import is_source_prefixed_stem

    assert is_source_prefixed_stem("session__bad-title")
    assert is_source_prefixed_stem("codex-20_good-title")
    assert is_source_prefixed_stem("019f2324_good-title")
    assert is_source_prefixed_stem("hash8_good-title")
    assert not is_source_prefixed_stem("redis-ttl策略")
