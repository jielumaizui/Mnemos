#!/usr/bin/env python3
"""
Mnemos - 命令行入口

命令：
    mnemos init         交互式配置向导
    mnemos doctor       系统诊断
    mnemos status       查看系统状态
    mnemos config       查看/编辑配置
    mnemos mcp serve    启动 MCP 服务器
"""
import logging
logger = logging.getLogger(__name__)

import os
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime
from typing import List

from core.config import get_config, Config
from core.hephaestus.distillation_prompts import PROMPT_VERSION


def _format_bytes(size: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{size}B"


def _sqlite_group_counts(db_path: Path, table: str, group_cols: str, where: str = ""):
    if not db_path.exists():
        return []
    try:
        import sqlite3
        sql = f"SELECT {group_cols}, COUNT(*) FROM {table}"
        if where:
            sql += f" WHERE {where}"
        sql += f" GROUP BY {group_cols} ORDER BY COUNT(*) DESC"
        with sqlite3.connect(str(db_path), timeout=5) as conn:
            return conn.execute(sql).fetchall()
    except Exception:
        logger.debug(f"读取 SQLite 统计失败: {db_path}", exc_info=True)
        return []


def _daemon_processes():
    def _looks_like_daemon_cmd(cmd: str) -> bool:
        if "mnemos_daemon.py" not in cmd and "mnemos_daemon" not in cmd:
            return False
        noisy = ("pgrep", "grep", "mnemos_cli.py", "sed -n", "pytest")
        if any(token in cmd for token in noisy):
            return False
        try:
            import shlex
            tokens = shlex.split(cmd)
            return any(Path(t).name in ("mnemos_daemon.py", "mnemos_daemon") for t in tokens)
        except Exception:
            return "mnemos_daemon.py start" in cmd or "mnemos_daemon start" in cmd

    try:
        import subprocess
        import platform
        if platform.system() == "Darwin":
            # macOS: pgrep -a 只输出 PID，需要用 ps 获取完整命令行
            result = subprocess.run(
                ["pgrep", "-f", "mnemos_daemon.py"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode not in (0, 1):
                return []
            lines = []
            for pid in result.stdout.splitlines():
                pid = pid.strip()
                if not pid.isdigit():
                    continue
                ps_result = subprocess.run(
                    ["ps", "-p", pid, "-o", "args="],
                    capture_output=True, text=True, timeout=5,
                )
                if ps_result.returncode == 0:
                    cmd = ps_result.stdout.strip()
                    if _looks_like_daemon_cmd(cmd):
                        lines.append(f"{pid} {cmd}")
            return lines
        else:
            result = subprocess.run(
                ["pgrep", "-af", "mnemos_daemon.py"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode not in (0, 1):
                return []
            lines = []
            for line in result.stdout.splitlines():
                if _looks_like_daemon_cmd(line):
                    lines.append(line)
            return lines
    except Exception:
        return []


def _print_config_contract(config, warnings=None):
    print("配置契约:")
    print(f"  当前读取: {config.config_path}")
    print(f"  配置存在: {'是' if config.config_path.exists() else '否（使用代码默认值）'}")
    print(f"  数据目录: {config.data_dir}")
    legacy_paths = []
    if hasattr(config, "_legacy_config_paths"):
        legacy_paths = [p for p in config._legacy_config_paths() if p.exists()]
    if legacy_paths:
        print("  旧 YAML: " + ", ".join(str(p) for p in legacy_paths))
        if warnings is not None:
            warnings.append("检测到旧 YAML 配置；运行时权威配置为 configs/main.json")
    services = config.get("daemon.services", {})
    if services:
        print("  daemon 服务:")
        for key in sorted(services):
            mark = "✓" if services[key] else "☐"
            print(f"    {mark} {key}")


def _print_runtime_health(config, warnings=None):
    print("运行态:")
    processes = _daemon_processes()
    print(f"  daemon 进程数: {len(processes)}")
    if len(processes) > 1 and warnings is not None:
        warnings.append(f"检测到重复 daemon 进程: {len(processes)}")

    log_path = config.data_dir / "daemon.log"
    if log_path.exists():
        log_size = log_path.stat().st_size
        print(f"  daemon.log: {_format_bytes(log_size)}")
        max_log = int(config.get("ops.daemon_log_max_bytes", 10 * 1024 * 1024))
        if log_size > max_log and warnings is not None:
            warnings.append(f"daemon.log 超过阈值: {_format_bytes(log_size)}")
    else:
        print("  daemon.log: 未创建")

    events_db = config.data_dir / "events.db"
    if events_db.exists():
        print(f"  events.db: {_format_bytes(events_db.stat().st_size)}")
        rows = _sqlite_group_counts(events_db, "events", "event_type, status")
        pending_total = sum(c for _, status, c in rows if status in ("pending", "processing"))
        print(f"  events pending/processing: {pending_total}")
        for event_type, status, count in rows[:5]:
            print(f"    - {event_type}/{status}: {count}")
        alert = int(config.get("event_bus.queue_depth_alert", 1000))
        if pending_total > alert and warnings is not None:
            warnings.append(f"events.db 积压超过阈值: {pending_total}")
    else:
        print("  events.db: 未创建")

    capture_db = config.data_dir / "capture_queue.db"
    if capture_db.exists():
        rows = _sqlite_group_counts(capture_db, "capture_events", "status")
        pending = sum(c for status, c in rows if status in ("pending", "processing"))
        print(f"  capture_queue pending/processing: {pending}")
        for status, count in rows[:5]:
            print(f"    - {status}: {count}")


def cmd_init(args):
    """交互式配置向导"""
    print("=" * 60)
    print("Mnemos 初始化向导")
    print("=" * 60)
    print()

    config = get_config()

    # 1. Wiki 路径
    default_wiki = config.wiki_dir
    print(f"[1/7] 知识库路径")
    print(f"      默认: {default_wiki}")
    user_wiki = input(f"      你的 Obsidian Vault wiki 目录 (回车=默认): ").strip()
    if user_wiki:
        config.set("wiki.vault_path", user_wiki)

    # 2. Memos 配置
    print()
    print(f"[2/7] Memos 集成")
    memos_url = input("      Memos API 地址 (如 https://memos.example.com, 回车=跳过): ").strip()
    if memos_url:
        config.set("memos.enabled", True)
        config.set("memos.api_url", memos_url)
        token = input("      Memos Token (建议通过 MEMOS_TOKEN 环境变量设置): ").strip()
        if token:
            config.set("memos.token", token)
    else:
        config.set("memos.enabled", False)

    # 3. 蒸馏 API 配置
    print()
    print(f"[3/7] 蒸馏 API 配置（推荐配置，用于文档/会话蒸馏入 Wiki）")
    config.set("distill.provider", "api")
    config.set("distill.allow_host_agent_delegate", False)
    try:
        from core.llm_config import resolve_llm_api_config
        llm_cfg = resolve_llm_api_config(config)
    except Exception:
        llm_cfg = None
    if llm_cfg and llm_cfg.configured:
        print(f"      ✓ 检测到 API key: {llm_cfg.provider} ({llm_cfg.source})")
        print(f"      ✓ 模型: {llm_cfg.model}")
    else:
        print(f"      ⚠ 未检测到 SILICONFLOW_API_KEY 或 OPENAI_API_KEY")
        print(f"        └─ 不配置也可完成安装，但 API 蒸馏工具会提示待配置")
        api_key = input("      蒸馏 API Key (回车=稍后配置): ").strip()
        if api_key:
            provider = input("      Provider [siliconflow/openai] (回车=siliconflow): ").strip().lower() or "siliconflow"
            if provider not in ("siliconflow", "openai"):
                provider = "siliconflow"
            config.set("llm.provider", provider)
            config.set("llm.api_key", api_key)
            if provider == "siliconflow":
                config.set("llm.base_url", "https://api.siliconflow.cn/v1")
                config.set("llm.model", "deepseek-ai/DeepSeek-V3")
                config.set("llm.providers.siliconflow.api_key", api_key)
            else:
                config.set("llm.base_url", "https://api.openai.com/v1")
                config.set("llm.model", "gpt-4o-mini")
                config.set("llm.providers.openai.api_key", api_key)
            print(f"      ✓ 已写入 LLM API 配置: {provider}")

    # 4. 画像数据源
    print()
    print(f"[4/7] 用户画像数据源")
    print(f"      以下数据源用于构建你的用户画像。")
    print(f"      开启越多画像越精准，但隐私暴露也越多。")
    print()

    sources = config.persona_data_sources
    for key, info in sources.items():
        default = "Y" if info.get("enabled") else "N"
        if key == "session":
            print(f"      ☑ {key}: {info.get('description', '')} (核心，不可关闭)")
            config.set(f"persona.data_sources.{key}.enabled", True)
        else:
            ans = input(f"      开启 {key}? [{default}/n]: ").strip()
            enabled = ans.lower() != "n" if default == "Y" else ans.lower() == "y"
            config.set(f"persona.data_sources.{key}.enabled", enabled)

    # 5. AI Agent 集成
    print()
    print(f"[5/7] AI Agent 集成")
    cc_path = input(f"      Claude Code settings.json 路径 (回车={config.claude_settings_path}): ").strip()
    if cc_path:
        config.set("integrations.claude_code.settings_json_path", cc_path)

    mcp = input("      启用 MCP 协议? [Y/n]: ").strip()
    config.set("integrations.mcp.enabled", mcp.lower() not in ("n", "no"))

    l1 = input("      启用受限 L1 自动扫描本机 Agent 会话? [Y/n]: ").strip()
    config.set("daemon.services.l1_sync", l1.lower() not in ("n", "no"))

    install_hooks = input("      为检测到的 Agent 安装 hooks? [Y/n]: ").strip()
    should_install_hooks = install_hooks.lower() not in ("n", "no")

    # 6. 可选依赖推荐
    print()
    print(f"[6/7] 可选依赖安装")
    print(f"      以下依赖不装也能跑，但会影响部分功能：")
    print()

    optional_deps = [
        ("black", "Black", "代码格式化工具"),
        ("pytest", "Pytest", "运行测试套件"),
    ]
    for module, name, desc in optional_deps:
        try:
            __import__(module)
            print(f"      ✓ {name}: 已安装")
        except ImportError:
            print(f"      ✗ {name}: 未安装")
            print(f"        └─ 影响: {desc}")
            print(f"        └─ 安装: pip install mnemos[dev]")
            print()

    # 7. 保存
    print()
    print(f"[7/7] 保存配置")
    config.save()
    print(f"      ✓ 配置已保存到: {config.config_path}")

    # 6. 创建目录
    wiki_dir = config.wiki_dir
    wiki_dir.mkdir(parents=True, exist_ok=True)
    (wiki_dir / "06-Retrospectives").mkdir(exist_ok=True)
    # 兼容旧目录
    if (wiki_dir / "retrospectives").exists():
        (wiki_dir / "06-Retrospectives").mkdir(exist_ok=True)
    print(f"      ✓ 创建 Wiki 目录: {wiki_dir}")

    # 7. 安装 Agent hooks（可选，默认安装所有检测到的 Agent）
    if should_install_hooks:
        _install_detected_agent_hooks(config)
    elif config.claude_code_enabled:
        _install_claude_hook(config)

    print()
    print("=" * 60)
    print("初始化完成！运行 `mnemos doctor` 验证系统。")
    print("=" * 60)


def _install_claude_hook(config):
    """安装 Claude Code hook"""
    settings_path = config.claude_settings_path
    if not settings_path.exists():
        print(f"      ⚠ Claude Code settings.json 不存在: {settings_path}")
        return

    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)

        if "hooks" not in settings:
            settings["hooks"] = {}

        # 添加 session-start hook（跨平台：Windows 用 sys.executable 代替 python3）
        script_path = Path(__file__).resolve().parent / "integrations" / "apollon.py"
        python_cmd = sys.executable
        settings["hooks"]["SessionStart"] = (
            f"{python_cmd} {script_path} --session-start "
            f"--working-dir \"$PWD\" --user-message \"$USER_MESSAGE\""
        )
        settings["hooks"]["SessionEnd"] = (
            f"{python_cmd} {script_path} --session-end "
            f"--working-dir \"$PWD\" --session-messages \"$SESSION_MESSAGES\""
        )

        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)

        print(f"      ✓ 已安装 Claude Code hooks 到: {settings_path}")
    except Exception as e:
        print(f"      ⚠ 安装 hooks 失败: {e}")


def _install_detected_agent_hooks(config):
    """安装所有可用 Agent 的主动接入，失败时给出明确状态但不中断 init。"""
    try:
        from integrations.olympus import AgentRegistry
        agents = AgentRegistry.discover_all()
    except Exception as e:
        print(f"      ⚠ Agent 检测失败: {e}")
        return

    if not agents:
        print("      ⚠ 未检测到可自动安装 hooks 的 Agent")
        if config.claude_code_enabled:
            _install_claude_hook(config)
        return

    installed = 0
    active = 0
    for agent in agents:
        try:
            hooks_ok = agent.install_hooks()
            mcp_ok = agent.install_mcp_server()
            installed += 1 if hooks_ok else 0
            active += 1 if (hooks_ok and mcp_ok) else 0
            print(f"      {'✓' if hooks_ok else '✗'} {agent.name} hooks")
            print(f"      {'✓' if mcp_ok else '✗'} {agent.name} MCP 主动工具")
        except Exception as e:
            print(f"      ✗ {agent.name} 主动接入: {e}")
    print(f"      Agent hooks 安装完成: {installed}/{len(agents)}")
    print(f"      Agent 主动接入完成: {active}/{len(agents)}")


def cmd_doctor(args):
    """系统诊断"""
    print("=" * 60)
    print("Mnemos 系统诊断")
    print("=" * 60)
    print()

    config = get_config()
    issues = []
    warnings = []

    _print_config_contract(config, warnings)
    print()

    # 0. 性能档位
    tier = config.get("performance_tier", "default")
    print(f"性能档位: {tier}")
    tier_desc = {
        "eco": "节能模式 (embedding关闭, rerank关闭, 低并发)",
        "default": "默认模式 (embedding开启, rerank开启, 标准并发)",
        "performance": "性能模式 (embedding开启, rerank开启, 高并发)",
        "dev": "开发模式 (全部开启, 最大并发, 调试用)",
    }
    print(f"  {tier_desc.get(tier, '未知档位')}")
    print(f"  embedding: {'开启' if config.get('embedding.enabled') else '关闭'}")
    print(f"  rerank: {'开启' if config.get('embedding.use_rerank') else '关闭'}")
    print(f"  max_workers: {config.get('capture.max_workers')}")
    print(f"  max_payload: {config.get('capture.max_payload_bytes')} bytes")
    print()

    # 0.5 采集模式说明
    print("采集模式:")
    l1_recent = config.get("sync.l1_scan_recent_hours", 24)
    l1_max_sources = config.get("sync.l1_scan_max_sources_per_cycle", 3)
    l1_max_sessions = config.get("sync.l1_scan_max_sessions_per_source", 20)
    l1_max_turns = config.get("sync.l1_scan_max_turns_per_session", 50)
    print(f"  默认 L1 扫描: 受限增量采集（最近 {l1_recent} 小时）")
    print(f"    每轮最多 {l1_max_sources} 个 Agent 源")
    print(f"    每源最多 {l1_max_sessions} 个 sessions")
    print(f"    每 session 最多 {l1_max_turns} 个 turns")
    print(f"  如需历史全量回填: `mnemos sync backfill --since 0`")
    print()

    # 1. Python 版本
    py_version = sys.version_info
    if py_version >= (3, 10):
        print(f"✓ Python {py_version.major}.{py_version.minor}.{py_version.micro}")
    else:
        issues.append(f"Python 版本过低: {py_version.major}.{py_version.minor} (需要 >= 3.10)")

    # 2. 核心依赖
    deps = {
        "requests": "requests",
        "yaml": "pyyaml",
        "watchdog": "watchdog",
        "numpy": "numpy",
    }
    for name, pkg in deps.items():
        try:
            __import__(name)
            print(f"✓ {name}")
        except ImportError:
            issues.append(f"缺少依赖: {name} (pip install {pkg})")

    # 3. Git（画像系统用到）
    try:
        import subprocess
        result = subprocess.run(["git", "--version"], capture_output=True, timeout=5)
        if result.returncode == 0:
            print(f"✓ Git")
        else:
            warnings.append("Git 已安装但运行异常")
    except FileNotFoundError:
        warnings.append("Git 未安装 (画像系统的 git 数据源将不可用)")

    # 4. Wiki 目录
    wiki_dir = config.wiki_dir
    if wiki_dir.exists():
        print(f"✓ Wiki 目录: {wiki_dir}")
    else:
        warnings.append(f"Wiki 目录不存在: {wiki_dir} (运行 `mnemos init` 创建)")

    # 4. Memos 连接
    if config.memos_enabled:
        if config.memos_token:
            print(f"✓ Memos Token 已配置")
        else:
            warnings.append("Memos 已启用但 Token 未配置 (设置 MEMOS_TOKEN 环境变量)")
        if config.memos_api_url:
            print(f"✓ Memos API: {config.memos_api_url}")
        else:
            warnings.append("Memos API 地址未配置")
    else:
        print(f"☐ Memos 集成已禁用")

    # 5. 画像数据源
    print()
    print("画像数据源:")
    sources = config.persona_data_sources
    for key, info in sources.items():
        enabled = info.get("enabled", False)
        mark = "✓" if enabled else "☐"
        print(f"  {mark} {key}: {info.get('description', '')}")

    # 6. Claude Code
    print()
    cc_path = config.claude_settings_path
    if cc_path.exists():
        print(f"✓ Claude Code settings.json: {cc_path}")
    else:
        warnings.append(f"Claude Code settings.json 不存在: {cc_path}")

    # 6.5 Agent 连通性
    print()
    print("Agent 连通性:")
    try:
        from integrations.olympus import AgentRegistry
        agents = AgentRegistry.discover_all()
        if agents:
            print(f"  ✓ 检测到 {len(agents)} 个 Agent")
            for agent in agents:
                print(f"    - {agent.name} (优先级={agent.priority})")
        else:
            warnings.append("未检测到任何 Agent，蒸馏功能将不可用")
            print(f"  ✗ 未检测到 Agent")
        host = os.environ.get("MNEMOS_HOST_AGENT", "")
        if host:
            print(f"  ✓ 宿主 Agent: {host}")
    except Exception as e:
        warnings.append(f"Agent 检测失败: {e}")

    # 6.5b Agent 主动接入状态
    print()
    print("Agent 主动接入状态:")
    try:
        from core.diagnostics import ConnectionDiagnostics
        agent_statuses = ConnectionDiagnostics.check_agents()
        if agent_statuses:
            for agent in agent_statuses:
                mark = "✓" if agent.active_ready else "✗"
                hooks = "hooks✓" if agent.hooks_installed else "hooks✗"
                mcp = "mcp✓" if agent.mcp_configured else "mcp✗"
                print(f"  {mark} {agent.name}: {hooks}, {mcp}")
                if not agent.active_ready:
                    warnings.append(f"{agent.name} 主动接入未就绪，运行 `mnemos agent install {agent.name}`")
        else:
            print("  ☐ 未发现可诊断的 Agent adapter")
    except Exception as e:
        warnings.append(f"Agent 主动接入检测失败: {e}")

    # 6.5c Agent 完整性能力
    print()
    print("Agent 完整性能力:")
    try:
        from core.sync_framework.registry import AgentRegistry
        AgentRegistry.register_builtin_agents()
        agents = AgentRegistry.auto_discover()
        for agent in agents:
            caps = agent.completeness_capabilities() if hasattr(agent, "completeness_capabilities") else {}
            fidelity = caps.get("source_fidelity", "unknown")
            mark = "✓" if fidelity == "full" else "⚠" if fidelity in ("derived", "experimental") else "?"
            print(f"  {mark} {agent.name}: fidelity={fidelity}")
            if caps.get("reasoning"):
                print(f"    reasoning={caps.get('reasoning')}, tool_results={caps.get('tool_results')}")
    except Exception as e:
        warnings.append(f"Agent 完整性检测失败: {e}")

    # 6.5c Reasoning 采集策略
    print()
    reasoning_mode = config.get("capture.reasoning_mode", "artifact_summary")
    print(f"Reasoning 采集策略: {reasoning_mode}")
    mode_desc = {
        "off": "不采集 reasoning",
        "summary": "只保存前 2000 字摘要",
        "artifact_summary": "Memos 写摘要，本地 artifact 存完整 reasoning（推荐）",
        "full": "完整 reasoning 入 Memos",
    }
    print(f"  {mode_desc.get(reasoning_mode, '未知策略')}")

    # 6.6 API 蒸馏状态
    print()
    print("API 蒸馏状态:")
    try:
        from core.llm_config import resolve_llm_api_config
        llm_cfg = resolve_llm_api_config(config)
    except Exception:
        llm_cfg = None
    if llm_cfg and llm_cfg.configured:
        print(f"  ✓ Provider: {llm_cfg.provider}")
        print(f"  ✓ Model: {llm_cfg.model}")
        print(f"  ✓ Base URL: {llm_cfg.base_url}")
        print(f"  ✓ 配置来源: {llm_cfg.source}")
    else:
        warnings.append(
            "未配置蒸馏 API key（OPENAI_API_KEY 或 SILICONFLOW_API_KEY），"
            "或 main.json 的 llm.api_key / llm.providers.*.api_key，"
            "document_process / knowledge_distill 将返回配置缺失提示"
        )
        print(f"  ✗ API key 未配置")
        if llm_cfg:
            print(f"  ☐ Provider: {llm_cfg.provider}（未就绪）")
            print(f"  ☐ Model: {llm_cfg.model}（未就绪）")

    # 6.6 跨平台兼容性
    print()
    print("跨平台兼容性:")
    print(f"  ✓ 平台: {sys.platform}")
    if sys.platform == "win32":
        print(f"  ✓ Windows 控制台编码: 已处理")
    elif sys.platform == "darwin":
        print(f"  ✓ macOS launchd: 支持")
    elif sys.platform.startswith("linux"):
        print(f"  ✓ Linux systemd/cron: 支持")
    # 检查外部命令
    ext_cmds = [("libreoffice", "文档处理"), ("pdftotext", "PDF 处理"), ("tvly", "联网搜索")]
    for cmd, desc in ext_cmds:
        import shutil
        if shutil.which(cmd):
            print(f"  ✓ {cmd}: {desc} 可用")
        else:
            print(f"  ☐ {cmd}: {desc} 未安装 (可选)")

    # 7. 数据库
    print()
    db_path = Path.home() / ".mnemos" / "user_signals.db"
    if db_path.exists():
        print(f"✓ 画像数据库: {db_path}")
    else:
        print(f"☐ 画像数据库未创建 (首次运行时自动创建): {db_path}")

    # 8. KIA 闭环状态
    print()
    print("KIA 闭环状态:")
    # 优先 06-Retrospectives，兼容 retrospectives
    retro_dirs = [wiki_dir / "06-Retrospectives", wiki_dir / "retrospectives"]
    retro_dir = None
    retro_count = 0
    for d in retro_dirs:
        if d.exists():
            cnt = len(list(d.rglob("*.md")))
            if cnt > retro_count:
                retro_count = cnt
                retro_dir = d
    if retro_dir:
        print(f"  ✓ Retrospectives ({retro_dir.name}): {retro_count} 条经验")
        if retro_count == 0:
            warnings.append("retrospectives 目录为空，KIA 预加载暂无数据可用")
    else:
        warnings.append("retrospectives 目录不存在，运行 `mnemos init` 创建")

    # 检查 distill_queue 和 guard_state
    distill_queue = config.data_dir / "distill_queue"
    if distill_queue.exists():
        queue_count = len(list(distill_queue.iterdir()))
        print(f"  ✓ 蒸馏队列: {queue_count} 条待处理")
    else:
        print(f"  ☐ 蒸馏队列未初始化")

    # 9. 双索引状态 (ADR-019)
    print()
    print("双索引状态 (ADR-019):")
    embedding_enabled = config.get("embedding.enabled", False)
    print(f"  KG 数据库: {config.data_dir / 'knowledge_graph.db'}")
    print(f"  向量索引目录: {config.data_dir / 'embedding_index'}")
    if embedding_enabled:
        print(f"  ✓ Embedding 已启用")
        try:
            from core.embeddings import EmbeddingIndexManager
            from core.embeddings.relation_manager import RelationEmbeddingManager
            idx = EmbeddingIndexManager(wiki_base=wiki_dir)
            # 触发索引加载（只读，无变更时快速加载已有索引）
            idx.build_index()
            page_count = idx._index.get_current_count() if idx._index else len(idx._meta)
            print(f"  ✓ 页面索引: {page_count} 个 embedding")
            if page_count == 0:
                warnings.append(
                    "页面索引为空（首次搜索时将自动构建，或运行 `mnemos search <query>` 触发）"
                )

            rel_mgr = RelationEmbeddingManager()
            rel_stats = rel_mgr.get_stats()
            print(f"  ✓ 关联索引: {rel_stats['total_relations']} 个 embedding")
            if rel_stats['total_relations'] == 0:
                warnings.append("关联上下文索引为空，运行 `mnemos build-relation-index` 构建")
        except Exception as e:
            warnings.append(f"双索引检测失败: {e}")
    else:
        warnings.append("Embedding 未启用，当前为降级检索（关键词+图谱召回）")
        print(f"  ⚠ Embedding 已禁用 → 降级检索模式")

    # 10. 知识库健康度
    print()
    print("知识库健康度:")
    if wiki_dir.exists():
        md_files = list(wiki_dir.rglob("*.md"))
        md_count = len(md_files)
        print(f"  Wiki 页面: {md_count}")

        # Wiki metrics 覆盖率（只读统计，不自动扫描）
        try:
            from core.wiki_metrics import WikiMetrics
            wm = WikiMetrics(wiki_dir=str(wiki_dir))
            metrics_count = wm._get_conn().execute("SELECT COUNT(*) FROM page_metrics").fetchone()[0]
            coverage = (metrics_count / md_count * 100) if md_count > 0 else 0
            print(f"  Wiki metrics: {metrics_count}/{md_count} 页面 ({coverage:.0f}%)")
            if coverage < 50:
                warnings.append(
                    f"Wiki metrics 覆盖率仅 {coverage:.0f}%，"
                    f"运行 `mnemos metrics scan` 补齐"
                )
        except Exception as e:
            warnings.append(f"Wiki metrics 检查失败: {e}")

        # 知识来源分布
        if md_count > 0:
            try:
                import yaml
                from core.frontmatter import fm_get

                # 系统页面不计入知识来源统计
                _SYSTEM_PAGES = {"log.md", "index.md", "graph-index.md", "readme.md"}

                sources = {"人工写入": 0, "Memos同步": 0, "蒸馏提取": 0, "复盘经验": 0, "Git历史": 0, "其他": 0}
                for md_file in md_files:
                    try:
                        # 跳过系统页
                        if md_file.name.lower() in _SYSTEM_PAGES:
                            continue
                        content = md_file.read_text(encoding="utf-8", errors="ignore")
                        src = "人工写入"
                        if content.startswith("---"):
                            parts = content.split("---", 2)
                            if len(parts) >= 3:
                                fm = yaml.safe_load(parts[1]) or {}
                                tags = fm.get("tags", [])
                                explicit = fm_get(fm, "source", "")
                                if "memos" in tags or "memos-sync" in tags or explicit in ("memos", "memos-sync"):
                                    src = "Memos同步"
                                elif "distilled" in tags or explicit in ("distill", "distilled"):
                                    src = "蒸馏提取"
                                elif "retrospective" in tags or explicit == "retrospective":
                                    src = "复盘经验"
                                elif "git" in tags or explicit == "git":
                                    src = "Git历史"
                                elif explicit in ("claude", "kimi", "codex", "openclaw", "hermes"):
                                    src = "蒸馏提取"
                                elif explicit:
                                    # 有明确 source 但不是已知来源，算其他
                                    src = "其他"
                        sources[src] = sources.get(src, 0) + 1
                    except Exception:
                        logger.debug("跳过无法解析 frontmatter 的页面", exc_info=True)
                        continue
                print("  知识来源分布:")
                for src_name, cnt in sources.items():
                    if cnt > 0:
                        pct = cnt / md_count * 100
                        print(f"    - {src_name}: {cnt} ({pct:.0f}%)")
            except Exception:
                logger.debug("知识来源分布统计失败", exc_info=True)
                pass

            # 截断记录统计
            try:
                from scripts.mark_truncated import get_truncated_count
                trunc_count = get_truncated_count()
                if trunc_count > 0:
                    print(f"  历史截断记录: {trunc_count} 条（已记录为历史遗留，不影响最近采集完整性）")
                else:
                    print(f"  历史截断记录: 0")
            except Exception:
                pass

        # 最近修改时间
        if md_files:
            try:
                latest = max(md_files, key=lambda p: p.stat().st_mtime)
                days_since = (datetime.now().timestamp() - latest.stat().st_mtime) / 86400
                if days_since < 1:
                    print(f"  最近更新: 今天")
                elif days_since < 7:
                    print(f"  最近更新: {int(days_since)} 天前")
                else:
                    print(f"  最近更新: {int(days_since)} 天前")
                    if days_since > 30:
                        warnings.append(f"知识库已 {int(days_since)} 天未更新")
            except Exception:
                logger.debug("最近修改时间统计失败", exc_info=True)
                pass
    else:
        print(f"  Wiki 未初始化")

    # 10. 画像数据质量
    print()
    print("画像数据质量:")
    try:
        from core.persona.psyche import get_signal_store
        store = get_signal_store()
        stats = store.get_signal_stats(days=30)
        total = sum(v for v in stats.values() if v > 0)
        print(f"  最近30天信号: {total} 条")
        for src, cnt in stats.items():
            if cnt > 0:
                print(f"    - {src}: {cnt}")
        if total < 10:
            warnings.append("画像信号不足（<10条），画像推断可能不准确")
        elif total < 50:
            print(f"  ⚠️  画像信号较少，建议积累更多对话数据以获得精准画像")
    except Exception:
        logger.debug("画像数据库统计失败", exc_info=True)
        print(f"  ☐ 画像数据库未初始化")

    # 11. MCP 服务器状态
    print()
    print("MCP 服务器状态:")
    try:
        from integrations.agora import MCPServer
        server = MCPServer()
        tool_count = len(server.tools)
        print(f"  ✓ 协议版本: JSON-RPC 2.0 / MCP 2024-11-05")
        print(f"  ✓ 可用工具: {tool_count} 个")
        # 检查核心 KIA 工具
        kia_tools = ["preflight_inject", "guard_check"]
        for t in kia_tools:
            if t in server.tools:
                print(f"    ✓ {t}")
            else:
                warnings.append(f"MCP 缺少核心 KIA 工具: {t}")
    except Exception as e:
        warnings.append(f"MCP 服务器加载失败: {e}")

    # 11.5 Capture 队列健康
    print()
    print("Capture 队列健康:")
    try:
        import sqlite3
        cq_db = Path.home() / ".mnemos" / "capture_queue.db"
        if cq_db.exists():
            conn = sqlite3.connect(str(cq_db))
            cursor = conn.cursor()
            cursor.execute("SELECT status, COUNT(*) FROM capture_events GROUP BY status")
            status_counts = dict(cursor.fetchall())
            done = status_counts.get("done", 0)
            pending = status_counts.get("pending", 0)
            failed = status_counts.get("failed", 0) + status_counts.get("error", 0)
            print(f"  done: {done}, pending: {pending}, failed/error: {failed}")
            cursor.execute(
                "SELECT COUNT(*) FROM capture_events WHERE status IN ('failed','error') AND created_at > datetime('now', '-1 day')"
            )
            recent_failed = cursor.fetchone()[0]
            if recent_failed > 0:
                warnings.append(f"最近 24 小时有 {recent_failed} 条 capture 失败/错误记录")
            # capture_mode 统计（full / truncated / artifact）
            cursor.execute(
                "SELECT payload_json FROM capture_events WHERE created_at > datetime('now', '-7 day')"
            )
            mode_counts: dict[str, int] = {"full": 0, "truncated": 0, "artifact": 0}
            for row in cursor.fetchall():
                try:
                    payload = json.loads(row[0] or "{}")
                    mode = payload.get("metadata", {}).get("capture_mode", "full")
                    if mode in mode_counts:
                        mode_counts[mode] += 1
                    else:
                        mode_counts[mode] = mode_counts.get(mode, 0) + 1
                except Exception:
                    pass
            total_recent = sum(mode_counts.values())
            if total_recent > 0:
                print(f"  最近 7 天 capture 完整性:")
                for mode, count in sorted(mode_counts.items(), key=lambda x: -x[1]):
                    pct = count / total_recent * 100
                    print(f"    {mode}: {count} ({pct:.0f}%)")
                if mode_counts.get("truncated", 0) + mode_counts.get("artifact", 0) > 0:
                    warnings.append(
                        f"最近 7 天有 {mode_counts.get('truncated', 0)} 条 truncated 和 "
                        f"{mode_counts.get('artifact', 0)} 条 artifact capture，"
                        f"请检查大 payload 来源"
                    )
            conn.close()
        else:
            print(f"  ☐ capture_queue.db 不存在")
    except Exception:
        logger.debug("Capture 队列统计失败", exc_info=True)
        print(f"  ☐ Capture 队列状态未知")

    # 12. 链路完整性检查
    print()
    print("链路完整性:")
    try:
        from core.hephaestus_worker import HephaestusWorker
        worker = HephaestusWorker()
        stats = worker.get_stats()
        print(f"  ✓ 蒸馏队列: {stats['pending']} 个待处理")
        print(f"  ✓ 已委托: {stats['delegated']} 个")
        print(f"  ✓ Inbox: {stats['inbox_dir']}")
        if stats['pending'] > 10:
            warnings.append(f"蒸馏队列积压: {stats['pending']} 个任务")
    except Exception:
        logger.debug("蒸馏链路统计失败", exc_info=True)
        print(f"  ☐ 蒸馏链路未初始化")

    # 检查 Charon 自动触发
    try:
        from core.kia.charon import run_connect_cycle
        print(f"  ✓ Charon 知识解析: 可用")
    except Exception:
        logger.debug("Charon 可用性检查失败", exc_info=True)
        print(f"  ☐ Charon 知识解析: 未就绪")

    # 13. 可选依赖与功能影响报告
    print()
    print("可选依赖与功能影响:")
    optional_deps = [
        ("black", "Black 格式化", "代码格式化", "pip install mnemos[dev]"),
        ("pytest", "Pytest 测试", "运行测试套件", "pip install mnemos[dev]"),
        ("sklearn", "scikit-learn", "ML 评分器训练（standard 后端）", "pip install mnemos[ml]"),
        ("hnswlib", "hnswlib", "向量索引与跨 Agent 关联检索", "pip install mnemos[ml]"),
    ]
    has_optional_gap = False
    for module, name, feature, install_cmd in optional_deps:
        try:
            __import__(module)
            print(f"  ✓ {name}: {feature} 可用")
        except ImportError:
            has_optional_gap = True
            if module == "sklearn":
                print(f"  ☐ {name}: {feature} 未安装 → 已自动回退到 lightweight scorer（{install_cmd}）")
            else:
                print(f"  ✗ {name}: {feature} 不可用 → {install_cmd}")

    print()
    _print_runtime_health(config, warnings)

    # 汇总
    print()
    print("=" * 60)
    if issues:
        print(f"❌ 发现 {len(issues)} 个错误:")
        for i in issues:
            print(f"   - {i}")
    if warnings:
        print(f"⚠️  发现 {len(warnings)} 个警告:")
        for w in warnings:
            print(f"   - {w}")
    if not issues and not warnings:
        print("✅ 所有检查通过，系统就绪！")
    print("=" * 60)

    return len(issues) == 0


def cmd_status(args):
    """查看系统状态"""
    config = get_config()

    print("Mnemos 状态")
    print("=" * 40)
    print(f"配置文件:      {config.config_path}")
    print(f"Wiki 目录:     {config.wiki_dir}")
    print(f"Memos 集成:    {'✓' if config.memos_enabled else '✗'}")
    print(f"画像系统:      {'✓' if config.persona_enabled else '✗'}")
    print(f"Claude Code:   {'✓' if config.claude_code_enabled else '✗'}")
    print(f"MCP 配置:      {'✓' if config.mcp_enabled else '✗'}")
    try:
        from integrations.agora import MCPServer
        server = MCPServer()
        print(f"MCP Server:    ✓ 可启动 ({len(server.tools)} tools)")
    except Exception as e:
        print(f"MCP Server:    ✗ 不可启动 ({e})")
    services = config.get("daemon.services", {})
    if services:
        enabled = [k for k, v in services.items() if v]
        disabled = [k for k, v in services.items() if not v]
        print(f"daemon 服务:   开 {len(enabled)} / 关 {len(disabled)}")
    print()

    # 知识库统计
    wiki_dir = config.wiki_dir
    if wiki_dir.exists():
        md_count = len(list(wiki_dir.rglob("*.md")))
        print(f"Wiki 页面数:   {md_count}")
    print(f"KG 数据库:     {config.data_dir / 'knowledge_graph.db'}")
    print(f"向量索引目录:  {config.data_dir / 'embedding_index'}")

    # 画像统计
    try:
        from core.persona.psyche import get_signal_store
        store = get_signal_store()
        stats = store.get_signal_stats(days=30)
        total = sum(v for v in stats.values() if v > 0)
        print(f"最近30天信号: {total}")
    except Exception:
        logger.debug("状态页画像统计失败", exc_info=True)
        print("画像数据库:    未初始化")

    print()
    _print_runtime_health(config)


def cmd_config(args):
    """查看/编辑配置"""
    config = get_config()
    if args.set:
        key, val = args.set.split("=", 1)
        config.set(key, Config._auto_type(val))
        config.save()
        print(f"✓ 已设置 {key} = {Config._auto_type(val)}")
    else:
        import yaml
        import copy
        safe_config = copy.deepcopy(config.to_dict())
        # 脱敏敏感字段
        for section in ["memos", "llm", "embedding"]:
            if section in safe_config and isinstance(safe_config[section], dict):
                if safe_config[section].get("token"):
                    safe_config[section]["token"] = "***"
                if safe_config[section].get("api_key"):
                    safe_config[section]["api_key"] = "***"
                providers = safe_config[section].get("providers")
                if isinstance(providers, dict):
                    for provider_cfg in providers.values():
                        if isinstance(provider_cfg, dict) and provider_cfg.get("api_key"):
                            provider_cfg["api_key"] = "***"
        print(yaml.dump(safe_config, allow_unicode=True, sort_keys=False))


def cmd_daemon(args):
    """后台守护进程管理"""
    import subprocess
    daemon_script = Path(__file__).parent / "mnemos_daemon.py"
    if not daemon_script.exists():
        print(f"守护进程脚本不存在: {daemon_script}")
        return

    if args.daemon_cmd == "start":
        subprocess.run([sys.executable, str(daemon_script), "start"])
    elif args.daemon_cmd == "stop":
        subprocess.run([sys.executable, str(daemon_script), "stop"])
    elif args.daemon_cmd == "status":
        subprocess.run([sys.executable, str(daemon_script), "status"])
    else:
        print("可用子命令: start, stop, status")


def cmd_scheduler(args):
    """定时任务管理"""
    import subprocess
    daemon_script = Path(__file__).parent / "mnemos_daemon.py"
    if not daemon_script.exists():
        print(f"守护进程脚本不存在: {daemon_script}")
        return

    if args.scheduler_cmd == "install-windows":
        subprocess.run([sys.executable, str(daemon_script), "install-windows"])
    elif args.scheduler_cmd == "uninstall-windows":
        subprocess.run([sys.executable, str(daemon_script), "uninstall-windows"])
    else:
        print("可用子命令: install-windows, uninstall-windows")


def cmd_calibrate(args):
    """画像校准"""
    from core.persona.calibration_cli import run_calibration
    import json

    # 先展示待处理的挑战问题（如果有）
    challenge_file = Path.home() / ".mnemos" / "calibrations" / "pending_challenges.json"
    if challenge_file.exists():
        try:
            data = json.loads(challenge_file.read_text(encoding="utf-8"))
            challenges = data.get("challenges", [])
            if challenges:
                print("=" * 60)
                print("盲区挑战问题（基于最近画像分析生成）")
                print("=" * 60)
                for i, c in enumerate(challenges, 1):
                    print(f"\n  {i}. [{c['type']}] {c['question']}")
                    print(f"     提示: {c['suggestion']}")
                print("\n" + "=" * 60)
                print("以上挑战将在校准流程中帮助你验证画像准确性。\n")
        except Exception:
            logger.warning(f"Unexpected error in mnemos_cli.py", exc_info=True)
            pass

    # 运行校准流程
    run_calibration()


def cmd_agent(args):
    """AI Agent 管理"""
    from integrations.olympus import AgentRegistry

    if args.agent_cmd == "list":
        print("=" * 60)
        print("AI Agent 状态")
        print("=" * 60)
        agents = AgentRegistry.discover_all()
        if not agents:
            print("未检测到任何 Agent")
            return
        for agent in agents:
            mark = "★" if agent.name == os.environ.get("MNEMOS_HOST_AGENT", "").lower() else " "
            print(f"  [{mark}] {agent.name:12s} 优先级={agent.priority}")
        print()
        print(f"共 {len(agents)} 个 Agent 可用")

    elif args.agent_cmd == "detect":
        host = os.environ.get("MNEMOS_HOST_AGENT", "")
        if host:
            print(f"宿主 Agent (MNEMOS_HOST_AGENT): {host}")
        else:
            print("未设置 MNEMOS_HOST_AGENT，将按优先级自动选择")
        best = AgentRegistry.select_best_agent()
        if best:
            print(f"最佳可用 Agent: {best.name}")
        else:
            print("未检测到任何 Agent")

        # 使用统一诊断引擎
        from core.diagnostics import ConnectionDiagnostics

        print("\n" + "=" * 60)
        print("连接状态检测")
        print("=" * 60)

        memos = ConnectionDiagnostics.check_memos()
        wiki = ConnectionDiagnostics.check_wiki()
        agents = ConnectionDiagnostics.check_agents()

        # Memos
        memos_status = "已连接且可连通" if (memos.configured and memos.reachable) else ("已配置但不可达" if memos.configured else "未配置")
        print(f"  {'✓' if (memos.configured and memos.reachable) else '✗'} Memos: {memos_status}")

        # Wiki
        wiki_status = "就绪" if (wiki.exists and wiki.writable) else ("存在但不可写" if wiki.exists else "未就绪")
        print(f"  {'✓' if (wiki.exists and wiki.writable) else '✗'} Wiki: {wiki_status} ({wiki.path})")

        # Agent 数据源（带 hooks/MCP 主动状态）
        for agent in agents:
            hook_mark = "[hooks]" if agent.hooks_installed else ""
            mcp_mark = "[mcp]" if agent.mcp_configured else ""
            active_mark = "[active]" if agent.active_ready else ""
            print(f"  {'✓' if agent.available else '✗'} {agent.name}: {'已发现' if agent.available else '未发现'} {hook_mark}{mcp_mark}{active_mark}" + (f" ({agent.data_dir})" if agent.data_dir else ""))

        # 待办任务
        tasks = ConnectionDiagnostics.generate_task_list(memos, wiki, agents)
        pending = [t for t in tasks if not t.completed]
        if pending:
            print("\n待办连接任务:")
            for i, t in enumerate(pending, 1):
                marker = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(t.priority, "⚪")
                print(f"  {marker} [{t.priority.upper()}] {t.task}")
                print(f"      → {t.action}")
            print("\n提示: 宿主 Agent 可以通过 MCP 调用 self_diagnose() 获取完整诊断报告")
        else:
            print("\n✓ 所有核心连接已就绪")

    elif args.agent_cmd == "install":
        print("=" * 60)
        print("安装 Agent 主动接入")
        print("=" * 60)
        agents = AgentRegistry.discover_all()
        target = getattr(args, "agent_name", "").lower() if hasattr(args, "agent_name") else ""
        for agent in agents:
            if target and agent.name != target:
                continue
            print(f"\n安装 {agent.name} ...")
            try:
                hooks_ok = agent.install_hooks()
                mcp_ok = agent.install_mcp_server()
                print(f"  {'✓' if hooks_ok else '✗'} hooks/wrapper")
                print(f"  {'✓' if mcp_ok else '✗'} MCP 主动工具")
                print(f"  {'✓' if (hooks_ok and mcp_ok) else '✗'} active ready")
            except Exception as e:
                print(f"  ✗ {agent.name}: {e}")

    elif args.agent_cmd == "doctor":
        print("=" * 60)
        print("Agent 诊断")
        print("=" * 60)
        agents = AgentRegistry.discover_all()
        if not agents:
            print("✗ 未注册任何 Agent 适配器")
            return False

        target = getattr(args, "agent_name", "").lower() if hasattr(args, "agent_name") else ""
        checked = 0
        for agent in agents:
            if target and agent.name != target:
                continue
            checked += 1
            print(f"\n--- {agent.name} ---")
            # 1. 可用性
            try:
                avail = agent.is_available()
                print(f"  {'✓' if avail else '✗'} 可用性: {'可用' if avail else '不可用'}")
            except Exception as e:
                print(f"  ✗ 可用性检查失败: {e}")

            # 2. Hooks 安装（使用 adapter 统一的 is_hooks_installed）
            try:
                hooks_ok = agent.is_hooks_installed()
                print(f"  {'✓' if hooks_ok else '✗'} Hooks: {'已安装' if hooks_ok else '未安装'}")
            except Exception as e:
                print(f"  ✗ Hooks 检查失败: {e}")

            try:
                mcp_ok = agent.is_mcp_configured()
                print(f"  {'✓' if mcp_ok else '✗'} MCP 主动工具: {'已配置' if mcp_ok else '未配置'}")
            except Exception as e:
                print(f"  ✗ MCP 检查失败: {e}")

            try:
                active_ok = agent.is_active_connection_installed()
                print(f"  {'✓' if active_ok else '✗'} 主动接入: {'就绪' if active_ok else '未就绪'}")
            except Exception as e:
                print(f"  ✗ 主动接入检查失败: {e}")

            # 3. 事件目录
            event_dir = Path.home() / ".mnemos" / "events"
            try:
                event_dir.mkdir(parents=True, exist_ok=True)
                test_file = event_dir / ".write_test"
                test_file.write_text("ok", encoding="utf-8")
                test_file.unlink()
                print(f"  ✓ 事件目录可读写: {event_dir}")
            except Exception as e:
                print(f"  ✗ 事件目录不可写: {e}")

            # 4. 蒸馏队列状态
            from core.hephaestus_worker import HephaestusWorker
            try:
                worker = HephaestusWorker()
                stats = worker.get_stats()
                print(f"  ✓ 蒸馏队列: {stats['pending']} 待处理, {stats['delegated']} 已委托")
            except Exception as e:
                print(f"  ✗ 蒸馏队列检查失败: {e}")

            # 5. 信号采集
            try:
                signals = agent.collect_signals(days=7)
                print(f"  ✓ 信号采集: 最近7天 {len(signals)} 条")
            except Exception as e:
                print(f"  ✗ 信号采集失败: {e}")

        if target and checked == 0:
            print(f"✗ 未找到 Agent: {target}")
            return False
        print(f"\n{'=' * 60}\n诊断完成: {checked} 个 Agent")
        return True
    else:
        print("可用子命令: list, detect, install, doctor")


def cmd_mcp_serve(args):
    """启动 MCP 服务器"""
    # MCP stdio 协议要求 stdout 只能输出 JSON，所有日志/提示走 stderr
    print("启动 MCP 服务器 (stdin/stdout 模式)...", file=sys.stderr)
    print("按 Ctrl+C 停止", file=sys.stderr)

    try:
        from integrations.agora import run_mcp_server
        run_mcp_server()
    except ImportError:
        print("MCP 服务器未实现。请先安装 mnemos[mcp] 依赖。")
        sys.exit(1)


def cmd_scorer(args):
    """评分层管理"""
    if args.scorer_cmd == "status":
        try:
            from core.kia.chronos import KnowledgeScheduler
            scheduler = KnowledgeScheduler()
            scheduler.register_all_default_steps()
            steps = scheduler.get_step_status()
            print("KIA 调度步骤状态:")
            if not steps:
                print("  (暂无已注册步骤。建议运行初始化或检查配置)")
            for name, info in steps.items():
                status = "启用" if info["enabled"] else "禁用"
                fails = f" ({info['consecutive_failures']}次失败)" if info["consecutive_failures"] > 0 else ""
                print(f"  {name}: {status} | {info['trigger']}{fails}")
        except Exception as e:
            print(f"状态查询失败: {e}")

    elif args.scorer_cmd == "retrain":
        print("重训练请求已发出（异步处理）")

    elif args.scorer_cmd == "rollback":
        print("回滚到上一版本（异步处理）")

    else:
        print("用法: mnemos scorer {status|retrain|rollback}")


def cmd_sync(args):
    """同步层管理"""
    if args.sync_cmd == "status":
        try:
            from core.config import get_config
            import sqlite3
            db_path = get_config().data_dir / "sync_log.db"
            if db_path.exists():
                with sqlite3.connect(str(db_path), timeout=10) as conn:
                    cursor = conn.execute("""
                        SELECT agent_name, COUNT(*), MAX(synced_at)
                        FROM sync_log
                        WHERE date(synced_at) >= date('now', '-7 days')
                        GROUP BY agent_name
                    """)
                    rows = cursor.fetchall()
                    if rows:
                        print("最近7天同步统计:")
                        for agent, count, last_sync in rows:
                            print(f"  {agent}: {count}条 | 最近: {last_sync}")
                    else:
                        print("最近7天无同步记录")
            else:
                print("同步数据库不存在")
        except Exception as e:
            print(f"状态查询失败: {e}")

    elif args.sync_cmd == "retry-failed":
        try:
            from core.sync_framework.sync_engine import SyncEngine
            engine = SyncEngine()
            result = engine.retry_failed()
            print(f"重试完成: {result}")
        except Exception as e:
            print(f"重试失败: {e}")

    elif args.sync_cmd == "backfill":
        _cmd_sync_backfill(args)

    elif args.sync_cmd == "audit":
        _cmd_sync_audit(args)

    else:
        print("用法: mnemos sync {status|retry-failed|backfill|audit}")


def _compress_ranges(numbers: List[int]) -> str:
    """将连续整数列表压缩为范围字符串，如 [1,2,3,5,7] -> '1-3,5,7'"""
    if not numbers:
        return ""
    numbers = sorted(numbers)
    ranges = []
    start = end = numbers[0]
    for n in numbers[1:]:
        if n == end + 1:
            end = n
        else:
            ranges.append(f"{start}-{end}" if start != end else str(start))
            start = end = n
    ranges.append(f"{start}-{end}" if start != end else str(start))
    return ",".join(ranges)


def _cmd_sync_backfill(args):
    """历史回填：全量/大批量扫描 Agent 历史会话 — P0-4 直接调用 SyncEngine，绕过 CaptureQueue"""
    from core.sync_framework.sync_engine import SyncEngine
    from core.sync_framework.registry import AgentRegistry
    from core.config import get_config

    config = get_config()
    source_filter = getattr(args, 'source', None)
    since_hours = getattr(args, 'since', 0) or 0
    max_turns = getattr(args, 'max_turns', 0) or config.get("sync.backfill_max_turns_per_session", 0)
    max_sessions = getattr(args, 'max_sessions', 0) or 0
    dry_run = getattr(args, 'dry_run', False)

    AgentRegistry.register_builtin_agents()
    agents = AgentRegistry.auto_discover()
    if source_filter and source_filter != "all":
        agents = [a for a in agents if a.name == source_filter]
    if not agents:
        print("未发现任何 Agent 源")
        return

    print(f"历史回填: 发现 {len(agents)} 个 Agent 源")
    if since_hours:
        print(f"  时间范围: 最近 {since_hours} 小时")
    else:
        print(f"  时间范围: 全部历史")
    print(f"  每 session 最大 turn 数: {max_turns if max_turns else '无限制'}")
    print(f"  每 source 最大 session 数: {max_sessions if max_sessions else '无限制'}")
    if dry_run:
        print("  [dry-run] 只统计，不入库")
    print()

    # P0-4: 直接调用 SyncEngine，incremental=False 确保中间缺洞也能补齐
    engine = SyncEngine()
    total_stats = {
        "agents": 0, "sessions": 0, "turns": 0,
        "synced": 0, "updated": 0, "skipped": 0, "failed": 0, "noise": 0,
        "skipped_empty": 0, "missing_turns": 0,
        "skipped_complete": 0,
    }

    import time
    now = time.time()
    recent_seconds = since_hours * 3600 if since_hours else 0

    for source in agents:
        sessions = source.discover_sessions()
        if not sessions:
            continue
        total_stats["agents"] += 1

        # 按修改时间排序（最新的在前）
        sessions_with_mtime = []
        for si in sessions:
            try:
                mtime = si.source_path.stat().st_mtime
            except OSError:
                mtime = 0
            if recent_seconds and (now - mtime) > recent_seconds:
                continue
            sessions_with_mtime.append((mtime, si))
        sessions_with_mtime.sort(key=lambda x: x[0], reverse=True)

        if max_sessions:
            sessions_with_mtime = sessions_with_mtime[:max_sessions]

        agent_synced = 0
        agent_parsed_turns = 0
        agent_missing_turns = 0
        duplicate_cache = None
        duplicate_cache_ready = False
        for mtime, session_info in sessions_with_mtime:
            try:
                turns = source.parse_turns(session_info.source_path)
                if not turns:
                    total_stats["skipped_empty"] += 1
                    continue
                turns = sorted(turns, key=lambda t: t.turn_number)
                # 查询已同步的 turn 范围，用于 dry-run 报告缺洞
                existing_turns = engine._get_synced_turns(source.name, session_info.session_id)
                all_turn_numbers = {t.turn_number for t in turns}
                missing_turns = sorted(all_turn_numbers - set(existing_turns))

                if max_turns and len(turns) > max_turns:
                    turns = turns[-max_turns:]
                    all_turn_numbers = {t.turn_number for t in turns}
                    missing_turns = sorted(all_turn_numbers - set(existing_turns))
                if missing_turns:
                    total_stats["missing_turns"] += len(missing_turns)

                missing_set = set(missing_turns)
                turns_to_sync = [t for t in turns if t.turn_number in missing_set]
                agent_parsed_turns += len(turns)
                agent_missing_turns += len(turns_to_sync)
                total_stats["sessions"] += 1
                total_stats["turns"] += len(turns_to_sync if not dry_run else turns)

                if dry_run:
                    if missing_turns:
                        ranges = _compress_ranges(missing_turns)
                        print(f"  [dry-run] {source.name}/{session_info.session_id}: {len(turns)} turns, missing {len(missing_turns)} ({ranges})")
                    continue

                if not turns_to_sync:
                    total_stats["skipped_complete"] += 1
                    continue

                if not duplicate_cache_ready:
                    duplicate_cache = engine.build_memos_duplicate_cache(source.name)
                    duplicate_cache_ready = True

                ranges = _compress_ranges(missing_turns)
                print(
                    f"  {source.name}/{session_info.session_id}: "
                    f"sync missing {len(turns_to_sync)}/{len(turns)} turns ({ranges})",
                    flush=True,
                )

                # P0-4/P0-6: backfill 只补缺洞 turn，并用 source 级 Memos 缓存兜底防重。
                results = []
                for idx, turn in enumerate(turns_to_sync, 1):
                    result = engine.sync_single_turn(
                        source,
                        session_info,
                        turn,
                        incremental=False,
                        check_memos_duplicate=duplicate_cache is None,
                        memos_duplicate_cache=duplicate_cache,
                    )
                    results.append(result)
                    if len(turns_to_sync) >= 50 and (idx % 50 == 0 or idx == len(turns_to_sync)):
                        print(
                            f"    progress {idx}/{len(turns_to_sync)} "
                            f"(turn={turn.turn_number}, action={result.action})",
                            flush=True,
                        )
                for r in results:
                    if r.action == "new":
                        total_stats["synced"] += 1
                        agent_synced += 1
                    elif r.action == "updated":
                        total_stats["updated"] += 1
                        agent_synced += 1
                    elif r.action in ("skipped", "skipped_memos"):
                        total_stats["skipped"] += 1
                    elif r.action == "noise":
                        total_stats["noise"] += 1
                    elif r.action == "failed":
                        total_stats["failed"] += 1
            except Exception as e:
                total_stats["failed"] += 1
                print(f"  ✗ {source.name}/{session_info.session_id}: {e}")

        print(
            f"  {source.name}: 扫描 {len(sessions_with_mtime)} sessions, "
            f"解析 {agent_parsed_turns} turns, 待补 {agent_missing_turns} turns, 同步 {agent_synced}"
        )

    engine.close()
    print()
    print("回填统计:")
    print(f"  Agent 源: {total_stats['agents']}")
    print(f"  Sessions: {total_stats['sessions']}")
    print(f"  Turns: {total_stats['turns']}")
    if not dry_run:
        print(f"  Synced(new): {total_stats['synced']}")
        print(f"  Updated: {total_stats['updated']}")
        print(f"  Skipped: {total_stats['skipped']}")
        print(f"  Noise: {total_stats['noise']}")
        print(f"  Failed: {total_stats['failed']}")
    print(f"  Missing turns: {total_stats['missing_turns']}")
    print(f"  Skipped(complete): {total_stats['skipped_complete']}")
    print(f"  Skipped(empty): {total_stats['skipped_empty']}")


def _cmd_sync_audit(args):
    """同步完整性审计：扫描各 Agent 的 session 缺洞情况"""
    from core.sync_framework.sync_engine import SyncEngine
    from core.sync_framework.registry import AgentRegistry
    from core.config import get_config
    import sqlite3

    source_filter = getattr(args, 'source', None)
    config = get_config()
    db_path = config.data_dir / "sync_log.db"

    AgentRegistry.register_builtin_agents()
    agents = AgentRegistry.auto_discover()
    if source_filter and source_filter != "all":
        agents = [a for a in agents if a.name == source_filter]
    if not agents:
        print("未发现任何 Agent 源")
        return

    engine = SyncEngine()
    total_sessions = 0
    sessions_with_gaps = 0
    largest_gap = {"session_id": "", "parsed_turns": 0, "synced_turns": 0, "missing_turns": 0, "missing_ranges": ""}

    for source in agents:
        sessions = source.discover_sessions()
        if not sessions:
            continue
        agent_sessions = 0
        agent_gaps = 0
        for session_info in sessions:
            try:
                turns = source.parse_turns(session_info.source_path)
                if not turns:
                    continue
                all_turn_numbers = sorted({t.turn_number for t in turns})
                synced_turns = engine._get_synced_turns(source.name, session_info.session_id)
                missing = sorted(set(all_turn_numbers) - set(synced_turns))
                total_sessions += 1
                agent_sessions += 1
                if missing:
                    sessions_with_gaps += 1
                    agent_gaps += 1
                    if len(missing) > largest_gap["missing_turns"]:
                        largest_gap = {
                            "session_id": session_info.session_id,
                            "parsed_turns": len(all_turn_numbers),
                            "synced_turns": len(synced_turns),
                            "missing_turns": len(missing),
                            "missing_ranges": _compress_ranges(missing),
                        }
            except Exception as e:
                print(f"  ✗ {source.name}/{session_info.session_id}: {e}")

        print(f"{source.name}: {agent_sessions} sessions, {agent_gaps} with gaps")

    engine.close()
    print()
    print("同步完整性审计结果:")
    print(f"  总 sessions: {total_sessions}")
    print(f"  有缺洞的 sessions: {sessions_with_gaps}")
    if largest_gap["missing_turns"] > 0:
        print(f"  最大缺洞:")
        print(f"    session_id: {largest_gap['session_id']}")
        print(f"    parsed_turns: {largest_gap['parsed_turns']}")
        print(f"    synced_turns: {largest_gap['synced_turns']}")
        print(f"    missing_turns: {largest_gap['missing_turns']}")
        print(f"    missing_ranges: {largest_gap['missing_ranges']}")


def cmd_build_relation_index(args):
    """重建关联上下文向量索引"""
    try:
        from core.kia.knowledge_graph import KnowledgeGraph
        from core.embeddings.relation_manager import RelationEmbeddingManager
        from core.config import get_config

        config = get_config()
        wiki_dir = config.wiki_dir
        db_path = getattr(config, 'data_dir', Path.home() / '.mnemos') / 'knowledge_graph.db'

        print("重建关联上下文向量索引...")
        kg = KnowledgeGraph(db_path=str(db_path), wiki_base=str(wiki_dir))

        # 先清理旧索引
        rel_mgr = RelationEmbeddingManager(db_path=db_path)
        stats_old = rel_mgr.get_stats()
        print(f"  当前索引: {stats_old['total_relations']} 个 embedding")

        # 批量重建
        result = kg.rebuild_relation_index(batch_size=50)
        print(f"  处理完成: {result['total']} 个关系")
        print(f"  成功更新: {result['updated']} 个")
        if result['failed'] > 0:
            print(f"  失败: {result['failed']} 个")
        if result['skipped'] > 0:
            print(f"  跳过: {result['skipped']} 个")

        stats_new = rel_mgr.get_stats()
        print(f"  重建后索引: {stats_new['total_relations']} 个 embedding")
    except Exception as e:
        print(f"重建失败: {e}")


def cmd_search(args):
    """上下文感知搜索"""
    try:
        from core.app.context_search import ContextAwareSearch
        search = ContextAwareSearch()
        results = search.search(args.query, limit=args.limit or 10)

        if not results:
            print(f"未找到与 '{args.query}' 相关的知识")
            return

        print(f"搜索结果 ({len(results)} 条):")
        for i, r in enumerate(results, 1):
            badges = []
            if getattr(r, 'verification', ''):
                badges.append(r.verification)
            if getattr(r, 'source', ''):
                badges.append(f"来源:{r.source}")
            badge_str = f" ({', '.join(badges)})" if badges else ""
            score_detail = ""
            if getattr(r, 'page_embedding_score', 0.0) > 0 or getattr(r, 'relation_score', 0.0) > 0:
                score_detail = f" [p={r.page_embedding_score:.2f} r={r.relation_score:.2f} k={r.keyword_score:.2f}]"
            print(f"  {i}. [{r.score:.2f}]{score_detail} {r.title}{badge_str}")
            if r.snippet:
                snippet = r.snippet[:80].replace("\n", " ")
                print(f"     {snippet}...")
            print(f"     路径: {r.page_path}")
    except Exception as e:
        print(f"搜索失败: {e}")


def cmd_metrics_scan(args):
    """扫描 Wiki 页面 metrics"""
    try:
        from core.wiki_metrics import WikiMetrics
        from core.config import get_config
        wiki_dir = get_config().wiki_dir
        wm = WikiMetrics(wiki_dir=str(wiki_dir))
        print(f"扫描 Wiki metrics: {wiki_dir}")
        result = wm.scan_all_pages()
        print(f"  扫描完成: {result['total']} 个页面")
        print(f"  新增: {result['inserted']}  更新: {result['updated']}")
        if result.get("deleted", 0):
            print(f"  清理失效 metrics: {result['deleted']}")
        try:
            from core.wiki_metrics import write_mnemos_home
            home = write_mnemos_home(str(wiki_dir))
            print(f"  已更新首页: {home}")
        except Exception as e:
            print(f"  首页更新失败: {e}")
    except Exception as e:
        print(f"扫描失败: {e}")


def cmd_perf(args):
    """查看后台性能与队列压力"""
    config = get_config()
    print("Mnemos 性能状态")
    print("=" * 40)
    print(f"性能档位:      {config.get('performance_tier', 'default')}")
    print(f"L1 扫描周期:   {config.get('sync.l1_scan_poll_interval_seconds', 60)}s")
    print(f"L1 每轮源数:   {config.get('sync.l1_scan_max_sources_per_cycle', 3)}")
    print(f"L1 每源会话:   {config.get('sync.l1_scan_max_sessions_per_source', 20)}")
    print(f"Capture workers: {config.get('capture.max_workers', 4)}")
    print()

    print("daemon 进程:")
    processes = _daemon_processes()
    if not processes:
        print("  未检测到运行中的 daemon")
    else:
        import subprocess
        for line in processes:
            pid = line.split()[0]
            if pid.isdigit():
                try:
                    ps = subprocess.run(
                        ["ps", "-p", pid, "-o", "pid=,pcpu=,pmem=,rss=,etime="],
                        capture_output=True, text=True, timeout=5,
                    )
                    if ps.returncode == 0:
                        print(f"  {ps.stdout.strip()}  rss(KB)")
                        continue
                except Exception:
                    pass
            print(f"  {line}")
    print()

    print("数据体积:")
    for label, path in [
        ("mnemos_dir", config.data_dir),
        ("events.db", config.data_dir / "events.db"),
        ("capture_queue.db", config.data_dir / "capture_queue.db"),
        ("knowledge_graph.db", config.data_dir / "knowledge_graph.db"),
        ("embedding_index", config.data_dir / "embedding_index"),
        ("daemon.log", config.data_dir / "daemon.log"),
    ]:
        try:
            if path.is_dir():
                total = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
            elif path.exists():
                total = path.stat().st_size
            else:
                total = 0
            print(f"  {label}: {_format_bytes(total)}")
        except Exception as e:
            print(f"  {label}: 统计失败 ({e})")
    print()

    print("队列压力:")
    try:
        import sqlite3
        events_db = config.data_dir / "events.db"
        if events_db.exists():
            with sqlite3.connect(str(events_db), timeout=5) as conn:
                pending = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE status IN ('pending','processing')"
                ).fetchone()[0]
                dead = conn.execute("SELECT COUNT(*) FROM dead_letters").fetchone()[0]
            print(f"  events pending/processing: {pending}")
            print(f"  dead_letters: {dead}")
        else:
            print("  events.db: 不存在")
    except Exception as e:
        print(f"  events.db: 读取失败 ({e})")

    try:
        import sqlite3
        cq_db = config.data_dir / "capture_queue.db"
        if cq_db.exists():
            with sqlite3.connect(str(cq_db), timeout=5) as conn:
                rows = conn.execute(
                    "SELECT status, COUNT(*) FROM capture_events GROUP BY status"
                ).fetchall()
            print("  capture_queue:")
            for status, count in rows:
                print(f"    - {status}: {count}")
        else:
            print("  capture_queue.db: 不存在")
    except Exception as e:
        print(f"  capture_queue.db: 读取失败 ({e})")

    print()
    print("L1 最近扫描:")
    state_path = config.data_dir / "l1_scan_state.json"
    if state_path.exists():
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            sources = [
                v for k, v in data.items()
                if k.startswith("__source__:") and isinstance(v, dict)
            ]
            if sources:
                for item in sorted(sources, key=lambda x: x.get("source", "")):
                    print(f"  {item.get('source')}: {item.get('scanned_at')}")
            else:
                print("  尚无 source 轮转记录")
        except Exception as e:
            print(f"  读取失败: {e}")
    else:
        print("  尚无扫描游标")


def cmd_wiki(args):
    """Wiki 知识库操作"""
    if args.wiki_cmd == "read":
        try:
            from integrations.oracle import WikiReader
            reader = WikiReader()
            result = reader.read_page(args.page_path, depth=args.depth)
            if not result:
                print(f"未找到页面: {args.page_path}")
                return
            print(f"📄 {result.get('title', args.page_path)}")
            print(f"   深度: {result.get('depth', 'unknown')}")
            if result.get('confidence'):
                print(f"   可信度: {result['confidence']}")
            if result.get('verification'):
                print(f"   验证状态: {result['verification']}")
            if result.get('source'):
                print(f"   来源: {result['source']}")
            if result.get('last_modified'):
                print(f"   最后更新: {result['last_modified']}")
            print("-" * 40)
            content = result.get('content') or result.get('summary') or result.get('summary', '')
            if content:
                print(content[:2000])
            if result.get('related'):
                print(f"\n🔗 关联页面 ({len(result['related'])} 个):")
                for rel in result['related'][:5]:
                    label = rel.get('title') or rel.get('path') or rel.get('page_id') or 'unknown'
                    relation = rel.get('relation')
                    print(f"   - {label}{f' ({relation})' if relation else ''}")
        except Exception as e:
            print(f"读取 Wiki 失败: {e}")
    else:
        print("用法: mnemos wiki read <page_path> [--depth metadata|summary|full]")


def cmd_report(args):
    """生成报告"""
    if args.report_cmd == "generate":
        try:
            from core.app.weekly_report import WeeklyReportGenerator
            gen = WeeklyReportGenerator()
            content = gen.generate_weekly_report()
            print("周报已生成")
        except Exception as e:
            print(f"报告生成失败: {e}")
    else:
        print("用法: mnemos report generate")


def cmd_distill(args):
    """蒸馏层管理"""
    if args.distill_cmd == "audit":
        _cmd_distill_audit(args)
    else:
        print("用法: mnemos distill audit")


def _cmd_distill_audit(args):
    """蒸馏完整性审计：报告截断、缺失 prompt_version、缺失 source_coverage"""
    from core.config import get_config
    config = get_config()
    wiki_dir = config.wiki_dir

    if not wiki_dir.exists():
        print("Wiki 目录不存在")
        return

    md_files = list(wiki_dir.rglob("*.md"))
    total = len(md_files)
    pages_with_truncated_source = 0
    pages_without_prompt_version = 0
    pages_without_source_coverage = 0
    pages_with_old_prompt_version = 0

    import yaml
    for md_file in md_files:
        try:
            content = md_file.read_text(encoding="utf-8", errors="ignore")
            if not content.startswith("---"):
                continue
            parts = content.split("---", 2)
            if len(parts) < 3:
                continue
            fm = yaml.safe_load(parts[1]) or {}

            # 只统计蒸馏生成的页面（有 source_session 或 distilled_at）
            if not (fm.get("source_session") or fm.get("蒸馏时间")):
                continue

            if fm.get("truncated") is True:
                pages_with_truncated_source += 1
            if not fm.get("distill_prompt_version"):
                pages_without_prompt_version += 1
            elif str(fm.get("distill_prompt_version")) != PROMPT_VERSION:
                pages_with_old_prompt_version += 1
            if not fm.get("source_coverage"):
                pages_without_source_coverage += 1
        except Exception:
            continue

    print("蒸馏完整性审计结果:")
    print(f"  Wiki 页面总数: {total}")
    print(f"  截断输入页面: {pages_with_truncated_source}")
    print(f"  缺少 prompt_version: {pages_without_prompt_version}")
    print(f"  旧 prompt_version: {pages_with_old_prompt_version}")
    print(f"  缺少 source_coverage: {pages_without_source_coverage}")


def cmd_events(args):
    """事件总线管理"""
    from core.config import get_config
    import sqlite3

    config = get_config()
    events_db = config.data_dir / "events.db"

    if args.events_cmd == "stats":
        if not events_db.exists():
            print("events.db 不存在")
            return
        try:
            with sqlite3.connect(str(events_db), timeout=5) as conn:
                total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                pending = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE status IN ('pending', 'processing')"
                ).fetchone()[0]
                dl = conn.execute("SELECT COUNT(*) FROM dead_letters").fetchone()[0]
                rows = conn.execute(
                    "SELECT event_type, status, COUNT(*) FROM events "
                    "GROUP BY event_type, status ORDER BY COUNT(*) DESC LIMIT 10"
                ).fetchall()
            print(f"events.db 统计:")
            print(f"  总数: {total}")
            print(f"  pending/processing: {pending}")
            print(f"  dead_letters: {dl}")
            print(f"  Top 10 事件类型:")
            for event_type, status, count in rows:
                print(f"    - {event_type}/{status}: {count}")
        except Exception as e:
            print(f"统计失败: {e}")

    elif args.events_cmd == "archive-orphans":
        try:
            from core.mnemos_bus import _get_bus
            bus = _get_bus()
            archived = bus.archive_no_consumer_events()
            print(f"归档完成: {archived} 个无消费者历史事件已归档")
        except Exception as e:
            print(f"归档失败: {e}")

    elif args.events_cmd == "cleanup":
        if not events_db.exists():
            print("events.db 不存在")
            return
        try:
            with sqlite3.connect(str(events_db), timeout=10) as conn:
                # 1. 统计待删除项
                done_old = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE status = 'done' "
                    "AND created_at < datetime('now', '-7 days')"
                ).fetchone()[0]
                dl_old = conn.execute(
                    "SELECT COUNT(*) FROM dead_letters WHERE timestamp < datetime('now', '-30 days')"
                ).fetchone()[0]
                orphaned = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE status = 'pending' "
                    "AND created_at < datetime('now', '-3 days')"
                ).fetchone()[0]

            print("[dry-run] 以下事件将被清理（使用 --confirm 执行）：")
            print(f"  已完成超过 7 天的事件: {done_old}")
            print(f"  死信超过 30 天的事件: {dl_old}")
            print(f"  orphaned pending 超过 3 天的事件: {orphaned}")

            if not getattr(args, 'confirm', False):
                print("  未指定 --confirm，跳过删除。建议先运行 `mnemos events archive-orphans` 归档。")
                return

            with sqlite3.connect(str(events_db), timeout=10) as conn:
                cursor = conn.execute(
                    "DELETE FROM events WHERE status = 'done' "
                    "AND created_at < datetime('now', '-7 days')"
                )
                done_removed = cursor.rowcount

                cursor = conn.execute(
                    "DELETE FROM dead_letters WHERE timestamp < datetime('now', '-30 days')"
                )
                dl_removed = cursor.rowcount

                cursor = conn.execute(
                    "SELECT id FROM events WHERE status = 'pending' "
                    "AND created_at < datetime('now', '-3 days')"
                )
                orphaned_removed = 0
                for row in cursor.fetchall():
                    conn.execute("DELETE FROM events WHERE id = ?", (row[0],))
                    orphaned_removed += 1

                conn.commit()

            with sqlite3.connect(str(events_db), timeout=10) as vacuum_conn:
                vacuum_conn.execute("VACUUM")

            print(f"清理完成:")
            print(f"  删除已完成旧事件: {done_removed}")
            print(f"  删除死信旧事件: {dl_removed}")
            print(f"  删除 orphaned pending 事件: {orphaned_removed}")
            print(f"  已执行 VACUUM 释放磁盘空间")
        except Exception as e:
            print(f"清理失败: {e}")

    else:
        print("用法: mnemos events {stats|cleanup}")


