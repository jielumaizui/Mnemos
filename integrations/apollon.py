#!/usr/bin/env python3
# Apollon — 阿波罗/预言之神 — Claude Code 专属适配
# 原模块: claude_integration.py

"""
Claude Code 适配器

意图判定规则：
1. 【上下文回忆类】→ 仅读取 L1 存储
   - 历史对话、过往沟通细节、会话接续、任务复盘
   - 关键词：上次、之前、刚才、回忆、继续、复盘

2. 【知识查询类】→ 自动检索Wiki
   - 概念定义、架构规则、标准流程、专业知识点、既定规范
   - 关键词：是什么、如何、怎么、原理、架构、流程、规范

3. 【禁止】
   - 两类不混合滥用
   - 无意义重复检索
   - 随意交叉调用

Hook/CLI 只降级已知 I/O、配置、SQLite、存储与运行时故障；未知编程错误保持可见。
"""

import importlib
import os
import logging
import sqlite3
import time
import shutil
import tempfile
from dataclasses import asdict

logger = logging.getLogger(__name__)
import sys  # noqa: E402
import json  # noqa: E402
import argparse  # noqa: E402
from pathlib import Path  # noqa: E402

# 确保从任意工作目录执行时都能找到 core 模块
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
from datetime import datetime, timedelta, timezone  # noqa: E402
from typing import Any, Dict, List, Optional, Tuple  # noqa: E402

try:
    import fcntl
except (OSError, ValueError, TypeError, ImportError, AttributeError):  # pragma: no cover - Windows 没有 fcntl
    fcntl = None  # type: ignore[assignment]


def _import_optional_class(module_path: str, class_name: str):
    """尝试导入可选模块中的类；缺失时返回 None 而不是让调用方执行 None()。"""
    from core.import_guard import assert_allowed_module

    try:
        assert_allowed_module(module_path)
        module = importlib.import_module(module_path)
        return getattr(module, class_name)
    except (OSError, ValueError, TypeError, ImportError, AttributeError):
        logger.debug("可选模块未加载: %s.%s", module_path, class_name, exc_info=True)
        return None


from integrations.active import (  # noqa: E402
    json_mcp_configured,
    upsert_json_mcp_server,
    write_active_context,
)
from integrations.preflight_builder import (  # noqa: E402
    build_kia_section,
    build_l1_section,
    build_lightweight_preflight,
    build_observation_section,
    build_persona_section,
    build_predictive_push_section,
    build_wiki_section,
    _guard_state_file,
)
from integrations.apollon_context import (  # noqa: E402,F401
    ContextProviders,
    IntentClassifier,
    QueryIntent,
    build_context_for_agent,
    detect_private_keywords,
)
from integrations.thread_call import run_daemon_call as _run_daemon_call  # noqa: E402

# Knowledge-in-Action 闭环系统
from core.kia.dike import TaskClassifier  # noqa: E402
from core.kia.prophasis import PreFlightInjector  # noqa: E402
from core.kia.epimetheus import generate_retrospective, should_retrospect  # noqa: E402
from core.kia.proteus import IterationTracker  # noqa: E402
from core.kia.chronos import KnowledgeScheduler  # noqa: E402

# 用户画像闭环系统
from core.persona.psyche import get_signal_store, SessionSignal, log_session_signal  # noqa: E402
from core.persona.daimon import SignalCollector  # noqa: E402
from core.persona.pythia import PreferenceAnalyzer  # noqa: E402,F401  # test patch anchor
from core.persona.delphi import PersonaStore  # noqa: E402
from core.persona.hamartia import BlindSpotProfileManager  # noqa: E402,F401  # test patch anchor
from core.config import get_config  # noqa: E402
from core.sync_framework.storage_backend import StorageError  # noqa: E402


APOLLON_OPERATION_ERRORS = (
    ImportError,
    KeyError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
    sqlite3.Error,
    StorageError,
)


def get_wiki_knowledge(user_message: str, agent: str = "claude") -> Optional[str]:
    """【知识查询类】专用 - 检索Wiki知识（热力值控制深度）。"""
    result = build_wiki_section(user_message, mode="deep", agent=agent)
    return result if result else None


def get_l1_context(
    working_dir: str,
    authorize_cross: List[str] | None = None,
    agent: str = "claude",
) -> str:
    """【上下文回忆类】专用 - 读取 StorageBackend 历史记录。"""
    return build_l1_section(working_dir, agent, authorize_cross)


def _get_persona_behavior_prompt(working_dir: str | None = None) -> str:
    """根据用户画像生成行为策略提示（统一使用 build_persona_section）。"""
    return build_persona_section("claude", working_dir=working_dir or "")


def get_context_for_agent(
    agent: str,
    working_dir: str | None = None,
    user_message: str | None = None,
    authorize_cross: List[str] | None = None,
    mode: str | None = None,
) -> str:
    """Build intent-routed preflight context through the extracted core."""
    resolved_working_dir = working_dir or os.getcwd()
    resolved_mode = mode or get_config().get("preflight.mode", "full")
    providers = ContextProviders(
        classify_intent=IntentClassifier.classify,
        detect_private_keywords=detect_private_keywords,
        get_l1_context=get_l1_context,
        get_wiki_knowledge=get_wiki_knowledge,
        load_knowledge_in_action=load_knowledge_in_action,
        build_lightweight_preflight=build_lightweight_preflight,
        build_predictive_push_section=(
            lambda user_message: build_predictive_push_section(
                user_message,
                agent=agent,
            )
        ),
        build_observation_section=(
            lambda: build_observation_section(agent=agent)
        ),
        get_persona_behavior_prompt=_get_persona_behavior_prompt,
        build_persona_section=build_persona_section,
    )
    return build_context_for_agent(
        agent=agent,
        working_dir=resolved_working_dir,
        user_message=user_message or "",
        authorize_cross=authorize_cross,
        mode=str(resolved_mode).lower(),
        providers=providers,
    )


