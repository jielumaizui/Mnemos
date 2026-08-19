"""Agent-agnostic Mnemos preflight context builders.

This module owns the reusable markdown paragraphs that make up Mnemos'
session-start context.  Both the full Claude path and the lightweight
active-agent path consume these builders so the two preflight flows stay
consistent and can be improved in one place.

Design notes:
- Each builder returns a markdown string (or an empty string when nothing
  relevant is found).
- Builders catch their own exceptions and return empty strings so one slow
  or broken subsystem cannot break session start.
- ``print()`` statements are intentionally kept in the full KIA builder
  because Claude's hook runs synchronously and the output is useful for
  end-user feedback; the active path wraps the call in ``redirect_stdout``
  when timeout is enabled.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.agent_kit.protocol import CONTEXT_SHARE_AGENT_NAMES
from core.config import get_config
from core.runtime_environment import environment_get

logger = logging.getLogger(__name__)

PREFLIGHT_SECTION_ERRORS = (
    ImportError,
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    KeyError,
    sqlite3.Error,
)

_MCP_LAUNCH_REF_ENV = "MNEMOS_MCP_LAUNCH_CAPABILITY_REF"


def resolve_preflight_principal(agent: str) -> PrincipalEnvelope | None:
    """Resolve an active-agent principal without trusting an agent string."""
    reference = str(environment_get(_MCP_LAUNCH_REF_ENV, "")).strip()
    if not reference:
        return None
    try:
        from core.agent_kit.authorization import (
            AgentAuthorizationStore,
            MCPLaunchCredentialStore,
        )

        credential = MCPLaunchCredentialStore().resolve(reference)
        if not credential:
            return None
        principal = AgentAuthorizationStore(initialize=False).resolve_mcp_principal(credential)
    except (ImportError, OSError, RuntimeError, ValueError):
        return None
    if principal is None:
        return None
    requested_agent = str(agent or "").strip().lower()
    if requested_agent and principal.agent != requested_agent:
        return None
    return principal


# ---------------------------------------------------------------------------
# Knowledge-in-Action
# ---------------------------------------------------------------------------


def _format_loaded_knowledge(knowledge: Any) -> str:
    lines = [
        "## KIA Checklist",
        "",
        f"- Task type: {knowledge.task_type}",
        f"- Loaded version: {knowledge.version}",
    ]
    if getattr(knowledge, "is_compact", False):
        lines.append(
            f"- Compact view: showing {len(knowledge.checklist)}/{knowledge.total_items} items"
        )
    if getattr(knowledge, "lessons_summary", ""):
        lines.extend(["", "### Lessons", "", str(knowledge.lessons_summary).strip()])
    if getattr(knowledge, "checklist", None):
        lines.extend(["", "### Checklist", ""])
        for item in knowledge.checklist[:10]:
            severity = getattr(item, "severity", "medium")
            text = getattr(item, "item", str(item))
            lines.append(f"- [{severity}] {text}")
    return "\n".join(lines)


def _guard_state_file() -> Path:
    """Guard 状态文件路径（统一在 ~/.mnemos/ 下）"""
    return get_config().data_dir / "guard_state.json"


def _save_guard_state(guard: Any, task_type: str, subtype: str) -> None:
    """保存 Guard 会话状态到文件，供复盘时使用。"""
    if not guard or not getattr(guard, "session", None):
        return
    try:
        state = {
            "task_type": task_type,
            "subtype": subtype,
            "checklist": [
                {
                    "item": item.item,
                    "severity": item.severity,
                    "trigger_keywords": item.trigger_keywords,
                    "risk_patterns": item.risk_patterns,
                    "detail": item.detail,
                }
                for item in guard.session.checklist
            ],
            "triggered_alerts": [
                {
                    "level": alert.level.value,
                    "item": alert.checklist_item.item,
                    "triggered_by": alert.triggered_by,
                    "trigger_text": alert.trigger_text,
                }
                for alert in guard.session.triggered_alerts
            ],
            "silent_records": guard.session.silent_records,
            "timestamp": datetime.now().isoformat(),
        }
        _guard_state_file().parent.mkdir(parents=True, exist_ok=True)
        _guard_state_file().write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except PREFLIGHT_SECTION_ERRORS as e:
        logger.warning("[KIA-Guard] 状态保存失败: %s", e)


def _mark_guard_checklist_usage(guard: Any) -> None:
    """把 Guard 触发/静默记录的命中回写到源 checklist（KIA 闭环反馈边）。"""
    if not guard or not getattr(guard, "session", None):
        return
    session = guard.session
    checklist = session.checklist
    if not checklist:
        return

    try:
        from core.kia.prophasis import PreFlightInjector

        injector = PreFlightInjector()
        task_type = session.task_type
        subtype = session.subtype

        # 以源复盘文件中的 checklist 顺序为准建立 text -> file_index 映射。
        # 注意：session.checklist 可能经过过滤/排序/前置行为约束，其索引与源文件
        # 不一定一致，因此必须从最新版本复盘文件重新读取。
        file_index_map: Dict[str, int] = {}
        try:
            latest = injector._find_latest_version(task_type, subtype)
            if latest:
                fm, _ = injector._parse_retrospective(latest)
                for idx, raw in enumerate(fm.get("checklist", [])):  # type: ignore[union-attr]
                    if isinstance(raw, dict):
                        text = raw.get("item")
                        if text and text not in file_index_map:
                            file_index_map[text] = idx
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
        ):
            logger.debug("读取源 checklist 索引失败", exc_info=True)

        def _mark(item_text: str) -> None:
            idx = file_index_map.get(item_text)
            if idx is None:
                return
            try:
                injector.mark_checklist_used(task_type, subtype, idx, used=True)
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                ImportError,
                AttributeError,
                RuntimeError,
            ):
                logger.debug("标记 checklist 命中失败", exc_info=True)

        for alert in session.triggered_alerts:
            _mark(alert.checklist_item.item)

        for record in session.silent_records:
            item_text = record.get("item") if isinstance(record, dict) else None
            if item_text:
                _mark(item_text)
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
        logger.debug("回写 checklist 命中统计失败", exc_info=True)


def _build_light_kia(task_type: str, query: str) -> str:
    """Light 模式：直接加载指定任务类型的 checklist。"""
    from core.kia.kairos import TimeWindow, TimeWindowType
    from core.kia.prophasis import PreFlightInjector

    knowledge = PreFlightInjector().inject(
        task_type or "general",
        "",
        TimeWindow(window=TimeWindowType.IMMEDIATE, days_until=0),
        query,
    )
    return _format_loaded_knowledge(knowledge) if knowledge else ""


def _format_guard_output(
    knowledge: Any, alert: Any, knowledge_text: str
) -> Tuple[List[str], str, str]:
    """格式化 Guard 输出：动态 guard_lines、静态 guard_text、interrupt 短路文本。"""
    from core.kia.aegis import GuardLevel

    guard_lines: List[str] = []
    interrupt_text = ""
    if alert:
        emoji = {"interrupt": "🛑", "hint": "💡", "silent": "📝"}.get(alert.level.value, "⚠️")
        guard_lines.append(f"{emoji} [Guard Alert] {alert.checklist_item.item}")
        if alert.suggestion:
            guard_lines.append(f"   {alert.suggestion}")
        if alert.level == GuardLevel.INTERRUPT:
            guard_text_block = "\n".join(guard_lines)
            interrupt_text = f"\n{knowledge_text}\n\n{guard_text_block}\n"

    static_guard_lines = ["[Guard Rules]"]
    for item in knowledge.checklist:
        if item.severity in ("critical", "high"):
            static_guard_lines.append(f"⚠️ {item.item}")
    guard_text = "\n".join(static_guard_lines) if len(static_guard_lines) > 1 else ""

    return guard_lines, guard_text, interrupt_text


def _format_task_confirmation_notice(result: Any, task_label: str) -> str:
    """格式化任务分类确认提示，仅用于用户可见的 preflight 输出。"""
    strategy = result.suggested_confirmation
    if strategy == "ask":
        return f"[KIA] 任务分类需要确认: {task_label} " f"(置信度: {result.confidence:.2f})"
    if strategy == "hint":
        return f"[KIA] 任务分类建议确认: {task_label} " f"(置信度: {result.confidence:.2f})"
    return ""


def _build_full_kia(user_message: str, query: str) -> str:
    """Full 模式：任务分类、时间窗、Guard 检查并返回完整 KIA 段落。"""
    from core.kia.dike import TaskClassifier
    from core.kia.aegis import InProcessGuard
    from core.kia.chronos import KnowledgeScheduler
    from core.kia.kairos import TimeParser, should_load_knowledge
    from core.kia.prophasis import PreFlightInjector

    classifier = TaskClassifier()
    injector = PreFlightInjector()
    scheduler = KnowledgeScheduler()

    messages = [{"role": "user", "content": user_message}]
    result = classifier.classify(messages)

    task_label = classifier.get_task_type_label(result.task_type, result.subtype)
    confirmation_notice = _format_task_confirmation_notice(result, task_label)
    if confirmation_notice:
        print(confirmation_notice)

    if result.confidence < 0.7:
        return ""

    print(f"[KIA] 识别任务: {task_label} (置信度: {result.confidence:.2f})")

    should_load, time_window = should_load_knowledge(
        user_message,
        task_type=result.task_type,
    )

    if not should_load:
        if time_window.due_date:
            parser = TimeParser()
            task_id = scheduler.schedule(
                result.task_type,
                result.subtype,
                time_window.due_date,
                context=user_message,
                is_periodic=time_window.is_periodic,
                period=time_window.period,
            )
            print(
                f"[KIA] 任务已记入调度器: {task_id}，"
                f"提前 {parser.get_reminder_days_before(time_window)} 天提醒"
            )
        return ""

    knowledge = injector.inject(
        result.task_type,
        result.subtype,
        time_window,
        context_text=user_message,
    )
    if not knowledge:
        print(f"[KIA] 暂无历史经验 ({task_label})")
        return ""

    knowledge_text = injector.format_for_context(knowledge)
    guard = InProcessGuard(knowledge)
    alert = guard.check(user_message, "")

    if alert:
        print(f"[KIA-Guard] {alert.level.value.upper()}: {alert.checklist_item.item}")
    else:
        silent_records = guard.check_silent(user_message, "")
        if silent_records:
            print(f"[KIA-Guard] 静默记录 {len(silent_records)} 条")

    guard_lines, guard_text, interrupt_text = _format_guard_output(knowledge, alert, knowledge_text)

    _save_guard_state(guard, result.task_type, result.subtype)
    _mark_guard_checklist_usage(guard)

    if interrupt_text:
        return interrupt_text

    print(f"[KIA] 已装载 {task_label} v{knowledge.version}，" f"{len(knowledge.checklist)} 条经验")

    parts = [knowledge_text]
    if guard_lines:
        parts.append("\n".join(guard_lines))
    if guard_text:
        parts.append(guard_text)
    return "\n\n".join(parts) + "\n"


def build_kia_section(
    user_message: str,
    task_type: str = "",
    context_text: str = "",
    mode: str = "full",
) -> str:
    """Build the Knowledge-in-Action section.

    Args:
        user_message: The user's first message.
        task_type: Fallback task type (used by the lightweight path).
        context_text: Extra context text (used for light-mode query).
        mode: ``"full"`` runs task classification, time-window parsing,
            guard checks and scheduling.  ``"light"`` returns the immediate
            checklist only.
    """
    query = " ".join(p for p in [user_message, context_text] if p).strip()
    if not query:
        return ""

    try:
        if mode == "light":
            return _build_light_kia(task_type, query)
        return _build_full_kia(user_message, query)

    except PREFLIGHT_SECTION_ERRORS as e:
        logger.error("[KIA] 知识装载失败: %s", e, exc_info=True)
        return ""


# ---------------------------------------------------------------------------
# Wiki
# ---------------------------------------------------------------------------


def _log_wiki_trail(result: Dict, query: str) -> None:
    """记录 Wiki 查询轨迹（可选，失败时静默）。"""
    if not result.get("found"):
        return
    try:
        log_knowledge_usage = _import_optional_class("core.kia.ariadne", "log_knowledge_usage")
        if log_knowledge_usage is None:
            return
        for group in result.get("by_heat_level", {}).values():
            for page in group.get("pages", []):
                page_path = page.get("page_id", "")
                if page_path:
                    log_knowledge_usage(page_path, event_type="query", context=query)
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
        logger.debug("轨迹记录失败", exc_info=True)


def _render_deep_wiki_context(result: Dict, query: str) -> str:
    """渲染 deep 模式的 <wiki-context> 段落。"""
    context_parts = [
        "\n## Wiki知识参考（【知识查询类】自动检索）",
        f"查询: {result['query']}",
        f"找到 {result['total_pages']} 个相关页面，按热力值分层读取:\n",
    ]
    for level_group in ["hot", "warm", "cold", "unknown"]:
        if level_group not in result["by_heat_level"]:
            continue
        group = result["by_heat_level"][level_group]
        if group["count"] <= 0:
            continue
        context_parts.append(f"\n### [{level_group}] {group['count']}个页面 - {group['depth']}")
        for page in group["pages"][:3]:
            page_content = page["content"] or {}
            lines = [f"\n**{page_content.get('title', page['title'])}** [{page['heat_level']}]"]
            if page_content.get("content"):
                lines.append(str(page_content["content"])[:1500])
            elif page_content.get("summary"):
                lines.append(str(page_content["summary"]))
            elif page_content.get("note"):
                lines.append(str(page_content["note"]))
            if page_content.get("related"):
                related = [r["page_id"] for r in page_content["related"][:3]]
                lines.append(f"\n关联: {', '.join(related)}")
            context_parts.append("\n".join(lines))
    context_parts.append("\n---\n")
    full_context = "\n".join(context_parts)
    return '<wiki-context source="knowledge-query">\n' f"{full_context}\n" "</wiki-context>"


def _read_authorized_wiki_body(
    facade: Any,
    page_id: str,
    principal: PrincipalEnvelope,
    narrowing: AccessNarrowing,
) -> str:
    """Fetch a body only through the facade's frontmatter-first read seam."""
    try:
        response = facade.wiki_read(
            page_id,
            principal=principal,
            narrowing=narrowing,
        )
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
        return ""
    if not isinstance(response, dict) or response.get("success") is not True:
        return ""
    content = response.get("content")
    if not isinstance(content, str):
        return ""
    try:
        from core.frontmatter import parse_frontmatter

        _frontmatter, body = parse_frontmatter(content)
    except (OSError, ValueError, TypeError):
        return ""
    return body.strip()


