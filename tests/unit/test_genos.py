from pathlib import Path


def _write_page(path, frontmatter, body="# Title\n\nBody content"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n{body}", encoding="utf-8")


def test_compute_dna_uses_contract_frontmatter_fields(tmp_path):
    from core.kia.genos import DNAEngine

    page = tmp_path / "03-Tech" / "python.md"
    _write_page(
        page,
        """domain: engineering
knowledge_type: guide
complexity: advanced
emotion: neutral
confidence: 0.8
evidence_level: curated
temporal_scope: stable
关键词:
  核心概念: [Python]
  工具实体: [pytest]
""",
    )
    engine = DNAEngine(wiki_base=str(tmp_path), db_path=str(tmp_path / "dna.db"))

    dna = engine.compute_dna(page)

    assert dna.domain == "engineering"
    assert dna.knowledge_type == "guide"
    assert dna.complexity == "advanced"
    assert dna.emotion == "neutral"
    assert dna.evidence_level == "curated"
    assert dna.temporal == "stable"
    assert "pytest" in dna.tool_entities


def test_save_and_load_preserves_structured_fields(tmp_path):
    from core.kia.genos import DNAEngine, KnowledgeDNA

    engine = DNAEngine(wiki_base=str(tmp_path), db_path=str(tmp_path / "dna.db"))
    dna = KnowledgeDNA(
        page_path="a.md",
        semantic_signature="dev:guide:basic:neutral",
        domain_type_hash="hash-a",
        domain="dev",
        knowledge_type="guide",
        complexity="basic",
        emotion="neutral",
    )

    assert engine.save_dna(dna) is True
    loaded = engine.load_dna("a.md")

    assert loaded.domain == "dev"
    assert loaded.knowledge_type == "guide"
    assert loaded.complexity == "basic"
    assert loaded.emotion == "neutral"


def test_scan_all_pages_covers_vault_and_excludes_reports(tmp_path):
    from core.kia.genos import DNAEngine

    keep_a = tmp_path / "00-Inbox" / "a.md"
    keep_b = tmp_path / "03-Tech" / "b.md"
    excluded = tmp_path / "99-Reports" / "r.md"
    for path in [keep_a, keep_b, excluded]:
        _write_page(path, "domain: dev\nknowledge_type: guide\n")

    engine = DNAEngine(wiki_base=str(tmp_path), db_path=str(tmp_path / "dna.db"))
    stats = engine.scan_all_pages()

    assert stats == {"scanned": 2, "computed": 2, "failed": 0}


def test_find_similar_prefilters_by_signature_hash_or_md5(tmp_path):
    from core.kia.genos import DNAEngine, KnowledgeDNA, SimilarityResult

    engine = DNAEngine(wiki_base=str(tmp_path), db_path=str(tmp_path / "dna.db"))
    target = KnowledgeDNA(
        page_path="target.md",
        content_md5="md5-target",
        semantic_signature="dev:guide:basic:neutral",
        domain_type_hash="dev-guide",
    )
    same_hash = KnowledgeDNA(
        page_path="same.md",
        content_md5="md5-same",
        semantic_signature="dev:note:basic:neutral",
        domain_type_hash="dev-guide",
    )
    different = KnowledgeDNA(
        page_path="different.md",
        content_md5="md5-different",
        semantic_signature="ops:runbook:basic:neutral",
        domain_type_hash="ops-runbook",
    )
    engine.save_dna(target)
    engine.save_dna(same_hash)
    engine.save_dna(different)

    compared = []

    def fake_compare(dna_a, dna_b):
        compared.append(dna_b.page_path)
        return SimilarityResult(
            target_page=dna_b.page_path,
            overall_score=0.9,
            dimension_scores={},
            verdict="related",
            reason="test",
        )

    engine.compare = fake_compare
    results = engine.find_similar(target, threshold=0.1)

    assert [r.target_page for r in results] == ["same.md"]
    assert compared == ["same.md"]
