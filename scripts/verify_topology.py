#!/usr/bin/env python3
"""全量校验 .project-topology.md 的所有文件引用与数字断言。

用法: python3 scripts/verify_topology.py
"""
import re
import subprocess
import sys
from pathlib import Path

# 脚本位于 <repo>/scripts/ 下 → 仓库根 = 本文件上级的上级
ROOT = Path(__file__).resolve().parent.parent
TOPOLOGY = ROOT / ".project-topology.md"


def git_ls() -> set[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files"],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        print(f"❌ git ls-files 超时（{ROOT}）")
        sys.exit(2)
    if out.returncode != 0:
        print(f"❌ 无法读取 git 索引（{ROOT} 不是 git 仓库？）: {out.stderr.strip()}")
        sys.exit(2)
    return {line for line in out.stdout.splitlines() if line}


def read_or_fail(rel: str) -> str | None:
    """读取文件内容；不存在/不可读时返回 None（调用方报告，而不是崩溃）。"""
    p = ROOT / rel
    try:
        return p.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return None


def parse_bracket_body(src: str, anchor: str) -> str | None:
    """括号配平解析：从 anchor（如 'X = ('）起，返回到配平 ) 为止的正文。

    引号包裹的字符串字面量内的括号不参与配平（防止字符串含
    不成对括号时提前终止/越界）。注意：注释内的括号不特殊处理，
    若注释本身含括号须成对出现，否则会干扰配平。
    """
    i = src.find(anchor)
    if i < 0:
        return None
    depth = 1
    j = i + len(anchor)
    quote: str | None = None      # 当前所在引号（" 或 '），None=不在字符串内
    escaped = False               # 上一位是反斜杠（\\\" 等转义）
    while j < len(src) and depth > 0:
        ch = src[j]
        if quote is not None:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
        elif ch in ("\"", "'"):
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        j += 1
    return src[i + len(anchor): j - 1] if depth == 0 else None


def submodule_dirs(tracked: set[str], prefix: str) -> set[str]:
    out = set()
    for f in tracked:
        if f.startswith(prefix):
            parts = f.split("/")
            if len(parts) >= 3 and not parts[1].endswith(".py"):
                out.add(parts[1])
    return out


def main() -> int:
    tracked = git_ls()
    try:
        text = TOPOLOGY.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, PermissionError) as e:
        print(f"❌ 无法读取拓扑文档: {TOPOLOGY} ({e})")
        return 2
    failures: list[str] = []

    # ── 1. 按行提取所有文件路径引用并校验 ──
    # 路径型 token：含 / 的路径 或 裸文件名（.py/.json/.yaml/.md/.toml/.txt）
    TOKEN_RE = re.compile(
        r"(?<![\w/])"                      # 前界：不是单词字符或 /
        r"(?:[\w./~-]+/[\w./*?-]+)"        # 含 / 的路径（含 ~ 开头）
        r"|(?:[\w*?-]+(?:\.[\w*?-]+)*\.(?:py|json|yaml|yml|md|toml|txt))"  # 裸文件名（含双后缀 config.example.yaml）
    )
    # 只校验看起来像仓库内路径的 token：已知根前缀 或 带扩展名
    ROOT_PREFIXES = (
        "core/", "daemon/", "integrations/", "scripts/", "tests/",
        "config/", "prompts/", "docs/", "build/", "pyproject.toml",
        "README", "AGENTS",
    )
    # 已知非文件引用：通配占位碎片 / 配置键名 / 叙述省略
    NONFILE_EXACT = {
        "_source.py", "vaults.raw", "vaults.mnemos",
        "vaults.raw/vaults.mnemos", "configs/main.json",
    }

    def looks_like_repo_ref(tok: str) -> bool:
        if tok in NONFILE_EXACT:
            return False
        if re.search(r"[\u4e00-\u9fff]", tok):
            return False
        if tok.startswith("~/"):
            return True  # home 路径在下方单独解析校验，不得过滤
        if "~" in tok:
            return False  # ~foo/ 形式：既非 home 路径也非仓库路径，跳过
        if tok.endswith("/"):
            return tok.startswith(ROOT_PREFIXES)
        return (
            "." in tok  # 带扩展名
            or tok.startswith(ROOT_PREFIXES)
        )

    refs: set[str] = set()
    for line in text.splitlines():
        # 剥离成对 markdown 粗体标记（**x** → x），保留内容与 glob 通配（** 不成对时不动）
        line = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", line)
        for tok in TOKEN_RE.findall(line):
            tok = tok.strip("`.,;:()[]{}\"'|?")
            if not tok:
                continue
            if not looks_like_repo_ref(tok):
                continue
            refs.add(tok)
    checked = 0
    for ref in sorted(refs):
        # home 路径（~/Documents/raw 等）→ 解析真实路径校验
        if ref.startswith("~/"):
            real = Path.home() / ref[2:]
            if real.is_dir() or real.is_file():
                checked += 1
            else:
                failures.append(f"home 路径不存在: {ref}")
            continue
        # glob 引用（如 *_source.py、integrations/sources/*.py、core/*/）：
        # 必须在目录分支之前判断（core/*/ 以 / 结尾但带通配符）
        if any(ch in ref for ch in "*?"):
            is_dir_glob = ref.endswith("/")
            core_esc = re.escape(ref.rstrip("/")).replace(r"\*", ".*").replace(r"\?", ".")
            if is_dir_glob:
                # 目录 glob（core/*/）→ 匹配其下任意 tracked 文件
                hits = [
                    f for f in tracked
                    if re.match("^" + core_esc + "/", f)
                    or re.match("^.*/" + core_esc + "/", f)
                ]
            else:
                pattern = re.compile("^" + core_esc + "$")
                name_pattern = re.compile("^.*/" + core_esc + "$")
                hits = [
                    f for f in tracked
                    if pattern.match(f) or name_pattern.match(f)
                ]
            if not hits:
                failures.append(f"glob 无匹配: {ref}")
            checked += 1
            continue
        # 目录引用
        if ref.endswith("/"):
            if not (ROOT / ref).is_dir():
                failures.append(f"目录不存在: {ref}")
            elif not any(f.startswith(ref) for f in tracked):
                failures.append(f"目录在 git 索引中无文件（本地残留）: {ref}")
            checked += 1
            continue
        p = ROOT / ref
        if p.is_dir():
            # 无尾斜杠目录引用（**core/trust**）与带尾斜杠同等强度：须在 git 索引中有文件
            if not any(f.startswith(ref + "/") for f in tracked):
                failures.append(f"目录在 git 索引中无文件（本地残留）: {ref}")
            checked += 1
            continue
        if p.is_file():
            checked += 1
            continue
        if ref in tracked:
            checked += 1
            continue
        # 叙述中省略 .py 后缀（如 "daemon/agent_source_runtime"）
        if (ROOT / (ref + ".py")).is_file():
            checked += 1
            continue
        # 树图/表格缩略写法：ref 是某 tracked 文件的后缀路径
        # （如 "agora_tools/schema.py" 实际是 integrations/agora_tools/schema.py）
        suffix_hits = [f for f in tracked if f.endswith("/" + ref)]
        if suffix_hits:
            if len(suffix_hits) > 1:
                print(f"⚠️ 后缀引用存在歧义（匹配 {len(suffix_hits)} 个文件）: {ref} → {suffix_hits}")
            checked += 1
            continue
        # 裸文件名 → git 索引 basename 匹配（含补 .py 后缀的叙述写法）
        if "/" not in ref:
            target = ref if ref.endswith(".py") else ref + ".py"
            basename_hits = [
                f for f in tracked
                if Path(f).name == ref or Path(f).name == target
            ]
            if basename_hits:
                if len(basename_hits) > 1:
                    print(f"⚠️ 裸名引用存在歧义（匹配 {len(basename_hits)} 个文件）: {ref} → {basename_hits}")
                checked += 1
                continue
        failures.append(f"文件不存在: {ref}")

    # ── 2. 正向解析数字断言并校验 ──
    # 模式 → (计算函数, 语义)：文档写什么数字，就与实际值比对
    py = [f for f in tracked if f.endswith(".py")]
    cli_cmds = [f for f in py if f.startswith("core/cli/commands/") and not f.endswith("__init__.py")]

    def counted(prefix: str) -> int:
        return len([f for f in py if f.startswith(prefix)])

    def counted_no_init(prefix: str) -> int:
        """Count .py files in a submodule excluding __init__.py."""
        return len([
            f for f in py
            if f.startswith(prefix)
            and not f.endswith("__init__.py")
        ])

    def core_top_count() -> int:
        """core 顶层 .py 文件数（排除 __init__.py，即 core/xxx.py 形式）。"""
        return len([
            f for f in py
            if f.startswith("core/")
            and f.count("/") == 1
            and not f.endswith("__init__.py")
        ])

    def integ_top_count() -> int:
        """integrations 顶层 .py 文件数（排除 __init__.py）。"""
        return len([
            f for f in py
            if f.startswith("integrations/")
            and f.count("/") == 1
            and not f.endswith("__init__.py")
        ])

    def cli_count() -> int:
        return len(cli_cmds)

    def submodules() -> int:
        return len(submodule_dirs(tracked, "core/"))

    def facade_lines() -> int:
        src = read_or_fail("core/application/facade.py")
        if src is None:
            failures.append("核心文件缺失（影响 facade 行数断言）: core/application/facade.py")
            return -1
        return len(src.splitlines())

    def bus_lines() -> int:
        src = read_or_fail("core/mnemos_bus.py")
        if src is None:
            failures.append("核心文件缺失（影响 mnemos_bus 行数断言）: core/mnemos_bus.py")
            return -1
        return len(src.splitlines())

    def facade_methods() -> int:
        src = read_or_fail("core/application/facade.py")
        if src is None:
            failures.append("核心文件缺失（影响 facade 方法数断言）: core/application/facade.py")
            return -1
        return sum(
            1 for line in src.splitlines()
            if re.match(r"    (?:async )?def ", line)
        )

    def source_parsers() -> int:
        return len([f for f in py if f.startswith("integrations/sources/") and f.endswith("_source.py")])

    def source_helpers() -> int:
        return len([
            f for f in py
            if f.startswith("integrations/sources/")
            and f.endswith(".py")
            and not f.endswith("_source.py")
            and not f.endswith("__init__.py")
        ])

    def required_consumers() -> int:
        src = read_or_fail("core/wiki_projection_lifecycle.py")
        if src is None:
            failures.append("核心文件缺失（影响 required consumers 断言）: core/wiki_projection_lifecycle.py")
            return -1
        body = parse_bracket_body(src, "DEFAULT_REQUIRED_CONSUMERS = (")
        return len(re.findall(r'"([^"]+)"', body)) if body is not None else -1

    def authority_classes() -> int:
        src = read_or_fail("core/evidence/source_authority.py")
        if src is None:
            failures.append("核心文件缺失（影响 source_authority 断言）: core/evidence/source_authority.py")
            return -1
        # 4 空格缩进 + 全大写常量键 = 权限类常量；值放宽（含数字/下划线），防未来值变体漏计
        return len(re.findall(r'^\s{4}[A-Z][A-Z0-9_]* = "[^"]*"', src, re.M))

    def version() -> str:
        src = read_or_fail("pyproject.toml")
        if src is None:
            failures.append("核心文件缺失（影响版本断言）: pyproject.toml")
            return "?"
        # 只取 [project] 段内的 version 键：段体止于下一个 ^[ 行，跨段不匹配
        m = re.search(
            r'^\[project\]\s*$(?P<body>(?:(?!^\[).)*?)^version\s*=\s*"(\d+\.\d+\.\d+)"',
            src, re.S | re.M,
        )
        if m:
            return m.group(2)
        # [project] 段存在但无 version 键 → 显式报告而非静默返回 ?
        if re.search(r'^\[project\]\s*$', src, re.M):
            failures.append("pyproject [project] 段缺少 version 键（影响版本断言）")
        return "?"

    # 精确断言：(正则, 计算值, 校验说明)。模式必须至少命中 1 次，否则视为失效
    exact_asserts = [
        (r"(\d+) 个 Python 文件(?![0-9])", len(py), "总 Python 文件数"),
        (r"tests (\d+)，", counted("tests/"), "tests 目录"),
        (r"scripts (\d+)(?![0-9])", counted("scripts/"), "scripts 目录"),
        (r"commands/（(\d+) 个命令模块）", cli_count(), "CLI 命令模块"),
        (r"\| (\d+) 个命令解析与执行", cli_count(), "§3 CLI 命令数"),
        (r"commands/ (\d+) 模块）", cli_count(), "§9 命令路由表"),
        (r"守护服务层（(\d+) 文件）", counted("daemon/"), "daemon 文件数"),
        (r"认知层（(\d+) 文件）", counted("core/cognitive/"), "cognitive 文件数"),
        (r"KIA 神话系（(\d+) 文件）", counted("core/kia/"), "kia 文件数"),
        (r"蒸馏引擎（(\d+) 文件）", counted("core/hephaestus/"), "hephaestus 文件数"),
        (r"捕获管线（(\d+) 文件）", counted("core/sync_framework/"), "sync_framework 文件数"),
        (r"（(\d+) 个子模块）", submodules(), "core 子模块数"),
        (r"门面 Facade（MCP 工具实现层，(\d+) 行）", facade_lines(), "facade 行数"),
        (r"mnemos_bus.py\(事件总线 (\d+) 行\)", bus_lines(), "mnemos_bus 行数"),
        (r"统一入口（(\d+) 个方法）", facade_methods(), "facade 方法数"),
        (r"(\d+) 个平台解析器 \+ (\d+) 辅助文件", (source_parsers(), source_helpers()), "sources 解析器+辅助"),
        (r"audit_\* \((\d+)\)", counted("scripts/audit_"), "audit 脚本数"),
        (r"reconcile_\* \((\d+)\)", counted("scripts/reconcile_"), "reconcile 脚本数"),
        # L4 新增断言：子模块文件数（排除 __init__.py）
        (r"core 顶层模块（(\d+) 个）", core_top_count(), "core 顶层模块数"),
        (r"core/agent_kit（(\d+) 个）", counted_no_init("core/agent_kit/"), "agent_kit 文件数"),
        (r"core/application（(\d+) 个）", counted_no_init("core/application/"), "application 文件数"),
        (r"core/persona（(\d+) 个）", counted_no_init("core/persona/"), "persona 文件数"),
        (r"core/reflection（(\d+) 个）", counted_no_init("core/reflection/"), "reflection 文件数"),
        (r"core/trust（(\d+) 个）", counted_no_init("core/trust/"), "trust 文件数"),
        (r"daemon（(\d+) 个）", counted_no_init("daemon/"), "daemon 文件数(no-init)"),
        (r"integrations/ 顶层（(\d+) 个）", integ_top_count(), "integrations 顶层文件数"),
    ]
    for pattern, actual, label in exact_asserts:
        matches = list(re.finditer(pattern, text))
        if not matches:
            failures.append(f"断言模式失效（零命中，需修正脚本）: {pattern} | {label}")
            continue
        for m in matches:
            if isinstance(actual, tuple):
                claimed = tuple(int(x) for x in m.groups())
                if claimed != actual:
                    failures.append(f"{label}: 文档写 {claimed} != 实际 {actual}")
            else:
                claimed = int(m.group(1))
                if claimed != actual:
                    ctx = text[max(0, m.start() - 30): m.end() + 20].replace("\n", " ")
                    failures.append(f"{label}: 文档写 {claimed} != 实际 {actual} | 上下文: ...{ctx}...")

    def sqlite_count() -> int:
        """统计 ~/.mnemos 下实际 SQLite 文件数（运行时数据，不在 git 索引）。"""
        data_dir = Path.home() / ".mnemos"
        if not data_dir.is_dir():
            print("⚠️ ~/.mnemos 不存在，SQLite 库下限断言跳过")
            return None
        return len(list(data_dir.rglob("*.db")))

    # 约数/下限断言：~800（±5%）、250+、600+、50+、50+
    approx_asserts = [
        (
            r"core\+daemon\+integrations ~(\d+)",
            len([f for f in py if f.startswith(("core/", "daemon/", "integrations/"))]),
            0.05,
            "core+daemon+integrations",
        ),
        (r"(\d+)\+ 运维/审计/对账脚本", counted("scripts/"), None, "scripts 下限"),
        (r"(\d+)\+ 测试", counted("tests/"), None, "tests 下限"),
        (r"懒加载 (\d+)\+ 子命令", cli_count(), None, "CLI 懒加载下限"),
    ]
    sqlite_actual = sqlite_count()
    if sqlite_actual is not None:
        approx_asserts.append(
            (r"全库 (\d+)\+ 个 SQLite 文件", sqlite_actual, None, "SQLite 库下限")
        )
    for pattern, actual, tol, label in approx_asserts:
        matches = list(re.finditer(pattern, text))
        if not matches:
            failures.append(f"约数模式失效（零命中，需修正脚本）: {pattern} | {label}")
            continue
        for m in matches:
            claimed = int(m.group(1))
            if tol is not None:
                if abs(actual - claimed) > claimed * tol:
                    failures.append(f"{label} 约数: 文档写 ~{claimed} != 实际 {actual}")
            elif actual < claimed:
                failures.append(f"{label} 下限: 文档写 {claimed}+，实际 {actual}")

    # 语义断言：期望值动态取自代码，文档必须与代码一致
    semantic = [
        (r"(\d+) 个 required consumer", required_consumers(), "required consumers"),
        (r"权限只有 (\d+) 类", authority_classes(), "source_authority 权限类"),
        (r"mnemos v(\d+\.\d+\.\d+)", version(), "版本号"),
    ]
    for pattern, expected, label in semantic:
        matches = list(re.finditer(pattern, text))
        if not matches:
            failures.append(f"语义模式失效（零命中，需修正脚本）: {pattern} | {label}")
            continue
        for m in matches:
            claimed = m.group(1)
            if str(claimed) != str(expected):
                failures.append(f"{label}: 文档写 {claimed} != 代码实际 {expected}")

    # ── 3. 汇总 ──
    print(f"文件引用校验: {checked} 条")
    print(f"数字断言校验: {len(exact_asserts) + len(approx_asserts) + len(semantic)} 组")
    if failures:
        print(f"\n❌ 发现 {len(failures)} 个问题:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\n✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
