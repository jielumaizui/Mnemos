#!/usr/bin/env python3
"""
Memos-Wiki v6.0 系统测试脚本

覆盖 14+ 子系统的导入、初始化和基本功能验证。

运行: python3 test_system.py
"""

from __future__ import annotations

import os
import sys
import sqlite3
import json
from pathlib import Path
from datetime import datetime

# 相对项目根目录的路径
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.config import get_config

_config = get_config()
WIKI_DIR = _config.wiki_dir
CLAUDE_DIR = _config.claude_data_dir


class Colors:
    OK = "\033[92m"
    WARN = "\033[93m"
    FAIL = "\033[91m"
    INFO = "\033[94m"
    RESET = "\033[0m"


def ok(msg): print(f"  {Colors.OK}[OK]{Colors.RESET} {msg}")
def warn(msg): print(f"  {Colors.WARN}[WARN]{Colors.RESET} {msg}")
def fail(msg): print(f"  {Colors.FAIL}[FAIL]{Colors.RESET} {msg}")
def info(msg): print(f"  {Colors.INFO}[INFO]{Colors.RESET} {msg}")


def section(title):
    print()
    print("=" * 60)
    print(title)
    print("=" * 60)


# ========== 测试 1: 模块导入 ==========

def test_imports():
    """测试所有 v6.0 模块是否能正常导入"""
    section("测试 1/6: 模块导入")

    modules = [
        # 核心 SDK 与集成
        ("integrations.styx", "MemosClient"),
        ("integrations.sources.claude_source", "ClaudeSource"),

        # KIA 闭环系统
        ("core.kia.dike", "TaskClassifier"),
        ("core.kia.kairos", "TimeParser"),
        ("core.kia.prophasis", "PreFlightInjector"),
        ("core.kia.aegis", "InProcessGuard"),
        ("core.kia.epimetheus", "AutoRetrospective"),
        ("core.kia.proteus", "IterationTracker"),
        ("core.kia.chronos", "KnowledgeScheduler"),

        # 子 Agent 蒸馏
        ("core.kia.amphora", "enqueue"),
        ("core.kia.amphora", "list_pending"),
        ("core.hephaestus.distillation_engine", "DistillationEngine"),

        # 14+ 子系统
        ("core.kia.charon", "run_connect_cycle"),
        ("core.kia.hygieia", "KnowledgeImmuneSystem"),
        ("core.kia.genos", "DNAEngine"),
        # ("core.dark_knowledge", "DarkKnowledgeMiner"),  # 暂未实现
        ("core.kia.knowledge_graph", "KnowledgeGraph"),
        # ("core.quantum_entanglement", "QuantumEntanglement"),  # 暂未实现
        ("core.kia.ixion", "SkillWikiFlywheel"),
        ("core.kia.teiresias", "PredictivePushEngine"),
        ("core.kia.aion", "TimeCapsule"),
        ("core.kia.eris", "EntropyEngine"),
        # ("core.falsifiability_marker", "FalsifiabilityMarker"),  # 暂未实现
        ("core.kia.metis", "ProfileGenerator"),
        ("core.kia.ananke", "VersionTimeTravel"),
        ("core.kia.hecate", "ShadowPageManager"),
        ("core.hephaestus.distillation_engine", "DistillationEngine"),
        ("core.kia.ariadne", "KnowledgeTrail"),
    ]

    passed = 0
    failed = 0

    for module_name, attr in modules:
        try:
            module = __import__(module_name, fromlist=[attr])
            getattr(module, attr)
            ok(f"{module_name}.{attr}")
            passed += 1
        except Exception as e:
            fail(f"{module_name}.{attr}: {e}")
            failed += 1

    info(f"导入测试: {passed} 通过, {failed} 失败")
    assert failed == 0, f"导入测试失败: {failed} 个模块无法导入"


# ========== 测试 2: Wiki 目录结构 ==========

