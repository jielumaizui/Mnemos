"""
wiki_rebuild.py 单元测试

覆盖项：
- _parse_distill_time — 多种时间格式解析
- _parse_quality_score — 质量分提取
- _compute_readability_score — 三维度可读性评分
- _is_user_edited — 用户编辑检测（显式标记 + mtime 对比）
- _get_session_id_from_fm — session_id 提取
- _fetch_l1_for_session — StorageBackend 查询
- analyze_page — 单页面分析（含 frontmatter 解析、蒸馏标记过滤）
- scan_wiki_pages — Wiki 目录扫描
- filter_pages_for_rebuild — 筛选逻辑
- generate_dry_run_report — 报告生成
- backup_page — 页面备份
- rebuild_single_page — 单页面重跑（dry-run / 正常 / 失败路径）
- run_selective_rebuild — 主入口（空选中 / dry-run / 全链路）
"""

import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest

# 先 mock config，避免 import 时读取文件系统
_FAKE_CONFIG = MagicMock()
_FAKE_CONFIG.wiki_dir = Path(tempfile.gettempdir()) / "mnemos_test_wiki"
_FAKE_CONFIG.data_dir = Path(tempfile.gettempdir()) / "mnemos_test_data"
_FAKE_CONFIG.database_dir = _FAKE_CONFIG.data_dir

with patch("core.config.get_config", return_value=_FAKE_CONFIG):
    from core.hephaestus import wiki_rebuild as wr
    from core.hephaestus.distillation_engine import (
        DistillationEngine,
        DistillationResult,
    )
    from core.hephaestus.distillation_prompts import PROMPT_VERSION
    from core.frontmatter import write_frontmatter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_wiki_dir(tmp_path, monkeypatch):
    """提供隔离的 Wiki 目录，并注入 _get_wiki_dir"""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    monkeypatch.setattr(wr, "_get_wiki_dir", lambda: wiki)
    return wiki


@pytest.fixture
def mock_backend():
    """构造返回可控数据的 StorageBackend mock"""
    client = Mock()
    return client


@pytest.fixture
def mock_engine():
    """构造 DistillationEngine mock"""
    engine = Mock(spec=DistillationEngine)
    return engine


# ---------------------------------------------------------------------------
# 1. _parse_distill_time
# ---------------------------------------------------------------------------


def test_parse_distill_time_iso_with_z():
    """ISO 格式带 Z 应正确解析"""
    fm = {"distilled_at": "2024-01-15T10:30:00Z"}
    dt = wr._parse_distill_time(fm)
    assert dt is not None
    assert dt.year == 2024
    assert dt.hour == 10


def test_parse_distill_time_chinese_format():
    """中文格式 '蒸馏时间' 应正确解析"""
    fm = {"蒸馏时间": "2024-01-15 10:30:00"}
    dt = wr._parse_distill_time(fm)
    assert dt is not None
    assert dt.month == 1
    assert dt.day == 15


def test_parse_distill_time_iso_without_z():
    """ISO 格式不带 Z 应正确解析"""
    fm = {"distill_time": "2024-06-01T12:00:00+00:00"}
    dt = wr._parse_distill_time(fm)
    assert dt is not None
    assert dt.year == 2024


def test_parse_distill_time_returns_none_when_missing():
    """无时间字段应返回 None"""
    assert wr._parse_distill_time({}) is None
    assert wr._parse_distill_time({"other": "value"}) is None


def test_parse_distill_time_returns_none_on_bad_format():
    """格式错误应返回 None 不抛异常"""
    fm = {"distilled_at": "not-a-date"}
    assert wr._parse_distill_time(fm) is None


# ---------------------------------------------------------------------------
# 2. _parse_quality_score
# ---------------------------------------------------------------------------


def test_parse_quality_score_float():
    """正常浮点质量分应正确解析"""
    assert wr._parse_quality_score({"quality_score": 75.5}) == 75.5


def test_parse_quality_score_int():
    """整数质量分应正确解析为 float"""
    assert wr._parse_quality_score({"质量分": 80}) == 80.0


def test_parse_quality_score_string_number():
    """字符串数字应正确解析"""
    assert wr._parse_quality_score({"quality": "60.0"}) == 60.0


def test_parse_quality_score_returns_none_when_missing():
    """无质量分字段应返回 None"""
    assert wr._parse_quality_score({}) is None


def test_parse_quality_score_returns_none_on_invalid():
    """无效值应返回 None"""
    assert wr._parse_quality_score({"quality_score": "bad"}) is None


# ---------------------------------------------------------------------------
# 3. _compute_readability_score
# ---------------------------------------------------------------------------


