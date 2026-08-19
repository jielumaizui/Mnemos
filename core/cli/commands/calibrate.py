"""Calibrate command for Mnemos CLI."""

import logging

from core.cli.helpers import _get_config

logger = logging.getLogger(__name__)


def cmd_calibrate(args):
    """画像校准"""
    from core.persona.calibration_cli import run_calibration
    import json

    # 先展示待处理的挑战问题（如果有）
    challenge_file = _get_config().data_dir / "calibrations" / "pending_challenges.json"
    if challenge_file.exists():
        try:
            data = json.loads(challenge_file.read_text(encoding="utf-8"))
            challenges = data.get("challenges", [])
            if challenges:
                print("=" * 60)
                print("盲区挑战问题（基于最近画像分析生成）")
                print("=" * 60)
                for i, c in enumerate(challenges, 1):
                    print(f"\n  {i}. [{c['type']}] {c['question']}")
                    print(f"     提示: {c['suggestion']}")
                print("\n" + "=" * 60)
                print("以上挑战将在校准流程中帮助你验证画像准确性。\n")
        except (json.JSONDecodeError, OSError, TypeError):
            logger.debug("校准挑战文件解析失败", exc_info=True)

    # 运行校准流程
    run_calibration()