def get_context_for_claude(
    working_dir: str | None = None,
    user_message: str | None = None,
    authorize_cross: List[str] | None = None,
) -> str:
    """Claude Code 兼容入口，代理到 :func:`get_context_for_agent`。"""
    return get_context_for_agent("claude", working_dir, user_message, authorize_cross)


def load_knowledge_in_action(user_message: str) -> str:
    """Knowledge-in-Action 闭环系统 - 会话开始时装载历史经验。"""
    return build_kia_section(user_message, mode="full")


def _load_guard_state() -> Optional[Dict]:
    """加载 Guard 会话状态"""
    try:
        if _guard_state_file().exists():
            # type: ignore[no-any-return]
            return json.loads(_guard_state_file().read_text(encoding="utf-8"))  # type: ignore[no-any-return]  # noqa: E501
    except APOLLON_OPERATION_ERRORS as e:
        logger.warning("加载 Guard 状态失败: %s", e)
    return None


def _build_checklist_usage_from_guard(
    messages: List[Dict], task_type: str, subtype: str
) -> List[Dict]:
    """
    基于消息历史和 Guard 状态构建 checklist 使用情况
    在 session_end 时调用，模拟 Guard 检查整个对话历史
    """
    usage = []

    # 1. 尝试加载历史 Guard 状态
    state = _load_guard_state()
    if state:
        # 任务类型匹配才使用
        if state.get("task_type") == task_type and state.get("subtype") == subtype:
            # 从状态恢复告警记录
            for alert in state.get("triggered_alerts", []):
                usage.append(
                    {
                        "item": alert["item"],
                        "loaded": True,
                        "used": True,
                        "triggered": alert["level"] in ("interrupt", "hint"),
                        "level": alert["level"],
                        "severity": "high",  # 被触发的通常级别较高
                        "reason_ignored": "",
                    }
                )
            for record in state.get("silent_records", []):
                usage.append(
                    {
                        "item": record["item"],
                        "loaded": True,
                        "used": True,
                        "triggered": False,
                        "level": "silent",
                        "severity": record.get("severity", "medium"),
                        "reason_ignored": "",
                    }
                )

    # 2. 如果没有历史状态，基于消息内容做简化推断
    if not usage:
        # 从消息中提取是否提到了 checklist 相关操作
        all_text = " ".join([m.get("content", "") for m in messages])
        # 简化：如果消息中提到了"已注意""已检查"等，认为 checklist 被使用了
        if "注意" in all_text or "检查了" in all_text or "确认" in all_text:
            usage.append(
                {
                    "item": "用户声明已注意风险",
                    "loaded": True,
                    "used": True,
                    "triggered": False,
                    "level": "none",
                    "severity": "medium",
                    "reason_ignored": "",
                }
            )

    return usage


# ========== 用户画像闭环 ==========

PERSONA_MIN_SIGNALS = 10  # 最小信号数才分析
PERSONA_MIN_DAYS = 7  # 最少间隔天数


def _extract_user_metrics(messages: List[Dict]) -> tuple[float, int, int]:
    """提取用户消息平均长度、纠正次数、追问深度。"""
    user_msgs = [m for m in messages if m.get("role") == "user"]
    user_contents = [m.get("content", "") for m in user_msgs]
    avg_len = sum(len(c) for c in user_contents) / max(len(user_contents), 1)

    correction_keywords = ["不对", "错了", "不是", "应该", "换个", "不对，"]
    correction_count = sum(
        1 for c in user_contents if any(kw in c for kw in correction_keywords)
    )

    follow_up_depth = 0
    for i, msg in enumerate(messages):
        if msg.get("role") == "user" and i > 0:
            prev = messages[:i]
            if any(m.get("role") == "assistant" for m in prev):
                follow_up_depth += 1

    return avg_len, correction_count, follow_up_depth


def _infer_termination_type(messages: List[Dict]) -> str:
    """基于最后一条用户消息推断终止类型。"""
    last_user = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user = m.get("content", "").lower()
            break

    if any(kw in last_user for kw in ["好的", "完美", "可以", "ok", "谢谢", "搞定了"]):
        return "satisfied"
    if any(kw in last_user for kw in ["开始吧", "执行", "推进", "下一步", "继续"]):
        return "progress"
    if any(kw in last_user for kw in ["你决定", "你来", "按你的"]):
        return "delegated"
    if any(kw in last_user for kw in ["算了", "放弃", "不做了", "先这样吧"]):
        return "abandoned"
    return "unknown"


def _infer_output_type(messages: List[Dict]) -> str:
    """基于消息内容推断产出类型。"""
    all_text = " ".join(m.get("content", "") for m in messages)
    if "```" in all_text or "def " in all_text or "class " in all_text:
        return "code"
    if "# " in all_text and len(all_text) > 500:
        return "document"
    return "discussion"


def _derive_session_id(messages: List[Dict], working_dir: str, session_id: str | None) -> str:
    """优先使用外部传入的 session_id，否则用内容 hash 生成。"""
    if session_id:
        return session_id
    import hashlib

    all_text = " ".join(m.get("content", "") for m in messages)
    content_hash = hashlib.md5(all_text.encode(), usedforsecurity=False).hexdigest()[:16]
    dir_hash = hashlib.md5(
        (working_dir or os.getcwd()).encode(), usedforsecurity=False
    ).hexdigest()[:8]
    return f"{dir_hash}:{content_hash}"


