"""Characterization tests for scripts/health_check.py report renderer."""

from unittest.mock import patch

from scripts import health_check


class TestRenderConfigSection:
    def test_ok_status(self):
        lines = health_check._render_config_section({"status": "ok"})
        assert "## Config Health (P5)" in lines
        assert "Status: OK — 无敏感文件未提交修改" in lines

    def test_warning_with_diff_truncation(self):
        lines = health_check._render_config_section(
            {
                "status": "warning",
                "uncommitted_files": ["config/main.json"],
                "diff_summary": "x" * 500,
            }
        )
        text = "\n".join(lines)
        assert "**Status: WARNING**" in text
        assert "- `config/main.json`" in text
        assert "```" in text
        assert "x" * 300 in text
        assert "x" * 301 not in text

    def test_error_status(self):
        lines = health_check._render_config_section(
            {"status": "error", "error": "git not found"}
        )
        assert "**Status: ERROR** — git not found" in lines


class TestRenderDatabaseSection:
    def test_status_emojis(self):
        db_health = {
            "live_sync": {
                "status": "ok",
                "size_mb": 0.2,
                "journal_mode": "wal",
            },
            "locked_db": {
                "status": "locked",
                "size_mb": 0.1,
                "journal_mode": "delete",
                "error": "database is locked",
            },
            "bad_db": {
                "status": "error",
                "size_mb": 0.3,
                "journal_mode": "delete",
                "error": "corrupt",
            },
        }
        lines = health_check._render_database_section(db_health)
        text = "\n".join(lines)
        assert "## Database Health (P6)" in text
        assert "- **live_sync**: OK | 0.2 MB | journal=wal" in text
        assert "- **locked_db**: LOCKED | 0.1 MB | journal=delete" in text
        assert "  - Error: database is locked" in text
        assert "- **bad_db**: ERR | 0.3 MB | journal=delete" in text
        assert "  - Error: corrupt" in text


class TestRenderRetentionSection:
    def test_emoji_and_oldest(self):
        retention = {
            "observations": {
                "status": "ok",
                "size_mb": 1.0,
                "oldest_record_age_days": 5.5,
                "journal_mode": "delete",
            },
            "missing": {
                "status": "missing",
            },
        }
        lines = health_check._render_retention_section(retention)
        text = "\n".join(lines)
        assert "## Retention Database Health (P0 4.1)" in text
        assert "- **observations**: OK | 1.0 MB | oldest=5.5d | journal=delete" in text
        assert "- **missing**: MISSING | ? MB | oldest=N/A | journal=?" in text


class TestRenderWikiMetricsSection:
    def test_ok_metrics(self):
        lines = health_check._render_wiki_metrics_section(
            {
                "status": "ok",
                "total_pages": 100,
                "new_this_month": 10,
                "avg_quality": 75.5,
                "avg_heat": 4.2,
                "stage_distribution": {"seed": 5, "mature": 95},
            }
        )
        text = "\n".join(lines)
        assert "## Wiki Metrics Stats" in text
        assert "- Total pages: 100" in text
        assert "- New this month: 10" in text
        assert "- Avg quality: 75.5/100" in text
        assert "- Avg heat: 4.2" in text
        assert "- Stage distribution: mature=95, seed=5" in text

    def test_error_metrics(self):
        lines = health_check._render_wiki_metrics_section(
            {"status": "error", "error": "table missing"}
        )
        assert "Error: table missing" in lines


class TestRenderWikiDirectorySection:
    def test_ok_directory(self):
        lines = health_check._render_wiki_directory_section(
            {"status": "ok", "total_md_files": 42, "by_directory": {"root": 10, "a/b": 32}}
        )
        text = "\n".join(lines)
        assert "## Wiki Directory" in text
        assert "Total .md files: 42" in text
        assert "- root: 10" in text
        assert "- a/b: 32" in text

    def test_error_directory(self):
        lines = health_check._render_wiki_directory_section(
            {"status": "error", "error": "not found"}
        )
        assert "Error: not found" in lines


