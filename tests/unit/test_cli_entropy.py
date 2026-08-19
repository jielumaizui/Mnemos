# -*- coding: utf-8 -*-
"""Tests for entropy CLI."""

from argparse import Namespace

import pytest

from core.cli.commands.entropy import cmd_entropy
from core.kia.eris import EntropyReport, MergeCandidate


class FakeEntropyEngine:
    def __init__(self, wiki_base=None):
        self.wiki_base = wiki_base

    def scan(self, sample_size=None):
        return EntropyReport(
            total_pairs_scanned=42,
            candidates=[
                MergeCandidate(
                    page_a="a.md",
                    page_b="b.md",
                    similarity=0.9,
                    merge_strategy="merge_into_one",
                    reason="相似",
                    recommended_action="合并",
                    keep_page="a.md",
                    confidence=0.9,
                ),
            ],
        )

    def auto_fix(self, report, apply_duplicates=False, apply_links=False):
        return ["linked a.md -> b.md"] if apply_links else []


@pytest.fixture
def wiki(tmp_path):
    base = tmp_path / "wiki"
    base.mkdir()
    return base


@pytest.fixture  # noqa
def monkeypatch_config(monkeypatch, wiki):
    class Cfg:
        wiki_dir = wiki

    monkeypatch.setattr("core.cli.commands.entropy._config_mod.get_config", lambda: Cfg())


def test_entropy_scan_uses_public_helpers(capsys, monkeypatch_config, monkeypatch, wiki):
    assert monkeypatch_config is None
    report = EntropyReport(total_pairs_scanned=7)
    calls = {}

    def fake_scan(wiki_base=None, sample_size=None):
        calls["scan"] = {"wiki_base": wiki_base, "sample_size": sample_size}
        return report

    def fake_report(wiki_base=None, report=None, sample_size=None):
        calls["report"] = {
            "wiki_base": wiki_base,
            "report": report,
            "sample_size": sample_size,
        }
        return "# helper report\n"

    monkeypatch.setattr("core.cli.commands.entropy.run_entropy_scan", fake_scan, raising=False)
    monkeypatch.setattr("core.cli.commands.entropy.run_and_report", fake_report, raising=False)
    monkeypatch.setattr("core.cli.commands.entropy.EntropyEngine", FakeEntropyEngine)

    args = Namespace(entropy_cmd="scan", limit=7, write_report=True)
    assert cmd_entropy(args) == 0

    assert calls["scan"] == {"wiki_base": str(wiki), "sample_size": 7}
    assert calls["report"] == {
        "wiki_base": str(wiki),
        "report": report,
        "sample_size": None,
    }
    written = list((wiki / "99-Reports").glob("知识熵减报告-*.md"))
    assert written[0].read_text(encoding="utf-8") == "# helper report\n"


def test_entropy_scan(capsys, monkeypatch_config, monkeypatch):  # noqa
    def fake_scan(wiki_base=None, sample_size=None):
        return FakeEntropyEngine(wiki_base=wiki_base).scan(sample_size=sample_size)

    monkeypatch.setattr("core.cli.commands.entropy.run_entropy_scan", fake_scan)
    args = Namespace(entropy_cmd="scan", limit=10, write_report=False)
    ret = cmd_entropy(args)
    assert ret == 0
    out = capsys.readouterr().out
    assert "candidates=1" in out
    assert "mergeable=1" in out


def test_entropy_scan_write_report(capsys, monkeypatch_config, monkeypatch, wiki):  # noqa
    report = FakeEntropyEngine(wiki_base=str(wiki)).scan(sample_size=10)

    def fake_scan(wiki_base=None, sample_size=None):
        return report

    def fake_report(wiki_base=None, report=None, sample_size=None):
        return "# helper report\n"

    monkeypatch.setattr("core.cli.commands.entropy.run_entropy_scan", fake_scan)
    monkeypatch.setattr("core.cli.commands.entropy.run_and_report", fake_report)
    args = Namespace(entropy_cmd="scan", limit=10, write_report=True)
    ret = cmd_entropy(args)
    assert ret == 0
    reports = list((wiki / "99-Reports").glob("知识熵减报告-*.md"))
    assert len(reports) == 1
    assert reports[0].read_text(encoding="utf-8") == "# helper report\n"


def test_entropy_autofix(capsys, monkeypatch_config, monkeypatch):  # noqa
    monkeypatch.setattr("core.cli.commands.entropy.EntropyEngine", FakeEntropyEngine)
    args = Namespace(entropy_cmd="auto-fix", apply_links=True)
    ret = cmd_entropy(args)
    assert ret == 0
    out = capsys.readouterr().out
    assert "linked a.md -> b.md" in out