def _build_session_signal(
    messages: List[Dict],
    working_dir: str,
    task_type: str,
    task_subtype: str,
    session_id: str | None,
) -> "SessionSignal":
    """根据消息内容构造 SessionSignal。"""
    user_msgs = [m for m in messages if m.get("role") == "user"]
    avg_len, correction_count, follow_up_depth = _extract_user_metrics(messages)
    termination_type = _infer_termination_type(messages)
    output_type = _infer_output_type(messages)
    sid = _derive_session_id(messages, working_dir, session_id)

    return SessionSignal(
        session_id=sid,
        timestamp=datetime.now().isoformat(),
        task_type=task_type,
        task_subtype=task_subtype,
        user_msg_count=len(user_msgs),
        avg_user_msg_length=avg_len,
        correction_count=correction_count,
        follow_up_depth=follow_up_depth,
        termination_type=termination_type,
        output_type=output_type,
        working_dir=working_dir or os.getcwd(),
        agent="claude",
    )


def _collect_session_signal(
    messages: List[Dict],
    working_dir: str,
    task_type: str = "",
    task_subtype: str = "",
    session_id: str | None = None,
) -> int:
    """
    从本次会话提取行为信号并入库。
    在 session_end 时调用。

    Args:
        session_id: 外部传入的统一 session_id（如 JSONL stem）。
                    若未提供，回退到 hash 生成的内部 session_id。
    """
    if not messages:
        return 0

    try:
        if not any(m.get("role") == "user" for m in messages):
            return 0

        signal = _build_session_signal(
            messages, working_dir, task_type, task_subtype, session_id
        )

        # 盲区反馈闭环：分析用户对挑战的反应
        _analyze_blindspot_feedback(messages)

        log_session_signal(**asdict(signal))
        return 1
    except APOLLON_OPERATION_ERRORS as e:
        logger.warning("[Persona] Session signal collection failed: %s", e, exc_info=True)
        return 0


def _analyze_blindspot_feedback(_messages: List[Dict]) -> Dict[str, object]:
    """Reject transcript inference; feedback requires an exact delivery API call."""

    return {
        "status": "noop",
        "reason": "exact_delivery_feedback_required",
        "recorded": 0,
    }


def _should_analyze_persona() -> bool:
    """检查是否应该触发画像分析（频率控制）。"""
    try:
        store = get_signal_store()
        stats = store.get_signal_stats(days=30)
        total = sum(v for v in stats.values() if v > 0)

        if total < PERSONA_MIN_SIGNALS:
            return False

        # 检查上次分析时间
        latest = store.get_latest_persona_version()
        if latest and latest.get("generated_at"):
            try:
                last = datetime.fromisoformat(latest["generated_at"].replace("Z", "+00:00"))
                days_since = (datetime.now(timezone.utc) - last).days
                if days_since < PERSONA_MIN_DAYS:
                    return False
            except (TypeError, ValueError) as e:
                logger.warning("日期解析失败: %s", e)

        return True
    except APOLLON_OPERATION_ERRORS as e:
        logger.warning("检查画像分析触发条件失败: %s", e)
        return False


def _run_persona_cycle() -> str:
    """Keep session-end hooks observation-only.

    Persona persistence is owned solely by the daemon application command;
    this hook has neither a sealed signal batch nor a decision context for a
    blindspot admission.
    """

    return "[Persona] deferred to daemon canonical revision command"


def run_retrospective(messages_json: str) -> str:
    """
    Knowledge-in-Action 闭环系统 - 会话结束时自动复盘

    Args:
        messages_json: JSON格式的会话消息列表

    Returns:
        复盘结果文本
    """
    if not messages_json:
        return ""

    try:
        messages = json.loads(messages_json)
        if not messages or not should_retrospect(messages):
            return ""

        classifier = TaskClassifier()
        _ = PreFlightInjector()
        tracker = IterationTracker()

        # 识别任务类型
        result = classifier.classify(messages)
        if result.confidence < 0.7:
            return ""

        task_type = result.task_type
        subtype = result.subtype

        # 获取装载的 checklist 使用情况（基于 Guard 历史状态）
        checklist_usage = _build_checklist_usage_from_guard(messages, task_type, subtype)

        # 生成复盘
        retro_result = generate_retrospective(task_type, subtype, messages, checklist_usage)

        # 创建新版本
        new_path = tracker.create_next_version(retro_result)  # type: ignore[attr-defined]

        if new_path:
            return (
                f"\n[KIA] 复盘已生成: {new_path}\n"
                f"       新增 {len(retro_result.new_lessons)} 条教训\n"
                f"       版本: v{retro_result.version}\n"
            )

        return ""

    except APOLLON_OPERATION_ERRORS as e:
        logger.warning("[KIA] 复盘失败: %s", e, exc_info=True)
        return ""


def _run_cognitive_decision_flywheel(results: List[str]) -> None:
    """运行认知决策飞轮，加载画像并汇总认知决策资产候选。"""
    CognitiveDecisionFlywheel = _import_optional_class("core.kia.ixion", "CognitiveDecisionFlywheel")  # noqa: E501
    if CognitiveDecisionFlywheel is None:
        results.append("认知决策飞轮: 模块未安装/已移除")
        return

    try:
        # 加载用户画像并传入飞轮，启用画像驱动闭环
        profile, blindspot = None, None
        try:
            profile, blindspot = PersonaStore().load_persona()
        except (OSError, ValueError, TypeError, ImportError, AttributeError, RuntimeError):
            logger.debug("[apollon] 加载画像失败，飞轮将使用默认画像", exc_info=True)

        flywheel = CognitiveDecisionFlywheel(persona=profile, blindspot=blindspot)
        flywheel_results = flywheel.run_cycle()
        assets = flywheel_results.get(
            "wiki_to_cognitive_decision", []
        ) + flywheel_results.get("behavior_to_cognitive_decision", [])
        persona_driven = flywheel_results.get("persona_driven", {})
        executed = flywheel_results.get("executed", {})
        report_path = flywheel_results.get("report_path", "")

        summary_parts = []
        if assets:
            summary_parts.append(f"{len(assets)} 个认知决策资产候选")
        if persona_driven:
            summary_parts.append("画像驱动分析已执行")
        if executed.get("count", 0):
            summary_parts.append(f"{executed['count']} 项自动操作")
        if report_path:
            summary_parts.append(f"报告: {Path(report_path).name}")

        if summary_parts:
            results.append(f"认知决策飞轮: {', '.join(summary_parts)}")
        else:
            results.append("认知决策飞轮: 无新资产候选")
    except APOLLON_OPERATION_ERRORS as e:
        results.append(f"认知决策飞轮: 失败 ({e})")


