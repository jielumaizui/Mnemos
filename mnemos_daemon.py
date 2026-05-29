#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemos Daemon — 后台守护进程 (v2.0.0)

职责（全自动闭环）：
1. L1同步：监控Claude session文件变化 → 自动进Memos
2. 蒸馏：distill_queue新任务 → 触发蒸馏
3. 合并：Memos → 蒸馏 → Wiki（Orchestrator全流程）
4. 心跳：定期健康检查 + 热力衰减
5. 收件箱：扫描inbox目录 → 处理文件进Memos
6. 画像：定期采集信号

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
import threading
from pathlib import Path
from datetime import datetime, timezone
from typing import List

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

# 全局停止事件
_stop_event = threading.Event()


def _is_process_running(pid: int) -> bool:
    """跨平台进程存在性检测"""
    if sys.platform == "win32":
        import subprocess
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )
            return str(pid) in result.stdout
        except Exception:
            logger.warning(f"Unexpected error in mnemos_daemon.py", exc_info=True)
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


def is_daemon_running() -> bool:
    """检查 daemon 是否已在运行"""
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        return _is_process_running(pid)
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


# ==================== 自动化服务 ====================

def service_l1_sync(stop_event: threading.Event):
    """
    服务1: L1同步 — 通过 CaptureService 监控所有 Agent 源文件变化，自动同步到 Memos

    架构约束：
    - 所有 AgentSource 必须走 CaptureService → CaptureQueue → CaptureWorkerPool → SyncEngine
    - 不复用旧的 sync_engine.sync_batch() 直接写入路径
    - 原始 L0 解析能力复用（discover_sessions + parse_turns），写入路径废弃/下沉
    """
    logger.info("[L1同步] 服务启动")
    try:
        from core.sync_framework.capture_service import CaptureService
        from core.sync_framework.registry import AgentRegistry
        from core.config import get_config

        config = get_config()

        # 1. 先注册内置 Agent（确保 Worker 能正确解析来源标签）
        AgentRegistry.register_builtin_agents()

        # 2. 初始化 CaptureService（consumer 模式，启动 WorkerPool 消费 pending 队列）
        capture_service = CaptureService(start_worker=True)

        # 定时轮询模式（watchdog 由 TriggerDispatcher 管理）
        poll_interval = 30  # 30秒轮询一次

        while not stop_event.is_set():
            try:
                # 重新发现活跃 Agent
                agents = AgentRegistry.auto_discover()
                logger.debug(f"[L1同步] 发现 {len(agents)} 个活跃 Agent 源")

                for source in agents:
                    try:
                        sessions = source.discover_sessions()
                        if not sessions:
                            continue

                        queued_count = 0
                        dup_count = 0
                        bp_count = 0
                        error_count = 0

                        for session_info in sessions:
                            try:
                                turns = source.parse_turns(session_info.source_path)
                                if not turns:
                                    continue

                                # 确保按 turn_number 顺序入队，避免增量跳过逻辑错乱
                                turns = sorted(turns, key=lambda t: t.turn_number)

                                # 复用旧 sync_session 的 session lifecycle hooks
                                try:
                                    from core.mnemos_bus import publish_event
                                    publish_event("polled", source.name, {
                                        "file_path": str(session_info.source_path),
                                        "session_id": session_info.session_id,
                                    })
                                except Exception:
                                    pass

                                context = source.on_session_start(
                                    session_info.session_id,
                                    {"working_dir": session_info.working_dir, "agent": source.name},
                                )

                                try:
                                    for turn in turns:
                                        result = capture_service.capture_turn(
                                            source_agent=source.name,
                                            session_id=session_info.session_id,
                                            turn_number=turn.turn_number,
                                            user_content=turn.user_content,
                                            assistant_content=turn.assistant_content,
                                            timestamp=turn.timestamp,
                                            model=source.model_tag,
                                            cwd=str(session_info.source_path),
                                            metadata=turn.metadata,
                                        )
                                        status = result.get("status")
                                        if status == "queued":
                                            queued_count += 1
                                        elif status == "duplicate":
                                            dup_count += 1
                                        elif status == "backpressure":
                                            bp_count += 1
                                        elif status == "error":
                                            error_count += 1

                                    # 触发异步 end_session，让 Worker 优先 flush 该 session
                                    capture_service.end_session(source.name, session_info.session_id)
                                finally:
                                    # KIA Hook: session_end（无论入队是否成功都执行，避免泄漏）
                                    all_messages = []
                                    for turn in turns:
                                        if turn.user_content:
                                            all_messages.append({"role": "user", "content": turn.user_content})
                                        if turn.assistant_content:
                                            all_messages.append({"role": "assistant", "content": turn.assistant_content})
                                    source.on_session_end(session_info.session_id, all_messages)

                            except Exception as e:
                                logger.error(
                                    f"[L1同步] {source.name}/{session_info.session_id} 解析失败: {e}"
                                )

                        if queued_count > 0 or dup_count > 0 or bp_count > 0 or error_count > 0:
                            logger.info(
                                f"[L1同步] {source.name}: "
                                f"queued={queued_count}, "
                                f"duplicate={dup_count}, "
                                f"backpressure={bp_count}, "
                                f"error={error_count}"
                            )
                    except Exception as e:
                        logger.error(f"[L1同步] {source.name} 同步失败: {e}")
            except Exception as e:
                logger.error(f"[L1同步] 轮询失败: {e}")

            stop_event.wait(timeout=poll_interval)

        logger.info("[L1同步] 已停止")
    except Exception as e:
        logger.error(f"[L1同步] 服务异常: {e}")


