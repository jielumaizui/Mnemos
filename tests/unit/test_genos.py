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


def test_scan_all_pages_covers_vault_and_excludes_hidden_dirs(tmp_path):
    from core.kia.genos import DNAEngine

    keep_a = tmp_path / "00-Inbox" / "a.md"
    keep_b = tmp_path / "03-Tech" / "b.md"
    excluded = tmp_path / ".git" / "r.md"
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


def test_find_similar_uses_vector_candidates_beyond_exact_prefilter(tmp_path):
    from core.kia.genos import DNAEngine, KnowledgeDNA

    engine = DNAEngine(wiki_base=str(tmp_path), db_path=str(tmp_path / "dna.db"))
    target = KnowledgeDNA(
        page_path="target.md",
        content_md5="md5-target",
        content_simhash="a" * 16,
        semantic_signature="engineering:guide:basic:neutral",
        domain_type_hash="hash-target",
    )
    close_signature = KnowledgeDNA(
        page_path="close.md",
        content_md5="md5-close",
        content_simhash="b" * 16,
        semantic_signature="engineering:guide:advanced:neutral",
        domain_type_hash="hash-close",
    )
    distant_signature = KnowledgeDNA(
        page_path="distant.md",
        content_md5="md5-distant",
        content_simhash="c" * 16,
        semantic_signature="finance:ledger:archived:negative",
        domain_type_hash="hash-distant",
    )
    for dna in (target, close_signature, distant_signature):
        engine.save_dna(dna)

    results = engine.find_similar(target, threshold=0.0)

    assert "close.md" in [result.target_page for result in results]


def test_similarity_result_dimension_scores_json_contract(tmp_path):
    from dataclasses import asdict

    from core.cli.commands.genos import _similarity_to_dict
    from core.kia.genos import DNAEngine, KnowledgeDNA

    engine = DNAEngine(wiki_base=str(tmp_path), db_path=str(tmp_path / "dna.db"))
    target = KnowledgeDNA(
        page_path="target.md",
        content_simhash="f" * 16,
        semantic_signature="engineering:guide:basic:neutral",
        domain_type_hash="engineering-guide",
        keyword_set={"python", "debug"},
        core_concepts={"python"},
        tool_entities={"pytest"},
        title_keywords={"python", "debug"},
        title_pattern="guide",
        evidence_level="curated",
        temporal="stable",
    )
    candidate = KnowledgeDNA(
        page_path="candidate.md",
        content_simhash="f" * 16,
        semantic_signature="engineering:guide:basic:neutral",
        domain_type_hash="engineering-guide",
        keyword_set={"python", "testing"},
        core_concepts={"python"},
        tool_entities={"pytest"},
        title_keywords={"python", "testing"},
        title_pattern="guide",
        evidence_level="curated",
        temporal="stable",
    )

    result = engine.compare(target, candidate)

    assert set(result.dimension_scores) == {
        "content",
        "semantic",
        "keyword",
        "title",
        "structure",
    }
    assert asdict(result)["dimension_scores"] == result.dimension_scores
    assert _similarity_to_dict(result)["dimension_scores"] == result.dimension_scores


def test_find_cluster_expands_breadth_first_to_requested_depth(tmp_path):
    from core.kia.genos import DNAEngine, KnowledgeDNA, SimilarityResult

    engine = DNAEngine(wiki_base=str(tmp_path), db_path=str(tmp_path / "dna.db"))
    dnas = {
        page: KnowledgeDNA(page_path=page, semantic_signature="dev:guide:basic:neutral")
        for page in ("a.md", "b.md", "c.md")
    }
    for dna in dnas.values():
        engine.save_dna(dna)

    adjacency = {
        "a.md": ["b.md"],
        "b.md": ["c.md"],
        "c.md": [],
    }

    def fake_find_similar(dna, threshold=None):
        assert threshold == engine.CLUSTER_THRESHOLD
        return [
            SimilarityResult(
                target_page=page,
                overall_score=0.6,
                dimension_scores={},
                verdict="cluster",
                reason="test",
            )
            for page in adjacency[dna.page_path]
        ]

    engine.find_similar = fake_find_similar

    assert engine.find_cluster(dnas["a.md"], depth=2) == {"a.md", "b.md", "c.md"}


def test_vector_search_uses_signature_vocab_and_excludes_self(tmp_path):
    from core.kia.genos import DNAEngine, KnowledgeDNA

    engine = DNAEngine(wiki_base=str(tmp_path), db_path=str(tmp_path / "dna.db"))
    target = KnowledgeDNA(
        page_path="target.md",
        semantic_signature="engineering:guide:basic:neutral",
    )
    close_signature = KnowledgeDNA(
        page_path="close.md",
        semantic_signature="engineering:guide:advanced:neutral",
    )
    distant_signature = KnowledgeDNA(
        page_path="distant.md",
        semantic_signature="finance:ledger:archived:negative",
    )
    for dna in (target, close_signature, distant_signature):
        engine.save_dna(dna)

    results = engine.vector_search(target, top_k=1)

    assert len(results) == 1
    assert results[0]["page_path"] == "close.md"
    assert "distance" in results[0]