def run_kia_cycles_light():
    """KIA Orchestrator 超轻量周期任务（session_end hook 专用）

    Hook 有执行时间限制，只运行最关键且轻量的子系统：
    1. 关联周期（dry_run 模式，只分析不写入）
    2. 调度提醒（只检查到期，不执行全量扫描）
    3. 用户画像（仅检查条件，满足才触发）
    """
    print("[KIA-Orchestrator-Light] 启动轻量周期...")
    results = []

    # 1. 轻量关联（dry_run）
    try:
        try:
            from core.kia.charon import run_connect_cycle
        except ImportError:
            run_connect_cycle = None
        if run_connect_cycle:  # type: ignore[truthy-function]
            timed_out, stats = _run_daemon_call(
                lambda: run_connect_cycle(dry_run=True),
                timeout=30,
            )
            if timed_out:
                results.append("关联(dry): 超时跳过")
            else:
                results.append(f"关联(dry): {stats.get('pages_processed', 0)} 页待处理")
        else:
            results.append("关联(dry): 模块不可用")
    except APOLLON_OPERATION_ERRORS as e:
        results.append(f"关联(dry): 失败 ({e})")

    # 2. 调度提醒
    try:
        scheduler = KnowledgeScheduler()
        pending = scheduler.get_pending_reminders()
        results.append(f"调度: {len(pending)} 个提醒")
    except APOLLON_OPERATION_ERRORS as e:
        results.append(f"调度: 失败 ({e})")

    # 3. 用户画像（仅条件检查）
    try:
        if _should_analyze_persona():
            timed_out, persona_result = _run_daemon_call(
                _run_persona_cycle,
                timeout=60,
            )
            if timed_out:
                persona_result = "画像: 分析超时，跳过"
            first_line = persona_result.split("\n")[0] if persona_result else "画像: 无输出"
            results.append(first_line.replace("[Persona] ", "画像: "))
        else:
            results.append("画像: 跳过")
    except APOLLON_OPERATION_ERRORS as e:
        results.append(f"画像: 失败 ({e})")

    print(f"[KIA-Orchestrator-Light] 完成: {' | '.join(results)}")


def show_stats():
    """显示 Mnemos v2.0.0 系统统计"""
    from core.kia.proteus import IterationTracker
    from core.kia.chronos import KnowledgeScheduler

    WIKI_DIR = get_config().wiki_dir

    print("=" * 50)
    print("Mnemos v2.0.0 系统统计")
    print("=" * 50)

    # 1. Wiki 文件统计
    if WIKI_DIR.exists():
        for subdir in [
            "00-Inbox",
            "01-People",
            "02-Projects",
            "03-Tech",
            "04-Concepts",
            "05-MOCs",
            "retrospectives",
        ]:
            path = WIKI_DIR / subdir
            if path.exists():
                count = len(list(path.rglob("*.md")))
                print(f"  {subdir}/: {count} 个文件")

    # 2. 知识状态统计
    try:
        tracker = IterationTracker()
        stats = tracker.get_stats()
        print("\n知识状态统计:")
        print(f"  总知识条目: {stats['total']}")
        print(f"  P序列分布: {stats['p_distribution']}")
        print(f"  L序列分布: {stats['l_distribution']}")
    except APOLLON_OPERATION_ERRORS as e:
        print(f"\n知识状态统计: 获取失败 ({e})")

    # 3. 调度任务统计
    try:
        scheduler = KnowledgeScheduler()
        tasks = scheduler.list_all()
        status_count = {}
        for t in tasks:
            status_count[t.status] = status_count.get(t.status, 0) + 1
        print("\n调度任务统计:")
        for status, count in sorted(status_count.items()):
            print(f"  {status}: {count}")
    except APOLLON_OPERATION_ERRORS as e:
        print(f"\n调度任务统计: 获取失败 ({e})")

    print()


def _get_session_id_from_jsonl(working_dir: str) -> str:
    """从 Claude Code JSONL 文件名提取 session uuid，用于和 LiveSync 统一 session_id。"""
    try:
        from pathlib import Path

        wd = Path(working_dir).resolve()
        project_name = "-" + str(wd).lstrip("/").replace("/", "-")
        projects_dir = get_config().claude_data_dir / "projects"

        candidate_dirs = [projects_dir / project_name]
        if not candidate_dirs[0].exists():
            for child in projects_dir.iterdir():
                if child.is_dir() and project_name.startswith(child.name):
                    candidate_dirs.append(child)

        all_jsonls = []  # type: ignore[var-annotated]
        for proj_dir in candidate_dirs:
            if proj_dir.exists():
                all_jsonls.extend(proj_dir.glob("*.jsonl"))

        if not all_jsonls:
            return ""

        latest = max(all_jsonls, key=lambda p: p.stat().st_mtime)
        # 文件名即 uuid，如 e8b8161d-41ae-4963-a0ff-6a23bd831ea6.jsonl
        return f"claude:{latest.stem}"
    except (OSError, ValueError, TypeError, ImportError, AttributeError):  # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
        return ""


