#!/usr/bin/env python3
"""
Mnemos 部署后验证脚本 — 确认安装完整、核心链路可运转

用法:
    python3 scripts/verify_installation.py [--full]

选项:
    --full  同时运行集成测试（需要 pytest）
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def _err(msg: str) -> None:
    print(f"  ✗ {msg}")


def _warn(msg: str) -> None:
    print(f"  ⚠ {msg}")


def _step(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print("=" * 60)


def check_python_version() -> bool:
    v = sys.version_info
    if v >= (3, 10):
        _ok(f"Python {v.major}.{v.minor}.{v.micro}")
        return True
    _err(f"Python {v.major}.{v.minor} < 3.10")
    return False


def check_compileall() -> bool:
    try:
        r = subprocess.run(
            [sys.executable, "-m", "compileall", "-q", "core", "integrations",
             "mnemos_cli.py", "mnemos_daemon.py", "scripts", "tests"],
            cwd=PROJECT_ROOT,
            capture_output=True,
        )
        if r.returncode == 0:
            _ok("compileall 通过（无语法错误）")
            return True
        _err("compileall 失败")
        return False
    except Exception as e:
        _err(f"compileall 异常: {e}")
        return False


def check_cli_help() -> bool:
    try:
        r = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "mnemos_cli.py"), "--help"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and "Mnemos" in r.stdout:
            _ok("CLI 可执行")
            return True
        _err("CLI --help 失败")
        return False
    except Exception as e:
        _err(f"CLI 异常: {e}")
        return False


def check_daemon_import() -> bool:
    try:
        r = subprocess.run(
            [sys.executable, "-c", "from mnemos_daemon import main; print('daemon_import_ok')"],
            cwd=PROJECT_ROOT,
            capture_output=True, text=True, timeout=10,
        )
        if "daemon_import_ok" in r.stdout:
            _ok("Daemon 可导入")
            return True
        _err("Daemon 导入失败")
        return False
    except Exception as e:
        _err(f"Daemon 导入异常: {e}")
        return False


def check_doctor() -> dict:
    result = {"ok": False, "warnings": [], "errors": []}
    try:
        r = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "mnemos_cli.py"), "doctor"],
            capture_output=True, text=True, timeout=30,
        )
        stdout = r.stdout + r.stderr
        if "知识库健康度" in stdout:
            _ok("doctor 可运行")
            result["ok"] = True
        else:
            _err("doctor 输出异常")
            result["errors"].append("doctor 未输出预期内容")

        # 提取警告
        for line in stdout.splitlines():
            if "⚠" in line or "警告" in line or "warning" in line.lower():
                result["warnings"].append(line.strip())
        if result["warnings"]:
            _warn(f"doctor 发现 {len(result['warnings'])} 条警告")
    except Exception as e:
        _err(f"doctor 运行异常: {e}")
        result["errors"].append(str(e))
    return result


def check_integration_tests(full: bool = False) -> dict:
    result = {"ok": False, "passed": 0, "failed": 0}
    if not full:
        _warn("跳过集成测试（使用 --full 启用）")
        result["ok"] = True
        return result

    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest",
             "tests/integration/test_l1_memos_duplicate_fallback.py",
             "tests/integration/test_memos_wiki_traceability.py",
             "tests/integration/test_distill_to_kg_event_path.py",
             "tests/integration/test_worker_kg_event_path.py",
             "tests/integration/test_deferred_distill_kg_event.py",
             "-v", "--tb=short"],
            cwd=PROJECT_ROOT,
            capture_output=True, text=True, timeout=120,
        )
        stdout = r.stdout
        if "passed" in stdout:
            import re
            m = re.search(r'(\d+) passed', stdout)
            if m:
                result["passed"] = int(m.group(1))
            m = re.search(r'(\d+) failed', stdout)
            if m:
                result["failed"] = int(m.group(1))
            if result["failed"] == 0:
                _ok(f"核心集成测试通过: {result['passed']} passed")
                result["ok"] = True
            else:
                _err(f"集成测试失败: {result['failed']} failed")
        else:
            _err("集成测试未产生有效输出")
    except Exception as e:
        _err(f"集成测试运行异常: {e}")
    return result


def check_db_writable() -> bool:
    try:
        from core.config import get_config
        cfg = get_config()
        test_paths = [
            cfg.data_dir,
            cfg.wiki_dir,
        ]
        all_ok = True
        for p in test_paths:
            if not p.exists():
                p.mkdir(parents=True, exist_ok=True)
            if not p.exists() or not p.is_dir():
                _err(f"目录不存在: {p}")
                all_ok = False
            else:
                # 尝试写临时文件
                test_file = p / ".mnemos_write_test"
                try:
                    test_file.write_text("ok")
                    test_file.unlink()
                except Exception as e:
                    _err(f"目录不可写: {p} ({e})")
                    all_ok = False
        if all_ok:
            _ok("数据目录可写")
        return all_ok
    except Exception as e:
        _err(f"目录检查异常: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Mnemos 部署后验证")
    parser.add_argument("--full", action="store_true", help="运行完整验证（含集成测试）")
    parser.add_argument("--json", action="store_true", help="JSON 输出（用于脚本集成）")
    args = parser.parse_args()

    print("=" * 60)
    print("Mnemos 部署验证")
    print("=" * 60)
    print(f"项目路径: {PROJECT_ROOT}")

    results = {}
    all_ok = True

    _step("1. 环境检查")
    results["python"] = check_python_version()
    all_ok &= results["python"]

    _step("2. 代码编译检查")
    results["compileall"] = check_compileall()
    all_ok &= results["compileall"]

    _step("3. CLI / Daemon 可导入")
    results["cli"] = check_cli_help()
    results["daemon"] = check_daemon_import()
    all_ok &= results["cli"] and results["daemon"]

    _step("4. 目录权限")
    results["db_writable"] = check_db_writable()
    all_ok &= results["db_writable"]

    _step("5. Doctor 诊断")
    doctor_result = check_doctor()
    results["doctor"] = doctor_result["ok"]
    all_ok &= results["doctor"]

    _step("6. 核心链路集成测试")
    test_result = check_integration_tests(full=args.full)
    results["integration_tests"] = test_result["ok"]
    all_ok &= results["integration_tests"]

    # 总结
    print("\n" + "=" * 60)
    if all_ok:
        print("✅ 验证通过 — Mnemos 已就绪")
    else:
        print("❌ 验证未通过 — 请检查上方错误项")
    print("=" * 60)

    if args.json:
        print(json.dumps({
            "ok": all_ok,
            "results": results,
            "doctor_warnings": doctor_result.get("warnings", []),
            "doctor_errors": doctor_result.get("errors", []),
            "tests_passed": test_result.get("passed", 0),
            "tests_failed": test_result.get("failed", 0),
        }, ensure_ascii=False, indent=2))

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
