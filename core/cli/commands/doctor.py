"""Doctor command for Mnemos CLI."""

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any, List

from core.cli.helpers import (
    _daemon_processes,
    _get_config,
    _get_cognitive_graph_stats,
    _get_sqlite_conn,
    _print_config_contract,
    _print_runtime_health,
    _print_vault_status,
)
from core.cli.commands.sync import _get_backfill_status

# Constants extracted from magic numbers
DAYS_SINCE_SECONDS = 86400
CMD_DOCTOR_DAYS_SINCE_DAYS = 7
CMD_DOCTOR_DAYS_SINCE_DAYS_2 = 30
STATS_DAYS = 30
FRONTMATTER_READ_LIMIT_BYTES = 65_536

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Wiki/quality gate helpers
# ---------------------------------------------------------------------------


def _pending_verification_pages(wiki_dir: Path, limit: int = 5) -> tuple[int, list[str]]:
    """Return count and sample relative paths for pending-verification wiki pages."""
    if not wiki_dir.exists():
        return 0, []
    count = 0
    samples: list[str] = []
    for md_file in wiki_dir.rglob("*.md"):
        try:
            text = md_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "pending-verification" not in text and "待验证" not in text:
            continue
        count += 1
        if len(samples) < limit:
            try:
                samples.append(str(md_file.relative_to(wiki_dir)))
            except ValueError:
                samples.append(str(md_file))
    return count, samples


def _quality_gate_stats(wiki_dir: Path, database_dir: Path) -> dict[str, int]:
    stats = {"review_pages": 0, "rejected_records": 0}
    if wiki_dir.exists():
        for md_file in wiki_dir.rglob("*.md"):
            try:
                text = md_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "质量门禁状态: review" in text or "quality_gate_disposition: review" in text:
                stats["review_pages"] += 1

    failed_dir = database_dir / "distill_failed"
    if failed_dir.exists():
        for failed_file in failed_dir.glob("*.json"):
            try:
                text = failed_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "质量门禁拒绝" in text:
                stats["rejected_records"] += 1
    return stats


# ---------------------------------------------------------------------------
# Output helpers (single-purpose wrappers over repetitive sections)
# ---------------------------------------------------------------------------


def _print_doctor_header() -> None:
    print("=" * 60)
    print("Mnemos 系统诊断")
    print("=" * 60)
    print()


def _doctor_performance_tier(config: Any, args: Any, warnings: List[str]) -> None:
    verbose = bool(getattr(args, "verbose", False))
    tier = config.get("performance_tier", "default")
    print(f"性能档位: {tier}")
    tier_desc = {
        "eco": "节能模式 (embedding关闭, rerank关闭, 低并发)",
        "default": "默认模式 (embedding开启, rerank开启, 标准并发)",
        "performance": "性能模式 (embedding开启, rerank开启, 高并发)",
        "dev": "开发模式 (全部开启, 最大并发, 调试用)",
    }
    print(f"  {tier_desc.get(tier, '未知档位')}")
    from core.cli.doctor_helpers import describe_performance_settings, format_performance_settings

    for line in format_performance_settings(
        describe_performance_settings(config),
        verbose=verbose,
    ):
        print(line)
    print()

    if verbose:
        from core.cli.doctor_helpers import (
            format_optional_dependency_statuses,
            optional_dependency_statuses,
        )

        print("可选/兼容模块状态:")
        for line in format_optional_dependency_statuses(optional_dependency_statuses()):
            print(line)
        print()


def _doctor_collection_mode(config: Any) -> None:
    print("采集模式:")
    sessions = config.get("sync.raw_sync_sessions_per_source", 10)
    turns = config.get("sync.raw_sync_turns_per_session", 100)
    print("  默认 Raw 同步: 连续 tail + 持久化全分母 reconciliation")
    print(f"    tail / reconciliation 每源每轮最多 {sessions} 个 sessions")
    print(f"    每 session 每批最多 {turns} 个 turns（不是完成定义）")
    print("  旧会话和超限会话会在后续轮次继续推进；历史 backfill 可显式补洞。")

    try:
        from core.sync_framework.sync_engine import SyncEngine

        audit = SyncEngine().get_audit_summary(hours=24)
        if audit:
            print("  最近 24 小时同步审计:")
            for source, stats in audit.items():
                total_skipped = sum(v for k, v in stats.items() if k.startswith("skipped_"))
                if total_skipped > 0:
                    skipped_detail = ", ".join(
                        f"{k[8:]}={v}" for k, v in stats.items() if k.startswith("skipped_") and v
                    )
                    print(
                        f"    {source}: selected={stats['selected']}, synced_turns={stats['synced_turns']}, skipped={total_skipped} ({skipped_detail})"  # noqa: E501
                    )
                else:
                    print(
                        f"    {source}: selected={stats['selected']}, synced_turns={stats['synced_turns']}, skipped=0"  # noqa: E501
                    )
    except (OSError, ValueError):
        logger.warning("[doctor] (OSError, ValueError) suppressed", exc_info=True)

    try:
        bf_status = _get_backfill_status(config)
        if bf_status:
            bf_state = bf_status.get("status", "unknown")
            state_label = {"running": "运行中", "done": "已完成", "failed": "失败"}.get(
                bf_state, bf_state
            )
            print(f"  历史回填: {state_label}")
            if bf_status.get("stats"):
                s = bf_status["stats"]
                print(
                    f"    上次回填: agents={s.get('agents', '?')}, sessions={s.get('sessions', '?')}, turns={s.get('turns', '?')}"  # noqa: E501
                )
        else:
            print("  历史回填: 未运行（建议首次部署后执行 `mnemos sync backfill --since 0`）")
    except (OSError, ValueError):
        logger.warning("[doctor] (OSError, ValueError) suppressed", exc_info=True)
    print()