def service_distill_and_merge(stop_event: threading.Event):
    """
    服务2: 蒸馏+合并 — 高频处理 distill_queue + 定时运行 KIA 调度器

    高频（每60秒）：
    - 处理 distill_queue 中的待蒸馏任务
    - 收集已完成的蒸馏结果

    中频（每5分钟）：
    - 运行 KnowledgeScheduler.tick() 并行调度 KIA 步骤
    """
    logger.info("[蒸馏合并] 服务启动")
    from core.config import get_config

    poll_interval = 60        # 60秒轮询一次
    tick_interval = 5 * 60    # 5分钟运行一次 KIA 调度
    tick_counter = 0

    # 初始化 KnowledgeScheduler
    scheduler = None
    try:
        from core.kia.chronos import KnowledgeScheduler
        scheduler = KnowledgeScheduler(max_workers=4)
        scheduler.register_all_default_steps()
        logger.info("[蒸馏合并] KnowledgeScheduler 已初始化（16 步骤）")
    except Exception as e:
        logger.warning(f"[蒸馏合并] KnowledgeScheduler 初始化失败: {e}，回退到 Orchestrator")

    while not stop_event.is_set():
        try:
            # 高频：处理 distill_queue
            from core.hephaestus_worker import HephaestusWorker
            worker = HephaestusWorker()
            pending = worker.process_all()
            if pending > 0:
                logger.info(f"[蒸馏合并] 处理了 {pending} 个待蒸馏任务")

            collected = worker.collect_completed()
            if collected > 0:
                logger.info(f"[蒸馏合并] 收集了 {collected} 个完成的蒸馏结果")

            # 中频：运行 KIA 调度器
            tick_counter += poll_interval
            if tick_counter >= tick_interval:
                tick_counter = 0
                if scheduler:
                    results = scheduler.tick()
                    if results:
                        ok_count = sum(1 for r in results.values() if r.get("status") == "ok")
                        err_count = sum(1 for r in results.values() if r.get("status") == "error")
                        skip_count = sum(1 for r in results.values() if r.get("status") == "skipped")
                        logger.info(f"[蒸馏合并] KIA tick: {ok_count}成功, {err_count}失败, {skip_count}跳过")
                else:
                    # 回退到 Orchestrator
                    from core.orchestrator import Orchestrator
                    orch = Orchestrator(wiki_dir=get_config().wiki_dir)
                    report = orch.run_full()
                    logger.info(f"[蒸馏合并] Orchestrator 完成: {len(report.get('errors', []))} 错误")

        except Exception as e:
            logger.error(f"[蒸馏合并] 运行失败: {e}")

        stop_event.wait(timeout=poll_interval)

    logger.info("[蒸馏合并] 服务已停止")


