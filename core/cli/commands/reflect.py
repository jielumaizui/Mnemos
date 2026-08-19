"""Reflect command for Mnemos CLI."""

import logging

from core.cli.helpers import _get_config

logger = logging.getLogger(__name__)


def _print_reflection_result(result) -> None:
    """打印 ReflectionEngine 返回的结果。"""
    print(f"Triggered: {result.triggered}")
    print(f"LLM 调用: {'是' if result.insight and result.insight.llm_called else '否'}")
    if result.record:
        print(f"Record ID: {result.record.id}")
    if result.insight:
        if result.insight.summary:
            print(f"洞察摘要: {result.insight.summary}")
        if result.insight.key_points:
            print("关键发现:")
            for point in result.insight.key_points:
                print(f"  - {point}")
        if result.insight.llm_error:
            print(f"LLM 失败原因: {result.insight.llm_error}")
        print(f"Prompt:\n{result.insight.prompt_used}")


def _build_reflection_engine(*, use_llm: bool = True, with_consumers: bool = True):
    """Build a CLI ReflectionEngine with Layer 5 consumers enabled by config."""
    from core.reflection.reflection_engine import ReflectionEngine

    cfg = _get_config()
    register_consumers = bool(
        with_consumers and cfg.get("reflection.register_default_consumers", True)
    )
    if not register_consumers and not use_llm:
        return ReflectionEngine()
    persona_store = None
    if register_consumers:
        try:
            from core.persona.psyche import get_signal_store

            persona_store = get_signal_store()
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
            logger.debug("[reflect] persona signal store unavailable", exc_info=True)

    engine = ReflectionEngine(
        register_default_consumers=register_consumers,
        persona_store=persona_store,
        kia_store=None,
        wiki_dir=str(cfg.wiki_dir) if getattr(cfg, "wiki_dir", None) else None,
        export_to_wiki=True,
        use_llm=use_llm,
    )
    if register_consumers:
        try:
            for consumer in getattr(engine, "_consumers", []):
                for child in getattr(consumer, "consumers", []):
                    if getattr(child, "kia_store", None) is None:
                        child.kia_store = engine.ref_store
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
            logger.debug("[reflect] KIA reflection store wiring failed", exc_info=True)
    return engine


def _cmd_reflect_on(args, auto_llm: bool) -> None:
    """执行反射并打印结果。"""
    from core.reflection.reflection_router import ReflectionRouter

    router = ReflectionRouter()
    route = router.route(args.text)
    engine = _build_reflection_engine(use_llm=auto_llm)
    result = engine.reflect_on_user_input(args.text)
    # 如果 Router 判定应该反射但触发器未命中，fallback 到手动反射
    if route.should_reflect and not result.triggered:
        result = engine.reflect_manually(args.text)
    print(f"Router: {route.reason}")
    _print_reflection_result(result)


def _cmd_reflect_manual(args, auto_llm: bool) -> None:
    """手动触发反射。"""
    cfg = _get_config()
    query = args.query or cfg.get("reflection.manual_query", "分析最近认知与决策模式")
    engine = _build_reflection_engine(use_llm=auto_llm)
    result = engine.reflect_manually(query)
    _print_reflection_result(result)


def _cmd_reflect_pending(args) -> None:
    """列出待反馈的 reflection。"""
    engine = _build_reflection_engine(use_llm=False, with_consumers=False)
    pending = engine.get_pending_feedback(
        hours_since=args.hours_since,
        limit=args.limit,
    )
    print(f"待反馈 Reflection: {len(pending)} 条")
    for r in pending:
        summary = (r.insight_summary or "").replace("\n", " ")[:80]
        print(
            f"  - {r.reflection_id} ({r.created_at}, "
            f"{r.trigger}, {r.hours_ago:.1f}h ago): {summary}"
        )


def _cmd_reflect_feedback(args) -> None:
    """Fail closed because this local CLI has no authenticated principal."""

    del args
    print("该本地 CLI 反馈入口已停用：请使用带认证 principal 的 reflection_feedback 工具。")


def cmd_reflect(args):
    """Reflection（L4）CLI 入口。"""
    auto_llm = getattr(args, "auto_llm", True)

    handlers = {
        "on": lambda: _cmd_reflect_on(args, auto_llm),
        "manual": lambda: _cmd_reflect_manual(args, auto_llm),
        "pending": lambda: _cmd_reflect_pending(args),
        "feedback": lambda: _cmd_reflect_feedback(args),
    }
    handler = handlers.get(args.reflect_cmd)
    if handler is None:
        print("用法: mnemos reflect {on|manual|pending|feedback}")
        return
    handler()