def test_compute_readability_score_perfect():
    """完整页面应得满分或接近满分"""
    fm = {
        "summary": "a" * 50,
        "name": "Test Page",
        "domain": "tech",
        "evidence_level": "high",
        "confidence": 0.9,
        "temporal_scope": "permanent",
        "quality_score": 80,
        "distill_prompt_version": PROMPT_VERSION,
        "truncated": False,
    }
    body = (
        "## 结论\n\nConclusion\n\n"
        "## 怎么用\n\nUsage\n\n"
        "## 详细内容\n\nCore\n\n"
        "## 可信度提示\n\nCaveats\n\n"
    )
    score, detail = wr._compute_readability_score(fm, body)
    assert score >= 90
    assert detail["structure_score"] == 40.0
    assert detail["summary_length"] == 50
    assert detail["name_valid"] is True
    assert detail["truncated"] is False
    assert detail["prompt_version_current"] is True


def test_compute_readability_score_minimal():
    """空 frontmatter + 空 body 应得低分"""
    score, detail = wr._compute_readability_score({}, "")
    assert score < 20
    assert detail["structure_score"] == 0.0
    assert detail["summary_length"] == 0
    assert not detail["name_valid"]


def test_compute_readability_score_partial_structure():
    """只含部分章节应得部分结构分"""
    body = "## 怎么用\n\nUsage\n\n## 详细内容\n\nDetails\n\n"
    score, detail = wr._compute_readability_score({}, body)
    assert detail["structure_score"] == 20.0
    assert "怎么用" in detail["found_sections"]
    assert "可信度提示" in detail["missing_sections"]


def test_compute_readability_score_truncated_penalty():
    """truncated=True 应扣截断分"""
    fm = {"truncated": True}
    score1, _ = wr._compute_readability_score(fm, "")
    score2, _ = wr._compute_readability_score({}, "")
    assert score1 < score2


def test_compute_readability_score_bad_name():
    """无效名称应不得名称分"""
    fm = {"name": "untitled"}
    _, detail = wr._compute_readability_score(fm, "")
    assert detail["name_valid"] is False


def test_compute_readability_score_quality_tiers():
    """不同质量分应得不同元数据分"""
    _, d_high = wr._compute_readability_score({"quality_score": 80}, "")
    _, d_mid = wr._compute_readability_score({"quality_score": 50}, "")
    _, d_none = wr._compute_readability_score({}, "")
    assert d_high["meta_score"] > d_mid["meta_score"]
    assert d_mid["meta_score"] > d_none["meta_score"]


# ---------------------------------------------------------------------------
# 4. _is_user_edited
# ---------------------------------------------------------------------------


def test_is_user_edited_explicit_flag():
    """frontmatter 显式标记 user_edited 应返回 True"""
    fp = Path("/tmp/fake.md")
    edited, reason = wr._is_user_edited(fp, {"user_edited": True})
    assert edited is True
    assert "显式标记" in reason


def test_is_user_edited_chinese_flag():
    """frontmatter 显式标记 手工编辑 应返回 True"""
    fp = Path("/tmp/fake.md")
    edited, reason = wr._is_user_edited(fp, {"手工编辑": True})
    assert edited is True


def test_is_user_edited_mtime_later(tmp_path):
    """mtime 明显晚于蒸馏时间应判定为编辑过"""
    fp = tmp_path / "test.md"
    fp.write_text("content")
    # 设置 mtime 为现在（naive datetime）
    now = datetime.now()
    os.utime(fp, (now.timestamp(), now.timestamp()))
    fm = {"distilled_at": "2020-01-01 00:00:00"}
    edited, reason = wr._is_user_edited(fp, fm)
    assert edited is True
    assert "mtime" in reason


def test_is_user_edited_mtime_within_hour(tmp_path):
    """mtime 在蒸馏时间 1 小时内应判定为未编辑"""
    fp = tmp_path / "test.md"
    fp.write_text("content")
    distill_dt = datetime.now() - timedelta(minutes=30)
    # 设置 mtime 为蒸馏时间 + 30 分钟（仍在 1 小时内），使用 naive datetime
    mtime_dt = distill_dt + timedelta(minutes=30)
    os.utime(fp, (mtime_dt.timestamp(), mtime_dt.timestamp()))
    fm = {"distilled_at": distill_dt.strftime("%Y-%m-%d %H:%M:%S")}
    edited, reason = wr._is_user_edited(fp, fm)
    assert edited is False
    assert "未检测到" in reason


def test_is_user_edited_no_distill_time():
    """无蒸馏时间应保守返回未编辑"""
    fp = Path("/tmp/fake.md")
    edited, reason = wr._is_user_edited(fp, {})
    assert edited is False
    assert "无法解析" in reason


def test_is_user_edited_no_explicit_no_time():
    """无显式标记且无蒸馏时间应返回未编辑"""
    fp = Path("/tmp/fake.md")
    edited, reason = wr._is_user_edited(fp, {"other": "value"})
    assert edited is False


# ---------------------------------------------------------------------------
# 5. _get_session_id_from_fm
# ---------------------------------------------------------------------------


def test_get_session_id_from_fm_chinese_key():
    """中文键 来源会话 应正确提取"""
    assert wr._get_session_id_from_fm({"来源会话": "sess-123"}) == "sess-123"


def test_get_session_id_from_fm_english_key():
    """英文键 source_session 应正确提取"""
    assert wr._get_session_id_from_fm({"source_session": "abc456"}) == "abc456"


