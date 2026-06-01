# -*- coding: utf-8 -*-
"""
P0-3 长链路测试 — Orchestrator / Chronos 调度烟测

目标：每个 mode 跑一次最小临时 vault + 临时 DB，
      抓旧模块 import 失败、schema 缺失、循环依赖。

策略：临时目录，临时 SQLite，不验证业务效果，
      只断言 "能初始化、能调用、不抛未捕获异常"。
"""

import sqlite3
from pathlib import Path

import pytest


class TestOrchestratorSmoke:
    """Orchestrator 各 mode 烟测 — 最小临时环境。"""

    @pytest.fixture(autouse=True)
    def _mock_slow_ops(self, monkeypatch):
        """mock 耗时的全量扫描，避免烟测超时。只验证 import 和调用链路。"""
        # KnowledgeImmuneSystem.full_scan → 空报告
        try:
            from core.kia.hygieia import KnowledgeImmuneSystem, HealthReport
            monkeypatch.setattr(
                KnowledgeImmuneSystem, "full_scan",
                lambda self, pages=None: HealthReport(scanned_pages=0),
            )
        except Exception:
            pass
        # EntropyEngine.scan → 空结果
        try:
            from core.kia.eris import EntropyEngine
            monkeypatch.setattr(
                EntropyEngine, "scan", lambda self, **kwargs: [],
            )
        except Exception:
            pass
        # StressTestEngine.run → 空结果
        try:
            from core.kia.stress_test import StressTestEngine
            monkeypatch.setattr(
                StressTestEngine, "run", lambda self, pages=None: {},
            )
        except Exception:
            pass
        # ProfileGenerator.generate → 空结果
        try:
            from core.kia.metis import ProfileGenerator
            monkeypatch.setattr(
                ProfileGenerator, "generate", lambda self: {},
            )
        except Exception:
            pass
        # PredictivePushEngine.analyze → 空结果
        try:
            from core.kia.teiresias import PredictivePushEngine
            monkeypatch.setattr(
                PredictivePushEngine, "analyze", lambda self: [],
            )
        except Exception:
            pass

    @pytest.fixture
    def wiki_dir(self, tmp_path):
        """创建最小 wiki 目录结构。"""
        wiki = tmp_path / "wiki"
        for sub in ["00-Inbox", "01-Projects", "02-Areas", "03-Tech", "04-Concepts"]:
            (wiki / sub).mkdir(parents=True)
        # 注意：不放 test.md，因为很多步骤会触发 EventBus publish_event，
        # 而全局 EventBus 有 200万+ pending 事件，会导致超时。
        # 烟测只验证"能初始化、能调用、不抛未捕获异常"。
        return wiki

    def test_orchestrator_init_and_run_distill(self, wiki_dir):
        from core.orchestrator import Orchestrator

        orch = Orchestrator(wiki_base=str(wiki_dir), dry_run=True)
        result = orch.run_distill()
        assert isinstance(result, dict)
        # run_distill 返回基本统计字段之一即可
        assert any(k in result for k in ("pending", "delegated", "inbox_pages", "error"))

    def test_orchestrator_run_dna(self, wiki_dir):
        from core.orchestrator import Orchestrator

        orch = Orchestrator(wiki_base=str(wiki_dir), dry_run=True)
        result = orch.run_dna()
        assert isinstance(result, dict)

    def test_orchestrator_run_graph(self, wiki_dir):
        from core.orchestrator import Orchestrator

        orch = Orchestrator(wiki_base=str(wiki_dir), dry_run=True)
        result = orch.run_graph()
        assert isinstance(result, dict)

    def test_orchestrator_run_immune(self, wiki_dir):
        from core.orchestrator import Orchestrator

        orch = Orchestrator(wiki_base=str(wiki_dir), dry_run=True)
        result = orch.run_immune()
        assert isinstance(result, dict)

    def test_orchestrator_run_entropy(self, wiki_dir):
        from core.orchestrator import Orchestrator

        orch = Orchestrator(wiki_base=str(wiki_dir), dry_run=True)
        result = orch.run_entropy()
        assert isinstance(result, dict)

    def test_orchestrator_run_stress(self, wiki_dir):
        from core.orchestrator import Orchestrator

        orch = Orchestrator(wiki_base=str(wiki_dir), dry_run=True)
        result = orch.run_stress()
        assert isinstance(result, dict)

    def test_orchestrator_run_falsify(self, wiki_dir):
        from core.orchestrator import Orchestrator

        orch = Orchestrator(wiki_base=str(wiki_dir), dry_run=True)
        result = orch.run_falsify()
        assert isinstance(result, dict)

    def test_orchestrator_run_dark(self, wiki_dir):
        from core.orchestrator import Orchestrator

        orch = Orchestrator(wiki_base=str(wiki_dir), dry_run=True)
        result = orch.run_dark()
        assert isinstance(result, dict)

    def test_orchestrator_run_entangle(self, wiki_dir):
        from core.orchestrator import Orchestrator

        orch = Orchestrator(wiki_base=str(wiki_dir), dry_run=True)
        result = orch.run_entangle()
        assert isinstance(result, dict)

    def test_orchestrator_run_shadow(self, wiki_dir):
        from core.orchestrator import Orchestrator

        orch = Orchestrator(wiki_base=str(wiki_dir), dry_run=True)
        result = orch.run_shadow()
        assert isinstance(result, dict)

    def test_orchestrator_run_capsule(self, wiki_dir):
        from core.orchestrator import Orchestrator

        orch = Orchestrator(wiki_base=str(wiki_dir), dry_run=True)
        result = orch.run_capsule()
        assert isinstance(result, dict)

    def test_orchestrator_run_snapshot(self, wiki_dir):
        from core.orchestrator import Orchestrator

        orch = Orchestrator(wiki_base=str(wiki_dir), dry_run=True)
        result = orch.run_snapshot()
        assert isinstance(result, dict)

    def test_orchestrator_run_push(self, wiki_dir):
        from core.orchestrator import Orchestrator

        orch = Orchestrator(wiki_base=str(wiki_dir), dry_run=True)
        result = orch.run_push()
        assert isinstance(result, dict)

    def test_orchestrator_run_profile(self, wiki_dir):
        from core.orchestrator import Orchestrator

        orch = Orchestrator(wiki_base=str(wiki_dir), dry_run=True)
        result = orch.run_profile()
        assert isinstance(result, dict)

    def test_orchestrator_run_full(self, wiki_dir):
        """完整循环烟测 — 所有 mode 串行跑一遍。"""
        from core.orchestrator import Orchestrator

        orch = Orchestrator(wiki_base=str(wiki_dir), dry_run=True)
        result = orch.run_full()

        assert isinstance(result, dict)
        assert "timestamp" in result
        assert "results" in result
        assert "errors" in result
        # 12 个阶段都应返回 dict（即使内部出错也是 dict）
        for key in ["distill", "dna", "graph", "immune", "entropy",
                    "stress", "falsify", "shadow", "capsule",
                    "snapshot", "push", "profile"]:
            assert key in result["results"], f"{key} 阶段缺失"
            assert isinstance(result["results"][key], dict)


