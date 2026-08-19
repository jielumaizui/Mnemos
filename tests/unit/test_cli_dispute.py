# -*- coding: utf-8 -*-
"""Tests for mnemos dispute CLI."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.cli.commands.dispute import cmd_dispute
from tests.knowledge_graph_decision_fixtures import authorized_knowledge_graph


class Args:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


@pytest.fixture
def resolver_with_conflict(tmp_path, patched_get_config):
    from core.kia.relation_schema import Relation, RelationType
    from core.app.dispute_resolver import DisputeResolver

    patched_get_config.wiki_dir = tmp_path
    patched_get_config.data_dir = tmp_path / "data"
    patched_get_config.database_dir = tmp_path / "data"
    patched_get_config.mnemos_dir = tmp_path / ".mnemos"
    # 使用默认 db_path，确保 CLI 创建的 DisputeResolver 能读到同一数据库
    kg = authorized_knowledge_graph(wiki_base=str(tmp_path))

    (tmp_path / "a.md").write_text("A", encoding="utf-8")
    (tmp_path / "b.md").write_text("B", encoding="utf-8")

    kg.add_relation(
        Relation(
            source="a.md",
            target="b.md",
            relation_type=RelationType.BUILDS_ON,
            strength=1.0,
            confidence=0.6,
        )
    )
    kg.add_relation(
        Relation(
            source="a.md",
            target="b.md",
            relation_type=RelationType.CONTRADICTS,
            strength=1.0,
            confidence=0.5,
        )
    )

    resolver = DisputeResolver(wiki_base=str(tmp_path))
    return resolver, patched_get_config


def test_cmd_dispute_scan_creates_pages(resolver_with_conflict, capsys):
    resolver, cfg = resolver_with_conflict
    cfg._values["dispute_scan"] = {
        "enabled": True,
        "max_daily_disputes": 10,
        "min_conflict_strength": 0.5,
        "auto_resolve_min_gap": 0.30,
        "merge_min_gap": 0.15,
        "freshness_half_life_days": 30,
        "citation_max_reference": 20,
        "weights": {
            "confidence": 0.25,
            "freshness": 0.25,
            "citation": 0.20,
            "quality": 0.15,
            "source": 0.10,
            "core": 0.05,
        },
        "adaptive_learning": {"enabled": False},
    }

    args = Args(dispute_cmd="scan", max_disputes=None)
    assert cmd_dispute(args) == 0

    captured = capsys.readouterr()
    assert "争议扫描完成" in captured.out
    assert "disputes_created=1" in captured.out

    dispute_dir = Path(cfg.wiki_dir).expanduser() / "08-Disputes"
    assert any(dispute_dir.glob("*.md"))


def test_cmd_dispute_list_and_stats(resolver_with_conflict, capsys):
    resolver, cfg = resolver_with_conflict
    cfg._values["dispute_scan"] = {
        "enabled": True,
        "max_daily_disputes": 10,
        "min_conflict_strength": 0.5,
        "auto_resolve_min_gap": 0.30,
        "merge_min_gap": 0.15,
        "freshness_half_life_days": 30,
        "citation_max_reference": 20,
        "weights": {
            "confidence": 0.25,
            "freshness": 0.25,
            "citation": 0.20,
            "quality": 0.15,
            "source": 0.10,
            "core": 0.05,
        },
        "adaptive_learning": {"enabled": False},
    }
    resolver.scan()

    args = Args(dispute_cmd="list", unresolved_only=False)
    assert cmd_dispute(args) == 0
    captured = capsys.readouterr()
    assert "共 1 条争议" in captured.out

    args = Args(dispute_cmd="stats")
    assert cmd_dispute(args) == 0
    captured = capsys.readouterr()
    assert "争议统计: 总数=1" in captured.out


def test_cmd_dispute_resolve_updates_page(resolver_with_conflict):
    resolver, cfg = resolver_with_conflict
    cfg._values["dispute_scan"] = {
        "enabled": True,
        "max_daily_disputes": 10,
        "min_conflict_strength": 0.5,
        "auto_resolve_min_gap": 0.30,
        "merge_min_gap": 0.15,
        "freshness_half_life_days": 30,
        "citation_max_reference": 20,
        "weights": {
            "confidence": 0.25,
            "freshness": 0.25,
            "citation": 0.20,
            "quality": 0.15,
            "source": 0.10,
            "core": 0.05,
        },
        "adaptive_learning": {"enabled": False},
    }
    resolver.scan()

    dispute_dir = Path(cfg.wiki_dir).expanduser() / "08-Disputes"
    page_path = str(next(dispute_dir.glob("*.md")).relative_to(Path(cfg.wiki_dir).expanduser()))

    args = Args(
        dispute_cmd="resolve",
        page_path=page_path,
        resolution="adopt_new",
        context="test context",
    )
    assert cmd_dispute(args) == 0

    content = (Path(cfg.wiki_dir).expanduser() / page_path).read_text(encoding="utf-8")
    assert "- [x] **采纳新断言**" in content
    assert "test context" in content


def test_cmd_dispute_rollback_context_updates_original_pages(resolver_with_conflict, capsys):
    resolver, cfg = resolver_with_conflict
    cfg._values["dispute_scan"] = {
        "enabled": True,
        "max_daily_disputes": 10,
        "min_conflict_strength": 0.5,
        "auto_resolve_min_gap": 0.30,
        "merge_min_gap": 0.15,
        "freshness_half_life_days": 30,
        "citation_max_reference": 20,
        "weights": {
            "confidence": 0.25,
            "freshness": 0.25,
            "citation": 0.20,
            "quality": 0.15,
            "source": 0.10,
            "core": 0.05,
        },
        "adaptive_learning": {"enabled": False},
    }
    resolver.scan()

    wiki_dir = Path(cfg.wiki_dir).expanduser()
    dispute_dir = wiki_dir / "08-Disputes"
    page_path = str(next(dispute_dir.glob("*.md")).relative_to(wiki_dir))

    args = Args(
        dispute_cmd="resolve",
        page_path=page_path,
        resolution="keep_both",
        context="CLI context",
    )
    assert cmd_dispute(args) == 0
    assert "CLI context" in (wiki_dir / "a.md").read_text(encoding="utf-8")

    args = Args(dispute_cmd="rollback-context", page_path=page_path)
    assert cmd_dispute(args) == 0
    captured = capsys.readouterr()
    assert "已回滚争议上下文" in captured.out
    assert "CLI context" not in (wiki_dir / "a.md").read_text(encoding="utf-8")


def test_cmd_dispute_resolve_invalid_resolution_returns_error(resolver_with_conflict):
    resolver, cfg = resolver_with_conflict
    resolver.scan()

    dispute_dir = Path(cfg.wiki_dir).expanduser() / "08-Disputes"
    page_path = str(next(dispute_dir.glob("*.md")).relative_to(Path(cfg.wiki_dir).expanduser()))

    args = Args(
        dispute_cmd="resolve",
        page_path=page_path,
        resolution="invalid",
        context="",
    )
    assert cmd_dispute(args) == 1


def test_cmd_dispute_unknown_subcommand_returns_error():
    args = Args(dispute_cmd="unknown")
    assert cmd_dispute(args) == 1


def test_cmd_dispute_weights_show_and_set(resolver_with_conflict, capsys):
    """weights 子命令可查看、设置、重置权重。"""
    resolver, cfg = resolver_with_conflict
    # 默认查看
    args = Args(dispute_cmd="weights", set_weights=None, reset=False, learn=False)
    assert cmd_dispute(args) == 0
    captured = capsys.readouterr()
    assert "当前权重:" in captured.out
    assert "confidence:" in captured.out

    # 设置权重
    args = Args(
        dispute_cmd="weights",
        set_weights=["confidence=0.40", "freshness=0.30"],
        reset=False,
        learn=False,
    )
    assert cmd_dispute(args) == 0
    captured = capsys.readouterr()
    assert "已保存权重到 state" in captured.out

    # 重置
    args = Args(dispute_cmd="weights", set_weights=None, reset=True, learn=False)
    assert cmd_dispute(args) == 0
    captured = capsys.readouterr()
    assert "已清除 state 权重" in captured.out


def test_cmd_dispute_show_renders_breakdown(resolver_with_conflict, capsys):
    """show 子命令可解析争议页 frontmatter 并输出评分详情。"""
    resolver, cfg = resolver_with_conflict
    cfg._values["dispute_scan"] = {
        "enabled": True,
        "max_daily_disputes": 10,
        "min_conflict_strength": 0.5,
        "auto_resolve_min_gap": 0.30,
        "merge_min_gap": 0.15,
        "freshness_half_life_days": 30,
        "citation_max_reference": 20,
        "weights": {
            "confidence": 0.25,
            "freshness": 0.25,
            "citation": 0.20,
            "quality": 0.15,
            "source": 0.10,
            "core": 0.05,
        },
        "adaptive_learning": {"enabled": False},
    }
    resolver.scan()

    dispute_dir = Path(cfg.wiki_dir).expanduser() / "08-Disputes"
    page_path = str(next(dispute_dir.glob("*.md")).relative_to(Path(cfg.wiki_dir).expanduser()))

    args = Args(dispute_cmd="show", page_path=page_path)
    assert cmd_dispute(args) == 0
    captured = capsys.readouterr()
    assert "综合分:" in captured.out
    assert "建议动作:" in captured.out
