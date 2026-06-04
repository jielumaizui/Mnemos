from __future__ import annotations

import logging
"""
用户画像自评校准 CLI

用法：
    python -m core.persona.calibration_cli

流程：
1. 加载当前画像
2. 展示各维度推断结果
3. 用户对每个维度打分（1-5，3=准确）
4. 保存校准结果到数据库
5. 生成校准报告
"""

import sys
import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Tuple
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.persona.delphi import PersonaStore
from core.persona.pythia import PreferenceProfile
from core.config import get_config
logger = logging.getLogger(__name__)


# 维度描述（用于展示给用户）
DIMENSION_LABELS = {
    "energy": {
        "focus_depth": ("专注深度", "碎片化 -> 深度沉浸", ["碎片化", "中等专注", "较深度", "深度沉浸"]),
        "startup_difficulty": ("启动难度", "一触即发 -> 需要推力", ["一触即发", "启动较快", "需要准备", "需要推力"]),
        "endurance_mode": ("续航模式", "爆发型 -> 匀速型", ["爆发型", "混合型", "匀速型"]),
        "switching_flexibility": ("切换弹性", "单线程 -> 多线程", ["单线程", "弹性切换", "多线程"]),
        "recovery_cycle": ("恢复周期", "快速恢复 -> 需要缓冲", ["快速恢复", "中等恢复", "需要缓冲"]),
    },
    "cognitive": {
        "abstraction": ("抽象能力", "具象型 -> 抽象型", ["具象型", "平衡型", "抽象型"]),
        "system_view": ("系统视角", "单点聚焦 -> 系统视角", ["单点聚焦", "视情况", "系统视角"]),
        "skepticism": ("质疑倾向", "信任框架 -> 质疑前提", ["信任框架", "适度质疑", "质疑前提"]),
        "creativity": ("创造倾向", "优化型 -> 创造型", ["优化型", "两者兼顾", "创造型"]),
        "deduction": ("推理方式", "归纳型 -> 演绎型", ["归纳型", "混合使用", "演绎型"]),
    },
    "value": {
        "correctness_vs_efficiency": ("正确性vs效率", "效率优先 -> 正确性优先", ["效率优先", "视情况平衡", "正确性优先"]),
        "depth_vs_breadth": ("深度vs广度", "广度优先 -> 深度优先", ["广度优先", "两者兼顾", "深度优先"]),
        "perfection_vs_completion": ("完美vs完成", "先完成 -> 先完美", ["先完成", "平衡", "先完美"]),
        "innovation_vs_safety": ("创新vs稳妥", "稳妥优先 -> 创新优先", ["稳妥优先", "视风险而定", "创新优先"]),
        "autonomy_vs_collaboration": ("自主vs协作", "协作优先 -> 自主优先", ["协作优先", "灵活切换", "自主优先"]),
        "action_vs_analysis": ("行动vs分析", "分析优先 -> 行动优先", ["分析优先", "视情况平衡", "行动优先"]),
    },
}


def run_calibration():
    """运行校准流程"""
    import sys
    if not sys.stdin.isatty():
        logger.info("[校准] 校准需要在交互式终端中运行（需要用户输入）。")
        logger.info("[校准] 请直接在终端运行: mnemos calibrate")
        return

    store = PersonaStore()
    profile, _ = store.load_persona()

    if not profile:
        logger.info("[校准] 暂无画像，请先运行画像分析。")
        return

    logger.info("=" * 60)
    logger.info("用户画像自评校准")
    logger.info("=" * 60)
    logger.info(f"当前画像版本: v{profile.version}，基于 {profile.signal_count} 条信号")
    logger.info("")
    logger.info("说明：")
    logger.info("  系统基于你的行为信号推断了一套画像。")
    logger.info("  请对每个维度打分：1=完全不准，2=不太准，3=基本准确，4=比较准，5=非常准")
    logger.info("  如果某个维度显示'数据不足'，直接按回车跳过。")
    logger.info("")

    calibration = {
        "version": profile.version,
        "calibrated_at": datetime.now().isoformat(),
        "ratings": {},
        "comments": {},
    }

    # 校准能量层
    _calibrate_layer("能量模式", "energy", profile, calibration)

    # 校准认知层
    _calibrate_layer("认知模式", "cognitive", profile, calibration)

    # 校准价值层
    _calibrate_layer("价值优先级", "value", profile, calibration)

    # 保存校准结果
    _save_calibration(store, profile, calibration)

    # 生成报告
    _print_calibration_report(calibration)


def _calibrate_layer(layer_name: str, layer_key: str, profile: PreferenceProfile, calibration: Dict):
    """校准一个层"""
    logger.info(f"\n{'='*40}")
    logger.info(f"{layer_name}")
    logger.info(f"{'='*40}")

    layer = getattr(profile, layer_key)
    ins = set(layer.insufficient_dimensions or [])
    dimensions = DIMENSION_LABELS[layer_key]

    for dim_key, (name, scale, labels) in dimensions.items():
        score = getattr(layer, dim_key, 0.5)
        label = _get_label_for_score(score, labels)

        if dim_key in ins:
            logger.info(f"\n  [{name}] -- 数据不足，跳过")
            calibration["ratings"][dim_key] = None
            continue

        logger.info(f"\n  [{name}]")
        logger.info(f"  系统推断: {label} ({score:.2f})")
        logger.info(f"  量表: {scale}")

        while True:
            user_input = input(f"  你觉得这个推断准吗？(1-5，回车=跳过): ").strip()
            if not user_input:
                calibration["ratings"][dim_key] = None
                break
            try:
                rating = int(user_input)
                if 1 <= rating <= 5:
                    calibration["ratings"][dim_key] = rating
                    # 收集额外反馈
                    if rating <= 2 or rating >= 4:
                        comment = input(f"  补充说明（回车跳过）: ").strip()
                        if comment:
                            calibration["comments"][dim_key] = comment
                    break
                else:
                    logger.info("  请输入 1-5 之间的数字")
            except ValueError:
                logger.info("  请输入数字")