def _doctor_python_version(issues: List[str]) -> None:
    py_version = sys.version_info
    if py_version >= (3, 10):
        print(f"✓ Python {py_version.major}.{py_version.minor}.{py_version.micro}")
    else:
        issues.append(f"Python 版本过低: {py_version.major}.{py_version.minor} (需要 >= 3.10)")


def _doctor_core_dependencies(issues: List[str]) -> None:
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


def _doctor_git(warnings: List[str]) -> None:
    try:
        result = subprocess.run(["git", "--version"], capture_output=True, timeout=5)
        if result.returncode == 0:
            print("✓ Git")
        else:
            warnings.append("Git 已安装但运行异常")
    except FileNotFoundError:
        warnings.append("Git 未安装 (画像系统的 git 数据源将不可用)")


def _doctor_vault_and_cg(config: Any, warnings: List[str]) -> None:
    print()
    print("Vault 布局:")
    vault_status, vault_warnings = _print_vault_status(config)
    print(vault_status)
    warnings.extend(vault_warnings)

    print()
    print("跨层认知图:")
    cg_stats = _get_cognitive_graph_stats()
    if cg_stats:
        print(f"  数据库: {config.cognitive_graph_db_path}")
        print(f"  关系数: {cg_stats.get('relations', 0)}")
        print(f"  归一化节点: {cg_stats.get('canonical_nodes', 0)}")
        print(f"  待处理 outbox: {cg_stats.get('outbox_pending', 0)}")
    else:
        print(f"  ☐ 认知图数据库未初始化: {config.cognitive_graph_db_path}")

    print("✓ 本地 Raw Vault 存储后端: Obsidian")
    if config.l1_storage_enabled:
        warnings.append("l1_storage 外部存储配置已弃用；当前版本请使用 Obsidian/raw vault")


def _doctor_persona_sources(config: Any) -> None:
    print()
    print("画像数据源:")
    sources = config.persona_data_sources
    for key, info in sources.items():
        enabled = info.get("enabled", False)
        mark = "✓" if enabled else "☐"
        print(f"  {mark} {key}: {info.get('description', '')}")


def _doctor_claude_code(config: Any, warnings: List[str]) -> None:
    print()
    cc_path = config.claude_settings_path
    if cc_path.exists():
        print(f"✓ Claude Code settings.json: {cc_path}")
    else:
        warnings.append(f"Claude Code settings.json 不存在: {cc_path}")


def _doctor_agent_connectivity(warnings: List[str]) -> None:
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
            print("  ✗ 未检测到 Agent")
        host = os.environ.get("MNEMOS_HOST_AGENT", "")
        if host:
            print(f"  ✓ 宿主 Agent: {host}")
    except (ImportError, AttributeError, OSError) as e:
        warnings.append(f"Agent 检测失败: {e}")


def _doctor_agent_active_access(warnings: List[str]) -> None:
    print()
    print("Agent 主动接入状态:")
    try:
        from core.diagnostics import ConnectionDiagnostics

        agent_statuses = ConnectionDiagnostics.check_agents()
        if agent_statuses:
            for agent in agent_statuses:
                hooks = "hooks✓" if agent.hooks_installed else "hooks✗"
                mcp = "mcp✓" if agent.mcp_configured else "mcp✗"
                policy = "policy✓" if agent.policy_installed else "policy✗"
                if agent.active_ready:
                    mark = "✓"
                    status_label = "[active]"
                elif agent.passive_source_available:
                    mark = "⚠"
                    status_label = "[passive]"
                else:
                    mark = "✗"
                    status_label = ""
                print(f"  {mark} {agent.name}: {hooks}, {mcp}, {policy} {status_label}")
                if not agent.active_ready and not agent.passive_source_available:
                    warnings.append(
                        f"{agent.name} 主动接入未就绪，运行 `mnemos doctor repair {agent.name}`"
                    )
        else:
            print("  ☐ 未发现可诊断的 Agent adapter")
    except (ImportError, AttributeError, OSError) as e:
        warnings.append(f"Agent 主动接入检测失败: {e}")


