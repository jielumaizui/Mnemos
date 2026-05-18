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

import os
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime

from core.config import get_config, Config


def cmd_init(args):
    """交互式配置向导"""
    print("=" * 60)
    print("Mnemos 初始化向导")
    print("=" * 60)
    print()

    config = get_config()

    # 1. Wiki 路径
    default_wiki = config.wiki_dir
    print(f"[1/6] 知识库路径")
    print(f"      默认: {default_wiki}")
    user_wiki = input(f"      你的 Obsidian Vault wiki 目录 (回车=默认): ").strip()
    if user_wiki:
        config.set("wiki.vault_path", user_wiki)

    # 2. Memos 配置
    print()
    print(f"[2/6] Memos 集成")
    memos_url = input("      Memos API 地址 (如 https://memos.example.com, 回车=跳过): ").strip()
    if memos_url:
        config.set("memos.enabled", True)
        config.set("memos.api_url", memos_url)
        token = input("      Memos Token (建议通过 MEMOS_TOKEN 环境变量设置): ").strip()
        if token:
            config.set("memos.token", token)
    else:
        config.set("memos.enabled", False)

    # 3. 画像数据源
    print()
    print(f"[3/6] 用户画像数据源")
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

    # 4. AI Agent 集成
    print()
    print(f"[4/6] AI Agent 集成")
    cc_path = input(f"      Claude Code settings.json 路径 (回车={config.claude_settings_path}): ").strip()
    if cc_path:
        config.set("integrations.claude_code.settings_json_path", cc_path)

    mcp = input("      启用 MCP 协议? [y/N]: ").strip()
    config.set("integrations.mcp.enabled", mcp.lower() == "y")

    # 5. 可选依赖推荐
    print()
    print(f"[5/6] 可选依赖安装")
    print(f"      以下依赖不装也能跑，但会影响部分功能：")
    print()

    optional_deps = [
        ("mcp", "MCP SDK", "MCP 协议服务器 (让 AI Agent 通过标准协议调用 Mnemos)"),
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
            print(f"        └─ 安装: pip install mnemos[{ 'mcp' if module == 'mcp' else 'dev'}]")
            print()

    # 6. 保存
    print()
    print(f"[6/6] 保存配置")
    config.save()
    print(f"      ✓ 配置已保存到: {config.config_path}")

    # 6. 创建目录
    wiki_dir = config.wiki_dir
    wiki_dir.mkdir(parents=True, exist_ok=True)
    (wiki_dir / "retrospectives").mkdir(exist_ok=True)
    print(f"      ✓ 创建 Wiki 目录: {wiki_dir}")

    # 7. 安装 Claude Code hook（可选）
    if config.claude_code_enabled:
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
        script_path = Path(__file__).resolve()
        python_cmd = sys.executable if sys.platform == "win32" else "python3"
        settings["hooks"]["session_start"] = (
            f"{python_cmd} {script_path} --session-start "
            f"--working-dir \"$PWD\" --user-message \"$USER_MESSAGE\""
        )
        settings["hooks"]["session_end"] = (
            f"{python_cmd} {script_path} --session-end "
            f"--working-dir \"$PWD\" --session-messages \"$SESSION_MESSAGES\""
        )

        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)

        print(f"      ✓ 已安装 Claude Code hooks 到: {settings_path}")
    except Exception as e:
        print(f"      ⚠ 安装 hooks 失败: {e}")