def _write_sync_trigger(working_dir: str) -> Optional[Path]:
    """写入 sync trigger 文件，通知 LiveSync 立即处理该 session。"""
    try:
        triggers_dir = get_config().data_dir / "claude_sync_triggers"
        triggers_dir.mkdir(parents=True, exist_ok=True)

        sid = _get_session_id_from_jsonl(working_dir)
        if not sid:
            return None

        trigger_path = triggers_dir / f"{sid.replace(':', '_')}.trigger"
        trigger_path.write_text(datetime.now().isoformat(), encoding="utf-8")
        return trigger_path
    except (OSError, ValueError, TypeError, ImportError, AttributeError):  # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
        return None


def _get_latest_jsonl(working_dir: str) -> Optional[Path]:
    """找到对应工作目录的最新 JSONL 文件路径。"""
    try:
        from pathlib import Path

        wd = Path(working_dir).resolve()
        project_name = "-" + str(wd).lstrip("/").replace("/", "-")
        projects_dir = get_config().claude_data_dir / "projects"

        candidate_dirs = [projects_dir / project_name]
        if not candidate_dirs[0].exists():
            for child in projects_dir.iterdir():
                if child.is_dir() and project_name.startswith(child.name):
                    candidate_dirs.append(child)

        all_jsonls = []  # type: ignore[var-annotated]
        for proj_dir in candidate_dirs:
            if proj_dir.exists():
                all_jsonls.extend(proj_dir.glob("*.jsonl"))

        if not all_jsonls:
            return None
        return max(all_jsonls, key=lambda p: p.stat().st_mtime)  # type: ignore[no-any-return]
    except (OSError, ValueError, TypeError, ImportError, AttributeError):  # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
        return None


def _read_session_from_jsonl(working_dir: str, max_retries: int = 3) -> List[Dict]:
    """从 Claude Code JSONL 文件中读取当前会话消息。

    Claude Code 不提供 $SESSION_MESSAGES 环境变量，但会在
    {claude_data_dir}/projects/{project}/ 下写入 JSONL 文件。
    我们找到对应工作目录的最新 JSONL 文件并解析。

    由于 JSONL 文件可能在 Hook 触发时仍在写入中，
    采用"加共享锁 + 拷贝到临时文件 + 重试"策略降低竞态风险，
    避免并发 truncate 导致整段会话丢失。
    """
    from integrations.sources.claude_source import ClaudeSource

    latest = _get_latest_jsonl(working_dir)
    if not latest:
        return []

    def _copy_latest_to_temp() -> Optional[Path]:
        """加共享锁后将文件拷贝到临时文件，返回临时路径。"""
        if not latest.exists():
            return None
        temp_fd, temp_path = tempfile.mkstemp(
            suffix=".jsonl.tmp", prefix=latest.stem + "_", dir=str(latest.parent)
        )
        try:
            with os.fdopen(temp_fd, "wb") as tmp_fout:
                with open(latest, "rb") as fin:
                    if fcntl is not None:
                        try:
                            fcntl.flock(fin.fileno(), fcntl.LOCK_SH)
                        except OSError:
                            pass
                    shutil.copyfileobj(fin, tmp_fout)
                    if fcntl is not None:
                        try:
                            fcntl.flock(fin.fileno(), fcntl.LOCK_UN)
                        except OSError:
                            pass
            return Path(temp_path)
        except (OSError, ValueError, TypeError, ImportError, AttributeError):
            try:
                os.close(temp_fd)
            except (OSError, ValueError, TypeError, ImportError, AttributeError):
                logger.debug("关闭临时文件描述符失败", exc_info=True)
            Path(temp_path).unlink(missing_ok=True)
            return None

    for attempt in range(max_retries):
        temp_path: Optional[Path] = None
        try:
            # 检查文件是否仍在增长（最后写入期间 mtime 变化说明正在写入）
            if latest.exists():
                mtime_before = latest.stat().st_mtime
                time.sleep(0.1 * (attempt + 1))
                mtime_after = latest.stat().st_mtime
                if mtime_after != mtime_before:
                    # 文件仍在写入，等待更久
                    time.sleep(0.3)

            temp_path = _copy_latest_to_temp()
            if temp_path is None:
                # 文件可能刚刚被删除或不可读，重试
                time.sleep(0.2 * (attempt + 1))
                continue

            source = ClaudeSource()
            turns = source.parse_turns(temp_path)

            # 完整性校验：JSONL 文件如果损坏通常返回空 turns
            if not turns and latest.stat().st_size > 0:
                logger.debug(
                    "JSONL 读取得到空 turns，文件大小 %s，重试 %s/%s",
                    latest.stat().st_size,
                    attempt + 1,
                    max_retries,
                )
                continue

            messages = []
            for turn in turns:
                if turn.user_content:
                    messages.append({"role": "user", "content": turn.user_content})
                if turn.assistant_content:
                    messages.append({"role": "assistant", "content": turn.assistant_content})
            return messages
        except APOLLON_OPERATION_ERRORS as e:
            logger.debug(
                "JSONL 读取尝试 %s/%s 失败: %s", attempt + 1, max_retries, e, exc_info=True
            )
            if attempt < max_retries - 1:
                time.sleep(0.2 * (attempt + 1))
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    return []


def save_session(working_dir: str | None = None, summary: str = ""):
    """保存当前会话"""
    from core.sync_framework.storage_backend import create_storage_backend

    backend = create_storage_backend()

    if working_dir is None:
        working_dir = os.getcwd()

    session_content = f"[SESSION] claude\n工作目录: {working_dir}\n摘要: {summary}"
    backend.save(session_content, tags=["type=session", "agent=claude"], title="session")
    print(f"Session saved: {working_dir}")