def test_get_session_id_from_fm_session_id_key():
    """session_id 键应正确提取"""
    assert wr._get_session_id_from_fm({"session_id": "xyz789"}) == "xyz789"


def test_get_session_id_from_fm_empty():
    """无 session_id 应返回空字符串"""
    assert wr._get_session_id_from_fm({}) == ""


def test_get_session_id_from_fm_strips_whitespace():
    """应去除前后空白"""
    assert wr._get_session_id_from_fm({"source_session": "  abc  "}) == "abc"


# ---------------------------------------------------------------------------
# 6. _fetch_l1_for_session
# ---------------------------------------------------------------------------

from core.sync_framework.storage_backend import StorageResult  # noqa: E402


def test_fetch_l1_for_session_success(mock_backend):
    """正常查询应返回匹配的 L1 记录"""
    mock_backend.list_by_tags.return_value = [
        StorageResult(
            uid="m1",
            content="a",
            tags=["layer=L1", "session=s1"],
            metadata={},
            created_at="2024-01-01T00:00:00Z",
            updated_at="",
        ),
        StorageResult(
            uid="m2",
            content="b",
            tags=["layer=L1", "session=s1"],
            metadata={},
            created_at="2024-01-01T00:00:00Z",
            updated_at="",
        ),
    ]
    result = wr._fetch_l1_for_session("s1", mock_backend)
    assert len(result) == 2
    mock_backend.list_by_tags.assert_called_once()


def test_fetch_l1_for_session_empty_session_id(mock_backend):
    """空 session_id 应直接返回空列表"""
    result = wr._fetch_l1_for_session("", mock_backend)
    assert result == []
    mock_backend.list_by_tags.assert_not_called()


def test_fetch_l1_for_session_exception(mock_backend):
    """查询异常应返回空列表不抛异常"""
    mock_backend.list_by_tags.side_effect = RuntimeError("network down")
    result = wr._fetch_l1_for_session("s1", mock_backend)
    assert result == []


# ---------------------------------------------------------------------------
# 7. analyze_page
# ---------------------------------------------------------------------------


def test_analyze_page_valid_distilled_page(tmp_path):
    """有效的蒸馏页面应返回完整 PageAnalysis"""
    fp = tmp_path / "page.md"
    fm = {
        "source_session": "sess-abc",
        "distilled_at": "2024-01-01T00:00:00Z",
        "summary": "a" * 50,
        "name": "Test",
    }
    body = "## 怎么用\n\nUsage\n\n## 核心内容\n\nCore\n\n"
    fp.write_text(write_frontmatter(fm, body), encoding="utf-8")

    result = wr.analyze_page(fp)
    assert result is not None
    assert result.path == fp
    assert result.session_id == "sess-abc"
    assert result.readability_score > 0
    assert result.is_user_edited is False


def test_analyze_page_no_frontmatter(tmp_path):
    """无 frontmatter 应返回 None"""
    fp = tmp_path / "page.md"
    fp.write_text("Just plain markdown without frontmatter.\n")
    assert wr.analyze_page(fp) is None


def test_analyze_page_no_distill_marker(tmp_path):
    """无蒸馏标记的页面应返回 None"""
    fp = tmp_path / "page.md"
    fm = {"title": "Normal Note", "tags": ["idea"]}
    body = "Some personal note."
    fp.write_text(write_frontmatter(fm, body), encoding="utf-8")
    assert wr.analyze_page(fp) is None


def test_analyze_page_with_source_agent(tmp_path):
    """来源字段为已知 agent 应被识别为蒸馏页面"""
    fp = tmp_path / "page.md"
    fm = {
        "source": "claude",
        "summary": "a" * 50,
        "name": "Test",
        "distilled_at": "2024-01-01T00:00:00Z",
    }
    body = "## 怎么用\n\nUsage\n\n"
    fp.write_text(write_frontmatter(fm, body), encoding="utf-8")
    result = wr.analyze_page(fp)
    assert result is not None
    assert result.session_id == ""


def test_analyze_page_user_edited(tmp_path):
    """用户编辑过的页面应正确标记"""
    fp = tmp_path / "page.md"
    fm = {
        "source_session": "sess-xyz",
        "distilled_at": "2020-01-01T00:00:00Z",
        "user_edited": True,
    }
    body = "Some content."
    fp.write_text(write_frontmatter(fm, body), encoding="utf-8")
    result = wr.analyze_page(fp)
    assert result is not None
    assert result.is_user_edited is True


def test_analyze_page_unreadable_file(tmp_path):
    """无法读取的文件应返回 None 不抛异常"""
    fp = tmp_path / "page.md"
    fp.write_text("content")
    # 移除读权限
    fp.chmod(0o000)
    try:
        result = wr.analyze_page(fp)
        assert result is None
    finally:
        fp.chmod(0o644)


# ---------------------------------------------------------------------------
# 8. scan_wiki_pages
# ---------------------------------------------------------------------------