class TestRenderSecuritySection:
    def test_renders_status_and_findings(self):
        sec = {
            "status": "warning",
            "keyring_available": True,
            "keyring_status": "accepted",
            "keyring_risk_level": "safe_but_not_best",
            "keyring_env_fallback_accepted": True,
            "keyring_safe_but_not_best": True,
            "keyring_backend": "keyring.backends.macOS.Keyring",
            "keyring_error": "unavailable",
            "legacy_key_rows": {"enc_rows": 1, "plaintext_rows": 2, "keyref_rows": 3},
            "pickle_findings": [("a.py", 1, "pickle import")],
            "weak_hash_findings": [("b.py", 2, "hashlib.md5()")],
            "permission_violations": ["cfg: mode=0o777"],
            "plaintext_api_key_risks": ["risk"],
            "warnings": ["keyring unavailable; env fallback accepted"],
            "repair_actions": ["chmod 600 cfg"],
        }
        lines = health_check._render_security_section(sec)
        text = "\n".join(lines)
        assert "## Security Health (S47)" in text
        assert "Status: WARNING" in text
        assert "- keyring available: True" in text
        assert "- keyring status: accepted" in text
        assert "- keyring risk: safe_but_not_best" in text
        assert "- keyring env fallback accepted: True" in text
        assert "- keyring safe but not best: True" in text
        assert "- keyring backend: keyring.backends.macOS.Keyring" in text
        assert "- keyring error: unavailable" in text
        assert "- legacy credential rows: enc=1, plaintext=2, keyref=3" in text
        assert "- pickle findings: 1" in text
        assert "- weak hash findings: 1" in text
        assert "- permission violations: 1" in text
        assert "- plaintext api key risks: 1" in text
        assert "  - pickle: a.py:1" in text
        assert "  - weak hash: b.py:2" in text
        assert "  - permission: cfg: mode=0o777" in text
        assert "  - warning: keyring unavailable; env fallback accepted" in text
        assert "  - repair: chmod 600 cfg" in text
        assert "Tags: `system=health-report, agent=claude, type=heartbeat`" in text


class TestGenerateHealthReport:
    def test_section_ordering_and_final_tags(self):
        with patch.object(health_check, "check_git_uncommitted", return_value={"status": "ok"}):
            with patch.object(health_check, "check_database", return_value={}):
                with patch.object(
                    health_check, "check_retention_databases", return_value={}
                ):
                    with patch.object(
                        health_check,
                        "check_wiki_metrics",
                        return_value={
                            "status": "ok",
                            "total_pages": 1,
                            "new_this_month": 1,
                            "avg_quality": 70.0,
                            "avg_heat": 4.0,
                        },
                    ):
                        with patch.object(
                            health_check,
                            "check_wiki",
                            return_value={"status": "ok", "total_md_files": 1},
                        ):
                            with patch.object(
                                health_check,
                                "check_security",
                                return_value={
                                    "status": "ok",
                                    "keyring_available": True,
                                    "legacy_key_rows": {
                                        "enc_rows": 0,
                                        "plaintext_rows": 0,
                                        "keyref_rows": 0,
                                    },
                                    "pickle_findings": [],
                                    "weak_hash_findings": [],
                                    "permission_violations": [],
                                    "plaintext_api_key_risks": [],
                                },
                            ):
                                report = health_check.generate_health_report()

        assert report.startswith("# Health Check Report |")
        headings = [
            "## Config Health (P5)",
            "## Database Health (P6)",
            "## Retention Database Health (P0 4.1)",
            "## Wiki Metrics Stats",
            "## Wiki Directory",
            "## Security Health (S47)",
        ]
        positions = [report.index(h) for h in headings]
        assert positions == sorted(positions)
        assert report.endswith(
            "Tags: `system=health-report, agent=claude, type=heartbeat`"
        )