def _extract_last_user_message(messages: List[Dict]) -> str:
    """从消息列表中提取最后一条用户消息。"""
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content
    return ""


def _extract_session_start_time(messages: List[Dict]) -> Optional[datetime]:
    """从消息列表中提取最早时间戳。"""
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        ts = msg.get("timestamp") or msg.get("created_at")
        if ts:
            try:
                if isinstance(ts, (int, float)):
                    return datetime.fromtimestamp(ts, tz=timezone.utc)
                return datetime.fromisoformat(str(ts))
            except (ValueError, TypeError):
                continue
    return None


def _run_observation_on_session_end(messages: List[Dict]) -> Dict:
    """Session end 时增量运行 Observation Engine。"""
    result = {"ran": False, "observations": 0}
    try:
        cfg = get_config()
        if not cfg.get("observation.enabled", True):
            return result

        from core.cognitive.observation_engine import (
            ObservationEngine,
            canonical_raw_engine_kwargs,
        )

        since = _extract_session_start_time(messages)
        engine = ObservationEngine(
            wiki_dir=str(cfg.wiki_dir),
            **canonical_raw_engine_kwargs(cfg),
        )
        if since:
            batch = engine.run_incremental(since=since, persist=True)
        else:
            batch = engine.run(persist=True)
        result["ran"] = True
        observation_total = batch.total_observations
        result["observations"] = observation_total
        if observation_total:
            print(f"[Observation] 增量提取 {observation_total} 条观察")
    except APOLLON_OPERATION_ERRORS as e:
        logger.debug("[Observation] session end 运行失败: %s", e, exc_info=True)
    return result


def _run_reflection_on_session_end(messages: List[Dict]) -> Dict:
    """Session end 时按 Router 决策自动触发 Reflection。"""
    result = {"triggered": False, "route": None, "record_id": None}
    try:
        cfg = get_config()
        if not cfg.get("reflection.enabled", True):
            return result
        if not cfg.get("reflection.auto_trigger_on_session_end", True):
            return result

        from core.reflection.reflection_engine import ReflectionEngine
        from core.reflection.reflection_router import ReflectionRouter
        from core.mnemos_bus import get_event_bus

        last_msg = _extract_last_user_message(messages)
        if not last_msg:
            return result

        router = ReflectionRouter()
        route = router.route(last_msg)
        result["route"] = route.to_dict()
        if not route.should_reflect:
            logger.debug("[Reflection] Router 判定不触发: %s", route.reason)
            return result

        engine = ReflectionEngine()
        reflection_result = engine.reflect_on_user_input(last_msg)
        # 如果 Router 判定应该反射但触发器未命中，fallback 到手动反射
        if route.should_reflect and not reflection_result.triggered:
            reflection_result = engine.reflect_manually(last_msg)
        result["triggered"] = reflection_result.triggered
        if reflection_result.record:
            result["record_id"] = reflection_result.record.id  # type: ignore[assignment]

        get_event_bus().publish(
            "reflection.completed",
            payload={
                "triggered": reflection_result.triggered,
                "route": route.to_dict(),
                "record_id": reflection_result.record.id if reflection_result.record else None,
                "insight_summary": (
                    reflection_result.insight.summary if reflection_result.insight else ""
                ),
                "feedback_messages": reflection_result.feedback_messages,
            },
        )
        if reflection_result.triggered:
            print(f"[Reflection] 自动触发: {route.reason}")
    except APOLLON_OPERATION_ERRORS as e:
        logger.debug("[Reflection] session end 运行失败: %s", e, exc_info=True)
    return result


def _run_feedback_prompt_on_session_end() -> Dict:
    """Session end 时检查 pending feedback 并提示用户。"""
    result = {"pending_count": 0}
    try:
        cfg = get_config()
        if not cfg.get("feedback.enabled", True):
            return result

        from core.reflection.reflection_engine import ReflectionEngine
        from core.mnemos_bus import get_event_bus

        engine = ReflectionEngine()
        pending = engine.get_pending_feedback(
            hours_since=cfg.get("feedback.pending_hours", 24),
            limit=cfg.get("feedback.pending_limit", 10),
        )
        result["pending_count"] = len(pending)
        if pending:
            get_event_bus().publish(
                "feedback.prompt_due",
                payload={
                    "pending_count": len(pending),
                    "reflection_ids": [r.id for r in pending],
                    "trigger": "session_end",
                },
            )
            print(f"[Feedback] {len(pending)} 条 Reflection 等待你的反馈")
    except APOLLON_OPERATION_ERRORS as e:
        logger.debug("[Feedback] session end 检查失败: %s", e, exc_info=True)
    return result


def _resolve_working_dir(args) -> str:
    """从命令行参数解析工作目录，未指定时使用当前目录。"""
    return args.working_dir or os.getcwd()


def _handle_stats() -> None:
    """处理 --stats 子命令。"""
    show_stats()


def _handle_session_start(args) -> None:
    """处理 --session-start 子命令：加载并输出上下文。"""
    wd = _resolve_working_dir(args)
    context = get_context_for_claude(
        wd, user_message=args.user_message, authorize_cross=args.authorize
    )
    try:
        write_active_context("claude", wd, args.user_message or "")
    except APOLLON_OPERATION_ERRORS as e:
        logger.debug("写入 Claude active context 失败: %s", e, exc_info=True)
    print(context)


