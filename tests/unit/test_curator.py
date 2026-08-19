import os
import subprocess
import sys
from pathlib import Path

from scripts import curator


def _empty_plan():
    return {
        "generated_at": "2026-07-01T00:00:00",
        "total_pages": 0,
        "total_topics": 0,
        "pileups": [],
        "daily_merge_candidates": [],
        "weekly_merge_candidates": [],
    }


def test_write_curator_report_persists_wiki_and_log_copy(tmp_path, monkeypatch):
    wiki_dir = tmp_path / "wiki"
    log_dir = tmp_path / "logs" / "curator"
    monkeypatch.setattr(curator, "WIKI_DIR", wiki_dir)
    monkeypatch.setattr(curator, "CURATOR_LOG_DIR", log_dir)

    wiki_path, log_path = curator.write_curator_report(
        _empty_plan(), timestamp="20260701-120000"
    )

    assert wiki_path == wiki_dir / "curator-report-20260701-120000.md"
    assert log_path == log_dir / "curator-report-20260701-120000.md"
    assert wiki_path.read_text(encoding="utf-8") == log_path.read_text(encoding="utf-8")


def test_generate_merge_plan_reuses_find_stale_pages_for_weekly_candidates(
    monkeypatch,
):
    pages = [
        {"name": "topic-a", "mtime": 0},
        {"name": "topic-b", "mtime": 0},
        {"name": "topic-c", "mtime": 0},
    ]
    calls = []

    def fake_find_stale_pages(candidate_pages, days):
        calls.append((candidate_pages, days))
        return list(candidate_pages)

    monkeypatch.setattr(curator, "find_stale_pages", fake_find_stale_pages)

    plan = curator.generate_merge_plan({"topic": pages}, [])

    assert calls == [(pages, curator.P1_P0_DAYS)]
    assert plan["weekly_merge_candidates"] == [
        {
            "topic": "topic",
            "stale_pages": ["topic-a", "topic-b", "topic-c"],
            "count": 3,
        }
    ]


def test_daily_merge_archives_candidate_pages_before_merge(tmp_path, monkeypatch):
    wiki_dir = tmp_path / "wiki"
    archive_dir = wiki_dir / ".archive"
    log_dir = tmp_path / "logs" / "curator"
    wiki_dir.mkdir()
    (wiki_dir / "topic-v1.md").write_text("# topic v1", encoding="utf-8")
    (wiki_dir / "topic-v2.md").write_text("# topic v2", encoding="utf-8")

    monkeypatch.setattr(curator, "WIKI_DIR", wiki_dir)
    monkeypatch.setattr(curator, "ARCHIVE_DIR", archive_dir)
    monkeypatch.setattr(curator, "CURATOR_LOG_DIR", log_dir)
    monkeypatch.setattr(sys, "argv", ["curator.py", "--daily-merge"])

    curator.main()

    archive_batches = list(archive_dir.glob("curator-*"))
    assert len(archive_batches) == 1
    assert sorted(path.name for path in archive_batches[0].iterdir()) == [
        "topic-v1.md",
        "topic-v2.md",
    ]


def test_curator_status_script_runs_from_repo_root(tmp_path):
    repo_root = Path(__file__).resolve().parents[2]
    wiki_dir = tmp_path / "wiki"
    data_dir = tmp_path / "data"
    wiki_dir.mkdir()

    env = os.environ.copy()
    env["MNEMOS_WIKI_DIR"] = str(wiki_dir)
    env["MNEMOS_DATABASE_DIR"] = str(data_dir)

    result = subprocess.run(
        [sys.executable, "scripts/curator.py", "--status"],
        cwd=repo_root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode == 0, result.stderr
    assert "[Curator] 总页面:" in result.stdout