def service_heartbeat(stop_event: threading.Event):
    """
    服务3: 心跳守护 — OpsScorer 健康评分 + 热力衰减
    每60秒运行一次
    """
    logger.info("[心跳] 服务启动")
    interval = 60

    while not stop_event.is_set():
        try:
            # 优先使用 OpsScorer
            try:
                from core.scoring.scorers.ops_scorer import OpsScorer
                scorer = OpsScorer()
                result = scorer.score_system()
                health = result.get("health_score", 0)
                if health > 0:
                    logger.debug(f"[心跳] 系统健康度: {health:.1f}")
            except ImportError:
                # 回退到 HeartbeatDaemon
                from core.heartbeat import HeartbeatDaemon
                daemon = HeartbeatDaemon()
                daemon.run_once()
        except Exception as e:
            logger.error(f"[心跳] 运行失败: {e}")

        stop_event.wait(timeout=interval)

    logger.info("[心跳] 服务已停止")


def service_inbox_scanner(stop_event: threading.Event):
    """
    服务4: 收件箱扫描 — 扫描inbox目录，处理文件进Memos
    每10分钟扫描一次
    """
    logger.info("[收件箱] 服务启动")
    from core.config import get_config

    interval = 10 * 60  # 10分钟

    while not stop_event.is_set():
        try:
            from core.kia.knowledge_inbox import KnowledgeInboxProcessor
            inbox = KnowledgeInboxProcessor()
            inbox_dir = get_config().data_dir / "inbox"
            if inbox_dir.exists():
                result = inbox.scan_inbox()
                processed = result.get("processed", 0) if isinstance(result, dict) else 0
                errors = result.get("errors", 0) if isinstance(result, dict) else 0
                if processed > 0 or errors > 0:
                    logger.info(f"[收件箱] 扫描完成: {processed}已处理, {errors}错误")
        except ImportError as e:
            logger.warning(f"[收件箱] knowledge_inbox不可用，服务降级: {e}")
        except Exception as e:
            logger.error(f"[收件箱] 扫描失败: {e}")

        stop_event.wait(timeout=interval)

    logger.info("[收件箱] 服务已停止")


def service_signal_collector(stop_event: threading.Event):
    """
    服务5: 画像信号采集 — 定期采集信号更新用户画像
    每小时运行一次，轮询所有 Agent 适配器。
    采集后检查信号总数，达到阈值时自动触发画像分析。
    """
    logger.info("[画像] 服务启动")

    interval = 60 * 60  # 1小时
    MIN_SIGNALS_FOR_ANALYSIS = 10  # 触发画像分析的最小信号数

    while not stop_event.is_set():
        try:
            from integrations.olympus import AgentRegistry
            from core.persona.psyche import get_signal_store

            store = get_signal_store()
            adapters = AgentRegistry.discover_all()
            total_signals = 0

            for agent in adapters:
                try:
                    signals = agent.collect_signals(days=7)
                    for sig in signals:
                        if isinstance(sig, dict):
                            # 统一转换为 SessionSignal，确保 agent 字段正确
                            from core.persona.psyche import SessionSignal
                            sid = sig.get("session_id") or ""
                            if not sid:
                                continue
                            session_signal = SessionSignal(
                                session_id=sid,
                                timestamp=sig.get("timestamp", datetime.now(timezone.utc).isoformat()),
                                task_type=sig.get("task_type", "unknown"),
                                task_subtype=sig.get("task_subtype", ""),
                                user_msg_count=sig.get("user_msg_count", 0),
                                avg_user_msg_length=sig.get("avg_user_msg_length", 0),
                                correction_count=sig.get("correction_count", 0),
                                follow_up_depth=sig.get("follow_up_depth", 0),
                                termination_type=sig.get("termination_type", "unknown"),
                                output_type=sig.get("output_type", "discussion"),
                                working_dir=sig.get("working_dir", ""),
                                agent=agent.name,
                            )
                            store.insert_session_signal(session_signal)
                            total_signals += 1
                except Exception as e:
                    logger.warning(f"[画像] {agent.name} 信号采集失败: {e}")

            if total_signals > 0:
                logger.info(f"[画像] 信号采集完成: {total_signals}条 (来自 {len(adapters)} 个 Agent)")

            # 采集后检查信号总数，达到阈值时自动触发画像分析
            stats = store.get_signal_stats(days=30)
            total_all = sum(v for v in stats.values() if v > 0)
            if total_all >= MIN_SIGNALS_FOR_ANALYSIS:
                _trigger_persona_analysis()
            else:
                logger.debug(f"[画像] 信号数 {total_all} < {MIN_SIGNALS_FOR_ANALYSIS}，暂不分析")
        except Exception as e:
            logger.error(f"[画像] 信号采集失败: {e}")

        stop_event.wait(timeout=interval)

    logger.info("[画像] 服务已停止")


