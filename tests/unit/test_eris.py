from types import SimpleNamespace


def _trusted_config(wiki, db, mode):
    return SimpleNamespace(
        wiki_dir=wiki,
        database_dir=db.parent,
        get=lambda key, default=None: {
            "trusted_push.mode": mode,
            "trusted_push.db_path": str(db),
        }.get(key, default),
    )


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

    savings = engine._estimate_savings(
        [
            MergeCandidate(
                page_a=str(keep),
                page_b=str(discard),
                similarity=0.99,
                merge_strategy="delete_duplicate",
                reason="duplicate",
                recommended_action="delete",
                keep_page=str(keep),
            )
        ]
    )

    assert savings == {"pages": 1, "characters": len("discard-content")}


def test_auto_fix_duplicate_enforce_proposes_without_deleting(
    monkeypatch,
    tmp_path,
):
    from core.kia.eris import EntropyEngine, EntropyReport, MergeCandidate
    from core.trust.proposal_queue import ProposalQueue

    db = tmp_path / "trusted.db"
    monkeypatch.setattr(
        "core.trust.config.get_config",
        lambda: _trusted_config(tmp_path, db, "enforce"),
    )
    keep = tmp_path / "keep.md"
    discard = tmp_path / "discard.md"
    keep.write_text("same", encoding="utf-8")
    discard.write_text("same", encoding="utf-8")
    candidate = MergeCandidate(
        page_a=str(keep),
        page_b=str(discard),
        similarity=1.0,
        merge_strategy="delete_duplicate",
        reason="exact duplicate",
        recommended_action="delete",
        keep_page=str(keep),
    )

    logs = EntropyEngine(wiki_base=str(tmp_path)).auto_fix(
        EntropyReport(candidates=[candidate]),
        apply_duplicates=True,
    )

    assert discard.read_text(encoding="utf-8") == "same"
    assert any("已提交删除重复页面提案" in line for line in logs)
    proposals = ProposalQueue(db, wiki_base=tmp_path).list()
    assert len(proposals) == 1
    assert proposals[0].candidate.target_path == str(discard)


def test_incremental_scan_only_compares_new_vs_existing(tmp_path):
    """增量扫描应仅将新页面与已有页面对比，避免 O(n²)"""
    from core.kia.eris import EntropyEngine

    existing = tmp_path / "00-Inbox" / "existing.md"
    new_page = tmp_path / "00-Inbox" / "new.md"
    for path in [existing, new_page]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.name, encoding="utf-8")

    engine = EntropyEngine(wiki_base=str(tmp_path))
    engine._dna_engine = FakeDNAEngine(
        {
            str(existing): _dna(existing, keyword_set={"python"}),
            str(new_page): _dna(new_page, keyword_set={"python", "debug"}),
        },
        score=0.85,  # 超过 MERGE_THRESHOLD
    )

    report = engine._incremental_scan(new_page)

    # 只比对了 1 对（新页面 vs 已有页面）
    assert report.total_pairs_scanned == 1
    assert len(report.candidates) == 1
    assert report.candidates[0].merge_strategy == "merge_into_one"


def test_scan_reports_total_candidates_seen_when_top_k_truncates(tmp_path):
    from core.kia.eris import EntropyEngine

    pages = [tmp_path / "00-Inbox" / f"page-{i}.md" for i in range(3)]
    for page in pages:
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(page.name, encoding="utf-8")

    engine = EntropyEngine(wiki_base=str(tmp_path))
    engine._dna_engine = FakeDNAEngine(
        {str(page): _dna(page, keyword_set={"python", page.stem}) for page in pages},
        score=0.85,
    )

    report = engine.scan(sample_size=1)

    assert report.total_pairs_scanned == 3
    assert len(report.candidates) == 1
    assert report.total_candidates_seen == 3


def test_incremental_scan_skips_itself(tmp_path):
    """增量扫描不应将新页面与自身对比"""
    from core.kia.eris import EntropyEngine

    new_page = tmp_path / "00-Inbox" / "only.md"
    new_page.parent.mkdir(parents=True, exist_ok=True)
    new_page.write_text("only", encoding="utf-8")

    engine = EntropyEngine(wiki_base=str(tmp_path))
    engine._dna_engine = FakeDNAEngine(
        {str(new_page): _dna(new_page)},
        score=1.0,
    )

    report = engine._incremental_scan(new_page)

    assert report.total_pairs_scanned == 0
    assert len(report.candidates) == 0


def test_public_helpers_delegate_to_entropy_engine(monkeypatch):
    import core.kia.eris as eris

    calls = []

    class FakeEngine:
        def __init__(self, wiki_base=None):
            calls.append(("init", wiki_base))

        def scan(self, sample_size=None):
            calls.append(("scan", sample_size))
            return eris.EntropyReport(total_pairs_scanned=sample_size or 0)

        def generate_report(self, report):
            calls.append(("report", report.total_pairs_scanned))
            return f"pairs={report.total_pairs_scanned}"

    monkeypatch.setattr(eris, "EntropyEngine", FakeEngine)

    report = eris.run_entropy_scan(wiki_base="/tmp/wiki", sample_size=3)
    rendered = eris.run_and_report(wiki_base="/tmp/wiki", report=report)

    assert rendered == "pairs=3"
    assert calls == [
        ("init", "/tmp/wiki"),
        ("scan", 3),
        ("init", "/tmp/wiki"),
        ("report", 3),
    ]
