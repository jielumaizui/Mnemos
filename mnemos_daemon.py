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
import logging.handlers
import argparse
import threading
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# 配置日志（使用 RotatingFileHandler 避免单文件无限膨胀）
log_dir = Path.home() / ".mnemos"
log_dir.mkdir(parents=True, exist_ok=True)
log_file = log_dir / "daemon.log"

# 默认保留 5 个备份文件，单个文件最大 10MB
max_bytes = 10 * 1024 * 1024
backup_count = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [daemon] %(levelname)s: %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        ),
        logging.StreamHandler(sys.stderr),
    ],
)
logger = logging.getLogger(__name__)

PID_FILE = log_dir / "daemon.pid"

# 全局停止事件
_stop_event = threading.Event()


def _as_bool(value: Any, default: bool = False) -> bool:
    """配置布尔值归一化，兼容 JSON/env 中的字符串写法。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "enabled")
    return bool(value)


def _config_int(config: Any, key: str, default: int) -> int:
    try:
        return int(config.get(key, default))
    except (TypeError, ValueError):
        return default


def _config_float(config: Any, key: str, default: float) -> float:
    try:
        return float(config.get(key, default))
    except (TypeError, ValueError):
        return default


def _service_enabled(config: Any, key: str, default: bool = True) -> bool:
    return _as_bool(config.get(key, default), default)


class _L1ScanState:
    """记录 L1 扫描游标，避免 daemon 每轮重复解析历史文件。"""

    def __init__(self, path: Path):
        self.path = path
        self._data: Dict[str, Dict[str, Any]] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._data = loaded
        except Exception as e:
            logger.warning(f"[L1同步] 扫描游标读取失败，使用空游标: {e}")
            self._data = {}

    def _key(self, source_name: str, session_info: Any) -> str:
        return f"{source_name}:{session_info.session_id}:{session_info.source_path}"

    def is_unchanged(
        self,
        source_name: str,
        session_info: Any,
        file_state: Dict[str, Any],
    ) -> bool:
        record = self._data.get(self._key(source_name, session_info))
        if not record:
            return False
        return (
            record.get("mtime") == file_state.get("mtime")
            and record.get("size") == file_state.get("size")
        )

    def mark_scanned(
        self,
        source_name: str,
        session_info: Any,
        file_state: Dict[str, Any],
        status: str,
    ) -> None:
        self._data[self._key(source_name, session_info)] = {
            "path": str(session_info.source_path),
            "session_id": session_info.session_id,
            "mtime": file_state.get("mtime"),
            "size": file_state.get("size"),
            "status": status,
            "scanned_at": datetime.now(timezone.utc).isoformat(),
        }
        self._dirty = True

    def save(self) -> None:
        if not self._dirty:
            return
        # 防止内存/磁盘无限增长：只保留最近 1000 条扫描记录
        if len(self._data) > 1000:
            sorted_items = sorted(
                self._data.items(),
                key=lambda kv: kv[1].get("scanned_at", ""),
                reverse=True,
            )
            self._data = dict(sorted_items[:1000])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(self.path)
        self._dirty = False


def _l1_scan_limits(config: Any) -> Dict[str, Any]:
    return {
        "poll_interval": max(10, _config_int(config, "sync.l1_scan_poll_interval_seconds", 60)),
        "max_sources_per_cycle": max(0, _config_int(config, "sync.l1_scan_max_sources_per_cycle", 3)),
        "max_sessions_per_source": max(1, _config_int(config, "sync.l1_scan_max_sessions_per_source", 20)),
        "max_turns_per_session": max(1, _config_int(config, "sync.l1_scan_max_turns_per_session", 50)),
        "max_file_bytes": max(0, _config_int(config, "sync.l1_scan_max_file_bytes", 2 * 1024 * 1024)),
        "recent_hours": max(0.0, _config_float(config, "sync.l1_scan_recent_hours", 24.0)),
    }


def _l1_session_file_state(session_info: Any) -> Optional[Dict[str, Any]]:
    try:
        stat = session_info.source_path.stat()
        return {
            "mtime": session_info.mtime if session_info.mtime is not None else stat.st_mtime,
            "size": stat.st_size,
        }
    except OSError:
        return None


def _select_l1_sessions(
    source_name: str,
    sessions: List[Any],
    state: _L1ScanState,
    limits: Dict[str, Any],
) -> Tuple[List[Tuple[Any, Dict[str, Any]]], Dict[str, int]]:
    """按安全策略选择本轮允许解析的 session。"""
    now = time.time()
    recent_seconds = float(limits["recent_hours"]) * 3600
    max_file_bytes = int(limits["max_file_bytes"])
    max_sessions = int(limits["max_sessions_per_source"])
    stats = {
        "discovered": len(sessions),
        "selected": 0,
        "skipped_missing": 0,
        "skipped_large": 0,
        "skipped_stale": 0,
        "skipped_unchanged": 0,
        "skipped_over_limit": 0,
    }

    candidates: List[Tuple[float, Any, Dict[str, Any]]] = []
    for session_info in sessions:
        file_state = _l1_session_file_state(session_info)
        if file_state is None:
            stats["skipped_missing"] += 1
            continue
        if max_file_bytes and file_state["size"] > max_file_bytes:
            stats["skipped_large"] += 1
            continue
        if recent_seconds and (now - float(file_state["mtime"])) > recent_seconds:
            stats["skipped_stale"] += 1
            continue
        if state.is_unchanged(source_name, session_info, file_state):
            stats["skipped_unchanged"] += 1
            continue
        candidates.append((float(file_state["mtime"]), session_info, file_state))

    candidates.sort(key=lambda item: item[0], reverse=True)
    selected = candidates[:max_sessions]
    stats["selected"] = len(selected)
    stats["skipped_over_limit"] = max(0, len(candidates) - len(selected))
    return [(session_info, file_state) for _, session_info, file_state in selected], stats


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


def _count_daemon_processes() -> int:
    """通过 pgrep/tasklist 统计实际运行的 mnemos_daemon 进程数（不依赖 pid 文件）"""
    if sys.platform == "win32":
        try:
            import subprocess
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV"],
                capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )
            return sum(1 for line in result.stdout.splitlines() if "mnemos_daemon" in line)
        except Exception:
            return 0
    else:
        try:
            import subprocess
            import platform
            if platform.system() == "Darwin":
                result = subprocess.run(
                    ["pgrep", "-f", "mnemos_daemon.py"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode not in (0, 1):
                    return 0
                count = 0
                for pid in result.stdout.splitlines():
                    pid = pid.strip()
                    if not pid.isdigit():
                        continue
                    ps_result = subprocess.run(
                        ["ps", "-p", pid, "-o", "args="],
                        capture_output=True, text=True, timeout=5,
                    )
                    if ps_result.returncode == 0:
                        cmd = ps_result.stdout.strip()
                        if "mnemos_daemon.py" in cmd and "pgrep" not in cmd:
                            count += 1
                return count
            else:
                result = subprocess.run(
                    ["pgrep", "-af", "mnemos_daemon.py"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode not in (0, 1):
                    return 0
                return sum(
                    1 for line in result.stdout.splitlines()
                    if "mnemos_daemon.py" in line and "pgrep" not in line
                )
        except Exception:
            return 0


def is_daemon_running() -> bool:
    """检查 daemon 是否已在运行（pid 文件 + 进程扫描双重验证）"""
    # 1. 先检查 pid 文件
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text(encoding="utf-8").strip())
            if _is_process_running(pid):
                return True
            # pid 文件存在但进程已死，清理脏文件
            PID_FILE.unlink()
        except (ValueError, OSError, ProcessLookupError):
            pass

    # 2. 再扫描实际进程（防止 pid 文件被删除或覆盖后重复启动）
    return _count_daemon_processes() > 0


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
    capture_service = None
    try:
        from core.sync_framework.capture_service import CaptureService
        from core.sync_framework.registry import AgentRegistry
        from core.config import get_config

        config = get_config()
        limits = _l1_scan_limits(config)
        state = _L1ScanState(config.data_dir / "l1_scan_state.json")

        # 1. 先注册内置 Agent（确保 Worker 能正确解析来源标签）
        AgentRegistry.register_builtin_agents()

        # 2. 初始化 CaptureService（producer 模式）。
        # CaptureQueue consumer 已由独立的 service_capture_worker 负责，避免 L1 扫描和队列消费互相拖死。
        capture_service = CaptureService(start_worker=False)

        # 定时轮询模式（watchdog 由 TriggerDispatcher 管理）
        poll_interval = limits["poll_interval"]

        while not stop_event.is_set():
            try:
                # 重新发现活跃 Agent
                agents = AgentRegistry.auto_discover()
                max_sources = limits["max_sources_per_cycle"]
                if max_sources and len(agents) > max_sources:
                    logger.info(
                        f"[L1同步] 本轮发现 {len(agents)} 个 Agent，仅扫描前 {max_sources} 个"
                    )
                    agents = agents[:max_sources]
                logger.debug(f"[L1同步] 发现 {len(agents)} 个活跃 Agent 源")

                for source in agents:
                    try:
                        sessions = source.discover_sessions()
                        if not sessions:
                            continue

                        selected_sessions, scan_stats = _select_l1_sessions(
                            source.name, sessions, state, limits
                        )
                        if not selected_sessions:
                            if any(v for k, v in scan_stats.items() if k.startswith("skipped_")):
                                logger.debug(f"[L1同步] {source.name}: {scan_stats}")
                            continue

                        queued_count = 0
                        dup_count = 0
                        bp_count = 0
                        error_count = 0
                        scanned_count = 0
                        parsed_turns = 0

                        for session_info, file_state in selected_sessions:
                            try:
                                turns = source.parse_turns(session_info.source_path)
                                if not turns:
                                    state.mark_scanned(source.name, session_info, file_state, "empty")
                                    continue

                                # 确保按 turn_number 顺序入队，避免增量跳过逻辑错乱
                                turns = sorted(turns, key=lambda t: t.turn_number)
                                max_turns = limits["max_turns_per_session"]
                                if max_turns and len(turns) > max_turns:
                                    turns = turns[-max_turns:]
                                parsed_turns += len(turns)

                                context = source.on_session_start(
                                    session_info.session_id,
                                    {"working_dir": session_info.working_dir, "agent": source.name},
                                )

                                # 发布 session.start 事件（供 task_classifier / kia_guard 消费）
                                try:
                                    from core.mnemos_bus import publish_event
                                    publish_event("session.start", source.name, {
                                        "session_id": session_info.session_id,
                                        "user_message": turns[0].user_content if turns else "",
                                        "working_dir": str(session_info.working_dir) if session_info.working_dir else "",
                                    })
                                except Exception:
                                    pass

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

                                        # 发布 message.exchanged 事件（供 kia_guard 消费）
                                        try:
                                            from core.mnemos_bus import publish_event
                                            publish_event("message.exchanged", source.name, {
                                                "session_id": session_info.session_id,
                                                "turn_number": turn.turn_number,
                                                "role": "user" if turn.user_content else "assistant",
                                                "content_preview": (turn.user_content or turn.assistant_content or "")[:200],
                                            })
                                        except Exception:
                                            pass

                                    scanned_count += 1
                                    state.mark_scanned(source.name, session_info, file_state, "scanned")
                                finally:
                                    # 触发异步 end_session，让 Worker 优先 flush 该 session
                                    # 放在 finally 中确保 capture_turn 异常时也会执行
                                    try:
                                        capture_service.end_session(source.name, session_info.session_id)
                                    except Exception as e:
                                        logger.warning(f"[L1同步] end_session 失败: {e}")
                                    # KIA Hook: session_end（无论入队是否成功都执行，避免泄漏）
                                    all_messages = []
                                    for turn in turns:
                                        if turn.user_content:
                                            all_messages.append({"role": "user", "content": turn.user_content})
                                        if turn.assistant_content:
                                            all_messages.append({"role": "assistant", "content": turn.assistant_content})
                                    source.on_session_end(session_info.session_id, all_messages)

                            except Exception as e:
                                error_count += 1
                                state.mark_scanned(source.name, session_info, file_state, "error")
                                logger.error(
                                    f"[L1同步] {source.name}/{session_info.session_id} 解析失败: {e}"
                                )

                        try:
                            state.save()
                        except Exception as e:
                            logger.warning(f"[L1同步] 扫描游标保存失败: {e}")

                        if queued_count > 0 or dup_count > 0 or bp_count > 0 or error_count > 0:
                            logger.info(
                                f"[L1同步] {source.name}: "
                                f"discovered={scan_stats['discovered']}, "
                                f"selected={scan_stats['selected']}, "
                                f"scanned={scanned_count}, "
                                f"turns={parsed_turns}, "
                                f"queued={queued_count}, "
                                f"duplicate={dup_count}, "
                                f"backpressure={bp_count}, "
                                f"error={error_count}, "
                                f"skipped={{{', '.join(f'{k[8:]}={v}' for k, v in scan_stats.items() if k.startswith('skipped_') and v)}}}"
                            )
                            try:
                                from core.mnemos_bus import publish_event
                                publish_event("polled", source.name, {
                                    "sessions_discovered": scan_stats["discovered"],
                                    "sessions_selected": scan_stats["selected"],
                                    "sessions_scanned": scanned_count,
                                    "turns_seen": parsed_turns,
                                    "queued": queued_count,
                                    "duplicate": dup_count,
                                    "backpressure": bp_count,
                                    "errors": error_count,
                                    "limits": limits,
                                })
                            except Exception:
                                pass
                    except Exception as e:
                        logger.error(f"[L1同步] {source.name} 同步失败: {e}")
            except Exception as e:
                logger.error(f"[L1同步] 轮询失败: {e}")

            stop_event.wait(timeout=poll_interval)

        logger.info("[L1同步] 已停止")
    except Exception as e:
        logger.error(f"[L1同步] 服务异常: {e}")
    finally:
        if capture_service is not None:
            try:
                capture_service.close()
            except Exception as e:
                logger.warning(f"[L1同步] 关闭 capture_service 失败: {e}")


def service_capture_worker(stop_event: threading.Event):
    """
    服务0: CaptureQueue 消费者 — 独立消费 MCP / L1 producer 入队的 capture_events。

    这个服务不扫描任何 Agent 历史文件，只负责把 pending 队列写入 Memos。
    这样 daemon 可以在 L1 扫描关闭时仍然消费 MCP/Hook 上报，避免“扫描爆炸”和“队列堵塞”绑定。
    """
    logger.info("[捕获消费] 服务启动")
    worker = None
    queue = None
    try:
        from core.sync_framework.capture_queue import CaptureQueue
        from core.sync_framework.capture_worker import CaptureWorkerPool

        queue = CaptureQueue()
        worker = CaptureWorkerPool(queue=queue)
        worker.start()

        while not stop_event.is_set():
            stop_event.wait(timeout=1)
    except Exception as e:
        logger.error(f"[捕获消费] 服务异常: {e}", exc_info=True)
    finally:
        if worker is not None:
            try:
                worker.close()
            except Exception as e:
                logger.warning(f"[捕获消费] 停止 worker 失败: {e}")
        if queue is not None:
            try:
                queue.close()
            except Exception as e:
                logger.warning(f"[捕获消费] 关闭队列连接失败: {e}")
        logger.info("[捕获消费] 服务已停止")


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

    # 初始化 JobScheduler（KG 维护任务：每周自动清洗）
    job_scheduler = None
    try:
        from core.job_scheduler import JobScheduler
        job_scheduler = JobScheduler()
        job_scheduler.register_job(
            name="kg_clean_entities",
            script="clean_kg.py",
            cron="0 3 * * 0",  # 每周日 03:00
            description="清洗知识图谱噪声实体（哈希前缀/MOC/片段等）",
            timeout_seconds=300,
            max_retries=1,
        )
        job_scheduler.register_job(
            name="kg_clean_relations",
            script="clean_relations.py",
            cron="30 3 * * 0",  # 每周日 03:30
            description="清洗知识图谱低质量关系（悬空引用/通用关键词相似/高密度溢出）",
            timeout_seconds=300,
            max_retries=1,
        )
        logger.info("[蒸馏合并] JobScheduler 已注册 KG 维护任务（每周日 03:00/03:30）")
    except Exception as e:
        logger.warning(f"[蒸馏合并] JobScheduler 初始化失败: {e}")

    # 在循环外初始化 worker，避免每次循环新建连接/资源
    from core.hephaestus_worker import HephaestusWorker
    worker = HephaestusWorker()

    # Memos 客户端（复用，避免每次循环新建）
    memos_client = None
    try:
        from integrations.styx import MemosClient
        memos_client = MemosClient()
    except Exception as e:
        logger.warning(f"[蒸馏合并] MemosClient 初始化失败，无法自动入队: {e}")

    while not stop_event.is_set():
        try:
            # Step 0: 扫描 Memos，将新完成的 L1 sessions 自动入队到 amphora
            if memos_client:
                try:
                    from core.hephaestus.wiki_builder import (
                        fetch_l1_sessions, reconstruct_session,
                        _is_session_completed, _is_processed,
                    )
                    from core.kia import amphora

                    sessions = fetch_l1_sessions(memos_client)
                    enqueued = 0
                    doc_processed = 0
                    for sid, memos in sessions.items():
                        if _is_session_completed(sid, memos) and not _is_processed(sid):
                            try:
                                messages, meta = reconstruct_session(memos)
                                # Doc sessions (external documents) — 深度蒸馏，不入队
                                if sid.startswith('doc-'):
                                    from core.hephaestus.document_pipeline import process_doc_session
                                    inbox = worker.inbox_dir
                                    pages = process_doc_session(sid, messages, meta, inbox)
                                    if pages > 0:
                                        from core.hephaestus.wiki_builder import _mark_processed
                                        _mark_processed(sid, meta.get('source', 'unknown'), len(messages), 0, 'pipeline')
                                        doc_processed += 1
                                    continue
                                # Regular chat sessions — 质量门槛后入队
                                if len(messages) >= 5:
                                    amphora.enqueue(sid, messages, meta)
                                    enqueued += 1
                            except Exception:
                                continue
                    if enqueued > 0:
                        logger.info(f"[蒸馏合并] Memos 扫描: {enqueued} 个新 session 已入队")
                    if doc_processed > 0:
                        logger.info(f"[蒸馏合并] Memos 扫描: {doc_processed} 个外部文档已直接入库")
                except Exception as e:
                    logger.debug(f"[蒸馏合并] Memos 扫描失败: {e}")

            # 高频：处理 distill_queue
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
                # 构造 Teiresias 推送上下文（从最近 memos）
                push_context = _build_push_context(memos_client)
                if scheduler:
                    results = scheduler.tick()
                    if results:
                        ok_count = sum(1 for r in results.values() if r.get("status") == "ok")
                        err_count = sum(1 for r in results.values() if r.get("status") == "error")
                        skip_count = sum(1 for r in results.values() if r.get("status") == "skipped")
                        logger.info(f"[蒸馏合并] KIA tick: {ok_count}成功, {err_count}失败, {skip_count}跳过")
                    # KIA tick 后额外运行 Teiresias 推送引擎
                    if push_context:
                        try:
                            from core.orchestrator import Orchestrator
                            orch = Orchestrator(wiki_dir=get_config().wiki_dir)
                            push_result = orch.run_push(context=push_context)
                            if push_result.get("triggered"):
                                logger.info(f"[推送] Teiresias 触发推送: {push_result.get('reason', '')}")
                        except Exception:
                            pass
                else:
                    # 回退到 Orchestrator
                    from core.orchestrator import Orchestrator
                    orch = Orchestrator(wiki_dir=get_config().wiki_dir)
                    report = orch.run_full(push_context=push_context)
                    logger.info(f"[蒸馏合并] Orchestrator 完成: {len(report.get('errors', []))} 错误")
                    # 报告推送结果
                    push_res = report.get("push", {})
                    if push_res.get("triggered"):
                        logger.info(f"[推送] Teiresias 触发推送: {push_res.get('reason', '')}")

                # 同 tick 内运行 JobScheduler（KG 维护任务等）
                if job_scheduler:
                    try:
                        job_results = job_scheduler.run_due_jobs()
                        if job_results:
                            for jr in job_results:
                                if jr.status == "success":
                                    logger.info(f"[JobScheduler] {jr.job_name} 成功 ({jr.duration_ms}ms)")
                                elif jr.status == "failed":
                                    logger.warning(f"[JobScheduler] {jr.job_name} 失败: {jr.error_message}")
                                elif jr.status == "skipped":
                                    logger.info(f"[JobScheduler] {jr.job_name} 跳过")
                    except Exception as e:
                        logger.warning(f"[JobScheduler] 运行失败: {e}")

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

    # 在循环外初始化评分器，避免每次循环新建
    scorer = None
    daemon = None
    try:
        from core.scoring.scorers.ops_scorer import OpsScorer
        scorer = OpsScorer()
    except ImportError:
        from core.heartbeat import HeartbeatDaemon
        daemon = HeartbeatDaemon()

    # 初始化蒸馏评分器状态追踪
    distill_scorer = None
    try:
        from core.scoring.scorers.distill_scorer import DistillScorer
        distill_scorer = DistillScorer()
    except Exception:
        pass

    heartbeat_count = 0

    while not stop_event.is_set():
        try:
            # 优先使用 OpsScorer
            if scorer is not None and hasattr(scorer, "score_system"):
                result = scorer.score_system()
                health = result.get("health_score", 0)
                if health > 0:
                    logger.debug(f"[心跳] 系统健康度: {health:.1f}")
            elif daemon is not None:
                daemon.run_once()
            else:
                logger.debug("[心跳] OpsScorer 未提供 score_system，跳过系统评分")

            # 每 5 次心跳（5 分钟）报告蒸馏评分器状态
            heartbeat_count += 1
            if heartbeat_count % 5 == 0 and distill_scorer is not None:
                try:
                    status = distill_scorer._scorer.get_status()
                    mode = status.get("mode", "unknown")
                    version = status.get("model_version", 0)
                    buffer = status.get("retrain_buffer_size", 0)
                    threshold = status.get("retrain_threshold", 40)
                    dims = status.get("dimensions", [])
                    versions = status.get("versions_on_disk", [])

                    if version == 0 and buffer == 0:
                        logger.info(
                            f"[心跳] 蒸馏评分器: 模式={mode}, 尚未积累反馈样本"
                        )
                    elif version == 0 and buffer > 0:
                        progress = min(100, int(buffer / threshold * 100))
                        logger.info(
                            f"[心跳] 蒸馏评分器: 模式={mode}, 重训练缓冲={buffer}/{threshold} "
                            f"({progress}%), 维度={dims}"
                        )
                    else:
                        logger.info(
                            f"[心跳] 蒸馏评分器: 模式={mode}, 版本=v{version}, "
                            f"缓冲={buffer}/{threshold}, 维度={dims}, "
                            f"磁盘版本={len(versions)}"
                        )
                except Exception:
                    pass

            # 每 24 小时（1440 次心跳）运行一次争议扫描
            if heartbeat_count % 1440 == 0:
                try:
                    dr = _run_dispute_scan()
                    logger.info(
                        f"[争议] 每日扫描: 新建={dr.get('new_disputes', 0)}, "
                        f"未解决={dr.get('unresolved', 0)}, "
                        f"需升级={dr.get('escalated', 0)}"
                    )
                except Exception:
                    pass

            # 每 24 小时运行知识新鲜度检查
            if heartbeat_count % 1440 == 0:
                try:
                    from core.app.freshness_alert import FreshnessAlertChecker
                    checker = FreshnessAlertChecker()
                    alerts = checker.scan_all_freshness()
                    if alerts:
                        logger.info(
                            f"[新鲜度] 发现 {len(alerts)} 条过期知识: "
                            f"{', '.join(a.entity_name for a in alerts[:3])}"
                        )
                    else:
                        logger.debug("[新鲜度] 知识库状态良好")
                except Exception:
                    pass

            # 每 12 小时（720 次心跳）注入 synthetic ground_truth
            if heartbeat_count % 720 == 0:
                try:
                    injected = _inject_synthetic_ground_truth()
                    if injected > 0:
                        logger.info(f"[评分器] 注入 {injected} 条 synthetic ground_truth")
                except Exception:
                    pass

            # 每 12 小时（720 次心跳）运行评分器训练调度
            if heartbeat_count % 720 == 0:
                try:
                    from core.scoring.training_scheduler import ScorerTrainingScheduler
                    sched = ScorerTrainingScheduler()
                    jobs = sched.on_hourly_tick()
                    if jobs:
                        logger.info(
                            f"[评分调度] 触发 {len(jobs)} 个训练任务: "
                            f"{', '.join(j.dimension for j in jobs)}"
                        )
                    cleanup = sched.on_daily_cleanup()
                    if cleanup.get("cleaned"):
                        logger.info(
                            f"[评分调度] 清理 {cleanup.get('cleaned', 0)} 个过期任务"
                        )
                except Exception:
                    pass

            # 每 30 分钟（30 次心跳）运行搜索索引健康检查
            if heartbeat_count % 30 == 0:
                try:
                    from core.app.context_search import ContextAwareSearch
                    searcher = ContextAwareSearch()
                    # 用最近更新页面做一次空查询，验证索引连通性
                    results = searcher.search("recent", limit=3)
                    if results:
                        logger.debug(
                            f"[搜索] 索引健康: {len(results)} 条候选"
                        )
                except Exception:
                    pass

            # 每 30 分钟运行问答检索缓存刷新
            if heartbeat_count % 30 == 0:
                try:
                    from core.app.question_answer_search import QuestionAnswerSearch
                    qa = QuestionAnswerSearch()
                    # 空查询验证模块可用
                    _ = qa.search("", limit=1)
                    logger.debug("[问答检索] 模块可用")
                except Exception:
                    pass
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

    # 在循环外初始化处理器，避免每次循环新建
    try:
        from core.kia.knowledge_inbox import KnowledgeInboxProcessor
        inbox = KnowledgeInboxProcessor()
    except ImportError:
        inbox = None

    while not stop_event.is_set():
        try:
            if inbox is None:
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
                if stop_event.is_set():
                    break
                try:
                    signals = agent.collect_signals(days=7)
                    # 限制单次采集数量，防止内存/时间爆炸
                    max_signals_per_agent = 10000
                    if len(signals) > max_signals_per_agent:
                        logger.warning(f"[画像] {agent.name} 信号数 {len(signals)} 超过限制，截断到 {max_signals_per_agent}")
                        signals = signals[:max_signals_per_agent]
                    for sig in signals:
                        if stop_event.is_set():
                            break
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


# 画像分析全局锁，防止多个服务线程同时触发分析
_persona_analysis_lock = threading.Lock()

def _run_persona_extensions(profile):
    """激活所有 Persona 边缘/死代码模块。在画像分析完成后调用。"""
    results = {}

    # ── 1. Daimon — 补充信号采集 ──────────────────────────────────
    try:
        from core.persona.daimon import SignalCollector
        collector = SignalCollector()
        d_total = 0
        d_total += collector.collect_from_distill_queue()
        d_total += collector.collect_from_wiki_state()
        if d_total:
            logger.info(f"[画像扩展] Daimon 补充采集 {d_total} 条信号")
        results["daimon"] = {"signals_collected": d_total}
    except Exception as e:
        logger.debug(f"[画像扩展] Daimon 失败: {e}")

    # ── 2. Echo — 微信信号采集（条件性）────────────────────────────
    try:
        from core.persona.echo import WeChatCollector
        wc = WeChatCollector()
        dbs = wc.discover_databases()
        if dbs:
            count = wc.collect_and_store(days=7)
            if count:
                logger.info(f"[画像扩展] Echo 采集 {count} 条微信信号")
            results["echo"] = {"wechat_signals": count}
    except Exception as e:
        logger.debug(f"[画像扩展] Echo 失败: {e}")

    # ── 3. ContextualPersona — 情境隔离画像 ────────────────────────
    try:
        from core.persona.contextual_persona import ContextualPersona
        cp = ContextualPersona()
        context = cp.detect_context()
        # 将当前画像按情境存储
        profile_dict = profile.to_dict() if hasattr(profile, "to_dict") else {}
        cp.add_signal(profile_dict, context=context)
        contexts = list(cp.get_profile().keys())
        logger.info(f"[画像扩展] ContextualPersona 情境: {contexts}")
        results["contextual_persona"] = {"contexts": contexts}
    except Exception as e:
        logger.debug(f"[画像扩展] ContextualPersona 失败: {e}")

    # ── 4. EvolutionTimeline — 14 维演化时间线 ─────────────────────
    try:
        from core.persona.evolution_timeline import PersonaEvolutionTimeline
        timeline = PersonaEvolutionTimeline()
        profile_dict = profile.to_dict() if hasattr(profile, "to_dict") else {}
        timeline.add_snapshot(profile_dict)
        report_path = timeline.generate()
        logger.info(f"[画像扩展] 演化时间线更新: {report_path}")
        results["evolution_timeline"] = {"report_path": str(report_path)}
    except Exception as e:
        logger.debug(f"[画像扩展] EvolutionTimeline 失败: {e}")

    # ── 5. CrossValidator — 双画像交叉验证 ─────────────────────────
    try:
        from core.persona.cross_validator import ProfileCrossValidator
        validator = ProfileCrossValidator()
        profile_dict = profile.to_dict() if hasattr(profile, "to_dict") else {}
        behavior = {}
        for layer in ("energy", "cognitive", "value"):
            layer_data = profile_dict.get(layer, {})
            if hasattr(layer_data, "items"):
                behavior.update(layer_data)
        knowledge = {}  # 知识画像暂时为空，待后续接入
        contradictions = validator.validate(behavior, knowledge)
        if contradictions:
            logger.info(f"[画像扩展] CrossValidator 发现 {len(contradictions)} 个矛盾")
        results["cross_validator"] = {"contradictions": len(contradictions)}
    except Exception as e:
        logger.debug(f"[画像扩展] CrossValidator 失败: {e}")

    # ── 6. Rhapsode — 画像报告生成 ─────────────────────────────────
    try:
        from core.persona.rhapsode import SelfReportGenerator
        gen = SelfReportGenerator()
        report = gen.generate(days=30)
        if report:
            path = gen.save_to_wiki(report)
            logger.info(f"[画像扩展] Rhapsode 报告: {path}")
            results["rhapsode"] = {"report_path": str(path)}
    except Exception as e:
        logger.debug(f"[画像扩展] Rhapsode 失败: {e}")

    # ── 7. Harmonia + CalibrationCLI — 自动校准 ────────────────────
    try:
        from core.persona.calibration_cli import run_calibration
        # 提取非交互式校准（使用当前画像作为 ground_truth）
        cal_result = run_calibration()
        logger.info(f"[画像扩展] 自动校准完成")
        results["harmonia"] = {"calibrated": True}
    except Exception as e:
        logger.debug(f"[画像扩展] Harmonia 失败: {e}")

    # ── 8. DialogueStrategy — 对话策略 ─────────────────────────────
    try:
        from core.persona.dialogue_strategy import PersonaDrivenDialogueStrategy
        strategy = PersonaDrivenDialogueStrategy()
        profile_dict = profile.to_dict() if hasattr(profile, "to_dict") else {}
        adapted = strategy.adapt_prompt("", preference_profile=profile_dict)
        # 保存策略到画像元数据
        logger.info(f"[画像扩展] DialogueStrategy 策略已生成")
        results["dialogue_strategy"] = {"adapted": bool(adapted)}
    except Exception as e:
        logger.debug(f"[画像扩展] DialogueStrategy 失败: {e}")

    # ── 9. BehaviorDrivenSkillEngine — 行为驱动技能 ────────────────
    try:
        from core.persona.behavior_driven_skill_engine import BehaviorDrivenSkillEngine
        from core.persona.psyche import get_signal_store
        engine = BehaviorDrivenSkillEngine()
        store = get_signal_store()
        # 获取最近行为数据（session 信号）
        actions = []
        try:
            stats = store.get_signal_stats(days=30)
            for sig in store.get_recent_signals(limit=100):
                if hasattr(sig, "task_type"):
                    actions.append({
                        "action": sig.task_type,
                        "timestamp": sig.timestamp if hasattr(sig, "timestamp") else "",
                        "context": sig.working_dir if hasattr(sig, "working_dir") else "",
                    })
        except Exception:
            pass
        if actions:
            patterns = engine.analyze_behavior(actions)
            suggestions = engine.suggest_skill_updates([], patterns)
            if patterns:
                logger.info(f"[画像扩展] BehaviorDrivenSkillEngine: {len(patterns)} 个模式, {len(suggestions)} 个建议")
            results["behavior_driven_skill"] = {
                "patterns": len(patterns),
                "suggestions": len(suggestions),
            }
    except Exception as e:
        logger.debug(f"[画像扩展] BehaviorDrivenSkillEngine 失败: {e}")

    # ── 10. AvoidanceDetector — 回避模式检测 ───────────────────────
    try:
        from core.app.avoidance_detector import AvoidanceDetector
        from core.persona.psyche import get_signal_store
        detector = AvoidanceDetector()
        store = get_signal_store()
        history = []
        try:
            for sig in store.get_recent_signals(limit=200):
                content = getattr(sig, "working_dir", "") or ""
                history.append({
                    "query": content,
                    "timestamp": getattr(sig, "timestamp", ""),
                    "clicked": True,
                })
        except Exception:
            pass
        if len(history) >= 3:
            patterns = detector.analyze(history)
            if patterns:
                logger.info(
                    f"[画像扩展] AvoidanceDetector: {len(patterns)} 个回避模式, "
                    f"主题: {', '.join(p.topic for p in patterns[:3])}"
                )
            results["avoidance_detector"] = {"patterns": len(patterns)}
    except Exception as e:
        logger.debug(f"[画像扩展] AvoidanceDetector 失败: {e}")

    # ── 11. CrossAgentDivergenceDetector — 跨 Agent 分歧检测 ───────
    try:
        from core.app.cross_agent_divergence_detector import CrossAgentDivergenceDetector
        from core.persona.psyche import get_signal_store
        detector = CrossAgentDivergenceDetector()
        store = get_signal_store()
        # 按 agent 分组收集最近信号
        agent_outputs: Dict[str, List[str]] = {}
        try:
            for sig in store.get_recent_signals(limit=300):
                agent = getattr(sig, "agent", "unknown") or "unknown"
                content = getattr(sig, "working_dir", "") or ""
                if content:
                    agent_outputs.setdefault(agent, []).append(content)
        except Exception:
            pass
        # 当存在 2+ 个 agent 且每个都有足够数据时检测分歧
        if len(agent_outputs) >= 2:
            # 构建对比列表：取每个 agent 的输出拼接
            outputs_for_comparison = []
            for agent, texts in agent_outputs.items():
                if len(texts) >= 5:
                    outputs_for_comparison.append({
                        "agent_id": agent,
                        "output": " ".join(texts[:20]),
                        "topic": "behavior_patterns",
                    })
            if len(outputs_for_comparison) >= 2:
                reports = detector.detect_divergence(outputs_for_comparison)
                if reports:
                    logger.info(
                        f"[画像扩展] CrossAgentDivergenceDetector: {len(reports)} 个分歧报告, "
                        f"主题: {', '.join(r.topic for r in reports[:3])}"
                    )
                results["cross_agent_divergence"] = {"reports": len(reports)}
    except Exception as e:
        logger.debug(f"[画像扩展] CrossAgentDivergenceDetector 失败: {e}")

    # 汇总日志
    active = [k for k, v in results.items() if v]
    if active:
        logger.info(f"[画像扩展] 激活模块: {', '.join(active)}")
    return results


def _inject_synthetic_ground_truth() -> int:
    """从 persona 信号中推断并注入 synthetic ground_truth，加速评分器冷启动。"""
    import sqlite3
    from pathlib import Path

    db_dir = Path.home() / ".mnemos"
    signals_db = db_dir / "user_signals.db"
    gt_db = db_dir / "mnemos.db"

    if not signals_db.exists():
        return 0

    try:
        with sqlite3.connect(str(signals_db)) as conn:
            # 读取最近 7 天未处理的 session_signals
            rows = conn.execute("""
                SELECT session_id, task_type, user_msg_count, avg_user_msg_length,
                       correction_count, follow_up_depth, working_dir
                FROM session_signals
                WHERE timestamp > datetime('now', '-7 days')
                ORDER BY timestamp DESC
                LIMIT 50
            """).fetchall()
    except Exception:
        return 0

    if not rows:
        return 0

    injected = 0
    try:
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2
        for row in rows:
            session_id, task_type, msg_count, avg_len, corrections, follow_up, wd = row
            if msg_count is None or avg_len is None:
                continue
            base_confidence = min(1.0, 0.5 + msg_count * 0.05 + avg_len * 0.001)

            # 为每个 session 生成 3 个维度的 ground_truth
            signals = [
                (task_type or "session_quality", 1 if (msg_count >= 3 and corrections == 0) else 0),
                ("engagement", 1 if msg_count >= 5 else 0),
                ("correction_pattern", 0 if corrections == 0 else 1),
            ]
            for signal_type, label in signals:
                try:
                    AdaptiveScorerV2.insert_ground_truth(
                        session_id=session_id,
                        signal_type=signal_type,
                        label=label,
                        confidence=round(base_confidence, 2),
                        latency_hours=0,
                        db_path=gt_db,
                    )
                    injected += 1
                except Exception:
                    continue
    except Exception:
        return 0

    return injected


def _trigger_persona_analysis():
    """触发画像分析（被 signal_collector 调用）"""
    if not _persona_analysis_lock.acquire(blocking=False):
        logger.debug("[画像] 分析已在进行中，跳过重复触发")
        return
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
            # 激活所有 Persona 边缘模块
            _run_persona_extensions(profile)
    except Exception as e:
        logger.error(f"[画像] 画像分析失败: {e}")
    finally:
        _persona_analysis_lock.release()


def _build_push_context(memos_client) -> str:
    """从最近 memos 笔记构造 Teiresias 推送上下文。"""
    if not memos_client:
        return ""
    try:
        recent = memos_client.list_all_memos(max_records=3)
        if not recent:
            return ""
        # 取最近一条的非空内容作为 push context
        for m in recent:
            content = m.get("content", "").strip()
            if content:
                # 截断过长内容，保留前 300 字符
                return content[:300] if len(content) <= 300 else content[:300] + "..."
    except Exception:
        pass
    return ""


def _run_dispute_scan() -> Dict:
    """运行争议扫描：检测知识图谱 suspect 关系 + wiki 争议标记，生成仲裁页面。"""
    result = {"new_disputes": 0, "unresolved": 0, "escalated": 0}
    try:
        from core.app.dispute_resolver import DisputeResolver, DisputeAssertion
        resolver = DisputeResolver()

        # 1. 报告未解决争议（含升级）
        unresolved = resolver.get_unresolved_disputes()
        result["unresolved"] = len(unresolved)
        escalated = [d for d in unresolved if d.get("needs_escalation")]
        result["escalated"] = len(escalated)
        if escalated:
            titles = ", ".join(d["title"] for d in escalated[:3])
            logger.warning(f"[争议] {len(escalated)} 个争议超过 7 天未解决: {titles}")
        elif unresolved:
            logger.info(f"[争议] 当前 {len(unresolved)} 个未解决争议")

        # 2. 扫描知识图谱 suspect 关系，生成新争议
        try:
            from core.kia.knowledge_graph import DB_PATH
            import sqlite3
            if DB_PATH.exists():
                with sqlite3.connect(str(DB_PATH)) as conn:
                    # 取最近 7 天内标记为 suspect 的关系
                    week_ago = (datetime.now(timezone.utc) - __import__(
                        "datetime"
                    ).timedelta(days=7)).isoformat()
                    rows = conn.execute("""
                        SELECT source, target, relation_type, confidence, created_at
                        FROM relations
                        WHERE source_method = 'suspect'
                          AND created_at >= ?
                        ORDER BY confidence ASC
                        LIMIT 10
                    """, (week_ago,)).fetchall()

                    seen_topics = set()
                    for source, target, rel_type, confidence, created_at in rows:
                        topic = f"{source} → {target} ({rel_type})"
                        if topic in seen_topics:
                            continue
                        seen_topics.add(topic)

                        new_assertion = DisputeAssertion(
                            page_path=source,
                            title=source,
                            content=f"实体 '{source}' 与 '{target}' 存在低置信度关系（{rel_type}），置信度 {confidence:.2f}",
                            reference_count=1,
                        )
                        conflicts = [DisputeAssertion(
                            page_path=target,
                            title=target,
                            content=f"目标实体 '{target}' 在知识图谱中被质疑",
                            reference_count=1,
                        )]
                        resolver.create_dispute_page(
                            new_assertion=new_assertion,
                            conflicts=conflicts,
                            conflict_strength=1.0 - confidence,
                            is_core_knowledge=(confidence < 0.1),
                        )
                        result["new_disputes"] += 1
        except Exception as e:
            logger.debug(f"[争议] suspect 扫描失败: {e}")

        # 3. 扫描 wiki 中的争议标记
        try:
            from core.config import get_config
            wiki_base = get_config().wiki_dir
            dispute_markers = ["⚠️ 争议", "conflict:", "contradiction", "TODO: verify"]
            for md_file in wiki_base.rglob("*.md"):
                if "08-Disputes" in str(md_file):
                    continue
                try:
                    content = md_file.read_text(encoding="utf-8")
                    lower = content.lower()
                    if any(m.lower() in lower for m in dispute_markers):
                        rel_path = str(md_file.relative_to(wiki_base))
                        topic = md_file.stem
                        # 避免重复创建（检查 08-Disputes 中是否已有）
                        dispute_dir = wiki_base / "08-Disputes"
                        existing = list(dispute_dir.glob(f"*-{topic[:30].replace('/', '-').replace(' ', '_')}.md"))
                        if existing:
                            continue
                        new_assertion = DisputeAssertion(
                            page_path=rel_path,
                            title=topic,
                            content=f"页面中包含争议标记: {[m for m in dispute_markers if m.lower() in lower][0]}",
                            reference_count=1,
                        )
                        resolver.create_dispute_page(
                            new_assertion=new_assertion,
                            conflicts=[],
                            conflict_strength=0.5,
                            is_core_knowledge=False,
                        )
                        result["new_disputes"] += 1
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f"[争议] wiki 标记扫描失败: {e}")

        if result["new_disputes"]:
            logger.info(f"[争议] 新建 {result['new_disputes']} 个争议仲裁页面")
    except Exception as e:
        logger.debug(f"[争议] 扫描失败: {e}")
    return result


def _generate_blindspot_challenges(profile):
    """基于画像生成盲区挑战问题，写入待处理队列。

    整合硬编码规则（确定性画像挑战）与 BlindspotDiscovery 动态检测。
    """
    try:
        challenges = []
        # ── 1. 硬编码画像挑战（确定性规则）─────────────────────────────
        if profile.energy.confidence >= 0.6:
            if profile.energy.focus_depth > 0.7:
                challenges.append({
                    "dimension": "energy.focus_depth",
                    "type": "反向验证",
                    "source": "profile_rule",
                    "question": "系统推断你倾向于深度专注。但最近你是否有刻意浅层浏览、快速扫过大量信息的时刻？",
                    "suggestion": "如果经常有这样的时刻，你的专注模式可能比画像显示的更灵活。",
                })
            if profile.energy.switching_flexibility < 0.3:
                challenges.append({
                    "dimension": "energy.switching_flexibility",
                    "type": "盲区检测",
                    "source": "profile_rule",
                    "question": "画像显示你偏单线程。你是否注意到有些任务并行处理反而更高效？",
                    "suggestion": "多线程不一定意味着分心，有些组合任务可以并行而不损失质量。",
                })
        if profile.cognitive.confidence >= 0.6:
            if profile.cognitive.skepticism > 0.7:
                challenges.append({
                    "dimension": "cognitive.skepticism",
                    "type": "反向验证",
                    "source": "profile_rule",
                    "question": "画像显示你经常质疑前提。但你是否有时会过度质疑，导致决策拖延？",
                    "suggestion": "质疑是优点，但时机和对象很重要。",
                })
        if profile.value.confidence >= 0.6:
            if profile.value.perfection_vs_completion > 0.7:
                challenges.append({
                    "dimension": "value.perfection_vs_completion",
                    "type": "盲区检测",
                    "source": "profile_rule",
                    "question": "画像显示你追求完美。回想一下，是否有'先完成再优化'反而效果更好的经历？",
                    "suggestion": "完成度有时候比完美度更有价值，尤其是在信息不完备的早期阶段。",
                })

        # ── 2. 激活 BlindspotDiscovery 动态模块 ──────────────────────
        try:
            from core.app.blindspot_discovery import BlindspotDiscovery
            bd = BlindspotDiscovery()

            # 2a. 触发周期性信用恢复
            credit_resp = bd.handle_event("periodic_persona_analysis")
            if credit_resp.get("status") == "ok":
                logger.debug(
                    f"[画像] 盲区信用恢复: {credit_resp.get('challenge_credit', 'n/a')}"
                )

            # 2b. 获取本周动态盲点并转成挑战格式
            weekly = bd.get_weekly_summary()
            for bs in weekly:
                if bs.get("status") not in ("resolved", "mitigated"):
                    challenges.append({
                        "dimension": bs.get("topic", "unknown"),
                        "type": "动态盲点",
                        "source": "blindspot_discovery",
                        "confidence": bs.get("confidence", 0.5),
                        "question": bs.get("description", ""),
                        "suggestion": "可在 mnemos-cli 中通过 `blindspot-feedback <topic> resolved|mitigated|ignored` 反馈。",
                    })
        except Exception as e:
            logger.debug(f"[画像] BlindspotDiscovery 调用失败: {e}")

        # ── 3. 持久化 ────────────────────────────────────────────────
        if challenges:
            calib_dir = Path.home() / ".mnemos" / "calibrations"
            calib_dir.mkdir(parents=True, exist_ok=True)
            challenge_file = calib_dir / "pending_challenges.json"
            profile_rules = sum(1 for c in challenges if c.get("source") == "profile_rule")
            dynamic = sum(1 for c in challenges if c.get("source") == "blindspot_discovery")
            challenge_file.write_text(
                json.dumps({
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "profile_version": getattr(profile, "version", "unknown"),
                    "summary": {
                        "total": len(challenges),
                        "profile_rules": profile_rules,
                        "dynamic_blindspots": dynamic,
                    },
                    "challenges": challenges,
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info(
                f"[画像] 已生成 {len(challenges)} 个盲区挑战问题"
                f"（规则{profile_rules} / 动态{dynamic}）"
            )
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

    from core.mnemos_bus import EventProcessor, EventBus, _get_bus

    bus = _get_bus()
    processor = EventProcessor(bus=bus)

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

                # 自动回顾触发
                try:
                    from core.kia.epimetheus import AutoRetrospective, generate_retrospective
                    ar = AutoRetrospective()
                    if ar.should_trigger(messages):
                        result = generate_retrospective(
                            task_type=meta.get("source", "unknown"),
                            subtype="",
                            messages=messages,
                            checklist_usage=[],
                        )
                        if result and result.lessons:
                            logger.info(
                                f"[事件总线] 自动复盘生成: {len(result.lessons)} 条教训, "
                                f"gaps={len(result.gaps)}"
                            )
                except Exception:
                    pass

                # Session 跳过检测：如果被蒸馏系统标记为 skipped，自动创建复盘任务
                try:
                    if session_id:
                        import sqlite3
                        from core.config import get_config
                        wiki_state_db = get_config().data_dir / "wiki_state.db"
                        if wiki_state_db.exists():
                            with sqlite3.connect(str(wiki_state_db), timeout=5) as conn:
                                cursor = conn.execute(
                                    "SELECT distill_method FROM processed_sessions WHERE session_id = ?",
                                    (session_id,),
                                )
                                row = cursor.fetchone()
                                if row and row[0] in ("skipped_low_quality", "skipped_by_pipeline"):
                                    from core.app.forced_retrospective import ForcedRetrospective
                                    fr = ForcedRetrospective()
                                    fr._create_from_session_end(session_id, row[0])
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"[事件总线] session.end 处理失败: {e}")

    # 复用 HephaestusWorker 实例，避免事件高频触发时反复新建
    _distill_worker = None

    def _handle_distill_request(event):
        """处理 distill.request 事件：触发蒸馏"""
        try:
            nonlocal _distill_worker
            if _distill_worker is None:
                from core.hephaestus_worker import HephaestusWorker
                _distill_worker = HephaestusWorker()
            processed = _distill_worker.process_all()
            if processed > 0:
                logger.info(f"[事件总线] 处理 {processed} 个蒸馏任务")
        except Exception as e:
            logger.warning(f"[事件总线] distill.request 处理失败: {e}")

    def _handle_polled(event):
        """L1 轮询审计事件：已完成同步侧处理，这里只确认消费。"""
        return {
            "status": "ack",
            "source": event.source,
            "session_id": event.payload.get("session_id"),
        }

    processor.register("session.start", _handle_session_start)
    processor.register("session.end", _handle_session_end)
    processor.register("distill.request", _handle_distill_request)
    processor.register("polled", _handle_polled)

    # KIA 事件触发步骤：由事件总线直接调用
    # 复用 KnowledgeScheduler 实例，避免每次事件新建
    _kia_scheduler = None

    def _handle_page_created(event):
        """页面创建 → 直接触发 connect_worker"""
        try:
            nonlocal _kia_scheduler
            if _kia_scheduler is None:
                from core.kia.chronos import KnowledgeScheduler
                _kia_scheduler = KnowledgeScheduler()
            result = _kia_scheduler.trigger_event("page.created", event.payload)
            logger.info(f"[事件总线] connect_worker: {result.get('status')}")
        except Exception as e:
            logger.warning(f"[事件总线] page.created 处理失败: {e}")

    def _handle_page_modified(event):
        """页面修改 → 直接触发 iteration_tracker"""
        try:
            nonlocal _kia_scheduler
            if _kia_scheduler is None:
                from core.kia.chronos import KnowledgeScheduler
                _kia_scheduler = KnowledgeScheduler()
            result = _kia_scheduler.trigger_event("page.modified", event.payload)
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

    # 注册知识图谱事件处理器（蒸馏完成后自动更新实体和关系）
    try:
        from core.kia.kg_event_handler import KGEventHandler
        _kg_handler = KGEventHandler()

        def _handle_knowledge_distilled(event):
            return _kg_handler.on_distilled(event.payload)

        processor.register("knowledge_distilled", _handle_knowledge_distilled)
        logger.info("[事件总线] 已注册 KGEventHandler 到 knowledge_distilled")
    except Exception as e:
        logger.warning(f"[事件总线] KGEventHandler 注册失败: {e}")

    # 启动新分发线程（与旧轮询双轨运行，逐步过渡）
    try:
        bus.start_dispatch()
        logger.info("[事件总线] 后台分发线程已启动")
    except Exception as e:
        logger.warning(f"[事件总线] 启动分发线程失败: {e}")

    while not stop_event.is_set():
        try:
            stats_before = bus.stats()
            processed = processor.process_all(
                event_types=list(processor._handlers.keys()),
                limit=50,
            )
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


def _print_model_status():
    """CLI: 打印蒸馏评分器模型状态"""
    try:
        from core.scoring.scorers.distill_scorer import DistillScorer
        scorer = DistillScorer()
        status = scorer._scorer.get_status()

        print("\n" + "=" * 50)
        print("🧠 Mnemos 蒸馏评分器模型状态")
        print("=" * 50)
        print(f"  Domain:        {status.get('domain', '?')}")
        print(f"  Mode:          {status.get('mode', '?').upper()}")
        print(f"  Dimensions:    {', '.join(status.get('dimensions', []))}")
        print(f"  Model Version: v{status.get('model_version', 0)}")
        print(f"  Retrain Buffer: {status.get('retrain_buffer_size', 0)} / {status.get('retrain_threshold', 40)}")
        print(f"  Min Samples/Dim: {status.get('min_samples_per_dim', 12)}")
        print(f"  Model Dir:     {status.get('model_dir', '?')}")

        versions = status.get("versions_on_disk", [])
        if versions:
            print(f"\n  📦 磁盘版本 ({len(versions)} 个):")
            for v in versions:
                print(f"    v{v.get('version', '?')} | {v.get('mode', '?')} | {', '.join(v.get('dimensions', []))} | {v.get('timestamp', '?')}")
        else:
            print("\n  📦 磁盘版本: 无 (模型尚未持久化)")

        print("=" * 50 + "\n")
    except Exception as e:
        print(f"获取模型状态失败: {e}")


def _generate_drift_report():
    """CLI: 生成漂移检测报告"""
    try:
        from scripts.drift_report import generate_report
        path = generate_report()
        print(f"\n✅ 漂移检测报告已生成: {path}")
        print("   请用浏览器打开查看")
    except Exception as e:
        print(f"生成报告失败: {e}")


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

    # 4. 资源健康检查（防止重复启动导致资源爆炸）
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        used_fd = len(os.listdir(f"/proc/{os.getpid()}/fd")) if os.path.exists(f"/proc/{os.getpid()}/fd") else 0
        if soft - used_fd < 100:
            warnings.append(f"文件句柄紧张: 已用 {used_fd}/{soft}，daemon 可能因 Too many open files 崩溃")
    except Exception:
        pass

    try:
        events_db = config.data_dir / "events.db"
        if events_db.exists():
            size_mb = events_db.stat().st_size / (1024 * 1024)
            if size_mb > 500:
                warnings.append(f"events.db 过大 ({size_mb:.0f}MB)，建议先运行 `mnemos events cleanup` 清理")
            elif size_mb > 100:
                logger.info(f"[前置检查] events.db {size_mb:.0f}MB，建议定期 cleanup")
    except Exception:
        pass

    # 5. 数据库可访问性
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

    # 注册线程异常钩子，防止未捕获异常导致线程静默崩溃
    def handle_thread_exception(args):
        logger.error(f"未捕获的线程异常 in {args.thread.name}: {args.exc_type.__name__}: {args.exc_value}", exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    threading.excepthook = handle_thread_exception

    # 启动所有服务线程。L1 扫描默认关闭：只消费 MCP/Hook 入队，避免首次运行全量扫历史会话。
    service_defs = [
        ("捕获消费", service_capture_worker, "daemon.services.capture_worker", True),
        ("L1同步", service_l1_sync, "daemon.services.l1_sync", False),
        ("蒸馏合并", service_distill_and_merge, "daemon.services.distill_merge", True),
        ("心跳", service_heartbeat, "daemon.services.heartbeat", True),
        ("收件箱", service_inbox_scanner, "daemon.services.inbox_scanner", True),
        ("画像信号", service_signal_collector, "daemon.services.signal_collector", True),
        ("画像分析", service_persona_analyzer, "daemon.services.persona_analyzer", True),
        ("事件总线", service_event_bus, "daemon.services.event_bus", True),
    ]
    services = []
    for name, func, key, default in service_defs:
        if _service_enabled(config, key, default):
            services.append((name, func))
        else:
            logger.info(f"服务 [{name}] 已禁用 ({key}=false)")

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

    # 等待所有线程退出（最多30秒，给 Worker 足够时间 flush）
    logger.info("等待所有服务停止...")
    for t in threads:
        t.join(timeout=30)
        if t.is_alive():
            logger.warning(f"服务 [{t.name}] 未能在30秒内停止")

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
    os.umask(0o022)  # 安全默认值: owner=rwx, group=rx, other=rx

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
    # 先扫描并终止所有 mnemos_daemon 残留进程
    _kill_all_daemon_processes()

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
            try:
                os.kill(pid, signal.SIGTERM)
                # 等待进程退出
                for _ in range(30):
                    try:
                        os.kill(pid, 0)
                        time.sleep(0.5)
                    except OSError:
                        break
            except OSError:
                pass  # 进程已不存在
        remove_pid()
        print("Mnemos daemon 已停止")
    except Exception as e:
        print(f"停止 daemon 失败: {e}")


def _kill_all_daemon_processes():
    """终止所有 mnemos_daemon.py 进程（清理残留）"""
    try:
        import subprocess
        import platform
        if platform.system() == "Darwin":
            result = subprocess.run(
                ["pgrep", "-f", "mnemos_daemon.py"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode not in (0, 1):
                return
            for pid_str in result.stdout.splitlines():
                pid_str = pid_str.strip()
                if not pid_str.isdigit():
                    continue
                pid = int(pid_str)
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
        elif sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/IM", "python.exe"],
                capture_output=True, timeout=10,
            )
        else:
            result = subprocess.run(
                ["pgrep", "-af", "mnemos_daemon.py"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode not in (0, 1):
                return
            for line in result.stdout.splitlines():
                if "mnemos_daemon.py" in line and "pgrep" not in line:
                    try:
                        pid = int(line.split()[0])
                        os.kill(pid, signal.SIGTERM)
                    except (ValueError, OSError):
                        pass
    except Exception:
        pass


def status_daemon():
    """查看守护进程状态"""
    def _fmt(size: int) -> str:
        value = float(size)
        for unit in ("B", "KB", "MB", "GB"):
            if value < 1024 or unit == "GB":
                return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
            value /= 1024

    def _daemon_process_count() -> int:
        try:
            import subprocess
            import platform
            if platform.system() == "Darwin":
                result = subprocess.run(
                    ["pgrep", "-f", "mnemos_daemon.py"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode not in (0, 1):
                    return 0
                count = 0
                for pid in result.stdout.splitlines():
                    pid = pid.strip()
                    if not pid.isdigit():
                        continue
                    ps_result = subprocess.run(
                        ["ps", "-p", pid, "-o", "args="],
                        capture_output=True, text=True, timeout=5,
                    )
                    if ps_result.returncode == 0:
                        cmd = ps_result.stdout.strip()
                        if "mnemos_daemon.py" in cmd and "pgrep" not in cmd:
                            count += 1
                return count
            else:
                result = subprocess.run(
                    ["pgrep", "-af", "mnemos_daemon.py"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode not in (0, 1):
                    return 0
                return sum(
                    1 for line in result.stdout.splitlines()
                    if "mnemos_daemon.py" in line and "pgrep" not in line
                )
        except Exception:
            return 0

    def _print_runtime_stats(config):
        print(f"\n配置:")
        print(f"  当前读取: {config.config_path}")
        print(f"  配置存在: {'是' if config.config_path.exists() else '否（使用默认值）'}")
        print(f"  数据目录: {config.data_dir}")
        print(f"  Wiki目录: {config.wiki_dir}")
        print(f"  Memos: {'已配置' if config.memos_token else '未配置'}")
        services = config.get("daemon.services", {})
        if services:
            print("  服务开关:")
            for key in sorted(services):
                print(f"    {'✓' if services[key] else '☐'} {key}")

        print(f"\n运行态:")
        print(f"  daemon 进程数: {_daemon_process_count()}")
        if log_file.exists():
            print(f"  daemon.log: {_fmt(log_file.stat().st_size)}")
        events_db = config.data_dir / "events.db"
        if events_db.exists():
            print(f"  events.db: {_fmt(events_db.stat().st_size)}")
            try:
                import sqlite3
                with sqlite3.connect(str(events_db), timeout=5) as conn:
                    pending_total = conn.execute(
                        "SELECT COUNT(*) FROM events WHERE status IN ('pending', 'processing')"
                    ).fetchone()[0]
                    rows = conn.execute(
                        "SELECT event_type, status, COUNT(*) FROM events "
                        "GROUP BY event_type, status ORDER BY COUNT(*) DESC LIMIT 5"
                    ).fetchall()
                print(f"  events pending/processing: {pending_total}")
                for event_type, status, count in rows:
                    print(f"    - {event_type}/{status}: {count}")
            except Exception as e:
                print(f"  events.db 统计失败: {e}")

    if is_daemon_running():
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        print(f"Mnemos daemon 运行中 (PID: {pid})")
        print(f"日志: {log_file}")

        # 显示服务状态
        try:
            from core.config import get_config
            config = get_config()
            _print_runtime_stats(config)

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
        try:
            from core.config import get_config
            _print_runtime_stats(get_config())
        except Exception:
            logger.warning(f"Unexpected error in mnemos_daemon.py", exc_info=True)
        if log_file.exists():
            print(f"\n最近日志:")
            try:
                # 只读取最后 5 行，避免大日志文件导致内存问题
                import subprocess
                result = subprocess.run(
                    ["tail", "-n", "5", str(log_file)],
                    capture_output=True, text=True, timeout=5,
                )
                for line in result.stdout.strip().split("\n"):
                    if line:
                        print(f"  {line}")
            except Exception:
                # 回退：逐行读取最后 5 行
                lines = []
                with open(log_file, "rb") as f:
                    f.seek(0, 2)
                    pos = f.tell()
                    while pos > 0 and len(lines) < 5:
                        pos -= 1
                        f.seek(pos)
                        if f.read(1) == b"\n":
                            line = f.readline().decode("utf-8", errors="ignore").strip()
                            if line:
                                lines.insert(0, line)
                for line in lines:
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
    sub.add_parser("model-status", help="查看蒸馏评分器模型状态")
    sub.add_parser("drift-report", help="生成漂移检测报告 HTML")
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
    elif args.cmd == "model-status":
        _print_model_status()
    elif args.cmd == "drift-report":
        _generate_drift_report()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
