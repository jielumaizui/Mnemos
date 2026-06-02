#!/usr/bin/env python3
"""
对照 Mnemos-蓝图实现审计-2026-06-02.md 的复查脚本
"""
import sys
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def check_compileall():
    """P0-5: compileall 检查"""
    import py_compile
    import compileall
    errors = []
    for p in ["core", "integrations", "mnemos_cli.py", "mnemos_daemon.py"]:
        try:
            if Path(p).is_dir():
                if not compileall.compile_dir(p, quiet=True):
                    errors.append(f"{p}: 编译失败")
            else:
                py_compile.compile(p, doraise=True)
        except Exception as e:
            errors.append(f"{p}: {e}")
    return errors


def check_relation_manager():
    """P0-1: relation_manager.py 编译检查"""
    import py_compile
    try:
        py_compile.compile("core/kia/relation_manager.py", doraise=True)
        return []
    except Exception as e:
        return [str(e)]


def check_list_by_tags():
    """P0-2: list_by_tags 语义检查"""
    from integrations.styx import MemosClient
    import inspect
    src = inspect.getsource(MemosClient.list_by_tags)
    # 检查是否使用 all 匹配
    if "all(t in" in src:
        return []
    return ["list_by_tags 未使用 all 匹配"]


def check_sync_log():
    """P0-3 / P1: sync_log 可追踪性"""
    db = Path.home() / ".mnemos" / "sync_log.db"
    if not db.exists():
        return ["sync_log.db 不存在"]
    conn = sqlite3.connect(str(db))
    count = conn.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0]
    conn.close()
    if count < 100:
        return [f"sync_log 仅 {count} 条，与 Memos 2511 条严重不匹配"]
    return []


def check_kg_db():
    """P1-2: KG 数据库是否非空"""
    db = Path.home() / ".mnemos" / "knowledge_graph.db"
    if not db.exists():
        return ["knowledge_graph.db 不存在"]
    conn = sqlite3.connect(str(db))
    entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    relations = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
    conn.close()
    issues = []
    if entities == 0:
        issues.append(f"entities 表为空 ({entities})")
    if relations == 0:
        issues.append(f"relations 表为空 ({relations})")
    if not issues:
        issues.append(f"KG 正常: entities={entities}, relations={relations}")
    return issues


def check_wiki_state():
    """L2 蒸馏产物质量"""
    db = Path.home() / ".mnemos" / "wiki_state.db"
    if not db.exists():
        return ["wiki_state.db 不存在"]
    conn = sqlite3.connect(str(db))
    try:
        total = conn.execute("SELECT COUNT(*) FROM processed_sessions").fetchone()[0]
        low_q = conn.execute("SELECT COUNT(*) FROM processed_sessions WHERE status='skipped_low_quality'").fetchone()[0]
        avg_score = conn.execute("SELECT AVG(quality_score) FROM processed_sessions WHERE status='pipeline'").fetchone()[0] or 0
        return [f"processed_sessions={total}, low_quality={low_q}, avg_quality_score={avg_score:.2f}"]
    except Exception as e:
        return [str(e)]
    finally:
        conn.close()


def check_preflight():
    """P2-1: PreFlight 是否有历史经验"""
    try:
        from core.kia.preflight import run_preflight
        result = run_preflight("claude", "开始写代码", "/tmp")
        if "未找到" in result or "无历史" in result:
            return ["preflight(coding) 返回无历史经验"]
        return ["preflight(coding) 有返回"]
    except Exception as e:
        return [f"preflight 异常: {e}"]


def check_guard():
    """P2-2: Guard 是否能拦截高风险操作"""
    try:
        from core.kia.aegis import InProcessGuard
        guard = InProcessGuard()
        result = guard.check("删除生产数据库")
        if result is not None:
            return ["guard_check 能识别高风险"]
        return ["guard_check 未识别高风险操作"]
    except Exception as e:
        return [f"guard_check 异常: {e}"]


def check_blindspot():
    """P2-4: Blindspot 是否静默失败"""
    try:
        from core.persona.hamartia import detect_blindspots
        from core.persona.pythia import PreferenceProfile
        result = detect_blindspots({"query": "codex-cli"}, [], PreferenceProfile())
        # 只要正常执行返回列表（即使为空），即表示未静默失败
        if isinstance(result, list):
            return ["blindspot_check 正常返回"]
        return ["blindspot_check 返回异常类型"]
    except Exception as e:
        return [f"blindspot_check 异常: {e}"]


def check_scorer_training():
    """P3: 评分闭环"""
    db = Path.home() / ".mnemos" / "mnemos.db"
    if not db.exists():
        return ["mnemos.db 不存在"]
    conn = sqlite3.connect(str(db))
    issues = []
    try:
        models = conn.execute("SELECT COUNT(*) FROM scorer_models WHERE is_active = 1").fetchone()[0]
        queue = conn.execute("SELECT COUNT(*) FROM scorer_training_queue WHERE status = 'pending'").fetchone()[0]
        gt = conn.execute("SELECT COUNT(*) FROM ground_truth_signals").fetchone()[0]

        if models == 0:
            issues.append(f"无活跃评分模型（scorer_models=0, queue={queue}, ground_truth={gt}）")
        if queue == 0 and gt == 0:
            issues.append("训练队列为空且无 ground_truth，评分闭环无法启动")
        return issues if issues else [f"models={models}, queue={queue}, ground_truth={gt}"]
    except Exception as e:
        return [str(e)]
    finally:
        conn.close()


def main():
    checks = [
        ("P0-1 relation_manager 编译", check_relation_manager),
        ("P0-2 list_by_tags 语义", check_list_by_tags),
        ("P0-3 sync_log 可追踪性", check_sync_log),
        ("P0-5 compileall", check_compileall),
        ("P1-2 KG 数据库状态", check_kg_db),
        ("L2 蒸馏产物状态", check_wiki_state),
        ("P2-1 PreFlight", check_preflight),
        ("P2-2 Guard", check_guard),
        ("P2-4 Blindspot", check_blindspot),
        ("P3 评分闭环", check_scorer_training),
    ]

    print("=" * 60)
    print("Mnemos 蓝图审计复查")
    print("=" * 60)

    for name, fn in checks:
        results = fn()
        status = "❌" if any("异常" in r or "未" in r or "空" in r or "不存在" in r or "失败" in r for r in results) else "✅"
        print(f"\n{status} {name}")
        for r in results:
            print(f"   {r}")


if __name__ == "__main__":
    main()
