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

    issues = KnowledgeImmuneSystem(
        wiki_base=str(tmp_path), dna_engine=dna_engine
    ).detect_duplicates()

    assert len(issues) == 1
    assert issues[0].issue_type == "duplicate"
    # page_a/page_b 顺序取决于文件系统遍历顺序，不假定固定顺序
    pages = {issues[0].page, issues[0].related_pages[0]}
    assert pages == {str(a), str(b)}


def test_detect_knowledge_gaps_unsolved(tmp_path):
    """多次 effect 失败应被识别为 unsolved 盲区。"""
    from core.kia.hygieia import KnowledgeImmuneSystem
    from core.kia.ariadne import KnowledgeTrail

    page = tmp_path / "03-Tech" / "troubled.md"
    _write_page(page)
    trail = KnowledgeTrail(wiki_base=str(tmp_path))
    for _ in range(3):
        trail.log_effect(str(page), solved=False)

    issues = KnowledgeImmuneSystem(wiki_base=str(tmp_path)).detect_knowledge_gaps([page])
    unsolved = [i for i in issues if "未解决" in i.description]

    assert len(unsolved) >= 1
    assert unsolved[0].issue_type == "knowledge_gap"


def test_detect_knowledge_gaps_unrecorded(tmp_path):
    """高频 query 但无 effect 应被识别为 unrecorded 盲区。"""
    from core.kia.hygieia import KnowledgeImmuneSystem
    from core.kia.ariadne import KnowledgeTrail

    page = tmp_path / "03-Tech" / "missing.md"
    _write_page(page)
    trail = KnowledgeTrail(wiki_base=str(tmp_path))
    for _ in range(6):
        trail.log_query(str(page), context="test")

    issues = KnowledgeImmuneSystem(wiki_base=str(tmp_path)).detect_knowledge_gaps([page])

    # 描述中包含"未记录"或"无任何 effect"
    assert any("未记录" in i.description or "无任何 effect" in i.description for i in issues)


def test_detect_knowledge_gaps_no_trail_db(tmp_path):
    """没有 trail.db 时应静默返回，不抛异常。"""
    from core.kia.hygieia import KnowledgeImmuneSystem

    page = tmp_path / "03-Tech" / "page.md"
    _write_page(page)
    issues = KnowledgeImmuneSystem(wiki_base=str(tmp_path)).detect_knowledge_gaps([page])
    assert isinstance(issues, list)


def test_query_coverage_gap_is_owned_by_hygieia(tmp_path):
    from core.kia.hygieia import KnowledgeImmuneSystem, QueryCoverageObservation

    issues = KnowledgeImmuneSystem(wiki_base=str(tmp_path)).detect_knowledge_gaps(
        pages=[],
        query_observation=QueryCoverageObservation(
            query="projection receipts",
            authorized_hit_count=0,
            evidence_ref="authorized-context-search:sha256:test",
            scope_key="sha256:scope",
        ),
    )

    assert {issue.page for issue in issues} == {"projection", "receipts"}
    assert all(issue.dimension == "missing_topic" for issue in issues)
    assert all(issue.scope_key == "sha256:scope" for issue in issues)


def test_chinese_producer_forms_normalize_to_consumer_vocabulary(tmp_path):
    from core.kia.hygieia import KnowledgeImmuneSystem

    immune = KnowledgeImmuneSystem(wiki_base=str(tmp_path))
    _, forms = immune._extract_tags_and_forms(
        {"标签": ["runtime"], "知识形态": ["决策记录", "经验法则", "洞察关联"]}
    )

    assert forms == {"decision", "heuristic", "insight"}


def test_generate_report_markdown_renders_health_summary(tmp_path):
    from core.kia.hygieia import HealthReport, ImmuneIssue, KnowledgeImmuneSystem

    page = tmp_path / "03-Tech" / "weak.md"
    issue = ImmuneIssue(
        issue_type="weak_evidence",
        severity="high",
        page=str(page),
        description="证据来源不足",
        suggestion="补充更多来源",
        auto_fixable=True,
    )
    report = HealthReport(
        scanned_pages=3,
        issues=[issue],
        summary={"低置信度": 1},
        auto_fixable_count=1,
        critical_count=0,
    )

    markdown = KnowledgeImmuneSystem(wiki_base=str(tmp_path)).generate_report_markdown(report)

    assert "# 知识库健康报告" in markdown
    assert "**扫描页面**: 3" in markdown
    assert "- 低置信度: 1" in markdown
    assert "**[weak_evidence]** `weak.md`" in markdown
    assert "- 状态: ✅ 可自动修复" in markdown