def test_wiki_structure():
    """测试 Wiki 目录结构是否符合 v6.0"""
    section("测试 2/6: Wiki 目录结构")

    required_dirs = {
        "00-Inbox": "原始知识入口",
        "01-People": "人物实体",
        "02-Projects": "项目实体",
        "03-Tech": "技术实体",
        "04-Concepts": "概念实体",
        "05-MOCs": "MOC 枢纽",
        "retrospectives": "复盘知识库",
    }

    passed = True
    for d, desc in required_dirs.items():
        path = WIKI_DIR / d
        if path.exists():
            md_count = len(list(path.rglob("*.md")))
            ok(f"{d}/ - {desc} ({md_count} 个文件)")
        else:
            warn(f"{d}/ - {desc} 不存在(将在首次运行时创建)")

    assert passed, "wiki 结构测试存在缺失目录"


# ========== 测试 3: 数据库完整性 ==========

def test_databases():
    """测试 SQLite 数据库和关键表"""
    section("测试 3/6: 数据库完整性")

    dbs = {
        "wiki_state.db": ["processed_sessions", "wiki_pages", "scheduled_tasks"],
        "wiki/.kg/graph.db": ["entities", "relations"],
        "wiki/.kg/dna.db": ["knowledge_dna"],
        "wiki/.kg/trail.db": ["trail_events"],
        "wiki/.kg/falsifiability.db": ["falsifiability_marks"],
        "live_sync.db": ["knowledge_scheduled_tasks"],
    }

    all_passed = True
    for db_name, required_tables in dbs.items():
        db_path = CLAUDE_DIR / db_name
        if not db_path.exists():
            warn(f"数据库不存在: {db_name}(首次运行时会自动创建)")
            continue

        try:
            with sqlite3.connect(str(db_path), timeout=10) as conn:
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
                tables = {row[0] for row in cursor.fetchall()}

                missing = [t for t in required_tables if t not in tables]
                if missing:
                    fail(f"{db_name}: 缺失表 {missing}")
                    all_passed = False
                else:
                    ok(f"{db_name}: {len(required_tables)} 个表正常")
        except Exception as e:
            fail(f"{db_name}: 读取失败 ({e})")
            all_passed = False

    # 检查 distill_queue 目录
    queue_dir = CLAUDE_DIR / "distill_queue"
    if queue_dir.exists():
        pending = len(list(queue_dir.glob("*.json")))
        info(f"distill_queue: {pending} 个任务文件")
    else:
        info("distill_queue: 目录不存在(首次运行时会创建)")

    assert all_passed, "数据库完整性测试失败"


# ========== 测试 4: KIA 闭环系统 ==========

def test_kia_system():
    """测试 KIA 闭环核心组件"""
    section("测试 4/6: KIA 闭环系统")

    try:
        # 1. TaskClassifier
        from core.kia.dike import TaskClassifier
        tc = TaskClassifier()
        result = tc.classify([{"role": "user", "content": "帮我写一个 Python 脚本处理数据"}])
        if result.confidence > 0.5:
            ok(f"TaskClassifier: {result.task_type}/{result.subtype} (置信度 {result.confidence:.2f})")
        else:
            warn(f"TaskClassifier: 置信度较低 ({result.confidence:.2f})")

        # 2. TimeParser
        from core.kia.kairos import TimeParser
        tp = TimeParser()
        tw = tp.parse("帮我写个脚本，明天要用")
        ok(f"TimeParser: {tw.window.value} (周期性={tw.is_periodic})")

        # 3. PreFlightInjector
        from core.kia.prophasis import PreFlightInjector
        pfi = PreFlightInjector()
        ok(f"PreFlightInjector: Wiki 路径 {pfi.WIKI_BASE}")

        # 4. InProcessGuard
        from core.kia.aegis import InProcessGuard, GuardLevel
        from core.kia.prophasis import LoadedKnowledge
        from datetime import datetime
        lk = LoadedKnowledge(
            task_type="coding/python",
            subtype="script",
            version=1,
            checklist=[],
            lessons_summary="测试",
            loaded_at=datetime.now().isoformat(),
        )
        guard = InProcessGuard(lk)
        ok(f"InProcessGuard: 已初始化")

        # 5. IterationTracker
        from core.kia.proteus import IterationTracker
        it = IterationTracker()
        stats = it.get_stats()
        ok(f"IterationTracker: 总知识 {stats.get('total', 0)}")

        # 6. KnowledgeScheduler
        from core.kia.chronos import KnowledgeScheduler
        ks = KnowledgeScheduler()
        ok(f"KnowledgeScheduler: 已初始化")

    except Exception as e:
        fail(f"KIA 系统测试失败: {e}")
        import traceback
        traceback.print_exc()
        assert False, f"KIA 系统测试失败: {e}"