def _load_session_end_messages(
    args,
) -> Tuple[List[Dict[str, Any]], Optional[str], Optional[str]]:
    """为 session-end 加载消息，返回 (messages, json_str, session_id)。

    优先使用 --session-messages；否则从 JSONL 文件读取。
    session_id 从最新 JSONL 文件名推导。
    """
    wd = _resolve_working_dir(args)
    session_messages_json: Optional[str] = args.session_messages
    messages: List[Dict[str, Any]] = []
    if session_messages_json:
        messages = json.loads(session_messages_json)
    else:
        messages = _read_session_from_jsonl(wd)
        if messages:
            session_messages_json = json.dumps(messages, ensure_ascii=False)
            print(f"[Capture] 从 JSONL 文件读取 {len(messages)} 条消息")

    session_id = None
    if messages:
        latest_jsonl = _get_latest_jsonl(wd)
        session_id = latest_jsonl.stem if latest_jsonl else None
    return messages, session_messages_json, session_id


def _maybe_enqueue_session(
    messages: List[Dict[str, Any]], wd: str, session_id: Optional[str]
) -> Optional[str]:
    """尝试通过统一链路把 session 入队蒸馏，返回成功时的 session_id。"""
    try:
        from integrations.active_bridge import _enqueue_session

        latest_jsonl = _get_latest_jsonl(wd)
        sid = latest_jsonl.stem if latest_jsonl else session_id
        return _enqueue_session("claude", wd, messages, session_id=sid)
    except APOLLON_OPERATION_ERRORS as e:
        logger.warning("[L1] 统一链路入队失败: %s", e)
        return None


def _run_retrospective_if_present(session_messages_json: Optional[str]) -> str:
    """若存在会话 JSON，则运行自动复盘并输出结果。"""
    if not session_messages_json:
        return ""
    retro_result = run_retrospective(session_messages_json)
    if retro_result:
        print(retro_result)
    return retro_result


def _collect_persona_signals_from_messages(
    messages: List[Dict[str, Any]],
    wd: str,
    session_id: Optional[str],
    session_messages_json: Optional[str],
) -> None:
    """从会话消息中采集用户画像信号。"""
    if not session_messages_json:
        return
    try:
        messages = json.loads(session_messages_json)
        task_type = ""
        task_subtype = ""
        try:
            classifier = TaskClassifier()
            result = classifier.classify(messages)
            if result.confidence >= 0.7:
                task_type = result.task_type
                task_subtype = result.subtype
        except APOLLON_OPERATION_ERRORS as e:
            logger.warning("任务分类失败: %s", e)

        sig_count = _collect_session_signal(
            messages,
            working_dir=wd,
            task_type=task_type,
            task_subtype=task_subtype,
            session_id=session_id,
        )
        if sig_count > 0:
            print(f"[Persona] Session signal collected: {sig_count}")
    except APOLLON_OPERATION_ERRORS as e:
        print(f"[Persona] Signal collection error: {e}")


def _handle_session_end(args) -> None:
    """处理 --session-end 子命令：保存、入队、复盘、信号采集与 KIA 周期。"""
    wd = _resolve_working_dir(args)
    save_session(wd, args.summary)

    messages, session_messages_json, session_id = _load_session_end_messages(args)

    if messages:
        sid = _maybe_enqueue_session(messages, wd, session_id)
        if sid:
            print(f"[L1] Session queued for sync & distillation: {sid}")
    else:
        print("[L1] 无消息数据，跳过同步")

    _run_retrospective_if_present(session_messages_json)
    _collect_persona_signals_from_messages(messages, wd, session_id, session_messages_json)

    try:
        trigger_path = _write_sync_trigger(wd)
        if trigger_path:
            print(f"[Distill] Sync trigger written: {trigger_path.name}")
    except APOLLON_OPERATION_ERRORS as e:
        logger.warning("写入 sync trigger 失败: %s", e, exc_info=True)

    # CaptureWorkerPool.flush_session() 已在 L1 写入成功后自动入队 amphora，
    # 实际蒸馏处理由 daemon watchdog / HephaestusWorker 定期轮询完成。
    run_kia_cycles_light()

    # L3/L4/L5 自动触发（失败隔离，不影响主链路）
    if messages:
        _run_observation_on_session_end(messages)
        _run_reflection_on_session_end(messages)
    _run_feedback_prompt_on_session_end()


def _handle_kia_check() -> None:
    """处理 --kia-check 子命令：检查到期提醒。"""
    scheduler = KnowledgeScheduler()
    reminders = scheduler.get_pending_reminders()
    missed = scheduler.startup_compensation()
    all_reminders = reminders + missed
    if all_reminders:
        print(f"[KIA] 发现 {len(all_reminders)} 个到期提醒:")
        for task in all_reminders:
            print(f"  - {task.task_type}/{task.subtype}: {task.due_date[:10]}")
            print(f"    {scheduler.format_reminder(task)}")
            scheduler.mark_reminded(task.task_id)
    else:
        print("[KIA] 暂无到期提醒")


def _handle_default(parser: argparse.ArgumentParser) -> None:
    """默认输出帮助信息。"""
    parser.print_help()


def main() -> None:
    parser = argparse.ArgumentParser(description="Claude Code Mnemos Integration")
    parser.add_argument("--session-start", action="store_true", help="Session start - load context")
    parser.add_argument("--session-end", action="store_true", help="Session end - save context")
    parser.add_argument("--working-dir", default=None, help="Working directory")
    parser.add_argument("--user-message", default=None, help="用户输入（用于意图判定）")
    parser.add_argument("--summary", default="", help="Session summary")
    parser.add_argument(
        "--authorize", nargs="+", default=None, help="授权读取的跨agent列表，如 hermes openclaw"
    )
    parser.add_argument(
        "--session-messages", default=None, help="会话消息历史(JSON格式)，用于自动复盘"
    )
    parser.add_argument(
        "--kia-check", action="store_true", help="检查Knowledge-in-Action调度器中的到期提醒"
    )
    parser.add_argument("--stats", action="store_true", help="显示Mnemos系统统计")

    args = parser.parse_args()

    if args.stats:
        _handle_stats()
    elif args.session_start:
        _handle_session_start(args)
    elif args.session_end:
        _handle_session_end(args)
    elif args.kia_check:
        _handle_kia_check()
    else:
        _handle_default(parser)


