"""Predictive push command for Mnemos CLI."""

from __future__ import annotations

import json
from typing import Any

from core.kia.teiresias import KnowledgeMatch, PredictivePushEngine, PushDecision, check_and_push


def _match_to_dict(match: KnowledgeMatch) -> dict[str, Any]:
    return {
        "page_path": match.page_path,
        "page_title": match.page_title,
        "match_score": match.match_score,
        "match_reason": match.match_reason,
    }


def _decision_to_dict(decision: PushDecision) -> dict[str, Any]:
    return {
        "should_push": decision.should_push,
        "reason": decision.reason,
        "push_content": decision.push_content,
        "matches": [_match_to_dict(match) for match in decision.matches],
    }


def _print_decision(decision: PushDecision) -> None:
    print(f"应该推送: {'是' if decision.should_push else '否'}")
    print(f"原因: {decision.reason}")
    if decision.push_content:
        print(f"内容: {decision.push_content}")
    if decision.matches:
        print("命中知识:")
        for index, match in enumerate(decision.matches, 1):
            print(
                f"  {index}. [{match.match_score:.2f}] "
                f"{match.page_title or match.page_path}"
            )
            if match.match_reason:
                print(f"     原因: {match.match_reason}")


def _cmd_push_check(args: Any) -> int:
    decision = check_and_push(
        args.message,
        wiki_base=args.wiki_base,
        current_task=args.task,
        session_id=args.session_id,
    )
    if getattr(args, "json", False):
        print(json.dumps(_decision_to_dict(decision), ensure_ascii=False, indent=2))
    else:
        _print_decision(decision)
    return 0


def _cmd_push_stats(args: Any) -> int:
    engine = PredictivePushEngine(wiki_base=args.wiki_base)
    stats = engine.get_push_stats(days=args.days)
    if getattr(args, "json", False):
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    else:
        print(f"预测推送统计（最近 {args.days} 天）")
        print(f"总推送: {stats.get('total_pushes', 0)}")
        print(f"接受率: {stats.get('accept_rate', 0):.0%}")
        responses = stats.get("response_distribution", {})
        if responses:
            print("用户反馈:")
            for response, count in sorted(responses.items()):
                print(f"  {response}: {count}")
    return 0


def cmd_push(args: Any) -> int:
    """Handle predictive push subcommands."""
    if getattr(args, "push_cmd", "") == "check":
        return _cmd_push_check(args)
    if getattr(args, "push_cmd", "") == "stats":
        return _cmd_push_stats(args)

    print("用法: mnemos push {check|stats} ...")
    return 2