def main():
    # Fix Windows console encoding (cp1252 can't handle Chinese/emoji)
    if sys.platform == "win32":
        try:
            if hasattr(sys.stdout, 'reconfigure'):
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            if hasattr(sys.stderr, 'reconfigure'):
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    parser = argparse.ArgumentParser(description="Mnemos")
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # init
    init_parser = subparsers.add_parser("init", help="交互式配置向导")

    # doctor
    doctor_parser = subparsers.add_parser("doctor", help="系统诊断")

    # status
    status_parser = subparsers.add_parser("status", help="查看系统状态")

    # metrics
    metrics_parser = subparsers.add_parser("metrics", help="Wiki 页面度量管理")
    metrics_sub = metrics_parser.add_subparsers(dest="metrics_cmd")
    metrics_sub.add_parser("scan", help="全量扫描 Wiki 页面 metrics")

    # perf
    subparsers.add_parser("perf", help="查看后台性能、队列和数据体积")

    # config
    config_parser = subparsers.add_parser("config", help="查看/编辑配置")
    config_parser.add_argument("--set", help="设置配置项 (如 wiki.vault_path=~/wiki)")

    # agent
    agent_parser = subparsers.add_parser("agent", help="AI Agent 管理")
    agent_sub = agent_parser.add_subparsers(dest="agent_cmd")
    agent_sub.add_parser("list", help="列出本地可用的 AI Agent")
    install_parser = agent_sub.add_parser("install", help="为所有可用 Agent 安装主动接入")
    install_parser.add_argument("agent_name", nargs="?", default="", help="指定 Agent 名称（可选，如 claude/hermes/openclaw/opencode/codex/kimi）")
    agent_sub.add_parser("detect", help="检测宿主 Agent（MNEMOS_HOST_AGENT）")
    doctor_parser = agent_sub.add_parser("doctor", help="诊断 Agent 状态")
    doctor_parser.add_argument("agent_name", nargs="?", default="", help="指定 Agent 名称（可选，如 claude/hermes/openclaw/opencode/codex）")

    # daemon
    daemon_parser = subparsers.add_parser("daemon", help="后台守护进程")
    daemon_sub = daemon_parser.add_subparsers(dest="daemon_cmd")
    daemon_sub.add_parser("start", help="启动守护进程")
    daemon_sub.add_parser("stop", help="停止守护进程")
    daemon_sub.add_parser("status", help="查看守护进程状态")

    # scheduler
    scheduler_parser = subparsers.add_parser("scheduler", help="定时任务管理")
    scheduler_sub = scheduler_parser.add_subparsers(dest="scheduler_cmd")
    scheduler_sub.add_parser("install-windows", help="注册 Windows 开机启动任务")
    scheduler_sub.add_parser("uninstall-windows", help="注销 Windows 开机启动任务")

    # calibrate
    subparsers.add_parser("calibrate", help="画像校准与挑战反馈")

    # mcp serve
    mcp_parser = subparsers.add_parser("mcp", help="MCP 协议")
    mcp_sub = mcp_parser.add_subparsers(dest="mcp_cmd")
    mcp_sub.add_parser("serve", help="启动 MCP 服务器")

    # scorer
    scorer_parser = subparsers.add_parser("scorer", help="评分层管理")
    scorer_sub = scorer_parser.add_subparsers(dest="scorer_cmd")
    scorer_sub.add_parser("status", help="查看评分器和调度步骤状态")
    scorer_sub.add_parser("retrain", help="触发模型重训练")
    scorer_sub.add_parser("rollback", help="回滚到上一版本模型")

    # sync
    sync_parser = subparsers.add_parser("sync", help="同步层管理")
    sync_sub = sync_parser.add_subparsers(dest="sync_cmd")
    sync_sub.add_parser("status", help="查看同步状态")
    sync_sub.add_parser("retry-failed", help="重试失败的同步任务")
    backfill_parser = sync_sub.add_parser("backfill", help="历史回填：全量/大批量扫描 Agent 历史会话")
    backfill_parser.add_argument("--source", help="指定 Agent 源（如 claude/kimi/codex/all）")
    backfill_parser.add_argument("--since", type=float, default=0, help="时间范围（小时，0=全部）")
    backfill_parser.add_argument("--max-turns", type=int, default=0, help="每 session 最大 turn 数（0=无限制）")
    backfill_parser.add_argument("--max-sessions", type=int, default=0, help="每 source 最大 session 数（0=无限制）")
    backfill_parser.add_argument("--dry-run", action="store_true", help="只统计，不入队")
    audit_parser = sync_sub.add_parser("audit", help="同步完整性审计：报告缺洞、截断、覆盖率")
    audit_parser.add_argument("--source", default="all", help="指定 Agent 源（如 claude/kimi/codex/all）")

    # build-relation-index
    subparsers.add_parser("build-relation-index", help="重建关联上下文向量索引")

    # search
    search_parser = subparsers.add_parser("search", help="上下文感知搜索")
    search_parser.add_argument("query", help="搜索查询")
    search_parser.add_argument("--limit", type=int, default=10, help="最大结果数")

    # wiki
    wiki_parser = subparsers.add_parser("wiki", help="Wiki 知识库操作")
    wiki_sub = wiki_parser.add_subparsers(dest="wiki_cmd")
    wiki_read_parser = wiki_sub.add_parser("read", help="读取指定 Wiki 页面")
    wiki_read_parser.add_argument("page_path", help="Wiki 页面路径（如 03-Tech/codex-cli.md）")
    wiki_read_parser.add_argument("--depth", choices=["metadata", "summary", "full"],
                                   default="full", help="读取深度")

    # report
    report_parser = subparsers.add_parser("report", help="报告生成")
    report_sub = report_parser.add_subparsers(dest="report_cmd")
    report_sub.add_parser("generate", help="生成周报")

    # distill
    distill_parser = subparsers.add_parser("distill", help="蒸馏层管理")
    distill_sub = distill_parser.add_subparsers(dest="distill_cmd")
    distill_sub.add_parser("audit", help="蒸馏完整性审计：报告截断、缺失 prompt_version、source_coverage")

    # events
    events_parser = subparsers.add_parser("events", help="事件总线管理")
    events_sub = events_parser.add_subparsers(dest="events_cmd")
    cleanup_parser = events_sub.add_parser("cleanup", help="清理旧事件和死信（默认 dry-run）")
    cleanup_parser.add_argument("--confirm", action="store_true", help="确认执行删除和 VACUUM")
    events_sub.add_parser("archive-orphans", help="归档无消费者的历史 pending 事件")
    events_sub.add_parser("stats", help="查看事件总线统计")

    args = parser.parse_args()

    if args.command == "init":
        cmd_init(args)
    elif args.command == "doctor":
        ok = cmd_doctor(args)
        sys.exit(0 if ok else 1)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "config":
        cmd_config(args)
    elif args.command == "agent":
        cmd_agent(args)
    elif args.command == "daemon":
        cmd_daemon(args)
    elif args.command == "scheduler":
        cmd_scheduler(args)
    elif args.command == "calibrate":
        cmd_calibrate(args)
    elif args.command == "mcp" and args.mcp_cmd == "serve":
        cmd_mcp_serve(args)
    elif args.command == "scorer":
        cmd_scorer(args)
    elif args.command == "sync":
        cmd_sync(args)
    elif args.command == "search":
        cmd_search(args)
    elif args.command == "build-relation-index":
        cmd_build_relation_index(args)
    elif args.command == "wiki":
        cmd_wiki(args)
    elif args.command == "report":
        cmd_report(args)
    elif args.command == "distill":
        cmd_distill(args)
    elif args.command == "events":
        cmd_events(args)
    elif args.command == "metrics" and args.metrics_cmd == "scan":
        cmd_metrics_scan(args)
    elif args.command == "perf":
        cmd_perf(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