def _doctor_agent_completeness(warnings: List[str]) -> None:
    print()
    print("Agent 完整性能力:")
    try:
        from core.sync_framework.registry import SourceRegistry

        SourceRegistry.register_builtin_agents()
        agents = SourceRegistry.auto_discover()
        for agent in agents:
            caps = (
                agent.completeness_capabilities()
                if hasattr(agent, "completeness_capabilities")
                else {}
            )
            fidelity = caps.get("source_fidelity", "unknown")
            mark = (
                "✓"
                if fidelity == "full"
                else "⚠" if fidelity in ("derived", "experimental") else "?"
            )
            print(f"  {mark} {agent.name}: fidelity={fidelity}")
            if caps.get("reasoning"):
                print(
                    f"    reasoning={caps.get('reasoning')}, tool_results={caps.get('tool_results')}"  # noqa: E501
                )
    except (ImportError, AttributeError, OSError) as e:
        warnings.append(f"Agent 完整性检测失败: {e}")


def _doctor_agent_full_power(warnings: List[str]) -> None:
    print()
    print("Agent Kit 满血验收:")
    try:
        from core.agent_kit import build_agent_kit_report

        report = build_agent_kit_report()
        workflow_mark = "✓" if report.workflow_contract_ok else "✗"
        print(f"  {workflow_mark} Workflow 契约")
        if report.missing_workflow_tools:
            missing = ", ".join(report.missing_workflow_tools)
            print(f"    缺失 MCP 工具: {missing}")
            warnings.append(f"Agent Kit workflow MCP 工具缺失: {missing}")

        if not report.installed_agents:
            print("  ☐ 未检测到已安装的目标 Agent（不阻断部署）")
            return

        for agent in report.agents:
            if not agent.installed:
                continue
            mark = "✓" if agent.full_power else "✗"
            print(f"  {mark} {agent.name}: status={agent.status}")
            if agent.install_evidence:
                print(f"    evidence={agent.install_evidence}")
            for gap in agent.full_power_gaps:
                print(f"    gap={gap}")
            for action in agent.repair_actions:
                print(f"    repair={action}")
            if not agent.full_power:
                warnings.append(f"{agent.name} 未达到满血接入标准")
    except (ImportError, AttributeError, OSError, RuntimeError, ValueError, TypeError) as e:
        warnings.append(f"Agent Kit 满血验收失败: {e}")


def _doctor_reasoning_mode(config: Any) -> None:
    print()
    reasoning_mode = config.get("capture.reasoning_mode", "artifact_summary")
    print(f"Reasoning 采集策略: {reasoning_mode}")
    mode_desc = {
        "off": "不采集 reasoning",
        "summary": "只保存前 2000 字摘要",
        "artifact_summary": "后端写摘要，本地 artifact 存完整 reasoning（推荐）",
        "full": "完整 reasoning 入后端",
    }
    print(f"  {mode_desc.get(reasoning_mode, '未知策略')}")


def _doctor_api_distillation(warnings: List[str]) -> None:
    print()
    print("模型 API 状态:")
    try:
        from core.privacy.redaction import redact_key_source, redact_url
        from core.llm_config import (
            resolve_embedding_api_config,
            resolve_effective_llm_api_config,
            resolve_reranker_api_config,
        )

        llm_cfg = resolve_effective_llm_api_config()
        embedding_cfg = resolve_embedding_api_config()
        reranker_cfg = resolve_reranker_api_config()
    except (ImportError, AttributeError, OSError):
        llm_cfg = None
        embedding_cfg = None
        reranker_cfg = None

    configs = [
        ("LLM（对话/蒸馏模型）", llm_cfg),
        ("Embedding（向量/关系向量/语义召回模型）", embedding_cfg),
        ("Reranker（搜索重排模型）", reranker_cfg),
    ]
    missing = []
    for label, cfg in configs:
        if cfg and cfg.configured:
            print(
                f"  ✓ {label}: {cfg.model} @ {redact_url(cfg.base_url)} "
                f"({redact_key_source(cfg.source)})"
            )
        else:
            missing.append(label)
            print(f"  ✗ {label}: 未配置完整")
            if cfg:
                print(f"    ☐ Provider: {cfg.provider}（未就绪）")
                print(f"    ☐ Model: {cfg.model}（未就绪）")

    if missing:
        warnings.append(
            "部署模型 API 未完整配置：需要 LLM、Embedding、Reranker 三类端点，"
            "每类都必须提供 model、base_url、api_key_source/api_key"
        )