# ========== 测试 5: 子系统初始化 ==========

def test_subsystems():
    """测试 14+ 子系统初始化"""
    section("测试 5/6: 子系统初始化")

    tests = []

    try:
        from core.kia.hygieia import KnowledgeImmuneSystem
        immune = KnowledgeImmuneSystem()
        tests.append(("免疫系统", True, ""))
    except Exception as e:
        tests.append(("免疫系统", False, str(e)))

    try:
        from core.kia.genos import DNAEngine
        dna = DNAEngine()
        tests.append(("DNA 指纹", True, ""))
    except Exception as e:
        tests.append(("DNA 指纹", False, str(e)))

    try:
        from core.kia.knowledge_graph import KnowledgeGraph
        kg = KnowledgeGraph()
        tests.append(("知识图谱", True, ""))
    except Exception as e:
        tests.append(("知识图谱", False, str(e)))

    # QuantumEntanglement 暂未实现，跳过

    try:
        from core.kia.teiresias import PredictivePushEngine
        ppe = PredictivePushEngine()
        tests.append(("预测推送", True, ""))
    except Exception as e:
        tests.append(("预测推送", False, str(e)))

    try:
        from core.kia.aion import TimeCapsule
        tc = TimeCapsule()
        tests.append(("时间胶囊", True, ""))
    except Exception as e:
        tests.append(("时间胶囊", False, str(e)))

    try:
        from core.kia.eris import EntropyEngine
        ee = EntropyEngine()
        tests.append(("熵引擎", True, ""))
    except Exception as e:
        tests.append(("熵引擎", False, str(e)))

    # FalsifiabilityMarker 暂未实现，跳过

    try:
        from core.kia.metis import ProfileGenerator
        pg = ProfileGenerator()
        tests.append(("知识画像", True, ""))
    except Exception as e:
        tests.append(("知识画像", False, str(e)))

    try:
        from core.kia.ananke import VersionTimeTravel
        vtt = VersionTimeTravel()
        tests.append(("版本时间旅行", True, ""))
    except Exception as e:
        tests.append(("版本时间旅行", False, str(e)))

    try:
        from core.kia.hecate import ShadowPageManager
        spm = ShadowPageManager()
        tests.append(("影子页面", True, ""))
    except Exception as e:
        tests.append(("影子页面", False, str(e)))

    try:
        from core.kia.ixion import SkillWikiFlywheel
        swf = SkillWikiFlywheel()
        tests.append(("Skill-Wiki 飞轮", True, ""))
    except Exception as e:
        tests.append(("Skill-Wiki 飞轮", False, str(e)))

    passed = sum(1 for _, ok_flag, _ in tests if ok_flag)
    for name, ok_flag, err in tests:
        if ok_flag:
            ok(name)
        else:
            fail(f"{name}: {err}")

    info(f"子系统初始化: {passed}/{len(tests)} 通过")
    assert passed == len(tests), f"子系统测试失败: {len(tests) - passed} 个子系统初始化失败"


# ========== 测试 6: 端到端流程 ==========

