"""Characterization tests for scripts/wiki_lint.py output helpers."""

import sys
import json
from unittest.mock import patch

import pytest

from scripts import wiki_lint


class TestSummarizeSeverity:
    def test_counts_severity_buckets(self):
        results = [
            {"severity": "error", "issues": []},
            {"severity": "warning", "issues": []},
            {"severity": "ok", "issues": []},
            {"severity": "error", "issues": []},
        ]
        assert wiki_lint._summarize_severity(results) == (4, 2, 1, 1)

    def test_empty_results(self):
        assert wiki_lint._summarize_severity([]) == (0, 0, 0, 0)


class TestWikiQualityReport:
    def test_builds_stable_schema_budget_state_and_manual_review(self, tmp_path):
        results = [
            {
                "page": "broken.md",
                "severity": "error",
                "issues": [{"type": "broken_link", "msg": "坏链接: [[missing]]"}],
            },
            {
                "page": "meta.md",
                "severity": "warning",
                "issues": [{"type": "missing_meta", "msg": "缺少 status"}],
            },
            {
                "page": "ok.md",
                "severity": "ok",
                "issues": [],
            },
        ]

        report = wiki_lint.build_quality_report(results, vault_dir=tmp_path)

        assert report["schema_version"] == "mnemos.wiki_quality.v1"
        assert report["summary"]["issue_counts"]["broken_link"] == 1
        assert report["state_machine"]["missing_meta"]["auto_fixable"] is True
        assert report["state_machine"]["broken_link"]["lifecycle_status"] == "needs_user"
        assert report["manual_review"]["broken_link"]["count"] == 1
        assert report["scorecard"]["dimension"] == "obsidian_experience"
        assert report["budgets"]["ok"] is False

    def test_budget_file_can_override_warning_limits(self, tmp_path):
        budget_file = tmp_path / "budget.json"
        budget_file.write_text(
            json.dumps({"budgets": {"orphan": {"limit": 2, "owner": "qa"}}}),
            encoding="utf-8",
        )

        overrides = wiki_lint._load_budget_overrides(str(budget_file))
        lines = wiki_lint._budget_lines({"orphan": 2}, overrides)
        orphan = next(line for line in lines if line["issue_type"] == "orphan")

        assert orphan["ok"] is True
        assert orphan["owner"] == "qa"


class TestRenderIssueSection:
    def test_renders_error_section_with_filter(self):
        results = [
            {
                "page": "bad.md",
                "severity": "error",
                "issues": [
                    {"type": "broken_link", "msg": "坏链接: [[x]]"},
                    {"type": "stub", "msg": "stub"},
                ],
            }
        ]
        lines = wiki_lint._render_issue_section(
            "错误",
            results,
            lambda r: r["severity"] == "error",
            lambda issue: issue["type"] in ("no_frontmatter", "broken_link"),
        )
        assert "## 错误" in lines
        assert "- **bad.md**" in lines
        assert "  - 坏链接: [[x]]" in lines
        assert "stub" not in "\n".join(lines)

    def test_skips_empty_sections(self):
        results = [{"page": "ok.md", "severity": "ok", "issues": []}]
        lines = wiki_lint._render_issue_section(
            "错误", results, lambda r: r["severity"] == "error"
        )
        assert lines == []

    def test_renders_warning_section(self):
        results = [
            {
                "page": "warn.md",
                "severity": "warning",
                "issues": [{"type": "stub", "msg": "太短"}],
            }
        ]
        lines = wiki_lint._render_issue_section(
            "警告", results, lambda r: r["severity"] == "warning"
        )
        assert "## 警告" in lines
        assert "- **warn.md**" in lines
        assert "  - 太短" in lines


class TestRenderIssueCounts:
    def test_counts_sorted_descending(self):
        results = [
            {
                "page": "a.md",
                "severity": "error",
                "issues": [
                    {"type": "broken_link"},
                    {"type": "broken_link"},
                    {"type": "no_frontmatter"},
                ],
            },
            {
                "page": "b.md",
                "severity": "warning",
                "issues": [{"type": "stub"}],
            },
        ]
        lines = wiki_lint._render_issue_counts(results)
        text = "\n".join(lines)
        assert "## 问题统计" in text
        assert "- broken_link: 2" in text
        assert "- no_frontmatter: 1" in text
        assert "- stub: 1" in text
        # Sorted by count descending
        assert text.index("broken_link") < text.index("no_frontmatter")


class TestRenderRecommendations:
    def test_both_errors_and_warnings(self):
        lines = wiki_lint._render_recommendations(1, 1)
        text = "\n".join(lines)
        assert "## 修复建议" in text
        assert "1. **优先修复错误**" in text
        assert "2. **处理警告**" in text
        assert "3. **定期运行**" in text

    def test_only_warnings(self):
        lines = wiki_lint._render_recommendations(0, 1)
        text = "\n".join(lines)
        assert "1. **优先修复错误**" not in text
        assert "2. **处理警告**" in text

    def test_clean(self):
        lines = wiki_lint._render_recommendations(0, 0)
        text = "\n".join(lines)
        assert "1." not in text
        assert "2." not in text
        assert "3. **定期运行**" in text


