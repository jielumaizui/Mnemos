"""Report command for Mnemos CLI."""

import logging

logger = logging.getLogger(__name__)


def cmd_report(args):
    """生成报告"""
    if args.report_cmd == "generate":
        try:
            from core.app.weekly_report import WeeklyReportGenerator

            gen = WeeklyReportGenerator()
            content = gen.generate_weekly_report()
            print("周报已生成")
            # [P0-4] 真正保存/展示周报内容，避免生成后用户看不到输出
            print(content)
        except (ImportError, AttributeError, OSError) as e:
            print(f"报告生成失败: {e}")
    else:
        print("用法: mnemos report generate")
