# -*- coding: utf-8 -*-
"""
P2-1 长链路测试 — Persona / Ixion / Aegis 链路

链路：ProfileGenerator → SkillWikiFlywheel → SmartMatcher → InProcessGuard

策略：临时 wiki 目录/SQLite，mock 外部依赖（persona 子系统）。
断言目标：画像生成、技能匹配、守护检查。
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest


class TestProfileGeneratorLoop:
    """Metis 画像生成链路。"""

    @pytest.fixture
    def wiki_env(self, tmp_path):
        wiki = tmp_path / "wiki"
        inbox = wiki / "00-Inbox"
        inbox.mkdir(parents=True)

        # 创建几个有 frontmatter 的页面
        (inbox / "docker.md").write_text(
            "---\n领域: 技术\n类型: 概念\n复杂度: 入门\n置信度: 0.9\n"
            "时效性: 稳定\n创建日期: 2024-01-01\n关键词:\n  工具实体: [Docker, Container]\n---\n\n"
            "# Docker\n\nDocker is a containerization platform.\n",
            encoding="utf-8",
        )
        (inbox / "k8s.md").write_text(
            "---\n领域: 技术\n类型: 概念\n复杂度: 进阶\n置信度: 0.85\n"
            "时效性: 版本绑定\n创建日期: 2024-02-01\n关键词:\n  工具实体: [Kubernetes, K8s]\n---\n\n"
            "# Kubernetes\n\nKubernetes orchestrates containers.\n",
            encoding="utf-8",
        )
        return wiki

    def test_generate_profile_from_wiki(self, wiki_env):
        from core.kia.metis import ProfileGenerator

        gen = ProfileGenerator(wiki_base=str(wiki_env))
        profile = gen.generate()

        assert profile is not None
        assert profile.total_knowledge >= 2
        # 应提取到工具实体
        tools = [t[0] for t in profile.tool_stack]
        assert any("Docker" in t or "Kubernetes" in t for t in tools)


class TestSmartMatcherLoop:
    """Aegis 三层级联匹配链路。"""

    def test_exact_match(self):
        from core.kia.aegis import SmartMatcher

        matcher = SmartMatcher()
        result = matcher.match_exact("delete user", ["delete user", "create user"])
        assert result is not None
        assert result[0] == "delete user"
        assert result[1] == 1.0

    def test_keyword_match(self):
        from core.kia.aegis import SmartMatcher

        matcher = SmartMatcher()
        result = matcher.match_keyword("please delete the account", ["delete", "remove"])
        assert result is not None
        assert result[0] == "delete"

    def test_semantic_match(self):
        from core.kia.aegis import SmartMatcher

        matcher = SmartMatcher(semantic_threshold=0.3)
        result = matcher.match_semantic(
            "delete file from system permanently",
            ["delete file operation system guide", "create new directory"],
        )
        assert result is not None
        assert "delete" in result[0].lower()

    def test_three_tier_falls_back(self):
        from core.kia.aegis import SmartMatcher

        matcher = SmartMatcher(semantic_threshold=0.3)
        # 输入与候选有一定关联但不精确，走语义层
        result = matcher.match_three_tier(
            "drop table database",
            semantic_refs=["drop table database system"],
        )
        # 语义层应能匹配到
        assert result is not None
        assert result["layer"] == 3


class TestInProcessGuardLoop:
    """Aegis 执行中守护链路。"""

    def test_guard_detects_critical_keyword(self):
        from core.kia.aegis import InProcessGuard, GuardLevel
        from core.kia.prophasis import LoadedKnowledge, ChecklistItem

        checklist = [
            ChecklistItem(
                item="禁止删除生产数据",
                source="safety_rule",
                trigger_keywords=["delete", "drop", "truncate"],
                risk_patterns=["rm -rf", "drop table"],
            )
        ]
        knowledge = LoadedKnowledge(
            task_type="coding",
            subtype="database",
            version=1,
            checklist=checklist,
            lessons_summary="",
            loaded_at="2024-01-01",
        )

        guard = InProcessGuard(knowledge=knowledge)
        alert = guard.check("I want to drop table users")

        assert alert is not None
        assert alert.level in (GuardLevel.INTERRUPT, GuardLevel.HINT)
        assert "drop" in alert.trigger_text.lower()

    def test_guard_allows_safe_message(self):
        from core.kia.aegis import InProcessGuard
        from core.kia.prophasis import LoadedKnowledge, ChecklistItem

        checklist = [ChecklistItem(item="安全检查", source="test")]
        knowledge = LoadedKnowledge(
            task_type="coding",
            subtype="general",
            version=1,
            checklist=checklist,
            lessons_summary="",
            loaded_at="2024-01-01",
        )

        guard = InProcessGuard(knowledge=knowledge)
        alert = guard.check("Hello, how are you today?")

        # 安全消息不应触发严重告警
        assert alert is None or alert.triggered_by != "user"
