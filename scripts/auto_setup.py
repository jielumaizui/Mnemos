#!/usr/bin/env python3
"""
Mnemos 自动部署脚本 — 一键开箱即用

功能：
1. 检测 Python >= 3.10
2. 安装项目依赖
3. 自动检测 Memos 服务器（进程 / 端口 5230）
4. 自动检测 Obsidian Vault（常见路径扫描）
5. 生成 ~/.mnemos/configs/main.json（运行时权威配置）
6. 初始化标准 wiki 目录结构（00-Inbox ~ 07-Shadow）
7. 安装 AI Agent Hooks
8. 启动后台守护进程
9. 配置系统定时任务（macOS launchd / Linux cron / Windows Task Scheduler）

用法：
    python3 scripts/auto_setup.py [--yes] [--skip-memos] [--skip-obsidian]

选项：
    --yes          全自动模式，不提示确认（适合 CI）
    --skip-memos   跳过 Memos 检测和配置
    --skip-obsidian 跳过 Obsidian 检测和配置
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
# 确保项目根目录在 sys.path 中，支持从任意位置运行
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 当前使用的 Python 解释器（可能被虚拟环境替换）
_PYTHON_EXE = sys.executable

# 跨平台 Obsidian Vault 常见路径
OBSIDIAN_VAULT_PATHS = {
    "Darwin": [
        "~/Documents/Obsidian Vault",
        "~/Documents/Obsidian",
        "~/Library/Mobile Documents/iCloud~md~obsidian/Documents",
    ],
    "Linux": [
        "~/Documents/Obsidian Vault",
        "~/Documents/Obsidian",
        "~/obsidian",
    ],
    "Windows": [
        r"~\Documents\Obsidian Vault",
        r"~\Documents\Obsidian",
        r"~\Obsidian",
    ],
}

# Memos 服务器进程名常见模式
MEMOS_PROCESS_PATTERNS = ["memos", "memos-server"]


def print_step(n: int, total: int, title: str) -> None:
    print(f"\n{'='*60}")
    print(f"[{n}/{total}] {title}")
    print("=" * 60)


def print_ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def print_warn(msg: str) -> None:
    print(f"  ⚠ {msg}")


def print_err(msg: str) -> None:
    print(f"  ✗ {msg}")


def ask(prompt: str, default: str = "", yes_mode: bool = False) -> str:
    if yes_mode:
        print(f"  {prompt} (auto: {default or 'skip'})")
        return default
    try:
        return input(f"  {prompt} ").strip() or default
    except (EOFError, KeyboardInterrupt):
        return default


def ask_yes_no(prompt: str, default: bool = True, yes_mode: bool = False) -> bool:
    if yes_mode:
        print(f"  {prompt} (auto: {'yes' if default else 'no'})")
        return default
    default_str = "Y/n" if default else "y/N"
    ans = input(f"  {prompt} [{default_str}]: ").strip().lower()
    if not ans:
        return default
    return ans in ("y", "yes")


# ── 步骤 1: Python 版本 ──

def check_python() -> bool:
    v = sys.version_info
    print(f"  Python {v.major}.{v.minor}.{v.micro}")
    if v >= (3, 10):
        print_ok("版本满足 >= 3.10")
        return True
    print_err(f"需要 Python >= 3.10，当前 {v.major}.{v.minor}")
    return False


# ── 步骤 2: 安装依赖 ──

def _ensure_venv() -> Optional[Path]:
    """在项目根目录创建 .venv，返回 venv 中的 python 路径"""
    venv_dir = PROJECT_ROOT / ".venv"
    if not venv_dir.exists():
        print("  创建虚拟环境 .venv ...")
        result = subprocess.run(
            [sys.executable, "-m", "venv", str(venv_dir)],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print_err(f"创建虚拟环境失败: {result.stderr[:200]}")
            return None
    # 确定虚拟环境的 python 路径
    if platform.system() == "Windows":
        venv_python = venv_dir / "Scripts" / "python.exe"
    else:
        venv_python = venv_dir / "bin" / "python"
    if venv_python.exists():
        return venv_python
    print_err(f"虚拟环境 Python 未找到: {venv_python}")
    return None


def install_dependencies(yes_mode: bool = False) -> bool:
    print("  安装项目依赖...")
    req_file = PROJECT_ROOT / "requirements.txt"
    if not req_file.exists():
        print_warn("requirements.txt 不存在，跳过")
        return True

    global _PYTHON_EXE
    extras = f"{PROJECT_ROOT}[dev]"

    def _try_install(python: str, silent: bool = False) -> tuple[bool, str]:
        c = [python, "-m", "pip", "install", "-e", str(PROJECT_ROOT), "-e", extras]
        if silent:
            r = subprocess.run(c, capture_output=True, text=True)
            return r.returncode == 0, r.stderr
        # 实时输出进度，避免用户长时间看不到反馈
        print("  开始安装依赖，预计需要 1-3 分钟（取决于网络速度）...")
        print("  首次安装可能需要编译部分包，请耐心等待...")
        r = subprocess.run(c)
        if r.returncode == 0:
            return True, ""
        # 失败后重试并捕获输出以诊断
        r2 = subprocess.run(c, capture_output=True, text=True)
        return False, r2.stderr

    # 先尝试直接安装（实时输出进度）
    ok, err = _try_install(_PYTHON_EXE, silent=False)
    if ok:
        print_ok("依赖安装完成")
        return True

    # 检测 externally-managed-environment
    if "externally-managed-environment" in err or "externally managed" in err:
        print_warn("检测到系统 Python 外部管理限制，尝试创建虚拟环境...")
        venv_python = _ensure_venv()
        if venv_python:
            _PYTHON_EXE = str(venv_python)
            ok, err2 = _try_install(_PYTHON_EXE, silent=False)
            if ok:
                print_ok(f"依赖已在虚拟环境安装: {venv_python}")
                print("  后续操作将使用虚拟环境 Python")
                return True
            print_err(f"虚拟环境安装失败: {err2[:200]}")
            return False
        print_err("无法创建虚拟环境")
        return False

    # 其他错误
    print_err(f"安装失败: {err[:200]}")
    return False


# ── 步骤 3: 检测 Memos ──

def find_memos_process() -> Optional[Path]:
    """查找 Memos 服务器进程，返回可执行文件路径"""
    try:
        if platform.system() == "Darwin":
            result = subprocess.run(
                ["pgrep", "-lf", "memos"],
                capture_output=True, text=True, timeout=5
            )
        elif platform.system() == "Linux":
            result = subprocess.run(
                ["pgrep", "-af", "memos"],
                capture_output=True, text=True, timeout=5
            )
        else:
            # Windows: wmic 已弃用，改用 PowerShell Get-Process
            try:
                result = subprocess.run(
                    ["powershell", "-Command",
                     "Get-Process | Where-Object {$_.ProcessName -like '*memos*'} "
                     "| Select-Object -ExpandProperty Path"],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0 and result.stdout.strip():
                    for line in result.stdout.strip().split("\n"):
                        line = line.strip()
                        if line and Path(line).exists():
                            return Path(line)
            except Exception:
                pass
            # 回退: tasklist
            try:
                result = subprocess.run(
                    ["tasklist", "/FI", "IMAGENAME eq memos*", "/FO", "CSV"],
                    capture_output=True, text=True, timeout=5
                )
            except Exception:
                result = None
        if result and result.returncode == 0 and result.stdout:
            for line in result.stdout.strip().split("\n"):
                for pat in MEMOS_PROCESS_PATTERNS:
                    if pat in line.lower():
                        # 尝试提取路径 (支持带空格的路径)
                        for match in re.finditer(r'["\']?((?:[A-Za-z]:)?[/\\][^"\'\s]+(?:\s[^"\'\s]+)*)["\']?', line):
                            p = match.group(1)
                            if "memos" in p.lower():
                                exe = Path(p)
                                if exe.exists():
                                    return exe
            return Path("memos")  # 进程存在但路径不确定
    except Exception:
        pass
    return None


def detect_memos_port(port: int = 5230) -> bool:
    """检测 Memos 端口是否开放"""
    import socket
    try:
        with socket.create_connection(("localhost", port), timeout=2):
            return True
    except OSError:
        return False


def detect_memos_data_dir() -> Optional[Path]:
    """根据进程命令行提取 Memos 数据目录（支持带空格路径）"""
    try:
        if platform.system() in ("Darwin", "Linux"):
            result = subprocess.run(
                ["pgrep", "-af", "memos"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    # 支持带空格的路径（引号包裹或到下一个参数前）
                    m = re.search(r'--data\s+(["\'])(.+?)\1', line)
                    if m:
                        path = Path(m.group(2)).expanduser()
                        if path.exists():
                            return path
                    else:
                        m = re.search(r'--data\s+(\S+)', line)
                        if m:
                            path = Path(m.group(1)).expanduser()
                            if path.exists():
                                return path
    except Exception:
        pass
    # 默认猜测
    defaults = [
        Path.home() / "memos-server",
        Path.home() / ".memos",
        Path.home() / "Documents" / "memos",
    ]
    for p in defaults:
        if p.exists():
            return p
    return None


def setup_memos(skip: bool = False, yes_mode: bool = False) -> Tuple[bool, Optional[str]]:
    if skip:
        print_warn("跳过 Memos 检测")
        return True, None

    print("  检测 Memos 服务器...")
    proc = find_memos_process()
    port_open = detect_memos_port()

    if proc or port_open:
        print_ok(f"Memos 已运行 (进程: {proc or 'unknown'}, 端口 5230: {'开放' if port_open else '未检测到'})")
        data_dir = detect_memos_data_dir()
        if data_dir:
            print_ok(f"Memos 数据目录: {data_dir}")
    else:
        print_warn("未检测到 Memos 服务器（端口 5230 未开放，无 memos 进程）")
        if ask_yes_no("是否继续配置 Memos？", default=False, yes_mode=yes_mode):
            url = ask("Memos API 地址 (如 http://localhost:5230):", default="http://localhost:5230", yes_mode=yes_mode)
            return True, url
        return True, None

    url = "http://localhost:5230"
    if not yes_mode:
        custom = ask(f"Memos API 地址 (回车={url}):", default=url, yes_mode=yes_mode)
        if custom:
            url = custom
    print_ok(f"配置 Memos URL: {url}")
    return True, url


# ── 步骤 4: 检测 Obsidian ──

def find_obsidian_vaults() -> List[Path]:
    """扫描常见路径查找 Obsidian Vault"""
    found = []
    system = platform.system()
    paths = OBSIDIAN_VAULT_PATHS.get(system, [])
    for p in paths:
        expanded = Path(p).expanduser()
        if expanded.exists() and expanded.is_dir():
            # 检查是否真的是 vault（有 .obsidian 目录或 Markdown 文件）
            if (expanded / ".obsidian").exists() or list(expanded.glob("*.md")):
                found.append(expanded)
    return found


def setup_obsidian(skip: bool = False, yes_mode: bool = False) -> Tuple[bool, Optional[Path]]:
    if skip:
        print_warn("跳过 Obsidian 检测")
        return True, None

    print("  检测 Obsidian Vault...")
    vaults = find_obsidian_vaults()

    if vaults:
        print_ok(f"发现 {len(vaults)} 个 Vault:")
        for i, v in enumerate(vaults, 1):
            print(f"    {i}. {v}")
        if len(vaults) == 1:
            chosen = vaults[0]
        elif yes_mode:
            chosen = vaults[0]
            print(f"  自动选择第 1 个: {chosen}")
        else:
            idx = ask("选择 Vault 编号 (回车=1):", default="1", yes_mode=yes_mode)
            try:
                chosen = vaults[int(idx) - 1]
            except (ValueError, IndexError):
                chosen = vaults[0]
        wiki_dir = chosen / "wiki"
        print_ok(f"Wiki 目录: {wiki_dir}")
        return True, wiki_dir
    else:
        print_warn("未检测到 Obsidian Vault")
        default_wiki = Path.home() / "Documents" / "Obsidian Vault" / "wiki"
        if yes_mode:
            print(f"  使用默认路径: {default_wiki}")
            return True, default_wiki
        custom = ask(f"Wiki 目录路径 (回车={default_wiki}):", default=str(default_wiki), yes_mode=yes_mode)
        return True, Path(custom)


# ── 步骤 5: 生成配置 ──

def _runtime_config_path() -> Path:
    """运行时权威配置路径，与 core.config.Config 保持一致。"""
    mnemos_dir = Path(os.environ.get("MNEMOS_DIR", Path.home() / ".mnemos")).expanduser()
    return mnemos_dir / "configs" / "main.json"


def _deep_merge(base: dict, override: dict) -> None:
    for key, val in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(val, dict):
            _deep_merge(base[key], val)
        else:
            base[key] = val


def generate_config(wiki_dir: Path, memos_url: Optional[str], yes_mode: bool = False) -> Path:
    config_file = _runtime_config_path()
    config_file.parent.mkdir(parents=True, exist_ok=True)

    if config_file.exists() and not yes_mode:
        if not ask_yes_no(f"配置已存在: {config_file}，是否覆盖？", default=False, yes_mode=yes_mode):
            print_warn("保留现有配置")
            return config_file

    from core.config import DEFAULT_CONFIG

    data = copy.deepcopy(DEFAULT_CONFIG)
    if config_file.exists():
        try:
            existing = json.loads(config_file.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                _deep_merge(data, existing)
        except Exception as e:
            print_warn(f"现有 JSON 配置读取失败，将重新生成: {e}")

    data["mnemos_dir"] = str(_runtime_config_path().parent.parent)
    data.setdefault("wiki", {})["vault_path"] = str(wiki_dir)
    data.setdefault("memos", {})
    if memos_url:
        data["memos"]["enabled"] = True
        data["memos"]["api_url"] = memos_url
    elif not config_file.exists():
        data["memos"]["enabled"] = False
        data["memos"]["api_url"] = ""

    # 安装期安全默认值：允许消费 MCP/Hook 入队，但不主动扫描历史聊天文件。
    services = data.setdefault("daemon", {}).setdefault("services", {})
    services["capture_worker"] = True
    services["l1_sync"] = False
    services["event_bus"] = True

    config_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print_ok(f"配置已写入: {config_file}")
    return config_file


# ── 步骤 6: 初始化 Wiki 目录 ──

def init_wiki_structure(wiki_dir: Path) -> None:
    print(f"  初始化 Wiki 目录结构: {wiki_dir}")

    dirs = [
        "00-Inbox",
        "01-People",
        "02-Projects",
        "03-Tech",
        "04-Concepts",
        "05-MOCs",
        "06-Retrospectives",
        "07-Shadow",
        "99-Reports",
        "retrospectives",
    ]
    for d in dirs:
        (wiki_dir / d).mkdir(parents=True, exist_ok=True)
    # 创建 index.md
    index = wiki_dir / "index.md"
    if not index.exists():
        index.write_text("# Mnemos 知识库\n\n自动生成的知识库入口。\n", encoding="utf-8")
    print_ok(f"Wiki 目录结构已就绪 ({len(dirs)} 个子目录)")


# ── 步骤 7: 安装 Agent Hooks ──

def install_agent_hooks(yes_mode: bool = False) -> bool:
    print("  安装 AI Agent Hooks...")
    try:
        from integrations.olympus import AgentRegistry
        agents = AgentRegistry.discover_all()
        if not agents:
            print_warn("未检测到任何 Agent，跳过 hooks 安装")
            return True
        for agent in agents:
            print(f"    安装 {agent.name} ...", end=" ")
            try:
                ok = agent.install_hooks()
                print("✓" if ok else "✗")
            except Exception as e:
                print(f"✗ ({e})")
        print_ok(f"Agent hooks 安装完成 ({len(agents)} 个 Agent)")
        return True
    except Exception as e:
        print_err(f"Agent hooks 安装失败: {e}")
        return False


# ── 步骤 7.5: 配置自动蒸馏 ──

def _detect_installed_agents() -> Dict[str, bool]:
    """检测本地安装的 AI Agent"""
    agents = {}
    # Kimi
    agents["kimi"] = shutil.which("kimi") is not None
    # Claude
    agents["claude"] = shutil.which("claude") is not None or (Path.home() / ".claude").exists()
    # Cursor
    agents["cursor"] = (Path.home() / ".cursor").exists() or shutil.which("cursor") is not None
    # Windsurf
    agents["windsurf"] = (Path.home() / ".windsurf").exists() or shutil.which("windsurf") is not None
    return agents


def _detect_api_configs() -> Dict[str, bool]:
    """检测已配置的 API key"""
    apis = {}
    apis["siliconflow"] = bool(os.getenv("SILICONFLOW_API_KEY"))
    apis["openai"] = bool(os.getenv("OPENAI_API_KEY"))
    apis["anthropic"] = bool(os.getenv("ANTHROPIC_API_KEY"))
    apis["deepseek"] = bool(os.getenv("DEEPSEEK_API_KEY"))
    # 也检查运行时配置
    try:
        from core.config import get_config
        cfg = get_config()
        providers = cfg.get("llm.providers", {})
        for name in ["siliconflow", "openai", "anthropic", "deepseek"]:
            if name in providers and providers[name].get("api_key"):
                apis[name] = True
    except Exception:
        pass
    return apis


def setup_distillation(yes_mode: bool = False) -> str:
    """交互式配置自动蒸馏方案

    Returns:
        用户选择的方案标识: "api" | "kimi" | "claude" | "generic"
    """
    print("  检测自动蒸馏环境...")
    agents = _detect_installed_agents()
    apis = _detect_api_configs()

    # 显示检测结果
    print("  检测到以下 AI Agent:")
    for name, available in agents.items():
        icon = "✓" if available else "✗"
        print(f"    [{icon}] {name.title()}")

    print("  检测到 API 配置:")
    has_any_api = False
    for name, available in apis.items():
        icon = "✓" if available else "✗"
        print(f"    [{icon}] {name.title()} API")
        if available:
            has_any_api = True

    # 全自动模式：自动选择最佳方案
    if yes_mode:
        if has_any_api:
            print("  自动选择: API 蒸馏模式")
            _write_distill_config("api")
            return "api"
        elif agents.get("kimi"):
            print("  自动选择: Kimi CLI 蒸馏模式")
            _install_kimi_hook()
            _write_distill_config("kimi")
            return "kimi"
        elif agents.get("claude"):
            print("  自动选择: Claude 占位符模式")
            _write_distill_config("claude")
            return "claude"
        else:
            print("  自动选择: 手动模式")
            _write_distill_config("generic")
            return "generic"

    # 交互式选择
    print()
    print("  请选择自动蒸馏方案:")
    print("    A) 配置 API key（推荐，全自动，体验最佳）")
    print("    B) 利用 Kimi CLI（利用 Coding Plan 额度，有 rate limit）")
    print("    C) 利用 Claude Code（占位符模式，下次开 Claude 时提醒）")
    print("    D) 手动模式（需要时运行 `mnemos distill`）")
    print()

    choice = ask("你的选择 (A/B/C/D):", default="", yes_mode=yes_mode).strip().upper()

    if choice == "A":
        print("  API 配置方式:")
        print("    1. 环境变量: export SILICONFLOW_API_KEY=your_key")
        print("    2. 配置文件: mnemos config set llm.providers.siliconflow.api_key your_key")
        print()
        print("  配置完成后，运行 `mnemos doctor` 验证。")
        _write_distill_config("api")
        return "api"

    elif choice == "B":
        if not agents.get("kimi"):
            print_warn("未检测到 Kimi CLI，将回退到手动模式")
            _write_distill_config("generic")
            return "generic"
        _install_kimi_hook()
        _write_distill_config("kimi")
        print_ok("Kimi SessionEnd hook 已安装")
        print("  每次 Kimi 会话结束时，将自动触发蒸馏任务")
        return "kimi"

    elif choice == "C":
        if not agents.get("claude"):
            print_warn("未检测到 Claude Code，将回退到手动模式")
            _write_distill_config("generic")
            return "generic"
        _write_distill_config("claude")
        print_ok("Claude 占位符模式已配置")
        print("  蒸馏任务将生成占位符，下次开 Claude 时自动提醒执行")
        return "claude"

    else:  # D 或其他
        _write_distill_config("generic")
        print_ok("手动模式已配置")
        print("  运行 `mnemos distill --session <id>` 手动触发蒸馏")
        return "generic"


def _write_distill_config(strategy: str):
    """将蒸馏策略写入运行时配置"""
    try:
        config_file = _runtime_config_path()
        if config_file.exists():
            data = json.loads(config_file.read_text(encoding="utf-8"))
        else:
            data = {}
        data.setdefault("distill", {})
        data["distill"]["strategy"] = strategy
        data["distill"]["auto_enabled"] = strategy != "generic"
        config_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception as e:
        print_warn(f"写入蒸馏配置失败: {e}")


def _install_kimi_hook():
    """安装 Kimi SessionEnd hook"""
    try:
        config_path = Path.home() / ".kimi" / "config.toml"
        if not config_path.exists():
            print_warn("Kimi 配置文件不存在，跳过 hook 安装")
            return

        # 检查是否已有 mnemos hook（文本检查，兼容 Python 3.10）
        config_text = config_path.read_text(encoding="utf-8")
        has_mnemos = "mnemos" in config_text and "SessionEnd" in config_text
        if has_mnemos:
            print_warn("Kimi mnemos hook 已存在，跳过")
            return

        # 追加 hook
        script_path = Path.home() / ".mnemos" / "hooks" / "kimi_session_end.py"
        script_path.parent.mkdir(parents=True, exist_ok=True)
        _write_kimi_hook_script(script_path)

        hook_text = f'\n[[hooks]]\ncommand = "python3 {script_path}"\nevent = "SessionEnd"\n'
        with open(config_path, "a", encoding="utf-8") as f:
            f.write(hook_text)
        print_ok(f"Kimi hook 已写入: {config_path}")
    except Exception as e:
        print_warn(f"安装 Kimi hook 失败: {e}")


def _write_kimi_hook_script(path: Path):
    """写入 Kimi SessionEnd hook 脚本"""
    script = '''#!/usr/bin/env python3
"""Kimi SessionEnd hook — 会话结束时触发 Mnemos 蒸馏"""
import os
import sys
from pathlib import Path

# 确保能找到 Mnemos 模块
mnemos_dir = Path.home() / "mnemos"
if str(mnemos_dir) not in sys.path:
    sys.path.insert(0, str(mnemos_dir))

try:
    from core.kia import amphora
    # 触发收集完成的蒸馏结果
    from core.hephaestus_worker import HephaestusWorker
    worker = HephaestusWorker()
    worker.collect_completed()
except Exception:
    pass
'''
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


# ── 步骤 8: 启动 Daemon ──

def start_daemon(yes_mode: bool = False) -> bool:
    print("  启动 Mnemos 守护进程...")
    # 先检查是否已在运行
    pid_file = Path.home() / ".mnemos" / "daemon.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            if pid > 0:
                os.kill(pid, 0)  # 检查进程是否存在
                print_ok(f"Daemon 已在运行 (PID: {pid})")
                return True
        except (OSError, ValueError):
            pass

    if not ask_yes_no("是否启动守护进程？", default=True, yes_mode=yes_mode):
        print_warn("跳过 daemon 启动")
        return True

    result = subprocess.run(
        [_PYTHON_EXE, str(PROJECT_ROOT / "mnemos_daemon.py"), "start"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print_ok("Daemon 已启动")
        return True
    print_err(f"Daemon 启动失败: {result.stderr[:200]}")
    return False


# ── 步骤 9: 配置定时任务 ──

def setup_scheduler(yes_mode: bool = False) -> bool:
    print("  配置系统定时任务...")
    system = platform.system()

    if system == "Darwin":
        return _setup_macos_scheduler(yes_mode)
    elif system == "Linux":
        return _setup_linux_scheduler(yes_mode)
    elif system == "Windows":
        print_warn("Windows 请手动运行: python mnemos_cli.py scheduler install-windows")
        return True
    else:
        print_warn(f"未知平台 {system}，跳过定时任务配置")
        return True


def _setup_macos_scheduler(yes_mode: bool = False) -> bool:
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.mnemos.daemon.plist"
    if plist_path.exists() and not yes_mode:
        if not ask_yes_no(f"launchd 配置已存在: {plist_path}，是否覆盖？", default=False, yes_mode=yes_mode):
            print_warn("保留现有 launchd 配置")
            return True

    def _xml_esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    exe = _xml_esc(str(_PYTHON_EXE))
    script = _xml_esc(str(PROJECT_ROOT / "mnemos_daemon.py"))
    log_out = _xml_esc(str(Path.home() / ".mnemos" / "logs" / "daemon.launchd.log"))
    log_err = _xml_esc(str(Path.home() / ".mnemos" / "logs" / "daemon.launchd.err"))

    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.mnemos.daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>{exe}</string>
        <string>{script}</string>
        <string>run</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_out}</string>
    <key>StandardErrorPath</key>
    <string>{log_err}</string>
</dict>
</plist>
"""
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    (Path.home() / ".mnemos" / "logs").mkdir(parents=True, exist_ok=True)
    plist_path.write_text(plist_content, encoding="utf-8")

    # 加载: 先 unload 再 load，避免重复加载报错
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    result = subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True)
    if result.returncode == 0:
        print_ok(f"macOS launchd 已配置: {plist_path}")
        return True
    # launchctl 在较新 macOS 上可能需要 domain/user 参数
    result = subprocess.run(
        ["launchctl", "load", "-w", str(plist_path)],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print_ok(f"macOS launchd 已配置: {plist_path}")
        return True
    print_warn(f"launchctl load 返回非零: {result.stderr[:200]}")
    return False


def _setup_linux_scheduler(yes_mode: bool = False) -> bool:
    log_file = Path.home() / ".mnemos" / "logs" / "daemon.cron.log"
    cron_line = f"*/5 * * * * {_PYTHON_EXE} {PROJECT_ROOT / 'mnemos_daemon.py'} run >> {log_file} 2>&1"
    print_warn("Linux 定时任务请手动配置 cron:")
    print(f"    echo '{cron_line}' | crontab -")
    return True


# ── 主流程 ──

def main():
    parser = argparse.ArgumentParser(description="Mnemos 自动部署脚本")
    parser.add_argument("--yes", "-y", action="store_true", help="全自动模式，不提示")
    parser.add_argument("--skip-memos", action="store_true", help="跳过 Memos 配置")
    parser.add_argument("--skip-obsidian", action="store_true", help="跳过 Obsidian 配置")
    parser.add_argument("--skip-daemon", action="store_true", help="跳过启动守护进程")
    parser.add_argument("--skip-scheduler", action="store_true", help="跳过配置系统定时任务")
    parser.add_argument("--skip-hooks", action="store_true", help="跳过安装 Agent Hooks")
    parser.add_argument("--dry-run", action="store_true", help="只检查环境，不执行任何安装/启动操作")
    args = parser.parse_args()

    # 非交互式终端自动启用 --yes，避免 input() 抛 EOFError
    if not sys.stdin.isatty():
        args.yes = True
        print("检测到非交互式终端，自动启用 --yes 模式")

    if args.dry_run:
        print("=" * 60)
        print("Mnemos 部署检查 (dry-run)")
        print("=" * 60)
        print(f"项目路径: {PROJECT_ROOT}")
        print(f"平台: {platform.system()} {platform.release()}")
        print(f"Python: {sys.version.split()[0]}")
        print()
        check_python()
        print()
        print("dry-run 完成，未执行任何安装或启动操作。")
        print("如需实际部署，请去掉 --dry-run 参数。")
        sys.exit(0)

    yes_mode = args.yes
    total_steps = 10

    print("=" * 60)
    print("Mnemos 自动部署")
    print("=" * 60)
    print(f"项目路径: {PROJECT_ROOT}")
    print(f"平台: {platform.system()} {platform.release()}")
    print(f"Python: {sys.version.split()[0]}")
    if yes_mode:
        print("模式: 全自动 (--yes)")

    # 步骤 1
    print_step(1, total_steps, "检查 Python 版本")
    if not check_python():
        sys.exit(1)

    # 步骤 2
    print_step(2, total_steps, "安装依赖")
    if not install_dependencies(yes_mode=yes_mode):
        if not ask_yes_no("依赖安装失败，是否继续？", default=False, yes_mode=yes_mode):
            sys.exit(1)

    # 步骤 3
    print_step(3, total_steps, "检测 Memos")
    memos_ok, memos_url = setup_memos(skip=args.skip_memos, yes_mode=yes_mode)

    # 步骤 4
    print_step(4, total_steps, "检测 Obsidian")
    obsidian_ok, wiki_dir = setup_obsidian(skip=args.skip_obsidian, yes_mode=yes_mode)
    if not wiki_dir:
        wiki_dir = Path.home() / "Documents" / "Obsidian Vault" / "wiki"

    # 步骤 5
    print_step(5, total_steps, "生成配置")
    generate_config(wiki_dir, memos_url, yes_mode=yes_mode)

    # 步骤 6
    print_step(6, total_steps, "初始化 Wiki 目录")
    init_wiki_structure(wiki_dir)

    # 步骤 7
    if not args.skip_hooks:
        print_step(7, total_steps, "安装 Agent Hooks")
        install_agent_hooks(yes_mode=yes_mode)
    else:
        print_step(7, total_steps, "安装 Agent Hooks")
        print_warn("已跳过 (--skip-hooks)")

    # 步骤 8: 配置自动蒸馏
    print_step(8, total_steps, "配置自动蒸馏")
    distill_strategy = setup_distillation(yes_mode=yes_mode)

    # 步骤 9
    if not args.skip_daemon:
        print_step(9, total_steps, "启动守护进程")
        start_daemon(yes_mode=yes_mode)
    else:
        print_step(9, total_steps, "启动守护进程")
        print_warn("已跳过 (--skip-daemon)")

    # 步骤 10
    if not args.skip_scheduler:
        print_step(10, total_steps, "配置定时任务")
        setup_scheduler(yes_mode=yes_mode)
    else:
        print_step(10, total_steps, "配置定时任务")
        print_warn("已跳过 (--skip-scheduler)")

    # 完成
    print("\n" + "=" * 60)
    print("部署完成！")
    print("=" * 60)
    print(f"  配置: {_runtime_config_path()}")
    print(f"  Wiki: {wiki_dir}")
    if memos_url:
        print(f"  Memos: {memos_url}")
    print(f"  蒸馏策略: {distill_strategy}")
    print()
    print("后续操作:")
    print("  python3 mnemos_cli.py doctor    # 系统诊断")
    print("  python3 mnemos_cli.py status    # 查看状态")
    print("  python3 mnemos_cli.py init      # 交互式调整配置")
    if distill_strategy == "generic":
        print("  python3 mnemos_cli.py distill   # 手动触发蒸馏")
    print()
    print("Claude Code / Kimi 重启后 hooks 生效。")
    print("=" * 60)


if __name__ == "__main__":
    main()