def test_scan_wiki_pages_finds_distilled_pages(fake_wiki_dir):
    """应正确扫描并识别蒸馏页面"""
    # 创建蒸馏页面
    p1 = fake_wiki_dir / "01-Projects" / "proj.md"
    p1.parent.mkdir(parents=True)
    p1.write_text(
        write_frontmatter(
            {"source_session": "s1", "distilled_at": "2024-01-01T00:00:00Z"}, "## 核心内容\n\nBody"
        ),
        encoding="utf-8",
    )

    # 创建普通页面（无蒸馏标记）
    p2 = fake_wiki_dir / "02-Areas" / "note.md"
    p2.parent.mkdir(parents=True)
    p2.write_text(write_frontmatter({"title": "Personal Note"}, "Just a note."), encoding="utf-8")

    results = wr.scan_wiki_pages(fake_wiki_dir)
    assert len(results) == 1
    assert results[0].path.name == "proj.md"


def test_scan_wiki_pages_skips_system_pages(fake_wiki_dir):
    """应跳过系统页面如 index.md"""
    for name in ["index.md", "log.md", "readme.md", "graph-index.md"]:
        fp = fake_wiki_dir / name
        fp.write_text(write_frontmatter({"source_session": "s1"}, "body"), encoding="utf-8")

    results = wr.scan_wiki_pages(fake_wiki_dir)
    assert len(results) == 0


def test_scan_wiki_pages_empty_dir(fake_wiki_dir):
    """空目录应返回空列表"""
    assert wr.scan_wiki_pages(fake_wiki_dir) == []


def test_scan_wiki_pages_nonexistent_dir():
    """不存在的目录应返回空列表"""
    assert wr.scan_wiki_pages(Path("/nonexistent/path")) == []


# ---------------------------------------------------------------------------
# 9. filter_pages_for_rebuild
# ---------------------------------------------------------------------------


def test_filter_pages_selects_low_quality_unedited():
    """低质量 + 未编辑 + 有 session_id 应被选中"""
    a = wr.PageAnalysis(
        path=Path("/tmp/p.md"),
        frontmatter={"source_session": "s1"},
        body="",
        readability_score=30.0,
        is_user_edited=False,
        session_id="s1",
    )
    selected = wr.filter_pages_for_rebuild([a], min_readability=60.0)
    assert len(selected) == 1
    assert selected[0].selected_for_rebuild is True


def test_filter_pages_skips_high_quality():
    """高质量页面应被跳过"""
    a = wr.PageAnalysis(
        path=Path("/tmp/p.md"),
        frontmatter={"source_session": "s1"},
        body="",
        readability_score=80.0,
        is_user_edited=False,
        session_id="s1",
    )
    selected = wr.filter_pages_for_rebuild([a], min_readability=60.0)
    assert len(selected) == 0
    assert a.skip_reason != ""
    assert "可读性" in a.skip_reason


def test_filter_pages_skips_edited():
    """已编辑页面默认应被跳过"""
    a = wr.PageAnalysis(
        path=Path("/tmp/p.md"),
        frontmatter={"source_session": "s1"},
        body="",
        readability_score=30.0,
        is_user_edited=True,
        edit_detection_reason="frontmatter 显式标记 user_edited",
        session_id="s1",
    )
    selected = wr.filter_pages_for_rebuild([a], min_readability=60.0, include_edited=False)
    assert len(selected) == 0
    assert "手工编辑" in a.skip_reason


def test_filter_pages_includes_edited_when_flag_set():
    """include_edited=True 时应包含已编辑页面"""
    a = wr.PageAnalysis(
        path=Path("/tmp/p.md"),
        frontmatter={"source_session": "s1"},
        body="",
        readability_score=30.0,
        is_user_edited=True,
        edit_detection_reason="frontmatter 显式标记 user_edited",
        session_id="s1",
    )
    selected = wr.filter_pages_for_rebuild([a], min_readability=60.0, include_edited=True)
    assert len(selected) == 1


def test_filter_pages_skips_no_session():
    """无 session_id 的页面应被跳过"""
    a = wr.PageAnalysis(
        path=Path("/tmp/p.md"),
        frontmatter={},
        body="",
        readability_score=30.0,
        is_user_edited=False,
        session_id="",
    )
    selected = wr.filter_pages_for_rebuild([a], min_readability=60.0)
    assert len(selected) == 0
    assert "无来源会话" in a.skip_reason


def test_filter_pages_marks_has_l1_source():
    """有 session_id 的页面应被标记 has_l1_source"""
    a = wr.PageAnalysis(
        path=Path("/tmp/p.md"),
        frontmatter={"source_session": "s1"},
        body="",
        readability_score=80.0,
        is_user_edited=False,
        session_id="s1",
    )
    wr.filter_pages_for_rebuild([a], min_readability=60.0)
    assert a.has_l1_source is True


# ---------------------------------------------------------------------------
# 10. generate_dry_run_report
# ---------------------------------------------------------------------------


