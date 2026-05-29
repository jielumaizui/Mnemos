from types import SimpleNamespace


def _write_page(path, frontmatter="", body="## 核心内容\n内容足够长。" * 10):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n{body}", encoding="utf-8")


def _dna(path, **kwargs):
    defaults = {
        "page_path": str(path),
        "domain": "dev",
        "knowledge_type": "guide",
        "semantic_signature": "dev:guide:入门:中性",
        "tool_entities": set(),
        "keyword_set": {"python", "debug"},
        "core_concepts": set(),
        "scenario_tags": set(),
        "confidence": 0.8,
        "title_pattern": "guide",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class FakeDNAEngine:
    def __init__(self, mapping, score=0.99):
        self.mapping = mapping
        self.score = score

    def compute_dna(self, page):
        return self.mapping.get(str(page))

    def save_dna(self, dna):
        return None

    def compare(self, dna_a, dna_b):
        return SimpleNamespace(overall_score=self.score)


def test_list_pages_scans_vault_and_excludes_reports_and_shadow(tmp_path):
    from core.kia.hygieia import KnowledgeImmuneSystem

    keep_a = tmp_path / "00-Inbox" / "a.md"
    keep_b = tmp_path / "03-Tech" / "b.md"
    excluded_report = tmp_path / "99-Reports" / "r.md"
    excluded_shadow = tmp_path / "07-Shadow" / "s.md"
    for path in [keep_a, keep_b, excluded_report, excluded_shadow]:
        _write_page(path)

    pages = KnowledgeImmuneSystem(wiki_base=str(tmp_path))._list_pages()

    assert set(pages) == {keep_a, keep_b}


def test_outdated_supports_contract_frontmatter_names(tmp_path):
    from core.kia.hygieia import KnowledgeImmuneSystem

    page = tmp_path / "03-Tech" / "old.md"
    _write_page(page, "temporal_scope: 版本绑定\ncreated_at: 2020-01-01\nversion_tag: 1.19\n")

    issues = KnowledgeImmuneSystem(wiki_base=str(tmp_path)).detect_outdated([page])

    assert any(issue.issue_type == "outdated" for issue in issues)
    assert any(issue.issue_type == "version_check" for issue in issues)


def test_low_confidence_supports_contract_frontmatter_names(tmp_path):
    from core.kia.hygieia import KnowledgeImmuneSystem

    page = tmp_path / "03-Tech" / "weak.md"
    _write_page(page, "confidence: 0.5\nevidence_level: single-source\n")

    issues = KnowledgeImmuneSystem(wiki_base=str(tmp_path)).detect_low_confidence([page])

    assert len(issues) == 1
    assert issues[0].issue_type == "weak_evidence"


def test_duplicates_delegate_to_entropy_engine(tmp_path):
    from core.kia.hygieia import KnowledgeImmuneSystem

    a = tmp_path / "00-Inbox" / "a.md"
    b = tmp_path / "03-Tech" / "b.md"
    _write_page(a)
    _write_page(b)
    dna_engine = FakeDNAEngine({str(a): _dna(a), str(b): _dna(b)})

    issues = KnowledgeImmuneSystem(wiki_base=str(tmp_path), dna_engine=dna_engine).detect_duplicates()

    assert len(issues) == 1
    assert issues[0].issue_type == "duplicate"
    # page_a/page_b 顺序取决于文件系统遍历顺序，不假定固定顺序
    pages = {issues[0].page, issues[0].related_pages[0]}
    assert pages == {str(a), str(b)}