class TestChronosSmoke:
    """Chronos 调度器烟测。"""

    def test_scheduler_init_with_temp_db(self, tmp_path):
        from core.kia.chronos import KnowledgeScheduler

        db = tmp_path / "sched.db"
        sched = KnowledgeScheduler(db_path=str(db))
        assert sched.DB_PATH == db

        # DB schema 应已创建
        with sqlite3.connect(str(db)) as conn:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            assert "knowledge_scheduled_tasks" in tables
            assert "scheduler_step_log" in tables

    def test_scheduler_register_and_run_step(self, tmp_path):
        from core.kia.chronos import KnowledgeScheduler, ScheduledStep, CronTrigger

        db = tmp_path / "sched.db"
        sched = KnowledgeScheduler(db_path=str(db))

        executed = []

        def dummy_step():
            executed.append("ran")
            return {"ok": True}

        step = ScheduledStep(
            name="test_step",
            trigger=CronTrigger("* * * * *"),
            func=dummy_step,
        )
        sched.register(step)

        # 直接执行步骤（绕过 cron 和线程池）
        result = sched._run_step(step)
        assert result.get("ok") is True
        assert "ran" in executed

    def test_scheduler_step_failure_isolated(self, tmp_path):
        from core.kia.chronos import KnowledgeScheduler, ScheduledStep, CronTrigger

        db = tmp_path / "sched.db"
        sched = KnowledgeScheduler(db_path=str(db))

        def bad_step():
            raise RuntimeError("intentional failure")

        step = ScheduledStep(
            name="bad_step",
            trigger=CronTrigger("* * * * *"),
            func=bad_step,
        )
        sched.register(step)

        result = sched._run_step(step)
        assert result["status"] == "error"
        assert "intentional failure" in result.get("error", "")