def test_generate_dry_run_report_structure(fake_wiki_dir):
    """报告应包含预期结构"""
    a1 = wr.PageAnalysis(
        path=fake_wiki_dir / "p1.md",
        frontmatter={},
        body="",
        readability_score=30.0,
        session_id="sess-abc",
        l1_count=2,
        selected_for_rebuild=True,
    )
    a2 = wr.PageAnalysis(
        path=fake_wiki_dir / "p2.md",
        frontmatter={},
        body="",
        readability_score=80.0,
        session_id="sess-def",
        selected_for_rebuild=False,
        skip_reason="可读性足够",
    )
    report = wr.generate_dry_run_report([a1, a2], [a1], 60.0)
    assert "Wiki 选择性重跑" in report
    assert "Dry Run 报告" in report
    assert "选中重跑" in report
    assert "跳过的页面" in report
    assert "p1.md" in report
    assert "p2.md" in report
    assert "sess-abc" in report
    assert "L1 records: 2" in report


def test_generate_dry_run_report_shows_truncated_warning(fake_wiki_dir):
    """截断页面应在报告中显示警告"""
    a = wr.PageAnalysis(
        path=fake_wiki_dir / "p1.md",
        frontmatter={},
        body="",
        readability_score=30.0,
        readability_detail={"truncated": True, "missing_sections": [], "quality_score": 40.0},
        session_id="s1",
        selected_for_rebuild=True,
    )
    report = wr.generate_dry_run_report([a], [a], 60.0)
    assert "截断" in report


def test_generate_dry_run_report_shows_low_quality(fake_wiki_dir):
    """低质量分应在报告中显示"""
    a = wr.PageAnalysis(
        path=fake_wiki_dir / "p1.md",
        frontmatter={},
        body="",
        readability_score=30.0,
        readability_detail={
            "truncated": False,
            "missing_sections": ["怎么用"],
            "quality_score": 30.0,
        },
        session_id="s1",
        selected_for_rebuild=True,
    )
    report = wr.generate_dry_run_report([a], [a], 60.0)
    assert "缺失章节" in report
    assert "质量分较低" in report


def test_generate_dry_run_report_limits_skipped(fake_wiki_dir):
    """跳过的页面应只显示前20个"""
    analyses = []
    for i in range(25):
        a = wr.PageAnalysis(
            path=fake_wiki_dir / f"p{i}.md",
            frontmatter={},
            body="",
            readability_score=80.0,
            session_id="s1",
            selected_for_rebuild=False,
            skip_reason="可读性足够",
        )
        analyses.append(a)
    report = wr.generate_dry_run_report(analyses, [], 60.0)
    assert "还有 5 个" in report


# ---------------------------------------------------------------------------
# 11. backup_page
# ---------------------------------------------------------------------------


def test_backup_page_copies_file(fake_wiki_dir):
    """备份应正确复制文件到备份目录"""
    page = fake_wiki_dir / "01-Projects" / "test.md"
    page.parent.mkdir(parents=True)
    page.write_text("original content", encoding="utf-8")

    backup_dir = fake_wiki_dir / ".backup"
    result = wr.backup_page(page, backup_dir)

    assert result.exists()
    assert result.read_text(encoding="utf-8") == "original content"
    assert result.relative_to(backup_dir) == Path("01-Projects/test.md")


def test_backup_page_nested_dirs(fake_wiki_dir):
    """备份应创建嵌套目录结构"""
    page = fake_wiki_dir / "a" / "b" / "c" / "deep.md"
    page.parent.mkdir(parents=True)
    page.write_text("deep", encoding="utf-8")

    backup_dir = fake_wiki_dir / ".backup"
    result = wr.backup_page(page, backup_dir)
    assert result.exists()


# ---------------------------------------------------------------------------
# 12. rebuild_single_page
# ---------------------------------------------------------------------------


def test_rebuild_single_page_dry_run():
    """dry_run=True 应直接返回 success=True 不执行操作"""
    analysis = wr.PageAnalysis(
        path=Path("/tmp/p.md"),
        frontmatter={"source_session": "s1"},
        body="",
        session_id="s1",
    )
    result = wr.rebuild_single_page(analysis, Mock(), Mock(), dry_run=True)
    assert result.success is True
    assert result.new_paths == []


def test_rebuild_single_page_backup_failure(monkeypatch):
    """备份失败应返回错误"""
    analysis = wr.PageAnalysis(
        path=Path("/tmp/p.md"),
        frontmatter={"source_session": "s1"},
        body="",
        session_id="s1",
    )
    monkeypatch.setattr(
        wr, "backup_page", lambda p, b: (_ for _ in ()).throw(OSError("disk full"))
    )
    result = wr.rebuild_single_page(
        analysis, Mock(), Mock(), dry_run=False, backup_dir=Path("/backup")
    )
    assert result.success is False
    assert "备份失败" in result.error


def test_rebuild_single_page_no_l1_records(mock_backend, monkeypatch):
    """StorageBackend 中找不到记录应返回错误"""
    monkeypatch.setattr(wr, "backup_page", lambda p, b: b / "p.md")
    mock_backend.list_by_tags.return_value = []
    analysis = wr.PageAnalysis(
        path=Path("/tmp/p.md"),
        frontmatter={"source_session": "s1"},
        body="",
        session_id="s1",
    )
    result = wr.rebuild_single_page(
        analysis, mock_backend, Mock(), dry_run=False, backup_dir=Path("/backup")
    )
    assert result.success is False
    assert "找不到" in result.error


