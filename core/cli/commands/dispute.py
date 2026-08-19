# -*- coding: utf-8 -*-
"""
mnemos dispute — 争议仲裁管理

- scan [--max-disputes N]: 手动触发争议扫描
- list [--unresolved-only]: 列出争议页面
- resolve <page_path> --resolution <adopt_new|keep_old|keep_both|need_more_info>
  [--context TEXT]: 解决争议
- rollback-context <page_path>: 回滚同步到原始页面的 keep_both 上下文块
- stats: 统计争议数量
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Dict

from core import config as _config_mod
from core.app.dispute_resolver import DisputeResolver

REASONABLE_RESOLUTIONS = {"adopt_new", "keep_old", "keep_both", "need_more_info"}


def _dispute_path_arg(args) -> str | None:
    page_path = getattr(args, "page_path", None)
    if not page_path:
        return None
    # 允许传入绝对路径或相对 wiki 的路径，统一为相对路径
    p = Path(page_path)
    wiki = Path(_config_mod.get_config().wiki_dir).expanduser()
    if p.is_absolute() and wiki in p.parents:
        return str(p.relative_to(wiki))
    return page_path  # type: ignore[no-any-return]


def _resolve_page_path(page_path: str | None, cfg) -> Path | None:
    """校验争议页面路径并返回完整路径；无效时打印错误并返回 None。"""
    if not page_path:
        print("错误: 缺少争议页面路径")
        return None
    full_path = Path(cfg.wiki_dir).expanduser() / page_path
    if not full_path.exists():
        print(f"错误: 争议页面不存在 {page_path}")
        return None
    return full_path


def _cmd_dispute_scan(args, resolver: DisputeResolver, _cfg) -> int:
    """扫描争议。"""
    max_disputes = getattr(args, "max_disputes", None)
    report = resolver.scan(max_disputes=max_disputes)
    print(
        f"争议扫描完成: "
        f"conflicts_found={report.get('conflicts_found', 0)}, "
        f"auto_resolved={report.get('auto_resolved', 0)}, "
        f"merged={report.get('merged', 0)}, "
        f"disputes_created={report.get('disputes_created', 0)}, "
        f"skipped={report.get('skipped', 0)}"
    )
    return 0


def _cmd_dispute_list(args, resolver: DisputeResolver, cfg) -> int:
    """列出争议页面。"""
    unresolved_only = getattr(args, "unresolved_only", False)
    if unresolved_only:
        disputes = resolver.get_unresolved_disputes()
    else:
        dispute_dir = Path(_config_mod.get_config().wiki_dir).expanduser() / "08-Disputes"
        disputes = []
        if dispute_dir.exists():
            wiki_dir = Path(cfg.wiki_dir).expanduser()
            for md_file in sorted(dispute_dir.glob("*.md")):
                disputes.append(
                    {
                        "path": str(md_file.relative_to(wiki_dir)),
                        "title": md_file.stem,
                        "days_old": 0,
                        "needs_escalation": False,
                    }
                )
    if not disputes:
        print("暂无争议页面" if not unresolved_only else "暂无未解决争议")
        return 0
    print(f"共 {len(disputes)} 条争议")
    for d in disputes:
        escalation = " [需升级]" if d.get("needs_escalation") else ""
        print(f"  - {d['path']} ({d['title']}){escalation}")
    return 0


def _cmd_dispute_resolve(args, resolver: DisputeResolver, cfg) -> int:
    """解决指定争议。"""
    page_path = _dispute_path_arg(args)
    resolution = getattr(args, "resolution", None)
    context = getattr(args, "context", "")

    if not page_path:
        print("错误: 缺少争议页面路径")
        return 1
    if resolution not in REASONABLE_RESOLUTIONS:
        print(
            f"错误: 无效解决方案 {resolution!r}，可选: {', '.join(sorted(REASONABLE_RESOLUTIONS))}"
        )
        return 1

    if _resolve_page_path(page_path, cfg) is None:
        return 1
    assert page_path is not None

    resolver.resolve_dispute(page_path, resolution, context=context)
    print(f"已解决争议: {page_path} -> {resolution}")
    return 0


def _cmd_dispute_rollback(args, resolver: DisputeResolver, cfg) -> int:
    """回滚争议上下文。"""
    page_path = _dispute_path_arg(args)
    if _resolve_page_path(page_path, cfg) is None:
        return 1
    assert page_path is not None

    updated = resolver.rollback_resolution_context(page_path)
    print(f"已回滚争议上下文: {page_path}, pages_updated={updated}")
    return 0


def _cmd_dispute_stats(args, resolver: DisputeResolver, _cfg) -> int:  # noqa: U100
    """统计争议数量。"""
    dispute_dir = Path(_config_mod.get_config().wiki_dir).expanduser() / "08-Disputes"
    total = 0
    if dispute_dir.exists():
        total = len(list(dispute_dir.glob("*.md")))
    unresolved = resolver.get_unresolved_disputes()
    escalated = sum(1 for d in unresolved if d.get("needs_escalation"))
    print(f"争议统计: 总数={total}, 未解决={len(unresolved)}, 需升级={escalated}")
    return 0


def cmd_dispute(args) -> int:
    """争议仲裁 CLI 入口。"""
    cfg = _config_mod.get_config()
    resolver = DisputeResolver(wiki_base=str(cfg.wiki_dir))
    cmd = getattr(args, "dispute_cmd", None) or ""

    handlers: dict[str, Callable[..., int]] = {
        "scan": _cmd_dispute_scan,
        "list": _cmd_dispute_list,
        "resolve": _cmd_dispute_resolve,
        "rollback-context": _cmd_dispute_rollback,
        "stats": _cmd_dispute_stats,
        "weights": _cmd_dispute_weights,
        "show": _cmd_dispute_show,
    }
    handler = handlers.get(cmd)
    if handler is None:
        print("未知子命令")
        return 1
    return handler(args, resolver, cfg)


def _cmd_dispute_weights(args, _resolver=None, _cfg=None) -> int:
    """查看/调整/学习争议仲裁权重。"""
    from core.app.dispute_scorer import DisputeScorer

    scorer = DisputeScorer(wiki_dir=Path(_config_mod.get_config().wiki_dir).expanduser())

    if getattr(args, "reset", False):
        scorer.reset_weights()
        print("已清除 state 权重，回退到 config/默认值")
        print(f"当前来源: {scorer.weight_source()}")
        _print_weights(scorer.current_weights())
        return 0

    if getattr(args, "learn", False):
        learned = scorer.learn()
        if learned:
            print("自适应权重学习完成")
            print(f"当前来源: {scorer.weight_source()}")
            _print_weights(scorer.current_weights())
        else:
            print("未产生权重更新（学习未启用或样本不足）")
        return 0

    sets = getattr(args, "set_weights", None)
    if sets:
        new_weights = dict(scorer.current_weights())
        for item in sets:
            if "=" not in item:
                print(f"错误: --set 格式应为 dim=value， got {item!r}")
                return 1
            dim, value = item.split("=", 1)
            dim = dim.strip()
            if dim not in new_weights:
                print(f"错误: 未知维度 {dim!r}，可选: {', '.join(sorted(new_weights))}")
                return 1
            try:
                new_weights[dim] = float(value)
            except ValueError:
                print(f"错误: {value!r} 不是有效数值")
                return 1
        scorer.save_weights_to_state(new_weights)
        print("已保存权重到 state")

    print(f"当前来源: {scorer.weight_source()}")
    _print_weights(scorer.current_weights())
    return 0


def _print_weights(weights: Dict[str, float]) -> None:
    print("当前权重:")
    for dim, w in sorted(weights.items()):
        print(f"  {dim}: {w:.4f}")


def _cmd_dispute_show(args, _resolver=None, _cfg=None) -> int:
    """展示指定争议页的评分详情。"""
    page_path = _dispute_path_arg(args)
    if not page_path:
        print("错误: 缺少争议页面路径")
        return 1

    full_path = Path(_config_mod.get_config().wiki_dir).expanduser() / page_path
    if not full_path.exists():
        print(f"错误: 争议页面不存在 {page_path}")
        return 1

    from core.frontmatter import parse_frontmatter
    from core.app.dispute_scorer import DisputeScorer, RelationFeatures

    content = full_path.read_text(encoding="utf-8")
    frontmatter, _ = parse_frontmatter(content)
    if not frontmatter:
        print("错误: 无法解析争议页 frontmatter")
        return 1

    topic = frontmatter.get("topic", full_path.stem)
    conflict_strength = frontmatter.get("conflict_strength", 0.0)
    is_core = frontmatter.get("is_core_knowledge", False)
    features_a = frontmatter.get("features_a")
    features_b = frontmatter.get("features_b")

    print(f"争议: {topic}")
    print(f"冲突强度: {conflict_strength} | 核心知识: {'是' if is_core else '否'}")

    scorer = DisputeScorer(wiki_dir=Path(_config_mod.get_config().wiki_dir).expanduser())
    weights = scorer.current_weights()

    if features_a and features_b:
        fa = RelationFeatures.from_dict(features_a)
        fb = RelationFeatures.from_dict(features_b)
        score_a = scorer.composite_score(fa)
        score_b = scorer.composite_score(fb)
        gap = abs(score_a - score_b)
        print(f"\n来源: {scorer.weight_source()}")
        print("维度得分:")
        for dim in sorted(weights):
            va = getattr(fa, dim)
            vb = getattr(fb, dim)
            w = weights[dim]
            print(f"  {dim}: {va:.3f} vs {vb:.3f} (权重 {w:.3f})")
        print(f"\n综合分: {score_a:.3f} vs {score_b:.3f} (差距 {gap:.3f})")
        if gap >= scorer.auto_gap:
            print("建议动作: auto_resolve")
        elif gap >= scorer.merge_gap:
            print("建议动作: merge")
        else:
            print("建议动作: create_dispute")
    else:
        print("\n该争议页缺少评分明细（旧页面）")

    return 0
