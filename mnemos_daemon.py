#!/usr/bin/env python3
"""
Mnemos Daemon — 后台守护进程

职责：
- 监控 distill_queue 变化，实时触发蒸馏
- 定期同步 Memos → Wiki
- 定期采集画像信号
- 检查 KnowledgeScheduler 到期任务

启动: mnemos daemon start
停止: mnemos daemon stop
状态: mnemos daemon status
"""

import os
import sys
import time
import json
import signal
import logging
import argparse
from pathlib import Path
from datetime import datetime

# 配置日志
log_dir = Path.home() / ".mnemos"
log_dir.mkdir(parents=True, exist_ok=True)
log_file = log_dir / "daemon.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [daemon] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ],
)
logger = logging.getLogger(__name__)

PID_FILE = log_dir / "daemon.pid"


def is_daemon_running() -> bool:
    """检查 daemon 是否已在运行"""
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)  # 信号 0 用于检测进程是否存在
        return True
    except (ValueError, OSError, ProcessLookupError):
        return False


def write_pid():
    """写入 PID 文件"""
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")


def remove_pid():
    """删除 PID 文件"""
    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass


def run_daemon():
    """主循环 — 守护进程逻辑"""
    logger.info("Mnemos daemon starting...")
    write_pid()

    # 尝试导入 watchdog，回退到定时轮询
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
        WATCHDOG_AVAILABLE = True
    except ImportError:
        WATCHDOG_AVAILABLE = False
        logger.warning("watchdog 未安装，使用定时轮询模式")

    # 尝试导入 APScheduler
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        APSCHEDULER_AVAILABLE = True
    except ImportError:
        APSCHEDULER_AVAILABLE = False
        logger.warning("apscheduler 未安装，定时任务将不可用")

    # 导入核心组件
    from core.hephaestus_worker import HephaestusWorker
    from core.helios import AgentDetector

    worker = HephaestusWorker()
    detector = AgentDetector()

    # 初始化目录
    queue_dir = worker.queue_dir
    queue_dir.mkdir(parents=True, exist_ok=True)
    worker.output_dir.mkdir(parents=True, exist_ok=True)
    worker.inbox_dir.mkdir(parents=True, exist_ok=True)
    worker.archive_dir.mkdir(parents=True, exist_ok=True)

    # 处理当前队列中的任务
    pending = worker.process_all()
    if pending > 0:
        logger.info(f"启动时处理了 {pending} 个待蒸馏任务")

    # 设置文件监控或定时轮询
    if WATCHDOG_AVAILABLE:
        class DistillQueueHandler(FileSystemEventHandler):
            def on_created(self, event):
                if event.is_directory:
                    return
                if event.src_path.endswith(".json"):
                    logger.info(f"检测到新任务: {event.src_path}")
                    time.sleep(0.5)  # 等待文件写入完成
                    try:
                        worker.process_all()
                    except Exception as e:
                        logger.error(f"处理新任务失败: {e}")

        observer = Observer()
        handler = DistillQueueHandler()
        observer.schedule(handler, str(queue_dir), recursive=False)
        observer.start()
        logger.info(f"文件监控已启动: {queue_dir}")
    else:
        observer = None

    # 设置定时任务
    if APSCHEDULER_AVAILABLE:
        scheduler = BackgroundScheduler()

        # 每 5 分钟收集已完成的蒸馏结果
        @scheduler.scheduled_job("interval", minutes=5, id="collect_completed")
        def job_collect_completed():
            try:
                collected = worker.collect_completed()
                if collected > 0:
                    logger.info(f"收集了 {collected} 个完成的蒸馏结果")
            except Exception as e:
                logger.error(f"收集完成结果失败: {e}")

        # 每 1 小时采集画像信号
        @scheduler.scheduled_job("interval", hours=1, id="collect_signals")
        def job_collect_signals():
            try:
                from core.persona.daimon import SignalCollector
                collector = SignalCollector()
                results = collector.collect_all()
                total = sum(len(v) for v in results.values() if isinstance(v, list))
                logger.info(f"画像信号采集完成: {total} 条")
            except Exception as e:
                logger.error(f"信号采集失败: {e}")

        # 每天检查知识调度
        @scheduler.scheduled_job("cron", hour=9, minute=0, id="check_scheduler")
        def job_check_scheduler():
            try:
                from core.kia.chronos import KnowledgeScheduler
                s = KnowledgeScheduler()
                reminders = s.get_pending_reminders()
                missed = s.startup_compensation()
                all_r = reminders + missed
                if all_r:
                    logger.info(f"发现 {len(all_r)} 个到期知识调度任务")
            except Exception as e:
                logger.error(f"知识调度检查失败: {e}")

        scheduler.start()
        logger.info("定时任务调度器已启动")
    else:
        scheduler = None

    # 主循环 — 定时轮询（当 watchdog 不可用时）
    try:
        while True:
            if not WATCHDOG_AVAILABLE:
                # 定时轮询模式
                try:
                    worker.process_all()
                    worker.collect_completed()
                except Exception as e:
                    logger.error(f"轮询处理失败: {e}")

            # 每秒检查一次是否需要退出
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在停止...")
    finally:
        if observer:
            observer.stop()
            observer.join()
        if scheduler:
            scheduler.shutdown()
        remove_pid()
        logger.info("Mnemos daemon 已停止")


def start_daemon():
    """启动守护进程"""
    if is_daemon_running():
        print("Mnemos daemon 已在运行")
        return

    # 后台运行
    pid = os.fork()
    if pid > 0:
        print(f"Mnemos daemon 已启动 (PID: {pid})")
        print(f"日志: {log_file}")
        return

    # 子进程
    os.setsid()
    os.umask(0)

    # 第二次 fork
    pid = os.fork()
    if pid > 0:
        sys.exit(0)

    # 守护进程
    run_daemon()


def stop_daemon():
    """停止守护进程"""
    if not PID_FILE.exists():
        print("Mnemos daemon 未运行")
        return

    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        # 等待进程退出
        for _ in range(30):
            try:
                os.kill(pid, 0)
                time.sleep(0.5)
            except OSError:
                break
        remove_pid()
        print("Mnemos daemon 已停止")
    except Exception as e:
        print(f"停止 daemon 失败: {e}")


def status_daemon():
    """查看守护进程状态"""
    if is_daemon_running():
        pid = int(PID_FILE.read_text().strip())
        print(f"Mnemos daemon 运行中 (PID: {pid})")
        print(f"日志: {log_file}")

        # 显示队列统计
        try:
            from core.hephaestus_worker import HephaestusWorker
            worker = HephaestusWorker()
            stats = worker.get_stats()
            print(f"\n蒸馏队列统计:")
            print(f"  待处理: {stats['pending']}")
            print(f"  已委托: {stats['delegated']}")
        except Exception:
            pass
    else:
        print("Mnemos daemon 未运行")
        print(f"日志文件: {log_file}")
        if log_file.exists():
            print(f"\n最近日志:")
            # 显示最后 5 行日志
            lines = log_file.read_text(encoding="utf-8").strip().split("\n")
            for line in lines[-5:]:
                print(f"  {line}")


def main():
    parser = argparse.ArgumentParser(description="Mnemos Daemon")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("start", help="启动守护进程")
    sub.add_parser("stop", help="停止守护进程")
    sub.add_parser("status", help="查看状态")
    args = parser.parse_args()

    if args.cmd == "start":
        start_daemon()
    elif args.cmd == "stop":
        stop_daemon()
    elif args.cmd == "status":
        status_daemon()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