def test_rebuild_single_page_reconstruct_empty(mock_backend, monkeypatch):
    """重建会话为空应返回错误"""
    monkeypatch.setattr(wr, "backup_page", lambda p, b: b / "p.md")
    mock_backend.list_by_tags.return_value = [
        StorageResult(
            uid="m1",
            content="",
            tags=["layer=L1", "session=s1"],
            metadata={},
            created_at="",
            updated_at="",
        )
    ]
    monkeypatch.setattr(wr, "reconstruct_session", lambda records: ([], {}))
    analysis = wr.PageAnalysis(
        path=Path("/tmp/p.md"),
        frontmatter={"source_session": "s1"},
        body="",
        session_id="s1",
    )
    result = wr.rebuild_single_page(
        analysis, mock_backend, Mock(), dry_run=False, backup_dir=Path("/backup")
    )
    assert result.success is False
    assert "重建会话失败" in result.error


def test_rebuild_single_page_success(mock_backend, mock_engine, monkeypatch, tmp_path):
    """正常重跑应成功并返回新路径"""
    monkeypatch.setattr(wr, "backup_page", lambda p, b: b / "p.md")
    monkeypatch.setattr(
        wr,
        "reconstruct_session",
        lambda records: ([{"role": "user", "content": "hi"}], {"source": "claude"}),
    )
    monkeypatch.setattr(wr, "score_session", lambda msgs: (75.0, {}))
    monkeypatch.setattr(wr, "_mark_processed", Mock())
    monkeypatch.setattr(wr, "_link_session_records_to_wiki", Mock())
    monkeypatch.setattr(wr, "_log", Mock())

    mock_backend.list_by_tags.return_value = [
        StorageResult(
            uid="m1",
            content="",
            tags=["layer=L1", "session=s1"],
            metadata={},
            created_at="",
            updated_at="",
        )
    ]

    distill_result = DistillationResult(
        session_id="s1",
        judgment="knowledge",
        judgment_reason="has knowledge",
        fragments=[MagicMock()],
    )
    mock_engine.process.return_value = distill_result
    mock_engine.write_pages.return_value = ["00-Inbox/s1-insight.md"]

    # 创建真实文件以便 unlink
    page_path = tmp_path / "p.md"
    page_path.write_text("old content", encoding="utf-8")

    analysis = wr.PageAnalysis(
        path=page_path,
        frontmatter={"source_session": "s1"},
        body="",
        session_id="s1",
    )
    result = wr.rebuild_single_page(
        analysis, mock_backend, mock_engine, dry_run=False, backup_dir=tmp_path / "backup"
    )
    assert result.success is True
    assert result.new_paths == ["00-Inbox/s1-insight.md"]
    assert result.l1_count == 1
    assert not page_path.exists()  # 旧文件应被删除


def test_rebuild_single_page_not_knowledge(mock_backend, mock_engine, monkeypatch):
    """蒸馏判断非 knowledge 应返回错误"""
    monkeypatch.setattr(wr, "backup_page", lambda p, b: b / "p.md")
    monkeypatch.setattr(
        wr,
        "reconstruct_session",
        lambda records: ([{"role": "user", "content": "hi"}], {"source": "claude"}),
    )
    monkeypatch.setattr(wr, "score_session", lambda msgs: (75.0, {}))
    mock_backend.list_by_tags.return_value = [
        StorageResult(
            uid="m1",
            content="",
            tags=["layer=L1", "session=s1"],
            metadata={},
            created_at="",
            updated_at="",
        )
    ]

    distill_result = DistillationResult(
        session_id="s1",
        judgment="skip",
        judgment_reason="low value",
    )
    mock_engine.process.return_value = distill_result

    analysis = wr.PageAnalysis(
        path=Path("/tmp/p.md"),
        frontmatter={"source_session": "s1"},
        body="",
        session_id="s1",
    )
    result = wr.rebuild_single_page(
        analysis, mock_backend, mock_engine, dry_run=False, backup_dir=Path("/backup")
    )
    assert result.success is False
    assert "skip" in result.error


def test_rebuild_single_page_engine_exception(mock_backend, mock_engine, monkeypatch):
    """蒸馏引擎抛异常应返回错误"""
    monkeypatch.setattr(wr, "backup_page", lambda p, b: b / "p.md")
    monkeypatch.setattr(
        wr,
        "reconstruct_session",
        lambda records: ([{"role": "user", "content": "hi"}], {"source": "claude"}),
    )
    monkeypatch.setattr(wr, "score_session", lambda msgs: (75.0, {}))
    mock_backend.list_by_tags.return_value = [
        StorageResult(
            uid="m1",
            content="",
            tags=["layer=L1", "session=s1"],
            metadata={},
            created_at="",
            updated_at="",
        )
    ]
    mock_engine.process.side_effect = RuntimeError("LLM timeout")

    analysis = wr.PageAnalysis(
        path=Path("/tmp/p.md"),
        frontmatter={"source_session": "s1"},
        body="",
        session_id="s1",
    )
    result = wr.rebuild_single_page(
        analysis, mock_backend, mock_engine, dry_run=False, backup_dir=Path("/backup")
    )
    assert result.success is False
    assert "蒸馏失败" in result.error