def _render_light_wiki_lines(
    results: List[Dict],
    facade: Any,
    principal: PrincipalEnvelope,
    narrowing: AccessNarrowing,
) -> str:
    """Render light-mode excerpts after each body has passed the read seam."""
    lines = ["## Related Wiki Knowledge", ""]
    for result in results[:5]:
        title = result.get("title", "") or result.get("page_id", "")
        page_id = result.get("page_id", "")
        score = result.get("relevance_score", result.get("score", 0))
        snippet = _read_authorized_wiki_body(facade, page_id, principal, narrowing)
        snippet = str(snippet).replace("\n", " ").strip()
        lines.append(f"- {title} ({page_id}, score={score:.2f}): {snippet[:220]}")
    return "\n".join(lines)


def _render_authorized_deep_wiki_context(
    query: str,
    results: List[Dict],
    facade: Any,
    principal: PrincipalEnvelope,
    narrowing: AccessNarrowing,
) -> str:
    """Render only individually authorized Wiki bodies for the deep path."""
    grouped: Dict[str, List[Dict]] = {}
    for result in results:
        grouped.setdefault(str(result.get("heat_level") or "unknown"), []).append(result)
    context_parts = [
        "\n## Wiki知识参考（【知识查询类】自动检索）",
        f"查询: {query}",
        f"找到 {len(results)} 个已授权相关页面，按热力值分层读取:\n",
    ]
    for level_group in ("hot", "warm", "cold", "unknown"):
        pages = grouped.get(level_group, [])
        if not pages:
            continue
        context_parts.append(f"\n### [{level_group}] {len(pages)}个页面")
        for page in pages[:3]:
            page_id = str(page.get("page_id") or "")
            title = str(page.get("title") or page_id)
            body = _read_authorized_wiki_body(facade, page_id, principal, narrowing)
            if not body:
                continue
            limit = 1500 if level_group == "hot" else 500 if level_group == "warm" else 220
            context_parts.append(f"\n**{title}** [{level_group}]\n{body[:limit]}")
    context_parts.append("\n---\n")
    full_context = "\n".join(context_parts)
    return '<wiki-context source="knowledge-query">\n' f"{full_context}\n" "</wiki-context>"