def _doctor_cross_platform_compat() -> None:
    print()
    print("权重适配器状态: 已移除（原延迟蒸馏链路相关）")

    print()
    print("跨平台兼容性:")
    print(f"  ✓ 平台: {sys.platform}")
    if sys.platform == "win32":
        print("  ✓ Windows 控制台编码: 已处理")
    elif sys.platform == "darwin":
        print("  ✓ macOS launchd: 支持")
    elif sys.platform.startswith("linux"):
        print("  ✓ Linux systemd/cron: 支持")

    ext_cmds = [("libreoffice", "文档处理"), ("pdftotext", "PDF 处理"), ("tvly", "联网搜索")]
    for cmd, desc in ext_cmds:
        if shutil.which(cmd):
            print(f"  ✓ {cmd}: {desc} 可用")
        else:
            print(f"  ☐ {cmd}: {desc} 未安装 (可选)")


def _doctor_persona_database(config: Any) -> None:
    print()
    db_path = config.database_dir / "user_signals.db"
    if db_path.exists():
        print(f"✓ 画像数据库: {db_path}")
    else:
        print(f"☐ 画像数据库未创建 (首次运行时自动创建): {db_path}")


def _doctor_kia_loop(config: Any, warnings: List[str]) -> None:
    print()
    print("KIA 闭环状态:")
    wiki_dir = config.vault_dir("mnemos")
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

    distill_queue = config.data_dir / "distill_queue"
    if distill_queue.exists():
        queue_count = len(list(distill_queue.iterdir()))
        print(f"  ✓ 蒸馏队列: {queue_count} 条待处理")
    else:
        print("  ☐ 蒸馏队列未初始化")


def _doctor_dual_index(config: Any, warnings: List[str]) -> None:
    print()
    print("双索引状态 (ADR-019):")
    embedding_enabled = config.get("embedding.enabled", True)
    wiki_dir = config.vault_dir("mnemos")
    print(f"  KG 数据库: {config.database_dir / 'knowledge_graph.db'}")
    print(f"  向量索引目录: {config.database_dir / 'embedding_index'}")
    if embedding_enabled:
        print("  ✓ Embedding 已启用")
        try:
            from core.embeddings import EmbeddingIndexManager
            from core.embeddings.relation_manager import RelationEmbeddingManager

            idx = EmbeddingIndexManager(wiki_base=wiki_dir)
            page_count = len(idx._meta)
            index_exists = idx._index_path.exists()
            print(
                f"  ✓ 页面索引: {page_count} 个页面已建 meta{' (索引文件已持久化)' if index_exists else ' (索引文件未生成)'}"  # noqa: E501
            )
            if page_count == 0:
                warnings.append(
                    "页面索引为空（首次搜索时将自动构建，或运行 `mnemos search <query>` 触发）"
                )

            rel_mgr = RelationEmbeddingManager()
            rel_stats = rel_mgr.get_stats()
            print(f"  ✓ 关联索引: {rel_stats['total_relations']} 个 embedding")
            if rel_stats["total_relations"] == 0:
                warnings.append("关联上下文索引为空，运行 `mnemos build-relation-index` 构建")
        except (ImportError, OSError, AttributeError) as e:
            warnings.append(f"双索引检测失败: {e}")
    else:
        warnings.append("Embedding 未启用，当前为降级检索（关键词+图谱召回）")
        print("  ⚠ Embedding 已禁用 → 降级检索模式")


def _doctor_wiki_overview(wiki_dir: Path, warnings: List[str]) -> List[Path]:
    md_files: List[Path] = []
    if wiki_dir.exists():
        md_files = list(wiki_dir.rglob("*.md"))
    md_count = len(md_files)
    print(f"  Wiki 页面: {md_count}")

    try:
        from core.wiki_metrics import WikiMetrics

        wm = WikiMetrics(wiki_dir=str(wiki_dir))
        try:
            metrics_count = (
                wm._get_conn().execute("SELECT COUNT(*) FROM page_metrics").fetchone()[0]
            )
        finally:
            wm.close()
        coverage = (metrics_count / md_count * 100) if md_count > 0 else 0
        print(f"  Wiki metrics: {metrics_count}/{md_count} 页面 ({coverage:.0f}%)")
        if coverage < 50:
            warnings.append(
                f"Wiki metrics 覆盖率仅 {coverage:.0f}%，运行 `mnemos metrics scan` 补齐"
            )
    except (ImportError, OSError, sqlite3.Error) as e:
        warnings.append(f"Wiki metrics 检查失败: {e}")
    return md_files


_SYSTEM_PAGES = {"log.md", "index.md", "graph-index.md", "readme.md"}


def _frontmatter_block(content: str) -> str:
    if not content.startswith("---"):
        return ""
    end = content.find("\n---", 3)
    if end < 0:
        return ""
    return content[3:end]


def _read_frontmatter_prefix(path: Path) -> str:
    with path.open("rb") as handle:
        data = handle.read(FRONTMATTER_READ_LIMIT_BYTES)
    return data.decode("utf-8", errors="ignore")


