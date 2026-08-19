"""Cognitive-graph command for Mnemos CLI."""

import json
import logging

from core.config import get_config as _get_config  # noqa: F401
from core.mnemos_bus import Event, get_event_bus

logger = logging.getLogger(__name__)


def cmd_cognitive_graph(args):
    """跨层认知图 CLI 入口。"""
    from core.cognitive_graph import CognitiveGraphStore, CognitiveGraphUpdater

    store = CognitiveGraphStore()
    updater = CognitiveGraphUpdater(store=store)

    if args.cg_cmd == "stats":
        stats = store.get_stats()
        print("跨层认知图统计：")
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    elif args.cg_cmd == "reconcile":
        print("开始认知图 reconciliation...")
        result = updater.reconcile()
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    elif args.cg_cmd == "ingest":
        payload = {}
        if args.session_id:
            payload["session_id"] = args.session_id
        if args.page_path:
            payload["page_path"] = args.page_path
        event = Event(event_type=args.event_type, source="cli", payload=payload)

        bus = get_event_bus()
        updater.subscribe(bus)
        bus.publish(event)

        # CLI 通常是独立进程且无后台分发线程，同步派发一次保证命令有即时效果；
        # 若 daemon 已在运行，事件同样会由其 EventBus 消费（at-least-once）。
        dispatch_thread = getattr(bus, "_dispatch_thread", None)
        if dispatch_thread is None or not dispatch_thread.is_alive():
            bus._submit_event(event)

        print(f"已发布事件: {args.event_type}")
    else:
        print("用法: mnemos cognitive-graph {stats|reconcile|ingest}")