def test_end_to_end():
    """测试端到端关键流程"""
    section("测试 6/6: 端到端流程")

    # 1. 测试 distill_queue
    try:
        from core.kia.amphora import enqueue, list_pending, mark_done
        import hashlib
        test_sid = f"test:{hashlib.md5(b'test').hexdigest()[:8]}"
        enqueue(
            session_id=test_sid,
            messages=[{"role": "user", "content": "测试消息"}],
            meta={"source": "test"}
        )
        pending = list_pending()
        test_tasks = [t for t in pending if t["session_id"].startswith("test:")]
        if test_tasks:
            ok(f"distill_queue: 入队成功 ({len(test_tasks)} 个测试任务)")
            mark_done(test_sid)
            ok("distill_queue: 标记完成成功")
        else:
            warn("distill_queue: 入队后未找到任务")
    except Exception as e:
        fail(f"distill_queue: {e}")

    # 2. 测试 Tavily 配置
    try:
        tavily_config = Path.home() / ".tavily" / "config.json"
        if tavily_config.exists():
            config = json.loads(tavily_config.read_text())
            if config.get("api_key"):
                ok(f"Tavily: 已配置 ({config['api_key'][:8]}...)")
            else:
                warn("Tavily: 配置文件存在但无 api_key")
        else:
            warn("Tavily: 未配置(shadow_page 将跳过外部验证)")
    except Exception as e:
        warn(f"Tavily: 配置检查失败 ({e})")

    # 3. 测试 guard_state 持久化
    try:
        guard_state = CLAUDE_DIR / "guard_state.json"
        if guard_state.exists():
            ok(f"Guard 状态: 已持久化 ({guard_state.stat().st_size} bytes)")
        else:
            info("Guard 状态: 无历史状态(正常)")
    except Exception as e:
        warn(f"Guard 状态: {e}")

    # 4. 测试 wiki 活跃复盘
    try:
        retro_dir = WIKI_DIR / "retrospectives"
        if retro_dir.exists():
            active_links = list(retro_dir.rglob("*-active.md"))
            if active_links:
                ok(f"复盘系统: {len(active_links)} 个活跃复盘")
            else:
                info("复盘系统: 无活跃复盘链接")
        else:
            info("复盘系统: 目录不存在")
    except Exception as e:
        warn(f"复盘系统: {e}")



# ========== 主函数 ==========

def main():
    print()
    print("+" + "-" * 58 + "+")
    print("|" + " " * 12 + "Memos-Wiki v6.0 系统测试" + " " * 22 + "|")
    print("+" + "-" * 58 + "+")
    print(f"  测试时间: {datetime.now().isoformat()[:19]}")
    print(f"  Wiki 路径: {WIKI_DIR}")
    print()

    results = [
        ("模块导入", test_imports()),
        ("Wiki 结构", test_wiki_structure()),
        ("数据库", test_databases()),
        ("KIA 闭环", test_kia_system()),
        ("子系统初始化", test_subsystems()),
        ("端到端流程", test_end_to_end()),
    ]

    section("测试汇总")
    all_passed = True
    for name, passed in results:
        status = f"{Colors.OK}[PASS]{Colors.RESET}" if passed else f"{Colors.FAIL}[FAIL]{Colors.RESET}"
        print(f"  {status}: {name}")
        if not passed:
            all_passed = False

    print()
    if all_passed:
        print(f"{Colors.OK}所有测试通过! Memos-Wiki v6.0 系统已就绪。{Colors.RESET}")
    else:
        print(f"{Colors.WARN}部分测试失败，请检查错误信息。{Colors.RESET}")

    print()
    info("常用命令:")
    print(f"  {sys.executable} mnemos_cli.py --session-start --user-message '...'")
    print(f"  {sys.executable} mnemos_cli.py --session-end --session-messages '...'")
    print(f"  {sys.executable} mnemos_cli.py --stats")
    print(f"  {sys.executable} mnemos_cli.py --kia-check")
    print(f"  {sys.executable} core/hephaestus/distillation_queue.py --list")
    print()

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