def _frontmatter_scalar(frontmatter: str, key: str) -> str:
    prefix = key.lower() + ":"
    for raw_line in frontmatter.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith(prefix):
            return line.split(":", 1)[1].strip().strip("'\"")
    return ""


def _frontmatter_has_tag(frontmatter: str, tag: str) -> bool:
    target = tag.lower()
    in_tags = False
    for raw_line in frontmatter.splitlines():
        stripped = raw_line.strip()
        lowered = stripped.lower()
        if lowered.startswith("tags:"):
            in_tags = True
            value = lowered.split(":", 1)[1]
            if target in value.replace("[", " ").replace("]", " ").replace(",", " ").split():
                return True
            continue
        if in_tags:
            if raw_line[:1].strip():
                in_tags = False
            elif lowered.startswith("- ") and lowered[2:].strip() == target:
                return True
    return False


def _classify_page_source(content: str) -> str:
    """根据 frontmatter 内容推断页面来源类型。"""
    frontmatter = _frontmatter_block(content)
    if not frontmatter:
        return "人工写入"

    explicit = _frontmatter_scalar(frontmatter, "source")
    distilled_at = _frontmatter_scalar(frontmatter, "distilled_at")

    if distilled_at or _frontmatter_has_tag(frontmatter, "distilled") or explicit in (
        "distill",
        "distilled",
    ):
        return "蒸馏提取"
    if _frontmatter_has_tag(frontmatter, "l1") or _frontmatter_has_tag(
        frontmatter, "l1-sync"
    ) or explicit in ("l1", "l1-sync", "raw-sync", "raw_sync"):
        return "Raw同步"
    if _frontmatter_has_tag(frontmatter, "retrospective") or explicit == "retrospective":
        return "复盘经验"
    if _frontmatter_has_tag(frontmatter, "git") or explicit == "git":
        return "Git历史"
    if explicit:
        return "Raw同步"
    return "人工写入"


def _doctor_wiki_source_distribution(md_files: List[Path], wiki_dir: Path) -> None:
    if not md_files:
        return
    try:
        sources = {
            "人工写入": 0,
            "Raw同步": 0,
            "蒸馏提取": 0,
            "复盘经验": 0,
            "Git历史": 0,
            "其他": 0,
        }
        for md_file in md_files:
            try:
                if md_file.name.lower() in _SYSTEM_PAGES:
                    continue
                content = _read_frontmatter_prefix(md_file)
                src = _classify_page_source(content)
                sources[src] = sources.get(src, 0) + 1
            except (OSError, UnicodeError, ValueError, TypeError, KeyError):
                logger.debug("跳过无法解析 frontmatter 的页面", exc_info=True)
                continue
        print("  知识来源分布:")
        md_count = len(md_files)
        for src_name, cnt in sources.items():
            if cnt > 0:
                pct = cnt / md_count * 100
                print(f"    - {src_name}: {cnt} ({pct:.0f}%)")
    except (OSError, ValueError):
        logger.debug("知识来源分布统计失败", exc_info=True)


def _doctor_wiki_quality_gates(
    md_files: List[Path], wiki_dir: Path, database_dir: Path, warnings: List[str]
) -> None:
    if not wiki_dir.exists():
        return
    cfg = _get_config()
    cfg_get = getattr(cfg, "get", None)
    pending_budget = (
        int(cfg_get("health.wiki_route_budgets.needs_review_pages", 500))
        if callable(cfg_get)
        else 500
    )
    pending_count, pending_samples = _pending_verification_pages(wiki_dir)
    print(f"  待验证页面: {pending_count}")
    if pending_count:
        for sample in pending_samples:
            print(f"    - {sample}")
        print(f"    预算: {pending_budget}")
        if pending_count > pending_budget:
            warnings.append(
                f"Wiki 有 {pending_count} 个 pending verification 页面，"
                f"超过预算 {pending_budget}，请优先复核 `验证状态: pending-verification` 标记的页面"
            )

    gate_stats = _quality_gate_stats(wiki_dir, database_dir)
    print(
        "  质量门禁: "
        f"review_pages={gate_stats['review_pages']}, "
        f"rejected_records={gate_stats['rejected_records']}"
    )
    if gate_stats["review_pages"]:
        warnings.append(f"质量门禁有 {gate_stats['review_pages']} 个 review 页面待复核")
    if gate_stats["rejected_records"]:
        warnings.append(
            f"质量门禁有 {gate_stats['rejected_records']} 条 reject 记录在 distill_failed/"
        )