class TestGenerateReport:
    def test_chinese_markdown_format(self):
        results = [
            {
                "page": "broken.md",
                "severity": "error",
                "issues": [{"type": "broken_link", "msg": "坏链接: [[missing]]"}],
            },
            {
                "page": "warn.md",
                "severity": "warning",
                "issues": [{"type": "stub", "msg": "内容过短（50 字符，阈值 200）"}],
            },
            {"page": "ok.md", "severity": "ok", "issues": []},
        ]
        report = wiki_lint.generate_report(results)
        assert report.startswith("# Wiki Lint 报告")
        assert "总页面: 3" in report
        assert "  - 健康: 1" in report
        assert "  - 警告: 1" in report
        assert "  - 错误: 1" in report
        assert "## 错误" in report
        assert "## 警告" in report
        assert "## 问题统计" in report
        assert "## 修复建议" in report
        assert "坏链接: [[missing]]" in report
        assert "内容过短" in report

    def test_no_errors_or_warnings(self):
        results = [{"page": "ok.md", "severity": "ok", "issues": []}]
        report = wiki_lint.generate_report(results)
        assert "## 错误" not in report
        assert "## 警告" not in report
        assert "---" in report
        assert "## 修复建议" in report


class TestMainExitCode:
    def test_json_summary_stdout_is_parseable(self, tmp_path, monkeypatch, capsys):
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        (wiki_dir / "broken.md").write_text("See [[missing]].", encoding="utf-8")
        monkeypatch.setattr(wiki_lint, "WIKI_DIR", wiki_dir)

        with patch.object(
            sys,
            "argv",
            ["wiki_lint.py", "--json", "--summary", "--full"],
        ):
            with pytest.raises(SystemExit) as exc_info:
                wiki_lint.main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["schema_version"] == "mnemos.wiki_quality.v1"
        assert payload["summary"]["pages"] == 1
        assert payload["pages"][0]["page"] == "broken.md"
        assert "[Lint] 扫描 Wiki 目录" in captured.err

    def test_fix_records_action_ledger(self, tmp_path, monkeypatch, capsys):
        wiki_dir = tmp_path / "wiki"
        db_dir = tmp_path / "db"
        wiki_dir.mkdir()
        db_dir.mkdir()
        body_a = (
            "This page is long enough to avoid stub warnings and links to b. "
            "It keeps the test focused on missing metadata only. [[b]]\n"
        ) * 3
        body_b = (
            "This page is long enough to avoid stub warnings and links to a. "
            "It keeps the test focused on missing metadata only. [[a]]\n"
        ) * 3
        (wiki_dir / "a.md").write_text("---\ntitle: A\n---\n" + body_a, encoding="utf-8")
        (wiki_dir / "b.md").write_text(
            "---\nstatus: seed\nsource_count: 1\nknowledge_stage: 原始\nevidence_level: 单源\n---\n"
            + body_b,
            encoding="utf-8",
        )
        monkeypatch.setattr(wiki_lint, "WIKI_DIR", wiki_dir)

        class FakeConfig:
            database_dir = db_dir

        monkeypatch.setattr(wiki_lint, "get_config", lambda: FakeConfig())

        with patch.object(
            sys,
            "argv",
            ["wiki_lint.py", "--fix", "--json", "--summary"],
        ):
            wiki_lint.main()

        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["auto_fix"]["fixed"] == 1
        assert payload["action_ledger_ref"]

        from core.system_contracts import ActionLedger

        rows = ActionLedger(db_dir / "action_ledger.db").recent()
        assert rows[0]["action_id"] == payload["action_ledger_ref"]
        assert rows[0]["action_type"] == "wiki_quality_fix"
        assert rows[0]["verification"]["schema_version"] == "mnemos.wiki_quality.v1"

    def test_exits_one_when_errors_present(self, tmp_path, monkeypatch):
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        (wiki_dir / "broken.md").write_text("See [[missing]].", encoding="utf-8")
        (wiki_dir / "ok.md").write_text("Some content here.", encoding="utf-8")
        monkeypatch.setattr(wiki_lint, "WIKI_DIR", wiki_dir)

        with patch.object(sys, "argv", ["wiki_lint.py"]):
            with pytest.raises(SystemExit) as exc_info:
                wiki_lint.main()
        assert exc_info.value.code == 1

    def test_no_exit_when_clean(self, tmp_path, monkeypatch):
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        long_body_a = (
            "This page contains a substantial amount of content so that it does not "
            "trigger the stub warning threshold which is set to two hundred characters. "
            "It also links to another page to avoid being flagged as an orphan. [[b]]\n"
        )
        long_body_b = (
            "This page also contains a substantial amount of content so that it does not "
            "trigger the stub warning threshold which is set to two hundred characters. "
            "It links back to the first page to avoid being flagged as an orphan. [[a]]\n"
        )
        (wiki_dir / "a.md").write_text(
            "---\nstatus: seed\nsource_count: 1\nknowledge_stage: 原始\nevidence_level: 单源\n---\n"
            + long_body_a,
            encoding="utf-8",
        )
        (wiki_dir / "b.md").write_text(
            "---\nstatus: seed\nsource_count: 1\nknowledge_stage: 原始\nevidence_level: 单源\n---\n"
            + long_body_b,
            encoding="utf-8",
        )
        monkeypatch.setattr(wiki_lint, "WIKI_DIR", wiki_dir)

        with patch.object(sys, "exit") as mock_exit:
            with patch.object(sys, "argv", ["wiki_lint.py"]):
                wiki_lint.main()
        mock_exit.assert_not_called()