def _trigger_persona_analysis():
    """触发画像分析（被 signal_collector 调用）"""
    try:
        from core.persona.pythia import analyze_preferences
        from core.persona.delphi import get_persona_store

        logger.info("[画像] 信号数达标，触发画像分析...")
        profile = analyze_preferences(days=30)
        if profile:
            store = get_persona_store()
            store.save_persona(profile)
            logger.info(f"[画像] 画像分析完成，signal_count={profile.signal_count}")
            # 生成盲区挑战问题，供用户校准
            _generate_blindspot_challenges(profile)
    except Exception as e:
        logger.error(f"[画像] 画像分析失败: {e}")


def _generate_blindspot_challenges(profile):
    """基于画像生成盲区挑战问题，写入待处理队列"""
    try:
        challenges = []
        # 能量层挑战：对高置信度维度提出反向假设
        if profile.energy.confidence >= 0.6:
            if profile.energy.focus_depth > 0.7:
                challenges.append({
                    "dimension": "energy.focus_depth",
                    "type": "反向验证",
                    "question": "系统推断你倾向于深度专注。但最近你是否有刻意浅层浏览、快速扫过大量信息的时刻？",
                    "suggestion": "如果经常有这样的时刻，你的专注模式可能比画像显示的更灵活。",
                })
            if profile.energy.switching_flexibility < 0.3:
                challenges.append({
                    "dimension": "energy.switching_flexibility",
                    "type": "盲区检测",
                    "question": "画像显示你偏单线程。你是否注意到有些任务并行处理反而更高效？",
                    "suggestion": "多线程不一定意味着分心，有些组合任务可以并行而不损失质量。",
                })
        # 认知层挑战
        if profile.cognitive.confidence >= 0.6:
            if profile.cognitive.skepticism > 0.7:
                challenges.append({
                    "dimension": "cognitive.skepticism",
                    "type": "反向验证",
                    "question": "画像显示你经常质疑前提。但你是否有时会过度质疑，导致决策拖延？",
                    "suggestion": "质疑是优点，但时机和对象很重要。",
                })
        # 价值层挑战
        if profile.value.confidence >= 0.6:
            if profile.value.perfection_vs_completion > 0.7:
                challenges.append({
                    "dimension": "value.perfection_vs_completion",
                    "type": "盲区检测",
                    "question": "画像显示你追求完美。回想一下，是否有'先完成再优化'反而效果更好的经历？",
                    "suggestion": "完成度有时候比完美度更有价值，尤其是在信息不完备的早期阶段。",
                })

        if challenges:
            calib_dir = Path.home() / ".mnemos" / "calibrations"
            calib_dir.mkdir(parents=True, exist_ok=True)
            challenge_file = calib_dir / "pending_challenges.json"
            challenge_file.write_text(
                json.dumps({
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "profile_version": profile.version,
                    "challenges": challenges,
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info(f"[画像] 已生成 {len(challenges)} 个盲区挑战问题")
    except Exception as e:
        logger.debug(f"[画像] 生成挑战问题失败: {e}")


def service_persona_analyzer(stop_event: threading.Event):
    """
    服务6: 画像分析 — 定期全量分析生成用户画像
    每天运行一次（24小时），确保画像定期更新。
    """
    logger.info("[画像分析] 服务启动")

    interval = 24 * 60 * 60  # 24小时

    while not stop_event.is_set():
        try:
            _trigger_persona_analysis()
        except Exception as e:
            logger.error(f"[画像分析] 定期分析失败: {e}")

        stop_event.wait(timeout=interval)

    logger.info("[画像分析] 服务已停止")


def service_event_bus(stop_event: threading.Event):
    """
    服务7: 事件总线 — 轮询并处理所有 Agent 发布的事件
    每10秒运行一次
    """
    logger.info("[事件总线] 服务启动")

    interval = 10  # 10秒

    from core.mnemos_bus import EventProcessor, EventBus

    processor = EventProcessor()
    bus = EventBus()

    # 注册事件处理器
    def _handle_session_start(event):
        """处理 session.start 事件：KIA 预加载"""
        try:
            from core.kia.preflight import run_preflight

            payload = event.payload
            user_message = payload.get("user_message", "")
            working_dir = payload.get("working_dir", "")
            agent = event.agent

            # 统一 KIA 预加载入口（Agent-agnostic）
            context = run_preflight(agent, user_message, working_dir)
            if context:
                # 将结果写回事件 payload，供 Agent 读取
                payload["kia_context"] = context
                logger.info(f"[事件总线] KIA 预加载完成: agent={agent}")
        except Exception as e:
            logger.warning(f"[事件总线] session.start 处理失败: {e}")

    def _handle_session_end(event):
        """处理 session.end 事件：入蒸馏队列 + 采集信号"""
        try:
            payload = event.payload
            session_id = payload.get("session_id")
            messages = payload.get("messages", [])
            meta = payload.get("meta", {})

            if messages and session_id:
                # 入蒸馏队列
                from core.kia.amphora import enqueue
                enqueue(session_id=session_id, messages=messages, meta=meta)
                logger.info(f"[事件总线] 蒸馏入队: {session_id}")

                # 采集信号
                from core.persona.psyche import get_signal_store, SessionSignal
                store = get_signal_store()

                user_msgs = [m for m in messages if m.get("role") == "user"]
                if user_msgs:
                    user_contents = [m.get("content", "") for m in user_msgs]
                    avg_len = sum(len(c) for c in user_contents) / max(len(user_contents), 1)

                    signal = SessionSignal(
                        session_id=session_id,
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        task_type=meta.get("source", "unknown"),
                        task_subtype="",
                        user_msg_count=len(user_msgs),
                        avg_user_msg_length=avg_len,
                        correction_count=0,
                        follow_up_depth=0,
                        termination_type="unknown",
                        output_type="discussion",
                        working_dir=meta.get("working_dir", ""),
                        agent=event.agent,
                    )
                    store.insert_session_signal(signal)
        except Exception as e:
            logger.warning(f"[事件总线] session.end 处理失败: {e}")

    def _handle_distill_request(event):
        """处理 distill.request 事件：触发蒸馏"""
        try:
            from core.hephaestus_worker import HephaestusWorker
            worker = HephaestusWorker()
            processed = worker.process_all()
            if processed > 0:
                logger.info(f"[事件总线] 处理 {processed} 个蒸馏任务")
        except Exception as e:
            logger.warning(f"[事件总线] distill.request 处理失败: {e}")

    processor.register("session.start", _handle_session_start)
    processor.register("session.end", _handle_session_end)
    processor.register("distill.request", _handle_distill_request)

    # KIA 事件触发步骤：由事件总线直接调用
    def _handle_page_created(event):
        """页面创建 → 直接触发 connect_worker"""
        try:
            from core.kia.chronos import KnowledgeScheduler
            scheduler = KnowledgeScheduler()
            result = scheduler.trigger_event("page.created", event.payload)
            logger.info(f"[事件总线] connect_worker: {result.get('status')}")
        except Exception as e:
            logger.warning(f"[事件总线] page.created 处理失败: {e}")

    def _handle_page_modified(event):
        """页面修改 → 直接触发 iteration_tracker"""
        try:
            from core.kia.chronos import KnowledgeScheduler
            scheduler = KnowledgeScheduler()
            result = scheduler.trigger_event("page.modified", event.payload)
            logger.info(f"[事件总线] iteration_tracker: {result.get('status')}")
        except Exception as e:
            logger.warning(f"[事件总线] page.modified 处理失败: {e}")

    def _handle_message_exchanged(event):
        """消息交换 → 直接触发 KIA 守护"""
        try:
            from core.kia.chronos import KnowledgeScheduler
            scheduler = KnowledgeScheduler()
            result = scheduler.trigger_event("message.exchanged", event.payload)
            if result.get("status") == "error":
                logger.warning(f"[事件总线] KIA guard: {result.get('error')}")
        except Exception as e:
            logger.debug(f"[事件总线] message.exchanged 处理: {e}")

    processor.register("page.created", _handle_page_created)
    processor.register("page.modified", _handle_page_modified)
    processor.register("message.exchanged", _handle_message_exchanged)

    while not stop_event.is_set():
        try:
            stats_before = bus.stats()
            processed = processor.process_all(limit=50)
            if processed > 0:
                logger.info(f"[事件总线] 处理 {processed} 个事件")
        except Exception as e:
            logger.error(f"[事件总线] 运行失败: {e}")

        stop_event.wait(timeout=interval)

    logger.info("[事件总线] 服务已停止")


# ==================== 主循环 ====================

def _run_startup_compensation():
    """启动补偿：扫描关机期间过期的复盘预约，立即补发。

    蓝图 §9 关键边界：
    - 用户预约过期 → 直接打开 Obsidian
    - 系统提醒过期 → 走组合权重判断
    """
    try:
        from core.app.forced_retrospective import ForcedRetrospective
        fr = ForcedRetrospective()
        expired = fr.startup_compensation()
        if expired:
            logger.info(f"启动补偿: {len(expired)} 个过期复盘任务已处理")
        else:
            logger.debug("启动补偿: 无过期任务")
    except Exception as e:
        logger.warning(f"启动补偿执行失败: {e}")


def _run_preflight_checks() -> List[str]:
    """启动前置检查，返回警告列表（非阻塞，仅日志提示）"""
    warnings = []

    # 1. 目录可写检查
    from core.config import get_config
    config = get_config()
    critical_dirs = [
        ("数据目录", config.data_dir),
        ("Wiki 目录", config.wiki_dir),
        ("蒸馏队列", config.claude_data_dir / "distill_queue"),
        ("蒸馏输出", Path.home() / ".mnemos" / "distill_output"),
    ]
    for name, path in critical_dirs:
        try:
            path.mkdir(parents=True, exist_ok=True)
            test_file = path / ".mnemos_writable_test"
            test_file.write_text("test")
            test_file.unlink()
        except Exception as e:
            warnings.append(f"{name} ({path}) 不可写: {e}")

    # 2. Memos API 可访问性（如果启用）
    if config.memos_enabled:
        try:
            import requests
            # 轻量探测：尝试访问 memos 列表（不依赖 token 有效性）
            resp = requests.get(
                f"{config.memos_api_url}/api/v1/memos",
                timeout=5,
            )
            if resp.status_code not in (200, 401):
                resp.raise_for_status()
        except Exception as e:
            warnings.append(f"Memos API 连接异常: {e}")

    # 3. Agent 可用性检查
    try:
        from integrations.olympus import AgentRegistry
        adapters = AgentRegistry.discover_all()
        available = [a for a in adapters if a.is_available()]
        if not available:
            warnings.append("未检测到可用 Agent，蒸馏任务将无法处理")
        else:
            logger.info(f"[前置检查] 检测到 {len(available)} 个可用 Agent: {[a.name for a in available]}")
    except Exception as e:
        warnings.append(f"Agent 检测失败: {e}")

    # 4. 数据库可访问性
    try:
        from core.persona.psyche import get_signal_store
        store = get_signal_store()
        stats = store.get_signal_stats(days=1)
        logger.info(f"[前置检查] 信号数据库正常，最近1天 {sum(stats.values())} 条信号")
    except Exception as e:
        warnings.append(f"信号数据库访问异常: {e}")

    return warnings


def run_daemon():
    """主循环 — 启动所有自动化服务"""
    logger.info("=" * 50)
    logger.info("Mnemos daemon v2.0.0 starting...")
    logger.info("=" * 50)
    write_pid()

    # 启动前置检查
    preflight_warnings = _run_preflight_checks()
    for w in preflight_warnings:
        logger.warning(f"[前置检查] {w}")
    if preflight_warnings:
        logger.warning(f"[前置检查] 共 {len(preflight_warnings)} 项警告，服务继续启动")
    else:
        logger.info("[前置检查] 全部通过")

    # 确保数据目录存在
    from core.config import get_config
    config = get_config()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    (config.data_dir / "inbox").mkdir(parents=True, exist_ok=True)

    # 初始化所有数据库表（幂等）
    try:
        from core.db_init import init_all_tables
        init_all_tables()
        logger.info("[DB] 数据库表初始化完成")
    except Exception as e:
        logger.warning(f"[DB] 数据库表初始化失败（非阻塞）: {e}")

    # 启动补偿：检查关机期间过期的复盘预约
    _run_startup_compensation()

    # 注册信号处理（优雅退出）
    def handle_signal(signum, frame):
        logger.info(f"收到信号 {signum}，正在停止所有服务...")
        _stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # 启动所有服务线程
    services = [
        ("L1同步", service_l1_sync),
        ("蒸馏合并", service_distill_and_merge),
        ("心跳", service_heartbeat),
        ("收件箱", service_inbox_scanner),
        ("画像信号", service_signal_collector),
        ("画像分析", service_persona_analyzer),
        ("事件总线", service_event_bus),
    ]

    threads = []
    for name, func in services:
        t = threading.Thread(target=func, args=(_stop_event,), name=name, daemon=True)
        t.start()
        threads.append(t)
        logger.info(f"服务 [{name}] 已启动 (thread: {t.ident})")

    logger.info(f"所有 {len(threads)} 个服务已启动")
    logger.info(f"日志文件: {log_file}")
    logger.info(f"数据目录: {config.data_dir}")
    logger.info(f"Wiki目录: {config.wiki_dir}")

    # 主线程等待停止信号
    try:
        while not _stop_event.is_set():
            _stop_event.wait(timeout=1)
    except KeyboardInterrupt:
        logger.info("收到键盘中断，正在停止...")
        _stop_event.set()

    # 等待所有线程退出（最多10秒）
    logger.info("等待所有服务停止...")
    for t in threads:
        t.join(timeout=10)
        if t.is_alive():
            logger.warning(f"服务 [{t.name}] 未能在10秒内停止")

    remove_pid()
    logger.info("Mnemos daemon 已停止")


def _daemonize_unix():
    """Unix 平台：使用 fork 后台化"""
    pid = os.fork()
    if pid > 0:
        print(f"Mnemos daemon 已启动 (PID: {pid})")
        print(f"日志: {log_file}")
        return

    os.setsid()
    os.umask(0)

    pid = os.fork()
    if pid > 0:
        sys.exit(0)

    run_daemon()


def _daemonize_windows():
    """Windows 平台：使用 CREATE_NEW_PROCESS_GROUP + DETACHED_PROCESS 启动独立子进程"""
    import subprocess

    # 使用 pythonw.exe 避免控制台窗口
    python_exe = Path(sys.executable)
    pythonw_exe = python_exe.parent / "pythonw.exe"
    if pythonw_exe.exists():
        python_exe = str(pythonw_exe)
    else:
        python_exe = sys.executable

    cmd = [python_exe, "-c",
           "import mnemos_daemon; mnemos_daemon.run_daemon()"]

    creation_flags = (
        subprocess.CREATE_NO_WINDOW
        | subprocess.CREATE_NEW_PROCESS_GROUP
        | subprocess.DETACHED_PROCESS
    )

    proc = subprocess.Popen(
        cmd,
        creationflags=creation_flags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    print(f"Mnemos daemon 已启动 (PID: {proc.pid})")
    print(f"日志: {log_file}")


def start_daemon():
    """启动守护进程（跨平台）"""
    if is_daemon_running():
        print("Mnemos daemon 已在运行")
        return

    if sys.platform == "win32":
        _daemonize_windows()
    else:
        _daemonize_unix()


def stop_daemon():
    """停止守护进程（跨平台）"""
    if not PID_FILE.exists():
        print("Mnemos daemon 未运行")
        return

    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        if sys.platform == "win32":
            import subprocess
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=10)
        else:
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
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        print(f"Mnemos daemon 运行中 (PID: {pid})")
        print(f"日志: {log_file}")

        # 显示服务状态
        try:
            from core.config import get_config
            config = get_config()
            print(f"\n配置:")
            print(f"  数据目录: {config.data_dir}")
            print(f"  Wiki目录: {config.wiki_dir}")
            print(f"  Memos: {'已配置' if config.memos_token else '未配置'}")

            from core.hephaestus_worker import HephaestusWorker
            worker = HephaestusWorker()
            stats = worker.get_stats()
            print(f"\n蒸馏队列:")
            print(f"  待处理: {stats['pending']}")
            print(f"  已委托: {stats['delegated']}")
        except Exception:
            logger.warning(f"Unexpected error in mnemos_daemon.py", exc_info=True)
            pass
    else:
        print("Mnemos daemon 未运行")
        print(f"日志文件: {log_file}")
        if log_file.exists():
            print(f"\n最近日志:")
            lines = log_file.read_text(encoding="utf-8").strip().split("\n")
            for line in lines[-5:]:
                print(f"  {line}")


def install_windows_task() -> bool:
    """将 daemon 注册为 Windows Task Scheduler 任务，开机自动启动"""
    if sys.platform != "win32":
        print("[ERR] 此命令仅支持 Windows")
        return False

    from core.config import get_config
    import subprocess
    task_name = "MnemosDaemon"
    python_exe = sys.executable
    script_path = Path(__file__).resolve()
    logs_dir = get_config().data_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_out = logs_dir / "daemon_scheduler.log"
    log_err = logs_dir / "daemon_scheduler.error.log"

    # 先卸载旧任务（如果存在）
    uninstall_windows_task()

    cmd = [
        "schtasks", "/Create", "/F",
        "/TN", task_name,
        "/TR", f'"{python_exe}" "{script_path}" run',
        "/SC", "ONLOGON",
        "/RL", "HIGHEST",
        "/NP",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode == 0 or "SUCCESS" in result.stdout:
            print(f"[OK] Windows Task Scheduler 任务已注册: {task_name}")
            print(f"  触发: 用户登录时自动启动")
            print(f"  命令: {python_exe} {script_path} run")
            print(f"  日志: {log_out}")
            return True
        else:
            print(f"[ERR] 注册失败: {result.stderr}")
            return False
    except FileNotFoundError:
        print("[ERR] schtasks 命令未找到，请确保 Windows 系统正常")
        return False
    except Exception as e:
        print(f"[ERR] 注册失败: {e}")
        return False


def uninstall_windows_task() -> bool:
    """从 Windows Task Scheduler 注销 daemon 任务"""
    if sys.platform != "win32":
        return False

    import subprocess
    task_name = "MnemosDaemon"
    try:
        result = subprocess.run(
            ["schtasks", "/Delete", "/F", "/TN", task_name],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        if result.returncode == 0 or "SUCCESS" in result.stdout:
            print(f"[OK] 已注销任务: {task_name}")
            return True
        return False
    except Exception:
        logger.warning(f"Unexpected error in mnemos_daemon.py", exc_info=True)
        return False


def main():
    parser = argparse.ArgumentParser(description="Mnemos Daemon v2.0.0")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("start", help="启动守护进程（全自动模式）")
    sub.add_parser("stop", help="停止守护进程")
    sub.add_parser("status", help="查看状态")
    sub.add_parser("run", help="前台运行（调试用）")
    sub.add_parser("install-windows", help="注册为 Windows 开机启动任务")
    sub.add_parser("uninstall-windows", help="注销 Windows 开机启动任务")
    args = parser.parse_args()

    if args.cmd == "start":
        start_daemon()
    elif args.cmd == "stop":
        stop_daemon()
    elif args.cmd == "status":
        status_daemon()
    elif args.cmd == "run":
        # 前台运行，方便调试
        run_daemon()
    elif args.cmd == "install-windows":
        install_windows_task()
    elif args.cmd == "uninstall-windows":
        uninstall_windows_task()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