# ---- Claude Code Agent Adapter (Olympus 基类实现) ----

from integrations.olympus import AgentAdapter, AgentRegistry  # noqa: E402


class ClaudeCodeAdapter(AgentAdapter):
    """Claude Code Agent 适配器 — 阿波罗预言之神"""

    @property
    def name(self) -> str:
        return "claude"

    @property
    def priority(self) -> int:
        return 1  # 最高优先级

    def is_available(self) -> bool:
        """检测 Claude Code 是否安装"""
        # 1. 新版/已配置的 Claude Code settings.json 路径
        settings_path = get_config().claude_data_dir / "settings.json"
        if settings_path.exists():
            return True
        # 2. macOS 旧版标准路径
        settings_path = Path.home() / "Library" / "Application Support" / "Claude" / "settings.json"
        if settings_path.exists():
            return True
        # 3. Linux/Windows 旧版标准路径
        settings_path = Path.home() / ".config" / "claude" / "settings.json"
        if settings_path.exists():
            return True
        # 4. 检查 claude 命令是否在 PATH 中
        import shutil

        if shutil.which("claude"):
            return True
        return False

    def get_config_path(self) -> Optional[Path]:
        # 优先新版路径
        candidates = [
            Path.home() / ".claude" / "settings.json",
            Path.home() / "Library" / "Application Support" / "Claude" / "settings.json",
            Path.home() / ".config" / "claude" / "settings.json",
        ]
        for p in candidates:
            if p.exists():
                return p
        return candidates[0]

    def is_hooks_installed(self) -> bool:
        """检查 Claude Code settings.json 中是否已安装 Mnemos hooks"""
        settings_path = self.get_config_path()
        if not settings_path or not settings_path.exists():
            return False
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                settings = json.load(f)
            hooks = settings.get("hooks", {})
            script_path = str(Path(__file__).resolve())
            start_hook = hooks.get("SessionStart", "")
            end_hook = hooks.get("SessionEnd", "")
            return script_path in start_hook and script_path in end_hook
        except (OSError, ValueError, TypeError, ImportError, AttributeError):  # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
            return False

    def is_mcp_configured(self) -> bool:
        return json_mcp_configured(get_config().claude_settings_path)

    def install_mcp_server(self) -> bool:
        return upsert_json_mcp_server(get_config().claude_settings_path, claude=True, agent="claude")  # noqa: E501

    def install_hooks(self) -> bool:
        """安装 Claude Code settings.json hooks"""
        settings_path = self.get_config_path()
        if not settings_path:
            return False
        try:
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            if settings_path.exists():
                with open(settings_path, "r", encoding="utf-8") as f:
                    settings = json.load(f)
            else:
                settings = {}
            if "hooks" not in settings:
                settings["hooks"] = {}
            script_path = Path(__file__).resolve()
            python_cmd = sys.executable
            settings["hooks"]["SessionStart"] = (
                f"{python_cmd} {script_path} --session-start "
                f'--working-dir "$PWD" --user-message "$USER_MESSAGE"'
            )
            settings["hooks"]["SessionEnd"] = (
                f"{python_cmd} {script_path} --session-end "
                f'--working-dir "$PWD" --session-messages "$SESSION_MESSAGES"'
            )
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2, ensure_ascii=False)
            self.install_mcp_server()
            return True
        except APOLLON_OPERATION_ERRORS as e:
            logger.warning("安装 hooks 失败: %s", e, exc_info=True)
            return False

    def collect_signals(self, days: int = 7) -> List[Dict]:
        """从 Claude Code 相关数据源采集信号

        SignalCollector 没有按 Agent 分的方法，使用通用采集方法
        从 distill_queue、wiki_state、git、wiki、filesystem 聚合信号。
        """
        try:
            collector = SignalCollector()
            # 使用通用采集方法，结果中 agent 字段默认为 "claude"
            collector.collect_all()
            # 从数据库读取最近 N 天的信号
            store = get_signal_store()
            cutoff = datetime.now() - timedelta(days=days)
            signals = []
            with sqlite3.connect(str(store.db_path), timeout=10) as conn:
                cursor = conn.execute(
                    """SELECT session_id, timestamp, task_type, task_subtype,
                              user_msg_count, avg_user_msg_length, correction_count,
                              follow_up_depth, termination_type, output_type, working_dir
                       FROM session_signals
                       WHERE timestamp >= ?
                       ORDER BY timestamp DESC
                    """,
                    (cutoff.isoformat(),),
                )
                for row in cursor.fetchall():
                    signals.append(
                        {
                            "source": "claude",
                            "session_id": row[0],
                            "timestamp": row[1],
                            "task_type": row[2] or "unknown",
                            "task_subtype": row[3] or "",
                            "user_msg_count": row[4] or 0,
                            "avg_user_msg_length": row[5] or 0.0,
                            "correction_count": row[6] or 0,
                            "follow_up_depth": row[7] or 0,
                            "termination_type": row[8] or "unknown",
                            "output_type": row[9] or "discussion",
                            "working_dir": row[10] or "",
                            "agent": "claude",
                        }
                    )
            return signals
        except APOLLON_OPERATION_ERRORS as e:
            logger.warning("Claude 信号采集失败: %s", e)
            return []


# 注册到 Olympian 众神殿堂
AgentRegistry.register(ClaudeCodeAdapter)


if __name__ == "__main__":
    main()