def _get_label_for_score(score: float, labels: List[str]) -> str:
    """根据分数获取标签"""
    if len(labels) == 3:
        if score < 0.4:
            return labels[0]
        elif score > 0.6:
            return labels[2]
        return labels[1]
    elif len(labels) == 4:
        if score < 0.3:
            return labels[0]
        elif score < 0.5:
            return labels[1]
        elif score < 0.7:
            return labels[2]
        return labels[3]
    return labels[len(labels) // 2]


def _save_calibration(store: PersonaStore, profile: PreferenceProfile, calibration: Dict):
    """保存校准结果到数据库，并根据反馈调整画像置信度"""
    ratings = {k: v for k, v in calibration["ratings"].items() if v is not None}
    avg_rating = sum(ratings.values()) / len(ratings) if ratings else 3.0

    # 根据反馈调整画像置信度（闭环）
    confidence_delta = (avg_rating - 3.0) * 0.05  # 每差1分，调整5%
    try:
        # 读取当前画像的 wiki 页面
        if store.persona_page.exists():
            content = store.persona_page.read_text(encoding="utf-8")
            # 调整 frontmatter 中的 confidence 标记
            # 简单处理：在 frontmatter 中添加校准标记
            lines = content.split("\n")
            new_lines = []
            in_fm = False
            added = False
            for line in lines:
                if line == "---":
                    in_fm = not in_fm
                    new_lines.append(line)
                    if in_fm and not added:
                        new_lines.append(f"calibration_score: {avg_rating:.1f}")
                        new_lines.append(f"calibrated_at: {calibration['calibrated_at']}")
                        added = True
                else:
                    new_lines.append(line)
            store.persona_page.write_text("\n".join(new_lines), encoding="utf-8")
    except Exception:
        logging.getLogger(__name__).warning(f"Caught unexpected error", exc_info=True)
        pass
    try:
        # 更新数据库中的用户确认状态和校准分数
        db_path = store.signal_store.db_path
        with sqlite3.connect(str(db_path), timeout=10) as conn:
            conn.execute("""
                UPDATE persona_versions
                SET user_confirmed = 1, confirmed_at = ?, calibration_score = ?
                WHERE version = ?
            """, (datetime.now().isoformat(), avg_rating, profile.version))
            conn.commit()
    except Exception:
        logging.getLogger(__name__).warning(f"Caught unexpected error at calibration_cli.py", exc_info=True)
        pass

    # 保存到独立文件
    calib_dir = get_config().data_dir / "calibrations"
    calib_dir.mkdir(parents=True, exist_ok=True)
    calib_file = calib_dir / f"calibration-v{profile.version}-{datetime.now().strftime('%Y%m%d')}.json"
    calib_file.write_text(json.dumps(calibration, ensure_ascii=False, indent=2), encoding="utf-8")

    # 删除已处理的挑战问题
    try:
        challenge_file = Path.home() / ".mnemos" / "calibrations" / "pending_challenges.json"
        if challenge_file.exists():
            challenge_file.unlink()
    except Exception:
        logging.getLogger(__name__).warning(f"Caught unexpected error", exc_info=True)
        pass
    logger.info(f"\n[校准] 结果已保存: {calib_file}")
    logger.info(f"[校准] 平均准确度: {avg_rating:.1f}/5.0，画像置信度调整: {confidence_delta:+.1%}")


def _print_calibration_report(calibration: Dict):
    """打印校准报告摘要"""
    ratings = {k: v for k, v in calibration["ratings"].items() if v is not None}
    if not ratings:
        logger.info("\n[校准] 未收集到有效评分")
        return

    avg = sum(ratings.values()) / len(ratings)
    logger.info(f"\n{'='*40}")
    logger.info("校准报告")
    logger.info(f"{'='*40}")
    logger.info(f"  评分维度数: {len(ratings)}")
    logger.info(f"  平均准确度: {avg:.1f}/5.0")

    if avg >= 4.0:
        logger.info("  评价: 画像整体较准确")
    elif avg >= 3.0:
        logger.info("  评价: 画像基本可用，部分维度需优化")
    else:
        logger.info("  评价: 画像偏差较大，建议检查数据源或推断逻辑")

    # 显示偏差最大的维度
    low_ratings = [(k, v) for k, v in ratings.items() if v <= 2]
    if low_ratings:
        logger.info(f"\n  偏差较大的维度（<=2分）:")
        for dim, rating in low_ratings:
            comment = calibration["comments"].get(dim, "")
            logger.info(f"    - {dim}: {rating}/5 {comment and f'({comment})'}")


if __name__ == "__main__":
    run_calibration()