def build_wiki_section(
    query: str,
    mode: str = "light",
    agent: str = "",
    *,
    principal: PrincipalEnvelope | None = None,
    narrowing: AccessNarrowing | None = None,
) -> str:
    """Build the Wiki knowledge section.

    Args:
        query: Query string.
        mode: ``"deep"`` returns heat-level grouped full knowledge (Claude path).
            ``"light"`` returns the top-5 search snippets (active path).
        agent: Agent identifier used to resolve the active server principal.
        principal: Server-resolved principal.  When omitted, the active launch
            credential is resolved; a missing credential fails closed.
        narrowing: Request constraints that may only narrow the principal.
    """
    if not query:
        return ""

    resolved_principal = principal or resolve_preflight_principal(agent)
    if resolved_principal is None:
        return ""
    requested_agent = str(agent or "").strip().lower()
    if requested_agent and resolved_principal.agent != requested_agent:
        return ""
    effective_narrowing = narrowing or AccessNarrowing()

    try:
        from core.application.facade import DefaultMnemosServiceFacade

        facade = DefaultMnemosServiceFacade(logger)
        results, _access_summary = facade.wiki_search(
            query,
            limit=5,
            principal=resolved_principal,
            narrowing=effective_narrowing,
        )
        if not results:
            return "\n（Wiki中未找到相关知识）\n" if mode == "deep" else ""
        _log_wiki_trail(
            {
                "found": True,
                "by_heat_level": {"authorized": {"pages": results}},
            },
            query,
        )
        if mode == "deep":
            return _render_authorized_deep_wiki_context(
                query,
                results,
                facade,
                resolved_principal,
                effective_narrowing,
            )
        return _render_light_wiki_lines(
            results,
            facade,
            resolved_principal,
            effective_narrowing,
        )

    except PREFLIGHT_SECTION_ERRORS as e:
        logger.warning("Wiki 构建失败: %s", e, exc_info=True)
        return ""