def cmd_doctor(args):
    """系统诊断"""
    print("=" * 60)
    print("Mnemos 系统诊断")
    print("=" * 60)
    print()

    config = get_config()
    issues = []
    warnings = []

    # 1. Python 版本
    py_version = sys.version_info
    if py_version >= (3, 10):
        print(f"✓ Python {py_version.major}.{py_version.minor}.{py_version.micro}")
    else:
        issues.append(f"Python 版本过低: {py_version.major}.{py_version.minor} (需要 >= 3.10)")

    # 2. 核心依赖
    deps = ["requests", "yaml"]
    for dep in deps:
        try:
            __import__(dep)
            print(f"✓ {dep}")
        except ImportError:
            issues.append(f"缺少依赖: {dep} (pip install {dep if dep != 'yaml' else 'pyyaml'})")

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
    retro_dir = wiki_dir / "retrospectives"
    if retro_dir.exists():
        retro_count = len(list(retro_dir.rglob("*.md")))
        print(f"  ✓ Retrospectives: {retro_count} 条经验")
        if retro_count == 0:
            warnings.append("retrospectives 目录为空，KIA 预加载暂无数据可用")
    else:
        warnings.append("retrospectives 目录不存在，运行 `mnemos init` 创建")

    # 检查 distill_queue 和 guard_state
    distill_queue = config.claude_data_dir / "distill_queue"
    if distill_queue.exists():
        queue_count = len(list(distill_queue.iterdir()))
        print(f"  ✓ 蒸馏队列: {queue_count} 条待处理")
    else:
        print(f"  ☐ 蒸馏队列未初始化")

    # 9. 知识库健康度
    print()
    print("知识库健康度:")
    if wiki_dir.exists():
        md_files = list(wiki_dir.rglob("*.md"))
        md_count = len(md_files)
        print(f"  Wiki 页面: {md_count}")

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
                pass
    else:
        print(f"  Wiki 未初始化")

    # 10. 画像数据质量
    print()
    print("画像数据质量:")
    try:
        from core.persona.signal_store import get_signal_store
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
        print(f"  ☐ 画像数据库未初始化")

    # 11. MCP 服务器状态
    print()
    print("MCP 服务器状态:")
    try:
        from integrations.mcp_server import MCPServer
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

    # 12. 可选依赖与功能影响报告
    print()
    print("可选依赖与功能影响:")
    optional_deps = [
        ("mcp", "MCP SDK", "MCP 协议服务器", "pip install mnemos[mcp]"),
        ("black", "Black 格式化", "代码格式化", "pip install mnemos[dev]"),
        ("pytest", "Pytest 测试", "运行测试套件", "pip install mnemos[dev]"),
    ]
    has_optional_gap = False
    for module, name, feature, install_cmd in optional_deps:
        try:
            __import__(module)
            print(f"  ✓ {name}: {feature} 可用")
        except ImportError:
            has_optional_gap = True
            print(f"  ✗ {name}: {feature} 不可用 → {install_cmd}")

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
    print(f"Wiki 目录:     {config.wiki_dir}")
    print(f"Memos 集成:    {'✓' if config.memos_enabled else '✗'}")
    print(f"画像系统:      {'✓' if config.persona_enabled else '✗'}")
    print(f"Claude Code:   {'✓' if config.claude_code_enabled else '✗'}")
    print(f"MCP 协议:      {'✓' if config.mcp_enabled else '✗'}")
    print()

    # 知识库统计
    wiki_dir = config.wiki_dir
    if wiki_dir.exists():
        md_count = len(list(wiki_dir.rglob("*.md")))
        print(f"Wiki 页面数:   {md_count}")

    # 画像统计
    try:
        from core.persona.signal_store import get_signal_store
        store = get_signal_store()
        stats = store.get_signal_stats(days=30)
        total = sum(v for v in stats.values() if v > 0)
        print(f"最近30天信号: {total}")
    except Exception:
        print("画像数据库:    未初始化")


def cmd_config(args):
    """查看/编辑配置"""
    config = get_config()
    if args.set:
        key, val = args.set.split("=", 1)
        config.set(key, val)
        config.save()
        print(f"✓ 已设置 {key} = {val}")
    else:
        import yaml
        print(yaml.dump(config.to_dict(), allow_unicode=True, sort_keys=False))


def cmd_mcp_serve(args):
    """启动 MCP 服务器"""
    print("启动 MCP 服务器 (stdin/stdout 模式)...")
    print("按 Ctrl+C 停止")

    try:
        from integrations.mcp_server import run_mcp_server
        run_mcp_server()
    except ImportError:
        print("❌ MCP 服务器未实现。请先安装 mnemos[mcp] 依赖。")
        sys.exit(1)


def main():
    # Fix Windows console encoding (cp1252 can't handle Chinese/emoji)
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Mnemos")
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # init
    init_parser = subparsers.add_parser("init", help="交互式配置向导")

    # doctor
    doctor_parser = subparsers.add_parser("doctor", help="系统诊断")

    # status
    status_parser = subparsers.add_parser("status", help="查看系统状态")

    # config
    config_parser = subparsers.add_parser("config", help="查看/编辑配置")
    config_parser.add_argument("--set", help="设置配置项 (如 wiki.vault_path=~/wiki)")

    # mcp serve
    mcp_parser = subparsers.add_parser("mcp", help="MCP 协议")
    mcp_sub = mcp_parser.add_subparsers(dest="mcp_cmd")
    mcp_sub.add_parser("serve", help="启动 MCP 服务器")

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
    elif args.command == "mcp" and args.mcp_cmd == "serve":
        cmd_mcp_serve(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
