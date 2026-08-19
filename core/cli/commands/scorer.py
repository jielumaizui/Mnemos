"""Scorer command for Mnemos CLI."""

import logging

logger = logging.getLogger(__name__)


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
                fails = (
                    f" ({info['consecutive_failures']}次失败)"
                    if info["consecutive_failures"] > 0
                    else ""
                )
                print(f"  {name}: {status} | {info['trigger']}{fails}")
        except (ImportError, AttributeError, OSError) as e:
            print(f"状态查询失败: {e}")

    elif args.scorer_cmd == "retrain":
        print(
            "旧 scorer retrain 已停用：模型只可由 canonical "
            "TrainingGovernanceStore 的 current-manifest run 生成和应用。"
        )

    elif args.scorer_cmd == "rollback":
        print(
            "旧 scorer rollback 已停用：修正会追加 stale lineage，"
            "随后从完整 current manifest 重建；不会重新激活 legacy 版本。"
        )

    else:
        print("用法: mnemos scorer {status|retrain|rollback}")