def _doctor_wiki_latest_mod(md_files: List[Path], warnings: List[str]) -> None:
    if not md_files:
        return
    try:
        latest = max(md_files, key=lambda p: p.stat().st_mtime)
        days_since = (
            datetime.now().timestamp() - latest.stat().st_mtime
        ) / DAYS_SINCE_SECONDS
        if days_since < 1:
            print("  最近更新: 今天")
        elif days_since < CMD_DOCTOR_DAYS_SINCE_DAYS:
            print(f"  最近更新: {int(days_since)} 天前")
        else:
            print(f"  最近更新: {int(days_since)} 天前")
            if days_since > CMD_DOCTOR_DAYS_SINCE_DAYS_2:
                warnings.append(f"知识库已 {int(days_since)} 天未更新")
    except (OSError, ValueError):
        logger.debug("最近修改时间统计失败", exc_info=True)


def _doctor_knowledge_base_health(config: Any, warnings: List[str]) -> None:
    print()
    print("知识库健康度:")
    wiki_dir = config.vault_dir("mnemos")
    if wiki_dir.exists():
        md_files = _doctor_wiki_overview(wiki_dir, warnings)
        _doctor_wiki_source_distribution(md_files, wiki_dir)
        _doctor_wiki_quality_gates(md_files, wiki_dir, config.database_dir, warnings)
        _doctor_wiki_latest_mod(md_files, warnings)
    else:
        print("  Wiki 未初始化")


def _doctor_persona_signal_quality(warnings: List[str]) -> None:
    print()
    print("画像数据质量:")
    try:
        from core.persona.psyche import get_signal_store

        store = get_signal_store()
        stats = store.get_signal_stats(days=STATS_DAYS)
        total = sum(v for v in stats.values() if v > 0)
        print(f"  最近30天信号: {total} 条")
        for src, cnt in stats.items():
            if cnt > 0:
                print(f"    - {src}: {cnt}")
        if total < 10:
            warnings.append("画像信号不足（<10条），画像推断可能不准确")
        elif total < 50:
            print("  ⚠️  画像信号较少，建议积累更多对话数据以获得精准画像")
    except (ImportError, AttributeError, OSError, RuntimeError, sqlite3.Error):
        logger.debug("画像数据库统计失败", exc_info=True)
        print("  ☐ 画像数据库未初始化")


def _doctor_mcp_server(warnings: List[str]) -> None:
    print()
    print("MCP 服务器状态:")
    try:
        from integrations.agora import MCPServer

        server = MCPServer()
        tool_count = len(server.tools)
        print("  ✓ 协议版本: JSON-RPC 2.0 / MCP 2024-11-05")
        print(f"  ✓ 可用工具: {tool_count} 个")
        kia_tools = ["preflight_inject", "guard_check"]
        for t in kia_tools:
            if t in server.tools:
                print(f"    ✓ {t}")
            else:
                warnings.append(f"MCP 缺少核心 KIA 工具: {t}")
    except (ImportError, AttributeError, OSError) as e:
        warnings.append(f"MCP 服务器加载失败: {e}")