# ---------------------------------------------------------------------------
# 13. run_selective_rebuild
# ---------------------------------------------------------------------------


def test_run_selective_rebuild_empty_selection(fake_wiki_dir, monkeypatch):
    """无选中页面时应生成报告并返回"""
    monkeypatch.setattr(wr, "_ensure_wiki_dirs", Mock())
    result = wr.run_selective_rebuild(dry_run=False, min_readability=60.0)
    assert result["total_scanned"] == 0
    assert result["selected"] == 0
    assert result["success"] == 0
    assert result["failed"] == 0
    assert "report_path" in result


def test_run_selective_rebuild_dry_run(fake_wiki_dir, monkeypatch, mock_backend):
    """dry_run=True 时应生成报告不执行重跑"""
    monkeypatch.setattr(wr, "_ensure_wiki_dirs", Mock())
    mock_backend.list_by_tags.return_value = [
        StorageResult(
            uid="m1",
            content="",
            tags=["layer=L1", "session=s1"],
            metadata={},
            created_at="",
            updated_at="",
        )
    ]

    # 创建低质量蒸馏页面
    p = fake_wiki_dir / "00-Inbox" / "low.md"
    p.parent.mkdir(parents=True)
    p.write_text(
        write_frontmatter(
            {"source_session": "s1", "distilled_at": "2024-01-01T00:00:00Z"}, "bad content"
        ),
        encoding="utf-8",
    )

    result = wr.run_selective_rebuild(dry_run=True, min_readability=60.0, backend=mock_backend)
    assert result["dry_run"] is True
    assert result["selected"] >= 1
    assert result["success"] == 0
    assert result["l1_total"] == 1
    assert "report_path" in result
    assert "L1 records: 1" in Path(result["report_path"]).read_text(encoding="utf-8")


def test_run_selective_rebuild_full_pipeline(fake_wiki_dir, monkeypatch, mock_backend, mock_engine):
    """完整重跑流程应正确执行"""
    monkeypatch.setattr(wr, "_ensure_wiki_dirs", Mock())
    monkeypatch.setattr(wr, "update_index_md", Mock())
    monkeypatch.setattr(wr, "_git_auto_commit", Mock())
    monkeypatch.setattr(wr, "_mark_processed", Mock())
    monkeypatch.setattr(wr, "_link_session_records_to_wiki", Mock())
    monkeypatch.setattr(wr, "_log", Mock())
    monkeypatch.setattr(
        wr,
        "reconstruct_session",
        lambda records: ([{"role": "user", "content": "hi"}], {"source": "claude"}),
    )
    monkeypatch.setattr(wr, "score_session", lambda msgs: (75.0, {}))

    mock_backend.list_by_tags.return_value = [
        StorageResult(
            uid="m1",
            content="",
            tags=["layer=L1", "session=s1"],
            metadata={},
            created_at="",
            updated_at="",
        )
    ]

    distill_result = DistillationResult(
        session_id="s1",
        judgment="knowledge",
        judgment_reason="has knowledge",
        fragments=[MagicMock()],
    )
    mock_engine.process.return_value = distill_result
    mock_engine.write_pages.return_value = ["00-Inbox/s1-insight.md"]
    monkeypatch.setattr(wr, "DistillationEngine", lambda: mock_engine)

    # 创建低质量蒸馏页面
    p = fake_wiki_dir / "00-Inbox" / "low.md"
    p.parent.mkdir(parents=True)
    p.write_text(
        write_frontmatter(
            {"source_session": "s1", "distilled_at": "2024-01-01T00:00:00Z"}, "bad content"
        ),
        encoding="utf-8",
    )

    result = wr.run_selective_rebuild(dry_run=False, min_readability=60.0, backend=mock_backend)
    assert result["dry_run"] is False
    assert result["selected"] >= 1
    assert result["success"] >= 1
    assert result["l1_total"] >= 1
    assert "report_path" in result
    assert "L1 records: 1" in Path(result["report_path"]).read_text(encoding="utf-8")
    assert "backup_dir" in result


def test_run_selective_rebuild_backend_creation_fails(fake_wiki_dir, monkeypatch):
    """backend 自动创建失败时应返回错误"""
    monkeypatch.setattr(wr, "_ensure_wiki_dirs", Mock())

    # 创建低质量页面
    p = fake_wiki_dir / "00-Inbox" / "low.md"
    p.parent.mkdir(parents=True)
    p.write_text(
        write_frontmatter({"source_session": "s1", "distilled_at": "2024-01-01T00:00:00Z"}, "bad"),
        encoding="utf-8",
    )

    # 模拟 backend 创建失败
    monkeypatch.setattr(wr, "get_config", lambda: MagicMock(storage_backend="obsidian"))

    def _raise(*args, **kwargs):
        raise RuntimeError("vault not found")

    monkeypatch.setattr("integrations.backends.ObsidianBackend", _raise)

    result = wr.run_selective_rebuild(dry_run=False, min_readability=60.0)
    assert "error" in result


