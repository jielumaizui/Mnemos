#!/usr/bin/env python3
"""检查源码中的裸 ``except Exception:`` 块。

S8 要求：每个 ``except Exception:`` 至少要记录日志，或带有说明为何允许
静默处理的注释。本脚本扫描 ``core/``、``integrations/``、``daemon/``、
``scripts/`` 以及顶层入口文件，找出缺少日志和说明的 bare except，并
支持 ``--fix`` 自动添加 ``# DEBT(S8):`` 注释。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Iterable, List, Tuple

# 扫描范围（与审计文档一致）
SCAN_PATHS: Tuple[str, ...] = (
    "core",
    "integrations",
    "daemon",
    "scripts",
    "mnemos_cli.py",
    "mnemos_daemon.py",
)

# 匹配 ``except Exception:``（允许额外的类型捕获，如 ``except Exception as exc:``）
_EXCEPT_RE = re.compile(r"^\s*except\s+Exception\b")
# 行内已有说明性注释即视为已处理
_HAS_COMMENT_RE = re.compile(r"#.*?(DEBT\(S8\)|TODO|FIXME|NOTE|P2-FIX|noqa)", re.IGNORECASE)
# 下一行包含日志调用即视为已处理
_LOG_RE = re.compile(
    r"\b(logger|log|logging)\.(debug|info|warning|warn|error|exception|critical)\("
)

_REASONS = {
    "pass": "静默容错，避免副作用影响主流程",
    "continue": "容错跳过，避免单条记录中断批量处理",
    "break": "容错退出循环，避免异常扩散",
    "return": "容错降级，返回默认值避免局部失败扩散",
}


def _next_statement(lines: List[str], idx: int) -> Tuple[str, int]:
    """返回 except 块之后的第一个非空、非纯注释语句。"""
    for j in range(idx + 1, len(lines)):
        stripped = lines[j].strip()
        if not stripped or stripped.startswith("#"):
            continue
        return stripped, j
    return "", -1


def _classify(statement: str) -> str:
    token = statement.split()[0] if statement.split() else ""
    if token in ("pass", "continue", "break"):
        return _REASONS[token]
    if token == "return":
        return _REASONS["return"]
    return "容错降级，避免局部失败扩散"


def scan_file(path: Path) -> List[Tuple[int, str, str]]:
    """返回 [(行号, except 行文本, 下一行语句), ...]。"""
    findings: List[Tuple[int, str, str]] = []
    text = path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not _EXCEPT_RE.match(line):
            continue
        # 行内已有注释或 noqa，视为已说明
        if _HAS_COMMENT_RE.search(line):
            continue
        nxt, _ = _next_statement(lines, i)
        if not nxt:
            continue
        # 下一行是日志调用，视为已处理
        if _LOG_RE.search(nxt):
            continue
        # 常见静默动作
        if nxt.split()[0] in ("pass", "continue", "break", "return"):
            findings.append((i + 1, line, nxt))
    return findings


def scan_directory(root: Path) -> List[Tuple[Path, int, str, str]]:
    results: List[Tuple[Path, int, str, str]] = []
    for rel in SCAN_PATHS:
        path = root / rel
        if path.is_file():
            files: Iterable[Path] = (path,)
        else:
            files = sorted(path.rglob("*.py"))
        for fpath in files:
            for lineno, line, nxt in scan_file(fpath):
                results.append((fpath, lineno, line, nxt))
    return results


def fix_file(path: Path) -> int:
    """自动为裸 except 添加 # DEBT(S8) 注释。返回修改行数。"""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    edited = 0
    for i, line in enumerate(lines):
        if not _EXCEPT_RE.match(line):
            continue
        if _HAS_COMMENT_RE.search(line):
            continue
        nxt, _ = _next_statement(lines, i)
        if not nxt or nxt.split()[0] not in ("pass", "continue", "break", "return"):
            continue
        if _LOG_RE.search(nxt):
            continue
        # 避免重复修复
        if "# DEBT(S8):" in line:
            continue
        reason = _classify(nxt)
        stripped = line.rstrip()
        lines[i] = f"{stripped}  # DEBT(S8): {reason}"
        edited += 1
    if edited:
        path.write_text("\n".join(lines) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")
    return edited


def main() -> int:
    parser = argparse.ArgumentParser(description="Check for bare except Exception blocks.")
    parser.add_argument("--fix", action="store_true", help="Auto-annotate findings with # DEBT(S8).")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    if args.fix:
        total = 0
        for rel in SCAN_PATHS:
            path = root / rel
            files = (path,) if path.is_file() else sorted(path.rglob("*.py"))
            for fpath in files:
                total += fix_file(fpath)
        print(f"Annotated {total} bare except block(s) with # DEBT(S8).")
        return 0

    results = scan_directory(root)
    if not results:
        print("No bare except Exception blocks found.")
        return 0

    for fpath, lineno, line, nxt in results:
        print(f"{fpath}:{lineno}: {line.strip()} -> {nxt.split()[0]}")
    print(f"\nFound {len(results)} bare except Exception block(s).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