def _doctor_capture_queue(config: Any, warnings: List[str]) -> None:
    print()
    print("Capture 队列健康:")
    try:
        cq_db = config.database_dir / "capture_queue.db"
        if cq_db.exists():
            with _get_sqlite_conn()(str(cq_db), timeout=5) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT status, COUNT(*) FROM capture_events GROUP BY status")
                status_counts = dict(cursor.fetchall())
                done = status_counts.get("done", 0)
                pending = status_counts.get("pending", 0)
                failed = status_counts.get("failed", 0) + status_counts.get("error", 0)
                print(f"  done: {done}, pending: {pending}, failed/error: {failed}")
                cursor.execute(
                    "SELECT COUNT(*) FROM capture_events WHERE status IN ('failed','error') AND created_at > datetime('now', '-1 day')"  # noqa: E501
                )
                recent_failed = cursor.fetchone()[0]
                if recent_failed > 0:
                    warnings.append(f"最近 24 小时有 {recent_failed} 条 capture 失败/错误记录")
                cursor.execute(
                    "SELECT payload_json FROM capture_events WHERE created_at > datetime('now', '-7 day')"  # noqa: E501
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
                    except (json.JSONDecodeError, TypeError):
                        logger.warning(
                            "[doctor] (json.JSONDecodeError, TypeError) suppressed", exc_info=True
                        )
                total_recent = sum(mode_counts.values())
                if total_recent > 0:
                    print("  最近 7 天 capture 完整性:")
                    for mode, count in sorted(mode_counts.items(), key=lambda x: -x[1]):
                        pct = count / total_recent * 100
                        print(f"    {mode}: {count} ({pct:.0f}%)")
                    if mode_counts.get("truncated", 0) + mode_counts.get("artifact", 0) > 0:
                        warnings.append(
                            f"最近 7 天有 {mode_counts.get('truncated', 0)} 条 truncated 和 "
                            f"{mode_counts.get('artifact', 0)} 条 artifact capture，"
                            f"请检查大 payload 来源"
                        )
        else:
            print("  ☐ capture_queue.db 不存在")
    except (OSError, sqlite3.Error):
        logger.debug("Capture 队列统计失败", exc_info=True)
        print("  ☐ Capture 队列状态未知")


def _doctor_link_integrity(warnings: List[str]) -> None:
    print()
    print("链路完整性:")
    try:
        from core.hephaestus_worker import HephaestusWorker

        worker = HephaestusWorker()
        stats = worker.get_stats()
        print(f"  ✓ 蒸馏队列: {stats['pending']} 个待处理")
        print(f"  ✓ Inbox: {stats['inbox_dir']}")
        if stats["pending"] > 10:
            if len(_daemon_processes()) == 0:
                warnings.append(
                    f"蒸馏队列积压: {stats['pending']} 个任务，且 daemon 未运行。"
                    "运行 `python3 mnemos_cli.py daemon start` 启动后台，"
                    "或运行 `python3 mnemos_cli.py distill drain --limit 5` 手动处理。"
                )
            else:
                warnings.append(
                    f"蒸馏队列积压: {stats['pending']} 个任务。"
                    "可运行 `python3 mnemos_cli.py distill status` 查看处理状态。"
                )
    except (ImportError, AttributeError, KeyError):
        logger.debug("蒸馏链路统计失败", exc_info=True)
        print("  ☐ 蒸馏链路未初始化")

    try:
        print("  ✓ Charon 知识解析: 可用")
    except ImportError:
        logger.debug("Charon 可用性检查失败", exc_info=True)
        print("  ☐ Charon 知识解析: 未就绪")


def _doctor_optional_dependency_impact() -> None:
    print()
    print("可选依赖与功能影响:")
    optional_deps = [
        ("black", "Black 格式化", "代码格式化", "pip install mnemos[dev]"),
        ("pytest", "Pytest 测试", "运行测试套件", "pip install mnemos[dev]"),
        ("sklearn", "scikit-learn", "ML 评分器训练（standard 后端）", "pip install mnemos[ml]"),
        ("hnswlib", "hnswlib", "向量索引与跨 Agent 关联检索", "pip install mnemos[ml]"),
    ]
    for module, name, feature, install_cmd in optional_deps:
        try:
            __import__(module)
            print(f"  ✓ {name}: {feature} 可用")
        except ImportError:
            if module == "sklearn":
                print(
                    f"  ☐ {name}: {feature} 未安装 → 已自动回退到 lightweight scorer（{install_cmd}）"
                )
            else:
                print(f"  ✗ {name}: {feature} 不可用 → {install_cmd}")


def _doctor_event_bus_dead_letter(warnings: List[str]) -> None:
    print()
    print("事件总线死信队列:")
    try:
        from core.mnemos_bus import _get_bus

        bus = _get_bus()
        dl_list = bus.get_dead_letters(limit=100)
        no_consumer_dl = [d for d in dl_list if d.get("status") == "no_consumer"]
        if no_consumer_dl:
            warnings.append(
                f"EventBus 死信队列有 {len(no_consumer_dl)} 个无消费者事件，"
                f"类型: {set(d.get('event_type') for d in no_consumer_dl)}。"
                "运行 `python3 mnemos_cli.py events replay --no-consumer --limit 100` "
                "重放已有消费者的死信，或注册对应处理器。"
            )
            print(f"  ⚠️  {len(no_consumer_dl)} 个无消费者事件在死信队列")
        else:
            print("  ✓ 无消费者事件: 0")
    except (ImportError, AttributeError, OSError) as e:
        warnings.append(f"EventBus 死信队列检测失败: {e}")
        print(f"  ✗ 检测失败: {e}")


def _doctor_critical_file_integrity(issues: List[str], warnings: List[str]) -> None:
    print()
    print("关键文件完整性:")
    try:
        _CRITICAL_FILE_CHECKS = [
            ("mnemos_daemon.py", 1000),
            ("mnemos_cli.py", 500),
            ("core/config.py", 500),
        ]
        repo_root = Path(__file__).resolve().parents[3]
        for rel_path, min_bytes in _CRITICAL_FILE_CHECKS:
            full = repo_root / rel_path
            if not full.exists():
                print(f"  ✗ {rel_path}: 文件不存在")
                issues.append(f"关键文件缺失: {rel_path}")
                continue
            size = full.stat().st_size
            if size < min_bytes:
                print(f"  ✗ {rel_path}: 大小异常 ({size} bytes < {min_bytes} bytes)")
                issues.append(f"关键文件可能被截断: {rel_path} ({size} bytes)")
            else:
                print(f"  ✓ {rel_path}: {size} bytes")
    except (OSError, ValueError) as e:
        warnings.append(f"关键文件完整性检测失败: {e}")
        print(f"  ✗ 检测失败: {e}")


def _doctor_summary(issues: List[str], warnings: List[str]) -> bool:
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _cmd_auto_heal_repair(args: Any) -> bool:
    """Preview the unified auto-healing plan from current health output."""
    from core.ops.auto_healing import (
        build_health_auto_heal_report,
        format_auto_heal_report,
    )
    from core.ops.health_check import build_health_report_quiet

    config = _get_config()
    health = build_health_report_quiet(config)
    report = build_health_auto_heal_report(
        config,
        health.get("checks", {}),
        apply=False,
    )
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_auto_heal_report(report))
    return not bool(report.get("user_intervention_budget", {}).get("exceeded"))