def test_run_selective_rebuild_with_backup_dir(
    fake_wiki_dir, monkeypatch, mock_backend, mock_engine
):
    """自定义备份目录应被使用"""
    monkeypatch.setattr(wr, "_ensure_wiki_dirs", Mock())
    monkeypatch.setattr(wr, "update_index_md", Mock())
    monkeypatch.setattr(wr, "_git_auto_commit", Mock())
    monkeypatch.setattr(wr, "_mark_processed", Mock())
    monkeypatch.setattr(wr, "_link_session_records_to_wiki", Mock())
    monkeypatch.setattr(wr, "_log", Mock())
    monkeypatch.setattr(
        wr,
        "reconstruct_session",
        lambda records: ([{"role": "user", "content": "hi"}], {"source": "claude"}),
    )
    monkeypatch.setattr(wr, "score_session", lambda msgs: (75.0, {}))

    mock_backend.list_by_tags.return_value = [
        StorageResult(
            uid="m1",
            content="",
            tags=["layer=L1", "session=s1"],
            metadata={},
            created_at="",
            updated_at="",
        )
    ]

    distill_result = DistillationResult(
        session_id="s1",
        judgment="knowledge",
        judgment_reason="has knowledge",
        fragments=[MagicMock()],
    )
    mock_engine.process.return_value = distill_result
    mock_engine.write_pages.return_value = ["00-Inbox/s1-insight.md"]
    monkeypatch.setattr(wr, "DistillationEngine", lambda: mock_engine)

    p = fake_wiki_dir / "00-Inbox" / "low.md"
    p.parent.mkdir(parents=True)
    p.write_text(
        write_frontmatter({"source_session": "s1", "distilled_at": "2024-01-01T00:00:00Z"}, "bad"),
        encoding="utf-8",
    )

    custom_backup = fake_wiki_dir / "custom_backup"
    result = wr.run_selective_rebuild(
        dry_run=False, min_readability=60.0, backend=mock_backend, backup_dir=custom_backup
    )
    assert result["backup_dir"] == str(custom_backup)
    assert custom_backup.exists()


# ---------------------------------------------------------------------------
# 14. PageAnalysis / RebuildResult dataclass defaults
# ---------------------------------------------------------------------------


def test_page_analysis_defaults():
    """PageAnalysis 默认值应正确"""
    pa = wr.PageAnalysis(
        path=Path("/tmp/p.md"),
        frontmatter={},
        body="",
    )
    assert pa.readability_score == 0.0
    assert pa.readability_detail == {}
    assert pa.is_user_edited is False
    assert pa.has_l1_source is False
    assert pa.selected_for_rebuild is False


def test_rebuild_result_defaults():
    """RebuildResult 默认值应正确"""
    rr = wr.RebuildResult(page_path=Path("/tmp/p.md"), success=False)
    assert rr.new_paths == []
    assert rr.error == ""
    assert rr.backed_up_to is None


# ---------------------------------------------------------------------------
# 15. Integration-style: analyze_page + filter_pages + generate_dry_run_report
# ---------------------------------------------------------------------------


def test_analyze_filter_report_integration(fake_wiki_dir):
    """端到端：分析 → 筛选 → 生成报告"""
    # 创建高质量页面
    p1 = fake_wiki_dir / "good.md"
    p1.write_text(
        write_frontmatter(
            {
                "source_session": "s-good",
                "distilled_at": "2024-01-01T00:00:00Z",
                "summary": "a" * 50,
                "name": "Good Page",
                "domain": "tech",
                "evidence_level": "high",
                "confidence": 0.9,
                "temporal_scope": "permanent",
                "quality_score": 80,
            },
            "## 怎么用\n\nUsage\n\n## 可信度提示\n\nCaveats\n\n"
            "## 背景\n\nBackground\n\n## 核心内容\n\nCore\n\n",
        ),
        encoding="utf-8",
    )

    # 创建低质量页面
    p2 = fake_wiki_dir / "bad.md"
    p2.write_text(
        write_frontmatter(
            {"source_session": "s-bad", "distilled_at": "2024-01-01T00:00:00Z"},
            "barely any content",
        ),
        encoding="utf-8",
    )

    analyses = wr.scan_wiki_pages(fake_wiki_dir)
    assert len(analyses) == 2

    selected = wr.filter_pages_for_rebuild(analyses, min_readability=60.0)
    # 高质量页面应被跳过，低质量应被选中
    assert len(selected) == 1
    assert selected[0].session_id == "s-bad"

    report = wr.generate_dry_run_report(analyses, selected, 60.0)
    assert "good.md" in report
    assert "bad.md" in report
