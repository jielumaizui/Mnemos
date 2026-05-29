from types import SimpleNamespace


def _dna(path, **kwargs):
    defaults = {
        "page_path": str(path),
        "domain": "dev",
        "knowledge_type": "guide",
        "semantic_signature": "dev:guide:入门:中性",
        "tool_entities": set(),
        "keyword_set": set(),
        "core_concepts": set(),
        "scenario_tags": set(),
        "confidence": 0.8,
        "title_pattern": "guide",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class FakeDNAEngine:
    def __init__(self, mapping, score):
        self.mapping = mapping
        self.score = score

    def compute_dna(self, page):
        return self.mapping.get(str(page))

    def save_dna(self, dna):
        return None

    def compare(self, dna_a, dna_b):
        return SimpleNamespace(overall_score=self.score)


def test_scan_covers_vault_and_excludes_reports_and_shadow(tmp_path):
    from core.kia.eris import EntropyEngine

    keep_a = tmp_path / "00-Inbox" / "a.md"
    keep_b = tmp_path / "03-Tech" / "b.md"
    excluded_report = tmp_path / "99-Reports" / "r.md"
    excluded_shadow = tmp_path / "07-Shadow" / "s.md"
    for path in [keep_a, keep_b, excluded_report, excluded_shadow]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.name, encoding="utf-8")

    engine = EntropyEngine(wiki_base=str(tmp_path))
    engine._dna_engine = FakeDNAEngine(
        {
            str(keep_a): _dna(keep_a, keyword_set={"python", "debug"}),
            str(keep_b): _dna(keep_b, keyword_set={"python", "trace"}),
        },
        score=0.7,
    )

    report = engine.scan()

    assert report.total_pairs_scanned == 1
    assert len(report.candidates) == 1
    assert {report.candidates[0].page_a, report.candidates[0].page_b} == {str(keep_a), str(keep_b)}


def test_should_compare_uses_structured_domain_and_type():
    from core.kia.eris import EntropyEngine

    engine = EntropyEngine(wiki_base="/tmp")
    a = _dna("a.md", domain="dev", knowledge_type="guide", semantic_signature="")
    b = _dna("b.md", domain="dev", knowledge_type="note", semantic_signature="")
    c = _dna("c.md", domain="ops", knowledge_type="guide", semantic_signature="")
    d = _dna("d.md", domain="ops", knowledge_type="note", semantic_signature="")

    assert engine._should_compare(a, b) is True
    assert engine._should_compare(a, c) is True
    assert engine._should_compare(a, d) is False


def test_cross_reference_candidate_for_complementary_score():
    from core.kia.eris import EntropyEngine

    engine = EntropyEngine(wiki_base="/tmp")
    a = _dna("a.md", keyword_set={"python", "traceback"})
    b = _dna("b.md", keyword_set={"python", "logging"})
    result = SimpleNamespace(overall_score=0.5)

    candidate = engine._generate_candidate(a, b, result)

    assert candidate.merge_strategy == "cross_reference"
    assert "双向引用" in candidate.recommended_action


def test_estimated_savings_counts_discarded_pages(tmp_path):
    from core.kia.eris import EntropyEngine, MergeCandidate

    keep = tmp_path / "keep.md"
    discard = tmp_path / "discard.md"
    keep.write_text("keep", encoding="utf-8")
    discard.write_text("discard-content", encoding="utf-8")
    engine = EntropyEngine(wiki_base=str(tmp_path))

    savings = engine._estimate_savings([
        MergeCandidate(
            page_a=str(keep),
            page_b=str(discard),
            similarity=0.99,
            merge_strategy="delete_duplicate",
            reason="duplicate",
            recommended_action="delete",
            keep_page=str(keep),
        )
    ])

    assert savings == {"pages": 1, "characters": len("discard-content")}