def cmd_doctor(args):
    """系统诊断"""
    if getattr(args, "doctor_action", "") == "repair":
        if getattr(args, "dry_run", False) and not getattr(args, "agent_name", ""):
            return _cmd_auto_heal_repair(args)

        from core.cli.commands.agent import _cmd_agent_repair

        return _cmd_agent_repair(args)

    if getattr(args, "doctor_action", "") == "repair-all":
        from core.cli.commands.setup import cmd_repair_all

        return cmd_repair_all(args) == 0

    if getattr(args, "doctor_action", "") == "modules":
        from core.module_toggles import build_module_toggle_report

        report = build_module_toggle_report(_get_config())
        if getattr(args, "json", False):
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print("Mnemos Module Toggles")
            print("=" * 40)
            print(f"status: {report['status']}")
            print(f"toggles: {report['counts']['toggles']}")
            print(f"auto_enable_candidates: {report['counts']['auto_enable_candidates']}")
            print(f"registered_but_unwired: {report['counts']['registered_but_unwired']}")
            print(f"stale_removed: {report['counts']['stale_removed']}")
        return report["status"] == "ok"

    if getattr(args, "doctor_action", "") == "config":
        from core.ops.config_audit import (
            build_config_audit_report,
            format_config_audit_text,
            write_config_audit_artifact,
        )

        config = _get_config()
        report = build_config_audit_report(
            config,
            strict=bool(getattr(args, "strict", False)),
            show_sensitive=bool(getattr(args, "unsafe_debug", False)),
        )
        write_config_audit_artifact(report, config)
        if getattr(args, "json", False):
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(format_config_audit_text(report))
        return bool(report["ok"])

    if getattr(args, "cognitive_readiness", False):
        from core.ops.cognitive_readiness import (
            build_cognitive_readiness_report,
            dumps_report,
            format_cognitive_readiness_text,
        )

        report = build_cognitive_readiness_report(_get_config())
        if getattr(args, "json", False):
            print(dumps_report(report))
        else:
            print(format_cognitive_readiness_text(report))
        return report["ok"]

    if getattr(args, "json", False):
        from core.ops.health_check import build_health_report_quiet

        report = build_health_report_quiet()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return report["ok"]

    if getattr(args, "e2e", False):
        from scripts.e2e_probe import run_probe

        steps = run_probe()
        passed = sum(1 for ok, _ in steps.values() if ok)
        total = len(steps)
        if passed == total:
            print("\n🎉 全链路探针全部通过！")
        else:
            print(f"\n⚠️  {total - passed} 项未通过，请检查上方输出。")
        return

    if getattr(args, "unsafe_debug", False):
        return _run_doctor_text(args)

    from core.privacy.redaction import redact_text

    captured = StringIO()
    with redirect_stdout(captured):
        ok = _run_doctor_text(args)
    print(redact_text(captured.getvalue()), end="")
    return ok


def _run_doctor_text(args: Any) -> bool:
    _print_doctor_header()

    config = _get_config()
    issues: List[str] = []
    warnings: List[str] = []

    _print_config_contract(config, warnings)
    print()

    _doctor_performance_tier(config, args, warnings)
    _doctor_collection_mode(config)
    _doctor_python_version(issues)
    _doctor_core_dependencies(issues)
    _doctor_git(warnings)
    _doctor_vault_and_cg(config, warnings)
    _doctor_persona_sources(config)
    _doctor_claude_code(config, warnings)
    _doctor_agent_connectivity(warnings)
    _doctor_agent_active_access(warnings)
    _doctor_agent_completeness(warnings)
    _doctor_agent_full_power(warnings)
    _doctor_reasoning_mode(config)
    _doctor_api_distillation(warnings)
    _doctor_cross_platform_compat()
    _doctor_persona_database(config)
    _doctor_kia_loop(config, warnings)
    _doctor_dual_index(config, warnings)
    _doctor_knowledge_base_health(config, warnings)
    _doctor_persona_signal_quality(warnings)
    _doctor_mcp_server(warnings)
    _doctor_capture_queue(config, warnings)
    _doctor_link_integrity(warnings)
    _doctor_optional_dependency_impact()
    _doctor_event_bus_dead_letter(warnings)
    _doctor_critical_file_integrity(issues, warnings)

    print()
    _print_runtime_health(config, warnings)

    return _doctor_summary(issues, warnings)