def _import_optional_class(module_path: str, class_name: str):
    """尝试导入可选模块中的类；缺失时返回 None。"""
    from core.import_guard import assert_allowed_module

    try:
        assert_allowed_module(module_path)
        import importlib

        module = importlib.import_module(module_path)
        return getattr(module, class_name)
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
        logger.debug("可选模块未加载: %s.%s", module_path, class_name, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# L1 / raw memory recall
# ---------------------------------------------------------------------------


def _parse_created_at(value: str) -> Optional[datetime]:
    """把各种 ISO 时间字符串统一解析为 aware UTC datetime。"""
    if not value:
        return None
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _filter_recent(memories: List[Any], cutoff: datetime) -> List[Any]:
    """过滤出在 cutoff 时间之后的记忆。"""
    return [
        m
        for m in memories
        if (created := _parse_created_at(_memory_field(m, "created_at"))) and created > cutoff
    ]


def _memory_field(memory: Any, field: str, default: Any = "") -> Any:
    if isinstance(memory, dict):
        return memory.get(field, default)
    return getattr(memory, field, default)


def _format_my_memories(memories: List[Any]) -> str:
    """格式化当前 agent 的最近会话记忆。"""
    if not memories:
        return ""
    lines = [f"\n## 最近会话上下文（{len(memories)}条）\n"]
    for mem in memories[:10]:
        session_id = "unknown"
        for tag in _memory_field(mem, "tags", []):
            if tag.startswith("session="):
                session_id = tag.split("=", 1)[1]
                break
        content_preview = str(_memory_field(mem, "content"))[:200].replace("\n", " ")
        lines.append(f"- Session `{session_id}`: {content_preview}...")
    return "\n".join(lines)


def _format_cross_agent_memories(agent: str, memories: List[Any]) -> str:
    """格式化单个跨 agent 共享记忆。"""
    if not memories:
        return ""
    lines = [f"\n## {agent} 框架共享记忆（{len(memories)}条）\n"]
    for mem in memories[:5]:
        content_preview = str(_memory_field(mem, "content"))[:150].replace("\n", " ")
        lines.append(f"- {content_preview}...")
    return "\n".join(lines)


def _format_related_memories(results: List[Any]) -> str:
    """格式化目录相关记忆。"""
    if not results:
        return ""
    lines = [f"\n## 相关记忆（{len(results)}条）\n"]
    for r in results[:5]:
        content_preview = str(_memory_field(r, "content"))[:150].replace("\n", " ")
        source = "search"
        for tag in _memory_field(r, "tags", []):
            if tag.startswith("source="):
                source = tag.split("=", 1)[1]
                break
        lines.append(f"- [{source}] {content_preview}...")
    return "\n".join(lines)


def build_l1_section(
    working_dir: str,
    agent: str,
    authorize_cross: Optional[List[str]] = None,
    *,
    principal: PrincipalEnvelope | None = None,
    narrowing: AccessNarrowing | None = None,
) -> str:
    """Build the raw-memory / L1 recall section.

    Args:
        working_dir: Current working directory.
        agent: Agent identifier used as ``source=<agent>``.
        authorize_cross: Explicit list of agents whose memories may be recalled.
            When ``None`` the global ``cross_agent_share`` config decides.
    """
    resolved_principal = principal or resolve_preflight_principal(agent)
    if resolved_principal is None:
        return ""
    requested_agent = str(agent or "").strip().lower()
    if requested_agent and resolved_principal.agent != requested_agent:
        return ""
    effective_narrowing = narrowing or AccessNarrowing()
    config = get_config()

    try:
        from core.application.facade import DefaultMnemosServiceFacade

        facade = DefaultMnemosServiceFacade(logger)
    except (ImportError, OSError, RuntimeError, ValueError):
        return ""

    def authorized_raw_memories(source: str = "", query: str = "", limit: int = 10) -> List[Dict]:
        """Read canonical Raw only through the header-first facade seam."""
        try:
            response = facade.session_search(
                query=query,
                source=source or None,
                days=7,
                limit=limit,
                principal=resolved_principal,
                narrowing=effective_narrowing,
            )
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
            return []
        if not isinstance(response, dict) or response.get("success") is not True:
            return []
        memories: List[Dict] = []
        for result in response.get("results") or []:
            if not isinstance(result, dict):
                continue
            source_agent = str(result.get("source_agent") or result.get("source") or "")
            session_id = str(result.get("session_id") or "")
            content = str(result.get("snippet") or "")
            created_at = str(result.get("created_at") or "")
            if not source_agent or not session_id or not content or not created_at:
                continue
            memories.append(
                {
                    "content": content,
                    "created_at": created_at,
                    "tags": [f"source={source_agent}", f"session={session_id}"],
                }
            )
        return memories

    all_agents = list(CONTEXT_SHARE_AGENT_NAMES)
    if authorize_cross is None:
        authorize_cross = all_agents if config.cross_agent_share else []

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    context_parts: List[str] = []

    my_memories = _filter_recent(authorized_raw_memories(source=agent, limit=30), cutoff)
    context_parts.append(_format_my_memories(my_memories))

    for cross_agent in authorize_cross:
        if cross_agent == agent:
            continue
        cross_memories = _filter_recent(
            authorized_raw_memories(source=cross_agent, limit=10), cutoff
        )
        context_parts.append(_format_cross_agent_memories(cross_agent, cross_memories))

    dir_name = Path(working_dir).name
    if dir_name:
        related = _filter_recent(authorized_raw_memories(query=dir_name, limit=10), cutoff)
        context_parts.append(_format_related_memories(related))

    context_parts = [p for p in context_parts if p]
    if not context_parts:
        return "\n（暂无相关上下文）\n"
    return "\n".join(context_parts)


# ---------------------------------------------------------------------------
# Predictive Push
# ---------------------------------------------------------------------------


def build_predictive_push_section(
    user_message: str,
    agent: str = "",
    *,
    principal: PrincipalEnvelope | None = None,
    narrowing: AccessNarrowing | None = None,
) -> str:
    """Build the PredictivePush recommendation section."""
    if not user_message:
        return ""
    resolved_principal = principal or resolve_preflight_principal(agent)
    if resolved_principal is None:
        return ""
    requested_agent = str(agent or "").strip().lower()
    if requested_agent and resolved_principal.agent != requested_agent:
        return ""
    effective_narrowing = narrowing or AccessNarrowing()
    try:
        # Build the candidate allow-list from frontmatter only.  The push
        # engine may read an excerpt only after this filter has admitted the
        # page, including its embedding/index path.
        from core.app.context_search import ContextAwareSearch
        from core.kia.teiresias import PredictivePushEngine

        push_engine = PredictivePushEngine()
        search = ContextAwareSearch(wiki_base=str(push_engine.wiki_base))
        authorized_frontmatter, _summary = search._authorized_frontmatter_pages(
            resolved_principal,
            effective_narrowing,
        )
        if not authorized_frontmatter:
            return ""
        authorized_paths = {
            str((push_engine.wiki_base / relative_path).resolve())
            for relative_path in authorized_frontmatter
        }
        push_decision = push_engine.decide_push(
            user_message,
            candidate_path_filter=lambda path: str(Path(path).resolve()) in authorized_paths,
        )
        if push_decision and push_decision.should_push:
            return (
                "\n[Predictive Push] 基于上下文主动推荐:\n"
                f"  推荐内容: {push_decision.push_content[:200] if push_decision.push_content else 'N/A'}\n"  # noqa: E501
                f"  推荐理由: {push_decision.reason}\n"
                f"  匹配数: {len(push_decision.matches)}\n"
            )
    except PREFLIGHT_SECTION_ERRORS as e:
        logger.warning("PredictivePush 失败: %s", e, exc_info=True)
    return ""


# ---------------------------------------------------------------------------
# Observations (L3 read-only projection)
# ---------------------------------------------------------------------------


def build_observation_section(
    limit: int = 5,
    agent: str = "",
    *,
    principal: PrincipalEnvelope | None = None,
    narrowing: AccessNarrowing | None = None,
) -> str:
    """Build the recent-observation section."""
    resolved_principal = principal or resolve_preflight_principal(agent)
    if resolved_principal is None:
        return ""
    requested_agent = str(agent or "").strip().lower()
    if requested_agent and resolved_principal.agent != requested_agent:
        return ""
    try:
        cfg = get_config()
        if not cfg.get("observation.enabled", True):
            return ""
        if not cfg.get("observation.inject_on_session_start", True):
            return ""
        from core.cognitive.observation_store import ObservationIndex

        recent, _access = ObservationIndex().authorized_get_latest(
            principal=resolved_principal,
            narrowing=narrowing or AccessNarrowing(),
            purpose="preflight_inject",
            limit=limit,
        )
        if not recent:
            return ""
        lines = ["\n[近期观察]"]
        for obs in recent:
            summary = ""
            if obs.evidence:
                summary = str(obs.evidence[0])[:120]
            lines.append(
                f"- {obs.dimension.value} / {obs.observation_type.value}: "
                f"{summary or str(obs.value)[:120]}"
            )
        return "\n".join(lines)
    except PREFLIGHT_SECTION_ERRORS as e:
        logger.debug("近期观察注入失败: %s", e, exc_info=True)
        return ""


# ---------------------------------------------------------------------------
# Persona
# ---------------------------------------------------------------------------


def _profile_number(value: Any) -> float | None:
    if isinstance(value, dict):
        for key in ("score", "value", "confidence"):
            if key in value:
                return _profile_number(value[key])
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_contextual_persona_profiles(
    principal: PrincipalEnvelope,
    narrowing: AccessNarrowing,
) -> tuple[Dict[str, float], Dict[str, float]]:
    """Return ACL-authorized persona strategy dimensions only.

    Historical ``persona_versions`` rows do not have object ACL lineage.  They
    must not be read into a prompt merely because a server principal exists;
    their reconciliation is a separate migration.  The current typed profile
    assertion path below remains available when its own per-object ACL admits
    it.
    """

    del principal, narrowing
    return {}, {}


def _load_user_cognitive_profile_v2(
    principal: PrincipalEnvelope,
    narrowing: AccessNarrowing,
) -> tuple[Dict[str, Any], str]:
    """Load cognitive persona v2 assertions without failing preflight."""
    try:
        from core.persona.psyche import get_signal_store

        profile, access = get_signal_store().build_authorized_user_cognitive_profile_v2(
            principal=principal,
            narrowing=narrowing,
            purpose="persona_preflight_read",
            consumer="preflight_builder",
        )
        return profile, str(access.get("read_authorization_token") or "")
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
        logger.debug("认知画像 v2 读取失败", exc_info=True)
        return {}, ""


def _record_profile_v2_usage(
    consumer: str,
    matched_assertion_revisions: Dict[str, str],
    *,
    principal: PrincipalEnvelope,
    narrowing: AccessNarrowing,
    baseline_output: Any,
    persona_enabled_output: Any,
    expected_delta: Dict[str, Any],
    outcome: str,
    read_authorization_token: str,
) -> None:
    if not matched_assertion_revisions:
        return
    from core.persona.psyche import ProfileUsageLog, get_signal_store
    from core.persona.profile_effect import compare_profile_effect

    emitted_revisions = dict(sorted(matched_assertion_revisions.items()))
    receipt_delta = dict(expected_delta)
    supplied_emitted = receipt_delta.get("emitted_assertion_revisions")
    if supplied_emitted is not None and supplied_emitted != emitted_revisions:
        raise ValueError("preflight receipt fields must equal exact emitted revisions")
    receipt_delta["emitted_assertion_revisions"] = emitted_revisions
    get_signal_store().record_profile_usage(
        ProfileUsageLog(
            consumer=consumer,
            profile_fields_used=sorted(emitted_revisions),
            read_purpose="persona_preflight_read",
            read_authorization_token=read_authorization_token,
            target_receipt=compare_profile_effect(
                owner=consumer,
                target_type="prompt",
                target_id="preflight_persona_section",
                matched_assertion_revisions=emitted_revisions,
                baseline_output=baseline_output,
                persona_enabled_output=persona_enabled_output,
                expected_delta=receipt_delta,
            ),
            outcome=outcome,
        ),
        principal=principal,
        narrowing=narrowing,
    )


def _format_cognitive_profile_v2_section_with_matches(
    profile_v2: Dict[str, Any],
    *,
    token_limit: int | None = None,
) -> tuple[str, Dict[str, str]]:
    assertions = profile_v2.get("profile_assertions", []) or []
    if not assertions:
        return "", {}

    lines = [
        "[User Cognitive Profile v2]",
        f"- confidence: {profile_v2.get('confidence', 0.0)}",
    ]
    matched_revisions: Dict[str, str] = {}
    canonical_assertions: Dict[str, tuple[str, str]] = {}
    for item in assertions:
        assertion_id = str(item.get("assertion_id") or "")
        revision_id = str(item.get("current_revision_id") or "")
        canonical_claim = " ".join(str(item.get("claim") or "").strip().split())
        if not assertion_id or not revision_id:
            continue
        canonical = (revision_id, canonical_claim)
        previous = canonical_assertions.get(assertion_id)
        if previous is not None and previous != canonical:
            raise ValueError("profile prompt assertion projection is ambiguous")
        canonical_assertions[assertion_id] = canonical
    bucket_specs = [
        ("judgment_standards", "判断标准"),
        ("decision_preferences", "决策偏好"),
        ("interaction_contracts", "交互契约"),
        ("risk_boundaries", "风险边界"),
        ("negative_feedback", "负反馈/纠错"),
        ("intent_patterns", "意图模式"),
    ]
    emitted = 0
    seen_assertions: set[str] = set()
    seen_claims: set[str] = set()
    for key, label in bucket_specs:
        for item in profile_v2.get(key, []) or []:
            claim = str(item.get("claim", "")).strip()
            normalized_claim = " ".join(claim.split())
            assertion_id = str(item.get("assertion_id") or "")
            if (
                not normalized_claim
                or assertion_id in seen_assertions
                or normalized_claim in seen_claims
            ):
                continue
            conf = item.get("confidence", 0.0)
            emitted_line = f"- {label}: {claim} (confidence={conf})"
            canonical = canonical_assertions.get(assertion_id)
            bucket_revision = str(item.get("current_revision_id") or "")
            if (
                canonical is None
                or canonical[1] != normalized_claim
                or (bucket_revision and bucket_revision != canonical[0])
            ):
                raise ValueError("profile prompt claim/revision projection drift")
            revision_id = canonical[0]
            if not assertion_id:
                raise ValueError("profile prompt assertion lacks immutable revision")
            candidate = "\n".join((*lines, emitted_line))
            if token_limit is not None and _persona_token_estimate(candidate) > max(
                0, int(token_limit)
            ):
                continue
            lines.append(emitted_line)
            matched_revisions[assertion_id] = revision_id
            seen_assertions.add(assertion_id)
            seen_claims.add(normalized_claim)
            emitted += 1
            if emitted >= 6:
                break
        if emitted >= 6:
            break
    return ("\n".join(lines), matched_revisions) if emitted else ("", {})


def _format_cognitive_profile_v2_section(profile_v2: Dict[str, Any]) -> str:
    section, _matches = _format_cognitive_profile_v2_section_with_matches(profile_v2)
    return section


def _persona_token_estimate(text: str) -> int:
    return max(0, len(text or "") // 4)


def build_persona_section(
    agent: str,
    working_dir: str = "",
    session_tags: List[str] | tuple[str, ...] | None = None,
    *,
    principal: PrincipalEnvelope | None = None,
    narrowing: AccessNarrowing | None = None,
) -> str:
    """Build the persona-driven behavior section."""
    resolved_principal = principal or resolve_preflight_principal(agent)
    if resolved_principal is None:
        return ""
    requested_agent = str(agent or "").strip().lower()
    if requested_agent and resolved_principal.agent != requested_agent:
        return ""
    try:
        cfg = get_config()
        if not cfg.get("persona.enabled", True):
            return ""
        from core.persona.delphi import get_behavior_prompt

        base = get_behavior_prompt(agent) or ""
        if not cfg.get("persona.strategy_injection_enabled", True):
            return base

        from core.persona.contextual_strategy import (
            PersonaStrategyBuilder,
            detect_persona_context,
        )

        token_limit = int(cfg.get("persona.strategy_token_limit", 300) or 300)
        preference, blindspot = _load_contextual_persona_profiles(
            resolved_principal,
            narrowing or AccessNarrowing(),
        )
        strategy = PersonaStrategyBuilder(
            token_limit=max(1, token_limit - 24),
            enabled=True,
        ).build(preference, blindspot)
        profile_v2, profile_read_authorization = _load_user_cognitive_profile_v2(
            resolved_principal,
            narrowing or AccessNarrowing(),
        )
        cognitive_profile, cognitive_profile_revisions = (
            _format_cognitive_profile_v2_section_with_matches(
                profile_v2,
                token_limit=token_limit,
            )
        )
        if cognitive_profile:
            profile_enabled_output = "\n\n".join(
                part for part in (base, cognitive_profile) if part.strip()
            )
            _record_profile_v2_usage(
                "preflight_builder",
                cognitive_profile_revisions,
                principal=resolved_principal,
                narrowing=narrowing or AccessNarrowing(),
                baseline_output=base,
                persona_enabled_output=profile_enabled_output,
                expected_delta={
                    "kind": "prompt_append",
                    "section": "user_cognitive_profile_v2",
                },
                outcome="persona_section_augmented",
                read_authorization_token=profile_read_authorization,
            )
        if not strategy.get("prompt"):
            return "\n\n".join(part for part in (base, cognitive_profile) if part.strip())

        context = detect_persona_context(
            working_dir=working_dir,
            session_tags=tuple(session_tags or ()),
        )
        scope_notes = {
            "work": "工作上下文：优先说明业务影响、风险边界和验证步骤。",
            "personal": "个人上下文：保留探索空间，避免把问题过早工程化。",
            "study": "学习上下文：先讲原理和迁移方法，再给练习式步骤。",
            "default": "默认上下文：保持策略轻量，只在明显有帮助时介入。",
        }
        contextual_lines = [
            "[Contextual Persona Strategy]",
            f"- scope: {context.scope}",
            f"- {scope_notes.get(context.scope, scope_notes['default'])}",
            *str(strategy["prompt"]).splitlines(),
        ]
        while (
            len(contextual_lines) > 2
            and _persona_token_estimate("\n".join(contextual_lines)) > token_limit
        ):
            contextual_lines.pop()
        contextual = "\n".join(contextual_lines)
        return "\n\n".join(part for part in (base, contextual, cognitive_profile) if part.strip())
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
        logger.debug("画像行为提示生成失败", exc_info=True)
        return ""


# ---------------------------------------------------------------------------
# Lightweight preflight assembly
# ---------------------------------------------------------------------------


def _default_task_type(agent: str) -> str:
    if agent in {
        "claude",
        "codex",
        "crush",
        "hermes",
        "kiro",
        "kimi",
        "opencode",
        "openclaw",
    }:
        return "coding"
    return "general"


def build_lightweight_preflight(
    agent: str,
    working_dir: str,
    user_message: str,
) -> str:
    """Assemble the lightweight preflight context used by non-Claude agents.

    This intentionally avoids heavy L1 scans and deep Wiki reads so that
    session-start stays fast even when the full preflight path times out.
    """
    # These two sections are public, body-free operating instructions.  They
    # remain useful when no MCP principal is available and must not be coupled
    # to authorization for user data reads below.
    static_sections = [
        build_active_tooling_section(),
        build_user_visible_behavior_section(),
    ]
    parts: List[str] = []
    principal = resolve_preflight_principal(agent)
    if principal is None:
        return "\n\n".join(static_sections)
    task_type = _default_task_type(agent)
    query = " ".join(p for p in [user_message, Path(working_dir).name] if p).strip()

    kia_section = build_kia_section(
        user_message=user_message,
        task_type=task_type,
        context_text=Path(working_dir).name,
        mode="light",
    )
    if kia_section:
        parts.append(kia_section)

    if query:
        wiki_section = build_wiki_section(
            query,
            mode="light",
            agent=agent,
            principal=principal,
        )
        if wiki_section:
            parts.append(wiki_section)

    persona_section = build_persona_section(
        agent,
        working_dir=working_dir,
        principal=principal,
    )
    if persona_section:
        parts.append(persona_section)

    parts.extend(static_sections)
    return "\n\n".join(p for p in parts if p.strip())


# ---------------------------------------------------------------------------
# Active-agent helper sections
# ---------------------------------------------------------------------------


def build_active_tooling_section() -> str:
    """Return the active tooling reminder used by lightweight preflights."""
    return (
        "## Active Tooling\n\n"
        "For raw quotes, evidence, or chat history, call `session_search`. "
        "For durable knowledge, decisions, experience, or preferences, call `context_aware_search` or `wiki_search`. "
        "For 'how did we solve this last time' questions, call both `session_search` and `context_aware_search`. "  # noqa: E501
        "For system state use health/status/doctor tools; for persona questions use persona tools. "  # noqa: E501
        "Call `check_pending_recaps` at session start, task wrap-up, and when recap/follow-up is relevant. "  # noqa: E501
        "Call `predictive_push` once when the current task resembles known work or a timely suggestion may help; "  # noqa: E501
        "if you show a push to the user, ask if it was helpful and call `push_feedback(delivery_event_id=<delivery_event_id>, topic=<topic>, action=accept|ignore|dismiss)`. For inaccurate/outdated, first obtain the latest canonical feedback_event_id plus an exact correction target and reason. "  # noqa: E501
        "Call `intent_route` when the user's intent is ambiguous; if it returns `needs_correction=true`, confirm with the user and call `intent_correct`. "  # noqa: E501
        "Call `guard_check` before high-impact final answers and whenever repeated analysis/read loops appear; "  # noqa: E501
        "include recent tool_calls/current_file in context when available."
    )


def build_user_visible_behavior_section() -> str:
    """Return the user-visible behaviour contract used by lightweight preflights."""
    return (
        "## User-Visible Behavior\n\n"
        "- If Mnemos changes the plan, warning, or next action, mention the specific retrieved lesson in one concise sentence.\n"  # noqa: E501
        "- If no useful Mnemos knowledge is found, stay focused on the task instead of showing empty memory status.\n"  # noqa: E501
        "- If a recap item is urgent or force-open, nudge the user before closing the task."
    )
